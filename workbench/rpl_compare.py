# -*- coding: utf-8 -*-
"""Compare two RPL revisions of the same cable segment.

Two revisions of a route are two documents, not two versions of one table:
positions get inserted, dropped, renumbered and renamed between issues, so
``PosNo`` is worthless as an identity and a row-by-row diff reports the whole
tail of the route as changed the moment one alter course is added. This
module instead *maps* the positions of one revision onto the other and reports
what actually moved.

The mapping is anchored, then aligned:

1. **Anchors** — positions that can only be each other: the same coordinate
   (to ~1 m) where that coordinate is unique in both revisions, or the same
   event text where that text is unique in both ("BU-1", "JT-3"). Anchors are
   thinned to a longest increasing subsequence, so the mapping can never cross
   itself: a match always keeps route order.
2. **Alignment** — each gap between consecutive anchors is aligned with a
   Needleman-Wunsch pass over a cost that blends event text and separation on
   the ground, so an inserted or deleted position costs a gap rather than
   shifting everything after it. Wide gaps (a wholly re-routed stretch) fall
   back to a monotone greedy pass so the cost stays linear.
3. **Classification** — a matched pair is *unchanged* or carries the list of
   what differs (event, position, KP, depth, remarks); unmatched positions in
   the new revision are *added* and in the old one *removed*.

Legs follow the positions: a leg whose two endpoints both map to adjacent
positions in the other revision is the same leg and its attributes are
compared; anything else is reported as removed/added.

Pure Python — no QGIS imports — so it runs headless and is unit-tested in
``tests/test_rpl_compare.py``. Distances default to a haversine on the WGS84
mean radius, which is well inside the tolerance of "did this position move".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Cable-type length sums (shared with rpl_summary, which re-exports them)
# ---------------------------------------------------------------------------
# (cable type, route km, cable km) in first-appearance order. Legs whose
# CableType is blank accumulate under "" so totals still add up.
CableTypeLength = Tuple[str, Optional[float], Optional[float]]


def complete_sum(values) -> Optional[float]:
    """Sum, or None when any value is missing (never a misleading partial)."""
    return (sum(float(value) for value in values)
            if values and all(value is not None for value in values) else None)


def cable_type_lengths(legs: Iterable[Dict]) -> Tuple[CableTypeLength, ...]:
    """Sum route/cable km per cable type over leg rows, first-appearance order.

    A type whose legs miss a length value reports ``None`` for that measure
    rather than a misleading partial sum (same rule as the RPL totals).
    """
    order: List[str] = []
    route: Dict[str, List] = {}
    cable: Dict[str, List] = {}
    for leg in legs:
        name = str(leg.get("cable_type") or "").strip()
        if name not in route:
            order.append(name)
            route[name], cable[name] = [], []
        route[name].append(leg.get("route_km"))
        cable[name].append(leg.get("cable_km"))
    return tuple((name, complete_sum(route[name]), complete_sum(cable[name]))
                 for name in order)


def sum_cable_type_lengths(groups: Iterable[Sequence[CableTypeLength]]
                           ) -> Tuple[CableTypeLength, ...]:
    """Merge per-type sums from several RPLs/sections (a system total)."""
    order: List[str] = []
    route: Dict[str, List] = {}
    cable: Dict[str, List] = {}
    for group in groups:
        for name, route_km, cable_km in group or ():
            if name not in route:
                order.append(name)
                route[name], cable[name] = [], []
            route[name].append(route_km)
            cable[name].append(cable_km)
    return tuple((name, complete_sum(route[name]), complete_sum(cable[name]))
                 for name in order)


def format_cable_type_lengths(type_lengths: Sequence[CableTypeLength],
                              measure: str = "route", separator: str = " · ",
                              limit: int = 0) -> str:
    """``"LW 12.300 km · DA 4.100 km"`` — blank types read as "Cable type not set"."""
    bits = []
    for name, route_km, cable_km in type_lengths or ():
        value = route_km if measure == "route" else cable_km
        label = name or "Cable type not set"
        bits.append(f"{label} {value:.3f} km" if value is not None else label)
    if limit and len(bits) > limit:
        bits = bits[:limit] + [f"+{len(bits) - limit} more"]
    return separator.join(bits)


# ---------------------------------------------------------------------------
# Tolerances
# ---------------------------------------------------------------------------
# A position is "the same place" within this distance; anything further is a
# move worth reporting. RPL coordinates are quoted to 0.001', ~2 m, so 5 m
# absorbs rounding without hiding a real nudge.
SAME_POSITION_M = 5.0
# How far apart two positions may be and still be considered the same position
# that moved. Beyond this the alignment prefers to call them added + removed.
MATCH_TOLERANCE_M = 500.0
SAME_KP_KM = 0.0005          # 0.5 m
SAME_DEPTH_M = 0.5
SAME_LENGTH_KM = 0.0005
SAME_SLACK_PCT = 0.001

# Alignment cost model. A pair is only ever matched below MATCH_THRESHOLD, and
# GAP_COST is half of it so the dynamic program prefers a gap over a bad pair.
MATCH_THRESHOLD = 0.6
GAP_COST = 0.3
# Above this many cells the gap is aligned greedily instead of by DP; 250k
# cells is a fraction of a second, and only a fully re-routed stretch with no
# shared event text can reach it.
MAX_DP_CELLS = 250_000

COORD_DECIMALS = 5           # ~1 m at the equator

STATUS_UNCHANGED = "unchanged"
STATUS_CHANGED = "changed"
STATUS_ADDED = "added"
STATUS_REMOVED = "removed"

ANCHOR_COORDINATE = "coordinate"
ANCHOR_EVENT = "event"
MATCH_ALIGNED = "aligned"

CHANGE_EVENT = "event"
CHANGE_POSITION = "position"
CHANGE_KP = "kp"
CHANGE_CABLE_KP = "cable_kp"
CHANGE_DEPTH = "depth"
CHANGE_REMARKS = "remarks"

CHANGE_LABELS = {
    CHANGE_EVENT: "event text",
    CHANGE_POSITION: "position",
    CHANGE_KP: "KP",
    CHANGE_CABLE_KP: "cable distance",
    CHANGE_DEPTH: "depth",
    CHANGE_REMARKS: "remarks",
}

LEG_CHANGE_LABELS = {
    "cable_type": "cable type",
    "cable_code": "cable code",
    "route_km": "route length",
    "cable_km": "cable length",
    "slack": "slack",
    "protection": "protection",
    "target_burial_m": "target burial",
}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RevisionStats:
    """Headline numbers for one revision, all derived from its two layers."""

    label: str = ""
    kind: str = ""
    status: str = ""
    position_count: int = 0
    leg_count: int = 0
    event_count: int = 0
    section_count: int = 0
    alter_course_count: int = 0
    body_count: int = 0
    body_counts: Tuple[Tuple[str, int], ...] = ()
    geographic_counts: Tuple[Tuple[str, int], ...] = ()
    unclassified_event_count: int = 0
    start_kp_km: Optional[float] = None
    end_kp_km: Optional[float] = None
    route_length_km: Optional[float] = None
    cable_length_km: Optional[float] = None
    slack_pct: Optional[float] = None
    start_event: str = ""
    end_event: str = ""
    cable_type_lengths: Tuple[CableTypeLength, ...] = ()


def revision_stats(points: Sequence[Dict], legs: Sequence[Dict],
                   label: str = "", kind: str = "", status: str = "",
                   classify: Optional[Callable] = None) -> RevisionStats:
    """Summarise one revision from its point and leg rows.

    ``classify`` is an :class:`assembly_model.EventClassifier` ``classify``
    callable; the project's own event rules are used when the caller passes
    them, so "AC" counts what this project calls an alter course.
    """
    if classify is None:
        from .assembly_model import EventClassifier

        classify = EventClassifier.with_defaults().classify

    events = [str(row.get("event") or "").strip() for row in points]
    event_count = sum(1 for text in events if text)
    body_counts: Dict[str, int] = {}
    geo_counts: Dict[str, int] = {}
    bodies = 0
    unclassified = 0
    for text in events:
        if not text:
            continue
        result = classify(text)
        if not getattr(result, "matched", False):
            unclassified += 1
        if getattr(result, "is_assembly", False):
            bodies += 1
            key = getattr(result, "body_type", "") or "other"
            body_counts[key] = body_counts.get(key, 0) + 1
        if getattr(result, "is_geographic", False):
            key = getattr(result, "geo_type", "") or "other"
            geo_counts[key] = geo_counts.get(key, 0) + 1

    route_length = complete_sum([leg.get("route_km") for leg in legs])
    cable_length = complete_sum([leg.get("cable_km") for leg in legs])
    start_kp = points[0].get("kp") if points else None
    end_kp = points[-1].get("kp") if points else None
    if route_length is None:
        route_length = _difference(start_kp, end_kp)
    if cable_length is None:
        cable_length = _difference(
            points[0].get("cable_kp") if points else None,
            points[-1].get("cable_kp") if points else None)
    slack = None
    if route_length and cable_length is not None:
        slack = (cable_length / route_length - 1.0) * 100.0

    # Same rule as rpl_summary: a section runs event to event, with the two
    # ends of the revision always section boundaries.
    intermediate_events = sum(1 for text in events[1:-1] if text)
    section_count = intermediate_events + (1 if len(points) >= 2 else 0)

    return RevisionStats(
        label=label, kind=kind, status=status,
        position_count=len(points), leg_count=len(legs),
        event_count=event_count, section_count=section_count,
        alter_course_count=geo_counts.get("alter_course", 0),
        body_count=bodies,
        body_counts=tuple(sorted(body_counts.items())),
        geographic_counts=tuple(sorted(geo_counts.items())),
        unclassified_event_count=unclassified,
        start_kp_km=start_kp, end_kp_km=end_kp,
        route_length_km=route_length, cable_length_km=cable_length,
        slack_pct=slack,
        start_event=events[0] if events else "",
        end_event=events[-1] if events else "",
        cable_type_lengths=cable_type_lengths(legs),
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PositionMatch:
    """One row of the position diff: a pair, an addition or a deletion."""

    status: str
    a_index: Optional[int] = None
    b_index: Optional[int] = None
    a: Optional[Dict] = None
    b: Optional[Dict] = None
    how: str = ""
    distance_m: Optional[float] = None
    kp_delta_km: Optional[float] = None
    cable_kp_delta_km: Optional[float] = None
    depth_delta_m: Optional[float] = None
    changes: Tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.a_index is not None and self.b_index is not None


@dataclass(frozen=True)
class LegMatch:
    """One row of the leg diff, keyed off the matched endpoints."""

    status: str
    a_index: Optional[int] = None
    b_index: Optional[int] = None
    a: Optional[Dict] = None
    b: Optional[Dict] = None
    start_event: str = ""
    end_event: str = ""
    changes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RplComparison:
    a_label: str = ""
    b_label: str = ""
    stats_a: RevisionStats = field(default_factory=RevisionStats)
    stats_b: RevisionStats = field(default_factory=RevisionStats)
    positions: Tuple[PositionMatch, ...] = ()
    legs: Tuple[LegMatch, ...] = ()

    def position_counts(self) -> Dict[str, int]:
        return _counts(match.status for match in self.positions)

    def leg_counts(self) -> Dict[str, int]:
        return _counts(match.status for match in self.legs)

    def max_offset_m(self) -> Optional[float]:
        offsets = [m.distance_m for m in self.positions if m.distance_m is not None]
        return max(offsets) if offsets else None


def normalise_event(value) -> str:
    """Event text reduced to what identifies it: case, spacing and punctuation
    vary between issues of the same RPL ("BU 1", "BU-1", "bu1")."""
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def haversine_m(lat1, lon1, lat2, lon2) -> Optional[float]:
    """Great-circle distance in metres on the WGS84 mean radius."""
    try:
        lat1, lon1, lat2, lon2 = (float(lat1), float(lon1), float(lat2), float(lon2))
    except (TypeError, ValueError):
        return None
    radius = 6371008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2)
    return 2.0 * radius * math.asin(min(1.0, math.sqrt(a)))


def compare_revisions(points_a: Sequence[Dict], legs_a: Sequence[Dict],
                      points_b: Sequence[Dict], legs_b: Sequence[Dict],
                      a_label: str = "", b_label: str = "",
                      a_kind: str = "", b_kind: str = "",
                      a_status: str = "", b_status: str = "",
                      classify: Optional[Callable] = None,
                      distance_fn: Optional[Callable] = None,
                      tolerance_m: float = MATCH_TOLERANCE_M) -> RplComparison:
    """Full comparison of revision A (older) against revision B (newer)."""
    points_a = list(points_a or [])
    points_b = list(points_b or [])
    legs_a = list(legs_a or [])
    legs_b = list(legs_b or [])
    distance_fn = distance_fn or haversine_m

    pairs = match_positions(points_a, points_b, distance_fn=distance_fn,
                            tolerance_m=tolerance_m)
    positions = _position_rows(points_a, points_b, pairs, distance_fn)
    legs = _leg_rows(points_a, points_b, legs_a, legs_b, pairs)
    return RplComparison(
        a_label=a_label, b_label=b_label,
        stats_a=revision_stats(points_a, legs_a, a_label, a_kind, a_status, classify),
        stats_b=revision_stats(points_b, legs_b, b_label, b_kind, b_status, classify),
        positions=tuple(positions), legs=tuple(legs),
    )


def match_positions(points_a: Sequence[Dict], points_b: Sequence[Dict],
                    distance_fn: Optional[Callable] = None,
                    tolerance_m: float = MATCH_TOLERANCE_M
                    ) -> List[Tuple[int, int, str]]:
    """Map A's positions onto B's, in route order.

    Returns ``(index_a, index_b, how)`` triples sorted by ``index_a``, where
    ``how`` names what made the match (an anchor kind or the alignment).
    """
    distance_fn = distance_fn or haversine_m
    if not points_a or not points_b:
        return []

    anchors = _anchor_pairs(points_a, points_b)
    anchors = _longest_increasing(anchors)

    matches: List[Tuple[int, int, str]] = []
    previous_a, previous_b = -1, -1
    for index_a, index_b, how in anchors + [(len(points_a), len(points_b), "")]:
        gap_a = range(previous_a + 1, index_a)
        gap_b = range(previous_b + 1, index_b)
        for pair in _align_gap(points_a, points_b, list(gap_a), list(gap_b),
                               distance_fn, tolerance_m):
            matches.append(pair)
        if how:
            matches.append((index_a, index_b, how))
        previous_a, previous_b = index_a, index_b
    matches.sort(key=lambda item: item[0])
    return matches


# -- anchors -----------------------------------------------------------------
def _anchor_pairs(points_a: Sequence[Dict], points_b: Sequence[Dict]
                  ) -> List[Tuple[int, int, str]]:
    """Pairs that can only be each other: unique shared coordinate or event."""
    pairs: Dict[int, Tuple[int, int, str]] = {}
    for key_fn, how in ((_coordinate_key, ANCHOR_COORDINATE),
                        (_event_key, ANCHOR_EVENT)):
        index_a = _unique_index(points_a, key_fn)
        index_b = _unique_index(points_b, key_fn)
        taken_b = {pair[1] for pair in pairs.values()}
        for key, position_a in index_a.items():
            position_b = index_b.get(key)
            if position_b is None or position_a in pairs or position_b in taken_b:
                continue
            pairs[position_a] = (position_a, position_b, how)
            taken_b.add(position_b)
    return [pairs[key] for key in sorted(pairs)]


def _unique_index(points: Sequence[Dict], key_fn) -> Dict[object, int]:
    """key -> index, keeping only keys that occur exactly once."""
    seen: Dict[object, int] = {}
    duplicated = set()
    for index, row in enumerate(points):
        key = key_fn(row)
        if key is None:
            continue
        if key in seen:
            duplicated.add(key)
        else:
            seen[key] = index
    for key in duplicated:
        seen.pop(key, None)
    return seen


def _coordinate_key(row: Dict):
    lat, lon = row.get("lat"), row.get("lon")
    if lat is None or lon is None:
        return None
    try:
        return (round(float(lat), COORD_DECIMALS), round(float(lon), COORD_DECIMALS))
    except (TypeError, ValueError):
        return None


def _event_key(row: Dict):
    key = normalise_event(row.get("event"))
    return key or None


def _longest_increasing(pairs: Sequence[Tuple[int, int, str]]
                        ) -> List[Tuple[int, int, str]]:
    """Thin anchors to the longest strictly increasing run in B's order.

    Anchors are already increasing in A; dropping the ones that would need the
    mapping to cross itself is what keeps a match from claiming a position
    moved past its neighbours.
    """
    if not pairs:
        return []
    tails: List[int] = []          # tails[k] = B index ending a run of length k+1
    tail_index: List[int] = []     # index into pairs for each tail
    previous: List[Optional[int]] = [None] * len(pairs)
    for position, (_a, b, _how) in enumerate(pairs):
        low, high = 0, len(tails)
        while low < high:
            middle = (low + high) // 2
            if tails[middle] < b:
                low = middle + 1
            else:
                high = middle
        if low > 0:
            previous[position] = tail_index[low - 1]
        if low == len(tails):
            tails.append(b)
            tail_index.append(position)
        else:
            tails[low] = b
            tail_index[low] = position
    result: List[Tuple[int, int, str]] = []
    cursor: Optional[int] = tail_index[-1] if tail_index else None
    while cursor is not None:
        result.append(pairs[cursor])
        cursor = previous[cursor]
    result.reverse()
    return result


# -- gap alignment -----------------------------------------------------------
def _pair_cost(row_a: Dict, row_b: Dict, distance_fn, tolerance_m: float) -> float:
    """0 (certainly the same position) .. 1 (certainly not), or ``inf``."""
    key_a, key_b = normalise_event(row_a.get("event")), normalise_event(row_b.get("event"))
    if key_a and key_b:
        text_cost = 0.0 if key_a == key_b else 0.8
    elif not key_a and not key_b:
        text_cost = 0.35
    else:
        text_cost = 0.6
    distance = distance_fn(row_a.get("lat"), row_a.get("lon"),
                           row_b.get("lat"), row_b.get("lon"))
    if distance is None:
        spatial_cost = 0.5
    else:
        spatial_cost = min(1.0, distance / max(tolerance_m * 4.0, 1.0))
    cost = 0.5 * text_cost + 0.5 * spatial_cost
    return cost if cost <= MATCH_THRESHOLD else float("inf")


def _align_gap(points_a: Sequence[Dict], points_b: Sequence[Dict],
               gap_a: List[int], gap_b: List[int], distance_fn,
               tolerance_m: float) -> List[Tuple[int, int, str]]:
    """Align one anchor-free stretch of both revisions."""
    if not gap_a or not gap_b:
        return []
    if len(gap_a) * len(gap_b) > MAX_DP_CELLS:
        return _align_greedy(points_a, points_b, gap_a, gap_b, distance_fn, tolerance_m)

    rows, columns = len(gap_a), len(gap_b)
    # Needleman-Wunsch: cost[i][j] is the cheapest alignment of the first i
    # positions of the gap in A with the first j of B.
    cost = [[0.0] * (columns + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        cost[i][0] = cost[i - 1][0] + GAP_COST
    for j in range(1, columns + 1):
        cost[0][j] = cost[0][j - 1] + GAP_COST
    for i in range(1, rows + 1):
        row_a = points_a[gap_a[i - 1]]
        cost_row, previous_row = cost[i], cost[i - 1]
        for j in range(1, columns + 1):
            pair = previous_row[j - 1] + _pair_cost(
                row_a, points_b[gap_b[j - 1]], distance_fn, tolerance_m)
            skip_a = previous_row[j] + GAP_COST
            skip_b = cost_row[j - 1] + GAP_COST
            cost_row[j] = min(pair, skip_a, skip_b)

    matches: List[Tuple[int, int, str]] = []
    i, j = rows, columns
    while i > 0 and j > 0:
        pair = cost[i - 1][j - 1] + _pair_cost(
            points_a[gap_a[i - 1]], points_b[gap_b[j - 1]], distance_fn, tolerance_m)
        if abs(cost[i][j] - pair) < 1e-12:
            matches.append((gap_a[i - 1], gap_b[j - 1], MATCH_ALIGNED))
            i, j = i - 1, j - 1
        elif abs(cost[i][j] - (cost[i - 1][j] + GAP_COST)) < 1e-12:
            i -= 1
        else:
            j -= 1
    matches.reverse()
    return matches


def _align_greedy(points_a: Sequence[Dict], points_b: Sequence[Dict],
                  gap_a: List[int], gap_b: List[int], distance_fn,
                  tolerance_m: float) -> List[Tuple[int, int, str]]:
    """Monotone nearest-acceptable pass, for gaps too wide to align properly."""
    matches: List[Tuple[int, int, str]] = []
    cursor = 0
    # Look only a little way ahead: a monotone mapping cannot jump far, and
    # this is what keeps the fallback linear.
    window = 50
    for index_a in gap_a:
        best: Optional[Tuple[float, int]] = None
        for offset in range(cursor, min(len(gap_b), cursor + window)):
            cost = _pair_cost(points_a[index_a], points_b[gap_b[offset]],
                              distance_fn, tolerance_m)
            if cost == float("inf"):
                continue
            if best is None or cost < best[0]:
                best = (cost, offset)
        if best is None:
            continue
        matches.append((index_a, gap_b[best[1]], MATCH_ALIGNED))
        cursor = best[1] + 1
    return matches


# -- rows --------------------------------------------------------------------
def _position_rows(points_a: Sequence[Dict], points_b: Sequence[Dict],
                   pairs: Sequence[Tuple[int, int, str]], distance_fn
                   ) -> List[PositionMatch]:
    """Merge matched pairs with the unmatched positions, in route order."""
    by_a = {pair[0]: pair for pair in pairs}
    matched_b = {pair[1] for pair in pairs}
    rows: List[PositionMatch] = []
    cursor_b = 0
    for index_a in range(len(points_a)):
        pair = by_a.get(index_a)
        if pair is not None:
            # Everything in B before this partner is new.
            while cursor_b < pair[1]:
                if cursor_b not in matched_b:
                    rows.append(PositionMatch(status=STATUS_ADDED, b_index=cursor_b,
                                              b=points_b[cursor_b]))
                cursor_b += 1
            rows.append(_compare_positions(index_a, pair[1], points_a[index_a],
                                           points_b[pair[1]], pair[2], distance_fn))
            cursor_b = pair[1] + 1
        else:
            rows.append(PositionMatch(status=STATUS_REMOVED, a_index=index_a,
                                      a=points_a[index_a]))
    while cursor_b < len(points_b):
        if cursor_b not in matched_b:
            rows.append(PositionMatch(status=STATUS_ADDED, b_index=cursor_b,
                                      b=points_b[cursor_b]))
        cursor_b += 1
    return rows


def _compare_positions(index_a: int, index_b: int, row_a: Dict, row_b: Dict,
                       how: str, distance_fn) -> PositionMatch:
    distance = distance_fn(row_a.get("lat"), row_a.get("lon"),
                           row_b.get("lat"), row_b.get("lon"))
    kp_delta = _difference(row_a.get("kp"), row_b.get("kp"))
    cable_delta = _difference(row_a.get("cable_kp"), row_b.get("cable_kp"))
    depth_delta = _difference(row_a.get("depth"), row_b.get("depth"))

    changes: List[str] = []
    if normalise_event(row_a.get("event")) != normalise_event(row_b.get("event")):
        changes.append(CHANGE_EVENT)
    if distance is not None and distance > SAME_POSITION_M:
        changes.append(CHANGE_POSITION)
    if kp_delta is not None and abs(kp_delta) > SAME_KP_KM:
        changes.append(CHANGE_KP)
    if cable_delta is not None and abs(cable_delta) > SAME_KP_KM:
        changes.append(CHANGE_CABLE_KP)
    if depth_delta is not None and abs(depth_delta) > SAME_DEPTH_M:
        changes.append(CHANGE_DEPTH)
    if _text(row_a.get("remarks")) != _text(row_b.get("remarks")):
        changes.append(CHANGE_REMARKS)

    return PositionMatch(
        status=STATUS_CHANGED if changes else STATUS_UNCHANGED,
        a_index=index_a, b_index=index_b, a=row_a, b=row_b, how=how,
        distance_m=distance, kp_delta_km=kp_delta,
        cable_kp_delta_km=cable_delta, depth_delta_m=depth_delta,
        changes=tuple(changes),
    )


def _leg_rows(points_a: Sequence[Dict], points_b: Sequence[Dict],
              legs_a: Sequence[Dict], legs_b: Sequence[Dict],
              pairs: Sequence[Tuple[int, int, str]]) -> List[LegMatch]:
    """A leg is the same leg when both its endpoints map to adjacent positions."""
    mapping = {pair[0]: pair[1] for pair in pairs}
    used_b = set()
    rows: List[LegMatch] = []
    for index_a, leg_a in enumerate(legs_a):
        start_b = mapping.get(index_a)
        end_b = mapping.get(index_a + 1)
        if start_b is not None and end_b == start_b + 1 and start_b < len(legs_b):
            leg_b = legs_b[start_b]
            used_b.add(start_b)
            changes = _leg_changes(leg_a, leg_b)
            rows.append(LegMatch(
                status=STATUS_CHANGED if changes else STATUS_UNCHANGED,
                a_index=index_a, b_index=start_b, a=leg_a, b=leg_b,
                start_event=_event_of(points_a, index_a),
                end_event=_event_of(points_a, index_a + 1),
                changes=changes))
        else:
            rows.append(LegMatch(
                status=STATUS_REMOVED, a_index=index_a, a=leg_a,
                start_event=_event_of(points_a, index_a),
                end_event=_event_of(points_a, index_a + 1)))
    for index_b, leg_b in enumerate(legs_b):
        if index_b not in used_b:
            rows.append(LegMatch(
                status=STATUS_ADDED, b_index=index_b, b=leg_b,
                start_event=_event_of(points_b, index_b),
                end_event=_event_of(points_b, index_b + 1)))
    return rows


def _leg_changes(leg_a: Dict, leg_b: Dict) -> Tuple[str, ...]:
    changes: List[str] = []
    for key, tolerance in (("route_km", SAME_LENGTH_KM), ("cable_km", SAME_LENGTH_KM),
                           ("slack", SAME_SLACK_PCT)):
        delta = _difference(leg_a.get(key), leg_b.get(key))
        if delta is not None and abs(delta) > tolerance:
            changes.append(key)
        elif delta is None and (leg_a.get(key) is None) != (leg_b.get(key) is None):
            changes.append(key)
    for key in ("cable_type", "cable_code", "protection"):
        if _text(leg_a.get(key)).upper() != _text(leg_b.get(key)).upper():
            changes.append(key)
    delta = _difference(leg_a.get("target_burial_m"), leg_b.get("target_burial_m"))
    if delta is not None and abs(delta) > 0.01:
        changes.append("target_burial_m")
    return tuple(changes)


def _event_of(points: Sequence[Dict], index: int) -> str:
    if 0 <= index < len(points):
        return _text(points[index].get("event"))
    return ""


# ---------------------------------------------------------------------------
# Presentation helpers (pure, so the table and the CSV cannot drift apart)
# ---------------------------------------------------------------------------
def statistic_rows(comparison: RplComparison) -> List[Tuple[str, str, str, str]]:
    """``(measure, A, B, change)`` rows for the statistics table / CSV."""
    a, b = comparison.stats_a, comparison.stats_b
    rows: List[Tuple[str, str, str, str]] = [
        ("Kind", a.kind or "", b.kind or "", ""),
        ("Status", a.status or "", b.status or "", ""),
        _count_row("Positions", a.position_count, b.position_count),
        _count_row("Legs", a.leg_count, b.leg_count),
        _count_row("Events", a.event_count, b.event_count),
        _count_row("Alter courses (AC)", a.alter_course_count, b.alter_course_count),
        _count_row("Cable bodies", a.body_count, b.body_count),
    ]
    for key in sorted({name for name, _n in a.body_counts}
                      | {name for name, _n in b.body_counts}):
        rows.append(_count_row(f"  {key.replace('_', ' ')}",
                               dict(a.body_counts).get(key, 0),
                               dict(b.body_counts).get(key, 0)))
    for key in sorted({name for name, _n in a.geographic_counts}
                      | {name for name, _n in b.geographic_counts}):
        if key == "alter_course":
            continue
        rows.append(_count_row(f"  {key.replace('_', ' ')}",
                               dict(a.geographic_counts).get(key, 0),
                               dict(b.geographic_counts).get(key, 0)))
    rows.extend([
        _count_row("Unclassified events", a.unclassified_event_count,
                   b.unclassified_event_count),
        _count_row("RPL sections", a.section_count, b.section_count),
        _value_row("Start KP", a.start_kp_km, b.start_kp_km, "km", 3),
        _value_row("End KP", a.end_kp_km, b.end_kp_km, "km", 3),
        _value_row("Route length", a.route_length_km, b.route_length_km, "km", 3),
        _value_row("Cable length", a.cable_length_km, b.cable_length_km, "km", 3),
        _value_row("Slack", a.slack_pct, b.slack_pct, "%", 3),
    ])
    a_types = dict((name, (route, cable)) for name, route, cable in a.cable_type_lengths)
    b_types = dict((name, (route, cable)) for name, route, cable in b.cable_type_lengths)
    for name in sorted(set(a_types) | set(b_types)):
        label = name or "Cable type not set"
        rows.append(_value_row(f"{label} (route)", a_types.get(name, (None, None))[0],
                               b_types.get(name, (None, None))[0], "km", 3))
        rows.append(_value_row(f"{label} (cable)", a_types.get(name, (None, None))[1],
                               b_types.get(name, (None, None))[1], "km", 3))
    return rows


def change_summary(comparison: RplComparison) -> str:
    """One sentence naming what the comparison found."""
    counts = comparison.position_counts()
    legs = comparison.leg_counts()
    offset = comparison.max_offset_m()
    bits = [
        f"{counts.get(STATUS_UNCHANGED, 0)} position(s) unchanged",
        f"{counts.get(STATUS_CHANGED, 0)} changed",
        f"{counts.get(STATUS_ADDED, 0)} added",
        f"{counts.get(STATUS_REMOVED, 0)} removed",
    ]
    text = " · ".join(bits)
    text += (f". Legs: {legs.get(STATUS_CHANGED, 0)} changed, "
             f"{legs.get(STATUS_ADDED, 0)} added, {legs.get(STATUS_REMOVED, 0)} removed.")
    if offset is not None:
        text += f" Largest matched position offset {offset:,.1f} m."
    return text


def describe_changes(changes: Sequence[str],
                     labels: Optional[Dict[str, str]] = None) -> str:
    labels = labels or CHANGE_LABELS
    return ", ".join(labels.get(key, key.replace("_", " ")) for key in changes or ())


def _count_row(label: str, a_value: int, b_value: int) -> Tuple[str, str, str, str]:
    delta = int(b_value) - int(a_value)
    return (label, str(a_value), str(b_value), f"{delta:+d}" if delta else "")


def _value_row(label: str, a_value, b_value, unit: str,
               decimals: int) -> Tuple[str, str, str, str]:
    a_text = "" if a_value is None else f"{float(a_value):.{decimals}f} {unit}".strip()
    b_text = "" if b_value is None else f"{float(b_value):.{decimals}f} {unit}".strip()
    delta = _difference(a_value, b_value)
    delta_text = ""
    if delta is not None and abs(delta) >= 0.5 * 10 ** -decimals:
        delta_text = f"{delta:+.{decimals}f} {unit}".strip()
    return (label, a_text, b_text, delta_text)


def _counts(values: Iterable[str]) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return result


def _text(value) -> str:
    return str(value or "").strip()


def _difference(start, end) -> Optional[float]:
    if start is None or end is None:
        return None
    try:
        return float(end) - float(start)
    except (TypeError, ValueError):
        return None
