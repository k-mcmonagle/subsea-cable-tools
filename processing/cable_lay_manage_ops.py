# cable_lay_manage_ops.py
# -*- coding: utf-8 -*-
"""In-place management operations for cable-lay GeoPackage layers.

Canonical home for operations that *modify* already-imported cable-lay data
(as opposed to :mod:`cable_lay_parsers`, which handles parsing and import).
Used by the "Recompute ISO Time" processing algorithm and the Data Explorer's
management UI, so both stay in sync.

Every edit is all-or-nothing. The scan and the writes run on one SQLite
connection inside a single ``BEGIN IMMEDIATE`` transaction
(:class:`GpkgEditSession`) — targeted UPDATE/DELETE by fid, no table rewrite,
streamed in batches so memory stays flat on multi-gigabyte GeoPackages — and
commit once at the end. Cancelling (``feedback.isCanceled()``) or any error
rolls the whole operation back and raises (:class:`OperationCancelled` for a
cancel), so a half-applied start-date fix can never be re-run on top of
itself. Several operations plus their ``edit_log`` audit row can share one
session so they commit together.
"""

from __future__ import annotations

import os
import sqlite3
import struct
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Set, Tuple

from qgis.core import QgsFeatureRequest, QgsVectorLayer

from ..plugin_log import log_exception
from ..qgis_compat import FEATURE_REQUEST_NO_GEOMETRY
from . import cable_lay_parsers as clp

# numpy is imported inside the gap functions only: this module is loaded at
# QGIS start-up (via the Recompute ISO Time algorithm) and numpy is slow to
# import.
if TYPE_CHECKING:  # annotations only
    import numpy as np

# Raw day-count time columns used by the importers, in preference order.
RAW_TIME_FIELDS = ("Time", "Event Time", "Lay Time")

# Per-file provenance columns used by the importers, in preference order.
SOURCE_FIELDS = ("source_file", "event_file", "slack_file", "body_file")

_BATCH_SIZE = 5000

# Seconds a management edit waits for other connections (a map render, an
# open attribute table) to release the GeoPackage before failing cleanly.
_BUSY_TIMEOUT_S = 30.0


class OperationCancelled(RuntimeError):
    """The user cancelled a management edit; it was rolled back entirely."""

    def __init__(self, message: str = "Cancelled - nothing was changed."):
        super().__init__(message)


def _no_geometry_flag():
    return FEATURE_REQUEST_NO_GEOMETRY


def _report(feedback, done: int, total: int) -> None:
    """Push a percentage to ``feedback`` when it can take one.

    Both ``QgsProcessingFeedback`` and ``QgsTask`` expose ``setProgress``; a
    plain object without it (or ``None``) is silently ignored.
    """
    if feedback is None or total <= 0:
        return
    setter = getattr(feedback, "setProgress", None)
    if setter is not None:
        try:
            setter(min(100.0, done / total * 100.0))
        except Exception:
            pass


def _canceled(feedback) -> bool:
    return feedback is not None and feedback.isCanceled()


def _check_cancel(feedback) -> None:
    if _canceled(feedback):
        raise OperationCancelled()


