# cable_lay_manage_ops.py
# -*- coding: utf-8 -*-
"""In-place management operations for cable-lay GeoPackage layers.

Canonical home for operations that *modify* already-imported cable-lay data
(as opposed to :mod:`cable_lay_parsers`, which handles parsing and import).
Used by the "Recompute ISO Time" processing algorithm and intended to back the
Data Explorer's management UI as well, so both stay in sync.

All edits go through the layer's data provider (``changeAttributeValues`` /
``deleteFeatures``), i.e. SQLite UPDATE/DELETE under the hood — no full-table
rewrite — so they stay fast and memory-light on multi-gigabyte GeoPackages.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from qgis.core import QgsFeatureRequest, QgsVectorLayer

from ..qgis_compat import FEATURE_REQUEST_NO_GEOMETRY
from . import cable_lay_parsers as clp

# Raw day-count time columns used by the importers, in preference order.
RAW_TIME_FIELDS = ("Time", "Event Time", "Lay Time")

# Per-file provenance columns used by the importers, in preference order.
SOURCE_FIELDS = ("source_file", "event_file", "slack_file", "body_file")

_BATCH_SIZE = 5000


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


def _estimated_count(layer: QgsVectorLayer) -> int:
    try:
        return max(int(layer.featureCount()), 1)
    except Exception:
        return 1


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
        except Exception:
            continue
    return reloaded


def source_field_for(layer: QgsVectorLayer) -> Optional[str]:
    """The provenance (file-name) field of a cable-lay layer, if any."""
    names = {field.name() for field in layer.fields()}
    for candidate in SOURCE_FIELDS:
        if candidate in names:
            return candidate
    return None


def raw_time_field_for(layer: QgsVectorLayer, sample_size: int = 200) -> Optional[str]:
    """The field holding the raw ``day,HH:MM:SS`` values, if one exists.

    Known importer column names are preferred; otherwise every string field is
    probed. A field qualifies when at least one sampled non-null value matches
    the day-count pattern.
    """
    names = [field.name() for field in layer.fields()]
    candidates = [c for c in RAW_TIME_FIELDS if c in names]
    candidates += [n for n in names if n not in candidates and n != "ISO_Time"]

    samples: Dict[str, List] = {c: [] for c in candidates}
    request = QgsFeatureRequest().setFlags(_no_geometry_flag()).setLimit(sample_size)
    for feature in layer.getFeatures(request):
        for candidate in candidates:
            value = feature[candidate]
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
    time source, or when the provider rejects the update.
    """
    fields = layer.fields()
    iso_idx = fields.indexOf("ISO_Time")
    if iso_idx < 0:
        raise RuntimeError("Layer has no ISO_Time field to recompute.")

    raw_field = raw_time_field_for(layer)
    delta = _day_delta(start_date, old_start_date)
    if raw_field is None and delta is None:
        raise RuntimeError(
            "Layer has no day-count time column, and no previous start date was "
            "given to shift the existing ISO_Time values by."
        )

    source_field = source_field_for(layer)
    wanted: Optional[Set[str]] = set(source_files) if source_files is not None else None
    if wanted is not None and source_field is None:
        raise RuntimeError(
            "A source-file filter was given but the layer has no provenance "
            "(source_file) column."
        )

    attr_names = ["ISO_Time"]
    if raw_field:
        attr_names.append(raw_field)
    if source_field:
        attr_names.append(source_field)
    request = QgsFeatureRequest().setFlags(_no_geometry_flag())
    request.setSubsetOfAttributes(attr_names, fields)

    provider = layer.dataProvider()
    counts = {"examined": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    changes: Dict[int, Dict[int, object]] = {}
    total = _estimated_count(layer)
    scanned = 0

    def flush():
        if not changes:
            return
        if not provider.changeAttributeValues(dict(changes)):
            raise RuntimeError(
                f"Provider rejected the ISO_Time update: {provider.error().summary()}"
            )
        changes.clear()

    for feature in layer.getFeatures(request):
        scanned += 1
        if scanned % _BATCH_SIZE == 0:
            if _canceled(feedback):
                break
            _report(feedback, scanned, total)
        if source_field and not _wanted(feature[source_field], wanted):
            continue
        counts["examined"] += 1
        current_str = _text(feature[iso_idx])

        new_iso: Optional[str] = None
        if raw_field:
            new_iso = clp.iso_str(clp.parse_day_time(feature[raw_field], start_date))
        if new_iso is None and delta is not None:
            new_iso = _shift_iso(current_str, delta)

        if new_iso is None:
            counts["skipped"] += 1
            continue
        if new_iso == current_str:
            counts["unchanged"] += 1
            continue
        changes[feature.id()] = {iso_idx: new_iso}
        counts["updated"] += 1
        if len(changes) >= _BATCH_SIZE:
            flush()
    flush()
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
) -> int:
    """Delete rows duplicating an earlier row on ``key_fields`` (keep lowest fid).

    Mirrors :func:`cable_lay_parsers.merge_and_dedupe` but works in place via
    ``deleteFeatures`` instead of rewriting the table. Key fields missing from
    the layer are treated as empty (matching the merge behaviour). Returns the
    number of features deleted.
    """
    fields = layer.fields()
    source_field = source_field_for(layer)
    wanted: Optional[Set[str]] = set(source_files) if source_files is not None else None

    attr_names = [f for f in key_fields if fields.indexOf(f) >= 0]
    if source_field and source_field not in attr_names:
        attr_names.append(source_field)
    request = QgsFeatureRequest().setFlags(_no_geometry_flag())
    request.setSubsetOfAttributes(attr_names, fields)

    seen: Set[Tuple] = set()
    doomed: List[int] = []
    total = _estimated_count(layer)
    for scanned, feature in enumerate(layer.getFeatures(request), 1):
        if scanned % _BATCH_SIZE == 0:
            if _canceled(feedback):
                return 0
            _report(feedback, scanned, total)
        if source_field and wanted is not None and not _wanted(feature[source_field], wanted):
            continue
        key = tuple(
            "" if fields.indexOf(f) < 0 else clp.key_value(feature[f]) for f in key_fields
        )
        if key in seen:
            doomed.append(feature.id())
        else:
            seen.add(key)
    if not doomed:
        return 0
    _delete_fids(layer, doomed)
    return len(doomed)


