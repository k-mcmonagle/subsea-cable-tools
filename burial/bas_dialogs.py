# -*- coding: utf-8 -*-
"""BAS register dialogs: CSV/XLSX import (KP columns + flexible columns +
KP reference) and the column editor."""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    CHECK_STATE_CHECKED,
    CHECK_STATE_UNCHECKED,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    ITEM_FLAG_USER_CHECKABLE,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
)
from . import bas_model, ground_model, kp_rereference, ui_helpers
from .ground_dialogs import KpReferenceWidget


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


class BasImportDialog(QDialog):
    """Table file → BAS rows. Start/end KP columns are chosen (guessed);
    every other column comes in as an editable register column."""

    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self.setWindowTitle("Import Burial Assessment Study")
        self.resize(720, 700)
        self._headers: List[str] = []
        self._rows: List[List[str]] = []
        self._sheets: List[str] = []
        self._problems: List[str] = []
        self._tally: Dict[str, int] = {}
        self.rows: List[Dict] = []
        self.columns: List[Dict] = []
        self.kp_map: Optional[kp_rereference.KpMap] = None
        self.replace_existing = True
        self.source_ref = ""

        layout = QVBoxLayout(self)
        file_box = QGroupBox("File")
        file_form = QFormLayout(file_box)
        file_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        file_row.addWidget(self.path_edit, 1)
        file_row.addWidget(browse)
        file_form.addRow("File:", file_row)
        self.sheet_combo = QComboBox()
        self.sheet_combo.currentIndexChanged.connect(self._reload_sheet)
        self.sheet_label = QLabel("Sheet:")
        self.sheet_combo.setVisible(False)
        self.sheet_label.setVisible(False)
        file_form.addRow(self.sheet_label, self.sheet_combo)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Document and revision, e.g. 'BAS 1234-R-002 Rev B'")
        file_form.addRow("Source reference:", self.source_edit)
        self.file_summary = QLabel("No file loaded.")
        self.file_summary.setWordWrap(True)
        self.file_summary.setStyleSheet(ui_helpers.hint_style())
        file_form.addRow("", self.file_summary)
        layout.addWidget(file_box)

        columns_box = QGroupBox("Columns")
        columns_form = QFormLayout(columns_box)
        self.start_combo = QComboBox()
        self.end_combo = QComboBox()
        self.start_combo.currentIndexChanged.connect(self._kp_columns_changed)
        self.end_combo.currentIndexChanged.connect(self._kp_columns_changed)
        columns_form.addRow("Start KP column *:", self.start_combo)
        columns_form.addRow("End KP column *:", self.end_combo)
        self.kp_unit = QComboBox()
        self.kp_unit.addItem("km", False)
        self.kp_unit.addItem("m", True)
        columns_form.addRow("KP unit in file:", self.kp_unit)
        self.include_list = QListWidget()
        self.include_list.setMaximumHeight(150)
        self.include_list.setToolTip(
            "Every ticked column becomes an editable register column "
            "(number/text guessed from the data). Headers matching an "
            "existing column reuse it.")
        columns_form.addRow("Import as columns:", self.include_list)
        layout.addWidget(columns_box)

        self.kp_ref = KpReferenceWidget(model, dock, self)
        layout.addWidget(self.kp_ref)

        mode_row = QHBoxLayout()
        self.replace_check = QCheckBox("Replace the plan's existing BAS rows")
        self.replace_check.setChecked(True)
        self.replace_check.setToolTip("Untick to append to the rows already in the register.")
        mode_row.addWidget(self.replace_check)
        mode_row.addStretch(1)
        preview = QPushButton("Preview")
        preview.clicked.connect(self._preview)
        mode_row.addWidget(preview)
        layout.addLayout(mode_row)
        self.preview_label = QLabel("Load a file, check the KP columns, then Preview.")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        self.ok_button = box.button(BUTTON_BOX_OK)
        self.ok_button.setText("Import")
        self.ok_button.setEnabled(False)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import Burial Assessment Study", "",
            "Tables (*.csv *.txt *.tsv *.xlsx *.xlsm);;CSV (*.csv *.txt *.tsv);;"
            "Excel (*.xlsx *.xlsm);;All files (*)")
        if not path:
            return
        self.path_edit.setText(path)
        if not self.source_edit.text().strip():
            self.source_edit.setText(os.path.splitext(os.path.basename(path))[0])
        self._load(path, None)

    def _load(self, path: str, sheet: Optional[str]) -> None:
        try:
            headers, rows, sheets = ground_model.read_table_file(path, sheet)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Import BAS", f"Could not read the file:\n{exc}")
            return
        self._headers, self._rows, self._sheets = headers, rows, sheets
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        for name in sheets:
            self.sheet_combo.addItem(name)
        if sheet and sheet in sheets:
            self.sheet_combo.setCurrentIndex(sheets.index(sheet))
        self.sheet_combo.blockSignals(False)
        self.sheet_combo.setVisible(bool(sheets))
        self.sheet_label.setVisible(bool(sheets))
        self.file_summary.setText(
            f"{len(rows)} data row(s), {len(headers)} column(s): "
            + ", ".join(h or "(blank)" for h in headers[:12])
            + (" …" if len(headers) > 12 else ""))
        start, end = bas_model.guess_kp_columns(headers)
        for combo, guess in ((self.start_combo, start), (self.end_combo, end)):
            combo.blockSignals(True)
            combo.clear()
            for i, header in enumerate(headers):
                combo.addItem(header or f"(column {i + 1})", i)
            combo.setCurrentIndex(max(0, combo.findData(guess if guess is not None else -1)))
            combo.blockSignals(False)
        self._rebuild_include_list()
        self.ok_button.setEnabled(False)
        self.preview_label.setText("Check the KP columns, then Preview.")

    def _reload_sheet(self, *_args) -> None:
        if self._sheets and self.path_edit.text():
            self._load(self.path_edit.text(), self.sheet_combo.currentText())

    def _kp_columns_changed(self, *_args) -> None:
        self._rebuild_include_list()
        self.ok_button.setEnabled(False)

    def _rebuild_include_list(self) -> None:
        checked = {self.include_list.item(i).data(ITEM_DATA_USER_ROLE)
                   for i in range(self.include_list.count())
                   if self.include_list.item(i).checkState() == CHECK_STATE_CHECKED}
        fresh = self.include_list.count() == 0
        self.include_list.clear()
        skip = {self.start_combo.currentData(), self.end_combo.currentData()}
        for i, header in enumerate(self._headers):
            if i in skip:
                continue
            item = QListWidgetItem(header or f"(column {i + 1})")
            item.setFlags(item.flags() | ITEM_FLAG_USER_CHECKABLE)
            item.setData(ITEM_DATA_USER_ROLE, i)
            item.setCheckState(CHECK_STATE_CHECKED if fresh or i in checked
                               else CHECK_STATE_UNCHECKED)
            self.include_list.addItem(item)

    def _compute(self) -> None:
        if not self._rows:
            raise ValueError("Load a file first.")
        start = self.start_combo.currentData()
        end = self.end_combo.currentData()
        if start is None or end is None or start == end:
            raise ValueError("Choose two different columns for start and end KP.")
        include = [self.include_list.item(i).data(ITEM_DATA_USER_ROLE)
                   for i in range(self.include_list.count())
                   if self.include_list.item(i).checkState() == CHECK_STATE_CHECKED]
        existing = self.model.bas_columns() if not self.replace_check.isChecked() else []
        rows, columns, problems = bas_model.table_to_rows(
            self._headers, self._rows, int(start), int(end), include=include,
            kp_in_metres=bool(self.kp_unit.currentData()),
            existing_columns=existing)
        self._problems = problems
        if not rows:
            raise ValueError("No rows could be read" + (f": {problems[0]}" if problems else "."))
        kp_map = self.kp_ref.build_map()
        mapped, tally = bas_model.rereference_rows(
            rows, kp_map, source_label=self.kp_ref.source_label(), use_source_kps=False)
        self.rows, self.columns, self.kp_map, self._tally = mapped, columns, kp_map, tally

    def _preview(self) -> None:
        try:
            self._compute()
        except ValueError as exc:
            self.preview_label.setText(f"<b style='color:{ui_helpers.color('error')}'>"
                                       f"{_esc(exc)}</b>")
            self.ok_button.setEnabled(False)
            return
        except Exception as exc:
            self.preview_label.setText(f"<b style='color:{ui_helpers.color('error')}'>"
                                       f"Preview failed: {_esc(exc)}</b>")
            self.ok_button.setEnabled(False)
            return
        lo = min(r["start_kp"] for r in self.rows)
        hi = max(r["end_kp"] for r in self.rows)
        lines = [f"<b>{len(self.rows)} row(s)</b>, KP {lo:.3f}–{hi:.3f} on the plan "
                 f"route; {len(self.columns)} column(s): "
                 + _esc(", ".join(c["label"] for c in self.columns[:10]))
                 + (" …" if len(self.columns) > 10 else "")]
        scope = self.model.gen_params().scope
        if scope.length_km > 0 and (hi < scope.start_km or lo > scope.end_km):
            lines.append(f"<span style='color:{ui_helpers.color('warn')}'>The KP range "
                         f"does not overlap the plan scope (KP {scope.start_km:.3f}–"
                         f"{scope.end_km:.3f}) — check the KP unit and reference.</span>")
        if self.kp_map is not None and not self.kp_map.is_identity:
            lines.append(_esc(self.kp_map.diagnostics.summary()))
            if self._tally:
                lines.append("Flags — " + _esc(", ".join(
                    f"{k}: {v}" for k, v in sorted(self._tally.items()))))
        issues = bas_model.validate_rows(self.rows, self.columns)
        if issues:
            lines.append(f"<span style='color:{ui_helpers.color('warn')}'>{len(issues)} "
                         f"note(s): {_esc('; '.join(issues[:3]))}"
                         + (" …" if len(issues) > 3 else "") + "</span>")
        if self._problems:
            lines.append(f"<span style='color:{ui_helpers.color('warn')}'>"
                         f"{len(self._problems)} row(s) skipped: "
                         f"{_esc('; '.join(self._problems[:3]))}"
                         + (" …" if len(self._problems) > 3 else "") + "</span>")
        self.preview_label.setText("<br>".join(lines))
        self.ok_button.setEnabled(True)

    def _accept(self) -> None:
        if not self.ok_button.isEnabled():
            self._preview()
            if not self.ok_button.isEnabled():
                return
        self.replace_existing = self.replace_check.isChecked()
        self.source_ref = self.source_edit.text().strip()
        self.accept()


