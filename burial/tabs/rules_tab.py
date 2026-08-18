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

from qgis.PyQt.QtCore import QSettings, Qt, QTimer
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
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    CONTEXT_MENU_POLICY_CUSTOM,
    DIALOG_ACCEPTED,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
    SELECTION_MODE_SINGLE,
    TOOLBUTTON_POPUP_MODE_INSTANT,
    qt_exec,
)
from ...workbench import schema as wb_schema
from ...workbench.kp_bars import ACTION_COLORS, FireBarDelegate, VerdictStrip
from ...workbench.rules_engine import STATUS_EXCLUDED, STATUS_RISK
from .. import change_log, generation, profile_data, schema
from .. import ui_helpers

FIRE_COL = 2

_KIND_LABELS = {
    wb_schema.RULE_KIND_THRESHOLD: "Water depth / slope threshold",
    wb_schema.RULE_KIND_PROXIMITY: "Crossings / proximity",
    wb_schema.RULE_KIND_POLYGON: "Seabed soils / polygon class",
    wb_schema.RULE_KIND_KP_TABLE: "KP range table",
    wb_schema.RULE_KIND_MANUAL: "Manual ranges",
}

# Add-menu entries. Water depth and slope share the threshold_profile kind
# (schema/engine unchanged) but edit in their own dialogs — the preset
# ``profile`` picks the variant.
_ADD_MENU = (
    ("Water depth", wb_schema.RULE_KIND_THRESHOLD, {"profile": "depth"}),
    ("Slope", wb_schema.RULE_KIND_THRESHOLD, {"profile": "slope"}),
    ("Crossings / proximity", wb_schema.RULE_KIND_PROXIMITY, None),
    ("Seabed soils / polygon class", wb_schema.RULE_KIND_POLYGON, None),
    ("KP range table", wb_schema.RULE_KIND_KP_TABLE, None),
    ("Manual ranges", wb_schema.RULE_KIND_MANUAL, None),
)

_CLASS_BADGES = {
    schema.CRITERION_NON_DEVIABLE: "ND",
    schema.CRITERION_PROJECT: "PR",
    schema.CRITERION_SCREENING: "SC",
}

_BADGE_LEGEND = ("Criterion class: [ND] Non-Deviable Requirement, "
                 "[PR] Project Exclusion Criterion, [SC] Screening "
                 "Criterion (flags for assessment — does not exclude).")

_VERTICAL = getattr(Qt, "Orientation", Qt).Vertical

_SHOW_RESOLVED_SETTINGS_KEY = \
    "SubseaCableTools/BurialPlanner/rules_show_resolved"

_RESOLVED_COLUMNS = ["Start KP", "End KP", "Length (km)", "Status",
                     "Dominant criterion", "Triggered by"]
_BY_RULE_COLUMNS = ["Criterion", "Start KP", "End KP", "Length (km)",
                    "Status"]

_RESOLVED_VIEW_SETTINGS_KEY = \
    "SubseaCableTools/BurialPlanner/rules_resolved_view"


def verdict_row_values(verdict, rule_names: Dict[str, str]) -> List[str]:
    """One resolved excluded/flagged range as display strings."""
    names = [rule_names.get(rid, rid)
             for rid in (verdict.fired_rule_ids or [])]
    dominant = rule_names.get(verdict.dominant_rule_id or "",
                              verdict.dominant_rule_id or "")
    status = ("Excluded" if verdict.status == STATUS_EXCLUDED
              else "Flagged (screening)")
    return [
        schema.format_kp(verdict.start_km),
        schema.format_kp(verdict.end_km),
        schema.format_kp(verdict.end_km - verdict.start_km),
        status,
        dominant,
        ", ".join(n for n in names if n),
    ]


def _parse_scope_strict(text: str):
    """(ranges, malformed_chunks) — so typos are reported, not dropped."""
    ranges: List[Dict] = []
    bad: List[str] = []
    for chunk in (text or "").replace(";", ",").split(","):
        raw = chunk.strip()
        chunk = raw.replace("–", "-").replace("..", "-")
        if not chunk:
            continue
        parts = chunk.split("-")
        if len(parts) == 2:
            try:
                ranges.append({"start_kp": float(parts[0]),
                               "end_kp": float(parts[1])})
                continue
            except ValueError:
                pass
        bad.append(raw)
    return ranges, bad


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


def _normalised_methods_json(methods_json) -> str:
    """methods_json with alias method ids mapped to the planner vocabulary."""
    try:
        methods = json.loads(methods_json or "[]")
    except (ValueError, TypeError):
        methods = []
    if not isinstance(methods, list):
        methods = []
    return json.dumps(schema.normalise_methods(methods))


