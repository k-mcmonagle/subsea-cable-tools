# -*- coding: utf-8 -*-
"""Checks for the Burial Planner generation pipeline (pure python, no QGIS).

Synthetic per-rule intervals through the §12 pipeline: expected sections,
screening never removes candidates, influence flags on boundaries,
min-length drop, extension buffers, no-data -> Insufficient Information,
boundary refinement convergence, cache-key invalidation, determinism,
client-proposal diff.
"""

from __future__ import annotations

import json

from ..burial import generation as gen
from ..burial import schema
from ..workbench import rules_engine as eng
from ..workbench.rules_engine import Interval


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _counter_id_fn():
    counter = [0]

    def id_fn():
        counter[0] += 1
        return f"id-{counter[0]:04d}"

    return id_fn


def _rule(rid, name="rule", action="exclude", criterion="project",
          config=None, seq=0, methods=("plough",)):
    return {
        "rule_id": rid, "plan_id": "p", "seq": seq, "name": name, "enabled": 1,
        "kind": "manual", "action": action, "risk_level": 0,
        "criterion_class": criterion, "source_ref": "DOC-1 Rev A",
        "methods_json": json.dumps(list(methods)),
        "config_json": json.dumps(config or {}), "notes": "",
    }


def _params(**kwargs):
    defaults = dict(scope_start_kp=0.0, scope_end_kp=20.0, direction=1,
                    method="plough", min_section_km=0.5, coarse_step_m=50.0)
    defaults.update(kwargs)
    return gen.GenParams(**defaults)


def test_basic_sections_and_events() -> bool:
    acq = gen.RuleAcquisition(_rule("r1", "deep water"), [Interval(8.0, 12.0)])
    out = gen.generate(_params(), [acq], id_fn=_counter_id_fn())
    kinds = [(s["kind"], round(s["start_kp"], 3), round(s["end_kp"], 3))
             for s in out.sections]
    ok = kinds == [("burial", 0.0, 8.0), ("skip", 8.0, 12.0), ("burial", 12.0, 20.0)]
    types = [(e["event_type"], round(e["kp"], 3)) for e in out.events]
    ok = ok and types == [("BURIAL_START", 0.0), ("BURIAL_END", 8.0),
                          ("BURIAL_START", 12.0), ("BURIAL_END", 20.0)]
    skip = out.sections[1]
    reason = json.loads(skip["reason_json"])
    ok = ok and reason.get("dominant_rule") == "deep water"
    return _result("basic exclusion -> sections + ordered events + reasons", ok, str(kinds))


def test_direction_reversed_events() -> bool:
    acq = gen.RuleAcquisition(_rule("r1"), [Interval(8.0, 12.0)])
    out = gen.generate(_params(direction=-1), [acq], id_fn=_counter_id_fn())
    types = [(e["event_type"], round(e["kp"], 3)) for e in out.events]
    # travel B->A: starts at high KP
    ok = types == [("BURIAL_START", 20.0), ("BURIAL_END", 12.0),
                   ("BURIAL_START", 8.0), ("BURIAL_END", 0.0)]
    return _result("direction -1 orders events against KP", ok, str(types))


def test_screening_never_removes() -> bool:
    screening = gen.RuleAcquisition(
        _rule("s1", "sand", criterion="screening"), [Interval(2.0, 6.0)])
    out = gen.generate(_params(), [screening], id_fn=_counter_id_fn())
    ok = len(out.candidates) == 1 and abs(out.candidates[0].length_km - 20.0) < 1e-6
    burial = [s for s in out.sections if s["kind"] == "burial"]
    ok = ok and len(burial) == 1
    reason = json.loads(burial[0]["reason_json"])
    ok = ok and any(a.get("rule") == "sand" for a in reason.get("screening", []))
    return _result("Screening Criterion annotates, never excludes", ok)


