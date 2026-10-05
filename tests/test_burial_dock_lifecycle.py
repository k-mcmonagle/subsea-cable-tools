# -*- coding: utf-8 -*-
"""Burial Planner dock lifecycle checks (requires the QGIS API).

The plugin keeps one dock and re-shows it, so closing the window must be
reversible: close suspends (project hooks, watchdog, background tasks and
the SQL handle released), the next show re-arms everything and re-reads the
project's plan file, and plugin unload (``shutdown``) is final. Background
task results that arrive after a close / unload — or after Stop — are
ignored instead of being written into a closed store.

No QSettings are written (the window-state saver is stubbed) and the stall
watchdog logs to a temporary file.
"""

from __future__ import annotations

import os
import tempfile

from qgis.core import QgsProject, QgsTask, QgsVectorLayer
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QShowEvent
from qgis.PyQt.QtWidgets import QMessageBox
from qgis.PyQt.QtXml import QDomDocument

from ..burial import burial_dock, gpkg_sql, watchdog
from ..burial import store as store_module
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore

_STATUS = getattr(QgsTask, "TaskStatus", QgsTask)


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


class _Iface:
    def __init__(self):
        self._canvas = QgsMapCanvas()

    def mapCanvas(self):
        return self._canvas

    def layerTreeView(self):
        return None


class _FakeTask:
    """Stands in for a QgsTask whose worker is still running."""

    def __init__(self, status=_STATUS.Running, plan_id=""):
        self._status = status
        self.plan_id = plan_id
        self.cancelled = False
        self.error = None
        self.results = []
        self.series = [(0.0, 10.0)]
        self.run_check_ids = ["c1"]
        self.hazards = []
        self.warnings = []

    def status(self):
        return self._status

    def cancel(self):
        self.cancelled = True


class _Harness:
    """Build a dock on a fresh plan file with dialogs and settings stubbed."""

    def __enter__(self):
        self._saved_boxes = {name: getattr(QMessageBox, name)
                             for name in ("warning", "information",
                                          "question")}
        for name in self._saved_boxes:
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: 0))
        self._saved_log_path = watchdog.default_log_path
        folder = tempfile.mkdtemp(prefix="bp_dock_life_")
        watchdog.default_log_path = lambda: os.path.join(folder, "stall.log")
        self.store = BurialStore(os.path.join(folder, "plans.gpkg"))
        self.store.migrate()
        self._saved_project_path = store_module.project_gpkg_path()
        store_module.set_project_gpkg_path(self.store.gpkg_path)
        self.plan_id = PlanModel(self.store).create_plan("Life", "plough")
        self.dock = burial_dock.BurialPlannerDock(_Iface())
        self.dock._save_window_state = lambda: None  # never touch QSettings
        QCoreApplication.processEvents()
        return self

    def __exit__(self, *_exc):
        try:
            self.dock.shutdown()
            self.dock.deleteLater()
            QCoreApplication.processEvents()
        finally:
            for name, value in self._saved_boxes.items():
                setattr(QMessageBox, name, value)
            watchdog.default_log_path = self._saved_log_path
            store_module.set_project_gpkg_path(self._saved_project_path or "")
        return False


def _show(dock) -> None:
    """What ``show()`` delivers, without putting a window on screen."""
    QCoreApplication.sendEvent(dock, QShowEvent())


def test_close_reopen_rearms_hooks_and_follows_project() -> bool:
    project = QgsProject.instance()
    with _Harness() as h:
        dock = h.dock
        calls = {"refresh": 0}
        real_refresh = dock.refresh

        def counting_refresh():
            calls["refresh"] += 1
            real_refresh()

        dock.refresh = counting_refresh
        ok = len(dock._project_hooks) == 3 and dock._watchdog.active
        ok = ok and dock.workflow_tabs.isTabEnabled(0)
        ok = ok and all(not dock.workflow_tabs.isTabEnabled(i) for i in (1, 2, 3))
        ok = ok and dock.tabs.isTabEnabled(dock.tabs.indexOf(dock.ground_tab))
        ok = ok and dock.model.plan_id == h.plan_id
        dock.cursor_outline_toggle.setChecked(True)

        dock.close()  # the window's X / View > Panels untick
        QCoreApplication.processEvents()
        closed = (dock._suspended, len(dock._project_hooks),
                  dock._watchdog.active,
                  gpkg_sql._key(h.store.gpkg_path) in gpkg_sql._connections,
                  dock.cursor_outline_toggle.isChecked())
        ok = ok and closed == (True, 0, False, False, False)
        # While closed, project events are not followed.
        project.readProject.emit(QDomDocument())
        layer = QgsVectorLayer("Point?crs=EPSG:4326", "life_probe", "memory")
        project.addMapLayer(layer)
        QCoreApplication.processEvents()
        ok = ok and calls["refresh"] == 0 and not dock._layers_timer.isActive()
        project.removeMapLayer(layer.id())

        # Reopened from View > Panels: hooks + watchdog back, and the plan
        # file is re-resolved once the event loop runs.
        _show(dock)
        reopened = (dock._suspended, len(dock._project_hooks),
                    dock._watchdog.active)
        QCoreApplication.processEvents()
        ok = ok and reopened == (False, 3, True) and calls["refresh"] == 1

        # Reopened by the plugin (show() then refresh()): one refresh only.
        dock.close()
        _show(dock)
        dock.refresh()
        QCoreApplication.processEvents()
        ok = ok and calls["refresh"] == 2

        # Project events are followed again.
        project.readProject.emit(QDomDocument())
        QCoreApplication.processEvents()
        ok = ok and calls["refresh"] == 3 and dock.model.plan_id == h.plan_id
        layer = QgsVectorLayer("Point?crs=EPSG:4326", "life_probe", "memory")
        project.addMapLayer(layer)
        ok = ok and dock._layers_timer.isActive()
        project.removeMapLayer(layer.id())
        # The store reconnects lazily after the close released it.
        ok = ok and [p["plan_id"] for p in dock.store.list_plans()] \
            == [h.plan_id]

        # Plugin unload is final: a later show does not re-arm anything.
        dock.shutdown()
        _show(dock)
        ok = ok and dock._shutting_down and not dock._project_hooks \
            and not dock._watchdog.active
        return _result("close suspends, reopen re-arms and follows the "
                       "project, unload is final", ok,
                       f"closed={closed} reopened={reopened} "
                       f"refreshes={calls['refresh']}")


