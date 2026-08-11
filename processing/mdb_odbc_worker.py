# -*- coding: utf-8 -*-
"""Worker process for reading GeoMedia MDB files.

Two backends, tried in order:

1. Pure Python (preferred): the ``access_parser`` package vendored in the
   plugin's ``lib/`` folder reads Jet 3/4 ``.mdb`` tables directly. No ODBC,
   no Microsoft Access driver, works out of the box on any platform.
2. ODBC fallback: ``pyodbc`` + the Microsoft Access ODBC driver, kept for
   files the pure reader cannot handle (password-protected/unusual variants).

Why this is a subprocess at all:
- Some ODBC/Access drivers can hard-crash the host process (QGIS) with no
  Python exception; running here contains those crashes. The pure-Python
  backend cannot crash QGIS, but the isolation also caps memory usage for
  very large tables.

This script is invoked by the QGIS processing algorithm and communicates via
stdout/stderr.

Modes:
- list: prints a JSON discovery envelope (feature tables, non-spatial tables)
- export: writes GeoJSON FeatureCollections for a single table and prints a
  structured per-table result, even when nothing could be exported

Diagnostics never include record values, coordinates or BLOB contents; table
and field names only.

This script intentionally does NOT import qgis.*.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import traceback

try:
    import pyodbc
except Exception:
    pyodbc = None


def _plugin_lib_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")


try:
    from access_parser import AccessParser
except Exception:
    _lib_dir = _plugin_lib_dir()
    if os.path.isdir(_lib_dir) and _lib_dir not in sys.path:
        sys.path.insert(0, _lib_dir)
    try:
        from access_parser import AccessParser
    except Exception:
        AccessParser = None

try:  # Running as a package module (QGIS, tests).
    from .geomedia_blob import (
        GeomediaGeometry,
        coerce_blob_bytes,
        decode_geometry_blob,
        is_closed_ring,
        iter_vertices,
        to_geojson_geometry,
    )
except ImportError:  # Running as a bare subprocess script.
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from geomedia_blob import (  # type: ignore[no-redef]
        GeomediaGeometry,
        coerce_blob_bytes,
        decode_geometry_blob,
        is_closed_ring,
        iter_vertices,
        to_geojson_geometry,
    )

ACCESS_ODBC_DRIVER_NAME = "Microsoft Access Driver (*.mdb, *.accdb)"

_GFEATURES_NAME_COLS = {"FEATURENAME", "FEATURECLASSNAME", "NAME"}

#: GeoMedia / Access housekeeping tables that must never become map layers.
_METADATA_TABLES = {
    "GFEATURES", "GFIELDMAPPING", "GALIASTABLE", "GCOORDSYSTEM", "GPARAMETERS",
    "GPICKLISTS", "GINDEXCOLUMNS", "GEOMETRYPROPERTIES", "ATTRIBUTEPROPERTIES",
    "FIELDLOOKUP", "MODIFICATIONLOG", "GEXCLUSIVELOCK", "GOPTIONS", "GLAYERS",
    "GMETADATA", "GTABLECATALOG", "GQUEUE", "GSERVERINFO", "GDATABASEPROPERTIES",
}
_METADATA_PREFIXES = ("MSYS", "USYS", "GDO")

#: Companion attribute/annotation tables that belong to a feature class.
_COMPANION_SUFFIXES = ("_NAME", "_TEXT", "_ANNOTATION", "_LABEL")

#: Field names GeoMedia commonly uses for the geometry BLOB itself.
_GEOMETRY_FIELD_HINTS = (
    "geometry", "geom", "coordgeocode",
)

#: Explicit, ordered coordinate-pair aliases. Pairs are never mixed across
#: families, so a Depth column can never be taken as a Y ordinate.
_COORDINATE_PAIRS = (
    ("easting", "northing"),
    ("east", "north"),
    ("x_coord", "y_coord"),
    ("coord_x", "coord_y"),
    ("xcoord", "ycoord"),
    ("x", "y"),
    ("longitude", "latitude"),
    ("long", "lat"),
    ("lon", "lat"),
    ("lng", "lat"),
)

#: Explicit vertical aliases used only for the Z ordinate.
_Z_FIELDS = (
    "z", "depth", "water_depth", "waterdepth", "depth_m", "elevation", "elev",
    "height", "z_value", "zvalue",
)

_SPLIT_FILE_SUFFIXES = {
    "Point": "_points.geojson",
    "LineString": "_lines.geojson",
    "Polygon": "_polygons.geojson",
    "MultiPoint": "_multipoints.geojson",
    "MultiLineString": "_multilines.geojson",
    "MultiPolygon": "_multipolygons.geojson",
    "GeometryCollection": "_collections.geojson",
}

_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/)[^\s'\"]*")
_LONG_LITERAL_PATTERN = re.compile(r"b?['\"][^'\"]{40,}['\"]")


def _odbc_braced_value(value):
    return "{" + os.fspath(value).replace("}", "}}") + "}"


def _normalize_mdb_path(mdb_path):
    path = os.path.abspath(os.path.normpath(os.fspath(mdb_path).strip().strip('"')))
    return path.replace("/", "\\") if os.name == "nt" else path


def _access_connection_string(mdb_path):
    normalized = _normalize_mdb_path(mdb_path)
    return (
        "Driver="
        + _odbc_braced_value(ACCESS_ODBC_DRIVER_NAME)
        + ";DBQ="
        + normalized
        + ";"
    )


def _quote_access_identifier(identifier):
    text = str(identifier)
    if not text:
        raise ValueError("Access identifier is empty")
    if any(ch in text for ch in "[]"):
        raise ValueError(f"Access identifier contains brackets: {text!r}")
    if any(ord(ch) < 32 for ch in text):
        raise ValueError(f"Access identifier contains control characters: {text!r}")
    return "[" + text + "]"


def _get_column_names(cursor, table_name):
    sql = "SELECT * FROM " + _quote_access_identifier(table_name) + " WHERE 1=0"  # nosec B608 identifier validated & bracket-quoted
    cursor.execute(sql)
    return [desc[0] for desc in cursor.description]


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

def sanitise_diagnostic(message, limit=300):
    """Strip paths and long literals from a message destined for the log."""
    text = "" if message is None else str(message)
    text = _LONG_LITERAL_PATTERN.sub("<omitted>", text)
    text = _PATH_PATTERN.sub("<path>", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def make_table_result(table, status, **overrides):
    """Build the structured result contract shared by every export attempt."""
    result = {
        "table": sanitise_diagnostic(table, 200),
        "status": status,
        "row_count": 0,
        "non_null_geometry_count": 0,
        "blob_decoded_count": 0,
        "secondary_blob_decoded_count": 0,
        "xy_fallback_count": 0,
        "invalid_geometry_count": 0,
        "geometry_types_found": [],
        "geometry_fields_used": [],
        "outputs": {},
        "message": "",
    }
    result.update(overrides)
    result["message"] = sanitise_diagnostic(result.get("message"))
    return result


# --------------------------------------------------------------------------
# Field-name matching
# --------------------------------------------------------------------------

def normalise_field_name(name):
    """Case-insensitive, whitespace-insensitive field key."""
    text = "" if name is None else str(name)
    return "_".join(text.strip().split()).casefold()


def _field_lookup(col_names):
    lookup = {}
    for name in col_names:
        lookup.setdefault(normalise_field_name(name), name)
    return lookup


def find_candidate_coordinate_pair(col_names):
    """Return ``(x_field, y_field)`` for an explicitly allowed alias pair."""
    lookup = _field_lookup(col_names)
    for x_alias, y_alias in _COORDINATE_PAIRS:
        if x_alias in lookup and y_alias in lookup:
            return lookup[x_alias], lookup[y_alias]
    return None


def find_candidate_z_field(col_names, exclude=()):
    """Return an explicit vertical field, or ``None``."""
    excluded = {normalise_field_name(name) for name in exclude}
    lookup = _field_lookup(col_names)
    for alias in _Z_FIELDS:
        if alias in lookup and alias not in excluded:
            return lookup[alias]
    return None


def find_candidate_geometry_fields(col_names):
    """Return fields whose names look like GeoMedia geometry BLOB columns."""
    return [
        name for name in col_names
        if any(hint in normalise_field_name(name) for hint in _GEOMETRY_FIELD_HINTS)
    ]


def _finite_number(value):
    """Return a finite float for a credible numeric value, else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def build_fallback_point(row_values, x_field, y_field, z_field=None):
    """Build a GeoJSON point from an explicit coordinate pair.

    Returns ``(geometry, z)`` or ``None``. ``z`` is only populated when an
    explicit vertical field holds a finite number.
    """
    if not x_field or not y_field:
        return None
    x = _finite_number(row_values.get(x_field))
    y = _finite_number(row_values.get(y_field))
    if x is None or y is None:
        return None
    z = _finite_number(row_values.get(z_field)) if z_field else None
    if z is None:
        return {"type": "Point", "coordinates": [x, y]}, None
    return {"type": "Point", "coordinates": [x, y, z]}, z


