"""Validation checks for the multi-span drape solver.

Pure Python + NumPy; no QGIS imports. These tests validate the lumped-node
dynamic-relaxation solver against closed-form catenary results and physical
invariants (no bed penetration, tension-only members, friction bounds).
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
from typing import Callable, List


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cs = _load("subsea_catenary_solver_for_drape", "catenary/catenary_solver.py")
dr = _load("subsea_drape_solver", "catenary/drape_solver.py")

import numpy as np  # noqa: E402  (after module load; QGIS python provides it)

FlatSeabed = cs.FlatSeabed
PolylineSeabed = cs.PolylineSeabed
solve_drape = dr.solve_drape


def _assert_close(name: str, actual: float, expected: float, tolerance: float):
    assert abs(actual - expected) <= tolerance, (
        f"{name}: actual={actual:.6g} expected={expected:.6g} "
        f"diff={abs(actual - expected):.3g} tol={tolerance:.3g}"
    )


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def test_flat_bed_matches_closed_form_catenary():
    """Taut flat-bed drape must reproduce the analytic single-span catenary:
    top tension, departure angle, touchdown position and the frictionless
    transfer of H along the bed to the anchor."""
    q, D, H = 22.0, 100.0, 50_000.0
    a = H / q
    x_an = a * math.acosh(1.0 + D / a)
    s_an = a * math.sinh(x_an / a)
    T_top_an = math.sqrt(H * H + (q * s_an) ** 2)

    res = solve_drape(
        FlatSeabed(D),
        top_xy=(0.0, 0.0),
        cable_length_m=s_an + 300.0,
        q_water_npm=q,
        mu=0.0,
        bottom_anchor_xy=(x_an + 300.0, -D),
        n_nodes=400,
        tension_scale_N=T_top_an,
    )
    assert res.converged, f"residual_ratio={res.residual_ratio:.2e}"
    _assert_close("top tension", res.top_tension_kN, T_top_an / 1000.0, 0.01 * T_top_an / 1000.0)
    theta_an = math.degrees(math.acos(H / T_top_an))
    _assert_close("top angle", res.top_angle_deg, theta_an, 0.5)
    first_contact_x = float(res.x[np.argmax(res.contact)])
    _assert_close("touchdown x", first_contact_x, x_an, 0.025 * x_an)
    # Frictionless flat bed transfers H unchanged to the anchor.
    _assert_close("anchor tension == H", res.end_tension_kN, H / 1000.0, 0.02 * H / 1000.0)
    assert res.max_penetration_m < 0.02


def test_ridge_produces_multiple_spans_without_penetration():
    """Cable draped over a ridge crest must contact the crest, form at least
    two suspended regions, and never penetrate the bed."""
    xs = [0.0, 100.0, 250.0, 400.0, 700.0, 2000.0]
    dep = [120.0, 120.0, 40.0, 120.0, 150.0, 300.0]
    seabed = PolylineSeabed(xs, dep, slope_smoothing_m=5.0)
    res = solve_drape(
        seabed,
        top_xy=(0.0, 0.0),
        cable_length_m=900.0,
        q_water_npm=22.0,
        mu=0.0,
        bottom_anchor_xy=(820.0, -float(seabed.depth_at(820.0))),
        n_nodes=400,
        tension_scale_N=60_000.0,
    )
    assert res.converged
    assert res.max_penetration_m < 0.05, f"penetration {res.max_penetration_m:.3f} m"
    assert len(res.spans) >= 2, f"expected multi-span, got {len(res.spans)}"
    # The crest (x ~ 250, depth 40 m) must be a contact region.
    crest_nodes = (np.abs(res.x - 250.0) < 30.0)
    assert np.any(res.contact & crest_nodes), "expected contact on the ridge crest"
    assert np.all(res.tension_kN >= -1e-9)
    # Tension must be continuous along the chain (no jumps > a few nodal weights).
    dT = np.abs(np.diff(res.tension_kN)) * 1000.0
    assert float(np.max(dT)) < 10.0 * 22.0 * (900.0 / 400.0), "tension discontinuity"


def test_friction_reduces_anchor_tension():
    """With Coulomb friction the bed absorbs tension; the anchor-end tension
    must not exceed the frictionless value (and typically decays to ~0 in a
    slack lay)."""
    xs = [0.0, 100.0, 250.0, 400.0, 700.0, 2000.0]
    dep = [120.0, 120.0, 40.0, 120.0, 150.0, 300.0]
    seabed = PolylineSeabed(xs, dep, slope_smoothing_m=5.0)
    common = dict(
        top_xy=(0.0, 0.0),
        cable_length_m=900.0,
        q_water_npm=22.0,
        bottom_anchor_xy=(820.0, -float(seabed.depth_at(820.0))),
        n_nodes=300,
        tension_scale_N=60_000.0,
    )
    res0 = solve_drape(seabed, mu=0.0, **common)
    res5 = solve_drape(seabed, mu=0.5, **common)
    assert res0.converged and res5.converged
    assert res5.end_tension_kN <= res0.end_tension_kN + 0.1, (
        f"friction increased anchor tension: {res5.end_tension_kN:.2f} vs {res0.end_tension_kN:.2f}"
    )
    assert any("non-unique" in w for w in res5.warnings)


def test_free_end_frictionless_rejected():
    try:
        solve_drape(
            FlatSeabed(100.0),
            top_xy=(0.0, 0.0),
            cable_length_m=500.0,
            q_water_npm=22.0,
            mu=0.0,
            bottom_anchor_xy=None,
        )
    except ValueError as exc:
        assert "equilibrium" in str(exc)
        return
    raise AssertionError("free end + mu=0 must be rejected")


def test_anchor_beyond_cable_length_rejected():
    try:
        solve_drape(
            FlatSeabed(100.0),
            top_xy=(0.0, 0.0),
            cable_length_m=300.0,
            q_water_npm=22.0,
            mu=0.0,
            bottom_anchor_xy=(500.0, -100.0),
        )
    except ValueError as exc:
        assert "shorter" in str(exc)
        return
    raise AssertionError("unreachable anchor must be rejected")


def test_query_helpers():
    """tension_at_s interpolates and radius_at_s is ~a = H/q at the touchdown
    region of a taut flat-bed catenary."""
    q, D, H = 22.0, 100.0, 50_000.0
    a = H / q
    x_an = a * math.acosh(1.0 + D / a)
    s_an = a * math.sinh(x_an / a)
    res = solve_drape(
        FlatSeabed(D),
        top_xy=(0.0, 0.0),
        cable_length_m=s_an + 200.0,
        q_water_npm=q,
        mu=0.0,
        bottom_anchor_xy=(x_an + 200.0, -D),
        n_nodes=400,
        tension_scale_N=60_000.0,
    )
    assert res.converged
    t_top = res.tension_at_s(0.0)
    _assert_close("query top tension", t_top, res.top_tension_kN, 1e-6)
    # Catenary curvature radius at the TDP is a = H/q; query slightly above it.
    s_query = s_an * 0.95
    r = res.radius_at_s(s_query)
    assert 0.5 * a < r < 2.0 * a, f"radius at near-TDP {r:.0f} m vs a={a:.0f} m"


def run_all() -> List[str]:
    failures: List[str] = []
    tests: List[Callable[[], None]] = [
        test_flat_bed_matches_closed_form_catenary,
        test_ridge_produces_multiple_spans_without_penetration,
        test_friction_reduces_anchor_tension,
        test_free_end_frictionless_rejected,
        test_anchor_beyond_cable_length_rejected,
        test_query_helpers,
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
    run_all()
