"""SQLite diagnostics and checked snapshots; no QGIS or source-file writes.

These checks establish SQLite structural integrity, not which competing copy
contains the user's intended edits or whether all GeoPackage features are valid.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import os
import sqlite3
import time
from urllib.parse import quote


def is_corruption(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", 0) or 0
    return (code & 255) in (11, 26) or any(text in str(exc).lower() for text in (
        "database disk image is malformed", "file is not a database",
        "database corruption", "malformed database schema"))


def readonly_connection(path: str) -> sqlite3.Connection:
    full = os.path.abspath(path).replace("\\", "/")
    if full.startswith("//"):
        full = "//" + full
    elif not full.startswith("/"):
        full = "/" + full
    return sqlite3.connect("file:" + quote(full, safe="/:") + "?mode=ro",
                           uri=True, timeout=2.0)


def _integrity(conn: sqlite3.Connection) -> None:
    messages = [str(row[0]) for row in conn.execute("PRAGMA integrity_check(20)")]
    if messages != ["ok"]:
        raise sqlite3.DatabaseError("SQLite integrity check failed:\n" + "\n".join(messages))


def checked_snapshot(source: str, target: str, *, rebuild_indexes: bool = False,
                     timeout: float = 60.0) -> str:
    """Create a new, validated SQLite snapshot, including committed WAL data.

    Never overwrite an existing path. An index rebuild operates only on the new
    copy. No unreadable tables/rows are skipped; unsuccessful output is removed.
    A separate read-only connection also works when QGIS owns all live handles.
    """
    if os.path.realpath(source) == os.path.realpath(target):
        raise ValueError("Choose a new file; the source must be preserved.")
    deadline = time.monotonic() + timeout

    def progress(*_args):
        if time.monotonic() > deadline:
            raise TimeoutError("The snapshot timed out. Close other editors and try again.")

    # Exclusive creation makes ownership unambiguous, including on failure.
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(readonly_connection(source)) as src, closing(sqlite3.connect(target)) as dest:
            src.backup(dest, pages=256, progress=progress, sleep=0.05)
            dest.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            # Standalone file for hand-off: do not leave a WAL dependency.
            dest.execute("PRAGMA journal_mode=DELETE")
            if rebuild_indexes:
                dest.execute("REINDEX")
                dest.commit()
            _integrity(dest)
        return target
    except BaseException:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(target + suffix)
            except OSError:
                pass
        raise


def migration_snapshot(path: str, label: str) -> str:
    """Keep every previous backup, including backups a user may have opened."""
    stem, ext = os.path.splitext(path)
    base = f"{stem}.{label}.bak"
    for number in range(10000):
        target = base + (f".{number}" if number else "") + ext
        try:
            return checked_snapshot(path, target)
        except FileExistsError:
            continue
    raise OSError("Too many backup files; choose another location.")


@dataclass
class FileHealth:
    path: str
    healthy: bool = False
    error: str = ""
    schema_version: str = ""
    plans: list = field(default_factory=list)
    latest_change: str = ""
    counts: dict = field(default_factory=dict)
    saved_times: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def inspect_file(path: str, timeout: float = 60.0) -> FileHealth:
    """Inspect independently; keep partial readable details clearly qualified."""
    report = FileHealth(path)
    deadline = time.monotonic() + timeout
    try:
        with closing(readonly_connection(path)) as conn:
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            conn.execute("BEGIN")  # all metadata and integrity from one read snapshot
            try:
                _integrity(conn)
                report.healthy = True
            except sqlite3.Error as exc:
                report.error = str(exc)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            def query(sql):
                try:
                    return conn.execute(sql).fetchall()
                except sqlite3.Error as exc:
                    report.notes.append(str(exc))
                    return []

            if "bp_meta" in tables:
                rows = query("SELECT value FROM bp_meta WHERE key='schema_version'")
                report.schema_version = str(rows[0][0]) if rows else ""
            if "bp_plan" in tables:
                report.plans = query("SELECT name, modified_utc FROM bp_plan ORDER BY name")
            else:
                report.notes.append("No Burial Planner registry found.")
            if "bp_change_log" in tables:
                rows = query("SELECT MAX(utc) FROM bp_change_log")
                report.latest_change = str(rows[0][0] or "") if rows else ""
            for table in ("bp_event", "bp_section", "bp_change_log", "bp_cpt", "bp_cpt_data",
                          "bp_ground_profile", "bp_ground_dataset"):
                if table in tables:
                    rows = query(f'SELECT COUNT(*) FROM "{table}"')
                    if rows:
                        report.counts[table] = rows[0][0]
            for table, column in (("bp_analysis", "modified_utc"), ("bp_generation", "run_utc"),
                                  ("bp_ground_dataset", "updated_utc"), ("bp_cpt_campaign", "imported_utc")):
                if table in tables:
                    columns = {r[1] for r in query(f'PRAGMA table_info("{table}")')}
                    if column in columns:
                        rows = query(f'SELECT MAX("{column}") FROM "{table}"')
                        if rows and rows[0][0]:
                            report.saved_times[table] = str(rows[0][0])
    except (sqlite3.Error, OSError) as exc:
        report.healthy = False
        report.error = str(exc)
    return report
