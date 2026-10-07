"""Lay Assessment in QGIS: cable library GeoPackage, the Explorer tab and profile.

Synthetic data only: a 1 km straight lay over a generated sand-wave raster.
"""
import math
import os
import tempfile
import unittest

import numpy as np
from qgis.core import QgsProject, QgsRasterLayer

from ..cable_library import store as library
from ..cable_library.dialog import CableLibraryDialog
from ..explorer.panels.assessment_panel import SEABED_LAY_MODEL, SEABED_RASTER, AssessmentPanel
from ..explorer.panels.seabed_profile_panel import SeabedProfilePanel
from ..laydata import LayDataset
from ..laydata.qc_base import Severity

REQUIRES_QGIS = True

DEG_PER_M = 1.0 / 111320.0
CABLE_ROW = {"name": "Test LW", "category": "cable", "aliases": "LW, LWA", "weight_water_kg_m": 1.0,
             "weight_air_kg_m": 2.0, "cbl_kn": 100.0, "ntts_kn": 80.0, "nots_kn": 50.0, "npts_kn": 20.0}


def _iso(second):
    minute, sec = divmod(int(second), 60)
    hour, minute = divmod(minute, 60)
    return f"2024-01-05T{hour:02d}:{minute:02d}:{sec:02d}"


def sand_wave_raster(folder):
    """GeoTIFF (EPSG:4326, ~1 m cells) of depth 1000 m with 40 m, 1.5 m sand waves along x."""
    from osgeo import gdal, osr

    cols, rows = 1200, 40
    path = os.path.join(folder, "sand_waves.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, cols, rows, 1, gdal.GDT_Float32)
    x0, y0 = -50.0 * DEG_PER_M, 20.0 * DEG_PER_M
    ds.SetGeoTransform((x0, DEG_PER_M, 0.0, y0, 0.0, -DEG_PER_M))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    x_m = (np.arange(cols) + 0.5) - 50.0
    row = 1000.0 + 1.5 * np.sin(2.0 * np.pi * x_m / 40.0)
    ds.GetRasterBand(1).WriteArray(np.tile(row, (rows, 1)).astype(np.float32))
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    ds.FlushCache()
    ds = None
    return path


def lay_dataset(n=501):
    """Touchdown every 2 m along the equator; slack laid for the first half,
    bottom tension (no slack) for the second."""
    kp = np.arange(n) * 0.002
    tension = [0.0] * (n // 2) + [3.0] * (n - n // 2)
    slack = [3.0] * (n // 2) + [0.0] * (n - n // 2)
    columns = {
        "ISO_Time": [_iso(i) for i in range(n)],
        "TD KP": list(kp), "Bot.Tension": tension, "Inst.Bot.Slack": slack,
        "Meas.Top Tension": [10.0 + t for t in tension], "TD Depth": [1000.0] * n,
        "Ship Speed": [1.0] * n, "Payout Speed": [1.02] * n, "Bot.Cable": ["LW"] * n,
        "TD_Lat_dd": [0.0] * n, "TD_Lon_dd": list(kp * 1000.0 * DEG_PER_M),
        "source_file": ["synthetic.csv"] * n,
    }
    return LayDataset(columns)


class _MapSync:
    def __init__(self):
        self.lines = []

    def highlight_polyline(self, lonlat, zoom=False):
        self.lines.append((list(lonlat), zoom))


class _Controller:
    """The slice of the Explorer window the panels use."""

    def __init__(self):
        self.map_sync = _MapSync()
        self.selected = []
        self.profile = None

    def seabed_profile_panel(self, create=False):
        if self.profile is None and create:
            self.profile = SeabedProfilePanel(self)
        return self.profile

    def select_rows(self, rows):
        self.selected = list(rows)

    def layer_name(self):
        return "synthetic"

    def broadcast_hover(self, row, origin=None):
        pass

    def highlight_record(self, row, from_plot=False):
        pass

    def go_to_record(self, row):
        pass


class CableLibraryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.path = os.path.join(self.folder, "library.gpkg")

    def test_create_write_read_and_match(self):
        library.create_library(self.path)
        self.assertTrue(library.is_library(self.path))
        rope = {"name": "Rope 20", "category": "rope", "weight_water_kg_m": "0,3", "cbl_kn": "250"}
        library.write_rows(self.path, [CABLE_ROW, rope])
        rows = library.read_rows(self.path)
        self.assertEqual([r["name"] for r in rows], ["Test LW", "Rope 20"])
        self.assertAlmostEqual(rows[1]["weight_water_kg_m"], 0.3)
        self.assertEqual(library.find_row(rows, "lwa")["name"], "Test LW")
        self.assertIsNone(library.find_row(rows, "DA"))
        props = library.to_props(rows[0])
        self.assertAlmostEqual(props.w_water_npm, 9.80665)
        with self.assertRaises(ValueError):
            library.write_rows(self.path, [CABLE_ROW, dict(CABLE_ROW)])
        self.assertEqual(len(library.read_rows(self.path)), 2)  # failed write changed nothing

    def test_csv_round_trip_and_warnings(self):
        csv_path = os.path.join(self.folder, "types.csv")
        bad = dict(CABLE_ROW, name="Odd", npts_kn=60.0)  # NPTS above NOTS
        library.write_csv(csv_path, [CABLE_ROW, bad])
        rows = library.read_csv(csv_path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["aliases"], "LW, LWA")
        self.assertTrue(any("NPTS" in note for note in library.warnings_for(rows)))

    def test_dialog_edits_and_saves(self):
        library.create_library(self.path)
        dialog = CableLibraryDialog(path=self.path)
        try:
            dialog._add_row()
            row = dialog.table.rowCount() - 1
            for name, value in (("name", "New type"), ("weight_water_kg_m", "0.8"), ("npts_kn", "15")):
                dialog.table.item(row, library.COLUMN_NAMES.index(name)).setText(value)
            self.assertTrue(dialog.save())
            rows = library.read_rows(self.path)
            self.assertEqual(rows[0]["name"], "New type")
            self.assertAlmostEqual(rows[0]["npts_kn"], 15.0)
        finally:
            dialog._dirty = False
            dialog.deleteLater()


class AssessmentPanelTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.controller = _Controller()
        self.panel = AssessmentPanel(self.controller)
        self.panel.persist = False
        self.panel.synchronous = True
        self.panel._library_rows = [library.clean_row(CABLE_ROW)]
        self.panel.cable_combo.addItem("Test LW", "Test LW")
        self.panel.cable_combo.setCurrentIndex(self.panel.cable_combo.count() - 1)
        self.panel.units_combo.setCurrentText("kN")
        for box in self.panel._check_boxes.values():
            box.setChecked(True)
        self.panel.set_dataset(lay_dataset())

    def tearDown(self):
        self.panel.deleteLater()
        if self.controller.profile is not None:
            self.controller.profile.deleteLater()

    def test_columns_detected(self):
        mapping = self.panel.mapping()
        self.assertEqual(mapping.get("kp"), "TD KP")
        self.assertEqual(mapping.get("bottom_tension"), "Bot.Tension")
        self.assertEqual(mapping.get("cable_type"), "Bot.Cable")

    def test_raster_seabed_spans_where_tensioned(self):
        raster = QgsRasterLayer(sand_wave_raster(self.folder), "sand waves")
        self.assertTrue(raster.isValid())
        QgsProject.instance().addMapLayer(raster)
        self.addCleanup(QgsProject.instance().removeMapLayer, raster.id())
        self.panel.seabed_combo.setCurrentIndex(self.panel.seabed_combo.findData(SEABED_RASTER))
        self.panel.raster_combo.setLayer(raster)
        self.panel.interval_spin.setValue(1)
        self.panel.run()
        model = self.panel._model
        self.assertIsNotNone(model, self.panel.status_label.text())
        spans = [f for f in self.panel._findings if f.check_id == "suspension"]
        self.assertTrue(spans, self.panel.status_label.text())
        self.assertTrue(all(f.kp_start > 0.49 for f in spans), [f.kp_start for f in spans])
        terrain = [f for f in self.panel._findings if f.check_id == "terrain_slack"]
        self.assertTrue(terrain and all(f.kp_start >= 0.4 for f in terrain), terrain)
        # Sand waves: about 1.4 % more seabed than plan, so the 3 % slack half is clear.
        self.assertFalse([f for f in terrain if f.kp_end <= 0.4])
        self.assertFalse(any(f.severity == Severity.ERROR and f.check_id == "top_tension"
                             for f in self.panel._findings))
        profile = self.controller.profile
        self.assertIsNotNone(profile)
        self.assertIs(profile._model, model)
        self.assertTrue(profile.measure.series)
        # Selecting a range highlights its track on the map; double-click selects records.
        self.panel.table.selectRow(0)
        self.assertTrue(self.controller.map_sync.lines)
        self.panel._on_double_clicked(0, 0)
        self.assertTrue(self.controller.selected)
        layer = self.panel.build_ranges_layer("ranges")
        self.assertEqual(layer.featureCount(), len(self.panel._findings))
        csv_path = os.path.join(self.folder, "ranges.csv")
        self.panel.write_csv(csv_path)
        with open(csv_path, encoding="utf-8") as handle:
            self.assertEqual(len(handle.readlines()), len(self.panel._findings) + 1)

    def test_lay_model_seabed_and_record_checks(self):
        self.panel.seabed_combo.setCurrentIndex(self.panel.seabed_combo.findData(SEABED_LAY_MODEL))
        self.panel.run()
        self.assertIsNotNone(self.panel._model)
        ids = {f.check_id for f in self.panel._findings}
        self.assertIn("laid_under_tension", ids)
        self.assertNotIn("suspension", ids)  # the lay model's own seabed is flat
        self.assertIn("Slack vs seabed", self.panel.status_label.text())  # skipped with a reason
        laid = next(f for f in self.panel._findings if f.check_id == "laid_under_tension")
        self.assertAlmostEqual(laid.kp_start, 0.5, places=3)
        self.assertTrue(math.isclose(laid.value, 3.0))


class ExplorerWindowTests(unittest.TestCase):
    def test_tab_and_profile_dock(self):
        from qgis.gui import QgsMapCanvas

        from ..explorer import CableLayExplorerWindow

        class _Iface:
            def __init__(self):
                self.canvas = QgsMapCanvas()

            def mapCanvas(self):
                return self.canvas

        iface = _Iface()
        window = CableLayExplorerWindow(iface)
        try:
            tabs = [window.analysis_tabs.tabText(i) for i in range(window.analysis_tabs.count())]
            self.assertIn("Lay Assessment", tabs)
            self.assertIsNone(window.seabed_profile_panel())
            panel = window.seabed_profile_panel(create=True)
            self.assertIsInstance(panel, SeabedProfilePanel)
            self.assertIs(window.seabed_profile_panel(), panel)
            window.broadcast_hover(0)  # no dataset: must not fail
        finally:
            # No shutdown()/close(): those save window state to the tester's QSettings.
            window.assessment_panel.shutdown()
            window.map_sync.cleanup()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
