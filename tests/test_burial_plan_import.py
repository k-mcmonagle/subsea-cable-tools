# -*- coding: utf-8 -*-
"""Burial plan import from KP-range tables (burial.plan_import, no QGIS)."""

import os
import tempfile

from ..burial import events as ev
from ..burial import plan_import as pi
from ..burial import schema


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


_TOOLS = [{"tool_id": "t_pl", "name": "SMD Plough", "tool_type": "plough"},
          {"tool_id": "t_tr", "name": "Q1400 Trencher", "tool_type": "trencher"}]

# A contractor-style sheet: title rows, a header, KP ranges with an action
# column that mixes tool words and skip wording, a notes column.
_CSV = """Burial Plan Rev B,,,,
Client: Example,,,,
Section,KP From,KP To,Activity,Remarks
1,0.000,1.250,Plough,start of plan
2,1.250,1.600,Skip,cable crossing C-12
3,1.600,4.000,Plough,
4,4.000,4.800,Surface lay,rock outcrop
5,4.800,6.500,PLB (trencher),post-lay burial
6,6.500,7.000,Skip,
"""


def _rows():
    return pi.parse_delimited(_CSV)


def test_parse_kp_forms():
    cases = {"12.345": 12.345, "12,345": 12.345, "KP 7.5": 7.5, "3+250": 3.25,
             "": None, "abc": None, " 4.2km ": 4.2}
    got = {k: pi.parse_kp(k) for k in cases}
    ok = all((got[k] is None) == (v is None) and (v is None or abs(got[k] - v) < 1e-9)
             for k, v in cases.items())
    ok = ok and abs(pi.parse_kp("1250", 0.001) - 1.25) < 1e-12
    return _result("KP cells: decimal comma, 'KP' prefix, km+m chainage, metres", ok, str(got))


def test_header_and_roles_guess():
    rows = _rows()
    header = pi.guess_header_row(rows)
    roles = pi.guess_roles(rows[header], rows[header + 1:])
    expected = [pi.ROLE_IGNORE, pi.ROLE_START, pi.ROLE_END, pi.ROLE_ACTION, pi.ROLE_NOTES]
    # Headerless numeric table: first two numeric columns become KPs.
    bare = [["0.5", "1.5", "Plough"], ["2.0", "3.0", "Skip"]]
    bare_roles = pi.guess_roles(["", "", ""], bare)
    ok = header == 2 and roles == expected and pi.guess_header_row(bare) == -1
    ok = ok and bare_roles[:2] == [pi.ROLE_START, pi.ROLE_END]
    return _result("header row found under title rows; roles guessed from titles/content", ok,
                   f"header={header} roles={roles} bare={bare_roles}")


def test_value_guesses():
    actions = {v: pi.guess_action(v) for v in
               ("Plough", "PLB (trencher)", "Skip", "Surface lay", "Y", "N", "MFE",
                "Jetting", "No burial", "post-lay burial", "Something else", "")}
    ok = actions["Plough"] == actions["PLB (trencher)"] == actions["MFE"] == pi.ACTION_BURY
    ok = ok and actions["Jetting"] == actions["Y"] == actions["post-lay burial"] == pi.ACTION_BURY
    ok = ok and actions["Skip"] == actions["Surface lay"] == actions["N"] == pi.ACTION_SKIP
    ok = ok and actions["No burial"] == pi.ACTION_SKIP
    ok = ok and actions["Something else"] == actions[""] == pi.ACTION_IGNORE
    tools = {v: pi.guess_tool(v, _TOOLS) for v in ("Plough", "PLB (trencher)", "q1400 trencher", "Skip")}
    ok = ok and tools == {"Plough": "t_pl", "PLB (trencher)": "t_tr", "q1400 trencher": "t_tr", "Skip": ""}
    return _result("value guesses: bury/skip wording and registered tools", ok, f"{actions} {tools}")


def _spec(rows, header=2):
    roles = pi.guess_roles(rows[header], rows[header + 1:])
    spec = pi.ImportSpec(roles=roles)
    col = roles.index(pi.ROLE_ACTION)
    for value, _count in pi.distinct_values(rows[header + 1:], col):
        spec.action_map[value] = pi.guess_action(value)
        spec.tool_map[value] = pi.guess_tool(value, _TOOLS)
    return spec


