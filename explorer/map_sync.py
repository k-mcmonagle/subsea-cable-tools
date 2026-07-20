# -*- coding: utf-8 -*-
"""Map-canvas highlighting for the Cable Lay Data Explorer.

Draws a vertex marker (single record) and a rubber band (a span between two
records / a finding extent) on the QGIS map canvas, transforming from the
WGS84 coordinates carried by the dataset into the canvas CRS. Optionally selects
the underlying feature in the source layer so it highlights natively too.
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsPointXY,
    QgsProject,
)
from qgis.gui import QgsRubberBand, QgsVertexMarker

from ..qgis_compat import GEOMETRY_LINE


class MapSyncController:
    """Owns the transient canvas graphics used to highlight records/findings."""

    def __init__(self, canvas):
        self.canvas = canvas
        self._wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        self._layer = None

        self.marker = QgsVertexMarker(canvas)
        self.marker.setColor(QColor(255, 0, 0))
        self.marker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.marker.setIconSize(14)
        self.marker.setPenWidth(3)
        self.marker.hide()

        self.rubber = QgsRubberBand(canvas, GEOMETRY_LINE)
        self.rubber.setColor(QColor(255, 0, 0, 180))
        self.rubber.setWidth(3)
        try:
            self.rubber.setLineStyle(Qt.PenStyle.DashLine)
        except Exception:
            pass
        self.rubber.reset(GEOMETRY_LINE)

    # -- configuration -----------------------------------------------------
    def set_layer(self, layer) -> None:
        self._layer = layer

    def _to_canvas(self, lon: float, lat: float) -> Optional[QgsPointXY]:
        try:
            point = QgsPointXY(float(lon), float(lat))
        except (TypeError, ValueError):
            return None
        dest = self.canvas.mapSettings().destinationCrs()
        if dest.isValid() and dest != self._wgs84:
            try:
                transform = QgsCoordinateTransform(self._wgs84, dest, QgsProject.instance())
                point = transform.transform(point)
            except Exception:
                return None
        return point

    # -- highlighting ------------------------------------------------------
    def highlight_point(self, lon: float, lat: float, pan: bool = False) -> None:
        point = self._to_canvas(lon, lat)
        self.rubber.reset(GEOMETRY_LINE)
        if point is None:
            self.marker.hide()
            return
        self.marker.setCenter(point)
        self.marker.show()
        if pan:
            self.canvas.setCenter(point)
            self.canvas.refresh()

    def highlight_span(self, lon1, lat1, lon2, lat2, pan: bool = False) -> None:
        p1 = self._to_canvas(lon1, lat1)
        p2 = self._to_canvas(lon2, lat2)
        self.rubber.reset(GEOMETRY_LINE)
        if p1 is None or p2 is None:
            self.highlight_point(lon1, lat1, pan=pan)
            return
        self.rubber.addPoint(p1, False)
        self.rubber.addPoint(p2, True)
        self.marker.setCenter(p1)
        self.marker.show()
        if pan:
            mid = QgsPointXY((p1.x() + p2.x()) / 2.0, (p1.y() + p2.y()) / 2.0)
            self.canvas.setCenter(mid)
            self.canvas.refresh()

    def select_feature(self, fid: Optional[int]) -> None:
        if self._layer is None or fid is None:
            return
        try:
            self._layer.selectByIds([int(fid)])
        except Exception:
            pass

    def clear(self) -> None:
        if self.marker is not None:
            self.marker.hide()
        if self.rubber is not None:
            self.rubber.reset(GEOMETRY_LINE)

    def cleanup(self) -> None:
        """Remove graphics items from the canvas scene (call on window close)."""
        for item in (self.marker, self.rubber):
            if item is None:
                continue
            try:
                self.canvas.scene().removeItem(item)
            except Exception:
                pass
        self.marker = None
        self.rubber = None
