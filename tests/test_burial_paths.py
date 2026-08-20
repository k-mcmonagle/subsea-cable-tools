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
    # Interacting corners form one cluster. If exact-through would require a
    # Dubins revolution, they become an explicit reviewed best fit; the
    # independent final corner remains the requested default fillet.
    clustered = [item for item in solution.diagnostics
                 if item.solution in ("compound", "best_fit")]
    ok = ok and len(clustered) >= 2
    ok = ok and all(item.status == "review" for item in clustered)
    ok = ok and all(part.length_m / part.radius_m <= math.pi + 1e-7
                    for part in solution.primitives if part.radius_m)
    ok = ok and any(item.solution == "fillet" and item.miss_m > 0.0
                    for item in solution.diagnostics)
    limited = geom.generate_route_path(
        route, 25.0, "fillet", max_deviation_m=1.0)
    ok = ok and limited.max_offset_m > 1.0
    ok = ok and any(
        item.status == "review" and "review threshold" in item.message
        for item in limited.diagnostics)
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


def test_polyline_slice_uses_chainage_index() -> bool:
    """Dense route slicing must use indexed bounds, not scan every vertex."""
    class IndexedOnlySequence:
        def __init__(self, values):
            self.values = values

        def __len__(self):
            return len(self.values)

        def __getitem__(self, item):
            return self.values[item]

        def __iter__(self):
            raise AssertionError("polyline_slice walked the complete route")

    raw_points = [(float(index), 0.0) for index in range(10001)]
    raw_chainages = [float(index) for index in range(10001)]
    sliced = geom.polyline_slice(
        IndexedOnlySequence(raw_points),
        IndexedOnlySequence(raw_chainages), 4000.25, 4002.75)
    expected = [(4000.25, 0.0), (4001.0, 0.0), (4002.0, 0.0),
                (4002.75, 0.0)]
    return _result("polyline slicing uses chainage bisection",
                   sliced == expected)


def test_heading_lattice_prunes_dominated_predecessors() -> bool:
    """Strict DP lower bounds skip work without changing solver output."""
    controls = [(0.0, 0.0), (500.0, 50.0),
                (1000.0, -50.0), (1500.0, 0.0)]
    original_edge = geom._edge_best
    original_prune = geom._predecessor_cannot_beat
    calls = [0, 0]
    run = [0]

    def counted(*args, **kwargs):
        calls[run[0]] += 1
        return original_edge(*args, **kwargs)

    geom._edge_best = counted
    try:
        optimized = geom.solve_waypoint_path(
            controls, 100.0, reference=controls,
            heading_step_deg=15.0, refine_step_deg=15.0)
        run[0] = 1
        geom._predecessor_cannot_beat = lambda *_args: False
        unpruned = geom.solve_waypoint_path(
            controls, 100.0, reference=controls,
            heading_step_deg=15.0, refine_step_deg=15.0)
    finally:
        geom._edge_best = original_edge
        geom._predecessor_cannot_beat = original_prune
    ok = calls[0] < calls[1]
    ok = ok and optimized.points == unpruned.points
    ok = ok and optimized.primitives == unpruned.primitives
    ok = ok and optimized.waypoint_headings == unpruned.waypoint_headings
    ok = ok and optimized.path_types == unpruned.path_types
    ok = ok and optimized.max_offset_m == unpruned.max_offset_m
    ok = ok and optimized.rms_offset_m == unpruned.rms_offset_m
    ok = ok and optimized.length_m == unpruned.length_m
    return _result("heading lattice prunes dominated predecessors", ok,
                   f"{calls[0]} vs {calls[1]} edge solves")


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


def test_tight_route_end_uses_wide_recovery() -> bool:
    """A too-close route end gets one reviewed excursion, not an orbit."""
    theta = math.radians(82.0)
    route = [(0.0, 0.0), (900.0, 0.0), (900.0, 56.0),
             (900.0 + 3.0 * math.cos(theta), 56.0 + 3.0 * math.sin(theta))]

    def radius_for_vertex(_index, station_m):
        return 20.0 if station_m < 930.0 else 400.0

    solution = geom.generate_route_path(
        route, 20.0, "fillet", radius_for_vertex=radius_for_vertex)
    arcs = [part.length_m / part.radius_m
            for part in solution.primitives if part.radius_m]
    ok = geom.distance(solution.points[-1], route[-1]) <= 1e-6
    ok = ok and bool(arcs) and max(arcs) <= math.pi + 1e-7
    ok = ok and any(
        item.solution == "best_fit" and item.status == "review"
        and "wide minimum-radius recovery" in item.message
        for item in solution.diagnostics)
    return _result("tight route end uses reviewed wide recovery", ok,
                   f"offset={solution.max_offset_m:.1f} m")


