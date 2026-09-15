# -*- coding: utf-8 -*-
"""Ground model data handling for the Burial Planner (pure python).

A ground model is a set of *units*: rectangles (or trapezoids, when the
top/base depths differ between the start and end KP) in the KP × depth-
below-seabed plane, each assigned a soil class. Units are plain dict rows
matching ``schema.GROUND_UNIT_FIELDS``; classes are dict rows matching
``schema.GROUND_CLASS_FIELDS`` (project-scoped, shared by every plan).

This module owns everything that does not need Qt or QGIS: validation and
sorting, point-in-unit lookups (which class is at KP x, depth y), coverage
checks at a target burial depth, the class summary, the CSV/XLSX readers
with header guessing, the horizon-table conversion and the CSV export.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import schema

# -- soil groups and colours ------------------------------------------------
GROUP_ROCK = "rock"
GROUP_GRAVEL = "gravel"
GROUP_SAND = "sand"
GROUP_SILT = "silt"
GROUP_CLAY = "clay"
GROUP_PEAT = "peat"
GROUP_MIXED = "mixed"
GROUP_UNKNOWN = "unknown"
GROUPS: List[str] = [GROUP_SAND, GROUP_SILT, GROUP_CLAY, GROUP_GRAVEL,
                     GROUP_ROCK, GROUP_PEAT, GROUP_MIXED, GROUP_UNKNOWN]
GROUP_LABELS: Dict[str, str] = {
    GROUP_SAND: "Sand", GROUP_SILT: "Silt", GROUP_CLAY: "Clay",
    GROUP_GRAVEL: "Gravel / cobbles", GROUP_ROCK: "Rock", GROUP_PEAT: "Peat / organic",
    GROUP_MIXED: "Mixed / interbedded", GROUP_UNKNOWN: "Unknown",
}
# Conventional geological-map hues so an imported model reads at a glance.
GROUP_COLORS: Dict[str, str] = {
    GROUP_SAND: "#f2d16b", GROUP_SILT: "#c9b98a", GROUP_CLAY: "#8fb4d9",
    GROUP_GRAVEL: "#e0a458", GROUP_ROCK: "#b06f8a", GROUP_PEAT: "#8c6d4f",
    GROUP_MIXED: "#a7c98a", GROUP_UNKNOWN: "#c8c8c8",
}
_AUTO_PALETTE = ["#f2d16b", "#8fb4d9", "#e0a458", "#a7c98a", "#b06f8a",
                 "#c9b98a", "#7fc8c2", "#d98fb4", "#9aa8e6", "#e6b89a"]

_GROUP_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    (GROUP_PEAT, ("peat", "organic")),
    (GROUP_ROCK, ("rock", "bedrock", "chalk", "limestone", "sandstone",
                  "mudstone", "granite", "basalt", "boulder", "cemented")),
    (GROUP_GRAVEL, ("gravel", "cobble", "shingle", "pebble", "till", "diamict")),
    (GROUP_CLAY, ("clay",)),
    (GROUP_SILT, ("silt", "mud")),
    (GROUP_SAND, ("sand",)),
]

CONFIDENCE_LEVELS: List[str] = ["", "high", "medium", "low"]
CONFIDENCE_LABELS: Dict[str, str] = {"": "—", "high": "High",
                                     "medium": "Medium", "low": "Low"}


def guess_group(text: str) -> str:
    """Soil group from a class code/description (keyword order matters)."""
    lowered = (text or "").casefold()
    hits = [group for group, words in _GROUP_KEYWORDS
            if any(word in lowered for word in words)]
    if not hits:
        return GROUP_UNKNOWN
    if len(set(hits)) > 1 and GROUP_ROCK not in hits and GROUP_PEAT not in hits:
        return GROUP_MIXED
    return hits[0]


def auto_color(code: str, group: str = "") -> str:
    """Deterministic colour for a class that has none configured."""
    if group and group in GROUP_COLORS and group != GROUP_UNKNOWN:
        return GROUP_COLORS[group]
    digest = hashlib.md5((code or "").casefold().encode("utf-8")).hexdigest()
    return _AUTO_PALETTE[int(digest[:8], 16) % len(_AUTO_PALETTE)]


def class_lookup(classes: Iterable[Dict]) -> Dict[str, Dict]:
    """Classes keyed by casefolded code."""
    out: Dict[str, Dict] = {}
    for row in classes or []:
        code = str(row.get("code") or "").strip()
        if code:
            out.setdefault(code.casefold(), row)
    return out


def color_for(code: str, classes_by_code: Dict[str, Dict]) -> str:
    row = classes_by_code.get((code or "").casefold())
    if row is not None and row.get("color"):
        return str(row["color"])
    group = str(row.get("group") or "") if row is not None else guess_group(code)
    return auto_color(code, group)


def label_for(code: str, classes_by_code: Dict[str, Dict]) -> str:
    row = classes_by_code.get((code or "").casefold())
    if row is not None and row.get("label"):
        return str(row["label"])
    return code or "(unclassified)"


def missing_classes(units: Iterable[Dict], classes: Iterable[Dict]) -> List[Dict]:
    """Class rows to create for codes the units use but the registry lacks."""
    known = class_lookup(classes)
    created: Dict[str, Dict] = {}
    for unit in units or []:
        code = str(unit.get("soil_class") or "").strip()
        if not code or code.casefold() in known or code.casefold() in created:
            continue
        group = guess_group(f"{code} {unit.get('description') or ''}")
        created[code.casefold()] = {
            "class_id": schema.new_id(), "code": code, "label": code,
            "group": group, "color": auto_color(code, group), "notes": "",
            "seq": len(known) + len(created),
        }
    return list(created.values())


# -- units -------------------------------------------------------------------
def _float_or_none(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def normalise_unit(row: Dict) -> Dict:
    """A unit row with numeric fields coerced and KP order fixed."""
    out = dict(row)
    start = _float_or_none(out.get("start_kp"))
    end = _float_or_none(out.get("end_kp"))
    if start is not None and end is not None and end < start:
        start, end = end, start
        # Keep the trapezoid consistent when the KPs were swapped.
        out["top_m"], out["top_end_m"] = out.get("top_end_m"), out.get("top_m")
        out["base_m"], out["base_end_m"] = out.get("base_end_m"), out.get("base_m")
    out["start_kp"] = start
    out["end_kp"] = end
    top = _float_or_none(out.get("top_m"))
    out["top_m"] = 0.0 if top is None else max(0.0, top)
    for key in ("base_m", "top_end_m", "base_end_m", "src_start_kp",
                "src_end_kp"):
        out[key] = _float_or_none(out.get(key))
    if out["top_end_m"] is not None:
        out["top_end_m"] = max(0.0, out["top_end_m"])
    out["soil_class"] = str(out.get("soil_class") or "").strip()
    for key in ("description", "strength", "confidence", "source_ref",
                "src_rpl", "notes", "rereference_flags"):
        out[key] = str(out.get(key) or "")
    try:
        out["seq"] = int(out.get("seq") or 0)
    except (TypeError, ValueError):
        out["seq"] = 0
    return out


def sort_units(units: Iterable[Dict]) -> List[Dict]:
    """By start KP, then top depth; ``seq`` rewritten to match."""
    rows = [normalise_unit(u) for u in units or []]
    rows.sort(key=lambda u: (u["start_kp"] if u["start_kp"] is not None else 1e12,
                             u["top_m"], u["end_kp"] or 0.0))
    for i, row in enumerate(rows):
        row["seq"] = i
    return rows


def unit_depths_at(unit: Dict, kp: float) -> Tuple[float, Optional[float]]:
    """(top, base) of a unit at a KP, interpolating trapezoid edges."""
    start, end = unit.get("start_kp"), unit.get("end_kp")
    top0 = float(unit.get("top_m") or 0.0)
    top1 = unit.get("top_end_m")
    base0 = unit.get("base_m")
    base1 = unit.get("base_end_m")
    try:
        span = float(end) - float(start)
    except (TypeError, ValueError):
        return top0, base0
    t = 0.0 if span <= 1e-12 else min(1.0, max(0.0, (float(kp) - float(start)) / span))
    top = top0 if top1 is None else top0 + t * (float(top1) - top0)
    if base0 is None:
        base = None
    else:
        base = float(base0) if base1 is None else float(base0) + t * (float(base1) - float(base0))
    return top, base


def units_at_kp(units: Iterable[Dict], kp: float) -> List[Dict]:
    out = []
    for unit in units or []:
        start, end = unit.get("start_kp"), unit.get("end_kp")
        if start is None or end is None:
            continue
        if float(start) - 1e-9 <= kp <= float(end) + 1e-9:
            out.append(unit)
    out.sort(key=lambda u: unit_depths_at(u, kp)[0])
    return out


def unit_at(units: Iterable[Dict], kp: float, depth_m: float) -> Optional[Dict]:
    """The topmost unit containing (kp, depth); open-based units extend
    downwards indefinitely."""
    for unit in units_at_kp(units, kp):
        top, base = unit_depths_at(unit, kp)
        if depth_m + 1e-9 < top:
            continue
        if base is None or depth_m <= base + 1e-9:
            return unit
    return None


def validate_units(units: Iterable[Dict]) -> List[str]:
    """Human-readable problems: missing KPs, inverted depths, and overlaps
    in the KP × depth plane between units of different classes."""
    issues: List[str] = []
    rows = [normalise_unit(u) for u in units or []]
    for i, unit in enumerate(rows):
        tag = f"Unit {i + 1}"
        if unit.get("soil_class"):
            tag += f" ({unit['soil_class']})"
        if unit["start_kp"] is None or unit["end_kp"] is None:
            issues.append(f"{tag}: start and end KP are required.")
            continue
        if unit["end_kp"] - unit["start_kp"] <= 1e-9:
            issues.append(f"{tag}: end KP must be greater than start KP.")
        for top_key, base_key, where in (("top_m", "base_m", "start"),
                                         ("top_end_m", "base_end_m", "end")):
            top = unit.get(top_key)
            base = unit.get(base_key)
            if top is None or base is None:
                continue
            if float(base) <= float(top):
                issues.append(f"{tag}: base must be deeper than top at the "
                              f"{where} KP.")
        if not unit.get("soil_class"):
            issues.append(f"{tag}: no soil class.")
    # Overlap scan on the middle KP of every pairwise KP overlap.
    valid = [u for u in rows if u["start_kp"] is not None and u["end_kp"] is not None]
    valid.sort(key=lambda u: u["start_kp"])
    for i, a in enumerate(valid):
        for b in valid[i + 1:]:
            if b["start_kp"] >= a["end_kp"] - 1e-9:
                break
            lo = max(a["start_kp"], b["start_kp"])
            hi = min(a["end_kp"], b["end_kp"])
            if hi - lo <= 1e-6:
                continue
            mid = (lo + hi) / 2.0
            ta, ba = unit_depths_at(a, mid)
            tb, bb = unit_depths_at(b, mid)
            ba_v = float("inf") if ba is None else ba
            bb_v = float("inf") if bb is None else bb
            if min(ba_v, bb_v) - max(ta, tb) > 1e-6:
                issues.append(
                    f"{a['soil_class'] or 'unit'} and {b['soil_class'] or 'unit'} "
                    f"overlap around KP {schema.format_kp(mid)} "
                    f"(depths {max(ta, tb):.2f}–{min(ba_v, bb_v):.2f} m).")
    return issues


def coverage_gaps(units: Iterable[Dict], start_kp: float, end_kp: float,
                  depth_m: float = 0.0, step_km: float = 0.01) -> List[Tuple[float, float]]:
    """KP ranges inside the scope with no unit at ``depth_m``.

    Sampled at ``step_km`` (10 m default) — cheap enough for any plan and
    exact enough for a coverage check that only feeds a warning.
    """
    lo, hi = sorted((float(start_kp), float(end_kp)))
    if hi - lo <= 1e-9:
        return []
    rows = [normalise_unit(u) for u in units or []]
    rows = [u for u in rows if u["start_kp"] is not None and u["end_kp"] is not None]
    gaps: List[Tuple[float, float]] = []
    n = max(1, int(math.ceil((hi - lo) / step_km)))
    gap_start: Optional[float] = None
    for i in range(n + 1):
        kp = min(hi, lo + i * step_km)
        covered = unit_at(rows, kp, depth_m) is not None
        if not covered and gap_start is None:
            gap_start = kp
        elif covered and gap_start is not None:
            gaps.append((gap_start, kp))
            gap_start = None
    if gap_start is not None:
        gaps.append((gap_start, hi))
    return [(a, b) for a, b in gaps if b - a > 1e-9]


def summarise_by_class(units: Iterable[Dict], depth_m: Optional[float] = None,
                       start_kp: Optional[float] = None,
                       end_kp: Optional[float] = None,
                       step_km: float = 0.01) -> List[Dict]:
    """Route length per class: at the seabed (``depth_m`` None → top units)
    or at a given depth below seabed, optionally clipped to a KP window."""
    rows = [normalise_unit(u) for u in units or []]
    rows = [u for u in rows if u["start_kp"] is not None and u["end_kp"] is not None]
    if not rows:
        return []
    lo = min(u["start_kp"] for u in rows) if start_kp is None else float(start_kp)
    hi = max(u["end_kp"] for u in rows) if end_kp is None else float(end_kp)
    lo, hi = sorted((lo, hi))
    totals: Dict[str, float] = {}
    n = max(1, int(math.ceil((hi - lo) / step_km)))
    probe = 0.0 if depth_m is None else float(depth_m)
    for i in range(n):
        kp = lo + (i + 0.5) * step_km
        if kp > hi:
            break
        hit = unit_at(rows, kp, probe)
        code = hit["soil_class"] if hit else ""
        totals[code] = totals.get(code, 0.0) + min(step_km, hi - kp + step_km / 2.0)
    out = [{"soil_class": code, "length_km": length}
           for code, length in totals.items()]
    out.sort(key=lambda r: -r["length_km"])
    return out


# -- CSV / XLSX import -------------------------------------------------------
TARGET_START = "start_kp"
TARGET_END = "end_kp"
TARGET_TOP = "top_m"
TARGET_BASE = "base_m"
TARGET_TOP_END = "top_end_m"
TARGET_BASE_END = "base_end_m"
TARGET_CLASS = "soil_class"
TARGET_DESCRIPTION = "description"
TARGET_STRENGTH = "strength"
TARGET_CONFIDENCE = "confidence"
TARGET_SOURCE = "source_ref"
TARGET_NOTES = "notes"
TARGET_KP = "kp"  # horizon tables: one station per row

IMPORT_TARGETS: List[Tuple[str, str, bool]] = [
    # (key, label, required for the interval format)
    (TARGET_START, "Start KP", True),
    (TARGET_END, "End KP", True),
    (TARGET_CLASS, "Soil class / unit", True),
    (TARGET_TOP, "Top depth (m below seabed)", False),
    (TARGET_BASE, "Base depth (m below seabed)", False),
    (TARGET_TOP_END, "Top depth at end KP", False),
    (TARGET_BASE_END, "Base depth at end KP", False),
    (TARGET_DESCRIPTION, "Description", False),
    (TARGET_STRENGTH, "Strength / density", False),
    (TARGET_CONFIDENCE, "Confidence", False),
    (TARGET_SOURCE, "Source reference", False),
    (TARGET_NOTES, "Notes", False),
]

_HEADER_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    TARGET_START: ("start_kp", "kp_start", "kp from", "kp_from", "from kp",
                   "from_kp", "start kp", "kpstart", "kp1", "kp_a", "from",
                   "start", "kp (from)", "begin kp"),
    TARGET_END: ("end_kp", "kp_end", "kp to", "kp_to", "to kp", "to_kp",
                 "end kp", "kpend", "kp2", "kp_b", "to", "end", "kp (to)"),
    TARGET_CLASS: ("soil_class", "soil class", "class", "unit", "soil unit",
                   "soil_unit", "geotechnical unit", "geo unit", "lithology",
                   "soil type", "soil_type", "sediment", "classification",
                   "ground unit", "layer", "soil"),
    TARGET_TOP: ("top_m", "top", "depth_from", "depth from", "from depth",
                 "top depth", "top (m)", "depth top", "z_top", "top_bsb",
                 "top bsb", "upper"),
    TARGET_BASE: ("base_m", "base", "depth_to", "depth to", "to depth",
                  "base depth", "base (m)", "bottom", "depth base", "z_base",
                  "base_bsb", "base bsb", "lower", "thickness"),
    TARGET_TOP_END: ("top_end_m", "top end", "top at end", "top_end"),
    TARGET_BASE_END: ("base_end_m", "base end", "base at end", "base_end"),
    TARGET_DESCRIPTION: ("description", "desc", "soil description",
                         "lithological description", "comment"),
    TARGET_STRENGTH: ("strength", "su", "shear strength", "undrained shear",
                      "cu", "density", "relative density", "qc", "spt"),
    TARGET_CONFIDENCE: ("confidence", "certainty", "quality"),
    TARGET_SOURCE: ("source_ref", "source", "reference", "ref", "document",
                    "report", "borehole", "cpt id", "sample"),
    TARGET_NOTES: ("notes", "note", "remarks", "remark"),
    TARGET_KP: ("kp", "chainage", "kp (km)", "kp_km", "station", "distance"),
}


def read_table_file(path: str, sheet: Optional[str] = None) -> Tuple[List[str], List[List[str]], List[str]]:
    """``(headers, rows, sheet_names)`` from a CSV/TSV/TXT or XLSX file.

    Cells come back as stripped strings; empty trailing rows are dropped.
    XLSX uses the vendored openpyxl lazily (never at import time).
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(path, sheet)
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        text = handle.read()
    headers, rows = parse_delimited_text(text)
    return headers, rows, []


