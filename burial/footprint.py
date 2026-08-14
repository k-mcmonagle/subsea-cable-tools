# -*- coding: utf-8 -*-
"""Burial-tool footprint import (QGIS/OGR DXF reader — no new dependencies).

Produces the same body-fixed frame as the Import Ship Outline (DXF)
processing algorithm: drawing coordinates are shifted so the CRP sits at the
origin, scaled to metres, then rotated (0° = vehicle front along +Y). The
normalised outline is stored as WKT on the bp_tool row, so a registered tool
keeps its footprint when the registry JSON travels to another machine
without the source DXF.
"""

from __future__ import annotations

import os
from math import cos, radians, sin
from typing import Dict, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)

from ..qgis_compat import GEOMETRY_LINE, GEOMETRY_POLYGON
from . import geometry2d

WGS84 = "EPSG:4326"


class FootprintError(ValueError):
    """Raised when a DXF outline cannot be read."""


def map_vertices(geom: QgsGeometry, fn) -> QgsGeometry:
    """Rebuild a line/polygon geometry with ``fn(QgsPointXY) -> QgsPointXY``
    applied to every vertex (the shape-preserving per-vertex transform used
    by both the DXF import and KP placement)."""
    if geom.isMultipart():
        if geom.type() == GEOMETRY_LINE:
            parts = [[fn(pt) for pt in line]
                     for line in geom.asMultiPolyline()]
            return QgsGeometry.fromMultiPolylineXY(parts)
        parts = [[[fn(pt) for pt in ring] for ring in poly]
                 for poly in geom.asMultiPolygon()]
        return QgsGeometry.fromMultiPolygonXY(parts)
    if geom.type() == GEOMETRY_LINE:
        return QgsGeometry.fromPolylineXY([fn(pt) for pt in geom.asPolyline()])
    return QgsGeometry.fromPolygonXY(
        [[fn(pt) for pt in ring] for ring in geom.asPolygon()])


def _transform_geometry(geom: QgsGeometry, scale: float, rotation_deg: float,
                        offset_x: float, offset_y: float) -> QgsGeometry:
    """Per-vertex translate(CRP→origin) → scale → rotate (CCW-positive)."""
    rotation = radians(rotation_deg)
    cos_r, sin_r = cos(rotation), sin(rotation)

    def transform_point(pt) -> QgsPointXY:
        x = (pt.x() - offset_x) * scale
        y = (pt.y() - offset_y) * scale
        return QgsPointXY(x * cos_r - y * sin_r, x * sin_r + y * cos_r)

    return map_vertices(geom, transform_point)


def load_dxf_outline(dxf_path: str, scale: float = 1.0,
                     crp_offset_x: float = 0.0, crp_offset_y: float = 0.0,
                     rotation_deg: float = 0.0) -> Tuple[str, Dict]:
    """Read a DXF outline into body-fixed WKT (metres, CRP at origin).

    Only line and polygon entities contribute — a real GA drawing's POINT/
    TEXT/dimension entities are skipped rather than poisoning the outline.
    Returns ``(wkt, info)`` where ``info`` carries provenance and the
    outline's bounding dimensions for display/sanity checking. Raises
    ``FootprintError`` when the file cannot be read or holds no usable
    outline geometry.
    """
    layer = QgsVectorLayer(dxf_path, "tool_outline", "ogr")
    if not layer.isValid():
        raise FootprintError(
            f"The DXF could not be read: {os.path.basename(dxf_path)}")

    geoms = []
    for feat in layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty() \
                or geom.type() not in (GEOMETRY_LINE, GEOMETRY_POLYGON):
            continue
        geom = _transform_geometry(QgsGeometry(geom), float(scale or 1.0),
                                   float(rotation_deg or 0.0),
                                   float(crp_offset_x or 0.0),
                                   float(crp_offset_y or 0.0))
        if geom is not None and not geom.isNull() and not geom.isEmpty():
            geoms.append(geom)
    if not geoms:
        raise FootprintError("The DXF contains no usable outline geometry.")

    # One O(n) collection instead of an O(n²) chain of combine() calls; the
    # footprint is a display outline, so no dissolve is needed.
    merged = QgsGeometry.collectGeometry(geoms) if len(geoms) > 1 else geoms[0]
    if merged is None or merged.isNull() or merged.isEmpty():
        raise FootprintError("The DXF outline could not be assembled.")

    box = merged.boundingBox()
    info = {
        "source": os.path.basename(dxf_path),
        "scale": float(scale or 1.0),
        "crp_x": float(crp_offset_x or 0.0),
        "crp_y": float(crp_offset_y or 0.0),
        "rotation_deg": float(rotation_deg or 0.0),
        "length_m": box.height(),   # +Y is the vehicle's fore/aft axis
        "width_m": box.width(),
    }
    return merged.asWkt(3), info


