# -*- coding: utf-8 -*-
"""Reader for .pthmdb path files.

A ``.pthmdb`` file is a GeoMedia-format Access (Jet) database describing one
cable path. The tables of interest:

``PathPoints``
    The route position list: one row per alter-course/way point, with KP,
    cable distance, slack, depth, label, comment and cable type. Geometry is
    a GeoMedia oriented-point BLOB (type 0xC8).
``PathLines``
    One row per segment between consecutive path points, with bearing, dKP,
    surface/bottom slack, cable type and a burial flag. Geometry is a
    GeoMedia line-segment BLOB (type 0xC1: exactly two vertices).
``AssemblyPoints``
    Cable assembly positions (joints, branching units, landings) keyed by KP.
``Profile``
    Optional KP/depth profile sampled from a bathymetry in the source application.
    Often empty; when populated it also names the source bathy table.
``GCoordSystem``
    GeoMedia coordinate-system record. Path files store geographic
    coordinates in degrees on WGS84, which this module verifies so callers
    can default the CRS instead of demanding one from the user.

This module is deliberately QGIS-free so it can be exercised by the plain
Python test suite; it reuses the backend dispatch (bundled ``access_parser``
first, ODBC fallback) and the GeoMedia BLOB decoder from the MDB import
stack.
"""

from __future__ import annotations

import math
import os

try:  # Running as a package module (QGIS, tests).
    from . import mdb_odbc_worker as _worker
    from .geomedia_blob import decode_geometry_blob
except ImportError:  # Running as a bare script.
    import sys
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    import mdb_odbc_worker as _worker  # type: ignore[no-redef]
    from geomedia_blob import decode_geometry_blob  # type: ignore[no-redef]


PATH_FILE_EXTENSIONS = {".pthmdb"}

#: Tables read from the path database. Missing tables are tolerated; the
#: reader only fails when PathPoints is absent or empty.
_TABLES = ("PathPoints", "PathLines", "AssemblyPoints", "Profile",
           "PathWidth", "SideSlopes", "GCoordSystem", "PathInfo",
           "PathEditInfo")

#: GeoMedia framework/metadata tables: known, deliberately not imported, and
#: excluded from the "unrecognised table" warning.
_METADATA_TABLES = frozenset({
    "GFEATURES", "GALIASTABLE", "GSQLOPERATORTABLE", "GEOMETRYPROPERTIES",
    "ATTRIBUTEPROPERTIES", "FIELDLOOKUP", "MODIFICATIONLOG", "MODIFIEDTABLES",
})

# WGS84 defining parameters, as stored by GeoMedia.
_WGS84_RADIUS = 6378137.0
_WGS84_INV_FLATTENING = 298.257223563
_DEG_TO_RAD = math.pi / 180.0
_MEAN_EARTH_RADIUS_M = 6371008.8


class PathFileError(RuntimeError):
    """Raised when a file cannot be read as a path database."""


class PathFileData:
    """Decoded content of one ``.pthmdb`` file.

    Attributes hold plain Python values only (no QGIS types):

    ``path_points`` / ``path_lines`` / ``assembly_points`` / ``profile`` /
    ``path_width`` / ``side_slopes``
        Lists of per-row dicts. Attribute keys keep the database column
        names; decoded coordinates are added as ``x``/``y``/``z`` (points)
        or ``vertices`` (a list of ``(x, y, z)`` tuples, lines).
    ``crs_auth_id``
        ``"EPSG:4326"`` when GCoordSystem matches the usual path-file
        degrees-on-WGS84 storage, else ``None`` (caller must ask the user).
    ``kp_unit``
        ``"m"`` or ``"km"`` when the PathPoints KP column agrees with the
        geodesic length of the decoded route, else ``None``.
    ``user_notes``
        Free-text notes stored in the file (PathInfo.UserNotes), or ``""``.
    ``warnings``
        Human-readable oddities that did not stop the import.
    """

    def __init__(self, source_file):
        self.source_file = source_file
        self.path_points = []
        self.path_lines = []
        self.assembly_points = []
        self.profile = []
        self.path_width = []
        self.side_slopes = []
        self.crs_auth_id = None
        self.crs_note = ""
        self.kp_unit = None
        self.user_notes = ""
        self.warnings = []

    @property
    def route_vertices(self):
        """Route line as ``[(x, y, z), ...]`` built from ordered PathPoints."""
        return [(p["x"], p["y"], p["z"]) for p in self.path_points]


