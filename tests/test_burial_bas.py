# -*- coding: utf-8 -*-
"""Checks for the Burial Planner BAS register (pure): column keys and
kinds, table import with existing-column reuse, encode/decode round trip,
validation, coverage gaps, KP re-referencing provenance and CSV export."""

from __future__ import annotations

from ..burial import bas_model as bm
from ..burial import kp_rereference as kr


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" - {detail}"
    print(msg)
    return ok


def test_columns() -> bool:
    ok = bm.column_key("Req. DoL (m)") == "req_dol_m"
    ok = ok and bm.column_key("start_kp") == "f_start_kp"       # reserved
    ok = ok and bm.column_key("Soil", ["soil"]) == "soil_2"      # unique
    ok = ok and bm.column_key("") == "col"
    cols = bm.normalise_columns([{"key": "a", "label": "A", "kind": "number"},
                                 {"key": "a", "label": "dup"},
                                 {"label": "No key", "kind": "weird"},
                                 "junk"])
    ok = ok and [c["key"] for c in cols] == ["a", "no_key"]
    ok = ok and cols[1]["kind"] == bm.KIND_TEXT
    ok = ok and bm.guess_kind(["1", "2.5", "", "3"]) == bm.KIND_NUMBER
    ok = ok and bm.guess_kind(["1", "x"]) == bm.KIND_TEXT
    ok = ok and bm.guess_kind(["", ""]) == bm.KIND_TEXT
    return _result("column keys / normalise / kind guess", ok)


def test_import_and_round_trip() -> bool:
    headers = ["KP From (km)", "KP To (km)", "Req DoL (m)", "Soil", "Risk"]
    table = [["0", "2.5", "1.5", "Sand", "Low"],
             ["2.5", "4", "2", "Clay", "High"],
             ["", "", "", "", ""],
             ["x", "5", "", "", ""]]
    start, end = bm.guess_kp_columns(headers)
    ok = (start, end) == (0, 1)
    rows, cols, problems = bm.table_to_rows(headers, table, 0, 1)
    ok = ok and len(rows) == 2 and len(problems) == 1
    ok = ok and [c["key"] for c in cols] == ["req_dol_m", "soil", "risk"]
    ok = ok and cols[0]["kind"] == bm.KIND_NUMBER and cols[1]["kind"] == bm.KIND_TEXT
    ok = ok and rows[1]["values"] == {"req_dol_m": "2", "soil": "Clay", "risk": "High"}
    # Append with existing columns: matching labels reuse keys; include subset
    rows2, cols2, _p = bm.table_to_rows(["from", "to", "soil", "Extra"],
                                        [["4", "6", "Rock", "z"]], 0, 1,
                                        include=[2], existing_columns=cols)
    ok = ok and [c["key"] for c in cols2] == ["req_dol_m", "soil", "risk"]
    ok = ok and rows2[0]["values"] == {"soil": "Rock"}
    # metres
    rows3, _c, _p = bm.table_to_rows(["a", "b"], [["1500", "2500 m"]], 0, 1,
                                     kp_in_metres=True)
    ok = ok and abs(rows3[0]["start_kp"] - 1.5) < 1e-9 and abs(rows3[0]["end_kp"] - 2.5) < 1e-9
    # encode/decode round trip keeps values and drops helpers
    enc = bm.encode_row(dict(rows[0], _map_start=1.0))
    ok = ok and "values" not in enc and "_map_start" not in enc
    dec = bm.decode_row(enc)
    ok = ok and dec["values"] == rows[0]["values"] and dec["start_kp"] == 0.0
    swapped = bm.decode_row({"start_kp": "5", "end_kp": "2"})
    ok = ok and swapped["start_kp"] == 2.0 and swapped["end_kp"] == 5.0
    return _result("table import + existing columns + encode/decode", ok,
                   "; ".join(problems))


def test_validate_and_coverage() -> bool:
    cols = [{"key": "dol", "label": "DoL", "kind": "number"}]
    rows = [bm.decode_row({"start_kp": 0, "end_kp": 2, "values": {"dol": "1.5"}}),
            bm.decode_row({"start_kp": 1.5, "end_kp": 3, "values": {"dol": "abc"}}),
            bm.decode_row({"start_kp": 3, "end_kp": 3, "values": {}}),
            bm.decode_row({"start_kp": None, "end_kp": 3, "values": {}})]
    issues = bm.validate_rows(rows, cols)
    ok = any("overlap" in i for i in issues) and any("not a number" in i for i in issues)
    ok = ok and any("greater than" in i for i in issues) and any("required" in i for i in issues)
    clean = [bm.decode_row({"start_kp": 0, "end_kp": 2}), bm.decode_row({"start_kp": 5, "end_kp": 8})]
    ok = ok and not bm.validate_rows(clean, cols)
    gaps = bm.coverage_gaps(clean, 0.0, 10.0)
    ok = ok and gaps == [(2.0, 5.0), (8.0, 10.0)]
    ok = ok and bm.coverage_gaps(clean, 0.0, 2.0) == []
    ok = ok and [r["end_kp"] for r in bm.rows_at_kp(clean, 6.0)] == [8.0]
    ordered = bm.sort_rows(reversed(clean))
    ok = ok and [r["seq"] for r in ordered] == [0, 1] and ordered[0]["start_kp"] == 0.0
    return _result("validate + coverage gaps + rows_at_kp + sort", ok, "; ".join(issues[:2]))


