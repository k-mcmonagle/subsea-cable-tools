"""Pure-Python validation checks for the legacy (V1) closed-form catenary core.

Runs without QGIS. NumPy is optional (shape/radius checks are skipped without it).
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
from typing import Callable, List


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "catenary" / "simple_catenary.py"

spec = importlib.util.spec_from_file_location("subsea_simple_catenary", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

CatenaryCalculator = module.CatenaryCalculator


def _assert_close(name: str, actual: float, expected: float, tolerance: float):
    assert abs(actual - expected) <= tolerance, (
        f"{name}: actual={actual:.12g} expected={expected:.12g} "
        f"diff={abs(actual - expected):.12g} tolerance={tolerance:.12g}"
    )


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" - {detail}"
    print(msg)
    return ok


def _solve(param: str, q: float = 22.0, h: float = 100.0, **inputs) -> CatenaryCalculator:
    cfg = {"weightInWater": q, "waterDepth": h, "inputParameter": param}
    cfg.update(inputs)
    calc = CatenaryCalculator(cfg)
    calc.calculate()
    return calc


def test_bottom_tension_closed_form_identities():
    """All outputs must satisfy the exact uniform-catenary identities."""
    q, h, H = 22.0, 100.0, 50_000.0
    calc = _solve("Bottom Tension", q=q, h=h, bottomTension=H / 1000.0)
    a = H / q
    x_expected = a * math.acosh(1.0 + h / a)
    s_expected = a * math.sinh(x_expected / a)
    _assert_close("layback", calc.xDeck, x_expected, 1e-9)
    _assert_close("length", calc.catenaryLength, s_expected, 1e-9)
    # s^2 = h^2 + 2*a*h (exact identity)
    _assert_close("length identity", calc.catenaryLength ** 2, h * h + 2 * a * h, 1e-6)
    # T_top = H + q*h (exact identity)
    _assert_close("top tension identity", calc.topTension * 1000.0, H + q * h, 1e-6)


def test_all_modes_round_trip():
    """Each input mode must reproduce the same configuration."""
    base = _solve("Bottom Tension", bottomTension=50.0)
    cases = [
        ("Top Tension", {"topTension": base.topTension}),
        ("Exit Angle", {"exitAngle": base.exitAngle}),
        ("Catenary Length", {"catenaryLength": base.catenaryLength}),
        ("Layback", {"layback": base.xDeck}),
    ]
    for mode, extra in cases:
        calc = _solve(mode, **extra)
        _assert_close(f"{mode} -> bottom tension", calc.bottomTension, base.bottomTension, 0.01)
        _assert_close(f"{mode} -> layback", calc.xDeck, base.xDeck, 0.05)
        _assert_close(f"{mode} -> length", calc.catenaryLength, base.catenaryLength, 0.05)


def test_catenary_length_mode_works_beyond_2_41x_depth():
    """Regression: S > (1+sqrt(2))*h used to fail with a bracketing error.
    For S=300 m at h=100 m the exact solution is a = (S^2-h^2)/(2h) = 400 m."""
    q, h, S = 22.0, 100.0, 300.0
    calc = _solve("Catenary Length", q=q, h=h, catenaryLength=S)
    a_expected = (S * S - h * h) / (2.0 * h)
    _assert_close("H from closed form", calc.bottomTension * 1000.0, q * a_expected, 1e-6)
    _assert_close("length round trip", calc.catenaryLength, S, 1e-6)


def test_layback_mode_works_beyond_14x_depth():
    """Regression: layback > ~14x depth used to fail with a bracketing error
    (realistic shallow-water case: 350 m layback in 20 m depth)."""
    calc = _solve("Layback", h=20.0, layback=350.0)
    _assert_close("layback round trip", calc.xDeck, 350.0, 0.01)
    # And a deep-ratio sanity case.
    calc2 = _solve("Layback", h=10.0, layback=500.0)
    _assert_close("extreme layback round trip", calc2.xDeck, 500.0, 0.01)


def test_top_tension_below_vertical_weight_is_rejected_clearly():
    """T_top <= q*h is physically unachievable; the error must say why."""
    q, h = 22.0, 100.0
    try:
        _solve("Top Tension", q=q, h=h, topTension=(q * h) / 1000.0 * 0.5)
    except ValueError as exc:
        assert "submerged weight" in str(exc), f"unhelpful message: {exc}"
        return
    raise AssertionError("Expected infeasible top tension to be rejected")


def test_length_not_exceeding_depth_rejected():
    try:
        _solve("Catenary Length", h=100.0, catenaryLength=90.0)
    except ValueError:
        return
    raise AssertionError("Expected length <= depth to be rejected")


def test_shape_starts_at_tdp_and_reaches_surface():
    if module.np is None:
        print("  (skipped shape check: numpy unavailable)")
        return
    calc = _solve("Bottom Tension", bottomTension=50.0)
    x, y = calc.get_catenary_shape()
    _assert_close("shape starts at x=0", float(x[0]), 0.0, 1e-12)
    _assert_close("shape starts at y=0", float(y[0]), 0.0, 1e-12)
    _assert_close("shape ends at layback", float(x[-1]), calc.xDeck, 1e-9)
    _assert_close("shape ends at depth", float(y[-1]), 100.0, 1e-6)
    # Minimum radius of the catenary is a = H/q at the TDP.
    r_min = calc.calculate_minimum_radius(x, y)
    _assert_close("min radius ~= a", r_min, 50_000.0 / 22.0, 50.0)


def run_all() -> List[str]:
    failures: List[str] = []
    tests: List[Callable[[], None]] = [
        test_bottom_tension_closed_form_identities,
        test_all_modes_round_trip,
        test_catenary_length_mode_works_beyond_2_41x_depth,
        test_layback_mode_works_beyond_14x_depth,
        test_top_tension_below_vertical_weight_is_rejected_clearly,
        test_length_not_exceeding_depth_rejected,
        test_shape_starts_at_tdp_and_reaches_surface,
    ]
    for test in tests:
        try:
            test()
            _result(test.__name__, True)
        except Exception as exc:  # pragma: no cover - manual runner support
            _result(test.__name__, False, repr(exc))
            failures.append(test.__name__)
    print(f"\n{len(failures)} failure(s)." if failures else "\nAll checks passed.")
    return failures


if __name__ == "__main__":  # pragma: no cover
    run_all()
