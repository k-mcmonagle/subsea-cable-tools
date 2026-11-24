"""Inputs: ordinary project line layers and Full route scope, without an RPL."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorFileWriter, QgsVectorLayer
from qgis.PyQt.QtWidgets import QApplication

from ..burial.plan_model import PlanModel
from ..burial import schema
from ..burial.store import BurialStore
from ..burial.tabs.inputs_tab import InputsTab

REQUIRES_QGIS = True


class ProjectLineRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BurialStore(str(Path(self.temp.name) / "plans.gpkg"))
        self.store.migrate()
        self.model = PlanModel(self.store)
        self.model.create_plan("Plain route", "plough")
        self.tab = InputsTab(self.model, lambda: None)
        self.layers = []

    def tearDown(self):
        self.tab.close()
        self.tab.deleteLater()
        self.model.close_plan()
        for layer in self.layers:
            QgsProject.instance().removeMapLayer(layer.id())
        QApplication.processEvents()
        self.store.close()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass  # OGR can retain file handles until Qt deletes its layers.

    def line(self, wkt="LINESTRING(0 0,0.01 0)"):
        layer = QgsVectorLayer("LineString?crs=EPSG:4326", "Plain line", "memory")
        if wkt:
            feature = QgsFeature()
            feature.setGeometry(QgsGeometry.fromWkt(wkt))
            layer.dataProvider().addFeatures([feature])
        QgsProject.instance().addMapLayer(layer)
        self.layers.append(layer)
        return layer

    def select(self, layer):
        self.tab.route_layer_radio.setChecked(True)
        self.tab.fallback_combo.setLayer(layer)
        self.tab.apply_rpl_button.click()

    def test_temporary_line_full_scope_and_reload(self):
        layer = self.line()
        self.assertNotIn("SeqNo", layer.fields().names())
        self.select(layer)
        self.assertIsNotNone(self.model.route, self.tab.apply_status.text())
        self.assertFalse(self.model.plan["rpl_id"])
        self.assertEqual(self.model.kp_datum_start(), 0)
        end = self.model.route.end_kp_km
        self.assertGreater(end, 1)
        self.tab.full_route_button.click()
        self.tab.apply_scope_button.click()
        self.assertAlmostEqual(self.model.plan["scope_end_kp"], end, places=6)
        self.model.load_plan(self.model.plan_id)
        self.assertIsNotNone(self.model.route)
        self.assertAlmostEqual(self.model.route.end_kp_km, end)
        self.assertAlmostEqual(self.model.plan["scope_end_kp"], end, places=6)

    def test_kp_labels_use_three_decimals_without_changing_full_route(self):
        self.select(self.line())
        self.tab.full_route_button.click()
        spin = self.tab.scope_end
        endpoint = self.model.route.end_kp_km
        self.assertEqual(spin.cleanText(), spin.locale().toString(endpoint, "f", 3))
        spin.interpretText()
        self.assertAlmostEqual(spin.value(), endpoint, places=9)
        self.tab.apply_scope_button.click()
        self.assertAlmostEqual(self.model.plan["scope_end_kp"], endpoint, places=9)
        spin.lineEdit().setText("0.500 km")
        spin.interpretText()
        self.assertEqual(spin.value(), .5)
        self.assertEqual(spin.singleStep(), .001)

    def test_setting_same_layer_again_reads_current_geometry(self):
        layer = self.line()
        self.select(layer)
        end = self.model.route.end_kp_km
        fid = next(layer.getFeatures()).id()
        layer.startEditing()
        layer.changeGeometry(fid, QgsGeometry.fromWkt("LINESTRING(0 0,0.02 0)"))
        self.select(layer)
        self.assertAlmostEqual(self.model.route.end_kp_km, end * 2)
        layer.rollBack()

    def review(self, layer, button):
        labels = []

        def choose(box):
            for candidate in box.buttons():
                name = candidate.text().replace("&", "")
                labels.append(name)
                if name == button:
                    candidate.click()

        with patch(InputsTab.__module__ + ".qt_exec", side_effect=choose):
            self.select(layer)
        self.assertIn(button, labels)
        return labels

    def test_disjoint_line_offers_keep_or_cancel_and_restamps_events(self):
        old, new = self.line(), self.line("LINESTRING(0 1,0.01 1)")
        self.select(old)
        self.model.update_plan({"scope_start_kp": 0., "scope_end_kp": 1.})
        self.model.add_event(.5, schema.EVENT_BURIAL_START)
        count = len(self.store.list_change_log(self.model.plan_id))
        labels = self.review(new, "Cancel")
        self.assertNotIn("Translate KPs", labels)
        self.assertEqual(self.model.plan["rpl_gpkg_path"], old.source())
        self.assertEqual(len(self.store.list_change_log(self.model.plan_id)), count)
        self.review(new, "Keep KP numbers")
        self.assertEqual(self.model.plan["rpl_gpkg_path"], new.source())
        self.assertEqual(self.model.events[0]["kp"], .5)
        self.assertAlmostEqual(self.model.events[0]["lat"], 1, places=6)
        changes = self.store.list_change_log(self.model.plan_id)
        self.assertEqual(len(changes), count + 1)
        self.assertTrue(self.model.rollback_to(changes[-1]["change_id"]))
        self.assertEqual(self.model.plan["rpl_gpkg_path"], old.source())
        self.assertAlmostEqual(self.model.events[0]["lat"], 0, places=6)

    def test_review_edited_line_with_only_saved_scope(self):
        layer = self.line()
        self.select(layer)
        self.model.update_plan({"scope_start_kp": .2, "scope_end_kp": .8})
        fid = next(layer.getFeatures()).id()
        layer.startEditing()
        layer.changeGeometry(fid, QgsGeometry.fromWkt("LINESTRING(-0.005 0,0.01 0)"))
        original = self.model.route
        with patch.object(self.store, "append_change", side_effect=RuntimeError("test write failure")):
            self.review(layer, "Translate KPs")
        self.assertIs(self.model.route, original)
        self.assertEqual(self.store.get_plan(self.model.plan_id)["scope_start_kp"], .2)
        self.review(layer, "Translate KPs")
        self.assertAlmostEqual(self.model.plan["scope_start_kp"], .756597, places=5)
        self.assertAlmostEqual(self.model.plan["scope_end_kp"], 1.356597, places=5)
        layer.rollBack()

    def test_reopen_overwritten_source_requires_explicit_acceptance(self):
        layer = self.line()
        self.select(layer)
        self.model.update_plan({"scope_start_kp": .2, "scope_end_kp": .8})
        self.model.save_risk_checks([{"plan_id": self.model.plan_id, "check_id": "crossings",
                                      "name": "Crossings", "config_json": "{}"}])
        self.model.record_risk_run(["crossings"], {}, {}, "Scanned")
        paths = self.model.path_fingerprints()
        fid = next(layer.getFeatures()).id()
        layer.startEditing()
        layer.changeGeometry(fid, QgsGeometry.fromWkt("LINESTRING(-0.005 0,0.01 0)"))
        self.model.load_plan(self.model.plan_id)
        self.assertTrue(self.model.route_baseline_changed())
        self.assertEqual(self.model.risk_status()["crossings"]["state"], "changed")
        self.assertNotEqual(self.model.path_fingerprints(), paths)
        self.assertAlmostEqual(self.model.plan["scope_start_kp"], .2)
        self.assertNotIn("Translate KPs", self.review(layer, "Cancel"))
        self.assertTrue(self.model.route_baseline_changed())
        self.review(layer, "Keep KP numbers")
        self.model.load_plan(self.model.plan_id)
        self.assertFalse(self.model.route_baseline_changed())
        self.assertAlmostEqual(self.model.plan["scope_end_kp"], .8)
        layer.rollBack()

    def test_keep_numbers_on_shorter_route_preserves_event_outside_extent(self):
        self.select(self.line())
        self.model.update_plan({"scope_start_kp": 0., "scope_end_kp": 1.})
        self.model.add_event(.8, schema.EVENT_BURIAL_START)
        short = self.line("LINESTRING(0 0,0.002 0)")
        self.review(short, "Keep KP numbers")
        self.assertEqual(self.model.events[0]["kp"], .8)
        self.assertIsNone(self.model.events[0]["lat"])
        self.assertIsNone(self.model.events[0]["lon"])

    def test_pending_scope_is_preserved_when_changing_routes(self):
        old, new = self.line(), self.line("LINESTRING(0 0,0.02 0)")
        self.select(old)
        self.tab.scope_end.setValue(.7)
        self.select(new)
        self.assertIn("pending edits in Inputs", self.tab.apply_status.text())
        self.assertEqual(self.model.plan["rpl_gpkg_path"], old.source())
        self.assertEqual(self.tab.scope_end.value(), .7)

    def test_empty_layer_does_not_replace_valid_route_and_short_scope_is_usable(self):
        layer = self.line("LINESTRING(0 0,0.000001 0)")
        self.select(layer)
        self.tab.full_route_button.click()
        self.tab.apply_scope_button.click()
        self.assertGreater(self.model.plan["scope_end_kp"], 0)
        before = self.model.plan["rpl_gpkg_path"]
        self.select(self.line(None))
        self.assertEqual(self.model.plan["rpl_gpkg_path"], before)
        self.assertIn("not changed", self.tab.apply_status.text())

    def test_file_layer_can_reopen_without_being_in_project(self):
        layer = self.line()
        path = str(Path(self.temp.name) / "route.gpkg")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = "route"
        QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, QgsProject.instance().transformContext(), options)
        saved = QgsVectorLayer(path + "|layername=route", "File route", "ogr")
        QgsProject.instance().addMapLayer(saved)
        self.select(saved)
        end = self.model.route.end_kp_km
        QgsProject.instance().removeMapLayer(saved.id())
        self.model.load_plan(self.model.plan_id)
        self.assertIsNotNone(self.model.route)
        self.assertAlmostEqual(self.model.route.end_kp_km, end)


class EmptyProfileRangeTests(unittest.TestCase):
    def test_unapplied_scope_previews_route_without_saving_it(self):
        from .test_burial_dock_lifecycle import _Harness
        from ..burial.analysis_task import build_route_frame
        with _Harness() as h:
            layer = QgsVectorLayer("LineString?crs=EPSG:4326", "Plain route", "memory")
            feature = QgsFeature()
            feature.setGeometry(QgsGeometry.fromWkt("LINESTRING(0 0,8 0)"))
            layer.dataProvider().addFeatures([feature])
            model = h.dock.model
            model.route, model.distance = build_route_frame(layer)
            model.plan["scope_start_kp"] = model.plan["scope_end_kp"] = 0.0
            h.dock._refresh_profile()
            plot = h.dock.profile
            self.assertEqual(plot._scope, (model.route.start_kp_km, model.route.end_kp_km))
            self.assertGreater(plot.plot.viewRange()[0][1] - plot.plot.viewRange()[0][0], 800)
            self.assertEqual(model.plan["scope_end_kp"], 0)
            for widget in (plot.plot, plot.slope_plot):
                axis = widget.getPlotItem().getAxis("bottom")
                self.assertEqual(axis.tickStrings([0, 100, 861.780529], 1, 100),
                                 ["0.000", "100.000", "861.781"])
            plot.set_scope(0, 1e-9)
            plot.reset_scope_view()
            self.assertGreater(plot.plot.viewRange()[0][1] - plot.plot.viewRange()[0][0], .9)
            plot.clear()
            self.assertGreater(plot.plot.viewRange()[0][1] - plot.plot.viewRange()[0][0], .9)
