"""Compare saved plan copies and create checked recovery files off the UI thread."""
from __future__ import annotations

import os

from qgis.core import QgsApplication, QgsTask
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTableWidgetItem, QTextEdit, QVBoxLayout,
)

from ..geopackage_health import checked_snapshot, inspect_file
from ..qgis_compat import EDIT_TRIGGER_NONE, SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_SINGLE
from ..table_widgets import ResizableTableWidget


class GeoPackageHealthDialog(QDialog):
    def __init__(self, current_path: str, parent=None):
        super().__init__(parent)
        self.current_path = current_path
        self.reports = []
        self._task = None
        self.setWindowTitle("Check / recover Burial Planner GeoPackages")
        self.resize(1100, 650)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "For OneDrive / SharePoint conflicts, close QGIS on both machines and pause sync first. "
            "Keep every candidate file and its -wal / -shm / -journal sidecars together. "
            "Compare saved plans and change times below; file modification times alone cannot identify "
            "the newest work. Copies are never merged or opened automatically. "
            "Use an unsynced local folder for recovery and a single editor for each working file.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = ResizableTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            "File", "SQLite integrity", "Schema", "Plans", "Latest saved change log entry (UTC)"])
        self.table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.table, 1)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        actions = QHBoxLayout()
        self.check = QPushButton("Choose files to check…")
        self.copy = QPushButton("Create checked copy…")
        self.rebuild = QPushButton("Try index repair in a new copy…")
        self.close_button = QPushButton("Close")
        for button in (self.check, self.copy, self.rebuild, self.close_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.status = QLabel("Select the active file and any migration or sync-conflict copies to compare.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.check.clicked.connect(self._choose)
        self.copy.clicked.connect(lambda: self._snapshot(False))
        self.rebuild.clicked.connect(lambda: self._snapshot(True))
        self.close_button.clicked.connect(self.reject)
        self._selection()

    def _selected(self):
        row = self.table.currentRow()
        return self.reports[row] if 0 <= row < len(self.reports) else None

    def _selection(self):
        report = self._selected()
        busy = self._task is not None
        self.check.setEnabled(not busy)
        self.copy.setEnabled(bool(report) and not busy)
        self.rebuild.setEnabled(bool(report) and not busy)
        self.close_button.setEnabled(not busy)
        if report:
            lines = [report.path, "SQLite check: " + ("OK" if report.healthy else "FAILED"),
                     report.error, "", "Saved plans (name — header modification time UTC):"]
            lines.extend(f"{name} — {stamp or 'unknown'}" for name, stamp in report.plans)
            lines.extend(["", "Saved row counts:"])
            lines.extend(f"{table}: {count}" for table, count in report.counts.items())
            lines.extend(["", "Latest saved analysis / generation / import times (UTC):"])
            lines.extend(f"{table}: {stamp}" for table, stamp in report.saved_times.items())
            lines.extend(report.notes)
            lines.append(
                "\nDetails from a damaged file may be incomplete. Times and row counts help compare "
                "copies; they do not establish which edits are intended or recover unsaved session data.")
            self.details.setPlainText("\n".join(lines))

    def _choose(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Burial Planner copies to compare", self.current_path,
            "GeoPackage (*.gpkg);;All files (*)")
        if paths:
            self._run(lambda _task: [inspect_file(path) for path in paths])

    def _snapshot(self, rebuild):
        report = self._selected()
        if not report:
            return
        if rebuild:
            QMessageBox.information(
                self, "Index repair in a separate file",
                "This can repair some index damage. It cannot recover damaged table pages or merge "
                "sync conflicts. The source is preserved. A new file is kept only if its SQLite "
                "integrity check passes; inspect your plans before adopting it.")
        stem, ext = os.path.splitext(os.path.basename(report.path))
        target, _ = QFileDialog.getSaveFileName(
            self, "Save a new copy in an unsynced local folder", stem + ".recovered" + ext,
            "GeoPackage (*.gpkg)")
        if not target:
            return
        if not target.lower().endswith(".gpkg"):
            target += ".gpkg"

        def work(_task):
            checked_snapshot(report.path, target, rebuild_indexes=rebuild)
            return [inspect_file(target)]

        self._run(work, target)

    def _run(self, work, created=""):
        def finished(error, reports=None):
            if sip.isdeleted(self):
                return
            self._task = None
            if error:
                self.status.setText(f"No recovery file was created: {error}" if created else str(error))
            else:
                for report in reports or []:
                    existing = next((i for i, old in enumerate(self.reports)
                                     if old.path == report.path), None)
                    if existing is None:
                        self.reports.append(report)
                    else:
                        self.reports[existing] = report
                self.table.setRowCount(len(self.reports))
                for row, report in enumerate(self.reports):
                    values = (os.path.basename(report.path), "OK" if report.healthy else "FAILED",
                              report.schema_version, str(len(report.plans)), report.latest_change)
                    for column, value in enumerate(values):
                        item = QTableWidgetItem(value)
                        item.setToolTip(report.path)
                        self.table.setItem(row, column, item)
                if self.reports:
                    self.table.selectRow(len(self.reports) - 1)
                self.status.setText(
                    f"Checked copy created: {created}. Review it using Plan file → Open existing plans."
                    if created else "Checks complete. Select a row to review saved plans and counts.")
            self._selection()

        self._task = QgsTask.fromFunction("Check Burial Planner GeoPackages", work, on_finished=finished)
        self.status.setText("Working… keep this window open. Large files may take a minute.")
        self._selection()
        QgsApplication.taskManager().addTask(self._task)

    def reject(self):
        if self._task is None:
            super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore()
        else:
            super().closeEvent(event)
