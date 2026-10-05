"""Numeric ground datasets: depth profiles, live KP placement, colour classes.

Pure python (no QGIS). A dataset holds one variable (e.g. CPT su) for many
investigations. Measurements never contain KPs: a separate KP-range or
polygon layer, read live, says where along the route each investigation
applies, so one profile can cover several disconnected intervals. Depth
support is explicit: point samples get a bounded cell, never a linear
interpolation or extrapolation across missing measurements.
"""
from __future__ import annotations

import bisect
import math
import re
import uuid
from collections import defaultdict

from . import attribute_rules


MISSING = {"", "na", "n/a", "null", "none", "nan", "-", "nodata"}

ROLE_LABELS = {"source_id": "investigation ID", "depth": "depth", "base": "depth base",
               "value": "value"}
COLOUR_MODES = ("continuous", "bands", "classes")


def compact(text):
    """ID comparison key for near-match hints: no case, spaces or punctuation."""
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def number(value, *, optional=False, decimal_comma=False, missing=()):
    text = str(value if value is not None else "").strip()
    if text.casefold() in MISSING | {str(v).strip().casefold() for v in missing if str(v).strip()}:
        if optional:
            return None
        raise ValueError("required number is missing")
    result = float(text.replace(",", ".") if decimal_comma else text)
    if not math.isfinite(result):
        if optional:
            return None
        raise ValueError("number must be finite")
    return result


# -- measurements ----------------------------------------------------------------
def column_indices(headers, columns):
    """``{role: index}`` from ``{role: header name}``; unknown names raise."""
    out = {}
    for role, name in (columns or {}).items():
        if not name:
            continue
        if name not in headers:
            raise ValueError(f"The source has no column named '{name}' "
                             f"(the {ROLE_LABELS.get(role, role)} column).")
        out[role] = headers.index(name)
    return out


def import_profiles(rows, mapping, *, dataset_id="", variable="", units="", depth_scale=1.0,
                    sample_support=0.02, decimal_comma=False, missing=(), provenance=None):
    """One profile per investigation from ``rows`` (header row excluded).

    ``mapping`` maps roles to zero-based columns: source_id, depth, value and
    optionally base (interval bottom). Bad rows reject the import with their
    row number; blank or missing-coded values stay missing, never zero. Point
    support is capped by ``sample_support`` and neighbouring midpoints.
    """
    if not math.isfinite(depth_scale) or depth_scale <= 0:
        raise ValueError("Depth scale must be positive.")
    if not math.isfinite(sample_support) or sample_support <= 0:
        raise ValueError("Point sample support must be positive (metres).")
    for role in ("source_id", "depth", "value"):
        if mapping.get(role) is None:
            raise ValueError(f"Choose the {ROLE_LABELS[role]} column.")
    grouped = defaultdict(list)

    def cell(row, role):
        index = mapping.get(role)
        return str(row[index]).strip() if index is not None and index < len(row) else ""

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
            value = number(cell(row, "value"), optional=True, decimal_comma=decimal_comma,
                           missing=missing)
            grouped[source].append({"depth": depth, "top": depth, "base": base, "value": value,
                                    "flags": "" if value is not None else "missing", "row": line})
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Data row {line}: {exc}") from exc
    profiles = []
    for source, samples in sorted(grouped.items()):
        samples.sort(key=lambda s: s["depth"])
        for i, sample in enumerate(samples):
            if i and sample["depth"] == samples[i - 1]["depth"]:
                raise ValueError(f"{source}: depth {sample['depth']:g} m appears twice "
                                 f"(data rows {samples[i - 1]['row']} and {sample['row']})")
            if sample["base"] is None:
                depth = sample["depth"]
                sample["top"] = max(0, depth - sample_support / 2,
                                    (depth + samples[i - 1]["depth"]) / 2 if i else 0)
                sample["base"] = min(depth + sample_support / 2,
                                     (depth + samples[i + 1]["depth"]) / 2
                                     if i + 1 < len(samples) else math.inf)
            if i and sample["top"] < samples[i - 1]["base"] - 1e-10:
                raise ValueError(f"{source}: depth intervals overlap near {sample['top']:g} m")
        identity = f"{dataset_id}␟{source}"
        profiles.append({"profile_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                         "source_id": source, "variable": variable, "units": units,
                         "samples": samples, "provenance": dict(provenance or {})})
    if not profiles:
        raise ValueError("The table has no data rows.")
    return profiles


