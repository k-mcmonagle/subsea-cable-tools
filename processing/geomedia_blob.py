# -*- coding: utf-8 -*-
"""Decoder for GeoMedia (GDO) geometry BLOBs held in Access/MDB feature tables.

This module is deliberately standalone: the MDB worker runs as a bare
subprocess script, so nothing here may import from the plugin package.

BLOB layout (little endian)::

    byte  0       geometry type code
    bytes 1..15   GeoMedia class GUID tail (only bytes 1..3 are diagnostic)
    bytes 16..    type-specific body

The bodies are *not* uniform, which is the historic cause of point tables
importing as zero features:

===============================  ==========================================
type code                        body
===============================  ==========================================
0xC0 point, 0xC8 oriented point  three doubles - **no point count**
0xC1 line segment                six doubles (two XYZ vertices) - no count
0xC2 polyline, 0xC3 polygon      int32 point count, then 24 bytes per point
0xC5 boundary                    int32 size + exterior, int32 size + interior
0xC6 collection, 0xCB multiline, int32 part count, then int32 size + part
0xCC multipolygon
===============================  ==========================================

A reader that always treats bytes 16..19 as a point count therefore decodes
garbage for every point, boundary and collection feature.
"""

from __future__ import annotations

import struct
from collections import namedtuple


# Bytes 1..3 of the class GUID. GeoMedia writers vary in the remaining GUID
# bytes, so only this well-known prefix is required (matching GDAL's decoder).
HEADER_SIGNATURE = bytes.fromhex("ffd20f")
HEADER_SIZE = 16

GEOMEDIA_POINT = 0xC0
GEOMEDIA_LINE = 0xC1
GEOMEDIA_POLYLINE = 0xC2
GEOMEDIA_POLYGON = 0xC3
GEOMEDIA_BOUNDARY = 0xC5
GEOMEDIA_COLLECTION = 0xC6
GEOMEDIA_ORIENTED_POINT = 0xC8
GEOMEDIA_MULTILINE = 0xCB
GEOMEDIA_MULTIPOLYGON = 0xCC

_MAX_NESTING = 8
_MAX_PARTS = 1_000_000

SIMPLE_KINDS = ("Point", "LineString", "Polygon")
MULTI_KINDS = ("MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection")


#: ``rings`` holds vertex tuples for simple geometries (a polygon's first ring
#: is its exterior); ``parts`` holds nested geometries for collections.
GeomediaGeometry = namedtuple("GeomediaGeometry", ("kind", "rings", "parts"))


