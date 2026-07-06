# -*- coding: utf-8 -*-
"""Validation checks for the V3 3D dynamic-relaxation solver.

Pure Python + NumPy; no QGIS imports. Mirrors the style of
``test_drape_solver.py``: closed-form catenary anchors, physical
invariants, a regression against the proven 2D drape solver, and
multi-chain (junction) sanity.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load the v3 engine as a package so relative imports work.
def _load_pkg():
    import importlib

    pkg_root = ROOT
    name = "sct_v3_test_pkg"
    spec = importlib.util.spec_from_file_location(
        name, pkg_root / "catenary" / "v3" / "__init__.py",
        submodule_search_locations=[str(pkg_root / "catenary" / "v3")],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    spec.loader.exec_module(pkg)
    eng_spec = importlib.util.spec_from_file_location(
        name + ".engine", pkg_root / "catenary" / "v3" / "engine" / "__init__.py",
        submodule_search_locations=[str(pkg_root / "catenary" / "v3" / "engine")],
    )
    eng = importlib.util.module_from_spec(eng_spec)
    sys.modules[name + ".engine"] = eng
    eng_spec.loader.exec_module(eng)
    mods = {}
    for m in ("bathymetry", "cable_system", "solver3d", "hydrodynamics", "steady_lay", "timeline"):
        p = pkg_root / "catenary" / "v3" / "engine" / f"{m}.py"
        if not p.exists():
            continue
        ms = importlib.util.spec_from_file_location(name + ".engine." + m, p)
        mm = importlib.util.module_from_spec(ms)
        sys.modules[name + ".engine." + m] = mm
        ms.loader.exec_module(mm)
        mods[m] = mm
    return mods


MODS = _load_pkg()
bathy_mod = MODS["bathymetry"]
cs = MODS["cable_system"]
s3d = MODS["solver3d"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def catenary_closed_form(H_N: float, q_npm: float, h_m: float):
    """Uniform catenary from a tangential TDP on a flat bed."""
    a = H_N / q_npm
    layback = a * math.acosh(1.0 + h_m / a)
    s_len = a * math.sinh(layback / a)
    T_top = H_N + q_npm * h_m
    return layback, s_len, T_top


def hang_shape(top, tdp_guess, bed_z, extra_on_bed, n_elems, direction=(1.0, 0.0)):
    """Initial geometry: 45-degree ramp from the top to the bed, then along
    the bed in ``direction`` — same benign seed as the 2D drape solver."""
    ux, uy = direction
    nrm = math.hypot(ux, uy)
    ux, uy = ux / nrm, uy / nrm
    top = np.asarray(top, dtype=float)
    drop = top[2] - bed_z
    pts = []
    ramp = math.hypot(drop, drop)
    total = ramp + extra_on_bed
    for k in range(n_elems + 1):
        s = total * k / n_elems
        if s <= ramp:
            f = s / ramp
            pts.append([top[0] + f * drop * ux, top[1] + f * drop * uy, top[2] - f * drop])
        else:
            r = s - ramp
            pts.append([top[0] + (drop + r) * ux, top[1] + (drop + r) * uy, bed_z])
    return np.asarray(pts)


def build_single_hang(H_N=5000.0, q=200.0, h=100.0, mu=0.0, extra_bed=200.0,
                      n_elems=300, direction=(1.0, 0.0), diameter=0.0):
    """Cable from a surface point to a flat bed with an anchored far end,
    sized so the closed-form catenary with bottom tension H_N applies."""
    layback, s_len, T_top = catenary_closed_form(H_N, q, h)
    total = s_len + extra_bed
    ux, uy = np.asarray(direction, dtype=float) / np.linalg.norm(direction)
    bathy = bathy_mod.FlatBathymetry(h)
    asm = cs.uniform_assembly(total, q, mu=mu, diameter_m=diameter)
    mapper = cs.AssemblyMapper(asm, cs.Defaults(q_water_npm=q, mu=mu))
    b = cs.SystemBuilder()
    shape = hang_shape((0.0, 0.0, 0.0), layback, -h, extra_bed, n_elems, (ux, uy))
    chain = b.add_chain("main", mapper, total, n_elems, shape)
    b.set_fixed(int(chain.idx[0]))
    anchor = np.array([ (layback + extra_bed) * ux, (layback + extra_bed) * uy, -h])
    # Place the far end on the bed at the arc-consistent position and fix it.
    b.set_fixed(int(chain.idx[-1]))
    sysm = b.build()
    sysm.X[chain.idx[-1]] = anchor
    return sysm, bathy, chain, (layback, s_len, T_top)


def _assert_close(value, expect, rel, label):
    err = abs(value - expect) / max(abs(expect), 1e-12)
    assert err <= rel, f"{label}: {value:.4f} vs {expect:.4f} (rel err {err:.3%} > {rel:.1%})"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_flat_bed_matches_closed_form_catenary():
    H, q, h = 5000.0, 200.0, 100.0
    sysm, bathy, chain, (layback, s_len, T_top) = build_single_hang(H, q, h)
    res = s3d.solve_system(sysm, bathy)
    assert res.converged, f"not converged: residual {res.residual_ratio:.2e}"
    c = res.chains[0]
    _assert_close(c.top_tension_kN * 1000.0, T_top, 0.01, "top tension")
    # Anchor tension equals H on a frictionless bed.
    _assert_close(c.end_tension_kN * 1000.0, H, 0.02, "anchor tension")
    # TDP position: first contact node.
    i_tdp = int(np.argmax(c.contact))
    x_tdp = float(np.hypot(c.xyz[i_tdp, 0], c.xyz[i_tdp, 1]))
    _assert_close(x_tdp, layback, 0.025, "TDP layback")
    # No node meaningfully below the bed.
    assert res.max_penetration_m < 0.05, f"penetration {res.max_penetration_m:.3f} m"


def test_frame_invariance_under_azimuth_rotation():
    H, q, h = 4000.0, 300.0, 80.0
    sys0, bathy, _, _ = build_single_hang(H, q, h, direction=(1.0, 0.0))
    sys45, _, _, _ = build_single_hang(H, q, h, direction=(1.0, 1.0))
    r0 = s3d.solve_system(sys0, bathy)
    r45 = s3d.solve_system(sys45, bathy)
    assert r0.converged and r45.converged
    _assert_close(
        r45.chains[0].top_tension_kN, r0.chains[0].top_tension_kN, 0.005,
        "top tension rotation invariance",
    )
    # The rotated solution must stay in its vertical plane (y = x direction).
    xy = r45.chains[0].xyz[:, :2]
    off_plane = np.abs(xy[:, 0] - xy[:, 1]) / max(1.0, np.max(np.abs(xy)))
    assert float(np.max(off_plane)) < 5e-3, "cable left its lay plane"


def test_ridge_profile_matches_2d_drape_solver():
    """Multi-span drape over a ridge: 3D solver vs the proven 2D solver."""
    dr = _load("subsea_drape_solver_v3ref", "catenary/drape_solver.py")
    cs2 = _load("subsea_catenary_solver_v3ref", "catenary/catenary_solver.py")

    profile_pts = [(0.0, 120.0), (150.0, 120.0), (230.0, 60.0), (310.0, 120.0), (900.0, 120.0)]
    seabed2d = cs2.PolylineSeabed([p[0] for p in profile_pts], [p[1] for p in profile_pts])
    q = 250.0
    total_len = 700.0
    top2d = (0.0, 0.0)
    n = 350
    res2d = dr.solve_drape(
        seabed2d, top2d, total_len, q, mu=0.0,
        bottom_anchor_xy=(640.0, -120.0), n_nodes=n,
    )
    assert res2d.converged

    bathy = bathy_mod.ProfileBathymetry(profile_pts, azimuth_deg=0.0)
    asm = cs.uniform_assembly(total_len, q, mu=0.0)
    mapper = cs.AssemblyMapper(asm, cs.Defaults(q_water_npm=q, mu=0.0))
    b = cs.SystemBuilder()
    shape = np.zeros((n + 1, 3))
    shape[:, 0] = res2d.x
    shape[:, 2] = res2d.y
    chain = b.add_chain("main", mapper, total_len, n, shape)
    b.set_fixed(int(chain.idx[0]))
    b.set_fixed(int(chain.idx[-1]))
    sysm = b.build()
    res3d = s3d.solve_system(sysm, bathy)
    assert res3d.converged
    c = res3d.chains[0]
    _assert_close(c.top_tension_kN, res2d.top_tension_kN, 0.02, "ridge top tension")
    # Same contact topology: cable rests on the ridge in both models.
    contact2d = res2d.contact
    n_spans_2d = len(res2d.spans)
    n_spans_3d = len(c.spans)
    assert n_spans_3d == n_spans_2d, f"span count {n_spans_3d} vs {n_spans_2d}"
    assert res3d.max_penetration_m < 0.05


def test_junction_slack_legs_trunk_carries_body():
    """Y-topology: a body hangs from a fixed top; two slack legs rest on the
    bed. The trunk must carry body weight + trunk self-weight."""
    h = 60.0
    bathy = bathy_mod.FlatBathymetry(h)
    q_trunk, q_leg = 150.0, 100.0
    body_kN = 20.0
    trunk_len = 50.0
    leg_len = 120.0

    b = cs.SystemBuilder()
    asm_t = cs.uniform_assembly(trunk_len, q_trunk, mu=0.3)
    asm_l = cs.uniform_assembly(leg_len, q_leg, mu=0.3)
    mt = cs.AssemblyMapper(asm_t, cs.Defaults(q_water_npm=q_trunk))
    ml = cs.AssemblyMapper(asm_l, cs.Defaults(q_water_npm=q_leg))

    top = np.array([0.0, 0.0, 0.0])
    bu = np.array([0.0, 0.0, -trunk_len * 0.9])
    trunk_shape = cs.straight_shape(top, bu, 40)
    trunk = b.add_chain("trunk", mt, trunk_len, 40, trunk_shape)
    b.set_fixed(int(trunk.idx[0]))
    j = int(trunk.idx[-1])
    b.add_point_force(j, (0.0, 0.0, -body_kN * 1000.0))

    for name, ux in (("leg1", 1.0), ("leg2", -1.0)):
        end = np.array([ux * leg_len * 0.85, 0.0, -h])
        shape = cs.straight_shape(bu, end, 60)
        leg = b.add_chain(name, ml, leg_len, 60, shape, start_node=j)
        b.set_fixed(int(leg.idx[-1]))
    sysm = b.build()
    res = s3d.solve_system(sysm, bathy)
    assert res.converged, f"not converged: {res.residual_ratio:.2e}"

    trunk_res = res.chain("trunk")
    # Trunk top supports: body + trunk weight + suspended part of both legs.
    T_top_N = trunk_res.top_tension_kN * 1000.0
    min_expected = body_kN * 1000.0 + q_trunk * trunk_len
    assert T_top_N > min_expected * 0.98, f"trunk tension too low: {T_top_N:.0f} N"
    # Legs' far ends rest on the bed with modest tension.
    for name in ("leg1", "leg2"):
        leg_res = res.chain(name)
        assert bool(leg_res.contact[-1]) or leg_res.clearance_m[-1] < 0.5
    # Junction equilibrium is implied by convergence; sanity: legs symmetric.
    l1 = res.chain("leg1").top_tension_kN
    l2 = res.chain("leg2").top_tension_kN
    assert abs(l1 - l2) / max(l1, 1e-6) < 0.05, f"legs asymmetric: {l1:.2f} vs {l2:.2f} kN"


def test_buoyant_span_bows_upward():
    """A net-buoyant cable between two fixed points arches upward."""
    b = cs.SystemBuilder()
    q = -80.0  # buoyant
    length = 120.0
    asm = cs.uniform_assembly(length, q)
    mapper = cs.AssemblyMapper(asm, cs.Defaults(q_water_npm=q))
    p0 = np.array([0.0, 0.0, -100.0])
    p1 = np.array([100.0, 0.0, -100.0])
    shape = cs.straight_shape(p0, p1, 80)
    chain = b.add_chain("span", mapper, length, 80, shape)
    b.set_fixed(int(chain.idx[0]))
    b.set_fixed(int(chain.idx[-1]))
    sysm = b.build()
    res = s3d.solve_system(sysm, bathy_mod.FlatBathymetry(200.0))
    assert res.converged
    zmax = float(np.max(res.chains[0].xyz[:, 2]))
    assert zmax > -99.0, "buoyant span did not rise"
    i_mid = len(res.chains[0].xyz) // 2
    assert res.chains[0].xyz[i_mid, 2] > -100.0 + 5.0


def test_uniform_current_deflects_hang_downstream():
    """Cross-flow drag pushes a hanging cable downstream and the deflection
    grows with current speed."""
    h = 100.0
    q = 200.0
    H = 3000.0
    deflections = []
    for u in (0.0, 0.5, 1.0):
        sysm, bathy, chain, _ = build_single_hang(H, q, h, diameter=0.05)
        current = None
        if u > 0:
            def current(z, _u=u):
                out = np.zeros((len(np.atleast_1d(z)), 3))
                out[:, 1] = _u
                return out
        res = s3d.solve_system(sysm, bathy, current_at=current)
        assert res.converged
        c = res.chains[0]
        suspended = ~c.contact
        deflections.append(float(np.max(np.abs(c.xyz[suspended, 1]))))
    assert deflections[0] < 0.5, "no-current case should stay in plane"
    assert deflections[1] > 0.5, "0.5 m/s current should deflect the hang"
    assert deflections[2] > 1.8 * deflections[1], "drag should grow ~quadratically"


def test_drag_force_balance_on_straight_tow():
    """A neutrally-buoyant straight cable in uniform normal flow: the two
    end reactions must carry the total drag (global force balance)."""
    length = 50.0
    dia = 0.1
    cdn = 1.2
    rho = 1025.0
    u = 1.0
    b = cs.SystemBuilder()
    asm = cs.uniform_assembly(length, 0.0, diameter_m=dia, cd_normal=cdn)
    mapper = cs.AssemblyMapper(asm, cs.Defaults(q_water_npm=0.0))
    p0 = np.array([0.0, 0.0, -50.0])
    p1 = np.array([49.5, 0.0, -50.0])  # slightly slack
    shape = cs.straight_shape(p0, p1, 50)
    chain = b.add_chain("tow", mapper, length, 50, shape)
    b.set_fixed(int(chain.idx[0]))
    b.set_fixed(int(chain.idx[-1]))
    sysm = b.build()

    def current(z):
        out = np.zeros((len(np.atleast_1d(z)), 3))
        out[:, 1] = u
        return out

    res = s3d.solve_system(sysm, None, current_at=current)
    assert res.converged
    c = res.chains[0]
    # Expected drag per metre ~ 0.5*rho*cdn*d*u^2 on a normal cylinder.
    f_expect = 0.5 * rho * cdn * dia * u * u * length
    # End reactions' y-components: tension * direction at the ends.
    P = c.xyz
    t0 = (P[1] - P[0]) / np.linalg.norm(P[1] - P[0])
    t1 = (P[-2] - P[-1]) / np.linalg.norm(P[-2] - P[-1])
    Fy = c.top_tension_kN * 1000.0 * t0[1] + c.end_tension_kN * 1000.0 * t1[1]
    # Reactions oppose drag: cable pulls the anchors downstream.
    _assert_close(abs(Fy), f_expect, 0.08, "total drag reacted at ends")


# ---------------------------------------------------------------------------

def _result(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run_all():
    failures = []
    tests = [
        test_flat_bed_matches_closed_form_catenary,
        test_frame_invariance_under_azimuth_rotation,
        test_ridge_profile_matches_2d_drape_solver,
        test_junction_slack_legs_trunk_carries_body,
        test_buoyant_span_bows_upward,
        test_uniform_current_deflects_hang_downstream,
        test_drag_force_balance_on_straight_tow,
    ]
    for test in tests:
        try:
            test()
            _result(test.__name__, True)
        except Exception as exc:  # pragma: no cover
            _result(test.__name__, False, repr(exc))
            failures.append(test.__name__)
    print(f"\n{len(failures)} failure(s)." if failures else "\nAll checks passed.")
    return failures


if __name__ == "__main__":  # pragma: no cover
    failures = run_all()
    sys.exit(1 if failures else 0)