def profile_stats(profile):
    """Per-investigation numbers for checks: depth range, counts, value range."""
    samples = profile["samples"]
    values = [s["value"] for s in samples if s["value"] is not None]
    base = max((s["base"] for s in samples if math.isfinite(s["base"])), default=None)
    return {"samples": len(samples), "missing": len(samples) - len(values),
            "top": min((s["top"] for s in samples), default=None), "base": base,
            "min": min(values) if values else None, "max": max(values) if values else None}


def summary_text(profiles, units=""):
    if not profiles:
        return "No investigations."
    stats = [profile_stats(p) for p in profiles]
    samples = sum(s["samples"] for s in stats)
    missing = sum(s["missing"] for s in stats)
    lows = [s["min"] for s in stats if s["min"] is not None]
    highs = [s["max"] for s in stats if s["max"] is not None]
    base = max((s["base"] for s in stats if s["base"] is not None), default=0)
    text = (f"{len(profiles)} investigation(s), {samples} depth sample(s) "
            f"({missing} missing), depth 0–{base:g} m")
    if lows:
        text += f", values {min(lows):g}–{max(highs):g} {units}".rstrip()
    return text


# -- placement along the route ----------------------------------------------------
def ranges_to_assignments(ranges, ids, source_ref=""):
    """Assignments from translated ``kp_table.KpRange`` items.

    ``ids`` maps a range's ``ref`` to its investigation ID ("" = missing).
    Returns ``(assignments, notes)``; rows without an ID are counted.
    """
    out, unnamed = [], 0
    for item in ranges:
        source = str(ids.get(item.ref) or "").strip()
        if not source:
            unnamed += 1
            continue
        if item.hi <= item.lo:
            continue
        out.append({"source_id": source, "start_kp": item.lo, "end_kp": item.hi,
                    "src_start_kp": min(item.source_start, item.source_end),
                    "src_end_kp": max(item.source_start, item.source_end),
                    "flags": ", ".join(item.flags), "source_ref": source_ref})
    notes = [f"{unnamed} KP range row(s) have no investigation ID and were skipped"] if unnamed else []
    return out, notes


def coverage_runs(assignments):
    """Sweep into disjoint runs; keep every conflicting assignment visible.

    Adjacent boundaries are half open. Overlaps are ambiguous even when IDs
    match; silently choosing one range would conceal a data problem.
    """
    events = defaultdict(lambda: [[], []])
    for i, row in enumerate(assignments):
        a, b = row["start_kp"], row["end_kp"]
        if not (math.isfinite(a) and math.isfinite(b) and b > a):
            raise ValueError("KP ranges must be finite and increasing.")
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


def unmatched_ids(ids, known):
    """IDs with no imported profile, each with a near match when one exists."""
    folded = {compact(k): k for k in known}
    out = []
    for source in sorted(set(ids) - set(known)):
        near = folded.get(compact(source))
        out.append(f"{source} (did you mean {near}?)" if near else source)
    return out


