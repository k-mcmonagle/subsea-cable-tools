# -*- coding: utf-8 -*-
"""BU Lowering Tool: the cable-over-sheave wrap rendering and the focused
dialog's config plumbing.

The wrap tests are pure NumPy (scene.py has no Qt imports). The dialog and
run tests follow test_v3_integration_ui: headless PyQt5 (offscreen), skipped
cleanly when Qt is not importable.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from catenary.v3.ui.scene import wrap_cable_over_sheave  # noqa: E402  (no Qt)

try:
    from PyQt5.QtWidgets import QApplication
    HAVE_QT = True
except Exception:
    HAVE_QT = False

if HAVE_QT:
    APP = QApplication.instance() or QApplication([])
    from catenary.v3.engine import bu_integration as bi
    from catenary.v3.ui import solve_controller as sc


# ---------------------------------------------------------------------------
# wrap_cable_over_sheave (pure geometry)
# ---------------------------------------------------------------------------

def _straight_cable(angle_deg: float, n: int = 41, length: float = 200.0,
                    top=(0.0, 0.0, 5.0), toward=(-1.0, 0.0)):
    """A straight polyline leaving ``top`` at ``angle_deg`` below horizontal."""
    a = math.radians(angle_deg)
    d = np.array([toward[0] * math.cos(a), toward[1] * math.cos(a), -math.sin(a)])
    s = np.linspace(0.0, length, n)
    xyz = np.asarray(top, dtype=float)[None, :] + s[:, None] * d[None, :]
    tension = np.linspace(50.0, 5.0, n)
    contact = np.zeros(n, dtype=bool)
    contact[-5:] = True
    return xyz, s, tension, contact


def test_wrap_starts_at_anchor_and_stays_on_the_arc():
    r = 5.0
    xyz, s, t, c = _straight_cable(45.0)
    top = xyz[0].copy()
    w_xyz, w_s, w_t, w_c = wrap_cable_over_sheave(xyz, s, t, c, r, (-1.0, 0.0))
    assert np.allclose(w_xyz[0], top)                     # anchor unchanged
    centre = top - np.array([0.0, 0.0, r])
    # Arc points sit on the sheave circle; arc length matches r*alpha.
    n_arc = len(w_xyz) - (len(xyz) - np.searchsorted(s, r * math.radians(45.0),
                                                     side="right"))
    arc = w_xyz[:n_arc]
    assert np.allclose(np.linalg.norm(arc - centre[None, :], axis=1), r,
                       atol=1e-9)
    # Exit tangent of the arc points ~45 deg below horizontal.
    d = arc[-1] - arc[-2]
    ang = math.degrees(math.atan2(-d[2], np.hypot(d[0], d[1])))
    assert abs(ang - 45.0) < 6.0
    # Arc-lengths stay monotonic; tensions carried over.
    assert np.all(np.diff(w_s) > 0)
    assert w_t is not None and len(w_t) == len(w_xyz)


def test_wrap_leaves_laid_cable_and_the_far_end_alone():
    r = 5.0
    xyz, s, t, c = _straight_cable(60.0)
    w_xyz, w_s, w_t, w_c = wrap_cable_over_sheave(xyz, s, t, c, r, (-1.0, 0.0))
    # Contact (laid) nodes are never moved, so the touchdown stays honest.
    assert np.allclose(w_xyz[-5:], xyz[-5:])
    assert bool(w_c[-1]) and not bool(w_c[0])


def test_wrap_noop_cases():
    xyz, s, t, c = _straight_cable(45.0)
    out = wrap_cable_over_sheave(xyz, s, t, c, 0.0, (-1.0, 0.0))
    assert out[0] is xyz                                  # no radius
    xyz2, s2, t2, c2 = _straight_cable(0.5)
    out2 = wrap_cable_over_sheave(xyz2, s2, t2, c2, 5.0, (-1.0, 0.0))
    assert out2[0] is xyz2                                # ~horizontal departure


def test_wrap_short_cable_keeps_the_far_end_pinned():
    """A trunk shorter than the offset-blend length (the t=0 lowering
    state: ~15 m of tail from the sheave to the BU) must still END exactly
    on the BU. The old fixed-length linear taper moved the bottom node ~2 m
    off the junction, drawing the trunk and the legs visibly disjointed."""
    top = np.array([0.0, 0.0, 5.0])
    bu = np.array([0.0, 0.0, -10.3])
    n = 31
    xyz = top[None, :] + (bu - top)[None, :] * np.linspace(0.0, 1.0, n)[:, None]
    s = np.linspace(0.0, 15.3, n)
    t = np.linspace(60.0, 55.0, n)
    w_xyz, w_s, w_t, w_c = wrap_cable_over_sheave(xyz, s, t, None, 3.0, (-1.0, 0.0))
    assert len(w_xyz) > n or not np.allclose(w_xyz, xyz)   # wrap applied
    assert np.allclose(w_xyz[-1], bu, atol=1e-9)           # BU end pinned
    assert np.allclose(w_xyz[0], top)


def test_wrap_adds_no_kink_at_the_arc_exit():
    """The offset taper is a smoothstep with zero slope at the arc exit, so
    the cable leaves the arc along its own direction — the old linear taper
    tilted it by delta/blend radians there, rendering a kink in the trunk
    catenary just below the sheave."""
    r = 3.0
    angle = 80.0
    xyz, s, t, c = _straight_cable(angle, n=161, length=200.0)
    w_xyz, w_s, _t, _c = wrap_cable_over_sheave(xyz, s, t, c, r, (-1.0, 0.0))
    # Find the arc/cable junction: last vertex on the sheave circle.
    centre = xyz[0] - np.array([0.0, 0.0, r])
    on_arc = np.abs(np.linalg.norm(w_xyz - centre[None, :], axis=1) - r) < 1e-6
    i_exit = int(np.max(np.nonzero(on_arc)[0]))
    # Analytic exit tangent of the arc (wrap angle = departure angle).
    a = math.radians(angle)
    u = np.array([-1.0, 0.0, 0.0])                      # toward = (-1, 0)
    tangent = math.cos(a) * u + math.sin(a) * np.array([0.0, 0.0, -1.0])
    leave = w_xyz[i_exit + 1] - w_xyz[i_exit]
    cosang = float(np.dot(tangent, leave) / np.linalg.norm(leave))
    ang = math.degrees(math.acos(max(-1.0, min(1.0, cosang))))
    assert ang < 2.0, f"kink of {ang:.1f} deg at the arc exit"


def test_wrap_vertical_hang_uses_the_fallback_direction():
    top = np.array([0.0, 0.0, 5.0])
    n = 31
    zs = np.linspace(5.0, -95.0, n)
    xyz = np.column_stack([np.zeros(n), np.zeros(n), zs])
    s = np.linspace(0.0, 100.0, n)
    w_xyz, w_s, _t, _c = wrap_cable_over_sheave(xyz, s, None, None, 4.0, (-1.0, 0.0))
    assert np.allclose(w_xyz[0], top)
    # The wrap bulges toward the fallback (aft) direction: -x.
    assert float(w_xyz[1:6, 0].min()) < -1e-6


# ---------------------------------------------------------------------------
# End-to-end: the tool's config through the quick model (needs Qt)
# ---------------------------------------------------------------------------

def _tool_config(quality: str = "quick") -> "sc.V3Config":
    cfg = sc.V3Config()
    cfg.mode = "operation"
    cfg.scenario = "bu_deployment"
    cfg.bathymetry = {"kind": "flat", "depth_m": 100.0}
    cfg.current_layers = []
    cfg.assembly = []
    cfg.chute_height_m = 5.0
    cfg.chute_radius_m = 4.0
    cfg.lay_azimuth_deg = 0.0
    cfg.trunk_slack_pct = 2.0
    cfg.op = {
        "bu_weight_kN": 15.0,
        "bu_cda_m2": 0.0,
        "leg_length_m": 300.0,
        "leg_lengths_m": [300.0, 300.0],
        "leg1_azimuth_deg": 150.0,
        "leg2_azimuth_deg": 210.0,
        "leg_far_ends_xy": None,
        "payout_mps": 0.4,
        "ship_speed_mps": 0.3,
        "bu_start_depth_m": None,
        "duration_s": None,
        "bottom_tension_target_kN": 0.0,
        "leg_bottom_tension_kN": 3.0,
        "plan_from_tension": True,
        "integration": bi.default_integration().to_dict(),
        "quality": quality,
    }
    return cfg


def test_quick_run_lands_the_bu_and_wraps_the_trunk_over_the_sheave():
    if not HAVE_QT:
        return
    out = sc.run_operation(_tool_config("quick"))
    assert not out.error, out.error
    assert out.snapshots and len(out.snapshots) > 3
    last = out.snapshots[-1]
    bu = last.junction_xyz.get("BU")
    assert bu is not None and bu[2] <= -95.0              # landed near the bed
    # Mid-descent scene: the trunk leaves over the sheave arc, not from a
    # point — its first vertex is the anchor, the next ones on the r-circle.
    scene = out.scene_builder(len(out.snapshots) // 2)
    trunk = next(p for p in scene.cables if p.name == "trunk")
    snap = out.snapshots[len(out.snapshots) // 2]
    top = np.array([snap.vessel_xy[0], snap.vessel_xy[1], 5.0])
    assert np.allclose(trunk.xyz[0], top, atol=1e-6)
    centre = top - np.array([0.0, 0.0, 4.0])
    assert abs(float(np.linalg.norm(trunk.xyz[1] - centre)) - 4.0) < 1e-6
    # The legs hang from the BU, not the vessel — no wrap applied.
    leg1 = next(p for p in scene.cables if p.name == "leg1")
    assert float(np.linalg.norm(leg1.xyz[0] - top)) > 1.0


def test_bu_lowering_dialog_builds_a_lowering_only_config():
    """Full dialog construction needs the QGIS plot shim — covered by
    tests/test_qgis_compat_widgets.py in the QGIS smoke run; here it runs
    only when qgis is importable."""
    if not HAVE_QT:
        return
    try:
        import qgis  # noqa: F401
    except Exception:
        print("      (skipped: needs QGIS for the plot shim)")
        return
    from catenary.v3.ui.bu_lowering_dialog import BULoweringDialog

    dlg = BULoweringDialog(None, iface=None)
    try:
        cfg = dlg.build_config("quick")
        assert cfg.mode == "operation"
        assert cfg.scenario == "bu_deployment"
        assert cfg.op["quality"] == "quick"
        assert cfg.op["plan_from_tension"] is True
        assert "integration" in cfg.op and cfg.op["integration"]["trunk"]["items"]
        assert cfg.current_layers == []                   # no drag inputs
        assert cfg.chute_radius_m == float(dlg.sheave_radius.value())
        full = dlg.build_config("full")
        assert full.op["quality"] == "full"
        # Own settings scope — never the main simulator's.
        assert dlg.settings.applicationName() == "BULoweringTool"
    finally:
        dlg._save_settings = lambda: None                 # don't write registry
        dlg.close()


def run_all():
    fails = []
    for fn in (test_wrap_starts_at_anchor_and_stays_on_the_arc,
               test_wrap_leaves_laid_cable_and_the_far_end_alone,
               test_wrap_noop_cases,
               test_wrap_short_cable_keeps_the_far_end_pinned,
               test_wrap_adds_no_kink_at_the_arc_exit,
               test_wrap_vertical_hang_uses_the_fallback_direction,
               test_quick_run_lands_the_bu_and_wraps_the_trunk_over_the_sheave,
               test_bu_lowering_dialog_builds_a_lowering_only_config):
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as exc:
            print(f"[FAIL] {fn.__name__} - {exc!r}")
            fails.append(fn.__name__)
    return fails


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(1 if run_all() else 0)
