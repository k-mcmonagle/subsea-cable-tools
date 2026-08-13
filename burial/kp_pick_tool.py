# -*- coding: utf-8 -*-
"""One-shot KP pick map tool for the Burial Planner.

Left-click the canvas: the click is snapped to the plan's route, converted
to a KP and delivered to the ``on_picked`` callback; right-click or Esc
cancels. Either way the tool deactivates itself and calls ``on_finished``
so the dock can restore the previously active map tool. While the tool is
live, a marker previews the snapped route position under the cursor.
"""

from __future__ import annotations

from typing import Callable, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
)
from qgis.gui import QgsMapTool, QgsVertexMarker
from qgis.PyQt.QtCore import Qt

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


class KpPickTool(QgsMapTool):
    """Single-use tool: pick one KP on the route, then hand control back."""

    def __init__(self, canvas, route,
                 on_picked: Callable[[float], None],
                 on_finished: Optional[Callable[[], None]] = None):
        super().__init__(canvas)
        self._route = route
        self._on_picked = on_picked
        self._on_finished = on_finished
        self._finished = False
        self._marker = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -- transforms ----------------------------------------------------------
    def _to_wgs84(self, point: QgsPointXY) -> Optional[QgsPointXY]:
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        if canvas_crs == WGS84:
            return point
        try:
            transform = QgsCoordinateTransform(canvas_crs, WGS84,
                                               QgsProject.instance())
            return transform.transform(point)
        except Exception:
            return None

    def _to_canvas(self, point: QgsPointXY) -> Optional[QgsPointXY]:
        canvas_crs = self.canvas().mapSettings().destinationCrs()
        if canvas_crs == WGS84:
            return point
        try:
            transform = QgsCoordinateTransform(WGS84, canvas_crs,
                                               QgsProject.instance())
            return transform.transform(point)
        except Exception:
            return None

    def _snap(self, event_pos):
        """(kp_km, snapped canvas point) for a mouse position, or (None, None)."""
        map_point = self.toMapCoordinates(event_pos)
        wgs_point = self._to_wgs84(map_point)
        if wgs_point is None:
            return None, None
        try:
            hit = self._route.kp_at_point(wgs_point)
        except Exception:
            return None, None
        if hit.snapped_xy is None:
            return None, None
        return float(hit.kp_km), self._to_canvas(QgsPointXY(hit.snapped_xy))

    # -- events --------------------------------------------------------------
    def canvasMoveEvent(self, event) -> None:  # noqa: N802 (QGIS API)
        _kp, snapped = self._snap(event.pos())
        if snapped is None:
            self._hide_marker()
            return
        if self._marker is None:
            self._marker = QgsVertexMarker(self.canvas())
            self._marker.setColor(Qt.GlobalColor.blue)
            self._marker.setIconSize(12)
            self._marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
            self._marker.setPenWidth(2)
        self._marker.setCenter(snapped)
        self._marker.show()

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 (QGIS API)
        if event.button() == Qt.MouseButton.RightButton:
            self._finish()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        kp, snapped = self._snap(event.pos())
        if kp is None:
            return
        callback = self._on_picked
        self._finish()
        if callback is not None:
            callback(round(kp, 3))

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 (QGIS API)
        if event.key() == Qt.Key.Key_Escape:
            self._finish()

    def deactivate(self) -> None:
        # The user switching to another map tool cancels the pick; the
        # _finished guard keeps the normal pick path from re-entering.
        self._finish()
        super().deactivate()

    # -- lifecycle -----------------------------------------------------------
    def _hide_marker(self) -> None:
        if self._marker is not None:
            try:
                self._marker.hide()
                scene = self._marker.scene()
                if scene is not None:
                    scene.removeItem(self._marker)
            except (AttributeError, RuntimeError):
                pass
            self._marker = None

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._hide_marker()
        if self._on_finished is not None:
            try:
                self._on_finished()
            except Exception:
                pass
