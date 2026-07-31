# -*- coding: utf-8 -*-
"""BUIntegration: the BU-datum description of the whole Y.

Pins down the one-datum contract: every joint, count and assembly feature is
entered as (branch, metres from the BU) and must come out at that distance —
on the compiled ChainState kwargs, on the lowering scenario, and on the quick
simulator's snapshots. Also covers validation and serialisation.

Pure Python + NumPy; no Qt/QGIS imports.
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
    pkg = types.ModuleType("sct_v3_bi")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "engine")]
    sys.modules["sct_v3_bi"] = pkg
    mods = {}
    for m in ("bathymetry", "cable_system", "seeds", "solver3d", "hydrodynamics",
              "steady_lay", "timeline", "control", "scenarios", "quick_bu",
              "bu_integration"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_bi.{m}", ROOT / "catenary" / "v3" / "engine" / f"{m}.py")
        mm = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_bi.{m}"] = mm
        spec.loader.exec_module(mm)
        mods[m] = mm
    return mods


M = _load_engine()
cs = M["cable_system"]
tl = M["timeline"]
qb = M["quick_bu"]
scen = M["scenarios"]
bath = M["bathymetry"]
bi = M["bu_integration"]


def sample_integration():
    """Deliberately asymmetric: each branch has its own tail so a mix-up
    between branches (or datum ends) shows up in the numbers."""
    integ = bi.BUIntegration(
        bu_weight_kN=18.0, bu_cda_m2=1.2,
        trunk=bi.default_branch(tail_length_m=60.0, tail_q_npm=350.0,
                                joint_name="trunk joint", main_q_npm=140.0),
        leg1=bi.default_branch(tail_length_m=90.0, tail_q_npm=300.0,
                               joint_name="leg1 joint", main_q_npm=120.0),
        leg2=bi.default_branch(tail_length_m=150.0, tail_q_npm=400.0,
                               joint_name="leg2 joint", main_q_npm=110.0),
    )
    integ.leg1.count_at_bu_m = 10000.0
    integ.leg1.count_increases_from_bu = False   # count grows toward the BU
    integ.leg2.count_at_bu_m = 20000.0
    integ.leg2.count_increases_from_bu = True
    integ.trunk.count_at_bu_m = 30000.0
    integ.trunk.count_increases_from_bu = True
    integ.leg1.joints.append(("repair joint", 400.0))
    return integ


# --- structure & validation -------------------------------------------------

def test_default_integration_is_valid():
    assert bi.default_integration().problems() == []
    assert sample_integration().problems() == []


def test_body_rows_are_tracked_automatically():
    br = sample_integration().leg1
    marks = br.joints_from_bu()
    assert ("leg1 joint", 90.0) in marks
    assert ("repair joint", 400.0) in marks


def test_validation_catches_bad_makeups():
    br = bi.default_branch()
    br.items.append(cs.SegmentSpec(name="extra fill", fill=True))
    msgs = " ".join(br.problems("leg1"))
    assert "more than one remainder" in msgs

    br2 = bi.default_branch()
    br2.items.append(cs.BodySpec(name="late joint", point_load_kN=1.0))
    msgs = " ".join(br2.problems("leg1"))
    assert "no fixed distance from the BU" in msgs

    br3 = bi.BranchMakeup(items=[
        cs.SegmentSpec(name="tail", length_m=90.0, q_water_npm=300.0)])
    msgs = " ".join(br3.problems("trunk"))
    assert "no remainder (fill) row" in msgs

    br4 = bi.default_branch()
    br4.joints.append(("bad", -5.0))
    assert any("negative" in p for p in br4.problems("leg2"))

    try:
        sample_integration().branch("leg3")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for an unknown branch")


def test_serialisation_round_trip():
    integ = sample_integration()
    clone = bi.BUIntegration.from_dict(integ.to_dict())
    assert clone.bu_weight_kN == 18.0 and clone.bu_cda_m2 == 1.2
    assert clone.leg1.count_at_bu_m == 10000.0
    assert clone.leg1.count_increases_from_bu is False
    assert clone.leg1.joints == [("repair joint", 400.0)]
    for name in bi.BRANCHES:
        a, b = integ.branch(name), clone.branch(name)
        assert cs.assembly_to_json_data(a.items) == cs.assembly_to_json_data(b.items)
    assert clone.problems() == []


# --- compilation to chain kwargs --------------------------------------------

def test_trunk_kwargs_use_the_bottom_end_identity():
    integ = sample_integration()
    kw = integ.chain_kwargs("trunk", bu_at="bottom")
    assert kw["assembly_datum"] == "bottom_end"
    assert ("trunk joint", 60.0) in kw["joints"]     # unchanged: bottom IS the BU
    assert kw["count_ref_m"] == 30000.0
    assert kw["count_to_top"] is True


def test_leg_kwargs_convert_to_metres_from_the_laid_end():
    integ = sample_integration()
    L = 1200.0
    kw = integ.chain_kwargs("leg1", bu_at="top", length_m=L)
    assert kw["assembly_datum"] == "top_end"
    joints = dict(kw["joints"])
    assert math.isclose(joints["leg1 joint"], L - 90.0)
    assert math.isclose(joints["repair joint"], L - 400.0)
    # leg1 counts DECREASE away from the BU: laid-end ref = 10000 - 1200.
    assert math.isclose(kw["count_ref_m"], 8800.0)
    assert kw["count_to_top"] is True
    # A joint beyond the deployed length is dropped, not wrapped.
    kw_short = integ.chain_kwargs("leg1", bu_at="top", length_m=200.0)
    assert "repair joint" not in dict(kw_short["joints"])

    try:
        integ.chain_kwargs("leg1", bu_at="top")
    except ValueError as exc:
        assert "length_m" in str(exc)
    else:
        raise AssertionError("expected ValueError without length_m")


def test_counts_agree_with_the_engine_count_convention():
    """count_at(branch, s_from_bu) must equal the engine's count_at_m at the
    same material point, for both count directions and both datum ends."""
    integ = sample_integration()
    cases = (("leg1", "top", 1200.0), ("leg2", "top", 800.0),
             ("trunk", "bottom", 500.0))
    for name, bu_at, L in cases:
        kw = integ.chain_kwargs(name, bu_at=bu_at, length_m=L)
        st = tl.ChainState(
            name=name, assembly=kw["assembly"], defaults=cs.Defaults(),
            length_m=L, top=tl.Attachment("free"), bottom=tl.Attachment("free"),
            shape=np.zeros((2, 3)), assembly_datum=kw["assembly_datum"],
            joints=kw["joints"], count_ref_m=kw["count_ref_m"],
            count_to_top=kw["count_to_top"])
        for s_bu in (0.0, 90.0, L / 2.0, L):
            s_bottom = s_bu if bu_at == "bottom" else L - s_bu
            got = tl.count_at_m(st, s_bottom)
            want = integ.count_at(name, s_bu)
            assert math.isclose(got, want), (name, s_bu, got, want)
    # The far-end cross-check figure is the same function at s = L.
    assert math.isclose(integ.far_end_count("leg1", 1200.0), 8800.0)


# --- the lowering scenario ---------------------------------------------------

def _lowering_scenario(bathy, integ, L1=600.0, L2=700.0, **over):
    kwargs = dict(defaults=cs.Defaults(), target_ds_m=10.0, static_only=True)
    kwargs.update(over)
    return scen.bu_deployment(
        bathy, **integ.lowering_inputs(leg1_length_m=L1, leg2_length_m=L2),
        **kwargs)


def test_lowering_inputs_build_an_asymmetric_scenario():
    integ = sample_integration()
    sc = _lowering_scenario(bath.FlatBathymetry(depth_m=120.0), integ)
    assert sc.chains["leg1"].length_m == 600.0
    assert sc.chains["leg2"].length_m == 700.0
    assert sc.chains["leg1"].assembly_datum == "top_end"
    assert sc.chains["trunk"].assembly_datum == "bottom_end"
    assert sc.junctions["BU"].load_kN == 18.0
    # Each leg carries its own tail: the quick model's mean weights differ.
    w1 = qb.mean_weight_npm(sc.chains["leg1"])
    w2 = qb.mean_weight_npm(sc.chains["leg2"])
    want1 = (90.0 * 300.0 + 510.0 * 120.0) / 600.0
    want2 = (150.0 * 400.0 + 550.0 * 110.0) / 700.0
    assert math.isclose(w1, want1, rel_tol=1e-9), (w1, want1)
    assert math.isclose(w2, want2, rel_tol=1e-9), (w2, want2)
    # Joints arrived in per-chain coordinates.
    assert ("leg2 joint", 700.0 - 150.0) in sc.chains["leg2"].joints
    assert ("trunk joint", 60.0) in sc.chains["trunk"].joints


def test_quick_settle_reports_bu_datum_marks_and_counts():
    """End to end: settle the static hold on the quick model and check the
    snapshot's joints sit at their BU distances and the top counts close."""
    integ = sample_integration()
    bathy = bath.FlatBathymetry(depth_m=120.0)
    sc = _lowering_scenario(bathy, integ, bu_start_depth_m=60.0)
    sim = qb.QuickOperationSimulator(sc, bathy)
    snap = sim.settle()
    assert snap.converged
    bu = np.asarray(snap.junction_xyz["BU"], dtype=float)
    for cname, tail in (("leg1", 90.0), ("leg2", 150.0)):
        c = snap.chain(cname)
        marks = dict(c.joints_xyz)
        label = f"{cname} joint"
        assert label in marks, (cname, marks)
        # Arc distance from the BU (the chain's top node) to the mark.
        p = np.asarray(marks[label], dtype=float)
        seg = np.linalg.norm(np.diff(c.xyz, axis=0), axis=1)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        d = np.linalg.norm(c.xyz - p[None, :], axis=1)
        s_mark = float(s[int(np.argmin(d))])
        # Sampled-arc tolerance: the polyline is coarse near the touchdown.
        assert abs(s_mark - tail) < 25.0, (cname, s_mark, tail)
        assert float(np.linalg.norm(c.xyz[0] - bu)) < 1e-6
        # Count at the leg's top (the BU end) must be the entered BU count.
        assert math.isclose(c.count_top_m, integ.branch(cname).count_at_bu_m)
    trunk = snap.chain("trunk")
    # Trunk count at the vessel end = BU count + deployed trunk length.
    assert math.isclose(trunk.count_top_m, 30000.0 + trunk.length_m)


def test_lowering_runs_to_landing_on_the_quick_model():
    integ = sample_integration()
    bathy = bath.FlatBathymetry(depth_m=120.0)
    sc = _lowering_scenario(bathy, integ, static_only=False,
                            payout_speed_mps=0.5, ship_speed_mps=0.4)
    sim = qb.QuickOperationSimulator(sc, bathy)
    out = sim.run()
    assert not out.aborted
    z_first = out.snapshots[0].junction_xyz["BU"][2]
    z_last = out.snapshots[-1].junction_xyz["BU"][2]
    assert z_last < z_first          # descended
    assert z_last <= -119.0          # ...and landed on the 120 m bed


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
