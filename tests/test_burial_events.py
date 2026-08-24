# -*- coding: utf-8 -*-
"""Checks for Burial Planner event invariants (pure python, no QGIS).

Alternation, ordering, conflict flagging, locked/confirmed survival across
regeneration, KP-only editing (spec §13, §16).
"""

from __future__ import annotations

from ..burial import events as ev
from ..burial import generation, schema
from ..workbench.rules_engine import Interval


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _event(eid, kp, etype, source="auto", status="candidate", locked=0):
    return {"event_id": eid, "plan_id": "p", "generation_id": "", "seq": 0,
            "event_type": etype, "kp": kp, "end_kp": None, "lat": None,
            "lon": None, "depth_m": None, "source": source, "status": status,
            "locked": locked, "notes": ""}


START = schema.EVENT_BURIAL_START
END = schema.EVENT_BURIAL_END


def _section(sid, kind, start, end):
    return {"section_id": sid, "kind": kind, "start_kp": start,
            "end_kp": end}


def test_labels() -> bool:
    ok = ev.event_label(START, schema.METHOD_PLOUGH) == "PLDN"
    ok = ok and ev.event_label(END, schema.METHOD_PLOUGH) == "PLUP"
    ok = ok and ev.event_label(START, schema.METHOD_TRENCHER) == "TRENCH_START"
    ok = ok and ev.event_label(END, schema.METHOD_TRENCHER) == "TRENCH_END"
    # Legacy rov_jet plans resolve through the alias to the trencher labels.
    ok = ok and ev.event_label(START, schema.METHOD_ROV_JET) == "TRENCH_START"
    ok = ok and ev.event_label(END, schema.METHOD_ROV_JET) == "TRENCH_END"
    return _result("per-method event labels", ok)


def test_ordering_and_seq() -> bool:
    events = [_event("b", 5.0, END), _event("a", 2.0, START)]
    ordered = ev.sort_events(events, 1)
    ok = [e["event_id"] for e in ordered] == ["a", "b"]
    ok = ok and [e["seq"] for e in ordered] == [0, 1]
    reversed_order = ev.sort_events(events, -1)
    ok = ok and [e["event_id"] for e in reversed_order] == ["b", "a"]
    return _result("travel ordering + seq per direction", ok)


def test_alternation_validation() -> bool:
    good = [_event("a", 2.0, START), _event("b", 5.0, END),
            _event("c", 6.0, START), _event("d", 8.0, END)]
    ok = ev.validate_events(good, 0.0, 10.0, 1, "plough").ok
    # two starts in a row
    bad = [_event("a", 2.0, START), _event("b", 5.0, START)]
    res = ev.validate_events(bad, 0.0, 10.0, 1, "plough")
    ok = ok and not res.ok and "PLDN" in res.errors[0]
    # end before any start
    bad2 = [_event("a", 2.0, END)]
    ok = ok and not ev.validate_events(bad2, 0.0, 10.0, 1, "plough").ok
    # dangling start -> warning only
    dangling = [_event("a", 2.0, START)]
    res3 = ev.validate_events(dangling, 0.0, 10.0, 1, "plough")
    ok = ok and res3.ok and len(res3.warnings) == 1
    # out of scope -> error
    out = [_event("a", 12.0, START)]
    ok = ok and not ev.validate_events(out, 0.0, 10.0, 1, "plough").ok
    return _result("alternation / scope / dangling-start rules", ok)


def test_move_past_partner_rejected() -> bool:
    events = [_event("a", 2.0, START), _event("b", 5.0, END)]
    ok = ev.check_move(events, "a", 3.0, 0.0, 10.0, 1, "plough") is None
    ok = ok and ev.check_move(events, "a", 6.0, 0.0, 10.0, 1, "plough") is not None
    ok = ok and ev.check_move(events, "b", 1.0, 0.0, 10.0, 1, "plough") is not None
    ok = ok and ev.check_move(events, "a", 11.0, 0.0, 10.0, 1, "plough") is not None
    return _result("moving an event past its partner / out of scope is rejected", ok)


