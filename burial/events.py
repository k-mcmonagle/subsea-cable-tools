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

# Display labels: generic semantics, per-method vocabulary. Generic terms
# appear only in code/schema, never in plough-mode UI (spec D4).
_METHOD_EVENT_LABELS: Dict[str, Dict[str, str]] = {
    schema.METHOD_PLOUGH: {
        schema.EVENT_BURIAL_START: "PLDN",
        schema.EVENT_BURIAL_END: "PLUP",
    },
    schema.METHOD_ROV_JET: {
        schema.EVENT_BURIAL_START: "JET_START",
        schema.EVENT_BURIAL_END: "JET_STOP",
    },
}

_KP_TOL = 5e-7  # km (~0.5 mm) — comparisons on stored floats


def event_label(event_type: str, method: str) -> str:
    """The method-correct display label for an event type."""
    labels = _METHOD_EVENT_LABELS.get(method or "", {})
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
