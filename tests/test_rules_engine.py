# -*- coding: utf-8 -*-
"""Checks for the pure route-suitability rules engine.

No QGIS required — exercises the interval algebra and the ordered-stack
resolution semantics (severity lattice, allow-override, dissolve, sliver
merging, provenance) directly.
"""

from __future__ import annotations

from ..workbench import rules_engine as eng
from ..workbench.rules_engine import Interval, Rule, RuleHit


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _rule(rid, seq, action, level=0, methods=("plough",), enabled=True):
    return Rule(rule_id=rid, name=rid, seq=seq, action=action, risk_level=level,
                methods=list(methods), enabled=enabled)


def test_normalize_and_intersect() -> bool:
    merged = eng.normalize([Interval(0, 2), Interval(1.5, 3), Interval(5, 6)])
    ok = len(merged) == 2 and abs(merged[0].end_km - 3) < 1e-9
    inter = eng.intersect_intervals([Interval(0, 5)], [Interval(2, 8)])
    ok = ok and len(inter) == 1 and abs(inter[0].start_km - 2) < 1e-9 and abs(inter[0].end_km - 5) < 1e-9
    length = eng.interval_length_km([Interval(0, 2), Interval(1, 5)])
    ok = ok and abs(length - 5.0) < 1e-9
    return _result("normalize + intersect + length", ok)


def test_profile_threshold_interpolation() -> bool:
    # value rises 0 -> 20 over KP 0 -> 10; ">10" should start at KP 5.
    series = [(0.0, 0.0), (10.0, 20.0)]
    ivs = eng.intervals_from_profile(series, ">", 10.0)
    ok = len(ivs) == 1 and abs(ivs[0].start_km - 5.0) < 1e-6 and abs(ivs[0].end_km - 10.0) < 1e-6
    # between band 5..15 -> KP 2.5 .. 7.5
    band = eng.intervals_from_profile(series, "between", 5.0, 15.0)
    ok = ok and len(band) == 1 and abs(band[0].start_km - 2.5) < 1e-6 and abs(band[0].end_km - 7.5) < 1e-6
    # abs: symmetric slope -10 .. +10 over 0..10, |v|>5 at the ends
    sym = eng.intervals_from_profile([(0.0, -10.0), (5.0, 0.0), (10.0, 10.0)], ">", 5.0, abs_value=True)
    total = eng.interval_length_km(sym)
    ok = ok and abs(total - 5.0) < 1e-6  # first 2.5 km + last 2.5 km
    return _result("profile threshold boundary interpolation", ok,
                   f"gt={[(round(i.start_km,2),round(i.end_km,2)) for i in ivs]}")


def test_bool_series_midpoint() -> bool:
    dom = Interval(0.0, 4.0)
    series = [(0.0, False), (1.0, True), (2.0, True), (3.0, False)]
    ivs = eng.intervals_from_bool_series(series, dom)
    # TRUE at 1 and 2 -> [0.5 .. 2.5]
    ok = len(ivs) == 1 and abs(ivs[0].start_km - 0.5) < 1e-9 and abs(ivs[0].end_km - 2.5) < 1e-9
    return _result("bool series midpoint ownership", ok)


def test_exclude_and_risk_lattice() -> bool:
    dom = Interval(0.0, 10.0)
    hits = [
        RuleHit(_rule("deep", 0, eng.ACTION_EXCLUDE), [Interval(6, 10)]),
        RuleHit(_rule("slopeR", 1, eng.ACTION_RISK, level=2), [Interval(2, 8)]),
    ]
    res = eng.evaluate(dom, ["plough"], hits)
    verdicts = res.per_method["plough"]
    # 0-2 allowed, 2-6 risk2, 6-10 excluded
    got = [(round(v.start_km, 1), round(v.end_km, 1), v.status) for v in verdicts]
    ok = got == [(0.0, 2.0, "allowed"), (2.0, 6.0, "risk"), (6.0, 10.0, "excluded")]
    # excluded wins over risk in the 6-8 overlap
    excl = next(v for v in verdicts if v.status == "excluded")
    ok = ok and excl.dominant_rule_id == "deep"
    return _result("exclude + risk severity lattice", ok, str(got))


