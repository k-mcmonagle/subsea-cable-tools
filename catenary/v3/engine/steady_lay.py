# -*- coding: utf-8 -*-
"""Steady-state cable lay with hydrodynamic drag (Zajac's stationary model).

Pure Python + NumPy; no Qt/QGIS imports.

Solves the stationary configuration of a cable being laid at constant ship
speed and pay-out rate by integrating the 3D force-balance ODEs from the
touchdown point up to the vessel, in the ship frame:

    d(r)/ds  = t                                (unit tangent)
    dT/ds    = -f . t
    dt/ds    = -(f - (f.t) t) / (T - rho_c*Vc^2)

with the distributed force ``f`` = weight (per-medium) + Morison drag from
the apparent flow ``u_rel = current(z) - V_ship + Vc*t`` (material transport
enters the relative velocity tangentially; Zajac eq. 13). ``rho_c*Vc^2`` is
the transport (centrifugal) correction of Zajac eq. 18a.

Boundary conditions: at the TDP the cable leaves the bed tangentially with
tension ``T0`` (bottom tension; >= a small positive floor — the ``T0 = 0``
limit is Zajac's straight line at the critical angle, which the integration
approaches smoothly). Integration stops at the chute elevation.

This module cross-validates the general 3D DR solver and provides the fast
"Steady lay" answer mode; closed-form identities from ``hydrodynamics``
(critical angle, top-tension theorem) are natural checks on both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

from .hydrodynamics import (
    CurrentProfile,
    RHO_SEAWATER,
    critical_angle_rad,
    hydrodynamic_constant,
)


@dataclass
class SteadyLayInput:
    """Inputs for a steady-lay solve. The lay azimuth is +x by default; the
    ship steams toward +x and the cable trails toward -x from the vessel."""

    depth_m: float                      # water depth at the TDP
    q_water_npm: float                  # submerged weight per length
    q_air_npm: float = 0.0              # in-air weight (0 -> q_water)
    diameter_m: float = 0.04
    cd_normal: float = 1.2
    cd_tangential: float = 0.01
    rho_c_kgpm: float = 0.0             # physical mass/length (transport term)
    ship_speed_mps: float = 0.0
    payout_speed_mps: Optional[float] = None   # None -> equal to ship speed
    current: Optional[CurrentProfile] = None
    chute_height_m: float = 0.0         # chute elevation above waterline
    rho_water: float = RHO_SEAWATER
    bed_slope_along_rad: float = 0.0    # bed slope at TDP along the lay dir
    ds_m: float = 0.0                   # 0 -> auto (depth/800, clamped)
    T0_N: float = 0.0                   # bottom tension (floored internally)


@dataclass
class SteadyLayResult:
    s: "np.ndarray"                     # arc length from TDP
    xyz: "np.ndarray"                   # (n, 3); TDP at origin, bed z=-depth
    tension_N: "np.ndarray"
    T0_N: float = 0.0
    top_tension_N: float = 0.0
    exit_angle_deg: float = 0.0         # from horizontal at the top end
    layback_m: float = 0.0              # horizontal TDP -> exit distance
    lateral_offset_m: float = 0.0       # exit y-offset (cross-current)
    suspended_length_m: float = 0.0
    critical_angle_deg: float = 0.0     # Zajac closed form (no current)
    hydrodynamic_constant_mps: float = 0.0
    min_radius_m: float = float("inf")
    min_radius_s_m: float = 0.0
    warnings: List[str] = field(default_factory=list)

    def tension_at_s(self, s_m: float) -> float:
        return float(np.interp(float(s_m), self.s, self.tension_N))


def _distributed_force(z: float, t_hat: "np.ndarray", inp: SteadyLayInput,
                       u_ship: "np.ndarray") -> "np.ndarray":
    """Weight + drag per unit length at elevation z with tangent t_hat."""
    if z < 0.0:
        q = inp.q_water_npm
    else:
        q = inp.q_air_npm if inp.q_air_npm != 0.0 else inp.q_water_npm
    f = np.array([0.0, 0.0, -q])
    if z < 0.0 and inp.diameter_m > 0.0:
        u_w = np.zeros(3)
        if inp.current is not None:
            u_w = inp.current.velocity_at(np.array([z]))[0]
        vc = inp.payout_speed_mps if inp.payout_speed_mps is not None else inp.ship_speed_mps
        u_rel = u_w - u_ship + vc * t_hat
        ut = float(np.dot(u_rel, t_hat))
        u_t = ut * t_hat
        u_n = u_rel - u_t
        un_mag = float(np.linalg.norm(u_n))
        f = f + 0.5 * inp.rho_water * inp.cd_normal * inp.diameter_m * un_mag * u_n
        f = f + 0.5 * inp.rho_water * inp.cd_tangential * math.pi * inp.diameter_m * abs(ut) * ut * t_hat
    return f


def integrate_steady_lay(inp: SteadyLayInput) -> SteadyLayResult:
    """Integrate the stationary configuration from the TDP to the chute."""
    warnings: List[str] = []
    h = float(inp.depth_m)
    if h <= 0:
        raise ValueError("depth_m must be positive")
    vc = inp.payout_speed_mps if inp.payout_speed_mps is not None else inp.ship_speed_mps
    u_ship = np.array([inp.ship_speed_mps, 0.0, 0.0])
    ds = inp.ds_m if inp.ds_m > 0 else max(0.05, min(2.0, h / 800.0))
    z_stop = inp.chute_height_m

    # Bottom-tension floor: the ODE is singular at T = 0 (Zajac's straight
    # line); a tiny positive floor converges to the same shape.
    T_floor = max(1.0, 1e-4 * inp.q_water_npm * h)
    T = max(float(inp.T0_N), T_floor)
    if inp.T0_N < T_floor:
        warnings.append(
            f"Bottom tension floored at {T_floor:.1f} N for integration; "
            "the zero-tension limit is the straight-line critical-angle lay."
        )

    cent = inp.rho_c_kgpm * vc * vc  # transport correction, N

    # Touchdown condition (Zajac): either T0 > 0 with *tangential* departure
    # along the bed, or T0 = 0 with the cable landing at the straight-line
    # critical angle. Starting a T0 ~ 0 case tangentially would integrate
    # through the singular boundary layer and misses the physical solution.
    beta = float(inp.bed_slope_along_rad)
    t_hat = np.array([math.cos(beta), 0.0, math.sin(beta)])
    if inp.T0_N <= T_floor:
        u_bed = -u_ship
        if inp.current is not None:
            u_bed = u_bed + inp.current.velocity_at(np.array([-h]))[0]
        v_eff = float(math.hypot(u_bed[0], u_bed[1]))
        if v_eff > 0.05 and inp.diameter_m > 0.0:
            H_c0 = hydrodynamic_constant(
                inp.q_water_npm, inp.diameter_m, inp.cd_normal, inp.rho_water
            )
            alpha0 = critical_angle_rad(H_c0, v_eff)
            dir_h = -u_bed[:2] / v_eff  # rise against the apparent flow
            t_hat = np.array(
                [math.cos(alpha0) * dir_h[0], math.cos(alpha0) * dir_h[1], math.sin(alpha0)]
            )
        elif inp.q_water_npm > 0.0:
            # No meaningful flow: the zero-bottom-tension limit is a
            # vertical hang.
            t_hat = np.array([0.0, 0.0, 1.0])
    r = np.array([0.0, 0.0, -h])

    s_list = [0.0]
    r_list = [r.copy()]
    T_list = [T]

    s = 0.0

    # Below T ~ rho_c*Vc^2 the transverse equation is singular/ill-posed
    # (Zajac App. A). In the T0 ~ 0 regime the straight line at the critical
    # angle is an exact solution, so walk it analytically until the tension
    # comfortably exceeds the transport term, then hand over to RK4.
    T_safe = max(5.0 * cent, 20.0 * T_floor)
    if inp.T0_N <= T_floor and t_hat[2] > 1e-6:
        while T < T_safe and r[2] < z_stop - ds:
            f = _distributed_force(float(r[2]), t_hat, inp, u_ship)
            dT = -float(np.dot(f, t_hat))
            r = r + ds * t_hat
            T = max(T_floor, T + ds * dT)
            s += ds
            s_list.append(s)
            r_list.append(r.copy())
            T_list.append(T)
    s_max = 30.0 * h + 2000.0
    n_max = int(s_max / ds) * 40 + 10  # headroom for adaptive refinement
    theta_max = 0.03                   # max tangent rotation per step (rad)

    def deriv(r_c, t_c, T_c):
        f = _distributed_force(float(r_c[2]), t_c, inp, u_ship)
        ft = float(np.dot(f, t_c))
        dT = -ft
        denom = T_c - cent
        if denom < T_floor * 0.5:
            denom = T_floor * 0.5
        dt = -(f - ft * t_c) / denom
        return t_c, dT, dt

    for _ in range(n_max):
        # RK4 on (r, T, t) with tangent renormalisation.
        k1r, k1T, k1t = deriv(r, t_hat, T)
        # Curvature-adaptive step: refine where the tangent turns fast
        # (the chute/top region) so the reported minimum bend radius is
        # actually resolved rather than smeared over a 2 m step.
        kappa = float(np.linalg.norm(k1t))
        h_s = ds if kappa * ds <= theta_max else max(0.05, theta_max / kappa)
        r2 = r + 0.5 * h_s * k1r
        t2 = t_hat + 0.5 * h_s * k1t
        t2 /= np.linalg.norm(t2)
        T2 = T + 0.5 * h_s * k1T
        k2r, k2T, k2t = deriv(r2, t2, T2)
        r3 = r + 0.5 * h_s * k2r
        t3 = t_hat + 0.5 * h_s * k2t
        t3 /= np.linalg.norm(t3)
        T3 = T + 0.5 * h_s * k2T
        k3r, k3T, k3t = deriv(r3, t3, T3)
        r4 = r + h_s * k3r
        t4 = t_hat + h_s * k3t
        t4 /= np.linalg.norm(t4)
        T4 = T + h_s * k3T
        k4r, k4T, k4t = deriv(r4, t4, T4)

        r = r + (h_s / 6.0) * (k1r + 2 * k2r + 2 * k3r + k4r)
        T = T + (h_s / 6.0) * (k1T + 2 * k2T + 2 * k3T + k4T)
        t_hat = t_hat + (h_s / 6.0) * (k1t + 2 * k2t + 2 * k3t + k4t)
        t_hat /= np.linalg.norm(t_hat)
        s += h_s

        s_list.append(s)
        r_list.append(r.copy())
        T_list.append(T)

        if r[2] >= z_stop or s >= s_max:
            break
    if r[2] < z_stop:
        warnings.append(
            "Integration reached the arc-length cap before the chute height — "
            "check weights/drag inputs (buoyant or near-neutral cable?)."
        )

    xyz = np.asarray(r_list)
    s_arr = np.asarray(s_list)
    T_arr = np.asarray(T_list)

    # Interpolate the exact chute crossing on the last step.
    if len(xyz) >= 2 and xyz[-1, 2] > z_stop and xyz[-2, 2] < z_stop:
        f = (z_stop - xyz[-2, 2]) / (xyz[-1, 2] - xyz[-2, 2])
        xyz[-1] = xyz[-2] + f * (xyz[-1] - xyz[-2])
        s_arr[-1] = s_arr[-2] + f * (s_arr[-1] - s_arr[-2])
        T_arr[-1] = T_arr[-2] + f * (T_arr[-1] - T_arr[-2])

    d_end = xyz[-1] - xyz[-2] if len(xyz) >= 2 else np.array([1.0, 0.0, 0.0])
    exit_angle = math.degrees(math.atan2(d_end[2], math.hypot(d_end[0], d_end[1])))

    # Min bend radius from three-point circumradius on the polyline.
    min_r, min_r_s = float("inf"), 0.0
    if len(xyz) >= 3:
        a = xyz[:-2] - xyz[1:-1]
        b = xyz[2:] - xyz[1:-1]
        la = np.linalg.norm(a, axis=1)
        lb = np.linalg.norm(b, axis=1)
        lc = np.linalg.norm(xyz[2:] - xyz[:-2], axis=1)
        area2 = np.linalg.norm(np.cross(a, b), axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            radii = np.where(area2 > 1e-12, la * lb * lc / (2.0 * area2), np.inf)
        i = int(np.argmin(radii))
        min_r = float(radii[i])
        min_r_s = float(s_arr[i + 1])

    H_c = hydrodynamic_constant(inp.q_water_npm, inp.diameter_m, inp.cd_normal, inp.rho_water)
    alpha = critical_angle_rad(H_c, inp.ship_speed_mps) if inp.ship_speed_mps > 0 else math.pi / 2

    return SteadyLayResult(
        s=s_arr,
        xyz=xyz,
        tension_N=T_arr,
        T0_N=float(T_arr[0]),
        top_tension_N=float(T_arr[-1]),
        exit_angle_deg=exit_angle,
        layback_m=float(math.hypot(xyz[-1, 0] - xyz[0, 0], xyz[-1, 1] - xyz[0, 1])),
        lateral_offset_m=float(xyz[-1, 1] - xyz[0, 1]),
        suspended_length_m=float(s_arr[-1]),
        critical_angle_deg=math.degrees(alpha),
        hydrodynamic_constant_mps=H_c,
        min_radius_m=min_r,
        min_radius_s_m=min_r_s,
        warnings=warnings,
    )


SOLVE_MODES = ("bottom_tension", "top_tension", "exit_angle", "layback", "suspended_length")


def solve_steady_lay(inp: SteadyLayInput, mode: str = "bottom_tension",
                     value: Optional[float] = None) -> SteadyLayResult:
    """Solve with the given input mode.

    * ``bottom_tension`` (N): direct integration (value optional; defaults to
      ``inp.T0_N``).
    * ``top_tension`` (N), ``exit_angle`` (deg from horizontal), ``layback``
      (m), ``suspended_length`` (m): 1D root-find on T0.
    """
    if mode not in SOLVE_MODES:
        raise ValueError(f"mode must be one of {SOLVE_MODES}")
    if mode == "bottom_tension":
        if value is not None:
            inp.T0_N = float(value)
        return integrate_steady_lay(inp)
    if value is None:
        raise ValueError(f"mode {mode!r} needs a target value")
    target = float(value)

    def metric(res: SteadyLayResult) -> float:
        if mode == "top_tension":
            return res.top_tension_N
        if mode == "exit_angle":
            return res.exit_angle_deg
        if mode == "layback":
            return res.layback_m
        return res.suspended_length_m

    # Exit angle decreases with T0; the others increase. Bracket on T0.
    wh = inp.q_water_npm * inp.depth_m
    lo, hi = 0.0, max(4.0 * wh, 1e4)
    inp.T0_N = lo
    m_lo = metric(integrate_steady_lay(inp))
    inp.T0_N = hi
    m_hi = metric(integrate_steady_lay(inp))
    increasing = m_hi >= m_lo
    lo_ok = (m_lo <= target) if increasing else (m_lo >= target)
    for _ in range(40):
        if lo_ok and ((m_hi >= target) if increasing else (m_hi <= target)):
            break
        hi *= 2.0
        inp.T0_N = hi
        m_hi = metric(integrate_steady_lay(inp))
        if hi > 1e9:
            break
    res = None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        inp.T0_N = mid
        res = integrate_steady_lay(inp)
        m = metric(res)
        if abs(m - target) <= max(1e-4 * abs(target), 1e-6):
            break
        if (m < target) == increasing:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-3:
            break
    if res is None:
        res = integrate_steady_lay(inp)
    err = abs(metric(res) - target) / max(abs(target), 1e-9)
    if err > 0.01:
        res.warnings.append(
            f"Root-finding on bottom tension reached {metric(res):.4g} vs the "
            f"target {target:.4g} ({err:.1%} off) — the target may be "
            "unreachable with these inputs."
        )
    return res
