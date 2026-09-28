# -*- coding: utf-8 -*-
"""Burial plan from RPL events (burial.rpl_plan_import, no QGIS)."""

from ..burial import events as ev
from ..burial import plan_import as pi
from ..burial import rpl_plan_import as rpi


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


S, E = rpi.START, rpi.END
PL, TR, MFE, BUR, SK = rpi.M_PLOUGH, rpi.M_TRENCHER, rpi.M_MFE, rpi.M_BURIAL, rpi.M_SKIP


def _rows(spec):
    """``[(kp, event, remarks)]`` → RplRows placed at those plan KPs."""
    return [rpi.RplRow(seq=i, pos_no=i + 1, event=event, remarks=remarks,
                       stated_kp=kp, kp=kp, offset_m=0.0)
            for i, (kp, event, remarks) in enumerate(spec)]


def _spans(walk):
    return [(round(s.lo, 3), round(s.hi, 3), s.method) for s in walk.spans]


def _labels(text):
    return [t.label for t in rpi.detect_tokens(text)]


# A typical RPL: plough, a lift over a crossing, plough to a transition
# row onto post-lay burial, then a surface-laid stretch to the end.
_RPL = [
    (0.000, "BMH", ""),
    (0.500, "PLDN", "Start of plough burial"),
    (1.000, "A/C", ""),
    (2.000, "PLUP", "Crossing C-12"),
    (2.300, "PL-DN", ""),
    (4.000, "PLUP / Start PLB", ""),
    (5.500, "A/C", ""),
    (6.000, "End PLB", "surface lay to end"),
    (7.000, "RPL end", ""),
]


def test_phrasings():
    cases = {
        "PLDN": ["PLDN"], "pl dn": ["PLDN"], "PL-UP": ["PLUP"], "Plough down": ["PLDN"],
        "Plough recovered to deck": ["PLUP"], "Start of plough burial": ["PLDN"],
        "Commence ploughing": ["PLDN"], "Stop ploughing": ["PLUP"],
        "PLUP / Start PLB": ["PLUP", "Start PLB"], "End PLB, PLDN": ["End PLB", "PLDN"],
        "Start post-lay burial": ["Start PLB"], "PLB end": ["End PLB"],
        "SOPLB": ["Start PLB"], "EPLB": ["End PLB"], "Start jetting": ["Start PLB"],
        "Start MFE": ["Start MFE"], "Mass flow excavation complete": ["End MFE"],
        "Start of burial": ["Start burial"], "EOB": ["End burial"],
        "Start surface lay": ["Start skip"], "End of skip": ["End skip"],
        "Skip start (crossing)": ["Start skip"], "Start no burial zone": ["Start skip"],
        "Repeater": [], "A/C": [], "Crossing": [], "Burial depth 1.0m": [],
        "Plough burial start": ["PLDN"], "Surface laid section ends": ["End skip"],
    }
    got = {text: _labels(text) for text in cases}
    bad = {t: g for t, g in got.items() if g != cases[t]}
    return _result("phrasings: abbreviations, verb/noun order, transitions, skips", not bad,
                   f"mismatches={bad}")


def test_walk_typical_rpl():
    rows = _rows(_RPL)
    tokens = rpi.auto_tokens(rows)
    walk = rpi.walk(rows, tokens, 1)
    spans = _spans(walk)
    # Remarks repeat the event (row 2), so it reads once.
    ok = tokens[1] == [rpi.Token(S, PL)]
    ok = ok and spans == [(0.5, 2.0, PL), (2.3, 4.0, PL), (4.0, 6.0, TR)]
    ok = ok and walk.count() == 0 and not rpi.auto_swap(rows, tokens, 1)
    ok = ok and walk.after[2] == PL and walk.after[3] == "" and walk.after[6] == TR
    return _result("walk: plough / lift / plough → transition → PLB; gaps are skips", ok,
                   f"{spans} issues={walk.issues}")