def parse_delimited_text(text: str) -> Tuple[List[str], List[List[str]]]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return [], []
    sample = "\n".join(lines[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO("\n".join(lines)), dialect)
    table = [[cell.strip() for cell in row] for row in reader]
    table = [row for row in table if any(cell for cell in row)]
    if not table:
        return [], []
    headers = table[0]
    width = max(len(r) for r in table)
    headers = headers + [""] * (width - len(headers))
    rows = [r + [""] * (width - len(r)) for r in table[1:]]
    return headers, rows


def _read_xlsx(path: str, sheet: Optional[str]) -> Tuple[List[str], List[List[str]], List[str]]:
    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover — environment specific
        raise ValueError(
            "openpyxl is required to read Excel files but could not be "
            f"imported. Ensure the plugin's lib/ folder is present. ({exc})")
    if getattr(openpyxl, "LXML", False):
        raise ValueError(
            "Excel support was loaded with an unsafe native XML backend. "
            "Restart QGIS once to activate the safe workbook reader.")
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        names = list(book.sheetnames)
        ws = book[sheet] if sheet and sheet in names else book[names[0]]
        table: List[List[str]] = []
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).strip() for v in row]
            if any(cells):
                table.append(cells)
    finally:
        try:
            book.close()
        except Exception:
            pass
    if not table:
        return [], [], names
    width = max(len(r) for r in table)
    headers = [h for h in table[0]] + [""] * (width - len(table[0]))
    # Skip leading title rows: the header row is the first with ≥2 texts.
    start = 0
    for i, row in enumerate(table[:10]):
        if sum(1 for c in row if c) >= 2:
            headers = row + [""] * (width - len(row))
            start = i
            break
    rows = [r + [""] * (width - len(r)) for r in table[start + 1:]]
    return headers, rows, names


