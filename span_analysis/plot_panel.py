# -*- coding: utf-8 -*-
"""The Span & Slack Analysis profile: five linked rows on one KP axis.

* status bar: the worst finding per KP (green clear, blue info, amber, red, grey no data);
* profile: sampled seabed, the seabed the cable rests on, the modelled cable
  with spans filled red, span supports (red below the MBR; hover for the
  reaction and bend radius), bodies, and the detail model's cable;
* tension: residual tension on the seabed, the lay (deposit) tension, NPTS and
  the highest tension that keeps spans within the limits;
* slack: cable laid, the fill (seabed length over route length) and the
  cable the rest shape uses, all as % over the route; the surplus is shaded;
* slope and friction: the seabed slope against the friction angle ``atan mu``.

Items are built once and updated with ``setData`` (removing pyqtgraph items
is slow on long routes and noisy under QGIS 3.40). Hover moves a shared
crosshair and reports every value at that KP; the dialog links it to the map.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QPainterPath
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QGraphicsPathItem,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..cable_seabed import planning as pl
from ..maptools.profile_measure_controller import ProfileMeasureController
from ..plugin_log import log_exception

_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
_DOT = getattr(getattr(Qt, "PenStyle", Qt), "DotLine")
LEVEL_COLOURS = {-1: (200, 200, 200), 0: (76, 175, 80), 1: (31, 119, 180), 2: (255, 152, 0), 3: (211, 47, 47)}
SEABED = (121, 85, 72)
CABLE = (25, 70, 160)
SPAN_FILL = (211, 47, 47, 150)
SURPLUS_FILL = (76, 175, 80, 90)
DETAIL = (123, 31, 162)
PREVIOUS = (150, 150, 150)


def spans_path(kp: np.ndarray, top: np.ndarray, bottom: np.ndarray, spans) -> QPainterPath:
    """One path with a closed polygon per span (cable above, seabed below)."""
    xs, ys, connect = [], [], []
    for span in spans:
        sl = slice(span.i0, span.i1 + 1)
        k, t, b = kp[sl], top[sl], bottom[sl]
        ok = np.isfinite(k) & np.isfinite(t) & np.isfinite(b)
        if ok.sum() < 2:
            continue
        k, t, b = k[ok], t[ok], b[ok]
        xs.append(np.concatenate((k, k[::-1])))
        ys.append(np.concatenate((t, b[::-1])))
        link = np.ones(2 * len(k), dtype=bool)
        link[-1] = False
        connect.append(link)
    if not xs:
        return QPainterPath()
    return pg.arrayToQPath(np.concatenate(xs), np.concatenate(ys), connect=np.concatenate(connect))


def _tip(x, y, data):
    return data if isinstance(data, str) else f"KP {x:.3f}"


def _radius_text(radius: float) -> str:
    if not (math.isfinite(radius) or radius == math.inf):
        return "unknown (no EI)"
    return "over 1000 m" if radius > 1000 else f"{radius:.1f} m"


class ProfilePlot(QWidget):
    kpHovered = pyqtSignal(float)
    kpClicked = pyqtSignal(float)
    kpDoubleClicked = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result: Optional[pl.PlanningResult] = None
        self._previous: Optional[pl.PlanningResult] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        fit = QPushButton("Fit")
        fit.setToolTip("Show the whole profile.")
        fit.clicked.connect(self.fit)
        bar.addWidget(fit)
        self.measure = ProfileMeasureController(self, plot_factors=lambda: (0.001, 1.0),
                                                text_units=lambda: ("m", "m"), depth_down=lambda: True)
        self.measure.statusChanged.connect(lambda text: self.readout.setText(text))
        measure_btn = QToolButton()
        measure_btn.setDefaultAction(self.measure.action)
        bar.addWidget(measure_btn)
        bar.addWidget(self.measure.snap_check)
        bar.addWidget(self.measure.source_combo)
        bar.addWidget(self.measure.delete_btn)
        bar.addWidget(self.measure.clear_btn)
        self.row_checks = {}
        for key, text, tip in (("tension", "Tension", "Residual and lay tension, NPTS, highest tension for spans "
                                                       "within limits."),
                               ("slack", "Slack", "Cable laid, seabed fill and cable used by the rest shape, % over "
                                                  "the route length."),
                               ("slope", "Slope / friction", "Seabed slope against the friction angle atan(mu): "
                                                             "steeper slopes slide.")):
            check = QCheckBox(text)
            check.setToolTip(tip)
            check.setChecked(True)
            check.toggled.connect(self._update_rows)
            bar.addWidget(check)
            self.row_checks[key] = check
        self.previous_check = QCheckBox("Previous run")
        self.previous_check.setToolTip("Show the previous run's cable and tension (dashed grey), to see the effect "
                                       "of a change.")
        self.previous_check.setChecked(True)
        self.previous_check.toggled.connect(self._update_previous)
        bar.addWidget(self.previous_check)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.graphics = pg.GraphicsLayoutWidget()
        self.graphics.setBackground("w")
        layout.addWidget(self.graphics, 1)
        self.status_plot = self.graphics.addPlot(row=0, col=0)
        self.depth_plot = self.graphics.addPlot(row=1, col=0)
        self.tension_plot = self.graphics.addPlot(row=2, col=0)
        self.slack_plot = self.graphics.addPlot(row=3, col=0)
        self.slope_plot = self.graphics.addPlot(row=4, col=0)
        ci = self.graphics.ci.layout
        for row, stretch in enumerate((1, 9, 3, 3, 2)):
            ci.setRowStretchFactor(row, stretch)
        self.status_plot.setMaximumHeight(34)
        self.status_plot.hideAxis("left")
        self.status_plot.hideAxis("bottom")
        self.status_plot.setMouseEnabled(x=True, y=False)
        self.status_plot.setYRange(0, 1, padding=0)
        self.status_plot.getAxis("left").setWidth(64)
        self._data_plots = (self.depth_plot, self.tension_plot, self.slack_plot, self.slope_plot)
        for plot, columns in zip(self._data_plots, (4, 5, 3, 2)):
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getAxis("left").setWidth(64)
            plot.setAutoVisible(y=True)
            # Compact legends along the top right, so they hide little of the data.
            plot.addLegend(offset=(-5, 2), colCount=columns, labelTextColor=(40, 40, 40), labelTextSize="8pt",
                           brush=pg.mkBrush(255, 255, 255, 190), pen=pg.mkPen(210, 210, 210),
                           verSpacing=-6, horSpacing=12)
        self.depth_plot.invertY(True)
        self.depth_plot.setLabel("left", "Depth (m)")
        self.tension_plot.setLabel("left", "Tension (kN)")
        self.slack_plot.setLabel("left", "Slack (%)")
        self.slope_plot.setLabel("left", "Angle (°)")
        self.slope_plot.setLabel("bottom", "KP (km)")
        for plot in (self.status_plot,) + self._data_plots[1:]:
            plot.setXLink(self.depth_plot)

        self.status_bars = pg.BarGraphItem(x0=[0.0], x1=[0.0], y0=[0.0], y1=[1.0], brushes=[pg.mkBrush(0, 0, 0, 0)],
                                           pens=[pg.mkPen(None)])
        self.status_plot.addItem(self.status_bars)

        def curve(plot, colour, name=None, width=1.5, style=None):
            pen = pg.mkPen(colour, width=width) if style is None else pg.mkPen(colour, width=width, style=style)
            item = pg.PlotDataItem([], [], pen=pen, name=name, connect="finite")
            plot.addItem(item)
            item.setDownsampling(auto=True, method="peak")
            item.setClipToView(True)
            return item

        self.raw_curve = curve(self.depth_plot, (200, 175, 165), "Seabed (sampled)", 1)
        self.seabed_curve = curve(self.depth_plot, SEABED, "Seabed", 2)
        self.cable_curve = curve(self.depth_plot, CABLE, "Cable (rest shape)", 1.5)
        self.prev_cable_curve = curve(self.depth_plot, PREVIOUS, "Cable (previous run)", 1, _DASH)
        self.detail_curve = curve(self.depth_plot, DETAIL, "Cable (detail, with EI)", 1.5)
        self.span_fill = QGraphicsPathItem()
        self.span_fill.setBrush(pg.mkBrush(*SPAN_FILL))
        self.span_fill.setPen(pg.mkPen(None))
        self.depth_plot.addItem(self.span_fill, ignoreBounds=True)
        self.supports = pg.ScatterPlotItem([], [], size=7, symbol="t1", pen=pg.mkPen(None),
                                           brush=pg.mkBrush(60, 60, 60), hoverable=True, tip=_tip,
                                           name="Span supports")
        self.depth_plot.addItem(self.supports)
        self.bodies = pg.ScatterPlotItem([], [], size=11, symbol="d", pen=pg.mkPen((30, 30, 30)),
                                         brush=pg.mkBrush(255, 193, 7), hoverable=True, tip=_tip, name="Bodies")
        self.depth_plot.addItem(self.bodies)

        self.tension_curve = curve(self.tension_plot, CABLE, "Residual (on the seabed)", 1.5)
        self.deposit_curve = curve(self.tension_plot, (90, 90, 90), "Lay (touchdown)", 1, _DASH)
        self.npts_curve = curve(self.tension_plot, (211, 47, 47), "NPTS", 1, _DASH)
        self.allowable_curve = curve(self.tension_plot, (0, 137, 123), "Max for spans within limits", 1.2, _DOT)
        self.prev_tension_curve = curve(self.tension_plot, PREVIOUS, "Residual (previous run)", 1, _DASH)

        self.used_curve = curve(self.slack_plot, (90, 90, 90), "Used by the cable", 1, _DASH)
        self.laid_curve = curve(self.slack_plot, CABLE, "Laid", 1.5)
        self.fill_curve = curve(self.slack_plot, SEABED, "Fill (seabed)", 1.2)
        self.surplus = pg.FillBetweenItem(self.used_curve, self.laid_curve, brush=pg.mkBrush(*SURPLUS_FILL))
        self.slack_plot.addItem(self.surplus)
        self.slack_plot.addItem(pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen((160, 160, 160), width=1)))

        self.slope_curve = curve(self.slope_plot, SEABED, "Seabed slope", 1.2)
        self.friction_curve = curve(self.slope_plot, (230, 81, 0), "Friction angle", 1.2, _DASH)

        self._vlines = []
        for plot in self._data_plots:
            line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen((120, 120, 120), width=1, style=_DASH))
            line.hide()
            plot.addItem(line, ignoreBounds=True)
            self._vlines.append(line)

        self.readout = QLabel("Run the analysis to see the cable on the seabed.")
        self.readout.setWordWrap(True)
        self.readout.setTextInteractionFlags(getattr(getattr(Qt, "TextInteractionFlag", Qt), "TextSelectableByMouse"))
        layout.addWidget(self.readout)
        layout.addWidget(self.measure.table)
        self.measure.table.hide()
        self.measure.measurementsChanged.connect(lambda: self.measure.table.setVisible(bool(self.measure.measurements)))
        self.measure.attach(self.depth_plot)
        scene = self.graphics.scene()
        scene.sigMouseMoved.connect(self._on_mouse_moved)
        scene.sigMouseClicked.connect(self._on_mouse_clicked)
        self._update_rows()

    # -- data ------------------------------------------------------------------
    def set_result(self, result: Optional[pl.PlanningResult], previous: Optional[pl.PlanningResult] = None,
                   keep_view: bool = False) -> None:
        self._result = result
        self._previous = previous
        try:
            self._draw()
        except Exception:
            log_exception("Span & Slack Analysis: drawing the profile failed")
        if not keep_view:
            self.fit()

    def set_detail(self, detail) -> None:
        """Overlay a detail (bending stiffness) solution, or clear it (None)."""
        if detail is None:
            self.detail_curve.setData([], [])
            return
        self.detail_curve.setData(detail.kp_km, -detail.beam.y_m)

    def _draw(self) -> None:
        r = self._result
        if r is None:
            for item in (self.raw_curve, self.seabed_curve, self.cable_curve, self.detail_curve,
                         self.tension_curve, self.deposit_curve, self.npts_curve, self.allowable_curve,
                         self.used_curve, self.laid_curve, self.fill_curve, self.slope_curve, self.friction_curve):
                item.setData([], [])
            self.span_fill.setPath(QPainterPath())
            self.supports.setData([], [])
            self.bodies.setData([], [])
            self._clear_status()
            self.measure.set_series([])
            self.readout.setText("Run the analysis to see the cable on the seabed.")
            self._update_previous()
            return
        st = r.stations
        kp = st.kp_km
        seabed_depth = -st.seabed_m
        cable_depth = -r.cable_m
        filtered = st.filter_m > 0 or bool(np.any(st.feature_m > 0))
        self.raw_curve.setData(kp, -st.raw_elev_m if filtered else np.full(len(kp), np.nan))
        self.seabed_curve.setData(kp, -st.obstacle_m)
        self.cable_curve.setData(kp, cable_depth)
        self.detail_curve.setData([], [])
        self.span_fill.setPath(spans_path(kp, cable_depth, -st.obstacle_m, r.spans))
        self._draw_supports(r)
        self._draw_bodies(r)
        self.tension_curve.setData(kp, r.tension_kn)
        self.deposit_curve.setData(kp, r.deposit_kn)
        self.npts_curve.setData(kp, st.npts_kn)
        if r.allowable_kn is not None:
            self.allowable_curve.setData(kp, np.where(r.allowable_kn < 99.9, r.allowable_kn, np.nan))
        else:
            self.allowable_curve.setData([], [])
        self.used_curve.setData(kp, r.shape_pct)
        self.laid_curve.setData(kp, r.laid_pct)
        self.fill_curve.setData(kp, r.fill_pct)
        self.slope_curve.setData(kp, np.degrees(np.arctan(np.abs(st.slope))))
        self.friction_curve.setData(kp, np.degrees(np.arctan(st.mu)))
        self._draw_status(r)
        self._update_previous()
        ok = np.isfinite(seabed_depth)
        series = [{"name": "Seabed", "x": kp[ok] * 1000.0, "y": -st.obstacle_m[ok]}]
        ok = np.isfinite(cable_depth)
        series.append({"name": "Cable", "x": kp[ok] * 1000.0, "y": cable_depth[ok]})
        self.measure.set_series(series)
        self.readout.setText("Hover the profile for the values at a KP. Click a finding or a span to zoom to it; "
                             "click the profile to show the KP on the map (double-click centres the map).")

    def _draw_status(self, r: pl.PlanningResult) -> None:
        kp = r.stations.kp_km
        lo, hi = float(kp[0]), float(kp[-1])
        bin_m = max((hi - lo) * 1000.0 / 4000.0, 0.5)
        edges, level = r.status(bin_m)
        if not len(level):
            self._clear_status()
            return
        # Equal neighbours collapse, so the bar is a handful of rectangles.
        change = np.nonzero(np.diff(level))[0] + 1
        starts = np.concatenate(([0], change))
        ends = np.concatenate((change, [len(level)]))
        brushes = [pg.mkBrush(*LEVEL_COLOURS.get(int(level[s]), (200, 200, 200))) for s in starts]
        self.status_bars.setOpts(x0=edges[starts], x1=edges[ends], y0=np.zeros(len(starts)),
                                 y1=np.ones(len(starts)), brushes=brushes, pens=[pg.mkPen(None)] * len(starts))

    def _clear_status(self) -> None:
        self.status_bars.setOpts(x0=[0.0], x1=[0.0], y0=[0.0], y1=[1.0], brushes=[pg.mkBrush(0, 0, 0, 0)],
                                 pens=[pg.mkPen(None)])

    def _draw_supports(self, r: pl.PlanningResult) -> None:
        if not r.supports:
            self.supports.setData([], [])
            return
        st = r.stations
        kps, depths, brushes, tips = [], [], [], []
        for sp in r.supports:
            k = (sp.i0 + sp.i1) // 2
            kps.append(sp.kp)
            depths.append(float(-st.obstacle_m[k]))
            brushes.append(pg.mkBrush(211, 47, 47) if sp.below_mbr else pg.mkBrush(60, 60, 60, 170))
            lines = [f"Span support at KP {sp.kp:.3f}",
                     f"Seabed reaction {sp.reaction_kn:.2f} kN" if math.isfinite(sp.reaction_kn) else "",
                     f"Bend radius {_radius_text(sp.bend_radius_m)}"
                     + (f" (MBR {sp.mbr_m:.1f} m)" if math.isfinite(sp.mbr_m) else ""),
                     f"Moment {sp.moment_knm:.2f} kNm" if math.isfinite(sp.moment_knm) else "",
                     f"Tension {sp.tension_kn:.2f} kN"]
            tips.append("\n".join(line for line in lines if line))
        self.supports.setData(np.array(kps), np.array(depths), brush=brushes, data=tips)

    def _draw_bodies(self, r: pl.PlanningResult) -> None:
        if not r.bodies:
            self.bodies.setData([], [])
            return
        st = r.stations
        kps, depths, tips = [], [], []
        for b in r.bodies:
            i = int(np.clip(np.searchsorted(st.kp_km, b.kp_km), 0, st.n - 1))
            kps.append(b.kp_km)
            depths.append(float(-r.cable_m[i]))
            body = b.body
            tips.append("\n".join([
                f"{body.name} ({body.kind}) at KP {b.kp_km:.3f}",
                f"Weight in water {body.weight_kn:.2f} kN, length {body.length_m:.1f} m",
                f"Clear of the seabed by up to {b.max_gap_m:.2f} m" + (" (in a span)" if b.in_span else ""),
                f"Tension {b.tension_kn:.2f} kN"]))
        self.bodies.setData(np.array(kps), np.array(depths), data=tips)

    def _update_previous(self, *_args) -> None:
        prev = self._previous if self.previous_check.isChecked() else None
        if prev is None or self._result is None:
            self.prev_cable_curve.setData([], [])
            self.prev_tension_curve.setData([], [])
            return
        self.prev_cable_curve.setData(prev.stations.kp_km, -prev.cable_m)
        self.prev_tension_curve.setData(prev.stations.kp_km, prev.tension_kn)

    def _update_rows(self, *_args) -> None:
        for key, plot in (("tension", self.tension_plot), ("slack", self.slack_plot), ("slope", self.slope_plot)):
            visible = self.row_checks[key].isChecked()
            plot.setVisible(visible)
            plot.setMaximumHeight(16777215 if visible else 0)
        # The KP axis labels sit on the lowest visible row.
        lowest = next((p for p in reversed(self._data_plots) if p.isVisible()), self.depth_plot)
        for plot in self._data_plots:
            plot.showAxis("bottom", plot is lowest)
            if plot is lowest:
                plot.setLabel("bottom", "KP (km)")

    # -- view --------------------------------------------------------------------
    def fit(self) -> None:
        r = self._result
        if r is None:
            return
        kp = r.stations.kp_km
        self.depth_plot.setXRange(float(kp[0]), float(kp[-1]), padding=0.01)
        for plot in self._data_plots:
            plot.enableAutoRange(axis="y")

    def zoom_to(self, kp_a: float, kp_b: float) -> None:
        lo, hi = sorted((kp_a, kp_b))
        pad = max((hi - lo) * 0.75, 0.02)
        self.depth_plot.setXRange(lo - pad, hi + pad, padding=0)
        for plot in self._data_plots:
            plot.enableAutoRange(axis="y")

    def visible_kp(self):
        lo, hi = self.depth_plot.viewRange()[0]
        return float(lo), float(hi)

    # -- hover / click ----------------------------------------------------------
    def _kp_at(self, pos) -> Optional[float]:
        for plot in self._data_plots:
            if plot.isVisible() and plot.sceneBoundingRect().contains(pos):
                return float(plot.vb.mapSceneToView(pos).x())
        return None

    def _on_mouse_moved(self, pos) -> None:
        if self._result is None or self.measure.active:
            return
        kp = self._kp_at(pos)
        if kp is None:
            for line in self._vlines:
                line.hide()
            return
        self.show_kp(kp)
        self.kpHovered.emit(kp)

    def show_kp(self, kp: float) -> None:
        r = self._result
        if r is None:
            return
        st = r.stations
        i = int(np.clip(np.searchsorted(st.kp_km, kp), 0, st.n - 1))
        if i > 0 and abs(st.kp_km[i - 1] - kp) < abs(st.kp_km[i] - kp):
            i -= 1
        for line in self._vlines:
            line.setPos(float(st.kp_km[i]))
            line.show()
        self.readout.setText(self.describe(i))

    def describe(self, i: int) -> str:
        r = self._result
        st = r.stations
        gap = float(r.gap_m[i])
        cable = st.cable_at(i)
        parts = [f"KP {st.kp_km[i]:.3f}", f"depth {-st.seabed_m[i]:.1f} m",
                 f"gap {gap:.2f} m" if gap > 0.005 else "on the seabed",
                 f"T {r.tension_kn[i]:.2f} kN (laid {r.deposit_kn[i]:.2f})"]
        if math.isfinite(r.laid_pct[i]):
            parts.append(f"slack laid {r.laid_pct[i]:.2f}%, fill {r.fill_pct[i]:.2f}%, surplus {r.excess_pct[i]:.2f}%")
        parts.append(f"slope {math.degrees(math.atan(abs(st.slope[i]))):.1f}° (friction "
                     f"{math.degrees(math.atan(st.mu[i])):.0f}°, μ {st.mu[i]:.2f})")
        label = ""
        if st.body_index[i] >= 0:
            label = next((b.body.name for b in st.bodies if b.i0 <= i <= b.i1), "body")
        parts.append(label or cable.name)
        if st.soil[i]:
            parts.append(st.soil[i])
        return "  |  ".join(parts)

    def _on_mouse_clicked(self, event) -> None:
        if self._result is None or self.measure.active:
            return
        kp = self._kp_at(event.scenePos())
        if kp is None:
            return
        if event.double():
            self.kpDoubleClicked.emit(kp)
        else:
            self.kpClicked.emit(kp)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.measure.handle_key(event):
            return
        super().keyPressEvent(event)
