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
- list: prints a JSON dict of {table_name: {geom_field_name, geometry_type_code}}
- export: writes a GeoJSON FeatureCollection for a single table

This script intentionally does NOT import qgis.*.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import struct
import sys
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

ACCESS_ODBC_DRIVER_NAME = "Microsoft Access Driver (*.mdb, *.accdb)"

_GFEATURES_NAME_COLS = {"FEATURENAME", "FEATURECLASSNAME", "NAME"}


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


def parse_blob(blob):
    if blob is None:
        return None

    if isinstance(blob, memoryview):
        blob = blob.tobytes()

    if not isinstance(blob, (bytes, bytearray)):
        return None

    if len(blob) < 20:
        return None

    magic_16 = blob[:16]
    standard_tail = bytes.fromhex("ffd20fbc8ccf11abde08003601b769")
    if magic_16[1:] != standard_tail:
        return None

    try:
        num_points = struct.unpack("<i", blob[16:20])[0]
    except struct.error:
        return None

    if num_points < 0 or num_points > 100000:
        return None

    expected_length = 20 + (24 * num_points)
    if len(blob) < expected_length:
        return None

    vertices = []
    offset = 20
    try:
        for _ in range(num_points):
            x, y, z = struct.unpack("<ddd", blob[offset : offset + 24])
            vertices.append((x, y, z))
            offset += 24
    except struct.error:
        return None

    return vertices


def is_closed(vertices, tol=1e-6):
    if len(vertices) < 2:
        return False
    x0, y0, _ = vertices[0]
    xn, yn, _ = vertices[-1]
    return abs(x0 - xn) <= tol and abs(y0 - yn) <= tol


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


def _export_rows_pure(mdb_path, table_name, geom_field_name):
    tables = _PureTables(mdb_path)
    col_names, rows = tables.read(table_name)
    if geom_field_name not in col_names:
        raise RuntimeError(
            f"Geometry field '{geom_field_name}' not found in table {table_name}")
    geom_index = col_names.index(geom_field_name)
    return col_names, (row for row in rows if row[geom_index] is not None)


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
    sql = (
        "SELECT * FROM "  # nosec B608
        + _quote_access_identifier(table_name)
        + " WHERE "
        + _quote_access_identifier(geom_field_name)
        + " IS NOT NULL"
    )
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
# Shared GeoJSON writer
# --------------------------------------------------------------------------