def _norm_header(text: str) -> str:
    out = (text or "").strip().casefold().replace("_", " ")
    # Drop unit/qualifier brackets: "KP From (km)" -> "kp from".
    out = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", out)
    out = out.replace(":", " ")
    return " ".join(out.split())


def guess_mapping(headers: Sequence[str]) -> Dict[str, int]:
    """Best-effort target → column index from header names."""
    normed = [_norm_header(h) for h in headers]
    mapping: Dict[str, int] = {}
    used: set = set()
    # Exact matches first, then substring matches, longest synonym first.
    for exact in (True, False):
        for target, synonyms in _HEADER_SYNONYMS.items():
            if target in mapping:
                continue
            for syn in sorted(synonyms, key=len, reverse=True):
                s = _norm_header(syn)
                for i, h in enumerate(normed):
                    if i in used or not h:
                        continue
                    hit = (h == s) if exact else (len(s) >= 3 and s in h)
                    if hit:
                        mapping[target] = i
                        used.add(i)
                        break
                if target in mapping:
                    break
    # "thickness" mapped to base is only right when top is known; leave it.
    return mapping


def _clean_number(text: str) -> Optional[float]:
    cell = (text or "").strip().replace(",", ".")
    if not cell:
        return None
    for unit in ("km", "m"):
        if cell.lower().endswith(unit):
            cell = cell[:-len(unit)].strip()
    try:
        return float(cell)
    except ValueError:
        return None


