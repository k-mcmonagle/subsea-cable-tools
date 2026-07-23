# -*- coding: utf-8 -*-
"""Map-canvas helpers for the Cable Lay Simulator (3D).

Two small pieces, both only importable inside QGIS (``qgis.gui``):

* :class:`PointSequenceTool` — a click tool that collects N canvas points
  (position, position+heading, laid ends A/B, ...) and hands them to a
  callback, then restores the previous map tool.
* :class:`SimulatorMapOverlay` — rubber-band overlay of the current result
  (ship footprint, cable plan paths, TDP/junction markers) drawn live on
  the canvas in map coordinates; nothing is added to the layer tree.

The simulator's local frame is metric and axis-aligned with the (projected)
project CRS; ``origin_map_xy`` translates local -> map.
"""

from __future__ import annotations

import math
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np


class PointSequenceTool:
    """Collect ``n_points`` left-clicks on the canvas, then call back.

    ``on_done(points)`` receives a list of (x, y) tuples in the project
    CRS; ``on_cancel()`` fires on right-click or Escape-style deactivation;
    ``on_progress(i)`` fires after each accepted click with the count so
    far (use it to prompt for click i+1). The previously active map tool is
    restored either way.
    """

    def __init__(self, canvas, n_points: int, on_done: Callable[[List[Tuple[float, float]]], None],
                 on_cancel: Optional[Callable[[], None]] = None,
                 on_progress: Optional[Callable[[int], None]] = None):
        from qgis.gui import QgsMapToolEmitPoint, QgsVertexMarker

        self._canvas = canvas
        self._n = max(1, int(n_points))
        self._on_done = on_done
        self._on_cancel = on_cancel
        self._on_progress = on_progress
        self._points: List[Tuple[float, float]] = []
        self._markers: List[object] = []
        self._prev_tool = canvas.mapTool()
        self._marker_cls = QgsVertexMarker
        self._done = False

        tool = QgsMapToolEmitPoint(canvas)
        tool.canvasClicked.connect(self._clicked)
        tool.deactivated.connect(self._deactivated)
        self._tool = tool
        canvas.setMapTool(tool)

    # ------------------------------------------------------------- events

    def _clicked(self, point, button) -> None:
        from qgis.PyQt.QtCore import Qt

        right = getattr(getattr(Qt, "MouseButton", Qt), "RightButton")
        if button == right:
            self._finish(cancelled=True)
            return
        self._points.append((point.x(), point.y()))
        self._add_marker(point)
        if len(self._points) >= self._n:
            self._finish(cancelled=False)
        elif self._on_progress is not None:
            self._on_progress(len(self._points))

    def _deactivated(self) -> None:
        if not self._done:
            self._finish(cancelled=True, restore=False)

    def _add_marker(self, point) -> None:
        from qgis.PyQt.QtGui import QColor

        m = self._marker_cls(self._canvas)
        m.setCenter(point)
        m.setColor(QColor(255, 140, 0))
        m.setIconType(self._marker_cls.ICON_CROSS)
        m.setIconSize(12)
        m.setPenWidth(2)
        self._markers.append(m)

    def _finish(self, cancelled: bool, restore: bool = True) -> None:
        if self._done:
            return
        self._done = True
        for m in self._markers:
            try:
                self._canvas.scene().removeItem(m)
            except Exception:
                pass
        self._markers = []
        if restore:
            try:
                self._canvas.setMapTool(self._prev_tool)
            except Exception:
                pass
        if cancelled:
            if self._on_cancel is not None:
                self._on_cancel()
        else:
            self._on_done(list(self._points))

    def cancel(self) -> None:
        self._finish(cancelled=True)


def bearing_deg(from_xy: Tuple[float, float], to_xy: Tuple[float, float]) -> float:
    """Compass bearing (deg clockwise from grid north) between map points."""
    dx = float(to_xy[0]) - float(from_xy[0])
    dy = float(to_xy[1]) - float(from_xy[1])
    return math.degrees(math.atan2(dx, dy)) % 360.0


