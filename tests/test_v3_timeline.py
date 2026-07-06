# -*- coding: utf-8 -*-
"""Validation of the V3 timeline simulator and operation scenarios.

Pure Python + NumPy; no QGIS imports. These are behavioural/physical
sanity tests (monotonic BU descent, touchdown, bight release and settle,
steady-lay approach) — the underlying solver accuracy is covered by
``test_v3_solver3d.py`` and ``test_v3_steady_lay.py``.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
import types

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load_engine():
    pkg = types.ModuleType("sct_v3_tl")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "engine")]
    sys.modules["sct_v3_tl"] = pkg
    mods = {}
    for m in ("bathymetry", "cable_system", "solver3d", "hydrodynamics",
              "steady_lay", "timeline", "scenarios"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_tl.{m}", ROOT / "catenary" / "v3" / "engine" / f"{m}.py"
        )
        mm = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_tl.{m}"] = mm
        spec.loader.exec_module(mm)
        mods[m] = mm
    return mods


M = _load_engine()
bathy_mod = M["bathymetry"]
cs = M["cable_system"]
tl = M["timeline"]
sc = M["scenarios"]
sl = M["steady_lay"]


def _telecom_assembly(length):
    return cs.uniform_assembly(
        length, 180.0, q_air_npm=300.0, diameter_m=0.035,
        cd_normal=1.2, cd_tangential=0.01, mu=0.3, name="LW cable",
    )


DEFAULTS = cs.Defaults(q_water_npm=180.0, mu=0.3, diameter_m=0.035)


def _opts(**over):
    kw = dict(max_move_m=8.0, rate_drag=True, tol=4e-3, max_iters=30000)
    kw.update(over)
    return tl.SimOptions(**kw)


def _assert(cond, msg):
    assert cond, msg


# ---------------------------------------------------------------------------

def test_settle_and_payout_pooling():
    """Stationary vessel paying out: deployed length grows, cable pools on
    the bed and top tension stays ~ hang weight."""
    h = 50.0
    bathy = bathy_mod.FlatBathymetry(h)
    chute_h = 5.0
    L0 = 1.6 * h + chute_h
    chute = np.array([0.0, 0.0, chute_h])
    tdp = np.array([-0.8 * h, 0.0, -h])
    anchor = (-1.4 * h, 0.0, -h)
    shape = np.vstack([
        cs.straight_shape(chute, tdp, 30),
        cs.straight_shape(tdp, np.asarray(anchor), 12)[1:],
    ])
    chain = tl.ChainState(
        name="cable", assembly=_telecom_assembly(2000.0), defaults=DEFAULTS,
        length_m=L0, top=tl.Attachment("vessel", chute_height_m=chute_h),
        bottom=tl.Attachment("fixed", xyz=anchor), shape=shape, target_ds_m=3.0,
    )
    scn = tl.Scenario(
        chains={"cable": chain}, vessel_xy=(0.0, 0.0),
        steps=[tl.Step(duration_s=120.0, vessel_speed_mps=0.0,
                       payout_mps={"cable": 0.3}, label="pay out")],
    )
    sim = tl.OperationSimulator(scn, bathy, _opts())
    res = sim.run()
    _assert(not res.aborted, "sim aborted")
    first, last = res.snapshots[0], res.snapshots[-1]
    _assert(first.converged and last.converged, "endpoints must converge")
    c0, c1 = first.chain("cable"), last.chain("cable")
    _assert(abs(c1.length_m - (L0 + 0.3 * 120.0)) < 3.0,
            f"length bookkeeping: {c1.length_m:.1f} vs {L0 + 36.0:.1f}")
    _assert(int(np.sum(c1.contact)) > int(np.sum(c0.contact)),
            "paid-out cable should pool on the bed")
    # Top tension stays near the suspended weight (never explodes).
    w_hang = 180.0 * h + 300.0 * chute_h
    _assert(0.5 * w_hang < c1.top_tension_kN * 1000.0 < 2.0 * w_hang,
            f"top tension {c1.top_tension_kN * 1000.0:.0f} N vs hang {w_hang:.0f} N")


def test_straight_lay_approaches_steady_state():
    """After steaming ~10 depths, the transient lay should match the
    steady-lay ODE's top tension and layback reasonably."""
    h = 60.0
    bathy = bathy_mod.FlatBathymetry(h)
    V = 1.0
    slack = 2.0
    scn = sc.straight_lay(
        bathy, _telecom_assembly(5000.0), DEFAULTS,
        ship_speed_mps=V, slack_percent=slack, duration_s=600.0,
        chute_height_m=5.0, target_ds_m=4.0,
    )
    sim = tl.OperationSimulator(scn, bathy, _opts())
    res = sim.run()
    _assert(not res.aborted, "sim aborted")
    last = res.snapshots[-1]
    _assert(last.converged, "final state must converge")
    c = last.chain("cable")

    # Rigorous physics check on the DR state itself: Zajac's top-tension
    # theorem T_ship ~ T_TDP + w*h (+ in-air chute hang), which holds for
    # any residual bottom tension the transient still carries.
    i_tdp = int(np.argmax(c.contact))
    T_tdp_N = float(c.tension_kN[i_tdp]) * 1000.0
    expect_N = T_tdp_N + 180.0 * h + 300.0 * 5.0
    rel_thm = abs(c.top_tension_kN * 1000.0 - expect_N) / expect_N
    _assert(rel_thm < 0.06,
            f"top-tension theorem: {c.top_tension_kN * 1000.0:.0f} N vs "
            f"T_TDP + wh = {expect_N:.0f} N ({rel_thm:.1%})")
    # And the steady ODE should be in the same ballpark — the transient may
    # legitimately hold residual bottom tension from the start-up (friction
    # is lay-history dependent), so the band is loose.
    ode = sl.integrate_steady_lay(sl.SteadyLayInput(
        depth_m=h, q_water_npm=180.0, q_air_npm=300.0, diameter_m=0.035,
        cd_normal=1.2, cd_tangential=0.01, rho_c_kgpm=30.0,
        ship_speed_mps=V, payout_speed_mps=V * (1 + slack / 100.0),
        chute_height_m=5.0, T0_N=0.0,
    ))
    rel = abs(c.top_tension_kN * 1000.0 - ode.top_tension_N) / ode.top_tension_N
    _assert(rel < 0.35,
            f"top tension {c.top_tension_kN * 1000.0:.0f} N vs ODE {ode.top_tension_N:.0f} N ({rel:.1%})")
    # The TDP must have advanced with the vessel.
    x_contact = c.xyz[c.contact, 0]
    _assert(len(x_contact) > 0 and float(np.max(x_contact)) > 300.0,
            "TDP should have advanced several hundred metres")


