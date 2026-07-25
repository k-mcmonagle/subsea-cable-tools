# -*- coding: utf-8 -*-
"""Quick analytic BU-deployment model (tri-catenary equilibrium).

Pure Python + NumPy; no Qt/QGIS imports.

An interactive-speed alternative to the dynamic-relaxation solver for the
branching-unit scenarios: every suspended line is a **closed-form planar
catenary** and the only unknown per time step is the BU position, found by
a damped Newton iteration on the three-line force balance. Each solve is
sub-millisecond, so a full deployment simulates in well under a second.

Model assumptions (vs the full solver):

* Uniform line weight per chain (length-weighted mean of the assembly's
  submerged weight); in-line bodies/joints are ignored.
* No hydrodynamic drag, no current, no rate effects.
* Seabed friction enters as a **frozen-lay** approximation (default on):
  cable on the bed keeps its as-laid polyline (friction is assumed ample to
  hold it in plan position); only the suspended span re-solves each step,
  laying down at / picking up from the touchdown point. Bed tension decays
  from the touchdown as ``T(s) = max(0, H - mu*w*s)`` (display only — the
  frozen geometry does not depend on it). With ``lay_history=False`` the
  legacy behaviour returns: the bed portion runs straight from its anchor
  toward the touchdown with no lay history.
* The seabed under a suspended span is taken at the touchdown point only
  (real bathymetry supplies the depths, but a hump between TDP and anchor
  is not felt by the span).
* Submerged weight is used everywhere (the short in-air part near the
  sheave is treated as submerged).

Use it to explore schedules interactively; confirm with the full solver.

The :class:`QuickOperationSimulator` subclasses the timeline stepper, so
schedules, events (transfer / overboard), payout controllers and snapshots
behave identically to the full model — only the equilibrium backend
differs.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from .cable_system import resolve_assembly
from .seeds import solve_catenary_param
from .solver3d import _radii_array
from .timeline import (
    ChainSnapshot,
    ChainState,
    OperationSimulator,
    Snapshot,
    Step,
    count_at_m,
    joint_point,
    joint_points,
)


# ---------------------------------------------------------------------------
# Planar catenary primitives
# ---------------------------------------------------------------------------

def deployed_items(chain: ChainState) -> List:
    """The chain's assembly rows ordered from its BOTTOM (material zero) end.

    Remainder ("fill") rows are resolved against the deployed length and the
    assembly datum is honoured, so the same rows the full solver maps are the
    ones averaged here.
    """
    items, direction = chain.oriented_assembly()
    items, _fit = resolve_assembly(items, float(chain.length_m))
    if direction == "from_bottom":
        items = items[::-1]
    return items


def mean_weight_npm(chain: ChainState) -> float:
    """Length-weighted mean submerged weight of the chain's assembly over
    its deployed length, measured from the bottom (material end).

    A segment weight of ``0.0`` means "blank — use the defaults", matching
    :class:`cable_system.AssemblyMapper`. Raises for a non-sinking mean
    weight: a buoyant line has no hanging catenary, so the quick model
    cannot represent it (use the full solver).
    """
    remaining = max(1.0, float(chain.length_m))
    items = deployed_items(chain)
    total_w = 0.0
    total_l = 0.0
    for it in items:
        length = float(getattr(it, "length_m", 0.0) or 0.0)
        if length <= 0.0:
            continue  # bodies / zero-length rows
        q = float(getattr(it, "q_water_npm", 0.0) or 0.0)
        if q == 0.0:
            q = float(chain.defaults.q_water_npm)
        take = min(length, remaining - total_l)
        if take <= 0.0:
            break
        total_w += q * take
        total_l += take
        if total_l >= remaining:
            break
    w = total_w / total_l if total_l > 0.0 else float(chain.defaults.q_water_npm)
    if w <= 0.1:
        raise ValueError(
            f"Quick model: chain '{chain.name}' has a non-sinking mean "
            f"submerged weight ({w:.2f} N/m) — a buoyant/neutral line has "
            "no hanging catenary. Use the Full or Draft model, or check "
            "the assembly weights.")
    return w


def mean_friction_mu(chain: ChainState) -> float:
    """Length-weighted mean seabed friction coefficient over the deployed
    length (mirror of :func:`mean_weight_npm`; blank = chain defaults)."""
    remaining = max(1.0, float(chain.length_m))
    items = deployed_items(chain)
    total_mu = 0.0
    total_l = 0.0
    for it in items:
        length = float(getattr(it, "length_m", 0.0) or 0.0)
        if length <= 0.0:
            continue
        mu = getattr(it, "friction_mu", None)
        if mu is None:
            mu = float(chain.defaults.mu)
        take = min(length, remaining - total_l)
        if take <= 0.0:
            break
        total_mu += float(mu) * take
        total_l += take
        if total_l >= remaining:
            break
    return total_mu / total_l if total_l > 0.0 else float(chain.defaults.mu)


def _solve_beta(D: float, h: float) -> float:
    """Solve ``h/D = (cosh(beta) - 1) / beta`` for beta = D/a (tangent
    catenary of horizontal span D rising h). Monotonic in beta."""
    ratio = h / D
    lo, hi = 1e-9, 1.0
    def g(b):
        return (math.cosh(b) - 1.0) / b
    for _ in range(60):
        if g(hi) >= ratio:
            break
        hi *= 2.0
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        if g(mid) < ratio:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def tangent_catenary(D: float, h: float, w: float) -> Tuple[float, float, float]:
    """Catenary tangent to the bed at the TDP, spanning horizontal ``D`` and
    rising ``h`` to the top. Returns (H, s, T_top): horizontal tension,
    suspended length, top tension magnitude."""
    if D <= 1e-6:
        return 0.0, h, w * h
    beta = _solve_beta(D, h)
    a = D / beta
    H = w * a
    s = a * math.sinh(beta)
    T_top = w * (a + h)
    return H, s, T_top


# Axial stiffness of the taut-line constraint, per unit line weight. High
# enough that a taut trunk stretches only centimetres under a BU load, low
# enough for stable Newton steps with finite-difference Jacobians.
TAUT_K_PER_W = 1.0e4


def clamp_to_bed(xyz: "np.ndarray", bathy) -> Tuple["np.ndarray", "np.ndarray"]:
    """Clamp nodes that dipped below the seabed up onto it.

    Display-level bed contact for spans solved without a bed test (the
    two-point catenaries): the force balance is unchanged, but a rendered
    polyline never penetrates the bed. Returns ``(xyz, clamped_mask)``.
    """
    xyz = np.asarray(xyz, dtype=float).copy()
    bed = -np.asarray(bathy.depth_at(xyz[:, 0], xyz[:, 1]), dtype=float)
    below = xyz[:, 2] < bed
    if np.any(below):
        xyz[below, 2] = bed[below]
    return xyz, below


def two_point_catenary(p0: "np.ndarray", p1: "np.ndarray", L: float, w: float,
                       n: int = 30) -> dict:
    """Closed-form catenary from ``p0`` to ``p1`` with arc length ``L``.

    ``T0_vec`` / ``T1_vec`` are the forces the line exerts on whatever
    holds each end (a sagging span pulls both supports downward; a taut
    line pulls them toward each other). A taut line (``L <= chord``) is
    modelled as a stiff axial spring so the inextensible length constraint
    produces a finite, steep restoring force.
    """
    p0 = np.asarray(p0, dtype=float)
    p1 = np.asarray(p1, dtype=float)
    dxy = p1[:2] - p0[:2]
    D = float(np.hypot(dxy[0], dxy[1]))
    V = float(p1[2] - p0[2])
    chord = math.hypot(D, V)
    L_in = float(L)
    if L_in <= chord * (1.0 + 1e-9):
        # Taut: straight line + stiff axial spring, weight split between
        # the ends by height (upper end carries more).
        e = (p1 - p0) / max(chord, 1e-9)
        T_ax = TAUT_K_PER_W * w * max(0.0, chord - L_in)
        Wl = w * L_in
        up_frac = 0.5 + 0.5 * (abs(V) / max(chord, 1e-9))
        t = np.linspace(0.0, 1.0, n + 1)
        pts = p0[None, :] + (p1 - p0)[None, :] * t[:, None]
        if V >= 0.0:   # p1 is the upper end
            t0_vec = e * T_ax + np.array([0.0, 0.0, -(1.0 - up_frac) * Wl])
            t1_vec = -e * T_ax + np.array([0.0, 0.0, -up_frac * Wl])
        else:
            t0_vec = e * T_ax + np.array([0.0, 0.0, -up_frac * Wl])
            t1_vec = -e * T_ax + np.array([0.0, 0.0, -(1.0 - up_frac) * Wl])
        T0m = float(np.linalg.norm(t0_vec))
        T1m = float(np.linalg.norm(t1_vec))
        t_nodes = T0m + (T1m - T0m) * t
        return {"T0_vec": t0_vec, "T1_vec": t1_vec, "xyz": pts,
                "tension": np.maximum(t_nodes, 0.0), "H": T_ax}
    L = max(L_in, chord * (1.0 + 1e-6))
    if D < max(1e-6, 1e-4 * L):
        # Slack vertical hang: the upper end carries the full line weight,
        # the lower end nothing.
        zs = np.linspace(p0[2], p1[2], n + 1)
        pts = np.column_stack([np.full(n + 1, p0[0]), np.full(n + 1, p0[1]), zs])
        up_is_1 = p1[2] >= p0[2]
        T_low, T_high = 0.0, w * L
        if up_is_1:
            t_nodes = np.linspace(T_low, T_high, n + 1)
            t0_vec = np.array([0.0, 0.0, T_low])      # weightless lower end
            t1_vec = np.array([0.0, 0.0, -T_high])    # carries the line
        else:
            t_nodes = np.linspace(T_high, T_low, n + 1)
            t0_vec = np.array([0.0, 0.0, -T_high])
            t1_vec = np.array([0.0, 0.0, T_low])
        return {"T0_vec": t0_vec, "T1_vec": t1_vec, "xyz": pts,
                "tension": np.abs(t_nodes), "H": 0.0}
    a = solve_catenary_param(D, V, L)
    xbar = a * math.atanh(max(-1.0 + 1e-12, min(1.0 - 1e-12, V / L)))
    xv = D / 2.0 - xbar          # vertex x measured from p0 along the span
    H = w * a
    u = dxy / D                  # horizontal unit vector p0 -> p1

    xs = np.linspace(0.0, D, n + 1)
    zs = p0[2] + a * (np.cosh((xs - xv) / a) - math.cosh((0.0 - xv) / a))
    pts = np.column_stack([p0[0] + u[0] * xs, p0[1] + u[1] * xs, zs])
    pts[0] = p0
    pts[-1] = p1
    slopes = np.sinh((xs - xv) / a)
    t_nodes = H * np.sqrt(1.0 + slopes * slopes)

    # Tension vector at p0 pointing along the line toward p1 (+x direction).
    s0 = math.sinh((0.0 - xv) / a)
    t0_vec = np.array([H * u[0], H * u[1], H * s0])
    # Tension vector at p1 pointing along the line toward p0 (-x direction).
    s1 = math.sinh((D - xv) / a)
    t1_vec = np.array([-H * u[0], -H * u[1], -H * s1])
    return {"T0_vec": t0_vec, "T1_vec": t1_vec, "xyz": pts,
            "tension": t_nodes, "H": H}


def leg_solution(p_top: "np.ndarray", anchor_xy: Tuple[float, float],
                 bathy, L: float, w: float, n_susp: int = 30,
                 n_bed: int = 16) -> dict:
    """A line hanging from ``p_top`` that touches down and runs along the
    bed to a fixed ``anchor_xy``.

    Geometry: the bed portion runs straight from the anchor toward the
    hang; the suspended portion is a tangent catenary. Solved by a 1D
    root-find on the bed-run length. Falls back to a free two-point
    catenary when the line is too short to touch down.

    Returns: force vector the line applies to ``p_top`` (``F_top``), the
    polyline, per-node tensions, contact mask, TDP (or None) and the
    residual bottom tension H.
    """
    p_top = np.asarray(p_top, dtype=float)
    ax, ay = float(anchor_xy[0]), float(anchor_xy[1])
    dxy = p_top[:2] - np.array([ax, ay])
    R = float(np.hypot(dxy[0], dxy[1]))
    u = dxy / R if R > 1e-9 else np.array([1.0, 0.0])
    anchor_z = -float(bathy.depth_at(ax, ay))

    def bed_z_at(t):
        x, y = ax + u[0] * t, ay + u[1] * t
        return x, y, -float(bathy.depth_at(x, y))

    def h_of(t):
        return p_top[2] - bed_z_at(t)[2]

    # Closure f(t) = t + s(t) - L, with TDP at bed distance t from anchor.
    def closure(t):
        D = max(0.0, R - t)
        h = max(0.05, h_of(t))
        _H, s, _T = tangent_catenary(D, h, w)
        return t + s - L

    h0 = max(0.05, h_of(min(R, L)))
    if closure(0.0) > 0.0:
        # Too short to touch down tangentially: free span to the anchor.
        cat = two_point_catenary(p_top, np.array([ax, ay, anchor_z]), L, w,
                                 n=n_susp)
        xyz, contact = clamp_to_bed(cat["xyz"], bathy)
        contact[-1] = True
        return {"F_top": cat["T0_vec"], "xyz": xyz,
                "tension": cat["tension"], "contact": contact,
                "tdp": None, "H_bottom": float(cat["tension"][-1])}

    t_hi = min(R, L)
    if closure(t_hi) <= 0.0:
        # Surplus length pools at the hang: vertical drop + full bed run.
        t_star = t_hi
    else:
        lo, hi = 0.0, t_hi
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if closure(mid) <= 0.0:
                lo = mid
            else:
                hi = mid
        t_star = 0.5 * (lo + hi)

    tx, ty, tz = bed_z_at(t_star)
    D = max(0.0, R - t_star)
    h = max(0.05, p_top[2] - tz)
    H, s, T_top = tangent_catenary(D, h, w)

    # Suspended polyline: tangent catenary from TDP up to p_top.
    if D > 1e-6:
        a = H / w
        xs = np.linspace(0.0, D, n_susp + 1)
        zs = tz + a * (np.cosh(xs / a) - 1.0)
        e = (p_top[:2] - np.array([tx, ty])) / D
        pts_s = np.column_stack([tx + e[0] * xs, ty + e[1] * xs, zs])
        t_sus = H * np.sqrt(1.0 + np.sinh(xs / a) ** 2)
    else:
        zs = np.linspace(tz, p_top[2], n_susp + 1)
        pts_s = np.column_stack([np.full(n_susp + 1, tx),
                                 np.full(n_susp + 1, ty), zs])
        t_sus = w * (zs - tz)
    pts_s[-1] = p_top

    # Bed polyline TDP -> anchor.
    ts = np.linspace(t_star, 0.0, n_bed + 1)[1:]
    bx = ax + u[0] * ts
    by = ay + u[1] * ts
    bz = -np.asarray(bathy.depth_at(bx, by), dtype=float)
    pts_b = np.column_stack([bx, by, bz])
    t_bed = np.full(len(pts_b), H)

    xyz = np.vstack([pts_s[::-1], pts_b])       # top -> TDP -> anchor
    tension = np.concatenate([t_sus[::-1], t_bed])
    contact = np.zeros(len(xyz), dtype=bool)
    contact[n_susp:] = True

    # Force ON the top point: horizontal H toward the TDP, weight of the
    # suspended part downward.
    e_td = (np.array([tx, ty]) - p_top[:2])
    nrm = float(np.hypot(e_td[0], e_td[1]))
    e_td = e_td / nrm if nrm > 1e-9 else np.zeros(2)
    F_top = np.array([H * e_td[0], H * e_td[1], -w * s])
    return {"F_top": F_top, "xyz": xyz, "tension": tension,
            "contact": contact, "tdp": (tx, ty, tz), "H_bottom": H}


# ---------------------------------------------------------------------------
# Frozen-lay history (seabed friction approximation)
# ---------------------------------------------------------------------------

def _path_length(pts: "np.ndarray") -> float:
    if len(pts) < 2:
        return 0.0
    return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))


def leg_solution_frozen(p_top: "np.ndarray", path_pts: "np.ndarray",
                        L: float, w: float, bathy, mu: float,
                        ds: float = 3.0, n_susp: int = 30) -> dict:
    """A line hanging from ``p_top`` onto a FROZEN as-laid bed polyline.

    ``path_pts`` is the laid path, anchor-first, ending at the current
    touchdown. Friction is assumed ample to hold laid cable in plan
    position, so only the touchdown end evolves: surplus suspended length
    lays new points down (from the TDP toward the hang), a deficit picks
    points back up. The suspended span is the tangent catenary from the
    (possibly moved) TDP to ``p_top``; bed tension decays from the TDP as
    ``max(0, H - mu*w*s)``.

    Pure function: the (possibly grown/shrunk) path is returned as
    ``path_pts`` in the result — the caller decides whether to commit it,
    so equilibrium iterations (the BU Newton loop) stay side-effect free.
    Returns the same keys as :func:`leg_solution` plus ``path_pts``;
    ``path_pts`` is None when the line lifted fully off the bed.

    Touchdown rules (the essence of the friction model):

    * surplus over the tangent-catenary length lays new cable down;
    * a deficit peels cable back off the bed until the span is tangent
      again. Tangency at the touchdown is a *vertical* equilibrium
      requirement — the bed can only push up, so a span meeting laid cable
      at an angle would lift it regardless of friction. Friction acts in
      plan only: it keeps the laid polyline frozen in place and sets the
      bed tension decay; it does not hold the touchdown against peel.
      (An earlier chord-based rule held the touchdown until the span went
      straight — that rendered an unphysical kink at the TDP with a
      tension spike whenever the vessel moved away, and trapped slack
      states in a permanent near-vertical hang.)
    """
    p_top = np.asarray(p_top, dtype=float)
    pts = np.asarray(path_pts, dtype=float).reshape(-1, 3).copy()
    s_laid = _path_length(pts)
    s_free = float(L) - s_laid

    def geom(tdp):
        D = float(np.hypot(p_top[0] - tdp[0], p_top[1] - tdp[1]))
        h = max(0.05, float(p_top[2] - tdp[2]))
        H, s_tan, _T = tangent_catenary(D, h, w)
        chord = math.hypot(D, float(p_top[2] - tdp[2]))
        return D, h, H, s_tan, chord

    # Walk the touchdown: lay down surplus; peel a deficit back off the bed
    # until the span is tangent again (see docstring). A peeled segment can
    # overshoot tangency (path segments may be longer than ds); the surplus
    # branch then lays the excess back in fine steps, so the walk settles
    # inside the +/- 0.5*ds band around tangency.
    pooled = False
    max_iter = int(abs(s_free) / max(ds, 0.1)) + 2 * len(pts) + 64
    for _ in range(max_iter):
        tdp = pts[-1]
        D, h, H, s_tan, chord = geom(tdp)
        if s_free > s_tan + 0.5 * ds:
            if D <= max(ds, 1e-6):
                pooled = True     # no horizontal room: surplus hangs slack
                break
            u = (p_top[:2] - tdp[:2]) / D
            step = min(ds, s_free - s_tan)
            nx, ny = tdp[0] + u[0] * step, tdp[1] + u[1] * step
            new = np.array([nx, ny, -float(bathy.depth_at(nx, ny))])
            s_free -= float(np.linalg.norm(new - tdp))
            pts = np.vstack([pts, new])
        elif s_free < s_tan - 0.5 * ds and len(pts) > 1:
            s_free += float(np.linalg.norm(pts[-1] - pts[-2]))
            pts = pts[:-1]
        else:
            break

    if len(pts) == 1 and s_free < geom(pts[0])[4] * (1.0 + 1e-6):
        # Path exhausted and still taut: the whole line is suspended down to
        # the anchor — same fallback as the frictionless model.
        anchor = pts[0]
        cat = two_point_catenary(p_top, anchor, float(L), w, n=n_susp)
        xyz, contact = clamp_to_bed(cat["xyz"], bathy)
        contact[-1] = True
        return {"F_top": cat["T0_vec"], "xyz": xyz,
                "tension": cat["tension"], "contact": contact,
                "tdp": None, "H_bottom": float(cat["tension"][-1]),
                "path_pts": None}

    tdp = pts[-1]
    D, h, H, s_tan, chord = geom(tdp)
    susp_bed = None
    if not pooled and abs(s_free - s_tan) <= ds and D > 1e-6:
        # At (or close enough to) tangency: classic tangent catenary.
        a = H / w
        xs = np.linspace(0.0, D, n_susp + 1)
        zs = tdp[2] + a * (np.cosh(xs / a) - 1.0)
        e = (p_top[:2] - tdp[:2]) / D
        pts_s = np.column_stack([tdp[0] + e[0] * xs, tdp[1] + e[1] * xs, zs])[::-1]
        t_sus = (H * np.sqrt(1.0 + np.sinh(xs / a) ** 2))[::-1]
        pts_s[0] = p_top
        s_susp = s_tan
        e_td = tdp[:2] - p_top[:2]
        nrm = float(np.hypot(e_td[0], e_td[1]))
        e_td = e_td / nrm if nrm > 1e-9 else np.zeros(2)
        F_top = np.array([H * e_td[0], H * e_td[1], -w * s_susp])
    else:
        # Slack pooling (no horizontal room for a tangent span), or the
        # path is down to its anchor point with a deficit: a two-point
        # catenary from the top to the touchdown carries the state; its
        # lower-end tension is the residual bottom tension and its exact
        # end force loads the top.
        cat = two_point_catenary(p_top, tdp, max(s_free, 0.05), w, n=n_susp)
        pts_s, susp_bed = clamp_to_bed(cat["xyz"], bathy)   # top -> TDP
        t_sus = cat["tension"]
        H = float(t_sus[-1])
        F_top = np.asarray(cat["T0_vec"], dtype=float)

    # Bed run: the frozen path, TDP -> anchor, with friction decay.
    bed = pts[::-1]
    seg = np.linalg.norm(np.diff(bed, axis=0), axis=1)
    s_bed = np.concatenate([[0.0], np.cumsum(seg)])
    t_bed = np.maximum(0.0, H - mu * w * s_bed)

    xyz = np.vstack([pts_s, bed[1:]])           # top -> TDP -> anchor
    tension = np.concatenate([t_sus, t_bed[1:]])
    contact = np.zeros(len(xyz), dtype=bool)
    contact[len(pts_s) - 1:] = True
    if susp_bed is not None and np.any(susp_bed):
        contact[:len(pts_s)] |= susp_bed        # clamped sag counts as contact

    return {"F_top": F_top, "xyz": xyz, "tension": tension,
            "contact": contact, "tdp": (float(tdp[0]), float(tdp[1]), float(tdp[2])),
            "H_bottom": H, "path_pts": pts}


# ---------------------------------------------------------------------------
# Quick operation simulator
# ---------------------------------------------------------------------------

class QuickOperationSimulator(OperationSimulator):
    """Timeline stepper with the analytic tri-catenary equilibrium backend.

    Reuses all scripting machinery (steps, substeps, events, sheave
    transfer, payout controller, snapshots); only the per-substep solve is
    replaced. Supports the BU scenario family: chains topped on a vessel /
    sheave / the ``BU`` junction, with fixed or junction bottom ends.
    """

    JUNCTION = "BU"

    def __init__(self, scenario, bathy, options=None, lay_history: bool = True):
        super().__init__(scenario, bathy, options)
        self._landed = False
        # Frozen-lay history: per-chain as-laid bed polyline (anchor-first).
        self.lay_history = bool(lay_history)
        self._paths: Dict[str, "np.ndarray"] = {}

    def reset_lay_history(self):
        self._paths = {}

    def _solve_leg(self, st: ChainState, top: "np.ndarray",
                   anchor_xy: Tuple[float, float], n_susp: int = 30,
                   n_bed: int = 16, commit: bool = False) -> dict:
        """Solve one bed-contacting line, via the frozen as-laid path when
        lay history is on and a path exists; falls back to (and, on commit,
        initialises the path from) the frictionless closed form."""
        w = mean_weight_npm(st)
        if self.lay_history:
            path = self._paths.get(st.name)
            if path is not None and len(path) >= 1:
                sol = leg_solution_frozen(top, path, st.length_m, w,
                                          self.bathy, mean_friction_mu(st),
                                          n_susp=n_susp)
                if commit:
                    if sol["path_pts"] is None:
                        self._paths.pop(st.name, None)   # lifted clear off
                    else:
                        self._paths[st.name] = sol["path_pts"]
                return sol
        sol = leg_solution(top, anchor_xy, self.bathy, st.length_m, w,
                           n_susp=n_susp, n_bed=n_bed)
        if commit and self.lay_history and sol.get("tdp") is not None:
            bed_pts = np.asarray(sol["xyz"])[np.asarray(sol["contact"], dtype=bool)]
            if len(bed_pts) >= 1:
                self._paths[st.name] = np.asarray(bed_pts[::-1], dtype=float)
        return sol

    # -- equilibrium backend ------------------------------------------------

    def settle(self) -> Snapshot:
        snap = self._quick_snapshot(label="settle")
        self._check_assemblies()
        snap.warnings.extend(self.assembly_warnings)
        self._last_snap = snap
        return snap

    def _equilibrate(self, step: Step, dt: float, prev_shapes) -> Snapshot:
        snap = self._quick_snapshot(label=step.label)
        self._check_assemblies()
        self._last_snap = snap
        return snap

    def _check_assemblies(self) -> None:
        """Assembly-vs-length fit warnings (the quick backend never builds a
        mapper, so the base class's per-build check does not fire here)."""
        for name, st in self.sc.chains.items():
            if name in self._released:
                continue
            items, _direction = st.oriented_assembly()
            _items, fit = resolve_assembly(items, float(st.length_m))
            self._check_assembly_fit(st, fit)

    # -- core ---------------------------------------------------------------

    def _bu_weight_N(self) -> float:
        j = self.sc.junctions.get(self.JUNCTION)
        return float(j.load_kN) * 1000.0 if j is not None else 0.0

    def _chain_top_xyz(self, st: ChainState) -> "np.ndarray":
        if st.top.kind in ("vessel", "sheave"):
            return np.asarray(self._top_xyz(st), dtype=float)
        if st.top.kind == "fixed" and st.top.xyz is not None:
            return np.asarray(st.top.xyz, dtype=float)
        raise ValueError(f"quick model cannot hold a chain top by {st.top.kind!r}")

    def _classify(self):
        """Active chains split into junction-tied lines and free-hung legs."""
        trunk = []      # top at vessel/sheave, bottom at the junction
        legs_j = []     # top at the junction, bottom fixed (anchored leg)
        hung = []       # top at vessel/sheave, bottom fixed (pre-overboard leg)
        for name, st in self.sc.chains.items():
            if name in self._released:
                continue
            top_k, bot_k = st.top.kind, st.bottom.kind
            if bot_k == "junction" and st.bottom.junction == self.JUNCTION:
                trunk.append(st)
            elif top_k == "junction" and st.top.junction == self.JUNCTION:
                if st.bottom.kind != "fixed" or st.bottom.xyz is None:
                    raise ValueError("quick model needs fixed leg far ends")
                legs_j.append(st)
            elif top_k in ("vessel", "sheave") and bot_k == "fixed" and st.bottom.xyz is not None:
                hung.append(st)
            else:
                raise ValueError(
                    f"quick model does not support chain '{name}' "
                    f"({top_k} -> {bot_k}); use the full solver.")
        return trunk, legs_j, hung

    def _bu_force(self, p: "np.ndarray", trunk, legs) -> "np.ndarray":
        """Net force on the BU at position p (N)."""
        F = np.array([0.0, 0.0, -self._bu_weight_N()])
        for st in trunk:
            top = self._chain_top_xyz(st)
            w = mean_weight_npm(st)
            cat = two_point_catenary(p, top, st.length_m, w, n=8)
            F += cat["T0_vec"]
        for st in legs:
            sol = self._solve_leg(st, p, st.bottom.xyz[:2],
                                  n_susp=8, n_bed=2, commit=False)
            F += sol["F_top"]
        return F

    def _solve_bu(self, trunk, legs) -> "np.ndarray":
        j = self.sc.junctions[self.JUNCTION]
        p = np.asarray(j.xyz, dtype=float).copy()
        bed = lambda q: -float(self.bathy.depth_at(q[0], q[1]))
        if self._landed:
            p[2] = bed(p)
            return p

        def clip(q):
            # Iterates are projected onto/above the bed; landing is decided
            # only from the converged solution, never a Newton overshoot.
            q = q.copy()
            q[2] = max(q[2], bed(q))
            return q

        scale = max(self._bu_weight_N(), 1e3)
        h_fd = 0.05
        p = clip(p)
        F = self._bu_force(p, trunk, legs)
        for _ in range(50):
            fn = float(np.linalg.norm(F))
            if fn < 1e-3 * scale:
                break
            J = np.zeros((3, 3))
            for k in range(3):
                dp = np.zeros(3)
                dp[k] = h_fd
                J[:, k] = (self._bu_force(clip(p + dp), trunk, legs) - F) / h_fd
            try:
                step_v = np.linalg.solve(J, -F)
            except np.linalg.LinAlgError:
                step_v = -F * (1.0 / max(1.0, float(np.linalg.norm(J))))
            nrm = float(np.linalg.norm(step_v))
            if nrm > 5.0:
                step_v *= 5.0 / nrm
                nrm = 5.0
            # Backtracking line search: never accept a step that grows the
            # residual much (the taut-line stiffness makes full Newton
            # steps overshoot).
            accepted = False
            for _bt in range(4):
                p_try = clip(p + step_v)
                F_try = self._bu_force(p_try, trunk, legs)
                if float(np.linalg.norm(F_try)) < fn * 1.5:
                    p, F = p_try, F_try
                    accepted = True
                    break
                step_v *= 0.5
            if not accepted:
                p = clip(p + step_v)   # smallest step; keep moving
                F = self._bu_force(p, trunk, legs)
            if nrm < 1e-4:
                break
        # Landed only if the converged position rests on the bed and the
        # net line+weight force still pushes it down (the bed reaction
        # closes the balance).
        if p[2] <= bed(p) + 0.05 and F[2] <= 0.0:
            p[2] = bed(p)
            self._landed = True
        return p

    def _quick_snapshot(self, label: str = "") -> Snapshot:
        trunk, legs, hung = self._classify()
        warnings: List[str] = []
        chains: List[ChainSnapshot] = []
        junction_xyz: Dict[str, Tuple[float, float, float]] = {}

        if self.JUNCTION in self.sc.junctions and (trunk or legs):
            p = self._solve_bu(trunk, legs)
            self.sc.junctions[self.JUNCTION].xyz = (float(p[0]), float(p[1]), float(p[2]))
            junction_xyz[self.JUNCTION] = self.sc.junctions[self.JUNCTION].xyz
            for st in trunk:
                top = self._chain_top_xyz(st)
                w = mean_weight_npm(st)
                if self._landed:
                    # Trunk lays down toward the BU on the bed (its as-laid
                    # path is anchored at the landed BU position).
                    sol = self._solve_leg(st, top, (p[0], p[1]), commit=True)
                    chains.append(self._chain_snap(st, sol["xyz"], sol["tension"],
                                                   sol["contact"]))
                else:
                    cat = two_point_catenary(p, top, st.length_m, w)
                    xyz = cat["xyz"][::-1]          # top -> BU ordering
                    tension = cat["tension"][::-1]
                    # A slack trunk can sag below the bed just before the
                    # BU lands — clamp the sag onto the bed for display.
                    xyz, contact = clamp_to_bed(xyz, self.bathy)
                    chains.append(self._chain_snap(st, xyz, tension, contact))
            for st in legs:
                sol = self._solve_leg(st, p, st.bottom.xyz[:2], commit=True)
                chains.append(self._chain_snap(st, sol["xyz"], sol["tension"],
                                               sol["contact"]))
        for st in hung:
            top = self._chain_top_xyz(st)
            sol = self._solve_leg(st, top, st.bottom.xyz[:2], commit=True)
            chains.append(self._chain_snap(st, sol["xyz"], sol["tension"],
                                           sol["contact"]))

        # Keep the scenario shapes current (seeds a later full-solver run).
        for c in chains:
            stc = self.sc.chains.get(c.name)
            if stc is not None:
                stc.shape = c.xyz.copy()

        return Snapshot(
            t_s=self._t,
            vessel_xy=tuple(self.sc.vessel_xy),
            vessel_heading_deg=self.sc.vessel_heading_deg,
            chains=chains,
            junction_xyz=junction_xyz,
            converged=True,
            residual_ratio=0.0,
            warnings=warnings,
            label=label,
            payout_mps=dict(self._applied_payout),
        )

    def _chain_snap(self, st: ChainState, xyz: "np.ndarray",
                    tension_N: "np.ndarray", contact: "np.ndarray") -> ChainSnapshot:
        xyz = np.asarray(xyz, dtype=float)
        seg = np.linalg.norm(np.diff(xyz, axis=0), axis=1)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        t_kN = np.asarray(tension_N, dtype=float) / 1000.0
        radii = _radii_array(xyz)
        return ChainSnapshot(
            name=st.name,
            xyz=xyz,
            s=s,
            tension_kN=t_kN,
            contact=np.asarray(contact, dtype=bool),
            seg_id=np.zeros(max(1, len(xyz) - 1), dtype=int),
            top_tension_kN=float(t_kN[0]),
            end_tension_kN=float(t_kN[-1]),
            min_radius_m=float(np.min(radii)) if len(radii) else float("inf"),
            length_m=float(st.length_m),
            joint_xyz=joint_point(st, xyz, s),
            joints_xyz=joint_points(st, xyz, s),
            count_top_m=count_at_m(st, float(st.length_m)),
        )
