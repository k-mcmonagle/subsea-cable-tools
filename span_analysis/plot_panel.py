# -*- coding: utf-8 -*-
"""The Span & Slack Analysis profile: linked rows on one KP axis, in a splitter.

* status bar: the worst finding per KP (green clear, blue info, amber, red, grey no data);
* profile: sampled seabed, the seabed the cable rests on, the modelled cable,
  spans filled by level (red, amber; light grey where the cable is clear of
  the seabed but under the reporting limits), span supports (red below the
  MBR; hover for the reaction and bend radius), bodies (hover or click for
  their details, optional labels), the detail model's cable and sea level;
* tension: residual tension on the seabed, the lay (deposit) tension, NPTS and
  the highest tension that keeps spans within the limits;
* slack: cable laid, the fill (seabed length over route length), the cable the
  rest shape uses and the RPL's planned slack, all % over the route; the
  surplus is shaded;
* slope and friction: the seabed slope against the friction angle ``atan mu``.

Drag the splitter handles to share the height between rows. View options:
true scale (1:1), sea level, reversed KP and body labels. Items are built once
and updated with ``setData`` (removing pyqtgraph items is slow on long routes
and noisy under QGIS 3.40). Hover moves a shared crosshair and reports every
value at that KP; the dialog links it to the map.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import QEvent, QRectF, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtGui import QPainterPath
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QGraphicsPathItem,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..cable_seabed import planning as pl
from ..cable_seabed.core import runs
from ..maptools.profile_measure_controller import ProfileMeasureController
from ..plugin_log import log_exception

_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
_DOT = getattr(getattr(Qt, "PenStyle", Qt), "DotLine")
_VERTICAL = getattr(getattr(Qt, "Orientation", Qt), "Vertical")
_SETTINGS = "SubseaCableTools/SpanAnalysis/plot"
LEVEL_COLOURS = {-1: (200, 200, 200), 0: (76, 175, 80), 1: (31, 119, 180), 2: (255, 152, 0), 3: (211, 47, 47)}
SEABED = (121, 85, 72)
CABLE = (25, 70, 160)
RED_FILL = (211, 47, 47, 150)
AMBER_FILL = (255, 152, 0, 140)
CLEAR_FILL = (120, 120, 120, 55)
SURPLUS_FILL = (76, 175, 80, 90)
DETAIL = (123, 31, 162)
PROTECTION = (96, 125, 139, 150)
PREVIOUS = (150, 150, 150)
SEA = "#17becf"
_LEAVE = getattr(getattr(QEvent, "Type", QEvent), "Leave")
TRUE_SCALE_RATIO = 1000.0   # KP in km, depth in m


def runs_path(kp: np.ndarray, top: np.ndarray, bottom: np.ndarray, extents: Sequence[Tuple[int, int]]) -> QPainterPath:
    """One path with a closed polygon per ``(i0, i1)`` run (cable above, seabed below)."""
    xs, ys, connect = [], [], []
    for i0, i1 in extents:
        sl = slice(i0, i1 + 1)
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


_GLYPH = {"solid": "\u2501\u2501", "dash": "\u254d\u254d", "dot": "\u2508\u2508", "fill": "\u25a0",
          "triangle": "\u25b2", "diamond": "\u25c6"}


def _hex(colour) -> str:
    if isinstance(colour, str):
        return colour
    return "#%02x%02x%02x" % tuple(int(c) for c in colour[:3])


_LEGENDS = {
    "depth": [("dash", SEA, "Sea level"), ("solid", (200, 175, 165), "Seabed (sampled)"), ("solid", SEABED, "Seabed"),
              ("solid", CABLE, "Cable (rest shape)"), ("fill", PROTECTION, "Protection"),
              ("dash", PREVIOUS, "Previous run"),
              ("solid", DETAIL, "Cable with EI (detail)"), ("fill", RED_FILL, "Span (red)"),
              ("fill", AMBER_FILL, "Span (amber)"), ("fill", (170, 170, 170), "Clear of seabed, under the limits"),
              ("triangle", (60, 60, 60), "Span support / end"), ("diamond", (255, 193, 7), "Body")],
    "tension": [("solid", CABLE, "Residual (on the seabed)"), ("dash", (90, 90, 90), "Lay (touchdown)"),
                ("dash", (211, 47, 47), "NPTS"), ("dot", (0, 137, 123), "Max for spans within limits"),
                ("dash", PREVIOUS, "Previous run")],
    "slack": [("solid", CABLE, "Laid"), ("solid", SEABED, "Fill (seabed)"), ("dash", (90, 90, 90), "Used by the cable"),
              ("fill", SURPLUS_FILL, "Surplus"), ("dot", (233, 30, 99), "RPL slack (plan)")],
    "slope": [("solid", SEABED, "Seabed slope"), ("dash", (230, 81, 0), "Friction angle atan(\u03bc)")],
}


def _legend_html(entries) -> str:
    parts = []
    for kind, colour, text in entries:
        glyph = _GLYPH[kind]
        parts.append(f"<span style='color:{_hex(colour)}; font-weight:bold'>{glyph}</span>&nbsp;{text}")
    return "&nbsp;&nbsp;&nbsp;".join(parts)


class DirectAxis(pg.AxisItem):
    """An axis that paints straight to the screen.

    pyqtgraph records an axis (ticks, labels, grid lines) in a QPicture and
    replays it. Under Qt5 (QGIS 3) with display scaling a QPicture records at
    the screen's logical DPI (e.g. 144 at 150 %) while the plot paints at 96, so
    the replay shrank the axis and its grid to two thirds of the plot.
    """

    def paint(self, p, opt, widget):
        p.save()
        try:
            if self.style["tickFont"]:
                p.setFont(self.style["tickFont"])
            specs = self.generateDrawSpecs(p)
            if specs is not None:
                self.drawPicture(p, *specs)
        finally:
            p.restore()


def _plot_widget() -> "pg.PlotWidget":
    return pg.PlotWidget(axisItems={"left": DirectAxis("left"), "bottom": DirectAxis("bottom")})


def _tip(x, y, data):
    return data if isinstance(data, str) else f"KP {x:.3f}"


def _radius_text(radius: float) -> str:
    if not (math.isfinite(radius) or radius == math.inf):
        return "unknown (no EI)"
    return "over 1000 m" if radius > 1000 else f"{radius:.1f} m"


def _setting(key: str, default: bool) -> bool:
    try:
        return bool(QSettings().value(f"{_SETTINGS}/{key}", default, type=bool))
    except Exception:
        return default


class ProfilePlot(QWidget):
    kpHovered = pyqtSignal(float)    # nan: the cursor left the profile
    kpClicked = pyqtSignal(float)
    kpDoubleClicked = pyqtSignal(float)
    bodyClicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.persist = True
        self._result: Optional[pl.PlanningResult] = None
        self._previous: Optional[pl.PlanningResult] = None
        self._body_labels: List[pg.TextItem] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        fit = QPushButton("Fit")
        fit.setToolTip("Show the whole profile.")
        fit.clicked.connect(self.fit)
        bar.addWidget(fit)
        self.measure = ProfileMeasureController(self, plot_factors=lambda: (0.001, 1.0),
                                                text_units=lambda: ("m", "m"), depth_down=lambda: True,
                                                x_inverted=lambda: self.reverse_check.isChecked())
        self.measure.statusChanged.connect(lambda text: self.readout.setText(text))
        measure_btn = QToolButton()
        measure_btn.setDefaultAction(self.measure.action)
        bar.addWidget(measure_btn)
        bar.addWidget(self.measure.snap_check)
        bar.addWidget(self.measure.source_combo)
        bar.addWidget(self.measure.delete_btn)
        bar.addWidget(self.measure.clear_btn)
        bar.addStretch(1)
        layout.addLayout(bar)

        view = QHBoxLayout()
        self.true_scale_check = QCheckBox("True scale (1:1)")
        self.true_scale_check.setToolTip("Lock the depth and KP axes to the same scale (1 km along the route = "
                                         "1000 m of depth), so slopes and spans show as they really are.")
        self.sea_check = QCheckBox("Sea level (0 m)")
        self.sea_check.setStyleSheet(f"color: {SEA}; font-weight: 600;")
        self.sea_check.setToolTip("Show the water surface as a dashed line at 0 m. While shown, the full view "
                                  "includes the surface; zoom in to see the seabed detail.")
        self.reverse_check = QCheckBox("Reverse KP")
        self.reverse_check.setToolTip("KP increasing from right to left (e.g. to match the lay direction).")
        self.labels_check = QCheckBox("Body labels")
        self.labels_check.setToolTip("Name each in-line body on the profile. Hover or click a body for its details.")
        for check, key, default in ((self.true_scale_check, "true_scale", False), (self.sea_check, "sea_level", True),
                                    (self.reverse_check, "reverse_kp", False), (self.labels_check, "body_labels", True)):
            check.setChecked(_setting(key, default))
            check.toggled.connect(lambda on, k=key: self._remember(k, on))
            view.addWidget(check)
        view.addSpacing(12)
        self.row_checks = {}
        for key, text, tip in (("tension", "Tension", "Residual and lay tension, NPTS, highest tension for spans "
                                                       "within limits."),
                               ("slack", "Slack", "Cable laid, seabed fill, cable used by the rest shape and RPL "
                                                  "planned slack, % over the route length."),
                               ("slope", "Slope / friction", "Seabed slope against the friction angle atan(mu): "
                                                             "steeper slopes slide.")):
            check = QCheckBox(text)
            check.setToolTip(tip)
            check.setChecked(_setting(f"row_{key}", True))
            check.toggled.connect(self._update_rows)
            check.toggled.connect(lambda on, k=key: self._remember(f"row_{k}", on))
            view.addWidget(check)
            self.row_checks[key] = check
        self.previous_check = QCheckBox("Previous run")
        self.previous_check.setToolTip("Show the previous run's cable and tension (dashed grey), to see the effect "
                                       "of a change.")
        self.previous_check.setChecked(True)
        self.previous_check.toggled.connect(self._update_previous)
        view.addWidget(self.previous_check)
        self.legend_check = QCheckBox("Legends")
        self.legend_check.setToolTip("Show the key above each row.")
        self.legend_check.setChecked(_setting("legends", True))
        self.legend_check.toggled.connect(lambda on: self._remember("legends", on))
        view.addWidget(self.legend_check)
        view.addStretch(1)
        layout.addLayout(view)

        self.splitter = QSplitter(_VERTICAL)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)
        self.widgets = {}
        self.rows = {}
        self.captions = {}
        for key in ("status", "depth", "tension", "slack", "slope"):
            widget = _plot_widget()
            widget.setBackground("w")
            self.widgets[key] = widget
            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)
            if key != "status":
                # The legend sits above the plot: it never hides data or overlaps its own text.
                caption = QLabel(_legend_html(_LEGENDS[key]))
                caption.setWordWrap(True)
                caption.setStyleSheet("QLabel { background: white; color: #333; font-size: 8pt; "
                                      "padding: 1px 4px 0px 68px; }")
                row_layout.addWidget(caption)
                self.captions[key] = caption
            row_layout.addWidget(widget, 1)
            self.rows[key] = row
            self.splitter.addWidget(row)
        self.status_plot = self.widgets["status"].getPlotItem()
        self.depth_plot = self.widgets["depth"].getPlotItem()
        self.tension_plot = self.widgets["tension"].getPlotItem()
        self.slack_plot = self.widgets["slack"].getPlotItem()
        self.slope_plot = self.widgets["slope"].getPlotItem()
        self.rows["status"].setFixedHeight(26)
        self.widgets["depth"].setMinimumHeight(120)
        for key in ("tension", "slack", "slope"):
            self.widgets[key].setMinimumHeight(60)
        for index, stretch in enumerate((0, 5, 2, 2, 2)):
            self.splitter.setStretchFactor(index, stretch)
        self.splitter.setSizes([26, 440, 150, 150, 150])
        try:
            state = QSettings().value(f"{_SETTINGS}/splitter")
            if state is not None:
                self.splitter.restoreState(state)
        except Exception:
            pass
        self.splitter.splitterMoved.connect(lambda *_: self._remember("splitter", None))

        status_axis = self.status_plot.getAxis("left")
        status_axis.setWidth(64)
        status_axis.setStyle(showValues=False)
        status_axis.setPen(pg.mkPen(None))
        status_axis.setTicks([[], []])
        self.status_plot.hideAxis("bottom")
        self.status_plot.setMouseEnabled(x=True, y=False)
        self.status_plot.setYRange(0, 1, padding=0)
        self.status_plot.hideButtons()
        self._data_plots = (self.depth_plot, self.tension_plot, self.slack_plot, self.slope_plot)
        for plot in self._data_plots:
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getAxis("left").setWidth(64)
            plot.setAutoVisible(y=True)
            plot.hideButtons()
        self.depth_plot.invertY(True)
        small = {"font-size": "11px"}   # pixels: point sizes scale with the screen DPI under Qt5
        self.depth_plot.setLabel("left", "Depth (m)", **small)
        self.tension_plot.setLabel("left", "T (kN)", **small)
        self.slack_plot.setLabel("left", "Slack (%)", **small)
        self.slope_plot.setLabel("left", "Angle (°)", **small)
        for plot in (self.status_plot,) + self._data_plots[1:]:
            plot.setXLink(self.depth_plot)

        # One filled path per level. (BarGraphItem records its bars in a QPicture, which
        # rounds KP-in-km rectangles to whole units under Qt5.)
        self.status_paths = {}
        for level, colour in LEVEL_COLOURS.items():
            item = QGraphicsPathItem()
            item.setBrush(pg.mkBrush(*colour))
            item.setPen(pg.mkPen(None))
            self.status_plot.addItem(item)
            self.status_paths[level] = item

        def curve(plot, colour, name=None, width=1.5, style=None, fast=True):
            pen = pg.mkPen(colour, width=width) if style is None else pg.mkPen(colour, width=width, style=style)
            item = pg.PlotDataItem([], [], pen=pen, name=name, connect="finite")
            plot.addItem(item)
            if fast:
                item.setDownsampling(auto=True, method="peak")
                item.setClipToView(True)
            return item

        self.sea_curve = curve(self.depth_plot, SEA, "Sea level", 1.5, _DASH, fast=False)
        self.sea_curve.setZValue(3)
        self.raw_curve = curve(self.depth_plot, (200, 175, 165), "Seabed (sampled)", 1)
        self.seabed_curve = curve(self.depth_plot, SEABED, "Seabed", 2)
        self.protection_curve = curve(self.depth_plot, PROTECTION, "Protection", 7, fast=False)
        self.cable_curve = curve(self.depth_plot, CABLE, "Cable (rest shape)", 1.5)
        self.prev_cable_curve = curve(self.depth_plot, PREVIOUS, "Cable (previous run)", 1, _DASH)
        self.detail_curve = curve(self.depth_plot, DETAIL, "Cable (detail, with EI)", 1.5)
        self.fills = {}
        for key, brush in (("red", RED_FILL), ("amber", AMBER_FILL), ("clear", CLEAR_FILL)):
            item = QGraphicsPathItem()
            item.setBrush(pg.mkBrush(*brush))
            item.setPen(pg.mkPen(None))
            self.depth_plot.addItem(item, ignoreBounds=True)
            self.fills[key] = item
        self.supports = pg.ScatterPlotItem([], [], size=7, symbol="t1", pen=pg.mkPen(None),
                                           brush=pg.mkBrush(60, 60, 60), hoverable=True, tip=_tip,
                                           name="Span supports")
        self.depth_plot.addItem(self.supports)
        self.bodies = pg.ScatterPlotItem([], [], size=12, symbol="d", pen=pg.mkPen((30, 30, 30)),
                                         brush=pg.mkBrush(255, 193, 7), hoverable=True, tip=_tip, name="Bodies")
        self.bodies.setZValue(20)
        self.bodies.sigClicked.connect(self._on_body_clicked)
        self.depth_plot.addItem(self.bodies)

        self.tension_curve = curve(self.tension_plot, CABLE, "Residual (on the seabed)", 1.5)
        self.deposit_curve = curve(self.tension_plot, (90, 90, 90), "Lay (touchdown)", 1, _DASH)
        self.npts_curve = curve(self.tension_plot, (211, 47, 47), "NPTS", 1, _DASH)
        self.allowable_curve = curve(self.tension_plot, (0, 137, 123), "Max for spans within limits", 1.2, _DOT)
        self.prev_tension_curve = curve(self.tension_plot, PREVIOUS, "Residual (previous run)", 1, _DASH)

        self.used_curve = curve(self.slack_plot, (90, 90, 90), "Used by the cable", 1, _DASH)
        self.laid_curve = curve(self.slack_plot, CABLE, "Laid", 1.5)
        self.fill_curve = curve(self.slack_plot, SEABED, "Fill (seabed)", 1.2)
        self.plan_curve = curve(self.slack_plot, (233, 30, 99), "RPL slack (plan)", 1.2, _DOT, fast=False)
        self.surplus = pg.FillBetweenItem(self.used_curve, self.laid_curve, brush=pg.mkBrush(*SURPLUS_FILL))
        self.slack_plot.addItem(self.surplus)
        self.slack_plot.addItem(pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen((160, 160, 160), width=1)))

        self.slope_curve = curve(self.slope_plot, SEABED, "Seabed slope", 1.2)
        self.friction_curve = curve(self.slope_plot, (230, 81, 0), "Friction angle", 1.2, _DASH)

        self._vlines = []
        for plot in (self.status_plot,) + self._data_plots:
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
        for key in ("depth", "tension", "slack", "slope"):
            scene = self.widgets[key].scene()
            scene.sigMouseMoved.connect(lambda pos, k=key: self._on_mouse_moved(k, pos))
            scene.sigMouseClicked.connect(lambda event, k=key: self._on_mouse_clicked(k, event))
            self.widgets[key].installEventFilter(self)
        self.true_scale_check.toggled.connect(self._apply_true_scale)
        self.sea_check.toggled.connect(self._update_sea)
        self.reverse_check.toggled.connect(self._apply_reverse)
        self.labels_check.toggled.connect(self._update_body_labels)
        self.legend_check.toggled.connect(self._update_legends)
        self._update_legends()
        self._update_rows()
        self._apply_reverse(self.reverse_check.isChecked())

    # -- settings --------------------------------------------------------------
    def _remember(self, key: str, value) -> None:
        if not self.persist:
            return
        try:
            if key == "splitter":
                QSettings().setValue(f"{_SETTINGS}/splitter", self.splitter.saveState())
            else:
                QSettings().setValue(f"{_SETTINGS}/{key}", bool(value))
        except Exception:
            pass

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
            for item in (self.raw_curve, self.seabed_curve, self.cable_curve, self.detail_curve, self.sea_curve,
                         self.protection_curve,
                         self.tension_curve, self.deposit_curve, self.npts_curve, self.allowable_curve,
                         self.used_curve, self.laid_curve, self.fill_curve, self.plan_curve, self.slope_curve,
                         self.friction_curve):
                item.setData([], [])
            for item in self.fills.values():
                item.setPath(QPainterPath())
            self.supports.setData([], [])
            self.bodies.setData([], [])
            self._update_body_labels()
            self._clear_status()
            self.measure.set_series([])
            self._update_previous()
            self.readout.setText("Run the analysis to see the cable on the seabed.")
            return
        st = r.stations
        kp = st.kp_km
        cable_depth = -r.cable_m
        floor = -st.obstacle_m
        shaped = st.filter_m > 0 or bool(np.any(st.feature_m != 0))
        self.raw_curve.setData(kp, -st.raw_elev_m if shaped else np.full(len(kp), np.nan))
        self.seabed_curve.setData(kp, floor)
        self.cable_curve.setData(kp, cable_depth)
        index = st.protection_index
        if index is not None and np.any(index >= 0):
            self.protection_curve.setData(kp, np.where(index >= 0, cable_depth, np.nan))
        else:
            self.protection_curve.setData([], [])
        self.detail_curve.setData([], [])
        self._draw_fills(r, kp, cable_depth, floor)
        self._draw_supports(r)
        self._draw_bodies(r)
        self._update_sea()
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
        self._draw_plan(r)
        self.slope_curve.setData(kp, np.degrees(np.arctan(np.abs(st.slope))))
        self.friction_curve.setData(kp, np.degrees(np.arctan(st.mu)))
        self._draw_status(r)
        self._update_previous()
        ok = np.isfinite(floor)
        series = [{"name": "Seabed", "x": kp[ok] * 1000.0, "y": floor[ok]}]
        ok = np.isfinite(cable_depth)
        series.append({"name": "Cable", "x": kp[ok] * 1000.0, "y": cable_depth[ok]})
        self.measure.set_series(series)
        self.readout.setText("Hover the profile for the values at a KP; hover or click a body for its details. Click "
                             "a finding or a span to zoom to it; click the profile to show the KP on the map "
                             "(double-click centres the map). Drag the handles between rows to resize them.")

    def _draw_fills(self, r: pl.PlanningResult, kp, cable_depth, floor) -> None:
        criteria = r.criteria
        red, amber = [], []
        for span in r.spans:
            (red if pl._span_red(span, criteria) else amber).append((span.i0, span.i1))
        with np.errstate(invalid="ignore"):
            free = np.isfinite(r.gap_m) & (r.gap_m > pl.span_end_tol(criteria))
        reported = np.zeros(len(kp), dtype=bool)
        for span in r.spans:
            reported[span.i0:span.i1 + 1] = True
        clear = [(max(a - 1, 0), min(b + 1, len(kp) - 1)) for a, b in runs(free & ~reported)]
        self.fills["red"].setPath(runs_path(kp, cable_depth, floor, red))
        self.fills["amber"].setPath(runs_path(kp, cable_depth, floor, amber))
        self.fills["clear"].setPath(runs_path(kp, cable_depth, floor, clear))

    def _draw_plan(self, r: pl.PlanningResult) -> None:
        legs = list(r.control.legs or [])
        if not legs:
            self.plan_curve.setData([], [])
            return
        lo, hi = float(r.stations.kp_km[0]), float(r.stations.kp_km[-1])
        xs, ys = [], []
        for k0, k1, slack in sorted(legs):
            a, b = max(min(k0, k1), lo), min(max(k0, k1), hi)
            if b <= a:
                continue
            xs.extend([a, b, b])
            ys.extend([slack, slack, np.nan])
        self.plan_curve.setData(np.array(xs), np.array(ys))

    def _draw_status(self, r: pl.PlanningResult) -> None:
        kp = r.stations.kp_km
        lo, hi = float(kp[0]), float(kp[-1])
        bin_m = max((hi - lo) * 1000.0 / 4000.0, 0.5)
        edges, level = r.status(bin_m)
        if not len(level):
            self._clear_status()
            return
        # Equal neighbours collapse, so each level is a handful of rectangles.
        change = np.nonzero(np.diff(level))[0] + 1
        starts = np.concatenate(([0], change))
        ends = np.concatenate((change, [len(level)]))
        paths = {key: QPainterPath() for key in self.status_paths}
        for s, e in zip(starts, ends):
            key = int(level[s]) if int(level[s]) in paths else -1
            paths[key].addRect(QRectF(float(edges[s]), 0.0, float(edges[e] - edges[s]), 1.0))
        for key, item in self.status_paths.items():
            item.setPath(paths[key])

    def _clear_status(self) -> None:
        for item in self.status_paths.values():
            item.setPath(QPainterPath())

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
            over = pl._over_contact(sp.reaction_kn, r.criteria)
            brushes.append(pg.mkBrush(211, 47, 47) if (sp.below_mbr or over) else pg.mkBrush(60, 60, 60, 170))
            if sp.crest:
                head = f"Span support (crest) at KP {sp.kp:.3f}"
            else:
                head = f"Span end on the seabed (touchdown / lift-off) at KP {sp.kp:.3f}"
            if not math.isfinite(sp.reaction_kn):
                reaction = ""
            elif sp.reaction_kn < 0.005 and not sp.crest:
                reaction = "No concentrated reaction: the cable meets the seabed tangentially"
            else:
                reaction = f"Seabed reaction {sp.reaction_kn:.2f} kN" + (
                    f", ABOVE the {r.criteria.max_reaction_kn:g} kN contact limit" if over else "")
            lines = [head, reaction,
                     f"Bend radius {_radius_text(sp.bend_radius_m)}"
                     + (f" (MBR {sp.mbr_m:.1f} m)" if math.isfinite(sp.mbr_m) else ""),
                     f"Moment {sp.moment_knm:.2f} kNm" if math.isfinite(sp.moment_knm) else "",
                     f"Tension {sp.tension_kn:.2f} kN"]
            tips.append("\n".join(line for line in lines if line))
        self.supports.setData(np.array(kps), np.array(depths), brush=brushes, data=tips)

    def body_text(self, index: int) -> str:
        r = self._result
        if r is None or not (0 <= index < len(r.bodies)):
            return ""
        b = r.bodies[index]
        body = b.body
        if b.max_gap_m > 0.005:
            where = f"Clear of the seabed by up to {b.max_gap_m:.2f} m" + (" (in a span)" if b.in_span else "")
        else:
            where = "On the seabed" + (f" (housing standoff {b.standoff_m:.2f} m)" if b.standoff_m > 0 else "")
        lines = [f"{body.name} ({body.kind}) at KP {b.kp_km:.3f}",
                 f"Weight in water {body.weight_kn:.2f} kN, rigid length {body.length_m:.1f} m"
                 + (f", diameter {body.diameter_m * 1000:.0f} mm" if math.isfinite(body.diameter_m) else ""),
                 where, f"Tension {b.tension_kn:.2f} kN"]
        return "\n".join(lines)

    def _draw_bodies(self, r: pl.PlanningResult) -> None:
        if not r.bodies:
            self.bodies.setData([], [])
            self._update_body_labels()
            return
        st = r.stations
        kps, depths, tips = [], [], []
        for k, b in enumerate(r.bodies):
            i = int(np.clip(np.searchsorted(st.kp_km, b.kp_km), 0, st.n - 1))
            kps.append(b.kp_km)
            depths.append(float(-r.cable_m[i]))
            tips.append(self.body_text(k))
        self.bodies.setData(np.array(kps), np.array(depths), data=tips)
        self._update_body_labels()

    def _labels(self) -> List[Tuple[str, float, float]]:
        """Body and protection names with their KP and depth."""
        r = self._result
        if r is None or not self.labels_check.isChecked():
            return []
        st = r.stations
        out = []
        for b in r.bodies:
            i = int(np.clip(np.searchsorted(st.kp_km, b.kp_km), 0, st.n - 1))
            out.append((b.body.name, b.kp_km, float(-r.cable_m[i])))
        index = st.protection_index
        for j, item in enumerate(st.protection):
            idx = np.nonzero(index == j)[0] if index is not None else []
            if len(idx):
                i = int(idx[len(idx) // 2])
                out.append((item.name, float(st.kp_km[i]), float(-r.cable_m[i])))
        return out

    def _update_body_labels(self, *_args) -> None:
        bodies = self._labels()
        while len(self._body_labels) < len(bodies):
            label = pg.TextItem("", color=(40, 40, 40), anchor=(0.5, 1.3))
            font = label.textItem.font()
            font.setPixelSize(11)
            label.setFont(font)
            label.setZValue(25)
            self.depth_plot.addItem(label, ignoreBounds=True)
            self._body_labels.append(label)
        for k, label in enumerate(self._body_labels):
            if k < len(bodies):
                text, kp, depth = bodies[k]
                label.setText(text)
                label.setPos(kp, depth)
                label.setVisible(True)
            else:
                label.setVisible(False)

    def _on_body_clicked(self, _item, points, *_args) -> None:
        if points is None or not len(points):
            return
        index = int(points[0].index())
        text = self.body_text(index)
        if text:
            self.readout.setText(text.replace("\n", "  |  "))
        self.bodyClicked.emit(index)

    def _update_sea(self, *_args) -> None:
        r = self._result
        if r is None or not self.sea_check.isChecked():
            self.sea_curve.setData([], [])
        else:
            kp = r.stations.kp_km
            self.sea_curve.setData([float(kp[0]), float(kp[-1])], [0.0, 0.0])
        if self.true_scale_check.isChecked():
            self._fit_true_scale()

    def _update_previous(self, *_args) -> None:
        prev = self._previous if self.previous_check.isChecked() else None
        if prev is None or self._result is None:
            self.prev_cable_curve.setData([], [])
            self.prev_tension_curve.setData([], [])
            return
        self.prev_cable_curve.setData(prev.stations.kp_km, -prev.cable_m)
        self.prev_tension_curve.setData(prev.stations.kp_km, prev.tension_kn)

    def _update_rows(self, *_args) -> None:
        shown = {key: self.row_checks[key].isChecked() for key in ("tension", "slack", "slope")}
        for key, visible in shown.items():
            self.rows[key].setVisible(visible)
        # The KP axis labels sit on the lowest visible row.
        lowest = self.depth_plot
        for key, plot in (("tension", self.tension_plot), ("slack", self.slack_plot), ("slope", self.slope_plot)):
            if shown[key]:
                lowest = plot
        for plot in self._data_plots:
            show = plot is lowest
            plot.showAxis("bottom", show)
            if show:
                plot.setLabel("bottom", "KP (km)", **{"font-size": "11px"})

    def _update_legends(self, *_args) -> None:
        for caption in self.captions.values():
            caption.setVisible(self.legend_check.isChecked())

    # -- view --------------------------------------------------------------------
    def _apply_reverse(self, on: bool) -> None:
        for plot in (self.status_plot,) + self._data_plots:
            plot.invertX(bool(on))
        self.measure.refresh()

    def _apply_true_scale(self, on: bool) -> None:
        vb = self.depth_plot.vb
        if on:
            # Aspect locking and y auto-visible fight each other: the locked
            # view derives its own y span from the KP window.
            vb.setAutoVisible(y=False)
            vb.enableAutoRange(y=False)
            vb.setAspectLocked(True, ratio=TRUE_SCALE_RATIO)
            self._fit_true_scale()
        else:
            vb.setAspectLocked(False)
            vb.enableAutoRange(y=True)
            vb.setAutoVisible(y=True)
            self.fit()

    def _fit_true_scale(self) -> None:
        r = self._result
        if r is None:
            return
        lo, hi = sorted(self.visible_kp())
        kp = r.stations.kp_km
        sel = (kp >= lo) & (kp <= hi)
        depths = np.concatenate([-r.stations.obstacle_m[sel], -r.cable_m[sel]])
        depths = depths[np.isfinite(depths)]
        if not depths.size:
            return
        y_lo, y_hi = float(depths.min()), float(depths.max())
        if self.sea_check.isChecked():
            y_lo = min(y_lo, 0.0)
        if y_hi <= y_lo:
            y_hi = y_lo + 1.0
        self.depth_plot.vb.setRange(xRange=(lo, hi), yRange=(y_lo, y_hi), padding=0.02)

    def _auto_y(self) -> None:
        for plot in self._data_plots:
            if plot is self.depth_plot and self.true_scale_check.isChecked():
                continue
            plot.enableAutoRange(axis="y")

    def fit(self) -> None:
        r = self._result
        if r is None:
            return
        kp = r.stations.kp_km
        self.depth_plot.setXRange(float(kp[0]), float(kp[-1]), padding=0.01)
        if self.true_scale_check.isChecked():
            self._fit_true_scale()
        self._auto_y()

    def zoom_to(self, kp_a: float, kp_b: float) -> None:
        lo, hi = sorted((kp_a, kp_b))
        pad = max((hi - lo) * 0.75, 0.02)
        self.depth_plot.setXRange(lo - pad, hi + pad, padding=0)
        if self.true_scale_check.isChecked():
            self._fit_true_scale()
        self._auto_y()

    def visible_kp(self):
        lo, hi = self.depth_plot.viewRange()[0]
        return float(lo), float(hi)

    # -- hover / click ----------------------------------------------------------
    def _plot_for(self, key: str):
        return {"depth": self.depth_plot, "tension": self.tension_plot, "slack": self.slack_plot,
                "slope": self.slope_plot}[key]

    def _kp_at(self, key: str, pos) -> Optional[float]:
        plot = self._plot_for(key)
        if not plot.vb.sceneBoundingRect().contains(pos):
            return None
        return float(plot.vb.mapSceneToView(pos).x())

    def eventFilter(self, watched, event):  # noqa: N802 (Qt API)
        if event.type() == _LEAVE and watched in self.widgets.values():
            self._end_hover()
        return super().eventFilter(watched, event)

    def _end_hover(self) -> None:
        for line in self._vlines:
            line.hide()
        self.kpHovered.emit(math.nan)

    def _on_mouse_moved(self, key: str, pos) -> None:
        if self._result is None or self.measure.active:
            return
        kp = self._kp_at(key, pos)
        if kp is None:
            self._end_hover()
            return
        snapped = self.show_kp(kp)
        if snapped is not None:
            self.kpHovered.emit(snapped)     # the station the readout shows

    def _station(self, kp: float) -> Optional[int]:
        r = self._result
        if r is None:
            return None
        st = r.stations
        i = int(np.clip(np.searchsorted(st.kp_km, kp), 0, st.n - 1))
        if i > 0 and abs(st.kp_km[i - 1] - kp) < abs(st.kp_km[i] - kp):
            i -= 1
        return i

    def show_kp(self, kp: float) -> Optional[float]:
        """Crosshair and readout at the station nearest ``kp``; returns that station's KP."""
        i = self._station(kp)
        if i is None:
            return None
        at = float(self._result.stations.kp_km[i])
        for line in self._vlines:
            line.setPos(at)
            line.show()
        self.readout.setText(self.describe(i))
        return at

    def describe(self, i: int) -> str:
        r = self._result
        st = r.stations
        gap = float(r.gap_m[i])
        cable = st.cable_at(i)
        parts = [f"KP {st.kp_km[i]:.3f}", f"depth {-st.seabed_m[i]:.1f} m",
                 f"gap {gap:.2f} m" if gap > 0.005 else "on the seabed",
                 f"T {r.tension_kn[i]:.2f} kN (laid {r.deposit_kn[i]:.2f})"]
        span = next(((k, s) for k, s in enumerate(r.spans) if s.i0 <= i <= s.i1), None)
        if span is not None:
            k, s = span
            level = "red" if pl._span_red(s, r.criteria) else "amber"
            parts.append(f"span {k + 1} ({level}): {s.length_m:.1f} m long, up to {s.max_gap_m:.2f} m high")
        elif gap > pl.span_end_tol(r.criteria):
            parts.append("clear of the seabed, under the reporting limits")
        if math.isfinite(r.laid_pct[i]):
            parts.append(f"slack laid {r.laid_pct[i]:.2f}%, fill {r.fill_pct[i]:.2f}%, surplus {r.excess_pct[i]:.2f}%")
        parts.append(f"slope {math.degrees(math.atan(abs(st.slope[i]))):.1f}° (friction "
                     f"{math.degrees(math.atan(st.mu[i])):.0f}°, μ {st.mu[i]:.2f})")
        label = ""
        if st.body_index[i] >= 0:
            label = next((b.body.name for b in st.bodies if b.i0 <= i <= b.i1), "body")
        index = st.protection_index
        if index is not None and index[i] >= 0:
            item = st.protection[int(index[i])]
            label = (label or cable.name) + f" in {item.name} ({item.kind})"
        parts.append(label or cable.name)
        if st.soil[i]:
            parts.append(st.soil[i])
        return "  |  ".join(parts)

    def _on_mouse_clicked(self, key: str, event) -> None:
        if self._result is None or self.measure.active or event.isAccepted():
            return
        kp = self._kp_at(key, event.scenePos())
        if kp is None:
            return
        i = self._station(kp)
        kp = float(self._result.stations.kp_km[i]) if i is not None else kp
        if event.double():
            self.kpDoubleClicked.emit(kp)
        else:
            self.kpClicked.emit(kp)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.measure.handle_key(event):
            return
        super().keyPressEvent(event)
