# -*- coding: utf-8 -*-
"""Background task for the Lay Assessment's seabed work.

Runs a worker-safe callable ``work(cancel, progress)`` (seabed sampling
through the Depth Profile engine, then the numpy seabed model) off the GUI
thread; ``on_finished(task)`` runs on the main thread with ``result``,
``error`` or ``cancelled`` set.
"""

from __future__ import annotations

import logging
from typing import Callable

from qgis.core import QgsTask

from ..depth_profile_core import ProfileCancelled
from ..plugin_log import log_exception


def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


class AssessmentTask(QgsTask):
    def __init__(self, description: str, work: Callable, on_finished: Callable[["AssessmentTask"], None]):
        super().__init__(description, _task_flag("CanCancel"))
        self._work = work
        self._on_finished = on_finished
        self.result = None
        self.error = None
        self.cancelled = False
        self._last = -1.0

    def _progress(self, fraction: float) -> None:
        pct = 100.0 * max(0.0, min(1.0, fraction))
        if pct - self._last >= 0.5 or pct >= 100.0:
            self._last = pct
            self.setProgress(pct)

    def run(self) -> bool:
        try:
            self.result = self._work(self.isCanceled, self._progress)
            return True
        except ProfileCancelled:
            self.cancelled = True
            return False
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            log_exception("Lay Assessment: seabed modelling failed", level=logging.ERROR)
            return False

    def finished(self, ok: bool) -> None:
        if not ok and self.error is None and not self.cancelled:
            self.cancelled = self.isCanceled()
            if not self.cancelled:
                self.error = "The seabed task failed."
        try:
            self._on_finished(self)
        except Exception:  # never crash QGIS from a completion callback
            log_exception("Lay Assessment: applying the seabed result failed", level=logging.ERROR)