def _delete_fids(layer: QgsVectorLayer, fids: List[int]) -> None:
    provider = layer.dataProvider()
    for start in range(0, len(fids), _BATCH_SIZE):
        if not provider.deleteFeatures(fids[start:start + _BATCH_SIZE]):
            raise RuntimeError(
                f"Provider rejected the delete: {provider.error().summary()}"
            )


def delete_source_rows(
    layer: QgsVectorLayer, source_files: Sequence[str], feedback=None
) -> int:
    """Delete every row whose provenance column matches ``source_files``.

    Returns the number of features deleted.
    """
    source_field = source_field_for(layer)
    if source_field is None:
        raise RuntimeError("Layer has no provenance (source_file) column.")
    wanted = set(source_files)
    request = QgsFeatureRequest().setFlags(_no_geometry_flag())
    request.setSubsetOfAttributes([source_field], layer.fields())
    total = _estimated_count(layer)
    doomed: List[int] = []
    for scanned, feature in enumerate(layer.getFeatures(request), 1):
        if scanned % _BATCH_SIZE == 0:
            if _canceled(feedback):
                return 0
            _report(feedback, scanned, total)
        if _wanted(feature[source_field], wanted):
            doomed.append(feature.id())
    if doomed:
        _delete_fids(layer, doomed)
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


def apply_status(layer: QgsVectorLayer, fid_to_status: Dict[int, str], feedback=None) -> int:
    """Set ``record_status`` per feature id (batched). Returns rows changed."""
    if not fid_to_status:
        return 0
    idx = ensure_status_field(layer)
    provider = layer.dataProvider()
    items = list(fid_to_status.items())
    for start in range(0, len(items), _BATCH_SIZE):
        if _canceled(feedback):
            return start
        batch = {fid: {idx: status} for fid, status in items[start:start + _BATCH_SIZE]}
        if not provider.changeAttributeValues(batch):
            raise RuntimeError(
                f"Provider rejected the status update: {provider.error().summary()}"
            )
        _report(feedback, start + len(batch), len(items))
    return len(items)


def set_source_status(
    layer: QgsVectorLayer, status: str, source_files: Sequence[str], feedback=None
) -> int:
    """Set ``record_status`` for every row of the given source file(s)."""
    if status not in RECORD_STATUSES:
        raise RuntimeError(f"Unknown record status '{status}'.")
    source_field = source_field_for(layer)
    if source_field is None:
        raise RuntimeError("Layer has no provenance (source_file) column.")
    ensure_status_field(layer)
    wanted = set(source_files)
    request = QgsFeatureRequest().setFlags(_no_geometry_flag())
    request.setSubsetOfAttributes([source_field], layer.fields())
    changes: Dict[int, str] = {}
    total = _estimated_count(layer)
    for scanned, feature in enumerate(layer.getFeatures(request), 1):
        if scanned % _BATCH_SIZE == 0:
            if _canceled(feedback):
                return 0
            _report(feedback, scanned, total)
        if _wanted(feature[source_field], wanted):
            changes[feature.id()] = status
    return apply_status(layer, changes, feedback=feedback)


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
    arr = np.asarray(epochs, dtype=float)
    clean = np.sort(arr[np.isfinite(arr)])
    if clean.size < 2:
        return []
    breaks = np.nonzero(np.diff(clean) > threshold_s)[0]
    return [(float(clean[i]), float(clean[i + 1])) for i in breaks.tolist()]


def gap_index_for_epochs(
    epochs: Sequence[float], gaps: Sequence[Tuple[float, float]]
) -> np.ndarray:
    """Index (into ``gaps``) of the gap each epoch falls strictly inside, or -1.

    ``np.searchsorted`` on the gap starts, so the cost is O(n log g) instead
    of the O(n * g) of testing every epoch against every gap - the difference
    between a sub-second and a multi-second click on a million-row layer.
    """
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
    indices = gap_index_for_epochs(epochs, gaps)
    hits = indices[indices >= 0]
    return np.bincount(hits, minlength=len(gaps)).tolist()


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
