# -*- coding: utf-8 -*-
"""Regression tests for GeoMedia BLOB decoding and MDB worker diagnostics.

Everything here uses synthetic BLOBs and mocked reader rows. No real project
database is required, referenced or produced.
"""

import json
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from processing import geomedia_blob
from processing import mdb_odbc_worker as worker


# --------------------------------------------------------------------------
# Synthetic BLOB builders (see processing/geomedia_blob.py for the layout)
# --------------------------------------------------------------------------

_GUID_TAIL = bytes.fromhex("ffd20fbc8ccf11abde08003601b769")


def _header(type_code):
    return bytes([type_code]) + _GUID_TAIL


def _vertex_body(vertices):
    body = struct.pack("<i", len(vertices))
    for vertex in vertices:
        body += struct.pack("<ddd", *vertex)
    return body


def point_blob(x, y, z=0.0):
    return _header(geomedia_blob.GEOMEDIA_POINT) + struct.pack("<ddd", x, y, z)


def oriented_point_blob(x, y, z=0.0):
    return (_header(geomedia_blob.GEOMEDIA_ORIENTED_POINT)
            + struct.pack("<ddd", x, y, z)
            + struct.pack("<ddd", 1.0, 0.0, 0.0))


def line_blob(vertices):
    return _header(geomedia_blob.GEOMEDIA_POLYLINE) + _vertex_body(vertices)


def polygon_blob(vertices):
    return _header(geomedia_blob.GEOMEDIA_POLYGON) + _vertex_body(vertices)


def boundary_blob(exterior, interior):
    outer = polygon_blob(exterior)
    inner = polygon_blob(interior)
    return (_header(geomedia_blob.GEOMEDIA_BOUNDARY)
            + struct.pack("<i", len(outer)) + outer
            + struct.pack("<i", len(inner)) + inner)


def text_blob(x, y, text, z=0.0, encoding="cp1252", count=None):
    payload = text.encode(encoding)
    if count is None:
        count = len(text) if encoding == "utf-16-le" else len(payload)
    return (_header(geomedia_blob.GEOMEDIA_TEXT)
            + struct.pack("<ddd", x, y, z)
            + struct.pack("<dddd", 0.0, 0.0, 0.0, 1.0)
            + bytes.fromhex("00000109")
            + struct.pack("<i", count)
            + payload)


def collection_blob(type_code, sub_blobs):
    body = struct.pack("<i", len(sub_blobs))
    for sub in sub_blobs:
        body += struct.pack("<i", len(sub)) + sub
    return _header(type_code) + body


SQUARE = [(0.0, 0.0, -1.0), (10.0, 0.0, -1.0), (10.0, 10.0, -1.0),
          (0.0, 10.0, -1.0), (0.0, 0.0, -1.0)]
HOLE = [(2.0, 2.0, -1.0), (4.0, 2.0, -1.0), (4.0, 4.0, -1.0), (2.0, 2.0, -1.0)]
TRACK = [(0.0, 0.0, -5.0), (1.0, 1.0, -6.0), (2.0, 3.0, -7.0)]


def _export(tmp_path, col_names, rows, geom_field, geom_type, **kwargs):
    out_base = os.path.join(str(tmp_path), "table_out")
    kwargs.setdefault("split", True)
    return worker._write_rows_to_geojson(
        os.path.join(str(tmp_path), "synthetic.mdb"),
        kwargs.pop("table_name", "Synthetic_Table"),
        col_names,
        rows,
        geom_field,
        geom_type,
        out_base,
        row_count_hint=len(rows),
        **kwargs,
    )


def _features(result, kind):
    with open(result["outputs"][kind], "r", encoding="utf-8") as handle:
        return json.load(handle)["features"]


# --------------------------------------------------------------------------
# BLOB decoding
# --------------------------------------------------------------------------

def test_point_blob_decodes_without_a_point_count():
    decoded = geomedia_blob.decode_geometry_blob(point_blob(500000.0, 6000000.0, -42.5))
    assert decoded is not None
    assert decoded.kind == "Point"
    assert decoded.rings[0][0] == (500000.0, 6000000.0, -42.5)