def test_short_route_returns_non_looping_best_fit() -> bool:
    """Even a scope shorter than its radius returns a reviewed recovery."""
    route = [(0.0, 0.0), (30.0, 0.0), (30.0, 20.0)]
    solution = geom.generate_route_path(route, 200.0, "fillet")
    arcs = [part.length_m / part.radius_m
            for part in solution.primitives if part.radius_m]
    ok = geom.distance(solution.points[0], route[0]) <= 1e-6
    ok = ok and geom.distance(solution.points[-1], route[-1]) <= 1e-6
    ok = ok and bool(arcs) and max(arcs) <= math.pi + 1e-7
    ok = ok and all(item.solution == "best_fit"
                    and item.status == "review"
                    for item in solution.diagnostics)
    return _result("short route returns non-looping best fit", ok,
                   f"offset={solution.max_offset_m:.1f} m")


def test_tight_start_cluster_becomes_reviewed_best_fit() -> bool:
    """Close A/Cs at route entry must not generate orbiting Dubins loops."""
    route = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0),
             (15.0, 5.0), (4000.0, 5.0)]
    solution = geom.generate_route_path(route, 950.0, "through_ac")
    arc_angles = [part.length_m / part.radius_m
                  for part in solution.primitives
                  if part.kind in ("L", "R") and part.radius_m]
    ok = bool(arc_angles) and max(arc_angles) <= math.pi + 1e-7
    ok = ok and any(item.solution == "best_fit"
                    and item.status == "review"
                    and "tightly spaced" in item.message
                    for item in solution.diagnostics)
    ok = ok and solution.max_offset_m < 950.0
    return _result("tight start A/Cs use non-looping reviewed best fit", ok,
                   f"offset={solution.max_offset_m:.1f} m")


def test_failed_local_window_rejoins_farther_downstream() -> bool:
    """A failed chunk absorbs later controls before terminal recovery."""
    route = [
        (0.0, 0.0), (25.0, -43.301), (284.808, 106.699),
        (284.808, 156.699), (456.881, 402.444),
        (477.359, 388.105), (532.083, 237.754),
        (250.175, 135.148),
    ]
    solution = geom.generate_route_path(
        route, 60.0, "through_ac", max_cluster_size=2)
    recovered = [item for item in solution.diagnostics if item.recovery]
    arcs = [part.length_m / part.radius_m
            for part in solution.primitives if part.radius_m]
    ok = bool(recovered)
    ok = ok and not any(item.wide_recovery
                        for item in solution.diagnostics)
    ok = ok and all(item.solution == "best_fit"
                    and item.status == "review"
                    and "rejoins farther along" in item.message
                    for item in recovered)
    ok = ok and geom.distance(solution.points[-1], route[-1]) <= 1e-6
    ok = ok and bool(arcs) and max(arcs) <= math.pi + 1e-7
    return _result("failed local window rejoins farther downstream", ok,
                   f"controls={len(recovered)}")


def test_last_cluster_uses_remaining_straight_route() -> bool:
    """A last turn searches its collinear tail before going off-route."""
    p1 = (5.0, 5.0 * math.sqrt(3.0))
    p2 = (p1[0] - 10.0, p1[1] + 10.0 * math.sqrt(3.0))
    p3 = (p2[0] - 1000.0, p2[1] + 1000.0 * math.sqrt(3.0))
    route = [(0.0, 0.0), p1, p2, p3]
    solution = geom.generate_route_path(route, 200.0, "through_ac")
    ok = len(solution.diagnostics) == 1
    item = solution.diagnostics[0]
    ok = ok and item.recovery and not item.wide_recovery
    ok = ok and item.miss_m <= 1e-6
    ok = ok and "retains the feasible controls" in item.message
    ok = ok and geom.distance(solution.points[-1], route[-1]) <= 1e-6
    return _result("last cluster uses remaining straight RPL", ok,
                   f"offset={solution.max_offset_m:.1f} m")


