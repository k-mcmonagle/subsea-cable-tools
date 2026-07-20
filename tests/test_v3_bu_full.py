# -*- coding: utf-8 -*-
"""Validation of the full two-sheave BU deployment simulation.

Pure Python + NumPy; no QGIS imports. Covers the B1-B3 engine features:
vessel/sheave geometry, event-driven topology changes (BU overboarding),
the sheave transfer lerp, payout bookkeeping and the leg balance
controller / initial trim.
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
    pkg = types.ModuleType("sct_v3_bu")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "engine")]
    sys.modules["sct_v3_bu"] = pkg
    mods = {}
    for m in ("bathymetry", "cable_system", "solver3d", "hydrodynamics",
              "steady_lay", "seeds", "timeline", "control", "scenarios",
              "schedule_opt"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_bu.{m}", ROOT / "catenary" / "v3" / "engine" / f"{m}.py"
        )
        mm = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_bu.{m}"] = mm
        spec.loader.exec_module(mm)
        mods[m] = mm
    return mods


M = _load_engine()
bathy_mod = M["bathymetry"]
cs = M["cable_system"]
tl = M["timeline"]
sc = M["scenarios"]
ctl = M["control"]
sopt = M["schedule_opt"]


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


def _bu_scenario(bathy, *, schedule=None, heading=0.0, **over):
    asm = _telecom_assembly(3000.0)
    kw = dict(
        bu_weight_kN=15.0, bu_cda_m2=1.5,
        laid_end_1_xy=(-60.0, 140.0), laid_end_2_xy=(-60.0, -140.0),
        vessel_xy=(0.0, 0.0), vessel_heading_deg=heading,
        schedule=schedule, tail_length_m=60.0,
        payout_mps=0.5, lay_speed_mps=0.35, target_ds_m=6.0,
    )
    kw.update(over)
    return sc.bu_full_deployment(bathy, asm, asm, asm, DEFAULTS, **kw)


# ---------------------------------------------------------------------------

def test_sheave_geometry_rotates_with_heading():
    geom = sc.default_bu_vessel_geometry(sheave_fwd_m=20.0, sheave_height_m=6.0,
                                         sheave_spacing_m=10.0)
    # Heading +x: port sheave is to +y (port = left of heading in math frame).
    px, py, pz = geom.sheave_xyz((100.0, 50.0), 0.0, "port")
    sx, sy, _ = geom.sheave_xyz((100.0, 50.0), 0.0, "stbd")
    _assert(abs(px - 120.0) < 1e-9 and abs(py - 55.0) < 1e-9 and pz == 6.0,
            f"port sheave misplaced: {(px, py, pz)}")
    _assert(abs(sx - 120.0) < 1e-9 and abs(sy - 45.0) < 1e-9,
            f"stbd sheave misplaced: {(sx, sy)}")
    # Rotate heading to +y (90 deg CCW): fwd -> +y, stbd -> +x.
    px, py, _ = geom.sheave_xyz((0.0, 0.0), 90.0, "port")
    _assert(abs(px - (-5.0)) < 1e-9 and abs(py - 20.0) < 1e-9,
            f"rotated port sheave misplaced: {(px, py)}")
    try:
        geom.sheave_xyz((0.0, 0.0), 0.0, "nope")
        _assert(False, "unknown sheave must raise")
    except KeyError:
        pass


def test_two_sheave_hold_settles_balanced_symmetric():
    """Symmetric legs on port/stbd sheaves settle to near-equal tensions."""
    h = 40.0
    bathy = bathy_mod.FlatBathymetry(h)
    scn = _bu_scenario(bathy)
    sim = tl.OperationSimulator(scn, bathy, _opts())
    snap = sim.settle()
    _assert(snap.converged, "two-sheave hold must converge")
    t1 = snap.chain("leg1").top_tension_kN
    t2 = snap.chain("leg2").top_tension_kN
    _assert(t1 > 0.5 and t2 > 0.5, "legs must carry hang tension")
    _assert(abs(t1 - t2) < 0.15 * max(t1, t2),
            f"symmetric legs should balance ({t1:.2f} vs {t2:.2f} kN)")
    for name in ("leg1", "leg2"):
        c = snap.chain(name)
        _assert(bool(c.contact[-1]), f"{name} far end must rest on the bed")


def test_full_deployment_lands_bu_and_conserves_length():
    h = 40.0
    bathy = bathy_mod.FlatBathymetry(h)
    scn = _bu_scenario(bathy)
    opts = _opts()
    opts.controller = ctl.TensionBalanceController("leg1", "leg2")
    sim = tl.OperationSimulator(scn, bathy, opts)

    init_len = {n: st.length_m for n, st in scn.chains.items()}
    res = sim.run()
    _assert(not res.aborted, "sim aborted")
    last = res.snapshots[-1]
    _assert(last.converged, "final state must converge")

    # BU exists, descended monotonically after overboarding, and landed.
    zs = [s.junction_xyz["BU"][2] for s in res.snapshots if "BU" in s.junction_xyz]
    _assert(len(zs) > 3, "BU must appear after the overboard event")
    ups = sum(1 for a, b in zip(zs, zs[1:]) if b > a + 0.5)
    _assert(ups == 0, f"BU rose during lowering ({ups} upward moves)")
    _assert(zs[-1] < -h + 3.0, f"BU should land near the bed (z = {zs[-1]:.1f} m)")

    # Trunk spawned by the event and carries tension while the BU is
    # suspended mid-water (before landing unloads it onto the bed).
    bu_snaps = [s for s in res.snapshots if "BU" in s.junction_xyz]
    mid = min(bu_snaps, key=lambda s: abs(s.junction_xyz["BU"][2] + 0.5 * h))
    _assert(abs(mid.junction_xyz["BU"][2] + 0.5 * h) < 0.4 * h,
            "no snapshot caught the BU mid-water")
    _assert(mid.chain("trunk") is not None, "trunk must exist after overboard")
    _assert(mid.chain("trunk").top_tension_kN * 1000.0 > 15.0 * 1000.0 * 0.7,
            "suspended BU should load the trunk with ~its weight")

    # Length conservation: each chain's final length equals its initial
    # length plus the integral of the *applied* payout rates.
    integ = {n: 0.0 for n in ("leg1", "leg2", "trunk")}
    prev_t = res.snapshots[0].t_s
    for s in res.snapshots[1:]:
        dt = s.t_s - prev_t
        prev_t = s.t_s
        for n, r in s.payout_mps.items():
            integ[n] = integ.get(n, 0.0) + r * dt
    for n in ("leg1", "leg2"):
        expect = init_len[n] + integ[n]
        got = last.chain(n).length_m
        _assert(abs(got - expect) < 1.5,
                f"{n} length bookkeeping: {got:.1f} vs {expect:.1f} m")
    # Controller preserves the total leg payout of the schedule.
    base_total = sum(
        st.duration_s * sum(v for k, v in st.payout_mps.items() if k.startswith("leg"))
        for st in scn.steps
    )
    _assert(abs((integ["leg1"] + integ["leg2"]) - base_total) < 1.5,
            f"controller must conserve total leg payout "
            f"({integ['leg1'] + integ['leg2']:.1f} vs {base_total:.1f} m)")

    # The transfer walked leg2's top to the port sheave.
    _assert(scn.chains["leg2"].top.kind == "junction",
            "leg2 ends topped to the BU junction")
    # Every snapshot around the transfer stayed convergent (no shock).
    tr_snaps = [s for s in res.snapshots if "Transfer" in (s.label or "")]
    _assert(tr_snaps and all(s.converged for s in tr_snaps),
            "transfer substeps must all converge")


def test_controller_balances_on_asymmetric_slope():
    """On a cross-slope bed the uncontrolled legs drift apart in tension;
    the controller keeps them matched during the joint payout phase."""
    bathy = bathy_mod.PlanarSlopeBathymetry(40.0, gx=0.0, gy=0.06)
    schedule = [
        sc.PhaseRow("Pay joints overboard", 240.0, 0.0, 0.0,
                    {"leg1": 0.5, "leg2": 0.5}),
    ]

    def run(controller):
        scn = _bu_scenario(bathy, schedule=schedule)
        opts = _opts()
        opts.controller = controller
        sim = tl.OperationSimulator(scn, bathy, opts)
        res = sim.run()
        _assert(not res.aborted, "sim aborted")
        last = res.snapshots[-1]
        return abs(last.chain("leg1").top_tension_kN - last.chain("leg2").top_tension_kN)

    d_off = run(None)
    d_on = run(ctl.TensionBalanceController("leg1", "leg2"))
    _assert(d_on <= d_off + 1e-9,
            f"controller must not worsen imbalance ({d_on:.2f} vs {d_off:.2f} kN)")
    _assert(d_on < 0.6, f"controlled imbalance too large: {d_on:.2f} kN")


def test_balance_leg_lengths_trims_to_tolerance():
    """The secant trim equalises sheave tensions from a deliberately
    unbalanced start."""
    h = 40.0
    bathy = bathy_mod.FlatBathymetry(h)
    scn = _bu_scenario(bathy)
    # Unbalance: haul leg1 in by 12 m.
    scn.chains["leg1"].length_m -= 12.0
    sim = tl.OperationSimulator(scn, bathy, _opts())
    snap = ctl.balance_leg_lengths(sim, "leg1", "leg2", tol_kN=0.4)
    d = abs(snap.chain("leg1").top_tension_kN - snap.chain("leg2").top_tension_kN)
    _assert(snap.converged, "trim result must converge")
    _assert(d < 0.4, f"trim must balance tensions (|dT| = {d:.2f} kN)")


def test_optimizer_lands_on_target():
    """The preview-translate optimiser places the operation so the BU lands
    within tolerance of the target, on a sloping bed."""
    bathy = bathy_mod.PlanarSlopeBathymetry(45.0, gx=0.02, gy=0.0)
    asm = _telecom_assembly(3000.0)
    params = dict(
        leg1_assembly=asm, leg2_assembly=asm, trunk_assembly=asm,
        defaults=DEFAULTS,
        bu_weight_kN=15.0, bu_cda_m2=1.5,
        laid_end_1_xy=(-60.0, 140.0), laid_end_2_xy=(-60.0, -140.0),
        vessel_xy=(0.0, 0.0), vessel_heading_deg=0.0,
        tail_length_m=60.0, payout_mps=0.5, lay_speed_mps=0.35,
        target_ds_m=6.0,
    )
    target = (150.0, 30.0)
    out = sopt.optimize_bu_schedule(bathy, params, target, tol_m=12.0, max_rounds=3)
    _assert(out.preview is not None and not out.preview.aborted, "preview must run")
    _assert(math.isfinite(out.landing_error_m), "landing must be measured")
    _assert(out.landing_error_m <= 12.0,
            f"optimised landing error {out.landing_error_m:.1f} m > 12 m "
            f"(rounds={out.rounds}, warnings={out.warnings})")
    _assert(len(out.schedule) >= 4, "optimiser must return the phase schedule")
    # Limits machinery fires on an absurd tension limit.
    out2 = sopt.optimize_bu_schedule(
        bathy, params, target, tol_m=1e9,
        limits=sopt.DeploymentLimits(max_tension_kN=0.1), max_rounds=1)
    _assert(any("exceeds" in w for w in out2.warnings),
            f"limit warning must fire: {out2.warnings}")


# ---------------------------------------------------------------------------

def _result(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run_all():
    failures = []
    tests = [
        test_sheave_geometry_rotates_with_heading,
        test_two_sheave_hold_settles_balanced_symmetric,
        test_full_deployment_lands_bu_and_conserves_length,
        test_controller_balances_on_asymmetric_slope,
        test_balance_leg_lengths_trims_to_tolerance,
        test_optimizer_lands_on_target,
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
