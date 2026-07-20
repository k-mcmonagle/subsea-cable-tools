# -*- coding: utf-8 -*-
"""Validation of the quick analytic tri-catenary BU model.

Pure Python + NumPy; no QGIS imports. Checks the catenary primitives, the
BU force balance against the full dynamic-relaxation solver on a static
hold, and the speed/behaviour of full quick deployment runs.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
import time
import types

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load_engine():
    pkg = types.ModuleType("sct_v3_qk")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "engine")]
    sys.modules["sct_v3_qk"] = pkg
    mods = {}
    for m in ("bathymetry", "cable_system", "solver3d", "hydrodynamics",
              "steady_lay", "seeds", "timeline", "control", "scenarios",
              "quick_bu"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_qk.{m}", ROOT / "catenary" / "v3" / "engine" / f"{m}.py"
        )
        mm = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_qk.{m}"] = mm
        spec.loader.exec_module(mm)
        mods[m] = mm
    return mods


M = _load_engine()
bathy_mod = M["bathymetry"]
cs = M["cable_system"]
tl = M["timeline"]
sc = M["scenarios"]
qk = M["quick_bu"]
ctl = M["control"]


def _telecom_assembly(length):
    return cs.uniform_assembly(
        length, 180.0, q_air_npm=300.0, diameter_m=0.035,
        cd_normal=1.2, cd_tangential=0.01, mu=0.3, name="LW cable",
    )


DEFAULTS = cs.Defaults(q_water_npm=180.0, mu=0.3, diameter_m=0.035)


def _assert(cond, msg):
    assert cond, msg


# ---------------------------------------------------------------------------

def test_two_point_catenary_force_balance():
    """End tension vectors + line weight must balance; symmetric span
    splits the weight evenly."""
    w = 180.0
    p0 = np.array([0.0, 0.0, -30.0])
    p1 = np.array([60.0, 0.0, -30.0])
    L = 80.0
    cat = qk.two_point_catenary(p0, p1, L, w)
    # T vectors are the forces the line applies to its supports: a sagging
    # span pulls BOTH supports down, together carrying the full weight.
    _assert(abs(cat["T0_vec"][2] + 0.5 * w * L) / (w * L) < 0.01,
            f"symmetric span: each support carries half the weight "
            f"({cat['T0_vec'][2]:.0f} vs {-0.5 * w * L:.0f} N)")
    _assert(abs(cat["T1_vec"][2] + 0.5 * w * L) / (w * L) < 0.01,
            "other support must carry the other half")
    _assert(abs(cat["T0_vec"][2] + cat["T1_vec"][2] + w * L) < 0.01 * w * L,
            "support forces + line weight must balance")
    _assert(cat["T0_vec"][0] > 0 and cat["T1_vec"][0] < 0,
            "horizontal components must pull the supports toward each other")
    _assert(abs(cat["T0_vec"][0] + cat["T1_vec"][0]) < 1.0,
            "horizontal tension must be uniform")
    # Taut line: stiff axial pull toward the far end.
    taut = qk.two_point_catenary(np.array([0.0, 0.0, -40.0]),
                                 np.array([0.0, 0.0, 5.0]), 44.5, w)
    _assert(taut["T0_vec"][2] > w * 44.5,
            "over-stretched vertical line must pull the low end up hard")
    # Arc length of the sampled polyline ~ L.
    arc = float(np.sum(np.linalg.norm(np.diff(cat["xyz"], axis=0), axis=1)))
    _assert(abs(arc - L) < 0.02 * L, f"sampled arc {arc:.1f} vs L {L:.1f}")


def test_leg_solution_closure_and_tangency():
    h = 50.0
    bathy = bathy_mod.FlatBathymetry(h)
    w = 180.0
    p_top = np.array([0.0, 0.0, -10.0])
    anchor = (200.0, 40.0)
    L = 230.0    # long enough to touch down, short enough not to pool
    sol = qk.leg_solution(p_top, anchor, bathy, L, w)
    _assert(sol["tdp"] is not None, "long leg must touch down")
    arc = float(np.sum(np.linalg.norm(np.diff(sol["xyz"], axis=0), axis=1)))
    _assert(abs(arc - L) < 0.03 * L, f"length closure: arc {arc:.1f} vs {L:.1f}")
    # Bottom tension equals the horizontal tension (tangency).
    _assert(abs(sol["tension"][-1] - sol["H_bottom"]) < 1.0,
            "anchored end tension must equal H")
    # The force on the top has downward weight of the suspended part and a
    # horizontal pull toward the TDP.
    F = sol["F_top"]
    _assert(F[2] < 0, "leg must weigh the top point down")
    e = np.array(sol["tdp"][:2]) - p_top[:2]
    _assert(F[0] * e[0] + F[1] * e[1] > 0, "horizontal pull must aim at the TDP")
    _assert(np.any(sol["contact"]) and not sol["contact"][0],
            "bed portion in contact; top suspended")


def test_quick_matches_full_solver_static_hold():
    """Held-BU equilibrium: the analytic model must land close to the full
    dynamic-relaxation solver (position within metres, trunk tension within
    ~10 % — the quick model has no friction/drag)."""
    h = 80.0
    bathy = bathy_mod.FlatBathymetry(h)
    asm = _telecom_assembly(3000.0)

    def scenario():
        return sc.bu_deployment(
            bathy, asm, asm, DEFAULTS,
            bu_weight_kN=18.0, bu_cda_m2=0.0, leg_length_m=160.0,
            bu_start_depth_m=35.0, trunk_slack_pct=2.0, static_only=True,
            target_ds_m=5.0,
        )

    full = tl.OperationSimulator(scenario(), bathy, tl.SimOptions())
    snap_f = full.settle()
    _assert(snap_f.converged, "full solve must converge")

    quick = qk.QuickOperationSimulator(scenario(), bathy, tl.SimOptions())
    t0 = time.perf_counter()
    snap_q = quick.settle()
    dt_q = time.perf_counter() - t0
    _assert(dt_q < 1.0, f"quick settle took {dt_q:.2f} s")

    pf = np.array(snap_f.junction_xyz["BU"])
    pq = np.array(snap_q.junction_xyz["BU"])
    _assert(float(np.linalg.norm(pf - pq)) < 6.0,
            f"BU position: quick {pq.round(1)} vs full {pf.round(1)}")
    tf = snap_f.chain("trunk").top_tension_kN
    tq = snap_q.chain("trunk").top_tension_kN
    _assert(abs(tq - tf) / tf < 0.10,
            f"trunk top tension: quick {tq:.2f} vs full {tf:.2f} kN")
    for name in ("leg1", "leg2"):
        cf = snap_f.chain(name).top_tension_kN
        cq = snap_q.chain(name).top_tension_kN
        _assert(abs(cq - cf) < max(0.15 * max(cf, 0.5), 0.5),
                f"{name} tension: quick {cq:.2f} vs full {cf:.2f} kN")


def test_blank_segment_weight_uses_defaults():
    """A segment weight of 0.0 means 'blank -> defaults' (the UI leaves
    weights blank with default settings); the quick model must follow the
    AssemblyMapper convention rather than treat the line as weightless."""
    h = 60.0
    bathy = bathy_mod.FlatBathymetry(h)
    blank = cs.uniform_assembly(3000.0, 0.0, diameter_m=0.035)  # weight blank
    scn = sc.bu_deployment(
        bathy, blank, blank, cs.Defaults(q_water_npm=200.0, mu=0.3),
        bu_weight_kN=15.0, bu_cda_m2=0.0, leg_length_m=140.0,
        bu_start_depth_m=25.0, static_only=True, target_ds_m=5.0,
    )
    w = qk.mean_weight_npm(scn.chains["leg1"])
    _assert(abs(w - 200.0) < 1e-9, f"blank weight must resolve to defaults ({w})")
    sim = qk.QuickOperationSimulator(scn, bathy, tl.SimOptions())
    snap = sim.settle()   # must not divide by zero
    _assert(snap.chain("trunk").top_tension_kN > 10.0,
            "trunk must carry the BU with default weights")
    # A genuinely buoyant assembly is rejected with a clear message.
    buoyant = cs.uniform_assembly(3000.0, -50.0, diameter_m=0.05)
    scn2 = sc.bu_deployment(
        bathy, buoyant, buoyant, cs.Defaults(q_water_npm=200.0, mu=0.3),
        bu_weight_kN=15.0, bu_cda_m2=0.0, leg_length_m=140.0,
        bu_start_depth_m=25.0, static_only=True, target_ds_m=5.0,
    )
    try:
        qk.QuickOperationSimulator(scn2, bathy, tl.SimOptions()).settle()
        _assert(False, "buoyant assembly must raise a clear error")
    except ValueError as exc:
        _assert("buoyant" in str(exc) or "non-sinking" in str(exc), str(exc))


def test_frozen_lay_peels_to_tangency():
    """When the top moves away without payout, the frozen-lay span must
    peel cable off the bed and stay tangent at the touchdown — never render
    a kinked non-tangent span with a tension spike (the old chord-based
    pick-up rule held the TDP and produced exactly that)."""
    depth = 80.0
    bathy = bathy_mod.FlatBathymetry(depth)
    w = 180.0

    # Healthy tangent start: touchdown mid-way along a 400 m anchor run.
    p_top = np.array([0.0, 0.0, -5.0])
    L = 430.0
    sol = qk.leg_solution(p_top, (-400.0, 0.0), bathy, L, w)
    _assert(sol["tdp"] is not None, "init must touch down")
    path = np.asarray(sol["xyz"])[np.asarray(sol["contact"], dtype=bool)][::-1].copy()

    def depart_angle_deg(res):
        xyz = np.asarray(res["xyz"])
        i = int(np.argmax(np.asarray(res["contact"], dtype=bool)))
        a = xyz[i - 1] - xyz[i]
        return math.degrees(math.atan2(a[2], float(np.hypot(a[0], a[1]))))

    # Vessel moves away with no payout: cable must lift off tangentially.
    n0 = len(path)
    for _ in range(2):
        p_top = p_top + np.array([10.0, 0.0, 0.0])
        res = qk.leg_solution_frozen(p_top, path, L, w, bathy, 0.3)
        _assert(res["path_pts"] is not None, "must stay on the bed")
        path = res["path_pts"]
        ang = depart_angle_deg(res)
        _assert(abs(ang) < 5.0,
                f"span must stay tangent at the TDP (depart {ang:.1f} deg)")
        _assert(res["H_bottom"] < 2.0 * w * depth * 10,
                f"no tension spike at the TDP (H = {res['H_bottom']:.0f} N)")
    _assert(len(path) < n0, "tightening must peel laid cable off the bed")

    # Paying the length back out must re-lay and stay tangent too.
    for _ in range(3):
        L += 10.0
        res = qk.leg_solution_frozen(p_top, path, L, w, bathy, 0.3)
        path = res["path_pts"]
        ang = depart_angle_deg(res)
        _assert(abs(ang) < 5.0,
                f"re-laying must stay tangent (depart {ang:.1f} deg)")


def test_quick_deployment_runs_in_seconds_and_lands():
    h = 80.0
    bathy = bathy_mod.FlatBathymetry(h)
    asm = _telecom_assembly(3000.0)
    scn = sc.bu_deployment(
        bathy, asm, asm, DEFAULTS,
        bu_weight_kN=15.0, bu_cda_m2=1.5, leg_length_m=150.0,
        ship_speed_mps=0.3, payout_speed_mps=0.4, target_ds_m=5.0,
    )
    sim = qk.QuickOperationSimulator(scn, bathy, tl.SimOptions(max_move_m=8.0))
    t0 = time.perf_counter()
    res = sim.run()
    wall = time.perf_counter() - t0
    _assert(not res.aborted, "quick run aborted")
    _assert(wall < 5.0, f"quick deployment took {wall:.1f} s (want < 5 s)")
    zs = [s.junction_xyz["BU"][2] for s in res.snapshots if "BU" in s.junction_xyz]
    ups = sum(1 for a, b in zip(zs, zs[1:]) if b > a + 0.5)
    _assert(ups == 0, f"BU rose during quick lowering ({ups} upward moves)")
    _assert(zs[-1] < -h + 2.0, f"quick BU should land (z = {zs[-1]:.1f})")
    _assert(all(s.converged for s in res.snapshots), "quick snapshots converge")


def test_quick_full_two_sheave_deployment():
    """The quick backend drives the bu_full script (transfer + overboard
    events, balance controller) end to end."""
    bathy = bathy_mod.PlanarSlopeBathymetry(40.0, gx=0.0, gy=0.04)
    asm = _telecom_assembly(3000.0)
    scn = sc.bu_full_deployment(
        bathy, asm, asm, asm, DEFAULTS,
        bu_weight_kN=15.0, bu_cda_m2=1.5,
        laid_end_1_xy=(-60.0, 140.0), laid_end_2_xy=(-60.0, -140.0),
        tail_length_m=60.0, payout_mps=0.5, lay_speed_mps=0.35,
        target_ds_m=6.0,
    )
    opts = tl.SimOptions(max_move_m=8.0)
    opts.controller = ctl.TensionBalanceController("leg1", "leg2")
    # lay_history=False: this test validates the scripting/controller
    # integration against the idealised (frictionless) analytic backend,
    # where the tight balance tolerance below is meaningful. Frozen-lay
    # behaviour has dedicated tests in test_v3_manual.py.
    sim = qk.QuickOperationSimulator(scn, bathy, opts, lay_history=False)
    t0 = time.perf_counter()
    res = sim.run()
    wall = time.perf_counter() - t0
    _assert(not res.aborted, "quick bu_full aborted")
    _assert(wall < 10.0, f"quick bu_full took {wall:.1f} s")
    zs = [s.junction_xyz["BU"][2] for s in res.snapshots if "BU" in s.junction_xyz]
    _assert(len(zs) > 3 and zs[-1] < -35.0,
            f"BU must overboard and land (z = {zs[-1] if zs else None})")
    # Legs stayed balanced under the controller.
    last_bal = [abs(s.chain("leg1").top_tension_kN - s.chain("leg2").top_tension_kN)
                for s in res.snapshots
                if s.chain("leg1") is not None and s.chain("leg2") is not None
                and "BU" not in s.junction_xyz]
    _assert(last_bal and last_bal[-1] < 1.0,
            f"legs should balance ({last_bal[-1]:.2f} kN)" if last_bal else "no balance data")


# ---------------------------------------------------------------------------

def _result(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run_all():
    failures = []
    tests = [
        test_two_point_catenary_force_balance,
        test_leg_solution_closure_and_tangency,
        test_quick_matches_full_solver_static_hold,
        test_blank_segment_weight_uses_defaults,
        test_frozen_lay_peels_to_tangency,
        test_quick_deployment_runs_in_seconds_and_lands,
        test_quick_full_two_sheave_deployment,
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
