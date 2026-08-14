# -*- coding: utf-8 -*-
"""Event type registry, per-method labels and ordering/validation invariants.

Pure python — no QGIS imports — so the invariants are unit-testable under
plain Python. Events are plain dicts shaped like ``bp_event`` rows.

Invariants enforced here (spec §13):
- Events sorted by KP per direction of installation; ``seq`` follows that order.
- Strict alternation of BURIAL_START / BURIAL_END in travel order; the plan
  starts with a start-event or a skip; a dangling start at scope end is a
  warning, not an error.
- No zero/negative-length burial sections; moving an event past its partner
  is rejected with a clear message.
- Events lie within the plan scope.
- ``kp`` is the sole editable position; lat/lon/depth are derived.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import schema

_KP_TOL = 5e-7  # km (~0.5 mm) — comparisons on stored floats


def event_label(event_type: str, method: str) -> str:
    """The method-correct display label for an event type.

    The per-method vocabulary lives in ``schema.METHOD_EVENT_LABELS`` beside
    the section codes and kind labels.
    """
    labels = schema.METHOD_EVENT_LABELS.get(
        schema.normalise_method(method), {})
    return labels.get(event_type, event_type or "")


def is_start(event: Dict) -> bool:
    return event.get("event_type") == schema.EVENT_BURIAL_START


def is_end(event: Dict) -> bool:
    return event.get("event_type") == schema.EVENT_BURIAL_END


def travel_key(direction: int):
    """Sort key placing events in travel order for the given direction."""
    sign = -1.0 if int(direction or 1) < 0 else 1.0

    def key(event: Dict) -> float:
        try:
            return sign * float(event.get("kp") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    return key


def sort_events(events: List[Dict], direction: int) -> List[Dict]:
    """Events in travel order with ``seq`` renumbered from 0."""
    ordered = sorted(events, key=travel_key(direction))
    for seq, event in enumerate(ordered):
        event["seq"] = seq
    return ordered


@dataclass
class ValidationResult:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_events(events: List[Dict], scope_start_kp: float, scope_end_kp: float,
                    direction: int, method: str = "") -> ValidationResult:
    """Check the event invariants; alternation errors name the offending KP."""
    result = ValidationResult()
    lo = min(scope_start_kp, scope_end_kp)
    hi = max(scope_start_kp, scope_end_kp)

    def label(event: Dict) -> str:
        return event_label(event.get("event_type") or "", method)

    for event in events:
        try:
            kp = float(event.get("kp"))
        except (TypeError, ValueError):
            result.errors.append("An event has no valid KP.")
            continue
        if kp < lo - _KP_TOL or kp > hi + _KP_TOL:
            result.errors.append(
                f"{label(event)} at KP {schema.format_kp(kp)} lies outside the "
                f"plan scope KP {schema.format_kp(lo)}-{schema.format_kp(hi)}.")

    ordered = sorted(events, key=travel_key(direction))
    expecting_start = True
    last: Optional[Dict] = None
    for event in ordered:
        if not (is_start(event) or is_end(event)):
            continue  # reserved transition types: ignored by v1 alternation
        if expecting_start and is_end(event):
            result.errors.append(
                f"{label(event)} at KP {schema.format_kp(event.get('kp'))} has no "
                f"preceding {event_label(schema.EVENT_BURIAL_START, method)}.")
            continue
        if not expecting_start and is_start(event):
            result.errors.append(
                f"{label(event)} at KP {schema.format_kp(event.get('kp'))} follows "
                f"another {event_label(schema.EVENT_BURIAL_START, method)} without "
                f"a {event_label(schema.EVENT_BURIAL_END, method)} between them.")
            continue
        if not expecting_start and last is not None:
            try:
                length = abs(float(event.get("kp")) - float(last.get("kp")))
            except (TypeError, ValueError):
                length = 0.0
            if length <= _KP_TOL:
                result.errors.append(
                    f"Burial section at KP {schema.format_kp(last.get('kp'))} "
                    "would have zero length.")
        last = event
        expecting_start = not expecting_start

    if not expecting_start:
        result.warnings.append(
            f"Dangling {event_label(schema.EVENT_BURIAL_START, method)} at KP "
            f"{schema.format_kp(last.get('kp') if last else '')} — no matching "
            f"{event_label(schema.EVENT_BURIAL_END, method)} before scope end.")
    return result


def burial_pairs(events: List[Dict], direction: int) -> List[Tuple[Dict, Optional[Dict]]]:
    """(start, end) event pairs in travel order; a dangling start pairs None."""
    ordered = sorted(events, key=travel_key(direction))
    pairs: List[Tuple[Dict, Optional[Dict]]] = []
    open_start: Optional[Dict] = None
    for event in ordered:
        if is_start(event):
            if open_start is not None:
                pairs.append((open_start, None))
            open_start = event
        elif is_end(event):
            if open_start is not None:
                pairs.append((open_start, event))
                open_start = None
    if open_start is not None:
        pairs.append((open_start, None))
    return pairs


def check_move(events: List[Dict], event_id: str, new_kp: float,
               scope_start_kp: float, scope_end_kp: float, direction: int,
               method: str = "") -> Optional[str]:
    """Why moving ``event_id`` to ``new_kp`` is invalid, or None if fine.

    Validates the full invariant set with the event provisionally moved, so a
    move past the event's partner (or out of scope) is rejected with the
    specific message rather than a generic failure.
    """
    moved: List[Dict] = []
    found = False
    for event in events:
        copy = dict(event)
        if copy.get("event_id") == event_id:
            copy["kp"] = float(new_kp)
            found = True
        moved.append(copy)
    if not found:
        return "Event not found."
    result = validate_events(moved, scope_start_kp, scope_end_kp, direction, method)
    if result.errors:
        return result.errors[0]
    return None


def merge_section_events(events: List[Dict], sections: List[Dict],
                         section_ids: List[str], method: str = ""
                         ) -> Tuple[List[Dict], List[str], str]:
    """Remove boundary events so selected burial sections *or* skips merge.

    The selected sections must all have the same mergeable kind and must
    include every section of that kind between the first and last selection.
    Insufficient-information ranges are never swallowed by a manual merge.
    Returns ``(remaining_events, removed_event_ids, section_kind)``.
    """
    wanted = {str(section_id) for section_id in section_ids if section_id}
    selected = [section for section in sections
                if str(section.get("section_id") or "") in wanted]
    if len(selected) != len(wanted) or len(selected) < 2:
        raise ValueError("Select at least two sections to merge.")
    kinds = {section.get("kind") or "" for section in selected}
    if len(kinds) != 1:
        raise ValueError("Selected sections must all be the same kind.")
    kind = next(iter(kinds))
    if kind not in (schema.SECTION_BURIAL, schema.SECTION_SKIP):
        raise ValueError("Insufficient Information sections cannot be merged.")

    selected.sort(key=lambda section: float(section.get("start_kp") or 0.0))
    span_start = float(selected[0].get("start_kp") or 0.0)
    span_end = float(selected[-1].get("end_kp") or 0.0)
    within_span = [
        section for section in sections
        if float(section.get("end_kp") or 0.0) > span_start + _KP_TOL
        and float(section.get("start_kp") or 0.0) < span_end - _KP_TOL
    ]
    if any(section.get("kind") == schema.SECTION_INSUFFICIENT
           for section in within_span):
        raise ValueError(
            "Cannot merge across an Insufficient Information section.")
    same_kind_ids = {
        str(section.get("section_id") or "") for section in within_span
        if section.get("kind") == kind
    }
    if same_kind_ids != wanted:
        raise ValueError(
            "Select every section of this kind between the first and last selection.")

    merge_gaps = [
        (float(left.get("end_kp") or 0.0),
         float(right.get("start_kp") or 0.0))
        for left, right in zip(selected, selected[1:])
    ]
    removed: List[str] = []
    remaining: List[Dict] = []
    for event in events:
        kp = float(event.get("kp") or 0.0)
        in_gap = any(min(start, end) - _KP_TOL <= kp
                     <= max(start, end) + _KP_TOL
                     for start, end in merge_gaps)
        if not in_gap:
            remaining.append(dict(event))
            continue
        if int(event.get("locked") or 0):
            raise ValueError(
                "A locked event lies between the sections — unlock it first.")
        removed.append(str(event.get("event_id") or ""))
    if not removed:
        raise ValueError(
            f"No {event_label(schema.EVENT_BURIAL_START, method)}/"
            f"{event_label(schema.EVENT_BURIAL_END, method)} boundaries were "
            "found between the sections.")
    return remaining, removed, kind


def opposite_section_boundary_specs(section_kind: str, start_kp: float,
                                    end_kp: float, direction: int
                                    ) -> List[Tuple[str, float]]:
    """Boundary event types/KPs for an opposite-kind range inside a section.

    In a burial section this inserts a skip (PLUP then PLDN in travel order);
    in a skip it inserts a burial section (PLDN then PLUP). Direction -1
    reverses the KP order while retaining those travel-order semantics.
    """
    if section_kind not in (schema.SECTION_BURIAL, schema.SECTION_SKIP):
        raise ValueError(
            "Only burial sections and skips can be split with an inserted range.")
    lo, hi = sorted((float(start_kp), float(end_kp)))
    if hi - lo <= _KP_TOL:
        raise ValueError("The inserted section must have a positive length.")
    travel_kps = (lo, hi) if int(direction or 1) >= 0 else (hi, lo)
    types = ((schema.EVENT_BURIAL_END, schema.EVENT_BURIAL_START)
             if section_kind == schema.SECTION_BURIAL
             else (schema.EVENT_BURIAL_START, schema.EVENT_BURIAL_END))
    return list(zip(types, travel_kps))
