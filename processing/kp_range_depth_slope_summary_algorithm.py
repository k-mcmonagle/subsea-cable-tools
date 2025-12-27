# kp_range_depth_slope_summary_algorithm.py
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import math

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterField,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsRasterDataProvider,
    QgsRasterLayer,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)


@dataclass(frozen=True)
class _RasterSource:
    provider: QgsRasterDataProvider
    extent: QgsRectangle
    transform: Optional[QgsCoordinateTransform]
    nodata: Optional[float]
    pixel_area_m2: Optional[float]


class KPRangeDepthSlopeSummaryAlgorithm(QgsProcessingAlgorithm):
    """Summarise depth + slope statistics per KP-range feature.

    For each input line feature (a KP range segment), the algorithm samples depth along
    the feature at a fixed interval and computes:
      - Depth min/max/avg
      - Along-track slope (deg) min/max/avg
      - Cross-track side-slope (deg) min/max/avg (+ve = down to starboard)

    Depth sources:
      - Raster(s): samples band 1, preferring smallest grid size among overlapping rasters.
      - Contour(s): uses intersections with the KP-range line; if interpolation is enabled,
        depth at stations is linearly interpolated between contour hits.

    Sign conventions (matches Depth Profile defaults):
      - Along-track slope: +ve for up-slope, -ve for down-slope.
      - Side-slope: +ve for down to starboard, -ve for down to port.
    """

    INPUT = 'INPUT'
    DEPTH_SOURCE = 'DEPTH_SOURCE'

    RASTER_LAYERS = 'RASTER_LAYERS'

    CONTOUR_LAYER_1 = 'CONTOUR_LAYER_1'
    CONTOUR_DEPTH_FIELD_1 = 'CONTOUR_DEPTH_FIELD_1'
    CONTOUR_LAYER_2 = 'CONTOUR_LAYER_2'
    CONTOUR_DEPTH_FIELD_2 = 'CONTOUR_DEPTH_FIELD_2'
    INTERPOLATE_CONTOURS = 'INTERPOLATE_CONTOURS'

    SAMPLE_INTERVAL_M = 'SAMPLE_INTERVAL_M'

    ADAPTIVE_INTERVAL = 'ADAPTIVE_INTERVAL'
    ADAPTIVE_INTERVAL_FACTOR = 'ADAPTIVE_INTERVAL_FACTOR'

    SIDE_SLOPE_SEARCH_M = 'SIDE_SLOPE_SEARCH_M'

    INCLUDE_DIRECTIONAL_EXTREMES = 'INCLUDE_DIRECTIONAL_EXTREMES'

    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Input KP range line layer'),
                [QgsProcessing.TypeVectorLine],
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.DEPTH_SOURCE,
                self.tr('Depth source'),
                options=[
                    self.tr('Auto (rasters if provided, else contours)'),
                    self.tr('Raster'),
                    self.tr('Contours'),
                ],
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.RASTER_LAYERS,
                self.tr('Bathymetry raster layer(s)'),
                layerType=QgsProcessing.TypeRaster,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.CONTOUR_LAYER_1,
                self.tr('Contour layer 1'),
                [QgsProcessing.TypeVectorLine],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.CONTOUR_DEPTH_FIELD_1,
                self.tr('Depth field 1'),
                parentLayerParameterName=self.CONTOUR_LAYER_1,
                type=QgsProcessingParameterField.Numeric,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.CONTOUR_LAYER_2,
                self.tr('Contour layer 2 (optional)'),
                [QgsProcessing.TypeVectorLine],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.CONTOUR_DEPTH_FIELD_2,
                self.tr('Depth field 2'),
                parentLayerParameterName=self.CONTOUR_LAYER_2,
                type=QgsProcessingParameterField.Numeric,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.INTERPOLATE_CONTOURS,
                self.tr('Interpolate between contour hits'),
                defaultValue=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.SAMPLE_INTERVAL_M,
                self.tr('Sampling interval along KP range (m)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.1,
                defaultValue=50.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ADAPTIVE_INTERVAL,
                self.tr('Adaptive sampling (Raster only)'),
                defaultValue=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.ADAPTIVE_INTERVAL_FACTOR,
                self.tr('Adaptive factor (Raster only)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.25,
                maxValue=10.0,
                defaultValue=1.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.SIDE_SLOPE_SEARCH_M,
                self.tr('Side slope cross-search (m) (half-width)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=1.0,
                defaultValue=200.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.INCLUDE_DIRECTIONAL_EXTREMES,
                self.tr('Include directional extremes (up/down, port/stbd)'),
                defaultValue=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('KP range depth/slope summary'),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))

        depth_source_mode = int(self.parameterAsEnum(parameters, self.DEPTH_SOURCE, context))

        raster_layers = [
            lyr
            for lyr in (self.parameterAsLayerList(parameters, self.RASTER_LAYERS, context) or [])
            if isinstance(lyr, QgsRasterLayer)
        ]

        contour1 = self.parameterAsVectorLayer(parameters, self.CONTOUR_LAYER_1, context)
        depth_field1 = self.parameterAsString(parameters, self.CONTOUR_DEPTH_FIELD_1, context) or ''
        contour2 = self.parameterAsVectorLayer(parameters, self.CONTOUR_LAYER_2, context)
        depth_field2 = self.parameterAsString(parameters, self.CONTOUR_DEPTH_FIELD_2, context) or ''
        interpolate_contours = bool(self.parameterAsBool(parameters, self.INTERPOLATE_CONTOURS, context))

        sample_interval_m = float(self.parameterAsDouble(parameters, self.SAMPLE_INTERVAL_M, context))
        adaptive_interval = bool(self.parameterAsBool(parameters, self.ADAPTIVE_INTERVAL, context))
        adaptive_factor = float(self.parameterAsDouble(parameters, self.ADAPTIVE_INTERVAL_FACTOR, context))
        side_search_m = float(self.parameterAsDouble(parameters, self.SIDE_SLOPE_SEARCH_M, context))
        include_dir = bool(self.parameterAsBool(parameters, self.INCLUDE_DIRECTIONAL_EXTREMES, context))

        if sample_interval_m <= 0:
            raise QgsProcessingException(self.tr('Sampling interval must be > 0.'))
        if adaptive_factor <= 0:
            raise QgsProcessingException(self.tr('Adaptive factor must be > 0.'))
        if side_search_m <= 0:
            raise QgsProcessingException(self.tr('Side slope cross-search must be > 0.'))

        # Decide depth source if Auto
        if depth_source_mode == 0:
            if raster_layers:
                depth_source_mode = 1
            else:
                depth_source_mode = 2

        line_crs = source.sourceCrs()

        raster_sources: List[_RasterSource] = []
        if depth_source_mode == 1:
            if not raster_layers:
                raise QgsProcessingException(self.tr('Raster mode selected but no rasters provided.'))
            raster_sources = self._prepare_raster_sources(line_crs, raster_layers, context)
            if not raster_sources:
                raise QgsProcessingException(self.tr('No valid raster sources.'))
        else:
            adaptive_interval = False

        contour_layers: List[QgsVectorLayer] = []
        contour_fields: List[str] = []
        if contour1 and isinstance(contour1, QgsVectorLayer) and depth_field1:
            contour_layers.append(contour1)
            contour_fields.append(depth_field1)
        if contour2 and isinstance(contour2, QgsVectorLayer) and depth_field2:
            contour_layers.append(contour2)
            contour_fields.append(depth_field2)

        if depth_source_mode == 2 and not contour_layers:
            raise QgsProcessingException(self.tr('Contour mode selected but no contour layer/field provided.'))

        # Side-slope contour index is global for the run (all features share same CRS).
        contour_index: Optional[QgsSpatialIndex] = None
        contour_data: Optional[Dict[int, Tuple[QgsGeometry, float]]] = None
        if depth_source_mode == 2:
            contour_index, contour_data = self._build_combined_contour_index(line_crs, contour_layers, contour_fields)

        # Output fields: copy inputs, then append stats (ensure uniqueness)
        out_fields = QgsFields(source.fields())
        out_fields.append(QgsField(self._unique_field_name(out_fields, 'depth_min'), QVariant.Double))
        out_fields.append(QgsField(self._unique_field_name(out_fields, 'depth_max'), QVariant.Double))
        out_fields.append(QgsField(self._unique_field_name(out_fields, 'depth_avg'), QVariant.Double))
        out_fields.append(QgsField(self._unique_field_name(out_fields, 'slope_min_deg'), QVariant.Double))
        out_fields.append(QgsField(self._unique_field_name(out_fields, 'slope_max_deg'), QVariant.Double))
        out_fields.append(QgsField(self._unique_field_name(out_fields, 'slope_avg_deg'), QVariant.Double))
        out_fields.append(QgsField(self._unique_field_name(out_fields, 'side_min_deg'), QVariant.Double))
        out_fields.append(QgsField(self._unique_field_name(out_fields, 'side_max_deg'), QVariant.Double))
        out_fields.append(QgsField(self._unique_field_name(out_fields, 'side_avg_deg'), QVariant.Double))

        # Optional directional extremes
        # Along-route slope: +ve = up-slope; -ve = down-slope
        # Side-slope: +ve = down to stbd; -ve = down to port
        if include_dir:
            out_fields.append(QgsField(self._unique_field_name(out_fields, 'slope_up_max_deg'), QVariant.Double))
            out_fields.append(QgsField(self._unique_field_name(out_fields, 'slope_down_min_deg'), QVariant.Double))
            out_fields.append(QgsField(self._unique_field_name(out_fields, 'side_stbd_max_deg'), QVariant.Double))
            out_fields.append(QgsField(self._unique_field_name(out_fields, 'side_port_min_deg'), QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            out_fields,
            source.wkbType(),
            line_crs,
        )
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(line_crs, context.transformContext())
        distance_area.setEllipsoid(context.project().ellipsoid())

        features = list(source.getFeatures())
        total = len(features)
        for idx, feat in enumerate(features):
            if feedback.isCanceled():
                break

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                out_feat = QgsFeature(out_fields)
                out_feat.setGeometry(geom)
                out_feat.setAttributes(list(feat.attributes()) + [None] * 9)
                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                continue

            # Extract parts as point sequences
            parts = self._as_line_parts(geom)
            if not parts:
                out_feat = QgsFeature(out_fields)
                out_feat.setGeometry(geom)
                out_feat.setAttributes(list(feat.attributes()) + [None] * 9)
                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                continue

            # Precompute contour depth profile for this feature (if contour mode)
            contour_profile: Optional[List[Tuple[float, float]]] = None
            if depth_source_mode == 2:
                contour_profile = self._build_contour_profile_for_feature(
                    parts,
                    distance_area,
                    line_crs,
                    contour_layers,
                    contour_fields,
                    interpolate_contours,
                    feedback,
                )

            # Sample along the line parts
            station_dist_m: List[float] = []
            station_xy: List[QgsPointXY] = []
            station_depth: List[Optional[float]] = []
            station_side_slope_deg: List[Optional[float]] = []

            # Choose tangent delta based on station spacing for stability
            tangent_delta_m = max(5.0, min(50.0, sample_interval_m / 2.0))

            # Use a feature-global stationing (meters) so contour profiles and slope stats are consistent
            global_offset_m = 0.0
            for part_points in parts:
                part_len = self._measure_polyline_m(part_points, distance_area)
                if part_len <= 0:
                    continue

                dist_local = 0.0
                while dist_local <= part_len + 1e-6:
                    pt = self._interpolate_point_along_points(part_points, dist_local, distance_area)
                    if pt is None:
                        break
                    dist_global = global_offset_m + dist_local
                    station_dist_m.append(dist_global)
                    station_xy.append(pt)

                    z: Optional[float] = None
                    raster_used: Optional[_RasterSource] = None
                    if depth_source_mode == 1:
                        z, raster_used = self._sample_rasters_at_point_with_source(pt, raster_sources)
                    else:
                        if contour_profile:
                            z = self._sample_contour_profile_at_distance(contour_profile, dist_global)

                    station_depth.append(z)

                    # Side slope at this station
                    side_deg = None
                    try:
                        side_deg = self._compute_side_slope_at_station(
                            part_points,
                            part_len,
                            dist_local,
                            tangent_delta_m,
                            station_xy[-1],
                            distance_area,
                            line_crs,
                            depth_source_mode,
                            raster_sources,
                            contour_index,
                            contour_data,
                            side_search_m,
                        )
                    except Exception:
                        side_deg = None
                    station_side_slope_deg.append(side_deg)

                    # Step forward
                    step = float(sample_interval_m)
                    if adaptive_interval:
                        px = self._pixel_size_m_from_raster_source(raster_used)
                        if px is not None:
                            step = max(step, float(adaptive_factor) * float(px))
                    dist_local += step

                # Break between parts so we don't compute along-track slope across disjoint segments
                station_dist_m.append(float('nan'))
                station_xy.append(QgsPointXY())
                station_depth.append(None)
                station_side_slope_deg.append(None)

                global_offset_m += part_len

            depth_min, depth_max, depth_avg = self._min_max_avg(station_depth)

            # Along-track slopes per segment (degrees)
            slope_vals_deg: List[Optional[float]] = []
            prev_dist = None
            prev_depth = None
            for d, z in zip(station_dist_m, station_depth):
                if not math.isfinite(d) or z is None:
                    prev_dist = None
                    prev_depth = None
                    continue
                if prev_dist is None or prev_depth is None:
                    prev_dist = d
                    prev_depth = z
                    continue
                horiz_m = float(d) - float(prev_dist)
                if horiz_m <= 0:
                    prev_dist = d
                    prev_depth = z
                    continue
                vertical = float(z) - float(prev_depth)
                vertical_for_slope = -vertical  # +ve = up-slope (shallower)
                slope_rad = math.atan2(vertical_for_slope, horiz_m)
                slope_vals_deg.append(math.degrees(slope_rad))
                prev_dist = d
                prev_depth = z

            slope_min, slope_max, slope_avg = self._min_max_avg(slope_vals_deg)

            side_min, side_max, side_avg = self._min_max_avg(station_side_slope_deg)

            slope_up_max = None
            slope_down_min = None
            side_stbd_max = None
            side_port_min = None
            if include_dir:
                slope_up_max, slope_down_min = self._pos_max_neg_min(slope_vals_deg)
                side_stbd_max, side_port_min = self._pos_max_neg_min(station_side_slope_deg)

            out_feat = QgsFeature(out_fields)
            out_feat.setGeometry(geom)
            attrs = list(feat.attributes()) + [
                depth_min,
                depth_max,
                depth_avg,
                slope_min,
                slope_max,
                slope_avg,
                side_min,
                side_max,
                side_avg,
            ]
            if include_dir:
                attrs.extend([slope_up_max, slope_down_min, side_stbd_max, side_port_min])
            out_feat.setAttributes(attrs)
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)

            if total > 0:
                feedback.setProgress(int((idx + 1) * 100 / total))

        return {self.OUTPUT: dest_id}

    # --------------------------- Raster helpers ---------------------------

    @staticmethod
    def _prepare_raster_sources(line_crs, raster_layers: Sequence[QgsRasterLayer], context) -> List[_RasterSource]:
        sources: List[_RasterSource] = []
        for raster_layer in raster_layers:
            if not raster_layer or not raster_layer.isValid():
                continue
            provider = raster_layer.dataProvider()
            if provider is None or not provider.isValid():
                continue

            raster_crs = raster_layer.crs()
            transform = None
            if raster_crs != line_crs:
                try:
                    transform = QgsCoordinateTransform(line_crs, raster_crs, QgsProject.instance())
                except Exception:
                    transform = None

            nodata = None
            try:
                if provider.sourceHasNoDataValue(1):
                    nodata = float(provider.sourceNoDataValue(1))
            except Exception:
                nodata = None

            pixel_area_m2 = None
            try:
                rupx = abs(float(raster_layer.rasterUnitsPerPixelX()))
                rupy = abs(float(raster_layer.rasterUnitsPerPixelY()))
                if rupx > 0 and rupy > 0:
                    if raster_crs.isGeographic():
                        da = QgsDistanceArea()
                        da.setSourceCrs(raster_crs, context.transformContext())
                        da.setEllipsoid(context.project().ellipsoid())
                        c = raster_layer.extent().center()
                        p0 = QgsPointXY(c.x(), c.y())
                        px = QgsPointXY(c.x() + rupx, c.y())
                        py = QgsPointXY(c.x(), c.y() + rupy)
                        dx_m = float(da.measureLine(p0, px))
                        dy_m = float(da.measureLine(p0, py))
                        if dx_m > 0 and dy_m > 0:
                            pixel_area_m2 = dx_m * dy_m
                    else:
                        pixel_area_m2 = rupx * rupy
            except Exception:
                pixel_area_m2 = None

            sources.append(
                _RasterSource(
                    provider=provider,
                    extent=raster_layer.extent(),
                    transform=transform,
                    nodata=nodata,
                    pixel_area_m2=pixel_area_m2,
                )
            )

        # Prefer smallest grid size (smaller pixel area) first
        try:
            sources.sort(key=lambda s: (s.pixel_area_m2 is None, s.pixel_area_m2 if s.pixel_area_m2 is not None else float('inf')))
        except Exception:
            pass
        return sources

    @staticmethod
    def _sample_rasters_at_point(point_xy_line_crs: QgsPointXY, raster_sources: Sequence[_RasterSource]) -> Optional[float]:
        if not raster_sources:
            return None

        for src in raster_sources:
            sample_pt = QgsPointXY(point_xy_line_crs.x(), point_xy_line_crs.y())
            if src.transform is not None:
                try:
                    sample_pt = src.transform.transform(sample_pt)
                except Exception:
                    continue

            try:
                extent = src.extent
                if extent and (
                    sample_pt.x() < extent.xMinimum()
                    or sample_pt.x() > extent.xMaximum()
                    or sample_pt.y() < extent.yMinimum()
                    or sample_pt.y() > extent.yMaximum()
                ):
                    continue
            except Exception:
                pass

            try:
                sample, ok = src.provider.sample(sample_pt, 1)
            except Exception:
                continue
            if not ok:
                continue

            try:
                val = float(sample)
            except Exception:
                continue

            try:
                if src.nodata is not None and float(src.nodata) == val:
                    continue
            except Exception:
                pass

            if math.isnan(val):
                continue
            return val

        return None

    @staticmethod
    def _sample_rasters_at_point_with_source(
        point_xy_line_crs: QgsPointXY,
        raster_sources: Sequence[_RasterSource],
    ) -> Tuple[Optional[float], Optional[_RasterSource]]:
        if not raster_sources:
            return None, None

        for src in raster_sources:
            sample_pt = QgsPointXY(point_xy_line_crs.x(), point_xy_line_crs.y())
            if src.transform is not None:
                try:
                    sample_pt = src.transform.transform(sample_pt)
                except Exception:
                    continue

            try:
                extent = src.extent
                if extent and (
                    sample_pt.x() < extent.xMinimum()
                    or sample_pt.x() > extent.xMaximum()
                    or sample_pt.y() < extent.yMinimum()
                    or sample_pt.y() > extent.yMaximum()
                ):
                    continue
            except Exception:
                pass

            try:
                sample, ok = src.provider.sample(sample_pt, 1)
            except Exception:
                continue
            if not ok:
                continue

            try:
                val = float(sample)
            except Exception:
                continue

            try:
                if src.nodata is not None and float(src.nodata) == val:
                    continue
            except Exception:
                pass

            if math.isnan(val):
                continue
            return val, src

        return None, None

    @staticmethod
    def _pixel_size_m_from_raster_source(src: Optional[_RasterSource]) -> Optional[float]:
        if src is None:
            return None
        try:
            if src.pixel_area_m2 is None:
                return None
            a = float(src.pixel_area_m2)
            if a <= 0:
                return None
            return math.sqrt(a)
        except Exception:
            return None

    # --------------------------- Contour helpers --------------------------

    @staticmethod
    def _build_combined_contour_index(
        line_crs,
        contour_layers: Sequence[QgsVectorLayer],
        depth_fields: Sequence[str],
    ) -> Tuple[Optional[QgsSpatialIndex], Optional[Dict[int, Tuple[QgsGeometry, float]]]]:
        if not contour_layers:
            return None, None

        index = QgsSpatialIndex()
        data: Dict[int, Tuple[QgsGeometry, float]] = {}
        next_id = 1

        for layer_idx, layer in enumerate(contour_layers):
            if not layer:
                continue
            depth_field = depth_fields[layer_idx] if layer_idx < len(depth_fields) else ''
            if not depth_field:
                continue

            transform = None
            if layer.crs() != line_crs:
                try:
                    transform = QgsCoordinateTransform(layer.crs(), line_crs, QgsProject.instance())
                except Exception:
                    transform = None

            for feat in layer.getFeatures(QgsFeatureRequest()):
                try:
                    geom = feat.geometry()
                    if geom is None or geom.isEmpty():
                        continue
                    if transform is not None:
                        geom = QgsGeometry(geom)
                        geom.transform(transform)
                    z = feat[depth_field]
                    if z is None:
                        continue
                    zf = float(z)
                except Exception:
                    continue

                try:
                    f = QgsFeature()
                    f.setId(next_id)
                    f.setGeometry(geom)
                    index.addFeature(f)
                    data[next_id] = (geom, zf)
                    next_id += 1
                except Exception:
                    continue

        if not data:
            return None, None
        return index, data

    def _build_contour_profile_for_feature(
        self,
        parts: Sequence[Sequence[QgsPointXY]],
        distance_area: QgsDistanceArea,
        line_crs,
        contour_layers: Sequence[QgsVectorLayer],
        depth_fields: Sequence[str],
        interpolate: bool,
        feedback,
    ) -> Optional[List[Tuple[float, float]]]:
        # Collect intersections along the feature geometry
        try:
            if len(parts) == 1:
                route_geom = QgsGeometry.fromPolylineXY(list(parts[0]))
            else:
                route_geom = QgsGeometry.collectGeometry([QgsGeometry.fromPolylineXY(list(p)) for p in parts])
        except Exception:
            return None

        hits: List[Tuple[float, float]] = []
        request = QgsFeatureRequest()
        for layer_idx, contour_layer in enumerate(contour_layers):
            if feedback.isCanceled():
                return None
            depth_field = depth_fields[layer_idx] if layer_idx < len(depth_fields) else ''
            if not depth_field:
                continue

            transform = None
            if contour_layer.crs() != line_crs:
                try:
                    transform = QgsCoordinateTransform(contour_layer.crs(), line_crs, QgsProject.instance())
                except Exception:
                    transform = None

            for feat in contour_layer.getFeatures(request):
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                if transform is not None:
                    try:
                        geom = QgsGeometry(geom)
                        geom.transform(transform)
                    except Exception:
                        continue
                try:
                    zf = float(feat[depth_field])
                except Exception:
                    continue

                try:
                    inter = route_geom.intersection(geom)
                except Exception:
                    continue
                if inter is None or inter.isEmpty():
                    continue

                pts: List[QgsPointXY] = []
                try:
                    if inter.type() == QgsWkbTypes.PointGeometry:
                        pts = [QgsPointXY(p) for p in (inter.asMultiPoint() if inter.isMultipart() else [inter.asPoint()])]
                    elif inter.type() == QgsWkbTypes.LineGeometry:
                        if inter.isMultipart():
                            for pl in inter.asMultiPolyline():
                                pts.extend(QgsPointXY(p) for p in pl)
                        else:
                            pts.extend(QgsPointXY(p) for p in inter.asPolyline())
                except Exception:
                    pts = []

                for p in pts:
                    d_m = self._measure_along_parts_m(parts, p, distance_area)
                    if d_m is None:
                        continue
                    hits.append((float(d_m), float(zf)))

        if not hits:
            return None

        hits.sort(key=lambda t: t[0])

        # Reduce duplicates at same distance by keeping the last (stable)
        dedup: List[Tuple[float, float]] = []
        last_d = None
        for d, z in hits:
            if last_d is not None and abs(d - last_d) < 1e-6:
                dedup[-1] = (d, z)
            else:
                dedup.append((d, z))
                last_d = d

        # If not interpolating, just return the hit list
        if not interpolate:
            return dedup

        # Otherwise: return profile points (distance, depth) usable for linear interpolation
        return dedup

    @staticmethod
    def _sample_contour_profile_at_distance(profile: Sequence[Tuple[float, float]], distance_m: float) -> Optional[float]:
        if not profile:
            return None

        # Clamp to end values (matches typical profile expectations)
        if distance_m <= profile[0][0]:
            return float(profile[0][1])
        if distance_m >= profile[-1][0]:
            return float(profile[-1][1])

        # Binary search for bracketing points
        lo = 0
        hi = len(profile) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            dmid = profile[mid][0]
            if dmid < distance_m:
                lo = mid + 1
            elif dmid > distance_m:
                hi = mid - 1
            else:
                return float(profile[mid][1])

        j = max(1, lo)
        d1, z1 = profile[j - 1]
        d2, z2 = profile[j]
        if d2 <= d1:
            return float(z1)
        t = (distance_m - d1) / (d2 - d1)
        return float(z1 + t * (z2 - z1))

    # --------------------------- Side slope ---------------------------

    def _compute_side_slope_at_station(
        self,
        part_points: Sequence[QgsPointXY],
        part_len_m: float,
        dist_m: float,
        tangent_delta_m: float,
        center_xy: QgsPointXY,
        distance_area: QgsDistanceArea,
        line_crs,
        depth_source_mode: int,
        raster_sources: Sequence[_RasterSource],
        contour_index: Optional[QgsSpatialIndex],
        contour_data: Optional[Dict[int, Tuple[QgsGeometry, float]]],
        search_m: float,
    ) -> Optional[float]:
        # Derive local tangent using points ahead/behind
        d0 = max(0.0, dist_m - tangent_delta_m)
        d1 = min(part_len_m, dist_m + tangent_delta_m)
        g0 = self._interpolate_point_along_points(part_points, d0, distance_area)
        g1 = self._interpolate_point_along_points(part_points, d1, distance_area)
        if g0 is None or g1 is None:
            return None

        is_geo = False
        try:
            is_geo = bool(line_crs and line_crs.isGeographic())
        except Exception:
            is_geo = False

        nx = ny = 0.0
        normal_bearing = None
        port_pt = None
        stbd_pt = None

        if is_geo:
            try:
                bearing = float(distance_area.bearing(QgsPointXY(g0.x(), g0.y()), QgsPointXY(g1.x(), g1.y())))
            except Exception:
                return None
            normal_bearing = bearing + (math.pi / 2.0)  # starboard
            try:
                stbd_pt = distance_area.computeSpheroidProject(QgsPointXY(center_xy.x(), center_xy.y()), search_m, normal_bearing)
                port_pt = distance_area.computeSpheroidProject(QgsPointXY(center_xy.x(), center_xy.y()), search_m, normal_bearing + math.pi)
            except Exception:
                return None
            nx = math.sin(normal_bearing)
            ny = math.cos(normal_bearing)
        else:
            dx = g1.x() - g0.x()
            dy = g1.y() - g0.y()
            mag = math.hypot(dx, dy)
            if mag <= 0:
                return None
            ux = dx / mag
            uy = dy / mag
            nx = uy
            ny = -ux
            port_pt = QgsPointXY(center_xy.x() - nx * search_m, center_xy.y() - ny * search_m)
            stbd_pt = QgsPointXY(center_xy.x() + nx * search_m, center_xy.y() + ny * search_m)

        if port_pt is None or stbd_pt is None:
            return None

        # Raster mode: sample across transect and fit depth = a + b*t (t in meters, + starboard)
        if depth_source_mode == 1:
            if not raster_sources:
                return None
            cross_sample_count = 21 if search_m >= 500.0 else 11
            offsets = [(-search_m + i * (2.0 * search_m) / (cross_sample_count - 1)) for i in range(cross_sample_count)]
            t_vals: List[float] = []
            z_vals: List[float] = []
            for t in offsets:
                if is_geo:
                    if normal_bearing is None:
                        continue
                    try:
                        if t >= 0:
                            pt = distance_area.computeSpheroidProject(QgsPointXY(center_xy.x(), center_xy.y()), float(t), normal_bearing)
                        else:
                            pt = distance_area.computeSpheroidProject(QgsPointXY(center_xy.x(), center_xy.y()), float(-t), normal_bearing + math.pi)
                    except Exception:
                        continue
                else:
                    pt = QgsPointXY(center_xy.x() + nx * float(t), center_xy.y() + ny * float(t))

                z = self._sample_rasters_at_point(pt, raster_sources)
                if z is None:
                    continue
                t_vals.append(float(t))
                z_vals.append(float(z))

            if len(t_vals) < 2:
                return None

            b = self._ols_slope(t_vals, z_vals)
            if b is None:
                return None
            slope_rad = math.atan2(float(b), 1.0)  # b = dz/dt
            return math.degrees(slope_rad)

        # Contour mode: intersections along transect and fit depth vs t
        if depth_source_mode == 2:
            if contour_index is None or contour_data is None:
                return None

            transect = QgsGeometry.fromPolylineXY([port_pt, stbd_pt])
            hits = self._contour_intersections(transect, center_xy, nx, ny, distance_area, is_geo, contour_index, contour_data)
            if not hits:
                return None

            # Reduce duplicates: for same depth, keep closest-to-center per side
            best_by_depth_side: Dict[Tuple[int, float], Tuple[float, float, float]] = {}
            for t, z in hits:
                side = 1 if t > 0 else (-1 if t < 0 else 0)
                if side == 0:
                    continue
                key = (side, float(z))
                abs_t = abs(float(t))
                prev = best_by_depth_side.get(key)
                if prev is None or abs_t < prev[0]:
                    best_by_depth_side[key] = (abs_t, float(t), float(z))

            pairs = [(v[1], v[2]) for v in best_by_depth_side.values()]
            if len(pairs) < 2:
                return None

            t_vals = [p[0] for p in pairs]
            z_vals = [p[1] for p in pairs]
            b = self._ols_slope(t_vals, z_vals)
            if b is None:
                return None
            slope_rad = math.atan2(float(b), 1.0)
            return math.degrees(slope_rad)

        return None

    @staticmethod
    def _contour_intersections(
        transect: QgsGeometry,
        center_xy: QgsPointXY,
        nx: float,
        ny: float,
        distance_area: QgsDistanceArea,
        is_geo: bool,
        contour_index: QgsSpatialIndex,
        contour_data: Dict[int, Tuple[QgsGeometry, float]],
    ) -> List[Tuple[float, float]]:
        if transect is None or transect.isEmpty():
            return []

        bbox = transect.boundingBox()
        candidate_ids = contour_index.intersects(bbox)
        if not candidate_ids:
            return []

        cx = float(center_xy.x())
        cy = float(center_xy.y())

        out: List[Tuple[float, float]] = []
        for cid in candidate_ids:
            item = contour_data.get(cid)
            if not item:
                continue
            geom, depth = item
            try:
                inter = transect.intersection(geom)
            except Exception:
                continue
            if inter is None or inter.isEmpty():
                continue

            points: List[QgsPointXY] = []
            try:
                if inter.type() == QgsWkbTypes.PointGeometry:
                    pts = inter.asMultiPoint() if inter.isMultipart() else [inter.asPoint()]
                    points = [QgsPointXY(p) for p in pts]
                elif inter.type() == QgsWkbTypes.LineGeometry:
                    if inter.isMultipart():
                        for pl in inter.asMultiPolyline():
                            points.extend(QgsPointXY(p) for p in pl)
                    else:
                        points.extend(QgsPointXY(p) for p in inter.asPolyline())
            except Exception:
                points = []

            for p in points:
                try:
                    if is_geo:
                        sign_v = (p.x() - cx) * nx + (p.y() - cy) * ny
                        sign = 1.0 if sign_v > 0 else (-1.0 if sign_v < 0 else 0.0)
                        if sign == 0.0:
                            t = 0.0
                        else:
                            dist_m = float(distance_area.measureLine(QgsPointXY(cx, cy), QgsPointXY(p.x(), p.y())))
                            t = sign * dist_m
                    else:
                        vx = float(p.x()) - cx
                        vy = float(p.y()) - cy
                        t = vx * nx + vy * ny
                    out.append((float(t), float(depth)))
                except Exception:
                    continue

        return out

    @staticmethod
    def _ols_slope(x_vals: Sequence[float], y_vals: Sequence[float]) -> Optional[float]:
        if not x_vals or not y_vals or len(x_vals) != len(y_vals) or len(x_vals) < 2:
            return None

        # Filter finite pairs
        pairs = [(float(x), float(y)) for x, y in zip(x_vals, y_vals) if math.isfinite(x) and math.isfinite(y)]
        if len(pairs) < 2:
            return None

        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]

        x_mean = sum(xs) / float(len(xs))
        y_mean = sum(ys) / float(len(ys))

        denom = sum((x - x_mean) ** 2 for x in xs)
        if denom <= 0:
            return None

        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        return num / denom

    # --------------------------- Geometry helpers --------------------------

    @staticmethod
    def _as_line_parts(geom: QgsGeometry) -> List[List[QgsPointXY]]:
        if geom is None or geom.isEmpty():
            return []
        try:
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
                return [[QgsPointXY(p) for p in part] for part in parts if part]
            part = geom.asPolyline()
            return [[QgsPointXY(p) for p in part]] if part else []
        except Exception:
            return []

    @staticmethod
    def _measure_polyline_m(points: Sequence[QgsPointXY], distance_area: QgsDistanceArea) -> float:
        if not points or len(points) < 2:
            return 0.0
        total = 0.0
        for p0, p1 in zip(points[:-1], points[1:]):
            try:
                total += float(distance_area.measureLine(p0, p1))
            except Exception:
                total += math.hypot(p1.x() - p0.x(), p1.y() - p0.y())
        return float(total)

    @staticmethod
    def _interpolate_point_along_points(
        points: Sequence[QgsPointXY],
        distance_m: float,
        distance_area: QgsDistanceArea,
    ) -> Optional[QgsPointXY]:
        if not points:
            return None
        if distance_m <= 0:
            return points[0]

        cumulative = 0.0
        for p0, p1 in zip(points[:-1], points[1:]):
            try:
                seg = float(distance_area.measureLine(p0, p1))
            except Exception:
                seg = math.hypot(p1.x() - p0.x(), p1.y() - p0.y())
            if seg <= 0:
                continue
            if cumulative + seg >= distance_m:
                r = (distance_m - cumulative) / seg
                return QgsPointXY(p0.x() + r * (p1.x() - p0.x()), p0.y() + r * (p1.y() - p0.y()))
            cumulative += seg

        return points[-1]

    @staticmethod
    def _measure_along_parts_m(
        parts: Sequence[Sequence[QgsPointXY]],
        point_xy: QgsPointXY,
        distance_area: QgsDistanceArea,
    ) -> Optional[float]:
        cumulative = 0.0
        best_dist_sq = None
        best_along = None

        px = float(point_xy.x())
        py = float(point_xy.y())

        for part in parts:
            if not part or len(part) < 2:
                continue
            for p1, p2 in zip(part[:-1], part[1:]):
                try:
                    seg_len = float(distance_area.measureLine(p1, p2))
                except Exception:
                    seg_len = math.hypot(p2.x() - p1.x(), p2.y() - p1.y())
                if seg_len <= 0:
                    cumulative += seg_len
                    continue

                # Planar projection to get segment fraction; length comes from distance_area
                dx = float(p2.x() - p1.x())
                dy = float(p2.y() - p1.y())
                seg_sq = dx * dx + dy * dy
                if seg_sq <= 0:
                    cumulative += seg_len
                    continue

                t = ((px - float(p1.x())) * dx + (py - float(p1.y())) * dy) / seg_sq
                t_clamped = max(0.0, min(1.0, t))
                proj_x = float(p1.x()) + t_clamped * dx
                proj_y = float(p1.y()) + t_clamped * dy

                dist_sq = (px - proj_x) ** 2 + (py - proj_y) ** 2
                along = cumulative + t_clamped * seg_len

                if best_dist_sq is None or dist_sq < best_dist_sq:
                    best_dist_sq = dist_sq
                    best_along = along

                cumulative += seg_len

        return best_along

    # --------------------------- Stats helpers ----------------------------

    @staticmethod
    def _min_max_avg(values: Iterable[Optional[float]]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
        if not vals:
            return None, None, None
        vmin = min(vals)
        vmax = max(vals)
        vavg = sum(vals) / float(len(vals))
        return float(vmin), float(vmax), float(vavg)

    @staticmethod
    def _pos_max_neg_min(values: Iterable[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
        """Return (max_positive, min_negative) from a signed series.

        - max_positive: the largest value > 0 (or None if no positive values)
        - min_negative: the smallest value < 0 (or None if no negative values)
        """
        pos: List[float] = []
        neg: List[float] = []
        for v in values:
            if v is None:
                continue
            try:
                fv = float(v)
            except Exception:
                continue
            if not math.isfinite(fv):
                continue
            if fv > 0:
                pos.append(fv)
            elif fv < 0:
                neg.append(fv)
        return (max(pos) if pos else None, min(neg) if neg else None)

    @staticmethod
    def _unique_field_name(fields: QgsFields, base: str) -> str:
        existing = set(fields.names())
        if base not in existing:
            return base
        k = 2
        while f'{base}_{k}' in existing:
            k += 1
        return f'{base}_{k}'

    def name(self):
        return 'kp_range_depth_slope_summary'

    def displayName(self):
        return self.tr('KP Range Depth + Slope Summary')

    def group(self):
        return self.tr('KP Ranges')

    def groupId(self):
        return 'kp_ranges'

    def shortHelpString(self):
        return self.tr(
            """
Adds depth and slope summary fields to a KP range line layer.

Inputs:
- KP range line layer (each feature is summarised independently)
- Depth source:
  - Raster(s): samples band 1 and prefers the smallest grid size where rasters overlap.
  - Contours: uses 1–2 contour layers and their depth fields.

Outputs:
- depth_min / depth_max / depth_avg
- slope_min_deg / slope_max_deg / slope_avg_deg (along-route slope, signed)
- side_min_deg / side_max_deg / side_avg_deg (cross-track side slope, signed; +ve down to starboard)

Optional (when enabled):
- slope_up_max_deg (max +ve along-route slope)
- slope_down_min_deg (most -ve along-route slope)
- side_stbd_max_deg (max +ve side slope)
- side_port_min_deg (most -ve side slope)

Notes:
- Along-route slope sign matches Depth Profile default (Invert Slope enabled): +ve = up-slope.
- Side-slope sign matches Depth Profile: +ve = down to starboard.
- Adaptive sampling (Raster only): step = max(interval, factor × raster pixel size at each station).
- For contour mode, enabling interpolation will fill depth between contour intersections along the KP range.
"""
        )

    def createInstance(self):
        return KPRangeDepthSlopeSummaryAlgorithm()

    def tr(self, string):
        return QCoreApplication.translate('KPRangeDepthSlopeSummaryAlgorithm', string)
