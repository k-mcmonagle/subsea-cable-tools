# -*- coding: utf-8 -*-
"""The burial data GeoPackage: datasets, delivered files kept byte for byte, parsed columns.

Acquired data belongs to the project and its cable, not to a plan, so it
lives in its own file beside the plan file (``<project>_burial_data.gpkg``)
and duplicating a plan never copies it. Tables (plain SQL in transactions;
no QGIS needed):

* ``ba_meta``     key / value (schema version, created)
* ``ba_dataset``  one stream of data: type, measure, the RPL its KPs refer to
* ``ba_profile``  import profile versions (a changed profile is a new version)
* ``ba_file``     a delivery: original bytes (compressed), sha256, status
                  (current / replaced / withdrawn), counts, rejected rows,
                  time and KP range, the profile version that read it
* ``ba_chunk``    up to :data:`CHUNK_ROWS` records of a file, with their KP
                  and time extents (so a KP window reads only its chunks)
* ``ba_array``    one compressed column of a chunk (numpy, or JSON for text)
* ``ba_overlap``  overlap decisions between files of a dataset; changing a
                  rule adds a row and marks the old one superseded
* ``ba_log``      what was done, by whom, when and why
* ``ba_template`` burial graph report templates (a changed template is a new version)

Rows are only ever added, apart from statuses; every change writes a
``ba_log`` row in the same transaction. Columns are stored as compressed
float64 (exact for every delivered value): a month of 1 Hz data with 15
channels is about 80 MB and reads in a fraction of a second.

:class:`AcquiredStore` is for the main thread (cached connection, writes);
:class:`StoreReader` opens its own read-only connection, for a background
task. :func:`prepare_file` does the compression without the database, so an
import task can run it before the quick insert on the main thread. The UI
and analysis use only these classes: another backend (a server database)
can implement the same methods later.

The file uses SQLite's rollback journal, not WAL, so map layers QGIS opens
read-only on it work after the file is moved or copied (as the Route
Comments register does).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from ... import schema_guard
from .. import gpkg_sql
from ..change_log import current_user
from ...workbench.schema import new_id, utc_now_iso
from . import overlap as ov
from . import profile as P
from . import roles as R
from .reader import ParsedData

SCHEMA_VERSION = 1
CHUNK_ROWS = 250_000
COMPRESS_LEVEL = 6

STATUS_CURRENT, STATUS_REPLACED, STATUS_WITHDRAWN = "current", "replaced", "withdrawn"
FILE_STATUSES = (STATUS_CURRENT, STATUS_REPLACED, STATUS_WITHDRAWN)
KIND_TRENCHER, KIND_SURVEY, KIND_OTHER = "trencher", "survey", "other"
KINDS = {KIND_TRENCHER: "Trencher / plough log", KIND_SURVEY: "Burial survey", KIND_OTHER: "Other"}
MEASURE_DOL = {"kind": "dol", "reference": "msbl", "units": "m", "positive": "down"}

T_META, T_DATASET, T_PROFILE, T_FILE, T_CHUNK, T_ARRAY, T_OVERLAP, T_LOG, T_TEMPLATE = (
    "ba_meta", "ba_dataset", "ba_profile", "ba_file", "ba_chunk", "ba_array", "ba_overlap", "ba_log",
    "ba_template")

_TABLES: Dict[str, str] = {
    T_META: "key TEXT PRIMARY KEY, value TEXT",
    T_DATASET: ("dataset_id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT, measure_json TEXT, rpl_id TEXT, "
                "rpl_label TEXT, rpl_start_kp REAL, profile_id TEXT, originator TEXT, notes TEXT, "
                "sort_order INTEGER, status TEXT, created_utc TEXT, updated_utc TEXT, author TEXT"),
    T_PROFILE: ("profile_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, profile_json TEXT, "
                "fingerprint TEXT, created_utc TEXT, author TEXT"),
    T_FILE: ("file_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, seq INTEGER NOT NULL, name TEXT, sha256 TEXT, "
             "size_bytes INTEGER, original BLOB, received_utc TEXT, imported_utc TEXT, author TEXT, "
             "originator TEXT, profile_id TEXT, replaces_file_id TEXT, status TEXT, status_reason TEXT, "
             "rows_read INTEGER, rows_kept INTEGER, rows_rejected INTEGER, rejected BLOB, warnings_json TEXT, "
             "channels_json TEXT, time_format TEXT, time_min REAL, time_max REAL, kp_min REAL, kp_max REAL, "
             "acquired_utc TEXT"),
    T_CHUNK: ("chunk_id INTEGER PRIMARY KEY AUTOINCREMENT, file_id TEXT NOT NULL, row0 INTEGER, n INTEGER, "
              "kp_min REAL, kp_max REAL, time_min REAL, time_max REAL"),
    T_ARRAY: "chunk_id INTEGER NOT NULL, channel TEXT NOT NULL, dtype TEXT, data BLOB, PRIMARY KEY (chunk_id, channel)",
    T_OVERLAP: ("overlap_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, older_file_id TEXT, newer_file_id TEXT, "
                "t0 REAL, t1 REAL, older_count INTEGER, newer_count INTEGER, rule TEXT, split_utc REAL, "
                "reason TEXT, author TEXT, created_utc TEXT, superseded_by TEXT"),
    T_LOG: ("log_id INTEGER PRIMARY KEY AUTOINCREMENT, created_utc TEXT, author TEXT, action TEXT, "
            "dataset_id TEXT, target_id TEXT, detail_json TEXT"),
    T_TEMPLATE: ("template_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, template_json TEXT, "
                 "fingerprint TEXT, created_utc TEXT, author TEXT"),
}
_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS ba_file_dataset ON {T_FILE} (dataset_id, seq)",
    f"CREATE INDEX IF NOT EXISTS ba_file_sha ON {T_FILE} (sha256)",
    f"CREATE INDEX IF NOT EXISTS ba_chunk_file ON {T_CHUNK} (file_id, row0)",
    f"CREATE INDEX IF NOT EXISTS ba_overlap_dataset ON {T_OVERLAP} (dataset_id)",
    f"CREATE INDEX IF NOT EXISTS ba_log_dataset ON {T_LOG} (dataset_id, log_id)",
)
_FILE_COLUMNS = ("file_id", "dataset_id", "seq", "name", "sha256", "size_bytes", "received_utc", "imported_utc",
                 "author", "originator", "profile_id", "replaces_file_id", "status", "status_reason", "rows_read",
                 "rows_kept", "rows_rejected", "warnings_json", "channels_json", "time_format", "time_min",
                 "time_max", "kp_min", "kp_max", "acquired_utc")


class AcquiredStoreError(Exception):
    """A store operation failed; the message says why in plain words."""


class DuplicateFileError(AcquiredStoreError):
    def __init__(self, message: str, file_row: Dict):
        super().__init__(message)
        self.file_row = file_row


def _friendly(exc: BaseException, path: str, action: str) -> AcquiredStoreError:
    text = str(exc)
    low = text.lower()
    name = os.path.basename(path)
    if gpkg_sql.is_transient(exc):
        why = f"{name} is busy: another QGIS or program is writing to it. Wait a moment and try again."
    elif "readonly" in low or "read-only" in low or "permission" in low:
        why = f"{name} is read-only here (file or folder permissions); choose a writable location."
    elif "disk" in low and ("full" in low or "i/o" in low):
        why = f"The disk holding {name} is full or unavailable."
    elif "malformed" in low or "not a database" in low:
        why = f"{name} is damaged or is not a GeoPackage."
    else:
        why = f"{name}: {text}"
    return AcquiredStoreError(f"Could not {action}. {why}")


# --------------------------------------------------------------------------- columns as blobs
def pack(values) -> Tuple[str, bytes]:
    """``(dtype, compressed bytes)``: numpy arrays as raw little-endian, text lists as JSON."""
    if isinstance(values, np.ndarray):
        array = np.ascontiguousarray(values)
        dtype = array.dtype.newbyteorder("<").str
        return dtype, zlib.compress(array.astype(dtype, copy=False).tobytes(), COMPRESS_LEVEL)
    return "json", zlib.compress(json.dumps(list(values), ensure_ascii=False).encode("utf-8"), COMPRESS_LEVEL)


def unpack(dtype: str, blob: bytes):
    raw = zlib.decompress(blob)
    if dtype == "json":
        return json.loads(raw.decode("utf-8"))
    return np.frombuffer(raw, dtype=np.dtype(dtype)).copy()


@dataclass
class PreparedFile:
    """A parsed delivery ready to insert: compressed original and columns (no database needed)."""
    name: str
    sha256: str
    size_bytes: int
    original_z: bytes
    chunks: List[Dict]
    channels: List[Dict]
    rows_read: int
    rows_kept: int
    rejected_z: bytes
    rows_rejected: int
    warnings: List[str]
    time_format: str
    time_range: Tuple[Optional[float], Optional[float]]
    kp_range: Tuple[Optional[float], Optional[float]]
    times: np.ndarray = field(default_factory=lambda: np.zeros(0))   # for overlap detection before insert


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def prepare_file(name: str, content: bytes, parsed: ParsedData,
                 cancelled: Optional[Callable[[], bool]] = None) -> PreparedFile:
    """Compress a parsed delivery for :meth:`AcquiredStore.add_file` (safe off the main thread)."""
    chunks = []
    columns = dict(parsed.columns)
    columns["line"] = parsed.line.astype(np.int64)
    for row0 in range(0, parsed.n, CHUNK_ROWS):
        if cancelled and cancelled():
            raise InterruptedError("Import cancelled")
        part = slice(row0, min(row0 + CHUNK_ROWS, parsed.n))
        arrays = {key: pack(values[part]) for key, values in columns.items()}
        for key, values in parsed.texts.items():
            arrays[key] = pack(values[part])
        kp = parsed.columns[R.KP][part]
        time = parsed.columns.get(R.TIME)
        time = time[part] if time is not None else None
        chunks.append({"row0": row0, "n": part.stop - row0, "kp_min": _nanmin(kp), "kp_max": _nanmax(kp),
                       "time_min": _nanmin(time), "time_max": _nanmax(time), "arrays": arrays})
    channels = [{"key": c.key, "label": c.label, "role": c.role, "unit": c.unit, "header": c.header,
                 "auto": c.auto} for c in parsed.channels]
    channels += [{"key": key, "label": R.role(key).label, "role": key, "unit": "", "header": "", "auto": False}
                 for key in parsed.texts]
    rejected = [list(r) for r in parsed.rejected]
    times = parsed.columns.get(R.TIME)
    return PreparedFile(
        name=name, sha256=sha256(content), size_bytes=len(content),
        original_z=zlib.compress(content, COMPRESS_LEVEL), chunks=chunks, channels=channels,
        rows_read=parsed.rows_read, rows_kept=parsed.n,
        rejected_z=zlib.compress(json.dumps(rejected, ensure_ascii=False).encode("utf-8"), COMPRESS_LEVEL),
        rows_rejected=len(rejected), warnings=list(parsed.warnings), time_format=parsed.time_format,
        time_range=parsed.time_range, kp_range=parsed.kp_range,
        times=np.zeros(0) if times is None else times.copy())


def _nanmin(values) -> Optional[float]:
    if values is None or not np.isfinite(values).any():
        return None
    return float(np.nanmin(values))


def _nanmax(values) -> Optional[float]:
    if values is None or not np.isfinite(values).any():
        return None
    return float(np.nanmax(values))


# --------------------------------------------------------------------------- connections
_connections: Dict[str, sqlite3.Connection] = {}


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def connect(path: str) -> sqlite3.Connection:
    """Cached main-thread connection in rollback-journal mode (see the module notes)."""
    key = _key(path)
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
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            conn.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.OperationalError:
        pass  # another session holds the file; switched on a later open
    _connections[key] = conn
    return conn


def close_connection(path: Optional[str] = None) -> None:
    keys = [_key(path)] if path else list(_connections)
    for key in keys:
        conn = _connections.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def is_data_store(path: str) -> bool:
    """Whether ``path`` is a burial data GeoPackage (a read-only probe)."""
    if not (path and os.path.isfile(path)):
        return False
    try:
        return gpkg_sql.has_table(path, T_FILE) and gpkg_sql.has_table(path, T_DATASET)
    except sqlite3.Error:
        return False


def _create_gpkg_core(conn: sqlite3.Connection) -> None:
    """The GeoPackage system tables, so the file is a valid (empty) GeoPackage."""
    conn.execute("CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL "
                 "PRIMARY KEY, organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition "
                 "TEXT NOT NULL, description TEXT)")
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
    conn.execute("CREATE TABLE IF NOT EXISTS gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT "
                 "NOT NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '', last_change DATETIME NOT NULL "
                 "DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, "
                 "max_y DOUBLE, srs_id INTEGER, CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES "
                 "gpkg_spatial_ref_sys(srs_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT "
                 "NULL, geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT "
                 "NOT NULL, CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name), CONSTRAINT "
                 "uk_gc_table_name UNIQUE (table_name), CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES "
                 "gpkg_contents(table_name), CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES "
                 "gpkg_spatial_ref_sys (srs_id))")


def _create_tables(conn: sqlite3.Connection) -> None:
    for table, columns in _TABLES.items():
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
    for statement in _INDEXES:
        conn.execute(statement)


def _row(row) -> Optional[Dict]:
    return dict(row) if row is not None else None


def _json(text, default):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- reading
class _Reads:
    """Read methods shared by the main-thread store and background readers."""

    path: str

    def _conn(self) -> sqlite3.Connection:  # pragma: no cover - provided by subclasses
        raise NotImplementedError

    def _read(self, sql: str, params: Sequence = ()) -> List[sqlite3.Row]:
        try:
            return self._conn().execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise _friendly(exc, self.path, "read the burial data file") from exc

    def meta(self) -> Dict[str, str]:
        return {r["key"]: r["value"] for r in self._read(f"SELECT key, value FROM {T_META}")}

    # datasets
    def list_datasets(self, include_archived: bool = False) -> List[Dict]:
        rows = [self._dataset(r) for r in self._read(f"SELECT * FROM {T_DATASET} ORDER BY sort_order, created_utc")]
        return [r for r in rows if include_archived or r["status"] != "archived"]

    def get_dataset(self, dataset_id: str) -> Optional[Dict]:
        rows = self._read(f"SELECT * FROM {T_DATASET} WHERE dataset_id = ?", (dataset_id,))
        return self._dataset(rows[0]) if rows else None

    @staticmethod
    def _dataset(row) -> Dict:
        out = dict(row)
        out["measure"] = _json(out.pop("measure_json", None), dict(MEASURE_DOL))
        return out

    # profiles
    def get_profile(self, profile_id: str) -> Optional[Dict]:
        rows = self._read(f"SELECT * FROM {T_PROFILE} WHERE profile_id = ?", (profile_id,))
        if not rows:
            return None
        out = dict(rows[0])
        out["profile"] = P.from_json(out.pop("profile_json"))
        return out

    def list_profiles(self, latest_only: bool = True) -> List[Dict]:
        rows = [dict(r) for r in self._read(f"SELECT profile_id, name, version, created_utc, author FROM {T_PROFILE} "
                                            "ORDER BY name COLLATE NOCASE, version")]
        if not latest_only:
            return rows
        latest: Dict[str, Dict] = {}
        for row in rows:
            latest[row["name"]] = row
        return list(latest.values())

    # report templates
    def list_templates(self) -> List[Dict]:
        """The latest version of each saved report template: ``{template_id, name, version, template}``."""
        latest: Dict[str, Dict] = {}
        for row in self._read(f"SELECT * FROM {T_TEMPLATE} ORDER BY name COLLATE NOCASE, version"):
            latest[row["name"]] = dict(row)
        out = []
        for row in latest.values():
            row["template"] = _json(row.pop("template_json", None), {})
            out.append(row)
        return out

    # files
    def list_files(self, dataset_id: str, include_inactive: bool = True) -> List[Dict]:
        sql = f"SELECT {', '.join(_FILE_COLUMNS)} FROM {T_FILE} WHERE dataset_id = ?"
        if not include_inactive:
            sql += f" AND status = '{STATUS_CURRENT}'"
        return [self._file(r) for r in self._read(sql + " ORDER BY seq", (dataset_id,))]

    def get_file(self, file_id: str) -> Optional[Dict]:
        rows = self._read(f"SELECT {', '.join(_FILE_COLUMNS)} FROM {T_FILE} WHERE file_id = ?", (file_id,))
        return self._file(rows[0]) if rows else None

    def find_file_by_sha(self, dataset_id: str, digest: str) -> Optional[Dict]:
        rows = self._read(f"SELECT {', '.join(_FILE_COLUMNS)} FROM {T_FILE} WHERE dataset_id = ? AND sha256 = ? "
                          "ORDER BY seq", (dataset_id, digest))
        return self._file(rows[0]) if rows else None

    @staticmethod
    def _file(row) -> Dict:
        out = dict(row)
        out["warnings"] = _json(out.pop("warnings_json", None), [])
        out["channels"] = _json(out.pop("channels_json", None), [])
        return out

    def original(self, file_id: str) -> Tuple[str, bytes]:
        """The delivered bytes, checked against the sha256 recorded on import."""
        rows = self._read(f"SELECT name, sha256, original FROM {T_FILE} WHERE file_id = ?", (file_id,))
        if not rows:
            raise AcquiredStoreError("That file is not in the burial data file.")
        content = zlib.decompress(rows[0]["original"])
        if sha256(content) != rows[0]["sha256"]:
            raise AcquiredStoreError(f"The stored original of {rows[0]['name']} does not match its checksum.")
        return rows[0]["name"], content

    def rejected_rows(self, file_id: str) -> List[Tuple[int, str, str]]:
        rows = self._read(f"SELECT rejected FROM {T_FILE} WHERE file_id = ?", (file_id,))
        if not rows or rows[0]["rejected"] is None:
            return []
        return [tuple(r) for r in json.loads(zlib.decompress(rows[0]["rejected"]).decode("utf-8"))]

    def read_columns(self, file_id: str, channels: Optional[Iterable[str]] = None,
                     kp_range: Optional[Tuple[float, float]] = None) -> Dict[str, object]:
        """A file's columns (all, or ``channels`` plus ``line``), from the chunks touching ``kp_range``.

        Rows are not trimmed to the range (chunks are); callers filter exactly.
        """
        sql = f"SELECT chunk_id FROM {T_CHUNK} WHERE file_id = ?"
        params: List = [file_id]
        if kp_range is not None:
            lo, hi = sorted(kp_range)
            sql += " AND kp_max >= ? AND kp_min <= ?"
            params += [lo, hi]
        chunk_ids = [r["chunk_id"] for r in self._read(sql + " ORDER BY row0", params)]
        wanted = None if channels is None else set(channels) | {"line"}
        parts: Dict[str, List] = {}
        dtypes: Dict[str, str] = {}
        for chunk_id in chunk_ids:
            for row in self._read(f"SELECT channel, dtype, data FROM {T_ARRAY} WHERE chunk_id = ?", (chunk_id,)):
                if wanted is not None and row["channel"] not in wanted:
                    continue
                parts.setdefault(row["channel"], []).append(unpack(row["dtype"], row["data"]))
                dtypes[row["channel"]] = row["dtype"]
        out: Dict[str, object] = {}
        for key, pieces in parts.items():
            if dtypes[key] == "json":
                out[key] = [item for piece in pieces for item in piece]
            else:
                out[key] = np.concatenate(pieces) if pieces else np.zeros(0)
        return out

    # overlaps
    def list_overlaps(self, dataset_id: str, include_superseded: bool = False) -> List[Dict]:
        sql = f"SELECT * FROM {T_OVERLAP} WHERE dataset_id = ?"
        if not include_superseded:
            sql += " AND superseded_by IS NULL"
        return [dict(r) for r in self._read(sql + " ORDER BY created_utc", (dataset_id,))]

    @staticmethod
    def overlap_of(row: Dict) -> ov.Overlap:
        return ov.Overlap(row["older_file_id"], row["newer_file_id"], row["t0"], row["t1"], row["older_count"],
                          row["newer_count"], row["rule"], row["split_utc"], row["overlap_id"],
                          row.get("reason") or "")

    def file_times(self, dataset_id: str) -> List[ov.FileTimes]:
        """Record times of the dataset's current files (for overlap detection)."""
        out = []
        for row in self.list_files(dataset_id, include_inactive=False):
            columns = self.read_columns(row["file_id"], [R.TIME])
            times = columns.get(R.TIME)
            if times is None:
                times = np.full(int(row["rows_kept"] or 0), np.nan)
            out.append(ov.FileTimes(row["file_id"], int(row["seq"]), times, row["name"]))
        return out

    def effective(self, dataset_id: str, channels: Iterable[str],
                  kp_range: Optional[Tuple[float, float]] = None) -> Dict[str, object]:
        """The dataset's records after file status and overlap rules, concatenated in delivery order.

        Adds ``file_index`` (position in ``files``), ``line`` and, when a
        file has no time column, ``time`` filled with its acquisition date
        (so latest-wins can compare it). ``files`` lists the file rows.
        """
        wanted = list(dict.fromkeys(list(channels) + [R.KP, R.TIME]))
        files = self.list_files(dataset_id, include_inactive=False)
        timeline = [ov.FileTimes(f["file_id"], int(f["seq"]), np.zeros(0), f["name"]) for f in files]
        loaded = []
        for f, ft in zip(files, timeline):
            columns = self.read_columns(f["file_id"], wanted)
            n = len(columns.get("line", []))
            if R.TIME in columns:
                ft.time = np.asarray(columns[R.TIME], dtype=float)
            else:
                ft.time = np.full(n, np.nan)
            loaded.append(columns)
        masks = ov.keep_masks(timeline, [self.overlap_of(r) for r in self.list_overlaps(dataset_id)])
        out: Dict[str, List] = {key: [] for key in wanted + ["line", "file_index"]}
        for index, (f, columns) in enumerate(zip(files, loaded)):
            keep = masks[f["file_id"]]
            if kp_range is not None and R.KP in columns:
                lo, hi = sorted(kp_range)
                kp = np.asarray(columns[R.KP], dtype=float)
                with np.errstate(invalid="ignore"):
                    keep = keep & (kp >= lo) & (kp <= hi)
            for key in wanted:
                values = columns.get(key)
                if key == R.TIME and values is None:
                    acquired = _utc_seconds(f.get("acquired_utc"))
                    values = np.full(keep.size, np.nan if acquired is None else acquired)
                if values is None:
                    role = R.find(key.split(":", 1)[0])
                    text = role is not None and role.kind == R.KIND_TEXT
                    values = [""] * int(keep.size) if text else np.full(keep.size, np.nan)
                if isinstance(values, list):
                    out[key].append([v for v, k in zip(values, keep) if k])
                else:
                    out[key].append(np.asarray(values)[keep])
            out["line"].append(np.asarray(columns.get("line", np.zeros(0, dtype=np.int64)))[keep])
            out["file_index"].append(np.full(int(keep.sum()), index, dtype=np.int64))
        result: Dict[str, object] = {"files": files}
        for key, pieces in out.items():
            if pieces and isinstance(pieces[0], list):
                result[key] = [item for piece in pieces for item in piece]
            else:
                result[key] = np.concatenate(pieces) if pieces else np.zeros(0)
        return result

    def log(self, dataset_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict]:
        sql = f"SELECT * FROM {T_LOG}"
        params: List = []
        if dataset_id is not None:
            sql += " WHERE dataset_id = ?"
            params.append(dataset_id)
        sql += " ORDER BY log_id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = [dict(r) for r in self._read(sql, params)]
        for row in rows:
            row["detail"] = _json(row.pop("detail_json", None), {})
        return rows


