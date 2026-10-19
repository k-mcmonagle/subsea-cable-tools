# -*- coding: utf-8 -*-
"""Picking a KP range on the KP Mouse Tool's route for a range comment.

The start KP is fixed where the user chose *Comment over KP range…*; the
end follows the cursor's nearest KP and the route section between them is
drawn as a preview. The map tool forwards its events: a left click ends the
range, Escape or a right click cancels it.
"""

from __future__ import annotations

from typing import Optional, Tuple

from qgis.gui import QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtGui import QColor

from ..maptools.canvas_items import remove_canvas_item
from ..qgis_compat import GEOMETRY_LINE

PREVIEW_COLOR = QColor(230, 120, 0, 190)


class RangePicker:
    """A KP range being picked along ``route`` (a ``RouteFrame`` in the map CRS)."""

    def __init__(self, canvas, route, start_kp_km: float):
        self.canvas = canvas
        self.route = route
        self.start_kp = route.clamp_kp(float(start_kp_km))
        self.end_kp: Optional[float] = None
        self.band = QgsRubberBand(canvas, GEOMETRY_LINE)
        self.band.setColor(PREVIEW_COLOR)
        self.band.setWidth(5)
        self.marker = QgsVertexMarker(canvas)
        self.marker.setColor(PREVIEW_COLOR)
        self.marker.setIconType(QgsVertexMarker.ICON_BOX)
        self.marker.setIconSize(11)
        self.marker.setPenWidth(3)
        start = route.point_at_kp(self.start_kp, clamp=True)
        if start is not None:
            self.marker.setCenter(start)

    def update(self, kp_km: float) -> str:
        """Move the range end to ``kp_km``; returns the status text."""
        self.end_kp = self.route.clamp_kp(float(kp_km))
        lo, hi = sorted((self.start_kp, self.end_kp))
        geom = self.route.extract_segment(lo, hi) if hi - lo > 1e-6 else None
        if geom is not None and not geom.isEmpty():
            self.band.setToGeometry(geom, None)
        else:
            self.band.reset(GEOMETRY_LINE)
        return self.status_text()

    def status_text(self) -> str:
        if self.end_kp is None:
            return f"Comment range from KP {self.start_kp:.3f}: click the end of the range (Esc cancels)."
        lo, hi = sorted((self.start_kp, self.end_kp))
        return (f"Comment range KP {lo:.3f} – {hi:.3f} ({(hi - lo) * 1000.0:,.0f} m): click to finish, "
                "Esc or right-click cancels.")

    def kps(self) -> Optional[Tuple[float, float]]:
        if self.end_kp is None:
            return None
        lo, hi = sorted((self.start_kp, self.end_kp))
        return (lo, hi) if hi - lo > 1e-6 else None

    def dispose(self) -> None:
        remove_canvas_item(self.band)
        remove_canvas_item(self.marker)
        self.band = self.marker = None
