# -*- coding: utf-8 -*-
"""3D static / quasi-static cable equilibrium by dynamic relaxation.

Pure Python + NumPy; no Qt/QGIS imports.

This is the 3D generalisation of ``catenary/drape_solver.py`` (same method:
lumped nodes, stiff tension-only axial springs driven to the inextensible
limit by outer rest-length correction, penalty seabed contact, Coulomb
stick-slip friction, optional bending stiffness as discrete three-node
moments, dynamic relaxation with kinetic damping) extended with:

* three force components and arbitrary bathymetry surfaces ``z_bed(x, y)``;
* **multi-chain topologies** — several cable runs sharing junction nodes
  (e.g. a branching unit's trunk and two legs);
* **hydrodynamic drag** (Morison-type, quadratic) from a depth-dependent
  current, an apparent uniform flow (ship frame), material transport along
  the cable (pay-out) and/or prescribed node velocities (quasi-static
  stepping) — drag depends on geometry and *physical* velocities only, so
  the DR pseudo-dynamics stay clean;
* lumped body drag at nodes (``0.5 * rho * CdA * |u| u``).

Assumptions carried over from the 2D solver: static equilibrium (inertia of
the real cable is not modelled; rate effects enter only through drag),
inextensible limit, unilateral frictional contact, equilibria with friction
are lay-history dependent.

Frame: x, y horizontal metres; z vertical, 0 at the sea surface, negative
down. Weight acts in -z below the surface using the submerged weight and
above using the in-air weight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

from .cable_system import CableSystem, Chain


@dataclass
class FreeSpan3D:
    """A suspended run between contact regions (or chain ends)."""

    chain: str
    s_start_m: float
    s_end_m: float
    length_m: float
    max_clearance_m: float
    min_radius_m: float
    max_tension_kN: float


@dataclass
class ChainResult:
    name: str
    idx: "np.ndarray"
    xyz: "np.ndarray"                # (n+1, 3)
    s: "np.ndarray"                  # arc position (rest length) from node 0
    tension_kN: "np.ndarray"         # per node
    contact: "np.ndarray"            # bool per node
    clearance_m: "np.ndarray"        # vertical gap to bed
    seg_id: "np.ndarray"             # per element assembly segment id
    top_tension_kN: float = 0.0
    end_tension_kN: float = 0.0
    top_angle_deg: float = 0.0       # below horizontal at node 0
    min_radius_m: float = float("inf")
    min_radius_s_m: float = 0.0
    spans: List[FreeSpan3D] = field(default_factory=list)

    def tension_at_s(self, s_m: float) -> float:
        return float(np.interp(float(s_m), self.s, self.tension_kN))


@dataclass
class SolveResult:
    X: "np.ndarray"                  # (n_nodes, 3) equilibrium positions
    chains: List[ChainResult]
    converged: bool = False
    iterations: int = 0
    residual_ratio: float = float("inf")
    max_penetration_m: float = 0.0
    warnings: List[str] = field(default_factory=list)

    def chain(self, name: str) -> Optional[ChainResult]:
        for c in self.chains:
            if c.name == name:
                return c
        return None


def _three_point_radius_3d(P: "np.ndarray", i: int) -> float:
    if i <= 0 or i >= len(P) - 1:
        return float("inf")
    a = P[i - 1] - P[i]
    b = P[i + 1] - P[i]
    la = float(np.linalg.norm(a))
    lb = float(np.linalg.norm(b))
    lc = float(np.linalg.norm(P[i + 1] - P[i - 1]))
    cross = np.cross(a, b)
    area2 = float(np.linalg.norm(cross))
    if area2 < 1e-12:
        return float("inf")
    return la * lb * lc / (2.0 * area2)


def _radii_array(P: "np.ndarray") -> "np.ndarray":
    """Vectorised three-point bend radius at inner nodes; inf at the ends."""
    n = len(P)
    r = np.full(n, np.inf)
    if n < 3:
        return r
    a = P[:-2] - P[1:-1]
    b = P[2:] - P[1:-1]
    la = np.linalg.norm(a, axis=1)
    lb = np.linalg.norm(b, axis=1)
    lc = np.linalg.norm(P[2:] - P[:-2], axis=1)
    area2 = np.linalg.norm(np.cross(a, b), axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rr = np.where(area2 > 1e-12, la * lb * lc / (2.0 * area2), np.inf)
    r[1:-1] = rr
    return r


def solve_system(
    system: CableSystem,
    bathy=None,
    *,
    rho_water: float = 1025.0,
    current_at: Optional[Callable[["np.ndarray"], "np.ndarray"]] = None,
    apparent_flow: Sequence[float] = (0.0, 0.0, 0.0),
    node_velocity: Optional["np.ndarray"] = None,
    tol: float = 2e-3,
    max_iters: int = 120000,
    n_outer: int = 5,
    tension_scale_N: float = 0.0,
    warm_X: Optional["np.ndarray"] = None,
) -> SolveResult:
    """Relax the system to static equilibrium.

    Parameters
    ----------
    system:
        CableSystem (node pool, chains, fixed mask, point loads). ``system.X``
        provides the initial geometry unless ``warm_X`` is given. The system
        is not mutated.
    bathy:
        Bathymetry (or None for no seabed contact).
    current_at:
        Vectorised callable: z (n,) -> water velocity (n, 3), earth frame
        (or ship frame if ``apparent_flow`` is used consistently).
    apparent_flow:
        Uniform flow added to the current — pass ``-V_ship`` to solve a
        steady-lay configuration in the vessel frame.
    node_velocity:
        (n_nodes, 3) physical cable velocities for rate-dependent drag in
        quasi-static stepping. ``None`` = stationary cable.
    """
    warnings: List[str] = []
    n_pts = system.n_nodes
    X = np.array(warm_X if warm_X is not None else system.X, dtype=float)
    if X.shape != (n_pts, 3):
        raise ValueError("warm_X must be (n_nodes, 3)")
    fixed = system.fixed.copy()
    if not np.any(fixed):
        raise ValueError("At least one node must be fixed (or the system is in free fall).")

    chains = system.chains
    if not chains:
        raise ValueError("System has no chains.")
    app_flow = np.asarray(apparent_flow, dtype=float).reshape(3)
    has_flow = (
        current_at is not None
        or float(np.linalg.norm(app_flow)) > 0.0
        or node_velocity is not None
        or any(ch.transport_speed_mps for ch in chains)
    )
    v_node = np.zeros((n_pts, 3)) if node_velocity is None else np.asarray(node_velocity, dtype=float)

    # --- Scales and stiffness auto-tuning ---------------------------------
    q_ref = 1e-6
    L0_min = float("inf")
    total_len = 0.0
    for ch in chains:
        q_ref = max(q_ref, float(np.max(np.abs(ch.qw))))
        L0_min = min(L0_min, float(np.min(ch.L0)))
        total_len += ch.length_m
    pf_max = float(np.max(np.abs(system.point_force_N))) if system.point_force_N.size else 0.0
    if tension_scale_N <= 0:
        tension_scale_N = max(q_ref * total_len, 1e3)
        # Point loads can dominate the weight scale (e.g. a heavy BU).
        tension_scale_N = max(tension_scale_N, 2.0 * pf_max)
    # Reference nodal force for convergence: the largest of self-weight,
    # an a-priori drag estimate and (scaled) point loads — so weightless
    # drag-dominated systems still get a meaningful relative residual.
    u_ref = float(np.linalg.norm(app_flow))
    if node_velocity is not None and v_node.size:
        u_ref += float(np.max(np.linalg.norm(v_node, axis=1)))
    for ch in chains:
        u_ref = max(u_ref, abs(ch.transport_speed_mps))
    if current_at is not None:
        try:
            z_probe = np.array([0.0, -10.0, -100.0, -1000.0])
            u_ref += float(np.max(np.linalg.norm(np.asarray(current_at(z_probe)), axis=1)))
        except Exception:
            pass
    drag_ref = 0.0
    if u_ref > 0.0:
        for ch in chains:
            drag_ref = max(drag_ref, 0.5 * rho_water * float(np.max(ch.cdn * ch.dia)) * u_ref * u_ref)
    w_ref = max(q_ref * L0_min, drag_ref * L0_min, 1e-3 * pf_max, 1e-9)

    EA = 500.0 * tension_scale_N
    k_axial = EA / L0_min
    k_contact = (w_ref + 0.05 * tension_scale_N) / 0.005
    k_fric = k_contact
    EI_max = max(float(np.max(ch.EI)) for ch in chains)
    k_bend = 32.0 * EI_max / (L0_min ** 3)

    dt = 1.0
    m_node = (dt * dt / 2.0) * (2.0 * k_axial + k_contact + k_fric + k_bend) * 2.0

    # --- Per-chain preallocation ------------------------------------------
    # Rest lengths per chain (mutated by the outer correction).
    L0_rest = [ch.L0.copy() for ch in chains]

    v = np.zeros((n_pts, 3))
    F = np.zeros((n_pts, 3))
    fric_anchor = X[:, :2].copy()
    has_anchor = np.zeros(n_pts, dtype=bool)
    mu_node = _mu_per_node(system)
    mu_max = float(np.max(mu_node)) if len(mu_node) else 0.0
    cda = system.body_cda_m2

    iters_done = 0
    residual_ratio = float("inf")
    check_every = 200
    # Divergence recovery: dynamic relaxation can blow up (overflow -> NaN)
    # on stiff or badly-seeded systems. Checkpoint the last finite state and,
    # on a blow-up, rewind to it with a heavier nodal mass (smaller effective
    # step). Give up after a few rewinds and return the checkpoint.
    X_ok = X.copy()
    blowups = 0
    max_blowups = 6
    diverged = False
    converged = False
    T_seg_store: List["np.ndarray"] = [np.zeros(ch.n_elems) for ch in chains]

    # Scatter fast path: interior chain nodes are contiguous in the global
    # pool (SystemBuilder allocates sequentially; only shared junction end
    # nodes break the run), so slice-adds replace np.add.at where possible.
    scatter_plans = []
    for ch in chains:
        inner = ch.idx[1:-1]
        if len(inner) and np.all(np.diff(inner) == 1):
            scatter_plans.append((int(inner[0]), int(inner[-1]) + 1, int(ch.idx[0]), int(ch.idx[-1])))
        else:
            scatter_plans.append(None)

    for outer in range(int(n_outer)):
        v[:] = 0.0
        ke_prev = 0.0
        converged = False

        for it in range(int(max_iters)):
            iters_done += 1
            F[:] = 0.0

            for ci, ch in enumerate(chains):
                idx = ch.idx
                P = X[idx]
                d = np.diff(P, axis=0)
                seg_len = np.linalg.norm(d, axis=1)
                seg_len = np.maximum(seg_len, 1e-12)
                u = d / seg_len[:, None]
                strain = (seg_len - L0_rest[ci]) / L0_rest[ci]
                T = np.maximum(0.0, EA * strain)
                T_seg_store[ci] = T

                Floc = np.zeros((len(idx), 3))
                Tu = T[:, None] * u
                Floc[:-1] += Tu
                Floc[1:] -= Tu

                # Bending: discrete three-node moments, 3D form. Joint angle
                # theta between adjacent tangents; restoring moment in the
                # plane of the two segments (axis = u1 x u2).
                if float(np.max(ch.EI)) > 0.0 and len(idx) >= 3:
                    u1 = u[:-1]
                    u2 = u[1:]
                    L1 = np.maximum(seg_len[:-1], 0.5 * ch.L0[:-1])
                    L2 = np.maximum(seg_len[1:], 0.5 * ch.L0[1:])
                    EIl = ch.EI[:-1]
                    EIr = ch.EI[1:]
                    with np.errstate(divide="ignore", invalid="ignore"):
                        EIj = np.where(
                            (EIl > 0.0) & (EIr > 0.0),
                            (L1 + L2) / (L1 / EIl + L2 / EIr),
                            0.0,
                        )
                    cr = np.cross(u1, u2)
                    sin_t = np.linalg.norm(cr, axis=1)
                    cos_t = np.einsum("ij,ij->i", u1, u2)
                    theta = np.arctan2(sin_t, cos_t)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        b_hat = np.where(sin_t[:, None] > 1e-12, cr / np.maximum(sin_t, 1e-12)[:, None], 0.0)
                    M_b = EIj * 2.0 * theta / (L1 + L2)
                    n1 = np.cross(b_hat, u1)
                    n2 = np.cross(b_hat, u2)
                    a1 = (M_b / L1)[:, None]
                    a2 = (M_b / L2)[:, None]
                    Floc[:-2] += -a1 * n1
                    Floc[2:] += -a2 * n2
                    Floc[1:-1] += a1 * n1 + a2 * n2

                # Weight per element by mid-depth medium; physical length L0.
                z_mid = 0.5 * (P[:-1, 2] + P[1:, 2])
                qa_eff = np.where(ch.qa != 0.0, ch.qa, ch.qw)
                q_elem = np.where(z_mid < 0.0, ch.qw, qa_eff)
                w_elem = q_elem * ch.L0
                Floc[:-1, 2] -= 0.5 * w_elem
                Floc[1:, 2] -= 0.5 * w_elem

                # Hydrodynamic drag per element (submerged elements only).
                if has_flow:
                    u_w = np.zeros((len(seg_len), 3))
                    if current_at is not None:
                        u_w += current_at(z_mid)
                    u_w += app_flow
                    v_cab = 0.5 * (v_node[idx[:-1]] + v_node[idx[1:]])
                    if ch.transport_speed_mps:
                        # Material transport toward node 0 (bottom -> pay-out
                        # convention is set by callers via the sign).
                        v_cab = v_cab + ch.transport_speed_mps * u
                    u_rel = u_w - v_cab
                    ut_mag = np.einsum("ij,ij->i", u_rel, u)
                    u_t = ut_mag[:, None] * u
                    u_n = u_rel - u_t
                    un_mag = np.linalg.norm(u_n, axis=1)
                    subm = z_mid < 0.0
                    dia = ch.dia
                    f_n = (0.5 * rho_water * ch.cdn * dia * un_mag)[:, None] * u_n
                    f_t = (0.5 * rho_water * ch.cdt * math.pi * dia * np.abs(ut_mag) * ut_mag)[:, None] * u
                    f_drag = np.where(subm[:, None], (f_n + f_t) * ch.L0[:, None], 0.0)
                    Floc[:-1] += 0.5 * f_drag
                    Floc[1:] += 0.5 * f_drag

                plan = scatter_plans[ci]
                if plan is not None:
                    lo, hi, i0, i1 = plan
                    F[lo:hi] += Floc[1:-1]
                    F[i0] += Floc[0]
                    F[i1] += Floc[-1]
                else:
                    np.add.at(F, idx, Floc)

            # Static point loads (body weights etc.).
            F += system.point_force_N

            # Lumped body drag at nodes.
            if has_flow and np.any(cda > 0.0):
                sel = cda > 0.0
                u_w = np.zeros((int(np.sum(sel)), 3))
                if current_at is not None:
                    u_w += current_at(X[sel, 2])
                u_w += app_flow
                u_rel = u_w - v_node[sel]
                mag = np.linalg.norm(u_rel, axis=1)
                subm = X[sel, 2] < 0.0
                Fd = 0.5 * rho_water * (cda[sel] * mag)[:, None] * u_rel
                F[sel] += np.where(subm[:, None], Fd, 0.0)

            # Seabed contact + friction.
            if bathy is not None:
                bed_z = -np.asarray(bathy.depth_at(X[:, 0], X[:, 1]), dtype=float)
                pen = bed_z - X[:, 2]
                in_contact = pen > 0.0
                if np.any(in_contact):
                    gx, gy = bathy.grad_at(X[:, 0], X[:, 1])
                    gx = np.asarray(gx, dtype=float)
                    gy = np.asarray(gy, dtype=float)
                    inv_norm = 1.0 / np.sqrt(1.0 + gx * gx + gy * gy)
                    # Upward bed normal (for z = -D(x, y)): (gx, gy, 1)/|.|
                    Fn_mag = k_contact * pen * inv_norm
                    Fn_mag = np.where(in_contact, Fn_mag, 0.0)
                    F[:, 0] += Fn_mag * gx * inv_norm
                    F[:, 1] += Fn_mag * gy * inv_norm
                    F[:, 2] += Fn_mag * inv_norm

                    if mu_max > 0.0:
                        newly = in_contact & ~has_anchor
                        fric_anchor[newly] = X[newly, :2]
                        has_anchor[newly] = True
                        has_anchor[~in_contact] = False

                        dxy = X[:, :2] - fric_anchor
                        ft_want = -k_fric * dxy
                        ft_mag = np.linalg.norm(ft_want, axis=1)
                        ft_max = mu_node * Fn_mag
                        over = (ft_mag > ft_max) & in_contact & (ft_mag > 1e-12)
                        if np.any(over):
                            # Slide the anchor onto the friction cone.
                            scale = ft_max[over] / ft_mag[over]
                            keep = ft_want[over] * scale[:, None]
                            fric_anchor[over] = X[over, :2] + keep / k_fric
                            ft_want[over] = keep
                        ft_want = np.where(in_contact[:, None], ft_want, 0.0)
                        # Apply along the local bed tangent (horizontal force
                        # plus consistent z so the force lies in the surface).
                        F[:, 0] += ft_want[:, 0]
                        F[:, 1] += ft_want[:, 1]
                        F[:, 2] += -(gx * ft_want[:, 0] + gy * ft_want[:, 1])
                    else:
                        has_anchor[:] = False

            F[fixed] = 0.0

            # Kinetic damping.
            v += (F / m_node) * dt
            ke = float(np.sum(v * v))
            if ke < ke_prev:
                v[:] = 0.0
                ke = 0.0
            ke_prev = ke
            X += v * dt

            if (it + 1) % check_every == 0:
                residual_ratio = float(np.max(np.abs(F[~fixed]))) / max(w_ref, 1e-9)
                if not np.isfinite(residual_ratio) or not np.all(np.isfinite(X)):
                    blowups += 1
                    if blowups > max_blowups:
                        diverged = True
                        X = X_ok.copy()
                        break
                    X = X_ok.copy()
                    v[:] = 0.0
                    ke_prev = 0.0
                    m_node *= 4.0
                    residual_ratio = float("inf")
                    continue
                X_ok = X.copy()
                if residual_ratio < tol:
                    converged = True
                    break

        if diverged:
            warnings.append(
                "Solver diverged (numerical blow-up); returning the last "
                "stable state — treat the result as approximate."
            )
            break

        # Outer rest-length correction toward the inextensible limit.
        max_corr = 0.0
        for ci, ch in enumerate(chains):
            P = X[ch.idx]
            seg_len = np.maximum(np.linalg.norm(np.diff(P, axis=0), axis=1), 1e-12)
            strain_now = np.maximum(0.0, (seg_len - L0_rest[ci]) / L0_rest[ci])
            max_corr = max(max_corr, float(np.max(strain_now)))
            L0_rest[ci] = ch.L0 / (1.0 + strain_now)
        if max_corr < 2e-5:
            break

    if not np.all(np.isfinite(X)):
        # Blow-up between residual checks right at the iteration cap.
        X = X_ok.copy()
        converged = False
        if not diverged:
            warnings.append(
                "Solver diverged (numerical blow-up); returning the last "
                "stable state — treat the result as approximate."
            )

    # --- Post-processing ----------------------------------------------------
    if bathy is not None:
        bed_z = -np.asarray(bathy.depth_at(X[:, 0], X[:, 1]), dtype=float)
        clearance_all = X[:, 2] - bed_z
    else:
        clearance_all = np.full(n_pts, np.inf)
    contact_tol = max(0.02, 2.0 * (w_ref + 0.05 * tension_scale_N) / k_contact)
    contact_all = clearance_all < contact_tol
    max_pen = float(max(0.0, -np.min(clearance_all))) if np.all(np.isfinite(clearance_all)) else 0.0

    chain_results: List[ChainResult] = []
    for ci, ch in enumerate(chains):
        idx = ch.idx
        P = X[idx]
        seg_len = np.maximum(np.linalg.norm(np.diff(P, axis=0), axis=1), 1e-12)
        T_seg = np.maximum(0.0, EA * (seg_len - L0_rest[ci]) / L0_rest[ci])
        T_node = np.empty(len(idx))
        T_node[0] = T_seg[0]
        T_node[-1] = T_seg[-1]
        T_node[1:-1] = 0.5 * (T_seg[:-1] + T_seg[1:])
        s_nodes = ch.s_nodes()
        contact = contact_all[idx]
        clearance = clearance_all[idx]
        radii = _radii_array(P)
        rmin_i = int(np.argmin(radii))
        d0 = P[1] - P[0]
        top_angle = math.degrees(math.atan2(-d0[2], math.hypot(d0[0], d0[1])))

        spans: List[FreeSpan3D] = []
        i = 0
        n_ch = len(idx)
        while i < n_ch:
            if not contact[i]:
                j = i
                while j + 1 < n_ch and not contact[j + 1]:
                    j += 1
                inner = radii[max(1, i):min(n_ch - 1, j) + 1]
                min_r = float(np.min(inner)) if len(inner) else float("inf")
                spans.append(
                    FreeSpan3D(
                        chain=ch.name,
                        s_start_m=float(s_nodes[i]),
                        s_end_m=float(s_nodes[j]),
                        length_m=float(s_nodes[j] - s_nodes[i]),
                        max_clearance_m=float(np.max(clearance[i:j + 1])) if np.all(np.isfinite(clearance[i:j + 1])) else float("inf"),
                        min_radius_m=min_r,
                        max_tension_kN=float(np.max(T_node[i:j + 1])) / 1000.0,
                    )
                )
                i = j + 1
            else:
                i += 1

        chain_results.append(
            ChainResult(
                name=ch.name,
                idx=idx.copy(),
                xyz=P.copy(),
                s=s_nodes,
                tension_kN=T_node / 1000.0,
                contact=contact.copy(),
                clearance_m=clearance.copy(),
                seg_id=ch.seg_id.copy(),
                top_tension_kN=float(T_node[0]) / 1000.0,
                end_tension_kN=float(T_node[-1]) / 1000.0,
                top_angle_deg=top_angle,
                min_radius_m=float(radii[rmin_i]),
                min_radius_s_m=float(s_nodes[rmin_i]),
                spans=spans,
            )
        )

    if not converged:
        warnings.append(
            f"Relaxation did not reach tolerance (residual ratio {residual_ratio:.2e} "
            f"> {tol:.0e} after {iters_done} iterations); treat the result as approximate."
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

    return SolveResult(
        X=X,
        chains=chain_results,
        converged=converged,
        iterations=iters_done,
        residual_ratio=residual_ratio,
        max_penetration_m=max_pen,
        warnings=warnings,
    )


def _mu_per_node(system: CableSystem) -> "np.ndarray":
    """Friction coefficient per global node (max over adjoining elements)."""
    mu = np.zeros(system.n_nodes)
    for ch in system.chains:
        idx = ch.idx
        mu_elem = np.maximum(0.0, ch.mu)
        np.maximum.at(mu, idx[:-1], mu_elem)
        np.maximum.at(mu, idx[1:], mu_elem)
    return mu
