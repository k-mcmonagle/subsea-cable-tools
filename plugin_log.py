# -*- coding: utf-8 -*-
"""One logging entry point for the whole plugin.

Messages go to the QGIS *Log Messages* panel under the "Subsea Cable Tools"
tab, and always to the standard ``logging`` logger ``subsea_cable_tools`` so
headless runs and tests see them too. ``QgsMessageLog.logMessage`` is thread
safe, so these helpers may be called from QgsTask / QThread workers.

Use them instead of ``except Exception: pass`` wherever a failure could change
a result, lose data or hide a feature::

    from .plugin_log import log_exception

    try:
        ...
    except Exception:
        log_exception("Depth profile: contour sampling failed")

``log_debug`` is silent unless debug logging is switched on (QGIS setting
``SubseaCableTools/debug_logging`` = true, or environment variable
``SUBSEA_CABLE_TOOLS_DEBUG=1``); use it for expected, recoverable failures in
UI guards and teardown code where a message every time would be noise.
"""

from __future__ import annotations

import logging
import os
import traceback

TAG = "Subsea Cable Tools"

_logger = logging.getLogger("subsea_cable_tools")
# Library convention: no stderr output via logging's last-resort handler
# unless the host (or a test) configures logging itself.
_logger.addHandler(logging.NullHandler())

try:  # pragma: no cover - exercised inside QGIS
    from qgis.core import Qgis, QgsMessageLog

    def _level(name):
        scope = getattr(Qgis, "MessageLevel", Qgis)
        return getattr(scope, name)

    _QGIS_LEVELS = {
        logging.DEBUG: _level("Info"),
        logging.INFO: _level("Info"),
        logging.WARNING: _level("Warning"),
        logging.ERROR: _level("Critical"),
    }
except ImportError:  # headless / pure tests
    QgsMessageLog = None
    _QGIS_LEVELS = {}

_debug_enabled = None


def debug_enabled() -> bool:
    """True when debug messages should be written (cached per session)."""
    global _debug_enabled
    if _debug_enabled is None:
        enabled = os.environ.get("SUBSEA_CABLE_TOOLS_DEBUG", "") not in ("", "0")
        if not enabled:
            try:
                from qgis.core import QgsSettings

                value = QgsSettings().value("SubseaCableTools/debug_logging", False)
                enabled = str(value).lower() in ("1", "true", "yes")
            except Exception:  # noqa: BLE001 - no QGIS / settings unavailable
                enabled = False
        _debug_enabled = enabled
    return _debug_enabled


def set_debug_enabled(enabled) -> None:
    """Override the debug switch for this session (``None`` re-reads it)."""
    global _debug_enabled
    _debug_enabled = None if enabled is None else bool(enabled)


def _emit(level: int, message: str) -> None:
    _logger.log(level, message)
    if QgsMessageLog is not None:
        try:
            QgsMessageLog.logMessage(message, TAG, _QGIS_LEVELS[level])
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def log_debug(message: str) -> None:
    if debug_enabled():
        _emit(logging.DEBUG, message)


def log_info(message: str) -> None:
    _emit(logging.INFO, message)


def log_warning(message: str) -> None:
    _emit(logging.WARNING, message)


def log_error(message: str) -> None:
    _emit(logging.ERROR, message)


def log_exception(message: str, level: int = logging.WARNING) -> None:
    """Log *message* plus the traceback of the exception being handled.

    Call from inside an ``except`` block. ``level=logging.DEBUG`` makes it a
    debug-only message (see module docstring).
    """
    if level == logging.DEBUG and not debug_enabled():
        return
    detail = traceback.format_exc()
    if detail and detail.strip() != "NoneType: None":
        message = f"{message}\n{detail.rstrip()}"
    _emit(level, message)
