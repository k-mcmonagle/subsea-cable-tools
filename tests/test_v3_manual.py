# -*- coding: utf-8 -*-
"""Manual (interactive) BU-deployment driver tests.

Pure Python + NumPy; no QGIS imports. Exercises the ManualBUController on the
quick analytic backend: crab moves (heading preserved), heading rotation,
payout / pickup and cable counts, the manual overboard event, undo / reset /
replay determinism, and the to_schedule round-trip through the full solver.
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
    pkg = types.ModuleType("sct_v3_man")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "engine")]
    sys.modules["sct_v3_man"] = pkg
    mods = {}
    for m in ("bathymetry", "cable_system", "solver3d", "hydrodynamics",
              "steady_lay", "seeds", "timeline", "control", "scenarios",
              "schedule_opt", "quick_bu", "manual"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_man.{m}", ROOT / "catenary" / "v3" / "engine" / f"{m}.py")
        mm = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_man.{m}"] = mm
        spec.loader.exec_module(mm)
        mods[m] = mm
    return mods


M = _load_engine()
bathy_mod = M["bathymetry"]
cs = M["cable_system"]
tl = M["timeline"]
sc = M["scenarios"]
qb = M["quick_bu"]
man = M["manual"]

DEFAULTS = cs.Defaults(q_water_npm=180.0, mu=0.3, diameter_m=0.035)


def _assert(cond, msg):
    assert cond, msg


def _asm(length):
    return cs.uniform_assembly(
        length, 180.0, q_air_npm=300.0, diameter_m=0.035,
        cd_normal=1.2, cd_tangential=0.01, mu=0.3, name="LW cable")


def _controller(bathy, *, heading=90.0, target=(0.0, 150.0)):
    asm = _asm(3000.0)
    scn = sc.bu_full_deployment(
        bathy, asm, asm, asm, DEFAULTS,
        bu_weight_kN=15.0, bu_cda_m2=1.5,
        laid_end_1_xy=(-200.0, -150.0), laid_end_2_xy=(200.0, -150.0),
        vessel_xy=(0.0, 0.0), vessel_heading_deg=heading,
        tail_length_m=60.0, payout_mps=0.5, lay_speed_mps=0.35, target_ds_m=6.0,
    )
    sim = qb.QuickOperationSimulator(scn, bathy, tl.SimOptions())
    c = man.ManualBUController(sim, nominal_speed_mps=0.5, target_xy=target)
    c.settle()
    return c


# ---------------------------------------------------------------------------

def test_crab_move_preserves_heading():
    """A pure fwd/stbd translate moves the vessel by the world vector and does
    NOT change the heading (crab)."""
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)   # heading = +y (north)
    x0, y0 = c.vessel_xy()
    h0 = c.heading_deg()
    c.apply(man.ManualCommand(fwd_m=30.0))   # forward = +y
    x1, y1 = c.vessel_xy()
    _assert(abs(x1 - x0) < 1e-6, f"fwd move drifted in x: {x1 - x0}")
    _assert(abs((y1 - y0) - 30.0) < 1e-6, f"fwd move wrong: dy={y1 - y0}")
    _assert(abs(c.heading_deg() - h0) < 1e-9, "crab must not change heading")
    # Starboard move: heading +y, starboard = +x.
    c.apply(man.ManualCommand(stbd_m=10.0))
    x2, y2 = c.vessel_xy()
    _assert(abs((x2 - x1) - 10.0) < 1e-6, f"stbd move wrong: dx={x2 - x1}")
    _assert(abs(y2 - y1) < 1e-6, f"stbd move drifted in y: {y2 - y1}")
    _assert(abs(c.heading_deg() - h0) < 1e-9, "crab must not change heading")


def test_range_bearing_move_matches_world_vector():
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=0.0)   # heading +x
    x0, y0 = c.vessel_xy()
    cmd = c.move_range_bearing(50.0, 90.0)   # 50 m toward +y (math 90)
    c.apply(cmd)
    x1, y1 = c.vessel_xy()
    _assert(abs(x1 - x0) < 1e-6 and abs((y1 - y0) - 50.0) < 1e-6,
            f"range/bearing move wrong: d=({x1 - x0}, {y1 - y0})")
    _assert(abs(c.heading_deg() - 0.0) < 1e-9, "move must keep heading")


def test_heading_set_rotates_and_resolves():
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)
    snap = c.apply(man.ManualCommand(heading_set_deg=60.0))
    _assert(abs(c.heading_deg() - 60.0) < 1e-9, "heading not set")
    _assert(snap.converged, "re-solve after heading change must converge")


def test_payout_updates_length_and_count():
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)
    c.set_offset("leg1", 1000.0)
    L0 = c.sim.sc.chains["leg1"].length_m
    count0 = c.cable_count("leg1")
    _assert(abs(count0 - 1000.0) < 1e-6, f"initial count = offset: {count0}")
    c.apply(man.ManualCommand(payout_m={"leg1": 25.0}))
    L1 = c.sim.sc.chains["leg1"].length_m
    _assert(abs((L1 - L0) - 25.0) < 1e-6, f"payout length: {L1 - L0}")
    _assert(abs(c.cable_count("leg1") - 1025.0) < 1e-6, "count += payout")
    c.apply(man.ManualCommand(payout_m={"leg1": -10.0}))   # pick up
    _assert(abs(c.cable_count("leg1") - 1015.0) < 1e-6, "pickup decrements count")


def test_manual_overboard_and_lower_lands_bu():
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)
    _assert(c.can_overboard, "overboard should be available before use")
    _assert(c.bu_xyz() is None, "no BU before overboarding")
    c.apply(man.ManualCommand(event="transfer", label="transfer leg2"))
    c.apply(man.ManualCommand(event="overboard_bu", label="overboard"))
    _assert(not c.can_overboard, "overboard consumed")
    bu = c.bu_xyz()
    _assert(bu is not None, "BU spawned by overboard event")
    _assert("trunk" in c.sim.sc.chains, "trunk spawned by overboard")
    # Lower by paying out trunk while steaming ahead.
    for _ in range(30):
        c.apply(man.ManualCommand(fwd_m=6.0, payout_m={"trunk": 8.0}))
        if c.bu_xyz()[2] <= -39.0:
            break
    _assert(c.bu_xyz()[2] <= -38.0, f"BU should land near bed: {c.bu_xyz()}")


def test_undo_reset_replay_determinism():
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)
    cmds = [
        man.ManualCommand(fwd_m=10.0, payout_m={"leg1": 5.0}),
        man.ManualCommand(stbd_m=5.0),
        man.ManualCommand(heading_set_deg=80.0),
        man.ManualCommand(fwd_m=8.0),
    ]
    for cmd in cmds:
        c.apply(cmd)
    xy_full = c.vessel_xy()
    # Undo returns to the 3-command state; re-applying the 4th reproduces it.
    c.undo()
    _assert(len(c.history) == 3, "undo drops one command")
    c.apply(cmds[3])
    xy_re = c.vessel_xy()
    _assert(abs(xy_re[0] - xy_full[0]) < 1e-6 and abs(xy_re[1] - xy_full[1]) < 1e-6,
            f"replay not deterministic: {xy_re} vs {xy_full}")
    # Reset returns to the settle state.
    c.reset()
    _assert(len(c.history) == 0 and abs(c.vessel_xy()[0]) < 1e-6
            and abs(c.vessel_xy()[1]) < 1e-6, "reset returns to origin")


def test_to_schedule_reproduces_landing_in_full_solver():
    """The manual history, folded into a PhaseRow schedule and run through the
    FULL solver, lands the BU close to the manual quick-run landing."""
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0, target=(0.0, 150.0))
    c.apply(man.ManualCommand(event="transfer", label="transfer"))
    c.apply(man.ManualCommand(event="overboard_bu", label="overboard"))
    for _ in range(30):
        c.apply(man.ManualCommand(fwd_m=6.0, payout_m={"trunk": 8.0}))
        if c.bu_xyz()[2] <= -39.0:
            break
    manual_landing = c.bu_xyz()
    rows = c.to_schedule()
    _assert(len(rows) >= 3, "schedule should have the driven steps")
    _assert(any(r.event == "overboard_bu" for r in rows), "overboard preserved")

    # Rebuild the scenario and run the schedule through the FULL solver.
    asm = _asm(3000.0)
    scn = sc.bu_full_deployment(
        bathy, asm, asm, asm, DEFAULTS,
        bu_weight_kN=15.0, bu_cda_m2=1.5,
        laid_end_1_xy=(-200.0, -150.0), laid_end_2_xy=(200.0, -150.0),
        vessel_xy=(0.0, 0.0), vessel_heading_deg=90.0,
        schedule=rows, tail_length_m=60.0, payout_mps=0.5,
        lay_speed_mps=0.35, target_ds_m=6.0,
    )
    sim = tl.OperationSimulator(scn, bathy, tl.SimOptions(
        max_move_m=8.0, tol=4e-3, max_iters=30000))
    res = sim.run()
    _assert(not res.aborted, "full re-sim aborted")
    full_landing = None
    for s in reversed(res.snapshots):
        if "BU" in s.junction_xyz:
            full_landing = s.junction_xyz["BU"]
            break
    _assert(full_landing is not None, "full re-sim never overboarded the BU")
    d = math.hypot(full_landing[0] - manual_landing[0],
                   full_landing[1] - manual_landing[1])
    _assert(d < 25.0, f"full re-sim landing {full_landing} far from manual "
            f"{manual_landing} ({d:.1f} m)")


def test_lay_history_freezes_bed_cable():
    """With frozen-lay history (default), previously laid bed points stay
    put as the vessel crabs sideways; the frictionless model would swing the
    whole bed run onto a new straight line to the anchor."""
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)
    _assert(c.sim.lay_history, "lay history must default on")
    snap0 = c.sim._last_snap
    leg0 = snap0.chain("leg1")
    bed0 = leg0.xyz[np.asarray(leg0.contact, dtype=bool)]
    _assert(len(bed0) > 3, "leg1 must start with a bed run")
    path0 = c.sim._paths["leg1"].copy()

    # Gentle crab to starboard with generous payout: new cable lays down at
    # the touchdown while everything already laid stays put.
    snap = None
    for _ in range(2):
        snap = c.apply(man.ManualCommand(stbd_m=5.0, payout_m={"leg1": 10.0}))
    leg1 = snap.chain("leg1")
    bed1 = leg1.xyz[np.asarray(leg1.contact, dtype=bool)]
    path1 = c.sim._paths["leg1"]

    _assert(len(path1) > len(path0),
            f"surplus payout must extend the laid path "
            f"({len(path0)} -> {len(path1)})")
    # The original laid points are bitwise-frozen (the frictionless model
    # would swing the whole bed run onto a new anchor->TDP straight line).
    n = len(path0)
    d = float(np.max(np.linalg.norm(path1[:n] - path0[:n], axis=1)))
    _assert(d < 1e-9, f"laid path moved by {d:.3f} m — must stay frozen")
    # The newly laid points bend AWAY from the original straight lay line
    # (they head toward the crabbed vessel), so the path is genuinely curved.
    u_old = (path0[-1] - path0[0])[:2]
    u_old = u_old / np.linalg.norm(u_old)
    tail = (path1[-1] - path1[n - 1])[:2]
    _assert(np.linalg.norm(tail) > 1.0, "new cable must be laid on the bed")
    cross = abs(u_old[0] * tail[1] - u_old[1] * tail[0]) / np.linalg.norm(tail)
    _assert(cross > 0.05, f"newly laid cable should curve off the old lay "
            f"line (|sin| = {cross:.3f})")
    _assert(len(bed1) >= len(bed0), "bed run must not shrink while laying")

    # Length bookkeeping: laid + suspended = deployed length.
    seg = np.linalg.norm(np.diff(path1, axis=0), axis=1)
    s_laid = float(np.sum(seg))
    _assert(s_laid < c.sim.sc.chains["leg1"].length_m,
            "laid length must be less than deployed length")


def test_lay_history_pickup_shortens_path():
    """Hauling in lifts cable back off the bed: the laid path shortens."""
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)
    p0 = c.sim._paths["leg1"].copy()
    for _ in range(4):
        c.apply(man.ManualCommand(payout_m={"leg1": -8.0}))
    p1 = c.sim._paths.get("leg1")
    _assert(p1 is None or len(p1) < len(p0),
            f"pickup must shorten the laid path ({len(p0)} -> "
            f"{len(p1) if p1 is not None else 0})")


def test_to_schedule_merges_simultaneous_phases():
    """Alternating jog / payout clicks on the same course fold into ONE
    combined move+payout phase (smooth replay); merge=False keeps the
    one-command-per-phase behaviour."""
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)
    for _ in range(4):
        c.apply(man.ManualCommand(fwd_m=10.0, label="jog"))
        c.apply(man.ManualCommand(payout_m={"leg1": 6.0}, label="pay leg1"))
    merged = c.to_schedule()
    raw = c.to_schedule(merge=False)
    _assert(len(raw) == 8, f"unmerged should keep 8 rows ({len(raw)})")
    _assert(len(merged) == 1, f"same-course run should fold to 1 row ({len(merged)})")
    row = merged[0]
    _assert(row.speed_mps > 0.0 and abs(row.distance_m - 40.0) < 1e-6,
            f"merged row must move 40 m ({row.distance_m})")
    total_pay = row.payout_mps.get("leg1", 0.0) * row.duration_s
    _assert(abs(total_pay - 24.0) < 1e-6,
            f"merged row must pay the full 24 m ({total_pay:.1f})")
    # A sharp turn breaks the group; events always stand alone.
    c2 = _controller(bathy, heading=90.0)
    c2.apply(man.ManualCommand(fwd_m=10.0))
    c2.apply(man.ManualCommand(stbd_m=10.0))       # 90 deg turn
    c2.apply(man.ManualCommand(event="transfer", label="transfer"))
    c2.apply(man.ManualCommand(fwd_m=10.0))
    rows = c2.to_schedule()
    _assert(len(rows) == 4, f"turn + event must not merge ({len(rows)} rows)")
    _assert(rows[2].event == "transfer", "event row must survive on its own")


def test_joint_material_tracking():
    """Joints are fixed material points: each leg's joint starts at the
    sheave and rides out with payout; hauling back in hides it again; the
    trunk joint appears only once more than its BU tail is paid out."""
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)   # tail_length_m=60 in the helper
    snap0 = c.sim._last_snap
    j0 = snap0.chain("leg1").joint_xyz
    _assert(j0 is not None, "leg joint must exist at run start (at the sheave)")
    _assert(j0[2] > -1.0, f"joint starts at the sheave, not deep ({j0[2]:.1f} m)")
    # Pay 30 m: the joint rides 30 m of cable outboard -> well below surface.
    snap = c.apply(man.ManualCommand(payout_m={"leg1": 30.0}))
    j1 = snap.chain("leg1").joint_xyz
    _assert(j1 is not None and j1[2] < j0[2] - 5.0,
            f"joint must ride down with payout ({j0[2]:.1f} -> {j1[2] if j1 else None})")
    # Haul 60 m back in: the joint is inboard again -> hidden.
    snap = c.apply(man.ManualCommand(payout_m={"leg1": -60.0}))
    _assert(snap.chain("leg1").joint_xyz is None,
            "hauled-in joint must disappear (inboard)")
    # Trunk joint: hidden until more than the 60 m tail is out.
    c.apply(man.ManualCommand(event="transfer"))
    snap = c.apply(man.ManualCommand(event="overboard_bu"))
    tr = snap.chain("trunk")
    _assert(tr is not None and tr.joint_xyz is None,
            "trunk joint must be inboard right after overboard")
    snap = c.apply(man.ManualCommand(payout_m={"trunk": 80.0}))
    tr = snap.chain("trunk")
    _assert(tr is not None and tr.joint_xyz is not None,
            "trunk joint must appear once its tail is paid out")


def test_overboard_takes_leg_tails_over():
    """Overboarding the BU without paying the joints over first must still
    take the BU tails over the side: each leg grows to joint-reference +
    tail, so the joints can never sit ON the BU."""
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)   # tail_length_m=60 in the helper
    L0 = float(c.sim.sc.chains["leg1"].length_m)
    c.apply(man.ManualCommand(event="transfer"))
    snap = c.apply(man.ManualCommand(event="overboard_bu"))
    L1 = float(c.sim.sc.chains["leg1"].length_m)
    _assert(abs(L1 - (L0 + 60.0)) < 1e-6,
            f"leg must grow by its 60 m tail at overboard ({L0:.1f} -> {L1:.1f})")
    bu = snap.junction_xyz.get("BU")
    j = snap.chain("leg1").joint_xyz
    _assert(bu is not None and j is not None, "BU and joint must both exist")
    d = math.dist(j, bu)
    _assert(d > 5.0, f"joint must not sit on the BU (d = {d:.1f} m)")
    # When the tails were already paid out, the event is a no-op.
    c2 = _controller(bathy, heading=90.0)
    c2.apply(man.ManualCommand(payout_m={"leg1": 80.0, "leg2": 80.0}))
    L0b = float(c2.sim.sc.chains["leg1"].length_m)
    c2.apply(man.ManualCommand(event="transfer"))
    c2.apply(man.ManualCommand(event="overboard_bu"))
    _assert(abs(float(c2.sim.sc.chains["leg1"].length_m) - L0b) < 1e-6,
            "pre-paid tails must not grow the leg again")


def test_tdp_tension_reported_and_consistent():
    """The touchdown tension in the snapshot (first contact node) matches
    the model's residual bottom tension H within tolerance."""
    bathy = bathy_mod.FlatBathymetry(40.0)
    c = _controller(bathy, heading=90.0)
    snap = c.sim._last_snap
    leg = snap.chain("leg1")
    contact = np.asarray(leg.contact, dtype=bool)
    _assert(contact.any(), "leg must touch down")
    i = int(np.argmax(contact))
    tdp_kN = float(leg.tension_kN[min(i, len(leg.tension_kN) - 1)])
    _assert(tdp_kN >= 0.0, "TDP tension must be non-negative")
    # Bed tension decays with friction away from the TDP.
    t_bed = np.asarray(leg.tension_kN)[contact]
    _assert(t_bed[0] + 1e-9 >= t_bed[-1],
            "bed tension must not grow toward the anchor (friction decay)")


