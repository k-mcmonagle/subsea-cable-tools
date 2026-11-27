"""Interactions between plan drafts, route changes, BAS generation and reports."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox

from ..burial import bas_model
from ..burial.bas_polygon_dialog import BasChangesDialog
from ..burial.plan_model import PlanModel
from ..burial.profile_data import PlanProfile
from ..burial.report_profile import saved_profile_figure
from ..burial.tabs import bas_tab as bas_ui
from ..qgis_compat import BUTTON_BOX_OK, MESSAGE_BOX_NO, MESSAGE_BOX_YES
from .test_burial_dock_lifecycle import _Harness

REQUIRES_QGIS = True


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        h = _Harness()
        self.h = h.__enter__()
        self.addCleanup(h.__exit__, None, None, None)
        self.d = h.dock
        self.d.refresh_plans(h.plan_id)
        self.model = self.d.model
        self.bas = self.d.bas_tab
        self.inputs = self.d.inputs_tab
        self.route = self.layer("Review route", "LineString", "LINESTRING(0 60,0.1 60)")
        self.inputs.route_layer_radio.setChecked(True)
        self.inputs.fallback_combo.setLayer(self.route)
        self.inputs.apply_rpl_button.click()
        self.assertIsNotNone(self.model.route)

    def layer(self, title, geom_type, wkt):
        layer = QgsVectorLayer(f"{geom_type}?crs=EPSG:4326", title, "memory")
        f = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        layer.dataProvider().addFeatures([f])
        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self.addCleanup(QgsProject.instance().removeMapLayer, layer.id())
        return layer

    def test_plan_switch_protects_drafts_and_respects_explicit_discard(self):
        second = PlanModel(self.h.store).create_plan("Another plan", "plough")
        self.d.refresh_plans(self.h.plan_id)
        self.bas._insert_rows(None, 1)
        self.d.ground_tab._add_unit()
        for answer in (MESSAGE_BOX_NO, MESSAGE_BOX_YES):
            with patch.object(QMessageBox, "question", return_value=answer) as prompt:
                self.d.plan_combo.setCurrentIndex(self.d.plan_combo.findData(second))
                self.assertIn("BAS", prompt.call_args.args[2])
                self.assertIn("Ground Model", prompt.call_args.args[2])
            if answer == MESSAGE_BOX_NO:
                self.assertEqual(self.model.plan_id, self.h.plan_id)
                self.assertEqual(self.d.plan_combo.currentData(), self.h.plan_id)
                self.assertEqual(len(self.bas._working), 1)
        self.assertEqual(self.model.plan_id, second)
        self.assertFalse(self.d.ground_tab._dirty)
        self.assertFalse(self.bas._dirty)
        self.assertFalse(self.h.store.list_bas_rows(self.h.plan_id))

    def test_kp_mode_guard_and_stale_draft_cannot_undo_translation(self):
        self.assertTrue(self.model.save_bas([{"start_kp": 1, "end_kp": 3}]))
        self.bas.refresh()
        self.bas._working[0]["notes"] = "Edited note"
        self.bas._mark_dirty()
        self.inputs._change_kp_mode()
        self.assertIn("pending edits in BAS", self.inputs.apply_status.text())
        # Exercise a route change from outside this editor as well.
        other = PlanModel(self.h.store)
        other.load_plan(self.h.plan_id)
        self.assertIsNotNone(other.change_kp_mode("cartesian", "EPSG:3857"))
        translated = self.h.store.list_bas_rows(self.h.plan_id)[0]["end_kp"]
        self.assertGreater(translated, 4)
        with patch.object(QMessageBox, "warning") as warning:
            self.bas._apply()
            self.assertIn("KP basis changed", warning.call_args.args[2])
        self.assertTrue(self.bas._dirty)
        self.assertEqual(self.h.store.list_bas_rows(self.h.plan_id)[0]["end_kp"], translated)

    def test_drafts_preserve_edits_saved_from_another_editor(self):
        self.model.save_bas([{"start_kp": 0, "end_kp": 1}])
        self.bas.refresh()
        self.bas._working[0]["notes"] = "Local note"
        self.bas._mark_dirty()
        ground = self.d.ground_tab
        ground.refresh()
        ground._add_unit()
        other = PlanModel(self.h.store)
        other.load_plan(self.h.plan_id)
        other.save_bas([dict(other.bas_rows[0], notes="Saved note")])
        other.save_ground_units([{"start_kp": 2, "end_kp": 3, "soil_class": "Sand", "top_m": 0}])
        # Use a valid local row so Apply reaches the store-side check.
        ground._working[0]["soil_class"] = "Clay"
        with patch.object(QMessageBox, "warning") as warn:
            self.bas._apply()
            self.assertIn("BAS rows changed", warn.call_args.args[2])
            ground._apply()
            self.assertIn("Ground units changed", warn.call_args.args[2])
        self.assertEqual(self.h.store.list_ground_units(self.h.plan_id)[0]["soil_class"], "Sand")
        self.assertEqual(self.h.store.list_bas_rows(self.h.plan_id)[0]["notes"], "Saved note")
        self.assertEqual(self.bas._working[0]["notes"], "Local note")

    def test_full_route_repeat_and_changed_classifications(self):
        polygon = self.layer("Soil", "Polygon", "POLYGON((-1 59,1 59,1 61,-1 61,-1 59))")
        self.assertEqual(self.model.plan.get("scope_end_kp") or 0, 0)

        def choose_source(dialog):
            self.assertIn("full route", dialog.buttons.button(BUTTON_BOX_OK).text().lower())
            item = next(dialog.tree.topLevelItem(i) for i in range(dialog.tree.topLevelItemCount())
                        if dialog.tree.topLevelItem(i).text(0) == polygon.name())
            item.setCheckState(0, Qt.CheckState.Checked)
            return bas_ui.DIALOG_ACCEPTED

        with patch.object(bas_ui, "qt_exec", side_effect=choose_source):
            self.bas._generate()
            self.assertAlmostEqual(self.bas._working[0]["end_kp"], self.model.route.end_kp_km)
            self.bas._working[0]["notes"] = "Reviewed by hand"
            self.bas._apply()
            self.bas._generate()
        self.assertEqual(len(self.bas._working), 1)
        self.assertFalse(self.bas._dirty)
        row_id = self.bas._working[0]["row_id"]
        polygon.setName("Revised soil")

        def accept_update(dialog):
            if not isinstance(dialog, BasChangesDialog):
                return choose_source(dialog)
            self.assertEqual(dialog.selected(), [])
            dialog.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
            return bas_ui.DIALOG_ACCEPTED

        with patch.object(bas_ui, "qt_exec", side_effect=accept_update):
            self.bas._generate()
        self.assertEqual(self.bas._working[0]["notes"], "Reviewed by hand")
        self.assertEqual(self.bas._working[0]["row_id"], row_id)
        self.assertEqual(self.bas._working[0]["values"], {"soil_layer": "Revised soil"})
        self.assertEqual(self.model.bas_rows[0]["values"], {"soil_layer": "Soil"})
        self.assertEqual(self.model.plan.get("scope_end_kp") or 0, 0)

    def test_changed_boundaries_remain_available_for_review(self):
        existing = [bas_model.decode_row({"start_kp": 1, "end_kp": 3, "notes": "Manual pick"})]
        proposed = [bas_model.decode_row({"start_kp": a, "end_kp": b}) for a, b in [(1, 2), (2, 3), (3, 4)]]
        additions, conflicts, _ = bas_model.compare_polygon_rows(existing, proposed)
        self.assertEqual(additions, proposed[2:])
        self.assertEqual(conflicts, [(r, [0], None) for r in proposed[:2]])

    def test_report_uses_saved_state_across_live_layouts_and_zoom(self):
        self.model.update_plan({"scope_start_kp": 1, "scope_end_kp": 4})
        self.d._set_profile_layout("ground", False)
        self.d.ground_tab._add_unit()
        self.assertTrue(self.d.ground_tab._working)
        self.assertIsNone(saved_profile_figure(self.model)[0])
        self.assertTrue(self.model.save_ground_units([{"start_kp": 1, "end_kp": 4,
                                                      "top_m": 0, "base_m": 2, "soil_class": "Sand"}]))
        png, caption = saved_profile_figure(self.model)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIn("1.000–4.000", caption)
        self.assertIn("metres below seabed", caption)
        self.d._set_profile_layout("stacked", False)
        self.d.profile.plot.setXRange(2, 3, padding=0)
        self.assertEqual(saved_profile_figure(self.model), (png, caption))
        self.model.bathy_profile = PlanProfile(kps=[1, 2, 3, 4], depths=[4, None, 5, 6])
        with patch.object(self.model, "profile_state", return_value="current"):
            with tempfile.TemporaryDirectory() as folder:
                dest = Path(folder) / "plan.html"
                with patch.object(QFileDialog, "getSaveFileName", return_value=(str(dest), "")):
                    self.d.review_tab._export_report()
                html = dest.read_text(encoding="utf-8")
                self.assertIn("sampled depth in metres, positive down", html)
                self.assertIn("data:image/png;base64,", html)
        with patch.object(self.model, "profile_state", return_value="stale"):
            self.assertIn("out of date", saved_profile_figure(self.model)[1])
