# -*- coding: utf-8 -*-
"""Direct sqlite3 access to the Burial Planner's geometryless registry tables.

A GeoPackage is a SQLite database; the registry tables (``bp_plan``,
``bp_event``, …) carry no geometry, so they can be read and written with
plain SQL instead of the QGIS vector-file writer. The writer path — used
for table creation, migrations and the spatial plan layers — drops and
rewrites a whole table per call (DDL + full re-insert + fsync); this module
replaces that per-edit cost with targeted row operations inside real
transactions.

Pure stdlib (no QGIS imports) so the behaviour is unit-testable headlessly.
Connections are cached per file with ``journal_mode=WAL`` and
``synchronous=NORMAL``; ``checkpoint(truncate=True)`` folds the WAL back
into the main file before backups/copies so a copied ``.gpkg`` is complete
without its sidecar files.

Thread contract: main thread only (sqlite3's default same-thread check is
left enabled as a guard). Background tasks never touch the store.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Dict, List, Optional, Sequence, Tuple

_connections: Dict[str, sqlite3.Connection] = {}


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def quote_ident(name: str) -> str:
    """Quote a SQL identifier (table/column names come from our schema)."""
    return '"' + str(name).replace('"', '""') + '"'


def connect(path: str) -> sqlite3.Connection:
    """Cached connection to a GeoPackage, WAL-configured. Raises on failure."""
    key = _key(path)
    conn = _connections.get(key)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            _connections.pop(key, None)
    if not os.path.exists(path):
        raise sqlite3.OperationalError(f"No such file: {path}")
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL removes the per-write rollback-journal create/fsync/delete cycle
    # (the dominant cost of small writes on Windows); NORMAL is durable for
    # application data at WAL checkpoints.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _connections[key] = conn
    return conn


def checkpoint(path: str, truncate: bool = False) -> bool:
    """Fold the WAL into the main database file (best effort).

    Returns True when the WAL is known to be fully folded (or there is no
    cached connection, so no WAL of ours to fold); False when a concurrent
    reader blocked the checkpoint and frames remain in the ``-wal`` sidecar
    — a plain file copy of the main database would then miss those frames.
    """
    conn = _connections.get(_key(path))
    if conn is None:
        return True
    mode = "TRUNCATE" if truncate else "PASSIVE"
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    except sqlite3.Error:
        return False
    if row is None:
        return False
    try:
        busy, log_frames, moved = int(row[0]), int(row[1]), int(row[2])
    except (TypeError, ValueError, IndexError):
        return False
    return busy == 0 and log_frames == moved


def backup_to(path: str, target: str) -> bool:
    """Consistent snapshot via the SQLite backup API (WAL-safe).

    Used when a checkpoint could not fully fold the WAL: the backup API
    reads through the live connection, so the copy includes every
    committed transaction regardless of sidecar state.
    """
    conn = _connections.get(_key(path))
    if conn is None:
        return False
    try:
        dest = sqlite3.connect(target)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        return True
    except sqlite3.Error:
        try:
            os.remove(target)  # never leave a half-written backup behind
        except OSError:
            pass
        return False


def close(path: Optional[str] = None) -> None:
    """Checkpoint and close one cached connection (or all of them)."""
    keys = [_key(path)] if path else list(_connections)
    for key in keys:
        conn = _connections.pop(key, None)
        if conn is None:
            continue
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            conn.close()
        except sqlite3.Error:
            pass


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name = ?", (table,)).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """Column names of ``table`` excluding the OGR ``fid`` primary key."""
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    return [str(r["name"]) for r in rows if str(r["name"]).lower() != "fid"]


def _coerce(value):
    """Python value → sqlite parameter (bools become ints; rest pass)."""
    if isinstance(value, bool):
        return int(value)
    return value


@contextmanager
def transaction(conn: sqlite3.Connection):
    """One write transaction; nests by joining the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def read_rows(conn: sqlite3.Connection, table: str,
              where: str = "", params: Sequence = ()) -> List[Dict]:
    columns = table_columns(conn, table)
    if not columns:
        return []
    sql = "SELECT " + ", ".join(quote_ident(c) for c in columns) \
          + " FROM " + quote_ident(table)
    if where:
        sql += " WHERE " + where
    return [dict(zip(columns, row))
            for row in conn.execute(sql, tuple(params)).fetchall()]


def insert_rows(conn: sqlite3.Connection, table: str,
                rows: Sequence[Dict]) -> None:
    """Insert rows (columns from the live table; missing keys become NULL)."""
    if not rows:
        return
    columns = table_columns(conn, table)
    sql = ("INSERT INTO " + quote_ident(table) + " ("
           + ", ".join(quote_ident(c) for c in columns) + ") VALUES ("
           + ", ".join("?" for _c in columns) + ")")
    values = [tuple(_coerce(row.get(c)) for c in columns) for row in rows]
    with transaction(conn):
        conn.executemany(sql, values)


def delete_where(conn: sqlite3.Connection, table: str,
                 where: str, params: Sequence = ()) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM " + quote_ident(table)
                     + " WHERE " + where, tuple(params))


def delete_keys(conn: sqlite3.Connection, table: str, key_column: str,
                keys: Sequence[str]) -> None:
    keys = [str(k) for k in keys]
    if not keys:
        return
    with transaction(conn):
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            conn.execute(
                "DELETE FROM " + quote_ident(table) + " WHERE "
                + quote_ident(key_column) + " IN ("
                + ", ".join("?" for _k in chunk) + ")", tuple(chunk))


def upsert_rows(conn: sqlite3.Connection, table: str, key_column: str,
                rows: Sequence[Dict]) -> None:
    """Replace rows by key: delete existing keys, insert the new rows."""
    rows = list(rows)
    if not rows:
        return
    with transaction(conn):
        delete_keys(conn, table, key_column,
                    [str(r.get(key_column)) for r in rows])
        insert_rows(conn, table, rows)


def replace_where(conn: sqlite3.Connection, table: str,
                  where: str, params: Sequence,
                  rows: Sequence[Dict]) -> None:
    """Atomically replace every row matching ``where`` with ``rows``."""
    with transaction(conn):
        conn.execute("DELETE FROM " + quote_ident(table)
                     + " WHERE " + where, tuple(params))
        insert_rows(conn, table, rows)
