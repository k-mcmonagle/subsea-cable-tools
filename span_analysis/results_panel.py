# -*- coding: utf-8 -*-
"""Span & Slack Analysis results: findings, spans, bodies, detail model, find tension, tension sweep, summary."""

from __future__ import annotations

import html
import math
from typing import List, Optional, Sequence

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..cable_seabed import planning as pl
from .plot_panel import DirectAxis
from ..cable_seabed.core import ERROR, INFO, WARNING
from ..qgis_compat import (
    EDIT_TRIGGER_NONE,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
)

_USER_ROLE = getattr(getattr(Qt, "ItemDataRole", Qt), "UserRole")
_HORIZONTAL = getattr(getattr(Qt, "Orientation", Qt), "Horizontal")
_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
SEVERITY_TEXT = {ERROR: "Red", WARNING: "Amber", INFO: "Info"}
SEVERITY_COLOUR = {ERROR: "#d32f2f", WARNING: "#ff9800", INFO: "#1f77b4"}
FINDING_HEADERS = ["Level", "Check", "KP from", "KP to", "Length (m)", "Value", "Message"]
SPAN_HEADERS = ["#", "KP from", "KP to", "Length (m)", "Max gap (m)", "KP max gap", "Tension (kN)", "Tension from",
                "Min bend R (m)", "MBR (m)", "Reaction (kN)", "Max T for limits (kN)", "Bodies"]
BODY_HEADERS = ["Name", "Kind", "KP", "Weight (kN)", "Clear of seabed (m)", "In a span", "Tension (kN)",
                "Housing standoff (m)"]
DETAIL_HEADERS = ["KP from", "KP to", "Length (m)", "Max gap (m)", "Min bend R (m)", "KP of min R", "MBR (m)",
                  "Max moment (kNm)", "Reaction start (kN)", "Reaction end (kN)"]
FIND_HEADERS = ["Scope", "KP from", "KP to", "Highest tension (kN)", "Breaks the target first",
                "Spans at the lowest tension", "Seabed fill (%)"]
TARGETS = [
    ("report", "No reportable span (the reporting limits)"),
    ("red", "No red span (the red limits)"),
    ("none", "No span at all"),
    ("custom", "No span higher than … and longer than …"),
]
SCOPES = [("all", "Whole profile"), ("view", "Visible part of the profile"), ("sections", "Each lay control section")]
SWEEP_HEADERS = ["Bottom T (kN)", "Spans", "Red", "Total length (m)", "Longest (m)", "Max gap (m)",
                 "Min bend R (m)", "Below MBR", "Max residual T (kN)", "Above NPTS"]


def _axes():
    return {"left": DirectAxis("left"), "bottom": DirectAxis("bottom")}


def _num(value, digits: int = 2) -> str:
    if value is None:
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v == math.inf:
        return "∞"
    if not math.isfinite(v):
        return ""
    return f"{v:.{digits}f}"


_SORT_ROLE = int(getattr(_USER_ROLE, "value", _USER_ROLE)) + 1   # PyQt6 roles are enums


class SortItem(QTableWidgetItem):
    """A table cell that sorts numbers by value (and text as text)."""

    def __lt__(self, other):
        mine, theirs = self.data(_SORT_ROLE), other.data(_SORT_ROLE)
        if isinstance(mine, float) and isinstance(theirs, float):
            return mine < theirs
        if isinstance(mine, float) != isinstance(theirs, float):
            return isinstance(mine, float)   # numbers before blanks / text
        return self.text().lower() < other.text().lower()


def _sort_key(text: str):
    cleaned = str(text).replace(",", "").split(" ")[0]
    if cleaned == "∞":
        return math.inf
    try:
        return float(cleaned)
    except ValueError:
        return None