def _read_tables(mdb_path):
    """Return ``(tables, extra)``: known-table content plus unrecognised
    tables.

    ``tables`` is ``{upper_table_name: (col_names, rows)}`` for the tables in
    :data:`_TABLES`; ``extra`` is ``{actual_name: row_count_or_None}`` for
    every physical table that is neither imported nor known GeoMedia
    metadata, so callers can tell the user what was left behind.
    """
    known_upper = {name.upper() for name in _TABLES}

    def _is_extra(upper_name):
        return (upper_name not in known_upper
                and upper_name not in _METADATA_TABLES
                and not upper_name.startswith("MSYS"))

    def _pure():
        tables = _worker._PureTables(mdb_path)
        out = {}
        for name in _TABLES:
            actual = tables.find_table(name)
            if actual:
                out[name.upper()] = tables.read(actual)
        extra = {}
        for actual in tables.table_names():
            if _is_extra(actual.upper()):
                try:
                    _cols, row_count = tables.describe(actual)
                except Exception:
                    row_count = None
                extra[actual] = row_count
        return out, extra

    def _odbc():
        with _worker._connect(mdb_path) as conn:
            cur = conn.cursor()
            actual_names = {str(ti.table_name).upper(): str(ti.table_name)
                            for ti in cur.tables()}
            out = {}
            for name in _TABLES:
                actual = actual_names.get(name.upper())
                if not actual:
                    continue
                sql = ("SELECT * FROM "  # nosec B608 - identifier validated
                       + _worker._quote_access_identifier(actual))
                cur.execute(sql)
                col_names = [desc[0] for desc in cur.description]
                rows = [tuple(row) for row in cur.fetchall()]
                out[name.upper()] = (col_names, rows)
            extra = {actual: None for upper, actual in actual_names.items()
                     if _is_extra(upper)}
            return out, extra

    return _worker._run_with_backends(_pure, _odbc)


def _rows_as_dicts(table):
    col_names, rows = table
    return [dict(zip(col_names, row)) for row in rows]


def _get(row, name, default=None):
    """Case-insensitive column lookup."""
    for key, value in row.items():
        if str(key).upper() == name.upper():
            return value
    return default


def _clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _sort_by_index(rows, table_name, warnings):
    indexed = [(r, _get(r, "Index")) for r in rows]
    if any(i is None for _, i in indexed):
        warnings.append(f"{table_name}: missing Index values; file order kept")
        return rows
    return [r for r, _ in sorted(indexed, key=lambda pair: pair[1])]


def _decode_point_rows(rows, table_name, warnings):
    out = []
    dropped = 0
    for row in rows:
        geometry = decode_geometry_blob(_get(row, "Geometry"))
        if geometry is None or geometry.kind != "Point" or not geometry.rings:
            dropped += 1
            continue
        x, y, z = geometry.rings[0][0]
        row = dict(row)
        row.pop("Geometry", None)
        row.update(x=x, y=y, z=z)
        out.append(row)
    if dropped:
        warnings.append(
            f"{table_name}: {dropped} row(s) had undecodable geometry and were skipped")
    return out


