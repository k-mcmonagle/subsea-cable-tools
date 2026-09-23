# -*- coding: utf-8 -*-
"""Inputs tab — RPL, scope, direction, target burial depth, bathymetry and
registered inputs.

The scope spinboxes are the cost control for long routes: analysis is
limited to the scoped KP range (stated plainly in the UI). The target burial
depth has a plan default plus optional KP-range overrides (see
``target_depth.py``). Registered inputs become the only selectable sources
inside rule configs (stable ``input_id`` indirection), each carrying
optional Input Data Register metadata (originator, revision, status,
quality); *Add inputs…* registers many layers at once, and the table shows
whether each input still resolves (and which criteria use it) so a removed
or re-added layer is visible instead of silently breaking a rule.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.core import QgsProject
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    DIALOG_ACCEPTED,
    HEADER_RESIZE_MODE_CONTENTS,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MAP_LAYER_FILTER_LINE,
    MAP_LAYER_FILTER_POINT,
    MAP_LAYER_FILTER_POLYGON,
    MAP_LAYER_FILTER_VECTOR,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
    layer_filters,
    qt_exec,
)
from ...workbench.project_layers import normalised_path
from .. import schema, target_depth
from .. import ui_helpers
from .input_picker import AddInputsDialog

_ROLE_FILTERS = {
    schema.INPUT_ROLE_CROSSINGS_POINTS: (MAP_LAYER_FILTER_POINT,),
    schema.INPUT_ROLE_CROSSINGS_LINES: (MAP_LAYER_FILTER_LINE,),
    schema.INPUT_ROLE_SOILS: (MAP_LAYER_FILTER_POLYGON,),
    schema.INPUT_ROLE_OTHER: (MAP_LAYER_FILTER_VECTOR,),
}

_RPL_REVISION_ROLE = int(ITEM_DATA_USER_ROLE) + 1

_INPUT_COLUMNS = ["Role", "Layer", "Status", "Used by", "Originator",
                  "Revision", "Quality"]
_TARGET_COLUMNS = ["Start KP", "End KP", "Target (m)", "Notes"]
_STATUS_TEXT = {
    "ok": "✓ in project",
    "relinked": "✓ matched by source",
    "file": "⚠ not in project",
    "missing": "✗ missing",
}


class InputDialog(QDialog):
    """Register one input layer with optional register metadata."""

    def __init__(self, row: Optional[Dict] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Register input")
        self.setMinimumWidth(420)
        self.row = dict(row or {})
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.role_combo = QComboBox()
        for role in schema.INPUT_ROLES:
            if role == schema.INPUT_ROLE_BATHY:
                continue  # bathymetry has its own editor
            self.role_combo.addItem(schema.INPUT_ROLE_LABELS[role], role)
        self.layer_combo = QgsMapLayerComboBox()
        self.role_combo.currentIndexChanged.connect(self._sync_filter)
        form.addRow("Role:", self.role_combo)
        form.addRow("Layer:", self.layer_combo)
        layout.addLayout(form)

        details = QGroupBox("Input Data Register details (optional)")
        details.setCheckable(True)
        details.setChecked(bool(self.row.get("originator") or self.row.get("revision")))
        detail_form = QFormLayout(details)
        self.originator_edit = QLineEdit(self.row.get("originator") or "")
        self.revision_edit = QLineEdit(self.row.get("revision") or "")
        self.status_combo = QComboBox()
        self.status_combo.addItems(["current", "superseded"])
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["", "high", "moderate", "low", "insufficient"])
        self.notes_edit = QLineEdit(self.row.get("notes") or "")
        detail_form.addRow("Originator:", self.originator_edit)
        detail_form.addRow("Revision:", self.revision_edit)
        detail_form.addRow("Status:", self.status_combo)
        detail_form.addRow("Quality:", self.quality_combo)
        detail_form.addRow("Notes:", self.notes_edit)
        layout.addWidget(details)
        self.details = details

        buttons = QDialogButtonBox()
        from ...qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK

        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        index = self.role_combo.findData(self.row.get("role"))
        if index >= 0:
            self.role_combo.setCurrentIndex(index)
        self._sync_filter()
        if self.row.get("layer_id_hint") or self.row.get("layer_source"):
            # Editing an existing registration: start from the layer the
            # row is bound to — otherwise the combo silently defaults to
            # the first matching project layer and OK re-points the input.
            from .. import map_layers

            current = map_layers.resolve_input_layer(QgsProject.instance(),
                                                     self.row)
            if current is not None \
                    and QgsProject.instance().mapLayer(current.id()) is not None:
                self.layer_combo.setLayer(current)
        if self.row.get("status") == "superseded":
            self.status_combo.setCurrentIndex(1)
        quality_index = self.quality_combo.findText(self.row.get("quality") or "")
        if quality_index >= 0:
            self.quality_combo.setCurrentIndex(quality_index)

    def _sync_filter(self) -> None:
        role = self.role_combo.currentData()
        members = _ROLE_FILTERS.get(role, (MAP_LAYER_FILTER_VECTOR,))
        self.layer_combo.setFilters(layer_filters(*members))

    def _accept(self) -> None:
        if self.layer_combo.currentLayer() is None:
            QMessageBox.warning(
                self, "Burial Planner",
                "Pick a layer to register — no matching layer is selected "
                "(the list is filtered by the chosen role).")
            return
        self.accept()

    def result_row(self) -> Optional[Dict]:
        layer = self.layer_combo.currentLayer()
        if layer is None:
            return None
        row = dict(self.row)
        row.update({
            "role": self.role_combo.currentData(),
            "layer_name": layer.name(),
            "layer_source": layer.source(),
            "layer_id_hint": layer.id(),
            "config_json": row.get("config_json") or "{}",
            "originator": self.originator_edit.text() if self.details.isChecked() else "",
            "revision": self.revision_edit.text() if self.details.isChecked() else "",
            "status": self.status_combo.currentText(),
            "received_utc": row.get("received_utc") or schema.utc_now_iso(),
            "quality": self.quality_combo.currentText(),
            "notes": self.notes_edit.text(),
        })
        return row


class InputsTab(QWidget):
    def __init__(self, model, workbench_store_fn, dock=None, parent=None):
        super().__init__(parent)
        self.model = model
        self.workbench_store_fn = workbench_store_fn  # () -> WorkbenchStore | None
        self.dock = dock  # BurialPlannerDock, for KP map picking
        self._loading = False

        # Keep forms at a readable measure when the floating dock is maximised
        # on a wide monitor. Tables still get a useful 1050 px working width,
        # while labels and selectors no longer stretch across the whole
        # screen. The scroll area keeps everything reachable on short docks.
        tab_layout = QVBoxLayout(self)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame
                             if hasattr(QFrame, "Shape") else QFrame.NoFrame)
        tab_layout.addWidget(scroll)
        scroll_body = QWidget()
        scroll.setWidget(scroll_body)
        outer = QHBoxLayout(scroll_body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        content = QWidget()
        content.setMaximumWidth(1050)
        layout = QVBoxLayout(content)
        outer.addWidget(content, 4)
        outer.addStretch(1)

        # -- RPL --------------------------------------------------------------
        rpl_box = QGroupBox("Route (RPL)")
        rpl_form = QFormLayout(rpl_box)
        self.active_route_label = QLabel("—")
        self.active_route_label.setWordWrap(True)
        self.active_route_label.setToolTip(
            "The route the plan currently uses — set with one of the "
            "buttons below (the last one applied wins).")
        rpl_form.addRow("Active route:", self.active_route_label)
        rpl_row = QHBoxLayout()
        self.rpl_combo = QComboBox()
        self.rpl_combo.setToolTip(
            "RPLs registered in this project's Cable Workbench. Set route "
            "anchors the plan to the selected RPL and its revision.")
        self.rpl_combo.currentIndexChanged.connect(self._update_rpl_revision_preview)
        rpl_row.addWidget(self.rpl_combo, 1)
        refresh_rpls_button = QPushButton("⟳")
        refresh_rpls_button.setMaximumWidth(28)
        refresh_rpls_button.setToolTip(
            "Reload the Workbench RPL list (e.g. after registering a new "
            "RPL in the Workbench).")
        refresh_rpls_button.clicked.connect(self._refresh_rpls)
        rpl_row.addWidget(refresh_rpls_button)
        self.apply_rpl_button = QPushButton("Set route")
        self.apply_rpl_button.setToolTip(
            "Anchor the plan to the selected Workbench RPL.")
        self.apply_rpl_button.clicked.connect(self._apply_rpl)
        rpl_row.addWidget(self.apply_rpl_button)
        rpl_form.addRow("Workbench RPL:", rpl_row)
        self.rpl_revision_label = QLabel("—")
        self.rpl_revision_label.setToolTip(
            "Revision label stored on the RPL selected in the list above "
            "(a preview — Set route applies it).")
        rpl_form.addRow("Selected RPL revision:", self.rpl_revision_label)
        fallback_row = QHBoxLayout()
        self.fallback_combo = QgsMapLayerComboBox()
        self.fallback_combo.setFilters(layer_filters(MAP_LAYER_FILTER_LINE))
        self.fallback_combo.setToolTip(
            "Fallback: any project line layer in Workbench RPL format.")
        fallback_row.addWidget(self.fallback_combo, 1)
        self.apply_fallback_button = QPushButton("Use line layer")
        self.apply_fallback_button.setToolTip(
            "Any project line layer in Workbench RPL format. Registering the "
            "route in the Workbench is recommended.")
        self.apply_fallback_button.clicked.connect(self._apply_fallback)
        fallback_row.addWidget(self.apply_fallback_button)
        rpl_form.addRow("Or line layer:", fallback_row)
        layout.addWidget(rpl_box)

        # -- scope + direction ------------------------------------------------
        scope_box = QGroupBox("Scope and direction")
        scope_form = QFormLayout(scope_box)
        self.scope_start = QDoubleSpinBox()
        self.scope_end = QDoubleSpinBox()
        for spin in (self.scope_start, self.scope_end):
            spin.setDecimals(3)
            spin.setRange(0.0, 100000.0)
            spin.setSuffix(" km")
            spin.setToolTip(
                "Analysis and profile sampling are limited to this KP "
                "range. Apply scope / direction saves it.")
        # Two rows so the controls still fit a narrow docked width.
        start_row = QHBoxLayout()
        start_row.addWidget(self.scope_start, 1)
        self.scope_pick_start = QPushButton("Pick…")
        self.scope_pick_start.clicked.connect(
            lambda: self._pick_scope_kp(self.scope_start, "start"))
        start_row.addWidget(self.scope_pick_start)
        scope_form.addRow("Scope from KP:", start_row)
        end_row = QHBoxLayout()
        end_row.addWidget(self.scope_end, 1)
        self.scope_pick_end = QPushButton("Pick…")
        self.scope_pick_end.clicked.connect(
            lambda: self._pick_scope_kp(self.scope_end, "end"))
        end_row.addWidget(self.scope_pick_end)
        self.full_route_button = QPushButton("Full route")
        self.full_route_button.setToolTip(
            "Set the scope to the whole route (KP 0 to route end).")
        self.full_route_button.clicked.connect(self._full_route)
        end_row.addWidget(self.full_route_button)
        scope_form.addRow("to KP:", end_row)
        for button in (self.scope_pick_start, self.scope_pick_end):
            button.setToolTip(
                "Pick the KP by clicking the route on the map (right-click "
                "or Esc cancels). Apply scope / direction saves it.")
        self.direction_combo = QComboBox()
        self.direction_combo.addItem("A → B (with increasing KP)", 1)
        self.direction_combo.addItem("B → A (against KP)", -1)
        self.direction_combo.setToolTip(
            "Direction of installation. Approach/departure semantics "
            "(Exclusion Area extensions, influence zones, signed slope) "
            "follow it.")
        scope_form.addRow("Direction of installation:", self.direction_combo)
        scope_note = QLabel(
            "Analysis is limited to the scoped KP range — on long routes, "
            "scope is the run-time control.")
        scope_note.setWordWrap(True)
        scope_form.addRow(scope_note)
        self.apply_scope_button = QPushButton("Apply scope / direction")
        self.apply_scope_button.setToolTip(
            "Save the scope and direction to the plan.")
        self.apply_scope_button.clicked.connect(self._apply_scope)
        scope_form.addRow(self.apply_scope_button)
        layout.addWidget(scope_box)

        # -- target burial depth ----------------------------------------------
        target_box = QGroupBox("Target burial depth")
        target_layout = QVBoxLayout(target_box)
        target_form = QFormLayout()
        self.target_burial = QDoubleSpinBox()
        self.target_burial.setDecimals(2)
        self.target_burial.setRange(0.0, 20.0)
        self.target_burial.setSuffix(" m")
        self.target_burial.setSpecialValueText("None")
        self.target_burial.setToolTip(
            "Target depth of lowering below seabed wherever no KP range "
            "below sets another. Used by the Ground Model (target horizon), "
            "Plan Builder, exports and the report.")
        target_form.addRow("Default target:", self.target_burial)
        target_layout.addLayout(target_form)
        target_note = QLabel(
            "Different targets on parts of the route (e.g. deeper through a "
            "shipping lane or anchorage): add KP ranges — a range overrides "
            "the default inside it. Ranges may not overlap.")
        target_note.setWordWrap(True)
        target_note.setStyleSheet(ui_helpers.hint_style())
        target_layout.addWidget(target_note)
        self.target_table = QTableWidget(0, len(_TARGET_COLUMNS))
        self.target_table.setHorizontalHeaderLabels(_TARGET_COLUMNS)
        self.target_table.verticalHeader().setVisible(False)
        self.target_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.target_table.horizontalHeader().setSectionResizeMode(
            len(_TARGET_COLUMNS) - 1, HEADER_RESIZE_MODE_STRETCH)
        self.target_table.setMinimumHeight(90)
        self.target_table.setMaximumHeight(180)
        self.target_table.itemChanged.connect(
            lambda *_a: self._mark_dirty("target"))
        target_layout.addWidget(self.target_table)
        target_buttons = QHBoxLayout()
        self.add_target_button = QPushButton("Add range")
        self.add_target_button.setToolTip(
            "Add a KP range (starts after the last range; edit the cells).")
        self.add_target_button.clicked.connect(self._add_target_row)
        target_buttons.addWidget(self.add_target_button)
        self.pick_target_button = QPushButton("Pick KPs…")
        self.pick_target_button.setToolTip(
            "Click the route twice on the map to add a range from the two "
            "KPs (right-click or Esc cancels).")
        self.pick_target_button.clicked.connect(self._pick_target_range)
        target_buttons.addWidget(self.pick_target_button)
        self.remove_target_button = QPushButton("Remove")
        self.remove_target_button.clicked.connect(self._remove_target_rows)
        target_buttons.addWidget(self.remove_target_button)
        self.import_target_button = QPushButton("From RPL…")
        self.import_target_button.setToolTip(
            "Fill the ranges from the TargetBurialDepth of the plan's "
            "Workbench RPL legs (replaces the ranges below).")
        self.import_target_button.clicked.connect(self._import_rpl_targets)
        target_buttons.addWidget(self.import_target_button)
        target_buttons.addStretch(1)
        self.apply_target_button = QPushButton("Apply targets")
        self.apply_target_button.setToolTip(
            "Save the default target and the KP ranges to the plan.")
        self.apply_target_button.clicked.connect(self._apply_targets)
        target_buttons.addWidget(self.apply_target_button)
        target_layout.addLayout(target_buttons)
        self.target_summary = QLabel("")
        self.target_summary.setWordWrap(True)
        target_layout.addWidget(self.target_summary)
        layout.addWidget(target_box)

        # -- bathymetry -------------------------------------------------------
        bathy_box = QGroupBox("Bathymetry source")
        bathy_form = QFormLayout(bathy_box)
        manual_note = QLabel(
            "Select bathymetry specifically for this burial plan. Workbench "
            "RPL depth sources are not inherited.")
        manual_note.setWordWrap(True)
        bathy_form.addRow(manual_note)
        self.bathy_summary = QLabel("")
        self.bathy_summary.setWordWrap(True)
        bathy_form.addRow("Active source:", self.bathy_summary)
        self.bathy_relink_button = QPushButton("Save relinked layers")
        self.bathy_relink_button.setToolTip(
            "The saved bathymetry layer id is no longer in the project but a "
            "layer with the same source is — save the new link.")
        self.bathy_relink_button.clicked.connect(self._save_bathy_relink)
        self.bathy_relink_button.setVisible(False)
        bathy_form.addRow(self.bathy_relink_button)
        from ...qgis_compat import MAP_LAYER_FILTER_RASTER

        self.manual_source_combo = QComboBox()
        self.manual_source_combo.addItem("Raster layer", 1)
        self.manual_source_combo.addItem("Depth contours (up to two layers)", 2)
        self.manual_source_combo.currentIndexChanged.connect(self._sync_bathy_enabled)
        self.manual_source_combo.setToolTip(
            "Choose exactly one source type. The other controls are disabled "
            "so raster and contour inputs cannot be mixed accidentally.")
        self.raster_combo = QgsMapLayerComboBox()
        self.raster_combo.setFilters(layer_filters(MAP_LAYER_FILTER_RASTER))
        self.raster_combo.setAllowEmptyLayer(True)
        self.raster_band = QSpinBox()
        self.raster_band.setRange(1, 99)
        self.contour_combo = QgsMapLayerComboBox()
        self.contour_combo.setFilters(layer_filters(MAP_LAYER_FILTER_LINE))
        self.contour_combo.setAllowEmptyLayer(True)
        self.contour_field = QgsFieldComboBox()
        self.contour_combo.layerChanged.connect(self.contour_field.setLayer)
        self.contour_combo2 = QgsMapLayerComboBox()
        self.contour_combo2.setFilters(layer_filters(MAP_LAYER_FILTER_LINE))
        self.contour_combo2.setAllowEmptyLayer(True)
        self.contour_field2 = QgsFieldComboBox()
        self.contour_combo2.layerChanged.connect(self.contour_field2.setLayer)
        self.search_radius = QDoubleSpinBox()
        self.search_radius.setRange(1.0, 100000.0)
        self.search_radius.setSuffix(" m")
        self.search_radius.setValue(500.0)
        for widget in (self.contour_combo, self.contour_combo2):
            widget.setToolTip(
                "Depth contour line layer. When two layers are given (e.g. "
                "minor and major contours) their crossings are merged into "
                "one profile; depths are interpolated between the actual "
                "route crossings.")
        bathy_form.addRow("Manual source type:", self.manual_source_combo)
        bathy_form.addRow("Raster:", self.raster_combo)
        bathy_form.addRow("Band:", self.raster_band)
        bathy_form.addRow("Contour layer 1:", self.contour_combo)
        bathy_form.addRow("Depth field 1:", self.contour_field)
        bathy_form.addRow("Contour layer 2 (optional):", self.contour_combo2)
        bathy_form.addRow("Depth field 2:", self.contour_field2)
        bathy_form.addRow("Contour search radius:", self.search_radius)
        self.apply_bathy_button = QPushButton("Apply source")
        self.apply_bathy_button.setToolTip(
            "Save this bathymetry source configuration to the plan.")
        self.apply_bathy_button.clicked.connect(self._apply_bathy)
        bathy_form.addRow(self.apply_bathy_button)
        layout.addWidget(bathy_box)

        # -- other inputs -----------------------------------------------------
        inputs_box = QGroupBox("Registered inputs")
        inputs_layout = QVBoxLayout(inputs_box)
        filter_row = QHBoxLayout()
        self.inputs_filter = QLineEdit()
        self.inputs_filter.setPlaceholderText("Filter inputs…")
        self.inputs_filter.setClearButtonEnabled(True)
        self.inputs_filter.textChanged.connect(self._apply_inputs_filter)
        filter_row.addWidget(self.inputs_filter, 1)
        self.inputs_summary = QLabel("")
        filter_row.addWidget(self.inputs_summary)
        inputs_layout.addLayout(filter_row)
        self.inputs_table = QTableWidget(0, len(_INPUT_COLUMNS))
        self.inputs_table.setHorizontalHeaderLabels(_INPUT_COLUMNS)
        header = self.inputs_table.horizontalHeader()
        for column in range(len(_INPUT_COLUMNS)):
            header.setSectionResizeMode(column, HEADER_RESIZE_MODE_CONTENTS)
        header.setSectionResizeMode(1, HEADER_RESIZE_MODE_STRETCH)
        status_header = self.inputs_table.horizontalHeaderItem(2)
        if status_header is not None:
            status_header.setToolTip(
                "Whether the input still resolves: ✓ the registered project "
                "layer (or a project layer with the same source), ⚠ not in "
                "the project but readable from its file, ✗ missing — criteria "
                "using it report a missing input until it is relinked.")
        self.inputs_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.inputs_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.inputs_table.verticalHeader().setVisible(False)
        self.inputs_table.setSortingEnabled(False)
        self.inputs_table.doubleClicked.connect(lambda _i: self._edit_input())
        inputs_layout.addWidget(self.inputs_table, 1)
        self.inputs_table.itemSelectionChanged.connect(
            self._sync_input_buttons)
        button_row = QHBoxLayout()
        self.add_input_button = QPushButton("Add inputs…")
        self.add_input_button.setToolTip(
            "Pick one or many project layers (search, geometry filter, "
            "Layers-panel selection) and register them with roles and "
            "register details in one go.")
        self.add_input_button.clicked.connect(self._add_inputs)
        button_row.addWidget(self.add_input_button)
        self.edit_input_button = QPushButton("Edit / relink…")
        self.edit_input_button.setToolTip(
            "Edit the selected input's role, layer (relink) and register "
            "details. Double-click a row also edits.")
        self.edit_input_button.clicked.connect(self._edit_input)
        button_row.addWidget(self.edit_input_button)
        self.remove_input_button = QPushButton("Remove…")
        self.remove_input_button.clicked.connect(self._remove_input)
        button_row.addWidget(self.remove_input_button)
        button_row.addStretch(1)
        inputs_layout.addLayout(button_row)
        layout.addWidget(inputs_box, 1)

        # One status line for the whole tab (route, scope and bathymetry
        # feedback all land here, full-width so it survives narrow docks).
        self.apply_status = QLabel("")
        self.apply_status.setWordWrap(True)
        layout.addWidget(self.apply_status)

        # Unapplied edits mark the matching Apply button; a background
        # refresh of the same plan must not clobber them.
        self._dirty = set()
        for widget, signal in (
                (self.scope_start, "valueChanged"),
                (self.scope_end, "valueChanged"),
                (self.direction_combo, "currentIndexChanged")):
            getattr(widget, signal).connect(
                lambda *_a, s=self: s._mark_dirty("scope"))
        self.target_burial.valueChanged.connect(
            lambda *_a: self._mark_dirty("target"))
        for widget, signal in (
                (self.manual_source_combo, "currentIndexChanged"),
                (self.raster_combo, "layerChanged"),
                (self.raster_band, "valueChanged"),
                (self.contour_combo, "layerChanged"),
                (self.contour_field, "fieldChanged"),
                (self.contour_combo2, "layerChanged"),
                (self.contour_field2, "fieldChanged"),
                (self.search_radius, "valueChanged")):
            getattr(widget, signal).connect(
                lambda *_a, s=self: s._mark_dirty("bathy"))

        model.planChanged.connect(self.refresh)
        # Coalesced: input status can open layer files, and a project load
        # or a burst of rule toggles must cost one refresh, not dozens.
        inputs_soon = ui_helpers.coalesced(self, self._refresh_inputs)
        model.inputsChanged.connect(inputs_soon)
        model.inputsChanged.connect(self._refresh_bathy_notice)
        # "Used by" follows the criteria and checks (optional on
        # lightweight models).
        for name in ("rulesChanged", "riskChanged"):
            signal = getattr(model, name, None)
            if signal is not None:
                signal.connect(inputs_soon)
        self.refresh()

    def _mark_dirty(self, key: str) -> None:
        if self._loading:
            return
        self._dirty.add(key)
        self._sync_dirty_markers()

    def _clear_dirty(self, key: str) -> None:
        self._dirty.discard(key)
        self._sync_dirty_markers()

    def _sync_dirty_markers(self) -> None:
        self.apply_scope_button.setText(
            "Apply scope / direction *" if "scope" in self._dirty
            else "Apply scope / direction")
        self.apply_bathy_button.setText(
            "Apply source *" if "bathy" in self._dirty else "Apply source")
        self.apply_target_button.setText(
            "Apply targets *" if "target" in self._dirty else "Apply targets")

    def _sync_input_buttons(self) -> None:
        has_plan = bool(self.model.plan)
        selected = self._selected_input_ids()
        self.add_input_button.setEnabled(has_plan)
        self.edit_input_button.setEnabled(has_plan and len(selected) == 1)
        self.remove_input_button.setEnabled(has_plan and bool(selected))

    # -- refresh --------------------------------------------------------------
    def refresh(self) -> None:
        self._loading = True
        try:
            plan = self.model.plan
            enabled = bool(plan)
            plan_id = str(plan.get("plan_id") or "")
            same_plan = plan_id == getattr(self, "_loaded_plan_id", "")
            self._loaded_plan_id = plan_id
            if not same_plan:
                self._dirty = set()
                self._sync_dirty_markers()
                self.apply_status.setText("")
            for widget in (self.apply_rpl_button, self.apply_fallback_button,
                           self.apply_scope_button, self.apply_bathy_button,
                           self.apply_target_button, self.add_target_button,
                           self.pick_target_button, self.remove_target_button,
                           self.import_target_button):
                widget.setEnabled(enabled)
            self._refresh_rpls()
            route_name = plan.get("rpl_name") or ""
            revision = plan.get("rpl_revision") or ""
            if route_name:
                kind = ("Workbench RPL" if plan.get("rpl_id")
                        else "project line layer")
                self.active_route_label.setText(
                    route_name + (f" — {revision}" if revision else "")
                    + f"  ({kind})")
            else:
                self.active_route_label.setText(
                    "— (no route set)" if plan else "—")
            if not (same_plan and "scope" in self._dirty):
                self.scope_start.setValue(
                    float(plan.get("scope_start_kp") or 0.0))
                self.scope_end.setValue(float(plan.get("scope_end_kp") or 0.0))
                index = self.direction_combo.findData(
                    int(plan.get("direction") or 1))
                self.direction_combo.setCurrentIndex(max(0, index))
                self._clear_dirty("scope")
            if not (same_plan and "target" in self._dirty):
                self._load_targets()
                self._clear_dirty("target")
            if not (same_plan and "bathy" in self._dirty):
                self._load_bathy_config()
                self._clear_dirty("bathy")
            self._refresh_bathy_notice()
            self._update_target_summary()
            if self.model.route_notice:
                self._set_status(self.model.route_notice, "warn")
            elif plan and self.model.route_error:
                self._set_status(self.model.route_error, "error")
        finally:
            self._loading = False
        self._refresh_inputs()

    def _set_status(self, text: str, kind: str = "") -> None:
        self.apply_status.setText(text)
        self.apply_status.setStyleSheet(ui_helpers.status_style(kind))

    def _refresh_rpls(self) -> None:
        previous = self.rpl_combo.currentData()
        self.rpl_combo.clear()
        store = self.workbench_store_fn()
        if store is None:
            self.rpl_combo.addItem("(no Workbench GeoPackage in this project)", "")
            self.rpl_revision_label.setText("—")
            return
        try:
            rpls = store.list_rpls()
        except Exception:
            rpls = []
        current = (self.model.resolved_rpl_id or
                   self.model.plan.get("rpl_id") or "")
        for rpl in rpls:
            name = rpl.get("name") or "RPL"
            revision = (rpl.get("rev_label") or "").strip()
            label = name if revision and revision.lower() in name.lower() \
                else (f"{name} — {revision}" if revision else name)
            kind = (rpl.get("kind") or "").replace("_", "-").strip()
            if kind:
                label = f"{label} ({kind})"
            self.rpl_combo.addItem(label, rpl.get("rpl_id"))
            self.rpl_combo.setItemData(
                self.rpl_combo.count() - 1, revision, _RPL_REVISION_ROLE)
        index = self.rpl_combo.findData(current)
        if index >= 0:
            self.rpl_combo.setCurrentIndex(index)
        elif previous:
            previous_index = self.rpl_combo.findData(previous)
            if previous_index >= 0:
                self.rpl_combo.setCurrentIndex(previous_index)
        self._update_rpl_revision_preview()

    def _update_rpl_revision_preview(self, *_args) -> None:
        revision = self.rpl_combo.currentData(_RPL_REVISION_ROLE) \
            if self.rpl_combo.count() else ""
        self.rpl_revision_label.setText(str(revision or "—"))

    def _refresh_inputs(self) -> None:
        rows = [r for r in self.model.inputs
                if r.get("role") != schema.INPUT_ROLE_BATHY]
        rows.sort(key=lambda r: (schema.INPUT_ROLES.index(r.get("role"))
                                 if r.get("role") in schema.INPUT_ROLES else 99,
                                 (r.get("layer_name") or "").lower()))
        usage = self.model.input_usage() if self.model.plan else {}
        counts = {"ok": 0, "relinked": 0, "file": 0, "missing": 0}
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        self.inputs_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            try:
                state, detail, layer = self.model.input_status(row)
            except Exception as exc:  # never let one bad row break the tab
                state, detail, layer = "missing", str(exc), None
            counts[state] = counts.get(state, 0) + 1
            stored_name = row.get("layer_name") or ""
            live_name = layer.name() if layer is not None else ""
            name = live_name or stored_name
            users = usage.get(str(row.get("input_id") or ""), [])
            values = [
                schema.INPUT_ROLE_LABELS.get(row.get("role") or "",
                                             row.get("role") or ""),
                name,
                _STATUS_TEXT.get(state, state),
                str(len(users)) if users else "—",
                row.get("originator") or "", row.get("revision") or "",
                row.get("quality") or "",
            ]
            for j, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(flags)
                if j == 0:
                    item.setData(ITEM_DATA_USER_ROLE, row.get("input_id"))
                if j == 1:
                    tip = row.get("layer_source") or ""
                    if live_name and stored_name and live_name != stored_name:
                        tip = (f"Registered as '{stored_name}', now named "
                               f"'{live_name}'.\n") + tip
                    item.setToolTip(tip)
                if j == 2:
                    item.setToolTip(detail)
                    if state in ("file", "missing"):
                        item.setForeground(ui_helpers.qcolor(
                            "error" if state == "missing" else "warn"))
                if j == 3:
                    item.setToolTip("\n".join(users) if users else
                                    "Not used by any criterion or check.")
                self.inputs_table.setItem(i, j, item)
        bad = counts.get("missing", 0) + counts.get("file", 0)
        text = f"{len(rows)} input(s)"
        if bad:
            text += f" — {bad} need attention"
        self.inputs_summary.setText(text)
        self.inputs_summary.setStyleSheet(
            ui_helpers.status_style("warn") if bad else "")
        self._apply_inputs_filter()
        self._sync_input_buttons()

    def _apply_inputs_filter(self, *_args) -> None:
        words = self.inputs_filter.text().lower().split()
        for row in range(self.inputs_table.rowCount()):
            text = " ".join(
                (self.inputs_table.item(row, col).text()
                 if self.inputs_table.item(row, col) else "")
                for col in range(self.inputs_table.columnCount())).lower()
            self.inputs_table.setRowHidden(
                row, not all(word in text for word in words))

    # -- RPL / scope ----------------------------------------------------------
    def _apply_rpl(self) -> None:
        store = self.workbench_store_fn()
        rpl_id = self.rpl_combo.currentData()
        if not store or not rpl_id:
            self._set_status(
                "No Workbench RPL is selected — register the route in the "
                "Cable Workbench or use a line layer below.", "warn")
            return
        rpl = store.get_rpl(rpl_id) or {}
        from .. import map_layers

        # Keep the model's store handle current; the Workbench can be created
        # after the Burial Planner dock was first opened.
        self.model.workbench_store = store
        if not self.model.update_plan({
            "rpl_id": rpl_id,
            "rpl_name": rpl.get("name") or "",
            "rpl_revision": rpl.get("rev_label") or "",
            "rpl_gpkg_path": store.gpkg_path,
            "rpl_fingerprint": map_layers.rpl_fingerprint(rpl, store.gpkg_path),
        }, reason="route set"):
            return
        if self.model.route is None:
            # The reference saved but the route itself would not open —
            # a success message here would mask the broken state.
            self._set_status(
                self.model.route_error
                or "The route was saved but could not be opened.", "error")
            return
        self._set_status(
            "Workbench route and revision applied. Continue to Bathymetry "
            "Profile to review and rebuild the stored samples.", "ok")

    def _apply_fallback(self) -> None:
        layer = self.fallback_combo.currentLayer()
        if layer is None:
            self._set_status("No line layer is selected.", "warn")
            return
        if not self.model.update_plan({
            "rpl_id": "",
            "rpl_name": layer.name(),
            "rpl_revision": "",
            "rpl_gpkg_path": layer.source(),
            "rpl_fingerprint": normalised_path(layer.source().split("|")[0]),
        }, reason="route set (line layer)"):
            return
        if self.model.route is None:
            self._set_status(
                self.model.route_error
                or "The route was saved but could not be opened.", "error")
            return
        self._set_status(
            "Line-layer route applied. Continue to Bathymetry Profile to "
            "review and rebuild the stored samples.", "ok")

    def _full_route(self) -> None:
        if self.model.route is None:
            self._set_status(
                "Set the route first — Full route needs the route length.",
                "warn")
            return
        self.scope_start.setValue(0.0)
        self.scope_end.setValue(self.model.route.total_length_km)

    def _pick_scope_kp(self, spin, which: str) -> None:
        if self.dock is None:
            return
        self.dock.pick_kp_on_map(
            spin.setValue,
            f"Click the route to pick the scope {which} KP "
            "(right-click cancels).")

    def _apply_scope(self) -> None:
        start = self.scope_start.value()
        end = self.scope_end.value()
        if end <= start:
            self._set_status(
                "The scope end KP must be greater than the start KP — "
                "nothing was saved.", "error")
            return
        saved = self.model.update_plan({
            "scope_start_kp": start,
            "scope_end_kp": end,
            "direction": self.direction_combo.currentData(),
        }, reason="scope/direction")
        if saved:
            self._clear_dirty("scope")
            self._set_status(
                "Scope and direction applied. Continue to Bathymetry Profile "
                "to review and rebuild the stored samples.", "ok")

    # -- bathymetry -----------------------------------------------------------
    def _bathy_row(self) -> Optional[Dict]:
        for row in self.model.inputs:
            if row.get("role") == schema.INPUT_ROLE_BATHY:
                return row
        return None

    def _sync_bathy_enabled(self, *_args) -> None:
        source_mode = int(self.manual_source_combo.currentData() or 1)
        for widget in (self.raster_combo, self.raster_band):
            widget.setEnabled(source_mode == 1)
        for widget in (self.contour_combo, self.contour_field,
                       self.contour_combo2, self.contour_field2,
                       self.search_radius):
            widget.setEnabled(source_mode == 2)

    def _load_bathy_config(self) -> None:
        row = self._bathy_row()
        if row is None:
            self.bathy_summary.setText("No manual bathymetry source configured.")
            self._sync_bathy_enabled()
            return
        try:
            config = json.loads(row.get("config_json") or "{}")
        except (ValueError, TypeError):
            config = {}
        project = QgsProject.instance()
        self.raster_combo.setLayer(None)
        self.contour_combo.setLayer(None)
        self.contour_combo2.setLayer(None)
        # The model's config has stale layer ids relinked by source.
        resolved = self.model.depth_config()
        raster_ids = list(resolved.raster_layer_ids) or \
            (config.get("raster_layer_ids") or [])
        contours = list(resolved.contour_layers) or \
            (config.get("contour_layers") or [])
        source_mode = int(config.get("mode") or 0)
        if source_mode not in (1, 2):
            source_mode = 1 if raster_ids else 2
        source_index = self.manual_source_combo.findData(source_mode)
        self.manual_source_combo.setCurrentIndex(max(0, source_index))
        if raster_ids:
            layer = project.mapLayer(raster_ids[0])
            if layer is not None:
                self.raster_combo.setLayer(layer)
        self.raster_band.setValue(int(config.get("raster_band") or 1))
        if contours:
            layer = project.mapLayer(contours[0].get("layer_id") or "")
            if layer is not None:
                self.contour_combo.setLayer(layer)
                self.contour_field.setLayer(layer)
                self.contour_field.setField(contours[0].get("depth_field") or "")
        if len(contours) > 1:
            layer = project.mapLayer(contours[1].get("layer_id") or "")
            if layer is not None:
                self.contour_combo2.setLayer(layer)
                self.contour_field2.setLayer(layer)
                self.contour_field2.setField(contours[1].get("depth_field") or "")
        self.search_radius.setValue(float(config.get("contour_search_radius_m") or 500.0))
        self._sync_bathy_enabled()

    def _refresh_bathy_notice(self) -> None:
        """Describe the active source, flagging missing/relinked layers
        rather than silently showing an empty layer selector."""
        if self._bathy_row() is None:
            self.bathy_summary.setText("No manual bathymetry source configured.")
            self.bathy_summary.setStyleSheet("")
            self.bathy_relink_button.setVisible(False)
            return
        config = self.model.depth_config()
        project = QgsProject.instance()
        ids = list(config.raster_layer_ids) or [
            e.get("layer_id") or "" for e in config.contour_layers]
        names, missing = [], 0
        for layer_id in ids:
            layer = project.mapLayer(layer_id)
            if layer is None:
                missing += 1
            else:
                names.append(layer.name())
        kind = ("Raster" if config.raster_layer_ids
                else f"Contours ({len(config.contour_layers)} layer(s))")
        text = f"{kind}: " + (", ".join(names) or "—")
        style = ""
        if missing:
            text += (f" — ⚠ {missing} configured layer(s) are not in the "
                     "project. Re-add the layer (it is found again by its "
                     "source) or choose another source and Apply.")
            style = ui_helpers.status_style("warn")
        relinks = self.model.depth_relinks
        if relinks:
            text += (" — relinked by source: " + ", ".join(relinks)
                     + " (the saved layer id was gone).")
        self.bathy_summary.setText(text)
        self.bathy_summary.setStyleSheet(style)
        self.bathy_relink_button.setVisible(bool(relinks))

    def _save_bathy_relink(self) -> None:
        existing = self._bathy_row()
        if existing is None:
            return
        config = self.model.depth_config()
        try:
            data = json.loads(existing.get("config_json") or "{}")
        except (TypeError, ValueError):
            data = {}
        data["raster_layer_ids"] = list(config.raster_layer_ids)
        data["contour_layers"] = list(config.contour_layers)
        row = dict(existing)
        row["config_json"] = json.dumps(data)
        if self.model.save_input(row):
            self._set_status("Bathymetry relink saved.", "ok")

    def _apply_bathy(self) -> None:
        existing = self._bathy_row()
        source_mode = int(self.manual_source_combo.currentData() or 1)
        config: Dict = {"mode": source_mode, "raster_layer_ids": [], "raster_band": 1,
                        "contour_layers": [], "contour_search_radius_m": 0.0,
                        "auto_resample": True}
        raster = self.raster_combo.currentLayer() if source_mode == 1 else None
        if source_mode == 1 and raster is not None:
            config["raster_layer_ids"] = [raster.id()]
            # Sources ride along so a removed-and-re-added layer (new id)
            # is found again instead of silently dropping the bathymetry.
            config["raster_sources"] = [raster.source()]
            config["raster_band"] = self.raster_band.value()
        contour = self.contour_combo.currentLayer() if source_mode == 2 else None
        contour2 = self.contour_combo2.currentLayer() if source_mode == 2 else None
        for layer, field_combo, label in (
                (contour, self.contour_field, "contour layer 1"),
                (contour2, self.contour_field2, "contour layer 2")):
            if layer is not None and not (field_combo.currentField() or ""):
                QMessageBox.warning(
                    self, "Burial Planner",
                    f"Pick the depth field for {label} — without it the "
                    "first attribute would be used, which is rarely the "
                    "depth.")
                return
        if contour is not None:
            config["contour_layers"].append({
                "layer_id": contour.id(),
                "source": contour.source(),
                "depth_field": self.contour_field.currentField() or "",
            })
        if contour2 is not None:
            if contour is not None and contour2.id() == contour.id():
                QMessageBox.warning(
                    self, "Burial Planner",
                    "Contour layer 1 and contour layer 2 must be different layers.")
                return
            config["contour_layers"].append({
                "layer_id": contour2.id(),
                "source": contour2.source(),
                "depth_field": self.contour_field2.currentField() or "",
            })
        if config["contour_layers"]:
            config["contour_search_radius_m"] = self.search_radius.value()
        if not config["raster_layer_ids"] and not config["contour_layers"]:
            QMessageBox.warning(self, "Burial Planner",
                                "Pick a raster or at least one contour layer.")
            return
        primary_contour = contour or contour2
        row = dict(existing or {})
        row.update({
            "role": schema.INPUT_ROLE_BATHY,
            "layer_name": (raster.name() if raster is not None
                           else (primary_contour.name() if primary_contour is not None else "")),
            "layer_source": (raster.source() if raster is not None
                             else (primary_contour.source() if primary_contour is not None else "")),
            "layer_id_hint": (raster.id() if raster is not None
                              else (primary_contour.id() if primary_contour is not None else "")),
            "config_json": json.dumps(config),
        })
        if self.model.save_input(row):
            self._clear_dirty("bathy")
            kind = "raster" if source_mode == 1 else \
                f"{len(config['contour_layers'])} contour layer(s)"
            self._set_status(
                f"Manual {kind} applied. Continue to Bathymetry Profile to "
                "review resolution and rebuild the stored samples.", "ok")

    # -- other inputs ---------------------------------------------------------
    def _add_inputs(self) -> None:
        if not self.model.plan:
            return
        dialog = AddInputsDialog(self.model.inputs, self._panel_selection,
                                 parent=self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        rows = dialog.result_rows()
        if rows and self.model.save_inputs(rows):
            self._set_status(f"Registered {len(rows)} input(s).", "ok")

    def _panel_selection(self):
        iface = getattr(self.dock, "iface", None) if self.dock else None
        view = iface.layerTreeView() if iface is not None else None
        return view.selectedLayers() if view is not None else []

    def _add_input(self) -> None:
        """Single-layer registration (kept for scripts/tests)."""
        if not self.model.plan:
            return
        dialog = InputDialog(parent=self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            row = dialog.result_row()
            if row:
                self.model.save_input(row)

    def _selected_input_ids(self) -> List[str]:
        model = self.inputs_table.selectionModel()
        rows = sorted({index.row() for index in model.selectedRows()}) \
            if model is not None else []
        if not rows and self.inputs_table.currentRow() >= 0:
            rows = [self.inputs_table.currentRow()]
        ids = []
        for row in rows:
            item = self.inputs_table.item(row, 0)
            if item is not None and item.data(ITEM_DATA_USER_ROLE):
                ids.append(item.data(ITEM_DATA_USER_ROLE))
        return ids

    def _selected_input_id(self) -> str:
        ids = self._selected_input_ids()
        return ids[0] if len(ids) == 1 else ""

    def _edit_input(self) -> None:
        input_id = self._selected_input_id()
        if not input_id:
            return
        row = next((r for r in self.model.inputs
                    if r.get("input_id") == input_id), None)
        if row is None:
            return
        dialog = InputDialog(row, parent=self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            new_row = dialog.result_row()
            if new_row:
                self.model.save_input(new_row)

    def _remove_input(self) -> None:
        input_ids = self._selected_input_ids()
        if not input_ids:
            return
        usage = self.model.input_usage()
        users = []
        for input_id in input_ids:
            users.extend(usage.get(str(input_id), []))
        message = (f"Remove {len(input_ids)} registered input(s)?"
                   if len(input_ids) > 1 else
                   "Remove this registered input?")
        if users:
            listed = "\n• ".join(users[:8])
            more = f"\n… and {len(users) - 8} more" if len(users) > 8 else ""
            message += ("\n\nThese criteria/checks use it and will report a "
                        f"missing input until re-pointed:\n• {listed}{more}")
        answer = QMessageBox.question(
            self, "Remove input", message,
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer == MESSAGE_BOX_YES:
            for input_id in input_ids:
                if not self.model.delete_input(input_id):
                    break

    # -- target burial depth ---------------------------------------------------
    def _load_targets(self) -> None:
        plan = self.model.plan
        self.target_burial.setValue(float(plan.get("target_burial_m") or 0.0))
        self._fill_target_table(self.model.target_ranges() if plan else [])

    def _fill_target_table(self, ranges) -> None:
        self.target_table.blockSignals(True)
        try:
            self.target_table.setRowCount(0)
            for entry in ranges:
                self._append_target_row(entry)
        finally:
            self.target_table.blockSignals(False)

    def _append_target_row(self, entry: Dict) -> None:
        row = self.target_table.rowCount()
        self.target_table.insertRow(row)
        values = [schema.format_kp(entry.get("start_kp")),
                  schema.format_kp(entry.get("end_kp")),
                  "" if entry.get("depth_m") in (None, "")
                  else f"{float(entry['depth_m']):g}",
                  str(entry.get("notes") or "")]
        for column, value in enumerate(values):
            self.target_table.setItem(row, column, QTableWidgetItem(value))

    def _table_targets(self) -> List[Dict]:
        rows = []
        for row in range(self.target_table.rowCount()):
            def cell(column: int) -> str:
                item = self.target_table.item(row, column)
                return item.text().strip() if item is not None else ""

            def number(column: int):
                text = cell(column).replace(",", ".")
                try:
                    return float(text) if text else None
                except ValueError:
                    return text  # validate_ranges reports it
            rows.append({"start_kp": number(0), "end_kp": number(1),
                         "depth_m": number(2), "notes": cell(3)})
        return rows

    def _add_target_row(self) -> None:
        ranges = self._table_targets()
        starts = [r["end_kp"] for r in ranges
                  if isinstance(r.get("end_kp"), float)]
        start = max(starts) if starts else self.scope_start.value()
        end = start + 1.0
        if self.model.route is not None:
            end = min(end, self.model.route.total_length_km)
        self._append_target_row({"start_kp": start, "end_kp": end,
                                 "depth_m": self.target_burial.value() or None,
                                 "notes": ""})
        self._mark_dirty("target")
        self.target_table.setCurrentCell(self.target_table.rowCount() - 1, 2)

    def _pick_target_range(self) -> None:
        if self.dock is None:
            return
        picked: List[float] = []

        def second(kp: float) -> None:
            picked.append(kp)
            lo, hi = sorted(picked[:2])
            if hi - lo <= 1e-6:
                self._set_status("The two KPs are the same — no range added.",
                                 "warn")
                return
            self._append_target_row({
                "start_kp": lo, "end_kp": hi,
                "depth_m": self.target_burial.value() or None, "notes": ""})
            self._mark_dirty("target")
            self.target_table.setCurrentCell(self.target_table.rowCount() - 1, 2)
            self._set_status("Range added — enter its target depth, then "
                             "Apply targets.", "info")

        def first(kp: float) -> None:
            picked.append(kp)
            self.dock.pick_kp_on_map(
                second, "Click the route at the other end of the target "
                "range (right-click cancels).")

        self.dock.pick_kp_on_map(
            first, "Click the route at one end of the target range "
            "(right-click cancels).")

    def _remove_target_rows(self) -> None:
        rows = sorted({index.row() for index in
                       self.target_table.selectionModel().selectedRows()},
                      reverse=True)
        if not rows and self.target_table.currentRow() >= 0:
            rows = [self.target_table.currentRow()]
        for row in rows:
            self.target_table.removeRow(row)
        if rows:
            self._mark_dirty("target")

    def _import_rpl_targets(self) -> None:
        legs = self._rpl_legs_with_kp()
        ranges = target_depth.ranges_from_legs(legs)
        if not ranges:
            self._set_status(
                "The plan's RPL has no legs with a TargetBurialDepth (or the "
                "route is not a Workbench RPL) — nothing imported.", "warn")
            return
        if self.target_table.rowCount():
            answer = QMessageBox.question(
                self, "Target burial depth",
                f"Replace the {self.target_table.rowCount()} range(s) in the "
                f"table with {len(ranges)} range(s) from the RPL?",
                MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
            if answer != MESSAGE_BOX_YES:
                return
        self._fill_target_table(ranges)
        self._mark_dirty("target")
        self._set_status(f"{len(ranges)} range(s) read from the RPL — review "
                         "them, then Apply targets.", "info")

    def _rpl_legs_with_kp(self) -> List[Dict]:
        """RPL legs with start/end KP (from their From/To positions)."""
        store = self.workbench_store_fn()
        rpl_id = self.model.resolved_rpl_id or self.model.plan.get("rpl_id")
        if store is None or not rpl_id:
            return []
        try:
            from ...workbench import rpl_summary

            rpl = store.get_rpl(rpl_id) or {}
            points = rpl_summary.read_point_rows(rpl_summary.open_rpl_layer(
                store, rpl.get("points_layer") or ""))
            legs = rpl_summary.read_leg_rows(rpl_summary.open_rpl_layer(
                store, rpl.get("lines_layer") or ""))
        except Exception:
            return []
        kp_by_pos = {str(p.get("pos")): p.get("kp") for p in points
                     if p.get("kp") is not None}
        out, cursor = [], 0.0
        for leg in legs:
            start = kp_by_pos.get(str(leg.get("from_pos")))
            end = kp_by_pos.get(str(leg.get("to_pos")))
            if start is None or end is None:
                # Fall back to the cumulative leg length.
                length = leg.get("route_km") or 0.0
                start, end = cursor, cursor + float(length)
            cursor = end
            out.append(dict(leg, start_kp=start, end_kp=end,
                            label=f"RPL leg {leg.get('seq')}"))
        return out

    def _update_target_summary(self) -> None:
        if not self.model.plan:
            self.target_summary.setText("")
            return
        text = target_depth.summary_text(self.model.target_default(),
                                         self.model.target_ranges())
        if "target" in self._dirty:
            text += "  (unapplied edits)"
        self.target_summary.setText(text)

    def _apply_targets(self) -> None:
        ranges = self._table_targets()
        length = self.model.route.total_length_km \
            if self.model.route is not None else None
        problems = target_depth.validate_ranges(ranges, length)
        if problems:
            self._set_status("Targets not saved: " + " ".join(problems[:3]),
                             "error")
            return
        if length:
            # Clamp the 3-dp rounding past the route end onto the route.
            for entry in ranges:
                entry["start_kp"] = max(0.0, entry["start_kp"])
                entry["end_kp"] = min(length, entry["end_kp"])
        if self.model.update_targets(self.target_burial.value() or None,
                                     ranges):
            self._clear_dirty("target")
            self._update_target_summary()
            self._set_status("Target burial depth applied.", "ok")