def test_oriented_point_blob_decodes():
    decoded = geomedia_blob.decode_geometry_blob(oriented_point_blob(1.0, 2.0, 3.0))
    assert decoded.kind == "Point"
    assert decoded.rings[0][0] == (1.0, 2.0, 3.0)


def test_line_and_polygon_blobs_still_decode():
    assert geomedia_blob.decode_geometry_blob(line_blob(TRACK)).kind == "LineString"
    polygon = geomedia_blob.decode_geometry_blob(polygon_blob(SQUARE))
    assert polygon.kind == "Polygon"
    assert len(polygon.rings) == 1


def test_boundary_blob_decodes_with_a_hole():
    decoded = geomedia_blob.decode_geometry_blob(boundary_blob(SQUARE, HOLE))
    assert decoded.kind == "Polygon"
    assert len(decoded.rings) == 2
    assert len(geomedia_blob.to_geojson_geometry(decoded)["coordinates"]) == 2


def test_collection_blob_decodes_to_multi_geometry():
    blob = collection_blob(geomedia_blob.GEOMEDIA_MULTILINE,
                           [line_blob(TRACK), line_blob(TRACK)])
    decoded = geomedia_blob.decode_geometry_blob(blob)
    assert decoded.kind == "MultiLineString"
    assert len(decoded.parts) == 2


def test_collection_of_points_decodes_to_multipoint():
    blob = collection_blob(geomedia_blob.GEOMEDIA_COLLECTION,
                           [point_blob(1.0, 2.0), point_blob(3.0, 4.0)])
    assert geomedia_blob.decode_geometry_blob(blob).kind == "MultiPoint"


def test_text_blob_decodes_to_point_with_label():
    decoded = geomedia_blob.decode_geometry_blob(text_blob(107.15, -5.89, "ECHO-S3-TS-SC001"))
    assert decoded.kind == "Point"
    assert decoded.rings[0][0][:2] == (107.15, -5.89)
    assert decoded.text == "ECHO-S3-TS-SC001"


def test_text_blob_decodes_cp1252_labels():
    # Slope annotations use the Windows-1252 degree sign.
    decoded = geomedia_blob.decode_geometry_blob(text_blob(108.07, -4.07, "12°"))
    assert decoded.text == "12°"


def test_text_blob_decodes_utf16_labels():
    decoded = geomedia_blob.decode_geometry_blob(
        text_blob(1.0, 2.0, "Sand wave", encoding="utf-16-le"))
    assert decoded.kind == "Point"
    assert decoded.text == "Sand wave"


def test_text_blob_with_empty_label_still_yields_the_point():
    decoded = geomedia_blob.decode_geometry_blob(text_blob(3.0, 4.0, ""))
    assert decoded.kind == "Point"
    assert decoded.text == ""


def test_malformed_text_blobs_are_rejected():
    good = text_blob(1.0, 2.0, "abc")
    # Body shorter than origin + quaternion + flags + count.
    assert geomedia_blob.decode_geometry_blob(good[:16 + 63]) is None
    # Negative byte count.
    bad_count = good[:16 + 60] + struct.pack("<i", -5) + b"abc"
    assert geomedia_blob.decode_geometry_blob(bad_count) is None
    # A count larger than the payload keeps what is present rather than failing.
    truncated = geomedia_blob.decode_geometry_blob(good[:-1])
    assert truncated.kind == "Point"
    assert truncated.text == "ab"


def test_non_text_geometries_carry_no_text():
    assert geomedia_blob.decode_geometry_blob(point_blob(1.0, 2.0)).text is None
    assert geomedia_blob.decode_geometry_blob(line_blob(TRACK)).text is None