# --------------------------------------------------------------------------
# Table classification
# --------------------------------------------------------------------------

def classify_table(table_name, col_names=(), in_gfeatures=False):
    """Classify a physical Access table for discovery and reporting."""
    upper = str(table_name).upper()
    if upper in _METADATA_TABLES or upper.startswith(_METADATA_PREFIXES):
        return "metadata"
    if in_gfeatures:
        return "feature"
    if upper.endswith(_COMPANION_SUFFIXES):
        return "companion"
    if find_candidate_geometry_fields(col_names) or find_candidate_coordinate_pair(col_names):
        return "spatial_candidate"
    return "non_spatial"


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def parse_blob(blob):
    """Legacy flat-vertex decoder kept for callers that predate the structured API."""
    geometry = decode_geometry_blob(blob)
    if geometry is None:
        return None
    vertices = list(iter_vertices(geometry))
    return vertices or None


def is_closed(vertices, tol=1e-6):
    return is_closed_ring(vertices, tol)


def output_kind_for_geometry(decoded, geometry_type_code=None):
    """Decide the output layer type for a decoded BLOB.

    Simple single-ring shapes keep the historic metadata-led classification so
    existing line/polygon imports are unchanged. Structured shapes the old
    reader could not decode at all (holes, collections) keep their own type.
    """
    if decoded is None:
        return None
    if decoded.kind in ("Point", "LineString", "Polygon") and len(decoded.rings) <= 1:
        return _infer_geom_type(list(iter_vertices(decoded)), geometry_type_code)
    return decoded.kind


