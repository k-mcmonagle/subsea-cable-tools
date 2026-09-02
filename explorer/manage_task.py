# -*- coding: utf-8 -*-
"""Background execution of cable-lay management edits.

The Manage panel never edits the project layer directly: that layer carries
the user's provider filter (``subsetString``) so any scan over it silently
skips hidden rows, and provider writes on the GUI thread freeze QGIS for the
duration of a multi-million-row update. Instead each edit opens a *private*
``QgsVectorLayer`` on the same GeoPackage table inside a :class:`QgsTask`
(the same pattern the processing algorithms use), runs the operation from
:mod:`processing.cable_lay_manage_ops` against it with the task as the
progress / cancel feedback, and the panel reloads the project layer on the
main thread once the task completes.

:func:`run_edit_sync` is the same contract without a thread, used by tests
(and as a fallback when no task manager is available).
"""

from __future__ import annotations

from typing import Callable, Optional

from qgis.core import QgsTask

from ..processing import cable_lay_parsers as clp

EditWork = Callable[[object, object], object]  # (private layer, feedback) -> result


def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


_CAN_CANCEL = _task_flag("CanCancel")


def _open_private_layer(gpkg_path: str, layer_name: str):
    layer = clp.open_gpkg_layer(gpkg_path, layer_name)
    if layer is None:
        raise RuntimeError(f"Could not open '{layer_name}' in {gpkg_path}.")
    return layer


def run_edit_sync(gpkg_path: str, layer_name: str, work: EditWork, feedback=None):
    """Run ``work`` against a private layer on the calling thread."""
    layer = _open_private_layer(gpkg_path, layer_name)
    return work(layer, feedback)


class ManageEditTask(QgsTask):
    """Runs one management edit on a worker thread against a private layer.

    ``result`` holds whatever ``work`` returned when the task completed;
    ``error`` holds the message when it terminated. The task object itself is
    passed to ``work`` as the feedback (``isCanceled`` / ``setProgress``).
    """

    def __init__(self, description: str, gpkg_path: str, layer_name: str, work: EditWork):
        super().__init__(description, _CAN_CANCEL)
        self._gpkg_path = gpkg_path
        self._layer_name = layer_name
        self._work = work
        self.result = None
        self.error: Optional[str] = None

    def run(self) -> bool:  # worker thread
        try:
            layer = _open_private_layer(self._gpkg_path, self._layer_name)
            self.result = self._work(layer, self)
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            return False
        if self.isCanceled():
            self.error = "Cancelled."
            return False
        self.setProgress(100.0)
        return True
