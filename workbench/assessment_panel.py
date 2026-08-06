# -*- coding: utf-8 -*-
"""Route-suitability / burial-assessment panel.

An Excel-conditional-formatting-style rule stack crossed with the SLD strip:

- a top *overview bar* showing how the ordered stack combines for the selected
  method (green allowed / amber risk / red excluded) along route KP;
- a *rule stack* table where each rule row carries its own fire-bar, pixel
  aligned under the overview bar, showing exactly where that rule fires and how
  much of the route it rules out;
- a collapsible *results* table of the resolved ranges.

The engine (rules_engine) and acquisition (rules_inputs) are headless; this
module is only presentation + orchestration.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
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
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..qgis_compat import (
    DIALOG_ACCEPTED,
    MAP_LAYER_FILTER_POLYGON,
    MAP_LAYER_FILTER_VECTOR,
    layer_filters, qt_exec,
)
from . import assessment_output, rules_inputs, schema
from .rules_engine import Interval
from .selection_bus import selection_bus

# Colours shared by the overview bar, fire-bars and (conceptually) the layer.
STATUS_COLORS = {
    schema.STATUS_ALLOWED: QColor("#2ca02c"),
    schema.STATUS_RISK: QColor("#ff8c00"),
    schema.STATUS_EXCLUDED: QColor("#d62728"),
}
ACTION_COLORS = {
    schema.RULE_ACTION_EXCLUDE: QColor("#d62728"),
    schema.RULE_ACTION_RISK: QColor("#ff8c00"),
    schema.RULE_ACTION_ALLOW: QColor("#2ca02c"),
}
_EMPTY_BG = QColor(0, 0, 0, 18)

FIRE_COL = 2  # index of the fire-bar column in the rule table


# ---------------------------------------------------------------------------
# Small painting widgets
# ---------------------------------------------------------------------------


def _paint_spans(painter: QPainter, rect, domain_km: float,
                 spans: List, radius: int = 2) -> None:
    """spans: list of (start_km, end_km, QColor)."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.fillRect(rect, _EMPTY_BG)
    if domain_km <= 0:
        painter.restore()
        return
    x0, w = rect.x(), rect.width()
    for start_km, end_km, color in spans:
        sx = x0 + (max(0.0, start_km) / domain_km) * w
        ex = x0 + (min(domain_km, end_km) / domain_km) * w
        if ex - sx < 1.0:
            ex = sx + 1.0
        painter.fillRect(int(sx), rect.y() + 2, int(ex - sx), rect.height() - 4, color)
    painter.restore()