def test_burial_pairs_direction() -> bool:
    events = [_event("a", 2.0, START), _event("b", 5.0, END)]
    pairs = ev.burial_pairs(events, 1)
    ok = len(pairs) == 1 and pairs[0][0]["event_id"] == "a" and pairs[0][1]["event_id"] == "b"
    # direction -1: start at higher KP
    events_ba = [_event("a", 5.0, START), _event("b", 2.0, END)]
    pairs_ba = ev.burial_pairs(events_ba, -1)
    ok = ok and pairs_ba[0][0]["event_id"] == "a" and pairs_ba[0][1]["event_id"] == "b"
    return _result("burial pairs follow travel direction", ok)


def test_merge_keeps_user_events() -> bool:
    existing = [
        _event("auto1", 1.0, START, source="auto", status="candidate"),
        _event("conf", 4.0, END, source="auto", status="confirmed"),
        _event("lock", 6.0, START, source="auto", status="candidate", locked=1),
        _event("manual", 8.0, END, source="manual", status="candidate"),
    ]
    generated = [_event("new1", 2.0, START)]
    merged, conflicts, warnings = generation.merge_events(
        existing, generated, excluded=[], direction=1, method="plough")
    ids = {e["event_id"] for e in merged}
    ok = "auto1" not in ids                      # disposable auto candidate
    ok = ok and {"conf", "lock", "manual", "new1"} <= ids
    ok = ok and not conflicts
    return _result("regeneration keeps locked/confirmed/manual, drops auto candidates", ok)


def test_merge_conflict_flagging() -> bool:
    existing = [_event("conf", 4.0, END, source="auto", status="confirmed")]
    merged, conflicts, warnings = generation.merge_events(
        existing, [], excluded=[Interval(3.0, 5.0)], direction=1, method="plough")
    ok = len(conflicts) == 1 and merged[0]["status"] == schema.EVENT_STATUS_CONFLICT
    ok = ok and any("Exclusion Area" in w for w in warnings)
    # conflict clears once the exclusion goes away -> back to candidate
    merged2, conflicts2, _w = generation.merge_events(
        merged, [], excluded=[], direction=1, method="plough")
    ok = ok and not conflicts2 and merged2[0]["status"] == schema.EVENT_STATUS_CANDIDATE
    return _result("conflict flagging + clearing on regeneration", ok)


def test_merge_dedupes_at_boundary() -> bool:
    existing = [_event("conf", 2.0, START, source="manual", status="confirmed")]
    generated = [_event("gen", 2.0002, START)]  # within 0.5 m
    merged, _c, _w = generation.merge_events(existing, generated, [], 1)
    ok = len(merged) == 1 and merged[0]["event_id"] == "conf"
    return _result("kept event at a generated boundary supersedes the candidate", ok)


def test_merge_burial_sections_and_skips() -> bool:
    events = [
        _event("start-a", 0.0, START), _event("end-a", 2.0, END),
        _event("start-b", 4.0, START), _event("end-b", 6.0, END),
        _event("start-c", 8.0, START), _event("end-c", 10.0, END),
    ]
    sections = [
        _section("b1", schema.SECTION_BURIAL, 0.0, 2.0),
        _section("s1", schema.SECTION_SKIP, 2.0, 4.0),
        _section("b2", schema.SECTION_BURIAL, 4.0, 6.0),
        _section("s2", schema.SECTION_SKIP, 6.0, 8.0),
        _section("b3", schema.SECTION_BURIAL, 8.0, 10.0),
    ]
    merged_burial, removed_burial, kind, dismissed, moved = \
        ev.merge_section_events(events, sections, ["b1", "b2"])
    ok = kind == schema.SECTION_BURIAL
    ok = ok and set(removed_burial) == {"end-a", "start-b"}
    ok = ok and not dismissed and not moved
    ok = ok and ev.validate_events(merged_burial, 0.0, 10.0, 1).ok

    merged_skips, removed_skips, kind, dismissed, moved = \
        ev.merge_section_events(events, sections, ["s1", "s2"])
    ok = ok and kind == schema.SECTION_SKIP
    ok = ok and set(removed_skips) == {"start-b", "end-b"}
    ok = ok and not dismissed and not moved
    ok = ok and ev.validate_events(merged_skips, 0.0, 10.0, 1).ok
    return _result("merge selected burial sections or selected skips", ok)


