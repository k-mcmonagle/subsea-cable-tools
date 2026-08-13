# -*- coding: utf-8 -*-
"""Exclusions tab — the ordered rule stack over the plan scope.

Presents the stack with the shared overview-bar + fire-bar widgets
(``workbench/kp_bars.py``). Each rule carries its action, criterion class
badge (Non-Deviable / Project / Screening), source reference, extension
buffer, Constraint Influence Zone distances, scope ranges and kind-specific
config. Screening rules draw in the risk palette and are captioned
"flags for assessment — does not exclude".

Fire-bar recompute runs in the background through the dock's analysis task
with per-rule caching — no modal dialogs.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    DIALOG_ACCEPTED,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
    TOOLBUTTON_POPUP_MODE_INSTANT,
    qt_exec,
)
from ...workbench import schema as wb_schema
from ...workbench.kp_bars import ACTION_COLORS, FireBarDelegate, VerdictStrip
from ...workbench.rules_engine import STATUS_EXCLUDED, STATUS_RISK
from .. import change_log, schema

FIRE_COL = 2

_KIND_LABELS = {
    wb_schema.RULE_KIND_THRESHOLD: "Water depth / slope threshold",
    wb_schema.RULE_KIND_PROXIMITY: "Crossings / proximity",
    wb_schema.RULE_KIND_POLYGON: "Seabed soils / polygon class",
    wb_schema.RULE_KIND_KP_TABLE: "KP range table",
    wb_schema.RULE_KIND_MANUAL: "Manual ranges",
}

_CLASS_BADGES = {
    schema.CRITERION_NON_DEVIABLE: "ND",
    schema.CRITERION_PROJECT: "PR",
    schema.CRITERION_SCREENING: "SC",
}


def _parse_scope(text: str) -> List[Dict]:
    out: List[Dict] = []
    for chunk in (text or "").replace(";", ",").split(","):
        chunk = chunk.strip().replace("–", "-").replace("..", "-")
        if not chunk:
            continue
        parts = chunk.split("-")
        if len(parts) != 2:
            continue
        try:
            out.append({"start_kp": float(parts[0]), "end_kp": float(parts[1])})
        except ValueError:
            continue
    return out


def _format_scope(ranges: List[Dict]) -> str:
    return ", ".join(f"{float(r['start_kp']):.3f}-{float(r['end_kp']):.3f}"
                     for r in ranges or [])


class RuleEditorDialog(QDialog):
    """Edit one bp_rule row (kind fixed at creation)."""

    def __init__(self, rule: Dict, inputs: List[Dict], method: str, parent=None):
        super().__init__(parent)
        self.rule = dict(rule)
        self.inputs = inputs
        kind = self.rule.get("kind") or ""
        self.setWindowTitle(f"Exclusion criterion — {_KIND_LABELS.get(kind, kind)}")
        self.setMinimumWidth(460)
        try:
            self.config = json.loads(self.rule.get("config_json") or "{}")
        except (ValueError, TypeError):
            self.config = {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(self.rule.get("name") or "")
        form.addRow("Name:", self.name_edit)
        self.class_combo = QComboBox()
        for value in schema.CRITERION_CLASSES:
            self.class_combo.addItem(schema.CRITERION_LABELS[value], value)
        index = self.class_combo.findData(self.rule.get("criterion_class")
                                          or schema.CRITERION_PROJECT)
        self.class_combo.setCurrentIndex(max(0, index))
        self.class_combo.currentIndexChanged.connect(self._sync_class)
        form.addRow("Criterion class:", self.class_combo)
        self.class_note = QLabel("")
        self.class_note.setWordWrap(True)
        form.addRow(self.class_note)
        self.source_edit = QLineEdit(self.rule.get("source_ref") or "")
        self.source_edit.setPlaceholderText("Document + revision this value comes from")
        form.addRow("Source reference:", self.source_edit)
        layout.addLayout(form)

        condition = QGroupBox("Condition")
        self.condition_form = QFormLayout(condition)
        self._build_kind_form(kind)
        layout.addWidget(condition)

        zones = QGroupBox("Exclusion Area extension and Constraint Influence Zone")
        zone_form = QFormLayout(zones)
        self.extend_spin = QDoubleSpinBox()
        self.extend_spin.setRange(0.0, 100000.0)
        self.extend_spin.setSuffix(" m")
        self.extend_spin.setValue(float(self.config.get("extend_m") or 0.0))
        self.extend_spin.setToolTip(
            "Dilates the Exclusion Area footprint on both sides.")
        zone_form.addRow("Extension buffer:", self.extend_spin)
        self.influence_before = QDoubleSpinBox()
        self.influence_after = QDoubleSpinBox()
        for spin in (self.influence_before, self.influence_after):
            spin.setRange(0.0, 100000.0)
            spin.setSuffix(" m")
        self.influence_before.setValue(float(self.config.get("influence_before_m") or 0.0))
        self.influence_after.setValue(float(self.config.get("influence_after_m") or 0.0))
        zone_form.addRow("Influence before (approach):", self.influence_before)
        zone_form.addRow("Influence after (departure):", self.influence_after)
        zone_note = QLabel(
            "The Constraint Influence Zone flags candidate boundaries near the "
            "constraint; it never removes candidate length by itself.")
        zone_note.setWordWrap(True)
        zone_form.addRow(zone_note)
        layout.addWidget(zones)

        tail = QFormLayout()
        self.scope_edit = QLineEdit(_format_scope(self.config.get("scope_ranges") or []))
        self.scope_edit.setPlaceholderText("whole scope (or e.g. 12.0-45.0, 80-92)")
        tail.addRow("Applies to KP:", self.scope_edit)
        self.notes_edit = QLineEdit(self.rule.get("notes") or "")
        tail.addRow("Notes:", self.notes_edit)
        layout.addLayout(tail)

        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_class()

    def _sync_class(self) -> None:
        value = self.class_combo.currentData()
        if value == schema.CRITERION_SCREENING:
            self.class_note.setText("Flags for assessment — does not exclude.")
            self.class_note.setStyleSheet("color: #b36b00;")
        else:
            self.class_note.setText("Acts as an Exclusion Area.")
            self.class_note.setStyleSheet("color: #666;")

    # -- kind forms -----------------------------------------------------------
    def _input_combo(self, roles: Optional[List[str]] = None) -> QComboBox:
        combo = QComboBox()
        combo.addItem("(pick a registered input)", "")
        for row in self.inputs:
            role = row.get("role") or ""
            if role == schema.INPUT_ROLE_BATHY:
                continue
            if roles and role not in roles:
                continue
            combo.addItem(
                f"{row.get('layer_name') or '?'}  ({schema.INPUT_ROLE_LABELS.get(role, role)})",
                row.get("input_id"))
        index = combo.findData(self.config.get("input_id") or "")
        combo.setCurrentIndex(max(0, index))
        return combo

    def _build_kind_form(self, kind: str) -> None:
        form = self.condition_form
        config = self.config
        if kind == wb_schema.RULE_KIND_THRESHOLD:
            self.profile_combo = QComboBox()
            self.profile_combo.addItem("Water depth", "depth")
            self.profile_combo.addItem("Longitudinal slope", "slope")
            self.profile_combo.setCurrentIndex(
                1 if (config.get("profile") or "depth") == "slope" else 0)
            self.profile_combo.currentIndexChanged.connect(self._sync_threshold)
            form.addRow("Profile:", self.profile_combo)
            self.op_combo = QComboBox()
            for op in (">", ">=", "<", "<=", "between"):
                self.op_combo.addItem(op)
            index = self.op_combo.findText(config.get("op") or ">")
            self.op_combo.setCurrentIndex(max(0, index))
            self.op_combo.currentIndexChanged.connect(self._sync_threshold)
            form.addRow("Condition:", self.op_combo)
            self.value_spin = QDoubleSpinBox()
            self.value2_spin = QDoubleSpinBox()
            for spin in (self.value_spin, self.value2_spin):
                spin.setRange(-100000.0, 100000.0)
                spin.setDecimals(2)
            self.value_spin.setValue(float(config.get("value") or 0.0))
            if config.get("value2") is not None:
                self.value2_spin.setValue(float(config.get("value2")))
            form.addRow("Value:", self.value_spin)
            form.addRow("Value 2 (between):", self.value2_spin)
            self.signed_check = QCheckBox(
                "Signed slope with separate down/up-slope limits (direction-aware)")
            self.signed_check.setChecked(bool(config.get("slope_signed")))
            self.signed_check.toggled.connect(self._sync_threshold)
            form.addRow(self.signed_check)
            self.slope_note = QLabel(
                "Uses longitudinal slope along the route only (+ve = up-slope). "
                "Raster depths are evaluated at the analysis sampling interval; "
                "contour depths are linearly interpolated between their actual "
                "route crossings. Cross-route slope is not evaluated by this rule.")
            self.slope_note.setWordWrap(True)
            self.slope_note.setStyleSheet("color: #666;")
            form.addRow(self.slope_note)
            self.slope_window_spin = QDoubleSpinBox()
            self.slope_window_spin.setRange(0.0, 1000.0)
            self.slope_window_spin.setDecimals(1)
            self.slope_window_spin.setSuffix(" m")
            self.slope_window_spin.setSpecialValueText("Auto (2 × analysis step)")
            self.slope_window_spin.setToolTip(
                "Length over which slope is evaluated — set it to the burial "
                "vehicle's bearing length (plough skids / trencher tracks) so "
                "the rule sees the gradient the machine actually experiences "
                "rather than fine-scale terrain shorter than the vehicle. "
                "0 = Auto: twice the analysis sampling step. Lengths shorter "
                "than the bathymetry resolution add no real detail.")
            if config.get("slope_window_m"):
                self.slope_window_spin.setValue(float(config.get("slope_window_m")))
            form.addRow("Slope evaluation length:", self.slope_window_spin)
            self.down_spin = QDoubleSpinBox()
            self.up_spin = QDoubleSpinBox()
            for spin in (self.down_spin, self.up_spin):
                spin.setRange(0.0, 90.0)
                spin.setDecimals(1)
                spin.setSuffix(" °")
            if config.get("downslope_max_deg") is not None:
                self.down_spin.setValue(float(config.get("downslope_max_deg")))
            if config.get("upslope_max_deg") is not None:
                self.up_spin.setValue(float(config.get("upslope_max_deg")))
            form.addRow("Down-slope limit:", self.down_spin)
            form.addRow("Up-slope limit:", self.up_spin)
            self.bands_edit = QLineEdit(json.dumps(config.get("bands"))
                                        if config.get("bands") else "")
            self.bands_edit.setPlaceholderText(
                'WD-banded limits, e.g. [{"min_wd":0,"max_wd":500,"limit":10},'
                ' {"min_wd":500,"limit":6}]')
            self.bands_edit.setToolTip(
                "Optional JSON list of bands; the applicable band is selected "
                "per station by water depth (no interpolation between bands). "
                "Keys: min_wd, max_wd, limit (or downslope_limit/upslope_limit "
                "for signed slope).")
            form.addRow("WD bands (optional):", self.bands_edit)
            self._sync_threshold()
        elif kind == wb_schema.RULE_KIND_PROXIMITY:
            self.input_combo = self._input_combo(
                [schema.INPUT_ROLE_CROSSINGS_POINTS, schema.INPUT_ROLE_CROSSINGS_LINES,
                 schema.INPUT_ROLE_SOILS, schema.INPUT_ROLE_OTHER])
            form.addRow("Input:", self.input_combo)
            self.distance_spin = QDoubleSpinBox()
            self.distance_spin.setRange(0.0, 1000000.0)
            self.distance_spin.setSuffix(" m")
            self.distance_spin.setValue(float(config.get("distance_m") or 0.0))
            form.addRow("Within distance:", self.distance_spin)
            self.buffer_field_edit = QLineEdit(config.get("buffer_field") or "")
            self.buffer_field_edit.setPlaceholderText(
                "optional attribute holding a per-feature buffer (m)")
            form.addRow("Per-feature buffer field:", self.buffer_field_edit)
            self.filter_edit = QLineEdit(config.get("filter_expression") or "")
            self.filter_edit.setPlaceholderText("optional QGIS filter expression")
            form.addRow("Feature filter:", self.filter_edit)
        elif kind == wb_schema.RULE_KIND_POLYGON:
            self.input_combo = self._input_combo(
                [schema.INPUT_ROLE_SOILS, schema.INPUT_ROLE_OTHER])
            form.addRow("Input:", self.input_combo)
            self.attribute_edit = QLineEdit(config.get("attribute") or "")
            form.addRow("Attribute:", self.attribute_edit)
            self.values_edit = QLineEdit(
                ", ".join(config.get("match_values") or []))
            self.values_edit.setPlaceholderText("e.g. ROCK, BOULDERS")
            form.addRow("Match values:", self.values_edit)
        elif kind == wb_schema.RULE_KIND_KP_TABLE:
            self.input_combo = self._input_combo()
            form.addRow("Input:", self.input_combo)
            self.start_field_edit = QLineEdit(config.get("start_field") or "start_kp")
            self.end_field_edit = QLineEdit(config.get("end_field") or "end_kp")
            form.addRow("Start KP field:", self.start_field_edit)
            form.addRow("End KP field:", self.end_field_edit)
            self.filter_edit = QLineEdit(config.get("filter_expression") or "")
            form.addRow("Feature filter:", self.filter_edit)
        elif kind == wb_schema.RULE_KIND_MANUAL:
            ranges = config.get("ranges") or []
            self.ranges_edit = QLineEdit(_format_scope(ranges))
            self.ranges_edit.setPlaceholderText("e.g. 12.000-13.500, 40.2-41.0")
            form.addRow("KP ranges:", self.ranges_edit)

    def _sync_threshold(self) -> None:
        is_slope = self.profile_combo.currentData() == "slope"
        signed = is_slope and self.signed_check.isChecked()
        suffix = " °" if is_slope else " m"
        self.value_spin.setSuffix(suffix)
        self.value2_spin.setSuffix(suffix)
        self.slope_note.setVisible(is_slope)
        self.slope_window_spin.setEnabled(is_slope)
        self.signed_check.setEnabled(is_slope)
        self.down_spin.setEnabled(signed)
        self.up_spin.setEnabled(signed)
        self.op_combo.setEnabled(not signed)
        self.value_spin.setEnabled(not signed)
        self.value2_spin.setEnabled(not signed and self.op_combo.currentText() == "between")

    # -- result ---------------------------------------------------------------
    def result_rule(self) -> Dict:
        rule = dict(self.rule)
        kind = rule.get("kind") or ""
        config = dict(self.config)
        if kind == wb_schema.RULE_KIND_THRESHOLD:
            config["profile"] = self.profile_combo.currentData()
            config["op"] = self.op_combo.currentText()
            config["value"] = self.value_spin.value()
            config["value2"] = (self.value2_spin.value()
                                if self.op_combo.currentText() == "between" else None)
            config["abs"] = config.get("profile") == "slope" and not self.signed_check.isChecked()
            if config["profile"] == "slope" and self.slope_window_spin.value() > 0:
                config["slope_window_m"] = self.slope_window_spin.value()
            else:
                config.pop("slope_window_m", None)
            if config["profile"] == "slope" and self.signed_check.isChecked():
                config["slope_signed"] = True
                config["downslope_max_deg"] = self.down_spin.value() or None
                config["upslope_max_deg"] = self.up_spin.value() or None
            else:
                config.pop("slope_signed", None)
            bands_text = self.bands_edit.text().strip()
            if bands_text:
                try:
                    bands = json.loads(bands_text)
                    config["bands"] = bands if isinstance(bands, list) else None
                except ValueError:
                    config["bands"] = None
                if config.get("bands") is None:
                    config.pop("bands", None)
            else:
                config.pop("bands", None)
        elif kind == wb_schema.RULE_KIND_PROXIMITY:
            config["input_id"] = self.input_combo.currentData() or ""
            config["distance_m"] = self.distance_spin.value()
            config["mode"] = "distance"
            config["buffer_field"] = self.buffer_field_edit.text().strip()
            config["filter_expression"] = self.filter_edit.text().strip()
        elif kind == wb_schema.RULE_KIND_POLYGON:
            config["input_id"] = self.input_combo.currentData() or ""
            config["attribute"] = self.attribute_edit.text().strip()
            config["match_values"] = [v.strip() for v in
                                      self.values_edit.text().split(",") if v.strip()]
        elif kind == wb_schema.RULE_KIND_KP_TABLE:
            config["input_id"] = self.input_combo.currentData() or ""
            config["start_field"] = self.start_field_edit.text().strip() or "start_kp"
            config["end_field"] = self.end_field_edit.text().strip() or "end_kp"
            config["filter_expression"] = self.filter_edit.text().strip()
        elif kind == wb_schema.RULE_KIND_MANUAL:
            config["ranges"] = _parse_scope(self.ranges_edit.text())
        config["extend_m"] = self.extend_spin.value() or 0.0
        config["influence_before_m"] = self.influence_before.value() or 0.0
        config["influence_after_m"] = self.influence_after.value() or 0.0
        scope = _parse_scope(self.scope_edit.text())
        if scope:
            config["scope_ranges"] = scope
        else:
            config.pop("scope_ranges", None)

        criterion = self.class_combo.currentData()
        # Action follows the criterion class; an allow-action rule imported
        # from an Assessment keeps its engineer's-exception semantics.
        if (rule.get("action") or "") == wb_schema.RULE_ACTION_ALLOW:
            action = wb_schema.RULE_ACTION_ALLOW
        elif criterion == schema.CRITERION_SCREENING:
            action = wb_schema.RULE_ACTION_RISK
        else:
            action = wb_schema.RULE_ACTION_EXCLUDE
        rule.update({
            "name": self.name_edit.text().strip()
                    or _KIND_LABELS.get(kind, kind),
            "criterion_class": criterion,
            "source_ref": self.source_edit.text().strip(),
            "action": action,
            "risk_level": 2 if criterion == schema.CRITERION_SCREENING else 0,
            "methods_json": rule.get("methods_json") or json.dumps(schema.METHODS),
            "config_json": json.dumps(config),
            "notes": self.notes_edit.text(),
        })
        return rule


class RulesTab(QWidget):
    """The Exclusion stack UI; recompute is delegated to the dock."""

    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._loading = False
        self._last_results: Dict[str, List] = {}   # rule_id -> [(s, e), ...]
        self._last_verdicts: List = []

        layout = QVBoxLayout(self)
        self.overview = VerdictStrip()
        self.overview.kpClicked.connect(self.dock.goto_kp)
        layout.addWidget(self.overview)

        self.rule_table = QTableWidget(0, 4)
        self.rule_table.setHorizontalHeaderLabels(["On", "Criterion", "Fires", "Coverage"])
        self.rule_table.verticalHeader().setVisible(False)
        self.rule_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.rule_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.rule_table.setItemDelegateForColumn(FIRE_COL, FireBarDelegate(self.rule_table))
        header = self.rule_table.horizontalHeader()
        header.setSectionResizeMode(1, HEADER_RESIZE_MODE_STRETCH)
        header.setSectionResizeMode(FIRE_COL, HEADER_RESIZE_MODE_STRETCH)
        self.rule_table.itemChanged.connect(self._on_item_changed)
        self.rule_table.doubleClicked.connect(lambda _index: self._edit_rule())
        layout.addWidget(self.rule_table, 1)

        button_row = QHBoxLayout()
        self.add_button = QToolButton()
        self.add_button.setText("＋ Add criterion ▾")
        self.add_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        menu = QMenu(self.add_button)
        for kind, label in _KIND_LABELS.items():
            action = menu.addAction(label)
            action.triggered.connect(lambda _checked=False, k=kind: self._add_rule(k))
        self.add_button.setMenu(menu)
        button_row.addWidget(self.add_button)
        for label, slot in (("Edit…", self._edit_rule), ("Delete", self._delete_rule),
                            ("↑", lambda: self._move_rule(-1)),
                            ("↓", lambda: self._move_rule(1))):
            button = QPushButton(label)
            button.clicked.connect(slot)
            button_row.addWidget(button)
        button_row.addStretch(1)
        self.recompute_button = QPushButton("Recompute")
        self.recompute_button.clicked.connect(self._recompute)
        button_row.addWidget(self.recompute_button)
        layout.addLayout(button_row)

        params_row = QHBoxLayout()
        params_row.addWidget(QLabel("Min section:"))
        self.min_section_spin = QDoubleSpinBox()
        self.min_section_spin.setRange(0.0, 1000.0)
        self.min_section_spin.setDecimals(3)
        self.min_section_spin.setSuffix(" km")
        params_row.addWidget(self.min_section_spin)
        params_row.addWidget(QLabel("Sliver tolerance:"))
        self.sliver_spin = QDoubleSpinBox()
        self.sliver_spin.setRange(0.0, 100.0)
        self.sliver_spin.setDecimals(3)
        self.sliver_spin.setSuffix(" km")
        params_row.addWidget(self.sliver_spin)
        params_row.addWidget(QLabel("Sample step:"))
        self.step_spin = QSpinBox()
        self.step_spin.setRange(5, 5000)
        self.step_spin.setSuffix(" m")
        params_row.addWidget(self.step_spin)
        refine_label = QLabel("Boundary refinement: 1 m")
        refine_label.setToolTip(
            "Coarse sampling finds where conditions change; each boundary is "
            "then refined by bisection to 1 m. A feature narrower than the "
            "sample step between stations can still be missed — reduce the "
            "step or split the scope for local refinement where that matters.")
        params_row.addWidget(refine_label)
        params_row.addStretch(1)
        self.save_params_button = QPushButton("Apply parameters")
        self.save_params_button.clicked.connect(self._save_params)
        params_row.addWidget(self.save_params_button)
        layout.addLayout(params_row)

        io_row = QHBoxLayout()
        for label, slot in (("Import from Assessment…", self._import_from_assessment),
                            ("Import rule set JSON…", self._import_json),
                            ("Export rule set JSON…", self._export_json)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            io_row.addWidget(button)
        io_row.addStretch(1)
        self.status_label = QLabel("")
        io_row.addWidget(self.status_label)
        layout.addLayout(io_row)

        model.planChanged.connect(self.refresh)
        model.rulesChanged.connect(self.refresh)
        self.refresh()

    # -- refresh --------------------------------------------------------------
    def refresh(self) -> None:
        self._loading = True
        try:
            plan = self.model.plan
            params = self.model.gen_params()
            self.min_section_spin.setValue(params.min_section_km)
            self.sliver_spin.setValue(params.sliver_tol_km)
            self.step_spin.setValue(int(params.coarse_step_m))
            rules = self.model.rules
            self.rule_table.setRowCount(len(rules))
            scope = params.scope
            domain_km = scope.length_km if plan else 0.0
            for i, rule in enumerate(rules):
                on_item = QTableWidgetItem()
                on_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                                 | Qt.ItemFlag.ItemIsSelectable)
                on_item.setCheckState(Qt.CheckState.Checked if int(rule.get("enabled") or 0)
                                      else Qt.CheckState.Unchecked)
                on_item.setData(ITEM_DATA_USER_ROLE, rule.get("rule_id"))
                self.rule_table.setItem(i, 0, on_item)

                badge = _CLASS_BADGES.get(rule.get("criterion_class") or "", "")
                text = f"[{badge}] {rule.get('name') or ''}" if badge else (rule.get("name") or "")
                if rule.get("criterion_class") == schema.CRITERION_SCREENING:
                    text += "  — flags for assessment, does not exclude"
                name_item = QTableWidgetItem(text)
                name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                tooltip = f"{schema.CRITERION_LABELS.get(rule.get('criterion_class') or '', '')}"
                if rule.get("source_ref"):
                    tooltip += f"\nSource: {rule.get('source_ref')}"
                name_item.setToolTip(tooltip.strip())
                self.rule_table.setItem(i, 1, name_item)

                fire_item = QTableWidgetItem()
                fire_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                intervals = self._last_results.get(str(rule.get("rule_id")), [])
                color = (ACTION_COLORS[wb_schema.RULE_ACTION_RISK]
                         if rule.get("criterion_class") == schema.CRITERION_SCREENING
                         else ACTION_COLORS.get(rule.get("action") or "", QColor("#888")))
                fire_item.setData(ITEM_DATA_USER_ROLE,
                                  (domain_km, intervals, color, scope.start_km))
                self.rule_table.setItem(i, FIRE_COL, fire_item)

                covered = sum(e - s for s, e in intervals)
                pct = 100.0 * covered / domain_km if domain_km > 0 else 0.0
                coverage_item = QTableWidgetItem(
                    f"{covered:.2f} km · {pct:.0f}%" if intervals else "—")
                coverage_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.rule_table.setItem(i, 3, coverage_item)
            self._refresh_overview()
        finally:
            self._loading = False

    def _refresh_overview(self) -> None:
        params = self.model.gen_params()
        scope = params.scope
        spans = []
        from ...workbench.kp_bars import STATUS_COLORS

        for verdict in self._last_verdicts:
            color = STATUS_COLORS.get(verdict.status)
            if color is not None and verdict.status in (STATUS_EXCLUDED, STATUS_RISK):
                spans.append((verdict.start_km, verdict.end_km, color))
        method_label = schema.METHOD_LABELS.get(self.model.method, self.model.method)
        self.overview.set_spans(scope.length_km, spans, method_label,
                                domain_start_km=scope.start_km)

    def set_results(self, rule_hits: Dict[str, List], verdicts: List,
                    message: str = "") -> None:
        """Called by the dock when a background analysis lands."""
        self._last_results = rule_hits
        self._last_verdicts = verdicts
        self.status_label.setText(message)
        self.refresh()

    def set_progress(self, message: str) -> None:
        self.status_label.setText(message)

    # -- edits ----------------------------------------------------------------
    def _selected_index(self) -> int:
        return self.rule_table.currentRow()

    def _add_rule(self, kind: str) -> None:
        if not self.model.plan:
            return
        rule = {
            "rule_id": schema.new_id(),
            "plan_id": self.model.plan_id,
            "name": "",
            "enabled": 1,
            "kind": kind,
            "action": wb_schema.RULE_ACTION_EXCLUDE,
            "risk_level": 0,
            "criterion_class": schema.CRITERION_PROJECT,
            "source_ref": "",
            "methods_json": json.dumps([self.model.method]),
            "config_json": "{}",
            "notes": "",
        }
        dialog = RuleEditorDialog(rule, self.model.inputs, self.model.method, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            rules = list(self.model.rules) + [dialog.result_rule()]
            self.model.save_rules(rules, target_id=rule["rule_id"])
            self._recompute()

    def _edit_rule(self) -> None:
        index = self._selected_index()
        if index < 0 or index >= len(self.model.rules):
            return
        dialog = RuleEditorDialog(self.model.rules[index], self.model.inputs,
                                  self.model.method, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            rules = list(self.model.rules)
            rules[index] = dialog.result_rule()
            self.model.save_rules(rules, target_id=str(rules[index].get("rule_id")))
            self._recompute()

    def _delete_rule(self) -> None:
        index = self._selected_index()
        if index < 0 or index >= len(self.model.rules):
            return
        rule = self.model.rules[index]
        answer = QMessageBox.question(
            self, "Delete criterion", f"Delete '{rule.get('name') or 'criterion'}'?",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer != MESSAGE_BOX_YES:
            return
        rules = [r for i, r in enumerate(self.model.rules) if i != index]
        self.model.save_rules(rules, target_id=str(rule.get("rule_id")),
                              action=change_log.ACTION_DELETE_RULE)
        self._recompute()

    def _move_rule(self, delta: int) -> None:
        index = self._selected_index()
        rules = list(self.model.rules)
        target = index + delta
        if index < 0 or target < 0 or target >= len(rules):
            return
        rules[index], rules[target] = rules[target], rules[index]
        self.model.save_rules(rules, target_id=str(rules[target].get("rule_id")))
        self.rule_table.selectRow(target)
        self._recompute()

    def _on_item_changed(self, item) -> None:
        if self._loading or item.column() != 0:
            return
        index = item.row()
        rules = list(self.model.rules)
        if index >= len(rules):
            return
        rules[index] = dict(rules[index])
        rules[index]["enabled"] = 1 if item.checkState() == Qt.CheckState.Checked else 0
        self.model.save_rules(rules, target_id=str(rules[index].get("rule_id")))
        self._recompute()

    def _save_params(self) -> None:
        if not self.model.plan:
            return
        self.model.update_plan({"params_json": json.dumps({
            "min_section_km": self.min_section_spin.value(),
            "sliver_tol_km": self.sliver_spin.value(),
            "coarse_step_m": float(self.step_spin.value()),
            "refine_tol_m": 1.0,
        })}, reason="analysis parameters")
        self._recompute()

    def _recompute(self) -> None:
        self.dock.request_analysis()

    # -- rule-set IO ----------------------------------------------------------
    def _import_from_assessment(self) -> None:
        store = self.dock.workbench_store()
        if store is None:
            QMessageBox.information(self, "Burial Planner",
                                    "No Workbench GeoPackage found in this project.")
            return
        try:
            rule_sets = store.list_rule_sets()
        except Exception:
            rule_sets = []
        if not rule_sets:
            QMessageBox.information(self, "Burial Planner",
                                    "The Workbench has no rule sets to import.")
            return
        names = [rs.get("name") or "?" for rs in rule_sets]
        name, ok = QInputDialog.getItem(self, "Import rule set",
                                        "Workbench rule set:", names, 0, False)
        if not ok:
            return
        rule_set = rule_sets[names.index(name)]
        wb_rules = store.list_rules(rule_set.get("rule_set_id") or "")
        imported = []
        for row in wb_rules:
            imported.append({
                "rule_id": schema.new_id(),
                "plan_id": self.model.plan_id,
                "name": row.get("name") or "",
                "enabled": int(row.get("enabled") or 0),
                "kind": row.get("kind") or "",
                "action": row.get("action") or wb_schema.RULE_ACTION_EXCLUDE,
                "risk_level": int(row.get("risk_level") or 0),
                "criterion_class": (schema.CRITERION_SCREENING
                                    if (row.get("action") or "") == wb_schema.RULE_ACTION_RISK
                                    else schema.CRITERION_PROJECT),
                "source_ref": "",
                "methods_json": row.get("methods_json") or "[]",
                "config_json": row.get("config_json") or "{}",
                "notes": row.get("notes") or "",
            })
        self.model.save_rules(list(self.model.rules) + imported,
                              target_id="assessment_import")
        self._recompute()

    def _export_json(self) -> None:
        if not self.model.rules:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export rule set", "", "JSON (*.json)")
        if not path:
            return
        payload = {
            "format": "subsea_cable_tools.burial.rule_set",
            "version": 1,
            "method": self.model.method,
            "rules": [{k: v for k, v in rule.items()
                       if k not in ("plan_id",)} for rule in self.model.rules],
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        self.status_label.setText(f"Exported {len(self.model.rules)} criteria.")

    def _import_json(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import rule set", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            rows = payload.get("rules") or []
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"The rule set could not be read: {exc}")
            return
        imported = []
        for row in rows:
            row = dict(row)
            row["rule_id"] = schema.new_id()
            row["plan_id"] = self.model.plan_id
            row.setdefault("criterion_class", schema.CRITERION_PROJECT)
            row.setdefault("source_ref", "")
            imported.append(row)
        if imported:
            self.model.save_rules(list(self.model.rules) + imported,
                                  target_id="json_import")
            self._recompute()