def test_build_result_and_events():
    rows = _rows(_RPL)
    walk = rpi.walk(rows, rpi.auto_tokens(rows), 1)
    result = rpi.build_result(rows, walk, {PL: "t_pl", TR: "t_tr"}, (0.0, 7.0), 1)
    burial = [(r.start_kp, r.end_kp, r.tool_id) for r in result.burial]
    ok = not result.errors and burial == [(0.5, 2.0, "t_pl"), (2.3, 4.0, "t_pl"),
                                          (4.0, 6.0, "t_tr")]
    ok = ok and result.transitions == 1
    # The window is fully covered (gaps are skip ranges), so overlay replaces it all.
    covered = sum(r.end_kp - r.start_kp for r in result.ranges)
    ok = ok and abs(covered - 7.0) < 1e-9
    ok = ok and not ev.validate_events(result.events, 0.0, 7.0, 1).errors
    # A narrower window clips.
    part = rpi.build_result(rows, walk, {}, (1.0, 3.0), 1)
    ok = ok and [(r.start_kp, r.end_kp) for r in part.burial] == [(1.0, 2.0), (2.3, 3.0)]
    ok = ok and any("outside the import window" in w for w in part.warnings)
    return _result("build_result: tools per method, transition, whole window covered, clip",
                   bool(ok), f"{burial} warnings={result.warnings}")


def test_reverse_direction_and_swap():
    rows = _rows(_RPL)
    auto = rpi.auto_tokens(rows)
    # Plan laid from the far end: in travel order PLUP comes first.
    ok = rpi.auto_swap(rows, auto, -1)
    walk = rpi.walk(rows, rpi.effective_tokens(auto, {}, True), -1)
    ok = ok and _spans(walk) == [(4.0, 6.0, TR), (2.3, 4.0, PL), (0.5, 2.0, PL)] \
        and walk.count() == 0
    result = rpi.build_result(rows, walk, {}, (0.0, 7.0), -1)
    ok = ok and not ev.validate_events(result.events, 0.0, 7.0, -1).errors
    # Labels written the wrong way round on a forward plan.
    wrong = _rows([(1.0, "PLUP", ""), (2.0, "PLDN", ""), (3.0, "PLUP", ""), (4.0, "PLDN", "")])
    wrong_auto = rpi.auto_tokens(wrong)
    ok = ok and rpi.auto_swap(wrong, wrong_auto, 1)
    fixed = rpi.walk(wrong, rpi.effective_tokens(wrong_auto, {}, True), 1)
    ok = ok and _spans(fixed) == [(1.0, 2.0, PL), (3.0, 4.0, PL)]
    return _result("swap: reverse-direction plan and wrong-way-round labels auto-detected",
                   bool(ok), f"{_spans(walk)} {_spans(fixed)}")


def test_skip_events_and_fallbacks():
    # A skip inside a plough section resumes the plough after it.
    rows = _rows([(0.0, "PLDN", ""), (1.0, "Start skip", "crossing"), (1.2, "End skip", ""),
                  (3.0, "PLUP", "")])
    walk = rpi.walk(rows, rpi.auto_tokens(rows), 1)
    ok = _spans(walk) == [(0.0, 1.0, PL), (1.0, 1.2, SK), (1.2, 3.0, PL)] and walk.count() == 0
    result = rpi.build_result(rows, walk, {}, (0.0, 3.0), 1)
    skip = [r for r in result.ranges if r.action == pi.ACTION_SKIP and r.notes]
    ok = ok and [(r.start_kp, r.end_kp, r.notes) for r in skip] == [(1.0, 1.2, "Start skip / crossing")]
    # Only skips marked: the rest of the window is burial with the plan default tool.
    only = _rows([(0.0, "", ""), (2.0, "Start surface lay", ""), (3.0, "End surface lay", ""),
                  (5.0, "", "")])
    only_walk = rpi.walk(only, rpi.auto_tokens(only), 1)
    only_result = rpi.build_result(only, only_walk, {}, (0.0, 5.0), 1)
    ok = ok and [(r.start_kp, r.end_kp) for r in only_result.burial] == [(0.0, 2.0), (3.0, 5.0)]
    ok = ok and any("only skips" in w for w in only_result.warnings)
    return _result("skip events: skip inside plough resumes it; skip-only RPL → rest is burial",
                   bool(ok), f"{_spans(walk)} {[(r.start_kp, r.end_kp) for r in only_result.burial]}")