def test_influence_flags_on_boundary() -> bool:
    config = {"influence_before_m": 500.0, "influence_after_m": 250.0}
    acq = gen.RuleAcquisition(_rule("x1", "crossing", config=config),
                              [Interval(10.0, 10.5)])
    out = gen.generate(_params(), [acq], id_fn=_counter_id_fn())
    zones = [(round(z.start_km, 3), round(z.end_km, 3)) for z in out.influence]
    ok = zones == [(9.5, 10.0), (10.5, 10.75)]
    burial = [s for s in out.sections if s["kind"] == "burial"]
    flagged = []
    for section in burial:
        reason = json.loads(section["reason_json"])
        flagged.extend(reason.get("influence_flags", []))
    # burial ends at 10.0 (inside approach zone) and restarts at 10.5
    # (inside departure zone) -> both boundaries flagged
    ok = ok and len(flagged) >= 2
    ok = ok and all("Constraint Influence Zone" in f["message"] for f in flagged)
    return _result("Constraint Influence Zone flags candidate boundaries", ok, str(zones))


def test_extension_buffer_and_min_length() -> bool:
    config = {"extend_m": 500.0}
    acq = gen.RuleAcquisition(_rule("e1", "hazard", config=config),
                              [Interval(5.0, 6.0)])
    acq2 = gen.RuleAcquisition(_rule("e2", "hazard2", seq=1), [Interval(7.0, 9.0)])
    out = gen.generate(_params(min_section_km=1.0), [acq, acq2],
                       id_fn=_counter_id_fn())
    # extended footprint 4.5-6.5; candidate 6.5-7.0 (0.5 km) < 1.0 -> dropped
    kinds = [(s["kind"], round(s["start_kp"], 3), round(s["end_kp"], 3))
             for s in out.sections]
    ok = ("skip", 4.5, 9.0) in kinds or (
        ("skip", 4.5, 6.5) in kinds and ("skip", 6.5, 7.0) in kinds)
    short_skip = next((s for s in out.sections if s["kind"] == "skip"
                       and s["start_kp"] <= 6.5 <= s["end_kp"]), None)
    ok = ok and short_skip is not None
    below = any(json.loads(s["reason_json"]).get("below_min_length")
                for s in out.sections if s["kind"] == "skip")
    ok = ok and below
    return _result("extend_m dilates footprint; short candidate dropped with reason",
                   ok, str(kinds))


def test_asymmetric_extension_direction_aware() -> bool:
    config = {"extend_mode": "fixed", "extend_before_m": 500.0,
              "extend_after_m": 200.0}
    acq = gen.RuleAcquisition(_rule("e1", "hazard", config=config),
                              [Interval(10.0, 11.0)])
    out = gen.generate(_params(), [acq], id_fn=_counter_id_fn())
    skips = [(round(s["start_kp"], 3), round(s["end_kp"], 3))
             for s in out.sections if s["kind"] == "skip"]
    ok = skips == [(9.5, 11.2)]
    # Direction -1: approach is the high-KP side.
    out_rev = gen.generate(_params(direction=-1), [acq], id_fn=_counter_id_fn())
    skips_rev = [(round(s["start_kp"], 3), round(s["end_kp"], 3))
                 for s in out_rev.sections if s["kind"] == "skip"]
    ok = ok and skips_rev == [(9.8, 11.5)]
    return _result("asymmetric extension follows the direction of installation",
                   ok, f"{skips} / {skips_rev}")


