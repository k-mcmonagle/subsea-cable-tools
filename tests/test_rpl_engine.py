# -*- coding: utf-8 -*-
"""Checks for the workbench RPL recompute engine.

Builds a small synthetic RPL (points along a meridian so geodesic distances
are predictable), then exercises recompute under both slack modes, the
move/insert/delete operations and their invariants, the KP <-> cable-distance
inverse pair, depth application, and validation findings.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

from qgis.core import QgsCoordinateReferenceSystem, QgsProject

from ..kp_range_utils import make_distance_area
from ..workbench import rpl_engine as eng
from ..workbench.rpl_engine import RplModel, RplPoint, RplSegment, SlackMode


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _da():
    return make_distance_area(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsProject.instance().transformContext(),
    )


def _model(n_points: int = 5, slack_pct: float = 1.0) -> RplModel:
    """Points every 0.01 deg of latitude along the 0 meridian (~1.112 km)."""
    points = [
        RplPoint(seq=i, pos_no=i + 1, event="", lat=50.0 + 0.01 * i, lon=0.0)
        for i in range(n_points)
    ]
    segments = [RplSegment(seq=i, slack_pct=slack_pct) for i in range(n_points - 1)]
    return RplModel(points=points, segments=segments)


def test_recompute_hold_slack() -> bool:
    da = _da()
    model = _model(5, slack_pct=2.0)
    eng.recompute(model, da, slack_mode=SlackMode.HOLD_SLACK)
    seg = model.segments[0]
    ok = seg.dist_km is not None and 1.0 < seg.dist_km < 1.3
    ok = ok and abs(seg.cable_dist_km - seg.dist_km * 1.02) < 1e-9
    ok = ok and seg.bearing_deg is not None and min(seg.bearing_deg, 360 - seg.bearing_deg) < 0.5  # due north
    total = model.points[-1].dist_cum_km
    sum_segs = sum(s.dist_km for s in model.segments)
    ok = ok and total is not None and abs(total - sum_segs) < 1e-9
    cable_total = model.points[-1].cable_dist_cum_km
    ok = ok and abs(cable_total - total * 1.02) < 1e-9
    return _result("recompute HOLD_SLACK distances/bearings/cumulatives", ok,
                   f"seg={seg.dist_km:.4f} km total={total:.4f} km")


def test_recompute_hold_cable() -> bool:
    da = _da()
    model = _model(3, slack_pct=None)
    # authoritative cable distances: 1.5 km per segment
    for seg in model.segments:
        seg.cable_dist_km = 1.5
    eng.recompute(model, da, slack_mode=SlackMode.HOLD_CABLE)
    seg = model.segments[0]
    expected_slack = (1.5 / seg.dist_km - 1.0) * 100.0
    ok = seg.slack_pct is not None and abs(seg.slack_pct - expected_slack) < 1e-9
    ok = ok and model.points[-1].cable_dist_cum_km == 3.0
    return _result("recompute HOLD_CABLE derives slack", ok,
                   f"slack={seg.slack_pct:.3f}%")


def test_move_point() -> bool:
    da = _da()
    model = _model(5, slack_pct=0.0)
    eng.recompute(model, da)
    before_total = model.points[-1].dist_cum_km
    # nudge the middle point east
    changed = eng.move_point(model, 2, model.points[2].lat, 0.02, da)
    after_total = model.points[-1].dist_cum_km
    ok = after_total > before_total  # detour must lengthen the route
    ok = ok and 2 in changed.point_indices
    ok = ok and {1, 2}.issubset(changed.segment_indices)
    # downstream cumulative points marked dirty
    ok = ok and {3, 4}.issubset(changed.point_indices)
    return _result("move_point lengthens route + dirty tracking", ok,
                   f"{before_total:.4f} -> {after_total:.4f} km")


def test_insert_and_delete_point() -> bool:
    da = _da()
    model = _model(4, slack_pct=1.0)
    eng.recompute(model, da)
    total_before = model.points[-1].dist_cum_km

    changed = eng.insert_point(model, 1, 50.015, 0.0, da)  # on the line: length unchanged
    ok = changed.structural and len(model.points) == 5 and len(model.segments) == 4
    ok = ok and model.points[2].pos_no is None  # document numbering not invented
    ok = ok and model.segments[2].slack_pct == 1.0  # inherited
    ok = ok and abs(model.points[-1].dist_cum_km - total_before) < 1e-6
    ok = ok and [p.seq for p in model.points] == [0, 1, 2, 3, 4]

    changed = eng.delete_point(model, 2, da)
    ok = ok and changed.structural and len(model.points) == 4 and len(model.segments) == 3
    ok = ok and abs(model.points[-1].dist_cum_km - total_before) < 1e-6
    return _result("insert/delete point keep invariants", ok)


def test_kp_cable_inverse() -> bool:
    da = _da()
    model = _model(6, slack_pct=3.0)
    eng.recompute(model, da)
    ok = True
    for kp in (0.0, 0.5, 1.7, model.points[-1].dist_cum_km):
        cable = eng.cable_dist_from_kp(model, kp)
        back = eng.kp_from_cable_dist(model, cable)
        ok = ok and cable is not None and back is not None and abs(back - kp) < 1e-9
        ok = ok and abs(cable - kp * 1.03) < 1e-9  # uniform slack
    ok = ok and eng.cable_dist_from_kp(model, -1.0) is None
    ok = ok and eng.kp_from_cable_dist(model, 1e6) is None
    return _result("KP <-> cable distance inverse consistency", ok)


def test_point_at_kp_and_bearing() -> bool:
    da = _da()
    model = _model(5, slack_pct=0.0)
    eng.recompute(model, da)
    seg_km = model.segments[0].dist_km
    pos = eng.point_at_kp(model, seg_km * 1.5, da)
    ok = pos is not None and abs(pos[0] - 50.015) < 1e-6 and abs(pos[1]) < 1e-9
    bearing = eng.bearing_at_kp(model, seg_km * 1.5)
    ok = ok and bearing is not None and (bearing < 0.5 or bearing > 359.5)
    return _result("point_at_kp + bearing_at_kp", ok, f"pos={pos}")


def test_apply_depths_and_validate() -> bool:
    da = _da()
    model = _model(4)
    eng.recompute(model, da)
    changed = eng.apply_depths(model, lambda lat, lon: -100.0 - lat, indices=[1, 2])
    ok = model.points[1].depth_m is not None and model.points[0].depth_m is None
    ok = ok and changed.point_indices == {1, 2}

    findings = eng.validate(model)
    ok = ok and findings == []

    bad = _model(3)
    bad.points[1].lat = 95.0
    findings = eng.validate(bad)
    ok = ok and any(f["rule_id"] == "rpl.coordinate_range" for f in findings)
    return _result("apply_depths + validate", ok)


def test_derive_slack() -> bool:
    da = _da()
    model = _model(3, slack_pct=None)
    eng.recompute(model, da, slack_mode=SlackMode.HOLD_SLACK)
    for seg in model.segments:
        seg.slack_pct = None
        seg.cable_dist_km = seg.dist_km * 1.05
    n = eng.derive_slack(model)
    ok = n == 2 and all(abs(s.slack_pct - 5.0) < 1e-9 for s in model.segments)
    return _result("derive_slack from cable distances", ok)


def test_event_to_event_sections() -> bool:
    model = _model(6, slack_pct=2.0)
    model.points[0].event = "BMH East"
    model.points[2].event = "RPT-1"
    model.points[4].event = "BU-1"
    model.points[5].event = "Landing West"
    for index, segment in enumerate(model.segments):
        segment.attrs["CableType"] = "LW" if index < 2 else "DA"
    eng.recompute(model, _da())
    sections = eng.event_sections(model)
    ok = len(sections) == 3
    ok = ok and [(s.start_point_index, s.end_point_index) for s in sections] == [
        (0, 2), (2, 4), (4, 5)]
    ok = ok and sections[0].from_event == "BMH East"
    ok = ok and sections[1].to_event == "BU-1"
    ok = ok and sections[0].leg_count == 2 and sections[2].leg_count == 1
    ok = ok and abs(sections[0].slack_pct - 2.0) < 1e-9
    ok = ok and sections[0].attrs["CableType"] == "LW"
    ok = ok and sections[1].attrs["CableType"] == "DA"
    model.segments[2].attrs["ProtectionMethod"] = "Burial"
    mixed = eng.event_sections(model)[1].attrs["ProtectionMethod"]
    ok = ok and mixed == "Mixed: Burial | (blank)"
    return _result("RPL sections are derived between event positions", ok)


def run_all() -> list:
    return [
        test_recompute_hold_slack(),
        test_recompute_hold_cable(),
        test_move_point(),
        test_insert_and_delete_point(),
        test_kp_cable_inverse(),
        test_point_at_kp_and_bearing(),
        test_apply_depths_and_validate(),
        test_derive_slack(),
        test_event_to_event_sections(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)
