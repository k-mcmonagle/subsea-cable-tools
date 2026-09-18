# -*- coding: utf-8 -*-
"""Attribute-condition editing widgets shared by the rule and check editors.

- :class:`FieldCombo` — an editable combo listing the fields of the
  selected input's layer (free text still works when the layer is not
  loaded, so a stored attribute name never disappears).
- :class:`ExpressionEdit` — a line edit with a "…" button that opens the
  QGIS expression builder against the input's layer.
- :class:`AttributeRulesTable` — one structured row per condition: value
  equals / number in range (with ≥ or > and ≤ or < bounds, so bins are
  unambiguous) / QGIS expression, plus an optional risk-level column.

All of it builds under QGIS 3 and 4 (``qgis.gui`` expression builder,
scoped Qt enums via ``qgis_compat``).
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from qgis.core import QgsExpression, QgsProject, QgsVectorLayer
from qgis.gui import QgsExpressionBuilderDialog
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    DIALOG_ACCEPTED,
    HEADER_RESIZE_MODE_CONTENTS,
    HEADER_RESIZE_MODE_STRETCH,
    qt_exec,
)
from .. import attribute_rules, map_layers, schema

_ITEM_FLAG = getattr(Qt, "ItemFlag", Qt)
_FLAGS_EDITABLE = (_ITEM_FLAG.ItemIsEnabled | _ITEM_FLAG.ItemIsSelectable
                   | _ITEM_FLAG.ItemIsEditable)
_FLAGS_READONLY = _ITEM_FLAG.ItemIsSelectable


def resolve_input_layer(inputs: List[Dict], input_id: str,
                        project: Optional[QgsProject] = None):
    """The live layer behind a registered input id (None when unknown)."""
    if not input_id:
        return None
    for row in inputs or []:
        if str(row.get("input_id") or "") == str(input_id):
            try:
                return map_layers.resolve_input_layer(
                    project or QgsProject.instance(), row)
            except Exception:
                return None
    return None


def layer_field_names(layer) -> List[str]:
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return []
    try:
        return [field.name() for field in layer.fields()]
    except Exception:
        return []


class FieldCombo(QComboBox):
    """Editable field picker: lists the layer's fields, accepts free text."""

    def __init__(self, text: str = "", placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert
                             if hasattr(QComboBox, "InsertPolicy")
                             else QComboBox.NoInsert)
        if placeholder:
            self.lineEdit().setPlaceholderText(placeholder)
        self._fields: List[str] = []
        self.setText(text)

    def text(self) -> str:
        return self.currentText().strip()

    def setText(self, text: str) -> None:
        text = (text or "").strip()
        index = self.findText(text)
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            self.setCurrentIndex(-1)
            self.setEditText(text)

    def setPlaceholderText(self, text: str) -> None:
        self.lineEdit().setPlaceholderText(text)

    def set_layer(self, layer) -> None:
        names = layer_field_names(layer)
        if names == self._fields:
            return
        current = self.text()
        self._fields = names
        self.blockSignals(True)
        try:
            self.clear()
            self.addItems(names)
            self.setText(current)
        finally:
            self.blockSignals(False)


class ExpressionEdit(QWidget):
    """QGIS expression line edit with a builder button."""

    textChanged = pyqtSignal(str)

    def __init__(self, text: str = "", placeholder: str = "", parent=None):
        super().__init__(parent)
        self._layer = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.edit = QLineEdit(text or "")
        self.edit.setPlaceholderText(placeholder or "optional QGIS expression")
        self.edit.textChanged.connect(self.textChanged)
        layout.addWidget(self.edit, 1)
        self.button = QToolButton()
        self.button.setText("…")
        self.button.setToolTip("Open the QGIS expression builder (lists the "
                               "layer's fields and functions).")
        self.button.clicked.connect(self._open_builder)
        layout.addWidget(self.button)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, text: str) -> None:
        self.edit.setText(text or "")

    def setPlaceholderText(self, text: str) -> None:
        self.edit.setPlaceholderText(text)

    def setToolTip(self, text: str) -> None:  # type: ignore[override]
        self.edit.setToolTip(text)

    def set_layer(self, layer) -> None:
        self._layer = layer if isinstance(layer, QgsVectorLayer) else None

    def _open_builder(self) -> None:
        result = open_expression_builder(self._layer, self.text(), self)
        if result is not None:
            self.edit.setText(result)


