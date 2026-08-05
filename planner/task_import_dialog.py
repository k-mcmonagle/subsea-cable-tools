# -*- coding: utf-8 -*-
"""Dialog for pasting MS Project rows or loading a CSV of tasks."""

from __future__ import annotations

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QLabel, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK, ITEM_FLAG_EDITABLE
from . import task_import

_PREVIEW_ROWS = 30


class TaskImportDialog(QDialog):
    """Paste rows copied from MS Project (or any spreadsheet) or load a CSV.

    The first preview row holds one role combo per column so the user can
    correct the guessed mapping before importing.
    """

    def __init__(self, resources, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import tasks")
        self.resize(760, 560)
        self.resources = list(resources or [])
        self._rows = []
        self._roles = []
        self._suspend_parse = False
        self._parse_timer = QTimer(self)
        self._parse_timer.setSingleShot(True)
        self._parse_timer.timeout.connect(self._reparse)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Paste rows copied from MS Project (or any spreadsheet), or load a "
            "CSV file. Columns are matched automatically; use the drop-downs in "
            "the preview to correct them. Start/Finish columns are ignored — "
            "the planner recomputes the schedule from durations and links. "
            "Locations/geometry can be assigned after import.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(
            "Mobilisation\t24 ehrs\t...\nCable lay\t36 ehrs\t...")
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.text_edit.setFont(font)
        self.text_edit.textChanged.connect(lambda: self._parse_timer.start(350))
        layout.addWidget(self.text_edit, 1)

        options = QHBoxLayout()
        load_btn = QPushButton("Load CSV / text file…")
        load_btn.clicked.connect(self._load_file)
        options.addWidget(load_btn)
        self.header_check = QCheckBox("First row is headers")
        self.header_check.toggled.connect(self._on_options_changed)
        options.addWidget(self.header_check)
        self.chain_check = QCheckBox("Link tasks without predecessors to the previous task")
        self.chain_check.setChecked(True)
        options.addWidget(self.chain_check)
        options.addWidget(QLabel("First row's MS Project ID:"))
        self.first_id_spin = QSpinBox()
        self.first_id_spin.setRange(1, 99999)
        self.first_id_spin.setToolTip(
            "Predecessor numbers refer to MS Project row IDs. If you copied "
            "rows starting at ID 7, set 7 here so links resolve inside the "
            "pasted block.")
        options.addWidget(self.first_id_spin)
        options.addStretch(1)
        layout.addLayout(options)

        self.preview = QTableWidget(0, 0)
        self.preview.setToolTip(
            "First row: column roles. Following rows: preview of the parsed cells.")
        layout.addWidget(self.preview, 2)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        self.ok_button = buttons.button(BUTTON_BOX_OK)
        self.ok_button.setText("Import tasks")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_status([], [])

    # -- parsing -----------------------------------------------------------
    def _load_file(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import tasks", "",
            "CSV / text files (*.csv *.txt *.tsv);;All files (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
                self.text_edit.setPlainText(handle.read())
        except OSError as exc:
            self.status_label.setText("Could not read the file: %s" % exc)

    def _on_options_changed(self):
        if not self._suspend_parse:
            self._reparse(keep_roles=False)

    def _resource_names(self):
        return [str(row.get("name") or "") for row in self.resources]

    def _reparse(self, keep_roles=True):
        rows = task_import.split_rows(self.text_edit.toPlainText())
        self._suspend_parse = True
        try:
            if not self.header_check.isChecked() and rows:
                detected = task_import.detect_header(rows)
                if detected:
                    self.header_check.setChecked(True)
        finally:
            self._suspend_parse = False
        width = len(rows[0]) if rows else 0
        if keep_roles and len(self._roles) == width:
            roles = self._current_roles() or self._roles
        else:
            roles = task_import.guess_roles(
                rows, self.header_check.isChecked(), self._resource_names())
        self._rows = rows
        self._roles = roles
        self._rebuild_preview()
        self._update_result()

    def _current_roles(self):
        roles = []
        for column in range(self.preview.columnCount()):
            combo = self.preview.cellWidget(0, column)
            roles.append(combo.currentData() if combo is not None else "ignore")
        return roles

    def _rebuild_preview(self):
        self.preview.blockSignals(True)
        self.preview.clear()
        width = len(self._rows[0]) if self._rows else 0
        shown = self._rows[:_PREVIEW_ROWS + 1]
        self.preview.setColumnCount(width)
        self.preview.setRowCount((1 + len(shown)) if width else 0)
        self.preview.setHorizontalHeaderLabels(
            ["Column %d" % (index + 1) for index in range(width)])
        for column in range(width):
            combo = QComboBox()
            for role in task_import.ROLES:
                combo.addItem(task_import.ROLE_LABELS[role], role)
            index = combo.findData(self._roles[column]
                                   if column < len(self._roles) else "ignore")
            combo.setCurrentIndex(max(0, index))
            combo.currentIndexChanged.connect(self._on_role_changed)
            self.preview.setCellWidget(0, column, combo)
        for row_index, row in enumerate(shown, start=1):
            for column in range(width):
                item = QTableWidgetItem(row[column] if column < len(row) else "")
                item.setFlags(item.flags() & ~ITEM_FLAG_EDITABLE)
                self.preview.setItem(row_index, column, item)
        self.preview.resizeColumnsToContents()
        self.preview.blockSignals(False)

    def _on_role_changed(self, *_args):
        self._roles = self._current_roles()
        self._update_result()

    def _update_result(self):
        tasks, warnings = task_import.build_task_rows(
            self._rows, self._roles, self.header_check.isChecked(),
            self.resources,
            chain_missing_predecessors=self.chain_check.isChecked(),
            first_row_id=self.first_id_spin.value())
        self._tasks = tasks
        self._refresh_status(tasks, warnings)

    def _refresh_status(self, tasks, warnings):
        parts = ["%d task(s) ready to import." % len(tasks) if tasks
                 else "Nothing to import yet."]
        if warnings:
            shown = warnings[:6]
            parts.append("\n".join("⚠ " + warning for warning in shown))
            if len(warnings) > len(shown):
                parts.append("… and %d more warning(s)." % (len(warnings) - len(shown)))
        self.status_label.setText("\n".join(parts))
        if hasattr(self, "ok_button") and self.ok_button is not None:
            self.ok_button.setEnabled(bool(tasks))

    def accept(self):
        # Re-run with the latest options so late spinbox/checkbox edits count.
        self._update_result()
        if not self._tasks:
            return
        super().accept()

    def task_rows(self):
        return list(getattr(self, "_tasks", []) or [])
