# -*- coding: utf-8 -*-
"""Qt-free report dataset and calculations for the Planner reports window.

Everything here works on plain dicts, the timeline engine's result objects and
stdlib datetimes, so the whole module runs (and is tested) without QGIS. The
reports window is a thin chart/table view over these functions, and any future
custom report is another (measure, group-by) pair fed to :func:`aggregate`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple


def parse_datetime(value) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ReportRow:
    """One scheduled task flattened across plan, fuel, baseline and actuals."""

    task_id: str
    name: str = ""
    operation_type: str = ""
    resource_id: str = ""
    resource_name: str = ""
    is_phase: bool = False
    is_milestone: bool = False
    start: Optional[datetime] = None
    finish: Optional[datetime] = None
    duration_hours: float = 0.0
    distance_m: float = 0.0
    fuel_mode: str = ""
    fuel_burn: float = 0.0
    fuel_bunker: float = 0.0
    fuel_cost: float = 0.0
    rob_start: float = 0.0
    rob_end: float = 0.0
    progress_status: str = "not_started"
    percent_complete: float = 0.0
    actual_start: Optional[datetime] = None
    actual_finish: Optional[datetime] = None
    remaining_hours: Optional[float] = None
    baseline_start: Optional[datetime] = None
    baseline_finish: Optional[datetime] = None
    baseline_duration_hours: Optional[float] = None


def build_dataset(rows: Sequence[Dict], schedule, fuel, specs: Sequence,
                  resources: Sequence[Dict],
                  baseline: Optional[Dict] = None) -> List[ReportRow]:
    """Join task rows, the computed schedule, fuel tracking and the baseline.

    ``rows``/``resources`` are the planner's stored dicts, ``schedule``/``fuel``
    are ``TimelineResult``/``FuelResult`` and ``specs`` the ``TaskSpec`` list the
    schedule was computed from (its resolved route lengths supply distance).
    """
    scheduled = {item.task_id: item for item in getattr(schedule, "tasks", [])}
    specs_by_id = {spec.task_id: spec for spec in specs or []}
    fuel_by_task = getattr(fuel, "by_task", {}) or {}
    resource_rows = {str(row.get("resource_id") or ""): row for row in resources or []}
    baseline_tasks = {
        item.get("task_id"): item
        for item in (baseline or {}).get("tasks", []) if item.get("task_id")
    }
    dataset = []
    for row in rows or []:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        spec = specs_by_id.get(task_id)
        item = scheduled.get(task_id)
        task_fuel = fuel_by_task.get(task_id)
        resource = resource_rows.get(str(row.get("resource_id") or ""), {})
        base = baseline_tasks.get(task_id) or {}
        record = ReportRow(
            task_id=task_id, name=str(row.get("name") or ""),
            operation_type=str(row.get("operation_type") or ""),
            resource_id=str(row.get("resource_id") or ""),
            resource_name=str(resource.get("name") or ""),
            is_phase=bool(spec.is_phase) if spec is not None else bool(row.get("is_phase")),
            is_milestone=bool(row.get("is_milestone")),
            fuel_mode=str(row.get("fuel_mode") or ""),
            progress_status=str(row.get("progress_status") or "not_started"),
            percent_complete=_number(row.get("percent_complete")),
            actual_start=parse_datetime(row.get("actual_start_datetime")),
            actual_finish=parse_datetime(row.get("actual_finish_datetime")),
            baseline_start=parse_datetime(base.get("start")),
            baseline_finish=parse_datetime(base.get("finish")),
        )
        if row.get("remaining_duration_hours") not in (None, ""):
            record.remaining_hours = _number(row.get("remaining_duration_hours"))
        if base:
            record.baseline_duration_hours = _number(base.get("duration_hours"))
        if item is not None:
            record.start, record.finish = item.start, item.finish
            record.duration_hours = _number(item.duration_hours)
        else:
            record.duration_hours = _number(row.get("duration_hours"))
        if spec is not None and spec.route_length_m:
            record.distance_m = max(0.0, _number(spec.route_length_m))
        if task_fuel is not None:
            record.fuel_burn = task_fuel.burn
            record.fuel_bunker = task_fuel.bunker
            record.rob_start, record.rob_end = task_fuel.rob_start, task_fuel.rob_end
            record.fuel_cost = task_fuel.burn * max(
                0.0, _number(resource.get("fuel_cost_per_unit")))
        dataset.append(record)
    return dataset


# (key, label, unit) triples the aggregate report offers. "count" is unitless.
MEASURES = (
    ("duration_hours", "Duration", "h"),
    ("distance_km", "Distance", "km"),
    ("fuel_burn", "Fuel burned", ""),
    ("fuel_cost", "Fuel cost", ""),
    ("count", "Task count", ""),
)

GROUP_KEYS = (
    ("operation_type", "Operation type"),
    ("resource", "Resource"),
    ("progress_status", "Progress status"),
    ("fuel_mode", "Fuel mode"),
)


def _measure_value(record: ReportRow, measure: str) -> float:
    if measure == "duration_hours":
        return record.duration_hours
    if measure == "distance_km":
        return record.distance_m / 1000.0
    if measure == "fuel_burn":
        return record.fuel_burn
    if measure == "fuel_cost":
        return record.fuel_cost
    if measure == "count":
        return 1.0
    return 0.0


def _group_label(record: ReportRow, group_key: str, labels: Dict[str, str]) -> str:
    if group_key == "resource":
        return record.resource_name or "(no resource)"
    raw = getattr(record, group_key, "") or ""
    return labels.get(raw, raw) if labels else raw


def aggregate(dataset: Sequence[ReportRow], measure: str = "duration_hours",
              group_key: str = "operation_type",
              labels: Optional[Dict[str, str]] = None) -> List[Tuple[str, float]]:
    """Sum ``measure`` over non-phase tasks grouped by ``group_key``.

    ``labels`` maps raw codes (operation type, progress status) to display
    names; an unmapped or empty code falls back to the raw value. Returns
    (label, total) pairs sorted largest first, zero-total groups dropped.
    """
    totals: Dict[str, float] = {}
    for record in dataset:
        if record.is_phase:
            continue
        value = _measure_value(record, measure)
        if value <= 0.0:
            continue
        label = _group_label(record, group_key, labels or {})
        if group_key == "operation_type" and not (record.operation_type or "").strip():
            label = (labels or {}).get("", "") or "(unspecified)"
        totals[label] = totals.get(label, 0.0) + value
    return sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))


@dataclass
class FuelSeries:
    resource_id: str
    resource_name: str = ""
    unit: str = "t"
    color_hex: str = ""
    # Piecewise-linear ROB: task boundary points, so burn slopes through each
    # task and idle gaps stay flat. Bunkers appear as upward jumps at a finish.
    points: List[Tuple[datetime, float]] = field(default_factory=list)


def fuel_series(dataset: Sequence[ReportRow],
                resources: Sequence[Dict]) -> List[FuelSeries]:
    """Per-resource ROB-vs-time polylines from the per-task fuel tracking."""
    resource_rows = {str(row.get("resource_id") or ""): row for row in resources or []}
    by_resource: Dict[str, List[ReportRow]] = {}
    for record in dataset:
        if record.is_phase or record.start is None or record.finish is None:
            continue
        by_resource.setdefault(record.resource_id, []).append(record)
    series = []
    for resource_id, records in by_resource.items():
        if not any(rec.fuel_burn or rec.fuel_bunker or rec.rob_start or rec.rob_end
                   for rec in records):
            continue
        resource = resource_rows.get(resource_id, {})
        item = FuelSeries(
            resource_id=resource_id,
            resource_name=str(resource.get("name") or "") or "(no resource)",
            unit=str(resource.get("fuel_unit") or "t"),
            color_hex=str(resource.get("color_hex") or ""))
        for rec in sorted(records, key=lambda rec: (rec.start, rec.task_id)):
            item.points.append((rec.start, rec.rob_start))
            # The engine credits bunkers at task finish; keep the burn slope
            # honest by showing the pre-bunker low before the jump up.
            if rec.fuel_bunker > 0.0:
                item.points.append((rec.finish, rec.rob_end - rec.fuel_bunker))
            item.points.append((rec.finish, rec.rob_end))
        series.append(item)
    series.sort(key=lambda item: item.resource_name)
    return series


@dataclass
class SCurveResult:
    # Each curve is cumulative % complete (duration-weighted) against time.
    planned: List[Tuple[datetime, float]] = field(default_factory=list)
    baseline: List[Tuple[datetime, float]] = field(default_factory=list)
    actual: List[Tuple[datetime, float]] = field(default_factory=list)
    planned_pct_now: float = 0.0
    earned_pct_now: float = 0.0
    spi: Optional[float] = None


def _cumulative_curve(pairs: List[Tuple[datetime, float]],
                      start: Optional[datetime]) -> List[Tuple[datetime, float]]:
    """(finish, weight) pairs -> cumulative % curve, anchored at 0%."""
    total = sum(weight for _when, weight in pairs)
    if total <= 0.0 or not pairs:
        return []
    pairs = sorted(pairs, key=lambda pair: pair[0])
    curve = [(start or pairs[0][0], 0.0)]
    running = 0.0
    for when, weight in pairs:
        running += weight
        pct = running / total * 100.0
        if curve and curve[-1][0] == when:
            curve[-1] = (when, pct)
        else:
            curve.append((when, pct))
    return curve


def _interpolate(curve: List[Tuple[datetime, float]], when: datetime) -> float:
    if not curve:
        return 0.0
    if when <= curve[0][0]:
        return curve[0][1]
    for (t0, v0), (t1, v1) in zip(curve, curve[1:]):
        if t0 <= when <= t1:
            span = (t1 - t0).total_seconds()
            if span <= 0:
                return v1
            return v0 + (v1 - v0) * (when - t0).total_seconds() / span
    return curve[-1][1]


def s_curve(dataset: Sequence[ReportRow],
            now: Optional[datetime] = None) -> SCurveResult:
    """Planned / baseline / actual progress S-curves, weighted by duration.

    The actual curve steps through completed tasks at their recorded actual
    finish, then closes with the total earned value (including partially
    complete tasks) at ``now`` — an honest "where we are" point without
    needing a progress history.
    """
    result = SCurveResult()
    work = [rec for rec in dataset if not rec.is_phase]
    plan_start = min((rec.start for rec in work if rec.start is not None),
                     default=None)
    result.planned = _cumulative_curve(
        [(rec.finish, rec.duration_hours) for rec in work
         if rec.finish is not None and rec.duration_hours > 0.0], plan_start)
    base_start = min((rec.baseline_start for rec in work
                      if rec.baseline_start is not None), default=None)
    result.baseline = _cumulative_curve(
        [(rec.baseline_finish,
          rec.baseline_duration_hours if rec.baseline_duration_hours
          else rec.duration_hours)
         for rec in work if rec.baseline_finish is not None], base_start)

    total = sum(rec.duration_hours for rec in work if rec.duration_hours > 0.0)
    if total > 0.0:
        earned = sum(
            rec.duration_hours * min(100.0, max(0.0, rec.percent_complete)) / 100.0
            for rec in work if rec.duration_hours > 0.0)
        completed = [
            (rec.actual_finish, rec.duration_hours) for rec in work
            if rec.duration_hours > 0.0 and rec.actual_finish is not None
            and (rec.progress_status == "completed" or rec.percent_complete >= 100.0)]
        curve = []
        if completed:
            completed.sort(key=lambda pair: pair[0])
            start = min((rec.actual_start for rec in work
                         if rec.actual_start is not None),
                        default=completed[0][0])
            running = 0.0
            curve = [(start, 0.0)]
            for when, weight in completed:
                running += weight
                pct = running / total * 100.0
                if curve[-1][0] == when:
                    curve[-1] = (when, pct)
                else:
                    curve.append((when, pct))
        if now is not None and earned > 0.0:
            pct_now = earned / total * 100.0
            if not curve:
                anchor = plan_start or now
                curve = [(anchor, 0.0)] if anchor < now else []
            if not curve or now >= curve[-1][0]:
                curve.append((now, pct_now))
        result.actual = curve
        if now is not None:
            result.planned_pct_now = _interpolate(result.planned, now)
            result.earned_pct_now = earned / total * 100.0
            if result.planned_pct_now > 0.0:
                result.spi = result.earned_pct_now / result.planned_pct_now
    return result


@dataclass
class VarianceRow:
    task_id: str
    name: str
    baseline_finish: datetime
    forecast_finish: datetime
    variance_hours: float
    is_actual: bool  # True when the finish is recorded, not forecast


def variance_rows(dataset: Sequence[ReportRow]) -> List[VarianceRow]:
    """Finish variance vs baseline per task, worst slippage first.

    Uses the recorded actual finish when present, otherwise the currently
    scheduled (forecast) finish. Tasks without a baseline are skipped.
    """
    out = []
    for rec in dataset:
        if rec.is_phase or rec.baseline_finish is None:
            continue
        finish = rec.actual_finish or rec.finish
        if finish is None:
            continue
        variance = (finish - rec.baseline_finish).total_seconds() / 3600.0
        out.append(VarianceRow(
            task_id=rec.task_id, name=rec.name or rec.task_id,
            baseline_finish=rec.baseline_finish, forecast_finish=finish,
            variance_hours=variance, is_actual=rec.actual_finish is not None))
    out.sort(key=lambda row: (-abs(row.variance_hours), row.name))
    return out
