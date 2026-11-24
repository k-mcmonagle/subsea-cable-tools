# -*- coding: utf-8 -*-
"""Risk Profile tab — feature-level hazard register over the plan scope.

Sits between Exclusions and Plan Builder in the workflow: checks scan
registered inputs (rocks, pockmarks, sandwaves, mag/sonar/linear contacts,
crossings, …) for features on or near the route, and every interaction
lands in the hazard register with an auto-assigned level on the plan's scoring scale
(proximity bands and/or attribute rules — all values user-entered with a
source reference). Hazards never remove burial length — that is the
Exclusion stack's job; this tab profiles what remains.

The register carries the engineer's review: risk can be overridden,
status (Open/Noted/Accepted/Mitigated) and notes are kept, and a re-scan
carries that review over by feature identity. Manual hazards (desktop
study items, fishing grounds, …) live alongside scanned ones.

Each scan is recorded per check (``bp_analysis.risk_json``): when it ran, on
which settings and how many hazards it found — so after reopening the
project a check reads "none found" rather than "not run", and a check whose
settings or input changed since its scan is marked out of date.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.gui import QgsCollapsibleGroupBoxBasic
from qgis.PyQt.QtCore import Qt, QTimer
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
    QProgressBar,
    QPushButton,
    QScrollArea,
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
    HEADER_RESIZE_MODE_CONTENTS,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
    qt_exec,
)
from ...workbench.kp_bars import (
    FireBarDelegate,
    FireBarHover,
    VerdictStrip,
)
from ...workbench.rules_engine import Interval
from ...workbench import rules_engine as eng
from .. import (analysis_state, analysis_task, coverage, map_layers, risk, risk_scan,
                schema)
from .. import ui_helpers
from ..hazard_register import HazardRegister, NOTES, RISK, STATUS
from ..risk_scale import DEFAULT_SCALE
from ..risk_scale_dialog import RiskScaleDialog
from .attribute_widgets import (
    AttributeRulesTable,
    ExpressionEdit,
    FieldCombo,
    expression_problem,
    resolve_input_layer,
)

_CHECK_FIRE_COL = 2
_CHECK_COLUMNS = ["On", "Check", "Findings", "Hazards · total extent", "Unique extent"]
_CHECK_COUNT_COL = 3

class CheckEditorDialog(QDialog):
    """Edit one bp_risk_check row."""

    def __init__(self, check: Dict, inputs: List[Dict], parent=None,
                 model=None):
        super().__init__(parent)
        self.check = dict(check)
        self.inputs = inputs
        # The plan model, for a KP-range table's reference RPL picker.
        self.model = model
        self.scale = model.risk_scale() if model is not None else DEFAULT_SCALE
        config = risk.check_config(self.check)
        self.setWindowTitle("Risk check")
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        form = QFormLayout()
        self.name_edit = QLineEdit(self.check.get("name") or "")
        self.name_edit.setPlaceholderText("e.g. Boulders ≥ 0.5 m")
        form.addRow("Name:", self.name_edit)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Features on or near the route (layer)",
                                risk_scan.CHECK_KIND_FEATURES)
        self.kind_combo.addItem("KP-range table (start/end KP rows)",
                                risk_scan.CHECK_KIND_KP_TABLE)
        self.kind_combo.addItem("Route alter courses (course change)",
                                risk_scan.CHECK_KIND_ROUTE_TURNS)
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(
            risk_scan.check_kind(config))))
        self.kind_combo.currentIndexChanged.connect(self._sync_kind)
        self.kind_combo.setToolTip(
            "Feature checks scan a registered input layer. A KP-range "
            "table check registers each row of a table (with or without "
            "geometry) over its start-end KP, quoted on a chosen RPL. "
            "The alter-course "
            "check scans the route geometry itself: each A/C's course "
            "change (as in the Extract A/C Points algorithm) is assessed "
            "against the rules below — flag turns your burial tool cannot "
            "follow within its minimum turn radius.")
        form.addRow("Check type:", self.kind_combo)
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
        self.input_combo.currentIndexChanged.connect(self._sync_input_layer)
        form.addRow("Input:", self.input_combo)
        self.distance_spin = QDoubleSpinBox()
        self.distance_spin.setRange(0.0, 1000000.0)
        self.distance_spin.setDecimals(1)
        self.distance_spin.setSuffix(" m")
        self.distance_spin.setSpecialValueText("0 m · intersections only")
        self.distance_spin.setValue(float(config.get("distance_m") or 0.0))
        self.distance_spin.setToolTip(
            "At zero, register points on the centreline, line crossings, touches and overlaps. "
            "A positive distance also includes nearby features on either side.")
        form.addRow("Search each side", self.distance_spin)
        self.distance_hint = QLabel("Use 0 m for features that touch or overlap the route. "
                                    "Enter a distance to include nearby features on either side.")
        self.distance_hint.setWordWrap(True)
        self.distance_hint.setStyleSheet(ui_helpers.hint_style())
        form.addRow(self.distance_hint)
        self.filter_edit = ExpressionEdit(
            config.get("filter_expression") or "",
            "optional QGIS filter expression")
        self.filter_edit.setToolTip(
            "Only features for which this expression is true are scanned "
            "at all (e.g. \"Status\" = 'live'). Leave blank for every "
            "feature.")
        form.addRow("Feature filter:", self.filter_edit)
        self.label_edit = FieldCombo(
            config.get("label_attribute") or "",
            "attribute naming each feature (e.g. Name, ContactID)")
        form.addRow("Label attribute:", self.label_edit)
        layout.addLayout(form)

        # KP-range tables: start/end fields, unit and reference RPL.
        from .kp_table_form import KpTableForm
        self.kp_group = QGroupBox("KP ranges")
        kp_form = QFormLayout(self.kp_group)
        self.kp_table_form = KpTableForm(kp_form, config, model)
        layout.addWidget(self.kp_group)

        # Alter-course mode is deliberately simple: one threshold, one risk
        # level (set below). Main consequence of a sharp A/C is the burial
        # tool running off route at the turn.
        self.turns_group = QGroupBox("A/C course-change check")
        turns_form = QFormLayout(self.turns_group)
        self.min_cc_spin = QDoubleSpinBox()
        self.min_cc_spin.setRange(0.0, 180.0)
        self.min_cc_spin.setDecimals(1)
        self.min_cc_spin.setSuffix(" °")
        self.min_cc_spin.setValue(
            float(config.get("min_course_change_deg") or 0.5))
        self.min_cc_spin.setToolTip(
            "Every A/C whose course change is at least this angle is "
            "recorded as a hazard at the risk level chosen below; smaller "
            "course changes are ignored.")
        turns_form.addRow("Flag course changes of at least:", self.min_cc_spin)
        turns_note = QLabel(
            "Flagged A/Cs are recorded as hazards at the risk level below — "
            "the main consequence is running off route at the turn. The "
            "threshold is your criterion (e.g. from the tool's minimum "
            "turning radius); record its source underneath.")
        turns_note.setWordWrap(True)
        turns_note.setStyleSheet(ui_helpers.hint_style())
        turns_form.addRow(turns_note)
        layout.addWidget(self.turns_group)

        self.bands_group = QGroupBox("Spatial rating")
        bands_form = QFormLayout(self.bands_group)
        self.contact_combo = QComboBox()
        self.contact_combo.addItem("Use other criteria", "")
        for level in self.scale.levels:
            self.contact_combo.addItem(self.scale.caption(level), level)
        self.contact_combo.setCurrentIndex(max(0, self.contact_combo.findData(config.get("on_route_risk") or "")))
        bands_form.addRow("Risk on route contact", self.contact_combo)
        bands = QgsCollapsibleGroupBoxBasic("Distance bands")
        distance_form = QFormLayout(bands)
        self.band_spins: Dict[str, QDoubleSpinBox] = {}
        for level in reversed(self.scale.levels):
            key = f"band_{level}_m"
            spin = QDoubleSpinBox()
            spin.setRange(0.0, 1000000.0)
            spin.setDecimals(1)
            spin.setSuffix(" m")
            spin.setSpecialValueText("(off)")
            spin.setValue(float(config.get(key) or 0.0))
            self.band_spins[key] = spin
            distance_form.addRow(f"{self.scale.caption(level)} within", spin)
        bands_note = QLabel(
            "Route contacts are at 0 m and match every enabled distance band. "
            "The highest scoring spatial or attribute match wins.")
        bands_note.setWordWrap(True)
        bands_note.setStyleSheet(ui_helpers.hint_style())
        distance_form.addRow(bands_note)
        bands.setCollapsed(not any(spin.value() for spin in self.band_spins.values()))
        bands_form.addRow(bands)
        layout.addWidget(self.bands_group)

        self.attr_group = QgsCollapsibleGroupBoxBasic("Attribute and expression rules")
        attr_form = QFormLayout(self.attr_group)
        self.attribute_edit = FieldCombo(
            config.get("attribute") or "",
            "pick a field of the input layer (e.g. Height_m, Class)")
        self.attribute_edit.setToolTip(
            "The field the Value / Range rows below read. Expression rows "
            "can reference any field.")
        attr_form.addRow("Attribute:", self.attribute_edit)
        self.rules_table = AttributeRulesTable(with_kind=True, with_risk=True, risk_scale=self.scale)
        self.rules_table.set_attribute_name_provider(self.attribute_edit.text)
        self.rules_table.set_rules(config.get("attribute_rules") or [])
        attr_form.addRow("Rules:", self.rules_table)
        rules_note = QLabel(
            'The first matching row assigns a level. Expressions can combine fields, '
            'for example "Likelihood" * "Consequence" >= 15. The higher of the '
            'spatial and attribute scores wins.')
        rules_note.setWordWrap(True)
        rules_note.setStyleSheet(ui_helpers.hint_style())
        attr_form.addRow(rules_note)
        self.attr_group.setCollapsed(not bool(config.get("attribute_rules") or config.get("attribute")))
        layout.addWidget(self.attr_group)

        tail = QFormLayout()
        self.tail_form = tail
        self.default_combo = QComboBox()
        for level in ("", *self.scale.levels):
            self.default_combo.addItem(self.scale.caption(level), level)
        self.default_combo.setCurrentIndex(
            max(0, self.default_combo.findData(config.get("default_risk") or "")))
        self.default_combo.setToolTip(
            "Applied when a feature is inside the search distance but no "
            "band or attribute rule fires.")
        tail.addRow("Default risk", self.default_combo)
        self.source_edit = QLineEdit(self.check.get("source_ref") or "")
        self.source_edit.setPlaceholderText(
            "Document + revision these criteria come from")
        tail.addRow("Source reference:", self.source_edit)
        self.notes_edit = QLineEdit(self.check.get("notes") or "")
        tail.addRow("Notes:", self.notes_edit)
        layout.addLayout(tail)
        layout.addStretch(1)

        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.resize(620, 590)
        self._sync_input_layer()
        self._sync_kind()

    def _sync_input_layer(self, *_args) -> None:
        """Point the field pickers / expression builders at the input."""
        layer = resolve_input_layer(self.inputs,
                                    self.input_combo.currentData() or "")
        for widget in (self.attribute_edit, self.label_edit,
                       self.filter_edit, self.rules_table,
                       *self.kp_table_form.widgets()):
            widget.set_layer(layer)

    def _accept(self) -> None:
        turns = self.kind_combo.currentData() == risk_scan.CHECK_KIND_ROUTE_TURNS
        if turns and not (self.default_combo.currentData() or ""):
            QMessageBox.warning(
                self, "Burial Planner",
                "Choose a risk level for flagged A/Cs — with it unassigned "
                "the check records nothing.")
            return
        if not turns:
            if not (self.input_combo.currentData() or ""):
                QMessageBox.warning(
                    self, "Burial Planner",
                    "Pick a registered input for this feature check — "
                    "without one the check is skipped at scan time. "
                    "Register layers on the Inputs tab first.")
                return
            if self.kind_combo.currentData() == risk_scan.CHECK_KIND_KP_TABLE:
                layer = resolve_input_layer(
                    self.inputs, self.input_combo.currentData() or "")
                problems = self.kp_table_form.problems(layer)
                if problems:
                    QMessageBox.warning(self, "Burial Planner", " ".join(problems))
                    return
            filter_problem = expression_problem(self.filter_edit.text())
            if filter_problem:
                QMessageBox.warning(
                    self, "Burial Planner",
                    f"The feature filter does not parse: {filter_problem}")
                return
            bad_rules = self.rules_table.invalid_rows()
            if bad_rules:
                QMessageBox.warning(
                    self, "Burial Planner",
                    "These attribute rules are not valid and would be "
                    "ignored: " + "; ".join(bad_rules[:5])
                    + ". A range needs a numeric From and/or To; an "
                    "expression must parse.")
                return
            needs_attribute = any(
                "expression" not in rule for rule in self.rules_table.rules())
            if needs_attribute and not self.attribute_edit.text():
                QMessageBox.warning(
                    self, "Burial Planner",
                    "Pick the attribute the Value / Range rules read "
                    "(or use expression rules, which name their own fields).")
                return
        self.accept()

    def _sync_kind(self) -> None:
        turns = self.kind_combo.currentData() == risk_scan.CHECK_KIND_ROUTE_TURNS
        table = self.kind_combo.currentData() == risk_scan.CHECK_KIND_KP_TABLE
        self.turns_group.setVisible(turns)
        self.kp_group.setVisible(table)
        self.bands_group.setVisible(not turns and not table)
        self.attr_group.setVisible(not turns)
        for widget in (self.input_combo, self.filter_edit, self.label_edit):
            widget.setEnabled(not turns)
        self.distance_spin.setEnabled(not turns and not table)
        self.distance_hint.setVisible(not turns and not table)
        risk_label = self.tail_form.labelForField(self.default_combo)
        if risk_label is not None:
            risk_label.setText("Risk for flagged A/Cs" if turns else "Default risk")
        self.default_combo.setToolTip(
            "Every flagged A/C is recorded at this level." if turns else
            "Applied to a row when no attribute rule fires." if table else
            "Assigned when a feature matches the search and no contact, distance or attribute criterion applies.")
        if turns and not self.name_edit.text().strip():
            self.name_edit.setPlaceholderText("A/C course change")

    def result_check(self) -> Dict:
        turns = self.kind_combo.currentData() == risk_scan.CHECK_KIND_ROUTE_TURNS
        config: Dict = {
            "kind": self.kind_combo.currentData()
            or risk_scan.CHECK_KIND_FEATURES,
            "default_risk": self.default_combo.currentData() or "",
        }
        table = self.kind_combo.currentData() == risk_scan.CHECK_KIND_KP_TABLE
        if turns:
            # Simple by design: every A/C at or above the threshold becomes
            # a hazard at default_risk. (Older configs with turn_abs
            # attribute rules keep scanning unchanged until re-saved here.)
            config["min_course_change_deg"] = self.min_cc_spin.value()
        elif table:
            config.update({
                "input_id": self.input_combo.currentData() or "",
                "filter_expression": self.filter_edit.text().strip(),
                "label_attribute": self.label_edit.text().strip(),
                "attribute": self.attribute_edit.text().strip(),
            })
            self.kp_table_form.apply(config)
            rules = self.rules_table.rules()
            if rules:
                config["attribute_rules"] = rules
        else:
            config.update({
                "input_id": self.input_combo.currentData() or "",
                "distance_m": self.distance_spin.value(),
                "on_route_risk": self.contact_combo.currentData() or "",
                "filter_expression": self.filter_edit.text().strip(),
                "label_attribute": self.label_edit.text().strip(),
                "attribute": self.attribute_edit.text().strip(),
            })
            for key, spin in self.band_spins.items():
                if spin.value() > 0:
                    config[key] = spin.value()
            rules = self.rules_table.rules()
            if rules:
                config["attribute_rules"] = rules
        default_name = ("A/C course change" if turns
                        else "KP-range hazards" if table else "Risk check")
        check = dict(self.check)
        check.update({
            "name": self.name_edit.text().strip() or default_name,
            "config_json": json.dumps(config),
            "source_ref": self.source_edit.text().strip(),
            "notes": self.notes_edit.text(),
        })
        return check


class ManualHazardDialog(QDialog):
    """Add a user-entered hazard (desktop study item, fishing ground, …)."""

    def __init__(self, dock, parent=None, scale=DEFAULT_SCALE, scope=None):
        super().__init__(parent)
        self.dock = dock
        self.setWindowTitle("Add manual hazard")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("e.g. Charted wreck, Fishing area")
        form.addRow("Hazard:", self.label_edit)
        kp_row = QHBoxLayout()
        limits = sorted((scope.start_km, scope.end_km)) if scope is not None else (0.0, 100000.0)
        self.kp_spin = ui_helpers.KpSpinBox()
        self.kp_spin.setRange(*limits)
        self.kp_spin.setValue(limits[0])
        self.kp_spin.setSuffix(" km")
        kp_row.addWidget(self.kp_spin)
        pick = QPushButton("Pick…")
        pick.setToolTip("Pick the KP by clicking the route on the map.")
        pick.clicked.connect(self._pick_kp)
        kp_row.addWidget(pick)
        form.addRow("KP:", kp_row)
        self.range_check = QCheckBox("Extends to KP")
        self.range_check.setToolTip("Leave clear for a point hazard at a single KP.")
        self.end_spin = ui_helpers.KpSpinBox()
        self.end_spin.setRange(*limits)
        self.end_spin.setValue(limits[0])
        self.end_spin.setSuffix(" km")
        self.end_spin.setEnabled(False)
        self.range_check.toggled.connect(self.end_spin.setEnabled)
        range_row = QHBoxLayout()
        range_row.addWidget(self.range_check)
        range_row.addWidget(self.end_spin, 1)
        form.addRow("", range_row)
        self.risk_combo = QComboBox()
        for level in ("", *scale.levels):
            self.risk_combo.addItem(scale.caption(level), level)
        form.addRow("Risk:", self.risk_combo)
        self.notes_edit = QLineEdit()
        form.addRow("Notes:", self.notes_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self) -> None:
        if self.range_check.isChecked() \
                and self.end_spin.value() <= self.kp_spin.value():
            QMessageBox.warning(
                self, "Burial Planner",
                "The range end KP must be greater than the start KP "
                "for a range. Clear ‘Extends to KP’ for a point hazard.")
            return
        self.accept()

    def _pick_kp(self) -> None:
        # This dialog is application-modal while exec() runs: hide it for
        # the duration of the pick or the canvas can never take the click;
        # on_finished re-shows it on pick, cancel or tool switch alike.
        if self.dock.pick_kp_on_map(
                lambda kp: self.kp_spin.setValue(float(kp)),
                "Click the route to set the hazard KP (right-click cancels).",
                on_finished=self.show):
            self.hide()

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
        self._scan_task = None
        self._prescan_warnings: List[str] = []
        self._transient_status = ""

        layout = QVBoxLayout(self)
        self.overview = VerdictStrip()
        self.overview.kpClicked.connect(self.dock.goto_kp)
        self.overview.kpHovered.connect(self.dock.sync_hover_kp)
        layout.addWidget(self.overview)

        splitter = QSplitter(getattr(Qt, "Orientation", Qt).Vertical)

        checks_widget = QWidget()
        checks_layout = QVBoxLayout(checks_widget)
        checks_layout.setContentsMargins(0, 0, 0, 0)
        self.check_table = QTableWidget(0, len(_CHECK_COLUMNS))
        self.check_table.setHorizontalHeaderLabels(_CHECK_COLUMNS)
        findings_header = self.check_table.horizontalHeaderItem(_CHECK_FIRE_COL)
        if findings_header is not None:
            findings_header.setToolTip(
                "KP extent of this check's hazards along the scope; the bar "
                "colour is the check's worst assigned risk.")
        self.check_table.verticalHeader().setVisible(False)
        self.check_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.check_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self._fire_delegate = FireBarDelegate(self.check_table)
        self.check_table.setItemDelegateForColumn(
            _CHECK_FIRE_COL, self._fire_delegate)
        self._fire_hover = FireBarHover(self.check_table, _CHECK_FIRE_COL,
                                        self._fire_delegate,
                                        self._fire_tooltip)
        self._fire_hover.kpHovered.connect(self.dock.sync_hover_kp)
        header = self.check_table.horizontalHeader()
        header.setSectionResizeMode(0, HEADER_RESIZE_MODE_CONTENTS)
        header.setSectionResizeMode(_CHECK_FIRE_COL, HEADER_RESIZE_MODE_STRETCH)
        header.setSectionResizeMode(_CHECK_COUNT_COL,
                                    HEADER_RESIZE_MODE_CONTENTS)
        header.setSectionResizeMode(4, HEADER_RESIZE_MODE_CONTENTS)
        ui_helpers.enable_column_menu(
            self.check_table, "SubseaCableTools/BurialPlanner/risk_coverage_columns",
            always_visible=(0, 1, _CHECK_FIRE_COL))
        self._name_column = ui_helpers.ContentSizedColumn(
            self.check_table, 1, _CHECK_FIRE_COL,
            "SubseaCableTools/BurialPlanner/risk_name_width")
        header.setToolTip(
            "Drag the divider after Check to resize; double-click it to fit the "
            "names again. Right-click to show or hide coverage columns.")
        self.check_table.horizontalHeaderItem(4).setToolTip(
            "Length and percentage of the plan scope covered only by this check's "
            "hazard ranges, compared with other enabled checks. A point contributes zero length.")
        count_header = self.check_table.horizontalHeaderItem(_CHECK_COUNT_COL)
        if count_header is not None:
            count_header.setToolTip(
                "Hazards found by the check's last scan, and the length and "
                "share of the plan scope they cover, including overlaps with "
                "other checks. Point hazards count as findings and contribute zero length.")
        self.check_table.itemChanged.connect(self._on_check_item_changed)
        self.check_table.doubleClicked.connect(lambda _i: self._edit_check())
        self.check_table.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.check_table.customContextMenuRequested.connect(
            self._check_context_menu)
        checks_layout.addWidget(self.check_table, 1)

        check_buttons = QHBoxLayout()
        self.add_check_button = QPushButton("Add check…")
        self.add_check_button.clicked.connect(self._add_check)
        check_buttons.addWidget(self.add_check_button)
        self.check_actions = ui_helpers.menu_tool_button(
            "Selected check", (("Edit check…", self._edit_check),
                               ("Move up", lambda: self._move_check(-1)),
                               ("Move down", lambda: self._move_check(1)),
                               None, ("Delete check…", self._delete_check)),
            tooltip="Select a check to edit, move or delete it.")
        self.check_table.itemSelectionChanged.connect(self._sync_check_actions)
        check_buttons.addWidget(self.check_actions)
        check_buttons.addStretch(1)
        self.scale_button = QPushButton("Scoring scale…")
        self.scale_button.setToolTip("Edit this plan’s risk levels, numeric scores and colours.")
        self.scale_button.clicked.connect(self._edit_scale)
        check_buttons.addWidget(self.scale_button)
        self.run_button = ui_helpers.menu_tool_button(
            "Run checks",
            (("Run all enabled checks", lambda: self._run_checks()),
             ("Run selected check", self._run_selected_check)),
            tooltip="Scan the registered input layers for features on or "
                    "near the route and rebuild the hazard register. Your "
                    "review (risk overrides, status, notes) is carried over "
                    "by feature; manual hazards are never touched.")
        check_buttons.addWidget(self.run_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.hide()
        self.stop_button.clicked.connect(self._cancel_scan)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.progress.setMaximumWidth(160)
        checks_layout.addLayout(check_buttons)
        splitter.addWidget(checks_widget)

        hazards_widget = QWidget()
        hazards_layout = QVBoxLayout(hazards_widget)
        hazards_layout.setContentsMargins(0, 0, 0, 0)
        register_header = QHBoxLayout()
        self.register_label = QLabel("Hazard register")
        register_header.addWidget(self.register_label)
        register_header.addStretch(1)
        self.manual_button = QPushButton("Add manual hazard…")
        self.manual_button.clicked.connect(self._add_manual_hazard)
        register_header.addWidget(self.manual_button)
        self.export_button = QPushButton("Export CSV…")
        self.export_button.setToolTip("Export the full hazard register, including rows hidden by table filters.")
        self.export_button.clicked.connect(self._export_csv)
        register_header.addWidget(self.export_button)
        hazards_layout.addLayout(register_header)
        self.register = HazardRegister()
        self.hazard_table = self.register.table
        self.register.source.editRequested.connect(self._deferred_hazard_edit)
        self.hazard_table.selectionModel().selectionChanged.connect(self._on_hazard_selected)
        self.hazard_table.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.hazard_table.customContextMenuRequested.connect(self._hazard_context_menu)
        self.hazard_table.doubleClicked.connect(
            lambda index: self._goto_hazard_row(index.row())
            if index.column() not in (RISK, STATUS, NOTES) else None)
        hazards_layout.addWidget(self.register, 1)
        splitter.addWidget(hazards_widget)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        status_row = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.progress)
        status_row.addWidget(self.stop_button)
        layout.addLayout(status_row)

        refresh_soon = ui_helpers.coalesced(self, self.refresh)
        model.planChanged.connect(refresh_soon)
        model.riskChanged.connect(refresh_soon)
        model.inputsChanged.connect(refresh_soon)
        model.analysisChanged.connect(refresh_soon)
        self.refresh()

    # -- refresh --------------------------------------------------------------
    def refresh(self) -> None:
        self._loading = True
        try:
            has_plan = bool(self.model.plan)
            self.scale = self.model.risk_scale()
            self.scale_button.setEnabled(has_plan and self._scan_task is None)
            self.add_check_button.setEnabled(has_plan)
            self.manual_button.setEnabled(has_plan)
            self.export_button.setEnabled(bool(self.model.hazards))
            self.run_button.setEnabled(has_plan and self._scan_task is None)
            params = self.model.gen_params()
            scope = params.scope
            checks = self.model.risk_checks
            hazards = self.model.hazards
            by_check: Dict[str, List[Dict]] = {}
            for hazard in hazards:
                by_check.setdefault(str(hazard.get("check_id") or ""),
                                    []).append(hazard)
            run_state = self.model.risk_status() if has_plan else {}
            self._run_state = run_state
            domain_km = scope.length_km if has_plan else 0.0
            ranges_by_check = {cid: [(s, e) for s, e, _level in risk.hazard_spans(found, 0, self.scale) if e > s]
                               for cid, found in by_check.items()}
            enabled_ids = {str(c.get("check_id") or "") for c in checks
                           if int(c.get("enabled") or 0)}
            exclusive = coverage.route_coverage(
                {cid: ranges_by_check.get(cid, []) for cid in enabled_ids}, scope)
            risk_colors = ui_helpers.risk_colors(self.scale)
            unique_current = all(
                (run_state.get(cid) or {}).get("state") == analysis_state.STATE_CURRENT
                for cid in enabled_ids)

            self._checks_view = ui_helpers.preserve_table_view(
                self.check_table)
            self._checks_view.__enter__()
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
                name_item.setToolTip((check.get("name") or "") + (
                    f"\nSource: {check.get('source_ref')}" if check.get("source_ref") else ""))
                self.check_table.setItem(i, 1, name_item)

                found = by_check.get(str(check.get("check_id") or ""), [])
                worst = schema.RISK_UNASSIGNED
                for hazard in found:
                    worst = risk.risk_max(worst, hazard.get("risk") or "", scale=self.scale)
                check_id = str(check.get("check_id") or "")
                intervals = [(iv.start_km, iv.end_km) for iv in eng.clip_intervals(
                    [Interval(s, e) for s, e, _ in risk.hazard_spans(found, scale=self.scale)], scope)]
                run = run_state.get(check_id) or {}
                state = run.get("state", analysis_state.STATE_NEW)
                stale = bool(found) and state == analysis_state.STATE_CHANGED
                fire_item = QTableWidgetItem()
                fire_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                   | Qt.ItemFlag.ItemIsSelectable)
                fire_item.setData(
                    ITEM_DATA_USER_ROLE,
                    (scope.length_km, intervals, risk_colors[worst],
                     scope.start_km, {"stale": stale}))
                self.check_table.setItem(i, _CHECK_FIRE_COL, fire_item)

                covered = (exclusive[check_id].total_km if check_id in exclusive else
                           sum(iv.length_km for iv in eng.clip_intervals(
                               [Interval(s, e) for s, e in ranges_by_check.get(check_id, [])], scope)))
                count_text, count_tip = self._count_cell(
                    found, covered, domain_km, run, state,
                    bool(int(check.get("enabled") or 0)))
                count_item = QTableWidgetItem(count_text)
                count_item.setToolTip(count_tip)
                count_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                    | Qt.ItemFlag.ItemIsSelectable)
                self.check_table.setItem(i, _CHECK_COUNT_COL, count_item)
                if check_id not in enabled_ids:
                    unique_text, unique_tip = "off", "Disabled — not part of the comparison."
                elif not unique_current:
                    unique_text = "—"
                    unique_tip = ("Run all enabled checks to compare their coverage: "
                                  "at least one scan is missing or out of date.")
                else:
                    contribution = exclusive[check_id]
                    unique_text = coverage.length_label(contribution.unique_km, domain_km)
                    unique_tip = (
                        f"Covered only by this check: {unique_text} of the plan scope.\n"
                        f"Also covered by other enabled checks: "
                        f"{max(0.0, contribution.total_km - contribution.unique_km):.3f} km.\n"
                        "Only the recorded KP ranges contribute length. "
                        "Point symbols are widened on the strip for visibility.")
                unique_item = QTableWidgetItem(unique_text)
                unique_item.setToolTip(unique_tip)
                unique_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.check_table.setItem(i, 4, unique_item)
            self._checks_view.__exit__(None, None, None)
            self._sync_check_actions()
            self._name_column.fit()

            self._refresh_hazards()
            self._refresh_overview(scope)
            self._refresh_status_line()
        finally:
            self._loading = False

    @staticmethod
    def _count_cell(found: List[Dict], covered: float, domain_km: float,
                    run: Dict, state: str, enabled: bool):
        """(text, tooltip) for a check's Hazards · extent cell."""
        when = analysis_state.format_utc(run.get("run_utc") or "")
        if found:
            pct = 100.0 * covered / domain_km if domain_km > 0 else 0.0
            text = f"{len(found)} · {covered:.3f} km · {pct:.1f}%"
            tip = (f"{len(found)} hazard(s) over {covered:.3f} km "
                   f"({pct:.2f}% of the scope).")
        elif state in (analysis_state.STATE_CURRENT,
                       analysis_state.STATE_CHANGED):
            text = "none"
            tip = "Scanned — no features found on or near the route."
        elif not enabled:
            return "off", "Disabled — not scanned by Run all."
        else:
            return "—", "Not scanned yet — Run ▾ scans the enabled checks."
        if when:
            tip += f"\nLast scan: {when}."
        if state == analysis_state.STATE_CHANGED:
            text += " (out of date)"
            tip += ("\nRun again to verify this result against the current route, "
                    "check settings, input and KP method.")
        for warning in (run.get("warnings") or [])[:3]:
            tip += f"\n⚠ {warning}"
        return text, tip

    def _refresh_status_line(self) -> None:
        if self._transient_status or self._scan_task is not None:
            self.status_label.setText(self._transient_status)
            return
        runs = self.model.risk_runs() if self.model.plan else {}
        message = runs.get("message") or ""
        when = analysis_state.format_utc(runs.get("run_utc") or "")
        stale = sum(1 for info in (getattr(self, "_run_state", {}) or {}).values()
                    if info.get("state") == analysis_state.STATE_CHANGED)
        text = ""
        if when:
            text = f"Last scan {when}: {message}" if message else \
                f"Last scan {when}."
        if stale:
            text += (f"  ·  {stale} check(s) changed since their scan — "
                     "run them again.")
        self.status_label.setText(text.strip())

    def _set_transient(self, text: str) -> None:
        self._transient_status = text or ""
        self._refresh_status_line()

    def _fire_tooltip(self, row: int, kp: float, px_km: float) -> str:
        checks = self.model.risk_checks
        if row >= len(checks):
            return f"KP {kp:.3f}"
        check = checks[row]
        check_id = str(check.get("check_id") or "")
        lines = [f"KP {kp:.3f} — {check.get('name') or 'check'}"]
        near = self._hazards_by_distance(check_id, kp)
        tol = max(px_km, 0.025)  # a point hazard is drawn ±25 m wide
        under = [(d, h) for d, h in near if d <= tol]
        for _d, hazard in under[:5]:
            lines.append(self._hazard_line(hazard))
        if len(under) > 5:
            lines.append(f"… {len(under) - 5} more here")
        if not under:
            if near:
                distance, hazard = near[0]
                lines.append(f"Clear here — nearest: {hazard.get('label') or 'hazard'}"
                             f", {distance * 1000:,.0f} m away")
            else:
                lines.append("No hazards from this check")
        lines.append("Right-click for the hazards nearest this KP.")
        return "\n".join(lines)

    def _hazard_line(self, hazard: Dict) -> str:
        start = float(hazard.get("kp") or 0.0)
        end = float(hazard.get("end_kp") or start)
        lo, hi = min(start, end), max(start, end)
        where = (f"KP {lo:.3f}–{hi:.3f}" if hi - lo > 5e-4
                 else f"KP {lo:.3f}")
        level = self.scale.caption(hazard.get("risk"))
        return (f"{hazard.get('label') or 'hazard'} — {where}"
                + (f"  [{level}]" if level else ""))

    def _hazards_by_distance(self, check_id: str, kp: float):
        """``[(distance_km, hazard)]`` nearest-first (0 = under the KP)."""
        out = []
        for hazard in self.model.hazards:
            if str(hazard.get("check_id") or "") != check_id:
                continue
            try:
                start = float(hazard.get("kp") or 0.0)
                end = float(hazard.get("end_kp") if hazard.get("end_kp")
                            is not None else start)
            except (TypeError, ValueError):
                continue
            lo, hi = min(start, end), max(start, end)
            distance = 0.0 if lo <= kp <= hi else min(abs(kp - lo),
                                                      abs(kp - hi))
            out.append((distance, hazard))
        out.sort(key=lambda item: (item[0], float(item[1].get("kp") or 0.0)))
        return out

    def _refresh_overview(self, scope) -> None:
        spans = []
        risk_colors = ui_helpers.risk_colors(self.scale)
        for start_km, end_km, level in risk.hazard_spans(self.model.hazards, scale=self.scale):
            spans.append((start_km, end_km, risk_colors.get(
                level, risk_colors[schema.RISK_UNASSIGNED]),
                f"{self.scale.caption(level)} risk"))
        self.overview.set_spans(scope.length_km, spans, "Risk",
                                domain_start_km=scope.start_km)

    def _refresh_hazards(self) -> None:
        hazards = self.model.hazards
        counts = risk.summarise_hazards(hazards, self.scale)
        total = counts["total"]
        self.register_label.setText(f"Hazard register · {total:,} total · {counts['open']:,} open")
        self.register_label.setToolTip("\n".join(
            f"{self.scale.caption(level)}: {counts[level]:,}" for level in (*reversed(self.scale.levels), "")
            if counts[level]))
        self.register.replace(hazards, self.model.risk_checks, self.scale, self.model.plan_id)

    def _deferred_hazard_edit(self, hazard_id: str, field: str,
                              value: str) -> None:
        if self._loading or not hazard_id:
            return
        plan_id = self.model.plan_id

        def apply() -> None:
            if self.model.plan_id != plan_id:
                return
            hazard = next((h for h in self.model.hazards
                           if h.get("hazard_id") == hazard_id), None)
            if hazard is None or (hazard.get(field) or "") == (value or ""):
                return
            self.model.update_hazards([hazard_id], {field: value or ""})

        QTimer.singleShot(0, apply)

    # -- selections / navigation ---------------------------------------------
    def _selected_hazard_ids(self) -> List[str]:
        return self.register.selected_ids()

    def _hazard_for_row(self, row: int) -> Optional[Dict]:
        return self.register.hazard_at(row)

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

    def _hazard_context_menu(self, position) -> None:
        index = self.hazard_table.indexAt(position)
        if not index.isValid():
            return
        row = index.row()
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
        for level in ("", *self.scale.levels):
            risk_actions[risk_menu.addAction(self.scale.caption(level))] = level
        reset_action = menu.addAction(f"Restore automatic rating{suffix}")
        selected_ids = set(ids)
        reset_action.setEnabled(any(h.get("hazard_id") in selected_ids and h.get("check_id")
                                    for h in self.model.hazards))
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
            self.model.update_hazards(ids, {"risk_source": schema.RISK_SOURCE_AUTO})
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
    def _sync_check_actions(self) -> None:
        self.check_actions.setEnabled(self.check_table.selectionModel().hasSelection())

    def _edit_scale(self) -> None:
        dialog = RiskScaleDialog(self.scale, risk.used_levels(self.model.risk_checks, self.model.hazards), self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            try:
                self.model.set_risk_scale(dialog.scale.rows())
            except ValueError as exc:
                QMessageBox.warning(self, dialog.windowTitle(), str(exc))

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
        dialog = CheckEditorDialog(check, self.model.inputs, self,
                                   model=self.model)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            checks = list(self.model.risk_checks) + [dialog.result_check()]
            self.model.save_risk_checks(checks, target_id=check["check_id"])

    def _edit_check(self) -> None:
        index = self._selected_check_index()
        if index < 0 or index >= len(self.model.risk_checks):
            return
        dialog = CheckEditorDialog(self.model.risk_checks[index],
                                   self.model.inputs, self, model=self.model)
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
        self.model.delete_risk_check(check_id)

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

    def _run_selected_check(self) -> None:
        index = self._selected_check_index()
        if index < 0 or index >= len(self.model.risk_checks):
            self._set_transient("Select a check to run it alone.")
            return
        check_id = str(self.model.risk_checks[index].get("check_id") or "")
        self._run_checks([check_id])

    # -- scan (background task, BurialAnalysisTask pattern) --------------------
    def _run_checks(self, check_ids: Optional[List[str]] = None) -> None:
        if not self.model.plan:
            self._set_transient("Open a plan first.")
            return
        if self._scan_task is not None \
                and not analysis_task.task_is_active(self._scan_task):
            self._scan_task = None  # ended without reporting back
        if self._scan_task is not None:
            self._set_transient("A scan is already running.")
            return
        if self.model.route is None:
            QMessageBox.warning(
                self, "Burial Planner",
                "The plan's route is not available — set the route on the "
                "Plan tab first.")
            return
        wanted = {str(c) for c in check_ids or []}
        checks = [c for c in self.model.risk_checks
                  if int(c.get("enabled") or 0)
                  and (not wanted or str(c.get("check_id") or "") in wanted)]
        if not checks:
            self._set_transient("No enabled checks — add a check first.")
            return
        from qgis.core import QgsApplication, QgsGeometry, QgsProject

        params = self.model.gen_params()
        scope = Interval(params.scope.start_km, params.scope.end_km)
        inputs_by_id = {str(r.get("input_id") or ""): r
                        for r in self.model.inputs}
        # Feature loading happens here on the main thread; the geometry
        # work runs on the task with cloned route geometries.
        route_geoms = [QgsGeometry(g) for g in self.model.route.geometries]
        route_geom = QgsGeometry.collectGeometry(
            [QgsGeometry(g) for g in route_geoms])
        jobs = []
        warnings: List[str] = []
        # Load + index each distinct input layer once, however many checks
        # scan it (previously once per check).
        layer_cache: Dict[str, tuple] = {}
        # KP-range tables: one RPL translation per reference RPL.
        from ..rpl_reference import table_kp_map
        translations: Dict = {}

        def kp_reference(config: Dict):
            return table_kp_map(self.model, config, translations)
        for check in checks:
            config = risk.check_config(check)
            if risk_scan.check_kind(config) == risk_scan.CHECK_KIND_ROUTE_TURNS:
                jobs.append((dict(check), None))
                continue
            input_row = inputs_by_id.get(str(config.get("input_id") or ""))
            if input_row is None:
                warnings.append(
                    f"Check '{check.get('name')}': no registered input "
                    "selected — skipped.")
                continue
            layer = map_layers.resolve_input_layer(
                QgsProject.instance(), input_row)
            if risk_scan.check_kind(config) == risk_scan.CHECK_KIND_KP_TABLE:
                try:
                    entries, notes = risk_scan.snapshot_kp_table(
                        check, layer, kp_reference)
                except risk_scan.RiskScanError as exc:
                    warnings.append(str(exc))
                    continue
                warnings.extend(notes)
                jobs.append((dict(check), entries))
                continue
            try:
                preloaded = None
                layer_key = layer.id() if layer is not None else ""
                if layer_key:
                    preloaded = layer_cache.get(layer_key)
                    if preloaded is None and layer.isValid():
                        from ...workbench import rules_inputs as ri

                        preloaded = ri.load_features_wgs84(
                            layer, QgsProject.instance())
                        layer_cache[layer_key] = preloaded
                features = risk_scan.snapshot_check_features(
                    check, layer, route_geom, preloaded=preloaded)
            except risk_scan.RiskScanError as exc:
                warnings.append(str(exc))
                continue
            except Exception as exc:
                warnings.append(
                    f"Check '{check.get('name')}': the input layer could "
                    f"not be read ({exc}) — skipped.")
                continue
            jobs.append((dict(check), features))
        if not jobs:
            self._set_transient("  ·  ".join(warnings) or "Nothing to scan.")
            return
        self._prescan_warnings = warnings
        task = risk_scan.RiskScanTask(
            self.model.plan_id, jobs, route_geoms,
            QgsProject.instance().transformContext(), scope,
            self.model.direction, self._scan_finished,
            start_kp_km=(self.model.route.start_kp_km
                         if self.model.route is not None else 0.0),
            kp_mode=self.model.kp_mode(), scale=self.model.risk_scale())
        self._scan_fingerprints = self.model.risk_check_fingerprints()
        task.progressMessage.connect(self._set_transient)
        task.progressChanged.connect(
            lambda pct: self.progress.setValue(int(pct)))
        self._scan_task = task
        self.scale_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.stop_button.show()
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._set_transient("Scanning…")
        QgsApplication.taskManager().addTask(task)

    def _cancel_scan(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()

    def shutdown(self) -> None:
        """Cancel and forget a running scan (dock close / plugin unload).

        Without this a task that never delivers ``finished`` left the Run
        button permanently reporting "a scan is already running".
        """
        task = self._scan_task
        self._scan_task = None
        if task is not None:
            try:
                task.cancel()
            except RuntimeError:
                pass  # the task manager already deleted it
        self.run_button.setEnabled(bool(self.model.plan))
        self.scale_button.setEnabled(bool(self.model.plan))
        self.stop_button.setEnabled(False)
        self.progress.setVisible(False)
        self.stop_button.hide()

    def _scan_finished(self, task) -> None:
        """Completion callback on the main thread."""
        if task is not self._scan_task:
            # Forgotten by shutdown() (dock closed / plugin unloaded): a
            # scan that completed before its cancel took effect must not
            # write hazards later, nor clear a newer scan's reference.
            return
        self._scan_task = None
        self.run_button.setEnabled(bool(self.model.plan))
        self.scale_button.setEnabled(bool(self.model.plan))
        self.stop_button.setEnabled(False)
        self.progress.setVisible(False)
        self.stop_button.hide()
        if task.plan_id != self.model.plan_id:
            # The user switched plans while the scan ran — these hazards
            # belong to the plan the scan was started on, not this one.
            self._flash("Scan discarded — a different plan is now open. "
                        "Re-run the scan with its plan selected.")
            return
        if task.cancelled:
            self._flash("Scan stopped — the hazard register was not changed.")
            return
        if task.error:
            self._flash(f"Scan failed: {task.error}")
            return
        current = self.model.risk_check_fingerprints()
        if any(current.get(cid) != self._scan_fingerprints.get(cid) for cid in task.run_check_ids):
            self._flash("The route or check settings changed during the scan. Run the checks again.")
            return
        message = (f"Scanned {len(task.run_check_ids)} check(s): "
                   f"{len(task.hazards)} hazard(s).")
        warnings = self._prescan_warnings + task.warnings
        if warnings:
            message += "  ·  " + "  ·  ".join(warnings[:4])
            if len(warnings) > 4:
                message += f"  ·  … {len(warnings) - 4} more"
        self._transient_status = ""
        if task.run_check_ids:
            if not self.model.apply_risk_scan(task.hazards, check_ids=task.run_check_ids):
                self._flash("The scan results could not be saved.")
                return
            counts: Dict[str, int] = {}
            for hazard in task.hazards:
                key = str(hazard.get("check_id") or "")
                counts[key] = counts.get(key, 0) + 1
            per_check: Dict[str, List[str]] = {}
            names = {str(c.get("check_id") or ""): (c.get("name") or "")
                     for c in self.model.risk_checks}
            for warning in warnings:
                for check_id, name in names.items():
                    if name and f"'{name}'" in warning:
                        per_check.setdefault(check_id, []).append(warning)
            self.model.record_risk_run(task.run_check_ids, counts,
                                       per_check, message)
        else:
            self._flash(message)
        self._refresh_status_line()

    def _flash(self, message: str) -> None:
        """A transient outcome line that yields to the stored-scan summary."""
        self._set_transient(message)
        QTimer.singleShot(6000, lambda m=message: self._expire_transient(m))

    def _expire_transient(self, message: str) -> None:
        try:
            if self._transient_status == message:
                self._set_transient("")
        except RuntimeError:  # widget already deleted
            pass

    def _check_context_menu(self, position) -> None:
        item = self.check_table.itemAt(position)
        if item is None:
            return
        row = item.row()
        self.check_table.selectRow(row)
        if row >= len(self.model.risk_checks):
            return
        check = self.model.risk_checks[row]
        check_id = str(check.get("check_id") or "")
        # On the bar: nearest the clicked KP first; elsewhere: KP order.
        hit = self._fire_hover.kp_at(position)
        if hit is not None:
            click_kp = hit[1]
            found = [h for _d, h in self._hazards_by_distance(check_id,
                                                              click_kp)]
        else:
            click_kp = None
            found = risk.sort_hazards(
                [h for h in self.model.hazards
                 if str(h.get("check_id") or "") == check_id], self.scale)
        menu = QMenu(self)
        edit_action = menu.addAction("Edit check…")
        run_action = menu.addAction("Run this check")
        run_action.setEnabled(self._scan_task is None)
        delete_action = menu.addAction("Delete check")
        goto_actions = {}
        if found:
            menu.addSeparator()
            if click_kp is not None:
                header = menu.addAction(f"Hazards nearest KP {click_kp:.3f}:")
                header.setEnabled(False)
            shown = found[:20]
            for hazard in shown:
                start = float(hazard.get("kp") or 0.0)
                end = float(hazard.get("end_kp") or start)
                if abs(end - start) > 5e-4:
                    where = (f"KP {schema.format_kp(min(start, end))}-"
                             f"{schema.format_kp(max(start, end))}")
                else:
                    where = f"KP {schema.format_kp(start)}"
                level = self.scale.caption(hazard.get("risk"))
                action = menu.addAction(
                    f"Go to {hazard.get('label') or 'hazard'} — {where}"
                    + (f"  [{level}]" if level else ""))
                goto_actions[action] = (min(start, end), max(start, end))
            if len(found) > len(shown):
                more = menu.addAction(
                    f"… {len(found) - len(shown)} more in the register")
                more.setEnabled(False)
        chosen = qt_exec(menu,
                         self.check_table.viewport().mapToGlobal(position))
        if chosen == edit_action:
            self._edit_check()
        elif chosen == run_action:
            self._run_checks([check_id])
        elif chosen == delete_action:
            self._delete_check()
        elif chosen in goto_actions:
            start, end = goto_actions[chosen]
            if end - start > 5e-4:
                self.dock.goto_range(start, end)
            else:
                self.dock.goto_kp(start)

    # -- manual hazards / export ----------------------------------------------
    def _add_manual_hazard(self) -> None:
        if not self.model.plan:
            return
        dialog = ManualHazardDialog(self.dock, self, scale=self.scale, scope=self.model.gen_params().scope)
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
        self._flash(f"Exported {len(self.model.hazards)} hazard(s).")
