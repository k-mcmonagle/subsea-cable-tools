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

from datetime import timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple

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
    """A stripped string for an attribute value; QVariant/None nulls become ''."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.upper() == "NULL" else text


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

    def flush():
        if not changes:
            return
        if not provider.changeAttributeValues(dict(changes)):
            raise RuntimeError(
                f"Provider rejected the ISO_Time update: {provider.error().summary()}"
            )
        changes.clear()

    for feature in layer.getFeatures(request):
        if feedback is not None and feedback.isCanceled():
            break
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
    from datetime import datetime

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
    for feature in layer.getFeatures(request):
        if feedback is not None and feedback.isCanceled():
            return 0
        if source_field and wanted is not None and not _wanted(feature[source_field], wanted):
            continue
        key = tuple(
            "" if fields.indexOf(f) < 0 else _text(feature[f]) for f in key_fields
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
    doomed = [
        f.id()
        for f in layer.getFeatures(request)
        if _wanted(f[source_field], wanted)
        and not (feedback is not None and feedback.isCanceled())
    ]
    if feedback is not None and feedback.isCanceled():
        return 0
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


def apply_status(layer: QgsVectorLayer, fid_to_status: Dict[int, str]) -> int:
    """Set ``record_status`` per feature id (batched). Returns rows changed."""
    if not fid_to_status:
        return 0
    idx = ensure_status_field(layer)
    provider = layer.dataProvider()
    items = list(fid_to_status.items())
    for start in range(0, len(items), _BATCH_SIZE):
        batch = {fid: {idx: status} for fid, status in items[start:start + _BATCH_SIZE]}
        if not provider.changeAttributeValues(batch):
            raise RuntimeError(
                f"Provider rejected the status update: {provider.error().summary()}"
            )
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
    for feature in layer.getFeatures(request):
        if feedback is not None and feedback.isCanceled():
            return 0
        if _wanted(feature[source_field], wanted):
            changes[feature.id()] = status
    return apply_status(layer, changes)


# ---------------------------------------------------------------------------
# Gap analysis + gap fill (pure epoch math; the UI supplies the arrays)
# ---------------------------------------------------------------------------
def find_gaps_in_epochs(
    epochs: Sequence[float], threshold_s: float
) -> List[Tuple[float, float]]:
    """Gaps (as ``(start, end)`` epoch pairs) where consecutive sorted samples
    are more than ``threshold_s`` seconds apart. Non-finite values are ignored.
    """
    clean = sorted(t for t in epochs if t == t)  # drop NaN
    gaps: List[Tuple[float, float]] = []
    for previous, current in zip(clean, clean[1:]):
        if current - previous > threshold_s:
            gaps.append((previous, current))
    return gaps


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
    return [
        STATUS_ACTIVE if epoch_in_gaps(t, gaps) else STATUS_STANDBY
        for t in secondary_epochs
    ]


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