def test_wd_extension() -> bool:
    config = {"extend_mode": "wd", "extend_before_wd": 1.0,
              "extend_after_wd": 2.0}
    acq = gen.RuleAcquisition(_rule("e1", "hazard", config=config),
                              [Interval(10.0, 11.0)])
    # 500 m of water everywhere -> 0.5 km before, 1.0 km after.
    out = gen.generate(_params(), [acq], id_fn=_counter_id_fn(),
                       depth_at=lambda _kp: 500.0)
    skips = [(round(s["start_kp"], 3), round(s["end_kp"], 3))
             for s in out.sections if s["kind"] == "skip"]
    ok = skips == [(9.5, 12.0)]
    # No depth source -> unextended footprint plus a warning.
    out_none = gen.generate(_params(), [acq], id_fn=_counter_id_fn())
    skips_none = [(round(s["start_kp"], 3), round(s["end_kp"], 3))
                  for s in out_none.sections if s["kind"] == "skip"]
    ok = ok and skips_none == [(10.0, 11.0)]
    ok = ok and any("bathymetry" in w for w in out_none.warnings)
    # Legacy symmetric extend_m still honoured.
    legacy = gen.extension_config({"extend_m": 300.0})
    ok = ok and legacy == {"mode": "fixed", "before": 300.0, "after": 300.0}
    return _result("water-depth extension scales with depth at the boundary",
                   ok, f"{skips} / {skips_none}")


def test_extension_keys_do_not_invalidate_cache() -> bool:
    scope = Interval(0.0, 20.0)
    base = gen.rule_cache_key(_rule("r1", config={"value": 10.0}),
                              "fp1", scope, 50.0, "rplfp", 1)
    extended = _rule("r1", config={"value": 10.0, "extend_mode": "wd",
                                   "extend_before_wd": 1.0,
                                   "extend_after_wd": 1.5})
    ok = base == gen.rule_cache_key(extended, "fp1", scope, 50.0, "rplfp", 1)
    # The polygon route corridor IS acquisition config, so it must invalidate.
    poly = _rule("p1", config={"attribute": "class"})
    poly_corridor = _rule("p1", config={"attribute": "class",
                                        "route_buffer_mode": "fixed",
                                        "route_buffer_m": 15.0})
    ok = ok and gen.rule_cache_key(poly, "fp1", scope, 50.0, "rplfp", 1) \
        != gen.rule_cache_key(poly_corridor, "fp1", scope, 50.0, "rplfp", 1)
    return _result("extension keys resolution-only; corridor keys invalidate "
                   "acquisition", ok)


def test_nodata_becomes_insufficient() -> bool:
    acq = gen.RuleAcquisition(_rule("d1", "depth"), [Interval(5.0, 6.0)],
                              nodata=[Interval(15.0, 17.0)])
    out = gen.generate(_params(), [acq], id_fn=_counter_id_fn())
    insufficient = [s for s in out.sections if s["kind"] == "insufficient_info"]
    ok = len(insufficient) == 1
    ok = ok and abs(insufficient[0]["start_kp"] - 15.0) < 1e-6
    ok = ok and insufficient[0]["conclusion"] == schema.CONCLUSION_INSUFFICIENT
    # not treated as a candidate
    ok = ok and not any(iv.start_km <= 16.0 <= iv.end_km for iv in out.candidates)
    return _result("no-data ranges surface as Insufficient Information", ok)


def test_refinement_converges() -> bool:
    # analytic footprint: condition true where kp > 7.123456
    true_boundary = 7.123456
    predicate = lambda kp: kp > true_boundary
    coarse = [Interval(7.15, 20.0)]  # coarse acquisition off by < one step
    refined = gen.refine_intervals(coarse, predicate, coarse_step_km=0.05,
                                   domain=Interval(0.0, 20.0), tol_km=0.001)
    err_m = abs(refined[0].start_km - true_boundary) * 1000.0
    ok = err_m <= 1.0
    return _result("boundary refinement converges to <= 1 m", ok, f"err={err_m:.2f} m")


def test_default_refinement_keeps_symmetric_buffer_at_display_length() -> bool:
    """A 500 m crossing buffer must not display as a 1001 m exclusion."""
    centre_kp = 10.0
    half_width_km = 0.5
    predicate = lambda kp: abs(kp - centre_kp) <= half_width_km
    coarse = [Interval(9.45, 10.55)]
    refined = gen.refine_intervals(
        coarse, predicate, coarse_step_km=0.05,
        domain=Interval(0.0, 20.0))
    length_m = refined[0].length_km * 1000.0
    ok = abs(length_m - 1000.0) <= gen.BOUNDARY_REFINE_TOL_M + 1e-9
    ok = ok and round(length_m) == 1000
    return _result(
        "default refinement preserves a 500 m-each-side displayed length",
        ok, f"length={length_m:.3f} m")


