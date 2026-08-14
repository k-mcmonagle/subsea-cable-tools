# -*- coding: utf-8 -*-
"""Standalone HTML report for a burial plan (pure python).

One self-contained file (inline CSS, base64-embedded profile image) so the
report can be emailed or archived beside the GeoPackage. Nothing is computed
here beyond formatting: every value comes from the plan registry rows the
Review tab already holds, so the report always matches the tool state it was
exported from.
"""

from __future__ import annotations

import base64
import html
import json
from typing import Dict, List, Optional, Sequence

from ..workbench import schema as wb_schema
from . import events as ev
from . import schema
from . import tools as tools_mod

_KIND_LABELS = {
    wb_schema.RULE_KIND_THRESHOLD: "Water depth / slope threshold",
    wb_schema.RULE_KIND_PROXIMITY: "Crossings / proximity",
    wb_schema.RULE_KIND_POLYGON: "Seabed soils / polygon class",
    wb_schema.RULE_KIND_KP_TABLE: "KP range table",
    wb_schema.RULE_KIND_MANUAL: "Manual ranges",
}

_CSS = """
body { font-family: Segoe UI, Arial, sans-serif; color: #1a1a1a;
       margin: 2.2em auto; max-width: 62em; padding: 0 1em; }
h1 { font-size: 1.5em; margin-bottom: 0.1em; }
h2 { font-size: 1.15em; border-bottom: 1px solid #ccc; padding-bottom: 0.15em;
     margin-top: 1.6em; }
table { border-collapse: collapse; width: 100%; font-size: 0.85em;
        margin: 0.6em 0; }
th, td { border: 1px solid #d0d0d0; padding: 3px 7px; text-align: left;
         vertical-align: top; }
th { background: #f2f2f2; }
tr:nth-child(even) td { background: #fafafa; }
.meta { color: #555; font-size: 0.9em; }
.badge { display: inline-block; padding: 1px 9px; border-radius: 9px;
         font-size: 0.8em; font-weight: 600; }
.badge.draft { background: #e8f5e9; color: #1b5e20; }
.badge.stale { background: #fff3cd; color: #7a4f00; }
.badge.issued { background: #e3f2fd; color: #0d47a1; }
.kind-burial { color: #1b7f3b; font-weight: 600; }
.kind-skip { color: #d62728; }
.kind-insufficient_info { color: #757575; }
.summary { display: flex; flex-wrap: wrap; gap: 1.6em; margin: 0.8em 0; }
.summary div { min-width: 8em; }
.summary .big { font-size: 1.3em; font-weight: 600; }
.note { background: #fff8e1; border: 1px solid #f0e0a0; padding: 0.5em 0.8em;
        font-size: 0.85em; margin: 1em 0; }
.muted { color: #777; font-size: 0.85em; margin: 0.2em 0 0.6em; }
img.profile { max-width: 100%; border: 1px solid #ccc; margin: 0.5em 0; }
.footer { margin-top: 2.5em; color: #777; font-size: 0.8em;
          border-top: 1px solid #ccc; padding-top: 0.5em; }
@media print { body { margin: 0.5em; } }
"""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _kp(value) -> str:
    return schema.format_kp(value)


def _num(value, places: int) -> str:
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return ""


def _config(row: Dict) -> Dict:
    try:
        config = json.loads(row.get("config_json") or "{}")
    except (ValueError, TypeError):
        return {}
    return config if isinstance(config, dict) else {}


def section_kind_label(kind: str, method: str) -> str:
    return schema.section_kind_label(kind, method)