def test_unknown_and_malformed_blobs_are_rejected():
    assert geomedia_blob.decode_geometry_blob(None) is None
    assert geomedia_blob.decode_geometry_blob(b"") is None
    assert geomedia_blob.decode_geometry_blob(b"\x99" * 64) is None
    assert geomedia_blob.decode_geometry_blob(_header(0x7F) + b"\x00" * 32) is None
    # Truncated vertex payload.
    assert geomedia_blob.decode_geometry_blob(
        _header(geomedia_blob.GEOMEDIA_POLYLINE) + struct.pack("<i", 500)) is None


def test_blob_accepts_memoryview_and_bytearray():
    raw = point_blob(5.0, 6.0, 7.0)
    assert geomedia_blob.decode_geometry_blob(memoryview(raw)).kind == "Point"
    assert geomedia_blob.decode_geometry_blob(bytearray(raw)).kind == "Point"
    # The bundled reader yields "" for a zero-length variable-length column.
    assert geomedia_blob.coerce_blob_bytes("") is None


# --------------------------------------------------------------------------
# Export: existing geometry types keep working
# --------------------------------------------------------------------------

def test_linestring_blob_still_imports(tmp_path):
    result = _export(tmp_path, ["Id", "Geometry"], [(1, line_blob(TRACK))], "Geometry", 1)
    assert result["status"] == "success"
    assert result["geometry_types_found"] == ["LineString"]
    assert result["blob_decoded_count"] == 1
    assert result["xy_fallback_count"] == 0


def test_polygon_blob_still_imports(tmp_path):
    result = _export(tmp_path, ["Id", "Geometry"], [(1, polygon_blob(SQUARE))], "Geometry", 2)
    assert result["status"] == "success"
    assert result["geometry_types_found"] == ["Polygon"]


def test_point_blob_imports(tmp_path):
    result = _export(tmp_path, ["Id", "Geometry"], [(1, point_blob(4.0, 5.0, -3.0))],
                     "Geometry", 3)
    assert result["status"] == "success"
    assert result["geometry_types_found"] == ["Point"]
    assert result["blob_decoded_count"] == 1
    assert _features(result, "Point")[0]["geometry"]["coordinates"] == [4.0, 5.0]


def test_polygon_with_hole_keeps_its_rings(tmp_path):
    result = _export(tmp_path, ["Id", "Geometry"], [(1, boundary_blob(SQUARE, HOLE))],
                     "Geometry", 10)
    assert result["geometry_types_found"] == ["Polygon"]
    assert len(_features(result, "Polygon")[0]["geometry"]["coordinates"]) == 2


def test_mixed_geometry_output_remains_split(tmp_path):
    rows = [(1, line_blob(TRACK)), (2, point_blob(9.0, 9.0, -1.0))]
    result = _export(tmp_path, ["Id", "Geometry"], rows, "Geometry", 10)
    assert sorted(result["geometry_types_found"]) == ["LineString", "Point"]
    assert len(result["outputs"]) == 2


# --------------------------------------------------------------------------
# Coordinate-pair fallback
# --------------------------------------------------------------------------

def test_code_10_unparseable_blob_falls_back_to_easting_northing(tmp_path):
    cols = ["Contact_Number", "Amplitude", "Easting", "Northing", "CoordGeocodePoint"]
    rows = [
        ("C1", 12.5, 500100.25, 6000200.75, b"\x99" * 40),
        ("C2", 9.0, 500110.0, 6000210.0, b"\x99" * 40),
    ]
    result = _export(tmp_path, cols, rows, "CoordGeocodePoint", 10)

    assert result["status"] == "success"
    assert result["row_count"] == 2
    assert result["non_null_geometry_count"] == 2
    assert result["blob_decoded_count"] == 0
    assert result["xy_fallback_count"] == 2
    assert result["geometry_types_found"] == ["Point"]

    features = _features(result, "Point")
    assert features[0]["geometry"]["coordinates"] == [500100.25, 6000200.75]
    assert features[0]["properties"]["geometry_source"] == "xy_fallback"