def test_build_plan_merges_and_orders():
    rows = _rows()
    names = {t["tool_id"]: t["name"] for t in _TOOLS}
    result = pi.build_plan(rows[3:], _spec(rows), (0.0, 7.0), 1, 4, names)
    burial = [(r.start_kp, r.end_kp, r.tool_id) for r in result.burial]
    ok = not result.errors and burial == [(0.0, 1.25, "t_pl"), (1.6, 4.0, "t_pl"), (4.8, 6.5, "t_tr")]
    kinds = [(e["event_type"], e["kp"]) for e in result.events]
    ok = ok and kinds[0] == (schema.EVENT_BURIAL_START, 0.0) and kinds[-1] == (schema.EVENT_BURIAL_END, 6.5)
    ok = ok and not ev.validate_events(result.events, 0.0, 7.0, 1).errors
    ok = ok and abs(result.burial_km - (1.25 + 2.4 + 1.7)) < 1e-9
    ok = ok and result.burial[2].notes == "post-lay burial"
    return _result("KP ranges → merged burial ranges, per-range tool and valid events", ok, str(burial))


def test_reverse_direction_events():
    rows = _rows()
    result = pi.build_plan(rows[3:], _spec(rows), (0.0, 7.0), -1, 4)
    first = result.events[0]
    ok = first["event_type"] == schema.EVENT_BURIAL_START and abs(first["kp"] - 1.25) < 1e-9
    ok = ok and not ev.validate_events(result.events, 0.0, 7.0, -1).errors
    return _result("decreasing-KP plans get START at the high KP (valid alternation)", ok,
                   str([(e["event_type"][-5:], e["kp"]) for e in result.events[:2]]))


def test_contiguous_mixed_tools_and_overlaps():
    rows = [["Start", "End", "Tool"], ["0", "2", "Plough"], ["2", "3", "PLB"],
            ["5", "6", "Plough"], ["5.5", "7", "Plough"]]
    spec = pi.ImportSpec(roles=[pi.ROLE_START, pi.ROLE_END, pi.ROLE_TOOL],
                         tool_map={"Plough": "t_pl", "PLB": "t_tr"})
    result = pi.build_plan(rows[1:3], spec, (0.0, 10.0), 1, 2, {"t_pl": "SMD Plough", "t_tr": "Q1400"})
    # Plough 0-2 then PLB 2-3: a tool transition at KP 2 (PLUP + Start PLB).
    ok = [(r.start_kp, r.end_kp, r.tool_id) for r in result.burial] == \
        [(0.0, 2.0, "t_pl"), (2.0, 3.0, "t_tr")]
    ok = ok and result.transitions == 1 and not ev.validate_events(result.events, 0.0, 10.0, 1).errors
    kps = sorted(e["kp"] for e in result.events)
    ok = ok and kps == [0.0, 2.0, 2.0, 3.0]
    overlap = pi.build_plan(rows[3:], spec, (0.0, 10.0), 1, 4)
    ok = ok and overlap.errors and "overlap" in overlap.errors[0] and not overlap.events
    return _result("continuous Plough→PLB becomes a tool transition; overlapping rows are errors",
                   bool(ok), f"{result.burial} / {overlap.errors}")


def test_scope_clipping_and_bad_rows():
    rows = [["-1", "2"], ["3", "3"], ["abc", "4"], ["9", "12"], ["20", "25"]]
    spec = pi.ImportSpec(roles=[pi.ROLE_START, pi.ROLE_END])
    result = pi.build_plan(rows, spec, (0.0, 10.0), 1, 1)
    burial = [(r.start_kp, r.end_kp) for r in result.burial]
    ok = burial == [(0.0, 2.0), (9.0, 10.0)] and result.skipped_rows == 2
    ok = ok and any("clipped" in w for w in result.warnings) and any("outside" in w for w in result.warnings)
    return _result("scope clipping, out-of-scope, zero-length and non-numeric rows", ok,
                   f"{burial} {result.warnings}")


def _event(kp, kind, **extra):
    base = {"event_id": f"e{kp}{kind[-3:]}", "event_type": kind, "kp": kp, "locked": 0,
            "status": schema.EVENT_STATUS_CANDIDATE, "notes": ""}
    base.update(extra)
    return base


