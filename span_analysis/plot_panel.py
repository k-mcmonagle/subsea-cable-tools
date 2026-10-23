# -*- coding: utf-8 -*-
"""The Span & Slack Analysis profile: linked rows on one KP axis, in a splitter.

* status bar: the worst finding per KP (green clear, blue info, amber, red, grey no data);
* profile: sampled seabed, the seabed the cable rests on, the modelled cable,
  spans filled by level (red, amber; light grey where the cable is clear of
  the seabed but under the reporting limits), span supports (red below the
  MBR; hover for the reaction and bend radius), bodies (hover or click for
  their details, optional labels), pinned sections (a dark band, squares at
  their ends: hover for what they carry), the detail model's cable and sea
  level;
* tension: residual tension on the seabed, the lay (deposit) tension, NPTS and
  the highest tension that keeps spans within the limits;
* slack: cable laid and the cable the rest shape uses, with the RPL's planned
  slack, as bottom slack (% over the seabed: 0 where the cable lies on it) or
  % over the route as an RPL gives it (the seabed then sits at the fill); the
  surplus and the shortfall (where the cable bridges the seabed) are shaded,
  and the loop-risk threshold drawn;
* slope and friction: the seabed slope against the friction angle ``atan mu``.

Drag the splitter handles to share the height between rows. *Plots* shows or
hides each row and the profile's overlays (sea level, previous run, body
labels), reverses KP and restores the default view; *Tooltip* chooses what a
tooltip at the cursor shows. Items are built once and updated with ``setData``
(removing pyqtgraph items is slow on long routes and noisy under QGIS 3.40),
and drawn antialiased. Hover moves a shared crosshair and reports every value
at that KP; the dialog links it to the map. The status bar outlines the
stretch under the cursor; a click goes to its worst finding.
"""

from __future__ import annotations

import html
import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtGui as _QtGui   # QActionGroup: QtWidgets in Qt 5, QtGui in Qt 6

from qgis.PyQt.QtCore import QEvent, QRectF, QSettings, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QPainterPath
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QGraphicsPathItem,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..cable_seabed import planning as pl
from ..cable_seabed.core import ERROR, INFO, WARNING, runs
from ..maptools.profile_measure_controller import ProfileMeasureController
from ..plugin_log import log_exception
from ..plotkit.widgets import (DirectAxis, ProfileViewBox, StayOpenMenu, WrapLabel,  # noqa: F401
                               hex_colour as _hex, legend_html as _legend_html,
                               make_plot_widget as _plot_widget, menu_button, runs_path)
from . import cable_colour as cc
from . import events as ev
from .event_lane import LABEL_NAME, LABEL_NAME_KP, EventGuideItem, EventLaneItem, EventLayer, EventLeaderItem

_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
_DOT = getattr(getattr(Qt, "PenStyle", Qt), "DotLine")
_VERTICAL = getattr(getattr(Qt, "Orientation", Qt), "Vertical")
_POINTING = getattr(getattr(Qt, "CursorShape", Qt), "PointingHandCursor")
_NO_MOUSE = getattr(getattr(Qt, "WidgetAttribute", Qt), "WA_TransparentForMouseEvents")
_RICH = getattr(getattr(Qt, "TextFormat", Qt), "RichText")
_RIGHT_BUTTON = getattr(getattr(Qt, "MouseButton", Qt), "RightButton")
_SETTINGS = "SubseaCableTools/SpanAnalysis/plot"
STATUS_HEIGHT = 14
_DEFAULT_SIZES = (STATUS_HEIGHT, 440, 150, 150, 150)
ROWS = (("status", "Status bar", "The worst finding along the route: red, amber, blue (information), green (clear), "
                                 "grey (no seabed data). Hover for the findings at a KP; click to go to them."),
        ("depth", "Profile", "The seabed, the cable resting on it, spans, supports and bodies."),
        ("tension", "Tension", "Residual and lay tension, NPTS, highest tension for spans within limits."),
        ("slack", "Slack", "Cable laid and cable used by the rest shape, as bottom slack (% over the seabed) or % "
                           "over the route (choose below)."),
        ("slope", "Slope / friction", "Seabed slope against the friction angle atan(mu): steeper slopes slide."))
DATA_ROWS = ("depth", "tension", "slack", "slope")
# What the tooltip at the cursor can show: (key, menu text, on by default).
TIP_FIELDS = (("kp", "KP", True), ("depth", "Seabed depth", True), ("cable", "Cable depth", False),
              ("gap", "Gap and span", True), ("tension", "Residual tension", True), ("lay", "Lay tension", False),
              ("allowable", "Highest tension for spans within limits", False),
              ("slack", "Slack (laid, used, surplus)", False), ("slope", "Slope and friction", False),
              ("type", "Cable type, body, protection or pin", False), ("soil", "Soil", False))
LEVEL_NAMES = {-1: "No seabed data", 0: "Clear", 1: "Information", 2: "Amber", 3: "Red"}
SEVERITY_NAMES = {ERROR: "Red", WARNING: "Amber", INFO: "Info"}
_TIP_STYLE = ("QLabel { background: rgba(255, 255, 255, 240); color: #222; border: 1px solid #9e9e9e; "
              "border-radius: 3px; padding: 3px 6px; font-size: 11px; }")
LEVEL_COLOURS = {-1: (200, 200, 200), 0: (76, 175, 80), 1: (31, 119, 180), 2: (255, 152, 0), 3: (211, 47, 47)}
SEABED = (121, 85, 72)
CABLE = (25, 70, 160)
RED_FILL = (211, 47, 47, 150)
AMBER_FILL = (255, 152, 0, 140)
CLEAR_FILL = (120, 120, 120, 55)
INDICATIVE_FILL = (31, 119, 180, 70)   # spans shorter than the seabed data resolves: light blue
SURPLUS_FILL = (76, 175, 80, 90)
SHORT_FILL = (121, 85, 72, 70)       # cable shorter than the seabed under it (it bridges: spans)
LOOP_LINE = (255, 152, 0)            # the loop-risk threshold (an amber finding above it)
DETAIL = (123, 31, 162)
PROTECTION = (96, 125, 139, 150)
HELD = (78, 52, 46, 170)             # held (buried or pinned) sections
PREVIOUS = (150, 150, 150)
SEA = "#17becf"
_LEAVE = getattr(getattr(QEvent, "Type", QEvent), "Leave")
TRUE_SCALE_RATIO = 1000.0   # KP in km, depth in m


_LEGENDS = {
    "depth": [("dash", SEA, "Sea level"), ("solid", (200, 175, 165), "Seabed (sampled)"), ("solid", SEABED, "Seabed"),
              ("solid", CABLE, "Cable (rest shape)"), ("fill", PROTECTION, "Protection"),
              ("fill", HELD, "Pinned (buried or anchored)"),
              ("dash", PREVIOUS, "Previous run"),
              ("solid", DETAIL, "Cable with EI (detail)"), ("fill", RED_FILL, "Span (red)"),
              ("fill", AMBER_FILL, "Span (amber)"), ("fill", INDICATIVE_FILL, "Span below the data's resolution"),
              ("fill", (170, 170, 170), "Clear of seabed, under the limits"),
              ("triangle", (60, 60, 60), "Span support / end"), ("diamond", (255, 193, 7), "Body")],
    "tension": [("solid", CABLE, "Residual (on the seabed)"), ("dash", (90, 90, 90), "Lay (touchdown)"),
                ("dash", (211, 47, 47), "NPTS"), ("dot", (0, 137, 123), "Max for spans within limits"),
                ("dash", PREVIOUS, "Previous run")],
    "slack": [],   # by basis: _SLACK_LEGENDS
    "slope": [("solid", SEABED, "Seabed slope"), ("dash", (230, 81, 0), "Friction angle atan(\u03bc)")],
}
SLACK_SEABED, SLACK_ROUTE = "seabed", "route"
SLACK_BASES = ((SLACK_SEABED, "Slack over the seabed (bottom slack)",
                "0 where the cable lies on the seabed; below 0 it is shorter than the seabed under it (it bridges: "
                "spans), above 0 it has surplus."),
               (SLACK_ROUTE, "Slack over the route (as an RPL)",
                "Cable over the route's plan length, as RPL slack is given: a cable lying on the seabed sits at the "
                "fill (the seabed length over the route), not at 0."))
