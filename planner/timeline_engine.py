# -*- coding: utf-8 -*-
"""Qt-free Planner scheduling and simulation clock calculations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import heapq
from typing import Dict, List, Optional, Sequence

KNOT_MPS = 0.514444


@dataclass
class TaskSpec:
    task_id: str
    seq: int
    name: str = ""
    resource_id: str = ""
    duration_mode: str = "manual"
    duration_hours: float = 0.0
    predecessor_task_id: str = ""
    lag_hours: float = 0.0
    speed_knots: float = 0.0
    direction: str = "forward"
    geom_kind: str = ""
    route_length_m: Optional[float] = None
    is_phase: bool = False
    outline_level: int = 0
    fuel_mode: str = ""
    bunker_amount: float = 0.0
    dependency_type: str = "FS"
    constraint_type: str = ""
    constraint_datetime: object = None
    is_milestone: bool = False
    location_key: str = ""


@dataclass
class ScheduledTask:
    task_id: str
    row: int
    start: datetime
    finish: datetime
    duration_hours: float
    resource_id: str = ""
    warning: str = ""
    total_float_hours: float = 0.0
    critical: bool = False


@dataclass
class TimelineResult:
    tasks: List[ScheduledTask] = field(default_factory=list)
    by_resource: Dict[str, List[ScheduledTask]] = field(default_factory=dict)
    span_start: Optional[datetime] = None
    span_end: Optional[datetime] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class ActiveState:
    task_id: str
    fraction: float
    chainage_m: Optional[float]
    active: bool = True


# Maps a task's fuel mode to the per-24 h rate field on its resource row.
FUEL_RATE_FIELDS = {
    "transit": "fuel_rate_transit", "dp": "fuel_rate_dp",
    "anchor": "fuel_rate_anchor", "port": "fuel_rate_port",
}


@dataclass
class TaskFuel:
    task_id: str
    burn: float = 0.0
    bunker: float = 0.0
    rob_start: float = 0.0
    rob_end: float = 0.0


@dataclass
class ResourceFuel:
    resource_id: str
    unit: str = "t"
    rob_start: float = 0.0
    total_burn: float = 0.0
    total_bunker: float = 0.0
    rob_end: float = 0.0
    min_rob: float = 0.0
    cost: float = 0.0
    warnings: List[str] = field(default_factory=list)


@dataclass
class FuelResult:
    by_task: Dict[str, TaskFuel] = field(default_factory=dict)
    by_resource: Dict[str, ResourceFuel] = field(default_factory=dict)


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _duration(spec: TaskSpec):
    if spec.is_milestone:
        return 0.0, ""
    if spec.duration_mode == "computed":
        length = _number(spec.route_length_m, -1.0)
        speed = _number(spec.speed_knots)
        if spec.geom_kind == "line" and length >= 0.0 and speed > 0.0:
            return length / (speed * KNOT_MPS) / 3600.0, ""
        return max(0.0, _number(spec.duration_hours)), (
            "Computed duration needs a linked line and a speed; using manual duration."
        )
    return max(0.0, _number(spec.duration_hours)), ""


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _dependency_start(spec, hours, predecessor):
    """Earliest task start imposed by one predecessor relation."""
    lag = timedelta(hours=_number(spec.lag_hours))
    relation = str(spec.dependency_type or "FS").upper()
    if relation == "SS":
        return predecessor.start + lag
    if relation == "FF":
        return predecessor.finish + lag - timedelta(hours=hours)
    if relation == "SF":
        return predecessor.start + lag - timedelta(hours=hours)
    return predecessor.finish + lag


def _predecessor_finish(successor_spec, predecessor_hours, successor):
    """Latest predecessor finish imposed by one successor relation."""
    lag = timedelta(hours=_number(successor_spec.lag_hours))
    relation = str(successor_spec.dependency_type or "FS").upper()
    if relation == "SS":
        return successor.start - lag + timedelta(hours=predecessor_hours)
    if relation == "FF":
        return successor.finish - lag
    if relation == "SF":
        return successor.finish - lag + timedelta(hours=predecessor_hours)
    return successor.start - lag


def _warning_text(existing, message):
    return ((existing + " ") if existing else "") + message


def compute_schedule(anchor: datetime, tasks: Sequence[TaskSpec],
                     resource_start_offsets: Optional[Dict[str, float]] = None,
                     schedule_mode: str = "forward",
                     resource_start_datetimes: Optional[Dict[str, object]] = None) -> TimelineResult:
    """Schedule operational tasks and derive indented summary-row spans.

    In ``forward`` mode resources run concurrently, each no earlier than its
    configured offset from the scenario start.  In ``backward`` mode the anchor
    is a required plan finish and tasks are placed as late as their successor,
    resource-lane and finish-to-start constraints allow.  Resource start
    offsets only apply to forward schedules.
    """
    specs = sorted(list(tasks), key=lambda item: (int(item.seq), item.task_id))
    result = TimelineResult(span_start=anchor, span_end=anchor)
    if not specs:
        return result
    work_specs = [item for item in specs if not item.is_phase]
    by_id = {item.task_id: item for item in work_specs}
    indegree = {item.task_id: 0 for item in work_specs}
    children = {item.task_id: [] for item in work_specs}
    missing = set()
    for item in work_specs:
        predecessor = item.predecessor_task_id
        if predecessor and predecessor in by_id:
            indegree[item.task_id] += 1
            children[predecessor].append(item.task_id)
        elif predecessor:
            missing.add(item.task_id)
            result.errors.append("Task '%s' has a missing predecessor." % (item.name or item.task_id))
    heap = [(int(item.seq), item.task_id) for item in work_specs if indegree[item.task_id] == 0]
    heapq.heapify(heap)
    ordered = []
    while heap:
        _, task_id = heapq.heappop(heap)
        ordered.append(by_id[task_id])
        for child in children[task_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(heap, (int(by_id[child].seq), child))
    cycle = len(ordered) != len(work_specs)
    if cycle:
        result.errors.append("Predecessor cycle detected; tasks were chained in plan order.")
        ordered = work_specs

    scheduled = {}
    resource_start_offsets = dict(resource_start_offsets or {})
    resource_start_datetimes = {
        str(key): parsed for key, value in dict(resource_start_datetimes or {}).items()
        for parsed in [_as_datetime(value)] if parsed is not None
    }
    durations = {item.task_id: _duration(item)[0] for item in work_specs}
    row_by_id = {item.task_id: index + 1 for index, item in enumerate(specs)}
    if str(schedule_mode or "forward").lower() == "backward":
        # Reverse topological order makes every explicit successor available
        # before its predecessor is positioned.  The lane cursor supplies the
        # same implicit resource sequencing used by the forward pass.
        lane_start = {}
        successor_ids = {item.task_id: [] for item in work_specs}
        for successor in work_specs:
            if successor.predecessor_task_id in successor_ids:
                successor_ids[successor.predecessor_task_id].append(successor.task_id)
        next_scheduled = None
        for item in reversed(ordered):
            hours, warning = _duration(item)
            lane_key = item.resource_id or "__unassigned__"
            finish = anchor
            if cycle and next_scheduled is not None:
                finish = min(finish, next_scheduled.start)
            else:
                for successor_id in successor_ids.get(item.task_id, []):
                    successor = scheduled.get(successor_id)
                    if successor is None:
                        continue
                    successor_spec = by_id[successor_id]
                    finish = min(
                        finish,
                        _predecessor_finish(successor_spec, hours, successor),
                    )
            if lane_key in lane_start:
                finish = min(finish, lane_start[lane_key])
            if item.task_id in missing:
                warning = ((warning + " ") if warning else "") + (
                    "Missing predecessor; scheduled to the required finish normally.")
            constraint = _as_datetime(item.constraint_datetime)
            constraint_type = str(item.constraint_type or "").lower()
            if constraint is not None and constraint_type == "fnlt":
                finish = min(finish, constraint)
            elif constraint is not None and constraint_type == "mfo":
                if finish < constraint:
                    warning = _warning_text(
                        warning, "Must-finish constraint conflicts with a successor/deadline.")
                finish = constraint
            start = finish - timedelta(hours=hours)
            if constraint is not None and constraint_type == "snet" and start < constraint:
                warning = _warning_text(
                    warning, "Start-no-earlier constraint conflicts with a successor/deadline.")
                start = constraint
                finish = start + timedelta(hours=hours)
            elif constraint is not None and constraint_type == "mso":
                if start != constraint:
                    warning = _warning_text(
                        warning, "Must-start constraint overrides the calculated late start.")
                start = constraint
                finish = start + timedelta(hours=hours)
            scheduled_task = ScheduledTask(
                item.task_id, row_by_id[item.task_id], start, finish, hours,
                item.resource_id, warning,
            )
            scheduled[item.task_id] = scheduled_task
            lane_start[lane_key] = min(start, lane_start.get(lane_key, start))
            next_scheduled = scheduled_task
    else:
        lane_finish = {}
        previous = None
        for item in ordered:
            hours, warning = _duration(item)
            lane_key = item.resource_id or "__unassigned__"
            start = resource_start_datetimes.get(str(item.resource_id or ""))
            if start is None:
                start = anchor + timedelta(hours=max(
                    0.0, _number(resource_start_offsets.get(item.resource_id, 0.0))))
            if cycle and previous is not None:
                start = max(start, previous.finish)
            elif item.predecessor_task_id in scheduled:
                start = max(start, _dependency_start(
                    item, hours, scheduled[item.predecessor_task_id]))
            if lane_key in lane_finish:
                start = max(start, lane_finish[lane_key])
            if item.task_id in missing:
                warning = ((warning + " ") if warning else "") + (
                    "Missing predecessor; anchored normally.")
            constraint = _as_datetime(item.constraint_datetime)
            constraint_type = str(item.constraint_type or "").lower()
            if constraint is not None and constraint_type == "snet":
                start = max(start, constraint)
            elif constraint is not None and constraint_type == "mso":
                if start > constraint:
                    warning = _warning_text(
                        warning, "Must-start constraint conflicts with a predecessor/resource.")
                start = constraint
            finish = start + timedelta(hours=hours)
            if constraint is not None and constraint_type == "mfo":
                target_start = constraint - timedelta(hours=hours)
                if start > target_start:
                    warning = _warning_text(
                        warning, "Must-finish constraint conflicts with a predecessor/resource.")
                start, finish = target_start, constraint
            elif constraint is not None and constraint_type == "fnlt" and finish > constraint:
                warning = _warning_text(
                    warning, "Finish-no-later constraint is missed by the calculated schedule.")
            scheduled_task = ScheduledTask(
                item.task_id, row_by_id[item.task_id], start, finish, hours,
                item.resource_id, warning,
            )
            scheduled[item.task_id] = scheduled_task
            lane_finish[lane_key] = max(finish, lane_finish.get(lane_key, finish))
            previous = scheduled_task

    all_scheduled = dict(scheduled)
    for index, summary in enumerate(specs):
        if not summary.is_phase:
            continue
        descendants = []
        summary_level = int(summary.outline_level or 0)
        for candidate in specs[index + 1:]:
            if int(candidate.outline_level or 0) <= summary_level:
                break
            if not candidate.is_phase and candidate.task_id in scheduled:
                descendants.append(scheduled[candidate.task_id])
        if descendants:
            start = min(item.start for item in descendants)
            finish = max(item.finish for item in descendants)
            warning = ""
        else:
            start = finish = anchor
            warning = "Summary row has no operational tasks."
        all_scheduled[summary.task_id] = ScheduledTask(
            summary.task_id, row_by_id[summary.task_id], start, finish,
            (finish - start).total_seconds() / 3600.0, "", warning)

    result.tasks = sorted(all_scheduled.values(), key=lambda item: item.row)
    for task in sorted(scheduled.values(), key=lambda item: (item.start, item.row)):
        result.by_resource.setdefault(task.resource_id, []).append(task)
    result.span_start = min([anchor] + [item.start for item in result.tasks])
    result.span_end = max([anchor] + [item.finish for item in result.tasks])
    if str(schedule_mode or "forward").lower() == "backward":
        for task in scheduled.values():
            available = resource_start_datetimes.get(str(task.resource_id or ""))
            if available is not None and task.start < available:
                message = "Task '%s' starts before its resource is available." % (
                    by_id[task.task_id].name or task.task_id)
                task.warning = _warning_text(task.warning, message)
    for task in scheduled.values():
        if task.warning:
            result.warnings.append("%s: %s" % (
                by_id[task.task_id].name or task.task_id, task.warning))
    _detect_schedule_conflicts(result, scheduled, by_id)
    _apply_total_float(result, scheduled, by_id, durations)
    for index, summary in enumerate(specs):
        if not summary.is_phase:
            continue
        descendants = []
        level = int(summary.outline_level or 0)
        for candidate in specs[index + 1:]:
            if int(candidate.outline_level or 0) <= level:
                break
            if not candidate.is_phase and candidate.task_id in scheduled:
                descendants.append(scheduled[candidate.task_id])
        target = all_scheduled.get(summary.task_id)
        if target is not None and descendants:
            target.total_float_hours = min(item.total_float_hours for item in descendants)
            target.critical = any(item.critical for item in descendants)
    return result


def _apply_total_float(result, scheduled, specs_by_id, durations):
    """Calculate CPM-style total float over explicit links and resource lanes."""
    if not scheduled or result.span_end is None:
        return
    edges = []
    explicit = set()
    for successor in specs_by_id.values():
        predecessor = successor.predecessor_task_id
        if predecessor in scheduled and successor.task_id in scheduled:
            edge = (predecessor, successor.task_id,
                    str(successor.dependency_type or "FS").upper(),
                    _number(successor.lag_hours))
            edges.append(edge)
            explicit.add((predecessor, successor.task_id))
    lanes = {}
    for task in scheduled.values():
        lanes.setdefault(task.resource_id or "__unassigned__", []).append(task)
    for lane in lanes.values():
        lane.sort(key=lambda item: (item.start, item.row))
        for predecessor, successor in zip(lane, lane[1:]):
            if (predecessor.task_id, successor.task_id) not in explicit:
                edges.append((predecessor.task_id, successor.task_id, "FS", 0.0))

    latest = {
        task_id: result.span_end - timedelta(hours=durations.get(task_id, 0.0))
        for task_id in scheduled
    }
    for task_id, spec in specs_by_id.items():
        constraint = _as_datetime(spec.constraint_datetime)
        if constraint is None:
            continue
        kind = str(spec.constraint_type or "").lower()
        if kind in ("fnlt", "mfo"):
            latest[task_id] = min(
                latest[task_id], constraint - timedelta(hours=durations.get(task_id, 0.0)))
        elif kind == "mso":
            latest[task_id] = min(latest[task_id], constraint)

    for _pass in range(max(1, len(scheduled))):
        changed = False
        for predecessor_id, successor_id, relation, lag_hours in edges:
            successor_start = latest[successor_id]
            predecessor_hours = durations.get(predecessor_id, 0.0)
            successor_hours = durations.get(successor_id, 0.0)
            lag = timedelta(hours=lag_hours)
            if relation == "SS":
                allowed = successor_start - lag
            elif relation == "FF":
                allowed = (successor_start + timedelta(hours=successor_hours)
                           - lag - timedelta(hours=predecessor_hours))
            elif relation == "SF":
                allowed = successor_start + timedelta(hours=successor_hours) - lag
            else:
                allowed = successor_start - lag - timedelta(hours=predecessor_hours)
            if allowed < latest[predecessor_id]:
                latest[predecessor_id] = allowed
                changed = True
        if not changed:
            break
    for task_id, task in scheduled.items():
        value = max(0.0, (latest[task_id] - task.start).total_seconds() / 3600.0)
        task.total_float_hours = value
        task.critical = value <= 1e-6


def _detect_schedule_conflicts(result, scheduled, specs_by_id):
    """Report resource overlaps and simultaneous work at the same location."""
    tasks = sorted(scheduled.values(), key=lambda item: (item.start, item.row))
    for index, first in enumerate(tasks):
        if first.finish <= first.start:
            continue
        for second in tasks[index + 1:]:
            if second.start >= first.finish:
                break
            if second.finish <= second.start:
                continue
            first_spec = specs_by_id[first.task_id]
            second_spec = specs_by_id[second.task_id]
            if first.resource_id == second.resource_id:
                result.warnings.append(
                    "Resource conflict: '%s' overlaps '%s'." % (
                        first_spec.name or first.task_id,
                        second_spec.name or second.task_id))
            elif (first_spec.location_key and
                  first_spec.location_key == second_spec.location_key):
                result.warnings.append(
                    "SIMOPS review: '%s' and '%s' overlap at the same linked location." % (
                        first_spec.name or first.task_id,
                        second_spec.name or second.task_id))


def compute_fuel(result: TimelineResult, specs_by_id: Dict[str, TaskSpec],
                 resources: Sequence[Dict]) -> FuelResult:
    """Track remaining-on-board fuel along each resource lane.

    Each scheduled task burns its resource's per-24 h rate for the task's fuel
    mode over the scheduled duration; a task's bunker amount is credited at the
    task finish. Idle gaps between tasks burn nothing.
    """
    fuel = FuelResult()
    rows_by_id = {str(row.get("resource_id") or ""): row for row in resources}
    for resource_id, lane in result.by_resource.items():
        resource = rows_by_id.get(str(resource_id or ""))
        if resource is None:
            continue
        rob = _number(resource.get("fuel_start"))
        summary = ResourceFuel(
            resource_id=resource_id, unit=str(resource.get("fuel_unit") or "t"),
            rob_start=rob, rob_end=rob, min_rob=rob)
        for task in sorted(lane, key=lambda item: (item.start, item.row)):
            spec = specs_by_id.get(task.task_id)
            mode = (spec.fuel_mode or "") if spec is not None else ""
            rate = _number(resource.get(FUEL_RATE_FIELDS.get(mode, ""), 0.0))
            burn = max(0.0, rate) / 24.0 * max(0.0, _number(task.duration_hours))
            bunker = max(0.0, _number(spec.bunker_amount)) if spec is not None else 0.0
            rob_start = rob
            rob = rob - burn + bunker
            summary.min_rob = min(summary.min_rob, rob_start - burn)
            summary.total_burn += burn
            summary.total_bunker += bunker
            fuel.by_task[task.task_id] = TaskFuel(task.task_id, burn, bunker, rob_start, rob)
            if rob_start - burn < -1e-9 and rob_start >= -1e-9:
                name = (spec.name if spec is not None else "") or task.task_id
                summary.warnings.append("Fuel runs out during '%s' (%s)." % (
                    name, task.start.strftime("%d/%m/%Y %H:%M")))
        summary.rob_end = rob
        summary.cost = summary.total_burn * max(0.0, _number(resource.get("fuel_cost_per_unit")))
        fuel.by_resource[resource_id] = summary
    return fuel


def position_at(result: TimelineResult, specs_by_id: Dict[str, TaskSpec],
                when: datetime) -> Dict[str, ActiveState]:
    """Return the current/held state of each resource at ``when``."""
    states = {}
    for resource_id, lane in result.by_resource.items():
        previous = None
        chosen = None
        is_active = False
        for task in lane:
            if task.start <= when < task.finish:
                chosen = task
                is_active = True
                break
            if task.finish < when:
                previous = task
            elif task.start > when:
                break
        if chosen is None:
            chosen = previous
        if chosen is None:
            continue
        duration = max(0.0, (chosen.finish - chosen.start).total_seconds())
        fraction = 1.0 if not is_active else (
            1.0 if duration == 0.0 else (when - chosen.start).total_seconds() / duration
        )
        fraction = min(1.0, max(0.0, fraction))
        spec = specs_by_id.get(chosen.task_id)
        length = None if spec is None else spec.route_length_m
        chainage = None
        if spec is not None and spec.geom_kind == "line" and length is not None:
            chainage = float(length) * (1.0 - fraction if spec.direction == "reverse" else fraction)
            chainage = min(float(length), max(0.0, chainage))
        states[resource_id] = ActiveState(chosen.task_id, fraction, chainage, is_active)
    return states
