# -*- coding: utf-8 -*-
"""Standalone checks for the Qt-free Plan of Work timeline engine."""

from datetime import datetime, timedelta

from ..planner.timeline_engine import (
    TaskSpec, compute_cable, compute_fuel, compute_schedule, position_at,
)


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def test_duration_resolution():
    anchor = datetime(2026, 8, 14, 0, 0)
    tasks = [
        TaskSpec("a", 0, "Lay", "v", "computed", 99.0, speed_knots=2.0,
                 geom_kind="line", route_length_m=3704.0),
        TaskSpec("b", 1, "Fallback", "v", "computed", 3.0, speed_knots=0.0,
                 geom_kind="line", route_length_m=1000.0),
    ]
    result = compute_schedule(anchor, tasks)
    ok = abs(result.tasks[0].duration_hours - 1.0) < 1e-5
    ok = ok and result.tasks[1].duration_hours == 3.0 and bool(result.tasks[1].warning)
    return _result("computed duration + manual fallback", ok)


def test_dependencies_resources_and_lag():
    anchor = datetime(2026, 1, 1)
    tasks = [
        TaskSpec("a", 0, resource_id="v1", duration_hours=2),
        TaskSpec("b", 1, resource_id="v2", duration_hours=5),
        TaskSpec("c", 2, resource_id="v1", duration_hours=1,
                 predecessor_task_id="a", lag_hours=1),
    ]
    result = compute_schedule(anchor, tasks)
    by_id = {task.task_id: task for task in result.tasks}
    ok = by_id["a"].start == by_id["b"].start == anchor
    ok = ok and by_id["c"].start == anchor + timedelta(hours=3)
    ok = ok and result.span_end == anchor + timedelta(hours=5)
    return _result("FS lag + parallel resource lanes", ok)


def test_resource_starts_cross_vessel_links_and_outline_summary():
    anchor = datetime(2026, 1, 1)
    specs = [
        TaskSpec("phase", 0, "Installation", is_phase=True, outline_level=0),
        TaskSpec("a", 1, "Vessel 1 prep", "v1", duration_hours=2, outline_level=1),
        TaskSpec("subphase", 2, "SIMOPS", is_phase=True, outline_level=1),
        TaskSpec("b", 3, "Vessel 2 work", "v2", duration_hours=1,
                 predecessor_task_id="a", outline_level=2),
        TaskSpec("c", 4, "Vessel 1 recovery", "v1", duration_hours=1,
                 predecessor_task_id="b", outline_level=1),
    ]
    result = compute_schedule(anchor, specs, {"v1": 0.0, "v2": 5.0})
    by_id = {task.task_id: task for task in result.tasks}
    ok = by_id["a"].start == anchor
    ok = ok and by_id["b"].start == anchor + timedelta(hours=5)
    ok = ok and by_id["c"].start == anchor + timedelta(hours=6)
    ok = ok and by_id["subphase"].start == anchor + timedelta(hours=5)
    ok = ok and by_id["subphase"].finish == anchor + timedelta(hours=6)
    ok = ok and by_id["phase"].start == anchor and by_id["phase"].finish == anchor + timedelta(hours=7)
    ok = ok and set(result.by_resource) == {"v1", "v2"}
    states = position_at(
        result, {spec.task_id: spec for spec in specs}, anchor + timedelta(hours=5.5))
    ok = ok and set(states) == {"v1", "v2"}
    ok = ok and states["v1"].task_id == "a" and not states["v1"].active
    ok = ok and states["v2"].task_id == "b" and states["v2"].active
    return _result("resource availability + cross-vessel FS + outline summaries", ok)


def test_cycle_fallback():
    anchor = datetime(2026, 1, 1)
    result = compute_schedule(anchor, [
        TaskSpec("a", 0, duration_hours=1, predecessor_task_id="b"),
        TaskSpec("b", 1, duration_hours=2, predecessor_task_id="a"),
    ])
    ok = bool(result.errors) and result.tasks[0].start == anchor
    ok = ok and result.tasks[1].start == anchor + timedelta(hours=1)
    return _result("cycle fallback", ok)


def test_backward_from_required_finish():
    required_finish = datetime(2026, 1, 2, 12, 0)
    specs = [
        TaskSpec("a", 0, "Prep", "v1", duration_hours=2),
        TaskSpec("b", 1, "Install", "v1", duration_hours=1,
                 predecessor_task_id="a", lag_hours=1),
        TaskSpec("c", 2, "Support vessel", "v2", duration_hours=4),
    ]
    result = compute_schedule(required_finish, specs, schedule_mode="backward")
    by_id = {task.task_id: task for task in result.tasks}
    ok = by_id["b"].finish == required_finish
    ok = ok and by_id["b"].start == required_finish - timedelta(hours=1)
    ok = ok and by_id["a"].finish == required_finish - timedelta(hours=2)
    ok = ok and by_id["a"].start == required_finish - timedelta(hours=4)
    ok = ok and by_id["c"].start == required_finish - timedelta(hours=4)
    ok = ok and result.span_start == required_finish - timedelta(hours=4)
    ok = ok and result.span_end == required_finish
    return _result("backward schedule from required finish", ok)