def section_reason_text(section: Dict) -> str:
    """Readable rendering of a section's reason_json (mirrors the Builder tab)."""
    try:
        reason = json.loads(section.get("reason_json") or "{}")
    except (ValueError, TypeError):
        return ""
    parts: List[str] = []
    if reason.get("dominant_rule"):
        parts.append(f"Excluded by {reason['dominant_rule']}")
    elif reason.get("fired_rules"):
        parts.append("Excluded by " + ", ".join(reason["fired_rules"][:3]))
    if reason.get("below_min_length"):
        parts.append("below minimum section length")
    if reason.get("insufficient_information"):
        parts.append("Insufficient Information")
    if reason.get("manual"):
        parts.append("manual")
    if reason.get("dangling_start"):
        parts.append("no end event before scope end")
    for conflict in reason.get("exclusion_conflicts") or []:
        rules = ", ".join(conflict.get("rules") or []) or "configured rule"
        parts.append(f"Manual burial overlaps Exclusion Area ({rules}) at KP "
                     f"{_kp(conflict.get('start_kp'))}-{_kp(conflict.get('end_kp'))}")
    for entry in reason.get("screening") or []:
        parts.append(f"Screening: {entry.get('rule')} KP "
                     f"{_kp(entry.get('start_kp'))}-{_kp(entry.get('end_kp'))}")
    for flag in reason.get("influence_flags") or []:
        parts.append(flag.get("message") or "")
    return "; ".join(p for p in parts if p)


def rule_condition_text(rule: Dict) -> str:
    """Compact human-readable summary of one rule's condition config."""
    config = _config(rule)
    kind = rule.get("kind") or ""
    parts: List[str] = []
    if kind == wb_schema.RULE_KIND_THRESHOLD:
        profile = (config.get("profile") or "depth").lower()
        if profile == "slope":
            component = config.get("slope_component") or "long"
            profile = {"long": "longitudinal slope", "cross": "cross slope",
                       "absolute": "absolute slope"}.get(component,
                                                         f"{component} slope")
        if config.get("bands"):
            parts.append(f"{profile}: WD-banded limits "
                         f"({len(config.get('bands') or [])} bands)")
        elif config.get("slope_signed"):
            down = config.get("downslope_max_deg")
            up = config.get("upslope_max_deg")
            limits = []
            if down is not None:
                limits.append(f"down-slope > {down}°")
            if up is not None:
                limits.append(f"up-slope > {up}°")
            parts.append("signed slope: " + (" or ".join(limits) or "—"))
        else:
            op = config.get("op") or ">"
            value = config.get("value")
            unit = "°" if "slope" in profile else " m"
            text = f"{profile} {op} {value}{unit}"
            if op == "between" and config.get("value2") is not None:
                text = (f"{profile} between {value} and "
                        f"{config.get('value2')}{unit}")
            parts.append(text)
        if config.get("slope_window_m"):
            parts.append(f"slope window {config['slope_window_m']} m")
    elif kind == wb_schema.RULE_KIND_PROXIMITY:
        parts.append(f"within {config.get('distance_m') or 0} m")
        if config.get("buffer_field"):
            parts.append(f"per-feature buffer field '{config['buffer_field']}'")
        if config.get("filter_expression"):
            parts.append(f"filter: {config['filter_expression']}")
    elif kind == wb_schema.RULE_KIND_POLYGON:
        values = ", ".join(config.get("match_values") or [])
        if config.get("match_expression"):
            parts.append(f"match: {config['match_expression']}")
        elif config.get("attribute"):
            parts.append(f"{config.get('attribute')} in [{values}]")
        corridor_mode = (config.get("route_buffer_mode") or "").lower()
        if corridor_mode == "fixed" and config.get("route_buffer_m"):
            parts.append(f"within {config['route_buffer_m']} m of route")
        elif corridor_mode == "wd" and config.get("route_buffer_wd"):
            parts.append(f"within {config['route_buffer_wd']} ×WD of route")
    elif kind == wb_schema.RULE_KIND_KP_TABLE:
        parts.append(f"fields {config.get('start_field') or 'start_kp'}/"
                     f"{config.get('end_field') or 'end_kp'}")
        if config.get("filter_expression"):
            parts.append(f"filter: {config['filter_expression']}")
    elif kind == wb_schema.RULE_KIND_MANUAL:
        ranges = config.get("ranges") or []
        parts.append(", ".join(f"{_kp(r.get('start_kp'))}-{_kp(r.get('end_kp'))}"
                               for r in ranges) or "no ranges")
    from . import generation as _generation

    ext = _generation.extension_config(config)
    if ext["before"] or ext["after"]:
        unit = "×WD" if ext["mode"] == _generation.EXTEND_MODE_WD else "m"
        parts.append(f"extended {ext['before']:g}/{ext['after']:g} {unit} "
                     "(before/after)")
    if config.get("influence_before_m") or config.get("influence_after_m"):
        parts.append(f"influence {config.get('influence_before_m') or 0}/"
                     f"{config.get('influence_after_m') or 0} m")
    scope_ranges = config.get("scope_ranges") or []
    if scope_ranges:
        parts.append("applies KP " + ", ".join(
            f"{_kp(r.get('start_kp'))}-{_kp(r.get('end_kp'))}"
            for r in scope_ranges))
    return "; ".join(parts)


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]],
           raw: bool = False) -> str:
    """rows are already-escaped (raw=True) or plain text cells."""
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = "".join(f"<td>{cell if raw else _esc(cell)}</td>" for cell in row)
        body.append(f"<tr>{cells}</tr>")
    return (f"<table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>")


