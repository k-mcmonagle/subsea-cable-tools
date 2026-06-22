# cable_lay_parsers.py
# -*- coding: utf-8 -*-
"""
Shared helpers for the Cable Lay Data Import algorithms.

Pure-Python parsing utilities (only ``re``, ``csv``, ``datetime`` plus QGIS
core) used by the cable-lay importers so each algorithm does not re-implement
coordinate / time parsing, type inference, or the append-and-deduplicate merge.

No third-party dependencies (no pandas / shapely / geopandas).

Row representation
------------------
Importers build a list of "rows". Each row is a plain ``dict`` of attribute
name -> value. Geometry, when present, is carried as a WKT string stored under
the reserved key :data:`WKT_KEY`; it is popped before the attributes are written
to a feature. This keeps merge/deduplication trivial (everything is a dict) and
makes appending to an existing layer easy (read ``geometry.asWkt()``).
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_LONG_LONG, FIELD_TYPE_STRING

WGS84 = "EPSG:4326"

# Reserved row key holding the WKT geometry string (or None for no geometry).
WKT_KEY = "__wkt__"

# Degrees + decimal-minutes, hemisphere optionally separated by whitespace.
# Matches "17 09.7399N" and "17 09.7399 N" (the latter occurs when whitespace
# logs split a coordinate into three tokens that are re-joined with spaces).
DMS_PATTERN = re.compile(r"^\s*(\d{1,3})\s+([\d.]+)\s*([NSEW])\s*$", re.IGNORECASE)

# Day-count + time of day, e.g. "12,14:23:45".
DAY_TIME_PATTERN = re.compile(r"^\s*(\d+)\s*,\s*(\d{1,2}):(\d{2}):(\d{2})\s*$")

NULL_LIKE = {"", "null", "na", "n/a", "none", "-"}


# ---------------------------------------------------------------------------
# Coordinate / time parsing
# ---------------------------------------------------------------------------
def parse_dms_to_dd(value) -> Optional[float]:
    """Convert a degrees / decimal-minutes string to decimal degrees.

    Returns ``None`` when the value does not look like a coordinate.
    """
    if value is None:
        return None
    match = DMS_PATTERN.match(str(value))
    if not match:
        return None
    degrees, minutes, hemi = match.groups()
    decimal = float(degrees) + float(minutes) / 60.0
    if hemi.upper() in ("S", "W"):
        decimal = -decimal
    return decimal


def parse_day_time(value, start_date_str: str) -> Optional[datetime]:
    """Convert a ``"day,HH:MM:SS"`` value plus a start date to a datetime.

    ``start_date_str`` is the calendar date of day-count 1 (``YYYY-MM-DD``).
    Returns ``None`` when either input is malformed.
    """
    if not start_date_str:
        return None
    try:
        base_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    match = DAY_TIME_PATTERN.match(str(value)) if value is not None else None
    if not match:
        return None
    days, hour, minute, second = (int(group) for group in match.groups())
    return base_date.replace(hour=hour, minute=minute, second=second) + timedelta(days=days - 1)


def iso_str(dt: Optional[datetime]) -> Optional[str]:
    """Format a datetime as an ISO-8601 string for storage (or ``None``)."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def looks_like_day_time(value) -> bool:
    """True when ``value`` matches the day-count + time pattern."""
    return bool(DAY_TIME_PATTERN.match(str(value))) if value is not None else False


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def read_lines(path: str) -> List[str]:
    """Read all lines from a text file, tolerating non-UTF-8 survey exports."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.readlines()
        except UnicodeDecodeError:
            continue
    # Last resort: replace undecodable bytes so import still proceeds.
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.readlines()


def tokenize(line: str) -> List[str]:
    """Split a whitespace-delimited line into non-empty tokens."""
    return [tok for tok in line.strip().split() if tok]


def is_float_like(token: str) -> bool:
    """True when ``token`` parses as a float (used to find label boundaries)."""
    try:
        float(token)
        return True
    except (TypeError, ValueError):
        return False


def normalize_column_name(value) -> str:
    """Strip a BOM and collapse internal whitespace in a header cell."""
    text = "" if value is None else str(value)
    text = text.replace("﻿", "").strip()
    return re.sub(r"\s+", " ", text)


def read_csv_rows(path: str, delimiter: Optional[str] = None) -> Tuple[List[List[str]], str]:
    """Read a delimited file into a list of rows, sniffing the delimiter if needed.

    Returns ``(rows, delimiter)`` where ``rows`` includes every line (blank rows
    become empty lists); callers decide which header/units rows to skip.
    """
    text_lines = read_lines(path)
    if delimiter is None:
        delimiter = sniff_delimiter("".join(text_lines[:20]))
    rows = list(csv.reader(text_lines, delimiter=delimiter))
    return rows, delimiter


def non_blank_rows(rows: List[List[str]]) -> List[List[str]]:
    """Drop rows that are entirely empty/whitespace."""
    return [row for row in rows if any(str(cell).strip() for cell in row)]


def sniff_delimiter(sample: str, default: str = ",") -> str:
    """Best-effort delimiter detection for CSV/TSV exports."""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        return dialect.delimiter
    except Exception:
        if "\t" in sample and "," not in sample:
            return "\t"
        return default


# ---------------------------------------------------------------------------
# Type inference for CSV-style importers
# ---------------------------------------------------------------------------
def _clean_numeric(text: str) -> Tuple[str, bool]:
    """Normalise a value for numeric testing; returns (clean, is_negative)."""
    clean = str(text).strip().replace(",", ".")
    clean = clean.rstrip("%").replace(" ", "")
    is_negative = clean.startswith("-")
    if is_negative:
        clean = clean[1:]
    return clean, is_negative


def infer_column_types(
    records: Sequence[Dict[str, str]],
    text_columns: Sequence[str] = (),
    sample_size: int = 100,
) -> Dict[str, str]:
    """Infer ``'int'`` / ``'float'`` / ``'str'`` for each column of dict records.

    Mirrors the heuristic used by the existing cable-lay CSV importer: a column
    is numeric when >=80% of its non-null sampled values parse as numbers, and
    decimal when any of those carry a fractional part.
    """
    if not records:
        return {}
    text_set = {c for c in text_columns}
    columns = list(records[0].keys())
    sample = records[:sample_size] if len(records) > sample_size else records
    types: Dict[str, str] = {}

    for col in columns:
        if col in text_set:
            types[col] = "str"
            continue
        numeric_count = 0
        decimal_count = 0
        total = 0
        is_str = False
        for rec in sample:
            raw = str(rec.get(col, "")).strip()
            if raw.lower() in NULL_LIKE:
                continue
            total += 1
            clean, is_negative = _clean_numeric(raw)
            if any(c.isalpha() and c.lower() != "e" for c in clean):
                is_str = True
                break
            signed = ("-" + clean) if is_negative else clean
            try:
                fval = float(signed)
            except (TypeError, ValueError):
                is_str = True
                break
            numeric_count += 1
            if ("." in clean and clean.count(".") == 1) or "e" in clean.lower() or not fval.is_integer():
                decimal_count += 1
        if is_str or total == 0 or numeric_count < total * 0.8:
            types[col] = "str"
        elif decimal_count > 0:
            types[col] = "float"
        else:
            types[col] = "int"
    return types


def coerce_value(value, type_str: str):
    """Coerce a raw string to the inferred type, returning ``None`` for nulls."""
    if value is None or str(value).strip().lower() in NULL_LIKE:
        return None
    if type_str == "int":
        clean, is_negative = _clean_numeric(value)
        try:
            num = int(float(("-" + clean) if is_negative else clean))
            return num
        except (TypeError, ValueError):
            return None
    if type_str == "float":
        clean, is_negative = _clean_numeric(value)
        try:
            return float(("-" + clean) if is_negative else clean)
        except (TypeError, ValueError):
            return None
    return str(value).strip()


def qgis_field_for(type_str: str, name: str) -> QgsField:
    """Build a ``QgsField`` of the QGIS type matching an inferred ``type_str``."""
    if type_str == "int":
        return QgsField(name, FIELD_TYPE_LONG_LONG)
    if type_str == "float":
        return QgsField(name, FIELD_TYPE_DOUBLE)
    return QgsField(name, FIELD_TYPE_STRING)


def fields_from_specs(specs: Sequence[Tuple[str, str]]) -> QgsFields:
    """Build ``QgsFields`` from a list of ``(name, type_str)`` pairs."""
    fields = QgsFields()
    for name, type_str in specs:
        fields.append(qgis_field_for(type_str, name))
    return fields


# ---------------------------------------------------------------------------
# Append / merge support
# ---------------------------------------------------------------------------
def dedupe_key_for(layer_type: str) -> List[str]:
    """Per-type unique key used to drop duplicate rows on append.

    Keys mirror the original tool's merge behaviour. ``layer_type`` is one of:
    ``cable_lay``, ``event_logs``, ``slack_logs``, ``body_logs``,
    ``model_solutions``, ``as_laid``, ``plough_data``.
    """
    keys = {
        "cable_lay": ["ISO_Time", "source_file"],
        "event_logs": ["ISO_Time", "event_file"],
        "slack_logs": ["slack_file", "KP1", "KP2"],
        "body_logs": ["body_file", "Body_Label"],
        "model_solutions": ["ISO_Time", "source_file"],
        "as_laid": ["ISO_Time", "source_file"],
        "plough_data": ["ISO_Time", "source_file", "Record"],
    }
    return keys.get(layer_type, ["ISO_Time"])


def merge_and_dedupe(
    existing_rows: List[Dict],
    new_rows: List[Dict],
    key_fields: Sequence[str],
) -> Tuple[List[Dict], int]:
    """Concatenate rows and drop duplicates by ``key_fields`` (keep first).

    Returns ``(merged_rows, duplicates_removed)``.
    """
    merged: List[Dict] = []
    seen = set()
    duplicates = 0
    for row in list(existing_rows) + list(new_rows):
        key = tuple(_key_value(row.get(field)) for field in key_fields)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        merged.append(row)
    return merged, duplicates


def _key_value(value) -> str:
    if value is None:
        return ""
    return str(value)


def rows_from_source(source) -> Tuple[List[Dict], QgsFields]:
    """Read an existing feature source into rows + its field schema.

    Each feature becomes a row dict of attributes plus a :data:`WKT_KEY` entry
    holding the geometry WKT (or ``None``). Used when appending to an existing
    layer so new data merges into the same schema.
    """
    fields = source.fields()
    names = [field.name() for field in fields]
    rows: List[Dict] = []
    for feature in source.getFeatures():
        row = {name: feature[name] for name in names}
        geom = feature.geometry()
        row[WKT_KEY] = geom.asWkt() if geom is not None and not geom.isEmpty() else None
        rows.append(row)
    return rows, fields


def union_fields(existing: Optional[QgsFields], new_fields: QgsFields) -> QgsFields:
    """Return ``existing`` fields plus any ``new_fields`` not already present.

    When ``existing`` is ``None`` the new fields are returned unchanged. The
    existing layer's field definitions win on name collisions so an append does
    not change the stored schema.
    """
    if existing is None:
        return new_fields
    combined = QgsFields()
    present = set()
    for field in existing:
        combined.append(field)
        present.add(field.name())
    for field in new_fields:
        if field.name() not in present:
            combined.append(field)
            present.add(field.name())
    return combined


def write_rows(sink, fields: QgsFields, rows: List[Dict], feedback=None) -> int:
    """Write ``rows`` to a feature sink using ``fields`` as the schema.

    The reserved :data:`WKT_KEY` entry, when present and non-null, becomes the
    feature geometry. Returns the number of features written.
    """
    written = 0
    for row in rows:
        feature = QgsFeature(fields)
        for index, field in enumerate(fields):
            feature.setAttribute(index, row.get(field.name()))
        wkt = row.get(WKT_KEY)
        if wkt:
            geom = QgsGeometry.fromWkt(wkt)
            if geom is not None and not geom.isEmpty():
                feature.setGeometry(geom)
        sink.addFeature(feature)
        written += 1
    return written


# ---------------------------------------------------------------------------
# GeoPackage I/O
# ---------------------------------------------------------------------------
def gpkg_layer_uri(gpkg_path: str, layer_name: str) -> str:
    """OGR URI for a single layer within a GeoPackage."""
    return f"{gpkg_path}|layername={layer_name}"


def prefixed_layer_name(gpkg_path: str, layer_type: str) -> str:
    """Physical layer name for a canonical type, prefixed with the file stem.

    e.g. ``ProjectX.gpkg`` + ``cable_lay`` -> ``ProjectX_cable_lay``. The stem is
    sanitised to alphanumerics/underscores so it is a safe GeoPackage table name.
    """
    stem = os.path.splitext(os.path.basename(gpkg_path))[0]
    stem = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_")
    return f"{stem}_{layer_type}" if stem else layer_type


def open_gpkg_layer(gpkg_path: str, layer_name: str) -> Optional[QgsVectorLayer]:
    """Open an existing GeoPackage layer, or return ``None`` if it is absent."""
    if not os.path.exists(gpkg_path):
        return None
    layer = QgsVectorLayer(gpkg_layer_uri(gpkg_path, layer_name), layer_name, "ogr")
    return layer if layer.isValid() else None


def write_layer_to_gpkg(
    gpkg_path: str,
    layer_name: str,
    fields: QgsFields,
    wkb_type,
    rows: List[Dict],
    transform_context,
) -> int:
    """Create or overwrite ``layer_name`` in ``gpkg_path`` with ``rows``.

    Other layers in the GeoPackage are preserved (the file is created if it does
    not exist, otherwise only this layer is replaced). Returns the feature count.
    Raises ``RuntimeError`` if the writer reports an error.
    """
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = layer_name
    options.fileEncoding = "UTF-8"
    options.actionOnExistingFile = (
        QgsVectorFileWriter.CreateOrOverwriteLayer
        if os.path.exists(gpkg_path)
        else QgsVectorFileWriter.CreateOrOverwriteFile
    )
    writer = QgsVectorFileWriter.create(
        gpkg_path,
        fields,
        wkb_type,
        QgsCoordinateReferenceSystem(WGS84),
        transform_context,
        options,
    )
    if writer.hasError() != QgsVectorFileWriter.NoError:
        message = writer.errorMessage()
        del writer
        raise RuntimeError(f"Could not write layer '{layer_name}' to {gpkg_path}: {message}")
    written = write_rows(writer, fields, rows)
    del writer  # flush to disk
    return written


# ---------------------------------------------------------------------------
# Canonical layer schemas (used by the "Create Cable Lay GeoPackage" setup tool
# and by the fixed-schema importers). Field specs are (name, type_str); the
# CSV-style importers extend these with any extra columns found in the files.
# ---------------------------------------------------------------------------
SLACK_FIELD_SPECS: List[Tuple[str, str]] = [
    ("KP1", "float"), ("KP2", "float"),
    ("lat1", "str"), ("lon1", "str"), ("lat2", "str"), ("lon2", "str"),
    ("Cable_off_path1", "float"), ("Cable_off_path2", "float"),
    ("Depth1", "float"), ("Depth2", "float"),
    ("Seabed_slack", "float"), ("Ship_slack", "float"),
    ("Cable_and_Body_Labels", "str"), ("slack_file", "str"),
]

BODY_FIELD_SPECS: List[Tuple[str, str]] = [
    ("Body_Label", "str"), ("Touchdown_Cable_Dist_m", "float"),
    ("Touchdown_Latitude", "str"), ("Touchdown_Longitude", "str"),
    ("Water_Depth_m", "float"), ("Touchdown_KP_km", "float"),
    ("Td_Distance_Off_Path_m", "float"), ("Side", "str"),
    ("Avg_Seabed_Slack_pct", "float"), ("Deviation_From_Target_m", "float"),
    ("Inline_Offset_m", "float"), ("body_file", "str"),
]

# layer_name -> (wkb geometry type, core field specs)
CANONICAL_SCHEMAS = {
    "cable_lay": (
        QgsWkbTypes.Point,
        [("ISO_Time", "str"), ("Lat_dd", "float"), ("Lon_dd", "float"), ("source_file", "str")],
    ),
    "event_logs": (
        QgsWkbTypes.Point,
        [("ISO_Time", "str"), ("Lat_dd", "float"), ("Lon_dd", "float"), ("event_file", "str")],
    ),
    "slack_logs": (QgsWkbTypes.LineString, SLACK_FIELD_SPECS),
    "body_logs": (QgsWkbTypes.Point, BODY_FIELD_SPECS),
    "model_solutions": (
        QgsWkbTypes.Point,
        [
            ("ISO_Time", "str"), ("Lat_dd", "float"), ("Lon_dd", "float"),
            ("TD_Lat_dd", "float"), ("TD_Lon_dd", "float"), ("source_file", "str"),
        ],
    ),
    "as_laid": (
        QgsWkbTypes.Point,
        [("ISO_Time", "str"), ("Lat_dd", "float"), ("Lon_dd", "float"), ("source_file", "str")],
    ),
    "plough_data": (
        QgsWkbTypes.Point,
        [
            ("ISO_Time", "str"), ("Record", "int"), ("Time", "str"),
            ("Latitude", "str"), ("Longitude", "str"),
            ("Lat_dd", "float"), ("Lon_dd", "float"), ("source_file", "str"),
        ],
    ),
}
