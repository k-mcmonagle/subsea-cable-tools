# -*- coding: utf-8 -*-
"""Validation of the steady-lay ODE model (Catenary Calculator V2, Steady Lay
tab) against Zajac 1957 closed forms and the closed-form catenary (V = 0 limit).

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
    """Load catenary/hydrodynamics.py and catenary/steady_lay.py without
    importing the plugin package (steady_lay imports ``.hydrodynamics``)."""
    pkg = types.ModuleType("sct_steady_lay_pkg")
    pkg.__path__ = [str(ROOT / "catenary")]
    sys.modules[pkg.__name__] = pkg
    mods = []
    for m in ("hydrodynamics", "steady_lay"):
        name = f"{pkg.__name__}.{m}"
        spec = importlib.util.spec_from_file_location(name, ROOT / "catenary" / f"{m}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        mods.append(mod)
    return mods


hyd, sl = _load_engine()

KNOT = 0.514444


def _assert_close(value, expect, rel, label):
    err = abs(value - expect) / max(abs(expect), 1e-12)
    assert err <= rel, f"{label}: {value:.5g} vs {expect:.5g} (rel err {err:.3%} > {rel:.1%})"


def _telecom_input(**over):
    """A telecom-lightweight-like cable: 31 mm OD, ~0.5 kg/m in water."""
    kw = dict(
        depth_m=2000.0,
        q_water_npm=5.0,
        diameter_m=0.031,
        cd_normal=1.2,
        cd_tangential=0.01,
        rho_c_kgpm=1.0,
        ship_speed_mps=6.0 * KNOT,
        payout_speed_mps=6.0 * KNOT * 1.02,
        chute_height_m=0.0,
    )
    kw.update(over)
    return sl.SteadyLayInput(**kw)


# ---------------------------------------------------------------------------

def test_catenary_limit_at_zero_speed():
    """V = 0, no drag: the ODE must reproduce the closed-form catenary."""
    q, h, H = 225.6, 93.0, 11772.0  # ~JMSE fixture: 23 kg/m, 93 m, 1200 kgf
    inp = sl.SteadyLayInput(
        depth_m=h, q_water_npm=q, diameter_m=0.144, cd_normal=1.2,
        ship_speed_mps=0.0, payout_speed_mps=0.0, T0_N=H,
    )
    res = sl.integrate_steady_lay(inp)
    a = H / q
    layback = a * math.acosh(1.0 + h / a)
    s_len = a * math.sinh(layback / a)
    T_top = H + q * h
    _assert_close(res.layback_m, layback, 0.005, "layback")
    _assert_close(res.suspended_length_m, s_len, 0.005, "suspended length")
    _assert_close(res.top_tension_N, T_top, 0.005, "top tension")
    # Min radius at TDP = a for the pure catenary.
    _assert_close(res.min_radius_m, a, 0.03, "TDP bend radius")
    # Exit angle: tan(theta) = q*s/H.
    theta = math.degrees(math.atan(q * s_len / H))
    _assert_close(res.exit_angle_deg, theta, 0.01, "exit angle")


def test_zero_bottom_tension_gives_straight_line_at_critical_angle():
    """T0 -> 0 with ship speed: Zajac's straight line at the critical angle."""
    inp = _telecom_input(T0_N=0.0, payout_speed_mps=None)
    res = sl.integrate_steady_lay(inp)
    H_c = hyd.hydrodynamic_constant(inp.q_water_npm, inp.diameter_m, inp.cd_normal)
    alpha = math.degrees(hyd.critical_angle_rad(H_c, inp.ship_speed_mps))
    # The configuration should be nearly straight: exit angle ~ alpha and the
    # chord length ~ suspended length.
    _assert_close(res.exit_angle_deg, alpha, 0.03, "exit angle vs critical angle")
    chord = float(np.linalg.norm(res.xyz[-1] - res.xyz[0]))
    _assert_close(res.suspended_length_m, chord, 0.005, "straightness (arc vs chord)")
    # Layback ~ h / tan(alpha).
    _assert_close(res.layback_m, inp.depth_m / math.tan(math.radians(alpha)), 0.04, "layback")


def test_top_tension_theorem_with_drag():
    """T_s = T0 + w*h regardless of normal drag (Cd_t = 0). Zajac eq. 21."""
    for T0 in (500.0, 5000.0, 20000.0):
        inp = _telecom_input(T0_N=T0, cd_tangential=0.0, rho_c_kgpm=0.0)
        res = sl.integrate_steady_lay(inp)
        expect = T0 + inp.q_water_npm * inp.depth_m
        _assert_close(res.top_tension_N, expect, 0.005, f"T_s at T0={T0}")


def test_critical_angle_closed_form_values():
    """Zajac Table-II-style spot checks of H and alpha."""
    # Cable No. 1: d = 0.75 in, w = 0.243 lb/ft -> SI.
    d = 0.75 * 0.0254
    w = 0.243 * 4.4482216 / 0.3048
    H = hyd.hydrodynamic_constant(w, d, cd_normal=1.0, rho=1000.0)
    # Zajac: H ~ 67-70 degree-knots -> radians*m/s: 68 deg.kn ~ 0.611 m/s.
    H_deg_knots = math.degrees(H / (6.0 * KNOT)) * 6.0  # alpha0*V in deg-kn
    assert 55.0 < H_deg_knots < 80.0, f"H = {H_deg_knots:.1f} degree-knots (expected ~64-70)"
    # Small-angle consistency: alpha ~ H/V below 20 deg.
    V = 4.0  # m/s
    a_exact = hyd.critical_angle_rad(H, V)
    a_small = H / V
    assert abs(a_exact - a_small) / a_small < 0.02


