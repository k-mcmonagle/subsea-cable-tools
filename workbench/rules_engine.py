# -*- coding: utf-8 -*-
"""Pure KP-interval rules engine for route-suitability / burial assessment.

No QGIS imports — this is deterministic interval algebra over the route domain
so it can be unit-tested under plain Python. The acquisition layer
(``rules_inputs.py``) turns survey data into ``RuleHit`` intervals (the KP
ranges where each rule's condition is TRUE); this module resolves an ordered
stack of rules into per-method verdicts.

Domain model
------------
Everything lives in the route's KP domain (kilometres). An ``Interval`` is a
half-open-ish ``[start, end]`` range with ``start < end``. A ``Rule`` carries
its identity, evaluation ``seq`` (top-to-bottom, like Excel conditional
formatting), an ``action`` and the ``methods`` it applies to. A ``RuleHit``
pairs a rule with the intervals where its condition fires (already scoped).

Resolution semantics
--------------------
Rule *conditions* are evaluated order-independently into hits. *Order* only
matters at resolution. Per method, the domain is split at every hit breakpoint;
for each atomic interval the applicable rules are walked in ``seq`` order with a
running severity on the lattice::

    allowed (0) < risk 1 < risk 2 < risk 3 < excluded (4)

* ``exclude`` -> severity 4
* ``risk``    -> severity = max(current, risk_level)
* ``allow``   -> severity reset to 0 (an exception that overrides earlier rules)

Later rules override earlier ones. ``dominant_rule_id`` is the rule that set the
final severity; ``fired_rule_ids`` lists every rule whose condition covered the
interval. Adjacent equal verdicts are dissolved; sub-``min_range_km`` slivers are
absorbed into their more-severe neighbour (an excluded sliver is never dropped).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Severity lattice / status strings (kept in sync with schema constants).
SEVERITY_ALLOWED = 0
SEVERITY_EXCLUDED = 4
STATUS_ALLOWED = "allowed"
STATUS_RISK = "risk"
STATUS_EXCLUDED = "excluded"

ACTION_EXCLUDE = "exclude"
ACTION_RISK = "risk"
ACTION_ALLOW = "allow"

_TOL = 1e-9


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    start_km: float
    end_km: float

    @property
    def length_km(self) -> float:
        return max(0.0, self.end_km - self.start_km)

    def contains(self, kp: float, tol: float = _TOL) -> bool:
        return (self.start_km - tol) <= kp <= (self.end_km + tol)


@dataclass
class Rule:
    rule_id: str
    name: str
    seq: int
    action: str                       # exclude | risk | allow
    risk_level: int = 0
    methods: List[str] = field(default_factory=list)
    enabled: bool = True
    kind: str = ""


@dataclass
class RuleHit:
    rule: Rule
    intervals: List["Interval"] = field(default_factory=list)


@dataclass
class RangeVerdict:
    start_km: float
    end_km: float
    status: str
    risk_level: int
    fired_rule_ids: List[str] = field(default_factory=list)
    dominant_rule_id: Optional[str] = None

    @property
    def length_km(self) -> float:
        return max(0.0, self.end_km - self.start_km)


@dataclass
class RuleStat:
    rule_id: str
    name: str
    action: str
    risk_level: int
    coverage_km: float
    coverage_pct: float


@dataclass
class AssessmentResult:
    per_method: Dict[str, List[RangeVerdict]] = field(default_factory=dict)
    rule_stats: List[RuleStat] = field(default_factory=list)
    rule_hits: Dict[str, List["Interval"]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    domain: Optional["Interval"] = None


# ---------------------------------------------------------------------------
# Interval algebra
# ---------------------------------------------------------------------------


def normalize(intervals: List[Interval], tol: float = _TOL) -> List[Interval]:
    """Sort, drop zero/negative-length intervals, merge overlaps and abutments."""
    cleaned = []
    for iv in intervals:
        a, b = iv.start_km, iv.end_km
        if b < a:
            a, b = b, a
        if b - a > tol:
            cleaned.append((a, b))
    cleaned.sort()
    merged: List[Interval] = []
    for a, b in cleaned:
        if merged and a <= merged[-1].end_km + tol:
            last = merged[-1]
            merged[-1] = Interval(last.start_km, max(last.end_km, b))
        else:
            merged.append(Interval(a, b))
    return merged


def interval_length_km(intervals: List[Interval]) -> float:
    return sum(iv.length_km for iv in normalize(intervals))


def intersect_intervals(a: List[Interval], b: List[Interval]) -> List[Interval]:
    """Intersection of two interval lists (used for rule scoping)."""
    na, nb = normalize(a), normalize(b)
    out: List[Interval] = []
    i = j = 0
    while i < len(na) and j < len(nb):
        lo = max(na[i].start_km, nb[j].start_km)
        hi = min(na[i].end_km, nb[j].end_km)
        if hi - lo > _TOL:
            out.append(Interval(lo, hi))
        if na[i].end_km < nb[j].end_km:
            i += 1
        else:
            j += 1
    return out


def clip_intervals(intervals: List[Interval], domain: Interval) -> List[Interval]:
    return intersect_intervals(intervals, [domain])


def subtract_intervals(a: List[Interval], b: List[Interval]) -> List[Interval]:
    """Ranges of ``a`` not covered by ``b`` (interval-set difference)."""
    na, nb = normalize(a), normalize(b)
    out: List[Interval] = []
    j = 0
    for iv in na:
        cursor = iv.start_km
        while j < len(nb) and nb[j].end_km <= cursor + _TOL:
            j += 1
        k = j
        while k < len(nb) and nb[k].start_km < iv.end_km - _TOL:
            if nb[k].start_km - cursor > _TOL:
                out.append(Interval(cursor, nb[k].start_km))
            cursor = max(cursor, nb[k].end_km)
            if cursor >= iv.end_km - _TOL:
                break
            k += 1
        if iv.end_km - cursor > _TOL:
            out.append(Interval(cursor, iv.end_km))
    return normalize(out)


def complement_intervals(intervals: List[Interval], domain: Interval) -> List[Interval]:
    """Ranges of ``domain`` not covered by ``intervals``."""
    return subtract_intervals([domain], intervals)


def dilate_intervals(
    intervals: List[Interval],
    before_km: float = 0.0,
    after_km: float = 0.0,
    domain: Optional[Interval] = None,
) -> List[Interval]:
    """Extend each interval by ``before_km`` on the low-KP side and
    ``after_km`` on the high-KP side (both clamped to >= 0), then merge.

    Callers map travel direction onto the two sides: for direction -1 the
    "before" (approach) side is the high-KP side, so swap the arguments.
    """
    before_km = max(0.0, float(before_km or 0.0))
    after_km = max(0.0, float(after_km or 0.0))
    out = [Interval(iv.start_km - before_km, iv.end_km + after_km)
           for iv in normalize(intervals)]
    out = normalize(out)
    if domain is not None:
        out = clip_intervals(out, domain)
    return out


def dilate_intervals_variable(
    intervals: List[Interval],
    low_km_at,
    high_km_at,
    domain: Optional[Interval] = None,
) -> List[Interval]:
    """Dilate with per-boundary extensions (e.g. a water-depth multiple).

    ``low_km_at(kp)`` / ``high_km_at(kp)`` return the extension (km) to apply
    at an interval's low/high boundary, evaluated at that boundary's KP.
    ``None`` or negative results mean no extension there. Same low/high-side
    semantics as ``dilate_intervals``; callers map travel direction onto the
    two callables.
    """
    out: List[Interval] = []
    for iv in normalize(intervals):
        try:
            low = float(low_km_at(iv.start_km) or 0.0)
        except (TypeError, ValueError):
            low = 0.0
        try:
            high = float(high_km_at(iv.end_km) or 0.0)
        except (TypeError, ValueError):
            high = 0.0
        out.append(Interval(iv.start_km - max(low, 0.0),
                            iv.end_km + max(high, 0.0)))
    out = normalize(out)
    if domain is not None:
        out = clip_intervals(out, domain)
    return out


# ---------------------------------------------------------------------------
# Series -> intervals
# ---------------------------------------------------------------------------


def _cond_bounds(op: str, value: float, value2: Optional[float]) -> Tuple[float, float]:
    """The closed value-interval [lo, hi] where the condition holds."""
    if op in (">", ">="):
        return (float(value), math.inf)
    if op in ("<", "<="):
        return (-math.inf, float(value))
    if op == "between":
        hi = float(value2) if value2 is not None else float(value)
        lo = float(value)
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi)
    return (-math.inf, math.inf)


# Public name for the Burial Planner's refinement predicates
# (burial/analysis_task.py); keep it stable when refactoring.
cond_bounds = _cond_bounds


def _segment_true_range(
    v0: float, v1: float, lo: float, hi: float
) -> Optional[Tuple[float, float]]:
    """s-range within [0, 1] where the linear value v(s) lies in [lo, hi]."""
    if v1 == v0:
        return (0.0, 1.0) if (lo - _TOL) <= v0 <= (hi + _TOL) else None
    s_min, s_max = 0.0, 1.0
    slope = v1 - v0
    if lo != -math.inf:
        s = (lo - v0) / slope
        if slope > 0:
            s_min = max(s_min, s)
        else:
            s_max = min(s_max, s)
    if hi != math.inf:
        s = (hi - v0) / slope
        if slope > 0:
            s_max = min(s_max, s)
        else:
            s_min = max(s_min, s)
    if s_min <= s_max + _TOL:
        return (max(0.0, s_min), min(1.0, s_max))
    return None


def intervals_from_profile(
    series: List[Tuple[float, float]],
    op: str,
    value: float,
    value2: Optional[float] = None,
    abs_value: bool = False,
) -> List[Interval]:
    """Intervals where a continuous (kp, value) profile satisfies the condition.

    Values are linearly interpolated between samples, so a threshold crossing
    lands on the interpolated KP rather than snapping to a sample. When
    ``abs_value`` is set the magnitude is compared (e.g. slope steepness).
    """
    pts = sorted((float(kp), abs(float(v)) if abs_value else float(v)) for kp, v in series)
    lo, hi = _cond_bounds(op, value, value2)
    out: List[Interval] = []
    for (kp0, v0), (kp1, v1) in zip(pts, pts[1:]):
        if kp1 <= kp0:
            continue
        sr = _segment_true_range(v0, v1, lo, hi)
        if sr is None:
            continue
        a = kp0 + sr[0] * (kp1 - kp0)
        b = kp0 + sr[1] * (kp1 - kp0)
        if b - a > _TOL:
            out.append(Interval(a, b))
    return normalize(out)


def intervals_from_bool_series(
    series: List[Tuple[float, bool]], domain: Interval
) -> List[Interval]:
    """Intervals from a per-station boolean series via midpoint ownership.

    Each TRUE station owns the range from the midpoint to its previous station
    to the midpoint to its next station (clamped to the domain), so a run of
    TRUE stations forms one contiguous interval after normalisation.
    """
    pts = sorted((float(kp), bool(flag)) for kp, flag in series)
    n = len(pts)
    out: List[Interval] = []
    for i, (kp, flag) in enumerate(pts):
        if not flag:
            continue
        left = domain.start_km if i == 0 else 0.5 * (pts[i - 1][0] + kp)
        right = domain.end_km if i == n - 1 else 0.5 * (kp + pts[i + 1][0])
        a = max(domain.start_km, left)
        b = min(domain.end_km, right)
        if b - a > _TOL:
            out.append(Interval(a, b))
    return normalize(out)


def _interp_depth(xs: List[float], zs: List[float], kp: float) -> float:
    """Linear interpolation on a sorted (kp, depth) series, clamped at ends."""
    import bisect
    if kp <= xs[0]:
        return zs[0]
    if kp >= xs[-1]:
        return zs[-1]
    j = bisect.bisect_left(xs, kp)
    x0, x1 = xs[j - 1], xs[j]
    if x1 <= x0:
        return zs[j]
    t = (kp - x0) / (x1 - x0)
    return zs[j - 1] + t * (zs[j] - zs[j - 1])


def signed_slope_series(depth_series: List[Tuple[float, float]],
                        half_window_km: Optional[float] = None
                        ) -> List[Tuple[float, float]]:
    """Signed seabed slope (degrees) from a depth-magnitude series.
    Positive = shoaling with increasing KP (up-slope); negative = deepening
    (down-slope) — the plugin-wide sign convention.

    Slope at each station is a central difference over depths linearly
    interpolated at ``kp ± half_window_km`` (clamped to the series range),
    so the window keeps a consistent physical width even where stations are
    irregular — route vertices and contour crossings injected between the
    regular marks used to shrink the window to their local spacing and turn
    steps into near-vertical spikes. ``half_window_km`` defaults to the
    median station spacing; pass the acquisition step so coarse slope and
    the 1 m refinement predicate agree on scale.

    The unsigned variant (``rules_inputs._slope_series``) is the magnitude
    of this series. Callers map travel direction onto the sign
    (direction -1 swaps the up/down limits).
    """
    pts = sorted(depth_series)
    n = len(pts)
    if n < 2:
        return [(kp, 0.0) for kp, _ in pts]
    xs = [kp for kp, _ in pts]
    zs = [z for _, z in pts]
    if half_window_km is None:
        gaps = sorted(xs[i + 1] - xs[i] for i in range(n - 1))
        half_window_km = max(gaps[len(gaps) // 2], 1e-9)
    half = max(float(half_window_km), 1e-9)
    out: List[Tuple[float, float]] = []
    for kp in xs:
        k0 = max(xs[0], kp - half)
        k1 = min(xs[-1], kp + half)
        dx_m = (k1 - k0) * 1000.0
        if dx_m <= 1e-6:
            out.append((kp, 0.0))
            continue
        dz = _interp_depth(xs, zs, k1) - _interp_depth(xs, zs, k0)
        # Depth magnitudes grow downward, so negate for up-slope-positive.
        out.append((kp, math.degrees(math.atan2(-dz, dx_m))))
    return out


def intervals_from_signed_slope(
    slope_series: List[Tuple[float, float]],
    downslope_max_deg: Optional[float] = None,
    upslope_max_deg: Optional[float] = None,
) -> List[Interval]:
    """Intervals where a signed slope profile breaches either directional limit.

    Series sign: positive = shoaling (up-slope). ``upslope_max_deg`` limits
    positive slope; ``downslope_max_deg`` limits the magnitude of negative
    slope (deepening). Either may be None (no limit on that side); both
    limits are entered as magnitudes.
    """
    out: List[Interval] = []
    if downslope_max_deg is not None:
        out.extend(intervals_from_profile(slope_series, "<", -abs(float(downslope_max_deg))))
    if upslope_max_deg is not None:
        out.extend(intervals_from_profile(slope_series, ">", abs(float(upslope_max_deg))))
    return normalize(out)


def select_band(bands: List[Dict], wd: float) -> Optional[Dict]:
    """First band whose [min_wd, max_wd) contains the water depth (magnitude).

    A band omits either bound to leave that side open. No interpolation
    between bands — a station is governed by exactly the band it falls in.
    """
    for band in bands or []:
        lo = band.get("min_wd")
        hi = band.get("max_wd")
        if lo is not None and wd < float(lo):
            continue
        if hi is not None and wd >= float(hi):
            continue
        return band
    return None


def intervals_from_banded_threshold(
    value_series: List[Tuple[float, float]],
    wd_series: List[Tuple[float, float]],
    bands: List[Dict],
    op: str,
    domain: Interval,
) -> List[Interval]:
    """Intervals where a per-station value breaches its WD-band's limit.

    ``value_series`` and ``wd_series`` must share station KPs (extra stations
    on either side are ignored). Stations with no applicable band never fire.
    Evaluated per station (midpoint ownership) — band switching is a step
    change by design; callers wanting 1 m boundaries refine afterwards.
    """
    wd_by_kp = {round(kp, 9): wd for kp, wd in wd_series}
    flags: List[Tuple[float, bool]] = []
    for kp, value in value_series:
        wd = wd_by_kp.get(round(kp, 9))
        if wd is None:
            flags.append((kp, False))
            continue
        band = select_band(bands, wd)
        if band is None or band.get("limit") is None:
            flags.append((kp, False))
            continue
        lo, hi = _cond_bounds(op, float(band["limit"]), None)
        flags.append((kp, (lo - _TOL) <= value <= (hi + _TOL)))
    return intervals_from_bool_series(flags, domain)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def severity_to_status(severity: int) -> str:
    if severity >= SEVERITY_EXCLUDED:
        return STATUS_EXCLUDED
    if severity > SEVERITY_ALLOWED:
        return STATUS_RISK
    return STATUS_ALLOWED


def _collect_breakpoints(domain: Interval, hits: List[RuleHit], tol: float) -> List[float]:
    marks = [domain.start_km, domain.end_km]
    for hit in hits:
        for iv in hit.intervals:
            if domain.start_km - tol <= iv.start_km <= domain.end_km + tol:
                marks.append(min(max(iv.start_km, domain.start_km), domain.end_km))
            if domain.start_km - tol <= iv.end_km <= domain.end_km + tol:
                marks.append(min(max(iv.end_km, domain.start_km), domain.end_km))
    marks.sort()
    unique: List[float] = []
    for m in marks:
        if not unique or m - unique[-1] > tol:
            unique.append(m)
    return unique


def _resolve_atom(midpoint: float, applicable: List[RuleHit], tol: float
                  ) -> Tuple[int, Optional[str], List[str]]:
    """Walk applicable rules in seq order; return (severity, dominant, fired)."""
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


def dissolve_adjacent(verdicts: List[RangeVerdict], tol: float = 1e-6) -> List[RangeVerdict]:
    """Merge neighbouring verdicts that share status, risk level and dominant rule."""
    out: List[RangeVerdict] = []
    for v in verdicts:
        if out and out[-1].status == v.status and out[-1].risk_level == v.risk_level \
                and out[-1].dominant_rule_id == v.dominant_rule_id \
                and abs(out[-1].end_km - v.start_km) <= tol:
            prev = out[-1]
            merged_fired = list(dict.fromkeys(prev.fired_rule_ids + v.fired_rule_ids))
            out[-1] = RangeVerdict(prev.start_km, v.end_km, prev.status, prev.risk_level,
                                   merged_fired, prev.dominant_rule_id)
        else:
            out.append(v)
    return out


def _merge_slivers(verdicts: List[RangeVerdict], min_km: float, tol: float) -> List[RangeVerdict]:
    """Absorb sub-min slivers into their more-severe neighbour.

    An excluded sliver stricter than both neighbours is kept (exclusions are
    never silently dropped).

    Single left-to-right pass with local backtracking. The previous
    restart-from-scratch loop re-sorted and re-dissolved the whole list
    after every merge (O(n² log n)); a merge only changes its own
    neighbourhood, so re-examining from one entry back reaches the same
    fixpoint.
    """
    if min_km <= 0 or len(verdicts) < 2:
        return verdicts
    work = list(verdicts)

    def dissolve_at(index: int) -> int:
        """Dissolve ``index`` into equal neighbours; return its new index."""
        # Merge with left neighbour first, then right (same outcome as a
        # full dissolve pass over an otherwise-dissolved list).
        while index > 0:
            left, v = work[index - 1], work[index]
            if left.status == v.status and left.risk_level == v.risk_level \
                    and left.dominant_rule_id == v.dominant_rule_id \
                    and abs(left.end_km - v.start_km) <= tol:
                fired = list(dict.fromkeys(left.fired_rule_ids + v.fired_rule_ids))
                work[index - 1] = RangeVerdict(
                    left.start_km, v.end_km, left.status, left.risk_level,
                    fired, left.dominant_rule_id)
                del work[index]
                index -= 1
            else:
                break
        while index < len(work) - 1:
            v, right = work[index], work[index + 1]
            if v.status == right.status and v.risk_level == right.risk_level \
                    and v.dominant_rule_id == right.dominant_rule_id \
                    and abs(v.end_km - right.start_km) <= tol:
                fired = list(dict.fromkeys(v.fired_rule_ids + right.fired_rule_ids))
                work[index] = RangeVerdict(
                    v.start_km, right.end_km, v.status, v.risk_level,
                    fired, v.dominant_rule_id)
                del work[index + 1]
            else:
                break
        return index

    i = 0
    while i < len(work) and len(work) > 1:
        v = work[i]
        if v.length_km >= min_km - tol:
            i += 1
            continue
        left = work[i - 1] if i > 0 else None
        right = work[i + 1] if i < len(work) - 1 else None
        best = None
        best_index = -1
        best_sev = -1
        for neigh, index in ((left, i - 1), (right, i + 1)):
            if neigh is not None and neigh.risk_level > best_sev:
                best_sev = neigh.risk_level
                best = neigh
                best_index = index
        if best is None or v.risk_level > best_sev:
            # No neighbour, or this sliver is stricter than both -> keep it.
            i += 1
            continue
        # Extend the chosen neighbour to swallow the sliver.
        new_start = min(best.start_km, v.start_km)
        new_end = max(best.end_km, v.end_km)
        fired = list(dict.fromkeys(best.fired_rule_ids + v.fired_rule_ids))
        merged = RangeVerdict(new_start, new_end, best.status, best.risk_level,
                              fired, best.dominant_rule_id)
        low = min(i, best_index)
        work[low] = merged
        del work[low + 1]
        low = dissolve_at(low)
        # The merge only changed this neighbourhood — re-examine from just
        # before it.
        i = max(low - 1, 0)
    return work


def evaluate(
    domain: Interval,
    methods: List[str],
    hits: List[RuleHit],
    *,
    min_range_km: float = 0.0,
    tol_km: float = 1e-6,
) -> AssessmentResult:
    """Resolve the rule stack into per-method verdicts + per-rule coverage stats."""
    result = AssessmentResult(per_method={}, rule_stats=[], warnings=[], domain=domain)
    domain_len = max(domain.length_km, _TOL)

    # Per-rule coverage stats (method-independent: where each rule fires).
    for hit in hits:
        clipped = clip_intervals(hit.intervals, domain)
        result.rule_hits[hit.rule.rule_id] = clipped
        cov = interval_length_km(clipped)
        result.rule_stats.append(RuleStat(
            rule_id=hit.rule.rule_id,
            name=hit.rule.name,
            action=hit.rule.action,
            risk_level=int(hit.rule.risk_level or 0),
            coverage_km=cov,
            coverage_pct=100.0 * cov / domain_len,
        ))

    for method in methods:
        applicable = [
            RuleHit(h.rule, clip_intervals(h.intervals, domain))
            for h in hits
            if h.rule.enabled and method in h.rule.methods
        ]
        applicable.sort(key=lambda h: h.rule.seq)
        breaks = _collect_breakpoints(domain, applicable, tol_km)
        # Sweep: atoms are visited in KP order, so each rule keeps a cursor
        # into its (normalized, sorted, disjoint) interval list instead of
        # scanning every interval per atom — the per-atom containment test
        # was O(total intervals) and made noisy threshold rules quadratic.
        interval_lists = [h.intervals for h in applicable]
        rules = [h.rule for h in applicable]
        cursors = [0] * len(applicable)
        verdicts: List[RangeVerdict] = []
        for a, b in zip(breaks, breaks[1:]):
            if b - a <= tol_km:
                continue
            mid = 0.5 * (a + b)
            severity = SEVERITY_ALLOWED
            dominant: Optional[str] = None
            fired: List[str] = []
            for index, rule in enumerate(rules):
                ivs = interval_lists[index]
                cur = cursors[index]
                count = len(ivs)
                while cur < count and ivs[cur].end_km < mid - tol_km:
                    cur += 1
                cursors[index] = cur
                if cur >= count or ivs[cur].start_km > mid + tol_km:
                    continue
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
            verdicts.append(RangeVerdict(a, b, severity_to_status(severity), severity,
                                         fired, dominant))
        verdicts = dissolve_adjacent(verdicts, tol_km)
        verdicts = _merge_slivers(verdicts, min_range_km, tol_km)
        result.per_method[method] = verdicts

    return result


def summarise(verdicts: List[RangeVerdict]) -> Dict[str, float]:
    """Total km per status for a method's verdicts."""
    totals = {STATUS_ALLOWED: 0.0, STATUS_RISK: 0.0, STATUS_EXCLUDED: 0.0}
    for v in verdicts:
        totals[v.status] = totals.get(v.status, 0.0) + v.length_km
    return totals