class RuleEditorDialog(QDialog):
    """Edit one bp_rule row (kind fixed at creation)."""

    def __init__(self, rule: Dict, inputs: List[Dict], method: str, parent=None):
        super().__init__(parent)
        self.rule = dict(rule)
        self.inputs = inputs
        kind = self.rule.get("kind") or ""
        try:
            self.config = json.loads(self.rule.get("config_json") or "{}")
        except (ValueError, TypeError):
            self.config = {}
        # Water depth and slope share the stored kind but edit separately.
        self.threshold_profile = ""
        if kind == wb_schema.RULE_KIND_THRESHOLD:
            self.threshold_profile = \
                "slope" if (self.config.get("profile") or "depth") == "slope" \
                else "depth"
        if self.threshold_profile:
            title = "Slope" if self.threshold_profile == "slope" else "Water depth"
        else:
            title = _KIND_LABELS.get(kind, kind)
        self.setWindowTitle(f"Exclusion criterion — {title}")
        # The slope form carries the WD-band grid; give it room.
        self.setMinimumWidth(620 if self.threshold_profile == "slope" else 460)

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
        ext = generation.extension_config(self.config)
        self.extend_mode_combo = QComboBox()
        self.extend_mode_combo.addItem("Fixed distance (m)",
                                       generation.EXTEND_MODE_FIXED)
        self.extend_mode_combo.addItem("Water-depth multiple (×WD)",
                                       generation.EXTEND_MODE_WD)
        mode_index = self.extend_mode_combo.findData(ext["mode"])
        self.extend_mode_combo.setCurrentIndex(max(0, mode_index))
        self.extend_mode_combo.currentIndexChanged.connect(self._sync_extension)
        zone_form.addRow("Extension basis:", self.extend_mode_combo)
        self.extend_before = QDoubleSpinBox()
        self.extend_after = QDoubleSpinBox()
        for spin in (self.extend_before, self.extend_after):
            spin.setRange(0.0, 100000.0)
            spin.setDecimals(2)
        self.extend_before.setValue(float(ext["before"]))
        self.extend_after.setValue(float(ext["after"]))
        zone_form.addRow("Extend before (approach):", self.extend_before)
        zone_form.addRow("Extend after (departure):", self.extend_after)
        extend_note = QLabel(
            "Extends the Exclusion Area beyond the detected footprint. "
            "Before/after follow the direction of installation. A "
            "water-depth multiple scales with the depth at the footprint "
            "boundary (e.g. 1.0 ×WD = one water depth each time).")
        extend_note.setWordWrap(True)
        extend_note.setStyleSheet(ui_helpers.hint_style())
        zone_form.addRow(extend_note)
        self._sync_extension()
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
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_class()

    def _accept(self) -> None:
        """Reject malformed KP ranges / band values instead of dropping them."""
        _ranges, bad = _parse_scope_strict(self.scope_edit.text())
        if bad:
            QMessageBox.warning(
                self, "Burial Planner",
                "These 'Applies to KP' entries are not valid ranges and "
                "would be ignored: " + ", ".join(bad[:5])
                + ". Use start-end pairs like 12.0-45.0.")
            return
        if hasattr(self, "ranges_edit"):
            _ranges, bad = _parse_scope_strict(self.ranges_edit.text())
            if bad:
                QMessageBox.warning(
                    self, "Burial Planner",
                    "These KP ranges are not valid and would be ignored: "
                    + ", ".join(bad[:5])
                    + ". Use start-end pairs like 12.000-13.500.")
                return
        if hasattr(self, "bands_table"):
            bad_cells = []
            for row in range(self.bands_table.rowCount()):
                for column, _key in enumerate(self._BAND_COLUMN_KEYS):
                    item = self.bands_table.item(row, column)
                    text = (item.text() if item is not None else "").strip()
                    if not text:
                        continue
                    try:
                        float(text.replace(",", "."))
                    except ValueError:
                        bad_cells.append(f"band {row + 1}: '{text}'")
            if bad_cells:
                QMessageBox.warning(
                    self, "Burial Planner",
                    "These WD-band values are not numbers and would be "
                    "ignored: " + ", ".join(bad_cells[:5]) + ".")
                return
        self.accept()

    def _sync_corridor(self) -> None:
        mode = self.corridor_combo.currentData() or ""
        self.corridor_spin.setEnabled(bool(mode))
        wd = mode == "wd"
        self.corridor_spin.setSuffix(" ×WD" if wd else " m")
        self.corridor_spin.setDecimals(2 if wd else 1)
        self.corridor_spin.setSingleStep(0.25 if wd else 5.0)

    def _sync_extension(self) -> None:
        wd = self.extend_mode_combo.currentData() == generation.EXTEND_MODE_WD
        for spin in (self.extend_before, self.extend_after):
            spin.setSuffix(" ×WD" if wd else " m")
            spin.setDecimals(2 if wd else 1)
            spin.setSingleStep(0.25 if wd else 10.0)

    def _sync_class(self) -> None:
        value = self.class_combo.currentData()
        if value == schema.CRITERION_SCREENING:
            self.class_note.setText("Flags for assessment — does not exclude.")
            self.class_note.setStyleSheet(
                f"color: {ui_helpers.color('warn')};")
        else:
            self.class_note.setText("Acts as an Exclusion Area.")
            self.class_note.setStyleSheet(ui_helpers.hint_style())

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
            if self.threshold_profile == "slope":
                self._build_slope_form(form, config)
            else:
                self._build_depth_form(form, config)
        elif kind == wb_schema.RULE_KIND_PROXIMITY:
            self.input_combo = self._input_combo(
                [schema.INPUT_ROLE_CROSSINGS_POINTS, schema.INPUT_ROLE_CROSSINGS_LINES,
                 schema.INPUT_ROLE_SOILS, schema.INPUT_ROLE_OTHER])
            form.addRow("Input:", self.input_combo)
            self.distance_spin = QDoubleSpinBox()
            self.distance_spin.setRange(0.0, 1000000.0)
            self.distance_spin.setSuffix(" m")
            self.distance_spin.setValue(float(config.get("distance_m") or 0.0))
            self.distance_spin.setToolTip(
                "Shortest spatial distance from each route position to the "
                "feature. For a line, 500 m means a 500 m perpendicular "
                "buffer around it; the resulting KP length may be slightly "
                "more than 1,000 m at an oblique crossing or on a curved "
                "route.")
            form.addRow("Within distance (each side of route):",
                        self.distance_spin)
            self.buffer_field_edit = QLineEdit(config.get("buffer_field") or "")
            self.buffer_field_edit.setPlaceholderText(
                "optional attribute holding a per-feature buffer (m)")
            form.addRow("Per-feature buffer field:", self.buffer_field_edit)
            self.filter_edit = QLineEdit(config.get("filter_expression") or "")
            self.filter_edit.setPlaceholderText("optional QGIS filter expression")
            form.addRow("Feature filter:", self.filter_edit)
        elif kind == wb_schema.RULE_KIND_POLYGON:
            self._build_polygon_form(form, config)
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

    def _build_depth_form(self, form: QFormLayout, config: Dict) -> None:
        self.op_combo = QComboBox()
        for op in (">", ">=", "<", "<=", "between"):
            self.op_combo.addItem(op)
        index = self.op_combo.findText(config.get("op") or ">")
        self.op_combo.setCurrentIndex(max(0, index))
        self.op_combo.currentIndexChanged.connect(self._sync_depth)
        form.addRow("Condition:", self.op_combo)
        self.value_spin = QDoubleSpinBox()
        self.value2_spin = QDoubleSpinBox()
        for spin in (self.value_spin, self.value2_spin):
            spin.setRange(0.0, 100000.0)
            spin.setDecimals(2)
            spin.setSuffix(" m")
        self.value_spin.setValue(float(config.get("value") or 0.0))
        if config.get("value2") is not None:
            self.value2_spin.setValue(float(config.get("value2")))
        form.addRow("Water depth:", self.value_spin)
        form.addRow("Value 2 (between):", self.value2_spin)
        note = QLabel(
            "Water depth magnitude (m) from the stored bathymetry profile; "
            "contour depths are linearly interpolated between their actual "
            "route crossings.")
        note.setWordWrap(True)
        note.setStyleSheet(ui_helpers.hint_style())
        form.addRow(note)
        self._sync_depth()

    def _sync_depth(self) -> None:
        self.value2_spin.setEnabled(self.op_combo.currentText() == "between")

    def _build_slope_form(self, form: QFormLayout, config: Dict) -> None:
        self.component_combo = QComboBox()
        for component in profile_data.SLOPE_COMPONENTS:
            self.component_combo.addItem(
                profile_data.SLOPE_COMPONENT_LABELS[component], component)
        index = self.component_combo.findData(
            config.get("slope_component") or profile_data.SLOPE_COMPONENT_LONG)
        self.component_combo.setCurrentIndex(max(0, index))
        self.component_combo.currentIndexChanged.connect(self._sync_threshold)
        form.addRow("Slope component:", self.component_combo)
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
            spin.setRange(0.0, 90.0)
            spin.setDecimals(2)
            spin.setSuffix(" °")
        self.value_spin.setValue(float(config.get("value") or 0.0))
        if config.get("value2") is not None:
            self.value2_spin.setValue(float(config.get("value2")))
        form.addRow("Slope angle:", self.value_spin)
        form.addRow("Value 2 (between):", self.value2_spin)
        self.signed_check = QCheckBox(
            "Signed slope with separate down/up-slope limits (direction-aware)")
        self.signed_check.setChecked(bool(config.get("slope_signed")))
        self.signed_check.toggled.connect(self._sync_threshold)
        form.addRow(self.signed_check)
        self.slope_note = QLabel("")
        self.slope_note.setWordWrap(True)
        self.slope_note.setStyleSheet(ui_helpers.hint_style())
        form.addRow(self.slope_note)
        self.slope_window_spin = QDoubleSpinBox()
        self.slope_window_spin.setRange(0.0, 1000.0)
        self.slope_window_spin.setDecimals(1)
        self.slope_window_spin.setSuffix(" m")
        self.slope_window_spin.setSpecialValueText("Auto (2 × profile step)")
        self.slope_window_spin.setToolTip(
            "Length over which slope is evaluated — set it to the burial "
            "vehicle's bearing length (plough skids / trencher tracks) so "
            "the rule sees the gradient the machine actually experiences "
            "rather than local terrain shorter than the vehicle. "
            "0 = Auto: local terrain slope over twice the stored profile "
            "station step. Lengths shorter than the bathymetry resolution "
            "add no real detail. Cross slope ignores this — it is always "
            "the difference across the sampled ± cross offset.")
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
        self.bands_table = QTableWidget(0, 5)
        self.bands_table.setHorizontalHeaderLabels(
            ["Min WD (m)", "Max WD (m)", "Limit (°)",
             "Down-slope limit (°)", "Up-slope limit (°)"])
        self.bands_table.verticalHeader().setVisible(False)
        self.bands_table.setMinimumHeight(96)
        self.bands_table.setMaximumHeight(150)
        self.bands_table.horizontalHeader().setSectionResizeMode(
            HEADER_RESIZE_MODE_STRETCH)
        self.bands_table.setToolTip(
            "Optional water-depth-banded slope limits — for burial tools "
            "whose slope capability changes with depth. Per station the "
            "first band whose [Min WD, Max WD) contains the water depth "
            "applies (no interpolation between bands). Leave Min/Max blank "
            "to leave that side open; signed slope can instead set separate "
            "down/up-slope limits (Limit is the fallback for both).")
        for band in config.get("bands") or []:
            self._append_band_row(band)
        form.addRow("WD-banded limits (optional):", self.bands_table)
        bands_buttons = QHBoxLayout()
        add_band = QPushButton("＋ Add band")
        add_band.clicked.connect(lambda: self._append_band_row({}))
        bands_buttons.addWidget(add_band)
        remove_band = QPushButton("− Remove band")
        remove_band.clicked.connect(self._remove_band_row)
        bands_buttons.addWidget(remove_band)
        bands_buttons.addStretch(1)
        form.addRow("", bands_buttons)
        self._sync_threshold()

    def _build_polygon_form(self, form: QFormLayout, config: Dict) -> None:
        self.input_combo = self._input_combo(
            [schema.INPUT_ROLE_SOILS, schema.INPUT_ROLE_OTHER])
        form.addRow("Input:", self.input_combo)
        self.attribute_edit = QLineEdit(config.get("attribute") or "")
        form.addRow("Attribute:", self.attribute_edit)
        self.values_edit = QLineEdit(
            ", ".join(config.get("match_values") or []))
        self.values_edit.setPlaceholderText("e.g. ROCK, BOULDERS")
        form.addRow("Match values:", self.values_edit)
        self.corridor_combo = QComboBox()
        self.corridor_combo.addItem("Route centreline only (default)", "")
        self.corridor_combo.addItem("Within fixed distance of route", "fixed")
        self.corridor_combo.addItem(
            "Within water-depth multiple of route (×WD)", "wd")
        corridor_index = self.corridor_combo.findData(
            (config.get("route_buffer_mode") or "").lower())
        self.corridor_combo.setCurrentIndex(max(0, corridor_index))
        self.corridor_combo.currentIndexChanged.connect(self._sync_corridor)
        form.addRow("Search from:", self.corridor_combo)
        self.corridor_spin = QDoubleSpinBox()
        self.corridor_spin.setRange(0.0, 100000.0)
        mode = (config.get("route_buffer_mode") or "").lower()
        self.corridor_spin.setValue(
            float(config.get("route_buffer_wd") or 0.0) if mode == "wd"
            else float(config.get("route_buffer_m") or 0.0))
        self.corridor_spin.setToolTip(
            "Measured from the route centreline to the polygon, so it "
            "applies each side of the route — the full search corridor is "
            "twice this value (e.g. 10 m here checks a 20 m wide corridor). "
            "×WD scales the distance with the water depth at each station "
            "and needs a bathymetry source.")
        form.addRow("Distance each side of route:", self.corridor_spin)
        self._sync_corridor()

    _BAND_COLUMN_KEYS = ("min_wd", "max_wd", "limit",
                         "downslope_limit", "upslope_limit")

    def _append_band_row(self, band: Dict) -> None:
        row = self.bands_table.rowCount()
        self.bands_table.insertRow(row)
        for column, key in enumerate(self._BAND_COLUMN_KEYS):
            value = band.get(key)
            text = "" if value is None else f"{float(value):g}"
            self.bands_table.setItem(row, column, QTableWidgetItem(text))

    def _remove_band_row(self) -> None:
        row = self.bands_table.currentRow()
        if row < 0:
            row = self.bands_table.rowCount() - 1
        if row >= 0:
            self.bands_table.removeRow(row)

    def _bands_from_table(self) -> List[Dict]:
        bands: List[Dict] = []
        for row in range(self.bands_table.rowCount()):
            band: Dict = {}
            for column, key in enumerate(self._BAND_COLUMN_KEYS):
                item = self.bands_table.item(row, column)
                text = (item.text() if item is not None else "").strip()
                if not text:
                    continue
                try:
                    band[key] = float(text.replace(",", "."))
                except ValueError:
                    continue
            if band:
                bands.append(band)
        return bands

    _COMPONENT_NOTES = {
        profile_data.SLOPE_COMPONENT_LONG:
            "Longitudinal slope along the route (+ve = up-slope) from the "
            "stored bathymetry profile; contour depths are interpolated "
            "between their actual route crossings. Tick signed limits to "
            "set separate down/up-slope maxima (direction-of-installation "
            "aware).",
        profile_data.SLOPE_COMPONENT_CROSS:
            "Cross slope from the profile's ± cross-offset samples (two-"
            "point difference across the offset). The limit applies to the "
            "magnitude — leaning to port or starboard both count. Needs a "
            "profile sampled with a cross offset (Bathymetry Profile tab).",
        profile_data.SLOPE_COMPONENT_ABSOLUTE:
            "Absolute slope: magnitude of the combined longitudinal + cross "
            "gradient, matching the profile pane's Absolute trace. Where "
            "cross samples are missing it falls back to |longitudinal| "
            "(a lower bound). Needs a profile sampled with a cross offset.",
    }

    def _sync_threshold(self) -> None:
        component = self.component_combo.currentData() \
            or profile_data.SLOPE_COMPONENT_LONG
        is_long = component == profile_data.SLOPE_COMPONENT_LONG
        signed = is_long and self.signed_check.isChecked()
        self.slope_note.setText(self._COMPONENT_NOTES.get(component, ""))
        self.signed_check.setEnabled(is_long)
        self.down_spin.setEnabled(signed)
        self.up_spin.setEnabled(signed)
        self.op_combo.setEnabled(not signed)
        self.value_spin.setEnabled(not signed)
        self.value2_spin.setEnabled(not signed and self.op_combo.currentText() == "between")
        # Cross is a fixed two-point difference across the sampled offset.
        self.slope_window_spin.setEnabled(
            component != profile_data.SLOPE_COMPONENT_CROSS)
        # The directional band limits only mean anything for signed slope.
        self.bands_table.setColumnHidden(3, not signed)
        self.bands_table.setColumnHidden(4, not signed)

    # -- result ---------------------------------------------------------------
    def result_rule(self) -> Dict:
        rule = dict(self.rule)
        kind = rule.get("kind") or ""
        config = dict(self.config)
        if kind == wb_schema.RULE_KIND_THRESHOLD:
            config["profile"] = self.threshold_profile or "depth"
            config["op"] = self.op_combo.currentText()
            config["value"] = self.value_spin.value()
            config["value2"] = (self.value2_spin.value()
                                if self.op_combo.currentText() == "between" else None)
            if self.threshold_profile == "slope":
                component = self.component_combo.currentData() \
                    or profile_data.SLOPE_COMPONENT_LONG
                if component == profile_data.SLOPE_COMPONENT_LONG:
                    config.pop("slope_component", None)
                else:
                    config["slope_component"] = component
                is_long = component == profile_data.SLOPE_COMPONENT_LONG
                signed = is_long and self.signed_check.isChecked()
                config["abs"] = not signed
                if self.slope_window_spin.value() > 0 \
                        and component != profile_data.SLOPE_COMPONENT_CROSS:
                    config["slope_window_m"] = self.slope_window_spin.value()
                else:
                    config.pop("slope_window_m", None)
                if signed:
                    config["slope_signed"] = True
                    config["downslope_max_deg"] = self.down_spin.value() or None
                    config["upslope_max_deg"] = self.up_spin.value() or None
                else:
                    config.pop("slope_signed", None)
                bands = self._bands_from_table()
                if bands:
                    config["bands"] = bands
                else:
                    config.pop("bands", None)
            else:
                config["abs"] = False
                config.pop("slope_signed", None)
                config.pop("slope_component", None)
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
            corridor_mode = self.corridor_combo.currentData() or ""
            corridor_value = self.corridor_spin.value()
            for key in ("route_buffer_mode", "route_buffer_m", "route_buffer_wd"):
                config.pop(key, None)
            if corridor_mode and corridor_value > 0:
                config["route_buffer_mode"] = corridor_mode
                config["route_buffer_wd" if corridor_mode == "wd"
                       else "route_buffer_m"] = corridor_value
        elif kind == wb_schema.RULE_KIND_KP_TABLE:
            config["input_id"] = self.input_combo.currentData() or ""
            config["start_field"] = self.start_field_edit.text().strip() or "start_kp"
            config["end_field"] = self.end_field_edit.text().strip() or "end_kp"
            config["filter_expression"] = self.filter_edit.text().strip()
        elif kind == wb_schema.RULE_KIND_MANUAL:
            config["ranges"] = _parse_scope(self.ranges_edit.text())
        extend_mode = self.extend_mode_combo.currentData()
        for key in generation.EXTENSION_CONFIG_KEYS:
            config.pop(key, None)
        config["extend_mode"] = extend_mode
        suffix = "wd" if extend_mode == generation.EXTEND_MODE_WD else "m"
        config[f"extend_before_{suffix}"] = self.extend_before.value() or 0.0
        config[f"extend_after_{suffix}"] = self.extend_after.value() or 0.0
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
        if kind == wb_schema.RULE_KIND_THRESHOLD:
            default_name = ("Slope" if self.threshold_profile == "slope"
                            else "Water depth")
        else:
            default_name = _KIND_LABELS.get(kind, kind)
        rule.update({
            "name": self.name_edit.text().strip() or default_name,
            "criterion_class": criterion,
            "source_ref": self.source_edit.text().strip(),
            "action": action,
            "risk_level": 2 if criterion == schema.CRITERION_SCREENING else 0,
            # "[]" is the explicit all-methods marker: stamping the full
            # METHODS list would silently narrow the rule the next time a
            # method is added to the plugin (schema v6 healed old rows).
            "methods_json": rule.get("methods_json") or "[]",
            "config_json": json.dumps(config),
            "notes": self.notes_edit.text(),
        })
        return rule


