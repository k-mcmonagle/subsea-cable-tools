# -*- coding: utf-8 -*-
"""The comment register: one GeoPackage, plus an attachments folder beside it.

``<name>.gpkg`` holds named comment sets and their comments; images added
to comments are copied into ``<name>_attachments/`` and referenced by a
relative file name, so the register can be moved or shared as the pair.
Several QGIS projects (or sessions) can open the same register.

Tables (written with plain SQL in transactions; no QGIS needed):

* ``rc_meta``        key / value (schema version, created)
* ``rc_set``         comment sets: name, status and priority choices (JSON)
* ``rc_field``       per-set custom fields (text, number, date, yes/no, choice)
* ``rc_comment``     comments; a GeoPackage feature table (geometry ``geom``
                     in EPSG:4326, Point or LineString) that QGIS can open
* ``rc_reply``       chronological replies (``status_to``: the status the
                     reply set, e.g. *Send & close*, ``status_from`` the one
                     it replaced; ``edited_utc`` when its text was edited)
* ``rc_attachment``  image attachments (file in the attachments folder);
                     ``rc_comment.map_image`` names the one shown in the
                     comment's map label

Columns are only ever added, so a register opens in any version of the tool.
A change that older versions could not keep intact would raise
``SCHEMA_VERSION``, and versions that predate it then refuse the register
(:mod:`schema_guard`) instead of writing to it.

Every comment has a stable UUID, created/updated timestamps and a
``version`` that each save increments: a save from an editor opened on an
older version is refused (:class:`ConflictError`) instead of overwriting a
change made in another project or QGIS. Failed writes raise
:class:`CommentStoreError` with a plain explanation; a transaction either
commits completely or not at all.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import struct
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .. import schema_guard
from ..burial import gpkg_sql
from ..workbench.schema import new_id, utc_now_iso
from . import model

SCHEMA_VERSION = 1
SRS_ID = 4326
ATTACHMENTS_SUFFIX = "_attachments"
WINDOWS_MAX_PATH = 259          # longer file paths fail on Windows unless long paths are enabled
IMAGE_EXTENSIONS = model.IMAGE_EXTENSIONS

T_META, T_SET, T_FIELD, T_COMMENT, T_REPLY, T_ATTACH = (
    "rc_meta", "rc_set", "rc_field", "rc_comment", "rc_reply", "rc_attachment")

META_COLUMNS = [("key", "TEXT"), ("value", "TEXT")]
SET_COLUMNS = [("set_id", "TEXT"), ("name", "TEXT"), ("description", "TEXT"), ("statuses_json", "TEXT"),
               ("priorities_json", "TEXT"), ("created_utc", "TEXT"), ("updated_utc", "TEXT")]
FIELD_COLUMNS = [("field_id", "TEXT"), ("set_id", "TEXT"), ("name", "TEXT"), ("kind", "TEXT"),
                 ("choices_json", "TEXT"), ("seq", "INTEGER")]
COMMENT_COLUMNS = [
    ("comment_id", "TEXT"), ("set_id", "TEXT"), ("ref_no", "INTEGER"), ("ext_id", "TEXT"),
    ("text", "TEXT"), ("status", "TEXT"), ("priority", "TEXT"), ("author", "TEXT"),
    ("comment_date", "TEXT"), ("tags", "TEXT"), ("custom_json", "TEXT"),
    # Reference, as captured. KPs are never recomputed against another route.
    ("ref_kind", "TEXT"), ("kp_km", "REAL"), ("kp_end_km", "REAL"), ("dcc_m", "REAL"),
    ("lat", "REAL"), ("lon", "REAL"),
    ("route_name", "TEXT"), ("route_layer_id", "TEXT"), ("route_source", "TEXT"), ("route_provider", "TEXT"),
    ("route_crs", "TEXT"), ("rpl_id", "TEXT"), ("rpl_name", "TEXT"), ("rpl_rev", "TEXT"),
    ("rpl_route_id", "TEXT"), ("rpl_source_file", "TEXT"), ("route_hash", "TEXT"), ("route_length_m", "REAL"),
    ("start_kp_km", "REAL"), ("kp_convention", "TEXT"), ("kp_basis", "TEXT"),
    # The geometry in the CRS it was captured in (exact), besides ``geom`` (EPSG:4326).
    ("geom_crs", "TEXT"), ("geom_wkt", "TEXT"),
    ("created_utc", "TEXT"), ("updated_utc", "TEXT"), ("version", "INTEGER"),
    # The attachment file shown in the comment's map label ('' or NULL: none).
    ("map_image", "TEXT"),
]
REPLY_COLUMNS = [("reply_id", "TEXT"), ("comment_id", "TEXT"), ("author", "TEXT"), ("text", "TEXT"),
                 ("created_utc", "TEXT"), ("status_to", "TEXT"), ("status_from", "TEXT"), ("edited_utc", "TEXT")]
ATTACH_COLUMNS = [("attachment_id", "TEXT"), ("comment_id", "TEXT"), ("file_name", "TEXT"),
                  ("original_name", "TEXT"), ("caption", "TEXT"), ("created_utc", "TEXT")]
TABLES = {T_META: META_COLUMNS, T_SET: SET_COLUMNS, T_FIELD: FIELD_COLUMNS, T_COMMENT: COMMENT_COLUMNS,
          T_REPLY: REPLY_COLUMNS, T_ATTACH: ATTACH_COLUMNS}
COMMENT_NAMES = [name for name, _t in COMMENT_COLUMNS]
# Columns a save may change (identity, numbering and bookkeeping are the store's;
# the map image only through set_map_image, so it always names one of the comment's images).
EDITABLE = set(COMMENT_NAMES) - {"comment_id", "set_id", "ref_no", "created_utc", "updated_utc", "version",
                                 "map_image"}

_Q = gpkg_sql.quote_ident


class CommentStoreError(RuntimeError):
    """A register read or write failed; the message says why in plain words."""


class ConflictError(CommentStoreError):
    """The comment changed (or was deleted) elsewhere since it was opened."""


# --------------------------------------------------------------------------- geometry
def _wkb(kind: str, coords: Sequence[Tuple[float, float]]) -> bytes:
    if kind == "Point":
        x, y = coords[0]
        return struct.pack("<BIdd", 1, 1, float(x), float(y))
    body = struct.pack("<BII", 1, 2, len(coords))
    return body + b"".join(struct.pack("<dd", float(x), float(y)) for x, y in coords)


def parse_wkt(wkt: str) -> Optional[Tuple[str, List[Tuple[float, float]]]]:
    """``("Point" | "LineString", [(x, y), …])`` from 2D WKT (Z/M dropped)."""
    if not wkt:
        return None
    text = str(wkt).strip()
    head, _sep, rest = text.partition("(")
    name = head.strip().upper().replace(" ", "")
    for suffix in ("ZM", "Z", "M"):
        if name.endswith(suffix) and name[: -len(suffix)] in ("POINT", "LINESTRING"):
            name = name[: -len(suffix)]
    if name not in ("POINT", "LINESTRING"):
        return None
    body = rest.rsplit(")", 1)[0]
    coords = []
    for pair in body.split(","):
        parts = pair.split()
        if len(parts) < 2:
            return None
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        coords.append((x, y))
    if not coords or (name == "LINESTRING" and len(coords) < 2):
        return None
    return ("Point" if name == "POINT" else "LineString"), coords


def to_wkt(kind: str, coords: Sequence[Tuple[float, float]]) -> str:
    text = ", ".join(f"{x!r} {y!r}" for x, y in coords)
    return f"POINT({text})" if kind == "Point" else f"LINESTRING({text})"


def gpkg_blob(wkt: str) -> Optional[bytes]:
    """GeoPackage geometry blob (EPSG:4326, little endian, XY envelope)."""
    parsed = parse_wkt(wkt)
    if parsed is None:
        return None
    kind, coords = parsed
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    header = b"GP" + bytes((0, 0b00000011)) + struct.pack("<i", SRS_ID)
    envelope = struct.pack("<dddd", min(xs), max(xs), min(ys), max(ys))
    return header + envelope + _wkb(kind, coords)


def blob_to_wkt(blob) -> str:
    """WKT of a GeoPackage Point/LineString blob ('' when unreadable)."""
    if not blob or len(blob) < 8 or bytes(blob[:2]) != b"GP":
        return ""
    data = bytes(blob)
    flags = data[3]
    env = (flags >> 1) & 0x07
    offset = 8 + {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env, 0)
    if flags & 0x10:  # empty geometry
        return ""
    try:
        order = "<" if data[offset] == 1 else ">"
        gtype = struct.unpack_from(order + "I", data, offset + 1)[0] % 1000
        pos = offset + 5
        if gtype == 1:
            x, y = struct.unpack_from(order + "dd", data, pos)
            return to_wkt("Point", [(x, y)])
        if gtype == 2:
            count = struct.unpack_from(order + "I", data, pos)[0]
            pos += 4
            coords = [struct.unpack_from(order + "dd", data, pos + 16 * i) for i in range(count)]
            return to_wkt("LineString", coords)
    except (struct.error, IndexError):
        return ""
    return ""


# --------------------------------------------------------------------------- paths
def attachments_dir_for(gpkg_path: str) -> str:
    stem = os.path.splitext(os.path.basename(gpkg_path))[0]
    return os.path.join(os.path.dirname(os.path.abspath(gpkg_path)), stem + ATTACHMENTS_SUFFIX)


def is_register(path: str) -> bool:
    """Whether ``path`` is a GeoPackage holding a comment register (read-only probe)."""
    if not (path and os.path.isfile(path)):
        return False
    conn = _connections.get(os.path.normcase(os.path.abspath(path)))
    try:
        if conn is not None:
            return gpkg_sql.table_exists(conn, T_SET) and gpkg_sql.table_exists(conn, T_COMMENT)
        return gpkg_sql.has_table(path, T_SET) and gpkg_sql.has_table(path, T_COMMENT)
    except sqlite3.Error:
        return False


_connections: Dict[str, sqlite3.Connection] = {}


def connect(path: str) -> sqlite3.Connection:
    """Cached connection to a register, in SQLite's rollback-journal mode.

    Not write-ahead logging: QGIS opens the comment layers read-only, and a
    read-only reader cannot open a WAL database whose ``-wal``/``-shm`` side
    files are missing (after the register is moved or copied, or before any
    writer has opened it) - the layers would come up empty. A rollback
    journal also suits registers on network shares. Writers wait up to 10 s
    for a reader or another writer (``busy`` errors after that).
    """
    key = os.path.normcase(os.path.abspath(path))
    conn = _connections.get(key)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            _connections.pop(key, None)
    if not os.path.exists(path):
        raise sqlite3.OperationalError(f"No such file: {path}")
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() == "wal":
            conn.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.OperationalError:
        pass  # another session holds the file; it is switched on a later open
    _connections[key] = conn
    return conn


def close_connection(path: Optional[str] = None) -> None:
    keys = [os.path.normcase(os.path.abspath(path))] if path else list(_connections)
    for key in keys:
        conn = _connections.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _friendly(exc: BaseException, path: str, action: str) -> CommentStoreError:
    text = str(exc)
    low = text.lower()
    name = os.path.basename(path)
    if gpkg_sql.is_transient(exc):
        why = (f"{name} is busy: another QGIS or program is writing to it. Wait a moment and "
               "try again.")
    elif "readonly" in low or "read-only" in low or "permission" in low:
        why = f"{name} is read-only here (file or folder permissions); choose a writable location."
    elif "disk" in low and ("full" in low or "i/o" in low):
        why = f"The disk holding {name} is full or unavailable."
    elif "malformed" in low or "not a database" in low:
        why = f"{name} is damaged or is not a GeoPackage."
    else:
        why = f"{name}: {text}"
    return CommentStoreError(f"Could not {action}. {why}")


# --------------------------------------------------------------------------- store
class CommentStore:
    """A comment register (main thread only)."""

    def __init__(self, gpkg_path: str):
        self.path = os.path.abspath(gpkg_path)

    @property
    def attachments_dir(self) -> str:
        return attachments_dir_for(self.path)

    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]

    # -- lifecycle --------------------------------------------------------------
    @classmethod
    def create(cls, path: str, first_set: Optional[str] = "Comments") -> "CommentStore":
        """Create a register (or add the tables to an existing GeoPackage)."""
        store = cls(path)
        folder = os.path.dirname(store.path)
        try:
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            fresh = not os.path.exists(store.path)
            conn = sqlite3.connect(store.path, timeout=10.0)
            try:
                with conn:
                    if fresh:
                        conn.execute("PRAGMA application_id = 1196444487")  # 'GPKG'
                        conn.execute("PRAGMA user_version = 10200")
                    _create_gpkg_core(conn)
                    _create_tables(conn)
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as exc:
            raise _friendly(exc, store.path, "create the comment register") from exc
        store.open()
        if first_set and not store.list_sets():
            store.create_set(first_set)
        return store

    def open(self) -> "CommentStore":
        """Open an existing register, adding tables/columns a newer version uses.

        A register from a newer plugin version (see :mod:`schema_guard`) is
        refused before anything is written.
        """
        if not is_register(self.path):
            raise CommentStoreError(f"{os.path.basename(self.path)} is not a comment register.")
        try:
            conn = self._conn()
            with gpkg_sql.transaction(conn):
                if gpkg_sql.table_exists(conn, T_META):
                    meta = {r["key"]: r["value"] for r in conn.execute(f"SELECT key, value FROM {_Q(T_META)}")}
                    newer = schema_guard.newer_version_message(
                        meta, SCHEMA_VERSION, f"The comment register {os.path.basename(self.path)}")
                    if newer:
                        raise CommentStoreError(newer)
                _create_tables(conn)
                meta = {r["key"]: r["value"] for r in conn.execute(f"SELECT key, value FROM {_Q(T_META)}")}
                if "schema_version" not in meta:
                    _set_meta(conn, "schema_version", str(SCHEMA_VERSION))
                    _set_meta(conn, "created_utc", utc_now_iso())
        except sqlite3.Error as exc:
            raise _friendly(exc, self.path, "open the comment register") from exc
        return self

    def close(self) -> None:
        close_connection(self.path)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.path)

    @contextmanager
    def _write(self, action: str):
        try:
            conn = self._conn()
            with gpkg_sql.transaction(conn):
                yield conn
        except CommentStoreError:
            raise
        except sqlite3.Error as exc:
            raise _friendly(exc, self.path, action) from exc

    def _read(self, sql: str, params: Sequence = ()) -> List[sqlite3.Row]:
        try:
            return self._conn().execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise _friendly(exc, self.path, "read the comment register") from exc

    def data_version(self) -> int:
        """Changes when another connection (project, QGIS, program) commits."""
        try:
            return int(self._conn().execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.Error:
            return -1

    def meta(self) -> Dict[str, str]:
        return {r["key"]: r["value"] for r in self._read(f"SELECT key, value FROM {_Q(T_META)}")}

    # -- sets -------------------------------------------------------------------
    def list_sets(self) -> List[Dict]:
        rows = [dict(r) for r in self._read(f"SELECT {_cols(SET_COLUMNS)} FROM {_Q(T_SET)}")]
        return sorted(rows, key=lambda r: (str(r.get("name") or "").casefold(), str(r.get("created_utc") or "")))

    def get_set(self, set_id: str) -> Optional[Dict]:
        rows = self._read(f"SELECT {_cols(SET_COLUMNS)} FROM {_Q(T_SET)} WHERE set_id = ?", (set_id,))
        return dict(rows[0]) if rows else None

    def create_set(self, name: str, statuses=None, priorities=None, description: str = "") -> Dict:
        name = str(name or "").strip()
        if not name:
            raise CommentStoreError("A comment set needs a name.")
        if any(s["name"].casefold() == name.casefold() for s in self.list_sets()):
            raise CommentStoreError(f"There is already a comment set called '{name}'.")
        now = utc_now_iso()
        row = {"set_id": new_id(), "name": name, "description": description,
               "statuses_json": json.dumps(model.normalise_statuses(statuses or model.DEFAULT_STATUSES)),
               "priorities_json": json.dumps(model.normalise_priorities(priorities or model.DEFAULT_PRIORITIES)),
               "created_utc": now, "updated_utc": now}
        with self._write("create the comment set") as conn:
            _insert(conn, T_SET, SET_COLUMNS, [row])
        return row

    def update_set(self, set_id: str, name: Optional[str] = None, statuses=None, priorities=None,
                   description: Optional[str] = None) -> Dict:
        row = self.get_set(set_id)
        if row is None:
            raise CommentStoreError("The comment set no longer exists.")
        if name is not None:
            name = str(name).strip()
            if not name:
                raise CommentStoreError("A comment set needs a name.")
            if any(s["name"].casefold() == name.casefold() and s["set_id"] != set_id for s in self.list_sets()):
                raise CommentStoreError(f"There is already a comment set called '{name}'.")
            row["name"] = name
        if statuses is not None:
            row["statuses_json"] = json.dumps(model.normalise_statuses(statuses))
        if priorities is not None:
            row["priorities_json"] = json.dumps(model.normalise_priorities(priorities))
        if description is not None:
            row["description"] = description
        row["updated_utc"] = utc_now_iso()
        with self._write("save the comment set") as conn:
            _update(conn, T_SET, SET_COLUMNS, "set_id", row)
        return row

    def delete_set(self, set_id: str) -> None:
        """Delete a set with its comments, replies, fields and image files."""
        files = [r["file_name"] for r in self._read(
            f"SELECT a.file_name FROM {_Q(T_ATTACH)} a JOIN {_Q(T_COMMENT)} c ON a.comment_id = c.comment_id "
            "WHERE c.set_id = ?", (set_id,))]
        with self._write("delete the comment set") as conn:
            ids = f"SELECT comment_id FROM {_Q(T_COMMENT)} WHERE set_id = ?"
            conn.execute(f"DELETE FROM {_Q(T_REPLY)} WHERE comment_id IN ({ids})", (set_id,))
            conn.execute(f"DELETE FROM {_Q(T_ATTACH)} WHERE comment_id IN ({ids})", (set_id,))
            conn.execute(f"DELETE FROM {_Q(T_COMMENT)} WHERE set_id = ?", (set_id,))
            conn.execute(f"DELETE FROM {_Q(T_FIELD)} WHERE set_id = ?", (set_id,))
            conn.execute(f"DELETE FROM {_Q(T_SET)} WHERE set_id = ?", (set_id,))
        self._remove_files(files)

    @staticmethod
    def statuses_of(set_row: Optional[Dict]) -> List[Dict]:
        try:
            return model.normalise_statuses(json.loads((set_row or {}).get("statuses_json") or "[]"))
        except (TypeError, ValueError):
            return model.normalise_statuses(None)

    @staticmethod
    def priorities_of(set_row: Optional[Dict]) -> List[str]:
        try:
            return model.normalise_priorities(json.loads((set_row or {}).get("priorities_json") or "[]"))
        except (TypeError, ValueError):
            return model.normalise_priorities(None)

    # -- custom fields ------------------------------------------------------------
    def list_fields(self, set_id: str) -> List[model.CustomField]:
        rows = self._read(f"SELECT {_cols(FIELD_COLUMNS)} FROM {_Q(T_FIELD)} WHERE set_id = ?", (set_id,))
        return sorted((model.CustomField.from_row(dict(r)) for r in rows), key=lambda f: (f.seq, f.name.casefold()))

    def save_fields(self, set_id: str, fields: Sequence[model.CustomField]) -> List[model.CustomField]:
        """Replace the set's custom fields (values of removed fields stay in the
        comments' data, unseen, so re-adding a field by mistake loses nothing)."""
        names = set()
        out = []
        for seq, fld in enumerate(fields):
            name = str(fld.name or "").strip()
            if not name:
                raise CommentStoreError("Every custom field needs a name.")
            if name.casefold() in names:
                raise CommentStoreError(f"Two custom fields are called '{name}'.")
            names.add(name.casefold())
            out.append(model.CustomField(fld.field_id or new_id(), name,
                                         fld.kind if fld.kind in model.FIELD_TYPES else model.FIELD_TEXT,
                                         [str(c).strip() for c in fld.choices if str(c).strip()], seq))
        with self._write("save the custom fields") as conn:
            conn.execute(f"DELETE FROM {_Q(T_FIELD)} WHERE set_id = ?", (set_id,))
            _insert(conn, T_FIELD, FIELD_COLUMNS, [f.to_row(set_id) for f in out])
        return out

    # -- comments -----------------------------------------------------------------
    def _select_list(self, table: str, specs, alias: str) -> str:
        """``alias.column`` for each column; ``NULL AS column`` for one a register
        made by an earlier version lacks (another tool may read it without
        :meth:`open`, which adds the missing columns)."""
        present = {str(r[1]) for r in self._read(f"PRAGMA table_info({_Q(table)})")}
        return ", ".join(f"{alias}.{_Q(n)}" if n in present else f"NULL AS {_Q(n)}" for n, _t in specs)

    def _comment_select(self) -> str:
        cols = self._select_list(T_COMMENT, COMMENT_COLUMNS, "c")
        return (f"SELECT {cols}, c.geom AS geom, "
                f"(SELECT COUNT(*) FROM {_Q(T_REPLY)} r WHERE r.comment_id = c.comment_id) AS reply_count, "
                f"(SELECT COUNT(*) FROM {_Q(T_ATTACH)} a WHERE a.comment_id = c.comment_id) AS attachment_count "
                f"FROM {_Q(T_COMMENT)} c")

    @staticmethod
    def _comment_dict(row) -> Dict:
        data = dict(row)
        data["wkt"] = blob_to_wkt(data.pop("geom", None))
        return data

    def list_comments(self, set_id: Optional[str] = None) -> List[Dict]:
        sql, params = self._comment_select(), ()
        if set_id is not None:
            sql, params = sql + " WHERE c.set_id = ?", (set_id,)
        rows = [self._comment_dict(r) for r in self._read(sql + " ORDER BY c.ref_no", params)]
        return rows

    def get_comment(self, comment_id: str) -> Optional[Dict]:
        rows = self._read(self._comment_select() + " WHERE c.comment_id = ?", (comment_id,))
        return self._comment_dict(rows[0]) if rows else None

    def known_ids(self) -> set:
        """Every comment ID and external ID in the register (for duplicate checks)."""
        out = set()
        for row in self._read(f"SELECT comment_id, ext_id FROM {_Q(T_COMMENT)}"):
            out.update(str(v) for v in (row["comment_id"], row["ext_id"]) if v)
        return out

    def add_comment(self, set_id: str, values: Dict) -> Dict:
        return self.add_comments(set_id, [values])[0]

    def add_comments(self, set_id: str, rows: Sequence[Dict]) -> List[Dict]:
        """Add comments in one transaction (all or none). Each needs ``text``,
        ``ref_kind`` and ``wkt`` (EPSG:4326); a given ``comment_id`` must be new.
        ``attach_paths`` (image files) are copied in and attached, the first
        one shown on the map with ``attach_on_map``."""
        if self.get_set(set_id) is None:
            raise CommentStoreError("The comment set no longer exists.")
        for values in rows:
            _check_images(values.get("attach_paths") or ())
        now = utc_now_iso()
        prepared = []
        for values in rows:
            row = {k: values.get(k) for k in EDITABLE}
            row["text"] = str(row.get("text") or "").strip()
            if not row["text"]:
                raise CommentStoreError("A comment needs some text.")
            if row.get("ref_kind") not in model.REF_KINDS:
                raise CommentStoreError("A comment needs a location (route point, KP range or map location).")
            blob = gpkg_blob(values.get("wkt") or "")
            if blob is None:
                raise CommentStoreError("The comment's location geometry is missing or invalid.")
            row.update(comment_id=str(values.get("comment_id") or new_id()), set_id=set_id,
                       created_utc=values.get("created_utc") or now, updated_utc=now, version=1, map_image="")
            prepared.append((row, blob))
        staged: List[Dict] = []
        written: List[str] = []
        for (row, _blob), values in zip(prepared, rows):
            paths = list(values.get("attach_paths") or ())
            if not paths:
                continue
            attached, _files = self._stage(
                [(p, os.path.splitext(p)[1].lower(), os.path.basename(p)) for p in paths], written)
            for item in attached:
                item["comment_id"] = row["comment_id"]
            staged += attached
            if values.get("attach_on_map"):
                row["map_image"] = attached[0]["file_name"]
        try:
            self._insert_comments(set_id, prepared, staged)
        except CommentStoreError:
            self._remove_paths(written)
            raise
        return [self.get_comment(row["comment_id"]) for row, _blob in prepared]

    def _insert_comments(self, set_id: str, prepared, attachments: Sequence[Dict]) -> None:
        with self._write("save the new comments") as conn:
            existing = {r[0] for r in conn.execute(f"SELECT comment_id FROM {_Q(T_COMMENT)}")}
            next_no = (conn.execute(f"SELECT MAX(ref_no) FROM {_Q(T_COMMENT)} WHERE set_id = ?",
                                    (set_id,)).fetchone()[0] or 0) + 1
            for row, blob in prepared:
                if row["comment_id"] in existing:
                    raise CommentStoreError(f"A comment with ID {row['comment_id']} is already in the register.")
                existing.add(row["comment_id"])
                row["ref_no"] = next_no
                next_no += 1
                names = COMMENT_NAMES + ["geom"]
                conn.execute(f"INSERT INTO {_Q(T_COMMENT)} ({', '.join(_Q(n) for n in names)}) VALUES "
                             f"({', '.join('?' for _n in names)})",
                             [gpkg_sql._coerce(row.get(n)) for n in COMMENT_NAMES] + [blob])
            _insert(conn, T_ATTACH, ATTACH_COLUMNS, attachments)
            _touch_contents(conn)

    def update_comment(self, comment_id: str, changes: Dict, expected_version: Optional[int] = None) -> Dict:
        """Save ``changes`` (editable columns, ``wkt``). With ``expected_version``,
        raises :class:`ConflictError` when the comment changed since it was read."""
        with self._write("save the comment") as conn:
            current = conn.execute(f"SELECT version FROM {_Q(T_COMMENT)} WHERE comment_id = ?",
                                   (comment_id,)).fetchone()
            if current is None:
                raise ConflictError("The comment was deleted elsewhere (another project or QGIS) after you "
                                    "opened it. Copy your text before reloading.")
            version = int(current[0] or 0)
            if expected_version is not None and version != int(expected_version):
                raise ConflictError("The comment was changed elsewhere (another project or QGIS) after you "
                                    "opened it. Reload to see that change, or save again to overwrite it.")
            sets = {k: v for k, v in changes.items() if k in EDITABLE}
            if "text" in sets and not str(sets["text"] or "").strip():
                raise CommentStoreError("A comment needs some text.")
            assignments = [f"{_Q(k)} = ?" for k in sets]
            params = [gpkg_sql._coerce(v) for v in sets.values()]
            if "wkt" in changes:
                blob = gpkg_blob(changes.get("wkt") or "")
                if blob is None:
                    raise CommentStoreError("The comment's location geometry is invalid.")
                assignments.append("geom = ?")
                params.append(blob)
            assignments += ["updated_utc = ?", "version = ?"]
            params += [utc_now_iso(), version + 1, comment_id]
            conn.execute(f"UPDATE {_Q(T_COMMENT)} SET {', '.join(assignments)} WHERE comment_id = ?", params)
            _touch_contents(conn)
        return self.get_comment(comment_id)

    def delete_comments(self, comment_ids: Iterable[str]) -> None:
        ids = [str(i) for i in comment_ids]
        if not ids:
            return
        marks = ", ".join("?" for _i in ids)
        files = [r["file_name"] for r in self._read(
            f"SELECT file_name FROM {_Q(T_ATTACH)} WHERE comment_id IN ({marks})", ids)]
        with self._write("delete the comments") as conn:
            for table in (T_REPLY, T_ATTACH, T_COMMENT):
                conn.execute(f"DELETE FROM {_Q(table)} WHERE comment_id IN ({marks})", ids)
            _touch_contents(conn)
        self._remove_files(files)

    def _touch(self, conn, comment_id: str) -> None:
        conn.execute(f"UPDATE {_Q(T_COMMENT)} SET updated_utc = ? WHERE comment_id = ?", (utc_now_iso(), comment_id))

    # -- replies --------------------------------------------------------------------
    def list_replies(self, comment_id: str) -> List[Dict]:
        rows = self._read(f"SELECT {self._select_list(T_REPLY, REPLY_COLUMNS, 'r')} FROM {_Q(T_REPLY)} r "
                          "WHERE r.comment_id = ? ORDER BY r.created_utc, r.fid", (comment_id,))
        return [dict(r) for r in rows]

    def replies_by_comment(self, set_id: Optional[str] = None) -> Dict[str, List[Dict]]:
        sql = f"SELECT {self._select_list(T_REPLY, REPLY_COLUMNS, 'r')} FROM {_Q(T_REPLY)} r"
        params: Tuple = ()
        if set_id is not None:
            sql += f" JOIN {_Q(T_COMMENT)} c ON c.comment_id = r.comment_id WHERE c.set_id = ?"
            params = (set_id,)
        out: Dict[str, List[Dict]] = {}
        for row in self._read(sql + " ORDER BY r.created_utc, r.fid", params):
            out.setdefault(row["comment_id"], []).append(dict(row))
        return out

    def add_reply(self, comment_id: str, author: str, text: str, status_to: str = "") -> Dict:
        """A reply. With ``status_to`` the comment's status changes in the same
        save (*Send & close*), and the reply needs no text."""
        text = str(text or "").strip()
        status_to = str(status_to or "").strip()
        if not text and not status_to:
            raise CommentStoreError("A reply needs some text.")
        now = utc_now_iso()
        row = {"reply_id": new_id(), "comment_id": comment_id, "author": str(author or "").strip(),
               "text": text, "created_utc": now, "status_to": status_to}
        with self._write("save the reply") as conn:
            current = conn.execute(f"SELECT status FROM {_Q(T_COMMENT)} WHERE comment_id = ?",
                                   (comment_id,)).fetchone()
            if current is None:
                raise ConflictError("The comment was deleted elsewhere; the reply was not saved.")
            if status_to:
                row["status_from"] = str(current[0] or "")
            _insert(conn, T_REPLY, REPLY_COLUMNS, [row])
            if status_to:
                conn.execute(f"UPDATE {_Q(T_COMMENT)} SET status = ?, updated_utc = ?, version = version + 1 "
                             "WHERE comment_id = ?", (status_to, now, comment_id))
                _touch_contents(conn)
            else:
                self._touch(conn, comment_id)
        return row

    def update_reply(self, reply_id: str, text: str) -> Dict:
        """Change a reply's text (the status it set stays); it is marked edited."""
        text = str(text or "").strip()
        now = utc_now_iso()
        with self._write("save the reply") as conn:
            row = conn.execute(f"SELECT comment_id, status_to FROM {_Q(T_REPLY)} WHERE reply_id = ?",
                               (reply_id,)).fetchone()
            if row is None:
                raise ConflictError("The reply was deleted elsewhere (another project or QGIS).")
            if not text and not row["status_to"]:
                raise CommentStoreError("A reply needs some text.")
            conn.execute(f"UPDATE {_Q(T_REPLY)} SET text = ?, edited_utc = ? WHERE reply_id = ?", (text, now, reply_id))
            self._touch(conn, row["comment_id"])
        return dict(self._read(f"SELECT {self._select_list(T_REPLY, REPLY_COLUMNS, 'r')} FROM {_Q(T_REPLY)} r "
                               "WHERE r.reply_id = ?", (reply_id,))[0])

    def delete_reply(self, reply_id: str, set_status: Optional[str] = None) -> None:
        """Delete a reply; with ``set_status`` also set its comment's status (one save),
        e.g. back to what a deleted *Send & close* changed."""
        with self._write("delete the reply") as conn:
            row = conn.execute(f"SELECT comment_id FROM {_Q(T_REPLY)} WHERE reply_id = ?", (reply_id,)).fetchone()
            conn.execute(f"DELETE FROM {_Q(T_REPLY)} WHERE reply_id = ?", (reply_id,))
            if row is not None and set_status:
                conn.execute(f"UPDATE {_Q(T_COMMENT)} SET status = ?, updated_utc = ?, version = version + 1 "
                             "WHERE comment_id = ?", (str(set_status), utc_now_iso(), row["comment_id"]))
                _touch_contents(conn)
            elif row is not None:
                self._touch(conn, row["comment_id"])

    # -- attachments ------------------------------------------------------------------
    def list_attachments(self, comment_id: str) -> List[Dict]:
        rows = self._read(f"SELECT {_cols(ATTACH_COLUMNS)} FROM {_Q(T_ATTACH)} WHERE comment_id = ? "
                          "ORDER BY created_utc, fid", (comment_id,))
        return [dict(r) for r in rows]

    def attachment_path(self, row: Dict) -> str:
        return os.path.join(self.attachments_dir, os.path.basename(str(row.get("file_name") or "")))

    def add_attachments(self, comment_id: str, paths: Sequence[str],
                        names: Optional[Sequence[str]] = None) -> List[Dict]:
        """Copy images into the attachments folder and record them (all or
        none); ``names`` are the names shown (default: the files' own)."""
        _check_images(paths)
        names = list(names or [])
        return self._attach(comment_id, [(path, os.path.splitext(path)[1].lower(),
                                          (names[i] if i < len(names) else "") or os.path.basename(path))
                                         for i, path in enumerate(paths)])

    def add_image_bytes(self, comment_id: str, data: bytes, name: str, extension: str = ".png") -> Dict:
        """Record an image given as encoded bytes (e.g. pasted from the clipboard)."""
        extension = str(extension or "").lower()
        if extension not in IMAGE_EXTENSIONS or not data:
            raise CommentStoreError("The pasted image could not be saved.")
        return self._attach(comment_id, [(bytes(data), extension, str(name or "") or "Pasted image" + extension)])[0]

    def _stage(self, items, written: List[str]) -> Tuple[List[Dict], List[str]]:
        """Copy ``(source path or bytes, extension, name shown)`` items into the
        attachments folder under new names: ``(rows without comment_id, files)``.
        Every file started is added to ``written``; on failure they are removed."""
        rows, files = [], []
        try:
            os.makedirs(self.attachments_dir, exist_ok=True)
            for source, extension, shown in items:
                attachment_id = new_id()
                file_name = attachment_id + extension
                target = os.path.join(self.attachments_dir, file_name)
                if os.name == "nt" and len(target) > WINDOWS_MAX_PATH:
                    raise OSError(f"the path is too long for Windows ({len(target)} characters, at most "
                                  f"{WINDOWS_MAX_PATH}); move the register to a folder with a shorter path")
                written.append(target)
                files.append(target)
                if isinstance(source, (bytes, bytearray)):
                    with open(target, "xb") as handle:
                        handle.write(source)
                else:
                    shutil.copy2(source, target)
                rows.append({"attachment_id": attachment_id, "file_name": file_name, "original_name": shown,
                             "caption": "", "created_utc": utc_now_iso()})
        except OSError as exc:
            self._remove_paths(written)
            raise CommentStoreError(f"Could not copy the image into {self.attachments_dir}: {exc}") from exc
        return rows, files

    def _attach(self, comment_id: str, items) -> List[Dict]:
        written: List[str] = []
        rows, _files = self._stage(items, written)
        for row in rows:
            row["comment_id"] = comment_id
        try:
            with self._write("save the images") as conn:
                if conn.execute(f"SELECT 1 FROM {_Q(T_COMMENT)} WHERE comment_id = ?",
                                (comment_id,)).fetchone() is None:
                    raise ConflictError("The comment was deleted elsewhere; the images were not added.")
                _insert(conn, T_ATTACH, ATTACH_COLUMNS, rows)
                self._touch(conn, comment_id)
        except CommentStoreError:
            self._remove_paths(written)
            raise
        return rows

    def set_map_image(self, comment_id: str, attachment_id: Optional[str]) -> None:
        """Show one of the comment's images in its map label (``None``: none)."""
        with self._write("save the map image") as conn:
            name = ""
            if attachment_id:
                row = conn.execute(f"SELECT file_name FROM {_Q(T_ATTACH)} WHERE attachment_id = ? AND "
                                   "comment_id = ?", (attachment_id, comment_id)).fetchone()
                if row is None:
                    raise ConflictError("The image was removed elsewhere (another project or QGIS).")
                name = os.path.basename(str(row[0] or ""))
            changed = conn.execute(f"UPDATE {_Q(T_COMMENT)} SET map_image = ?, updated_utc = ? WHERE comment_id = ?",
                                   (name, utc_now_iso(), comment_id)).rowcount
            if not changed:
                raise ConflictError("The comment was deleted elsewhere (another project or QGIS).")
            _touch_contents(conn)

    def delete_attachment(self, attachment_id: str) -> None:
        rows = self._read(f"SELECT file_name FROM {_Q(T_ATTACH)} WHERE attachment_id = ?", (attachment_id,))
        with self._write("remove the image") as conn:
            conn.execute(f"DELETE FROM {_Q(T_ATTACH)} WHERE attachment_id = ?", (attachment_id,))
            for row in rows:
                if row["file_name"]:
                    conn.execute(f"UPDATE {_Q(T_COMMENT)} SET map_image = '' WHERE map_image = ?",
                                 (row["file_name"],))
            _touch_contents(conn)
        self._remove_files([r["file_name"] for r in rows])

    def _remove_files(self, file_names: Iterable[str]) -> None:
        self._remove_paths(self.attachment_path({"file_name": n}) for n in file_names if n)

    @staticmethod
    def _remove_paths(paths: Iterable[str]) -> None:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass  # already gone, or still open elsewhere: an orphan file is harmless


