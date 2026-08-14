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
from typing import Dict, Tuple

from qgis.core import QgsGeometry, QgsPointXY, QgsVectorLayer

from ..qgis_compat import GEOMETRY_LINE, GEOMETRY_POLYGON


class FootprintError(ValueError):
    """Raised when a DXF outline cannot be read."""


def _transform_geometry(geom: QgsGeometry, scale: float, rotation_deg: float,
                        offset_x: float, offset_y: float) -> QgsGeometry:
    """Per-vertex translate(CRP→origin) → scale → rotate (CCW-positive)."""
    rotation = radians(rotation_deg)
    cos_r, sin_r = cos(rotation), sin(rotation)

    def transform_point(pt) -> QgsPointXY:
        x = (pt.x() - offset_x) * scale
        y = (pt.y() - offset_y) * scale
        return QgsPointXY(x * cos_r - y * sin_r, x * sin_r + y * cos_r)

    if geom.isMultipart():
        if geom.type() == GEOMETRY_LINE:
            parts = [[transform_point(pt) for pt in line]
                     for line in geom.asMultiPolyline()]
            return QgsGeometry.fromMultiPolylineXY(parts)
        parts = [[[transform_point(pt) for pt in ring] for ring in poly]
                 for poly in geom.asMultiPolygon()]
        return QgsGeometry.fromMultiPolygonXY(parts)
    if geom.type() == GEOMETRY_LINE:
        return QgsGeometry.fromPolylineXY(
            [transform_point(pt) for pt in geom.asPolyline()])
    return QgsGeometry.fromPolygonXY(
        [[transform_point(pt) for pt in ring] for ring in geom.asPolygon()])


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
