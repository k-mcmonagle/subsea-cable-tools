# -*- coding: utf-8 -*-
"""GUI-thread stall watchdog for the Burial Planner (stdlib only).

QGIS "not responding" gives no diagnostics: whatever the GUI thread was
doing is lost when the user kills the process. While the dock is open this
watchdog re-arms ``faulthandler.dump_traceback_later`` from a main-thread
``QTimer``; if the event loop stops servicing timers for longer than the
threshold, the C-level dumper (which needs no Python bytecode to run and
therefore works through a deadlock or a busy loop) writes every thread's
stack to a log file in the QGIS profile folder. Cost while healthy: one
cancel + re-arm every ``interval_s`` seconds.

The log is append-only and human-readable; the newest dump is at the end.
"""

from __future__ import annotations

import datetime
import faulthandler
import os
from typing import Optional

from qgis.PyQt.QtCore import QObject, QTimer

LOG_NAME = "subsea_cable_tools_stall.log"


def default_log_path() -> str:
    """``<QGIS profile>/subsea_cable_tools_stall.log`` (temp dir fallback)."""
    try:
        from qgis.core import QgsApplication

        base = QgsApplication.qgisSettingsDirPath()
    except Exception:
        base = ""
    if not base:
        import tempfile

        base = tempfile.gettempdir()
    return os.path.join(base, LOG_NAME)


class StallWatchdog(QObject):
    """Dump all thread stacks when the GUI thread stalls for ``threshold_s``."""

    def __init__(self, threshold_s: float = 8.0, interval_s: float = 2.0,
                 log_path: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.threshold_s = max(float(threshold_s), 1.0)
        self.interval_s = max(min(float(interval_s), self.threshold_s / 2.0),
                              0.25)
        self.log_path = log_path or default_log_path()
        self._file = None
        self._timer = QTimer(self)
        self._timer.setInterval(int(self.interval_s * 1000))
        self._timer.timeout.connect(self._rearm)
        self.error = ""

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self) -> bool:
        if self._file is not None:
            return True
        try:
            self._file = open(self.log_path, "a", encoding="utf-8")
            self._file.write(
                f"\n=== Burial Planner stall watchdog armed "
                f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                f"(threshold {self.threshold_s:g} s; a dump below means the "
                f"QGIS GUI thread was unresponsive for that long) ===\n")
            self._file.flush()
        except Exception as exc:  # unwritable profile dir, etc.
            self.error = str(exc)
            self._file = None
            return False
        self._rearm()
        self._timer.start()
        return True

    def stop(self) -> None:
        self._timer.stop()
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        handle, self._file = self._file, None
        if handle is not None:
            try:
                handle.write(
                    f"=== watchdog disarmed "
                    f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
                handle.close()
            except Exception:
                pass

    def _rearm(self) -> None:
        """Runs on the GUI thread; not running it is what trips the dump."""
        if self._file is None:
            return
        try:
            faulthandler.cancel_dump_traceback_later()
            # exit=False: report, never kill QGIS. repeat=False: one dump per
            # stall (the next healthy tick re-arms).
            faulthandler.dump_traceback_later(
                self.threshold_s, repeat=False, file=self._file, exit=False)
        except Exception as exc:
            self.error = str(exc)
            self._timer.stop()
