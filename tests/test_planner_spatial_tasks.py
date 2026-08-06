# -*- coding: utf-8 -*-
"""QGIS checks for sketch lifecycle and connected spatial-task line merging."""

from datetime import datetime
import os
import tempfile
from types import MethodType

from qgis.PyQt.QtCore import QPoint, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QTableWidget
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPointXY, QgsProject,
    QgsRectangle, QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapTool

from ..planner.map_overlay import PlannerMapOverlay
from ..planner import schema
from ..planner.planner_dock import PlannerDock, ResourceDialog, SketchTasksDialog, _simulation_label
from ..planner.sketch_tool import SketchSession, _parse_lat_lon
from ..planner.spatial_tasks import _join_connected, _reversed_geometry
from ..planner.store import PlannerStore
from ..planner.task_table import TaskTableWidget, _float
from ..planner.timeline_engine import ActiveState, ScheduledTask
from ..qgis_compat import ITEM_FLAG_EDITABLE


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def test_connected_merge_and_reverse():
    first = QgsGeometry.fromWkt("LINESTRING(0 0, 1 0)")
    second = QgsGeometry.fromWkt("LINESTRING(2 0, 1 0)")
    merged = _join_connected([first, second], 1e-8)
    points = merged.asPolyline() if merged else []
    reversed_first = _reversed_geometry(first).asPolyline()
    ok = [point.x() for point in points] == [0.0, 1.0, 2.0]
    ok = ok and [point.x() for point in reversed_first] == [1.0, 0.0]
    disconnected = _join_connected(
        [first, QgsGeometry.fromWkt("LINESTRING(5 0, 6 0)")], 1e-8)
    ok = ok and disconnected is None
    return _result("connected merge/reverse + disconnected rejection", ok)


def test_sketch_session_completion_and_restore():
    canvas = QgsMapCanvas()
    completed = []
    session = SketchSession(canvas)
    session.start("line", lambda points: completed.append(points))
    tool = session.tool
    tool.points = [QgsPointXY(0, 0), QgsPointXY(1, 0), QgsPointXY(2, 0)]
    tool._refresh()
    tool.set_paused(True)
    ok = tool.paused and session.control.pause_btn.text() == "Continue"
    tool.set_paused(False)
    tool.undo()
    tool.points.append(QgsPointXY(2, 0))
    tool.finish()
    ok = ok and len(completed) == 1 and len(completed[0]) == 3
    ok = ok and session.tool is None and canvas.mapTool() is not tool
    return _result("sketch completion restores previous map tool", ok)


def test_sketch_does_not_reopen_transit_measure():
    class TransitMeasureTool(QgsMapTool):
        pass

    canvas = QgsMapCanvas()
    transit = TransitMeasureTool(canvas)
    pan = QgsMapTool(canvas)
    canvas.setMapTool(transit)
    session = SketchSession(canvas, fallback_tool=lambda: canvas.setMapTool(pan))
    session.start("line", lambda _points: None)
    session.tool.points = [QgsPointXY(0, 0), QgsPointXY(1, 0)]
    session.tool.finish()
    ok = canvas.mapTool() is pan and canvas.mapTool() is not transit
    return _result("planner sketch falls back to pan instead of reopening Transit Measure", ok)