def test_fallback_uses_pointz_when_an_explicit_depth_exists(tmp_path):
    cols = ["Easting", "Northing", "Depth", "Geometry"]
    rows = [(500100.0, 6000200.0, -37.5, None)]
    result = _export(tmp_path, cols, rows, "Geometry", 10)

    coordinates = _features(result, "Point")[0]["geometry"]["coordinates"]
    assert coordinates == [500100.0, 6000200.0, -37.5]
    assert result["xy_fallback_count"] == 1


def test_depth_is_never_used_as_the_horizontal_ordinate():
    assert worker.find_candidate_coordinate_pair(["Easting", "Depth"]) is None
    assert worker.find_candidate_coordinate_pair(["X", "Depth"]) is None
    # Mixing families is not allowed either.
    assert worker.find_candidate_coordinate_pair(["Easting", "Latitude"]) is None


def test_field_matching_is_case_insensitive():
    assert worker.find_candidate_coordinate_pair(["EASTING", "northing"]) == ("EASTING", "northing")
    assert worker.find_candidate_coordinate_pair(["Lon", "LAT"]) == ("Lon", "LAT")
    assert worker.find_candidate_z_field(["ELEVATION"]) == "ELEVATION"


def test_invalid_or_non_finite_coordinates_are_rejected(tmp_path):
    cols = ["Easting", "Northing", "Geometry"]
    rows = [
        (None, 6000200.0, None),
        ("", 6000200.0, None),
        (float("nan"), 6000200.0, None),
        (500100.0, float("inf"), None),
        ("not-a-number", 6000200.0, None),
        (True, False, None),
    ]
    result = _export(tmp_path, cols, rows, "Geometry", 10)

    assert result["xy_fallback_count"] == 0
    assert result["invalid_geometry_count"] == len(rows)
    assert result["status"] == "no_geometry"
    assert result["outputs"] == {}


def test_numeric_strings_are_accepted_as_coordinates(tmp_path):
    result = _export(tmp_path, ["Easting", "Northing", "Geometry"],
                     [(" 500100.5 ", "6000200.5", None)], "Geometry", 10)
    assert result["xy_fallback_count"] == 1


def test_fallback_can_be_disabled(tmp_path):
    result = _export(tmp_path, ["Easting", "Northing", "Geometry"],
                     [(1.0, 2.0, None)], "Geometry", 10, allow_xy_fallback=False)
    assert result["xy_fallback_count"] == 0
    assert result["status"] == "no_geometry"


def test_blob_geometry_wins_over_the_fallback(tmp_path):
    cols = ["Easting", "Northing", "Geometry"]
    rows = [(1.0, 2.0, point_blob(500.0, 600.0, -1.0))]
    result = _export(tmp_path, cols, rows, "Geometry", 10)
    assert result["blob_decoded_count"] == 1
    assert result["xy_fallback_count"] == 0
    feature = _features(result, "Point")[0]
    assert feature["geometry"]["coordinates"] == [500.0, 600.0]
    assert feature["properties"]["geometry_source"] == "blob"


# --------------------------------------------------------------------------
# Structured diagnostics
# --------------------------------------------------------------------------

def test_empty_table_reports_empty(tmp_path):
    result = _export(tmp_path, ["Id", "Geometry"], [], "Geometry", 2)
    assert result["status"] == "empty"
    assert result["row_count"] == 0
    assert result["outputs"] == {}


def test_populated_table_without_parseable_geometry_reports_parse_failed(tmp_path):
    rows = [(1, b"\x99" * 40), (2, b"\x99" * 40)]
    result = _export(tmp_path, ["Id", "Geometry"], rows, "Geometry", 10)
    assert result["status"] == "parse_failed"
    assert result["row_count"] == 2
    assert result["non_null_geometry_count"] == 2
    assert result["blob_decoded_count"] == 0
    assert result["outputs"] == {}
    assert result["message"]


def test_missing_geometry_column_reports_no_geometry(tmp_path):
    result = _export(tmp_path, ["Id", "Description"], [(1, "a"), (2, "b")],
                     "NotAColumn", 10)
    assert result["status"] == "no_geometry"
    assert result["row_count"] == 2


