# -*- coding: utf-8 -*-
"""Pure checks: KP-ranged target burial depth, persisted-analysis currency,
profile stale reasons and compact profile storage, per-criterion no-data."""

from __future__ import annotations

import json

from ..burial import analysis_state, generation, target_depth
from ..burial.profile_data import (
    PlanProfile,
    run_length_decode,
    run_length_encode,
)
from ..workbench.rules_engine import Interval


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


# -- target burial depth -------------------------------------------------------

def test_target_runs_and_lookup() -> bool:
    ranges = [{"start_kp": 12.0, "end_kp": 18.5, "depth_m": 3.0},
              {"start_kp": 2.0, "end_kp": 4.0, "depth_m": 2.0, "notes": "x"}]
    clean = target_depth.normalise_ranges(ranges)
    ok = [r["start_kp"] for r in clean] == [2.0, 12.0]
    runs = target_depth.target_runs(1.5, clean, 0.0, 20.0)
    ok = ok and runs == [(0.0, 2.0, 1.5), (2.0, 4.0, 2.0), (4.0, 12.0, 1.5),
                         (12.0, 18.5, 3.0), (18.5, 20.0, 1.5)]
    # No default: gaps between ranges have no target.
    runs = target_depth.target_runs(None, clean, 3.0, 13.0)
    ok = ok and runs == [(3.0, 4.0, 2.0), (4.0, 12.0, None),
                         (12.0, 13.0, 3.0)]
    ok = ok and target_depth.target_at(15.0, 1.5, clean) == 3.0
    ok = ok and target_depth.target_at(8.0, 1.5, clean) == 1.5
    ok = ok and target_depth.target_at(8.0, None, clean) is None
    # A section spanning a boundary reports the span of targets.
    ok = ok and target_depth.depth_span(1.5, clean, 10.0, 14.0) == (1.5, 3.0)
    ok = ok and target_depth.format_span((1.5, 3.0)) == "1.5–3"
    ok = ok and target_depth.format_span((2.0, 2.0)) == "2"
    ok = ok and target_depth.depth_span(None, [], 0.0, 1.0) is None
    # Same-depth neighbours merge into one run.
    touching = [{"start_kp": 0.0, "end_kp": 1.0, "depth_m": 2.0},
                {"start_kp": 1.0, "end_kp": 2.0, "depth_m": 2.0}]
    ok = ok and target_depth.target_runs(None, touching, 0.0, 2.0) == [
        (0.0, 2.0, 2.0)]
    return _result("target depth: runs, lookup, spans", ok)


def test_target_validation_and_plan_storage() -> bool:
    problems = target_depth.validate_ranges([
        {"start_kp": 1.0, "end_kp": 3.0, "depth_m": 2.0},
        {"start_kp": 2.5, "end_kp": 4.0, "depth_m": 3.0},   # overlaps row 1
        {"start_kp": 5.0, "end_kp": 5.0, "depth_m": 1.0},   # zero length
        {"start_kp": 6.0, "end_kp": 7.0, "depth_m": 0.0},   # no depth
        {"start_kp": 8.0, "end_kp": 30.0, "depth_m": 1.0},  # off the route
        {"start_kp": "x", "end_kp": 9.0, "depth_m": 1.0},   # not a number
    ], route_length_km=20.0)
    text = " ".join(problems)
    ok = "overlap" in text and "greater than" in text
    ok = ok and "above 0 m" in text and "outside the route" in text
    ok = ok and "both KPs" in text
    ok = ok and target_depth.validate_ranges(
        [{"start_kp": 0, "end_kp": 1, "depth_m": 1.2}], 20.0) == []
    plan = {"target_burial_m": 1.5, "params_json": json.dumps({
        "sliver_tol_km": 0.02, target_depth.PARAMS_KEY: [
            {"start_kp": 5, "end_kp": 6, "depth_m": 3}]})}
    ok = ok and target_depth.plan_default(plan) == 1.5
    ok = ok and target_depth.plan_ranges(plan)[0]["depth_m"] == 3.0
    ok = ok and target_depth.plan_ranges({"params_json": "junk"}) == []
    ok = ok and target_depth.plan_default({"target_burial_m": 0}) is None
    summary = target_depth.summary_text(1.5, target_depth.plan_ranges(plan))
    ok = ok and "Default 1.5 m" in summary and "1 KP range" in summary
    return _result("target depth: validation + plan params storage", ok)


