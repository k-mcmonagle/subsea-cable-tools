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
0xC9 graphic text                origin XYZ (3 doubles), rotation in
                                 degrees counter-clockwise (1 double),
                                 normal vector (3 doubles, usually 0,0,1),
                                 3 flag bytes, 1 alignment byte (0-10),
                                 int32 count, then the label bytes
                                 (Windows-1252 or UTF-16LE; plain or RTF)
===============================  ==========================================

UTF-16 labels are recognised
whether ``count`` holds characters or bytes: decoding a byte-counted UTF-16
label as Windows-1252 yields ``"l\\x00a\\x00..."``, which GDAL then truncates
at the first NUL, so the label arrives in QGIS as its first letter.

A reader that always treats bytes 16..19 as a point count therefore decodes
garbage for every point, boundary and collection feature.
"""

from __future__ import annotations

import math
import re
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
GEOMEDIA_TEXT = 0xC9
GEOMEDIA_MULTILINE = 0xCB
GEOMEDIA_MULTIPOLYGON = 0xCC

_MAX_NESTING = 8
_MAX_PARTS = 1_000_000

SIMPLE_KINDS = ("Point", "LineString", "Polygon")
MULTI_KINDS = ("MultiPoint", "MultiLineString", "MultiPolygon", "GeometryCollection")


#: ``rings`` holds vertex tuples for simple geometries (a polygon's first ring
#: is its exterior); ``parts`` holds nested geometries for collections;
#: ``text`` carries the plain label string of a graphic-text (0xC9) blob,
#: ``rtf`` the original string when GeoMedia stored rich text, ``rotation``
#: the label angle (degrees counter-clockwise) and ``alignment`` GeoMedia's
#: 0-10 justification code. All four are ``None`` for other geometry kinds.
GeomediaGeometry = namedtuple(
    "GeomediaGeometry",
    ("kind", "rings", "parts", "text", "rtf", "rotation", "alignment"),
    defaults=(None, None, None, None))


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


#: Text body: origin (24) + rotation (8) + normal (24) + flags (3) +
#: alignment (1) + count (4).
_TEXT_BODY_MIN = 64
_TEXT_ALIGNMENT_OFFSET = 59

#: One RTF token: control word, hex escape, control symbol, brace, line
#: break (ignored in RTF source) or a run of literal text.
_RTF_TOKEN = re.compile(
    r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z'])|([{}])|[\r\n]+|([^\\{}\r\n]+)")
#: Destinations whose content is formatting metadata, never label text.
_RTF_SKIPPED_DESTINATIONS = {
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer",
    "generator", "listtable", "listoverridetable", "rsidtbl", "themedata",
    "datastore", "latentstyles", "xmlnstbl", "mmathPr",
}
_RTF_CHARACTER_WORDS = {
    "par": "\n", "line": "\n", "tab": "\t", "emdash": "\u2014", "endash": "\u2013",
    "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c",
    "rdblquote": "\u201d", "bullet": "\u2022",
}


def rtf_to_text(rtf):
    """Return the visible text of an RTF string (formatting is discarded)."""
    out = []
    stack = []
    skip = False
    unicode_fallback = 1
    pending_fallback = 0
    for match in _RTF_TOKEN.finditer(rtf):
        word, arg, hex_code, symbol, brace, literal = match.groups()
        if brace == "{":
            stack.append((skip, unicode_fallback))
            continue
        if brace == "}":
            if stack:
                skip, unicode_fallback = stack.pop()
            continue
        # A \uN character is followed by fallback characters for old readers.
        if pending_fallback and (literal or hex_code):
            if hex_code:
                pending_fallback -= 1
                continue
            dropped = min(pending_fallback, len(literal))
            literal = literal[dropped:]
            pending_fallback -= dropped
            if not literal:
                continue
        if word is not None:
            if word in _RTF_SKIPPED_DESTINATIONS:
                skip = True
            elif word == "uc":
                unicode_fallback = int(arg or 1)
            elif word == "u":
                if not skip and arg:
                    out.append(chr(int(arg) & 0xFFFF))
                pending_fallback = unicode_fallback
            elif not skip and word in _RTF_CHARACTER_WORDS:
                out.append(_RTF_CHARACTER_WORDS[word])
            continue
        if symbol is not None:
            if symbol == "*":
                skip = True  # ignorable destination
            elif not skip:
                if symbol in "\\{}":
                    out.append(symbol)
                elif symbol == "~":
                    out.append("\u00a0")
                elif symbol in "\r\n":
                    out.append("\n")
            continue
        if skip:
            continue
        if hex_code:
            out.append(bytes([int(hex_code, 16)]).decode("cp1252", "replace"))
        elif literal:
            out.append(literal)
    return "".join(out).strip()


def is_rtf(text):
    return text.lstrip().startswith("{\\rtf")


def _decode_label_bytes(payload, count):
    """Decode a text-blob label, whichever way the writer counted it."""
    if count >= 1 and len(payload) >= 2 * count and not any(payload[1:2 * count:2]):
        # UTF-16LE, ``count`` in characters.
        data, encoding = payload[:2 * count], "utf-16-le"
    elif (count >= 2 and count % 2 == 0 and len(payload) >= count
            and not any(payload[1:count:2]) and any(payload[0:count:2])):
        # UTF-16LE, ``count`` in bytes.
        data, encoding = payload[:count], "utf-16-le"
    else:
        data, encoding = payload[:count], "cp1252"
    # A NUL terminator or padding would truncate the value downstream (GDAL
    # reads strings as C strings), so NULs never survive decoding.
    return data.decode(encoding, "replace").replace("\x00", "")


def _decode_text(body):
    if len(body) < _TEXT_BODY_MIN:
        return None
    vertex = struct.unpack_from("<ddd", body, 0)
    rotation = struct.unpack_from("<d", body, 24)[0]
    count = _read_int32(body, 60)
    if count is None or count < 0:
        return None
    text = _decode_label_bytes(body[_TEXT_BODY_MIN:], count)
    rtf = None
    if is_rtf(text):
        rtf, text = text, rtf_to_text(text)
    return GeomediaGeometry(
        "Point", ((vertex,),), (), text, rtf,
        rotation if math.isfinite(rotation) else None,
        body[_TEXT_ALIGNMENT_OFFSET])


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

        if type_code == GEOMEDIA_TEXT:
            return _decode_text(body)

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