def geojson_for_kind(kind, decoded):
    """Render a decoded geometry as ``kind``, reshaping simple types if needed."""
    if decoded is None or not kind:
        return None
    if kind == decoded.kind:
        return to_geojson_geometry(decoded)

    vertices = list(iter_vertices(decoded))
    if not vertices:
        return None
    if kind == "Point":
        return {"type": "Point", "coordinates": [vertices[0][0], vertices[0][1]]}
    if kind == "MultiPoint":
        return {"type": "MultiPoint", "coordinates": [[v[0], v[1]] for v in vertices]}
    if kind in ("LineString", "Polygon"):
        return to_geojson_geometry(GeomediaGeometry(kind, (tuple(vertices),), ()))
    return None


# --------------------------------------------------------------------------
# Backend dispatch
# --------------------------------------------------------------------------

def _no_backend_message():
    return (
        "No MDB reader is available. The plugin's bundled pure-Python reader "
        "(lib/access_parser) failed to import — try reinstalling the plugin. "
        "Alternatively install pyodbc and the Microsoft Access ODBC driver "
        "into the QGIS Python environment to use the ODBC fallback."
    )


def _run_with_backends(pure_fn, odbc_fn):
    """Run the pure-Python backend first, falling back to ODBC."""
    errors = []
    if AccessParser is not None:
        try:
            return pure_fn()
        except FileNotFoundError:
            raise
        except Exception as exc:
            errors.append(f"bundled pure-Python reader: {exc}")
    if pyodbc is not None:
        try:
            return odbc_fn()
        except FileNotFoundError:
            raise
        except Exception as exc:
            errors.append(f"ODBC reader: {exc}")
    if not errors:
        raise RuntimeError(_no_backend_message())
    if AccessParser is not None and pyodbc is None:
        errors.append(
            "pyodbc is not installed, so the ODBC fallback was unavailable")
    raise RuntimeError("Could not read the MDB. " + "; ".join(errors))


# --------------------------------------------------------------------------
# Pure-Python backend (bundled access_parser)
# --------------------------------------------------------------------------

class _PureTables:
    """Read-only table access via the bundled pure-Python Jet parser."""

    def __init__(self, mdb_path):
        normalized = _normalize_mdb_path(mdb_path)
        if not os.path.isfile(normalized):
            raise FileNotFoundError(f"MDB file does not exist: {normalized}")
        self.db = AccessParser(normalized)
        self._names = {str(name).upper(): str(name) for name in self.db.catalog}

    def find_table(self, upper_name):
        return self._names.get(str(upper_name).upper())

    def table_names(self):
        return list(self._names.values())

    def describe(self, table_name):
        """Return ``(col_names, row_count)`` without parsing any record data."""
        actual = self.find_table(table_name)
        if not actual:
            raise RuntimeError(f"Table not found in MDB: {table_name}")
        table = self.db.get_table(actual)
        if table is None:
            raise RuntimeError(f"Table definition unavailable: {table_name}")
        columns = [table.columns[key] for key in sorted(table.columns)]
        col_names = [str(column.col_name_str) for column in columns]
        try:
            row_count = int(table.table_header.number_of_rows)
        except Exception:
            row_count = None
        return col_names, row_count

    def read(self, table_name):
        """Return (col_names, rows) for a table; rows are tuples."""
        actual = self.find_table(table_name)
        if not actual:
            raise RuntimeError(f"Table not found in MDB: {table_name}")
        data = self.db.parse_table(actual)
        col_names = list(data)
        columns = [data[name] for name in col_names]
        rows = list(zip(*columns)) if columns else []
        return col_names, rows


