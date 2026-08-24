# -*- coding: utf-8 -*-
"""Multi-layer depth sampling shared by the KP Mouse tool.

Depth sources are raster layers (MBES/bathymetry grids) and line vector layers
(depth contours with a numeric depth field). Several sources can be configured
at once because survey data often arrives as multiple grids/contour sets, each
covering part of a route. Rasters are preferred by resolution; contours are
used for point sampling as nearest-feature values and for profiles as
line-crossing points.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject,
    QgsSpatialIndex,
)

from ..kp_range_utils import make_distance_area
from ..qgis_compat import GEOMETRY_LINE, LAYER_RASTER, LAYER_VECTOR


def is_depth_capable_layer(layer) -> bool:
    try:
        if layer.type() == LAYER_RASTER:
            return True
        return (layer.type() == LAYER_VECTOR
                and layer.geometryType() == GEOMETRY_LINE)
    except Exception:
        return False


class DepthSampler:
    """Samples depths from several raster/contour sources in a fixed CRS.

    ``sources`` is a list of ``(layer, field_name)`` tuples; the field is only
    used for vector (contour) layers. All query points/lines are expected in
    ``crs`` (normally the project/canvas CRS).
    """

    def __init__(self, crs, sources: List[Tuple[object, str]]):
        self.crs = crs
        self._rasters: List[Dict] = []
        self._contours: List[Dict] = []
        for layer, field in (sources or []):
            if layer is None:
                continue
            try:
                if layer.type() == LAYER_RASTER:
                    self._prepare_raster(layer)
                elif (layer.type() == LAYER_VECTOR
                      and layer.geometryType() == GEOMETRY_LINE and field):
                    self._prepare_contour(layer, field)
            except Exception:
                continue

    def has_sources(self) -> bool:
        return bool(self._rasters or self._contours)

    def finest_pixel_size_m(self) -> Optional[float]:
        """Cell size (m) of the highest-resolution configured raster."""
        sizes = [math.sqrt(src["pixel_area"]) for src in self._rasters
                 if src.get("pixel_area")]
        return min(sizes) if sizes else None

    def source_count(self) -> int:
        return len(self._rasters) + len(self._contours)

    # -- preparation -------------------------------------------------------
    def _transform_to(self, layer_crs) -> Optional[QgsCoordinateTransform]:
        if layer_crs == self.crs:
            return None
        return QgsCoordinateTransform(self.crs, layer_crs, QgsProject.instance())

    def _prepare_raster(self, layer):
        provider = layer.dataProvider()
        if provider is None:
            return
        nodata = None
        try:
            if provider.sourceHasNoDataValue(1):
                nodata = provider.sourceNoDataValue(1)
        except Exception:
            nodata = None
        pixel_area = None
        try:
            units_x = abs(float(layer.rasterUnitsPerPixelX()))
            units_y = abs(float(layer.rasterUnitsPerPixelY()))
            if units_x > 0 and units_y > 0:
                if layer.crs().isGeographic():
                    area = make_distance_area(
                        layer.crs(), QgsProject.instance().transformContext())
                    center = layer.extent().center()
                    dx = float(area.measureLine(
                        QgsPointXY(center.x(), center.y()),
                        QgsPointXY(center.x() + units_x, center.y())))
                    dy = float(area.measureLine(
                        QgsPointXY(center.x(), center.y()),
                        QgsPointXY(center.x(), center.y() + units_y)))
                    pixel_area = dx * dy if dx > 0 and dy > 0 else None
                else:
                    pixel_area = units_x * units_y
        except Exception:
            pixel_area = None
        self._rasters.append({
            "name": layer.name(), "provider": provider,
            "extent": layer.extent(), "transform": self._transform_to(layer.crs()),
            "nodata": nodata, "pixel_area": pixel_area,
        })
        # Prefer higher resolution rasters (smaller pixels) for first-valid wins.
        self._rasters.sort(key=lambda src: (
            src.get("pixel_area") is None,
            src.get("pixel_area") if src.get("pixel_area") is not None else float("inf")))

    def _prepare_contour(self, layer, field):
        field_index = layer.fields().lookupField(field)
        if field_index < 0:
            return
        index = QgsSpatialIndex(layer.getFeatures())
        self._contours.append({
            "name": layer.name(), "layer": layer, "field_index": field_index,
            "index": index, "transform": self._transform_to(layer.crs()),
            "back_transform": (
                None if layer.crs() == self.crs else QgsCoordinateTransform(
                    layer.crs(), self.crs, QgsProject.instance())),
        })

    # -- point sampling ----------------------------------------------------
    def _sample_raster(self, src, point: QgsPointXY) -> Optional[float]:
        sample_point = QgsPointXY(point)
        transform = src.get("transform")
        if transform is not None:
            try:
                sample_point = transform.transform(sample_point)
            except Exception:
                return None
        extent = src.get("extent")
        try:
            if extent is not None and not extent.contains(sample_point):
                return None
        except Exception:
            pass
        try:
            value, ok = src["provider"].sample(sample_point, 1)
        except Exception:
            return None
        if not ok:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        nodata = src.get("nodata")
        # Tolerant comparison: Float32 nodata values widened to double by the
        # provider are not always bit-identical to sourceNoDataValue().
        if nodata is not None and (
                value == float(nodata)
                or abs(value - float(nodata))
                <= 1e-6 * max(1.0, abs(float(nodata)))):
            return None
        if math.isnan(value):
            return None
        return value

    def _contour_value(self, src, feature_id) -> Optional[float]:
        try:
            feature = src["layer"].getFeature(feature_id)
            value = feature.attribute(src["field_index"])
            return float(value) if value is not None else None
        except (TypeError, ValueError, RuntimeError):
            return None

    def _sample_contour(self, src, point: QgsPointXY) -> Optional[float]:
        sample_point = QgsPointXY(point)
        transform = src.get("transform")
        if transform is not None:
            try:
                sample_point = transform.transform(sample_point)
            except Exception:
                return None
        try:
            nearest = src["index"].nearestNeighbor(sample_point, 1)
        except Exception:
            return None
        if not nearest:
            return None
        return self._contour_value(src, nearest[0])

    def sample_point(self, point: QgsPointXY) -> Optional[float]:
        """First valid raster value (best resolution first), else nearest contour."""
        for src in self._rasters:
            value = self._sample_raster(src, point)
            if value is not None:
                return value
        for src in self._contours:
            value = self._sample_contour(src, point)
            if value is not None:
                return value
        return None

    # -- profile sampling --------------------------------------------------
    def profile(self, start: QgsPointXY, end: QgsPointXY, distance_area,
                samples: int = 120) -> Dict:
        """Depth series along the straight line ``start``→``end``.

        Returns ``{"length_m", "pixel_size_m", "rasters": [{"name", "x", "y"}],
        "contours": [{"name", "x", "y"}]}`` with ``x`` in metres from ``start``
        and ``None`` gaps in raster series where a raster has no coverage.

        ``samples`` is a ceiling: on a coarse grid the station count drops so
        stations stay at least half a raster cell apart — denser sampling of a
        nearest-cell provider only re-reads the same pixels and turns each
        cell edge into a fake near-vertical step.
        """
        try:
            length_m = float(distance_area.measureLine(start, end))
        except Exception:
            length_m = math.hypot(end.x() - start.x(), end.y() - start.y())
        pixel_size_m = self.finest_pixel_size_m()
        result = {"length_m": length_m, "pixel_size_m": pixel_size_m,
                  "rasters": [], "contours": []}
        if length_m <= 0 or not self.has_sources():
            return result
        samples = max(2, int(samples))
        if pixel_size_m and pixel_size_m > 0:
            per_half_cell = int(math.ceil(length_m / (pixel_size_m / 2.0)))
            samples = max(2, min(samples, per_half_cell))
        stations = []
        for step in range(samples + 1):
            fraction = step / float(samples)
            stations.append((fraction * length_m, QgsPointXY(
                start.x() + fraction * (end.x() - start.x()),
                start.y() + fraction * (end.y() - start.y()))))
        for src in self._rasters:
            values = [self._sample_raster(src, point) for _dist, point in stations]
            if any(value is not None for value in values):
                result["rasters"].append({
                    "name": src["name"],
                    "x": [dist for dist, _point in stations], "y": values})
        if self._contours:
            line = QgsGeometry.fromPolylineXY([QgsPointXY(start), QgsPointXY(end)])
            for src in self._contours:
                crossings = self._contour_crossings(src, line, start, distance_area)
                if crossings:
                    crossings.sort(key=lambda pair: pair[0])
                    result["contours"].append({
                        "name": src["name"],
                        "x": [dist for dist, _val in crossings],
                        "y": [val for _dist, val in crossings]})
        return result

    def _contour_crossings(self, src, line_geometry, start: QgsPointXY,
                           distance_area) -> List[Tuple[float, float]]:
        query_line = QgsGeometry(line_geometry)
        transform = src.get("transform")
        if transform is not None:
            try:
                query_line.transform(transform)
            except Exception:
                return []
        try:
            candidate_ids = src["index"].intersects(query_line.boundingBox())
        except Exception:
            return []
        crossings: List[Tuple[float, float]] = []
        back = src.get("back_transform")
        for feature_id in candidate_ids[:500]:
            value = self._contour_value(src, feature_id)
            if value is None:
                continue
            try:
                geometry = src["layer"].getFeature(feature_id).geometry()
                if geometry is None or geometry.isEmpty():
                    continue
                intersection = query_line.intersection(geometry)
            except Exception:
                continue
            if intersection is None or intersection.isEmpty():
                continue
            for point in _geometry_points(intersection):
                if back is not None:
                    try:
                        point = back.transform(point)
                    except Exception:
                        continue
                try:
                    distance = float(distance_area.measureLine(start, point))
                except Exception:
                    continue
                crossings.append((distance, value))
        return crossings


def _geometry_points(geometry) -> List[QgsPointXY]:
    """Points of a point/multipoint intersection result (lines contribute vertices)."""
    try:
        if geometry.isMultipart():
            multi = geometry.asMultiPoint()
            if multi:
                return [QgsPointXY(point) for point in multi]
        else:
            point = geometry.asPoint()
            return [QgsPointXY(point)]
    except Exception:
        pass
    points: List[QgsPointXY] = []
    try:
        for vertex in geometry.vertices():
            points.append(QgsPointXY(vertex.x(), vertex.y()))
    except Exception:
        pass
    return points
