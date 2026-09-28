"""Translate every KP in a burial plan through a :class:`KpMap` (no QGIS).

Used when a plan moves to another RPL (a new revision, or a different
route) and when an RPL's start KP changes. A geometry map keeps each item
at its **seabed position** and gives it the new route's KP; a shift map
renumbers everything by a constant.

Everything KP-bearing is covered:

* plan: scope, KP-range target depths, resolved Insufficient Information
  ranges, installation-path adjustments;
* events and sections (sections are mapped, not re-derived, so conclusions,
  tools and notes stay attached);
* hazards; exclusion-rule manual ranges and KP scopes;
* ground-model units and BAS rows (their ``src_*`` delivery KPs are left
  untouched — they record where the data came from);
* the active generation's stored analysis context.

Nothing is silently dropped: every mapped value's flags (``gap``,
``extrapolated``, ``stretched``, ``reversed``) are collected into the
:class:`PlanRereferenceReport` for the confirmation dialog and the log.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .kp_rereference import KpMap


@dataclass
class PlanRereferenceReport:
    items: int = 0
    max_shift_m: float = 0.0
    flagged: List[str] = field(default_factory=list)   # "Event PLDN KP 1.234: gap"
    outside: List[str] = field(default_factory=list)   # beyond the new route

    def summary(self) -> str:
        text = (f"{self.items} KP value(s) translated; largest change "
                f"{self.max_shift_m:.1f} m.")
        if self.flagged:
            text += f" {len(self.flagged)} value(s) need checking (gap / extrapolated / stretched)."
        if self.outside:
            text += f" {len(self.outside)} value(s) fall outside the new route."
        return text


class _Mapper:
    def __init__(self, kp_map: KpMap, report: PlanRereferenceReport,
                 bounds: Optional[Tuple[float, float]] = None):
        self.map = kp_map
        self.report = report
        self.bounds = bounds

    def _note(self, label: str, old: float, new: float, flags: Sequence[str]) -> None:
        self.report.items += 1
        self.report.max_shift_m = max(self.report.max_shift_m, abs(new - old) * 1000.0)
        if flags:
            self.report.flagged.append(f"{label} KP {old:.3f}: {', '.join(flags)}")
        if self.bounds is not None:
            lo, hi = self.bounds
            if new < lo - 5e-4 or new > hi + 5e-4:
                self.report.outside.append(f"{label} KP {old:.3f} → {new:.3f}")

    def kp(self, value, label: str):
        try:
            old = float(value)
        except (TypeError, ValueError):
            return value
        new, flags = self.map.map_kp(old)
        new = round(new, 6)
        self._note(label, old, new, flags)
        return new

    def pair(self, start, end, label: str):
        try:
            a, b = float(start), float(end)
        except (TypeError, ValueError):
            return start, end
        na, nb, flags = self.map.map_range(a, b)
        na, nb = round(na, 6), round(nb, 6)
        self._note(label, a, na, flags)
        self._note(label + " end", b, nb, [])
        if (nb - na) * (b - a) < 0:
            na, nb = nb, na
        return na, nb


def _json(text, default):
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _map_range_dicts(mapper: _Mapper, rows, label: str) -> List:
    out = []
    for row in rows or []:
        if isinstance(row, dict) and "start_kp" in row and "end_kp" in row:
            row = dict(row)
            row["start_kp"], row["end_kp"] = mapper.pair(row["start_kp"], row["end_kp"], label)
        out.append(row)
    return out


def _map_context(mapper: _Mapper, context: Dict) -> Dict:
    context = copy.deepcopy(context or {})
    for key in ("excluded", "screening", "influence"):
        for item in context.get(key) or []:
            if isinstance(item, dict) and "start_km" in item:
                item["start_km"], item["end_km"] = mapper.pair(
                    item["start_km"], item["end_km"], f"Analysis {key}")
    for key in ("insufficient", "dropped_short", "candidates"):
        pairs = []
        for pair in context.get(key) or []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                a, b = mapper.pair(pair[0], pair[1], f"Analysis {key}")
                pairs.append([a, b] + list(pair[2:]))
        if key in context:
            context[key] = pairs
    for key in ("rule_hits", "rule_nodata"):
        table = context.get(key)
        if isinstance(table, dict):
            for rule_id, pairs in table.items():
                table[rule_id] = [list(mapper.pair(p[0], p[1], f"Analysis {key}"))
                                  for p in pairs or [] if len(p) >= 2]
    return context


def map_plan(kp_map: KpMap, plan: Dict, events: Sequence[Dict],
             sections: Sequence[Dict], hazards: Sequence[Dict] = (),
             rules: Sequence[Dict] = (), ground_units: Sequence[Dict] = (),
             bas_rows: Sequence[Dict] = (), generation: Optional[Dict] = None,
             bounds: Optional[Tuple[float, float]] = None,
             event_labels: Optional[Dict[str, str]] = None) -> Dict:
    """Mapped copies of every KP-bearing row, plus ``report``.

    ``bounds`` is the new route's (start KP, end KP) for the "outside the
    route" warnings. Returns ``{"plan", "events", "sections", "hazards",
    "rules", "ground_units", "bas_rows", "generation", "report"}``.
    """
    report = PlanRereferenceReport()
    m = _Mapper(kp_map, report, bounds)
    labels = event_labels or {}

    new_plan = dict(plan)
    lo, hi = new_plan.get("scope_start_kp"), new_plan.get("scope_end_kp")
    if lo is not None and hi is not None and (float(lo or 0.0) or float(hi or 0.0)):
        new_plan["scope_start_kp"], new_plan["scope_end_kp"] = m.pair(lo, hi, "Plan scope")
    params = _json(new_plan.get("params_json"), {})
    if params.get("target_burial_ranges"):
        params["target_burial_ranges"] = _map_range_dicts(
            m, params["target_burial_ranges"], "Target depth range")
    if params.get("dismissed_insufficient"):
        out = []
        for entry in params["dismissed_insufficient"]:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                a, b = m.pair(entry[0], entry[1], "Resolved Insufficient Information")
                out.append([a, b] + list(entry[2:]))
            else:
                out.append(entry)
        params["dismissed_insufficient"] = out
    paths = params.get("installation_paths")
    if isinstance(paths, dict) and paths.get("adjustments"):
        adjusted = []
        for adj in paths["adjustments"]:
            if isinstance(adj, dict) and "kp" in adj:
                adj = dict(adj)
                adj["kp"] = m.kp(adj["kp"], "Path adjustment")
            adjusted.append(adj)
        paths["adjustments"] = adjusted
    new_plan["params_json"] = json.dumps(params)

    new_events = []
    for event in events:
        event = dict(event)
        label = labels.get(str(event.get("event_id") or "")) or "Event"
        event["kp"] = m.kp(event.get("kp"), label)
        # Derived position: the caller re-stamps it on the new route.
        event["lat"] = event["lon"] = event["depth_m"] = None
        new_events.append(event)

    new_sections = []
    for section in sections:
        section = dict(section)
        section["start_kp"], section["end_kp"] = m.pair(
            section.get("start_kp"), section.get("end_kp"), "Section")
        try:
            section["length_km"] = round(abs(float(section["end_kp"]) - float(section["start_kp"])), 6)
        except (TypeError, ValueError):
            pass
        new_sections.append(section)

    new_hazards = []
    for hazard in hazards:
        hazard = dict(hazard)
        name = hazard.get("name") or hazard.get("hazard_type") or "Hazard"
        if hazard.get("end_kp") is not None and hazard.get("kp") is not None:
            hazard["kp"], hazard["end_kp"] = m.pair(hazard["kp"], hazard["end_kp"], str(name))
        elif hazard.get("kp") is not None:
            hazard["kp"] = m.kp(hazard["kp"], str(name))
        new_hazards.append(hazard)

    new_rules = []
    for rule in rules:
        rule = dict(rule)
        config = _json(rule.get("config_json"), {})
        changed = False
        for key in ("ranges", "scope_ranges"):
            if config.get(key):
                config[key] = _map_range_dicts(m, config[key],
                                               f"Rule '{rule.get('name') or ''}'")
                changed = True
        if changed:
            rule["config_json"] = json.dumps(config)
        new_rules.append(rule)

    def map_rows(rows, label):
        out = []
        for row in rows:
            row = dict(row)
            if row.get("start_kp") is not None and row.get("end_kp") is not None:
                row["start_kp"], row["end_kp"] = m.pair(row["start_kp"], row["end_kp"], label)
            out.append(row)
        return out

    new_generation = None
    if generation:
        new_generation = dict(generation)
        summary = _json(new_generation.get("summary_json"), {})
        if isinstance(summary.get("context"), dict):
            summary["context"] = _map_context(m, summary["context"])
            new_generation["summary_json"] = json.dumps(summary)

    return {
        "plan": new_plan, "events": new_events, "sections": new_sections,
        "hazards": new_hazards, "rules": new_rules,
        "ground_units": map_rows(ground_units, "Ground unit"),
        "bas_rows": map_rows(bas_rows, "BAS row"),
        "generation": new_generation, "report": report,
    }
