# -*- coding: utf-8 -*-
"""Review & Export tab — summary, change log + rollback, CSV import/export."""

from __future__ import annotations

import json
from typing import Dict, List

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
)
from .. import io_csv, report, schema
from .. import ui_helpers

_LOG_COLUMNS = ["Seq", "When (UTC)", "User", "Action", "Target", "Reason"]


class ReviewTab(QWidget):
    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock

        layout = QVBoxLayout(self)
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)

        log_box = QGroupBox("Change log")
        log_layout = QVBoxLayout(log_box)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(
            "action, user, target, reason or timestamp…")
        # Debounced: the log query re-runs 250 ms after typing stops, not
        # per keystroke (long histories hit the store each time).
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)
        self._filter_timer.timeout.connect(self._refresh_log)
        self.filter_edit.textChanged.connect(
            lambda _t: self._filter_timer.start())
        filter_row.addWidget(self.filter_edit, 1)
        self.rollback_button = QPushButton("Rollback to before selected…")
        self.rollback_button.setToolTip(
            "Restore the plan to the state just before the selected log "
            "entry. The rollback is itself recorded — history is never "
            "deleted. Select a log row to enable.")
        self.rollback_button.setEnabled(False)
        self.rollback_button.clicked.connect(self._rollback)
        filter_row.addWidget(self.rollback_button)
        log_layout.addLayout(filter_row)
        self.log_table = QTableWidget(0, len(_LOG_COLUMNS))
        self.log_table.setHorizontalHeaderLabels(_LOG_COLUMNS)
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.log_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.log_table.horizontalHeader().setSectionResizeMode(
            len(_LOG_COLUMNS) - 1, HEADER_RESIZE_MODE_STRETCH)
        self.log_table.itemSelectionChanged.connect(
            lambda: self.rollback_button.setEnabled(
                self.log_table.currentRow() >= 0))
        log_layout.addWidget(self.log_table, 1)
        layout.addWidget(log_box, 1)

        io_row = QHBoxLayout()
        self.export_button = ui_helpers.menu_tool_button(
            "Export ▾",
            (("Report (HTML)…", self._export_report),
             None,
             ("Events CSV…", self._export_events),
             ("Sections CSV…", self._export_sections),
             ("Hazards CSV…", self._export_hazards),
             ("Input register CSV…", self._export_inputs)),
            tooltip="Export the report (one self-contained HTML file with "
                    "summary, profile snapshot, sections, events, the "
                    "Exclusion stack, input register, provenance and change "
                    "log) or the individual CSV registers.")
        io_row.addWidget(self.export_button)
        io_row.addSpacing(12)
        self.import_button = QPushButton("Import events / KP ranges CSV…")
        self.import_button.clicked.connect(self._import_csv)
        io_row.addWidget(self.import_button)
        self.proposal_check = QCheckBox("This is a client burial proposal")
        self.proposal_check.setToolTip(
            "Imported boundaries are tagged client_proposal; the next "
            "generation reports its differences vs the proposal.")
        io_row.addWidget(self.proposal_check)
        io_row.addStretch(1)
        layout.addLayout(io_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(self.status_label)

        refresh_soon = ui_helpers.coalesced(self, self.refresh)
        model.planChanged.connect(refresh_soon)
        model.sectionsChanged.connect(refresh_soon)
        model.pathsChanged.connect(refresh_soon)
        model.logChanged.connect(self._refresh_log)
        self.refresh()

    # -- summary ---------------------------------------------------------------
    def refresh(self) -> None:
        plan = self.model.plan
        self.export_button.setEnabled(bool(plan))
        self.import_button.setEnabled(bool(plan))
        if not plan:
            self.summary_label.setText("No plan open.")
            self._refresh_log()
            return
        sections = self.model.sections
        scope_km = abs(float(plan.get("scope_end_kp") or 0.0)
                       - float(plan.get("scope_start_kp") or 0.0))
        burial = sum(s.get("length_km") or 0.0 for s in sections
                     if s.get("kind") == schema.SECTION_BURIAL)
        skips = sum(s.get("length_km") or 0.0 for s in sections
                    if s.get("kind") == schema.SECTION_SKIP)
        insufficient = sum(s.get("length_km") or 0.0 for s in sections
                           if s.get("kind") == schema.SECTION_INSUFFICIENT)
        by_conclusion: Dict[str, float] = {}
        for section in sections:
            key = section.get("conclusion") or ""
            by_conclusion[key] = by_conclusion.get(key, 0.0) + (section.get("length_km") or 0.0)
        conclusion_bits = "".join(
            f"<li>{schema.CONCLUSION_LABELS.get(k, k) or '(unassigned)'}: "
            f"{v:.3f} km</li>"
            for k, v in sorted(by_conclusion.items()) if v > 0)
        active = self.model.store.active_generation(self.model.plan_id) or {}
        pct = (100.0 * burial / scope_km) if scope_km > 0 else 0.0
        provenance = ""
        if active:
            try:
                fingerprints = json.loads(active.get("inputs_fingerprint_json") or "{}")
            except (ValueError, TypeError):
                fingerprints = {}
            provenance = (f"<br>Generation {str(active.get('generation_id') or '')[:8]} "
                          f"run {active.get('run_utc') or '?'} · "
                          f"{len(fingerprints)} input fingerprint(s) recorded")
        path_state = self.model.path_state()
        path_note = (f"<br><b>Installation path</b> {path_state.get('tool')} · "
                     f"<b>Barge track</b> {path_state.get('barge')}")
        self.summary_label.setText(
            f"<b>Scope</b> {scope_km:.3f} km · <b>Burial</b> {burial:.3f} km "
            f"({pct:.0f}%) · <b>Skips</b> {skips:.3f} km · "
            f"<b>Insufficient Information</b> {insufficient:.3f} km · "
            f"{len(sections)} sections, {len(self.model.events)} events"
            f"<ul>{conclusion_bits}</ul>{provenance}{path_note}")
        self._refresh_log()

    def _refresh_log(self) -> None:
        needle = (self.filter_edit.text() or "").lower()
        entries = self.model.store.list_change_log(self.model.plan_id) \
            if self.model.plan else []
        if needle:
            entries = [e for e in entries
                       if needle in (e.get("action") or "").lower()
                       or needle in (e.get("reason") or "").lower()
                       or needle in (e.get("user") or "").lower()
                       or needle in str(e.get("target_id") or "").lower()
                       or needle in (e.get("utc") or "").lower()]
        entries = list(reversed(entries))  # newest first
        with ui_helpers.preserve_table_view(self.log_table):
            self.log_table.setRowCount(len(entries))
            for i, entry in enumerate(entries):
                target = str(entry.get("target_id") or "")
                values = [str(entry.get("seq") or 0), entry.get("utc") or "",
                          entry.get("user") or "", entry.get("action") or "",
                          target[:12],
                          entry.get("reason") or ""]
                for j, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    if j == 0:
                        item.setData(ITEM_DATA_USER_ROLE, entry.get("change_id"))
                    if j == 4 and len(target) > 12:
                        item.setToolTip(target)
                    self.log_table.setItem(i, j, item)
        self.rollback_button.setEnabled(self.log_table.currentRow() >= 0)

    def _rollback(self) -> None:
        row = self.log_table.currentRow()
        if row < 0:
            return
        item = self.log_table.item(row, 0)
        change_id = item.data(ITEM_DATA_USER_ROLE) if item else ""
        if not change_id:
            return
        answer = QMessageBox.question(
            self, "Rollback",
            "Restore the plan to just before the selected change? The rollback "
            "is itself recorded — history is never deleted.",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer == MESSAGE_BOX_YES:
            self.model.rollback_to(change_id)

    # -- export / import -------------------------------------------------------
    def _export(self, kind: str, text: str) -> None:
        plan_name = schema.sanitize_slug(self.model.plan.get("name") or "plan")
        path, _filter = QFileDialog.getSaveFileName(
            self, f"Export {kind}", f"{plan_name}_{kind}.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"Could not write the CSV: {exc}")
            return
        self.status_label.setText(f"Exported {kind} CSV → {path}")

    def _profile_png(self):
        """Snapshot the dock's profile pane as PNG bytes, or None.

        The pane renders even when hidden/collapsed as long as it has a
        useful size, so the report doesn't silently lose its profile image
        because of transient dock state.
        """
        try:
            from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice

            widget = self.dock.profile
            if widget is None or widget.width() < 100 or widget.height() < 60:
                return None
            pixmap = widget.grab()
            if pixmap.isNull():
                return None
            data = QByteArray()
            buffer = QBuffer(data)
            open_mode = getattr(QIODevice, "OpenModeFlag", QIODevice).WriteOnly
            buffer.open(open_mode)
            pixmap.save(buffer, "PNG")
            buffer.close()
            return bytes(data)
        except Exception:
            return None

    def _export_report(self) -> None:
        if not self.model.plan:
            return
        plan_name = schema.sanitize_slug(self.model.plan.get("name") or "plan")
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export burial plan report",
            f"{plan_name}_burial_plan_report.html", "HTML (*.html)")
        if not path:
            return
        active = self.model.store.active_generation(self.model.plan_id) or {}
        entries = self.model.store.list_change_log(self.model.plan_id)
        text = report.build_report_html(
            plan=self.model.plan, sections=self.model.sections,
            events=self.model.events, rules=self.model.rules,
            inputs=self.model.inputs, generation=active,
            change_log=entries, profile_png=self._profile_png(),
            hazards=self.model.hazards, risk_checks=self.model.risk_checks,
            tools=self.model.tools, path_result=self.model.path_result,
            path_state=self.model.path_state(),
            path_vessel=self.model.vessel(
                str(self.model.path_config().get("vessel_id") or "")))
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"Could not write the report: {exc}")
            return
        answer = QMessageBox.question(
            self, "Burial Planner",
            "Report exported. Open it in the browser now?",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_YES)
        if answer == MESSAGE_BOX_YES:
            try:
                from qgis.PyQt.QtCore import QUrl
                from qgis.PyQt.QtGui import QDesktopServices

                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            except Exception:
                pass

    def _export_events(self) -> None:
        if self.model.plan:
            self._export("events", self.model.export_events_csv())

    def _export_sections(self) -> None:
        if self.model.plan:
            self._export("sections", self.model.export_sections_csv())

    def _export_hazards(self) -> None:
        if self.model.plan:
            self._export("hazards", self.model.export_hazards_csv())

    def _export_inputs(self) -> None:
        if self.model.plan:
            self._export("inputs", self.model.export_inputs_csv())

    def _import_csv(self) -> None:
        if not self.model.plan:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import events / KP ranges", "", "CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                text = handle.read()
            fmt, events = io_csv.detect_and_parse(
                text, client_proposal=self.proposal_check.isChecked())
        except (OSError, io_csv.ImportError_) as exc:
            QMessageBox.warning(self, "Burial Planner", f"Import failed: {exc}")
            return
        try:
            self.model.import_events(events, fmt,
                                     client_proposal=self.proposal_check.isChecked())
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner",
                                f"Import rejected — it breaks the plan invariants:\n{exc}")
            return
        QMessageBox.information(self, "Burial Planner",
                                f"Imported {len(events)} event(s) from {fmt}.")