def test_cache_key_sensitivity() -> bool:
    scope = Interval(0.0, 20.0)
    rule = _rule("r1", config={"value": 10.0})
    base = gen.rule_cache_key(rule, "fp1", scope, 50.0, "rplfp", 1)
    ok = base == gen.rule_cache_key(dict(rule), "fp1", scope, 50.0, "rplfp", 1)
    changed_config = _rule("r1", config={"value": 11.0})
    ok = ok and base != gen.rule_cache_key(changed_config, "fp1", scope, 50.0, "rplfp", 1)
    ok = ok and base != gen.rule_cache_key(rule, "fp2", scope, 50.0, "rplfp", 1)
    ok = ok and base != gen.rule_cache_key(rule, "fp1", Interval(0, 10), 50.0, "rplfp", 1)
    ok = ok and base != gen.rule_cache_key(rule, "fp1", scope, 25.0, "rplfp", 1)
    ok = ok and base != gen.rule_cache_key(rule, "fp1", scope, 50.0, "rplfp2", 1)
    ok = ok and base != gen.rule_cache_key(
        rule, "fp1", scope, 50.0, "rplfp", 1, refine_tol_m=1.0)
    # direction only matters for signed slope
    ok = ok and base == gen.rule_cache_key(rule, "fp1", scope, 50.0, "rplfp", -1)
    signed = _rule("r1", config={"slope_signed": True})
    ok = ok and gen.rule_cache_key(signed, "fp1", scope, 50.0, "rplfp", 1) \
        != gen.rule_cache_key(signed, "fp1", scope, 50.0, "rplfp", -1)
    # influence/extension changes do NOT invalidate acquisition
    influence = _rule("r1", config={"value": 10.0, "influence_before_m": 100.0})
    ok = ok and base == gen.rule_cache_key(influence, "fp1", scope, 50.0, "rplfp", 1)
    # Persisted profile resolution affects threshold acquisition independently
    # of the coarse rule-search step.
    threshold = _rule("t1", config={"profile": "slope", "value": 10.0})
    threshold["kind"] = "threshold_profile"
    key_5m = gen.rule_cache_key(
        threshold, "fp1", scope, 50.0, "rplfp", 1, profile_step_m=5.0)
    key_25m = gen.rule_cache_key(
        threshold, "fp1", scope, 50.0, "rplfp", 1, profile_step_m=25.0)
    ok = ok and key_5m != key_25m
    return _result("cache key: config/fingerprint/scope/step/refinement/rpl "
                   "invalidate; "
                   "profile step invalidates thresholds; influence + direction "
                   "(unsigned) do not", ok)


def test_determinism() -> bool:
    acq = [gen.RuleAcquisition(_rule("r1"), [Interval(8.0, 12.0)]),
           gen.RuleAcquisition(_rule("s1", criterion="screening", seq=1),
                               [Interval(2.0, 4.0)])]
    out1 = gen.generate(_params(), acq, id_fn=_counter_id_fn())
    out2 = gen.generate(_params(), acq, id_fn=_counter_id_fn())
    strip = lambda events: [(e["event_type"], e["kp"], e["status"]) for e in events]
    ok = strip(out1.events) == strip(out2.events)
    sections = lambda out: [(s["kind"], s["start_kp"], s["end_kp"], s["reason_json"])
                            for s in out.sections]
    ok = ok and sections(out1) == sections(out2)
    ok = ok and out1.summary == out2.summary
    return _result("identical snapshot -> identical output", ok)


