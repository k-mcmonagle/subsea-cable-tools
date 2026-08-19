# -*- coding: utf-8 -*-
"""Pure checks for the Burial Planner installation-path engine."""

from __future__ import annotations

import json
import math

from ..burial import path_data
from ..burial import path_geometry as geom


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _near_point(points, wanted, tolerance=1e-6):
    return min(geom.distance(point, wanted) for point in points) <= tolerance


def test_dubins_endpoints_and_radius() -> bool:
    start = (0.0, 0.0, 0.0)
    end = (90.0, 70.0, math.radians(125.0))
    solution = geom.shortest_dubins_path(start, end, 20.0)
    ok = geom.distance(solution.points[0], start[:2]) < 1e-8
    ok = ok and geom.distance(solution.points[-1], end[:2]) < 1e-8
    curved = [part.radius_m for part in solution.primitives
              if part.kind in ("L", "R")]
    ok = ok and bool(curved) and all(abs(radius - 20.0) < 1e-8
                                     for radius in curved)
    ok = ok and all(part.length_m >= 0.0 for part in solution.primitives)
    return _result("Dubins endpoint + minimum-radius invariants", ok,
                   "/".join(solution.path_types))


def test_fillet_uses_every_geometry_course_change() -> bool:
    # Both tiny deflections are real geometry vertices. No A/C label and no
    # risk/profile angle threshold is involved in path generation.
    route = [(0.0, 0.0), (100.0, 0.0), (200.0, 1.0), (300.0, 1.0)]
    solution = geom.generate_route_path(route, 10.0, "fillet")
    ok = solution.course_change_count == 2
    ok = ok and len(solution.diagnostics) == 2
    ok = ok and all(item.solution == "fillet"
                    for item in solution.diagnostics)
    ok = ok and solution.max_offset_m > 0.0
    return _result("all non-collinear RPL vertices are course changes", ok)


def test_radius_fillet_geometry() -> bool:
    route = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    solution = geom.generate_route_path(route, 20.0, "fillet")
    expected_miss = 20.0 * (math.sqrt(2.0) - 1.0)
    ok = solution.course_change_count == 1
    ok = ok and abs(solution.diagnostics[0].miss_m - expected_miss) < 1e-6
    ok = ok and abs((geom.minimum_polyline_radius(solution.points) or 0.0)
                    - 20.0) < 0.05
    ok = ok and not _near_point(solution.points, route[1], 0.01)
    return _result("tangent circular fillet", ok,
                   f"miss={solution.diagnostics[0].miss_m:.3f} m")


def test_pass_through_every_course_change() -> bool:
    route = [(0.0, 0.0), (100.0, 0.0),
             (105.0, 25.0), (150.0, 55.0), (220.0, 55.0)]
    solution = geom.generate_route_path(route, 20.0, "through_ac")
    controls = route[1:-1]
    ok = solution.course_change_count == len(controls)
    ok = ok and all(_near_point(solution.points, point) for point in controls)
    ok = ok and all(item.miss_m == 0.0 for item in solution.diagnostics)
    curved = [part.radius_m for part in solution.primitives
              if part.kind in ("L", "R")]
    ok = ok and bool(curved) and all(radius is not None and radius >= 20.0
                                     for radius in curved)
    return _result("turn-out/turn-in passes through every course change", ok,
                   f"clusters={solution.compound_cluster_count}")


def test_back_to_back_cluster_and_corridor() -> bool:
    route = [(0.0, 0.0), (60.0, 0.0), (60.0, 12.0),
             (120.0, 12.0), (120.0, 80.0)]
    solution = geom.generate_route_path(route, 25.0, "fillet")
    ok = solution.course_change_count == 3
    ok = ok and solution.compound_cluster_count >= 1
    # Interacting corners are exact-through as one cluster; the independent
    # final corner remains the requested default fillet.
    compound = [item for item in solution.diagnostics
                if item.solution == "compound"]
    ok = ok and len(compound) >= 2
    ok = ok and all(_near_point(solution.points, route[item.vertex_index])
                    for item in compound)
    ok = ok and any(item.solution == "fillet" and item.miss_m > 0.0
                    for item in solution.diagnostics)
    rejected = False
    try:
        geom.generate_route_path(route, 25.0, "fillet", max_deviation_m=1.0)
    except geom.PathGeometryError:
        rejected = True
    ok = ok and rejected
    return _result("back-to-back course changes form a compound cluster", ok)


