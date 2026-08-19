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
    out.extend(point for point, station in zip(points, chainages)
               if lo + _EPS < station < hi - _EPS)
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
    progress across many short reference legs and allowing local Dubins loops
    to move backwards.
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
                        cancel: Optional[Callable[[], bool]] = None
                        ) -> Tuple[float, float, float]:
    """Return maximum offset, integral(offset^2 ds), RMS offset."""
    if not points:
        return float("inf"), float("inf"), float("inf")
    offsets = []
    previous_segment = 0
    for index, point in enumerate(points):
        if cancel is not None and index % 128 == 0 and cancel():
            raise PathCancelled()
        offset, previous_segment = _ordered_point_offset(
            point, reference, previous_segment)
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
    step = math.radians(min(max(float(step_deg), 3.0), 45.0))
    uniform = [i * step for i in range(max(1, int(math.ceil(_TAU / step))))]
    sets: List[List[float]] = [[_mod2pi(start_heading)]]
    for index in range(1, len(waypoints) - 1):
        local = [heading(waypoints[index - 1], waypoints[index]),
                 heading(waypoints[index], waypoints[index + 1]),
                 _bisector_heading(waypoints[index - 1], waypoints[index],
                                   waypoints[index + 1])]
        sets.append(_unique_angles(uniform + local))
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
    # multiple gratuitous revolutions are not credible route fits.  Allow up
    # to 6piR beyond the direct span so very close controls keep a usable
    # local solution while runaway multi-loop candidates are discarded.
    length_ceiling = direct + 6.0 * math.pi * radius
    for candidate in dubins_candidates(start, end, radius,
                                       chord_tolerance_m):
        if cancel is not None and cancel():
            raise PathCancelled()
        if candidate.length_m > length_ceiling + 1e-7:
            continue
        maximum, integral, rms = path_offset_metrics(
            candidate.points, reference, cancel)
        if max_deviation_m is not None \
                and maximum > float(max_deviation_m) + 1e-7:
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


