# -*- coding: utf-8 -*-
"""Standalone checks for MS Project paste / CSV task import parsing."""

from ..planner.task_import import (
    build_task_rows, detect_header, guess_roles, parse_duration_hours,
    parse_predecessors, split_rows,
)

RESOURCES = [
    {"resource_id": "r1", "name": "Vessel 1"},
    {"resource_id": "r2", "name": "Barge A"},
]


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def test_duration_parsing():
    cases = (
        ("36", 36.0), ("1.5 days", 36.0), ("2 ehrs", 2.0), ("30 mins", 0.5),
        ("1w", 168.0), ("2 edays", 48.0), ("0.5 hrs", 0.5), ("3d", 72.0),
        ("", None), ("soon", None), ("2 fortnights", None),
    )
    ok = all(parse_duration_hours(raw) == expected for raw, expected in cases)
    detail = "; ".join("%r->%r" % (raw, parse_duration_hours(raw)) for raw, _ in cases)
    return _result("MS Project duration strings to hours", ok, detail)


def test_predecessor_parsing():
    ok = (parse_predecessors("3") == [(3, "FS", 0.0)]
          and parse_predecessors("3FS+2 days") == [(3, "FS", 48.0)]
          and parse_predecessors("12SS-4 hrs") == [(12, "SS", -4.0)]
          and parse_predecessors("2FF;5") == [(2, "FF", 0.0), (5, "FS", 0.0)]
          and parse_predecessors("") == []
          and parse_predecessors("after mobilisation") is None)
    return _result("MS Project predecessor tokens", ok)


def test_msproject_paste_round_trip():
    # Shape produced by the planner's own "Copy for MS Project" export.
    text = ("Mobilisation\t24 ehrs\t14/08/2026 03:20\t15/08/2026 03:20\t\tVessel 1\n"
            "Cable lay\t36 ehrs\t15/08/2026 03:20\t16/08/2026 15:20\t1\tVessel 1\n"
            "Demob\t12 ehrs\t16/08/2026 15:20\t17/08/2026 03:20\t2SS+6h\tBarge A")
    rows = split_rows(text)
    has_header = detect_header(rows)
    roles = guess_roles(rows, has_header, [r["name"] for r in RESOURCES])
    tasks, warnings = build_task_rows(rows, roles, has_header, RESOURCES)
    ok = (not has_header
          and roles == ["name", "duration", "start", "finish", "predecessor", "resource"]
          and len(tasks) == 3 and not warnings
          and tasks[0]["name"] == "Mobilisation"
          and tasks[0]["duration_hours"] == 24.0
          and tasks[0]["resource_id"] == "r1"
          and tasks[1]["predecessor_task_id"] == tasks[0]["task_id"]
          and tasks[1]["dependency_type"] == "FS"
          and tasks[2]["predecessor_task_id"] == tasks[1]["task_id"]
          and tasks[2]["dependency_type"] == "SS"
          and tasks[2]["lag_hours"] == 6.0
          and tasks[2]["resource_id"] == "r2")
    return _result("headerless MS Project paste round trip", ok,
                   "roles=%s warnings=%s" % (roles, warnings))


def test_csv_with_header_and_outline():
    text = ("name,duration,outline level,predecessors,resource names,notes\n"
            "Phase 1,0,1,,,\n"
            "Transit,12 hrs,2,,Vessel 1,to site\n"
            "Lay,2 days,2,2FS+1h,Vessel 1,main lay\n")
    rows = split_rows(text)
    has_header = detect_header(rows)
    roles = guess_roles(rows, has_header, [r["name"] for r in RESOURCES])
    tasks, warnings = build_task_rows(rows, roles, has_header, RESOURCES,
                                      first_row_id=1)
    ok = (has_header
          and roles == ["name", "duration", "outline", "predecessor", "resource", "notes"]
          and len(tasks) == 3 and not warnings
          and tasks[0]["outline_level"] == 0 and tasks[1]["outline_level"] == 1
          and tasks[1]["notes"] == "to site"
          and tasks[2]["duration_hours"] == 48.0
          and tasks[2]["predecessor_task_id"] == tasks[1]["task_id"]
          and tasks[2]["lag_hours"] == 1.0
          # Row without an explicit predecessor chains to the previous task.
          and tasks[1]["predecessor_task_id"] == tasks[0]["task_id"])
    return _result("CSV with header, outline levels, chaining", ok,
                   "roles=%s warnings=%s" % (roles, warnings))


def test_predecessor_offset_and_warnings():
    text = ("Task A\t6 hrs\t\n"
            "Task B\t6 hrs\t7\n"
            "Task C\t6 hrs\t99")
    rows = split_rows(text)
    roles = ["name", "duration", "predecessor"]
    tasks, warnings = build_task_rows(rows, roles, False, RESOURCES,
                                      first_row_id=7,
                                      chain_missing_predecessors=False)
    ok = (len(tasks) == 3
          and tasks[1]["predecessor_task_id"] == tasks[0]["task_id"]
          and tasks[2]["predecessor_task_id"] == ""
          and any("outside the imported rows" in w for w in warnings)
          and tasks[0]["predecessor_task_id"] == "")
    return _result("first-row ID offset + out-of-range warning", ok,
                   "warnings=%s" % warnings)


def test_bad_rows_and_unknown_resource():
    text = ("name;duration;resource\n"
            ";12 hrs;Vessel 1\n"
            "Jointing;one day;MV Unknown\n")
    rows = split_rows(text)
    has_header = detect_header(rows)
    roles = guess_roles(rows, has_header, [r["name"] for r in RESOURCES])
    tasks, warnings = build_task_rows(rows, roles, has_header, RESOURCES)
    ok = (len(tasks) == 1 and tasks[0]["name"] == "Jointing"
          and tasks[0]["duration_hours"] == 1.0
          and tasks[0]["resource_id"] == "r1"
          and any("no task name" in w for w in warnings)
          and any("could not read duration" in w for w in warnings)
          and any("MV Unknown" in w for w in warnings))
    return _result("semicolon CSV, skipped/defaulted rows, warnings", ok,
                   "warnings=%s" % warnings)


def test_name_only_lines():
    text = "Mobilise\nTransit to site\nLay shore end"
    rows = split_rows(text)
    has_header = detect_header(rows)
    roles = guess_roles(rows, has_header, [])
    tasks, warnings = build_task_rows(rows, roles, has_header, RESOURCES)
    ok = (not has_header and roles == ["name"] and len(tasks) == 3
          and tasks[0]["duration_hours"] == 1.0
          and tasks[2]["predecessor_task_id"] == tasks[1]["task_id"])
    return _result("plain task-name lines", ok, "roles=%s" % roles)


def run_all():
    return [test_duration_parsing(), test_predecessor_parsing(),
            test_msproject_paste_round_trip(), test_csv_with_header_and_outline(),
            test_predecessor_offset_and_warnings(),
            test_bad_rows_and_unknown_resource(), test_name_only_lines()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