class SimulatorMapOverlay:
    """Live canvas overlay of a solved scene (no layers created)."""

    _CABLE_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]

    def __init__(self, canvas):
        self._canvas = canvas
        self._vessel_band = None
        self._cable_bands: List[object] = []
        self._markers: List[object] = []

    # ------------------------------------------------------------- update

    def update(self, scene, origin_map_xy: Tuple[float, float],
               crs_authid: str) -> None:
        """Redraw the overlay from a SceneData in local coords.

        Local metric geometry is georeferenced through the AEQD frame at
        ``origin_map_xy`` (in CRS ``crs_authid``, the canvas CRS), so the
        drawn scale is true in any coordinate system."""
        from qgis.core import QgsGeometry, QgsPointXY
        from qgis.gui import QgsRubberBand, QgsVertexMarker
        from qgis.PyQt.QtGui import QColor

        from ....qgis_compat import GEOMETRY_LINE, GEOMETRY_POLYGON
        from .qgis_adapters import local_frame_transforms

        self.clear()
        to_map, _ = local_frame_transforms(origin_map_xy, crs_authid)

        def m2map(x: float, y: float) -> "QgsPointXY":
            return to_map.transform(QgsPointXY(float(x), float(y)))

        # Cable plan paths.
        for k, path in enumerate(getattr(scene, "cables", []) or []):
            xyz = np.asarray(path.xyz, dtype=float)
            if xyz.ndim != 2 or len(xyz) < 2:
                continue
            xyz = xyz[np.all(np.isfinite(xyz[:, :2]), axis=1)]
            if len(xyz) < 2:
                continue
            band = QgsRubberBand(self._canvas, GEOMETRY_LINE)
            color = QColor(str(getattr(path, "color", None)
                               or self._CABLE_COLORS[k % len(self._CABLE_COLORS)]))
            color.setAlpha(200)
            band.setColor(color)
            band.setWidth(3)
            pts = [m2map(x, y) for x, y in xyz[:, :2]]
            band.setToGeometry(QgsGeometry.fromPolylineXY(pts), None)
            self._cable_bands.append(band)

        # Vessel footprint (waterline outline).
        vessel = getattr(scene, "vessel", None)
        if vessel is not None and np.all(np.isfinite(np.asarray(vessel.xy, dtype=float))):
            from .scene import vessel_footprint

            foot = vessel_footprint(vessel)
            band = QgsRubberBand(self._canvas, GEOMETRY_POLYGON)
            fill = QColor(70, 80, 90, 90)
            band.setColor(fill)
            band.setStrokeColor(QColor(235, 240, 245))
            band.setWidth(2)
            pts = [m2map(x, y) for x, y in foot]
            band.setToGeometry(QgsGeometry.fromPolygonXY([pts]), None)
            self._vessel_band = band

        # Point markers (TDP, junctions...).
        for m in getattr(scene, "markers", []) or []:
            if not (math.isfinite(float(m.xyz[0])) and math.isfinite(float(m.xyz[1]))):
                continue
            vm = QgsVertexMarker(self._canvas)
            vm.setCenter(m2map(float(m.xyz[0]), float(m.xyz[1])))
            vm.setColor(QColor(str(getattr(m, "color", "#d62728"))))
            vm.setIconType(QgsVertexMarker.ICON_BOX
                           if getattr(m, "kind", "") == "junction"
                           else QgsVertexMarker.ICON_X)
            vm.setIconSize(10)
            vm.setPenWidth(2)
            self._markers.append(vm)

    def clear(self) -> None:
        for band in ([self._vessel_band] if self._vessel_band else []) + self._cable_bands:
            try:
                band.reset()
                self._canvas.scene().removeItem(band)
            except Exception:
                pass
        for m in self._markers:
            try:
                self._canvas.scene().removeItem(m)
            except Exception:
                pass
        self._vessel_band = None
        self._cable_bands = []
        self._markers = []