def test_layback_profile_and_track() -> bool:
    profile = [(10.0, 75.0), (50.0, 115.0)]
    ok = abs(geom.interpolate_profile(profile, 30.0) - 95.0) < 1e-9
    ok = ok and geom.interpolate_profile(profile, 5.0, "hold") == 75.0
    rejected = False
    try:
        geom.interpolate_profile(profile, 5.0, "error")
    except geom.PathGeometryError:
        rejected = True
    tool = [(0.0, 0.0), (100.0, 0.0), (200.0, 0.0)]
    barge = geom.layback_track(tool, [75.0, 95.0, 115.0])
    ok = ok and rejected and barge == [
        (75.0, 0.0), (195.0, 0.0), (315.0, 0.0)]
    return _result("depth-interpolated horizontal layback", ok)


def test_persistence_fingerprints_and_wkt() -> bool:
    plan = {
        "scope_start_kp": 0.0, "scope_end_kp": 1.0, "direction": 1,
        "params_json": json.dumps({"installation_paths": {
            "mode": "fillet", "layback_id": "l1",
            "generate_barge": True}}),
    }
    tool = {"tool_id": "t1", "modified_utc": "a"}
    config = {"config_id": "c1", "min_turn_radius_m": 20.0}
    layback = {"layback_id": "l1", "name": "constant",
               "points_json": "[[0,100]]", "modified_utc": "b"}
    path_config = path_data.config_from_plan(plan)
    first = path_data.build_fingerprints(
        plan, "route-a", tool, config, path_config, layback, "depth-a")
    # Constant layback intentionally ignores the bathymetry fingerprint.
    second = path_data.build_fingerprints(
        plan, "route-a", tool, config, path_config, layback, "depth-b")
    ok = first == second
    row = {
        "tool_path_wkt": path_data.linestring_wkt([(0.0, 1.0), (2.0, 3.0)]),
        "barge_track_wkt": "", "fingerprints_json": json.dumps(first),
    }
    ok = ok and path_data.result_state(row, first) == {
        "tool": "current", "barge": "missing"}
    ok = ok and path_data.parse_linestring_wkt(row["tool_path_wkt"]) == [
        (0.0, 1.0), (2.0, 3.0)]
    changed = dict(first, tool="changed")
    ok = ok and path_data.result_state(row, changed)["tool"] == "stale"
    return _result("path persistence + current/stale fingerprints", ok)


def test_dubins_words_reintegrate_exactly() -> bool:
    """Independently re-integrate every candidate word's primitives.

    The endpoint snap in dubins_candidates could hide a broken analytic
    word; advancing the primitives from the start pose by kind/length alone
    cannot, so this pins the formulae themselves.
    """
    def integrate(start, primitives):
        x, y, yaw = start
        for part in primitives:
            if part.kind == "S":
                x += part.length_m * math.cos(yaw)
                y += part.length_m * math.sin(yaw)
                continue
            sign = 1.0 if part.kind == "L" else -1.0
            angle = part.length_m / part.radius_m
            cx = x - sign * part.radius_m * math.sin(yaw)
            cy = y + sign * part.radius_m * math.cos(yaw)
            a0 = math.atan2(y - cy, x - cx)
            a1 = a0 + sign * angle
            x = cx + part.radius_m * math.cos(a1)
            y = cy + part.radius_m * math.sin(a1)
            yaw += sign * angle
        return x, y, yaw

    worst = 0.0
    seed = 1234567
    for _trial in range(400):
        seed = (1103515245 * seed + 12345) % (2 ** 31)
        values = []
        local = seed
        for _n in range(7):
            local = (1103515245 * local + 12345) % (2 ** 31)
            values.append(local / float(2 ** 31))
        radius = 5.0 + 300.0 * values[0]
        start = (values[1] * 800 - 400, values[2] * 800 - 400,
                 values[3] * math.tau - math.pi)
        end = (values[4] * 800 - 400, values[5] * 800 - 400,
               values[6] * math.tau - math.pi)
        for candidate in geom.dubins_candidates(start, end, radius):
            x, y, yaw = integrate(start, candidate.primitives)
            worst = max(worst, math.hypot(x - end[0], y - end[1])
                        + abs(geom.wrap_pi(yaw - end[2])) * radius)
    return _result("all Dubins words re-integrate to the target pose",
                   worst < 1e-6, f"worst {worst:.2e} m")


def test_offset_window_matches_brute_force() -> bool:
    route = [(0.0, 0.0), (150.0, 20.0), (160.0, 140.0),
             (40.0, 160.0), (30.0, 300.0), (200.0, 320.0)]
    solution = geom.generate_route_path(route, 90.0, "through_ac")
    reference = geom.clean_polyline(route)
    windowed, _integral, _rms = geom.path_offset_metrics(
        solution.points, reference)
    brute = max(geom.point_polyline_distance(point, reference)
                for point in solution.points)
    return _result("windowed offset scorer equals brute force",
                   abs(windowed - brute) < 1e-9,
                   f"{windowed:.3f} vs {brute:.3f} m")


