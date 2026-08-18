# -*- coding: utf-8 -*-
"""Candidate-section generation for the Burial Planner.

Headless: pure python over the shared interval engine
(``workbench/rules_engine.py``). Acquisition (turning layers into intervals)
happens elsewhere (``analysis_task.py`` via ``workbench/rules_inputs.py``);
this module takes per-rule interval results and produces the plan:

    excluded / influence / screening / insufficient-information / available
    -> candidate sections -> boundary events -> merged event set -> sections

Determinism: identical inputs produce identical output. Ids and timestamps
are injected (``id_fn`` / ``now_utc``) so runs are reproducible in tests.

Spec anchors: generation algorithm (§12), invariants (§13), precision (§14.3).
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..workbench import rules_engine as eng
from ..workbench.rules_engine import Interval, Rule, RuleHit
from . import events as ev
from . import schema

BOUNDARY_REFINE_TOL_M = 0.1

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class GenParams:
    scope_start_kp: float
    scope_end_kp: float
    direction: int = 1
    method: str = schema.METHOD_PLOUGH
    min_section_km: float = 0.0
    coarse_step_m: float = 50.0
    refine_tol_m: float = BOUNDARY_REFINE_TOL_M
    sliver_tol_km: float = 0.0
    cross_offset_m: float = 0.0     # 0 = auto (resolved profile step by model)
    profile_step_m: float = 0.0     # 0 = auto (bathymetry cell size)

    @property
    def scope(self) -> Interval:
        lo = min(self.scope_start_kp, self.scope_end_kp)
        hi = max(self.scope_start_kp, self.scope_end_kp)
        return Interval(lo, max(hi, lo + 1e-9))

    @property
    def effective_cross_offset_m(self) -> float:
        """Legacy context-free cross-offset fallback.

        PlanModel.resolve_cross_offset_m can inspect bathymetry resolution and
        is the authoritative Burial Planner path. This property remains for
        callers that only have GenParams.
        """
        if self.cross_offset_m > 0:
            return self.cross_offset_m
        if self.profile_step_m > 0:
            return self.profile_step_m
        return min(self.coarse_step_m, 5.0)

    def to_dict(self) -> Dict:
        return {
            "scope_start_kp": self.scope_start_kp,
            "scope_end_kp": self.scope_end_kp,
            "direction": self.direction,
            "method": self.method,
            "min_section_km": self.min_section_km,
            "coarse_step_m": self.coarse_step_m,
            "refine_tol_m": self.refine_tol_m,
            "sliver_tol_km": self.sliver_tol_km,
            "cross_offset_m": self.cross_offset_m,
            "profile_step_m": self.profile_step_m,
        }


@dataclass
class RuleAcquisition:
    """One rule's acquisition result (from cache or a fresh run)."""
    rule_row: Dict
    footprint: List[Interval] = field(default_factory=list)
    nodata: List[Interval] = field(default_factory=list)
    error: str = ""   # non-empty -> rule failed and was skipped (warning)


@dataclass
class LabelledInterval:
    start_km: float
    end_km: float
    rule_id: str = ""
    rule_name: str = ""

    @property
    def interval(self) -> Interval:
        return Interval(self.start_km, self.end_km)


