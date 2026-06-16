# -*- coding: utf-8 -*-
"""Multi-span static drape solver for a cable over a profiled seabed.

Pure Python + NumPy; no Qt/QGIS imports.

Method
------
Lumped-node static equilibrium found by dynamic relaxation (DR) with kinetic
damping (Barnes/Underwood). The cable is a chain of N segments with stiff
axial springs (near-inextensible, tension-only so slack is represented
correctly), distributed weight per node, optional point loads, unilateral
seabed contact via a penalty normal force, Coulomb friction via a penalty
stick-slip anchor model (scalar or per-segment friction coefficient), and
optional **bending stiffness** via discrete three-node moments
(``M = EI * kappa`` with the standard lumped-mass curvature
``kappa = 2*theta / (L1 + L2)``, energy-consistent nodal forces).

This formulation makes the same structural assumptions as the single-span
catenary solver (static; 2D in a vertical plane; no hydrodynamic loading)
but supports **multiple free spans and contact regions** over an arbitrary
seabed profile, an anchored or free bottom end, seabed friction, and a
finite EI. With ``EI_Nm2 = 0`` the cable is perfectly flexible, matching the
single-span solver's idealisation.

Frame convention (matches ``catenary_solver`` world frame):
  * ``x`` — horizontal, increasing from the chute outward (chute at x ≈ 0).
  * ``y`` — vertical, 0 at sea surface, negative down. Bed elevation is
    ``y_bed(x) = -seabed.depth_at(x)``.

Accuracy and limitations
------------------------
* Discretisation error scales with the segment length (L/n_nodes); contact
  lift-off/touchdown points are resolved to ~one node spacing.
* The axial stiffness is auto-tuned so elastic stretch is < 0.05 % — results
  represent the inextensible limit, consistent with the catenary solver.
* With friction (mu > 0) the static equilibrium of a cable is generally
  **non-unique** (it depends on lay history). The state returned here is one
  admissible equilibrium reached from the initial geometry; treat
  friction-sensitive outputs as indicative bounds, not unique answers.
* A frictionless bed cannot react horizontal force: a free (un-anchored)
  bottom end with mu == 0 everywhere has no equilibrium under bottom tension
  and is rejected.
* Bending stiffness is resolved only down to the node spacing. The bending
  boundary layer has characteristic length ``lambda = sqrt(EI/T)``; when
  ``lambda`` is smaller than the segment length (typical for telecom cables,
  where lambda is well under a metre) the EI forces are negligible at this
  discretisation and the shape correctly degenerates to the flexible
  catenary — which is also the physically correct limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import List, Optional, Sequence, Tuple, Union

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


@dataclass
class FreeSpan:
    """A suspended region between two seabed-contact regions (or ends)."""

    s_start_m: float          # arc length from the top end
    s_end_m: float
    length_m: float
    x_start_m: float
    x_end_m: float
    max_clearance_m: float    # largest gap to the bed within the span
    min_radius_m: float       # minimum bend radius within the span (inf if straight)
    max_tension_kN: float


@dataclass
class DrapeResult:
    x: "np.ndarray"                  # node x (world frame)
    y: "np.ndarray"                  # node y (0 at surface, negative down)
    s: "np.ndarray"                  # arc length from top end (rest length)
    tension_kN: "np.ndarray"         # per-node tension (kN)
    contact: "np.ndarray"            # bool per node: resting on the bed
    clearance_m: "np.ndarray"        # vertical gap to bed (>=0 above bed)
    spans: List[FreeSpan] = field(default_factory=list)
    top_tension_kN: float = 0.0
    top_angle_deg: float = 0.0       # from horizontal, at the top end
    end_tension_kN: float = 0.0      # at the bottom end (anchor or free end)
    converged: bool = False
    iterations: int = 0
    residual_ratio: float = float("inf")  # max residual force / reference force
    max_penetration_m: float = 0.0
    min_radius_m: float = float("inf")    # global min bend radius (incl. contact)
    min_radius_s_m: float = 0.0           # arc position of the minimum radius
    warnings: List[str] = field(default_factory=list)

    def tension_at_s(self, s_query_m: float) -> float:
        return float(np.interp(float(s_query_m), self.s, self.tension_kN))

    def radius_at_s(self, s_query_m: float) -> float:
        """Bend radius at an arc position (inf on straight/contact regions)."""
        idx = int(np.argmin(np.abs(self.s - float(s_query_m))))
        return _three_point_radius(self.x, self.y, idx)


def _three_point_radius(x: "np.ndarray", y: "np.ndarray", i: int) -> float:
    if i <= 0 or i >= len(x) - 1:
        return float("inf")
    ax, ay = x[i - 1], y[i - 1]
    bx, by = x[i], y[i]
    cx, cy = x[i + 1], y[i + 1]
    a = math.hypot(bx - ax, by - ay)
    b = math.hypot(cx - bx, cy - by)
    c = math.hypot(cx - ax, cy - ay)
    area2 = abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))  # 2*Area
    if area2 < 1e-12:
        return float("inf")
    return float(a * b * c / (2.0 * area2))


def solve_drape(
    seabed,
    top_xy: Tuple[float, float],
    cable_length_m: float,
    q_water_npm: Union[float, Sequence[float]],
    q_air_npm: float = 0.0,
    point_loads: Optional[List[Tuple[float, float]]] = None,
    mu: Union[float, Sequence[float]] = 0.0,
    bottom_anchor_xy: Optional[Tuple[float, float]] = None,
    n_nodes: int = 400,
    tension_scale_N: float = 0.0,
    tol: float = 2e-3,
    max_iters: int = 120000,
    initial_shape: Optional[Tuple["np.ndarray", "np.ndarray"]] = None,
    EI_Nm2: Union[float, Sequence[float]] = 0.0,
) -> DrapeResult:
    """Solve the static drape of a cable over a profiled seabed.

    Parameters
    ----------
    seabed:
        Object with ``depth_at(x_world) -> positive depth`` (the
        ``SeabedProfile`` implementations from ``catenary_solver``).
    top_xy:
        Fixed position of the top end (chute departure point), world frame.
    cable_length_m:
        Total unstretched cable length from the top end.
    q_water_npm:
        Submerged weight per metre (N/m). Either a scalar or a per-segment
        array of length ``n_nodes`` (ordered from the top end). Negative
        values are net buoyancy.
    q_air_npm:
        In-air weight per metre, applied where a node is above the surface.
        When 0, ``q_water`` is used everywhere.
    point_loads:
        List of ``(s_from_top_m, load_kN)``; positive = downward.
    mu:
        Coulomb friction coefficient cable/seabed. Either a scalar or a
        per-segment array of length ``n_nodes`` (ordered from the top end),
        e.g. for assemblies whose segments have different outer coverings.
    bottom_anchor_xy:
        Fix the bottom end here (e.g. on the bed at the far end). When
        ``None`` the bottom end is free — requires ``mu > 0``.
    n_nodes:
        Number of segments (there are ``n_nodes + 1`` nodes).
    tension_scale_N:
        Expected tension magnitude used to auto-tune stiffnesses; estimated
        from the weight when 0.
    tol:
        Convergence tolerance: max residual force on free nodes divided by
        the reference nodal force.
    initial_shape:
        Optional ``(x, y)`` arrays (n_nodes+1) to start from, e.g. the
        single-span solution. A straight-to-bed initial guess is used
        otherwise.
    EI_Nm2:
        Bending stiffness EI in N·m². Either a scalar or a per-segment
        array of length ``n_nodes`` (ordered from the top end). 0 (default)
        = perfectly flexible. At a material transition, joint stiffness is
        compliance-weighted from the adjacent segment EIs.
    """
    if np is None:
        raise ImportError("NumPy is required for the drape solver.")
    if cable_length_m <= 0:
        raise ValueError("cable_length_m must be > 0.")
    if n_nodes < 10:
        raise ValueError("n_nodes must be >= 10.")

    warnings: List[str] = []
    n_seg = int(n_nodes)
    n_pts = n_seg + 1
    L0 = float(cable_length_m) / n_seg  # rest length per segment

    # Per-segment weights (N/m), ordered from the top end.
    qw = np.full(n_seg, float(q_water_npm)) if np.isscalar(q_water_npm) else np.asarray(q_water_npm, dtype=float)
    if qw.shape[0] != n_seg:
        raise ValueError("q_water_npm array must have length n_nodes.")
    qa = float(q_air_npm) if q_air_npm else 0.0

    # Per-segment friction coefficients -> per-node values (mean of the
    # adjacent segments, consistent with how segment weight is lumped).
    mu_seg = np.full(n_seg, float(mu)) if np.isscalar(mu) else np.asarray(mu, dtype=float)
    if mu_seg.shape[0] != n_seg:
        raise ValueError("mu array must have length n_nodes.")
    mu_seg = np.maximum(0.0, mu_seg)
    mu_node = np.empty(n_pts)
    mu_node[0] = mu_seg[0]
    mu_node[-1] = mu_seg[-1]
    mu_node[1:-1] = 0.5 * (mu_seg[:-1] + mu_seg[1:])
    mu_max = float(np.max(mu_seg))
    if bottom_anchor_xy is None and mu_max <= 0.0:
        raise ValueError(
            "A free bottom end on a frictionless bed has no static equilibrium "
            "under bottom tension: anchor the end (bottom_anchor_xy) or set mu > 0."
        )

    # Per-segment bending stiffness (N.m2), ordered from the top end. This
    # mirrors the per-segment weight/friction handling and lets mixed cable
    # assemblies use the supplier EI for each section.
    EI_seg = np.full(n_seg, max(0.0, float(EI_Nm2))) if np.isscalar(EI_Nm2) else np.asarray(EI_Nm2, dtype=float)
    if EI_seg.shape[0] != n_seg:
        raise ValueError("EI_Nm2 array must have length n_nodes.")
    EI_seg = np.maximum(0.0, EI_seg)
    EI_max = float(np.max(EI_seg))

    q_ref = float(max(np.max(np.abs(qw)), 1e-6))
    w_ref = q_ref * L0  # reference nodal force

    # Point loads -> nearest node index (N, downward positive -> -y force).
    s_nodes = np.linspace(0.0, cable_length_m, n_pts)
    F_point = np.zeros(n_pts)
    for s_pt, load_kN in (point_loads or []):
        idx = int(round(float(s_pt) / L0))
        idx = max(0, min(n_pts - 1, idx))
        F_point[idx] += -float(load_kN) * 1000.0  # downward load reduces y-force

    # --- Stiffness auto-tuning -------------------------------------------
    if tension_scale_N <= 0:
        # Weight of the whole cable is a safe lower-bound scale.
        tension_scale_N = max(q_ref * cable_length_m, 1e3)
    # 500x tension scale -> elastic stretch <= 0.2 % at the tension scale.
    # (Stiffer springs would force a heavier DR mass and slower settling for
    # no meaningful gain over the inextensible idealisation.)
    EA = 500.0 * tension_scale_N
    k_axial = EA / L0
    # Contact penalty: target penetration ~5 mm under (nodal weight + a
    # tension-curvature reaction of ~5 % of the tension scale).
    k_contact = (w_ref + 0.05 * tension_scale_N) / 0.005
    k_fric = k_contact

    # --- Initial geometry --------------------------------------------------
    x = np.zeros(n_pts)
    y = np.zeros(n_pts)
    x0, y0 = float(top_xy[0]), float(top_xy[1])
    if initial_shape is not None:
        xi, yi = initial_shape
        if len(xi) != n_pts or len(yi) != n_pts:
            # Resample onto our node count by arc length.
            si = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xi), np.diff(yi)))])
            x = np.interp(s_nodes, si * (cable_length_m / max(si[-1], 1e-9)), xi)
            y = np.interp(s_nodes, si * (cable_length_m / max(si[-1], 1e-9)), yi)
        else:
            x = np.asarray(xi, dtype=float).copy()
            y = np.asarray(yi, dtype=float).copy()
        x[0], y[0] = x0, y0
    else:
        # Straight 45-degree ramp from the top point down to the bed, then
        # follow the bed outward.
        bed_y0 = -float(seabed.depth_at(x0))
        drop = max(1.0, y0 - bed_y0)
        ramp_len = min(cable_length_m, math.hypot(drop, drop))
        for i in range(n_pts):
            s = s_nodes[i]
            if s <= ramp_len:
                f = s / max(ramp_len, 1e-9)
                x[i] = x0 + f * drop
                y[i] = y0 - f * drop
            else:
                x[i] = x0 + drop + (s - ramp_len)
                y[i] = -float(seabed.depth_at(float(x[i])))
        # Never start below the bed.
        bed = -np.array([float(seabed.depth_at(float(xx))) for xx in x])
        y = np.maximum(y, bed)

    anchored = bottom_anchor_xy is not None
    if anchored:
        x[-1], y[-1] = float(bottom_anchor_xy[0]), float(bottom_anchor_xy[1])

    fixed = np.zeros(n_pts, dtype=bool)
    fixed[0] = True
    if anchored:
        fixed[-1] = True

    # Sanity: enough cable to reach the anchor.
    if anchored:
        chord = math.hypot(x[-1] - x[0], y[-1] - y[0])
        if chord > cable_length_m:
            raise ValueError(
                f"Cable length ({cable_length_m:.1f} m) is shorter than the straight distance "
                f"to the anchor ({chord:.1f} m)."
            )

    # --- Precomputed bed table (vectorised lookup) ---------------------------
    # Sample the bed once over every x the cable could possibly reach, then
    # interpolate per iteration. Per-node Python calls to depth_at inside the
    # DR loop would dominate the runtime otherwise.
    bx_lo = min(x0, float(np.min(x))) - cable_length_m - 10.0
    bx_hi = max(x0, float(np.max(x))) + cable_length_m + 10.0
    if anchored:
        bx_lo = min(bx_lo, x[-1] - 10.0)
        bx_hi = max(bx_hi, x[-1] + 10.0)
    bed_dx = max(0.25, min(L0 / 2.0, 2.0))
    bed_tab_x = np.arange(bx_lo, bx_hi + bed_dx, bed_dx)
    bed_tab_y = -np.array([float(seabed.depth_at(float(xx))) for xx in bed_tab_x])
    bed_tab_slope = np.gradient(bed_tab_y, bed_tab_x)

    # --- Dynamic relaxation -------------------------------------------------
    dt = 1.0
    # Fictitious nodal mass for stability (Barnes): m >= dt^2/2 * sum(k).
    # Bending adds an effective transverse stiffness from the discrete-beam
    # 5-point stencil (Gershgorin row sum 16*EI/L^3) plus geometric-rotation
    # terms ~M/L^2 at large transient kink angles; 32*EI/L0^3 keeps DR stable
    # through the worst start-up transients.
    k_bend = 32.0 * EI_max / (L0 ** 3)
    m_node = (dt * dt / 2.0) * (2.0 * k_axial + k_contact + k_fric + k_bend) * 2.0
    v = np.zeros((n_pts, 2))
    fric_anchor = x.copy()  # stick anchor (x along bed) for contact friction
    has_anchor = np.zeros(n_pts, dtype=bool)

    iters_done = 0
    residual_ratio = float("inf")
    check_every = 200
    converged = False

    bed_y = np.empty(n_pts)
    bed_slope = np.empty(n_pts)

    def bed_eval():
        bed_y[:] = np.interp(x, bed_tab_x, bed_tab_y, left=bed_tab_y[0], right=bed_tab_y[-1])
        bed_slope[:] = np.interp(x, bed_tab_x, bed_tab_slope, left=0.0, right=0.0)

    # Rest lengths per segment. Outer rest-length correction: after each DR
    # pass, shrink each rest length by its computed strain so the *stretched*
    # segment length equals the physical length L0. This recovers the
    # inextensible limit (taut systems are extremely tension-sensitive to
    # length error) without the very stiff springs that would cripple DR.
    L0_rest = np.full(n_seg, L0)
    n_outer = 5

    for outer in range(n_outer):
        v[:] = 0.0
        ke_prev = 0.0
        converged = False

        for it in range(int(max_iters)):
            iters_done += 1

            # Segment vectors and tensions (tension-only springs).
            dx = np.diff(x)
            dy = np.diff(y)
            seg_len = np.hypot(dx, dy)
            seg_len = np.maximum(seg_len, 1e-12)
            strain = (seg_len - L0_rest) / L0_rest
            T = np.maximum(0.0, EA * strain)  # N, slack -> 0
            ux = dx / seg_len
            uy = dy / seg_len

            F = np.zeros((n_pts, 2))
            # Axial forces.
            F[:-1, 0] += T * ux
            F[:-1, 1] += T * uy
            F[1:, 0] -= T * ux
            F[1:, 1] -= T * uy

            # Bending stiffness (discrete three-node moments). At inner node
            # i the joint angle theta between adjacent segments gives the
            # lumped-mass curvature kappa = 2*theta/(L1+L2) and a restoring
            # moment M = EI*kappa, applied as the energy-consistent force
            # couple F = -M * dtheta/dP on the three nodes involved.
            if EI_max > 0.0:
                u1x, u1y = ux[:-1], uy[:-1]
                u2x, u2y = ux[1:], uy[1:]
                # Clamp the lever lengths from below: a transiently collapsed
                # (slack) segment must not produce an unbounded moment force.
                L1 = np.maximum(seg_len[:-1], 0.5 * L0)
                L2 = np.maximum(seg_len[1:], 0.5 * L0)
                EI_left = EI_seg[:-1]
                EI_right = EI_seg[1:]
                with np.errstate(divide="ignore", invalid="ignore"):
                    EI_joint = np.where(
                        (EI_left > 0.0) & (EI_right > 0.0),
                        (L1 + L2) / (L1 / EI_left + L2 / EI_right),
                        0.0,
                    )
                theta = np.arctan2(u1x * u2y - u1y * u2x, u1x * u2x + u1y * u2y)
                M_b = EI_joint * 2.0 * theta / (L1 + L2)
                a1 = M_b / L1
                a2 = M_b / L2
                # Normals (perp-left of each segment): n1=(-u1y,u1x), n2=(-u2y,u2x).
                # F[i-1] = -a1*n1, F[i+1] = -a2*n2, F[i] = a1*n1 + a2*n2.
                F[:-2, 0] += a1 * u1y
                F[:-2, 1] += -a1 * u1x
                F[2:, 0] += a2 * u2y
                F[2:, 1] += -a2 * u2x
                F[1:-1, 0] += -(a1 * u1y + a2 * u2y)
                F[1:-1, 1] += a1 * u1x + a2 * u2x

            # Weight (per segment, split to nodes; medium by node y sign).
            # Physical weight uses the physical length L0, not the corrected
            # rest length.
            seg_w = np.where(0.5 * (y[:-1] + y[1:]) < 0.0, qw, (qa if qa else qw)) * L0
            F[:-1, 1] -= 0.5 * seg_w
            F[1:, 1] -= 0.5 * seg_w
            # Point loads.
            F[:, 1] += F_point

            # Seabed contact (penalty) + friction.
            bed_eval()
            pen = bed_y - y  # >0 when node below the bed
            in_contact = pen > 0.0
            if np.any(in_contact):
                cosa = 1.0 / np.sqrt(1.0 + bed_slope * bed_slope)
                sina = bed_slope * cosa
                # Normal (upward) unit vector on the bed: (-sin a, cos a)
                # with a = atan(slope). Normal penetration ~ vertical
                # penetration * cos a.
                Fn = k_contact * pen * cosa
                Fn = np.where(in_contact, Fn, 0.0)
                F[:, 0] += Fn * (-sina)
                F[:, 1] += Fn * cosa

                # Stick-slip friction along the bed tangent.
                if mu_max > 0.0:
                    newly = in_contact & ~has_anchor
                    fric_anchor[newly] = x[newly]
                    has_anchor[newly] = True
                    has_anchor[~in_contact] = False

                    ft_want = -k_fric * (x - fric_anchor)  # restoring toward anchor
                    ft_max = mu_node * Fn
                    slip_hi = ft_want > ft_max
                    slip_lo = ft_want < -ft_max
                    # Slide the anchor so the spring sits on the friction cone.
                    fric_anchor[slip_hi] = x[slip_hi] + ft_max[slip_hi] / k_fric
                    fric_anchor[slip_lo] = x[slip_lo] - ft_max[slip_lo] / k_fric
                    ft = np.clip(ft_want, -ft_max, ft_max)
                    ft = np.where(in_contact, ft, 0.0)
                    F[:, 0] += ft * cosa
                    F[:, 1] += ft * cosa * bed_slope
                else:
                    has_anchor[:] = False

            F[fixed] = 0.0

            # Kinetic damping step.
            v += (F / m_node) * dt
            ke = float(np.sum(v * v))
            if ke < ke_prev:
                v[:] = 0.0
                ke = 0.0
            ke_prev = ke
            x += v[:, 0] * dt
            y += v[:, 1] * dt

            if (it + 1) % check_every == 0:
                residual_ratio = float(np.max(np.abs(F[~fixed]))) / max(w_ref, 1e-9)
                if residual_ratio < tol:
                    converged = True
                    break

        # Rest-length correction: stretched length back to physical length.
        dx = np.diff(x)
        dy = np.diff(y)
        seg_len = np.maximum(np.hypot(dx, dy), 1e-12)
        strain_now = np.maximum(0.0, (seg_len - L0_rest) / L0_rest)
        max_corr = float(np.max(strain_now))
        L0_rest = L0 / (1.0 + strain_now)
        if max_corr < 2e-5:  # < 0.002 % residual stretch — inextensible enough
            break

    # --- Post-processing -----------------------------------------------------
    dx = np.diff(x)
    dy = np.diff(y)
    seg_len = np.maximum(np.hypot(dx, dy), 1e-12)
    T_seg = np.maximum(0.0, EA * (seg_len - L0_rest) / L0_rest)
    T_node = np.empty(n_pts)
    T_node[0] = T_seg[0]
    T_node[-1] = T_seg[-1]
    T_node[1:-1] = 0.5 * (T_seg[:-1] + T_seg[1:])

    bed_eval()
    clearance = y - bed_y
    contact_tol = max(0.02, 2.0 * (w_ref + 0.05 * tension_scale_N) / k_contact)
    contact = clearance < contact_tol
    max_pen = float(max(0.0, -np.min(clearance)))

    # Free spans: contiguous non-contact runs (excluding the top hang).
    spans: List[FreeSpan] = []
    i = 0
    while i < n_pts:
        if not contact[i]:
            j = i
            while j + 1 < n_pts and not contact[j + 1]:
                j += 1
            # The run from the very top node down to first touchdown is the
            # main suspended section; runs *between* contacts are free spans
            # but we report all of them, flagging the first as the hang.
            seg_slice = slice(max(1, i), min(n_pts - 1, j))
            radii = [
                _three_point_radius(x, y, k)
                for k in range(seg_slice.start, max(seg_slice.start + 1, seg_slice.stop))
            ]
            min_r = float(min(radii)) if radii else float("inf")
            spans.append(
                FreeSpan(
                    s_start_m=float(s_nodes[i]),
                    s_end_m=float(s_nodes[j]),
                    length_m=float(s_nodes[j] - s_nodes[i]),
                    x_start_m=float(x[i]),
                    x_end_m=float(x[j]),
                    max_clearance_m=float(np.max(clearance[i : j + 1])),
                    min_radius_m=min_r,
                    max_tension_kN=float(np.max(T_node[i : j + 1])) / 1000.0,
                )
            )
            i = j + 1
        else:
            i += 1

    if not converged:
        warnings.append(
            f"Drape relaxation did not reach tolerance (residual ratio "
            f"{residual_ratio:.2e} > {tol:.0e} after {iters_done} iterations); "
            "treat the result as approximate."
        )
    if max_pen > 5.0 * contact_tol:
        warnings.append(
            f"Maximum bed penetration {max_pen:.3f} m exceeds the contact resolution; "
            "consider more nodes."
        )
    if mu_max > 0.0:
        warnings.append(
            "Static equilibria with friction are non-unique (lay-history dependent); "
            "this is one admissible state reached from the initial geometry."
        )
    if EI_max > 0.0:
        lam = math.sqrt(EI_max / max(tension_scale_N, 1.0))
        if lam < L0:
            warnings.append(
                f"Bending boundary layer (~{lam:.2f} m at the tension scale) is below the "
                f"node spacing ({L0:.2f} m); EI has negligible effect on the shape at this "
                "discretisation (the flexible-catenary limit applies)."
            )

    # Global minimum bend radius over inner nodes (contact regions included:
    # a cable bent over a crest is in contact and its radius still matters
    # for MBR). Resolution is limited by the node spacing.
    min_radius = float("inf")
    min_radius_s = 0.0
    for i in range(1, n_pts - 1):
        r_i = _three_point_radius(x, y, i)
        if r_i < min_radius:
            min_radius = r_i
            min_radius_s = float(s_nodes[i])

    # Angle below horizontal of the cable leaving the top end, positive down.
    theta_top = math.degrees(math.atan2(-(y[1] - y[0]), (x[1] - x[0])))

    return DrapeResult(
        x=x,
        y=y,
        s=s_nodes,
        tension_kN=T_node / 1000.0,
        contact=contact,
        clearance_m=clearance,
        spans=spans,
        top_tension_kN=float(T_node[0]) / 1000.0,
        top_angle_deg=theta_top,
        end_tension_kN=float(T_node[-1]) / 1000.0,
        converged=converged,
        iterations=iters_done,
        residual_ratio=residual_ratio,
        max_penetration_m=max_pen,
        min_radius_m=min_radius,
        min_radius_s_m=min_radius_s,
        warnings=warnings,
    )
