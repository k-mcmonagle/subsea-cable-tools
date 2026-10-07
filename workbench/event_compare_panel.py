# -*- coding: utf-8 -*-
"""Event comparison tab: map A's events to B's, review offsets, report.

Side by side: every event of RPL A (e.g. the design) with the event of RPL B
(e.g. the as-laid) it was paired with. Pairs are suggested automatically
(:mod:`event_compare`); any pairing can be changed from a dropdown that lists
B's events nearest first. Corrections are saved with the QGIS project and
re-applied whenever the same two RPLs are compared again.

Filters narrow the table to the events that matter (repeaters only,
transitions, everything but alter courses, a text/regex search). The
selection drives everything downstream: the summary line, the radial
preview, the CSV export, the HTML report and the offset lines added to the
map.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Callable, Dict, List, Optional, Sequence

from qgis.PyQt.QtCore import QTimer, Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSplitter,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from ..qgis_compat import (
    EDIT_TRIGGER_DOUBLE_CLICKED,
    EDIT_TRIGGER_EDIT_KEY_PRESSED,
    EDIT_TRIGGER_SELECTED_CLICKED,
    FIELD_TYPE_DOUBLE,
    FIELD_TYPE_STRING,
    MESSAGEBOX_YES,
    QSvgWidget,
    TOOLBUTTON_POPUP_MODE_INSTANT,
)
from . import event_compare as ec
from . import event_compare_report as report

PROJECT_SCOPE = "SubseaCableTools"
PROJECT_KEY = "event_mapping/{key}"

COL_A, COL_A_KP, COL_B, COL_B_KP, COL_HOW, COL_TYPE = range(6)
COL_DKP, COL_ALONG, COL_CROSS, COL_RADIAL, COL_BEARING, COL_TARGET = range(6, 12)
HEADERS = ["A event", "A KP", "B event", "B KP", "Match", "Type", "ΔKP (m)",
           "Along (m)", "Cross (m)", "Radial (m)", "Bearing", "In target"]

ROW_COLOURS = {
    "only_a": QColor(250, 228, 228),
    "only_b": QColor(224, 244, 228),
    "review": QColor(252, 243, 218),
    "manual": QColor(226, 236, 250),
}
NO_MATCH = -1


class _PartnerDelegate(QStyledItemDelegate):
    """Dropdown editor for the A/B event cells, opened straight away."""

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def createEditor(self, parent, option, index):
        choices = self._owner.partner_choices(index.row(), index.column())
        if choices is None:
            return None
        combo = QComboBox(parent)
        for label, value, tooltip in choices:
            combo.addItem(label, value)
            if tooltip:
                combo.setItemData(combo.count() - 1, tooltip, Qt.ItemDataRole.ToolTipRole)
        combo.setMaxVisibleItems(20)
        combo.activated.connect(lambda _i, c=combo: self._commit(c))
        QTimer.singleShot(0, combo.showPopup)
        return combo

    def _commit(self, combo):
        self.commitData.emit(combo)
        self.closeEditor.emit(combo)

    def setEditorData(self, editor, index):
        current = self._owner.current_partner_value(index.row(), index.column())
        position = editor.findData(current)
        editor.setCurrentIndex(position if position >= 0 else 0)

    def setModelData(self, editor, model, index):
        self._owner.apply_partner(index.row(), index.column(), editor.currentData())


class EventComparisonWidget(QWidget):
    """The "Events" tab of the RPL comparison."""

    zoomRequested = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points_a: List[Dict] = []
        self._points_b: List[Dict] = []
        self._classify: Optional[Callable] = None
        self._key = ""
        self._a_label, self._b_label = "A", "B"
        self._a_detail, self._b_detail = "", ""
        self._mapping: Optional[ec.EventMapping] = None
        self._rows: List[ec.EventOffset] = []
        self._shown: List[ec.EventOffset] = []
        self._excluded: set = set()        # _row_key of rows unticked by the user
        self._type_checks: Dict[str, bool] = {}
        self._populating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Show:"))
        self.preset = QComboBox()
        for key, label, _include, _exclude in ec.FILTER_PRESETS:
            self.preset.addItem(label, key)
        self.preset.setToolTip("Quick event-type selections. Fine-tune with Types.")
        self.preset.currentIndexChanged.connect(self._preset_changed)
        filters.addWidget(self.preset)
        self.types_btn = QToolButton()
        self.types_btn.setText("Types")
        self.types_btn.setToolTip(
            "Tick the event types to include. Types come from the project's event rules.")
        self.types_btn.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        self.types_menu = QMenu(self.types_btn)
        self.types_btn.setMenu(self.types_menu)
        filters.addWidget(self.types_btn)
        self.text_filter = QLineEdit()
        self.text_filter.setPlaceholderText("Filter by event text or regex…")
        self.text_filter.setClearButtonEnabled(True)
        self.text_filter.textChanged.connect(self._refresh_table)
        filters.addWidget(self.text_filter, 1)
        self.show_unmatched = QCheckBox("Unmatched")
        self.show_unmatched.setChecked(True)
        self.show_unmatched.setToolTip("Also list events that have no partner in the other RPL.")
        self.show_unmatched.stateChanged.connect(self._refresh_table)
        filters.addWidget(self.show_unmatched)
        layout.addLayout(filters)

        options = QHBoxLayout()
        options.addWidget(QLabel("Target radius:"))
        self.target = QDoubleSpinBox()
        self.target.setRange(0.0, 100000.0)
        self.target.setDecimals(1)
        self.target.setSuffix(" m")
        self.target.setSpecialValueText("off")
        self.target.setValue(50.0)
        self.target.setToolTip("Events within this radial distance count as on target (0 = off).")
        self.target.valueChanged.connect(self._refresh_table)
        options.addWidget(self.target)
        options.addWidget(QLabel("Match within:"))
        self.search_radius = QDoubleSpinBox()
        self.search_radius.setRange(10.0, 1000000.0)
        self.search_radius.setDecimals(0)
        self.search_radius.setSuffix(" m")
        self.search_radius.setValue(ec.DEFAULT_SEARCH_RADIUS_M)
        self.search_radius.setToolTip(
            "How far apart two events with different names may be and still be paired.\n"
            "Exact names pair at any distance; similar names up to 10x this.")
        options.addWidget(self.search_radius)
        self.keep_order = QCheckBox("Keep route order")
        self.keep_order.setChecked(True)
        self.keep_order.setToolTip(
            "Pairs never cross: event order along the route is preserved.\n"
            "Untick for RPLs whose events are not in the same order.")
        options.addWidget(self.keep_order)
        rematch = QPushButton("Re-match")
        rematch.setToolTip("Suggest pairs again with these settings; your corrections are kept.")
        rematch.clicked.connect(lambda: self._rematch(keep_overrides=True))
        options.addWidget(rematch)
        reset = QPushButton("Clear corrections")
        reset.setToolTip("Forget the manual pairings saved for these two RPLs.")
        reset.clicked.connect(self._clear_overrides)
        options.addWidget(reset)
        options.addStretch()
        options.addWidget(QLabel("Plot:"))
        self.frame = QComboBox()
        self.frame.addItem("Route-relative", report.FRAME_ROUTE)
        self.frame.addItem("North-up", report.FRAME_NORTH)
        self.frame.setToolTip("Route-relative: up = ahead along A, right = starboard.\n"
                              "North-up: up = north, right = east.")
        self.frame.currentIndexChanged.connect(self._update_preview)
        options.addWidget(self.frame)
        layout.addLayout(options)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(EDIT_TRIGGER_DOUBLE_CLICKED | EDIT_TRIGGER_SELECTED_CLICKED
                                   | EDIT_TRIGGER_EDIT_KEY_PRESSED)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._delegate = _PartnerDelegate(self)
        self.table.setItemDelegateForColumn(COL_A, self._delegate)
        self.table.setItemDelegateForColumn(COL_B, self._delegate)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._update_preview)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        self.table.setToolTip(
            "Click a B event (or the A event of an 'only in B' row) to change its partner.\n"
            "Untick a row to leave it out of the report and exports.\n"
            "Double-click a KP or offset to zoom the map to the event.")
        splitter.addWidget(self.table)
        self.preview = QSvgWidget()
        self.preview.setMinimumSize(220, 240)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        actions = QHBoxLayout()
        self.counts = QLabel()
        self.counts.setStyleSheet("color:#555;")
        actions.addWidget(self.counts, 1)
        self.csv_btn = QPushButton("Export CSV…")
        self.csv_btn.clicked.connect(self._export_csv)
        actions.addWidget(self.csv_btn)
        self.report_btn = QPushButton("Report…")
        self.report_btn.setToolTip(
            "HTML report of the selected events: summary statistics, a radial plot of all of\n"
            "them, offsets along the route and one radial plot per event. Print it to PDF\n"
            "from the browser.")
        self.report_btn.clicked.connect(self._export_report)
        actions.addWidget(self.report_btn)
        self.map_btn = QPushButton("Add offset lines to map")
        self.map_btn.setToolTip("A line from each A event to its B partner, with the offsets.")
        self.map_btn.clicked.connect(self._add_to_map)
        actions.addWidget(self.map_btn)
        layout.addLayout(actions)
        self._set_enabled(False)

    # ------------------------------------------------------------ public --
    def clear(self, message: str = "") -> None:
        self._mapping = None
        self._rows, self._shown = [], []
        self.table.setRowCount(0)
        self.preview.load(b"")
        self.summary.setText(message)
        self.counts.clear()
        self._set_enabled(False)

    def set_data(self, points_a: Sequence[Dict], points_b: Sequence[Dict], key: str = "",
                 a_label: str = "A", b_label: str = "B", classify: Optional[Callable] = None,
                 a_detail: str = "", b_detail: str = "") -> None:
        """Compare two RPLs' point rows (route order). ``key`` identifies the
        pair for saving corrections with the project."""
        self._points_a = list(points_a or [])
        self._points_b = list(points_b or [])
        self._key = key
        self._a_label, self._b_label = a_label or "A", b_label or "B"
        self._a_detail, self._b_detail = a_detail, b_detail
        self._classify = classify
        self._excluded = set()
        self._rematch(keep_overrides=True)

    def mapping(self) -> Optional[ec.EventMapping]:
        return self._mapping

    def rows(self) -> List[ec.EventOffset]:
        return list(self._rows)

    def selected_rows(self) -> List[ec.EventOffset]:
        """Rows passing the filter and not unticked: what exports use."""
        return [row for row in self._shown if _row_key(row) not in self._excluded]

    def event_filter(self) -> ec.EventFilter:
        include = None
        if self._type_checks and not all(self._type_checks.values()):
            include = frozenset(k for k, v in self._type_checks.items() if v)
        return ec.EventFilter(include=include, text=self.text_filter.text(),
                              show_unmatched=self.show_unmatched.isChecked())

    def target_radius(self) -> Optional[float]:
        return self.target.value() or None

    # ---------------------------------------------------------- matching --
    def _options(self) -> ec.MatchOptions:
        return ec.MatchOptions(search_radius_m=self.search_radius.value(),
                               respect_order=self.keep_order.isChecked())

    def _rematch(self, keep_overrides=True):
        events_a = ec.extract_events(self._points_a, self._classify)
        events_b = ec.extract_events(self._points_b, self._classify)
        if not events_a or not events_b:
            side = "A" if not events_a else "B"
            self.clear(f"RPL {side} has no events (no position has Event text), so there is "
                       "nothing to pair.")
            return
        self._mapping = ec.EventMapping.suggest(events_a, events_b, self._options())
        if keep_overrides:
            self._mapping.apply_overrides(self._load_overrides())
        self._recompute(rebuild_types=True)
        self._set_enabled(True)

    def _clear_overrides(self):
        if self._mapping is None:
            return
        if self._mapping.manual_overrides():
            answer = QMessageBox.question(
                self, "Clear corrections",
                "Forget every manual pairing saved for these two RPLs?")
            if answer != MESSAGEBOX_YES:
                return
        self._save_overrides([])
        self._rematch(keep_overrides=False)

    def _recompute(self, rebuild_types=False):
        self._rows = ec.compute_offsets(self._mapping, self._points_a)
        if rebuild_types:
            self._rebuild_type_menu()
        self._refresh_table()

    # ---------------------------------------------------------- filters --
    def _rebuild_type_menu(self):
        previous = dict(self._type_checks)
        self.types_menu.clear()
        self._type_checks = {}
        for key, count in ec.type_counts(self._rows):
            action = self.types_menu.addAction(f"{ec.type_label(key)} ({count})")
            action.setCheckable(True)
            action.setChecked(previous.get(key, True))
            action.setData(key)
            action.toggled.connect(lambda checked, k=key: self._type_toggled(k, checked))
            self._type_checks[key] = action.isChecked()
        self._apply_preset_to_checks(self.preset.currentData(), refresh=False)

    def _preset_changed(self, *_args):
        self._apply_preset_to_checks(self.preset.currentData(), refresh=True)

    def _apply_preset_to_checks(self, key, refresh=True):
        for preset_key, _label, include, exclude in ec.FILTER_PRESETS:
            if preset_key != key:
                continue
            for action in self.types_menu.actions():
                type_key = action.data()
                wanted = (include is None or type_key in include) and type_key not in exclude
                action.blockSignals(True)
                action.setChecked(wanted)
                action.blockSignals(False)
                self._type_checks[type_key] = wanted
        if refresh:
            self._refresh_table()

    def _type_toggled(self, key, checked):
        self._type_checks[key] = checked
        self._refresh_table()

    # ------------------------------------------------------------ table --
    def _refresh_table(self, *_args):
        if self._populating:
            return
        self._shown = ec.filter_rows(self._rows, self.event_filter())
        target = self.target_radius()
        self._populating = True
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(self._shown))
            for index, row in enumerate(self._shown):
                self._fill_row(index, row, target)
        finally:
            self.table.setUpdatesEnabled(True)
            self._populating = False
        self._update_summary()
        self._update_preview()

    def _fill_row(self, index, row: ec.EventOffset, target):
        a, b = row.a, row.b
        within = row.within(target)
        values = [
            a.event if a else "(pick an A event…)",
            _km(a.kp if a else None),
            b.event if b else ("(no match)" if a else ""),
            _km(b.kp if b else None),
            ec.HOW_LABELS.get(row.how, row.how) if row.matched else (
                "Only in A" if a else "Only in B"),
            (a or b).type_text if (a or b) else "",
            _signed(row.kp_delta_m), _signed(row.along_m), _signed(row.cross_m),
            _unsigned(row.radial_m),
            "" if row.bearing_deg is None else f"{row.bearing_deg:.0f}°",
            "" if within is None else ("✓" if within else "✗"),
        ]
        editable = {COL_B} if a is not None else {COL_A}
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
            if column in editable:
                flags |= Qt.ItemFlag.ItemIsEditable
                item.setToolTip("Click to choose the partner event.")
            if column == COL_A:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
                item.setCheckState(Qt.CheckState.Unchecked if _row_key(row) in self._excluded
                                   else Qt.CheckState.Checked)
                item.setData(Qt.ItemDataRole.UserRole, index)
            if column >= COL_DKP or column in (COL_A_KP, COL_B_KP):
                item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            item.setFlags(flags)
            self.table.setItem(index, column, item)
        colour = None
        if a is not None and b is None:
            colour = ROW_COLOURS["only_a"]
        elif a is None:
            colour = ROW_COLOURS["only_b"]
        elif row.how == ec.HOW_MANUAL:
            colour = ROW_COLOURS["manual"]
        elif row.how in (ec.HOW_FUZZY, ec.HOW_POSITION):
            colour = ROW_COLOURS["review"]
        if colour is not None:
            brush = QBrush(colour)
            for column in range(self.table.columnCount()):
                self.table.item(index, column).setBackground(brush)
        how_item = self.table.item(index, COL_HOW)
        if row.matched and row.how in (ec.HOW_FUZZY, ec.HOW_POSITION):
            how_item.setToolTip(f"Suggested with confidence {row.score:.2f} — worth a check.")

    def _item_changed(self, item):
        if self._populating or item.column() != COL_A:
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        if index is None or not 0 <= index < len(self._shown):
            return
        row_id = _row_key(self._shown[index])
        if item.checkState() == Qt.CheckState.Checked:
            self._excluded.discard(row_id)
        else:
            self._excluded.add(row_id)
        self._update_summary()
        self._update_preview()

    # -- partner editing (used by the delegate) -------------------------------
    def partner_choices(self, table_row, column):
        """``[(label, value, tooltip)]`` for the dropdown, nearest first."""
        if self._mapping is None or not 0 <= table_row < len(self._shown):
            return None
        row = self._shown[table_row]
        if column == COL_B and row.a is not None:
            anchor, candidates, side = row.a, self._mapping.events_b, "b"
        elif column == COL_A and row.a is None and row.b is not None:
            anchor, candidates, side = row.b, self._mapping.events_a, "a"
        else:
            return None
        owners = self._owners(side)
        scored = []
        for index, event in enumerate(candidates):
            distance = ec.haversine_m(anchor.lat, anchor.lon, event.lat, event.lon)
            scored.append((distance if distance is not None else float("inf"), index, event))
        scored.sort(key=lambda item: (item[0], item[1]))
        choices = [("(no match)", NO_MATCH, "Leave this event unpaired.")]
        for distance, index, event in scored:
            where = "" if distance == float("inf") else f" · {_distance_text(distance)} away"
            owner = owners.get(index)
            used = f" · paired with {owner}" if owner else ""
            choices.append((f"{event.label()}{where}{used}", index,
                            "Choosing an event already paired moves it here." if owner else ""))
        return choices

    def _owners(self, side):
        out = {}
        for a_index, (b_index, _how, _score) in self._mapping.pairs.items():
            if side == "b":
                out[b_index] = self._mapping.events_a[a_index].event
            else:
                out[a_index] = self._mapping.events_b[b_index].event
        return out

    def current_partner_value(self, table_row, column):
        if self._mapping is None or not 0 <= table_row < len(self._shown):
            return NO_MATCH
        row = self._shown[table_row]
        if column == COL_B and row.a is not None:
            partner = self._mapping.partner(self._event_index(self._mapping.events_a, row.a))
            return NO_MATCH if partner is None else partner
        return NO_MATCH

    def apply_partner(self, table_row, column, value):
        if self._mapping is None or not 0 <= table_row < len(self._shown):
            return
        row = self._shown[table_row]
        value = None if value is None or int(value) == NO_MATCH else int(value)
        if column == COL_B and row.a is not None:
            a_index = self._event_index(self._mapping.events_a, row.a)
            if value == self._mapping.partner(a_index):
                return
            self._mapping.set_partner(a_index, value)
        elif column == COL_A and row.a is None and row.b is not None:
            if value is None:
                return
            b_index = self._event_index(self._mapping.events_b, row.b)
            self._mapping.set_partner(value, b_index)
        else:
            return
        self._save_overrides(self._mapping.manual_overrides())
        # Defer: the delegate is still inside setModelData for this cell.
        QTimer.singleShot(0, self._recompute)

    @staticmethod
    def _event_index(events, event):
        for index, candidate in enumerate(events):
            if candidate is event or candidate.index == event.index:
                return index
        return None

    # -- persistence -----------------------------------------------------------
    def _load_overrides(self):
        if not self._key:
            return []
        try:
            from qgis.core import QgsProject

            raw, ok = QgsProject.instance().readEntry(
                PROJECT_SCOPE, PROJECT_KEY.format(key=self._key), "")
            return json.loads(raw) if ok and raw else []
        except Exception:  # noqa: BLE001 - corrupt entries just start fresh
            return []

    def _save_overrides(self, overrides):
        if not self._key:
            return
        try:
            from qgis.core import QgsProject

            project = QgsProject.instance()
            key = PROJECT_KEY.format(key=self._key)
            if overrides:
                project.writeEntry(PROJECT_SCOPE, key, json.dumps(overrides))
            else:
                project.removeEntry(PROJECT_SCOPE, key)
        except Exception:  # noqa: BLE001 - persistence is a convenience
            pass

    # ---------------------------------------------------------- summary --
    def _update_summary(self):
        selected = self.selected_rows()
        stats = ec.offset_stats(selected, self.target_radius())
        text = ec.summary_text(stats)
        if self._mapping is not None and self._mapping.reversed:
            text += " · B runs in the opposite direction to A (ΔKP not shown)."
        self.summary.setText(text)
        if self._mapping is not None:
            counts = self._mapping.counts()
            review = counts.get(ec.HOW_FUZZY, 0) + counts.get(ec.HOW_POSITION, 0)
            self.counts.setText(
                f"Pairs: {counts.get(ec.HOW_EXACT, 0)} exact, "
                f"{counts.get(ec.HOW_FUZZY, 0)} similar name, "
                f"{counts.get(ec.HOW_POSITION, 0)} by position, "
                f"{counts.get(ec.HOW_MANUAL, 0)} manual · "
                f"{counts['unmatched_a']} only in A, {counts['unmatched_b']} only in B"
                + (f" · {review} to review (amber)" if review else ""))

    def _update_preview(self, *_args):
        if self._mapping is None:
            return
        frame = self.frame.currentData()
        selected_indexes = sorted({index.row() for index in self.table.selectedIndexes()})
        chosen = [self._shown[i] for i in selected_indexes if 0 <= i < len(self._shown)]
        chosen = [row for row in chosen if row.matched]
        rows = [row for row in self.selected_rows() if row.matched]
        colours = report.colour_map(rows)
        if len(chosen) == 1:
            title = chosen[0].a.event if chosen[0].a else ""
            svg = report.radial_plot_svg(chosen, frame=frame, size=300,
                                         target_radius_m=self.target_radius(),
                                         title=title, colours=colours, show_legend=False,
                                         label_points=False)
        else:
            svg = report.radial_plot_svg(chosen if len(chosen) > 1 else rows, frame=frame,
                                         size=300, target_radius_m=self.target_radius(),
                                         title="Selected rows" if len(chosen) > 1 else
                                         "All shown events", colours=colours)
        self.preview.load(svg.encode("utf-8"))
        renderer = self.preview.renderer()
        if renderer is not None and hasattr(renderer, "setAspectRatioMode"):
            renderer.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)

    # ---------------------------------------------------------- actions --
    def _cell_double_clicked(self, table_row, column):
        if column in (COL_A, COL_B) or not 0 <= table_row < len(self._shown):
            return
        row = self._shown[table_row]
        event = row.b if column == COL_B_KP and row.b is not None else (row.a or row.b)
        if event is not None and event.lat is not None and event.lon is not None:
            self.zoomRequested.emit(float(event.lat), float(event.lon))

    def _selection_text(self):
        bits = [self.preset.currentText()]
        if self._type_checks and not all(self._type_checks.values()):
            bits = [", ".join(ec.type_label(k) for k, v in self._type_checks.items() if v)
                    or "no types"]
        if self.text_filter.text().strip():
            bits.append(f"matching '{self.text_filter.text().strip()}'")
        if self._excluded:
            bits.append(f"{len(self._excluded)} row(s) excluded by hand")
        return ", ".join(bits)

    def _default_path(self, extension):
        from .schema import sanitize_slug

        name = f"event_comparison_{sanitize_slug(self._a_label)}_vs_{sanitize_slug(self._b_label)}"
        return os.path.join(os.path.expanduser("~"), f"{name}.{extension}")

    def _export_csv(self):
        rows = self.selected_rows()
        if not rows:
            QMessageBox.information(self, "Export CSV", "No events in the current selection.")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export event comparison", self._default_path("csv"), "CSV files (*.csv)")
        if not path:
            return
        try:
            write_csv(path, rows, self._a_label, self._b_label, self.target_radius(),
                      self._selection_text())
        except OSError as exc:
            QMessageBox.warning(self, "Export CSV", str(exc))
            return
        QMessageBox.information(self, "Export CSV", f"Written to {path}")

    def _export_report(self):
        rows = self.selected_rows()
        if not any(row.matched for row in rows):
            QMessageBox.information(self, "Report", "No matched events in the current selection.")
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save event comparison report", self._default_path("html"),
            "HTML report (*.html)")
        if not path:
            return
        page = report.html_report(
            rows, self._a_label, self._b_label, frame=self.frame.currentData(),
            target_radius_m=self.target_radius(), selection_text=self._selection_text(),
            a_detail=self._a_detail, b_detail=self._b_detail,
            reversed_b=bool(self._mapping and self._mapping.reversed))
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(page)
        except OSError as exc:
            QMessageBox.warning(self, "Report", str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _add_to_map(self):
        rows = [row for row in self.selected_rows() if row.matched]
        if not rows:
            QMessageBox.information(self, "Offset lines", "No matched events in the current selection.")
            return
        from qgis.core import QgsProject

        layer = build_offset_layer(rows, f"Event offsets {self._a_label} → {self._b_label}",
                                   self.target_radius())
        QgsProject.instance().addMapLayer(layer)

    def _set_enabled(self, enabled):
        for widget in (self.csv_btn, self.report_btn, self.map_btn):
            widget.setEnabled(enabled)


# ---------------------------------------------------------------- helpers --
def write_csv(path, rows, a_label, b_label, target_radius_m=None, selection_text=""):
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["RPL event comparison"])
        writer.writerow(["A (reference)", a_label, "B (compared)", b_label])
        writer.writerow(["Selection", selection_text,
                         "Target radius (m)", "" if not target_radius_m else f"{target_radius_m:g}"])
        stats = ec.offset_stats(rows, target_radius_m)
        writer.writerow(["Summary", ec.summary_text(stats)])
        writer.writerow([])
        writer.writerow(list(ec.CSV_HEADER))
        writer.writerows(ec.csv_rows(rows, target_radius_m))


def build_offset_layer(rows, name, target_radius_m=None):
    """Memory layer of A→B lines (EPSG:4326) carrying the offsets."""
    from qgis.core import QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsVectorLayer

    layer = QgsVectorLayer("LineString?crs=EPSG:4326", name, "memory")
    provider = layer.dataProvider()
    fields = [
        ("a_event", FIELD_TYPE_STRING), ("b_event", FIELD_TYPE_STRING),
        ("event_type", FIELD_TYPE_STRING), ("match", FIELD_TYPE_STRING),
        ("a_kp", FIELD_TYPE_DOUBLE), ("b_kp", FIELD_TYPE_DOUBLE),
        ("kp_delta_m", FIELD_TYPE_DOUBLE), ("along_m", FIELD_TYPE_DOUBLE),
        ("cross_m", FIELD_TYPE_DOUBLE), ("radial_m", FIELD_TYPE_DOUBLE),
        ("bearing_deg", FIELD_TYPE_DOUBLE), ("in_target", FIELD_TYPE_STRING),
    ]
    provider.addAttributes([QgsField(n, t) for n, t in fields])
    layer.updateFields()
    features = []
    for row in rows:
        a, b = row.a, row.b
        if None in (a.lat, a.lon, b.lat, b.lon):
            continue
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(a.lon, a.lat), QgsPointXY(b.lon, b.lat)]))
        within = row.within(target_radius_m)
        feature.setAttributes([
            a.event, b.event, a.type_text, ec.HOW_LABELS.get(row.how, row.how),
            a.kp, b.kp, row.kp_delta_m, row.along_m, row.cross_m, row.radial_m,
            row.bearing_deg, None if within is None else ("yes" if within else "no"),
        ])
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def _row_key(row: ec.EventOffset):
    """Stable identity of a table row across recomputes (A event, else B event)."""
    return ("a", row.a.index) if row.a is not None else ("b", row.b.index if row.b else None)


def _km(value) -> str:
    return "" if value is None else f"{float(value):.3f}"


def _signed(value) -> str:
    return "" if value is None else f"{float(value):+.1f}"


def _unsigned(value) -> str:
    return "" if value is None else f"{float(value):.1f}"


def _distance_text(metres: float) -> str:
    return f"{metres / 1000.0:.2f} km" if metres >= 1000 else f"{metres:.0f} m"
