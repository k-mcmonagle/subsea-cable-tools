# -*- coding: utf-8 -*-
"""UI wiring for the BU integration: editor round-trip and the config ->
scenario path of the lowering scenario.

Runs headless on plain PyQt5 (offscreen platform); skipped cleanly when Qt
is not importable. The heavy dialog itself is not instantiated — the editor
widget and the solve_controller build path are exercised directly, which is
where all the datum conversions live.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt5.QtWidgets import QApplication
    HAVE_QT = True
except Exception:
    HAVE_QT = False

if HAVE_QT:
    APP = QApplication.instance() or QApplication([])
    from catenary.v3.engine import bathymetry as bath
    from catenary.v3.engine import bu_integration as bi
    from catenary.v3.engine import cable_system as cs
    from catenary.v3.engine.quick_bu import QuickOperationSimulator
    from catenary.v3.ui import solve_controller as sc
    from catenary.v3.ui.integration_editor import (
        BUIntegrationEditor, KIND_CABLE, KIND_JOINT, KIND_REST, COL_KIND,
        COL_LEN, COL_NAME,
    )


def sample_dict() -> dict:
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
    integ.leg1.count_increases_from_bu = False
    return integ.to_dict()


def test_editor_round_trips_the_integration():
    ed = BUIntegrationEditor()
    ed.set_from_dict(sample_dict())
    out = bi.BUIntegration.from_dict(ed.to_dict(bu_weight_kN=18.0, bu_cda_m2=1.2))
    assert out.problems() == []
    assert out.leg1.fixed_length_m() == 90.0
    assert out.leg2.fixed_length_m() == 150.0
    assert ("leg1 joint", 90.0) in out.leg1.joints_from_bu()
    assert out.leg1.count_at_bu_m == 10000.0
    assert out.leg1.count_increases_from_bu is False
    assert out.leg2.count_at_bu_m is None
    # Weight survives the table (leg 2 tail = 400 N/m).
    tail2 = out.leg2.segments()[0]
    assert tail2.q_water_npm == 400.0 and tail2.length_m == 150.0


def test_editor_keeps_the_rest_row_last_and_positions_live():
    ed = BUIntegrationEditor()
    ed.set_from_dict({})
    page = ed.pages["leg1"]
    page._add_cable()          # inserted BEFORE the rest-of-line row
    kinds = [page.table.item(r, COL_KIND).text()
             for r in range(page.table.rowCount())]
    assert kinds[-1] == KIND_REST and kinds.count(KIND_REST) == 1
    assert KIND_CABLE in kinds and KIND_JOINT in kinds
    # Live from-BU positions: the default 90 m tail puts the joint at 90.
    page.refresh_positions()
    joint_row = kinds.index(KIND_JOINT)
    from catenary.v3.ui.integration_editor import COL_FROM_BU
    assert page.table.item(joint_row, COL_FROM_BU).text() == "at 90"
    # And the makeup is still valid.
    assert bi.BranchMakeup.from_dict(page.to_makeup_dict()).problems("leg1") == []


def test_default_editor_is_a_valid_integration():
    ed = BUIntegrationEditor()
    assert ed.problems() == []


def _cfg_with_integration(**op_over) -> "sc.V3Config":
    cfg = sc.V3Config()
    cfg.mode = "operation"
    cfg.scenario = "bu_deployment"
    cfg.bathymetry = {"kind": "flat", "depth_m": 120.0}
    cfg.op = {
        "bu_weight_kN": 18.0,
        "bu_cda_m2": 1.2,
        "leg_length_m": 600.0,
        "leg_lengths_m": [600.0, 700.0],
        "leg1_azimuth_deg": 150.0,
        "leg2_azimuth_deg": 210.0,
        "integration": sample_dict(),
        "payout_mps": 0.4,
        "ship_speed_mps": 0.3,
    }
    cfg.op.update(op_over)
    return cfg


def test_build_scenario_uses_the_integration():
    cfg = _cfg_with_integration()
    bathy = sc.build_bathymetry(cfg)
    scn = sc._build_scenario(cfg, bathy, "bu_deployment", static_only=True)
    assert scn.chains["leg1"].length_m == 600.0
    assert scn.chains["leg2"].length_m == 700.0
    assert scn.chains["leg1"].assembly_datum == "top_end"
    assert scn.chains["trunk"].assembly_datum == "bottom_end"
    # Per-branch make-ups arrived intact (leg 2 tail is 150 m @ 400 N/m).
    tail2 = scn.chains["leg2"].assembly[0]
    assert isinstance(tail2, cs.SegmentSpec)
    assert tail2.length_m == 150.0 and tail2.q_water_npm == 400.0
    # Joints in per-chain coordinates; leg1 count direction preserved.
    assert ("leg1 joint", 600.0 - 90.0) in scn.chains["leg1"].joints
    assert scn.chains["leg1"].count_ref_m == 10000.0 - 600.0
    assert scn.chains["leg1"].count_to_top is True
    # ...and the quick model runs on it.
    sim = QuickOperationSimulator(scn, bathy)
    snap = sim.settle()
    assert snap.converged
    leg1 = snap.chain("leg1")
    assert math.isclose(leg1.count_top_m, 10000.0)


def test_build_scenario_honours_picked_laid_ends_even_in_static():
    ends = [[-180.0, -420.0], [230.0, -400.0]]
    cfg = _cfg_with_integration(leg_far_ends_xy=ends)
    bathy = sc.build_bathymetry(cfg)
    scn = sc._build_scenario(cfg, bathy, "bu_deployment", static_only=True)
    for name, end in (("leg1", ends[0]), ("leg2", ends[1])):
        got = scn.chains[name].bottom.xyz
        assert (round(got[0], 6), round(got[1], 6)) == (end[0], end[1]), (name, got)


def test_plan_for_lowering_uses_picked_ends_and_per_leg_makeup():
    ends = [[-150.0, -500.0], [150.0, -500.0]]
    cfg = _cfg_with_integration(leg_far_ends_xy=ends,
                                leg_bottom_tension_kN=3.0,
                                leg_lengths_m=[700.0, 700.0])
    bathy = sc.build_bathymetry(cfg)
    rows, anchors, facts, warnings = sc._plan_for_lowering(cfg, bathy)
    assert rows, warnings
    assert [list(a) for a in anchors] == ends       # positions govern
    assert "Planned landing (BU)" in facts


# ---------------------------------------------------------------------------

def run_all():
    if not HAVE_QT:
        print("[SKIP] PyQt not importable — UI wiring tests skipped.")
        return 0
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