def test_issues_are_reported_not_fatal():
    rows = _rows([(0.0, "PLUP", ""), (1.0, "PLDN", ""), (1.5, "Crossing", ""),
                  (2.0, "Start PLB", ""), (3.0, "PLDN", ""), (4.0, "", "")])
    walk = rpi.walk(rows, rpi.auto_tokens(rows), 1)
    texts = {i: [x.text for x in items] for i, items in walk.issues.items()}
    ok = any("no open section" in t for t in texts.get(0, []))
    ok = ok and any("tool change" in t for t in texts.get(3, []))
    ok = ok and any("Crossing inside" in t for t in texts.get(2, []))
    # PLDN while the PLB is open is a tool change too; it is never closed.
    ok = ok and any("tool change" in t for t in texts.get(4, []))
    ok = ok and any("never closed" in t for t in texts.get(4, []))
    ok = ok and _spans(walk) == [(1.0, 2.0, PL), (2.0, 3.0, TR), (3.0, 4.0, PL)]
    # A second start of the same tool while it is open is ignored.
    dup = _rows([(0.0, "PLDN", ""), (1.0, "PLDN", ""), (2.0, "PLUP", "")])
    dup_walk = rpi.walk(dup, rpi.auto_tokens(dup), 1)
    ok = ok and _spans(dup_walk) == [(0.0, 2.0, PL)]
    ok = ok and "ignored" in dup_walk.issues[1][0].text
    unplaced = _rows([(0.0, "PLDN", "")])
    unplaced[0].kp = None
    ok = ok and "placed" in rpi.walk(unplaced, rpi.auto_tokens(unplaced), 1).issues[0][0].text
    return _result("issues: stray end, missing PLUP, crossing, unclosed start, unplaced row",
                   bool(ok), str(texts))


def test_paint_and_set_token():
    rows = _rows([(float(k), "", "") for k in range(11)])
    tokens = {}
    # Paint rows 2..6 plough, then 4..8 PLB over it: plough 2-4, PLB 4-8.
    overrides = rpi.paint(rows, tokens, [2, 3, 4, 5, 6], PL, 1)
    tokens = rpi.effective_tokens({}, overrides)
    ok = _spans(rpi.walk(rows, tokens, 1)) == [(2.0, 6.0, PL)]
    overrides.update(rpi.paint(rows, tokens, [4, 8], TR, 1))
    tokens = rpi.effective_tokens({}, overrides)
    walk = rpi.walk(rows, tokens, 1)
    ok = ok and _spans(walk) == [(2.0, 4.0, PL), (4.0, 8.0, TR)] and walk.count() == 0
    # A skip painted inside the PLB leaves PLB either side.
    overrides.update(rpi.paint(rows, tokens, [5, 6], SK, 1))
    tokens = rpi.effective_tokens({}, overrides)
    walk = rpi.walk(rows, tokens, 1)
    ok = ok and _spans(walk) == [(2.0, 4.0, PL), (4.0, 5.0, TR), (5.0, 6.0, SK),
                                 (6.0, 8.0, TR)] and walk.count() == 0
    try:
        rpi.paint(rows, tokens, [3], PL, 1)
        ok = False
    except ValueError:
        pass
    # set_token: ends before starts; same kind replaced.
    cur = rpi.set_token([rpi.Token(S, PL)], rpi.Token(E, PL))
    ok = ok and [t.label for t in cur] == ["PLUP", "PLDN"]
    ok = ok and [t.label for t in rpi.set_token(cur, rpi.Token(S, TR))] == ["PLUP", "Start PLB"]
    return _result("paint: selected rows become one section, rest unchanged; set_token order",
                   bool(ok), str(_spans(walk)))


