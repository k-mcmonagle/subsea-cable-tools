"""Migration must stop before writes without a backup; failed analysis is explicit."""
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qgis.PyQt.QtCore import QEventLoop, QTimer

from ..burial import burial_dock, generation, gpkg_sql, schema
from ..burial.geopackage_health_dialog import GeoPackageHealthDialog
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..planner.store import PlannerStore
from ..workbench.store import WorkbenchStore

REQUIRES_QGIS = True


class GeoPackageRecoveryTests(unittest.TestCase):
    def test_failed_backup_prevents_schema_creation_in_all_stores(self):
        for cls in (BurialStore, PlannerStore, WorkbenchStore):
            with self.subTest(store=cls.__name__):
                store = cls("unused.gpkg")
                with patch.object(store, "exists", return_value=True), \
                     patch.object(store, "read_meta", return_value={"schema_version": "1"}), \
                     patch.object(store, "backup_before", side_effect=OSError("backup failed")), \
                     patch.object(store, "ensure_created") as ensure:
                    with self.assertRaisesRegex(OSError, "backup failed"):
                        store.migrate()
                    ensure.assert_not_called()

    def test_corrupt_file_never_switches_to_legacy_writer(self):
        folder = Path(tempfile.mkdtemp(prefix="bp_corrupt_test_"))
        path = folder / "plans.gpkg"
        path.write_bytes(b"not a database")
        store = BurialStore(str(path))
        original = path.read_bytes()
        for probe in (store.exists, store._sql, lambda: store._table_exists(schema.TABLE_META)):
            with self.assertRaises(sqlite3.DatabaseError):
                probe()
        self.assertIsNone(store._sql_mode)
        self.assertEqual(path.read_bytes(), original)

    def test_model_retains_but_marks_unsaved_exclusions(self):
        folder = Path(tempfile.mkdtemp(prefix="bp_unsaved_test_"))
        store = BurialStore(str(folder / "plans.gpkg"))
        self.addCleanup(store.close)
        store.migrate()
        model = PlanModel(store)
        plan_id = model.create_plan("Trial", schema.METHOD_PLOUGH)
        self.assertTrue(plan_id)
        model.load_plan(plan_id)
        errors = []
        model.storeError.connect(errors.append)
        params = model.gen_params()
        with patch.object(store, "save_analysis", side_effect=sqlite3.DatabaseError(
                "database disk image is malformed")):
            self.assertFalse(model.save_analysis(generation.ResolutionContext(), [], params, []))
        self.assertTrue(model.analysis_row["_unsaved"])
        self.assertTrue(any("NOT SAVED" in warning for warning in model.analysis_status()["warnings"]))
        self.assertIn("Check / recover", errors[0])
        self.assertFalse(store.read_plan_table(schema.TABLE_ANALYSIS, plan_id))
        self.assertTrue(model.save_analysis(generation.ResolutionContext(), [], params, []))
        self.assertNotIn("_unsaved", model.analysis_row)
        self.assertFalse(any("NOT SAVED" in warning for warning in model.analysis_status()["warnings"]))
        self.assertEqual(len(store.read_plan_table(schema.TABLE_ANALYSIS, plan_id)), 1)

    def test_generation_stops_after_analysis_save_failure(self):
        params = generation.GenParams(0, 1, method=schema.METHOD_PLOUGH)
        model = Mock()
        model.save_analysis.return_value = False
        dock = SimpleNamespace(model=model, _task_params=params, _task_rules=[],
                               rules_tab=Mock(), builder_tab=Mock(),
                               _refresh_profile_overlays=Mock(), _generate_after_analysis=True)
        resolved = SimpleNamespace(per_method={}, rule_hits={})
        with patch.object(burial_dock.generation, "resolve_stack", return_value=(resolved, [], [], [])):
            burial_dock.BurialPlannerDock._apply_analysis_results(dock, SimpleNamespace(results=[]))
        dock.builder_tab.analysis_finished.assert_called_once()
        self.assertIn("NOT SAVED", dock.builder_tab.analysis_finished.call_args[0][0])
        model.apply_generation.assert_not_called()

    def test_commit_failure_rolls_back_transaction(self):
        conn = Mock(in_transaction=False)
        conn.commit.side_effect = sqlite3.DatabaseError("commit failed")
        with self.assertRaises(sqlite3.DatabaseError):
            with gpkg_sql.transaction(conn):
                pass
        conn.rollback.assert_called_once()

    def test_dialog_is_available_without_open_store(self):
        dialog = GeoPackageHealthDialog("missing.gpkg")
        self.assertTrue(dialog.check.isEnabled())
        self.assertFalse(dialog.copy.isEnabled())
        dialog.reject()

    def test_dialog_worker_populates_checked_copy(self):
        folder = Path(tempfile.mkdtemp(prefix="bp_health_ui_"))
        path = folder / "plans.gpkg"
        store = BurialStore(str(path))
        store.migrate()
        store.close()
        dialog = GeoPackageHealthDialog(str(path))
        self.addCleanup(dialog.reject)
        from ..geopackage_health import inspect_file
        dialog._run(lambda _task: [inspect_file(str(path))])
        self.assertFalse(dialog.check.isEnabled())
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: loop.quit() if dialog._task is None else None)
        timer.start()
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        timer.stop()
        self.assertIsNone(dialog._task, dialog.status.text())
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertTrue(dialog.reports[0].healthy)
        self.assertTrue(dialog.copy.isEnabled())