def rows_to_units(rows: Sequence[Sequence[str]], mapping: Dict[str, int],
                  kp_in_metres: bool = False, thickness_as_base: bool = False,
                  default_source: str = "") -> Tuple[List[Dict], List[str]]:
    """Interval-format rows → unit dicts; returns ``(units, problems)``."""
    units: List[Dict] = []
    problems: List[str] = []

    def cell(row, target) -> str:
        idx = mapping.get(target)
        if idx is None or idx < 0 or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    for n, row in enumerate(rows, start=2):
        start = _clean_number(cell(row, TARGET_START))
        end = _clean_number(cell(row, TARGET_END))
        code = cell(row, TARGET_CLASS)
        if start is None and end is None and not code:
            continue
        if start is None or end is None:
            problems.append(f"Row {n}: start/end KP missing or not numeric.")
            continue
        if kp_in_metres:
            start, end = start / 1000.0, end / 1000.0
        top = _clean_number(cell(row, TARGET_TOP))
        base = _clean_number(cell(row, TARGET_BASE))
        if thickness_as_base and base is not None:
            base = (top or 0.0) + base
        unit = {
            "unit_id": schema.new_id(),
            "start_kp": start, "end_kp": end,
            "top_m": 0.0 if top is None else top,
            "base_m": base,
            "top_end_m": _clean_number(cell(row, TARGET_TOP_END)),
            "base_end_m": _clean_number(cell(row, TARGET_BASE_END)),
            "soil_class": code,
            "description": cell(row, TARGET_DESCRIPTION),
            "strength": cell(row, TARGET_STRENGTH),
            "confidence": _norm_confidence(cell(row, TARGET_CONFIDENCE)),
            "source_ref": cell(row, TARGET_SOURCE) or default_source,
            "notes": cell(row, TARGET_NOTES),
        }
        if not code:
            problems.append(f"Row {n}: no soil class (imported as unclassified).")
        units.append(normalise_unit(unit))
    return units, problems


