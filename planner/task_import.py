# -*- coding: utf-8 -*-
"""Parse pasted MS Project rows or CSV text into planner task rows.

MS Project copies Entry-table rows to the clipboard as tab-separated text
(typically Name / Duration / Start / Finish / Predecessors / Resource Names,
matching this plugin's "Copy for MS Project" export). CSV files exported from
Project, Excel, or other planning tools are also accepted. Columns are mapped
to task roles by header aliases when a header row is present, otherwise by
content heuristics; the import dialog lets the user correct the mapping.

This module is deliberately QGIS-free so it can be tested with plain Python.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Dict, List, Optional, Sequence, Tuple

from . import operation_types, standard_tasks

# Column roles the importer understands. Start/finish dates are recognised so
# they can be skipped explicitly (the planner recomputes the schedule from the
# anchor, durations, and dependencies).
ROLES = ("ignore", "name", "duration", "outline", "predecessor", "resource",
         "operation", "speed", "description", "notes", "start", "finish")

ROLE_LABELS = {
    "ignore": "(ignore)", "name": "Task name", "duration": "Duration",
    "outline": "Outline level", "predecessor": "Predecessors",
    "resource": "Resource", "operation": "Operation type",
    "speed": "Speed (kn)", "description": "Description", "notes": "Notes",
    "start": "Start (ignored)", "finish": "Finish (ignored)",
}

_HEADER_ALIASES = {
    "name": "name", "task name": "name", "task": "name", "activity": "name",
    "task_name": "name",
    "duration": "duration", "duration_hours": "duration", "dur": "duration",
    "duration (hrs)": "duration", "duration (hours)": "duration",
    "hours": "duration",
    "outline level": "outline", "outline_level": "outline", "level": "outline",
    "predecessors": "predecessor", "predecessor": "predecessor",
    "pred": "predecessor", "predecessor_task_id": "predecessor",
    "resource names": "resource", "resource name": "resource",
    "resource": "resource", "resources": "resource", "vessel": "resource",
    "resource_names": "resource",
    "operation": "operation", "operation type": "operation",
    "operation_type": "operation",
    "speed": "speed", "speed_knots": "speed", "speed (kn)": "speed",
    "speed (knots)": "speed",
    "description": "description",
    "notes": "notes", "comment": "notes", "comments": "notes",
    "remarks": "notes",
    "start": "start", "start date": "start", "start_datetime": "start",
    "finish": "finish", "finish date": "finish", "end": "finish",
    "end date": "finish",
}

# MS Project duration units (optionally elapsed: "edays", "ehrs", …) mapped to
# hours. The planner schedules continuously, so a day is 24 h and a week 168 h.
_DURATION_UNIT_HOURS = {
    "m": 1.0 / 60.0, "min": 1.0 / 60.0, "mins": 1.0 / 60.0,
    "minute": 1.0 / 60.0, "minutes": 1.0 / 60.0,
    "h": 1.0, "hr": 1.0, "hrs": 1.0, "hour": 1.0, "hours": 1.0,
    "d": 24.0, "day": 24.0, "days": 24.0,
    "w": 168.0, "wk": 168.0, "wks": 168.0, "week": 168.0, "weeks": 168.0,
    "mo": 720.0, "mon": 720.0, "mons": 720.0, "month": 720.0, "months": 720.0,
}

_DURATION_RE = re.compile(
    r"^([0-9][\d.,]*)\s*e?([a-z]+)?\.?$", re.IGNORECASE)
_PREDECESSOR_TOKEN_RE = re.compile(
    r"^(\d+)\s*(FS|SS|FF|SF)?\s*(?:([+-])\s*([0-9][\d.,]*\s*e?[a-z]*)\.?)?$",
    re.IGNORECASE)
_DATE_RE = re.compile(
    r"^(?:[A-Za-z]{2,3}\s+)?\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)?$",
    re.IGNORECASE)


def _to_float(text: str) -> Optional[float]:
    raw = str(text or "").strip().replace(" ", "")
    if not raw:
        return None
    try:
        return float(raw.replace(",", "")) if raw.count(",") else float(raw)
    except ValueError:
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            return None


def parse_duration_hours(text) -> Optional[float]:
    """Parse '36', '1.5 days', '2 ehrs', '30 mins', '1w' into hours."""
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _DURATION_RE.match(raw)
    if not match:
        return None
    value = _to_float(match.group(1))
    if value is None:
        return None
    unit = (match.group(2) or "").lower()
    if not unit:
        return value
    # A leading "e" was consumed as elapsed only when the rest is a known unit.
    factor = _DURATION_UNIT_HOURS.get(unit)
    if factor is None and unit.startswith("e"):
        factor = _DURATION_UNIT_HOURS.get(unit[1:])
    if factor is None:
        return None
    return value * factor


def parse_predecessors(text) -> Optional[List[Tuple[int, str, float]]]:
    """Parse '3', '3FS+2 days;4SS' into [(row_no, dep_type, lag_hours), …].

    Returns None when the cell does not look like a predecessor list.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    out = []
    for token in re.split(r"[;,]", raw):
        token = token.strip()
        if not token:
            continue
        match = _PREDECESSOR_TOKEN_RE.match(token)
        if not match:
            return None
        lag = 0.0
        if match.group(4):
            lag = parse_duration_hours(match.group(4)) or 0.0
            if match.group(3) == "-":
                lag = -lag
        out.append((int(match.group(1)), (match.group(2) or "FS").upper(), lag))
    return out or None