def test_advanced_links_constraints_resource_dates_and_float():
    anchor = datetime(2026, 1, 1)
    specs = [
        TaskSpec("a", 0, "Main", "v1", duration_hours=4),
        TaskSpec("ss", 1, "SS", "v2", duration_hours=2,
                 predecessor_task_id="a", lag_hours=1, dependency_type="SS"),
        TaskSpec("ff", 2, "FF", "v3", duration_hours=1,
                 predecessor_task_id="a", lag_hours=1, dependency_type="FF"),
        TaskSpec("late", 3, "Constrained", "v4", duration_hours=2,
                 constraint_type="snet", constraint_datetime=anchor + timedelta(hours=8)),
        TaskSpec("available", 4, "Availability", "v5", duration_hours=1),
        TaskSpec("milestone", 5, "Milestone", "v6", duration_hours=99,
                 is_milestone=True),
    ]
    result = compute_schedule(
        anchor, specs, resource_start_datetimes={"v5": anchor + timedelta(hours=6)})
    by_id = {task.task_id: task for task in result.tasks}
    ok = by_id["ss"].start == anchor + timedelta(hours=1)
    ok = ok and by_id["ff"].finish == anchor + timedelta(hours=5)
    ok = ok and by_id["late"].start == anchor + timedelta(hours=8)
    ok = ok and by_id["available"].start == anchor + timedelta(hours=6)
    ok = ok and by_id["milestone"].duration_hours == 0.0

    critical = compute_schedule(anchor, [
        TaskSpec("p", 0, "Path 1", "v1", duration_hours=2),
        TaskSpec("q", 1, "Path 2", "v1", duration_hours=2),
        TaskSpec("short", 2, "Short", "v2", duration_hours=1),
    ])
    critical_by_id = {task.task_id: task for task in critical.tasks}
    ok = ok and critical_by_id["p"].critical and critical_by_id["q"].critical
    ok = ok and abs(critical_by_id["short"].total_float_hours - 3.0) < 1e-9
    return _result("advanced links + constraints + resource dates + critical float", ok)


def test_simops_same_location_warning():
    anchor = datetime(2026, 1, 1)
    result = compute_schedule(anchor, [
        TaskSpec("a", 0, "Vessel A", "v1", duration_hours=2, location_key="route|7|end"),
        TaskSpec("b", 1, "Vessel B", "v2", duration_hours=1, location_key="route|7|end"),
    ])
    ok = any("SIMOPS review" in warning for warning in result.warnings)
    return _result("same-location concurrent work raises SIMOPS review", ok)


def test_position_fraction_reverse_and_hold():
    anchor = datetime(2026, 1, 1)
    specs = [
        TaskSpec("a", 0, resource_id="v", duration_hours=2,
                 geom_kind="line", route_length_m=1000, direction="reverse"),
        TaskSpec("b", 1, resource_id="v", duration_hours=1,
                 predecessor_task_id="a"),
    ]
    result = compute_schedule(anchor, specs)
    lookup = {spec.task_id: spec for spec in specs}
    mid = position_at(result, lookup, anchor + timedelta(hours=1))["v"]
    held = position_at(result, lookup, anchor + timedelta(hours=5))["v"]
    ok = abs(mid.fraction - 0.5) < 1e-9 and abs(mid.chainage_m - 500) < 1e-9
    ok = ok and held.task_id == "b" and held.fraction == 1.0 and not held.active
    return _result("position fraction/reverse/clamp/hold", ok)


