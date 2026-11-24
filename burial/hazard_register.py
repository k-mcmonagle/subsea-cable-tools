# -*- coding: utf-8 -*-
"""Search, filter and sort the entire register while keeping review edits keyed by hazard ID."""

from __future__ import annotations

import json
import math

from qgis.PyQt.QtCore import (
    QAbstractTableModel, QItemSelectionModel, QModelIndex, QSortFilterProxyModel, Qt, pyqtSignal,
)
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from ..qgis_compat import (
    BUTTON_BOX_CANCEL, BUTTON_BOX_OK, DIALOG_ACCEPTED, SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED, qt_exec,
)
from . import risk, schema, ui_helpers
from .risk_scale import DEFAULT_SCALE

HEADERS = ("Risk", "Status", "KP", "End KP", "Offset (m)", "Interaction", "Angle (°)",
           "Feature", "Check", "Notes", "Score")
RISK, STATUS, KP, END_KP, OFFSET, CONTACT, ANGLE, FEATURE, CHECK, NOTES, SCORE = range(len(HEADERS))
NUMERIC = {KP, END_KP, OFFSET, ANGLE, SCORE}
CATEGORIES = {RISK, STATUS, CONTACT, CHECK}
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 1
TIPS = {
    RISK: "Click to change the effective rating. Hover over a rating to see the check's assessment.",
    STATUS: "Click to change review status.",
    KP: "Kilometre point along the route. Double-click to locate the hazard.",
    END_KP: "Last kilometre point of a range. A point hazard has a single KP.",
    OFFSET: "Nearest approach in metres. Positive is starboard and negative is port in the direction of travel.",
    CONTACT: "The spatial relationship to the route, or the origin of a KP-only entry.",
    ANGLE: "Acute angle in degrees at a line crossing.",
    NOTES: "Double-click to edit the review notes.",
    SCORE: "Numeric score of the effective risk level on this plan's scale.",
}


def _number(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class HazardModel(QAbstractTableModel):
    editRequested = pyqtSignal(str, str, str)

    def __init__(self, parent):
        super().__init__(parent)
        self.replace([], [], DEFAULT_SCALE)

    def replace(self, hazards, checks, scale):
        names = {c["check_id"]: c.get("name") or "" for c in checks}
        self.beginResetModel()
        self.scale = scale
        self.colors = ui_helpers.risk_colors(scale)
        self.hazards = list(hazards)
        self.by_id = {}
        self.values, self.texts, self.search_texts = [], [], []
        for row, hazard in enumerate(hazards):
            self.by_id[hazard.get("hazard_id")] = row
            kp, end = _number(hazard.get("kp")), _number(hazard.get("end_kp"))
            level, status = hazard.get("risk") or "", hazard.get("status") or schema.HAZARD_STATUS_OPEN
            values = (level, status, kp, end if end != kp else None, _number(risk.measured_offset(hazard)),
                      risk.interaction(hazard), _number(hazard.get("crossing_angle_deg")), hazard.get("label") or "",
                      hazard.get("check_id") or "", hazard.get("notes") or "", scale.scores.get(level))
            texts = [str(v) if v is not None else "" for v in values]
            texts[RISK] = scale.label(level)
            texts[STATUS] = schema.HAZARD_STATUS_LABELS.get(status, status)
            texts[CHECK] = names.get(values[CHECK], "Manual")
            for col in (KP, END_KP):
                texts[col] = schema.format_kp(values[col]) if values[col] is not None else ""
            if values[OFFSET] is not None:
                texts[OFFSET] = f"{values[OFFSET]:+.1f}" if values[OFFSET] else "0.0"
            if values[ANGLE] is not None:
                texts[ANGLE] = f"{values[ANGLE]:.1f}"
            self.values.append(values)
            self.texts.append(texts)
            self.search_texts.append(" ".join(texts).casefold())
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.hazards)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(HEADERS):
            if role == Qt.ItemDataRole.DisplayRole:
                return HEADERS[section]
            if role == Qt.ItemDataRole.ToolTipRole:
                return TIPS.get(section) or HEADERS[section]
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() in (RISK, STATUS, NOTES):
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        value = self.values[row][col]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self.texts[row][col]
        if role == SORT_ROLE:
            if col == RISK:
                return self.scale.rank(value)
            return value if col in NUMERIC else self.texts[row][col].casefold()
        if role == Qt.ItemDataRole.UserRole:
            return self.hazards[row].get("hazard_id")
        if role == ui_helpers.COMBO_VALUE_ROLE:
            return value
        if role == ui_helpers.COMBO_FLAG_ROLE:
            return col in (RISK, STATUS)
        if role == Qt.ItemDataRole.DecorationRole and col == RISK:
            return self.colors.get(value)
        if role == Qt.ItemDataRole.ToolTipRole:
            hazard = self.hazards[row]
            if col == RISK:
                source = "User rating" if hazard.get("risk_source") == schema.RISK_SOURCE_USER else "Automatic"
                return f"{source}. Check rating: {self.scale.caption(hazard.get('auto_risk'))}."
            if col == FEATURE:
                try:
                    attrs = json.loads(hazard.get("attributes_json") or "{}")
                except (TypeError, ValueError):
                    attrs = {}
                if isinstance(attrs, dict) and attrs:
                    return "\n".join(f"{k}: {v}" for k, v in attrs.items())
            return self.texts[row][col] or TIPS.get(col)
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if index.isValid() and index.column() == NOTES and role == Qt.ItemDataRole.EditRole:
            if value != self.values[index.row()][NOTES]:
                self.editRequested.emit(index.data(Qt.ItemDataRole.UserRole), "notes", str(value))
            return True
        return False

    def choices(self, column):
        if column == RISK:
            return [(key, self.scale.caption(key)) for key in ("", *self.scale.levels)]
        if column == STATUS:
            return list(schema.HAZARD_STATUS_LABELS.items())
        choices = {v[column]: t[column] for v, t in zip(self.values, self.texts)}
        return sorted(choices.items(), key=lambda pair: pair[1].casefold())