def _feature_tables_from_gfeatures(col_names, rows):
    """Interpret GFeatures metadata rows shared by both backends."""
    if not col_names:
        return {}
    feature_name_col = next(
        (col for col in col_names if str(col).upper() in _GFEATURES_NAME_COLS),
        col_names[0])
    geom_field_col = next(
        (col for col in col_names if str(col).upper() == "PRIMARYGEOMETRYFIELDNAME"), None)
    geom_type_col = next(
        (col for col in col_names if str(col).upper() == "GEOMETRYTYPE"), None)
    if not geom_field_col or not geom_type_col:
        return {}
    name_i = col_names.index(feature_name_col)
    field_i = col_names.index(geom_field_col)
    type_i = col_names.index(geom_type_col)
    out = {}
    for row in rows:
        try:
            geometry_type = int(row[type_i])
        except (TypeError, ValueError):
            continue
        if geometry_type == 33:
            continue
        if row[name_i] is None or row[field_i] is None:
            continue
        out[str(row[name_i])] = {
            "geom_field_name": str(row[field_i]),
            "geometry_type_code": geometry_type,
        }
    return out


def _list_feature_tables_pure(mdb_path):
    tables = _PureTables(mdb_path)
    gfeatures = tables.find_table("GFEATURES")
    if not gfeatures:
        return {}
    col_names, rows = tables.read(gfeatures)
    return _feature_tables_from_gfeatures(col_names, rows)


def _inventory_pure(mdb_path, budget_seconds=None):
    tables = _PureTables(mdb_path)
    gfeatures = tables.find_table("GFEATURES")
    registered = {}
    if gfeatures:
        col_names, rows = tables.read(gfeatures)
        registered = _feature_tables_from_gfeatures(col_names, rows)
    registered_upper = {name.upper(): name for name in registered}

    deadline = None if not budget_seconds else time.monotonic() + budget_seconds
    inventory = []
    for physical in tables.table_names():
        in_gfeatures = physical.upper() in registered_upper
        entry = {
            "table": physical,
            "in_gfeatures": in_gfeatures,
            "columns": [],
            "row_count": None,
            "error": "",
        }
        # Describing a table costs a full table-definition parse, so skip the
        # ones GFeatures already answers for and stop once the budget is spent.
        if in_gfeatures:
            inventory.append(entry)
            continue
        if deadline is not None and time.monotonic() > deadline:
            entry["error"] = "schema inspection time budget exhausted"
            inventory.append(entry)
            continue
        try:
            entry["columns"], entry["row_count"] = tables.describe(physical)
        except Exception as exc:
            entry["error"] = sanitise_diagnostic(exc)
        inventory.append(entry)
    return registered, registered_upper, inventory


def _export_rows_pure(mdb_path, table_name, geom_field_name):
    # Null-geometry rows are kept: they are needed for accurate row counts and
    # for the coordinate-pair fallback.
    tables = _PureTables(mdb_path)
    return tables.read(table_name)


# --------------------------------------------------------------------------
# ODBC backend (pyodbc + Microsoft Access driver)
# --------------------------------------------------------------------------

def _require_pyodbc_and_driver():
    if pyodbc is None:
        raise RuntimeError(
            "pyodbc import failed. Install pyodbc into the QGIS Python environment."
        )

    # Driver enumeration itself can fail in some broken environments.
    try:
        drivers = [d.strip() for d in pyodbc.drivers()]
    except Exception:
        drivers = []

    if drivers and ACCESS_ODBC_DRIVER_NAME not in drivers:
        raise RuntimeError(
            "Microsoft Access ODBC driver not found. "
            f"Expected '{ACCESS_ODBC_DRIVER_NAME}'."
        )


def _connect(mdb_path, timeout_seconds=10):
    _require_pyodbc_and_driver()
    normalized = _normalize_mdb_path(mdb_path)
    if not os.path.isfile(normalized):
        raise FileNotFoundError(f"MDB file does not exist: {normalized}")
    conn_str = _access_connection_string(normalized)
    return pyodbc.connect(conn_str, timeout=timeout_seconds)


def _list_feature_tables_odbc(mdb_path):
    with _connect(mdb_path) as conn:
        cur = conn.cursor()

        gfeatures_table = None
        for ti in cur.tables():
            if str(ti.table_name).upper() == "GFEATURES":
                gfeatures_table = ti.table_name
                break

        if not gfeatures_table:
            return {}

        col_names = _get_column_names(cur, gfeatures_table)
        if not col_names:
            return {}

        # Identifiers are validated and bracket-quoted by
        # _quote_access_identifier(); Access ODBC cannot parameterise
        # identifiers, so they are safely interpolated.
        sql = (
            "SELECT * FROM "  # nosec B608
            + _quote_access_identifier(gfeatures_table)
        )
        cur.execute(sql)
        rows = [tuple(row) for row in cur.fetchall()]
        return _feature_tables_from_gfeatures(col_names, rows)