def test_sketch_snapping_exact_coordinate_and_route_kp():
    class _Frame:
        def point_at_kp(self, kp_km, clamp=False):
            return QgsPointXY(kp_km, 10.0)

    canvas = QgsMapCanvas()
    canvas.resize(640, 480)
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    snap_layer = QgsVectorLayer("Point?crs=EPSG:4326", "Snap target", "memory")
    snap_feature = QgsFeature()
    snap_feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(5.0, 5.0)))
    snap_layer.dataProvider().addFeature(snap_feature)
    QgsProject.instance().addMapLayer(snap_layer)
    canvas.setLayers([snap_layer])
    canvas.setExtent(QgsRectangle(0.0, 0.0, 10.0, 10.0))
    original_snapping = canvas.snappingUtils().config()
    completed = []
    session = SketchSession(canvas)
    session.start("line", lambda points: completed.append(points), route_options=[{
        "label": "Imported route (KP 100.000–102.000)", "frame": _Frame(),
        "kp_start": 100.0, "kp_end": 102.0,
    }])
    tool = session.tool
    control = session.control
    ok = tool.snap_enabled and tool.snap_vertices and tool.snap_segments
    ok = ok and canvas.snappingUtils().config().enabled()
    pixel = canvas.getCoordinateTransform().transform(QgsPointXY(5.05, 5.0))
    snapped = tool._snap(QPoint(round(pixel.x()), round(pixel.y())))
    ok = ok and snapped == QgsPointXY(5.0, 5.0)
    control.segment_check.setChecked(False)
    ok = ok and tool.snap_enabled and tool.snap_vertices and not tool.snap_segments
    control.snap_check.setChecked(False)
    ok = ok and not canvas.snappingUtils().config().enabled()
    control.snap_check.setChecked(True)
    control.segment_check.setChecked(True)

    control.lat_lon_edit.setText("57.1497, -2.0943")
    control._add_lat_lon()
    control.kp_spin.setValue(101.0)
    control._add_route_kp()
    tool.finish()
    ok = ok and len(completed) == 1 and len(completed[0]) == 2
    ok = ok and abs(completed[0][0].x() + 2.0943) < 1e-8
    ok = ok and abs(completed[0][0].y() - 57.1497) < 1e-8
    ok = ok and completed[0][1] == QgsPointXY(1.0, 10.0)
    ok = ok and canvas.snappingUtils().config().enabled() == original_snapping.enabled()
    ok = ok and _parse_lat_lon("1.5 -2.5") == (1.5, -2.5)
    try:
        _parse_lat_lon("91, 0")
        ok = False
    except ValueError:
        pass
    QgsProject.instance().removeMapLayer(snap_layer.id())
    return _result("sketch snap controls + exact WGS84 and route-KP vertices", ok)


class _Resolver:
    def resolve(self, _task):
        return None

    def route_length_m(self, task):
        return 1852.0 if task.get("geom_kind") == "line" else None

    def clear_cache(self):
        pass


def _task(task_id, seq, predecessor="", line=False):
    return {
        "task_id": task_id, "seq": seq, "name": task_id.upper(),
        "resource_id": "v", "duration_mode": "computed" if line else "manual",
        "duration_hours": 1.0, "predecessor_task_id": predecessor,
        "lag_hours": 0.0, "speed_knots": 2.0 if line else None,
        "direction": "forward", "geom_kind": "line" if line else "",
        "feature_id": "line" if line else "", "notes": "",
    }


def test_multiselect_delete_move_and_bidirectional_duration():
    table = TaskTableWidget(_Resolver())
    rows = [_task("a", 0), _task("b", 1, "a"), _task("c", 2, "b"), _task("d", 3, "c")]
    table.set_plan(rows, [{"resource_id": "v", "name": "V"}], datetime(2026, 1, 1))
    table._select_rows([1, 2])
    table.delete_selected()
    ok = [row["task_id"] for row in table.rows] == ["a", "d"]
    ok = ok and table.rows[1]["predecessor_task_id"] == "a"

    table.set_plan(rows, [{"resource_id": "v", "name": "V"}], datetime(2026, 1, 1))
    table._select_rows([1, 2])
    table.move_selected(1)
    ok = ok and [row["task_id"] for row in table.rows] == ["a", "d", "b", "c"]

    line = _task("line", 0, line=True)
    table.set_plan([line], [{"resource_id": "v", "name": "V"}], datetime(2026, 1, 1))
    ok = ok and table.item(0, table.COL_DISTANCE).text() == "1 nm"
    table.item(0, table.COL_DURATION).setText("1")
    ok = ok and abs(table.rows[0]["speed_knots"] - 1.0) < 1e-4
    ok = ok and abs(table.schedule.tasks[0].duration_hours - 1.0) < 1e-6
    table.item(0, table.COL_SPEED).setText("2")
    ok = ok and abs(table.schedule.tasks[0].duration_hours - 0.5) < 1e-4
    point = _task("point", 0)
    point["geom_kind"] = "point"
    point["speed_knots"] = 5.0
    table.set_plan([point], [{"resource_id": "v", "name": "V"}], datetime(2026, 1, 1))
    ok = ok and table.item(0, table.COL_DISTANCE).text() == ""
    ok = ok and table.item(0, table.COL_SPEED).text() == ""
    return _result("multi-delete/block move + duration↔speed", ok)


