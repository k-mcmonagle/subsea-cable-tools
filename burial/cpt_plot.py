# -*- coding: utf-8 -*-
"""CPT traces on the Ground Model plot (KP × depth below seabed).

Each placed CPT is drawn at its KP the way logs are drawn on a fence
diagram: a baseline from the seabed to the deepest reading, the chosen
channel as a trace whose full scale is a fixed number of screen pixels (so
traces stay readable at any KP zoom), an optional SBTn colour strip beside
the baseline, and the CPT name above the seabed. Labels that would overlap
are left out. One item paints every CPT (thousands of items do not scale).
"""

from __future__ import annotations

import bisect
import math
from typing import Dict, List, Optional, Tuple

import pyqtgraph as pg
from qgis.PyQt.QtCore import QPointF, QRectF
from qgis.PyQt.QtGui import QColor, QFontMetricsF, QPainterPath

from ..geotech import cpt as cpt_mod

TRACE_COLOUR = "#7a1f5c"
_STRIP_PX = 5.0
_GAP_PX = 2.0


def nice_ceiling(value: float) -> float:
    """1, 2 or 5 × 10^n at or above ``value`` (full scales read cleanly)."""
    if not value or value <= 0 or not math.isfinite(value):
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * power + 1e-12:
            return step * power
    return 10 * power