def test_target_ranges_from_rpl_legs() -> bool:
    legs = [
        {"start_kp": 0.0, "end_kp": 2.0, "target_burial_m": 1.0},
        {"start_kp": 2.0, "end_kp": 3.5, "target_burial_m": 1.0},
        {"start_kp": 3.5, "end_kp": 5.0, "target_burial_m": None},
        {"start_kp": 5.0, "end_kp": 9.0, "target_burial_m": 2.5},
    ]
    ranges = target_depth.ranges_from_legs(legs)
    ok = len(ranges) == 2
    ok = ok and ranges[0]["start_kp"] == 0.0 and ranges[0]["end_kp"] == 3.5
    ok = ok and ranges[1]["depth_m"] == 2.5
    return _result("target depth: ranges from RPL legs (merged)", ok)


# -- analysis currency --------------------------------------------------------

class _Params:
    def __init__(self, start=0.0, end=10.0, **kw):
        self.scope = Interval(start, end)
        self.direction = kw.get("direction", 1)
        self.method = kw.get("method", "plough")
        self.coarse_step_m = kw.get("coarse_step_m", 50.0)
        self.sliver_tol_km = kw.get("sliver_tol_km", 0.0)
        self.refine_tol_m = kw.get("refine_tol_m", 0.1)


def _rule(rule_id, **kw):
    row = {"rule_id": rule_id, "name": f"Rule {rule_id}", "seq": 0,
           "enabled": 1, "kind": "proximity", "action": "exclude",
           "risk_level": 0, "criterion_class": "project",
           "methods_json": "[]", "config_json": json.dumps(
               {"input_id": "in-1", "distance_m": 100.0}), "notes": ""}
    row.update(kw)
    return row


def test_analysis_fingerprints() -> bool:
    inputs = [{"input_id": "in-1", "layer_source": "/data/cables.gpkg|layername=a",
               "config_json": "{}"}]
    rules = [_rule("r1"), _rule("r2", kind="threshold_profile",
                                config_json=json.dumps({"profile": "slope"}))]
    stored = analysis_state.exclusion_fingerprints(
        _Params(), rules, inputs, "geom:abc", "2026-09-01T10:00:00Z")
    # Renaming / reordering / notes never make a result stale.
    renamed = [dict(rules[0], name="Other name", seq=5, notes="n"), rules[1]]
    now = analysis_state.exclusion_fingerprints(
        _Params(), renamed, inputs, "geom:abc", "2026-09-01T10:00:00Z")
    state = analysis_state.compare_items(stored["rules"], now["rules"])
    ok = state == {"r1": "current", "r2": "current"}
    ok = ok and analysis_state.compare_global(stored["global"],
                                              now["global"]) == []
    # JSON key order in config never matters.
    reordered = [dict(rules[0], config_json='{"distance_m": 100.0, '
                                            '"input_id": "in-1"}'), rules[1]]
    now = analysis_state.exclusion_fingerprints(
        _Params(), reordered, inputs, "geom:abc", "2026-09-01T10:00:00Z")
    ok = ok and analysis_state.compare_items(
        stored["rules"], now["rules"])["r1"] == "current"
    # A config change, a re-pointed input and a new profile each count.
    changed = [dict(rules[0], config_json=json.dumps(
        {"input_id": "in-1", "distance_m": 150.0})), rules[1]]
    moved_inputs = [dict(inputs[0], layer_source="/data/other.gpkg")]
    now = analysis_state.exclusion_fingerprints(
        _Params(), changed + [_rule("r3")], moved_inputs, "geom:abc",
        "2026-09-02T10:00:00Z")
    state = analysis_state.compare_items(stored["rules"], now["rules"])
    ok = ok and state == {"r1": "changed", "r2": "changed", "r3": "new"}
    # Global changes are named.
    now = analysis_state.exclusion_fingerprints(
        _Params(0.0, 12.0, direction=-1), rules, inputs, "geom:xyz")
    reasons = analysis_state.compare_global(stored["global"], now["global"])
    ok = ok and "the scope" in reasons and "the route geometry" in reasons
    ok = ok and "the direction of installation" in reasons
    # A stored empty component (legacy) is never compared.
    legacy = dict(stored["global"], route="")
    ok = ok and "the route geometry" not in analysis_state.compare_global(
        legacy, now["global"])
    ok = ok and analysis_state.compare_items(None, {"a": "x"}) == {"a": "none"}
    return _result("analysis currency: per-criterion + global fingerprints", ok)