def test_manual_distance_units_and_duration_days():
    """A 20 km loading task at 4 km/h computes 5 h; unit switches never drift."""
    from ..planner.task_table import KNOT_M_PER_HOUR

    table = TaskTableWidget(_Resolver())
    table.set_duration_unit("h")
    loading = _task("load", 0)
    table.set_plan([loading], [{"resource_id": "v", "name": "V"}], datetime(2026, 1, 1))
    table.item(0, table.COL_DISTANCE).setText("20 km")
    ok = abs(_float(table.rows[0].get("manual_distance_m")) - 20000.0) < 1e-6
    ok = ok and table.rows[0].get("distance_unit") == "km"
    table.item(0, table.COL_SPEED).setText("4")
    ok = ok and abs(table.schedule.tasks[0].duration_hours - 5.0) < 1e-6
    ok = ok and table.item(0, table.COL_DISTANCE).text() == "20 km"
    ok = ok and table.item(0, table.COL_SPEED).text() == "4"

    # Editing duration back-computes the speed in the task's unit.
    table.item(0, table.COL_DURATION).setText("10")
    shown_speed = float(table.item(0, table.COL_SPEED).text())
    ok = ok and abs(shown_speed - 2.0) < 1e-3
    ok = ok and abs(table.schedule.tasks[0].duration_hours - 10.0) < 1e-6

    # Switching the display unit converts the view but not the stored data.
    stored_m = _float(table.rows[0].get("manual_distance_m"))
    stored_kn = _float(table.rows[0].get("speed_knots"))
    table.selectRow(0)
    table._set_distance_unit_selected("nm")
    ok = ok and abs(_float(table.rows[0].get("manual_distance_m")) - stored_m) < 1e-12
    ok = ok and abs(_float(table.rows[0].get("speed_knots")) - stored_kn) < 1e-12
    ok = ok and table.item(0, table.COL_DISTANCE).text().endswith("nm")
    ok = ok and abs(float(table.item(0, table.COL_DISTANCE).text().split()[0])
                    - 20000.0 / 1852.0) < 1e-3
    table.selectRow(0)
    table._set_distance_unit_selected("km")

    # Day display: same stored hours, shown /24; day entry converts back.
    table.set_duration_unit("d")
    ok = ok and abs(float(table.item(0, table.COL_DURATION).text())
                    - 10.0 / 24.0) < 1e-3
    table.item(0, table.COL_DURATION).setText("0.5")
    ok = ok and abs(table.schedule.tasks[0].duration_hours - 12.0) < 1e-6
    # An explicit suffix overrides the column unit.
    table.item(0, table.COL_DURATION).setText("6h")
    ok = ok and abs(table.schedule.tasks[0].duration_hours - 6.0) < 1e-6
    table.set_duration_unit("h")

    # A speed profile drives the duration: 5 km at 2 km/h + 15 km at 6 km/h.
    profile = {"segments": [
        {"distance_m": 5000.0, "speed_knots": 2000.0 / KNOT_M_PER_HOUR},
        {"distance_m": None, "speed_knots": 6000.0 / KNOT_M_PER_HOUR},
    ]}
    import json as _json
    table.update_task_fields("load", {
        "speed_profile_json": _json.dumps(profile), "duration_mode": "computed"})
    ok = ok and abs(table.schedule.tasks[0].duration_hours - 5.0) < 1e-6
    duration_item = table.item(0, table.COL_DURATION)
    speed_item = table.item(0, table.COL_SPEED)
    ok = ok and not (duration_item.flags() & ITEM_FLAG_EDITABLE)
    ok = ok and not (speed_item.flags() & ITEM_FLAG_EDITABLE)
    ok = ok and abs(float(speed_item.text()) - 4.0) < 1e-3
    return _result("manual distance + unit switching + duration days + profile", ok)


