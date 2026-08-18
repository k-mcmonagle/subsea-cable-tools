# -*- coding: utf-8 -*-
"""Golden equivalence checks for the sweep-line rules-engine resolver.

The O(N²) per-atom containment scan and the restart-on-every-merge sliver
loop were replaced with a sweep + local-backtrack pass. These checks embed
the ORIGINAL implementations verbatim and fuzz the new ``evaluate`` against
them over randomized rule stacks, so the precedence semantics (seq order,
allow-resets-severity, dominant-rule tie-breaks, sliver keep rules) are
proven unchanged.
"""

from __future__ import annotations

import random
from typing import List, Optional, Tuple

from ..workbench import rules_engine as eng
from ..workbench.rules_engine import (
    ACTION_ALLOW,
    ACTION_EXCLUDE,
    ACTION_RISK,
    Interval,
    RangeVerdict,
    Rule,
    RuleHit,
    SEVERITY_ALLOWED,
    SEVERITY_EXCLUDED,
    clip_intervals,
    dissolve_adjacent,
    severity_to_status,
    _collect_breakpoints,
)


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


# -- the original implementations, verbatim ---------------------------------

def _resolve_atom_ref(midpoint, applicable, tol):
    severity = SEVERITY_ALLOWED
    dominant: Optional[str] = None
    fired: List[str] = []
    for hit in applicable:
        if not any(iv.contains(midpoint, tol) for iv in hit.intervals):
            continue
        rule = hit.rule
        fired.append(rule.rule_id)
        if rule.action == ACTION_EXCLUDE:
            severity = SEVERITY_EXCLUDED
            dominant = rule.rule_id
        elif rule.action == ACTION_RISK:
            level = int(rule.risk_level or 0)
            if level > severity:
                severity = level
                dominant = rule.rule_id
            elif level == severity and severity > SEVERITY_ALLOWED:
                dominant = rule.rule_id
        elif rule.action == ACTION_ALLOW:
            severity = SEVERITY_ALLOWED
            dominant = rule.rule_id
    return severity, dominant, fired


def _merge_slivers_ref(verdicts, min_km, tol):
    if min_km <= 0 or len(verdicts) < 2:
        return verdicts
    work = list(verdicts)
    changed = True
    while changed and len(work) > 1:
        changed = False
        for i, v in enumerate(work):
            if v.length_km >= min_km - tol:
                continue
            left = work[i - 1] if i > 0 else None
            right = work[i + 1] if i < len(work) - 1 else None
            best = None
            best_sev = -1
            for neigh in (left, right):
                if neigh is not None and neigh.risk_level > best_sev:
                    best_sev = neigh.risk_level
                    best = neigh
            if best is None or v.risk_level > best_sev:
                continue
            new_start = min(best.start_km, v.start_km)
            new_end = max(best.end_km, v.end_km)
            fired = list(dict.fromkeys(best.fired_rule_ids + v.fired_rule_ids))
            best_new = RangeVerdict(new_start, new_end, best.status,
                                    best.risk_level, fired,
                                    best.dominant_rule_id)
            work = [w for w in work if w is not v and w is not best]
            work.append(best_new)
            work.sort(key=lambda w: w.start_km)
            work = dissolve_adjacent(work, tol)
            changed = True
            break
    return work


def _evaluate_ref(domain, methods, hits, min_range_km=0.0, tol_km=1e-6):
    """The original per-method resolution (verdicts only)."""
    per_method = {}
    for method in methods:
        applicable = [
            RuleHit(h.rule, clip_intervals(h.intervals, domain))
            for h in hits
            if h.rule.enabled and method in h.rule.methods
        ]
        applicable.sort(key=lambda h: h.rule.seq)
        breaks = _collect_breakpoints(domain, applicable, tol_km)
        verdicts = []
        for a, b in zip(breaks, breaks[1:]):
            if b - a <= tol_km:
                continue
            mid = 0.5 * (a + b)
            severity, dominant, fired = _resolve_atom_ref(
                mid, applicable, tol_km)
            verdicts.append(RangeVerdict(a, b, severity_to_status(severity),
                                         severity, fired, dominant))
        verdicts = dissolve_adjacent(verdicts, tol_km)
        verdicts = _merge_slivers_ref(verdicts, min_range_km, tol_km)
        per_method[method] = verdicts
    return per_method


# -- fuzzing -----------------------------------------------------------------

def _random_stack(rng: random.Random):
    domain = Interval(0.0, rng.uniform(5.0, 60.0))
    rule_count = rng.randint(1, 8)
    hits = []
    for seq in range(rule_count):
        action = rng.choice([ACTION_EXCLUDE, ACTION_RISK, ACTION_RISK,
                             ACTION_ALLOW])
        rule = Rule(
            rule_id=f"r{seq}", name=f"rule {seq}", seq=seq, action=action,
            risk_level=rng.randint(1, 4) if action == ACTION_RISK else 0,
            methods=["plough"], enabled=rng.random() > 0.1)
        intervals = []
        for _n in range(rng.randint(0, 12)):
            start = rng.uniform(-2.0, domain.end_km)
            intervals.append(Interval(start,
                                      start + rng.uniform(0.001, 6.0)))
        hits.append(RuleHit(rule, eng.normalize(intervals)))
    min_range = rng.choice([0.0, 0.0, rng.uniform(0.1, 2.0)])
    return domain, hits, min_range


def _verdict_tuple(v: RangeVerdict) -> Tuple:
    return (round(v.start_km, 9), round(v.end_km, 9), v.status,
            v.risk_level, tuple(v.fired_rule_ids), v.dominant_rule_id)


def test_sweep_matches_reference() -> bool:
    rng = random.Random(20260818)
    mismatches = 0
    for trial in range(300):
        domain, hits, min_range = _random_stack(rng)
        got = eng.evaluate(domain, ["plough"], hits,
                           min_range_km=min_range).per_method["plough"]
        want = _evaluate_ref(domain, ["plough"], hits,
                             min_range_km=min_range)["plough"]
        if [_verdict_tuple(v) for v in got] != \
                [_verdict_tuple(v) for v in want]:
            mismatches += 1
            if mismatches == 1:
                print(f"  first mismatch at trial {trial}: "
                      f"{[_verdict_tuple(v) for v in got][:4]} vs "
                      f"{[_verdict_tuple(v) for v in want][:4]}")
    return _result("sweep resolver == reference over 300 random stacks",
                   mismatches == 0, f"{mismatches} mismatch(es)")


def test_sliver_semantics_preserved() -> bool:
    """The subtle sliver rules, checked directly on the new code."""
    def verdict(start, end, severity):
        return RangeVerdict(start, end, severity_to_status(severity),
                            severity, [f"x{start}"], f"x{start}")

    # An excluded sliver stricter than both neighbours survives.
    kept = eng._merge_slivers(
        [verdict(0, 5, 0), verdict(5, 5.05, SEVERITY_EXCLUDED),
         verdict(5.05, 10, 0)], 0.5, 1e-6)
    ok = any(v.status == "excluded" for v in kept) and len(kept) == 3
    # A weak sliver is swallowed by its more severe neighbour.
    merged = eng._merge_slivers(
        [verdict(0, 5, SEVERITY_EXCLUDED), verdict(5, 5.05, 1),
         verdict(5.05, 10, 0)], 0.5, 1e-6)
    ok = ok and len(merged) == 2 and merged[0].end_km == 5.05 \
        and merged[0].status == "excluded"
    return _result("sliver keep/swallow rules preserved", ok)


def run_all() -> list:
    return [
        test_sweep_matches_reference(),
        test_sliver_semantics_preserved(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
