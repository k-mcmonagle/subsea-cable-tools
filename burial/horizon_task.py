# -*- coding: utf-8 -*-
"""Cancellable horizon work. Operations receive plain snapshots, never widgets."""

import time

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal, pyqtSlot

from .horizons import Cancelled


class HorizonTask(QgsTask):
    completed = pyqtSignal(object, str, bool)
    message = pyqtSignal(str)

    def __init__(self, description, operation):
        super().__init__(description, getattr(QgsTask, "Flag", QgsTask).CanCancel)
        self.operation = operation
        self.result = None
        self.error = ""
        self._last_update = 0.0
        self._last_yield = 0.0

    @pyqtSlot()
    def owner_destroyed(self):
        self.cancel()

    def checkpoint(self, percent=None, message=""):
        if self.isCanceled():
            raise Cancelled()
        now = time.monotonic()
        if now - self._last_yield >= 0.025:
            # Long Python coordinate loops compete with Python Qt callbacks
            # for the GIL. Give the GUI a scheduling opportunity as well as
            # checking the task's cancellation flag.
            time.sleep(0.001)
            self._last_yield = now
        if (percent is not None or message) and (now - self._last_update >= 0.1 or percent == 100):
            if percent is not None:
                self.setProgress(percent)
            if message:
                self.message.emit(message)
            self._last_update = now

    def run(self):
        try:
            self.checkpoint()
            self.result = self.operation(self.checkpoint)
            self.checkpoint(100)
            return True
        except Cancelled:
            return False
        except MemoryError:
            self.error = "Not enough memory for this DXF. No horizons have been imported."
            return False
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            return False
        finally:
            self.operation = None  # release input snapshots promptly

    def finished(self, success):
        # QgsTask guarantees main-thread delivery. QObject-bound receivers are
        # disconnected automatically if the dialog has been destroyed.
        self.completed.emit(self.result if success else None, self.error, self.isCanceled())
        self.result = None