def test_geometry_field_name_is_matched_case_insensitively(tmp_path):
    result = _export(tmp_path, ["Id", "GEOMETRY"], [(1, line_blob(TRACK))], "geometry", 1)
    assert result["status"] == "success"
    assert result["blob_decoded_count"] == 1


def test_attribute_values_are_preserved(tmp_path):
    cols = ["Contact_Number", "Amplitude", "Description", "Easting", "Northing", "Geometry"]
    rows = [("C-17", 12.5, "wreck", 500100.0, 6000200.0, None)]
    result = _export(tmp_path, cols, rows, "Geometry", 10)

    props = _features(result, "Point")[0]["properties"]
    assert props["Contact_Number"] == "C-17"
    assert props["Amplitude"] == 12.5
    assert props["Description"] == "wreck"
    assert props["Easting"] == 500100.0
    assert props["Northing"] == 6000200.0
    assert props["source"] == "synthetic.mdb"
    assert "Geometry" not in props


def test_result_contract_has_every_documented_key():
    result = worker.make_table_result("T", "empty")
    assert set(result) >= {
        "table", "status", "row_count", "non_null_geometry_count",
        "blob_decoded_count", "xy_fallback_count", "invalid_geometry_count",
        "geometry_types_found", "outputs", "message",
    }


def test_diagnostics_never_leak_paths_or_long_literals():
    message = worker.sanitise_diagnostic(
        r"failed on C:\Projects\ClientX\Survey 2024\data.mdb value b'\x99\x99...'")
    assert "ClientX" not in message
    assert "<path>" in message

    long_literal = worker.sanitise_diagnostic("blob was '" + "A" * 200 + "'")
    assert "AAAA" not in long_literal


def test_export_messages_contain_no_record_values(tmp_path):
    cols = ["Contact_Number", "Easting", "Northing", "Geometry"]
    rows = [("SECRET-ID", 123456.789, 987654.321, b"\x99" * 40)]
    result = _export(tmp_path, cols, rows, "Geometry", 10, allow_xy_fallback=False)
    serialised = json.dumps(result)
    assert "SECRET-ID" not in serialised
    assert "123456.789" not in serialised
    assert "\\x99" not in serialised


def test_output_paths_with_spaces_are_supported(tmp_path):
    directory = os.path.join(str(tmp_path), "Survey Data 2024")
    os.makedirs(directory)
    result = worker._write_rows_to_geojson(
        os.path.join(directory, "my file.mdb"),
        "Table With Spaces",
        ["Id", "Geometry"],
        [(1, line_blob(TRACK))],
        "Geometry",
        1,
        os.path.join(directory, "out base"),
        split=True,
        row_count_hint=1,
    )
    assert result["status"] == "success"
    assert os.path.isfile(result["outputs"]["LineString"])


def test_max_features_limit_is_reported(tmp_path):
    rows = [(i, line_blob(TRACK)) for i in range(5)]
    result = _export(tmp_path, ["Id", "Geometry"], rows, "Geometry", 1, max_features=2)
    assert result["row_count"] == 2
    assert "row limit" in result["message"]


# --------------------------------------------------------------------------
# Discovery and classification
# --------------------------------------------------------------------------

def test_classification_of_table_kinds():
    assert worker.classify_table("GFeatures", ["FeatureName"]) == "metadata"
    assert worker.classify_table("MSysObjects", ["Name"]) == "metadata"
    assert worker.classify_table("Bathy_Major", ["Geometry"], in_gfeatures=True) == "feature"
    assert worker.classify_table("Sediment_Classification_Name", ["Id", "Name"]) == "companion"
    assert worker.classify_table("Boulders_Text", ["Id", "Text"]) == "companion"
    assert worker.classify_table("Mag_Contact_ID", ["Easting", "Northing"]) == "spatial_candidate"
    assert worker.classify_table("Contacts", ["CoordGeocodePoint"]) == "spatial_candidate"
    assert worker.classify_table("Sediment_Classification", ["Id", "Description"]) == "non_spatial"