def _solve_heading_lattice(waypoints: Sequence[Point],
                           heading_sets: Sequence[Sequence[float]],
                           radius: float, reference: Sequence[Point],
                           chord_tolerance_m: float,
                           max_deviation_m: Optional[float],
                           cancel: Optional[Callable[[], bool]] = None
                           ) -> PathSolution:
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
        current = states[-1]
        following: Dict[int, Tuple[Tuple, int, PathSolution]] = {}
        for to_index, to_heading in enumerate(heading_sets[leg + 1]):
            best_state = None
            for from_index, (prior_cost, _prev, _edge) in current.items():
                key = (leg, from_index, to_index)
                edge = edge_cache.get(key)
                if key not in edge_cache:
                    edge = _edge_best(
                        (waypoints[leg][0], waypoints[leg][1],
                        heading_sets[leg][from_index]),
                        (waypoints[leg + 1][0], waypoints[leg + 1][1],
                        to_heading), radius, leg_references[leg],
                        chord_tolerance_m, max_deviation_m, cancel)
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
                        cancel: Optional[Callable[[], bool]] = None
                        ) -> PathSolution:
    """Bounded-curvature path through every ordered waypoint.

    Intermediate waypoint headings are free but shared by the incoming and
    outgoing Dubins legs.  A coarse full-circle lattice is solved first,
    followed by a local refinement around the selected headings.
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
    headings = _initial_heading_sets(controls, start, end,
                                     heading_step_deg)
    coarse = _solve_heading_lattice(
        controls, headings, radius, route, chord_tolerance_m,
        max_deviation_m, cancel)
    if len(controls) <= 2 or refine_step_deg >= heading_step_deg:
        return coarse
    refined = _refined_heading_sets(
        headings, coarse.waypoint_headings, heading_step_deg,
        refine_step_deg)
    try:
        return _solve_heading_lattice(
            controls, refined, radius, route, chord_tolerance_m,
            max_deviation_m, cancel)
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


def _compound_replacement(route: Sequence[Point], chainages: Sequence[float],
                          corners: Sequence[_Corner], group: Sequence[int],
                          chord_tolerance_m: float,
                          max_deviation_m: Optional[float],
                          previous_end: float, next_start: float,
                          cancel: Optional[Callable[[], bool]] = None
                          ) -> _Replacement:
    # A cluster with depth-varying corner radii is solved at the largest
    # member radius: the minimum-turn constraint is a lower bound, so the
    # widest requirement is the safe one for every corner it contains.
    radius = max(corners[index].radius_m for index in group)
    first, last = corners[group[0]], corners[group[-1]]
    first_tangent = first.tangent_m if math.isfinite(first.tangent_m) \
        else 3.0 * radius
    last_tangent = last.tangent_m if math.isfinite(last.tangent_m) \
        else 3.0 * radius
    requested_start = first.station_m - max(2.0 * radius,
                                             first_tangent + radius)
    requested_end = last.station_m + max(2.0 * radius,
                                          last_tangent + radius)
    # Do not consume an adjacent independent replacement: clamping to the
    # neighbours' boundaries keeps replacements ordered and disjoint while
    # granting this cluster the whole remaining lead-in/lead-out.
    start = max(0.0, requested_start, previous_end)
    end = min(chainages[-1], requested_end, next_start)
    if end - start <= max(radius * 0.1, 1e-6):
        raise PathGeometryError(
            "There is not enough route either side of a turn cluster to "
            "establish entry and exit tangents.")
    anchor_start = point_at_distance(route, chainages, start)
    anchor_end = point_at_distance(route, chainages, end)
    controls = [anchor_start]
    controls.extend(route[corners[index].index] for index in group)
    controls.append(anchor_end)
    reference = polyline_slice(route, chainages, start, end)
    solution = solve_waypoint_path(
        controls, radius,
        start_heading=polyline_heading_at(route, chainages, start),
        end_heading=polyline_heading_at(
            route, chainages, max(start, end - 1e-7)),
        reference=reference, max_deviation_m=max_deviation_m,
        chord_tolerance_m=chord_tolerance_m, cancel=cancel)
    return _Replacement(
        start, end, solution.points, solution.primitives,
        [corners[index].number for index in group], "compound",
        solution.max_offset_m, radius)


def generate_route_path(route_points: Sequence[Point], radius_m: float,
                        mode: str = "fillet",
                        max_deviation_m: Optional[float] = None,
                        chord_tolerance_m: float = 0.25,
                        cancel: Optional[Callable[[], bool]] = None,
                        progress: Optional[Callable[[int, int], None]] = None,
                        radius_for_vertex: Optional[
                            Callable[[int, float], float]] = None
                        ) -> RoutePathResult:
    """Generate a radius-constrained path over every route course change.

    ``fillet`` uses local tangent arcs where they fit.  Interacting/short-leg
    corners are solved as one exact-through compound cluster.  ``through_ac``
    uses the compound solver for every corner (the historical name is kept in
    persisted configs, but all geometry course changes are controls).

    ``progress(done_groups, total_groups)`` is called before each turn group
    is solved and once after the last — compound clusters dominate the wall
    time, so this is the callback a UI progress bar should follow.
    ``radius_for_vertex(vertex_index, station_m)`` supplies a per-corner
    minimum radius (e.g. banded by water depth); ``radius_m`` remains the
    scalar default and validation floor.
    """
    route = clean_polyline(route_points)
    if len(route) < 2:
        raise PathGeometryError("The scoped route needs at least two points.")
    radius = float(radius_m)
    if not math.isfinite(radius) or radius <= 0.0:
        raise PathGeometryError("Minimum turning radius must be greater than zero.")
    chainages = cumulative_lengths(route)
    corners = _route_corners(route, chainages, radius,
                             chord_tolerance_m, radius_for_vertex)
    if not corners:
        return RoutePathResult(
            points=list(route), length_m=chainages[-1],
            max_offset_m=0.0, rms_offset_m=0.0,
            course_change_count=0)

    through = mode in ("through_ac", "through", "pass_through")
    groups = _cluster_corner_numbers(corners, through)
    replacements: List[_Replacement] = []
    compound_count = 0
    for group_index, group in enumerate(groups):
        if cancel is not None and cancel():
            raise PathCancelled()
        if progress is not None:
            progress(group_index, len(groups))
        use_compound = through or len(group) > 1 \
            or corners[group[0]].fillet is None
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
            continue

        compound_count += 1
        previous_end = replacements[-1].end_m if replacements else 0.0
        if group_index + 1 < len(groups):
            following = corners[groups[group_index + 1][0]]
            following_pad = max(2.0 * following.radius_m,
                                (following.tangent_m
                                 if math.isfinite(following.tangent_m)
                                 else 3.0 * following.radius_m)
                                + following.radius_m)
            next_start = max(previous_end,
                             following.station_m - following_pad)
        else:
            next_start = chainages[-1]
        replacements.append(_compound_replacement(
            route, chainages, corners, group,
            chord_tolerance_m, max_deviation_m,
            previous_end, next_start, cancel))

    if progress is not None:
        progress(len(groups), len(groups))

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
    if max_deviation_m is not None and max_deviation_m > 0.0 \
            and maximum > max_deviation_m + 1e-6:
        raise PathGeometryError(
            f"The best path departs {maximum:.1f} m from the RPL, exceeding "
            f"the {max_deviation_m:.1f} m maximum.")

    replacement_by_corner = {}
    for replacement in replacements:
        for number in replacement.corner_numbers:
            replacement_by_corner[number] = replacement
    diagnostics: List[RoutePathDiagnostic] = []
    for corner in corners:
        replacement = replacement_by_corner[corner.number]
        if replacement.solution == "fillet":
            miss = corner.fillet.miss_distance_m if corner.fillet else 0.0
            message = "Tangent circular fillet."
        else:
            miss = 0.0
            message = ("Compound bounded-curvature solution through every "
                       "course-change point in the cluster, solved at "
                       f"{replacement.solved_radius_m:g} m (the largest "
                       "radius requirement among its corners).")
        status = "ok"
        if max_deviation_m is None or max_deviation_m <= 0.0:
            status = "review" if replacement.solution == "compound" else "ok"
        diagnostics.append(RoutePathDiagnostic(
            corner.number, corner.index, corner.station_m,
            math.degrees(corner.turn_rad),
            "port" if corner.turn_rad > 0.0 else "starboard",
            replacement.solution, miss, replacement.max_offset_m,
            status, message, corner.radius_m))
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
