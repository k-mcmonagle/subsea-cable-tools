# -*- coding: utf-8 -*-
"""Shared depth/elevation sampling helpers.

Extracted from AddDepthToPointLayerAlgorithm and
DynamicBufferLayCorridorAlgorithm so the same sampling behaviour is available
to processing algorithms and to interactive tools (e.g. the Cable Route
Workbench DepthService) without a QgsProcessingContext.

Samplers:
- raster sampler:  ``(QgsRasterLayer, Optional[QgsCoordinateTransform])``
- contour sampler: :class:`ContourSampler` — the layer, its depth field and
  transform, plus a spatial index (with stored geometries) of the features
  that carry a usable depth, built once by :func:`build_contour_samplers`.

All query points are in the source CRS the samplers were built with; the
stored transforms convert into each layer's CRS. Pass the processing
context's ``transformContext()`` when building samplers inside an algorithm:
the fallback (the project's transform context) must not be read from a
worker thread.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateTransform,
    QgsCsException,
    QgsDistanceArea,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
)

from ..kp_range_utils import make_distance_area
from ..plugin_log import log_exception

RasterSampler = Tuple[QgsRasterLayer, Optional[QgsCoordinateTransform]]

# Metres per degree used to turn a search distance into a lat/lon box. Both
# are deliberately on the small side (the meridian degree is 110.57 km at the
# equator, the parallel degree <= 111.32 km x cos(lat)) so the box always
# contains every point within the distance; exact distances filter after.
_M_PER_DEG_LAT_MIN = 110000.0
_M_PER_DEG_LON_EQUATOR = 111000.0

# Nearest-neighbour candidates fetched per query before the exact distance
# decides (ties and near-ties between contours are resolved exactly).
_NEAREST_CANDIDATES = 4


class ContourSampler(NamedTuple):
    """A contour layer prepared for nearest-contour depth queries."""

    layer: QgsVectorLayer
    depth_field: str
    transform: Optional[QgsCoordinateTransform]
    index: Optional[QgsSpatialIndex] = None
    depths: Optional[Dict[int, float]] = None
    distance: Optional[QgsDistanceArea] = None  # geodesic, geographic layers only


def _transform_context_or_project(transform_context):
    if transform_context is not None:
        return transform_context
    return QgsProject.instance().transformContext()


def build_raster_samplers(
    rasters: Sequence[QgsRasterLayer],
    source_crs,
    transform_context=None,
) -> List[RasterSampler]:
    """Pair each raster with a transform from ``source_crs`` into its CRS."""
    samplers: List[RasterSampler] = []
    for r in rasters:
        if not r:
            continue
        transform = None
        if r.crs() != source_crs:
            transform = QgsCoordinateTransform(
                source_crs, r.crs(), _transform_context_or_project(transform_context))
        samplers.append((r, transform))
    return samplers


def _stored_geometry_index() -> QgsSpatialIndex:
    flag = getattr(QgsSpatialIndex, "FlagStoreFeatureGeometries", None)
    if flag is None:
        flag = QgsSpatialIndex.Flag.FlagStoreFeatureGeometries
    return QgsSpatialIndex(flag)


def contour_feature_depth(feat: QgsFeature, depth_field: str) -> Optional[float]:
    """The feature's depth (``depth_field``, else its first attribute)."""
    if depth_field and depth_field in feat.fields().names():
        z = feat[depth_field]
    else:
        names = feat.fields().names()
        z = feat[names[0]] if names else None
    if z is None:
        return None
    try:
        return float(z)
    except (TypeError, ValueError):  # NULL QVariant / text
        return None


def prepare_contour_sampler(
    layer: QgsVectorLayer,
    depth_field: str,
    transform: Optional[QgsCoordinateTransform],
    transform_context=None,
    project=None,
) -> ContourSampler:
    """Index ``layer`` once for :func:`sample_contours`.

    Only features with a geometry and a numeric depth are indexed: a feature
    without a usable depth could never be the answer (the nearest *valid*
    contour always wins), so leaving it out lets a single nearest-neighbour
    query find the right feature.
    """
    index = _stored_geometry_index()
    depths: Dict[int, float] = {}
    for feat in layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        depth = contour_feature_depth(feat, depth_field)
        if depth is None:
            continue
        index.addFeature(feat)
        depths[int(feat.id())] = depth
    distance = None
    if layer.crs().isGeographic():
        distance = make_distance_area(
            layer.crs(), _transform_context_or_project(transform_context), project=project)
    return ContourSampler(layer, depth_field, transform, index, depths, distance)


def build_contour_samplers(
    contour_layers: Sequence[Optional[QgsVectorLayer]],
    depth_fields: Sequence[str],
    source_crs,
    transform_context=None,
    project=None,
) -> List[ContourSampler]:
    """Prepare each contour layer (depth field, CRS transform, spatial index).

    A blank depth field means "fall back to the first attribute".
    """
    out: List[ContourSampler] = []
    context = _transform_context_or_project(transform_context)
    for i, lyr in enumerate(contour_layers):
        if not lyr:
            continue
        depth_field = depth_fields[i] if i < len(depth_fields) else ''
        transform = None
        if lyr.crs() != source_crs:
            transform = QgsCoordinateTransform(source_crs, lyr.crs(), context)
        out.append(prepare_contour_sampler(lyr, depth_field, transform, context, project))
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
            except QgsCsException:
                # Outside the raster CRS's valid area: no data there.
                all_vals.append((raster.name(), None))
                continue

        try:
            val, ok = raster.dataProvider().sample(sample_pt, band)
        except Exception:
            log_exception(f"Depth sampling: raster '{raster.name()}' sample failed",
                          level=logging.DEBUG)
            ok = False
            val = None

        if ok and val is not None:
            try:
                fval = float(val)
            except (TypeError, ValueError):
                fval = None
        else:
            fval = None

        all_vals.append((raster.name(), fval))
        if best_val is None and fval is not None:
            best_val = fval
            best_src = raster.name()

    return best_val, best_src, all_vals


