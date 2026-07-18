# -*- coding: utf-8 -*-
"""Standalone checks for the Qt-free Plan of Work timeline engine."""

from datetime import datetime, timedelta

from ..planner.timeline_engine import TaskSpec, compute_schedule, position_at


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


def run_all():
    return [
        test_duration_resolution(), test_dependencies_resources_and_lag(),
        test_resource_starts_cross_vessel_links_and_outline_summary(),
        test_cycle_fallback(), test_position_fraction_reverse_and_hold(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
