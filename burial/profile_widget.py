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

        self._regions: List = []
        self._event_lines: List = []
        self._series: List[Tuple[float, float]] = []
        self._slope_series: Dict[str, List[Tuple[float, Optional[float]]]] = {}
        self._scope: Tuple[float, float] = (0.0, 0.0)
        self._editable = False
        self._slope_half_window_km: Optional[float] = None

        # Plan-outcome strip: a thin x-linked ViewBox pinned to the top of
        # the plot area, so section colouring never scales with the y-axis.
        self._strip_vb = pg.ViewBox(enableMouse=False, enableMenu=False)
        self._strip_vb.setZValue(5)
        item.scene().addItem(self._strip_vb)
        self._strip_vb.setXLink(item.vb)
        self._strip_vb.enableAutoRange(x=False, y=False)
        self._strip_vb.setYRange(0.0, 1.0, padding=0)
        self._strip_items: List = []
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
        self._curve.setData(xs, ys, connect="finite")

    def set_overlays(self, context: generation.ResolutionContext) -> None:
        item = self.plot.getPlotItem()
        for region in self._regions:
            item.removeItem(region)
        self._regions = []

        def add(kind: str, start: float, end: float, tooltip: str = ""):
            brush, pen = _REGION_STYLES[kind]
            region = pg.LinearRegionItem(values=(start, end), movable=False,
                                         brush=pg.mkBrush(brush),
                                         pen=pg.mkPen(pen))
            region.setZValue(2 if kind == "excluded" else 1)
            if tooltip:
                try:
                    region.setToolTip(tooltip)
                except Exception:
                    pass
            item.addItem(region)
            self._regions.append(region)

        for verdict in context.excluded:
            add("excluded", verdict.start_km, verdict.end_km, "Exclusion Area")
        for verdict in context.screening:
            add("screening", verdict.start_km, verdict.end_km,
                "Screening Criterion — flags for assessment")
        for zone in context.influence:
            add("influence", zone.start_km, zone.end_km,
                f"Constraint Influence Zone of {zone.rule_name}")
        for iv in context.insufficient:
            add("insufficient", iv.start_km, iv.end_km, "Insufficient Information")

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
            self._slope_curves[key].setData(xs, ys, connect="finite")

    def _slope_series_value_at(self, key: str, kp: float) -> Optional[float]:
        """Nearest stored slope-series value at kp (None = gap/no series)."""
        series = self._slope_series.get(key) or []
        if not series:
            return None
        import bisect

        xs = [p[0] for p in series]
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
        for strip_item in self._strip_items:
            self._strip_vb.removeItem(strip_item)
        self._strip_items = []
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
            region = pg.LinearRegionItem(values=(start, end), movable=False,
                                         brush=pg.mkBrush(color),
                                         pen=pg.mkPen(color))
            tooltip = (f"{label} KP {schema.format_kp(start)}-"
                       f"{schema.format_kp(end)}")
            conclusion = schema.CONCLUSION_LABELS.get(
                section.get("conclusion") or "", "")
            if conclusion:
                tooltip += f" — {conclusion}"
            try:
                region.setToolTip(tooltip)
            except Exception:
                pass
            self._strip_vb.addItem(region)
            self._strip_items.append(region)

    def set_events(self, events: List[Dict], method: str, editable: bool = False) -> None:
        item = self.plot.getPlotItem()
        for line in self._event_lines:
            item.removeItem(line)
        self._event_lines = []
        self._editable = editable
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
            movable = editable and not int(event.get("locked") or 0)
            try:
                line = pg.InfiniteLine(
                    pos=kp, angle=90, movable=movable,
                    pen=pg.mkPen(color, width=2, style=style),
                    label=f"{marker} {label} {schema.format_kp(kp)}",
                    labelOpts={"position": 0.92 if is_start else 0.84,
                               "color": color, "movable": False})
            except TypeError:  # older pyqtgraph without label kwargs
                line = pg.InfiniteLine(pos=kp, angle=90, movable=movable,
                                       pen=pg.mkPen(color, width=2, style=style))
            line.setZValue(10)
            lo, hi = self._scope
            if hi > lo and movable:
                line.setBounds((lo, hi))
            line._bp_event_id = event.get("event_id") or ""
            line._bp_original_kp = kp
            if movable:
                line.sigPositionChangeFinished.connect(self._on_line_moved)
            item.addItem(line, ignoreBounds=True)
            self._event_lines.append(line)

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

        xs = [p[0] for p in self._series]
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

        xs = [p[0] for p in self._series]
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
            self._readout.setText("\n".join(lines))
            self._readout.setPos(kp, sample[1])
            self._readout.setVisible(True)
        else:
            self._readout.setText(lines[0])
            self._readout.setVisible(True)

    def _handle_mouse_moved(self, pos, plot) -> None:
        kp = self._kp_at_scene_pos(pos, plot)
        if kp is None:
            self._vline.setVisible(False)
            self._slope_vline.setVisible(False)
            self._readout.setVisible(False)
            return
        self._show_kp_readout(kp)
        self.kpHovered.emit(kp)

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
