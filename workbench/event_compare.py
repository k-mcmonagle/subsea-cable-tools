# -*- coding: utf-8 -*-
"""Event-level comparison of two RPLs (design vs as-laid, or any pair).

Where :mod:`rpl_compare` diffs two revisions position by position, this
module answers the installation question: *where did each event end up?*
It pairs the events of RPL A (e.g. the design) with the events of RPL B
(e.g. the as-laid), lets the user correct the pairing, and measures every
pair against route A.

Matching
--------
Event text is the main key, but an as-laid RPL often spells an event
differently ("RPT 12" / "Repeater R12 (S/N 4432)"), adds notes, or carries a
typo. Each candidate pair therefore gets a cost built from:

* **text** — identical once case, spacing and punctuation are removed
  (*exact*); otherwise a similarity that rewards one name containing the
  other and the same identifying number, and that *penalises different
  numbers*: "RPT 12" and "RPT 13" look alike but are different repeaters;
* **type** — the project's event rules classify both sides (repeater, BU,
  joint, alter course ...); a repeater never pairs with a joint;
* **position** — separation on the ground, gated by a search radius.

Unique exact names become anchors, thinned to a longest increasing run so
the pairing keeps route order; the stretches between anchors are aligned
with the same Needleman-Wunsch pass :mod:`rpl_compare` uses, so an extra or
missing event costs a gap instead of shifting every later pair. An RPL
recorded in the opposite direction is detected and handled.

Every pair records *how* it was made (``exact``, ``fuzzy``, ``position``,
``manual``) so the review table can point the user at the uncertain ones.

Offsets
-------
For each pair, measured on route A (the RPL A positions joined in order):

* **along-track** — KP on route A of B's event minus A's event KP
  (+ ahead, in the direction of increasing KP);
* **cross-course** — signed perpendicular distance from route A
  (+ starboard / right of increasing KP, - port);
* **radial** — straight distance between the two events, with the bearing
  and east/north components;
* **ΔKP** — B's own RPL KP minus A's (chainage difference), plus cable
  distance and depth differences.

Distances use a local ellipsoidal (WGS84) tangent plane around each pair,
accurate to well under a metre at the separations this tool is about; very
large separations fall back to the haversine distance.

Pure Python — no QGIS imports — so it runs headless and is unit-tested in
``tests/test_event_compare.py``.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .rpl_compare import _longest_increasing, haversine_m, normalise_event

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HOW_EXACT = "exact"
HOW_FUZZY = "fuzzy"
HOW_POSITION = "position"
HOW_MANUAL = "manual"
HOW_UNMATCHED = ""

HOW_LABELS = {
    HOW_EXACT: "Exact name",
    HOW_FUZZY: "Similar name",
    HOW_POSITION: "Same type, nearby",
    HOW_MANUAL: "Manual",
    HOW_UNMATCHED: "Unmatched",
}

#: Default search radius: pairs that are not an exact name match must lie
#: within this distance (fuzzy names may reach FUZZY_RADIUS_FACTOR times it).
DEFAULT_SEARCH_RADIUS_M = 1000.0
FUZZY_RADIUS_FACTOR = 10.0
FUZZY_TEXT_THRESHOLD = 0.7

# Alignment cost model (same shape as rpl_compare): a pair only ever matches
# below MATCH_THRESHOLD, and GAP_COST is half of it so the dynamic program
# prefers a gap over a bad pair.
MATCH_THRESHOLD = 0.6
GAP_COST = 0.3
TEXT_WEIGHT = 0.6
MAX_DP_CELLS = 250_000

TYPE_OTHER = "other"

#: Readable names for the default event-rule subtypes.
TYPE_LABELS = {
    "repeater": "Repeater",
    "bu": "Branching unit",
    "equaliser": "Equaliser",
    "joint": "Joint",
    "bmh": "Beach manhole",
    "transition": "Transition",
    "crossing": "Crossing",
    "route_branch": "Route branch",
    "operations": "Operations mark",
    "alter_course": "Alter course",
    "water_depth": "Water depth mark",
    "boundary": "Boundary",
    TYPE_OTHER: "Other / unclassified",
}

#: Filter presets: (key, label, included types or None, excluded types).
FILTER_PRESETS: Tuple[Tuple[str, str, Optional[frozenset], frozenset], ...] = (
    ("all", "All events", None, frozenset()),
    ("bodies", "Cable bodies (repeaters, BUs, equalisers, joints)",
     frozenset({"repeater", "bu", "equaliser", "joint", "bmh"}), frozenset()),
    ("repeaters", "Repeaters", frozenset({"repeater"}), frozenset()),
    ("bu", "Branching units", frozenset({"bu"}), frozenset()),
    ("joints", "Joints", frozenset({"joint"}), frozenset()),
    ("transitions", "Transitions", frozenset({"transition"}), frozenset()),
    ("crossings", "Crossings", frozenset({"crossing"}), frozenset()),
    ("no_ac", "All except alter courses", None, frozenset({"alter_course"})),
)

_WGS84_A = 6378137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)
#: Beyond this the tangent plane is not trusted for the radial distance.
_LOCAL_PLANE_LIMIT_M = 20000.0


def type_label(key: str) -> str:
    return TYPE_LABELS.get(key, (key or TYPE_OTHER).replace("_", " ").capitalize())


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EventInfo:
    """One event of an RPL: a position whose Event field is not blank."""

    index: int                       # index into the RPL's ordered point list
    event: str
    seq: Optional[int] = None
    pos: object = None
    kp: Optional[float] = None       # km
    cable_kp: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    depth: Optional[float] = None
    remarks: str = ""
    types: Tuple[str, ...] = (TYPE_OTHER,)

    @property
    def type_key(self) -> str:
        return self.types[0] if self.types else TYPE_OTHER

    @property
    def type_text(self) -> str:
        return " + ".join(type_label(t) for t in self.types)

    @property
    def classified(self) -> bool:
        return self.type_key != TYPE_OTHER

    def label(self) -> str:
        kp = f"KP {self.kp:.3f}" if self.kp is not None else "KP ?"
        return f"{self.event} — {kp}"


def extract_events(points: Sequence[Dict], classify: Optional[Callable] = None
                   ) -> List[EventInfo]:
    """Events of one RPL in route order.

    ``points`` are the RPL point dicts (``rpl_summary.read_point_rows``
    shape: seq, pos, event, kp, cable_kp, lat, lon, depth, remarks), already
    in route order. ``classify`` is an ``EventClassifier.classify`` callable;
    the project's own event rules decide what is a repeater or a joint.
    """
    if classify is None:
        from .assembly_model import EventClassifier

        classify = EventClassifier.with_defaults().classify
    events: List[EventInfo] = []
    for index, row in enumerate(points or ()):
        text = str(row.get("event") or "").strip()
        if not text:
            continue
        result = classify(text)
        types = tuple(t for t in (
            getattr(result, "body_type", "") if getattr(result, "is_assembly", False) else "",
            getattr(result, "geo_type", "") if getattr(result, "is_geographic", False) else "",
        ) if t) if getattr(result, "matched", False) else ()
        events.append(EventInfo(
            index=index, event=text, seq=_int(row.get("seq")), pos=row.get("pos"),
            kp=_float(row.get("kp")), cable_kp=_float(row.get("cable_kp")),
            lat=_float(row.get("lat")), lon=_float(row.get("lon")),
            depth=_float(row.get("depth")), remarks=str(row.get("remarks") or "").strip(),
            types=types or (TYPE_OTHER,),
        ))
    return events


# ---------------------------------------------------------------------------
# Text similarity
# ---------------------------------------------------------------------------
_NUMBER = re.compile(r"\d+")


def event_numbers(text: str) -> Tuple[int, ...]:
    """Identifying numbers in an event name, leading zeros ignored."""
    return tuple(int(n) for n in _NUMBER.findall(str(text or "")))


def text_similarity(a: str, b: str) -> float:
    """0 (unrelated) .. 1 (same name) for two event names.

    Exact after normalisation is 1. One name containing the other ("BU1" /
    "BU1 AS LAID") scores high, so does the same leading number. A different
    leading number caps the score low: RPT12 and RPT13 are not a typo.
    """
    na, nb = normalise_event(a), normalise_event(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    shorter, longer = sorted((na, nb), key=len)
    if len(shorter) >= 2 and shorter in longer:
        ratio = max(ratio, 0.9)
    nums_a, nums_b = event_numbers(a), event_numbers(b)
    if nums_a and nums_b:
        if nums_a[0] == nums_b[0]:
            letters_a = re.sub(r"[^A-Z]", "", na)
            letters_b = re.sub(r"[^A-Z]", "", nb)
            if letters_a and letters_b and (letters_a[0] == letters_b[0]
                                            or letters_a in letters_b or letters_b in letters_a):
                ratio = max(ratio, 0.85)
        elif nums_a[0] not in nums_b and nums_b[0] not in nums_a:
            ratio = min(ratio, 0.3)
    elif bool(nums_a) != bool(nums_b):
        ratio = min(ratio, 0.75)
    return ratio


# ---------------------------------------------------------------------------
# Pair cost
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MatchOptions:
    search_radius_m: float = DEFAULT_SEARCH_RADIUS_M
    respect_order: bool = True


def _distance(a: EventInfo, b: EventInfo) -> Optional[float]:
    return haversine_m(a.lat, a.lon, b.lat, b.lon)


def types_compatible(a: EventInfo, b: EventInfo) -> bool:
    if not a.classified or not b.classified:
        return True
    return bool(set(a.types) & set(b.types))


def pair_cost(a: EventInfo, b: EventInfo, options: MatchOptions = MatchOptions()
              ) -> Tuple[float, str]:
    """``(cost, how)``: cost 0 (certainly the same event) .. ``inf`` (never)."""
    radius = max(float(options.search_radius_m or 0.0), 1.0)
    distance = _distance(a, b)
    exact = normalise_event(a.event) == normalise_event(b.event)
    if not exact:
        if not types_compatible(a, b):
            return float("inf"), HOW_UNMATCHED
        # Cheap gate before the string comparison.
        if distance is not None and distance > radius * FUZZY_RADIUS_FACTOR:
            return float("inf"), HOW_UNMATCHED
    text = 1.0 if exact else text_similarity(a.event, b.event)
    spatial = 0.5 if distance is None else math.exp(-distance / radius)
    cost = 1.0 - (TEXT_WEIGHT * text + (1.0 - TEXT_WEIGHT) * spatial)
    if exact:
        how = HOW_EXACT
    elif text >= FUZZY_TEXT_THRESHOLD:
        how = HOW_FUZZY
    elif (a.classified and b.classified and distance is not None
          and distance <= radius):
        how = HOW_POSITION
        cost = min(cost, MATCH_THRESHOLD - 0.05)
    else:
        return float("inf"), HOW_UNMATCHED
    if cost > MATCH_THRESHOLD:
        return float("inf"), HOW_UNMATCHED
    return cost, how


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AutoMatch:
    a: int            # index into events_a
    b: int            # index into events_b
    how: str
    score: float      # 0..1 confidence (1 - cost)


def detect_reversed(events_a: Sequence[EventInfo], events_b: Sequence[EventInfo]) -> bool:
    """True when B runs in the opposite direction to A.

    Decided by which direction keeps more exact-name anchors in order, with
    the end-point geometry as the tie-break.
    """
    anchors = _exact_anchors(events_a, events_b)
    if len(anchors) >= 2:
        forward = len(_longest_increasing(anchors))
        n_b = len(events_b)
        backward = len(_longest_increasing(
            [(a, n_b - 1 - b, how) for a, b, how in anchors]))
        if forward != backward:
            return backward > forward
    if len(events_a) >= 2 and len(events_b) >= 2:
        first_a, last_a = events_a[0], events_a[-1]
        first_b, last_b = events_b[0], events_b[-1]
        same = [_distance(first_a, first_b), _distance(last_a, last_b)]
        cross = [_distance(first_a, last_b), _distance(last_a, first_b)]
        if None not in same and None not in cross:
            return sum(cross) < 0.5 * sum(same)
    return False


def match_events(events_a: Sequence[EventInfo], events_b: Sequence[EventInfo],
                 options: MatchOptions = MatchOptions()) -> Tuple[List[AutoMatch], bool]:
    """Suggest a pairing of A's events with B's.

    Returns ``(matches, reversed)``: matches sorted by A index, each B event
    used at most once, and whether B was found to run backwards.
    """
    events_a = list(events_a)
    events_b = list(events_b)
    if not events_a or not events_b:
        return [], False
    is_reversed = detect_reversed(events_a, events_b)
    order_b = list(range(len(events_b)))
    if is_reversed:
        order_b.reverse()
    ordered_b = [events_b[i] for i in order_b]

    if not options.respect_order:
        pairs = _match_unordered(events_a, ordered_b, options)
    else:
        anchors = _longest_increasing(_exact_anchors(events_a, ordered_b))
        pairs = []
        previous_a, previous_b = -1, -1
        for index_a, index_b, how in anchors + [(len(events_a), len(ordered_b), "")]:
            gap_a = list(range(previous_a + 1, index_a))
            gap_b = list(range(previous_b + 1, index_b))
            pairs.extend(_align_gap(events_a, ordered_b, gap_a, gap_b, options))
            if how:
                cost, _how = pair_cost(events_a[index_a], ordered_b[index_b], options)
                pairs.append((index_a, index_b, HOW_EXACT,
                              0.0 if cost == float("inf") else cost))
            previous_a, previous_b = index_a, index_b

    matches = [AutoMatch(a=a, b=order_b[b], how=how, score=round(max(0.0, 1.0 - cost), 3))
               for a, b, how, cost in pairs]
    matches.sort(key=lambda m: m.a)
    return matches, is_reversed


def _exact_anchors(events_a, events_b) -> List[Tuple[int, int, str]]:
    """(a, b, how) for names that occur exactly once on each side."""
    def unique(events):
        seen: Dict[str, int] = {}
        duplicated = set()
        for index, event in enumerate(events):
            key = normalise_event(event.event)
            if not key:
                continue
            if key in seen:
                duplicated.add(key)
            seen[key] = index
        return {k: v for k, v in seen.items() if k not in duplicated}

    index_a, index_b = unique(events_a), unique(events_b)
    return sorted((a, index_b[key], HOW_EXACT) for key, a in index_a.items() if key in index_b)


def _align_gap(events_a, events_b, gap_a, gap_b, options):
    if not gap_a or not gap_b:
        return []
    if len(gap_a) * len(gap_b) > MAX_DP_CELLS:
        return _align_greedy(events_a, events_b, gap_a, gap_b, options)
    rows, columns = len(gap_a), len(gap_b)
    pair = [[pair_cost(events_a[gap_a[i]], events_b[gap_b[j]], options)
             for j in range(columns)] for i in range(rows)]
    cost = [[0.0] * (columns + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        cost[i][0] = cost[i - 1][0] + GAP_COST
    for j in range(1, columns + 1):
        cost[0][j] = cost[0][j - 1] + GAP_COST
    for i in range(1, rows + 1):
        row, previous = cost[i], cost[i - 1]
        pair_row = pair[i - 1]
        for j in range(1, columns + 1):
            row[j] = min(previous[j - 1] + pair_row[j - 1][0],
                         previous[j] + GAP_COST, row[j - 1] + GAP_COST)
    out = []
    i, j = rows, columns
    while i > 0 and j > 0:
        pair_value, how = pair[i - 1][j - 1]
        if pair_value != float("inf") and abs(cost[i][j] - (cost[i - 1][j - 1] + pair_value)) < 1e-12:
            out.append((gap_a[i - 1], gap_b[j - 1], how, pair_value))
            i, j = i - 1, j - 1
        elif abs(cost[i][j] - (cost[i - 1][j] + GAP_COST)) < 1e-12:
            i -= 1
        else:
            j -= 1
    out.reverse()
    return out


def _align_greedy(events_a, events_b, gap_a, gap_b, options):
    """Monotone best-in-window pass for gaps too wide for the DP."""
    out = []
    cursor = 0
    window = 50
    for index_a in gap_a:
        best = None
        for offset in range(cursor, min(len(gap_b), cursor + window)):
            value, how = pair_cost(events_a[index_a], events_b[gap_b[offset]], options)
            if value != float("inf") and (best is None or value < best[0]):
                best = (value, offset, how)
        if best is not None:
            out.append((index_a, gap_b[best[1]], best[2], best[0]))
            cursor = best[1] + 1
    return out


def _match_unordered(events_a, events_b, options):
    """Globally cheapest pairs first, each event used once (order ignored)."""
    candidates = []
    for i, a in enumerate(events_a):
        for j, b in enumerate(events_b):
            value, how = pair_cost(a, b, options)
            if value != float("inf"):
                candidates.append((value, i, j, how))
    candidates.sort()
    used_a, used_b, out = set(), set(), []
    for value, i, j, how in candidates:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        out.append((i, j, how, value))
    return out


# ---------------------------------------------------------------------------
# Editable mapping
# ---------------------------------------------------------------------------
@dataclass
class EventMapping:
    """The pairing of A's events with B's, as suggested and as corrected."""

    events_a: List[EventInfo]
    events_b: List[EventInfo]
    pairs: Dict[int, Tuple[int, str, float]] = field(default_factory=dict)  # a -> (b, how, score)
    reversed: bool = False
    rejected: set = field(default_factory=set)   # A indices the user un-matched

    @classmethod
    def suggest(cls, events_a, events_b, options: MatchOptions = MatchOptions()) -> "EventMapping":
        matches, is_reversed = match_events(events_a, events_b, options)
        mapping = cls(list(events_a), list(events_b), reversed=is_reversed)
        for match in matches:
            mapping.pairs[match.a] = (match.b, match.how, match.score)
        return mapping

    # -- queries --------------------------------------------------------------
    def partner(self, a_index: int) -> Optional[int]:
        pair = self.pairs.get(a_index)
        return pair[0] if pair else None

    def owner_of(self, b_index: int) -> Optional[int]:
        for a_index, (b, _how, _score) in self.pairs.items():
            if b == b_index:
                return a_index
        return None

    def how(self, a_index: int) -> str:
        pair = self.pairs.get(a_index)
        return pair[1] if pair else HOW_UNMATCHED

    def unmatched_b(self) -> List[int]:
        used = {b for b, _how, _score in self.pairs.values()}
        return [i for i in range(len(self.events_b)) if i not in used]

    def counts(self) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for _b, how, _score in self.pairs.values():
            result[how] = result.get(how, 0) + 1
        result["unmatched_a"] = len(self.events_a) - len(self.pairs)
        result["unmatched_b"] = len(self.unmatched_b())
        return result

    # -- edits ----------------------------------------------------------------
    def set_partner(self, a_index: int, b_index: Optional[int]) -> Optional[int]:
        """Pair A event ``a_index`` with B event ``b_index`` (None = no match).

        A B event belongs to one A event only, so a previous owner loses it;
        that owner's index is returned so the caller can refresh its row.
        """
        displaced = None
        if b_index is not None:
            displaced = self.owner_of(b_index)
            if displaced is not None and displaced != a_index:
                self.pairs.pop(displaced, None)
                self.rejected.add(displaced)
            self.pairs[a_index] = (b_index, HOW_MANUAL, 1.0)
            self.rejected.discard(a_index)
        else:
            self.pairs.pop(a_index, None)
            self.rejected.add(a_index)
        return displaced if displaced != a_index else None

    def manual_overrides(self) -> List[Dict]:
        """User corrections as identity records, for saving with the project."""
        out = []
        for a_index, (b_index, how, _score) in sorted(self.pairs.items()):
            if how == HOW_MANUAL:
                out.append({**_identity("a", self.events_a[a_index]),
                            **_identity("b", self.events_b[b_index])})
        for a_index in sorted(self.rejected):
            if a_index not in self.pairs:
                out.append({**_identity("a", self.events_a[a_index]), "b_event": None})
        return out

    def apply_overrides(self, overrides: Iterable[Dict]) -> int:
        """Re-apply saved corrections where both events still exist; returns
        how many applied."""
        applied = 0
        for record in overrides or ():
            a_index = _find(self.events_a, record.get("a_seq"), record.get("a_event"))
            if a_index is None:
                continue
            if record.get("b_event") is None:
                self.set_partner(a_index, None)
                applied += 1
                continue
            b_index = _find(self.events_b, record.get("b_seq"), record.get("b_event"))
            if b_index is not None:
                self.set_partner(a_index, b_index)
                applied += 1
        return applied


def _identity(side: str, event: EventInfo) -> Dict:
    return {f"{side}_seq": event.seq, f"{side}_event": event.event}


def _find(events: Sequence[EventInfo], seq, text) -> Optional[int]:
    if text is None:
        return None
    candidates = [i for i, e in enumerate(events) if e.event == text]
    if len(candidates) > 1 and seq is not None:
        exact = [i for i in candidates if events[i].seq == seq]
        candidates = exact or candidates
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Geometry: route A as a polyline with chainage
# ---------------------------------------------------------------------------
def _local_scale(lat_deg: float) -> Tuple[float, float]:
    """Metres per radian of latitude (M) and of longitude (N cos phi)."""
    phi = math.radians(lat_deg)
    sin2 = math.sin(phi) ** 2
    w = math.sqrt(1.0 - _WGS84_E2 * sin2)
    meridional = _WGS84_A * (1.0 - _WGS84_E2) / (w ** 3)
    prime_vertical = _WGS84_A / w
    return meridional, prime_vertical * math.cos(phi)


def _enu(origin_lat, origin_lon, lat, lon) -> Tuple[float, float]:
    """East/north metres of (lat, lon) from the origin on the local plane."""
    m_lat, m_lon = _local_scale(origin_lat)
    d_lon = (lon - origin_lon + 540.0) % 360.0 - 180.0
    return math.radians(d_lon) * m_lon, math.radians(lat - origin_lat) * m_lat


class RouteLine:
    """Route A as ordered positions with a chainage (km) per position.

    The chainage is the RPL's own KP when every position has one and it never
    decreases; otherwise the cumulative distance along the positions.
    """

    def __init__(self, points: Sequence[Dict]):
        coords = [(_float(p.get("lat")), _float(p.get("lon")), _float(p.get("kp")))
                  for p in points or ()]
        self._index_map: List[int] = []
        self.lat: List[float] = []
        self.lon: List[float] = []
        kps: List[Optional[float]] = []
        for index, (lat, lon, kp) in enumerate(coords):
            if lat is None or lon is None:
                continue
            self._index_map.append(index)
            self.lat.append(lat)
            self.lon.append(lon)
            kps.append(kp)
        rpl_kp_ok = (len(kps) >= 2 and all(k is not None for k in kps)
                     and all(b >= a for a, b in zip(kps, kps[1:])))
        if rpl_kp_ok:
            self.kp = [float(k) for k in kps]
            self.uses_rpl_kp = True
        else:
            self.kp = [0.0]
            for i in range(1, len(self.lat)):
                step = haversine_m(self.lat[i - 1], self.lon[i - 1], self.lat[i], self.lon[i]) or 0.0
                self.kp.append(self.kp[-1] + step / 1000.0)
            self.uses_rpl_kp = False

    def __len__(self):
        return len(self.lat)

    def vertex_for_point(self, point_index: int) -> Optional[int]:
        """Vertex index of RPL point ``point_index`` (None if it has no position)."""
        try:
            return self._index_map.index(point_index)
        except ValueError:
            return None

    def project(self, lat: float, lon: float, near_vertex: Optional[int] = None,
                window_km: float = 2.0) -> Optional[Tuple[float, float, int]]:
        """``(kp_km, signed_offset_m, segment)`` of the nearest point on the
        route; offset + starboard (right of increasing KP).

        With ``near_vertex`` only segments within ``window_km`` chainage of it
        are searched first, so a route that doubles back cannot snap to the
        wrong pass; the whole route is searched if that finds nothing.
        """
        if len(self.lat) < 2:
            return None
        segments = range(len(self.lat) - 1)
        if near_vertex is not None and 0 <= near_vertex < len(self.kp):
            centre = self.kp[near_vertex]
            local = [s for s in segments
                     if self.kp[s + 1] >= centre - window_km and self.kp[s] <= centre + window_km]
            best = self._nearest(lat, lon, local)
            if best is not None:
                return best
        return self._nearest(lat, lon, segments)

    def _nearest(self, lat, lon, segments):
        best = None
        for s in segments:
            east1, north1 = _enu(lat, lon, self.lat[s], self.lon[s])
            east2, north2 = _enu(lat, lon, self.lat[s + 1], self.lon[s + 1])
            dx, dy = east2 - east1, north2 - north1
            length2 = dx * dx + dy * dy
            if length2 <= 0.0:
                t = 0.0
            else:
                # Point is at the origin (0, 0) of this local plane.
                t = max(0.0, min(1.0, -(east1 * dx + north1 * dy) / length2))
            px, py = east1 + t * dx, north1 + t * dy
            distance = math.hypot(px, py)
            if best is None or distance < best[0]:
                # Cross product of the segment direction with segment->point:
                # positive = point left of travel = port.
                cross = dx * (0.0 - north1) - dy * (0.0 - east1)
                sign = -1.0 if cross > 0 else 1.0
                kp = self.kp[s] + t * (self.kp[s + 1] - self.kp[s])
                best = (distance, kp, sign * distance if distance > 1e-9 else 0.0, s)
        if best is None:
            return None
        return best[1], best[2], best[3]


# ---------------------------------------------------------------------------
# Offsets
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EventOffset:
    """One compared event pair (or an unmatched event, with ``b``/``a`` None)."""

    a: Optional[EventInfo]
    b: Optional[EventInfo]
    how: str = HOW_UNMATCHED
    score: float = 0.0
    along_m: Optional[float] = None
    cross_m: Optional[float] = None
    radial_m: Optional[float] = None
    bearing_deg: Optional[float] = None
    east_m: Optional[float] = None
    north_m: Optional[float] = None
    kp_on_a_km: Optional[float] = None
    kp_delta_m: Optional[float] = None
    cable_kp_delta_m: Optional[float] = None
    depth_delta_m: Optional[float] = None

    @property
    def matched(self) -> bool:
        return self.a is not None and self.b is not None

    @property
    def type_key(self) -> str:
        return (self.a or self.b).type_key if (self.a or self.b) else TYPE_OTHER

    def within(self, radius_m: Optional[float]) -> Optional[bool]:
        if not radius_m or self.radial_m is None:
            return None
        return self.radial_m <= radius_m


def measure_pair(a: EventInfo, b: EventInfo, route_a: Optional[RouteLine] = None,
                 reversed_b: bool = False) -> Dict[str, Optional[float]]:
    """Offsets of B's event from A's event (see the module docstring)."""
    out: Dict[str, Optional[float]] = dict.fromkeys(
        ("along_m", "cross_m", "radial_m", "bearing_deg", "east_m", "north_m",
         "kp_on_a_km", "kp_delta_m", "cable_kp_delta_m", "depth_delta_m"))
    if None not in (a.lat, a.lon, b.lat, b.lon):
        mid_lat = 0.5 * (a.lat + b.lat)
        east, north = _enu(mid_lat, a.lon, b.lat, b.lon)
        east0, north0 = _enu(mid_lat, a.lon, a.lat, a.lon)
        east, north = east - east0, north - north0
        radial = math.hypot(east, north)
        if radial > _LOCAL_PLANE_LIMIT_M:
            radial = haversine_m(a.lat, a.lon, b.lat, b.lon)
        out.update(radial_m=radial, east_m=east, north_m=north,
                   bearing_deg=(math.degrees(math.atan2(east, north)) + 360.0) % 360.0
                   if radial > 1e-9 else None)
        if route_a is not None and len(route_a) >= 2:
            vertex = route_a.vertex_for_point(a.index)
            window = max(2.0, 3.0 * radial / 1000.0 + 0.5)
            projected_b = route_a.project(b.lat, b.lon, vertex, window)
            if vertex is not None:
                kp_a = route_a.kp[vertex]
            else:
                projected_a = route_a.project(a.lat, a.lon)
                kp_a = projected_a[0] if projected_a else None
            if projected_b is not None and kp_a is not None:
                out.update(kp_on_a_km=projected_b[0],
                           along_m=(projected_b[0] - kp_a) * 1000.0,
                           cross_m=projected_b[1])
    if not reversed_b:
        if a.kp is not None and b.kp is not None:
            out["kp_delta_m"] = (b.kp - a.kp) * 1000.0
        if a.cable_kp is not None and b.cable_kp is not None:
            out["cable_kp_delta_m"] = (b.cable_kp - a.cable_kp) * 1000.0
    if a.depth is not None and b.depth is not None:
        out["depth_delta_m"] = b.depth - a.depth
    return out


def compute_offsets(mapping: EventMapping, points_a: Sequence[Dict]) -> List[EventOffset]:
    """Every A event (matched or not) plus B's unmatched events, in route order."""
    route = RouteLine(points_a)
    rows: List[EventOffset] = []
    for a_index, a in enumerate(mapping.events_a):
        pair = mapping.pairs.get(a_index)
        if pair is None:
            rows.append(EventOffset(a=a, b=None))
            continue
        b = mapping.events_b[pair[0]]
        rows.append(EventOffset(a=a, b=b, how=pair[1], score=pair[2],
                                **measure_pair(a, b, route, mapping.reversed)))
    for b_index in mapping.unmatched_b():
        b = mapping.events_b[b_index]
        kp_on_a = None
        if b.lat is not None and b.lon is not None:
            projected = route.project(b.lat, b.lon)
            kp_on_a = projected[0] if projected else None
        rows.append(EventOffset(a=None, b=b, kp_on_a_km=kp_on_a))
    rows.sort(key=_route_order_key)
    return rows


