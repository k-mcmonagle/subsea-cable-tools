# -*- coding: utf-8 -*-
"""Editable outlined task table for the spatial Planner."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import re

from qgis.PyQt.QtCore import QSettings, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QComboBox, QMenu, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QTableWidgetSelectionRange,
)

from ..qgis_compat import (
    CONTEXT_MENU_POLICY_CUSTOM,
    DRAG_DROP_MODE_INTERNAL_MOVE, DROP_ACTION_IGNORE, DROP_ACTION_MOVE,
    ITEM_DATA_USER_ROLE,
    ITEM_FLAG_EDITABLE, SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_EXTENDED,
    qt_exec,
)
from . import operation_types, schema
from .feature_ref import shared_owner_task_id
from .timeline_engine import (
    KNOT_M_PER_HOUR, TaskSpec, compute_cable, compute_fuel, compute_schedule,
    parse_speed_profile, profile_duration_hours, resolve_speed_profile,
)

LINK_KEYS = ("layer_id", "layer_source", "layer_name", "feature_id",
             "feature_label", "geom_kind", "linked_ref_json")

# Per-task distance display units; every stored value stays canonical
# (metres / knots / hours), so switching units never changes the data.
DISTANCE_FACTORS = {"nm": 1852.0, "km": 1000.0, "m": 1.0}
DISTANCE_UNIT_LABELS = (("nm", "Nautical miles (nm)"), ("km", "Kilometres (km)"),
                        ("m", "Metres (m)"))
DURATION_UNIT_SETTING = "subsea_cable_tools/planner/duration_display_unit"

_VALUE_UNIT_RE = re.compile(
    r"^\s*([-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)\s*([a-zA-Z]*)\s*$")

_DURATION_SUFFIXES = {"d": "d", "day": "d", "days": "d",
                      "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h"}


def distance_unit_for(task):
    """The task's distance display unit; legacy rows default to nm."""
    unit = str((task or {}).get("distance_unit") or "").lower()
    return unit if unit in DISTANCE_FACTORS else "nm"


def _parse_value_unit(text, suffixes):
    """(value, canonical_unit or None) from e.g. '20 km' / '1.5d'; None if bad."""
    match = _VALUE_UNIT_RE.match(str(text or ""))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    suffix = match.group(2).lower()
    if not suffix:
        return value, None
    unit = suffixes.get(suffix)
    if unit is None:
        return None
    return value, unit


