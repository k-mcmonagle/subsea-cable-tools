# -*- coding: utf-8 -*-
"""Burial graph templates: what a report page shows and how it is laid out.

A template is a plain JSON-able dict, saved in the burial data file (each
save a new version) and exportable to share between projects::

    {"version": 1, "name": "Burial graph A3",
     "page": {"size": "A3", "orientation": "landscape", "width_mm": 420, "height_mm": 297, "margin_mm": 10},
     "title": "{plan} burial graph", "subtitle": "KP {kp_start} to {kp_end}",
     "fields": [["Project", "{project}"], ["Route", "{rpl}"], ...],
     "kp_per_page": 5.0, "overlap_pct": 5.0, "reverse": False, "kp_tick": None,
     "panels": [{"kind": "status"}, {"kind": "dol", "weight": 4, "y_min": None, ...}, ...],
     "legend": True, "footer": True, "page_numbers": True, "footer_text": "",
     "summary_page": True, "sources_page": False}

Panels, top to bottom: ``status`` (meets / below / no data bar), ``dol``
(DOL with the target, the below-target fill and optionally each dataset),
``source`` (which dataset governs) and ``channel`` (any loaded channel:
water depth, pitch, roll, a tension…). Bars have a fixed height in mm;
plots share the rest by ``weight``. Text may use tokens, filled per page by
:func:`expand`: ``{project} {plan} {rpl} {rpl_rev} {datasets} {mode}
{target} {date} {kp_start} {kp_end} {range_start} {range_end} {page}
{pages}``.

Pure Python.
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

VERSION = 1
PAGE_SIZES = {"A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0), "A1": (841.0, 594.0),
              "Letter": (279.4, 215.9), "Tabloid": (431.8, 279.4)}
PANEL_STATUS, PANEL_DOL, PANEL_SOURCE, PANEL_CHANNEL = "status", "dol", "source", "channel"
PANEL_KINDS = {PANEL_STATUS: "Status bar (meets / below / no data)", PANEL_DOL: "DOL and target",
               PANEL_SOURCE: "Which dataset governs", PANEL_CHANNEL: "A channel (water depth, pitch, tension…)"}
BAR_KINDS = (PANEL_STATUS, PANEL_SOURCE)
BAR_HEIGHT_MM = 3.5
TOKENS = ("project", "plan", "rpl", "rpl_rev", "datasets", "mode", "target", "date", "kp_start", "kp_end",
          "range_start", "range_end", "page", "pages")
MAX_PAGES = 500


def default_template(name: str = "Burial graph A3", size: str = "A3") -> Dict:
    width, height = PAGE_SIZES.get(size, PAGE_SIZES["A3"])
    return {
        "version": VERSION,
        "name": name,
        "page": {"size": size, "orientation": "landscape", "width_mm": width, "height_mm": height,
                 "margin_mm": 10.0},
        "title": "{plan}: burial graph",
        "subtitle": "KP {kp_start} to {kp_end} · DOL in m below mean seabed level · {mode}",
        "fields": [["Project", "{project}"], ["Route", "{rpl} {rpl_rev}"], ["Data", "{datasets}"],
                   ["Target", "{target}"], ["Date", "{date}"], ["Revision", ""]],
        "kp_per_page": 5.0 if size in ("A3", "A2", "A1", "Tabloid") else 2.0,
        "overlap_pct": 5.0,
        "reverse": False,
        "kp_tick": None,
        "panels": [
            {"kind": PANEL_STATUS},
            {"kind": PANEL_DOL, "weight": 4.0, "label": "DOL (m)", "y_min": None, "y_max": None, "invert": True,
             "datasets": True, "target": True, "below": True},
            {"kind": PANEL_SOURCE},
            {"kind": PANEL_CHANNEL, "channel": "water_depth", "weight": 1.5, "label": "Water depth (m)",
             "y_min": None, "y_max": None, "invert": True},
        ],
        "legend": True,
        "footer": True,
        "page_numbers": True,
        "footer_text": "",
        "summary_page": True,
        "sources_page": False,
    }


def builtin_templates() -> List[Dict]:
    a4 = default_template("Burial graph A4", "A4")
    a4["fields"] = a4["fields"][:4]
    a4["panels"] = a4["panels"][:3]
    return [default_template(), a4]


def _number(value, default: Optional[float]) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def page_size_mm(template: Dict) -> Tuple[float, float]:
    """(width, height) of the page in mm, from the named size and orientation (or the custom size)."""
    page = template.get("page") or {}
    size = page.get("size") or "A3"
    if size in PAGE_SIZES:
        long_side, short_side = PAGE_SIZES[size]
    else:
        long_side = _number(page.get("width_mm"), 420.0)
        short_side = _number(page.get("height_mm"), 297.0)
        long_side, short_side = max(long_side, short_side), min(long_side, short_side)
    if page.get("orientation") == "portrait":
        return short_side, long_side
    return long_side, short_side


def normalise(template: Optional[Dict]) -> Dict:
    """A complete template from a stored or partial one (unknown keys dropped, values coerced)."""
    out = default_template()
    if not isinstance(template, dict):
        return out
    out["name"] = str(template.get("name") or out["name"])
    page = template.get("page") if isinstance(template.get("page"), dict) else {}
    out["page"]["size"] = page.get("size") if page.get("size") in PAGE_SIZES or page.get("size") == "Custom" \
        else out["page"]["size"]
    out["page"]["orientation"] = "portrait" if page.get("orientation") == "portrait" else "landscape"
    out["page"]["width_mm"] = _number(page.get("width_mm"), out["page"]["width_mm"])
    out["page"]["height_mm"] = _number(page.get("height_mm"), out["page"]["height_mm"])
    out["page"]["margin_mm"] = max(0.0, _number(page.get("margin_mm"), out["page"]["margin_mm"]))
    for key in ("title", "subtitle", "footer_text"):
        if key in template:
            out[key] = str(template.get(key) or "")
    if isinstance(template.get("fields"), list):
        out["fields"] = [[str(row[0]), str(row[1]) if len(row) > 1 else ""] for row in template["fields"]
                         if isinstance(row, (list, tuple)) and row]
    out["kp_per_page"] = _number(template.get("kp_per_page"), out["kp_per_page"])
    out["overlap_pct"] = min(50.0, max(0.0, _number(template.get("overlap_pct"), out["overlap_pct"])))
    out["kp_tick"] = _number(template.get("kp_tick"), None)
    for key in ("reverse", "legend", "footer", "page_numbers", "summary_page", "sources_page"):
        if key in template:
            out[key] = bool(template[key])
    if isinstance(template.get("panels"), list):
        panels = []
        for panel in template["panels"]:
            if not isinstance(panel, dict) or panel.get("kind") not in PANEL_KINDS:
                continue
            clean = {"kind": panel["kind"]}
            if panel["kind"] not in BAR_KINDS:
                clean["weight"] = max(0.2, _number(panel.get("weight"), 1.0))
                clean["label"] = str(panel.get("label") or "")
                clean["y_min"] = _number(panel.get("y_min"), None)
                clean["y_max"] = _number(panel.get("y_max"), None)
                clean["invert"] = bool(panel.get("invert", panel["kind"] == PANEL_DOL))
            if panel["kind"] == PANEL_DOL:
                for key in ("datasets", "target", "below"):
                    clean[key] = bool(panel.get(key, True))
            if panel["kind"] == PANEL_CHANNEL:
                clean["channel"] = str(panel.get("channel") or "water_depth")
            panels.append(clean)
        out["panels"] = panels
    return out


def problems(template: Dict) -> List[str]:
    """Plain-language reasons the template cannot make pages (empty: it can)."""
    out = []
    t = normalise(template)
    width, height = page_size_mm(t)
    margin = t["page"]["margin_mm"]
    if min(width, height) < 100.0:
        out.append("The page is too small (at least 100 mm each way).")
    if margin * 2 > min(width, height) / 3:
        out.append("The margins leave too little room.")
    if not t["panels"]:
        out.append("Add at least one panel.")
    elif not any(p["kind"] not in BAR_KINDS for p in t["panels"]):
        out.append("Add a DOL or channel panel (bars alone have no axis).")
    if not t["kp_per_page"] or t["kp_per_page"] <= 0:
        out.append("KP per page must be more than 0.")
    for panel in t["panels"]:
        if panel.get("y_min") is not None and panel.get("y_max") is not None and panel["y_min"] >= panel["y_max"]:
            out.append(f"{panel.get('label') or panel['kind']}: the axis minimum must be below the maximum.")
    return out


def page_windows(extent: Tuple[float, float], kp_per_page: float, overlap_pct: float = 0.0
                 ) -> List[Tuple[float, float]]:
    """KP windows covering ``extent``: ``kp_per_page`` each, each page repeating ``overlap_pct`` of the last."""
    lo, hi = sorted(float(v) for v in extent)
    span = float(kp_per_page)
    if span <= 0 or hi <= lo:
        return [(lo, hi if hi > lo else lo + 0.001)]
    step = span * (1.0 - min(max(overlap_pct, 0.0), 50.0) / 100.0)
    windows, start = [], lo
    while True:
        windows.append((start, start + span))
        if start + span >= hi - 1e-9 or len(windows) >= MAX_PAGES:
            break
        start += step
    return windows


def expand(text: str, tokens: Dict[str, str]) -> str:
    """Fill ``{token}`` placeholders; unknown ones stay as typed."""
    def replace(match: "re.Match") -> str:
        key = match.group(1)
        return str(tokens[key]) if key in tokens else match.group(0)
    return re.sub(r"\{([a-z_]+)\}", replace, str(text or ""))


def kp_text(kp: float) -> str:
    return f"{kp:.3f}"


def to_json(template: Dict) -> str:
    return json.dumps(normalise(template), ensure_ascii=False, indent=1, sort_keys=True)


def from_json(text: str) -> Dict:
    return normalise(json.loads(text))


def fingerprint_text(template: Dict) -> str:
    body = normalise(copy.deepcopy(template))
    body.pop("name", None)
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def nice_ticks(lo: float, hi: float, target: int = 6, step: Optional[float] = None) -> List[float]:
    """Round tick values over ``lo..hi``: steps of 1, 2, 2.5 or 5 x 10^n, about ``target`` of them."""
    lo, hi = sorted((float(lo), float(hi)))
    if hi - lo <= 0:
        return [lo]
    if step is None or step <= 0:
        raw = (hi - lo) / max(target, 1)
        power = 10.0 ** math.floor(math.log10(raw))
        step = next((m * power for m in (1.0, 2.0, 2.5, 5.0, 10.0) if m * power >= raw), 10.0 * power)
    first = math.ceil(lo / step - 1e-9) * step
    count = int(math.floor((hi - first) / step + 1e-9)) + 1
    if count > 2000:
        return [lo, hi]
    return [round(first + i * step, 10) for i in range(max(count, 0))]


def tick_label(value: float, step: float) -> str:
    decimals = max(0, min(6, -int(math.floor(math.log10(step))) if step > 0 else 0))
    if step > 0 and abs(step * 10 ** decimals - round(step * 10 ** decimals)) > 1e-9:
        decimals += 1
    return f"{value:.{decimals}f}"


def tokens_help() -> str:
    return ", ".join("{" + t + "}" for t in TOKENS)


def panel_title(panel: Dict) -> str:
    if panel["kind"] == PANEL_CHANNEL:
        return panel.get("label") or panel.get("channel") or "Channel"
    if panel["kind"] == PANEL_DOL:
        return panel.get("label") or "DOL"
    return PANEL_KINDS[panel["kind"]]


def bar_count(panels: Sequence[Dict]) -> int:
    return sum(1 for p in panels if p["kind"] in BAR_KINDS)