class CptTraceItem(pg.GraphicsObject):
    """All CPT traces painted from one item, clipped to the view."""

    def __init__(self):
        super().__init__()
        self._entries: List[Dict] = []
        self._kps: List[float] = []
        self.full_scale = 1.0
        self.width_px = 56.0
        self.strip = True
        self.selected = ""
        self._pen = pg.mkPen(QColor(TRACE_COLOUR), width=1.4)
        self._selected_pen = pg.mkPen(QColor("#2b0a20"), width=2.4)
        self._base_pen = pg.mkPen(QColor(120, 120, 120, 200), width=1.0)
        fill = QColor(TRACE_COLOUR)
        fill.setAlpha(45)
        self._fill = pg.mkBrush(fill)
        self._zone_brushes = {zone: pg.mkBrush(QColor(colour))
                              for zone, (_label, colour) in cpt_mod.SBT_ZONES.items()}
        self._max_depth = 0.0

    # -- data ----------------------------------------------------------------
    def set_entries(self, entries: List[Dict], full_scale: float, width_px: float = 56.0,
                    strip: bool = True) -> None:
        """``entries``: ``{cpt_id, kp, label, depths, values, zones, base}``."""
        self.prepareGeometryChange()
        self._entries = sorted(entries, key=lambda e: e["kp"])
        self._kps = [e["kp"] for e in self._entries]
        self.full_scale = full_scale if full_scale and full_scale > 0 else 1.0
        self.width_px = float(width_px)
        self.strip = bool(strip)
        self._max_depth = max((e.get("base") or 0.0 for e in self._entries), default=0.0)
        self.update()

    def entries(self) -> List[Dict]:
        return list(self._entries)

    @property
    def depth_extent(self) -> float:
        return self._max_depth

    def set_selected(self, cpt_id: str) -> None:
        self.selected = str(cpt_id or "")
        self.update()

    # -- geometry ---------------------------------------------------------------
    def boundingRect(self):
        if not self._entries:
            return QRectF()
        view = self.viewRect()
        if view is None:
            return QRectF(self._kps[0], 0.0, max(self._kps[-1] - self._kps[0], 1e-6),
                          max(self._max_depth, 1.0))
        rect = QRectF(view)
        rect.setLeft(min(rect.left(), self._kps[0]))
        rect.setRight(max(rect.right(), self._kps[-1]))
        return rect

    def dataBounds(self, axis, frac=1.0, orthoRange=None):
        if not self._entries:
            return None
        if axis == 0:
            return (self._kps[0], self._kps[-1])
        return (0.0, self._max_depth)

    def viewRangeChanged(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def _px(self) -> float:
        """KP units per screen pixel."""
        width = self.pixelWidth()
        return width if width and math.isfinite(width) else 0.0

    def _visible(self, left: float, right: float):
        start = bisect.bisect_left(self._kps, left)
        for index in range(start, len(self._entries)):
            if self._kps[index] > right:
                break
            yield self._entries[index]

    # -- hit testing --------------------------------------------------------------
    def hit(self, kp: float, depth: Optional[float] = None,
            tolerance_px: float = 6.0) -> Optional[Dict]:
        """The CPT whose trace band (baseline to full scale, seabed to the
        deepest reading) contains ``kp`` (and ``depth`` when given)."""
        px = self._px()
        if not self._entries or px <= 0:
            return None
        if depth is not None:
            reach = abs(self.pixelHeight() or 0.0) * tolerance_px
            if depth > self._max_depth + reach:
                return None
        band = (self.width_px + _GAP_PX) * px
        strip = (_STRIP_PX + _GAP_PX) * px if self.strip else 0.0
        tolerance = tolerance_px * px
        best, best_distance = None, math.inf
        start = bisect.bisect_left(self._kps, kp - band - tolerance)
        for index in range(start, len(self._entries)):
            entry = self._entries[index]
            if entry["kp"] - strip - tolerance > kp:
                break
            if depth is not None and depth > (entry.get("base") or 0.0) + \
                    abs(self.pixelHeight() or 0.0) * tolerance_px:
                continue
            if entry["kp"] - strip - tolerance <= kp <= entry["kp"] + band + tolerance:
                distance = abs(kp - entry["kp"])
                if distance < best_distance:
                    best, best_distance = entry, distance
        return best

    @staticmethod
    def reading_at(entry: Dict, depth: float) -> Optional[int]:
        """Index of the reading nearest ``depth`` (None beyond the test)."""
        depths = entry.get("depths") or []
        if not depths or depth < -0.05 or depth > (entry.get("base") or depths[-1]) + 0.05:
            return None
        return min(range(len(depths)), key=lambda i: abs(depths[i] - depth))

    # -- painting -----------------------------------------------------------------
    def paint(self, painter, *_args) -> None:
        if not self._entries:
            return
        view = self.viewRect()
        px = self._px()
        if view is None or px <= 0:
            return
        top, bottom = min(view.top(), view.bottom()), max(view.top(), view.bottom())
        width = self.width_px * px
        strip = _STRIP_PX * px
        gap = _GAP_PX * px
        labels: List[Tuple[QPointF, str, bool]] = []
        for entry in self._visible(view.left() - width - gap, view.right() + strip + gap):
            kp = entry["kp"]
            depths, values = entry["depths"], entry["values"]
            base = entry.get("base") or (depths[-1] if depths else 0.0)
            painter.setPen(self._base_pen)
            painter.drawLine(QPointF(kp, 0.0), QPointF(kp, base))
            if self.strip and entry.get("zones"):
                self._paint_strip(painter, entry, kp - gap - strip, strip, top, bottom)
            path = QPainterPath()
            fill = QPainterPath()
            drawing = False
            run: List[QPointF] = []
            for depth, value in zip(depths, values):
                if depth < top - 0.5 or depth > bottom + 0.5 or value is None:
                    if run:
                        self._close_fill(fill, run, kp)
                        run = []
                    drawing = False
                    continue
                x = kp + gap + min(max(value / self.full_scale, 0.0), 1.15) * width
                point = QPointF(x, depth)
                if drawing:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                    drawing = True
                run.append(point)
            if run:
                self._close_fill(fill, run, kp)
            painter.setPen(pg.mkPen(None))
            painter.setBrush(self._fill)
            painter.drawPath(fill)
            selected = entry.get("cpt_id") == self.selected
            painter.setPen(self._selected_pen if selected else self._pen)
            painter.setBrush(pg.mkBrush(None))
            painter.drawPath(path)
            labels.append((QPointF(kp, 0.0), entry.get("label") or "", selected))
        self._paint_labels(painter, labels, top)

    @staticmethod
    def _close_fill(fill: QPainterPath, run: List[QPointF], kp: float) -> None:
        fill.moveTo(QPointF(kp, run[0].y()))
        for point in run:
            fill.lineTo(point)
        fill.lineTo(QPointF(kp, run[-1].y()))
        fill.closeSubpath()

    def _paint_strip(self, painter, entry, left, strip, top, bottom) -> None:
        depths, zones = entry["depths"], entry["zones"]
        painter.setPen(pg.mkPen(None))
        start = 0
        for i in range(1, len(depths) + 1):
            if i < len(depths) and zones[i] == zones[start]:
                continue
            zone = zones[start]
            lo = depths[start] if start == 0 else (depths[start - 1] + depths[start]) / 2.0
            hi = depths[i - 1] if i == len(depths) else (depths[i - 1] + depths[i]) / 2.0
            if zone in self._zone_brushes and hi > lo and hi >= top and lo <= bottom:
                painter.setBrush(self._zone_brushes[zone])
                painter.drawRect(QRectF(left, lo, strip, hi - lo))
            start = i

    def _paint_labels(self, painter, labels, top: float) -> None:
        """Names just below the seabed (or the top of the view), right of the
        baseline; a label overlapping the previous one is skipped unless it
        is the selected CPT."""
        if not labels:
            return
        transform = painter.transform()
        painter.save()
        painter.resetTransform()
        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.85))
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        last_right = -math.inf
        for anchor, text, selected in labels:
            if not text:
                continue
            point = transform.map(anchor)
            ceiling = transform.map(QPointF(anchor.x(), max(top, 0.0))).y()
            width = (metrics.horizontalAdvance(text) if hasattr(metrics, "horizontalAdvance")
                     else metrics.width(text))
            left = point.x() + 3
            if left < last_right + 6 and not selected:
                continue
            baseline = max(point.y(), ceiling) + metrics.ascent() + 2
            painter.setPen(pg.mkPen(None))
            painter.setBrush(pg.mkBrush(255, 255, 255, 200))
            painter.drawRect(QRectF(left - 2, baseline - metrics.ascent() - 1, width + 4,
                                    metrics.height() + 1))
            painter.setPen(pg.mkPen(QColor("#2b0a20") if selected else QColor(TRACE_COLOUR)))
            painter.drawText(QPointF(left, baseline), text)
            last_right = left + width
        painter.restore()