_SLACK_LEGENDS = {
    SLACK_SEABED: [("solid", CABLE, "Laid"), ("dash", (90, 90, 90), "Used by the cable"),
                   ("solid", SEABED, "Seabed (0)"), ("fill", SURPLUS_FILL, "Surplus"),
                   ("fill", SHORT_FILL, "Short of the seabed (bridging)"), ("dash", LOOP_LINE, "Loop-risk threshold"),
                   ("dot", (233, 30, 99), "RPL slack (plan)")],
    SLACK_ROUTE: [("solid", CABLE, "Laid"), ("dash", (90, 90, 90), "Used by the cable"),
                  ("solid", SEABED, "Fill (seabed)"), ("fill", SURPLUS_FILL, "Surplus"),
                  ("fill", SHORT_FILL, "Short of the seabed (bridging)"), ("dash", LOOP_LINE, "Loop-risk threshold"),
                  ("dot", (233, 30, 99), "RPL slack (plan)")],
}


def _tip(x, y, data):
    return data if isinstance(data, str) else f"KP {x:.3f}"


def _single_shot(parent, msec: int, slot) -> QTimer:
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(msec)
    timer.timeout.connect(slot)
    return timer


def _finite_tension(values: np.ndarray) -> np.ndarray:
    """Tensions with the pull-taut search limit (no finite tension) left out."""
    values = np.asarray(values, dtype=float)
    return np.where(values >= 0.999 * pl.TAUT_CAP_KN, np.nan, values)


def _tension_text(value: float) -> str:
    return "no finite tension (cable short of the seabed)" if pl.unbounded(value) else f"{value:.2f} kN"


def body_placement_lines(body: pl.Body, kp_km: float) -> List[str]:
    """How a body's KP was found, and how far it moves with the slack (fitted bodies)."""
    if not body.placement:
        return ["KP as entered"]
    lines = [body.placement]
    if math.isfinite(body.anchor_kp_km):
        per_tenth = abs(kp_km - body.anchor_kp_km)   # km from the anchor -> m per 0.1% of slack
        lines.append(f"Laid with 0.1% more or less slack than planned between the anchor and here, it lands "
                     f"about {per_tenth:,.1f} m away")
    return lines


def _radius_text(radius: float) -> str:
    if not (math.isfinite(radius) or radius == math.inf):
        return "unknown (no EI)"
    return "over 1000 m" if radius > 1000 else f"{radius:.1f} m"


def _setting(key: str, default: bool) -> bool:
    try:
        return bool(QSettings().value(f"{_SETTINGS}/{key}", default, type=bool))
    except Exception:
        return default


def _setting_text(key: str, default: str) -> str:
    try:
        return str(QSettings().value(f"{_SETTINGS}/{key}", default, type=str))
    except Exception:
        return default


