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

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import schema

_KP_TOL = 5e-7  # km (~0.5 mm) — comparisons on stored floats
_BOUNDARY_TOL = 1e-6  # km — matching an event to a section boundary


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
                         ) -> Tuple[List[Dict], List[str], str,
                                    List[Tuple[float, float]], List[Dict]]:
    """Remove boundary events so selected burial sections *or* skips merge.

    The selection must contain exactly one mergeable kind (burial or skip)
    and every section of that kind between the first and last selection.
    Insufficient-information ranges are never swallowed silently, but they
    merge when *explicitly selected* alongside their neighbours: their KP
    ranges come back as ``dismissed_ranges`` (``(start_kp, end_kp)`` pairs)
    for the caller to remove from the plan's no-data context. A burial
    boundary event that must extend across an edge II range is moved to the
    span edge rather than removed; those (still-in-``remaining``) event
    dicts are returned as ``moved_events`` so the caller can re-stamp their
    derived position/depth.

    Returns ``(remaining_events, removed_event_ids, section_kind,
    dismissed_ranges, moved_events)``.
    """
    wanted = {str(section_id) for section_id in section_ids if section_id}
    selected = [section for section in sections
                if str(section.get("section_id") or "") in wanted]
    if len(selected) != len(wanted) or len(selected) < 2:
        raise ValueError("Select at least two sections to merge.")
    kinds = {section.get("kind") or "" for section in selected}
    target_kinds = kinds - {schema.SECTION_INSUFFICIENT}
    if not target_kinds:
        raise ValueError(
            "Select the neighbouring section to merge the Insufficient "
            "Information range into.")
    if len(target_kinds) != 1:
        raise ValueError("Selected sections must all be the same kind.")
    kind = next(iter(target_kinds))
    if kind not in (schema.SECTION_BURIAL, schema.SECTION_SKIP):
        raise ValueError("Only burial sections and skips can be merged.")

    selected.sort(key=lambda section: float(section.get("start_kp") or 0.0))
    span_start = float(selected[0].get("start_kp") or 0.0)
    span_end = float(selected[-1].get("end_kp") or 0.0)
    within_span = [
        section for section in sections
        if float(section.get("end_kp") or 0.0) > span_start + _KP_TOL
        and float(section.get("start_kp") or 0.0) < span_end - _KP_TOL
    ]
    if any(section.get("kind") == schema.SECTION_INSUFFICIENT
           and str(section.get("section_id") or "") not in wanted
           for section in within_span):
        raise ValueError(
            "Cannot merge across an Insufficient Information section that "
            "is not part of the selection.")
    same_kind_ids = {
        str(section.get("section_id") or "") for section in within_span
        if section.get("kind") == kind
    }
    wanted_target = {
        str(section.get("section_id") or "") for section in selected
        if section.get("kind") == kind
    }
    if same_kind_ids != wanted_target:
        raise ValueError(
            "Select every section of this kind between the first and last selection.")

    dismissed_ranges = [
        (float(section.get("start_kp") or 0.0),
         float(section.get("end_kp") or 0.0))
        for section in selected
        if section.get("kind") == schema.SECTION_INSUFFICIENT
    ]

    # A burial boundary event adjacent to a *selected edge* II range must
    # move to the span edge (removal would leave the pair dangling); every
    # other boundary event strictly inside the span is removed.
    targets = [section for section in selected if section.get("kind") == kind]
    moves: List[Tuple[float, float]] = []
    if kind == schema.SECTION_BURIAL:
        first_start = float(targets[0].get("start_kp") or 0.0)
        last_end = float(targets[-1].get("end_kp") or 0.0)
        if first_start - span_start > _KP_TOL:
            moves.append((first_start, span_start))
        if span_end - last_end > _KP_TOL:
            moves.append((last_end, span_end))

    removed: List[str] = []
    remaining: List[Dict] = []
    moved: List[Dict] = []
    for event in events:
        kp = float(event.get("kp") or 0.0)
        inside = span_start + _KP_TOL < kp < span_end - _KP_TOL
        if not inside:
            remaining.append(dict(event))
            continue
        if int(event.get("locked") or 0):
            raise ValueError(
                "A locked event lies between the sections — unlock it first.")
        new_kp = next((new for old, new in moves
                       if abs(kp - old) <= _BOUNDARY_TOL), None)
        if new_kp is not None:
            copy = dict(event)
            copy["kp"] = new_kp
            remaining.append(copy)
            moved.append(copy)
        else:
            removed.append(str(event.get("event_id") or ""))
    if not removed and not moved and not dismissed_ranges:
        raise ValueError(
            f"No {event_label(schema.EVENT_BURIAL_START, method)}/"
            f"{event_label(schema.EVENT_BURIAL_END, method)} boundaries were "
            "found between the sections.")
    return remaining, removed, kind, dismissed_ranges, moved


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