def test_risk_run_records() -> bool:
    inputs = [{"input_id": "in-1", "layer_source": "rocks.shp",
               "config_json": "{}"}]
    check = {"check_id": "c1", "name": "Rocks", "enabled": 1,
             "config_json": json.dumps({"input_id": "in-1",
                                        "distance_m": 50})}
    by_id = {r["input_id"]: r for r in inputs}
    fp = analysis_state.check_fingerprint(check, by_id)
    runs = analysis_state.record_risk_run(
        {}, {"c1": fp}, {"c1": 0}, {}, "Scanned 1 check(s): 0 hazard(s).",
        "2026-09-23T10:00:00Z")
    row = {"risk_json": analysis_state.canonical(runs)}
    back = analysis_state.risk_runs(row)
    ok = back["runs"]["c1"]["count"] == 0 and back["runs"]["c1"]["fp"] == fp
    ok = ok and back["message"].startswith("Scanned")
    ok = ok and analysis_state.risk_runs({"risk_json": "junk"})["runs"] == {}
    renamed = dict(check, name="Boulders")
    ok = ok and analysis_state.check_fingerprint(renamed, by_id) == fp
    edited = dict(check, config_json=json.dumps({"input_id": "in-1",
                                                 "distance_m": 80}))
    ok = ok and analysis_state.check_fingerprint(edited, by_id) != fp
    ok = ok and analysis_state.format_utc("2026-09-23T14:02:11Z") == \
        "2026-09-23 14:02 UTC"
    return _result("risk scan run records + check fingerprints", ok)


def test_context_rule_nodata_round_trip() -> bool:
    scope = Interval(0.0, 10.0)
    acquisitions = [
        generation.RuleAcquisition({"rule_id": "cross"}, [],
                                   [Interval(2.0, 3.0), Interval(9.5, 12.0)]),
        generation.RuleAcquisition({"rule_id": "depth"}, [], []),
        generation.RuleAcquisition({"rule_id": "broken"}, [],
                                   [Interval(1.0, 2.0)], error="failed"),
    ]
    by_rule = generation.rule_nodata_map(acquisitions, scope)
    ok = set(by_rule) == {"cross"}
    ok = ok and [(iv.start_km, iv.end_km) for iv in by_rule["cross"]] == [
        (2.0, 3.0), (9.5, 10.0)]
    ctx = generation.ResolutionContext(
        insufficient=[Interval(2.0, 3.0)],
        rule_hits={"r1": [Interval(4.0, 5.0)]}, rule_nodata=by_rule)
    back = generation.context_from_dict(json.loads(json.dumps(
        generation.context_dict(ctx))))
    ok = ok and [(iv.start_km, iv.end_km) for iv in back.rule_nodata["cross"]] \
        == [(2.0, 3.0), (9.5, 10.0)]
    ok = ok and back.rule_hits["r1"][0].start_km == 4.0
    # Contexts stored before rule_nodata existed still load.
    old = generation.context_from_dict({"insufficient": [[1.0, 2.0]]})
    ok = ok and old.rule_nodata == {} and len(old.insufficient) == 1
    return _result("per-criterion no-data: map + context round trip", ok)


# -- profile identity / storage -------------------------------------------------

def _profile(**kw) -> PlanProfile:
    base = dict(step_m=5.0, cross_offset_m=5.0, scope_start_kp=0.0,
                scope_end_kp=1.0,
                route_fingerprint="rpl-1|2026-01-01|lines|c:\\old\\wb.gpkg",
                depth_fingerprint="v2-fp",
                route_geom_fingerprint="geom:1",
                depth_layers={"c:\\data\\bathy.gpkg|layername=contours":
                              "gpkg:2026-01-01T00:00:00Z|120|{}"},
                depth_signature="2|1|[\"depth\"]",
                depth_layer_names={"c:\\data\\bathy.gpkg|layername=contours":
                                   "Contours"},
                kps=[0.0, 0.5, 1.0], depths=[10.0, 11.0, 12.0])
    base.update(kw)
    return PlanProfile(**base)


def _reasons(profile, **kw):
    args = dict(route_fingerprint="rpl-1|2026-01-01|lines|d:\\new\\wb.gpkg",
                route_geom_fingerprint="geom:1", depth_fingerprint="v2-fp",
                depth_layers=dict(profile.depth_layers),
                depth_signature=profile.depth_signature,
                scope_start_kp=0.0, scope_end_kp=1.0, cross_offset_m=5.0,
                step_m=5.0)
    args.update(kw)
    return profile.stale_reasons(**args)


