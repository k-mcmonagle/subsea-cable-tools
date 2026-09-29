# -*- coding: utf-8 -*-
"""Checks for the Burial Planner's direct-SQL registry fast path.

Pure python + sqlite3 — no QGIS. Builds registry-shaped tables (fid primary
key + typed columns, the layout the QGIS GPKG writer produces) and verifies
targeted reads/writes, transactional atomicity and WAL checkpointing.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from ..burial import gpkg_sql


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE bp_event (fid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT, plan_id TEXT, seq INTEGER, kp REAL, notes TEXT)")
    conn.execute(
        "CREATE TABLE bp_section (fid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "section_id TEXT, plan_id TEXT, start_kp REAL, end_kp REAL)")
    conn.commit()
    conn.close()


def _fresh_db():
    handle, path = tempfile.mkstemp(suffix=".gpkg")
    os.close(handle)
    os.remove(path)
    _make_db(path)
    return path


def _cleanup(path: str) -> None:
    gpkg_sql.close(path)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def test_read_write_filtered() -> bool:
    path = _fresh_db()
    try:
        conn = gpkg_sql.connect(path)
        gpkg_sql.insert_rows(conn, "bp_event", [
            {"event_id": "e1", "plan_id": "p1", "seq": 0, "kp": 1.5,
             "notes": "one"},
            {"event_id": "e2", "plan_id": "p2", "seq": 0, "kp": 2.5,
             "notes": None},
        ])
        p1 = gpkg_sql.read_rows(conn, "bp_event", "plan_id = ?", ("p1",))
        all_rows = gpkg_sql.read_rows(conn, "bp_event")
        ok = (len(p1) == 1 and p1[0]["event_id"] == "e1"
              and p1[0]["kp"] == 1.5 and len(all_rows) == 2
              and all_rows[1]["notes"] is None
              and "fid" not in all_rows[0])
        return _result("filtered SQL reads + typed round trip", ok)
    finally:
        _cleanup(path)


def test_replace_where_scoped_to_plan() -> bool:
    path = _fresh_db()
    try:
        conn = gpkg_sql.connect(path)
        gpkg_sql.insert_rows(conn, "bp_event", [
            {"event_id": "a", "plan_id": "p1", "seq": 0, "kp": 1.0},
            {"event_id": "b", "plan_id": "p2", "seq": 0, "kp": 2.0},
        ])
        gpkg_sql.replace_where(conn, "bp_event", "plan_id = ?", ("p1",), [
            {"event_id": "c", "plan_id": "p1", "seq": 0, "kp": 3.0},
            {"event_id": "d", "plan_id": "p1", "seq": 1, "kp": 4.0},
        ])
        ids = sorted(r["event_id"]
                     for r in gpkg_sql.read_rows(conn, "bp_event"))
        ok = ids == ["b", "c", "d"]
        return _result("replace_where touches only the target plan", ok,
                       str(ids))
    finally:
        _cleanup(path)


def test_upsert_and_delete_keys() -> bool:
    path = _fresh_db()
    try:
        conn = gpkg_sql.connect(path)
        gpkg_sql.insert_rows(conn, "bp_section", [
            {"section_id": "s1", "plan_id": "p1", "start_kp": 0, "end_kp": 1},
            {"section_id": "s2", "plan_id": "p1", "start_kp": 1, "end_kp": 2},
        ])
        gpkg_sql.upsert_rows(conn, "bp_section", "section_id", [
            {"section_id": "s2", "plan_id": "p1", "start_kp": 1.5,
             "end_kp": 2.0},
            {"section_id": "s3", "plan_id": "p1", "start_kp": 2, "end_kp": 3},
        ])
        rows = {r["section_id"]: r
                for r in gpkg_sql.read_rows(conn, "bp_section")}
        ok = (set(rows) == {"s1", "s2", "s3"}
              and rows["s2"]["start_kp"] == 1.5)
        gpkg_sql.delete_keys(conn, "bp_section", "section_id", ["s1", "s3"])
        ok = ok and {r["section_id"] for r in gpkg_sql.read_rows(
            conn, "bp_section")} == {"s2"}
        return _result("upsert replaces by key; delete_keys removes", ok)
    finally:
        _cleanup(path)


def test_transaction_atomicity() -> bool:
    path = _fresh_db()
    try:
        conn = gpkg_sql.connect(path)
        gpkg_sql.insert_rows(conn, "bp_event", [
            {"event_id": "keep", "plan_id": "p1", "seq": 0, "kp": 0.0}])
        try:
            with gpkg_sql.transaction(conn):
                gpkg_sql.replace_where(conn, "bp_event", "plan_id = ?",
                                       ("p1",), [
                    {"event_id": "new", "plan_id": "p1", "seq": 0, "kp": 9.9}])
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        rows = gpkg_sql.read_rows(conn, "bp_event")
        ok = len(rows) == 1 and rows[0]["event_id"] == "keep"
        # Nested transactions join the outer one (single commit).
        with gpkg_sql.transaction(conn):
            with gpkg_sql.transaction(conn):
                gpkg_sql.insert_rows(conn, "bp_event", [
                    {"event_id": "n2", "plan_id": "p1", "seq": 1, "kp": 1.0}])
            ok = ok and conn.in_transaction  # still open until outer exits
        ok = ok and not conn.in_transaction
        ok = ok and len(gpkg_sql.read_rows(conn, "bp_event")) == 2
        return _result("failed transaction rolls back; nesting joins", ok)
    finally:
        _cleanup(path)


def test_wal_checkpoint_folds_sidecar() -> bool:
    path = _fresh_db()
    try:
        conn = gpkg_sql.connect(path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        gpkg_sql.insert_rows(conn, "bp_event", [
            {"event_id": "e", "plan_id": "p", "seq": 0, "kp": 0.0}])
        gpkg_sql.checkpoint(path, truncate=True)
        wal_empty = (not os.path.exists(path + "-wal")
                     or os.path.getsize(path + "-wal") == 0)
        # An independent reader of just the main file sees the row.
        other = sqlite3.connect(path)
        count = other.execute("SELECT COUNT(*) FROM bp_event").fetchone()[0]
        other.close()
        ok = str(mode).lower() == "wal" and wal_empty and count == 1
        return _result("WAL mode + truncate checkpoint folds the sidecar",
                       ok, f"mode={mode}")
    finally:
        _cleanup(path)


def test_missing_column_becomes_null_extra_key_ignored() -> bool:
    path = _fresh_db()
    try:
        conn = gpkg_sql.connect(path)
        gpkg_sql.insert_rows(conn, "bp_event", [
            {"event_id": "e", "plan_id": "p", "unknown_key": "dropped"}])
        row = gpkg_sql.read_rows(conn, "bp_event")[0]
        ok = row["kp"] is None and "unknown_key" not in row
        return _result("missing columns NULL; unknown row keys ignored", ok)
    finally:
        _cleanup(path)


def _journal_mode(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def _sidecars(path: str) -> list:
    return [s for s in ("-wal", "-shm", "-journal")
            if os.path.exists(path + s)]


def test_readonly_probe_leaves_plain_file_untouched() -> bool:
    """has_table() on an ordinary database: no WAL switch, no sidecars, no
    cached connection and no handle left open (Windows file lock)."""
    folder = tempfile.mkdtemp(prefix="gpkg_probe_")
    path = os.path.join(folder, "plain #1 (copy).gpkg")  # URI-hostile name
    try:
        _make_db(path)
        ok = gpkg_sql.has_table(path, "bp_event")
        ok = ok and not gpkg_sql.has_table(path, "bp_meta")
        ok = ok and not gpkg_sql.has_table(os.path.join(folder, "none.gpkg"),
                                           "bp_meta")
        mode = _journal_mode(path)
        ok = ok and mode == "delete" and not _sidecars(path)
        ok = ok and gpkg_sql._key(path) not in gpkg_sql._connections
        moved = path + ".moved"
        os.rename(path, moved)  # fails on Windows while a handle is open
        os.rename(moved, path)
        return _result("read-only probe leaves a plain file untouched", ok,
                       f"mode={mode} sidecars={_sidecars(path)}")
    finally:
        _cleanup(path)


def test_probe_uses_cached_connection() -> bool:
    path = _fresh_db()
    try:
        conn = gpkg_sql.connect(path)
        with gpkg_sql.transaction(conn):
            conn.execute(
                "CREATE TABLE bp_meta (fid INTEGER PRIMARY KEY, key TEXT)")
            # Visible through the cached connection mid-transaction.
            ok = gpkg_sql.has_table(path, "bp_meta")
        return _result("probe answers through an open registry connection",
                       ok)
    finally:
        _cleanup(path)


def test_network_path_detection() -> bool:
    if os.name != "nt":
        return _result("UNC / long-path forms classified", True,
                       "Windows-only path forms: skipped")
    cases = {
        "\\\\server\\share\\plans.gpkg": True,
        "//server/share/plans.gpkg": True,
        "\\\\?\\UNC\\server\\share\\plans.gpkg": True,
        "\\\\?\\C:\\data\\plans.gpkg": False,
        os.path.join(tempfile.gettempdir(), "plans.gpkg"): False,
    }
    got = {case: gpkg_sql.is_network_path(case) for case in cases}
    ok = got == cases
    uri = gpkg_sql._readonly_uri("\\\\server\\share\\a b.gpkg")
    ok = ok and uri == "file:////server/share/a%20b.gpkg?mode=ro"
    return _result("UNC / long-path forms classified; UNC read-only URI", ok,
                   str({k: v for k, v in got.items() if cases[k] != v}))


def test_network_share_uses_rollback_journal() -> bool:
    """Files on a share never get WAL; one made WAL locally is switched
    back when it is reopened there."""
    path = _fresh_db()
    original = gpkg_sql.is_network_path
    try:
        gpkg_sql.connect(path)  # local: WAL
        gpkg_sql.close(path)
        was = _journal_mode(path)
        gpkg_sql.is_network_path = lambda _p: True
        conn = gpkg_sql.connect(path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        gpkg_sql.insert_rows(conn, "bp_event", [
            {"event_id": "e", "plan_id": "p", "seq": 0, "kp": 0.0}])
        ok = was == "wal" and str(mode).lower() == "delete" and int(sync) == 2
        ok = ok and gpkg_sql.checkpoint(path, truncate=True)  # harmless no-op
        ok = ok and len(gpkg_sql.read_rows(conn, "bp_event")) == 1
        # A real UNC path when the admin share is reachable (skipped if not).
        drive, rest = os.path.splitdrive(os.path.abspath(path))
        unc = "\\\\localhost\\" + drive.rstrip(":") + "$" + rest
        gpkg_sql.is_network_path = original
        gpkg_sql.close(path)
        detail = f"was={was} mode={mode} sync={sync}"
        if os.name == "nt" and drive and os.path.exists(unc):
            ok = ok and gpkg_sql.is_network_path(unc)
            ok = ok and gpkg_sql.has_table(unc, "bp_event")
            unc_conn = gpkg_sql.connect(unc)
            unc_mode = unc_conn.execute("PRAGMA journal_mode").fetchone()[0]
            gpkg_sql.close(unc)
            ok = ok and str(unc_mode).lower() == "delete"
            detail += f" unc={unc_mode}"
        else:
            detail += " (UNC admin share unavailable: skipped)"
        return _result("network share keeps the rollback journal", ok, detail)
    finally:
        gpkg_sql.is_network_path = original
        _cleanup(path)


def test_readonly_file_opens_without_wal() -> bool:
    """A read-only registry still opens through SQL: reads work, a write
    fails cleanly instead of pushing the store into non-atomic legacy mode."""
    import stat

    path = _fresh_db()
    try:
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO bp_event (event_id, plan_id) VALUES ('e', 'p')")
        conn.commit()
        conn.close()
        os.chmod(path, stat.S_IREAD)
        conn = gpkg_sql.connect(path)
        ok = len(gpkg_sql.read_rows(conn, "bp_event")) == 1
        try:
            gpkg_sql.insert_rows(conn, "bp_event", [{"event_id": "x"}])
            ok = False
        except sqlite3.OperationalError as exc:
            ok = ok and "readonly" in str(exc).replace(" ", "").lower()
        ok = ok and not conn.in_transaction
        gpkg_sql.close(path)
        ok = ok and _journal_mode(path) == "delete"
        return _result("read-only file opens through SQL without WAL", ok)
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        _cleanup(path)


def test_lock_errors_are_transient() -> bool:
    path = _fresh_db()
    holder = sqlite3.connect(path)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("SELECT COUNT(*) FROM bp_event").fetchone()
            busy = None
        except sqlite3.OperationalError as exc:
            busy = exc
        finally:
            other.close()
        ok = busy is not None and gpkg_sql.is_transient(busy)
        ok = ok and not gpkg_sql.is_transient(
            sqlite3.OperationalError("no such table: bp_meta"))
        ok = ok and not gpkg_sql.is_transient(
            sqlite3.DatabaseError("file is not a database"))
        return _result("lock/busy errors classified as transient", ok,
                       str(busy))
    finally:
        holder.rollback()
        holder.close()
        _cleanup(path)


def run_all() -> list:
    return [
        test_read_write_filtered(),
        test_replace_where_scoped_to_plan(),
        test_upsert_and_delete_keys(),
        test_transaction_atomicity(),
        test_wal_checkpoint_folds_sidecar(),
        test_missing_column_becomes_null_extra_key_ignored(),
        test_readonly_probe_leaves_plain_file_untouched(),
        test_probe_uses_cached_connection(),
        test_network_path_detection(),
        test_network_share_uses_rollback_journal(),
        test_readonly_file_opens_without_wal(),
        test_lock_errors_are_transient(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
