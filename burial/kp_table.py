# -*- coding: utf-8 -*-
"""KP-range tables: reading, units and RPL translation (pure python).

A KP-range table is any registered input with start and end KP fields,
usually without geometry (a desktop-study hazard list, a client schedule).
Its KPs were quoted against one RPL. The criterion or check that reads it
records that RPL in its config (``kp_rpl_id``, a readable
``kp_rpl_label`` and its start KP at that time, ``kp_rpl_start_kp``), and
every read translates the ranges onto the plan's
route through a :class:`kp_rereference.KpMap` — identity when the table
already refers to the plan's RPL.

Rows whose KPs cannot be read are counted and reported, never silently
dropped: a criterion that fires nowhere because its fields are wrong must
say so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

KP_REF_KEY = "kp_rpl_id"
KP_REF_LABEL_KEY = "kp_rpl_label"
KP_REF_START_KEY = "kp_rpl_start_kp"  # the RPL's start KP when referenced
KP_UNIT_KEY = "kp_unit"
KP_UNIT_KM = "km"
KP_UNIT_M = "m"
KP_UNITS = (KP_UNIT_KM, KP_UNIT_M)
KP_UNIT_LABELS = {KP_UNIT_KM: "km", KP_UNIT_M: "m"}

DEFAULT_START_FIELD = "start_kp"
DEFAULT_END_FIELD = "end_kp"

# Translated ranges listed by name in a note before the rest are counted.
_NOTE_EXAMPLES = 4


@dataclass
class KpRange:
    """One table row's range: as quoted, and on the plan route."""

    ref: str                     # row identity (feature id) for carry-over
    source_start: float          # km, as quoted on the reference RPL
    source_end: float
    start: float = 0.0           # km on the plan route
    end: float = 0.0
    flags: List[str] = field(default_factory=list)
    data: Dict = field(default_factory=dict)  # caller payload (attributes…)

    @property
    def lo(self) -> float:
        return min(self.start, self.end)

    @property
    def hi(self) -> float:
        return max(self.start, self.end)


def _number(value) -> Optional[float]:
    """A finite float from a number or a plain numeric string, else None.

    Strings with a decimal comma or units ("12,5", "KP 12.5") are rejected
    rather than guessed: "12,345" could be either 12.345 km or 12345.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def fields(config: Dict) -> Tuple[str, str]:
    return ((config.get("start_field") or DEFAULT_START_FIELD).strip(),
            (config.get("end_field") or DEFAULT_END_FIELD).strip())


def unit(config: Dict) -> str:
    value = (config.get(KP_UNIT_KEY) or KP_UNIT_KM).strip().lower()
    return value if value in KP_UNITS else KP_UNIT_KM


def read_ranges(rows: Sequence[Tuple[str, Dict]], config: Dict
                ) -> Tuple[List[KpRange], List[str]]:
    """``(ranges, notes)`` from ``(ref, attributes)`` rows.

    KPs are converted to km per the config's unit. A reversed range is
    kept (ordered by the caller via ``lo``/``hi``); unreadable rows are
    counted into ``notes``.
    """
    start_field, end_field = fields(config)
    scale = 0.001 if unit(config) == KP_UNIT_M else 1.0
    ranges: List[KpRange] = []
    missing_field = unreadable = 0
    for ref, attributes in rows:
        if start_field not in attributes or end_field not in attributes:
            missing_field += 1
            continue
        start = _number(attributes.get(start_field))
        end = _number(attributes.get(end_field))
        if start is None or end is None:
            unreadable += 1
            continue
        ranges.append(KpRange(ref=str(ref), source_start=start * scale,
                              source_end=end * scale))
    notes: List[str] = []
    total = len(rows)
    if missing_field:
        notes.append(
            f"{missing_field} of {total} row(s) lack the fields "
            f"'{start_field}'/'{end_field}' — choose the start and end KP "
            "fields")
    if unreadable:
        notes.append(
            f"{unreadable} of {total} row(s) have an empty or non-numeric "
            "start/end KP and were skipped (KPs must be plain numbers; "
            "decimal commas and text such as 'KP 12.3' are not read)")
    if not total:
        notes.append("the table has no rows (check the feature filter)")
    return ranges, notes


def translate(ranges: Sequence[KpRange],
              map_range: Optional[Callable[[float, float],
                                           Tuple[float, float, List[str]]]]
              ) -> List[KpRange]:
    """Place each range on the plan route (in place; returns ``ranges``).

    ``map_range`` is ``KpMap.map_range`` for a table on another RPL, or
    None when the table already refers to the plan's RPL.
    """
    for item in ranges:
        if map_range is None:
            item.start, item.end, item.flags = (
                item.source_start, item.source_end, [])
        else:
            start, end, flags = map_range(item.source_start, item.source_end)
            item.start, item.end = round(float(start), 6), round(float(end), 6)
            item.flags = list(flags)
    return list(ranges)


def flag_notes(ranges: Sequence[KpRange]) -> List[str]:
    """One note listing ranges whose translation needs checking."""
    flagged = [r for r in ranges if r.flags]
    if not flagged:
        return []
    shown = ", ".join(
        f"KP {min(r.source_start, r.source_end):.3f}–"
        f"{max(r.source_start, r.source_end):.3f} ({', '.join(r.flags)})"
        for r in flagged[:_NOTE_EXAMPLES])
    more = len(flagged) - _NOTE_EXAMPLES
    return [f"{len(flagged)} range(s) need checking after translation "
            f"(quoted KPs): {shown}" + (f" and {more} more" if more > 0 else "")]


def scope_note(ranges: Sequence[KpRange], scope_lo: float,
               scope_hi: float) -> List[str]:
    """A note when no readable range overlaps the analysis scope."""
    if not ranges:
        return []
    if any(r.hi >= scope_lo - 1e-9 and r.lo <= scope_hi + 1e-9
           for r in ranges):
        return []
    lo = min(r.lo for r in ranges)
    hi = max(r.hi for r in ranges)
    return [f"none of its {len(ranges)} range(s) (KP {lo:.3f}–{hi:.3f} on "
            f"the plan route) overlap the scope KP {scope_lo:.3f}–"
            f"{scope_hi:.3f} — check the KP unit and reference RPL"]


def reference_text(config: Dict) -> str:
    """Readable reference for logs: the RPL the table's KPs are quoted on."""
    if KP_REF_KEY not in config:
        return "not recorded (assumed: this plan's RPL)"
    return (config.get(KP_REF_LABEL_KEY) or config.get(KP_REF_KEY)
            or "this plan's route")


def fingerprint(ranges: Sequence[KpRange]) -> str:
    """Stable digest of the translated ranges (analysis cache key part)."""
    import hashlib

    digest = hashlib.sha1()
    for item in ranges:
        digest.update(f"{item.start:.6f},{item.end:.6f};".encode("ascii"))
    return digest.hexdigest()[:16]
