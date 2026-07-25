# -*- coding: utf-8 -*-
"""Tests for the inverse BU planner, per-leg bottom-tension control,
multi-joint / cable-count tracking, the transfer substep floor, the quick
model's landing latch and its analytic bed-tension decay.

Pure Python + NumPy; no QGIS imports.
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
    pkg = types.ModuleType("sct_v3_plan")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "engine")]
    sys.modules["sct_v3_plan"] = pkg
    mods = {}
    for m in ("bathymetry", "cable_system", "solver3d", "hydrodynamics",
              "steady_lay", "seeds", "timeline", "control", "scenarios",
              "quick_bu", "bu_plan"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_plan.{m}", ROOT / "catenary" / "v3" / "engine" / f"{m}.py"
        )
        mm = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_plan.{m}"] = mm
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
bp = M["bu_plan"]

W = 30.0
DEFAULTS = cs.Defaults(q_water_npm=W, mu=0.4, diameter_m=0.04)


def _asm(length=5000.0):
    return cs.uniform_assembly(length, W, diameter_m=0.04, cd_normal=1.2,
                               cd_tangential=0.01, mu=0.4, name="cable")


def _assert(cond, msg):
    assert cond, msg


# ---------------------------------------------------------------------------

def _make_plan(bathy, aim, depth=800.0, h_t=3000.0):
    return bp.plan_bu_descent(
        bathy, (-1200.0, 500.0), (-1200.0, -500.0), aim,
        w_leg1_npm=W, w_leg2_npm=W, w_trunk_npm=W,
        bu_weight_N=20e3, H1_target_N=h_t, H2_target_N=h_t,
        sheave_height_m=5.0, spawn_depth_m=2.0, n_steps=16)


def test_plan_holds_targets_and_lands():
    """Symmetric flat-bed plan: feasible, targets held at every mid-water
    state, BU tracks the symmetry plane and lands on the aim point."""
    bathy = bathy_mod.FlatBathymetry(800.0)
    plan = _make_plan(bathy, (0.0, 0.0))
    _assert(plan.feasible, f"plan infeasible: {plan.warnings}")
    _assert(not plan.warnings, f"unexpected warnings: {plan.warnings}")
    for st in plan.states:
        _assert(abs(st.bu_xy[1]) < 1.0, "symmetric plan must track y=0")
        _assert(abs(st.leg_H_N["leg1"] - 3000.0) < 1.0
                and abs(st.leg_H_N["leg2"] - 3000.0) < 1.0,
                "leg targets must be held exactly in the analytic plan")
    last = plan.states[-1]
    _assert(math.hypot(last.bu_xy[0], last.bu_xy[1]) < 1.0,
            f"landing {last.bu_xy} must hit the aim point")
    # Required lengths equal the bed path from each anchor to the landing.
    chord = math.hypot(1200.0, 500.0)
    _assert(abs(plan.leg_lengths_m["leg1"] - chord) < 2.0,
            "flat-bed leg length must equal the plan chord")
    # Trunk suspended length grows monotonically (payout is positive).
    s_t = [st.trunk_susp_m for st in plan.states]
    _assert(all(b >= a - 1e-6 for a, b in zip(s_t, s_t[1:])),
            "trunk suspended length must grow through the descent")
    rows = bp.plan_to_schedule(plan, payout_mps=0.5)
    _assert(rows and rows[0].event == "overboard_bu",
            "first planned phase must carry the overboard event")
    _assert(all(r.payout_mps.get("trunk", 0.0) >= 0.0 for r in rows),
            "planned trunk payout must never be negative")


def _run_plan_in_quick_sim(bathy, plan, tail=90.0, margin=20.0, payout=0.5):
    asm = _asm()
    setup = sc.default_bu_schedule(depth_m=800.0, payout_mps=payout,
                                   tail_length_m=tail)[:3]
    rows = setup + bp.plan_to_schedule(plan, payout_mps=payout)
    scen = sc.bu_full_deployment(
        bathy, asm, asm, asm, DEFAULTS,
        bu_weight_kN=20.0,
        laid_end_1_xy=(-1200.0, 500.0), laid_end_2_xy=(-1200.0, -500.0),
        leg1_deployed_m=plan.leg_lengths_m["leg1"] - tail - margin,
        leg2_deployed_m=plan.leg_lengths_m["leg2"] - tail - margin,
        vessel_xy=plan.states[0].vessel_xy,
        vessel_heading_deg=plan.states[0].course_deg,
        schedule=rows, payout_mps=payout, tail_length_m=tail,
    )
    sim = qk.QuickOperationSimulator(scen, bathy)
    res = sim.run()
    landed = None
    for snap in reversed(res.snapshots):
        xyz = snap.junction_xyz.get("BU")
        if xyz is not None:
            landed = (xyz[0], xyz[1], xyz[2])
            break
    return res, landed


def test_plan_verified_by_quick_sim_with_refinement():
    """The planned schedule, replayed through the quick simulator, must land
    the BU and hold the leg touchdown tensions near target; one refinement
    round must land within ~15 m of the target."""
    bathy = bathy_mod.FlatBathymetry(800.0)
    target = (0.0, 0.0)

    def simulate(plan):
        _res, landed = _run_plan_in_quick_sim(bathy, plan)
        return None if landed is None else (landed[0], landed[1])

    plan = bp.refine_landing(lambda aim: _make_plan(bathy, aim),
                             simulate, target, rounds=2, tol_m=5.0)
    _assert(plan.feasible, "refined plan must stay feasible")
    res, landed = _run_plan_in_quick_sim(bathy, plan)
    _assert(landed is not None and abs(landed[2] + 800.0) < 2.0,
            "BU must land on the bed")
    miss = math.hypot(landed[0] - target[0], landed[1] - target[1])
    _assert(miss < 15.0, f"refined landing miss {miss:.1f} m (want < 15)")
    # Leg touchdown tensions near target through the mid-descent.
    errs = []
    for snap in res.snapshots:
        bu = snap.junction_xyz.get("BU")
        if bu is None or bu[2] < -700.0 or bu[2] > -100.0:
            continue     # judge only the mid-water portion
        for leg in ("leg1", "leg2"):
            t = ctl.tdp_tension_kN(snap, leg)
            if t is not None:
                errs.append(abs(t - 3.0))
    _assert(errs, "descent must produce leg TDP readings")
    _assert(float(np.mean(errs)) < 1.0,
            f"mean leg TDP error {np.mean(errs):.2f} kN vs 3.0 kN target")


def test_bu_payout_controller_factory():
    """Absolute leg targets supersede the relative balance; trunk target
    composes; nothing requested -> None."""
    _assert(ctl.bu_payout_controllers() is None, "no controllers -> None")
    c = ctl.bu_payout_controllers(balance_legs=True)
    _assert(isinstance(c, ctl.TensionBalanceController), "balance only")
    c = ctl.bu_payout_controllers(
        balance_legs=True,
        leg_bottom_targets_kN={"leg1": 3.0, "leg2": 2.5})
    _assert(isinstance(c, ctl.CompositeController)
            and len(c.controllers) == 2
            and all(isinstance(x, ctl.BottomTensionController)
                    for x in c.controllers),
            "leg targets must supersede the balance controller")
    c = ctl.bu_payout_controllers(balance_legs=True,
                                  trunk_bottom_target_kN=4.0)
    _assert(isinstance(c, ctl.CompositeController)
            and isinstance(c.controllers[0], ctl.TensionBalanceController)
            and c.controllers[1].chain == "trunk",
            "balance + trunk target must compose")
    c = ctl.bu_payout_controllers(leg_bottom_targets_kN={"leg1": 0.0},
                                  trunk_bottom_target_kN=4.0)
    _assert(isinstance(c, ctl.BottomTensionController)
            and c.chain == "trunk", "zero leg targets are ignored")


def test_transfer_gets_minimum_substeps():
    """A pure-transfer phase (no motion, no payout) must still be lerped
    over the configured minimum number of substeps."""
    bathy = bathy_mod.FlatBathymetry(100.0)
    asm = _asm(1000.0)
    scen = sc.bu_full_deployment(
        bathy, asm, asm, asm, DEFAULTS, bu_weight_kN=10.0,
        laid_end_1_xy=(-200.0, 100.0), laid_end_2_xy=(-200.0, -100.0))
    sim = qk.QuickOperationSimulator(scen, bathy)
    step = tl.Step(duration_s=120.0, vessel_speed_mps=0.0, payout_mps={},
                   transfer=tl.SheaveTransfer("leg2", "stbd", "port"))
    n = sim._substeps(step)
    _assert(n >= sim.opt.transfer_min_substeps,
            f"pure-transfer phase got {n} substeps "
            f"(want >= {sim.opt.transfer_min_substeps})")
    plain = tl.Step(duration_s=120.0, vessel_speed_mps=0.0, payout_mps={})
    _assert(sim._substeps(plain) == 1, "a hold phase still takes one substep")


def test_quick_landing_latch_holds():
    """Once the quick model lands the BU it must stay latched to the bed for
    the remaining steps (no re-lift, position on the bed)."""
    bathy = bathy_mod.FlatBathymetry(60.0)
    asm = _asm(2000.0)
    scen = sc.bu_deployment(
        bathy, asm, asm, DEFAULTS, bu_weight_kN=12.0, leg_length_m=150.0,
        ship_speed_mps=0.3, payout_speed_mps=0.35,
        duration_s=600.0)     # much longer than needed to land in 60 m
    sim = qk.QuickOperationSimulator(scen, bathy)
    res = sim.run()
    zs = [s.junction_xyz["BU"][2] for s in res.snapshots
          if "BU" in s.junction_xyz]
    _assert(abs(zs[-1] + 60.0) < 0.5, "BU must land on the bed")
    i_land = next(i for i, z in enumerate(zs) if z <= -59.5)
    for z in zs[i_land:]:
        _assert(abs(z + 60.0) < 0.5, "BU must stay on the bed once landed")
    _assert(sim._landed, "landing latch must be set")


def test_frozen_bed_tension_decay_formula():
    """Along the frozen as-laid path the displayed bed tension must decay
    from the touchdown as max(0, H - mu*w*s)."""
    bathy = bathy_mod.FlatBathymetry(200.0)
    mu = 0.4
    # Straight frozen path on the bed, anchor-first, ending at the TDP.
    xs = np.linspace(900.0, 300.0, 41)
    path = np.column_stack([xs, np.zeros_like(xs),
                            np.full_like(xs, -200.0)])
    p_top = np.array([0.0, 0.0, -20.0])
    # Length chosen so the span is tangent at the frozen TDP (path 600 m +
    # ~360 m tangent catenary over D = 300, h = 180) — the decay formula
    # applies to the tangent state, not the slack-pooling one.
    L = 960.0
    sol = qk.leg_solution_frozen(p_top, path, L, W, bathy, mu)
    H = sol["H_bottom"]
    _assert(H > 0.0, "span must carry a positive bottom tension")
    xyz = sol["xyz"]
    tension = sol["tension"]
    contact = sol["contact"]
    i0 = int(np.argmax(contact))        # touchdown node
    seg = np.linalg.norm(np.diff(xyz[i0:], axis=0), axis=1)
    s_bed = np.concatenate([[0.0], np.cumsum(seg)])
    expect = np.maximum(0.0, H - mu * W * s_bed)
    got = tension[i0:]
    _assert(np.allclose(got, expect, atol=1.0),
            f"bed tension must follow max(0, H - mu*w*s); max err "
            f"{np.max(np.abs(got - expect)):.2f} N")
    _assert(got[-1] == 0.0 or s_bed[-1] < H / (mu * W),
            "tension must reach zero once friction has absorbed H")


def test_named_joints_and_counts_in_snapshots():
    """User-defined named joints resolve to positions once outboard, and
    count references produce top-end counts in every snapshot."""
    bathy = bathy_mod.FlatBathymetry(100.0)
    asm = _asm(2000.0)
    scen = sc.bu_full_deployment(
        bathy, asm, asm, asm, DEFAULTS, bu_weight_kN=10.0,
        laid_end_1_xy=(-250.0, 120.0), laid_end_2_xy=(-250.0, -120.0),
        leg1_joints=[("repair A", 100.0)],
        count_refs={"leg1": 5000.0, "leg2": 7000.0},
    )
    sim = qk.QuickOperationSimulator(scen, bathy)
    snap = sim.settle()
    c1 = snap.chain("leg1")
    labels = [lbl for lbl, _ in c1.joints_xyz]
    _assert("joint" in labels, "automatic splice joint must be tracked")
    _assert("repair A" in labels, "named joint must be tracked")
    named = dict(c1.joints_xyz)["repair A"]
    _assert(abs(named[2] + 100.0) < 1.5,
            "a joint 100 m up a laid leg must sit on the bed")
    # Counts: top count = reference + deployed length (counts increase
    # toward the vessel).
    st1 = scen.chains["leg1"]
    _assert(abs(c1.count_top_m - (5000.0 + st1.length_m)) < 1e-6,
            "leg1 top count must be ref + deployed length")
    c3 = snap.chain("leg2")
    _assert(c3.count_top_m is not None and c3.count_top_m > 7000.0,
            "leg2 count must come from its own reference")
    # Trunk has no reference -> no count.
    # (Trunk doesn't exist pre-overboard; check a leg without ref instead.)
    scen2 = sc.bu_full_deployment(
        bathy, asm, asm, asm, DEFAULTS, bu_weight_kN=10.0,
        laid_end_1_xy=(-250.0, 120.0), laid_end_2_xy=(-250.0, -120.0))
    snap2 = qk.QuickOperationSimulator(scen2, bathy).settle()
    _assert(snap2.chain("leg1").count_top_m is None,
            "no reference -> no count")


def test_fixed_lengths_plan_lands_on_bisector():
    """Lowering-only inverse: with the leg lengths GIVEN (laid to the
    jointing position), the landing point is an output — on a flat bed with
    symmetric leads it must sit on the bisector, lay-away side, and the
    planned course must run along it."""
    bathy = bathy_mod.FlatBathymetry(100.0)
    L = 300.0
    a1 = math.radians(150.0)
    a2 = math.radians(210.0)
    A1 = (0.92 * L * math.cos(a1), 0.92 * L * math.sin(a1))
    A2 = (0.92 * L * math.cos(a2), 0.92 * L * math.sin(a2))
    plan = bp.plan_bu_descent_fixed_lengths(
        bathy, A1, A2, L, L,
        w_leg1_npm=W, w_leg2_npm=W, w_trunk_npm=W,
        bu_weight_N=15e3, H1_target_N=3000.0, H2_target_N=3000.0,
        sheave_height_m=5.0, spawn_depth_m=10.0)
    _assert(plan.feasible, f"plan infeasible: {plan.warnings}")
    lx, ly = plan.landing_xy
    _assert(abs(ly) < 1.0, f"landing must sit on the bisector (y={ly:.1f})")
    _assert(lx > 0.0, "landing must be on the lay-away side (+x)")
    # Bed path from each anchor to the landing equals the leg length.
    _assert(abs(bp.bed_path_length(bathy, A1, plan.landing_xy) - L) < 1.0,
            "leg 1 must be exactly laid out at landing")
    for st in plan.states[1:]:
        _assert(abs(st.leg_H_N["leg1"] - 3000.0) < 1.0,
                "targets held through the descent")
    _assert(abs(plan.states[-1].course_deg % 360.0) < 2.0
            or abs(plan.states[-1].course_deg % 360.0 - 360.0) < 2.0,
            f"lay-away course must be the bisector (+x), got "
            f"{plan.states[-1].course_deg:.1f}")


def test_planned_lowering_schedule_holds_targets_in_sim():
    """The planned vessel track + payout, run through the quick simulator
    as a bu_deployment schedule, must hold the leg touchdown tensions near
    target through the later descent and land the BU near the planned
    point."""
    bathy = bathy_mod.FlatBathymetry(100.0)
    L = 300.0
    asm = _asm(2000.0)
    a1, a2 = math.radians(150.0), math.radians(210.0)
    A1 = (0.92 * L * math.cos(a1), 0.92 * L * math.sin(a1))
    A2 = (0.92 * L * math.cos(a2), 0.92 * L * math.sin(a2))
    spawn = 10.0
    plan = bp.plan_bu_descent_fixed_lengths(
        bathy, A1, A2, L, L,
        w_leg1_npm=W, w_leg2_npm=W, w_trunk_npm=W,
        bu_weight_N=15e3, H1_target_N=3000.0, H2_target_N=3000.0,
        sheave_height_m=5.0, spawn_depth_m=spawn)
    _assert(plan.feasible, "plan must be feasible")
    rows = bp.plan_to_schedule(plan, payout_mps=0.4,
                               overboard_event=False, start_xy=(0.0, 0.0),
                               start_trunk_susp_m=5.0 + spawn)
    _assert(rows and not any(r.event for r in rows),
            "lowering rows must carry no events")
    scen = sc.bu_deployment(
        bathy, asm, asm, DEFAULTS,
        bu_weight_kN=15.0, leg_length_m=L,
        leg_azimuths_deg=(150.0, 210.0),
        bu_start_depth_m=spawn, schedule=rows)
    sim = qk.QuickOperationSimulator(scen, bathy)
    res = sim.run()
    bu_final = None
    errs = []
    for snap in res.snapshots:
        bu = snap.junction_xyz.get("BU")
        if bu is None:
            continue
        bu_final = bu
        if -95.0 < bu[2] < -40.0:      # later descent, before touchdown
            for leg in ("leg1", "leg2"):
                t = ctl.tdp_tension_kN(snap, leg)
                if t is not None:
                    errs.append(abs(t - 3.0))
    _assert(bu_final is not None and abs(bu_final[2] + 100.0) < 1.0,
            "BU must land on the bed")
    miss = math.hypot(bu_final[0] - plan.landing_xy[0],
                      bu_final[1] - plan.landing_xy[1])
    _assert(miss < 20.0, f"landing {miss:.1f} m from plan (want < 20)")
    _assert(errs and float(np.mean(errs)) < 1.0,
            f"mean leg TDP error {np.mean(errs) if errs else -1:.2f} kN "
            "vs 3.0 kN target in the later descent")


def _layback_anchors(bathy, spawn, L, target_N, w, az_math_degs):
    """Layback-consistent anchors (mirrors the UI's planned-lowering
    geometry): TDP at the target-tension layback, bed run to the anchor."""
    a = target_N / w
    anchors = []
    for az in az_math_degs:
        ux, uy = math.cos(math.radians(az)), math.sin(math.radians(az))
        D0 = 0.0
        for _ in range(3):
            h = float(bathy.depth_at(D0 * ux, D0 * uy)) - spawn
            D0 = a * math.acosh(1.0 + h / a)
        s0 = a * math.sinh(D0 / a)
        assert L - s0 > 10.0, "leg too short for this test geometry"
        anchors.append(((D0 + L - s0) * ux, (D0 + L - s0) * uy))
    return anchors


def test_consistent_start_no_backtracking():
    """With the layback-consistent start, the planned vessel track begins
    at (about) the start position and only ever advances along the
    lay-away course — no doubling back to relieve an over-tight start —
    and the very first simulated state already sits at the leg targets."""
    bathy = bathy_mod.FlatBathymetry(100.0)
    spawn, L, target = 10.0, 600.0, 3000.0
    anchors = _layback_anchors(bathy, spawn, L, target, W, [150.0, 210.0])
    plan = bp.plan_bu_descent_fixed_lengths(
        bathy, anchors[0], anchors[1], L, L,
        w_leg1_npm=W, w_leg2_npm=W, w_trunk_npm=W,
        bu_weight_N=15e3, H1_target_N=target, H2_target_N=target,
        sheave_height_m=5.0, spawn_depth_m=spawn)
    _assert(plan.feasible, f"plan infeasible: {plan.warnings}")
    v0 = plan.states[0].vessel_xy
    _assert(math.hypot(v0[0], v0[1]) < 20.0,
            f"planned start {v0} must be near the actual start (0, 0)")
    # Progress along the lay-away course (+x) must be monotone.
    xs = [st.vessel_xy[0] for st in plan.states]
    _assert(all(b >= a - 1.0 for a, b in zip(xs, xs[1:])),
            f"vessel track must not double back: {['%.0f' % x for x in xs]}")

    rows = bp.plan_to_schedule(plan, payout_mps=0.4, overboard_event=False,
                               start_xy=(0.0, 0.0),
                               start_trunk_susp_m=5.0 + spawn)
    asm = _asm(30000.0)
    scen = sc.bu_deployment(
        bathy, asm, asm, DEFAULTS, bu_weight_kN=15.0, leg_length_m=L,
        leg_azimuths_deg=(150.0, 210.0), bu_start_depth_m=spawn,
        schedule=rows, leg_far_ends_xy=anchors)
    sim = qk.QuickOperationSimulator(scen, bathy)
    snap = sim.settle()
    for leg in ("leg1", "leg2"):
        t = ctl.tdp_tension_kN(snap, leg)
        _assert(t is not None and abs(t - 3.0) < 0.8,
                f"{leg} must START at the target tension (got {t})")


def test_deep_water_consistent_start():
    """5000 m water, 5 kN target, 20 km legs (the reported failure case):
    the start state must sit at the target tension — not thousands of kN —
    and the planned track must not back up."""
    bathy = bathy_mod.FlatBathymetry(5000.0)
    spawn, L, target = 10.0, 20000.0, 5000.0
    anchors = _layback_anchors(bathy, spawn, L, target, W, [150.0, 210.0])
    plan = bp.plan_bu_descent_fixed_lengths(
        bathy, anchors[0], anchors[1], L, L,
        w_leg1_npm=W, w_leg2_npm=W, w_trunk_npm=W,
        bu_weight_N=20e3, H1_target_N=target, H2_target_N=target,
        sheave_height_m=5.0, spawn_depth_m=spawn)
    _assert(plan.feasible, f"plan infeasible: {plan.warnings}")
    v0 = plan.states[0].vessel_xy
    _assert(math.hypot(v0[0], v0[1]) < 60.0,
            f"planned start {v0} must be near (0, 0) in deep water too")
    xs = [st.vessel_xy[0] for st in plan.states]
    _assert(all(b >= a - 2.0 for a, b in zip(xs, xs[1:])),
            "deep-water track must not double back")
    rows = bp.plan_to_schedule(plan, payout_mps=0.5, overboard_event=False,
                               start_xy=(0.0, 0.0),
                               start_trunk_susp_m=5.0 + spawn)
    asm = _asm(40000.0)
    scen = sc.bu_deployment(
        bathy, asm, asm, DEFAULTS, bu_weight_kN=20.0, leg_length_m=L,
        leg_azimuths_deg=(150.0, 210.0), bu_start_depth_m=spawn,
        schedule=rows, leg_far_ends_xy=anchors)
    snap = qk.QuickOperationSimulator(scen, bathy).settle()
    for leg in ("leg1", "leg2"):
        t = ctl.tdp_tension_kN(snap, leg)
        _assert(t is not None and abs(t - 5.0) < 1.5,
                f"{leg} must start near the 5 kN target (got {t} kN)")


# ---------------------------------------------------------------------------

def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[ERROR] {t.__name__}: {type(exc).__name__}: {exc}")
    print()
    print("All checks passed." if failed == 0 else f"{failed} test(s) failed.")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
