# -*- coding: utf-8 -*-
"""Plan Builder tab — generate, then refine events and sections.

Regeneration contract (spec §12.7): auto candidate events are disposable;
locked and confirmed events persist (conflict-flagged if newly inside an
Exclusion Area); nothing user-made is silently deleted or moved.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor, QKeySequence
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
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
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
    qt_exec,
)
from .. import events as ev
from .. import schema

_EVENT_COLUMNS = ["Seq", "Event", "KP", "Lat", "Lon", "Depth (m)", "Source",
                  "Status", "Locked", "Notes"]
_SECTION_COLUMNS = ["Kind", "Start KP", "End KP", "Length (km)", "State",
                    "Conclusion", "Confidence", "Reasons", "Notes"]

_STATUS_COLORS = {
    schema.EVENT_STATUS_CANDIDATE: QColor("#b36b00"),
    schema.EVENT_STATUS_CONFIRMED: QColor("#1b5e20"),
    schema.EVENT_STATUS_CONFLICT: QColor("#b71c1c"),
}

_VERTICAL = getattr(Qt, "Orientation", Qt).Vertical


class SectionRangeDialog(QDialog):
    """Explicit range for inserting an opposite-kind section."""

    def __init__(self, section: Dict, kind_label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Split section / insert range")
        start = float(section.get("start_kp") or 0.0)
        end = float(section.get("end_kp") or 0.0)
        length = max(end - start, 0.0)
        width = min(0.100, length / 3.0)
        centre = (start + end) / 2.0

        layout = QVBoxLayout(self)
        note = QLabel(
            f"Insert {kind_label} inside the selected section. Two editable "
            "PLDN/PLUP boundaries will be created at the entered KPs.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.start_spin = QDoubleSpinBox()
        self.end_spin = QDoubleSpinBox()
        for spin in (self.start_spin, self.end_spin):
            spin.setDecimals(3)
            spin.setRange(start, end)
            spin.setSuffix(" km")
        self.start_spin.setValue(centre - width / 2.0)
        self.end_spin.setValue(centre + width / 2.0)
        form.addRow("Start KP:", self.start_spin)
        form.addRow("End KP:", self.end_spin)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def range_kp(self):
        return self.start_spin.value(), self.end_spin.value()


class BuilderTab(QWidget):
    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._loading = False

        layout = QVBoxLayout(self)

        run_row = QHBoxLayout()
        self.generate_button = QPushButton("Generate")
        self.generate_button.setToolTip(
            "Run the Exclusion stack over the scope and rebuild candidate "
            "sections and events in the background.")
        self.generate_button.clicked.connect(lambda: self.dock.request_generation())
        run_row.addWidget(self.generate_button)
        self.cancel_button = QPushButton("Stop (resumable)")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.dock.cancel_analysis)
        run_row.addWidget(self.cancel_button)
        self.undo_button = QPushButton("Undo last edit")
        self.undo_button.setShortcut(QKeySequence("Ctrl+Z"))
        self.undo_button.setToolTip(
            "Undo the latest Plan Builder edit (Ctrl+Z). The undo is recorded "
            "in the change log and does not resample bathymetry.")
        self.undo_button.clicked.connect(self._undo_last_edit)
        run_row.addWidget(self.undo_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        run_row.addWidget(self.progress, 1)
        self.run_status = QLabel("")
        run_row.addWidget(self.run_status, 2)
        layout.addLayout(run_row)

        self.diff_label = QLabel("")
        self.diff_label.setWordWrap(True)
        layout.addWidget(self.diff_label)

        splitter = QSplitter(_VERTICAL)

        events_widget = QWidget()
        events_layout = QVBoxLayout(events_widget)
        events_layout.setContentsMargins(0, 0, 0, 0)
        events_layout.addWidget(QLabel("Events"))
        self.events_table = QTableWidget(0, len(_EVENT_COLUMNS))
        self.events_table.setHorizontalHeaderLabels(_EVENT_COLUMNS)
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.events_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.events_table.horizontalHeader().setSectionResizeMode(
            len(_EVENT_COLUMNS) - 1, HEADER_RESIZE_MODE_STRETCH)
        self.events_table.itemChanged.connect(self._on_event_item_changed)
        self.events_table.itemSelectionChanged.connect(self._on_event_selected)
        self.events_table.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.events_table.customContextMenuRequested.connect(
            self._event_context_menu)
        self.events_table.cellDoubleClicked.connect(
            lambda row, _column: self._goto_event_row(row))
        events_layout.addWidget(self.events_table, 1)

        event_buttons = QHBoxLayout()
        add_row = QHBoxLayout()
        self.add_type_combo = QComboBox()
        add_row.addWidget(self.add_type_combo)
        self.add_kp_spin = QDoubleSpinBox()
        self.add_kp_spin.setDecimals(3)
        self.add_kp_spin.setRange(0.0, 100000.0)
        self.add_kp_spin.setSuffix(" km")
        add_row.addWidget(self.add_kp_spin)
        pick_button = QPushButton("Pick…")
        pick_button.setToolTip(
            "Pick the KP by clicking the route on the map "
            "(right-click or Esc cancels). Double-clicking the profile "
            "also sets this KP.")
        pick_button.clicked.connect(self._pick_add_kp)
        add_row.addWidget(pick_button)
        add_button = QPushButton("Add event")
        add_button.clicked.connect(self._add_event)
        add_row.addWidget(add_button)
        event_buttons.addLayout(add_row)
        event_buttons.addSpacing(12)
        self.nudge_spin = QDoubleSpinBox()
        self.nudge_spin.setDecimals(0)
        self.nudge_spin.setRange(1.0, 1000.0)
        self.nudge_spin.setValue(10.0)
        self.nudge_spin.setSuffix(" m")
        self.nudge_spin.setToolTip("Nudge step")
        for label, slot in (("−", lambda: self._nudge(-1)), ("＋", lambda: self._nudge(1))):
            button = QPushButton(label)
            button.setMaximumWidth(28)
            button.clicked.connect(slot)
            event_buttons.addWidget(button)
        event_buttons.addWidget(self.nudge_spin)
        event_buttons.addSpacing(12)
        for label, slot in (("Confirm", self._confirm_selected),
                            ("Confirm all", self._confirm_all),
                            ("Lock", lambda: self._lock_selected(True)),
                            ("Unlock", lambda: self._lock_selected(False)),
                            ("Delete", self._delete_selected)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            event_buttons.addWidget(button)
        event_buttons.addStretch(1)
        events_layout.addLayout(event_buttons)
        splitter.addWidget(events_widget)

        sections_widget = QWidget()
        sections_layout = QVBoxLayout(sections_widget)
        sections_layout.setContentsMargins(0, 0, 0, 0)
        sections_layout.addWidget(QLabel("Sections"))
        self.sections_table = QTableWidget(0, len(_SECTION_COLUMNS))
        self.sections_table.setHorizontalHeaderLabels(_SECTION_COLUMNS)
        self.sections_table.verticalHeader().setVisible(False)
        self.sections_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.sections_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.sections_table.horizontalHeader().setSectionResizeMode(
            7, HEADER_RESIZE_MODE_STRETCH)
        self.sections_table.itemSelectionChanged.connect(self._on_section_selected)
        self.sections_table.itemChanged.connect(self._on_section_item_changed)
        self.sections_table.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.sections_table.customContextMenuRequested.connect(
            self._section_context_menu)
        self.sections_table.cellDoubleClicked.connect(
            lambda row, _column: self._goto_section_row(row))
        sections_layout.addWidget(self.sections_table, 1)

        section_hint = QLabel(
            "Select 2+ Candidate Plough Sections or 2+ Plough Skips to merge. "
            "Use Split / insert to create an explicit opposite section with "
            "two adjustable PLDN/PLUP boundaries.")
        section_hint.setWordWrap(True)
        section_hint.setStyleSheet("color: #666;")
        sections_layout.addWidget(section_hint)
        section_buttons = QHBoxLayout()
        for label, slot in (("Split / insert opposite…", self._split_section),
                            ("Merge selected sections", self._merge_sections),
                            ("Set conclusion…", self._set_conclusion),
                            ("Set confidence…", self._set_confidence),
                            ("Mark final", lambda: self._set_state(schema.SECTION_STATE_FINAL)),
                            ("Mark candidate", lambda: self._set_state(schema.SECTION_STATE_CANDIDATE))):
            button = QPushButton(label)
            button.clicked.connect(slot)
            section_buttons.addWidget(button)
        section_buttons.addStretch(1)
        sections_layout.addLayout(section_buttons)
        splitter.addWidget(sections_widget)
        layout.addWidget(splitter, 1)

        model.planChanged.connect(self.refresh)
        model.eventsChanged.connect(self.refresh)
        model.sectionsChanged.connect(self._refresh_sections)
        model.logChanged.connect(self._refresh_undo_state)
        self.refresh()

    # -- progress hooks (driven by the dock) ----------------------------------
    def analysis_started(self) -> None:
        self.generate_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)

    def analysis_progress(self, pct: float) -> None:
        self.progress.setValue(int(pct))

    def analysis_message(self, message: str) -> None:
        self.run_status.setText(message)

    def analysis_finished(self, message: str = "") -> None:
        self.generate_button.setEnabled(bool(self.model.plan))
        self.cancel_button.setEnabled(False)
        self.progress.setVisible(False)
        if message:
            self.run_status.setText(message)

    def show_diff(self, summary: Dict, proposal_diff: Optional[Dict]) -> None:
        parts = [
            f"Burial {summary.get('burial_km', 0):.3f} km "
            f"({summary.get('burial_pct', 0):.0f}%)",
            f"skips {summary.get('skip_km', 0):.3f} km",
            f"insufficient {summary.get('insufficient_km', 0):.3f} km",
            f"{summary.get('event_count', 0)} events",
        ]
        if summary.get("conflict_count"):
            parts.append(f"{summary['conflict_count']} conflict(s)")
        text = "Generated: " + ", ".join(parts) + "."
        if proposal_diff:
            text += (f"  Vs client proposal: {len(proposal_diff.get('added') or [])} added, "
                     f"{len(proposal_diff.get('removed') or [])} removed, "
                     f"{len(proposal_diff.get('moved') or [])} moved.")
        self.diff_label.setText(text)

    # -- refresh ---------------------------------------------------------------
    def refresh(self) -> None:
        self._loading = True
        try:
            method = self.model.method
            self.add_type_combo.clear()
            for event_type in (schema.EVENT_BURIAL_START, schema.EVENT_BURIAL_END):
                self.add_type_combo.addItem(ev.event_label(event_type, method), event_type)
            self.generate_button.setEnabled(bool(self.model.plan))

            events = self.model.events
            self.events_table.setRowCount(len(events))
            for i, event in enumerate(events):
                values = [
                    str(int(event.get("seq") or 0)),
                    ev.event_label(event.get("event_type") or "", method),
                    schema.format_kp(event.get("kp")),
                    f"{event.get('lat'):.7f}" if event.get("lat") is not None else "",
                    f"{event.get('lon'):.7f}" if event.get("lon") is not None else "",
                    f"{event.get('depth_m'):.1f}" if event.get("depth_m") is not None else "",
                    event.get("source") or "",
                    event.get("status") or "",
                    "🔒" if int(event.get("locked") or 0) else "",
                    event.get("notes") or "",
                ]
                for j, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    if j in (2, 9):  # KP and notes are editable
                        flags |= Qt.ItemFlag.ItemIsEditable
                    item.setFlags(flags)
                    if j == 0:
                        item.setData(ITEM_DATA_USER_ROLE, event.get("event_id"))
                    if j == 7:
                        color = _STATUS_COLORS.get(event.get("status") or "")
                        if color is not None:
                            item.setForeground(QBrush(color))
                    self.events_table.setItem(i, j, item)
        finally:
            self._loading = False
        self._refresh_sections()
        self._refresh_undo_state()

    def _refresh_undo_state(self) -> None:
        entry = self.model.last_undoable_builder_change()
        self.undo_button.setEnabled(entry is not None)
        if entry is not None:
            action = (entry.get("action") or "edit").replace("_", " ")
            self.undo_button.setToolTip(
                f"Undo last edit: {action} (Ctrl+Z). The undo remains in the audit log.")
        else:
            self.undo_button.setToolTip("There is no current Plan Builder edit to undo.")

    def _refresh_sections(self) -> None:
        self._loading = True
        try:
            sections = self.model.sections
            self.sections_table.setRowCount(len(sections))
            for i, section in enumerate(sections):
                reasons = self._reason_text(section)
                values = [
                    self._kind_label(section.get("kind") or ""),
                    schema.format_kp(section.get("start_kp")),
                    schema.format_kp(section.get("end_kp")),
                    schema.format_kp(section.get("length_km")),
                    section.get("state") or "",
                    schema.CONCLUSION_LABELS.get(section.get("conclusion") or "", ""),
                    section.get("confidence") or "",
                    reasons,
                    section.get("notes") or "",
                ]
                for j, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    if j == 8:
                        flags |= Qt.ItemFlag.ItemIsEditable
                    item.setFlags(flags)
                    if j == 0:
                        item.setData(ITEM_DATA_USER_ROLE, section.get("section_id"))
                    if j == 7:
                        item.setToolTip(reasons)
                    self.sections_table.setItem(i, j, item)
        finally:
            self._loading = False

    def _kind_label(self, kind: str) -> str:
        plough = self.model.method == schema.METHOD_PLOUGH
        if kind == schema.SECTION_BURIAL:
            return "Candidate Plough Section" if plough else "Burial section"
        if kind == schema.SECTION_SKIP:
            return "Plough Skip" if plough else "Skip"
        return "Insufficient Information"

    def _reason_text(self, section: Dict) -> str:
        try:
            reason = json.loads(section.get("reason_json") or "{}")
        except (ValueError, TypeError):
            return ""
        parts: List[str] = []
        if reason.get("dominant_rule"):
            parts.append(f"Excluded by {reason['dominant_rule']}")
        elif reason.get("fired_rules"):
            parts.append("Excluded by " + ", ".join(reason["fired_rules"][:3]))
        if reason.get("below_min_length"):
            parts.append("below minimum section length")
        if reason.get("insufficient_information"):
            parts.append("Insufficient Information")
        if reason.get("manual"):
            parts.append("manual")
        if reason.get("dangling_start"):
            parts.append("no end event before scope end")
        for conflict in reason.get("exclusion_conflicts") or []:
            rules = ", ".join(conflict.get("rules") or []) or "configured rule"
            parts.append(
                f"Manual burial overlaps Exclusion Area ({rules}) at KP "
                f"{schema.format_kp(conflict.get('start_kp'))}-"
                f"{schema.format_kp(conflict.get('end_kp'))}")
        for entry in reason.get("screening") or []:
            parts.append(f"Screening: {entry.get('rule')} "
                         f"KP {schema.format_kp(entry.get('start_kp'))}-"
                         f"{schema.format_kp(entry.get('end_kp'))}")
        for flag in reason.get("influence_flags") or []:
            parts.append(flag.get("message") or "")
        return "; ".join(p for p in parts if p)

    # -- selections ------------------------------------------------------------
    def _selected_event_ids(self) -> List[str]:
        ids = []
        for index in self.events_table.selectionModel().selectedRows():
            item = self.events_table.item(index.row(), 0)
            if item is not None:
                ids.append(item.data(ITEM_DATA_USER_ROLE))
        return [i for i in ids if i]

    def _selected_section_ids(self) -> List[str]:
        ids = []
        for index in self.sections_table.selectionModel().selectedRows():
            item = self.sections_table.item(index.row(), 0)
            if item is not None:
                ids.append(item.data(ITEM_DATA_USER_ROLE))
        return [i for i in ids if i]

    def _on_event_selected(self) -> None:
        ids = self._selected_event_ids()
        if len(ids) == 1:
            event = next((e for e in self.model.events
                          if e.get("event_id") == ids[0]), None)
            if event is not None:
                self.dock.highlight_kp(float(event.get("kp") or 0.0))

    def _on_section_selected(self) -> None:
        ids = self._selected_section_ids()
        if len(ids) == 1:
            section = next((s for s in self.model.sections
                            if s.get("section_id") == ids[0]), None)
            if section is not None:
                self.dock.highlight_range(float(section.get("start_kp") or 0.0),
                                          float(section.get("end_kp") or 0.0))

    def _context_row(self, table: QTableWidget, position) -> int:
        item = table.itemAt(position)
        if item is None:
            return -1
        row = item.row()
        selected_rows = {index.row() for index in
                         table.selectionModel().selectedRows()}
        if row not in selected_rows:
            table.clearSelection()
            table.selectRow(row)
        return row

    def _event_for_row(self, row: int) -> Optional[Dict]:
        item = self.events_table.item(row, 0)
        event_id = item.data(ITEM_DATA_USER_ROLE) if item else ""
        return next((event for event in self.model.events
                     if event.get("event_id") == event_id), None)

    def _section_for_row(self, row: int) -> Optional[Dict]:
        item = self.sections_table.item(row, 0)
        section_id = item.data(ITEM_DATA_USER_ROLE) if item else ""
        return next((section for section in self.model.sections
                     if section.get("section_id") == section_id), None)

    def _goto_event_row(self, row: int) -> None:
        event = self._event_for_row(row)
        if event is not None:
            self.dock.goto_kp(float(event.get("kp") or 0.0))

    def _goto_section_row(self, row: int) -> None:
        section = self._section_for_row(row)
        if section is not None:
            self.dock.goto_range(float(section.get("start_kp") or 0.0),
                                 float(section.get("end_kp") or 0.0))

    def _event_context_menu(self, position) -> None:
        row = self._context_row(self.events_table, position)
        event = self._event_for_row(row) if row >= 0 else None
        if event is None:
            return
        menu = QMenu(self)
        go_action = menu.addAction(
            f"Go to {ev.event_label(event.get('event_type') or '', self.model.method)} "
            f"at KP {schema.format_kp(event.get('kp'))}")
        scope_action = menu.addAction("Show full plan scope")
        chosen = qt_exec(menu, self.events_table.viewport().mapToGlobal(position))
        if chosen == go_action:
            self._goto_event_row(row)
        elif chosen == scope_action:
            self.dock.show_plan_scope()

    def _section_context_menu(self, position) -> None:
        row = self._context_row(self.sections_table, position)
        section = self._section_for_row(row) if row >= 0 else None
        if section is None:
            return
        menu = QMenu(self)
        go_action = menu.addAction("Go to section on map and profile")
        start_action = menu.addAction(
            f"Go to start KP {schema.format_kp(section.get('start_kp'))}")
        end_action = menu.addAction(
            f"Go to end KP {schema.format_kp(section.get('end_kp'))}")
        scope_action = menu.addAction("Show full plan scope")
        menu.addSeparator()
        split_action = menu.addAction("Split / insert opposite section…")
        split_action.setEnabled(len(self._selected_section_ids()) == 1)
        merge_action = menu.addAction("Merge selected sections…")
        merge_action.setEnabled(len(self._selected_section_ids()) >= 2)
        chosen = qt_exec(menu, self.sections_table.viewport().mapToGlobal(position))
        if chosen == go_action:
            self._goto_section_row(row)
        elif chosen == start_action:
            self.dock.goto_kp(float(section.get("start_kp") or 0.0))
        elif chosen == end_action:
            self.dock.goto_kp(float(section.get("end_kp") or 0.0))
        elif chosen == scope_action:
            self.dock.show_plan_scope()
        elif chosen == split_action:
            self._split_section()
        elif chosen == merge_action:
            self._merge_sections()

    # -- event edits -----------------------------------------------------------
    def _on_event_item_changed(self, item) -> None:
        if self._loading:
            return
        row = item.row()
        id_item = self.events_table.item(row, 0)
        event_id = id_item.data(ITEM_DATA_USER_ROLE) if id_item else ""
        if not event_id:
            return
        if item.column() == 2:  # KP
            try:
                new_kp = float(item.text())
            except ValueError:
                self.refresh()
                return
            self._try_move(event_id, new_kp)
        elif item.column() == 9:  # notes
            self.model.set_event_notes(event_id, item.text())

    def _try_move(self, event_id: str, new_kp: float) -> None:
        reason = self._maybe_reason("Move event")
        if reason is None:
            self.refresh()
            return
        try:
            self.model.move_event(event_id, new_kp, reason)
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))
            self.refresh()

    def _maybe_reason(self, title: str) -> Optional[str]:
        """Optional reason prompt on manual event edits (Cancel aborts)."""
        text, ok = QInputDialog.getText(
            self, title, "Reason (optional):", QLineEdit.EchoMode.Normal, "")
        if not ok:
            return None
        return text

    def set_add_kp(self, kp: float) -> None:
        """Prime the add-event KP (map pick / profile double-click)."""
        self.add_kp_spin.setValue(round(float(kp), 3))
        self.run_status.setText(
            f"Add-event KP set to {schema.format_kp(kp)} — choose the event "
            "type and click Add event.")

    def _pick_add_kp(self) -> None:
        self.dock.pick_kp_on_map(
            self.set_add_kp,
            "Click the route to set the add-event KP (right-click cancels).")

    def _add_event(self) -> None:
        if not self.model.plan:
            return
        reason = self._maybe_reason("Add event")
        if reason is None:
            return
        try:
            self.model.add_event(self.add_kp_spin.value(),
                                 self.add_type_combo.currentData(), reason=reason)
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))

    def _nudge(self, sign: int) -> None:
        ids = self._selected_event_ids()
        if len(ids) != 1:
            return
        event = next((e for e in self.model.events if e.get("event_id") == ids[0]), None)
        if event is None or int(event.get("locked") or 0):
            return
        delta_km = sign * self.nudge_spin.value() / 1000.0
        try:
            self.model.move_event(ids[0], float(event.get("kp") or 0.0) + delta_km,
                                  f"nudge {sign * self.nudge_spin.value():+.0f} m")
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))

    def _confirm_selected(self) -> None:
        ids = self._selected_event_ids()
        if ids:
            self.model.set_event_status(ids, schema.EVENT_STATUS_CONFIRMED)

    def _confirm_all(self) -> None:
        ids = [e.get("event_id") for e in self.model.events
               if e.get("status") == schema.EVENT_STATUS_CANDIDATE]
        if ids:
            self.model.set_event_status(ids, schema.EVENT_STATUS_CONFIRMED)

    def _lock_selected(self, locked: bool) -> None:
        ids = self._selected_event_ids()
        if ids:
            self.model.set_event_locked(ids, locked)

    def _delete_selected(self) -> None:
        ids = self._selected_event_ids()
        if not ids:
            return
        locked = [i for i in ids for e in self.model.events
                  if e.get("event_id") == i and int(e.get("locked") or 0)]
        if locked:
            QMessageBox.warning(self, "Burial Planner",
                                "Locked events cannot be deleted — unlock them first.")
            return
        answer = QMessageBox.question(
            self, "Delete events", f"Delete {len(ids)} event(s)?",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer != MESSAGE_BOX_YES:
            return
        try:
            self.model.delete_events(ids)
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))

    def _undo_last_edit(self) -> None:
        entry = self.model.undo_last_builder_edit()
        if entry is None:
            self.run_status.setText("There is no Plan Builder edit to undo.")
            return
        action = (entry.get("action") or "edit").replace("_", " ")
        self.run_status.setText(f"Undid: {action}.")

    # -- section edits ---------------------------------------------------------
    def _on_section_item_changed(self, item) -> None:
        if self._loading or item.column() != 8:
            return
        id_item = self.sections_table.item(item.row(), 0)
        section_id = id_item.data(ITEM_DATA_USER_ROLE) if id_item else ""
        if section_id:
            from .. import change_log

            self.model.update_section(section_id, {"notes": item.text()},
                                      action=change_log.ACTION_EDIT_SECTION)

    def _split_section(self) -> None:
        ids = self._selected_section_ids()
        if len(ids) != 1:
            QMessageBox.information(
                self, "Burial Planner",
                "Select one Candidate Plough Section or one Plough Skip.")
            return
        section = next((s for s in self.model.sections
                        if s.get("section_id") == ids[0]), None)
        if section is None:
            return
        if section.get("kind") == schema.SECTION_INSUFFICIENT:
            QMessageBox.warning(
                self, "Burial Planner",
                "Insufficient Information sections cannot be manually split.")
            return
        inserted_label = ("a Plough Skip" if section.get("kind") == schema.SECTION_BURIAL
                          else "a Candidate Plough Section")
        dialog = SectionRangeDialog(section, inserted_label, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        start_kp, end_kp = dialog.range_kp()
        reason = self._maybe_reason("Split section / insert range")
        if reason is None:
            return
        try:
            self.model.insert_opposite_section(
                ids[0], start_kp, end_kp, reason)
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))

    def _merge_sections(self) -> None:
        ids = self._selected_section_ids()
        selected = [section for section in self.model.sections
                    if section.get("section_id") in set(ids)]
        if len(selected) < 2:
            QMessageBox.information(
                self, "Burial Planner",
                "Select at least two Candidate Plough Sections or two Plough Skips.")
            return
        kinds = {section.get("kind") for section in selected}
        if len(kinds) != 1:
            QMessageBox.warning(
                self, "Burial Planner", "Selected sections must be the same kind.")
            return
        kind_label = self._kind_label(next(iter(kinds)) or "")
        answer = QMessageBox.question(
            self, "Merge sections",
            f"Merge {len(selected)} selected {kind_label} rows? The intervening "
            "PLDN/PLUP boundaries will be removed; the outer boundaries remain "
            "available for dragging or KP editing.",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer != MESSAGE_BOX_YES:
            return
        reason = self._maybe_reason("Merge sections")
        if reason is None:
            return
        try:
            self.model.merge_sections(ids, reason)
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))

    def _set_conclusion(self) -> None:
        ids = self._selected_section_ids()
        if not ids:
            return
        labels = [schema.CONCLUSION_LABELS[c] for c in [""] + schema.CONCLUSIONS]
        label, ok = QInputDialog.getItem(self, "Conclusion",
                                         "Operating-envelope conclusion:", labels, 0, False)
        if not ok:
            return
        value = next((k for k, v in schema.CONCLUSION_LABELS.items() if v == label), "")
        for section_id in ids:
            self.model.update_section(section_id, {"conclusion": value})

    def _set_confidence(self) -> None:
        ids = self._selected_section_ids()
        if not ids:
            return
        value, ok = QInputDialog.getItem(self, "Confidence", "Evidence confidence:",
                                         [""] + schema.CONFIDENCE_VALUES, 0, False)
        if not ok:
            return
        for section_id in ids:
            self.model.update_section(section_id, {"confidence": value})

    def _set_state(self, state: str) -> None:
        from .. import change_log

        for section_id in self._selected_section_ids():
            self.model.update_section(section_id, {"state": state},
                                      action=change_log.ACTION_EDIT_SECTION)
