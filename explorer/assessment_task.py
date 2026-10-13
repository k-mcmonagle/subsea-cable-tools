# -*- coding: utf-8 -*-
"""Background task for a Lay Assessment run.

Runs a worker-safe callable ``work(cancel, progress)`` (cable makeup, record
checks, seabed sampling through the Depth Profile engine and the numpy
seabed model) off the GUI thread. ``progress(fraction, text)`` drives the
task's progress and :attr:`stageChanged` (what it is doing, for the panel);
``on_finished(task)`` runs on the main thread with ``result``, ``error`` or
``cancelled`` set.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from ..depth_profile_core import ProfileCancelled
from ..laydata.lay_assessment import Cancelled
from ..plugin_log import log_exception


def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


class AssessmentTask(QgsTask):
    stageChanged = pyqtSignal(str)

    def __init__(self, description: str, work: Callable, on_finished: Callable[["AssessmentTask"], None]):
        super().__init__(description, _task_flag("CanCancel"))
        self._work = work
        self._on_finished = on_finished
        self.result = None
        self.error = None
        self.cancelled = False
        self.info = None   # the caller's run details, handed back with the result
        self._last = -1.0
        self._stage = ""

    def _progress(self, fraction: float, text: Optional[str] = None) -> None:
        pct = 100.0 * max(0.0, min(1.0, fraction))
        if pct - self._last >= 0.5 or pct >= 100.0 or pct < self._last:
            self._last = pct
            self.setProgress(pct)
        if text and text != self._stage:
            self._stage = text
            self.stageChanged.emit(text)

    def run(self) -> bool:
        try:
            self.result = self._work(self.isCanceled, self._progress)
            return True
        except (ProfileCancelled, Cancelled):
            self.cancelled = True
            return False
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            log_exception("Lay Assessment: the assessment failed", level=logging.ERROR)
            return False

    def finished(self, ok: bool) -> None:
        if not ok and self.error is None and not self.cancelled:
            self.cancelled = self.isCanceled()
            if not self.cancelled:
                self.error = "The assessment task failed."
        try:
            self._on_finished(self)
        except Exception:  # never crash QGIS from a completion callback
            log_exception("Lay Assessment: applying the result failed", level=logging.ERROR)
