# -*- coding: utf-8 -*-
"""Route Comments dialogs and the shared comment form."""

from __future__ import annotations

import datetime as _dt
import getpass
import os
from typing import Callable, Dict, List, Optional, Sequence

from qgis.core import QgsVectorLayer
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import QDate, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtGui import QKeySequence
from qgis.PyQt.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
                                 QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                                 QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QRadioButton,
                                 QSpinBox, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
                                 QWidget)

try:
    from qgis.PyQt.QtGui import QShortcut
except ImportError:  # Qt5: QShortcut is in QtWidgets
    from qgis.PyQt.QtWidgets import QShortcut

from ..qgis_compat import (BUTTON_BOX_CANCEL, BUTTON_BOX_OK, CHECK_STATE_CHECKED, CHECK_STATE_UNCHECKED,
                           ITEM_FLAG_EDITABLE, ITEM_FLAG_USER_CHECKABLE, MAP_LAYER_FILTER_LINE,
                           SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_SINGLE)
from . import model

SETTINGS = "SubseaCableTools/route_comments"
NO_DATE = QDate(1900, 1, 1)


def default_author() -> str:
    value = str(QSettings().value(f"{SETTINGS}/author", "") or "")
    if value:
        return value
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name available
        return ""


def remember_author(name: str) -> None:
    if name:
        QSettings().setValue(f"{SETTINGS}/author", name)


def today() -> str:
    return _dt.date.today().isoformat()


def _date_edit(optional: bool) -> QDateEdit:
    edit = QDateEdit()
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd")
    if optional:
        edit.setMinimumDate(NO_DATE)
        edit.setSpecialValueText(" ")
    return edit


def _set_date(edit: QDateEdit, iso: str) -> None:
    date = QDate.fromString(str(iso or ""), "yyyy-MM-dd")
    edit.setDate(date if date.isValid() else (edit.minimumDate() if edit.specialValueText() else QDate.currentDate()))


def _get_date(edit: QDateEdit) -> str:
    if edit.specialValueText() and edit.date() == edit.minimumDate():
        return ""
    return edit.date().toString("yyyy-MM-dd")