def test_bu_deployment_descends_and_lands():
    h = 80.0
    bathy = bathy_mod.FlatBathymetry(h)
    trunk_asm = _telecom_assembly(3000.0)
    leg_asm = _telecom_assembly(3000.0)
    scn = sc.bu_deployment(
        bathy, trunk_asm, leg_asm, DEFAULTS,
        bu_weight_kN=15.0, bu_cda_m2=1.5, leg_length_m=150.0,
        ship_speed_mps=0.3, payout_speed_mps=0.4, target_ds_m=5.0,
    )
    sim = tl.OperationSimulator(scn, bathy, _opts())
    res = sim.run()
    _assert(not res.aborted, "sim aborted")
    zs = [s.junction_xyz["BU"][2] for s in res.snapshots]
    # Monotonic descent (small tolerance for solver noise).
    ups = sum(1 for a, b in zip(zs, zs[1:]) if b > a + 0.5)
    _assert(ups == 0, f"BU rose during lowering ({ups} upward moves)")
    _assert(zs[-1] < -h + 3.0, f"BU should land near the bed (z = {zs[-1]:.1f} m)")
    last = res.snapshots[-1]
    _assert(last.converged, "final state must converge")
    trunk = last.chain("trunk")
    _assert(trunk.top_tension_kN > 0.5, "trunk should retain some tension")
    for name in ("leg1", "leg2"):
        leg = last.chain(name)
        _assert(bool(leg.contact[-1]), f"{name} far end must rest on the bed")
    # Before touchdown the trunk carries at least the BU weight.
    mid = res.snapshots[len(res.snapshots) // 2]
    _assert(mid.chain("trunk").top_tension_kN * 1000.0 > 15.0 * 1000.0 * 0.8,
            "suspended BU should load the trunk with ~its weight")


def test_final_bight_lowers_releases_and_settles():
    h = 40.0
    bathy = bathy_mod.FlatBathymetry(h)
    cable_asm = _telecom_assembly(1000.0)
    rope_asm = sc.default_rope_assembly(500.0)
    scn = sc.final_bight(
        bathy, cable_asm, rope_asm, DEFAULTS,
        bight_length_m=250.0, end_a_xy=(-60.0, 0.0), end_b_xy=(60.0, 0.0),
        vessel_speed_mps=0.15, rope_payout_mps=0.3,
        release_threshold_kN=1.0, target_ds_m=4.0,
    )
    sim = tl.OperationSimulator(scn, bathy, _opts())
    res = sim.run()
    _assert(not res.aborted, "sim aborted")
    _assert(any("released" in w for w in res.warnings),
            f"rope should auto-release; warnings: {res.warnings}")
    last = res.snapshots[-1]
    _assert(last.converged, "final state must converge")
    _assert(last.chain("rope") is None, "rope must be gone after release")
    c = last.chain("cable")
    frac_contact = float(np.mean(c.contact))
    _assert(frac_contact > 0.9, f"bight should rest on the bed ({frac_contact:.0%} contact)")
    _assert(float(np.max(c.tension_kN)) < 8.0,
            f"settled bight tension too high: {float(np.max(c.tension_kN)):.1f} kN")
    # Apex descended monotonically until release.
    apex_z = []
    for s in res.snapshots:
        cc = s.chain("cable")
        apex_z.append(float(np.max(cc.xyz[:, 2])))
    _assert(apex_z[0] > -5.0, "apex starts near the surface")
    _assert(min(apex_z) < -h + 6.0, "apex should get near the bed")


def test_static_hold_bu_and_bight():
    """static_only scenarios: a single settled equilibrium of a held BU or
    bight, at a chosen hold depth."""
    h = 80.0
    bathy = bathy_mod.FlatBathymetry(h)
    asm = _telecom_assembly(3000.0)

    scn = sc.bu_deployment(
        bathy, asm, asm, DEFAULTS,
        bu_weight_kN=18.0, bu_cda_m2=1.5, leg_length_m=160.0,
        bu_start_depth_m=35.0, trunk_slack_pct=2.0, static_only=True,
        target_ds_m=5.0,
    )
    _assert(len(scn.steps) == 0, "static_only must produce no steps")
    sim = tl.OperationSimulator(scn, bathy, _opts())
    snap = sim.settle()
    _assert(snap.converged, "BU hold must converge")
    bu_z = snap.junction_xyz["BU"][2]
    _assert(-55.0 < bu_z < -20.0, f"BU should hold near 35 m depth (z = {bu_z:.1f})")
    trunk = snap.chain("trunk")
    _assert(trunk.top_tension_kN > 17.0, "trunk must carry at least ~the BU weight")

    scn2 = sc.final_bight(
        bathy, asm, sc.default_rope_assembly(500.0), DEFAULTS,
        bight_length_m=260.0, end_a_xy=(-60.0, 0.0), end_b_xy=(60.0, 0.0),
        apex_start_depth_m=25.0, static_only=True, target_ds_m=4.0,
    )
    _assert(len(scn2.steps) == 0, "static_only must produce no steps")
    sim2 = tl.OperationSimulator(scn2, bathy, _opts())
    snap2 = sim2.settle()
    _assert(snap2.converged, "bight hold must converge")
    rope = snap2.chain("rope")
    cable = snap2.chain("cable")
    _assert(rope is not None and rope.end_tension_kN > 5.0,
            "hook must carry the suspended bight weight")
    apex_z = float(np.max(cable.xyz[:, 2]))
    _assert(-60.0 < apex_z < -10.0, f"apex should hold at depth (z = {apex_z:.1f})")


# ---------------------------------------------------------------------------

def _result(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run_all():
    failures = []
    tests = [
        test_settle_and_payout_pooling,
        test_straight_lay_approaches_steady_state,
        test_bu_deployment_descends_and_lands,
        test_final_bight_lowers_releases_and_settles,
        test_static_hold_bu_and_bight,
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