def test_rereference_and_csv() -> bool:
    m = kr.KpMap.from_anchors([(0.0, 0.0), (10.0, 10.5)])
    rows = [bm.decode_row({"start_kp": 2, "end_kp": 4, "values": {"dol": "1"}}),
            bm.decode_row({"start_kp": 6, "end_kp": 8, "src_start_kp": 5, "src_end_kp": 7,
                           "src_rpl": "Rev A", "values": {"dol": "2"}})]
    mapped, tally = bm.rereference_rows(rows, m, source_label="Rev B")
    ok = abs(mapped[0]["start_kp"] - 2.1) < 1e-9 and mapped[0]["src_start_kp"] == 2.0
    ok = ok and mapped[0]["src_rpl"] == "Rev B" and mapped[0]["values"] == {"dol": "1"}
    ok = ok and abs(mapped[1]["start_kp"] - 5.25) < 1e-9 and mapped[1]["src_rpl"] == "Rev A"
    text = bm.rows_csv({"name": "P", "rpl_name": "R", "rpl_revision": "Rev D"}, mapped,
                       [{"key": "dol", "label": "Req DoL (m)", "kind": "number"}])
    ok = ok and "# rpl: R Rev D" in text and "start_kp,end_kp,Req DoL (m),src_start_kp" in text
    ok = ok and "2.100,4.200,1,2.000,4.000,Rev B" in text
    ok = ok and isinstance(tally, dict)
    return _result("re-reference provenance + CSV export", ok)


def test_layer_fields_values() -> bool:
    cols = [{"key": "req_dol_m", "label": "Req DoL", "kind": "number"},
            {"key": "soil", "label": "Soil", "kind": "text"},
            {"key": "start_kp", "label": "clash", "kind": "text"}]  # ignored: fixed name
    specs = bm.layer_fields(cols)
    names = [n for n, _t in specs]
    ok = names[:5] == ["row_id", "plan_id", "start_kp", "end_kp", "length_km"]
    ok = ok and names[5:7] == ["req_dol_m", "soil"] and names[-1] == "notes"
    ok = ok and dict(specs)["req_dol_m"] == "float" and dict(specs)["soil"] == "str"
    row = bm.decode_row({"start_kp": 0, "end_kp": 1,
                         "values": {"req_dol_m": "1,5", "soil": "Sand"}})
    values = bm.layer_values(row, cols)
    ok = ok and values == {"req_dol_m": 1.5, "soil": "Sand"}
    bad = bm.layer_values(bm.decode_row({"values": {"req_dol_m": "n/a"}}), cols)
    ok = ok and bad["req_dol_m"] is None and bad["soil"] == ""
    return _result("map layer fields + values", ok)


def test_column_decimals() -> bool:
    cols = bm.normalise_columns([
        {"key": "dist", "label": "Distance (km)", "kind": "number", "decimals": 3},
        {"key": "dol", "label": "DoL", "kind": "number", "decimals": "9"},
        {"key": "soil", "label": "Soil", "kind": "text", "decimals": 2}])
    ok = cols[0].get("decimals") == 3
    ok = ok and "decimals" not in cols[1]           # out of range -> as entered
    ok = ok and "decimals" not in cols[2]           # text columns never round
    dist = cols[0]
    ok = ok and bm.format_value("12.3456789", dist) == "12.346"
    ok = ok and bm.format_value("12,3456789", dist) == "12.346"
    ok = ok and bm.format_value("", dist) == ""
    ok = ok and bm.format_value("n/a", dist) == "n/a"
    ok = ok and bm.format_value("-0.0001", dist) == "0.000"
    ok = ok and bm.format_value("12.3456789", cols[1]) == "12.3456789"
    ok = ok and bm.format_value("1.5", {"kind": "number", "decimals": 0}) == "2"
    ok = ok and bm.decimals_label(None) == "As entered"
    ok = ok and bm.decimals_label(0) == "0 (1)"
    ok = ok and bm.decimals_label(3) == "3 (0.001)"
    row = {"start_kp": 0.0, "end_kp": 1.0, "values": {"dist": "1.23456"}}
    ok = ok and bm.layer_values(bm.encode_row(row), [dist])["dist"] == 1.235
    csv_text = bm.rows_csv({"name": "P"}, [row], [dist])
    ok = ok and "1.235" in csv_text and "1.23456" not in csv_text
    return _result("number column decimals: normalise, format, layer, CSV", ok)


def run_all():
    return [test_layer_fields_values(), test_columns(), test_import_and_round_trip(),
            test_validate_and_coverage(), test_rereference_and_csv(),
            test_column_decimals()]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(run_all()) else 1)
