# -*- coding: utf-8 -*-
"""Files from a newer plugin version (no QGIS needed).

* ``schema_guard``: a higher ``schema_version`` is refused, an equal or
  lower one opened, an unstamped file opened, an unreadable stamp refused;
  ``min_compatible_version`` lets a newer file stay open to older versions.
* The comment register and the burial data file refuse a newer file
  before writing anything (no tables or columns added, stamp untouched);
  an unreadable stamp is a plain store error, not a Python error.
* A Burial Planner rollback over a change that touched a table this
  version does not know is refused instead of half done.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

from .. import schema_guard
from ..burial import change_log
from ..burial.acquired.store import AcquiredStore, AcquiredStoreError
from ..route_comments import store as rc_store
from ..route_comments.store import CommentStore, CommentStoreError

REQUIRES_QGIS = False


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def test_messages() -> bool:
    message = schema_guard.newer_version_message
    checks = {
        "newer refused": "newer version" in message({"schema_version": "14"}, 13, "The plan file p.gpkg"),
        "names the file and both versions": all(part in message({"schema_version": "14"}, 13, "The plan file p.gpkg")
                                                for part in ("The plan file p.gpkg", "schema 14", "schema 13")),
        "same version opens": message({"schema_version": "13"}, 13, "x") == "",
        "older version opens": message({"schema_version": "8"}, 13, "x") == "",
        "unstamped opens": message({}, 13, "x") == "" and message({"schema_version": ""}, 13, "x") == "",
        "unreadable stamp refused": message({"schema_version": "abc"}, 13, "x") != "",
        "integer stamp with spaces": message({"schema_version": " 13 "}, 13, "x") == "",
        "min_compatible lets older open": message({"schema_version": "15", schema_guard.MIN_COMPATIBLE_KEY: "13"},
                                                  13, "x") == "",
        "min_compatible above refused": message({"schema_version": "15", schema_guard.MIN_COMPATIBLE_KEY: "14"},
                                                13, "x") != "",
    }
    try:
        schema_guard.check({"schema_version": "2"}, 1, "x")
        checks["check raises"] = False
    except schema_guard.NewerVersionError:
        checks["check raises"] = True
    failed = [k for k, v in checks.items() if not v]
    return _result("schema_guard: newer files refused, others opened", not failed, ", ".join(failed))


def _meta(path: str, table: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute(f"SELECT key, value FROM {table}").fetchall())
    finally:
        conn.close()


def _set_meta(path: str, table: str, key: str, value: str) -> None:
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
            conn.execute(f"INSERT INTO {table} (key, value) VALUES (?, ?)", (key, value))
    finally:
        conn.close()


def test_comment_register_from_a_newer_version() -> bool:
    folder = tempfile.mkdtemp(prefix="rc_newer_")
    path = os.path.join(folder, "register.gpkg")
    CommentStore.create(path).close()
    # A newer version's register, without a column this version would add.
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("ALTER TABLE rc_attachment DROP COLUMN caption")
    conn.close()
    _set_meta(path, "rc_meta", "schema_version", "2")
    checks = {}
    try:
        CommentStore(path).open()
        checks["refused"] = False
    except CommentStoreError as exc:
        checks["refused"] = "newer version" in str(exc)
    finally:
        rc_store.close_connection()
    conn = sqlite3.connect(path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(rc_attachment)")]
    conn.close()
    checks["nothing added"] = "caption" not in columns
    checks["stamp kept"] = _meta(path, "rc_meta").get("schema_version") == "2"
    _set_meta(path, "rc_meta", schema_guard.MIN_COMPATIBLE_KEY, "1")
    try:
        CommentStore(path).open()
        checks["opens when the newer version allows it"] = True
    except CommentStoreError:
        checks["opens when the newer version allows it"] = False
    finally:
        rc_store.close_connection()
    checks["stamp never lowered"] = _meta(path, "rc_meta").get("schema_version") == "2"
    failed = [k for k, v in checks.items() if not v]
    return _result("comment register from a newer version: refused untouched", not failed, ", ".join(failed))


def test_burial_data_file_stamps() -> bool:
    folder = tempfile.mkdtemp(prefix="acq_newer_")
    path = os.path.join(folder, "data.gpkg")
    AcquiredStore.create(path).close()
    checks = {}
    for stamp, label in (("2", "newer"), ("two", "unreadable")):
        _set_meta(path, "ba_meta", "schema_version", stamp)
        try:
            AcquiredStore(path).open().close()
            checks[label] = False
        except AcquiredStoreError as exc:
            checks[label] = "newer version" in str(exc)
        except Exception as exc:  # e.g. ValueError from int(): a Python error in the dock
            checks[label] = False
            print(f"  {label}: {exc!r}")
        checks[label + " stamp kept"] = _meta(path, "ba_meta").get("schema_version") == stamp
    failed = [k for k, v in checks.items() if not v]
    return _result("burial data file: newer or unreadable stamp is a plain store error", not failed,
                   ", ".join(failed))


def test_rollback_over_unknown_table() -> bool:
    plan = "p1"
    known = change_log.make_entry(plan, 0, change_log.ACTION_EDIT_PLAN, plan,
                                  before={"bp_plan": [{"plan_id": plan, "name": "a"}]},
                                  after={"bp_plan": [{"plan_id": plan, "name": "b"}]})
    rollback = change_log.make_entry(plan, 1, change_log.ACTION_ROLLBACK, known["change_id"],
                                     after={"undone_change_ids": [known["change_id"]]})
    newer = change_log.make_entry(plan, 2, "future_action", plan,
                                  before={"bp_plan": [{"plan_id": plan, "name": "b"}], "bp_future": [{"id": "x"}]},
                                  after={"bp_plan": [{"plan_id": plan, "name": "c"}], "bp_future": [{"id": "y"}]})
    for entry in (known, rollback, newer):
        for side in ("before_json", "after_json"):
            json.loads(entry.get(side) or "{}")   # payloads are JSON text
    checks = {}
    ops, undone = change_log.rollback_operations([known, rollback], known["change_id"])
    checks["known tables and bookkeeping roll back"] = len(undone) == 2 and ops
    try:
        change_log.rollback_operations([known, rollback, newer], known["change_id"])
        checks["unknown table refused"] = False
    except ValueError as exc:
        checks["unknown table refused"] = "bp_future" in str(exc)
    failed = [k for k, v in checks.items() if not v]
    return _result("rollback refuses a change that touched a newer version's table", not failed,
                   ", ".join(failed))


def run_all():
    return [test_messages(), test_comment_register_from_a_newer_version(), test_burial_data_file_stamps(),
            test_rollback_over_unknown_table()]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_pure_tests.py.")