def _decode_line_rows(rows, warnings):
    out = []
    dropped = 0
    for row in rows:
        geometry = decode_geometry_blob(_get(row, "Geometry"))
        if geometry is None or geometry.kind != "LineString" or not geometry.rings:
            dropped += 1
            continue
        row = dict(row)
        row.pop("Geometry", None)
        row["vertices"] = list(geometry.rings[0])
        out.append(row)
    if dropped:
        warnings.append(
            f"PathLines: {dropped} row(s) had undecodable geometry and were skipped")
    return out


def _detect_crs(gcoordsystem_rows):
    """Return ``(auth_id_or_none, note)`` from the GCoordSystem record."""
    if not gcoordsystem_rows:
        return None, "no GCoordSystem table"
    row = gcoordsystem_rows[0]

    def _close(value, expected, tol):
        try:
            return abs(float(value) - expected) <= tol
        except (TypeError, ValueError):
            return False

    stored_in_degrees = _close(_get(row, "Stor2CompMatrix1"), _DEG_TO_RAD, 1e-12)
    wgs84_ellipsoid = (
        _close(_get(row, "EquatorialRadius"), _WGS84_RADIUS, 0.5)
        and _close(_get(row, "InverseFlattening"), _WGS84_INV_FLATTENING, 1e-4)
    )
    if stored_in_degrees and wgs84_ellipsoid:
        return "EPSG:4326", "geographic degrees on WGS84 (from GCoordSystem)"
    return None, (
        "GCoordSystem does not match the usual path-file storage "
        "(geographic degrees on WGS84); set the CRS manually"
    )


def _geodesic_length_m(vertices):
    """Approximate route length in metres (haversine over lon/lat degrees)."""
    total = 0.0
    for (lon1, lat1, _z1), (lon2, lat2, _z2) in zip(vertices, vertices[1:]):
        phi1, phi2 = lat1 * _DEG_TO_RAD, lat2 * _DEG_TO_RAD
        dphi = phi2 - phi1
        dlam = (lon2 - lon1) * _DEG_TO_RAD
        a = (math.sin(dphi / 2.0) ** 2
             + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2)
        total += 2.0 * _MEAN_EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))
    return total


def _detect_kp_unit(path_points, crs_auth_id, warnings):
    """Compare the last KP to the route's geodesic length to infer m vs km."""
    if crs_auth_id != "EPSG:4326" or len(path_points) < 2:
        return None
    kps = [_get(p, "KP") for p in path_points]
    if any(not isinstance(kp, (int, float)) for kp in kps):
        return None
    kp_span = max(kps) - min(kps)
    if kp_span <= 0:
        return None
    length_m = _geodesic_length_m(
        [(p["x"], p["y"], p["z"]) for p in path_points])
    if length_m <= 0:
        return None
    # Slack makes KP run a little long relative to the ground track, so allow
    # a generous band; m and km are three orders of magnitude apart.
    ratio = kp_span / length_m
    if 0.8 <= ratio <= 1.5:
        return "m"
    if 0.8 <= ratio * 1000.0 <= 1.5:
        return "km"
    warnings.append(
        f"KP column does not match the route length (ratio {ratio:.3g}); "
        "KP unit left undetermined")
    return None


