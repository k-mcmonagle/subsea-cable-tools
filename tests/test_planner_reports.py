# -*- coding: utf-8 -*-
"""Standalone checks for the Qt-free Planner reports calculations."""

from datetime import datetime, timedelta

from ..planner.reports import (
    aggregate, build_dataset, cable_series, cumulative_cable_laid, fuel_series,
    s_curve, variance_rows,
)
from ..planner.timeline_engine import (
    TaskSpec, compute_cable, compute_fuel, compute_schedule,
)


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


ANCHOR = datetime(2026, 1, 1, 0, 0)


def _fixture():
    """Two-task single-vessel plan with fuel, a baseline and actuals."""
    specs = [
        TaskSpec("t1", 0, "Lay main route", "v1", "manual", 24.0,
                 fuel_mode="transit", geom_kind="line", route_length_m=5000.0,
                 cable_mode="lay"),
        TaskSpec("t2", 1, "ROV survey", "v1", "manual", 12.0,
                 predecessor_task_id="t1", fuel_mode="dp", bunker_amount=10.0,
                 cable_mode="load", cable_amount_m=10000.0),
    ]
    schedule = compute_schedule(ANCHOR, specs)
    specs_by_id = {spec.task_id: spec for spec in specs}
    fuel = compute_fuel(schedule, specs_by_id, RESOURCES)
    cable = compute_cable(schedule, specs_by_id)
    rows = [
        {"task_id": "t1", "name": "Lay main route", "operation_type": "lay",
         "resource_id": "v1", "fuel_mode": "transit", "cable_mode": "lay",
         "progress_status": "completed", "percent_complete": 100.0,
         "actual_start_datetime": "2026-01-01T00:00",
         "actual_finish_datetime": "2026-01-02T06:00"},
        {"task_id": "t2", "name": "ROV survey", "operation_type": "",
         "resource_id": "v1", "fuel_mode": "dp",
         "cable_mode": "load", "cable_amount_m": 10000.0,
         "progress_status": "in_progress", "percent_complete": 50.0},
    ]
    baseline = {
        "span_start": "2026-01-01T00:00", "span_end": "2026-01-02T12:00",
        "tasks": [
            {"task_id": "t1", "duration_hours": 24.0,
             "start": "2026-01-01T00:00", "finish": "2026-01-01T18:00"},
            {"task_id": "t2", "duration_hours": 12.0,
             "start": "2026-01-01T18:00", "finish": "2026-01-02T12:00"},
        ],
    }
    return build_dataset(rows, schedule, fuel, specs, RESOURCES, baseline,
                         cable=cable)


RESOURCES = [{
    "resource_id": "v1", "name": "Vessel 1", "fuel_unit": "t",
    "fuel_start": 100.0, "fuel_rate_transit": 24.0, "fuel_rate_dp": 48.0,
    "fuel_cost_per_unit": 2.0, "color_hex": "#1f78b4",
}]


def test_build_dataset_joins():
    dataset = _fixture()
    by_id = {rec.task_id: rec for rec in dataset}
    t1, t2 = by_id["t1"], by_id["t2"]
    ok = t1.duration_hours == 24.0 and t1.distance_m == 5000.0
    ok = ok and t1.fuel_burn == 24.0 and t1.fuel_cost == 48.0
    ok = ok and t1.rob_start == 100.0 and t1.rob_end == 76.0
    ok = ok and t2.fuel_burn == 24.0 and t2.fuel_bunker == 10.0 and t2.rob_end == 62.0
    ok = ok and t1.resource_name == "Vessel 1"
    ok = ok and t1.baseline_finish == datetime(2026, 1, 1, 18, 0)
    ok = ok and t1.actual_finish == datetime(2026, 1, 2, 6, 0)
    ok = ok and t2.finish == ANCHOR + timedelta(hours=36)
    return _result("dataset joins schedule + fuel + baseline + actuals", ok)


def test_aggregate_measures_and_grouping():
    dataset = _fixture()
    labels = {"lay": "Lay", "": "(unspecified)"}
    by_op = aggregate(dataset, "duration_hours", "operation_type", labels)
    ok = by_op == [("Lay", 24.0), ("(unspecified)", 12.0)]
    by_distance = aggregate(dataset, "distance_km", "operation_type", labels)
    ok = ok and by_distance == [("Lay", 5.0)]
    by_count = aggregate(dataset, "count", "resource")
    ok = ok and by_count == [("Vessel 1", 2.0)]
    by_cost = aggregate(dataset, "fuel_cost", "fuel_mode")
    ok = ok and by_cost == [("dp", 48.0), ("transit", 48.0)]
    return _result("aggregate measures, grouping and label fallback", ok)