def test_fuel_burn_bunker_and_warnings():
    anchor = datetime(2026, 1, 1)
    specs = [
        TaskSpec("t", 0, "Transit out", "v", duration_hours=24, fuel_mode="transit"),
        TaskSpec("l", 1, "Lay", "v", duration_hours=12, fuel_mode="dp",
                 predecessor_task_id="t"),
        TaskSpec("p", 2, "Port call", "v", duration_hours=24, fuel_mode="port",
                 bunker_amount=100.0, predecessor_task_id="l"),
        TaskSpec("x", 3, "Long DP", "v", duration_hours=240, fuel_mode="dp",
                 predecessor_task_id="p"),
        TaskSpec("n", 4, "Untracked", "v", duration_hours=6,
                 predecessor_task_id="x"),
    ]
    resources = [{
        "resource_id": "v", "fuel_unit": "t", "fuel_rate_transit": 24.0,
        "fuel_rate_dp": 12.0, "fuel_rate_anchor": 4.0, "fuel_rate_port": 2.0,
        "fuel_start": 40.0, "fuel_cost_per_unit": 500.0,
    }]
    result = compute_schedule(anchor, specs)
    fuel = compute_fuel(result, {spec.task_id: spec for spec in specs}, resources)
    lane = fuel.by_resource["v"]
    ok = abs(fuel.by_task["t"].burn - 24.0) < 1e-9         # 24 h at 24 t/24 h
    ok = ok and abs(fuel.by_task["l"].burn - 6.0) < 1e-9   # 12 h at 12 t/24 h
    # ROB after port call: 40 - 24 - 6 - 2, then +100 bunkered at finish.
    ok = ok and abs(fuel.by_task["p"].rob_end - 108.0) < 1e-9
    ok = ok and abs(fuel.by_task["n"].burn) < 1e-9         # no fuel mode -> no burn
    ok = ok and abs(lane.total_bunker - 100.0) < 1e-9
    ok = ok and abs(lane.rob_end - (108.0 - 120.0)) < 1e-9
    ok = ok and abs(lane.min_rob - -12.0) < 1e-9
    ok = ok and len(lane.warnings) == 1 and "Long DP" in lane.warnings[0]
    ok = ok and abs(lane.cost - (24 + 6 + 2 + 120) * 500.0) < 1e-9
    return _result("fuel burn, bunkering, ROB warning, cost", ok)


def test_speed_profile_duration_and_position():
    import json

    from ..planner.timeline_engine import (
        KNOT_M_PER_HOUR, parse_speed_profile, profile_distance_at,
        profile_duration_hours, resolve_speed_profile,
    )

    # 5 km at 5 km/h (1 h) then the remainder (20 km) at 10 km/h (2 h).
    raw = json.dumps({"segments": [
        {"distance_m": 5000.0, "speed_knots": 5000.0 / KNOT_M_PER_HOUR},
        {"distance_m": None, "speed_knots": 10000.0 / KNOT_M_PER_HOUR},
    ]})
    segments = parse_speed_profile(raw)
    hours, warning = profile_duration_hours(segments, 25000.0)
    ok = segments is not None and hours is not None
    ok = ok and abs(hours - 3.0) < 1e-9 and not warning
    resolved = resolve_speed_profile(segments, 25000.0)
    ok = ok and abs(profile_distance_at(resolved, 0.5) - 2500.0) < 1e-6
    ok = ok and abs(profile_distance_at(resolved, 2.0) - 15000.0) < 1e-6
    ok = ok and abs(profile_distance_at(resolved, 99.0) - 25000.0) < 1e-6

    anchor = datetime(2026, 8, 14, 0, 0)
    spec = TaskSpec("t", 0, "Transit", "v", "manual", 1.0, geom_kind="line",
                    route_length_m=25000.0, speed_profile=segments)
    result = compute_schedule(anchor, [spec])
    ok = ok and abs(result.tasks[0].duration_hours - 3.0) < 1e-9
    # Playback follows the legs: after 2 h the vessel is 15 km along, not
    # the 16.67 km a constant average speed would give.
    states = position_at(result, {"t": spec}, anchor + timedelta(hours=2))
    ok = ok and states["v"].chainage_m is not None
    ok = ok and abs(states["v"].chainage_m - 15000.0) < 1e-6
    reverse = TaskSpec("t", 0, "Transit", "v", "manual", 1.0, geom_kind="line",
                       route_length_m=25000.0, speed_profile=segments,
                       direction="reverse")
    reverse_states = position_at(
        compute_schedule(anchor, [reverse]), {"t": reverse},
        anchor + timedelta(hours=2))
    ok = ok and abs(reverse_states["v"].chainage_m - 10000.0) < 1e-6

    # A remainder leg with no known distance degrades to the stored duration.
    unsized = TaskSpec("u", 0, "Load", "v", "manual", 4.0,
                       speed_profile=segments)
    unsized_result = compute_schedule(anchor, [unsized])
    ok = ok and abs(unsized_result.tasks[0].duration_hours - 4.0) < 1e-9
    ok = ok and any("remainder" in warning for warning in unsized_result.warnings)

    # Explicit legs only need no total: 20 km at 4 km/h -> 5 h.
    loading = TaskSpec("l", 0, "Loading", "v", "manual", 1.0,
                       route_length_m=20000.0,
                       speed_profile=[(20000.0, 4000.0 / KNOT_M_PER_HOUR)])
    loading_result = compute_schedule(anchor, [loading])
    ok = ok and abs(loading_result.tasks[0].duration_hours - 5.0) < 1e-9
    return _result("speed profile duration + leg-accurate playback", ok)