class HazardFilter(QSortFilterProxyModel):
    def __init__(self, parent):
        super().__init__(parent)
        self.words = []
        self.columns = {}
        self.setSortRole(SORT_ROLE)

    def filterAcceptsRow(self, row, parent):
        model = self.sourceModel()
        if any(word not in model.search_texts[row] for word in self.words):
            return False
        for col, wanted in self.columns.items():
            value = model.values[row][col]
            if col in NUMERIC:
                lo, hi = wanted
                if value is None or (lo is not None and value < lo) or (hi is not None and value > hi):
                    return False
            elif col in CATEGORIES:
                if value != wanted:
                    return False
            elif wanted.casefold() not in model.texts[row][col].casefold():
                return False
        return True


class ColumnFiltersDialog(QDialog):
    def __init__(self, model, filters, parent):
        super().__init__(parent)
        self.setWindowTitle("Hazard column filters")
        self.filters = dict(filters)
        self.fields = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for col, title in enumerate(HEADERS):
            current = filters.get(col)
            if col in NUMERIC:
                bounds = QHBoxLayout()
                edits = []
                for bound, hint in zip(current or (None, None), ("Minimum", "Maximum")):
                    edit = QLineEdit("" if bound is None else str(bound))
                    edit.setPlaceholderText(hint)
                    edit.setAccessibleName(f"{title} {hint.lower()}")
                    bounds.addWidget(edit)
                    edits.append(edit)
                self.fields[col] = edits
                form.addRow(title, bounds)
            elif col in CATEGORIES:
                combo = QComboBox()
                combo.addItem("All")
                for key, label in model.choices(col):
                    combo.addItem(label, key)
                if current is not None:
                    position = combo.findData(current)
                    if position < 0:
                        combo.addItem(str(current), current)
                        position = combo.count() - 1
                    combo.setCurrentIndex(position)
                self.fields[col] = combo
                form.addRow(title, combo)
            else:
                edit = QLineEdit(current or "")
                edit.setPlaceholderText("Contains…")
                self.fields[col] = edit
                form.addRow(title, edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        filters = {}
        for col, widget in self.fields.items():
            if col in NUMERIC:
                try:
                    lo, hi = [float(w.text()) if w.text().strip() else None for w in widget]
                    if any(v is not None and not math.isfinite(v) for v in (lo, hi)):
                        raise ValueError
                    if lo is not None and hi is not None and lo > hi:
                        raise ValueError
                except ValueError:
                    QMessageBox.warning(self, self.windowTitle(),
                                        f"Enter valid bounds for {HEADERS[col]}. The minimum must be at most the maximum.")
                    return
                if lo is not None or hi is not None:
                    filters[col] = (lo, hi)
            elif col in CATEGORIES:
                if widget.currentIndex() > 0:
                    filters[col] = widget.currentData()
            elif widget.text().strip():
                filters[col] = widget.text().strip()
        self.filters = filters
        self.accept()


class HazardRegister(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.plan_id = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search all hazard columns…")
        self.search.setClearButtonEnabled(True)
        self.filters_button = QPushButton("Column filters…")
        self.clear = QPushButton("Clear")
        self.clear.setToolTip("Clear the search and all column filters.")
        self.count = QLabel()
        bar.addWidget(self.search, 1)
        for widget in (self.filters_button, self.clear, self.count):
            bar.addWidget(widget)
        layout.addLayout(bar)
        self.source = HazardModel(self)
        self.proxy = HazardFilter(self)
        self.proxy.setSourceModel(self.source)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(KP, Qt.SortOrder.AscendingOrder)
        header = self.table.horizontalHeader()
        for position, col in enumerate((FEATURE, KP, RISK, SCORE, STATUS, CONTACT)):
            header.moveSection(header.visualIndex(col), position)
        for col, width in enumerate((105, 95, 82, 82, 86, 103, 68, 155, 125, 225, 62)):
            self.table.setColumnWidth(col, width)
        ui_helpers.enable_column_menu(self.table, "SubseaCableTools/BurialPlanner/hazard_hidden_columns",
                                      always_visible=(RISK, KP))
        header.setToolTip("Click a heading to sort. Right-click to show or hide columns.")
        self.delegate = ui_helpers.ComboColumnDelegate(self.table, self._edit_choices, self._commit)
        for col in (RISK, STATUS):
            self.table.setItemDelegateForColumn(col, self.delegate)
        layout.addWidget(self.table)
        self.filters_button.clicked.connect(self._edit_filters)
        self.clear.clicked.connect(self.clear_filters)
        self.search.textChanged.connect(self._apply_filters)

    def _edit_choices(self, index):
        return self.source.choices(index.column())

    def _commit(self, index, value):
        self.source.editRequested.emit(index.data(Qt.ItemDataRole.UserRole),
                                       "risk" if index.column() == RISK else "status", value)

    def _edit_filters(self):
        dialog = ColumnFiltersDialog(self.source, self.proxy.columns, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            self.proxy.columns = dialog.filters
            self._apply_filters()

    def clear_filters(self):
        self.proxy.columns = {}
        self.search.clear()
        self._apply_filters()

    def _apply_filters(self):
        self.proxy.words = self.search.text().casefold().split()
        self.proxy.invalidateFilter()
        n = len(self.proxy.columns)
        self.filters_button.setText(f"Column filters ({n})…" if n else "Column filters…")
        active = bool(n or self.proxy.words)
        self.clear.setEnabled(active)
        self.count.setText(f"{self.proxy.rowCount():,} of {self.source.rowCount():,}" if active else "")

    def selected_ids(self):
        return [i.data(Qt.ItemDataRole.UserRole) for i in self.table.selectionModel().selectedRows()]

    def hazard_at(self, row):
        source = self.proxy.mapToSource(self.proxy.index(row, 0))
        return self.source.hazards[source.row()] if source.isValid() else None

    def replace(self, hazards, checks, scale, plan_id):
        selection = self.table.selectionModel()
        same_plan = plan_id == self.plan_id
        selected = self.selected_ids() if same_plan else []
        current = self.table.currentIndex()
        current_id = current.data(Qt.ItemDataRole.UserRole) if same_plan else None
        scroll = self.table.verticalScrollBar()
        position = scroll.value() if same_plan else 0
        if not same_plan:
            self.clear_filters()
        self.plan_id = plan_id
        self.source.replace(hazards, checks, scale)
        self._apply_filters()
        flags = QItemSelectionModel.SelectionFlag
        for key in selected:
            row = self.source.by_id.get(key)
            if row is not None:
                index = self.proxy.mapFromSource(self.source.index(row, 0))
                if index.isValid():
                    selection.select(index, flags.Select | flags.Rows)
                    if key == current_id:
                        selection.setCurrentIndex(index, flags.NoUpdate)
        scroll.setValue(position)
