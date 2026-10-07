# -*- coding: utf-8 -*-
"""Pure-Python checks for GeoMedia coordinate-system detection (no QGIS).

Synthetic GCoordSystem / GeometryProperties / FieldLookup rows cover the
storage variants the MDB and path-file importers meet: geographic WGS84,
WGS84 UTM, other datums (hinted, never guessed), unusual storage, several
coordinate systems per file and both FieldLookup column namings.

Run directly: ``python tests/run_pure_tests.py test_geomedia_crs``.
"""

from __future__ import annotations

import math
import sys

from ..processing import geomedia_crs as gc

REQUIRES_QGIS = False

_GEO_GUID = "{AAAAAAAA-0000-0000-0000-000000000001}"
_PLACEHOLDER_GUID = "{AAAAAAAA-0000-0000-0000-000000000002}"
_UTM_GUID = "{AAAAAAAA-0000-0000-0000-000000000003}"


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def _geographic(**overrides):
    row = {
        "CSGUID": _GEO_GUID, "BaseStorageType": 1,
        "Stor2CompMatrix1": math.pi / 180.0, "Stor2CompMatrix2": 0.0,
        "Stor2CompMatrix4": 0.0, "Stor2CompMatrix5": 0.0,
        "Stor2CompMatrix6": math.pi / 180.0, "Stor2CompMatrix8": 0.0,
        "GeodeticDatum": 17, "Ellipsoid": 22,
        "EquatorialRadius": 6378137.0, "InverseFlattening": 298.25722356300156,
        "ProjAlgorithm": 39, "LonOfOrigin": 0.0, "ScaleReductFact": 1.0,
    }
    row.update(overrides)
    return row


def _utm(zone, south=False, **overrides):
    row = {
        "CSGUID": _UTM_GUID, "BaseStorageType": 0,
        "Stor2CompMatrix1": 1.0, "Stor2CompMatrix6": 1.0,
        "GeodeticDatum": 17, "EquatorialRadius": 6378137.0,
        "InverseFlattening": 298.257223563,
        "LonOfOrigin": math.radians(zone * 6 - 183), "LatOfOrigin": 0.0,
        "ScaleReductFact": 0.9996, "FalseX": 500000.0,
        "FalseY": 10000000.0 if south else 0.0,
    }
    row.update(overrides)
    return row


def test_geographic_wgs84() -> bool:
    d = gc.detect_coordinate_system(_geographic())
    return _result("geographic degrees on WGS84 -> EPSG:4326",
                   d.auth_id == "EPSG:4326" and "WGS84" in d.description, d.explain())


def test_partial_row_infers_storage_from_matrix() -> bool:
    row = {"Stor2CompMatrix1": math.pi / 180.0, "EquatorialRadius": 6378137.0,
           "InverseFlattening": 298.25722356300156}
    metres = dict(row, Stor2CompMatrix1=1.0)
    ok = (gc.detect_coordinate_system(row).auth_id == "EPSG:4326"
          and gc.detect_coordinate_system(metres).auth_id is None)
    return _result("rows without BaseStorageType infer the space from the matrix", ok)


def test_geographic_other_datums_are_hinted_not_guessed() -> bool:
    airy = gc.detect_coordinate_system(_geographic(
        EquatorialRadius=6377563.396, InverseFlattening=299.3249646, GeodeticDatum=5))
    grs80 = gc.detect_coordinate_system(_geographic(
        EquatorialRadius=6378137.0, InverseFlattening=298.257222101, GeodeticDatum=40))
    wrong_datum = gc.detect_coordinate_system(_geographic(GeodeticDatum=3))
    ok = (airy.auth_id is None and "Airy 1830" in airy.reason and "EPSG:4277" in airy.hint
          and grs80.auth_id is None and "ETRS89" in grs80.hint
          and wrong_datum.auth_id is None and "datum code" in wrong_datum.reason)
    return _result("non-WGS84 datums are reported with hints, never assigned", ok,
                   f"{airy.explain()} | {grs80.explain()} | {wrong_datum.explain()}")