def test_merge_with_insufficient_between_burials() -> bool:
    # burial [0,2] | II [2,4] | burial [4,6] | skip [6,10]
    events = [_event("start-a", 0.0, START), _event("end-a", 2.0, END),
              _event("start-b", 4.0, START), _event("end-b", 6.0, END)]
    sections = [
        _section("b1", schema.SECTION_BURIAL, 0.0, 2.0),
        _section("i1", schema.SECTION_INSUFFICIENT, 2.0, 4.0),
        _section("b2", schema.SECTION_BURIAL, 4.0, 6.0),
        _section("s1", schema.SECTION_SKIP, 6.0, 10.0),
    ]
    remaining, removed, kind, dismissed, moved = ev.merge_section_events(
        events, sections, ["b1", "i1", "b2"])
    ok = kind == schema.SECTION_BURIAL
    ok = ok and set(removed) == {"end-a", "start-b"}
    ok = ok and dismissed == [(2.0, 4.0)] and not moved
    ok = ok and ev.validate_events(remaining, 0.0, 10.0, 1).ok
    pairs = ev.burial_pairs(remaining, 1)
    ok = ok and len(pairs) == 1
    ok = ok and float(pairs[0][0]["kp"]) == 0.0
    ok = ok and float(pairs[0][1]["kp"]) == 6.0
    return _result("merge burials across a selected II range dismisses it", ok)


def test_merge_edge_insufficient_moves_boundary() -> bool:
    # II [0,2] | burial [2,5] | skip [5,10]: merging the edge II into the
    # burial must MOVE the burial start to KP 0, never remove it.
    events = [_event("start-a", 2.0, START), _event("end-a", 5.0, END)]
    sections = [
        _section("i1", schema.SECTION_INSUFFICIENT, 0.0, 2.0),
        _section("b1", schema.SECTION_BURIAL, 2.0, 5.0),
        _section("s1", schema.SECTION_SKIP, 5.0, 10.0),
    ]
    remaining, removed, kind, dismissed, moved = ev.merge_section_events(
        events, sections, ["i1", "b1"])
    ok = kind == schema.SECTION_BURIAL and not removed
    ok = ok and dismissed == [(0.0, 2.0)]
    ok = ok and len(moved) == 1 and float(moved[0]["kp"]) == 0.0
    ok = ok and moved[0]["event_id"] == "start-a"
    ok = ok and ev.validate_events(remaining, 0.0, 10.0, 1).ok
    pairs = ev.burial_pairs(remaining, 1)
    ok = ok and len(pairs) == 1 and float(pairs[0][0]["kp"]) == 0.0
    return _result("merging an edge II moves the burial boundary event", ok)


def test_merge_insufficient_between_skips() -> bool:
    # burial [0,2] | skip [2,4] | II [4,6] | skip [6,8] | burial [8,10]:
    # no events sit at skip/II boundaries — the dismissal alone merges them.
    events = [_event("a", 0.0, START), _event("b", 2.0, END),
              _event("c", 8.0, START), _event("d", 10.0, END)]
    sections = [
        _section("b1", schema.SECTION_BURIAL, 0.0, 2.0),
        _section("s1", schema.SECTION_SKIP, 2.0, 4.0),
        _section("i1", schema.SECTION_INSUFFICIENT, 4.0, 6.0),
        _section("s2", schema.SECTION_SKIP, 6.0, 8.0),
        _section("b2", schema.SECTION_BURIAL, 8.0, 10.0),
    ]
    remaining, removed, kind, dismissed, moved = ev.merge_section_events(
        events, sections, ["s1", "i1", "s2"])
    ok = kind == schema.SECTION_SKIP
    ok = ok and not removed and not moved
    ok = ok and dismissed == [(4.0, 6.0)]
    ok = ok and len(remaining) == 4
    ok = ok and ev.validate_events(remaining, 0.0, 10.0, 1).ok
    return _result("skips merge across a selected II range via dismissal", ok)


