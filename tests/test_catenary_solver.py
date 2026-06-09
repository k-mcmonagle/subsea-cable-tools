"""Pure-Python validation checks for Catenary Calculator V2's solver.

These tests intentionally avoid QGIS imports so the numerical engine can be
validated outside the QGIS GUI runtime.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
from typing import Callable, List


ROOT = Path(__file__).resolve().parents[1]
SOLVER_PATH = ROOT / "catenary" / "catenary_solver.py"

spec = importlib.util.spec_from_file_location("subsea_catenary_solver", SOLVER_PATH)
solver_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = solver_module
spec.loader.exec_module(solver_module)

AssemblyItem = solver_module.AssemblyItem
CatenarySystemCalculator = solver_module.CatenarySystemCalculator
parse_components = solver_module.parse_components
FlatSeabed = solver_module.FlatSeabed
PlanarSlopeSeabed = solver_module.PlanarSlopeSeabed
PolylineSeabed = solver_module.PolylineSeabed


def _base_config(**overrides):
    cfg = {
        "water_depth_m": 100.0,
        "chute_exit_height_m": 0.0,
        "chute_radius_m": 0.0,
        "ds_m": 0.05,
        "max_integration_steps": 100000,
        "q_water_npm": 22.0,
        "q_air_npm": 22.0,
        "assembly": [],
        "components": [],
        "input_mode": "Bottom Tension",
        "H_input_N": 50_000.0,
        "H_guess_N": 50_000.0,
        "S_guess_m": 700.0,
    }
    cfg.update(overrides)
    return cfg


def _analytic_uniform_from_bottom_tension(H: float, q: float, vertical_height: float):
    a = H / q
    x = a * math.acosh(1.0 + vertical_height / a)
    s = a * math.sinh(x / a)
    vertical_force = q * s
    top_tension = math.sqrt(H * H + vertical_force * vertical_force)
    angle_deg = math.degrees(math.atan2(vertical_force, H))
    return x, s, top_tension, angle_deg


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


def test_unit_conversions():
    _assert_close("N/m conversion", CatenarySystemCalculator._unit_to_npm(12.3, "N/m"), 12.3, 1e-12)
    _assert_close("kg/m conversion", CatenarySystemCalculator._unit_to_npm(2.0, "kg/m"), 19.6133, 1e-9)
    _assert_close("lbf/ft conversion", CatenarySystemCalculator._unit_to_npm(1.0, "lbf/ft"), 14.593902936351707, 1e-12)


def test_bottom_tension_uniform_catenary_matches_closed_form():
    H = 50_000.0
    q = 22.0
    D = 100.0
    cfg = _base_config(water_depth_m=D, q_water_npm=q, q_air_npm=q, H_input_N=H, H_guess_N=H)
    calc = CatenarySystemCalculator(cfg)
    calc.solve()

    x_expected, s_expected, top_t_expected, angle_expected = _analytic_uniform_from_bottom_tension(H, q, D)

    _assert_close("layback", calc.layback, x_expected, 0.02)
    _assert_close("cable length", calc.S_total, s_expected, 0.02)
    _assert_close("top tension", calc.top_tension_kN, top_t_expected / 1000.0, 0.002)
    _assert_close("exit angle", calc.exit_angle_deg_from_h, angle_expected, 0.002)
    _assert_close("bottom tension", calc.bottom_tension_kN, H / 1000.0, 1e-9)
    assert calc.tension_kN is not None
    assert len(calc.tension_kN) == len(calc.s)
    _assert_close("stored top tension", calc.tension_kN[-1], calc.top_tension_kN, 1e-9)

    if calc.s_sea_surface is not None:
        _assert_close("sea surface crossing", calc.s_sea_surface, calc.S_total, 0.1)
    else:
        _assert_close("final height at sea surface", calc.y[-1], 0.0, 1e-4)


def test_diagnostics_report_residuals_and_refinement_delta():
    cfg = _base_config(ds_m=0.1)
    calc = CatenarySystemCalculator(cfg)
    calc.solve()

    diagnostics = calc.diagnostics
    assert diagnostics.input_mode == "Bottom Tension"
    assert diagnostics.integration_steps == len(calc.s) - 1
    assert diagnostics.refinement_position_delta_m is not None
    assert diagnostics.refinement_angle_delta_deg is not None
    assert diagnostics.refinement_top_tension_delta_kN is not None
    assert diagnostics.refinement_position_delta_m < 0.01
    _assert_close("boundary residual", diagnostics.boundary_residual_m, 0.0, 1e-3)
    _assert_close("bottom tension residual", diagnostics.input_residual, 0.0, 1e-12)
    assert any("fallback cable weights" in warning for warning in diagnostics.warnings)


def test_solve_modes_round_trip_from_bottom_tension_solution():
    base = CatenarySystemCalculator(_base_config())
    base.solve()

    cases = [
        ("Contact Tension", {"Ttop_input_N": base.top_tension_kN * 1000.0}),
        ("Tangent Angle", {"exit_angle_from_h_deg": base.exit_angle_deg_from_h}),
        ("Catenary Length", {"S_input_m": base.S_total}),
        ("Layback", {"layback_input_m": base.layback}),
    ]

    for mode, extra in cases:
        cfg = _base_config(input_mode=mode, H_guess_N=base.H_N, S_guess_m=base.S_total, **extra)
        calc = CatenarySystemCalculator(cfg)
        calc.solve()
        _assert_close(f"{mode} bottom tension", calc.bottom_tension_kN, base.bottom_tension_kN, 0.01)
        _assert_close(f"{mode} total length", calc.S_total, base.S_total, 0.05)
        _assert_close(f"{mode} layback", calc.layback, base.layback, 0.05)
        _assert_close(f"{mode} angle", calc.exit_angle_deg_from_h, base.exit_angle_deg_from_h, 0.005)


def test_chute_contact_geometry_matches_tangent_angle():
    radius = 12.0
    top_height = 4.0
    angle_deg = 30.0
    cfg = _base_config(
        water_depth_m=75.0,
        chute_exit_height_m=top_height,
        chute_radius_m=radius,
        input_mode="Tangent Angle",
        exit_angle_from_h_deg=angle_deg,
        S_guess_m=260.0,
    )
    calc = CatenarySystemCalculator(cfg)
    calc.solve()

    theta = math.radians(angle_deg)
    _assert_close("chute contact length", calc.chute_contact_len_m, radius * theta, 0.01)
    _assert_close("chute departure height", calc.y[-1], top_height - radius + radius * math.cos(theta), 0.01)
    _assert_close("layback includes chute x offset", calc.layback, calc.x[-1] + radius * math.sin(theta), 0.01)


def test_legacy_component_distributed_load_applies_only_inside_range():
    comps = parse_components("Heavy, 25, 50, 10, 10, 0")
    cfg = _base_config(water_depth_m=20.0, q_water_npm=10.0, q_air_npm=10.0, components=comps)
    calc = CatenarySystemCalculator(cfg)
    _, _, vertical_force, _, _, _, _, _ = calc.integrate(H_N=10_000.0, S_free_m=100.0, ds=0.5)
    _assert_close("component range vertical force", vertical_force, 1_500.0, 1e-9)


def test_body_point_load_changes_vertical_force_in_direct_integration():
    assembly = [
        AssemblyItem("segment", "Upper", 50.0, 10.0, 10.0, 0.0),
        AssemblyItem("body", "Repeater", 0.0, 0.0, 0.0, 5.0),
        AssemblyItem("segment", "Lower", 1000.0, 10.0, 10.0, 0.0),
    ]
    cfg = _base_config(
        water_depth_m=20.0,
        q_water_npm=10.0,
        q_air_npm=10.0,
        assembly=assembly,
    )
    calc = CatenarySystemCalculator(cfg)
    _, _, vertical_force, _, _, _, _, _ = calc.integrate(H_N=10_000.0, S_free_m=100.0, ds=0.5)
    _assert_close("vertical force with body", vertical_force, 6_000.0, 1e-9)


def test_invalid_bottom_tension_is_rejected():
    cfg = _base_config(H_input_N=0.0)
    calc = CatenarySystemCalculator(cfg)
    try:
        calc.solve()
    except ValueError as exc:
        assert "Bottom tension" in str(exc)
        return
    raise AssertionError("Expected zero bottom tension to be rejected")


def test_diagnostics_warn_for_point_load_kink():
    comps = parse_components("Body, 10, 0, 0, 0, 2")
    cfg = _base_config(
        water_depth_m=20.0,
        H_input_N=10_000.0,
        H_guess_N=10_000.0,
        S_guess_m=80.0,
        components=comps,
    )
    calc = CatenarySystemCalculator(cfg)
    calc.solve()
    assert any("Point loads create" in warning for warning in calc.diagnostics.warnings)


def test_parse_components_sorts_and_skips_malformed_lines():
    comps = parse_components(
        """
        # name, s, length, dq_w, dq_a, point_load
        Later, 40, 2, 10, 20, 0
        malformed, nope
        Earlier, 10, 0, 0, 0, 3
        """
    )
    assert [c.name for c in comps] == ["Earlier", "Later"]
    assert comps[0].is_point
    _assert_close("point load", comps[0].point_load_kN, 3.0, 1e-12)
    _assert_close("distributed length", comps[1].length_m, 2.0, 1e-12)


def test_flat_seabed_object_matches_water_depth_scalar():
    """Passing FlatSeabed(D) in cfg must be identical to the legacy scalar."""
    cfg_scalar = _base_config()
    cfg_object = _base_config(seabed=FlatSeabed(_base_config()["water_depth_m"]))
    a = CatenarySystemCalculator(cfg_scalar)
    a.solve()
    b = CatenarySystemCalculator(cfg_object)
    b.solve()
    _assert_close("H_N", b.H_N, a.H_N, 1e-9)
    _assert_close("layback", b.layback, a.layback, 1e-9)
    _assert_close("S_total", b.S_total, a.S_total, 1e-9)
    _assert_close("top tension", b.top_tension_kN, a.top_tension_kN, 1e-9)


def test_planar_zero_slope_matches_flat():
    """PlanarSlopeSeabed(D, 0) must reproduce flat-seabed results exactly."""
    D = 100.0
    cfg_flat = _base_config(water_depth_m=D)
    cfg_slope = _base_config(seabed=PlanarSlopeSeabed(D, 0.0))
    a = CatenarySystemCalculator(cfg_flat)
    a.solve()
    b = CatenarySystemCalculator(cfg_slope)
    b.solve()
    _assert_close("layback", b.layback, a.layback, 1e-6)
    _assert_close("top tension", b.top_tension_kN, a.top_tension_kN, 1e-6)
    _assert_close("bottom tension", b.bottom_tension_kN, a.bottom_tension_kN, 1e-6)
    _assert_close("tdp slope deg", b.tdp_slope_deg, 0.0, 1e-12)


def test_planar_slope_tdp_tangency_and_bottom_tension():
    """At the TDP, V/H must equal tan(alpha) and bottom tension = H/cos(alpha)."""
    for slope_deg in (5.0, 10.0, 20.0):
        seabed = PlanarSlopeSeabed(depth_at_chute_m=100.0, slope_deg=slope_deg)
        cfg = _base_config(
            seabed=seabed,
            input_mode="Bottom Tension",
            H_input_N=50_000.0,
            H_guess_N=50_000.0,
            S_guess_m=900.0,
        )
        calc = CatenarySystemCalculator(cfg)
        calc.solve()

        # vertical_force_N is recorded from TDP outward; index 0 is V(0).
        assert calc.vertical_force_N is not None
        V0 = float(calc.vertical_force_N[0])
        tan_alpha_expected = math.tan(math.radians(slope_deg))
        _assert_close(
            f"V(0)/H slope={slope_deg}",
            V0 / calc.H_N,
            tan_alpha_expected,
            1e-9,
        )
        T_tdp_expected = calc.H_N / math.cos(math.radians(slope_deg)) / 1000.0
        _assert_close(
            f"bottom tension slope={slope_deg}",
            calc.bottom_tension_kN,
            T_tdp_expected,
            1e-6,
        )
        _assert_close(
            f"tdp slope reported slope={slope_deg}",
            calc.tdp_slope_deg,
            slope_deg,
            1e-9,
        )


def test_polyline_planar_equivalence():
    """A polyline sampled from a planar slope must reproduce the planar result."""
    D0 = 100.0
    slope_deg = 8.0
    xs = [0.0, 100.0, 200.0, 400.0, 800.0, 1600.0]
    tan_a = math.tan(math.radians(slope_deg))
    ds_samples = [D0 + tan_a * x for x in xs]

    cfg_plane = _base_config(
        seabed=PlanarSlopeSeabed(D0, slope_deg),
        S_guess_m=900.0,
    )
    cfg_poly = _base_config(
        seabed=PolylineSeabed(xs, ds_samples, slope_smoothing_m=1.0),
        S_guess_m=900.0,
    )
    a = CatenarySystemCalculator(cfg_plane)
    a.solve()
    b = CatenarySystemCalculator(cfg_poly)
    b.solve()
    _assert_close("polyline vs plane layback", b.layback, a.layback, 0.05)
    _assert_close("polyline vs plane bottom T", b.bottom_tension_kN, a.bottom_tension_kN, 0.01)
    _assert_close("polyline vs plane S_total", b.S_total, a.S_total, 0.1)


def test_tdp_fixed_point_converges_quickly():
    """For modest slopes across all solve modes, TDP fixed-point should be cheap."""
    seabed = PlanarSlopeSeabed(depth_at_chute_m=100.0, slope_deg=10.0)
    base = CatenarySystemCalculator(
        _base_config(seabed=seabed, S_guess_m=900.0)
    )
    base.solve()
    cases = [
        ("Bottom Tension", {"H_input_N": base.H_N}),
        ("Contact Tension", {"Ttop_input_N": base.top_tension_kN * 1000.0}),
        ("Tangent Angle", {"exit_angle_from_h_deg": base.exit_angle_deg_from_h}),
        ("Catenary Length", {"S_input_m": base.S_total}),
        ("Layback", {"layback_input_m": base.layback}),
    ]
    for mode, extra in cases:
        cfg = _base_config(
            input_mode=mode,
            seabed=PlanarSlopeSeabed(depth_at_chute_m=100.0, slope_deg=10.0),
            H_guess_N=base.H_N,
            S_guess_m=base.S_total,
            **extra,
        )
        calc = CatenarySystemCalculator(cfg)
        calc.solve()
        assert calc.diagnostics.tdp_iterations <= 6, (
            f"mode={mode} took {calc.diagnostics.tdp_iterations} TDP iters"
        )


def test_steep_slope_emits_sliding_warning():
    seabed = PlanarSlopeSeabed(depth_at_chute_m=100.0, slope_deg=25.0)
    cfg = _base_config(seabed=seabed, S_guess_m=900.0)
    calc = CatenarySystemCalculator(cfg)
    calc.solve()
    assert any("sliding" in w.lower() for w in calc.diagnostics.warnings), (
        f"Expected a sliding-stability warning; got: {calc.diagnostics.warnings}"
    )


def test_bottom_tension_input_is_actual_tension_at_tdp_on_slope():
    """User-facing Bottom Tension input must equal the reported tension at TDP,
    regardless of seabed slope (H is back-solved as T_TDP·cos α internally)."""
    T_input_kN = 50.0
    for slope_deg in (0.0, 10.0, 25.0):
        seabed = PlanarSlopeSeabed(depth_at_chute_m=100.0, slope_deg=slope_deg)
        cfg = _base_config(
            seabed=seabed,
            input_mode="Bottom Tension",
            H_input_N=T_input_kN * 1000.0,  # treated as T_TDP
            H_guess_N=T_input_kN * 1000.0,
            S_guess_m=900.0,
        )
        calc = CatenarySystemCalculator(cfg)
        calc.solve()
        _assert_close(
            f"bottom tension out == in, slope={slope_deg}",
            calc.bottom_tension_kN,
            T_input_kN,
            1e-6,
        )
        # And internally H = T·cos α.
        expected_H_N = T_input_kN * 1000.0 * math.cos(math.radians(slope_deg))
        _assert_close(
            f"internal H = T·cos α, slope={slope_deg}",
            float(calc.H_N),
            expected_H_N,
            1e-6,
        )


def test_buoyant_component_allows_negative_effective_weight():
    """A distributed-buoyancy component whose magnitude exceeds the base weight
    must yield a net-negative effective weight (the cable bows upward), rather
    than being clamped to a tiny positive floor."""
    base_q = 10.0
    # delta_q_water = -15 -> net q = -5 N/m inside the component span.
    comps = parse_components("Buoy, 50, 20, -15, -15, 0")
    cfg = _base_config(water_depth_m=50.0, q_water_npm=base_q, q_air_npm=base_q, components=comps)
    calc = CatenarySystemCalculator(cfg)
    # Sample inside the buoyant span (s_from_tdp=55) and submerged (y<0).
    q_inside = calc._q_effective(
        y=-10.0, s_from_tdp=55.0, S_free=100.0, L_chute_contact=0.0,
        assembly=[], comps=comps,
    )
    assert q_inside < 0.0, f"expected buoyant net-negative q, got {q_inside}"
    # Outside the span the base weight applies unchanged.
    q_outside = calc._q_effective(
        y=-10.0, s_from_tdp=5.0, S_free=100.0, L_chute_contact=0.0,
        assembly=[], comps=comps,
    )
    _assert_close("base weight outside span", q_outside, base_q, 1e-12)


def test_clean_flat_solve_reports_converged_and_no_penetration():
    """A normal flat-seabed solve should report convergence, no seabed
    penetration, and zero touchdown migration."""
    cfg = _base_config(seabed=FlatSeabed(100.0), S_guess_m=700.0, ds_m=1.0)
    calc = CatenarySystemCalculator(cfg)
    calc.solve()
    d = calc.diagnostics
    assert d.converged, f"expected converged solve; warnings={d.warnings}"
    assert d.boundary_residual_ok and d.input_residual_ok
    assert not d.seabed_penetration, "flat seabed should never be penetrated"
    assert d.touchdown_advance_iterations == 0
    assert d.min_seabed_clearance_m is not None
    assert d.min_seabed_clearance_m >= -0.5


def test_seabed_high_spot_is_detected_as_penetration():
    """When a shallow ridge sits between the chute and the nominal touchdown,
    the self-consistent single-span solution dips through the crest. The solver
    keeps the consistent geometry (touchdown world-x == layback, chute fixed at
    world-x 0) and *reports* the penetration rather than relocating the
    touchdown, which would violate the chute boundary condition."""
    xs = [0.0, 100.0, 250.0, 400.0, 700.0, 2000.0]
    dep = [120.0, 120.0, 40.0, 120.0, 150.0, 300.0]  # ridge crest (40 m) at x=250
    seabed = PolylineSeabed(xs, dep, slope_smoothing_m=5.0)
    cfg = _base_config(seabed=seabed, S_guess_m=700.0, ds_m=1.0)
    calc = CatenarySystemCalculator(cfg)
    calc.solve()
    d = calc.diagnostics
    # Touchdown is never relocated: the world frame stays self-consistent.
    assert d.touchdown_advance_iterations == 0
    assert d.touchdown_advanced_m == 0.0
    assert calc.tdp_x_world is not None and calc.layback is not None
    assert abs(float(calc.tdp_x_world) - float(calc.layback)) < 3.0, (
        f"tdp_x_world={calc.tdp_x_world} should equal layback={calc.layback}"
    )
    # The ridge crest is shallower than the free span, so penetration is detected.
    assert d.seabed_penetration, "expected seabed penetration to be detected"
    assert d.min_seabed_clearance_m is not None and d.min_seabed_clearance_m < 0.0
    assert d.seabed_penetration_max_m > 0.0


def test_zero_weight_assembly_segment_warns_about_fallback():
    """An assembly segment with blank/zero weight silently inherits the
    fallback cable weight; the diagnostics must surface that substitution."""
    assembly = [
        AssemblyItem("segment", "Unspecified", 500.0, 0.0, 0.0, 0.0),
    ]
    cfg = _base_config(assembly=assembly)
    calc = CatenarySystemCalculator(cfg)
    calc.solve()
    assert any(
        "fallback cable weight" in w and "Unspecified" in w
        for w in calc.diagnostics.warnings
    ), f"expected fallback-weight warning; got: {calc.diagnostics.warnings}"


def test_buoyant_assembly_segment_reduces_vertical_force():
    """A negative assembly-segment weight must act as distributed buoyancy.
    Regression: q_seg <= 0 used to be silently replaced by the fallback cable
    weight, so buoyant segments had no effect. V_end must equal the exact
    signed integral of q over the span."""
    assembly = [
        AssemblyItem("segment", "Upper", 40.0, 10.0, 10.0, 0.0),
        AssemblyItem("segment", "Buoy", 20.0, -6.0, -6.0, 0.0),
        AssemblyItem("segment", "Lower", 1000.0, 10.0, 10.0, 0.0),
    ]
    cfg = _base_config(water_depth_m=20.0, q_water_npm=10.0, q_air_npm=10.0, assembly=assembly)
    calc = CatenarySystemCalculator(cfg)
    _, _, vertical_force, _, _, s_arr, _, _ = calc.integrate(H_N=10_000.0, S_free_m=100.0, ds=0.5)
    # s from TDP: Lower occupies [0,40], Buoy [40,60], Upper [60,100].
    expected = 40.0 * 10.0 + 20.0 * (-6.0) + 40.0 * 10.0
    _assert_close("V_end with buoyant segment", vertical_force, expected, 1e-9)
    # V must actually *decrease* across the buoyant arc.
    assert calc.vertical_force_N is not None
    v = calc.vertical_force_N
    i40 = int(round(40.0 / 0.5))
    i60 = int(round(60.0 / 0.5))
    assert v[i60] < v[i40], f"V should fall across the buoyant arc: V(40)={v[i40]}, V(60)={v[i60]}"


def test_buoyant_assembly_segment_full_solve_warns_and_converges():
    """A full solve with a buoyant assembly segment must converge and emit the
    net-buoyancy advisory."""
    assembly = [
        AssemblyItem("segment", "Top", 80.0, 22.0, 28.0, 0.0),
        AssemblyItem("segment", "Buoy", 30.0, -10.0, 5.0, 0.0),
        AssemblyItem("segment", "Main", 2000.0, 22.0, 28.0, 0.0),
    ]
    cfg = _base_config(assembly=assembly)
    calc = CatenarySystemCalculator(cfg)
    calc.solve()
    assert calc.diagnostics.converged, f"warnings={calc.diagnostics.warnings}"
    assert any("Net-buoyant" in w for w in calc.diagnostics.warnings)
    # Zero/blank weights still inherit fallback and still warn.
    assert not any("fallback cable weight" in w for w in calc.diagnostics.warnings)


def test_surface_piercing_detected_with_redundant_buoyancy():
    """A strongly buoyant arc near the surface lifts a bight above sea level
    in the unconstrained static solution. The solver must flag it, exempt the
    legitimate final run up to the chute, and estimate the redundant buoyancy
    as ~|net upward q| x piercing arc length."""
    comps = parse_components("Buoy, 100, 60, -80, -80, 0")
    cfg = _base_config(
        water_depth_m=30.0,
        chute_exit_height_m=5.0,
        ds_m=0.25,
        q_water_npm=10.0,
        q_air_npm=12.0,
        components=comps,
        H_input_N=1500.0,
        H_guess_N=1500.0,
        S_guess_m=150.0,
    )
    calc = CatenarySystemCalculator(cfg)
    calc.solve()
    d = calc.diagnostics
    assert d.surface_piercing, "expected surface piercing to be detected"
    assert d.surface_piercing_span_m > 1.0
    assert d.surface_piercing_max_height_m > 0.0
    # net upward q in the buoyant arc = 80 - 10 = 70 N/m.
    expected_kN = 70.0 * d.surface_piercing_span_m / 1000.0
    _assert_close("redundant buoyancy", d.redundant_buoyancy_kN, expected_kN, 0.15 * expected_kN)
    assert any("float at the surface" in w for w in d.warnings)
    # The mask must not flag the final run up to the chute (which is above water).
    assert calc.surface_piercing_mask is not None
    assert not calc.surface_piercing_mask[-1], "chute-side run must be exempt"


def test_normal_solve_does_not_flag_surface_piercing():
    """A standard catenary with the chute above water leaves the surface once
    near the vessel; that run must NOT be flagged as piercing."""
    cfg = _base_config(chute_exit_height_m=5.0)
    calc = CatenarySystemCalculator(cfg)
    calc.solve()
    assert not calc.diagnostics.surface_piercing
    assert calc.y[-1] > 0.0, "sanity: cable does end above the surface"


def test_refinement_delta_is_small_on_sloped_seabed():
    """Regression: the half-step refinement check must seed the converged TDP
    world-x. Previously it started from x=0 (the chute), so on a sloped seabed
    a fully converged solve reported a spurious position delta equal to the
    depth difference between chute and TDP (~116 m at 10 deg)."""
    seabed = PlanarSlopeSeabed(depth_at_chute_m=100.0, slope_deg=10.0)
    cfg = _base_config(seabed=seabed, S_guess_m=900.0, ds_m=0.5)
    calc = CatenarySystemCalculator(cfg)
    calc.solve()
    d = calc.diagnostics
    assert d.refinement_position_delta_m is not None
    assert d.refinement_position_delta_m < 0.05, (
        f"refinement delta {d.refinement_position_delta_m:.3f} m is spuriously large"
    )


def test_failed_solve_restores_bottom_tension_input():
    """Regression: an infeasible Bottom Tension solve on a sloped seabed must
    not leave the internally-scaled H = T*cos(alpha) in cfg."""
    seabed = PlanarSlopeSeabed(depth_at_chute_m=100.0, slope_deg=20.0)
    T_in = 50_000.0
    cfg = _base_config(
        seabed=seabed,
        H_input_N=T_in,
        H_guess_N=T_in,
        S_guess_m=900.0,
        # Force failure: free span cannot reach an impossible chute height
        # with a tiny step budget.
        max_integration_steps=10,
    )
    calc = CatenarySystemCalculator(cfg)
    try:
        calc.solve()
    except Exception:
        pass
    _assert_close("H_input_N restored after failure", cfg["H_input_N"], T_in, 1e-9)


def run_all() -> List[str]:
    failures: List[str] = []
    tests: List[Callable[[], None]] = [
        test_unit_conversions,
        test_bottom_tension_uniform_catenary_matches_closed_form,
        test_diagnostics_report_residuals_and_refinement_delta,
        test_solve_modes_round_trip_from_bottom_tension_solution,
        test_chute_contact_geometry_matches_tangent_angle,
        test_legacy_component_distributed_load_applies_only_inside_range,
        test_body_point_load_changes_vertical_force_in_direct_integration,
        test_invalid_bottom_tension_is_rejected,
        test_diagnostics_warn_for_point_load_kink,
        test_parse_components_sorts_and_skips_malformed_lines,
        test_flat_seabed_object_matches_water_depth_scalar,
        test_planar_zero_slope_matches_flat,
        test_planar_slope_tdp_tangency_and_bottom_tension,
        test_polyline_planar_equivalence,
        test_tdp_fixed_point_converges_quickly,
        test_steep_slope_emits_sliding_warning,
        test_bottom_tension_input_is_actual_tension_at_tdp_on_slope,
        test_buoyant_component_allows_negative_effective_weight,
        test_clean_flat_solve_reports_converged_and_no_penetration,
        test_seabed_high_spot_is_detected_as_penetration,
        test_zero_weight_assembly_segment_warns_about_fallback,
        test_buoyant_assembly_segment_reduces_vertical_force,
        test_buoyant_assembly_segment_full_solve_warns_and_converges,
        test_surface_piercing_detected_with_redundant_buoyancy,
        test_normal_solve_does_not_flag_surface_piercing,
        test_refinement_delta_is_small_on_sloped_seabed,
        test_failed_solve_restores_bottom_tension_input,
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
