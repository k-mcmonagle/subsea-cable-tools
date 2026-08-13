# -*- coding: utf-8 -*-
"""Inputs tab — RPL, scope, direction, bathymetry and registered inputs.

The scope spinboxes are the cost control for long routes: analysis is
limited to the scoped KP range (stated plainly in the UI). Registered
inputs become the only selectable sources inside rule configs (stable
``input_id`` indirection), each carrying optional Input Data Register
metadata (originator, revision, status, quality).
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.core import QgsProject
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    DIALOG_ACCEPTED,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MAP_LAYER_FILTER_LINE,
    MAP_LAYER_FILTER_POINT,
    MAP_LAYER_FILTER_POLYGON,
    MAP_LAYER_FILTER_VECTOR,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    layer_filters,
    qt_exec,
)
from ...workbench.project_layers import normalised_path
from .. import schema

_ROLE_FILTERS = {
    schema.INPUT_ROLE_CROSSINGS_POINTS: (MAP_LAYER_FILTER_POINT,),
    schema.INPUT_ROLE_CROSSINGS_LINES: (MAP_LAYER_FILTER_LINE,),
    schema.INPUT_ROLE_SOILS: (MAP_LAYER_FILTER_POLYGON,),
    schema.INPUT_ROLE_OTHER: (MAP_LAYER_FILTER_VECTOR,),
}

_RPL_REVISION_ROLE = int(ITEM_DATA_USER_ROLE) + 1


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
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        index = self.role_combo.findData(self.row.get("role"))
        if index >= 0:
            self.role_combo.setCurrentIndex(index)
        self._sync_filter()
        if self.row.get("status") == "superseded":
            self.status_combo.setCurrentIndex(1)
        quality_index = self.quality_combo.findText(self.row.get("quality") or "")
        if quality_index >= 0:
            self.quality_combo.setCurrentIndex(quality_index)

    def _sync_filter(self) -> None:
        role = self.role_combo.currentData()
        members = _ROLE_FILTERS.get(role, (MAP_LAYER_FILTER_VECTOR,))
        self.layer_combo.setFilters(layer_filters(*members))

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
        # while labels and selectors no longer stretch across the whole screen.
        outer = QHBoxLayout(self)
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
        rpl_row = QHBoxLayout()
        self.rpl_combo = QComboBox()
        self.rpl_combo.currentIndexChanged.connect(self._update_rpl_revision_preview)
        rpl_row.addWidget(self.rpl_combo, 1)
        self.apply_rpl_button = QPushButton("Set route")
        self.apply_rpl_button.clicked.connect(self._apply_rpl)
        rpl_row.addWidget(self.apply_rpl_button)
        rpl_form.addRow("Workbench RPL:", rpl_row)
        self.rpl_revision_label = QLabel("—")
        self.rpl_revision_label.setToolTip(
            "Revision label stored on the selected Cable Workbench RPL.")
        rpl_form.addRow("Selected RPL revision:", self.rpl_revision_label)
        fallback_row = QHBoxLayout()
        self.fallback_combo = QgsMapLayerComboBox()
        self.fallback_combo.setFilters(layer_filters(MAP_LAYER_FILTER_LINE))
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
        scope_row = QHBoxLayout()
        self.scope_start = QDoubleSpinBox()
        self.scope_end = QDoubleSpinBox()
        for spin in (self.scope_start, self.scope_end):
            spin.setDecimals(3)
            spin.setRange(0.0, 100000.0)
            spin.setSuffix(" km")
        scope_row.addWidget(QLabel("KP"))
        scope_row.addWidget(self.scope_start)
        self.scope_pick_start = QPushButton("Pick…")
        self.scope_pick_start.clicked.connect(
            lambda: self._pick_scope_kp(self.scope_start, "start"))
        scope_row.addWidget(self.scope_pick_start)
        scope_row.addWidget(QLabel("to"))
        scope_row.addWidget(self.scope_end)
        self.scope_pick_end = QPushButton("Pick…")
        self.scope_pick_end.clicked.connect(
            lambda: self._pick_scope_kp(self.scope_end, "end"))
        scope_row.addWidget(self.scope_pick_end)
        for button in (self.scope_pick_start, self.scope_pick_end):
            button.setToolTip(
                "Pick the KP by clicking the route on the map (right-click "
                "or Esc cancels). Apply scope / direction saves it.")
        self.full_route_button = QPushButton("Full route")
        self.full_route_button.clicked.connect(self._full_route)
        scope_row.addWidget(self.full_route_button)
        scope_row.addStretch(1)
        scope_form.addRow("Scope:", scope_row)
        self.direction_combo = QComboBox()
        self.direction_combo.addItem("A → B (with increasing KP)", 1)
        self.direction_combo.addItem("B → A (against KP)", -1)
        scope_form.addRow("Direction of installation:", self.direction_combo)
        self.target_burial = QDoubleSpinBox()
        self.target_burial.setDecimals(2)
        self.target_burial.setRange(0.0, 20.0)
        self.target_burial.setSuffix(" m")
        self.target_burial.setToolTip("Informational in this version.")
        scope_form.addRow("Target burial depth:", self.target_burial)
        scope_note = QLabel(
            "Analysis is limited to the scoped KP range — on long routes, "
            "scope is the run-time control.")
        scope_note.setWordWrap(True)
        scope_form.addRow(scope_note)
        self.apply_scope_button = QPushButton("Apply scope / direction")
        self.apply_scope_button.clicked.connect(self._apply_scope)
        scope_form.addRow(self.apply_scope_button)
        layout.addWidget(scope_box)

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
        bathy_form.addRow("Manual source type:", self.manual_source_combo)
        bathy_form.addRow("Raster:", self.raster_combo)
        bathy_form.addRow("Band:", self.raster_band)
        bathy_form.addRow("Contour layer 1 (minor or major):", self.contour_combo)
        bathy_form.addRow("Depth field 1:", self.contour_field)
        bathy_form.addRow("Contour layer 2 (optional):", self.contour_combo2)
        bathy_form.addRow("Depth field 2:", self.contour_field2)
        bathy_form.addRow("Contour search radius:", self.search_radius)
        self.apply_bathy_button = QPushButton("Apply source")
        self.apply_bathy_button.clicked.connect(self._apply_bathy)
        bathy_form.addRow(self.apply_bathy_button)
        self.apply_status = QLabel("")
        self.apply_status.setWordWrap(True)
        bathy_form.addRow(self.apply_status)
        layout.addWidget(bathy_box)

        # -- other inputs -----------------------------------------------------
        inputs_box = QGroupBox("Registered inputs")
        inputs_layout = QVBoxLayout(inputs_box)
        self.inputs_table = QTableWidget(0, 5)
        self.inputs_table.setHorizontalHeaderLabels(
            ["Role", "Layer", "Originator", "Revision", "Quality"])
        self.inputs_table.horizontalHeader().setSectionResizeMode(
            1, HEADER_RESIZE_MODE_STRETCH)
        self.inputs_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.inputs_table.verticalHeader().setVisible(False)
        inputs_layout.addWidget(self.inputs_table, 1)
        button_row = QHBoxLayout()
        for label, slot in (("Add…", self._add_input), ("Edit…", self._edit_input),
                            ("Remove", self._remove_input)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            button_row.addWidget(button)
        button_row.addStretch(1)
        inputs_layout.addLayout(button_row)
        layout.addWidget(inputs_box, 1)

        model.planChanged.connect(self.refresh)
        model.inputsChanged.connect(self._refresh_inputs)
        self.refresh()

    # -- refresh --------------------------------------------------------------
    def refresh(self) -> None:
        self._loading = True
        try:
            plan = self.model.plan
            enabled = bool(plan)
            self.setEnabled(True)
            for widget in (self.apply_rpl_button, self.apply_fallback_button,
                           self.apply_scope_button, self.apply_bathy_button):
                widget.setEnabled(enabled)
            self._refresh_rpls()
            self.scope_start.setValue(float(plan.get("scope_start_kp") or 0.0))
            self.scope_end.setValue(float(plan.get("scope_end_kp") or 0.0))
            index = self.direction_combo.findData(int(plan.get("direction") or 1))
            self.direction_combo.setCurrentIndex(max(0, index))
            self.target_burial.setValue(float(plan.get("target_burial_m") or 0.0))
            self._load_bathy_config()
            if self.model.route_notice:
                self.apply_status.setText(self.model.route_notice)
            elif plan and self.model.route_error:
                self.apply_status.setText(self.model.route_error)
        finally:
            self._loading = False
        self._refresh_inputs()

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
        self.inputs_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            values = [schema.INPUT_ROLE_LABELS.get(row.get("role") or "", row.get("role") or ""),
                      row.get("layer_name") or "", row.get("originator") or "",
                      row.get("revision") or "", row.get("quality") or ""]
            for j, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if j == 0:
                    from ...qgis_compat import ITEM_DATA_USER_ROLE

                    item.setData(ITEM_DATA_USER_ROLE, row.get("input_id"))
                self.inputs_table.setItem(i, j, item)

    # -- RPL / scope ----------------------------------------------------------
    def _apply_rpl(self) -> None:
        store = self.workbench_store_fn()
        rpl_id = self.rpl_combo.currentData()
        if not store or not rpl_id:
            return
        rpl = store.get_rpl(rpl_id) or {}
        from .. import map_layers

        # Keep the model's store handle current; the Workbench can be created
        # after the Burial Planner dock was first opened.
        self.model.workbench_store = store
        self.model.update_plan({
            "rpl_id": rpl_id,
            "rpl_name": rpl.get("name") or "",
            "rpl_revision": rpl.get("rev_label") or "",
            "rpl_gpkg_path": store.gpkg_path,
            "rpl_fingerprint": map_layers.rpl_fingerprint(rpl, store.gpkg_path),
        }, reason="route set")
        self.apply_status.setText(
            "Workbench route and revision applied. Continue to Bathymetry "
            "Profile to review and rebuild the stored samples.")

    def _apply_fallback(self) -> None:
        layer = self.fallback_combo.currentLayer()
        if layer is None:
            return
        self.model.update_plan({
            "rpl_id": "",
            "rpl_name": layer.name(),
            "rpl_revision": "",
            "rpl_gpkg_path": layer.source(),
            "rpl_fingerprint": normalised_path(layer.source().split("|")[0]),
        }, reason="route set (line layer)")
        self.apply_status.setText(
            "Line-layer route applied. Continue to Bathymetry Profile to "
            "review and rebuild the stored samples.")

    def _full_route(self) -> None:
        if self.model.route is not None:
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
        saved = self.model.update_plan({
            "scope_start_kp": self.scope_start.value(),
            "scope_end_kp": self.scope_end.value(),
            "direction": self.direction_combo.currentData(),
            "target_burial_m": self.target_burial.value() or None,
        }, reason="scope/direction")
        if saved:
            self.apply_status.setText(
                "Scope and direction applied. Continue to Bathymetry Profile "
                "to review and rebuild the stored samples.")

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
        raster_ids = config.get("raster_layer_ids") or []
        contours = config.get("contour_layers") or []
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
        self.bathy_summary.setText(
            "Manual raster source." if source_mode == 1
            else f"Manual contours: {len(contours)} layer(s).")
        self._sync_bathy_enabled()

    def _apply_bathy(self) -> None:
        existing = self._bathy_row()
        source_mode = int(self.manual_source_combo.currentData() or 1)
        config: Dict = {"mode": source_mode, "raster_layer_ids": [], "raster_band": 1,
                        "contour_layers": [], "contour_search_radius_m": 0.0,
                        "auto_resample": True}
        raster = self.raster_combo.currentLayer() if source_mode == 1 else None
        if source_mode == 1 and raster is not None:
            config["raster_layer_ids"] = [raster.id()]
            config["raster_band"] = self.raster_band.value()
        contour = self.contour_combo.currentLayer() if source_mode == 2 else None
        contour2 = self.contour_combo2.currentLayer() if source_mode == 2 else None
        if contour is not None:
            config["contour_layers"].append({
                "layer_id": contour.id(),
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
            kind = "raster" if source_mode == 1 else \
                f"{len(config['contour_layers'])} contour layer(s)"
            self.apply_status.setText(
                f"Manual {kind} applied. Continue to Bathymetry Profile to "
                "review resolution and rebuild the stored samples.")

    # -- other inputs ---------------------------------------------------------
    def _add_input(self) -> None:
        if not self.model.plan:
            return
        dialog = InputDialog(parent=self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            row = dialog.result_row()
            if row:
                self.model.save_input(row)

    def _selected_input_id(self) -> str:
        row = self.inputs_table.currentRow()
        if row < 0:
            return ""
        item = self.inputs_table.item(row, 0)
        from ...qgis_compat import ITEM_DATA_USER_ROLE

        return item.data(ITEM_DATA_USER_ROLE) if item else ""

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
        input_id = self._selected_input_id()
        if not input_id:
            return
        answer = QMessageBox.question(
            self, "Remove input",
            "Remove this registered input? Rules referencing it will report a "
            "missing input until re-pointed.",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer == MESSAGE_BOX_YES:
            self.model.delete_input(input_id)