def test_depth_banded_radius_per_corner() -> bool:
    # Two identical corners; a synthetic depth lookup puts the first in
    # shallow water (small radius band) and the second in deep water.
    route = [(0.0, 0.0), (600.0, 0.0), (600.0, 600.0),
             (1200.0, 600.0), (1200.0, 1200.0)]

    def radius_for_vertex(_index, station_m):
        return 30.0 if station_m < 1000.0 else 80.0

    solution = geom.generate_route_path(
        route, 20.0, "fillet", radius_for_vertex=radius_for_vertex)
    ok = solution.course_change_count == 3
    radii = [item.radius_m for item in solution.diagnostics]
    ok = ok and radii == [30.0, 80.0, 80.0]
    expected = [radius * (math.sqrt(2.0) - 1.0) for radius in radii]
    ok = ok and all(abs(item.miss_m - want) < 1e-6
                    for item, want in zip(solution.diagnostics, expected))
    rejected = False
    try:
        geom.generate_route_path(route, 20.0, "fillet",
                                 radius_for_vertex=lambda *_a: -1.0)
    except geom.PathGeometryError:
        rejected = True
    ok = ok and rejected
    return _result("depth-banded radius applies per corner", ok,
                   f"radii={radii}")


def test_progress_callback_counts_groups() -> bool:
    route = [(0.0, 0.0), (100.0, 0.0), (200.0, 80.0), (300.0, 80.0)]
    calls = []
    geom.generate_route_path(route, 15.0, "fillet",
                             progress=lambda done, total: calls.append(
                                 (done, total)))
    ok = bool(calls) and calls[0] == (0, calls[0][1]) \
        and calls[-1][0] == calls[-1][1]
    ok = ok and all(total == calls[0][1] for _done, total in calls)
    return _result("solver progress reports group counts", ok,
                   f"{len(calls)} callbacks")


def test_radius_rules_config_and_fingerprints() -> bool:
    raw = [{"max_depth_m": "1000", "radius_m": 1150.0},
           [100.0, 950.0], {"max_depth_m": -5, "radius_m": 10},
           "junk", {"max_depth_m": 100.0, "radius_m": 999.0}]
    rules = path_data.sanitise_radius_rules(raw)
    ok = rules == [{"max_depth_m": 100.0, "radius_m": 950.0},
                   {"max_depth_m": 1000.0, "radius_m": 1150.0}]
    ok = ok and path_data.radius_for_depth(rules, 50.0) == 950.0
    ok = ok and path_data.radius_for_depth(rules, 100.0) == 950.0
    ok = ok and path_data.radius_for_depth(rules, -400.0) == 1150.0
    ok = ok and path_data.radius_for_depth(rules, 1500.0) is None

    plan = {"scope_start_kp": 0.0, "scope_end_kp": 1.0, "direction": 1,
            "params_json": "{}"}
    tool = {"tool_id": "t1", "modified_utc": "a"}
    config_row = {"config_id": "c1", "min_turn_radius_m": 200.0}
    base_config = path_data.config_from_plan(plan)
    banded_config = dict(base_config, radius_rules=rules)
    base = path_data.build_fingerprints(
        plan, "route-a", tool, config_row, base_config, None, "depth-a")
    banded = path_data.build_fingerprints(
        plan, "route-a", tool, config_row, banded_config, None, "depth-a")
    moved = path_data.build_fingerprints(
        plan, "route-a", tool, config_row, banded_config, None, "depth-b")
    # Bands change the geometry; with bands the depth source does too.
    ok = ok and base["tool"] != banded["tool"]
    ok = ok and banded["tool"] != moved["tool"]
    ok = ok and base["tool"] == path_data.build_fingerprints(
        plan, "route-a", tool, config_row, base_config, None, "depth-b")["tool"]
    return _result("radius rules sanitise + fingerprint staleness", ok)


def test_compound_solver_cancellation() -> bool:
    route = [(0.0, 0.0), (60.0, 0.0), (60.0, 12.0),
             (120.0, 12.0), (120.0, 80.0)]
    cancelled = False
    try:
        geom.generate_route_path(
            route, 25.0, "through_ac", cancel=lambda: True)
    except geom.PathCancelled:
        cancelled = True
    return _result("compound solver cancellation is cooperative", cancelled)


def run_all() -> list:
    return [
        test_dubins_endpoints_and_radius(),
        test_fillet_uses_every_geometry_course_change(),
        test_radius_fillet_geometry(),
        test_pass_through_every_course_change(),
        test_back_to_back_cluster_and_corridor(),
        test_layback_profile_and_track(),
        test_persistence_fingerprints_and_wkt(),
        test_dubins_words_reintegrate_exactly(),
        test_offset_window_matches_brute_force(),
        test_depth_banded_radius_per_corner(),
        test_progress_callback_counts_groups(),
        test_radius_rules_config_and_fingerprints(),
        test_compound_solver_cancellation(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