def resolve_insufficient_events(events: List[Dict], sections: List[Dict],
                                section_ids: List[str], direction: int
                                ) -> Tuple[List[Dict], List[Tuple[str, float]],
                                           List[str]]:
    """Boundary-event surgery turning selected II ranges into burial ranges.

    The desired burial coverage is the union of the current burial sections
    with the selected Insufficient Information ranges (abutting ranges
    coalesce, so a resolved range merges with its burial neighbours into
    one section). Within every union component touching a selected range:

    - boundary events strictly inside the component are removed (they are
      seams being merged away; a locked one aborts with a clear message);
    - a component edge without an existing boundary event gets a new one,
      returned as ``(event_type, kp)`` specs for the caller to create
      (direction −1 reverses which edge is the start).

    Burial sections in untouched components keep their events unchanged.
    Returns ``(remaining_events, new_event_specs, removed_event_ids)``.
    """
    wanted = {str(section_id) for section_id in section_ids if section_id}
    selected = [section for section in sections
                if str(section.get("section_id") or "") in wanted]
    if len(selected) != len(wanted) or not selected:
        raise ValueError("Section not found.")
    if any(section.get("kind") != schema.SECTION_INSUFFICIENT
           for section in selected):
        raise ValueError(
            "Only Insufficient Information sections can be resolved.")
    ii_ranges = [(float(section.get("start_kp") or 0.0),
                  float(section.get("end_kp") or 0.0))
                 for section in selected]
    burial_ranges = [(float(section.get("start_kp") or 0.0),
                      float(section.get("end_kp") or 0.0))
                     for section in sections
                     if section.get("kind") == schema.SECTION_BURIAL]

    # Union with abutting merge (sections tile the scope, so a resolved
    # range and its burial neighbour share an exact boundary KP).
    merged: List[List[float]] = []
    for lo, hi in sorted(ii_ranges + burial_ranges):
        if merged and lo <= merged[-1][1] + _BOUNDARY_TOL:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    affected = [component for component in merged
                if any(lo < component[1] - _KP_TOL
                       and hi > component[0] + _KP_TOL
                       for lo, hi in ii_ranges)]

    def component_for(kp: float) -> Optional[List[float]]:
        for component in affected:
            if component[0] - _BOUNDARY_TOL <= kp \
                    <= component[1] + _BOUNDARY_TOL:
                return component
        return None

    remaining: List[Dict] = []
    removed: List[str] = []
    found_edges = set()
    for event in events:
        if not (is_start(event) or is_end(event)):
            remaining.append(dict(event))
            continue
        kp = float(event.get("kp") or 0.0)
        component = component_for(kp)
        if component is None:
            remaining.append(dict(event))
            continue
        at_start = abs(kp - component[0]) <= _BOUNDARY_TOL
        at_end = abs(kp - component[1]) <= _BOUNDARY_TOL
        if at_start or at_end:
            # Already the outer boundary of the merged burial range; its
            # type is correct by construction (an abutting burial section
            # is part of the same component).
            remaining.append(dict(event))
            found_edges.add((component[0], component[1],
                             "start" if at_start else "end"))
            continue
        if int(event.get("locked") or 0):
            raise ValueError(
                f"A locked event at KP {schema.format_kp(kp)} lies inside "
                "the resolved range — unlock it first.")
        removed.append(str(event.get("event_id") or ""))

    forward = int(direction or 1) >= 0
    low_type = schema.EVENT_BURIAL_START if forward else schema.EVENT_BURIAL_END
    high_type = schema.EVENT_BURIAL_END if forward else schema.EVENT_BURIAL_START
    specs: List[Tuple[str, float]] = []
    for component in affected:
        if (component[0], component[1], "start") not in found_edges:
            specs.append((low_type, component[0]))
        if (component[0], component[1], "end") not in found_edges:
            specs.append((high_type, component[1]))
    return remaining, specs, removed


