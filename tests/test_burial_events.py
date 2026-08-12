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


def test_labels() -> bool:
    ok = ev.event_label(START, schema.METHOD_PLOUGH) == "PLDN"
    ok = ok and ev.event_label(END, schema.METHOD_PLOUGH) == "PLUP"
    ok = ok and ev.event_label(START, schema.METHOD_ROV_JET) == "JET_START"
    ok = ok and ev.event_label(END, schema.METHOD_ROV_JET) == "JET_STOP"
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
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
