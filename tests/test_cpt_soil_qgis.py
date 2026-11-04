"""CPT soil constraints: geometry, saved sources and Qt widget lifetimes."""
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
                       QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QCoreApplication, QEvent
from qgis.PyQt.QtWidgets import QApplication
from qgis.PyQt.QtTest import QTest

from ..burial import numeric_datasets as sources, numeric_profiles as numeric
from ..burial.cpt_soil import unit_runs
from ..burial.ground_plot import GroundModelPlot
from ..burial.numeric_profile_dialogs import DatasetDialog
from ..burial.numeric_profile_panel import NumericProfilePanel
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..kp_geo_utils import RouteFrame

REQUIRES_QGIS = True


def pump(wait=0):
    if wait:
        QTest.qWait(wait)
    for _ in range(4):
        QApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, getattr(QEvent, "Type", QEvent).DeferredDelete)


@contextmanager
def no_qt_errors():
    errors, original = [], sys.excepthook
    sys.excepthook = lambda kind, value, tb: errors.append((kind.__name__, str(value)))
    try:
        yield
    finally:
        sys.excepthook = original
    assert not errors, errors


def box(x0, x1, y0=-200, y1=200):
    return f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"


class SoilConstraintTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="soil_section_")
        self.project = QgsProject.instance()
        self.layers, self.windows = [], []
        self.path = os.path.join(self.folder, "plans.gpkg")
        self.store = BurialStore(self.path)
        self.store.migrate()
        self.plan_id = self.store.save_plan({"name": "Synthetic soil section", "method": "plough",
                                            "scope_start_kp": 10, "scope_end_kp": 14, "target_burial_m": 1.5})
        self.model = PlanModel(self.store)
        self.model.load_plan(self.plan_id)
        self.model.route = self.route()
        self.cpts(("A", 500, 0))
        self.profiles = numeric.import_profiles([["A", 0, 4], ["A", 1, 20]],
                                                {"source_id": 0, "depth": 1, "value": 2},
                                                dataset_id="dataset", variable="Su", units="kPa")

    def tearDown(self):
        for window in self.windows:
            try:
                window.close()
                window.deleteLater()
            except RuntimeError:
                pass
        self.model.close_plan()
        pump()
        for layer_id in self.layers:
            self.project.removeMapLayer(layer_id)
        self.store.close()
        pump()
        shutil.rmtree(self.folder, ignore_errors=True)

    def route(self, wkt="LINESTRING(0 0,4000 0)", start=10, distance=None):
        if distance is None:
            distance = QgsDistanceArea()
            distance.setSourceCrs(QgsCoordinateReferenceSystem("EPSG:3857"), self.project.transformContext())
            distance.setEllipsoid("NONE")
        return RouteFrame.from_source([QgsGeometry.fromWkt(wkt)], distance, start_kp_km=start)

    def cpts(self, *points, crs="EPSG:3857"):
        transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem(crs),
                                            QgsCoordinateReferenceSystem("EPSG:4326"), self.project)
        rows = []
        for name, x, y in points:
            p = transform.transform(QgsPointXY(x, y))
            rows.append(dict(cpt_id=name, name=name, lon=p.x(), lat=p.y()))
        self.store.save_cpt_campaign({"campaign_id": "synthetic", "name": "Synthetic", "status": "current"}, rows, {})
        self.model.cpts.invalidate()

    def soils(self, *features, crs="EPSG:3857"):
        layer = QgsVectorLayer(f"MultiPolygon?crs={crs}&field=unit:string", "Mapped soil units", "memory")
        for wkt, unit in features:
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromWkt(wkt))
            feature.setAttributes([unit])
            self.assertTrue(layer.dataProvider().addFeatures([feature])[0])
        layer.updateExtents()
        self.project.addMapLayer(layer)
        self.layers.append(layer.id())
        return layer

    def placement(self, layer, **updates):
        return {"kind": "cpt_points", "max_offset_m": 500, "half_range_m": 10000,
                "soil_constraint": {"enabled": True, "layer": sources.layer_ref(layer), "unit_field": "unit"},
                **updates}

    def spans(self, placement):
        rows, notes, _ = sources.read_placement(self.model, placement)
        return [(r["source_id"], round(r["start_kp"], 6), round(r["end_kp"], 6)) for r in rows], notes

    def dataset(self, placement):
        return {"dataset_id": "dataset", "name": "Soil-constrained Su", "variable": "Su", "units": "kPa",
                "config": {"placement": placement, "colours": {}, "measurements": {}}}

    def panel(self, placement):
        self.store.save_ground_dataset(self.dataset(placement), self.profiles)
        self.model.update_gen_params({"numeric_ground": {"dataset_id": "dataset"}}, stale=False)
        plot = GroundModelPlot()
        panel = NumericProfilePanel(self.model, None, plot)
        self.windows.extend([panel, plot])
        panel.active = True
        panel.refresh()
        return panel

    def test_midpoints_stop_at_unit_change_and_never_jump_to_same_class(self):
        self.cpts(("A", 500, 0), ("B", 3500, 0))
        soil = self.soils((box(0, 1500), "clay"), (box(1500, 2500), "rock"), (box(2500, 4000), "clay"))
        spans, notes = self.spans(self.placement(soil))
        self.assertEqual(spans, [("A", 10, 11.5), ("B", 12.5, 14)])
        self.assertIn("2 shortened", notes[0])
        legacy = self.placement(soil)
        legacy.pop("soil_constraint")
        self.assertEqual(self.spans(legacy)[0], [("A", 10, 12), ("B", 12, 14)])

    def test_same_unit_seams_merge_and_edited_limits_remain_limits(self):
        soil = self.soils((box(0, 1000), "clay"), (box(1000, 4000), "clay"))
        self.assertEqual(self.spans(self.placement(soil))[0], [("A", 10, 14)])
        settings = self.placement(soil, cpt_limits={"A": {"before_m": 100, "after_m": 200}})
        self.assertEqual(self.spans(settings)[0], [("A", 10.4, 10.7)])

    def test_conflicting_and_unclassified_overlaps_are_unknown(self):
        for unit in ("rock", None):
            soil = self.soils((box(0, 4000), "clay"), (box(900, 1100), unit))
            self.assertEqual(self.spans(self.placement(soil))[0], [("A", 10, 10.9)])
            self.cpts(("A", 1000, 0))
            spans, notes = self.spans(self.placement(soil))
            self.assertEqual(spans, [])
            self.assertIn("ambiguous", " ".join(notes))
            self.cpts(("A", 500, 0))

    def test_polygon_holes_and_unmapped_gaps_stop_influence(self):
        soil = self.soils(("POLYGON((0 -200,4000 -200,4000 200,0 200,0 -200),"
                           "(1000 -100,2000 -100,2000 100,1000 100,1000 -100))", "clay"))
        self.assertEqual(self.spans(self.placement(soil))[0], [("A", 10, 11)])

    def test_projection_cannot_cross_a_different_or_unmapped_lateral_strip(self):
        self.cpts(("A", 500, 150))
        soil = self.soils((box(0, 4000, -50, 50), "clay"), (box(0, 4000, 100, 200), "clay"))
        spans, notes = self.spans(self.placement(soil))
        self.assertEqual(spans, [])
        self.assertIn("projection crosses", " ".join(notes))

    def test_missing_layer_field_invalid_geometry_and_unmapped_cpt_do_not_fall_back(self):
        soil = self.soils((box(1000, 4000), "clay"))
        self.assertEqual(self.spans(self.placement(soil))[0], [])
        settings = self.placement(soil)
        settings["soil_constraint"]["unit_field"] = "missing"
        rows, notes, watched = sources.read_placement(self.model, settings)
        self.assertEqual(rows, [])
        self.assertIs(watched, soil)
        self.assertIn("no unit field", notes[0])
        settings = self.placement(soil)
        self.project.removeMapLayer(soil.id())
        self.assertEqual(self.spans(settings)[0], [])
        self.assertIn("unavailable", " ".join(self.spans(settings)[1]))
        invalid = self.soils(("POLYGON((0 0,4000 100,0 100,4000 0,0 0))", "clay"))
        self.assertIn("invalid geometry", " ".join(self.spans(self.placement(invalid))[1]))

    def test_geodesic_grid_and_nonzero_kp_are_used_consistently(self):
        from ..kp_range_utils import GridDistanceArea
        crs = QgsCoordinateReferenceSystem("EPSG:4326")
        context = self.project.transformContext()
        distance = QgsDistanceArea()
        distance.setSourceCrs(crs, context)
        distance.setEllipsoid("WGS84")
        grid = GridDistanceArea(crs, context, QgsCoordinateReferenceSystem("EPSG:3857"))
        self.cpts(("A", .004, 60), crs="EPSG:4326")
        soil = self.soils((box(.002, .008, 59.99, 60.01), "clay"), crs="EPSG:4326")
        widths = []
        for measure in (distance, grid):
            self.model.route = self.route("LINESTRING(0 60,0.01 60)", 25, measure)
            rows, _, _ = sources.read_placement(self.model, self.placement(soil))
            self.assertEqual(len(rows), 1)
            length = self.model.route.total_length_km
            self.assertAlmostEqual(rows[0]["start_kp"], 25 + length * .2)
            self.assertAlmostEqual(rows[0]["end_kp"], 25 + length * .8)
            widths.append(rows[0]["end_kp"] - rows[0]["start_kp"])
        self.assertGreater(widths[1], widths[0] * 1.9)

    def test_retraced_and_disconnected_routes_do_not_extend_influence(self):
        soil = self.soils((box(0, 1000), "clay"), (box(3000, 4000), "clay"))
        self.model.route = self.route("LINESTRING(0 0,4000 0,0 0)")
        self.assertEqual(self.spans(self.placement(soil))[0], [("A", 10, 11)])
        self.model.route = self.route("MULTILINESTRING((0 0,1000 0),(3000 0,4000 0))")
        self.assertEqual(self.spans(self.placement(soil))[0], [("A", 10, 11)])

    def test_saved_file_source_reopens_without_project_layer_and_dataset_survives_restart(self):
        soil = self.soils((box(0, 1500), "clay"))
        path = os.path.join(self.folder, "soil.gpkg")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName, options.layerName = "GPKG", "soil"
        result = QgsVectorFileWriter.writeAsVectorFormatV3(soil, path, self.project.transformContext(), options)
        self.assertEqual(int(result[0]), 0)
        saved = QgsVectorLayer(path + "|layername=soil", "Saved soil", "ogr")
        self.project.addMapLayer(saved)
        self.layers.append(saved.id())
        placement = self.placement(saved)
        self.store.save_ground_dataset(self.dataset(placement), self.profiles)
        self.model.close_plan()
        self.store.close()
        self.project.removeMapLayer(saved.id())
        self.store = BurialStore(self.path)
        self.model = PlanModel(self.store)
        self.model.load_plan(self.plan_id)
        self.model.route = self.route(start=30)
        restored = self.store.list_ground_datasets()[0]
        self.assertEqual(restored["config"]["placement"], placement)
        self.assertEqual(self.spans(restored["config"]["placement"])[0], [("A", 30, 31.5)])
        dialog = DatasetDialog(self.model, restored, self.store.list_numeric_profiles("dataset"))
        self.windows.append(dialog)
        self.assertTrue(dialog.soil_controls.enabled.isChecked())
        self.assertIn("saved source", dialog.soil_controls.layer.currentText())
        self.assertEqual(dialog.placement_settings()["soil_constraint"], placement["soil_constraint"])

    def test_dialog_save_cancel_missing_source_and_plan_close(self):
        soil = self.soils((box(0, 1500), "clay"))
        placement = self.placement(soil)
        dataset = self.dataset(placement)
        dialog = DatasetDialog(self.model, dataset, self.profiles)
        self.windows.append(dialog)
        self.assertTrue(dialog.soil_controls.enabled.isChecked())
        dialog._accept()
        self.assertEqual(dialog.result()[0]["config"]["placement"]["soil_constraint"], placement["soil_constraint"])
        self.assertTrue(dialog.soil_controls._disposed)
        self.project.removeMapLayer(soil.id())
        reopened = DatasetDialog(self.model, dataset, self.profiles)
        self.windows.append(reopened)
        self.assertIn("unavailable", reopened.soil_controls.layer.currentText())
        self.assertTrue(reopened.soil_controls.enabled.isChecked())
        reopened.soil_controls.enabled.setChecked(False)
        reopened.reject()
        self.assertIsNone(reopened.result())
        again = DatasetDialog(self.model, dataset, self.profiles)
        self.windows.append(again)
        self.assertTrue(again.soil_controls.enabled.isChecked())
        with no_qt_errors():
            again.show()
            self.model.close_plan()
            pump(350)
            self.assertFalse(again.isVisible())
            self.assertTrue(again.soil_controls._disposed)

    def test_live_edits_missing_field_recovery_and_deleted_panel_callbacks(self):
        soil = self.soils((box(0, 1500), "clay"))
        panel = self.panel(self.placement(soil))
        self.assertAlmostEqual(panel.assignments[0]["end_kp"], 11.5)
        with no_qt_errors():
            soil.startEditing()
            fid = next(soil.getFeatures()).id()
            soil.changeGeometry(fid, QgsGeometry.fromWkt(box(0, 1000)))
            pump(350)
            self.assertAlmostEqual(panel.assignments[0]["end_kp"], 11)
            soil.renameAttribute(0, "other")
            pump(350)
            self.assertEqual(panel.assignments, [])
            soil.renameAttribute(0, "unit")
            pump(350)
            self.assertAlmostEqual(panel.assignments[0]["end_kp"], 11)
            soil.rollBack()
            pump(350)
            self.assertAlmostEqual(panel.assignments[0]["end_kp"], 11.5)
            panel.deleteLater()
            pump()
            # A surviving project/layer must not call a deleted widget timer.
            soil.startEditing()
            soil.changeGeometry(fid, QgsGeometry.fromWkt(box(0, 1200)))
            self.soils((box(2000, 3000), "rock"))
            pump(350)
            soil.rollBack()

    def test_pending_refresh_after_plan_close_does_not_query_closed_store(self):
        soil = self.soils((box(0, 1500), "clay"))
        panel = self.panel(self.placement(soil))
        panel._queue_refresh()
        self.model.close_plan()
        self.store.close()
        with no_qt_errors(), patch.object(self.store, "list_ground_datasets", side_effect=AssertionError("closed store read")):
            panel.refresh()
            pump(350)
        self.assertEqual(panel.assignments, [])

    def test_sweep_same_unit_overlaps_and_blank_mapping(self):
        rows = [dict(source_id=u, start_kp=a, end_kp=b) for a, b, u in
                [(0, 5, "clay"), (1, 2, "clay"), (3, 4, "rock"), (4.5, 5, "")]]
        self.assertEqual(unit_runs(rows), [(0, 3, "clay"), (4, 4.5, "clay")])
