# -*- coding: utf-8 -*-
"""QGIS widget checks for Planner task defaults, grouping, and labels."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from ..planner.planner_dock import (
    AdvancedTaskDialog, ProgressDialog, ScheduleAvailabilityDialog,
    _simulation_label,
)
from ..planner.task_table import TaskTableWidget
from ..planner.timeline_engine import TaskFuel


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


class _Resolver:
    def route_length_m(self, _task):
        return None

    def resolve(self, _task):
        return None


def _row(task_id, name, seq, predecessor=""):
    return {
        "task_id": task_id, "seq": seq, "name": name, "outline_level": 0,
        "resource_id": "v1", "duration_mode": "manual", "duration_hours": 1.0,
        "predecessor_task_id": predecessor, "lag_hours": 0.0,
        "direction": "forward", "fuel_mode": "", "layer_id": "layer-1",
        "layer_source": "source", "layer_name": "Positions", "feature_id": "7",
        "feature_label": "KP 12.300", "geom_kind": "point",
        "linked_ref_json": '{"feature_id":"7"}',
    }


def test_new_task_inherits_previous_context():
    table = TaskTableWidget(_Resolver())
    table.set_plan([_row("a", "Previous", 0)], [{
        "resource_id": "v1", "name": "Vessel 1", "default_speed_kn": 1.2,
    }], datetime(2026, 1, 1))
    table.add_task()
    added = table.rows[-1]
    ok = added["predecessor_task_id"] == "a"
    ok = ok and added["resource_id"] == "v1"
    ok = ok and added["feature_id"] == "7" and added["feature_label"] == "KP 12.300"
    ok = ok and added["geom_kind"] == "point"
    table.close()
    return _result("new task inherits predecessor and feature context", ok)


def test_new_task_uses_previous_line_endpoint():
    previous = _row("a", "Previous route", 0)
    previous.update({
        "geom_kind": "line", "direction": "forward",
        "linked_ref_json": '{"owned_geometry":true,"source_kind":"sketch"}',
    })
    table = TaskTableWidget(_Resolver())
    table.set_plan([previous], [{"resource_id": "v1", "name": "Vessel 1"}],
                   datetime(2026, 1, 1))
    table.add_task()
    added = table.rows[-1]
    ok = added["location_mode"] == "line_end"
    ok = ok and table.spatial_kind(added) == "point"
    ok = ok and added["predecessor_task_id"] == "a"
    ok = ok and "owned_geometry" not in added["linked_ref_json"]
    ok = ok and '"referenced_task_id": "a"' in added["linked_ref_json"]
    table.close()
    return _result("follow-on task references previous route endpoint", ok)


def test_group_selected_tasks():
    table = TaskTableWidget(_Resolver())
    table.set_plan([
        _row("a", "First", 0), _row("b", "Second", 1, "a"),
    ], [{"resource_id": "v1", "name": "Vessel 1"}], datetime(2026, 1, 1))
    table._select_rows([0, 1])
    table.group_selected()
    ok = len(table.rows) == 3 and table.rows[0]["name"] == "New group"
    ok = ok and [row["outline_level"] for row in table.rows] == [0, 1, 1]
    ok = ok and table._is_summary(0)
    table.close()
    return _result("right-click grouping creates an outline summary", ok)


def test_fuel_rob_label():
    state = SimpleNamespace(fraction=0.25, active=True)
    task_fuel = TaskFuel("a", burn=20.0, rob_start=100.0, rob_end=80.0)
    text = _simulation_label(
        {"name": "Lay", "seq": 0}, {"name": "Vessel 1"}, state, None,
        datetime(2026, 1, 1), {"show": True, "fuel_rob": True},
        task_fuel=task_fuel, fuel_unit="t")
    ok = "Fuel ROB 95 t" in text
    off = _simulation_label(
        {"name": "Lay", "seq": 0}, {}, state, None, datetime(2026, 1, 1),
        {"show": True}, task_fuel=task_fuel, fuel_unit="t")
    ok = ok and "Fuel ROB" not in off
    return _result("fuel ROB playback label is optional and interpolated", ok)


def test_progress_update_appends_history():
    task = _row("a", "Task", 0)
    task.update({
        "progress_status": "not_started", "percent_complete": 0.0,
        "actual_log_json": "[]",
    })
    dialog = ProgressDialog(task)
    dialog.status.setCurrentIndex(dialog.status.findData("in_progress"))
    dialog.percent.setValue(35.0)
    dialog.notes.setPlainText("Weather delay recorded offshore")
    values = dialog.values()
    history = __import__("json").loads(values["actual_log_json"])
    ok = values["progress_status"] == "in_progress"
    ok = ok and values["percent_complete"] == 35.0
    ok = ok and len(history) == 1 and "Weather delay" in history[0]["note"]
    dialog.close()
    return _result("actual progress update appends auditable history", ok)


def test_optional_advanced_dialogs():
    task = _row("a", "Task", 0)
    task.update({"geom_kind": "line", "operation_type": "lay"})
    advanced = AdvancedTaskDialog(task)
    advanced.dependency.setCurrentIndex(advanced.dependency.findData("SS"))
    advanced.lag.setValue(2.0)
    advanced.constraint.setCurrentIndex(advanced.constraint.findData("snet"))
    advanced.location.setCurrentIndex(advanced.location.findData("line_end"))
    values = advanced.values()
    ok = values["dependency_type"] == "SS" and values["lag_hours"] == 2.0
    ok = ok and values["constraint_type"] == "snet"
    ok = ok and values["location_mode"] == "line_end"
    advanced.close()

    availability = ScheduleAvailabilityDialog(
        [{"resource_id": "v1", "name": "Vessel 1", "start_offset_hours": 2.0}],
        {}, datetime(2026, 1, 1))
    availability._rows[0][1].setChecked(True)
    dates = availability.values()
    ok = ok and dates["v1"] == "2026-01-01T02:00"
    availability.close()
    return _result("optional advanced task and availability dialogs", ok)


def test_duplicate_selected_block():
    group = _row("g", "Campaign", 0)
    group["outline_level"] = 0
    first = _row("a", "Lay", 1)
    first["outline_level"] = 1
    first.update({
        "geom_kind": "line", "progress_status": "completed",
        "percent_complete": 100.0, "actual_finish_datetime": "2026-01-02T00:00",
        "linked_ref_json": '{"owned_geometry":true,"source_kind":"sketch"}',
    })
    second = _row("b", "Bury", 2, "a")
    second["outline_level"] = 1
    table = TaskTableWidget(_Resolver())
    table.set_plan([group, first, second],
                   [{"resource_id": "v1", "name": "Vessel 1"}],
                   datetime(2026, 1, 1))
    table.selectRow(0)  # summary row; descendants come along automatically
    table.duplicate_selected()
    ok = len(table.rows) == 6
    copies = table.rows[3:]
    copy_ids = [row.get("task_id") for row in copies]
    ok = ok and len(set(copy_ids) - {"g", "a", "b"}) == 3
    copy_group, copy_first, copy_second = copies
    ok = ok and copy_group.get("name") == "Campaign"
    ok = ok and int(copy_group.get("outline_level") or 0) == 0
    ok = ok and int(copy_first.get("outline_level") or 0) == 1
    # Head chains after the last original task; in-block link is remapped.
    ok = ok and copy_first.get("predecessor_task_id") == "b"
    ok = ok and copy_second.get("predecessor_task_id") == copy_first.get("task_id")
    # Actual progress does not survive duplication.
    ok = ok and copy_first.get("progress_status") == "not_started"
    ok = ok and float(copy_first.get("percent_complete") or 0.0) == 0.0
    ok = ok and not copy_first.get("actual_finish_datetime")
    # An owned sketch is shared, never cloned: the copy references task "a".
    ok = ok and "owned_geometry" not in (copy_first.get("linked_ref_json") or "")
    ok = ok and '"owner_task_id": "a"' in (copy_first.get("linked_ref_json") or "")
    table.close()
    return _result("duplicate group: fresh ids, chained heads, shared sketch", ok)


def run_all():
    return [
        test_new_task_inherits_previous_context(), test_new_task_uses_previous_line_endpoint(),
        test_group_selected_tasks(), test_fuel_rob_label(),
        test_progress_update_appends_history(), test_optional_advanced_dialogs(),
        test_duplicate_selected_block(),
    ]
