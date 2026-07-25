# -*- coding: utf-8 -*-
"""Assembly referencing: remainder rows, the BU datum, and fit warnings.

The BU scenarios describe every line outward from the branching unit (tail,
tail joint, then the cable beyond). These tests pin that mapping down: a
feature entered at 90 m from the BU must land 90 m from the BU whatever
length the geometry gives the line, on the full solver's per-element
properties AND on the quick model's per-chain means.

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
    pkg = types.ModuleType("sct_v3_ad")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "engine")]
    sys.modules["sct_v3_ad"] = pkg
    mods = {}
    for m in ("bathymetry", "cable_system", "seeds", "solver3d", "hydrodynamics",
              "steady_lay", "timeline", "control", "scenarios", "quick_bu"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_ad.{m}", ROOT / "catenary" / "v3" / "engine" / f"{m}.py")
        mm = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_ad.{m}"] = mm
        spec.loader.exec_module(mm)
        mods[m] = mm
    return mods


M = _load_engine()
cs = M["cable_system"]
tl = M["timeline"]
qb = M["quick_bu"]
scen = M["scenarios"]
bath = M["bathymetry"]

TAIL_W = 300.0        # N/m — heavy BU tail
MAIN_W = 120.0        # N/m — main cable
JOINT_KN = 1.2        # kN — tail joint body


def bu_outward_assembly():
    """[BU tail 90 m][tail joint body][main cable, remainder] — as a user
    would enter it, reading outward from the BU."""
    return [
        cs.SegmentSpec(name="BU tail", length_m=90.0, q_water_npm=TAIL_W,
                       diameter_m=0.05, friction_mu=0.5),
        cs.BodySpec(name="tail joint", point_load_kN=JOINT_KN, cda_m2=0.4),
        cs.SegmentSpec(name="Main cable", q_water_npm=MAIN_W, diameter_m=0.021,
                       friction_mu=0.25, fill=True),
    ]


def _chain_state(name, length_m, datum):
    return tl.ChainState(
        name=name, assembly=bu_outward_assembly(), defaults=cs.Defaults(),
        length_m=length_m,
        top=tl.Attachment("free"), bottom=tl.Attachment("free"),
        shape=np.zeros((2, 3)), assembly_datum=datum,
    )


def _element_weights(state, n_elems=40):
    """Per-element submerged weight from the top node down, as the solver
    would map it for this chain state."""
    items, direction = state.oriented_assembly()
    mapper = cs.AssemblyMapper(items, state.defaults, direction,
                               span_m=state.length_m)
    ds = state.length_m / n_elems
    s_top = (np.arange(n_elems) + 0.5) * ds
    s_mid = (state.length_m - s_top) if direction == "from_bottom" else s_top
    return mapper.element_arrays(s_mid)["qw"], mapper, ds


# --- resolve_assembly ------------------------------------------------------

def test_resolve_assembly_expands_the_remainder_row():
    items, fit = cs.resolve_assembly(bu_outward_assembly(), 1200.0)
    lengths = [it.length_m for it in items if isinstance(it, cs.SegmentSpec)]
    assert lengths == [90.0, 1110.0]
    assert fit.exact and fit.n_fill == 1
    assert fit.fixed_m == 90.0 and fit.fill_m == 1110.0 and fit.total_m == 1200.0
    # Inputs are not mutated.
    assert bu_outward_assembly()[2].length_m == 0.0


def test_resolve_assembly_reports_a_short_assembly():
    fixed = [cs.SegmentSpec(name="tail", length_m=90.0, q_water_npm=TAIL_W),
             cs.SegmentSpec(name="main", length_m=400.0, q_water_npm=MAIN_W)]
    items, fit = cs.resolve_assembly(fixed, 1200.0)
    assert [it.length_m for it in items] == [90.0, 400.0]      # unchanged
    assert not fit.exact
    assert fit.short_by_m == 710.0 and fit.over_by_m == 0.0
    # ... and an assembly longer than the chain.
    _items, fit2 = cs.resolve_assembly(fixed, 300.0)
    assert fit2.over_by_m == 190.0 and fit2.short_by_m == 0.0
    # No span given: nothing to resolve against, no complaint.
    _items, fit3 = cs.resolve_assembly(bu_outward_assembly(), None)
    assert fit3.short_by_m == 0.0 and fit3.fill_m == 0.0


def test_fill_row_never_goes_negative():
    _items, fit = cs.resolve_assembly(bu_outward_assembly(), 50.0)
    assert fit.fill_m == 0.0 and fit.over_by_m == 40.0


# --- the datum -------------------------------------------------------------

def test_tail_stays_at_the_bu_for_any_leg_length():
    """A leg hangs from the BU at its top end: the 90 m tail must occupy the
    top 90 m whether the leg is 300 m or 3 km."""
    for length in (300.0, 1200.0, 3000.0):
        st = _chain_state("leg1", length, "top_end")
        qw, _mapper, ds = _element_weights(st)
        n_tail = int(round(90.0 / ds))
        assert np.allclose(qw[:n_tail], TAIL_W), length
        assert np.allclose(qw[n_tail + 1:], MAIN_W), length


def test_trunk_datum_is_its_bottom_end():
    """The trunk's BU is its bottom end, so the tail occupies the LAST 90 m
    and the remainder grows at the vessel end as it pays out."""
    for length in (150.0, 800.0, 2500.0):
        st = _chain_state("trunk", length, "bottom_end")
        qw, _mapper, ds = _element_weights(st)
        n_tail = int(round(90.0 / ds))
        assert np.allclose(qw[-n_tail:], TAIL_W), length
        assert np.allclose(qw[:-(n_tail + 1)], MAIN_W), length


def test_legacy_datum_pins_the_assembly_by_its_last_row():
    """Without a datum the old behaviour stands: the assembly's last row sits
    at the chain's bottom end, so an over-long chain stretches the first row
    (this is what the remainder row exists to avoid)."""
    st = _chain_state("leg1", 1200.0, None)      # mapper_direction default
    st.assembly = [it for it in bu_outward_assembly()
                   if not getattr(it, "fill", False)]
    st.assembly.append(cs.SegmentSpec(name="Main cable", length_m=400.0,
                                      q_water_npm=MAIN_W))
    qw, mapper, _ds = _element_weights(st)
    assert mapper.fit.short_by_m == 710.0
    assert qw[0] == TAIL_W and qw[-1] == MAIN_W
    # The tail is smeared far past 90 m — the trap the datum + fill removes.
    assert float(np.count_nonzero(qw == TAIL_W)) / len(qw) > 0.5


def test_invalid_datum_raises():
    st = _chain_state("leg1", 500.0, "middle")
    try:
        st.oriented_assembly()
    except ValueError as exc:
        assert "assembly_datum" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unknown datum")


# --- bodies ---------------------------------------------------------------

def test_tail_joint_body_lands_at_the_tail_length_from_the_bu():
    """The tail joint is an assembly body, so it carries load — and it must
    hang 90 m from the BU on both a leg (top datum) and the trunk."""
    for name, datum, expect_from_top in (("leg1", "top_end", True),
                                         ("trunk", "bottom_end", False)):
        st = _chain_state(name, 1200.0, datum)
        items, direction = st.oriented_assembly()
        mapper = cs.AssemblyMapper(items, st.defaults, direction,
                                   span_m=st.length_m)
        n = 60
        b = cs.SystemBuilder()
        shape = cs.straight_shape((0.0, 0.0, 0.0), (0.0, 0.0, -1200.0), n)
        chain = b.add_chain(name, mapper, st.length_m, n, shape)
        sysm = b.build()
        loaded = np.flatnonzero(np.abs(sysm.point_force_N[:, 2]) > 1.0)
        assert len(loaded) == 1, (name, loaded)
        node = int(loaded[0])
        assert math.isclose(sysm.point_force_N[node, 2], -JOINT_KN * 1000.0)
        k = int(np.flatnonzero(chain.idx == node)[0])   # index from the top
        s_from_top = k * st.length_m / n
        s_from_bu = s_from_top if expect_from_top else st.length_m - s_from_top
        assert abs(s_from_bu - 90.0) <= st.length_m / n, (name, s_from_bu)
        assert sysm.body_cda_m2[node] == 0.4


# --- clamped flag ---------------------------------------------------------

def test_chain_records_which_elements_were_clamped():
    st = _chain_state("leg1", 1200.0, None)
    st.assembly = [cs.SegmentSpec(name="tail", length_m=90.0, q_water_npm=TAIL_W),
                   cs.SegmentSpec(name="main", length_m=400.0, q_water_npm=MAIN_W)]
    items, direction = st.oriented_assembly()
    mapper = cs.AssemblyMapper(items, st.defaults, direction, span_m=st.length_m)
    n = 40
    b = cs.SystemBuilder()
    chain = b.add_chain("leg1", mapper, st.length_m, n,
                        cs.straight_shape((0, 0, 0), (0, 0, -1200.0), n))
    assert chain.clamped is not None
    assert chain.clamped.shape == (n,)
    assert chain.clamped.any()          # 710 m beyond the assembly


# --- quick model agreement ------------------------------------------------

def test_quick_means_see_the_resolved_remainder():
    """The quick model averages the same rows the full solver maps, so a
    90 m tail on a 1200 m leg barely shifts the mean (it must not average
    the fixed rows alone, nor miss the tail)."""
    st = _chain_state("leg1", 1200.0, "top_end")
    w = qb.mean_weight_npm(st)
    expect = (90.0 * TAIL_W + 1110.0 * MAIN_W) / 1200.0
    assert abs(w - expect) < 1e-6, (w, expect)
    mu = qb.mean_friction_mu(st)
    expect_mu = (90.0 * 0.5 + 1110.0 * 0.25) / 1200.0
    assert abs(mu - expect_mu) < 1e-6, (mu, expect_mu)
    # Orientation must not change a whole-chain mean.
    trunk = _chain_state("trunk", 1200.0, "bottom_end")
    assert abs(qb.mean_weight_npm(trunk) - expect) < 1e-6


def test_deployed_items_are_ordered_from_the_bottom_end():
    leg = qb.deployed_items(_chain_state("leg1", 1200.0, "top_end"))
    assert [getattr(it, "name", "") for it in leg] == [
        "Main cable", "tail joint", "BU tail"]
    trunk = qb.deployed_items(_chain_state("trunk", 1200.0, "bottom_end"))
    assert [getattr(it, "name", "") for it in trunk] == [
        "BU tail", "tail joint", "Main cable"]


# --- scenario wiring ------------------------------------------------------

def test_bu_deployment_wires_the_bu_datum_and_warns_when_short():
    bathy = bath.FlatBathymetry(depth_m=120.0)
    sc = scen.bu_deployment(
        bathy, bu_outward_assembly(), bu_outward_assembly(), cs.Defaults(),
        bu_weight_kN=15.0, leg_length_m=600.0, target_ds_m=10.0,
    )
    assert sc.chains["leg1"].assembly_datum == "top_end"
    assert sc.chains["leg2"].assembly_datum == "top_end"
    assert sc.chains["trunk"].assembly_datum == "bottom_end"

    # A fixed-length assembly shorter than the leg must be reported, not
    # silently stretched.
    short = [cs.SegmentSpec(name="BU tail", length_m=90.0, q_water_npm=TAIL_W),
             cs.SegmentSpec(name="Main cable", length_m=200.0, q_water_npm=MAIN_W)]
    sc2 = scen.bu_deployment(
        bathy, bu_outward_assembly(), short, cs.Defaults(),
        bu_weight_kN=15.0, leg_length_m=600.0, target_ds_m=10.0,
        static_only=True,
    )
    sim = tl.OperationSimulator(sc2, bathy, tl.SimOptions.preview())
    sim._build()
    msgs = " ".join(sim.assembly_warnings)
    assert "leg1" in msgs and "leg2" in msgs and "310 m" in msgs, msgs
    assert "trunk" not in msgs, msgs      # trunk has a remainder row


def test_quick_model_also_warns_about_a_short_assembly():
    """The quick backend never builds a mapper, so it needs its own check —
    otherwise the fastest path is the silent one."""
    bathy = bath.FlatBathymetry(depth_m=120.0)
    short = [cs.SegmentSpec(name="BU tail", length_m=90.0, q_water_npm=TAIL_W),
             cs.SegmentSpec(name="Main cable", length_m=200.0, q_water_npm=MAIN_W)]
    sc = scen.bu_deployment(
        bathy, bu_outward_assembly(), short, cs.Defaults(),
        bu_weight_kN=15.0, leg_length_m=600.0, target_ds_m=10.0,
        static_only=True,
    )
    sim = qb.QuickOperationSimulator(sc, bathy)
    snap = sim.settle()
    msgs = " ".join(snap.warnings)
    assert "leg1" in msgs and "310 m" in msgs, msgs


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