def looks_like_date(text) -> bool:
    return bool(_DATE_RE.match(str(text or "").strip()))


def split_rows(text: str) -> List[List[str]]:
    """Split pasted/loaded text into cell rows (tab first, then CSV)."""
    cleaned = str(text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line for line in cleaned.split("\n") if line.strip()]
    if not lines:
        return []
    if any("\t" in line for line in lines):
        delimiter = "\t"
    else:
        counts = {sep: sum(line.count(sep) for line in lines) for sep in (",", ";")}
        delimiter = ";" if counts[";"] > counts[","] else ","
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = [[cell.strip() for cell in row] for row in reader]
    rows = [row for row in rows if any(cell for cell in row)]
    width = max((len(row) for row in rows), default=0)
    return [row + [""] * (width - len(row)) for row in rows]


def detect_header(rows: Sequence[Sequence[str]]) -> bool:
    if not rows:
        return False
    matches = sum(1 for cell in rows[0]
                  if str(cell or "").strip().lower() in _HEADER_ALIASES)
    return matches >= 2 or (matches == 1 and len([c for c in rows[0] if c]) == 1)


def _column_cells(rows, index, skip_first):
    cells = [row[index] for row in (rows[1:] if skip_first else rows)]
    return [cell for cell in cells if str(cell or "").strip()]