def test_allow_override_order() -> bool:
    dom = Interval(0.0, 10.0)
    exclude = RuleHit(_rule("deep", 0, eng.ACTION_EXCLUDE), [Interval(0, 10)])
    allow = RuleHit(_rule("approved", 1, eng.ACTION_ALLOW), [Interval(4, 6)])
    # exclude first, allow later -> allow overrides in 4-6
    res = eng.evaluate(dom, ["plough"], [exclude, allow])
    v = res.per_method["plough"]
    seg = next(x for x in v if x.start_km <= 5.0 <= x.end_km)
    ok = seg.status == "allowed" and seg.dominant_rule_id == "approved"
    # reversed order: allow first, exclude later -> exclude NOT overridden
    exclude2 = RuleHit(_rule("deep", 1, eng.ACTION_EXCLUDE), [Interval(0, 10)])
    allow2 = RuleHit(_rule("approved", 0, eng.ACTION_ALLOW), [Interval(4, 6)])
    res2 = eng.evaluate(dom, ["plough"], [exclude2, allow2])
    seg2 = next(x for x in res2.per_method["plough"] if x.start_km <= 5.0 <= x.end_km)
    ok = ok and seg2.status == "excluded"
    return _result("allow overrides earlier, not later, exclude", ok)


def test_scope_via_intersect() -> bool:
    # A rule scoped to KP 0-5 only fires inside its scope even if condition is wider.
    condition = [Interval(0, 10)]
    scope = [Interval(0, 5)]
    scoped = eng.intersect_intervals(condition, scope)
    ok = len(scoped) == 1 and abs(scoped[0].end_km - 5.0) < 1e-9
    return _result("rule scoping via intersect_intervals", ok)


def test_rule_stats_coverage() -> bool:
    dom = Interval(0.0, 10.0)
    hits = [RuleHit(_rule("deep", 0, eng.ACTION_EXCLUDE), [Interval(0, 2.5)])]
    res = eng.evaluate(dom, ["plough"], hits)
    stat = res.rule_stats[0]
    ok = abs(stat.coverage_km - 2.5) < 1e-9 and abs(stat.coverage_pct - 25.0) < 1e-9
    return _result("per-rule coverage stats", ok, f"{stat.coverage_km} km / {stat.coverage_pct}%")


def test_sliver_merge_and_dissolve() -> bool:
    dom = Interval(0.0, 10.0)
    # tiny risk sliver (4.9-5.1) inside a big excluded run -> absorbed by excluded
    hits = [
        RuleHit(_rule("deep", 0, eng.ACTION_EXCLUDE), [Interval(0, 10)]),
        RuleHit(_rule("allowSliver", 1, eng.ACTION_ALLOW), [Interval(4.9, 5.1)]),
    ]
    res = eng.evaluate(dom, ["plough"], hits, min_range_km=0.5)
    v = res.per_method["plough"]
    # the 0.2 km allowed sliver is below 0.5 km -> absorbed into excluded neighbours
    ok = len(v) == 1 and v[0].status == "excluded"
    return _result("sub-min sliver merged into more-severe neighbour", ok,
                   f"{[(round(x.start_km,1),round(x.end_km,1),x.status) for x in v]}")


def test_method_filtering() -> bool:
    dom = Interval(0.0, 10.0)
    # rule applies only to plough; jet should be fully allowed
    hits = [RuleHit(_rule("deep", 0, eng.ACTION_EXCLUDE, methods=("plough",)), [Interval(0, 10)])]
    res = eng.evaluate(dom, ["plough", "jet"], hits)
    ok = res.per_method["plough"][0].status == "excluded"
    ok = ok and all(v.status == "allowed" for v in res.per_method["jet"])
    return _result("per-method rule applicability", ok)


def run_all() -> list:
    return [
        test_normalize_and_intersect(),
        test_profile_threshold_interpolation(),
        test_bool_series_midpoint(),
        test_exclude_and_risk_lattice(),
        test_allow_override_order(),
        test_scope_via_intersect(),
        test_rule_stats_coverage(),
        test_sliver_merge_and_dissolve(),
        test_method_filtering(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)