def test_merge_selection_guards() -> bool:
    events = [_event("a", 0.0, START), _event("b", 2.0, END),
              _event("c", 4.0, START), _event("d", 6.0, END),
              _event("e", 8.0, START), _event("f", 10.0, END)]
    sections = [
        _section("b1", schema.SECTION_BURIAL, 0.0, 2.0),
        _section("s1", schema.SECTION_SKIP, 2.0, 4.0),
        _section("b2", schema.SECTION_BURIAL, 4.0, 6.0),
        _section("s2", schema.SECTION_SKIP, 6.0, 8.0),
        _section("b3", schema.SECTION_BURIAL, 8.0, 10.0),
    ]
    guarded = 0
    for chosen in (["b1", "s1"], ["b1", "b3"]):
        try:
            ev.merge_section_events(events, sections, list(chosen))
        except ValueError:
            guarded += 1
    locked = [dict(event) for event in events]
    locked[1]["locked"] = 1
    try:
        ev.merge_section_events(locked, sections, ["b1", "b2"])
    except ValueError:
        guarded += 1
    return _result("merge protects mixed, skipped and locked boundaries", guarded == 3)


def test_merge_insufficient_guards() -> bool:
    # burial [0,2] | II [2,4] | burial [4,6] | skip [6,10]
    events = [_event("start-a", 0.0, START), _event("end-a", 2.0, END),
              _event("start-b", 4.0, START), _event("end-b", 6.0, END)]
    sections = [
        _section("b1", schema.SECTION_BURIAL, 0.0, 2.0),
        _section("i1", schema.SECTION_INSUFFICIENT, 2.0, 4.0),
        _section("b2", schema.SECTION_BURIAL, 4.0, 6.0),
        _section("s1", schema.SECTION_SKIP, 6.0, 10.0),
    ]
    guarded = 0
    # An unselected II range between the sections still blocks the merge.
    try:
        ev.merge_section_events(events, sections, ["b1", "b2"])
    except ValueError as exc:
        guarded += 1 if "Insufficient Information" in str(exc) else 0
    # II ranges never merge on their own.
    try:
        ev.merge_section_events(
            events, sections + [
                _section("i2", schema.SECTION_INSUFFICIENT, 6.0, 7.0)],
            ["i1", "i2"])
    except ValueError:
        guarded += 1
    # Mixed target kinds stay rejected even with an II selected.
    try:
        ev.merge_section_events(events, sections, ["i1", "b2", "s1"])
    except ValueError:
        guarded += 1
    return _result("II merge guards: unselected/only-II/mixed selections",
                   guarded == 3)


def test_opposite_section_boundaries_follow_travel_direction() -> bool:
    forward_skip = ev.opposite_section_boundary_specs(
        schema.SECTION_BURIAL, 2.0, 3.0, 1)
    reverse_skip = ev.opposite_section_boundary_specs(
        schema.SECTION_BURIAL, 2.0, 3.0, -1)
    forward_burial = ev.opposite_section_boundary_specs(
        schema.SECTION_SKIP, 2.0, 3.0, 1)
    reverse_burial = ev.opposite_section_boundary_specs(
        schema.SECTION_SKIP, 2.0, 3.0, -1)
    ok = forward_skip == [(END, 2.0), (START, 3.0)]
    ok = ok and reverse_skip == [(END, 3.0), (START, 2.0)]
    ok = ok and forward_burial == [(START, 2.0), (END, 3.0)]
    ok = ok and reverse_burial == [(START, 3.0), (END, 2.0)]
    return _result("inserted skip/burial boundaries follow travel direction", ok)


