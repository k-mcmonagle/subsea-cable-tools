# -*- coding: utf-8 -*-
"""Checks for the Risk Profile logic (pure python, no QGIS).

Risk evaluation (proximity bands + attribute rules + default), the
attribute-rule text form, register carry-over across re-scans, overview
spans, summaries and the hazards CSV export.
"""

from __future__ import annotations

import json

from ..burial import attribute_rules, io_csv, risk, schema


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def test_risk_evaluation() -> bool:
    config = {"band_high_m": 10.0, "band_medium_m": 50.0, "band_low_m": 200.0,
              "attribute": "height_m",
              "attribute_rules": [{"min": 1.0, "risk": "high"},
                                  {"min": 0.5, "max": 1.0, "risk": "medium"},
                                  {"match": "WRECK", "risk": "high"}],
              "default_risk": "low"}
    ok = risk.evaluate_risk(config, 0.0, {}) == schema.RISK_HIGH      # crossing
    ok = ok and risk.evaluate_risk(config, 30.0, {}) == schema.RISK_MEDIUM
    ok = ok and risk.evaluate_risk(config, 150.0, {}) == schema.RISK_LOW
    # Attribute outranks a milder proximity band (max of the two).
    ok = ok and risk.evaluate_risk(
        config, 150.0, {"height_m": 2.0}) == schema.RISK_HIGH
    ok = ok and risk.evaluate_risk(
        config, 150.0, {"height_m": 0.7}) == schema.RISK_MEDIUM
    # Exact value match works on the same rule list.
    ok = ok and risk.attribute_risk(
        config, {"height_m": "WRECK"}) == schema.RISK_HIGH
    # Signed offsets (+stbd / -port): bands apply to the magnitude.
    ok = ok and risk.evaluate_risk(config, -30.0, {}) == schema.RISK_MEDIUM
    ok = ok and risk.proximity_risk(config, -8.0) == schema.RISK_HIGH
    # Nothing fires inside the search corridor -> default risk.
    bare = {"default_risk": "medium"}
    ok = ok and risk.evaluate_risk(bare, 500.0, {}) == schema.RISK_MEDIUM
    # No default -> unassigned; missing attribute value never fires.
    ok = ok and risk.evaluate_risk({}, 500.0, {}) == schema.RISK_UNASSIGNED
    ok = ok and risk.attribute_risk(config, {}) == schema.RISK_UNASSIGNED
    ok = ok and risk.risk_max("low", "", "high", "medium") == "high"
    return _result("risk evaluation: bands + attribute rules + default", ok)


def test_attribute_rule_text_form() -> bool:
    cases = [
        ("ROCK", {"match": "ROCK", "risk": "high"}),
        ("1.5-3", {"min": 1.5, "max": 3.0, "risk": "high"}),
        ("2-", {"min": 2.0, "risk": "high"}),
        ("-3", {"max": 3.0, "risk": "high"}),
        ("7", {"match": "7", "risk": "high"}),
    ]
    ok = True
    for text, expected in cases:
        parsed = risk.parse_attribute_rule(text, "high")
        ok = ok and parsed == expected
        # Round trip through the display form parses identically.
        ok = ok and risk.parse_attribute_rule(
            risk.format_attribute_rule(parsed), "high") == expected
    ok = ok and risk.parse_attribute_rule("", "high") is None
    ok = ok and risk.parse_attribute_rule("x", "") is None
    return _result("attribute-rule text form round trip", ok)


def _hazard(check_id, feature_ref, kp, level="low", **overrides):
    row = risk.new_hazard_row("p1", check_id, feature_ref, feature_ref,
                              kp, None, 5.0, False, None, None, None, level)
    row.update(overrides)
    return row