# --------------------------------------------------------------------------- schema helpers
def _check_images(paths: Iterable[str]) -> None:
    for path in paths:
        if os.path.splitext(path)[1].lower() not in IMAGE_EXTENSIONS:
            raise CommentStoreError(f"{os.path.basename(path)} is not a supported image "
                                    f"({', '.join(IMAGE_EXTENSIONS)}).")
        if not os.path.isfile(path):
            raise CommentStoreError(f"{path} does not exist.")


def _cols(specs) -> str:
    return ", ".join(_Q(n) for n, _t in specs)


def _set_meta(conn, key: str, value: str) -> None:
    conn.execute(f"DELETE FROM {_Q(T_META)} WHERE key = ?", (key,))
    conn.execute(f"INSERT INTO {_Q(T_META)} (key, value) VALUES (?, ?)", (key, value))


def _insert(conn, table: str, specs, rows: Sequence[Dict]) -> None:
    if not rows:
        return
    names = [n for n, _t in specs]
    conn.executemany(f"INSERT INTO {_Q(table)} ({', '.join(_Q(n) for n in names)}) VALUES "
                     f"({', '.join('?' for _n in names)})",
                     [[gpkg_sql._coerce(row.get(n)) for n in names] for row in rows])


def _update(conn, table: str, specs, key: str, row: Dict) -> None:
    names = [n for n, _t in specs if n != key]
    conn.execute(f"UPDATE {_Q(table)} SET {', '.join(_Q(n) + ' = ?' for n in names)} WHERE {_Q(key)} = ?",
                 [gpkg_sql._coerce(row.get(n)) for n in names] + [row.get(key)])