def test_task_paced_playback_clock():
    """Each boundary interval gets equal wall time; markers stay smooth."""
    from ..planner.timeline_engine import advance_task_paced, schedule_boundaries

    anchor = datetime(2026, 8, 14, 0, 0)
    # A 1 h operation followed by a 24 h transit on one lane.
    specs = [
        TaskSpec("short", 0, "Op", "v", "manual", 1.0),
        TaskSpec("long", 1, "Transit", "v", "manual", 24.0,
                 predecessor_task_id="short"),
    ]
    result = compute_schedule(anchor, specs)
    boundaries = schedule_boundaries(result)
    ok = boundaries == [anchor, anchor + timedelta(hours=1),
                        anchor + timedelta(hours=25)]

    pace = 2.0  # two real seconds per interval
    # Half a real second into the short task -> a quarter of the way through.
    quarter = advance_task_paced(boundaries, anchor, 0.5, pace)
    ok = ok and quarter == anchor + timedelta(minutes=15)
    # Two real seconds finish the short task exactly; rate stays constant
    # inside the interval, so the marker glides rather than jumping.
    ok = ok and advance_task_paced(boundaries, anchor, 2.0, pace) == (
        anchor + timedelta(hours=1))
    # One tick can span boundaries: 3 s = short task + half the transit.
    ok = ok and advance_task_paced(boundaries, anchor, 3.0, pace) == (
        anchor + timedelta(hours=13))
    # Overshoot clamps to the end of the plan.
    ok = ok and advance_task_paced(boundaries, anchor, 99.0, pace) == (
        anchor + timedelta(hours=25))
    # Resuming mid-interval keeps the same per-interval pacing.
    ok = ok and advance_task_paced(boundaries, quarter, 1.5, pace) == (
        anchor + timedelta(hours=1))
    return _result("task-paced playback clock", ok)


def test_cable_onboard_tracking():
    anchor = datetime(2026, 1, 1)
    specs = [
        TaskSpec("load", 0, "Load out", "v1", duration_hours=12,
                 cable_mode="load", cable_amount_m=50000.0),
        TaskSpec("lay", 1, "Lay route", "v1", duration_hours=24,
                 predecessor_task_id="load", cable_mode="lay",
                 geom_kind="line", route_length_m=30000.0),
        TaskSpec("recover", 2, "Recover stub", "v1", duration_hours=6,
                 predecessor_task_id="lay", cable_mode="recover",
                 cable_amount_m=2000.0),
        TaskSpec("transit", 3, "Transit", "v1", duration_hours=10,
                 predecessor_task_id="recover"),
        # Second vessel lays with nothing loaded: onboard goes negative.
        TaskSpec("bare", 4, "Lay unloaded", "v2", duration_hours=8,
                 cable_mode="lay", cable_amount_m=4000.0),
    ]
    result = compute_schedule(anchor, specs)
    cable = compute_cable(result, {spec.task_id: spec for spec in specs})
    v1 = cable.by_resource["v1"]
    ok = cable.by_task["load"].onboard_end_m == 50000.0
    # Lay amount comes from the route length when no amount is typed.
    ok = ok and cable.by_task["lay"].amount_m == 30000.0
    ok = ok and cable.by_task["lay"].onboard_end_m == 20000.0
    ok = ok and cable.by_task["recover"].onboard_end_m == 22000.0
    ok = ok and "transit" not in cable.by_task  # no cable mode, no entry
    ok = ok and v1.total_loaded_m == 50000.0 and v1.total_laid_m == 30000.0
    ok = ok and v1.total_recovered_m == 2000.0 and v1.onboard_end_m == 22000.0
    ok = ok and not v1.warnings
    v2 = cable.by_resource["v2"]
    ok = ok and v2.onboard_end_m == -4000.0 and v2.min_onboard_m == -4000.0
    ok = ok and len(v2.warnings) == 1 and "Lay unloaded" in v2.warnings[0]
    return _result("cable onboard: auto lay amount, totals, negative warning", ok)


def run_all():
    return [
        test_duration_resolution(), test_dependencies_resources_and_lag(),
        test_resource_starts_cross_vessel_links_and_outline_summary(),
        test_cycle_fallback(), test_backward_from_required_finish(),
        test_advanced_links_constraints_resource_dates_and_float(),
        test_simops_same_location_warning(),
        test_position_fraction_reverse_and_hold(),
        test_fuel_burn_bunker_and_warnings(),
        test_cable_onboard_tracking(),
        test_speed_profile_duration_and_position(),
        test_task_paced_playback_clock(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
