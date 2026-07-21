# -*- coding: utf-8 -*-
"""Build tab-delimited MS Project Entry-table clipboard text."""

from __future__ import annotations

from typing import Dict, Sequence

MSP_DATE_FMT = "%d/%m/%Y %H:%M"


def _clean(value) -> str:
    return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def build_msp_tsv(scheduled_tasks: Sequence, specs_by_id: Dict[str, object],
                  resources_by_id: Dict[str, object] = None, first_row: int = 1,
                  elapsed: bool = True) -> str:
    """Return rows in Name/Duration/Start/Finish/Predecessors/Resources order."""
    resources_by_id = resources_by_id or {}
    row_numbers = {task.task_id: int(first_row) + int(task.row) - 1
                   for task in scheduled_tasks}
    suffix = " ehrs" if elapsed else " hrs"
    lines = []
    for task in scheduled_tasks:
        spec = specs_by_id.get(task.task_id)
        name = _get(spec, "name", task.task_id)
        predecessor = _get(spec, "predecessor_task_id", "")
        dependency_type = str(_get(spec, "dependency_type", "FS") or "FS").upper()
        try:
            lag_hours = float(_get(spec, "lag_hours", 0.0) or 0.0)
        except (TypeError, ValueError):
            lag_hours = 0.0
        resource_id = _get(spec, "resource_id", task.resource_id)
        resource = resources_by_id.get(resource_id)
        resource_name = _get(resource, "name", resource_id)
        duration = ("%.6f" % float(task.duration_hours)).rstrip("0").rstrip(".") or "0"
        predecessor_text = ""
        if predecessor in row_numbers:
            predecessor_text = str(row_numbers[predecessor])
            if dependency_type != "FS":
                predecessor_text += dependency_type
            if abs(lag_hours) > 1e-9:
                predecessor_text += "%+.6gh" % lag_hours
        columns = [
            _clean(name), duration + suffix, task.start.strftime(MSP_DATE_FMT),
            task.finish.strftime(MSP_DATE_FMT),
            predecessor_text,
            _clean(resource_name),
        ]
        lines.append("\t".join(columns))
    return "\n".join(lines)


def _get(obj, key, default=""):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)
