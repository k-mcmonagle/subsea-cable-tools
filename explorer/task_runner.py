# -*- coding: utf-8 -*-
"""Run one callable on a QGIS worker thread with visible progress.

:class:`TaskRunner` is owned by a panel and runs at most one job at a time.
``work(feedback)`` executes inside a :class:`QgsTask` (``feedback`` offers
``setProgress`` / ``isCanceled``); ``finish(result)`` and ``on_error(message)``
run back on the GUI thread. A cancellable ``QProgressDialog`` is shown for
foreground jobs; background jobs (an inventory scan) report through a status
callback instead so the panel stays usable.

Every Qt slot is a bound method on this ``QObject`` so nothing is garbage
collected while the task runs (closure slots crash QGIS - see the project
notes). ``run_async = False`` executes the job inline on the calling thread,
which is how the tests drive the panels deterministically.
"""

from __future__ import annotations

from typing import Callable, Optional

from qgis.PyQt.QtCore import QObject, Qt
from qgis.PyQt.QtWidgets import QMessageBox, QProgressDialog
from qgis.core import QgsApplication, QgsTask

_WINDOW_MODAL = getattr(getattr(Qt, "WindowModality", Qt), "WindowModal")


def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


_CAN_CANCEL = _task_flag("CanCancel")


class _InlineFeedback:
    """Feedback stand-in when a job runs on the calling thread."""

    def setProgress(self, _value) -> None:  # noqa: N802 (QgsTask API)
        pass

    def isCanceled(self) -> bool:  # noqa: N802 (QgsTask API)
        return False


class CallableTask(QgsTask):
    """A QgsTask around ``work(feedback)``; the task itself is the feedback."""

    def __init__(self, description: str, work: Callable, cancellable: bool = True):
        super().__init__(description, _CAN_CANCEL if cancellable else 0)
        self._work = work
        self.result = None
        self.error: Optional[str] = None

    def run(self) -> bool:  # worker thread
        try:
            self.result = self._work(self)
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            return False
        if self.isCanceled():
            self.error = "Cancelled."
            return False
        self.setProgress(100.0)
        return True


class TaskRunner(QObject):
    #: Tests set this to ``False`` to run jobs inline.
    run_async = True

    def __init__(self, parent_widget, title: str = "Cable Lay Data Explorer"):
        super().__init__(parent_widget)
        self._parent_widget = parent_widget
        self._title = title
        self._task: Optional[CallableTask] = None
        self._progress: Optional[QProgressDialog] = None
        self._finish: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_progress: Optional[Callable] = None
        self._description = ""

    @property
    def busy(self) -> bool:
        return self._task is not None

    def start(
        self,
        description: str,
        work: Callable,
        finish: Callable,
        on_error: Optional[Callable[[str], None]] = None,
        show_dialog: bool = True,
        indeterminate: bool = False,
        cancellable: bool = True,
        on_progress: Optional[Callable[[float], None]] = None,
    ) -> bool:
        """Start ``work``; returns False if a job is already running."""
        if self._task is not None:
            return False
        self._description = description
        manager = QgsApplication.taskManager() if self.run_async else None
        if manager is None:
            try:
                result = work(_InlineFeedback())
            except Exception as exc:
                self._report_error(on_error, str(exc) or exc.__class__.__name__)
                return True
            finish(result)
            return True

        if show_dialog:
            maximum = 0 if indeterminate else 100
            progress = QProgressDialog(
                description + "…", "Cancel" if cancellable else None, 0, maximum,
                self._parent_widget.window() if self._parent_widget is not None else None,
            )
            progress.setWindowTitle(self._title)
            progress.setWindowModality(_WINDOW_MODAL)
            progress.setMinimumDuration(0)
            progress.setAutoClose(False)
            progress.setAutoReset(False)
            progress.setValue(0)
            self._progress = progress
        task = CallableTask(description, work, cancellable)
        self._task = task
        self._finish = finish
        self._on_error = on_error
        self._on_progress = on_progress
        task.progressChanged.connect(self._task_progress)
        task.taskCompleted.connect(self._task_completed)
        task.taskTerminated.connect(self._task_terminated)
        if self._progress is not None:
            self._progress.canceled.connect(task.cancel)
        manager.addTask(task)
        if self._progress is not None:
            self._progress.show()
        return True

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    # -- slots (bound methods, never closures) ------------------------------
    def _task_progress(self, value) -> None:
        if self._progress is not None and self._progress.maximum() > 0:
            self._progress.setValue(int(value))
        if self._on_progress is not None:
            try:
                self._on_progress(float(value))
            except Exception:
                pass

    def _task_completed(self) -> None:
        task, finish = self._task, self._finish
        self._teardown()
        if task is not None and finish is not None:
            finish(task.result)

    def _task_terminated(self) -> None:
        task, on_error = self._task, self._on_error
        error = getattr(task, "error", None) or "The operation did not complete."
        self._teardown()
        self._report_error(on_error, error)

    def _teardown(self) -> None:
        if self._progress is not None:
            self._progress.reset()
            self._progress.deleteLater()
            self._progress = None
        self._task = None
        self._finish = None
        self._on_error = None
        self._on_progress = None

    def _report_error(self, on_error: Optional[Callable], message: str) -> None:
        if on_error is not None:
            on_error(message)
            return
        if message == "Cancelled.":
            return
        QMessageBox.critical(self._parent_widget, self._description or self._title, message)
