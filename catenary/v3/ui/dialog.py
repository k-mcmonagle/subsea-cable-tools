# -*- coding: utf-8 -*-
"""Cable Lay Simulator (3D) — main dialog.

Thin shell: builds the input sections (left), the 3D/profile/plan views and
results pane (right), persists settings declaratively, and delegates all
computation to :mod:`solve_controller` on a worker thread. No physics here.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    from qgis.PyQt import QtCore, QtGui
    from qgis.PyQt.QtCore import Qt, QSettings
    from qgis.PyQt.QtWidgets import (
        QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
        QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
        QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget,
        QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QToolButton,
        QVBoxLayout, QWidget,
    )
except Exception:  # pragma: no cover - standalone testing
    from PyQt5 import QtCore, QtGui
    from PyQt5.QtCore import Qt, QSettings
    from PyQt5.QtWidgets import (
        QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
        QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
        QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget,
        QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QToolButton,
        QVBoxLayout, QWidget,
    )

from .results_panel import render_results_html
from .scene import compass_to_math_deg
from .solve_controller import RunOutput, SolveWorker, V3Config
from .view3d import View3DWidget
from .views2d import PlanView, ProfileView

_ORIENT = getattr(Qt, "Orientation", Qt)
_ARROW = getattr(Qt, "ArrowType", Qt)

MODES = [("static", "Static hang"), ("steady", "Steady lay"), ("operation", "Operation simulation")]
SOLVE_MODES = [
    ("bottom_tension", "Bottom tension (kN)"),
    ("top_tension", "Top tension (kN)"),
    ("exit_angle", "Exit angle (deg from horizontal)"),
    ("layback", "Layback (m)"),
    ("suspended_length", "Suspended length (m)"),
]
SCENARIOS = [
    ("bu_deployment", "Branching-unit deployment (lowering only)"),
    ("bu_full", "BU deployment — full (two-sheave)"),
    ("final_bight", "Final bight lay-down"),
    ("straight_lay", "Straight lay (transient)"),
]

# Schedule table columns (bu_full operation page).
SCH_COL_LABEL, SCH_COL_EVENT, SCH_COL_DUR, SCH_COL_COURSE, SCH_COL_SPEED, \
    SCH_COL_LEG1, SCH_COL_LEG2, SCH_COL_TRUNK = range(8)
SCH_HEADERS = ["Phase", "Event", "Duration\n(s)", "Course\n(degN)",
               "Speed\n(kn)", "Leg 1\n(m/s)", "Leg 2\n(m/s)", "Trunk\n(m/s)"]

# Assembly table columns.
COL_TYPE, COL_NAME, COL_LEN, COL_QW, COL_QA, COL_LOAD, COL_MU, COL_EI, COL_MBR, \
    COL_DIA, COL_CDN, COL_CDT, COL_COLOR = range(13)
ASM_HEADERS = ["Type", "Name", "Length\n(m)", "Wt water\n(N/m)", "Wt air\n(N/m)",
               "Load\n(kN)", "Friction\nmu", "EI\n(kN.m2)", "MBR\n(m)",
               "Dia (m) /\nCdA (m2)", "Cd\nnormal", "Cd\ntangential", "Color"]
# Columns that only apply to one row type (greyed out on the other).
_SEG_ONLY_COLS = (COL_LEN, COL_QW, COL_QA, COL_MU, COL_EI, COL_MBR, COL_CDN, COL_CDT)
_BODY_ONLY_COLS = (COL_LOAD,)


class _TableResizeGrip(QWidget):
    """A thin drag handle placed below a table so the user can set the table's
    height by dragging, instead of being locked to the auto-computed size."""

    def __init__(self, table, min_h=60, parent=None):
        super().__init__(parent)
        self._table = table
        self._min_h = min_h
        self._press_y = None
        self._start_h = 0
        self.setFixedHeight(11)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip("Drag to resize the table height")

    def paintEvent(self, _e):
        try:
            p = QtGui.QPainter(self)
            w = self.width()
            cy = self.height() // 2
            p.setPen(QtGui.QColor(150, 150, 150))
            for dx in (-14, -5, 4, 13):
                cx = w // 2 + dx
                p.drawLine(cx, cy - 1, cx + 3, cy - 1)
                p.drawLine(cx, cy + 1, cx + 3, cy + 1)
            p.end()
        except Exception:
            pass

    def mousePressEvent(self, e):
        try:
            self._press_y = e.globalY()
        except Exception:
            self._press_y = int(e.globalPosition().y())
        self._start_h = self._table.height()

    def mouseMoveEvent(self, e):
        if self._press_y is None:
            return
        try:
            gy = e.globalY()
        except Exception:
            gy = int(e.globalPosition().y())
        new_h = max(self._min_h, self._start_h + (gy - self._press_y))
        self._table._manual_height = True
        self._table.setMinimumHeight(new_h)
        self._table.setMaximumHeight(new_h)

    def mouseReleaseEvent(self, _e):
        self._press_y = None


class LaySimulatorDialog(QDialog):
    """Cable Lay Simulator (3D) — beta."""

    # Secondary sections start collapsed on first run (saved state wins).
    _DEFAULT_COLLAPSED = {"ship_shape", "advanced", "display"}

    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("Cable Lay Simulator (3D) — beta")
        self.setWindowFlags(self.windowFlags() | getattr(Qt, "WindowType", Qt).WindowMaximizeButtonHint
                            if hasattr(Qt, "WindowType") else self.windowFlags())
        self.settings = QSettings("subsea_cable_tools", "CableLaySimulatorV3")
        self._registry: List[Tuple[str, QWidget]] = []
        self._collapsibles: Dict[str, Tuple[QToolButton, QWidget]] = {}
        self._initializing = True
        self._worker: Optional[SolveWorker] = None
        self._pending: bool = False
        self._last_out: Optional[RunOutput] = None
        self._grid_bathy: Optional[dict] = None       # sampled raster grid cfg
        self._grid_origin: Optional[dict] = None      # map origin/crs for export
        self._picked_centre: Optional[Tuple[float, float]] = None  # map-CRS local origin
        self._origin_set: bool = False                # origin explicitly chosen
        self._pick_tool = None                        # active PointSequenceTool
        self._map_overlay = None                      # SimulatorMapOverlay
        self._dirty = False                           # inputs changed since last solve
        self._solve_origin = None                     # (origin, crs) captured at solve start
        self._scene_origin = None                     # origin the shown scene was solved with

        self._build_ui()
        self._capture_defaults()
        self._restore_settings()
        self._initializing = False
        self._on_mode_changed()
        # No auto-solve on open: restored inputs can describe an expensive
        # scenario, so wait for an explicit click.
        self._set_dirty(True)
        self.dirty_label.setText("Ready — review the inputs and click "
                                 + self.run_btn.text() + ".")
        self.resize(1280, 820)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QHBoxLayout(self)
        self.main_split = QSplitter(getattr(_ORIENT, "Horizontal", 1))
        try:
            self.main_split.setChildrenCollapsible(False)
        except Exception:
            pass
        root.addWidget(self.main_split)

        # ---- Left: inputs in a scroll area
        left_holder = QWidget()
        form = QVBoxLayout(left_holder)
        form.setContentsMargins(4, 4, 8, 4)

        # Workflow order: what (scenario) -> where (position) -> environment
        # -> cable -> vessel -> scenario properties -> what to solve /
        # operation script, then secondary sections (drawing, advanced
        # solver, display) collapsed by default.
        form.addWidget(self._section_mode())
        form.addWidget(self._section_position())
        form.addWidget(self._section_environment())
        form.addWidget(self._section_assembly())
        form.addWidget(self._section_vessel())
        form.addWidget(self._section_bu_bight())
        form.addWidget(self._section_solve())
        form.addWidget(self._section_operation())
        form.addWidget(self._section_ship_shape())
        form.addWidget(self._section_advanced())
        form.addWidget(self._section_display())
        reset_btn = QPushButton("Reset all inputs to defaults...")
        reset_btn.setToolTip("Restore every input, table and section to its "
                             "factory default (asks for confirmation).")
        reset_btn.clicked.connect(self._reset_defaults)
        form.addWidget(reset_btn)
        form.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left_holder)
        # Keep the input panel wide enough that fields are never clipped
        # (the V2-on-Qt5 lesson); a scrollbar appears if space runs out.
        scroll.setMinimumWidth(430)
        left_holder.setMinimumWidth(470)
        self.main_split.addWidget(scroll)

        # ---- Right: views + results
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 4, 4, 4)

        self.tabs = QTabWidget()
        self.view3d = View3DWidget()
        self.profile_view = ProfileView()
        self.plan_view = PlanView()
        self.tabs.addTab(self.view3d, "3D")
        self.tabs.addTab(self.profile_view.widget(), "Profile")
        self.tabs.addTab(self.plan_view.widget(), "Plan")
        from .timeseries_view import TimeSeriesView
        self.timeseries_view = TimeSeriesView()
        self.tabs.addTab(self.timeseries_view.widget(), "Time series")

        self.right_split = QSplitter(getattr(_ORIENT, "Vertical", 2))
        try:
            self.right_split.setChildrenCollapsible(False)
        except Exception:
            pass
        self.right_split.addWidget(self.tabs)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)

        # Timeline scrubber (operation mode).
        scrub_row = QHBoxLayout()
        self.scrub_label = QLabel("t = 0 s")
        self.scrubber = QSlider(getattr(_ORIENT, "Horizontal", 1))
        self.scrubber.setMinimum(0)
        self.scrubber.setMaximum(0)
        self.scrubber.valueChanged.connect(self._on_scrub)
        self.play_btn = QToolButton()
        self.play_btn.setText("▶")
        self.play_btn.setCheckable(True)
        self.play_btn.setToolTip("Play / pause the operation timeline.")
        self.play_btn.toggled.connect(self._on_play_toggled)
        self._play_timer = QtCore.QTimer(self)
        self._play_timer.setInterval(120)
        self._play_timer.timeout.connect(self._play_tick)
        scrub_row.addWidget(QLabel("Timeline:"))
        scrub_row.addWidget(self.play_btn)
        scrub_row.addWidget(self.scrubber, 1)
        scrub_row.addWidget(self.scrub_label)
        self.scrub_widget = QWidget()
        self.scrub_widget.setLayout(scrub_row)
        self.scrub_widget.setVisible(False)
        bl.addWidget(self.scrub_widget)

        # Explicit solve controls: results only update on request, with an
        # indicator once any input differs from the last solved state.
        solve_row = QHBoxLayout()
        self.run_btn = QPushButton("Solve")
        self.run_btn.clicked.connect(self._solve_clicked)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_worker)
        self.dirty_label = QLabel("")
        self.dirty_label.setStyleSheet("color:#c07f00; font-weight:bold;")
        self.op_progress = QProgressBar()
        self.op_progress.setRange(0, 100)
        solve_row.addWidget(self.run_btn)
        solve_row.addWidget(self.cancel_btn)
        solve_row.addWidget(self.dirty_label, 1)
        solve_row.addWidget(self.op_progress, 1)
        bl.addLayout(solve_row)

        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setMinimumHeight(90)
        bl.addWidget(self.results, 1)

        self.hover_label = QLabel(" ")
        self.hover_label.setStyleSheet("color:#555;")
        bl.addWidget(self.hover_label)

        btn_row = QHBoxLayout()
        self.btn_csv = QPushButton("Export CSV...")
        self.btn_sched_csv = QPushButton("Export ops schedule...")
        self.btn_sched_csv.setToolTip(
            "Operation runs: time / vessel position / payout per line / "
            "tensions as a CSV the lay crew can follow.")
        self.btn_dxf = QPushButton("Export DXF (3D)...")
        self.btn_map = QPushButton("Send to map")
        self.btn_csv.clicked.connect(self._export_csv)
        self.btn_sched_csv.clicked.connect(self._export_schedule_csv)
        self.btn_dxf.clicked.connect(self._export_dxf)
        self.btn_map.clicked.connect(self._send_to_map)
        for b in (self.btn_csv, self.btn_sched_csv, self.btn_dxf, self.btn_map):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        bl.addLayout(btn_row)

        self.right_split.addWidget(bottom)
        self.right_split.setStretchFactor(0, 3)
        self.right_split.setStretchFactor(1, 1)
        rv.addWidget(self.right_split)
        self.main_split.addWidget(right)
        self.main_split.setStretchFactor(0, 0)
        self.main_split.setStretchFactor(1, 1)
        self.main_split.setSizes([500, 780])

        self.view3d.hoverInfo.connect(self._on_hover)

    # ---- collapsible-section helper (V2 pattern) --------------------------

    def _collapsible(self, title: str, key: str) -> Tuple[QWidget, QFormLayout]:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 2, 0, 2)
        btn = QToolButton()
        btn.setText(title)
        btn.setCheckable(True)
        btn.setChecked(True)
        btn.setAutoRaise(True)
        try:
            btn.setToolButtonStyle(getattr(Qt, "ToolButtonStyle", Qt).ToolButtonTextBesideIcon)
            btn.setArrowType(getattr(_ARROW, "DownArrow", 2))
        except Exception:
            pass
        btn.setStyleSheet("QToolButton { font-weight: bold; }")
        body = QWidget()
        lay = QFormLayout(body)
        lay.setContentsMargins(14, 2, 2, 4)

        def toggle(checked):
            body.setVisible(checked)
            try:
                btn.setArrowType(getattr(_ARROW, "DownArrow", 2) if checked else getattr(_ARROW, "RightArrow", 4))
            except Exception:
                pass
            if not self._initializing:
                self.settings.setValue(f"section_{key}", "1" if checked else "0")

        btn.toggled.connect(toggle)
        v.addWidget(btn)
        v.addWidget(body)
        self._collapsibles[key] = (btn, body)
        if key in self._DEFAULT_COLLAPSED:
            btn.setChecked(False)
            body.setVisible(False)
        return box, lay

    def _auto_size_table(self, table: QTableWidget, min_rows: int = 3, max_rows: int = 8,
                         stretch_last: bool = True):
        """Size a table to show its rows (between min_rows and max_rows) so
        several rows are visible at once; beyond max_rows it scrolls
        internally, and the whole left panel scrolls anyway.

        The auto-size only applies until the user drags the resize grip below
        the table (which sets ``table._manual_height``); after that the height
        is left to the user."""

        table._manual_height = False

        # Row height derived from the current font so text is never clipped,
        # with a comfortable floor for readability.
        try:
            fm = table.fontMetrics()
            row_h = max(34, fm.height() + 14)
        except Exception:
            row_h = 34
        try:
            vh = table.verticalHeader()
            vh.setMinimumSectionSize(row_h)
            vh.setDefaultSectionSize(row_h)
            vh.setSectionResizeMode(QHeaderView.Fixed)
        except Exception:
            pass

        # Let the user drag column dividers; the last column fills the rest.
        try:
            hh = table.horizontalHeader()
            hh.setSectionResizeMode(QHeaderView.Interactive)
            hh.setStretchLastSection(stretch_last)
            hh.setMinimumSectionSize(40)
        except Exception:
            pass

        def update(*_a):
            if getattr(table, "_manual_height", False):
                return
            rows = max(min_rows, min(max_rows, table.rowCount()))
            try:
                rh = table.verticalHeader().defaultSectionSize() or row_h
            except Exception:
                rh = row_h
            try:
                hh = table.horizontalHeader().sizeHint().height() or 24
            except Exception:
                hh = 24
            h = hh + rows * rh + 2 * table.frameWidth() + 20  # + h-scrollbar room
            table.setMinimumHeight(h)
            table.setMaximumHeight(h)

        try:
            table.model().rowsInserted.connect(update)
            table.model().rowsRemoved.connect(update)
        except Exception:
            pass
        update()

    def _add_table_grip(self, table: QTableWidget, layout):
        """Add a draggable resize handle directly beneath ``table``."""
        try:
            layout.addWidget(_TableResizeGrip(table))
        except Exception:
            pass

    def _fit_columns(self, table: QTableWidget, padding: int = 18,
                     min_w: int = 44, max_w: int = 340):
        """Size each column to fit the wider of its (possibly multi-line)
        header text or its current content. Font-metrics based, so it scales
        with the display DPI. Columns stay Interactive, so the user can drag
        any divider afterwards to override."""
        try:
            fm = table.horizontalHeader().fontMetrics()
        except Exception:
            return
        for c in range(table.columnCount()):
            it = table.horizontalHeaderItem(c)
            htext = it.text() if it is not None else ""
            hw = 0
            for line in (htext or "").split("\n"):
                try:
                    lw = fm.horizontalAdvance(line)
                except Exception:
                    lw = fm.width(line)
                hw = max(hw, lw)
            try:
                cw = table.sizeHintForColumn(c)
            except Exception:
                cw = 0
            w = max(min_w, min(max_w, max(hw, cw) + padding))
            table.setColumnWidth(c, w)

    # ---- input widget helpers ---------------------------------------------

    def _dspin(self, key, lo, hi, val, step=1.0, dec=2, suffix="") -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setDecimals(dec)
        w.setSingleStep(step)
        w.setValue(val)
        if suffix:
            w.setSuffix(suffix)
        w.setMinimumWidth(110)
        w.valueChanged.connect(self._schedule)
        self._registry.append((key, w))
        return w

    def _coord_spin(self, key) -> QDoubleSpinBox:
        """A wide-range map-coordinate spin box (project CRS easting/northing).

        Not wired to ``_schedule`` on every keystroke — the origin is committed
        on ``editingFinished`` via :meth:`_origin_boxes_changed`. Registered so
        the last coordinates persist across sessions."""
        w = QDoubleSpinBox()
        w.setRange(-1.0e12, 1.0e12)
        w.setDecimals(2)
        w.setSingleStep(10.0)
        w.setMinimumWidth(140)
        try:
            w.setGroupSeparatorShown(True)
        except Exception:
            pass
        self._registry.append((key, w))
        return w

    def _combo(self, key, entries) -> QComboBox:
        w = QComboBox()
        for data, label in entries:
            w.addItem(label, data)
        w.currentIndexChanged.connect(self._schedule)
        self._registry.append((key, w))
        return w

    def _check(self, key, text, val=False) -> QCheckBox:
        w = QCheckBox(text)
        w.setChecked(val)
        w.toggled.connect(self._schedule)
        self._registry.append((key, w))
        return w

    # ---- sections -----------------------------------------------------------

    def _section_mode(self):
        box, lay = self._collapsible("Scenario", "mode")
        self.scenario_choice = self._combo("scenario_choice", [
            ("single_static", "Single cable — static hang"),
            ("single_steady", "Single cable — steady lay"),
            ("bu_static", "Branching unit — static hold"),
            ("fs_static", "Final splice (bight) — static hold"),
            ("operation", "Operation simulation (beta)"),
        ])
        self.scenario_choice.currentIndexChanged.connect(self._apply_scenario_choice)
        lay.addRow("Modelling", self.scenario_choice)
        self.scenario_desc = QLabel("")
        self.scenario_desc.setWordWrap(True)
        self.scenario_desc.setStyleSheet("color:#777; font-size: small;")
        lay.addRow(self.scenario_desc)
        # Hidden holders retained for config plumbing + settings migration:
        # the scenario choice above drives both.
        self.mode_combo = self._combo("mode", MODES)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.mode_combo.setVisible(False)
        self.static_config = self._combo("static_config", [
            ("single", "Single cable span"),
            ("bu", "Branching unit (held)"),
            ("bight", "Final bight (held)"),
        ])
        self.static_config.currentIndexChanged.connect(self._update_config_visibility)
        self.static_config.setVisible(False)
        return box

    _SCENARIO_DESCS = {
        "single_static": (
            "Stationary vessel holding a single cable span to the seabed. "
            "Set the solve target under 'What to solve'."),
        "single_steady": (
            "Constant-speed lay (ship frame): tension, layback and touchdown "
            "for the ship speed and slack under 'Vessel & lay'. Uses the "
            "first assembly segment as a uniform cable."),
        "bu_static": (
            "Branching unit held at depth from the vessel: trunk plus two "
            "laid legs. Set leg bearings in 'Position & heading' and the "
            "hold depth under 'What to solve'."),
        "fs_static": (
            "Final-splice bight held at depth on a lowering rope between "
            "two laid ends. Set the laid ends in 'Position & heading' and "
            "the apex depth under 'What to solve'."),
        "operation": (
            "Time-stepped quasi-static simulation of a scripted operation "
            "(BU deployment, bight lay-down or straight lay). Configure it "
            "under 'Operation scenario', then click Run simulation."),
    }

    def _apply_scenario_choice(self, *args):
        """Map the single scenario picker onto the internal mode/config."""
        choice = self.scenario_choice.currentData() or "single_static"
        self.scenario_desc.setText(self._SCENARIO_DESCS.get(choice, ""))
        mode = {"single_steady": "steady", "operation": "operation"}.get(choice, "static")
        config = {"bu_static": "bu", "fs_static": "bight"}.get(choice, "single")
        for combo, val in ((self.mode_combo, mode), (self.static_config, config)):
            i = combo.findData(val)
            if i >= 0 and i != combo.currentIndex():
                combo.blockSignals(True)
                combo.setCurrentIndex(i)
                combo.blockSignals(False)
        self._on_mode_changed()

    def _section_position(self):
        box, lay = self._collapsible("Position && heading", "position")

        # Local origin: type project-CRS coordinates directly, or pick from
        # the map. The two stay in sync; either sets the simulator's origin.
        self.origin_x = self._coord_spin("origin_x")
        self.origin_y = self._coord_spin("origin_y")
        self.origin_x.editingFinished.connect(self._origin_boxes_changed)
        self.origin_y.editingFinished.connect(self._origin_boxes_changed)
        lay.addRow("Origin easting (X)", self.origin_x)
        lay.addRow("Origin northing (Y)", self.origin_y)

        pick_btn = QPushButton("Pick position on map...")
        pick_btn.setToolTip("Click one point on the map to set the origin above.")
        pick_btn.clicked.connect(self._pick_position_only)
        lay.addRow(pick_btn)

        self.setup_map_btn = QPushButton("Set up on map...")
        self.setup_map_btn.setToolTip(
            "Guided map picks: origin, then heading and leg/end bearings.")
        self.setup_map_btn.clicked.connect(self._guided_setup)
        lay.addRow(self.setup_map_btn)
        self.origin_label = QLabel("")
        self.origin_label.setWordWrap(True)
        self.origin_label.setStyleSheet("color:#777; font-size: small;")
        lay.addRow(self.origin_label)

        self.lay_az = self._dspin("lay_az", 0.0, 360.0, 0.0, 5.0, 0, " degN")
        self.lay_az.setToolTip("Ship course as a compass bearing (degrees clockwise from north).")
        self._lay_az_label = QLabel("Ship course (lay azimuth)")
        lay.addRow(self._lay_az_label, self.lay_az)

        # BU leg leads (bearings from the setup position).
        self.bu_leg1_az = self._dspin("bu_leg1_az", 0.0, 360.0, 150.0, 5.0, 0, " degN")
        self.bu_leg2_az = self._dspin("bu_leg2_az", 0.0, 360.0, 210.0, 5.0, 0, " degN")
        self._pos_bu_rows = [
            (QLabel("Leg 1 lead bearing"), self.bu_leg1_az),
            (QLabel("Leg 2 lead bearing"), self.bu_leg2_az),
        ]
        for lbl, w in self._pos_bu_rows:
            lay.addRow(lbl, w)

        # Final-splice laid ends (separation + axis define A and B about the
        # origin, which sits on their midpoint).
        self.fb_sep = self._dspin("fb_sep", 5.0, 10000.0, 120.0, 10.0, 0, " m")
        self.fb_axis = self._dspin("fb_axis_degN", 0.0, 360.0, 90.0, 5.0, 0, " degN")
        self.fb_axis.setToolTip("Compass bearing of the laid A -> B axis on the seabed.")
        self._pos_fs_rows = [
            (QLabel("Laid-end separation"), self.fb_sep),
            (QLabel("Laid A -> B axis bearing"), self.fb_axis),
        ]
        for lbl, w in self._pos_fs_rows:
            lay.addRow(lbl, w)
        self._update_origin_label()
        return box

    def _update_origin_label(self):
        if self._grid_origin:
            crs = self._grid_origin["crs_authid"]
            self.origin_label.setText(f"Origin CRS: {crs} — snapped to the raster sample centre.")
        elif self._origin_set:
            self.origin_label.setText("Origin set (project CRS). Edit above or re-pick from the map.")
        else:
            self.origin_label.setText(
                "Origin not set — enter coordinates above or pick from the map; "
                "until then exports use the visible canvas centre.")

    def _guided_setup(self):
        config = self._active_config()
        if config == "bight":
            self._pick_bight_ends()
        elif config == "bu":
            self._pick_bu_setup()
        else:
            self._pick_ship_position()

    def _section_environment(self):
        box, lay = self._collapsible("Environment", "env")
        self.bathy_mode = self._combo("bathy_mode", [
            ("flat", "Flat seabed"), ("slope", "Planar slope"),
            ("profile", "Depth profile (along lay azimuth)"), ("grid", "QGIS raster (sampled)"),
        ])
        self.bathy_mode.currentIndexChanged.connect(self._on_bathy_mode)
        lay.addRow("Seabed", self.bathy_mode)
        self.depth_spin = self._dspin("depth_m", 1.0, 12000.0, 100.0, 10.0, 1, " m")
        lay.addRow("Water depth (at vessel)", self.depth_spin)

        self.slope_deg = self._dspin("slope_deg", -45.0, 45.0, 3.0, 0.5, 1, " deg")
        self.slope_azimuth = self._dspin("slope_azimuth_deg", 0.0, 360.0, 0.0, 5.0, 0, " degN")
        self._slope_rows = [
            (QLabel("Down-slope angle"), self.slope_deg),
            (QLabel("Down-slope azimuth"), self.slope_azimuth),
        ]
        for lbl, w in self._slope_rows:
            lay.addRow(lbl, w)

        self.profile_table = QTableWidget(0, 2)
        self.profile_table.setHorizontalHeaderLabels(["Distance from vessel (m)", "Depth (m)"])
        self._auto_size_table(self.profile_table, min_rows=3, max_rows=8)
        self._fit_columns(self.profile_table)
        self.profile_table.cellChanged.connect(self._on_profile_cell_changed)
        prof_btns = QHBoxLayout()
        for text, cb in (("Add", self._profile_add), ("Delete", self._profile_del)):
            b = QPushButton(text)
            b.clicked.connect(cb)
            prof_btns.addWidget(b)
        prof_btns.addStretch(1)
        self._profile_rows_widget = QWidget()
        pv = QVBoxLayout(self._profile_rows_widget)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(self.profile_table)
        self._add_table_grip(self.profile_table, pv)
        pv.addLayout(prof_btns)
        self._profile_label = QLabel("Profile")
        lay.addRow(self._profile_label, self._profile_rows_widget)

        # QGIS raster sampling.
        self.raster_combo = QComboBox()
        self.raster_extent = self._dspin("raster_half_extent_m", 100.0, 100000.0, 2000.0, 100.0, 0, " m")
        self.raster_positive_down = self._check("raster_positive_down", "Raster stores positive-down depths", True)
        self.raster_sample_btn = QPushButton("Sample raster around origin")
        self.raster_sample_btn.setToolTip(
            "Samples a depth grid centred on the local origin (set it with "
            "'Set up on map' in Position & heading; falls back to the "
            "visible map centre).")
        self.raster_sample_btn.clicked.connect(self._sample_raster)
        self.raster_status = QLabel("No grid sampled.")
        self._raster_rows = [
            (QLabel("Raster layer"), self.raster_combo),
            (QLabel("Half extent"), self.raster_extent),
            (QLabel(""), self.raster_positive_down),
            (QLabel(""), self.raster_sample_btn),
            (QLabel(""), self.raster_status),
        ]
        for lbl, w in self._raster_rows:
            lay.addRow(lbl, w)

        # Current profile table.
        self.current_table = QTableWidget(0, 3)
        self.current_table.setHorizontalHeaderLabels(["Depth (m)", "Speed (m/s)", "Toward (degN)"])
        self._auto_size_table(self.current_table, min_rows=2, max_rows=6)
        self._fit_columns(self.current_table)
        self.current_table.cellChanged.connect(self._on_current_cell_changed)
        cur_btns = QHBoxLayout()
        for text, cb in (("Add", self._current_add), ("Delete", self._current_del)):
            b = QPushButton(text)
            b.clicked.connect(cb)
            cur_btns.addWidget(b)
        cur_btns.addStretch(1)
        cur_holder = QWidget()
        cv = QVBoxLayout(cur_holder)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addWidget(self.current_table)
        self._add_table_grip(self.current_table, cv)
        cv.addLayout(cur_btns)
        lay.addRow("Current vs depth", cur_holder)
        return box

    def _section_assembly(self):
        box, lay = self._collapsible("Cable assembly", "assembly")
        self.asm_table = QTableWidget(0, len(ASM_HEADERS))
        self.asm_table.setHorizontalHeaderLabels(ASM_HEADERS)
        try:
            self.asm_table.horizontalHeaderItem(COL_DIA).setToolTip(
                "Segment rows: outer diameter (m). Body rows: lumped drag "
                "area Cd*A (m2).")
            self.asm_table.horizontalHeaderItem(COL_LOAD).setToolTip(
                "Body rows only: submerged point load (kN).")
        except Exception:
            pass
        self._auto_size_table(self.asm_table, min_rows=3, max_rows=8, stretch_last=False)
        self._fit_columns(self.asm_table)
        self.asm_table.cellChanged.connect(self._on_asm_cell_changed)
        btns = QHBoxLayout()
        for text, cb in (("Add segment", self._asm_add_segment), ("Add body", self._asm_add_body),
                         ("Delete", self._asm_del), ("Up", lambda: self._asm_move(-1)),
                         ("Down", lambda: self._asm_move(1)), ("JSON...", self._asm_json)):
            b = QPushButton(text)
            b.clicked.connect(cb)
            btns.addWidget(b)
        holder = QWidget()
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.addWidget(self.asm_table)
        self._add_table_grip(self.asm_table, hv)
        hv.addLayout(btns)
        lay.addRow(holder)
        note = QLabel("Ordered from the chute down. Blank = use the defaults below. "
                      "Compatible with Catenary Calculator V2 assembly JSON.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777; font-size: small;")
        lay.addRow(note)

        self.def_qw = self._dspin("def_q_water", -5000.0, 50000.0, 200.0, 10.0, 1, " N/m")
        self.def_qw.setToolTip("Submerged weight per metre. Negative = buoyant.")
        self.def_dia = self._dspin("def_dia", 0.0, 1.0, 0.035, 0.005, 3, " m")
        self.def_cdn = self._dspin("def_cdn", 0.0, 5.0, 1.2, 0.05, 2)
        self.def_cdn.setToolTip(
            "Normal (cross-flow) drag coefficient. Typical for bare cable: "
            "1.0-1.3.")
        self.def_cdt = self._dspin("def_cdt", 0.0, 1.0, 0.01, 0.005, 3)
        self.def_cdt.setToolTip(
            "Tangential (skin) drag coefficient. Typical: 0.003-0.05.")
        self.def_mu = self._dspin("def_mu", 0.0, 3.0, 0.3, 0.05, 2)
        self.def_mu.setToolTip(
            "Cable-seabed Coulomb friction coefficient. Typical: 0.2-0.6 "
            "(sand/clay); higher for rough rock.")
        self.def_ei = self._dspin("def_ei", 0.0, 10000.0, 0.0, 1.0, 1, " kN.m2")
        self.def_ei.setToolTip(
            "Bending stiffness. 0 = perfectly flexible (usual for long "
            "spans); set for stiff products near the touchdown point.")
        self.def_mbr = self._dspin("def_mbr", 0.0, 100.0, 0.0, 0.5, 1, " m")
        self.def_mbr.setToolTip(
            "Minimum bend radius limit used for the violation check in the "
            "results. 0 disables the check.")
        lay.addRow("Default weight in water", self.def_qw)
        lay.addRow("Default diameter", self.def_dia)
        lay.addRow("Default Cd (normal)", self.def_cdn)
        lay.addRow("Default Cd (tangential)", self.def_cdt)
        lay.addRow("Default seabed friction mu", self.def_mu)
        lay.addRow("Default EI", self.def_ei)
        lay.addRow("Default MBR limit (0 = off)", self.def_mbr)
        return box

    def _section_vessel(self):
        box, lay = self._collapsible("Vessel && lay", "vessel")
        self.chute_h = self._dspin("chute_h", 0.0, 50.0, 5.0, 0.5, 1, " m")
        self.chute_h.setToolTip(
            "Height of the cable departure point (chute top) above the "
            "waterline. Also used as the drawn hull freeboard in the 3D view."
        )
        self.ship_speed = self._dspin("ship_speed_kn", 0.0, 12.0, 6.0, 0.25, 2, " kn")
        self.ship_speed.setToolTip(
            "Lay speed over ground. Used by steady-lay and operation modes; "
            "hidden for static solves (vessel stationary).")
        self.slack = self._dspin("slack_pct", -10.0, 30.0, 2.0, 0.5, 1, " %")
        self.slack.setToolTip(
            "Pay-out speed margin over ship speed: pay-out = ship speed x "
            "(1 + slack). Positive slack lays extra cable on the bed; "
            "typical values are 1-5 %.")
        self.chute_mu = self._dspin("chute_mu", 0.0, 1.0, 0.3, 0.05, 2)
        self.chute_mu.setToolTip(
            "Capstan friction coefficient over the chute: converts the "
            "cable-side top tension into the machinery-side holdback "
            "figure shown in the results.")
        lay.addRow("Chute height above waterline", self.chute_h)
        self._ship_speed_label = QLabel("Ship speed")
        lay.addRow(self._ship_speed_label, self.ship_speed)
        self._slack_label = QLabel("Slack")
        lay.addRow(self._slack_label, self.slack)
        lay.addRow("Chute friction mu (capstan)", self.chute_mu)
        return box

    def _section_ship_shape(self):
        """Parametric ship drawing: hull around a CRP, chute offset from the
        CRP. The chute stays the solver's departure point; the hull is drawn
        geometry only (3D view and map overlay)."""
        box, lay = self._collapsible("Vessel drawing && sheaves", "ship_shape")
        self.ship_len = self._dspin("ship_length_m", 5.0, 400.0, 60.0, 5.0, 0, " m")
        self.ship_beam = self._dspin("ship_beam_m", 2.0, 80.0, 12.0, 1.0, 0, " m")
        self.crp_fwd = self._dspin("crp_fwd_m", -200.0, 200.0, 0.0, 1.0, 1, " m")
        self.crp_stbd = self._dspin("crp_stbd_m", -40.0, 40.0, 0.0, 0.5, 1, " m")
        self.chute_fwd = self._dspin("chute_fwd_m", -200.0, 200.0, 0.0, 1.0, 1, " m")
        self.chute_stbd = self._dspin("chute_stbd_m", -40.0, 40.0, 0.0, 0.5, 1, " m")
        self.chute_radius = self._dspin("chute_radius_m", 0.0, 30.0, 0.0, 0.5, 1, " m")
        self.chute_radius.setToolTip(
            "Overboarding chute radius, drawn as a quarter arc at the "
            "departure point (as in the 2D Catenary Calculator). Rendering "
            "only — chute contact is not modelled in the 3D solver."
        )
        lay.addRow("Ship length", self.ship_len)
        lay.addRow("Ship breadth", self.ship_beam)
        lay.addRow("CRP forward of midship", self.crp_fwd)
        lay.addRow("CRP starboard of centreline", self.crp_stbd)
        lay.addRow("Chute forward of CRP", self.chute_fwd)
        lay.addRow("Chute starboard of CRP", self.chute_stbd)
        lay.addRow("Chute radius (drawn)", self.chute_radius)
        # Sheave geometry — functional for the two-sheave BU deployment
        # scenario (port/stbd overboarding points rotate with the heading).
        self.sheave_fwd = self._dspin("sheave_fwd_m", -200.0, 200.0, 0.0, 1.0, 1, " m")
        self.sheave_fwd.setToolTip(
            "Fore/aft position of the port and starboard sheaves (used by "
            "the two-sheave BU deployment scenario).")
        self.sheave_spacing = self._dspin("sheave_spacing_m", 0.5, 60.0, 12.0, 0.5, 1, " m")
        self.sheave_spacing.setToolTip(
            "Athwartships distance between the port and starboard sheaves.")
        lay.addRow("Sheaves forward of CRP", self.sheave_fwd)
        lay.addRow("Port-stbd sheave spacing", self.sheave_spacing)
        note = QLabel("Hull geometry is drawn only, but the sheave offsets "
                      "are used by the two-sheave BU deployment. "
                      "Negative 'forward' = aft, negative 'starboard' = port. "
                      "With zero offsets the chute sits at the hull centre.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777; font-size: small;")
        lay.addRow(note)
        return box

    def _section_advanced(self):
        box, lay = self._collapsible("Advanced (solver)", "advanced")
        self.adv_ds = self._dspin("target_ds_m", 0.5, 50.0, 5.0, 0.5, 1, " m")
        self.adv_ds.setToolTip(
            "Target element length for the 3D discretisation. Smaller = "
            "finer touchdown/bend-radius resolution but slower solves.")
        self.adv_tol = self._dspin("dr_tol", 0.0001, 0.05, 0.002, 0.0005, 4)
        self.adv_tol.setToolTip(
            "Relative force residual at which the relaxation is accepted. "
            "Smaller = tighter equilibrium but longer solve times.")
        self.adv_rho = self._dspin("rho_water", 950.0, 1100.0, 1025.0, 5.0, 0, " kg/m3")
        self.adv_rho.setToolTip("Water density (1025 seawater, ~1000 fresh).")
        lay.addRow("Mesh target element length", self.adv_ds)
        lay.addRow("Convergence tolerance", self.adv_tol)
        lay.addRow("Water density", self.adv_rho)
        return box

    def _section_bu_bight(self):
        """Physical properties shared by the static-hold configurations and
        the operation scenarios (BU or bight); positions and bearings live
        in the 'Position & heading' section."""
        box, lay = self._collapsible("BU / splice properties", "bu_bight")
        # Branching-unit group.
        self.bu_weight = self._dspin("bu_weight_kN", 0.1, 500.0, 15.0, 1.0, 1, " kN")
        self.bu_weight.setToolTip("Submerged weight of the branching unit body.")
        self.bu_cda = self._dspin("bu_cda", 0.0, 50.0, 1.5, 0.1, 2, " m2")
        self.bu_cda.setToolTip(
            "Lumped drag area Cd x A of the BU body for current loading "
            "(frontal area times its drag coefficient).")
        self.bu_leg_len = self._dspin("bu_leg_len", 10.0, 20000.0, 300.0, 10.0, 0, " m")
        self.bu_leg_len.setToolTip(
            "Deployed length of each pre-laid leg, measured from the BU "
            "along its lead bearing.")
        self._bu_geo_rows = [
            (QLabel("BU submerged weight"), self.bu_weight),
            (QLabel("BU drag area Cd*A"), self.bu_cda),
            (QLabel("Leg length (each)"), self.bu_leg_len),
        ]
        for lbl, w in self._bu_geo_rows:
            lay.addRow(lbl, w)
        # Final-bight group.
        self.fb_length = self._dspin("fb_length", 20.0, 20000.0, 300.0, 10.0, 0, " m")
        self._fb_geo_rows = [
            (QLabel("Bight length (joined loop)"), self.fb_length),
        ]
        for lbl, w in self._fb_geo_rows:
            lay.addRow(lbl, w)
        self._bu_bight_box = box
        return box

    # Presentation of the "Value" spin box per solve-target mode:
    # (suffix, lo, hi, step, decimals, tooltip).
    _SOLVE_VALUE_SPECS = {
        "bottom_tension": (" kN", 0.0, 1e5, 0.5, 2,
                           "Residual tension in the cable at the touchdown point."),
        "top_tension": (" kN", 0.0, 1e6, 1.0, 2,
                        "Cable-side tension at the chute departure point."),
        "exit_angle": (" deg", 0.0, 90.0, 1.0, 1,
                       "Departure angle below horizontal at the chute."),
        "layback": (" m", 0.0, 1e5, 10.0, 0,
                    "Horizontal distance from the vessel to the touchdown point."),
        "suspended_length": (" m", 0.0, 1e6, 10.0, 0,
                             "Cable length in the water column (chute to touchdown)."),
    }

    def _update_solve_value_units(self, *args):
        spec = self._SOLVE_VALUE_SPECS.get(self.solve_mode.currentData())
        if not spec:
            return
        suffix, lo, hi, step, dec, tip = spec
        w = self.solve_value
        w.blockSignals(True)
        w.setSuffix(suffix)
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setDecimals(dec)
        w.blockSignals(False)
        w.setToolTip(tip)

    def _section_solve(self):
        box, lay = self._collapsible("What to solve", "solve")
        self.solve_mode = self._combo("solve_mode", SOLVE_MODES)
        self.solve_mode.setToolTip(
            "Which quantity you specify; the solver finds the matching "
            "configuration and reports all the others.")
        self.solve_value = self._dspin("solve_value", -1e6, 1e6, 5.0, 1.0, 3)
        self.solve_mode.currentIndexChanged.connect(self._update_solve_value_units)
        self._update_solve_value_units()
        self.on_bed_tail = self._dspin("on_bed_tail", 10.0, 5000.0, 150.0, 10.0, 0, " m")
        self.on_bed_tail.setToolTip(
            "Extra cable modelled lying on the bed beyond the touchdown "
            "point (anchors the far end of the static solve).")
        self._solve_rows = [
            (QLabel("Input"), self.solve_mode),
            (QLabel("Value"), self.solve_value),
            (QLabel("On-bed tail beyond TDP (static)"), self.on_bed_tail),
        ]
        for lbl, w in self._solve_rows:
            lay.addRow(lbl, w)

        # Static-hold inputs (BU / bight configurations).
        self.bu_depth = self._dspin("bu_depth_m", 1.0, 8000.0, 20.0, 5.0, 1, " m")
        self.bu_depth.setToolTip(
            "Depth below the surface at which the branching unit is held "
            "from the vessel.")
        self.trunk_slack = self._dspin("trunk_slack_pct", 0.0, 50.0, 2.0, 0.5, 1, " %")
        self.trunk_slack.setToolTip(
            "Trunk length margin over the straight chute-to-BU distance; "
            "more slack lets the trunk hang deeper.")
        self.apex_depth = self._dspin("apex_depth_m", 1.0, 8000.0, 10.0, 5.0, 1, " m")
        self.apex_depth.setToolTip(
            "Depth below the surface at which the bight apex is held on "
            "the lowering rope.")
        self._bu_hold_rows = [
            (QLabel("BU hold depth below surface"), self.bu_depth),
            (QLabel("Trunk slack over hold distance"), self.trunk_slack),
        ]
        self._fb_hold_rows = [
            (QLabel("Bight apex hold depth"), self.apex_depth),
        ]
        for lbl, w in self._bu_hold_rows + self._fb_hold_rows:
            lay.addRow(lbl, w)
        self._solve_box = box
        return box

    def _section_operation(self):
        box, lay = self._collapsible("Operation scenario", "operation")
        self.scenario_combo = self._combo("scenario", SCENARIOS)
        self.scenario_combo.currentIndexChanged.connect(self._on_scenario_changed)
        lay.addRow("Scenario", self.scenario_combo)

        self.op_quality = self._combo("op_quality", [
            ("full", "Full — accurate solver"),
            ("draft", "Draft — accurate solver, coarse/fast"),
            ("quick", "Quick — analytic catenary model (BU only)"),
        ])
        self.op_quality.setToolTip(
            "Full: the dynamic-relaxation solver at normal settings.\n"
            "Draft: the same solver with a coarser mesh, looser tolerance "
            "and bigger steps (~5-10x faster) — for iterating.\n"
            "Quick: closed-form tri-catenary equilibrium (no friction/drag/"
            "lay history) — solves a whole BU deployment in about a second; "
            "confirm with Full.")
        lay.addRow("Model quality", self.op_quality)

        self.op_stack = QStackedWidget()

        # BU deployment page (physical geometry lives in "BU / bight geometry").
        bu = QWidget()
        bl = QFormLayout(bu)
        self.bu_payout = self._dspin("bu_payout", 0.01, 3.0, 0.4, 0.05, 2, " m/s")
        self.bu_ship_speed = self._dspin("bu_ship_speed_kn", 0.0, 4.0, 0.6, 0.1, 2, " kn")
        bl.addRow("Trunk pay-out rate", self.bu_payout)
        bl.addRow("Ship speed", self.bu_ship_speed)
        self.op_stack.addWidget(bu)

        # Full two-sheave BU deployment page.
        self.op_stack.addWidget(self._build_bu_full_page())

        # Final bight page.
        fb = QWidget()
        fl = QFormLayout(fb)
        self.fb_payout = self._dspin("fb_payout", 0.01, 3.0, 0.3, 0.05, 2, " m/s")
        self.fb_course = self._dspin("fb_course", 0.0, 360.0, 0.0, 5.0, 0, " degN")
        self.fb_ship_speed = self._dspin("fb_ship_speed_kn", 0.0, 3.0, 0.3, 0.05, 2, " kn")
        self.fb_release = self._dspin("fb_release_kN", 0.0, 100.0, 2.0, 0.5, 1, " kN")
        fl.addRow("Rope pay-out rate", self.fb_payout)
        fl.addRow("Vessel step course", self.fb_course)
        fl.addRow("Vessel step speed", self.fb_ship_speed)
        fl.addRow("Release when hook load below", self.fb_release)
        self.op_stack.addWidget(fb)

        # Straight lay page.
        slp = QWidget()
        slf = QFormLayout(slp)
        self.sl_duration = self._dspin("sl_duration", 10.0, 24 * 3600.0, 1200.0, 60.0, 0, " s")
        slf.addRow("Duration", self.sl_duration)
        slf.addRow(QLabel("Uses the ship speed and slack from 'Vessel & lay'."))
        self.op_stack.addWidget(slp)

        lay.addRow(self.op_stack)
        self._operation_box = box
        return box

    def _build_bu_full_page(self) -> QWidget:
        """Operation page for the full two-sheave BU deployment: set-up
        geometry, deployment parameters and the editable phase schedule."""
        page = QWidget()
        fl = QFormLayout(page)
        note = QLabel(
            "Both legs are pre-laid to their far ends; the vessel holds the "
            "recovered ends over the port (leg 1) and starboard (leg 2) "
            "sheaves at the jointing position. Positions below are local "
            "metres from the origin. Set the sheave offsets under 'Vessel "
            "drawing'.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777; font-size: small;")
        fl.addRow(note)

        def coord(key, val):
            return self._dspin(key, -1e6, 1e6, val, 10.0, 1, " m")

        self.bf_end1_x = coord("bf_end1_x", -150.0)
        self.bf_end1_y = coord("bf_end1_y", 200.0)
        self.bf_end2_x = coord("bf_end2_x", -150.0)
        self.bf_end2_y = coord("bf_end2_y", -200.0)
        self.bf_target_x = coord("bf_target_x", 150.0)
        self.bf_target_y = coord("bf_target_y", 0.0)
        self.bf_vessel_x = coord("bf_vessel_x", 0.0)
        self.bf_vessel_y = coord("bf_vessel_y", 0.0)
        self.bf_vessel_x.setToolTip(
            "Vessel jointing position in the local frame (the optimiser "
            "adjusts this so the BU lands on target).")
        fl.addRow("Vessel start (jointing) x / y", self._pair(self.bf_vessel_x, self.bf_vessel_y))
        fl.addRow("Leg 1 laid end x / y", self._pair(self.bf_end1_x, self.bf_end1_y))
        fl.addRow("Leg 2 laid end x / y", self._pair(self.bf_end2_x, self.bf_end2_y))
        fl.addRow("Target BU landing x / y", self._pair(self.bf_target_x, self.bf_target_y))

        self.bf_tail = self._dspin("bf_tail_m", 5.0, 500.0, 90.0, 5.0, 0, " m")
        self.bf_tail.setToolTip("BU tail length per leg (joint to BU body).")
        self.bf_payout = self._dspin("bf_payout", 0.01, 3.0, 0.4, 0.05, 2, " m/s")
        self.bf_ship_speed = self._dspin("bf_ship_speed_kn", 0.0, 4.0, 0.6, 0.1, 2, " kn")
        self.bf_balance = self._check(
            "bf_balance", "Auto-balance leg payout (tension controller)", True)
        self.bf_balance.setToolTip(
            "Continuously redistributes the scheduled leg payout so the two "
            "sheave tensions stay matched, like a winch operator would.")
        fl.addRow("BU tail length (each leg)", self.bf_tail)
        fl.addRow("Nominal pay-out rate", self.bf_payout)
        fl.addRow("Lay-ahead ship speed", self.bf_ship_speed)
        fl.addRow(self.bf_balance)

        self.sched_table = QTableWidget(0, len(SCH_HEADERS))
        self.sched_table.setHorizontalHeaderLabels(SCH_HEADERS)
        self.sched_table.setMinimumHeight(150)
        self.sched_table.itemChanged.connect(self._schedule)
        fl.addRow(self.sched_table)

        btns = QHBoxLayout()
        self.btn_optimize = QPushButton("Optimise schedule…")
        self.btn_optimize.setToolTip(
            "Build (or refine) the phase schedule and shift the whole "
            "set-up so the BU lands on the target, using fast preview "
            "simulations. Then click Run simulation for full-quality "
            "results.")
        self.btn_optimize.clicked.connect(self._optimize_clicked)
        btn_default = QPushButton("Default schedule")
        btn_default.setToolTip("Rebuild the five-phase nominal schedule from "
                               "the parameters above.")
        btn_default.clicked.connect(self._sched_fill_default)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(lambda: self.sched_table.setRowCount(0))
        for b in (self.btn_optimize, btn_default, btn_clear):
            btns.addWidget(b)
        btns.addStretch(1)
        holder = QWidget()
        holder.setLayout(btns)
        fl.addRow(holder)
        return page

    def _pair(self, w1, w2) -> QWidget:
        holder = QWidget()
        h = QHBoxLayout(holder)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(w1)
        h.addWidget(w2)
        return holder

    # ---- schedule table <-> PhaseRow dicts --------------------------------

    def _sched_fill_default(self):
        try:
            from ..engine.bathymetry import bathymetry_from_dict
            from ..engine.scenarios import default_bu_schedule

            bathy = bathymetry_from_dict(self._bathy_cfg())
            rows = default_bu_schedule(
                depth_m=float(bathy.depth_at(0.0, 0.0)),
                tail_length_m=float(self.bf_tail.value()),
                payout_mps=float(self.bf_payout.value()),
                lay_speed_mps=float(self.bf_ship_speed.value()) * 0.514444,
                course_deg=0.0,   # stored math-frame 0; column shows compass
            )
        except Exception as exc:
            QMessageBox.warning(self, "Schedule", f"Could not build the "
                                f"default schedule:\n{exc}")
            return
        # Column shows the ship course as compass; engine rows are math-frame.
        for r in rows:
            r.course_deg = float(self.lay_az.value())
        self._schedule_to_table([r.to_dict() for r in rows], course_is_compass=True)

    def _schedule_to_table(self, rows: list, course_is_compass: bool = True):
        """Fill the schedule table from PhaseRow dicts (course shown as a
        compass bearing)."""
        from .scene import math_to_compass_deg

        t = self.sched_table
        t.blockSignals(True)
        try:
            t.setRowCount(0)
            for d in rows:
                r = t.rowCount()
                t.insertRow(r)
                course = float(d.get("course_deg", 0.0))
                if not course_is_compass:
                    course = math_to_compass_deg(course)
                pay = d.get("payout_mps") or {}
                vals = [
                    str(d.get("label", "")),
                    str(d.get("event", "")),
                    f"{float(d.get('duration_s', 0.0)):.0f}",
                    f"{course:.0f}",
                    f"{float(d.get('speed_mps', 0.0)) / 0.514444:.2f}",
                    f"{float(pay.get('leg1', 0.0)):.2f}",
                    f"{float(pay.get('leg2', 0.0)):.2f}",
                    f"{float(pay.get('trunk', 0.0)):.2f}",
                ]
                for c, v in enumerate(vals):
                    t.setItem(r, c, QTableWidgetItem(v))
            self._auto_size_table(t)
        finally:
            t.blockSignals(False)

    def _schedule_from_table(self) -> list:
        """PhaseRow dicts from the table (course converted compass -> math)."""
        from .scene import compass_to_math_deg as c2m

        rows = []
        t = self.sched_table
        for r in range(t.rowCount()):
            def txt(c):
                it = t.item(r, c)
                return it.text().strip() if it is not None and it.text() else ""

            def num(c, default=0.0):
                v = _of(t.item(r, c))
                return float(v) if v is not None else default

            pay = {}
            for name, col in (("leg1", SCH_COL_LEG1), ("leg2", SCH_COL_LEG2),
                              ("trunk", SCH_COL_TRUNK)):
                v = num(col, 0.0)
                if v != 0.0:
                    pay[name] = v
            rows.append({
                "label": txt(SCH_COL_LABEL),
                "event": txt(SCH_COL_EVENT),
                "duration_s": num(SCH_COL_DUR, 0.0),
                "course_deg": c2m(num(SCH_COL_COURSE, 0.0)),
                "speed_mps": num(SCH_COL_SPEED, 0.0) * 0.514444,
                "payout_mps": pay,
            })
        return [r for r in rows if r["duration_s"] > 0]

    def _section_display(self):
        box, lay = self._collapsible("Display", "display")
        self.zex = self._dspin("z_exaggeration", 0.1, 200.0, 1.0, 0.5, 1, " x")
        self.zex.setToolTip(
            "Vertical exaggeration of the 3D view only. The Profile and "
            "Plan tabs always plot true scale (see the true-scale profile "
            "option below).")
        self.zex.valueChanged.connect(lambda v: self.view3d.set_z_exaggeration(float(v)))
        self.color_mode = self._combo("color_mode", [("segment", "Color by segment"), ("tension", "Color by tension")])
        self.color_mode.currentIndexChanged.connect(
            lambda _i: self.view3d.set_cable_color_mode(self.color_mode.currentData())
        )
        lay.addRow("Depth exaggeration", self.zex)
        lay.addRow("Cable colors", self.color_mode)
        self.profile_true_scale = self._check(
            "profile_true_scale", "True-scale profile (1:1 horizontal/vertical)", True)
        self.profile_true_scale.setToolTip(
            "Lock the Profile tab axes to a 1:1 scale so the side view shows "
            "true geometry. Untick to let the profile stretch to fill the plot.")
        self.profile_true_scale.toggled.connect(
            lambda on: self.profile_view.set_equal_aspect(bool(on)))
        lay.addRow(self.profile_true_scale)
        self.show_on_map = self._check(
            "show_on_map", "Show result on map canvas (ship, cable plan, TDP)", False)
        self.show_on_map.toggled.connect(self._refresh_map_overlay)
        self.clear_map_btn = QPushButton("Clear map visuals")
        self.clear_map_btn.clicked.connect(self._clear_map_overlay)
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.addWidget(self.show_on_map)
        row_lay.addWidget(self.clear_map_btn)
        row_lay.addStretch(1)
        lay.addRow(row)
        return box

    # ------------------------------------------------------------- tables

    @staticmethod
    def _mark_cell(table, r, c, bad: bool, tip: str = ""):
        it = table.item(r, c)
        if it is None:
            return
        it.setBackground(QtGui.QColor("#ffd6d6") if bad else QtGui.QBrush())
        it.setToolTip(tip if bad else "")

    def _validate_profile_table(self):
        """Inline feedback: non-numeric cells and non-increasing distances."""
        t = self.profile_table
        t.blockSignals(True)
        try:
            prev = None
            for r in range(t.rowCount()):
                d = _of(t.item(r, 0))
                z = _of(t.item(r, 1))
                bad_d = d is None or (prev is not None and d <= prev)
                self._mark_cell(t, r, 0, bad_d,
                                "Distance must be a number and increase down the table.")
                self._mark_cell(t, r, 1, z is None, "Depth must be a number.")
                if d is not None:
                    prev = d
        finally:
            t.blockSignals(False)

    def _validate_current_table(self):
        t = self.current_table
        t.blockSignals(True)
        try:
            for r in range(t.rowCount()):
                d = _of(t.item(r, 0))
                s = _of(t.item(r, 1))
                a = _of(t.item(r, 2))
                self._mark_cell(t, r, 0, d is None or d < 0,
                                "Depth must be a non-negative number.")
                self._mark_cell(t, r, 1, s is None, "Speed must be a number (m/s).")
                self._mark_cell(t, r, 2, a is not None and not (0.0 <= a <= 360.0),
                                "Direction must be a compass bearing 0-360.")
        finally:
            t.blockSignals(False)

    def _on_profile_cell_changed(self, *_a):
        self._validate_profile_table()
        self._schedule()

    def _on_current_cell_changed(self, *_a):
        self._validate_current_table()
        self._schedule()

    def _style_asm_row(self, r: int):
        """Grey out cells that don't apply to the row's type and label the
        dual-purpose Dia/CdA column."""
        titem = self.asm_table.item(r, COL_TYPE)
        is_body = bool(titem and titem.text().strip().lower().startswith("b"))
        off_cols = _SEG_ONLY_COLS if is_body else _BODY_ONLY_COLS
        on_cols = _BODY_ONLY_COLS if is_body else _SEG_ONLY_COLS
        editable = getattr(Qt, "ItemFlag", Qt).ItemIsEditable
        for cols, on in ((off_cols, False), (on_cols, True)):
            for c in cols:
                it = self.asm_table.item(r, c)
                if it is None:
                    it = QTableWidgetItem("")
                    self.asm_table.setItem(r, c, it)
                if on:
                    it.setFlags(it.flags() | editable)
                    it.setBackground(QtGui.QBrush())
                    it.setToolTip("")
                else:
                    it.setFlags(it.flags() & ~editable)
                    it.setBackground(QtGui.QColor("#e8e8e8"))
                    it.setToolTip("Not used for %s rows."
                                  % ("body" if is_body else "segment"))
        dia = self.asm_table.item(r, COL_DIA)
        if dia is None:
            dia = QTableWidgetItem("")
            self.asm_table.setItem(r, COL_DIA, dia)
        dia.setToolTip("Body drag area Cd*A (m2)." if is_body
                       else "Segment outer diameter (m).")

    def _style_asm_rows(self):
        self.asm_table.blockSignals(True)
        try:
            for r in range(self.asm_table.rowCount()):
                self._style_asm_row(r)
        finally:
            self.asm_table.blockSignals(False)

    def _on_asm_cell_changed(self, row: int, col: int):
        if col == COL_TYPE:
            self.asm_table.blockSignals(True)
            try:
                self._style_asm_row(row)
            finally:
                self.asm_table.blockSignals(False)
        self._schedule()

    def _profile_add(self):
        r = self.profile_table.rowCount()
        self.profile_table.insertRow(r)
        base = 0.0 if r == 0 else _f(self.profile_table.item(r - 1, 0), 0.0) + 500.0
        depth = self.depth_spin.value() if r == 0 else _f(self.profile_table.item(r - 1, 1), 100.0)
        self.profile_table.setItem(r, 0, QTableWidgetItem(str(base)))
        self.profile_table.setItem(r, 1, QTableWidgetItem(str(depth)))
        self._schedule()

    def _profile_del(self):
        r = self.profile_table.currentRow()
        if r >= 0:
            self.profile_table.removeRow(r)
            self._schedule()

    def _current_add(self):
        r = self.current_table.rowCount()
        self.current_table.insertRow(r)
        self.current_table.setItem(r, 0, QTableWidgetItem("0" if r == 0 else "100"))
        self.current_table.setItem(r, 1, QTableWidgetItem("0.5"))
        self.current_table.setItem(r, 2, QTableWidgetItem("90"))
        self._schedule()

    def _current_del(self):
        r = self.current_table.currentRow()
        if r >= 0:
            self.current_table.removeRow(r)
            self._schedule()

    def _asm_add_segment(self):
        r = self.asm_table.rowCount()
        self.asm_table.insertRow(r)
        vals = ["Segment", f"Cable {r + 1}", "1000", "", "", "", "", "", "", "", "", "", ""]
        for c, v in enumerate(vals):
            self.asm_table.setItem(r, c, QTableWidgetItem(v))
        self._style_asm_rows()
        self._schedule()

    def _asm_add_body(self):
        r = self.asm_table.rowCount()
        self.asm_table.insertRow(r)
        vals = ["Body", f"Body {r + 1}", "", "", "", "5.0", "", "", "", "", "", "", ""]
        for c, v in enumerate(vals):
            self.asm_table.setItem(r, c, QTableWidgetItem(v))
        self._style_asm_rows()
        self._schedule()

    def _asm_del(self):
        r = self.asm_table.currentRow()
        if r >= 0:
            self.asm_table.removeRow(r)
            self._schedule()

    def _asm_move(self, delta: int):
        r = self.asm_table.currentRow()
        r2 = r + delta
        if r < 0 or r2 < 0 or r2 >= self.asm_table.rowCount():
            return
        for c in range(self.asm_table.columnCount()):
            a = self.asm_table.takeItem(r, c)
            bq = self.asm_table.takeItem(r2, c)
            self.asm_table.setItem(r, c, bq)
            self.asm_table.setItem(r2, c, a)
        self.asm_table.selectRow(r2)
        self._schedule()

    def _asm_json(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Assembly JSON (V2-compatible)")
        v = QVBoxLayout(dlg)
        te = QTextEdit()
        te.setPlainText(json.dumps(self._assembly_json(), indent=2))
        v.addWidget(te)
        row = QHBoxLayout()
        ok = QPushButton("Apply")
        cancel = QPushButton("Close")
        ok.clicked.connect(lambda: (self._assembly_from_json(te.toPlainText()), dlg.accept()))
        cancel.clicked.connect(dlg.reject)
        row.addWidget(ok)
        row.addWidget(cancel)
        v.addLayout(row)
        dlg.resize(560, 420)
        dlg.exec() if hasattr(dlg, "exec") else dlg.exec_()

    def _assembly_json(self) -> List[dict]:
        out = []
        for r in range(self.asm_table.rowCount()):
            kind = (_s(self.asm_table.item(r, COL_TYPE)) or "Segment").strip().lower()
            name = _s(self.asm_table.item(r, COL_NAME))
            if kind.startswith("b"):
                d = {"type": "body", "name": name or "Body",
                     "point_load_kN": _f(self.asm_table.item(r, COL_LOAD), 0.0)}
                # For a body row the "Dia" column is repurposed as lumped
                # drag area Cd*A (m2), matching the JSON key cda_m2.
                cda = _of(self.asm_table.item(r, COL_DIA))
                if cda:
                    d["cda_m2"] = cda
                color = _s(self.asm_table.item(r, COL_COLOR))
                if color:
                    d["color"] = color
            else:
                d = {"type": "segment", "name": name or "Cable",
                     "length_m": _f(self.asm_table.item(r, COL_LEN), 0.0),
                     "q_water_npm": _f(self.asm_table.item(r, COL_QW), 0.0),
                     "q_air_npm": _f(self.asm_table.item(r, COL_QA), 0.0)}
                for col, keyname in ((COL_MU, "friction_mu"), (COL_EI, "bending_stiffness_kNm2"),
                                     (COL_MBR, "min_bend_radius_m"), (COL_DIA, "diameter_m"),
                                     (COL_CDN, "cd_normal"), (COL_CDT, "cd_tangential")):
                    val = _of(self.asm_table.item(r, col))
                    if val is not None:
                        d[keyname] = val
                color = _s(self.asm_table.item(r, COL_COLOR))
                if color:
                    d["color"] = color
            out.append(d)
        return out

    def _assembly_from_json(self, raw: str):
        try:
            data = json.loads(raw or "[]")
            if isinstance(data, dict):
                data = data.get("assembly", [])
            if not isinstance(data, list):
                raise ValueError("expected a list")
        except Exception as exc:
            QMessageBox.warning(self, "Assembly JSON", f"Could not parse JSON: {exc}")
            return
        self.asm_table.blockSignals(True)
        self.asm_table.setRowCount(0)
        for entry in data:
            r = self.asm_table.rowCount()
            self.asm_table.insertRow(r)
            is_body = str(entry.get("type", "segment")).lower() == "body"
            def put(col, val):
                self.asm_table.setItem(r, col, QTableWidgetItem("" if val in (None, "") else str(val)))
            put(COL_TYPE, "Body" if is_body else "Segment")
            put(COL_NAME, entry.get("name", ""))
            if is_body:
                put(COL_LOAD, entry.get("point_load_kN", 0.0))
                put(COL_DIA, entry.get("cda_m2", ""))
            else:
                put(COL_LEN, entry.get("length_m", 0.0))
                put(COL_QW, entry.get("q_water_npm", ""))
                put(COL_QA, entry.get("q_air_npm", ""))
                put(COL_MU, entry.get("friction_mu", ""))
                put(COL_EI, entry.get("bending_stiffness_kNm2", ""))
                put(COL_MBR, entry.get("min_bend_radius_m", ""))
                put(COL_DIA, entry.get("diameter_m", ""))
                put(COL_CDN, entry.get("cd_normal", ""))
                put(COL_CDT, entry.get("cd_tangential", ""))
            put(COL_COLOR, entry.get("color", ""))
        self.asm_table.blockSignals(False)
        self._style_asm_rows()
        self._fit_columns(self.asm_table)
        self._schedule()

    # ------------------------------------------------------------- config

    def _bathy_cfg(self) -> dict:
        mode = self.bathy_mode.currentData()
        depth = float(self.depth_spin.value())
        if mode == "slope":
            g = math.tan(math.radians(float(self.slope_deg.value())))
            az = math.radians(compass_to_math_deg(float(self.slope_azimuth.value())))
            return {"kind": "slope", "depth0_m": depth,
                    "gx": g * math.cos(az), "gy": g * math.sin(az)}
        if mode == "profile":
            pts = []
            for r in range(self.profile_table.rowCount()):
                d = _of(self.profile_table.item(r, 0))
                z = _of(self.profile_table.item(r, 1))
                if d is not None and z is not None:
                    pts.append((d, z))
            if len(pts) >= 1:
                # Profile distances run along the cable trail direction
                # (behind the vessel) — azimuth of the trail.
                return {"kind": "profile", "points": pts,
                        "azimuth_deg": compass_to_math_deg(float(self.lay_az.value())) + 180.0}
            return {"kind": "flat", "depth_m": depth}
        if mode == "grid" and self._grid_bathy is not None:
            return self._grid_bathy
        return {"kind": "flat", "depth_m": depth}

    def _current_cfg(self) -> List[dict]:
        out = []
        for r in range(self.current_table.rowCount()):
            d = _of(self.current_table.item(r, 0))
            s = _of(self.current_table.item(r, 1))
            a = _of(self.current_table.item(r, 2))
            if d is not None and s:
                out.append({"depth_m": d, "speed_mps": s, "direction_deg": a or 0.0})
        return out

    def build_config(self) -> V3Config:
        cfg = V3Config()
        cfg.mode = self.mode_combo.currentData()
        cfg.bathymetry = self._bathy_cfg()
        cfg.current_layers = self._current_cfg()
        cfg.assembly = self._assembly_json()
        cfg.default_q_water_npm = float(self.def_qw.value())
        cfg.default_diameter_m = float(self.def_dia.value())
        cfg.default_cd_normal = float(self.def_cdn.value())
        cfg.default_cd_tangential = float(self.def_cdt.value())
        cfg.default_mu = float(self.def_mu.value())
        cfg.default_EI_kNm2 = float(self.def_ei.value())
        cfg.default_mbr_m = float(self.def_mbr.value())
        cfg.chute_height_m = float(self.chute_h.value())
        cfg.lay_azimuth_deg = float(self.lay_az.value())
        cfg.ship_length_m = float(self.ship_len.value())
        cfg.ship_beam_m = float(self.ship_beam.value())
        cfg.crp_fwd_m = float(self.crp_fwd.value())
        cfg.crp_stbd_m = float(self.crp_stbd.value())
        cfg.chute_fwd_m = float(self.chute_fwd.value())
        cfg.chute_stbd_m = float(self.chute_stbd.value())
        cfg.chute_radius_m = float(self.chute_radius.value())
        cfg.ship_speed_kn = float(self.ship_speed.value())
        cfg.slack_percent = float(self.slack.value())
        cfg.solve_mode = self.solve_mode.currentData()
        cfg.solve_value = float(self.solve_value.value())
        cfg.on_bed_tail_m = float(self.on_bed_tail.value())
        cfg.chute_mu = float(self.chute_mu.value())
        cfg.target_ds_m = float(self.adv_ds.value())
        cfg.dr_tol = float(self.adv_tol.value())
        cfg.rho_water = float(self.adv_rho.value())
        cfg.static_config = self.static_config.currentData() or "single"
        cfg.bu_depth_m = float(self.bu_depth.value())
        cfg.trunk_slack_pct = float(self.trunk_slack.value())
        cfg.apex_depth_m = float(self.apex_depth.value())
        cfg.scenario = self.scenario_combo.currentData()
        cfg.sheave_fwd_m = float(self.sheave_fwd.value())
        cfg.sheave_spacing_m = float(self.sheave_spacing.value())
        # The op dict carries the geometry for whichever configuration is
        # active — the static-hold path reads the same keys as the
        # operation scenarios.
        kind = self._active_config() if cfg.mode == "static" else {
            "bu_deployment": "bu", "bu_full": "bu_full",
            "final_bight": "bight"}.get(cfg.scenario, "single")
        if kind == "bu_full":
            cfg.op = {
                "bu_weight_kN": float(self.bu_weight.value()),
                "bu_cda_m2": float(self.bu_cda.value()),
                "laid_end_1_x": float(self.bf_end1_x.value()),
                "laid_end_1_y": float(self.bf_end1_y.value()),
                "laid_end_2_x": float(self.bf_end2_x.value()),
                "laid_end_2_y": float(self.bf_end2_y.value()),
                "vessel_x": float(self.bf_vessel_x.value()),
                "vessel_y": float(self.bf_vessel_y.value()),
                "target_x": float(self.bf_target_x.value()),
                "target_y": float(self.bf_target_y.value()),
                "tail_length_m": float(self.bf_tail.value()),
                "payout_mps": float(self.bf_payout.value()),
                "ship_speed_kn": float(self.bf_ship_speed.value()),
                "balance": bool(self.bf_balance.isChecked()),
                "limit_mbr_m": float(self.def_mbr.value()),
                "schedule": self._schedule_from_table() or None,
            }
        elif kind == "bu":
            cfg.op = {
                "bu_weight_kN": float(self.bu_weight.value()),
                "bu_cda_m2": float(self.bu_cda.value()),
                "leg_length_m": float(self.bu_leg_len.value()),
                "leg1_azimuth_deg": float(self.bu_leg1_az.value()),
                "leg2_azimuth_deg": float(self.bu_leg2_az.value()),
                "payout_mps": float(self.bu_payout.value()),
                "ship_speed_kn": float(self.bu_ship_speed.value()),
            }
        elif kind == "bight":
            cfg.op = {
                "bight_length_m": float(self.fb_length.value()),
                "end_separation_m": float(self.fb_sep.value()),
                "bight_axis_deg": float(self.fb_axis.value()),
                "payout_mps": float(self.fb_payout.value()),
                "step_course_deg": float(self.fb_course.value()),
                "ship_speed_kn": float(self.fb_ship_speed.value()),
                "release_threshold_kN": float(self.fb_release.value()),
            }
        else:
            cfg.op = {
                "duration_s": float(self.sl_duration.value()),
                "slack_percent": float(self.slack.value()),
                "ship_speed_kn": float(self.ship_speed.value()),
            }
        if cfg.mode == "operation":
            cfg.op["quality"] = self.op_quality.currentData() or "full"
        return cfg

    # ------------------------------------------------------------- solving

    def _schedule(self, *args):
        """An input changed: flag the shown results as stale (explicit solve)."""
        if self._initializing:
            return
        if self.sender() in (getattr(self, "zex", None),
                             getattr(self, "color_mode", None),
                             getattr(self, "show_on_map", None),
                             getattr(self, "profile_true_scale", None)):
            return  # display-only options don't invalidate the solution
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool):
        self._dirty = bool(dirty)
        if dirty:
            verb = ("Run simulation" if self.mode_combo.currentData() == "operation"
                    else "Solve")
            self.dirty_label.setText(f"Inputs changed — click {verb} to update.")
            self.run_btn.setStyleSheet("font-weight: bold;")
        else:
            self.dirty_label.setText("")
            self.run_btn.setStyleSheet("")

    def _solve_clicked(self):
        if self.mode_combo.currentData() == "operation":
            self._run_operation()
        else:
            self._solve_now()

    def _solve_now(self):
        if self._initializing:
            return
        cfg = self.build_config()
        if cfg.mode == "operation":
            self._set_dirty(True)  # operations only run via the button
            return
        if self._worker is not None and self._worker.isRunning():
            self._pending = True
            return
        self._start_worker(cfg)

    def _run_operation(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self.op_progress.setValue(0)
        self._start_worker(self.build_config())

    def _optimize_clicked(self):
        """Run the deployment-schedule optimiser (bu_full scenario only)."""
        if self._worker is not None and self._worker.isRunning():
            return
        cfg = self.build_config()
        if cfg.scenario != "bu_full" or cfg.mode != "operation":
            QMessageBox.information(
                self, "Optimise schedule",
                "Select the 'Operation simulation' scenario with 'BU "
                "deployment — full (two-sheave)' to optimise a schedule.")
            return
        cfg.mode = "optimize"
        self.op_progress.setValue(0)
        self._start_worker(cfg)

    def _start_worker(self, cfg: V3Config):
        # Freeze the map placement alongside the config: the overlay must be
        # drawn at the origin this solve used, not wherever a later pick
        # moved it.
        try:
            self._solve_origin = self._origin_for_map()
        except Exception:
            self._solve_origin = None
        self._set_dirty(False)
        self._worker = SolveWorker(cfg, self)
        self._worker.finishedWith.connect(self._on_solved)
        self._worker.progressed.connect(self._on_progress)
        self.cancel_btn.setEnabled(True)
        self.run_btn.setEnabled(False)
        if cfg.mode in ("operation", "optimize"):
            self.op_progress.setRange(0, 100)
            self.op_progress.setValue(0)
        else:
            self.op_progress.setRange(0, 0)  # indeterminate while relaxing
        self.results.setHtml("<i>Solving...</i>")
        self._worker.start()

    def _cancel_worker(self):
        if self._worker is not None:
            self._worker.cancel()
            self.dirty_label.setText("Cancelling...")

    def _on_progress(self, frac: float, label: str):
        if frac < 0:
            # Within-solve feedback (static/steady): indeterminate bar plus
            # an iteration/residual readout.
            if self.op_progress.maximum() != 0:
                self.op_progress.setRange(0, 0)
            if label:
                self.dirty_label.setText(label)
            return
        if self.op_progress.maximum() == 0:
            self.op_progress.setRange(0, 100)
        self.op_progress.setValue(int(frac * 100))
        if label:
            self.scrub_label.setText(label)

    def _on_solved(self, out: RunOutput):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if self.op_progress.maximum() == 0:
            self.op_progress.setRange(0, 100)
            self.op_progress.setValue(0)
        if not self._dirty:
            self.dirty_label.setText("")
        if out.error == "cancelled":
            self._set_dirty(True)
        self._last_out = out
        self.results.setHtml(render_results_html(out))
        if out.error:
            if self._pending:
                self._pending = False
                self._solve_now()
            return
        self._scene_origin = self._solve_origin

        # Bathy lookup for the profile view's bed-under-cable line.
        try:
            from ..engine.bathymetry import bathymetry_from_dict

            bathy = bathymetry_from_dict(self.build_config().bathymetry)
            self.profile_view.set_bathy_lookup(bathy.depth_at)
        except Exception:
            self.profile_view.set_bathy_lookup(None)

        if out.mode == "optimize":
            # Adopt the optimised schedule + translated set-up so a
            # subsequent "Run simulation" reproduces the preview at full
            # quality.
            if out.schedule:
                self._schedule_to_table(out.schedule, course_is_compass=False)
            setup = out.optimized_setup or {}
            # Adopt the translated set-up verbatim (all in the same local
            # frame over the same bathymetry, so a full-quality re-run
            # reproduces the preview physics).
            for key, spin in (
                ("vessel_x", self.bf_vessel_x), ("vessel_y", self.bf_vessel_y),
                ("laid_end_1_x", self.bf_end1_x), ("laid_end_1_y", self.bf_end1_y),
                ("laid_end_2_x", self.bf_end2_x), ("laid_end_2_y", self.bf_end2_y),
            ):
                if key in setup:
                    spin.blockSignals(True)
                    spin.setValue(float(setup[key]))
                    spin.blockSignals(False)
            self._set_dirty(True)
            self.dirty_label.setText(
                "Schedule optimised (preview) — click Run simulation for "
                "full-quality results.")

        if out.mode in ("operation", "optimize") and out.snapshots:
            self.scrub_widget.setVisible(True)
            self.scrubber.blockSignals(True)
            self.scrubber.setMaximum(len(out.snapshots) - 1)
            self.scrubber.setValue(len(out.snapshots) - 1)
            self.scrubber.blockSignals(False)
            self._show_scene(out.scene, preserve=False)
            self.op_progress.setValue(100)
            self.scrub_label.setText(f"t = {out.snapshots[-1].t_s:.0f} s")
            self.timeseries_view.set_snapshots(out.snapshots)
            self.timeseries_view.set_time(float(out.snapshots[-1].t_s))
        else:
            self.scrub_widget.setVisible(False)
            self._show_scene(out.scene, preserve=True)
            self.timeseries_view.clear()

        if self._pending:
            self._pending = False
            self._solve_now()

    def _on_scrub(self, i: int):
        out = self._last_out
        if out is None or out.scene_builder is None or out.snapshots is None:
            return
        i = max(0, min(len(out.snapshots) - 1, int(i)))
        scene = out.scene_builder(i)
        snap = out.snapshots[i]
        label = f"t = {snap.t_s:.0f} s"
        if getattr(snap, "label", ""):
            label += f" — {snap.label}"
        self.scrub_label.setText(label)
        self.timeseries_view.set_time(float(snap.t_s))
        self._show_scene(scene, preserve=True)

    def _on_play_toggled(self, on: bool):
        if on:
            out = self._last_out
            if out is None or not out.snapshots:
                self.play_btn.setChecked(False)
                return
            if self.scrubber.value() >= self.scrubber.maximum():
                self.scrubber.setValue(0)
            self.play_btn.setText("❚❚")
            self._play_timer.start()
        else:
            self.play_btn.setText("▶")
            self._play_timer.stop()

    def _play_tick(self):
        v = self.scrubber.value()
        if v >= self.scrubber.maximum():
            self.play_btn.setChecked(False)
            return
        self.scrubber.setValue(v + 1)

    def _show_scene(self, scene, preserve=True):
        self._last_scene = scene
        self.view3d.set_scene(scene, preserve_view=preserve)
        self.profile_view.update_scene(scene)
        self.plan_view.update_scene(scene)
        self._refresh_map_overlay()

    def _on_hover(self, text: str):
        self.hover_label.setText(text or " ")

    # ------------------------------------------------------------- modes

    def _active_config(self) -> str:
        """'single' | 'bu' | 'bight' for the currently relevant geometry."""
        mode = self.mode_combo.currentData()
        if mode == "operation":
            return {"bu_deployment": "bu", "bu_full": "bu",
                    "final_bight": "bight"}.get(
                self.scenario_combo.currentData(), "single")
        if mode == "static":
            return self.static_config.currentData() or "single"
        return "single"

    def _update_config_visibility(self, *args):
        mode = self.mode_combo.currentData()
        config = self._active_config()
        # Shared BU / splice properties section.
        gbtn, gbody = self._collapsibles["bu_bight"]
        show_geo = config in ("bu", "bight")
        gbtn.setVisible(show_geo)
        gbody.setVisible(show_geo and gbtn.isChecked())
        for lbl, w in self._bu_geo_rows:
            lbl.setVisible(config == "bu")
            w.setVisible(config == "bu")
        for lbl, w in self._fb_geo_rows:
            lbl.setVisible(config == "bight")
            w.setVisible(config == "bight")
        # Position & heading section: scenario-specific bearing rows and
        # the guided setup button caption.
        for lbl, w in self._pos_bu_rows:
            lbl.setVisible(config == "bu")
            w.setVisible(config == "bu")
        for lbl, w in self._pos_fs_rows:
            lbl.setVisible(config == "bight")
            w.setVisible(config == "bight")
        show_course = config != "bight"
        self._lay_az_label.setVisible(show_course)
        self.lay_az.setVisible(show_course)
        if config == "bight":
            self.setup_map_btn.setText("Set up on map:  end A, end B...")
            self.setup_map_btn.setToolTip(
                "Click laid end A, then laid end B on the map canvas. Sets "
                "the separation and axis bearing, and centres the local "
                "frame (and vessel) on the midpoint.")
        elif config == "bu":
            self.setup_map_btn.setText(
                "Set up on map:  position, heading, leg 1, leg 2...")
            self.setup_map_btn.setToolTip(
                "Four clicks on the map canvas: 1) the BU setup position "
                "(local origin), 2) a point in the steaming direction, "
                "3) a point along leg 1's lead, 4) a point along leg 2's lead.")
        else:
            self.setup_map_btn.setText("Set up on map:  position, heading...")
            self.setup_map_btn.setToolTip(
                "Click the ship position on the map canvas, then a second "
                "point in the steaming direction. Sets the local origin and "
                "the ship course.")
        # Solve-section rows.
        is_static = mode == "static"
        single_solve = (mode == "steady") or (is_static and config == "single")
        for lbl, w in self._solve_rows:
            lbl.setVisible(single_solve)
            w.setVisible(single_solve)
        for lbl, w in self._bu_hold_rows:
            lbl.setVisible(is_static and config == "bu")
            w.setVisible(is_static and config == "bu")
        for lbl, w in self._fb_hold_rows:
            lbl.setVisible(is_static and config == "bight")
            w.setVisible(is_static and config == "bight")
        self._schedule()

    def _on_mode_changed(self, *args):
        mode = self.mode_combo.currentData()
        is_op = mode == "operation"
        self.run_btn.setText("Run simulation" if is_op else "Solve")
        # Cancel and progress apply to every mode (static relaxations can be
        # long too); they are enabled/animated only while a worker runs.
        self.op_progress.setVisible(True)
        self.cancel_btn.setVisible(True)
        if self._dirty:
            self._set_dirty(True)  # re-word the indicator for the new mode
        self._operation_box.setVisible(True)
        btn, body = self._collapsibles["operation"]
        btn.setVisible(is_op)
        body.setVisible(is_op and btn.isChecked())
        sbtn, sbody = self._collapsibles["solve"]
        sbtn.setVisible(not is_op)
        sbody.setVisible((not is_op) and sbtn.isChecked())
        self._ship_speed_label.setVisible(mode != "static")
        self.ship_speed.setVisible(mode != "static")
        self._slack_label.setVisible(mode != "static")
        self.slack.setVisible(mode != "static")
        self.scrub_widget.setVisible(is_op and bool(self._last_out and self._last_out.snapshots))
        self._update_config_visibility()

    def _on_bathy_mode(self, *args):
        mode = self.bathy_mode.currentData()
        for lbl, w in self._slope_rows:
            lbl.setVisible(mode == "slope")
            w.setVisible(mode == "slope")
        self._profile_label.setVisible(mode == "profile")
        self._profile_rows_widget.setVisible(mode == "profile")
        for lbl, w in self._raster_rows:
            lbl.setVisible(mode == "grid")
            w.setVisible(mode == "grid")
        if mode == "grid":
            self._refresh_raster_layers()
        self._schedule()

    def _on_scenario_changed(self, *args):
        idx = {"bu_deployment": 0, "bu_full": 1, "final_bight": 2,
               "straight_lay": 3}.get(self.scenario_combo.currentData(), 0)
        self.op_stack.setCurrentIndex(idx)
        self._update_config_visibility()

    # ------------------------------------------------------------- QGIS

    def _refresh_raster_layers(self):
        self.raster_combo.clear()
        try:
            from .qgis_adapters import list_raster_layers

            for lid, name in list_raster_layers():
                self.raster_combo.addItem(name, lid)
        except Exception:
            pass
        if self.raster_combo.count() == 0:
            self.raster_combo.addItem("(no raster layers)", "")

    def _sample_raster(self):
        lid = self.raster_combo.currentData()
        if not lid:
            QMessageBox.information(self, "Sample raster", "No raster layer selected.")
            return
        try:
            from .qgis_adapters import sample_raster_bathymetry

            centre = (0.0, 0.0)
            if self._picked_centre is not None:
                centre = self._picked_centre
            elif self.iface is not None:
                c = self.iface.mapCanvas().center()
                centre = (c.x(), c.y())
            app = None
            try:
                from qgis.PyQt.QtWidgets import QApplication as app
                cursor_shape = getattr(Qt, "CursorShape", Qt)
                app.setOverrideCursor(QtGui.QCursor(cursor_shape.WaitCursor))
            except Exception:
                app = None
            try:
                grid = sample_raster_bathymetry(
                    lid, centre, float(self.raster_extent.value()),
                    depths_positive_down=self.raster_positive_down.isChecked(),
                )
            finally:
                if app is not None:
                    app.restoreOverrideCursor()
            self._grid_origin = {"origin_map_xy": grid.pop("origin_map_xy"),
                                 "crs_authid": grid.pop("crs_authid")}
            # Adopt the sampling centre as the explicit origin so the coordinate
            # boxes show where the grid is anchored (even if it came from the
            # canvas centre rather than an earlier pick).
            self._picked_centre = (float(centre[0]), float(centre[1]))
            self._origin_set = True
            self._set_origin_boxes(centre)
            grid["kind"] = "grid"
            self._grid_bathy = grid
            d = np.asarray(grid["depths"], dtype=float)
            nodata_pct = 100.0 * float(grid.get("nodata_fraction", 0.0))
            status = (f"Sampled {d.shape[1]}x{d.shape[0]} grid, "
                      f"depth {d.min():.0f}-{d.max():.0f} m.")
            if nodata_pct >= 0.5:
                status += f" {nodata_pct:.0f}% nodata (filled from neighbours)."
            self.raster_status.setText(status)
            self.raster_status.setStyleSheet(
                "color:#c07f00; font-weight:bold;" if nodata_pct >= 20.0 else "")
            if nodata_pct >= 20.0:
                self._push_map_message(
                    f"Warning: {nodata_pct:.0f}% of the sampled bathymetry "
                    "window is nodata — the filled area may misplace the "
                    "touchdown point. Consider a smaller extent or another "
                    "raster.")
            self._update_origin_label()
            self._schedule()
        except Exception as exc:
            QMessageBox.warning(self, "Sample raster", f"Sampling failed:\n{exc}")

    def _send_to_map(self):
        out = self._last_out
        if out is None or out.scene is None:
            QMessageBox.information(self, "Send to map", "Nothing to export yet.")
            return
        try:
            from .qgis_adapters import push_chains_to_map, push_markers_to_map

            origin, crs = (self._scene_origin if self._scene_origin
                           else self._origin_for_map())
            chains = [(p.name, np.asarray(p.xyz)) for p in out.scene.cables]
            push_chains_to_map("Lay simulator result", chains, origin, crs)
            # Operation runs: add key deployment points (BU landing, laid
            # ends, target) as a labelled point layer.
            markers = []
            if out.snapshots:
                last = out.snapshots[-1]
                for name, xyz in (last.junction_xyz or {}).items():
                    markers.append((f"{name} final position", tuple(xyz)))
                if self.scenario_combo.currentData() == "bu_full":
                    markers.append(("Target BU landing",
                                    (float(self.bf_target_x.value()),
                                     float(self.bf_target_y.value()), 0.0)))
            for m in out.scene.markers:
                if m.label:
                    markers.append((m.label, tuple(m.xyz)))
            if markers:
                push_markers_to_map("Lay simulator points", markers, origin, crs)
            QMessageBox.information(self, "Send to map", "Memory layer(s) added to the project.")
        except Exception as exc:
            QMessageBox.warning(self, "Send to map", f"Export failed:\n{exc}")

    def _origin_for_map(self) -> Tuple[Tuple[float, float], str]:
        """Local-frame origin in map coordinates + CRS authid.

        Preference: sampled-raster origin, then a picked centre, then the
        visible canvas centre."""
        origin = (0.0, 0.0)
        crs = "EPSG:3857"
        if self.iface is not None:
            try:
                from qgis.core import QgsProject

                crs = QgsProject.instance().crs().authid() or crs
            except Exception:
                pass
        if self._grid_origin:
            return tuple(self._grid_origin["origin_map_xy"]), self._grid_origin["crs_authid"]
        if self._picked_centre is not None:
            return self._picked_centre, crs
        if self.iface is not None:
            try:
                c = self.iface.mapCanvas().center()
                origin = (c.x(), c.y())
            except Exception:
                pass
        return origin, crs

    # ------------------------------------------------- map picking / overlay

    def _map_canvas(self):
        try:
            return self.iface.mapCanvas() if self.iface is not None else None
        except Exception:
            return None

    def _start_pick(self, n_points: int, on_done, prompts=None):
        """Fade the dialog behind QGIS, collect ``n_points`` canvas clicks,
        then restore it (less disorienting than minimizing the window).

        ``prompts`` (optional) is one short instruction per click, shown in
        the QGIS message bar as the sequence advances."""
        canvas = self._map_canvas()
        if canvas is None:
            QMessageBox.information(self, "Pick on map",
                                    "Map picking needs the QGIS map canvas.")
            return
        try:
            from .map_tools import PointSequenceTool
        except Exception as exc:
            QMessageBox.warning(self, "Pick on map", f"Map tools unavailable:\n{exc}")
            return
        if self._pick_tool is not None:
            try:
                self._pick_tool.cancel()
            except Exception:
                pass
            self._pick_tool = None
        # Keep the window open but ghosted and behind QGIS so the canvas is
        # clickable and the user keeps their bearings.
        try:
            self.setWindowOpacity(0.3)
            self.lower()
        except Exception:
            self.showMinimized()

        def prompt(i: int):
            if prompts and i < len(prompts):
                self._push_map_message(
                    f"Click {i + 1}/{n_points}: {prompts[i]}  (right-click cancels)")

        def done(points):
            self._pick_tool = None
            self._restore_after_pick()
            try:
                on_done(points)
            except Exception as exc:
                QMessageBox.warning(self, "Pick on map", f"Could not apply pick:\n{exc}")

        def cancelled():
            self._pick_tool = None
            self._restore_after_pick()

        prompt(0)
        self._pick_tool = PointSequenceTool(canvas, n_points, done, cancelled,
                                            on_progress=prompt)

    def _push_map_message(self, text: str):
        try:
            self.iface.messageBar().pushMessage("Lay simulator", text, duration=6)
        except Exception:
            pass

    def _restore_after_pick(self):
        try:
            self.setWindowOpacity(1.0)
        except Exception:
            pass
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _set_local_origin(self, xy: Tuple[float, float], update_boxes: bool = True):
        """Adopt a map point as the local frame origin (vessel / BU setup
        position); resample the raster there when one is in use. ``update_boxes``
        mirrors the coordinates into the origin spin boxes (skip it when the
        boxes are the source of the change to avoid a feedback loop)."""
        self._picked_centre = (float(xy[0]), float(xy[1]))
        self._origin_set = True
        if update_boxes:
            self._set_origin_boxes(xy)
        if self.bathy_mode.currentData() == "grid" and self.raster_combo.currentData():
            self._sample_raster()
        else:
            self._grid_origin = None  # stale raster origin no longer applies
            self._schedule()
        self._update_origin_label()

    def _set_origin_boxes(self, xy: Tuple[float, float]):
        for w, v in ((self.origin_x, xy[0]), (self.origin_y, xy[1])):
            w.blockSignals(True)
            w.setValue(float(v))
            w.blockSignals(False)

    def _origin_boxes_changed(self):
        """The user typed origin coordinates: adopt them as the origin."""
        if self._initializing:
            return
        self._set_local_origin(
            (float(self.origin_x.value()), float(self.origin_y.value())),
            update_boxes=False)

    def _pick_position_only(self):
        """Single map click that sets just the origin (leaves headings)."""
        def apply(pts):
            self._set_local_origin(pts[0])

        self._start_pick(1, apply, prompts=("position (local origin)",))

    def _picks_to_local(self, pts):
        """Picked map points -> local metric frame centred on the first
        pick, so bearings/distances are true regardless of the project CRS
        (geographic degrees, feet, ...). Falls back to planar map units if
        the transform machinery is unavailable."""
        try:
            from .qgis_adapters import map_points_to_local

            _origin, crs = self._origin_for_map()
            return map_points_to_local(pts, pts[0], crs)
        except Exception:
            x0, y0 = pts[0]
            return [(x - x0, y - y0) for x, y in pts]

    def _pick_ship_position(self):
        def apply(pts):
            from .map_tools import bearing_deg

            self._set_local_origin(pts[0])
            if len(pts) > 1:
                loc = self._picks_to_local(pts)
                self.lay_az.setValue(bearing_deg(loc[0], loc[1]))

        self._start_pick(2, apply, prompts=(
            "ship position (local origin)",
            "a point in the steaming direction",
        ))

    def _pick_bu_setup(self):
        def apply(pts):
            from .map_tools import bearing_deg

            self._set_local_origin(pts[0])
            loc = self._picks_to_local(pts)
            self.lay_az.setValue(bearing_deg(loc[0], loc[1]))
            self.bu_leg1_az.setValue(bearing_deg(loc[0], loc[2]))
            self.bu_leg2_az.setValue(bearing_deg(loc[0], loc[3]))

        self._start_pick(4, apply, prompts=(
            "BU setup position (local origin)",
            "a point in the steaming direction",
            "a point along leg 1's lead",
            "a point along leg 2's lead",
        ))

    def _pick_bight_ends(self):
        def apply(pts):
            from .map_tools import bearing_deg

            loc = self._picks_to_local(pts)
            (ax, ay), (bx, by) = loc[0], loc[1]
            self.fb_sep.setValue(math.hypot(bx - ax, by - ay))
            self.fb_axis.setValue(bearing_deg(loc[0], loc[1]))
            # Origin on the midpoint, mapped back from the local frame.
            try:
                from .qgis_adapters import local_points_to_map

                _origin, crs = self._origin_for_map()
                mid = local_points_to_map(
                    [((ax + bx) / 2.0, (ay + by) / 2.0)], pts[0], crs)[0]
            except Exception:
                mid = ((pts[0][0] + pts[1][0]) / 2.0, (pts[0][1] + pts[1][1]) / 2.0)
            self._set_local_origin(mid)

        self._start_pick(2, apply, prompts=(
            "laid end A",
            "laid end B",
        ))

    def _refresh_map_overlay(self, *_a):
        canvas = self._map_canvas()
        if canvas is None:
            return
        want = bool(getattr(self, "show_on_map", None) and self.show_on_map.isChecked())
        scene = getattr(self, "_last_scene", None)
        if not want or scene is None:
            if self._map_overlay is not None:
                self._map_overlay.clear()
            return
        try:
            from .map_tools import SimulatorMapOverlay

            if self._map_overlay is None:
                self._map_overlay = SimulatorMapOverlay(canvas)
            # Draw at the origin the scene was SOLVED with, so re-picking a
            # new origin can't shift the overlay until the next solve.
            origin, crs = (self._scene_origin if self._scene_origin
                           else self._origin_for_map())
            self._map_overlay.update(scene, origin, crs)
        except Exception:
            pass  # overlay is best-effort; never break the solve flow

    def _clear_map_overlay(self):
        if self._map_overlay is not None:
            try:
                self._map_overlay.clear()
            except Exception:
                pass
        if getattr(self, "show_on_map", None) is not None and self.show_on_map.isChecked():
            self.show_on_map.setChecked(False)

    def _cleanup_map_artifacts(self):
        if self._pick_tool is not None:
            try:
                self._pick_tool.cancel()
            except Exception:
                pass
            self._pick_tool = None
        if self._map_overlay is not None:
            try:
                self._map_overlay.clear()
            except Exception:
                pass

    def reject(self):
        self._cleanup_map_artifacts()
        super().reject()

    # ------------------------------------------------------------- export

    def _export_csv(self):
        out = self._last_out
        if out is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "lay_simulator.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            from . import exporters

            if out.snapshots:
                header, rows = exporters.timeline_csv_rows(out.snapshots)
            else:
                header, rows = _scene_csv(out.scene)
            exporters.write_csv(path, header, rows)
        except Exception as exc:
            QMessageBox.warning(self, "Export CSV", f"Export failed:\n{exc}")

    def _export_schedule_csv(self):
        out = self._last_out
        if out is None or not out.snapshots:
            QMessageBox.information(
                self, "Export ops schedule",
                "Run an operation simulation first — the schedule sheet is "
                "built from its timeline.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export ops schedule", "deployment_schedule.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            from . import exporters

            header, rows = exporters.schedule_csv_rows(out.snapshots)
            exporters.write_csv(path, header, rows)
        except Exception as exc:
            QMessageBox.warning(self, "Export ops schedule", f"Export failed:\n{exc}")

    def _export_dxf(self):
        out = self._last_out
        if out is None or out.scene is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export DXF", "lay_simulator.dxf", "DXF (*.dxf)")
        if not path:
            return
        try:
            from . import exporters

            exporters.scene_to_dxf_3d(out.scene, path)
        except Exception as exc:
            QMessageBox.warning(self, "Export DXF", f"Export failed:\n{exc}")

    # ------------------------------------------------------------ settings

    def _capture_defaults(self):
        """Snapshot factory-default widget values (taken before the settings
        restore) so 'Reset all inputs' works without a restart."""
        snap: Dict[str, Any] = {}
        for key, w in self._registry:
            try:
                if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                    snap[key] = w.value()
                elif isinstance(w, QComboBox):
                    snap[key] = w.currentIndex()
                elif isinstance(w, QCheckBox):
                    snap[key] = w.isChecked()
                elif isinstance(w, QLineEdit):
                    snap[key] = w.text()
            except Exception:
                pass
        self._factory_defaults = snap

    def _reset_defaults(self):
        btn = QMessageBox.question(
            self, "Reset inputs",
            "Reset every input, table and saved value of the Cable Lay "
            "Simulator to its factory default?")
        if btn != QMessageBox.Yes:
            return
        self._initializing = True
        try:
            for key, w in self._registry:
                if key not in self._factory_defaults:
                    continue
                val = self._factory_defaults[key]
                try:
                    if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                        w.setValue(val)
                    elif isinstance(w, QComboBox):
                        w.setCurrentIndex(int(val))
                    elif isinstance(w, QCheckBox):
                        w.setChecked(bool(val))
                    elif isinstance(w, QLineEdit):
                        w.setText(str(val))
                except Exception:
                    pass
            self.profile_table.setRowCount(0)
            self.current_table.setRowCount(0)
            self.asm_table.setRowCount(0)
            self._asm_add_segment()
            self._grid_bathy = None
            self._grid_origin = None
            self._picked_centre = None
            self._origin_set = False
            self.raster_status.setText("No grid sampled.")
            self.settings.clear()
        finally:
            self._initializing = False
        self._apply_scenario_choice()
        self._on_bathy_mode()
        self._on_scenario_changed()
        self._update_solve_value_units()
        self._update_origin_label()
        self._set_dirty(True)

    def _restore_settings(self):
        for key, w in self._registry:
            val = self.settings.value(f"w_{key}")
            if val is None:
                continue
            try:
                if isinstance(w, QDoubleSpinBox):
                    w.setValue(float(val))
                elif isinstance(w, QSpinBox):
                    w.setValue(int(float(val)))
                elif isinstance(w, QComboBox):
                    i = w.findData(val)
                    if i >= 0:
                        w.setCurrentIndex(i)
                elif isinstance(w, QCheckBox):
                    w.setChecked(str(val) in ("1", "true", "True"))
                elif isinstance(w, QLineEdit):
                    w.setText(str(val))
            except Exception:
                pass
        raw = self.settings.value("assembly_json")
        if raw:
            self._assembly_from_json(str(raw))
        else:
            self._asm_add_segment()
        raw = self.settings.value("current_json")
        if raw:
            try:
                for row in json.loads(str(raw)):
                    r = self.current_table.rowCount()
                    self.current_table.insertRow(r)
                    self.current_table.setItem(r, 0, QTableWidgetItem(str(row.get("depth_m", 0))))
                    self.current_table.setItem(r, 1, QTableWidgetItem(str(row.get("speed_mps", 0))))
                    self.current_table.setItem(r, 2, QTableWidgetItem(str(row.get("direction_deg", 0))))
            except Exception:
                pass
        raw = self.settings.value("profile_json")
        if raw:
            try:
                for d, z in json.loads(str(raw)):
                    r = self.profile_table.rowCount()
                    self.profile_table.insertRow(r)
                    self.profile_table.setItem(r, 0, QTableWidgetItem(str(d)))
                    self.profile_table.setItem(r, 1, QTableWidgetItem(str(z)))
            except Exception:
                pass
        raw = self.settings.value("schedule_json")
        if raw:
            try:
                rows = json.loads(str(raw))
                if rows:
                    # Stored rows are engine-frame (math degrees).
                    self._schedule_to_table(rows, course_is_compass=False)
            except Exception:
                pass
        for key, (btn, body) in self._collapsibles.items():
            val = self.settings.value(f"section_{key}")
            if val is not None:
                expanded = str(val) == "1"
                btn.setChecked(expanded)
                body.setVisible(expanded)
        # Migrate pre-scenario-picker settings (mode + static config saved
        # separately) onto the single scenario choice, then sync internals.
        if self.settings.value("w_scenario_choice") is None:
            mode = self.mode_combo.currentData()
            if mode == "steady":
                choice = "single_steady"
            elif mode == "operation":
                choice = "operation"
            else:
                choice = {"bu": "bu_static", "bight": "fs_static"}.get(
                    self.static_config.currentData(), "single_static")
            i = self.scenario_choice.findData(choice)
            if i >= 0:
                self.scenario_choice.blockSignals(True)
                self.scenario_choice.setCurrentIndex(i)
                self.scenario_choice.blockSignals(False)
        self._apply_scenario_choice()
        # Restore the explicit origin (the coordinate boxes were restored with
        # the rest of the registry above).
        if str(self.settings.value("origin_set")) in ("1", "true", "True"):
            self._origin_set = True
            self._picked_centre = (float(self.origin_x.value()),
                                   float(self.origin_y.value()))
        self._update_origin_label()
        self._on_bathy_mode()
        self._on_scenario_changed()
        try:
            self.view3d.set_z_exaggeration(float(self.zex.value()))
            self.view3d.set_cable_color_mode(self.color_mode.currentData())
        except Exception:
            pass

    def _save_settings(self):
        for key, w in self._registry:
            try:
                if isinstance(w, QDoubleSpinBox):
                    self.settings.setValue(f"w_{key}", w.value())
                elif isinstance(w, QSpinBox):
                    self.settings.setValue(f"w_{key}", w.value())
                elif isinstance(w, QComboBox):
                    self.settings.setValue(f"w_{key}", w.currentData())
                elif isinstance(w, QCheckBox):
                    self.settings.setValue(f"w_{key}", "1" if w.isChecked() else "0")
                elif isinstance(w, QLineEdit):
                    self.settings.setValue(f"w_{key}", w.text())
            except Exception:
                pass
        self.settings.setValue("origin_set", "1" if self._origin_set else "0")
        self.settings.setValue("assembly_json", json.dumps(self._assembly_json()))
        self.settings.setValue("current_json", json.dumps(self._current_cfg()))
        pts = []
        for r in range(self.profile_table.rowCount()):
            d = _of(self.profile_table.item(r, 0))
            z = _of(self.profile_table.item(r, 1))
            if d is not None and z is not None:
                pts.append([d, z])
        self.settings.setValue("profile_json", json.dumps(pts))
        try:
            self.settings.setValue("schedule_json",
                                   json.dumps(self._schedule_from_table()))
        except Exception:
            pass

    def closeEvent(self, event):  # noqa: N802 - Qt API
        self._save_settings()
        self._cancel_worker()
        self._cleanup_map_artifacts()
        super().closeEvent(event)


# ---------------------------------------------------------------------------

def _f(item, default: float = 0.0) -> float:
    try:
        return float(item.text()) if item is not None and item.text().strip() != "" else default
    except (TypeError, ValueError):
        return default


def _of(item) -> Optional[float]:
    try:
        t = item.text().strip() if item is not None else ""
        return float(t) if t != "" else None
    except (TypeError, ValueError):
        return None


def _s(item) -> str:
    return item.text().strip() if item is not None and item.text() else ""


def _scene_csv(scene) -> Tuple[List[str], List[List]]:
    header = ["chain", "s_m", "x_m", "y_m", "z_m", "depth_m", "tension_kN", "contact"]
    rows: List[List] = []
    if scene is None:
        return header, rows
    for p in scene.cables:
        xyz = np.asarray(p.xyz)
        n = len(xyz)
        s = p.s_m if p.s_m is not None else np.zeros(n)
        t = p.tension_kN if p.tension_kN is not None else np.zeros(n)
        contact = p.contact if p.contact is not None else np.zeros(n, dtype=bool)
        for i in range(n):
            rows.append([p.name, f"{s[i]:.2f}", f"{xyz[i, 0]:.2f}", f"{xyz[i, 1]:.2f}",
                         f"{xyz[i, 2]:.2f}", f"{-xyz[i, 2]:.2f}", f"{t[i]:.3f}", int(bool(contact[i]))])
    return header, rows
