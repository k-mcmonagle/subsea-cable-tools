# -*- coding: utf-8 -*-
"""Pure 2D geometry for Burial Planner installation paths.

The module intentionally has no QGIS or third-party imports.  Coordinates are
metres in a local projected frame and headings are mathematical radians
(counter-clockwise from +X).  QGIS-facing code is responsible for projecting
route clusters into a suitable local frame and transforming results back.

Two path families are exposed:

* tangent circular fillets for ordinary, well-spaced route corners;
* forward-only Dubins paths for mandatory control points and interacting
  corners.  A dynamic-programming heading search gives every shared waypoint
  one heading, so concatenated legs are tangent-continuous and honour the
  requested minimum turn radius.

The result is planning geometry: curvature is bounded but may step between
zero and +/-1/R at primitive joins.  A future clothoid layer can replace the
primitive generator without changing the persisted/UI contracts.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Pose = Tuple[float, float, float]

_TAU = 2.0 * math.pi
_EPS = 1e-9


class PathGeometryError(ValueError):
    """The requested path cannot be constructed from the supplied controls."""


class PathCancelled(Exception):
    """Cooperative cancellation requested by a background-task adapter."""


@dataclass(frozen=True)
class Primitive:
    """One straight or constant-radius component of a generated path."""

    kind: str                       # "S" | "L" | "R"
    start: Pose
    end: Pose
    length_m: float
    radius_m: Optional[float] = None


@dataclass
class PathSolution:
    points: List[Point] = field(default_factory=list)
    primitives: List[Primitive] = field(default_factory=list)
    waypoint_headings: List[float] = field(default_factory=list)
    max_offset_m: float = 0.0
    rms_offset_m: float = 0.0
    length_m: float = 0.0
    path_types: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Fillet:
    points: List[Point]
    tangent_in: Point
    tangent_out: Point
    center: Point
    turn_rad: float
    tangent_distance_m: float
    miss_distance_m: float
    primitive: Primitive


@dataclass
class RoutePathDiagnostic:
    control_no: int
    vertex_index: int
    station_m: float
    turn_deg: float
    side: str
    solution: str
    miss_m: float
    max_offset_m: float
    status: str
    message: str = ""
    radius_m: float = 0.0
    control_kind: str = "corner"    # "corner" | "adjustment"
    recovery: bool = False
    wide_recovery: bool = False
    corridor_relaxed: bool = False


@dataclass
class RoutePathResult(PathSolution):
    diagnostics: List[RoutePathDiagnostic] = field(default_factory=list)
    course_change_count: int = 0
    compound_cluster_count: int = 0


def _mod2pi(value: float) -> float:
    return value % _TAU


def wrap_pi(value: float) -> float:
    """Angle normalized to (-pi, pi]."""
    value = (float(value) + math.pi) % _TAU - math.pi
    return math.pi if value <= -math.pi + 1e-14 else value


def distance(a: Point, b: Point) -> float:
    return math.hypot(float(b[0]) - float(a[0]),
                      float(b[1]) - float(a[1]))


def heading(a: Point, b: Point) -> float:
    dx = float(b[0]) - float(a[0])
    dy = float(b[1]) - float(a[1])
    if math.hypot(dx, dy) <= _EPS:
        raise PathGeometryError("A route leg has zero length.")
    return math.atan2(dy, dx)


def course_change(p0: Point, p1: Point, p2: Point) -> float:
    """Signed heading change at p1; positive is a left turn."""
    return wrap_pi(heading(p1, p2) - heading(p0, p1))


def clean_polyline(points: Iterable[Point], tolerance_m: float = 1e-6
                   ) -> List[Point]:
    """Drop consecutive duplicates without simplifying genuine corners."""
    out: List[Point] = []
    tol = max(float(tolerance_m), 0.0)
    for raw in points:
        point = (float(raw[0]), float(raw[1]))
        if not out or distance(out[-1], point) > tol:
            out.append(point)
    return out


def polyline_length(points: Sequence[Point]) -> float:
    return sum(distance(a, b) for a, b in zip(points, points[1:]))


def cumulative_lengths(points: Sequence[Point]) -> List[float]:
    out = [0.0]
    for a, b in zip(points, points[1:]):
        out.append(out[-1] + distance(a, b))
    return out


def point_at_distance(points: Sequence[Point], chainages: Sequence[float],
                      station_m: float) -> Point:
    if not points:
        raise PathGeometryError("The route has no points.")
    if len(points) == 1:
        return points[0]
    value = min(max(float(station_m), 0.0), float(chainages[-1]))
    index = bisect.bisect_left(chainages, value)
    if index <= 0:
        return points[0]
    if index >= len(points):
        return points[-1]
    if abs(chainages[index] - value) <= _EPS:
        return points[index]
    lo = index - 1
    span = chainages[index] - chainages[lo]
    if span <= _EPS:
        return points[index]
    t = (value - chainages[lo]) / span
    a, b = points[lo], points[index]
    return (a[0] + t * (b[0] - a[0]),
            a[1] + t * (b[1] - a[1]))


def polyline_slice(points: Sequence[Point], chainages: Sequence[float],
                   start_m: float, end_m: float) -> List[Point]:
    if len(points) < 2:
        return list(points)
    lo, hi = sorted((max(0.0, float(start_m)),
                     min(float(chainages[-1]), float(end_m))))
    if hi <= lo + _EPS:
        point = point_at_distance(points, chainages, lo)
        return [point]
    out = [point_at_distance(points, chainages, lo)]
    # Chainages are monotone. Locate the interior span directly instead of
    # walking the complete route for every replacement. The latter makes a
    # long route with many independent fillets quadratic in its vertex count.
    first = bisect.bisect_right(chainages, lo + _EPS)
    stop = bisect.bisect_left(chainages, hi - _EPS)
    out.extend(points[first:stop])
    out.append(point_at_distance(points, chainages, hi))
    return clean_polyline(out)


def polyline_heading_at(points: Sequence[Point], chainages: Sequence[float],
                        station_m: float) -> float:
    value = min(max(float(station_m), 0.0), float(chainages[-1]))
    index = bisect.bisect_right(chainages, value + _EPS) - 1
    index = min(max(index, 0), len(points) - 2)
    # At an exact interior vertex, use the outgoing leg in travel order.
    if index + 1 < len(points) and distance(points[index], points[index + 1]) > _EPS:
        return heading(points[index], points[index + 1])
    for candidate in range(index - 1, -1, -1):
        if distance(points[candidate], points[candidate + 1]) > _EPS:
            return heading(points[candidate], points[candidate + 1])
    raise PathGeometryError("No route heading is available at the station.")


def _arc_step(radius_m: float, chord_tolerance_m: float) -> float:
    """Maximum angular sample step for a requested chord sagitta."""
    radius = max(float(radius_m), _EPS)
    tolerance = min(max(float(chord_tolerance_m), 0.01), radius)
    if tolerance >= radius:
        return math.pi / 4.0
    return min(math.pi / 12.0,
               2.0 * math.acos(max(-1.0, 1.0 - tolerance / radius)))


def circular_fillet(p0: Point, vertex: Point, p2: Point, radius_m: float,
                    chord_tolerance_m: float = 0.25) -> Fillet:
    """Radius-exact tangent fillet around one non-collinear corner.

    Raises when either adjacent leg cannot hold the two tangent points.  The
    caller should merge such neighbouring corners into a compound cluster.
    """
    radius = float(radius_m)
    if not math.isfinite(radius) or radius <= 0.0:
        raise PathGeometryError("Minimum turning radius must be greater than zero.")
    len_in, len_out = distance(p0, vertex), distance(vertex, p2)
    if len_in <= _EPS or len_out <= _EPS:
        raise PathGeometryError("A fillet needs two non-zero route legs.")
    h_in, h_out = heading(p0, vertex), heading(vertex, p2)
    turn = wrap_pi(h_out - h_in)
    magnitude = abs(turn)
    if magnitude <= 1e-10:
        raise PathGeometryError("The route vertex has no course change.")
    if math.pi - magnitude <= 1e-7:
        raise PathGeometryError("A 180 degree route reversal cannot be filleted.")
    tangent = radius * math.tan(magnitude / 2.0)
    if tangent >= len_in - 1e-7 or tangent >= len_out - 1e-7:
        raise PathGeometryError(
            "The adjacent route legs are too short for this radius fillet.")

    ux_in, uy_in = math.cos(h_in), math.sin(h_in)
    ux_out, uy_out = math.cos(h_out), math.sin(h_out)
    t_in = (vertex[0] - tangent * ux_in,
            vertex[1] - tangent * uy_in)
    t_out = (vertex[0] + tangent * ux_out,
             vertex[1] + tangent * uy_out)
    side = 1.0 if turn > 0.0 else -1.0
    center = (t_in[0] - side * radius * uy_in,
              t_in[1] + side * radius * ux_in)
    start_angle = math.atan2(t_in[1] - center[1],
                             t_in[0] - center[0])
    step = _arc_step(radius, chord_tolerance_m)
    count = max(1, int(math.ceil(magnitude / max(step, 1e-6))))
    points = []
    for index in range(count + 1):
        angle = start_angle + turn * index / count
        points.append((center[0] + radius * math.cos(angle),
                       center[1] + radius * math.sin(angle)))
    points[0], points[-1] = t_in, t_out
    end_heading = h_in + turn
    primitive = Primitive(
        "L" if turn > 0.0 else "R",
        (t_in[0], t_in[1], h_in),
        (t_out[0], t_out[1], end_heading),
        radius * magnitude, radius)
    miss = max(0.0, distance(vertex, center) - radius)
    return Fillet(points, t_in, t_out, center, turn, tangent, miss,
                  primitive)


# -- Dubins primitives ------------------------------------------------------

def _lsl(alpha: float, beta: float, d: float):
    p2 = 2.0 + d * d - 2.0 * math.cos(alpha - beta) \
        + 2.0 * d * (math.sin(alpha) - math.sin(beta))
    if p2 < -1e-10:
        return None
    tmp = math.atan2(math.cos(beta) - math.cos(alpha),
                     d + math.sin(alpha) - math.sin(beta))
    return (_mod2pi(-alpha + tmp), math.sqrt(max(0.0, p2)),
            _mod2pi(beta - tmp))


def _rsr(alpha: float, beta: float, d: float):
    p2 = 2.0 + d * d - 2.0 * math.cos(alpha - beta) \
        + 2.0 * d * (-math.sin(alpha) + math.sin(beta))
    if p2 < -1e-10:
        return None
    tmp = math.atan2(math.cos(alpha) - math.cos(beta),
                     d - math.sin(alpha) + math.sin(beta))
    return (_mod2pi(alpha - tmp), math.sqrt(max(0.0, p2)),
            _mod2pi(-beta + tmp))


def _lsr(alpha: float, beta: float, d: float):
    p2 = -2.0 + d * d + 2.0 * math.cos(alpha - beta) \
        + 2.0 * d * (math.sin(alpha) + math.sin(beta))
    if p2 < -1e-10:
        return None
    p = math.sqrt(max(0.0, p2))
    tmp = math.atan2(-math.cos(alpha) - math.cos(beta),
                     d + math.sin(alpha) + math.sin(beta)) \
        - math.atan2(-2.0, p)
    return (_mod2pi(-alpha + tmp), p,
            _mod2pi(-beta + tmp))


def _rsl(alpha: float, beta: float, d: float):
    p2 = d * d - 2.0 + 2.0 * math.cos(alpha - beta) \
        - 2.0 * d * (math.sin(alpha) + math.sin(beta))
    if p2 < -1e-10:
        return None
    p = math.sqrt(max(0.0, p2))
    tmp = math.atan2(math.cos(alpha) + math.cos(beta),
                     d - math.sin(alpha) - math.sin(beta)) \
        - math.atan2(2.0, p)
    return (_mod2pi(alpha - tmp), p,
            _mod2pi(beta - tmp))


def _rlr(alpha: float, beta: float, d: float):
    tmp = (6.0 - d * d + 2.0 * math.cos(alpha - beta)
           + 2.0 * d * (math.sin(alpha) - math.sin(beta))) / 8.0
    if abs(tmp) > 1.0 + 1e-10:
        return None
    p = _mod2pi(_TAU - math.acos(max(-1.0, min(1.0, tmp))))
    t = _mod2pi(alpha - math.atan2(
        math.cos(alpha) - math.cos(beta),
        d - math.sin(alpha) + math.sin(beta)) + p / 2.0)
    return t, p, _mod2pi(alpha - beta - t + p)


def _lrl(alpha: float, beta: float, d: float):
    tmp = (6.0 - d * d + 2.0 * math.cos(alpha - beta)
           + 2.0 * d * (-math.sin(alpha) + math.sin(beta))) / 8.0
    if abs(tmp) > 1.0 + 1e-10:
        return None
    p = _mod2pi(_TAU - math.acos(max(-1.0, min(1.0, tmp))))
    t = _mod2pi(-alpha - math.atan2(
        math.cos(alpha) - math.cos(beta),
        d + math.sin(alpha) - math.sin(beta)) + p / 2.0)
    return t, p, _mod2pi(beta - alpha - t + p)


_DUBINS_WORDS = (
    ("LSL", _lsl), ("RSR", _rsr), ("LSR", _lsr),
    ("RSL", _rsl), ("RLR", _rlr), ("LRL", _lrl),
)


def _advance(pose: Pose, kind: str, parameter: float, radius: float,
             chord_tolerance_m: float) -> Tuple[List[Point], Pose, Primitive]:
    x, y, yaw = pose
    points: List[Point] = [(x, y)]
    if kind == "S":
        length = parameter * radius
        end = (x + length * math.cos(yaw),
               y + length * math.sin(yaw), yaw)
        points.append((end[0], end[1]))
        return points, end, Primitive("S", pose, end, length, None)

    angle = max(float(parameter), 0.0)
    sign = 1.0 if kind == "L" else -1.0
    cx = x - sign * radius * math.sin(yaw)
    cy = y + sign * radius * math.cos(yaw)
    start_angle = math.atan2(y - cy, x - cx)
    step = _arc_step(radius, chord_tolerance_m)
    count = max(1, int(math.ceil(angle / max(step, 1e-6))))
    for index in range(1, count + 1):
        circle_angle = start_angle + sign * angle * index / count
        points.append((cx + radius * math.cos(circle_angle),
                       cy + radius * math.sin(circle_angle)))
    end_yaw = yaw + sign * angle
    end = (points[-1][0], points[-1][1], end_yaw)
    return points, end, Primitive(kind, pose, end, radius * angle, radius)


def dubins_candidates(start: Pose, end: Pose, radius_m: float,
                      chord_tolerance_m: float = 0.25
                      ) -> List[PathSolution]:
    """Every admissible six-word Dubins candidate between two poses."""
    radius = float(radius_m)
    if not math.isfinite(radius) or radius <= 0.0:
        raise PathGeometryError("Minimum turning radius must be greater than zero.")
    dx, dy = end[0] - start[0], end[1] - start[1]
    d = math.hypot(dx, dy) / radius
    theta = math.atan2(dy, dx) if d > _EPS else start[2]
    alpha = _mod2pi(start[2] - theta)
    beta = _mod2pi(end[2] - theta)
    out: List[PathSolution] = []
    for word, solver in _DUBINS_WORDS:
        params = solver(alpha, beta, d)
        if params is None:
            continue
        pose = start
        points: List[Point] = [(start[0], start[1])]
        primitives: List[Primitive] = []
        for kind, parameter in zip(word, params):
            sampled, pose, primitive = _advance(
                pose, kind, parameter, radius, chord_tolerance_m)
            points.extend(sampled[1:])
            primitives.append(primitive)
        # Analytic formulae and sampling accumulate tiny endpoint error.
        # Preserve the requested endpoint exactly without changing the
        # primitive semantics or introducing an extra straight segment.
        if points:
            points[-1] = (float(end[0]), float(end[1]))
        if primitives:
            last = primitives[-1]
            primitives[-1] = Primitive(
                last.kind, last.start, end, last.length_m, last.radius_m)
        out.append(PathSolution(
            points=points, primitives=primitives,
            waypoint_headings=[start[2], end[2]],
            length_m=sum(p.length_m for p in primitives),
            path_types=[word]))
    out.sort(key=lambda solution: (solution.length_m,
                                   solution.path_types[0]))
    return out


def shortest_dubins_path(start: Pose, end: Pose, radius_m: float,
                         chord_tolerance_m: float = 0.25) -> PathSolution:
    candidates = dubins_candidates(start, end, radius_m,
                                   chord_tolerance_m)
    if not candidates:
        raise PathGeometryError("No bounded-curvature path joins the two poses.")
    return candidates[0]


# -- reference scoring / waypoint heading optimization ----------------------

def _point_segment_distance(point: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    denom = dx * dx + dy * dy
    if denom <= _EPS:
        return distance(point, a)
    t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / denom
    t = min(max(t, 0.0), 1.0)
    return math.hypot(point[0] - (a[0] + t * dx),
                      point[1] - (a[1] + t * dy))


def point_polyline_distance(point: Point,
                            polyline: Sequence[Point]) -> float:
    if not polyline:
        return float("inf")
    if len(polyline) == 1:
        return distance(point, polyline[0])
    return min(_point_segment_distance(point, a, b)
               for a, b in zip(polyline, polyline[1:]))


def _ordered_point_offset(point: Point, reference: Sequence[Point],
                          previous_segment: int) -> Tuple[float, int]:
    """Nearest route segment around the prior ordered match.

    Generated paths and their reference RPL are both travel-ordered. Keeping
    a moving segment window makes offset scoring O(path vertices) for dense
    routes instead of O(path vertices x RPL vertices). The window expands
    whenever its best match touches an edge, retaining exactness for rapid
    progress across many short reference legs and allowing local turn-out /
    turn-in excursions to move backwards.
    """
    count = len(reference) - 1
    if count <= 0:
        return distance(point, reference[0]), 0
    center = min(max(int(previous_segment), 0), count - 1)
    lower = max(0, center - 24)
    upper = min(count, center + 65)  # exclusive
    while True:
        best_distance = float("inf")
        best_index = lower
        for index in range(lower, upper):
            value = _point_segment_distance(
                point, reference[index], reference[index + 1])
            if value < best_distance:
                best_distance, best_index = value, index
        expand_lower = lower > 0 and best_index <= lower + 1
        expand_upper = upper < count and best_index >= upper - 2
        if not expand_lower and not expand_upper:
            return best_distance, best_index
        span = max(upper - lower, 1)
        new_lower = max(0, lower - span) if expand_lower else lower
        new_upper = min(count, upper + span) if expand_upper else upper
        if new_lower == lower and new_upper == upper:
            return best_distance, best_index
        lower, upper = new_lower, new_upper


def path_offset_metrics(points: Sequence[Point], reference: Sequence[Point],
                        cancel: Optional[Callable[[], bool]] = None,
                        abort_above: Optional[float] = None
                        ) -> Tuple[float, float, float]:
    """Return maximum offset, integral(offset^2 ds), RMS offset.

    ``abort_above`` short-circuits candidate ranking: the maximum offset is
    the primary cost key, so a path already strictly worse than the best
    candidate so far cannot win and scoring the rest of it is wasted work.
    Returns ``(inf, inf, inf)`` when aborted.
    """
    if not points:
        return float("inf"), float("inf"), float("inf")
    offsets = []
    previous_segment = 0
    for index, point in enumerate(points):
        if cancel is not None and index % 128 == 0 and cancel():
            raise PathCancelled()
        offset, previous_segment = _ordered_point_offset(
            point, reference, previous_segment)
        if abort_above is not None and offset > abort_above:
            return float("inf"), float("inf"), float("inf")
        offsets.append(offset)
    maximum = max(offsets)
    integral = 0.0
    length = 0.0
    for i, (a, b) in enumerate(zip(points, points[1:])):
        ds = distance(a, b)
        integral += 0.5 * (offsets[i] ** 2 + offsets[i + 1] ** 2) * ds
        length += ds
    rms = math.sqrt(integral / length) if length > _EPS else maximum
    return maximum, integral, rms


def signed_offset_series(points: Sequence[Point],
                         reference: Sequence[Point]
                         ) -> List[Tuple[float, float]]:
    """(station_m along reference, signed cross-course offset) per point.

    Positive offsets lie to the LEFT of the reference travel direction
    (port when the reference is in travel order).  Uses the same moving
    segment window as :func:`path_offset_metrics`.
    """
    if len(reference) < 2:
        return []
    chainages = cumulative_lengths(reference)
    previous_segment = 0
    out: List[Tuple[float, float]] = []
    for point in points:
        offset, segment = _ordered_point_offset(point, reference,
                                                previous_segment)
        previous_segment = segment
        a, b = reference[segment], reference[segment + 1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        denom = dx * dx + dy * dy
        if denom <= _EPS:
            out.append((chainages[segment], 0.0))
            continue
        t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / denom
        t = min(max(t, 0.0), 1.0)
        qx, qy = a[0] + t * dx, a[1] + t * dy
        cross = dx * (point[1] - qy) - dy * (point[0] - qx)
        sign = 1.0 if cross > 0.0 else (-1.0 if cross < 0.0 else 0.0)
        station = chainages[segment] + t * distance(a, b)
        out.append((station, sign * offset))
    return out


def _unique_angles(values: Iterable[float], tolerance: float = 1e-7
                   ) -> List[float]:
    out: List[float] = []
    for value in values:
        angle = _mod2pi(value)
        if not any(abs(wrap_pi(angle - old)) <= tolerance for old in out):
            out.append(angle)
    return sorted(out)


def _bisector_heading(before: Point, at: Point, after: Point) -> float:
    incoming, outgoing = heading(before, at), heading(at, after)
    return incoming + 0.5 * wrap_pi(outgoing - incoming)


def _initial_heading_sets(waypoints: Sequence[Point], start_heading: float,
                          end_heading: float, step_deg: float
                          ) -> List[List[float]]:
    """Candidate headings per waypoint: a fan around the local direction.

    A route-following path's waypoint heading lies near the local route
    direction — turn-out/turn-in solutions deviate by up to ~90° plus the
    course change itself, never arbitrarily. Restricting the lattice to
    that fan (instead of the historical full circle) cuts the DP edge
    count roughly quadratically without excluding credible solutions; a
    near-reversal widens back to the full circle.
    """
    step = math.radians(min(max(float(step_deg), 3.0), 45.0))
    sets: List[List[float]] = [[_mod2pi(start_heading)]]
    for index in range(1, len(waypoints) - 1):
        h_in = heading(waypoints[index - 1], waypoints[index])
        h_out = heading(waypoints[index], waypoints[index + 1])
        bisector = _bisector_heading(waypoints[index - 1], waypoints[index],
                                     waypoints[index + 1])
        turn = abs(wrap_pi(h_out - h_in))
        half = min(math.pi, math.radians(90.0) + turn)
        count = int(math.ceil(half / step))
        fan = [bisector + offset * step
               for offset in range(-count, count + 1)]
        sets.append(_unique_angles(fan + [h_in, h_out, bisector]))
    sets.append([_mod2pi(end_heading)])
    return sets


def _refined_heading_sets(base: Sequence[Sequence[float]], chosen: Sequence[float],
                          coarse_step_deg: float, refine_step_deg: float
                          ) -> List[List[float]]:
    coarse = math.radians(max(float(coarse_step_deg), 3.0))
    fine = math.radians(min(max(float(refine_step_deg), 0.5),
                            max(float(coarse_step_deg), 3.0)))
    out: List[List[float]] = []
    for index, values in enumerate(base):
        if index == 0 or index == len(base) - 1:
            out.append(list(values))
            continue
        center = chosen[index]
        count = max(1, int(math.ceil(coarse / fine)))
        candidates = list(values[-3:])
        candidates.extend(center + offset * fine
                          for offset in range(-count, count + 1))
        out.append(_unique_angles(candidates))
    return out


def _edge_best(start: Pose, end: Pose, radius: float,
               reference: Sequence[Point], chord_tolerance_m: float,
               max_deviation_m: Optional[float],
               cancel: Optional[Callable[[], bool]] = None
               ) -> Optional[Tuple[Tuple, PathSolution]]:
    best = None
    direct = distance((start[0], start[1]), (end[0], end[1]))
    # A mathematical Dubins connection always exists, but paths containing
    # gratuitous revolutions are not credible burial-tool movements. The
    # broad length ceiling remains a final sanity bound after the explicit
    # per-arc / wrapped-heading credibility check below.
    length_ceiling = direct + 6.0 * math.pi * radius
    corridor = (float(max_deviation_m) + 1e-7
                if max_deviation_m is not None else None)
    for candidate in dubins_candidates(start, end, radius,
                                       chord_tolerance_m):
        if cancel is not None and cancel():
            raise PathCancelled()
        # A route course change is normalised to at most 180 degrees. A
        # Dubins edge that turns farther than that in one primitive, or takes
        # the wrapped long way between its headings, is orbiting merely to
        # hit poses that are too close for this radius. Reject it so the
        # compound control ladder can drop the tight A/C(s), report a
        # reviewed best fit, and avoid operationally impossible loops.
        signed_turn = 0.0
        credible = True
        for primitive in candidate.primitives:
            if primitive.kind not in ("L", "R"):
                continue
            angle = primitive.length_m / max(
                float(primitive.radius_m or radius), _EPS)
            if angle > math.pi + 1e-7:
                credible = False
                break
            signed_turn += angle if primitive.kind == "L" else -angle
        if not credible or abs(signed_turn) > math.pi + 1e-7:
            continue
        if candidate.length_m > length_ceiling + 1e-7:
            continue
        # Anything strictly beyond the best-so-far max (or the corridor)
        # cannot win; ties on the max still score fully so the integral
        # tie-break stays exact.
        abort_above = corridor
        if best is not None and (abort_above is None
                                 or best[0][0] < abort_above):
            abort_above = best[0][0]
        maximum, integral, rms = path_offset_metrics(
            candidate.points, reference, cancel, abort_above=abort_above)
        if not math.isfinite(maximum):
            continue
        if corridor is not None and maximum > corridor:
            continue
        candidate.max_offset_m = maximum
        candidate.rms_offset_m = rms
        # Stable, explainable priority: worst departure, area-like departure,
        # then path length and path word as a deterministic tie-break.
        cost = (maximum, integral, candidate.length_m,
                candidate.path_types[0])
        if best is None or cost < best[0]:
            best = (cost, candidate)
    return best


def _reference_legs(waypoints: Sequence[Point], reference: Sequence[Point]
                    ) -> List[List[Point]]:
    """Split an ordered reference at its waypoint controls.

    Compound controls are drawn from the reference itself. Scoring a Dubins
    edge only against its corresponding RPL leg avoids repeatedly scanning a
    whole dense corner cluster for every heading-lattice candidate.
    """
    if len(waypoints) < 2 or len(reference) < 2:
        return [list(reference)] * max(len(waypoints) - 1, 0)
    indices = [0]
    cursor = 0
    for control_index, control in enumerate(waypoints[1:-1], 1):
        # Leave at least one reference vertex for each remaining control and
        # the endpoint. In normal use the minimum is exactly zero.
        remaining = len(waypoints) - control_index - 1
        stop = max(cursor + 1, len(reference) - remaining)
        best = min(range(cursor, stop),
                   key=lambda index: distance(reference[index], control))
        indices.append(best)
        cursor = best
    indices.append(len(reference) - 1)
    legs: List[List[Point]] = []
    for index, (start, end) in enumerate(zip(indices, indices[1:])):
        piece = list(reference[start:end + 1])
        if not piece or distance(piece[0], waypoints[index]) > 1e-7:
            piece.insert(0, waypoints[index])
        if distance(piece[-1], waypoints[index + 1]) > 1e-7:
            piece.append(waypoints[index + 1])
        legs.append(clean_polyline(piece))
    return legs


def _resample_edge(edge: PathSolution, chord_tolerance_m: float
                   ) -> PathSolution:
    """Regenerate an edge's sampled points from its exact primitives.

    The heading-lattice search scores candidates at a coarse chord
    tolerance for speed; only the winning edges are resampled at the
    requested output tolerance. Endpoints stay exact.
    """
    if not edge.primitives:
        return edge
    pose = edge.primitives[0].start
    points: List[Point] = [(pose[0], pose[1])]
    primitives: List[Primitive] = []
    for primitive in edge.primitives:
        if primitive.kind == "S":
            parameter, radius = primitive.length_m, 1.0
        else:
            radius = float(primitive.radius_m or 1.0)
            parameter = primitive.length_m / max(radius, _EPS)
        sampled, pose, rebuilt = _advance(pose, primitive.kind, parameter,
                                          radius, chord_tolerance_m)
        points.extend(sampled[1:])
        primitives.append(rebuilt)
    end = edge.primitives[-1].end
    points[-1] = (float(end[0]), float(end[1]))
    if primitives:
        last = primitives[-1]
        primitives[-1] = Primitive(last.kind, last.start, end,
                                   last.length_m, last.radius_m)
    return PathSolution(
        points=points, primitives=primitives,
        waypoint_headings=list(edge.waypoint_headings),
        max_offset_m=edge.max_offset_m, rms_offset_m=edge.rms_offset_m,
        length_m=edge.length_m, path_types=list(edge.path_types))


def _predecessor_cannot_beat(prior_cost: Tuple,
                             incumbent_cost: Tuple,
                             direct_distance_m: float) -> bool:
    """Whether a DP predecessor is strictly worse than an incumbent.

    Edge maximum offset and integral are non-negative, and a bounded-
    curvature connection cannot be shorter than the direct endpoint span.
    These lower bounds let the heading lattice skip an edge solve only when
    it cannot change the selected state. Strict comparisons preserve full
    scoring of ties and therefore the existing deterministic tie-breaks.
    """
    if prior_cost[0] != incumbent_cost[0]:
        return prior_cost[0] > incumbent_cost[0]
    if prior_cost[1] != incumbent_cost[1]:
        return prior_cost[1] > incumbent_cost[1]
    length_floor = prior_cost[2] + direct_distance_m
    # Stay conservative around floating-point equality: an analytically
    # straight Dubins edge can accumulate a few ulps below its direct span.
    tolerance = 1e-9 * max(1.0, abs(length_floor),
                           abs(incumbent_cost[2]))
    return length_floor > incumbent_cost[2] + tolerance


def _solve_heading_lattice(waypoints: Sequence[Point],
                           heading_sets: Sequence[Sequence[float]],
                           radius: float, reference: Sequence[Point],
                           chord_tolerance_m: float,
                           max_deviation_m: Optional[float],
                           cancel: Optional[Callable[[], bool]] = None,
                           search_tolerance_m: Optional[float] = None,
                           leg_progress: Optional[
                               Callable[[int, int], None]] = None
                           ) -> PathSolution:
    search_tolerance = max(float(search_tolerance_m or chord_tolerance_m),
                           chord_tolerance_m)
    # states[index][heading_index] = (aggregate cost, previous index,
    # edge solution).  max offset combines with max; integral and length sum.
    states: List[Dict[int, Tuple[Tuple, Optional[int], Optional[PathSolution]]]] = [
        {0: ((0.0, 0.0, 0.0, ""), None, None)}
    ]
    edge_cache: Dict[Tuple[int, int, int], Optional[Tuple[Tuple, PathSolution]]] = {}
    leg_references = _reference_legs(waypoints, reference)
    for leg in range(len(waypoints) - 1):
        if cancel is not None and cancel():
            raise PathCancelled()
        if leg_progress is not None:
            leg_progress(leg, len(waypoints) - 1)
        current = states[-1]
        following: Dict[int, Tuple[Tuple, int, PathSolution]] = {}
        direct_distance = distance(waypoints[leg], waypoints[leg + 1])
        for to_index, to_heading in enumerate(heading_sets[leg + 1]):
            best_state = None
            for from_index, (prior_cost, _prev, _edge) in current.items():
                if best_state is not None and _predecessor_cannot_beat(
                        prior_cost, best_state[0], direct_distance):
                    continue
                key = (leg, from_index, to_index)
                edge = edge_cache.get(key)
                if key not in edge_cache:
                    edge = _edge_best(
                        (waypoints[leg][0], waypoints[leg][1],
                        heading_sets[leg][from_index]),
                        (waypoints[leg + 1][0], waypoints[leg + 1][1],
                        to_heading), radius, leg_references[leg],
                        search_tolerance, max_deviation_m, cancel)
                    edge_cache[key] = edge
                if edge is None:
                    continue
                edge_cost, solution = edge
                combined = (max(prior_cost[0], edge_cost[0]),
                            prior_cost[1] + edge_cost[1],
                            prior_cost[2] + edge_cost[2],
                            prior_cost[3] + edge_cost[3])
                candidate = (combined, from_index, solution)
                if best_state is None or candidate[0] < best_state[0]:
                    best_state = candidate
            if best_state is not None:
                following[to_index] = best_state
        if not following:
            raise PathGeometryError(
                "No bounded-curvature waypoint path fits the deviation envelope.")
        states.append(following)

    final_index = min(states[-1], key=lambda idx: states[-1][idx][0])
    edges: List[PathSolution] = []
    chosen_indices = [final_index]
    for layer in range(len(states) - 1, 0, -1):
        _cost, previous, edge = states[layer][chosen_indices[-1]]
        if edge is None or previous is None:
            raise PathGeometryError("The waypoint solver produced an incomplete path.")
        edges.append(edge)
        chosen_indices.append(previous)
    edges.reverse()
    chosen_indices.reverse()

    points: List[Point] = []
    primitives: List[Primitive] = []
    path_types: List[str] = []
    for edge in edges:
        if search_tolerance > chord_tolerance_m + 1e-9:
            edge = _resample_edge(edge, chord_tolerance_m)
        if not points:
            points.extend(edge.points)
        else:
            points.extend(edge.points[1:])
        primitives.extend(edge.primitives)
        path_types.extend(edge.path_types)
    maximum, _integral, rms = path_offset_metrics(points, reference, cancel)
    return PathSolution(
        points=points, primitives=primitives,
        waypoint_headings=[heading_sets[i][choice]
                           for i, choice in enumerate(chosen_indices)],
        max_offset_m=maximum, rms_offset_m=rms,
        length_m=sum(p.length_m for p in primitives),
        path_types=path_types)


def solve_waypoint_path(waypoints: Sequence[Point], radius_m: float,
                        start_heading: Optional[float] = None,
                        end_heading: Optional[float] = None,
                        reference: Optional[Sequence[Point]] = None,
                        max_deviation_m: Optional[float] = None,
                        heading_step_deg: float = 15.0,
                        refine_step_deg: float = 3.0,
                        chord_tolerance_m: float = 0.25,
                        cancel: Optional[Callable[[], bool]] = None,
                        progress: Optional[Callable[[int, int], None]] = None
                        ) -> PathSolution:
    """Bounded-curvature path through every ordered waypoint.

    Intermediate waypoint headings are free but shared by the incoming and
    outgoing Dubins legs.  A coarse lattice fanned around the local route
    direction is solved first, followed by a local refinement around the
    selected headings.  Candidates are scored on a coarse arc sampling
    (proportional to the radius); the winning edges are resampled at
    ``chord_tolerance_m`` so the output geometry keeps full fidelity.
    ``progress(done_units, total_units)`` reports DP legs across both the
    coarse and refinement passes.
    """
    controls = clean_polyline(waypoints)
    if len(controls) < 2:
        raise PathGeometryError("At least two distinct waypoints are required.")
    radius = float(radius_m)
    if not math.isfinite(radius) or radius <= 0.0:
        raise PathGeometryError("Minimum turning radius must be greater than zero.")
    start = heading(controls[0], controls[1]) \
        if start_heading is None else float(start_heading)
    end = heading(controls[-2], controls[-1]) \
        if end_heading is None else float(end_heading)
    route = clean_polyline(reference or controls)
    # Ranking candidates does not need millimetric arc sampling: a sagitta
    # around R/300 (capped at 3 m) keeps peaks visible while cutting the
    # dominant scoring cost for large radii several-fold.
    search_tolerance = max(chord_tolerance_m, min(3.0, radius / 300.0))
    headings = _initial_heading_sets(controls, start, end,
                                     heading_step_deg)
    legs = len(controls) - 1
    two_pass = len(controls) > 2 and refine_step_deg < heading_step_deg
    total_units = legs * (2 if two_pass else 1)

    def _pass_progress(offset: int):
        if progress is None:
            return None
        return lambda done, _total: progress(offset + done, total_units)

    coarse = _solve_heading_lattice(
        controls, headings, radius, route, chord_tolerance_m,
        max_deviation_m, cancel, search_tolerance_m=search_tolerance,
        leg_progress=_pass_progress(0))
    if not two_pass:
        return coarse
    refined = _refined_heading_sets(
        headings, coarse.waypoint_headings, heading_step_deg,
        refine_step_deg)
    try:
        return _solve_heading_lattice(
            controls, refined, radius, route, chord_tolerance_m,
            max_deviation_m, cancel, search_tolerance_m=search_tolerance,
            leg_progress=_pass_progress(legs))
    except PathGeometryError:
        return coarse


# -- complete route construction -------------------------------------------

@dataclass
class _Corner:
    number: int
    index: int
    station_m: float
    turn_rad: float
    tangent_m: float
    before_m: float
    after_m: float
    radius_m: float = 0.0
    fillet: Optional[Fillet] = None
    # Manual shaping control: an off-route point the path must pass through
    # (a user adjustment). ``None`` means the control is the route vertex.
    control: Optional[Point] = None
    adjustment: bool = False


@dataclass
class _Replacement:
    start_m: float
    end_m: float
    points: List[Point]
    primitives: List[Primitive]
    corner_numbers: List[int]
    solution: str
    max_offset_m: float
    solved_radius_m: float = 0.0
    # Per-corner distance from each member control to the final path.
    misses: Dict[int, float] = field(default_factory=dict)
    dropped: List[int] = field(default_factory=list)
    # Recovery paths deliberately relax route controls while preserving the
    # tool radius. ``wide_recovery`` is the final, off-route excursion used
    # only when the scoped route ends before a normal rejoin is possible.
    recovery: bool = False
    wide_recovery: bool = False
    corridor_relaxed: bool = False


def _route_corners(points: Sequence[Point], chainages: Sequence[float],
                   radius: float, chord_tolerance_m: float,
                   radius_for_vertex: Optional[Callable[[int, float], float]]
                   = None) -> List[_Corner]:
    corners: List[_Corner] = []
    for index in range(1, len(points) - 1):
        turn = wrap_pi(heading(points[index], points[index + 1])
                       - heading(points[index - 1], points[index]))
        # "All course changes" means only numerical collinearity is removed;
        # no event label or user threshold participates.
        if abs(turn) <= 1e-8:
            continue
        corner_radius = radius
        if radius_for_vertex is not None:
            corner_radius = float(radius_for_vertex(index, chainages[index]))
            if not math.isfinite(corner_radius) or corner_radius <= 0.0:
                raise PathGeometryError(
                    "A depth-based minimum turning radius must be finite "
                    "and greater than zero.")
        if math.pi - abs(turn) <= 1e-8:
            tangent = float("inf")
        else:
            tangent = corner_radius * math.tan(abs(turn) / 2.0)
        before = chainages[index] - chainages[index - 1]
        after = chainages[index + 1] - chainages[index]
        fillet = None
        if math.isfinite(tangent) and tangent < before - 1e-7 \
                and tangent < after - 1e-7:
            try:
                fillet = circular_fillet(
                    points[index - 1], points[index], points[index + 1],
                    corner_radius, chord_tolerance_m)
            except PathGeometryError:
                fillet = None
        corners.append(_Corner(
            len(corners) + 1, index, chainages[index], turn, tangent,
            before, after, corner_radius, fillet))
    return corners


def _cluster_corner_numbers(corners: Sequence[_Corner],
                            through: bool) -> List[List[int]]:
    if not corners:
        return []
    intervals = []
    for corner in corners:
        tangent = corner.tangent_m if math.isfinite(corner.tangent_m) \
            else 4.0 * corner.radius_m
        if through:
            pad = max(2.0 * corner.radius_m, tangent + corner.radius_m)
        else:
            pad = tangent
        intervals.append((corner.station_m - pad,
                          corner.station_m + pad))
    groups: List[List[int]] = [[0]]
    active_end = intervals[0][1]
    for index in range(1, len(corners)):
        # Fillets also interact whenever the two tangent demands consume the
        # complete leg between consecutive corners.
        previous = corners[index - 1]
        shared_leg = corners[index].station_m - previous.station_m
        tangent_overlap = (not through and (
            not math.isfinite(previous.tangent_m)
            or not math.isfinite(corners[index].tangent_m)
            or previous.tangent_m + corners[index].tangent_m
            >= shared_leg - 1e-7))
        if intervals[index][0] <= active_end + 1e-7 or tangent_overlap:
            groups[-1].append(index)
            active_end = max(active_end, intervals[index][1])
        else:
            groups.append([index])
            active_end = intervals[index][1]
    return groups


def _compound_window(corners: Sequence[_Corner], group: Sequence[int]
                     ) -> Tuple[float, float, float]:
    """(requested_start, requested_end, radius) for one compound cluster."""
    # A cluster with depth-varying corner radii is solved at the largest
    # member radius: the minimum-turn constraint is a lower bound, so the
    # widest requirement is the safe one for every corner it contains.
    radius = max(corners[index].radius_m for index in group)
    first, last = corners[group[0]], corners[group[-1]]
    first_tangent = first.tangent_m if math.isfinite(first.tangent_m) \
        else 3.0 * radius
    last_tangent = last.tangent_m if math.isfinite(last.tangent_m) \
        else 3.0 * radius
    return (first.station_m - max(2.0 * radius, first_tangent + radius),
            last.station_m + max(2.0 * radius, last_tangent + radius),
            radius)


def _control_point(route: Sequence[Point], corner: _Corner) -> Point:
    return corner.control if corner.control is not None \
        else route[corner.index]


def _control_ladder(members: Sequence[_Corner]) -> List[List[_Corner]]:
    """Progressively relaxed control subsets for the best-fit fallback.

    The first rung passes through every control (the exact objective).
    When that has no bounded-curvature solution the next rung drops only
    the corners that are individually infeasible at their radius (no
    tangent fillet fits their legs) — those are what usually break an
    exact-through solve, while the surviving gentle corners keep the path
    pinned to the RPL. Later rungs fall back to the historical racing
    line (largest course changes), manual adjustments only, and finally
    nothing but the entry/exit anchors.
    """
    ladder: List[List[_Corner]] = [list(members)]

    def _push(subset: List[_Corner]) -> None:
        if len(subset) < len(ladder[-1]) \
                and all(subset != rung for rung in ladder):
            ladder.append(subset)

    feasible = [c for c in members if c.adjustment or c.fillet is not None]
    if feasible:
        _push(feasible)
    if len(members) > 1:
        keep = max(1, (len(members) + 1) // 2)
        ranked = sorted(members,
                        key=lambda c: (not c.adjustment, -abs(c.turn_rad)))
        _push(sorted(ranked[:keep], key=lambda c: c.station_m))
    _push([c for c in members if c.adjustment])
    # Even manual shaping points are preferences once an exact solution is
    # impossible. The minimum radius and forward-only motion remain hard;
    # every missed control is retained in diagnostics for review.
    if ladder[-1]:
        ladder.append([])            # anchors only
    return ladder


def _has_credible_connection(start: Pose, end: Pose, radius: float) -> bool:
    """Whether a non-orbiting Dubins path joins two poses.

    Mirrors the credibility rule in :func:`_edge_best` (every arc at most a
    half revolution and no wrapped net turn) without any reference scoring,
    so a reachability probe costs microseconds.
    """
    for candidate in dubins_candidates(start, end, radius,
                                       chord_tolerance_m=radius):
        signed_turn = 0.0
        credible = True
        for primitive in candidate.primitives:
            if primitive.kind not in ("L", "R"):
                continue
            angle = primitive.length_m / max(
                float(primitive.radius_m or radius), _EPS)
            if angle > math.pi + 1e-7:
                credible = False
                break
            signed_turn += angle if primitive.kind == "L" else -angle
        if credible and abs(signed_turn) <= math.pi + 1e-7:
            return True
    return False


def _rejoin_stations(route: Sequence[Point], chainages: Sequence[float],
                     entry_point: Point, station_a: float, station_b: float,
                     radius: float, max_points: int = 4) -> List[float]:
    """On-route waypoint stations for one long control gap.

    The first station is found by scanning for the earliest point where a
    credible Dubins manoeuvre from the gap's entry pose can merge with the
    local route direction — placing it any earlier would make the whole
    (mandatory-waypoint) rung infeasible, any later wastes route adherence.
    Follow-up stations at a comfortable spacing keep the path pinned to the
    RPL across the remainder of the gap.
    """
    gap = float(station_b) - float(station_a)
    if gap <= 2.0 * radius:
        return []
    entry_heading = polyline_heading_at(route, chainages, station_a)
    pose = (float(entry_point[0]), float(entry_point[1]), entry_heading)
    end_target = point_at_distance(route, chainages, station_b)
    end_heading = polyline_heading_at(route, chainages,
                                      max(station_a, station_b - 1e-7))
    end_pose = (end_target[0], end_target[1], end_heading)
    # A slim end margin only: the continuation credibility check below is
    # what actually guarantees the tail edge works, and the best merge
    # station often sits close to the gap end.
    limit = station_b - 0.1 * radius

    fan_step = math.radians(15.0)

    def usable(station: float) -> bool:
        # A rejoin waypoint must be passable at SOME lattice-like heading:
        # reachable from the gap entry AND leaving a credible continuation
        # to the gap end. The heading need not be the route tangent — the
        # earliest physical rejoin is often a shallow crossing followed by
        # an arc onto the line, which the heading lattice can express as
        # two edges around one waypoint. Without the continuation check a
        # station still upstream of the excursion (trivially reachable
        # straight ahead) would be accepted and the impossible manoeuvre
        # merely deferred to the following edge.
        # Near-tangent band only (±30°): a realistic merge crosses or joins
        # the line shallowly. Steeper crossings are usually reachable much
        # earlier but force a taller excursion first, which the mandatory
        # waypoint would then lock in.
        target = point_at_distance(route, chainages, station)
        base = polyline_heading_at(route, chainages, station)
        # Only stations already pointing the gap-exit way are rejoin
        # candidates. A station on the entry side of the excursion (e.g.
        # still on a spur before a hairpin) is trivially reachable straight
        # ahead and would anchor the waypoint before the manoeuvre.
        if abs(wrap_pi(base - end_heading)) > math.radians(45.0):
            return False
        for k in (0, 1, -1, 2, -2):
            target_pose = (target[0], target[1], base + k * fan_step)
            if _has_credible_connection(pose, target_pose, radius) \
                    and _has_credible_connection(target_pose, end_pose,
                                                 radius):
                return True
        return False

    step = max(radius / 4.0, gap / 64.0)
    first = None
    station = station_a + step
    while station < limit:
        if usable(station):
            first = station
            break
        station += step
    if first is None:
        return []
    # The coarse step means ``first`` sits up to one step past the true
    # earliest rejoin — deliberate margin so the mandatory waypoint stays
    # comfortably inside the reachable set for the lattice's discrete fan.
    out = [first]
    station = first + 2.5 * radius
    while station < limit and len(out) < int(max_points):
        out.append(station)
        station += 2.5 * radius
    return out


def _controls_with_rejoin(route: Sequence[Point], chainages: Sequence[float],
                          subset: Sequence[_Corner], start: float, end: float,
                          anchor_start: Point, anchor_end: Point,
                          radius: float) -> Optional[List[Point]]:
    """Control list augmented with on-route rejoin waypoints.

    A single Dubins edge is one arc-straight-arc: after a forced turn-out
    excursion it can only crawl back to a distant anchor along one long
    diagonal. Mandatory waypoints placed ON the reference inside long
    control gaps let the heading lattice rejoin the RPL early and then
    follow it, which is how a real plough/trencher would be driven.
    Returns ``None`` when every gap is short enough not to matter.
    """
    entries: List[Tuple[float, Point]] = [(float(start), anchor_start)]
    for corner in subset:
        entries.append((min(max(corner.station_m, float(start)), float(end)),
                        _control_point(route, corner)))
    entries.append((float(end), anchor_end))
    controls: List[Point] = [anchor_start]
    added = False
    for (station_a, point_a), (station_b, point_b) in zip(entries,
                                                          entries[1:]):
        for station in _rejoin_stations(route, chainages, point_a,
                                        station_a, station_b, radius):
            controls.append(point_at_distance(route, chainages, station))
            added = True
        controls.append(point_b)
    return controls if added else None


def _wide_recovery_solution(
        anchor_start: Point, anchor_end: Point,
        start_heading: float, end_heading: float, radius: float,
        reference: Sequence[Point], chord_tolerance_m: float,
        cancel: Optional[Callable[[], bool]] = None) -> PathSolution:
    """Return a non-orbiting excursion that reaches a too-close endpoint.

    Some finite route scopes are shorter than the minimum-radius vehicle
    needs to reverse or acquire the terminal heading. A direct Dubins edge
    can then contain a near-complete circle. Instead, place one temporary
    control well outside the tight area and solve two individually credible
    edges. This produces a wide, reviewable recovery manoeuvre which still
    honours the radius and rejoins the exact route endpoint and heading.
    """
    direct = distance(anchor_start, anchor_end)
    start_forward = (math.cos(start_heading), math.sin(start_heading))
    start_left = (-start_forward[1], start_forward[0])
    end_forward = (math.cos(end_heading), math.sin(end_heading))
    end_left = (-end_forward[1], end_forward[0])

    def shifted(origin: Point, along: Point, along_m: float,
                across: Point = (0.0, 0.0), across_m: float = 0.0) -> Point:
        return (origin[0] + along[0] * along_m + across[0] * across_m,
                origin[1] + along[1] * along_m + across[1] * across_m)

    # The first successful scale is used so a pathological short scope does
    # not choose a needlessly enormous recovery merely to reduce a secondary
    # score by a small amount.
    for factor in (2.0, 4.0, 8.0, 16.0):
        if cancel is not None and cancel():
            raise PathCancelled()
        advance = direct + factor * radius
        lateral = factor * radius
        candidates = [
            shifted(anchor_start, start_forward, advance),
            shifted(anchor_start, start_forward, advance,
                    start_left, lateral),
            shifted(anchor_start, start_forward, advance,
                    start_left, -lateral),
            shifted(anchor_end, end_forward, -advance),
            shifted(anchor_end, end_forward, -advance,
                    end_left, lateral),
            shifted(anchor_end, end_forward, -advance,
                    end_left, -lateral),
        ]
        best = None
        seen = set()
        for waypoint in candidates:
            key = (round(waypoint[0], 7), round(waypoint[1], 7))
            if key in seen:
                continue
            seen.add(key)
            try:
                candidate = solve_waypoint_path(
                    [anchor_start, waypoint, anchor_end], radius,
                    start_heading=start_heading, end_heading=end_heading,
                    reference=reference, max_deviation_m=None,
                    chord_tolerance_m=chord_tolerance_m, cancel=cancel)
            except PathGeometryError:
                continue
            score = (candidate.max_offset_m, candidate.length_m,
                     waypoint[0], waypoint[1])
            if best is None or score < best[0]:
                best = (score, candidate)
        if best is not None:
            return best[1]
    raise PathGeometryError(
        "No forward-only minimum-radius recovery reaches the scoped route "
        "endpoint without an orbit.")


def _compound_replacement(route: Sequence[Point], chainages: Sequence[float],
                          corners: Sequence[_Corner], group: Sequence[int],
                          chord_tolerance_m: float,
                          max_deviation_m: Optional[float],
                          start: float, end: float,
                          cancel: Optional[Callable[[], bool]] = None,
                          fraction_progress: Optional[
                              Callable[[float], None]] = None,
                          recovery: bool = False,
                          allow_wide_recovery: bool = False
                          ) -> _Replacement:
    _rs, _re, radius = _compound_window(corners, group)
    anchor_start = point_at_distance(route, chainages, start)
    anchor_end = point_at_distance(route, chainages, end)
    members = [corners[index] for index in group]
    reference = polyline_slice(route, chainages, start, end)
    start_heading = polyline_heading_at(route, chainages, start)
    end_heading = polyline_heading_at(route, chainages,
                                      max(start, end - 1e-7))
    solution = None
    passed: set = set()
    corridor_relaxed = False
    wide_recovery = False
    ladder = _control_ladder(members)
    if recovery and len(members) > 4:
        # A widened recovery may cover many future course changes. Retrying
        # all of them would recreate the large heading lattice that the
        # chunking optimisation avoids. Prefer the individually feasible
        # controls (they keep the path on the RPL after the excursion),
        # then the four most important, then the anchors alone.
        feasible = [corner for corner in members
                    if corner.adjustment or corner.fillet is not None][:8]
        selected = sorted(
            sorted(members,
                   key=lambda corner: (not corner.adjustment,
                                       -abs(corner.turn_rad)))[:4],
            key=lambda corner: corner.station_m)
        ladder = []
        if feasible:
            ladder.append(feasible)
        if selected and selected != feasible:
            ladder.append(selected)
        ladder.append([])
    limits = [max_deviation_m]
    if max_deviation_m is not None and max_deviation_m > 0.0:
        limits.append(None)
    for limit in limits:
        for subset in ladder:
            # Each relaxed rung is solved both plain and with on-route
            # rejoin waypoints in its long control gaps (early rejoin +
            # line following, which one arc-straight-arc Dubins edge can
            # never express). The better result by the solver's own cost
            # order wins, so augmentation can never worsen a rung.
            # Exact-through (nothing dropped, no recovery) keeps its
            # historical unconstrained turn-out freedom.
            plain = [anchor_start]
            plain.extend(_control_point(route, corner) for corner in subset)
            plain.append(anchor_end)
            variants: List[List[Point]] = [plain]
            if recovery or len(subset) < len(members):
                augmented = _controls_with_rejoin(
                    route, chainages, subset, start, end,
                    anchor_start, anchor_end, radius)
                if augmented is not None:
                    variants.insert(0, augmented)
            best = None
            for controls in variants:
                try:
                    candidate = solve_waypoint_path(
                        controls, radius, start_heading=start_heading,
                        end_heading=end_heading, reference=reference,
                        max_deviation_m=limit,
                        chord_tolerance_m=chord_tolerance_m, cancel=cancel,
                        progress=(None if fraction_progress is None else
                                  lambda done, total:
                                  fraction_progress(done / max(total, 1))))
                except PathGeometryError:
                    continue
                key = (candidate.max_offset_m, candidate.rms_offset_m,
                       candidate.length_m)
                if best is None or key < best[0]:
                    best = (key, candidate)
            if best is not None:
                solution = best[1]
                passed = {corner.number for corner in subset}
                corridor_relaxed = limit is None and max_deviation_m is not None
                break
        if solution is not None:
            break
    if solution is None and allow_wide_recovery:
        solution = _wide_recovery_solution(
            anchor_start, anchor_end, start_heading, end_heading, radius,
            reference, chord_tolerance_m, cancel)
        passed = set()
        corridor_relaxed = bool(max_deviation_m)
        wide_recovery = True
    if solution is None:
        raise PathGeometryError(
            "No direct non-looping bounded-curvature path fits the turn "
            f"cluster near station {corners[group[0]].station_m:.0f} m, "
            "even after relaxing its course-change controls. A farther "
            f"RPL rejoin is required for the {radius:g} m turning radius.")
    misses: Dict[int, float] = {}
    dropped: List[int] = []
    for corner in members:
        miss = point_polyline_distance(_control_point(route, corner),
                                       solution.points)
        # Controls the solver passed through are hit exactly (they are
        # Dubins endpoints); keep those at a clean 0.0.
        misses[corner.number] = 0.0 \
            if corner.number in passed and miss <= 1e-6 else miss
        if corner.number not in passed:
            dropped.append(corner.number)
    kind = "compound" if not (dropped or recovery or wide_recovery
                               or corridor_relaxed) else "best_fit"
    return _Replacement(
        start, end, solution.points, solution.primitives,
        [corner.number for corner in members], kind,
        solution.max_offset_m, radius, misses, dropped,
        recovery, wide_recovery, corridor_relaxed)


def _adjustment_corners(route: Sequence[Point], chainages: Sequence[float],
                        extra_controls: Sequence[Tuple[float, Point]],
                        radius: float,
                        radius_for_vertex: Optional[
                            Callable[[int, float], float]] = None
                        ) -> List[_Corner]:
    """Manual shaping controls as pseudo-corners (turn 0, always compound)."""
    out: List[_Corner] = []
    for raw_station, raw_point in extra_controls or []:
        station = min(max(float(raw_station), 0.0), float(chainages[-1]))
        point = (float(raw_point[0]), float(raw_point[1]))
        index = min(max(bisect.bisect_left(chainages, station), 1),
                    len(route) - 2) if len(route) > 2 else 1
        corner_radius = radius
        if radius_for_vertex is not None:
            corner_radius = float(radius_for_vertex(index, station))
            if not math.isfinite(corner_radius) or corner_radius <= 0.0:
                raise PathGeometryError(
                    "A depth-based minimum turning radius must be finite "
                    "and greater than zero.")
        out.append(_Corner(
            0, index, station, 0.0, corner_radius,
            station, chainages[-1] - station, corner_radius,
            None, control=point, adjustment=True))
    return out


def _split_oversized(plan_items: List[Dict], corners: Sequence[_Corner],
                     max_corners: int) -> List[Dict]:
    """Split any turn group with too many corners into bounded chunks.

    A single mega-cluster (dense course changes at a large radius) makes
    the heading-lattice solve intractable — the pass-through "stuck at a
    few percent" failure.  Chunks share forced boundaries at the midpoint
    of the gap between their edge corners; both sides anchor to the route
    heading there, so the stitched path stays tangent-continuous.
    """
    out: List[Dict] = []
    for item in plan_items:
        group = item["group"]
        if len(group) <= max_corners:
            out.append(item)
            continue
        chunks = [group[i:i + max_corners]
                  for i in range(0, len(group), max_corners)]
        for k, chunk in enumerate(chunks):
            piece = {"group": chunk, "start": None, "end": None}
            if k == 0:
                piece["start"] = item.get("start")
            else:
                piece["start"] = 0.5 * (
                    corners[chunks[k - 1][-1]].station_m
                    + corners[chunk[0]].station_m)
            if k == len(chunks) - 1:
                piece["end"] = item.get("end")
            else:
                piece["end"] = 0.5 * (
                    corners[chunk[-1]].station_m
                    + corners[chunks[k + 1][0]].station_m)
            out.append(piece)
    return out


def generate_route_path(route_points: Sequence[Point], radius_m: float,
                        mode: str = "fillet",
                        max_deviation_m: Optional[float] = None,
                        chord_tolerance_m: float = 0.25,
                        cancel: Optional[Callable[[], bool]] = None,
                        progress: Optional[Callable[[int, int], None]] = None,
                        radius_for_vertex: Optional[
                            Callable[[int, float], float]] = None,
                        extra_controls: Optional[
                            Sequence[Tuple[float, Point]]] = None,
                        max_cluster_size: int = 8
                        ) -> RoutePathResult:
    """Generate a radius-constrained path over every route course change.

    ``fillet`` uses local tangent arcs where they fit.  Interacting/short-leg
    corners are solved as one exact-through compound cluster.  ``through_ac``
    uses the compound solver for every corner (the historical name is kept in
    persisted configs, but all geometry course changes are controls).

    Robustness contract: a turn cluster crowded by its neighbour or the
    route ends merges with the crowding group instead of failing. A cluster
    with no credible non-looping exact-through solution degrades to a
    best-fit path that drops the infeasible corners first, merges back onto
    the RPL at the earliest credible station (reachability-scanned on-route
    rejoin waypoints), progressively skips further controls, and widens its
    RPL rejoin downstream. If the finite route ends before that is possible, one
    wide non-orbiting recovery excursion returns to the exact endpoint. The
    configured deviation is a review threshold in these fallbacks, never a
    reason to suppress the most credible path that can be generated.

    ``progress(done_groups, total_groups)`` is called before each turn group
    is solved and once after the last — compound clusters dominate the wall
    time, so this is the callback a UI progress bar should follow.
    ``radius_for_vertex(vertex_index, station_m)`` supplies a per-corner
    minimum radius (e.g. banded by water depth); ``radius_m`` remains the
    scalar default and validation floor.
    ``extra_controls`` are manual shaping points ``(station_m, (x, y))`` the
    path must additionally pass through (user path adjustments); each forces
    a compound solve around its station.  ``max_cluster_size`` bounds the
    corners solved in one heading lattice; larger clusters are chunked.
    """
    route = clean_polyline(route_points)
    if len(route) < 2:
        raise PathGeometryError("The scoped route needs at least two points.")
    radius = float(radius_m)
    if not math.isfinite(radius) or radius <= 0.0:
        raise PathGeometryError("Minimum turning radius must be greater than zero.")
    if max_deviation_m is not None:
        max_deviation_m = float(max_deviation_m)
        if not math.isfinite(max_deviation_m) or max_deviation_m <= 0.0:
            max_deviation_m = None
    chainages = cumulative_lengths(route)
    corners = _route_corners(route, chainages, radius,
                             chord_tolerance_m, radius_for_vertex)
    corners.extend(_adjustment_corners(route, chainages, extra_controls or [],
                                       radius, radius_for_vertex))
    corners.sort(key=lambda corner: corner.station_m)
    for number, corner in enumerate(corners, 1):
        corner.number = number
    if not corners:
        return RoutePathResult(
            points=list(route), length_m=chainages[-1],
            max_offset_m=0.0, rms_offset_m=0.0,
            course_change_count=0)

    through = mode in ("through_ac", "through", "pass_through")
    plan_items: List[Dict] = [
        {"group": group, "start": None, "end": None}
        for group in _cluster_corner_numbers(corners, through)]
    plan_items = _split_oversized(plan_items, corners,
                                  max(2, int(max_cluster_size)))
    replacements: List[_Replacement] = []
    compound_count = 0
    index = 0
    while index < len(plan_items):
        if cancel is not None and cancel():
            raise PathCancelled()
        if progress is not None:
            progress(index, len(plan_items))
        item = plan_items[index]
        group = item["group"]
        use_compound = through or len(group) > 1 \
            or corners[group[0]].fillet is None \
            or any(corners[member].adjustment for member in group)
        if not use_compound:
            corner = corners[group[0]]
            fillet = corner.fillet
            if fillet is None:
                raise PathGeometryError("A route corner could not be filleted.")
            replacements.append(_Replacement(
                corner.station_m - fillet.tangent_distance_m,
                corner.station_m + fillet.tangent_distance_m,
                fillet.points, [fillet.primitive], [corner.number],
                "fillet", fillet.miss_distance_m, corner.radius_m))
            index += 1
            continue

        previous_end = replacements[-1].end_m if replacements else 0.0
        if item.get("start") is not None:
            previous_end = max(previous_end, float(item["start"]))
        if item.get("end") is not None:
            next_start = float(item["end"])
        elif index + 1 < len(plan_items):
            following = corners[plan_items[index + 1]["group"][0]]
            following_pad = max(2.0 * following.radius_m,
                                (following.tangent_m
                                 if math.isfinite(following.tangent_m)
                                 else 3.0 * following.radius_m)
                                + following.radius_m)
            next_start = max(previous_end,
                             following.station_m - following_pad)
        else:
            next_start = chainages[-1]
        requested_start, requested_end, group_radius = \
            _compound_window(corners, group)
        start = max(0.0, requested_start, previous_end)
        forced_rejoin = item.get("rejoin_end")
        end = (min(chainages[-1], float(forced_rejoin))
               if forced_rejoin is not None
               else min(chainages[-1], requested_end, next_start))
        # The window must contain every member corner: a neighbour's large
        # entry pad can otherwise push ``end`` before this cluster's own
        # stations, a trivially solvable anchor-to-anchor window "succeeds",
        # and the excluded corners are stitched back in as RAW RPL — a
        # silent minimum-radius violation.
        uncovered_by_previous = start > corners[group[0]].station_m + 1e-6
        uncovered_by_next = end < corners[group[-1]].station_m - 1e-6
        if end - start <= max(group_radius * 0.1, 1e-6) \
                or uncovered_by_previous or uncovered_by_next:
            # The cluster is crowded.  Merge with whichever neighbour is
            # doing the crowding and re-solve the union as one cluster —
            # the "not enough route either side" failure becomes a bigger
            # compound solve instead of an error.
            crowded_by_previous = index > 0 and item.get("start") is None \
                and (previous_end > requested_start + 1e-9
                     or uncovered_by_previous)
            crowded_by_next = index + 1 < len(plan_items) \
                and item.get("end") is None \
                and (next_start < requested_end - 1e-9
                     or uncovered_by_next)
            if crowded_by_previous:
                previous_item = plan_items.pop(index - 1)
                if replacements:
                    replacements.pop()
                item["group"] = previous_item["group"] + group
                item["start"] = previous_item.get("start")
                index -= 1
            elif crowded_by_next:
                next_item = plan_items.pop(index + 1)
                item["group"] = group + next_item["group"]
                item["end"] = next_item.get("end")
            elif end > start + 1e-6:
                # Route ends bind on both sides (a short scoped route):
                # solve with whatever window exists; the best-fit ladder
                # keeps this from failing outright.
                pass
            else:
                raise PathGeometryError(
                    f"The scoped route around station "
                    f"{corners[group[0]].station_m:.0f} m is shorter than "
                    f"the {group_radius:g} m turning radius allows — there "
                    "is no usable route length to fit a path. Reduce the "
                    "radius or extend the plan scope.")
            if crowded_by_previous or crowded_by_next:
                if len(item["group"]) > max(2, int(max_cluster_size)):
                    merged = _split_oversized([item], corners,
                                              max(2, int(max_cluster_size)))
                    plan_items[index:index + 1] = merged
                continue

        fraction_progress = None
        if progress is not None:
            def fraction_progress(fraction: float, _index=index) -> None:
                total = len(plan_items)
                progress(min(_index + max(0.0, min(fraction, 1.0)),
                             total), total)
        try:
            replacement = _compound_replacement(
                route, chainages, corners, group,
                chord_tolerance_m, max_deviation_m,
                start, end, cancel, fraction_progress,
                recovery=bool(item.get("recovery")),
                allow_wide_recovery=(
                    index + 1 >= len(plan_items)
                    and end >= chainages[-1] - 1e-7))
        except PathGeometryError:
            if index + 1 < len(plan_items):
                # The original exit pose is too close for a credible direct
                # connection. Absorb the next planned group and retry with
                # its later exit; skipped controls remain review diagnostics.
                following_item = plan_items.pop(index + 1)
                item["group"] = group + following_item["group"]
                item["end"] = following_item.get("end")
                item["recovery"] = True
                continue
            if end < chainages[-1] - 1e-7:
                # No later turn group exists, but straight/collinear RPL may
                # remain. Search progressively farther along it before using
                # the terminal off-route excursion.
                advance = max(2.0 * group_radius, end - start, 1.0)
                item["rejoin_end"] = min(chainages[-1], end + advance)
                item["recovery"] = True
                continue
            raise
        replacements.append(replacement)
        compound_count += 1
        index += 1

    if progress is not None:
        progress(len(plan_items), len(plan_items))

    # Stitch untouched RPL pieces and replacements in travel order.
    points: List[Point] = []
    primitives: List[Primitive] = []
    cursor = 0.0
    for replacement in replacements:
        untouched = polyline_slice(route, chainages, cursor,
                                   replacement.start_m)
        if untouched:
            points.extend(untouched if not points else untouched[1:])
        points.extend(replacement.points if not points
                      else replacement.points[1:])
        primitives.extend(replacement.primitives)
        cursor = replacement.end_m
    tail = polyline_slice(route, chainages, cursor, chainages[-1])
    if tail:
        points.extend(tail if not points else tail[1:])
    points = clean_polyline(points)
    maximum, _integral, rms = path_offset_metrics(points, route, cancel)

    replacement_by_corner = {}
    for replacement in replacements:
        for number in replacement.corner_numbers:
            replacement_by_corner[number] = replacement
    diagnostics: List[RoutePathDiagnostic] = []
    for corner in corners:
        replacement = replacement_by_corner[corner.number]
        dropped = corner.number in replacement.dropped
        if replacement.solution == "fillet":
            miss = corner.fillet.miss_distance_m if corner.fillet else 0.0
            message = "Tangent circular fillet."
        else:
            miss = replacement.misses.get(corner.number, 0.0)
            if corner.number in replacement.dropped:
                # A dropped control can still end up close to (or on) the
                # final path once the untouched RPL pieces are stitched
                # around its window — report the real distance, not the
                # distance to the truncated window solution.
                miss = min(miss, point_polyline_distance(
                    _control_point(route, corner), points))
                replacement.misses[corner.number] = miss
            if corner.adjustment:
                message = (f"Manual path adjustment; the path passes "
                           f"{miss:.2f} m from the requested point.")
            elif replacement.solution == "best_fit":
                if replacement.wide_recovery:
                    message = (
                        "The scoped route ends before a direct non-looping "
                        f"rejoin is possible at {replacement.solved_radius_m:g} "
                        "m radius. A wide minimum-radius recovery excursion "
                        f"rejoins the exact endpoint; vertex miss {miss:.2f} m.")
                elif replacement.recovery:
                    message = (
                        "The original rejoin window was too tight for a "
                        f"credible {replacement.solved_radius_m:g} m-radius "
                        "path. The recovery "
                        + ("skips conflicting controls and "
                           if replacement.dropped
                           else "retains the feasible controls and ")
                        + ("rejoins farther along the RPL; vertex miss "
                           f"{miss:.2f} m."))
                else:
                    message = (
                        "The course changes in this cluster are too tightly "
                        "spaced for a credible non-looping path to pass through "
                        f"every point at {replacement.solved_radius_m:g} m "
                        f"— best-fit path shown; vertex miss {miss:.2f} m.")
                if dropped:
                    message += " This point was dropped as an exact control."
                if replacement.corridor_relaxed:
                    message += (
                        " The configured route-deviation limit was exceeded "
                        "and is reported as a review threshold.")
            else:
                message = ("Compound bounded-curvature solution through "
                           "every course-change point in the cluster, "
                           f"solved at {replacement.solved_radius_m:g} m "
                           "(the largest radius requirement among its "
                           "corners).")
        corridor_review = bool(
            max_deviation_m is not None and max_deviation_m > 0.0
            and replacement.max_offset_m > max_deviation_m + 1e-6)
        if replacement.solution == "best_fit" or corridor_review:
            status = "review"
        elif max_deviation_m is None or max_deviation_m <= 0.0:
            status = "review" if replacement.solution == "compound" else "ok"
        else:
            status = "ok"
        side = "" if corner.adjustment else \
            ("port" if corner.turn_rad > 0.0 else "starboard")
        if corridor_review and "review threshold" not in message:
            message += (
                f" The {max_deviation_m:g} m route-deviation limit was "
                "exceeded and is reported as a review threshold.")
        diagnostics.append(RoutePathDiagnostic(
            corner.number, corner.index, corner.station_m,
            math.degrees(corner.turn_rad), side,
            "adjustment" if corner.adjustment else replacement.solution,
            miss, replacement.max_offset_m,
            status, message, corner.radius_m,
            "adjustment" if corner.adjustment else "corner",
            replacement.recovery, replacement.wide_recovery,
            replacement.corridor_relaxed or corridor_review))
    return RoutePathResult(
        points=points, primitives=primitives,
        max_offset_m=maximum, rms_offset_m=rms,
        length_m=polyline_length(points), diagnostics=diagnostics,
        course_change_count=len(corners),
        compound_cluster_count=compound_count,
        path_types=[replacement.solution for replacement in replacements])


# -- layback ----------------------------------------------------------------

def interpolate_profile(points: Sequence[Tuple[float, float]], value: float,
                        outside: str = "error") -> float:
    """Piecewise-linear profile with explicit outside-range behaviour."""
    prepared = sorted((float(x), float(y)) for x, y in points)
    if not prepared:
        raise PathGeometryError("The layback profile has no points.")
    xs = [item[0] for item in prepared]
    if len(set(xs)) != len(xs):
        raise PathGeometryError("Layback profile depths must be unique.")
    x = float(value)
    if x < xs[0] - _EPS:
        if outside == "hold":
            return prepared[0][1]
        raise PathGeometryError(
            f"Water depth {x:g} m is below the layback profile range.")
    if x > xs[-1] + _EPS:
        if outside == "hold":
            return prepared[-1][1]
        raise PathGeometryError(
            f"Water depth {x:g} m is above the layback profile range.")
    index = bisect.bisect_left(xs, x)
    if index < len(xs) and abs(xs[index] - x) <= _EPS:
        return prepared[index][1]
    if index <= 0:
        return prepared[0][1]
    x0, y0 = prepared[index - 1]
    x1, y1 = prepared[index]
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def vertex_tangents(points: Sequence[Point]) -> List[Point]:
    """Unit forward tangent per point using centred differences."""
    clean = clean_polyline(points)
    if len(clean) < 2:
        raise PathGeometryError("A track needs at least two distinct points.")
    tangents: List[Point] = []
    for index in range(len(clean)):
        if index == 0:
            h = heading(clean[0], clean[1])
        elif index == len(clean) - 1:
            h = heading(clean[-2], clean[-1])
        else:
            h0 = heading(clean[index - 1], clean[index])
            h1 = heading(clean[index], clean[index + 1])
            sx, sy = math.cos(h0) + math.cos(h1), \
                math.sin(h0) + math.sin(h1)
            h = math.atan2(sy, sx) if math.hypot(sx, sy) > _EPS else h1
        tangents.append((math.cos(h), math.sin(h)))
    return tangents


def layback_track(tool_path: Sequence[Point], laybacks_m: Sequence[float]
                  ) -> List[Point]:
    """Tow-point track B(s)=T(s)+L(s)t(s) for a planned tool path."""
    points = clean_polyline(tool_path)
    if len(points) != len(laybacks_m):
        raise PathGeometryError(
            "Tool-path points and layback values must have the same length.")
    tangents = vertex_tangents(points)
    out: List[Point] = []
    for point, tangent, raw in zip(points, tangents, laybacks_m):
        layback = float(raw)
        if not math.isfinite(layback) or layback < 0.0:
            raise PathGeometryError("Layback values must be finite and non-negative.")
        out.append((point[0] + layback * tangent[0],
                    point[1] + layback * tangent[1]))
    return out


def minimum_polyline_radius(points: Sequence[Point]) -> Optional[float]:
    """Smallest three-point circumradius; None for a wholly straight line."""
    clean = clean_polyline(points)
    best: Optional[float] = None
    for a, b, c in zip(clean, clean[1:], clean[2:]):
        ab, bc, ca = distance(a, b), distance(b, c), distance(c, a)
        cross = abs((b[0] - a[0]) * (c[1] - a[1])
                    - (b[1] - a[1]) * (c[0] - a[0]))
        if cross <= 1e-8 * max(ab * bc, 1.0):
            continue
        radius = ab * bc * ca / (2.0 * cross)
        if math.isfinite(radius) and radius > 0.0:
            best = radius if best is None else min(best, radius)
    return best