def delete_section_events(events: List[Dict], sections: List[Dict],
                          section_id: str, method: str = ""
                          ) -> Tuple[List[Dict], List[str], Dict]:
    """Remove a section's boundary events so its neighbours merge into one.

    Deleting a skip removes its bounding BURIAL_END/BURIAL_START pair and
    the burial sections either side become one; deleting a burial section
    removes its own pair and the surrounding skips merge. Only interior
    sections qualify: a boundary without an event is the plan scope edge,
    where there is no second neighbour to merge with.
    Returns ``(remaining_events, removed_event_ids, section)``.
    """
    section = next((s for s in sections
                    if str(s.get("section_id") or "") == str(section_id)), None)
    if section is None:
        raise ValueError("Section not found.")
    kind = section.get("kind") or ""
    if kind not in (schema.SECTION_BURIAL, schema.SECTION_SKIP):
        # Insufficient Information sections have no boundary events; the
        # plan model dismisses their no-data range instead of calling here.
        raise ValueError(
            "Only burial sections and skips are deleted via their "
            "boundary events.")
    start = float(section.get("start_kp") or 0.0)
    end = float(section.get("end_kp") or 0.0)
    removed: List[str] = []
    remaining: List[Dict] = []
    has_start = has_end = False
    for event in events:
        kp = float(event.get("kp") or 0.0)
        if start - _BOUNDARY_TOL <= kp <= end + _BOUNDARY_TOL:
            if int(event.get("locked") or 0):
                raise ValueError(
                    f"{event_label(event.get('event_type') or '', method)} at "
                    f"KP {schema.format_kp(kp)} is locked — unlock it before "
                    "deleting the section.")
            has_start = has_start or abs(kp - start) <= _BOUNDARY_TOL
            has_end = has_end or abs(kp - end) <= _BOUNDARY_TOL
            removed.append(str(event.get("event_id") or ""))
        else:
            remaining.append(dict(event))
    if not (has_start and has_end):
        missing = schema.format_kp(end if has_start else start)
        raise ValueError(
            f"The section boundary at KP {missing} has no boundary event "
            "(the plan scope edge or an Insufficient Information boundary), "
            "so there is no neighbour on that side to merge with. Move the "
            "other boundary event instead, or adjust the plan scope on "
            "Inputs.")
    return remaining, removed, section


# -- notes ---------------------------------------------------------------
# Automatic audit notes appended to the Plan Builder Notes columns. They are
# plain editable text: "[...]" marks machine-appended context, "; " joins
# entries, and reasons are sanitised so a note can never break the pattern.

_NOTE_SEP = "; "


def _clean_reason(reason: str) -> str:
    return (reason or "").replace("[", "(").replace("]", ")").strip()


def append_note(existing: str, addition: str) -> str:
    """Join a new note onto existing notes without disturbing them."""
    existing = (existing or "").strip()
    addition = (addition or "").strip()
    if not addition:
        return existing
    if not existing:
        return addition
    return existing + _NOTE_SEP + addition


def audit_note(text: str, reason: str = "") -> str:
    """One bracketed audit entry, e.g. ``[skip KP 2.000-3.000 removed: r]``."""
    reason = _clean_reason(reason)
    return f"[{text}: {reason}]" if reason else f"[{text}]"


def upsert_move_note(existing: str, label: str, old_kp, new_kp,
                     reason: str = "") -> str:
    """Append a boundary-move audit note, coalescing move chains.

    Repeated moves of the same event (e.g. successive nudges) collapse into
    one ``[<label> moved KP <origin>→<final>]`` entry instead of a trail: an
    existing entry ending at ``old_kp`` is replaced, keeping its origin KP
    and the latest reason.
    """
    existing = (existing or "").strip()
    old_text = schema.format_kp(old_kp)
    new_text = schema.format_kp(new_kp)
    origin = old_text
    pattern = re.compile(
        r"\[" + re.escape(label) + r" moved KP (\d+(?:\.\d+)?)→"
        + re.escape(old_text) + r"(?::[^\]]*)?\]")
    match = pattern.search(existing)
    if match:
        origin = match.group(1)
        head, tail = existing[:match.start()], existing[match.end():]
        # Absorb one adjacent separator so a mid-string removal cannot
        # leave "a; ; b" behind.
        if tail.startswith(_NOTE_SEP):
            tail = tail[len(_NOTE_SEP):]
        elif head.endswith(_NOTE_SEP):
            head = head[:-len(_NOTE_SEP)]
        existing = (head + tail).strip()
    if origin == new_text:
        # The chain returned to its origin — drop the note entirely.
        return existing
    return append_note(existing, audit_note(
        f"{label} moved KP {origin}→{new_text}", reason))