def _search_rect(point: QgsPointXY, distance: float, geographic: bool) -> QgsRectangle:
    """Box around ``point`` containing everything within ``distance``.

    ``distance`` is metres for geographic layers (converted to a
    conservative degree box) and layer units otherwise.
    """
    if not geographic:
        return QgsRectangle(point.x() - distance, point.y() - distance,
                            point.x() + distance, point.y() + distance)
    deg_lat = distance / _M_PER_DEG_LAT_MIN
    lat_max = min(89.9, abs(point.y()) + deg_lat)
    cos_lat = max(0.01, math.cos(math.radians(lat_max)))
    deg_lon = min(180.0, distance / (_M_PER_DEG_LON_EQUATOR * cos_lat))
    return QgsRectangle(point.x() - deg_lon, point.y() - deg_lat,
                        point.x() + deg_lon, point.y() + deg_lat)


def _contour_distance(sampler: ContourSampler, fid: int, query_point: QgsPointXY,
                      pt_geom: QgsGeometry) -> Optional[float]:
    """Distance from the query point to contour ``fid``.

    Layer units on projected layers; on geographic layers, geodesic metres to
    the (planar) closest point of the contour. (This used to call the
    non-existent ``QgsGeometry.closestPoint`` inside a blanket ``except``, so
    geographic contour layers silently never returned a depth.)
    """
    geom = sampler.index.geometry(fid)
    if geom is None or geom.isEmpty():
        return None
    if sampler.distance is None:
        return float(geom.distance(pt_geom))
    closest = geom.nearestPoint(pt_geom)
    if closest is None or closest.isEmpty():
        return None
    return float(sampler.distance.measureLine(query_point, QgsPointXY(closest.asPoint())))


def sample_contours(
    point: QgsPointXY,
    contour_samplers: Sequence[ContourSampler],
    search_radius_m: float,
    transform_context=None,
    project=None,
) -> Tuple[Optional[float], Optional[str], Optional[float]]:
    """Depth from the nearest contour feature across all contour samplers.

    Returns ``(best_depth, best_source_layer_name, best_distance_m)``.
    ``search_radius_m`` of 0 means unlimited. Each query is a spatial-index
    lookup on the samplers built by :func:`build_contour_samplers`; plain
    ``(layer, depth_field, transform)`` tuples are still accepted and are
    indexed on the fly (slow — build the samplers once instead).
    ``transform_context`` / ``project`` are only used for such tuples.
    """
    best_depth = None
    best_dist = None
    best_src = None
    radius = float(search_radius_m or 0.0)

    for sampler in contour_samplers:
        if not isinstance(sampler, ContourSampler) or sampler.index is None:
            if not sampler or not sampler[0]:
                continue
            sampler = prepare_contour_sampler(
                sampler[0], sampler[1], sampler[2], transform_context, project)
        if not sampler.layer or not sampler.depths:
            continue

        query_point = QgsPointXY(point)
        if sampler.transform is not None:
            try:
                query_point = sampler.transform.transform(point)
            except QgsCsException:
                continue  # point outside the contour CRS's valid area

        pt_geom = QgsGeometry.fromPointXY(query_point)
        geographic = sampler.distance is not None

        if radius > 0:
            candidates = sampler.index.intersects(_search_rect(query_point, radius, geographic))
        else:
            candidates = sampler.index.nearestNeighbor(query_point, _NEAREST_CANDIDATES)
            if geographic and candidates:
                # The index ranks by planar degrees; widen to every contour
                # that could be geodesically nearer than the best of those.
                dists = [d for d in (_contour_distance(sampler, fid, query_point, pt_geom)
                                     for fid in candidates) if d is not None]
                if dists:
                    candidates = sampler.index.intersects(
                        _search_rect(query_point, min(dists), True))

        layer_best = None  # (distance, fid)
        for fid in candidates:
            dist = _contour_distance(sampler, fid, query_point, pt_geom)
            if dist is None:
                continue
            if radius > 0 and dist > radius:
                continue
            if layer_best is None or (dist, fid) < layer_best:
                layer_best = (dist, fid)

        if layer_best is not None and (best_dist is None or layer_best[0] < best_dist):
            best_dist = layer_best[0]
            best_depth = sampler.depths[layer_best[1]]
            best_src = sampler.layer.name()

    return best_depth, best_src, best_dist


def sample_depth(
    point: QgsPointXY,
    depth_source_mode: int,
    raster_samplers: Sequence[RasterSampler],
    contour_samplers: Sequence[ContourSampler],
    contour_search_radius_m: float,
    transform_context=None,
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