# ---------------------------------------------------------------------------
# Transactional edit session
# ---------------------------------------------------------------------------
def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _gpkg_header(blob) -> Optional[Tuple[int, int, bool]]:
    """(flags, envelope byte length, little-endian) of a GPKG geometry blob."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    blob = bytes(blob)
    if len(blob) < 8 or blob[:2] != b"GP":
        return None
    flags = blob[3]
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 0x07)
    if envelope is None:
        return None
    return flags, envelope, bool(flags & 0x01)


def _st_isempty(blob):
    header = _gpkg_header(blob)
    if header is None:
        return None
    return 1 if header[0] & 0x10 else 0


def _st_bound(position: int):
    """ST_MinX/MaxX/MinY/MaxY over a GPKG blob (envelope, else a 2D point)."""
    def bound(blob):
        header = _gpkg_header(blob)
        if header is None or header[0] & 0x10:
            return None
        blob = bytes(blob)
        _flags, envelope, little = header
        order = "<" if little else ">"
        if envelope:
            return struct.unpack_from(order + "d", blob, 8 + 8 * position)[0]
        wkb = blob[8:]
        if len(wkb) < 21:
            raise ValueError("geometry bounds need an envelope")
        wkb_order = "<" if wkb[0] == 1 else ">"
        (geom_type,) = struct.unpack_from(wkb_order + "I", wkb, 1)
        if geom_type % 1000 != 1:  # Point / PointZ / PointM / PointZM
            raise ValueError("geometry bounds need an envelope")
        x, y = struct.unpack_from(wkb_order + "dd", wkb, 5)
        return (x, x, y, y)[position]
    return bound


def _register_gpkg_functions(conn: sqlite3.Connection) -> None:
    """The ST_* functions GeoPackage R-tree triggers reference.

    GDAL registers these on its own connections; plain sqlite3 must too, or
    every UPDATE of a spatial table fails to prepare ("no such function:
    ST_IsEmpty") even though attribute edits never fire those triggers.
    """
    conn.create_function("ST_IsEmpty", 1, _st_isempty)
    for position, name in enumerate(("ST_MinX", "ST_MaxX", "ST_MinY", "ST_MaxY")):
        conn.create_function(name, 1, _st_bound(position))


def _layer_table(layer: QgsVectorLayer) -> Tuple[str, str]:
    """(GeoPackage path, table name) behind an OGR layer."""
    from qgis.core import QgsProviderRegistry

    if layer is None or layer.providerType() != "ogr":
        raise RuntimeError("Management edits need a GeoPackage layer.")
    decoded = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
    path = decoded.get("path") or ""
    if not path.lower().endswith(".gpkg") or not os.path.exists(path):
        raise RuntimeError("Management edits need a GeoPackage layer.")
    table = decoded.get("layerName") or ""
    if not table:
        conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_S)
        try:
            names = [r[0] for r in conn.execute(
                "SELECT table_name FROM gpkg_contents "
                "WHERE data_type IN ('features', 'attributes')")]
        finally:
            conn.close()
        if len(names) != 1:
            raise RuntimeError("Could not tell which GeoPackage table the layer uses.")
        table = names[0]
    return path, table


class GpkgEditSession:
    """One all-or-nothing edit of a cable-lay GeoPackage table.

    ``with GpkgEditSession(layer) as session:`` opens a private SQLite
    connection, begins an IMMEDIATE transaction and commits on a clean exit;
    any exception — including :class:`OperationCancelled` — rolls back.
    ``layer`` (normally a private layer opened on the table) is reloaded
    after a commit so its fields and feature count are current. Pass the
    session to several operations, and call :meth:`log_edit`, to commit them
    together.
    """

    def __init__(self, layer: QgsVectorLayer):
        self.layer = layer
        self.gpkg_path, self.table = _layer_table(layer)
        self.conn: Optional[sqlite3.Connection] = None
        # Honour a provider filter like the old provider scan did (OGR hands
        # a GeoPackage filter to SQLite verbatim, so it is valid SQL here).
        self.where = (layer.subsetString() or "").strip()
        if self.where.lower().startswith("select"):
            raise RuntimeError("Clear the layer's SQL query filter before editing it here.")
        self._columns: Optional[List[str]] = None
        self._fid: Optional[str] = None

    def __enter__(self) -> "GpkgEditSession":
        conn = sqlite3.connect(self.gpkg_path, timeout=_BUSY_TIMEOUT_S)
        try:
            _register_gpkg_functions(conn)
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            conn.close()
            raise RuntimeError(
                f"Could not start the edit ({exc}). Close other programs using "
                "this GeoPackage and try again.") from exc
        self.conn = conn
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        conn, self.conn = self.conn, None
        committed = False
        try:
            if exc_type is None:
                try:
                    conn.commit()
                    committed = True
                except sqlite3.Error as error:
                    conn.rollback()
                    raise RuntimeError(f"Could not save the edit ({error}); "
                                       "nothing was changed.") from error
            else:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass  # closing the connection discards the transaction too
        finally:
            conn.close()
            if committed:
                self._reload_layer()
        if isinstance(exc, sqlite3.Error):
            raise RuntimeError(f"The edit failed ({exc}); nothing was changed.") from exc
        return False

    def _reload_layer(self) -> None:
        try:
            self.layer.reload()
        except RuntimeError:
            pass  # layer deleted meanwhile: nothing to refresh

    # -- schema --------------------------------------------------------------
    def columns(self) -> List[str]:
        if self._columns is None:
            info = self.conn.execute(f"PRAGMA table_info({_quote(self.table)})").fetchall()
            self._columns = [str(row[1]) for row in info]
            self._fid = next((str(row[1]) for row in info if row[5] == 1), "rowid")
        return self._columns

    def fid_column(self) -> str:
        self.columns()
        return self._fid

    def ensure_text_column(self, name: str) -> None:
        """Add a TEXT column in the transaction (rolled back with it)."""
        if name.lower() in (c.lower() for c in self.columns()):
            return
        self.conn.execute(f"ALTER TABLE {_quote(self.table)} ADD COLUMN {_quote(name)} TEXT")
        self._columns = None

    # -- data ------------------------------------------------------------------
    def _select(self, names: Sequence[str], with_fid: bool) -> str:
        fid = _quote(self.fid_column())
        cols = [fid] if with_fid else []
        cols += [_quote(n) for n in names]
        sql = f"SELECT {', '.join(cols)} FROM {_quote(self.table)}"
        if self.where:
            sql += f" WHERE ({self.where})"
        return sql + f" ORDER BY {fid}"

    def row_count(self) -> int:
        """Table row count for progress (the maintained OGR count; >= 1)."""
        try:
            row = self.conn.execute(
                "SELECT feature_count FROM gpkg_ogr_contents "
                "WHERE lower(table_name) = lower(?)", (self.table,)).fetchone()
            return max(int(row[0]), 1) if row and row[0] is not None else 1
        except sqlite3.Error:
            return 1

    def sample(self, names: Sequence[str], limit: int) -> List[Tuple]:
        """The first ``limit`` rows' ``names`` values (layer order)."""
        return self.conn.execute(self._select(names, False) + " LIMIT ?",
                                 (int(limit),)).fetchall()

    def scan(self, names: Sequence[str]):
        """Yield ``(fid, value, ...)`` for every row the layer shows.

        Rows are streamed; edits through :meth:`update` / :meth:`delete`
        may interleave (they only touch rows already yielded).
        """
        return self.conn.execute(self._select(names, True))

    def update(self, column: str, values: Sequence[Tuple[object, int]]) -> None:
        """``SET column = value WHERE fid = ?`` for ``(value, fid)`` pairs."""
        if values:
            self.conn.executemany(
                f"UPDATE {_quote(self.table)} SET {_quote(column)} = ? "
                f"WHERE {_quote(self.fid_column())} = ?", values)

    def delete(self, fids: Sequence[int]) -> None:
        fids = list(fids)
        sql = (f"DELETE FROM {_quote(self.table)} "
               f"WHERE {_quote(self.fid_column())} = ?")
        for start in range(0, len(fids), _BATCH_SIZE):
            self.conn.executemany(sql, [(f,) for f in fids[start:start + _BATCH_SIZE]])

    def log_edit(self, row: Dict) -> None:
        """Append an ``edit_log`` row inside this transaction.

        The table must already exist — call :func:`prepare_edit_log` before
        opening the session (creating a table needs the file unlocked).
        """
        table = clp.prefixed_layer_name(self.gpkg_path, "edit_log")
        entry = dict(row)
        entry.setdefault("edited_at", clp.now_iso())
        names = [name for name, _type in clp.EDIT_LOG_SPECS if name in entry]
        self.conn.execute(
            f"INSERT INTO {_quote(table)} ({', '.join(_quote(n) for n in names)}) "
            f"VALUES ({', '.join('?' for _n in names)})",
            [entry[n] for n in names])


def edit_log_row(layer_name: str, operation: str, params: Dict,
                 rows_affected: int, details: str) -> Dict:
    """An ``edit_log`` entry (for :meth:`GpkgEditSession.log_edit`)."""
    import json

    return {
        "layer_name": layer_name,
        "operation": operation,
        "params_json": json.dumps(params, sort_keys=True),
        "rows_affected": int(rows_affected),
        "details": details,
    }


def prepare_edit_log(layer: QgsVectorLayer, transform_context=None) -> None:
    """Create the ``edit_log`` table for ``layer``'s GeoPackage if absent.

    Call before :class:`GpkgEditSession` when the session will log.
    """
    from qgis.core import QgsCoordinateTransformContext

    path, _table = _layer_table(layer)
    clp.ensure_management_layers(path, transform_context or QgsCoordinateTransformContext())


@contextmanager
def _session_for(layer: QgsVectorLayer, session: Optional[GpkgEditSession]):
    if session is not None:
        yield session
        return
    with GpkgEditSession(layer) as own:
        yield own


def check_not_editing(layer: QgsVectorLayer) -> None:
    """Refuse provider-level edits on a layer that is in QGIS edit mode.

    Provider writes bypass the edit buffer, so they would be invisible to an
    open editing session and could be undone by its rollback.
    """
    if layer is not None and layer.isEditable():
        raise RuntimeError(
            f"'{layer.name()}' is in edit mode. Save or discard its edits in "
            "QGIS (toggle editing off) before managing it here."
        )


def reload_project_layers(gpkg_path: str, layer_name: Optional[str] = None, project=None) -> int:
    """Reload every project layer backed by ``gpkg_path`` (optionally one table).

    Call on the main thread after editing a GeoPackage through a private
    connection so loaded copies pick up new rows, fields and deletions.
    Returns the number of layers reloaded.
    """
    from qgis.core import QgsProject, QgsProviderRegistry

    if project is None:
        project = QgsProject.instance()
    target = os.path.normcase(os.path.normpath(gpkg_path))
    registry = QgsProviderRegistry.instance()
    reloaded = 0
    for layer in project.mapLayers().values():
        try:
            decoded = registry.decodeUri(layer.providerType(), layer.source())
        except Exception:
            continue
        path = decoded.get("path", "")
        if not path or os.path.normcase(os.path.normpath(path)) != target:
            continue
        if layer_name and decoded.get("layerName") != layer_name:
            continue
        try:
            layer.reload()
            layer.triggerRepaint()
            reloaded += 1
        except Exception:  # noqa: BLE001 - one stale layer must not stop the rest
            log_exception(f"Could not reload '{layer.name()}' after editing {gpkg_path}; "
                          "it may show stale data until the project is reopened")
            continue
    return reloaded


def source_field_for(layer: QgsVectorLayer) -> Optional[str]:
    """The provenance (file-name) field of a cable-lay layer, if any."""
    names = {field.name() for field in layer.fields()}
    for candidate in SOURCE_FIELDS:
        if candidate in names:
            return candidate
    return None


def raw_time_field_for(layer: QgsVectorLayer, sample_size: int = 200,
                       session: Optional["GpkgEditSession"] = None) -> Optional[str]:
    """The field holding the raw ``day,HH:MM:SS`` values, if one exists.

    Known importer column names are preferred; otherwise every string field is
    probed. A field qualifies when at least one sampled non-null value matches
    the day-count pattern. With ``session`` the sample is read through its
    connection (another connection could be blocked by the open transaction).
    """
    names = [field.name() for field in layer.fields()]
    candidates = [c for c in RAW_TIME_FIELDS if c in names]
    candidates += [n for n in names if n not in candidates and n != "ISO_Time"]

    if session is not None:  # layer-only (virtual) fields are not in the table
        columns = {c.lower() for c in session.columns()}
        candidates = [c for c in candidates if c.lower() in columns]
    samples: Dict[str, List] = {c: [] for c in candidates}
    if session is not None:
        rows = session.sample(candidates, sample_size) if candidates else []
    else:
        request = QgsFeatureRequest().setFlags(_no_geometry_flag()).setLimit(sample_size)
        rows = ([feature[c] for c in candidates] for feature in layer.getFeatures(request))
    for row in rows:
        for candidate, value in zip(candidates, row):
            if value is not None and str(value).strip():
                samples[candidate].append(value)
    for candidate in candidates:
        values = samples[candidate]
        if values and any(clp.looks_like_day_time(v) for v in values):
            return candidate
    return None


def layer_type_for_name(layer_name: str) -> Optional[str]:
    """Map a physical (possibly prefixed) layer name to its canonical type."""
    for layer_type in clp.CANONICAL_SCHEMAS:
        if layer_name == layer_type or layer_name.endswith("_" + layer_type):
            return layer_type
    return None


def _text(value) -> str:
    """A stripped string for an attribute value; QVariant/None nulls become ''.

    Only a *null QVariant* (whose text is ``NULL``) collapses to ``""``; a
    genuine string ``"NULL"`` / ``"null"`` stored in the data is kept.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text == "NULL" and not isinstance(value, str):
        return ""
    return text


def _wanted(value, source_files: Optional[Set[str]]) -> bool:
    if source_files is None:
        return True
    return _text(value) in source_files


def recompute_iso_time(
    layer: QgsVectorLayer,
    start_date: str,
    old_start_date: str = "",
    source_files: Optional[Sequence[str]] = None,
    feedback=None,
    session: Optional[GpkgEditSession] = None,
) -> Dict[str, int]:
    """Rewrite ``ISO_Time`` in place from the stored day-count time column.

    ``start_date`` is the corrected calendar date of day count 1. Rows whose
    raw time column does not parse fall back to shifting the existing
    ``ISO_Time`` by ``start_date - old_start_date`` days when ``old_start_date``
    is given. ``source_files`` (file names, as stored in the layer's
    provenance column) limits the rows touched; ``None`` means every row.

    Returns counts: ``examined``, ``updated``, ``unchanged``, ``skipped``
    (rows with neither a parseable raw time nor a shiftable ``ISO_Time``).
    Raises ``RuntimeError`` on a layer without ``ISO_Time``, without any usable
    time source, or when the update fails, and :class:`OperationCancelled`
    when ``feedback`` is cancelled; either way nothing is changed (or, with a
    shared ``session``, the whole session rolls back).
    """
    fields = layer.fields()
    if fields.indexOf("ISO_Time") < 0:
        raise RuntimeError("Layer has no ISO_Time field to recompute.")
    source_field = source_field_for(layer)
    wanted: Optional[Set[str]] = set(source_files) if source_files is not None else None
    if wanted is not None and source_field is None:
        raise RuntimeError(
            "A source-file filter was given but the layer has no provenance "
            "(source_file) column."
        )
    delta = _day_delta(start_date, old_start_date)

    counts = {"examined": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    with _session_for(layer, session) as edit:
        raw_field = raw_time_field_for(layer, session=edit)
        if raw_field is None and delta is None:
            raise RuntimeError(
                "Layer has no day-count time column, and no previous start date was "
                "given to shift the existing ISO_Time values by."
            )
        names = ["ISO_Time", raw_field or "ISO_Time", source_field or "ISO_Time"]
        total = edit.row_count()
        changes: List[Tuple[str, int]] = []
        for scanned, (fid, iso_value, raw_value, source_value) in enumerate(
                edit.scan(names), 1):
            if scanned % _BATCH_SIZE == 0:
                _check_cancel(feedback)
                _report(feedback, scanned, total)
            if source_field and not _wanted(source_value, wanted):
                continue
            counts["examined"] += 1
            current_str = _text(iso_value)

            new_iso: Optional[str] = None
            if raw_field:
                new_iso = clp.iso_str(clp.parse_day_time(raw_value, start_date))
            if new_iso is None and delta is not None:
                new_iso = _shift_iso(current_str, delta)

            if new_iso is None:
                counts["skipped"] += 1
                continue
            if new_iso == current_str:
                counts["unchanged"] += 1
                continue
            changes.append((new_iso, fid))
            counts["updated"] += 1
            if len(changes) >= _BATCH_SIZE:
                edit.update("ISO_Time", changes)
                changes = []
        _check_cancel(feedback)
        edit.update("ISO_Time", changes)
    return counts


def _day_delta(start_date: str, old_start_date: str) -> Optional[timedelta]:
    """``start_date - old_start_date``, or ``None`` when either is absent/bad."""
    if not start_date or not old_start_date:
        return None
    new_dt = clp.parse_day_time("1,00:00:00", start_date)
    old_dt = clp.parse_day_time("1,00:00:00", old_start_date)
    if new_dt is None or old_dt is None:
        return None
    return new_dt - old_dt


def _shift_iso(iso_value: str, delta: timedelta) -> Optional[str]:
    try:
        dt = datetime.strptime(iso_value, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None
    return clp.iso_str(dt + delta)


def dedupe_layer_in_place(
    layer: QgsVectorLayer,
    key_fields: Sequence[str],
    source_files: Optional[Sequence[str]] = None,
    feedback=None,
    session: Optional[GpkgEditSession] = None,
) -> int:
    """Delete rows duplicating an earlier row on ``key_fields`` (keep lowest fid).

    Mirrors :func:`cable_lay_parsers.merge_and_dedupe` but works in place with
    targeted deletes instead of rewriting the table. Key fields missing from
    the layer are treated as empty (matching the merge behaviour). Returns the
    number of features deleted. All-or-nothing (see :func:`recompute_iso_time`).
    """
    fields = layer.fields()
    source_field = source_field_for(layer)
    wanted: Optional[Set[str]] = set(source_files) if source_files is not None else None

    with _session_for(layer, session) as edit:
        columns = {c.lower() for c in edit.columns()}
        present = [f for f in key_fields if fields.indexOf(f) >= 0 and f.lower() in columns]
        names = present + [source_field or (present[0] if present else "rowid")]
        total = edit.row_count()
        seen: Set[Tuple] = set()
        doomed: List[int] = []
        for scanned, row in enumerate(edit.scan(names), 1):
            if scanned % _BATCH_SIZE == 0:
                _check_cancel(feedback)
                _report(feedback, scanned, total)
            if source_field and wanted is not None and not _wanted(row[-1], wanted):
                continue
            values = dict(zip(present, row[1:]))
            key = tuple(clp.key_value(values[f]) if f in values else "" for f in key_fields)
            if key in seen:
                doomed.append(row[0])
            else:
                seen.add(key)
        _check_cancel(feedback)
        edit.delete(doomed)
    return len(doomed)


def delete_source_rows(
    layer: QgsVectorLayer, source_files: Sequence[str], feedback=None,
    session: Optional[GpkgEditSession] = None,
) -> int:
    """Delete every row whose provenance column matches ``source_files``.

    Returns the number of features deleted. All-or-nothing.
    """
    source_field = source_field_for(layer)
    if source_field is None:
        raise RuntimeError("Layer has no provenance (source_file) column.")
    wanted = set(source_files)
    with _session_for(layer, session) as edit:
        total = edit.row_count()
        doomed: List[int] = []
        for scanned, (fid, source_value) in enumerate(edit.scan([source_field]), 1):
            if scanned % _BATCH_SIZE == 0:
                _check_cancel(feedback)
                _report(feedback, scanned, total)
            if _wanted(source_value, wanted):
                doomed.append(fid)
        _check_cancel(feedback)
        edit.delete(doomed)
    return len(doomed)


# ---------------------------------------------------------------------------
# Record status (non-destructive curation)
# ---------------------------------------------------------------------------
# ``record_status`` marks each row's curation state without deleting anything:
# ``active`` (or empty/NULL) rows are the working dataset; ``standby`` rows are
# available to fill gaps (e.g. a secondary lay computer); ``excluded`` rows are
# curated out. Downstream consumers filter with :func:`active_subset_expression`.
STATUS_FIELD = "record_status"
STATUS_ACTIVE = "active"
STATUS_STANDBY = "standby"
STATUS_EXCLUDED = "excluded"
RECORD_STATUSES = (STATUS_ACTIVE, STATUS_STANDBY, STATUS_EXCLUDED)


def active_subset_expression() -> str:
    """Provider filter that keeps active rows (treating NULL/'' as active)."""
    return (
        f'"{STATUS_FIELD}" IS NULL OR "{STATUS_FIELD}" = \'\' '
        f'OR "{STATUS_FIELD}" = \'{STATUS_ACTIVE}\''
    )


def ensure_status_field(layer: QgsVectorLayer) -> int:
    """Add the ``record_status`` string field if missing; return its index."""
    idx = layer.fields().indexOf(STATUS_FIELD)
    if idx >= 0:
        return idx
    from qgis.core import QgsField

    from ..qgis_compat import FIELD_TYPE_STRING

    if not layer.dataProvider().addAttributes([QgsField(STATUS_FIELD, FIELD_TYPE_STRING)]):
        raise RuntimeError(
            f"Could not add the {STATUS_FIELD} field: "
            f"{layer.dataProvider().error().summary()}"
        )
    layer.updateFields()
    idx = layer.fields().indexOf(STATUS_FIELD)
    if idx < 0:
        raise RuntimeError(f"{STATUS_FIELD} field missing after add.")
    return idx


def apply_status(layer: QgsVectorLayer, fid_to_status: Dict[int, str], feedback=None,
                 session: Optional[GpkgEditSession] = None) -> int:
    """Set ``record_status`` per feature id. Returns rows changed.

    All-or-nothing: the ``record_status`` column (added if missing) and every
    status land in one transaction, or none of them do.
    """
    if not fid_to_status:
        return 0
    items = list(fid_to_status.items())
    with _session_for(layer, session) as edit:
        edit.ensure_text_column(STATUS_FIELD)
        for start in range(0, len(items), _BATCH_SIZE):
            _check_cancel(feedback)
            batch = items[start:start + _BATCH_SIZE]
            edit.update(STATUS_FIELD, [(status, fid) for fid, status in batch])
            _report(feedback, start + len(batch), len(items))
        _check_cancel(feedback)
    return len(items)


def set_source_status(
    layer: QgsVectorLayer, status: str, source_files: Sequence[str], feedback=None,
    session: Optional[GpkgEditSession] = None,
) -> int:
    """Set ``record_status`` for every row of the given source file(s)."""
    if status not in RECORD_STATUSES:
        raise RuntimeError(f"Unknown record status '{status}'.")
    source_field = source_field_for(layer)
    if source_field is None:
        raise RuntimeError("Layer has no provenance (source_file) column.")
    wanted = set(source_files)
    with _session_for(layer, session) as edit:
        total = edit.row_count()
        changes: Dict[int, str] = {}
        for scanned, (fid, source_value) in enumerate(edit.scan([source_field]), 1):
            if scanned % _BATCH_SIZE == 0:
                _check_cancel(feedback)
                _report(feedback, scanned, total)
            if _wanted(source_value, wanted):
                changes[fid] = status
        return apply_status(layer, changes, feedback=feedback, session=edit)


# ---------------------------------------------------------------------------
# Gap analysis + gap fill (pure epoch math; the UI supplies the arrays)
# ---------------------------------------------------------------------------
def find_gaps_in_epochs(
    epochs: Sequence[float], threshold_s: float
) -> List[Tuple[float, float]]:
    """Gaps (as ``(start, end)`` epoch pairs) where consecutive sorted samples
    are more than ``threshold_s`` seconds apart. Non-finite values are ignored.
    Vectorised: one sort plus one diff, whatever the row count.
    """
    import numpy as np

    arr = np.asarray(epochs, dtype=float)
    clean = np.sort(arr[np.isfinite(arr)])
    if clean.size < 2:
        return []
    breaks = np.nonzero(np.diff(clean) > threshold_s)[0]
    return [(float(clean[i]), float(clean[i + 1])) for i in breaks.tolist()]


def gap_index_for_epochs(
    epochs: Sequence[float], gaps: Sequence[Tuple[float, float]]
) -> "np.ndarray":
    """Index (into ``gaps``) of the gap each epoch falls strictly inside, or -1.

    ``np.searchsorted`` on the gap starts, so the cost is O(n log g) instead
    of the O(n * g) of testing every epoch against every gap - the difference
    between a sub-second and a multi-second click on a million-row layer.
    """
    import numpy as np

    arr = np.asarray(epochs, dtype=float)
    out = np.full(arr.shape, -1, dtype=np.int64)
    if arr.size == 0 or not len(gaps):
        return out
    bounds = np.asarray(gaps, dtype=float).reshape(-1, 2)
    order = np.argsort(bounds[:, 0], kind="stable")
    starts = bounds[order, 0]
    ends = bounds[order, 1]
    finite = np.isfinite(arr)
    values = arr[finite]
    pos = np.searchsorted(starts, values, side="right") - 1
    valid = pos >= 0
    inside = np.zeros(values.shape, dtype=bool)
    inside[valid] = (values[valid] > starts[pos[valid]]) & (values[valid] < ends[pos[valid]])
    result = np.full(values.shape, -1, dtype=np.int64)
    result[inside] = order[pos[inside]]
    out[finite] = result
    return out


def epoch_in_gaps(epoch: float, gaps: Sequence[Tuple[float, float]]) -> bool:
    """True when ``epoch`` falls strictly inside one of ``gaps``."""
    if epoch != epoch:
        return False
    return any(start < epoch < end for start, end in gaps)


def classify_gap_fill(
    secondary_epochs: Sequence[float], gaps: Sequence[Tuple[float, float]]
) -> List[str]:
    """Per secondary sample: ``active`` when it fills a primary gap, else
    ``standby``. Same order as the input epochs."""
    indices = gap_index_for_epochs(secondary_epochs, gaps)
    return [STATUS_ACTIVE if i >= 0 else STATUS_STANDBY for i in indices.tolist()]


def count_in_gaps(
    epochs: Sequence[float], gaps: Sequence[Tuple[float, float]]
) -> List[int]:
    """How many of ``epochs`` fall inside each gap (same order as ``gaps``)."""
    if not len(gaps):
        return []
    import numpy as np

    indices = gap_index_for_epochs(epochs, gaps)
    hits = indices[indices >= 0]
    return np.bincount(hits, minlength=len(gaps)).tolist()


# ---------------------------------------------------------------------------
# QGIS temporal navigation
# ---------------------------------------------------------------------------
TEMPORAL_FIELD = "ISO_DateTime"


def _temporal_mode_instant():
    """``FeatureDateTimeInstantFromField`` across the QGIS 3 / 4 enum homes."""
    from qgis.core import Qgis, QgsVectorLayerTemporalProperties

    scope = getattr(Qgis, "VectorTemporalMode", None)
    if scope is not None and hasattr(scope, "FeatureDateTimeInstantFromField"):
        return scope.FeatureDateTimeInstantFromField
    return QgsVectorLayerTemporalProperties.ModeFeatureDateTimeInstantFromField


def enable_temporal_navigation(
    layer: QgsVectorLayer, time_field: str = "ISO_Time", accumulate: bool = False
) -> str:
    """Make a cable-lay layer usable by the QGIS Temporal Controller.

    The importers store ``ISO_Time`` as text (portable, dedupe-friendly), which
    the temporal panel's field picker does not list. Instead of changing the
    stored schema this adds a *virtual* DateTime field ``ISO_DateTime``
    (``to_datetime("ISO_Time")``, lives in the project, never written to the
    GeoPackage) and switches the layer's temporal properties to "single field
    with date/time" on it. ``accumulate`` shows every feature up to the current
    frame instead of only the frame's own features (useful for lay data).
    Returns the virtual field name. Raises ``RuntimeError`` without the time
    field.
    """
    from qgis.core import QgsField

    from ..qgis_compat import FIELD_TYPE_DATETIME

    fields = layer.fields()
    if fields.indexOf(time_field) < 0:
        raise RuntimeError(f"Layer has no {time_field} field.")
    if fields.indexOf(TEMPORAL_FIELD) < 0:
        layer.addExpressionField(
            f'to_datetime("{time_field}")', QgsField(TEMPORAL_FIELD, FIELD_TYPE_DATETIME)
        )
    props = layer.temporalProperties()
    props.setMode(_temporal_mode_instant())
    props.setStartField(TEMPORAL_FIELD)
    try:
        props.setAccumulateFeatures(bool(accumulate))
    except Exception:
        pass
    props.setIsActive(True)
    layer.triggerRepaint()
    return TEMPORAL_FIELD


# ---------------------------------------------------------------------------
# GeoPackage maintenance
# ---------------------------------------------------------------------------
def vacuum_gpkg(gpkg_path: str) -> Tuple[int, int]:
    """Compact a GeoPackage with SQLite ``VACUUM``.

    Returns ``(bytes_before, bytes_after)``. Raises ``RuntimeError`` when the
    file is locked (e.g. layers loaded in another application) or the VACUUM
    fails. Uses only the standard-library ``sqlite3`` module.
    """
    import os
    import sqlite3

    before = os.path.getsize(gpkg_path)
    try:
        connection = sqlite3.connect(gpkg_path, timeout=5)
        try:
            connection.isolation_level = None  # VACUUM cannot run in a transaction
            connection.execute("VACUUM")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"VACUUM failed ({exc}). Close other applications using this "
            "GeoPackage and try again."
        )
    return before, os.path.getsize(gpkg_path)
