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
    # direction only matters for signed slope
    ok = ok and base == gen.rule_cache_key(rule, "fp1", scope, 50.0, "rplfp", -1)
    signed = _rule("r1", config={"slope_signed": True})
    ok = ok and gen.rule_cache_key(signed, "fp1", scope, 50.0, "rplfp", 1) \
        != gen.rule_cache_key(signed, "fp1", scope, 50.0, "rplfp", -1)
    # influence/extension changes do NOT invalidate acquisition
    influence = _rule("r1", config={"value": 10.0, "influence_before_m": 100.0})
    ok = ok and base == gen.rule_cache_key(influence, "fp1", scope, 50.0, "rplfp", 1)
    return _result("cache key: config/fingerprint/scope/step/rpl invalidate; "
                   "influence + direction (unsigned) do not", ok)


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


def run_all() -> list:
    return [
        test_basic_sections_and_events(),
        test_direction_reversed_events(),
        test_screening_never_removes(),
        test_influence_flags_on_boundary(),
        test_extension_buffer_and_min_length(),
        test_nodata_becomes_insufficient(),
        test_refinement_converges(),
        test_cache_key_sensitivity(),
        test_determinism(),
        test_proposal_diff(),
        test_conclusion_carry_over(),
        test_manual_burial_across_exclusion_is_flagged(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
