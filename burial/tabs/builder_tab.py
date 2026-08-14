# -*- coding: utf-8 -*-
"""Plan Builder tab — generate, then refine events and sections.

Regeneration contract (spec §12.7): auto candidate events are disposable;
locked and confirmed events persist (conflict-flagged if newly inside an
Exclusion Area); nothing user-made is silently deleted or moved.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QKeySequence
from qgis.PyQt.QtWidgets import (
    QCheckBox,
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
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
    qt_exec,
)
from .. import events as ev
from .. import schema
from .. import tools as tools_mod

_EVENT_COLUMNS = ["Seq", "Event", "KP", "Lat", "Lon", "Depth (m)", "Source",
                  "Status", "Locked", "Notes"]
_SECTION_COLUMNS = ["ID", "Kind", "Start KP", "End KP", "Length (km)",
                    "State", "Conclusion", "Confidence", "Tool",
                    "Tool config", "Skip handling", "Reasons", "Notes"]
# Derived from the header list so reordering/inserting columns cannot
# silently desynchronise a widget from the field it edits.
_SECTION_CONCLUSION_COL = _SECTION_COLUMNS.index("Conclusion")
_SECTION_CONFIDENCE_COL = _SECTION_COLUMNS.index("Confidence")
_SECTION_TOOL_COL = _SECTION_COLUMNS.index("Tool")
_SECTION_TOOL_CONFIG_COL = _SECTION_COLUMNS.index("Tool config")
_SECTION_SKIP_HANDLING_COL = _SECTION_COLUMNS.index("Skip handling")
_SECTION_REASONS_COL = _SECTION_COLUMNS.index("Reasons")
_SECTION_NOTES_COL = _SECTION_COLUMNS.index("Notes")

_SHOW_EVENTS_SETTINGS_KEY = "SubseaCableTools/BurialPlanner/builder_show_events"

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
            "boundary events will be created at the entered KPs.")
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
        run_row.addWidget(QLabel("Minimum candidate section:"))
        self.min_section_spin = QDoubleSpinBox()
        self.min_section_spin.setRange(0.0, 1000.0)
        self.min_section_spin.setDecimals(3)
        self.min_section_spin.setSuffix(" km")
        self.min_section_spin.setToolTip(
            "Do not create an automatic candidate burial section shorter "
            "than this operational minimum. Exclusion and insufficient-data "
            "ranges are never removed by this setting. Set 0 to keep every "
            "candidate section.")
        run_row.addWidget(self.min_section_spin)
        self.generate_button = QPushButton("Generate plan")
        self.generate_button.setToolTip(
            "Run the Exclusion stack over the scope and rebuild candidate "
            "sections and events in the background. Locked, confirmed and "
            "manual events are kept (flagged if now inside an Exclusion "
            "Area); conclusions, notes and skip handling carry over for "
            "unchanged sections. Only automatic candidates are replaced.")
        self.generate_button.clicked.connect(self._generate)
        run_row.addWidget(self.generate_button)
        self.fresh_button = QPushButton("Regenerate fresh…")
        self.fresh_button.setToolTip(
            "Discard manual edits and rebuild the plan purely from the "
            "Exclusion stack. Asks for confirmation, lists what will be "
            "discarded, and records the previous state in the change log "
            "so it can be rolled back from Review && Export.")
        self.fresh_button.clicked.connect(self._regenerate_fresh)
        run_row.addWidget(self.fresh_button)
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
        self.show_events_check = QCheckBox("Show events")
        self.show_events_check.setToolTip(
            "Show the burial start/end event list alongside the Sections "
            "table. "
            "Sections are the primary working view; open the event list to "
            "add, nudge, confirm, lock or delete individual boundary "
            "events. The setting is remembered.")
        self.show_events_check.setChecked(
            QSettings().value(_SHOW_EVENTS_SETTINGS_KEY, False, type=bool))
        self.show_events_check.toggled.connect(self._set_events_visible)
        run_row.addWidget(self.show_events_check)
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

        self.events_widget = QWidget()
        events_widget = self.events_widget
        events_layout = QVBoxLayout(events_widget)
        events_layout.setContentsMargins(0, 0, 0, 0)
        events_layout.addWidget(QLabel("Events"))
        self.events_table = QTableWidget(0, len(_EVENT_COLUMNS))
        self.events_table.setHorizontalHeaderLabels(_EVENT_COLUMNS)
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.events_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.events_table.horizontalHeader().setStretchLastSection(True)
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
        confirm_all_button = QPushButton("Confirm all")
        confirm_all_button.setToolTip(
            "Confirm every candidate event. Confirm, lock, unlock and "
            "delete for a selection are on the row's right-click menu.")
        confirm_all_button.clicked.connect(self._confirm_all)
        event_buttons.addWidget(confirm_all_button)
        event_buttons.addStretch(1)
        events_layout.addLayout(event_buttons)

        sections_widget = QWidget()
        sections_layout = QVBoxLayout(sections_widget)
        sections_layout.setContentsMargins(0, 0, 0, 0)
        sections_layout.addWidget(QLabel("Sections"))
        self.sections_table = QTableWidget(0, len(_SECTION_COLUMNS))
        self.sections_table.setHorizontalHeaderLabels(_SECTION_COLUMNS)
        self.sections_table.verticalHeader().setVisible(False)
        self.sections_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.sections_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        # Every column stays user-resizable (Stretch mode would lock it);
        # the trailing Notes column absorbs the remaining width.
        self.sections_table.horizontalHeader().setStretchLastSection(True)
        self.sections_table.setColumnWidth(_SECTION_REASONS_COL, 240)
        self.sections_table.itemSelectionChanged.connect(self._on_section_selected)
        self.sections_table.itemChanged.connect(self._on_section_item_changed)
        self.sections_table.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        self.sections_table.customContextMenuRequested.connect(
            self._section_context_menu)
        self.sections_table.cellDoubleClicked.connect(
            lambda row, _column: self._goto_section_row(row))
        sections_layout.addWidget(self.sections_table, 1)

        section_hint = QLabel(
            "Set conclusion and confidence in the table (or right-click a "
            "selection to set several at once, mark final, split or merge). "
            "Select 2+ sections of the same kind to merge.")
        section_hint.setWordWrap(True)
        section_hint.setStyleSheet("color: #666;")
        sections_layout.addWidget(section_hint)
        section_buttons = QHBoxLayout()
        for label, slot in (("Split / insert opposite…", self._split_section),
                            ("Merge selected sections", self._merge_sections),
                            ("Auto-assign skip handling…", self._auto_assign_skip_handling)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            section_buttons.addWidget(button)
        section_buttons.addStretch(1)
        sections_layout.addLayout(section_buttons)

        splitter.addWidget(sections_widget)
        splitter.addWidget(events_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)
        events_widget.setVisible(self.show_events_check.isChecked())

        model.planChanged.connect(self.refresh)
        model.eventsChanged.connect(self.refresh)
        model.sectionsChanged.connect(self._refresh_sections)
        model.toolsChanged.connect(self._refresh_sections)
        model.logChanged.connect(self._refresh_undo_state)
        self.refresh()

    def _set_events_visible(self, visible: bool) -> None:
        QSettings().setValue(_SHOW_EVENTS_SETTINGS_KEY, bool(visible))
        self.events_widget.setVisible(bool(visible))

    # -- progress hooks (driven by the dock) ----------------------------------
    def analysis_started(self) -> None:
        self.generate_button.setEnabled(False)
        self.fresh_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)

    def analysis_progress(self, pct: float) -> None:
        self.progress.setValue(int(pct))

    def analysis_message(self, message: str) -> None:
        self.run_status.setText(message)

    def analysis_finished(self, message: str = "") -> None:
        self.generate_button.setEnabled(bool(self.model.plan))
        self.fresh_button.setEnabled(bool(self.model.plan))
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
            self.min_section_spin.setValue(
                self.model.gen_params().min_section_km)
            self.add_type_combo.clear()
            for event_type in (schema.EVENT_BURIAL_START, schema.EVENT_BURIAL_END):
                self.add_type_combo.addItem(ev.event_label(event_type, method), event_type)
            self.generate_button.setEnabled(bool(self.model.plan))
            self.fresh_button.setEnabled(bool(self.model.plan))

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

    def _generate(self) -> None:
        """Save the candidate-section policy, then generate the plan."""
        if not self.model.plan:
            return
        params = self.model.gen_params()
        wanted = self.min_section_spin.value()
        if abs(params.min_section_km - wanted) > 1e-9:
            if not self.model.update_gen_params(
                    {"min_section_km": wanted},
                    reason="minimum candidate section"):
                return
        self.dock.request_generation()

    def _regenerate_fresh(self) -> None:
        """Confirmed rebuild from the Exclusion stack alone.

        Lists exactly what will be discarded before doing anything; the
        previous state lands in the change log as part of the generation
        entry, so the action is destructive-looking but rollback-able.
        """
        if not self.model.plan:
            return
        events = self.model.events
        manual = sum(1 for e in events
                     if e.get("source") in (schema.EVENT_SOURCE_MANUAL,
                                            schema.EVENT_SOURCE_IMPORT))
        confirmed = sum(1 for e in events
                        if e.get("status") == schema.EVENT_STATUS_CONFIRMED)
        locked = sum(1 for e in events if int(e.get("locked") or 0))
        client = sum(1 for e in events
                     if e.get("source") == schema.EVENT_SOURCE_CLIENT)
        curated = sum(1 for s in self.model.sections
                      if s.get("conclusion") or s.get("confidence")
                      or s.get("notes") or s.get("skip_handling")
                      or s.get("state") == schema.SECTION_STATE_FINAL)
        parts = []
        if manual:
            parts.append(f"{manual} manual/imported event(s)")
        if confirmed:
            parts.append(f"{confirmed} confirmation(s)")
        if locked:
            parts.append(f"{locked} lock(s)")
        if curated:
            parts.append(f"{curated} section(s) with conclusions, "
                         "confidence, notes, skip handling or final state")
        summary = ("This will discard " + ", ".join(parts) + "."
                   if parts else
                   "No manual changes were found — this simply rebuilds the "
                   "plan from the Exclusion stack.")

        dialog = QDialog(self)
        dialog.setWindowTitle("Regenerate fresh")
        layout = QVBoxLayout(dialog)
        note = QLabel(
            f"Rebuild the plan purely from the Exclusion stack?\n\n{summary}"
            "\n\nThe previous state is recorded in the change log and can "
            "be restored from Review & Export → Rollback.")
        note.setWordWrap(True)
        layout.addWidget(note)
        client_check = None
        if client:
            client_check = QCheckBox(
                f"Also remove the {client} client burial-proposal event(s) "
                "(otherwise kept as reference)")
            layout.addWidget(client_check)
        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        params = self.model.gen_params()
        wanted = self.min_section_spin.value()
        if abs(params.min_section_km - wanted) > 1e-9:
            if not self.model.update_gen_params(
                    {"min_section_km": wanted},
                    reason="minimum candidate section"):
                return
        keep_client = not (client_check is not None
                           and client_check.isChecked())
        self.dock.request_generation(fresh=True, keep_client=keep_client)

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
            refs = schema.section_refs(sections, self.model.direction,
                                       self.model.method)
            ref_legend = schema.section_ref_legend(self.model.method)
            self.sections_table.setRowCount(len(sections))
            for i, section in enumerate(sections):
                reasons = self._reason_text(section)
                section_id = section.get("section_id")
                is_skip = section.get("kind") == schema.SECTION_SKIP
                # Insufficient Information rows keep their fixed conclusion
                # as plain text; burial and skip rows edit in-table.
                editable = section.get("kind") in (schema.SECTION_BURIAL,
                                                   schema.SECTION_SKIP)
                is_burial = section.get("kind") == schema.SECTION_BURIAL
                values = [
                    refs.get(str(section_id or ""), ""),
                    self._kind_label(section.get("kind") or ""),
                    schema.format_kp(section.get("start_kp")),
                    schema.format_kp(section.get("end_kp")),
                    schema.format_kp(section.get("length_km")),
                    section.get("state") or "",
                    "" if editable else schema.CONCLUSION_LABELS.get(
                        section.get("conclusion") or "", ""),
                    "" if editable else (section.get("confidence") or ""),
                    "",  # tool: combo widget on burial rows
                    "",  # tool config: combo widget on burial rows
                    "",  # skip handling: combo widget on skip rows
                    reasons,
                    section.get("notes") or "",
                ]
                for j, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    if j == _SECTION_NOTES_COL:
                        flags |= Qt.ItemFlag.ItemIsEditable
                    item.setFlags(flags)
                    if j == 0:
                        item.setData(ITEM_DATA_USER_ROLE, section_id)
                        item.setToolTip(ref_legend)
                    if j == _SECTION_REASONS_COL:
                        item.setToolTip(reasons)
                    if j == _SECTION_NOTES_COL and value:
                        item.setToolTip(value)
                    self.sections_table.setItem(i, j, item)
                if editable:
                    self._add_section_combo(
                        i, _SECTION_CONCLUSION_COL, section_id, "conclusion",
                        [(c, schema.CONCLUSION_LABELS[c])
                         for c in [""] + schema.CONCLUSIONS],
                        section.get("conclusion") or "",
                        "Operating-envelope conclusion. Right-click a "
                        "selection to set several sections at once.")
                    self._add_section_combo(
                        i, _SECTION_CONFIDENCE_COL, section_id, "confidence",
                        [("", "")] + [(v, v) for v in schema.CONFIDENCE_VALUES],
                        section.get("confidence") or "",
                        "Evidence confidence. Right-click a selection to "
                        "set several sections at once.")
                else:
                    self.sections_table.removeCellWidget(
                        i, _SECTION_CONCLUSION_COL)
                    self.sections_table.removeCellWidget(
                        i, _SECTION_CONFIDENCE_COL)
                if is_burial:
                    self._add_tool_combos(i, section)
                else:
                    self.sections_table.removeCellWidget(i, _SECTION_TOOL_COL)
                    self.sections_table.removeCellWidget(
                        i, _SECTION_TOOL_CONFIG_COL)
                if is_skip:
                    self._add_section_combo(
                        i, _SECTION_SKIP_HANDLING_COL, section_id,
                        "skip_handling",
                        [(h, schema.SKIP_HANDLING_LABELS[h])
                         for h in schema.SKIP_HANDLING_VALUES],
                        section.get("skip_handling") or "",
                        "How this skip is executed: recover the burial tool "
                        "to deck, or transit with the tool suspended "
                        "mid-water. TBC until decided.")
                else:
                    self.sections_table.removeCellWidget(
                        i, _SECTION_SKIP_HANDLING_COL)
        finally:
            self._loading = False

    def _add_section_combo(self, row: int, column: int, section_id: str,
                           field: str, options, current: str,
                           tooltip: str = "") -> None:
        combo = QComboBox()
        for value, label in options:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(current or "")))
        if tooltip:
            combo.setToolTip(tooltip)
        combo.currentIndexChanged.connect(
            lambda _index, c=combo, sid=section_id, f=field:
            self._deferred_section_edit(sid, f, c.currentData()))
        self.sections_table.setCellWidget(row, column, combo)

    def _add_tool_combos(self, row: int, section: Dict) -> None:
        """Tool + configuration combos on a burial row.

        "" = inherit the plan default (shown with the resolved name). An
        explicit assignment also stamps the section's ``method`` with the
        tool's type; generation still resolves rules against the plan
        method until mixed-method generation lands.
        """
        section_id = section.get("section_id") or ""
        default_tool_id, default_config_id = self.model.default_tool()
        default_text = tools_mod.tool_display(self.model.tools,
                                              default_tool_id)
        inherit_label = (f"Plan default ({default_text})"
                         if default_text else "Plan default (none)")

        tool_options = [("", inherit_label)]
        for tool in self.model.tools:
            tool_options.append((tool.get("tool_id") or "",
                                 tool.get("name") or "?"))
        current_tool = str(section.get("tool_id") or "")
        if current_tool and not tools_mod.tool_by_id(self.model.tools,
                                                     current_tool):
            tool_options.append((current_tool, "(unregistered tool)"))
        self._add_section_combo(
            row, _SECTION_TOOL_COL, section_id, "tool_id", tool_options,
            current_tool,
            "Burial tool for this section; blank inherits the plan "
            "default set on the Plan tab. Register tools on the "
            "Burial Tools tab.")

        config_tool_id = current_tool or default_tool_id
        config_tool = tools_mod.tool_by_id(self.model.tools, config_tool_id)
        config_options = [("", "(default)" if current_tool == ""
                           else "(no configuration)")]
        for config in tools_mod.parse_configs(config_tool):
            config_options.append((config.get("config_id") or "",
                                   tools_mod.config_label(config) or "?"))
        current_config = str(section.get("tool_config_id") or "")
        if current_config and all(value != current_config
                                  for value, _label in config_options):
            config_options.append((current_config,
                                   "(unknown configuration)"))
        self._add_section_combo(
            row, _SECTION_TOOL_CONFIG_COL, section_id, "tool_config_id",
            config_options, current_config,
            "Operating configuration (e.g. jetting vs passive mode) of "
            "the section's tool.")

    def _kind_label(self, kind: str) -> str:
        return schema.section_kind_label(kind, self.model.method)

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
        count = len(self._selected_event_ids())
        menu = QMenu(self)
        go_action = menu.addAction(
            f"Go to {ev.event_label(event.get('event_type') or '', self.model.method)} "
            f"at KP {schema.format_kp(event.get('kp'))}")
        scope_action = menu.addAction("Show full plan scope")
        menu.addSeparator()
        confirm_action = menu.addAction(f"Confirm {count} event(s)")
        lock_action = menu.addAction(f"Lock {count} event(s)")
        unlock_action = menu.addAction(f"Unlock {count} event(s)")
        delete_action = menu.addAction(f"Delete {count} event(s)…")
        chosen = qt_exec(menu, self.events_table.viewport().mapToGlobal(position))
        if chosen == go_action:
            self._goto_event_row(row)
        elif chosen == scope_action:
            self.dock.show_plan_scope()
        elif chosen == confirm_action:
            self._confirm_selected()
        elif chosen == lock_action:
            self._lock_selected(True)
        elif chosen == unlock_action:
            self._lock_selected(False)
        elif chosen == delete_action:
            self._delete_selected()

    def _section_context_menu(self, position) -> None:
        row = self._context_row(self.sections_table, position)
        section = self._section_for_row(row) if row >= 0 else None
        if section is None:
            return
        selected_count = len(self._selected_section_ids())
        suffix = f" ({selected_count} selected)" if selected_count > 1 else ""
        menu = QMenu(self)
        go_action = menu.addAction("Go to section on map and profile")
        start_action = menu.addAction(
            f"Go to start KP {schema.format_kp(section.get('start_kp'))}")
        end_action = menu.addAction(
            f"Go to end KP {schema.format_kp(section.get('end_kp'))}")
        scope_action = menu.addAction("Show full plan scope")
        menu.addSeparator()
        conclusion_menu = menu.addMenu(f"Set conclusion{suffix}")
        conclusion_actions = {}
        for value in [""] + schema.CONCLUSIONS:
            label = schema.CONCLUSION_LABELS[value] or "(clear)"
            conclusion_actions[conclusion_menu.addAction(label)] = value
        confidence_menu = menu.addMenu(f"Set confidence{suffix}")
        confidence_actions = {}
        for value in [""] + schema.CONFIDENCE_VALUES:
            confidence_actions[confidence_menu.addAction(value or "(clear)")] = value
        skip_menu = menu.addMenu(f"Set skip handling{suffix}")
        skip_actions = {}
        for value in schema.SKIP_HANDLING_VALUES:
            skip_actions[skip_menu.addAction(
                schema.SKIP_HANDLING_LABELS[value])] = value
        wanted = set(self._selected_section_ids())
        skip_menu.setEnabled(any(
            s.get("kind") == schema.SECTION_SKIP
            for s in self.model.sections if s.get("section_id") in wanted))
        # Tool + configuration in one gesture: top level for tools without
        # configurations, a submenu per tool that has them.
        tool_menu = menu.addMenu(f"Set tool{suffix}")
        tool_actions = {}
        default_text = tools_mod.tool_display(self.model.tools,
                                              self.model.default_tool()[0])
        tool_actions[tool_menu.addAction(
            f"Plan default ({default_text or 'none'})")] = ("", "")
        for tool in self.model.tools:
            name = tool.get("name") or "?"
            tool_id = tool.get("tool_id") or ""
            configs = tools_mod.parse_configs(tool)
            if configs:
                sub = tool_menu.addMenu(name)
                tool_actions[sub.addAction("(no configuration)")] = (tool_id, "")
                for config in configs:
                    tool_actions[sub.addAction(
                        tools_mod.config_label(config) or "?")] = \
                        (tool_id, config.get("config_id") or "")
            else:
                tool_actions[tool_menu.addAction(name)] = (tool_id, "")
        tool_menu.setEnabled(any(
            s.get("kind") == schema.SECTION_BURIAL
            for s in self.model.sections if s.get("section_id") in wanted))
        notes_action = menu.addAction(f"Set notes{suffix}…")
        final_action = menu.addAction(f"Mark final{suffix}")
        candidate_action = menu.addAction(f"Mark candidate{suffix}")
        menu.addSeparator()
        split_action = menu.addAction("Split / insert opposite section…")
        split_action.setEnabled(selected_count == 1)
        merge_action = menu.addAction("Merge selected sections…")
        merge_action.setEnabled(selected_count >= 2)
        chosen = qt_exec(menu, self.sections_table.viewport().mapToGlobal(position))
        if chosen == go_action:
            self._goto_section_row(row)
        elif chosen == start_action:
            self.dock.goto_kp(float(section.get("start_kp") or 0.0))
        elif chosen == end_action:
            self.dock.goto_kp(float(section.get("end_kp") or 0.0))
        elif chosen == scope_action:
            self.dock.show_plan_scope()
        elif chosen in conclusion_actions:
            self._apply_section_field("conclusion", conclusion_actions[chosen])
        elif chosen in confidence_actions:
            self._apply_section_field("confidence", confidence_actions[chosen])
        elif chosen in skip_actions:
            self._apply_section_field("skip_handling", skip_actions[chosen])
        elif chosen in tool_actions:
            self._apply_section_tool(*tool_actions[chosen])
        elif chosen == notes_action:
            text, ok = QInputDialog.getText(
                self, "Set notes",
                f"Notes for the {len(wanted)} selected section(s) "
                "(replaces existing notes):",
                QLineEdit.EchoMode.Normal, section.get("notes") or "")
            if ok:
                self._apply_section_field("notes", text)
        elif chosen == final_action:
            self._apply_section_field("state", schema.SECTION_STATE_FINAL)
        elif chosen == candidate_action:
            self._apply_section_field("state", schema.SECTION_STATE_CANDIDATE)
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
        if not self.show_events_check.isChecked():
            self.show_events_check.setChecked(True)  # reveal the add-event row
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
        if self._loading or item.column() != _SECTION_NOTES_COL:
            return
        id_item = self.sections_table.item(item.row(), 0)
        section_id = id_item.data(ITEM_DATA_USER_ROLE) if id_item else ""
        if section_id:
            from .. import change_log

            self.model.update_section(section_id, {"notes": item.text()},
                                      action=change_log.ACTION_EDIT_SECTION)

    def _deferred_section_edit(self, section_id: str, field: str,
                               value: str) -> None:
        """Apply one in-table combo edit (conclusion/confidence/skip handling).

        Deferred: update_section rebuilds this table, which would destroy
        the combo that is still delivering its change signal.
        """
        if self._loading or not section_id:
            return
        from qgis.PyQt.QtCore import QTimer

        def apply() -> None:
            from .. import change_log

            section = next((s for s in self.model.sections
                            if s.get("section_id") == section_id), None)
            if section is None or (section.get(field) or "") == (value or ""):
                return
            # The tool_id invariant (config reset + method stamp) is
            # enforced inside PlanModel.update_section for every writer.
            action = (change_log.ACTION_SET_CONCLUSION
                      if field in ("conclusion", "confidence")
                      else change_log.ACTION_EDIT_SECTION)
            self.model.update_section(section_id, {field: value or ""},
                                      action=action)

        QTimer.singleShot(0, apply)

    def _split_section(self) -> None:
        ids = self._selected_section_ids()
        if len(ids) != 1:
            method = self.model.method
            QMessageBox.information(
                self, "Burial Planner",
                f"Select one "
                f"{schema.section_kind_label(schema.SECTION_BURIAL, method)} "
                f"or one "
                f"{schema.section_kind_label(schema.SECTION_SKIP, method)}.")
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
        inserted_kind = (schema.SECTION_SKIP
                         if section.get("kind") == schema.SECTION_BURIAL
                         else schema.SECTION_BURIAL)
        inserted_label = \
            f"a {schema.section_kind_label(inserted_kind, self.model.method)}"
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
            method = self.model.method
            QMessageBox.information(
                self, "Burial Planner",
                f"Select at least two "
                f"{schema.section_kind_label(schema.SECTION_BURIAL, method)} "
                f"rows or two "
                f"{schema.section_kind_label(schema.SECTION_SKIP, method)} rows.")
            return
        kinds = {section.get("kind") for section in selected}
        if len(kinds) != 1:
            QMessageBox.warning(
                self, "Burial Planner", "Selected sections must be the same kind.")
            return
        kind_label = self._kind_label(next(iter(kinds)) or "")
        start_label = ev.event_label(schema.EVENT_BURIAL_START, self.model.method)
        end_label = ev.event_label(schema.EVENT_BURIAL_END, self.model.method)
        answer = QMessageBox.question(
            self, "Merge sections",
            f"Merge {len(selected)} selected {kind_label} rows? The intervening "
            f"{start_label}/{end_label} boundaries will be removed; the outer "
            "boundaries remain available for dragging or KP editing.",
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

    def _auto_assign_skip_handling(self) -> None:
        """Length-based skip handling with a user-entered threshold.

        No engineering value is shipped: the mid-water-transit maximum length
        is entered here (remembered per machine) and recorded in the change
        log with the assignment, which is a single undoable edit.
        """
        if not self.model.plan:
            return
        skips = [s for s in self.model.sections
                 if s.get("kind") == schema.SECTION_SKIP]
        if not skips:
            QMessageBox.information(self, "Burial Planner",
                                    "The plan has no skips to assign.")
            return
        settings = QSettings()
        settings_key = "SubseaCableTools/BurialPlanner/skip_transit_max_km"
        dialog = QDialog(self)
        dialog.setWindowTitle("Auto-assign skip handling")
        layout = QVBoxLayout(dialog)
        note = QLabel(
            "Skips no longer than the threshold are set to Mid-water "
            "transit; longer skips to Recover to deck. The threshold is "
            "your operational policy — no default is prescribed. Skips "
            "already assigned keep their handling unless overwrite is "
            "ticked. One undoable edit (Ctrl+Z).")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        threshold_spin = QDoubleSpinBox()
        threshold_spin.setRange(0.0, 1000.0)
        threshold_spin.setDecimals(3)
        threshold_spin.setSuffix(" km")
        threshold_spin.setValue(float(settings.value(settings_key, 0.0,
                                                     type=float)))
        form.addRow("Mid-water transit up to:", threshold_spin)
        overwrite_check = QCheckBox("Overwrite existing assignments (not just TBC)")
        form.addRow(overwrite_check)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        threshold = threshold_spin.value()
        settings.setValue(settings_key, threshold)
        changed = self.model.assign_skip_handling(
            threshold, overwrite=overwrite_check.isChecked())
        if changed < 0:
            return  # store error already surfaced by the model
        if changed == 0:
            self.run_status.setText(
                "No skip handling changed — all skips already assigned "
                "(tick overwrite to reassign).")
        else:
            self.run_status.setText(
                f"Assigned skip handling on {changed} skip(s): mid-water "
                f"transit ≤ {threshold:g} km, recover to deck above.")

    def _apply_section_field(self, field: str, value: str) -> None:
        """Bulk-apply one field to the selected sections (context menu).

        Rows the field does not apply to are skipped: Insufficient
        Information rows keep their fixed conclusion/confidence and skip
        handling only exists on skips. State and notes apply to every
        selected row.
        """
        from .. import change_log

        action = (change_log.ACTION_SET_CONCLUSION
                  if field in ("conclusion", "confidence")
                  else change_log.ACTION_EDIT_SECTION)
        wanted = set(self._selected_section_ids())
        for section in self.model.sections:
            section_id = section.get("section_id")
            if section_id not in wanted:
                continue
            if field in ("conclusion", "confidence") \
                    and section.get("kind") == schema.SECTION_INSUFFICIENT:
                continue
            if field == "skip_handling" \
                    and section.get("kind") != schema.SECTION_SKIP:
                continue
            if (section.get(field) or "") == (value or ""):
                continue
            self.model.update_section(section_id, {field: value}, action=action)

    def _apply_section_tool(self, tool_id: str, config_id: str) -> None:
        """Bulk-assign tool + configuration to the selected burial sections.

        Skip / Insufficient Information rows in the selection are ignored;
        PlanModel.update_section stamps the section method from the tool.
        """
        from .. import change_log

        wanted = set(self._selected_section_ids())
        for section in self.model.sections:
            if section.get("section_id") not in wanted \
                    or section.get("kind") != schema.SECTION_BURIAL:
                continue
            if (section.get("tool_id") or "") == (tool_id or "") \
                    and (section.get("tool_config_id") or "") == (config_id or ""):
                continue
            self.model.update_section(
                section.get("section_id"),
                {"tool_id": tool_id, "tool_config_id": config_id},
                action=change_log.ACTION_EDIT_SECTION)