def test_solve_modes_roundtrip():
    """Solving for a derived quantity must return the T0 that produces it."""
    base = sl.solve_steady_lay(_telecom_input(), "bottom_tension", 8000.0)
    # Well-conditioned modes: T0 must round-trip.
    for mode, target in (
        ("top_tension", base.top_tension_N),
        ("layback", base.layback_m),
        ("suspended_length", base.suspended_length_m),
    ):
        res = sl.solve_steady_lay(_telecom_input(), mode, target)
        _assert_close(res.T0_N, base.T0_N, 0.02, f"T0 roundtrip via {mode}")
    # Exit angle is nearly flat vs T0 for a fast lay of light cable (the
    # shape stays near the critical angle), so only require the achieved
    # angle to match the target — T0 itself is ill-conditioned there.
    res = sl.solve_steady_lay(_telecom_input(), "exit_angle", base.exit_angle_deg)
    _assert_close(res.exit_angle_deg, base.exit_angle_deg, 0.005, "exit angle achieved")


def test_cross_current_produces_lateral_offset():
    """A surface cross-current stratum deflects the exit point laterally
    (Zajac Sec. 7.1); deeper no-current water keeps the touchdown plane."""
    cur = hyd.CurrentProfile([
        hyd.CurrentLayer(0.0, 0.5, 90.0),
        hyd.CurrentLayer(180.0, 0.5, 90.0),
        hyd.CurrentLayer(200.0, 0.0, 90.0),
    ])
    inp = _telecom_input(depth_m=1800.0, current=cur, T0_N=2000.0)
    res = sl.integrate_steady_lay(inp)
    no_cur = sl.integrate_steady_lay(_telecom_input(depth_m=1800.0, T0_N=2000.0))
    assert abs(no_cur.lateral_offset_m) < 1.0
    assert abs(res.lateral_offset_m) > 5.0, (
        f"expected a clear lateral offset, got {res.lateral_offset_m:.2f} m"
    )
    # The cable lands DOWNSTREAM of the ship track (Zajac Sec. 7.1): with
    # the exit measured relative to the TDP, the offset is opposite the
    # current (+y current -> negative exit-minus-TDP y).
    assert res.lateral_offset_m < 0


def test_payout_helpers():
    H = 0.7  # m/s
    dv = hyd.payout_increment_mps(H, math.radians(10.0))
    _assert_close(dv, 0.5 * H * math.radians(10.0), 1e-9, "payout increment")
    vlim = hyd.suspension_speed_limit_mps(H, math.radians(35.0))
    _assert_close(vlim, H / math.radians(35.0), 1e-9, "suspension speed limit")
    assert hyd.suspension_speed_limit_mps(H, 0.0) == float("inf")


def test_air_weight_adds_displaced_water():
    """Weight in air = submerged weight + weight of the displaced water."""
    d = 0.040
    expect = 15.0 + 1025.0 * hyd.G * math.pi * d * d / 4.0
    _assert_close(sl.air_weight_npm(15.0, d), expect, 1e-12, "air weight")
    assert sl.air_weight_npm(15.0, 0.0) == 15.0


def test_speed_sweep_slack_lay_follows_critical_angle():
    """Zero bottom tension: every speed lands on Zajac's critical angle, the
    angle falls and the layback grows with speed, and the input is untouched."""
    inp = _telecom_input(T0_N=0.0, payout_speed_mps=None, ds_m=2000.0 / 150.0)
    speeds = [1.0 * KNOT, 3.0 * KNOT, 6.0 * KNOT]
    sweep = sl.speed_sweep(inp, speeds)
    for a, c in zip(sweep.angle_deg, sweep.critical_angle_deg):
        _assert_close(a, c, 0.03, "sweep angle vs critical angle")
    assert np.all(np.diff(sweep.angle_deg) < 0)
    assert np.all(np.diff(sweep.layback_m) > 0)
    assert inp.ship_speed_mps == 6.0 * KNOT and inp.payout_speed_mps is None


def test_speed_sweep_matches_single_solves():
    """With bottom tension the sweep is the single solve at each speed."""
    inp = _telecom_input(depth_m=150.0, q_water_npm=200.0, diameter_m=0.06,
                         T0_N=5000.0, payout_speed_mps=None)
    sweep = sl.speed_sweep(inp, [0.5 * KNOT, 2.0 * KNOT])
    for v, a, t in zip(sweep.speed_mps, sweep.angle_deg, sweep.top_tension_N):
        one = sl.integrate_steady_lay(sl.SteadyLayInput(**{**inp.__dict__, "ship_speed_mps": v}))
        _assert_close(a, one.exit_angle_deg, 1e-9, "sweep angle")
        _assert_close(t, one.top_tension_N, 1e-9, "sweep top tension")


# ---------------------------------------------------------------------------

def _result(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run_all():
    failures = []
    tests = [
        test_catenary_limit_at_zero_speed,
        test_zero_bottom_tension_gives_straight_line_at_critical_angle,
        test_top_tension_theorem_with_drag,
        test_critical_angle_closed_form_values,
        test_solve_modes_roundtrip,
        test_cross_current_produces_lateral_offset,
        test_payout_helpers,
        test_air_weight_adds_displaced_water,
        test_speed_sweep_slack_lay_follows_critical_angle,
        test_speed_sweep_matches_single_solves,
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
