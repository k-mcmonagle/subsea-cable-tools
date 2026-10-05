"""Numeric investigation profiles and route coverage, independent of QGIS.

Source measurements never contain KPs. Assignments name investigations, so
one profile can be used over several disconnected intervals or several plans.
Depth support is explicit: point samples have bounded cells, never a linear
interpolation or extrapolation across missing measurements.
"""
from __future__ import annotations

import bisect
import json
import math
import re
import uuid
from collections import defaultdict

from . import attribute_rules


MISSING = {"", "na", "n/a", "null", "none", "nan", "-", "nodata"}

ROLE_LABELS = {"source_id": "investigation ID", "depth": "depth", "start_kp": "start KP",
               "end_kp": "end KP"}


def compact(text):
    """ID comparison key for near-match hints: no case, spaces or punctuation."""
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def number(value, *, optional=False, decimal_comma=False, missing=()):
    text = str(value if value is not None else "").strip()
    if text.casefold() in MISSING | {str(v).casefold() for v in missing}:
        if optional:
            return None
        raise ValueError("required number is missing")
    result = float(text.replace(",", ".") if decimal_comma else text)
    if not math.isfinite(result):
        if optional:
            return None
        raise ValueError("number must be finite")
    return result


def import_profiles(rows, mapping, *, variables=(), variable="", units="",
                    depth_scale=1.0, sample_support=0.02, decimal_comma=False,
                    missing=(), provenance=None):
    """Import wide columns ``[(column, variable, units)]`` or long-form rows.

    ``mapping`` maps roles to zero-based columns: source_id, depth, base
    (optional interval bottom), variable/value/units (long format), flags.
    Bad rows reject the import with row numbers; missing values are retained.
    Point support is capped by sample_support and neighbouring midpoints.
    """
    if not math.isfinite(depth_scale) or depth_scale <= 0:
        raise ValueError("Depth scale must be positive.")
    if not math.isfinite(sample_support) or sample_support <= 0:
        raise ValueError("Point sample support must be positive (metres).")
    for role in ("source_id", "depth"):
        if mapping.get(role) is None:
            raise ValueError(f"Map the {ROLE_LABELS.get(role, role)} column.")
    if not variables and mapping.get("value") is None:
        raise ValueError("Map a value column or select wide-format variables.")
    grouped = defaultdict(list)

    def cell(row, role, default=""):
        index = mapping.get(role)
        return str(row[index]).strip() if index is not None and index < len(row) else default

    for line, row in enumerate(rows, 1):
        if not any(str(v).strip() for v in row):
            continue
        try:
            source = cell(row, "source_id")
            if not source:
                raise ValueError("investigation ID is missing")
            depth = number(cell(row, "depth"), decimal_comma=decimal_comma) * depth_scale
            base = (number(cell(row, "base"), decimal_comma=decimal_comma) * depth_scale
                    if mapping.get("base") is not None else None)
            if depth < 0 or (base is not None and base <= depth):
                raise ValueError("depth must be nonnegative and base deeper than top")
            values = ([(name, unit, row[col] if col < len(row) else "")
                       for col, name, unit in variables] if variables else
                      [(cell(row, "variable", variable), cell(row, "units", units), cell(row, "value"))])
            for name, unit, raw in values:
                name, unit = name.strip(), unit.strip()
                if not name:
                    raise ValueError("variable name is missing")
                value = number(raw, optional=True, decimal_comma=decimal_comma, missing=missing)
                flags = cell(row, "flags")
                if value is None:
                    flags = "; ".join(filter(None, (flags, "missing")))
                grouped[source, name, unit].append({
                    "depth": depth, "top": depth, "base": base, "value": value,
                    "flags": flags, "row": line})
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Data row {line}: {exc}") from exc
    profiles = []
    for (source, name, unit), samples in sorted(grouped.items()):
        samples.sort(key=lambda s: s["depth"])
        for i, sample in enumerate(samples):
            if i and sample["depth"] == samples[i - 1]["depth"]:
                raise ValueError(f"{source} / {name}: duplicate depth {sample['depth']:g} m")
            if sample["base"] is None:
                depth = sample["depth"]
                sample["top"] = max(0, depth - sample_support / 2,
                                    (depth + samples[i - 1]["depth"]) / 2 if i else 0)
                sample["base"] = min(depth + sample_support / 2,
                                     (depth + samples[i + 1]["depth"]) / 2
                                     if i + 1 < len(samples) else math.inf)
            if i and sample["top"] < samples[i - 1]["base"] - 1e-10:
                raise ValueError(f"{source} / {name}: overlapping depth intervals")
        identity = json.dumps([source, name, unit], ensure_ascii=False)
        profiles.append({"profile_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                         "source_id": source, "variable": name, "units": unit,
                         "samples": samples, "provenance": dict(provenance or {})})
    if not profiles:
        raise ValueError("No profiles found.")
    return profiles