def build_entries(register, placements: List[Dict], channel: str, values: Dict,
                  depth_window: Optional[Tuple[float, float]] = None) -> Tuple[List[Dict], float]:
    """Trace entries for placed CPTs and a suggested full scale.

    ``values`` are the processing settings. The full scale is the largest
    value of ``channel`` among the placed CPTs (within ``depth_window``),
    rounded up to 1, 2, 2.5 or 5 × 10^n.
    """
    placed = [p for p in placements if p.get("status") == "placed"]
    if not placed:
        return [], 1.0
    from .cpt_register import side_text
    entries = []
    largest = 0.0
    lo, hi = depth_window or (-math.inf, math.inf)
    for placement in placed:
        cpt = register.by_id(placement["cpt_id"])
        if cpt is None:
            continue
        channels = register.channels(cpt, values)
        data = {"depth": channels.get("depth") or []}
        if not data["depth"]:
            continue
        series = channels.get(channel) or [None] * len(data["depth"])
        zones = channels.get("zone") or []
        for depth, value in zip(data["depth"], series):
            if value is not None and lo <= depth <= hi and math.isfinite(value):
                largest = max(largest, value)
        entries.append({"cpt_id": cpt["cpt_id"], "name": cpt.get("name") or "",
                        "kp": placement["kp"], "offset_m": placement.get("offset_m"),
                        "label": cpt.get("name") or "", "side": side_text(placement.get("offset_m")),
                        "depths": list(data["depth"]), "values": list(series),
                        "zones": list(zones) if any(z is not None for z in zones) else [],
                        "base": max(data["depth"]) if data["depth"] else 0.0,
                        "channels": channels})
    return entries, nice_ceiling(largest)
