# -*- coding: utf-8 -*-
"""QC panel: configure checks, run them, review findings and persist results.

Check parameter editors are generated automatically from each check's
``param_specs`` so adding a new check needs no UI changes here.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Dict, List

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsApplication

from ...qgis_compat import (
    EDIT_TRIGGER_DOUBLE_CLICKED,
    EDIT_TRIGGER_NONE,
    HEADER_RESIZE_MODE_STRETCH,
    MESSAGEBOX_NO,
    MESSAGEBOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
    WKB_POINT,
)
from ...laydata import QcRunner
from ...laydata.qc_checks import ALL_CHECKS
from ...processing import cable_lay_parsers as clp
from ..qc_task import QcRunTask


class QcPanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._dataset = None
        self._findings = []
        self._task = None
        self._progress = None
        self._editors: Dict[str, Dict[str, object]] = {}
        self._enable_boxes: Dict[str, QCheckBox] = {}
        self._user_role = getattr(getattr(Qt, "ItemDataRole", Qt), "UserRole")
        self._window_modal = getattr(getattr(Qt, "WindowModality", Qt), "WindowModal")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(getattr(getattr(Qt, "Orientation", Qt), "Vertical"))
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        # Scrollable check configuration (top of the splitter).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        config_widget = QWidget()
        config_layout = QVBoxLayout(config_widget)
        config_layout.setContentsMargins(2, 2, 2, 2)
        for check_cls in ALL_CHECKS:
            config_layout.addWidget(self._build_check_group(check_cls))
        config_layout.addStretch(1)
        scroll.setWidget(config_widget)
        splitter.addWidget(scroll)

        # Results area (bottom of the splitter): run buttons, status, findings.
        results = QWidget()
        results_layout = QVBoxLayout(results)
        results_layout.setContentsMargins(0, 0, 0, 0)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("Run QC")
        self.run_button.clicked.connect(self.run_qc)
        buttons.addWidget(self.run_button)
        self.save_button = QPushButton("Save findings to GeoPackage")
        self.save_button.clicked.connect(self.save_findings)
        self.save_button.setEnabled(False)
        buttons.addWidget(self.save_button)
        results_layout.addLayout(buttons)

        self.status_label = QLabel("")
        results_layout.addWidget(self.status_label)

        self.findings_table = QTableWidget(0, 5)
        self.findings_table.setHorizontalHeaderLabels(["Severity", "Check", "Message", "Time", "Value"])
        self.findings_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.findings_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.findings_table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.findings_table.setSortingEnabled(True)
        self.findings_table.horizontalHeader().setSectionResizeMode(2, HEADER_RESIZE_MODE_STRETCH)
        self.findings_table.setToolTip(
            "Click a finding to highlight it; double-click to go to it (pans the "
            "map and centres every plot)."
        )
        self.findings_table.itemSelectionChanged.connect(self._on_finding_selected)
        self.findings_table.cellDoubleClicked.connect(self._on_finding_double_clicked)
        results_layout.addWidget(self.findings_table, 1)

        splitter.addWidget(results)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

    # -- UI construction ---------------------------------------------------
    def _build_check_group(self, check_cls) -> QGroupBox:
        group = QGroupBox(check_cls.name)
        group.setToolTip(check_cls.description)
        form = QFormLayout(group)

        enable = QCheckBox("Enabled")
        enable.setChecked(True)
        self._enable_boxes[check_cls.check_id] = enable
        form.addRow(enable)

        self._editors[check_cls.check_id] = {}
        for spec in check_cls.param_specs():
            editor = self._editor_for(spec)
            self._editors[check_cls.check_id][spec.name] = editor
            label = QLabel(spec.label)
            if spec.help:
                label.setToolTip(spec.help)
                editor.setToolTip(spec.help)
            form.addRow(label, editor)
        return group

    def _editor_for(self, spec):
        if spec.kind == "bool":
            box = QCheckBox()
            box.setChecked(bool(spec.default))
            return box
        if spec.kind == "choice":
            combo = QComboBox()
            for choice in (spec.choices or []):
                combo.addItem(str(choice), choice)
            if spec.default is not None:
                idx = combo.findData(spec.default)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            return combo
        if spec.kind == "field":
            combo = QComboBox()
            combo.setEditable(True)
            return combo  # populated in set_dataset
        line = QLineEdit()
        if spec.default is not None:
            line.setText(str(spec.default))
        return line

    # -- data binding ------------------------------------------------------
    def set_dataset(self, dataset) -> None:
        self._dataset = dataset
        numeric = (
            [name for name in dataset.field_names if dataset.is_numeric_field(name)]
            if dataset is not None
            else []
        )
        for editors in self._editors.values():
            for editor in editors.values():
                if isinstance(editor, QComboBox) and editor.isEditable():
                    current = editor.currentText()
                    editor.clear()
                    editor.addItem("")
                    for name in numeric:
                        editor.addItem(name)
                    if current:
                        editor.setCurrentText(current)

    def _params_for(self, check_id: str) -> Dict[str, object]:
        params: Dict[str, object] = {}
        for name, editor in self._editors[check_id].items():
            if isinstance(editor, QCheckBox):
                params[name] = editor.isChecked()
            elif isinstance(editor, QComboBox):
                params[name] = editor.currentData() if not editor.isEditable() else editor.currentText()
            elif isinstance(editor, QLineEdit):
                params[name] = editor.text()
        return params

    # -- actions -----------------------------------------------------------
    def run_qc(self) -> None:
        if self._dataset is None:
            self.status_label.setText("Load a data layer first.")
            return
        checks = []
        for check_cls in ALL_CHECKS:
            if self._enable_boxes[check_cls.check_id].isChecked():
                checks.append((check_cls(), self._params_for(check_cls.check_id)))
        if not checks:
            self.status_label.setText("Enable at least one check.")
            return

        if self._task is not None:
            return
        self.run_button.setEnabled(False)
        self.status_label.setText("Running QC checks...")
        self._progress = QProgressDialog("Running QC checks...", "Cancel", 0, 100, self)
        self._progress.setWindowTitle("Cable Lay Data Explorer")
        self._progress.setWindowModality(self._window_modal)
        self._progress.setMinimumDuration(0)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.setValue(0)

        task = QcRunTask(self._dataset, checks)
        self._task = task
        task.progressChanged.connect(self._on_task_progress)
        task.taskCompleted.connect(self._on_task_completed)
        task.taskTerminated.connect(self._on_task_terminated)
        self._progress.canceled.connect(task.cancel)
        QgsApplication.taskManager().addTask(task)
        self._progress.show()

    def _on_task_progress(self, value) -> None:
        if self._progress is not None:
            self._progress.setValue(int(value))

    def _on_task_completed(self) -> None:
        findings = self._task.findings if self._task is not None else []
        self._findings = findings
        self._populate_findings()
        self.save_button.setEnabled(bool(self._findings))
        self.status_label.setText(f"{len(self._findings)} finding(s).")
        refresh = getattr(self.controller, "refresh_plot_overlays", None)
        if callable(refresh):
            refresh()
        self._teardown_task_ui()

    def _on_task_terminated(self) -> None:
        findings = self._task.findings if self._task is not None else []
        error = getattr(self._task, "error", None) if self._task is not None else None
        self._findings = findings
        self._populate_findings()
        self.save_button.setEnabled(bool(self._findings))
        if error:
            self.status_label.setText(f"QC failed: {error}")
        else:
            self.status_label.setText(f"QC cancelled ({len(self._findings)} finding(s) so far).")
        self._teardown_task_ui()

    def _teardown_task_ui(self) -> None:
        if self._progress is not None:
            self._progress.reset()
            self._progress.deleteLater()
            self._progress = None
        self.run_button.setEnabled(True)
        self._task = None

    def _populate_findings(self) -> None:
        self.findings_table.setSortingEnabled(False)
        self.findings_table.setRowCount(0)
        for idx, finding in enumerate(self._findings):
            row = self.findings_table.rowCount()
            self.findings_table.insertRow(row)
            value = "" if finding.value is None else f"{finding.value:g}"
            cells = [
                finding.severity,
                finding.check_id,
                finding.message,
                finding.time_start or "",
                value,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col == 0:
                    item.setData(self._user_role, idx)
                self.findings_table.setItem(row, col, item)
        self.findings_table.setSortingEnabled(True)

    def _on_finding_selected(self) -> None:
        rows = self.findings_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        item = self.findings_table.item(row, 0)
        if item is None:
            return
        index = item.data(self._user_role)
        if index is None:
            return
        index = int(index)
        if 0 <= index < len(self._findings):
            self.controller.highlight_finding(self._findings[index])

    def _on_finding_double_clicked(self, row: int, _col: int) -> None:
        item = self.findings_table.item(row, 0)
        if item is None:
            return
        index = item.data(self._user_role)
        if index is None:
            return
        index = int(index)
        if 0 <= index < len(self._findings):
            self.controller.go_to_finding(self._findings[index])

    def save_findings(self) -> None:
        if not self._findings:
            return
        gpkg_path = self.controller.gpkg_path()
        if not gpkg_path:
            QMessageBox.warning(
                self,
                "Save findings",
                "The loaded layer is not in a GeoPackage, so findings cannot be saved back to it.",
            )
            return
        run_id = uuid.uuid4().hex[:12]
        run_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        layer_name = self.controller.layer_name() or ""
        rows = QcRunner.findings_to_rows(self._findings, layer_name, run_id, run_time, clp.WKT_KEY)

        transform_context = self.controller.transform_context()
        clp.ensure_qc_layers(gpkg_path, transform_context)
        findings_layer = clp.prefixed_layer_name(gpkg_path, "qc_findings")

        replace = (
            QMessageBox.question(
                self,
                "Save findings",
                "Replace previous findings in the GeoPackage?\n"
                "Choose No to append to the existing qc_findings layer.",
                MESSAGEBOX_YES | MESSAGEBOX_NO,
            )
            == MESSAGEBOX_YES
        )
        existing_rows: List[dict] = []
        if not replace:
            existing = clp.open_gpkg_layer(gpkg_path, findings_layer)
            if existing is not None:
                existing_rows, _ = clp.rows_from_source(existing)

        try:
            clp.write_layer_to_gpkg(
                gpkg_path,
                findings_layer,
                clp.fields_from_specs(clp.QC_FINDINGS_SPECS),
                WKB_POINT,
                existing_rows + rows,
                transform_context,
            )
        except Exception as exc:  # pragma: no cover - IO failure surface to user
            QMessageBox.critical(self, "Save findings", f"Could not write findings:\n{exc}")
            return

        self.status_label.setText(f"Saved {len(rows)} finding(s) to '{findings_layer}'.")
        self.controller.refresh_findings_layer(gpkg_path, findings_layer)
