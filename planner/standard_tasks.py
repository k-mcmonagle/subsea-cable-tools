# -*- coding: utf-8 -*-
"""User-curated standard task templates with CSV import/export.

Templates are stored per user (the dock keeps them in QSettings as JSON) so a
library curated once is available in every project, and can be shared inside an
organisation through the CSV round-trip in this module.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Dict, List, Sequence, Tuple

from . import schema

TEMPLATE_FIELDS = ("name", "description", "duration_hours", "speed_knots",
                   "fuel_mode", "bunker_amount", "notes")
_NUMBER_FIELDS = ("duration_hours", "speed_knots", "bunker_amount")
_VALID_FUEL_MODES = {value for value, _label in schema.FUEL_MODES}
_FUEL_ALIASES = {label.lower(): value for value, label in schema.FUEL_MODES}
_FUEL_ALIASES["none"] = ""


def default_templates() -> List[Dict]:
    """Starter library shown the first time the dialog opens."""
    rows = [
        ("Mobilisation", "Crew change, loading, and departure preparations",
         24.0, None, "port", None),
        ("Transit", "Transit to or between work sites", 24.0, 10.0, "transit", None),
        ("PLGR", "Pre-lay grapnel run along the route", 24.0, 0.8, "dp", None),
        ("Cable lay", "Surface lay along the route", 24.0, 1.5, "dp", None),
        ("Post-lay burial", "ROV jet burial along the route", 24.0, 0.4, "dp", None),
        ("Joint / splice", "Jointing operations on DP or at anchor", 36.0, None, "dp", None),
        ("Port call (bunker)", "Alongside for stores and bunkering", 24.0, None, "port", None),
        ("Demobilisation", "Offload and demobilise", 24.0, None, "port", None),
    ]
    return [{
        "name": name, "description": description, "duration_hours": duration,
        "speed_knots": speed, "fuel_mode": fuel_mode, "bunker_amount": bunker,
        "notes": "",
    } for name, description, duration, speed, fuel_mode, bunker in rows]


def templates_to_json(templates: Sequence[Dict]) -> str:
    return json.dumps([
        {field: template.get(field) for field in TEMPLATE_FIELDS}
        for template in templates
    ])


def templates_from_json(raw) -> List[Dict]:
    try:
        data = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    templates = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            templates.append({field: item.get(field) for field in TEMPLATE_FIELDS})
    return templates


def templates_to_csv_text(templates: Sequence[Dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(TEMPLATE_FIELDS)
    for template in templates:
        writer.writerow([_csv_cell(template.get(field)) for field in TEMPLATE_FIELDS])
    return buffer.getvalue()


def templates_from_csv_text(text: str) -> Tuple[List[Dict], List[str]]:
    """Parse CSV text into templates plus human-readable warnings.

    Columns are matched by header name in any order; unknown columns are
    ignored and rows without a name are skipped.
    """
    reader = csv.DictReader(io.StringIO(str(text or "").lstrip(chr(0xFEFF))))
    if not reader.fieldnames:
        return [], ["The CSV file is empty."]
    headers = {str(name or "").strip().lower(): name for name in reader.fieldnames}
    if "name" not in headers:
        return [], ["The CSV file needs a 'name' column. Expected headers: %s."
                    % ", ".join(TEMPLATE_FIELDS)]
    templates, warnings = [], []
    for line_number, row in enumerate(reader, start=2):
        def cell(key):
            return str(row.get(headers.get(key, ""), "") or "").strip()

        name = cell("name")
        if not name:
            continue
        template = {"name": name, "description": cell("description"),
                    "notes": cell("notes")}
        for field in _NUMBER_FIELDS:
            raw_value = cell(field)
            try:
                template[field] = float(raw_value) if raw_value else None
            except ValueError:
                template[field] = None
                warnings.append("Row %d: '%s' is not a number for %s."
                                % (line_number, raw_value, field))
        mode = cell("fuel_mode").lower()
        resolved = mode if mode in _VALID_FUEL_MODES else _FUEL_ALIASES.get(mode)
        if resolved is None:
            resolved = ""
            warnings.append("Row %d: unknown fuel mode '%s' (use transit, dp, "
                            "anchor, or port)." % (line_number, cell("fuel_mode")))
        template["fuel_mode"] = resolved
        templates.append(template)
    return templates, warnings


def template_to_task_row(template: Dict, resource_id: str = "", seq: int = 0) -> Dict:
    """Build a full, ordinary task row from a template."""
    duration = _number(template.get("duration_hours"))
    now = schema.utc_now_iso()
    return {
        "task_id": schema.new_id(), "seq": int(seq),
        "name": str(template.get("name") or "Standard task"),
        "description": str(template.get("description") or ""),
        "is_phase": 0, "outline_level": 0, "resource_id": resource_id or "",
        "duration_mode": "manual",
        "duration_hours": 1.0 if duration is None else max(0.0, duration),
        "predecessor_task_id": "", "lag_hours": 0.0,
        "speed_knots": _number(template.get("speed_knots")),
        "direction": "forward",
        "fuel_mode": str(template.get("fuel_mode") or ""),
        "bunker_amount": _number(template.get("bunker_amount")),
        "layer_id": "", "layer_source": "", "layer_name": "", "feature_id": "",
        "feature_label": "", "geom_kind": "", "linked_ref_json": "",
        "created_utc": now, "modified_utc": now,
        "notes": str(template.get("notes") or ""),
    }


def _number(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return ("%.6f" % value).rstrip("0").rstrip(".") or "0"
    return str(value)