def test_unusual_storage_is_rejected() -> bool:
    radians = gc.detect_coordinate_system(_geographic(
        Stor2CompMatrix1=1.0, Stor2CompMatrix6=1.0))
    rotated = gc.detect_coordinate_system(_geographic(Stor2CompMatrix2=0.5))
    offset = gc.detect_coordinate_system(_geographic(Stor2CompMatrix4=1000.0))
    scaled = gc.detect_coordinate_system(_utm(31, Stor2CompMatrix1=0.001, Stor2CompMatrix6=0.001))
    ok = (radians.auth_id is None and "radians" in radians.reason
          and rotated.auth_id is None and "rotates" in rotated.reason
          and offset.auth_id is None and "offsets" in offset.reason
          and scaled.auth_id is None and "scale factor" in scaled.reason)
    return _result("radians, rotated, offset and scaled storage are not assigned", ok)


def test_utm_wgs84() -> bool:
    north = gc.detect_coordinate_system(_utm(31))
    south = gc.detect_coordinate_system(_utm(56, south=True))
    degrees_cm = gc.detect_coordinate_system(_utm(10, LonOfOrigin=-123.0))
    zone_only = gc.detect_coordinate_system(_utm(
        31, LonOfOrigin=None, ScaleReductFact=None, FalseX=None, Zone=31))
    ok = (north.auth_id == "EPSG:32631" and south.auth_id == "EPSG:32756"
          and degrees_cm.auth_id == "EPSG:32610" and zone_only.auth_id == "EPSG:32631")
    return _result("WGS84 UTM north/south from the central meridian or zone", ok,
                   f"{north.auth_id} {south.auth_id} {degrees_cm.auth_id} {zone_only.auth_id}")


def test_utm_ambiguities_are_reported() -> bool:
    no_hemisphere = gc.detect_coordinate_system(_utm(31, FalseY=None))
    disagree = gc.detect_coordinate_system(_utm(31, Zone=30))
    ed50 = gc.detect_coordinate_system(_utm(
        31, EquatorialRadius=6378388.0, InverseFlattening=297.0, GeodeticDatum=8))
    lambert = gc.detect_coordinate_system(_utm(
        31, ScaleReductFact=1.0, FalseX=700000.0, ProjAlgorithm=12))
    ok = (no_hemisphere.auth_id is None and "hemisphere" in no_hemisphere.reason
          and disagree.auth_id is None and "disagrees" in disagree.reason
          and ed50.auth_id is None and "EPSG:23031" in ed50.hint
          and lambert.auth_id is None and "code 12" in lambert.reason)
    return _result("UTM without hemisphere, conflicting zone, other datums, other projections",
                   ok, f"{ed50.explain()} | {lambert.explain()}")


def test_named_coordinate_system_is_offered_as_hint() -> bool:
    d = gc.detect_coordinate_system(_utm(
        31, ScaleReductFact=1.0, Name="Local grid", Description="EPSG:2154 RGF93"))
    return _result("the file's own CS name is passed on as a hint",
                   d.auth_id is None and "EPSG:2154" in d.hint, d.hint)


def test_referenced_row_wins_over_placeholder() -> bool:
    placeholder = _geographic(CSGUID=_PLACEHOLDER_GUID, GeodeticDatum=99,
                              EquatorialRadius=6378388.0, InverseFlattening=297.0)
    used = _utm(31, CSGUID=_GEO_GUID)
    properties = [{"IndexID": 7, "GeometryType": 1, "PrimaryGeometryFlag": True,
                   "GCoordSystemGUID": _GEO_GUID}]
    lookup = [{"IndexID": 7, "Table": "Contours", "Field": "SpatialLine"}]
    report = gc.analyse_coordinate_systems([placeholder, used], properties, lookup)
    table = report.crs_for_table("contours")       # case-insensitive
    ok = (report.file_crs().auth_id == "EPSG:32631"
          and table is not None and table.auth_id == "EPSG:32631"
          and report.table_guids == {"Contours": _GEO_GUID.upper()})
    return _result("the CS the geometry references is used, not the first row", ok,
                   report.summary())