def _route_order_key(row: EventOffset):
    if row.a is not None:
        return (row.a.kp if row.a.kp is not None else float(row.a.index), 0)
    return (row.kp_on_a_km if row.kp_on_a_km is not None else float("inf"), 1)


# ---------------------------------------------------------------------------
# Filters and statistics
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EventFilter:
    include: Optional[frozenset] = None      # None = every type
    exclude: frozenset = frozenset()
    text: str = ""
    show_unmatched: bool = True

    @classmethod
    def preset(cls, key: str, **kwargs) -> "EventFilter":
        for preset_key, _label, include, exclude in FILTER_PRESETS:
            if preset_key == key:
                return cls(include=include, exclude=exclude, **kwargs)
        return cls(**kwargs)

    def accepts(self, row: EventOffset) -> bool:
        if not self.show_unmatched and not row.matched:
            return False
        types = set()
        for event in (row.a, row.b):
            if event is not None:
                types.update(event.types)
        if self.include is not None and not (types & self.include):
            return False
        if self.exclude and types and types <= self.exclude:
            return False
        if self.text:
            needle = self.text.strip().lower()
            haystack = " ".join(e.event for e in (row.a, row.b) if e is not None).lower()
            if needle and needle not in haystack:
                try:
                    if not re.search(self.text, haystack, re.IGNORECASE):
                        return False
                except re.error:
                    return False
        return True