def _guess_column_role(cells, assigned, known_resources, date_seen) -> str:
    if all(looks_like_date(cell) for cell in cells):
        return "finish" if date_seen else "start"
    if all(parse_duration_hours(cell) is not None for cell in cells):
        if "duration" not in assigned:
            return "duration"
        unitful = any(re.search(r"[a-zA-Z]", cell) for cell in cells)
        if "predecessor" not in assigned and not unitful:
            return "predecessor"
    if all(parse_predecessors(cell) is not None for cell in cells) \
            and "predecessor" not in assigned:
        return "predecessor"
    if known_resources and "resource" not in assigned and sum(
            1 for cell in cells if cell.strip().lower() in known_resources
            ) >= max(1, len(cells) // 2):
        return "resource"
    if "name" not in assigned:
        return "name"
    if "notes" not in assigned:
        return "notes"
    return "ignore"


def guess_roles(rows: Sequence[Sequence[str]], has_header: bool,
                resource_names: Sequence[str] = ()) -> List[str]:
    """Best-effort role per column; the dialog lets the user correct these."""
    if not rows:
        return []
    width = len(rows[0])
    roles = ["ignore"] * width
    if has_header:
        for index, cell in enumerate(rows[0]):
            roles[index] = _HEADER_ALIASES.get(
                str(cell or "").strip().lower(), "ignore")
    known_resources = {str(name or "").strip().lower()
                      for name in resource_names if str(name or "").strip()}
    for index in range(width):
        if roles[index] != "ignore":
            continue
        cells = _column_cells(rows, index, has_header)
        if not cells:
            continue
        date_seen = any(role in ("start", "finish") for role in roles)
        role = _guess_column_role(cells, roles, known_resources, date_seen)
        roles[index] = role
    # Headerless paste in this plugin's own MS Project export shape:
    # Name / Duration / Start / Finish / Predecessors / Resource Names.
    if (not has_header and width == 6 and roles[:4] ==
            ["name", "duration", "start", "finish"] and roles[5] in ("notes", "ignore")):
        roles[5] = "resource"
    return roles


def build_task_rows(rows: Sequence[Sequence[str]], roles: Sequence[str],
                    has_header: bool, resources: Sequence[Dict],
                    default_resource_id: str = "",
                    chain_missing_predecessors: bool = True,
                    first_row_id: int = 1) -> Tuple[List[Dict], List[str]]:
    """Build full planner task rows from parsed cells and a role mapping.

    ``first_row_id`` is the MS Project ID of the first imported row, used to
    resolve predecessor references relative to the pasted block. Returns
    ``(task_rows, warnings)``.
    """
    column_for = {}
    for index, role in enumerate(roles):
        column_for.setdefault(role, index)
    if "name" not in column_for:
        return [], ["Assign a Task name column before importing."]
    resource_by_name = {str(row.get("name") or "").strip().lower():
                        str(row.get("resource_id") or "") for row in resources}
    if not default_resource_id and resources:
        default_resource_id = str(resources[0].get("resource_id") or "")
    data_rows = list(rows[1:] if has_header else rows)
    warnings: List[str] = []
    unknown_resources = set()
    tasks: List[Dict] = []
    predecessor_refs: List[Optional[Tuple[int, str, float]]] = []

    def cell(row, role):
        index = column_for.get(role)
        return str(row[index] or "").strip() if index is not None and index < len(row) else ""

    for line_no, row in enumerate(data_rows, start=2 if has_header else 1):
        name = cell(row, "name")
        if not name:
            warnings.append("Row %d: skipped (no task name)." % line_no)
            continue
        duration = None
        raw_duration = cell(row, "duration")
        if raw_duration:
            duration = parse_duration_hours(raw_duration)
            if duration is None:
                warnings.append("Row %d: could not read duration '%s'; using 1 h."
                                % (line_no, raw_duration))
        resource_id = default_resource_id
        raw_resource = cell(row, "resource")
        if raw_resource:
            matched = resource_by_name.get(raw_resource.lower())
            if matched:
                resource_id = matched
            else:
                unknown_resources.add(raw_resource)
        operation = cell(row, "operation").lower()
        if operation in standard_tasks._OPERATION_ALIASES:
            operation = standard_tasks._OPERATION_ALIASES[operation]
        elif operation:
            operation = operation_types.slugify(operation)
        template = {
            "name": name, "description": cell(row, "description"),
            "operation_type": operation, "duration_hours": duration,
            "speed_knots": _to_float(cell(row, "speed")),
            "fuel_mode": "", "bunker_amount": None, "notes": cell(row, "notes"),
        }
        task = standard_tasks.template_to_task_row(
            template, resource_id, seq=len(tasks))
        raw_outline = cell(row, "outline")
        if raw_outline:
            try:
                # MS Project outline levels start at 1; the planner's at 0.
                task["outline_level"] = max(0, int(float(raw_outline)) - 1)
            except ValueError:
                warnings.append("Row %d: could not read outline level '%s'."
                                % (line_no, raw_outline))
        refs = parse_predecessors(cell(row, "predecessor")) \
            if "predecessor" in column_for else []
        if refs is None:
            warnings.append("Row %d: could not read predecessors '%s'."
                            % (line_no, cell(row, "predecessor")))
            refs = []
        if len(refs) > 1:
            warnings.append("Row %d: multiple predecessors; keeping the first "
                            "(the planner supports one per task)." % line_no)
        predecessor_refs.append(refs[0] if refs else None)
        tasks.append(task)

    for index, (task, ref) in enumerate(zip(tasks, predecessor_refs)):
        if ref is not None:
            target = ref[0] - int(first_row_id)
            if 0 <= target < len(tasks) and target != index:
                task["predecessor_task_id"] = tasks[target]["task_id"]
                task["dependency_type"] = ref[1]
                task["lag_hours"] = ref[2]
                continue
            warnings.append(
                "Task '%s': predecessor row %d is outside the imported rows "
                "(check the first-row ID)." % (task["name"], ref[0]))
        if chain_missing_predecessors and index > 0:
            task["predecessor_task_id"] = tasks[index - 1]["task_id"]

    if unknown_resources:
        warnings.append("Unmatched resource name(s) kept on the default "
                        "resource: %s. Add them in Resources… first to map "
                        "them." % ", ".join(sorted(unknown_resources)))
    return tasks, warnings
