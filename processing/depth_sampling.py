# -*- coding: utf-8 -*-
"""Shared depth/elevation sampling helpers.

Extracted from AddDepthToPointLayerAlgorithm and
DynamicBufferLayCorridorAlgorithm so the same sampling behaviour is available
to processing algorithms and to interactive tools (e.g. the Cable Route
Workbench DepthService) without a QgsProcessingContext.

Sampler tuples:
- raster sampler:  (QgsRasterLayer, Optional[QgsCoordinateTransform])
- contour sampler: (QgsVectorLayer, depth_field_name, Optional[QgsCoordinateTransform])

All query points are in the source CRS the samplers were built with; the
stored transforms convert into each layer's CRS.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from ..kp_range_utils import make_distance_area

RasterSampler = Tuple[QgsRasterLayer, Optional[QgsCoordinateTransform]]
ContourSampler = Tuple[QgsVectorLayer, str, Optional[QgsCoordinateTransform]]


def build_raster_samplers(
    rasters: Sequence[QgsRasterLayer],
    source_crs,
) -> List[RasterSampler]:
    """Pair each raster with a transform from ``source_crs`` into its CRS."""
    samplers: List[RasterSampler] = []
    for r in rasters:
        if not r:
            continue
        transform = None
        if r.crs() != source_crs:
            try:
                transform = QgsCoordinateTransform(source_crs, r.crs(), QgsProject.instance())
            except Exception:
                transform = None
        samplers.append((r, transform))
    return samplers


def build_contour_samplers(
    contour_layers: Sequence[Optional[QgsVectorLayer]],
    depth_fields: Sequence[str],
    source_crs,
) -> List[ContourSampler]:
    """Pair each contour layer with its depth field and CRS transform.

    A blank depth field means "fall back to the first attribute" at sample
    time.
    """
    out: List[ContourSampler] = []
    for i, lyr in enumerate(contour_layers):
        if not lyr:
            continue
        depth_field = depth_fields[i] if i < len(depth_fields) else ''
        transform = None
        if lyr.crs() != source_crs:
            try:
                transform = QgsCoordinateTransform(source_crs, lyr.crs(), QgsProject.instance())
            except Exception:
                transform = None
        out.append((lyr, depth_field, transform))
    return out


def sample_rasters(
    point: QgsPointXY,
    raster_samplers: Sequence[RasterSampler],
    band: int = 1,
) -> Tuple[Optional[float], Optional[str], List[Tuple[str, Optional[float]]]]:
    """Sample every raster at ``point``.

    Returns ``(best_value, best_source_name, all_values)`` where ``best`` is
    the first raster (in order) with valid data.
    """
    best_val: Optional[float] = None
    best_src: Optional[str] = None
    all_vals: List[Tuple[str, Optional[float]]] = []

    band = int(band) if band and int(band) > 0 else 1

    for raster, transform in raster_samplers:
        sample_pt = point
        if transform is not None:
            try:
                sample_pt = transform.transform(point)
            except Exception:
                all_vals.append((raster.name(), None))
                continue

        try:
            val, ok = raster.dataProvider().sample(sample_pt, band)
        except Exception:
            ok = False
            val = None

        if ok and val is not None:
            try:
                fval = float(val)
            except Exception:
                fval = None
        else:
            fval = None

        all_vals.append((raster.name(), fval))
        if best_val is None and fval is not None:
            best_val = fval
            best_src = raster.name()

    return best_val, best_src, all_vals


def sample_contours(
    point: QgsPointXY,
    contour_samplers: Sequence[ContourSampler],
    search_radius_m: float,
    transform_context,
    project=None,
) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    """Depth from the nearest contour feature across all contour samplers.

    Returns ``(best_depth, best_source_layer_name, best_distance_m)``.
    ``search_radius_m`` of 0 means unlimited.
    """
    best_depth = None
    best_dist = None
    best_src = None

    for lyr, depth_field, transform in contour_samplers:
        if not lyr:
            continue

        query_point = point
        if transform is not None:
            try:
                query_point = transform.transform(point)
            except Exception:
                continue

        pt_geom = QgsGeometry.fromPointXY(query_point)

        # Filter candidates by bbox if a search radius is provided.
        feat_iter = None
        if search_radius_m and search_radius_m > 0:
            if lyr.crs().isGeographic():
                # Approx meters -> degrees
                lat = query_point.y()
                deg_lat = search_radius_m / 111320.0
                cos_lat = max(0.1, abs(math.cos(math.radians(lat))))
                deg_lon = search_radius_m / (111320.0 * cos_lat)
                rect = pt_geom.boundingBox()
                rect.setXMinimum(rect.xMinimum() - deg_lon)
                rect.setXMaximum(rect.xMaximum() + deg_lon)
                rect.setYMinimum(rect.yMinimum() - deg_lat)
                rect.setYMaximum(rect.yMaximum() + deg_lat)
                request = QgsFeatureRequest().setFilterRect(rect)
                feat_iter = lyr.getFeatures(request)
            else:
                rect = pt_geom.buffer(search_radius_m, 8).boundingBox()
                request = QgsFeatureRequest().setFilterRect(rect)
                feat_iter = lyr.getFeatures(request)

        if feat_iter is None:
            feat_iter = lyr.getFeatures()

        dist_area = None
        if lyr.crs().isGeographic():
            dist_area = make_distance_area(lyr.crs(), transform_context, project=project)

        for feat in feat_iter:
            g = feat.geometry()
            if not g or g.isEmpty():
                continue

            if dist_area is None:
                dist = float(g.distance(pt_geom))
            else:
                try:
                    closest = g.closestPoint(pt_geom)
                    closest_pt = closest.asPoint() if not closest.isEmpty() else None
                    if closest_pt is None:
                        continue
                    dist = float(dist_area.measureLine(query_point, QgsPointXY(closest_pt)))
                except Exception:
                    continue

            if search_radius_m and search_radius_m > 0 and dist > search_radius_m:
                continue

            if best_dist is None or dist < best_dist:
                # Extract depth/elevation
                if depth_field and depth_field in feat.fields().names():
                    z = feat[depth_field]
                else:
                    names = feat.fields().names()
                    z = feat[names[0]] if names else None
                if z is None:
                    continue
                try:
                    zf = float(z)
                except Exception:
                    continue

                best_dist = dist
                best_depth = zf
                best_src = lyr.name()

    return best_depth, best_src, best_dist


def sample_depth(
    point: QgsPointXY,
    depth_source_mode: int,
    raster_samplers: Sequence[RasterSampler],
    contour_samplers: Sequence[ContourSampler],
    contour_search_radius_m: float,
    transform_context,
    project=None,
    band: int = 1,
) -> Optional[float]:
    """Single best depth value at ``point``.

    ``depth_source_mode``: 0 = Auto (raster first, contour fallback),
    1 = Raster only, 2 = Contours only.
    """
    want_raster = depth_source_mode in (0, 1)
    want_contours = depth_source_mode in (0, 2)

    if want_raster and raster_samplers:
        best, _src, _all = sample_rasters(point, raster_samplers, band)
        if best is not None:
            return best

    if want_contours and contour_samplers:
        best, _src, _dist = sample_contours(
            point, contour_samplers, contour_search_radius_m, transform_context, project
        )
        return best

    return None