def test_carry_over() -> bool:
    old = _hazard("c1", "10#0", 5.0, "low",
                  status=schema.HAZARD_STATUS_ACCEPTED, notes="reviewed",
                  risk=schema.RISK_HIGH, risk_source=schema.RISK_SOURCE_USER)
    old_auto = _hazard("c1", "11#0", 6.0, "medium")
    new = [_hazard("c1", "10#0", 5.001, "medium"),   # feature moved slightly
           _hazard("c1", "12#0", 7.0, "low")]        # new feature
    merged = risk.carry_over_hazards(new, [old, old_auto])
    by_ref = {h["feature_ref"]: h for h in merged}
    kept = by_ref["10#0"]
    ok = kept["hazard_id"] == old["hazard_id"]           # stable identity
    ok = ok and kept["status"] == schema.HAZARD_STATUS_ACCEPTED
    ok = ok and kept["notes"] == "reviewed"
    ok = ok and kept["risk"] == schema.RISK_HIGH         # user override kept
    ok = ok and kept["risk_source"] == schema.RISK_SOURCE_USER
    ok = ok and kept["auto_risk"] == "medium"            # fresh auto retained
    fresh = by_ref["12#0"]
    ok = ok and fresh["status"] == schema.HAZARD_STATUS_OPEN
    ok = ok and "11#0" not in by_ref                     # gone feature dropped
    return _result("re-scan carry-over: status/notes/user risk survive", ok)


def test_spans_and_summary() -> bool:
    hazards = [
        _hazard("c1", "1#0", 5.0, "high"),
        _hazard("c1", "2#0", 8.0, "low", end_kp=9.5),
        _hazard("", "m1", 3.0, "", source=schema.HAZARD_SOURCE_MANUAL),
    ]
    spans = risk.hazard_spans(hazards)
    ok = len(spans) == 3
    # Point hazards get a visible halfwidth; ranges keep their extent.
    point_span = next(s for s in spans if s[2] == "high")
    ok = ok and abs((point_span[1] - point_span[0]) - 0.05) < 1e-9
    range_span = next(s for s in spans if s[2] == "low")
    ok = ok and abs(range_span[0] - 8.0) < 1e-9 \
        and abs(range_span[1] - 9.5) < 1e-9
    # Severe spans sort last (drawn on top).
    ok = ok and spans[-1][2] == "high"
    counts = risk.summarise_hazards(hazards)
    ok = ok and counts["total"] == 3 and counts["high"] == 1 \
        and counts["low"] == 1 and counts[""] == 1 and counts["open"] == 3
    # Dense same-level fields merge to bounded spans (strip performance).
    dense = [_hazard("c1", f"d{i}#0", 5.0 + i * 0.01, "high")
             for i in range(50)]
    ok = ok and len(risk.hazard_spans(dense)) == 1
    ordered = risk.sort_hazards(list(reversed(hazards)))
    ok = ok and [h["feature_ref"] for h in ordered] == ["m1", "1#0", "2#0"]
    return _result("overview spans + summary + KP ordering", ok)


def test_hazards_csv() -> bool:
    plan = {"plan_id": "p1", "name": "Test Plan", "method": "plough",
            "direction": 1, "scope_start_kp": 0.0, "scope_end_kp": 20.0}
    checks = [{"check_id": "c1", "name": "Boulders"}]
    hazards = [
        _hazard("c1", "1#0", 5.0, "high", crossing=1,
                crossing_angle_deg=87.5, offset_m=0.0),
        _hazard("", "m1", 3.25, "medium", label="Charted wreck",
                source=schema.HAZARD_SOURCE_MANUAL, notes="from DTS"),
    ]
    text = io_csv.hazards_csv(plan, hazards, checks)
    lines = text.splitlines()
    ok = any(line.startswith("risk,status,kp") for line in lines)
    ok = ok and any(line.startswith("High,Open,5.000") and ",Boulders," in line
                    and ",87.5," in line for line in lines)
    ok = ok and any("Charted wreck,manual" in line and "from DTS" in line
                    for line in lines)
    return _result("hazards CSV export (labels, checks, angles)", ok)


