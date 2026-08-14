# -*- coding: utf-8 -*-
"""Risk Profile tab — feature-level hazard register over the plan scope.

Sits between Exclusions and Plan Builder in the workflow: checks scan
registered inputs (rocks, pockmarks, sandwaves, mag/sonar/linear contacts,
crossings, …) for features on or near the route, and every interaction
lands in the hazard register with an auto-assigned Low/Medium/High risk
(proximity bands and/or attribute rules — all values user-entered with a
source reference). Hazards never remove burial length — that is the
Exclusion stack's job; this tab profiles what remains.

The register carries the engineer's review: risk can be overridden,
status (Open/Noted/Accepted/Mitigated) and notes are kept, and a re-scan
carries that review over by feature identity. Manual hazards (desktop
study items, fishing grounds, …) live alongside scanned ones.
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
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
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
    qt_exec,
)
from ...workbench.kp_bars import FireBarDelegate, VerdictStrip
from ...workbench.rules_engine import Interval
from .. import change_log, map_layers, risk, schema

_CHECK_FIRE_COL = 2
_CHECK_COLUMNS = ["On", "Check", "Findings", "Hazards"]

_HAZARD_COLUMNS = ["Risk", "Status", "KP", "End KP", "Offset (m)", "X-ing",
                   "Angle (°)", "Feature", "Check", "Notes"]
_HAZARD_RISK_COL = 0
_HAZARD_STATUS_COL = 1
_HAZARD_NOTES_COL = 9

_RISK_COLORS = {
    schema.RISK_HIGH: QColor("#d62728"),
    schema.RISK_MEDIUM: QColor("#ff8c00"),
    schema.RISK_LOW: QColor("#e0b000"),
    schema.RISK_UNASSIGNED: QColor("#909090"),
}


class CheckEditorDialog(QDialog):
    """Edit one bp_risk_check row."""

    def __init__(self, check: Dict, inputs: List[Dict], parent=None):
        super().__init__(parent)
        self.check = dict(check)
        self.inputs = inputs
        config = risk.check_config(self.check)
        self.setWindowTitle("Risk check")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(self.check.get("name") or "")
        self.name_edit.setPlaceholderText("e.g. Boulders ≥ 0.5 m")
        form.addRow("Name:", self.name_edit)
        self.input_combo = QComboBox()
        self.input_combo.addItem("(pick a registered input)", "")
        for row in inputs:
            role = row.get("role") or ""
            if role == schema.INPUT_ROLE_BATHY:
                continue
            self.input_combo.addItem(
                f"{row.get('layer_name') or '?'}  "
                f"({schema.INPUT_ROLE_LABELS.get(role, role)})",
                row.get("input_id"))
        self.input_combo.setCurrentIndex(
            max(0, self.input_combo.findData(config.get("input_id") or "")))
        form.addRow("Input:", self.input_combo)
        self.distance_spin = QDoubleSpinBox()
        self.distance_spin.setRange(0.0, 1000000.0)
        self.distance_spin.setDecimals(1)
        self.distance_spin.setSuffix(" m")
        self.distance_spin.setValue(float(config.get("distance_m") or 0.0))
        self.distance_spin.setToolTip(
            "Search distance from the route centreline, applied each side. "
            "Features beyond this are not registered at all.")
        form.addRow("Search within (each side of route):", self.distance_spin)
        self.filter_edit = QLineEdit(config.get("filter_expression") or "")
        self.filter_edit.setPlaceholderText("optional QGIS filter expression")
        form.addRow("Feature filter:", self.filter_edit)
        self.label_edit = QLineEdit(config.get("label_attribute") or "")
        self.label_edit.setPlaceholderText(
            "attribute naming each feature (e.g. Name, ContactID)")
        form.addRow("Label attribute:", self.label_edit)
        layout.addLayout(form)

        bands = QGroupBox("Risk from proximity (nearest approach)")
        bands_form = QFormLayout(bands)
        self.band_spins: Dict[str, QDoubleSpinBox] = {}
        for key, label in (("band_high_m", "High within:"),
                           ("band_medium_m", "Medium within:"),
                           ("band_low_m", "Low within:")):
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 1000000.0)
            spin.setDecimals(1)
            spin.setSuffix(" m")
            spin.setSpecialValueText("(off)")
            spin.setValue(float(config.get(key) or 0.0))
            self.band_spins[key] = spin
            bands_form.addRow(label, spin)
        bands_note = QLabel(
            "The tightest matching band wins (crossings are 0 m). 0 turns a "
            "band off. Values are your project's criteria — record the "
            "source below.")
        bands_note.setWordWrap(True)
        bands_note.setStyleSheet("color: #666;")
        bands_form.addRow(bands_note)
        layout.addWidget(bands)

        attr = QGroupBox("Risk from an attribute (optional)")
        attr_form = QFormLayout(attr)
        self.attribute_edit = QLineEdit(config.get("attribute") or "")
        self.attribute_edit.setPlaceholderText(
            "e.g. Height_m, Class, Diameter")
        attr_form.addRow("Attribute:", self.attribute_edit)
        self.rules_table = QTableWidget(0, 2)
        self.rules_table.setHorizontalHeaderLabels(["Value or range", "Risk"])
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setMinimumHeight(90)
        self.rules_table.setMaximumHeight(140)
        self.rules_table.horizontalHeader().setSectionResizeMode(
            0, HEADER_RESIZE_MODE_STRETCH)
        self.rules_table.setToolTip(
            "First matching rule wins. Exact value (e.g. ROCK) or numeric "
            "range a-b with either side open (2- = at least 2, -3 = up to "
            "3). The effective risk is the most severe of proximity and "
            "attribute.")
        for rule in config.get("attribute_rules") or []:
            self._append_rule_row(risk.format_attribute_rule(rule),
                                  rule.get("risk") or "")
        attr_form.addRow("Rules:", self.rules_table)
        rule_buttons = QHBoxLayout()
        add_rule = QPushButton("＋ Add rule")
        add_rule.clicked.connect(lambda: self._append_rule_row("", ""))
        rule_buttons.addWidget(add_rule)
        remove_rule = QPushButton("− Remove rule")
        remove_rule.clicked.connect(self._remove_rule_row)
        rule_buttons.addWidget(remove_rule)
        rule_buttons.addStretch(1)
        attr_form.addRow("", rule_buttons)
        layout.addWidget(attr)

        tail = QFormLayout()
        self.default_combo = QComboBox()
        for level in [schema.RISK_UNASSIGNED] + schema.RISK_LEVELS:
            self.default_combo.addItem(schema.RISK_LABELS[level], level)
        self.default_combo.setCurrentIndex(
            max(0, self.default_combo.findData(config.get("default_risk") or "")))
        self.default_combo.setToolTip(
            "Applied when a feature is inside the search distance but no "
            "band or attribute rule fires.")
        tail.addRow("Risk when nothing fires:", self.default_combo)
        self.source_edit = QLineEdit(self.check.get("source_ref") or "")
        self.source_edit.setPlaceholderText(
            "Document + revision these criteria come from")
        tail.addRow("Source reference:", self.source_edit)
        self.notes_edit = QLineEdit(self.check.get("notes") or "")
        tail.addRow("Notes:", self.notes_edit)
        layout.addLayout(tail)

        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _append_rule_row(self, text: str, level: str) -> None:
        row = self.rules_table.rowCount()
        self.rules_table.insertRow(row)
        self.rules_table.setItem(row, 0, QTableWidgetItem(text))
        combo = QComboBox()
        for value in schema.RISK_LEVELS:
            combo.addItem(schema.RISK_LABELS[value], value)
        combo.setCurrentIndex(max(0, combo.findData(level or schema.RISK_LOW)))
        self.rules_table.setCellWidget(row, 1, combo)

    def _remove_rule_row(self) -> None:
        row = self.rules_table.currentRow()
        if row < 0:
            row = self.rules_table.rowCount() - 1
        if row >= 0:
            self.rules_table.removeRow(row)

    def result_check(self) -> Dict:
        config: Dict = {
            "input_id": self.input_combo.currentData() or "",
            "distance_m": self.distance_spin.value(),
            "filter_expression": self.filter_edit.text().strip(),
            "label_attribute": self.label_edit.text().strip(),
            "attribute": self.attribute_edit.text().strip(),
            "default_risk": self.default_combo.currentData() or "",
        }
        for key, spin in self.band_spins.items():
            if spin.value() > 0:
                config[key] = spin.value()
        rules: List[Dict] = []
        for row in range(self.rules_table.rowCount()):
            item = self.rules_table.item(row, 0)
            combo = self.rules_table.cellWidget(row, 1)
            level = combo.currentData() if combo is not None else ""
            rule = risk.parse_attribute_rule(
                item.text() if item is not None else "", level or "")
            if rule is not None:
                rules.append(rule)
        if rules:
            config["attribute_rules"] = rules
        check = dict(self.check)
        check.update({
            "name": self.name_edit.text().strip() or "Risk check",
            "config_json": json.dumps(config),
            "source_ref": self.source_edit.text().strip(),
            "notes": self.notes_edit.text(),
        })
        return check


class ManualHazardDialog(QDialog):
    """Add a user-entered hazard (desktop study item, fishing ground, …)."""

    def __init__(self, dock, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.setWindowTitle("Add manual hazard")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("e.g. Charted wreck, Fishing area")
        form.addRow("Hazard:", self.label_edit)
        kp_row = QHBoxLayout()
        self.kp_spin = QDoubleSpinBox()
        self.kp_spin.setDecimals(3)
        self.kp_spin.setRange(0.0, 100000.0)
        self.kp_spin.setSuffix(" km")
        kp_row.addWidget(self.kp_spin)
        pick = QPushButton("Pick…")
        pick.setToolTip("Pick the KP by clicking the route on the map.")
        pick.clicked.connect(self._pick_kp)
        kp_row.addWidget(pick)
        form.addRow("KP:", kp_row)
        self.range_check = QCheckBox("KP range (to)")
        self.end_spin = QDoubleSpinBox()
        self.end_spin.setDecimals(3)
        self.end_spin.setRange(0.0, 100000.0)
        self.end_spin.setSuffix(" km")
        self.end_spin.setEnabled(False)
        self.range_check.toggled.connect(self.end_spin.setEnabled)
        range_row = QHBoxLayout()
        range_row.addWidget(self.range_check)
        range_row.addWidget(self.end_spin, 1)
        form.addRow("", range_row)
        self.risk_combo = QComboBox()
        for level in [schema.RISK_UNASSIGNED] + schema.RISK_LEVELS:
            self.risk_combo.addItem(schema.RISK_LABELS[level], level)
        form.addRow("Risk:", self.risk_combo)
        self.notes_edit = QLineEdit()
        form.addRow("Notes:", self.notes_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _pick_kp(self) -> None:
        self.dock.pick_kp_on_map(
            lambda kp: self.kp_spin.setValue(round(float(kp), 3)),
            "Click the route to set the hazard KP (right-click cancels).")

    def values(self):
        end_kp = self.end_spin.value() if self.range_check.isChecked() else None
        return (self.kp_spin.value(), end_kp,
                self.label_edit.text().strip() or "Manual hazard",
                self.risk_combo.currentData() or "",
                self.notes_edit.text())


class RiskTab(QWidget):
    """Risk Profile: checks stack + hazard register; scans run in-process."""

    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._loading = False

        layout = QVBoxLayout(self)
        self.overview = VerdictStrip()
        self.overview.kpClicked.connect(self.dock.goto_kp)
        layout.addWidget(self.overview)

        splitter = QSplitter(getattr(Qt, "Orientation", Qt).Vertical)

        checks_widget = QWidget()
        checks_layout = QVBoxLayout(checks_widget)
        checks_layout.setContentsMargins(0, 0, 0, 0)
        self.check_table = QTableWidget(0, len(_CHECK_COLUMNS))
        self.check_table.setHorizontalHeaderLabels(_CHECK_COLUMNS)
        self.check_table.verticalHeader().setVisible(False)
        self.check_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.check_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.check_table.setItemDelegateForColumn(
            _CHECK_FIRE_COL, FireBarDelegate(self.check_table))
        header = self.check_table.horizontalHeader()
        header.setSectionResizeMode(1, HEADER_RESIZE_MODE_STRETCH)
        header.setSectionResizeMode(_CHECK_FIRE_COL, HEADER_RESIZE_MODE_STRETCH)
        self.check_table.itemChanged.connect(self._on_check_item_changed)
        self.check_table.doubleClicked.connect(lambda _i: self._edit_check())
        checks_layout.addWidget(self.check_table, 1)

        check_buttons = QHBoxLayout()
        add_button = QPushButton("＋ Add check…")
        add_button.clicked.connect(self._add_check)
        check_buttons.addWidget(add_button)
        for label, slot in (("Edit…", self._edit_check),
                            ("Delete", self._delete_check),
                            ("↑", lambda: self._move_check(-1)),
                            ("↓", lambda: self._move_check(1))):
            button = QPushButton(label)
            button.clicked.connect(slot)
            check_buttons.addWidget(button)
        check_buttons.addStretch(1)
        self.run_button = QPushButton("Run checks")
        self.run_button.setToolTip(
            "Scan every enabled check's input layer for features on or "
            "near the route and rebuild the hazard register. Your review "
            "(risk overrides, status, notes) is carried over by feature; "
            "manual hazards are never touched.")
        self.run_button.clicked.connect(self._run_checks)
        check_buttons.addWidget(self.run_button)
        checks_layout.addLayout(check_buttons)
        splitter.addWidget(checks_widget)

        hazards_widget = QWidget()
        hazards_layout = QVBoxLayout(hazards_widget)
        hazards_layout.setContentsMargins(0, 0, 0, 0)
        register_header = QHBoxLayout()
        self.register_label = QLabel("Hazard register")
        register_header.addWidget(self.register_label)
        register_header.addStretch(1)
        manual_button = QPushButton("＋ Add manual hazard…")
        manual_button.clicked.connect(self._add_manual_hazard)
        register_header.addWidget(manual_button)
        export_button = QPushButton("Export CSV…")
        export_button.clicked.connect(self._export_csv)
        register_header.addWidget(export_button)
        hazards_layout.addLayout(register_header)
        self.hazard_table = QTableWidget(0, len(_HAZARD_COLUMNS))
        self.hazard_table.setHorizontalHeaderLabels(_HAZARD_COLUMNS)
        self.hazard_table.verticalHeader().setVisible(False)
        self.hazard_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.hazard_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.hazard_table.horizontalHeader().setStretchLastSection(True)
        self.hazard_table.itemChanged.connect(self._on_hazard_item_changed)
        self.hazard_table.itemSelectionChanged.connect(self._on_hazard_selected)
        self.hazard_table.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.hazard_table.customContextMenuRequested.connect(
            self._hazard_context_menu)
        self.hazard_table.cellDoubleClicked.connect(
            lambda row, _column: self._goto_hazard_row(row))
        hazards_layout.addWidget(self.hazard_table, 1)
        splitter.addWidget(hazards_widget)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        status_row = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        layout.addLayout(status_row)

        model.planChanged.connect(self.refresh)
        model.riskChanged.connect(self.refresh)
        self.refresh()

    # -- refresh --------------------------------------------------------------
    def refresh(self) -> None:
        self._loading = True
        try:
            params = self.model.gen_params()
            scope = params.scope
            checks = self.model.risk_checks
            hazards = self.model.hazards
            by_check: Dict[str, List[Dict]] = {}
            for hazard in hazards:
                by_check.setdefault(str(hazard.get("check_id") or ""),
                                    []).append(hazard)

            self.check_table.setRowCount(len(checks))
            for i, check in enumerate(checks):
                on_item = QTableWidgetItem()
                on_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                 | Qt.ItemFlag.ItemIsUserCheckable
                                 | Qt.ItemFlag.ItemIsSelectable)
                on_item.setCheckState(
                    Qt.CheckState.Checked if int(check.get("enabled") or 0)
                    else Qt.CheckState.Unchecked)
                on_item.setData(ITEM_DATA_USER_ROLE, check.get("check_id"))
                self.check_table.setItem(i, 0, on_item)

                name_item = QTableWidgetItem(check.get("name") or "")
                name_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                   | Qt.ItemFlag.ItemIsSelectable)
                if check.get("source_ref"):
                    name_item.setToolTip(f"Source: {check.get('source_ref')}")
                self.check_table.setItem(i, 1, name_item)

                found = by_check.get(str(check.get("check_id") or ""), [])
                worst = schema.RISK_UNASSIGNED
                for hazard in found:
                    worst = risk.risk_max(worst, hazard.get("risk") or "")
                intervals = [(s, e) for s, e, _level in
                             risk.hazard_spans(found)]
                fire_item = QTableWidgetItem()
                fire_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                   | Qt.ItemFlag.ItemIsSelectable)
                fire_item.setData(
                    ITEM_DATA_USER_ROLE,
                    (scope.length_km, intervals, _RISK_COLORS[worst],
                     scope.start_km))
                self.check_table.setItem(i, _CHECK_FIRE_COL, fire_item)

                count_item = QTableWidgetItem(
                    f"{len(found)}" if found else "—")
                count_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                    | Qt.ItemFlag.ItemIsSelectable)
                self.check_table.setItem(i, 3, count_item)

            self._refresh_hazards()
            self._refresh_overview(scope)
        finally:
            self._loading = False

    def _refresh_overview(self, scope) -> None:
        spans = []
        for start_km, end_km, level in risk.hazard_spans(self.model.hazards):
            spans.append((start_km, end_km, _RISK_COLORS.get(
                level, _RISK_COLORS[schema.RISK_UNASSIGNED])))
        self.overview.set_spans(scope.length_km, spans, "Risk",
                                domain_start_km=scope.start_km)

    def _refresh_hazards(self) -> None:
        hazards = self.model.hazards
        check_names = {str(c.get("check_id") or ""): (c.get("name") or "")
                       for c in self.model.risk_checks}
        counts = risk.summarise_hazards(hazards)
        summary = f"Hazard register — {counts['total']} hazard(s)"
        if counts["total"]:
            bits = [f"{counts[level]} {schema.RISK_LABELS[level].lower()}"
                    for level in reversed(schema.RISK_LEVELS) if counts[level]]
            if counts[schema.RISK_UNASSIGNED]:
                bits.append(f"{counts[schema.RISK_UNASSIGNED]} unassigned")
            bits.append(f"{counts['open']} open")
            summary += " (" + ", ".join(bits) + ")"
        self.register_label.setText(summary)

        self.hazard_table.setRowCount(len(hazards))
        for i, hazard in enumerate(hazards):
            hazard_id = hazard.get("hazard_id")
            end_kp = hazard.get("end_kp")
            is_range = (end_kp is not None
                        and abs(float(end_kp) - float(hazard.get("kp") or 0.0))
                        > 5e-4)
            angle = hazard.get("crossing_angle_deg")
            values = [
                "",  # risk combo
                "",  # status combo
                schema.format_kp(hazard.get("kp")),
                schema.format_kp(end_kp) if is_range else "",
                (f"{float(hazard.get('offset_m') or 0.0):.1f}"
                 if not int(hazard.get("crossing") or 0) else "0.0"),
                "✕" if int(hazard.get("crossing") or 0) else "",
                f"{float(angle):.1f}" if angle is not None else "",
                hazard.get("label") or "",
                check_names.get(str(hazard.get("check_id") or ""), "manual"),
                hazard.get("notes") or "",
            ]
            for j, value in enumerate(values):
                item = QTableWidgetItem(value)
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if j == _HAZARD_NOTES_COL:
                    flags |= Qt.ItemFlag.ItemIsEditable
                item.setFlags(flags)
                if j == 0:
                    item.setData(ITEM_DATA_USER_ROLE, hazard_id)
                if j == 7:
                    try:
                        attributes = json.loads(
                            hazard.get("attributes_json") or "{}")
                    except (ValueError, TypeError):
                        attributes = {}
                    if attributes:
                        item.setToolTip("\n".join(
                            f"{k}: {v}" for k, v in attributes.items()))
                if j == _HAZARD_NOTES_COL and value:
                    item.setToolTip(value)
                self.hazard_table.setItem(i, j, item)
            self._add_hazard_combo(
                i, _HAZARD_RISK_COL, hazard_id, "risk",
                [(lv, schema.RISK_LABELS[lv])
                 for lv in [schema.RISK_UNASSIGNED] + schema.RISK_LEVELS],
                hazard.get("risk") or "",
                "Effective risk. Auto-assigned by the check "
                f"({schema.RISK_LABELS.get(hazard.get('auto_risk') or '', 'Unassigned')}); "
                "changing it here records a user override that survives "
                "re-scans.")
            self._add_hazard_combo(
                i, _HAZARD_STATUS_COL, hazard_id, "status",
                [(s, schema.HAZARD_STATUS_LABELS[s])
                 for s in schema.HAZARD_STATUSES],
                hazard.get("status") or schema.HAZARD_STATUS_OPEN,
                "Review status — carried over when checks are re-run.")

    def _add_hazard_combo(self, row: int, column: int, hazard_id: str,
                          field: str, options, current: str,
                          tooltip: str = "") -> None:
        combo = QComboBox()
        for value, label in options:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(current or "")))
        if tooltip:
            combo.setToolTip(tooltip)
        if field == "risk":
            color = _RISK_COLORS.get(current or "")
            if color is not None:
                combo.setStyleSheet(f"color: {color.name()};")
        combo.currentIndexChanged.connect(
            lambda _index, c=combo, hid=hazard_id, f=field:
            self._deferred_hazard_edit(hid, f, c.currentData()))
        self.hazard_table.setCellWidget(row, column, combo)

    def _deferred_hazard_edit(self, hazard_id: str, field: str,
                              value: str) -> None:
        if self._loading or not hazard_id:
            return
        from qgis.PyQt.QtCore import QTimer

        def apply() -> None:
            hazard = next((h for h in self.model.hazards
                           if h.get("hazard_id") == hazard_id), None)
            if hazard is None or (hazard.get(field) or "") == (value or ""):
                return
            self.model.update_hazards([hazard_id], {field: value or ""})

        QTimer.singleShot(0, apply)

    # -- selections / navigation ---------------------------------------------
    def _selected_hazard_ids(self) -> List[str]:
        ids = []
        for index in self.hazard_table.selectionModel().selectedRows():
            item = self.hazard_table.item(index.row(), 0)
            if item is not None:
                ids.append(item.data(ITEM_DATA_USER_ROLE))
        return [i for i in ids if i]

    def _hazard_for_row(self, row: int) -> Optional[Dict]:
        item = self.hazard_table.item(row, 0)
        hazard_id = item.data(ITEM_DATA_USER_ROLE) if item else ""
        return next((h for h in self.model.hazards
                     if h.get("hazard_id") == hazard_id), None)

    def _on_hazard_selected(self) -> None:
        if self._loading:
            return
        ids = self._selected_hazard_ids()
        if len(ids) != 1:
            return
        hazard = next((h for h in self.model.hazards
                       if h.get("hazard_id") == ids[0]), None)
        if hazard is None:
            return
        start = float(hazard.get("kp") or 0.0)
        end = float(hazard.get("end_kp") or start)
        if abs(end - start) > 5e-4:
            self.dock.highlight_range(min(start, end), max(start, end))
        else:
            self.dock.highlight_kp(start)

    def _goto_hazard_row(self, row: int) -> None:
        hazard = self._hazard_for_row(row)
        if hazard is None:
            return
        start = float(hazard.get("kp") or 0.0)
        end = float(hazard.get("end_kp") or start)
        if abs(end - start) > 5e-4:
            self.dock.goto_range(min(start, end), max(start, end))
        else:
            self.dock.goto_kp(start)

    def _on_hazard_item_changed(self, item) -> None:
        if self._loading or item.column() != _HAZARD_NOTES_COL:
            return
        id_item = self.hazard_table.item(item.row(), 0)
        hazard_id = id_item.data(ITEM_DATA_USER_ROLE) if id_item else ""
        if hazard_id:
            self.model.update_hazards([hazard_id], {"notes": item.text()})

    def _hazard_context_menu(self, position) -> None:
        item = self.hazard_table.itemAt(position)
        if item is None:
            return
        row = item.row()
        selected_rows = {index.row() for index in
                         self.hazard_table.selectionModel().selectedRows()}
        if row not in selected_rows:
            self.hazard_table.clearSelection()
            self.hazard_table.selectRow(row)
        hazard = self._hazard_for_row(row)
        if hazard is None:
            return
        ids = self._selected_hazard_ids()
        suffix = f" ({len(ids)} selected)" if len(ids) > 1 else ""
        menu = QMenu(self)
        go_action = menu.addAction("Go to hazard on map and profile")
        menu.addSeparator()
        risk_menu = menu.addMenu(f"Set risk{suffix}")
        risk_actions = {}
        for level in [schema.RISK_UNASSIGNED] + schema.RISK_LEVELS:
            risk_actions[risk_menu.addAction(
                schema.RISK_LABELS[level])] = level
        reset_action = menu.addAction(f"Reset risk to check's{suffix}")
        status_menu = menu.addMenu(f"Set status{suffix}")
        status_actions = {}
        for status in schema.HAZARD_STATUSES:
            status_actions[status_menu.addAction(
                schema.HAZARD_STATUS_LABELS[status])] = status
        menu.addSeparator()
        delete_action = menu.addAction(f"Delete{suffix}…")
        chosen = qt_exec(menu,
                         self.hazard_table.viewport().mapToGlobal(position))
        if chosen == go_action:
            self._goto_hazard_row(row)
        elif chosen in risk_actions:
            self.model.update_hazards(ids, {"risk": risk_actions[chosen]})
        elif chosen == reset_action:
            groups: Dict[str, List[str]] = {}
            for h in self.model.hazards:
                if h.get("hazard_id") in ids:
                    groups.setdefault(h.get("auto_risk") or "",
                                      []).append(h.get("hazard_id"))
            for auto, group_ids in groups.items():
                self.model.update_hazards(
                    group_ids, {"risk": auto,
                                "risk_source": schema.RISK_SOURCE_AUTO})
        elif chosen in status_actions:
            self.model.update_hazards(ids,
                                      {"status": status_actions[chosen]})
        elif chosen == delete_action:
            answer = QMessageBox.question(
                self, "Delete hazards",
                f"Delete {len(ids)} hazard(s)? Scanned hazards will "
                "reappear on the next Run checks unless the check or "
                "feature changes.",
                MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
            if answer == MESSAGE_BOX_YES:
                self.model.delete_hazards(ids)

    # -- check edits -----------------------------------------------------------
    def _selected_check_index(self) -> int:
        return self.check_table.currentRow()

    def _add_check(self) -> None:
        if not self.model.plan:
            return
        check = {
            "check_id": schema.new_id(),
            "plan_id": self.model.plan_id,
            "name": "",
            "enabled": 1,
            "config_json": "{}",
            "source_ref": "",
            "notes": "",
        }
        dialog = CheckEditorDialog(check, self.model.inputs, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            checks = list(self.model.risk_checks) + [dialog.result_check()]
            self.model.save_risk_checks(checks, target_id=check["check_id"])

    def _edit_check(self) -> None:
        index = self._selected_check_index()
        if index < 0 or index >= len(self.model.risk_checks):
            return
        dialog = CheckEditorDialog(self.model.risk_checks[index],
                                   self.model.inputs, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            checks = list(self.model.risk_checks)
            checks[index] = dialog.result_check()
            self.model.save_risk_checks(
                checks, target_id=str(checks[index].get("check_id")))

    def _delete_check(self) -> None:
        index = self._selected_check_index()
        if index < 0 or index >= len(self.model.risk_checks):
            return
        check = self.model.risk_checks[index]
        check_id = str(check.get("check_id") or "")
        found = [h for h in self.model.hazards
                 if str(h.get("check_id") or "") == check_id]
        message = f"Delete '{check.get('name') or 'check'}'?"
        if found:
            message += f" Its {len(found)} hazard(s) will also be removed."
        answer = QMessageBox.question(self, "Delete check", message,
                                      MESSAGE_BOX_YES | MESSAGE_BOX_NO,
                                      MESSAGE_BOX_NO)
        if answer != MESSAGE_BOX_YES:
            return
        checks = [c for i, c in enumerate(self.model.risk_checks) if i != index]
        self.model.save_risk_checks(
            checks, target_id=check_id,
            action=change_log.ACTION_DELETE_RISK_CHECK)
        if found:
            self.model.delete_hazards(
                [h.get("hazard_id") for h in found])

    def _move_check(self, delta: int) -> None:
        index = self._selected_check_index()
        checks = list(self.model.risk_checks)
        target = index + delta
        if index < 0 or target < 0 or target >= len(checks):
            return
        checks[index], checks[target] = checks[target], checks[index]
        self.model.save_risk_checks(
            checks, target_id=str(checks[target].get("check_id")))
        self.check_table.selectRow(target)

    def _on_check_item_changed(self, item) -> None:
        if self._loading or item.column() != 0:
            return
        index = item.row()
        checks = list(self.model.risk_checks)
        if index >= len(checks):
            return
        checks[index] = dict(checks[index])
        checks[index]["enabled"] = \
            1 if item.checkState() == Qt.CheckState.Checked else 0
        self.model.save_risk_checks(
            checks, target_id=str(checks[index].get("check_id")))

    # -- scan -----------------------------------------------------------------
    def _run_checks(self) -> None:
        if not self.model.plan:
            return
        if self.model.route is None:
            QMessageBox.warning(
                self, "Burial Planner",
                "The plan's route is not available — set the route on the "
                "Plan tab first.")
            return
        checks = [c for c in self.model.risk_checks
                  if int(c.get("enabled") or 0)]
        if not checks:
            self.status_label.setText(
                "No enabled checks — add a check first.")
            return
        from qgis.core import QgsProject
        from qgis.PyQt.QtWidgets import QApplication

        from .. import risk_scan

        params = self.model.gen_params()
        scope = Interval(params.scope.start_km, params.scope.end_km)
        inputs_by_id = {str(r.get("input_id") or ""): r
                        for r in self.model.inputs}
        hazards: List[Dict] = []
        warnings: List[str] = []
        run_ids: List[str] = []
        QApplication.setOverrideCursor(
            getattr(Qt, "CursorShape", Qt).WaitCursor)
        try:
            for check in checks:
                config = risk.check_config(check)
                input_row = inputs_by_id.get(str(config.get("input_id") or ""))
                if input_row is None:
                    warnings.append(
                        f"Check '{check.get('name')}': no registered input "
                        "selected — skipped.")
                    continue
                layer = map_layers.resolve_input_layer(
                    QgsProject.instance(), input_row)
                try:
                    found, check_warnings = risk_scan.scan_check(
                        self.model.plan_id, check, layer, self.model.route,
                        self.model.distance, scope=scope,
                        progress=self.status_label.setText)
                except risk_scan.RiskScanError as exc:
                    warnings.append(str(exc))
                    continue
                hazards.extend(found)
                warnings.extend(check_warnings)
                run_ids.append(str(check.get("check_id") or ""))
        finally:
            QApplication.restoreOverrideCursor()
        if run_ids:
            self.model.apply_risk_scan(hazards, check_ids=run_ids)
        message = (f"Scanned {len(run_ids)} check(s): "
                   f"{len(hazards)} hazard(s).")
        if warnings:
            message += "  ·  " + "  ·  ".join(warnings[:4])
            if len(warnings) > 4:
                message += f"  ·  … {len(warnings) - 4} more"
        self.status_label.setText(message)

    # -- manual hazards / export ----------------------------------------------
    def _add_manual_hazard(self) -> None:
        if not self.model.plan:
            return
        dialog = ManualHazardDialog(self.dock, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        kp, end_kp, label, level, notes = dialog.values()
        try:
            self.model.add_manual_hazard(kp, end_kp, label, level, notes)
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))

    def _export_csv(self) -> None:
        if not self.model.hazards:
            QMessageBox.information(self, "Burial Planner",
                                    "The hazard register is empty.")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export hazard register", "hazard_register.csv",
            "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(self.model.export_hazards_csv())
        except OSError as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"Could not write the CSV: {exc}")
            return
        self.status_label.setText(
            f"Exported {len(self.model.hazards)} hazard(s).")
