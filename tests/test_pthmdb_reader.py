# -*- coding: utf-8 -*-
"""Tests for the path file (.pthmdb) reader.

Synthetic-BLOB and detection-logic tests always run. Integration tests
against real path databases run only when anonymised reference files exist
in the local (gitignored) ref/ folder — they are skipped elsewhere.
"""

import math
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from processing import geomedia_blob
from processing import pthmdb_reader

_REF_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ref")
_REF_FILES = sorted(
    os.path.join(_REF_DIR, name) for name in (
        os.listdir(_REF_DIR) if os.path.isdir(_REF_DIR) else [])
    if name.lower().endswith(".pthmdb"))

_GUID_TAIL = bytes.fromhex("ffd20fbc8ccf11abde08003601b769")


def _line_segment_blob(start, end):
    return (bytes([geomedia_blob.GEOMEDIA_LINE]) + _GUID_TAIL
            + struct.pack("<ddd", *start) + struct.pack("<ddd", *end))


# --------------------------------------------------------------------------
# GeoMedia 0xC1 line-segment BLOBs (used by PathLines)
# --------------------------------------------------------------------------

def test_line_segment_blob_decodes_to_two_vertex_linestring():
    blob = _line_segment_blob((104.9, 1.26, 0.0), (104.8, 1.20, -5.0))
    decoded = geomedia_blob.decode_geometry_blob(blob)
    assert decoded is not None
    assert decoded.kind == "LineString"
    assert decoded.rings == (((104.9, 1.26, 0.0), (104.8, 1.20, -5.0)),)


def test_line_segment_blob_truncated_body_is_rejected():
    blob = _line_segment_blob((0.0, 0.0, 0.0), (1.0, 1.0, 0.0))[:-1]
    assert geomedia_blob.decode_geometry_blob(blob) is None


def test_line_segment_geojson_round_trip():
    blob = _line_segment_blob((1.0, 2.0, 0.0), (3.0, 4.0, 0.0))
    decoded = geomedia_blob.decode_geometry_blob(blob)
    assert geomedia_blob.to_geojson_geometry(decoded) == {
        "type": "LineString", "coordinates": [[1.0, 2.0], [3.0, 4.0]]}


# --------------------------------------------------------------------------
# CRS detection from GCoordSystem
# --------------------------------------------------------------------------

_WGS84_ROW = {
    "Stor2CompMatrix1": math.pi / 180.0,
    "EquatorialRadius": 6378137.0,
    "InverseFlattening": 298.25722356300156,
}


def test_detect_crs_wgs84_degrees():
    auth_id, note = pthmdb_reader._detect_crs([_WGS84_ROW])
    assert auth_id == "EPSG:4326"
    assert "WGS84" in note


def test_detect_crs_rejects_other_ellipsoid():
    row = dict(_WGS84_ROW, EquatorialRadius=6377563.396)  # Airy 1830
    auth_id, _note = pthmdb_reader._detect_crs([row])
    assert auth_id is None


def test_detect_crs_rejects_projected_storage():
    row = dict(_WGS84_ROW, Stor2CompMatrix1=1.0)  # metres, not degrees
    auth_id, _note = pthmdb_reader._detect_crs([row])
    assert auth_id is None


def test_detect_crs_missing_table():
    auth_id, note = pthmdb_reader._detect_crs([])
    assert auth_id is None and "no GCoordSystem" in note


# --------------------------------------------------------------------------
# KP unit detection
# --------------------------------------------------------------------------

def _points_along_equator(kp_values):
    # 0.01 deg of longitude at the equator is ~1.11 km.
    return [
        {"KP": kp, "x": 0.01 * i, "y": 0.0, "z": 0.0}
        for i, kp in enumerate(kp_values)]


def test_kp_unit_metres():
    points = _points_along_equator([0.0, 1113.0, 2226.0])
    assert pthmdb_reader._detect_kp_unit(points, "EPSG:4326", []) == "m"


def test_kp_unit_kilometres():
    points = _points_along_equator([0.0, 1.113, 2.226])
    assert pthmdb_reader._detect_kp_unit(points, "EPSG:4326", []) == "km"


def test_kp_unit_mismatch_warns():
    warnings = []
    points = _points_along_equator([0.0, 50.0, 100.0])
    assert pthmdb_reader._detect_kp_unit(points, "EPSG:4326", warnings) is None
    assert warnings


def test_kp_unit_needs_known_crs():
    points = _points_along_equator([0.0, 1113.0, 2226.0])
    assert pthmdb_reader._detect_kp_unit(points, None, []) is None


def test_kp_to_km():
    assert pthmdb_reader.kp_to_km(1500.0, "m") == 1.5
    assert pthmdb_reader.kp_to_km(1.5, "km") == 1.5
    assert pthmdb_reader.kp_to_km(None, "m") is None


# --------------------------------------------------------------------------
# Integration: real (anonymised, local-only) path files
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _REF_FILES, reason="no local ref/*.pthmdb files")
@pytest.mark.parametrize("path", _REF_FILES)
def test_reference_path_file_reads(path):
    data = pthmdb_reader.read_path_file(path)

    assert len(data.path_points) >= 2
    assert len(data.path_lines) == len(data.path_points) - 1
    assert data.crs_auth_id == "EPSG:4326"
    assert data.kp_unit in ("m", "km")

    # Points are ordered and KP is monotonic non-decreasing.
    kps = [p["KP"] for p in data.path_points]
    assert all(b >= a for a, b in zip(kps, kps[1:]))
    for point in data.path_points:
        assert -180.0 <= point["x"] <= 180.0
        assert -90.0 <= point["y"] <= 90.0

    # Each segment joins consecutive path points (same coordinates).
    for i, line in enumerate(data.path_lines):
        start, end = line["vertices"][0], line["vertices"][-1]
        p0, p1 = data.path_points[i], data.path_points[i + 1]
        assert abs(start[0] - p0["x"]) < 1e-9 and abs(start[1] - p0["y"]) < 1e-9
        assert abs(end[0] - p1["x"]) < 1e-9 and abs(end[1] - p1["y"]) < 1e-9

    assert data.route_vertices[0][:2] == (
        data.path_points[0]["x"], data.path_points[0]["y"])


@pytest.mark.skipif(not _REF_FILES, reason="no local ref/*.pthmdb files")
def test_reference_file_rejects_wrong_table_shape(tmp_path):
    bogus = tmp_path / "not_a_path.pthmdb"
    bogus.write_bytes(b"\x00" * 4096)
    with pytest.raises(pthmdb_reader.PathFileError):
        pthmdb_reader.read_path_file(str(bogus))


def test_missing_file_raises():
    with pytest.raises(pthmdb_reader.PathFileError):
        pthmdb_reader.read_path_file(r"C:\nonexistent\missing.pthmdb")
