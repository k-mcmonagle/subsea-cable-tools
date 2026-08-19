# -*- coding: utf-8 -*-
"""Burial Tools registry helpers (pure python — no QGIS imports).

A *tool* is a burial vehicle (plough, ROV jet trencher, mechanical trencher…)
registered once per project GeoPackage and shared by every plan — the
Planner-vessels model. A tool carries:

- its type (a ``schema.METHODS`` id, open enum);
- a list of operating *configurations* (``configs_json``): e.g. a plough's
  "Jetting 3 m" vs "Passive 2 m share" modes, each with its own geometry and
  capability numbers. Every value is user-entered with a source reference —
  the plugin ships no engineering values;
- an optional body-fixed footprint outline (WKT, metres, CRP at the origin,
  bow/front along +Y) imported from a DXF — see ``footprint.py``.

Plans reference tools loosely: ``params_json["tool_id"]``/["tool_config_id"]
hold the plan default; ``bp_section.tool_id``/``tool_config_id`` override it
per section ("" = inherit the plan default). Deleting a tool never edits
plans — dangling references render as "(unregistered tool)".

Registries travel between projects as versioned JSON (the rule-set format
precedent): ``{"format": "subsea_cable_tools.burial.tool_registry",
"version": 1, "tools": [...]}``.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence, Tuple

from . import schema

TOOL_REGISTRY_FORMAT = "subsea_cable_tools.burial.tool_registry"
TOOL_REGISTRY_VERSION = 1

# Configuration dict keys (all optional except config_id/label). Numeric
# values are metres unless suffixed otherwise.
CONFIG_NUMERIC_FIELDS: List[Tuple[str, str]] = [
    ("track_width_m", "Track width (m)"),
    ("bearing_length_m", "Bearing length (m)"),
    ("min_turn_radius_m", "Min turning radius (m)"),
    ("max_burial_depth_m", "Max burial depth (m)"),
    ("max_water_depth_m", "Max water depth (m)"),
]


def parse_configs(tool: Optional[Dict]) -> List[Dict]:
    """The tool's configuration list ([] on missing/invalid JSON)."""
    if not tool:
        return []
    try:
        configs = json.loads(tool.get("configs_json") or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(configs, list):
        return []
    return [c for c in configs if isinstance(c, dict)]


def new_config(label: str = "") -> Dict:
    """A blank configuration with a stable id (survives relabelling)."""
    return {"config_id": schema.new_id(), "label": label, "mode": "",
            "notes": "", "source_ref": ""}


def config_by_id(tool: Optional[Dict], config_id: str) -> Optional[Dict]:
    for config in parse_configs(tool):
        if str(config.get("config_id") or "") == str(config_id or ""):
            return config
    return None


def tool_by_id(tools: Sequence[Dict], tool_id: str) -> Optional[Dict]:
    if not tool_id:
        return None
    return next((t for t in tools or []
                 if str(t.get("tool_id") or "") == str(tool_id)), None)


def config_label(config: Optional[Dict]) -> str:
    if not config:
        return ""
    label = str(config.get("label") or "").strip()
    mode = str(config.get("mode") or "").strip()
    if label and mode and mode.lower() not in label.lower():
        return f"{label} ({mode})"
    return label or mode


def tool_display(tools: Sequence[Dict], tool_id: str,
                 config_id: str = "") -> str:
    """Readable "Tool — Configuration" text; flags unregistered references."""
    if not tool_id:
        return ""
    tool = tool_by_id(tools, tool_id)
    if tool is None:
        return "(unregistered tool)"
    text = str(tool.get("name") or "?")
    if config_id:
        config = config_by_id(tool, config_id)
        text += f" — {config_label(config) or '(unknown configuration)'}"
    return text


def plan_default_tool(plan: Optional[Dict]) -> Tuple[str, str]:
    """The plan's default (tool_id, tool_config_id) from params_json."""
    try:
        params = json.loads((plan or {}).get("params_json") or "{}")
    except (ValueError, TypeError):
        params = {}
    if not isinstance(params, dict):
        params = {}
    return (str(params.get("tool_id") or ""),
            str(params.get("tool_config_id") or ""))


def plan_default_config(plan: Optional[Dict], tools: Sequence[Dict]
                        ) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Return the registered default tool and configuration rows."""
    tool_id, config_id = plan_default_tool(plan)
    tool = tool_by_id(tools, tool_id)
    return tool, config_by_id(tool, config_id)


def section_tool_display(section: Dict, plan: Optional[Dict],
                         tools: Sequence[Dict]) -> str:
    """The effective tool text for one section ("" when nothing is set).

    A blank tool assignment inherits the plan default tool; the section may
    still carry its own configuration of that inherited tool (the Plan
    Builder allows exactly that), which overrides the default configuration.
    Only burial sections carry a tool.
    """
    if (section.get("kind") or "") != schema.SECTION_BURIAL:
        return ""
    tool_id = str(section.get("tool_id") or "")
    config_id = str(section.get("tool_config_id") or "")
    if tool_id:
        return tool_display(tools, tool_id, config_id)
    default_tool, default_config = plan_default_tool(plan)
    if default_tool:
        return tool_display(tools, default_tool, config_id or default_config)
    return ""


def tool_at_kp(sections: Sequence[Dict], plan: Optional[Dict],
               tools: Sequence[Dict], kp_km: float) -> Optional[Dict]:
    """The effective tool row at a KP: the containing burial section's
    assignment (blank = plan default), else the plan default tool."""
    tool_id = ""
    for section in sections or []:
        if (section.get("kind") or "") != schema.SECTION_BURIAL:
            continue
        try:
            start = float(section.get("start_kp"))
            end = float(section.get("end_kp"))
        except (TypeError, ValueError):
            continue
        if min(start, end) - 1e-9 <= float(kp_km) <= max(start, end) + 1e-9:
            tool_id = str(section.get("tool_id") or "")
            break
    if not tool_id:
        tool_id, _config = plan_default_tool(plan)
    return tool_by_id(tools, tool_id)


# ---------------------------------------------------------------------------
# JSON registry import/export (the rule-set format precedent)
# ---------------------------------------------------------------------------


def registry_json(tools: Sequence[Dict]) -> str:
    payload = {
        "format": TOOL_REGISTRY_FORMAT,
        "version": TOOL_REGISTRY_VERSION,
        "tools": [dict(tool) for tool in tools or []],
    }
    return json.dumps(payload, indent=2)


def parse_registry_json(text: str) -> List[Dict]:
    """Tool rows from a registry JSON export.

    Tool/config ids are preserved so a registry shared across an
    organisation keeps section assignments meaningful; the caller decides
    how to handle collisions with already-registered ids (upsert).
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"The file is not valid JSON: {exc}")
    if not isinstance(payload, dict) \
            or payload.get("format") != TOOL_REGISTRY_FORMAT:
        raise ValueError("The file is not a Burial Tools registry export.")
    rows = payload.get("tools")
    if not isinstance(rows, list):
        raise ValueError("The registry contains no tools list.")
    out: List[Dict] = []
    numeric = {name for name, kind in schema.TOOL_FIELDS if kind == "float"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        tool = {name: row.get(name) for name, _t in schema.TOOL_FIELDS}
        tool["tool_id"] = str(tool.get("tool_id") or "") or schema.new_id()
        tool["name"] = str(tool.get("name") or "").strip() or "Unnamed tool"
        tool["tool_type"] = schema.normalise_method(
            str(tool.get("tool_type") or ""))
        # Hand-edited files may carry strings in numeric fields; coerce or
        # drop them so a bad value cannot crash the registry table later.
        for name in numeric:
            value = tool.get(name)
            if value in (None, ""):
                tool[name] = None
                continue
            try:
                tool[name] = float(str(value).replace(",", "."))
            except (TypeError, ValueError):
                tool[name] = None
        # Let the store stamp created/modified times for rows that lack them.
        for name in ("created_utc", "modified_utc"):
            if not tool.get(name):
                tool.pop(name, None)
        # Round-trip the configs through the parser so malformed JSON in a
        # hand-edited file degrades to an empty list rather than breaking UI.
        tool["configs_json"] = json.dumps(parse_configs(tool))
        out.append(tool)
    return out
