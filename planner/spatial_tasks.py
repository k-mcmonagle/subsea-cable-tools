# -*- coding: utf-8 -*-
"""Spatial task editing operations shared by planner UI actions."""

from __future__ import annotations

from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox
from qgis.core import QgsGeometry


def merge_selected_tasks(dock):
    indices = dock.task_table.selected_row_indices()
    if len(indices) < 2:
        QMessageBox.information(dock, "Merge tasks", "Select at least two adjacent line-task rows.")
        return
    if indices != list(range(indices[0], indices[-1] + 1)):
        QMessageBox.warning(dock, "Merge tasks", "Only one contiguous block of tasks can be merged.")
        return
    selected = [dock.task_table.rows[index] for index in indices]
    resources = {task.get("resource_id") or "" for task in selected}
    if len(resources) != 1:
        QMessageBox.warning(dock, "Merge tasks", "Selected tasks must use the same resource.")
        return
    geometries = []
    for task in selected:
        frame = dock.resolver.route_frame(task)
        if frame is None:
            QMessageBox.warning(
                dock, "Merge tasks", "Every selected task must have resolvable line geometry.")
            return
        task_geometries = frame.geometries
        if task.get("direction") == "reverse":
            task_geometries = [_reversed_geometry(geometry) for geometry in reversed(task_geometries)]
        geometries.extend(task_geometries)
    tolerance = max(dock.canvas.mapUnitsPerPixel() * 5.0, 1e-9)
    merged_geometry = _join_connected(geometries, tolerance)
    if merged_geometry is None:
        QMessageBox.warning(
            dock, "Merge tasks",
            "The selected task routes do not meet at their endpoints. Move/snap them together first.")
        return
    default_name = "%s + %d more" % (selected[0].get("name") or "Merged task", len(selected) - 1)
    name, ok = QInputDialog.getText(dock, "Merge tasks", "Merged task name:", text=default_name)
    if not ok or not name.strip():
        return

    scheduled = {task.task_id: task for task in dock.task_table.schedule.tasks}
    total_duration = sum(scheduled[task["task_id"]].duration_hours
                         for task in selected if task.get("task_id") in scheduled)
    speeds = {float(task.get("speed_knots") or 0.0) for task in selected}
    all_computed = all(task.get("duration_mode") == "computed" for task in selected)
    first = dict(selected[0])
    first["name"] = name.strip()
    first["direction"] = "forward"
    if all_computed and len(speeds) == 1 and next(iter(speeds)) > 0:
        first["duration_mode"] = "computed"
        first["speed_knots"] = next(iter(speeds))
    else:
        first["duration_mode"] = "manual"
        first["duration_hours"] = total_duration
        first["speed_knots"] = None
    selected_ids = [task["task_id"] for task in selected]
    dock.task_table.checkpoint()
    dock.store.delete_task_geometries(selected_ids)
    reference = dock.store.set_task_geometry(
        first["task_id"], dock.current_scenario_id, indices[0], first["name"],
        merged_geometry, "line", source_crs=dock.canvas.mapSettings().destinationCrs(),
        resource_id=first.get("resource_id") or "", speed_knots=first.get("speed_knots"),
        duration_hours=first.get("duration_hours"), notes=first.get("notes") or "",
        source_kind="merged_tasks", source_ref={
            "merged_task_ids": selected_ids,
            "source_links": [task.get("linked_ref_json") or "" for task in selected],
        })
    first.update(reference)
    removed = set(selected_ids[1:])
    rows = []
    for index, task in enumerate(dock.task_table.rows):
        if index == indices[0]:
            rows.append(first)
        elif task.get("task_id") not in removed:
            copied = dict(task)
            if copied.get("predecessor_task_id") in removed:
                copied["predecessor_task_id"] = first["task_id"]
            rows.append(copied)
    dock.resolver.clear_cache()
    dock.task_table.replace_tasks(rows, indices[0], record_history=False)


def _reversed_geometry(geometry):
    parts = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
    reversed_parts = [list(reversed(part)) for part in reversed(parts) if part]
    if not reversed_parts:
        return QgsGeometry()
    if len(reversed_parts) == 1:
        return QgsGeometry.fromPolylineXY(reversed_parts[0])
    return QgsGeometry.fromMultiPolylineXY(reversed_parts)


def _join_connected(geometries, tolerance):
    points = []
    for geometry in geometries:
        parts = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
        for part in parts:
            part = list(part)
            if not part:
                continue
            if points:
                direct = _distance(points[-1], part[0])
                reverse = _distance(points[-1], part[-1])
                if reverse < direct:
                    part.reverse()
                    direct = reverse
                if direct > tolerance:
                    return None
                part = part[1:]
            points.extend(part)
    return QgsGeometry.fromPolylineXY(points) if len(points) >= 2 else None


def _distance(a, b):
    return ((a.x() - b.x()) ** 2 + (a.y() - b.y()) ** 2) ** 0.5
