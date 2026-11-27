"""Optional event lane and full-route navigator for the Burial Planner profile."""
from __future__ import annotations

import html
import math

import pyqtgraph as pg
from qgis.PyQt.QtCore import Qt, pyqtSignal

from ..kp_axis_item import KPAxisItem
from ..span_analysis.event_lane import LABEL_NAME_KP, EventLaneItem, EventLayer, EventLeaderItem
from ..span_analysis.events import GROUP_OPERATIONS, SRC_BURIAL, ProfileEvent
from . import events as plan_events
from . import schema


def _finite_kp(value):
    try:
        kp = float(value)
    except (TypeError, ValueError):
        return None
    return kp if math.isfinite(kp) else None


class PlanEventLane(pg.PlotWidget):
    kpHovered = pyqtSignal(float)
    kpClicked = pyqtSignal(float)

    def __init__(self, depth_plot):
        super().__init__(background="w")
        item = self.getPlotItem()
        axis = item.getAxis("left")
        axis.setStyle(showValues=False)
        axis.setTicks([[]])
        axis.setPen(pg.mkPen(None))
        item.hideAxis("bottom")
        item.hideButtons()
        item.setMouseEnabled(x=True, y=False)
        item.setYRange(0, 1, padding=0)
        self.layer = EventLayer(item.vb, self)
        self.layer.set_style(label_mode=LABEL_NAME_KP)
        item.addItem(EventLaneItem(self.layer), ignoreBounds=True)
        self.leaders = EventLeaderItem(self.layer)
        self.leaders.setZValue(9)
        depth_plot.addItem(self.leaders, ignoreBounds=True)
        item.vb.sigXRangeChanged.connect(lambda *_: self.leaders.update())
        self.scene().sigMouseMoved.connect(self._hover)
        self.scene().sigMouseClicked.connect(self._click)

    def set_rows(self, events, sections, labels, method, event_color, section_styles):
        entries = []
        for row in events:
            kp = _finite_kp(row.get("kp"))
            if kp is None:
                continue
            label = labels.get(str(row.get("event_id") or "")) or plan_events.event_label(row.get("event_type"), method)
            status = row.get("status") or ""
            details = [f"{label}  KP {kp:.3f}", status, "Locked" if row.get("locked") else ""]
            entries.append(ProfileEvent(kp, None, SRC_BURIAL, GROUP_OPERATIONS, label,
                                        " · ".join(filter(None, details)), event_color(status).getRgb()[:3],
                                        priority=0 if status == schema.EVENT_STATUS_CONFLICT else 10))
        for row in sections:
            kind = row.get("kind")
            if kind not in section_styles:
                continue
            a, b = _finite_kp(row.get("start_kp")), _finite_kp(row.get("end_kp"))
            if a is None or b is None:
                continue
            label = schema.section_kind_label(kind, plan_events.section_method(row, method))
            detail = f"{label}  KP {a:.3f}–{b:.3f}"
            if row.get("state"):
                detail += " · " + row["state"]
            entries.append(ProfileEvent(a, b, SRC_BURIAL, GROUP_OPERATIONS, label, detail,
                                        section_styles[kind][0].getRgb()[:3]))
        self.layer.set_events(entries)
        self.setFixedHeight(self.layer.lane_height() + 2)
        self.setVisible(bool(entries))

    def _at(self, pos):
        vb = self.getPlotItem().vb
        rect = vb.sceneBoundingRect()
        if not rect.contains(pos):
            return None, []
        hits = self.layer.events_at(pos.x() - rect.left(), pos.y(), rect.top())
        point = next((e for e in hits if not e.is_range), None)
        return (point.kp if point else vb.mapSceneToView(pos).x()), hits

    def _hover(self, pos):
        kp, hits = self._at(pos)
        self.setToolTip("<br>".join(html.escape(e.detail) for e in hits) if hits else "")
        if kp is not None:
            self.kpHovered.emit(kp)

    def _click(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            kp, _hits = self._at(event.scenePos())
            if kp is not None:
                self.kpClicked.emit(kp)


class _WindowRegion(pg.LinearRegionItem):
    def mouseDragEvent(self, event):
        """Pan both edges together, stopping the whole window at the route ends."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        event.accept()
        if event.isStart():
            self.startPositions = [line.pos() for line in self.lines]
            self.moving = True
        if self.moving:
            left, right = (pos.x() for pos in self.startPositions)
            lo, hi = self.lines[0].bounds()
            delta = event.pos().x() - event.buttonDownPos().x()
            delta = max(lo - left, min(delta, hi - right))
            self.setRegion((left + delta, right + delta))
            if event.isFinish():
                self.moving = False


class RouteNavigator(pg.PlotWidget):
    """The overview keeps its full domain while the region follows the detail plot."""

    def __init__(self):
        axis = KPAxisItem()
        axis.set_linear()
        super().__init__(background="w", axisItems={"bottom": axis})
        self.setFixedHeight(68)
        self.setToolTip("Full route. Drag the window to pan or drag either edge to zoom.")
        item = self.getPlotItem()
        item.hideButtons()
        item.setMenuEnabled(False)
        item.setMouseEnabled(x=False, y=False)
        item.setLabel("bottom", "Full route KP", units="km")
        item.getAxis("bottom").enableAutoSIPrefix(False)
        item.getAxis("left").setStyle(showValues=False)
        item.getAxis("left").setTicks([[]])
        item.getAxis("left").setPen(pg.mkPen(None))
        item.invertY(True)
        self.curve = item.plot([], [], pen=pg.mkPen("#447590"), connect="finite")
        self.curve.setDownsampling(auto=True, method="peak")
        self.region = _WindowRegion(values=(0, 1), swapMode="block", brush=(52, 114, 158, 45),
                                    pen=pg.mkPen("#35759c", width=2))
        for boundary in self.region.lines:
            boundary.addMarker("<|>", position=0.5, size=8)
        item.addItem(self.region, ignoreBounds=True)
        self.region.sigRegionChanged.connect(self._apply_window)
        self._view = None
        self._domain = None
        self._syncing = False

    def set_view(self, view):
        if self._view is not view:
            if self._view is not None:
                self._view.sigXRangeChanged.disconnect(self._read_window)
            self._view = view
            if view is not None:
                view.sigXRangeChanged.connect(self._read_window)
        self._read_window()

    def set_domain(self, domain):
        self._domain = domain
        self.region.setVisible(domain is not None)
        if domain is not None:
            self._syncing = True
            try:
                self.region.setBounds(domain)
                self.setXRange(*domain, padding=0)
            finally:
                self._syncing = False
        self._read_window()

    def _read_window(self, *_args):
        if self._syncing or self._view is None or self._domain is None:
            return
        lo, hi = self._domain
        left, right = self._view.viewRange()[0]
        self._syncing = True
        try:
            self.region.setRegion((max(lo, min(hi, left)), max(lo, min(hi, right))))
        finally:
            self._syncing = False

    def _apply_window(self, *_args):
        if self._syncing or self._view is None or self._domain is None:
            return
        left, right = self.region.getRegion()
        if right > left:
            self._view.setXRange(left, right, padding=0)
        else:
            self._read_window()