def test_delete_section_events() -> bool:
    # burial [2,5] | skip [5,6] | burial [6,9] inside scope [0,10]:
    # the leading skip [0,2] and trailing skip [9,10] are scope-edge skips.
    events = [_event("a", 2.0, START), _event("b", 5.0, END),
              _event("c", 6.0, START), _event("d", 9.0, END)]
    sections = [_section("s0", schema.SECTION_SKIP, 0.0, 2.0),
                _section("b1", schema.SECTION_BURIAL, 2.0, 5.0),
                _section("s1", schema.SECTION_SKIP, 5.0, 6.0),
                _section("b2", schema.SECTION_BURIAL, 6.0, 9.0),
                _section("s2", schema.SECTION_SKIP, 9.0, 10.0),
                _section("i1", schema.SECTION_INSUFFICIENT, 10.0, 11.0)]
    # Deleting the interior skip removes its PLUP/PLDN pair -> burials merge.
    remaining, removed, section = ev.delete_section_events(
        events, sections, "s1", "plough")
    ok = sorted(removed) == ["b", "c"]
    ok = ok and [e["event_id"] for e in remaining] == ["a", "d"]
    ok = ok and section["section_id"] == "s1"
    ok = ok and ev.validate_events(remaining, 0.0, 10.0, 1, "plough").ok
    # Deleting a burial section removes its own pair -> skips merge.
    remaining2, removed2, _ = ev.delete_section_events(
        events, sections, "b1", "plough")
    ok = ok and sorted(removed2) == ["a", "b"]
    ok = ok and ev.validate_events(remaining2, 0.0, 10.0, 1, "plough").ok
    guarded = 0
    # A scope-edge skip has no event on one side: nothing to merge with.
    for target in ("s0", "s2"):
        try:
            ev.delete_section_events(events, sections, target, "plough")
        except ValueError:
            guarded += 1
    # Insufficient Information sections cannot be deleted.
    try:
        ev.delete_section_events(events, sections, "i1", "plough")
    except ValueError:
        guarded += 1
    # A locked boundary event blocks the deletion.
    locked = [dict(e, locked=1 if e["event_id"] == "b" else 0) for e in events]
    try:
        ev.delete_section_events(locked, sections, "s1", "plough")
    except ValueError:
        guarded += 1
    return _result("delete section merges neighbours, guards edges/locks",
                   ok and guarded == 4)


def test_note_helpers() -> bool:
    ok = ev.append_note("", "x") == "x"
    ok = ok and ev.append_note("a", "") == "a"
    ok = ok and ev.append_note("a", "b") == "a; b"
    # Reasons cannot break the bracket pattern.
    ok = ok and ev.audit_note("t", "why [not]") == "[t: why (not)]"
    ok = ok and ev.audit_note("t") == "[t]"
    return _result("note append/audit formatting", ok)


def test_upsert_move_note_coalesces() -> bool:
    manual = "manual note"
    one = ev.upsert_move_note(manual, "PLUP", 5.0, 5.01, "nudge +10 m")
    ok = one == "manual note; [PLUP moved KP 5.000→5.010: nudge +10 m]"
    # A second move of the same event collapses the chain, keeping the
    # origin KP and the latest reason.
    two = ev.upsert_move_note(one, "PLUP", 5.01, 5.02, "nudge +10 m")
    ok = ok and two == "manual note; [PLUP moved KP 5.000→5.020: nudge +10 m]"
    # Moving back to the origin drops the note entirely.
    back = ev.upsert_move_note(two, "PLUP", 5.02, 5.0)
    ok = ok and back == manual
    # Coalescing is per-label: another event's note is left alone.
    other = ev.upsert_move_note(two, "PLDN", 2.0, 2.1)
    ok = ok and "[PLUP moved KP 5.000→5.020: nudge +10 m]" in other
    ok = ok and "[PLDN moved KP 2.000→2.100]" in other
    # Mid-string removal keeps the separators tidy.
    mid = ev.upsert_move_note(two + "; trailing", "PLUP", 5.02, 5.03)
    ok = ok and mid == ("manual note; trailing; "
                        "[PLUP moved KP 5.000→5.030]")
    return _result("move notes coalesce chains and stay editable", ok)