def build_report_html(plan: Dict,
                      sections: Sequence[Dict],
                      events: Sequence[Dict],
                      rules: Sequence[Dict],
                      inputs: Sequence[Dict],
                      generation: Optional[Dict] = None,
                      change_log: Optional[Sequence[Dict]] = None,
                      profile_png: Optional[bytes] = None,
                      now_utc: str = "",
                      hazards: Optional[Sequence[Dict]] = None,
                      risk_checks: Optional[Sequence[Dict]] = None,
                      tools: Optional[Sequence[Dict]] = None) -> str:
    """Assemble the full report; pure formatting, no QGIS access."""
    method = plan.get("method") or ""
    status = plan.get("status") or ""
    now = now_utc or schema.utc_now_iso()
    scope_start = float(plan.get("scope_start_kp") or 0.0)
    scope_end = float(plan.get("scope_end_kp") or 0.0)
    scope_km = abs(scope_end - scope_start)

    def total(kind: str) -> float:
        return sum(float(s.get("length_km") or 0.0) for s in sections
                   if s.get("kind") == kind)

    burial_km = total(schema.SECTION_BURIAL)
    skip_km = total(schema.SECTION_SKIP)
    insufficient_km = total(schema.SECTION_INSUFFICIENT)
    pct = (100.0 * burial_km / scope_km) if scope_km > 0 else 0.0

    parts: List[str] = []
    parts.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    parts.append(f"<title>Burial plan — {_esc(plan.get('name'))}</title>")
    parts.append(f"<style>{_CSS}</style></head><body>")

    # -- header ---------------------------------------------------------------
    badge = (f"<span class='badge {_esc(status)}'>{_esc(status)}</span>"
             if status else "")
    parts.append(f"<h1>Burial plan — {_esc(plan.get('name'))} {badge}</h1>")
    direction = "A → B (with increasing KP)" if int(plan.get("direction") or 1) >= 0 \
        else "B → A (against KP)"
    default_tool_id, default_config_id = tools_mod.plan_default_tool(plan)
    default_tool_text = tools_mod.tool_display(
        tools or [], default_tool_id, default_config_id)
    meta = [
        ("Method", schema.METHOD_LABELS.get(method, method)),
        ("Default burial tool", default_tool_text or "—"),
        ("RPL", f"{plan.get('rpl_name') or '—'}"
                + (f" ({plan.get('rpl_revision')})" if plan.get("rpl_revision") else "")),
        ("Scope", f"KP {_kp(scope_start)} – {_kp(scope_end)} ({scope_km:.3f} km)"),
        ("Direction of installation", direction),
        ("Plan revision", plan.get("rev_label") or "—"),
        ("Target burial depth",
         f"{plan.get('target_burial_m')} m" if plan.get("target_burial_m") else "—"),
        ("Description", plan.get("description") or "—"),
    ]
    parts.append("<p class='meta'>" + "<br>".join(
        f"<b>{_esc(k)}:</b> {_esc(v)}" for k, v in meta) + "</p>")
    if plan.get("notes"):
        parts.append(f"<p class='meta'><b>Notes:</b> {_esc(plan.get('notes'))}</p>")
    parts.append(
        "<div class='note'><b>Beta</b> — sanity-check the output. All criteria "
        "values are user-entered; see the Exclusion stack's source references "
        "below.</div>")

    # -- summary --------------------------------------------------------------
    parts.append("<h2>Summary</h2><div class='summary'>")
    for label, value in (
            ("Burial", f"{burial_km:.3f} km ({pct:.0f}%)"),
            ("Skips", f"{skip_km:.3f} km"),
            ("Insufficient information", f"{insufficient_km:.3f} km"),
            ("Sections", str(len(sections))),
            ("Events", str(len(events)))):
        parts.append(f"<div><div class='big'>{_esc(value)}</div>"
                     f"{_esc(label)}</div>")
    parts.append("</div>")
    by_conclusion: Dict[str, float] = {}
    for section in sections:
        key = section.get("conclusion") or ""
        by_conclusion[key] = by_conclusion.get(key, 0.0) \
            + float(section.get("length_km") or 0.0)
    conclusion_rows = [
        (schema.CONCLUSION_LABELS.get(key, key) or "(unassigned)",
         f"{value:.3f} km")
        for key, value in sorted(by_conclusion.items()) if value > 0]
    if conclusion_rows:
        parts.append(_table(("Operating-envelope conclusion", "Length"),
                            conclusion_rows))

    # -- profile --------------------------------------------------------------
    if profile_png:
        encoded = base64.b64encode(profile_png).decode("ascii")
        parts.append("<h2>Longitudinal profile</h2>")
        parts.append(f"<img class='profile' alt='Bathymetry profile' "
                     f"src='data:image/png;base64,{encoded}'>")

    # -- sections -------------------------------------------------------------
    parts.append("<h2>Sections</h2>")
    section_refs = schema.section_refs(
        sections, int(plan.get("direction") or 1), method)
    parts.append(f"<p class='muted'>{_esc(schema.section_ref_legend(method))}"
                 "</p>")
    section_rows = []
    for section in sections:
        kind = section.get("kind") or ""
        section_rows.append((
            _esc(section_refs.get(str(section.get("section_id") or ""), "")),
            f"<span class='kind-{_esc(kind)}'>"
            f"{_esc(section_kind_label(kind, method))}</span>",
            _kp(section.get("start_kp")), _kp(section.get("end_kp")),
            _num(section.get("length_km"), 3),
            _esc(section.get("state")),
            _esc(schema.CONCLUSION_LABELS.get(section.get("conclusion") or "", "")),
            _esc(section.get("confidence")),
            _esc(tools_mod.section_tool_display(section, plan, tools or [])),
            _esc(schema.SKIP_HANDLING_LABELS.get(
                section.get("skip_handling") or "", "")
                if kind == schema.SECTION_SKIP else ""),
            _esc(section_reason_text(section)),
            _esc(section.get("notes")),
        ))
    parts.append(_table(("ID", "Kind", "Start KP", "End KP", "Length (km)",
                         "State", "Conclusion", "Confidence", "Tool",
                         "Skip handling", "Reasons", "Notes"),
                        section_rows, raw=True))

    # -- events ---------------------------------------------------------------
    parts.append("<h2>Events</h2>")
    event_rows = []
    for event in events:
        event_rows.append((
            str(int(event.get("seq") or 0)),
            ev.event_label(event.get("event_type") or "", method),
            _kp(event.get("kp")),
            _num(event.get("lat"), 7), _num(event.get("lon"), 7),
            _num(event.get("depth_m"), 1),
            event.get("source") or "", event.get("status") or "",
            "yes" if int(event.get("locked") or 0) else "",
            event.get("notes") or "",
        ))
    parts.append(_table(("Seq", "Event", "KP", "Lat", "Lon", "Depth (m)",
                         "Source", "Status", "Locked", "Notes"), event_rows))

    # -- risk profile ---------------------------------------------------------
    if hazards:
        parts.append("<h2>Risk profile</h2>")
        counts: Dict[str, int] = {}
        for hazard in hazards:
            level = hazard.get("risk") or ""
            counts[level] = counts.get(level, 0) + 1
        summary_bits = [
            f"{counts.get(level, 0)} {schema.RISK_LABELS[level].lower()}"
            for level in (schema.RISK_HIGH, schema.RISK_MEDIUM, schema.RISK_LOW)
            if counts.get(level)]
        if counts.get(""):
            summary_bits.append(f"{counts['']} unassigned")
        parts.append(f"<p>{len(list(hazards))} hazard(s)"
                     + (": " + ", ".join(summary_bits) if summary_bits else "")
                     + ".</p>")
        check_names = {str(c.get("check_id") or ""): (c.get("name") or "")
                       for c in (risk_checks or [])}
        hazard_rows = []
        for hazard in hazards:
            hazard_rows.append((
                _esc(schema.RISK_LABELS.get(hazard.get("risk") or "", "")),
                _esc(schema.HAZARD_STATUS_LABELS.get(
                    hazard.get("status") or "", "")),
                _kp(hazard.get("kp")),
                _kp(hazard.get("end_kp")),
                _num(hazard.get("offset_m"), 1),
                "yes" if int(hazard.get("crossing") or 0) else "",
                _num(hazard.get("crossing_angle_deg"), 1),
                _esc(hazard.get("label")),
                _esc(check_names.get(str(hazard.get("check_id") or ""),
                                     "manual")),
                _esc(hazard.get("notes")),
            ))
        parts.append(_table(("Risk", "Status", "KP", "End KP",
                             "Offset (m, +stbd/−port)", "Crossing",
                             "Angle (°)", "Feature", "Check", "Notes"),
                            hazard_rows, raw=True))

    # -- exclusion stack ------------------------------------------------------
    parts.append("<h2>Exclusion stack</h2>")
    rule_rows = []
    for rule in rules:
        rule_rows.append((
            str(int(rule.get("seq") or 0) + 1),
            rule.get("name") or "",
            schema.CRITERION_LABELS.get(rule.get("criterion_class") or "", ""),
            _KIND_LABELS.get(rule.get("kind") or "", rule.get("kind") or ""),
            rule_condition_text(rule),
            rule.get("source_ref") or "",
            "enabled" if int(rule.get("enabled") or 0) else "disabled",
            rule.get("notes") or "",
        ))
    parts.append(_table(("#", "Criterion", "Class", "Kind", "Condition",
                         "Source reference", "State", "Notes"), rule_rows))

    # -- input register -------------------------------------------------------
    if inputs:
        parts.append("<h2>Input data register</h2>")
        input_rows = []
        for row in inputs:
            input_rows.append((
                schema.INPUT_ROLE_LABELS.get(row.get("role") or "",
                                             row.get("role") or ""),
                row.get("layer_name") or "",
                row.get("originator") or "", row.get("revision") or "",
                row.get("status") or "", row.get("received_utc") or "",
                row.get("quality") or "", row.get("notes") or "",
            ))
        parts.append(_table(("Role", "Layer", "Originator", "Revision",
                             "Status", "Received", "Quality", "Notes"),
                            input_rows))

    # -- provenance -----------------------------------------------------------
    if generation:
        parts.append("<h2>Generation provenance</h2>")
        try:
            params = json.loads(generation.get("params_json") or "{}")
        except (ValueError, TypeError):
            params = {}
        try:
            fingerprints = json.loads(
                generation.get("inputs_fingerprint_json") or "{}")
        except (ValueError, TypeError):
            fingerprints = {}
        parts.append("<p class='meta'>" + "<br>".join([
            f"<b>Generation:</b> {_esc(str(generation.get('generation_id') or '')[:8])}",
            f"<b>Run (UTC):</b> {_esc(generation.get('run_utc'))}",
            f"<b>Parameters:</b> " + _esc(", ".join(
                f"{k}={v}" for k, v in sorted(params.items()))),
            f"<b>Input fingerprints recorded:</b> {len(fingerprints)}",
        ]) + "</p>")

    # -- change log -----------------------------------------------------------
    entries = list(change_log or [])
    if entries:
        parts.append("<h2>Change log</h2>")
        log_rows = [(str(e.get("seq") or 0), e.get("utc") or "",
                     e.get("user") or "", e.get("action") or "",
                     str(e.get("target_id") or "")[:12], e.get("reason") or "")
                    for e in reversed(entries)]
        parts.append(_table(("Seq", "When (UTC)", "User", "Action", "Target",
                             "Reason"), log_rows))

    parts.append(f"<div class='footer'>Generated by Subsea Cable Tools — "
                 f"Burial Planner on {_esc(now)}. Plan id "
                 f"{_esc(str(plan.get('plan_id') or '')[:8])}.</div>")
    parts.append("</body></html>")
    return "".join(parts)