def _inventory_odbc(mdb_path, budget_seconds=None):
    with _connect(mdb_path) as conn:
        cur = conn.cursor()
        physical_tables = [
            str(ti.table_name) for ti in cur.tables()
            if str(getattr(ti, "table_type", "TABLE")).upper() == "TABLE"
        ]

        registered = {}
        gfeatures_table = next(
            (name for name in physical_tables if name.upper() == "GFEATURES"), None)
        if gfeatures_table:
            col_names = _get_column_names(cur, gfeatures_table)
            if col_names:
                cur.execute(
                    "SELECT * FROM " + _quote_access_identifier(gfeatures_table))  # nosec B608
                registered = _feature_tables_from_gfeatures(
                    col_names, [tuple(row) for row in cur.fetchall()])
        registered_upper = {name.upper(): name for name in registered}

        deadline = None if not budget_seconds else time.monotonic() + budget_seconds
        inventory = []
        for physical in physical_tables:
            in_gfeatures = physical.upper() in registered_upper
            entry = {
                "table": physical,
                "in_gfeatures": in_gfeatures,
                "columns": [],
                "row_count": None,
                "error": "",
            }
            if in_gfeatures:
                inventory.append(entry)
                continue
            if deadline is not None and time.monotonic() > deadline:
                entry["error"] = "schema inspection time budget exhausted"
                inventory.append(entry)
                continue
            try:
                entry["columns"] = _get_column_names(cur, physical)
                cur.execute(
                    "SELECT COUNT(*) FROM " + _quote_access_identifier(physical))  # nosec B608
                entry["row_count"] = int(cur.fetchone()[0])
            except Exception as exc:
                entry["error"] = sanitise_diagnostic(exc)
            inventory.append(entry)
    return registered, registered_upper, inventory


def _coerce_json_value(v):
    if v is None:
        return None

    # Backends may return Decimal, datetime, etc. Keep it robust.
    try:
        import datetime
        if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
            return v.isoformat()
    except Exception:
        pass

    if isinstance(v, (int, float, bool, str)):
        return v

    # bytes-like (excluding the geometry blob which we omit)
    if isinstance(v, (bytes, bytearray, memoryview)):
        # Avoid huge embedded blobs; represent as length.
        try:
            ln = len(v)
        except Exception:
            ln = None
        return f"<binary:{ln}>"

    try:
        return str(v)
    except Exception:
        return None


def _infer_geom_type(vertices, geometry_type_code=None):
    if not vertices:
        return None

    # Always trust actual shape first: a single coordinate is a point.
    # This avoids misclassifying valid point tables.
    if len(vertices) == 1:
        return "Point"

    # Preserve explicit polygon metadata for true polygon tables.
    if geometry_type_code == 2:
        return "Polygon"

    # GeoMedia metadata can mislabel bathy contour/track features as point-like
    # classes (e.g. code 3). Any multi-vertex feature should be line output.
    if geometry_type_code in {1, 3}:
        return "LineString"

    # Ambiguous GeoMedia type codes (e.g. 10) are prone to treating closed
    # contour lines as polygons. Prefer line output to avoid false polygons.
    return "LineString"


def _export_rows_odbc(mdb_path, table_name, geom_field_name):
    conn = _connect(mdb_path)
    cur = conn.cursor()
    # No WHERE clause: null-geometry rows are still needed for accurate row
    # counts and for the coordinate-pair fallback.
    sql = "SELECT * FROM " + _quote_access_identifier(table_name)  # nosec B608
    cur.execute(sql)
    col_names = [desc[0] for desc in cur.description]

    def rows():
        try:
            for row in cur:
                yield tuple(row)
        finally:
            conn.close()

    return col_names, rows()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def inventory_tables(mdb_path, budget_seconds=None):
    """Return ``(registered, registered_upper, inventory)`` for the database.

    ``inventory`` carries one entry per physical table with its column names
    and a row count taken from the table definition. No record values are read.
    """
    return _run_with_backends(
        lambda: _inventory_pure(mdb_path, budget_seconds),
        lambda: _inventory_odbc(mdb_path, budget_seconds),
    )


def schema_discovery_enabled():
    return os.environ.get("SUBSEA_MDB_SCHEMA_DISCOVERY", "0") in {"1", "true", "True"}


def schema_discovery_budget():
    try:
        return max(0.0, float(os.environ.get("SUBSEA_MDB_SCHEMA_BUDGET", "30")))
    except ValueError:
        return 30.0


def _tables_from_registered(registered):
    return {
        name: {
            "geom_field_name": meta.get("geom_field_name"),
            "geometry_type_code": meta.get("geometry_type_code"),
            "discovery": "gfeatures",
            "row_count": None,
            "classification": "feature",
        }
        for name, meta in registered.items()
    }