def _patch_inventory(monkeypatch, registered, inventory):
    registered_upper = {name.upper(): name for name in registered}
    monkeypatch.setattr(
        worker, "inventory_tables",
        lambda mdb_path, budget_seconds=None: (registered, registered_upper, inventory))


def test_secondary_geometry_column_recovers_rows_with_a_null_primary(tmp_path):
    """GFeatures names only the primary geometry column."""
    cols = ["Id", "LinearGeometry", "CoordGeocodePoint"]
    rows = [
        (1, line_blob(TRACK), None),
        (2, None, point_blob(500.0, 600.0, -3.0)),
        (3, None, None),
    ]
    result = _export(tmp_path, cols, rows, "LinearGeometry", 1)

    assert result["row_count"] == 3
    assert result["non_null_geometry_count"] == 1
    assert result["blob_decoded_count"] == 1
    assert result["secondary_blob_decoded_count"] == 1
    assert result["invalid_geometry_count"] == 1
    assert sorted(result["geometry_types_found"]) == ["LineString", "Point"]
    assert sorted(result["geometry_fields_used"]) == ["CoordGeocodePoint", "LinearGeometry"]

    feature = _features(result, "Point")[0]
    assert feature["properties"]["geometry_source"] == "secondary_blob"


def test_secondary_geometry_never_overrides_a_decodable_primary(tmp_path):
    cols = ["Id", "LinearGeometry", "CoordGeocodePoint"]
    rows = [(1, line_blob(TRACK), point_blob(1.0, 2.0, 3.0))]
    result = _export(tmp_path, cols, rows, "LinearGeometry", 1)

    assert result["blob_decoded_count"] == 1
    assert result["secondary_blob_decoded_count"] == 0
    assert result["geometry_types_found"] == ["LineString"]


def test_secondary_geometry_can_be_disabled(tmp_path):
    cols = ["Id", "LinearGeometry", "CoordGeocodePoint"]
    rows = [(1, None, point_blob(1.0, 2.0, 3.0))]
    result = _export(tmp_path, cols, rows, "LinearGeometry", 1,
                     allow_secondary_geometry=False)
    assert result["secondary_blob_decoded_count"] == 0
    assert result["status"] == "no_geometry"


def test_non_blob_columns_matching_geometry_hints_are_harmless(tmp_path):
    cols = ["Id", "LinearGeometry", "CoordGeocodeStatus", "CoordGeocodePoint_sk"]
    rows = [(1, None, "Geocoded", "abc")]
    result = _export(tmp_path, cols, rows, "LinearGeometry", 1)
    assert result["secondary_blob_decoded_count"] == 0
    assert result["status"] == "no_geometry"


def test_default_discovery_does_not_inspect_every_table(monkeypatch):
    """Describing every physical table is far too slow to do on every import."""
    called = []
    monkeypatch.setattr(
        worker, "inventory_tables",
        lambda *args, **kwargs: called.append(1) or ({}, {}, []))
    monkeypatch.setattr(
        worker, "list_feature_tables",
        lambda mdb_path: {"Bathy_Major": {"geom_field_name": "Geometry",
                                          "geometry_type_code": 1}})
    monkeypatch.delenv("SUBSEA_MDB_SCHEMA_DISCOVERY", raising=False)

    envelope = worker.discover_tables("ignored.mdb")
    assert called == []
    assert envelope["schema_discovery"] is False
    assert envelope["tables"]["Bathy_Major"]["discovery"] == "gfeatures"
    assert envelope["non_spatial"] == []


def test_schema_discovery_is_enabled_by_environment(monkeypatch):
    monkeypatch.setenv("SUBSEA_MDB_SCHEMA_DISCOVERY", "1")
    _patch_inventory(monkeypatch, {}, [
        {"table": "Mag_Contact_ID", "in_gfeatures": False,
         "columns": ["Easting", "Northing"], "row_count": 17},
    ])
    envelope = worker.discover_tables("ignored.mdb")
    assert envelope["schema_discovery"] is True
    assert "Mag_Contact_ID" in envelope["tables"]