class TaskTableWidget(QTableWidget):
    tasksChanged = pyqtSignal(object)
    scheduleChanged = pyqtSignal(object)
    linkRequested = pyqtSignal(str)
    speedProfileRequested = pyqtSignal(str)
    taskSelected = pyqtSignal(str)
    zoomRequested = pyqtSignal(str)
    advancedRequested = pyqtSignal(str)
    progressRequested = pyqtSignal(str)
    historyStateChanged = pyqtSignal(bool, bool)

    COL_NUMBER = 0
    COL_TASK = 1
    COL_OPERATION = 2
    COL_DESCRIPTION = 3
    COL_RESOURCE = 4
    COL_DURATION = 5
    COL_PREDECESSOR = 6
    COL_FEATURE = 7
    COL_DISTANCE = 8
    COL_SPEED = 9
    COL_DIRECTION = 10
    COL_FUEL_MODE = 11
    COL_BUNKER = 12
    COL_CABLE_MODE = 13
    COL_CABLE = 14
    COL_START = 15
    COL_FINISH = 16
    COL_FLOAT = 17
    COL_FUEL_USED = 18
    COL_ROB = 19
    COL_CABLE_ONBOARD = 20
    COL_PROGRESS = 21
    COL_STATUS = 22
    COL_NOTES = 23

    HEADERS = ["#", "Task", "Operation", "Description", "Resource", "Duration (h)", "Predecessor",
               "Linked feature", "Distance", "Speed", "Dir", "Fuel", "Bunker",
               "Cable", "Cable (km)",
               "Start", "Finish", "Float (h)", "Fuel used", "Fuel ROB",
               "Cable onboard", "Progress", "Status", "Notes"]

    def __init__(self, resolver, parent=None):
        super().__init__(0, len(self.HEADERS), parent)
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.horizontalHeaderItem(self.COL_DURATION).setToolTip(
            "Enter speed to calculate duration, or edit duration to calculate speed. "
            "Values accept an h or d suffix (e.g. 36h, 1.5d); right-click the header "
            "to display the whole column in days or hours.")
        self.horizontalHeaderItem(self.COL_SPEED).setToolTip(
            "Speed in the task's distance unit per hour: nm/h (knots), km/h, or m/h. "
            "Right-click a row to change its distance unit. Blank for tasks with no distance.")
        self.horizontalHeaderItem(self.COL_DISTANCE).setToolTip(
            "Route tasks show the measured route length (read-only). Other tasks accept a "
            "manual distance — e.g. '20 km' for a 20 km loading task — which combines with "
            "Speed to compute the duration. Right-click a row to change its unit.")
        self.horizontalHeaderItem(self.COL_FUEL_MODE).setToolTip(
            "Which of the assigned resource's fuel rates (per 24 h) this task burns: "
            "Transit, DP, Anchor, or Port. '(none)' burns no fuel. Rates are set in "
            "Resources….")
        self.horizontalHeaderItem(self.COL_BUNKER).setToolTip(
            "Fuel taken on during this task (e.g. bunkering at a port call), in the "
            "resource's fuel unit. Credited to remaining fuel at the task finish.")
        self.horizontalHeaderItem(self.COL_CABLE_MODE).setToolTip(
            "How this task moves cable relative to its resource: Load and "
            "Recover bring cable onboard, Lay and Discharge pay it off. "
            "'(none)' moves no cable.")
        self.horizontalHeaderItem(self.COL_CABLE).setToolTip(
            "Cable quantity this task moves, in km. Leave blank on Lay/Recover "
            "tasks to use the task's distance automatically; Load/Discharge "
            "need a typed amount.")
        self.horizontalHeaderItem(self.COL_CABLE_ONBOARD).setToolTip(
            "Read-only cable on board at the task finish, in km (loads and "
            "recoveries minus lays and discharges; every resource starts at 0). "
            "Red when more cable is paid off than the resource has onboard.")
        self.horizontalHeaderItem(self.COL_FUEL_USED).setToolTip(
            "Read-only fuel burned by this task, in the resource's fuel unit. "
            "Summary rows show the total for their group.")
        self.horizontalHeaderItem(self.COL_ROB).setToolTip(
            "Read-only fuel remaining on board at the task finish "
            "(start fuel − burn + bunkers). Red when the plan runs out of fuel.")
        self.horizontalHeaderItem(self.COL_FLOAT).setToolTip(
            "Calculated total float. Zero-float tasks are on the critical path.")
        self.horizontalHeaderItem(self.COL_PROGRESS).setToolTip(
            "Actual completion recorded through Update progress… in the row context menu.")
        self.resolver = resolver
        self.rows = []
        self.resources = []
        # Column-wide duration display unit ("h" or "d"); storage stays hours.
        unit = str(QSettings().value(DURATION_UNIT_SETTING, "h") or "h").lower()
        self.duration_unit = "d" if unit == "d" else "h"
        # (value, label) operation-type choices; user-configured, blank by
        # default. The dock pushes the current list via set_operation_choices.
        self.operation_choices = [operation_types.UNSPECIFIED]
        self.collapsed_groups = set()
        self._active_task_ids = set()
        self.anchor = datetime.now().replace(second=0, microsecond=0)
        self.schedule_mode = "forward"
        self.resource_start_datetimes = {}
        self.schedule = compute_schedule(self.anchor, [])
        self.fuel = compute_fuel(self.schedule, {}, [])
        self.cable = compute_cable(self.schedule, {})
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
        header = self.horizontalHeader()
        header.setSectionsMovable(True)
        header.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        header.customContextMenuRequested.connect(self._header_menu)
        # Widths, order, and hidden columns persist per user; the key embeds the
        # column count so stale layouts are ignored when columns change.
        self._header_key = ("subsea_cable_tools/planner/task_table_header_%d"
                            % len(self.HEADERS))
        self._header_muted = False
        self._user_layout = False
        self._default_header_state = header.saveState()
        saved_state = QSettings().value(self._header_key)
        if saved_state is not None:
            self._header_muted = True
            try:
                self._user_layout = bool(header.restoreState(saved_state))
            except TypeError:
                self._user_layout = False
            finally:
                self._header_muted = False
        elif not self._user_layout:
            for column in (self.COL_FLOAT, self.COL_PROGRESS, self.COL_STATUS):
                self.setColumnHidden(column, True)
        header.sectionResized.connect(self._header_changed)
        header.sectionMoved.connect(self._header_changed)
        self._apply_duration_header()

    # -- unit display helpers ---------------------------------------------
    def _apply_duration_header(self):
        item = self.horizontalHeaderItem(self.COL_DURATION)
        if item is not None:
            item.setText("Duration (d)" if self.duration_unit == "d" else "Duration (h)")

    def set_duration_unit(self, unit):
        unit = "d" if str(unit or "").lower() == "d" else "h"
        if unit == self.duration_unit:
            return
        self.duration_unit = unit
        QSettings().setValue(DURATION_UNIT_SETTING, unit)
        self._apply_duration_header()
        if self.rows:
            self._rebuild()

    def _display_duration(self, hours):
        if hours in (None, ""):
            return ""
        try:
            value = float(hours)
        except (TypeError, ValueError):
            return ""
        return _display_number(value / 24.0 if self.duration_unit == "d" else value)

    def _parse_duration_hours(self, text):
        """Hours from duration-cell text; honours h/d suffix, else column unit."""
        parsed = _parse_value_unit(text, _DURATION_SUFFIXES)
        if parsed is None:
            return None
        value, unit = parsed
        unit = unit or self.duration_unit
        return value * 24.0 if unit == "d" else value

    @staticmethod
    def _manual_distance_m(task):
        value = _float(task.get("manual_distance_m"), 0.0)
        return value if value > 0.0 else 0.0

    def _task_distance_m(self, task):
        """Distance used for duration maths: measured route or manual entry."""
        if self.spatial_kind(task) == "line":
            return self.resolver.route_length_m(task)
        manual = self._manual_distance_m(task)
        return manual if manual > 0.0 else None

    @staticmethod
    def _display_distance(metres, unit):
        factor = DISTANCE_FACTORS.get(unit, 1852.0)
        return _display_number(float(metres) / factor)

    @staticmethod
    def _display_speed(speed_knots, unit):
        if speed_knots in (None, ""):
            return ""
        factor = DISTANCE_FACTORS.get(unit, 1852.0)
        try:
            return _display_number(float(speed_knots) * KNOT_M_PER_HOUR / factor)
        except (TypeError, ValueError):
            return ""

    def set_plan(self, rows, resources, anchor, schedule_mode="forward",
                 resource_start_datetimes=None):
        self.rows = [dict(row) for row in rows]
        for row in self.rows:
            # v3 stored explicit phases.  Keep the field for file compatibility,
            # but grouping is now derived entirely from indentation.
            row["is_phase"] = 0
            row.setdefault("outline_level", 0)
        self._normalise_outline()
        self.resources = [dict(row) for row in resources]
        self.anchor = anchor
        self.schedule_mode = (
            "backward" if str(schedule_mode or "").lower() == "backward" else "forward")
        self.resource_start_datetimes = dict(resource_start_datetimes or {})
        self.collapsed_groups.clear()
        self.clear_history()
        self._rebuild()
        if not self._user_layout:
            self._auto_size_columns()

    def set_operation_choices(self, choices):
        """Replace the operation-type dropdown choices and refresh the table.

        ``choices`` is a sequence of ``(value, label)`` pairs (typically from
        ``operation_types.as_choices``). The unspecified entry is ensured to
        lead the list.
        """
        cleaned = [(str(value or ""), str(label or "")) for value, label in (choices or [])]
        if not cleaned or cleaned[0][0] != "":
            cleaned = [operation_types.UNSPECIFIED] + [c for c in cleaned if c[0] != ""]
        self.operation_choices = cleaned
        if self.rows:
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

    def set_schedule_mode(self, schedule_mode):
        self.schedule_mode = (
            "backward" if str(schedule_mode or "").lower() == "backward" else "forward")
        self._recompute()

    def set_resource_start_datetimes(self, values):
        self.resource_start_datetimes = dict(values or {})
        self._recompute()

    def add_task(self):
        self.checkpoint()
        previous = self.rows[-1] if self.rows else None
        resource_id = ((previous or {}).get("resource_id") or
                       (self.resources[0].get("resource_id", "") if self.resources else ""))
        resource = next((row for row in self.resources
                         if row.get("resource_id") == resource_id), {})
        default_speed = resource.get("default_speed_kn")
        now = schema.utc_now_iso()
        row = {
            "task_id": schema.new_id(), "seq": len(self.rows), "name": "New task",
            "description": "", "operation_type": "", "is_phase": 0,
            "outline_level": int((previous or {}).get("outline_level") or 0),
            "resource_id": resource_id, "duration_mode": "manual",
            "duration_hours": 1.0,
            "predecessor_task_id": (previous or {}).get("task_id") or "",
            "dependency_type": "FS",
            "lag_hours": 0.0,
            "speed_knots": default_speed, "direction": "forward",
            "location_mode": "feature", "location_chainage_m": None,
            "constraint_type": "", "constraint_datetime": "", "is_milestone": 0,
            "fuel_mode": "", "bunker_amount": None,
            "cable_mode": "", "cable_amount_m": None, "layer_id": "",
            "layer_source": "", "layer_name": "", "feature_id": "",
            "feature_label": "", "geom_kind": "", "linked_ref_json": "",
            "progress_status": "not_started", "percent_complete": 0.0,
            "actual_start_datetime": "", "actual_finish_datetime": "",
            "remaining_duration_hours": None, "progress_notes": "",
            "actual_log_json": "[]", "progress_updated_utc": "",
            "created_utc": now, "modified_utc": now, "notes": "",
        }
        # Most follow-on operations happen where the preceding operation
        # finishes.  Reusing its editable feature reference gives the new task
        # that position/context without creating another geometry snapshot.
        if previous is not None:
            for key in ("layer_id", "layer_source", "layer_name", "feature_id",
                        "feature_label", "geom_kind", "linked_ref_json"):
                row[key] = previous.get(key) or ""
            if row.get("linked_ref_json"):
                try:
                    metadata = json.loads(str(row["linked_ref_json"]))
                    if metadata.get("owned_geometry"):
                        metadata.pop("owned_geometry", None)
                        metadata["referenced_task_id"] = previous.get("task_id") or ""
                        metadata["location_reference"] = True
                        row["linked_ref_json"] = json.dumps(metadata, sort_keys=True)
                except (TypeError, ValueError):
                    pass
            row["direction"] = previous.get("direction") or "forward"
            if previous.get("geom_kind") == "line" and not self._is_summary(len(self.rows) - 1):
                row["location_mode"] = (
                    "line_start" if previous.get("direction") == "reverse" else "line_end")
                row["speed_knots"] = None
        self.rows.append(row)
        self._rebuild()
        self.selectRow(len(self.rows) - 1)
        self._emit_change()

    def group_selected(self):
        """Insert an editable summary row above a contiguous selected block."""
        indices = self.selected_row_indices()
        if not indices:
            return
        indices = self._include_summary_descendants(indices)
        first, last = min(indices), max(indices)
        if indices != list(range(first, last + 1)):
            QMessageBox.information(
                self, "Group tasks",
                "Select one contiguous block of tasks to create an outline group.")
            return
        self.checkpoint()
        level = min(int(self.rows[index].get("outline_level") or 0) for index in indices)
        now = schema.utc_now_iso()
        group = {
            "task_id": schema.new_id(), "seq": first, "name": "New group",
            "description": "", "operation_type": "", "is_phase": 0, "outline_level": level,
            "resource_id": "", "duration_mode": "manual", "duration_hours": 0.0,
            "predecessor_task_id": "", "dependency_type": "FS",
            "lag_hours": 0.0, "speed_knots": None,
            "direction": "forward", "fuel_mode": "", "bunker_amount": None,
            "cable_mode": "", "cable_amount_m": None,
            "location_mode": "feature", "location_chainage_m": None,
            "constraint_type": "", "constraint_datetime": "", "is_milestone": 0,
            "layer_id": "", "layer_source": "", "layer_name": "", "feature_id": "",
            "feature_label": "", "geom_kind": "", "linked_ref_json": "",
            "progress_status": "not_started", "percent_complete": 0.0,
            "actual_start_datetime": "", "actual_finish_datetime": "",
            "remaining_duration_hours": None, "progress_notes": "",
            "actual_log_json": "[]", "progress_updated_utc": "",
            "created_utc": now, "modified_utc": now, "notes": "",
        }
        for index in indices:
            self.rows[index]["outline_level"] = (
                int(self.rows[index].get("outline_level") or 0) + 1)
        self.rows.insert(first, group)
        self._normalise_outline()
        self._renumber()
        self._rebuild()
        self.selectRow(first)
        self.editItem(self.item(first, self.COL_TASK))
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

    def insert_tasks(self, rows, at=None):
        """Insert task rows at ``at`` (default: end), indented like their new siblings."""
        if not rows:
            return
        self.checkpoint()
        at = len(self.rows) if at is None else min(max(0, int(at)), len(self.rows))
        prepared = []
        for row in rows:
            copied = dict(row)
            copied["is_phase"] = 0
            copied.setdefault("outline_level", 0)
            prepared.append(copied)
        self._rebase_outline(prepared, self.rows, at)
        self.rows = self.rows[:at] + prepared + self.rows[at:]
        self._normalise_outline()
        self._renumber()
        self._rebuild()
        self._select_rows(range(at, at + len(prepared)))
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
        # A drop just below a collapsed summary points between its hidden
        # children; land after the whole hidden block instead of inside it.
        while target < len(self.rows) and self.isRowHidden(target):
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
        self._rebase_outline(moving, remaining, insert_at)
        self.rows = remaining[:insert_at] + moving + remaining[insert_at:]
        self._normalise_outline()
        self._renumber()
        self._rebuild()
        self._select_rows(range(insert_at, insert_at + len(moving)))
        self._emit_change()
        # Accepting the proposed MoveAction would make QAbstractItemView's
        # InternalMove cleanup remove the re-selected rows after this handler,
        # so the dropped tasks vanished from the view. The move is already done
        # above; report IgnoreAction so Qt performs no removal of its own.
        event.setDropAction(DROP_ACTION_IGNORE)
        event.accept()

    @staticmethod
    def _rebase_outline(moving, remaining, insert_at):
        """Indent the dropped block to match the row it will sit above."""
        if insert_at < len(remaining):
            base = int(remaining[insert_at].get("outline_level") or 0)
        elif remaining:
            base = int(remaining[-1].get("outline_level") or 0)
        else:
            base = 0
        shift = base - int(moving[0].get("outline_level") or 0)
        for row in moving:
            row["outline_level"] = max(0, int(row.get("outline_level") or 0) + shift)

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
        self.update_links_bulk({task_id: reference}, record_history=record_history)

    def update_links_bulk(self, references, record_history=True):
        """Apply several link updates in one checkpoint/rebuild/save cycle.

        ``references`` maps task_id -> reference fields ({} clears a link).
        """
        updates = [(self.row_by_id(task_id), reference or {})
                   for task_id, reference in (references or {}).items()]
        updates = [(task, reference) for task, reference in updates if task is not None]
        if not updates:
            return
        if record_history:
            self.checkpoint()
        for task, reference in updates:
            for key in LINK_KEYS:
                task[key] = reference.get(key, "")
            # Optional location fields ride along when a caller shares an
            # explicit start/end/chainage position with other tasks.
            for key in ("location_mode", "location_chainage_m"):
                if key in reference:
                    task[key] = reference[key]
            if self.spatial_kind(task) == "line" and _float(task.get("speed_knots")) > 0:
                task["duration_mode"] = "computed"
        self.resolver.clear_cache()
        self._rebuild()
        updated_ids = {task.get("task_id") for task, _reference in updates}
        rows = [index for index, row in enumerate(self.rows)
                if row.get("task_id") in updated_ids]
        if len(rows) == 1:
            self.selectRow(rows[0])
        elif rows:
            self._select_rows(rows)
        self._emit_change()

    def apply_reference_fields(self, references):
        """Mirror reference repairs already written to disk (no re-save)."""
        changed = False
        for task_id, reference in (references or {}).items():
            task = self.row_by_id(task_id)
            if task is None:
                continue
            for key in LINK_KEYS:
                task[key] = (reference or {}).get(key, "")
            changed = True
        if changed:
            self.resolver.clear_cache()
            self._rebuild()

    def selected_task_ids(self, include_summary=False):
        """Task ids of the selected rows, top to bottom."""
        ids = []
        for index in self.selected_row_indices():
            if not (0 <= index < len(self.rows)):
                continue
            if not include_summary and self._is_summary(index):
                continue
            ids.append(self.rows[index].get("task_id") or "")
        return [task_id for task_id in ids if task_id]

    def update_task_fields(self, task_id, values, record_history=True):
        task = self.row_by_id(task_id)
        if task is None or not values:
            return
        if record_history:
            self.checkpoint()
        task.update(dict(values))
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
            spatial_kind = "" if summary else self.spatial_kind(row)
            if spatial_kind == "line":
                route_length = self.resolver.route_length_m(row)
            elif summary:
                route_length = None
            else:
                # A manually entered distance (loading a cable, spooling, …)
                # feeds the same computed-duration path as a measured route.
                manual = self._manual_distance_m(row)
                route_length = manual if manual > 0.0 else None
            profile = None if summary else parse_speed_profile(
                row.get("speed_profile_json"))
            specs.append(TaskSpec(
                task_id=row.get("task_id") or schema.new_id(), seq=index,
                name=row.get("name") or "", resource_id=row.get("resource_id") or "",
                duration_mode=row.get("duration_mode") or "manual",
                duration_hours=_float(row.get("duration_hours")),
                predecessor_task_id=row.get("predecessor_task_id") or "",
                lag_hours=_float(row.get("lag_hours")), speed_knots=_float(row.get("speed_knots")),
                direction=row.get("direction") or "forward",
                geom_kind=spatial_kind,
                route_length_m=route_length, is_phase=summary,
                outline_level=int(row.get("outline_level") or 0),
                fuel_mode="" if summary else row.get("fuel_mode") or "",
                bunker_amount=0.0 if summary else _float(row.get("bunker_amount")),
                cable_mode="" if summary else row.get("cable_mode") or "",
                cable_amount_m=None if summary or row.get("cable_amount_m") in (None, "")
                else _float(row.get("cable_amount_m")),
                dependency_type=row.get("dependency_type") or "FS",
                constraint_type="" if summary else row.get("constraint_type") or "",
                constraint_datetime=None if summary else row.get("constraint_datetime") or None,
                is_milestone=False if summary else bool(row.get("is_milestone")),
                location_key="" if summary else self.location_key_for(row),
                speed_profile=profile,
            ))
        return specs

    def location_key_for(self, task):
        """Location key, following shared references to the owning task.

        Sharers of one task's geometry get the owner's key so overlapping
        tasks at the same shared point are flagged together, even after the
        owner's stored geometry has been re-placed.
        """
        owner_id = shared_owner_task_id(task)
        if owner_id and (task.get("location_mode") or "feature") == "feature":
            owner = self.row_by_id(owner_id)
            if owner is not None:
                key = self.location_key(owner)
                if key:
                    return key
        return self.location_key(task)

    @staticmethod
    def spatial_kind(task):
        if (task.get("location_mode") or "feature") in (
                "line_start", "line_end", "route_chainage"):
            return "point"
        return task.get("geom_kind") or ""

    @staticmethod
    def location_key(task):
        feature = task.get("feature_id") or ""
        source = task.get("layer_source") or task.get("layer_id") or ""
        if not feature or not source:
            return ""
        mode = task.get("location_mode") or "feature"
        chainage = task.get("location_chainage_m") if mode == "route_chainage" else ""
        return "%s|%s|%s|%s" % (source, feature, mode, chainage)

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
                self._operation_combo(row_index, task, summary)
                self._set_text(row_index, self.COL_DESCRIPTION, task.get("description"))
                self._resource_combo(row_index, task, summary)
                if summary:
                    self._readonly_item(row_index, self.COL_DURATION, "")
                else:
                    self._set_text(
                        row_index, self.COL_DURATION,
                        self._display_duration(task.get("duration_hours")))
                self._predecessor_combo(row_index, task, summary)
                self._feature_button(row_index, task, summary)
                self._readonly_item(row_index, self.COL_DISTANCE, "")
                if summary:
                    self._readonly_item(row_index, self.COL_SPEED, "")
                else:
                    # Text, flags, and tooltips are refreshed in _recompute,
                    # which knows the task's distance unit and speed profile.
                    self._set_text(row_index, self.COL_SPEED, "")
                self._direction_combo(row_index, task, summary)
                self._fuel_mode_combo(row_index, task, summary)
                if summary:
                    self._readonly_item(row_index, self.COL_BUNKER, "")
                else:
                    self._set_text(row_index, self.COL_BUNKER,
                                   _display_number(task.get("bunker_amount")))
                self._cable_mode_combo(row_index, task, summary)
                if summary:
                    self._readonly_item(row_index, self.COL_CABLE, "")
                else:
                    amount = task.get("cable_amount_m")
                    self._set_text(
                        row_index, self.COL_CABLE,
                        "" if amount in (None, "") else
                        _display_number(_float(amount) / 1000.0))
                self._readonly_item(row_index, self.COL_CABLE_ONBOARD, "")
                self._readonly_item(row_index, self.COL_START, "")
                self._readonly_item(row_index, self.COL_FINISH, "")
                self._readonly_item(row_index, self.COL_FLOAT, "")
                self._readonly_item(row_index, self.COL_FUEL_USED, "")
                self._readonly_item(row_index, self.COL_ROB, "")
                self._readonly_item(
                    row_index, self.COL_PROGRESS,
                    "" if summary else "%s%%" % _display_number(task.get("percent_complete")))
                status = dict(schema.PROGRESS_STATUSES).get(
                    task.get("progress_status") or "not_started", "Not started")
                self._readonly_item(row_index, self.COL_STATUS, "" if summary else status)
                self._set_text(row_index, self.COL_NOTES, task.get("notes"))
        finally:
            self._muted = False
        self._recompute()
        self._apply_collapsed_rows()
        if self._active_task_ids:
            self._apply_active_highlight()

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

    def contextMenuEvent(self, event):
        position = self.viewport().mapFromGlobal(event.globalPos())
        row = self.rowAt(position.y())
        if not (0 <= row < len(self.rows)):
            return
        if row not in self.selected_row_indices():
            self.selectRow(row)
        task = self.rows[row]
        task_id = task.get("task_id") or ""
        menu = QMenu(self)
        zoom = menu.addAction("Zoom to task on map",
                              lambda: self.zoomRequested.emit(task_id))
        zoom.setEnabled(bool(task.get("feature_id")))
        advanced = menu.addAction("Advanced task settings…",
                                  lambda: self.advancedRequested.emit(task_id))
        progress = menu.addAction("Update actual progress…",
                                  lambda: self.progressRequested.emit(task_id))
        advanced.setEnabled(not self._is_summary(row))
        progress.setEnabled(not self._is_summary(row))
        link = menu.addAction("Link or share location…",
                              lambda: self.linkRequested.emit(task_id))
        link.setEnabled(not self._is_summary(row))
        profile_action = menu.addAction(
            "Speed profile…", lambda: self.speedProfileRequested.emit(task_id))
        profile_action.setEnabled(not self._is_summary(row))
        unit_menu = menu.addMenu("Distance unit")
        unit_menu.setEnabled(not self._is_summary(row))
        current_unit = distance_unit_for(task)
        for unit, label in DISTANCE_UNIT_LABELS:
            unit_action = unit_menu.addAction(label)
            unit_action.setCheckable(True)
            unit_action.setChecked(unit == current_unit)
            unit_action.triggered.connect(
                lambda _checked=False, u=unit: self._set_distance_unit_selected(u))
        menu.addSeparator()
        menu.addAction("Group selected tasks", self.group_selected)
        menu.addAction("Indent (make child)", lambda: self.indent_selected(1))
        menu.addAction("Outdent (promote)", lambda: self.indent_selected(-1))
        menu.addAction("Move up", lambda: self.move_selected(-1))
        menu.addAction("Move down", lambda: self.move_selected(1))
        menu.addSeparator()
        menu.addAction("Delete selected", self.delete_selected)
        qt_exec(menu, event.globalPos())

    def set_active_tasks(self, task_ids):
        """Tint the rows whose tasks are active at the playback time."""
        wanted = {str(task_id) for task_id in (task_ids or ())}
        if wanted == self._active_task_ids:
            return
        self._active_task_ids = wanted
        self._apply_active_highlight()

    def _apply_active_highlight(self):
        highlight = QBrush(QColor(255, 214, 79, 90))
        self._muted = True
        try:
            for row_index, row in enumerate(self.rows):
                brush = (highlight if str(row.get("task_id")) in self._active_task_ids
                         else QBrush())
                for column in range(self.columnCount()):
                    item = self.item(row_index, column)
                    if item is not None:
                        item.setBackground(brush)
        finally:
            self._muted = False

    def _cell_double_clicked(self, row, column):
        if not (0 <= row < len(self.rows)):
            return
        if column == self.COL_NUMBER:
            self.zoomRequested.emit(self.rows[row].get("task_id") or "")
            return
        if column != self.COL_TASK:
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
            combo.addItem(_colour_icon(resource.get("color_hex")),
                          resource.get("name") or "Resource",
                          resource.get("resource_id") or "")
        index = combo.findData(task.get("resource_id") or "")
        combo.setCurrentIndex(max(0, index))
        combo.setEnabled(not summary)
        task_id = task.get("task_id")
        combo.currentIndexChanged.connect(
            lambda _index, c=combo, tid=task_id: self._combo_changed(tid, "resource_id", c.currentData()))
        self.setCellWidget(row, self.COL_RESOURCE, combo)

    def _operation_combo(self, row, task, summary=False):
        combo = QComboBox()
        combo.setEditable(True)
        current = task.get("operation_type") or ""
        choices = list(self.operation_choices)
        if current and current not in {value for value, _label in choices}:
            choices.append((current, current))
        for value, label in choices:
            combo.addItem(label, value)
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setEditText(current)
        combo.setEnabled(not summary)
        task_id = task.get("task_id")
        combo.activated.connect(
            lambda _index, c=combo, tid=task_id: self._combo_changed(
                tid, "operation_type", c.currentData() or c.currentText()))
        if combo.lineEdit() is not None:
            combo.lineEdit().editingFinished.connect(
                lambda c=combo, tid=task_id: self._combo_changed(
                    tid, "operation_type", c.currentData() or c.currentText()))
        self.setCellWidget(row, self.COL_OPERATION, combo)

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
        location_labels = {
            "line_start": "start", "line_end": "end", "route_chainage": "position",
        }
        location = location_labels.get(task.get("location_mode") or "feature")
        if location:
            label = "%s (%s)" % (label, location)
        owner_id = shared_owner_task_id(task)
        if owner_id:
            label = "%s ⇄" % label
        button = QPushButton((layer + " / " if layer else "") + label)
        button.clicked.connect(lambda _checked=False, tid=task.get("task_id"): self.linkRequested.emit(tid))
        if owner_id:
            owner = self.row_by_id(owner_id)
            owner_name = (owner or {}).get("name") or "a deleted task"
            button.setToolTip(
                "Shared location — this task follows the point/route owned by "
                "'%s'. Move that task's geometry to move every task sharing it." % owner_name)
        if task.get("feature_id") and self.resolver.resolve(task) is None:
            button.setStyleSheet("QPushButton { background: #d99b32; }")
            button.setToolTip("Linked feature is unavailable: %s / %s" % (layer, label))
        self.setCellWidget(row, self.COL_FEATURE, button)

    def _direction_combo(self, row, task, summary=False):
        combo = QComboBox()
        combo.addItem("Forward", "forward")
        combo.addItem("Reverse", "reverse")
        combo.setCurrentIndex(max(0, combo.findData(task.get("direction") or "forward")))
        combo.setEnabled(not summary and self.spatial_kind(task) == "line")
        task_id = task.get("task_id")
        combo.currentIndexChanged.connect(
            lambda _index, c=combo, tid=task_id: self._combo_changed(tid, "direction", c.currentData()))
        self.setCellWidget(row, self.COL_DIRECTION, combo)

    def _fuel_mode_combo(self, row, task, summary=False):
        combo = QComboBox()
        for value, label in schema.FUEL_MODES:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(task.get("fuel_mode") or "")))
        combo.setEnabled(not summary)
        task_id = task.get("task_id")
        combo.currentIndexChanged.connect(
            lambda _index, c=combo, tid=task_id: self._combo_changed(tid, "fuel_mode", c.currentData()))
        self.setCellWidget(row, self.COL_FUEL_MODE, combo)

    def _cable_mode_combo(self, row, task, summary=False):
        combo = QComboBox()
        for value, label in schema.CABLE_MODES:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(task.get("cable_mode") or "")))
        combo.setEnabled(not summary)
        task_id = task.get("task_id")
        combo.currentIndexChanged.connect(
            lambda _index, c=combo, tid=task_id: self._combo_changed(tid, "cable_mode", c.currentData()))
        self.setCellWidget(row, self.COL_CABLE_MODE, combo)

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
        if item.column() == self.COL_DISTANCE:
            self._distance_edited(item, task)
            return
        mapping = {
            self.COL_TASK: "name", self.COL_DESCRIPTION: "description",
            self.COL_DURATION: "duration_hours", self.COL_SPEED: "speed_knots",
            self.COL_BUNKER: "bunker_amount", self.COL_CABLE: "cable_amount_m",
            self.COL_NOTES: "notes",
        }
        field = mapping.get(item.column())
        if field is None:
            return
        self.checkpoint()
        if field == "duration_hours":
            task[field] = self._parse_duration_hours(item.text())
        elif field == "speed_knots":
            # Entered in the task's distance unit per hour; stored as knots.
            entered = _float(item.text(), None)
            factor = DISTANCE_FACTORS[distance_unit_for(task)]
            task[field] = (None if entered is None
                           else entered * factor / KNOT_M_PER_HOUR)
        elif field == "bunker_amount":
            task[field] = _float(item.text(), None)
        elif field == "cable_amount_m":
            # Entered in km; stored in metres. Blank = auto (task distance
            # for Lay/Recover). The sign comes from the Cable mode.
            entered = _float(item.text(), None)
            task[field] = None if entered is None else abs(entered) * 1000.0
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
            length_m = self._task_distance_m(task) if duration > 0 else None
            if length_m is not None and length_m > 0:
                task["speed_knots"] = length_m / (duration * 3600.0 * 0.514444)
                task["duration_mode"] = "computed"
                self._muted = True
                try:
                    speed_item = self.item(item.row(), self.COL_SPEED)
                    if speed_item is not None:
                        speed_item.setText(self._display_speed(
                            task["speed_knots"], distance_unit_for(task)))
                finally:
                    self._muted = False
            else:
                task["duration_mode"] = "manual"
        elif field == "speed_knots":
            distance_m = self._task_distance_m(task)
            task["duration_mode"] = (
                "computed" if distance_m and _float(task[field]) > 0 else "manual")
        self._recompute()
        self._emit_change()

    def _distance_edited(self, item, task):
        """A manual distance was typed (non-route tasks only)."""
        if self._is_summary(item.row()) or self.spatial_kind(task) == "line":
            return
        text = item.text().strip()
        value = None
        unit = None
        if text:
            parsed = _parse_value_unit(text, {"nm": "nm", "km": "km", "m": "m"})
            if parsed is None or parsed[0] < 0:
                # Unparseable entry: restore the previous display, change nothing.
                self._recompute()
                return
            value, unit = parsed
        self.checkpoint()
        if value is None or value == 0:
            task["manual_distance_m"] = None
            if task.get("duration_mode") == "computed":
                task["duration_mode"] = "manual"
        else:
            if unit:
                task["distance_unit"] = unit
            factor = DISTANCE_FACTORS[distance_unit_for(task)]
            task["manual_distance_m"] = value * factor
            if (_float(task.get("speed_knots")) > 0
                    or parse_speed_profile(task.get("speed_profile_json"))):
                task["duration_mode"] = "computed"
        self._recompute()
        self._emit_change()

    def _on_selection_changed(self):
        row = self.currentRow()
        if 0 <= row < len(self.rows):
            self.taskSelected.emit(self.rows[row].get("task_id") or "")

    def _refresh_distance_cell(self, item, row, spec, unit, summary_row):
        if item is None:
            return
        if summary_row:
            item.setText("")
            item.setToolTip("")
            item.setFlags(item.flags() & ~ITEM_FLAG_EDITABLE)
            return
        if self.spatial_kind(row) == "line":
            item.setFlags(item.flags() & ~ITEM_FLAG_EDITABLE)
            if spec.route_length_m is not None:
                item.setText("%s %s" % (
                    self._display_distance(spec.route_length_m, unit), unit))
                item.setToolTip(
                    "Measured route distance: %s m. Right-click the row to "
                    "change the display unit." % _display_number(spec.route_length_m))
            else:
                item.setText("")
                item.setToolTip("")
            return
        item.setFlags(item.flags() | ITEM_FLAG_EDITABLE)
        manual = self._manual_distance_m(row)
        item.setText("%s %s" % (self._display_distance(manual, unit), unit)
                     if manual else "")
        item.setToolTip(
            "Manual distance (e.g. cable length for a loading task). With a "
            "speed it computes the duration. Accepts a unit suffix: 20 km, "
            "10 nm, 5000 m.")

    def _refresh_speed_cell(self, row_index, row, spec, unit, summary_row):
        item = self.item(row_index, self.COL_SPEED)
        if item is None or summary_row:
            return
        factor = DISTANCE_FACTORS.get(unit, 1852.0)
        if spec.speed_profile:
            resolved = resolve_speed_profile(spec.speed_profile, spec.route_length_m)
            average = ""
            if resolved:
                total_m = sum(distance for distance, _speed in resolved)
                hours = sum(distance / (speed * KNOT_M_PER_HOUR)
                            for distance, speed in resolved)
                if hours > 0:
                    average = _display_number(total_m / hours / factor)
            item.setText(average)
            item.setFlags(item.flags() & ~ITEM_FLAG_EDITABLE)
            item.setToolTip(
                "Average speed from the task's speed profile (%d legs), in %s/h. "
                "Right-click the row → Speed profile… to edit." % (
                    len(spec.speed_profile), unit))
            return
        item.setFlags(item.flags() | ITEM_FLAG_EDITABLE)
        if spec.route_length_m is not None:
            item.setText(self._display_speed(row.get("speed_knots"), unit))
            item.setToolTip("Speed in %s/h." % unit)
        else:
            item.setText("")
            item.setToolTip(
                "Speed in %s/h. Link a route or enter a distance for the "
                "speed to compute a duration." % unit)

    def _recompute(self):
        specs = self.task_specs()
        resource_offsets = {
            row.get("resource_id") or "": _float(row.get("start_offset_hours"))
            for row in self.resources
        }
        self.schedule = compute_schedule(
            self.anchor, specs, resource_offsets, self.schedule_mode,
            self.resource_start_datetimes)
        specs_by_id = {spec.task_id: spec for spec in specs}
        self.fuel = compute_fuel(self.schedule, specs_by_id, self.resources)
        self.cable = compute_cable(self.schedule, specs_by_id)
        # Only lanes with a fuel profile in use get visible fuel figures.
        fuel_tracked = {
            resource_id for resource_id, summary in self.fuel.by_resource.items()
            if summary.rob_start or summary.total_burn or summary.total_bunker
        }
        by_id = {task.task_id: task for task in self.schedule.tasks}
        self._muted = True
        try:
            for row_index, row in enumerate(self.rows):
                scheduled = by_id.get(row.get("task_id"))
                duration_item = self.item(row_index, self.COL_DURATION)
                distance_item = self.item(row_index, self.COL_DISTANCE)
                float_item = self.item(row_index, self.COL_FLOAT)
                duration_item.setToolTip("")
                spec = specs[row_index]
                unit = distance_unit_for(row)
                summary_row = self._is_summary(row_index)
                self._refresh_distance_cell(distance_item, row, spec, unit, summary_row)
                self._refresh_speed_cell(row_index, row, spec, unit, summary_row)
                if row.get("is_milestone") and not summary_row:
                    duration_item.setText("0")
                    duration_item.setFlags(duration_item.flags() & ~ITEM_FLAG_EDITABLE)
                    duration_item.setToolTip("Milestone (zero duration).")
                elif summary_row:
                    duration_item.setText(self._display_duration(
                        scheduled.duration_hours if scheduled is not None else 0.0))
                    duration_item.setFlags(duration_item.flags() & ~ITEM_FLAG_EDITABLE)
                    duration_item.setForeground(QBrush(QColor("#303030")))
                    font = duration_item.font()
                    font.setItalic(False)
                    font.setBold(True)
                    duration_item.setFont(font)
                    duration_item.setToolTip("Summary span derived from this row's indented tasks.")
                elif spec.speed_profile:
                    duration_item.setText(self._display_duration(
                        scheduled.duration_hours if scheduled is not None
                        else row.get("duration_hours")))
                    duration_item.setFlags(duration_item.flags() & ~ITEM_FLAG_EDITABLE)
                    duration_item.setForeground(QBrush(QColor("#777777")))
                    font = duration_item.font()
                    font.setItalic(True)
                    duration_item.setFont(font)
                    duration_item.setToolTip(
                        "Duration comes from the task's speed profile (%d legs). "
                        "Right-click the row → Speed profile… to change or clear it."
                        % len(spec.speed_profile))
                elif row.get("duration_mode") == "computed" and spec.route_length_m is not None and spec.speed_knots > 0:
                    duration_item.setText(self._display_duration(scheduled.duration_hours))
                    duration_item.setFlags(duration_item.flags() | ITEM_FLAG_EDITABLE)
                    duration_item.setForeground(QBrush(QColor("#777777")))
                    font = duration_item.font()
                    font.setItalic(True)
                    duration_item.setFont(font)
                    duration_item.setToolTip(
                        "Calculated from the task's distance and speed. Edit this value to recalculate speed.")
                else:
                    duration_item.setText(self._display_duration(row.get("duration_hours")))
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
                    float_item.setText(_display_number(scheduled.total_float_hours))
                    float_item.setToolTip(
                        "Critical path" if scheduled.critical else "Total float")
                    float_item.setForeground(
                        QBrush(QColor("#c62828")) if scheduled.critical else QBrush())
                    task_item = self.item(row_index, self.COL_TASK)
                    if task_item is not None and not self._is_summary(row_index):
                        task_item.setToolTip(
                            "Critical path task" if scheduled.critical else "")
                self._update_fuel_cells(row_index, row, fuel_tracked)
                self._update_cable_cells(row_index, row)
        finally:
            self._muted = False
        self.scheduleChanged.emit(self.schedule)

    def _auto_size_columns(self):
        minimums = {self.COL_OPERATION: 100, self.COL_RESOURCE: 110,
                    self.COL_PREDECESSOR: 130,
                    self.COL_FEATURE: 150, self.COL_DIRECTION: 80,
                    self.COL_FUEL_MODE: 80, self.COL_CABLE_MODE: 90,
                    self.COL_START: 110,
                    self.COL_FINISH: 110, self.COL_PROGRESS: 75,
                    self.COL_STATUS: 90}
        self._header_muted = True
        try:
            self.resizeColumnsToContents()
            for column in range(self.columnCount()):
                width = max(minimums.get(column, 56),
                            min(self.columnWidth(column), 320))
                self.setColumnWidth(column, width)
        finally:
            self._header_muted = False

    def _header_changed(self, *_args):
        if self._header_muted:
            return
        self._user_layout = True
        QSettings().setValue(self._header_key, self.horizontalHeader().saveState())

    def _header_menu(self, pos):
        menu = QMenu(self)
        for column in range(len(self.HEADERS)):
            if column in (self.COL_NUMBER, self.COL_TASK):
                continue
            header_item = self.horizontalHeaderItem(column)
            action = menu.addAction(
                header_item.text() if header_item else self.HEADERS[column])
            action.setCheckable(True)
            action.setChecked(not self.isColumnHidden(column))
            action.toggled.connect(
                lambda checked, c=column: self._set_column_visible(c, checked))
        menu.addSeparator()
        days_action = menu.addAction("Show durations in days")
        days_action.setCheckable(True)
        days_action.setChecked(self.duration_unit == "d")
        days_action.toggled.connect(
            lambda checked: self.set_duration_unit("d" if checked else "h"))
        menu.addSeparator()
        menu.addAction("Size columns to contents", self._size_columns_to_contents)
        menu.addAction("Reset column layout", self._reset_header)
        qt_exec(menu, self.horizontalHeader().mapToGlobal(pos))

    def _set_distance_unit_selected(self, unit):
        """Set the distance display unit on the selected rows (data unchanged)."""
        if unit not in DISTANCE_FACTORS:
            return
        indices = [index for index in self.selected_row_indices()
                   if 0 <= index < len(self.rows) and not self._is_summary(index)]
        if not indices and 0 <= self.currentRow() < len(self.rows):
            indices = [self.currentRow()]
        rows = [self.rows[index] for index in indices
                if distance_unit_for(self.rows[index]) != unit]
        if not rows:
            return
        self.checkpoint()
        for row in rows:
            row["distance_unit"] = unit
        self._rebuild()
        self._emit_change()

    def _set_column_visible(self, column, visible):
        self.setColumnHidden(column, not visible)
        self._header_changed()

    def _size_columns_to_contents(self):
        self._auto_size_columns()
        self._header_changed()

    def _reset_header(self):
        header = self.horizontalHeader()
        self._header_muted = True
        try:
            header.restoreState(self._default_header_state)
            for column in range(self.columnCount()):
                self.setColumnHidden(column, False)
            for column in (self.COL_FLOAT, self.COL_PROGRESS, self.COL_STATUS):
                self.setColumnHidden(column, True)
        finally:
            self._header_muted = False
        self._user_layout = False
        QSettings().remove(self._header_key)
        self._auto_size_columns()

    def _update_fuel_cells(self, row_index, row, fuel_tracked):
        fuel_item = self.item(row_index, self.COL_FUEL_USED)
        rob_item = self.item(row_index, self.COL_ROB)
        if fuel_item is None or rob_item is None:
            return
        fuel_item.setText("")
        rob_item.setText("")
        rob_item.setForeground(QBrush())
        rob_item.setToolTip("")
        if self._is_summary(row_index):
            burn = sum(
                self.fuel.by_task[self.rows[child].get("task_id")].burn
                for child in self._include_summary_descendants([row_index])
                if child != row_index and self.rows[child].get("task_id") in self.fuel.by_task)
            if burn:
                fuel_item.setText(_display_number(burn))
            return
        task_fuel = self.fuel.by_task.get(row.get("task_id"))
        if task_fuel is None or (row.get("resource_id") or "") not in fuel_tracked:
            return
        fuel_item.setText(_display_number(task_fuel.burn) or "0")
        rob_item.setText(_display_number(task_fuel.rob_end) or "0")
        if task_fuel.rob_start - task_fuel.burn < -1e-9:
            rob_item.setForeground(QBrush(QColor("#c62828")))
            rob_item.setToolTip("The resource runs out of fuel during this task. "
                                "Add a bunker earlier in the plan or raise the start fuel.")

    def _update_cable_cells(self, row_index, row):
        """Refresh the read-only onboard cell and the auto-amount display.

        Only ever called from _recompute, inside its muted block, so item
        edits here do not re-enter _on_item_changed.
        """
        onboard_item = self.item(row_index, self.COL_CABLE_ONBOARD)
        cable_item = self.item(row_index, self.COL_CABLE)
        if onboard_item is None:
            return
        onboard_item.setText("")
        onboard_item.setForeground(QBrush())
        onboard_item.setToolTip("")
        if cable_item is not None and not self._is_summary(row_index):
            cable_item.setForeground(QBrush())
            font = cable_item.font()
            font.setItalic(False)
            cable_item.setFont(font)
            cable_item.setToolTip("")
        if self._is_summary(row_index):
            return
        task_cable = self.cable.by_task.get(row.get("task_id"))
        if task_cable is None:
            if (cable_item is not None
                    and row.get("cable_amount_m") in (None, "")):
                cable_item.setText("")
            return
        onboard_item.setText(
            _display_number(task_cable.onboard_end_m / 1000.0) or "0")
        if task_cable.onboard_end_m < -1e-9:
            onboard_item.setForeground(QBrush(QColor("#c62828")))
            onboard_item.setToolTip(
                "More cable paid off than the resource has onboard. Add a "
                "Load task earlier in the plan or check the amounts.")
        if cable_item is not None and row.get("cable_amount_m") in (None, ""):
            cable_item.setText(
                _display_number(task_cable.amount_m / 1000.0)
                if task_cable.amount_m > 0.0 else "")
            cable_item.setForeground(QBrush(QColor("#777777")))
            font = cable_item.font()
            font.setItalic(True)
            cable_item.setFont(font)
            cable_item.setToolTip(
                "Automatic: the task's distance. Type a value to override, "
                "or clear it to return to automatic.")

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


def _colour_icon(color_hex):
    colour = QColor(str(color_hex or ""))
    if not colour.isValid():
        colour = QColor(schema.DEFAULT_RESOURCE_COLOR)
    pixmap = QPixmap(12, 12)
    pixmap.fill(colour)
    return QIcon(pixmap)
