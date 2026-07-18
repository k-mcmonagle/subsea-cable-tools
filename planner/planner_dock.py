# -*- coding: utf-8 -*-
"""Dockable spatial scenario Planner and playback UI."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json

from qgis.PyQt.QtCore import QDateTime, QSettings, Qt
from qgis.PyQt.QtGui import QColor, QCursor, QFontMetrics
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDateTimeEdit, QDialog,
    QDialogButtonBox, QDoubleSpinBox,
    QDockWidget, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QMessageBox,
    QLineEdit, QMenu, QPushButton, QSlider, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils, QgsGeometry, QgsProject, QgsRectangle,
)
from qgis.gui import QgsMapLayerComboBox

from ..qgis_compat import (
    BUTTON_BOX_CANCEL, BUTTON_BOX_OK, DIALOG_ACCEPTED, ITEM_DATA_USER_ROLE,
    MAP_LAYER_FILTER_LINE,
    MAP_LAYER_FILTER_POINT, MESSAGE_BOX_NO, MESSAGE_BOX_YES, MESSAGE_CRITICAL,
    MESSAGE_INFO, MESSAGE_WARNING,
    SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_EXTENDED,
    SIZE_POLICY_IGNORED, SIZE_POLICY_PREFERRED, TEXT_ELIDE_RIGHT,
    WINDOW_HINT_CLOSE,
    WINDOW_HINT_CUSTOMIZE, WINDOW_HINT_MIN_MAX, WINDOW_HINT_TITLE, WINDOW_TYPE_WINDOW,
    qt_exec,
)
from . import schema, standard_tasks
from .feature_ref import FeatureReferenceResolver, feature_reference
from .map_overlay import FeaturePickSession, PlannerMapOverlay
from .msproject_export import build_msp_tsv
from .sim_controller import SimulationController
from .sketch_tool import SketchSession
from .store import PlannerStore, default_project_gpkg_path, project_gpkg_path, set_project_gpkg_path
from .task_table import TaskTableWidget
from .timeline_engine import position_at


LABEL_SETTING_PREFIX = "subsea_cable_tools/planner/simulation_labels/"
STANDARD_TASKS_SETTING = "subsea_cable_tools/planner/standard_tasks"
LABEL_OPTIONS = (
    ("show", "Show task labels", True),
    ("task_name", "Task name", True),
    ("task_number", "Task number", False),
    ("resource", "Resource name", False),
    ("progress", "Progress", False),
    ("clock", "Simulation time", False),
    ("speed_distance", "Speed and distance", False),
)


class _ElidedLabel(QLabel):
    """Label that elides its text instead of demanding layout width."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(SIZE_POLICY_IGNORED, SIZE_POLICY_PREFERRED)
        self.setText(text)

    def setText(self, text):
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._apply_elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self):
        metrics = QFontMetrics(self.font())
        super().setText(metrics.elidedText(
            self._full_text, TEXT_ELIDE_RIGHT, max(0, self.width() - 4)))


class PlannerDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Planner", parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        # Keep the historic object name so saved QGIS dock placement is retained.
        self.setObjectName("PlanOfWorkPlannerDock")
        self._loading = False
        self._save_error = False
        self.current_scenario_id = ""
        path = project_gpkg_path() or default_project_gpkg_path()
        self.store = PlannerStore(path)
        try:
            self.store.migrate()
        except Exception as exc:
            QMessageBox.warning(
                None, "Planner",
                "Could not open or migrate the planner GeoPackage:\n%s\n\n%s\n\n"
                "Check the file is writable (not locked by another program or "
                "cloud sync) and reopen the Planner." % (path, exc))
        if not project_gpkg_path():
            set_project_gpkg_path(path)
        self.geometry_layers = self.store.load_geometry_layers()
        self.resolver = FeatureReferenceResolver(QgsProject.instance(), self.canvas, self.store)
        self.overlay = PlannerMapOverlay(self.canvas)
        self.pick_session = FeaturePickSession(self.canvas)
        self.sketch_session = SketchSession(
            self.canvas, self, fallback_tool=lambda: self.iface.actionPan().trigger())
        self.sim = SimulationController(self)
        self.label_settings = {
            key: _setting_bool(LABEL_SETTING_PREFIX + key, default)
            for key, _label, default in LABEL_OPTIONS
        }

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.addLayout(self._build_scenario_bar())
        outer.addLayout(self._build_task_buttons())
        self.task_table = TaskTableWidget(self.resolver)
        self.task_table.set_history_hooks(
            self._planner_history_snapshot, self._restore_planner_history_snapshot)
        outer.addWidget(self.task_table, 1)
        self.totals_label = _ElidedLabel("")
        self.totals_label.setToolTip("Whole-plan totals; updates live with the schedule.")
        outer.addWidget(self.totals_label)
        outer.addLayout(self._build_transport_bar())
        self.setWidget(container)

        self.task_table.tasksChanged.connect(self._save_tasks)
        self.task_table.scheduleChanged.connect(self._schedule_changed)
        self.task_table.linkRequested.connect(self._link_feature)
        self.task_table.taskSelected.connect(self._task_selected)
        self.task_table.zoomRequested.connect(self._zoom_to_task)
        self.task_table.historyStateChanged.connect(self._history_state_changed)
        for layer in self.geometry_layers:
            if hasattr(layer, "geometryChanged"):
                layer.geometryChanged.connect(self._planner_geometry_changed)
        self.sim.timeChanged.connect(self._simulation_time_changed)
        self.sim.playingChanged.connect(self.play_btn.setChecked)
        self.topLevelChanged.connect(self._top_level_changed)
        self.refresh_scenarios()

    def _build_scenario_bar(self):
        layout = QHBoxLayout()
        layout.addWidget(QLabel("Scenario:"))
        self.scenario_combo = QComboBox()
        self.scenario_combo.currentIndexChanged.connect(self._scenario_selected)
        layout.addWidget(self.scenario_combo, 1)
        for label, slot in (("New", self._new_scenario), ("Rename", self._rename_scenario),
                            ("Duplicate", self._duplicate_scenario), ("Delete", self._delete_scenario)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            layout.addWidget(button)
        self.anchor_edit = QDateTimeEdit()
        self.anchor_edit.setCalendarPopup(True)
        self.anchor_edit.setDisplayFormat("dd/MM/yyyy HH:mm")
        self.anchor_edit.dateTimeChanged.connect(self._anchor_changed)
        layout.addWidget(QLabel("Anchor:"))
        layout.addWidget(self.anchor_edit)
        resources_btn = QPushButton("Resources…")
        resources_btn.setToolTip(
            "Project-level vessels/resources shared by every scenario; deleting "
            "a scenario never deletes resources.")
        resources_btn.clicked.connect(self._edit_resources)
        layout.addWidget(resources_btn)
        return layout

    def _build_task_buttons(self):
        layout = QHBoxLayout()
        for label, slot in (("Add task", lambda: self.task_table.add_task()),
                            ("Standard tasks…", self._standard_tasks),
                            ("Sketch tasks…", self._sketch_tasks),
                            ("Import RPL…", self._import_rpl)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            layout.addWidget(button)
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.setEnabled(False)
        self.undo_btn.setToolTip("Undo the last planning change (Ctrl+Z).")
        self.undo_btn.clicked.connect(lambda: self.task_table.undo())
        layout.addWidget(self.undo_btn)
        self.redo_btn = QPushButton("Redo")
        self.redo_btn.setEnabled(False)
        self.redo_btn.setToolTip("Redo the last undone planning change (Ctrl+Y or Ctrl+Shift+Z).")
        self.redo_btn.clicked.connect(lambda: self.task_table.redo())
        layout.addWidget(self.redo_btn)
        edit_btn = QPushButton("Edit / outline…")
        edit_menu = QMenu(edit_btn)
        for label, slot in (
                ("Delete selected", lambda: self.task_table.delete_selected()),
                ("Merge selected routes", self._merge_selected),
                ("Indent (make child)", lambda: self.task_table.indent_selected(1)),
                ("Outdent (promote)", lambda: self.task_table.indent_selected(-1)),
                ("Move up", lambda: self.task_table.move_selected(-1)),
                ("Move down", lambda: self.task_table.move_selected(1))):
            action = edit_menu.addAction(label)
            action.triggered.connect(slot)
        edit_btn.setMenu(edit_menu)
        edit_btn.setToolTip(
            "Indent tasks beneath the preceding row to form a group. "
            "Double-click a bold summary row to collapse or expand it.")
        layout.addWidget(edit_btn)
        fuel_btn = QPushButton("Fuel report…")
        fuel_btn.setToolTip(
            "Per-resource fuel totals: start fuel, burn, bunkers, remaining on board, "
            "and cost. Set fuel rates in Resources… and a fuel mode on each task.")
        fuel_btn.clicked.connect(self._show_fuel_report)
        layout.addWidget(fuel_btn)
        layout.addStretch(1)
        return layout

    def _build_transport_bar(self):
        layout = QHBoxLayout()
        self.play_btn = QPushButton("Play / Pause")
        self.play_btn.setCheckable(True)
        self.play_btn.toggled.connect(self._toggle_play)
        back = QPushButton("◀|")
        back.setToolTip("Previous task boundary")
        back.clicked.connect(lambda: self.sim.step_boundary(-1))
        forward = QPushButton("|▶")
        forward.setToolTip("Next task boundary")
        forward.clicked.connect(lambda: self.sim.step_boundary(1))
        layout.addWidget(back)
        layout.addWidget(self.play_btn)
        layout.addWidget(forward)
        self.speed_combo = QComboBox()
        for label, value in (("1 min/s", 60), ("10 min/s", 600), ("1 hour/s", 3600),
                             ("6 hours/s", 21600), ("1 day/s", 86400)):
            self.speed_combo.addItem(label, value)
        self.speed_combo.setCurrentIndex(2)
        self.speed_combo.currentIndexChanged.connect(
            lambda _index: self.sim.set_speed(self.speed_combo.currentData()))
        self.sim.set_speed(self.speed_combo.currentData())
        layout.addWidget(self.speed_combo)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.valueChanged.connect(lambda value: self.sim.seek_fraction(value / 1000.0))
        # The slider and status share leftover width by fixed stretch factors;
        # the label elides instead of resizing, so text changes during playback
        # can no longer push the slider around.
        layout.addWidget(self.slider, 3)
        self.status_label = _ElidedLabel("No scenario")
        layout.addWidget(self.status_label, 2)
        self.labels_btn = QPushButton("Labels…")
        labels_menu = QMenu(self.labels_btn)
        self.label_actions = {}
        for index, (key, label, _default) in enumerate(LABEL_OPTIONS):
            if index == 1:
                labels_menu.addSeparator()
            action = labels_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.label_settings[key])
            action.toggled.connect(
                lambda checked, setting_key=key: self._label_setting_changed(
                    setting_key, checked))
            self.label_actions[key] = action
        self.labels_btn.setMenu(labels_menu)
        self.labels_btn.setToolTip("Show or hide moving labels and choose their contents.")
        layout.addWidget(self.labels_btn)
        copy_btn = QPushButton("Copy to MS Project")
        copy_btn.clicked.connect(self._copy_ms_project)
        layout.addWidget(copy_btn)
        return layout

    def refresh_scenarios(self, select_id=None):
        scenarios = self.store.list_scenarios()
        selected = select_id or self.current_scenario_id
        self._loading = True
        try:
            self.scenario_combo.clear()
            for scenario in scenarios:
                self.scenario_combo.addItem(scenario.get("name") or "Scenario", scenario["scenario_id"])
            index = self.scenario_combo.findData(selected)
            self.scenario_combo.setCurrentIndex(index if index >= 0 else (0 if scenarios else -1))
        finally:
            self._loading = False
        self._load_scenario(self.scenario_combo.currentData() or "")

    def _scenario_selected(self):
        if not self._loading:
            self._load_scenario(self.scenario_combo.currentData() or "")

    def _load_scenario(self, scenario_id):
        self.sim.pause()
        self.overlay.clear()
        self.task_table.set_active_tasks(set())
        self.current_scenario_id = scenario_id
        scenario = self.store.get_scenario(scenario_id) if scenario_id else None
        if scenario is None:
            self._loading = True
            try:
                self.task_table.set_plan([], [], datetime.now().replace(second=0, microsecond=0))
                self.status_label.setText("Create a scenario to begin")
            finally:
                self._loading = False
            return
        anchor = _parse_anchor(scenario.get("start_datetime"))
        self._loading = True
        try:
            self.anchor_edit.setDateTime(QDateTime(anchor))
            self.task_table.set_plan(self.store.list_tasks(scenario_id),
                                     self.store.list_resources(), anchor)
        finally:
            self._loading = False
        self._schedule_changed(self.task_table.schedule)

    def _new_scenario(self):
        name, ok = QInputDialog.getText(self, "New planning scenario", "Name:")
        if not ok or not name.strip():
            return
        anchor = datetime.now().replace(second=0, microsecond=0)
        scenario_id = self.store.create_scenario(name.strip(), anchor.strftime("%Y-%m-%dT%H:%M"))
        self.refresh_scenarios(scenario_id)

    def _rename_scenario(self):
        scenario = self.store.get_scenario(self.current_scenario_id)
        if scenario is None:
            return
        name, ok = QInputDialog.getText(self, "Rename scenario", "Name:", text=scenario.get("name") or "")
        if ok and name.strip():
            scenario["name"] = name.strip()
            self.store.save_scenario(scenario)
            self.refresh_scenarios(self.current_scenario_id)

    def _duplicate_scenario(self):
        scenario = self.store.get_scenario(self.current_scenario_id)
        if scenario is None:
            return
        proposed = "%s copy" % (scenario.get("name") or "Scenario")
        name, ok = QInputDialog.getText(self, "Duplicate scenario", "Copy name:", text=proposed)
        if ok and name.strip():
            copied_id = self.store.duplicate_scenario(self.current_scenario_id, name.strip())
            self.refresh_scenarios(copied_id)

    def _delete_scenario(self):
        scenario = self.store.get_scenario(self.current_scenario_id)
        if scenario is None:
            return
        answer = QMessageBox.question(
            self, "Delete scenario",
            "Delete '%s' and all of its tasks? Resources are shared across "
            "scenarios and are kept." % scenario.get("name"),
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer == MESSAGE_BOX_YES:
            self.store.delete_scenario(self.current_scenario_id)
            self.refresh_scenarios()

    def _anchor_changed(self):
        if self._loading:
            return
        scenario = self.store.get_scenario(self.current_scenario_id)
        if scenario is None:
            return
        anchor = self.anchor_edit.dateTime().toPyDateTime().replace(second=0, microsecond=0)
        scenario["start_datetime"] = anchor.strftime("%Y-%m-%dT%H:%M")
        self.store.save_scenario(scenario)
        self.task_table.set_anchor(anchor)

    def _save_tasks(self, rows):
        if self._loading or not self.current_scenario_id:
            return
        try:
            self.store.save_tasks(self.current_scenario_id, rows)
            self._sync_spatial_attributes(self.task_table.schedule)
            self._save_error = False
        except Exception as exc:
            # Keep editing usable (changes stay in the table) and report once
            # rather than raising out of an itemChanged slot on every keystroke.
            if not self._save_error:
                self._save_error = True
                self.iface.messageBar().pushMessage(
                    "Planner",
                    "Could not save the plan to %s (%s). Changes remain in the "
                    "table; check the GeoPackage is writable, then edit again "
                    "to retry." % (self.store.gpkg_path, exc),
                    level=MESSAGE_CRITICAL, duration=10)

    def _history_state_changed(self, can_undo, can_redo):
        self.undo_btn.setEnabled(can_undo)
        self.redo_btn.setEnabled(can_redo)

    def _planner_history_snapshot(self):
        """Capture task rows and only the planner-owned geometry they reference."""
        rows = deepcopy(self.task_table.rows)
        geometries = {}
        for task in rows:
            task_id = str(task.get("task_id") or "")
            if not task_id or not _owned_geometry(task):
                continue
            found = self.store.get_task_geometry(task_id)
            if found is None:
                continue
            layer, feature, kind = found
            metadata = _owned_geometry_metadata(task)
            geometries[task_id] = {
                "geometry": QgsGeometry(feature.geometry()),
                "kind": kind,
                "crs": QgsCoordinateReferenceSystem(layer.crs()),
                "source_kind": metadata.get("source_kind") or "history",
                "source_ref": deepcopy(metadata.get("source_ref") or {}),
            }
        return {"rows": rows, "geometries": geometries}

    def _restore_planner_history_snapshot(self, snapshot):
        rows = deepcopy(snapshot.get("rows", []))
        geometries = snapshot.get("geometries", {})
        current_owned = {
            str(task.get("task_id")) for task in self.task_table.rows
            if task.get("task_id") and _owned_geometry(task)
        }
        desired_owned = set(geometries)
        self.store.delete_task_geometries(current_owned | desired_owned)
        by_id = {str(task.get("task_id")): task for task in rows}
        for task_id, saved in geometries.items():
            task = by_id.get(task_id)
            if task is None:
                continue
            reference = self.store.set_task_geometry(
                task_id, self.current_scenario_id, int(task.get("seq") or 0),
                task.get("name") or "Task", saved["geometry"], saved["kind"],
                source_crs=saved["crs"], resource_id=task.get("resource_id") or "",
                speed_knots=task.get("speed_knots"),
                duration_hours=task.get("duration_hours"), notes=task.get("notes") or "",
                source_kind=saved.get("source_kind") or "history",
                source_ref=saved.get("source_ref") or {})
            task.update(reference)
        self.resolver.clear_cache()
        return rows

    def _edit_resources(self):
        dialog = ResourceDialog(self.store.list_resources(), self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        resources = dialog.resources()
        if not resources:
            QMessageBox.warning(self, "Resources", "The project must have at least one resource.")
            return
        self.store.save_resources(resources)
        # Resources are shared: repoint tasks in every scenario, not just this one.
        self.store.remap_task_resources(
            {row["resource_id"] for row in resources}, resources[0]["resource_id"])
        if self.current_scenario_id:
            self._load_scenario(self.current_scenario_id)

    def _link_feature(self, task_id):
        menu = QMenu(self)
        existing_action = menu.addAction("Choose or pick existing feature…")
        point_action = menu.addAction("Place new point on map")
        line_action = menu.addAction("Draw new route on map")
        menu.addSeparator()
        clear_action = menu.addAction("Clear geometry link")
        chosen = qt_exec(menu, QCursor.pos())
        if chosen == point_action:
            self._start_task_sketch(task_id, "point")
            return
        if chosen == line_action:
            self._start_task_sketch(task_id, "line")
            return
        if chosen == clear_action:
            self._clear_task_link(task_id)
            return
        if chosen != existing_action:
            return
        dialog = FeatureLinkDialog(self.canvas, self.pick_session, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED and dialog.reference is not None:
            self.task_table.checkpoint()
            self._discard_owned_geometry(task_id)
            self.task_table.update_link(task_id, dialog.reference, record_history=False)

    def _start_task_sketch(self, task_id, mode):
        task = self.task_table.row_by_id(task_id)
        if task is None or not self.current_scenario_id:
            return
        instruction = ("Click, snap, or enter latitude/longitude or route KP to place the task "
                       "point. Escape cancels." if mode == "point" else
                       "Click route vertices; use the route-sketch controls to pause, continue, "
                       "undo, clear, snap, enter coordinates/KP, or accept the route.")
        self.iface.messageBar().pushMessage("Planner", instruction, level=MESSAGE_INFO, duration=8)

        def completed(points):
            geometry = (QgsGeometry.fromPointXY(points[0]) if mode == "point"
                        else QgsGeometry.fromPolylineXY(points))
            self._apply_owned_geometry(task_id, geometry, mode, "drawn", {})

        self.sketch_session.start(
            mode, completed, route_options=self._sketch_route_options(task_id))

    def _apply_owned_geometry(self, task_id, geometry, kind, source_kind, source_ref):
        task = self.task_table.row_by_id(task_id)
        if task is None:
            return
        self.task_table.checkpoint()
        try:
            reference = self.store.set_task_geometry(
                task_id, self.current_scenario_id, int(task.get("seq") or 0),
                task.get("name") or "Task", geometry, kind,
                source_crs=self.canvas.mapSettings().destinationCrs(),
                resource_id=task.get("resource_id") or "",
                speed_knots=task.get("speed_knots"), duration_hours=task.get("duration_hours"),
                notes=task.get("notes") or "", source_kind=source_kind, source_ref=source_ref)
        except Exception as exc:
            self.iface.messageBar().pushMessage(
                "Planner", "Could not store the sketched geometry: %s" % exc,
                level=MESSAGE_CRITICAL, duration=8)
            return
        self.task_table.update_link(task_id, reference, record_history=False)

    def _discard_owned_geometry(self, task_id):
        task = self.task_table.row_by_id(task_id)
        if task is not None and _owned_geometry(task):
            self.store.delete_task_geometries([task_id])

    def _clear_task_link(self, task_id):
        self.task_table.checkpoint()
        self._discard_owned_geometry(task_id)
        self.task_table.update_link(task_id, {}, record_history=False)

    def _standard_tasks(self):
        settings = QSettings()
        raw = settings.value(STANDARD_TASKS_SETTING, "")
        templates = (standard_tasks.templates_from_json(raw) if raw
                     else standard_tasks.default_templates())
        dialog = StandardTasksDialog(templates, self)
        result = qt_exec(dialog)
        settings.setValue(STANDARD_TASKS_SETTING,
                          standard_tasks.templates_to_json(dialog.templates()))
        if result != DIALOG_ACCEPTED or not dialog.selected_templates:
            return
        if not self.current_scenario_id:
            QMessageBox.information(self, "Standard tasks",
                                    "Create or select a scenario first.")
            return
        current = self.task_table.currentRow()
        at = len(self.task_table.rows) if current < 0 else min(
            current + 1, len(self.task_table.rows))
        anchor_task = self.task_table.rows[at - 1] if 0 < at <= len(self.task_table.rows) else None
        resource_id = (anchor_task or {}).get("resource_id") or (
            self.task_table.resources[0].get("resource_id", "")
            if self.task_table.resources else "")
        rows = [standard_tasks.template_to_task_row(template, resource_id)
                for template in dialog.selected_templates]
        self.task_table.insert_tasks(rows, at)

    def _sketch_tasks(self):
        if not self.current_scenario_id:
            QMessageBox.information(self, "Sketch tasks", "Create or select a scenario first.")
            return
        self.iface.messageBar().pushMessage(
            "Planner",
            "Click or enter waypoints, then use snapping and the route-sketch controls to adjust "
            "and accept the route.",
            level=MESSAGE_INFO, duration=10)
        self.sketch_session.start(
            "line", self._create_tasks_from_sketch,
            route_options=self._sketch_route_options())

    def _sketch_route_options(self, exclude_task_id=""):
        """Existing line tasks available for exact KP-based vertex placement."""
        options = []
        for index, task in enumerate(self.task_table.rows):
            if (task.get("task_id") == exclude_task_id or
                    task.get("geom_kind") != "line" or
                    self.task_table._is_summary(index)):
                continue
            frame = self.resolver.route_frame(task)
            if frame is None or frame.total_length_m <= 0:
                continue
            metadata = _owned_geometry_metadata(task)
            source_ref = metadata.get("source_ref") or {}
            try:
                kp_start = float(source_ref["kp_start"])
                kp_end = float(source_ref["kp_end"])
            except (KeyError, TypeError, ValueError):
                kp_start = 0.0
                kp_end = frame.total_length_m / 1000.0
            options.append({
                "label": "#%d %s (KP %.3f–%.3f)" % (
                    index + 1, task.get("name") or "Route", kp_start, kp_end),
                "task_id": task.get("task_id") or "", "frame": frame,
                "kp_start": kp_start, "kp_end": kp_end,
            })
        return options

    def _create_tasks_from_sketch(self, points):
        dialog = SketchTasksDialog(self.task_table.resources, len(points), self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        config = dialog.configuration()
        created = []
        previous_id = self.task_table.rows[-1].get("task_id") if self.task_table.rows else ""
        sequence = len(self.task_table.rows)

        def add_owned(name, geometry, kind, speed, duration, source_ref):
            nonlocal previous_id, sequence
            task = _new_task_row(
                name, config["resource_id"], sequence, speed, duration, previous_id,
                computed=(kind == "line" and speed > 0))
            reference = self.store.set_task_geometry(
                task["task_id"], self.current_scenario_id, sequence, name, geometry, kind,
                source_crs=self.canvas.mapSettings().destinationCrs(),
                resource_id=config["resource_id"], speed_knots=speed,
                duration_hours=duration, source_kind="sketch",
                source_ref=source_ref)
            task.update(reference)
            created.append(task)
            previous_id = task["task_id"]
            sequence += 1

        try:
            if config["mode"] == "whole":
                add_owned(config["base_name"], QgsGeometry.fromPolylineXY(points), "line",
                          config["speed_knots"], 1.0, {"sketch_mode": "whole"})
            else:
                for index in range(len(points) - 1):
                    waypoint = config["waypoint_tasks"].get(index)
                    if waypoint is not None:
                        add_owned(waypoint["name"], QgsGeometry.fromPointXY(points[index]),
                                  "point", 0.0, waypoint["duration_hours"],
                                  {"waypoint": index + 1})
                    add_owned("%s leg %d" % (config["base_name"], index + 1),
                              QgsGeometry.fromPolylineXY(points[index:index + 2]), "line",
                              config["speed_knots"], 1.0, {"leg": index + 1})
                    if index == len(points) - 2:
                        waypoint = config["waypoint_tasks"].get(index + 1)
                        if waypoint is not None:
                            add_owned(waypoint["name"], QgsGeometry.fromPointXY(points[index + 1]),
                                      "point", 0.0, waypoint["duration_hours"],
                                      {"waypoint": index + 2})
        except Exception as exc:
            self.iface.messageBar().pushMessage(
                "Planner", "Sketch stopped early — could not store task geometry: %s" % exc,
                level=MESSAGE_CRITICAL, duration=8)
        self.task_table.append_tasks(created)

    def _import_rpl(self):
        if not self.current_scenario_id:
            QMessageBox.information(self, "Import RPL", "Create or select a scenario first.")
            return
        # Implemented in planner.rpl_import; kept lazy so the core dock remains fast to open.
        from .rpl_import import RplImportDialog
        dialog = RplImportDialog(self.store, self.task_table.resources, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        self._append_imported_drafts(dialog.task_drafts())

    def _append_imported_drafts(self, drafts):
        if not drafts:
            return
        created = []
        previous_id = self.task_table.rows[-1].get("task_id") if self.task_table.rows else ""
        sequence = len(self.task_table.rows)
        try:
            for draft in drafts:
                task = _new_task_row(
                    draft["name"], draft["resource_id"], sequence, draft["speed_knots"],
                    draft.get("duration_hours", 1.0), previous_id,
                    computed=draft.get("duration_mode", "computed") == "computed")
                task["description"] = draft.get("description", "")
                task["notes"] = draft.get("notes", "")
                reference = self.store.set_task_geometry(
                    task["task_id"], self.current_scenario_id, sequence, task["name"],
                    draft["geometry"], "line", source_crs=draft["source_crs"],
                    resource_id=task["resource_id"], speed_knots=task["speed_knots"],
                    duration_hours=task["duration_hours"], notes=task["notes"],
                    source_kind=draft.get("source_kind", "rpl_import"),
                    source_ref=draft.get("source_ref", {}))
                task.update(reference)
                created.append(task)
                previous_id = task["task_id"]
                sequence += 1
        except Exception as exc:
            self.iface.messageBar().pushMessage(
                "Planner", "Import stopped early — could not store task geometry: %s" % exc,
                level=MESSAGE_CRITICAL, duration=8)
        self.task_table.append_tasks(created)

    def _merge_selected(self):
        from .spatial_tasks import merge_selected_tasks
        merge_selected_tasks(self)

    def _task_selected(self, task_id):
        task = self.task_table.row_by_id(task_id)
        resolved = self.resolver.resolve(task) if task else None
        if resolved is not None:
            try:
                self.canvas.flashFeatureIds(resolved.layer, [resolved.feature.id()])
            except Exception:
                pass

    def _zoom_to_task(self, task_id):
        task = self.task_table.row_by_id(task_id)
        resolved = self.resolver.resolve(task) if task else None
        if resolved is None:
            self.iface.messageBar().pushMessage(
                "Planner", "This task has no available map geometry to zoom to.",
                level=MESSAGE_WARNING, duration=4)
            return
        try:
            geometry = QgsGeometry(resolved.feature.geometry())
            source_crs = resolved.layer.crs()
            destination_crs = self.canvas.mapSettings().destinationCrs()
            if source_crs.isValid() and source_crs != destination_crs:
                geometry.transform(QgsCoordinateTransform(
                    source_crs, destination_crs, QgsProject.instance()))
            box = geometry.boundingBox()
            if box.width() == 0 and box.height() == 0:
                centre = box.center()
                current = self.canvas.extent()
                box = QgsRectangle(
                    centre.x() - current.width() / 2.0, centre.y() - current.height() / 2.0,
                    centre.x() + current.width() / 2.0, centre.y() + current.height() / 2.0)
            else:
                box.scale(1.25)
            self.canvas.setExtent(box)
            self.canvas.refresh()
            self.canvas.flashFeatureIds(resolved.layer, [resolved.feature.id()])
        except Exception:
            self.iface.messageBar().pushMessage(
                "Planner", "Could not zoom to the task geometry.",
                level=MESSAGE_WARNING, duration=4)

    def _planner_geometry_changed(self, *_args):
        self.resolver.clear_cache()
        self.task_table._recompute()

    def _schedule_changed(self, result):
        self.sim.set_result(result)
        self._sync_spatial_attributes(result)
        self._update_totals(result)
        if result.errors:
            self.status_label.setText(result.errors[0])

    def _update_totals(self, result):
        table = self.task_table
        count = sum(1 for index in range(len(table.rows)) if not table._is_summary(index))
        if not count or result.span_start is None or result.span_end is None:
            self.totals_label.setText("")
            return
        span_hours = (result.span_end - result.span_start).total_seconds() / 3600.0
        parts = [
            "%d task%s" % (count, "" if count == 1 else "s"),
            "%s → %s" % (result.span_start.strftime("%d/%m/%Y %H:%M"),
                         result.span_end.strftime("%d/%m/%Y %H:%M")),
            "%s h (%.1f d)" % (("%.1f" % span_hours).rstrip("0").rstrip("."),
                               span_hours / 24.0),
        ]
        resources = {row.get("resource_id"): row for row in table.resources}
        fuel_parts, fuel_cost = [], 0.0
        for resource_id, summary in table.fuel.by_resource.items():
            if not (summary.rob_start or summary.total_burn or summary.total_bunker):
                continue
            name = (resources.get(resource_id) or {}).get("name") or "Resource"
            note = " ⚠" if summary.warnings else ""
            fuel_parts.append("%s %s %s%s" % (
                name, _fmt_fuel(summary.total_burn), summary.unit or "t", note))
            fuel_cost += summary.cost
        if fuel_parts:
            parts.append("Fuel: " + ", ".join(fuel_parts))
        if fuel_cost:
            parts.append("Fuel cost {:,.0f}".format(fuel_cost))
        self.totals_label.setText("   |   ".join(parts))

    def _sync_spatial_attributes(self, result):
        if not self.current_scenario_id:
            return
        scheduled = {task.task_id: task for task in result.tasks}
        rows = []
        for row in self.task_table.rows:
            copied = dict(row)
            copied["scenario_id"] = self.current_scenario_id
            if copied.get("task_id") in scheduled:
                copied["duration_hours"] = scheduled[copied["task_id"]].duration_hours
            rows.append(copied)
        self.store.sync_geometry_attributes(rows)

    def _toggle_play(self, playing):
        if playing:
            # Feature geometry may have changed since the task was linked.
            self.resolver.clear_cache()
            self.task_table._recompute()
            self.sim.play()
        else:
            self.sim.pause()

    def _label_setting_changed(self, key, checked):
        self.label_settings[key] = bool(checked)
        QSettings().setValue(LABEL_SETTING_PREFIX + key, bool(checked))
        if self.sim.current_time is not None:
            self._simulation_time_changed(self.sim.current_time)

    def _simulation_time_changed(self, when):
        result = self.task_table.schedule
        if result.span_start is None or result.span_end is None:
            return
        seconds = (result.span_end - result.span_start).total_seconds()
        fraction = 0.0 if seconds <= 0 else (when - result.span_start).total_seconds() / seconds
        self.slider.blockSignals(True)
        self.slider.setValue(round(min(1.0, max(0.0, fraction)) * 1000))
        self.slider.blockSignals(False)
        specs = {spec.task_id: spec for spec in self.task_table.task_specs()}
        states = position_at(result, specs, when)
        # Tint the rows of tasks under way at the playback time, once the
        # playhead is engaged (playing or scrubbed off the very start).
        engaged = self.sim.is_playing() or when > result.span_start
        self.task_table.set_active_tasks(
            {state.task_id for state in states.values() if state.active}
            if engaged else set())
        self.overlay.hide_resources_not_in(states)
        resources = {row.get("resource_id"): row for row in self.task_table.resources}
        active_descriptions = []
        for resource_id, state in states.items():
            task = self.task_table.row_by_id(state.task_id)
            if task is None:
                continue
            resource = resources.get(resource_id, {})
            color = resource.get("color_hex") or schema.DEFAULT_RESOURCE_COLOR
            scheduled = next((item for item in result.tasks if item.task_id == state.task_id), None)
            frame = self.resolver.route_frame(task) if task.get("geom_kind") == "line" else None
            label_text = _simulation_label(
                task, resource, state, scheduled, when, self.label_settings,
                frame.total_length_m / 1852.0 if frame is not None else None)
            if task.get("geom_kind") == "line":
                self.overlay.update_resource(
                    resource_id, frame, state.chainage_m, color,
                    task.get("direction") or "forward", label_text)
            elif task.get("geom_kind") == "point":
                self.overlay.show_point(
                    resource_id, self.resolver.point_at_chainage(task, 0), color, label_text)
            else:
                self.overlay.hold_resource(resource_id, color, label_text)
            if state.active:
                row = scheduled.row if scheduled else int(task.get("seq") or 0) + 1
                active_descriptions.append((row, task.get("name") or "Task", state.fraction))
        if active_descriptions:
            row, name, task_fraction = sorted(active_descriptions)[0]
            self.status_label.setText("Task %d/%d — %s | %s | %.0f%%" % (
                row, len(result.tasks), name, when.strftime("%d/%m/%Y %H:%M"), task_fraction * 100))
        else:
            self.status_label.setText(when.strftime("%d/%m/%Y %H:%M"))

    def _show_fuel_report(self):
        fuel = self.task_table.fuel
        resources = {row.get("resource_id"): row for row in self.task_table.resources}
        sections = []
        for resource_id, summary in fuel.by_resource.items():
            if not (summary.rob_start or summary.total_burn or summary.total_bunker):
                continue
            resource = resources.get(resource_id, {})
            unit = summary.unit or "t"
            lines = ["%s (%s):" % (resource.get("name") or "Resource", unit)]
            lines.append("  Start fuel: %s" % _fmt_fuel(summary.rob_start))
            lines.append("  Burned: %s" % _fmt_fuel(summary.total_burn))
            if summary.total_bunker:
                lines.append("  Bunkered: %s" % _fmt_fuel(summary.total_bunker))
            lines.append("  End ROB: %s (lowest %s)" % (
                _fmt_fuel(summary.rob_end), _fmt_fuel(summary.min_rob)))
            if summary.cost:
                lines.append("  Fuel cost: %s" % ("{:,.2f}".format(summary.cost)))
            for warning in summary.warnings:
                lines.append("  ⚠ %s" % warning)
            sections.append("\n".join(lines))
        if not sections:
            QMessageBox.information(
                self, "Fuel report",
                "No fuel data yet.\n\nSet fuel rates, start fuel, and optionally cost "
                "per unit in Resources…, then choose a fuel mode (Transit/DP/Anchor/"
                "Port) for each task. Enter a Bunker amount on port-call tasks to "
                "take fuel on.")
            return
        QMessageBox.information(self, "Fuel report", "\n\n".join(sections))

    def _copy_ms_project(self):
        result = self.task_table.schedule
        specs = {spec.task_id: spec for spec in self.task_table.task_specs()}
        resources = {row.get("resource_id"): row for row in self.task_table.resources}
        text = build_msp_tsv(result.tasks, specs, resources)
        QApplication.clipboard().setText(text)
        self.iface.messageBar().pushMessage(
            "Planner", "%d task(s) copied for MS Project." % len(result.tasks),
            level=MESSAGE_INFO, duration=4)

    def _top_level_changed(self, floating):
        if floating:
            self.setWindowFlags(
                WINDOW_TYPE_WINDOW | WINDOW_HINT_CUSTOMIZE | WINDOW_HINT_TITLE |
                WINDOW_HINT_MIN_MAX | WINDOW_HINT_CLOSE)
            self.show()

    def contextMenuEvent(self, event):
        if self.isFloating():
            from qgis.PyQt.QtWidgets import QMenu
            menu = QMenu(self)
            menu.addAction("Re-dock", lambda: self.setFloating(False))
            qt_exec(menu, event.globalPos())
        else:
            super().contextMenuEvent(event)

    def refresh(self):
        self.refresh_scenarios(self.current_scenario_id)

    def shutdown(self):
        self.sim.shutdown()
        self.pick_session.cancel()
        self.sketch_session.cancel(False)
        self.overlay.clear()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


class SketchTasksDialog(QDialog):
    def __init__(self, resources, point_count, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create tasks from sketch")
        self.resources = list(resources)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit("Sketched route")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("One task per leg (%d)" % max(0, point_count - 1), "legs")
        self.mode_combo.addItem("One task for the whole route", "whole")
        self.resource_combo = QComboBox()
        for resource in self.resources:
            self.resource_combo.addItem(resource.get("name") or "Resource",
                                        resource.get("resource_id") or "")
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.0, 100.0)
        self.speed_spin.setDecimals(3)
        self.speed_spin.setSuffix(" kn")
        self.waypoint_table = QTableWidget(point_count, 3)
        self.waypoint_table.setHorizontalHeaderLabels(["Create", "Waypoint task", "Duration (h)"])
        self.waypoint_table.setMaximumHeight(190)
        for row in range(point_count):
            create = QCheckBox()
            create.setChecked(True)
            create.setToolTip("Untick shape-only route vertices which should not become tasks.")
            self.waypoint_table.setCellWidget(row, 0, create)
            self.waypoint_table.setItem(row, 1, QTableWidgetItem("Waypoint %d" % (row + 1)))
            self.waypoint_table.setItem(row, 2, QTableWidgetItem("0"))
        self.resource_combo.currentIndexChanged.connect(self._resource_changed)
        self.mode_combo.currentIndexChanged.connect(
            lambda _index: self.waypoint_table.setEnabled(self.mode_combo.currentData() == "legs"))
        form.addRow("Base name:", self.name_edit)
        form.addRow("Create:", self.mode_combo)
        form.addRow("Resource:", self.resource_combo)
        form.addRow("Default speed:", self.speed_spin)
        form.addRow("Waypoint operations:", self.waypoint_table)
        layout.addLayout(form)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self._resource_changed()

    def _resource_changed(self, _index=None):
        resource_id = self.resource_combo.currentData()
        resource = next((row for row in self.resources
                         if row.get("resource_id") == resource_id), {})
        self.speed_spin.setValue(float(resource.get("default_speed_kn") or 0.0))

    def configuration(self):
        waypoint_tasks = {}
        if self.mode_combo.currentData() == "legs":
            for row in range(self.waypoint_table.rowCount()):
                checkbox = self.waypoint_table.cellWidget(row, 0)
                if checkbox is None or not checkbox.isChecked():
                    continue
                name_item = self.waypoint_table.item(row, 1)
                duration_item = self.waypoint_table.item(row, 2)
                try:
                    duration = max(0.0, float(duration_item.text()))
                except (AttributeError, TypeError, ValueError):
                    duration = 0.0
                waypoint_tasks[row] = {
                    "name": name_item.text().strip() if name_item and name_item.text().strip()
                    else "Waypoint %d" % (row + 1),
                    "duration_hours": duration,
                }
        return {
            "base_name": self.name_edit.text().strip() or "Sketched route",
            "mode": self.mode_combo.currentData(),
            "resource_id": self.resource_combo.currentData() or "",
            "speed_knots": self.speed_spin.value(),
            "waypoint_tasks": waypoint_tasks,
        }


class StandardTasksDialog(QDialog):
    """Curate the user's reusable task library and pick templates to insert."""

    COL_NAME = 0
    COL_DESCRIPTION = 1
    COL_DURATION = 2
    COL_SPEED = 3
    COL_FUEL = 4
    COL_BUNKER = 5
    COL_NOTES = 6

    def __init__(self, templates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Standard tasks")
        self.resize(940, 430)
        self.selected_templates = []
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Curate reusable task templates, stored per user across projects. "
            "Select rows and Insert to add them to the plan after the current "
            "task; inserted tasks are ordinary tasks and can be edited or "
            "linked to geometry as usual.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Name", "Description", "Duration (h)", "Speed (kn)", "Fuel",
            "Bunker", "Notes"])
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.table.setColumnWidth(self.COL_NAME, 160)
        self.table.setColumnWidth(self.COL_DESCRIPTION, 260)
        self.table.setColumnWidth(self.COL_DURATION, 90)
        self.table.setColumnWidth(self.COL_SPEED, 85)
        self.table.setColumnWidth(self.COL_FUEL, 85)
        self.table.setColumnWidth(self.COL_BUNKER, 75)
        self.table.horizontalHeaderItem(self.COL_SPEED).setToolTip(
            "Optional speed for route tasks; duration becomes computed once a "
            "line is linked.")
        self.table.horizontalHeaderItem(self.COL_FUEL).setToolTip(
            "Fuel mode the inserted task burns (per the resource's fuel profile).")
        self.table.horizontalHeaderItem(self.COL_BUNKER).setToolTip(
            "Fuel taken on during the task, credited at the task finish.")
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        for label, slot in (("Add", lambda: self._add_row()),
                            ("Remove", self._remove_selected),
                            ("Import CSV…", self._import_csv),
                            ("Export CSV…", self._export_csv)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        insert_btn = QPushButton("Insert selected into plan")
        insert_btn.setDefault(True)
        insert_btn.clicked.connect(self._insert)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(insert_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)
        for template in templates:
            self._add_row(template)
        _polish_dialog_table(self.table)

    def _add_row(self, template=None):
        template = template or {"name": "New standard task", "description": "",
                                "duration_hours": 1.0, "speed_knots": None,
                                "fuel_mode": "", "bunker_amount": None, "notes": ""}
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column, key in ((self.COL_NAME, "name"),
                            (self.COL_DESCRIPTION, "description"),
                            (self.COL_NOTES, "notes")):
            self.table.setItem(row, column, QTableWidgetItem(str(template.get(key) or "")))
        for column, key in ((self.COL_DURATION, "duration_hours"),
                            (self.COL_SPEED, "speed_knots"),
                            (self.COL_BUNKER, "bunker_amount")):
            value = template.get(key)
            text = "" if value in (None, "") else (
                ("%.4f" % float(value)).rstrip("0").rstrip(".") or "0")
            self.table.setItem(row, column, QTableWidgetItem(text))
        combo = QComboBox()
        for value, label in schema.FUEL_MODES:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(template.get("fuel_mode") or "")))
        self.table.setCellWidget(row, self.COL_FUEL, combo)

    def _remove_selected(self):
        for row in sorted({index.row() for index in
                           self.table.selectionModel().selectedRows()}, reverse=True):
            self.table.removeRow(row)

    def _template_at(self, row):
        name_item = self.table.item(row, self.COL_NAME)
        name = name_item.text().strip() if name_item else ""
        if not name:
            return None
        combo = self.table.cellWidget(row, self.COL_FUEL)
        return {
            "name": name,
            "description": self._text(row, self.COL_DESCRIPTION),
            "duration_hours": self._number(row, self.COL_DURATION),
            "speed_knots": self._number(row, self.COL_SPEED),
            "fuel_mode": (combo.currentData() if combo else "") or "",
            "bunker_amount": self._number(row, self.COL_BUNKER),
            "notes": self._text(row, self.COL_NOTES),
        }

    def _text(self, row, column):
        item = self.table.item(row, column)
        return item.text().strip() if item else ""

    def _number(self, row, column):
        try:
            text = self.table.item(row, column).text().strip()
            return float(text) if text else None
        except (AttributeError, TypeError, ValueError):
            return None

    def templates(self):
        rows = [self._template_at(row) for row in range(self.table.rowCount())]
        return [template for template in rows if template is not None]

    def _insert(self):
        selected = sorted({index.row() for index in
                           self.table.selectionModel().selectedRows()})
        templates = [self._template_at(row) for row in selected]
        self.selected_templates = [t for t in templates if t is not None]
        if not self.selected_templates:
            QMessageBox.information(self, "Standard tasks",
                                    "Select one or more standard tasks to insert.")
            return
        self.accept()

    def _import_csv(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import standard tasks", "", "CSV files (*.csv);;All files (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                text = handle.read()
        except OSError as exc:
            QMessageBox.warning(self, "Import standard tasks",
                                "Could not read the file: %s" % exc)
            return
        imported, warnings = standard_tasks.templates_from_csv_text(text)
        if not imported:
            QMessageBox.warning(self, "Import standard tasks",
                                "\n".join(warnings) or "No standard tasks found in the file.")
            return
        for template in imported:
            self._add_row(template)
        message = "%d standard task(s) added to the library." % len(imported)
        if warnings:
            message += "\n\n" + "\n".join(warnings[:8])
        QMessageBox.information(self, "Import standard tasks", message)

    def _export_csv(self):
        templates = self.templates()
        if not templates:
            QMessageBox.information(self, "Export standard tasks",
                                    "There are no standard tasks to export.")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export standard tasks", "standard_tasks.csv",
            "CSV files (*.csv);;All files (*.*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write(standard_tasks.templates_to_csv_text(templates))
        except OSError as exc:
            QMessageBox.warning(self, "Export standard tasks",
                                "Could not write the file: %s" % exc)
            return
        QMessageBox.information(self, "Export standard tasks",
                                "%d standard task(s) exported to:\n%s"
                                % (len(templates), path))


class ResourceDialog(QDialog):
    def __init__(self, resources, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Project resources")
        self.resize(1180, 380)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels([
            "Name", "Kind", "Colour", "Default speed (kn)", "Start offset (h)",
            "Fuel unit", "Transit", "DP", "Anchor", "Port", "Start fuel",
            "Cost / unit"])
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 110)
        self.table.setColumnWidth(5, 80)
        for column in (6, 7, 8, 9, 10, 11):
            self.table.setColumnWidth(column, 78)
        self.table.horizontalHeaderItem(4).setToolTip(
            "Earliest availability relative to the scenario anchor; useful for multi-vessel plans.")
        self.table.horizontalHeaderItem(5).setToolTip(
            "Unit for all fuel figures on this resource: tonnes or cubic metres.")
        for column, label in ((6, "transit"), (7, "DP"), (8, "anchor"), (9, "in port")):
            self.table.horizontalHeaderItem(column).setToolTip(
                "Fuel burned per 24 h while %s, in the fuel unit. Tasks pick which "
                "rate applies via their Fuel column." % label)
        self.table.horizontalHeaderItem(10).setToolTip(
            "Fuel on board at the scenario start (remaining-on-board), in the fuel unit.")
        self.table.horizontalHeaderItem(11).setToolTip(
            "Optional cost per fuel unit; used for the fuel cost total in the fuel report.")
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: self.table.removeRow(self.table.currentRow())
                               if self.table.currentRow() >= 0 else None)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        for resource in resources:
            self._add(resource)
        _polish_dialog_table(self.table)

    NUMBER_COLUMNS = (
        (3, "default_speed_kn", None), (4, "start_offset_hours", 0.0),
        (6, "fuel_rate_transit", 0.0), (7, "fuel_rate_dp", 0.0),
        (8, "fuel_rate_anchor", 0.0), (9, "fuel_rate_port", 0.0),
        (10, "fuel_start", 0.0), (11, "fuel_cost_per_unit", 0.0),
    )

    def _add(self, resource=None):
        resource = resource or {"resource_id": schema.new_id(), "name": "Vessel",
                                "kind": "vessel", "color_hex": "#1f78b4",
                                "default_speed_kn": 1.0, "start_offset_hours": 0.0,
                                "fuel_unit": schema.DEFAULT_FUEL_UNIT,
                                "fuel_rate_transit": 0.0, "fuel_rate_dp": 0.0,
                                "fuel_rate_anchor": 0.0, "fuel_rate_port": 0.0,
                                "fuel_start": 0.0, "fuel_cost_per_unit": 0.0,
                                "notes": ""}
        row = self.table.rowCount()
        self.table.insertRow(row)
        name_item = QTableWidgetItem(str(resource.get("name", "")))
        name_item.setData(ITEM_DATA_USER_ROLE, resource.get("resource_id") or schema.new_id())
        self.table.setItem(row, 0, name_item)
        for column, key, _minimum in self.NUMBER_COLUMNS:
            self.table.setItem(row, column, QTableWidgetItem(str(resource.get(key, "") or 0)))
        colour = str(resource.get("color_hex") or schema.DEFAULT_RESOURCE_COLOR)
        colour_btn = QPushButton(colour)
        colour_btn.setProperty("color_hex", colour)
        colour_btn.setToolTip("Choose the marker and label colour for this resource.")
        self._style_colour_button(colour_btn, colour)
        colour_btn.clicked.connect(lambda _checked=False, button=colour_btn: self._choose_colour(button))
        self.table.setCellWidget(row, 2, colour_btn)
        kind_combo = QComboBox()
        for label, value in (("Vessel", "vessel"), ("Party", "party"), ("Equipment", "equipment")):
            kind_combo.addItem(label, value)
        kind_combo.setCurrentIndex(max(0, kind_combo.findData(resource.get("kind") or "vessel")))
        self.table.setCellWidget(row, 1, kind_combo)
        unit_combo = QComboBox()
        for label, value in (("t", "t"), ("m³", "m3")):
            unit_combo.addItem(label, value)
        unit_combo.setCurrentIndex(max(0, unit_combo.findData(
            resource.get("fuel_unit") or schema.DEFAULT_FUEL_UNIT)))
        self.table.setCellWidget(row, 5, unit_combo)

    def _cell_float(self, row, column, minimum=None):
        try:
            value = float(self.table.item(row, column).text())
        except (AttributeError, TypeError, ValueError):
            return 0.0
        return value if minimum is None else max(minimum, value)

    def resources(self):
        rows = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            name = name_item.text().strip() if name_item else ""
            if not name:
                continue
            colour_btn = self.table.cellWidget(row, 2)
            unit_combo = self.table.cellWidget(row, 5)
            item = {
                "resource_id": name_item.data(ITEM_DATA_USER_ROLE) or schema.new_id(),
                "name": name, "kind": self.table.cellWidget(row, 1).currentData() or "vessel",
                "color_hex": str(colour_btn.property("color_hex") if colour_btn else "")
                or schema.DEFAULT_RESOURCE_COLOR,
                "fuel_unit": (unit_combo.currentData() if unit_combo else "")
                or schema.DEFAULT_FUEL_UNIT,
                "seq": row, "notes": "",
            }
            for column, key, minimum in self.NUMBER_COLUMNS:
                item[key] = self._cell_float(row, column, minimum)
            rows.append(item)
        return rows

    def _choose_colour(self, button):
        current = QColor(str(button.property("color_hex") or schema.DEFAULT_RESOURCE_COLOR))
        colour = QColorDialog.getColor(current, self, "Resource marker colour")
        if not colour.isValid():
            return
        value = colour.name()
        button.setProperty("color_hex", value)
        button.setText(value)
        self._style_colour_button(button, value)

    @staticmethod
    def _style_colour_button(button, value):
        colour = QColor(value)
        text = "#ffffff" if colour.lightness() < 128 else "#202020"
        button.setStyleSheet(
            "QPushButton { background-color: %s; color: %s; padding: 2px 6px; }" %
            (colour.name(), text))


class FeatureLinkDialog(QDialog):
    def __init__(self, canvas, pick_session, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Link task to map feature")
        self.canvas = canvas
        self.pick_session = pick_session
        self.reference = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(MAP_LAYER_FILTER_POINT | MAP_LAYER_FILTER_LINE)
        self.feature_combo = QComboBox()
        form.addRow("Layer:", self.layer_combo)
        form.addRow("Feature:", self.feature_combo)
        layout.addLayout(form)
        pick = QPushButton("Pick on map")
        pick.clicked.connect(self._pick)
        layout.addWidget(pick)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.accepted.connect(self._accept_selected)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.layer_combo.layerChanged.connect(self._populate_features)
        self._populate_features(self.layer_combo.currentLayer())

    def _populate_features(self, layer):
        self.feature_combo.clear()
        if layer is None:
            return
        expression = QgsExpression(layer.displayExpression() or "$id")
        context = QgsExpressionContext()
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
        for index, feature in enumerate(layer.getFeatures()):
            if index >= 500:
                break
            context.setFeature(feature)
            label = expression.evaluate(context)
            self.feature_combo.addItem(str(label if label not in (None, "") else feature.id()), feature.id())

    def _accept_selected(self):
        layer = self.layer_combo.currentLayer()
        if layer is None or self.feature_combo.currentIndex() < 0:
            return
        feature = layer.getFeature(int(self.feature_combo.currentData()))
        self.reference = feature_reference(layer, feature, self.feature_combo.currentText())
        self.accept()

    def _pick(self):
        layer = self.layer_combo.currentLayer()
        if layer is None:
            return
        self.hide()

        def picked(picked_layer, feature):
            expression = QgsExpression(picked_layer.displayExpression() or "$id")
            context = QgsExpressionContext()
            context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(picked_layer))
            context.setFeature(feature)
            label = expression.evaluate(context)
            self.reference = feature_reference(picked_layer, feature, label)
            self.accept()

        self.pick_session.start(layer, picked)

    def reject(self):
        self.pick_session.cancel()
        super().reject()


def _polish_dialog_table(table, minimum=70, maximum=340):
    """Content-sized default column widths, still user-adjustable and movable."""
    table.horizontalHeader().setSectionsMovable(True)
    table.resizeColumnsToContents()
    for column in range(table.columnCount()):
        table.setColumnWidth(column, max(minimum, min(table.columnWidth(column), maximum)))


def _fmt_fuel(value):
    return ("%.2f" % float(value or 0.0)).rstrip("0").rstrip(".") or "0"


def _parse_anchor(value):
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%dT%H:%M")
    except ValueError:
        return datetime.now().replace(second=0, microsecond=0)


def _setting_bool(key, default):
    value = QSettings().value(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _simulation_label(task, resource, state, scheduled, when, settings, distance_nm=None):
    if not settings.get("show", True):
        return ""
    heading = []
    if settings.get("task_number"):
        heading.append("#%d" % (
            scheduled.row if scheduled is not None else int(task.get("seq") or 0) + 1))
    if settings.get("task_name", True):
        heading.append(task.get("name") or "Task")
    lines = [" ".join(heading)] if heading else []
    if settings.get("resource"):
        lines.append(resource.get("name") or "Unassigned resource")
    if settings.get("progress"):
        lines.append("Progress %.0f%%" % (float(state.fraction or 0.0) * 100.0))
    if settings.get("clock"):
        lines.append(when.strftime("%d/%m/%Y %H:%M"))
    if settings.get("speed_distance") and task.get("geom_kind") == "line":
        details = []
        speed = task.get("speed_knots")
        if speed not in (None, ""):
            details.append("%s kn" % ("%.3f" % float(speed)).rstrip("0").rstrip("."))
        if distance_nm is not None:
            details.append("%s nm" % ("%.3f" % float(distance_nm)).rstrip("0").rstrip("."))
        if details:
            lines.append(" / ".join(details))
    return "\n".join(line for line in lines if line)


def _new_task_row(name, resource_id, seq, speed_knots, duration_hours,
                  predecessor_task_id="", computed=False):
    now = schema.utc_now_iso()
    return {
        "task_id": schema.new_id(), "seq": int(seq), "name": name or "Task",
        "description": "", "is_phase": 0, "outline_level": 0,
        "resource_id": resource_id or "",
        "duration_mode": "computed" if computed else "manual",
        "duration_hours": float(duration_hours or 0.0),
        "predecessor_task_id": predecessor_task_id or "", "lag_hours": 0.0,
        "speed_knots": float(speed_knots or 0.0) if speed_knots is not None else None,
        "direction": "forward", "fuel_mode": "", "bunker_amount": None,
        "layer_id": "", "layer_source": "",
        "layer_name": "", "feature_id": "", "feature_label": "",
        "geom_kind": "", "linked_ref_json": "", "created_utc": now,
        "modified_utc": now, "notes": "",
    }


def _owned_geometry(task):
    return bool(_owned_geometry_metadata(task).get("owned_geometry"))


def _owned_geometry_metadata(task):
    try:
        return json.loads(str(task.get("linked_ref_json") or "{}"))
    except (TypeError, ValueError):
        return {}