def discover_tables(mdb_path, include_schema_discovery=None, budget_seconds=None):
    """Build the discovery envelope returned by ``--mode list``.

    GFeatures is authoritative and is the only thing read by default: one
    metadata table. The secondary pass that inspects every physical table
    needs a full table-definition parse per table, which is far too slow to
    run on every import, so it is opt-in via SUBSEA_MDB_SCHEMA_DISCOVERY=1.
    """
    if include_schema_discovery is None:
        include_schema_discovery = schema_discovery_enabled()

    if not include_schema_discovery:
        return {
            "tables": _tables_from_registered(list_feature_tables(mdb_path)),
            "non_spatial": [],
            "schema_discovery": False,
        }

    if budget_seconds is None:
        budget_seconds = schema_discovery_budget()
    registered, registered_upper, inventory = inventory_tables(mdb_path, budget_seconds)

    tables = _tables_from_registered(registered)

    non_spatial = []
    for entry in inventory:
        name = entry["table"]
        columns = entry.get("columns") or []

        if entry.get("in_gfeatures"):
            known = tables.get(registered_upper.get(name.upper(), name))
            if known is not None and entry.get("row_count") is not None:
                known["row_count"] = entry.get("row_count")
            continue

        if entry.get("error") and not columns:
            continue

        classification = classify_table(name, columns, in_gfeatures=False)
        if classification == "metadata":
            continue

        if classification == "spatial_candidate":
            geometry_fields = find_candidate_geometry_fields(columns)
            pair = find_candidate_coordinate_pair(columns)
            tables[name] = {
                "geom_field_name": geometry_fields[0] if geometry_fields else None,
                "geometry_type_code": None,
                "discovery": "schema",
                "row_count": entry.get("row_count"),
                "classification": classification,
                "coordinate_pair": list(pair) if pair else None,
            }
            continue

        non_spatial.append({
            "table": name,
            "classification": classification,
            "row_count": entry.get("row_count"),
            "columns": columns,
            "reason": (
                "companion attribute/annotation table"
                if classification == "companion"
                else "no geometry field and no recognised coordinate pair"
            ),
        })

    return {"tables": tables, "non_spatial": non_spatial, "schema_discovery": True}


# --------------------------------------------------------------------------
# Shared GeoJSON writer
# --------------------------------------------------------------------------

class _GeoJsonSink:
    """Lazily opened GeoJSON FeatureCollection files, one per geometry kind."""

    def __init__(self, out_path, split):
        self._out_path = out_path
        self._split = split
        self._files = {}
        self._paths = {}
        self._counts = {}
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    def _path_for(self, kind):
        if not self._split:
            if self._out_path.lower().endswith(".geojson"):
                return self._out_path
            return self._out_path + ".geojson"
        suffix = _SPLIT_FILE_SUFFIXES.get(kind)
        return None if suffix is None else self._out_path + suffix

    def write(self, kind, feature):
        handle = self._files.get(kind)
        if handle is None:
            path = self._path_for(kind)
            if path is None:
                return False
            handle = open(path, "w", encoding="utf-8")
            handle.write('{"type":"FeatureCollection","features":[\n')
            self._files[kind] = handle
            self._paths[kind] = path
            self._counts[kind] = 0
        else:
            handle.write(",\n")
        json.dump(feature, handle, ensure_ascii=False)
        self._counts[kind] += 1
        return True

    def close(self):
        for handle in self._files.values():
            handle.write("\n]}\n")
            handle.close()
        self._files = {}

    def outputs(self):
        return {kind: path for kind, path in self._paths.items() if self._counts.get(kind)}

    def counts(self):
        return dict(self._counts)


def _layer_type_for_code(geometry_type_code):
    if geometry_type_code == 1:
        return "LineString"
    if geometry_type_code == 2:
        return "Polygon"
    if geometry_type_code == 3:
        return "Point"
    return None


def _resolve_geometry_field(col_names, geom_field_name):
    """Return ``(index, resolved_name, missing)`` for a metadata geometry field."""
    if not geom_field_name:
        return None, None, False
    if geom_field_name in col_names:
        return col_names.index(geom_field_name), geom_field_name, False
    # GFeatures spelling does not always match the physical column exactly.
    resolved = _field_lookup(col_names).get(normalise_field_name(geom_field_name))
    if resolved is None:
        return None, geom_field_name, True
    return col_names.index(resolved), resolved, False


def _decode_row_geometry(blob_bytes, geometry_type_code, forced_kind=None):
    """Return ``(geojson, kind, mean_z)`` for a decodable BLOB, else ``None``."""
    decoded = decode_geometry_blob(blob_bytes)
    if decoded is None:
        return None
    vertices = list(iter_vertices(decoded))
    if not vertices:
        return None
    if not all(math.isfinite(v[0]) and math.isfinite(v[1]) and math.isfinite(v[2])
               for v in vertices):
        return None
    kind = forced_kind or output_kind_for_geometry(decoded, geometry_type_code)
    geometry = geojson_for_kind(kind, decoded)
    if geometry is None:
        return None
    return geometry, kind, sum(v[2] for v in vertices) / len(vertices)


