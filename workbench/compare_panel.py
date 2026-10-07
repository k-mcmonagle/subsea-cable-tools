# -*- coding: utf-8 -*-
"""Side-by-side comparison of two RPLs (revisions, or design vs as-laid).

Pick any two RPLs — this segment's revisions are listed first, then every
other RPL in the project — and the panel reports four things: the event
mapping with along-track / cross-course / radial offsets and a report
(:mod:`event_compare_panel`), the headline statistics with the change between
them, the position-by-position mapping (what moved, what was renamed, what
was added or dropped) and the same for the legs. The mappings live in
:mod:`event_compare` and :mod:`rpl_compare`; this module is the pickers, the
tables, the filter and the CSV export.

The comparison is only computed when the tab is actually shown, and only when
the chosen pair changes, because it reads both revisions' layers in full.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..qgis_compat import (
    WINDOW_HINT_CLOSE,
    WINDOW_HINT_CUSTOMIZE,
    WINDOW_HINT_MIN_MAX,
    WINDOW_HINT_TITLE,
    WINDOW_TYPE_WINDOW,
)

from . import schema
from .event_compare_panel import EventComparisonWidget
from .rpl_compare import (
    LEG_CHANGE_LABELS, STATUS_ADDED, STATUS_CHANGED, STATUS_REMOVED,
    STATUS_UNCHANGED, change_summary, compare_revisions, describe_changes,
    statistic_rows,
)

STATUS_LABELS = {
    STATUS_UNCHANGED: "Unchanged",
    STATUS_CHANGED: "Changed",
    STATUS_ADDED: "Added",
    STATUS_REMOVED: "Removed",
}

STATUS_COLOURS = {
    STATUS_ADDED: QColor(224, 244, 228),
    STATUS_REMOVED: QColor(250, 228, 228),
    STATUS_CHANGED: QColor(252, 243, 218),
}

HOW_LABELS = {
    "coordinate": "same coordinate",
    "event": "same event",
    "aligned": "aligned",
}

KIND_AS_LAID = "as_laid"


def default_pair(rows):
    """``(rpl_a, rpl_b)`` to compare first from a segment's revisions
    (oldest first): newest design against newest as-laid when both exist,
    else the two newest revisions."""
    as_laid = [r for r in rows if (r.get("kind") or "") == KIND_AS_LAID]
    planned = [r for r in rows if (r.get("kind") or "") != KIND_AS_LAID]
    if as_laid and planned:
        return planned[-1].get("rpl_id") or "", as_laid[-1].get("rpl_id") or ""
    if len(rows) >= 2:
        return rows[-2].get("rpl_id") or "", rows[-1].get("rpl_id") or ""
    return "", ""


def _rpl_label(row, route_name=""):
    label = row.get("rev_label") or row.get("name") or "Unlabelled"
    kind = (row.get("kind") or "").replace("_", "-")
    status = row.get("status") or schema.STATUS_DRAFT
    prefix = f"{route_name} / " if route_name else ""
    return f"{prefix}{label} · {kind or 'planned'} ({status})"


class RevisionComparePanel(QWidget):
    """Compare two revisions of the same cable segment."""

    zoomRequested = pyqtSignal(float, float)
    openRevisionRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._store = None
        self._route_id = ""
        self._revisions: List[Dict] = []
        self._comparison = None
        self._rendered_key = None
        self._visible = False

        layout = QVBoxLayout(self)
        picker = QHBoxLayout()
        picker.addWidget(QLabel("Compare:"))
        self.combo_a = QComboBox()
        self.combo_a.setToolTip("The earlier revision (the baseline).")
        self.combo_a.currentIndexChanged.connect(self._selection_changed)
        picker.addWidget(self.combo_a, 1)
        picker.addWidget(QLabel("against:"))
        self.combo_b = QComboBox()
        self.combo_b.setToolTip("The later revision (what changed).")
        self.combo_b.currentIndexChanged.connect(self._selection_changed)
        picker.addWidget(self.combo_b, 1)
        swap_btn = QPushButton("Swap")
        swap_btn.clicked.connect(self._swap)
        picker.addWidget(swap_btn)
        layout.addLayout(picker)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.tabs = QTabWidget()
        self.events = EventComparisonWidget()
        self.events.zoomRequested.connect(self.zoomRequested)
        self.tabs.addTab(self.events, "Events")
        self.tabs.setTabToolTip(
            0, "Pair the events of A with B (e.g. design vs as-laid repeaters) and "
               "measure along-track, cross-course and radial offsets.")
        self.stats_table = QTableWidget(0, 4)
        self.stats_table.setHorizontalHeaderLabels(["Measure", "A", "B", "Change"])
        _configure_table(self.stats_table)
        self.tabs.addTab(self.stats_table, "Statistics")

        self.positions_table = QTableWidget(0, 10)
        self.positions_table.setHorizontalHeaderLabels([
            "Status", "A position", "A event", "B position", "B event",
            "A KP", "B KP", "Δ KP", "Offset", "What changed",
        ])
        _configure_table(self.positions_table)
        self.positions_table.setToolTip(
            "Double-click a row to zoom the map to that position.")
        self.positions_table.cellDoubleClicked.connect(self._position_activated)
        self.tabs.addTab(self.positions_table, "Positions")

        self.legs_table = QTableWidget(0, 8)
        self.legs_table.setHorizontalHeaderLabels([
            "Status", "From", "To", "A cable type", "B cable type",
            "A route length", "B route length", "What changed",
        ])
        _configure_table(self.legs_table)
        self.tabs.addTab(self.legs_table, "Legs")
        layout.addWidget(self.tabs, 1)

        controls = QHBoxLayout()
        self.changes_only = QCheckBox("Show changes only (positions, legs)")
        self.changes_only.setChecked(True)
        self.changes_only.setToolTip(
            "Hide positions and legs that are identical in both revisions.")
        self.changes_only.stateChanged.connect(self._populate_tables)
        controls.addWidget(self.changes_only)
        controls.addStretch()
        self.export_btn = QPushButton("Export comparison (CSV)...")
        self.export_btn.clicked.connect(self._export_csv)
        self.export_btn.setEnabled(False)
        controls.addWidget(self.export_btn)
        layout.addLayout(controls)

    # ------------------------------------------------------------- load --
    def load_segment(self, store, route_id: str, rpl_a: str = "", rpl_b: str = "") -> None:
        """Point the panel at a segment; the comparison waits until shown.

        The pickers list the segment's revisions (newest first) and then every
        other RPL in the project, so design and as-laid RPLs held under
        different segments can be compared too. ``rpl_a``/``rpl_b`` preselect
        a pair; by default the newest design is compared with the newest
        as-laid, else the two newest revisions.
        """
        self._store = store
        self._route_id = route_id or ""
        self._comparison = None
        self._rendered_key = None
        rows, others, route_names = [], [], {}
        if store is not None:
            try:
                rows = store.revisions_of_route(route_id) if route_id else []
                own = {row.get("rpl_id") for row in rows}
                others = [row for row in store.list_rpls() if row.get("rpl_id") not in own]
                route_names = {route.get("route_id"): route.get("name") or ""
                               for route in store.list_routes()}
            except Exception:
                rows, others = rows or [], []
        self._revisions = rows
        others.sort(key=lambda row: (route_names.get(row.get("route_id"), "~"),
                                     row.get("rev_label") or row.get("name") or ""))
        for combo in (self.combo_a, self.combo_b):
            combo.blockSignals(True)
            combo.clear()
            for row in reversed(rows):           # newest first
                combo.addItem(_rpl_label(row), row.get("rpl_id") or "")
            if rows and others:
                combo.insertSeparator(combo.count())
            for row in others:
                combo.addItem(_rpl_label(row, route_names.get(row.get("route_id"), "")
                                         or "(no segment)"), row.get("rpl_id") or "")
            combo.blockSignals(False)
        default_a, default_b = default_pair(rows)
        choice_a, choice_b = rpl_a or default_a, rpl_b or default_b
        if choice_a and choice_a == choice_b:
            # Keep the side the caller asked for; move the other one.
            fixed = choice_b if (rpl_b and not rpl_a) else choice_a
            pool = [default_a, default_b] + [self.combo_a.itemData(i)
                                             for i in range(self.combo_a.count())]
            other = next((rpl for rpl in pool if rpl and rpl != fixed), "")
            if fixed == choice_b:
                choice_a = other
            else:
                choice_b = other
        self._select(self.combo_a, choice_a)
        self._select(self.combo_b, choice_b)
        self._clear_tables()
        self.events.clear()
        if len(rows) + len(others) < 2:
            self.summary.setText(
                "The project has fewer than two RPLs, so there is nothing to compare yet.")
            self.export_btn.setEnabled(False)
        else:
            self.summary.setText("Select two RPLs to compare.")
        self._refresh_if_visible()

    def select_pair(self, rpl_a: str, rpl_b: str) -> None:
        """Preselect A and B (either may be empty to keep the current choice)."""
        for combo, rpl_id in ((self.combo_a, rpl_a), (self.combo_b, rpl_b)):
            combo.blockSignals(True)
            self._select(combo, rpl_id)
            combo.blockSignals(False)
        self._refresh_if_visible()

    @staticmethod
    def _select(combo, rpl_id):
        if not rpl_id:
            return
        index = combo.findData(rpl_id)
        if index >= 0:
            combo.setCurrentIndex(index)

    def set_visible_tab(self, visible: bool) -> None:
        """Told by the owner whether this tab is on screen."""
        self._visible = bool(visible)
        self._refresh_if_visible()

    # ---------------------------------------------------------- compute --
    def _selection_changed(self, *_args):
        self._refresh_if_visible()

    def _swap(self):
        index_a, index_b = self.combo_a.currentIndex(), self.combo_b.currentIndex()
        self.combo_a.blockSignals(True)
        self.combo_b.blockSignals(True)
        self.combo_a.setCurrentIndex(index_b)
        self.combo_b.setCurrentIndex(index_a)
        self.combo_a.blockSignals(False)
        self.combo_b.blockSignals(False)
        self._refresh_if_visible()

    def _refresh_if_visible(self):
        if not self._visible:
            return
        rpl_a = str(self.combo_a.currentData() or "")
        rpl_b = str(self.combo_b.currentData() or "")
        if not rpl_a or not rpl_b:
            return
        if rpl_a == rpl_b:
            self._comparison = None
            self._rendered_key = None
            self._clear_tables()
            self.events.clear()
            self.summary.setText("Pick two different RPLs.")
            self.export_btn.setEnabled(False)
            return
        key = (rpl_a, rpl_b)
        if key == self._rendered_key:
            return
        self._rendered_key = key
        self._compare(rpl_a, rpl_b)

    def _compare(self, rpl_a: str, rpl_b: str) -> None:
        store = self._store
        if store is None:
            return
        from .rpl_summary import open_rpl_layer, read_leg_rows, read_point_rows

        row_a = store.get_rpl(rpl_a) or {}
        row_b = store.get_rpl(rpl_b) or {}
        points_a = read_point_rows(open_rpl_layer(store, row_a.get("points_layer") or ""))
        legs_a = read_leg_rows(open_rpl_layer(store, row_a.get("lines_layer") or ""))
        points_b = read_point_rows(open_rpl_layer(store, row_b.get("points_layer") or ""))
        legs_b = read_leg_rows(open_rpl_layer(store, row_b.get("lines_layer") or ""))
        self._comparison = compare_revisions(
            points_a, legs_a, points_b, legs_b,
            a_label=row_a.get("rev_label") or row_a.get("name") or "A",
            b_label=row_b.get("rev_label") or row_b.get("name") or "B",
            a_kind=(row_a.get("kind") or "").replace("_", " "),
            b_kind=(row_b.get("kind") or "").replace("_", " "),
            a_status=row_a.get("status") or schema.STATUS_DRAFT,
            b_status=row_b.get("status") or schema.STATUS_DRAFT,
            classify=self._classifier(),
        )
        self.summary.setText(
            f"A = {self._comparison.a_label} → B = {self._comparison.b_label}. "
            + change_summary(self._comparison))
        self.export_btn.setEnabled(True)
        self._populate_tables()
        self.events.set_data(
            points_a, points_b, key=f"{rpl_a}__{rpl_b}",
            a_label=self._comparison.a_label, b_label=self._comparison.b_label,
            classify=self._classifier(),
            a_detail=" ".join(bit for bit in (
                (row_a.get("kind") or "").replace("_", "-"), row_a.get("status") or "") if bit),
            b_detail=" ".join(bit for bit in (
                (row_b.get("kind") or "").replace("_", "-"), row_b.get("status") or "") if bit))

    def _classifier(self):
        from .assembly_model import EventClassifier

        try:
            if self._store is not None and self._store.exists():
                return EventClassifier(self._store.list_event_rules()).classify
        except Exception:
            pass
        return EventClassifier.with_defaults().classify

    # --------------------------------------------------------- populate --
    def _clear_tables(self):
        for table in (self.stats_table, self.positions_table, self.legs_table):
            table.setRowCount(0)

    def _populate_tables(self, *_args):
        comparison = self._comparison
        if comparison is None:
            self._clear_tables()
            return
        rows = statistic_rows(comparison)
        self.stats_table.setHorizontalHeaderLabels([
            "Measure", comparison.a_label or "A", comparison.b_label or "B", "Change"])
        self.stats_table.setUpdatesEnabled(False)
        try:
            self.stats_table.setRowCount(len(rows))
            for index, values in enumerate(rows):
                _set_row(self.stats_table, index, values)
                if values[3]:
                    _tint_row(self.stats_table, index, STATUS_COLOURS[STATUS_CHANGED])
        finally:
            self.stats_table.setUpdatesEnabled(True)

        changes_only = self.changes_only.isChecked()
        positions = [m for m in comparison.positions
                     if not changes_only or m.status != STATUS_UNCHANGED]
        self.positions_table.setUpdatesEnabled(False)
        try:
            self.positions_table.setRowCount(len(positions))
            for index, match in enumerate(positions):
                a, b = match.a or {}, match.b or {}
                _set_row(self.positions_table, index, [
                    STATUS_LABELS.get(match.status, match.status),
                    a.get("pos"), _text(a.get("event")),
                    b.get("pos"), _text(b.get("event")),
                    _km(a.get("kp")), _km(b.get("kp")),
                    _signed(match.kp_delta_km, 3, " km"),
                    _metres(match.distance_m),
                    describe_changes(match.changes),
                ], user_data=match)
                colour = STATUS_COLOURS.get(match.status)
                if colour is not None:
                    _tint_row(self.positions_table, index, colour)
                item = self.positions_table.item(index, 0)
                if item is not None and match.how:
                    item.setToolTip(
                        f"Matched by {HOW_LABELS.get(match.how, match.how)}.")
        finally:
            self.positions_table.setUpdatesEnabled(True)

        legs = [m for m in comparison.legs
                if not changes_only or m.status != STATUS_UNCHANGED]
        self.legs_table.setUpdatesEnabled(False)
        try:
            self.legs_table.setRowCount(len(legs))
            for index, match in enumerate(legs):
                a, b = match.a or {}, match.b or {}
                _set_row(self.legs_table, index, [
                    STATUS_LABELS.get(match.status, match.status),
                    match.start_event or (a.get("from_pos") if a else b.get("from_pos")),
                    match.end_event or (a.get("to_pos") if a else b.get("to_pos")),
                    _text(a.get("cable_type")), _text(b.get("cable_type")),
                    _km(a.get("route_km")), _km(b.get("route_km")),
                    describe_changes(match.changes, LEG_CHANGE_LABELS),
                ])
                colour = STATUS_COLOURS.get(match.status)
                if colour is not None:
                    _tint_row(self.legs_table, index, colour)
        finally:
            self.legs_table.setUpdatesEnabled(True)

    # ---------------------------------------------------------- actions --
    def _position_activated(self, row, _column):
        item = self.positions_table.item(row, 0)
        match = item.data(Qt.ItemDataRole.UserRole) if item else None
        if match is None:
            return
        source = match.b or match.a or {}
        lat, lon = source.get("lat"), source.get("lon")
        if lat is not None and lon is not None:
            self.zoomRequested.emit(float(lat), float(lon))

    def _export_csv(self):
        comparison = self._comparison
        if comparison is None:
            return
        default = f"rpl_comparison_{_slug(comparison.a_label)}_vs_{_slug(comparison.b_label)}.csv"
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export comparison", os.path.join(os.path.expanduser("~"), default),
            "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["RPL revision comparison"])
                writer.writerow(["A", comparison.a_label, "B", comparison.b_label])
                writer.writerow([])
                writer.writerow(["Measure", comparison.a_label, comparison.b_label,
                                 "Change"])
                for values in statistic_rows(comparison):
                    writer.writerow(list(values))
                writer.writerow([])
                writer.writerow([
                    "Position status", "A position", "A event", "B position",
                    "B event", "A KP (km)", "B KP (km)", "KP change (km)",
                    "Offset (m)", "What changed", "Matched by",
                ])
                for match in comparison.positions:
                    a, b = match.a or {}, match.b or {}
                    writer.writerow([
                        STATUS_LABELS.get(match.status, match.status),
                        a.get("pos"), _text(a.get("event")),
                        b.get("pos"), _text(b.get("event")),
                        _plain(a.get("kp"), 6), _plain(b.get("kp"), 6),
                        _plain(match.kp_delta_km, 6), _plain(match.distance_m, 2),
                        describe_changes(match.changes),
                        HOW_LABELS.get(match.how, match.how),
                    ])
                writer.writerow([])
                writer.writerow([
                    "Leg status", "From", "To", "A cable type", "B cable type",
                    "A route length (km)", "B route length (km)",
                    "A cable length (km)", "B cable length (km)", "What changed",
                ])
                for match in comparison.legs:
                    a, b = match.a or {}, match.b or {}
                    writer.writerow([
                        STATUS_LABELS.get(match.status, match.status),
                        match.start_event, match.end_event,
                        _text(a.get("cable_type")), _text(b.get("cable_type")),
                        _plain(a.get("route_km"), 6), _plain(b.get("route_km"), 6),
                        _plain(a.get("cable_km"), 6), _plain(b.get("cable_km"), 6),
                        describe_changes(match.changes, LEG_CHANGE_LABELS),
                    ])
        except OSError as exc:
            QMessageBox.warning(self, "Export comparison", str(exc))
            return
        QMessageBox.information(self, "Export comparison", f"Written to {path}")


class RplCompareDialog(QDialog):
    """The comparison in its own resizable, non-modal window."""

    def __init__(self, store, route_id: str, rpl_a: str = "", rpl_b: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Compare RPLs")
        self.setWindowFlags(WINDOW_TYPE_WINDOW | WINDOW_HINT_CUSTOMIZE | WINDOW_HINT_TITLE
                            | WINDOW_HINT_MIN_MAX | WINDOW_HINT_CLOSE)
        layout = QVBoxLayout(self)
        self.panel = RevisionComparePanel(self)
        layout.addWidget(self.panel)
        self.panel.load_segment(store, route_id, rpl_a, rpl_b)
        self.panel.set_visible_tab(True)
        self.resize(1200, 760)


# ---------------------------------------------------------------- helpers --
def _configure_table(table):
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(False)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)


def _set_row(table, row, values, user_data=None):
    for column, value in enumerate(values):
        item = QTableWidgetItem("" if value is None else str(value))
        if column == 0 and user_data is not None:
            item.setData(Qt.ItemDataRole.UserRole, user_data)
        table.setItem(row, column, item)


def _tint_row(table, row, colour: QColor):
    brush = QBrush(colour)
    for column in range(table.columnCount()):
        item = table.item(row, column)
        if item is not None:
            item.setBackground(brush)


def _text(value) -> str:
    return str(value or "").strip()


def _km(value) -> str:
    return "" if value is None else f"{float(value):.3f}"


def _signed(value, decimals: int, suffix: str = "") -> str:
    if value is None:
        return ""
    return f"{float(value):+.{decimals}f}{suffix}"


def _metres(value) -> str:
    return "" if value is None else f"{float(value):,.1f} m"


def _plain(value, decimals: int) -> str:
    return "" if value is None else f"{float(value):.{decimals}f}"


def _slug(text: str) -> str:
    return schema.sanitize_slug(text)