def test_proposal_diff() -> bool:
    proposal = [
        {"event_type": "BURIAL_START", "kp": 0.0},
        {"event_type": "BURIAL_END", "kp": 7.9},       # moved to 8.0
        {"event_type": "BURIAL_START", "kp": 15.0},    # removed
    ]
    acq = gen.RuleAcquisition(_rule("r1"), [Interval(8.0, 12.0)])
    out = gen.generate(_params(), [acq], proposal_events=proposal,
                       id_fn=_counter_id_fn())
    diff = out.proposal_diff
    ok = diff is not None
    ok = ok and len(diff["moved"]) == 1 and abs(diff["moved"][0]["shift_km"] - 0.1) < 1e-6
    ok = ok and len(diff["removed"]) == 1
    ok = ok and len(diff["added"]) >= 1
    return _result("client-proposal diff records added/removed/moved", ok)


def test_conclusion_carry_over() -> bool:
    acq = [gen.RuleAcquisition(_rule("r1"), [Interval(8.0, 12.0)])]
    out1 = gen.generate(_params(), acq, id_fn=_counter_id_fn())
    first = out1.sections[0]
    first["conclusion"] = schema.CONCLUSION_NORMAL
    first["state"] = schema.SECTION_STATE_FINAL
    out2 = gen.generate(_params(), acq, previous_sections=out1.sections,
                        id_fn=_counter_id_fn())
    ok = out2.sections[0]["conclusion"] == schema.CONCLUSION_NORMAL
    ok = ok and out2.sections[0]["state"] == schema.SECTION_STATE_FINAL
    return _result("unchanged sections keep their conclusions across regeneration", ok)


def test_fresh_existing_events() -> bool:
    events = [
        {"event_id": "a", "source": "auto", "status": "candidate"},
        {"event_id": "m", "source": "manual", "status": "confirmed",
         "locked": 1},
        {"event_id": "i", "source": "import", "status": "candidate"},
        {"event_id": "c", "source": "client_proposal", "status": "candidate"},
    ]
    kept = gen.fresh_existing_events(events)
    ok = [e["event_id"] for e in kept] == ["c"]
    ok = ok and gen.fresh_existing_events(events, keep_client=False) == []
    # Inputs are copied, never shared.
    kept[0]["status"] = "changed"
    ok = ok and events[3]["status"] == "candidate"
    # A fresh generate over the filtered events rebuilds from the stack only.
    acq = gen.RuleAcquisition(_rule("r1"), [Interval(8.0, 12.0)])
    out = gen.generate(_params(), [acq],
                       existing_events=gen.fresh_existing_events(events),
                       id_fn=_counter_id_fn())
    sources = {e.get("source") for e in out.events}
    ok = ok and "manual" not in sources and "import" not in sources
    ok = ok and "client_proposal" in sources
    return _result("fresh regeneration keeps only client-proposal events", ok)


def test_assign_skip_handling() -> bool:
    sections = [
        {"section_id": "b1", "kind": schema.SECTION_BURIAL, "length_km": 5.0},
        {"section_id": "s1", "kind": schema.SECTION_SKIP, "length_km": 0.4,
         "skip_handling": ""},
        {"section_id": "s2", "kind": schema.SECTION_SKIP, "length_km": 2.0,
         "skip_handling": ""},
        {"section_id": "s3", "kind": schema.SECTION_SKIP, "length_km": 0.2,
         "skip_handling": schema.SKIP_HANDLING_RECOVER},
        {"section_id": "i1", "kind": schema.SECTION_INSUFFICIENT,
         "length_km": 1.0},
    ]
    updated, changed = gen.assign_skip_handling(sections, 0.5)
    by_id = {s["section_id"]: s for s in updated}
    ok = changed == 2
    ok = ok and by_id["s1"]["skip_handling"] == schema.SKIP_HANDLING_MIDWATER
    ok = ok and by_id["s2"]["skip_handling"] == schema.SKIP_HANDLING_RECOVER
    # An engineer's existing choice is never silently replaced…
    ok = ok and by_id["s3"]["skip_handling"] == schema.SKIP_HANDLING_RECOVER
    ok = ok and "skip_handling" not in by_id["b1"]
    # …unless overwrite is explicit.
    forced, forced_changed = gen.assign_skip_handling(sections, 0.5,
                                                      overwrite=True)
    forced_by_id = {s["section_id"]: s for s in forced}
    ok = ok and forced_changed == 3
    ok = ok and forced_by_id["s3"]["skip_handling"] == schema.SKIP_HANDLING_MIDWATER
    # Inputs are never mutated in place.
    ok = ok and sections[1]["skip_handling"] == ""
    return _result("skip handling auto-assign: length policy, TBC-only "
                   "default, overwrite explicit", ok)


