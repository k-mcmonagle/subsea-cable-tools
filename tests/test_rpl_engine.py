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


# ---------------------------------------------------------------------------
# Indexed KP lookups vs the original linear walks
# ---------------------------------------------------------------------------
# Verbatim copies of the pre-bisection implementations: the reference the
# indexed lookups must reproduce exactly (same value, same None, same error).
def _old_cable_dist_from_kp(model, kp_km):
    pts = model.points
    if not pts or pts[0].dist_cum_km is None:
        return None
    if kp_km < pts[0].dist_cum_km or kp_km > pts[-1].dist_cum_km:
        return None
    for i in range(len(pts) - 1):
        k0, k1 = pts[i].dist_cum_km, pts[i + 1].dist_cum_km
        if k0 is None or k1 is None:
            return None
        if kp_km <= k1 or i == len(pts) - 2:
            c0, c1 = pts[i].cable_dist_cum_km or 0.0, pts[i + 1].cable_dist_cum_km or 0.0
            if k1 - k0 <= 0:
                return c0
            t = (kp_km - k0) / (k1 - k0)
            if 0.0 <= t <= 1.0:
                return c0 + t * (c1 - c0)
    return None


def _old_kp_from_cable_dist(model, cable_km):
    pts = model.points
    if not pts or pts[0].cable_dist_cum_km is None:
        return None
    if cable_km < pts[0].cable_dist_cum_km or cable_km > pts[-1].cable_dist_cum_km:
        return None
    for i in range(len(pts) - 1):
        c0, c1 = pts[i].cable_dist_cum_km, pts[i + 1].cable_dist_cum_km
        if c0 is None or c1 is None:
            return None
        if cable_km <= c1 or i == len(pts) - 2:
            k0, k1 = pts[i].dist_cum_km or 0.0, pts[i + 1].dist_cum_km or 0.0
            if c1 - c0 <= 0:
                return k0
            t = (cable_km - c0) / (c1 - c0)
            if 0.0 <= t <= 1.0:
                return k0 + t * (k1 - k0)
    return None


def _old_point_at_kp(model, kp_km, da=None):
    pts = model.points
    if not pts or pts[0].dist_cum_km is None:
        return None
    if kp_km < pts[0].dist_cum_km or kp_km > pts[-1].dist_cum_km:
        return None
    for i in range(len(pts) - 1):
        k0, k1 = pts[i].dist_cum_km, pts[i + 1].dist_cum_km
        if k0 is None or k1 is None:
            return None
        if kp_km <= k1 or i == len(pts) - 2:
            if k1 - k0 <= 0:
                return (pts[i].lat, pts[i].lon)
            t = (kp_km - k0) / (k1 - k0)
            if 0.0 <= t <= 1.0:
                lat = pts[i].lat + t * (pts[i + 1].lat - pts[i].lat)
                lon = pts[i].lon + t * (pts[i + 1].lon - pts[i].lon)
                return (lat, lon)
    return None


def _old_bearing_at_kp(model, kp_km):
    pts = model.points
    if not pts:
        return None
    for i in range(len(pts) - 1):
        k0, k1 = pts[i].dist_cum_km, pts[i + 1].dist_cum_km
        if k0 is None or k1 is None:
            return None
        if kp_km <= k1 or i == len(pts) - 2:
            if kp_km >= k0:
                return model.segments[i].bearing_deg
    return None


def _outcome(fn, *args):
    try:
        return ("ok", fn(*args))
    except Exception as exc:  # noqa: BLE001 - the error type is the result
        return ("error", type(exc).__name__)


def _same(a, b) -> bool:
    if a == b:
        return True

    def nan_eq(x, y):
        return x == y or (isinstance(x, float) and isinstance(y, float) and x != x and y != y)

    if a[0] == b[0] == "ok" and isinstance(a[1], tuple) and isinstance(b[1], tuple):
        return len(a[1]) == len(b[1]) and all(nan_eq(x, y) for x, y in zip(a[1], b[1]))
    return a[0] == b[0] == "ok" and nan_eq(a[1], b[1])


