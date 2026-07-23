# -*- coding: utf-8 -*-
"""RplEditTool — drag-edit a registered RPL on the map canvas.

Interaction (extends the transit measure tool's patterns):
- hover within 10 px of an RPL position -> open-hand cursor + highlight
- left-press on a position, drag -> closed-hand; live rubber-band preview of
  the point and its two adjacent segments; live table preview via the
  controller (no layer writes during the drag)
- release -> snap (QgsSnappingUtils), commit through the engine + layer sync
- Ctrl+click on a segment -> insert a position (splits the segment)
- right-click on a position -> context menu (delete)
- Esc -> hand back to the previous map tool

The tool owns only canvas presentation; all model math goes through the
controller (the RPL Manager dock), which exposes:
    model_points_lonlat() -> List[(lon, lat)]
    preview_move(idx, lat, lon)
    commit_move(idx, lat, lon)
    commit_insert(seg_idx, lat, lon)
    commit_delete(idx)
"""

from __future__ import annotations

import math
from typing import List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QMenu
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker

from ..qgis_compat import GEOMETRY_LINE, qt_exec

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
HIT_TOLERANCE_PX = 10


class RplEditTool(QgsMapTool):
    def __init__(self, iface, controller):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.controller = controller

        self._canvas_points: List[QgsPointXY] = []
        self._to_canvas: Optional[QgsCoordinateTransform] = None
        self._to_wgs84: Optional[QgsCoordinateTransform] = None

        self._hover_idx: Optional[int] = None
        self._drag_idx: Optional[int] = None
        self._dragging = False

        self._drag_marker: Optional[QgsVertexMarker] = None
        self._snap_marker: Optional[QgsVertexMarker] = None
        self._preview_band: Optional[QgsRubberBand] = None

        self.edit_cursor = Qt.CursorShape.OpenHandCursor
        self.drag_cursor = Qt.CursorShape.ClosedHandCursor

    # -- lifecycle -------------------------------------------------------------
    def activate(self):
        super().activate()
        self.canvas.setCursor(Qt.CursorShape.CrossCursor)
        self.refresh_geometry()

    def deactivate(self):
        self._end_drag(cancel=True)
        self._clear_canvas_items()
        super().deactivate()

    def refresh_geometry(self):
        """Rebuild the canvas-CRS point cache from the controller's model."""
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        project = QgsProject.instance()
        self._to_canvas = QgsCoordinateTransform(WGS84, canvas_crs, project)
        self._to_wgs84 = QgsCoordinateTransform(canvas_crs, WGS84, project)
        self._canvas_points = []
        for lon, lat in self.controller.model_points_lonlat():
            try:
                self._canvas_points.append(self._to_canvas.transform(QgsPointXY(lon, lat)))
            except Exception:
                self._canvas_points.append(QgsPointXY(lon, lat))

    # -- hit testing ---------------------------------------------------------------
    def _tolerance_map_units(self) -> float:
        return HIT_TOLERANCE_PX * self.canvas.mapUnitsPerPixel()

    def _point_at(self, pt: QgsPointXY) -> Optional[int]:
        tol = self._tolerance_map_units()
        best_idx, best_dist = None, None
        for idx, candidate in enumerate(self._canvas_points):
            d = math.hypot(candidate.x() - pt.x(), candidate.y() - pt.y())
            if d <= tol and (best_dist is None or d < best_dist):
                best_idx, best_dist = idx, d
        return best_idx

    def _segment_at(self, pt: QgsPointXY) -> Optional[int]:
        tol = self._tolerance_map_units()
        best_idx, best_dist = None, None
        for i in range(len(self._canvas_points) - 1):
            d = _dist_point_segment(pt, self._canvas_points[i], self._canvas_points[i + 1])
            if d <= tol and (best_dist is None or d < best_dist):
                best_idx, best_dist = i, d
        return best_idx

    # -- events -----------------------------------------------------------------
    def canvasPressEvent(self, event):
        pt = self.toMapCoordinates(event.pos())
        idx = self._point_at(pt)

        if event.button() == Qt.MouseButton.RightButton:
            if idx is not None:
                self._context_menu(idx, event)
            return

        if event.button() != Qt.MouseButton.LeftButton:
            return

        if idx is not None:
            self._drag_idx = idx
            self._dragging = True
            self.canvas.setCursor(self.drag_cursor)
            self._show_drag_marker(self._canvas_points[idx])
            return

        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            seg_idx = self._segment_at(pt)
            if seg_idx is not None:
                wgs = self._to_wgs84.transform(pt)
                self.controller.commit_insert(seg_idx, wgs.y(), wgs.x())
                self.refresh_geometry()

    def canvasMoveEvent(self, event):
        pt = self.toMapCoordinates(event.pos())

        if self._dragging and self._drag_idx is not None:
            snapped = self._snap(event)
            self._show_drag_marker(snapped)
            self._update_preview_band(self._drag_idx, snapped)
            wgs = self._to_wgs84.transform(snapped)
            self.controller.preview_move(self._drag_idx, wgs.y(), wgs.x())
            return

        idx = self._point_at(pt)
        if idx is not None:
            if idx != self._hover_idx:
                self._hover_idx = idx
                self.canvas.setCursor(self.edit_cursor)
        elif self._hover_idx is not None:
            self._hover_idx = None
            self.canvas.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, event):
        if not (self._dragging and self._drag_idx is not None):
            return
        snapped = self._snap(event)
        wgs = self._to_wgs84.transform(snapped)
        idx = self._drag_idx
        self._end_drag()
        self.controller.commit_move(idx, wgs.y(), wgs.x())
        self.refresh_geometry()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            if self._dragging:
                self._end_drag(cancel=True)
                self.controller.cancel_preview()
            else:
                self.controller.edit_tool_escaped()

    # -- drag presentation ---------------------------------------------------------
    def _end_drag(self, cancel: bool = False):
        self._dragging = False
        self._drag_idx = None
        self.canvas.setCursor(Qt.CursorShape.CrossCursor)
        self._remove_marker("_drag_marker")
        self._remove_marker("_snap_marker")
        if self._preview_band is not None:
            self._preview_band.reset(GEOMETRY_LINE)

    def _show_drag_marker(self, pt: QgsPointXY):
        if self._drag_marker is None:
            marker = QgsVertexMarker(self.canvas)
            marker.setIconSize(14)
            marker.setPenWidth(3)
            marker.setColor(QColor(255, 140, 0))
            marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
            self._drag_marker = marker
        self._drag_marker.setCenter(pt)

    def _update_preview_band(self, idx: int, pt: QgsPointXY):
        if self._preview_band is None:
            band = QgsRubberBand(self.canvas, GEOMETRY_LINE)
            band.setColor(QColor(255, 140, 0, 200))
            band.setWidth(2)
            band.setLineStyle(Qt.PenStyle.DashLine)
            self._preview_band = band
        self._preview_band.reset(GEOMETRY_LINE)
        if idx > 0:
            self._preview_band.addPoint(self._canvas_points[idx - 1])
        self._preview_band.addPoint(pt)
        if idx < len(self._canvas_points) - 1:
            self._preview_band.addPoint(self._canvas_points[idx + 1])

    def _snap(self, event) -> QgsPointXY:
        match = self.canvas.snappingUtils().snapToMap(event.originalPixelPoint())
        if match.isValid():
            if self._snap_marker is None:
                marker = QgsVertexMarker(self.canvas)
                marker.setIconSize(12)
                marker.setPenWidth(2)
                marker.setColor(QColor(Qt.GlobalColor.magenta))
                marker.setIconType(QgsVertexMarker.ICON_BOX)
                self._snap_marker = marker
            self._snap_marker.setCenter(match.point())
            return match.point()
        self._remove_marker("_snap_marker")
        return self.toMapCoordinates(event.pos())

    def _remove_marker(self, attr: str):
        marker = getattr(self, attr)
        if marker is not None:
            self.canvas.scene().removeItem(marker)
            setattr(self, attr, None)

    def _clear_canvas_items(self):
        self._remove_marker("_drag_marker")
        self._remove_marker("_snap_marker")
        if self._preview_band is not None:
            self.canvas.scene().removeItem(self._preview_band)
            self._preview_band = None

    def _context_menu(self, idx: int, event):
        menu = QMenu()
        delete_action = menu.addAction(f"Delete position {idx}")
        chosen = qt_exec(menu, self.canvas.mapToGlobal(event.pos()))
        if chosen == delete_action:
            self.controller.commit_delete(idx)
            self.refresh_geometry()

    # -- markers for external flash (selection bus) ------------------------------
    def flash_position(self, lon: float, lat: float):
        try:
            pt = self._to_canvas.transform(QgsPointXY(lon, lat)) if self._to_canvas else QgsPointXY(lon, lat)
        except Exception:
            return
        marker = QgsVertexMarker(self.canvas)
        marker.setIconSize(16)
        marker.setPenWidth(3)
        marker.setColor(QColor(0, 200, 255))
        marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        marker.setCenter(pt)
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(1200, lambda: self.canvas.scene().removeItem(marker))


def _dist_point_segment(p: QgsPointXY, a: QgsPointXY, b: QgsPointXY) -> float:
    ax, ay, bx, by, px, py = a.x(), a.y(), b.x(), b.y(), p.x(), p.y()
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)