def test_protection_method_source():
    cases = {"Plough 1.0m": PL, "Ploughed": PL, "PLB": TR, "Jetted 1.5 m": TR, "PLIB": TR,
             "MFE": MFE, "Buried": BUR, "Burial 1.0m": BUR, "Surface laid": SK, "Surface lay": SK,
             "Not buried": SK, "No burial": SK, "Rock placement": SK, "Mattress": SK, "": SK,
             "Plough (no rock)": PL, "None": SK}
    got = {v: rpi.classify_protection(v) for v in cases}
    ok = got == cases
    # Segments (row k → k+1): surface, plough ×2, PLB, surface, "Rock" (user maps to skip).
    rows = _rows([(0.0, "", ""), (1.0, "", ""), (2.0, "", ""), (3.0, "", ""), (4.0, "", ""),
                  (5.0, "", ""), (6.0, "", "")])
    for row, value in zip(rows, ["Surface laid", "Plough 1.0m", "Plough 1.5m", "PLB",
                                 "Surface laid", "Buried", ""]):
        row.protection = value
    ok = ok and rpi.protection_values(rows)[:2] == [("Surface laid", 2), ("Plough 1.0m", 1)]
    ok = ok and rpi.has_burial_protection(rows)
    tokens = rpi.protection_tokens(rows)
    labels = {i: [t.label for t in ts] for i, ts in tokens.items()}
    ok = ok and labels == {1: ["PLDN"], 3: ["PLUP", "Start PLB"], 4: ["End PLB"],
                           5: ["Start burial"], 6: ["End burial"]}
    walk = rpi.walk(rows, tokens, 1)
    ok = ok and _spans(walk) == [(1.0, 3.0, PL), (3.0, 4.0, TR), (5.0, 6.0, BUR)]
    ok = ok and walk.count() == 0
    # The value map overrides the guess ("Buried" → skip here).
    mapped = rpi.walk(rows, rpi.protection_tokens(rows, {"buried": SK}), 1)
    ok = ok and _spans(mapped) == [(1.0, 3.0, PL), (3.0, 4.0, TR)]
    # Burial sections note the protection values they were read from.
    result = rpi.build_result(rows, mapped, {PL: "t_pl", TR: "t_tr"}, (0.0, 6.0), 1,
                              protection_notes=True)
    notes = [(r.start_kp, r.notes) for r in result.burial]
    ok = ok and notes == [(1.0, "Protection: Plough 1.0m; Plough 1.5m"), (3.0, "Protection: PLB")]
    # A reverse-direction plan reads it the other way round (auto swap).
    ok = ok and rpi.auto_swap(rows, tokens, -1)
    return _result("protection method: classify values, boundaries at changes, map, notes",
                   bool(ok), f"got={ {k: v for k, v in got.items() if v != cases[k]} } "
                   f"labels={labels} notes={notes}")


def test_segment_methods_direction_independent():
    rows = _rows(_RPL)
    auto = rpi.auto_tokens(rows)
    forward = rpi.segment_methods(rows, rpi.walk(rows, auto, 1))
    # The same RPL on a plan laid the other way (events swapped automatically)
    # buries the same segments.
    reverse = rpi.segment_methods(rows, rpi.walk(rows, rpi.effective_tokens(auto, {}, True), -1))
    expected = ["", PL, PL, "", PL, TR, TR, ""]
    skip_rows = _rows([(0.0, "", ""), (1.0, "Start skip", ""), (2.0, "End skip", ""),
                       (3.0, "", "")])
    skip_rows[3].kp = None
    marked = rpi.segment_methods(skip_rows, rpi.walk(skip_rows, rpi.auto_tokens(skip_rows), 1))
    ok = forward == expected and reverse == expected and marked == ["", SK, None]
    return _result("segment sections: by mid-point, same whichever way the plan is laid",
                   ok, f"forward={forward} reverse={reverse} marked={marked}")


def run_all():
    return [test_phrasings(), test_walk_typical_rpl(), test_build_result_and_events(),
            test_reverse_direction_and_swap(), test_skip_events_and_fallbacks(),
            test_issues_are_reported_not_fatal(), test_paint_and_set_token(),
            test_protection_method_source(), test_segment_methods_direction_independent()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