# --------------------------------------------------------------------------- comment form
class CommentForm(QWidget):
    """Text, status, priority, author, date and, under *More*, tags and custom fields."""

    changed = pyqtSignal()

    def __init__(self, parent=None, text_lines: int = 4, show_state: bool = True):
        super().__init__(parent)
        self._fields: List[model.CustomField] = []
        self._custom_widgets: Dict[str, QWidget] = {}
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Comment")
        self.text.setTabChangesFocus(True)
        line = self.text.fontMetrics().lineSpacing()
        self.text.setMinimumHeight(line * text_lines + 12)
        self.text.setMaximumHeight(line * (text_lines + 3) + 12)
        layout.addWidget(self.text)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)
        self.status = QComboBox()
        self.status.setEditable(True)
        self.priority = QComboBox()
        self.priority.setEditable(True)
        self.author = QLineEdit()
        self.date = _date_edit(optional=True)
        state = [QLabel("Status"), self.status, QLabel("Priority"), self.priority]
        for col, widget in enumerate(state):
            grid.addWidget(widget, 0, col)
            if not show_state:
                # The dock edits them in its header; values() still returns the loaded ones. (Never
                # setVisible(True) here: the grid has no parent yet, so it would flash up as a window.)
                widget.hide()
        grid.addWidget(QLabel("Author"), 1, 0)
        grid.addWidget(self.author, 1, 1)
        grid.addWidget(QLabel("Date"), 1, 2)
        grid.addWidget(self.date, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        layout.addLayout(grid)

        self.more_button = QToolButton()
        self.more_button.setCheckable(True)
        self.more_button.setAutoRaise(True)
        self.more_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.more_button.setArrowType(Qt.ArrowType.RightArrow)
        self.more_button.setText("More fields")
        self.more_button.toggled.connect(self._toggle_more)
        layout.addWidget(self.more_button)
        self.more = QWidget()
        self.more_form = QFormLayout(self.more)
        self.more_form.setContentsMargins(12, 0, 0, 0)
        self.more_form.setVerticalSpacing(3)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("Optional, comma separated")
        self.more_form.addRow("Tags", self.tags)
        self.more.setVisible(False)
        layout.addWidget(self.more)

        self.text.textChanged.connect(self._emit)
        for combo in (self.status, self.priority):
            combo.currentTextChanged.connect(self._emit)
        self.author.textChanged.connect(self._emit)
        self.date.dateChanged.connect(self._emit)
        self.tags.textChanged.connect(self._emit)

    def _emit(self, *_args):
        if not self._loading:
            self.changed.emit()

    def _toggle_more(self, on: bool) -> None:
        self.more.setVisible(on)
        self.more_button.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

    def set_more_visible(self, on: bool) -> None:
        self.more_button.setChecked(bool(on))
        self._toggle_more(bool(on))

    def set_choices(self, statuses: Sequence[str], priorities: Sequence[str]) -> None:
        self._loading = True
        try:
            for combo, items in ((self.status, statuses), (self.priority, priorities)):
                current = combo.currentText()
                combo.clear()
                combo.addItems(list(items))
                if current:
                    combo.setCurrentText(current)
        finally:
            self._loading = False

    def set_fields(self, fields: Sequence[model.CustomField]) -> None:
        """Rebuild the custom field editors (under *More fields*)."""
        while self.more_form.rowCount() > 1:
            self.more_form.removeRow(1)
        self._fields = list(fields)
        self._custom_widgets = {}
        for fld in self._fields:
            if fld.kind == model.FIELD_NUMBER:
                widget = QLineEdit()
                widget.setPlaceholderText("Number")
                widget.textChanged.connect(self._emit)
            elif fld.kind == model.FIELD_DATE:
                widget = _date_edit(optional=True)
                widget.setDate(NO_DATE)
                widget.dateChanged.connect(self._emit)
            elif fld.kind == model.FIELD_BOOLEAN:
                widget = QComboBox()
                widget.addItems(["", "Yes", "No"])
                widget.currentIndexChanged.connect(self._emit)
            elif fld.kind == model.FIELD_CHOICE:
                widget = QComboBox()
                widget.setEditable(True)
                widget.addItems([""] + list(fld.choices))
                widget.currentTextChanged.connect(self._emit)
            else:
                widget = QLineEdit()
                widget.textChanged.connect(self._emit)
            self._custom_widgets[fld.field_id] = widget
            self.more_form.addRow(fld.name, widget)
        self.more_button.setText("More fields" + (f" ({len(self._fields) + 1})" if self._fields else ""))

    def load(self, comment: Dict) -> None:
        self._loading = True
        try:
            self.text.setPlainText(str(comment.get("text") or ""))
            self.status.setCurrentText(str(comment.get("status") or ""))
            self.priority.setCurrentText(str(comment.get("priority") or ""))
            self.author.setText(str(comment.get("author") or ""))
            _set_date(self.date, comment.get("comment_date") or "")
            self.tags.setText(str(comment.get("tags") or ""))
            values = model.load_custom(comment)
            for fld in self._fields:
                widget, value = self._custom_widgets[fld.field_id], values.get(fld.field_id)
                if fld.kind == model.FIELD_DATE:
                    _set_date(widget, value or "")
                elif fld.kind == model.FIELD_BOOLEAN:
                    widget.setCurrentIndex(0 if value is None else (1 if value else 2))
                elif fld.kind == model.FIELD_CHOICE:
                    widget.setCurrentText("" if value is None else str(value))
                else:
                    widget.setText(model.custom_text(fld, value))
            if any(values.get(f.field_id) not in (None, "") for f in self._fields) or comment.get("tags"):
                self.set_more_visible(True)
        finally:
            self._loading = False

    def custom_values(self, existing: Optional[Dict] = None) -> Dict:
        """Custom values as stored; values of fields not shown are kept."""
        values = dict(existing or {})
        for fld in self._fields:
            widget = self._custom_widgets[fld.field_id]
            if fld.kind == model.FIELD_DATE:
                raw = _get_date(widget)
            elif fld.kind == model.FIELD_BOOLEAN:
                raw = {1: True, 2: False}.get(widget.currentIndex())
            elif fld.kind == model.FIELD_CHOICE:
                raw = widget.currentText()
            else:
                raw = widget.text()
            value, _problem = model.coerce_custom(fld, raw)
            if value is None:
                values.pop(fld.field_id, None)
            else:
                values[fld.field_id] = value
        return values

    def problems(self) -> List[str]:
        out = []
        if not self.text.toPlainText().strip():
            out.append("The comment needs some text.")
        for fld in self._fields:
            widget = self._custom_widgets[fld.field_id]
            if fld.kind == model.FIELD_NUMBER and widget.text().strip():
                _value, problem = model.coerce_custom(fld, widget.text())
                if problem:
                    out.append(problem)
        return out

    def values(self, existing_custom: Optional[Dict] = None) -> Dict:
        return {
            "text": self.text.toPlainText().strip(), "status": self.status.currentText().strip(),
            "priority": self.priority.currentText().strip(), "author": self.author.text().strip(),
            "comment_date": _get_date(self.date), "tags": self.tags.text().strip(),
            "custom_json": model.dump_custom(self.custom_values(existing_custom)),
        }


# --------------------------------------------------------------------------- new comment
class NewCommentDialog(QDialog):
    """Quick entry: location (editable), then the comment. Ctrl+Enter saves."""

    def __init__(self, parent, values: Dict, capture, statuses: Sequence[str], priorities: Sequence[str],
                 fields: Sequence[model.CustomField], default_status: str, default_prio: str, set_name: str):
        super().__init__(parent)
        self.setWindowTitle(f"New comment — {set_name}" if set_name else "New comment")
        self.capture = capture
        self.kind = values.get("ref_kind")
        self.initial = dict(values)
        layout = QVBoxLayout(self)

        box = QGroupBox(model.REF_LABELS.get(self.kind, "Location"))
        form = QFormLayout(box)
        form.setVerticalSpacing(3)
        lo, hi = capture.start_kp_km, capture.end_kp_km
        self.kp = self.kp_end = self.lat = self.lon = None
        if self.kind in (model.REF_ROUTE_POINT, model.REF_KP_RANGE):
            self.kp = self._kp_spin(lo, hi, values.get("kp_km"))
            form.addRow("KP from" if self.kind == model.REF_KP_RANGE else "KP", self.kp)
            if self.kind == model.REF_KP_RANGE:
                self.kp_end = self._kp_spin(lo, hi, values.get("kp_end_km"))
                form.addRow("KP to", self.kp_end)
        else:
            self.lat = self._deg_spin(90.0, values.get("lat"))
            self.lon = self._deg_spin(180.0, values.get("lon"))
            row = QHBoxLayout()
            row.addWidget(self.lat)
            row.addWidget(QLabel("Lon"))
            row.addWidget(self.lon)
            form.addRow("Lat", row)
            self.near = QLabel()
            self.near.setWordWrap(True)
            form.addRow("Nearest KP", self.near)
            self.lat.valueChanged.connect(self._update_near)
            self.lon.valueChanged.connect(self._update_near)
        route = QLabel(f"{model.route_text(values) or values.get('route_name') or ''}  ·  KP start "
                       f"{lo:.3f}, end {hi:.3f}  ·  {values.get('kp_basis') or ''}")
        route.setWordWrap(True)
        route.setStyleSheet("color: palette(mid);")
        form.addRow("Route", route)
        layout.addWidget(box)

        self.form = CommentForm(self)
        self.form.set_choices(statuses, priorities)
        self.form.set_fields(fields)
        self.form.load({"status": default_status, "priority": default_prio, "author": default_author(),
                        "comment_date": today()})
        self.form.set_more_visible(bool(QSettings().value(f"{SETTINGS}/new_more", False, type=bool)))
        layout.addWidget(self.form)

        self.error = QLabel()
        self.error.setStyleSheet("color: #b00020;")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.button(BUTTON_BOX_OK).setText("Add comment")
        buttons.button(BUTTON_BOX_OK).setToolTip("Ctrl+Enter")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(keys), self, activated=self.accept)
        self._update_near()
        self.resize(460, self.sizeHint().height())
        self.form.text.setFocus()

    @staticmethod
    def _kp_spin(lo: float, hi: float, value) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(3)
        spin.setRange(lo, hi)
        spin.setSingleStep(0.001)
        spin.setSuffix(" km")
        spin.setValue(float(value) if value is not None else lo)
        return spin

    @staticmethod
    def _deg_spin(limit: float, value) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(6)
        spin.setRange(-limit, limit)
        spin.setSingleStep(0.0001)
        spin.setValue(float(value) if value is not None else 0.0)
        return spin

    def _update_near(self, *_args) -> None:
        if self.lat is None:
            return
        try:
            values = self.location_values()
            if values.get("kp_km") is None:
                self.near.setText("—")
            else:
                self.near.setText(f"KP {values['kp_km']:.3f}, {values.get('dcc_m') or 0.0:,.0f} m off route "
                                  "(kept apart from the position)")
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            self.near.setText(str(exc))

    def location_values(self) -> Dict:
        """The reference columns for the location as now entered."""
        if self.kind == model.REF_ROUTE_POINT:
            if abs(self.kp.value() - float(self.initial.get("kp_km") or 0.0)) < 5e-7:
                return dict(self.initial)
            return self.capture.point_values(self.kp.value())
        if self.kind == model.REF_KP_RANGE:
            if abs(self.kp.value() - float(self.initial.get("kp_km") or 0.0)) < 5e-7 and \
                    abs(self.kp_end.value() - float(self.initial.get("kp_end_km") or 0.0)) < 5e-7:
                return dict(self.initial)
            return self.capture.range_values(self.kp.value(), self.kp_end.value())
        if abs(self.lat.value() - float(self.initial.get("lat") or 0.0)) < 5e-9 and \
                abs(self.lon.value() - float(self.initial.get("lon") or 0.0)) < 5e-9:
            return dict(self.initial)
        return self.capture.location_from_wgs84(self.lat.value(), self.lon.value())

    def result_values(self) -> Dict:
        values = self.location_values()
        values.update(self.form.values())
        return values

    def accept(self) -> None:
        problems = self.form.problems()
        if not problems:
            try:
                self.location_values()
            except Exception as exc:  # noqa: BLE001 - shown in the dialog
                problems.append(str(exc))
        if problems:
            self.error.setText("\n".join(problems))
            self.error.show()
            return
        remember_author(self.form.author.text().strip())
        QSettings().setValue(f"{SETTINGS}/new_more", self.form.more_button.isChecked())
        super().accept()