def _write_rows_to_geojson(mdb_path, table_name, col_names, rows,
                           geom_field_name, geometry_type_code, out_path,
                           max_features=0, split=False, row_count_hint=None,
                           allow_xy_fallback=True, allow_secondary_geometry=True):
    """Stream rows from either backend into GeoJSON FeatureCollections.

    Always returns a structured result, including for tables that produce no
    output at all, so the caller never has to infer failure from a missing file.
    """
    col_names = list(col_names)
    rows = iter(rows)

    geom_index, geom_field_name, geom_field_missing = _resolve_geometry_field(
        col_names, geom_field_name)

    # GFeatures names only the *primary* geometry column; GeoMedia tables often
    # carry a second one (e.g. CoordGeocodePoint) that holds the geometry for
    # rows whose primary BLOB is null.
    secondary_geom_indexes = []
    if allow_secondary_geometry:
        secondary_geom_indexes = [
            col_names.index(name) for name in find_candidate_geometry_fields(col_names)
            if col_names.index(name) != geom_index
        ]

    pair = find_candidate_coordinate_pair(col_names) if allow_xy_fallback else None
    x_field, y_field = pair if pair else (None, None)
    z_field = find_candidate_z_field(col_names, exclude=(x_field, y_field)) if pair else None

    layer_type = None
    if not split:
        layer_type = _layer_type_for_code(geometry_type_code)
        if layer_type is None and geometry_type_code not in (10, None, -1):
            return make_table_result(
                table_name,
                "unsupported",
                row_count=row_count_hint or 0,
                message=f"unsupported GeoMedia geometry type code {geometry_type_code}",
            )

    row_count = 0
    non_null_geometry_count = 0
    blob_decoded_count = 0
    secondary_blob_decoded_count = 0
    xy_fallback_count = 0
    invalid_geometry_count = 0
    geometry_fields_used = []
    truncated = False

    sink = _GeoJsonSink(out_path, split)
    source_name = os.path.basename(mdb_path)

    try:
        for row in rows:
            if max_features and row_count >= max_features:
                truncated = True
                break
            row_count += 1

            blob = row[geom_index] if geom_index is not None else None
            blob_bytes = coerce_blob_bytes(blob)
            if blob_bytes is not None:
                non_null_geometry_count += 1

            geometry = None
            geometry_source = None
            depth = None
            kind = None
            forced_kind = layer_type if not split else None

            attempt = (_decode_row_geometry(blob_bytes, geometry_type_code, forced_kind)
                       if blob_bytes is not None else None)
            if attempt is not None:
                geometry, kind, depth = attempt
                geometry_source = "blob"
                blob_decoded_count += 1
                if not split and layer_type is None:
                    layer_type = kind
                if geom_field_name and geom_field_name not in geometry_fields_used:
                    geometry_fields_used.append(geom_field_name)

            if geometry is None:
                for index in secondary_geom_indexes:
                    alternate = coerce_blob_bytes(row[index])
                    if alternate is None:
                        continue
                    attempt = _decode_row_geometry(alternate, geometry_type_code, forced_kind)
                    if attempt is None:
                        continue
                    geometry, kind, depth = attempt
                    geometry_source = "secondary_blob"
                    secondary_blob_decoded_count += 1
                    if not split and layer_type is None:
                        layer_type = kind
                    if col_names[index] not in geometry_fields_used:
                        geometry_fields_used.append(col_names[index])
                    break

            row_values = None
            if geometry is None and pair:
                row_values = dict(zip(col_names, row))
                fallback = build_fallback_point(row_values, x_field, y_field, z_field)
                if fallback is not None:
                    geometry, depth = fallback
                    kind = "Point"
                    if not split:
                        if layer_type is None:
                            layer_type = "Point"
                        if layer_type != "Point":
                            geometry = None
                    if geometry is not None:
                        geometry_source = "xy_fallback"
                        xy_fallback_count += 1

            if geometry is None:
                invalid_geometry_count += 1
                continue

            if row_values is None:
                row_values = dict(zip(col_names, row))
            props = {
                str(name): _coerce_json_value(value)
                for name, value in row_values.items()
                if name != geom_field_name
            }
            props["depth"] = depth
            props["source"] = source_name
            props["geometry_source"] = geometry_source

            feature = {"type": "Feature", "geometry": geometry, "properties": props}
            if not sink.write(str(kind), feature):
                invalid_geometry_count += 1
    finally:
        sink.close()

    outputs = sink.outputs()
    written = sum(sink.counts().values())
    effective_row_count = row_count if row_count else (row_count_hint or 0)

    if written:
        status, message = "success", ""
    elif effective_row_count == 0:
        status, message = "empty", "table is empty"
    elif geom_field_missing and not pair:
        status = "no_geometry"
        message = (
            "the metadata geometry field is not a physical column and no "
            "recognised coordinate pair is present"
        )
    elif non_null_geometry_count:
        status = "parse_failed"
        message = "no geometry BLOB could be decoded and no coordinate fallback succeeded"
    elif geom_index is None and not pair:
        status = "no_geometry"
        message = "no geometry field and no recognised coordinate pair"
    else:
        status = "no_geometry"
        message = "all geometry values were null and no coordinate fallback succeeded"

    if truncated:
        message = (message + "; row limit reached") if message else "row limit reached"

    return make_table_result(
        table_name,
        status,
        row_count=effective_row_count,
        non_null_geometry_count=non_null_geometry_count,
        blob_decoded_count=blob_decoded_count,
        secondary_blob_decoded_count=secondary_blob_decoded_count,
        xy_fallback_count=xy_fallback_count,
        invalid_geometry_count=invalid_geometry_count,
        geometry_types_found=sorted(outputs.keys()),
        geometry_fields_used=geometry_fields_used,
        outputs=outputs,
        message=message,
        layer_type=layer_type,
        written=written,
    )