def test_resolve_insufficient_events() -> bool:
    """Resolving II ranges as burial creates or reuses boundary events so
    the ranges coalesce with abutting burial sections; a locked interior
    event aborts; non-II selections are rejected."""
    # burial [0,2] | II [2,4] | burial [4,6] | skip [6,8] | II [8,9] | skip [9,10]
    events = [_event("a", 0.0, START), _event("b", 2.0, END),
              _event("c", 4.0, START), _event("d", 6.0, END)]
    sections = [
        _section("b1", schema.SECTION_BURIAL, 0.0, 2.0),
        _section("i1", schema.SECTION_INSUFFICIENT, 2.0, 4.0),
        _section("b2", schema.SECTION_BURIAL, 4.0, 6.0),
        _section("s1", schema.SECTION_SKIP, 6.0, 8.0),
        _section("i2", schema.SECTION_INSUFFICIENT, 8.0, 9.0),
        _section("s2", schema.SECTION_SKIP, 9.0, 10.0),
    ]
    # II between two burial sections: the seam events go, edges stay.
    remaining, specs, removed = ev.resolve_insufficient_events(
        events, sections, ["i1"], 1)
    ok = set(removed) == {"b", "c"} and specs == []
    ok = ok and [e["event_id"] for e in remaining] == ["a", "d"]
    ok = ok and ev.validate_events(remaining, 0.0, 10.0, 1).ok

    # Standalone II inside skips: both boundary events are created.
    remaining2, specs2, removed2 = ev.resolve_insufficient_events(
        events, sections, ["i2"], 1)
    ok = ok and not removed2 and len(remaining2) == 4
    ok = ok and specs2 == [(START, 8.0), (END, 9.0)]
    # Direction -1 swaps which edge is the start.
    _r3, specs3, _d3 = ev.resolve_insufficient_events(
        events, sections, ["i2"], -1)
    ok = ok and specs3 == [(END, 8.0), (START, 9.0)]

    # Both II ranges in one call: one merged component [0,6] + one new pair.
    remaining4, specs4, removed4 = ev.resolve_insufficient_events(
        events, sections, ["i1", "i2"], 1)
    ok = ok and set(removed4) == {"b", "c"}
    ok = ok and specs4 == [(START, 8.0), (END, 9.0)]
    merged_events = remaining4 + [_event(f"n{i}", kp, etype)
                                  for i, (etype, kp) in enumerate(specs4)]
    ok = ok and ev.validate_events(merged_events, 0.0, 10.0, 1).ok
    pairs = ev.burial_pairs(ev.sort_events(merged_events, 1), 1)
    spans = [(float(s["kp"]), float(e["kp"])) for s, e in pairs]
    ok = ok and spans == [(0.0, 6.0), (8.0, 9.0)]

    # Guards: a locked seam event aborts; non-II selections are rejected.
    guarded = 0
    locked = [dict(e) for e in events]
    locked[1]["locked"] = 1
    try:
        ev.resolve_insufficient_events(locked, sections, ["i1"], 1)
    except ValueError:
        guarded += 1
    try:
        ev.resolve_insufficient_events(events, sections, ["b1"], 1)
    except ValueError:
        guarded += 1
    ok = ok and guarded == 2
    return _result("resolve II ranges as burial via event surgery", ok)


def run_all() -> list:
    return [
        test_labels(),
        test_ordering_and_seq(),
        test_alternation_validation(),
        test_move_past_partner_rejected(),
        test_burial_pairs_direction(),
        test_merge_keeps_user_events(),
        test_merge_conflict_flagging(),
        test_merge_dedupes_at_boundary(),
        test_merge_burial_sections_and_skips(),
        test_merge_with_insufficient_between_burials(),
        test_merge_edge_insufficient_moves_boundary(),
        test_merge_insufficient_between_skips(),
        test_merge_selection_guards(),
        test_merge_insufficient_guards(),
        test_resolve_insufficient_events(),
        test_opposite_section_boundaries_follow_travel_direction(),
        test_delete_section_events(),
        test_note_helpers(),
        test_upsert_move_note_coalesces(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
