# -*- coding: utf-8 -*-
"""Inverse BU-deployment planner: schedule from bottom-tension targets.

Pure Python + NumPy; no Qt/QGIS imports.

The forward simulators (full / quick) take a schedule of vessel moves and
payout and *report* the resulting leg tensions. This module inverts that,
matching how a deployment is actually specified: hold each leg's touchdown
(bottom) tension at a target while the BU descends, and land it on a target
position. It uses the same closed-form tangent-catenary primitives as the
quick model, so a plan solves in milliseconds.

The controllable degrees of freedom after the BU is overboarded are the
vessel position and the trunk payout — the legs' deployed lengths are
frozen (their tops hang from the BU). Holding both legs at target bottom
tension therefore works like this:

* the required leg lengths are fixed by the landing: each leg must exactly
  cover the seabed path from its laid end to the landing target, so at
  touchdown the residual tension equals the held target;
* at each BU depth, a leg's bottom tension depends only on the plan range
  from its laid end to the BU (tangent catenary + length closure), so the
  target tension fixes that range — the BU plan position is the
  intersection of two circles about the laid ends;
* the horizontal resultant of the two leg tensions fixes the direction the
  trunk must pull — the lay-away course — and its magnitude, from which
  the trunk catenary gives the vessel stand-off and suspended length; the
  payout schedule is the growth of that suspended length.

Assumptions match the quick model: uniform per-chain weight, no drag or
current, straight plan-line bed runs, tangency at every touchdown. Verify
a planned schedule by running it through the simulator (quick, then full).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from .quick_bu import tangent_catenary
from .scenarios import PhaseRow


def _bed_z(bathy, x: float, y: float) -> float:
    return -float(bathy.depth_at(x, y))


def bed_path_length(bathy, a_xy: Tuple[float, float],
                    b_xy: Tuple[float, float], ds: float = 10.0) -> float:
    """3D length of the seabed along the straight plan line a -> b."""
    ax, ay = float(a_xy[0]), float(a_xy[1])
    bx, by = float(b_xy[0]), float(b_xy[1])
    plan = math.hypot(bx - ax, by - ay)
    n = max(2, int(math.ceil(plan / max(ds, 1.0))) + 1)
    ts = np.linspace(0.0, 1.0, n)
    xs = ax + (bx - ax) * ts
    ys = ay + (by - ay) * ts
    zs = -np.asarray(bathy.depth_at(xs, ys), dtype=float)
    pts = np.column_stack([xs, ys, zs])
    return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))


def _leg_geometry(R: float, h: float, L: float, w: float
                  ) -> Optional[Tuple[float, float, float]]:
    """Tangent-catenary state of a leg of length ``L`` whose hang point is
    ``h`` above a flat bed at plan range ``R`` from its anchor.

    Solves the closure ``t + s(D = R - t) = L`` for the bed-run length
    ``t`` (same construction as the quick model's ``leg_solution``).
    Returns ``(H, s_susp, D)`` or None when the leg cannot reach tangency
    (too short — it would lift off the bed).
    """
    if h <= 1e-3:
        return (0.0, 0.0, 0.0)

    def closure(t: float) -> float:
        D = max(0.0, R - t)
        _H, s, _T = tangent_catenary(D, h, w)
        return t + s - L

    if closure(0.0) > 0.0:
        return None                    # cannot touch down tangentially
    lo, hi = 0.0, min(R, L)
    if closure(hi) <= 0.0:
        t = hi                          # surplus pools at the hang point
    else:
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if closure(mid) <= 0.0:
                lo = mid
            else:
                hi = mid
        t = 0.5 * (lo + hi)
    D = max(0.0, R - t)
    H, s, _T = tangent_catenary(D, h, w)
    return H, s, D


def _range_for_tension(H_target: float, h: float, L: float, w: float
                       ) -> Tuple[Optional[float], float]:
    """Plan range R from the anchor at which the leg's bottom tension is
    ``H_target``. Returns ``(R or None, H_max)`` — None when the target
    exceeds the largest tension reachable without lifting the leg off the
    bed (H at the range where the bed run vanishes).
    """
    if h <= 1e-3:
        return L, float("inf")
    # Largest reachable range: bed run t = 0, all length suspended.
    lo_R, hi_R = 1e-3, float(L)
    for _ in range(60):
        mid = 0.5 * (lo_R + hi_R)
        if _leg_geometry(mid, h, L, w) is None:
            hi_R = mid
        else:
            lo_R = mid
    R_max = lo_R
    g = _leg_geometry(R_max, h, L, w)
    H_max = g[0] if g is not None else 0.0
    if H_target >= H_max:
        return None, H_max
    lo, hi = 1e-3, R_max

    def H_of(R: float) -> float:
        gg = _leg_geometry(R, h, L, w)
        return gg[0] if gg is not None else float("inf")

    for _ in range(70):
        mid = 0.5 * (lo + hi)
        if H_of(mid) < H_target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi), H_max


def _circle_intersection(c1: "np.ndarray", r1: float, c2: "np.ndarray",
                         r2: float) -> Optional[Tuple["np.ndarray", "np.ndarray"]]:
    """Intersection points of two plan circles, or None."""
    d = float(np.linalg.norm(c2 - c1))
    if d < 1e-9 or d > r1 + r2 or d < abs(r1 - r2):
        return None
    a = (r1 * r1 - r2 * r2 + d * d) / (2.0 * d)
    h2 = r1 * r1 - a * a
    if h2 < 0.0:
        return None
    h = math.sqrt(max(0.0, h2))
    e = (c2 - c1) / d
    n = np.array([-e[1], e[0]])
    mid = c1 + a * e
    return mid + h * n, mid - h * n


@dataclass
class PlanState:
    """One planned quasi-static state during the descent."""

    bu_depth_m: float                     # positive metres below surface
    bu_xy: Tuple[float, float]
    vessel_xy: Tuple[float, float]
    course_deg: float                     # lay-away direction (math deg)
    leg_H_N: Dict[str, float]
    leg_susp_m: Dict[str, float]
    trunk_H_N: float
    trunk_top_N: float
    trunk_susp_m: float


@dataclass
class BUPlanResult:
    states: List[PlanState]
    leg_lengths_m: Dict[str, float]       # required deployed length per leg
    landing_xy: Tuple[float, float]
    warnings: List[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return len(self.states) >= 2


def plan_bu_descent(
    bathy,
    anchor1_xy: Tuple[float, float],
    anchor2_xy: Tuple[float, float],
    target_xy: Tuple[float, float],
    *,
    w_leg1_npm: float,
    w_leg2_npm: float,
    w_trunk_npm: float,
    bu_weight_N: float,
    H1_target_N: float,
    H2_target_N: float,
    sheave_height_m: float = 5.0,
    spawn_depth_m: float = 2.0,
    n_steps: int = 24,
) -> BUPlanResult:
    """Plan the BU descent that holds each leg at its target bottom tension
    and lands the BU on ``target_xy``.

    Returns the required leg deployed lengths and the sequence of planned
    states (BU position, vessel position, lay-away course, trunk payout
    state). Warnings flag depths where a target is unreachable (clamped to
    what the geometry allows).
    """
    warnings: List[str] = []
    A1 = np.asarray(anchor1_xy, dtype=float)
    A2 = np.asarray(anchor2_xy, dtype=float)
    P = np.asarray(target_xy, dtype=float)
    depth_land = float(bathy.depth_at(P[0], P[1]))
    if depth_land <= 0.0:
        raise ValueError("target position is not under water")

    # Required leg lengths: exactly cover the bed path to the landing.
    L1 = bed_path_length(bathy, anchor1_xy, target_xy)
    L2 = bed_path_length(bathy, anchor2_xy, target_xy)
    lengths = {"leg1": L1, "leg2": L2}

    zs = np.linspace(max(0.5, float(spawn_depth_m)), depth_land,
                     max(2, int(n_steps)))
    states: List[PlanState] = []
    p_prev = P.copy()
    for z in zs:
        # Bed height under the BU plan position (previous estimate).
        bed_here = float(bathy.depth_at(p_prev[0], p_prev[1]))
        h = max(0.0, bed_here - float(z))
        at_land = h <= 1e-3 or abs(z - depth_land) < 1e-9

        if at_land:
            p = P.copy()
            H1, s1 = H1_target_N, 0.0
            H2, s2 = H2_target_N, 0.0
        else:
            R1, H1max = _range_for_tension(H1_target_N, h, L1, w_leg1_npm)
            R2, H2max = _range_for_tension(H2_target_N, h, L2, w_leg2_npm)
            H1, H2 = H1_target_N, H2_target_N
            if R1 is None:
                warnings.append(
                    f"z={z:.0f} m: leg 1 target {H1_target_N/1e3:.2f} kN "
                    f"exceeds the reachable {H1max/1e3:.2f} kN — clamped.")
                H1 = 0.95 * H1max
                R1, _ = _range_for_tension(H1, h, L1, w_leg1_npm)
            if R2 is None:
                warnings.append(
                    f"z={z:.0f} m: leg 2 target {H2_target_N/1e3:.2f} kN "
                    f"exceeds the reachable {H2max/1e3:.2f} kN — clamped.")
                H2 = 0.95 * H2max
                R2, _ = _range_for_tension(H2, h, L2, w_leg2_npm)
            if R1 is None or R2 is None:
                warnings.append(f"z={z:.0f} m: no feasible BU position — "
                                "state skipped.")
                continue
            inter = _circle_intersection(A1, R1, A2, R2)
            if inter is None:
                warnings.append(
                    f"z={z:.0f} m: the leg tension targets place the BU at "
                    f"ranges {R1:.0f} m / {R2:.0f} m from the laid ends, "
                    "which no single position satisfies — state skipped.")
                continue
            # Pick the intersection nearer the previous BU position (the
            # branch that converges onto the landing target).
            p = min(inter, key=lambda q: float(np.linalg.norm(q - p_prev)))
            g1 = _leg_geometry(float(np.linalg.norm(p - A1)), h, L1, w_leg1_npm)
            g2 = _leg_geometry(float(np.linalg.norm(p - A2)), h, L2, w_leg2_npm)
            s1 = g1[1] if g1 is not None else 0.0
            s2 = g2[1] if g2 is not None else 0.0

        # Trunk force balance at the BU.
        u1 = (A1 - p)
        u2 = (A2 - p)
        n1 = float(np.linalg.norm(u1))
        n2 = float(np.linalg.norm(u2))
        u1 = u1 / n1 if n1 > 1e-9 else np.zeros(2)
        u2 = u2 / n2 if n2 > 1e-9 else np.zeros(2)
        R_vec = H1 * u1 + H2 * u2          # horizontal pull of the legs
        H_t = float(np.linalg.norm(R_vec))
        V_b = float(bu_weight_N) + w_leg1_npm * s1 + w_leg2_npm * s2
        dz = float(sheave_height_m) + float(z)      # climb BU -> sheave
        a_t = H_t / w_trunk_npm if H_t > 1e-6 else 0.0
        if a_t > 1e-9:
            x0 = a_t * math.asinh(V_b / H_t)
            arg = math.cosh(x0 / a_t) + dz / a_t
            D_t = a_t * math.acosh(arg) - x0
            s_t = a_t * (math.sinh((x0 + D_t) / a_t) - math.sinh(x0 / a_t))
            T_top = math.hypot(H_t, w_trunk_npm * s_t + V_b)
            e_v = -R_vec / H_t
        else:
            # Balanced legs: the trunk hangs vertically over the BU.
            D_t, s_t = 0.0, dz
            T_top = V_b + w_trunk_npm * dz
            e_v = np.array([0.0, 0.0])
        vessel = p + e_v * D_t
        course = (math.degrees(math.atan2(e_v[1], e_v[0]))
                  if float(np.linalg.norm(e_v)) > 1e-9
                  else (states[-1].course_deg if states else 0.0))
        states.append(PlanState(
            bu_depth_m=float(z),
            bu_xy=(float(p[0]), float(p[1])),
            vessel_xy=(float(vessel[0]), float(vessel[1])),
            course_deg=float(course),
            leg_H_N={"leg1": float(H1), "leg2": float(H2)},
            leg_susp_m={"leg1": float(s1), "leg2": float(s2)},
            trunk_H_N=float(H_t),
            trunk_top_N=float(T_top),
            trunk_susp_m=float(s_t),
        ))
        p_prev = p

    if len(states) < 2:
        warnings.append("Plan infeasible: fewer than two valid states.")
    return BUPlanResult(states=states, leg_lengths_m=lengths,
                        landing_xy=(float(P[0]), float(P[1])),
                        warnings=warnings)


def plan_bu_descent_fixed_lengths(
    bathy,
    anchor1_xy: Tuple[float, float],
    anchor2_xy: Tuple[float, float],
    leg1_length_m: float,
    leg2_length_m: float,
    *,
    w_leg1_npm: float,
    w_leg2_npm: float,
    w_trunk_npm: float,
    bu_weight_N: float,
    H1_target_N: float,
    H2_target_N: float,
    sheave_height_m: float = 5.0,
    spawn_depth_m: float = 2.0,
    n_steps: int = 24,
    start_xy: Tuple[float, float] = (0.0, 0.0),
) -> BUPlanResult:
    """Plan a descent when the leg lengths are GIVEN (the lowering-only
    scenario: the legs were laid to the jointing position and their lengths
    are facts, not choices).

    The landing point is then an *output*: the position whose seabed paths
    from the two laid ends equal the leg lengths (each leg fully laid with
    the target residual tension at the moment of touchdown) — found as a
    two-circle intersection, iterated so the bed-path lengths (not just the
    plan chords) match on non-flat bathymetry. Of the two intersections,
    the landing is the one on the SAME side of the anchor-to-anchor line
    as ``start_xy`` (the jointing position): the legs run outward from the
    start toward their laid ends, so the BU lays away on the start's side,
    never through the laid ends. The descent states are the same
    target-tension solve as :func:`plan_bu_descent`.
    """
    A1 = np.asarray(anchor1_xy, dtype=float)
    A2 = np.asarray(anchor2_xy, dtype=float)
    S = np.asarray(start_xy, dtype=float)
    L1, L2 = float(leg1_length_m), float(leg2_length_m)
    warnings: List[str] = []

    def pick_branch(inter):
        e = A2 - A1
        side_start = float(e[0] * (S[1] - A1[1]) - e[1] * (S[0] - A1[0]))
        if abs(side_start) < 1e-9:
            # Start on the line (degenerate): nearest to the start.
            return min(inter, key=lambda q: float(np.linalg.norm(q - S)))
        for q in inter:
            side = float(e[0] * (q[1] - A1[1]) - e[1] * (q[0] - A1[0]))
            if side * side_start > 0.0:
                return q
        return min(inter, key=lambda q: float(np.linalg.norm(q - S)))

    # Straight-line radii that make the BED paths equal the leg lengths.
    R1, R2 = L1, L2
    P = None
    for _ in range(4):
        inter = _circle_intersection(A1, R1, A2, R2)
        if inter is None:
            break
        P = pick_branch(inter)
        b1 = bed_path_length(bathy, tuple(A1), tuple(P))
        b2 = bed_path_length(bathy, tuple(A2), tuple(P))
        if abs(b1 - L1) < 0.5 and abs(b2 - L2) < 0.5:
            break
        R1 *= L1 / max(b1, 1e-6)
        R2 *= L2 / max(b2, 1e-6)
    if P is None:
        return BUPlanResult(
            states=[], leg_lengths_m={"leg1": L1, "leg2": L2},
            landing_xy=(float("nan"), float("nan")),
            warnings=["The leg lengths cannot both be fully laid to one "
                      "landing point (circles do not intersect) — check "
                      "the lead bearings and lengths."])

    plan = plan_bu_descent(
        bathy, tuple(A1), tuple(A2), (float(P[0]), float(P[1])),
        w_leg1_npm=w_leg1_npm, w_leg2_npm=w_leg2_npm,
        w_trunk_npm=w_trunk_npm, bu_weight_N=bu_weight_N,
        H1_target_N=H1_target_N, H2_target_N=H2_target_N,
        sheave_height_m=sheave_height_m, spawn_depth_m=spawn_depth_m,
        n_steps=n_steps)
    # Report the *given* lengths (the derived bed paths match them by
    # construction, within the iteration tolerance above).
    plan.leg_lengths_m = {"leg1": L1, "leg2": L2}
    plan.warnings = warnings + plan.warnings
    return plan


def refine_landing(make_plan, simulate, target_xy: Tuple[float, float],
                   rounds: int = 2, tol_m: float = 5.0) -> "BUPlanResult":
    """Close the plan-vs-simulation landing gap by re-aiming the plan.

    The analytic plan assumes straight bed runs; the simulated frozen-lay
    path curves, so the landing typically misses by tens of metres. Aiming
    a fresh plan at ``target - error`` cancels that bias (the same
    translation trick as the schedule optimiser).

    ``make_plan(aim_xy) -> BUPlanResult`` builds a plan aimed at a point;
    ``simulate(plan) -> landed_xy or None`` runs it (quick quality is
    plenty) and returns where the BU landed.
    """
    aim = np.asarray(target_xy, dtype=float)
    plan = make_plan((float(aim[0]), float(aim[1])))
    for _ in range(max(0, int(rounds))):
        landed = simulate(plan)
        if landed is None or not plan.feasible:
            break
        err = np.asarray(landed, dtype=float) - np.asarray(target_xy, dtype=float)
        if float(np.linalg.norm(err)) <= tol_m:
            break
        aim = aim - err
        plan = make_plan((float(aim[0]), float(aim[1])))
    return plan


def plan_to_schedule(
    plan: BUPlanResult,
    *,
    payout_mps: float = 0.4,
    trunk_slack_pct: float = 2.0,
    min_phase_s: float = 20.0,
    overboard_event: bool = True,
    start_xy: Optional[Tuple[float, float]] = None,
    start_trunk_susp_m: Optional[float] = None,
) -> List[PhaseRow]:
    """Convert a descent plan into schedule phases for the lowering.

    Each pair of consecutive planned states becomes one phase: the trunk
    pays out the growth in suspended trunk length (plus the slack
    allowance), timed at ``payout_mps``, while the vessel runs the chord
    between the planned positions.

    For the full two-sheave deployment, splice these rows in place of the
    default "Overboard BU and lower" phase — the first row is tagged with
    the ``overboard_bu`` event (default). For the lowering-only scenario
    pass ``overboard_event=False`` (the BU already hangs on the trunk) and
    give ``start_xy`` / ``start_trunk_susp_m``: a transition phase is
    prepended that steers the vessel from its actual start onto the
    planned track (early leg tensions deviate from target while the BU is
    near the surface — that transient is real physics, not a planner
    error).
    """
    rows: List[PhaseRow] = []
    if not plan.feasible:
        return rows
    slack = 1.0 + max(0.0, trunk_slack_pct) / 100.0
    if start_xy is not None and plan.states:
        s0 = plan.states[0]
        move = math.hypot(s0.vessel_xy[0] - start_xy[0],
                          s0.vessel_xy[1] - start_xy[1])
        base = (float(start_trunk_susp_m) if start_trunk_susp_m is not None
                else s0.trunk_susp_m)
        dL = max(0.0, (s0.trunk_susp_m - base)) * slack
        if move > 1.0 or dL > 0.5:
            dt = max(min_phase_s, dL / max(payout_mps, 0.01),
                     move / max(payout_mps, 0.01))
            course = (math.degrees(math.atan2(s0.vessel_xy[1] - start_xy[1],
                                              s0.vessel_xy[0] - start_xy[0]))
                      if move > 1e-6 else s0.course_deg)
            rows.append(PhaseRow(
                label="Steer onto the planned track",
                duration_s=dt, course_deg=course, speed_mps=move / dt,
                payout_mps={"trunk": dL / dt} if dL > 0.0 else {},
                distance_m=move,
            ))
    for k in range(len(plan.states) - 1):
        s0, s1 = plan.states[k], plan.states[k + 1]
        dL = max(0.0, (s1.trunk_susp_m - s0.trunk_susp_m)) * slack
        move = math.hypot(s1.vessel_xy[0] - s0.vessel_xy[0],
                          s1.vessel_xy[1] - s0.vessel_xy[1])
        dt = max(min_phase_s, dL / max(payout_mps, 0.01))
        course = math.degrees(math.atan2(
            s1.vessel_xy[1] - s0.vessel_xy[1],
            s1.vessel_xy[0] - s0.vessel_xy[0])) if move > 1e-6 else s0.course_deg
        rows.append(PhaseRow(
            label=(f"Lower BU to {s1.bu_depth_m:.0f} m "
                   f"(legs {s1.leg_H_N['leg1']/1e3:.1f}/"
                   f"{s1.leg_H_N['leg2']/1e3:.1f} kN)"),
            duration_s=dt,
            course_deg=course,
            speed_mps=move / dt,
            payout_mps={"trunk": dL / dt} if dL > 0.0 else {},
            event="overboard_bu" if (k == 0 and overboard_event) else "",
            distance_m=move,
        ))
    return rows
