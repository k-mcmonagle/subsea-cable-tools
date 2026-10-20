# -*- coding: utf-8 -*-
"""Steady Lay (beta): the cable shape while laying at a constant ship speed.

A tab of Catenary Calculator V2. For a uniform cable laid at a steady ship
speed (pay-out equal to the ship speed), it shows the cable angle at the
chute, the layback, the tensions and the suspended length, with current
drag, solved by :mod:`steady_lay` (Zajac 1957's stationary model). A sweep
plots the cable angle against ship speed, and Zajac's closed forms are shown
next to the numerical answer as a check.

Inputs persist through the dialog's QSettings under ``steady_lay/``; the
dialog calls :meth:`SteadyLayTab.save_settings` / :meth:`restore_settings`.
"""

from __future__ import annotations

from dataclasses import replace
import math
from html import escape
from typing import Dict, List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QScrollArea, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ..plugin_log import log_exception
from ..qgis_compat import PEN_STYLE_DASH
from . import hydrodynamics as hyd
from . import steady_lay as sl

KNOT = hyd.KNOT
G = hyd.G
_ORIENTATION = getattr(Qt, "Orientation", Qt)
CUSTOM_CABLE = "Custom (values below)"


def _spin(lo: float, hi: float, value: float, decimals: int, step: float,
          suffix: str = "", tip: str = "", special: str = "") -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(lo, hi)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    if special:
        box.setSpecialValueText(special)   # shown at the minimum
    if tip:
        box.setToolTip(tip)
    return box


def _fmt_m(value: float) -> str:
    return f"{value:,.0f} m" if abs(value) >= 100 else f"{value:.1f} m"


