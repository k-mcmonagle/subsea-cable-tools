"""Optional annotations stay display-only across import, reopen and teardown."""

import copy
import json
import os
import time
import unittest
from unittest.mock import patch

from qgis.PyQt.QtCore import QCoreApplication

from ..burial import dxf_annotations as a, horizons as h
from ..burial.horizon_dialogs import HorizonImportDialog, HorizonsDialog
from ..burial.tabs.ground_tab import GroundTab
from ..burial.tabs.inputs_tab import InputsTab
from ..qgis_compat import CHECK_STATE_CHECKED, DIALOG_ACCEPTED
from . import test_burial_horizons_qgis as base
from .test_burial_horizons import dxf, line
from .test_dxf_annotations import text_entity

REQUIRES_QGIS = True


class AnnotationQgisTests(unittest.TestCase):
    setUp = base.HorizonQgisTests.setUp
    tearDown = base.HorizonQgisTests.tearDown
    widget = base.HorizonQgisTests.widget
    wait_for = base.HorizonQgisTests.wait_for

    def annotated(self, only=False, labels=True):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(dxf(("" if only else line("H1", 0, 2, 10000, 3))
                             + text_entity("Fine <sand> & gravel", x=4000, y=3)))
        dialog = self.widget(HorizonImportDialog)
        dialog.load_file(self.path)
        self.wait_for(dialog)
        self.assertFalse(dialog.import_labels.isChecked())
        if not only:
            dialog.layers.item(0).setCheckState(CHECK_STATE_CHECKED)
        if labels:
            dialog.import_labels.setChecked(True)
            dialog.label_layers.item(0).setCheckState(CHECK_STATE_CHECKED)
        dialog._preview()
        self.wait_for(dialog)
        return dialog

    def saved(self, imported):
        manager = self.widget(HorizonsDialog)
        manager.rows, manager.annotations = imported.rows, imported.annotations
        manager._rebuild()
        manager._save()
        self.assertEqual(manager.result(), DIALOG_ACCEPTED)
        return manager

    def test_opt_in_tooltips_preview_and_plain_text(self):
        dialog = self.annotated(labels=False)
        self.assertTrue(dialog.rows)
        self.assertFalse(dialog.annotations)
        self.assertIn("10 m", dialog.x_scale.toolTip())
        self.assertIn("0.1", dialog.exaggeration.toolTip())
        dialog.import_labels.setChecked(True)
        dialog.label_layers.item(0).setCheckState(CHECK_STATE_CHECKED)
        self.assertFalse(dialog.import_button.isEnabled())
        dialog._preview()
        self.wait_for(dialog)
        self.assertEqual(dialog.annotations[0]["labels"][0]["kp"], 4)
        dialog.show()
        QCoreApplication.processEvents()
        overlay = dialog.preview_plot.annotations
        overlay.redraw()
        shown = [item for item in overlay.items if item.isVisible()]
        self.assertTrue(shown)
        self.assertEqual(shown[0].textItem.toPlainText(), "Fine <sand> & gravel")
        self.assertIn("&lt;sand&gt;", shown[0].toolTip())
        self.assertFalse(self.model.ground_units)
        self.assertFalse(a.plan_state(self.model.plan)["groups"])

    def test_visibility_reopen_old_horizon_write_and_input_register(self):
        imported = self.annotated()
        manager = self.saved(imported)
        stored = a.plan_state(self.model.plan)
        self.assertEqual(stored["groups"], imported.annotations)
        # Simulate the older editor patching only its existing horizon key.
        self.model.update_gen_params({h.PARAMS_KEY: {"version": 1, "rows": manager.rows}})
        self.assertEqual(a.plan_state(self.model.plan), stored)
        tab = InputsTab(self.model, lambda: None)
        self.widgets.append(tab)
        self.assertEqual(tab.inputs_table.rowCount(), 1)
        self.assertIn("1 annotation(s)", tab.inputs_table.item(0, 2).toolTip())
        reopened = self.widget(HorizonsDialog)
        self.assertEqual(reopened.list.count(), 2)
        reopened.list.setCurrentRow(1)
        self.assertFalse(reopened.edit_vertices_button.isEnabled())
        reopened.show_labels.setChecked(False)
        reopened._save()
        self.assertFalse(a.plan_state(self.model.plan)["visible"])
        os.remove(self.path)
        self.model.load_plan(self.model.plan_id)
        again = self.widget(HorizonsDialog)
        self.assertFalse(again.show_labels.isChecked())
        self.assertEqual(again.annotations, imported.annotations)
        ground = self.widget(GroundTab)
        self.assertFalse(ground.plot.annotations.entries)
        again.show_labels.setChecked(True)
        again._save()
        ground.refresh()
        self.assertTrue(ground.plot.annotations.entries)
        self.model.close_plan()
        ground.refresh()  # the tab normally coalesces planChanged refreshes
        self.assertFalse(ground.plot.annotations.entries)

    def test_annotation_only_save_remove_cancel_and_rollback(self):
        imported = self.annotated(only=True)
        self.assertTrue(imported.import_button.isEnabled(), imported.message.toPlainText())
        self.assertFalse(imported.rows)
        before = copy.deepcopy(self.model.plan)
        manager = self.saved(imported)
        self.assertFalse(h.plan_rows(self.model.plan))
        self.assertFalse(self.model.ground_units)
        log = self.store.list_change_log(self.model.plan_id)
        cancelled = self.widget(HorizonsDialog)
        cancelled._remove()
        cancelled.reject()
        self.assertTrue(a.plan_state(self.model.plan)["groups"])
        removing = self.widget(HorizonsDialog)
        removing._remove()
        removing._save()
        self.assertFalse(a.plan_state(self.model.plan)["groups"])
        self.assertTrue(self.model.rollback_to(log[-1]["change_id"]))
        self.assertEqual(self.model.plan["params_json"], before["params_json"])
        self.assertEqual(manager.rows, [])

    def test_failed_save_and_plan_switch_never_write_annotations(self):
        imported = self.annotated()
        manager = self.widget(HorizonsDialog)
        manager.rows, manager.annotations = imported.rows, imported.annotations
        before = copy.deepcopy(self.model.plan)
        errors = []
        self.model.storeError.connect(errors.append)
        with patch.object(self.store, "append_change", side_effect=RuntimeError("simulated write failure")):
            manager._save()
        self.assertTrue(errors)
        self.assertEqual(self.model.plan, before)
        self.assertEqual(self.store.get_plan(self.model.plan_id), before)
        self.model.close_plan()
        imported._accept()
        self.assertNotEqual(imported.result(), DIALOG_ACCEPTED)

    def test_annotation_conversion_cancellation_is_discarded(self):
        dialog = self.annotated()
        before = copy.deepcopy(self.model.plan)
        def slow(*args):
            progress = args[-1]
            while True:
                progress(50, "Placing test annotations")
                time.sleep(0.005)
        with patch.object(a, "build", side_effect=slow):
            dialog._preview()
            QCoreApplication.processEvents()
            dialog.reject()
            self.wait_for(dialog)
        self.assertFalse(dialog.annotations)
        self.assertEqual(self.model.plan, before)

    def test_dense_annotations_bounded_zoomable_and_cleared_on_reference_change(self):
        imported = self.annotated(only=True)
        group = imported.annotations[0]
        seed = group["labels"][0]
        group["labels"] = [dict(seed, text=f"Note {i}", kp=i * .001) for i in range(2000)]
        self.saved(imported)
        ground = self.widget(GroundTab)
        ground.show()
        ground.plot.plot.setXRange(0, 2, padding=0)
        QCoreApplication.processEvents()
        overlay = ground.plot.annotations
        overlay.redraw()
        self.assertLessEqual(len(overlay.items), overlay.MAX_VISIBLE)
        self.assertIn("zoom in", ground.plot.annotation_notice.text())
        ground.plot.plot.setXRange(1, 1.002, padding=0)
        overlay.redraw()
        self.assertTrue(any(item.isVisible() for item in overlay.items))
        self.model.plan = self.model._set_kp_datum(self.model.plan, 5)
        ground.refresh()
        overlay.redraw()
        self.assertFalse(overlay.entries)
        self.assertTrue(all(not item.isVisible() for item in overlay.items))
        self.assertIn("check reference", ground.horizon_status.text())

    def test_annotation_state_does_not_change_ground_or_other_settings(self):
        imported = self.annotated()
        state = dict(version=1, groups=imported.annotations, visible=True)
        self.model.update_gen_params({a.PARAMS_KEY: state})
        self.assertNotIn(a.PARAMS_KEY, self.model._display_settings())
        self.assertTrue(json.loads(self.model.plan["params_json"])["unknown_future_option"]["keep"])
        self.assertFalse(self.model.ground_units)
        duplicate = self.store.duplicate_plan(self.model.plan_id, "Annotation copy")
        self.assertEqual(a.plan_state(self.store.get_plan(duplicate)), state)
