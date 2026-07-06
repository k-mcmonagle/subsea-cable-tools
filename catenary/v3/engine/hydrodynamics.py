# -*- coding: utf-8 -*-
"""Hydrodynamic loading, current profiles and Zajac closed forms.

Pure Python + NumPy; no Qt/QGIS imports.

Drag model (per unit length of cable, tangent ``t``):

    u_rel = u_water(z) - v_cable
    u_t   = (u_rel . t) t;   u_n = u_rel - u_t
    f_n   = 0.5 * rho * Cd_n * d       * |u_n| * u_n
    f_t   = 0.5 * rho * Cd_t * pi * d  * |u_t| * u_t

Closed forms from Zajac 1957 (see ``ref/REFERENCE_DIGEST.md``): hydrodynamic
constant, critical angle, sloped-bed pay-out increments, the free-span
suspension criterion and the snap-tension impedance. These are exposed both
as quick answers in the UI and as validation anchors for the numerical
solvers.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import List, Optional, Sequence, Tuple

import numpy as np

G = 9.80665
RHO_SEAWATER = 1025.0
KNOT = 0.514444  # m/s


@dataclass
class CurrentLayer:
    """One layer of a piecewise-linear current profile."""

    depth_m: float          # positive down
    speed_mps: float
    direction_deg: float    # direction the current flows TOWARD, 0 = +x, CCW


class CurrentProfile:
    """Depth-dependent horizontal current, linear between layers.

    With no layers the profile is zero everywhere. A single layer gives a
    uniform current. Outside the sampled depth range the end values hold.
    """

    def __init__(self, layers: Optional[Sequence[CurrentLayer]] = None):
        layers = sorted(layers or [], key=lambda l: l.depth_m)
        self._depths = np.array([l.depth_m for l in layers], dtype=float)
        self._u = np.array(
            [l.speed_mps * math.cos(math.radians(l.direction_deg)) for l in layers], dtype=float
        )
        self._v = np.array(
            [l.speed_mps * math.sin(math.radians(l.direction_deg)) for l in layers], dtype=float
        )

    @classmethod
    def uniform(cls, speed_mps: float, direction_deg: float = 0.0) -> "CurrentProfile":
        return cls([CurrentLayer(0.0, speed_mps, direction_deg)])

    @property
    def is_zero(self) -> bool:
        return len(self._depths) == 0 or bool(np.all(np.hypot(self._u, self._v) < 1e-12))

    def max_speed(self) -> float:
        if len(self._depths) == 0:
            return 0.0
        return float(np.max(np.hypot(self._u, self._v)))

    def velocity_at(self, z) -> "np.ndarray":
        """(n, 3) water velocity at elevations z (negative down)."""
        z_arr = np.atleast_1d(np.asarray(z, dtype=float))
        out = np.zeros((len(z_arr), 3))
        if len(self._depths):
            depth = np.maximum(0.0, -z_arr)
            out[:, 0] = np.interp(depth, self._depths, self._u, left=self._u[0], right=self._u[-1])
            out[:, 1] = np.interp(depth, self._depths, self._v, left=self._v[0], right=self._v[-1])
        return out

    def to_dict(self) -> dict:
        return {
            "layers": [
                {
                    "depth_m": float(d),
                    "speed_mps": float(math.hypot(u, v)),
                    "direction_deg": float(math.degrees(math.atan2(v, u))),
                }
                for d, u, v in zip(self._depths, self._u, self._v)
            ]
        }

    @classmethod
    def from_dict(cls, cfg: dict) -> "CurrentProfile":
        return cls([CurrentLayer(l["depth_m"], l["speed_mps"], l.get("direction_deg", 0.0))
                    for l in (cfg or {}).get("layers", [])])


# ---------------------------------------------------------------------------
# Zajac 1957 closed forms
# ---------------------------------------------------------------------------

def hydrodynamic_constant(q_water_npm: float, diameter_m: float,
                          cd_normal: float = 1.2, rho: float = RHO_SEAWATER) -> float:
    """H = sqrt(2 w / (C_D rho d)) in m/s — equals the transverse sinking
    velocity. Zajac eq. (8)/(16)."""
    if q_water_npm <= 0 or diameter_m <= 0 or cd_normal <= 0:
        return 0.0
    return math.sqrt(2.0 * q_water_npm / (cd_normal * rho * diameter_m))


def critical_angle_rad(H_mps: float, ship_speed_mps: float) -> float:
    """Exact straight-line lay angle: cos a = sqrt(1 + r^4/4) - r^2/2 with
    r = H/V. Zajac eq. (10)."""
    if ship_speed_mps <= 0:
        return math.pi / 2.0
    r2 = (H_mps / ship_speed_mps) ** 2
    cos_a = math.sqrt(1.0 + 0.25 * r2 * r2) - 0.5 * r2
    return math.acos(max(-1.0, min(1.0, cos_a)))


def payout_increment_mps(H_mps: float, bed_angle_rad: float) -> float:
    """Extra pay-out (descent, +) or reduction (ascent, -) to keep bottom
    slack on a bed slope: V_c - V = H*beta/2 (Zajac eq. 35/36; independent
    of ship speed, small-angle)."""
    return 0.5 * H_mps * bed_angle_rad


def suspension_speed_limit_mps(H_mps: float, upslope_angle_rad: float) -> float:
    """Free spans form on an up-slope of angle gamma unless V < H/gamma
    (Zajac eq. 37). Returns the limiting ship speed (inf for flat)."""
    if upslope_angle_rad <= 1e-9:
        return float("inf")
    return H_mps / upslope_angle_rad


def snap_tension_N(EA_N: float, rho_c_kgpm: float, dV_mps: float) -> float:
    """Longitudinal snap tension for a sudden pay-out rate change dV:
    T = sqrt(EA * rho_c) * dV (Zajac eq. 29, semi-infinite cable)."""
    if EA_N <= 0 or rho_c_kgpm <= 0:
        return 0.0
    return math.sqrt(EA_N * rho_c_kgpm) * abs(dV_mps)


def capstan_tension(T_N: float, mu: float, wrap_angle_rad: float, laying: bool = True) -> float:
    """Tension on the machinery side of a chute/capstan: laying reduces the
    tensioner load (friction helps hold), recovery increases it.
    T_machine = T_outboard * exp(-mu*phi) laying, * exp(+mu*phi) recovery."""
    sign = -1.0 if laying else 1.0
    return T_N * math.exp(sign * mu * wrap_angle_rad)


def top_tension_theorem_N(T0_N: float, q_water_npm: float, depth_m: float) -> float:
    """T_ship = T_0 + w*h — independent of the normal drag law (Zajac 21)."""
    return T0_N + q_water_npm * depth_m


def slack_for_route(payout_speed_mps: float, ship_speed_mps: float) -> float:
    """Slack fraction eps = (V_c - V)/V."""
    if ship_speed_mps <= 0:
        return 0.0
    return (payout_speed_mps - ship_speed_mps) / ship_speed_mps


def drag_force_per_length(u_rel: "np.ndarray", tangent: "np.ndarray",
                          diameter_m, cd_normal, cd_tangential,
                          rho: float = RHO_SEAWATER) -> "np.ndarray":
    """Vectorised Morison drag per unit length. All inputs (n, 3)/(n,)."""
    u_rel = np.atleast_2d(np.asarray(u_rel, dtype=float))
    t = np.atleast_2d(np.asarray(tangent, dtype=float))
    ut = np.einsum("ij,ij->i", u_rel, t)
    u_t = ut[:, None] * t
    u_n = u_rel - u_t
    un_mag = np.linalg.norm(u_n, axis=1)
    dia = np.asarray(diameter_m, dtype=float)
    cdn = np.asarray(cd_normal, dtype=float)
    cdt = np.asarray(cd_tangential, dtype=float)
    f_n = (0.5 * rho * cdn * dia * un_mag)[:, None] * u_n
    f_t = (0.5 * rho * cdt * math.pi * dia * np.abs(ut) * ut)[:, None] * t
    return f_n + f_t
