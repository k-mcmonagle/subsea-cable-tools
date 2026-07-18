# -*- coding: utf-8 -*-
"""Lightweight map-canvas overlay and identify-tool lifecycle for the planner."""

from __future__ import annotations

from qgis.PyQt.QtGui import QBrush, QColor, QFont, QPen
from qgis.PyQt.QtWidgets import QGraphicsTextItem
from qgis.gui import QgsMapToolIdentifyFeature, QgsRubberBand, QgsVertexMarker

from ..qgis_compat import GEOMETRY_LINE, PEN_STYLE_DASH


class PlannerMapOverlay:
    def __init__(self, canvas):
        self.canvas = canvas
        self._items = {}
        self.canvas.extentsChanged.connect(self._reposition_labels)

    def _resource_items(self, resource_id, color_hex):
        if resource_id in self._items:
            return self._items[resource_id]
        remaining = QgsRubberBand(self.canvas, GEOMETRY_LINE)
        remaining.setColor(QColor("#888888"))
        remaining.setWidth(3)
        remaining.setLineStyle(PEN_STYLE_DASH)
        completed = QgsRubberBand(self.canvas, GEOMETRY_LINE)
        completed.setColor(QColor("#2ca25f"))
        completed.setWidth(4)
        marker = QgsVertexMarker(self.canvas)
        marker.setColor(QColor(color_hex or "#1f78b4"))
        marker.setFillColor(QColor(color_hex or "#1f78b4"))
        marker.setIconSize(12)
        marker.setPenWidth(2)
        marker.setIconType(getattr(QgsVertexMarker, "ICON_CIRCLE",
                                   getattr(QgsVertexMarker, "ICON_BOX", 2)))
        marker.hide()
        label = _PlannerLabelItem()
        label.setFont(QFont("Sans Serif", 9))
        label.setDefaultTextColor(QColor("#202020"))
        label.setZValue(10000)
        label.hide()
        self.canvas.scene().addItem(label)
        self._items[resource_id] = (completed, remaining, marker, label)
        return self._items[resource_id]

    def update_resource(self, resource_id, frame, chainage_m, color_hex="#1f78b4",
                        direction="forward", label_text=""):
        completed, remaining, marker, label = self._resource_items(resource_id, color_hex)
        completed.reset(GEOMETRY_LINE)
        remaining.reset(GEOMETRY_LINE)
        if frame is None or chainage_m is None:
            marker.hide()
            label.hide()
            return
        total = frame.total_length_m
        chainage = min(total, max(0.0, float(chainage_m)))
        if direction == "reverse":
            done_geom = frame.extract_segment(chainage / 1000.0, total / 1000.0)
            rest_geom = frame.extract_segment(0.0, chainage / 1000.0)
        else:
            done_geom = frame.extract_segment(0.0, chainage / 1000.0)
            rest_geom = frame.extract_segment(chainage / 1000.0, total / 1000.0)
        if done_geom is not None and not done_geom.isEmpty():
            completed.setToGeometry(done_geom, None)
        if rest_geom is not None and not rest_geom.isEmpty():
            remaining.setToGeometry(rest_geom, None)
        point = frame.point_at_kp(chainage / 1000.0, clamp=True)
        if point is not None:
            marker.setCenter(point)
            marker.show()
            self._set_label(label, point, label_text)

    def show_point(self, resource_id, point, color_hex="#1f78b4", label_text=""):
        completed, remaining, marker, label = self._resource_items(resource_id, color_hex)
        completed.reset(GEOMETRY_LINE)
        remaining.reset(GEOMETRY_LINE)
        if point is None:
            marker.hide()
            label.hide()
        else:
            marker.setCenter(point)
            marker.show()
            self._set_label(label, point, label_text)

    def hold_resource(self, resource_id, color_hex="#1f78b4", label_text=""):
        """Keep the last marker position while clearing route progress bands."""
        completed, remaining, _marker, label = self._resource_items(resource_id, color_hex)
        completed.reset(GEOMETRY_LINE)
        remaining.reset(GEOMETRY_LINE)
        point = getattr(label, "map_point", None)
        if point is not None:
            self._set_label(label, point, label_text)
        else:
            label.hide()

    def hide_resources_not_in(self, resource_ids):
        visible = set(resource_ids)
        for resource_id, (completed, remaining, marker, label) in self._items.items():
            if resource_id not in visible:
                completed.reset(GEOMETRY_LINE)
                remaining.reset(GEOMETRY_LINE)
                marker.hide()
                label.hide()

    def _set_label(self, label, point, text):
        label.map_point = point
        label.setPlainText(str(text or ""))
        label.setVisible(bool(text))
        if text:
            self._position_label(label)

    def _position_label(self, label):
        point = getattr(label, "map_point", None)
        if point is None:
            return
        pixel = self.canvas.mapSettings().mapToPixel().transform(point)
        label.setPos(pixel.x() + 12, pixel.y() - label.boundingRect().height() - 12)

    def _reposition_labels(self):
        for _completed, _remaining, _marker, label in self._items.values():
            if label.isVisible():
                self._position_label(label)

    def clear(self):
        scene = self.canvas.scene()
        for completed, remaining, marker, label in self._items.values():
            scene.removeItem(completed)
            scene.removeItem(remaining)
            scene.removeItem(marker)
            scene.removeItem(label)
        self._items.clear()


class _PlannerLabelItem(QGraphicsTextItem):
    """Small readable canvas label positioned beside a resource marker."""

    def __init__(self):
        super().__init__()
        self.map_point = None

    def boundingRect(self):
        return super().boundingRect().adjusted(-4, -2, 4, 2)

    def paint(self, painter, option, widget=None):
        painter.setPen(QPen(QColor(70, 70, 70, 210)))
        painter.setBrush(QBrush(QColor(255, 255, 255, 225)))
        painter.drawRoundedRect(self.boundingRect(), 3, 3)
        super().paint(painter, option, widget)


class FeaturePickSession:
    """Temporarily install QGIS's identify-feature tool, then restore the old tool."""
    def __init__(self, canvas):
        self.canvas = canvas
        self.tool = None
        self.previous_tool = None

    def start(self, layer, callback):
        self.cancel()
        self.previous_tool = self.canvas.mapTool()
        self.tool = QgsMapToolIdentifyFeature(self.canvas, layer)

        def identified(feature):
            callback(layer, feature)
            self.cancel()

        self.tool.featureIdentified.connect(identified)
        self.canvas.setMapTool(self.tool)

    def cancel(self):
        if self.tool is not None and self.canvas.mapTool() is self.tool:
            if self.previous_tool is not None:
                self.canvas.setMapTool(self.previous_tool)
            else:
                self.canvas.unsetMapTool(self.tool)
        self.tool = None
        self.previous_tool = None
