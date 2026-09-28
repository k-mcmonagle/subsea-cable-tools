# -*- coding: utf-8 -*-
"""KP datum (no QGIS): stated-KP check and whole-plan KP translation."""

import json

from ..burial import plan_rereference, schema
from ..burial.kp_rereference import KpMap
from ..kp_datum import compare_stated_kps, utm_epsg_for


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def test_stated_kp_levels():
    measured = [10.0, 11.0, 12.0, 13.0]
    agree = compare_stated_kps(measured, [10.0, 11.0003, 12.0004, 13.0002])
    info = compare_stated_kps(measured, [10.0, 11.001, 12.002, 13.003])
    warn = compare_stated_kps(measured, [10.0, 11.004, 12.008, 13.012])
    ok = agree.level == "ok" and info.level == "info" and warn.level == "warn"
    ok = ok and abs(warn.max_diff_m - 12.0) < 1e-6 and abs(warn.end_diff_m - 12.0) < 1e-6
    ok = ok and warn.start_kp_km == 10.0 and "12.0 m" in warn.summary()
    none = compare_stated_kps(measured, [None] * 4)
    ok = ok and none.level == "ok" and "no stated KPs" in none.summary()
    return _result("stated-KP check: agree / info / warn thresholds", ok,
                   f"{agree.max_diff_m:.2f} {info.max_diff_m:.2f} {warn.max_diff_m:.2f}")


def test_grid_distances_recognised():
    measured = [0.0, 5.0, 10.0]
    stated = [0.0, 5.002, 10.004]         # 4 m longer than geodesic
    grid = [0.0, 5.0021, 10.0041]          # planar chainage matches the document
    check = compare_stated_kps(measured, stated, grid, "UTM EPSG:32630")
    ok = check.looks_grid and "grid (cartesian" in check.summary()
    unrelated = compare_stated_kps(measured, stated, [0.0, 5.0, 10.0])
    ok = ok and not unrelated.looks_grid
    ok = ok and utm_epsg_for(-3.2, 55.9) == 32630 and utm_epsg_for(151.2, -33.9) == 32756
    return _result("stated KPs chained on the projection are recognised as grid", ok,
                   check.summary()[:90])


def test_shift_map_not_extrapolated():
    shift = KpMap.shift(12.345)
    value, flags = shift.map_kp(57.0)
    a, b, rflags = shift.map_range(40.0, 41.0)
    ok = abs(value - 69.345) < 1e-9 and not flags and not rflags and abs(b - a - 1.0) < 1e-9
    return _result("constant KP shift is exact everywhere (no 'extrapolated' flags)", ok,
                   f"{value} {flags} {rflags}")


def _plan():
    params = {
        "target_burial_ranges": [{"start_kp": 1.0, "end_kp": 2.0, "depth_m": 3.0}],
        "dismissed_insufficient": [[4.0, 4.5, "skip"]],
        "installation_paths": {"adjustments": [{"kp": 3.0, "dcc_m": 5.0}]},
    }
    return {"plan_id": "p", "scope_start_kp": 0.0, "scope_end_kp": 9.0,
            "params_json": json.dumps(params)}


