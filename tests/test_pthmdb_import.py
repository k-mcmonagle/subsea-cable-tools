# -*- coding: utf-8 -*-
"""QGIS-backed checks for the path file → workbench RPL import.

Covers the pure model builder (label → event mapping, KP unit conversion,
segment attributes, assembly-point label matching, depth-profile
interpolation) and end-to-end registration through the shared commit path.
Real anonymised .pthmdb files are used when the local (gitignored) ref/
folder holds them; the fabricated-data tests always run.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile

from ..processing.pthmdb_reader import PathFileData, read_path_file
from ..workbench.pthmdb_import import model_from_path_data
from ..workbench.rpl_import_service import (
    CommitRequest, commit_import, make_wgs84_distance_area, read_import_audit,
)
from ..workbench.store import WorkbenchStore

_REF_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ref")
_REF_FILES = sorted(
    os.path.join(_REF_DIR, name) for name in (
        os.listdir(_REF_DIR) if os.path.isdir(_REF_DIR) else [])
    if name.lower().endswith(".pthmdb"))


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         (" — " + detail) if detail else ""))
    return bool(ok)


def _temp_store() -> WorkbenchStore:
    folder = tempfile.mkdtemp(prefix="pthmdb_import_")
    store = WorkbenchStore(os.path.join(folder, "wb.gpkg"))
    store.migrate()
    return store


def _fake_data() -> PathFileData:
    """Hand-built equivalent of a small decoded path file (KP in metres)."""
    data = PathFileData("fake.pthmdb")
    data.crs_auth_id = "EPSG:4326"
    data.crs_note = "test"
    data.kp_unit = "m"
    coords = [(0.00, 50.0), (0.02, 50.0), (0.04, 50.0), (0.06, 50.0)]
    kps = [0.0, 1432.0, 2864.0, 4296.0]
    labels = ["BMH West", "", "UnDef", "BU1"]
    for i, ((lon, lat), kp, label) in enumerate(zip(coords, kps, labels)):
        data.path_points.append({
            "Index": i, "Label": label, "Comment": "note" if i == 1 else "",
            "KP": kp, "CableDist": kp * 1.02, "Depth": 0.0,
            "x": lon, "y": lat, "z": 0.0,
        })
    for i in range(3):
        data.path_lines.append({
            "Index": i, "Bearing": 90.0, "dKP": 1432.0,
            "SegCableDist": 1432.0 * 1.02,
            "CableType": "DA" if i == 0 else "UnDef", "Buried": "Y",
            "vertices": [coords[i] + (0.0,), coords[i + 1] + (0.0,)],
        })
    # One matchable joint next to position 2 (unlabelled), one too far away.
    data.assembly_points = [
        {"Label": "JT1", "Type": "CABLE", "KP": 2870.0},
        {"Label": "GHOST", "Type": "CABLE", "KP": 3600.0},
    ]
    data.profile = [
        {"Kp": 0.0, "Depth": 10.0},
        {"Kp": 4296.0, "Depth": 20.0},
    ]
    return data


def test_model_mapping() -> bool:
    model, audit, warnings = model_from_path_data(_fake_data(), da=None)
    points, segments = model.points, model.segments
    ok = len(points) == 4 and len(segments) == 3
    ok = ok and points[0].event == "BMH West" and points[3].event == "BU1"
    ok = ok and points[1].event == ""          # blank stays blank mid-route
    ok = ok and points[1].attrs.get("Remarks") == "note"
    ok = ok and abs(points[1].dist_cum_km - 1.432) < 1e-9
    ok = ok and abs(points[1].cable_dist_cum_km - 1.432 * 1.02) < 1e-9
    ok = ok and segments[0].attrs.get("CableType") == "DA"
    ok = ok and "CableType" not in segments[1].attrs   # UnDef dropped
    ok = ok and segments[0].attrs.get("Buried") == "Y"
    ok = ok and abs(segments[0].dist_km - 1.432) < 1e-9
    ok = ok and segments[0].slack_pct is None  # implied later by reconcile
    ok = ok and audit["method"] == "pthmdb" and audit["kp_unit"] == "m"
    return _result("path data → model field mapping", ok,
                   f"warnings={warnings}")


def test_assembly_point_matching() -> bool:
    model, audit, warnings = model_from_path_data(_fake_data(), da=None)
    # JT1 lands on the unlabelled position 3 (KP 2.864 vs 2.870, within 100 m);
    # GHOST is 700+ m from every position and must warn instead.
    ok = model.points[2].event == "JT1"
    entries = {e["label"]: e for e in audit["assembly_points"]}
    ok = ok and entries["JT1"]["matched_pos_no"] == 3
    ok = ok and entries["GHOST"]["matched_pos_no"] is None
    ok = ok and any("GHOST" in w for w in warnings)
    return _result("assembly points label nearest position / warn when far", ok)


def test_profile_interpolation() -> bool:
    model, audit, _warnings = model_from_path_data(_fake_data(), da=None)
    # Linear 10 → 20 m over the route; position 2 sits at 2/3 of the span.
    ok = abs(model.points[0].depth_m - 10.0) < 1e-9
    ok = ok and abs(model.points[2].depth_m - (10.0 + 10.0 * 2864.0 / 4296.0)) < 1e-6
    ok = ok and audit["depth_profile"]["sample_count"] == 2
    ok = ok and audit["depth_profile"]["applied_to_points"] == 4
    return _result("depth profile interpolates onto positions", ok)


def test_end_labels_default() -> bool:
    data = _fake_data()
    data.path_points[0]["Label"] = ""
    data.path_points[-1]["Label"] = "UnDef"
    data.assembly_points = []
    model, _audit, _warnings = model_from_path_data(data, da=None)
    ok = model.points[0].event == "A End" and model.points[-1].event == "B End"
    return _result("unlabelled ends default to A End / B End", ok)


def test_segment_mismatch_degrades() -> bool:
    data = _fake_data()
    data.path_lines = data.path_lines[:1]  # wrong count
    model, _audit, warnings = model_from_path_data(data, da=None)
    ok = len(model.segments) == len(model.points) - 1
    ok = ok and all(seg.dist_km is None for seg in model.segments)
    ok = ok and any("PathLines count" in w for w in warnings)
    return _result("segment count mismatch drops to derived segments", ok)


def test_unknown_kp_unit_leaves_distances_for_reconcile() -> bool:
    data = _fake_data()
    data.kp_unit = None
    da = make_wgs84_distance_area(None)
    model, _audit, warnings = model_from_path_data(data, da=da)
    ok = any("KP unit" in w for w in warnings)
    # reconcile derived cumulatives from geodesic spans (~1.43 km each)
    ok = ok and model.points[0].dist_cum_km == 0.0
    ok = ok and model.points[-1].dist_cum_km is not None
    ok = ok and 3.5 < model.points[-1].dist_cum_km < 5.0
    return _result("undetermined KP unit falls back to derived distances", ok,
                   f"end KP={model.points[-1].dist_cum_km}")


def test_commit_round_trip() -> bool:
    store = _temp_store()
    da = make_wgs84_distance_area(None)
    model, audit, _warnings = model_from_path_data(_fake_data(), da=da)
    result = commit_import(store, model, CommitRequest(
        route_name="Fake Segment", kind="planned", rev_label="Rev 1",
        source_file="fake.pthmdb", audit=audit))
    ok = bool(result.rpl_id)
    stored_audit = read_import_audit(store, result.rpl_id) or {}
    ok = ok and stored_audit.get("method") == "pthmdb"
    ok = ok and stored_audit.get("kp_unit") == "m"
    ok = ok and "derivation" in stored_audit
    rpl = store.get_rpl(result.rpl_id) or {}
    ok = ok and rpl.get("route_id")
    return _result("commit registers RPL with pthmdb audit", ok)


def test_reference_files_end_to_end() -> bool:
    if not _REF_FILES:
        return _result("reference .pthmdb end-to-end", True, "skipped (no ref files)")
    store = _temp_store()
    da = make_wgs84_distance_area(None)
    ok = True
    for path in _REF_FILES:
        data = read_path_file(path)
        model, audit, warnings = model_from_path_data(data, da=da)
        name = os.path.splitext(os.path.basename(path))[0]
        result = commit_import(store, model, CommitRequest(
            route_name=name, kind="planned", rev_label="Rev 1",
            source_file=path, audit=audit))
        ok = ok and bool(result.rpl_id)
        # File KP totals survive into the model (within numerical noise).
        expected_km = (data.path_points[-1]["KP"] - data.path_points[0]["KP"])
        if data.kp_unit == "m":
            expected_km /= 1000.0
        ok = ok and abs(model.total_route_km() - expected_km) < 0.005
        stated = sum(1 for p in model.points if p.event)
        detail = (f"{name}: {len(model.points)} pts, "
                  f"{model.total_route_km():.1f} km, {stated} events, "
                  f"warnings={warnings}")
        ok = _result(f"reference import {name}", ok, detail) and ok
    return ok


def run_all():
    return [
        test_model_mapping(),
        test_assembly_point_matching(),
        test_profile_interpolation(),
        test_end_labels_default(),
        test_segment_mismatch_degrades(),
        test_unknown_kp_unit_leaves_distances_for_reconcile(),
        test_commit_round_trip(),
        test_reference_files_end_to_end(),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(run_all()) else 1)
