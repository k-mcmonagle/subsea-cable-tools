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

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QVBoxLayout, QWidget

from . import events as ev
from . import generation, schema

_PEN_STYLE = getattr(Qt, "PenStyle", Qt)

_MAX_PLOT_POINTS = 20000

_REGION_STYLES = {
    "excluded": (QColor(214, 39, 40, 60), QColor(214, 39, 40, 110)),
    "screening": (QColor(255, 140, 0, 45), QColor(255, 140, 0, 90)),
    "influence": (QColor(31, 119, 180, 35), QColor(31, 119, 180, 80)),
    "insufficient": (QColor(120, 120, 120, 55), QColor(120, 120, 120, 100)),
}


class BurialProfileWidget(QWidget):
    kpHovered = pyqtSignal(float)
    kpClicked = pyqtSignal(float)
    eventMoveRequested = pyqtSignal(str, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("w")
        self.plot.setMenuEnabled(False)
        self.plot.setLabel("bottom", "KP", units="km")
        self.plot.setLabel("left", "Depth", units="m")
        self.plot.setMouseEnabled(x=True, y=True)
        item = self.plot.getPlotItem()
        item.showGrid(x=True, y=True, alpha=0.25)
        item.vb.invertY(True)  # depth-down axis

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot)

        self._curve = item.plot([], [], pen=pg.mkPen("#1f77b4", width=2),
                                name="Depth", connect="finite")
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
        self._scope: Tuple[float, float] = (0.0, 0.0)
        self._editable = False

        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)

    # -- data ---------------------------------------------------------------
    def set_scope(self, start_kp: float, end_kp: float) -> None:
        lo, hi = min(start_kp, end_kp), max(start_kp, end_kp)
        self._scope = (lo, hi)
        if hi > lo:
            self.plot.setXRange(lo, hi, padding=0.02)

    def set_profile(self, series: List[Tuple[float, float]]) -> None:
        """series: (kp_km, depth_m magnitude); rendered at data resolution."""
        self._series = sorted(series)
        xs = [kp for kp, _d in self._series]
        ys = [d for _kp, d in self._series]
        if len(xs) > _MAX_PLOT_POINTS:
            step = max(1, len(xs) // _MAX_PLOT_POINTS)
            xs, ys = xs[::step], ys[::step]
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
        self.set_profile([])
        self.set_overlays(generation.ResolutionContext())
        self.set_events([], "")
        self._vline.setVisible(False)
        self._readout.setVisible(False)

    # -- interaction --------------------------------------------------------
    def _on_line_moved(self, line) -> None:
        event_id = getattr(line, "_bp_event_id", "")
        if event_id:
            self.eventMoveRequested.emit(event_id, float(line.value()))

    def _kp_at_scene_pos(self, pos) -> Optional[float]:
        if not self.plot.sceneBoundingRect().contains(pos):
            return None
        view = self.plot.getViewBox().mapSceneToView(pos)
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

    def _slope_at(self, kp: float) -> Optional[float]:
        if len(self._series) < 2:
            return None
        import bisect

        xs = [p[0] for p in self._series]
        i = min(max(bisect.bisect_left(xs, kp), 1), len(xs) - 1)
        kp0, d0 = self._series[i - 1]
        kp1, d1 = self._series[i]
        dx_m = (kp1 - kp0) * 1000.0
        if dx_m <= 0:
            return None
        return math.degrees(math.atan2(d1 - d0, dx_m))

    def _on_mouse_moved(self, pos) -> None:
        kp = self._kp_at_scene_pos(pos)
        if kp is None:
            self._vline.setVisible(False)
            self._readout.setVisible(False)
            return
        sample = self._nearest_sample(kp)
        self._vline.setPos(kp)
        self._vline.setVisible(True)
        lines = [f"KP {schema.format_kp(kp)}"]
        if sample is not None:
            lines.append(f"Depth {sample[1]:.1f} m")
            slope = self._slope_at(kp)
            if slope is not None:
                lines.append(f"Slope {slope:+.1f}°")
            self._readout.setText("\n".join(lines))
            self._readout.setPos(kp, sample[1])
            self._readout.setVisible(True)
        else:
            self._readout.setText(lines[0])
            self._readout.setVisible(True)
        self.kpHovered.emit(kp)

    def _on_mouse_clicked(self, event) -> None:
        kp = self._kp_at_scene_pos(event.scenePos())
        if kp is not None:
            self.kpClicked.emit(kp)