def read_path_file(path):
    """Read a ``.pthmdb`` file into a :class:`PathFileData`.

    Raises :class:`PathFileError` when the file is missing, unreadable, or
    has no usable PathPoints; everything else degrades to ``warnings``.
    """
    normalized = os.path.abspath(os.path.normpath(os.fspath(path)))
    if not os.path.isfile(normalized):
        raise PathFileError(f"Path file does not exist: {normalized}")

    try:
        tables, extra_tables = _read_tables(normalized)
    except FileNotFoundError as exc:
        raise PathFileError(str(exc)) from exc
    except Exception as exc:
        raise PathFileError(
            f"Could not read {os.path.basename(normalized)} as an Access "
            f"database: {exc}") from exc

    if "PATHPOINTS" not in tables:
        raise PathFileError(
            f"{os.path.basename(normalized)} has no PathPoints table - "
            "not a recognised path file?")

    data = PathFileData(normalized)

    point_rows = _rows_as_dicts(tables["PATHPOINTS"])
    point_rows = _sort_by_index(point_rows, "PathPoints", data.warnings)
    data.path_points = _decode_point_rows(point_rows, "PathPoints", data.warnings)
    if not data.path_points:
        raise PathFileError(
            f"{os.path.basename(normalized)}: no PathPoints row has decodable "
            "geometry")

    if "PATHLINES" in tables:
        line_rows = _rows_as_dicts(tables["PATHLINES"])
        line_rows = _sort_by_index(line_rows, "PathLines", data.warnings)
        data.path_lines = _decode_line_rows(line_rows, data.warnings)

    if "ASSEMBLYPOINTS" in tables:
        assembly_rows = _rows_as_dicts(tables["ASSEMBLYPOINTS"])
        assembly_rows.sort(key=lambda r: (_get(r, "KP") is None, _get(r, "KP")))
        data.assembly_points = _decode_point_rows(
            assembly_rows, "AssemblyPoints", data.warnings)

    if "PROFILE" in tables:
        profile_rows = _rows_as_dicts(tables["PROFILE"])
        for row in profile_rows:
            geometry = decode_geometry_blob(_get(row, "Geometry"))
            row.pop("Geometry", None)
            if geometry is not None and geometry.kind == "Point" and geometry.rings:
                row["x"], row["y"], row["z"] = geometry.rings[0][0]
        profile_rows.sort(key=lambda r: (_get(r, "Kp") is None, _get(r, "Kp")))
        data.profile = profile_rows

    if "PATHWIDTH" in tables:
        width_rows = _rows_as_dicts(tables["PATHWIDTH"])
        width_rows = _sort_by_index(width_rows, "PathWidth", data.warnings)
        decoded_width = []
        dropped = 0
        for row in width_rows:
            blob = _get(row, "Geometry")
            geometry = decode_geometry_blob(blob)
            row = dict(row)
            row.pop("Geometry", None)
            if geometry is None:
                if blob is not None:
                    dropped += 1
            elif geometry.kind == "Point" and geometry.rings:
                row["x"], row["y"], row["z"] = geometry.rings[0][0]
            elif geometry.rings:
                row["vertices"] = list(geometry.rings[0])
            decoded_width.append(row)
        if dropped:
            data.warnings.append(
                f"PathWidth: {dropped} row(s) had undecodable geometry")
        data.path_width = decoded_width

    if "SIDESLOPES" in tables:
        slope_rows = _rows_as_dicts(tables["SIDESLOPES"])
        slope_rows.sort(key=lambda r: (_get(r, "kp") is None, _get(r, "kp")))
        data.side_slopes = slope_rows

    for name in sorted(extra_tables):
        row_count = extra_tables[name]
        rows_text = "unknown rows" if row_count is None else f"{row_count} row(s)"
        data.warnings.append(
            f"table '{name}' ({rows_text}) is not recognised and was not "
            "imported")

    data.crs_auth_id, data.crs_note = _detect_crs(
        _rows_as_dicts(tables["GCOORDSYSTEM"]) if "GCOORDSYSTEM" in tables else [])
    data.kp_unit = _detect_kp_unit(data.path_points, data.crs_auth_id, data.warnings)

    if "PATHINFO" in tables:
        info_rows = _rows_as_dicts(tables["PATHINFO"])
        if info_rows:
            data.user_notes = _clean_text(_get(info_rows[0], "UserNotes"))

    npt, nln = len(data.path_points), len(data.path_lines)
    if nln and nln != npt - 1:
        data.warnings.append(
            f"expected {npt - 1} PathLines for {npt} PathPoints, found {nln}")

    return data


def kp_to_km(value, kp_unit):
    """Convert a KP/distance value to kilometres given the detected unit."""
    if value is None:
        return None
    if kp_unit == "m":
        return float(value) / 1000.0
    return float(value)