def filter_rows(rows: Iterable[EventOffset], event_filter: EventFilter) -> List[EventOffset]:
    return [row for row in rows if event_filter.accepts(row)]


def type_counts(rows: Iterable[EventOffset]) -> List[Tuple[str, int]]:
    """``(type, count)`` over A and B events, most common first."""
    counts: Dict[str, int] = {}
    for row in rows:
        seen = set()
        for event in (row.a, row.b):
            if event is not None:
                seen.update(event.types)
        for key in seen:
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], type_label(item[0])))


@dataclass(frozen=True)
class OffsetStats:
    count: int = 0
    unmatched_a: int = 0
    unmatched_b: int = 0
    radial_mean: Optional[float] = None
    radial_rms: Optional[float] = None
    radial_median: Optional[float] = None
    radial_p95: Optional[float] = None
    radial_max: Optional[float] = None
    along_mean: Optional[float] = None
    along_std: Optional[float] = None
    along_min: Optional[float] = None
    along_max: Optional[float] = None
    cross_mean: Optional[float] = None
    cross_std: Optional[float] = None
    cross_min: Optional[float] = None
    cross_max: Optional[float] = None
    kp_delta_mean: Optional[float] = None
    kp_delta_max_abs: Optional[float] = None
    within_target: Optional[int] = None
    target_radius_m: Optional[float] = None
    worst_event: str = ""