def test_waypoint_task_configuration():
    dialog = SketchTasksDialog(
        [{"resource_id": "v", "name": "Vessel", "default_speed_kn": 1.0}], 3)
    dialog.waypoint_table.item(1, 1).setText("Joint operation")
    dialog.waypoint_table.item(1, 2).setText("4.5")
    dialog.waypoint_table.cellWidget(2, 0).setChecked(False)
    config = dialog.configuration()
    ok = set(config["waypoint_tasks"]) == {0, 1}
    ok = ok and config["waypoint_tasks"][1]["name"] == "Joint operation"
    ok = ok and config["waypoint_tasks"][1]["duration_hours"] == 4.5
    dialog.close()
    return _result("per-waypoint create/name/operation duration", ok)


def test_simulation_label_content_and_canvas_follow():
    task = _task("lay", 0, line=True)
    task["name"] = "Lay section A"
    resource = {"name": "Vessel 1"}
    state = ActiveState("lay", 0.25, 463.0, True)
    scheduled = ScheduledTask(
        "lay", 1, datetime(2026, 1, 1), datetime(2026, 1, 1, 1), 1.0, "v")
    default_text = _simulation_label(
        task, resource, state, scheduled, datetime(2026, 1, 1, 0, 15),
        {"show": True, "task_name": True})
    settings = {
        "show": True, "task_name": True, "task_number": True, "resource": True,
        "progress": True, "clock": True, "speed_distance": True,
    }
    full_text = _simulation_label(
        task, resource, state, scheduled, datetime(2026, 1, 1, 0, 15), settings, 1.0)
    ok = default_text == "Lay section A"
    ok = ok and full_text == (
        "#1 Lay section A\nVessel 1\nProgress 25%\n01/01/2026 00:15\n2 kn / 1 nm")
    ok = ok and _simulation_label(
        task, resource, state, scheduled, datetime(2026, 1, 1), {"show": False}) == ""

    canvas = QgsMapCanvas()
    canvas.resize(640, 480)
    canvas.setExtent(QgsRectangle(0, 0, 10, 10))
    overlay = PlannerMapOverlay(canvas)
    overlay.show_point("v", QgsPointXY(5, 5), "#123456", default_text)
    label = overlay._items["v"][3]
    ok = ok and label.isVisible() and label.toPlainText() == default_text
    ok = ok and label.map_point == QgsPointXY(5, 5)
    overlay.clear()
    return _result("configurable simulation label follows resource marker", ok)


def test_summary_outline_undo_redo_compact_rows_and_resource_configuration():
    table = TaskTableWidget(_Resolver())
    rows = [_task("group", 0), _task("a", 1), _task("b", 2, "a")]
    resources = [{
        "resource_id": "v", "name": "Vessel", "kind": "vessel",
        "color_hex": "#ff0000", "default_speed_kn": 2.0, "start_offset_hours": 6.0,
    }]
    table.set_plan(rows, resources, datetime(2026, 1, 1))
    table._select_rows([1, 2])
    table.indent_selected(1)
    ok = not any(bool(row["is_phase"]) for row in table.rows)
    ok = ok and [row["outline_level"] for row in table.rows] == [0, 1, 1]
    ok = ok and table._is_summary(0)
    ok = ok and table.schedule.tasks[0].duration_hours == 2.0
    table._cell_double_clicked(0, table.COL_TASK)
    ok = ok and table.isRowHidden(1) and table.isRowHidden(2)
    table._cell_double_clicked(0, table.COL_TASK)
    ok = ok and not table.isRowHidden(1)
    table.undo()
    ok = ok and [row["outline_level"] for row in table.rows] == [0, 0, 0]
    table.redo()
    ok = ok and [row["outline_level"] for row in table.rows] == [0, 1, 1]
    table._select_rows([1, 2])
    table.delete_selected()
    ok = ok and [row["task_id"] for row in table.rows] == ["group"]
    table.undo()
    ok = ok and [row["task_id"] for row in table.rows] == ["group", "a", "b"]
    table.redo()
    ok = ok and [row["task_id"] for row in table.rows] == ["group"]
    # Display scaling can inflate the stored pixel size, so compare with a
    # reference widget configured to the same compact height instead of == 24.
    reference = QTableWidget()
    reference.verticalHeader().setDefaultSectionSize(24)
    ok = ok and (table.verticalHeader().defaultSectionSize()
                 == reference.verticalHeader().defaultSectionSize())

    dialog = ResourceDialog(resources)
    saved = dialog.resources()[0]
    colour_button = dialog.table.cellWidget(0, 2)
    ok = ok and saved["color_hex"] == "#ff0000"
    ok = ok and saved["start_offset_hours"] == 6.0
    ok = ok and colour_button.property("color_hex") == "#ff0000"
    dialog.close()
    return _result("indent summary/collapse + undo/redo + compact/resource settings", ok)