def test_unregistered_spatial_table_is_discovered_conservatively(monkeypatch):
    registered = {"Bathy_Major": {"geom_field_name": "Geometry", "geometry_type_code": 1}}
    inventory = [
        {"table": "Bathy_Major", "in_gfeatures": True,
         "columns": [], "row_count": None},
        {"table": "GFeatures", "in_gfeatures": False,
         "columns": ["FeatureName"], "row_count": 7},
        {"table": "Mag_Contact_ID", "in_gfeatures": False,
         "columns": ["Contact_Number", "Easting", "Northing", "CoordGeocodePoint"],
         "row_count": 17},
    ]
    _patch_inventory(monkeypatch, registered, inventory)

    envelope = worker.discover_tables("ignored.mdb", include_schema_discovery=True)
    tables = envelope["tables"]

    assert tables["Bathy_Major"]["discovery"] == "gfeatures"
    assert "GFeatures" not in tables
    assert tables["Mag_Contact_ID"]["discovery"] == "schema"
    assert tables["Mag_Contact_ID"]["geom_field_name"] == "CoordGeocodePoint"
    assert tables["Mag_Contact_ID"]["coordinate_pair"] == ["Easting", "Northing"]


def test_metadata_and_companion_tables_are_not_offered_as_layers(monkeypatch):
    inventory = [
        {"table": "GAliasTable", "in_gfeatures": False, "columns": ["Id"], "row_count": 3},
        {"table": "MSysObjects", "in_gfeatures": False, "columns": ["Name"], "row_count": 90},
        {"table": "Sediment_Classification_Name", "in_gfeatures": False,
         "columns": ["Id", "Name"], "row_count": 5},
        {"table": "Boulders_Text", "in_gfeatures": False,
         "columns": ["Id", "Easting", "Northing"], "row_count": 5},
    ]
    _patch_inventory(monkeypatch, {}, inventory)

    envelope = worker.discover_tables("ignored.mdb", include_schema_discovery=True)
    assert envelope["tables"] == {}

    reported = {entry["table"]: entry for entry in envelope["non_spatial"]}
    assert set(reported) == {"Sediment_Classification_Name", "Boulders_Text"}
    assert reported["Boulders_Text"]["classification"] == "companion"


def test_non_spatial_table_is_reported_without_invented_geometry(monkeypatch):
    inventory = [
        {"table": "Sediment_Classification", "in_gfeatures": False,
         "columns": ["Id", "Description", "Sediment_Code"], "row_count": 12},
    ]
    _patch_inventory(monkeypatch, {}, inventory)

    envelope = worker.discover_tables("ignored.mdb", include_schema_discovery=True)
    assert envelope["tables"] == {}
    entry = envelope["non_spatial"][0]
    assert entry["table"] == "Sediment_Classification"
    assert entry["classification"] == "non_spatial"
    assert entry["row_count"] == 12
    assert "no geometry" in entry["reason"]


# --------------------------------------------------------------------------
# Text (graphic) feature classes
# --------------------------------------------------------------------------

def test_text_table_exports_points_with_label_text(tmp_path):
    # Real GeoMedia text tables: TextGeometry BLOB plus a TextGeometry_sk
    # spatial key, and an empty PrimaryGeometryFieldName in GFeatures.
    rows = [
        (1, text_blob(107.15, -5.89, "Possible GRAVEL patch"), b"\x01\x02"),
        (2, text_blob(107.16, -5.90, "12°"), b"\x03\x04"),
    ]
    result = _export(tmp_path, ["ID1", "TextGeometry", "TextGeometry_sk"],
                     rows, "", 33)
    assert result["status"] == "success"
    assert result["geometry_types_found"] == ["Point"]
    assert result["geometry_fields_used"] == ["TextGeometry"]
    features = _features(result, "Point")
    assert [f["properties"]["label_text"] for f in features] == [
        "Possible GRAVEL patch", "12°"]
    assert features[0]["geometry"]["coordinates"] == [107.15, -5.89]
    # The spatial key is not mistaken for the geometry BLOB.
    assert features[0]["properties"]["TextGeometry_sk"] == "<binary:2>"


