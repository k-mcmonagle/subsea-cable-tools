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
    from qgis.PyQt.QtCore import Qt, QSettings, QTimer
    from qgis.PyQt.QtWidgets import (
        QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
        QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
        QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget, QTabWidget,
        QTableWidget, QTableWidgetItem, QTextEdit, QToolButton, QVBoxLayout,
        QWidget,
    )
except Exception:  # pragma: no cover - standalone testing
    from PyQt5 import QtCore, QtGui
    from PyQt5.QtCore import Qt, QSettings, QTimer
    from PyQt5.QtWidgets import (
        QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
        QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
        QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget, QTabWidget,
        QTableWidget, QTableWidgetItem, QTextEdit, QToolButton, QVBoxLayout,
        QWidget,
    )

from .results_panel import render_results_html
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
    ("bu_deployment", "Branching-unit deployment"),
    ("final_bight", "Final bight lay-down"),
    ("straight_lay", "Straight lay (transient)"),
]

# Assembly table columns.
COL_TYPE, COL_NAME, COL_LEN, COL_QW, COL_QA, COL_LOAD, COL_MU, COL_EI, COL_MBR, \
    COL_DIA, COL_CDN, COL_CDT, COL_COLOR = range(13)
ASM_HEADERS = ["Type", "Name", "Length\n(m)", "Wt water\n(N/m)", "Wt air\n(N/m)",
               "Load\n(kN)", "Friction\nmu", "EI\n(kN.m2)", "MBR\n(m)",
               "Dia\n(m)", "Cd\nnormal", "Cd\ntangential", "Color"]