def _fmt_duration(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "—"
    minutes = seconds / 60.0
    if minutes < 1.0:
        return f"{seconds:.0f} s"
    if minutes < 90.0:
        return f"{minutes:.0f} min"
    return f"{int(minutes // 60)} h {int(round(minutes % 60)):02d} min"


class SteadyLayTab(QWidget):
    """The Steady Lay (beta) tab."""

    SETTINGS_PREFIX = "steady_lay/"
    # Integration step as a fraction of the water depth: the single solve and
    # the (drawn-only) speed sweep. Both keep the angle within ~0.05 deg of
    # the engine's fine default on telecom and armoured cases, 3-6x faster.
    _DS_DIV_SOLVE = 300.0
    _DS_DIV_SWEEP = 150.0
    _SWEEP_POINTS = 15
    # Speed-axis ranges (kn): the smallest that covers 1.25x the ship speed,
    # so small speed edits keep the same sweep (and its cache).
    _SWEEP_RANGES_KN = (2.0, 4.0, 8.0, 12.0, 16.0, 20.0, 25.0)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._library_rows: List[Dict] = []
        self._filling = False
        self._dirty = True              # inputs changed since the last solve
        self._sweep_key: Optional[Tuple] = None
        self._sweep: Optional[sl.SpeedSweep] = None
        self._result: Optional[sl.SteadyLayResult] = None
        self._profile: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.solve)
        self._build_ui()
        self._load_library()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(getattr(_ORIENTATION, "Horizontal"))
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter)

        inputs = QWidget()
        form_box = QVBoxLayout(inputs)
        intro = QLabel(
            "<b>Beta.</b> The cable while laying at a steady ship speed, pay-out "
            "equal to the ship speed (Zajac 1957). Uniform cable, with drag from "
            "the ship's speed and any current. See the catenary model notes.")
        intro.setWordWrap(True)
        form_box.addWidget(intro)

        cable = QGroupBox("Cable")
        cf = QFormLayout(cable)
        self.cable_type = QComboBox()
        self.cable_type.setToolTip(
            "Fill the weights and diameter from your Cable Type Library "
            "(Subsea Cable Tools ▸ Cable Type Library…).")
        self.w_water = _spin(0.0, 1000.0, 1.5, 3, 0.1, " kg/m",
                             "Submerged weight per metre (kg/m in water).")
        self.w_air = _spin(0.0, 1000.0, 0.0, 3, 0.1, " kg/m",
                           "Weight per metre in air: used above the waterline and as the "
                           "cable's mass. At 0 it is estimated from the weight in water "
                           "and the diameter (submerged weight + displaced water).",
                           special="from diameter")
        self.diameter = _spin(0.0, 1000.0, 40.0, 1, 1.0, " mm",
                              "Outer diameter. The drag on the cable scales with it; at 0 "
                              "there is no drag and the answer is the static catenary.")
        self.cd_normal = _spin(0.0, 5.0, 1.2, 2, 0.05, "",
                               "Normal (cross-flow) drag coefficient. Measured values: about "
                               "1.1 for a smooth polyethylene sheath, 1.5 for a rough served "
                               "or armoured cable (Zajac 1957). Supplier data governs.")
        self.cd_tangential = _spin(0.0, 0.5, 0.01, 3, 0.005, "",
                                   "Tangential (skin-friction) drag coefficient: small, and "
                                   "mainly affects the tension at high speed.")
        cf.addRow("Cable type:", self.cable_type)
        cf.addRow("Weight in water:", self.w_water)
        cf.addRow("Weight in air:", self.w_air)
        cf.addRow("Outer diameter:", self.diameter)
        cf.addRow("Drag coefficient, normal:", self.cd_normal)
        cf.addRow("Drag coefficient, tangential:", self.cd_tangential)
        form_box.addWidget(cable)

        lay = QGroupBox("Lay")
        lf = QFormLayout(lay)
        self.depth = _spin(1.0, 12000.0, 1000.0, 1, 10.0, " m", "Water depth at the touchdown.")
        self.speed = _spin(0.0, 20.0, 4.0, 2, 0.1, " kn",
                           "Ship speed over the ground. Pay-out equals the ship speed.")
        self.bottom_tension = _spin(0.0, 10000.0, 0.0, 2, 0.5, " kN",
                                    "Tension where the cable meets the seabed. 0 is a slack lay: "
                                    "the cable runs straight down at the critical angle and any "
                                    "slack is laid on the seabed.",
                                    special="0 (slack lay)")
        self.chute_height = _spin(0.0, 100.0, 5.0, 1, 0.5, " m",
                                  "Height of the chute/sheave above the waterline. The angle and "
                                  "tension at the chute include the cable in air below it.")
        self.rho_water = _spin(990.0, 1050.0, hyd.RHO_SEAWATER, 1, 1.0, " kg/m³", "Sea water density.")
        lf.addRow("Water depth:", self.depth)
        lf.addRow("Ship speed:", self.speed)
        lf.addRow("Bottom tension:", self.bottom_tension)
        lf.addRow("Chute height above waterline:", self.chute_height)
        lf.addRow("Water density:", self.rho_water)
        form_box.addWidget(lay)

        self.current_box = QGroupBox("Current")
        self.current_box.setCheckable(True)
        self.current_box.setChecked(False)
        self.current_box.setToolTip("Include a current (uniform from the surface down).")
        cuf = QFormLayout(self.current_box)
        self.current_speed = _spin(0.0, 10.0, 0.5, 2, 0.1, " kn", "Current speed.")
        self.current_dir = _spin(0.0, 359.9, 90.0, 1, 15.0, "°",
                                 "Direction the current flows towards, relative to the lay "
                                 "course: 0° with the ship, 90° to starboard, 180° against the "
                                 "ship, 270° to port.")
        self.current_depth = _spin(0.0, 12000.0, 0.0, 0, 50.0, " m",
                                   "The current runs from the surface down to this depth (no "
                                   "current below). At 0 it reaches the seabed.",
                                   special="seabed")
        cuf.addRow("Speed:", self.current_speed)
        cuf.addRow("Towards (relative to course):", self.current_dir)
        cuf.addRow("From the surface down to:", self.current_depth)
        form_box.addWidget(self.current_box)
        form_box.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inputs)
        scroll.setMinimumWidth(360)
        splitter.addWidget(scroll)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 0, 0, 0)
        out_split = QSplitter(getattr(_ORIENTATION, "Vertical"))
        out_split.setChildrenCollapsible(False)
        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setMinimumHeight(80)
        out_split.addWidget(self.results)

        self.plot_tabs = QTabWidget()
        self.profile_plot = pg.PlotWidget()
        self.speed_plot = pg.PlotWidget()
        for plot in (self.profile_plot, self.speed_plot):
            plot.setBackground("w")
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setMenuEnabled(False)
        self.profile_plot.getViewBox().invertY(True)
        self.profile_plot.setLabel("bottom", "Horizontal distance astern of the chute (m)")
        self.profile_plot.setLabel("left", "Depth (m)")
        self.speed_plot.setLabel("bottom", "Ship speed (kn)")
        self.speed_plot.setLabel("left", "Cable angle at the chute (° from horizontal)")
        # Bottom-left: empty in every case (the angle is high at low speed).
        self.speed_plot.addLegend(offset=(10, -10))
        self.plot_tabs.addTab(self._with_readout(self.profile_plot, "profile"), "Profile")
        self.plot_tabs.addTab(self._with_readout(self.speed_plot, "speed"), "Angle vs speed")
        out_split.addWidget(self.plot_tabs)
        out_split.setStretchFactor(0, 0)
        out_split.setStretchFactor(1, 1)
        out_split.setSizes([300, 400])
        rv.addWidget(out_split)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([450, 750])

        for box in (self.w_water, self.w_air, self.diameter, self.cd_normal, self.cd_tangential,
                    self.depth, self.speed, self.bottom_tension, self.chute_height,
                    self.rho_water, self.current_speed, self.current_dir, self.current_depth):
            box.valueChanged.connect(self._on_input_changed)
        self.current_box.toggled.connect(self._on_input_changed)
        self.cable_type.currentIndexChanged.connect(self._on_cable_type_changed)

    def _with_readout(self, plot: "pg.PlotWidget", which: str) -> QWidget:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(plot)
        label = QLabel(" ")
        label.setStyleSheet("color: #555;")
        layout.addWidget(label)
        if which == "profile":
            self.profile_readout = label
            plot.scene().sigMouseMoved.connect(self._on_profile_mouse)
        else:
            self.speed_readout = label
            plot.scene().sigMouseMoved.connect(self._on_speed_mouse)
        return holder

    # ------------------------------------------------------- cable library

    def _load_library(self) -> None:
        """(Re)list the Cable Type Library's cables, keeping the selection."""
        try:
            from ..cable_library import store as library
            rows = [r for r in library.load_current()
                    if (r.get("category") or "cable") == "cable" and r.get("name")]
        except Exception:
            log_exception("Steady Lay: reading the cable type library failed")
            rows = []
        current = self.cable_type.currentText()
        self._library_rows = rows
        self.cable_type.blockSignals(True)
        self.cable_type.clear()
        self.cable_type.addItem(CUSTOM_CABLE)
        for row in rows:
            self.cable_type.addItem(str(row["name"]))
        index = self.cable_type.findText(current)
        self.cable_type.setCurrentIndex(max(0, index))
        self.cable_type.blockSignals(False)
        self.cable_type.setEnabled(bool(rows))
        if not rows:
            self.cable_type.setToolTip("No Cable Type Library is open (Subsea Cable Tools ▸ "
                                       "Cable Type Library…); type the cable's values below.")

    def _on_cable_type_changed(self, index: int) -> None:
        if index <= 0 or index > len(self._library_rows):
            return
        row = self._library_rows[index - 1]

        def num(key: str) -> Optional[float]:
            try:
                value = float(row.get(key))
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) else None

        self._filling = True
        try:
            for key, box in (("weight_water_kg_m", self.w_water),
                             ("weight_air_kg_m", self.w_air),
                             ("diameter_mm", self.diameter)):
                value = num(key)
                box.setValue(value if value is not None else box.minimum())
        finally:
            self._filling = False
        self.schedule_solve()

    def showEvent(self, event):  # noqa: N802 - Qt API
        self._load_library()
        super().showEvent(event)
        if self._dirty:                 # changed while hidden: solve now
            self._timer.start(0)

    # ------------------------------------------------------------- solving

    def _on_input_changed(self, *_args) -> None:
        if not self._filling and self.cable_type.currentIndex() > 0 and \
                self.sender() in (self.w_water, self.w_air, self.diameter):
            self.cable_type.blockSignals(True)
            self.cable_type.setCurrentIndex(0)   # typed values: no longer the library type
            self.cable_type.blockSignals(False)
        self.schedule_solve()

    def schedule_solve(self) -> None:
        """Solve shortly (debounced) - or when the tab is next shown."""
        self._dirty = True
        if self.isVisible():
            self._timer.start(200)

    def stop(self) -> None:
        """Stop a pending solve (dialog shutdown)."""
        self._timer.stop()

    def _current_profile(self) -> Optional[hyd.CurrentProfile]:
        if not self.current_box.isChecked() or self.current_speed.value() <= 0:
            return None
        speed = self.current_speed.value() * KNOT
        # Engine frame: the ship steams along +x, +y is to port. "Towards
        # 90 deg (starboard)" is therefore -90 deg in the engine's CCW angle.
        direction = -self.current_dir.value()
        bottom = self.current_depth.value()
        if bottom <= 0 or bottom >= self.depth.value():
            return hyd.CurrentProfile.uniform(speed, direction)
        return hyd.CurrentProfile([
            hyd.CurrentLayer(0.0, speed, direction),
            hyd.CurrentLayer(bottom, speed, direction),
            hyd.CurrentLayer(bottom + 0.01, 0.0, direction),
        ])

    def build_input(self) -> sl.SteadyLayInput:
        """The engine input for the current widget values."""
        q_water = self.w_water.value() * G
        d = self.diameter.value() / 1000.0
        rho = self.rho_water.value()
        q_air = self.w_air.value() * G if self.w_air.value() > 0 else sl.air_weight_npm(q_water, d, rho)
        depth = self.depth.value()
        return sl.SteadyLayInput(
            depth_m=depth,
            q_water_npm=q_water,
            q_air_npm=q_air,
            diameter_m=d,
            cd_normal=self.cd_normal.value(),
            cd_tangential=self.cd_tangential.value(),
            rho_c_kgpm=q_air / G,
            ship_speed_mps=self.speed.value() * KNOT,
            current=self._current_profile(),
            chute_height_m=self.chute_height.value(),
            rho_water=rho,
            ds_m=max(0.05, min(20.0, depth / self._DS_DIV_SOLVE)),
            T0_N=self.bottom_tension.value() * 1000.0,
        )

    def solve(self) -> None:
        """Solve the current inputs and refresh the results and plots."""
        self._dirty = False
        inp = self.build_input()
        problems = self._input_problems(inp)
        if problems:
            self._result = None
            self._profile = None
            self.results.setHtml("<b>Check the inputs</b><br>" + "<br>".join(
                f"• {escape(p)}" for p in problems))
            self.profile_plot.clear()
            self.speed_plot.clear()
            return
        try:
            res = sl.integrate_steady_lay(inp)
            sweep = self._speed_sweep(inp)
        except Exception as exc:
            log_exception("Steady Lay: the solve failed")
            self._result = None
            self.results.setHtml(f"<b>The solve failed</b><br>{escape(str(exc))}")
            return
        self._result = res
        self.results.setHtml(self._results_html(inp, res))
        self._draw_profile(inp, res)
        self._draw_speed(inp, res, sweep)

    @staticmethod
    def _input_problems(inp: sl.SteadyLayInput) -> List[str]:
        problems = []
        if inp.q_water_npm <= 0:
            problems.append("The weight in water must be positive: a neutral or buoyant "
                            "cable does not reach the seabed.")
        if inp.depth_m <= 0:
            problems.append("The water depth must be positive.")
        return problems

    def _speed_sweep(self, inp: sl.SteadyLayInput) -> sl.SpeedSweep:
        """The angle-vs-speed sweep, recomputed only when an input other than
        the ship speed changes (it does not depend on the ship speed)."""
        need_kn = 1.25 * inp.ship_speed_mps / KNOT
        v_max = next((r for r in self._SWEEP_RANGES_KN if r >= need_kn),
                     self._SWEEP_RANGES_KN[-1]) * KNOT
        key = (inp.depth_m, inp.q_water_npm, inp.q_air_npm, inp.diameter_m, inp.cd_normal,
               inp.cd_tangential, inp.chute_height_m, inp.rho_water, inp.T0_N,
               None if inp.current is None else repr(inp.current.to_dict()), round(v_max, 6))
        if key != self._sweep_key or self._sweep is None:
            coarse = replace(inp, ds_m=max(0.05, min(40.0, inp.depth_m / self._DS_DIV_SWEEP)))
            speeds = np.linspace(0.0, v_max, self._SWEEP_POINTS)
            speeds[0] = 0.02 * KNOT
            self._sweep = sl.speed_sweep(coarse, speeds)
            self._sweep_key = key
        return self._sweep

    # ------------------------------------------------------------- results

    def _results_html(self, inp: sl.SteadyLayInput, res: sl.SteadyLayResult) -> str:
        V = inp.ship_speed_mps
        h = inp.depth_m
        H_c = res.hydrodynamic_constant_mps
        alpha = res.critical_angle_deg
        angle = res.exit_angle_deg
        rows_ship = [
            ("Cable angle", f"<b>{angle:.1f}°</b> from horizontal ({90.0 - angle:.1f}° from vertical)"),
        ]
        water_angle = self._waterline_angle_deg(res)
        if inp.chute_height_m > 0 and water_angle is not None:
            rows_ship.append(("Cable angle at the waterline",
                              f"{water_angle:.1f}° (no drag in air: the cable sags towards the chute)"))
        rows_ship += [
            ("Tension", f"{res.top_tension_N / 1000.0:.2f} kN"),
            ("Layback (chute to touchdown)", _fmt_m(res.layback_m)),
            ("Suspended length", f"{_fmt_m(res.suspended_length_m)} ({res.suspended_length_m / h:.2f} × depth)"),
            ("Time from chute to touchdown",
             _fmt_duration(res.suspended_length_m / V) if V > 0 else "—"),
        ]
        rows_bed = [("Bottom tension", f"{res.T0_N / 1000.0:.2f} kN" if inp.T0_N > 0
                     else "0 kN (slack lay: the cable lands at the critical angle)")]
        if inp.T0_N > 0 and math.isfinite(res.min_radius_m):
            rows_bed.append(("Smallest bend radius (suspended cable)",
                             f"{res.min_radius_m:,.1f} m, {res.min_radius_s_m:,.0f} m from the touchdown"))
        if inp.current is not None:
            offset = -res.lateral_offset_m      # touchdown relative to the chute, +y = port
            side = "port" if offset > 0 else "starboard"
            rows_bed.append(("Touchdown offset (current)",
                             f"{abs(offset):,.1f} m to {side} of the ship's track"))
        quick = []
        if V > 0 and H_c > 0:
            straight = h / math.tan(math.radians(alpha)) if alpha > 0 else float("inf")
            per_degree = hyd.payout_increment_mps(H_c, math.radians(1.0)) / V * 100.0
            quick = [
                ("Hydrodynamic constant H", f"{H_c:.3f} m/s (the cable's sinking speed broadside)"),
                ("Critical angle (zero bottom tension)", f"{alpha:.1f}°"),
                ("Straight-line layback, h / tan α", _fmt_m(straight)),
                ("Top tension, T₀ + w·h (no tangential drag)",
                 f"{hyd.top_tension_theorem_N(res.T0_N, inp.q_water_npm, h) / 1000.0:.2f} kN at the waterline"),
                ("Extra slack to keep zero bottom tension downslope", f"{per_degree:.2f} % per degree of slope"),
                ("Upslopes that make free spans at this speed",
                 f"steeper than about {math.degrees(H_c / V):.1f}°"),
            ]
        warnings = list(res.warnings)
        if inp.diameter_m <= 0:
            warnings.append("Diameter 0: no drag, so this is the static catenary.")
        if V > 0 and angle < 5.0:
            warnings.append(f"A very shallow angle: {res.suspended_length_m / h:.0f}× the depth is "
                            "suspended. Check the drag coefficient against your lay records.")
        sections = [("Cable at the chute", rows_ship), ("At the seabed", rows_bed)]
        if quick:
            sections.append(("Closed-form checks (Zajac 1957; no current)", quick))
        html = [self._table(sections)]
        if warnings:
            html.append("<b>Warnings</b><br>" + "<br>".join(f"• {escape(w)}" for w in warnings))
        html.append("<span style='color:#777'>Steady state: constant speed, pay-out equal to the "
                    "ship speed, a uniform cable and a flat seabed at the touchdown. Not validated "
                    "against field data: planning estimates only.</span>")
        return "".join(html)

    @staticmethod
    def _waterline_angle_deg(res: sl.SteadyLayResult) -> Optional[float]:
        """Cable angle (from horizontal) where it crosses the sea surface."""
        z = res.xyz[:, 2]
        above = np.nonzero(z >= 0.0)[0]
        if len(above) == 0 or above[0] == 0:
            return None
        i = int(above[0])
        d = res.xyz[i] - res.xyz[i - 1]
        return math.degrees(math.atan2(d[2], math.hypot(d[0], d[1])))

    @staticmethod
    def _table(sections: List[Tuple[str, List[Tuple[str, str]]]]) -> str:
        """One two-column table (so the values align), a bold row per section."""
        cells = []
        for title, rows in sections:
            cells.append(f"<tr><td colspan='2' style='padding-top:4px'><b>{escape(title)}</b></td></tr>")
            cells += [f"<tr><td style='padding-right:14px'>{escape(k)}</td><td>{v}</td></tr>"
                      for k, v in rows]
        return f"<table cellspacing='0' cellpadding='1'>{''.join(cells)}</table><br>"

    # --------------------------------------------------------------- plots

    def _draw_profile(self, inp: sl.SteadyLayInput, res: sl.SteadyLayResult) -> None:
        plot = self.profile_plot
        plot.clear()
        xyz = res.xyz
        # Distance astern of the chute (the top end) and depth below the surface.
        chute_x = xyz[-1, 0]
        x = chute_x - xyz[:, 0]
        depth = -xyz[:, 2]
        tension = res.tension_N / 1000.0
        self._profile = (x, depth, tension)
        far = max(float(x.max()) * 1.08, 10.0)
        plot.plot([-0.04 * far, far], [0.0, 0.0], pen=pg.mkPen("#4a90d9", width=1))
        plot.plot([-0.04 * far, far], [inp.depth_m, inp.depth_m], pen=pg.mkPen("#8c6d46", width=2))
        alpha = math.radians(res.critical_angle_deg)
        if inp.ship_speed_mps > 0 and 0 < alpha < math.pi / 2:
            # Zajac's straight line through the touchdown, for comparison.
            x_tdp = float(x[0])
            plot.plot([x_tdp, x_tdp - inp.depth_m / math.tan(alpha)], [inp.depth_m, 0.0],
                      pen=pg.mkPen((150, 150, 150), width=1, style=PEN_STYLE_DASH))
        plot.plot(x, depth, pen=pg.mkPen("#1f4e9c", width=2.5))
        plot.plot([float(x[0])], [float(depth[0])], pen=None, symbol="o", symbolSize=8,
                  symbolBrush="#d62728", symbolPen=None)
        plot.plot([float(x[-1])], [float(depth[-1])], pen=None, symbol="s", symbolSize=7,
                  symbolBrush="#333333", symbolPen=None)
        plot.getViewBox().autoRange(padding=0.05)

    def _draw_speed(self, inp: sl.SteadyLayInput, res: sl.SteadyLayResult,
                    sweep: sl.SpeedSweep) -> None:
        plot = self.speed_plot
        plot.clear()
        kn = sweep.speed_mps / KNOT
        label = ("at this bottom tension" if inp.T0_N > 0 else "slack lay")
        plot.plot(kn, sweep.angle_deg, pen=pg.mkPen("#1f4e9c", width=2.5), name=label)
        if inp.T0_N > 0 or inp.current is not None:
            plot.plot(kn, sweep.critical_angle_deg,
                      pen=pg.mkPen((150, 150, 150), width=1.5, style=PEN_STYLE_DASH),
                      name="critical angle (zero bottom tension, no current)")
        plot.plot([inp.ship_speed_mps / KNOT], [res.exit_angle_deg], pen=None, symbol="o",
                  symbolSize=9, symbolBrush="#d62728", symbolPen=None, name="this speed")
        plot.setYRange(0.0, 92.0, padding=0.0)
        plot.setXRange(0.0, float(kn[-1]), padding=0.02)

    # ------------------------------------------------------------ readouts

    def _view_point(self, plot: "pg.PlotWidget", pos) -> Optional[Tuple[float, float]]:
        if not plot.sceneBoundingRect().contains(pos):
            return None
        point = plot.getViewBox().mapSceneToView(pos)
        return float(point.x()), float(point.y())

    def _on_profile_mouse(self, pos) -> None:
        hit = self._view_point(self.profile_plot, pos)
        if hit is None or self._profile is None:
            self.profile_readout.setText(" ")
            return
        x, depth, tension = self._profile
        xs = float(np.clip(hit[0], x.min(), x.max()))
        order = np.argsort(x)
        d = float(np.interp(xs, x[order], depth[order]))
        t = float(np.interp(xs, x[order], tension[order]))
        self.profile_readout.setText(
            f"{xs:,.1f} m astern of the chute · cable at {d:,.1f} m depth · tension {t:.2f} kN")

    def _on_speed_mouse(self, pos) -> None:
        hit = self._view_point(self.speed_plot, pos)
        sweep = self._sweep
        if hit is None or sweep is None:
            self.speed_readout.setText(" ")
            return
        kn = sweep.speed_mps / KNOT
        v = float(np.clip(hit[0], kn.min(), kn.max()))
        a = float(np.interp(v, kn, sweep.angle_deg))
        lb = float(np.interp(v, kn, sweep.layback_m))
        self.speed_readout.setText(f"{v:.2f} kn · cable angle {a:.1f}° · layback {_fmt_m(lb)}")

    # ------------------------------------------------------------ settings

    def _spins(self) -> Dict[str, QDoubleSpinBox]:
        return {
            "w_water": self.w_water, "w_air": self.w_air, "diameter": self.diameter,
            "cd_normal": self.cd_normal, "cd_tangential": self.cd_tangential,
            "depth": self.depth, "speed": self.speed, "bottom_tension": self.bottom_tension,
            "chute_height": self.chute_height, "rho_water": self.rho_water,
            "current_speed": self.current_speed, "current_dir": self.current_dir,
            "current_depth": self.current_depth,
        }

    def save_settings(self, settings) -> None:
        p = self.SETTINGS_PREFIX
        for key, box in self._spins().items():
            settings.setValue(p + key, box.value())
        settings.setValue(p + "current_on", bool(self.current_box.isChecked()))
        settings.setValue(p + "cable_type", self.cable_type.currentText())
        settings.setValue(p + "plot_tab", self.plot_tabs.currentIndex())

    def restore_settings(self, settings) -> None:
        p = self.SETTINGS_PREFIX
        self._filling = True
        try:
            for key, box in self._spins().items():
                value = settings.value(p + key)
                if value is None:
                    continue
                try:
                    box.setValue(float(value))
                except (TypeError, ValueError):
                    pass
            on = settings.value(p + "current_on")
            if on is not None:
                self.current_box.setChecked(str(on).lower() in ("1", "true", "yes"))
            name = settings.value(p + "cable_type")
            if name:
                self.cable_type.blockSignals(True)
                self.cable_type.setCurrentIndex(max(0, self.cable_type.findText(str(name))))
                self.cable_type.blockSignals(False)
            tab = settings.value(p + "plot_tab")
            if tab is not None:
                try:
                    self.plot_tabs.setCurrentIndex(int(tab))
                except (TypeError, ValueError):
                    pass
        finally:
            self._filling = False
        self.schedule_solve()