def test_through_mode_chunks_large_clusters() -> bool:
    """A dense corner chain in pass-through mode is split into bounded
    chunks (the mega-cluster used to sit at one group and looked hung)."""
    route = [(0.0, 0.0)]
    x, y = 0.0, 0.0
    for index in range(13):
        x += 40.0
        y += 6.0 if index % 2 == 0 else -6.0
        route.append((x, y))
    calls = []
    solution = geom.generate_route_path(
        route, 30.0, "through_ac",
        progress=lambda done, total: calls.append((done, total)))
    ok = solution.course_change_count == len(route) - 2
    ok = ok and len(solution.diagnostics) == len(route) - 2
    ok = ok and bool(calls) and calls[-1][1] >= 2   # chunked: >1 group
    ok = ok and calls[-1][0] == calls[-1][1]
    return _result("through mode chunks dense clusters", ok,
                   f"groups={calls[-1][1]}")


def test_manual_adjustment_control() -> bool:
    """A user path adjustment (station, offset point) pulls the path
    through it and reports an 'adjustment' diagnostic."""
    route = [(0.0, 0.0), (1000.0, 0.0)]
    solution = geom.generate_route_path(
        route, 50.0, "fillet", extra_controls=[(500.0, (500.0, 40.0))])
    ok = _near_point(solution.points, (500.0, 40.0), 1e-6)
    adj = [item for item in solution.diagnostics
           if item.control_kind == "adjustment"]
    ok = ok and len(adj) == 1 and adj[0].solution == "adjustment"
    ok = ok and adj[0].miss_m == 0.0
    series = geom.signed_offset_series(solution.points, route)
    peak = max(series, key=lambda pair: abs(pair[1]))
    # The path reaches the +40 m adjustment (small arc overshoot allowed)
    # on the port side, near the requested station.
    ok = ok and 39.0 <= peak[1] <= 50.0 and abs(peak[0] - 500.0) < 100.0
    return _result("manual adjustment control pulls the path", ok,
                   f"peak {peak[1]:.1f} m at {peak[0]:.0f} m")


def test_adjustments_config_and_fingerprints() -> bool:
    raw = [{"kp": "2.5", "dcc_m": 30.0}, [1.0, -15.0],
           {"kp": -1.0, "dcc_m": 5.0}, "junk",
           {"kp": 2.5, "dcc_m": 12.0}]
    adjustments = path_data.sanitise_adjustments(raw)
    ok = adjustments == [{"kp": 1.0, "dcc_m": -15.0},
                         {"kp": 2.5, "dcc_m": 12.0}]
    plan = {"scope_start_kp": 0.0, "scope_end_kp": 5.0, "direction": 1,
            "params_json": "{}"}
    tool = {"tool_id": "t1", "modified_utc": "a"}
    config_row = {"config_id": "c1", "min_turn_radius_m": 200.0}
    base_config = path_data.config_from_plan(plan)
    ok = ok and base_config["adjustments"] == []
    adjusted = dict(base_config, adjustments=adjustments)
    base = path_data.build_fingerprints(
        plan, "route-a", tool, config_row, base_config, None, "d")
    with_adjustments = path_data.build_fingerprints(
        plan, "route-a", tool, config_row, adjusted, None, "d")
    # Adjustments change the geometry fingerprint; an empty list keeps the
    # pre-upgrade digest so stored results do not all flip to stale.
    ok = ok and base["tool"] != with_adjustments["tool"]
    explicit_empty = path_data.build_fingerprints(
        plan, "route-a", tool, config_row,
        dict(base_config, adjustments=[]), None, "d")
    ok = ok and explicit_empty["tool"] == base["tool"]
    return _result("adjustments sanitise + fingerprint staleness", ok)


