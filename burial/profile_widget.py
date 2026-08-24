# -*- coding: utf-8 -*-
"""Longitudinal profile pane for the Burial Planner (vendored pyqtgraph).

Depth vs KP over the plan scope with the plan always in view: combined
Exclusion Area shading (red), screening annotations (amber), Constraint
Influence Zones (blue tint), Insufficient Information (grey), section strip
colouring via region items, and event markers (draggable in edit mode).
Crosshair readout shows KP (3 dp), depth and longitudinal slope; hover and
click are re-emitted so the dock can sync the map canvas.

Signals:
    kpHovered(float)          crosshair moved to this KP
    kpClicked(float)          user clicked the profile at this KP
    eventMoveRequested(str, float)  drag finished: event_id, proposed KP
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import pyqtgraph as pg

from qgis.PyQt.QtCore import QRectF, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import events as ev
from . import generation, schema

_PEN_STYLE = getattr(Qt, "PenStyle", Qt)
_VERTICAL = getattr(Qt, "Orientation", Qt).Vertical
_MOUSE_LEFT = getattr(Qt, "MouseButton", Qt).LeftButton

# One fixed left-axis width for both plots: x-linking alone still leaves the
# plot areas offset when the depth and slope tick labels need different
# widths, which breaks the visual KP alignment between the panels.
_LEFT_AXIS_WIDTH = 62

_SETTINGS_ROOT = "SubseaCableTools/BurialPlanner"

_REGION_STYLES = {
    "excluded": (QColor(214, 39, 40, 60), QColor(214, 39, 40, 110)),
    "screening": (QColor(255, 140, 0, 45), QColor(255, 140, 0, 90)),
    "influence": (QColor(31, 119, 180, 35), QColor(31, 119, 180, 80)),
    "insufficient": (QColor(120, 120, 120, 55), QColor(120, 120, 120, 100)),
}

# Plan-outcome strip along the top of the plot; colours match the map
# symbology in map_layers._SECTION_STYLES.
_STRIP_HEIGHT_PX = 12
_STRIP_STYLES = {
    schema.SECTION_BURIAL: (QColor(27, 127, 59, 170), "Burial"),
    schema.SECTION_SKIP: (QColor(214, 39, 40, 140), "Skip"),
    schema.SECTION_INSUFFICIENT: (QColor(158, 158, 158, 140),
                                  "Insufficient Information"),
}


class RangeBandItem(pg.GraphicsObject):
    """Every KP range of one kind, painted in a single item.

    One ``LinearRegionItem`` per range meant thousands of graphics items on
    plans with many slivers (edge-of-bathymetry Insufficient Information
    ranges, one strip rectangle per section): every zoom re-laid-out each of
    them and the auto-range walked them all. This item keeps the ranges as
    plain tuples, paints only those inside the visible KP window, and
    contributes nothing to auto-range (as the region items did on the y
    axis). ``labels_at(kp)`` replaces the per-region tooltips — the profile
    readout lists the ranges under the crosshair.
    """

    def __init__(self, brush=None, pen=None):
        super().__init__()
        self._ranges: List[Tuple[float, float, str, object]] = []
        self._starts: List[float] = []
        self._max_len = 0.0
        self._brush = pg.mkBrush(brush) if brush is not None else None
        self._pen = pg.mkPen(pen) if pen is not None else None
        self._xmin = 0.0
        self._xmax = 0.0

    def set_ranges(self, ranges) -> None:
        """``ranges``: iterable of ``(start, end, label[, brush])``."""
        cleaned = []
        for entry in ranges or []:
            try:
                start = float(entry[0])
                end = float(entry[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not (math.isfinite(start) and math.isfinite(end)):
                continue
            lo, hi = (start, end) if start <= end else (end, start)
            label = entry[2] if len(entry) > 2 else ""
            brush = (pg.mkBrush(entry[3])
                     if len(entry) > 3 and entry[3] is not None else None)
            cleaned.append((lo, hi, label, brush))
        cleaned.sort(key=lambda r: (r[0], r[1]))
        self._ranges = cleaned
        self._starts = [r[0] for r in cleaned]
        self._max_len = max((r[1] - r[0] for r in cleaned), default=0.0)
        self._xmin = cleaned[0][0] if cleaned else 0.0
        self._xmax = max((r[1] for r in cleaned), default=0.0)
        self.prepareGeometryChange()
        self.update()

    def ranges(self):
        return [(lo, hi, label) for lo, hi, label, _b in self._ranges]

    def _visible(self, lo: float, hi: float):
        """Ranges overlapping [lo, hi] (sorted by start; bounded scan)."""
        import bisect

        first = bisect.bisect_left(self._starts, lo - self._max_len)
        for index in range(first, len(self._ranges)):
            start, end, label, brush = self._ranges[index]
            if start > hi:
                break
            if end >= lo:
                yield start, end, label, brush

    def labels_at(self, kp: float) -> List[str]:
        return [label for _s, _e, label, _b in self._visible(kp, kp)
                if label]

    def boundingRect(self):
        if not self._ranges:
            return QRectF()
        view = self.viewRect()
        if view is None:
            return QRectF()
        rect = QRectF(view)
        rect.setLeft(self._xmin)
        rect.setRight(self._xmax)
        return rect

    def dataBounds(self, axis, frac=1.0, orthoRange=None):
        return None  # never drives auto-range (matches the y behaviour before)

    def viewRangeChanged(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def paint(self, painter, *_args) -> None:
        if not self._ranges:
            return
        view = self.viewRect()
        if view is None:
            return
        top, height = view.top(), view.height()
        painter.setPen(self._pen if self._pen is not None
                       else pg.mkPen(None))
        default_brush = (self._brush if self._brush is not None
                         else pg.mkBrush(None))
        for start, end, _label, brush in self._visible(view.left(),
                                                       view.right()):
            painter.setBrush(brush if brush is not None else default_brush)
            painter.drawRect(QRectF(start, top, end - start, height))


class EventMarkerItem(pg.GraphicsObject):
    """Read-only event markers (line + label) painted from one item.

    An ``InfiniteLine`` with an ``InfLineLabel`` per event cost ~1.3 ms each
    to build, on every event edit and plan load; a few hundred events made
    each refresh a visible stall. This item keeps ``(kp, pen, label,
    row)`` tuples, paints the lines inside the visible KP window and draws
    labels in device space (unscaled text) only while few enough events are
    visible for the labels to be readable. Draggable lines are still used
    in edit mode, where the user explicitly opts into per-event handles.
    """

    LABEL_LIMIT = 60  # labels are drawn only when at most this many are visible

    def __init__(self):
        super().__init__()
        self._events: List[Tuple[float, object, str, int]] = []
        self._kps: List[float] = []
        self._font = None

    def set_events(self, events) -> None:
        """``events``: iterable of ``(kp, pen, label, row)``; row 0/1 stacks
        labels so a start and an end at nearby KPs do not overprint."""
        cleaned = []
        for kp, pen, label, row in events or ():
            try:
                kp = float(kp)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(kp):
                continue
            cleaned.append((kp, pen, str(label or ""), int(row or 0)))
        cleaned.sort(key=lambda e: e[0])
        self._events = cleaned
        self._kps = [e[0] for e in cleaned]
        self.prepareGeometryChange()
        self.update()

    def count(self) -> int:
        return len(self._events)

    def _visible(self, lo: float, hi: float):
        import bisect

        first = bisect.bisect_left(self._kps, lo)
        for index in range(first, len(self._events)):
            entry = self._events[index]
            if entry[0] > hi:
                break
            yield entry

    def boundingRect(self):
        if not self._events:
            return QRectF()
        view = self.viewRect()
        if view is None:
            return QRectF()
        rect = QRectF(view)
        rect.setLeft(self._kps[0])
        rect.setRight(self._kps[-1])
        return rect

    def dataBounds(self, axis, frac=1.0, orthoRange=None):
        return None

    def viewRangeChanged(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def paint(self, painter, *_args) -> None:
        if not self._events:
            return
        view = self.viewRect()
        if view is None:
            return
        from qgis.PyQt.QtCore import QPointF

        top, bottom = view.top(), view.bottom()
        visible = list(self._visible(view.left(), view.right()))
        for kp, pen, _label, _row in visible:
            painter.setPen(pen)
            painter.drawLine(QPointF(kp, top), QPointF(kp, bottom))
        if not visible or len(visible) > self.LABEL_LIMIT:
            return
        # Labels in device space so text is never scaled by the view.
        transform = painter.transform()
        device_top = min(transform.map(QPointF(view.left(), top)).y(),
                         transform.map(QPointF(view.left(), bottom)).y())
        painter.save()
        try:
            painter.resetTransform()
            if self._font is None:
                self._font = painter.font()
                self._font.setPointSizeF(max(7.0, self._font.pointSizeF() - 1))
            painter.setFont(self._font)
            line_h = painter.fontMetrics().height()
            for kp, pen, label, row in visible:
                if not label:
                    continue
                x = transform.map(QPointF(kp, top)).x()
                y = device_top + 4 + line_h * (row + 1)
                painter.setPen(pen.color())
                painter.drawText(QPointF(x + 3, y), label)
        finally:
            painter.restore()


class BurialProfileWidget(QWidget):
    kpHovered = pyqtSignal(float)
    kpClicked = pyqtSignal(float)
    kpDoubleClicked = pyqtSignal(float)
    eventMoveRequested = pyqtSignal(str, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("w")
        # Right-click context menu: view-all, axis options, export (CSV/PNG/
        # SVG). kpClicked is emitted for left clicks only, so the menu and
        # the map sync don't fight over the right button.
        self.plot.setMenuEnabled(True)
        self.plot.setLabel("bottom", "KP", units="km")
        self.plot.setLabel("left", "Depth", units="m")
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setMinimumHeight(110)
        item = self.plot.getPlotItem()
        item.showGrid(x=True, y=True, alpha=0.25)
        item.vb.invertY(True)  # depth-down axis
        self._setup_axes(item)
        # Y follows the data visible in the current KP window while panning/
        # zooming x (manual y zoom disables this until View All / auto).
        item.vb.enableAutoRange(y=True)
        item.vb.setAutoVisible(y=True)

        # Slope panel: a second x-linked plot under the depth profile.
        # Longitudinal +ve = up-slope; cross +ve = deeper to starboard of
        # travel; absolute = combined gradient magnitude.
        self.slope_plot = pg.PlotWidget()
        self.slope_plot.setBackground("w")
        self.slope_plot.setMenuEnabled(True)
        self.slope_plot.setLabel("bottom", "KP", units="km")
        self.slope_plot.setLabel("left", "Slope", units="°")
        self.slope_plot.setMinimumHeight(80)
        slope_item = self.slope_plot.getPlotItem()
        slope_item.showGrid(x=True, y=True, alpha=0.25)
        self._setup_axes(slope_item)
        slope_item.setXLink(item)
        slope_item.vb.enableAutoRange(y=True)
        slope_item.vb.setAutoVisible(y=True)
        zero_line = pg.InfiniteLine(
            angle=0, pos=0.0, movable=False,
            pen=pg.mkPen((150, 150, 150), width=1, style=_PEN_STYLE.DashLine))
        slope_item.addItem(zero_line, ignoreBounds=True)
        self._slope_curves = {}
        for key, color, style, label in (
                ("long", "#ff7f0e", _PEN_STYLE.SolidLine, "Longitudinal"),
                ("cross", "#9467bd", _PEN_STYLE.SolidLine, "Cross"),
                ("abs", "#8c564b", _PEN_STYLE.DashLine, "Absolute")):
            curve = slope_item.plot(
                [], [], pen=pg.mkPen(color, width=1.6, style=style),
                name=label, connect="finite")
            try:
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
            except Exception:
                pass
            self._slope_curves[key] = curve

        # Crosshair mirrored onto the slope panel.
        self._slope_vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen((120, 120, 120), width=1, style=_PEN_STYLE.DashLine))
        self._slope_vline.setZValue(20)
        self._slope_vline.setVisible(False)
        slope_item.addItem(self._slope_vline, ignoreBounds=True)

        # Per-series show/hide, persisted; the coloured checkboxes double as
        # the legend.
        settings = QSettings()
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(8, 2, 8, 0)
        toggle_row.setSpacing(12)
        self._series_toggles: Dict[str, QCheckBox] = {}
        for key, color, label in (("long", "#ff7f0e", "Longitudinal"),
                                  ("cross", "#9467bd", "Cross"),
                                  ("abs", "#8c564b", "Absolute")):
            box = QCheckBox(label)
            box.setStyleSheet(f"color: {color}; font-weight: 600;")
            box.setChecked(bool(settings.value(
                f"{_SETTINGS_ROOT}/slope_series_{key}", True, type=bool)))
            box.toggled.connect(
                lambda checked, k=key: self._series_toggled(k, checked))
            self._slope_curves[key].setVisible(box.isChecked())
            self._series_toggles[key] = box
            toggle_row.addWidget(box)
        toggle_row.addStretch(1)

        self._slope_pane = QWidget()
        pane_layout = QVBoxLayout(self._slope_pane)
        pane_layout.setContentsMargins(0, 0, 0, 0)
        pane_layout.setSpacing(0)
        pane_layout.addLayout(toggle_row)
        pane_layout.addWidget(self.slope_plot, 1)
        self._slope_pane.setVisible(False)

        # Depth pane: a slim toggle row (sea level now; deck-transit path
        # later) above the depth plot, mirroring the slope pane's legend row.
        depth_toggle_row = QHBoxLayout()
        depth_toggle_row.setContentsMargins(8, 2, 8, 0)
        depth_toggle_row.setSpacing(12)
        self._sea_toggle = QCheckBox("Sea level (0 m)")
        self._sea_toggle.setStyleSheet("color: #17becf; font-weight: 600;")
        self._sea_toggle.setToolTip(
            "Show the water surface as a dashed line at 0 m depth. While "
            "shown, the depth axis auto-range includes the surface.")
        self._sea_toggle.setChecked(bool(settings.value(
            f"{_SETTINGS_ROOT}/show_sea_level", True, type=bool)))
        self._sea_toggle.toggled.connect(self._sea_level_toggled)
        depth_toggle_row.addWidget(self._sea_toggle)
        depth_toggle_row.addStretch(1)

        self._depth_pane = QWidget()
        depth_layout = QVBoxLayout(self._depth_pane)
        depth_layout.setContentsMargins(0, 0, 0, 0)
        depth_layout.setSpacing(0)
        depth_layout.addLayout(depth_toggle_row)
        depth_layout.addWidget(self.plot, 1)

        # User-adjustable split between depth and slope panels, persisted.
        self._splitter = QSplitter(_VERTICAL)
        self._splitter.addWidget(self._depth_pane)
        self._splitter.addWidget(self._slope_pane)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setCollapsible(0, False)
        state = settings.value(f"{_SETTINGS_ROOT}/profile_splitter_state")
        if state is not None:
            try:
                self._splitter.restoreState(state)
            except Exception:
                pass
        self._splitter.splitterMoved.connect(self._save_splitter_state)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._splitter)

        # Sea level: a real (dashed) data item rather than an InfiniteLine so
        # the y auto-range includes the surface exactly while it is visible.
        self._sea_curve = item.plot(
            [], [], pen=pg.mkPen("#17becf", width=1.5,
                                 style=_PEN_STYLE.DashLine),
            name="Sea level")
        self._sea_curve.setZValue(3)

        self._curve = item.plot([], [], pen=pg.mkPen("#1f77b4", width=2),
                                name="Depth", connect="finite")
        # Peak-preserving decimation: a naive stride can hide exactly the
        # narrow bathymetric spikes burial planning cares about.
        try:
            self._curve.setDownsampling(auto=True, method="peak")
            self._curve.setClipToView(True)
        except Exception:  # pragma: no cover — older pyqtgraph
            pass
        self._vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen((120, 120, 120), width=1, style=_PEN_STYLE.DashLine))
        self._vline.setZValue(20)
        self._vline.setVisible(False)
        item.addItem(self._vline, ignoreBounds=True)
        self._readout = pg.TextItem(color=(10, 10, 10), anchor=(0, 1),
                                    fill=pg.mkBrush(255, 255, 255, 220))
        self._readout.setZValue(22)
        self._readout.setVisible(False)
        item.addItem(self._readout)

        # One band item per overlay kind (see RangeBandItem).
        self._regions: Dict[str, RangeBandItem] = {}
        for kind, (brush, pen) in _REGION_STYLES.items():
            band = RangeBandItem(brush=brush, pen=pen)
            band.setZValue(2 if kind == "excluded" else 1)
            item.addItem(band, ignoreBounds=True)
            self._regions[kind] = band
        self._event_lines: List = []
        # Read-only markers: one painted item (see EventMarkerItem).
        self._event_markers = EventMarkerItem()
        self._event_markers.setZValue(10)
        item.addItem(self._event_markers, ignoreBounds=True)
        self._series: List[Tuple[float, float]] = []
        # Cached KP arrays for the crosshair lookups: rebuilding these lists
        # (up to ~500k floats, three or four times) on EVERY mouse move was
        # the dominant hover cost on long routes.
        self._series_xs: List[float] = []
        self._slope_series: Dict[str, List[Tuple[float, Optional[float]]]] = {}
        self._slope_series_xs: Dict[str, List[float]] = {}
        self._scope: Tuple[float, float] = (0.0, 0.0)
        self._editable = False
        self._slope_half_window_km: Optional[float] = None
        # Map-sync throttle: the crosshair/readout stays immediate, but the
        # canvas marker + tool footprint redraw at most every ~30 ms.
        from qgis.PyQt.QtCore import QTimer

        self._hover_kp: Optional[float] = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(30)
        self._hover_timer.timeout.connect(self._emit_hover)

        # Plan-outcome strip: a thin x-linked ViewBox pinned to the top of
        # the plot area, so section colouring never scales with the y-axis.
        self._strip_vb = pg.ViewBox(enableMouse=False, enableMenu=False)
        self._strip_vb.setZValue(5)
        item.scene().addItem(self._strip_vb)
        self._strip_vb.setXLink(item.vb)
        self._strip_vb.enableAutoRange(x=False, y=False)
        self._strip_vb.setYRange(0.0, 1.0, padding=0)
        self._strip_band = RangeBandItem()
        self._strip_vb.addItem(self._strip_band, ignoreBounds=True)
        item.vb.sigResized.connect(self._position_strip)
        self._position_strip()

        # Both plots drive one crosshair: hovering or clicking the slope
        # panel behaves exactly like the depth plot (map sync included).
        self.plot.scene().sigMouseMoved.connect(
            lambda pos: self._handle_mouse_moved(pos, self.plot))
        self.plot.scene().sigMouseClicked.connect(
            lambda event: self._handle_mouse_clicked(event, self.plot))
        self.slope_plot.scene().sigMouseMoved.connect(
            lambda pos: self._handle_mouse_moved(pos, self.slope_plot))
        self.slope_plot.scene().sigMouseClicked.connect(
            lambda event: self._handle_mouse_clicked(event, self.slope_plot))

    def _setup_axes(self, plot_item) -> None:
        """Fixed units and a shared left-axis width on a plot item.

        pyqtgraph's SI auto-prefix relabels an axis once tick values pass
        1000 — a long route showed "KP (kkm)" — so depths stay in metres and
        KP in km at any magnitude. The fixed left-axis width keeps both plot
        areas x-aligned regardless of tick label widths.
        """
        for name in ("bottom", "left"):
            axis = plot_item.getAxis(name)
            try:
                axis.enableAutoSIPrefix(False)
            except Exception:  # pragma: no cover — very old pyqtgraph
                pass
        plot_item.getAxis("left").setWidth(_LEFT_AXIS_WIDTH)

    def link_kp_plot(self, plot_widget) -> None:
        """Give another plot the profile's KP range and axis alignment.

        Installation-path deviation is optional and lives in another tab,
        but it represents the same scoped KP domain. Linking through this
        method keeps zoom/pan and the fixed plot-area origin consistent with
        both the depth and slope panels without exposing layout constants.
        """
        other_item = plot_widget.getPlotItem()
        self._setup_axes(other_item)
        other_item.setXLink(self.plot.getPlotItem())

    def _series_toggled(self, key: str, checked: bool) -> None:
        QSettings().setValue(f"{_SETTINGS_ROOT}/slope_series_{key}",
                             bool(checked))
        curve = self._slope_curves.get(key)
        if curve is not None:
            curve.setVisible(bool(checked))

    def _save_splitter_state(self, *_args) -> None:
        QSettings().setValue(f"{_SETTINGS_ROOT}/profile_splitter_state",
                             self._splitter.saveState())

    def _sea_level_toggled(self, checked: bool) -> None:
        QSettings().setValue(f"{_SETTINGS_ROOT}/show_sea_level", bool(checked))
        self._update_sea_level()

    def _update_sea_level(self) -> None:
        lo, hi = self._scope
        show = self._sea_toggle.isChecked() and hi > lo
        self._sea_curve.setData([lo, hi] if show else [],
                                [0.0, 0.0] if show else [])
        self._sea_curve.setVisible(show)

    # -- data ---------------------------------------------------------------
    def set_scope(self, start_kp: float, end_kp: float) -> None:
        lo, hi = min(start_kp, end_kp), max(start_kp, end_kp)
        self._scope = (lo, hi)
        if hi > lo:
            self.plot.setXRange(lo, hi, padding=0.02)
        self._update_sea_level()

    def set_slope_window_m(self, step_m: float) -> None:
        """Match the crosshair slope to the local profile scale.

        The readout differences depths at ±(profile station step), matching
        the local slope panel and Auto slope rules. An explicit rule-level
        vehicle footprint can intentionally use a wider evaluation length.
        """
        try:
            self._slope_half_window_km = max(float(step_m), 1.0) / 1000.0
        except (TypeError, ValueError):
            self._slope_half_window_km = None

    def set_profile(self, series: List[Tuple[float, float]]) -> None:
        """series: (kp_km, depth_m magnitude).

        The full series is handed to pyqtgraph; peak-preserving auto
        downsampling keeps rendering fast without hiding narrow spikes.
        """
        self._series = sorted(series)
        xs = [kp for kp, _d in self._series]
        ys = [d for _kp, d in self._series]
        self._series_xs = xs  # cached for the per-hover lookups
        self._curve.setData(xs, ys, connect="finite")

    def set_overlays(self, context: generation.ResolutionContext) -> None:
        self._regions["excluded"].set_ranges(
            (v.start_km, v.end_km, "Exclusion Area")
            for v in context.excluded)
        self._regions["screening"].set_ranges(
            (v.start_km, v.end_km, "Screening Criterion — flags for assessment")
            for v in context.screening)
        self._regions["influence"].set_ranges(
            (z.start_km, z.end_km,
             f"Constraint Influence Zone of {z.rule_name}")
            for z in context.influence)
        self._regions["insufficient"].set_ranges(
            (iv.start_km, iv.end_km, "Insufficient Information")
            for iv in context.insufficient)

    def set_slope_visible(self, visible: bool) -> None:
        self._slope_pane.setVisible(bool(visible))

    def set_slope_series(self, long_series, cross_series, abs_series) -> None:
        """Series are (kp, degrees|None) lists; None renders as a gap."""
        nan = float("nan")
        for key, series in (("long", long_series), ("cross", cross_series),
                            ("abs", abs_series)):
            series = list(series or [])
            self._slope_series[key] = series
            xs = [kp for kp, _v in series]
            ys = [nan if v is None else float(v) for _kp, v in series]
            self._slope_series_xs[key] = xs  # cached for hover lookups
            self._slope_curves[key].setData(xs, ys, connect="finite")

    def _slope_series_value_at(self, key: str, kp: float) -> Optional[float]:
        """Nearest stored slope-series value at kp (None = gap/no series)."""
        series = self._slope_series.get(key) or []
        if not series:
            return None
        import bisect

        xs = self._slope_series_xs.get(key) or [p[0] for p in series]
        i = bisect.bisect_left(xs, kp)
        candidates = [j for j in (i - 1, i) if 0 <= j < len(series)]
        if not candidates:
            return None
        j = min(candidates, key=lambda j: abs(xs[j] - kp))
        return series[j][1]

    def _position_strip(self) -> None:
        """Pin the outcome strip to the top of the plot area (fixed height)."""
        vb = self.plot.getPlotItem().vb
        rect = vb.sceneBoundingRect()
        self._strip_vb.setGeometry(QRectF(rect.left(), rect.top(),
                                          rect.width(), _STRIP_HEIGHT_PX))
        try:
            self._strip_vb.linkedViewChanged(vb, self._strip_vb.XAxis)
        except Exception:
            pass
        self._strip_vb.setYRange(0.0, 1.0, padding=0)

    def set_sections(self, sections: List[Dict]) -> None:
        """Colour the top strip with the plan outcome (burial/skip/insufficient)."""
        ranges = []
        for section in sections or []:
            style = _STRIP_STYLES.get(section.get("kind") or "")
            if style is None:
                continue
            try:
                start = float(section.get("start_kp"))
                end = float(section.get("end_kp"))
            except (TypeError, ValueError):
                continue
            color, label = style
            text = (f"{label} KP {schema.format_kp(start)}-"
                    f"{schema.format_kp(end)}")
            conclusion = schema.CONCLUSION_LABELS.get(
                section.get("conclusion") or "", "")
            if conclusion:
                text += f" — {conclusion}"
            ranges.append((start, end, text, color))
        self._strip_band.set_ranges(ranges)

    def overlay_labels_at(self, kp: float) -> List[str]:
        """Overlay/section descriptions under a KP (the old region tooltips)."""
        labels: List[str] = []
        for kind in ("excluded", "screening", "influence", "insufficient"):
            for label in self._regions[kind].labels_at(kp):
                if label not in labels:
                    labels.append(label)
        labels.extend(self._strip_band.labels_at(kp))
        return labels

    def set_events(self, events: List[Dict], method: str, editable: bool = False) -> None:
        """Event markers: one painted item normally; draggable lines in
        edit mode (the profile drag toggle), where per-event handles are
        the point."""
        item = self.plot.getPlotItem()
        for line in self._event_lines:
            item.removeItem(line)
        self._event_lines = []
        self._editable = editable
        painted = []
        lo, hi = self._scope
        for event in events:
            try:
                kp = float(event.get("kp"))
            except (TypeError, ValueError):
                continue
            is_start = event.get("event_type") == schema.EVENT_BURIAL_START
            status = event.get("status") or ""
            if status == schema.EVENT_STATUS_CONFLICT:
                color = QColor("#d62728")
            elif status == schema.EVENT_STATUS_CONFIRMED:
                color = QColor("#0e4d22")
            else:
                color = QColor("#1b7f3b")
            style = _PEN_STYLE.SolidLine if status == schema.EVENT_STATUS_CONFIRMED \
                else _PEN_STYLE.DashLine
            label = ev.event_label(event.get("event_type") or "", method)
            marker = "▼" if is_start else "▲"
            text = f"{marker} {label} {schema.format_kp(kp)}"
            pen = pg.mkPen(color, width=2, style=style)
            movable = editable and not int(event.get("locked") or 0)
            if not editable:
                painted.append((kp, pen, text, 0 if is_start else 1))
                continue
            try:
                line = pg.InfiniteLine(
                    pos=kp, angle=90, movable=movable, pen=pen, label=text,
                    labelOpts={"position": 0.92 if is_start else 0.84,
                               "color": color, "movable": False})
            except TypeError:  # older pyqtgraph without label kwargs
                line = pg.InfiniteLine(pos=kp, angle=90, movable=movable,
                                       pen=pen)
            line.setZValue(10)
            if hi > lo and movable:
                line.setBounds((lo, hi))
            line._bp_event_id = event.get("event_id") or ""
            line._bp_original_kp = kp
            if movable:
                line.sigPositionChangeFinished.connect(self._on_line_moved)
            item.addItem(line, ignoreBounds=True)
            self._event_lines.append(line)
        self._event_markers.set_events(painted)

    def revert_event_line(self, event_id: str) -> None:
        """Snap a dragged line back after a rejected move."""
        for line in self._event_lines:
            if getattr(line, "_bp_event_id", "") == event_id:
                try:
                    line.blockSignals(True)
                    line.setPos(getattr(line, "_bp_original_kp", line.value()))
                finally:
                    line.blockSignals(False)
                return

    def clear(self) -> None:
        self._scope = (0.0, 0.0)
        self._update_sea_level()
        self.set_profile([])
        self.set_overlays(generation.ResolutionContext())
        self.set_events([], "")
        self.set_sections([])
        self.set_slope_series([], [], [])
        self._vline.setVisible(False)
        self._slope_vline.setVisible(False)
        self._readout.setVisible(False)

    # -- interaction --------------------------------------------------------
    def _on_line_moved(self, line) -> None:
        event_id = getattr(line, "_bp_event_id", "")
        if event_id:
            self.eventMoveRequested.emit(event_id, float(line.value()))

    def _kp_at_scene_pos(self, pos, plot=None) -> Optional[float]:
        plot = plot or self.plot
        if not plot.sceneBoundingRect().contains(pos):
            return None
        view = plot.getViewBox().mapSceneToView(pos)
        return float(view.x())

    def _nearest_sample(self, kp: float) -> Optional[Tuple[float, float]]:
        if not self._series:
            return None
        import bisect

        xs = self._series_xs
        i = bisect.bisect_left(xs, kp)
        candidates = [j for j in (i - 1, i) if 0 <= j < len(self._series)]
        if not candidates:
            return None
        j = min(candidates, key=lambda j: abs(xs[j] - kp))
        return self._series[j]

    def _interp_depth(self, xs: List[float], kp: float) -> Optional[float]:
        import bisect
        if kp <= xs[0]:
            return self._series[0][1]
        if kp >= xs[-1]:
            return self._series[-1][1]
        j = bisect.bisect_left(xs, kp)
        kp0, d0 = self._series[j - 1]
        kp1, d1 = self._series[j]
        if kp1 <= kp0:
            return d1
        t = (kp - kp0) / (kp1 - kp0)
        return d0 + t * (d1 - d0)

    def _slope_at(self, kp: float) -> Optional[float]:
        """Signed slope (°) at kp; positive = shoaling with increasing KP.

        Central difference over ± the analysis half-window when the dock has
        provided one (so the readout matches what the rules measured), else
        over the single bracketing display interval. The series holds depth
        magnitudes, hence the negated difference for up-slope-positive.
        """
        if len(self._series) < 2:
            return None
        import bisect

        xs = self._series_xs
        half = self._slope_half_window_km
        if half:
            k0 = max(xs[0], kp - half)
            k1 = min(xs[-1], kp + half)
            dx_m = (k1 - k0) * 1000.0
            if dx_m <= 1e-6:
                return None
            d0 = self._interp_depth(xs, k0)
            d1 = self._interp_depth(xs, k1)
            if d0 is None or d1 is None:
                return None
            return math.degrees(math.atan2(-(d1 - d0), dx_m))
        i = min(max(bisect.bisect_left(xs, kp), 1), len(xs) - 1)
        kp0, d0 = self._series[i - 1]
        kp1, d1 = self._series[i]
        dx_m = (kp1 - kp0) * 1000.0
        if dx_m <= 0:
            return None
        return math.degrees(math.atan2(-(d1 - d0), dx_m))

    def focus_kp(self, kp: float) -> None:
        """Show the profile crosshair/readout at a table-selected KP."""
        lo, hi = self._scope
        value = float(kp)
        if hi > lo:
            value = min(max(value, lo), hi)
        self._show_kp_readout(value)

    def focus_range(self, start_kp: float, end_kp: float) -> None:
        """Zoom the shared KP axis to a selected section and show its centre."""
        lo, hi = sorted((float(start_kp), float(end_kp)))
        if hi > lo:
            self.plot.setXRange(lo, hi, padding=0.08)
            self.focus_kp((lo + hi) / 2.0)

    def reset_scope_view(self) -> None:
        lo, hi = self._scope
        if hi > lo:
            self.plot.setXRange(lo, hi, padding=0.02)

    def _show_kp_readout(self, kp: float) -> None:
        sample = self._nearest_sample(kp)
        self._vline.setPos(kp)
        self._vline.setVisible(True)
        self._slope_vline.setPos(kp)
        self._slope_vline.setVisible(True)
        lines = [f"KP {schema.format_kp(kp)}"]
        if sample is not None:
            lines.append(f"Depth {sample[1]:.1f} m")
            slope = self._slope_at(kp)
            if slope is not None:
                lines.append(f"Slope {slope:+.1f}°")
            # Cross/absolute at the cursor when the slope panel shows them.
            if self._slope_pane.isVisibleTo(self):
                for key, fmt in (("cross", "Cross {:+.1f}°"),
                                 ("abs", "Abs {:.1f}°")):
                    toggle = self._series_toggles.get(key)
                    if toggle is None or not toggle.isChecked():
                        continue
                    value = self._slope_series_value_at(key, kp)
                    if value is not None:
                        lines.append(fmt.format(value))
            lines.extend(self.overlay_labels_at(kp)[:4])
            self._readout.setText("\n".join(lines))
            self._readout.setPos(kp, sample[1])
            self._readout.setVisible(True)
        else:
            lines.extend(self.overlay_labels_at(kp)[:4])
            self._readout.setText("\n".join(lines))
            self._readout.setVisible(True)

    def _emit_hover(self) -> None:
        if self._hover_kp is not None:
            self.kpHovered.emit(self._hover_kp)

    def _handle_mouse_moved(self, pos, plot) -> None:
        kp = self._kp_at_scene_pos(pos, plot)
        if kp is None:
            self._vline.setVisible(False)
            self._slope_vline.setVisible(False)
            self._readout.setVisible(False)
            return
        self._show_kp_readout(kp)
        # Throttled: the map marker + tool footprint follow the latest KP at
        # ~30 ms cadence instead of once per mouse-move event.
        self._hover_kp = kp
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    def _handle_mouse_clicked(self, event, plot) -> None:
        try:
            if event.button() != _MOUSE_LEFT:
                return  # leave right-click to the pyqtgraph context menu
        except (AttributeError, TypeError):
            pass
        kp = self._kp_at_scene_pos(event.scenePos(), plot)
        if kp is None:
            return
        try:
            double = bool(event.double())
        except (AttributeError, TypeError):
            double = False
        if double:
            self.kpDoubleClicked.emit(kp)
        else:
            self.kpClicked.emit(kp)
