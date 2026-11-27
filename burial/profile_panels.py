"""Optional event lane and full-route navigator for the Burial Planner profile."""
from __future__ import annotations

import html
import math
from bisect import bisect_right

import pyqtgraph as pg
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from ..kp_axis_item import KPAxisItem
from ..span_analysis.event_lane import LABEL_NAME_KP, EventLaneItem, EventLayer, EventLeaderItem
from ..span_analysis.events import GROUP_OPERATIONS, SRC_BURIAL, SRC_RPL, ProfileEvent
from . import events as plan_events
from . import schema


def read_rpl_lane_events(model):
    from ..span_analysis.events import GROUP_ALTER, GROUP_BODIES, GROUP_CROSSINGS, rpl_point_event
    from ..workbench.assembly_model import EventClassifier
    from ..plugin_log import log_exception
    from .rpl_reference import rpl_event_rows

    rows, _ = rpl_event_rows(model, "", events_only=True)
    try:
        classifier = EventClassifier(model.workbench_store.list_event_rules())
    except Exception:
        log_exception("Burial Planner: using default RPL event rules")
        classifier = EventClassifier.with_defaults()
    entries = []
    for row in rows:
        if _finite_kp(row.kp) is None:
            continue
        event = rpl_point_event(row.event, row.kp, row.stated_kp, row.pos_no,
                                row.remarks, classifier.classify, f"rpl:{row.seq}")
        if event.kind == "repeater":
            category = "repeater"
            event.priority = 5
        else:
            category = event.group if event.group in (GROUP_ALTER, GROUP_BODIES, GROUP_CROSSINGS) else "other"
        entries.append((category, event))
    return entries


def _finite_kp(value):
    try:
        kp = float(value)
    except (TypeError, ValueError):
        return None
    return kp if math.isfinite(kp) else None


class _ProfileLane(pg.PlotWidget):
    kpHovered = pyqtSignal(float)
    kpClicked = pyqtSignal(float)

    def __init__(self, *, compact_when_crowded=False):
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
        self.layer = EventLayer(item.vb, self, compact_when_crowded=compact_when_crowded)
        self.layer.set_style(label_mode=LABEL_NAME_KP)
        item.addItem(EventLaneItem(self.layer), ignoreBounds=True)
        self.scene().sigMouseMoved.connect(self._hover)
        self.scene().sigMouseClicked.connect(self._click)

    def _set_entries(self, entries):
        self.layer.set_events(entries)
        self.setFixedHeight(self.layer.lane_height() + 2)
        self.setVisible(bool(entries))

    def _at(self, pos):
        vb = self.getViewBox()
        rect = vb.sceneBoundingRect()
        if not rect.contains(pos):
            return None, []
        hits = self.layer.events_at(pos.x() - rect.left(), pos.y(), rect.top())
        kp = vb.mapSceneToView(pos).x()
        hits.sort(key=lambda e: (abs(e.kp - kp), e.priority))
        point = next((e for e in hits if not e.is_range), None)
        return (point.kp if point else kp), hits

    def _hover(self, pos):
        kp, hits = self._at(pos)
        details = [html.escape(e.detail).replace("\n", "<br>") for e in hits[:12]]
        if len(hits) > len(details):
            details.append(f"{len(hits) - len(details)} more nearby. Zoom in for detail.")
        self.setToolTip("<br>".join(details))
        if kp is not None:
            self.kpHovered.emit(kp)

    def _click(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            kp, _hits = self._at(event.scenePos())
            if kp is not None:
                self.kpClicked.emit(kp)


class PlanEventLane(_ProfileLane):
    def __init__(self, depth_plot, *, compact_when_crowded=False):
        super().__init__(compact_when_crowded=compact_when_crowded)
        self.leaders = EventLeaderItem(self.layer)
        self.leaders.setZValue(9)
        depth_plot.addItem(self.leaders, ignoreBounds=True)
        self.getViewBox().sigXRangeChanged.connect(lambda *_: self.leaders.update())

    def set_rows(self, events, sections, labels, method, event_color, section_styles, rpl_events=()):
        entries = list(rpl_events)
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
        self._set_entries(entries)


class CableTypeBar(_ProfileLane):
    """One read-only row of contiguous RPL cable types at the plan's KPs."""

    def __init__(self):
        super().__init__()
        self.layer.set_style(tiers=1)
        axis = self.getAxis("left")
        axis.setStyle(showValues=True, tickLength=0, tickTextOffset=2, tickFont=self.layer.font)
        axis.setTextPen(pg.mkPen("#444"))
        axis.setTicks([[(0.5, "Cable type")]])
        self.cursor = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#444", style=Qt.PenStyle.DashLine))
        self.addItem(self.cursor, ignoreBounds=True)
        self.cursor.setZValue(10)
        self.cursor.hide()
        self._starts = []

    def set_ranges(self, ranges):
        from ..workbench.layer_style import colour_for_cable_type, user_cable_type_colours
        palette = user_cable_type_colours() if ranges else {}
        entries = []
        for start, end, label in ranges:
            rgb = QColor(colour_for_cable_type(label, palette)).getRgb()[:3]
            detail = f"Cable type: {label}\nKP {start:.3f}–{end:.3f}"
            entries.append(ProfileEvent(start, end, SRC_RPL, "cable_type", label, detail, rgb))
        self._set_entries(entries)
        self._starts = [e.kp for e in self.layer.events]
        self.setToolTip("")
        self.cursor.hide()

    def _at(self, pos):
        # The whole slim row is a target, including very short or unlabelled runs.
        vb = self.getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            return None, []
        kp = vb.mapSceneToView(pos).x()
        i = bisect_right(self._starts, kp) - 1
        if i >= 0 and kp <= self.layer.events[i].kp_end:
            return kp, [self.layer.events[i]]
        return kp, []


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