def horizons_to_units(rows: Sequence[Sequence[str]], kp_index: int,
                      horizons: Sequence[Tuple[int, str]],
                      kp_in_metres: bool = False,
                      default_source: str = "") -> Tuple[List[Dict], List[str]]:
    """Horizon-table rows (one KP station per row, one depth column per
    horizon top, ordered shallow → deep) → trapezoid units between
    consecutive stations. ``horizons``: ``[(column_index, class_code)]``;
    the unit *below* horizon i carries class i and its base is horizon
    i+1 (open for the last one). A blank depth at a station ends the unit
    there (no interpolation across missing picks)."""
    stations: List[Tuple[float, List[Optional[float]]]] = []
    problems: List[str] = []
    for n, row in enumerate(rows, start=2):
        kp = _clean_number(row[kp_index] if kp_index < len(row) else "")
        if kp is None:
            if any(str(c).strip() for c in row):
                problems.append(f"Row {n}: KP missing or not numeric.")
            continue
        if kp_in_metres:
            kp /= 1000.0
        depths = [_clean_number(row[i] if i < len(row) else "") for i, _c in horizons]
        stations.append((kp, depths))
    stations.sort(key=lambda s: s[0])
    units: List[Dict] = []
    for (kp0, d0), (kp1, d1) in zip(stations, stations[1:]):
        if kp1 - kp0 <= 1e-9:
            continue
        for i, (_col, code) in enumerate(horizons):
            top0, top1 = d0[i], d1[i]
            if top0 is None or top1 is None:
                continue
            base0 = d0[i + 1] if i + 1 < len(horizons) else None
            base1 = d1[i + 1] if i + 1 < len(horizons) else None
            if (base0 is None) != (base1 is None):
                base0 = base1 = None if base0 is None or base1 is None else base0
            unit = {
                "unit_id": schema.new_id(),
                "start_kp": kp0, "end_kp": kp1,
                "top_m": top0, "top_end_m": None if abs(top1 - top0) < 1e-9 else top1,
                "base_m": base0,
                "base_end_m": (None if base0 is None or base1 is None
                               or abs(base1 - base0) < 1e-9 else base1),
                "soil_class": code, "description": "", "strength": "",
                "confidence": "", "source_ref": default_source, "notes": "",
            }
            units.append(normalise_unit(unit))
    return units, problems