class VerdictStrip(QWidget):
    """Overview bar: paints the combined verdict spans for one method."""

    kpClicked = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(30)
        self._domain_km = 0.0
        self._spans: List = []
        self._method_name = ""
        self.setToolTip("Combined suitability for the selected method. Click to locate on the map.")

    def set_spans(self, domain_km: float, spans: List, method_name: str = "") -> None:
        self._domain_km = domain_km
        self._spans = spans
        self._method_name = method_name or ""
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        rect = self.rect().adjusted(0, 0, -1, -1)
        _paint_spans(painter, rect, self._domain_km, self._spans)
        painter.setPen(QPen(QColor(120, 120, 120)))
        painter.drawRect(rect)
        painter.setPen(QPen(QColor(40, 40, 40)))
        if self._method_name:
            painter.drawText(rect.adjusted(6, 0, -6, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             self._method_name)
        if self._domain_km > 0:
            painter.drawText(rect.adjusted(6, 0, -6, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             f"0 - {self._domain_km:.1f} km")

    def mousePressEvent(self, event):
        if self._domain_km > 0 and self.width() > 0:
            kp = (event.pos().x() / self.width()) * self._domain_km
            self.kpClicked.emit(max(0.0, min(self._domain_km, kp)))


class FireBarDelegate(QStyledItemDelegate):
    """Paints a rule's fire intervals in its table cell (domain-aligned)."""

    def paint(self, painter, option, index):
        data = index.data(Qt.ItemDataRole.UserRole)
        if not data:
            super().paint(painter, option, index)
            return
        domain_km, intervals, color = data
        spans = [(s, e, color) for (s, e) in intervals]
        _paint_spans(painter, option.rect.adjusted(2, 0, -2, 0), domain_km, spans)


# ---------------------------------------------------------------------------
# Rule editor
# ---------------------------------------------------------------------------


def parse_scope(text: str) -> List[Dict]:
    ranges = []
    for chunk in (text or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        for sep in ("-", "–", ".."):
            if sep in chunk:
                a, _, b = chunk.partition(sep)
                try:
                    ranges.append({"start_kp": float(a), "end_kp": float(b)})
                except ValueError:
                    pass
                break
    return ranges


def format_scope(ranges: List[Dict]) -> str:
    return ", ".join(f"{float(r['start_kp']):.3f}-{float(r['end_kp']):.3f}"
                     for r in ranges or [] if "start_kp" in r and "end_kp" in r)


class RuleEditorDialog(QDialog):
    """Edit one rule. Kind is fixed at creation; complexity comes from stacking."""

    def __init__(self, rule: Dict, methods: List[str], parent=None):
        super().__init__(parent)
        self.rule = dict(rule)
        self.methods = methods
        self.setWindowTitle(f"Rule — {schema_kind_label(self.rule.get('kind'))}")
        self.setMinimumWidth(420)
        config = _load_json(self.rule.get("config_json"), {})

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.name_edit = QLineEdit(self.rule.get("name") or "")
        form.addRow("Name", self.name_edit)

        self.action_combo = QComboBox()
        self.action_combo.addItems([schema.RULE_ACTION_EXCLUDE, schema.RULE_ACTION_RISK,
                                    schema.RULE_ACTION_ALLOW])
        self.action_combo.setCurrentText(self.rule.get("action") or schema.RULE_ACTION_EXCLUDE)
        self.action_combo.currentTextChanged.connect(self._sync_risk)
        form.addRow("Action", self.action_combo)

        self.risk_spin = QSpinBox()
        self.risk_spin.setRange(1, 3)
        self.risk_spin.setValue(int(self.rule.get("risk_level") or 1))
        form.addRow("Risk level (1–3)", self.risk_spin)

        # methods this rule applies to
        methods_box = QGroupBox("Applies to methods")
        mlayout = QHBoxLayout(methods_box)
        selected = _load_json(self.rule.get("methods_json"), list(methods))
        self.method_checks: Dict[str, QCheckBox] = {}
        for method in methods:
            cb = QCheckBox(method)
            cb.setChecked(method in selected)
            self.method_checks[method] = cb
            mlayout.addWidget(cb)
        layout.addWidget(methods_box)

        # kind-specific form
        self.kind_form = QFormLayout()
        kbox = QGroupBox("Condition")
        kbox.setLayout(self.kind_form)
        layout.addWidget(kbox)
        self._build_kind_form(self.rule.get("kind"), config)

        # scope
        self.scope_edit = QLineEdit(format_scope(config.get("scope_ranges")))
        self.scope_edit.setPlaceholderText("whole route (or e.g. 12.0-45.0, 80-92)")
        form2 = QFormLayout()
        form2.addRow("Applies to KP", self.scope_edit)
        layout.addLayout(form2)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_risk()

    def _sync_risk(self):
        self.risk_spin.setEnabled(self.action_combo.currentText() == schema.RULE_ACTION_RISK)

    def _build_kind_form(self, kind: str, config: Dict):
        self._widgets: Dict[str, QWidget] = {}
        if kind == schema.RULE_KIND_THRESHOLD:
            self.profile_combo = QComboBox()
            self.profile_combo.addItems(["depth", "slope"])
            self.profile_combo.setCurrentText(config.get("profile", "depth"))
            self.op_combo = QComboBox()
            self.op_combo.addItems([">", ">=", "<", "<=", "between"])
            self.op_combo.setCurrentText(config.get("op", ">"))
            self.value_spin = QDoubleSpinBox()
            self.value_spin.setRange(-12000.0, 12000.0)
            self.value_spin.setDecimals(2)
            self.value_spin.setValue(float(config.get("value", 0.0)))
            self.value2_spin = QDoubleSpinBox()
            self.value2_spin.setRange(-12000.0, 12000.0)
            self.value2_spin.setDecimals(2)
            self.value2_spin.setValue(float(config.get("value2") or 0.0))
            self.kind_form.addRow("Profile", self.profile_combo)
            self.kind_form.addRow("Operator", self.op_combo)
            self.kind_form.addRow("Value (m / deg)", self.value_spin)
            self.kind_form.addRow("Upper value (between)", self.value2_spin)
        elif kind == schema.RULE_KIND_PROXIMITY:
            self.layer_combo = QgsMapLayerComboBox()
            self.layer_combo.setFilters(layer_filters(MAP_LAYER_FILTER_VECTOR))
            _preselect_layer(self.layer_combo, config.get("layer_id"))
            self.dist_spin = QDoubleSpinBox()
            self.dist_spin.setRange(0.0, 100000.0)
            self.dist_spin.setValue(float(config.get("distance_m", 250.0)))
            self.mode_combo = QComboBox()
            self.mode_combo.addItems(["distance", "intersect"])
            self.mode_combo.setCurrentText(config.get("mode", "distance"))
            self.filter_edit = QLineEdit(config.get("filter_expression", ""))
            self.kind_form.addRow("Hazard layer", self.layer_combo)
            self.kind_form.addRow("Distance (m)", self.dist_spin)
            self.kind_form.addRow("Mode", self.mode_combo)
            self.kind_form.addRow("Filter expression", self.filter_edit)
        elif kind == schema.RULE_KIND_POLYGON:
            self.layer_combo = QgsMapLayerComboBox()
            self.layer_combo.setFilters(layer_filters(MAP_LAYER_FILTER_POLYGON))
            _preselect_layer(self.layer_combo, config.get("layer_id"))
            self.field_combo = QgsFieldComboBox()
            self.field_combo.setLayer(self.layer_combo.currentLayer())
            self.field_combo.setField(config.get("attribute", ""))
            self.layer_combo.layerChanged.connect(self.field_combo.setLayer)
            self.values_edit = QLineEdit(", ".join(config.get("match_values", [])))
            self.kind_form.addRow("Polygon layer", self.layer_combo)
            self.kind_form.addRow("Attribute", self.field_combo)
            self.kind_form.addRow("Match values (comma)", self.values_edit)
        elif kind == schema.RULE_KIND_KP_TABLE:
            self.layer_combo = QgsMapLayerComboBox()
            self.layer_combo.setFilters(layer_filters(MAP_LAYER_FILTER_VECTOR))
            _preselect_layer(self.layer_combo, config.get("layer_id"))
            self.start_field = QgsFieldComboBox()
            self.start_field.setLayer(self.layer_combo.currentLayer())
            self.start_field.setField(config.get("start_field", "start_kp"))
            self.end_field = QgsFieldComboBox()
            self.end_field.setLayer(self.layer_combo.currentLayer())
            self.end_field.setField(config.get("end_field", "end_kp"))
            self.layer_combo.layerChanged.connect(self.start_field.setLayer)
            self.layer_combo.layerChanged.connect(self.end_field.setLayer)
            self.filter_edit = QLineEdit(config.get("filter_expression", ""))
            self.kind_form.addRow("KP-range layer", self.layer_combo)
            self.kind_form.addRow("Start KP field", self.start_field)
            self.kind_form.addRow("End KP field", self.end_field)
            self.kind_form.addRow("Filter expression", self.filter_edit)
        elif kind == schema.RULE_KIND_MANUAL:
            self.ranges_edit = QLineEdit(format_scope(config.get("ranges")))
            self.ranges_edit.setPlaceholderText("e.g. 12.0-14.5, 60-63")
            self.kind_form.addRow("KP ranges", self.ranges_edit)

    def result(self) -> Dict:
        kind = self.rule.get("kind")
        config: Dict = {}
        if kind == schema.RULE_KIND_THRESHOLD:
            config = {
                "profile": self.profile_combo.currentText(),
                "op": self.op_combo.currentText(),
                "value": self.value_spin.value(),
                "value2": self.value2_spin.value() if self.op_combo.currentText() == "between" else None,
                "abs": False,
            }
        elif kind == schema.RULE_KIND_PROXIMITY:
            layer = self.layer_combo.currentLayer()
            config = {
                "layer_id": layer.id() if layer else "",
                "layer_source": layer.source() if layer else "",
                "distance_m": self.dist_spin.value(),
                "mode": self.mode_combo.currentText(),
                "filter_expression": self.filter_edit.text().strip(),
            }
        elif kind == schema.RULE_KIND_POLYGON:
            layer = self.layer_combo.currentLayer()
            config = {
                "layer_id": layer.id() if layer else "",
                "layer_source": layer.source() if layer else "",
                "attribute": self.field_combo.currentField(),
                "match_values": [v.strip() for v in self.values_edit.text().split(",") if v.strip()],
            }
        elif kind == schema.RULE_KIND_KP_TABLE:
            layer = self.layer_combo.currentLayer()
            config = {
                "layer_id": layer.id() if layer else "",
                "layer_source": layer.source() if layer else "",
                "start_field": self.start_field.currentField(),
                "end_field": self.end_field.currentField(),
                "filter_expression": self.filter_edit.text().strip(),
            }
        elif kind == schema.RULE_KIND_MANUAL:
            config = {"ranges": parse_scope(self.ranges_edit.text())}

        scope = parse_scope(self.scope_edit.text())
        if scope:
            config["scope_ranges"] = scope

        methods = [m for m, cb in self.method_checks.items() if cb.isChecked()]
        self.rule.update({
            "name": self.name_edit.text().strip() or schema_kind_label(kind),
            "action": self.action_combo.currentText(),
            "risk_level": self.risk_spin.value() if self.action_combo.currentText() == schema.RULE_ACTION_RISK else 0,
            "methods_json": json.dumps(methods),
            "config_json": json.dumps(config),
        })
        return self.rule


# ---------------------------------------------------------------------------
# Main panel
# ---------------------------------------------------------------------------


class AssessmentPanel(QWidget):
    assessments_changed = pyqtSignal()

    def __init__(self, iface, embedded: bool = True, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.store = None
        self.rpl_id: Optional[str] = None
        self.assessment: Optional[Dict] = None
        self.rule_set: Optional[Dict] = None
        self.rules: List[Dict] = []
        self.methods: List[str] = list(schema.DEFAULT_ASSESSMENT_METHODS)
        self.result = None
        self.sampler = None
        self._loading = False

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self._run)

        layout = QVBoxLayout(self)

        # header ------------------------------------------------------------
        header = QHBoxLayout()
        self.name_label = QLabel("—")
        self.name_label.setStyleSheet("font-weight: bold;")
        header.addWidget(self.name_label)
        self.status_chip = QLabel("not run")
        self.status_chip.setStyleSheet("padding: 2px 6px; border: 1px solid #aaa; color: #555;")
        header.addWidget(self.status_chip)
        self.rpl_status_label = QLabel("RPL is issued")
        self.rpl_status_label.setStyleSheet("color: #8a5a00;")
        self.rpl_status_label.setVisible(False)
        header.addWidget(self.rpl_status_label)
        header.addWidget(QLabel("Rule set:"))
        self.ruleset_combo = QComboBox()
        self.ruleset_combo.setMinimumWidth(160)
        self.ruleset_combo.currentIndexChanged.connect(self._on_ruleset_changed)
        header.addWidget(self.ruleset_combo)
        header.addWidget(QLabel("Step (m):"))
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(1.0, 5000.0)
        self.step_spin.setValue(50.0)
        header.addWidget(self.step_spin)
        header.addStretch(1)
        self.run_btn = QPushButton("Run assessment")
        self.run_btn.clicked.connect(self._run)
        header.addWidget(self.run_btn)
        layout.addLayout(header)

        # method selector ---------------------------------------------------
        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("Method:"))
        self.method_combo = QComboBox()
        self.method_combo.currentTextChanged.connect(lambda _t: self._refresh_overview())
        method_row.addWidget(self.method_combo)
        self.summary_label = QLabel("")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        method_row.addWidget(self.summary_label)
        method_row.addStretch(1)
        layout.addLayout(method_row)

        # overview bar (aligned to fire column) -----------------------------
        self.overview_holder = QWidget()
        oh = QHBoxLayout(self.overview_holder)
        oh.setContentsMargins(0, 0, 0, 0)
        self.overview = VerdictStrip()
        self.overview.kpClicked.connect(self._emit_kp)
        oh.addWidget(self.overview)
        layout.addWidget(self.overview_holder)

        # rule toolbar ------------------------------------------------------
        toolbar = QHBoxLayout()
        add_btn = QToolButton()
        add_btn.setText("＋ Add rule ▾")
        add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        add_menu = QMenu(add_btn)
        add_menu.addAction("Depth / slope threshold", lambda: self._add_rule(schema.RULE_KIND_THRESHOLD))
        add_menu.addAction("Proximity to features", lambda: self._add_rule(schema.RULE_KIND_PROXIMITY))
        add_menu.addAction("Polygon attribute (soils)", lambda: self._add_rule(schema.RULE_KIND_POLYGON))
        add_menu.addAction("KP-range table", lambda: self._add_rule(schema.RULE_KIND_KP_TABLE))
        add_menu.addAction("Manual ranges", lambda: self._add_rule(schema.RULE_KIND_MANUAL))
        add_btn.setMenu(add_menu)
        toolbar.addWidget(add_btn)
        for label, slot in (("Edit", self._edit_rule), ("Delete", self._delete_rule),
                            ("↑", lambda: self._move_rule(-1)), ("↓", lambda: self._move_rule(1))):
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            toolbar.addWidget(btn)
        toolbar.addStretch(1)
        self.export_btn = QPushButton("Export CSV…")
        self.export_btn.clicked.connect(self._export_csv)
        toolbar.addWidget(self.export_btn)
        layout.addLayout(toolbar)

        # rule stack table --------------------------------------------------
        self.rule_table = QTableWidget(0, 4)
        self.rule_table.setHorizontalHeaderLabels(["On", "Rule", "Fires", "Coverage"])
        self.rule_table.verticalHeader().setVisible(False)
        self.rule_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rule_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.rule_table.setItemDelegateForColumn(FIRE_COL, FireBarDelegate(self.rule_table))
        self.rule_table.setColumnWidth(0, 34)
        self.rule_table.setColumnWidth(1, 260)
        self.rule_table.setColumnWidth(3, 110)
        hheader = self.rule_table.horizontalHeader()
        hheader.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hheader.setSectionResizeMode(FIRE_COL, QHeaderView.ResizeMode.Stretch)
        hheader.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hheader.sectionResized.connect(lambda *a: self._align_overview())
        self.rule_table.itemChanged.connect(self._on_item_changed)
        self.rule_table.itemDoubleClicked.connect(lambda _i: self._edit_rule())
        layout.addWidget(self.rule_table, 2)

        # results table -----------------------------------------------------
        self.results_box = QGroupBox("Result ranges (selected method)")
        self.results_box.setCheckable(True)
        self.results_box.setChecked(False)
        rlayout = QVBoxLayout(self.results_box)
        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(
            ["Start KP", "End KP", "Status", "Risk", "Deciding rule"])
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.results_table.cellClicked.connect(self._on_result_clicked)
        rlayout.addWidget(self.results_table)
        layout.addWidget(self.results_box, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # ------------------------------------------------------------- loading --
    def set_store(self, store):
        self.store = store

    def new_assessment(self, store, rpl_id: str):
        self.store = store
        self.rpl_id = rpl_id
        rpl = store.get_rpl(rpl_id) or {}
        existing = store.list_assessments(rpl_id)
        name = f"Assessment {len(existing) + 1}"
        rule_sets = store.list_rule_sets()
        rule_set_id = rule_sets[0]["rule_set_id"] if rule_sets else store.seed_default_rule_set()
        self.assessment = {
            "assessment_id": schema.new_id(),
            "rpl_id": rpl_id,
            "rule_set_id": rule_set_id,
            "name": name,
            "sample_step_m": 50.0,
            "min_range_km": 0.0,
            "status": "",
        }
        store.save_assessment(self.assessment)
        self._load(rpl.get("name") or rpl_id)
        self.assessments_changed.emit()

    def load_assessment(self, store, assessment_row: Dict):
        self.store = store
        self.assessment = dict(assessment_row)
        self.rpl_id = assessment_row.get("rpl_id")
        rpl = store.get_rpl(self.rpl_id) or {}
        self._load(rpl.get("name") or self.rpl_id)
        if self.assessment.get("status") in ("current", "stale"):
            self._load_stored_ranges()

    def _load(self, rpl_name: str):
        self._loading = True
        self.name_label.setText(f"{self.assessment.get('name')}  ·  RPL: {rpl_name}")
        rpl = self.store.get_rpl(self.rpl_id) if self.store and self.rpl_id else None
        self.rpl_status_label.setVisible(bool(rpl and rpl.get("status") == schema.STATUS_ISSUED))
        self.step_spin.setValue(float(self.assessment.get("sample_step_m") or 50.0))
        self._refresh_status_chip()
        self._reload_rulesets_combo()
        self._loading = False
        self._load_rule_set(self.assessment.get("rule_set_id"))

    def _refresh_status_chip(self):
        status = (self.assessment or {}).get("status") or ""
        run_utc = (self.assessment or {}).get("run_utc") or ""
        if status == "current":
            text = f"current - run {run_utc}" if run_utc else "current"
            style = "background: #e8f5e9; color: #1b5e20; border: 1px solid #78a878;"
            self.run_btn.setStyleSheet("")
        elif status == "stale":
            text = f"stale - RPL changed since {run_utc}" if run_utc else "stale"
            style = "background: #fff3cd; color: #7a4f00; border: 1px solid #e0b84b;"
            self.run_btn.setStyleSheet("background: #fff3cd;")
        else:
            text = "not run"
            style = "background: #f0f0f0; color: #555; border: 1px solid #aaa;"
            self.run_btn.setStyleSheet("")
        self.status_chip.setText(text)
        self.status_chip.setStyleSheet(f"padding: 2px 6px; {style}")

    def _reload_rulesets_combo(self):
        self.ruleset_combo.blockSignals(True)
        self.ruleset_combo.clear()
        for rs in (self.store.list_rule_sets() if self.store else []):
            self.ruleset_combo.addItem(rs.get("name") or "?", rs.get("rule_set_id"))
        self.ruleset_combo.addItem("New from template…", "__new__")
        # select current
        rid = self.assessment.get("rule_set_id") if self.assessment else None
        idx = self.ruleset_combo.findData(rid)
        if idx >= 0:
            self.ruleset_combo.setCurrentIndex(idx)
        self.ruleset_combo.blockSignals(True)
        self.ruleset_combo.blockSignals(False)

    def _on_ruleset_changed(self, _idx):
        if self._loading or not self.store or not self.assessment:
            return
        data = self.ruleset_combo.currentData()
        if data == "__new__":
            data = self.store.seed_default_rule_set()
            self.assessment["rule_set_id"] = data
            self.store.save_assessment(self.assessment)
            self._reload_rulesets_combo()
        else:
            self.assessment["rule_set_id"] = data
            self.store.save_assessment(self.assessment)
        self._load_rule_set(data)

    def _load_rule_set(self, rule_set_id: Optional[str]):
        if not self.store or not rule_set_id:
            return
        self.rule_set = self.store.get_rule_set(rule_set_id)
        self.rules = self.store.list_rules(rule_set_id)
        self.methods = _load_json(self.rule_set.get("methods_json") if self.rule_set else None,
                                  list(schema.DEFAULT_ASSESSMENT_METHODS))
        self.method_combo.blockSignals(True)
        self.method_combo.clear()
        self.method_combo.addItems(self.methods)
        self.method_combo.blockSignals(False)
        self._rebuild_rule_table()

    # -------------------------------------------------------- rule table --
    def _rebuild_rule_table(self):
        self._loading = True
        self.rule_table.setRowCount(len(self.rules))
        domain_km = self._domain_km()
        for row, rule in enumerate(self.rules):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            on.setCheckState(Qt.CheckState.Checked if int(rule.get("enabled") or 0) else Qt.CheckState.Unchecked)
            self.rule_table.setItem(row, 0, on)

            full_summary = rule_summary(rule)
            summary = QTableWidgetItem(
                f"{rule.get('name') or schema_kind_label(rule.get('kind'))}: {rule_condition(rule)}"
            )
            summary.setToolTip(full_summary)
            summary.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.rule_table.setItem(row, 1, summary)

            fire = QTableWidgetItem()
            fire.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            intervals = []
            if self.result is not None:
                intervals = [(iv.start_km, iv.end_km) for iv in self.result.rule_hits.get(rule.get("rule_id"), [])]
            fire.setData(Qt.ItemDataRole.UserRole,
                         (domain_km, intervals, ACTION_COLORS.get(rule.get("action"), QColor("#888"))))
            self.rule_table.setItem(row, 2, fire)

            cov_text = ""
            if self.result is not None:
                stat = next((s for s in self.result.rule_stats if s.rule_id == rule.get("rule_id")), None)
                if stat is not None:
                    cov_text = f"{stat.coverage_km:.2f} km · {stat.coverage_pct:.0f}%"
            cov = QTableWidgetItem(cov_text)
            cov.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.rule_table.setItem(row, 3, cov)
        self._loading = False
        self._align_overview()

    def _on_item_changed(self, item):
        if self._loading or item.column() != 0:
            return
        row = item.row()
        if 0 <= row < len(self.rules):
            self.rules[row]["enabled"] = 1 if item.checkState() == Qt.CheckState.Checked else 0
            self._save_rules()
            self._maybe_rerun()

    def _selected_row(self) -> int:
        rows = self.rule_table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _add_rule(self, kind: str):
        if not self._ensure_ready():
            return
        rule = {
            "rule_id": schema.new_id(),
            "name": schema_kind_label(kind),
            "enabled": 1,
            "kind": kind,
            "action": schema.RULE_ACTION_EXCLUDE,
            "risk_level": 0,
            "methods_json": json.dumps(list(self.methods)),
            "config_json": "{}",
            "notes": "",
        }
        dlg = RuleEditorDialog(rule, self.methods, self)
        if qt_exec(dlg) == DIALOG_ACCEPTED:
            self.rules.append(dlg.result())
            self._save_rules()
            self._rebuild_rule_table()
            self._maybe_rerun()

    def _edit_rule(self):
        row = self._selected_row()
        if not (0 <= row < len(self.rules)):
            return
        dlg = RuleEditorDialog(self.rules[row], self.methods, self)
        if qt_exec(dlg) == DIALOG_ACCEPTED:
            self.rules[row] = dlg.result()
            self._save_rules()
            self._rebuild_rule_table()
            self._maybe_rerun()

    def _delete_rule(self):
        row = self._selected_row()
        if not (0 <= row < len(self.rules)):
            return
        del self.rules[row]
        self._save_rules()
        self._rebuild_rule_table()
        self._maybe_rerun()

    def _move_rule(self, delta: int):
        row = self._selected_row()
        new = row + delta
        if not (0 <= row < len(self.rules) and 0 <= new < len(self.rules)):
            return
        self.rules[row], self.rules[new] = self.rules[new], self.rules[row]
        self._save_rules()
        self._rebuild_rule_table()
        self.rule_table.selectRow(new)
        self._maybe_rerun()

    def _save_rules(self):
        if not self.store or not self.rule_set:
            return
        self.store.save_rule_set(self.rule_set, self.rules)
        self.rules = self.store.list_rules(self.rule_set["rule_set_id"])

    def _ensure_ready(self) -> bool:
        if not self.store or not self.assessment:
            QMessageBox.information(self, "Assessment", "Select or create an assessment first.")
            return False
        return True

    # --------------------------------------------------------------- run --
    def _maybe_rerun(self):
        if self.result is not None:
            self._debounce.start()

    def _run(self):
        if not self._ensure_ready() or not self.rpl_id:
            return
        self.assessment["sample_step_m"] = self.step_spin.value()
        self.store.save_assessment(self.assessment)
        progress = QProgressDialog("Running assessment…", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        def _tick(msg):
            progress.setLabelText(msg)
            from qgis.PyQt.QtWidgets import QApplication
            QApplication.processEvents()

        try:
            result, sampler = rules_inputs.run_assessment(
                self.store, self.rpl_id, self.assessment["rule_set_id"],
                sample_step_m=self.step_spin.value(),
                min_range_km=float(self.assessment.get("min_range_km") or 0.0),
                project=QgsProject.instance(), progress=_tick,
            )
        except Exception as exc:
            progress.close()
            QMessageBox.critical(self, "Assessment failed", str(exc))
            return
        self.result = result
        self.sampler = sampler
        rule_names = {r["rule_id"]: r.get("name") or r["rule_id"] for r in self.rules}
        try:
            layer_name = assessment_output.write_assessment_ranges(
                self.store, self.assessment, result, sampler.route, rule_names)
            self.assessment = self.store.get_assessment(self.assessment["assessment_id"]) or self.assessment
            self._refresh_status_chip()
            self._load_output_layer(layer_name)
        except Exception as exc:
            self.status_label.setText(f"Ranges computed but layer write failed: {exc}")
        progress.close()

        self._rebuild_rule_table()
        self._refresh_overview()
        self._refresh_results()
        warn = f"  ⚠ {len(result.warnings)} warning(s)" if result.warnings else ""
        self.status_label.setText(
            "  ·  ".join(result.warnings) if result.warnings
            else f"Assessment current — {self._domain_km():.2f} km route.{warn}")
        self.assessments_changed.emit()

    # ----------------------------------------------------------- display --
    def _domain_km(self) -> float:
        if self.sampler is not None:
            return self.sampler.total_km
        if self.result is not None and self.result.domain is not None:
            return self.result.domain.length_km
        return 0.0

    def _current_method(self) -> str:
        return self.method_combo.currentText() or (self.methods[0] if self.methods else "")

    def _refresh_overview(self):
        method = self._current_method()
        domain_km = self._domain_km()
        spans = []
        if self.result is not None and method in self.result.per_method:
            for v in self.result.per_method[method]:
                spans.append((v.start_km, v.end_km, STATUS_COLORS.get(v.status, QColor("#888"))))
            from .rules_engine import summarise
            totals = summarise(self.result.per_method[method])
            total = max(domain_km, 1e-9)
            self.summary_label.setText(
                _legend_chip("#d62728", "Excluded", totals["excluded"], total)
                + "  "
                + _legend_chip("#ff8c00", "Risk", totals["risk"], total)
                + "  "
                + _legend_chip("#2ca02c", "Allowed", totals["allowed"], total)
            )
        else:
            self.summary_label.setText("")
        self.overview.set_spans(domain_km, spans, method)

    def _refresh_results(self):
        method = self._current_method()
        self._loading = True
        verdicts = self.result.per_method.get(method, []) if self.result else []
        self.results_table.setRowCount(len(verdicts))
        rule_names = {r["rule_id"]: r.get("name") or "" for r in self.rules}
        for row, v in enumerate(verdicts):
            for col, text in enumerate([
                f"{v.start_km:.3f}", f"{v.end_km:.3f}", v.status,
                str(v.risk_level), rule_names.get(v.dominant_rule_id or "", ""),
            ]):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if col == 2:
                    item.setForeground(QBrush(STATUS_COLORS.get(v.status, QColor("#000"))))
                self.results_table.setItem(row, col, item)
        self._loading = False

    def _align_overview(self):
        """Left-pad the overview bar so it starts under the Fires column."""
        x = self.rule_table.columnViewportPosition(FIRE_COL)
        x += self.rule_table.verticalHeader().width() + self.rule_table.frameWidth()
        # right padding: everything right of the fire column (none — it is last)
        self.overview_holder.layout().setContentsMargins(max(0, x), 0, self.rule_table.frameWidth(), 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._align_overview()

    # -------------------------------------------------------- selection --
    def _emit_kp(self, kp_km: float):
        if self.rpl_id:
            selection_bus().kpSelected.emit(self.rpl_id, kp_km)

    def _on_result_clicked(self, row, _col):
        method = self._current_method()
        verdicts = self.result.per_method.get(method, []) if self.result else []
        if 0 <= row < len(verdicts):
            v = verdicts[row]
            self._emit_kp(0.5 * (v.start_km + v.end_km))
            self._select_output_feature(v, method)

    # ---------------------------------------------------- output layer --
    def _load_output_layer(self, layer_name: str):
        if not layer_name:
            return
        layer = self._find_or_load_layer(layer_name)
        if layer is not None:
            assessment_output.apply_assessment_style(layer, self.methods)
            layer.dataProvider().forceReload()
            layer.triggerRepaint()

    def _load_stored_ranges(self):
        """Rebuild the results/overview from persisted ranges (no re-run)."""
        rows = self.store.list_assessment_ranges(self.assessment["assessment_id"])
        if not rows:
            return
        from .rules_engine import AssessmentResult, RangeVerdict
        result = AssessmentResult(per_method={}, domain=None)
        for r in rows:
            method = r.get("method")
            result.per_method.setdefault(method, []).append(RangeVerdict(
                float(r.get("start_kp") or 0.0), float(r.get("end_kp") or 0.0),
                r.get("status") or "", int(r.get("risk_level") or 0),
                _load_json(r.get("fired_rules_json"), []), r.get("dominant_rule_id") or None))
        # domain from the widest range
        from .rules_engine import Interval as _Iv
        max_kp = max((float(r.get("end_kp") or 0.0) for r in rows), default=0.0)
        result.domain = _Iv(0.0, max_kp)
        self.result = result
        self._refresh_overview()
        self._refresh_results()
        self._refresh_status_chip()

    def _select_output_feature(self, verdict, method: str):
        layer = self._output_layer()
        if layer is None:
            return
        expr = (f"\"method\" = '{method}' AND abs(\"start_kp\" - {verdict.start_km}) < 0.001")
        layer.selectByExpression(expr)

    def _output_layer(self) -> Optional[QgsVectorLayer]:
        name = self.assessment.get("ranges_layer") if self.assessment else None
        return self._find_or_load_layer(name) if name else None

    def _find_or_load_layer(self, layer_name: Optional[str]) -> Optional[QgsVectorLayer]:
        if not layer_name or not self.store:
            return None
        uri_fragment = f"|layername={layer_name}"
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and layer.source().endswith(uri_fragment) \
                    and self.store.gpkg_path in layer.source():
                return layer
        layer = self.store.open_layer(layer_name)
        if layer is not None:
            project = QgsProject.instance()
            root = project.layerTreeRoot()
            group = root.findGroup("Cable Route Workbench") or root.insertGroup(0, "Cable Route Workbench")
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        return layer

    # -------------------------------------------------------------- export --
    def _export_csv(self):
        if self.result is None:
            QMessageBox.information(self, "Export", "Run the assessment first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export assessment ranges", "", "CSV (*.csv)")
        if not path:
            return
        import csv
        rule_names = {r["rule_id"]: r.get("name") or "" for r in self.rules}
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["method", "start_kp", "end_kp", "status", "risk_level", "deciding_rule"])
            for method, verdicts in self.result.per_method.items():
                for v in verdicts:
                    writer.writerow([method, f"{v.start_km:.3f}", f"{v.end_km:.3f}", v.status,
                                     v.risk_level, rule_names.get(v.dominant_rule_id or "", "")])
        self.status_label.setText(f"Exported ranges to {path}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _load_json(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def _preselect_layer(combo: "QgsMapLayerComboBox", layer_id: Optional[str]):
    if not layer_id:
        return
    layer = QgsProject.instance().mapLayer(layer_id)
    if layer is not None:
        combo.setLayer(layer)


_KIND_LABELS = {
    schema.RULE_KIND_THRESHOLD: "Threshold",
    schema.RULE_KIND_PROXIMITY: "Proximity",
    schema.RULE_KIND_POLYGON: "Polygon attribute",
    schema.RULE_KIND_KP_TABLE: "KP-range table",
    schema.RULE_KIND_MANUAL: "Manual ranges",
}


def schema_kind_label(kind: Optional[str]) -> str:
    return _KIND_LABELS.get(kind or "", "Rule")


def _legend_chip(color: str, label: str, km: float, total: float) -> str:
    pct = 100.0 * km / max(total, 1e-9)
    return (
        f"<span style='color:{color}; font-weight:bold;'>&#9679;</span> "
        f"{label} {km:.1f} km ({pct:.0f}%)"
    )


def rule_condition(rule: Dict) -> str:
    kind = rule.get("kind")
    config = _load_json(rule.get("config_json"), {})
    if kind == schema.RULE_KIND_THRESHOLD:
        op = config.get("op", ">")
        if op == "between":
            cond = f"{config.get('profile','depth')} in {config.get('value')}–{config.get('value2')}"
        else:
            cond = f"{config.get('profile','depth')} {op} {config.get('value')}"
    elif kind == schema.RULE_KIND_PROXIMITY:
        cond = f"within {config.get('distance_m')} m of {_layer_name(config.get('layer_id'))}"
    elif kind == schema.RULE_KIND_POLYGON:
        cond = f"{config.get('attribute')} ∈ {config.get('match_values')}"
    elif kind == schema.RULE_KIND_KP_TABLE:
        cond = f"KP ranges from {_layer_name(config.get('layer_id'))}"
    elif kind == schema.RULE_KIND_MANUAL:
        cond = f"{len(config.get('ranges', []))} manual range(s)"
    else:
        cond = "?"
    return cond


def rule_summary(rule: Dict) -> str:
    config = _load_json(rule.get("config_json"), {})
    cond = rule_condition(rule)
    action = rule.get("action")
    if action == schema.RULE_ACTION_RISK:
        action = f"risk {rule.get('risk_level')}"
    methods = _load_json(rule.get("methods_json"), [])
    scope = config.get("scope_ranges")
    scope_txt = f"  [KP {format_scope(scope)}]" if scope else ""
    return f"{rule.get('name')}: {cond} → {action} ({', '.join(methods)}){scope_txt}"


def _layer_name(layer_id: Optional[str]) -> str:
    if not layer_id:
        return "?"
    layer = QgsProject.instance().mapLayer(layer_id)
    return layer.name() if layer is not None else "(missing layer)"