_PAIRS = (
    ("cable_dist_from_kp", eng.cable_dist_from_kp, _old_cable_dist_from_kp),
    ("kp_from_cable_dist", eng.kp_from_cable_dist, _old_kp_from_cable_dist),
    ("point_at_kp", lambda m, x: eng.point_at_kp(m, x, None), _old_point_at_kp),
    ("bearing_at_kp", eng.bearing_at_kp, _old_bearing_at_kp),
)


def _random_series(rng, n):
    """Mostly clean cumulative series, with the anomalies the fallback must
    catch: repeated values (zero-length legs), None, NaN and disorder."""
    value = rng.uniform(-5.0, 5.0)
    series = []
    for _ in range(n):
        series.append(value)
        roll = rng.random()
        value += 0.0 if roll < 0.15 else rng.uniform(0.0, 3.0)
    roll = rng.random()
    if series and roll < 0.08:
        series[rng.randrange(n)] = None
    elif series and roll < 0.12:
        series[rng.randrange(n)] = float("nan")
    elif len(series) > 1 and roll < 0.18:
        i = rng.randrange(n - 1)
        series[i], series[i + 1] = series[i + 1], series[i] + 0.5
    return series


def _random_model(rng) -> RplModel:
    n = rng.randint(1, 25)
    kps = _random_series(rng, n)
    cables = _random_series(rng, n)
    points = [
        RplPoint(seq=i, pos_no=i + 1, event="", lat=rng.uniform(-60, 60),
                 lon=rng.uniform(-170, 170), dist_cum_km=kps[i],
                 cable_dist_cum_km=cables[i])
        for i in range(n)
    ]
    segments = [RplSegment(seq=i, bearing_deg=rng.uniform(0, 360)) for i in range(n - 1)]
    return RplModel(points=points, segments=segments)


def _queries(rng, model):
    values = [v for p in model.points for v in (p.dist_cum_km, p.cable_dist_cum_km)
              if isinstance(v, float) and v == v]
    out = [0, float("nan"), float("inf"), float("-inf")]
    out.extend(values)  # exact vertex hits
    lo = min(values) if values else -1.0
    hi = max(values) if values else 1.0
    out.extend(rng.uniform(lo - 2.0, hi + 2.0) for _ in range(40))
    return out


def _compare(model, queries):
    for name, new, old in _PAIRS:
        for q in queries:
            a, b = _outcome(new, model, q), _outcome(old, model, q)
            if not _same(a, b):
                return f"{name}({q!r}) new={a} old={b} kps={[p.dist_cum_km for p in model.points]}"
    return ""


def test_indexed_lookups_match_linear_walk() -> bool:
    import random

    rng = random.Random(20260928)
    mismatch = ""
    for _ in range(400):
        model = _random_model(rng)
        mismatch = _compare(model, _queries(rng, model))
        if mismatch:
            break
    return _result("indexed KP lookups == original linear walk (randomised)",
                   not mismatch, mismatch)


def test_indexed_lookups_follow_model_edits() -> bool:
    """The cached arrays are rebuilt after every kind of model edit."""
    import random

    rng = random.Random(7)
    da = _da()
    model = _model(8)
    eng.recompute(model, da)
    queries = _queries(rng, model)
    problems = []

    def check(label):
        eng.cable_dist_from_kp(model, 1.0)  # (re)build the cache first
        mismatch = _compare(model, queries)
        if mismatch:
            problems.append(f"{label}: {mismatch}")

    check("initial")
    eng.move_point(model, 3, 50.05, 0.02, da)
    check("move_point")
    eng.insert_point(model, 2, 50.025, 0.0, da)
    check("insert_point")
    eng.delete_point(model, 5, da)
    check("delete_point")
    model.points[4].dist_cum_km += 0.25
    check("attribute edit")
    model.points[1], model.points[2] = model.points[2], model.points[1]
    check("in-place swap")
    model.points.reverse()
    check("reverse")
    model.points = list(model.points)  # replaced by a plain list
    model.points[0].cable_dist_cum_km = -1.0
    check("plain list")
    return _result("indexed KP lookups follow model edits", not problems, "; ".join(problems))


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
        test_indexed_lookups_match_linear_walk(),
        test_indexed_lookups_follow_model_edits(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)
