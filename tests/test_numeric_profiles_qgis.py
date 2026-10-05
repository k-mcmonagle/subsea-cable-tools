"""Numeric Ground Model geometry, storage and Qt5/Qt6 rendering integration."""
import json
import tempfile
import unittest
from pathlib import Path

from qgis.core import QgsCoordinateReferenceSystem, QgsDistanceArea, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
from qgis.PyQt.QtWidgets import QApplication
from qgis.PyQt.QtCore import QPointF, Qt

from ..kp_geo_utils import RouteFrame
from ..burial import numeric_profiles as n, schema
from ..burial.ground_plot import GroundModelPlot
from ..burial.numeric_profile_dialogs import NumericImportDialog, source_dialog
from ..burial.numeric_profile_geometry import polygon_assignments
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..burial.tabs.ground_tab import GroundTab

REQUIRES_QGIS = True


class NumericQgisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BurialStore(str(Path(self.temp.name) / "numeric.gpkg"))
        self.store.migrate()
        self.plan_id = self.store.save_plan({"name": "Numeric", "method": "plough", "scope_start_kp": 10,
                                            "scope_end_kp": 14, "target_burial_m": 1.5})
        self.model = PlanModel(self.store)
        self.model.load_plan(self.plan_id)
        self.profiles = n.import_profiles([["A", 0, 0], ["A", .02, ""], ["A", .04, 250]],
                                         {"source_id": 0, "depth": 1, "value": 2}, variable="su", units="kPa")
        self.rows = [{"source_id": "A", "start_kp": 10, "end_kp": 11},
                     {"source_id": "A", "start_kp": 12, "end_kp": 13}]

    def tearDown(self):
        self.model.close_plan()
        self.store.close()
        QApplication.processEvents()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def route(self, wkt="LINESTRING(0 0, 4000 0)", start=10):
        distance = QgsDistanceArea()
        distance.setSourceCrs(QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance().transformContext())
        distance.setEllipsoid("NONE")
        return RouteFrame.from_source([QgsGeometry.fromWkt(wkt)], distance, start_kp_km=start)

    def polygon(self, wkt, source="A", crs="EPSG:3857"):
        layer = QgsVectorLayer(f"MultiPolygon?crs={crs}&field=Investigation:string", "Investigations", "memory")
        feature = QgsFeature(layer.fields())
        feature["Investigation"] = source
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        layer.dataProvider().addFeatures([feature])
        return layer

    def test_polygon_holes_disconnected_intervals_and_nonzero_datum(self):
        layer = self.polygon("POLYGON((500 -100,3500 -100,3500 100,500 100,500 -100),"
                             "(1500 -50,2500 -50,2500 50,1500 50,1500 -50))")
        rows, warnings = polygon_assignments(self.route(), layer, "Investigation")
        self.assertEqual(warnings, [])
        self.assertEqual([(r["start_kp"], r["end_kp"]) for r in rows], [(10.5, 11.5), (12.5, 13.5)])

    def test_retraced_route_does_not_snap_to_first_limb(self):
        layer = self.polygon("POLYGON((1000 -10,2000 -10,2000 10,1000 10,1000 -10))")
        rows, _ = polygon_assignments(self.route("LINESTRING(0 0,4000 0,0 0)"), layer, "Investigation")
        self.assertEqual([(r["start_kp"], r["end_kp"]) for r in rows], [(11, 12), (16, 17)])

    def test_polygon_crs_transformed_and_unknown_ids_flagged(self):
        layer = self.polygon("POLYGON((0.01 -0.001,0.02 -0.001,0.02 0.001,0.01 0.001,0.01 -0.001))",
                             source="unknown", crs="EPSG:4326")
        rows, _ = polygon_assignments(self.route(), layer, "Investigation")
        self.assertAlmostEqual(rows[0]["start_kp"], 11.1131949, places=5)
        self.assertIn("Unmatched ID: unknown", n.assignment_issues(rows, self.profiles))

    def test_polygon_assignment_uses_geodesic_and_grid_route_lengths(self):
        from ..kp_range_utils import GridDistanceArea
        crs = QgsCoordinateReferenceSystem("EPSG:4326")
        context = QgsProject.instance().transformContext()
        geodesic = QgsDistanceArea()
        geodesic.setSourceCrs(crs, context)
        geodesic.setEllipsoid("WGS84")
        grid = GridDistanceArea(crs, context, QgsCoordinateReferenceSystem("EPSG:3857"))
        layer = self.polygon("POLYGON((0.002 59.99,0.008 59.99,0.008 60.01,0.002 60.01,0.002 59.99))",
                             crs="EPSG:4326")
        lengths = []
        for distance in (geodesic, grid):
            route = RouteFrame.from_source([QgsGeometry.fromWkt("LINESTRING(0 60,0.01 60)")], distance, start_kp_km=25)
            rows, _ = polygon_assignments(route, layer, "Investigation")
            self.assertAlmostEqual(rows[0]["start_kp"], 25 + route.total_length_km * .2)
            self.assertAlmostEqual(rows[0]["end_kp"], 25 + route.total_length_km * .8)
            lengths.append(rows[0]["end_kp"] - rows[0]["start_kp"])
        self.assertGreater(lengths[1], 1.9 * lengths[0])

    def test_disconnected_route_parts_do_not_gain_a_joining_interval(self):
        layer = self.polygon("POLYGON((-100 -10,5000 -10,5000 10,-100 10,-100 -10))")
        rows, _ = polygon_assignments(self.route("MULTILINESTRING((0 0,1000 0),(3000 0,4000 0))"), layer, "Investigation")
        self.assertEqual([(r["start_kp"], r["end_kp"]) for r in rows], [(10, 11), (11, 12)])

    def test_persistence_duplicate_and_assignment_rollback(self):
        self.store.save_numeric_profiles(self.profiles)
        self.assertEqual(self.store.list_numeric_profiles()[0]["samples"], self.profiles[0]["samples"])
        self.model.update_gen_params({"numeric_ground": {"assignments": self.rows, "display": {"ramp": "Plasma"}}})
        copy_id = self.store.duplicate_plan(self.plan_id, "Copy")
        copied = json.loads(self.store.get_plan(copy_id)["params_json"])["numeric_ground"]
        self.assertEqual(copied["assignments"], self.rows)
        self.assertEqual(copied["display"]["ramp"], "Plasma")
        self.assertEqual(len(self.store.list_numeric_profiles()), 1)
        self.model.update_gen_params({"numeric_ground": {"assignments": []}})
        latest = self.store.list_change_log(self.plan_id)[-1]
        self.assertTrue(self.model.rollback_to(latest["change_id"]))
        self.assertEqual(json.loads(self.model.plan["params_json"])["numeric_ground"]["assignments"], self.rows)
        self.store.delete_plan(copy_id)
        self.assertEqual(len(self.store.list_numeric_profiles()), 1)
        self.assertEqual(int(self.store.read_meta()["schema_version"]), schema.SCHEMA_VERSION)

    def test_plot_controls_missing_hover_click_and_stable_colours(self):
        plot = GroundModelPlot()
        plot.resize(800, 350)
        index = n.ProfileIndex(self.profiles, self.rows, ("su", "kPa"))
        settings = {"variable": ["su", "kPa"], "depth_min": 0, "depth_max": .1}
        plot.set_numeric(index, settings)
        plot.set_scope(10, 14)
        plot.set_target_runs([(10, 14, .035)])
        plot.plot.setXRange(10, 14)
        plot.show()
        QApplication.processEvents()
        image = plot.grab().toImage()
        self.assertFalse(image.isNull())
        self.assertEqual(plot._numeric_item.limits, (0, 250))
        self.assertTrue(plot._numeric_item._cache)
        self.assertIn("missing", plot._readout_text(10.5, .02))
        self.assertIn("kPa", plot._readout_text(10.5, 0))
        self.assertIn("no route assignment", plot._readout_text(11.5, .02))
        self.assertIn("unmeasured", plot._readout_text(10.5, .5))
        selected = []
        plot.profileClicked.connect(selected.append)
        position = plot.plot.getViewBox().mapViewToScene(QPointF(10.5, .02))
        class Click:
            def button(self):
                return getattr(Qt, "MouseButton", Qt).LeftButton
            def scenePos(self):
                return position
        plot._mouse_clicked(Click())
        self.assertEqual(selected, ["A"])
        plot.plot.setXRange(10, 10.5)
        plot.plot.setYRange(0, .02)
        QApplication.processEvents()
        self.assertEqual(plot._numeric_item.limits, (0, 250))
        self.assertEqual(plot.target_at(10.5), .035)
        plot.set_numeric(None)
        self.assertTrue(plot._unit_item.isVisible())
        plot.close()

    def test_tab_and_import_dialogs(self):
        class Dock:
            def workbench_store(self):
                return None
        self.store.save_numeric_profiles(self.profiles)
        self.model.update_gen_params({"numeric_ground": {"assignments": self.rows}})
        tab = GroundTab(self.model, Dock())
        tab.display_mode.setCurrentIndex(1)
        tab.numeric.depth_max.setValue(5)
        tab.numeric._apply_display()
        self.assertEqual(json.loads(self.model.plan["params_json"])["numeric_ground"]["display"]["depth_max"], 5)
        self.assertIsNotNone(tab.plot._numeric_index)
        tab.numeric.auto_colour.setChecked(False)
        tab.numeric.colour_min.setValue(10)
        tab.numeric.colour_max.setValue(200)
        tab.numeric.bands.setValue(4)
        tab.numeric._apply_display()
        tab.refresh()
        self.assertEqual(tab.plot._numeric_item.limits, (10, 200))
        self.assertEqual(len({tuple(c) for c in tab.plot._numeric_item.lut}), 4)
        self.assertEqual(tab.numeric.bands.value(), 4)
        for assignments in (True, False):
            dialog = NumericImportDialog(self.model, Dock(), assignments=assignments)
            dialog.grid = [["ID", "depth", "su"], ["A", "0", "20"]]
            dialog._columns()
            self.assertEqual(dialog.mapping["source_id"].currentData(), 0)
            dialog.close()
        dialog = source_dialog("A", self.profiles, tab)
        dialog.close()
        tab.close()

    def test_csv_import_workflow(self):
        class Dock:
            def workbench_store(self):
                return None
        path = Path(self.temp.name) / "measurements.csv"
        path.write_text("source_id;depth;value;units;flags\n001;0;1,5;kPa;partial\n001;0,02;;kPa;missing\n")
        dialog = NumericImportDialog(self.model, Dock())
        dialog.path = str(path)
        dialog._reload()
        dialog.name.setText("su")
        dialog.decimal_comma.setChecked(True)
        dialog._accept()
        profile = dialog.result_rows[0]
        self.assertEqual(profile["source_id"], "001")
        self.assertEqual(profile["samples"][0]["value"], 1.5)
        self.assertIsNone(profile["samples"][1]["value"])
        self.assertEqual(profile["provenance"]["mapping"]["depth"], 1)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