# --------------------------------------------------------------------------
# Public entry points (backend dispatch)
# --------------------------------------------------------------------------

def list_feature_tables(mdb_path):
    """Return mapping {table_name: {geom_field_name, geometry_type_code}}."""
    return _run_with_backends(
        lambda: _list_feature_tables_pure(mdb_path),
        lambda: _list_feature_tables_odbc(mdb_path),
    )


def export_table_to_geojson(mdb_path, table_name, geom_field_name, geometry_type_code,
                            out_path, max_features=0, split=False,
                            allow_xy_fallback=True, allow_secondary_geometry=True):
    """Export a single table to GeoJSON and return a structured result."""
    def run_pure():
        col_names, rows = _export_rows_pure(mdb_path, table_name, geom_field_name)
        return _write_rows_to_geojson(
            mdb_path, table_name, col_names, rows, geom_field_name,
            geometry_type_code, out_path, max_features=max_features, split=split,
            row_count_hint=len(rows), allow_xy_fallback=allow_xy_fallback,
            allow_secondary_geometry=allow_secondary_geometry)

    def run_odbc():
        col_names, rows = _export_rows_odbc(mdb_path, table_name, geom_field_name)
        return _write_rows_to_geojson(
            mdb_path, table_name, col_names, rows, geom_field_name,
            geometry_type_code, out_path, max_features=max_features, split=split,
            allow_xy_fallback=allow_xy_fallback,
            allow_secondary_geometry=allow_secondary_geometry)

    return _run_with_backends(run_pure, run_odbc)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["list", "export"], required=True)
    parser.add_argument("--mdb", required=True)

    # export args
    parser.add_argument("--table")
    parser.add_argument("--geom-field", default="")
    parser.add_argument("--geom-type", type=int, default=-1)
    parser.add_argument("--out")
    parser.add_argument("--max-features", type=int, default=0)
    parser.add_argument("--split", choices=["0", "1"], default="0")
    parser.add_argument("--xy-fallback", choices=["0", "1"], default="1")
    parser.add_argument("--secondary-geometry", choices=["0", "1"], default="1")
    parser.add_argument("--schema-discovery", choices=["0", "1"], default=None)

    args = parser.parse_args(argv)

    try:
        if args.mode == "list":
            include_schema = (
                None if args.schema_discovery is None else args.schema_discovery == "1")
            sys.stdout.write(json.dumps(discover_tables(args.mdb, include_schema)))
            return 0

        if args.mode == "export":
            if not args.table or not args.out:
                raise RuntimeError("Missing required --table/--out")
            try:
                info = export_table_to_geojson(
                    args.mdb,
                    args.table,
                    args.geom_field or None,
                    args.geom_type,
                    args.out,
                    max_features=args.max_features or 0,
                    split=(args.split == "1"),
                    allow_xy_fallback=(args.xy_fallback == "1"),
                    allow_secondary_geometry=(args.secondary_geometry == "1"),
                )
            except FileNotFoundError:
                raise
            except Exception as exc:
                # One unreadable table must not abort the whole database.
                info = make_table_result(args.table, "error", message=exc)
            sys.stdout.write(json.dumps(info))
            return 0

        raise RuntimeError("Unknown mode")

    except Exception as e:
        err = {
            "error": sanitise_diagnostic(e),
            "traceback": sanitise_diagnostic(traceback.format_exc(), 2000),
        }
        sys.stderr.write(json.dumps(err))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