def test_fuel_series_points():
    dataset = _fixture()
    series = fuel_series(dataset, RESOURCES)
    ok = len(series) == 1 and series[0].unit == "t"
    points = series[0].points
    t1_finish = ANCHOR + timedelta(hours=24)
    t2_finish = ANCHOR + timedelta(hours=36)
    expected = [
        (ANCHOR, 100.0), (t1_finish, 76.0),          # burn slope through t1
        (t1_finish, 76.0),                            # t2 starts where t1 ended
        (t2_finish, 52.0), (t2_finish, 62.0),         # pre-bunker low, then jump
    ]
    ok = ok and points == expected
    return _result("fuel ROB polyline with bunker jump", ok)


def test_s_curve_and_earned_value():
    dataset = _fixture()
    now = ANCHOR + timedelta(hours=36)
    curves = s_curve(dataset, now=now)
    planned_pct = [pct for _when, pct in curves.planned]
    ok = planned_pct[0] == 0.0 and abs(planned_pct[-1] - 100.0) < 1e-9
    ok = ok and abs(curves.planned[1][1] - 24.0 / 36.0 * 100.0) < 1e-6
    ok = ok and curves.baseline[-1][0] == datetime(2026, 1, 2, 12, 0)
    # Earned: t1 complete (24 h) + half of t2 (6 h) of 36 h total.
    ok = ok and abs(curves.earned_pct_now - 30.0 / 36.0 * 100.0) < 1e-6
    ok = ok and abs(curves.planned_pct_now - 100.0) < 1e-6
    ok = ok and curves.spi is not None and abs(curves.spi - 30.0 / 36.0) < 1e-6
    # Actual curve: completed t1 at its recorded finish, then "now" at earned %.
    ok = ok and curves.actual[-1] == (now, curves.earned_pct_now)
    ok = ok and any(
        when == datetime(2026, 1, 2, 6, 0) and abs(pct - 24.0 / 36.0 * 100.0) < 1e-6
        for when, pct in curves.actual)
    return _result("s-curve planned/baseline/actual + SPI", ok)


def test_cable_tracking_reports():
    dataset = _fixture()
    by_id = {rec.task_id: rec for rec in dataset}
    # Lay amount auto-resolves to the route length; load uses its typed amount.
    ok = by_id["t1"].cable_amount_m == 5000.0 and by_id["t1"].cable_delta_m == -5000.0
    ok = ok and by_id["t1"].cable_onboard_end_m == -5000.0
    ok = ok and by_id["t2"].cable_onboard_end_m == 5000.0
    series = cable_series(dataset, RESOURCES)
    t1_finish = ANCHOR + timedelta(hours=24)
    t2_finish = ANCHOR + timedelta(hours=36)
    ok = ok and len(series) == 1 and series[0].points == [
        (ANCHOR, 0.0), (t1_finish, -5.0),
        (t1_finish, -5.0), (t2_finish, 5.0)]
    laid = cumulative_cable_laid(dataset)
    ok = ok and laid == [(ANCHOR, 0.0), (t1_finish, 5.0)]
    by_cable = aggregate(dataset, "cable_laid_km", "operation_type", {"lay": "Lay"})
    ok = ok and by_cable == [("Lay", 5.0)]
    return _result("cable onboard/laid series + laid measure", ok)


def test_s_curve_cable_weighting():
    dataset = _fixture()
    now = ANCHOR + timedelta(hours=36)
    curves = s_curve(dataset, now=now, weight="cable_laid")
    # Only the lay task carries cable weight: planned 0 -> 100% at its finish.
    ok = curves.planned[-1] == (ANCHOR + timedelta(hours=24), 100.0)
    ok = ok and abs(curves.earned_pct_now - 100.0) < 1e-9
    ok = ok and curves.spi is not None and abs(curves.spi - 1.0) < 1e-9
    return _result("s-curve weighted by cable laid", ok)


def test_variance_rows():
    dataset = _fixture()
    rows = variance_rows(dataset)
    ok = [row.task_id for row in rows] == ["t1", "t2"]
    # t1 finished 12 h after its baseline finish (recorded actual).
    ok = ok and rows[0].variance_hours == 12.0 and rows[0].is_actual
    # t2 has no actual yet; forecast finish equals baseline finish.
    ok = ok and rows[1].variance_hours == 0.0 and not rows[1].is_actual
    return _result("finish variance vs baseline, worst first", ok)


def test_empty_dataset_is_safe():
    empty = build_dataset([], None, None, [], [], None)
    curves = s_curve(empty, now=ANCHOR)
    ok = empty == [] and aggregate(empty) == [] and fuel_series(empty, []) == []
    ok = ok and curves.planned == [] and curves.actual == [] and curves.spi is None
    ok = ok and variance_rows(empty) == []
    return _result("empty inputs produce empty reports", ok)


def run_all():
    return [
        test_build_dataset_joins(),
        test_aggregate_measures_and_grouping(),
        test_fuel_series_points(),
        test_s_curve_and_earned_value(),
        test_cable_tracking_reports(),
        test_s_curve_cable_weighting(),
        test_variance_rows(),
        test_empty_dataset_is_safe(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
