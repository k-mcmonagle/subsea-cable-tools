# -*- coding: utf-8 -*-
"""Pure RPL recompute engine.

Holds an in-memory model of one RPL (points + segments) and derives every
dependent quantity: per-segment geodesic distance and bearing, cumulative
route distance, cable distance via slack (or slack via cable distance), KP
lookups, and the cable-domain <-> route-domain conversions that let an
assembly be fitted onto a route.

Imports only ``qgis.core`` (for QgsDistanceArea / QgsPointXY) plus stdlib, so
it runs headless in the test harness. No widgets, no project access, no
raster I/O — a configured QgsDistanceArea is injected and depth values arrive
through a sampler callable.

Units follow the RPL document convention: distances in the model are in
**kilometres** (DistCumulative / CableDistCumulative / DistBetweenPos /
CableDistBetweenPos), slack in percent. QgsDistanceArea returns metres; the
engine converts.

Slack semantics: ``cable_km = dist_km * (1 + slack_pct / 100)`` per segment.

- HOLD_SLACK (planning): slack is authoritative; geometry edits recompute
  cable distances.
- HOLD_CABLE (as-laid): cable distances are authoritative; geometry edits
  recompute slack.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from qgis.core import QgsDistanceArea, QgsPointXY


class SlackMode(enum.Enum):
    HOLD_SLACK = "hold_slack"
    HOLD_CABLE = "hold_cable"

    @classmethod
    def from_string(cls, value: Optional[str]) -> "SlackMode":
        for mode in cls:
            if mode.value == (value or "").strip().lower():
                return mode
        return cls.HOLD_SLACK


@dataclass
class RplPoint:
    seq: int
    pos_no: Optional[int]
    event: str
    lat: float
    lon: float
    dist_cum_km: Optional[float] = None
    cable_dist_cum_km: Optional[float] = None
    depth_m: Optional[float] = None
    attrs: Dict = field(default_factory=dict)


@dataclass
class RplSegment:
    seq: int
    bearing_deg: Optional[float] = None
    dist_km: Optional[float] = None
    slack_pct: Optional[float] = None
    cable_dist_km: Optional[float] = None
    attrs: Dict = field(default_factory=dict)


@dataclass
class RplSection:
    """Derived event-to-event portion of an RPL.

    ``RplSegment`` remains the computational point-to-point leg. A user-facing
    RPL section can contain several such legs and is bounded by positions with
    event labels (the first and last positions are always boundaries).
    """

    seq: int
    start_point_index: int
    end_point_index: int
    from_pos: Optional[int]
    to_pos: Optional[int]
    from_event: str
    to_event: str
    start_kp_km: Optional[float]
    end_kp_km: Optional[float]
    dist_km: Optional[float]
    cable_dist_km: Optional[float]
    slack_pct: Optional[float]
    leg_count: int
    attrs: Dict = field(default_factory=dict)


@dataclass
class RplModel:
    points: List[RplPoint] = field(default_factory=list)
    segments: List[RplSegment] = field(default_factory=list)

    def __post_init__(self):
        if self.points and len(self.segments) != len(self.points) - 1:
            raise ValueError(
                f"RplModel needs len(points)-1 segments "
                f"(got {len(self.points)} points, {len(self.segments)} segments)"
            )

    def copy(self) -> "RplModel":
        return RplModel(
            points=[RplPoint(p.seq, p.pos_no, p.event, p.lat, p.lon, p.dist_cum_km,
                             p.cable_dist_cum_km, p.depth_m, dict(p.attrs))
                    for p in self.points],
            segments=[RplSegment(s.seq, s.bearing_deg, s.dist_km, s.slack_pct,
                                 s.cable_dist_km, dict(s.attrs))
                      for s in self.segments],
        )

    def start_kp_km(self) -> float:
        if self.points and self.points[0].dist_cum_km is not None:
            return float(self.points[0].dist_cum_km)
        return 0.0

    def end_kp_km(self) -> float:
        if self.points and self.points[-1].dist_cum_km is not None:
            return float(self.points[-1].dist_cum_km)
        return self.start_kp_km()

    def total_route_km(self) -> float:
        return self.end_kp_km() - self.start_kp_km()

    def total_cable_km(self) -> float:
        if not self.points:
            return 0.0
        start = self.points[0].cable_dist_cum_km or 0.0
        end = self.points[-1].cable_dist_cum_km or 0.0
        return end - start


def event_sections(model: RplModel) -> List[RplSection]:
    """Return the RPL broken into sections between labelled event positions.

    Route endpoints are implicit boundaries even when their Event cells are
    blank. Attributes common to every point-to-point leg are retained;
    differing values are reported explicitly rather than choosing one leg.
    """
    if len(model.points) < 2:
        return []
    boundaries = [0]
    boundaries.extend(
        i for i, point in enumerate(model.points[1:-1], 1)
        if str(point.event or "").strip()
    )
    boundaries.append(len(model.points) - 1)

    out: List[RplSection] = []
    for seq, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        legs = model.segments[start:end]
        p0, p1 = model.points[start], model.points[end]
        dist = (sum(float(leg.dist_km) for leg in legs)
                if legs and all(leg.dist_km is not None for leg in legs) else None)
        cable = (sum(float(leg.cable_dist_km) for leg in legs)
                 if legs and all(leg.cable_dist_km is not None for leg in legs) else None)
        slack = None
        if dist is not None and dist > 0 and cable is not None:
            slack = (cable / dist - 1.0) * 100.0
        out.append(RplSection(
            seq=seq,
            start_point_index=start,
            end_point_index=end,
            from_pos=p0.pos_no,
            to_pos=p1.pos_no,
            from_event=str(p0.event or ""),
            to_event=str(p1.event or ""),
            start_kp_km=p0.dist_cum_km,
            end_kp_km=p1.dist_cum_km,
            dist_km=dist,
            cable_dist_km=cable,
            slack_pct=slack,
            leg_count=len(legs),
            attrs=_aggregate_leg_attrs(legs),
        ))
    return out


def _aggregate_leg_attrs(legs: Sequence[RplSegment]) -> Dict:
    keys: List[str] = []
    for leg in legs:
        for key in leg.attrs:
            if key not in keys:
                keys.append(key)
    aggregated = {}
    for key in keys:
        values: List[str] = []
        for leg in legs:
            value = leg.attrs.get(key)
            text = "" if value is None else str(value)
            if text not in values:
                values.append(text)
        if not values or values == [""]:
            aggregated[key] = ""
        elif len(values) == 1:
            aggregated[key] = values[0]
        else:
            shown = " | ".join(value or "(blank)" for value in values[:3])
            if len(values) > 3:
                shown += f" | +{len(values) - 3} more"
            aggregated[key] = f"Mixed: {shown}"
    return aggregated


@dataclass
class ChangeSet:
    """Dirty indices produced by an engine operation.

    ``structural`` marks insert/delete operations where seq numbering changed
    and the layer sync must rebuild rather than patch.
    """
    point_indices: Set[int] = field(default_factory=set)
    segment_indices: Set[int] = field(default_factory=set)
    structural: bool = False
    label: str = ""

    def merge(self, other: "ChangeSet") -> "ChangeSet":
        return ChangeSet(
            point_indices=self.point_indices | other.point_indices,
            segment_indices=self.segment_indices | other.segment_indices,
            structural=self.structural or other.structural,
            label=self.label or other.label,
        )


# ---------------------------------------------------------------------------
# Geodesy helpers
# ---------------------------------------------------------------------------
def _point_xy(p: RplPoint) -> QgsPointXY:
    return QgsPointXY(p.lon, p.lat)


def segment_distance_km(a: RplPoint, b: RplPoint, da: QgsDistanceArea) -> float:
    return float(da.measureLine(_point_xy(a), _point_xy(b))) / 1000.0


def segment_bearing_deg(a: RplPoint, b: RplPoint, da: QgsDistanceArea) -> float:
    import math

    bearing = math.degrees(da.bearing(_point_xy(a), _point_xy(b)))
    return bearing % 360.0


# ---------------------------------------------------------------------------
# Recompute
# ---------------------------------------------------------------------------
def recompute(
    model: RplModel,
    da: QgsDistanceArea,
    *,
    slack_mode: SlackMode = SlackMode.HOLD_SLACK,
    from_seg: int = 0,
) -> ChangeSet:
    """Recompute derived fields from segment ``from_seg`` onwards.

    Segment distances/bearings are measured from the point coordinates; then
    slack/cable distance are reconciled per ``slack_mode``; finally the
    cumulative distances cascade downstream. The first point's cumulative
    values are the anchor (kept as-is; default 0).
    """
    n_seg = len(model.segments)
    changed = ChangeSet()
    if not model.points:
        return changed

    start = max(0, from_seg)
    for i in range(start, n_seg):
        seg = model.segments[i]
        a, b = model.points[i], model.points[i + 1]
        seg.dist_km = segment_distance_km(a, b, da)
        seg.bearing_deg = segment_bearing_deg(a, b, da)
        if slack_mode is SlackMode.HOLD_SLACK:
            slack = seg.slack_pct if seg.slack_pct is not None else 0.0
            seg.cable_dist_km = seg.dist_km * (1.0 + slack / 100.0)
        else:  # HOLD_CABLE
            if seg.cable_dist_km is None:
                # nothing authoritative to hold: fall back to zero slack
                seg.cable_dist_km = seg.dist_km
            if seg.dist_km > 0:
                seg.slack_pct = (seg.cable_dist_km / seg.dist_km - 1.0) * 100.0
        changed.segment_indices.add(i)

    # cumulative cascade (always from the first affected point)
    anchor_route = model.points[0].dist_cum_km or 0.0
    anchor_cable = model.points[0].cable_dist_cum_km or 0.0
    model.points[0].dist_cum_km = anchor_route
    model.points[0].cable_dist_cum_km = anchor_cable
    route = anchor_route
    cable = anchor_cable
    for i, seg in enumerate(model.segments):
        route += seg.dist_km or 0.0
        cable += seg.cable_dist_km or 0.0
        point = model.points[i + 1]
        if i >= start - 1:
            if point.dist_cum_km != route or point.cable_dist_cum_km != cable:
                changed.point_indices.add(i + 1)
        point.dist_cum_km = route
        point.cable_dist_cum_km = cable
    return changed


def derive_slack(model: RplModel) -> int:
    """Fill missing per-segment slack from imported cable/route distances.

    Uses the segment's own CableDistBetweenPos vs DistBetweenPos where both
    are present; returns the number of segments whose slack was derived.
    """
    derived = 0
    for seg in model.segments:
        if seg.slack_pct is not None:
            continue
        if seg.cable_dist_km is not None and seg.dist_km and seg.dist_km > 0:
            seg.slack_pct = (seg.cable_dist_km / seg.dist_km - 1.0) * 100.0
            derived += 1
    return derived


# ---------------------------------------------------------------------------
# Edit operations
# ---------------------------------------------------------------------------
def move_point(
    model: RplModel,
    idx: int,
    lat: float,
    lon: float,
    da: QgsDistanceArea,
    slack_mode: SlackMode = SlackMode.HOLD_SLACK,
) -> ChangeSet:
    if not (0 <= idx < len(model.points)):
        raise IndexError(f"point index {idx} out of range")
    point = model.points[idx]
    point.lat = lat
    point.lon = lon
    changed = ChangeSet(point_indices={idx}, label=f"Move position {point.pos_no or idx}")
    from_seg = max(0, idx - 1)
    changed = changed.merge(recompute(model, da, slack_mode=slack_mode, from_seg=from_seg))
    return changed


def insert_point(
    model: RplModel,
    seg_idx: int,
    lat: float,
    lon: float,
    da: QgsDistanceArea,
    slack_mode: SlackMode = SlackMode.HOLD_SLACK,
) -> ChangeSet:
    """Split segment ``seg_idx`` at (lat, lon).

    The new second half inherits the split segment's attributes and slack.
    ``PosNo`` of the new point is left None (document numbering is not
    invented); ``seq`` values are renumbered.
    """
    if not (0 <= seg_idx < len(model.segments)):
        raise IndexError(f"segment index {seg_idx} out of range")
    split = model.segments[seg_idx]
    new_point = RplPoint(seq=0, pos_no=None, event="", lat=lat, lon=lon)
    new_seg = RplSegment(seq=0, slack_pct=split.slack_pct, attrs=dict(split.attrs))
    model.points.insert(seg_idx + 1, new_point)
    model.segments.insert(seg_idx + 1, new_seg)
    _renumber(model)
    changed = ChangeSet(structural=True, label="Insert position")
    changed = changed.merge(recompute(model, da, slack_mode=slack_mode, from_seg=seg_idx))
    return changed


def delete_point(
    model: RplModel,
    idx: int,
    da: QgsDistanceArea,
    slack_mode: SlackMode = SlackMode.HOLD_SLACK,
) -> ChangeSet:
    """Delete a point, merging its two segments (upstream attributes win)."""
    if len(model.points) <= 2:
        raise ValueError("An RPL needs at least two positions")
    if not (0 <= idx < len(model.points)):
        raise IndexError(f"point index {idx} out of range")

    if idx == 0:
        model.points.pop(0)
        model.segments.pop(0)
        from_seg = 0
    elif idx == len(model.points) - 1:
        model.points.pop()
        model.segments.pop()
        from_seg = max(0, len(model.segments) - 1)
    else:
        # keep the upstream segment's attrs/slack, drop the downstream one
        model.segments.pop(idx)
        model.points.pop(idx)
        from_seg = max(0, idx - 1)
    _renumber(model)
    changed = ChangeSet(structural=True, label="Delete position")
    changed = changed.merge(recompute(model, da, slack_mode=slack_mode, from_seg=from_seg))
    return changed


def _renumber(model: RplModel) -> None:
    for i, point in enumerate(model.points):
        point.seq = i
    for i, seg in enumerate(model.segments):
        seg.seq = i


# ---------------------------------------------------------------------------
# KP / cable-distance conversions
# ---------------------------------------------------------------------------
def kp_of_point(model: RplModel, idx: int) -> Optional[float]:
    return model.points[idx].dist_cum_km


def cable_dist_from_kp(model: RplModel, kp_km: float) -> Optional[float]:
    """Cable distance (km) at route KP, piecewise-linear via per-segment slack."""
    pts = model.points
    if not pts or pts[0].dist_cum_km is None:
        return None
    if kp_km < pts[0].dist_cum_km or kp_km > pts[-1].dist_cum_km:
        return None
    for i in range(len(pts) - 1):
        k0, k1 = pts[i].dist_cum_km, pts[i + 1].dist_cum_km
        if k0 is None or k1 is None:
            return None
        if kp_km <= k1 or i == len(pts) - 2:
            c0, c1 = pts[i].cable_dist_cum_km or 0.0, pts[i + 1].cable_dist_cum_km or 0.0
            if k1 - k0 <= 0:
                return c0
            t = (kp_km - k0) / (k1 - k0)
            if 0.0 <= t <= 1.0:
                return c0 + t * (c1 - c0)
    return None


def kp_from_cable_dist(model: RplModel, cable_km: float) -> Optional[float]:
    """Route KP at a cable distance (km) — inverse of :func:`cable_dist_from_kp`."""
    pts = model.points
    if not pts or pts[0].cable_dist_cum_km is None:
        return None
    if cable_km < pts[0].cable_dist_cum_km or cable_km > pts[-1].cable_dist_cum_km:
        return None
    for i in range(len(pts) - 1):
        c0, c1 = pts[i].cable_dist_cum_km, pts[i + 1].cable_dist_cum_km
        if c0 is None or c1 is None:
            return None
        if cable_km <= c1 or i == len(pts) - 2:
            k0, k1 = pts[i].dist_cum_km or 0.0, pts[i + 1].dist_cum_km or 0.0
            if c1 - c0 <= 0:
                return k0
            t = (cable_km - c0) / (c1 - c0)
            if 0.0 <= t <= 1.0:
                return k0 + t * (k1 - k0)
    return None


def point_at_kp(model: RplModel, kp_km: float, da: QgsDistanceArea) -> Optional[Tuple[float, float]]:
    """(lat, lon) at route KP, interpolated linearly within the segment.

    Good to well under a metre for typical RPL segment lengths; UI callers
    with a RouteFrame available may prefer its geodesic interpolation.
    """
    pts = model.points
    if not pts or pts[0].dist_cum_km is None:
        return None
    if kp_km < pts[0].dist_cum_km or kp_km > pts[-1].dist_cum_km:
        return None
    for i in range(len(pts) - 1):
        k0, k1 = pts[i].dist_cum_km, pts[i + 1].dist_cum_km
        if k0 is None or k1 is None:
            return None
        if kp_km <= k1 or i == len(pts) - 2:
            if k1 - k0 <= 0:
                return (pts[i].lat, pts[i].lon)
            t = (kp_km - k0) / (k1 - k0)
            if 0.0 <= t <= 1.0:
                lat = pts[i].lat + t * (pts[i + 1].lat - pts[i].lat)
                lon = pts[i].lon + t * (pts[i + 1].lon - pts[i].lon)
                return (lat, lon)
    return None


def bearing_at_kp(model: RplModel, kp_km: float) -> Optional[float]:
    """Forward route bearing (deg) of the segment containing KP."""
    pts = model.points
    if not pts:
        return None
    for i in range(len(pts) - 1):
        k0, k1 = pts[i].dist_cum_km, pts[i + 1].dist_cum_km
        if k0 is None or k1 is None:
            return None
        if kp_km <= k1 or i == len(pts) - 2:
            if kp_km >= k0:
                return model.segments[i].bearing_deg
    return None


# ---------------------------------------------------------------------------
# Depth application
# ---------------------------------------------------------------------------
def apply_depths(
    model: RplModel,
    sampler: Callable[[float, float], Optional[float]],
    indices: Optional[Sequence[int]] = None,
) -> ChangeSet:
    """Resample point depths through ``sampler(lat, lon) -> depth_m``.

    ``indices`` limits the pass (e.g. just a moved point); None means all.
    Points where the sampler returns None keep their existing depth.
    """
    changed = ChangeSet(label="Resample depths")
    targets = range(len(model.points)) if indices is None else indices
    for idx in targets:
        if not (0 <= idx < len(model.points)):
            continue
        point = model.points[idx]
        depth = sampler(point.lat, point.lon)
        if depth is not None and depth != point.depth_m:
            point.depth_m = float(depth)
            changed.point_indices.add(idx)
    return changed


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(model: RplModel) -> List[Dict]:
    """Structural sanity report (used at registration and before save)."""
    findings: List[Dict] = []

    def add(rule: str, severity: str, message: str):
        findings.append({"rule_id": rule, "severity": severity, "message": message})

    if len(model.points) < 2:
        add("rpl.too_few_points", "error", "An RPL needs at least two positions.")
    if model.points and len(model.segments) != len(model.points) - 1:
        add("rpl.segment_count", "error",
            f"{len(model.points)} points require {len(model.points) - 1} segments, "
            f"found {len(model.segments)}.")
    for point in model.points:
        if not (-90.0 <= point.lat <= 90.0) or not (-180.0 <= point.lon <= 180.0):
            add("rpl.coordinate_range", "error",
                f"Position {point.pos_no or point.seq} has out-of-range coordinates "
                f"({point.lat}, {point.lon}).")
    for seg in model.segments:
        if seg.dist_km is not None and seg.dist_km <= 0:
            add("rpl.zero_length_segment", "warning",
                f"Segment {seg.seq} has zero/negative length.")
        if seg.slack_pct is not None and seg.slack_pct < -50:
            add("rpl.negative_slack", "warning",
                f"Segment {seg.seq} slack is {seg.slack_pct:.1f}% — check cable distances.")
    return findings