class BasColumnsDialog(QDialog):
    """Rename, retype, reorder, add and remove the register's columns.
    Values keep their keys, so renaming never loses data; removing a
    column drops its values on the next Apply."""

    def __init__(self, columns: List[Dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("BAS columns")
        self.resize(520, 380)
        self._rows: List[Dict] = [dict(c) for c in bas_model.normalise_columns(columns)]
        self.columns: List[Dict] = []
        layout = QVBoxLayout(self)
        hint = QLabel("Number columns are checked for numeric values on Apply "
                      "and sort numerically; text columns sort alphabetically.")
        hint.setWordWrap(True)
        hint.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(hint)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Label", "Kind"])
        self.table.horizontalHeader().setSectionResizeMode(HEADER_RESIZE_MODE_STRETCH)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        self._delegate = ui_helpers.ComboColumnDelegate(
            self.table, self._combo_options, self._combo_commit)
        self.table.setItemDelegateForColumn(1, self._delegate)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        for text, slot in (("Add", self._add), ("Remove", self._remove),
                           ("Move up", lambda: self._move(-1)),
                           ("Move down", lambda: self._move(1))):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self._rebuild()

    def _combo_options(self, index):
        if index.column() != 1:
            return None
        return [(k, bas_model.KIND_LABELS[k]) for k in bas_model.KINDS]

    def _combo_commit(self, index, value) -> None:
        if 0 <= index.row() < len(self._rows):
            self._rows[index.row()]["kind"] = value

    def _rebuild(self) -> None:
        with ui_helpers.silent_rebuild(self.table):
            self.table.setRowCount(len(self._rows))
            for i, col in enumerate(self._rows):
                self.table.setItem(i, 0, QTableWidgetItem(col.get("label") or ""))
                kind_item = QTableWidgetItem()
                kind = col.get("kind") or bas_model.KIND_TEXT
                ui_helpers.ComboColumnDelegate.mark_item(
                    kind_item, kind, bas_model.KIND_LABELS.get(kind, kind))
                self.table.setItem(i, 1, kind_item)

    def _sync(self) -> None:
        for i, col in enumerate(self._rows):
            item = self.table.item(i, 0)
            col["label"] = (item.text() if item else "").strip() or col.get("label") or "Column"

    def _add(self) -> None:
        self._sync()
        taken = {c["key"] for c in self._rows}
        self._rows.append(bas_model.make_column("New column", bas_model.KIND_TEXT, taken))
        self._rebuild()
        self.table.setCurrentCell(len(self._rows) - 1, 0)
        self.table.editItem(self.table.item(len(self._rows) - 1, 0))

    def _remove(self) -> None:
        self._sync()
        row = self.table.currentRow()
        if 0 <= row < len(self._rows):
            del self._rows[row]
            self._rebuild()

    def _move(self, delta: int) -> None:
        self._sync()
        row = self.table.currentRow()
        target = row + delta
        if 0 <= row < len(self._rows) and 0 <= target < len(self._rows):
            self._rows[row], self._rows[target] = self._rows[target], self._rows[row]
            self._rebuild()
            self.table.selectRow(target)

    def _save(self) -> None:
        self._sync()
        labels = [c["label"].casefold() for c in self._rows]
        if len(set(labels)) != len(labels):
            QMessageBox.warning(self, "BAS columns", "Column labels must be unique.")
            return
        self.columns = bas_model.normalise_columns(self._rows)
        self.accept()
