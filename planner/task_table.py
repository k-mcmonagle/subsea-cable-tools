# -*- coding: utf-8 -*-
"""Editable outlined task table for the spatial Planner."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QComboBox, QPushButton, QTableWidget, QTableWidgetItem, QTableWidgetSelectionRange,
)

from ..qgis_compat import (
    DRAG_DROP_MODE_INTERNAL_MOVE, DROP_ACTION_MOVE, ITEM_DATA_USER_ROLE,
    ITEM_FLAG_EDITABLE, SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_EXTENDED,
)
from . import schema
from .timeline_engine import TaskSpec, compute_schedule


class TaskTableWidget(QTableWidget):
    tasksChanged = pyqtSignal(object)
    scheduleChanged = pyqtSignal(object)
    linkRequested = pyqtSignal(str)
    taskSelected = pyqtSignal(str)
    historyStateChanged = pyqtSignal(bool, bool)

    COL_NUMBER = 0
    COL_TASK = 1
    COL_DESCRIPTION = 2
    COL_RESOURCE = 3
    COL_DURATION = 4
    COL_PREDECESSOR = 5
    COL_FEATURE = 6
    COL_DISTANCE = 7
    COL_SPEED = 8
    COL_DIRECTION = 9
    COL_START = 10
    COL_FINISH = 11
    COL_NOTES = 12

    HEADERS = ["#", "Task", "Description", "Resource", "Duration (h)", "Predecessor",
               "Linked feature", "Distance (nm)", "Speed (kn)", "Dir", "Start", "Finish",
               "Notes"]

    def __init__(self, resolver, parent=None):
        super().__init__(0, len(self.HEADERS), parent)
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.horizontalHeaderItem(self.COL_DURATION).setToolTip(
            "Line tasks: enter speed to calculate duration, or edit duration to calculate speed.")
        self.horizontalHeaderItem(self.COL_SPEED).setToolTip(
            "Knots for route tasks; blank for point operations. Accurate route length is measured "
            "using the project ellipsoid/CRS settings.")
        self.horizontalHeaderItem(self.COL_DISTANCE).setToolTip(
            "Read-only measured route distance in nautical miles (1 nm = 1,852 m).")
        self.resolver = resolver
        self.rows = []
        self.resources = []
        self.collapsed_groups = set()
        self.anchor = datetime.now().replace(second=0, microsecond=0)
        self.schedule = compute_schedule(self.anchor, [])
        self._muted = False
        self._undo_stack = []
        self._redo_stack = []
        self._history_limit = 100
        self._snapshot_provider = None
        self._snapshot_restorer = None
        self.itemChanged.connect(self._on_item_changed)
        self.itemSelectionChanged.connect(self._on_selection_changed)
        self.cellDoubleClicked.connect(self._cell_double_clicked)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(24)
        self.verticalHeader().setMinimumSectionSize(22)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropOverwriteMode(False)
        self.setDragDropMode(DRAG_DROP_MODE_INTERNAL_MOVE)
        self.setDefaultDropAction(DROP_ACTION_MOVE)
        self.setColumnWidth(self.COL_TASK, 160)
        self.setColumnWidth(self.COL_DESCRIPTION, 200)
        self.setColumnWidth(self.COL_FEATURE, 180)

    def set_plan(self, rows, resources, anchor):
        self.rows = [dict(row) for row in rows]
        for row in self.rows:
            # v3 stored explicit phases.  Keep the field for file compatibility,
            # but grouping is now derived entirely from indentation.
            row["is_phase"] = 0
            row.setdefault("outline_level", 0)
        self._normalise_outline()
        self.resources = [dict(row) for row in resources]
        self.anchor = anchor
        self.collapsed_groups.clear()
        self.clear_history()
        self._rebuild()

    def set_history_hooks(self, snapshot_provider=None, snapshot_restorer=None):
        """Add optional state hooks so the dock can include owned geometries."""
        self._snapshot_provider = snapshot_provider
        self._snapshot_restorer = snapshot_restorer

    def clear_history(self):
        self._undo_stack = []
        self._redo_stack = []
        self.historyStateChanged.emit(False, False)

    def checkpoint(self):
        self._undo_stack.append(self._capture_snapshot())
        if len(self._undo_stack) > self._history_limit:
            self._undo_stack.pop(0)
        self._redo_stack = []
        self._emit_history_state()

    def undo(self):
        if not self._undo_stack:
            return
        current = self._capture_snapshot()
        target = self._undo_stack.pop()
        self._redo_stack.append(current)
        self._restore_snapshot(target)

    def redo(self):
        if not self._redo_stack:
            return
        current = self._capture_snapshot()
        target = self._redo_stack.pop()
        self._undo_stack.append(current)
        self._restore_snapshot(target)

    def _capture_snapshot(self):
        if self._snapshot_provider is not None:
            return self._snapshot_provider()
        return deepcopy(self.rows)

    def _restore_snapshot(self, snapshot):
        rows = (self._snapshot_restorer(snapshot) if self._snapshot_restorer is not None
                else deepcopy(snapshot))
        self.rows = [dict(row) for row in rows]
        for row in self.rows:
            row["is_phase"] = 0
            row.setdefault("outline_level", 0)
        self._normalise_outline()
        valid_ids = {row.get("task_id") for row in self.rows}
        self.collapsed_groups.intersection_update(valid_ids)
        self._renumber()
        self._rebuild()
        self._emit_change()
        self._emit_history_state()

    def _emit_history_state(self):
        self.historyStateChanged.emit(bool(self._undo_stack), bool(self._redo_stack))

    def set_anchor(self, anchor):
        self.anchor = anchor
        self._recompute()

    def add_task(self):
        self.checkpoint()
        resource_id = self.resources[0].get("resource_id", "") if self.resources else ""
        default_speed = self.resources[0].get("default_speed_kn") if self.resources else None
        now = schema.utc_now_iso()
        self.rows.append({
            "task_id": schema.new_id(), "seq": len(self.rows), "name": "New task",
            "description": "", "is_phase": 0, "outline_level": 0,
            "resource_id": resource_id, "duration_mode": "manual",
            "duration_hours": 1.0, "predecessor_task_id": "", "lag_hours": 0.0,
            "speed_knots": default_speed, "direction": "forward", "layer_id": "",
            "layer_source": "", "layer_name": "", "feature_id": "",
            "feature_label": "", "geom_kind": "", "linked_ref_json": "",
            "created_utc": now, "modified_utc": now, "notes": "",
        })
        self._rebuild()
        self.selectRow(len(self.rows) - 1)
        self._emit_change()

    def indent_selected(self, delta):
        indices = self.selected_row_indices()
        if not indices:
            return
        indices = self._include_summary_descendants(indices)
        minimum = min(int(self.rows[index].get("outline_level") or 0) for index in indices)
        if delta > 0:
            if indices[0] == 0:
                return
            previous_level = int(self.rows[indices[0] - 1].get("outline_level") or 0)
            target = min(minimum + 1, previous_level + 1)
            shift = target - minimum
            if shift <= 0:
                return
        else:
            if minimum <= 0:
                return
            shift = -1
        self.checkpoint()
        for index in indices:
            self.rows[index]["outline_level"] = max(
                0, int(self.rows[index].get("outline_level") or 0) + shift)
        self._rebuild()
        self._select_rows(indices)
        self._emit_change()

    def append_tasks(self, rows):
        if not rows:
            return
        self.checkpoint()
        for row in rows:
            copied = dict(row)
            copied["is_phase"] = 0
            copied.setdefault("outline_level", 0)
            self.rows.append(copied)
        self._renumber()
        self._rebuild()
        self.selectRow(len(self.rows) - 1)
        self._emit_change()

    def replace_tasks(self, rows, selected_row=0, record_history=True):
        if record_history:
            self.checkpoint()
        self.rows = [dict(row) for row in rows]
        for row in self.rows:
            row["is_phase"] = 0
            row.setdefault("outline_level", 0)
        self._normalise_outline()
        self._renumber()
        self._rebuild()
        if self.rows:
            self.selectRow(min(max(0, selected_row), len(self.rows) - 1))
        self._emit_change()

    def selected_row_indices(self):
        return sorted({index.row() for index in self.selectionModel().selectedRows()})

    def _select_rows(self, rows):
        self.clearSelection()
        for row in rows:
            if 0 <= row < self.rowCount():
                self.setRangeSelected(
                    QTableWidgetSelectionRange(row, 0, row, self.columnCount() - 1), True)

    def delete_selected(self):
        indices = self.selected_row_indices()
        if not indices and 0 <= self.currentRow() < len(self.rows):
            indices = [self.currentRow()]
        if not indices:
            return
        indices = self._include_summary_descendants(indices)
        self.checkpoint()
        original = {row.get("task_id"): row for row in self.rows}
        deleted_ids = {self.rows[index].get("task_id") for index in indices}
        selected_row = min(indices)
        self.rows = [row for row in self.rows if row.get("task_id") not in deleted_ids]

        def surviving_predecessor(task_id):
            seen = set()
            while task_id in deleted_ids and task_id not in seen:
                seen.add(task_id)
                task_id = (original.get(task_id) or {}).get("predecessor_task_id") or ""
            return task_id

        for task in self.rows:
            task["predecessor_task_id"] = surviving_predecessor(
                task.get("predecessor_task_id") or "")
        self._normalise_outline()
        self._renumber()
        self._rebuild()
        if self.rows:
            self.selectRow(min(selected_row, len(self.rows) - 1))
        self._emit_change()

    def move_selected(self, delta):
        indices = self.selected_row_indices()
        if not indices and 0 <= self.currentRow() < len(self.rows):
            indices = [self.currentRow()]
        if not indices:
            return
        indices = self._include_summary_descendants(indices)
        moving = [self.rows[index] for index in indices]
        moving_ids = {row.get("task_id") for row in moving}
        remaining = [row for row in self.rows if row.get("task_id") not in moving_ids]
        if delta < 0:
            if indices[0] == 0:
                return
            insert_at = indices[0] - 1
        else:
            if indices[-1] == len(self.rows) - 1:
                return
            insert_at = indices[0] + 1
        self.checkpoint()
        self.rows = remaining[:insert_at] + moving + remaining[insert_at:]
        self._normalise_outline()
        self._renumber()
        self._rebuild()
        self._select_rows(range(insert_at, insert_at + len(moving)))
        self._emit_change()

    def dropEvent(self, event):
        if event.source() is not self:
            super().dropEvent(event)
            return
        indices = self.selected_row_indices()
        if not indices:
            event.ignore()
            return
        indices = self._include_summary_descendants(indices)
        position = event.position().toPoint() if hasattr(event, "position") else event.pos()
        target_index = self.indexAt(position)
        target = target_index.row()
        if target < 0:
            target = len(self.rows)
        elif position.y() > self.visualRect(target_index).center().y():
            target += 1
        moving = [self.rows[index] for index in indices]
        moving_ids = {row.get("task_id") for row in moving}
        remaining = [row for row in self.rows if row.get("task_id") not in moving_ids]
        insert_at = target - sum(1 for index in indices if index < target)
        insert_at = min(max(0, insert_at), len(remaining))
        if insert_at == indices[0]:
            event.ignore()
            return
        self.checkpoint()
        self.rows = remaining[:insert_at] + moving + remaining[insert_at:]
        self._normalise_outline()
        self._renumber()
        self._rebuild()
        self._select_rows(range(insert_at, insert_at + len(moving)))
        self._emit_change()
        event.acceptProposedAction()

    def _include_summary_descendants(self, indices):
        expanded = set(indices)
        for index in list(indices):
            if not self._is_summary(index):
                continue
            level = int(self.rows[index].get("outline_level") or 0)
            for child in range(index + 1, len(self.rows)):
                if int(self.rows[child].get("outline_level") or 0) <= level:
                    break
                expanded.add(child)
        return sorted(expanded)

    def keyPressEvent(self, event):
        control = (Qt.KeyboardModifier.ControlModifier if hasattr(Qt, "KeyboardModifier")
                   else Qt.ControlModifier)
        shift = (Qt.KeyboardModifier.ShiftModifier if hasattr(Qt, "KeyboardModifier")
                 else Qt.ShiftModifier)
        key_z = Qt.Key.Key_Z if hasattr(Qt, "Key") else Qt.Key_Z
        key_y = Qt.Key.Key_Y if hasattr(Qt, "Key") else Qt.Key_Y
        key_delete = Qt.Key.Key_Delete if hasattr(Qt, "Key") else Qt.Key_Delete
        if event.modifiers() & control and event.key() == key_z:
            self.redo() if event.modifiers() & shift else self.undo()
            event.accept()
            return
        if event.modifiers() & control and event.key() == key_y:
            self.redo()
            event.accept()
            return
        if event.key() == key_delete:
            self.delete_selected()
            event.accept()
            return
        super().keyPressEvent(event)

    def update_link(self, task_id, reference, record_history=True):
        task = self.row_by_id(task_id)
        if task is None:
            return
        if record_history:
            self.checkpoint()
        for key in ("layer_id", "layer_source", "layer_name", "feature_id",
                    "feature_label", "geom_kind", "linked_ref_json"):
            task[key] = reference.get(key, "")
        if task.get("geom_kind") == "line" and _float(task.get("speed_knots")) > 0:
            task["duration_mode"] = "computed"
        self.resolver.clear_cache()
        self._rebuild()
        row_index = next((index for index, row in enumerate(self.rows)
                          if row.get("task_id") == task_id), -1)
        if row_index >= 0:
            self.selectRow(row_index)
        self._emit_change()

    def row_by_id(self, task_id):
        return next((row for row in self.rows if row.get("task_id") == task_id), None)

    def task_specs(self):
        specs = []
        for index, row in enumerate(self.rows):
            summary = self._is_summary(index)
            route_length = (self.resolver.route_length_m(row)
                            if not summary and row.get("geom_kind") == "line" else None)
            specs.append(TaskSpec(
                task_id=row.get("task_id") or schema.new_id(), seq=index,
                name=row.get("name") or "", resource_id=row.get("resource_id") or "",
                duration_mode=row.get("duration_mode") or "manual",
                duration_hours=_float(row.get("duration_hours")),
                predecessor_task_id=row.get("predecessor_task_id") or "",
                lag_hours=_float(row.get("lag_hours")), speed_knots=_float(row.get("speed_knots")),
                direction=row.get("direction") or "forward",
                geom_kind="" if summary else row.get("geom_kind") or "",
                route_length_m=route_length, is_phase=summary,
                outline_level=int(row.get("outline_level") or 0),
            ))
        return specs

    def _renumber(self):
        seen = set()
        for index, row in enumerate(self.rows):
            row["seq"] = index
            row["is_phase"] = 0
            if self._is_summary(index):
                row["predecessor_task_id"] = ""
                continue
            if row.get("predecessor_task_id") not in seen:
                row["predecessor_task_id"] = ""
            seen.add(row.get("task_id"))

    def _rebuild(self):
        self._muted = True
        try:
            self.clearContents()
            self.setRowCount(len(self.rows))
            for row_index, task in enumerate(self.rows):
                task["seq"] = row_index
                summary = self._is_summary(row_index)
                number = QTableWidgetItem(str(row_index + 1))
                number.setFlags(number.flags() & ~ITEM_FLAG_EDITABLE)
                number.setData(ITEM_DATA_USER_ROLE, task.get("task_id"))
                self.setItem(row_index, self.COL_NUMBER, number)
                self._set_text(row_index, self.COL_TASK, task.get("name"))
                task_item = self.item(row_index, self.COL_TASK)
                task_item.setText(self._outline_text(row_index))
                font = task_item.font()
                font.setBold(summary)
                task_item.setFont(font)
                self._set_text(row_index, self.COL_DESCRIPTION, task.get("description"))
                self._resource_combo(row_index, task, summary)
                if summary:
                    self._readonly_item(row_index, self.COL_DURATION, "")
                else:
                    self._set_text(
                        row_index, self.COL_DURATION, _display_number(task.get("duration_hours")))
                self._predecessor_combo(row_index, task, summary)
                self._feature_button(row_index, task, summary)
                self._readonly_item(row_index, self.COL_DISTANCE, "")
                if summary or task.get("geom_kind") == "point":
                    self._readonly_item(row_index, self.COL_SPEED, "")
                else:
                    self._set_text(
                        row_index, self.COL_SPEED, _display_number(task.get("speed_knots")))
                self._direction_combo(row_index, task, summary)
                self._readonly_item(row_index, self.COL_START, "")
                self._readonly_item(row_index, self.COL_FINISH, "")
                self._set_text(row_index, self.COL_NOTES, task.get("notes"))
        finally:
            self._muted = False
        self._recompute()
        self._apply_collapsed_rows()

    def _is_summary(self, row):
        if not (0 <= row < len(self.rows) - 1):
            return False
        level = int(self.rows[row].get("outline_level") or 0)
        next_level = int(self.rows[row + 1].get("outline_level") or 0)
        return next_level > level

    def _normalise_outline(self):
        previous = 0
        for index, task in enumerate(self.rows):
            level = max(0, int(task.get("outline_level") or 0))
            if index == 0:
                level = 0
            else:
                level = min(level, previous + 1)
            task["outline_level"] = level
            previous = level

    def _outline_text(self, row):
        task = self.rows[row]
        level = max(0, int(task.get("outline_level") or 0))
        prefix = "    " * level
        if self._is_summary(row):
            marker = "▸ " if task.get("task_id") in self.collapsed_groups else "▾ "
        else:
            marker = ""
        return prefix + marker + (task.get("name") or "")

    def _cell_double_clicked(self, row, column):
        if column != self.COL_TASK or not (0 <= row < len(self.rows)):
            return
        task = self.rows[row]
        if not self._is_summary(row):
            return
        task_id = task.get("task_id")
        if task_id in self.collapsed_groups:
            self.collapsed_groups.remove(task_id)
        else:
            self.collapsed_groups.add(task_id)
        self._rebuild()
        self.selectRow(row)

    def _apply_collapsed_rows(self):
        for row in range(len(self.rows)):
            self.setRowHidden(row, False)
        for index, summary in enumerate(self.rows):
            if not self._is_summary(index) or summary.get("task_id") not in self.collapsed_groups:
                continue
            level = int(summary.get("outline_level") or 0)
            for child in range(index + 1, len(self.rows)):
                if int(self.rows[child].get("outline_level") or 0) <= level:
                    break
                self.setRowHidden(child, True)

    def _set_text(self, row, column, value):
        self.setItem(row, column, QTableWidgetItem(str(value or "")))

    def _readonly_item(self, row, column, value):
        item = QTableWidgetItem(str(value or ""))
        item.setFlags(item.flags() & ~ITEM_FLAG_EDITABLE)
        self.setItem(row, column, item)

    def _resource_combo(self, row, task, summary=False):
        combo = QComboBox()
        combo.addItem("(unassigned)", "")
        for resource in self.resources:
            combo.addItem(resource.get("name") or "Resource", resource.get("resource_id") or "")
        index = combo.findData(task.get("resource_id") or "")
        combo.setCurrentIndex(max(0, index))
        combo.setEnabled(not summary)
        task_id = task.get("task_id")
        combo.currentIndexChanged.connect(
            lambda _index, c=combo, tid=task_id: self._combo_changed(tid, "resource_id", c.currentData()))
        self.setCellWidget(row, self.COL_RESOURCE, combo)

    def _predecessor_combo(self, row, task, summary=False):
        combo = QComboBox()
        combo.addItem("(anchor)", "")
        for prior_index, prior in enumerate(self.rows[:row]):
            if self._is_summary(prior_index):
                continue
            combo.addItem("%s — %s" % (int(prior.get("seq") or 0) + 1, prior.get("name") or "Task"),
                          prior.get("task_id"))
        index = combo.findData(task.get("predecessor_task_id") or "")
        combo.setCurrentIndex(max(0, index))
        combo.setEnabled(not summary)
        task_id = task.get("task_id")
        combo.currentIndexChanged.connect(
            lambda _index, c=combo, tid=task_id: self._combo_changed(
                tid, "predecessor_task_id", c.currentData()))
        self.setCellWidget(row, self.COL_PREDECESSOR, combo)

    def _feature_button(self, row, task, summary=False):
        if summary:
            button = QPushButton("Group summary")
            button.setEnabled(False)
            self.setCellWidget(row, self.COL_FEATURE, button)
            return
        label = task.get("feature_label") or task.get("feature_id") or "Link…"
        layer = task.get("layer_name") or ""
        button = QPushButton((layer + " / " if layer else "") + label)
        button.clicked.connect(lambda _checked=False, tid=task.get("task_id"): self.linkRequested.emit(tid))
        if task.get("feature_id") and self.resolver.resolve(task) is None:
            button.setStyleSheet("QPushButton { background: #d99b32; }")
            button.setToolTip("Linked feature is unavailable: %s / %s" % (layer, label))
        self.setCellWidget(row, self.COL_FEATURE, button)

    def _direction_combo(self, row, task, summary=False):
        combo = QComboBox()
        combo.addItem("Forward", "forward")
        combo.addItem("Reverse", "reverse")
        combo.setCurrentIndex(max(0, combo.findData(task.get("direction") or "forward")))
        combo.setEnabled(not summary and task.get("geom_kind") == "line")
        task_id = task.get("task_id")
        combo.currentIndexChanged.connect(
            lambda _index, c=combo, tid=task_id: self._combo_changed(tid, "direction", c.currentData()))
        self.setCellWidget(row, self.COL_DIRECTION, combo)

    def _combo_changed(self, task_id, field, value):
        if self._muted:
            return
        task = self.row_by_id(task_id)
        if task is None:
            return
        self.checkpoint()
        task[field] = value or ""
        self._recompute()
        self._emit_change()

    def _on_item_changed(self, item):
        if self._muted or item.row() >= len(self.rows):
            return
        task = self.rows[item.row()]
        mapping = {
            self.COL_TASK: "name", self.COL_DESCRIPTION: "description",
            self.COL_DURATION: "duration_hours", self.COL_SPEED: "speed_knots",
            self.COL_NOTES: "notes",
        }
        field = mapping.get(item.column())
        if field is None:
            return
        self.checkpoint()
        if field in ("duration_hours", "speed_knots"):
            task[field] = _float(item.text(), None)
        elif field == "name":
            text = item.text().lstrip()
            if text.startswith(("▸ ", "▾ ")):
                text = text[2:]
            task[field] = text
            self._muted = True
            try:
                item.setText(self._outline_text(item.row()))
            finally:
                self._muted = False
        else:
            task[field] = item.text()
        if field == "duration_hours":
            duration = _float(task.get("duration_hours"))
            if task.get("geom_kind") == "line" and duration > 0:
                length_m = self.resolver.route_length_m(task)
                if length_m is not None and length_m > 0:
                    task["speed_knots"] = length_m / (duration * 3600.0 * 0.514444)
                    task["duration_mode"] = "computed"
                    self._muted = True
                    try:
                        speed_item = self.item(item.row(), self.COL_SPEED)
                        if speed_item is not None:
                            speed_item.setText(_display_number(task["speed_knots"]))
                    finally:
                        self._muted = False
            else:
                task["duration_mode"] = "manual"
        elif field == "speed_knots":
            task["duration_mode"] = (
                "computed" if task.get("geom_kind") == "line" and _float(task[field]) > 0 else "manual")
        self._recompute()
        self._emit_change()

    def _on_selection_changed(self):
        row = self.currentRow()
        if 0 <= row < len(self.rows):
            self.taskSelected.emit(self.rows[row].get("task_id") or "")

    def _recompute(self):
        specs = self.task_specs()
        resource_offsets = {
            row.get("resource_id") or "": _float(row.get("start_offset_hours"))
            for row in self.resources
        }
        self.schedule = compute_schedule(self.anchor, specs, resource_offsets)
        by_id = {task.task_id: task for task in self.schedule.tasks}
        self._muted = True
        try:
            for row_index, row in enumerate(self.rows):
                scheduled = by_id.get(row.get("task_id"))
                duration_item = self.item(row_index, self.COL_DURATION)
                distance_item = self.item(row_index, self.COL_DISTANCE)
                duration_item.setToolTip("")
                spec = specs[row_index]
                if spec.geom_kind == "line" and spec.route_length_m is not None:
                    distance_item.setText(_display_number(spec.route_length_m / 1852.0))
                    distance_item.setToolTip("Measured distance: %s m" % _display_number(
                        spec.route_length_m))
                else:
                    distance_item.setText("")
                    distance_item.setToolTip("")
                if self._is_summary(row_index):
                    duration_item.setText(_display_number(
                        scheduled.duration_hours if scheduled is not None else 0.0))
                    duration_item.setFlags(duration_item.flags() & ~ITEM_FLAG_EDITABLE)
                    duration_item.setForeground(QBrush(QColor("#303030")))
                    font = duration_item.font()
                    font.setItalic(False)
                    font.setBold(True)
                    duration_item.setFont(font)
                    duration_item.setToolTip("Summary span derived from this row's indented tasks.")
                elif row.get("duration_mode") == "computed" and spec.route_length_m is not None and spec.speed_knots > 0:
                    duration_item.setText(_display_number(scheduled.duration_hours))
                    duration_item.setFlags(duration_item.flags() | ITEM_FLAG_EDITABLE)
                    duration_item.setForeground(QBrush(QColor("#777777")))
                    font = duration_item.font()
                    font.setItalic(True)
                    duration_item.setFont(font)
                    duration_item.setToolTip(
                        "Calculated from measured route length and speed. Edit this value to recalculate speed.")
                else:
                    duration_item.setText(_display_number(row.get("duration_hours")))
                    duration_item.setFlags(duration_item.flags() | ITEM_FLAG_EDITABLE)
                    duration_item.setForeground(QBrush())
                    font = duration_item.font()
                    font.setItalic(False)
                    duration_item.setFont(font)
                if scheduled is not None:
                    self.item(row_index, self.COL_START).setText(scheduled.start.strftime("%d/%m/%Y %H:%M"))
                    self.item(row_index, self.COL_FINISH).setText(scheduled.finish.strftime("%d/%m/%Y %H:%M"))
                    if scheduled.warning:
                        duration_item.setToolTip(scheduled.warning)
        finally:
            self._muted = False
        self.scheduleChanged.emit(self.schedule)

    def _emit_change(self):
        self.tasksChanged.emit([dict(row) for row in self.rows])


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _display_number(value):
    if value in (None, ""):
        return ""
    try:
        return ("%.4f" % float(value)).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""