class ProfilePlot(QWidget):
    kpHovered = pyqtSignal(float)    # nan: the cursor left the profile
    kpClicked = pyqtSignal(float)
    kpDoubleClicked = pyqtSignal(float)
    bodyClicked = pyqtSignal(int)
    statusClicked = pyqtSignal(float, float, float)   # the status stretch (KP from, KP to) and the KP clicked
    eventClicked = pyqtSignal(object)                 # a ProfileEvent clicked in the lane
    eventActivated = pyqtSignal(object)               # ... double-clicked (open the comment, edit the marker)
    eventsSettingsChanged = pyqtSignal()              # Events menu: what shows, or the check, changed
    eventsRefreshRequested = pyqtSignal()
    markerRequested = pyqtSignal(float, object)       # add a marker at KP (to KP or None)
    markerEditRequested = pyqtSignal(int)
    markerDeleteRequested = pyqtSignal(int)
    commentRequested = pyqtSignal(float, object)      # add a Route Comment at KP (to KP or None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.persist = True
        self._holding_view = False      # a redraw that keeps the view (no re-fit)
        self._stretching = False        # true scale ended by a right-button drag (keep the view)
        self._result: Optional[pl.PlanningResult] = None
        self._previous: Optional[pl.PlanningResult] = None
        self._all_events: List[ev.ProfileEvent] = []
        self._shown_events: List[ev.ProfileEvent] = []
        self._events_outside = 0
        self.can_comment = lambda: False    # set by the window: Route Comments can take a comment on this route
        self._colour_runs: List[Tuple[int, int, int]] = []
        self._colour_legend: List[Tuple[Tuple[int, int, int], str]] = []
        self._colour_classes: Optional[np.ndarray] = None
        self._colour_note = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        fit = QPushButton("Fit")
        fit.setToolTip("Show the whole profile (as the A at the bottom left of a plot does).")
        fit.clicked.connect(self.fit)
        bar.addWidget(fit)
        self.true_scale_check = QCheckBox("True scale (1:1)")
        self.true_scale_check.setToolTip("Lock the depth and KP axes to the same scale (1 km along the route = "
                                         "1000 m of depth), so slopes and spans show as they really are. Panning "
                                         "and zooming keep it; stretching an axis with a right-button drag turns "
                                         "it off.")
        self.true_scale_check.setChecked(_setting("true_scale", False))
        self.true_scale_check.toggled.connect(lambda on: self._remember("true_scale", on))
        bar.addWidget(self.true_scale_check)

        # Plots: a check box per row, the profile's overlays, then the default view. The toggles
        # are checkable actions (isChecked / setChecked / toggled, as check boxes).
        plots = StayOpenMenu(self)
        plots.setToolTipsVisible(True)
        self.row_checks = {}
        for key, text, tip in ROWS:
            action = self._toggle(plots, text, tip, f"row_{key}", True)
            action.toggled.connect(self._update_rows)
            self.row_checks[key] = action
        plots.addSeparator()
        self.sea_check = self._toggle(plots, "Sea level (0 m)", "The water surface as a dashed line at 0 m. While "
                                      "shown, the full view includes the surface; zoom in for the seabed detail.",
                                      "sea_level", True)
        self.previous_check = self._toggle(plots, "Previous run", "The previous run's cable and tension (dashed "
                                           "grey), to see the effect of a change.", None, True)
        self.legend_check = self._toggle(plots, "Legends", "The key above each plot.", "legends", True)
        self.reverse_check = self._toggle(plots, "Reverse KP", "KP increasing from right to left (e.g. to match "
                                          "the lay direction).", "reverse_kp", False)
        plots.addSeparator()
        basis = _setting_text("slack_basis", SLACK_SEABED)
        self.slack_basis_actions: Dict[str, object] = {}
        self._slack_group = _QtGui.QActionGroup(self)
        self._slack_group.setExclusive(True)
        for key, text, tip in SLACK_BASES:
            action = plots.addAction(text)
            action.setCheckable(True)
            action.setToolTip(tip)
            action.setChecked(key == basis)
            self._slack_group.addAction(action)
            self.slack_basis_actions[key] = action
        if not any(a.isChecked() for a in self.slack_basis_actions.values()):
            self.slack_basis_actions[SLACK_SEABED].setChecked(True)
        plots.addSeparator()
        colour = plots.addMenu("Colour the cable by")
        colour.setToolTipsVisible(True)
        chosen = _setting_text("cable_colour", cc.PLAIN)
        self.colour_actions: Dict[str, object] = {}
        self._colour_group = _QtGui.QActionGroup(self)
        self._colour_group.setExclusive(True)
        for key, text, tip in cc.MODES:
            action = colour.addAction(text)
            action.setCheckable(True)
            action.setToolTip(tip)
            action.setChecked(key == chosen)
            self._colour_group.addAction(action)
            self.colour_actions[key] = action
        if not any(a.isChecked() for a in self.colour_actions.values()):
            self.colour_actions[cc.PLAIN].setChecked(True)
        plots.addSeparator()
        plots.addAction("Default view", self.default_view).setToolTip("Every plot at its default height, and the "
                                                                      "whole profile.")
        bar.addWidget(menu_button("Plots", "Show or hide each plot, and what the profile shows.", plots))
        bar.addWidget(menu_button("Events", "What shows on the events lane above the profile: RPL events, what is "
                                  "in the model, Route Comments, the burial plan and your markers.",
                                  self._build_events_menu()))

        tips = StayOpenMenu(self)
        tips.setToolTipsVisible(True)
        self.tip_check = self._toggle(tips, "Show a tooltip at the cursor", "The values at the KP under the cursor, "
                                      "beside it. The line below the plots shows them all.", "tooltip", True)
        tips.addSeparator()
        chosen = set(_setting_text("tooltip_fields", ",".join(k for k, _t, on in TIP_FIELDS if on)).split(","))
        self.tip_fields: Dict[str, object] = {}
        for key, text, _on in TIP_FIELDS:
            action = tips.addAction(text)
            action.setCheckable(True)
            action.setChecked(key in chosen)
            action.toggled.connect(self._remember_tip_fields)
            self.tip_fields[key] = action
        bar.addWidget(menu_button("Tooltip", "Turn the tooltip at the cursor on or off, and choose what it shows.",
                                   tips))
        bar.addSpacing(12)

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
        self.previous_check.toggled.connect(self._update_previous)

        self.splitter = QSplitter(_VERTICAL)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)
        self.widgets = {}
        self.rows = {}
        self.captions = {}
        for key in ("status", "depth", "tension", "slack", "slope"):
            widget = _plot_widget()
            widget.setBackground("w")
            widget.setAntialiasing(True)   # fills, markers and text (curves set their own)
            self.widgets[key] = widget
            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)
            if key != "status":
                # The legend sits above the plot: it never hides data or overlaps its own text.
                caption = WrapLabel(_legend_html(_LEGENDS[key]))
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
        self.rows["status"].setFixedHeight(STATUS_HEIGHT)
        self.widgets["status"].viewport().setCursor(_POINTING)
        self.widgets["depth"].setMinimumHeight(100)
        for key in ("tension", "slack", "slope"):
            self.widgets[key].setMinimumHeight(60)
        for index, stretch in enumerate((0, 5, 2, 2, 2)):
            self.splitter.setStretchFactor(index, stretch)
        self.splitter.setSizes(list(_DEFAULT_SIZES))
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

        # The events lane: in the profile's row, between its legend and the plot, so each label's
        # leader runs on down into the profile.
        self.lane_widget = _plot_widget()
        self.lane_widget.setBackground("w")
        self.lane_widget.setAntialiasing(True)
        self.lane_plot = self.lane_widget.getPlotItem()
        lane_axis = self.lane_plot.getAxis("left")
        lane_axis.setWidth(64)
        lane_axis.setStyle(showValues=False)
        lane_axis.setPen(pg.mkPen(None))
        lane_axis.setTicks([[], []])
        self.lane_plot.hideAxis("bottom")
        self.lane_plot.setMouseEnabled(x=True, y=False)
        self.lane_plot.setYRange(0, 1, padding=0)
        self.lane_plot.hideButtons()
        self.lane_plot.layout.setContentsMargins(1, 1, 1, 0)
        self.depth_plot.layout.setContentsMargins(1, 0, 1, 1)
        self.lane_widget.viewport().setCursor(_POINTING)
        self.rows["depth"].layout().insertWidget(1, self.lane_widget)
        self.event_layer = EventLayer(self.lane_plot.vb, self)
        self.lane_item = EventLaneItem(self.event_layer)
        self.lane_plot.addItem(self.lane_item, ignoreBounds=True)
        self.leader_item = EventLeaderItem(self.event_layer)
        self.leader_item.setZValue(15)
        self.depth_plot.addItem(self.leader_item, ignoreBounds=True)
        self.guide_items = []
        for plot in (self.tension_plot, self.slack_plot, self.slope_plot):
            guide = EventGuideItem(self.event_layer)
            plot.addItem(guide, ignoreBounds=True)
            self.guide_items.append(guide)
        self.lane_widget.hide()
        self._data_plots = (self.depth_plot, self.tension_plot, self.slack_plot, self.slope_plot)
        for plot in self._data_plots:
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getAxis("left").setWidth(64)
            plot.setAutoVisible(y=True)
            # The A at the bottom left (shown on hover once zoomed) fits the whole profile, every row.
            try:
                plot.autoBtn.clicked.disconnect(plot.autoBtnClicked)
            except (TypeError, RuntimeError):
                pass
            plot.autoBtn.clicked.connect(lambda *_: self.fit())
            plot.autoBtn.setToolTip("Show the whole profile")
        self.depth_plot.invertY(True)
        small = {"font-size": "11px"}   # pixels: point sizes scale with the screen DPI under Qt5
        self.depth_plot.setLabel("left", "Depth (m)", **small)
        self.tension_plot.setLabel("left", "T (kN)", **small)
        self.slack_plot.setLabel("left", "Slack (%)", **small)
        self.slope_plot.setLabel("left", "θ (°)", **small)
        for plot in (self.status_plot, self.lane_plot) + self._data_plots[1:]:
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
        self.status_hover = QGraphicsPathItem()      # the stretch under the cursor
        self.status_hover.setPen(pg.mkPen((20, 20, 20), width=1.5))
        self.status_hover.setZValue(10)
        self.status_plot.addItem(self.status_hover, ignoreBounds=True)
        self._status_runs: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]] = None
        self._status_tol_km = 0.0

        def curve(plot, colour, name=None, width=1.5, style=None, fast=True):
            pen = pg.mkPen(colour, width=width) if style is None else pg.mkPen(colour, width=width, style=style)
            # Antialiased (smooth) lines. Long routes are thinned to one min / max pair per pixel
            # (peak): the same picture, and antialiasing stays quick on dense, noisy curves.
            item = pg.PlotDataItem([], [], pen=pen, name=name, connect="finite", antialias=True,
                                   autoDownsampleFactor=1.0)
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
        self.held_curve = curve(self.depth_plot, HELD, "Pinned (buried or anchored)", 5, fast=False)
        self.hold_ends = pg.ScatterPlotItem([], [], size=9, symbol="s", pen=pg.mkPen((30, 30, 30)),
                                            brush=pg.mkBrush(*HELD[:3]), hoverable=True, tip=_tip,
                                            name="Pinned ends")
        self.hold_ends.setZValue(19)
        self.depth_plot.addItem(self.hold_ends)
        self.cable_curve = curve(self.depth_plot, CABLE, "Cable (rest shape)", 1.5)
        self.prev_cable_curve = curve(self.depth_plot, PREVIOUS, "Cable (previous run)", 1, _DASH)
        self.detail_curve = curve(self.depth_plot, DETAIL, "Cable (detail, with EI)", 1.5)
        self.fills = {}
        # (A hatch would read better for indicative spans, but Qt 5 scales brush patterns with the plot's
        # KP x depth transform until they vanish: a light fill draws the same under Qt 5 and 6.)
        for key, brush in (("red", RED_FILL), ("amber", AMBER_FILL), ("clear", CLEAR_FILL),
                           ("indicative", INDICATIVE_FILL)):
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
        # The cable coloured by a quantity: one path per class over the plain cable, built for the KPs in
        # view (thinned to the pixels, peaks kept) when the view moves.
        self.colour_paths: List[QGraphicsPathItem] = []
        for _k in range(cc.MAX_CLASSES):
            item = QGraphicsPathItem()
            item.setBrush(pg.mkBrush(None))
            item.setZValue(5)
            self.depth_plot.addItem(item, ignoreBounds=True)
            self.colour_paths.append(item)
        self._colour_timer = _single_shot(self, 40, self._draw_colour_paths)
        self.depth_plot.vb.sigXRangeChanged.connect(lambda *_: self._colour_timer.start())

        self.tension_curve = curve(self.tension_plot, CABLE, "Residual (on the seabed)", 1)
        self.deposit_curve = curve(self.tension_plot, (90, 90, 90), "Lay (touchdown)", 1, _DASH)
        self.npts_curve = curve(self.tension_plot, (211, 47, 47), "NPTS", 1, _DASH)
        self.allowable_curve = curve(self.tension_plot, (0, 137, 123), "Max for spans within limits", 1, _DOT)
        self.prev_tension_curve = curve(self.tension_plot, PREVIOUS, "Residual (previous run)", 1, _DASH)

        self.used_curve = curve(self.slack_plot, (90, 90, 90), "Used by the cable", 1, _DASH)
        self.laid_curve = curve(self.slack_plot, CABLE, "Laid", 1)
        self.fill_curve = curve(self.slack_plot, SEABED, "Fill (seabed)", 1)
        self.plan_curve = curve(self.slack_plot, (233, 30, 99), "RPL slack (plan)", 1, _DOT, fast=False)
        self.surplus = pg.FillBetweenItem(self.used_curve, self.laid_curve, brush=pg.mkBrush(*SURPLUS_FILL))
        self.slack_plot.addItem(self.surplus)
        # Where the cable uses less than the seabed under it (it bridges: spans). Only there: part-way along
        # a span a short window can hold more cable than the flat floor under it.
        self.short_fill = QGraphicsPathItem()
        self.short_fill.setBrush(pg.mkBrush(*SHORT_FILL))
        self.short_fill.setPen(pg.mkPen(None))
        self.slack_plot.addItem(self.short_fill, ignoreBounds=True)
        self.slack_plot.addItem(pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen((160, 160, 160), width=1)))
        # The loop-risk threshold: the surplus at which the loop check flags (at zero tension). Kept out of
        # the auto range, so it shows only where the slack comes near it.
        self.loop_curve = pg.PlotDataItem([], [], pen=pg.mkPen(LOOP_LINE, width=1, style=_DASH), connect="finite",
                                          antialias=True)
        self.slack_plot.addItem(self.loop_curve, ignoreBounds=True)

        self.slope_curve = curve(self.slope_plot, SEABED, "Seabed slope", 1)
        self.friction_curve = curve(self.slope_plot, (230, 81, 0), "Friction angle", 1, _DASH)

        self._vlines = []
        for plot in (self.status_plot,) + self._data_plots:
            # On the coloured status bar a solid dark line shows; on the plots a light dashed one.
            pen = (pg.mkPen((20, 20, 20), width=1.5) if plot is self.status_plot
                   else pg.mkPen((120, 120, 120), width=1, style=_DASH))
            line = pg.InfiniteLine(angle=90, movable=False, pen=pen)
            line.hide()
            plot.addItem(line, ignoreBounds=True)
            self._vlines.append(line)

        self.readout = WrapLabel("Run the analysis to see the cable on the seabed.")
        self.readout.setTextInteractionFlags(getattr(getattr(Qt, "TextInteractionFlag", Qt), "TextSelectableByMouse"))
        layout.addWidget(self.readout)
        layout.addWidget(self.measure.table)
        self.measure.table.hide()
        self.measure.measurementsChanged.connect(lambda: self.measure.table.setVisible(bool(self.measure.measurements)))
        self.measure.attach(self.depth_plot)
        for key in DATA_ROWS:
            scene = self.widgets[key].scene()
            scene.sigMouseMoved.connect(lambda pos, k=key: self._on_mouse_moved(k, pos))
            scene.sigMouseClicked.connect(lambda event, k=key: self._on_mouse_clicked(k, event))
            self.widgets[key].installEventFilter(self)
        status_scene = self.widgets["status"].scene()
        status_scene.sigMouseMoved.connect(self._on_status_moved)
        status_scene.sigMouseClicked.connect(self._on_status_clicked)
        self.widgets["status"].installEventFilter(self)
        # The tooltip: a label over the plots, beside the cursor (mouse events pass through it).
        self.tip = QLabel(self)
        self.tip.setTextFormat(_RICH)
        self.tip.setAttribute(_NO_MOUSE)
        self.tip.setStyleSheet(_TIP_STYLE)
        self.tip.hide()
        self.tip_check.toggled.connect(lambda on: on or self.tip.hide())
        self.true_scale_check.toggled.connect(self._apply_true_scale)
        self.depth_plot.vb.stretchStarted.connect(self._leave_true_scale)
        self.sea_check.toggled.connect(self._update_sea)
        self.reverse_check.toggled.connect(self._apply_reverse)
        self.legend_check.toggled.connect(self._update_legends)
        for action in self.slack_basis_actions.values():
            action.toggled.connect(lambda on: on and self._on_slack_basis())
        for action in self.colour_actions.values():
            action.toggled.connect(lambda on: on and self._on_colour_mode())
        lane_scene = self.lane_widget.scene()
        lane_scene.sigMouseMoved.connect(self._on_lane_moved)
        lane_scene.sigMouseClicked.connect(self._on_lane_clicked)
        self.lane_widget.installEventFilter(self)
        self.lane_plot.vb.extra_menu = self._context_actions
        self.depth_plot.vb.extra_menu = self._context_actions
        self.event_layer.changed.connect(self._update_lane_height)
        self._update_slack_caption()
        self._update_legends()
        self._update_rows()
        self._apply_reverse(self.reverse_check.isChecked())
        if self.true_scale_check.isChecked():
            # Restored ticked from the last session: lock the axes too (the box was
            # ticked over a plot that was not to scale).
            self._apply_true_scale(True)

    # -- settings --------------------------------------------------------------
    def _toggle(self, menu: QMenu, text: str, tip: str, key: Optional[str], default: bool):
        """A checkable menu action, remembered under ``key`` (None: not remembered)."""
        action = menu.addAction(text)
        action.setCheckable(True)
        action.setToolTip(tip)
        action.setChecked(_setting(key, default) if key else default)
        if key:
            action.toggled.connect(lambda on, k=key: self._remember(k, on))
        return action

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

    def _remember_tip_fields(self, *_args) -> None:
        if self.persist:
            QSettings().setValue(f"{_SETTINGS}/tooltip_fields", ",".join(self.tip_keys()))

    def tip_keys(self) -> List[str]:
        return [key for key, action in self.tip_fields.items() if action.isChecked()]

    def default_view(self) -> None:
        """Every plot at its default height, and the whole profile."""
        for action in self.row_checks.values():
            action.setChecked(True)
        self.splitter.setSizes(list(_DEFAULT_SIZES))
        self._remember("splitter", None)
        self.fit()

    # -- data ------------------------------------------------------------------
    def set_result(self, result: Optional[pl.PlanningResult], previous: Optional[pl.PlanningResult] = None,
                   keep_view: bool = False) -> None:
        """Draw a result. The view is kept unless the result covers other KPs.

        Kept means held exactly: the profile's KP and depth ranges are put back
        after drawing, so the seabed stays still and only the cable moves
        (an auto-ranged depth axis, or true scale, would otherwise re-fit to
        the new cable). The other rows' value axes still follow their data.
        """
        old = self._result
        same_kps = (old is not None and result is not None
                    and abs(float(old.stations.kp_km[0]) - float(result.stations.kp_km[0])) < 1e-9
                    and abs(float(old.stations.kp_km[-1]) - float(result.stations.kp_km[-1])) < 1e-9)
        keep = result is not None and old is not None and (keep_view or same_kps)
        view = self.depth_plot.vb.viewRange() if keep else None
        self._result = result
        self._previous = previous
        self._holding_view = keep
        try:
            self._draw()
        except Exception:
            log_exception("Span & Slack Analysis: drawing the profile failed")
        finally:
            self._holding_view = False
        if view is not None:
            self.depth_plot.vb.setRange(xRange=view[0], yRange=view[1], padding=0)
        else:
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
                         self.protection_curve, self.held_curve,
                         self.tension_curve, self.deposit_curve, self.npts_curve, self.allowable_curve,
                         self.used_curve, self.laid_curve, self.fill_curve, self.plan_curve, self.slope_curve,
                         self.friction_curve, self.loop_curve):
                item.setData([], [])
            for item in list(self.fills.values()) + [self.short_fill]:
                item.setPath(QPainterPath())
            self._update_slack_caption()
            self.supports.setData([], [])
            self.bodies.setData([], [])
            self.hold_ends.setData([], [])
            self._all_events = []
            self._apply_events()
            self._update_colour()
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
        self._draw_holds(r, kp, cable_depth)
        self.detail_curve.setData([], [])
        self._draw_fills(r, kp, cable_depth, floor)
        self._draw_supports(r)
        self._draw_bodies(r)
        self._update_sea()
        # A cable laid shorter than the seabed has no finite tension (the pull-taut search limit is not one):
        # leave a gap, which the findings and the readout explain.
        self.tension_curve.setData(kp, _finite_tension(r.tension_kn))
        self.deposit_curve.setData(kp, _finite_tension(r.deposit_kn))
        self.npts_curve.setData(kp, st.npts_kn)
        allowable = pl.allowable_shown(r)
        if allowable is not None:
            self.allowable_curve.setData(kp, np.where(allowable < 99.9, allowable, np.nan))
        else:
            self.allowable_curve.setData([], [])
        self._draw_slack(r)
        self.slope_curve.setData(kp, np.degrees(np.arctan(np.abs(st.slope))))
        self.friction_curve.setData(kp, np.degrees(np.arctan(st.mu)))
        self._draw_status(r)
        self._apply_events()
        self._update_colour()
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
        levels = {"red": [], "amber": [], "indicative": []}
        for k, span in enumerate(r.spans):
            levels[pl.span_level(r, k)].append((span.i0, span.i1))
        with np.errstate(invalid="ignore"):
            free = np.isfinite(r.gap_m) & (r.gap_m > pl.span_end_tol(criteria))
        reported = np.zeros(len(kp), dtype=bool)
        for span in r.spans:
            reported[span.i0:span.i1 + 1] = True
        clear = [(max(a - 1, 0), min(b + 1, len(kp) - 1)) for a, b in runs(free & ~reported)]
        for key, extents in levels.items():
            self.fills[key].setPath(runs_path(kp, cable_depth, floor, extents))
        self.fills["clear"].setPath(runs_path(kp, cable_depth, floor, clear))

    def slack_basis(self) -> str:
        return next((key for key, action in self.slack_basis_actions.items() if action.isChecked()), SLACK_SEABED)

    def slack_series(self, r: pl.PlanningResult) -> Dict[str, np.ndarray]:
        """The slack row's curves on the chosen basis: ``laid``, ``used``, ``seabed`` (the
        seabed's own line: 0, or the fill over the route), ``surplus`` and ``loop`` (threshold)."""
        n = r.stations.n
        loop = float(r.criteria.loop_excess_pct)
        if self.slack_basis() == SLACK_ROUTE or r.used_bottom_pct is None:
            seabed = r.fill_pct
            out = {"laid": r.laid_pct, "used": r.shape_pct, "seabed": seabed, "surplus": r.excess_pct,
                   # The loop check compares the surplus over the route with the threshold.
                   "loop": r.shape_pct + loop if loop > 0 else np.full(n, np.nan)}
        else:
            seabed = np.where(np.isfinite(r.used_bottom_pct), 0.0, np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                loop_here = loop / r.seabed_over_route   # the same surplus, over the seabed length
            out = {"laid": r.bottom_slack_pct, "used": r.used_bottom_pct, "seabed": seabed,
                   "surplus": r.surplus_bottom_pct,
                   "loop": r.used_bottom_pct + loop_here if loop > 0 else np.full(n, np.nan)}
        return out

    def _draw_slack(self, r: pl.PlanningResult) -> None:
        series = self.slack_series(r)
        kp = r.stations.kp_km
        self.used_curve.setData(kp, series["used"])
        self.laid_curve.setData(kp, series["laid"])
        self.fill_curve.setData(kp, series["seabed"])
        self.loop_curve.setData(kp, series["loop"])
        with np.errstate(invalid="ignore"):
            short = np.isfinite(series["used"]) & np.isfinite(series["seabed"]) \
                & (series["used"] < series["seabed"] - 1e-6)
        extents = [(max(a - 1, 0), min(b + 1, len(kp) - 1)) for a, b in runs(short)]
        self.short_fill.setPath(runs_path(kp, series["seabed"], np.fmin(series["used"], series["seabed"]), extents))
        self._draw_plan(r)
        self._update_slack_caption()

    def _draw_plan(self, r: pl.PlanningResult) -> None:
        legs = list(r.control.legs or [])
        if not legs:
            self.plan_curve.setData([], [])
            return
        kp = r.stations.kp_km
        lo, hi = float(kp[0]), float(kp[-1])
        if self.slack_basis() == SLACK_ROUTE or r.seabed_over_route is None:
            xs, ys = [], []
            for k0, k1, slack in sorted(legs):
                a, b = max(min(k0, k1), lo), min(max(k0, k1), hi)
                if b <= a:
                    continue
                xs.extend([a, b, b])
                ys.extend([slack, slack, np.nan])
            self.plan_curve.setData(np.array(xs), np.array(ys))
            return
        # Over the seabed: the plan's cable over the seabed under it, (1 + s) route / seabed - 1.
        plan = np.full(len(kp), np.nan)
        for k0, k1, slack in sorted(legs):
            a, b = sorted((k0, k1))
            sel = (kp >= a) & (kp < b)
            with np.errstate(invalid="ignore", divide="ignore"):
                plan[sel] = ((1.0 + slack / 100.0) / r.seabed_over_route[sel] - 1.0) * 100.0
        self.plan_curve.setData(kp, plan)

    def _update_slack_caption(self) -> None:
        basis = self.slack_basis()
        caption = self.captions.get("slack")
        if caption is None:
            return
        # The basis leads the legend (a longer axis title does not fit a short row).
        text = (f"<b>{'Bottom slack, over the seabed' if basis == SLACK_SEABED else 'Slack over the route'}:</b>"
                "&nbsp;&nbsp;" + _legend_html(_SLACK_LEGENDS[basis]))
        r = self._result
        if r is not None:
            window = max(float(r.criteria.slack_window_m), 2.0 * r.stations.spacing_m)
            text += (f"<br><span style='color:#666'>Over a {window:,.0f} m running window; lengths on the seabed "
                     f"smoothed over {r.stations.smoothing_m:.0f} m (the cable's conformity length)."
                     + (" Bottom slack: 0 = lying on the seabed." if basis == SLACK_SEABED else
                        " A cable lying on the seabed sits at the fill.") + "</span>")
        caption.setText(text)

    def _on_slack_basis(self, *_args) -> None:
        key = self.slack_basis()
        if self.persist:
            try:
                QSettings().setValue(f"{_SETTINGS}/slack_basis", key)
            except Exception:
                pass
        if self._result is not None:
            self._draw_slack(self._result)
            self.slack_plot.enableAutoRange(axis="y")
        else:
            self._update_slack_caption()

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
        self._status_runs = (np.asarray(edges[starts], dtype=float), np.asarray(edges[ends], dtype=float),
                             np.asarray(level[starts], dtype=int))
        self._status_tol_km = bin_m / 2000.0
        self.status_hover.setPath(QPainterPath())

    def refresh_status(self) -> None:
        """Draw the status bar again (the findings changed)."""
        if self._result is not None:
            self._draw_status(self._result)

    def _clear_status(self) -> None:
        for item in self.status_paths.values():
            item.setPath(QPainterPath())
        self._status_runs = None
        self.status_hover.setPath(QPainterPath())

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
                     f"Tension {_tension_text(sp.tension_kn)}"]
            tips.append("\n".join(line for line in lines if line))
        self.supports.setData(np.array(kps), np.array(depths), brush=brushes, data=tips)

    def _draw_holds(self, r: pl.PlanningResult, kp: np.ndarray, cable_depth: np.ndarray) -> None:
        """Pinned sections: a band along the cable, a square at each end (one for an anchor)."""
        if r.held is None or not r.holds:
            self.held_curve.setData([], [])
            self.hold_ends.setData([], [])
            return
        self.held_curve.setData(kp, np.where(r.held, cable_depth, np.nan))
        kps, depths, tips = [], [], []
        for hold in r.holds:
            where = (f"at KP {hold.kp_start:.3f}" if hold.point
                     else f"KP {hold.kp_start:.3f} to {hold.kp_end:.3f}")
            text = f"{hold.name}: pinned (buried or anchored), {where}\n" + ev.hold_text(hold)
            for i in sorted({hold.i0, hold.i1}):
                kps.append(float(kp[i]))
                depths.append(float(cable_depth[i]))
                tips.append(text)
        self.hold_ends.setData(np.array(kps), np.array(depths), data=tips)

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
                 where, f"Tension {_tension_text(b.tension_kn)}"]
        lines.extend(body_placement_lines(body, b.kp_km))
        return "\n".join(lines)

    def _draw_bodies(self, r: pl.PlanningResult) -> None:
        if not r.bodies:
            self.bodies.setData([], [])
            return
        st = r.stations
        kps, depths, tips = [], [], []
        for k, b in enumerate(r.bodies):
            i = int(np.clip(np.searchsorted(st.kp_km, b.kp_km), 0, st.n - 1))
            kps.append(b.kp_km)
            depths.append(float(-r.cable_m[i]))
            tips.append(self.body_text(k))
        self.bodies.setData(np.array(kps), np.array(depths), data=tips)

    # -- events ------------------------------------------------------------------------------
    def _build_events_menu(self) -> QMenu:
        menu = StayOpenMenu(self)
        menu.setToolTipsVisible(True)
        menu.addSection("Show")
        self.event_sources: Dict[str, object] = {}
        for key, text, tip, on in ev.SOURCES:
            self.event_sources[key] = self._toggle(menu, text, tip, f"events_src_{key}", on)
        menu.addSection("RPL and burial events")
        self.event_groups: Dict[str, object] = {}
        for key, text, on in ev.GROUPS:
            self.event_groups[key] = self._toggle(menu, text, "", f"events_grp_{key}", on)
        self.closed_comments_check = self._toggle(menu, "Closed comments", "Show Route Comments with a closed status "
                                                  "(in grey).", "events_closed", False)
        menu.addSection("Labels")
        mode = _setting_text("events_label", LABEL_NAME)
        self._label_group = _QtGui.QActionGroup(self)
        self.label_mode_actions: Dict[str, object] = {}
        for key, text in ((LABEL_NAME, "Name"), (LABEL_NAME_KP, "Name and KP")):
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(key == mode)
            self._label_group.addAction(action)
            self.label_mode_actions[key] = action
        if not any(a.isChecked() for a in self.label_mode_actions.values()):
            self.label_mode_actions[LABEL_NAME].setChecked(True)
        rows = menu.addMenu("Label rows")
        tiers = _setting_text("events_rows", "2")
        self._rows_group = _QtGui.QActionGroup(self)
        self.label_row_actions: Dict[int, object] = {}
        for count in (1, 2, 3, 4):
            action = rows.addAction(f"{count} row" + ("s" if count > 1 else ""))
            action.setCheckable(True)
            action.setChecked(str(count) == tiers)
            self._rows_group.addAction(action)
            self.label_row_actions[count] = action
        if not any(a.isChecked() for a in self.label_row_actions.values()):
            self.label_row_actions[2].setChecked(True)
        self.leaders_check = self._toggle(menu, "Leaders down to the cable", "Continue each label's leader down the "
                                          "profile to the cable, with a mark there.", "events_leaders", True)
        self.guides_check = self._toggle(menu, "Lines through the other plots", "Faint lines at each labelled event "
                                         "on the tension, slack and slope plots.", "events_guides", False)
        menu.addSeparator()
        self.event_check_action = self._toggle(
            menu, "Check RPL bodies and crossings against the model",
            f"An information finding where the RPL lists a repeater, joint, BU or equaliser with no body in the "
            f"model within {ev.MATCH_M:g} m, or the RPL or burial plan has a crossing with no seabed feature.",
            "events_check", True)
        self.burial_menu = menu.addMenu("Burial plan")
        self.burial_menu.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Add a marker in view…", lambda: self.markerRequested.emit(self._view_centre(), None))
        menu.addAction("Refresh events", self.eventsRefreshRequested.emit)
        for action in (list(self.event_sources.values()) + list(self.event_groups.values())
                       + [self.closed_comments_check, self.event_check_action]):
            action.toggled.connect(self._on_events_setting)
        for action in list(self.label_mode_actions.values()) + list(self.label_row_actions.values()):
            action.toggled.connect(lambda on: on and self._on_label_style())
        self.leaders_check.toggled.connect(self._update_event_items)
        self.guides_check.toggled.connect(self._update_event_items)
        return menu

    def _view_centre(self) -> float:
        lo, hi = self.visible_kp()
        return 0.5 * (lo + hi)

    def _remember_text(self, key: str, value: str) -> None:
        if self.persist:
            try:
                QSettings().setValue(f"{_SETTINGS}/{key}", value)
            except Exception:
                pass

    def event_settings(self) -> Dict:
        return {"sources": {k: a.isChecked() for k, a in self.event_sources.items()},
                "groups": {k: a.isChecked() for k, a in self.event_groups.items()},
                "closed": self.closed_comments_check.isChecked(), "check": self.event_check_action.isChecked()}

    def _on_events_setting(self, *_args) -> None:
        self._apply_events()
        self.eventsSettingsChanged.emit()

    def _on_label_style(self) -> None:
        mode = next(k for k, a in self.label_mode_actions.items() if a.isChecked())
        tiers = next(k for k, a in self.label_row_actions.items() if a.isChecked())
        self._remember_text("events_label", mode)
        self._remember_text("events_rows", str(tiers))
        self.event_layer.set_style(tiers=tiers, label_mode=mode)

    def set_burial_plans(self, plans: Sequence[Tuple[str, str]], current: str, choose) -> None:
        """The Burial Planner plans for this RPL (``(plan_id, name)``), the one shown and ``choose(plan_id)``."""
        self.burial_menu.clear()
        self.burial_menu.setEnabled(len(plans) > 1)
        group = _QtGui.QActionGroup(self.burial_menu)
        group.setExclusive(True)
        for plan_id, name in plans:
            action = self.burial_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(plan_id == current)
            group.addAction(action)
            action.triggered.connect(lambda _c=False, p=plan_id: choose(p))
        self.burial_menu.setTitle("Burial plan" + (f": {next((n for p, n in plans if p == current), '')}"
                                                   if plans else ""))

    def set_events(self, events: Sequence[ev.ProfileEvent]) -> None:
        """Every event collected for the route (the Events menu chooses what shows)."""
        self._all_events = list(events)
        self._apply_events()

    def shown_events(self) -> List[ev.ProfileEvent]:
        return list(self._shown_events)

    def events_outside(self) -> int:
        return self._events_outside

    def _apply_events(self) -> None:
        r = self._result
        if r is None:
            shown, outside = [], 0
        else:
            kp = r.stations.kp_km
            settings = self.event_settings()
            shown, outside = ev.visible_events(self._all_events, settings["sources"], settings["groups"],
                                               float(kp[0]), float(kp[-1]))
        self._shown_events, self._events_outside = shown, outside
        mode = next((k for k, a in self.label_mode_actions.items() if a.isChecked()), LABEL_NAME)
        tiers = next((k for k, a in self.label_row_actions.items() if a.isChecked()), 2)
        self.event_layer.tiers = tiers
        self.event_layer.label_mode = mode
        self.event_layer.set_events(shown)
        self._update_event_items()

    def _update_lane_height(self) -> None:
        show = bool(self.event_layer.events)
        self.lane_widget.setFixedHeight(self.event_layer.lane_height())
        self.lane_widget.setVisible(show)

    def _update_event_items(self, *_args) -> None:
        r = self._result
        if r is not None and self.leaders_check.isChecked():
            self.leader_item.set_cable(r.stations.kp_km, -r.cable_m)
        else:
            self.leader_item.set_cable(None, None)
        for guide in self.guide_items:
            guide.setVisible(self.guides_check.isChecked())
            guide.update()
        self.leader_item.update()

    def _lane_point(self, scene_pos) -> Optional[Tuple[float, float, float]]:
        """``(x from the lane's left, y, KP)`` of a scene position over the lane."""
        vb = self.lane_plot.vb
        rect = vb.sceneBoundingRect()
        if not rect.contains(scene_pos):
            return None
        kp = float(vb.mapSceneToView(scene_pos).x())
        return float(scene_pos.x() - rect.left()), float(scene_pos.y()), kp

    def _events_at_scene(self, scene_pos) -> List[ev.ProfileEvent]:
        point = self._lane_point(scene_pos)
        if point is None:
            return []
        x, y, _kp = point
        return self.event_layer.events_at(x, y, self.lane_plot.vb.sceneBoundingRect().top())

    def _on_lane_moved(self, pos) -> None:
        if self._result is None or self.measure.active:
            return
        point = self._lane_point(pos)
        if point is None:
            self._end_hover()
            return
        hits = self._events_at_scene(pos)
        kp = hits[0].kp if hits else point[2]
        for line in self._vlines:
            line.setPos(kp)
            line.show()
        self.kpHovered.emit(kp)
        if not hits:
            self.tip.hide()
            self.readout.setText(f"KP {point[2]:.3f}: no event here. Right-click to add a marker or a comment.")
            return
        blocks = [html.escape(e.detail).replace("\n", "<br>") for e in hits[:5]]
        if len(hits) > 5:
            blocks.append(f"and {len(hits) - 5} more")
        self.readout.setText("  |  ".join(e.detail.split("\n")[0] for e in hits[:5]))
        self._show_tip("events", pos, "<hr>".join(blocks))

    def _on_lane_clicked(self, event) -> None:
        if self._result is None or self.measure.active or event.isAccepted():
            return
        if event.button() == _RIGHT_BUTTON:
            return
        hits = self._events_at_scene(event.scenePos())
        if not hits:
            return
        if event.double():
            self.eventActivated.emit(hits[0])
        else:
            self.eventClicked.emit(hits[0])

    def _context_actions(self, kp: float, scene_pos) -> List[Tuple[Optional[str], object, bool]]:
        """Right-click on the profile or the lane: add a marker or a comment here; edit a marker under it."""
        if self._result is None:
            return []
        st = self._result.stations
        kp = float(min(max(kp, st.kp_km[0]), st.kp_km[-1]))
        lo, hi = sorted(self.visible_kp())
        lo, hi = max(lo, float(st.kp_km[0])), min(hi, float(st.kp_km[-1]))
        comment = bool(self.can_comment())
        items: List[Tuple[Optional[str], object, bool]] = [
            (f"Add a marker at KP {kp:.3f}…", lambda: self.markerRequested.emit(kp, None), True),
            (f"Add a Route Comment at KP {kp:.3f}…", lambda: self.commentRequested.emit(kp, None), comment),
            (f"Add a Route Comment over the KPs in view ({lo:.3f} to {hi:.3f})…",
             lambda: self.commentRequested.emit(lo, hi), comment)]
        near = self._marker_near(kp, scene_pos)
        if near is not None:
            index, label = near
            items += [(None, None, True), (f"Edit marker '{label}'…", lambda: self.markerEditRequested.emit(index), True),
                      (f"Delete marker '{label}'", lambda: self.markerDeleteRequested.emit(index), True)]
        return items

    def _marker_near(self, kp: float, scene_pos) -> Optional[Tuple[int, str]]:
        """The marker under a right-click (within a few pixels), as ``(index, label)``."""
        lo, hi = self.visible_kp()
        width = max(self.depth_plot.vb.sceneBoundingRect().width(), 1.0)
        tol = abs(hi - lo) / width * 6.0
        best = None
        for e in self._shown_events:
            if e.source != ev.SRC_MARKER:
                continue
            d = 0.0 if e.lo <= kp <= e.hi else min(abs(kp - e.lo), abs(kp - e.hi))
            if d <= tol and (best is None or d < best[0]):
                best = (d, e)
        if best is None:
            return None
        try:
            return int(best[1].key.split(":", 1)[1]), best[1].label
        except (IndexError, ValueError):
            return None

    # -- cable colour --------------------------------------------------------------------------
    def colour_mode(self) -> str:
        return next((k for k, a in self.colour_actions.items() if a.isChecked()), cc.PLAIN)

    def _on_colour_mode(self) -> None:
        self._remember_text("cable_colour", self.colour_mode())
        self._update_colour()

    def _update_colour(self) -> None:
        r = self._result
        mode = self.colour_mode()
        if r is None or mode == cc.PLAIN:
            self._colour_runs, self._colour_legend, self._colour_classes, self._colour_note = [], [], None, ""
        else:
            classes, legend, note = cc.cable_classes(r, mode)
            self._colour_classes, self._colour_legend, self._colour_note = classes, legend, note
            self._colour_runs = cc.class_runs(classes)
        for k, item in enumerate(self.colour_paths):
            if k < len(self._colour_legend):
                item.setPen(pg.mkPen(self._colour_legend[k][0], width=3.5))
        self._draw_colour_paths()
        self._update_depth_caption()

    def _draw_colour_paths(self) -> None:
        r = self._result
        if r is None or not self._colour_runs:
            for item in self.colour_paths:
                item.setPath(QPainterPath())
            return
        lo, hi = sorted(self.visible_kp())
        width = max(int(self.depth_plot.vb.sceneBoundingRect().width()), 50)
        paths = cc.class_paths(r.stations.kp_km, -r.cable_m, self._colour_runs, lo, hi, width)
        for k, item in enumerate(self.colour_paths):
            xs, ys, connect = paths.get(k, ([], [], []))
            item.setPath(pg.arrayToQPath(np.asarray(xs), np.asarray(ys), connect=np.asarray(connect, dtype=bool))
                         if len(xs) > 1 else QPainterPath())

    def colour_text(self, i: int) -> str:
        """The cable's colour class at station ``i`` (empty when plain)."""
        if self._colour_classes is None or not (0 <= i < len(self._colour_classes)):
            return ""
        k = int(self._colour_classes[i])
        return self._colour_legend[k][1] if 0 <= k < len(self._colour_legend) else ""

    def _update_depth_caption(self) -> None:
        caption = self.captions.get("depth")
        if caption is None:
            return
        text = _legend_html(_LEGENDS["depth"])
        if self._colour_legend:
            mode = next((t for k, t, _tip in cc.MODES if k == self.colour_mode()), "")
            swatches = "&nbsp;&nbsp;&nbsp;".join(
                f"<span style='color:{_hex(colour)}; font-weight:bold'>━━</span>&nbsp;{html.escape(label)}"
                for colour, label in self._colour_legend)
            text += f"<br><b>Cable colour: {html.escape(mode)}</b>&nbsp;&nbsp;{swatches}"
            if self._colour_note:
                text += f"<br><span style='color:#666'>{html.escape(self._colour_note)}</span>"
        caption.setText(text)

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
        if self.true_scale_check.isChecked() and not self._holding_view:
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
        if not any(self.row_checks[key].isChecked() for key in DATA_ROWS):
            self.row_checks["depth"].setChecked(True)   # at least one plot: the profile comes back
            return
        for key, action in self.row_checks.items():
            self.rows[key].setVisible(action.isChecked())
        # The KP axis labels sit on the lowest visible row.
        lowest = None
        for key in DATA_ROWS:
            if self.row_checks[key].isChecked():
                lowest = self._plot_for(key)
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
            view = vb.viewRange()
            vb.setAspectLocked(False)
            vb.setAutoVisible(y=True)
            if self._stretching:
                # The user is stretching an axis: keep the view under the mouse (unlocked,
                # pyqtgraph would go back to the range asked for before the lock adjusted it).
                vb.setRange(xRange=view[0], yRange=view[1], padding=0)
                return
            vb.enableAutoRange(y=True)
            self.fit()

    def _leave_true_scale(self) -> None:
        """A right-button drag stretches the axes apart: true scale ends, the view stays.

        Pan, the wheel, a zoom box and Fit keep the locked 1:1 scale.
        """
        if not self.true_scale_check.isChecked():
            return
        self._stretching = True
        try:
            self.true_scale_check.setChecked(False)
        finally:
            self._stretching = False

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
        return {"status": self.status_plot, "depth": self.depth_plot, "tension": self.tension_plot,
                "slack": self.slack_plot, "slope": self.slope_plot}[key]

    def _kp_at(self, key: str, pos) -> Optional[float]:
        plot = self._plot_for(key)
        if not plot.vb.sceneBoundingRect().contains(pos):
            return None
        return float(plot.vb.mapSceneToView(pos).x())

    def eventFilter(self, watched, event):  # noqa: N802 (Qt API)
        if event.type() == _LEAVE and (watched in self.widgets.values() or watched is self.lane_widget):
            self._end_hover()
        return super().eventFilter(watched, event)

    def _end_hover(self) -> None:
        for line in self._vlines:
            line.hide()
        self.tip.hide()
        self.status_hover.setPath(QPainterPath())
        self.kpHovered.emit(math.nan)

    def _on_mouse_moved(self, key: str, pos) -> None:
        if self._result is None or self.measure.active:
            return
        kp = self._kp_at(key, pos)
        if kp is None:
            self._end_hover()
            return
        self.status_hover.setPath(QPainterPath())
        snapped = self.show_kp(kp)
        if snapped is None:
            return
        self.kpHovered.emit(snapped)     # the station the readout shows
        if not self.tip_check.isChecked() or self._over_marker(key, pos):
            self.tip.hide()              # a support or body shows its own tooltip
        else:
            self._show_tip(key, pos, self.tip_html(self._station(snapped), self.tip_keys()))

    def _over_marker(self, key: str, pos) -> bool:
        if key != "depth":
            return False
        point = self.depth_plot.vb.mapSceneToView(pos)
        return any(len(item.pointsAt(point)) for item in (self.supports, self.bodies) if item.isVisible())

    def _show_tip(self, key: str, scene_pos, text: str) -> None:
        """The tooltip beside the cursor, kept inside the plots."""
        if not text:
            self.tip.hide()
            return
        widget = self.lane_widget if key == "events" else self.widgets[key]
        at = widget.viewport().mapTo(self, widget.mapFromScene(scene_pos))
        self.tip.setText(text)
        self.tip.adjustSize()
        x, y = at.x() + 16, at.y() + 16
        if x + self.tip.width() > self.width():
            x = at.x() - 12 - self.tip.width()
        if y + self.tip.height() > self.height():
            y = at.y() - 12 - self.tip.height()
        self.tip.move(max(x, 0), max(y, 0))
        self.tip.raise_()
        self.tip.show()

    # -- status bar -----------------------------------------------------------------
    def _segment_at(self, kp: float) -> Optional[Tuple[float, float, int]]:
        """The stretch of one status level under ``kp``: (KP from, KP to, level)."""
        if self._status_runs is None:
            return None
        lo, hi, level = self._status_runs
        j = int(np.searchsorted(lo, kp, side="right")) - 1
        if 0 <= j < len(lo) and lo[j] <= kp <= hi[j]:
            return float(lo[j]), float(hi[j]), int(level[j])
        return None

    def findings_at(self, kp: float) -> List:
        """The findings at ``kp`` (within half a status bin), worst first."""
        r = self._result
        if r is None:
            return []
        tol = self._status_tol_km
        rank = {ERROR: 0, WARNING: 1, INFO: 2}
        found = [f for f in r.findings if min(f.kp_start, f.kp_end) - tol <= kp <= max(f.kp_start, f.kp_end) + tol]
        return sorted(found, key=lambda f: (rank.get(f.severity, 3), min(f.kp_start, f.kp_end)))

    def _on_status_moved(self, pos) -> None:
        if self._result is None or self.measure.active:
            return
        kp = self._kp_at("status", pos)
        segment = self._segment_at(kp) if kp is not None else None
        if segment is None:
            self._end_hover()
            return
        lo, hi, level = segment
        path = QPainterPath()
        path.addRect(QRectF(lo, 0.06, hi - lo, 0.88))
        self.status_hover.setPath(path)
        for line in self._vlines:
            line.setPos(kp)
            line.show()
        findings = self.findings_at(kp)
        lines = [f"KP {kp:.3f}: {LEVEL_NAMES.get(level, '')}"]
        for f in findings[:3]:
            lines.append(f"{SEVERITY_NAMES.get(f.severity, f.severity)}: {pl.CHECK_LABELS.get(f.check_id, f.check_id)}"
                         f", KP {min(f.kp_start, f.kp_end):.3f} to {max(f.kp_start, f.kp_end):.3f}")
        if len(findings) > 3:
            lines.append(f"and {len(findings) - 3} more")
        lines.append("Click to go to " + ("the worst finding" if findings else "this stretch"))
        self.readout.setText("  |  ".join(lines))
        self._show_tip("status", pos, "<br>".join(html.escape(line) for line in lines))
        self.kpHovered.emit(kp)

    def _on_status_clicked(self, event) -> None:
        if self._result is None or self.measure.active or event.isAccepted():
            return
        kp = self._kp_at("status", event.scenePos())
        segment = self._segment_at(kp) if kp is not None else None
        if segment is not None:
            self.statusClicked.emit(segment[0], segment[1], kp)

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
        parts = [f"KP {st.kp_km[i]:.3f}", f"depth {-st.seabed_m[i]:.1f} m",
                 f"gap {gap:.2f} m" if gap > 0.005 else "on the seabed",
                 f"T {_tension_text(r.tension_kn[i])} (laid {_tension_text(r.deposit_kn[i])})"]
        span = self._span_at(i)
        if span is not None:
            k, s = span
            level = pl.span_level(r, k).replace("indicative", "indicative: below the data's resolution")
            parts.append(f"span {k + 1} ({level}): {s.length_m:.1f} m long, up to {s.max_gap_m:.2f} m high"
                         + self._short_text(k))
        elif gap > pl.span_end_tol(r.criteria):
            parts.append("clear of the seabed, under the reporting limits")
        slack = self._slack_text(i)
        if slack:
            parts.append(slack)
        parts.append(f"slope {math.degrees(math.atan(abs(st.slope[i]))):.1f}° (friction "
                     f"{math.degrees(math.atan(st.mu[i])):.0f}°, μ {st.mu[i]:.2f})")
        parts.append(self._type_label(i))
        if st.soil[i]:
            parts.append(st.soil[i])
        colour = self.colour_text(i)
        if colour:
            parts.append(f"cable colour: {colour}")
        return "  |  ".join(parts)

    def _span_at(self, i: int):
        """``(number from 0, span)`` of the reported span over station ``i``, or None."""
        return next(((k, s) for k, s in enumerate(self._result.spans) if s.i0 <= i <= s.i1), None)

    def _short_text(self, k: int) -> str:
        """``", 1.23 m shorter than the seabed under it"`` for span ``k`` (as drawn)."""
        r = self._result
        if r is None or k >= len(r.span_short_m) or not math.isfinite(r.span_short_m[k]):
            return ""
        return f", {r.span_short_m[k]:.2f} m shorter than the seabed under it"

    def _slack_text(self, i: int) -> str:
        r = self._result
        series = self.slack_series(r)
        laid, used, surplus = (float(series[key][i]) for key in ("laid", "used", "surplus"))
        if not math.isfinite(laid):
            return ""
        if self.slack_basis() == SLACK_SEABED and r.used_bottom_pct is not None:
            return f"bottom slack laid {laid:+.2f}%, used {used:+.2f}%, surplus {surplus:.2f}%"
        return (f"slack over the route laid {laid:.2f}%, used {used:.2f}%, fill {r.fill_pct[i]:.2f}%, "
                f"surplus {surplus:.2f}%")

    def _type_label(self, i: int) -> str:
        st = self._result.stations
        label = ""
        if st.body_index[i] >= 0:
            label = next((b.body.name for b in st.bodies if b.i0 <= i <= b.i1), "body")
        index = st.protection_index
        if index is not None and index[i] >= 0:
            item = st.protection[int(index[i])]
            label = (label or st.cable_at(i).name) + f" in {item.name} ({item.kind})"
        hold = self._hold_at(i)
        if hold is not None:
            label = (label or st.cable_at(i).name) + f", pinned: {hold.name}"
        return label or st.cable_at(i).name

    def _hold_at(self, i: int):
        r = self._result
        if r is None or r.held is None or not r.held[i]:
            return None
        return next((h for h in r.holds if h.i0 <= i <= h.i1), None)

    def tip_rows(self, i: int, keys: Sequence[str]) -> List[Tuple[str, str]]:
        """``(label, value)`` rows of the tooltip at station ``i`` for the chosen fields."""
        r = self._result
        st = r.stations
        gap = float(r.gap_m[i])
        rows: List[Tuple[str, str]] = []
        for key in keys:
            if key == "kp":
                rows.append(("KP", f"{st.kp_km[i]:.3f}"))
            elif key == "depth":
                rows.append(("Seabed", f"{-st.seabed_m[i]:.2f} m"))
            elif key == "cable" and math.isfinite(r.cable_m[i]):
                rows.append(("Cable", f"{-r.cable_m[i]:.2f} m"))
            elif key == "gap":
                span = self._span_at(i)
                if span is not None:
                    k, s = span
                    level = pl.span_level(r, k)
                    rows.append((f"Span {k + 1} ({level})", f"{s.length_m:.1f} m long, up to {s.max_gap_m:.2f} m"))
                    if k < len(r.span_short_m) and math.isfinite(r.span_short_m[k]):
                        rows.append(("Short of seabed", f"{r.span_short_m[k]:.2f} m over the span"))
                    rows.append(("Gap here", f"{gap:.2f} m"))
                elif gap > pl.span_end_tol(r.criteria):
                    rows.append(("Gap", f"{gap:.2f} m (under the reporting limits)"))
                else:
                    rows.append(("Gap", "on the seabed"))
            elif key == "tension":
                rows.append(("Residual T", _tension_text(r.tension_kn[i])))
            elif key == "lay":
                rows.append(("Lay T", _tension_text(r.deposit_kn[i])))
            elif key == "allowable" and pl.allowable_shown(r) is not None and pl.allowable_shown(r)[i] < 99.9:
                rows.append(("Highest T, spans in limits", f"{r.allowable_kn[i]:.2f} kN"))
            elif key == "slack":
                series = self.slack_series(r)
                laid, used, surplus = (float(series[k][i]) for k in ("laid", "used", "surplus"))
                if math.isfinite(laid):
                    if self.slack_basis() == SLACK_SEABED and r.used_bottom_pct is not None:
                        rows.append(("Bottom slack", f"laid {laid:+.2f}%, used {used:+.2f}%, surplus {surplus:.2f}%"))
                    else:
                        rows.append(("Slack / route", f"laid {laid:.2f}%, used {used:.2f}%, fill "
                                                      f"{r.fill_pct[i]:.2f}%, surplus {surplus:.2f}%"))
            elif key == "slope":
                rows.append(("Slope", f"{math.degrees(math.atan(abs(st.slope[i]))):.1f}° (friction "
                                      f"{math.degrees(math.atan(st.mu[i])):.0f}°, μ {st.mu[i]:.2f})"))
            elif key == "type":
                rows.append(("Cable", self._type_label(i)))
            elif key == "soil" and st.soil[i]:
                rows.append(("Soil", st.soil[i]))
        colour = self.colour_text(i)
        if colour:
            rows.append(("Cable colour", colour))
        return rows

    def tip_html(self, i: Optional[int], keys: Sequence[str]) -> str:
        if i is None or self._result is None:
            return ""
        rows = self.tip_rows(i, keys)
        if not rows:
            return ""
        cells = "".join(f"<tr><td style='color:#666; padding-right:8px'>{html.escape(label)}</td>"
                        f"<td>{html.escape(value)}</td></tr>" for label, value in rows)
        return f"<table cellspacing='0' cellpadding='0'>{cells}</table>"

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
