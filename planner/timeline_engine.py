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


@dataclass
class ScheduledTask:
    task_id: str
    row: int
    start: datetime
    finish: datetime
    duration_hours: float
    resource_id: str = ""
    warning: str = ""


@dataclass
class TimelineResult:
    tasks: List[ScheduledTask] = field(default_factory=list)
    by_resource: Dict[str, List[ScheduledTask]] = field(default_factory=dict)
    span_start: Optional[datetime] = None
    span_end: Optional[datetime] = None
    errors: List[str] = field(default_factory=list)


@dataclass
class ActiveState:
    task_id: str
    fraction: float
    chainage_m: Optional[float]
    active: bool = True


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _duration(spec: TaskSpec):
    if spec.duration_mode == "computed":
        length = _number(spec.route_length_m, -1.0)
        speed = _number(spec.speed_knots)
        if spec.geom_kind == "line" and length >= 0.0 and speed > 0.0:
            return length / (speed * KNOT_MPS) / 3600.0, ""
        return max(0.0, _number(spec.duration_hours)), (
            "Computed duration needs a linked line and a speed; using manual duration."
        )
    return max(0.0, _number(spec.duration_hours)), ""


def compute_schedule(anchor: datetime, tasks: Sequence[TaskSpec],
                     resource_start_offsets: Optional[Dict[str, float]] = None) -> TimelineResult:
    """Schedule operational tasks and derive indented summary-row spans.

    Resources run concurrently, each no earlier than its configured offset from
    the scenario anchor. Cross-resource finish-to-start links and lag are then
    applied in the same dependency graph as same-resource links.
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
    lane_finish = {}
    resource_start_offsets = dict(resource_start_offsets or {})
    previous = None
    row_by_id = {item.task_id: index + 1 for index, item in enumerate(specs)}
    for item in ordered:
        hours, warning = _duration(item)
        lane_key = item.resource_id or "__unassigned__"
        start = anchor + timedelta(hours=max(
            0.0, _number(resource_start_offsets.get(item.resource_id, 0.0))))
        if cycle and previous is not None:
            start = max(start, previous.finish)
        elif item.predecessor_task_id in scheduled:
            start = max(
                start,
                scheduled[item.predecessor_task_id].finish + timedelta(
                    hours=_number(item.lag_hours)),
            )
        if lane_key in lane_finish:
            start = max(start, lane_finish[lane_key])
        if item.task_id in missing:
            warning = (warning + " " if warning else "") + "Missing predecessor; anchored normally."
        finish = start + timedelta(hours=hours)
        scheduled_task = ScheduledTask(
            item.task_id, row_by_id[item.task_id], start, finish, hours,
            item.resource_id, warning,
        )
        scheduled[item.task_id] = scheduled_task
        lane_finish[lane_key] = finish
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
    for task in sorted(scheduled.values(), key=lambda item: item.row):
        result.by_resource.setdefault(task.resource_id, []).append(task)
    result.span_start = min([anchor] + [item.start for item in result.tasks])
    result.span_end = max([anchor] + [item.finish for item in result.tasks])
    return result


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