def offset_stats(rows: Iterable[EventOffset], target_radius_m: Optional[float] = None
                 ) -> OffsetStats:
    rows = list(rows)
    matched = [r for r in rows if r.matched]
    radial = [r.radial_m for r in matched if r.radial_m is not None]
    along = [r.along_m for r in matched if r.along_m is not None]
    cross = [r.cross_m for r in matched if r.cross_m is not None]
    kp_delta = [r.kp_delta_m for r in matched if r.kp_delta_m is not None]
    worst = max((r for r in matched if r.radial_m is not None),
                key=lambda r: r.radial_m, default=None)
    within = None
    if target_radius_m:
        within = sum(1 for value in radial if value <= target_radius_m)
    return OffsetStats(
        count=len(matched),
        unmatched_a=sum(1 for r in rows if r.a is not None and r.b is None),
        unmatched_b=sum(1 for r in rows if r.a is None and r.b is not None),
        radial_mean=_mean(radial), radial_rms=_rms(radial),
        radial_median=_percentile(radial, 50), radial_p95=_percentile(radial, 95),
        radial_max=max(radial) if radial else None,
        along_mean=_mean(along), along_std=_std(along),
        along_min=min(along) if along else None, along_max=max(along) if along else None,
        cross_mean=_mean(cross), cross_std=_std(cross),
        cross_min=min(cross) if cross else None, cross_max=max(cross) if cross else None,
        kp_delta_mean=_mean(kp_delta),
        kp_delta_max_abs=max((abs(v) for v in kp_delta), default=None),
        within_target=within, target_radius_m=target_radius_m or None,
        worst_event=(worst.a.event if worst is not None and worst.a is not None else ""),
    )