def _touch_contents(conn) -> None:
    conn.execute("UPDATE gpkg_contents SET last_change = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                 "WHERE table_name = ?", (T_COMMENT,))


def _create_gpkg_core(conn) -> None:
    """The GeoPackage system tables (kept when the file already has them)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL "
        "PRIMARY KEY, organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT "
        "NOT NULL, description TEXT)")
    wgs84 = ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
             'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",'
             '0.0174532925199433,AUTHORITY["EPSG","9122"]],AXIS["Latitude",NORTH],AXIS["Longitude",EAST],'
             'AUTHORITY["EPSG","4326"]]')
    for row in (("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate "
                 "reference system"),
                ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate "
                 "reference system"),
                ("WGS 84 geodetic", 4326, "EPSG", 4326, wgs84, "longitude/latitude coordinates in decimal "
                 "degrees on the WGS 84 spheroid")):
        conn.execute("INSERT OR IGNORE INTO gpkg_spatial_ref_sys (srs_name, srs_id, organization, "
                     "organization_coordsys_id, definition, description) VALUES (?, ?, ?, ?, ?, ?)", row)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT "
        "NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT "
        "(strftime('%Y-%m-%dT%H:%M:%fZ','now')), min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, "
        "srs_id INTEGER, CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES "
        "gpkg_spatial_ref_sys(srs_id))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT "
        "NULL, geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT "
        "NULL, CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name), CONSTRAINT uk_gc_table_name "
        "UNIQUE (table_name), CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES "
        "gpkg_contents(table_name), CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES "
        "gpkg_spatial_ref_sys (srs_id))")


def _create_tables(conn) -> None:
    """Create missing register tables and add missing columns (idempotent)."""
    for table, specs in TABLES.items():
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        if not exists:
            cols = ", ".join(f"{_Q(n)} {t}" for n, t in specs)
            geom = ", geom BLOB" if table == T_COMMENT else ""
            conn.execute(f"CREATE TABLE {_Q(table)} (fid INTEGER PRIMARY KEY AUTOINCREMENT, {cols}{geom})")
            kind = "features" if table == T_COMMENT else "attributes"
            conn.execute("INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, description, "
                         "srs_id) VALUES (?, ?, ?, ?, ?)",
                         (table, kind, table, "Route Comments register", SRS_ID if table == T_COMMENT else None))
            if table == T_COMMENT:
                conn.execute("INSERT OR IGNORE INTO gpkg_geometry_columns (table_name, column_name, "
                             "geometry_type_name, srs_id, z, m) VALUES (?, 'geom', 'GEOMETRY', ?, 0, 0)",
                             (table, SRS_ID))
            continue
        present = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({_Q(table)})")}
        for name, sql_type in specs:
            if name not in present:
                conn.execute(f"ALTER TABLE {_Q(table)} ADD COLUMN {_Q(name)} {sql_type}")
    for table, column in ((T_COMMENT, "set_id"), (T_COMMENT, "comment_id"), (T_REPLY, "comment_id"),
                          (T_ATTACH, "comment_id"), (T_FIELD, "set_id")):
        conn.execute(f"CREATE INDEX IF NOT EXISTS {_Q('idx_' + table + '_' + column)} ON {_Q(table)} ({_Q(column)})")


__all__ = ["CommentStore", "CommentStoreError", "ConflictError", "attachments_dir_for", "blob_to_wkt",
           "gpkg_blob", "is_register", "parse_wkt", "to_wkt"]
