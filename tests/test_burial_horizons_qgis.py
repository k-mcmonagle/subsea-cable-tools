"""QGIS 3/4: import preview, overlays and non-migrating, undoable persistence.

Only temporary GeoPackages are used. No user's project is opened or edited.
"""

import copy
import json
import os
import sqlite3
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from qgis.PyQt.QtCore import QCoreApplication, QEvent, QTimer
from qgis.PyQt.QtWidgets import QDialogButtonBox, QMessageBox, QPlainTextEdit

from ..burial import horizon_dialogs, horizons as h, schema
from ..burial.horizon_dialogs import HorizonImportDialog, HorizonsDialog, reference_notice, route_reference
from ..burial.plan_model import PlanModel
from ..burial.profile_data import PlanProfile
from ..burial.store import BurialStore
from ..burial.tabs.ground_tab import GroundTab
from ..burial.tabs.inputs_tab import InputsTab
from ..qgis_compat import BUTTON_BOX_OK, CHECK_STATE_CHECKED, CHECK_STATE_UNCHECKED, DIALOG_ACCEPTED
from .test_burial_horizons import dxf, line

REQUIRES_QGIS = True


class Dock:
    def workbench_store(self):
        return None

    def highlight_kp(self, _kp):
        pass


class HorizonQgisTests(unittest.TestCase):
    def setUp(self):
        self.callback_errors = []
        self._exception_hook = sys.excepthook
        sys.excepthook = lambda *args: self.callback_errors.append(args)
        # OGR can retain pooled handles on Windows: let OS temp cleanup own them.
        self.folder = tempfile.mkdtemp(prefix="bp_horizons_")
        self.store = BurialStore(os.path.join(self.folder, "plans.gpkg"))
        self.store.migrate()
        self.model = PlanModel(self.store)
        self.model.create_plan("Existing plan", "plough")
        self.model.update_plan({"scope_start_kp": 0, "scope_end_kp": 10, "target_burial_m": 1.5})
        self.model.update_gen_params({"unknown_future_option": {"keep": True}, "min_section_km": 0.25})
        self.widgets = []
        self.path = os.path.join(self.folder, "section.dxf")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(dxf(line("H1", 0, 2, 4000, 3) + line("H1", 6000, 3, 10000, 4)))

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        self.model._layer_timer.stop()
        # Exercise Qt teardown while the application and plan store are alive.
        # Leaving deferred widget deletion until native process exit can crash
        # the Windows Qt6 runtime even after every assertion has passed.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        self.store.close()
        sys.excepthook = self._exception_hook
        self.assertFalse(self.callback_errors, str(self.callback_errors))

    def widget(self, cls):
        widget = cls(self.model, Dock())
        self.widgets.append(widget)
        return widget

    def wait_for(self, dialog, timeout=20):
        deadline = time.monotonic() + timeout
        while dialog._task is not None and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        self.assertIsNone(dialog._task, "Horizon background work did not finish")

    def imported(self):
        dialog = self.widget(HorizonImportDialog)
        dialog.load_file(self.path)
        self.wait_for(dialog)
        dialog.layers.item(0).setCheckState(CHECK_STATE_CHECKED)
        dialog._preview()
        self.wait_for(dialog)
        self.assertTrue(dialog.import_button.isEnabled(), dialog.message.toPlainText())
        return dialog

    def schema_snapshot(self):
        with sqlite3.connect(self.store.gpkg_path) as conn:
            return conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()

    def test_open_cancel_and_unchanged_apply_do_not_write(self):
        before = self.store.get_plan(self.model.plan_id)
        log = self.store.list_change_log(self.model.plan_id)
        tables = self.schema_snapshot()
        dialog = self.widget(HorizonsDialog)
        dialog._save()
        dialog = self.widget(HorizonsDialog)
        dialog.rows = self.imported().rows
        dialog.reject()
        self.assertEqual(self.store.get_plan(self.model.plan_id), before)
        self.assertEqual(self.store.list_change_log(self.model.plan_id), log)
        self.assertEqual(self.schema_snapshot(), tables)

    def test_save_reopen_duplicate_rollback_and_other_plan_isolation(self):
        tables = self.schema_snapshot()
        other_id = self.store.save_plan({"name": "Other", "method": "plough", "params_json": '{"untouched":7}'})
        other = self.store.get_plan(other_id)
        self.model.save_ground_units([{"unit_id": schema.new_id(), "start_kp": 0, "end_kp": 10,
                                      "top_m": 0, "base_m": 1, "soil_class": "SAND"}])
        units = copy.deepcopy(self.model.ground_units)
        log_count = len(self.store.list_change_log(self.model.plan_id))
        dialog = self.widget(HorizonsDialog)
        dialog.rows = self.imported().rows
        dialog._save()
        saved = h.plan_rows(self.model.plan)
        self.assertEqual(saved, dialog.rows)
        self.assertEqual(self.schema_snapshot(), tables)
        self.assertEqual(self.model.ground_units, units)
        params = json.loads(self.model.plan["params_json"])
        self.assertEqual(params["unknown_future_option"], {"keep": True})
        self.assertEqual(params["min_section_km"], 0.25)
        self.assertEqual(self.model.plan["status"], schema.PLAN_STATUS_DRAFT)
        log = self.store.list_change_log(self.model.plan_id)
        self.assertEqual(len(log), log_count + 1)
        model2 = PlanModel(self.store)
        model2.load_plan(self.model.plan_id)
        self.assertEqual(h.plan_rows(model2.plan), saved)
        duplicate_id = self.store.duplicate_plan(self.model.plan_id, "Copy")
        self.assertEqual(h.plan_rows(self.store.get_plan(duplicate_id)), saved)
        self.assertTrue(self.model.rollback_to(log[-1]["change_id"]))
        self.assertEqual(h.plan_rows(self.model.plan), [])
        self.assertEqual(self.model.ground_units, units)
        self.assertEqual(self.store.get_plan(other_id), other)
        model2._layer_timer.stop()

    def test_preview_invalidation_and_coordinate_controls(self):
        dialog = self.imported()
        self.assertEqual(dialog.rows[0]["segments"][0], [[0, 2], [4, 3]])
        dialog.kp_origin.setValue(2)
        self.assertFalse(dialog.import_button.isEnabled())
        self.assertEqual(dialog.rows, [])
        dialog._preview()
        self.wait_for(dialog)
        self.assertEqual(dialog.rows[0]["segments"][0][0], [2, 2])
        dialog.kp_ref.radio_shift.setChecked(True)
        dialog.kp_ref.shift_spin.setValue(0.25)
        dialog._preview()
        self.wait_for(dialog)
        self.assertEqual(dialog.rows[0]["segments"][0][0], [2.25, 2])
        dialog.load_file(os.path.join(self.folder, "missing.dxf"))
        self.assertFalse(dialog.import_button.isEnabled())
        self.assertIsNone(dialog.drawing)

    def test_vertex_editor_validates_then_saves_edits_without_changing_source(self):
        dialog = self.widget(HorizonsDialog)
        dialog.rows = self.imported().rows
        original = copy.deepcopy(dialog.rows[0])
        dialog._rebuild()

        def edit(editor):
            text = editor.findChild(QPlainTextEdit)
            button = editor.findChild(QDialogButtonBox).button(BUTTON_BOX_OK)
            text.setPlainText("0, 2\n0, 3")
            button.click()
            self.assertNotEqual(editor.result(), DIALOG_ACCEPTED)
            self.assertEqual(dialog.rows[0], original)
            text.setPlainText("0, 2.5\n4, 3.5\n\n6, 4\n10, 5")
            button.click()
            self.assertEqual(editor.result(), DIALOG_ACCEPTED)
            return editor.result()

        with patch.object(horizon_dialogs, "qt_exec", side_effect=edit):
            dialog._edit_vertices()
        dialog.name.setText("Edited horizon")
        dialog._save()
        saved = h.plan_rows(self.store.get_plan(self.model.plan_id))[0]
        self.assertEqual(saved["name"], "Edited horizon")
        self.assertEqual(saved["segments"], [[[0, 2.5], [4, 3.5]], [[6, 4], [10, 5]]])
        self.assertEqual(saved["source_segments"], original["source_segments"])
        self.assertEqual(saved["source"], original["source"])
        self.assertEqual(self.widget(HorizonsDialog).rows, [saved])

    def test_cancelled_edits_and_removal_do_not_change_saved_horizons(self):
        rows = self.imported().rows
        self.model.update_gen_params({h.PARAMS_KEY: {"version": 1, "rows": rows}})
        before = self.store.get_plan(self.model.plan_id)
        log = self.store.list_change_log(self.model.plan_id)
        dialog = self.widget(HorizonsDialog)

        def cancel(editor):
            editor.findChild(QPlainTextEdit).setPlainText("0, 99\n10, 99")
            editor.reject()
            return editor.result()

        with patch.object(horizon_dialogs, "qt_exec", side_effect=cancel):
            dialog._edit_vertices()
        self.assertEqual(dialog.rows, rows)
        dialog.name.setText("Unsaved name")
        dialog._rename()
        dialog._remove()
        self.assertEqual(dialog.rows, [])
        dialog.reject()
        self.assertEqual(self.store.get_plan(self.model.plan_id), before)
        self.assertEqual(self.store.list_change_log(self.model.plan_id), log)

    def test_repeated_preview_and_clear_have_no_qt_callback_errors(self):
        errors = []
        original = sys.excepthook
        sys.excepthook = lambda *args: errors.append(args)
        try:
            dialog = self.imported()
            for value in (1, 2, 3):
                dialog.kp_origin.setValue(value)
                dialog._preview()
                self.wait_for(dialog)
                dialog.layers.item(0).setCheckState(CHECK_STATE_UNCHECKED)
                dialog.layers.item(0).setCheckState(CHECK_STATE_CHECKED)
            dialog.preview_plot.clear()
            self.assertEqual(dialog.preview_plot._horizon_items, [])
            self.assertTrue(all(not item.isVisible() and item.xData is None
                                for item in dialog.preview_plot._horizon_pool))
            dialog.load_file(self.path)
            self.wait_for(dialog)
        finally:
            sys.excepthook = original
        self.assertFalse(errors, str(errors))

    def test_datum_reference_requires_confirmation_and_uses_seabed(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(dxf(line("H1", 0, -43, 10000, -43) + line("SEABED", 0, -40, 10000, -40)))
        dialog = self.imported()
        dialog.vertical_ref.setCurrentIndex(1)
        dialog.positive_up.setChecked(True)
        dialog.datum.setText("MSL")
        dialog.seabed.setCurrentIndex(dialog.seabed.findData("SEABED"))
        dialog._preview()
        self.wait_for(dialog)
        self.assertFalse(dialog.import_button.isEnabled())
        dialog.datum_confirm.setChecked(True)
        dialog._preview()
        self.wait_for(dialog)
        self.assertTrue(dialog.import_button.isEnabled(), dialog.message.toPlainText())
        self.assertEqual(dialog.rows[0]["segments"], [[[0, 3], [10, 3]]])

    def test_overlay_modes_visibility_and_plan_switch(self):
        rows = self.imported().rows
        self.model.update_gen_params({h.PARAMS_KEY: {"version": 1, "rows": rows}})
        tab = self.widget(GroundTab)
        self.assertEqual(len(tab.plot._horizon_items), 1)
        self.assertIn("H1", tab.plot._readout_text(2, 2.5))
        tab.sync_kp(2)
        self.assertTrue(tab.plot._vline.isVisible())
        self.assertEqual(tab.plot._vline.value(), 2)
        for mode in (1, 2, 0):
            tab._display_changed(mode)
            self.assertEqual(len(tab.plot._horizon_items), 1)
        dialog = self.widget(HorizonsDialog)
        dialog.list.item(0).setCheckState(CHECK_STATE_UNCHECKED)
        dialog._save()
        tab.refresh()
        self.assertEqual(len(tab.plot._horizon_items), 0)
        self.model.close_plan()
        tab.refresh()
        self.assertFalse(tab.horizons_button.isEnabled())
        self.assertEqual(tab.plot._horizon_items, [])

    def test_plan_bathymetry_conversion_keeps_gaps_and_reference(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(dxf(line("H1", 0, -43, 10000, -43)))
        self.model.bathy_profile = PlanProfile(kps=[0, 2, 4, 6, 8, 10],
                                              depths=[40, 40, None, 41, 41, 41], sampled_utc="test sample")
        self.model.profile_stale_reasons = lambda: []
        dialog = self.imported()
        dialog.vertical_ref.setCurrentIndex(1)
        dialog.positive_up.setChecked(True)
        dialog.datum.setText("MSL")
        dialog.datum_confirm.setChecked(True)
        dialog._preview()
        self.wait_for(dialog)
        self.assertTrue(dialog.import_button.isEnabled(), dialog.message.toPlainText())
        self.assertEqual(dialog.rows[0]["segments"], [[[0, 3], [2, 3]], [[6, 2], [8, 2], [10, 2]]])
        self.assertEqual(dialog.rows[0]["source"]["profile_sampled_utc"], "test sample")
        self.assertIn("missing seabed", dialog.message.toPlainText())
        self.model.profile_stale_reasons = lambda: ["raster changed"]
        dialog._preview()
        self.wait_for(dialog)
        self.assertFalse(dialog.import_button.isEnabled())

    def test_route_change_hides_picks_and_reference_change_is_flagged(self):
        rows = self.imported().rows
        self.model.update_gen_params({h.PARAMS_KEY: {"version": 1, "rows": rows}})
        tab = self.widget(GroundTab)
        self.assertEqual(len(tab.plot._horizon_items), 1)
        self.model.plan = self.model._set_kp_datum(self.model.plan, 5)
        tab.refresh()
        self.assertEqual(len(tab.plot._horizon_items), 0)
        self.assertIn("reference", tab.horizon_status.text())
        row = copy.deepcopy(rows[0])
        row["source"]["route"] = route_reference(self.model)
        row["source"]["profile_sampled_utc"] = "old"
        self.model.bathy_profile = PlanProfile(sampled_utc="new")
        self.assertIn("Seabed profile changed", reference_notice(row, self.model))

    def test_failed_save_is_atomic(self):
        dialog = self.widget(HorizonsDialog)
        dialog.rows = self.imported().rows
        before = copy.deepcopy(self.model.plan)
        log = self.store.list_change_log(self.model.plan_id)
        errors = []
        self.model.storeError.connect(errors.append)
        original = self.store.append_change

        def fail(*_args, **_kwargs):
            raise RuntimeError("simulated log failure")

        self.store.append_change = fail
        try:
            dialog._save()
        finally:
            self.store.append_change = original
        self.assertTrue(errors)
        self.assertEqual(self.model.plan, before)
        self.assertEqual(self.store.get_plan(self.model.plan_id), before)
        self.assertEqual(self.store.list_change_log(self.model.plan_id), log)

    def test_display_settings_cache_invalidates_without_losing_horizons(self):
        rows = self.imported().rows
        self.model.update_gen_params({h.PARAMS_KEY: {"version": 1, "rows": rows},
            "kp_datum": {"start_kp": 4, "mode": "cartesian", "grid_crs": "EPSG:32631"},
            "target_burial_ranges": [{"start_kp": 2, "end_kp": 3, "depth_m": 2.5}],
            "cpt": {"max_offset_m": 200}})
        self.assertEqual(self.model.kp_datum_start(), 4)
        self.assertEqual(self.model.kp_mode(), ("cartesian", "EPSG:32631"))
        self.assertEqual(self.model.target_ranges()[0]["depth_m"], 2.5)
        self.assertNotIn(h.PARAMS_KEY, self.model._display_settings())
        self.assertEqual(h.plan_rows(self.model.plan), rows)
        cache = self.model._display_settings()
        self.assertIs(self.model._display_settings(), cache)
        self.model.update_gen_params({"min_section_km": 0.8})
        self.assertEqual(self.model.gen_params().min_section_km, 0.8)
        self.assertIsNot(self.model._display_settings(), cache)
        self.assertEqual(h.plan_rows(self.model.plan), rows)
        self.model.close_plan()
        self.assertEqual(self.model.target_ranges(), [])
        self.assertEqual(self.model.kp_mode(), ("ellipsoidal", ""))

    def test_horizon_cursor_keeps_overlapping_segments_and_gaps(self):
        plot = self.widget(HorizonsDialog).plot
        plot.set_horizons([dict(name="A", colour="#ff0000", segments=[[[0, 1], [10, 1]], [[20, 1], [21, 1]]]),
                           dict(name="B", colour="#00ff00", segments=[[[1, 1], [2, 1]]])])
        self.assertIn("A:", plot._readout_text(1.5, 1))
        self.assertIn("B:", plot._readout_text(1.5, 1))
        self.assertIn("A:", plot._readout_text(3, 1))
        self.assertNotIn("B:", plot._readout_text(3, 1))
        self.assertNotIn("A:", plot._readout_text(15, 1))
        plot.set_horizons([])
        self.assertNotIn("A:", plot._readout_text(1.5, 1))

    def test_background_cancel_close_plan_switch_and_reopen(self):
        before = self.store.get_plan(self.model.plan_id)
        for action in ("cancel", "close", "switch"):
            entered = threading.Event()
            dialog = self.widget(HorizonImportDialog)

            def slow_read(path, progress):
                entered.set()
                while True:
                    progress(10, "Reading test data")
                    time.sleep(0.005)

            ticks = []
            timer = QTimer()
            timer.timeout.connect(lambda: ticks.append(1))
            timer.start(5)
            with patch.object(h, "read_dxf", side_effect=slow_read):
                dialog.load_file(self.path)
                deadline = time.monotonic() + 5
                while (not entered.is_set() or len(ticks) < 3) and time.monotonic() < deadline:
                    QCoreApplication.processEvents()
                    time.sleep(0.005)
                self.assertTrue(entered.is_set())
                self.assertGreaterEqual(len(ticks), 3, "GUI event loop was blocked")
                if action == "cancel":
                    dialog._cancel_work()
                elif action == "close":
                    dialog.reject()
                else:
                    self.model.close_plan()
                self.wait_for(dialog)
            timer.stop()
            self.assertFalse(dialog.rows)
            if action == "switch":
                self.model.load_plan(before["plan_id"])
            self.assertEqual(self.store.get_plan(before["plan_id"]), before)
        self.assertTrue(self.imported().rows)

    def test_destroyed_importer_cancels_without_late_callback(self):
        from qgis.core import QgsApplication
        entered = threading.Event()
        exited = threading.Event()
        dialog = HorizonImportDialog(self.model, Dock())

        def slow_read(path, progress):
            entered.set()
            try:
                while True:
                    progress(10, "Reading test data")
                    time.sleep(0.005)
            finally:
                exited.set()

        with patch.object(h, "read_dxf", side_effect=slow_read):
            dialog.load_file(self.path)
            self.assertTrue(entered.wait(5))
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            deadline = time.monotonic() + 5
            while QgsApplication.taskManager().activeTasks() and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                time.sleep(0.005)
            self.assertTrue(exited.is_set())
        self.assertTrue(self.imported().rows)

    def test_conversion_cancel_discards_result_and_accept_rechecks_reference(self):
        dialog = self.imported()
        original = h.build_horizons

        def cancel_in_conversion(*args):
            raise h.Cancelled()

        # Actual cancel button owns the cancellation state, including a task
        # that completes just before the event is delivered.
        with patch.object(h, "build_horizons", side_effect=cancel_in_conversion):
            dialog._preview()
            dialog._cancel_work()
            self.wait_for(dialog)
        self.assertFalse(dialog.rows)
        self.assertFalse(dialog.import_button.isEnabled())
        with patch.object(h, "build_horizons", wraps=original):
            dialog._preview()
            self.wait_for(dialog)
        self.model.plan = self.model._set_kp_datum(self.model.plan, 2)
        dialog._accept()
        self.assertNotEqual(dialog.result(), DIALOG_ACCEPTED)

    def test_multiple_files_and_import_register_survive_reopen_and_removal(self):
        manager = self.widget(HorizonsDialog)
        first = self.imported().rows
        other_path = os.path.join(self.folder, "second.dxf")
        with open(other_path, "w", encoding="utf-8") as handle:
            handle.write(dxf(line("H1", 0, 5, 10000, 6)))
        second = self.widget(HorizonImportDialog)
        second.load_file(other_path)
        self.wait_for(second)
        second.layers.item(0).setCheckState(CHECK_STATE_CHECKED)
        second._preview()
        self.wait_for(second)
        manager.rows = first + second.rows
        manager._save()
        self.assertEqual(len(self.widget(HorizonsDialog).rows), 2)
        campaign = dict(campaign_id=schema.new_id(), name="Example survey", status="current",
                        config={"files": [{"path": "example.ags"}]}, imported_utc=schema.utc_now_iso())
        self.assertTrue(self.model.save_cpt_campaign(campaign, [], {}))
        tab = InputsTab(self.model, lambda: None)
        self.widgets.append(tab)
        def names():
            return [tab.inputs_table.item(i, 1).text() for i in range(tab.inputs_table.rowCount())]
        self.assertCountEqual(names(), ["section.dxf", "second.dxf", "Example survey"])
        tab.inputs_table.selectRow(0)
        self.assertFalse(tab.edit_input_button.isEnabled())
        self.assertFalse(tab.remove_input_button.isEnabled())
        self.model.load_plan(self.model.plan_id)
        tab.refresh()
        self.assertEqual(len(names()), 3)
        self.model.update_gen_params({h.PARAMS_KEY: {"version": 1, "rows": second.rows}})
        self.model.delete_cpt_campaign(campaign["campaign_id"])
        tab._refresh_inputs()
        self.assertEqual(names(), ["second.dxf"])
        self.model.close_plan()
        tab._refresh_inputs()
        self.assertEqual(names(), [])

    def test_concurrent_horizon_edit_refused(self):
        dialog = self.widget(HorizonsDialog)
        dialog.rows = self.imported().rows
        self.model.update_gen_params({h.PARAMS_KEY: {"version": 1, "rows": dialog.rows}})
        dialog.rows = []
        messages = []
        original = QMessageBox.warning
        QMessageBox.warning = lambda *_args: messages.append(_args[-1])
        try:
            dialog._save()
        finally:
            QMessageBox.warning = original
        self.assertIn("Saved horizons changed", messages[0])
        self.assertTrue(h.plan_rows(self.model.plan))
