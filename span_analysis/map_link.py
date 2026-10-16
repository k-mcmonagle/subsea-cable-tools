# -*- coding: utf-8 -*-
"""Link the profile to the map: a KP marker on the route, highlighted KP ranges,
and a map tool that picks a KP by clicking near the route."""

from __future__ import annotations

import math
from typing import Callable, Optional

from qgis.gui import QgsMapTool

from ..maptools.map_sync import MapSyncController
from ..qgis_compat import MOUSE_BUTTON_LEFT
from .sources import RouteGeo


class MapLink:
    """KP-aware map highlighting for one route (None: no map, e.g. a CSV profile)."""

    def __init__(self, canvas):
        self.canvas = canvas
        self.sync = MapSyncController(canvas) if canvas is not None else None
        self.route: Optional[RouteGeo] = None

    def set_route(self, route: Optional[RouteGeo]) -> None:
        self.route = route
        self.clear()

    def hover_kp(self, kp: Optional[float]) -> None:
        if self.sync is None or self.route is None or kp is None or not math.isfinite(kp):
            if self.sync is not None:
                self.sync.clear_hover()
            return
        point = self.route.lonlat_at_kp(kp)
        if point is None:
            self.sync.clear_hover()
        else:
            self.sync.hover_point(*point)

    def show_kp(self, kp: float, pan=True) -> None:
        if self.sync is None or self.route is None:
            return
        point = self.route.lonlat_at_kp(kp)
        if point is not None:
            self.sync.highlight_point(*point, pan=pan)

    def show_range(self, kp_a: float, kp_b: float, zoom: bool = False) -> None:
        if self.sync is None or self.route is None:
            return
        if abs(kp_b - kp_a) < 1e-6:
            self.show_kp(kp_a, pan=zoom or "if_outside")
            return
        self.sync.highlight_polyline(self.route.polyline_lonlat(kp_a, kp_b), zoom=zoom)

    def clear(self) -> None:
        if self.sync is not None:
            self.sync.clear()

    def cleanup(self) -> None:
        if self.sync is not None:
            self.sync.cleanup()
            self.sync = None


class KpPickTool(QgsMapTool):
    """Click on the map: the KP of the nearest route position goes to ``on_kp``."""

    def __init__(self, canvas, link: MapLink, on_kp: Callable[[float], None]):
        super().__init__(canvas)
        self._link = link
        self._on_kp = on_kp

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 (QGIS API)
        if event.button() != MOUSE_BUTTON_LEFT or self._link.route is None:
            return
        point = self.toMapCoordinates(event.pos())
        kp = self._link.route.kp_at(point, self.canvas().mapSettings().destinationCrs())
        if kp is not None:
            self._on_kp(kp)
