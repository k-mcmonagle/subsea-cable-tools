"""Inputs: ordinary project line layers and Full route scope, without an RPL."""
import tempfile
import unittest
from pathlib import Path

from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorFileWriter, QgsVectorLayer
from qgis.PyQt.QtWidgets import QApplication

from ..burial.plan_model import PlanModel
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