def import_assignments(rows, mapping, kp_map=None, *, kp_scale=1.0, decimal_comma=False,
                       source_ref=""):
    for role in ("source_id", "start_kp", "end_kp"):
        if mapping.get(role) is None:
            raise ValueError(f"Map the {ROLE_LABELS.get(role, role)} column.")
    result = []
    for line, row in enumerate(rows, 1):
        try:
            source = str(row[mapping["source_id"]]).strip()
            a = number(row[mapping["start_kp"]], decimal_comma=decimal_comma) * kp_scale
            b = number(row[mapping["end_kp"]], decimal_comma=decimal_comma) * kp_scale
            if not source or b <= a:
                raise ValueError("ID is required and end KP must exceed start KP")
            start, end, flags = kp_map.map_range(a, b) if kp_map else (a, b, [])
            if end <= start:
                raise ValueError("KP mapping collapses or reverses the interval")
            result.append({"source_id": source, "start_kp": start, "end_kp": end,
                           "src_start_kp": a, "src_end_kp": b, "source_ref": source_ref,
                           "flags": "; ".join(flags)})
        except (ValueError, IndexError, TypeError) as exc:
            raise ValueError(f"Assignment row {line}: {exc}") from exc
    return result


def coverage_runs(assignments):
    """Sweep into disjoint runs; keep every conflicting assignment visible.

    Adjacent boundaries are half open. Overlaps are ambiguous even when IDs
    match; silently choosing the last polygon would conceal a data problem.
    """
    events = defaultdict(lambda: [[], []])
    for i, row in enumerate(assignments):
        a, b = row["start_kp"], row["end_kp"]
        if not (math.isfinite(a) and math.isfinite(b) and b > a):
            raise ValueError("Assignment intervals must be finite and increasing.")
        events[a][1].append(i)
        events[b][0].append(i)
    active, runs, previous = set(), [], None
    for kp in sorted(events):
        if previous is not None and active:
            runs.append((previous, kp, tuple(sorted(active))))
        ends, starts = events[kp]
        active.difference_update(ends)
        active.update(starts)
        previous = kp
    return runs


def assignment_issues(assignments, profiles, bounds=None):
    sources = {p["source_id"] for p in profiles}
    issues = []
    for row in assignments:
        if row["source_id"] not in sources:
            issues.append(f"Unmatched ID: {row['source_id']}")
        if bounds and (row["start_kp"] < bounds[0] or row["end_kp"] > bounds[1]):
            issues.append(f"{row['source_id']}: assignment extends beyond the selected route")
    for a, b, active in coverage_runs(assignments):
        if len(active) > 1:
            issues.append(f"Overlap KP {a:.3f}–{b:.3f}: " + ", ".join(assignments[i]["source_id"] for i in active))
    assigned = {r["source_id"] for r in assignments}
    issues.extend(f"Unassigned profile: {s}" for s in sorted(sources - assigned))
    return list(dict.fromkeys(issues))


class ProfileIndex:
    """Bisected queries over route runs and measured depth supports."""
    def __init__(self, profiles, assignments, variable):
        self.assignments = assignments
        self.profiles = {p["source_id"]: p for p in profiles
                         if (p["variable"], p["units"]) == tuple(variable)}
        self.runs = coverage_runs(assignments)
        self.starts = [r[0] for r in self.runs]
        self.depths = {source: [s["top"] for s in p["samples"]] for source, p in self.profiles.items()}

    def sample(self, source, depth):
        profile = self.profiles.get(source)
        if not profile:
            return None
        i = bisect.bisect_right(self.depths[source], depth) - 1
        if i >= 0 and depth < profile["samples"][i]["base"]:
            return profile["samples"][i]
        return None

    def at(self, kp, depth):
        i = bisect.bisect_right(self.starts, kp) - 1
        if i < 0 or kp >= self.runs[i][1]:
            return []
        return [(self.assignments[j], self.sample(self.assignments[j]["source_id"], depth))
                for j in self.runs[i][2]]

    def limits(self):
        # Full assigned dataset, independent of viewport and depth limits.
        assigned = {a["source_id"] for a in self.assignments}
        values = [s["value"] for source, p in self.profiles.items() if source in assigned
                  for s in p["samples"] if s["value"] is not None]
        if not values:
            return 0.0, 1.0
        lo, hi = min(values), max(values)
        return (lo, hi) if hi > lo else (lo - max(abs(lo) * .01, .5), hi + max(abs(hi) * .01, .5))