def test_map_plan_shift_everything():
    S, E = schema.EVENT_BURIAL_START, schema.EVENT_BURIAL_END
    events = [{"event_id": "a", "event_type": S, "kp": 1.0, "lat": 1, "lon": 2},
              {"event_id": "b", "event_type": E, "kp": 3.0, "lat": 1, "lon": 2}]
    sections = [{"section_id": "s", "kind": schema.SECTION_BURIAL, "start_kp": 1.0,
                 "end_kp": 3.0, "length_km": 2.0, "tool_id": "t"}]
    hazards = [{"hazard_id": "h", "kp": 5.0, "end_kp": 5.0}]
    rules = [{"rule_id": "r", "name": "Manual", "config_json": json.dumps(
        {"ranges": [{"start_kp": 6.0, "end_kp": 7.0}],
         "scope_ranges": [{"start_kp": 0.0, "end_kp": 9.0}]})}]
    ground = [{"unit_id": "g", "start_kp": 2.0, "end_kp": 4.0, "src_start_kp": 2.0,
               "src_end_kp": 4.0, "src_rpl": "Rev A"}]
    generation = {"generation_id": "gen", "summary_json": json.dumps(
        {"context": {"excluded": [{"start_km": 6.0, "end_km": 7.0, "status": "x"}],
                     "insufficient": [[4.0, 4.5]], "rule_hits": {"r": [[6.0, 7.0]]}}})}
    out = plan_rereference.map_plan(KpMap.shift(10.0), _plan(), events, sections,
                                    hazards, rules, ground, [], generation, (10.0, 19.0))
    params = json.loads(out["plan"]["params_json"])
    ctx = json.loads(out["generation"]["summary_json"])["context"]
    rule_cfg = json.loads(out["rules"][0]["config_json"])
    ok = (out["plan"]["scope_start_kp"], out["plan"]["scope_end_kp"]) == (10.0, 19.0)
    ok = ok and params["target_burial_ranges"][0]["start_kp"] == 11.0
    ok = ok and params["dismissed_insufficient"][0][:2] == [14.0, 14.5]
    ok = ok and params["installation_paths"]["adjustments"][0]["kp"] == 13.0
    ok = ok and [e["kp"] for e in out["events"]] == [11.0, 13.0]
    ok = ok and out["events"][0]["lat"] is None     # re-stamped by the model
    ok = ok and out["sections"][0]["start_kp"] == 11.0 and out["sections"][0]["tool_id"] == "t"
    ok = ok and out["hazards"][0]["kp"] == 15.0
    ok = ok and rule_cfg["ranges"][0]["start_kp"] == 16.0 and rule_cfg["scope_ranges"][0]["end_kp"] == 19.0
    ok = ok and out["ground_units"][0]["start_kp"] == 12.0 and out["ground_units"][0]["src_start_kp"] == 2.0
    ok = ok and ctx["excluded"][0]["start_km"] == 16.0 and ctx["insufficient"][0] == [14.0, 14.5]
    ok = ok and ctx["rule_hits"]["r"][0] == [16.0, 17.0]
    report = out["report"]
    ok = ok and abs(report.max_shift_m - 10000.0) < 1e-6 and not report.flagged and not report.outside
    return _result("whole-plan shift covers scope, targets, resolutions, paths, events, "
                   "sections, hazards, rules, ground (src kept) and analysis context",
                   ok, report.summary())


def test_map_plan_flags_and_outside():
    # A geometry-style map with a gap: KP 4-6 interpolated, route ends at 8.
    kp_map = KpMap([(0.0, 0.0), (4.0, 4.1), (6.0, 6.3), (10.0, 10.3)],
                   gap_ranges=[(4.0, 6.0)])
    events = [{"event_id": "a", "event_type": schema.EVENT_BURIAL_START, "kp": 5.0},
              {"event_id": "b", "event_type": schema.EVENT_BURIAL_END, "kp": 9.0}]
    out = plan_rereference.map_plan(kp_map, {"plan_id": "p", "params_json": "{}"},
                                    events, [], bounds=(0.0, 8.0),
                                    event_labels={"a": "PLDN", "b": "PLUP"})
    report = out["report"]
    ok = any(s.startswith("PLDN KP 5.000: gap") for s in report.flagged)
    ok = ok and any(s.startswith("PLUP KP 9.000") for s in report.outside)
    return _result("translation reports gap stretches and KPs beyond the new route", ok,
                   f"{report.flagged} {report.outside}")


def run_all():
    return [test_stated_kp_levels(), test_grid_distances_recognised(),
            test_shift_map_not_extrapolated(), test_map_plan_shift_everything(),
            test_map_plan_flags_and_outside()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