def _spur_hairpin_route(tail: str) -> list:
    """A near-vertical spur into a ~95 deg hairpin onto a long ESE line —
    the 'route not designed for ploughing' shape. ``tail`` selects a
    sparse (one vertex then 3 km straight) or gently curving remainder."""
    points = []
    y = -160.0
    while y < -5.0:
        points.append((0.0, y))
        y += 18.0
    points.append((0.0, 0.0))
    heading = math.radians(-4.0)
    x, yy = 0.0, 0.0
    if tail == "sparse":
        legs = ((400.0, -2.0), (3000.0, -2.0), (500.0, -1.5), (600.0, 2.0))
        for leg, delta in legs:
            heading += math.radians(delta)
            x += leg * math.cos(heading)
            yy += leg * math.sin(heading)
            points.append((x, yy))
        return points
    heading = math.radians(-2.0)
    wiggles = (4, -6, 2, -3, 5, -2, 3, -4, 2, -8, -6, -4, 3, -5)
    legs = (140, 180, 240, 240, 250, 250, 300, 300, 400, 400, 350, 350,
            350, 400)
    for leg, wiggle in zip(legs, wiggles):
        heading -= math.radians(1.0)
        x += leg * math.cos(heading)
        yy += leg * math.sin(heading) + wiggle * 0.15
        points.append((x, yy))
    return points


def test_relaxed_cluster_rejoins_route_early() -> bool:
    """After a forced hairpin excursion the path must merge back onto the
    RPL at the earliest credible station and then follow it, instead of
    crawling to the far anchor along one Dubins diagonal."""
    route = _spur_hairpin_route("sparse")
    solution = geom.generate_route_path(route, 400.0, "fillet")
    reference = geom.clean_polyline(route)
    series = geom.signed_offset_series(solution.points, reference)
    tail = [abs(offset) for station, offset in series if station > 1500.0]
    ok = bool(tail) and max(tail) < 25.0
    ok = ok and solution.max_offset_m < 320.0
    arcs = [part.length_m / part.radius_m
            for part in solution.primitives if part.radius_m]
    ok = ok and bool(arcs) and max(arcs) <= math.pi + 1e-7
    return _result("relaxed cluster rejoins the RPL early", ok,
                   f"max={solution.max_offset_m:.0f} m, "
                   f"tail<{max(tail):.1f} m" if tail else "no tail")


def test_dropped_control_miss_uses_final_path() -> bool:
    """A reported control miss can never exceed the control's true distance
    to the FINAL stitched path (the truncated-window bug reported hundreds
    of metres for controls the path actually passes through)."""
    clean = geom.clean_polyline(_spur_hairpin_route("curved"))
    solution = geom.generate_route_path(clean, 400.0, "fillet")
    checked = 0
    ok = True
    for item in solution.diagnostics:
        actual = geom.point_polyline_distance(
            clean[item.vertex_index], solution.points)
        ok = ok and item.miss_m <= actual + 1e-6
        checked += 1
    ok = ok and checked > 0 and any(
        item.solution == "best_fit" for item in solution.diagnostics)
    return _result("dropped-control miss measured on the final path", ok,
                   f"{checked} diagnostics checked")


def test_window_always_covers_cluster_corners() -> bool:
    """A neighbour's large entry pad must not shrink a cluster window to
    exclude the cluster's own corners — that used to stitch the excluded
    corners back in as raw RPL, silently violating the minimum radius."""
    points = []
    y = -400.0
    while y < -20.0:
        points.append((6.0 * (1.0 + y / 400.0), y))
        y += 45.0
    points.append((0.0, 0.0))
    heading = math.radians(-1.0)
    x, yy = 0.0, 0.0
    legs = (250, 350, 500, 300, 350, 450, 500, 400, 2200, 2600, 1500)
    turns = (-1.0, -0.8, 1.2, -1.5, 0.8, -1.0, 1.5, -1.2, -3.0, -2.0, -1.0)
    for leg, delta in zip(legs, turns):
        heading += math.radians(delta)
        x += leg * math.cos(heading)
        yy += leg * math.sin(heading)
        points.append((x, yy))
    solution = geom.generate_route_path(points, 1000.0, "fillet")
    tightest = geom.minimum_polyline_radius(solution.points)
    ok = tightest is not None and tightest >= 999.0
    ok = ok and geom.distance(solution.points[-1], points[-1]) <= 1e-6
    return _result("cluster window always covers its corners", ok,
                   f"tightest radius {0.0 if tightest is None else tightest:.0f} m")


def test_infeasible_corners_dropped_route_followed() -> bool:
    """The control ladder drops the individually infeasible tight corners
    and keeps the path pinned to the remaining straight route."""
    route = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0),
             (15.0, 5.0), (4000.0, 5.0)]
    solution = geom.generate_route_path(route, 950.0, "through_ac")
    ok = solution.max_offset_m < 50.0
    ok = ok and any(item.solution == "best_fit"
                    for item in solution.diagnostics)
    return _result("infeasible corners dropped, route followed", ok,
                   f"offset={solution.max_offset_m:.1f} m")


