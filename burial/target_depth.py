# -*- coding: utf-8 -*-
"""Target burial depth along the route (pure python).

A plan has one *default* target burial depth (``bp_plan.target_burial_m``)
and optional KP-range overrides kept in the plan's ``params_json`` under
``target_burial_ranges``::

    [{"start_kp": 12.0, "end_kp": 18.5, "depth_m": 3.0, "notes": "TSS"}, …]

A range wins over the default inside it; outside every range the default
applies (or there is no target when the default is blank). Ranges may not
overlap — two targets for the same KP would be ambiguous — and are kept
sorted by start KP. Storing them in ``params_json`` needs no schema change,
and plan edits are change-logged like every other plan parameter.

Everything here is display/reporting input: generation does not read the
target (exclusions decide *where* to bury; the target says *how deep*).
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PARAMS_KEY = "target_burial_ranges"
_TOL_KM = 1e-6
# KPs are shown/picked at 3 dp (1 m): a range ending "at the route end" can
# read up to half a metre past the computed route length.
_ROUTE_END_TOL_KM = 0.0005

Run = Tuple[float, float, Optional[float]]


def _float(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN -> None


def normalise_ranges(value) -> List[Dict]:
    """Clean a stored/entered range list: numeric, start < end, sorted.

    Entries with a missing or non-positive depth, or zero length, are
    dropped (``validate_ranges`` reports them when validating user input).
    """
    out: List[Dict] = []
    for entry in value or []:
        if not isinstance(entry, dict):
            continue
        start = _float(entry.get("start_kp"))
        end = _float(entry.get("end_kp"))
        depth = _float(entry.get("depth_m"))
        if start is None or end is None or depth is None or depth <= 0:
            continue
        lo, hi = sorted((start, end))
        if hi - lo <= _TOL_KM:
            continue
        out.append({"start_kp": lo, "end_kp": hi, "depth_m": depth,
                    "notes": str(entry.get("notes") or "")})
    out.sort(key=lambda r: (r["start_kp"], r["end_kp"]))
    return out


def validate_ranges(value, route_length_km: Optional[float] = None
                    ) -> List[str]:
    """Problems with user-entered ranges (empty list = valid)."""
    problems: List[str] = []
    rows = []
    for index, entry in enumerate(value or [], start=1):
        entry = entry if isinstance(entry, dict) else {}
        start = _float(entry.get("start_kp"))
        end = _float(entry.get("end_kp"))
        depth = _float(entry.get("depth_m"))
        if start is None or end is None:
            problems.append(f"Row {index}: enter both KPs.")
            continue
        if end - start <= _TOL_KM:
            problems.append(f"Row {index}: the end KP must be greater than "
                            "the start KP.")
            continue
        if depth is None or depth <= 0:
            problems.append(f"Row {index}: enter a target depth above 0 m.")
            continue
        if route_length_km and route_length_km > 0 and (
                start < -_ROUTE_END_TOL_KM
                or end > route_length_km + _ROUTE_END_TOL_KM):
            problems.append(f"Row {index}: KP {start:.3f}–{end:.3f} is outside "
                            f"the route (0–{route_length_km:.3f}).")
        rows.append((start, end, index))
    rows.sort()
    for (s1, e1, i1), (s2, e2, i2) in zip(rows, rows[1:]):
        if s2 < e1 - _TOL_KM:
            problems.append(f"Rows {i1} and {i2} overlap (KP {s2:.3f}–"
                            f"{min(e1, e2):.3f}) — a KP can have only one "
                            "target.")
    return problems


def plan_ranges(plan: Dict) -> List[Dict]:
    """The KP-range overrides stored on a plan row."""
    try:
        params = json.loads((plan or {}).get("params_json") or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(params, dict):
        return []
    return normalise_ranges(params.get(PARAMS_KEY))


def plan_default(plan: Dict) -> Optional[float]:
    value = _float((plan or {}).get("target_burial_m"))
    return value if value is not None and value > 0 else None


def target_at(kp: float, default: Optional[float],
              ranges: Sequence[Dict]) -> Optional[float]:
    """Target depth at a KP: the covering range, else the default."""
    for entry in ranges:
        if entry["start_kp"] - _TOL_KM <= kp <= entry["end_kp"] + _TOL_KM:
            return entry["depth_m"]
    return default


def target_runs(default: Optional[float], ranges: Sequence[Dict],
                start_kp: float, end_kp: float) -> List[Run]:
    """Partition ``[start_kp, end_kp]`` into ``(start, end, depth|None)``
    runs; adjacent runs with the same depth are merged."""
    lo, hi = sorted((float(start_kp), float(end_kp)))
    if hi - lo <= _TOL_KM:
        return []
    runs: List[Run] = []
    cursor = lo
    for entry in normalise_ranges(ranges):
        a, b = max(entry["start_kp"], lo), min(entry["end_kp"], hi)
        if b - a <= _TOL_KM:
            continue
        if a - cursor > _TOL_KM:
            runs.append((cursor, a, default))
        runs.append((a, b, entry["depth_m"]))
        cursor = max(cursor, b)
    if hi - cursor > _TOL_KM:
        runs.append((cursor, hi, default))
    merged: List[Run] = []
    for run in runs:
        if merged and merged[-1][2] == run[2] \
                and abs(merged[-1][1] - run[0]) <= _TOL_KM:
            merged[-1] = (merged[-1][0], run[1], run[2])
        else:
            merged.append(run)
    return merged


def depth_span(default: Optional[float], ranges: Sequence[Dict],
               start_kp, end_kp) -> Optional[Tuple[float, float]]:
    """(min, max) target over a KP window, None when it has no target."""
    start, end = _float(start_kp), _float(end_kp)
    if start is None or end is None:
        return None
    depths = [depth for _a, _b, depth in
              target_runs(default, ranges, start, end) if depth is not None]
    if not depths:
        return None
    return min(depths), max(depths)


def format_span(span: Optional[Tuple[float, float]]) -> str:
    if span is None:
        return ""
    lo, hi = span
    return f"{lo:g}" if abs(hi - lo) < 1e-9 else f"{lo:g}–{hi:g}"


def summary_text(default: Optional[float], ranges: Sequence[Dict]) -> str:
    """One line for status labels and reports."""
    base = f"{default:g} m" if default else "none"
    if not ranges:
        return f"Default {base}."
    depths = sorted({r["depth_m"] for r in ranges})
    return (f"Default {base}; {len(ranges)} KP range override(s) "
            f"({', '.join(f'{d:g}' for d in depths)} m).")


def ranges_from_legs(legs: Iterable[Dict]) -> List[Dict]:
    """Ranges from Workbench RPL legs carrying ``target_burial_m``.

    Legs with the same target that touch are merged into one range; legs
    without a target are skipped (the plan default covers them).
    """
    rows = []
    for leg in legs or []:
        depth = _float(leg.get("target_burial_m"))
        start = _float(leg.get("start_kp", leg.get("kp_start")))
        end = _float(leg.get("end_kp", leg.get("kp_end")))
        if depth is None or depth <= 0 or start is None or end is None:
            continue
        lo, hi = sorted((start, end))
        if hi - lo <= _TOL_KM:
            continue
        rows.append({"start_kp": lo, "end_kp": hi, "depth_m": depth,
                     "notes": str(leg.get("label") or "from RPL")})
    rows.sort(key=lambda r: (r["start_kp"], r["end_kp"]))
    merged: List[Dict] = []
    for row in rows:
        if merged and merged[-1]["depth_m"] == row["depth_m"] \
                and row["start_kp"] <= merged[-1]["end_kp"] + _TOL_KM:
            merged[-1]["end_kp"] = max(merged[-1]["end_kp"], row["end_kp"])
        else:
            merged.append(dict(row))
    return merged