def stats_by_type(rows: Iterable[EventOffset], target_radius_m: Optional[float] = None
                  ) -> List[Tuple[str, OffsetStats]]:
    groups: Dict[str, List[EventOffset]] = {}
    for row in rows:
        groups.setdefault(row.type_key, []).append(row)
    return [(key, offset_stats(group, target_radius_m))
            for key, group in sorted(groups.items(), key=lambda item: type_label(item[0]))]


# ---------------------------------------------------------------------------
# Tabular output (shared by the panel's CSV export and the report)
# ---------------------------------------------------------------------------
CSV_HEADER = (
    "A event", "A type", "A position", "A KP (km)", "B event", "B position", "B KP (km)",
    "Match", "Match score", "ΔKP B-A (m)", "Along-track (m)", "Cross-course (m)",
    "Radial (m)", "Bearing A→B (deg)", "East (m)", "North (m)", "Δ cable distance (m)",
    "Δ depth (m)", "A lat", "A lon", "B lat", "B lon", "Within target",
)


def csv_rows(rows: Iterable[EventOffset], target_radius_m: Optional[float] = None
             ) -> List[List[str]]:
    out = []
    for row in rows:
        a, b = row.a, row.b
        within = row.within(target_radius_m)
        out.append([
            a.event if a else "", a.type_text if a else (b.type_text if b else ""),
            _plain(a.pos if a else None), _num(a.kp if a else None, 6),
            b.event if b else "", _plain(b.pos if b else None), _num(b.kp if b else None, 6),
            HOW_LABELS.get(row.how, row.how) if row.matched else (
                "Only in A" if a else "Only in B"),
            _num(row.score if row.matched else None, 3),
            _num(row.kp_delta_m, 2), _num(row.along_m, 2), _num(row.cross_m, 2),
            _num(row.radial_m, 2), _num(row.bearing_deg, 1), _num(row.east_m, 2),
            _num(row.north_m, 2), _num(row.cable_kp_delta_m, 2), _num(row.depth_delta_m, 2),
            _num(a.lat if a else None, 8), _num(a.lon if a else None, 8),
            _num(b.lat if b else None, 8), _num(b.lon if b else None, 8),
            "" if within is None else ("yes" if within else "no"),
        ])
    return out


