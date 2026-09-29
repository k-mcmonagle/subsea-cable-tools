# -*- coding: utf-8 -*-
"""Standalone checks for the shared plugin logging helper."""

import logging

from .. import plugin_log


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capturing(fn):
    handler = _Capture()
    logger = logging.getLogger("subsea_cable_tools")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        fn()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        plugin_log.set_debug_enabled(None)
    return handler.records


def test_levels_reach_python_logging():
    def body():
        plugin_log.log_info("info msg")
        plugin_log.log_warning("warn msg")
        plugin_log.log_error("error msg")
    records = _capturing(body)
    got = [(r.levelno, r.getMessage()) for r in records]
    ok = got == [(logging.INFO, "info msg"), (logging.WARNING, "warn msg"),
                 (logging.ERROR, "error msg")]
    return _result("levels reach the python logger", ok, str(got))


def test_debug_is_gated():
    def body():
        plugin_log.set_debug_enabled(False)
        plugin_log.log_debug("hidden")
        try:
            raise ValueError("quiet")
        except ValueError:
            plugin_log.log_exception("hidden too", level=logging.DEBUG)
        plugin_log.set_debug_enabled(True)
        plugin_log.log_debug("shown")
    records = _capturing(body)
    got = [r.getMessage() for r in records]
    return _result("debug messages only when enabled", got == ["shown"], str(got))


def test_exception_includes_traceback():
    def body():
        try:
            raise ValueError("boom")
        except ValueError:
            plugin_log.log_exception("sampling failed")
    records = _capturing(body)
    msg = records[0].getMessage() if records else ""
    ok = (len(records) == 1 and records[0].levelno == logging.WARNING
          and msg.startswith("sampling failed\n") and "ValueError: boom" in msg)
    return _result("log_exception appends the traceback", ok)


def test_exception_outside_handler_has_no_stub():
    records = _capturing(lambda: plugin_log.log_exception("no active error"))
    msg = records[0].getMessage() if records else ""
    return _result("log_exception outside except adds no traceback",
                   msg == "no active error", repr(msg))


def run_all():
    return [test_levels_reach_python_logging(),
            test_debug_is_gated(),
            test_exception_includes_traceback(),
            test_exception_outside_handler_has_no_stub()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