def test_range_bounds_and_expression_rules() -> bool:
    """Explicit ≥/> and </≤ bounds, legacy inclusive default, expression
    hits, validation and the readable summary."""
    ar = attribute_rules
    # Adjoining bins with exclusive upper bounds: a boundary value lands
    # in exactly one bin.
    bins = [{"min": 0, "max": 5, "max_inclusive": False, "risk": "high"},
            {"min": 5, "max": 10, "max_inclusive": False, "risk": "medium"},
            {"min": 10, "max": 200, "risk": "low"}]
    config = {"attribute": "grade", "attribute_rules": bins}
    ok = risk.attribute_risk(config, {"grade": 4.999}) == "high"
    ok = ok and risk.attribute_risk(config, {"grade": 5}) == "medium"
    ok = ok and risk.attribute_risk(config, {"grade": 10}) == "low"
    ok = ok and risk.attribute_risk(config, {"grade": 200}) == "low"
    ok = ok and risk.attribute_risk(config, {"grade": 200.5}) == ""
    ok = ok and risk.attribute_risk(config, {"grade": "7,5"}) == "medium"
    ok = ok and risk.attribute_risk(config, {"grade": None}) == ""
    # Strict lower bound; legacy rules without flags stay inclusive.
    strict = {"min": 2, "min_inclusive": False}
    ok = ok and not ar.rule_matches(strict, 2) and ar.rule_matches(strict, 2.01)
    ok = ok and ar.rule_matches({"min": 2, "max": 3}, 3)
    ok = ok and ar.rule_matches({"max": 3, "max_inclusive": True}, 3)
    ok = ok and not ar.rule_matches({"max": 3, "max_inclusive": False}, 3)
    # Value rules: case-insensitive; null never matches.
    ok = ok and ar.rule_matches({"match": "Rock"}, " ROCK ")
    ok = ok and not ar.rule_matches({"match": "None"}, None)
    # Expression rules fire from pre-evaluated hits (aligned with the
    # expression rules only) and need no attribute at all.
    mixed = {"attribute": "", "attribute_rules": [
        {"expression": '"h" > 2', "risk": "high"},
        {"match": "X", "risk": "low"},
        {"expression": '"h" > 1', "risk": "medium"}]}
    ok = ok and risk.evaluate_risk(mixed, 500.0, {}, [False, True]) == "medium"
    ok = ok and risk.evaluate_risk(mixed, 500.0, {}, [True, True]) == "high"
    ok = ok and risk.evaluate_risk(mixed, 500.0, {}, None) == ""
    ok = ok and ar.expression_texts(mixed["attribute_rules"]) == \
        ['"h" > 2', '"h" > 1']
    # Attribute rules still work alongside expression rules.
    both = {"attribute": "cls", "attribute_rules": [
        {"expression": "1=1", "risk": "low"},
        {"match": "WRECK", "risk": "high"}]}
    ok = ok and risk.attribute_risk(both, {"cls": "wreck"}, [False]) == "high"
    ok = ok and risk.attribute_risk(both, {"cls": "sand"}, [True]) == "low"
    # Validation + summary.
    ok = ok and ar.validate_rule({"min": 5, "max": 2}) is not None
    ok = ok and ar.validate_rule({"match": "  "}) is not None
    ok = ok and ar.validate_rule({"min": "abc"}) is not None
    ok = ok and ar.validate_rule({}) is not None
    ok = ok and ar.validate_rule(bins[0]) is None
    ok = ok and ar.describe_rule(bins[0], "grade") == "0 \u2264 grade < 5"
    ok = ok and ar.describe_rule({"min": 2, "min_inclusive": False}, "h") == "2 < h"
    ok = ok and ar.describe_rule({"match": "ROCK"}, "soil") == "soil = ROCK"
    # Polygon rule: values and ranges together, any may match.
    poly = {"attribute": "soil", "match_values": ["ROCK"],
            "match_rules": [{"min": 5, "max_inclusive": False}]}
    rules = ar.polygon_match_rules(poly)
    ok = ok and len(rules) == 2
    ok = ok and ar.any_rule_matches(rules, "rock")
    ok = ok and ar.any_rule_matches(rules, 7) and not ar.any_rule_matches(rules, 4)
    return _result("range bounds, expression hits, validation, summaries", ok)


def run_all() -> list:
    return [
        test_risk_evaluation(),
        test_attribute_rule_text_form(),
        test_range_bounds_and_expression_rules(),
        test_carry_over(),
        test_spans_and_summary(),
        test_hazards_csv(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