def _utc_seconds(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    from datetime import datetime, timezone
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


class StoreReader(_Reads):
    """A read-only view on its own connection (for background tasks): use as a context manager."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._connection: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "StoreReader":
        try:
            self._connection = sqlite3.connect(gpkg_sql._readonly_uri(self.path), uri=True, timeout=10.0)
        except sqlite3.Error as exc:
            raise _friendly(exc, self.path, "open the burial data file") from exc
        self._connection.row_factory = sqlite3.Row
        return self

    def __exit__(self, *exc_info) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise AcquiredStoreError("The reader is not open.")
        return self._connection


# --------------------------------------------------------------------------- the store
class AcquiredStore(_Reads):
    """The burial data file: the main thread's cached connection, or a task's own (:meth:`for_task`)."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._private: Optional[sqlite3.Connection] = None

    @classmethod
    def for_task(cls, path: str) -> "AcquiredStore":
        """A store on a private connection, for writes from a background task (close it there)."""
        store = cls(path)
        if not is_data_store(store.path):
            raise AcquiredStoreError(f"{os.path.basename(store.path)} is not a burial data file.")
        try:
            store._private = sqlite3.connect(store.path, timeout=30.0)
        except sqlite3.Error as exc:
            raise _friendly(exc, store.path, "open the burial data file") from exc
        store._private.row_factory = sqlite3.Row
        return store

    @contextmanager
    def batch(self, action: str):
        """One transaction around several writes: all of them, or none (an exception rolls back)."""
        with self._write(action) as conn:
            yield conn

    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]

    @classmethod
    def create(cls, path: str) -> "AcquiredStore":
        """Create the file (or add the tables to an existing GeoPackage) and open it."""
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
            raise _friendly(exc, store.path, "create the burial data file") from exc
        return store.open()

    def open(self) -> "AcquiredStore":
        """Open an existing file, adding tables a newer version uses."""
        if not is_data_store(self.path):
            raise AcquiredStoreError(f"{os.path.basename(self.path)} is not a burial data file.")
        try:
            conn = self._conn()
            with gpkg_sql.transaction(conn):
                _create_tables(conn)
                meta = {r["key"]: r["value"] for r in conn.execute(f"SELECT key, value FROM {T_META}")}
                newer = schema_guard.newer_version_message(
                    meta, SCHEMA_VERSION, f"The burial data file {os.path.basename(self.path)}")
                if newer:
                    raise AcquiredStoreError(newer)
                if not meta.get("schema_version"):
                    self._set_meta(conn, "schema_version", str(SCHEMA_VERSION))
                    self._set_meta(conn, "created_utc", utc_now_iso())
        except sqlite3.Error as exc:
            raise _friendly(exc, self.path, "open the burial data file") from exc
        return self

    def close(self) -> None:
        if self._private is not None:
            try:
                self._private.close()
            except sqlite3.Error:
                pass
            self._private = None
            return
        close_connection(self.path)

    def _conn(self) -> sqlite3.Connection:
        return self._private if self._private is not None else connect(self.path)

    def data_version(self) -> int:
        """Changes when another connection (another QGIS or program) commits."""
        try:
            return int(self._conn().execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.Error:
            return -1

    @contextmanager
    def _write(self, action: str):
        try:
            conn = self._conn()
            with gpkg_sql.transaction(conn):
                yield conn
        except AcquiredStoreError:
            raise
        except sqlite3.Error as exc:
            raise _friendly(exc, self.path, action) from exc

    @staticmethod
    def _set_meta(conn, key: str, value: str) -> None:
        conn.execute(f"INSERT OR REPLACE INTO {T_META} (key, value) VALUES (?, ?)", (key, value))

    @staticmethod
    def _log(conn, action: str, dataset_id: Optional[str], target_id: Optional[str], detail: Dict) -> None:
        conn.execute(f"INSERT INTO {T_LOG} (created_utc, author, action, dataset_id, target_id, detail_json) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (utc_now_iso(), current_user(), action, dataset_id, target_id,
                      json.dumps(detail, ensure_ascii=False, sort_keys=True)))

    # datasets
    def create_dataset(self, name: str, kind: str = KIND_TRENCHER, rpl_id: str = "", rpl_label: str = "",
                       rpl_start_kp: Optional[float] = None, originator: str = "", notes: str = "") -> str:
        if not str(name).strip():
            raise AcquiredStoreError("Give the dataset a name.")
        if kind not in KINDS:
            raise AcquiredStoreError(f"'{kind}' is not a dataset type.")
        dataset_id = new_id()
        now = utc_now_iso()
        with self._write("create the dataset") as conn:
            order = conn.execute(f"SELECT COALESCE(MAX(sort_order), -1) + 1 FROM {T_DATASET}").fetchone()[0]
            conn.execute(f"INSERT INTO {T_DATASET} (dataset_id, name, kind, measure_json, rpl_id, rpl_label, "
                         "rpl_start_kp, profile_id, originator, notes, sort_order, status, created_utc, "
                         "updated_utc, author) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'active', ?, ?, ?)",
                         (dataset_id, name.strip(), kind, json.dumps(MEASURE_DOL), rpl_id, rpl_label, rpl_start_kp,
                          originator, notes, order, now, now, current_user()))
            self._log(conn, "create_dataset", dataset_id, dataset_id,
                      {"name": name.strip(), "kind": kind, "rpl_id": rpl_id, "rpl_label": rpl_label})
        return dataset_id

    _DATASET_EDITABLE = ("name", "kind", "rpl_id", "rpl_label", "rpl_start_kp", "profile_id", "originator",
                         "notes", "sort_order", "status")

    def update_dataset(self, dataset_id: str, changes: Dict, reason: str = "") -> Dict:
        unknown = set(changes) - set(self._DATASET_EDITABLE)
        if unknown:
            raise AcquiredStoreError(f"Cannot change {', '.join(sorted(unknown))} of a dataset.")
        before = self.get_dataset(dataset_id)
        if before is None:
            raise AcquiredStoreError("That dataset is not in the burial data file.")
        if "kind" in changes and changes["kind"] not in KINDS:
            raise AcquiredStoreError(f"'{changes['kind']}' is not a dataset type.")
        if "name" in changes and not str(changes["name"]).strip():
            raise AcquiredStoreError("Give the dataset a name.")
        with self._write("change the dataset") as conn:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE {T_DATASET} SET {sets}, updated_utc = ? WHERE dataset_id = ?",
                         (*changes.values(), utc_now_iso(), dataset_id))
            self._log(conn, "update_dataset", dataset_id, dataset_id,
                      {"before": {k: before.get(k) for k in changes}, "after": changes, "reason": reason})
        return self.get_dataset(dataset_id)

    # profiles
    def save_profile(self, name: str, profile: Dict) -> str:
        """The id of ``profile`` under ``name``: the latest version if it reads the same, else a new version."""
        name = str(name or "").strip() or "Import profile"
        clean = P.normalise(profile)
        clean["name"] = name
        fingerprint = hashlib.sha256(P.fingerprint_text(clean).encode("utf-8")).hexdigest()
        rows = self._read(f"SELECT profile_id, version, fingerprint FROM {T_PROFILE} WHERE name = ? "
                          "ORDER BY version DESC LIMIT 1", (name,))
        if rows and rows[0]["fingerprint"] == fingerprint:
            return rows[0]["profile_id"]
        version = rows[0]["version"] + 1 if rows else 1
        profile_id = new_id()
        with self._write("save the import profile") as conn:
            conn.execute(f"INSERT INTO {T_PROFILE} (profile_id, name, version, profile_json, fingerprint, "
                         "created_utc, author) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (profile_id, name, version, P.to_json(clean), fingerprint, utc_now_iso(), current_user()))
            self._log(conn, "save_profile", None, profile_id, {"name": name, "version": version})
        return profile_id

    def save_template(self, name: str, template: Dict) -> str:
        """The id of ``template`` under ``name``: the latest version if unchanged, else a new version."""
        from . import report_template as RT
        name = str(name or "").strip() or "Burial graph"
        clean = RT.normalise(template)
        clean["name"] = name
        fingerprint = hashlib.sha256(RT.fingerprint_text(clean).encode("utf-8")).hexdigest()
        rows = self._read(f"SELECT template_id, version, fingerprint FROM {T_TEMPLATE} WHERE name = ? "
                          "ORDER BY version DESC LIMIT 1", (name,))
        if rows and rows[0]["fingerprint"] == fingerprint:
            return rows[0]["template_id"]
        version = rows[0]["version"] + 1 if rows else 1
        template_id = new_id()
        with self._write("save the report template") as conn:
            conn.execute(f"INSERT INTO {T_TEMPLATE} (template_id, name, version, template_json, fingerprint, "
                         "created_utc, author) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (template_id, name, version, RT.to_json(clean), fingerprint, utc_now_iso(), current_user()))
            self._log(conn, "save_template", None, template_id, {"name": name, "version": version})
        return template_id

    # files
    def add_file(self, dataset_id: str, prepared: PreparedFile, profile_id: Optional[str] = None,
                 received_utc: str = "", originator: str = "", acquired_utc: str = "",
                 replaces_file_id: Optional[str] = None, overlaps: Sequence[ov.Overlap] = (),
                 reason: str = "") -> str:
        """Insert a prepared delivery, its replacement and overlap decisions in one transaction.

        Raises :class:`DuplicateFileError` when the same bytes are already in
        the dataset. ``overlaps`` are those found for this file (its id is
        filled in as the newer file).
        """
        if self.get_dataset(dataset_id) is None:
            raise AcquiredStoreError("That dataset is not in the burial data file.")
        duplicate = self.find_file_by_sha(dataset_id, prepared.sha256)
        if duplicate is not None:
            raise DuplicateFileError(
                f"{prepared.name} is already in this dataset: imported as {duplicate['name']} on "
                f"{str(duplicate['imported_utc'])[:10]} by {duplicate['author'] or 'unknown'}.", duplicate)
        if prepared.time_range[0] is None and not acquired_utc:
            raise AcquiredStoreError(f"{prepared.name} has no time column: give the date it was acquired.")
        for o in overlaps:
            problems = o.problems()
            if problems:
                raise AcquiredStoreError(" ".join(problems))
        file_id = new_id()
        now = utc_now_iso()
        with self._write(f"import {prepared.name}") as conn:
            if replaces_file_id:
                old = conn.execute(f"SELECT status, name FROM {T_FILE} WHERE file_id = ? AND dataset_id = ?",
                                   (replaces_file_id, dataset_id)).fetchone()
                if old is None:
                    raise AcquiredStoreError("The file to replace is not in this dataset.")
                conn.execute(f"UPDATE {T_FILE} SET status = ?, status_reason = ? WHERE file_id = ?",
                             (STATUS_REPLACED, reason or f"Replaced by {prepared.name}", replaces_file_id))
                self._log(conn, "replace_file", dataset_id, replaces_file_id,
                          {"replaced_by": file_id, "name": old["name"], "reason": reason})
            seq = conn.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {T_FILE} WHERE dataset_id = ?",
                               (dataset_id,)).fetchone()[0]
            conn.execute(
                f"INSERT INTO {T_FILE} (file_id, dataset_id, seq, name, sha256, size_bytes, original, received_utc, "
                "imported_utc, author, originator, profile_id, replaces_file_id, status, status_reason, rows_read, "
                "rows_kept, rows_rejected, rejected, warnings_json, channels_json, time_format, time_min, "
                "time_max, kp_min, kp_max, acquired_utc) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, dataset_id, seq, prepared.name, prepared.sha256, prepared.size_bytes,
                 prepared.original_z, received_utc or now, now, current_user(), originator, profile_id,
                 replaces_file_id, STATUS_CURRENT, "", prepared.rows_read, prepared.rows_kept,
                 prepared.rows_rejected, prepared.rejected_z, json.dumps(prepared.warnings, ensure_ascii=False),
                 json.dumps(prepared.channels, ensure_ascii=False), prepared.time_format,
                 prepared.time_range[0], prepared.time_range[1], prepared.kp_range[0], prepared.kp_range[1],
                 acquired_utc or None))
            for chunk in prepared.chunks:
                cursor = conn.execute(
                    f"INSERT INTO {T_CHUNK} (file_id, row0, n, kp_min, kp_max, time_min, time_max) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (file_id, chunk["row0"], chunk["n"], chunk["kp_min"], chunk["kp_max"], chunk["time_min"],
                     chunk["time_max"]))
                conn.executemany(f"INSERT INTO {T_ARRAY} (chunk_id, channel, dtype, data) VALUES (?, ?, ?, ?)",
                                 [(cursor.lastrowid, key, dtype, blob)
                                  for key, (dtype, blob) in chunk["arrays"].items()])
            self._log(conn, "import_file", dataset_id, file_id,
                      {"name": prepared.name, "sha256": prepared.sha256, "rows_read": prepared.rows_read,
                       "rows_kept": prepared.rows_kept, "rows_rejected": prepared.rows_rejected,
                       "profile_id": profile_id, "replaces": replaces_file_id, "reason": reason})
            for o in overlaps:
                o.newer_id = file_id
                self._insert_overlap(conn, dataset_id, o, reason or "found on import")
        return file_id

    def _insert_overlap(self, conn, dataset_id: str, o: ov.Overlap, reason: str) -> str:
        overlap_id = new_id()
        conn.execute(f"INSERT INTO {T_OVERLAP} (overlap_id, dataset_id, older_file_id, newer_file_id, t0, t1, "
                     "older_count, newer_count, rule, split_utc, reason, author, created_utc, superseded_by) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                     (overlap_id, dataset_id, o.older_id, o.newer_id, o.t0, o.t1, o.older_count, o.newer_count,
                      o.rule, o.split_utc, reason, current_user(), utc_now_iso()))
        self._log(conn, "overlap", dataset_id, overlap_id,
                  {"older": o.older_id, "newer": o.newer_id, "t0": o.t0, "t1": o.t1, "rule": o.rule,
                   "split_utc": o.split_utc, "reason": reason})
        o.overlap_id = overlap_id
        return overlap_id

    def add_overlaps(self, dataset_id: str, overlaps: Sequence[ov.Overlap], reason: str) -> List[str]:
        """Record overlaps found later (e.g. after restoring a replaced file)."""
        for o in overlaps:
            if o.problems():
                raise AcquiredStoreError(" ".join(o.problems()))
        with self._write("record the overlaps") as conn:
            return [self._insert_overlap(conn, dataset_id, o, reason) for o in overlaps]

    def change_overlap(self, overlap_id: str, rule: str, reason: str, split_utc: Optional[float] = None) -> str:
        """A new decision for an overlap (the old one is kept, marked superseded). Needs a reason."""
        if not str(reason).strip():
            raise AcquiredStoreError("Say why the overlap rule changes.")
        rows = self._read(f"SELECT * FROM {T_OVERLAP} WHERE overlap_id = ?", (overlap_id,))
        if not rows:
            raise AcquiredStoreError("That overlap is not in the burial data file.")
        row = dict(rows[0])
        if row["superseded_by"]:
            raise AcquiredStoreError("That overlap decision has already been changed; refresh and try again.")
        new = self.overlap_of(row)
        new.rule, new.split_utc = rule, split_utc
        problems = new.problems()
        if problems:
            raise AcquiredStoreError(" ".join(problems))
        with self._write("change the overlap rule") as conn:
            new_id_ = self._insert_overlap(conn, row["dataset_id"], new, reason.strip())
            conn.execute(f"UPDATE {T_OVERLAP} SET superseded_by = ? WHERE overlap_id = ?", (new_id_, overlap_id))
        return new_id_

    def record(self, action: str, dataset_id: Optional[str], detail: Dict) -> None:
        """Note something done with the data (e.g. a KP check) in the history; nothing else changes."""
        with self._write("record it in the history") as conn:
            self._log(conn, action, dataset_id, dataset_id, detail)

    def set_file_status(self, file_id: str, status: str, reason: str) -> None:
        """Withdraw a file or bring a replaced one back (needs a reason)."""
        if status not in FILE_STATUSES:
            raise AcquiredStoreError(f"'{status}' is not a file status.")
        if not str(reason).strip():
            raise AcquiredStoreError("Say why the file's status changes.")
        before = self.get_file(file_id)
        if before is None:
            raise AcquiredStoreError("That file is not in the burial data file.")
        with self._write("change the file's status") as conn:
            conn.execute(f"UPDATE {T_FILE} SET status = ?, status_reason = ? WHERE file_id = ?",
                         (status, reason.strip(), file_id))
            self._log(conn, "file_status", before["dataset_id"], file_id,
                      {"name": before["name"], "before": before["status"], "after": status, "reason": reason})
