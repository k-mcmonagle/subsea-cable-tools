# -*- coding: utf-8 -*-
"""The Burial Planner's acquired-data controller: the data file, background work, KP placement.

One per dock. It follows the project like every tool (see DECISIONS.md,
"Tools across project changes"): :meth:`leave_project` on
``aboutToBeCleared`` stops the running task and lets go of the file;
:meth:`follow_project` opens the next project's data file only if the
project names one. The file is created (and named in the project) when the
user first imports (:meth:`ensure_store`).

Background work runs one task at a time (:meth:`run`); a result that
arrives after the project changed, or after a newer task started, is
dropped. While a task writes, the tabs do not read the file.

:func:`dataset_kp_map` places a dataset's KPs on the plan's route, as the
KP-range tables do (:func:`..rpl_reference.table_kp_map`): KPs on the
plan's own RPL are used as they are (shifted if that RPL's start KP has
been renumbered since), KPs on another RPL are translated by seabed
position.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtCore import QObject, pyqtSignal

from ...plugin_log import log_exception
from ..kp_rereference import KpMap
from . import location
from .store import AcquiredStore, AcquiredStoreError
from .task import WorkTask


class AcquiredController(QObject):
    changed = pyqtSignal()                  # datasets or files changed, or another file is open
    busyChanged = pyqtSignal(bool)
    progressChanged = pyqtSignal(float, str)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.store: Optional[AcquiredStore] = None
        self.path: Optional[str] = None
        self.state = location.STATE_NONE
        self._task: Optional[WorkTask] = None
        self._generation = 0
        self._stage = ""

    # -- the data file -------------------------------------------------------------
    def follow_project(self) -> None:
        """Open the current project's data file, if it names one (creates nothing)."""
        self._release()
        path, state = location.resolve()
        self.path, self.state = path, state
        if state == location.STATE_OK:
            try:
                self.store = AcquiredStore(path).open()
            except AcquiredStoreError as exc:
                self.state = location.STATE_INVALID
                log_exception(f"Burial Planner: burial data file not opened: {exc}")
        self.changed.emit()

    def leave_project(self) -> None:
        """``aboutToBeCleared``: stop work and let go of the file; nothing is written."""
        self.cancel()
        self._generation += 1
        self._release()
        self.path, self.state = None, location.STATE_NONE
        self.changed.emit()

    def _release(self) -> None:
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                log_exception("Burial Planner: closing the burial data file failed")
        self.store = None

    def default_path(self) -> str:
        return location.default_data_path()

    def ensure_store(self) -> AcquiredStore:
        """The data file, created and named in the project on the user's first import."""
        if self.store is None:
            self.store = location.open_or_create(path=self.path if self.state == location.STATE_MISSING else None)
            self.path, self.state = self.store.path, location.STATE_OK
        return self.store

    def open_file(self, path: str) -> None:
        """Use another (existing or new) data file for this project."""
        self._release()
        self.store = location.open_or_create(path=path)
        self.path, self.state = self.store.path, location.STATE_OK
        self.changed.emit()

    def datasets(self) -> List[Dict]:
        if self.store is None or self.busy:
            return []
        try:
            return self.store.list_datasets()
        except AcquiredStoreError:
            log_exception("Burial Planner: reading the burial data file failed")
            return []

    # -- background work ----------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._task is not None

    @property
    def stage(self) -> str:
        return self._stage

    def run(self, title: str, work: Callable, done: Callable[[WorkTask], None]) -> bool:
        """Start ``work(cancelled, progress)`` in the background; ``done(task)`` gets the result.

        False when another task is running.
        """
        if self._task is not None:
            return False
        generation = self._generation

        def finished(task: WorkTask) -> None:
            if task is not self._task:
                return
            self._task = None
            self._stage = ""
            self.busyChanged.emit(False)
            if generation != self._generation:
                return                       # the project changed: drop it
            done(task)

        task = WorkTask(title, work, finished)
        task.progressChanged.connect(lambda value, t=task: self._on_progress(t, value))
        task.stageChanged.connect(lambda text, t=task: self._on_stage(t, text))
        self._task = task
        self._stage = title
        self.busyChanged.emit(True)
        QgsApplication.taskManager().addTask(task)
        return True

    def _on_progress(self, task: WorkTask, value: float) -> None:
        if task is self._task:
            self.progressChanged.emit(float(value), self._stage)

    def _on_stage(self, task: WorkTask, text: str) -> None:
        if task is self._task:
            self._stage = text
            self.progressChanged.emit(float(task.progress()), text)

    def cancel(self) -> None:
        task = self._task
        if task is not None:
            try:
                task.cancel()
            except RuntimeError:
                pass

    def shutdown(self) -> None:
        self.leave_project()


def dataset_kp_map(model, dataset: Dict) -> Tuple[Optional[Dict], str]:
    """``(KpMap dict or None, note)`` placing ``dataset``'s KPs on the plan's route. Raises ValueError."""
    route = getattr(model, "route", None)
    if route is None:
        raise ValueError("The plan has no route: set its RPL on the Inputs tab.")
    ref = str(dataset.get("rpl_id") or "")
    recorded = dataset.get("rpl_start_kp")
    label = dataset.get("rpl_label") or "its RPL"
    plan_rpl = str(getattr(model, "resolved_rpl_id", "") or "")
    if not ref or ref == plan_rpl:
        shift = 0.0 if recorded is None else float(route.start_kp_km) - float(recorded)
        if abs(shift) <= 5e-7:
            return None, "KPs on this plan's RPL"
        return (KpMap.shift(shift).to_dict(),
                f"KPs on this plan's RPL, shifted {shift * 1000.0:+.1f} m for its new start KP")
    from ..rereference_qgis import geometry_map, rpl_label, rpl_route
    store = getattr(model, "workbench_store", None)
    if store is None:
        raise ValueError("The Cable Workbench is needed to translate KPs from another RPL.")
    rpl = store.get_rpl(ref)
    if rpl is None:
        raise ValueError(f"{label} is no longer in the Workbench: register it again or change the "
                         "dataset's RPL.")
    src_route, _distance, _positions = rpl_route(store, rpl, QgsProject.instance(), model.kp_mode())
    kp_map = geometry_map(src_route, route, source_label=rpl_label(rpl),
                          target_label=model.plan.get("rpl_name") or "plan route")
    data = kp_map.to_dict()
    note = f"KPs translated from {rpl_label(rpl)} to this plan's route by seabed position"
    if recorded is not None:
        shift = float(src_route.start_kp_km) - float(recorded)
        if abs(shift) > 5e-7:
            # The source RPL was renumbered since: quoted KPs move by the change first.
            data["anchors"] = [[a - shift, b] for a, b in data["anchors"]]
            data["gap_ranges"] = [[a - shift, b - shift] for a, b in data["gap_ranges"]]
            note += f" (its start KP moved {shift * 1000.0:+.1f} m since)"
    return data, note


def file_dialog_filter() -> str:
    return "Burial data (*.csv *.txt *.dat *.asc *.xyz *.xlsx *.xlsm);;All files (*)"