def test_overlay_keeps_uncovered_plan():
    S, E = schema.EVENT_BURIAL_START, schema.EVENT_BURIAL_END
    existing = [_event(0.0, S), _event(2.0, E), _event(3.0, S, locked=1), _event(8.0, E)]
    # File covers KP 4-6: skip 4-5, bury 5-6.
    rows = [["4", "5", "Skip"], ["5", "6", "Bury"]]
    spec = pi.ImportSpec(roles=[pi.ROLE_START, pi.ROLE_END, pi.ROLE_ACTION],
                         action_map={"Skip": pi.ACTION_SKIP, "Bury": pi.ACTION_BURY})
    result = pi.build_plan(rows, spec, (0.0, 10.0), 1, 1)
    events, dropped = pi.plan_events(existing, result, pi.MODE_MERGE, 1, (0.0, 10.0))
    pairs = [(a["kp"], b["kp"] if b else None) for a, b in ev.burial_pairs(events, 1)]
    ok = pairs == [(0.0, 2.0), (3.0, 4.0), (5.0, 8.0)]
    kept_ids = {e["event_id"] for e in events}
    ok = ok and {"e0.0ART", "e2.0END", "e3.0ART", "e8.0END"} <= kept_ids and not dropped
    ok = ok and not ev.validate_events(events, 0.0, 10.0, 1).errors
    replaced, dropped2 = pi.plan_events(existing, result, pi.MODE_REPLACE, 1, (0.0, 10.0))
    ok = ok and [(a["kp"], b["kp"]) for a, b in ev.burial_pairs(replaced, 1)] == [(5.0, 6.0)]
    ok = ok and any(int(e.get("locked") or 0) for e in dropped2)
    return _result("overlay replaces only covered KPs and reuses existing events; replace reports "
                   "dropped locked events", ok, f"{pairs}")


def test_section_updates_by_kp():
    ranges = [pi.ImportRange(2, 0.0, 1.25, pi.ACTION_BURY, "t_pl", "a"),
              pi.ImportRange(5, 4.8, 6.5, pi.ACTION_BURY, "", "")]
    skips = [pi.ImportRange(3, 1.25, 1.6, pi.ACTION_SKIP, "", "crossing")]
    sections = [{"section_id": "s1", "kind": schema.SECTION_BURIAL, "start_kp": 0.0, "end_kp": 1.25},
                {"section_id": "s2", "kind": schema.SECTION_SKIP, "start_kp": 1.25, "end_kp": 4.8},
                {"section_id": "s3", "kind": schema.SECTION_BURIAL, "start_kp": 4.8, "end_kp": 6.5}]
    updates = pi.section_updates(sections, ranges, skips)
    ok = updates == {"s1": {"tool_id": "t_pl", "notes": "a"}, "s2": {"notes": "crossing"}}
    return _result("derived sections receive imported tool and notes by KP", ok, str(updates))


def test_read_grid_xlsx():
    try:
        import openpyxl
    except Exception:
        return _result("xlsx grid read (skipped: no openpyxl in this Python)", True)
    with tempfile.TemporaryDirectory() as temp:
        path = os.path.join(temp, "plan.xlsx")
        book = openpyxl.Workbook()
        ws = book.active
        ws.title = "Plan"
        for row in (["Burial plan"], [], ["KP From", "KP To", "Action"], [0, 1.5, "Plough"],
                    [1.5, 2.25, "Skip"]):
            ws.append(row)
        book.create_sheet("Other")
        book.save(path)
        del book
        rows, sheets = pi.read_grid(path)
        import gc
        gc.collect()
    ok = sheets == ["Plan", "Other"] and rows[1] == ["KP From", "KP To", "Action"]
    ok = ok and rows[2][:2] == ["0", "1.5"] and pi.guess_header_row(rows) == 1
    return _result("xlsx grid read: sheets, integer cells without '.0', header row", ok, str(rows[:3]))


def run_all():
    return [test_parse_kp_forms(), test_header_and_roles_guess(), test_value_guesses(),
            test_build_plan_merges_and_orders(), test_reverse_direction_events(),
            test_contiguous_mixed_tools_and_overlaps(), test_scope_clipping_and_bad_rows(),
            test_overlay_keeps_uncovered_plan(), test_section_updates_by_kp(), test_read_grid_xlsx()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
