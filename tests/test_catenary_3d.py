"""Validation checks for the Catenary V2 3D/multi-span foundation module.

These tests avoid QGIS imports so the numerical backend can be exercised from a
normal Python environment.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "catenary" / "catenary_3d.py"

spec = importlib.util.spec_from_file_location("subsea_catenary_3d", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

Point3D = module.Point3D
BodySpanConnection = module.BodySpanConnection
chute_friction_bounds = module.chute_friction_bounds
evaluate_body_equilibrium = module.evaluate_body_equilibrium
project_2d_catenary_to_3d = module.project_2d_catenary_to_3d
seabed_contact_report = module.seabed_contact_report
solve_body_equilibrium = module.solve_body_equilibrium
solve_uniform_catenary_span_3d = module.solve_uniform_catenary_span_3d


def _assert_close(name: str, actual: float, expected: float, tolerance: float) -> None:
    assert abs(actual - expected) <= tolerance, (
        f"{name}: actual={actual:.12g} expected={expected:.12g} "
        f"diff={abs(actual - expected):.12g} tolerance={tolerance:.12g}"
    )


def test_project_2d_catenary_to_3d_uses_compass_bearing():
    points_north = project_2d_catenary_to_3d([0.0, 10.0], [0.0, -2.0], bearing_deg=0.0)
    _assert_close("north/east x", points_north[1].x, 0.0, 1e-12)
    _assert_close("north/y", points_north[1].y, 10.0, 1e-12)
    _assert_close("north/z", points_north[1].z, -2.0, 1e-12)

    points_east = project_2d_catenary_to_3d([0.0, 10.0], [0.0, -2.0], bearing_deg=90.0)
    _assert_close("east/x", points_east[1].x, 10.0, 1e-12)
    _assert_close("east/y", points_east[1].y, 0.0, 1e-12)


def test_uniform_span_endpoint_geometry_and_force_balance():
    q = 20.0
    length = 160.0
    start = Point3D(0.0, 0.0, -100.0)
    end = Point3D(100.0, 0.0, 0.0)
    sol = solve_uniform_catenary_span_3d("test", start, end, length, q, samples=101)

    _assert_close("end x", sol.points[-1].x, end.x, 1e-9)
    _assert_close("end z", sol.points[-1].z, end.z, 1e-8)
    _assert_close("arc length", sol.s_m[-1], length, 1e-7)
    assert sol.horizontal_tension_N > 0.0
    assert sol.end_tension_N > sol.start_tension_N

    # Cable forces on both endpoint supports should sum to the cable's own
    # submerged weight acting downward.
    total_support_force_z = sol.force_on_start_N.z + sol.force_on_end_N.z
    _assert_close("vertical endpoint force balance", total_support_force_z, -q * length, 1e-6)


def test_chute_friction_bounds_use_capstan_equation():
    result = chute_friction_bounds(
        contact_tension_kN=50.0,
        wrap_angle_deg=90.0,
        friction_coefficient=0.10,
    )
    expected_ratio = math.exp(0.10 * math.pi / 2.0)
    _assert_close("capstan ratio", result.capstan_ratio, expected_ratio, 1e-12)
    _assert_close("top side high", result.top_tension_if_top_side_high_kN, 50.0 * expected_ratio, 1e-12)
    _assert_close("top side low", result.top_tension_if_top_side_low_kN, 50.0 / expected_ratio, 1e-12)


def test_seabed_contact_report_detects_multiple_contacts_and_penetration():
    points = [
        Point3D(0.0, 0.0, -95.0),
        Point3D(10.0, 0.0, -100.1),
        Point3D(20.0, 0.0, -94.0),
        Point3D(30.0, 0.0, -100.2),
        Point3D(40.0, 0.0, -95.0),
    ]

    report = seabed_contact_report(
        points,
        seabed_depth_at_xy=lambda x, y: 100.0,
        tolerance_m=0.25,
        penetration_tolerance_m=0.05,
    )

    assert report.first_touch is not None
    assert len(report.contact_intervals) == 2
    assert len(report.penetration_intervals) == 2
    assert report.min_clearance_m < 0.0
    assert any("single TDP" in warning for warning in report.warnings)


def test_symmetric_two_leg_body_equilibrium_converges():
    spans = [
        BodySpanConnection("port leg", Point3D(-40.0, 0.0, -100.0), length_m=90.0, q_npm=20.0),
        BodySpanConnection("starboard leg", Point3D(40.0, 0.0, -100.0), length_m=90.0, q_npm=20.0),
    ]

    result = solve_body_equilibrium(
        initial_body_position=Point3D(5.0, 2.0, -50.0),
        spans=spans,
        submerged_weight_N=-3000.0,  # net buoyant body balances the two cable legs
        tolerance_N=1.0,
        max_iterations=20,
    )

    assert result.converged, result.warnings
    assert result.residual_norm_N <= 1.0
    _assert_close("symmetric x", result.body_position.x, 0.0, 0.01)
    _assert_close("symmetric y", result.body_position.y, 0.0, 0.01)
    assert -100.0 < result.body_position.z < 0.0
    assert len(result.span_solutions) == 2


def test_body_equilibrium_residual_changes_with_body_buoyancy():
    spans = [
        BodySpanConnection("port leg", Point3D(-40.0, 0.0, -100.0), length_m=90.0, q_npm=20.0),
        BodySpanConnection("starboard leg", Point3D(40.0, 0.0, -100.0), length_m=90.0, q_npm=20.0),
    ]
    pos = Point3D(0.0, 0.0, -50.0)

    less_buoyant = evaluate_body_equilibrium(pos, spans, submerged_weight_N=-1000.0)
    more_buoyant = evaluate_body_equilibrium(pos, spans, submerged_weight_N=-3000.0)

    # More buoyancy adds an upward external force, so the z residual should move
    # in the positive/upward direction.
    assert more_buoyant.residual_force_N.z > less_buoyant.residual_force_N.z
