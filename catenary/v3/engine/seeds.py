# -*- coding: utf-8 -*-
"""Analytic seed shapes for dynamic-relaxation cold starts.

Pure Python + NumPy; no Qt/QGIS imports.

A seed only changes the *path* to equilibrium, never the converged physics —
but starting from a hanging catenary instead of a straight line cuts the
relaxation iteration count severalfold. All helpers return (n+1, 3) node
polylines compatible with :func:`cable_system.straight_shape` consumers.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple

import numpy as np

from .cable_system import sagged_shape, straight_shape, resample_polyline


def solve_catenary_param(H: float, V: float, L: float) -> float:
    """Catenary parameter ``a`` for a hanging chain of arc length ``L``
    spanning horizontal distance ``H`` and vertical rise ``V``.

    Solves ``2 a sinh(H / 2a) = sqrt(L^2 - V^2)`` by bisection (the left
    side is strictly decreasing in ``a`` from +inf to ``H``). Requires
    ``L > sqrt(H^2 + V^2)`` (slack chain) and ``H > 0``.
    """
    k = math.sqrt(max(L * L - V * V, 0.0))
    if H <= 0.0 or k <= H:
        raise ValueError("catenary requires H > 0 and L > chord")

    def f(a: float) -> float:
        return 2.0 * a * math.sinh(H / (2.0 * a)) - k

    a_lo = H * 1e-3
    for _ in range(60):
        if f(a_lo) > 0.0:
            break
        a_lo *= 0.5
    a_hi = H
    for _ in range(120):
        if f(a_hi) < 0.0:
            break
        a_hi *= 2.0
    for _ in range(80):
        a_mid = 0.5 * (a_lo + a_hi)
        if f(a_mid) > 0.0:
            a_lo = a_mid
        else:
            a_hi = a_mid
    return 0.5 * (a_lo + a_hi)


def catenary_seed(p0: Sequence[float], p1: Sequence[float], length_m: float, n_elems: int) -> "np.ndarray":
    """(n+1, 3) nodes on the hanging catenary from ``p0`` to ``p1`` with
    total arc length ``length_m``, uniformly spaced in arc length.

    Falls back to :func:`sagged_shape` when the geometry degenerates
    (near-vertical span or length within a hair of the chord).
    """
    a3 = np.asarray(p0, dtype=float)
    b3 = np.asarray(p1, dtype=float)
    dxy = b3[:2] - a3[:2]
    H = float(np.hypot(dxy[0], dxy[1]))
    V = float(b3[2] - a3[2])
    L = float(length_m)
    chord = math.hypot(H, V)
    if L <= chord * (1.0 + 1e-9):
        return straight_shape(a3, b3, n_elems)
    if H < max(1e-6, 1e-3 * L) or L <= abs(V) * (1.0 + 1e-9):
        # Near-vertical: the catenary parameterisation degenerates.
        return sagged_shape(a3, b3, n_elems, slack_frac=max(0.0, L / max(chord, 1e-9) - 1.0))
    try:
        a = solve_catenary_param(H, V, L)
    except (ValueError, OverflowError):
        return sagged_shape(a3, b3, n_elems)
    # Vertex offset from end 0: tanh(xbar / a) = V / L.
    xbar = a * math.atanh(max(-1.0 + 1e-12, min(1.0 - 1e-12, V / L)))
    xv = H / 2.0 - xbar
    s0 = a * math.sinh((0.0 - xv) / a)     # arc coordinate of end 0 from vertex
    s_i = s0 + np.linspace(0.0, L, n_elems + 1)
    x_i = xv + a * np.arcsinh(s_i / a)
    z_i = a3[2] + a * (np.cosh((x_i - xv) / a) - math.cosh((0.0 - xv) / a))
    u = dxy / max(H, 1e-12)
    pts = np.empty((n_elems + 1, 3))
    pts[:, 0] = a3[0] + u[0] * x_i
    pts[:, 1] = a3[1] + u[1] * x_i
    pts[:, 2] = z_i
    # Pin the ends exactly (float noise from the arc inversion).
    pts[0] = a3
    pts[-1] = b3
    return pts


def bed_polyline(start_xy: Tuple[float, float], azimuth_deg: float, run_m: float,
                 bathy, ds_m: float = 10.0) -> "np.ndarray":
    """(m, 3) points marching ``run_m`` along ``azimuth_deg`` from
    ``start_xy``, each on the seabed (z = -depth)."""
    a = math.radians(float(azimuth_deg))
    ux, uy = math.cos(a), math.sin(a)
    n = max(2, int(math.ceil(run_m / max(ds_m, 1.0))) + 1)
    t = np.linspace(0.0, run_m, n)
    xs = start_xy[0] + ux * t
    ys = start_xy[1] + uy * t
    zs = -np.asarray(bathy.depth_at(xs, ys), dtype=float)
    return np.column_stack([xs, ys, zs])


def hanging_leg_seed(p_top: Sequence[float], azimuth_deg: float, length_m: float,
                     bathy, n_elems: int, drop_frac: float = 0.35) -> "np.ndarray":
    """Seed for a leg hanging from ``p_top`` (e.g. a BU junction) that
    touches down and then follows the seabed along ``azimuth_deg``:
    a hanging catenary to a touchdown guess plus a bed-following tail,
    resampled to ``n_elems`` (matters on slopes and grids where a straight
    seed can start far from the bed)."""
    p_top = np.asarray(p_top, dtype=float)
    a = math.radians(float(azimuth_deg))
    ux, uy = math.cos(a), math.sin(a)
    depth_below = float(bathy.depth_at(p_top[0], p_top[1]))
    h = max(1.0, depth_below + float(p_top[2]))  # vertical gap from top to the bed below it
    # Touchdown guess a fraction of the hang height along the azimuth.
    r = max(2.0, drop_frac * h)
    td_xy = (p_top[0] + ux * r, p_top[1] + uy * r)
    td = np.array([td_xy[0], td_xy[1], -float(bathy.depth_at(*td_xy))])
    chord = float(np.linalg.norm(td - p_top))
    L_susp = min(float(length_m), 1.03 * chord + 0.02 * h)
    run = max(0.0, float(length_m) - L_susp)
    parts = [catenary_seed(p_top, td, max(L_susp, chord * (1.0 + 1e-6)), 24)]
    if run > 1.0:
        parts.append(bed_polyline(td_xy, azimuth_deg, run, bathy)[1:])
    shape = np.vstack(parts)
    return resample_polyline(shape, n_elems)
