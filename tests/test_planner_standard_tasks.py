# -*- coding: utf-8 -*-
"""Checks for the standard task template library: CSV/JSON and plan insertion."""

from datetime import datetime

from ..planner import standard_tasks
from ..planner.planner_dock import StandardTasksDialog
from ..planner.task_table import TaskTableWidget


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


class _Resolver:
    def resolve(self, _task):
        return None

    def route_length_m(self, _task):
        return None

    def clear_cache(self):
        pass


def _task(task_id, seq, level=0):
    return {
        "task_id": task_id, "seq": seq, "name": task_id.upper(),
        "outline_level": level, "resource_id": "v", "duration_mode": "manual",
        "duration_hours": 1.0, "predecessor_task_id": "", "lag_hours": 0.0,
        "speed_knots": None, "direction": "forward", "geom_kind": "",
        "feature_id": "", "notes": "",
    }


def test_csv_round_trip_and_validation():
    templates = [
        {"name": "Lay", "description": "Surface lay", "duration_hours": 1.0,
         "speed_knots": 1.5, "fuel_mode": "dp", "bunker_amount": None, "notes": "n1"},
        {"name": "Bunker call", "description": "", "duration_hours": 24.0,
         "speed_knots": None, "fuel_mode": "port", "bunker_amount": 250.0, "notes": ""},
    ]
    text = standard_tasks.templates_to_csv_text(templates)
    parsed, warnings = standard_tasks.templates_from_csv_text(text)
    ok = parsed == templates and not warnings
    messy = ("name,fuel_mode,duration_hours\n"
             "PLGR,Transit,12\n"
             "Bad,rocket,x\n"
             ",ignored,1\n")
    parsed2, warnings2 = standard_tasks.templates_from_csv_text(messy)
    ok = ok and len(parsed2) == 2
    ok = ok and parsed2[0]["fuel_mode"] == "transit" and parsed2[0]["duration_hours"] == 12.0
    ok = ok and parsed2[1]["fuel_mode"] == "" and parsed2[1]["duration_hours"] is None
    ok = ok and len(warnings2) == 2
    missing, missing_warnings = standard_tasks.templates_from_csv_text("foo,bar\n1,2\n")
    ok = ok and not missing and bool(missing_warnings)
    return _result("CSV round-trip + header mapping + validation", ok)


def test_json_round_trip_and_defaults():
    defaults = standard_tasks.default_templates()
    raw = standard_tasks.templates_to_json(defaults)
    ok = standard_tasks.templates_from_json(raw) == defaults
    ok = ok and standard_tasks.templates_from_json("not json") == []
    ok = ok and standard_tasks.templates_from_json(None) == []
    ok = ok and any(template["fuel_mode"] == "port" for template in defaults)
    return _result("JSON round-trip + starter library", ok)


def test_insert_into_plan():
    table = TaskTableWidget(_Resolver())
    rows = [_task("group", 0, 0), _task("a", 1, 1), _task("b", 2, 1)]
    resources = [{"resource_id": "v", "name": "Vessel", "kind": "vessel",
                  "color_hex": "#ff0000", "default_speed_kn": 2.0,
                  "start_offset_hours": 0.0}]
    table.set_plan(rows, resources, datetime(2026, 1, 1))
    template = {"name": "PLGR", "description": "Grapnel run", "duration_hours": 12.0,
                "speed_knots": 0.8, "fuel_mode": "dp", "bunker_amount": None,
                "notes": ""}
    task_row = standard_tasks.template_to_task_row(template, "v")
    table.insert_tasks([task_row], 2)
    ok = [row["name"] for row in table.rows] == ["GROUP", "A", "PLGR", "B"]
    ok = ok and table.rows[2]["outline_level"] == 1  # indented like new siblings
    ok = ok and table.rows[2]["fuel_mode"] == "dp"
    ok = ok and table.rows[2]["resource_id"] == "v"
    ok = ok and table.rows[2]["duration_hours"] == 12.0
    ok = ok and table.rows[2]["duration_mode"] == "manual"
    table.undo()
    ok = ok and [row["name"] for row in table.rows] == ["GROUP", "A", "B"]
    return _result("template insertion + indentation + undo", ok)


def test_dialog_round_trip_and_selection():
    templates = standard_tasks.default_templates()
    dialog = StandardTasksDialog(templates)
    ok = dialog.templates() == templates
    dialog.table.selectRow(1)
    dialog._insert()
    ok = ok and dialog.selected_templates == [templates[1]]
    dialog.close()
    return _result("dialog round-trip + insert selection", ok)


def run_all():
    return [
        test_csv_round_trip_and_validation(), test_json_round_trip_and_defaults(),
        test_insert_into_plan(), test_dialog_round_trip_and_selection(),
    ]
