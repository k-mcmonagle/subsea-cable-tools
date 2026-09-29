# -*- coding: utf-8 -*-
"""Checks for the Burial Planner GUI-stall watchdog (needs Qt from QGIS).

Arm / disarm markers in the log, interval clamping, a real stall producing
a thread dump, and graceful failure when the log cannot be opened. Each
check stops its watchdog: ``faulthandler.dump_traceback_later`` is
process-global.
"""

from __future__ import annotations

import os
import tempfile
import time

from ..burial import watchdog


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def test_arm_disarm_and_interval_clamp() -> bool:
    log = os.path.join(tempfile.mkdtemp(prefix="bp_watchdog_"), "stall.log")
    dog = watchdog.StallWatchdog(threshold_s=4.0, interval_s=30.0,
                                 log_path=log)
    try:
        # The re-arm interval never exceeds half the threshold (or a
        # healthy GUI would trip it) and never drops below 0.25 s.
        ok = dog.interval_s == 2.0
        ok = ok and watchdog.StallWatchdog(threshold_s=0.1).threshold_s == 1.0
        ok = ok and watchdog.StallWatchdog(interval_s=0.01).interval_s == 0.25
        ok = ok and not dog.active and dog.start() and dog.active
        ok = ok and dog.start()  # idempotent while armed
        ok = ok and dog._timer.isActive()
    finally:
        dog.stop()
    text = _read(log)
    ok = ok and not dog.active and not dog._timer.isActive()
    ok = ok and text.count("watchdog armed") == 1 and "disarmed" in text
    dog.stop()  # a second stop is harmless
    return _result("watchdog arms once, disarms, clamps its interval", ok)


def test_stall_writes_thread_dump() -> bool:
    log = os.path.join(tempfile.mkdtemp(prefix="bp_watchdog_"), "stall.log")
    dog = watchdog.StallWatchdog(threshold_s=1.0, log_path=log)
    try:
        ok = dog.start()
        time.sleep(1.6)  # the GUI thread services no timer meanwhile
    finally:
        dog.stop()
    text = _read(log)
    ok = ok and "most recent call first" in text
    ok = ok and "test_stall_writes_thread_dump" in text
    return _result("a GUI-thread stall writes every thread's stack", ok)


def test_unwritable_log_fails_gracefully() -> bool:
    folder = tempfile.mkdtemp(prefix="bp_watchdog_")
    dog = watchdog.StallWatchdog(log_path=folder)  # a directory: cannot open
    ok = not dog.start() and not dog.active and bool(dog.error)
    ok = ok and not dog._timer.isActive()
    dog.stop()
    dog._rearm()  # never arms without a log file
    ok = ok and not dog.active
    default = watchdog.default_log_path()
    ok = ok and os.path.basename(default) == watchdog.LOG_NAME
    return _result("unwritable log: start() reports failure, no timer", ok,
                   dog.error[:60])


def run_all() -> list:
    return [
        test_arm_disarm_and_interval_clamp(),
        test_stall_writes_thread_dump(),
        test_unwritable_log_fails_gracefully(),
    ]


if __name__ == "__main__":  # pragma: no cover
    run_all()
