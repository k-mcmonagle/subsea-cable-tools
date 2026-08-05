# -*- coding: utf-8 -*-
"""BU Lowering Tool — the branching-unit lowering scenario as its own dialog.

A focused extraction of the Cable Lay Simulator's "BU deployment — lowering"
scenario: the BU is lowered on the trunk while the vessel steams ahead, its
two pre-laid legs anchored on the bed. The quick analytic tri-catenary model
drives the interactive runs (sub-second), with a one-click verification run
on the full dynamic-relaxation solver.

Same architecture as the main simulator dialog: inputs on the left in
collapsible sections, 3D/Profile/Plan/Time-series views on the right, all
computation delegated to :mod:`solve_controller` on a worker thread. Only
the inputs the lowering scenario actually consumes are shown. Settings
persist in their own QSettings scope ("BULoweringTool"), independent of the
main simulator.
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
        QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QTabWidget,
        QTableWidget, QTableWidgetItem, QTextEdit, QToolButton, QVBoxLayout,
        QWidget,
    )
    from ....qgis_compat import (
        HEADER_RESIZE_MODE_FIXED,
        HEADER_RESIZE_MODE_INTERACTIVE,
    )
except Exception:  # pragma: no cover - standalone testing
    from PyQt5 import QtCore, QtGui
    from PyQt5.QtCore import Qt, QSettings
    from PyQt5.QtWidgets import (
        QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
        QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
        QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QTabWidget,
        QTableWidget, QTableWidgetItem, QTextEdit, QToolButton, QVBoxLayout,
        QWidget,
    )
    _RESIZE_MODE = getattr(QHeaderView, "ResizeMode", QHeaderView)
    HEADER_RESIZE_MODE_FIXED = _RESIZE_MODE.Fixed
    HEADER_RESIZE_MODE_INTERACTIVE = _RESIZE_MODE.Interactive

from .integration_editor import BUIntegrationEditor
from .results_panel import render_results_html
from .solve_controller import RunOutput, SolveWorker, V3Config
from .view3d import View3DWidget
from .views2d import PlanView, ProfileView

_ORIENT = getattr(Qt, "Orientation", Qt)
_ARROW = getattr(Qt, "ArrowType", Qt)

KMH = 1.0 / 3.6      # km/h -> m/s


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


class BULoweringDialog(QDialog):
    """BU Lowering Tool (3D) — beta."""

    _DEFAULT_COLLAPSED = {"vessel", "advanced", "display"}

    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("BU Lowering Tool (3D) — beta")
        try:
            self.setWindowFlags(self.windowFlags()
                                | getattr(Qt, "WindowType", Qt).WindowMaximizeButtonHint)
        except Exception:
            pass
        # Own settings scope — independent of the Cable Lay Simulator.
        self.settings = QSettings("subsea_cable_tools", "BULoweringTool")
        self._registry: List[Tuple[str, QWidget]] = []
        self._collapsibles: Dict[str, Tuple[QToolButton, QWidget]] = {}
        self._initializing = True
        self._worker: Optional[SolveWorker] = None
        self._last_out: Optional[RunOutput] = None
        self._last_scene = None
        self._grid_bathy: Optional[dict] = None       # sampled raster grid cfg
        self._grid_origin: Optional[dict] = None      # map origin/crs for export
        self._picked_centre: Optional[Tuple[float, float]] = None
        self._origin_set: bool = False
        self._pick_tool = None
        self._map_overlay = None
        self._dirty = False
        self._solve_origin = None
        self._scene_origin = None
        self._syncing_bu = False

        self._build_ui()
        self._capture_defaults()
        self._restore_settings()
        self._initializing = False
        self._set_dirty(True)
        self.dirty_label.setText("Ready — review the inputs and click Run.")
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

        intro = QLabel(
            "Lower a branching unit on its trunk while the vessel steams "
            "ahead, the two pre-laid legs anchored on the bed. Runs on the "
            "quick analytic model (instant); verify the final schedule with "
            "the full solver.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#777; font-size: small;")
        form.addWidget(intro)

        form.addWidget(self._section_position())
        form.addWidget(self._section_environment())
        form.addWidget(self._section_integration())
        form.addWidget(self._section_operation())
        form.addWidget(self._section_vessel())
        form.addWidget(self._section_advanced())
        form.addWidget(self._section_display())
        reset_btn = QPushButton("Reset all inputs to defaults...")
        reset_btn.setToolTip("Restore every input, table and section of this "
                             "tool to its factory default (asks first).")
        reset_btn.clicked.connect(self._reset_defaults)
        form.addWidget(reset_btn)
        form.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left_holder)
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

        # Timeline scrubber.
        scrub_row = QHBoxLayout()
        self.scrub_label = QLabel("t = 0 s")
        self.scrubber = QSlider(getattr(_ORIENT, "Horizontal", 1))
        self.scrubber.setMinimum(0)
        self.scrubber.setMaximum(0)
        self.scrubber.valueChanged.connect(self._on_scrub)
        self.play_btn = QToolButton()
        self.play_btn.setText("▶")
        self.play_btn.setCheckable(True)
        self.play_btn.setToolTip("Play / pause the lowering timeline.")
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

        # Run controls: the quick model is the working loop; the full solver
        # is the one-click confirmation of the same inputs.
        solve_row = QHBoxLayout()
        self.run_btn = QPushButton("Run (quick model)")
        self.run_btn.setToolTip(
            "Simulate the lowering with the analytic tri-catenary model — "
            "closed-form catenaries, frozen-lay seabed, no drag. Solves in "
            "about a second.")
        self.run_btn.clicked.connect(self._run_quick)
        self.verify_btn = QPushButton("Verify (full solver)")
        self.verify_btn.setToolTip(
            "Re-run the same lowering on the full dynamic-relaxation solver "
            "(seabed friction with lay history, bend stiffness, real bed "
            "relief under the spans). Slower — use it to confirm a schedule "
            "the quick model produced.")
        self.verify_btn.clicked.connect(self._run_full)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_worker)
        self.dirty_label = QLabel("")
        self.dirty_label.setStyleSheet("color:#c07f00; font-weight:bold;")
        self.op_progress = QProgressBar()
        self.op_progress.setRange(0, 100)
        solve_row.addWidget(self.run_btn)
        solve_row.addWidget(self.verify_btn)
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
            "Time / vessel position / payout per line / tensions as a CSV "
            "the lay crew can follow.")
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

    # ---- collapsible-section helper ----------------------------------------

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
                btn.setArrowType(getattr(_ARROW, "DownArrow", 2) if checked
                                 else getattr(_ARROW, "RightArrow", 4))
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

    def _auto_size_table(self, table: QTableWidget, min_rows: int = 3, max_rows: int = 8):
        try:
            fm = table.fontMetrics()
            row_h = max(34, fm.height() + 14)
        except Exception:
            row_h = 34
        try:
            vh = table.verticalHeader()
            vh.setMinimumSectionSize(row_h)
            vh.setDefaultSectionSize(row_h)
            vh.setSectionResizeMode(HEADER_RESIZE_MODE_FIXED)
        except Exception:
            pass
        try:
            hh = table.horizontalHeader()
            hh.setSectionResizeMode(HEADER_RESIZE_MODE_INTERACTIVE)
            hh.setStretchLastSection(True)
            hh.setMinimumSectionSize(40)
        except Exception:
            pass

        def update(*_a):
            rows = max(min_rows, min(max_rows, table.rowCount()))
            try:
                rh = table.verticalHeader().defaultSectionSize() or row_h
            except Exception:
                rh = row_h
            try:
                hh_h = table.horizontalHeader().sizeHint().height() or 24
            except Exception:
                hh_h = 24
            h = hh_h + rows * rh + 2 * table.frameWidth() + 20
            table.setMinimumHeight(h)
            table.setMaximumHeight(h)

        try:
            table.model().rowsInserted.connect(update)
            table.model().rowsRemoved.connect(update)
        except Exception:
            pass
        update()

    # ---- input widget helpers ----------------------------------------------

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

    def _pair(self, w1, w2) -> QWidget:
        holder = QWidget()
        h = QHBoxLayout(holder)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(w1)
        h.addWidget(w2)
        return holder

    # ---- sections -----------------------------------------------------------

    def _section_position(self):
        box, lay = self._collapsible("Position && geometry", "position")

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

        self.setup_map_btn = QPushButton(
            "Set up on map:  position, heading, laid ends...")
        self.setup_map_btn.setToolTip(
            "Four clicks on the map canvas: 1) the BU setup position (local "
            "origin), 2) a point in the steaming direction, 3) leg 1's laid "
            "end, 4) leg 2's laid end — pick them off the as-laid route. "
            "Sets the laid-end coordinates and the derived lead bearings.")
        self.setup_map_btn.clicked.connect(self._pick_bu_setup)
        lay.addRow(self.setup_map_btn)
        self.origin_label = QLabel("")
        self.origin_label.setWordWrap(True)
        self.origin_label.setStyleSheet("color:#777; font-size: small;")
        lay.addRow(self.origin_label)

        self.lay_az = self._dspin("lay_az", 0.0, 360.0, 0.0, 5.0, 0, " degN")
        self.lay_az.setToolTip(
            "Ship course as a compass bearing (degrees clockwise from "
            "north). With the planned lowering the course is re-derived to "
            "balance the legs; this sets the initial heading.")
        lay.addRow("Ship course (lay azimuth)", self.lay_az)

        # Laid ends govern when set; lead bearings are then derived. Editing
        # a bearing clears the ends (bearings govern again).
        self.bu_leg1_az = self._dspin("bu_leg1_az", 0.0, 360.0, 150.0, 5.0, 0, " degN")
        self.bu_leg2_az = self._dspin("bu_leg2_az", 0.0, 360.0, 210.0, 5.0, 0, " degN")
        for w in (self.bu_leg1_az, self.bu_leg2_az):
            w.setToolTip(
                "Compass bearing of the leg's bed route away from the BU "
                "setup position. Derived automatically when the laid ends "
                "below are set; editing it clears the laid ends (bearings "
                "then govern).")
            w.valueChanged.connect(self._bu_az_edited)
        ends = {}
        for key in ("bu_end1_x", "bu_end1_y", "bu_end2_x", "bu_end2_y"):
            ends[key] = self._dspin(key, -1e6, 1e6, 0.0, 10.0, 1, " m")
            ends[key].setToolTip(
                "Laid-end position in local metres from the origin (set "
                "with 'Set up on map', or type surveyed coordinates). "
                "(0, 0) = not set — the lead bearing and leg length place "
                "the far end instead.")
            ends[key].valueChanged.connect(self._bu_end_edited)
        self.bu_end1_x, self.bu_end1_y = ends["bu_end1_x"], ends["bu_end1_y"]
        self.bu_end2_x, self.bu_end2_y = ends["bu_end2_x"], ends["bu_end2_y"]
        lay.addRow("Leg 1 laid end x / y", self._pair(self.bu_end1_x, self.bu_end1_y))
        lay.addRow("Leg 2 laid end x / y", self._pair(self.bu_end2_x, self.bu_end2_y))
        lay.addRow("Leg 1 lead bearing", self.bu_leg1_az)
        lay.addRow("Leg 2 lead bearing", self.bu_leg2_az)

        self.bu_leg_len = self._dspin("bu_leg_len", 10.0, 20000.0, 300.0, 10.0, 0, " m")
        self.bu_leg_len.setToolTip(
            "Length of cable in leg 1 from the BU to its laid end — from "
            "the cable counts at jointing. Compared against what the picked "
            "laid-end geometry demands; a mismatch shows up as slack or a "
            "taut leg.")
        self.bu_leg2_len = self._dspin("bu_leg2_len", 0.0, 20000.0, 0.0, 10.0, 0, " m")
        self.bu_leg2_len.setSpecialValueText("same as leg 1")
        self.bu_leg2_len.setToolTip(
            "Length of cable in leg 2 from the BU to its laid end "
            "(0 = same as leg 1).")
        lay.addRow("Leg 1 length (BU to laid end)", self.bu_leg_len)
        lay.addRow("Leg 2 length (BU to laid end)", self.bu_leg2_len)
        self._update_origin_label()
        return box

    def _section_environment(self):
        box, lay = self._collapsible("Environment", "env")
        self.bathy_mode = self._combo("bathy_mode", [
            ("flat", "Flat seabed"), ("slope", "Planar slope"),
            ("profile", "Depth profile (along lay azimuth)"),
            ("grid", "QGIS raster (sampled)"),
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
        self.profile_table.setHorizontalHeaderLabels(
            ["Distance from vessel (m)", "Depth (m)"])
        self._auto_size_table(self.profile_table, min_rows=3, max_rows=8)
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
        pv.addLayout(prof_btns)
        self._profile_label = QLabel("Profile")
        lay.addRow(self._profile_label, self._profile_rows_widget)

        # QGIS raster sampling.
        self.raster_combo = QComboBox()
        self.raster_extent = self._dspin("raster_half_extent_m", 100.0, 100000.0,
                                         2000.0, 100.0, 0, " m")
        self.raster_positive_down = self._check(
            "raster_positive_down", "Raster stores positive-down depths", True)
        self.raster_sample_btn = QPushButton("Sample raster around origin")
        self.raster_sample_btn.setToolTip(
            "Samples a depth grid centred on the local origin (set it with "
            "'Set up on map'; falls back to the visible map centre).")
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
        note = QLabel(
            "No current input: the quick model has no hydrodynamic drag, so "
            "a current would be ignored. Model current loading in the full "
            "Cable Lay Simulator.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777; font-size: small;")
        lay.addRow(note)
        return box

    def _section_integration(self):
        box, lay = self._collapsible("BU && cable make-up (from the BU outward)",
                                     "integration")
        self.bu_weight = self._dspin("bu_weight_kN", 0.1, 500.0, 15.0, 1.0, 1, " kN")
        self.bu_weight.setToolTip("Submerged weight of the branching unit body.")
        lay.addRow("BU submerged weight", self.bu_weight)
        self.integration_editor = BUIntegrationEditor()
        self.integration_editor.changed.connect(self._schedule)
        lay.addRow(self.integration_editor)
        self.def_qw = self._dspin("def_q_water", -5000.0, 50000.0, 200.0, 10.0, 1, " N/m")
        self.def_qw.setToolTip(
            "Submerged weight per metre used wherever a row above leaves "
            "the weight blank.")
        self.def_mu = self._dspin("def_mu", 0.0, 3.0, 0.3, 0.05, 2)
        self.def_mu.setToolTip(
            "Cable-seabed Coulomb friction coefficient used wherever a row "
            "leaves friction blank. Typical: 0.2-0.6 (sand/clay).")
        self.def_mbr = self._dspin("def_mbr", 0.0, 100.0, 0.0, 0.5, 1, " m")
        self.def_mbr.setToolTip(
            "Minimum bend radius limit for the violation check in the "
            "results. 0 disables the check.")
        lay.addRow("Default weight in water", self.def_qw)
        lay.addRow("Default seabed friction mu", self.def_mu)
        lay.addRow("Default MBR limit (0 = off)", self.def_mbr)
        return box

    def _section_operation(self):
        box, lay = self._collapsible("Lowering operation", "operation")
        self.bu_payout = self._dspin("bu_payout_kmh", 0.05, 10.0, 1.4, 0.1, 2, " km/h")
        self.bu_payout.setToolTip("Trunk pay-out rate during the lowering.")
        self.bu_ship_speed = self._dspin("bu_ship_speed_kmh", 0.0, 8.0, 1.1, 0.1, 2, " km/h")
        self.bu_ship_speed.setToolTip(
            "Ship speed over ground. With the planned lowering this drives "
            "the lay-ahead after landing; the descent track is derived.")
        lay.addRow("Trunk pay-out rate", self.bu_payout)
        lay.addRow("Ship speed", self.bu_ship_speed)
        self.bu_start_depth = self._dspin("bu_start_depth_m", 0.0, 8000.0, 0.0, 5.0, 1, " m")
        self.bu_start_depth.setSpecialValueText("auto")
        self.bu_start_depth.setToolTip(
            "BU depth below the surface at the start of the lowering "
            "(auto = just below the surface).")
        lay.addRow("BU start depth", self.bu_start_depth)
        self.bu_duration = self._dspin("bu_duration_s", 0.0, 86400.0, 0.0, 60.0, 0, " s")
        self.bu_duration.setSpecialValueText("auto")
        self.bu_duration.setToolTip(
            "Simulated time (auto = long enough for the BU to land at the "
            "pay-out rate). With the planned lowering this times the "
            "lay-ahead phase.")
        lay.addRow("Duration", self.bu_duration)
        self.bu_leg_btt = self._dspin("bu_leg_btt_kN", 0.0, 500.0, 3.0, 0.5, 1, " kN")
        self.bu_leg_btt.setToolTip(
            "Target touchdown (TDP) tension for BOTH legs — the residual "
            "tension each leg is laid down with. The planned lowering "
            "solves the vessel track and trunk payout to hold this while "
            "the BU descends.")
        lay.addRow("Target leg TDP tension (both legs)", self.bu_leg_btt)
        self.bu_btt = self._dspin("bu_bottom_tension_kN", 0.0, 500.0, 0.0, 0.5, 1, " kN")
        self.bu_btt.setToolTip(
            "Target trunk touchdown (TDP) tension: once the trunk touches "
            "down, a controller trims the scheduled trunk payout to hold "
            "this bottom tension. 0 = off.")
        lay.addRow("Target trunk TDP tension (0 = off)", self.bu_btt)
        self.bu_plan_check = self._check(
            "bu_plan_from_tension",
            "Solve vessel path && payout to hold the leg targets", True)
        self.bu_plan_check.setToolTip(
            "Planned lowering: instead of a single straight run at fixed "
            "rates, derive the balanced vessel track (the course that keeps "
            "both legs at the target TDP tension) and the trunk payout per "
            "phase, then simulate it. Uncheck for the plain fixed-rate run.")
        lay.addRow(self.bu_plan_check)
        self.trunk_slack = self._dspin("trunk_slack_pct", 0.0, 50.0, 2.0, 0.5, 1, " %")
        self.trunk_slack.setToolTip(
            "Trunk length margin over the straight sheave-to-BU distance at "
            "the start; more slack lets the trunk hang deeper.")
        lay.addRow("Trunk slack at start", self.trunk_slack)
        return box

    def _section_vessel(self):
        """Vessel and sheave geometry. The solver hangs the trunk from the
        sheave (departure) point — the tracked vessel position — and the
        hull is drawn around it from the offsets below."""
        box, lay = self._collapsible("Vessel && sheave geometry", "vessel")
        self.chute_h = self._dspin("chute_h", 0.0, 50.0, 5.0, 0.5, 1, " m")
        self.chute_h.setToolTip(
            "Height of the sheave (cable departure point) above the "
            "waterline. Also the drawn hull freeboard.")
        lay.addRow("Sheave height above waterline", self.chute_h)
        self.sheave_radius = self._dspin("sheave_radius_m", 0.0, 30.0, 3.0, 0.5, 1, " m")
        self.sheave_radius.setToolTip(
            "Sheave / overboarding chute radius. The drawn trunk runs over "
            "this arc and leaves at its tangent point, as in the 2D "
            "Catenary Calculator; the solver still hangs the span from the "
            "departure point (the wrap is drawn geometry).")
        lay.addRow("Sheave radius", self.sheave_radius)
        self.ship_len = self._dspin("ship_length_m", 5.0, 400.0, 127.0, 5.0, 0, " m")
        self.ship_beam = self._dspin("ship_beam_m", 2.0, 80.0, 27.0, 1.0, 0, " m")
        self.crp_fwd = self._dspin("crp_fwd_m", -200.0, 200.0, 0.0, 1.0, 1, " m")
        self.crp_stbd = self._dspin("crp_stbd_m", -40.0, 40.0, 0.0, 0.5, 1, " m")
        self.sheave_fwd = self._dspin("sheave_fwd_m", -200.0, 200.0, -63.5, 1.0, 1, " m")
        self.sheave_stbd = self._dspin("sheave_stbd_m", -40.0, 40.0, 0.0, 0.5, 1, " m")
        lay.addRow("Ship length", self.ship_len)
        lay.addRow("Ship breadth", self.ship_beam)
        lay.addRow("CRP forward of midship", self.crp_fwd)
        lay.addRow("CRP starboard of centreline", self.crp_stbd)
        lay.addRow("Sheave forward of CRP", self.sheave_fwd)
        lay.addRow("Sheave starboard of CRP", self.sheave_stbd)
        note = QLabel(
            "The tracked position (origin, plan, exports) is the SHEAVE — "
            "the point the trunk hangs from. The hull is drawn around it "
            "from the offsets above (negative 'forward' = aft, negative "
            "'starboard' = port); defaults put the sheave at the aft end "
            "of a 127 m hull.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#777; font-size: small;")
        lay.addRow(note)
        return box

    def _section_advanced(self):
        box, lay = self._collapsible("Advanced (solver)", "advanced")
        self.adv_ds = self._dspin("target_ds_m", 0.5, 50.0, 5.0, 0.5, 1, " m")
        self.adv_ds.setToolTip(
            "Target element length for the full-solver verification mesh. "
            "Smaller = finer touchdown resolution but slower verifies.")
        self.adv_tol = self._dspin("dr_tol", 0.0001, 0.05, 0.002, 0.0005, 4)
        self.adv_tol.setToolTip(
            "Relative force residual at which the full solver's relaxation "
            "is accepted (verification runs only).")
        self.adv_rho = self._dspin("rho_water", 950.0, 1100.0, 1025.0, 5.0, 0, " kg/m3")
        self.adv_rho.setToolTip("Water density (1025 seawater, ~1000 fresh).")
        lay.addRow("Mesh target element length", self.adv_ds)
        lay.addRow("Convergence tolerance", self.adv_tol)
        lay.addRow("Water density", self.adv_rho)
        return box

    def _section_display(self):
        box, lay = self._collapsible("Display", "display")
        self.zex = self._dspin("z_exaggeration", 0.1, 200.0, 1.0, 0.5, 1, " x")
        self.zex.setToolTip(
            "Vertical exaggeration of the 3D view only. The Profile and "
            "Plan tabs always plot true scale.")
        self.zex.valueChanged.connect(lambda v: self.view3d.set_z_exaggeration(float(v)))
        self.color_mode = self._combo("color_mode", [
            ("segment", "Color by segment"), ("tension", "Color by tension")])
        self.color_mode.currentIndexChanged.connect(
            lambda _i: self.view3d.set_cable_color_mode(self.color_mode.currentData()))
        lay.addRow("Depth exaggeration", self.zex)
        lay.addRow("Cable colors", self.color_mode)
        self.profile_true_scale = self._check(
            "profile_true_scale", "True-scale profile (1:1 horizontal/vertical)", True)
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

    def _on_profile_cell_changed(self, *_a):
        self._validate_profile_table()
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

    # ------------------------------------------------------------- config

    def _bathy_cfg(self) -> dict:
        from .scene import compass_to_math_deg

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
                return {"kind": "profile", "points": pts,
                        "azimuth_deg": compass_to_math_deg(float(self.lay_az.value())) + 180.0}
            return {"kind": "flat", "depth_m": depth}
        if mode == "grid" and self._grid_bathy is not None:
            return self._grid_bathy
        return {"kind": "flat", "depth_m": depth}

    def build_config(self, quality: str = "quick") -> V3Config:
        cfg = V3Config()
        cfg.mode = "operation"
        cfg.scenario = "bu_deployment"
        cfg.bathymetry = self._bathy_cfg()
        cfg.current_layers = []                  # quick model: no drag
        cfg.assembly = []                        # lines come from the integration
        cfg.default_q_water_npm = float(self.def_qw.value())
        cfg.default_mu = float(self.def_mu.value())
        cfg.default_mbr_m = float(self.def_mbr.value())
        cfg.chute_height_m = float(self.chute_h.value())
        cfg.lay_azimuth_deg = float(self.lay_az.value())
        cfg.ship_length_m = float(self.ship_len.value())
        cfg.ship_beam_m = float(self.ship_beam.value())
        cfg.crp_fwd_m = float(self.crp_fwd.value())
        cfg.crp_stbd_m = float(self.crp_stbd.value())
        cfg.chute_fwd_m = float(self.sheave_fwd.value())
        cfg.chute_stbd_m = float(self.sheave_stbd.value())
        cfg.chute_radius_m = float(self.sheave_radius.value())
        cfg.trunk_slack_pct = float(self.trunk_slack.value())
        cfg.target_ds_m = float(self.adv_ds.value())
        cfg.dr_tol = float(self.adv_tol.value())
        cfg.rho_water = float(self.adv_rho.value())
        L1 = float(self.bu_leg_len.value())
        L2 = float(self.bu_leg2_len.value()) or L1
        cfg.op = {
            "bu_weight_kN": float(self.bu_weight.value()),
            "bu_cda_m2": 0.0,                    # no drag in this tool
            "leg_length_m": L1,
            "leg_lengths_m": [L1, L2],
            "leg1_azimuth_deg": float(self.bu_leg1_az.value()),
            "leg2_azimuth_deg": float(self.bu_leg2_az.value()),
            "leg_far_ends_xy": self._bu_far_ends_cfg(),
            "payout_mps": float(self.bu_payout.value()) * KMH,
            "ship_speed_mps": float(self.bu_ship_speed.value()) * KMH,
            "bu_start_depth_m": (float(self.bu_start_depth.value())
                                 if self.bu_start_depth.value() > 0 else None),
            "duration_s": (float(self.bu_duration.value())
                           if self.bu_duration.value() > 0 else None),
            "bottom_tension_target_kN": float(self.bu_btt.value()),
            "leg_bottom_tension_kN": float(self.bu_leg_btt.value()),
            "plan_from_tension": bool(self.bu_plan_check.isChecked()),
            "integration": self.integration_editor.to_dict(
                bu_weight_kN=float(self.bu_weight.value()),
                bu_cda_m2=0.0),
            "quality": quality,
        }
        return cfg

    # ------------------------------------------------------------- solving

    def _schedule(self, *args):
        if self._initializing:
            return
        if self.sender() in (getattr(self, "zex", None),
                             getattr(self, "color_mode", None),
                             getattr(self, "show_on_map", None),
                             getattr(self, "profile_true_scale", None)):
            return  # display-only options don't invalidate the run
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool):
        self._dirty = bool(dirty)
        if dirty:
            self.dirty_label.setText("Inputs changed — click Run to update.")
            self.run_btn.setStyleSheet("font-weight: bold;")
        else:
            self.dirty_label.setText("")
            self.run_btn.setStyleSheet("")

    def _run_quick(self):
        self._run_with_quality("quick")

    def _run_full(self):
        self._run_with_quality("full")

    def _run_with_quality(self, quality: str):
        if self._worker is not None and self._worker.isRunning():
            return
        probs = self.integration_editor.problems()
        if probs:
            QMessageBox.warning(
                self, "BU make-up",
                "Fix the BU make-up first:\n\n" + "\n".join(probs[:6]))
            return
        self.op_progress.setValue(0)
        self._start_worker(self.build_config(quality))

    def _start_worker(self, cfg: V3Config):
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
        self.verify_btn.setEnabled(False)
        self.op_progress.setRange(0, 100)
        self.op_progress.setValue(0)
        self.results.setHtml("<i>Running...</i>")
        self._worker.start()

    def _cancel_worker(self):
        if self._worker is not None:
            self._worker.cancel()
            self.dirty_label.setText("Cancelling...")

    def _on_progress(self, frac: float, label: str):
        if frac < 0:
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
        self.verify_btn.setEnabled(True)
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
            return
        self._scene_origin = self._solve_origin

        try:
            from ..engine.bathymetry import bathymetry_from_dict

            bathy = bathymetry_from_dict(self._bathy_cfg())
            self.profile_view.set_bathy_lookup(bathy.depth_at)
        except Exception:
            self.profile_view.set_bathy_lookup(None)

        if out.snapshots:
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
            push_chains_to_map("BU lowering result", chains, origin, crs)
            markers = []
            if out.snapshots:
                last = out.snapshots[-1]
                for name, xyz in (last.junction_xyz or {}).items():
                    markers.append((f"{name} final position", tuple(xyz)))
            for m in out.scene.markers:
                if m.label:
                    markers.append((m.label, tuple(m.xyz)))
            if markers:
                push_markers_to_map("BU lowering points", markers, origin, crs)
            QMessageBox.information(self, "Send to map", "Memory layer(s) added to the project.")
        except Exception as exc:
            QMessageBox.warning(self, "Send to map", f"Export failed:\n{exc}")

    def _origin_for_map(self) -> Tuple[Tuple[float, float], str]:
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
            self.iface.messageBar().pushMessage("BU lowering", text, duration=6)
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

    def _update_origin_label(self):
        if self._grid_origin:
            crs = self._grid_origin["crs_authid"]
            self.origin_label.setText(
                f"Origin CRS: {crs} — snapped to the raster sample centre.")
        elif self._origin_set:
            self.origin_label.setText(
                "Origin set (project CRS). Edit above or re-pick from the map.")
        else:
            self.origin_label.setText(
                "Origin not set — enter coordinates above or pick from the "
                "map; until then exports use the visible canvas centre.")

    def _set_local_origin(self, xy: Tuple[float, float], update_boxes: bool = True):
        self._picked_centre = (float(xy[0]), float(xy[1]))
        self._origin_set = True
        if update_boxes:
            self._set_origin_boxes(xy)
        if self.bathy_mode.currentData() == "grid" and self.raster_combo.currentData():
            self._sample_raster()
        else:
            self._grid_origin = None
            self._schedule()
        self._update_origin_label()

    def _set_origin_boxes(self, xy: Tuple[float, float]):
        for w, v in ((self.origin_x, xy[0]), (self.origin_y, xy[1])):
            w.blockSignals(True)
            w.setValue(float(v))
            w.blockSignals(False)

    def _origin_boxes_changed(self):
        if self._initializing:
            return
        self._set_local_origin(
            (float(self.origin_x.value()), float(self.origin_y.value())),
            update_boxes=False)

    def _pick_position_only(self):
        def apply(pts):
            self._set_local_origin(pts[0])

        self._start_pick(1, apply, prompts=("position (local origin)",))

    def _picks_to_local(self, pts):
        try:
            from .qgis_adapters import map_points_to_local

            _origin, crs = self._origin_for_map()
            return map_points_to_local(pts, pts[0], crs)
        except Exception:
            x0, y0 = pts[0]
            return [(x - x0, y - y0) for x, y in pts]

    def _pick_bu_setup(self):
        """Map-first set-up: origin, heading, then the two LAID ENDS."""
        def apply(pts):
            from .map_tools import bearing_deg

            self._set_local_origin(pts[0])
            loc = self._picks_to_local(pts)
            self.lay_az.setValue(bearing_deg(loc[0], loc[1]))
            self._set_bu_far_ends(loc[2], loc[3])
            # Seed each leg length from the picked geometry when the entered
            # length could not reach the laid end anyway.
            for spin, end, other in (
                    (self.bu_leg_len, loc[2], None),
                    (self.bu_leg2_len, loc[3], self.bu_leg_len)):
                dist = math.hypot(end[0], end[1])
                cur = float(spin.value())
                if cur <= 0.0 and other is not None:
                    cur = float(other.value())
                if cur < dist * 1.02:
                    spin.setValue(round(dist * 1.08))

        self._start_pick(4, apply, prompts=(
            "BU setup position (local origin)",
            "a point in the steaming direction",
            "leg 1 laid end (far end of the pre-laid leg)",
            "leg 2 laid end",
        ))

    def _set_bu_far_ends(self, end1, end2):
        self._syncing_bu = True
        try:
            for spin, v in ((self.bu_end1_x, end1[0]), (self.bu_end1_y, end1[1]),
                            (self.bu_end2_x, end2[0]), (self.bu_end2_y, end2[1])):
                spin.setValue(float(v))
        finally:
            self._syncing_bu = False
        self._sync_bu_bearings_from_ends()
        self._schedule()

    def _sync_bu_bearings_from_ends(self):
        from .map_tools import bearing_deg

        self._syncing_bu = True
        try:
            for (ex, ey), az in (((self.bu_end1_x, self.bu_end1_y), self.bu_leg1_az),
                                 ((self.bu_end2_x, self.bu_end2_y), self.bu_leg2_az)):
                end = (float(ex.value()), float(ey.value()))
                if end != (0.0, 0.0):
                    az.setValue(bearing_deg((0.0, 0.0), end))
        finally:
            self._syncing_bu = False

    def _bu_end_edited(self, *_a):
        if self._syncing_bu or self._initializing:
            return
        self._sync_bu_bearings_from_ends()

    def _bu_az_edited(self, *_a):
        """A bearing was typed by hand: bearings govern again, so clear the
        stored laid ends (they no longer match)."""
        if self._syncing_bu or self._initializing:
            return
        self._syncing_bu = True
        try:
            for spin in (self.bu_end1_x, self.bu_end1_y,
                         self.bu_end2_x, self.bu_end2_y):
                spin.setValue(0.0)
        finally:
            self._syncing_bu = False

    def _bu_far_ends_cfg(self):
        e1 = (float(self.bu_end1_x.value()), float(self.bu_end1_y.value()))
        e2 = (float(self.bu_end2_x.value()), float(self.bu_end2_y.value()))
        if e1 == (0.0, 0.0) and e2 == (0.0, 0.0):
            return None
        return [list(e1), list(e2)]

    def _refresh_map_overlay(self, *_a):
        canvas = self._map_canvas()
        if canvas is None:
            return
        want = bool(getattr(self, "show_on_map", None) and self.show_on_map.isChecked())
        scene = self._last_scene
        if not want or scene is None:
            if self._map_overlay is not None:
                self._map_overlay.clear()
            return
        try:
            from .map_tools import SimulatorMapOverlay

            if self._map_overlay is None:
                self._map_overlay = SimulatorMapOverlay(canvas)
            origin, crs = (self._scene_origin if self._scene_origin
                           else self._origin_for_map())
            self._map_overlay.update(scene, origin, crs)
        except Exception:
            pass  # overlay is best-effort; never break the run flow

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
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", "bu_lowering.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            from . import exporters

            header, rows = exporters.timeline_csv_rows(out.snapshots or [])
            exporters.write_csv(path, header, rows)
        except Exception as exc:
            QMessageBox.warning(self, "Export CSV", f"Export failed:\n{exc}")

    def _export_schedule_csv(self):
        out = self._last_out
        if out is None or not out.snapshots:
            QMessageBox.information(
                self, "Export ops schedule",
                "Run the lowering first — the schedule sheet is built from "
                "its timeline.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export ops schedule", "bu_lowering_schedule.csv", "CSV (*.csv)")
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
        path, _ = QFileDialog.getSaveFileName(
            self, "Export DXF", "bu_lowering.dxf", "DXF (*.dxf)")
        if not path:
            return
        try:
            from . import exporters

            exporters.scene_to_dxf_3d(out.scene, path)
        except Exception as exc:
            QMessageBox.warning(self, "Export DXF", f"Export failed:\n{exc}")

    # ------------------------------------------------------------ settings

    def _capture_defaults(self):
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
            "Reset every input, table and saved value of the BU Lowering "
            "Tool to its factory default?")
        if btn != getattr(QMessageBox, "StandardButton", QMessageBox).Yes:
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
            self.integration_editor.set_from_dict({})
            self._grid_bathy = None
            self._grid_origin = None
            self._picked_centre = None
            self._origin_set = False
            self.raster_status.setText("No grid sampled.")
            self.settings.clear()
        finally:
            self._initializing = False
        self._on_bathy_mode()
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
        raw = self.settings.value("bu_integration_json")
        if raw:
            self.integration_editor.set_from_json(str(raw))
        for key, (btn, body) in self._collapsibles.items():
            val = self.settings.value(f"section_{key}")
            if val is not None:
                expanded = str(val) == "1"
                btn.setChecked(expanded)
                body.setVisible(expanded)
        if str(self.settings.value("origin_set")) in ("1", "true", "True"):
            self._origin_set = True
            self._picked_centre = (float(self.origin_x.value()),
                                   float(self.origin_y.value()))
        self._update_origin_label()
        self._on_bathy_mode()
        try:
            self.view3d.set_z_exaggeration(float(self.zex.value()))
            self.view3d.set_cable_color_mode(self.color_mode.currentData())
        except Exception:
            pass

    def _save_settings(self):
        for key, w in self._registry:
            try:
                if isinstance(w, (QDoubleSpinBox, QSpinBox)):
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
        pts = []
        for r in range(self.profile_table.rowCount()):
            d = _of(self.profile_table.item(r, 0))
            z = _of(self.profile_table.item(r, 1))
            if d is not None and z is not None:
                pts.append([d, z])
        self.settings.setValue("profile_json", json.dumps(pts))
        try:
            self.settings.setValue("bu_integration_json",
                                   self.integration_editor.to_json())
        except Exception:
            pass

    def closeEvent(self, event):  # noqa: N802 - Qt API
        self._save_settings()
        self._cancel_worker()
        self._cleanup_map_artifacts()
        super().closeEvent(event)