def test_both_field_lookup_namings_and_per_table_crs() -> bool:
    rows = [_geographic(), _utm(31)]
    properties = [
        {"IndexID": 1, "PrimaryGeometryFlag": True, "GCoordSystemGUID": _GEO_GUID},
        {"IndexID": 2, "PrimaryGeometryFlag": True, "GCoordSystemGUID": _UTM_GUID},
    ]
    old_style = [{"IndexID": 1, "FeatureName": "PathPoints", "FieldName": "Geometry"},
                 {"IndexID": 2, "FeatureName": "Grid", "FieldName": "Geometry"}]
    new_style = [{"IndexID": 1, "Table": "PathPoints", "Field": "Geometry"},
                 {"IndexID": 2, "Table": "Grid", "Field": "Geometry"}]
    ok = True
    for lookup in (old_style, new_style):
        report = gc.analyse_coordinate_systems(rows, properties, lookup)
        ok = ok and report.crs_for_table("PathPoints").auth_id == "EPSG:4326"
        ok = ok and report.crs_for_table("Grid").auth_id == "EPSG:32631"
        mixed = report.file_crs()
        ok = ok and mixed.auth_id is None and "several coordinate systems" in mixed.reason
    return _result("FieldLookup FeatureName/Table namings; per-table CRS; mixed file", ok)


def test_missing_linkage_and_missing_rows() -> bool:
    empty = gc.analyse_coordinate_systems([])
    single = gc.analyse_coordinate_systems([_geographic(CSGUID=None)])
    dangling = gc.analyse_coordinate_systems(
        [_geographic()], [{"IndexID": 1, "PrimaryGeometryFlag": True,
                           "GCoordSystemGUID": "{DEADBEEF-0000-0000-0000-000000000000}"}])
    unlinked_conflict = gc.analyse_coordinate_systems([_geographic(), _utm(31)])
    ok = (empty.file_crs() is None and "no GCoordSystem" in empty.summary()
          and single.file_crs().auth_id == "EPSG:4326"
          and dangling.file_crs().auth_id is None
          and "missing from GCoordSystem" in dangling.file_crs().reason
          and unlinked_conflict.file_crs().auth_id is None)
    return _result("no table, unkeyed row, dangling reference, unlinked conflict", ok)


def test_round_trip_through_json_dict() -> bool:
    report = gc.analyse_coordinate_systems(
        [_geographic()], [{"IndexID": 3, "PrimaryGeometryFlag": 1, "GCoordSystemGUID": _GEO_GUID}],
        [{"IndexID": 3, "FeatureName": "T", "FieldName": "G"}])
    copy = gc.FileCrsReport.from_dict(report.to_dict())
    error = gc.FileCrsReport.from_dict({"error": "boom"})
    ok = (copy.crs_for_table("T").auth_id == "EPSG:4326"
          and copy.summary() == report.summary()
          and error.file_crs() is None and "boom" in error.summary()
          and gc.FileCrsReport.from_dict(None).file_crs() is None)
    return _result("FileCrsReport survives the worker's JSON round trip", ok)


def run_all():
    return [
        test_geographic_wgs84(),
        test_partial_row_infers_storage_from_matrix(),
        test_geographic_other_datums_are_hinted_not_guessed(),
        test_unusual_storage_is_rejected(),
        test_utm_wgs84(),
        test_utm_ambiguities_are_reported(),
        test_named_coordinate_system_is_offered_as_hint(),
        test_referenced_row_wins_over_placeholder(),
        test_both_field_lookup_namings_and_per_table_crs(),
        test_missing_linkage_and_missing_rows(),
        test_round_trip_through_json_dict(),
    ]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0 if all(run_all()) else 1)
