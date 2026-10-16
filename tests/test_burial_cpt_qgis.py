"""CPTs in the Burial Planner and Processing: storage, placement, map layer,
Ground Model display, log window, CPT datasets and the import algorithm."""
import os
import shutil
import tempfile
import unittest

from qgis.core import (QgsCoordinateReferenceSystem, QgsDistanceArea, QgsGeometry, QgsProcessingContext,
                       QgsProcessingFeedback, QgsProject)
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtWidgets import QApplication

from ..burial import cpt_register, map_layers, numeric_datasets as sources, schema
from ..burial.cpt_import_dialog import CptImportDialog, ags_candidates
from ..burial.numeric_profile_dialogs import DatasetDialog
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..burial.tabs.ground_tab import MODE_CPT, MODE_NUMERIC, GroundTab
from ..kp_geo_utils import RouteFrame
from ..processing.import_ags_cpt_algorithm import ImportAgsCptAlgorithm, reading_field

REQUIRES_QGIS = True

# Two seabed CPTs, one per file, with a generic location ID and no position:
# positions come from the summary table (degrees and decimal minutes).
_AGS = "\r\n".join([
    '"**PROJ"', '"*PROJ_ID","*PROJ_NAME","*PROJ_LOC","*PROJ_AGS"', '"<UNITS>","","",""',
    '"","Test","Route A","3.1"', "",
    '"**HOLE"', '"*HOLE_ID","*HOLE_LOCZ"', '"<UNITS>","m"', '"0001",""', "",
    '"**STCN"', '"*HOLE_ID","*STCN_DPTH","*STCN_RES","*STCN_FRES","*STCN_PWP1"',
    '"<UNITS>","m","MPa","MPa","MPa"'] + [
    f'"0001","{z / 100:.2f}","{0.05 + z / 400:.3f}","{0.001 if z % 3 else 0.002:.3f}","{z / 10000:.4f}"'
    for z in range(0, 201, 2)] + [""])
_TABLE = "\n".join([
    "Project name:,Test",
    "CPT ID,Water depth (m),Longitude,Latitude,Penetration (m),Horizon description 1,Horizon description 2,"
    "Class 1,Class 2,Interface depth (m) 1,Summary",
    "CP-1,40,\"0° 03.000' E\",\"50° 00.010' N\",2,Soft CLAY,Firm CLAY,A,B,1.2,Soft over firm",
    "CP-2,41,\"0° 03.600' E\",\"49° 59.990' N\",2,Soft CLAY,,A,,,Soft",
    ""])


def route():
    distance = QgsDistanceArea()
    distance.setSourceCrs(QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance().transformContext())
    distance.setEllipsoid("WGS84")
    return RouteFrame.from_source([QgsGeometry.fromWkt("LINESTRING(0 50, 0.1 50)")], distance, start_kp_km=10)


class Dock:
    def __init__(self):
        self.calls = []

    def highlight_kp(self, kp):
        self.calls.append(("kp", round(kp, 3)))

    def flash_point(self, lon, lat):
        self.calls.append(("point", round(lon, 4), round(lat, 4)))


class CptQgisTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix=f"cpt{os.getpid()}_")
        self.files = []
        for name in ("CP-1", "CP-2"):
            path = os.path.join(self.folder, name)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(_AGS)
            self.files.append(path)
        self.table = os.path.join(self.folder, "summary.csv")
        with open(self.table, "w", encoding="utf-8", newline="") as handle:
            handle.write(_TABLE)
        self.store = BurialStore(os.path.join(self.folder, "plans.gpkg"))
        self.store.migrate()
        self.plan_id = self.store.save_plan({"name": "CPT", "method": "plough", "scope_start_kp": 10,
                                             "scope_end_kp": 17, "target_burial_m": 1.5})
        self.model = PlanModel(self.store)
        self.model.load_plan(self.plan_id)
        self.model.route = route()
        self.config = {"files": [{"path": p} for p in self.files], "name_rule": "auto",
                       "positions": {"path": self.table, "header_row": 2, "coords": "latlon", "loose": True,
                                     "columns": {"id": "CPT ID", "lat": "Latitude", "lon": "Longitude",
                                                 "water_depth": "Water depth (m)", "penetration": "Penetration (m)",
                                                 "summary": "Summary", "horizon_desc": "Horizon description 1",
                                                 "horizon_class": "Class 1",
                                                 "horizon_interface": "Interface depth (m) 1"}}}
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.hide()
            window.deleteLater()
        layer = map_layers.find_layer(QgsProject.instance(), self.store.gpkg_path, schema.CPT_LAYER_NAME)
        if layer is not None:
            QgsProject.instance().removeMapLayer(layer.id())
        self.model.close_plan()
        self.store.close()
        QApplication.processEvents()
        shutil.rmtree(self.folder, ignore_errors=True)

    def campaign(self):
        built = cpt_register.build(self.config, campaign_id="c1")
        return ({"campaign_id": "c1", "name": "Route A CPTs", "originator": "Survey Co", "revision": "Rev 0",
                 "config": cpt_register.stamp_fingerprints(self.config)}, built)

    def test_build_joins_the_table_and_places_on_the_route(self):
        _campaign, built = self.campaign()
        one, two = built["cpts"]
        self.assertEqual((one["name"], two["name"]), ("CP-1", "CP-2"))
        self.assertAlmostEqual(one["lat"], 50 + 0.01 / 60)
        self.assertAlmostEqual(one["lon"], 0.05)
        self.assertEqual((one["position_source"], one["water_depth_m"], one["final_depth_m"]), ("table", 40.0, 2.0))
        self.assertEqual([(l["top"], l["base"], l["soil_class"]) for l in one["layers"]],
                         [(0.0, 1.2, "A"), (1.2, 2.0, "B")])
        self.assertEqual(one["readings"], 101)
        self.assertIn("u2", built["readings"][one["cpt_id"]]["channels"])
        rows, notes = cpt_register.place(self.model.route, built["cpts"], 250.0, 100.0)
        first, second = rows
        self.assertEqual((first["status"], second["status"]), ("placed", "placed"))
        self.assertAlmostEqual(first["offset_m"], -18.5, delta=1.0)   # north of an eastward route: port
        self.assertAlmostEqual(second["offset_m"], 18.5, delta=1.0)
        self.assertEqual(cpt_register.side_text(first["offset_m"]), "19 m port")
        middle = (first["kp"] + second["kp"]) / 2
        self.assertAlmostEqual(first["end_kp"], min(middle, first["kp"] + 0.1))
        self.assertAlmostEqual(first["start_kp"], first["kp"] - 0.1)
        rows, notes = cpt_register.place(self.model.route, built["cpts"], 10.0, 100.0)
        self.assertEqual({r["status"] for r in rows}, {"too far"})
        self.assertTrue(any("more than 10 m" in n for n in notes))

    def test_storage_layer_reload_and_removal(self):
        campaign, built = self.campaign()
        self.assertEqual(self.model.save_cpt_campaign(campaign, built["cpts"], built["readings"]), "c1")
        self.assertEqual(int(self.store.read_meta()["schema_version"]), schema.SCHEMA_VERSION)
        (row,) = self.store.list_cpt_campaigns()
        self.assertEqual((row["name"], row["revision"], len(row["config"]["files"])), ("Route A CPTs", "Rev 0", 2))
        cpts = self.store.list_cpts()
        self.assertEqual([c["name"] for c in cpts], ["CP-1", "CP-2"])
        readings = self.store.cpt_readings([cpts[0]["cpt_id"]])
        self.assertEqual(len(readings[cpts[0]["cpt_id"]]["depth"]), 101)
        layer = self.store.open_layer(schema.CPT_LAYER_NAME)
        self.assertEqual(layer.featureCount(), 2)
        self.assertEqual(cpt_register.changed_sources(row), [])
        with open(self.files[0], "a", encoding="utf-8") as handle:
            handle.write('"0001","2.02","0.5","0.001","0.02"\r\n')
        os.utime(self.files[0], (1, 1))
        self.assertEqual(cpt_register.changed_sources(row), ["CP-1 has changed"])
        # A plan duplicate shares the project's CPTs; settings are project-wide.
        self.model.save_cpt_settings({"nkt": 12})
        self.assertEqual(self.model.cpt_settings()["nkt"], 12.0)
        self.assertTrue(self.model.save_cpt_view({"max_offset_m": 50.0}))
        self.assertEqual(self.model.cpt_view()["max_offset_m"], 50.0)
        self.assertTrue(self.model.delete_cpt_campaign("c1"))
        self.assertEqual(self.store.list_cpts(), [])
        self.assertEqual(self.store.cpt_readings(), {})
        self.assertEqual(self.store.open_layer(schema.CPT_LAYER_NAME).featureCount(), 0)

    def test_ground_model_cpt_display_traces_readout_and_logs(self):
        campaign, built = self.campaign()
        dock = Dock()
        tab = GroundTab(self.model, dock)
        tab.cpts.save(campaign, built["cpts"], built["readings"])
        QCoreApplication.processEvents()
        tab.display_mode.setCurrentIndex(MODE_CPT)
        QCoreApplication.processEvents()
        self.assertTrue(tab.cpts.isVisibleTo(tab) and not tab.soil_editor.isVisibleTo(tab))
        self.assertEqual(tab.cpts.table.rowCount(), 2)
        self.assertEqual(tab.cpts.table.item(0, 3).text(), "placed")
        entries = tab.plot._cpt_item.entries()
        self.assertEqual([e["name"] for e in entries], ["CP-1", "CP-2"])
        self.assertEqual(tab.plot._cpt_item.full_scale, 1.0)            # qc up to 0.55 MPa, rounded up
        kp = entries[0]["kp"]
        text = tab.plot._readout_text(kp, 1.0)
        self.assertIn("CPT CP-1", text)
        self.assertIn("qc 0.3 MPa", text)
        self.assertTrue(text.startswith(f"KP {kp:.3f}"))                 # ground readout kept
        self.assertIsNone(tab.plot.cpt_at(kp, 9.0))                       # below the deepest reading
        self.assertNotIn("CPT CP-1", tab.plot._readout_text(kp, 9.0))
        tab.cpts.table.selectRow(0)
        QCoreApplication.processEvents()
        self.assertIn(("point", 0.05, round(50 + 0.01 / 60, 4)), dock.calls)
        # Selecting a CPT on the map selects it in the table.
        layer = map_layers.find_layer(QgsProject.instance(), self.store.gpkg_path, schema.CPT_LAYER_NAME)
        self.assertIsNotNone(layer)
        second = next(f.id() for f in layer.getFeatures() if f["name"] == "CP-2")
        layer.selectByIds([second])
        QCoreApplication.processEvents()
        self.assertEqual(tab.cpts.selected_ids(), [entries[1]["cpt_id"]])
        self.model.save_cpt_view({"show": False})
        QCoreApplication.processEvents()
        self.assertFalse(tab.plot._cpt_item.isVisible())
        tab.cpts.open_logs([entries[0]["cpt_id"], entries[1]["cpt_id"]])
        window = tab.cpts._log_window
        self.windows.append(window)
        self.assertEqual((window.current, window.compare), (entries[0]["cpt_id"], [entries[1]["cpt_id"]]))
        self.assertIn("qc", window._track_plots)
        self.assertIn("CP-2", window.readout_text(1.0))
        headers, rows = window.table_rows()
        self.assertEqual(len(rows), 202)
        self.assertIn("Su — Undrained shear strength (kPa)", headers)
        path = os.path.join(self.folder, "logs.csv")
        self.assertEqual(window.export_csv(path), path)
        window.show_cpts([entries[1]["cpt_id"]])
        window._step(-1)
        self.assertEqual(window.current, entries[0]["cpt_id"])
        window.set_track_log("qc", True, persist=False)
        window.set_track_visible("dr", True, persist=False)
        window.set_bands_visible(False, persist=False)
        self.assertIn("dr", window.visible_tracks)
        # Removing the import while its log is open leaves an empty, working window.
        self.assertTrue(self.model.delete_cpt_campaign("c1"))
        QCoreApplication.processEvents()
        self.assertEqual((window.shown(), window.current), ([], ""))
        window.set_track_visible("bq", True, persist=False)
        self.assertEqual(window.readout_text(1.0), "<b>1.00 m</b> below seabed")

    def test_cpt_numeric_dataset_and_live_placement(self):
        campaign, built = self.campaign()
        self.model.save_cpt_campaign(campaign, built["cpts"], built["readings"])
        tab = GroundTab(self.model, Dock())
        prefill = tab.numeric.cpt_prefill("su")
        self.assertEqual(prefill["config"]["colours"]["classes"][0]["label"], "Extremely low")
        dialog = DatasetDialog(self.model, None, [], tab, prefill=prefill)
        dialog._check_measurements()
        self.assertTrue(dialog.measure_check.text().startswith("✓ 2 investigation(s)"), dialog.measure_check.text())
        dialog._check_placement()
        self.assertIn("2 KP range(s) for 2 investigation(s)", dialog.place_check.text())
        dialog._accept()
        dataset, profiles = dialog.result()
        self.assertEqual(dataset["config"]["placement"]["kind"], sources.PLACEMENT_CPT)
        self.assertEqual(sorted(p["source_id"] for p in profiles), ["CP-1", "CP-2"])
        self.assertTrue(tab.numeric.save_dataset(dataset, profiles))
        tab.display_mode.setCurrentIndex(MODE_NUMERIC)
        QCoreApplication.processEvents()
        tab.numeric.render()
        self.assertIn("2 placed", tab.numeric.status.text())
        stored = self.store.list_ground_datasets()[0]
        self.assertIsNone(sources.source_changed(stored, self.model))
        self.model.save_cpt_settings({"nkt": 10})
        self.assertIn("CPT settings have changed", sources.source_changed(stored, self.model))
        profiles, _config = sources.reload_measurements(stored, self.model)
        self.assertEqual(len(profiles), 2)
        assignments, _notes, layer = sources.read_placement(self.model, dict(stored["config"]["placement"],
                                                                             max_offset_m=5.0))
        self.assertEqual((assignments, layer), ([], None))

    def test_superseded_imports_and_shared_names(self):
        campaign, built = self.campaign()
        self.model.save_cpt_campaign(campaign, built["cpts"], built["readings"])
        again = cpt_register.build(self.config, campaign_id="c2")
        self.model.save_cpt_campaign({"campaign_id": "c2", "name": "Rev 1", "seq": 1,
                                      "config": cpt_register.stamp_fingerprints(self.config)},
                                     again["cpts"], again["readings"])
        grid = sources.cpt_grid(self.model, {"kind": "cpt", "channel": "qc", "interval_m": 0.5})
        self.assertEqual(sorted({row[0] for row in grid[1:]}),
                         ["CP-1 [Rev 1]", "CP-1 [Route A CPTs]", "CP-2 [Rev 1]", "CP-2 [Route A CPTs]"])
        tab = GroundTab(self.model, Dock())
        self.assertEqual(sum(1 for p in tab.cpts.placements.values() if p["status"] == "placed"), 4)
        rows = self.store.list_cpt_campaigns()
        old = next(r for r in rows if r["campaign_id"] == "c1")
        _o, old_built = self.campaign()
        self.model.save_cpt_campaign(dict(old, status="superseded"), old_built["cpts"], old_built["readings"])
        QCoreApplication.processEvents()
        statuses = sorted(p["status"] for p in tab.cpts.placements.values())
        self.assertEqual(statuses, ["placed", "placed", "superseded import", "superseded import"])
        self.assertEqual([e["name"] for e in tab.plot._cpt_item.entries()], ["CP-1", "CP-2"])
        grid = sources.cpt_grid(self.model, {"kind": "cpt", "channel": "qc", "interval_m": 0.5})
        self.assertEqual(sorted({row[0] for row in grid[1:]}), ["CP-1", "CP-2"])
        assignments, _notes = sources.cpt_assignments(self.model, {"kind": "cpt_points", "max_offset_m": 0.0})
        self.assertEqual(assignments, [])                                # 0 m is kept, not the default

    def test_import_dialog(self):
        self.assertEqual(sorted(os.path.basename(p) for p in ags_candidates(self.folder)), ["CP-1", "CP-2"])
        dialog = CptImportDialog(self.model, None)
        self.windows.append(dialog)
        dialog._add_paths(self.files)
        dialog._rebuild()
        self.assertIn("✓ 2 CPT(s)", dialog.files_check.text())
        self.assertTrue(dialog.ags3_pore.isVisibleTo(dialog))
        dialog._open_table(self.table, "", {})
        self.assertEqual(dialog.header_row.value(), 2)          # below the title row
        dialog._fill_columns(self.config["positions"]["columns"])
        dialog._rebuild()
        self.assertIn("2 of 2 CPT(s) matched a table row", dialog.table_check.text())
        self.assertEqual(dialog.preview_rows[0][4], "table")
        self.assertEqual(dialog.preview_rows[0][13], "placed")
        dialog.originator.setText("Survey Co")
        dialog._accept()
        campaign, cpts, readings = dialog.result()
        self.assertEqual((campaign["originator"], len(cpts), len(readings)), ("Survey Co", 2, 2))
        self.assertEqual(campaign["config"]["positions"]["columns"]["id"], "CPT ID")
        self.assertTrue(all(f["fingerprint"] for f in campaign["config"]["files"]))

    def test_chosen_worksheet_is_kept_from_any_tab(self):
        import openpyxl
        path = os.path.join(self.folder, "summary.xlsx")
        book = openpyxl.Workbook()
        book.active.title = "Info"
        book.active.append(["Notes"])
        sheet = book.create_sheet("Rev1")
        for line in _TABLE.splitlines()[1:3]:
            sheet.append(next(__import__("csv").reader([line])))
        book.save(path)
        dialog = CptImportDialog(self.model, None)
        self.windows.append(dialog)
        dialog._add_paths(self.files)
        dialog._open_table(path, "Rev1", {})
        dialog.tabs.setCurrentIndex(2)          # the sheet combo is hidden with its tab
        self.assertEqual(dialog.current_config()["positions"]["sheet"], "Rev1")

    def test_processing_algorithm(self):
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        algorithm = ImportAgsCptAlgorithm()
        algorithm.initAlgorithm({})
        params = {"INPUT": self.files, "POSITIONS": self.table, "ID_COLUMN": "CPT ID",
                  "LAT_COLUMN": "Latitude", "LON_COLUMN": "Longitude", "WATER_DEPTH_COLUMN": "Water depth (m)",
                  "HORIZON_DESC_COLUMN": "Horizon description 1", "INTERFACE_COLUMN": "Interface depth (m) 1",
                  "OUTPUT_LOCATIONS": "memory:", "OUTPUT_READINGS": "memory:", "OUTPUT_STRATA": "memory:"}
        results, ok = algorithm.run(params, context, QgsProcessingFeedback())
        self.assertTrue(ok, results)
        locations = context.takeResultLayer(results["OUTPUT_LOCATIONS"])
        readings = context.takeResultLayer(results["OUTPUT_READINGS"])
        strata = context.takeResultLayer(results["OUTPUT_STRATA"])
        self.assertEqual(locations.featureCount(), 2)
        feature = next(locations.getFeatures())
        self.assertEqual((feature["name"], feature["water_depth_m"]), ("CP-1", 40.0))
        self.assertAlmostEqual(feature.geometry().asPoint().y(), 50 + 0.01 / 60)
        self.assertEqual(readings.featureCount(), 202)
        for name in ("qc_mpa", "fs_kpa", "u2_kpa", "qt_mpa", "su_kpa", "sbtn_zone", "ic"):
            self.assertIn(name, readings.fields().names())
        self.assertEqual(reading_field("rf"), "rf_pct")
        self.assertEqual(strata.featureCount(), 3)
        params["ID_COLUMN"] = "Nope"
        results, ok = algorithm.run(params, context, QgsProcessingFeedback())
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