def normalise_classes(rows):
    """Validated colour classes, in row order (the first matching row wins).

    Each class is an ``attribute_rules`` range — ``min``/``max`` with
    ``min_inclusive``/``max_inclusive`` (either side open) — plus a
    ``colour`` and optional ``label``, as in the Exclusions value ranges.
    """
    out = []
    for n, row in enumerate(rows, 1):
        item = {"min": row.get("min"), "max": row.get("max"),
                "min_inclusive": bool(row.get("min_inclusive", True)),
                "max_inclusive": bool(row.get("max_inclusive", True))}
        for key in ("min", "max"):
            if item[key] is not None:
                number = attribute_rules.to_number(item[key])
                if number is None:
                    raise ValueError(f"Class {n}: '{item[key]}' is not a number.")
                item[key] = number
        if item["min"] is None and item["max"] is None:
            raise ValueError(f"Class {n}: enter a From or To value.")
        problem = attribute_rules.validate_rule(item)
        if problem:
            raise ValueError(f"Class {n}: {problem}.")
        colour = str(row.get("colour") or "")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
            raise ValueError(f"Class {n}: colour must be #rrggbb.")
        item.update(colour=colour.lower(), label=str(row.get("label") or "").strip())
        out.append(item)
    if not out:
        raise ValueError("Add at least one class.")
    return out


def classes_from_breaks(breaks, colours):
    """Classes either side of the sorted, distinct ``breaks``: ≥ a … < b."""
    edges = sorted(set(breaks))
    if not edges:
        raise ValueError("Enter at least one break value.")
    bounds = list(zip([None] + edges, edges + [None]))
    return [{"min": lo, "max": hi, "min_inclusive": True, "max_inclusive": False,
             "colour": colours[i % len(colours)], "label": ""}
            for i, (lo, hi) in enumerate(bounds)]


def class_label(item, name="value"):
    return item.get("label") or attribute_rules.describe_rule(item, name)


def class_of(value, classes):
    """The first class containing ``value``, or None (missing or uncovered)."""
    if value is None:
        return None
    return attribute_rules.first_matching_rule(classes, value)


def _edges(item):
    lo = (-math.inf, False) if item["min"] is None else (item["min"], item["min_inclusive"])
    hi = (math.inf, False) if item["max"] is None else (item["max"], item["max_inclusive"])
    return lo, hi


def class_coverage(classes, name="value"):
    """Readable notes on value ranges no class covers, and overlapping rows."""
    def text(lo, hi):
        rule = {"min": None if lo[0] == -math.inf else lo[0], "max": None if hi[0] == math.inf else hi[0],
                "min_inclusive": lo[1], "max_inclusive": hi[1]}
        if rule["min"] is not None and rule["min"] == rule["max"]:
            return f"{name} = {rule['min']:g}"
        return attribute_rules.describe_rule(rule, name)

    ordered = sorted(((_edges(c), i + 1) for i, c in enumerate(classes)),
                     key=lambda e: (e[0][0][0], not e[0][0][1]))
    gaps, overlaps = [], []
    reach, reach_row = (-math.inf, False), None
    for (lo, hi), row in ordered:
        if lo[0] > reach[0] or (lo[0] == reach[0] and not lo[1] and not reach[1]):
            if not (lo[0] == -math.inf and reach[0] == -math.inf):
                # Uncovered between the reach and this lower bound.
                gaps.append(text((reach[0], not reach[1]), (lo[0], not lo[1])))
        elif reach_row is not None and (lo[0] < reach[0] or (lo[0] == reach[0] and lo[1] and reach[1])):
            overlaps.append(f"rows {reach_row} and {row}")
        if hi[0] > reach[0] or (hi[0] == reach[0] and hi[1] and not reach[1]):
            reach, reach_row = hi, row
    if reach[0] < math.inf:
        gaps.append(text((reach[0], not reach[1]), (math.inf, False)))
    notes = []
    if gaps:
        notes.append("No class covers " + "; ".join(gaps) + " (drawn dark grey).")
    if overlaps:
        notes.append("Overlapping " + "; ".join(overlaps) + ": the first matching row's colour is used.")
    return notes


def scheme_key(variable):
    return json.dumps(list(variable or ["", ""]), ensure_ascii=False)


def display_mode(settings):
    """Colour mode of saved display settings; older plans stored only bands."""
    mode = settings.get("colour_mode")
    if mode in ("continuous", "bands", "classes"):
        return mode
    return "bands" if (settings.get("bands") or 0) >= 2 else "continuous"


def display_classes(settings):
    """The selected variable's classes when classes are shown, else None."""
    if display_mode(settings) != "classes":
        return None
    scheme = (settings.get("class_schemes") or {}).get(scheme_key(settings.get("variable")))
    try:
        return normalise_classes(scheme) if scheme else None
    except ValueError:
        return None


def unmatched_ids(ids, known):
    """IDs with no imported profile, each with a near match when one exists."""
    folded = {compact(k): k for k in known}
    out = []
    for source in sorted(set(ids) - set(known)):
        near = folded.get(compact(source))
        out.append(f"{source} (did you mean {near}?)" if near else source)
    return out