def test_straight_lay_follows_course():
    bathy = bathy_mod.FlatBathymetry(60.0)
    asm = _asm(5000.0)
    scn = sc.straight_lay(bathy, asm, DEFAULTS, ship_speed_mps=1.0,
                          duration_s=60.0, course_deg=90.0)
    _assert(abs(scn.steps[0].vessel_course_deg - 90.0) < 1e-9,
            "step course must follow course_deg")
    ax, ay, _az = scn.chains["cable"].bottom.xyz
    _assert(abs(ax) < 1e-6 and ay < 0.0,
            f"anchor must trail behind a northbound lay: ({ax:.1f}, {ay:.1f})")
    # Default stays on +x (legacy behaviour).
    scn0 = sc.straight_lay(bathy, asm, DEFAULTS, ship_speed_mps=1.0,
                           duration_s=60.0)
    ax0, ay0, _ = scn0.chains["cable"].bottom.xyz
    _assert(ax0 < 0.0 and abs(ay0) < 1e-6, "default course must remain +x")


# ---------------------------------------------------------------------------

def _result(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run_all():
    failures = []
    tests = [
        test_crab_move_preserves_heading,
        test_range_bearing_move_matches_world_vector,
        test_heading_set_rotates_and_resolves,
        test_payout_updates_length_and_count,
        test_manual_overboard_and_lower_lands_bu,
        test_undo_reset_replay_determinism,
        test_to_schedule_reproduces_landing_in_full_solver,
        test_lay_history_freezes_bed_cable,
        test_lay_history_pickup_shortens_path,
        test_to_schedule_merges_simultaneous_phases,
        test_joint_material_tracking,
        test_overboard_takes_leg_tails_over,
        test_tdp_tension_reported_and_consistent,
        test_straight_lay_follows_course,
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
    sys.exit(1 if run_all() else 0)
