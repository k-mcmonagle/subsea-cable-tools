# -*- coding: utf-8 -*-
"""Event labels for the Span & Slack profile: a lane above it, leaders into it.

* :class:`EventLayer` holds the events to draw and the label settings, and
  lays the labels out once per view (in screen pixels, on the lane's rows,
  never overlapping: :func:`span_analysis.events.layout_labels`).
* :class:`EventLaneItem` paints the lane: a tagged label per event with a
  leader down to its KP, KP ranges as bars, and a tick at every event (an
  event without room for its label keeps its tick; hover lists it).
* :class:`EventLeaderItem` continues each labelled event's leader down the
  profile to the cable, with a small mark there.
* :class:`EventGuideItem` draws the same KPs as faint lines on the other
  rows (optional).

All three paint in device pixels from one item each, so text never scales
with the view and nothing is added or removed per event (see the module
notes of :mod:`span_analysis.plot_panel`).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import QObject, QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QFontMetricsF, QPen

from . import events as ev

_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
_DOT = getattr(getattr(Qt, "PenStyle", Qt), "DotLine")
_ALIGN = getattr(getattr(Qt, "AlignmentFlag", Qt), "AlignCenter")
FONT_PX = 11
ROW_PX = 15            # one label row
BAR_PX = 5             # one KP-range bar row
TICK_PX = 6            # the ticks at the bottom of the lane
PAD_PX = 2
LABEL_NAME, LABEL_NAME_KP = "name", "name_kp"
MAX_LABEL_CHARS = 28
HIT_PX = 5


def _glyph(e: ev.ProfileEvent) -> str:
    """A mark before the label: filled where the model carries it, hollow where it does not."""
    if e.source == ev.SRC_MODEL and e.kind == "body":
        return "◆ "
    if e.group == ev.GROUP_BODIES and e.modelled is False:
        return "◇ "
    return ""


class EventLayer(QObject):
    """The events drawn on the profile, their label settings and the current label layout."""

    changed = pyqtSignal()

    def __init__(self, lane_vb, parent=None):
        super().__init__(parent)
        self.lane_vb = lane_vb
        self.events: List[ev.ProfileEvent] = []
        self.tiers = 2
        self.label_mode = LABEL_NAME
        self.font = QFont()
        self.font.setPixelSize(FONT_PX)
        self._metrics = QFontMetricsF(self.font)
        self._cache_key = None
        self._layout: Dict = {}
        self.range_rows = 0
        self._rows: List[int] = []

    # -- content -----------------------------------------------------------------------
    def set_events(self, events: Sequence[ev.ProfileEvent]) -> None:
        self.events = sorted(events, key=lambda e: e.lo)
        ranges = [i for i, e in enumerate(self.events) if e.is_range]
        rows = ev.range_rows([(self.events[i].lo, self.events[i].hi) for i in ranges], 2)
        self._rows = [-1] * len(self.events)
        for i, row in zip(ranges, rows):
            self._rows[i] = row
        self.range_rows = (max(rows) + 1) if rows else 0
        self._cache_key = None
        self.changed.emit()

    def set_style(self, tiers: Optional[int] = None, label_mode: Optional[str] = None) -> None:
        if tiers is not None:
            self.tiers = max(1, min(int(tiers), 6))
        if label_mode is not None:
            self.label_mode = label_mode
        self._cache_key = None
        self.changed.emit()

    def lane_height(self) -> int:
        return PAD_PX + self.tiers * ROW_PX + self.range_rows * (BAR_PX + 1) + TICK_PX + 1

    def text_of(self, e: ev.ProfileEvent) -> str:
        label = e.label if len(e.label) <= MAX_LABEL_CHARS else e.label[:MAX_LABEL_CHARS - 1] + "…"
        if self.label_mode == LABEL_NAME_KP and not e.is_range:
            label += f"  {e.kp:.3f}"
        return _glyph(e) + label

    def range_row(self, index: int) -> int:
        return self._rows[index] if 0 <= index < len(self._rows) else -1

    # -- layout ---------------------------------------------------------------------------
    def x_mapping(self, vb) -> Optional[Tuple[float, float, float, float, float]]:
        """``(lo, hi, x_lo, x_hi, left)``: the view's KP range and the device x of its ends
        (reversed KP maps hi to the left), and the view's left edge in device pixels."""
        try:
            (lo, hi), _y = vb.viewRange()
            a = vb.mapViewToDevice(QPointF(lo, 0.0))
            b = vb.mapViewToDevice(QPointF(hi, 0.0))
        except Exception:
            return None
        if a is None or b is None or not (hi > lo):
            return None
        return float(lo), float(hi), float(a.x()), float(b.x()), float(min(a.x(), b.x()))

    def layout(self) -> Dict:
        """``{"slots": [LabelSlot per event], "x": [device x rel. to the lane's left], "width", "lo", "hi"}``."""
        mapping = self.x_mapping(self.lane_vb)
        if mapping is None:
            return {}
        lo, hi, x_lo, x_hi, left = mapping
        width = abs(x_hi - x_lo)
        key = (round(lo, 9), round(hi, 9), round(x_lo, 1), round(x_hi, 1), self.tiers, self.label_mode,
               id(self.events), len(self.events))
        if key == self._cache_key:
            return self._layout
        scale = (x_hi - x_lo) / (hi - lo)
        xs, items = [], []
        for e in self.events:
            if e.is_range:
                a, b = max(e.lo, lo), min(e.hi, hi)
                x = x_lo - left + ((a + b) / 2.0 - lo) * scale
                visible_px = abs(b - a) * abs(scale)
                w = self._metrics.horizontalAdvance(self.text_of(e)) + 8.0
                # A range's label shows only over a stretch of it wide enough to hold it.
                items.append((x, w if b > a and visible_px >= w + 8.0 else 0.0, e.priority + 100))
            else:
                x = x_lo - left + (e.kp - lo) * scale
                inside = lo <= e.kp <= hi
                w = self._metrics.horizontalAdvance(self.text_of(e)) + 8.0 if inside else 0.0
                items.append((x, w, e.priority))
            xs.append(x)
        slots = ev.layout_labels(items, width, self.tiers)
        self._layout = {"slots": slots, "x": xs, "width": width, "lo": lo, "hi": hi}
        self._cache_key = key
        return self._layout

    def labelled_points(self) -> List[ev.ProfileEvent]:
        """The point events with a label in the lane now (the ones that get a leader)."""
        lay = self.layout()
        slots = lay.get("slots") or []
        return [e for e, s in zip(self.events, slots) if not e.is_range and s.tier >= 0]

    def events_at(self, x_px: float, y_px: float, lane_top: float) -> List[ev.ProfileEvent]:
        """Events under a point of the lane (device pixels relative to its left / top)."""
        lay = self.layout()
        if not lay:
            return []
        slots, xs = lay["slots"], lay["x"]
        bottom = self.lane_height()
        labels_bottom = PAD_PX + self.tiers * ROW_PX
        y = y_px - lane_top
        hits = []
        if y <= labels_bottom:
            tier = int((labels_bottom - y) // ROW_PX)
            hits = [e for e, s in zip(self.events, slots) if s.tier == tier and s.left - 2 <= x_px <= s.right + 2]
        if not hits and y <= bottom - TICK_PX and self.range_rows:
            row = int((y - labels_bottom) // (BAR_PX + 1))
            scale = (lay["width"]) / (lay["hi"] - lay["lo"]) if lay["hi"] > lay["lo"] else 0.0
            kp = lay["lo"] + x_px / scale if scale else math.nan
            reverse = self.x_mapping(self.lane_vb)
            if reverse is not None and reverse[2] > reverse[3]:   # reversed KP
                kp = lay["hi"] - x_px / scale if scale else math.nan
            hits = [e for i, e in enumerate(self.events) if e.is_range and self.range_row(i) == row
                    and e.lo <= kp <= e.hi]
        if not hits:
            hits = [e for e, x in zip(self.events, xs) if not e.is_range and abs(x - x_px) <= HIT_PX]
        return hits


class _DeviceItem(pg.GraphicsObject):
    """An item painting in device pixels across its whole view (never auto-ranges)."""

    def __init__(self, layer: EventLayer):
        super().__init__()
        self.layer = layer
        layer.changed.connect(self._refresh)

    def _refresh(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def boundingRect(self):
        view = self.viewRect()
        return QRectF() if view is None else QRectF(view)

    def dataBounds(self, axis, frac=1.0, orthoRange=None):
        return None

    def viewRangeChanged(self) -> None:
        self._refresh()

    def _view_device(self, painter) -> Optional[QRectF]:
        view = self.viewRect()
        if view is None:
            return None
        t = painter.transform()
        a, b = t.map(view.topLeft()), t.map(view.bottomRight())
        return QRectF(QPointF(min(a.x(), b.x()), min(a.y(), b.y())), QPointF(max(a.x(), b.x()), max(a.y(), b.y())))


class EventLaneItem(_DeviceItem):
    """The lane: labels on rows with leaders to their KP, range bars and ticks."""

    def paint(self, painter, *_args) -> None:
        layer = self.layer
        if not layer.events:
            return
        rect = self._view_device(painter)
        lay = layer.layout()
        if rect is None or not lay:
            return
        painter.save()
        try:
            painter.resetTransform()
            painter.setFont(layer.font)
            left, top, bottom = rect.left(), rect.top(), rect.bottom()
            labels_bottom = top + PAD_PX + layer.tiers * ROW_PX
            tick_top = bottom - TICK_PX
            slots, xs = lay["slots"], lay["x"]
            scale = lay["width"] / (lay["hi"] - lay["lo"])
            mapping = layer.x_mapping(layer.lane_vb)
            reverse = mapping is not None and mapping[2] > mapping[3]

            def x_of(kp):
                return left + ((lay["hi"] - kp) if reverse else (kp - lay["lo"])) * scale

            # KP ranges: bars on their rows.
            for i, e in enumerate(layer.events):
                if not e.is_range:
                    continue
                row = layer.range_row(i)
                a, b = sorted((x_of(max(e.lo, lay["lo"])), x_of(min(e.hi, lay["hi"]))))
                if b < rect.left() or a > rect.right():
                    continue
                y = labels_bottom + row * (BAR_PX + 1)
                colour = QColor(*((160, 160, 160) if e.muted else e.colour))
                colour.setAlpha(170)
                painter.fillRect(QRectF(a, y, max(b - a, 1.5), BAR_PX), colour)
            # Ticks at every point event.
            for e, x in zip(layer.events, xs):
                if e.is_range or not (lay["lo"] <= e.kp <= lay["hi"]):
                    continue
                painter.setPen(QPen(QColor(*((160, 160, 160) if e.muted else e.colour)), 1.5))
                painter.drawLine(QPointF(left + x, tick_top), QPointF(left + x, bottom))
            # Labels, with a leader from each point label down to its KP.
            for e, slot, x in zip(layer.events, slots, xs):
                if slot.tier < 0:
                    continue
                colour = QColor(*((140, 140, 140) if e.muted else e.colour))
                y1 = labels_bottom - slot.tier * ROW_PX
                box = QRectF(left + slot.left, y1 - ROW_PX + 2, slot.right - slot.left, ROW_PX - 3)
                if not e.is_range:
                    lead = QPen(colour, 1)
                    painter.setPen(lead)
                    anchor_x = min(max(left + x, box.left() + 2), box.right() - 2)
                    painter.drawLine(QPointF(anchor_x, box.bottom()), QPointF(left + x, tick_top))
                pen = QPen(colour, 1)
                if e.modelled is False:
                    pen.setStyle(_DASH)     # in the RPL or the plan, not in the model
                painter.setPen(pen)
                painter.setBrush(QColor(255, 255, 255, 235))
                painter.drawRoundedRect(box, 2.5, 2.5)
                painter.setPen(colour)
                painter.drawText(box, int(getattr(_ALIGN, "value", _ALIGN)), layer.text_of(e))
        finally:
            painter.restore()


class EventLeaderItem(_DeviceItem):
    """Leaders from the top of the profile down to the cable at each labelled event."""

    def __init__(self, layer: EventLayer):
        super().__init__(layer)
        self._kp: Optional[np.ndarray] = None
        self._depth: Optional[np.ndarray] = None

    def set_cable(self, kp: Optional[np.ndarray], depth: Optional[np.ndarray]) -> None:
        """The cable (KP, depth down) the leaders end on."""
        self._kp, self._depth = kp, depth
        self.update()

    def paint(self, painter, *_args) -> None:
        rect = self._view_device(painter)
        if rect is None or self._kp is None:
            return
        points = self.layer.labelled_points()
        if not points:
            return
        t = painter.transform()
        painter.save()
        try:
            painter.resetTransform()
            for e in points:
                depth = float(np.interp(e.kp, self._kp, self._depth)) if len(self._kp) else math.nan
                if not math.isfinite(depth):
                    continue
                end = t.map(QPointF(e.kp, depth))
                x = end.x()
                if not (rect.left() - 1 <= x <= rect.right() + 1):
                    continue
                y_end = min(max(end.y(), rect.top()), rect.bottom())
                colour = QColor(*((150, 150, 150) if e.muted else e.colour))
                pen = QPen(colour, 1)
                pen.setStyle(_DOT)
                painter.setPen(pen)
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, y_end))
                if e.source == ev.SRC_MODEL and e.kind == "body":
                    continue   # the body's own diamond marks it
                if rect.top() <= end.y() <= rect.bottom():
                    painter.setPen(QPen(colour, 1))
                    painter.setBrush(QColor(255, 255, 255) if e.modelled is False else colour)
                    painter.drawEllipse(QPointF(x, end.y()), 3.0, 3.0)
        finally:
            painter.restore()


class EventGuideItem(_DeviceItem):
    """Faint lines through a row at each labelled event."""

    def paint(self, painter, *_args) -> None:
        rect = self._view_device(painter)
        if rect is None:
            return
        points = self.layer.labelled_points()
        if not points:
            return
        t = painter.transform()
        painter.save()
        try:
            painter.resetTransform()
            for e in points:
                x = t.map(QPointF(e.kp, 0.0)).x()
                if not (rect.left() <= x <= rect.right()):
                    continue
                colour = QColor(*e.colour)
                colour.setAlpha(110)
                pen = QPen(colour, 1)
                pen.setStyle(_DOT)
                painter.setPen(pen)
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        finally:
            painter.restore()