class LaySimulatorDialog(QDialog):
    """Cable Lay Simulator (3D) — beta."""

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

        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._solve_now)

        self._build_ui()
        self._restore_settings()
        self._initializing = False
        self._on_mode_changed()
        QTimer.singleShot(0, self._solve_now)
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

        form.addWidget(self._section_mode())
        form.addWidget(self._section_environment())
        form.addWidget(self._section_assembly())
        form.addWidget(self._section_bu_bight())
        form.addWidget(self._section_vessel())
        form.addWidget(self._section_solve())
        form.addWidget(self._section_operation())
        form.addWidget(self._section_display())
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
        scrub_row.addWidget(QLabel("Timeline:"))
        scrub_row.addWidget(self.scrubber, 1)
        scrub_row.addWidget(self.scrub_label)
        self.scrub_widget = QWidget()
        self.scrub_widget.setLayout(scrub_row)
        self.scrub_widget.setVisible(False)
        bl.addWidget(self.scrub_widget)

        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setMinimumHeight(90)
        bl.addWidget(self.results, 1)

        self.hover_label = QLabel(" ")
        self.hover_label.setStyleSheet("color:#555;")
        bl.addWidget(self.hover_label)

        btn_row = QHBoxLayout()
        self.btn_csv = QPushButton("Export CSV...")
        self.btn_dxf = QPushButton("Export DXF (3D)...")
        self.btn_map = QPushButton("Send to map")
        self.btn_csv.clicked.connect(self._export_csv)
        self.btn_dxf.clicked.connect(self._export_dxf)
        self.btn_map.clicked.connect(self._send_to_map)
        for b in (self.btn_csv, self.btn_dxf, self.btn_map):
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
        return box, lay

    def _auto_size_table(self, table: QTableWidget, min_rows: int = 3, max_rows: int = 8):
        """Size a table to show its rows (between min_rows and max_rows) so
        several rows are visible at once; beyond max_rows it scrolls
        internally, and the whole left panel scrolls anyway."""

        try:
            table.verticalHeader().setMinimumSectionSize(22)
            table.verticalHeader().setDefaultSectionSize(24)
        except Exception:
            pass

        def update(*_a):
            rows = max(min_rows, min(max_rows, table.rowCount()))
            try:
                rh = table.verticalHeader().defaultSectionSize() or 24
            except Exception:
                rh = 24
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
        box, lay = self._collapsible("Mode", "mode")
        self.mode_combo = self._combo("mode", MODES)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        lay.addRow("Tool mode", self.mode_combo)
        return box

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
        self.slope_azimuth = self._dspin("slope_azimuth_deg", -360.0, 360.0, 0.0, 5.0, 0, " deg")
        self._slope_rows = [
            (QLabel("Down-slope angle"), self.slope_deg),
            (QLabel("Down-slope azimuth"), self.slope_azimuth),
        ]
        for lbl, w in self._slope_rows:
            lay.addRow(lbl, w)

        self.profile_table = QTableWidget(0, 2)
        self.profile_table.setHorizontalHeaderLabels(["Distance from vessel (m)", "Depth (m)"])
        self._auto_size_table(self.profile_table, min_rows=3, max_rows=8)
        self.profile_table.cellChanged.connect(self._schedule)
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
        pv.addLayout(prof_btns)
        self._profile_label = QLabel("Profile")
        lay.addRow(self._profile_label, self._profile_rows_widget)

        # QGIS raster sampling.
        self.raster_combo = QComboBox()
        self.raster_extent = self._dspin("raster_half_extent_m", 100.0, 100000.0, 2000.0, 100.0, 0, " m")
        self.raster_positive_down = self._check("raster_positive_down", "Raster stores positive-down depths", True)
        self.raster_sample_btn = QPushButton("Sample raster around map centre")
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
        self.current_table.setHorizontalHeaderLabels(["Depth (m)", "Speed (m/s)", "Direction (deg)"])
        self._auto_size_table(self.current_table, min_rows=2, max_rows=6)
        self.current_table.cellChanged.connect(self._schedule)
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
        cv.addLayout(cur_btns)
        lay.addRow("Current vs depth", cur_holder)
        return box

    def _section_assembly(self):
        box, lay = self._collapsible("Cable assembly", "assembly")
        self.asm_table = QTableWidget(0, len(ASM_HEADERS))
        self.asm_table.setHorizontalHeaderLabels(ASM_HEADERS)
        self._auto_size_table(self.asm_table, min_rows=3, max_rows=8)
        self.asm_table.cellChanged.connect(self._schedule)
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
        hv.addLayout(btns)
        lay.addRow(holder)
        note = QLabel("Ordered from the chute down. Blank = use the defaults below. "
                      "Compatible with Catenary Calculator V2 assembly JSON.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777; font-size: small;")
        lay.addRow(note)

        self.def_qw = self._dspin("def_q_water", -5000.0, 50000.0, 200.0, 10.0, 1, " N/m")
        self.def_dia = self._dspin("def_dia", 0.0, 1.0, 0.035, 0.005, 3, " m")
        self.def_cdn = self._dspin("def_cdn", 0.0, 5.0, 1.2, 0.05, 2)
        self.def_cdt = self._dspin("def_cdt", 0.0, 1.0, 0.01, 0.005, 3)
        self.def_mu = self._dspin("def_mu", 0.0, 3.0, 0.3, 0.05, 2)
        self.def_ei = self._dspin("def_ei", 0.0, 10000.0, 0.0, 1.0, 1, " kN.m2")
        self.def_mbr = self._dspin("def_mbr", 0.0, 100.0, 0.0, 0.5, 1, " m")
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
        self.lay_az = self._dspin("lay_az", -360.0, 360.0, 0.0, 5.0, 0, " deg")
        self.ship_speed = self._dspin("ship_speed_kn", 0.0, 12.0, 6.0, 0.25, 2, " kn")
        self.slack = self._dspin("slack_pct", -10.0, 30.0, 2.0, 0.5, 1, " %")
        self.chute_mu = self._dspin("chute_mu", 0.0, 1.0, 0.3, 0.05, 2)
        lay.addRow("Chute height above waterline", self.chute_h)
        lay.addRow("Lay azimuth (ship course)", self.lay_az)
        self._ship_speed_label = QLabel("Ship speed")
        lay.addRow(self._ship_speed_label, self.ship_speed)
        self._slack_label = QLabel("Slack")
        lay.addRow(self._slack_label, self.slack)
        lay.addRow("Chute friction mu (capstan)", self.chute_mu)
        return box

    def _section_bu_bight(self):
        """Physical geometry shared by the static-hold configurations and
        the operation scenarios (BU or bight)."""
        box, lay = self._collapsible("BU / bight geometry", "bu_bight")
        # Branching-unit group.
        self.bu_weight = self._dspin("bu_weight_kN", 0.1, 500.0, 15.0, 1.0, 1, " kN")
        self.bu_cda = self._dspin("bu_cda", 0.0, 50.0, 1.5, 0.1, 2, " m2")
        self.bu_leg_len = self._dspin("bu_leg_len", 10.0, 20000.0, 300.0, 10.0, 0, " m")
        self.bu_leg1_az = self._dspin("bu_leg1_az", -360.0, 360.0, 150.0, 5.0, 0, " deg")
        self.bu_leg2_az = self._dspin("bu_leg2_az", -360.0, 360.0, 210.0, 5.0, 0, " deg")
        self._bu_geo_rows = [
            (QLabel("BU submerged weight"), self.bu_weight),
            (QLabel("BU drag area Cd*A"), self.bu_cda),
            (QLabel("Leg length (each)"), self.bu_leg_len),
            (QLabel("Leg 1 azimuth"), self.bu_leg1_az),
            (QLabel("Leg 2 azimuth"), self.bu_leg2_az),
        ]
        for lbl, w in self._bu_geo_rows:
            lay.addRow(lbl, w)
        # Final-bight group.
        self.fb_length = self._dspin("fb_length", 20.0, 20000.0, 300.0, 10.0, 0, " m")
        self.fb_sep = self._dspin("fb_sep", 5.0, 10000.0, 120.0, 10.0, 0, " m")
        self._fb_geo_rows = [
            (QLabel("Bight length (joined loop)"), self.fb_length),
            (QLabel("Laid-end separation"), self.fb_sep),
        ]
        for lbl, w in self._fb_geo_rows:
            lay.addRow(lbl, w)
        self._bu_bight_box = box
        return box

    def _section_solve(self):
        box, lay = self._collapsible("Solve mode (static / steady)", "solve")
        self.static_config = self._combo("static_config", [
            ("single", "Single cable span"),
            ("bu", "Branching unit (held)"),
            ("bight", "Final bight (held)"),
        ])
        self.static_config.currentIndexChanged.connect(self._update_config_visibility)
        self._config_label = QLabel("Configuration (static)")
        lay.addRow(self._config_label, self.static_config)

        self.solve_mode = self._combo("solve_mode", SOLVE_MODES)
        self.solve_value = self._dspin("solve_value", -1e6, 1e6, 5.0, 1.0, 3)
        self.on_bed_tail = self._dspin("on_bed_tail", 10.0, 5000.0, 150.0, 10.0, 0, " m")
        self._solve_rows = [
            (QLabel("Input"), self.solve_mode),
            (QLabel("Value"), self.solve_value),
            (QLabel("On-bed tail beyond TDP (static)"), self.on_bed_tail),
        ]
        for lbl, w in self._solve_rows:
            lay.addRow(lbl, w)

        # Static-hold inputs (BU / bight configurations).
        self.bu_depth = self._dspin("bu_depth_m", 1.0, 8000.0, 20.0, 5.0, 1, " m")
        self.trunk_slack = self._dspin("trunk_slack_pct", 0.0, 50.0, 2.0, 0.5, 1, " %")
        self.apex_depth = self._dspin("apex_depth_m", 1.0, 8000.0, 10.0, 5.0, 1, " m")
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

        self.op_stack = QStackedWidget()

        # BU deployment page (physical geometry lives in "BU / bight geometry").
        bu = QWidget()
        bl = QFormLayout(bu)
        self.bu_payout = self._dspin("bu_payout", 0.01, 3.0, 0.4, 0.05, 2, " m/s")
        self.bu_ship_speed = self._dspin("bu_ship_speed_kn", 0.0, 4.0, 0.6, 0.1, 2, " kn")
        bl.addRow("Trunk pay-out rate", self.bu_payout)
        bl.addRow("Ship speed", self.bu_ship_speed)
        self.op_stack.addWidget(bu)

        # Final bight page.
        fb = QWidget()
        fl = QFormLayout(fb)
        self.fb_payout = self._dspin("fb_payout", 0.01, 3.0, 0.3, 0.05, 2, " m/s")
        self.fb_course = self._dspin("fb_course", -360.0, 360.0, 90.0, 5.0, 0, " deg")
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

        run_row = QHBoxLayout()
        self.run_btn = QPushButton("Run simulation")
        self.run_btn.clicked.connect(self._run_operation)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_worker)
        self.op_progress = QProgressBar()
        self.op_progress.setRange(0, 100)
        run_row.addWidget(self.run_btn)
        run_row.addWidget(self.cancel_btn)
        run_row.addWidget(self.op_progress, 1)
        holder = QWidget()
        holder.setLayout(run_row)
        lay.addRow(holder)
        self._operation_box = box
        return box

    def _section_display(self):
        box, lay = self._collapsible("Display", "display")
        self.zex = self._dspin("z_exaggeration", 0.1, 200.0, 1.0, 0.5, 1, " x")
        self.zex.valueChanged.connect(lambda v: self.view3d.set_z_exaggeration(float(v)))
        self.color_mode = self._combo("color_mode", [("segment", "Color by segment"), ("tension", "Color by tension")])
        self.color_mode.currentIndexChanged.connect(
            lambda _i: self.view3d.set_cable_color_mode(self.color_mode.currentData())
        )
        lay.addRow("Depth exaggeration", self.zex)
        lay.addRow("Cable colors", self.color_mode)
        return box

    # ------------------------------------------------------------- tables

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
        self._schedule()

    def _asm_add_body(self):
        r = self.asm_table.rowCount()
        self.asm_table.insertRow(r)
        vals = ["Body", f"Body {r + 1}", "", "", "", "5.0", "", "", "", "", "", "", ""]
        for c, v in enumerate(vals):
            self.asm_table.setItem(r, c, QTableWidgetItem(v))
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
        self._schedule()

    # ------------------------------------------------------------- config

    def _bathy_cfg(self) -> dict:
        mode = self.bathy_mode.currentData()
        depth = float(self.depth_spin.value())
        if mode == "slope":
            g = math.tan(math.radians(float(self.slope_deg.value())))
            az = math.radians(float(self.slope_azimuth.value()))
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
                        "azimuth_deg": float(self.lay_az.value()) + 180.0}
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
        cfg.ship_speed_kn = float(self.ship_speed.value())
        cfg.slack_percent = float(self.slack.value())
        cfg.solve_mode = self.solve_mode.currentData()
        cfg.solve_value = float(self.solve_value.value())
        cfg.on_bed_tail_m = float(self.on_bed_tail.value())
        cfg.chute_mu = float(self.chute_mu.value())
        cfg.static_config = self.static_config.currentData() or "single"
        cfg.bu_depth_m = float(self.bu_depth.value())
        cfg.trunk_slack_pct = float(self.trunk_slack.value())
        cfg.apex_depth_m = float(self.apex_depth.value())
        cfg.scenario = self.scenario_combo.currentData()
        # The op dict carries the geometry for whichever configuration is
        # active — the static-hold path reads the same keys as the
        # operation scenarios.
        kind = self._active_config() if cfg.mode == "static" else {
            "bu_deployment": "bu", "final_bight": "bight"}.get(cfg.scenario, "single")
        if kind == "bu":
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
        return cfg

    # ------------------------------------------------------------- solving

    def _schedule(self, *args):
        if self._initializing:
            return
        mode = self.mode_combo.currentData()
        if mode == "operation":
            return  # explicit run only
        self._update_timer.start(400)

    def _solve_now(self):
        if self._initializing:
            return
        cfg = self.build_config()
        if cfg.mode == "operation":
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

    def _start_worker(self, cfg: V3Config):
        self._worker = SolveWorker(cfg, self)
        self._worker.finishedWith.connect(self._on_solved)
        self._worker.progressed.connect(self._on_progress)
        self.cancel_btn.setEnabled(cfg.mode == "operation")
        if cfg.mode == "operation":
            self.run_btn.setEnabled(False)
        self.results.setHtml("<i>Solving...</i>")
        self._worker.start()

    def _cancel_worker(self):
        if self._worker is not None:
            self._worker.cancel()

    def _on_progress(self, frac: float, label: str):
        self.op_progress.setValue(int(frac * 100))
        if label:
            self.scrub_label.setText(label)

    def _on_solved(self, out: RunOutput):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._last_out = out
        self.results.setHtml(render_results_html(out))
        if out.error:
            if self._pending:
                self._pending = False
                self._solve_now()
            return

        # Bathy lookup for the profile view's bed-under-cable line.
        try:
            from ..engine.bathymetry import bathymetry_from_dict

            bathy = bathymetry_from_dict(self.build_config().bathymetry)
            self.profile_view.set_bathy_lookup(bathy.depth_at)
        except Exception:
            self.profile_view.set_bathy_lookup(None)

        if out.mode == "operation" and out.snapshots:
            self.scrub_widget.setVisible(True)
            self.scrubber.blockSignals(True)
            self.scrubber.setMaximum(len(out.snapshots) - 1)
            self.scrubber.setValue(len(out.snapshots) - 1)
            self.scrubber.blockSignals(False)
            self._show_scene(out.scene, preserve=False)
            self.op_progress.setValue(100)
            self.scrub_label.setText(f"t = {out.snapshots[-1].t_s:.0f} s")
        else:
            self.scrub_widget.setVisible(False)
            self._show_scene(out.scene, preserve=True)

        if self._pending:
            self._pending = False
            self._solve_now()

    def _on_scrub(self, i: int):
        out = self._last_out
        if out is None or out.scene_builder is None or out.snapshots is None:
            return
        i = max(0, min(len(out.snapshots) - 1, int(i)))
        scene = out.scene_builder(i)
        self.scrub_label.setText(f"t = {out.snapshots[i].t_s:.0f} s")
        self._show_scene(scene, preserve=True)

    def _show_scene(self, scene, preserve=True):
        self.view3d.set_scene(scene, preserve_view=preserve)
        self.profile_view.update_scene(scene)
        self.plan_view.update_scene(scene)

    def _on_hover(self, text: str):
        self.hover_label.setText(text or " ")

    # ------------------------------------------------------------- modes

    def _active_config(self) -> str:
        """'single' | 'bu' | 'bight' for the currently relevant geometry."""
        mode = self.mode_combo.currentData()
        if mode == "operation":
            return {"bu_deployment": "bu", "final_bight": "bight"}.get(
                self.scenario_combo.currentData(), "single")
        if mode == "static":
            return self.static_config.currentData() or "single"
        return "single"

    def _update_config_visibility(self, *args):
        mode = self.mode_combo.currentData()
        config = self._active_config()
        # Shared BU / bight geometry section.
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
        # Solve-section rows.
        is_static = mode == "static"
        self._config_label.setVisible(is_static)
        self.static_config.setVisible(is_static)
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
        idx = {"bu_deployment": 0, "final_bight": 1, "straight_lay": 2}.get(
            self.scenario_combo.currentData(), 0)
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
            if self.iface is not None:
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
            grid["kind"] = "grid"
            self._grid_bathy = grid
            d = np.asarray(grid["depths"], dtype=float)
            self.raster_status.setText(
                f"Sampled {d.shape[1]}x{d.shape[0]} grid, depth {d.min():.0f}-{d.max():.0f} m."
            )
            self._schedule()
        except Exception as exc:
            QMessageBox.warning(self, "Sample raster", f"Sampling failed:\n{exc}")

    def _send_to_map(self):
        out = self._last_out
        if out is None or out.scene is None:
            QMessageBox.information(self, "Send to map", "Nothing to export yet.")
            return
        try:
            from .qgis_adapters import push_chains_to_map

            origin = (0.0, 0.0)
            crs = "EPSG:3857"
            if self._grid_origin:
                origin = self._grid_origin["origin_map_xy"]
                crs = self._grid_origin["crs_authid"]
            elif self.iface is not None:
                try:
                    from qgis.core import QgsProject

                    crs = QgsProject.instance().crs().authid() or crs
                    c = self.iface.mapCanvas().center()
                    origin = (c.x(), c.y())
                except Exception:
                    pass
            chains = [(p.name, np.asarray(p.xyz)) for p in out.scene.cables]
            push_chains_to_map("Lay simulator result", chains, origin, crs)
            QMessageBox.information(self, "Send to map", "Memory layer added to the project.")
        except Exception as exc:
            QMessageBox.warning(self, "Send to map", f"Export failed:\n{exc}")

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

            if out.mode == "operation" and out.snapshots:
                header, rows = exporters.timeline_csv_rows(out.snapshots)
            else:
                header, rows = _scene_csv(out.scene)
            exporters.write_csv(path, header, rows)
        except Exception as exc:
            QMessageBox.warning(self, "Export CSV", f"Export failed:\n{exc}")

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
        for key, (btn, body) in self._collapsibles.items():
            val = self.settings.value(f"section_{key}")
            if val is not None:
                expanded = str(val) == "1"
                btn.setChecked(expanded)
                body.setVisible(expanded)
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
        self.settings.setValue("assembly_json", json.dumps(self._assembly_json()))
        self.settings.setValue("current_json", json.dumps(self._current_cfg()))
        pts = []
        for r in range(self.profile_table.rowCount()):
            d = _of(self.profile_table.item(r, 0))
            z = _of(self.profile_table.item(r, 1))
            if d is not None and z is not None:
                pts.append([d, z])
        self.settings.setValue("profile_json", json.dumps(pts))

    def closeEvent(self, event):  # noqa: N802 - Qt API
        self._save_settings()
        self._cancel_worker()
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