def _table(headers: Sequence[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setEditTriggers(EDIT_TRIGGER_NONE)
    table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
    table.setSelectionMode(SELECTION_MODE_SINGLE)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSortIndicatorShown(True)
    table.setToolTip("Click a column heading to sort by it.")
    return table


def _item(text, data=None, colour: Optional[str] = None) -> QTableWidgetItem:
    item = SortItem(str(text))
    key = _sort_key(text)
    if key is not None:
        item.setData(_SORT_ROLE, float(key))
    if data is not None:
        item.setData(_USER_ROLE, data)
    if colour:
        item.setForeground(QColor(colour))
    return item


def _fill(table: QTableWidget, rows: List[List], payloads: Optional[List] = None,
          after=None) -> None:
    """Replace the rows (sorting off while filling); ``after(table, r)`` runs per row
    before sorting is restored, so per-row data stays with its row."""
    header = table.horizontalHeader()
    column, order = header.sortIndicatorSection(), header.sortIndicatorOrder()
    table.setSortingEnabled(False)
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            colour = None
            if isinstance(value, tuple):
                value, colour = value
            table.setItem(r, c, _item(value, payloads[r] if (payloads is not None and c == 0) else None, colour))
        if after is not None:
            after(table, r)
    table.resizeColumnsToContents()
    table.setSortingEnabled(True)
    if 0 <= column < table.columnCount():
        table.sortItems(column, order)


class ResultsPanel(QTabWidget):
    findingSelected = pyqtSignal(object)
    findingActivated = pyqtSignal(object)
    rangeSelected = pyqtSignal(float, float)
    detailRequested = pyqtSignal(float, float, float)
    sweepRequested = pyqtSignal(object)
    findRequested = pyqtSignal(object)
    useTension = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result: Optional[pl.PlanningResult] = None
        self._findings: List = []
        self._build_findings()
        self._build_spans()
        self._build_bodies()
        self._build_detail()
        self._build_find()
        self._build_sweep()
        self.summary = QTextBrowser()
        self.summary.setOpenExternalLinks(False)
        self.addTab(self.summary, "Summary")

    # -- findings -------------------------------------------------------------------
    def _build_findings(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Show"))
        self.check_filter = QComboBox()
        self.check_filter.addItem("All checks", "")
        for key, label in pl.CHECK_LABELS.items():
            self.check_filter.addItem(label, key)
        self.check_filter.currentIndexChanged.connect(self._populate_findings)
        bar.addWidget(self.check_filter)
        self.red_only = QCheckBox("Red only")
        self.red_only.toggled.connect(self._populate_findings)
        bar.addWidget(self.red_only)
        bar.addStretch(1)
        self.findings_count = QLabel("")
        bar.addWidget(self.findings_count)
        layout.addLayout(bar)
        self.findings_table = _table(FINDING_HEADERS)
        self.findings_table.itemSelectionChanged.connect(self._on_finding_selected)
        self.findings_table.cellDoubleClicked.connect(self._on_finding_activated)
        layout.addWidget(self.findings_table)
        self.addTab(page, "Findings")

    def _populate_findings(self, *_args) -> None:
        check = self.check_filter.currentData()
        red = self.red_only.isChecked()
        shown = [f for f in self._findings if (not check or f.check_id == check) and (not red or f.severity == ERROR)]
        rows = []
        for f in shown:
            colour = SEVERITY_COLOUR.get(f.severity)
            rows.append([(SEVERITY_TEXT.get(f.severity, f.severity), colour),
                         pl.CHECK_LABELS.get(f.check_id, f.check_id), f"{f.kp_start:.3f}", f"{f.kp_end:.3f}",
                         _num(f.length_m, 0), (_num(f.value) + (f" {f.unit}" if f.unit else "")) if f.value is not None
                         else "", f.message])
        def tip(table, r):
            item = table.item(r, len(FINDING_HEADERS) - 1)
            if item is not None:
                item.setToolTip(shown[r].message)

        _fill(self.findings_table, rows, shown, tip)
        reds = sum(1 for f in self._findings if f.severity == ERROR)
        ambers = sum(1 for f in self._findings if f.severity == WARNING)
        self.findings_count.setText(f"{len(self._findings)} finding(s): {reds} red, {ambers} amber")

    def _finding_at(self, row: int):
        item = self.findings_table.item(row, 0)
        return item.data(_USER_ROLE) if item is not None else None

    def _on_finding_selected(self) -> None:
        rows = self.findings_table.selectionModel().selectedRows()
        if rows:
            finding = self._finding_at(rows[0].row())
            if finding is not None:
                self.findingSelected.emit(finding)

    def _on_finding_activated(self, row: int, _col: int) -> None:
        finding = self._finding_at(row)
        if finding is not None:
            self.findingActivated.emit(finding)

    # -- spans and bodies --------------------------------------------------------------
    def _build_spans(self) -> None:
        self.spans_table = _table(SPAN_HEADERS)
        self.spans_table.itemSelectionChanged.connect(lambda: self._emit_range(self.spans_table))
        self.addTab(self.spans_table, "Spans")

    def _build_bodies(self) -> None:
        self.bodies_table = _table(BODY_HEADERS)
        self.bodies_table.itemSelectionChanged.connect(lambda: self._emit_range(self.bodies_table))
        self.addTab(self.bodies_table, "Bodies")

    def _emit_range(self, table: QTableWidget) -> None:
        rows = table.selectionModel().selectedRows()
        if not rows:
            return
        item = table.item(rows[0].row(), 0)
        data = item.data(_USER_ROLE) if item is not None else None
        if isinstance(data, (tuple, list)) and len(data) == 2:
            self.rangeSelected.emit(float(data[0]), float(data[1]))

    def select_body(self, index: int) -> None:
        """Show body ``index`` (result order) in the Bodies tab."""
        for r in range(self.bodies_table.rowCount()):
            item = self.bodies_table.item(r, 1)
            if item is not None and item.data(_USER_ROLE) == index:
                self.setCurrentIndex(2)
                self.bodies_table.blockSignals(True)
                self.bodies_table.selectRow(r)
                self.bodies_table.blockSignals(False)
                self.bodies_table.scrollToItem(item)
                return

    def selected_span(self) -> Optional[pl.Span]:
        rows = self.spans_table.selectionModel().selectedRows()
        if not rows or self._result is None:
            return None
        index = rows[0].row()
        item = self.spans_table.item(index, 1)
        number = item.data(_USER_ROLE) if item is not None else None
        if number is None or not (0 <= int(number) < len(self._result.spans)):
            return None
        return self._result.spans[int(number)]

    # -- detail ----------------------------------------------------------------------------
    def _build_detail(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)
        bar = QHBoxLayout()
        self.detail_window = QComboBox()
        self.detail_window.addItem("Selected span (Spans tab)", "span")
        self.detail_window.addItem("Visible part of the profile", "view")
        self.detail_window.setToolTip("The KP window to solve with bending stiffness. A span is padded either side.")
        bar.addWidget(QLabel("Window"))
        bar.addWidget(self.detail_window)
        bar.addWidget(QLabel("Grid"))
        self.detail_spacing = QDoubleSpinBox()
        self.detail_spacing.setRange(0.0, 5.0)
        self.detail_spacing.setDecimals(3)
        self.detail_spacing.setSingleStep(0.01)
        self.detail_spacing.setSpecialValueText("auto")
        self.detail_spacing.setSuffix(" m")
        self.detail_spacing.setToolTip("Node spacing. Auto: a quarter of the bending length sqrt(EI / T).")
        bar.addWidget(self.detail_spacing)
        self.detail_button = QPushButton("Run detail model")
        self.detail_button.setToolTip("Solve the window as a tensioned beam (T and EI) resting on the seabed: "
                                      "bend radius, moment and seabed reaction along the cable.")
        self.detail_button.clicked.connect(self._request_detail)
        bar.addWidget(self.detail_button)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.detail_notes = QLabel("The detail model adds bending stiffness (EI) to the cable and treats bodies "
                                   "as rigid. Use it on a span, a crossing or a boulder.")
        self.detail_notes.setWordWrap(True)
        layout.addWidget(self.detail_notes)
        split = QSplitter(_HORIZONTAL)
        self.detail_graphics = pg.GraphicsLayoutWidget()
        self.detail_graphics.setBackground("w")
        self.d_profile = self.detail_graphics.addPlot(row=0, col=0, axisItems=_axes())
        self.d_radius = self.detail_graphics.addPlot(row=1, col=0, axisItems=_axes())
        self.d_moment = self.detail_graphics.addPlot(row=2, col=0, axisItems=_axes())
        for plot in (self.d_profile, self.d_radius, self.d_moment):
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getAxis("left").setWidth(64)
            plot.setAutoVisible(y=True)
            plot.addLegend(offset=(-5, 2), colCount=3, labelTextColor=(40, 40, 40), labelTextSize="8pt",
                           brush=pg.mkBrush(255, 255, 255, 190), verSpacing=-2, horSpacing=22)
        self.d_profile.invertY(True)
        self.d_profile.hideAxis("bottom")
        self.d_radius.hideAxis("bottom")
        self.d_profile.setLabel("left", "Depth (m)")
        self.d_radius.setLabel("left", "Bend R (m)")
        self.d_radius.setLogMode(y=True)
        self.d_moment.setLabel("left", "Moment (kNm)")
        self.d_moment.setLabel("bottom", "KP (km)")
        self.d_radius.setXLink(self.d_profile)
        self.d_moment.setXLink(self.d_profile)
        self.d_seabed = self.d_profile.plot([], [], pen=pg.mkPen((121, 85, 72), width=2), name="Seabed")
        self.d_cable = self.d_profile.plot([], [], pen=pg.mkPen((123, 31, 162), width=1.5), name="Cable (EI)")
        self.d_route = self.d_profile.plot([], [], pen=pg.mkPen((25, 70, 160), width=1, style=_DASH),
                                           name="Cable (route model)")
        self.d_r = self.d_radius.plot([], [], pen=pg.mkPen((123, 31, 162), width=1.5), name="Bend radius")
        self.d_mbr = self.d_radius.plot([], [], pen=pg.mkPen((211, 47, 47), width=1, style=_DASH), name="MBR")
        self.d_m = self.d_moment.plot([], [], pen=pg.mkPen((0, 137, 123), width=1.5), name="Moment")
        split.addWidget(self.detail_graphics)
        self.detail_table = _table(DETAIL_HEADERS)
        split.addWidget(self.detail_table)
        split.setSizes([650, 350])
        layout.addWidget(split, 1)
        self.detail_page = page
        self.addTab(page, "Detail (EI)")

    def _request_detail(self) -> None:
        self.detailRequested.emit(math.nan, math.nan, float(self.detail_spacing.value()))

    def detail_mode(self) -> str:
        return self.detail_window.currentData()

    def set_detail(self, detail, route_result: Optional[pl.PlanningResult]) -> None:
        if detail is None:
            for item in (self.d_seabed, self.d_cable, self.d_route, self.d_r, self.d_mbr, self.d_m):
                item.setData([], [])
            self.detail_table.setRowCount(0)
            return
        beam = detail.beam
        kp = detail.kp_km
        self.d_seabed.setData(kp, -beam.obstacle_m)
        self.d_cable.setData(kp, -beam.y_m)
        if route_result is not None:
            st = route_result.stations
            sel = (st.kp_km >= kp[0]) & (st.kp_km <= kp[-1])
            self.d_route.setData(st.kp_km[sel], -route_result.cable_m[sel])
        radius = np.where(detail.rigid, np.nan, beam.bend_radius_m)
        radius = np.where(np.isfinite(radius) & (radius < 1e5), radius, np.nan)
        self.d_r.setData(kp, radius, connect="finite")
        self.d_mbr.setData(kp, np.where(np.isfinite(detail.mbr_m), detail.mbr_m, np.nan), connect="finite")
        self.d_m.setData(kp, beam.moment_nm / 1000.0)
        rows, payloads = [], []
        for ds in detail.spans:
            s = ds.span
            red = "#d32f2f" if ds.below_mbr else None
            rows.append([f"{s.kp_start:.3f}", f"{s.kp_end:.3f}", _num(s.length_m, 1), _num(s.max_gap_m),
                         (_num(ds.min_bend_radius_m, 1), red), f"{ds.kp_min_radius:.4f}", _num(ds.mbr_m, 1),
                         _num(ds.max_moment_knm, 3), _num(ds.reaction_kn[0]), _num(ds.reaction_kn[1])])
            payloads.append((s.kp_start, s.kp_end))
        _fill(self.detail_table, rows, payloads)
        lam = detail.bending_length_m
        text = [f"Grid {beam.spacing_m:.3f} m, {len(kp):,} nodes, {beam.method}, {beam.iterations} iterations"
                + ("" if beam.converged else " (NOT converged)"),
                f"bending length sqrt(EI/T) {lam:.2f} m" if math.isfinite(lam) else "no bending stiffness",
                f"tightest bend {detail.min_bend_radius_m:.1f} m" if math.isfinite(detail.min_bend_radius_m)
                else ""]
        self.detail_notes.setText("; ".join(t for t in text if t) + (". " + " ".join(detail.notes)
                                                                      if detail.notes else "."))
        self.d_profile.enableAutoRange()
        self.d_radius.enableAutoRange()
        self.d_moment.enableAutoRange()

    def set_reverse(self, on: bool) -> None:
        """KP from right to left in the detail plots too (as on the profile)."""
        for plot in (self.d_profile, self.d_radius, self.d_moment):
            plot.invertX(bool(on))

    # -- find tension --------------------------------------------------------------------------
    def _build_find(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)
        intro = QLabel("The highest bottom tension that keeps the spans within a target. Spans shorten as the "
                       "tension falls, so this is a ceiling: lay at or below it. Each tension is solved in full "
                       "(friction, slopes, bodies and protection).")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.find_target = QComboBox()
        for key, label in TARGETS:
            self.find_target.addItem(label, key)
        self.find_gap = QDoubleSpinBox()
        self.find_length = QDoubleSpinBox()
        for spin, value, suffix in ((self.find_gap, 0.3, " m high"), (self.find_length, 5.0, " m long")):
            spin.setRange(0.0, 1000.0)
            spin.setDecimals(2)
            spin.setSuffix(suffix)
            spin.setValue(value)
        self.find_mbr = QCheckBox("bend at supports within the MBR")
        self.find_mbr.setChecked(True)
        self.find_contact = QCheckBox("support reaction within the contact limit")
        self.find_contact.setChecked(True)
        self.find_contact.setToolTip("Uses the contact limit in Reporting limits (when it is set).")
        for label, widget in (("Target", self.find_target), (None, self.find_gap), (None, self.find_length),
                              (None, self.find_mbr), (None, self.find_contact)):
            if label:
                row.addWidget(QLabel(label))
            row.addWidget(widget)
        row.addStretch(1)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.find_scope = QComboBox()
        for key, label in SCOPES:
            self.find_scope.addItem(label, key)
        self.find_hi = QDoubleSpinBox()
        self.find_hi.setRange(0.5, 1000.0)
        self.find_hi.setDecimals(1)
        self.find_hi.setSuffix(" kN")
        self.find_hi.setValue(50.0)
        self.find_hi.setToolTip("The search runs from just above the conformity tension to this.")
        self.find_button = QPushButton("Find tension")
        self.find_button.clicked.connect(self._request_find)
        self.find_use = QPushButton("Use for the lay control")
        self.find_use.setToolTip("Set the lay control to these bottom tensions (rounded down to 0.05 kN).")
        self.find_use.setEnabled(False)
        self.find_use.clicked.connect(self._use_found)
        for label, widget in (("Over", self.find_scope), ("search up to", self.find_hi)):
            row.addWidget(QLabel(label))
            row.addWidget(widget)
        row.addWidget(self.find_button)
        row.addWidget(self.find_use)
        row.addStretch(1)
        layout.addLayout(row)
        self.find_table = _table(FIND_HEADERS)
        layout.addWidget(self.find_table, 1)
        self.find_note = QLabel("")
        self.find_note.setWordWrap(True)
        layout.addWidget(self.find_note)
        self.find_target.currentIndexChanged.connect(self._on_find_target)
        self._found: List[pl.TensionAnswer] = []
        self._found_scope = "all"
        self._on_find_target()
        self.find_page = page
        self.addTab(page, "Find tension")

    def _on_find_target(self, *_args) -> None:
        custom = self.find_target.currentData() == "custom"
        self.find_gap.setEnabled(custom)
        self.find_length.setEnabled(custom)

    def _request_find(self) -> None:
        self.findRequested.emit({"target": self.find_target.currentData(), "gap_m": self.find_gap.value(),
                                 "length_m": self.find_length.value(), "mbr": self.find_mbr.isChecked(),
                                 "contact": self.find_contact.isChecked(), "scope": self.find_scope.currentData(),
                                 "hi_kn": self.find_hi.value()})

    def show_find(self) -> None:
        self.setCurrentWidget(self.find_page)

    def set_found(self, answers: Sequence[pl.TensionAnswer], scope: str = "all", target_text: str = "") -> None:
        self._found = list(answers)
        self._found_scope = scope
        rows, payloads = [], []
        for a in answers:
            if a.tension_kn <= 0:
                value = ("none", "#d32f2f")
            elif not a.limited:
                value = f"over {a.tension_kn:.1f}"
            else:
                value = _num(a.tension_kn)
            remaining = ", ".join(f"{s.kp_start:.3f}–{s.kp_end:.3f}" for s in a.remaining[:6])
            if len(a.remaining) > 6:
                remaining += f" (+{len(a.remaining) - 6})"
            rows.append([a.name, f"{a.kp_start:.3f}", f"{a.kp_end:.3f}", value,
                         a.governing or ("met at every tension searched" if not a.limited else ""),
                         remaining, _num(a.fill_pct, 3)])
            payloads.append((a.kp_start, a.kp_end))
        _fill(self.find_table, rows, payloads)
        usable = [a for a in answers if a.tension_kn > 0]
        self.find_use.setEnabled(bool(usable))
        notes = []
        if target_text:
            notes.append(f"Target: {target_text}.")
        if any(a.tension_kn <= 0 for a in answers):
            notes.append("'none': spans break the target even at the lowest tension searched (e.g. a crossing "
                         "always spans): change the target, the route or the seabed (e.g. a berm or a rock ramp).")
        notes.extend(sorted({a.note for a in answers if a.note}))
        if answers:
            notes.append("Slack: to follow the seabed the cable needs at least the seabed fill over the route "
                         "(last column), laid at near-zero tension. The floor of the tension window comes from "
                         "the loop and slope checks (Findings), not from spans.")
        self.find_note.setText(" ".join(notes))

    def _use_found(self) -> None:
        answers = [a for a in self._found if a.tension_kn > 0]
        if answers:
            self.useTension.emit({"scope": self._found_scope, "answers": answers})

    def clear_found(self) -> None:
        self.set_found([])

    # -- sweep -------------------------------------------------------------------------------
    def _build_sweep(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)
        form = QHBoxLayout()
        self.sweep_from = QDoubleSpinBox()
        self.sweep_to = QDoubleSpinBox()
        for spin, value in ((self.sweep_from, 0.5), (self.sweep_to, 20.0)):
            spin.setRange(0.01, 500.0)
            spin.setDecimals(2)
            spin.setSuffix(" kN")
            spin.setValue(value)
        self.sweep_steps = QSpinBox()
        self.sweep_steps.setRange(3, 60)
        self.sweep_steps.setValue(15)
        self.sweep_window = QComboBox()
        self.sweep_window.addItem("Whole profile", "all")
        self.sweep_window.addItem("Visible part of the profile", "view")
        for label, widget in (("Bottom tension from", self.sweep_from), ("to", self.sweep_to),
                              ("steps", self.sweep_steps), ("over", self.sweep_window)):
            form.addWidget(QLabel(label))
            form.addWidget(widget)
        self.sweep_button = QPushButton("Run sweep")
        self.sweep_button.setToolTip("Lay the cable at a constant bottom tension at each step (friction and slopes "
                                     "included) and count the spans: how much tension the seabed tolerates.")
        self.sweep_button.clicked.connect(self._request_sweep)
        form.addWidget(self.sweep_button)
        form.addStretch(1)
        layout.addLayout(form)
        split = QSplitter(_HORIZONTAL)
        self.sweep_graphics = pg.GraphicsLayoutWidget()
        self.sweep_graphics.setBackground("w")
        self.s_length = self.sweep_graphics.addPlot(row=0, col=0, axisItems=_axes())
        self.s_gap = self.sweep_graphics.addPlot(row=1, col=0, axisItems=_axes())
        for plot in (self.s_length, self.s_gap):
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getAxis("left").setWidth(64)
            plot.addLegend(offset=(5, 2), colCount=2, labelTextColor=(40, 40, 40), labelTextSize="8pt",
                           brush=pg.mkBrush(255, 255, 255, 190), verSpacing=-2, horSpacing=22)
        self.s_gap.setXLink(self.s_length)
        self.s_length.hideAxis("bottom")
        for plot in (self.s_length, self.s_gap):
            plot.setLogMode(x=True)
        self.s_length.setLabel("left", "Span length (m)")
        self.s_gap.setLabel("left", "Max gap (m)")
        self.s_gap.setLabel("bottom", "Bottom tension (kN)")
        sym = {"symbol": "o", "symbolSize": 6}
        self.s_total = self.s_length.plot([], [], pen=pg.mkPen((25, 70, 160), width=1.5), name="Total",
                                          symbolBrush=(25, 70, 160), **sym)
        self.s_longest = self.s_length.plot([], [], pen=pg.mkPen((211, 47, 47), width=1.5), name="Longest",
                                            symbolBrush=(211, 47, 47), **sym)
        self.s_maxgap = self.s_gap.plot([], [], pen=pg.mkPen((121, 85, 72), width=1.5), name="Max gap",
                                        symbolBrush=(121, 85, 72), **sym)
        split.addWidget(self.sweep_graphics)
        self.sweep_table = _table(SWEEP_HEADERS)
        split.addWidget(self.sweep_table)
        split.setSizes([550, 450])
        layout.addWidget(split, 1)
        self.sweep_note = QLabel("With a uniform cable, a constant tension and no friction, spans only grow as the "
                                 "tension rises (Binda & Contro 1979). Slopes, friction and bodies can bend that.")
        self.sweep_note.setWordWrap(True)
        layout.addWidget(self.sweep_note)
        self.sweep_page = page
        self.addTab(page, "Tension sweep")

    def _request_sweep(self) -> None:
        lo, hi = sorted((self.sweep_from.value(), self.sweep_to.value()))
        steps = int(self.sweep_steps.value())
        tensions = list(np.geomspace(lo, hi, steps)) if hi > lo else [lo]
        self.sweepRequested.emit({"tensions": tensions, "window": self.sweep_window.currentData()})

    def set_sweep(self, rows: Sequence[pl.SweepRow]) -> None:
        t = np.array([r.tension_kn for r in rows])
        self.s_total.setData(t, np.array([r.span_length_m for r in rows]))
        self.s_longest.setData(t, np.array([r.longest_span_m for r in rows]))
        self.s_maxgap.setData(t, np.array([r.max_gap_m for r in rows]))
        table_rows = []
        for r in rows:
            table_rows.append([_num(r.tension_kn), str(r.spans), (str(r.red_spans), "#d32f2f" if r.red_spans else None),
                               _num(r.span_length_m, 0), _num(r.longest_span_m, 1), _num(r.max_gap_m),
                               _num(r.min_bend_radius_m, 1), str(r.below_mbr), _num(r.max_tension_kn),
                               ("yes", "#d32f2f") if r.over_npts else "no"])
        _fill(self.sweep_table, table_rows)
        self.s_length.enableAutoRange()
        self.s_gap.enableAutoRange()

    def clear_sweep(self) -> None:
        self.set_sweep([])

    # -- result -------------------------------------------------------------------------------
    def set_result(self, result: Optional[pl.PlanningResult], summary_html: str = "") -> None:
        self._result = result
        self._findings = list(result.findings) if result is not None else []
        self._populate_findings()
        span_rows, span_payloads = [], []
        body_rows, body_payloads = [], []
        if result is not None:
            for k, s in enumerate(result.spans):
                red = pl._span_red(s, result.criteria)
                bodies = ", ".join(b.body.name for b in result.bodies if b.in_span and s.kp_start <= b.kp_km <= s.kp_end)
                span_rows.append([(str(k + 1), "#d32f2f" if red else None), f"{s.kp_start:.3f}", f"{s.kp_end:.3f}",
                                  _num(s.length_m, 1), _num(s.max_gap_m), f"{s.kp_max_gap:.3f}", _num(s.tension_kn),
                                  s.source, (_num(s.min_bend_radius_m, 1), "#d32f2f" if s.below_mbr else None),
                                  _num(s.mbr_m, 1), _num(s.max_reaction_kn), _num(s.allowable_kn), bodies])
                span_payloads.append((s.kp_start, s.kp_end))
            for b in result.bodies:
                body_rows.append([b.body.name, b.body.kind, f"{b.kp_km:.3f}", _num(b.body.weight_kn),
                                  (_num(b.max_gap_m), "#ff9800" if b.max_gap_m > result.criteria.min_gap_m else None),
                                  "yes" if b.in_span else "no", _num(b.tension_kn), _num(b.standoff_m, 3)])
                half = max(b.body.length_m, 10.0) / 2000.0
                body_payloads.append((b.kp_km - half, b.kp_km + half))
        def number(table, r):   # keep the span number for the detail model
            item = table.item(r, 1)
            if item is not None:
                item.setData(_USER_ROLE, r)

        def body_index(table, r):
            item = table.item(r, 1)
            if item is not None:
                item.setData(_USER_ROLE, r)

        _fill(self.spans_table, span_rows, span_payloads, number)
        _fill(self.bodies_table, body_rows, body_payloads, body_index)
        self.setTabText(1, f"Spans ({len(span_rows)})" if result is not None else "Spans")
        self.setTabText(2, f"Bodies ({len(body_rows)})" if result is not None else "Bodies")
        self.summary.setHtml(summary_html)


def summary_html(result: pl.PlanningResult, notes: Sequence[str], setup_lines: Sequence[str],
                 verification=None) -> str:
    """The Summary tab: what was modelled and what came out (and a closed-form check when there is one)."""
    s = result.summary
    c = result.criteria
    esc = html.escape
    rows = [
        ("Route length (plan)", f"{s.route_m:,.0f} m"),
        ("Seabed length", f"{s.seabed_m:,.0f} m (fill {s.fill_pct:.3f}%)"),
        ("Cable laid", f"{s.cable_laid_m:,.0f} m (slack {s.laid_slack_pct:.3f}% over the route)"),
        ("Cable used by the rest shape", f"{s.cable_shape_m:,.0f} m"),
        ("Spans", f"{s.spans} ({s.red_spans} red), {s.span_length_m:,.0f} m in total, longest "
                  f"{s.longest_span_m:.1f} m, highest {s.max_gap_m:.2f} m"),
        ("Tightest bend at a support", "none" if s.min_bend_radius_m == math.inf else
         f"{s.min_bend_radius_m:.1f} m ({s.below_mbr} support(s) below the MBR)"),
        ("Largest support reaction", f"{s.max_reaction_kn:.2f} kN"),
        ("Residual tension", f"mean {s.mean_tension_kn:.2f} kN, max {s.max_tension_kn:.2f} kN"
                             + (" (above NPTS)" if s.over_npts else "")),
        ("Reporting limits", f"spans higher than {c.min_gap_m:g} m and longer than {c.min_length_m:g} m, from "
                             + ("lift-off to touchdown" if c.touchdown_gap_m <= 0 else
                                f"where the gap passes {c.touchdown_gap_m:g} m")
                             + f"; red at {c.red_gap_m:g} m high, {c.red_length_m:g} m long, below the MBR"
                             + (f", or a support reaction over {c.max_reaction_kn:g} kN" if c.max_reaction_kn > 0
                                else "")),
        ("Model iterations", str(result.iterations)),
    ]
    parts = ["<h3>Span &amp; Slack Analysis</h3>", "<table cellspacing='0' cellpadding='3'>"]
    for key, value in rows:
        parts.append(f"<tr><td><b>{esc(key)}</b></td><td>{esc(value)}</td></tr>")
    parts.append("</table>")
    if verification is not None:
        colour = "#2e7d32" if verification.ok else "#d32f2f"
        parts.append(f"<h4>Verification against the closed form</h4><p>{esc(verification.title)}: the cable "
                     f"{esc(verification.regime)}. <span style='color:{colour}'><b>"
                     f"{'Agrees' if verification.ok else 'Differs'}</b></span> within 3%.</p>")
        parts.append("<table cellspacing='0' cellpadding='3' border='1' style='border-collapse:collapse'>"
                     "<tr><th>Quantity</th><th>Closed form</th><th>Model</th><th>Difference</th></tr>")
        for row in verification.rows:
            diff = row.difference_pct
            diff_text = "" if not math.isfinite(diff) else f"{diff:+.1f}%"
            fmt = "{:.0f}" if row.unit == "" else "{:.3f}"
            parts.append(f"<tr><td>{esc(row.quantity)}</td><td>{fmt.format(row.closed_form)} {esc(row.unit)}</td>"
                         f"<td>{fmt.format(row.model)} {esc(row.unit)}</td><td>{diff_text}</td></tr>")
        parts.append("</table><p><i>Closed form: the small-slope parabola at a constant tension (Binda &amp; Contro "
                     "1979). The model uses the catenary curvature and lets the tension fall by w&middot;d into the "
                     "trough (T - w y is constant in a span), so its sag reads slightly more and its spans slightly "
                     "shorter, by up to about w d / 2T.</i></p>")
    if setup_lines:
        parts.append("<h4>Inputs</h4><ul>" + "".join(f"<li>{esc(line)}</li>" for line in setup_lines) + "</ul>")
    if notes:
        parts.append("<h4>Notes</h4><ul>" + "".join(f"<li>{esc(n)}</li>" for n in notes) + "</ul>")
    parts.append("<p><i>Screening and planning results from a 2D static model (see Help). Not validated against "
                 "field data or other software: confirm critical spans by survey and detailed analysis.</i></p>")
    return "".join(parts)
