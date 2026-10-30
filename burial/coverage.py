"""Route coverage and the contribution unique to each criterion (no QGIS).

Inputs are the stored, extended fire-bar ranges, before stack/sliver
resolution. Uniqueness is geometric coverage, not a prediction of the
sections that a new plan generation would produce.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Dict, Iterable, Mapping, Optional, Tuple

from ..workbench import rules_engine as eng
from . import schema


@dataclass
class Coverage:
    total_km: float = 0.0
    unique_km: float = 0.0


def is_exclusion(rule: Dict) -> bool:
    """Whether the criterion can exclude (enabled is checked separately)."""
    return (rule.get("kind") != schema.RULE_KIND_COVERAGE
            and rule.get("criterion_class") != schema.CRITERION_SCREENING
            and (rule.get("action") or eng.ACTION_EXCLUDE) == eng.ACTION_EXCLUDE)


def applies_to_method(rule: Dict, method: str) -> bool:
    try:
        methods = json.loads(rule.get("methods_json") or "[]")
    except (TypeError, ValueError):
        methods = []
    methods = schema.normalise_methods(methods)
    return not methods or schema.normalise_method(method) in methods


def route_coverage(ranges: Mapping[str, Iterable[Tuple[float, float]]],
                   scope: eng.Interval,
                   unique_within: Optional[Iterable[Tuple[float, float]]] = None
                   ) -> Dict[str, Coverage]:
    """Total and sole-owner length, clipped to the plan scope.

    The caller supplies only the criteria being compared. Merge within
    each criterion first so duplicate/overlapping features never count as
    multiple criteria. Sweep the boundaries once to avoid comparing every
    pair of ranges on large routes. ``unique_within`` can restrict the sole
    contribution to resolved exclusions, honouring allow exceptions without
    treating sliver-merged extensions as a criterion's own footprint.
    """
    result = {key: Coverage() for key in ranges}
    if scope.length_km <= 0:
        return result
    boundaries = {}
    for key, spans in ranges.items():
        intervals = eng.clip_intervals([eng.Interval(a, b) for a, b in spans], scope)
        result[key].total_km = sum(iv.length_km for iv in intervals)
        for iv in intervals:
            boundaries.setdefault(iv.start_km, []).append((key, True))
            boundaries.setdefault(iv.end_km, []).append((key, False))
    unique_ranges = {key: [] for key in ranges}
    active = set()
    previous = scope.start_km
    for kp, changes in sorted(boundaries.items()):
        if len(active) == 1:
            key = next(iter(active))
            if unique_within is None:
                result[key].unique_km += kp - previous
            else:
                unique_ranges[key].append(eng.Interval(previous, kp))
        for key, starts in changes:
            if starts:
                active.add(key)
            else:
                active.discard(key)
        previous = kp
    if unique_within is not None:
        mask = eng.clip_intervals([eng.Interval(a, b) for a, b in unique_within], scope)
        for key, intervals in unique_ranges.items():
            result[key].unique_km = sum(iv.length_km for iv in eng.intersect_intervals(intervals, mask))
    return result


def length_label(length_km: float, domain_km: float) -> str:
    pct = 100.0 * length_km / domain_km if domain_km > 0 else 0.0
    return f"{length_km:.3f} km · {pct:.1f}%"