class ExcludedSectionsDialog(QDialog):
    """Resolved excluded / flagged KP ranges with their triggering criteria.

    The Exclusions tab now embeds this review as the resolved-exclusions
    table; the dialog remains for programmatic use and as a pop-out view.
    Double-click (or the Go to button) zooms map + profile to the range;
    Export CSV writes the same rows with the criteria names.
    """

    _COLUMNS = _RESOLVED_COLUMNS

    def __init__(self, verdicts: List, rule_names: Dict[str, str],
                 dock, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Excluded sections")
        self.resize(760, 420)
        self.dock = dock
        self.verdicts = sorted(verdicts, key=lambda v: (v.start_km, v.end_km))
        self.rule_names = rule_names

        layout = QVBoxLayout(self)
        self.table = QTableWidget(len(self.verdicts), len(self._COLUMNS))
        self.table.setHorizontalHeaderLabels(self._COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(len(self._COLUMNS) - 1,
                                    HEADER_RESIZE_MODE_STRETCH)
        for i, verdict in enumerate(self.verdicts):
            for j, value in enumerate(self._row_values(verdict)):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
                if j == 5:
                    item.setToolTip(value)
                self.table.setItem(i, j, item)
        self.table.cellDoubleClicked.connect(
            lambda row, _column: self._goto_row(row))
        layout.addWidget(self.table, 1)

        button_row = QHBoxLayout()
        goto_button = QPushButton("Go to on map")
        goto_button.clicked.connect(
            lambda: self._goto_row(self.table.currentRow()))
        button_row.addWidget(goto_button)
        export_button = QPushButton("Export CSV…")
        export_button.clicked.connect(self._export_csv)
        button_row.addWidget(export_button)
        button_row.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

    def _row_values(self, verdict) -> List[str]:
        return verdict_row_values(verdict, self.rule_names)

    def _goto_row(self, row: int) -> None:
        if 0 <= row < len(self.verdicts):
            verdict = self.verdicts[row]
            self.dock.goto_range(verdict.start_km, verdict.end_km)

    def _export_csv(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export excluded sections", "excluded_sections.csv",
            "CSV (*.csv)")
        if not path:
            return
        import csv

        headers = ["start_kp", "end_kp", "length_km", "status",
                   "dominant_criterion", "triggered_by"]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            for verdict in self.verdicts:
                writer.writerow(self._row_values(verdict))


class RulesTab(QWidget):
    """The Exclusion stack UI; recompute is delegated to the dock."""

    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._loading = False
        self._loaded_plan_id: Optional[str] = None
        self._params_dirty = False  # unapplied Sample-step/Sliver edits
        self._last_results: Dict[str, List] = {}   # rule_id -> [(s, e), ...]
        self._last_verdicts: List = []
        # Debounce enable-checkbox toggles: several quick toggles trigger
        # one recompute instead of queuing one per click.
        self._recompute_timer = QTimer(self)
        self._recompute_timer.setSingleShot(True)
        self._recompute_timer.setInterval(400)
        self._recompute_timer.timeout.connect(self._recompute)

        layout = QVBoxLayout(self)
        self.overview = VerdictStrip()
        self.overview.kpClicked.connect(self.dock.goto_kp)
        layout.addWidget(self.overview)

        self.rule_table = QTableWidget(0, 4)
        self.rule_table.setHorizontalHeaderLabels(
            ["On", "Criterion", "Excluded sections", "Coverage"])
        criterion_header = self.rule_table.horizontalHeaderItem(1)
        if criterion_header is not None:
            criterion_header.setToolTip(_BADGE_LEGEND)
        self.rule_table.verticalHeader().setVisible(False)
        self.rule_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        # Extended selection so several criteria can be deleted at once;
        # Edit / move act on the current row.
        self.rule_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.rule_table.setItemDelegateForColumn(FIRE_COL, FireBarDelegate(self.rule_table))
        header = self.rule_table.horizontalHeader()
        header.setSectionResizeMode(1, HEADER_RESIZE_MODE_STRETCH)
        header.setSectionResizeMode(FIRE_COL, HEADER_RESIZE_MODE_STRETCH)
        self.rule_table.itemChanged.connect(self._on_item_changed)
        self.rule_table.doubleClicked.connect(lambda _index: self._edit_rule())
        self.rule_table.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.rule_table.customContextMenuRequested.connect(self._rule_context_menu)

        splitter = QSplitter(_VERTICAL)
        splitter.addWidget(self.rule_table)

        self.resolved_widget = QWidget()
        resolved_layout = QVBoxLayout(self.resolved_widget)
        resolved_layout.setContentsMargins(0, 0, 0, 0)
        resolved_header = QHBoxLayout()
        self.resolved_label = QLabel("Resolved exclusions")
        self.resolved_label.setToolTip(
            "The excluded / flagged KP ranges after resolving the whole "
            "stack, with the criteria that triggered them. Click a row to "
            "highlight the range; double-click to zoom map and profile.")
        resolved_header.addWidget(self.resolved_label)
        resolved_header.addStretch(1)
        self.resolved_view_combo = QComboBox()
        self.resolved_view_combo.addItem("Grouped (resolved ranges)", "grouped")
        self.resolved_view_combo.addItem("By criterion", "by_rule")
        self.resolved_view_combo.setToolTip(
            "Grouped shows each resolved range once with every criterion "
            "that triggered it. By criterion breaks the ranges down to one "
            "row per criterion per range, so overlapping criteria can be "
            "reviewed independently.")
        stored_view = QSettings().value(_RESOLVED_VIEW_SETTINGS_KEY,
                                        "grouped", type=str)
        self.resolved_view_combo.setCurrentIndex(
            max(0, self.resolved_view_combo.findData(stored_view)))
        self.resolved_view_combo.currentIndexChanged.connect(
            self._on_resolved_view_changed)
        resolved_header.addWidget(self.resolved_view_combo)
        resolved_export = QPushButton("Export CSV…")
        resolved_export.setToolTip(
            "Write the resolved excluded / flagged ranges with their "
            "triggering criteria to a CSV file.")
        resolved_export.clicked.connect(self._export_resolved_csv)
        resolved_header.addWidget(resolved_export)
        resolved_layout.addLayout(resolved_header)
        self.resolved_table = QTableWidget(0, len(_RESOLVED_COLUMNS))
        self.resolved_table.setHorizontalHeaderLabels(_RESOLVED_COLUMNS)
        self.resolved_table.verticalHeader().setVisible(False)
        self.resolved_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.resolved_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.resolved_table.horizontalHeader().setSectionResizeMode(
            len(_RESOLVED_COLUMNS) - 1, HEADER_RESIZE_MODE_STRETCH)
        self.resolved_table.itemSelectionChanged.connect(self._on_resolved_selected)
        self.resolved_table.cellDoubleClicked.connect(
            lambda row, _column: self._goto_resolved_row(row))
        resolved_layout.addWidget(self.resolved_table, 1)
        splitter.addWidget(self.resolved_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        self._resolved_rows: List = []

        button_row = QHBoxLayout()
        self.add_button = QToolButton()
        self.add_button.setText("＋ Add criterion ▾")
        self.add_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        menu = QMenu(self.add_button)
        for label, kind, preset in _ADD_MENU:
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, k=kind, p=preset: self._add_rule(k, p))
        self.add_button.setMenu(menu)
        button_row.addWidget(self.add_button)
        for label, slot, tip in (
                ("Edit…", self._edit_rule,
                 "Edit the current criterion (double-click also edits)."),
                ("Duplicate", self._duplicate_rule,
                 "Copy the current criterion — handy for near-identical "
                 "depth or slope bands."),
                ("Delete…", self._delete_rule,
                 "Delete the selected criteria (multi-select supported)."),
                ("↑", lambda: self._move_rule(-1),
                 "Move the criterion up the stack (display/report order)."),
                ("↓", lambda: self._move_rule(1),
                 "Move the criterion down the stack.")):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            button_row.addWidget(button)
        button_row.addStretch(1)
        self.recompute_button = QPushButton("Recompute")
        self.recompute_button.setToolTip(
            "Re-evaluate the Exclusion stack in the background, applying any "
            "changed sampling parameters below first. Editing a criterion "
            "recomputes automatically; use this after input layers change.")
        self.recompute_button.clicked.connect(self._recompute)
        button_row.addWidget(self.recompute_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip(
            "Stop the running recompute (completed criteria stay cached).")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.dock.cancel_analysis)
        button_row.addWidget(self.stop_button)
        layout.addLayout(button_row)

        params_row = QHBoxLayout()
        params_row.addWidget(QLabel("Sample step:"))
        self.step_spin = QSpinBox()
        self.step_spin.setRange(5, 5000)
        self.step_spin.setSuffix(" m")
        self.step_spin.setToolTip(
            "Coarse route-search spacing for spatial exclusions such as "
            "polygons, crossings and proximity rules. Smaller values improve "
            "discovery of short features but add processing time. Depth and "
            "slope thresholds use the stored Bathymetry Profile resolution.")
        params_row.addWidget(self.step_spin)
        params_row.addWidget(QLabel("Sliver tolerance:"))
        self.sliver_spin = QDoubleSpinBox()
        self.sliver_spin.setRange(0.0, 100.0)
        self.sliver_spin.setDecimals(3)
        self.sliver_spin.setSuffix(" km")
        self.sliver_spin.setToolTip(
            "Absorb a final classification range shorter than this into its "
            "more-severe neighbour. A stricter exclusion is never silently "
            "discarded. Set 0 to keep every resolved range.")
        params_row.addWidget(self.sliver_spin)
        self.step_spin.valueChanged.connect(self._mark_params_dirty)
        self.sliver_spin.valueChanged.connect(self._mark_params_dirty)
        refine_label = QLabel("Boundary refinement: 0.1 m")
        refine_label.setToolTip(
            "Coarse sampling finds where conditions change; each boundary is "
            "then refined by bisection to 0.1 m. A spatial polygon/proximity "
            "feature narrower than the Sample step can still be missed — "
            "reduce the step where that matters. Depth/slope discovery uses "
            "the Bathymetry Profile step instead.")
        params_row.addWidget(refine_label)
        params_row.addStretch(1)
        layout.addLayout(params_row)

        io_row = QHBoxLayout()
        self.preview_check = QCheckBox("Show Exclusion Areas on map")
        self.preview_check.setToolTip(
            "Temporarily highlight the resolved Exclusion Areas (red) and "
            "screening flags (orange) on the map — useful for checking the "
            "criteria before the plan is built. The highlight is never "
            "saved; the plan's sections layer remains the built plan.")
        self.preview_check.toggled.connect(self._refresh_map_preview)
        io_row.addWidget(self.preview_check)
        self.resolved_check = QCheckBox("Show resolved exclusions")
        self.resolved_check.setToolTip(
            "Show the table of resolved excluded / flagged KP ranges below "
            "the criteria stack. The setting is remembered.")
        self.resolved_check.setChecked(
            QSettings().value(_SHOW_RESOLVED_SETTINGS_KEY, True, type=bool))
        self.resolved_check.toggled.connect(self._set_resolved_visible)
        io_row.addWidget(self.resolved_check)
        io_row.addSpacing(12)
        io_row.addWidget(ui_helpers.menu_tool_button(
            "Import / Export ▾",
            (("Import from Assessment…", self._import_from_assessment),
             ("Import rule set JSON…", self._import_json),
             None,
             ("Export rule set JSON…", self._export_json)),
            tooltip="Share the criteria stack with the Workbench Assessment "
                    "tool or as versioned JSON."))
        io_row.addStretch(1)
        layout.addLayout(io_row)
        # Full-width status line: recompute progress, export confirmations
        # and preview counts must stay readable at narrow dock widths.
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.resolved_widget.setVisible(self.resolved_check.isChecked())
        refresh_soon = ui_helpers.coalesced(self, self.refresh)
        model.planChanged.connect(refresh_soon)
        model.rulesChanged.connect(refresh_soon)
        self.refresh()

    # -- refresh --------------------------------------------------------------
    def refresh(self) -> None:
        plan_switched = self.model.plan_id != self._loaded_plan_id
        if plan_switched:
            # The last recompute's results, any pending debounced
            # recompute and unapplied parameter edits all belong to the
            # plan that was open before — never show or apply them here.
            self._loaded_plan_id = self.model.plan_id
            self._recompute_timer.stop()
            self._last_results = {}
            self._last_verdicts = []
            self._params_dirty = False
            self.status_label.setText("")
        self._loading = True
        try:
            plan = self.model.plan
            params = self.model.gen_params()
            if not self._params_dirty:
                # Never clobber a typed-but-unapplied Sample step/Sliver
                # value from an unrelated refresh (e.g. a rule toggle).
                self.sliver_spin.setValue(params.sliver_tol_km)
                self.step_spin.setValue(int(params.coarse_step_m))
            rules = self.model.rules
            self.rule_table.setRowCount(len(rules))
            scope = params.scope
            domain_km = scope.length_km if plan else 0.0
            rule_hits = self._current_rule_hits()
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
                intervals = rule_hits.get(str(rule.get("rule_id")), [])
                color = (ACTION_COLORS[wb_schema.RULE_ACTION_RISK]
                         if rule.get("criterion_class") == schema.CRITERION_SCREENING
                         else ACTION_COLORS.get(rule.get("action") or "", QColor("#888")))
                fire_item.setData(ITEM_DATA_USER_ROLE,
                                  (domain_km, intervals, color, scope.start_km))
                self.rule_table.setItem(i, FIRE_COL, fire_item)

                covered = sum(e - s for s, e in intervals)
                pct = 100.0 * covered / domain_km if domain_km > 0 else 0.0
                computed = bool(rule_hits) or bool(self._current_verdicts())
                if intervals:
                    coverage_text = f"{covered:.3f} km · {pct:.2f}%"
                    coverage_tip = ""
                elif computed and int(rule.get("enabled") or 0):
                    coverage_text = "none"
                    coverage_tip = ("Evaluated — this criterion fired "
                                    "nowhere in the scope.")
                else:
                    coverage_text = "—"
                    coverage_tip = ("Not evaluated yet — run Recompute "
                                    "(or enable the criterion).")
                coverage_item = QTableWidgetItem(coverage_text)
                if coverage_tip:
                    coverage_item.setToolTip(coverage_tip)
                coverage_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.rule_table.setItem(i, 3, coverage_item)
            self._refresh_overview()
            self._refresh_resolved()
        finally:
            self._loading = False
        if plan_switched:
            # Redraw the map preview from the new plan's stored context
            # (or clear it when the preview toggle is off).
            self._refresh_map_preview()

    def _current_verdicts(self) -> List:
        """Resolved verdicts: the latest recompute, else the stored plan
        context (so the overview and tools work right after reopening)."""
        if self._last_verdicts:
            return self._last_verdicts
        context = getattr(self.model, "context", None)
        if context is None:
            return []
        merged = list(context.excluded) + list(context.screening)
        merged.sort(key=lambda v: (v.start_km, v.end_km))
        return merged

    def _current_rule_hits(self) -> Dict[str, List]:
        """Per-rule resolved intervals: the latest recompute, else the stored
        plan context (so fire bars and coverage survive reopening)."""
        if self._last_results:
            return self._last_results
        context = getattr(self.model, "context", None)
        stored = getattr(context, "rule_hits", None) or {}
        return {rule_id: [(iv.start_km, iv.end_km) for iv in intervals]
                for rule_id, intervals in stored.items()}

    def _refresh_overview(self) -> None:
        params = self.model.gen_params()
        scope = params.scope
        spans = []
        from ...workbench.kp_bars import STATUS_COLORS

        for verdict in self._current_verdicts():
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
        self._refresh_map_preview()

    def set_progress(self, message: str) -> None:
        self.status_label.setText(message)

    # -- analysis lifecycle (driven by the dock) -------------------------------
    def analysis_started(self) -> None:
        """Prevent stacked recomputes while one is already running."""
        self.recompute_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def analysis_finished(self) -> None:
        self.recompute_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    # -- map preview / excluded sections --------------------------------------
    def _refresh_map_preview(self, _checked=None) -> None:
        if not self.preview_check.isChecked():
            self.dock.clear_exclusion_preview()
            return
        excluded = QColor(214, 39, 40, 150)
        flagged = QColor(255, 140, 0, 150)
        spans = []
        for verdict in self._current_verdicts():
            if verdict.status == STATUS_EXCLUDED:
                spans.append((verdict.start_km, verdict.end_km, excluded))
            elif verdict.status == STATUS_RISK:
                spans.append((verdict.start_km, verdict.end_km, flagged))
        self.dock.set_exclusion_preview(spans)
        if spans:
            self.status_label.setText(
                f"Previewing {len(spans)} Exclusion Area / flagged range(s) "
                "on the map.")

    def _rule_context_menu(self, position) -> None:
        item = self.rule_table.itemAt(position)
        if item is None:
            return
        row = item.row()
        self.rule_table.selectRow(row)
        if row >= len(self.model.rules):
            return
        rule = self.model.rules[row]
        intervals = self._current_rule_hits().get(str(rule.get("rule_id")), [])
        menu = QMenu(self)
        edit_action = menu.addAction("Edit criterion…")
        delete_action = menu.addAction("Delete criterion")
        goto_actions = {}
        if intervals:
            menu.addSeparator()
            shown = intervals[:20]
            for start_km, end_km in shown:
                action = menu.addAction(
                    f"Go to KP {start_km:.3f}-{end_km:.3f} "
                    f"({(end_km - start_km):.3f} km)")
                goto_actions[action] = (start_km, end_km)
            more_action = None
            if len(intervals) > len(shown):
                more_action = menu.addAction(
                    f"… {len(intervals) - len(shown)} more — open the "
                    "resolved list…")
        else:
            more_action = None
        chosen = qt_exec(menu, self.rule_table.viewport().mapToGlobal(position))
        if chosen == edit_action:
            self._edit_rule()
        elif chosen == delete_action:
            self._delete_rule()
        elif more_action is not None and chosen == more_action:
            dialog = ExcludedSectionsDialog(
                self._current_verdicts(), self._rule_names(), self.dock, self)
            qt_exec(dialog)
        elif chosen in goto_actions:
            start_km, end_km = goto_actions[chosen]
            self.dock.goto_range(start_km, end_km)

    def _set_resolved_visible(self, visible: bool) -> None:
        QSettings().setValue(_SHOW_RESOLVED_SETTINGS_KEY, bool(visible))
        self.resolved_widget.setVisible(bool(visible))

    def _rule_names(self) -> Dict[str, str]:
        return {str(r.get("rule_id")): (r.get("name") or "")
                for r in self.model.rules}

    def _on_resolved_view_changed(self) -> None:
        QSettings().setValue(_RESOLVED_VIEW_SETTINGS_KEY,
                             self.resolved_view_combo.currentData() or "grouped")
        self._refresh_resolved()

    def _resolved_view_rows(self):
        """(columns, display rows, (start_km, end_km) spans, summary text)."""
        rule_names = self._rule_names()
        if (self.resolved_view_combo.currentData() or "grouped") == "by_rule":
            columns = _BY_RULE_COLUMNS
            rows: List[List[str]] = []
            spans: List = []
            hits = self._current_rule_hits()
            fired_criteria = 0
            for rule in self.model.rules:
                if not int(rule.get("enabled") or 0):
                    continue
                intervals = hits.get(str(rule.get("rule_id")), [])
                if not intervals:
                    continue
                fired_criteria += 1
                if rule.get("criterion_class") == schema.CRITERION_SCREENING:
                    status = "Flagged (screening)"
                elif (rule.get("action") or "") == wb_schema.RULE_ACTION_ALLOW:
                    status = "Allow exception"
                else:
                    status = "Excluded"
                badge = _CLASS_BADGES.get(rule.get("criterion_class") or "", "")
                name = (f"[{badge}] " if badge else "") + (rule.get("name") or "")
                for start_km, end_km in intervals:
                    rows.append([name, schema.format_kp(start_km),
                                 schema.format_kp(end_km),
                                 schema.format_kp(end_km - start_km), status])
                    spans.append((start_km, end_km))
            summary = "Resolved exclusions — by criterion"
            if rows:
                summary += (f": {len(rows)} range(s) across "
                            f"{fired_criteria} criteria")
            else:
                summary += ": none yet (run Recompute)"
            return columns, rows, spans, summary

        verdicts = [v for v in self._current_verdicts()
                    if v.status in (STATUS_EXCLUDED, STATUS_RISK)]
        verdicts.sort(key=lambda v: (v.start_km, v.end_km))
        rows = [verdict_row_values(v, rule_names) for v in verdicts]
        spans = [(v.start_km, v.end_km) for v in verdicts]
        excluded_km = sum(v.end_km - v.start_km for v in verdicts
                          if v.status == STATUS_EXCLUDED)
        flagged = sum(1 for v in verdicts if v.status == STATUS_RISK)
        summary = "Resolved exclusions"
        if verdicts:
            summary += (f" — {excluded_km:.3f} km excluded in "
                        f"{len(verdicts) - flagged} range(s)")
            if flagged:
                summary += f", {flagged} screening flag(s)"
        else:
            summary += " — none yet (run Recompute)"
        return _RESOLVED_COLUMNS, rows, spans, summary

    def _refresh_resolved(self) -> None:
        """Rebuild the resolved-exclusions table in the selected view."""
        columns, rows, spans, summary = self._resolved_view_rows()
        self._resolved_rows = spans
        self.resolved_label.setText(summary)
        self.resolved_table.setColumnCount(len(columns))
        self.resolved_table.setHorizontalHeaderLabels(columns)
        self.resolved_table.horizontalHeader().setSectionResizeMode(
            len(columns) - 1, HEADER_RESIZE_MODE_STRETCH)
        self.resolved_table.setRowCount(len(rows))
        for i, row_values in enumerate(rows):
            for j, value in enumerate(row_values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
                if j in (0, len(columns) - 1):
                    item.setToolTip(value)
                self.resolved_table.setItem(i, j, item)

    def _on_resolved_selected(self) -> None:
        if self._loading:
            return
        row = self.resolved_table.currentRow()
        if 0 <= row < len(self._resolved_rows):
            start_km, end_km = self._resolved_rows[row]
            self.dock.highlight_range(start_km, end_km)

    def _goto_resolved_row(self, row: int) -> None:
        if 0 <= row < len(self._resolved_rows):
            start_km, end_km = self._resolved_rows[row]
            self.dock.goto_range(start_km, end_km)

    def _export_resolved_csv(self) -> None:
        columns, rows, _spans, _summary = self._resolved_view_rows()
        if not rows:
            QMessageBox.information(
                self, "Burial Planner",
                "No excluded or flagged sections are available yet — run "
                "Recompute first.")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export excluded sections", "excluded_sections.csv",
            "CSV (*.csv)")
        if not path:
            return
        import csv

        headers = [c.lower().replace(" (km)", "_km").replace(" ", "_")
                   for c in columns]
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
        self.status_label.setText(f"Exported {len(rows)} resolved range(s).")

    # -- edits ----------------------------------------------------------------
    def _selected_index(self) -> int:
        return self.rule_table.currentRow()

    def _selected_indices(self) -> List[int]:
        model = self.rule_table.selectionModel()
        if model is None:
            return []
        return sorted({index.row() for index in model.selectedRows()})

    def _add_rule(self, kind: str, preset: Optional[Dict] = None) -> None:
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
            "config_json": json.dumps(preset or {}),
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

    def _duplicate_rule(self) -> None:
        index = self._selected_index()
        if index < 0 or index >= len(self.model.rules):
            self.status_label.setText("Select a criterion to duplicate.")
            return
        copy = dict(self.model.rules[index])
        copy["rule_id"] = schema.new_id()
        copy["name"] = (copy.get("name") or "criterion") + " (copy)"
        rules = list(self.model.rules)
        rules.insert(index + 1, copy)
        self.model.save_rules(rules, target_id=copy["rule_id"])
        self.rule_table.selectRow(index + 1)
        self._recompute()

    def _delete_rule(self) -> None:
        indices = [i for i in self._selected_indices()
                   if i < len(self.model.rules)]
        if not indices:
            self.status_label.setText("Select the criteria to delete.")
            return
        names = [self.model.rules[i].get("name") or "criterion"
                 for i in indices]
        listed = ", ".join(f"'{n}'" for n in names[:5])
        if len(names) > 5:
            listed += f" and {len(names) - 5} more"
        answer = QMessageBox.question(
            self, "Delete criteria",
            f"Delete {len(names)} criterion/criteria: {listed}?",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer != MESSAGE_BOX_YES:
            return
        wanted = set(indices)
        removed_ids = [str(self.model.rules[i].get("rule_id"))
                       for i in indices]
        rules = [r for i, r in enumerate(self.model.rules)
                 if i not in wanted]
        self.model.save_rules(rules, target_id=",".join(removed_ids),
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
        # Debounced: several quick toggles collapse into one recompute.
        self._recompute_timer.start()

    def _mark_params_dirty(self, *_args) -> None:
        if not self._loading:
            self._params_dirty = True

    def _recompute(self) -> None:
        """Apply any changed analysis parameters, then recompute the stack."""
        if not self.model.plan:
            return
        self._params_dirty = False  # applied (or reconciled) below
        params = self.model.gen_params()
        try:
            stored = json.loads(self.model.plan.get("params_json") or "{}")
        except (TypeError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored_refine_tol = float(stored.get(
            "refine_tol_m", generation.BOUNDARY_REFINE_TOL_M))
        if abs(params.sliver_tol_km - self.sliver_spin.value()) > 1e-12 \
                or abs(params.coarse_step_m - float(self.step_spin.value())) > 1e-9 \
                or abs(stored_refine_tol
                       - generation.BOUNDARY_REFINE_TOL_M) > 1e-12:
            if not self.model.update_gen_params({
                    "sliver_tol_km": self.sliver_spin.value(),
                    "coarse_step_m": float(self.step_spin.value()),
                    "refine_tol_m": generation.BOUNDARY_REFINE_TOL_M,
            }, reason="exclusion analysis parameters"):
                return
        self.dock.request_analysis()

    # -- rule-set IO ----------------------------------------------------------
    def _dedupe_imported(self, imported: List[Dict]) -> (List[Dict], int):
        """Drop imported criteria identical to ones already in the stack.

        Identity = name + kind + configuration; importing the same file
        twice must not silently double the stack.
        """
        def key(rule: Dict):
            return ((rule.get("name") or "").strip().casefold(),
                    rule.get("kind") or "",
                    rule.get("config_json") or "")

        existing = {key(rule) for rule in self.model.rules}
        fresh = [rule for rule in imported if key(rule) not in existing]
        return fresh, len(imported) - len(fresh)

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
                # The Assessment tool's method ids ("jet") map onto the
                # Burial Planner vocabulary ("trencher") so the per-method
                # rule filter does not silently skip copied rules.
                "methods_json": _normalised_methods_json(
                    row.get("methods_json")),
                "config_json": row.get("config_json") or "{}",
                "notes": row.get("notes") or "",
            })
        imported, skipped = self._dedupe_imported(imported)
        if not imported:
            self.status_label.setText(
                "Nothing imported — every criterion in the rule set is "
                "already in the stack.")
            return
        self.model.save_rules(list(self.model.rules) + imported,
                              target_id="assessment_import")
        message = f"Imported {len(imported)} criteria from the Assessment."
        if skipped:
            message += f"  Skipped {skipped} identical duplicate(s)."
        self.status_label.setText(message)
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
            if row.get("methods_json"):
                row["methods_json"] = _normalised_methods_json(
                    row.get("methods_json"))
            imported.append(row)
        imported, skipped = self._dedupe_imported(imported)
        if not imported:
            self.status_label.setText(
                "Nothing imported — every criterion in the file is already "
                "in the stack." if skipped else
                "The file contains no criteria.")
            return
        self.model.save_rules(list(self.model.rules) + imported,
                              target_id="json_import")
        message = f"Imported {len(imported)} criteria."
        if skipped:
            message += f"  Skipped {skipped} identical duplicate(s)."
        self.status_label.setText(message)
        self._recompute()