def open_expression_builder(layer, text: str, parent=None) -> Optional[str]:
    """Run the QGIS expression builder; the accepted expression or None."""
    try:
        dialog = QgsExpressionBuilderDialog(layer, text or "", parent)
    except TypeError:
        dialog = QgsExpressionBuilderDialog(layer, text or "")
    dialog.setWindowTitle("Expression")
    if qt_exec(dialog) != DIALOG_ACCEPTED:
        return None
    return dialog.expressionText().strip()


def expression_problem(text: str) -> Optional[str]:
    """Parser error text for a QGIS expression, None when it parses."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        expression = QgsExpression(text)
        if expression.hasParserError():
            return expression.parserErrorString().strip() or "does not parse"
    except Exception as exc:  # pragma: no cover — defensive
        return str(exc)
    return None


class AttributeRulesTable(QWidget):
    """Structured condition rows (see ``burial.attribute_rules``).

    ``with_kind`` adds the Value / Range / Expression column (risk checks);
    without it every row is a numeric range (exclusion value ranges).
    ``with_risk`` adds the risk-level column.
    """

    _LOWER_BOUNDS = (("≥", True), (">", False))
    _UPPER_BOUNDS = (("<", False), ("≤", True))

    def __init__(self, with_kind: bool = True, with_risk: bool = True,
                 parent=None):
        super().__init__(parent)
        self.with_kind = with_kind
        self.with_risk = with_risk
        self._layer = None
        self._attribute_name: Callable[[], str] = lambda: ""

        columns: List[str] = []
        if with_kind:
            columns.append("Condition")
        columns += ["Value / From" if with_kind else "From", "",
                    "To", ""]
        if with_risk:
            columns.append("Risk")
        self.col_kind = 0 if with_kind else -1
        self.col_value = 1 if with_kind else 0
        self.col_lower = self.col_value + 1
        self.col_to = self.col_value + 2
        self.col_upper = self.col_value + 3
        self.col_risk = self.col_value + 4 if with_risk else -1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.table = QTableWidget(0, len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setMinimumHeight(96)
        self.table.setMaximumHeight(170)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(HEADER_RESIZE_MODE_CONTENTS)
        header.setSectionResizeMode(self.col_value, HEADER_RESIZE_MODE_STRETCH)
        header.setSectionResizeMode(self.col_to, HEADER_RESIZE_MODE_STRETCH)
        self.table.setToolTip(
            "Each row is one condition on the attribute; the first row that "
            "matches wins.\n"
            "• Value equals: exact (case-insensitive) match, e.g. ROCK.\n"
            "• Number in range: From / To with ≥ or > and < or "
            "≤ bounds — leave a side blank for open-ended. Use "
            "≥ a … < b for adjoining bins so a boundary value "
            "lands in exactly one row.\n"
            "• QGIS expression: any expression over the feature, e.g. "
            "\"Height_m\" > 2 AND \"Class\" = 'ROCK'."
            if with_kind else
            "Each row is one numeric range on the attribute (From / To with "
            "≥ or > and < or ≤ bounds; leave a side blank for "
            "open-ended). Use ≥ a … < b for adjoining bins so a "
            "boundary value lands in exactly one row.")
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.add_button = QPushButton("＋ Add " + ("rule" if with_kind
                                                        else "range"))
        self.add_button.clicked.connect(lambda: self.add_row({}))
        buttons.addWidget(self.add_button)
        self.remove_button = QPushButton("− Remove")
        self.remove_button.clicked.connect(self.remove_current_row)
        buttons.addWidget(self.remove_button)
        if with_kind:
            self.build_button = QPushButton("Build expression…")
            self.build_button.setToolTip(
                "Open the QGIS expression builder for the selected row "
                "(turns it into an expression rule).")
            self.build_button.clicked.connect(self._build_expression)
            buttons.addWidget(self.build_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    # -- layer / attribute context ------------------------------------------
    def set_layer(self, layer) -> None:
        self._layer = layer if isinstance(layer, QgsVectorLayer) else None

    def set_attribute_name_provider(self, provider: Callable[[], str]) -> None:
        self._attribute_name = provider

    # -- rows -----------------------------------------------------------------
    def set_rules(self, rules: List[Dict]) -> None:
        self.table.setRowCount(0)
        for rule in rules or []:
            if isinstance(rule, dict):
                self.add_row(rule)

    def add_row(self, rule: Dict) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        kind = attribute_rules.rule_kind(rule) or (
            attribute_rules.KIND_RANGE if not self.with_kind
            else attribute_rules.KIND_VALUE)
        if not self.with_kind:
            kind = attribute_rules.KIND_RANGE
        if self.with_kind:
            kind_combo = QComboBox()
            for value in attribute_rules.KINDS:
                kind_combo.addItem(attribute_rules.KIND_LABELS[value], value)
            kind_combo.setCurrentIndex(max(0, kind_combo.findData(kind)))
            kind_combo.currentIndexChanged.connect(self._sync_rows)
            self.table.setCellWidget(row, self.col_kind, kind_combo)

        if kind == attribute_rules.KIND_VALUE:
            value_text = str(rule.get("match") or "")
        elif kind == attribute_rules.KIND_EXPRESSION:
            value_text = str(rule.get("expression") or "")
        else:
            value_text = _num_text(rule.get("min"))
        self.table.setItem(row, self.col_value, QTableWidgetItem(value_text))
        self.table.setItem(row, self.col_to, QTableWidgetItem(
            _num_text(rule.get("max")) if kind == attribute_rules.KIND_RANGE
            else ""))

        lower = QComboBox()
        for label, inclusive in self._LOWER_BOUNDS:
            lower.addItem(label, inclusive)
        lower.setToolTip("Lower bound: ≥ includes the From value, "
                         "> excludes it.")
        lower.setCurrentIndex(max(0, lower.findData(
            bool(rule.get("min_inclusive", True)))))
        self.table.setCellWidget(row, self.col_lower, lower)
        upper = QComboBox()
        for label, inclusive in self._UPPER_BOUNDS:
            upper.addItem(label, inclusive)
        upper.setToolTip("Upper bound: < excludes the To value, "
                         "≤ includes it.")
        # New rows default to an exclusive upper bound ([a, b) bins); stored
        # rules without flags are inclusive (the pre-flag meaning).
        upper_inclusive = bool(rule.get("max_inclusive", True)) if rule \
            else False
        upper.setCurrentIndex(max(0, upper.findData(upper_inclusive)))
        self.table.setCellWidget(row, self.col_upper, upper)

        if self.with_risk:
            risk_combo = QComboBox()
            for level in schema.RISK_LEVELS:
                risk_combo.addItem(schema.RISK_LABELS[level], level)
            risk_combo.setCurrentIndex(max(0, risk_combo.findData(
                rule.get("risk") or schema.RISK_LOW)))
            self.table.setCellWidget(row, self.col_risk, risk_combo)
        self._sync_row(row)
        self.table.setCurrentCell(row, self.col_value)
        return row

    def remove_current_row(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            row = self.table.rowCount() - 1
        if row >= 0:
            self.table.removeRow(row)

    def row_count(self) -> int:
        return self.table.rowCount()

    def _row_kind(self, row: int) -> str:
        if not self.with_kind:
            return attribute_rules.KIND_RANGE
        combo = self.table.cellWidget(row, self.col_kind)
        return (combo.currentData() if combo is not None else "") \
            or attribute_rules.KIND_VALUE

    def _sync_rows(self, *_args) -> None:
        for row in range(self.table.rowCount()):
            self._sync_row(row)

    def _sync_row(self, row: int) -> None:
        kind = self._row_kind(row)
        is_range = kind == attribute_rules.KIND_RANGE
        for column in (self.col_lower, self.col_upper):
            widget = self.table.cellWidget(row, column)
            if widget is not None:
                widget.setEnabled(is_range)
        to_item = self.table.item(row, self.col_to)
        if to_item is not None:
            to_item.setFlags(_FLAGS_EDITABLE if is_range else _FLAGS_READONLY)
            if not is_range:
                to_item.setText("")
        value_item = self.table.item(row, self.col_value)
        if value_item is not None:
            value_item.setToolTip({
                attribute_rules.KIND_VALUE: "Exact value to match",
                attribute_rules.KIND_RANGE: "Lower bound (blank = open)",
                attribute_rules.KIND_EXPRESSION: "QGIS expression",
            }.get(kind, ""))

    def _cell_text(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return (item.text() if item is not None else "").strip()

    def _build_expression(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            row = self.add_row({"expression": ""})
        combo = self.table.cellWidget(row, self.col_kind)
        current = self._cell_text(row, self.col_value) \
            if self._row_kind(row) == attribute_rules.KIND_EXPRESSION else ""
        result = open_expression_builder(self._layer, current, self)
        if result is None:
            return
        if combo is not None:
            combo.setCurrentIndex(max(0, combo.findData(
                attribute_rules.KIND_EXPRESSION)))
        item = self.table.item(row, self.col_value)
        if item is None:
            item = QTableWidgetItem("")
            self.table.setItem(row, self.col_value, item)
        item.setText(result)
        self._sync_row(row)

    # -- result ---------------------------------------------------------------
    def _row_rule(self, row: int) -> Optional[Dict]:
        """The row as a rule dict; None for a completely blank row."""
        kind = self._row_kind(row)
        value_text = self._cell_text(row, self.col_value)
        to_text = self._cell_text(row, self.col_to)
        rule: Dict = {}
        if kind == attribute_rules.KIND_VALUE:
            if not value_text:
                return None
            rule["match"] = value_text
        elif kind == attribute_rules.KIND_EXPRESSION:
            if not value_text:
                return None
            rule["expression"] = value_text
        else:
            if not value_text and not to_text:
                return None
            lower = self.table.cellWidget(row, self.col_lower)
            upper = self.table.cellWidget(row, self.col_upper)
            if value_text:
                number = attribute_rules.to_number(value_text)
                rule["min"] = number if number is not None else value_text
                rule["min_inclusive"] = bool(lower.currentData()) \
                    if lower is not None else True
            if to_text:
                number = attribute_rules.to_number(to_text)
                rule["max"] = number if number is not None else to_text
                rule["max_inclusive"] = bool(upper.currentData()) \
                    if upper is not None else True
        if self.with_risk:
            risk_combo = self.table.cellWidget(row, self.col_risk)
            rule["risk"] = (risk_combo.currentData() if risk_combo is not None
                            else "") or schema.RISK_LOW
        return rule

    def rules(self) -> List[Dict]:
        """Well-formed rules in row order (blank / invalid rows dropped)."""
        out: List[Dict] = []
        for row in range(self.table.rowCount()):
            rule = self._row_rule(row)
            if rule is None or attribute_rules.validate_rule(rule):
                continue
            out.append(rule)
        return out

    def invalid_rows(self) -> List[str]:
        """Problems that would make a non-blank row be ignored."""
        problems: List[str] = []
        for row in range(self.table.rowCount()):
            rule = self._row_rule(row)
            if rule is None:
                continue
            problem = attribute_rules.validate_rule(rule)
            if problem is None and "expression" in rule:
                problem = expression_problem(rule["expression"])
            if problem:
                problems.append(f"row {row + 1}: {problem}")
        return problems

    def describe(self) -> List[str]:
        name = self._attribute_name() or ""
        return [attribute_rules.describe_rule(rule, name)
                for rule in self.rules()]


def _num_text(value) -> str:
    if value is None:
        return ""
    number = attribute_rules.to_number(value)
    return f"{number:g}" if number is not None else str(value)