def _write_rows_to_geojson(mdb_path, table_name, col_names, rows,
                           geom_field_name, geometry_type_code, out_path,
                           max_features=0, split=False):
    """Stream rows from either backend into GeoJSON FeatureCollections."""
    if not col_names or geom_field_name not in col_names:
        raise RuntimeError(
            f"Geometry field '{geom_field_name}' not found in table {table_name}")

    geom_index = col_names.index(geom_field_name)
    prop_cols = [c for c in col_names if c != geom_field_name]
    rows = iter(rows)

    # If splitting, we don't need a single layer type.
    layer_type = None
    if not split:
        if geometry_type_code == 1:
            layer_type = "LineString"
        elif geometry_type_code == 2:
            layer_type = "Polygon"
        elif geometry_type_code == 3:
            layer_type = "Point"
        elif geometry_type_code == 10:
            first = next(rows, None)
            if first is None:
                raise RuntimeError(
                    f"No records with non-null geometry in table {table_name}")
            vertices = parse_blob(first[geom_index])
            if not vertices:
                raise RuntimeError(
                    f"Failed to parse geometry for first row in table {table_name}")
            # IMPORTANT: default ambiguous geometry to LineString rather than MultiPoint.
            layer_type = _infer_geom_type(vertices, geometry_type_code=geometry_type_code)
            rows = itertools.chain([first], rows)
        else:
            raise RuntimeError(f"Unsupported geometry type code: {geometry_type_code}")

    processed = 0
    skipped_parse = 0
    skipped_invalid = 0
    written_by_type = {"Point": 0, "LineString": 0, "Polygon": 0}

    out_dir = os.path.dirname(out_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    def open_fc(path):
        f = open(path, "w", encoding="utf-8")
        f.write('{"type":"FeatureCollection","features":[\n')
        return f

    def close_fc(fh):
        fh.write('\n]}\n')
        fh.close()

    if split:
        paths = {
            "Point": out_path + "_points.geojson",
            "LineString": out_path + "_lines.geojson",
            "Polygon": out_path + "_polygons.geojson",
        }
        files = {k: open_fc(p) for k, p in paths.items()}
        first_feature = {k: True for k in paths.keys()}
    else:
        out_geojson = out_path + ".geojson" if not out_path.lower().endswith('.geojson') else out_path
        key = str(layer_type) if layer_type else "Unknown"
        paths = {key: out_geojson}
        files = {key: open_fc(out_geojson)}
        first_feature = {key: True}

    try:
        for row in rows:
            processed += 1
            if max_features and processed > max_features:
                break

            blob = row[geom_index]
            vertices = parse_blob(blob)
            if not vertices:
                skipped_parse += 1
                continue

            if any((not math.isfinite(x) or not math.isfinite(y) or not math.isfinite(z)) for (x, y, z) in vertices):
                skipped_invalid += 1
                continue

            this_type = layer_type
            if split:
                this_type = _infer_geom_type(vertices, geometry_type_code=geometry_type_code)
            if not this_type or this_type not in files:
                skipped_invalid += 1
                continue

            # Geometry
            geom = None
            if this_type == "Point":
                x, y, _ = vertices[0]
                geom = {"type": "Point", "coordinates": [x, y]}
            elif this_type == "LineString":
                geom = {"type": "LineString", "coordinates": [[x, y] for (x, y, _) in vertices]}
            elif this_type == "Polygon":
                ring = vertices
                if not is_closed(ring):
                    ring = ring + [ring[0]]
                geom = {"type": "Polygon", "coordinates": [[[x, y] for (x, y, _) in ring]]}

            if not geom:
                skipped_invalid += 1
                continue

            depths = [v[2] for v in vertices]
            avg_depth = sum(depths) / len(depths) if depths else None

            props = {str(c): _coerce_json_value(row[col_names.index(c)]) for c in prop_cols}
            props["depth"] = avg_depth
            props["source"] = os.path.basename(mdb_path)

            feat = {"type": "Feature", "geometry": geom, "properties": props}

            fh = files[str(this_type)]
            if not first_feature[this_type]:
                fh.write(',\n')
            first_feature[this_type] = False
            json.dump(feat, fh, ensure_ascii=False)
            written_by_type[str(this_type)] = written_by_type.get(str(this_type), 0) + 1
    finally:
        for fh in files.values():
            close_fc(fh)

    outputs = {k: p for k, p in paths.items() if written_by_type.get(k, 0) > 0}

    return {
        "layer_type": layer_type,
        "processed": processed,
        "written": sum(written_by_type.values()),
        "written_by_type": written_by_type,
        "skipped_parse": skipped_parse,
        "skipped_invalid": skipped_invalid,
        "outputs": outputs,
    }


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
                            out_path, max_features=0, split=False):
    """Export a single table to GeoJSON.

    Geometry is encoded from the GeoMedia BLOB format used by this plugin.
    """
    def run_pure():
        col_names, rows = _export_rows_pure(mdb_path, table_name, geom_field_name)
        return _write_rows_to_geojson(
            mdb_path, table_name, col_names, rows, geom_field_name,
            geometry_type_code, out_path, max_features=max_features, split=split)

    def run_odbc():
        col_names, rows = _export_rows_odbc(mdb_path, table_name, geom_field_name)
        return _write_rows_to_geojson(
            mdb_path, table_name, col_names, rows, geom_field_name,
            geometry_type_code, out_path, max_features=max_features, split=split)

    return _run_with_backends(run_pure, run_odbc)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["list", "export"], required=True)
    parser.add_argument("--mdb", required=True)

    # export args
    parser.add_argument("--table")
    parser.add_argument("--geom-field")
    parser.add_argument("--geom-type", type=int)
    parser.add_argument("--out")
    parser.add_argument("--max-features", type=int, default=0)
    parser.add_argument("--split", choices=["0", "1"], default="0")

    args = parser.parse_args(argv)

    try:
        if args.mode == "list":
            tables = list_feature_tables(args.mdb)
            sys.stdout.write(json.dumps(tables))
            return 0

        if args.mode == "export":
            if not args.table or not args.geom_field or args.geom_type is None or not args.out:
                raise RuntimeError("Missing required --table/--geom-field/--geom-type/--out")
            info = export_table_to_geojson(
                args.mdb,
                args.table,
                args.geom_field,
                args.geom_type,
                args.out,
                max_features=args.max_features or 0,
                split=(args.split == "1"),
            )
            sys.stdout.write(json.dumps(info))
            return 0

        raise RuntimeError("Unknown mode")

    except Exception as e:
        err = {
            "error": str(e),
            "traceback": traceback.format_exc(),
        }
        sys.stderr.write(json.dumps(err))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