def _hairpin_wiggle_route() -> list:
    """Spur into a 95 deg hairpin, then a gently wiggling 3.5 km tail with
    course changes every ~500 m — the user-reported 'multiple A/Cs in a
    short space' shape that used to lose the whole tail."""
    points = [(0.0, 0.0)]
    heading, x, y = 0.0, 0.0, 0.0
    for _ in range(8):
        x += 20.0 * math.cos(heading)
        y += 20.0 * math.sin(heading)
        points.append((x, y))
    heading += math.radians(95.0)
    x += 300.0 * math.cos(heading)
    y += 300.0 * math.sin(heading)
    points.append((x, y))
    for turn in (-2.0, -3.0, 2.0, -4.0, 3.0, -2.0, -3.0, 2.0):
        heading += math.radians(turn)
        x += 500.0 * math.cos(heading)
        y += 500.0 * math.sin(heading)
        points.append((x, y))
    heading += math.radians(0.0)
    points.append((x + 3000.0 * math.cos(heading),
                   y + 3000.0 * math.sin(heading)))
    return points


def test_unreachable_control_drops_alone() -> bool:
    """An infeasible leg near the window entry (the hairpin) must drop
    only the unreachable controls: the path merges back onto the RPL and
    follows the wiggling tail instead of crossing it as one diagonal."""
    route = geom.clean_polyline(_hairpin_wiggle_route())
    solution = geom.generate_route_path(route, 600.0, "fillet")
    series = geom.signed_offset_series(solution.points, route)
    tail = [abs(offset) for station, offset in series if station > 2400.0]
    ok = bool(tail) and max(tail) < 80.0
    # Not every downstream corner may be an exact control, but the final
    # path must pass close to the later ones (they used to miss by 250+ m).
    late = [item for item in solution.diagnostics
            if item.station_m > 2400.0 and item.turn_deg != 0.0]
    ok = ok and late and all(item.miss_m < 80.0 for item in late)
    arcs = [part.length_m / part.radius_m
            for part in solution.primitives if part.radius_m]
    ok = ok and bool(arcs) and max(arcs) <= math.pi + 1e-7
    ok = ok and geom.distance(solution.points[-1], route[-1]) <= 1e-6
    return _result("unreachable control drops alone, tail followed", ok,
                   f"tail max {max(tail):.1f} m" if tail else "no tail")


def test_signed_offset_series() -> bool:
    reference = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0)]
    points = [(10.0, 5.0), (50.0, -3.0), (105.0, 50.0), (95.0, 80.0)]
    series = geom.signed_offset_series(points, reference)
    ok = len(series) == 4
    # Left of +X travel is positive; right negative.
    ok = ok and abs(series[0][0] - 10.0) < 1e-9 and abs(series[0][1] - 5.0) < 1e-9
    ok = ok and abs(series[1][1] + 3.0) < 1e-9
    # Second leg travels +Y: x>100 is right (negative), x<100 left.
    ok = ok and abs(series[2][0] - 150.0) < 1e-9 and abs(series[2][1] + 5.0) < 1e-9
    ok = ok and abs(series[3][1] - 5.0) < 1e-9
    return _result("signed offsets follow travel-left convention", ok)


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
        test_polyline_slice_uses_chainage_index(),
        test_heading_lattice_prunes_dominated_predecessors(),
        test_depth_banded_radius_per_corner(),
        test_progress_callback_counts_groups(),
        test_radius_rules_config_and_fingerprints(),
        test_compound_solver_cancellation(),
        test_tight_route_end_uses_wide_recovery(),
        test_short_route_returns_non_looping_best_fit(),
        test_tight_start_cluster_becomes_reviewed_best_fit(),
        test_failed_local_window_rejoins_farther_downstream(),
        test_last_cluster_uses_remaining_straight_route(),
        test_through_mode_chunks_large_clusters(),
        test_manual_adjustment_control(),
        test_adjustments_config_and_fingerprints(),
        test_relaxed_cluster_rejoins_route_early(),
        test_dropped_control_miss_uses_final_path(),
        test_window_always_covers_cluster_corners(),
        test_infeasible_corners_dropped_route_followed(),
        test_unreachable_control_drops_alone(),
        test_signed_offset_series(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