def test_undo_redo_restores_owned_geometry():
    folder = tempfile.mkdtemp(prefix="planner_history_test_")
    store = PlannerStore(os.path.join(folder, "planner.gpkg"))
    store.ensure_created()
    scenario_id = store.create_scenario("History", "2026-01-01T00:00")
    resource = store.list_resources()[0]
    task = _task("route", 0, line=True)
    task["resource_id"] = resource["resource_id"]
    reference = store.set_task_geometry(
        task["task_id"], scenario_id, 0, task["name"],
        QgsGeometry.fromWkt("LINESTRING(0 0, 1 0)"), "line",
        source_crs=QgsCoordinateReferenceSystem("EPSG:4326"),
        resource_id=resource["resource_id"], source_kind="test_history")
    task.update(reference)
    store.save_tasks(scenario_id, [task])

    table = TaskTableWidget(_Resolver())
    table.set_plan(store.list_tasks(scenario_id), [resource], datetime(2026, 1, 1))

    class _HistoryHost:
        pass

    host = _HistoryHost()
    host.store = store
    host.task_table = table
    host.current_scenario_id = scenario_id
    host.resolver = table.resolver
    host._planner_history_snapshot = MethodType(PlannerDock._planner_history_snapshot, host)
    host._restore_planner_history_snapshot = MethodType(
        PlannerDock._restore_planner_history_snapshot, host)
    table.set_history_hooks(
        host._planner_history_snapshot, host._restore_planner_history_snapshot)
    table.tasksChanged.connect(lambda rows: store.save_tasks(scenario_id, rows))

    table.selectRow(0)
    table.delete_selected()
    ok = not table.rows and store.get_task_geometry("route") is None
    table.undo()
    restored = store.get_task_geometry("route")
    ok = ok and len(table.rows) == 1 and restored is not None
    ok = ok and restored[1].geometry().equals(
        QgsGeometry.fromWkt("LINESTRING(0 0, 1 0)"))
    table.redo()
    ok = ok and not table.rows and store.get_task_geometry("route") is None
    return _result("undo/redo restores and removes Planner-owned geometry", ok)


def test_zoom_signal_and_active_row_highlight():
    table = TaskTableWidget(_Resolver())
    rows = [_task("a", 0), _task("b", 1, "a")]
    table.set_plan(rows, [{"resource_id": "v", "name": "V"}], datetime(2026, 1, 1))
    fired = []
    table.zoomRequested.connect(fired.append)
    table._cell_double_clicked(1, table.COL_NUMBER)
    ok = fired == ["b"]
    no_brush = Qt.BrushStyle.NoBrush if hasattr(Qt, "BrushStyle") else Qt.NoBrush
    table.set_active_tasks({"a"})
    ok = ok and table.item(0, table.COL_TASK).background().color() == QColor(255, 214, 79, 90)
    ok = ok and table.item(1, table.COL_TASK).background().style() == no_brush
    table.set_active_tasks(set())
    ok = ok and table.item(0, table.COL_TASK).background().style() == no_brush
    return _result("zoom-to-task signal + active-row highlight", ok)


def run_all():
    return [
        test_connected_merge_and_reverse(), test_sketch_session_completion_and_restore(),
        test_sketch_does_not_reopen_transit_measure(),
        test_sketch_snapping_exact_coordinate_and_route_kp(),
        test_multiselect_delete_move_and_bidirectional_duration(),
        test_manual_distance_units_and_duration_days(),
        test_waypoint_task_configuration(),
        test_simulation_label_content_and_canvas_follow(),
        test_summary_outline_undo_redo_compact_rows_and_resource_configuration(),
        test_undo_redo_restores_owned_geometry(),
        test_zoom_signal_and_active_row_highlight(),
    ]