def summary_text(stats: OffsetStats, unit_decimals: int = 1) -> str:
    """One line for the panel."""
    if not stats.count:
        return "No matched events in the current selection."
    d = unit_decimals
    text = (f"{stats.count} matched · radial mean {stats.radial_mean:.{d}f} m, "
            f"RMS {stats.radial_rms:.{d}f} m, max {stats.radial_max:.{d}f} m")
    if stats.worst_event:
        text += f" ({stats.worst_event})"
    if stats.along_mean is not None:
        text += f" · along-track mean {stats.along_mean:+.{d}f} m"
    if stats.cross_mean is not None:
        text += f" · cross-course mean {stats.cross_mean:+.{d}f} m"
    if stats.within_target is not None:
        text += f" · {stats.within_target}/{stats.count} within {stats.target_radius_m:g} m"
    if stats.unmatched_a or stats.unmatched_b:
        text += f" · unmatched: {stats.unmatched_a} in A, {stats.unmatched_b} in B"
    return text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _float(value) -> Optional[float]:
    try:
        number = None if value is None else float(value)
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def _int(value) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _mean(values):
    return sum(values) / len(values) if values else None


def _rms(values):
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else None


def _std(values):
    if len(values) < 2:
        return 0.0 if values else None
    mean = _mean(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100.0
    low = int(math.floor(rank))
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _num(value, decimals) -> str:
    return "" if value is None else f"{float(value):.{decimals}f}"


def _plain(value) -> str:
    return "" if value is None else str(value)


# Re-exported for callers that build EventInfo rows themselves.
__all__ = [
    "EventInfo", "EventMapping", "EventOffset", "EventFilter", "MatchOptions",
    "OffsetStats", "RouteLine", "extract_events", "match_events", "measure_pair",
    "compute_offsets", "offset_stats", "stats_by_type", "filter_rows", "type_counts",
    "text_similarity", "csv_rows", "summary_text", "replace",
]