# --------------------------------------------------------------------------- list editors
class _RowsEditor(QWidget):
    """A small table with Add / Remove / Up / Down buttons."""

    def __init__(self, headers: Sequence[str], parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(list(headers))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        buttons = QVBoxLayout()
        for text, slot in (("Add", self.add_row), ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                           ("Down", lambda: self._move(1))):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    def add_row(self, *_args) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.init_row(row)
        self.table.setCurrentCell(row, 0)
        return row

    def init_row(self, row: int) -> None:
        for col in range(self.table.columnCount()):
            if self.table.item(row, col) is None and self.table.cellWidget(row, col) is None:
                self.table.setItem(row, col, QTableWidgetItem(""))

    def _remove(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _move(self, step: int) -> None:
        row = self.table.currentRow()
        target = row + step
        if row < 0 or not (0 <= target < self.table.rowCount()):
            return
        values = [self.take(row), self.take(target)]
        self.put(row, values[1])
        self.put(target, values[0])
        self.table.setCurrentCell(target, 0)

    # Subclasses: one row as a tuple and back.
    def take(self, row: int):
        return tuple(self.table.item(row, c).text() if self.table.item(row, c) else ""
                     for c in range(self.table.columnCount()))

    def put(self, row: int, values) -> None:
        for col, value in enumerate(values):
            self.table.setItem(row, col, QTableWidgetItem(str(value)))


class _StatusEditor(_RowsEditor):
    def __init__(self, parent=None):
        super().__init__(["Status", "Counts as closed"], parent)

    def init_row(self, row: int) -> None:
        self.put(row, ("", False))

    def take(self, row: int):
        name = self.table.item(row, 0).text() if self.table.item(row, 0) else ""
        check = self.table.item(row, 1)
        return name, bool(check is not None and check.checkState() == CHECK_STATE_CHECKED)

    def put(self, row: int, values) -> None:
        self.table.setItem(row, 0, QTableWidgetItem(str(values[0])))
        check = QTableWidgetItem("")
        check.setFlags((check.flags() | ITEM_FLAG_USER_CHECKABLE) & ~ITEM_FLAG_EDITABLE)
        check.setCheckState(CHECK_STATE_CHECKED if values[1] else CHECK_STATE_UNCHECKED)
        self.table.setItem(row, 1, check)


class ChoicesDialog(QDialog):
    """Status and priority choices of a comment set (any order, no rules)."""

    def __init__(self, parent, set_name: str, statuses: Sequence[Dict], priorities: Sequence[str]):
        super().__init__(parent)
        self.setWindowTitle(f"Statuses and priorities — {set_name}")
        layout = QVBoxLayout(self)
        note = QLabel("Choices offered when editing comments, in this order (the first status is the default "
                      "for new comments). Any status can follow any other. Comments with a closed status are "
                      "drawn grey and can be hidden.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.statuses = _StatusEditor()
        for item in statuses:
            self.statuses.put(self.statuses.add_row(), (item["name"], item.get("closed")))
        layout.addWidget(self.statuses)
        self.priorities = _RowsEditor(["Priority"])
        for name in priorities:
            self.priorities.put(self.priorities.add_row(), (name,))
        layout.addWidget(self.priorities)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(420, 460)

    def result_choices(self):
        statuses = [{"name": n.strip(), "closed": c} for n, c in
                    (self.statuses.take(r) for r in range(self.statuses.table.rowCount())) if n.strip()]
        priorities = [self.priorities.take(r)[0].strip() for r in range(self.priorities.table.rowCount())]
        return model.normalise_statuses(statuses), model.normalise_priorities([p for p in priorities if p])


class _FieldEditor(_RowsEditor):
    def __init__(self, parent=None):
        super().__init__(["Field", "Type", "Choices (comma separated)"], parent)
        self.ids: Dict[int, str] = {}

    def init_row(self, row: int) -> None:
        self.put(row, ("", "", model.FIELD_TEXT, []))

    def take(self, row: int):
        name = self.table.item(row, 0).text() if self.table.item(row, 0) else ""
        combo = self.table.cellWidget(row, 1)
        kind = combo.currentData() if combo is not None else model.FIELD_TEXT
        choices = self.table.item(row, 2).text() if self.table.item(row, 2) else ""
        field_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) if self.table.item(row, 0) else ""
        return name, field_id or "", kind, [c.strip() for c in choices.split(",") if c.strip()]

    def put(self, row: int, values) -> None:
        name, field_id, kind, choices = values
        item = QTableWidgetItem(str(name))
        item.setData(Qt.ItemDataRole.UserRole, field_id)
        self.table.setItem(row, 0, item)
        combo = QComboBox()
        for key in model.FIELD_TYPES:
            combo.addItem(model.FIELD_TYPE_LABELS[key], key)
        combo.setCurrentIndex(max(0, list(model.FIELD_TYPES).index(kind) if kind in model.FIELD_TYPES else 0))
        self.table.setCellWidget(row, 1, combo)
        self.table.setItem(row, 2, QTableWidgetItem(", ".join(choices)))


class FieldsDialog(QDialog):
    """Custom fields of a comment set."""

    def __init__(self, parent, set_name: str, fields: Sequence[model.CustomField]):
        super().__init__(parent)
        self.setWindowTitle(f"Custom fields — {set_name}")
        layout = QVBoxLayout(self)
        note = QLabel("Optional extra fields for this set's comments (shown under *More fields*, as table "
                      "columns and in exports). Choices apply to Choice fields only. Removing a field hides its "
                      "values; adding a field with the same name again shows them.")
        note.setTextFormat(Qt.TextFormat.MarkdownText)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.editor = _FieldEditor()
        for fld in fields:
            self.editor.put(self.editor.add_row(), (fld.name, fld.field_id, fld.kind, fld.choices))
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 360)

    def result_fields(self) -> List[model.CustomField]:
        out = []
        for row in range(self.editor.table.rowCount()):
            name, field_id, kind, choices = self.editor.take(row)
            if name.strip():
                out.append(model.CustomField(field_id, name.strip(), kind, choices, row))
        return out


# --------------------------------------------------------------------------- import
class ImportDialog(QDialog):
    """Import new comments from CSV/XLSX with column mapping and validation.

    ``build_rows(valid ImportRows, route layer or None)`` turns validated
    rows into comment values with locations (the dock supplies it).
    """

    def __init__(self, parent, set_name: str, fields: Sequence[model.CustomField], statuses, priorities,
                 existing_ids, route_layer: Optional[QgsVectorLayer], route_kp_range: Callable,
                 build_rows: Callable):
        super().__init__(parent)
        self.setWindowTitle(f"Import comments — {set_name}")
        self.fields = list(fields)
        self.statuses, self.priorities, self.existing = statuses, priorities, set(existing_ids)
        self.route_kp_range = route_kp_range
        self.build_rows = build_rows
        self.rows: List[List[str]] = []
        self.checked: List[model.ImportRow] = []
        self.result_rows: List[Dict] = []
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("CSV, TSV or Excel (.xlsx) file")
        self.path.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        top.addWidget(self.path, 1)
        top.addWidget(browse)
        layout.addLayout(top)
        opts = QHBoxLayout()
        self.sheet = QComboBox()
        self.sheet.setEnabled(False)
        self.sheet.currentTextChanged.connect(self._reload)
        self.header_row = QSpinBox()
        self.header_row.setRange(1, 100)
        self.header_row.valueChanged.connect(self._headers_changed)
        opts.addWidget(QLabel("Sheet"))
        opts.addWidget(self.sheet, 1)
        opts.addWidget(QLabel("Header row"))
        opts.addWidget(self.header_row)
        layout.addLayout(opts)

        route_row = QHBoxLayout()
        self.route = QgsMapLayerComboBox()
        self.route.setFilters(MAP_LAYER_FILTER_LINE)
        self.route.setAllowEmptyLayer(True)
        if route_layer is not None:
            self.route.setLayer(route_layer)
        else:
            self.route.setLayer(None)
        self.route.layerChanged.connect(self._validate)
        route_row.addWidget(QLabel("KPs refer to"))
        route_row.addWidget(self.route, 1)
        layout.addLayout(route_row)

        self.mapping = QTableWidget(0, 2)
        self.mapping.setHorizontalHeaderLabels(["Comment field", "Column in the file"])
        self.mapping.verticalHeader().setVisible(False)
        self.mapping.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.mapping.setMaximumHeight(230)
        layout.addWidget(self.mapping)
        self.targets = list(model.IMPORT_TARGETS) + [(f"cf:{f.field_id}", f.name) for f in self.fields]
        self._combos: Dict[str, QComboBox] = {}

        images = QHBoxLayout()
        self.image_folder = QLineEdit()
        self.image_folder.setPlaceholderText("The file's own folder")
        self.image_folder.setToolTip("Image files named without a folder in the image column are looked up here")
        self.image_folder.editingFinished.connect(self._validate)
        folder_button = QPushButton("Browse…")
        folder_button.clicked.connect(self._browse_images)
        self.images_on_map = QCheckBox("Show each comment's first image on the map")
        self.images_on_map.setChecked(QSettings().value(f"{SETTINGS}/import_images_on_map", False, type=bool))
        images.addWidget(QLabel("Image files from"))
        images.addWidget(self.image_folder, 1)
        images.addWidget(folder_button)
        layout.addLayout(images)
        layout.addWidget(self.images_on_map)

        self.preview = QTableWidget(0, 4)
        self.preview.setHorizontalHeaderLabels(["Row", "Import", "Comment", "Notes"])
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.preview, 1)
        self.summary = QLabel("Choose a file.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        note = QLabel("Rows need comment text and a location: latitude and longitude (a map location), a KP (a "
                      "route point) or KP and KP to (a KP range) on the route chosen above. IDs already in the "
                      "register, or repeated in the file, are skipped. Image files named in an image column "
                      "(several separated by ;) are copied into the register's attachments folder. Replies are "
                      "not imported.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)
        self.buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        self.buttons.button(BUTTON_BOX_OK).setText("Import")
        self.buttons.button(BUTTON_BOX_OK).setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.resize(720, 720)

    def _browse(self) -> None:
        folder = str(QSettings().value(f"{SETTINGS}/import_dir", "") or "")
        path, _filter = QFileDialog.getOpenFileName(self, "Import comments", folder,
                                                    "Tables (*.csv *.tsv *.txt *.xlsx *.xlsm);;All files (*)")
        if path:
            QSettings().setValue(f"{SETTINGS}/import_dir", os.path.dirname(path))
            self.load_file(path)

    def _browse_images(self) -> None:
        start = self.image_folder.text() or os.path.dirname(self.path.text())
        folder = QFileDialog.getExistingDirectory(self, "Image files from", start)
        if folder:
            self.image_folder.setText(folder)
            self._validate()

    def images_dir(self) -> str:
        return self.image_folder.text().strip() or os.path.dirname(self.path.text())

    def load_file(self, path: str, sheet: Optional[str] = None) -> None:
        from .exchange import read_table
        self.path.setText(path)
        try:
            rows, sheets = read_table(path, sheet)
        except Exception as exc:  # noqa: BLE001 - unreadable files raise many error types
            self.rows = []
            self.summary.setText(f"Could not read the file: {exc}")
            self._fill_mapping([])
            return
        self.rows = rows
        self.sheet.blockSignals(True)
        self.sheet.clear()
        self.sheet.addItems(sheets)
        if sheet:
            self.sheet.setCurrentText(sheet)
        self.sheet.setEnabled(bool(sheets))
        self.sheet.blockSignals(False)
        self.header_row.blockSignals(True)
        self.header_row.setValue(1)
        self.header_row.blockSignals(False)
        self._headers_changed()

    def _reload(self, sheet: str) -> None:
        if self.path.text() and sheet:
            self.load_file(self.path.text(), sheet)

    def headers(self) -> List[str]:
        index = self.header_row.value() - 1
        return list(self.rows[index]) if 0 <= index < len(self.rows) else []

    def _headers_changed(self, *_args) -> None:
        self._fill_mapping(self.headers())

    def _fill_mapping(self, headers: Sequence[str]) -> None:
        guess = model.guess_mapping(headers, self.fields)
        self.mapping.setRowCount(len(self.targets))
        self._combos = {}
        for row, (key, label) in enumerate(self.targets):
            item = QTableWidgetItem(label + (" *" if key == "text" else ""))
            item.setFlags(item.flags() & ~ITEM_FLAG_EDITABLE)
            self.mapping.setItem(row, 0, item)
            combo = QComboBox()
            combo.addItem("(not in the file)", -1)
            for col, header in enumerate(headers):
                combo.addItem(f"{col + 1}: {header}" if header else f"Column {col + 1}", col)
            combo.setCurrentIndex(guess.get(key, -1) + 1)
            combo.currentIndexChanged.connect(self._validate)
            self.mapping.setCellWidget(row, 1, combo)
            self._combos[key] = combo
        self._validate()

    def current_mapping(self) -> Dict[str, int]:
        return {key: combo.currentData() for key, combo in self._combos.items() if combo.currentData() >= 0}

    def _validate(self, *_args) -> None:
        start = self.header_row.value()
        data = self.rows[start:] if self.rows else []
        layer = self.route.currentLayer()
        kp_range = self.route_kp_range(layer) if layer is not None else None
        self.checked = model.validate_import(data, self.current_mapping(), self.fields, self.statuses,
                                             self.priorities, self.existing, kp_range, first_line=start + 1,
                                             default_author=default_author(), default_date=today(),
                                             image_folder=self.images_dir())
        self.preview.setRowCount(len(self.checked))
        for row, item in enumerate(self.checked):
            cells = [str(item.line), "Yes" if item.ok else "No", model.summary(item.values.get("text"), 60),
                     "; ".join(item.errors + item.warnings)]
            for col, text in enumerate(cells):
                self.preview.setItem(row, col, QTableWidgetItem(text))
        mapped = "text" in self.current_mapping()
        has_images = "images" in self.current_mapping()
        for widget in (self.image_folder, self.images_on_map):
            widget.setEnabled(has_images)
        self.summary.setText(model.import_summary(self.checked) if self.rows else "Choose a file.")
        if self.rows and not mapped:
            self.summary.setText("Choose the column that holds the comment text.")
        self.buttons.button(BUTTON_BOX_OK).setEnabled(mapped and any(r.ok for r in self.checked))

    def accept(self) -> None:
        try:
            self.result_rows = self.build_rows([r for r in self.checked if r.ok], self.route.currentLayer())
        except Exception as exc:  # noqa: BLE001 - shown in the dialog
            self.summary.setText(f"Could not place the comments: {exc}")
            return
        on_map = self.images_on_map.isEnabled() and self.images_on_map.isChecked()
        QSettings().setValue(f"{SETTINGS}/import_images_on_map", self.images_on_map.isChecked())
        for row in self.result_rows:
            row["attach_on_map"] = on_map
        super().accept()


# --------------------------------------------------------------------------- export
class ExportDialog(QDialog):
    """Choose what to export: the visible rows, the set, or the whole register."""

    def __init__(self, parent, counts: Dict[str, int], set_name: str):
        super().__init__(parent)
        self.setWindowTitle("Export comments")
        layout = QVBoxLayout(self)
        self.visible = QRadioButton(f"Visible rows of '{set_name}' ({counts.get('visible', 0)}, as filtered and "
                                    "sorted)")
        self.set_all = QRadioButton(f"All of '{set_name}' ({counts.get('set', 0)})")
        self.register = QRadioButton(f"The whole register, every set ({counts.get('register', 0)})")
        self.visible.setChecked(True)
        for button in (self.visible, self.set_all, self.register):
            layout.addWidget(button)
        note = QLabel(model.EXPORT_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.button(BUTTON_BOX_OK).setText("Export…")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(460, self.sizeHint().height())

    def scope(self) -> str:
        if self.register.isChecked():
            return "register"
        return "set" if self.set_all.isChecked() else "visible"



# --------------------------------------------------------------------------- labels
class LabelDialog(QDialog):
    """What comment labels show as the map zooms in: the number far out, then
    a short line, then the chosen fields (or any expression) with the image.
    Applied to both comment layers."""

    def __init__(self, parent, settings, layer=None):
        from qgis.gui import QgsExpressionLineEdit
        from .map_layers import LABEL_PARTS
        super().__init__(parent)
        self.setWindowTitle("Comment labels")
        self.layer = layer
        layout = QVBoxLayout(self)
        steps = QGroupBox("As you zoom in (each from the scale given)")
        steps_grid = QGridLayout(steps)
        steps_grid.setVerticalSpacing(4)
        self.numbers = QCheckBox("Number (#12)")
        self.number_scale = self._scale_spin(settings.number_scale)
        self.summary = QCheckBox("Number and the start of the comment")
        self.summary_scale = self._scale_spin(settings.summary_scale)
        self.summary_chars = QSpinBox()
        self.summary_chars.setRange(5, 80)
        self.summary_chars.setValue(int(settings.summary_chars))
        self.summary_chars.setSuffix(" characters")
        self.detail_scale = self._scale_spin(settings.detail_scale)
        steps_grid.addWidget(self.numbers, 0, 0)
        steps_grid.addWidget(self.number_scale, 0, 1)
        steps_grid.addWidget(self.summary, 1, 0)
        steps_grid.addWidget(self.summary_scale, 1, 1)
        steps_grid.addWidget(self.summary_chars, 1, 2)
        steps_grid.addWidget(QLabel("Details (below) and images"), 2, 0)
        steps_grid.addWidget(self.detail_scale, 2, 1)
        steps_grid.setColumnStretch(0, 1)
        self.numbers.toggled.connect(self.number_scale.setEnabled)
        self.summary.toggled.connect(self.summary_scale.setEnabled)
        self.summary.toggled.connect(self.summary_chars.setEnabled)
        for check in (self.numbers, self.summary):
            check.toggled.connect(self._update)
        for spin in (self.number_scale, self.summary_scale, self.detail_scale):
            spin.valueChanged.connect(self._update)
        steps.setToolTip("Labels grow as you zoom in. Where they would crowd, QGIS leaves some out, keeping "
                         "comments that need attention first.")
        layout.addWidget(steps)

        box = QGroupBox("Details")
        grid = QGridLayout(box)
        self.parts = {}
        for i, (key, text, _expr) in enumerate(LABEL_PARTS):
            check = QCheckBox(text)
            check.setChecked(key in settings.parts)
            check.toggled.connect(self._update)
            self.parts[key] = check
            grid.addWidget(check, i // 2, i % 2)
        row_index = (len(LABEL_PARTS) + 1) // 2
        self.separator = QComboBox()
        for key, text in (("space", "Space"), ("dot", "Middle dot ( · )"), ("newline", "New line")):
            self.separator.addItem(text, key)
        self.separator.setCurrentIndex(max(0, self.separator.findData(settings.separator)))
        self.separator.currentIndexChanged.connect(self._update)
        self.chars = QSpinBox()
        self.chars.setRange(5, 300)
        self.chars.setValue(int(settings.comment_chars))
        self.chars.setSuffix(" characters")
        self.chars.valueChanged.connect(self._update)
        grid.addWidget(QLabel("Between fields"), row_index, 0)
        grid.addWidget(self.separator, row_index, 1)
        grid.addWidget(QLabel("Comment shortened to"), row_index + 1, 0)
        grid.addWidget(self.chars, row_index + 1, 1)
        self.use_custom = QCheckBox("Use an expression instead")
        self.custom = QgsExpressionLineEdit()
        if layer is not None:
            self.custom.setLayer(layer)
        self.custom.setExpression(settings.custom or "")
        self.use_custom.setChecked(bool(settings.custom.strip()))
        self.use_custom.toggled.connect(self._custom_toggled)
        self.custom.expressionChanged.connect(self._update)
        grid.addWidget(self.use_custom, row_index + 2, 0, 1, 2)
        grid.addWidget(self.custom, row_index + 3, 0, 1, 2)
        layout.addWidget(box)

        from .map_layers import STYLE_LEADER, STYLES
        shape = QHBoxLayout()
        self.style = QComboBox()
        for key, text in STYLES.items():
            self.style.addItem(text, key)
        self.style.setCurrentIndex(max(0, self.style.findData(settings.style)))
        self.leader = QDoubleSpinBox()
        self.leader.setRange(2.0, 30.0)
        self.leader.setSingleStep(1.0)
        self.leader.setDecimals(1)
        self.leader.setValue(float(settings.leader_mm))
        self.leader.setSuffix(" mm")
        self.leader.setToolTip("How far the tag sits from the comment; QGIS picks the side with room")
        self.style.currentIndexChanged.connect(
            lambda _i: self.leader.setEnabled(self.style.currentData() == STYLE_LEADER))
        self.leader.setEnabled(self.style.currentData() == STYLE_LEADER)
        shape.addWidget(QLabel("Style"))
        shape.addWidget(self.style, 1)
        shape.addWidget(QLabel("Leader"))
        shape.addWidget(self.leader)
        layout.addLayout(shape)

        look = QHBoxLayout()
        self.size = QDoubleSpinBox()
        self.size.setRange(5.0, 20.0)
        self.size.setSingleStep(0.5)
        self.size.setValue(float(settings.size))
        self.size.setSuffix(" pt")
        self.background = QCheckBox("White tag behind the text")
        self.background.setChecked(bool(settings.background))
        look.addWidget(QLabel("Text size"))
        look.addWidget(self.size)
        look.addStretch(1)
        look.addWidget(self.background)
        layout.addLayout(look)
        pictures = QHBoxLayout()
        self.images = QCheckBox("Show images chosen for the map in the detail tags,")
        self.images.setToolTip("Each comment can show one of its images (Show on map, under the image in the "
                               "dock) at the top of its tag, with the details below it")
        self.images.setChecked(bool(settings.images))
        self.image_mm = QDoubleSpinBox()
        self.image_mm.setRange(8.0, 150.0)
        self.image_mm.setSingleStep(5.0)
        self.image_mm.setDecimals(0)
        self.image_mm.setValue(float(settings.image_mm))
        self.image_mm.setSuffix(" mm wide")
        self.images.toggled.connect(self.image_mm.setEnabled)
        self.image_mm.setEnabled(self.images.isChecked())
        pictures.addWidget(self.images)
        pictures.addWidget(self.image_mm)
        pictures.addStretch(1)
        layout.addLayout(pictures)
        self.example = QLabel()
        self.example.setWordWrap(True)
        self.example.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.example)
        note = QLabel("Labels go on the two comment layers. You can also change them (and the symbols) in "
                      "Layer Properties; this dialog only replaces the labels when you press OK.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        layout.addWidget(note)
        restore = QDialogButtonBox.StandardButton.RestoreDefaults
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL | restore)
        buttons.button(restore).setToolTip("Put every choice here back to the tool's defaults (applied on OK)")
        buttons.button(restore).clicked.connect(self.restore_defaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.numbers.setChecked(bool(settings.numbers))
        self.number_scale.setEnabled(bool(settings.numbers))
        self.summary.setChecked(bool(settings.summary))
        self.summary_scale.setEnabled(bool(settings.summary))
        self.summary_chars.setEnabled(bool(settings.summary))
        self._custom_toggled(self.use_custom.isChecked())

    def restore_defaults(self) -> None:
        from .map_layers import LabelSettings
        default = LabelSettings()
        self.numbers.setChecked(default.numbers)
        self.number_scale.setValue(default.number_scale)
        self.summary.setChecked(default.summary)
        self.summary_scale.setValue(default.summary_scale)
        self.summary_chars.setValue(int(default.summary_chars))
        self.detail_scale.setValue(default.detail_scale)
        for key, check in self.parts.items():
            check.setChecked(key in default.parts)
        self.separator.setCurrentIndex(max(0, self.separator.findData(default.separator)))
        self.chars.setValue(int(default.comment_chars))
        self.use_custom.setChecked(False)
        self.custom.setExpression("")
        self.style.setCurrentIndex(max(0, self.style.findData(default.style)))
        self.leader.setValue(float(default.leader_mm))
        self.size.setValue(float(default.size))
        self.background.setChecked(default.background)
        self.images.setChecked(default.images)
        self.image_mm.setValue(float(default.image_mm))
        self._update()

    @staticmethod
    def _scale_spin(value) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(100, 50000000)
        try:   # steps in proportion to the scale (1:20,000 and 1:2,000,000 alike)
            spin.setStepType(QAbstractSpinBox.StepType.AdaptiveDecimalStepType)
        except AttributeError:
            spin.setSingleStep(5000)
        spin.setPrefix("1:")
        spin.setGroupSeparatorShown(True)
        spin.setValue(int(value))
        return spin

    def _custom_toggled(self, on: bool) -> None:
        if on and not self.custom.expression().strip():
            self.custom.setExpression(self._settings(custom=False).detail_expression())
        self.custom.setEnabled(on)
        for widget in list(self.parts.values()) + [self.separator, self.chars]:
            widget.setEnabled(not on)
        self._update()

    def _settings(self, custom: bool = True):
        from .map_layers import LabelSettings
        return LabelSettings(
            numbers=self.numbers.isChecked(), number_scale=self.number_scale.value(),
            summary=self.summary.isChecked(), summary_scale=self.summary_scale.value(),
            summary_chars=self.summary_chars.value(), detail_scale=self.detail_scale.value(),
            parts=[k for k, check in self.parts.items() if check.isChecked()],
            separator=self.separator.currentData(), comment_chars=self.chars.value(),
            custom=self.custom.expression() if custom and self.use_custom.isChecked() else "",
            size=self.size.value(), background=self.background.isChecked(),
            style=self.style.currentData(), leader_mm=self.leader.value(),
            images=self.images.isChecked(), image_mm=self.image_mm.value())

    def result_settings(self):
        return self._settings()

    def _update(self, *_args) -> None:
        """Show the label of the layer's first feature."""
        text = ""
        if self.layer is not None:
            from qgis.core import QgsExpression, QgsExpressionContext, QgsExpressionContextUtils
            feature = next(self.layer.getFeatures(), None)
            if feature is not None:
                expression = QgsExpression(self._settings().detail_expression())
                context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(self.layer))
                context.setFeature(feature)
                value = expression.evaluate(context)
                text = "Expression error: " + expression.evalErrorString() if expression.hasEvalError() \
                    else str(value if value is not None else "")
        lines = [("Example: " + text.replace("\n", " ⏎ "))] if text else []
        steps = self._settings(custom=False)
        hidden = []
        if steps.summary and steps.summary_scale <= steps.detail_scale:
            hidden.append("the short line (set it further out than the details)")
        inner = steps.summary_scale if steps.summary and steps.summary_scale > steps.detail_scale \
            else steps.detail_scale
        if steps.numbers and steps.number_scale <= inner:
            hidden.append("the number (set it further out than the next step)")
        if hidden:
            lines.append("Not shown: " + "; ".join(hidden) + ".")
        self.example.setText("\n".join(lines))
