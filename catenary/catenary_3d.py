# -*- coding: utf-8 -*-
"""3D/static multi-span helpers for Catenary Calculator V2.

This module deliberately stays independent of QGIS and of the existing V2 dialog
so the new maths can be tested and iterated safely before it is exposed in the
UI.  Scope is quasi-static only: 3D projection, fixed-end uniform catenary spans,
point-body span equilibrium, seabed-contact reporting, and chute capstan friction
bounds.  Time-domain dynamics, current and hydrodynamic drag belong in the future
Cable Lay Simulator module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable, List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


_EPS = 1e-9


@dataclass(frozen=True)
class Point3D:
    """Simple 3D point/vector.

    Coordinate convention:
        x = Easting/local east (m)
        y = Northing/local north (m)
        z = elevation relative to sea level, positive upward (m)
    """

    x: float
    y: float
    z: float

    def __add__(self, other: "Point3D") -> "Point3D":
        return Point3D(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Point3D") -> "Point3D":
        return Point3D(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, factor: float) -> "Point3D":
        return Point3D(self.x * factor, self.y * factor, self.z * factor)

    __rmul__ = __mul__

    def dot(self, other: "Point3D") -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def norm(self) -> float:
        return math.sqrt(self.dot(self))

    def horizontal_norm(self) -> float:
        return math.hypot(self.x, self.y)

    def as_tuple(self) -> Tuple[float, float, float]:
        return (float(self.x), float(self.y), float(self.z))


def _unit_horizontal_vector(start: Point3D, end: Point3D) -> Tuple[Point3D, float]:
    dx = float(end.x - start.x)
    dy = float(end.y - start.y)
    Lh = math.hypot(dx, dy)
    if Lh <= _EPS:
        return Point3D(0.0, 0.0, 0.0), 0.0
    return Point3D(dx / Lh, dy / Lh, 0.0), Lh


def _solve_catenary_a(horizontal_distance_m: float, vertical_delta_m: float, length_m: float) -> float:
    """Solve the standard inextensible catenary parameter ``a = H/q``.

    The fixed-end, fixed-length catenary relation is::

        sqrt(S² - h²) = 2a sinh(L / 2a)

    where ``L`` is horizontal endpoint separation, ``h`` is vertical endpoint
    separation and ``S`` is arc length.
    """
    L = float(horizontal_distance_m)
    h = float(vertical_delta_m)
    S = float(length_m)

    if L <= _EPS:
        raise ValueError("Horizontal distance is too small for the standard catenary solve.")
    straight = math.hypot(L, h)
    if S <= straight + 1e-8:
        raise ValueError(
            f"Span length {S:.3f} m is not longer than the straight-line endpoint distance "
            f"{straight:.3f} m; an inextensible hanging catenary is impossible."
        )

    B2 = S * S - h * h
    if B2 <= L * L:
        raise ValueError("Invalid span geometry: sqrt(S^2-h^2) must exceed horizontal distance.")
    B = math.sqrt(B2)

    def f(a: float) -> float:
        u = L / (2.0 * a)
        # Very small a can overflow sinh; in that limit f is certainly positive.
        if u > 50:
            return float("inf")
        return 2.0 * a * math.sinh(u) - B

    lo = min(L, S) / 1e6
    hi = max(L, S, 1.0)
    while f(hi) > 0.0:
        hi *= 2.0
        if hi > 1e12:
            raise ValueError("Could not bracket catenary parameter a.")

    for _ in range(120):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if abs(fm) < 1e-10:
            return mid
        if fm > 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


@dataclass
class SpanSolution3D:
    name: str
    start: Point3D
    end: Point3D
    length_m: float
    q_npm: float
    horizontal_tension_N: float
    start_tension_N: float
    end_tension_N: float
    force_on_start_N: Point3D
    force_on_end_N: Point3D
    min_radius_m: float
    points: List[Point3D] = field(default_factory=list)
    s_m: List[float] = field(default_factory=list)
    tension_N: List[float] = field(default_factory=list)


def solve_uniform_catenary_span_3d(
    name: str,
    start: Point3D,
    end: Point3D,
    length_m: float,
    q_npm: float,
    samples: int = 80,
) -> SpanSolution3D:
    """Solve a static uniform-weight catenary between two fixed 3D endpoints.

    The catenary is solved in the vertical plane through the two endpoints, then
    mapped back to 3D. ``q_npm`` is effective submerged/in-air weight per metre
    acting downward; this first version assumes ``q_npm > 0``.

    Returned endpoint forces are the forces applied *by the cable* to the
    endpoint/body/support.
    """
    S = float(length_m)
    q = float(q_npm)
    if q <= 0:
        raise ValueError("This uniform catenary span solver currently requires q_npm > 0.")
    if S <= 0:
        raise ValueError("Span length must be positive.")

    e_h, L = _unit_horizontal_vector(start, end)
    dz = float(end.z - start.z)
    straight = math.hypot(L, dz)
    if S <= straight + 1e-8:
        raise ValueError(
            f"Span '{name}' length {S:.3f} m is not longer than straight-line distance "
            f"{straight:.3f} m."
        )

    # Near-vertical special case. This is uncommon for cable lay, but avoids
    # numerical singularities if a body is almost directly above an anchor.
    if L <= 1e-6:
        H = 0.0
        # For a vertical hanging line, the upper endpoint carries the line's
        # submerged weight. This approximation assumes the end point is above
        # the start point, which is the normal mid-water-body case.
        T_end = q * S if dz >= 0 else 0.0
        T_start = q * S - T_end
        force_on_end = Point3D(0.0, 0.0, -T_end if dz >= 0 else T_end)
        force_on_start = Point3D(0.0, 0.0, T_start if dz >= 0 else -T_start)
        pts = [
            Point3D(start.x, start.y, start.z + dz * i / max(1, samples - 1))
            for i in range(max(2, samples))
        ]
        return SpanSolution3D(
            name=name,
            start=start,
            end=end,
            length_m=S,
            q_npm=q,
            horizontal_tension_N=H,
            start_tension_N=abs(T_start),
            end_tension_N=abs(T_end),
            force_on_start_N=force_on_start,
            force_on_end_N=force_on_end,
            min_radius_m=float("inf"),
            points=pts,
            s_m=[S * i / max(1, len(pts) - 1) for i in range(len(pts))],
            tension_N=[abs(T_start + (T_end - T_start) * i / max(1, len(pts) - 1)) for i in range(len(pts))],
        )

    a = _solve_catenary_a(L, dz, S)
    B = math.sqrt(S * S - dz * dz)
    m = math.asinh(dz / B)
    x_c = 0.5 * L - a * m
    H = q * a

    def z_at(x: float) -> float:
        return start.z + a * (
            math.cosh((x - x_c) / a) - math.cosh((0.0 - x_c) / a)
        )

    def s_at(x: float) -> float:
        return a * (
            math.sinh((x - x_c) / a) - math.sinh((0.0 - x_c) / a)
        )

    def V_at(x: float) -> float:
        return H * math.sinh((x - x_c) / a)

    V0 = V_at(0.0)
    V1 = V_at(L)
    T0 = math.hypot(H, V0)
    T1 = math.hypot(H, V1)

    force_on_start = Point3D(H * e_h.x, H * e_h.y, V0)
    force_on_end = Point3D(-H * e_h.x, -H * e_h.y, -V1)

    n = max(2, int(samples))
    pts: List[Point3D] = []
    ss: List[float] = []
    tensions: List[float] = []
    min_radius = float("inf")
    for i in range(n):
        x_local = L * i / (n - 1)
        z = z_at(x_local)
        p = Point3D(
            start.x + e_h.x * x_local,
            start.y + e_h.y * x_local,
            z,
        )
        V = V_at(x_local)
        T = math.hypot(H, V)
        radius = (T * T) / max(abs(q * H), 1e-12)
        min_radius = min(min_radius, radius)
        pts.append(p)
        ss.append(s_at(x_local))
        tensions.append(T)

    return SpanSolution3D(
        name=name,
        start=start,
        end=end,
        length_m=S,
        q_npm=q,
        horizontal_tension_N=H,
        start_tension_N=T0,
        end_tension_N=T1,
        force_on_start_N=force_on_start,
        force_on_end_N=force_on_end,
        min_radius_m=min_radius,
        points=pts,
        s_m=ss,
        tension_N=tensions,
    )


def project_2d_catenary_to_3d(
    x_along_m: Sequence[float],
    z_m: Sequence[float],
    origin: Point3D = Point3D(0.0, 0.0, 0.0),
    bearing_deg: float = 0.0,
) -> List[Point3D]:
    """Project a solved 2D catenary into 3D along a compass bearing.

    ``x_along_m`` is distance along the vertical catenary plane. ``bearing_deg``
    follows navigation convention: 0° = north/+y, 90° = east/+x.
    """
    if len(x_along_m) != len(z_m):
        raise ValueError("x_along_m and z_m must have the same length.")
    brg = math.radians(float(bearing_deg))
    east = math.sin(brg)
    north = math.cos(brg)
    return [
        Point3D(
            origin.x + float(x) * east,
            origin.y + float(x) * north,
            origin.z + float(z),
        )
        for x, z in zip(x_along_m, z_m)
    ]


@dataclass
class ChuteFrictionResult:
    contact_tension_kN: float
    wrap_angle_deg: float
    friction_coefficient: float
    capstan_ratio: float
    top_tension_if_top_side_high_kN: float
    top_tension_if_top_side_low_kN: float
    tension_difference_kN: float


def chute_friction_bounds(
    contact_tension_kN: float,
    wrap_angle_deg: float,
    friction_coefficient: float,
) -> ChuteFrictionResult:
    """Return capstan-equation tension bounds across a chute contact arc.

    ``T_high / T_low = exp(mu * theta)``. Direction of sliding decides which side
    is high; this function deliberately returns both interpretations so the UI
    can label them for pay-out/recovery conventions.
    """
    T = float(contact_tension_kN)
    mu = max(0.0, float(friction_coefficient))
    theta = max(0.0, math.radians(float(wrap_angle_deg)))
    ratio = math.exp(mu * theta)
    top_high = T * ratio
    top_low = T / ratio if ratio > 0 else T
    return ChuteFrictionResult(
        contact_tension_kN=T,
        wrap_angle_deg=math.degrees(theta),
        friction_coefficient=mu,
        capstan_ratio=ratio,
        top_tension_if_top_side_high_kN=top_high,
        top_tension_if_top_side_low_kN=top_low,
        tension_difference_kN=top_high - top_low,
    )


@dataclass
class SeabedContactPoint:
    index: int
    station_m: float
    point: Point3D
    seabed_depth_m: float
    clearance_m: float


@dataclass
class SeabedContactInterval:
    start_index: int
    end_index: int
    start_station_m: float
    end_station_m: float
    min_clearance_m: float


@dataclass
class SeabedContactReport:
    tolerance_m: float
    penetration_tolerance_m: float
    min_clearance_m: float
    first_touch: Optional[SeabedContactPoint]
    contact_points: List[SeabedContactPoint]
    contact_intervals: List[SeabedContactInterval]
    penetration_intervals: List[SeabedContactInterval]
    warnings: List[str] = field(default_factory=list)


def seabed_contact_report(
    points: Sequence[Point3D],
    seabed_depth_at_xy: Callable[[float, float], float],
    tolerance_m: float = 0.25,
    penetration_tolerance_m: float = 0.5,
) -> SeabedContactReport:
    """Classify cable/seabed contacts for a 3D curve.

    ``clearance = seabed_depth + z`` where ``z`` is positive upward and seabed
    depth is positive downward from sea level. Positive clearance means cable is
    above the seabed; negative means the solved free-span curve passes below it.
    """
    pts = list(points)
    if not pts:
        raise ValueError("At least one point is required.")

    stations = [0.0]
    for a, b in zip(pts[:-1], pts[1:]):
        stations.append(stations[-1] + (b - a).norm())

    clearances: List[float] = []
    bed_depths: List[float] = []
    for p in pts:
        d = float(seabed_depth_at_xy(float(p.x), float(p.y)))
        bed_depths.append(d)
        clearances.append(d + float(p.z))

    def make_point(i: int) -> SeabedContactPoint:
        return SeabedContactPoint(
            index=i,
            station_m=stations[i],
            point=pts[i],
            seabed_depth_m=bed_depths[i],
            clearance_m=clearances[i],
        )

    contact_idx = [i for i, c in enumerate(clearances) if c <= tolerance_m]
    penetration_idx = [i for i, c in enumerate(clearances) if c < -abs(penetration_tolerance_m)]

    def intervals(indices: List[int]) -> List[SeabedContactInterval]:
        if not indices:
            return []
        out: List[SeabedContactInterval] = []
        start = prev = indices[0]
        for idx in indices[1:]:
            if idx == prev + 1:
                prev = idx
                continue
            out.append(
                SeabedContactInterval(
                    start_index=start,
                    end_index=prev,
                    start_station_m=stations[start],
                    end_station_m=stations[prev],
                    min_clearance_m=min(clearances[start:prev + 1]),
                )
            )
            start = prev = idx
        out.append(
            SeabedContactInterval(
                start_index=start,
                end_index=prev,
                start_station_m=stations[start],
                end_station_m=stations[prev],
                min_clearance_m=min(clearances[start:prev + 1]),
            )
        )
        return out

    contact_points = [make_point(i) for i in contact_idx]
    min_clear = min(clearances)
    report = SeabedContactReport(
        tolerance_m=float(tolerance_m),
        penetration_tolerance_m=float(penetration_tolerance_m),
        min_clearance_m=float(min_clear),
        first_touch=contact_points[0] if contact_points else None,
        contact_points=contact_points,
        contact_intervals=intervals(contact_idx),
        penetration_intervals=intervals(penetration_idx),
    )
    if report.penetration_intervals:
        report.warnings.append(
            "Solved free-span geometry penetrates the seabed. Treat this as a multi-contact problem; "
            "the static single-span catenary should be split at seabed supports before using tensions/MBR."
        )
    if len(report.contact_intervals) > 1:
        report.warnings.append(
            "Multiple apparent seabed contact regions detected; a single TDP assumption is not sufficient."
        )
    return report


@dataclass
class BodySpanConnection:
    name: str
    fixed_point: Point3D
    length_m: float
    q_npm: float


@dataclass
class BodyEquilibriumResult:
    body_position: Point3D
    residual_force_N: Point3D
    residual_norm_N: float
    span_solutions: List[SpanSolution3D]
    iterations: int
    converged: bool
    warnings: List[str] = field(default_factory=list)


def evaluate_body_equilibrium(
    body_position: Point3D,
    spans: Sequence[BodySpanConnection],
    submerged_weight_N: float,
    samples_per_span: int = 60,
) -> BodyEquilibriumResult:
    """Evaluate force balance at a point body connected to catenary spans.

    ``submerged_weight_N`` is positive downward and negative for net buoyancy.
    """
    span_solutions: List[SpanSolution3D] = []
    force = Point3D(0.0, 0.0, -float(submerged_weight_N))
    for sp in spans:
        sol = solve_uniform_catenary_span_3d(
            name=sp.name,
            start=sp.fixed_point,
            end=body_position,
            length_m=sp.length_m,
            q_npm=sp.q_npm,
            samples=samples_per_span,
        )
        span_solutions.append(sol)
        force = force + sol.force_on_end_N

    return BodyEquilibriumResult(
        body_position=body_position,
        residual_force_N=force,
        residual_norm_N=force.norm(),
        span_solutions=span_solutions,
        iterations=0,
        converged=False,
    )


def solve_body_equilibrium(
    initial_body_position: Point3D,
    spans: Sequence[BodySpanConnection],
    submerged_weight_N: float,
    tolerance_N: float = 25.0,
    max_iterations: int = 35,
    finite_difference_step_m: float = 0.25,
    samples_per_span: int = 60,
) -> BodyEquilibriumResult:
    """Solve a 3D point-body equilibrium connected to catenary spans.

    This is intentionally a compact quasi-static Newton solver. It is suitable
    as a first engineering tool for repeaters, clump weights, buoyancy bodies or
    splice bodies where the body can be approximated as a point. It is not a
    time-domain dynamics solver and does not include current/drag.
    """
    if np is None:
        raise ImportError("NumPy is required for solve_body_equilibrium.")
    if not spans:
        raise ValueError("At least one span connection is required.")

    p = initial_body_position
    warnings: List[str] = []

    def residual_at(pos: Point3D) -> np.ndarray:
        r = evaluate_body_equilibrium(
            pos,
            spans,
            submerged_weight_N=submerged_weight_N,
            samples_per_span=samples_per_span,
        ).residual_force_N
        return np.array([r.x, r.y, r.z], dtype=float)

    for iteration in range(1, max_iterations + 1):
        try:
            F = residual_at(p)
        except Exception as exc:
            raise ValueError(f"Could not evaluate body equilibrium at iteration {iteration}: {exc}")

        norm = float(np.linalg.norm(F))
        if norm <= tolerance_N:
            result = evaluate_body_equilibrium(p, spans, submerged_weight_N, samples_per_span)
            result.iterations = iteration
            result.converged = True
            result.warnings.extend(warnings)
            return result

        J = np.zeros((3, 3), dtype=float)
        h = float(finite_difference_step_m)
        perturbations = [
            Point3D(h, 0.0, 0.0),
            Point3D(0.0, h, 0.0),
            Point3D(0.0, 0.0, h),
        ]

        for col, dp in enumerate(perturbations):
            try:
                Fp = residual_at(p + dp)
                Fm = residual_at(p - dp)
                J[:, col] = (Fp - Fm) / (2.0 * h)
            except Exception:
                # One-sided fallback near the edge of feasible span geometry.
                Fp = residual_at(p + dp)
                J[:, col] = (Fp - F) / h

        try:
            delta = np.linalg.solve(J, -F)
        except Exception:
            delta, *_ = np.linalg.lstsq(J, -F, rcond=None)
            warnings.append("Body equilibrium Jacobian was singular/ill-conditioned; used least-squares step.")

        # Damped Newton: reduce the step until the residual improves and the
        # span geometry remains feasible.
        accepted = False
        step_scale = 1.0
        for _ in range(12):
            trial = Point3D(
                p.x + float(delta[0]) * step_scale,
                p.y + float(delta[1]) * step_scale,
                p.z + float(delta[2]) * step_scale,
            )
            try:
                F_trial = residual_at(trial)
                if float(np.linalg.norm(F_trial)) < norm:
                    p = trial
                    accepted = True
                    break
            except Exception:
                pass
            step_scale *= 0.5

        if not accepted:
            warnings.append("Newton step could not improve residual; returning best effort.")
            break

    result = evaluate_body_equilibrium(p, spans, submerged_weight_N, samples_per_span)
    result.iterations = max_iterations
    result.converged = result.residual_norm_N <= tolerance_N
    result.warnings.extend(warnings)
    if not result.converged:
        result.warnings.append(
            f"Point-body equilibrium did not converge to {tolerance_N:.1f} N; "
            f"residual is {result.residual_norm_N:.1f} N."
        )
    return result