@dataclass
class GenerationOutput:
    events: List[Dict] = field(default_factory=list)
    sections: List[Dict] = field(default_factory=list)
    excluded: List[eng.RangeVerdict] = field(default_factory=list)
    screening: List[eng.RangeVerdict] = field(default_factory=list)
    influence: List[LabelledInterval] = field(default_factory=list)
    insufficient: List[Interval] = field(default_factory=list)
    candidates: List[Interval] = field(default_factory=list)
    dropped_short: List[Interval] = field(default_factory=list)
    conflicts: List[Dict] = field(default_factory=list)   # events now in exclusion
    warnings: List[str] = field(default_factory=list)
    summary: Dict = field(default_factory=dict)
    proposal_diff: Optional[Dict] = None
    # Resolved per-rule footprints (extension buffers included) — feeds the
    # per-criterion fire bars and coverage after the plan is reopened.
    rule_hits: Dict[str, List[Interval]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Config / cache helpers
# ---------------------------------------------------------------------------


def rule_config(rule_row: Dict) -> Dict:
    try:
        config = json.loads(rule_row.get("config_json") or "{}")
    except (ValueError, TypeError):
        config = {}
    return config if isinstance(config, dict) else {}


# Exclusion Area extension config. Legacy rules carry a single symmetric
# ``extend_m``; current rules carry a mode plus per-side values, where
# "before"/"after" follow the direction of installation (like the Constraint
# Influence Zone) and mode "wd" scales each side by the water depth at the
# footprint boundary.
EXTEND_MODE_FIXED = "fixed"
EXTEND_MODE_WD = "wd"
EXTENSION_CONFIG_KEYS = (
    "extend_m", "extend_mode", "extend_before_m", "extend_after_m",
    "extend_before_wd", "extend_after_wd",
)


def extension_config(config: Dict) -> Dict:
    """Normalised extension settings: {mode, before, after}.

    ``before``/``after`` are metres for mode "fixed" and water-depth
    multiples for mode "wd". Legacy symmetric ``extend_m`` maps to fixed
    before = after = extend_m.
    """
    mode = (config.get("extend_mode") or "").strip().lower()
    if mode == EXTEND_MODE_WD:
        return {
            "mode": EXTEND_MODE_WD,
            "before": max(0.0, float(config.get("extend_before_wd") or 0.0)),
            "after": max(0.0, float(config.get("extend_after_wd") or 0.0)),
        }
    legacy = max(0.0, float(config.get("extend_m") or 0.0))
    if mode == EXTEND_MODE_FIXED or "extend_before_m" in config \
            or "extend_after_m" in config:
        return {
            "mode": EXTEND_MODE_FIXED,
            "before": max(0.0, float(config.get("extend_before_m", legacy) or 0.0)),
            "after": max(0.0, float(config.get("extend_after_m", legacy) or 0.0)),
        }
    return {"mode": EXTEND_MODE_FIXED, "before": legacy, "after": legacy}


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def rule_cache_key(rule_row: Dict, input_fingerprint: str, scope: Interval,
                   step_m: float, rpl_fingerprint: str, direction: int,
                   profile_step_m: Optional[float] = None,
                   refine_tol_m: float = BOUNDARY_REFINE_TOL_M) -> str:
    """Cache key for one rule's acquisition (spec §14.4).

    Direction participates only when the rule's condition is direction-aware
    (signed slope); influence/extension buffers are resolution-time interval
    ops and deliberately do not invalidate acquisition. Boundary-refinement
    tolerance does participate because refined intervals are cached.
    """
    config = rule_config(rule_row)
    acquisition_config = {
        k: v for k, v in config.items()
        if k not in EXTENSION_CONFIG_KEYS
        and k not in ("influence_before_m", "influence_after_m")
    }
    parts = {
        "kind": rule_row.get("kind") or "",
        "criterion_class": rule_row.get("criterion_class") or "",
        "config": acquisition_config,
        "input_fingerprint": input_fingerprint or "",
        "scope": [round(scope.start_km, 6), round(scope.end_km, 6)],
        "step_m": float(step_m),
        "refine_tol_m": float(refine_tol_m),
        "rpl_fingerprint": rpl_fingerprint or "",
    }
    # Burial threshold acquisition can use a denser persisted bathymetry
    # profile than the coarse rule-search stations. Its resolution affects
    # whether a narrow depth/slope feature is represented, so changing that
    # resolution must invalidate an otherwise identical acquisition cache.
    if (rule_row.get("kind") or "") == "threshold_profile" \
            and profile_step_m is not None:
        parts["profile_step_m"] = float(profile_step_m)
    if acquisition_config.get("slope_signed"):
        parts["direction"] = int(direction)
    return hashlib.sha1(canonical_json(parts).encode("utf-8")).hexdigest()


def rules_snapshot(rule_rows: Sequence[Dict]) -> str:
    """Frozen JSON of the rule stack at run time (reproducibility)."""
    return canonical_json([dict(r) for r in rule_rows])


# ---------------------------------------------------------------------------
# Boundary refinement (§14.3)
# ---------------------------------------------------------------------------


def refine_boundary(predicate: Callable[[float], bool], inside_kp: float,
                    outside_kp: float,
                    tol_km: float = BOUNDARY_REFINE_TOL_M / 1000.0,
                    max_iter: int = 40) -> float:
    """Bisect the footprint boundary between an inside and an outside KP.

    ``predicate(kp)`` is True inside the rule's footprint. Returns the
    boundary located to within ``tol_km`` (~9 evaluations for a 50 m step
    at 0.1 m tolerance). The bracket must genuinely straddle the boundary;
    callers verify that before calling.
    """
    a, b = float(inside_kp), float(outside_kp)
    for _ in range(max_iter):
        if abs(b - a) <= tol_km:
            break
        mid = 0.5 * (a + b)
        if predicate(mid):
            a = mid
        else:
            b = mid
    return 0.5 * (a + b)


class RefinementCancelled(Exception):
    """Raised by ``refine_intervals`` when its cancel callback fires."""


def refine_intervals(intervals: List[Interval], predicate: Callable[[float], bool],
                     coarse_step_km: float, domain: Interval,
                     tol_km: float = BOUNDARY_REFINE_TOL_M / 1000.0,
                     cancel: Optional[Callable[[], bool]] = None
                     ) -> List[Interval]:
    """Refine every interval boundary to ``tol_km`` by bisection.

    Each boundary is bracketed one coarse step outward from the interval and
    a bounded distance inward; if the predicate does not actually change
    across the bracket (e.g. the interval abuts the domain edge, or the
    condition is flat there) the boundary is left where acquisition put it.
    ``cancel`` (checked per interval) raises ``RefinementCancelled`` so a
    Stop pressed during refinement actually stops — a partially refined
    rule must be discarded by the caller, never cached.
    """
    out: List[Interval] = []
    for iv in eng.normalize(intervals):
        if cancel is not None and cancel():
            raise RefinementCancelled()
        start, end = iv.start_km, iv.end_km
        inward = min(coarse_step_km, 0.5 * iv.length_km)

        if start > domain.start_km + 1e-9:
            inside = start + inward
            outside = max(domain.start_km, start - coarse_step_km)
            try:
                if predicate(inside) and not predicate(outside):
                    start = refine_boundary(predicate, inside, outside, tol_km)
            except Exception:
                pass  # predicate failure -> keep the acquired boundary

        if end < domain.end_km - 1e-9:
            inside = end - inward
            outside = min(domain.end_km, end + coarse_step_km)
            try:
                if predicate(inside) and not predicate(outside):
                    end = refine_boundary(predicate, inside, outside, tol_km)
            except Exception:
                pass

        if end - start > 1e-9:
            out.append(Interval(start, end))
    return eng.normalize(out)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _engine_rule(rule_row: Dict) -> Rule:
    try:
        methods = json.loads(rule_row.get("methods_json") or "[]")
    except (ValueError, TypeError):
        methods = []
    # Alias ids ("jet" from a Workbench copy) are healed at read time so
    # rules stored before the vocabulary was normalised keep firing.
    methods = schema.normalise_methods(methods)
    return Rule(
        rule_id=str(rule_row.get("rule_id")),
        name=rule_row.get("name") or "",
        seq=int(rule_row.get("seq") or 0),
        action=rule_row.get("action") or eng.ACTION_EXCLUDE,
        risk_level=int(rule_row.get("risk_level") or 0),
        methods=list(methods) or list(schema.METHODS),
        enabled=bool(int(rule_row.get("enabled") or 0)),
        kind=rule_row.get("kind") or "",
    )


def _screening_as_risk(rule_row: Dict) -> Dict:
    """Screening Criteria annotate, never exclude (guide alignment, spec §5).

    A rule classed ``screening`` whose action is ``exclude`` is resolved as a
    risk-level-2 rule instead; exclusion-class rules pass through unchanged.
    """
    if (rule_row.get("criterion_class") == schema.CRITERION_SCREENING
            and (rule_row.get("action") or "") == eng.ACTION_EXCLUDE):
        row = dict(rule_row)
        row["action"] = eng.ACTION_RISK
        row["risk_level"] = int(row.get("risk_level") or 0) or 2
        return row
    return rule_row


def _extend_footprint(footprint: List[Interval], config: Dict, direction: int,
                      scope: Interval,
                      depth_at: Optional[Callable[[float], Optional[float]]],
                      warn: Callable[[str], None], rule_name: str
                      ) -> List[Interval]:
    """Apply the rule's Exclusion Area extension to its footprint.

    "Before"/"after" follow the direction of installation; mode "wd" scales
    each side by the water depth at the footprint boundary (falls back to no
    extension, with a warning, when no depth is available there).
    """
    ext = extension_config(config)
    if ext["before"] <= 0 and ext["after"] <= 0:
        return footprint
    # Map travel direction onto the low/high-KP sides.
    if int(direction) >= 0:
        low, high = ext["before"], ext["after"]
    else:
        low, high = ext["after"], ext["before"]
    if ext["mode"] == EXTEND_MODE_WD:
        if depth_at is None:
            warn(f"Rule '{rule_name}': water-depth-based extension needs a "
                 "bathymetry profile — extension not applied.")
            return footprint
        missing = [False]

        def side_km_at(factor: float):
            def at(kp: float) -> float:
                depth = depth_at(kp)
                if depth is None:
                    missing[0] = True
                    return 0.0
                return factor * abs(float(depth)) / 1000.0
            return at

        extended = eng.dilate_intervals_variable(
            footprint, side_km_at(low), side_km_at(high), scope)
        if missing[0]:
            warn(f"Rule '{rule_name}': no depth at one or more footprint "
                 "boundaries — those sides were not extended.")
        return extended
    return eng.dilate_intervals(footprint, low / 1000.0, high / 1000.0, scope)


def resolve_stack(params: GenParams, acquisitions: Sequence[RuleAcquisition],
                  depth_at: Optional[Callable[[float], Optional[float]]] = None
                  ) -> Tuple[eng.AssessmentResult, List[LabelledInterval], List[Interval], List[str]]:
    """Resolve the acquired stack: verdicts + influence zones + no-data + warnings.

    ``depth_at(kp) -> depth magnitude | None`` enables water-depth-scaled
    Exclusion Area extensions; without it those rules warn and stay
    unextended.
    """
    scope = params.scope
    warnings: List[str] = []
    hits: List[RuleHit] = []
    influence: List[LabelledInterval] = []
    nodata: List[Interval] = []

    for acq in acquisitions:
        row = _screening_as_risk(acq.rule_row)
        rule = _engine_rule(row)
        if acq.error:
            warnings.append(f"Rule '{rule.name}': {acq.error} — skipped.")
        config = rule_config(row)

        footprint = eng.clip_intervals(acq.footprint, scope)
        footprint = _extend_footprint(footprint, config, params.direction,
                                      scope, depth_at, warnings.append,
                                      rule.name)
        hits.append(RuleHit(rule, footprint))

        before_km = max(0.0, float(config.get("influence_before_m") or 0.0)) / 1000.0
        after_km = max(0.0, float(config.get("influence_after_m") or 0.0)) / 1000.0
        if (before_km > 0 or after_km > 0) and footprint:
            # Direction-aware: "before" is the approach side of travel.
            if int(params.direction) >= 0:
                zone = eng.dilate_intervals(footprint, before_km, after_km, scope)
            else:
                zone = eng.dilate_intervals(footprint, after_km, before_km, scope)
            for iv in eng.subtract_intervals(zone, footprint):
                influence.append(LabelledInterval(iv.start_km, iv.end_km,
                                                  rule.rule_id, rule.name))

        nodata.extend(eng.clip_intervals(acq.nodata, scope))

    result = eng.evaluate(scope, [params.method], hits,
                          min_range_km=params.sliver_tol_km)
    return result, influence, eng.normalize(nodata), warnings


# ---------------------------------------------------------------------------
# Event merge + emission (§12.6-7)
# ---------------------------------------------------------------------------

_MATCH_TOL_KM = 0.0005  # 0.5 m — an existing event "already at" a boundary


def _inside(kp: float, intervals: List[Interval]) -> bool:
    return any(iv.start_km + 1e-9 < kp < iv.end_km - 1e-9 for iv in intervals)


class _InsideIndex:
    """Bisect-backed 'strictly inside any interval' test.

    Same semantics as ``_inside`` (strict interior with the 1e-9 guard)
    without the per-query linear scan — event merges against noisy exclusion
    stacks were O(events × intervals).
    """

    def __init__(self, intervals: List[Interval]):
        ordered = sorted(intervals, key=lambda iv: (iv.start_km, iv.end_km))
        self._starts = [iv.start_km for iv in ordered]
        self._max_end: List[float] = []
        running = float("-inf")
        for iv in ordered:
            running = max(running, iv.end_km)
            self._max_end.append(running)

    def __call__(self, kp: float) -> bool:
        # inside ⇔ some interval has start + 1e-9 < kp and end - 1e-9 > kp.
        index = bisect.bisect_left(self._starts, kp - 1e-9)
        return index > 0 and self._max_end[index - 1] - 1e-9 > kp


def merge_events(existing: List[Dict], generated: List[Dict],
                 excluded: List[Interval], direction: int, method: str = ""
                 ) -> Tuple[List[Dict], List[Dict], List[str]]:
    """Regeneration contract: nothing user-made is silently deleted or moved.

    - previous auto *candidate* events are disposable;
    - locked and confirmed events persist; manual/import/client events persist;
    - any kept event now inside an excluded interval -> status=conflict;
    - a kept event already sitting at a generated boundary supersedes the
      generated candidate (no duplicates).
    Returns (merged_events, conflicts, warnings).
    """
    kept: List[Dict] = []
    for event in existing:
        disposable = (event.get("source") == schema.EVENT_SOURCE_AUTO
                      and event.get("status") == schema.EVENT_STATUS_CANDIDATE
                      and not int(event.get("locked") or 0))
        if not disposable:
            kept.append(dict(event))

    conflicts: List[Dict] = []
    warnings: List[str] = []
    inside_excluded = _InsideIndex(excluded)
    for event in kept:
        try:
            kp = float(event.get("kp"))
        except (TypeError, ValueError):
            continue
        if inside_excluded(kp):
            if event.get("status") != schema.EVENT_STATUS_CONFLICT:
                event["status"] = schema.EVENT_STATUS_CONFLICT
            conflicts.append(event)
            warnings.append(
                f"{ev.event_label(event.get('event_type') or '', method)} at KP "
                f"{schema.format_kp(kp)} now lies inside an Exclusion Area — "
                "flagged as conflict.")
        elif event.get("status") == schema.EVENT_STATUS_CONFLICT:
            # No longer inside an exclusion; needs the user's eye again.
            event["status"] = schema.EVENT_STATUS_CANDIDATE
            warnings.append(
                f"{ev.event_label(event.get('event_type') or '', method)} at KP "
                f"{schema.format_kp(kp)} is no longer inside an Exclusion Area — "
                "reset to candidate.")

    merged = list(kept)
    kept_kps: Dict[str, List[float]] = {}
    for event in kept:
        # An event with a NULL/unparsable KP has no position: it must not
        # register at 0.0 and suppress a generated boundary near KP 0.
        try:
            kept_kp = float(event.get("kp"))
        except (TypeError, ValueError):
            continue
        kept_kps.setdefault(str(event.get("event_type") or ""),
                            []).append(kept_kp)
    for kps in kept_kps.values():
        kps.sort()
    for gen in generated:
        kps = kept_kps.get(str(gen.get("event_type") or ""))
        duplicate = False
        if kps:
            kp = float(gen.get("kp") or 0.0)
            index = bisect.bisect_left(kps, kp)
            duplicate = ((index < len(kps)
                          and kps[index] - kp <= _MATCH_TOL_KM)
                         or (index > 0
                             and kp - kps[index - 1] <= _MATCH_TOL_KM))
        if not duplicate:
            merged.append(gen)
    return ev.sort_events(merged, direction), conflicts, warnings


# ---------------------------------------------------------------------------
# Sections (§12.8)
# ---------------------------------------------------------------------------


class _VerdictWindow:
    """Bisect window over disjoint, KP-sorted verdicts.

    Resolution verdicts partition the scope, so per-section overlap lookups
    can bisect instead of scanning every verdict (sections × verdicts was
    the residual main-thread quadratic after resolution).
    """

    def __init__(self, verdicts: List[eng.RangeVerdict]):
        self.ordered = sorted(verdicts, key=lambda v: (v.start_km, v.end_km))
        self._starts = [v.start_km for v in self.ordered]
        self._ends = [v.end_km for v in self.ordered]

    def overlapping(self, iv: Interval):
        """(verdict, overlap_lo, overlap_hi) with overlap > 1e-9 km."""
        index = bisect.bisect_right(self._ends, iv.start_km + 1e-9)
        count = len(self.ordered)
        while index < count and self._starts[index] < iv.end_km - 1e-9:
            verdict = self.ordered[index]
            lo = max(verdict.start_km, iv.start_km)
            hi = min(verdict.end_km, iv.end_km)
            if hi - lo > 1e-9:
                yield verdict, lo, hi
            index += 1


def _reason_for_range(iv: Interval, excluded_verdicts: List[eng.RangeVerdict],
                      rule_names: Dict[str, str],
                      window: Optional[_VerdictWindow] = None) -> Dict:
    dominant: Dict[str, float] = {}
    fired: Dict[str, float] = {}
    window = window or _VerdictWindow(excluded_verdicts)
    for verdict, lo, hi in window.overlapping(iv):
        length = hi - lo
        if verdict.dominant_rule_id:
            dominant[verdict.dominant_rule_id] = dominant.get(verdict.dominant_rule_id, 0.0) + length
        for rid in verdict.fired_rule_ids:
            fired[rid] = fired.get(rid, 0.0) + length
    dominant_id = max(dominant, key=dominant.get) if dominant else ""
    return {
        "dominant_rule_id": dominant_id,
        "dominant_rule": rule_names.get(dominant_id, ""),
        "fired_rule_ids": sorted(fired, key=fired.get, reverse=True),
        "fired_rules": [rule_names.get(rid, rid)
                        for rid in sorted(fired, key=fired.get, reverse=True)],
    }


def build_sections(merged_events: List[Dict], params: GenParams,
                   excluded_verdicts: List[eng.RangeVerdict],
                   screening_verdicts: List[eng.RangeVerdict],
                   influence: List[LabelledInterval],
                   insufficient: List[Interval],
                   dropped_short: List[Interval],
                   rule_names: Dict[str, str],
                   previous_sections: Optional[List[Dict]] = None,
                   id_fn: Callable[[], str] = schema.new_id,
                   plan_id: str = "") -> List[Dict]:
    """Rebuild the section partition (burial | skip | insufficient_info).

    Screening annotations and Constraint Influence Zone flags land in
    ``reason_json``; conclusions/state/confidence/notes carry over from
    ``previous_sections`` for sections whose kind and boundaries are unchanged
    (regeneration must not wipe the user's assessment of untouched sections).
    """
    scope = params.scope
    sections: List[Dict] = []
    excluded_window = _VerdictWindow(excluded_verdicts)
    screening_window = _VerdictWindow(screening_verdicts)

    prev_by_range: Dict[Tuple[str, float, float], Dict] = {}
    for section in previous_sections or []:
        try:
            key = (section.get("kind") or "",
                   round(float(section.get("start_kp")), 4),
                   round(float(section.get("end_kp")), 4))
        except (TypeError, ValueError):
            continue
        prev_by_range[key] = section

    def carry_over(row: Dict) -> Dict:
        key = (row["kind"], round(row["start_kp"], 4), round(row["end_kp"], 4))
        prev = prev_by_range.get(key)
        if prev:
            row["section_id"] = prev.get("section_id") or row["section_id"]
            for col in ("state", "conclusion", "confidence", "notes",
                        "method", "tool_id", "tool_config_id",
                        "grade_in_m", "grade_out_m",
                        "target_burial_m", "skip_handling"):
                if prev.get(col) not in (None, ""):
                    row[col] = prev.get(col)
        return row

    def base_row(kind: str, start: float, end: float) -> Dict:
        return {
            "section_id": id_fn(),
            "plan_id": plan_id,
            "kind": kind,
            "start_kp": min(start, end),
            "end_kp": max(start, end),
            "length_km": abs(end - start),
            "start_event_id": "",
            "end_event_id": "",
            "state": schema.SECTION_STATE_CANDIDATE,
            "conclusion": "",
            "confidence": "",
            "reason_json": "{}",
            "method": "",
            "tool_id": "",
            "tool_config_id": "",
            "grade_in_m": None,
            "grade_out_m": None,
            "target_burial_m": None,
            "skip_handling": "",
            "notes": "",
        }

    # Burial sections from START/END pairs in travel order.
    burial_ranges: List[Interval] = []
    for start_event, end_event in ev.burial_pairs(merged_events, params.direction):
        start_kp = float(start_event.get("kp") or 0.0)
        if end_event is None:
            end_kp = (scope.end_km if int(params.direction) >= 0 else scope.start_km)
        else:
            end_kp = float(end_event.get("kp") or 0.0)
        row = base_row(schema.SECTION_BURIAL, start_kp, end_kp)
        row["start_event_id"] = start_event.get("event_id") or ""
        row["end_event_id"] = (end_event or {}).get("event_id") or ""
        iv = Interval(row["start_kp"], row["end_kp"])
        burial_ranges.append(iv)

        reason: Dict = {}
        if end_event is None:
            reason["dangling_start"] = True
        exclusion_conflicts = []
        for verdict, lo, hi in excluded_window.overlapping(iv):
            names = [rule_names.get(rule_id, rule_id)
                     for rule_id in verdict.fired_rule_ids]
            exclusion_conflicts.append({
                "rules": [name for name in names if name],
                "start_kp": round(lo, 6),
                "end_kp": round(hi, 6),
            })
        if exclusion_conflicts:
            # This normally appears only after a deliberate manual event edit
            # merges burial candidates across an excluded range.
            reason["exclusion_conflicts"] = exclusion_conflicts
        annotations = []
        seen_annotations = set()
        for verdict, lo, hi in screening_window.overlapping(iv):
            for rid in verdict.fired_rule_ids:
                entry_key = (rid, round(lo, 6), round(hi, 6))
                if entry_key in seen_annotations:
                    continue
                seen_annotations.add(entry_key)
                annotations.append({"rule_id": rid,
                                    "rule": rule_names.get(rid, rid),
                                    "start_kp": entry_key[1],
                                    "end_kp": entry_key[2]})
        if annotations:
            reason["screening"] = annotations

        # An entry/exit inside a Constraint Influence Zone is a visible
        # engineering prompt on the section, never silent geometry (§12.4).
        flags = []
        for zone in influence:
            for boundary, kp in (("start", iv.start_km), ("end", iv.end_km)):
                if zone.interval.contains(kp, 1e-9) and zone.interval.length_km > 0:
                    flags.append({
                        "boundary": boundary, "rule_id": zone.rule_id,
                        "rule": zone.rule_name,
                        "message": f"{'Entry' if boundary == 'start' else 'Exit'} within "
                                   f"Constraint Influence Zone of {zone.rule_name}",
                    })
        if flags:
            reason["influence_flags"] = flags
        row["reason_json"] = json.dumps(reason)
        sections.append(carry_over(row))

    # Remaining scope: insufficient-information first, then skips.
    remaining = eng.subtract_intervals([scope], burial_ranges)
    insufficient_ranges = eng.intersect_intervals(remaining, insufficient)
    skip_ranges = eng.subtract_intervals(remaining, insufficient_ranges)

    for iv in insufficient_ranges:
        row = base_row(schema.SECTION_INSUFFICIENT, iv.start_km, iv.end_km)
        row["conclusion"] = schema.CONCLUSION_INSUFFICIENT
        row["confidence"] = "insufficient"
        row["reason_json"] = json.dumps({"insufficient_information": True})
        sections.append(carry_over(row))

    for iv in skip_ranges:
        row = base_row(schema.SECTION_SKIP, iv.start_km, iv.end_km)
        reason = _reason_for_range(iv, excluded_verdicts, rule_names,
                                   window=excluded_window)
        below_min = any(
            eng.interval_length_km(eng.intersect_intervals([d], [iv])) > 1e-9
            for d in dropped_short
        )
        if below_min:
            reason["below_min_length"] = True
        if not reason.get("fired_rule_ids") and not below_min:
            reason["manual"] = True
        row["reason_json"] = json.dumps(reason)
        sections.append(carry_over(row))

    sections.sort(key=lambda s: (float(s.get("start_kp") or 0.0),
                                 float(s.get("end_kp") or 0.0)))
    return sections


def fresh_existing_events(events: Sequence[Dict], keep_client: bool = True
                          ) -> List[Dict]:
    """The events that survive a *fresh* regeneration.

    A fresh run rebuilds the plan purely from the Exclusion stack: manual and
    imported events, confirmations and locks are all discarded (the normal
    Generate keeps them). Client burial-proposal events are external
    reference data rather than plan decisions, so they are kept unless
    ``keep_client`` is False. Callers snapshot the prior state in the change
    log, so a fresh run remains fully rollback-able.
    """
    return [dict(event) for event in events
            if keep_client
            and event.get("source") == schema.EVENT_SOURCE_CLIENT]


def assign_skip_handling(sections: Sequence[Dict], transit_max_km: float,
                         overwrite: bool = False) -> Tuple[List[Dict], int]:
    """Assign skip handling to skips by length (user-entered policy).

    Skips no longer than ``transit_max_km`` become mid-water transits;
    longer skips become recover-to-deck. Only TBC skips are touched unless
    ``overwrite`` — a handling the engineer already chose is never silently
    replaced. Burial / insufficient-information sections pass through
    unchanged. Returns ``(updated_sections, changed_count)``.
    """
    threshold = max(0.0, float(transit_max_km or 0.0))
    updated: List[Dict] = []
    changed = 0
    for section in sections:
        row = dict(section)
        if row.get("kind") == schema.SECTION_SKIP:
            current = row.get("skip_handling") or ""
            if overwrite or not current:
                value = (schema.SKIP_HANDLING_MIDWATER
                         if float(row.get("length_km") or 0.0) <= threshold + 1e-9
                         else schema.SKIP_HANDLING_RECOVER)
                if value != current:
                    row["skip_handling"] = value
                    changed += 1
        updated.append(row)
    return updated, changed


# ---------------------------------------------------------------------------
# Client-proposal diff (§5, §12)
# ---------------------------------------------------------------------------


def diff_events(reference: List[Dict], produced: List[Dict],
                tol_km: float = 0.010) -> Dict:
    """Boundary diff vs an imported proposal: added / removed / moved."""
    def keyed(items: List[Dict]) -> List[Tuple[str, float]]:
        out = []
        for event in items:
            try:
                out.append((event.get("event_type") or "", float(event.get("kp"))))
            except (TypeError, ValueError):
                continue
        return out

    ref = keyed(reference)
    new = keyed(produced)
    moved, added = [], []
    unmatched_ref = list(ref)
    for etype, kp in new:
        best = None
        best_d = None
        for cand in unmatched_ref:
            if cand[0] != etype:
                continue
            d = abs(cand[1] - kp)
            if best_d is None or d < best_d:
                best, best_d = cand, d
        if best is not None and best_d is not None and best_d <= 1.0:
            unmatched_ref.remove(best)
            if best_d > tol_km:
                moved.append({"event_type": etype,
                              "from_kp": round(best[1], 6), "to_kp": round(kp, 6),
                              "shift_km": round(kp - best[1], 6)})
        else:
            added.append({"event_type": etype, "kp": round(kp, 6)})
    removed = [{"event_type": t, "kp": round(k, 6)} for t, k in unmatched_ref]
    return {"added": added, "removed": removed, "moved": moved}


# ---------------------------------------------------------------------------
# Full generation (§12)
# ---------------------------------------------------------------------------


def generate(params: GenParams, acquisitions: Sequence[RuleAcquisition],
             existing_events: Optional[List[Dict]] = None,
             predicates: Optional[Dict[str, Callable[[float], bool]]] = None,
             position_fn: Optional[Callable[[float], Tuple[Optional[float], Optional[float]]]] = None,
             depth_fn: Optional[Callable[[float], Optional[float]]] = None,
             previous_sections: Optional[List[Dict]] = None,
             proposal_events: Optional[List[Dict]] = None,
             plan_id: str = "", generation_id: str = "",
             id_fn: Callable[[], str] = schema.new_id,
             depth_at: Optional[Callable[[float], Optional[float]]] = None,
             resolution: Optional[Tuple] = None
             ) -> GenerationOutput:
    # ``depth_at`` feeds WD-scaled Exclusion Area extensions at resolution
    # time; it falls back to ``depth_fn`` (the event-stamping depth source).
    """Run the §12 pipeline over already-acquired rule intervals.

    ``predicates`` (per rule_id, True inside the footprint) enable 0.1 m
    boundary refinement; rules without a predicate keep their acquired
    boundaries. ``position_fn(kp) -> (lat, lon)`` and ``depth_fn(kp)`` stamp
    the emitted events; omitted (e.g. in pure tests) they stamp None.

    ``resolution`` accepts a precomputed ``resolve_stack`` result for
    exactly these acquisitions (only honoured when no refinement predicates
    are supplied) — the dock resolves once for the fire bars and reuses it
    here instead of paying the full resolution twice per Generate.
    """
    out = GenerationOutput()
    scope = params.scope
    tol_km = max(params.refine_tol_m, 0.001) / 1000.0
    coarse_step_km = max(params.coarse_step_m, 1.0) / 1000.0

    # 2. Refine footprint boundaries before resolution.
    refined: List[RuleAcquisition] = []
    for acq in acquisitions:
        footprint = acq.footprint
        predicate = (predicates or {}).get(str(acq.rule_row.get("rule_id")))
        if predicate is not None and footprint:
            footprint = refine_intervals(footprint, predicate, coarse_step_km,
                                         scope, tol_km)
        refined.append(RuleAcquisition(acq.rule_row, footprint, acq.nodata, acq.error))

    # 3. Resolve the stack (or reuse the caller's identical resolution).
    if resolution is not None and not predicates:
        result, influence, nodata, warnings = resolution
        warnings = list(warnings)
    else:
        result, influence, nodata, warnings = resolve_stack(
            params, refined, depth_at=depth_at or depth_fn)
    out.warnings.extend(warnings)
    verdicts = result.per_method.get(params.method, [])
    out.excluded = [v for v in verdicts if v.status == eng.STATUS_EXCLUDED]
    out.screening = [v for v in verdicts if v.status == eng.STATUS_RISK]
    out.influence = influence
    out.rule_hits = {str(rule_id): list(intervals)
                     for rule_id, intervals in result.rule_hits.items()}
    excluded_ranges = [Interval(v.start_km, v.end_km) for v in out.excluded]
    out.insufficient = eng.subtract_intervals(nodata, excluded_ranges)

    # 4-5. Candidates = scope - excluded - insufficient; drop the short ones.
    available = eng.subtract_intervals([scope], excluded_ranges)
    candidates = eng.subtract_intervals(available, out.insufficient)
    kept: List[Interval] = []
    for iv in candidates:
        if params.min_section_km > 0 and iv.length_km < params.min_section_km - 1e-9:
            out.dropped_short.append(iv)
        else:
            kept.append(iv)
    out.candidates = kept

    # 6. Emit candidate boundary events, ordered per direction.
    generated: List[Dict] = []
    for iv in kept:
        if int(params.direction) >= 0:
            boundary_kps = [(schema.EVENT_BURIAL_START, iv.start_km),
                            (schema.EVENT_BURIAL_END, iv.end_km)]
        else:
            boundary_kps = [(schema.EVENT_BURIAL_START, iv.end_km),
                            (schema.EVENT_BURIAL_END, iv.start_km)]
        for event_type, kp in boundary_kps:
            lat, lon = position_fn(kp) if position_fn else (None, None)
            depth = depth_fn(kp) if depth_fn else None
            generated.append({
                "event_id": id_fn(),
                "plan_id": plan_id,
                "generation_id": generation_id,
                "seq": 0,
                "event_type": event_type,
                "kp": kp,
                "end_kp": None,
                "lat": lat,
                "lon": lon,
                "depth_m": depth,
                "source": schema.EVENT_SOURCE_AUTO,
                "status": schema.EVENT_STATUS_CANDIDATE,
                "locked": 0,
                "notes": "",
            })

    # 7. Merge with existing events.
    merged, conflicts, merge_warnings = merge_events(
        existing_events or [], generated, excluded_ranges,
        params.direction, params.method)
    out.events = merged
    out.conflicts = conflicts
    out.warnings.extend(merge_warnings)

    # 8. Rebuild sections.
    rule_names = {str(a.rule_row.get("rule_id")): (a.rule_row.get("name") or "")
                  for a in acquisitions}
    out.sections = build_sections(
        merged, params, out.excluded, out.screening, influence,
        out.insufficient, out.dropped_short, rule_names,
        previous_sections=previous_sections, id_fn=id_fn, plan_id=plan_id)

    # Proposal diff (client burial proposal as review starting point).
    if proposal_events:
        out.proposal_diff = diff_events(proposal_events, merged)

    out.summary = summarise(out, params)
    return out


# ---------------------------------------------------------------------------
# Resolution-context (de)serialisation
#
# The interval classes behind the current sections are persisted with the
# generation row so reasons/flags can be rebuilt after the plan is reopened
# (event edits rebuild sections without re-running acquisition).
# ---------------------------------------------------------------------------


def context_to_dict(out: GenerationOutput) -> Dict:
    def verdicts(items: List[eng.RangeVerdict]) -> List[Dict]:
        return [{
            "start_km": v.start_km, "end_km": v.end_km, "status": v.status,
            "risk_level": v.risk_level, "fired_rule_ids": list(v.fired_rule_ids),
            "dominant_rule_id": v.dominant_rule_id or "",
        } for v in items]

    return {
        "excluded": verdicts(out.excluded),
        "screening": verdicts(out.screening),
        "influence": [{"start_km": z.start_km, "end_km": z.end_km,
                       "rule_id": z.rule_id, "rule_name": z.rule_name}
                      for z in out.influence],
        "insufficient": [[iv.start_km, iv.end_km] for iv in out.insufficient],
        "dropped_short": [[iv.start_km, iv.end_km] for iv in out.dropped_short],
        "candidates": [[iv.start_km, iv.end_km] for iv in out.candidates],
        "rule_hits": {str(rule_id): [[iv.start_km, iv.end_km]
                                     for iv in intervals]
                      for rule_id, intervals in (out.rule_hits or {}).items()},
    }


@dataclass
class ResolutionContext:
    excluded: List[eng.RangeVerdict] = field(default_factory=list)
    screening: List[eng.RangeVerdict] = field(default_factory=list)
    influence: List[LabelledInterval] = field(default_factory=list)
    insufficient: List[Interval] = field(default_factory=list)
    dropped_short: List[Interval] = field(default_factory=list)
    candidates: List[Interval] = field(default_factory=list)
    rule_hits: Dict[str, List[Interval]] = field(default_factory=dict)


def context_from_dict(data: Optional[Dict]) -> ResolutionContext:
    ctx = ResolutionContext()
    data = data or {}

    def verdicts(items) -> List[eng.RangeVerdict]:
        out = []
        for d in items or []:
            try:
                out.append(eng.RangeVerdict(
                    float(d["start_km"]), float(d["end_km"]),
                    d.get("status") or "", int(d.get("risk_level") or 0),
                    list(d.get("fired_rule_ids") or []),
                    d.get("dominant_rule_id") or None))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    ctx.excluded = verdicts(data.get("excluded"))
    ctx.screening = verdicts(data.get("screening"))
    for d in data.get("influence") or []:
        try:
            ctx.influence.append(LabelledInterval(
                float(d["start_km"]), float(d["end_km"]),
                d.get("rule_id") or "", d.get("rule_name") or ""))
        except (KeyError, TypeError, ValueError):
            continue
    for key, target in (("insufficient", ctx.insufficient),
                        ("dropped_short", ctx.dropped_short),
                        ("candidates", ctx.candidates)):
        for pair in data.get(key) or []:
            try:
                target.append(Interval(float(pair[0]), float(pair[1])))
            except (IndexError, TypeError, ValueError):
                continue
    hits = data.get("rule_hits")
    if isinstance(hits, dict):
        for rule_id, pairs in hits.items():
            intervals: List[Interval] = []
            for pair in pairs or []:
                try:
                    intervals.append(Interval(float(pair[0]), float(pair[1])))
                except (IndexError, TypeError, ValueError):
                    continue
            ctx.rule_hits[str(rule_id)] = intervals
    return ctx


def summarise(out: GenerationOutput, params: GenParams) -> Dict:
    scope_km = params.scope.length_km
    burial_km = sum(s["length_km"] for s in out.sections
                    if s["kind"] == schema.SECTION_BURIAL)
    skip_km = sum(s["length_km"] for s in out.sections
                  if s["kind"] == schema.SECTION_SKIP)
    insufficient_km = sum(s["length_km"] for s in out.sections
                          if s["kind"] == schema.SECTION_INSUFFICIENT)
    return {
        "scope_km": round(scope_km, 6),
        "burial_km": round(burial_km, 6),
        "burial_pct": round(100.0 * burial_km / scope_km, 2) if scope_km > 0 else 0.0,
        "skip_km": round(skip_km, 6),
        "insufficient_km": round(insufficient_km, 6),
        "section_count": len(out.sections),
        "event_count": len(out.events),
        "conflict_count": len(out.conflicts),
        "dropped_below_min": len(out.dropped_short),
        "warning_count": len(out.warnings),
    }