def test_text_table_resolves_a_graphictext_column(tmp_path):
    # Bathy slope annotations name the BLOB column GraphicText.
    rows = [(1, text_blob(108.07, -4.07, "9°"), None)]
    result = _export(tmp_path, ["ID1", "GraphicText", "GraphicText_sk"], rows, "", 33)
    assert result["status"] == "success"
    assert result["geometry_fields_used"] == ["GraphicText"]
    assert _features(result, "Point")[0]["properties"]["label_text"] == "9°"


def test_text_table_null_geometry_rows_are_skipped_not_fatal(tmp_path):
    rows = [
        (1, None, None),
        (2, text_blob(1.0, 2.0, "abc"), None),
    ]
    result = _export(tmp_path, ["ID1", "TextGeometry", "TextGeometry_sk"], rows, "", 33)
    assert result["status"] == "success"
    assert result["written"] == 1
    assert result["row_count"] == 2


def test_spatial_key_columns_are_not_geometry_candidates():
    assert worker.find_candidate_geometry_fields(
        ["ID1", "TextGeometry", "TextGeometry_sk"]) == ["TextGeometry"]
    assert worker.find_candidate_geometry_fields(
        ["ID1", "GraphicText", "GraphicText_sk"]) == ["GraphicText"]


def test_non_text_tables_do_not_gain_a_label_field(tmp_path):
    result = _export(tmp_path, ["Id", "Geometry"], [(1, line_blob(TRACK))], "Geometry", 1)
    props = _features(result, "LineString")[0]["properties"]
    assert "label_text" not in props


# --------------------------------------------------------------------------
# Preserved behaviour
# --------------------------------------------------------------------------

def test_gfeatures_interpretation_is_unchanged():
    col_names = ["GeometryType", "Name", "PrimaryGeometryFieldName", "Description"]
    rows = [
        (1, "Bathy_Major", "LinearGeometry", ""),
        (33, "Coverage_Image", "Raster", ""),
        (33, "Description", None, ""),
        (2, "Areas", "AreaGeometry", ""),
        ("bad", "Broken", "Geom", ""),
        (1, None, "Geom", ""),
    ]
    assert worker._feature_tables_from_gfeatures(col_names, rows) == {
        "Bathy_Major": {"geom_field_name": "LinearGeometry", "geometry_type_code": 1},
        "Coverage_Image": {"geom_field_name": "Raster", "geometry_type_code": 33},
        "Description": {"geom_field_name": "", "geometry_type_code": 33},
        "Areas": {"geom_field_name": "AreaGeometry", "geometry_type_code": 2},
    }


def test_metadata_led_classification_is_preserved_for_simple_shapes():
    # A polygon BLOB in a line feature class still imports as a line.
    decoded = geomedia_blob.decode_geometry_blob(polygon_blob(SQUARE))
    assert worker.output_kind_for_geometry(decoded, 1) == "LineString"
    assert worker.output_kind_for_geometry(decoded, 2) == "Polygon"
    assert worker.output_kind_for_geometry(decoded, 10) == "LineString"
    single = geomedia_blob.decode_geometry_blob(point_blob(1.0, 2.0))
    assert worker.output_kind_for_geometry(single, 1) == "Point"


def test_depth_property_still_averages_blob_z(tmp_path):
    result = _export(tmp_path, ["Id", "Geometry"], [(1, line_blob(TRACK))], "Geometry", 1)
    depth = _features(result, "LineString")[0]["properties"]["depth"]
    assert math.isclose(depth, (-5.0 - 6.0 - 7.0) / 3.0)