def coerce_blob_bytes(value):
    """Normalise whatever an MDB backend returned for a BLOB column to bytes.

    The bundled pure-Python reader yields ``bytes`` for OLE/binary columns but
    ``""`` for a zero-length variable-length column; pyodbc can yield
    ``memoryview`` or ``bytearray``.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        return value or None
    if isinstance(value, (bytearray, memoryview)):
        data = bytes(value)
        return data or None
    if isinstance(value, str):
        if not value:
            return None
        try:
            return value.encode("latin-1") or None
        except UnicodeEncodeError:
            return None
    return None


def _read_int32(data, offset):
    if offset + 4 > len(data):
        return None
    return struct.unpack_from("<i", data, offset)[0]


def _read_vertices(body, offset=0):
    """Read an int32 count followed by that many XYZ triples."""
    count = _read_int32(body, offset)
    if count is None or count < 0:
        return None
    offset += 4
    if count * 24 > len(body) - offset:
        return None
    vertices = []
    for _ in range(count):
        vertices.append(struct.unpack_from("<ddd", body, offset))
        offset += 24
    return vertices


def _decode_collection(body, type_code, depth):
    count = _read_int32(body, 0)
    if count is None or count < 0 or count > _MAX_PARTS:
        return None
    offset = 4
    parts = []
    for _ in range(count):
        size = _read_int32(body, offset)
        if size is None or size < 0:
            return None
        offset += 4
        if size > len(body) - offset:
            return None
        part = decode_geometry_blob(body[offset:offset + size], _depth=depth + 1)
        offset += size
        if part is not None:
            parts.append(part)
    if not parts:
        return None

    if type_code == GEOMEDIA_MULTIPOLYGON:
        parts = [
            GeomediaGeometry("Polygon", part.rings, ())
            if part.kind == "LineString" else part
            for part in parts
        ]

    if len(parts) == 1:
        return parts[0]

    kinds = {part.kind for part in parts}
    if kinds == {"LineString"}:
        kind = "MultiLineString"
    elif kinds == {"Polygon"}:
        kind = "MultiPolygon"
    elif kinds == {"Point"}:
        kind = "MultiPoint"
    else:
        kind = "GeometryCollection"
    return GeomediaGeometry(kind, (), tuple(parts))


def _decode_boundary(body, depth):
    exterior_size = _read_int32(body, 0)
    if exterior_size is None or exterior_size < 0 or exterior_size > len(body) - 4:
        return None
    exterior = decode_geometry_blob(body[4:4 + exterior_size], _depth=depth + 1)
    if exterior is None or exterior.kind not in {"Polygon", "LineString"}:
        return None

    rings = list(exterior.rings)
    rest = body[4 + exterior_size:]
    interior_size = _read_int32(rest, 0)
    if interior_size is not None and 0 <= interior_size <= len(rest) - 4:
        interior = decode_geometry_blob(rest[4:4 + interior_size], _depth=depth + 1)
        if interior is not None:
            if interior.kind in {"Polygon", "LineString"}:
                rings.extend(interior.rings)
            elif interior.kind in {"MultiPolygon", "GeometryCollection"}:
                for part in interior.parts:
                    rings.extend(part.rings)
    return GeomediaGeometry("Polygon", tuple(rings), ())


def decode_geometry_blob(blob, _depth=0):
    """Decode a GeoMedia geometry BLOB, or return ``None`` if unrecognised."""
    if _depth > _MAX_NESTING:
        return None
    data = coerce_blob_bytes(blob)
    if data is None or len(data) < HEADER_SIZE:
        return None
    if data[1:4] != HEADER_SIGNATURE:
        return None

    type_code = data[0]
    body = data[HEADER_SIZE:]

    try:
        if type_code in (GEOMEDIA_POINT, GEOMEDIA_ORIENTED_POINT):
            if len(body) < 24:
                return None
            vertex = struct.unpack_from("<ddd", body, 0)
            return GeomediaGeometry("Point", ((vertex,),), ())

        if type_code == GEOMEDIA_LINE:
            if len(body) < 48:
                return None
            start = struct.unpack_from("<ddd", body, 0)
            end = struct.unpack_from("<ddd", body, 24)
            return GeomediaGeometry("LineString", ((start, end),), ())

        if type_code in (GEOMEDIA_POLYLINE, GEOMEDIA_POLYGON):
            vertices = _read_vertices(body)
            if not vertices:
                return None
            kind = "LineString" if type_code == GEOMEDIA_POLYLINE else "Polygon"
            return GeomediaGeometry(kind, (tuple(vertices),), ())

        if type_code == GEOMEDIA_BOUNDARY:
            return _decode_boundary(body, _depth)

        if type_code in (GEOMEDIA_COLLECTION, GEOMEDIA_MULTILINE, GEOMEDIA_MULTIPOLYGON):
            return _decode_collection(body, type_code, _depth)
    except struct.error:
        return None

    return None


def iter_vertices(geometry):
    """Yield every ``(x, y, z)`` vertex of a decoded geometry."""
    if geometry is None:
        return
    for ring in geometry.rings:
        for vertex in ring:
            yield vertex
    for part in geometry.parts:
        for vertex in iter_vertices(part):
            yield vertex


def is_closed_ring(vertices, tol=1e-6):
    if len(vertices) < 2:
        return False
    x0, y0 = vertices[0][0], vertices[0][1]
    xn, yn = vertices[-1][0], vertices[-1][1]
    return abs(x0 - xn) <= tol and abs(y0 - yn) <= tol


def to_geojson_geometry(geometry):
    """Convert a decoded geometry to a 2D GeoJSON geometry dict."""
    if geometry is None:
        return None
    kind = geometry.kind

    if kind == "Point":
        if not geometry.rings or not geometry.rings[0]:
            return None
        x, y = geometry.rings[0][0][0], geometry.rings[0][0][1]
        return {"type": "Point", "coordinates": [x, y]}

    if kind == "LineString":
        ring = geometry.rings[0] if geometry.rings else ()
        if len(ring) < 2:
            return None
        return {"type": "LineString", "coordinates": [[v[0], v[1]] for v in ring]}

    if kind == "Polygon":
        rings = []
        for ring in geometry.rings:
            if len(ring) < 3:
                continue
            closed = list(ring)
            if not is_closed_ring(closed):
                closed.append(closed[0])
            rings.append([[v[0], v[1]] for v in closed])
        if not rings:
            return None
        return {"type": "Polygon", "coordinates": rings}

    if kind in MULTI_KINDS:
        children = [to_geojson_geometry(part) for part in geometry.parts]
        children = [child for child in children if child]
        if not children:
            return None
        if kind == "GeometryCollection":
            return {"type": "GeometryCollection", "geometries": children}
        return {"type": kind, "coordinates": [child["coordinates"] for child in children]}

    return None


def parse_blob(blob):
    """Legacy helper: return a flat list of ``(x, y, z)`` vertices, or ``None``.

    Kept for the in-process ODBC path and for geometry-type inference that
    predates the structured decoder.
    """
    geometry = decode_geometry_blob(blob)
    if geometry is None:
        return None
    vertices = list(iter_vertices(geometry))
    return vertices or None