# ---------------------------------------------------------------------------
# Placement along a route
# ---------------------------------------------------------------------------


def utm_crs_for(point_wgs84: QgsPointXY) -> QgsCoordinateReferenceSystem:
    """Metre-true working CRS at a WGS84 point (the Dynamic Buffer pattern):
    the local UTM zone, EPSG:326xx north / 327xx south."""
    zone = min(60, max(1, int((float(point_wgs84.x()) + 180.0) / 6.0) + 1))
    epsg = (32600 if float(point_wgs84.y()) >= 0.0 else 32700) + zone
    crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
    return crs if crs.isValid() else QgsCoordinateReferenceSystem("EPSG:3857")


def place_outline(outline: QgsGeometry, route, kp_km: float,
                  heading_offset_deg: float = 0.0,
                  heading_step_m: float = 20.0,
                  target_crs: Optional[QgsCoordinateReferenceSystem] = None,
                  transform_context=None
                  ) -> Tuple[Optional[QgsGeometry], Optional[float]]:
    """Place a body-fixed outline on the route at ``kp_km``.

    ``route`` is a WGS84 ``RouteFrame``; the outline is the body frame from
    the importers (metres, CRP at origin, front along +Y). Placement runs
    in the local UTM zone so shape and size are metre-true, and the heading
    is measured between two projected route points ± ``heading_step_m``
    around the KP — grid convergence comes out in the wash. Returns
    ``(geometry in target_crs (default WGS84), heading_deg)`` or
    ``(None, None)`` when the KP cannot be placed.
    """
    if outline is None or outline.isNull() or outline.isEmpty() \
            or route is None:
        return None, None
    anchor = route.point_at_kp(float(kp_km), clamp=True)
    if anchor is None:
        return None, None
    step_km = max(float(heading_step_m), 1.0) / 1000.0
    p_before = route.point_at_kp(float(kp_km) - step_km, clamp=True)
    p_after = route.point_at_kp(float(kp_km) + step_km, clamp=True)
    if p_before is None or p_after is None:
        return None, None

    context = transform_context or QgsProject.instance().transformContext()
    working = utm_crs_for(anchor)
    wgs84 = QgsCoordinateReferenceSystem(WGS84)
    to_working = QgsCoordinateTransform(wgs84, working, context)
    try:
        anchor_w = to_working.transform(anchor)
        before_w = to_working.transform(p_before)
        after_w = to_working.transform(p_after)
    except Exception:
        return None, None
    heading = geometry2d.grid_heading_deg(
        (before_w.x(), before_w.y()), (after_w.x(), after_w.y()))
    heading = (heading + float(heading_offset_deg)) % 360.0

    from math import cos as _cos, radians as _radians, sin as _sin
    h = _radians(heading)
    cos_h, sin_h = _cos(h), _sin(h)
    ax, ay = anchor_w.x(), anchor_w.y()

    def placed_point(pt) -> QgsPointXY:
        x, y = pt.x(), pt.y()
        return QgsPointXY(ax + x * cos_h + y * sin_h,
                          ay - x * sin_h + y * cos_h)

    geom = map_vertices(QgsGeometry(outline), placed_point)
    out_crs = target_crs or wgs84
    if out_crs != working:
        try:
            geom.transform(QgsCoordinateTransform(working, out_crs, context))
        except Exception:
            return None, None
    return geom, heading