def check_dataset(profiles, assignments, bounds=None, scope=None):
    """``(rows, notes)``: one row per investigation and route-level findings.

    Rows carry the profile statistics and the KP ranges the investigation is
    placed on. Notes list unmatched IDs both ways, ranges beyond the route,
    overlaps and the share of the scope that is covered.
    """
    by_source = defaultdict(list)
    for row in assignments:
        by_source[row["source_id"]].append(row)
    rows = []
    for profile in sorted(profiles, key=lambda p: p["source_id"]):
        placed = sorted(by_source.get(profile["source_id"], []), key=lambda r: r["start_kp"])
        stats = profile_stats(profile)
        rows.append(dict(stats, source_id=profile["source_id"],
                         ranges=[(r["start_kp"], r["end_kp"]) for r in placed],
                         status="placed" if placed else "no KP range"))
    notes = []
    known = {p["source_id"] for p in profiles}
    missing = unmatched_ids(by_source, known)
    if missing:
        notes.append(f"{len(missing)} KP range ID(s) have no measurements: " + ", ".join(missing[:12])
                     + (" …" if len(missing) > 12 else ""))
    unplaced = [r["source_id"] for r in rows if not r["ranges"]]
    if unplaced:
        notes.append(f"{len(unplaced)} investigation(s) have no KP range: " + ", ".join(unplaced[:12])
                     + (" …" if len(unplaced) > 12 else ""))
    if bounds:
        beyond = sorted({r["source_id"] for r in assignments
                         if r["start_kp"] < bounds[0] - 1e-9 or r["end_kp"] > bounds[1] + 1e-9})
        if beyond:
            notes.append("KP ranges extend beyond the route: " + ", ".join(beyond[:12]))
    runs = coverage_runs(assignments)
    overlaps = [(a, b, active) for a, b, active in runs if len(active) > 1]
    if overlaps:
        shown = "; ".join(f"KP {a:.3f}–{b:.3f} ({', '.join(assignments[i]['source_id'] for i in active)})"
                          for a, b, active in overlaps[:6])
        notes.append(f"{len(overlaps)} overlapping stretch(es), drawn amber with no value chosen: " + shown)
    if scope and scope[1] > scope[0]:
        lo, hi = scope
        covered = sum(max(0.0, min(b, hi) - max(a, lo)) for a, b, _active in runs)
        notes.append(f"KP ranges cover {covered:.3f} of {hi - lo:.3f} km of the plan scope "
                     f"({100 * covered / (hi - lo):.0f}%).")
    return rows, notes


class ProfileIndex:
    """Bisected queries over route runs and measured depth supports."""
    def __init__(self, profiles, assignments):
        self.assignments = assignments
        self.profiles = {p["source_id"]: p for p in profiles}
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
        # Every placed measurement, independent of viewport and depth limits.
        placed = {a["source_id"] for a in self.assignments}
        values = [s["value"] for source, p in self.profiles.items() if source in placed
                  for s in p["samples"] if s["value"] is not None]
        if not values:
            return 0.0, 1.0
        lo, hi = min(values), max(values)
        return (lo, hi) if hi > lo else (lo - max(abs(lo) * .01, .5), hi + max(abs(hi) * .01, .5))

    def cells(self, classes=None, name="value"):
        """Every plotted cell, for export: KP run × depth sample.

        Overlapping stretches list each investigation with status "overlap";
        missing values keep an empty value and status "missing".
        """
        out = []
        for a, b, active in self.runs:
            for j in active:
                assignment = self.assignments[j]
                profile = self.profiles.get(assignment["source_id"])
                if profile is None:
                    out.append({"source_id": assignment["source_id"], "kp_from": a, "kp_to": b,
                                "depth_top_m": None, "depth_base_m": None, "value": None,
                                "class": "", "status": "no measurements"})
                    continue
                for sample in profile["samples"]:
                    item = class_of(sample["value"], classes) if classes else None
                    status = "overlap" if len(active) > 1 else ("missing" if sample["value"] is None else "ok")
                    out.append({"source_id": assignment["source_id"], "kp_from": a, "kp_to": b,
                                "depth_top_m": sample["top"],
                                "depth_base_m": sample["base"] if math.isfinite(sample["base"]) else None,
                                "value": sample["value"],
                                "class": class_label(item, name) if item else "",
                                "status": status})
        return out


# -- colours -----------------------------------------------------------------------
def colour_settings(colours):
    """Display colours with defaults: mode, ramp, bands, limits, classes."""
    colours = dict(colours or {})
    mode = colours.get("mode")
    return {"mode": mode if mode in COLOUR_MODES else "continuous",
            "ramp": colours.get("ramp") or "Viridis",
            "bands": max(2, int(colours.get("bands") or 5)),
            "auto": bool(colours.get("auto", True)),
            "min": float(colours.get("min") or 0.0), "max": float(colours.get("max") or 1.0),
            "classes": list(colours.get("classes") or [])}


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
                value = attribute_rules.to_number(item[key])
                if value is None:
                    raise ValueError(f"Class {n}: '{item[key]}' is not a number.")
                item[key] = value
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


def display_classes(colours):
    """Validated classes when the colours use custom classes, else None."""
    settings = colour_settings(colours)
    if settings["mode"] != "classes" or not settings["classes"]:
        return None
    try:
        return normalise_classes(settings["classes"])
    except ValueError:
        return None


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