def test_profile_stale_reasons() -> bool:
    profile = _profile()
    # Same route geometry opened from another folder -> still current.
    ok = _reasons(profile) == []
    key = next(iter(profile.depth_layers))
    # Identical per-layer identity but a different combined fingerprint
    # (e.g. the sampling method version changed) is still reported.
    ok = ok and _reasons(profile, depth_fingerprint="other") == [
        "the bathymetry source changed since sampling"]
    # The layer's data changed.
    edited = {key: "gpkg:2026-02-02T00:00:00Z|121|{}"}
    reasons = _reasons(profile, depth_fingerprint="x", depth_layers=edited)
    ok = ok and len(reasons) == 1 and "'Contours'" in reasons[0]
    ok = ok and "modified" in reasons[0] and "120 → 121" in reasons[0]
    # Conventions differ (set but not saved in the project).
    conv = {key: 'gpkg:2026-01-01T00:00:00Z|120|{"vertical": "elevation"}'}
    reasons = _reasons(profile, depth_fingerprint="x", depth_layers=conv)
    ok = ok and "conventions" in reasons[0] and "save it" in reasons[0]
    # Missing layer / route moved / scope / cross / step.
    reasons = _reasons(profile, depth_fingerprint="x",
                       depth_layers={"missing:abc": ""},
                       route_geom_fingerprint="geom:2", scope_end_kp=2.0,
                       cross_offset_m=10.0, step_m=2.0)
    text = " | ".join(reasons)
    ok = ok and "not in the project" in text
    ok = ok and "route geometry changed" in text
    ok = ok and "scope changed" in text and "half-width changed" in text
    ok = ok and "station step changed" in text
    # Legacy profile (no v2 identity): the v1 fingerprint decides, and the
    # RPL fingerprint is compared without its path component.
    legacy = _profile(route_geom_fingerprint="", depth_layers={},
                      depth_signature="", depth_fingerprint="v1-fp")
    ok = ok and _reasons(legacy, depth_fingerprint="v2-now",
                         legacy_depth_fingerprint="v1-fp") == []
    reasons = _reasons(legacy, depth_fingerprint="v2-now",
                       legacy_depth_fingerprint="v1-changed")
    ok = ok and reasons == ["the bathymetry source changed since sampling"]
    reasons = _reasons(legacy, legacy_depth_fingerprint="v1-fp",
                       depth_fingerprint="v2",
                       route_fingerprint="rpl-1|2026-03-03|lines|c:\\old\\wb.gpkg")
    ok = ok and reasons == ["the route (RPL revision) changed"]
    return _result("profile stale reasons (route geometry, per-layer bathy)",
                   ok)


def test_profile_compact_storage() -> bool:
    sources = ["C:/data/a.tif"] * 5000 + [None] * 3 + ["C:/data/b.tif"] * 2000
    cells = [0.5] * 5000 + [None] * 3 + [1.0] * 2000
    ok = run_length_decode(run_length_encode(sources)) == sources
    ok = ok and run_length_encode([]) == [] and run_length_decode(None) == []
    kps = [i * 0.001 for i in range(len(sources))]
    profile = _profile(kps=kps, depths=[10.0] * len(kps),
                       source_ids=sources, cell_sizes_m=cells,
                       cross_max_deg=[1.23456789] * len(kps))
    row = profile.to_row("plan-1")
    ok = ok and len(row["samples_json"]) < 200000  # URIs no longer repeated
    back = PlanProfile.from_row(row)
    ok = ok and back.source_ids == sources and back.cell_sizes_m == cells
    ok = ok and abs(back.cross_max_deg[0] - 1.2346) < 1e-9
    ok = ok and back.route_geom_fingerprint == "geom:1"
    ok = ok and back.depth_layers == profile.depth_layers
    ok = ok and back.depth_layer_names == profile.depth_layer_names
    # Rows written before run-length encoding still load.
    old = json.loads(row["samples_json"])
    old.pop("sources_rle")
    old.pop("cells_rle")
    old["sources"] = sources
    old["cells"] = cells
    back = PlanProfile.from_row(dict(row, samples_json=json.dumps(old)))
    ok = ok and back.source_ids == sources and back.cell_sizes_m == cells
    return _result("profile storage: run-length sources/cells, identity",
                   ok, f"{len(row['samples_json']):,} bytes")


def run_all() -> list:
    return [
        test_target_runs_and_lookup(),
        test_target_validation_and_plan_storage(),
        test_target_ranges_from_rpl_legs(),
        test_analysis_fingerprints(),
        test_risk_run_records(),
        test_context_rule_nodata_round_trip(),
        test_profile_stale_reasons(),
        test_profile_compact_storage(),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(run_all()) else 1)