def test_manual_burial_across_exclusion_is_flagged() -> bool:
    params = _params()
    events = [
        {"event_id": "start", "event_type": schema.EVENT_BURIAL_START,
         "kp": 0.0},
        {"event_id": "end", "event_type": schema.EVENT_BURIAL_END,
         "kp": 20.0},
    ]
    verdict = eng.RangeVerdict(
        8.0, 12.0, eng.STATUS_EXCLUDED, 0, ["r1"], None)
    sections = gen.build_sections(
        events, params, [verdict], [], [], [], [], {"r1": "steep slope"},
        id_fn=_counter_id_fn())
    burial = next(section for section in sections
                  if section["kind"] == schema.SECTION_BURIAL)
    reason = json.loads(burial["reason_json"])
    conflicts = reason.get("exclusion_conflicts") or []
    ok = len(conflicts) == 1
    ok = ok and conflicts[0]["rules"] == ["steep slope"]
    ok = ok and conflicts[0]["start_kp"] == 8.0
    ok = ok and conflicts[0]["end_kp"] == 12.0
    return _result("manual burial across an exclusion remains visibly flagged", ok)


def test_context_round_trip_keeps_rule_hits() -> bool:
    """Per-rule resolved footprints persist with the generation context, so
    the per-criterion fire bars and coverage survive reopening a plan."""
    acq1 = gen.RuleAcquisition(
        _rule("r1", "deep water"), [Interval(8.0, 12.0)])
    acq2 = gen.RuleAcquisition(
        _rule("r2", "boulders", criterion="screening", action="risk"),
        [Interval(2.0, 3.0), Interval(9.0, 10.0)])
    out = gen.generate(_params(), [acq1, acq2], id_fn=_counter_id_fn())
    ok = set(out.rule_hits) == {"r1", "r2"}
    ok = ok and len(out.rule_hits["r2"]) == 2

    # Round trip through JSON (as stored in bp_generation.summary_json).
    data = json.loads(json.dumps(gen.context_to_dict(out)))
    ctx = gen.context_from_dict(data)
    ok = ok and set(ctx.rule_hits) == {"r1", "r2"}
    ok = ok and abs(ctx.rule_hits["r1"][0].start_km - 8.0) < 1e-9
    ok = ok and abs(ctx.rule_hits["r1"][0].end_km - 12.0) < 1e-9
    ok = ok and abs(ctx.rule_hits["r2"][1].end_km - 10.0) < 1e-9
    # Older stored contexts without the key load cleanly.
    data.pop("rule_hits")
    ok = ok and gen.context_from_dict(data).rule_hits == {}
    return _result("resolution context round-trips per-rule hits", ok)


def run_all() -> list:
    return [
        test_basic_sections_and_events(),
        test_context_round_trip_keeps_rule_hits(),
        test_direction_reversed_events(),
        test_screening_never_removes(),
        test_influence_flags_on_boundary(),
        test_extension_buffer_and_min_length(),
        test_asymmetric_extension_direction_aware(),
        test_wd_extension(),
        test_extension_keys_do_not_invalidate_cache(),
        test_nodata_becomes_insufficient(),
        test_refinement_converges(),
        test_default_refinement_keeps_symmetric_buffer_at_display_length(),
        test_cache_key_sensitivity(),
        test_determinism(),
        test_proposal_diff(),
        test_conclusion_carry_over(),
        test_fresh_existing_events(),
        test_assign_skip_handling(),
        test_manual_burial_across_exclusion_is_flagged(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
