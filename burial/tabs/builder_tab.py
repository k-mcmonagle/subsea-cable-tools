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
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
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
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
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
        sections_layout.addWidget(self.sections_table, 1)

        section_buttons = QHBoxLayout()
        for label, slot in (("Split at KP…", self._split_section),
                            ("Merge selected", self._merge_sections),
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
            events = [dict(e) for e in self.model.events]
            for event in events:
                if event.get("event_id") == event_id:
                    event["notes"] = item.text()
            from .. import change_log

            self.model._write_events_and_sections(
                change_log.ACTION_MOVE_EVENT, event_id, events, "notes edit")

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
        for event_id in ids:
            self.model.delete_event(event_id)

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
            QMessageBox.information(self, "Burial Planner",
                                    "Select one burial section to split.")
            return
        section = next((s for s in self.model.sections
                        if s.get("section_id") == ids[0]), None)
        if section is None:
            return
        kp, ok = QInputDialog.getDouble(
            self, "Split section", "Split at KP:",
            (float(section.get("start_kp") or 0.0) + float(section.get("end_kp") or 0.0)) / 2.0,
            float(section.get("start_kp") or 0.0), float(section.get("end_kp") or 0.0), 3)
        if not ok:
            return
        try:
            self.model.split_section_at(ids[0], kp)
        except ValueError as exc:
            QMessageBox.warning(self, "Burial Planner", str(exc))

    def _merge_sections(self) -> None:
        ids = self._selected_section_ids()
        try:
            self.model.merge_sections(ids)
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
