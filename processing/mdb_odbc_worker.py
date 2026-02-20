# -*- coding: utf-8 -*-
"""Worker process for reading GeoMedia MDB via ODBC.

Why this exists:
- Some ODBC/Access drivers can hard-crash the host process (QGIS) with no
  Python exception.
- Running ODBC operations in a subprocess contains those crashes.

This script is invoked by the QGIS processing algorithm and communicates via
stdout/stderr.

Modes:
- list: prints a JSON dict of {table_name: {geom_field_name, geometry_type_code}}
- export: writes a GeoJSON FeatureCollection for a single table

This script intentionally does NOT import qgis.*.
"""

from __future__ import annotations

import argparse
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

ACCESS_ODBC_DRIVER_NAME = "Microsoft Access Driver (*.mdb, *.accdb)"


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
    conn_str = rf"Driver={{{ACCESS_ODBC_DRIVER_NAME}}};DBQ={mdb_path};"
    return pyodbc.connect(conn_str, timeout=timeout_seconds)


def list_feature_tables(mdb_path):
    """Return mapping {table_name: {geom_field_name, geometry_type_code}}."""
    out = {}
    with _connect(mdb_path) as conn:
        cur = conn.cursor()

        gfeatures_table = None
        for ti in cur.tables():
            if str(ti.table_name).upper() == "GFEATURES":
                gfeatures_table = ti.table_name
                break

        if not gfeatures_table:
            return out

        cur.execute(f"SELECT * FROM [{gfeatures_table}] WHERE 1=0")
        col_names = [desc[0] for desc in cur.description]
        if not col_names:
            return out

        feature_name_col = None
        for col in col_names:
            if str(col).upper() in {"FEATURENAME", "FEATURECLASSNAME", "NAME"}:
                feature_name_col = col
                break
        if not feature_name_col:
            feature_name_col = col_names[0]
        geom_field_col = None
        geom_type_col = None
        for col in col_names:
            if str(col).upper() == "PRIMARYGEOMETRYFIELDNAME":
                geom_field_col = col
            elif str(col).upper() == "GEOMETRYTYPE":
                geom_type_col = col

        if not geom_field_col or not geom_type_col:
            return out

        sql = (
            f"SELECT [{feature_name_col}], [{geom_field_col}], [{geom_type_col}] "
            f"FROM [{gfeatures_table}] "
            f"WHERE [{geom_type_col}] <> 33"
        )
        cur.execute(sql)
        for row in cur.fetchall():
            table_name, geom_field, geometry_type = row
            out[str(table_name)] = {
                "geom_field_name": str(geom_field),
                "geometry_type_code": int(geometry_type),
            }

    return out


def _coerce_json_value(v):
    if v is None:
        return None

    # pyodbc may return Decimal, datetime, etc. Keep it robust.
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

    # Respect explicit geometry metadata when available.
    if geometry_type_code == 1:
        return "LineString"
    if geometry_type_code == 2:
        return "Polygon"
    if geometry_type_code == 3:
        return "Point"

    # Ambiguous GeoMedia type codes (e.g. 10) are prone to treating closed
    # contour lines as polygons. Prefer line output to avoid false polygons.
    if len(vertices) == 1:
        return "Point"
    return "LineString"


def export_table_to_geojson(mdb_path, table_name, geom_field_name, geometry_type_code, out_path, max_features=0, split=False):
    """Export a single table to GeoJSON.

    Geometry is encoded from the GeoMedia BLOB format used by this plugin.
    """
    with _connect(mdb_path) as conn:
        cur = conn.cursor()
        sql = f"SELECT * FROM [{table_name}] WHERE [{geom_field_name}] IS NOT NULL"
        cur.execute(sql)
        col_names = [desc[0] for desc in cur.description]
        if not col_names or geom_field_name not in col_names:
            raise RuntimeError(f"Geometry field '{geom_field_name}' not found in table {table_name}")

        geom_index = col_names.index(geom_field_name)
        prop_cols = [c for c in col_names if c != geom_field_name]

        # If splitting, we don't need a single layer type.
        layer_type = None
        if not split:
            # Determine layer type similarly to in-process code
            if geometry_type_code == 1:
                layer_type = "LineString"
            elif geometry_type_code == 2:
                layer_type = "Polygon"
            elif geometry_type_code == 3:
                layer_type = "Point"
            elif geometry_type_code == 10:
                first = cur.fetchone()
                if not first:
                    raise RuntimeError(f"No records with non-null geometry in table {table_name}")
                vertices = parse_blob(first[geom_index])
                if not vertices:
                    raise RuntimeError(f"Failed to parse geometry for first row in table {table_name}")
                # IMPORTANT: default ambiguous geometry to LineString rather than MultiPoint.
                layer_type = _infer_geom_type(vertices, geometry_type_code=geometry_type_code)

                # Reset cursor by re-executing
                cur.execute(sql)
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
            for row in cur:
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
                else:
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