def _norm_confidence(text: str) -> str:
    lowered = (text or "").strip().casefold()
    if not lowered:
        return ""
    if lowered.startswith("h") or lowered in ("1", "a"):
        return "high"
    if lowered.startswith("m") or lowered in ("2", "b"):
        return "medium"
    if lowered.startswith("l") or lowered in ("3", "c"):
        return "low"
    return ""


def class_runs(units: Iterable[Dict], depth_m: float, start_kp: float,
               end_kp: float, fine_step_km: float = 0.025
               ) -> List[Tuple[float, float, str, int]]:
    """Consecutive KP runs of one soil class at ``depth_m`` over a window.

    Breakpoints are the unit boundaries inside the window, so flat units
    resolve exactly; when any unit slopes (``*_end_m`` set) the intervals
    are additionally subdivided at ``fine_step_km`` so the class change
    along a sloping horizon is picked up. Returns ``(start, end, code,
    unit_count)`` with uncovered stretches omitted and adjacent same-class
    pieces merged — the shape a map overlay wants.
    """
    lo, hi = sorted((float(start_kp), float(end_kp)))
    if hi - lo <= 1e-9:
        return []
    rows = [normalise_unit(u) for u in units or []]
    rows = [u for u in rows if u["start_kp"] is not None and u["end_kp"] is not None
            and u["end_kp"] > lo and u["start_kp"] < hi]
    if not rows:
        return []
    points = {lo, hi}
    for unit in rows:
        for kp in (unit["start_kp"], unit["end_kp"]):
            if lo < kp < hi:
                points.add(kp)
    sloping = any(u.get("top_end_m") is not None or u.get("base_end_m") is not None
                  for u in rows)
    ordered = sorted(points)
    runs: List[List] = []
    for a, b in zip(ordered, ordered[1:]):
        if b - a <= 1e-9:
            continue
        pieces = [(a, b)]
        if sloping and b - a > fine_step_km:
            n = int(math.ceil((b - a) / fine_step_km))
            pieces = [(a + i * (b - a) / n, a + (i + 1) * (b - a) / n) for i in range(n)]
        for pa, pb in pieces:
            hit = unit_at(rows, (pa + pb) / 2.0, float(depth_m))
            code = hit["soil_class"] if hit is not None else ""
            if runs and runs[-1][2] == code and abs(runs[-1][1] - pa) < 1e-9:
                runs[-1][1] = pb
                if hit is not None and hit is not runs[-1][3][-1]:
                    runs[-1][3].append(hit)
            else:
                runs.append([pa, pb, code, [hit] if hit is not None else []])
    return [(a, b, code, len(hits)) for a, b, code, hits in runs if code]


