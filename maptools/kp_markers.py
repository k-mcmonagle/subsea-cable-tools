"""Temporary station labels for the active KP mouse tool."""
import math

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QPen
from qgis.PyQt.QtWidgets import (QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsRectItem,
                                 QGraphicsSimpleTextItem)

from .canvas_items import remove_canvas_item


def station_step(minimum):
    """Round up to a 1, 2 or 5 interval, with one metre as the finest scale."""
    minimum = max(0.001, minimum)
    base = 10 ** math.floor(math.log10(minimum))
    return next(n * base for n in (1, 2, 5, 10) if n * base >= minimum)


class KpMarkers:
    def __init__(self, canvas, interval, leaders):
        self.canvas = canvas
        self.interval = interval  # km, zero for automatic
        self.leaders = leaders
        self.items = []

    def clear(self):
        for item in self.items:
            remove_canvas_item(item)
        self.items.clear()

    def draw(self, route):
        self.clear()
        if route is None:
            return
        ranges = route.kp_ranges_in_rect(self.canvas.extent())
        if not ranges:
            return
        km_per_pixel = self.canvas.scale() * 0.0000254 / max(1, self.canvas.mapSettings().outputDpi())
        step = self.interval or station_step(120 * km_per_pixel)
        # Thin very dense fixed intervals by a whole multiple to keep station alignment.
        visible_length = sum(end - start for start, end in ranges)
        step *= max(1, math.ceil(visible_length / (160 * step)))
        transform = self.canvas.getCoordinateTransform()
        view = self.canvas.mapToScene(self.canvas.viewport().rect()).boundingRect()
        occupied = []
        for start, end in ranges:
            for i in range(math.ceil(start / step - 1e-9), math.floor(end / step + 1e-9) + 1):
                kp = i * step
                point = route.point_at_kp(kp, clamp=True)
                if point is None:
                    continue
                screen = transform.transform(point)
                x, y = round(screen.x()), round(screen.y())
                if self.canvas.viewport().rect().contains(x, y):
                    self._add(self.canvas.mapToScene(x, y), kp, occupied, view)
                if len(self.items) >= 160:
                    return
        if not self.items and not self.interval:
            # A short visible route can lie entirely between two automatic stations.
            start, end = max(ranges, key=lambda span: span[1] - span[0])
            kp = (start + end) / 2
            point = route.point_at_kp(kp, clamp=True)
            if point is not None:
                screen = transform.transform(point)
                anchor = self.canvas.mapToScene(round(screen.x()), round(screen.y()))
                if view.contains(anchor):
                    self._add(anchor, kp, occupied, view)

    def _add(self, anchor, kp, occupied, view):
        text = QGraphicsSimpleTextItem(f"KP {kp:.3f}")
        offset = 18 if self.leaders else 5
        box = text.boundingRect().adjusted(-3, -1, 3, 1)
        x = offset if anchor.x() + box.width() + offset < view.right() else -box.width() - offset
        y = -box.height() - offset if anchor.y() - box.height() - offset > view.top() else offset
        position = anchor + QPointF(x, y)
        bounds = box.translated(position)
        if any(bounds.intersects(other) for other in occupied):
            return
        occupied.append(bounds)
        ink = QColor("#245267")
        dot = QGraphicsEllipseItem(QRectF(-2.5, -2.5, 5, 5))
        dot.setBrush(QBrush(ink))
        dot.setPen(QPen(QColor("white")))
        dot.setZValue(100)
        dot.setPos(anchor)
        if self.leaders:
            line = QGraphicsLineItem(0, 0, offset if x > 0 else -offset, offset if y > 0 else -offset, dot)
            line.setPen(QPen(ink))
            line.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        plate = QGraphicsRectItem(box, dot)
        plate.setPos(position - anchor)
        plate.setBrush(QBrush(QColor(255, 255, 255, 235)))
        plate.setPen(QPen(ink))
        text.setParentItem(plate)
        text.setBrush(QBrush(ink))
        for item in (dot, plate, text):
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.canvas.scene().addItem(dot)
        self.items.append(dot)