def test_late_analysis_results_are_ignored() -> bool:
    with _Harness() as h:
        dock = h.dock
        applied = []
        dock._apply_analysis_results = applied.append
        dock._task_plan_id = dock.model.plan_id

        # Stop: the reference stays until the task reports back.
        stopping = _FakeTask()
        dock._task = stopping
        dock.cancel_analysis()
        ok = stopping.cancelled and dock._task is stopping
        dock._analysis_finished(stopping)
        ok = ok and dock._task is None and not applied

        # Window closed mid-run; the run completes before the cancel lands.
        closing = _FakeTask()
        dock._task = closing
        dock.close()
        ok = ok and closing.cancelled and dock._task is None
        closing.cancelled = False
        dock._analysis_finished(closing)
        ok = ok and not applied
        ok = ok and "window closed" in dock.builder_tab.run_status.text()
        _show(dock)
        QCoreApplication.processEvents()

        # A task that ended without its callback never blocks a new run.
        dock._task = _FakeTask(status=_STATUS.Complete)
        ok = ok and not dock._analysis_running() and dock._task is None

        # The current task's results are still applied.
        current = _FakeTask()
        dock._task = current
        dock._analysis_finished(current)
        ok = ok and applied == [current]
        return _result("late / stopped analysis results are ignored; a lost "
                       "callback cannot block runs", ok)


def test_late_profile_and_scan_results_are_ignored() -> bool:
    with _Harness() as h:
        dock = h.dock
        saved = []
        dock.model.save_profile = saved.append

        # Stop profile refresh, then the worker finishes anyway.
        stopped = _FakeTask()
        dock._profile_task = stopped
        token = dock._profile_generation
        dock._cancel_profile_refresh()
        dock._profile_finished(stopped, token)
        ok = not saved and "cancelled" in dock.profile_status.text()

        # Window closed mid-sampling.
        closing = _FakeTask()
        dock._profile_task = closing
        token = dock._profile_generation
        dock.close()
        dock._profile_finished(closing, token)
        ok = ok and not saved and dock._profile_task is None
        ok = ok and "window closed" in dock.profile_status.text()
        _show(dock)
        QCoreApplication.processEvents()

        # Risk scan: a scan forgotten by shutdown must neither apply its
        # hazards nor clear the reference of the scan that replaced it.
        risk_tab = dock.risk_tab
        applied = []
        dock.model.apply_risk_scan = lambda *a, **k: applied.append(a)
        old = _FakeTask(plan_id=dock.model.plan_id)
        risk_tab._scan_task = old
        risk_tab.shutdown()
        new = _FakeTask(plan_id=dock.model.plan_id)
        risk_tab._scan_task = new
        risk_tab._scan_finished(old)
        ok = ok and old.cancelled and risk_tab._scan_task is new \
            and not applied
        risk_tab._scan_task = _FakeTask(status=_STATUS.Terminated)
        risk_tab._run_checks()  # a dead reference does not block a run
        ok = ok and risk_tab._scan_task is None
        risk_tab._scan_task = None

        # Installation paths: shutdown resets the running-state UI.
        paths_tab = dock.paths_tab
        paths_tab._task = _FakeTask()
        paths_tab.progress.setVisible(True)
        paths_tab.generate_button.setEnabled(False)
        paths_tab.shutdown()
        ok = ok and paths_tab._task is None and paths_tab.progress.isHidden()
        ok = ok and paths_tab.generate_button.isEnabled()
        return _result("late profile / scan results ignored; paths UI reset "
                       "on close", ok)


def run_all() -> list:
    return [
        test_close_reopen_rearms_hooks_and_follows_project(),
        test_late_analysis_results_are_ignored(),
        test_late_profile_and_scan_results_are_ignored(),
    ]


if __name__ == "__main__":  # pragma: no cover
    run_all()