# -- KP re-referencing -------------------------------------------------------
def rereference_units(units: Iterable[Dict], kp_map, source_label: str = "",
                      use_source_kps: bool = True,
                      stretch_tol: float = 0.10) -> Tuple[List[Dict], Dict[str, int]]:
    """Map units through a ``KpMap`` onto the plan's current route (see
    ``kp_rereference.rereference_rows`` for the provenance rules)."""
    from . import kp_rereference as kr

    mapped, tally = kr.rereference_rows(
        [normalise_unit(u) for u in units or []], kp_map,
        source_label=source_label, use_source_kps=use_source_kps,
        stretch_tol=stretch_tol)
    return [normalise_unit(row) for row in mapped], tally


# -- CSV export --------------------------------------------------------------
GROUND_COLUMNS: List[str] = [
    "seq", "start_kp", "end_kp", "top_m", "base_m", "top_end_m", "base_end_m",
    "soil_class", "description", "strength", "confidence", "source_ref",
    "src_start_kp", "src_end_kp", "src_rpl", "rereference_flags", "notes",
]


def _fmt(value, places: int = 3) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.{places}f}"
    except (TypeError, ValueError):
        return str(value)


def units_csv(plan: Dict, units: Sequence[Dict],
              classes: Sequence[Dict] = ()) -> str:
    """Ground-model units with the standard ``# key: value`` header."""
    lines = [
        f"# plan: {plan.get('name') or ''}",
        f"# rpl: {plan.get('rpl_name') or ''} {plan.get('rpl_revision') or ''}".rstrip(),
        f"# exported_utc: {schema.utc_now_iso()}",
        "# kp: km on the plan's current route; depths: m below seabed",
    ]
    by_code = class_lookup(classes)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(GROUND_COLUMNS + ["class_label", "group"])
    for unit in sort_units(units):
        row = []
        for key in GROUND_COLUMNS:
            value = unit.get(key)
            if key in ("start_kp", "end_kp", "src_start_kp", "src_end_kp"):
                row.append(_fmt(value, 3))
            elif key in ("top_m", "base_m", "top_end_m", "base_end_m"):
                row.append(_fmt(value, 2))
            else:
                row.append("" if value is None else str(value))
        cls = by_code.get(unit.get("soil_class", "").casefold())
        row.append(str(cls.get("label") or "") if cls else "")
        row.append(str(cls.get("group") or "") if cls else "")
        writer.writerow(row)
    return "\n".join(lines) + "\n" + out.getvalue()
