# dynamic_buffer_lay_corridor_algorithm.py
# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsLineString,
    QgsPointXY,
    QgsFeatureRequest,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterField,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsWkbTypes,
)


class DynamicBufferLayCorridorAlgorithm(QgsProcessingAlgorithm):
    """Creates a buffer (lay corridor) around a route line.

    Corridor width can be fixed (classic buffer) or dynamic based on sampled water depth.
    Depth can be sourced from one or more rasters, or one or more contour line layers.

    Notes:
    - Dynamic buffering is approximated by sampling points along the route and buffering
      each small segment between samples at the corresponding width, then dissolving.
    - Depth sign conventions vary; the algorithm defaults to using absolute values.
    """

    INPUT = 'INPUT'
    MODE = 'MODE'
    FIXED_BUFFER_M = 'FIXED_BUFFER_M'
    DEPTH_SOURCE = 'DEPTH_SOURCE'
    RASTER_LAYERS = 'RASTER_LAYERS'
    CONTOUR_LAYER_1 = 'CONTOUR_LAYER_1'
    CONTOUR_DEPTH_FIELD_1 = 'CONTOUR_DEPTH_FIELD_1'
    CONTOUR_LAYER_2 = 'CONTOUR_LAYER_2'
    CONTOUR_DEPTH_FIELD_2 = 'CONTOUR_DEPTH_FIELD_2'
    CONTOUR_SEARCH_RADIUS_M = 'CONTOUR_SEARCH_RADIUS_M'
    SAMPLE_INTERVAL_M = 'SAMPLE_INTERVAL_M'
    USE_ABS_DEPTH = 'USE_ABS_DEPTH'
    MISSING_DEPTH_BUFFER_M = 'MISSING_DEPTH_BUFFER_M'

    THRESH_25 = 'THRESH_25'
    THRESH_100 = 'THRESH_100'
    THRESH_1000 = 'THRESH_1000'
    BUF_LT_25 = 'BUF_LT_25'
    BUF_LT_100 = 'BUF_LT_100'
    BUF_LT_1000 = 'BUF_LT_1000'
    BUF_GE_1000 = 'BUF_GE_1000'

    DISSOLVE = 'DISSOLVE'

    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Input route line layer'),
                [QgsProcessing.TypeVectorLine],
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.MODE,
                self.tr('Buffer mode'),
                options=[self.tr('Fixed buffer'), self.tr('Depth-based rules')],
                defaultValue=1,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.FIXED_BUFFER_M,
                self.tr('Fixed buffer distance (m) (Fixed buffer mode only)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=30.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.DEPTH_SOURCE,
                self.tr('Depth source'),
                options=[self.tr('Auto (rasters if provided, else contours)'), self.tr('Raster'), self.tr('Contours')],
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
                self.tr('Bathymetry contour layer 1'),
                [QgsProcessing.TypeVectorLine],
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.CONTOUR_DEPTH_FIELD_1,
                self.tr('Depth field (contour layer 1)'),
                parentLayerParameterName=self.CONTOUR_LAYER_1,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.CONTOUR_LAYER_2,
                self.tr('Bathymetry contour layer 2 (optional)'),
                [QgsProcessing.TypeVectorLine],
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.CONTOUR_DEPTH_FIELD_2,
                self.tr('Depth field (contour layer 2)'),
                parentLayerParameterName=self.CONTOUR_LAYER_2,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.CONTOUR_SEARCH_RADIUS_M,
                self.tr('Contour search radius (m)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=500.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.SAMPLE_INTERVAL_M,
                self.tr('Sampling interval along route (m)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.1,
                defaultValue=50.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.USE_ABS_DEPTH,
                self.tr('Use absolute depth values'),
                defaultValue=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.MISSING_DEPTH_BUFFER_M,
                self.tr('Buffer distance used when depth is missing (m)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=30.0,
            )
        )

        # Default rule set (editable)
        self.addParameter(
            QgsProcessingParameterNumber(
                self.THRESH_25,
                self.tr('Rule threshold A (m) e.g. 25'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=25.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.THRESH_100,
                self.tr('Rule threshold B (m) e.g. 100'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=100.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.THRESH_1000,
                self.tr('Rule threshold C (m) e.g. 1000'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=1000.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUF_LT_25,
                self.tr('Buffer when depth < A (m)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=5.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUF_LT_100,
                self.tr('Buffer when A ≤ depth < B (m)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=10.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUF_LT_1000,
                self.tr('Buffer when B ≤ depth < C (m)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=30.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUF_GE_1000,
                self.tr('Buffer when depth ≥ C (m)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=100.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.DISSOLVE,
                self.tr('Dissolve result (single corridor polygon)'),
                defaultValue=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Lay corridor (buffer)')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if not source:
            raise QgsProcessingException(self.tr('Invalid input line layer'))

        mode = self.parameterAsEnum(parameters, self.MODE, context)
        fixed_buffer_m = self.parameterAsDouble(parameters, self.FIXED_BUFFER_M, context)
        depth_source_mode = self.parameterAsEnum(parameters, self.DEPTH_SOURCE, context)
        raster_layers = [lyr for lyr in (self.parameterAsLayerList(parameters, self.RASTER_LAYERS, context) or []) if isinstance(lyr, QgsRasterLayer)]
        contour_layer_1 = self.parameterAsVectorLayer(parameters, self.CONTOUR_LAYER_1, context)
        contour_depth_field_1 = (self.parameterAsString(parameters, self.CONTOUR_DEPTH_FIELD_1, context) or '').strip()
        contour_layer_2 = self.parameterAsVectorLayer(parameters, self.CONTOUR_LAYER_2, context)
        contour_depth_field_2 = (self.parameterAsString(parameters, self.CONTOUR_DEPTH_FIELD_2, context) or '').strip()
        contour_search_radius_m = self.parameterAsDouble(parameters, self.CONTOUR_SEARCH_RADIUS_M, context)
        sample_interval_m = self.parameterAsDouble(parameters, self.SAMPLE_INTERVAL_M, context)
        use_abs_depth = self.parameterAsBool(parameters, self.USE_ABS_DEPTH, context)
        missing_depth_buffer_m = self.parameterAsDouble(parameters, self.MISSING_DEPTH_BUFFER_M, context)

        thresh_25 = self.parameterAsDouble(parameters, self.THRESH_25, context)
        thresh_100 = self.parameterAsDouble(parameters, self.THRESH_100, context)
        thresh_1000 = self.parameterAsDouble(parameters, self.THRESH_1000, context)
        buf_lt_25 = self.parameterAsDouble(parameters, self.BUF_LT_25, context)
        buf_lt_100 = self.parameterAsDouble(parameters, self.BUF_LT_100, context)
        buf_lt_1000 = self.parameterAsDouble(parameters, self.BUF_LT_1000, context)
        buf_ge_1000 = self.parameterAsDouble(parameters, self.BUF_GE_1000, context)

        dissolve = self.parameterAsBool(parameters, self.DISSOLVE, context)

        if sample_interval_m <= 0:
            raise QgsProcessingException(self.tr('Sampling interval must be > 0'))

        if mode == 1:
            # Depth-based mode requires some depth source.
            if depth_source_mode == 1 and not raster_layers:
                raise QgsProcessingException(self.tr('Depth source is Raster but no raster layers were provided'))
            if depth_source_mode == 2 and not contour_layer_1 and not contour_layer_2:
                raise QgsProcessingException(self.tr('Depth source is Contours but no contour layers were provided'))
            if depth_source_mode == 0 and not raster_layers and not contour_layer_1 and not contour_layer_2:
                raise QgsProcessingException(self.tr('Depth-based rules selected but no raster/contour layers were provided'))

        source_crs = source.sourceCrs()

        # IMPORTANT: QgsGeometry.buffer() uses layer units.
        # If the input CRS is geographic (degrees), buffering with "meters" produces massive/wrong buffers.
        # We therefore do sampling + buffering in a projected working CRS (meters), then transform results back.
        features_preview = list(source.getFeatures(QgsFeatureRequest().setLimit(1)))
        preview_geom = features_preview[0].geometry() if features_preview else None
        working_crs = self._select_working_crs(source_crs, preview_geom)
        to_working = QgsCoordinateTransform(source_crs, working_crs, QgsProject.instance()) if working_crs != source_crs else None
        to_source = QgsCoordinateTransform(working_crs, source_crs, QgsProject.instance()) if working_crs != source_crs else None

        # Distance calculator in working CRS
        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(working_crs, context.transformContext())
        distance_area.setEllipsoid(context.project().ellipsoid())

        # Output fields: keep input fields + a few diagnostics (non-breaking)
        fields = QgsFields(source.fields())
        fields.append(QgsField('buf_min_m', QVariant.Double))
        fields.append(QgsField('buf_max_m', QVariant.Double))
        fields.append(QgsField('depth_ok_pct', QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.MultiPolygon,
            source.sourceCrs(),
        )

        # Build depth samplers
        raster_samplers = self._build_raster_samplers(raster_layers, source_crs)
        contour_samplers = self._build_contour_samplers(
            [contour_layer_1, contour_layer_2],
            [contour_depth_field_1, contour_depth_field_2],
            source_crs,
        )

        # Collect per-feature corridor geometries (for dissolve)
        corridor_geoms: List[QgsGeometry] = []

        # Re-fetch features, since we may have consumed one in preview.
        features = list(source.getFeatures())
        total = len(features) if features else 1
        for idx, f in enumerate(features):
            if feedback.isCanceled():
                break

            feedback.setProgress(int((idx / total) * 100))
            geom = f.geometry()
            if not geom or geom.isEmpty():
                continue

            if geom.type() != QgsWkbTypes.LineGeometry:
                continue

            geom_work = QgsGeometry(geom)
            if to_working is not None:
                try:
                    geom_work.transform(to_working)
                except Exception:
                    feedback.pushWarning(self.tr('Failed to transform a feature into working CRS; skipping it.'))
                    continue

            part_points = self._extract_line_parts_as_points(geom_work)
            if not part_points:
                continue

            segment_buffers: List[QgsGeometry] = []
            used_buffers: List[float] = []
            depth_samples_total = 0
            depth_samples_ok = 0

            for points in part_points:
                part_len = self._measure_points_length(points, distance_area)
                if part_len <= 0:
                    continue

                sampled = self._sample_along_points(points, part_len, sample_interval_m, distance_area)
                if len(sampled) < 2:
                    continue

                # Pre-sample depth at stations
                station_depths: List[Optional[float]] = []
                for pt in sampled:
                    depth = None
                    if mode == 1:
                        pt_source = pt
                        if to_source is not None:
                            try:
                                pt_source = to_source.transform(pt)
                            except Exception:
                                pt_source = pt
                        depth = self._sample_depth(
                            pt_source,
                            depth_source_mode,
                            raster_samplers,
                            contour_samplers,
                            contour_search_radius_m,
                            context,
                        )
                        if depth is not None and use_abs_depth:
                            depth = abs(float(depth))
                    station_depths.append(depth)
                    if mode == 1:
                        depth_samples_total += 1
                        if depth is not None:
                            depth_samples_ok += 1

                # Buffer each small segment
                for i in range(len(sampled) - 1):
                    p0 = sampled[i]
                    p1 = sampled[i + 1]
                    if p0 == p1:
                        continue

                    depth0 = station_depths[i]
                    depth1 = station_depths[i + 1]
                    depth_mid = None
                    if mode == 1:
                        if depth0 is not None and depth1 is not None:
                            depth_mid = (float(depth0) + float(depth1)) / 2.0
                        elif depth0 is not None:
                            depth_mid = float(depth0)
                        elif depth1 is not None:
                            depth_mid = float(depth1)
                        else:
                            # last attempt: sample at segment midpoint
                            mid = QgsPointXY((p0.x() + p1.x()) / 2.0, (p0.y() + p1.y()) / 2.0)
                            mid_source = mid
                            if to_source is not None:
                                try:
                                    mid_source = to_source.transform(mid)
                                except Exception:
                                    mid_source = mid
                            depth_mid = self._sample_depth(
                                mid_source,
                                depth_source_mode,
                                raster_samplers,
                                contour_samplers,
                                contour_search_radius_m,
                                context,
                            )
                            if depth_mid is not None and use_abs_depth:
                                depth_mid = abs(float(depth_mid))

                    buf = fixed_buffer_m if mode == 0 else self._buffer_for_depth(
                        depth_mid,
                        missing_depth_buffer_m,
                        thresh_25,
                        thresh_100,
                        thresh_1000,
                        buf_lt_25,
                        buf_lt_100,
                        buf_lt_1000,
                        buf_ge_1000,
                    )

                    used_buffers.append(buf)
                    seg = QgsGeometry.fromPolylineXY([p0, p1])
                    seg_buf = seg.buffer(buf, 8)
                    if seg_buf and not seg_buf.isEmpty():
                        segment_buffers.append(seg_buf)

            if not segment_buffers:
                continue

            corridor = QgsGeometry.unaryUnion(segment_buffers)
            if not corridor or corridor.isEmpty():
                continue

            # Transform result back to source CRS for output
            if to_source is not None:
                try:
                    corridor.transform(to_source)
                except Exception:
                    feedback.pushWarning(self.tr('Failed to transform corridor back to source CRS; skipping it.'))
                    continue

            if dissolve:
                corridor_geoms.append(corridor)
            else:
                out = QgsFeature(fields)
                attrs = list(f.attributes())
                if used_buffers:
                    attrs.extend([min(used_buffers), max(used_buffers)])
                else:
                    attrs.extend([None, None])
                ok_pct = (depth_samples_ok / depth_samples_total * 100.0) if depth_samples_total else 0.0
                attrs.append(ok_pct)
                out.setAttributes(attrs)
                out.setGeometry(self._as_multipolygon(corridor))
                sink.addFeature(out, QgsFeatureSink.FastInsert)

        if dissolve and corridor_geoms:
            merged = QgsGeometry.unaryUnion(corridor_geoms)
            if merged and not merged.isEmpty():
                out = QgsFeature(fields)
                # No single source feature to copy attrs from in dissolve mode.
                out.setAttributes([None] * len(source.fields()) + [None, None, None])
                out.setGeometry(self._as_multipolygon(merged))
                sink.addFeature(out, QgsFeatureSink.FastInsert)

        feedback.setProgress(100)
        return {self.OUTPUT: dest_id}

    @staticmethod
    def _as_multipolygon(geom: QgsGeometry) -> QgsGeometry:
        if geom.wkbType() in (QgsWkbTypes.Polygon, QgsWkbTypes.CurvePolygon):
            return QgsGeometry.fromMultiPolygonXY([geom.asPolygon()])
        if geom.wkbType() == QgsWkbTypes.MultiPolygon:
            return geom
        if geom.type() == QgsWkbTypes.PolygonGeometry:
            # Could be GeometryCollection; try to extract polygon parts
            parts = [g for g in geom.constParts() if g and QgsGeometry(g).type() == QgsWkbTypes.PolygonGeometry]
            if parts:
                polys = []
                for p in parts:
                    gg = QgsGeometry(p)
                    if gg.wkbType() == QgsWkbTypes.Polygon:
                        polys.append(gg.asPolygon())
                    elif gg.wkbType() == QgsWkbTypes.MultiPolygon:
                        polys.extend(gg.asMultiPolygon())
                if polys:
                    return QgsGeometry.fromMultiPolygonXY(polys)
        return geom

    @staticmethod
    def _buffer_for_depth(
        depth_m: Optional[float],
        missing_depth_buffer_m: float,
        thresh_25: float,
        thresh_100: float,
        thresh_1000: float,
        buf_lt_25: float,
        buf_lt_100: float,
        buf_lt_1000: float,
        buf_ge_1000: float,
    ) -> float:
        if depth_m is None:
            return float(missing_depth_buffer_m)

        depth_m = float(depth_m)
        a = float(thresh_25)
        b = float(thresh_100)
        c = float(thresh_1000)

        # Ensure increasing thresholds
        if not (a <= b <= c):
            # Fall back to canonical ordering if user supplied invalid thresholds
            a, b, c = sorted([a, b, c])

        if depth_m < a:
            return float(buf_lt_25)
        if depth_m < b:
            return float(buf_lt_100)
        if depth_m < c:
            return float(buf_lt_1000)
        return float(buf_ge_1000)

    @staticmethod
    def _extract_line_parts_as_points(geom: QgsGeometry) -> List[List[QgsPointXY]]:
        """Returns list of polyline point arrays; avoids connecting multipart gaps."""
        if not geom or geom.isEmpty():
            return []

        g = geom.constGet()
        parts: List[List[QgsPointXY]] = []

        if isinstance(g, QgsLineString):
            pts = [QgsPointXY(p.x(), p.y()) for p in g.points()]
            if len(pts) >= 2:
                parts.append(pts)
            return parts

        # Multi-part
        try:
            for part in g:
                if isinstance(part, QgsLineString):
                    pts = [QgsPointXY(p.x(), p.y()) for p in part.points()]
                    if len(pts) >= 2:
                        parts.append(pts)
        except Exception:
            # Fallback for unexpected geometry types
            if geom.isMultipart():
                for pl in geom.asMultiPolyline():
                    if len(pl) >= 2:
                        parts.append([QgsPointXY(p.x(), p.y()) for p in pl])
            else:
                pl = geom.asPolyline()
                if len(pl) >= 2:
                    parts.append([QgsPointXY(p.x(), p.y()) for p in pl])

        return parts

    @staticmethod
    def _measure_points_length(points: Sequence[QgsPointXY], distance_area: QgsDistanceArea) -> float:
        total = 0.0
        for i in range(len(points) - 1):
            total += distance_area.measureLine(points[i], points[i + 1])
        return total

    @staticmethod
    def _sample_along_points(
        points: Sequence[QgsPointXY],
        total_length_m: float,
        interval_m: float,
        distance_area: QgsDistanceArea,
    ) -> List[QgsPointXY]:
        if total_length_m <= 0:
            return list(points)

        sampled: List[QgsPointXY] = []
        dist = 0.0
        while dist <= total_length_m:
            pt = DynamicBufferLayCorridorAlgorithm._interpolate_point_along_points(points, dist, distance_area)
            if pt is not None:
                if not sampled or pt != sampled[-1]:
                    sampled.append(pt)
            dist += interval_m

        # Ensure last vertex
        if sampled and sampled[-1] != points[-1]:
            sampled.append(points[-1])

        return sampled

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
        for i in range(len(points) - 1):
            p0 = points[i]
            p1 = points[i + 1]
            seg = distance_area.measureLine(p0, p1)
            if seg <= 0:
                continue
            if cumulative + seg >= distance_m:
                r = (distance_m - cumulative) / seg
                return QgsPointXY(p0.x() + r * (p1.x() - p0.x()), p0.y() + r * (p1.y() - p0.y()))
            cumulative += seg

        return points[-1]

    @staticmethod
    def _build_raster_samplers(
        rasters: Sequence[QgsRasterLayer],
        line_crs,
    ) -> List[Tuple[QgsRasterLayer, Optional[QgsCoordinateTransform]]]:
        samplers: List[Tuple[QgsRasterLayer, Optional[QgsCoordinateTransform]]] = []
        for r in rasters:
            if not r:
                continue
            transform = None
            if r.crs() != line_crs:
                try:
                    transform = QgsCoordinateTransform(line_crs, r.crs(), QgsProject.instance())
                except Exception:
                    transform = None
            samplers.append((r, transform))
        return samplers

    @staticmethod
    def _build_contour_samplers(
        contour_layers: Sequence[Optional[QgsVectorLayer]],
        depth_fields: Sequence[str],
        line_crs,
    ) -> List[Tuple[QgsVectorLayer, str, Optional[QgsCoordinateTransform]]]:
        out: List[Tuple[QgsVectorLayer, str, Optional[QgsCoordinateTransform]]] = []
        for i, lyr in enumerate(contour_layers):
            if not lyr:
                continue
            depth_field = depth_fields[i] if i < len(depth_fields) else ''
            # If user didn't pick a field, we'll fall back to the first field at runtime.
            transform = None
            if lyr.crs() != line_crs:
                try:
                    transform = QgsCoordinateTransform(line_crs, lyr.crs(), QgsProject.instance())
                except Exception:
                    transform = None
            out.append((lyr, depth_field, transform))
        return out

    @staticmethod
    def _sample_depth(
        point: QgsPointXY,
        depth_source_mode: int,
        raster_samplers: Sequence[Tuple[QgsRasterLayer, Optional[QgsCoordinateTransform]]],
        contour_samplers: Sequence[Tuple[QgsVectorLayer, str, Optional[QgsCoordinateTransform]]],
        contour_search_radius_m: float,
        context,
    ) -> Optional[float]:
        # depth_source_mode: 0=Auto, 1=Raster, 2=Contours
        want_raster = depth_source_mode in (0, 1)
        want_contours = depth_source_mode in (0, 2)
        if depth_source_mode == 1:
            want_contours = False
        if depth_source_mode == 2:
            want_raster = False

        if want_raster and raster_samplers:
            for raster, transform in raster_samplers:
                sample_pt = point
                if transform is not None:
                    try:
                        sample_pt = transform.transform(point)
                    except Exception:
                        continue
                try:
                    val, ok = raster.dataProvider().sample(sample_pt, 1)
                except Exception:
                    ok = False
                    val = None
                if ok and val is not None:
                    try:
                        return float(val)
                    except Exception:
                        continue

        if want_contours and contour_samplers:
            best_depth = None
            best_dist = None
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

                # Cheap bbox filter using buffer radius if supplied
                feat_iter = None
                if contour_search_radius_m and contour_search_radius_m > 0:
                    if lyr.crs().isGeographic():
                        # Approx meters -> degrees (best-effort, avoids absurd bbox sizes)
                        lat = query_point.y()
                        deg_lat = contour_search_radius_m / 111320.0
                        cos_lat = max(0.1, abs(__import__('math').cos(__import__('math').radians(lat))))
                        deg_lon = contour_search_radius_m / (111320.0 * cos_lat)
                        rect = pt_geom.boundingBox()
                        rect.setXMinimum(rect.xMinimum() - deg_lon)
                        rect.setXMaximum(rect.xMaximum() + deg_lon)
                        rect.setYMinimum(rect.yMinimum() - deg_lat)
                        rect.setYMaximum(rect.yMaximum() + deg_lat)
                        request = QgsFeatureRequest().setFilterRect(rect)
                        feat_iter = lyr.getFeatures(request)
                    else:
                        rect = pt_geom.buffer(contour_search_radius_m, 8).boundingBox()
                        request = QgsFeatureRequest().setFilterRect(rect)
                        feat_iter = lyr.getFeatures(request)
                if feat_iter is None:
                    feat_iter = lyr.getFeatures()

                # Distance computation in meters if layer CRS is geographic
                dist_area = None
                if lyr.crs().isGeographic():
                    dist_area = QgsDistanceArea()
                    dist_area.setSourceCrs(lyr.crs(), context.transformContext())
                    dist_area.setEllipsoid(context.project().ellipsoid())

                for feat in feat_iter:
                    g = feat.geometry()
                    if not g or g.isEmpty():
                        continue

                    if dist_area is None:
                        # Projected layer: geometry distance matches meters (or at least linear units)
                        dist = g.distance(pt_geom)
                        if contour_search_radius_m and contour_search_radius_m > 0 and dist > contour_search_radius_m:
                            continue
                    else:
                        # Geographic layer: compute geodesic distance to closest point
                        try:
                            closest = g.closestPoint(pt_geom)
                            closest_pt = closest.asPoint() if not closest.isEmpty() else None
                            if closest_pt is None:
                                continue
                            dist = dist_area.measureLine(query_point, QgsPointXY(closest_pt))
                        except Exception:
                            continue
                        if contour_search_radius_m and contour_search_radius_m > 0 and dist > contour_search_radius_m:
                            continue

                    if best_dist is None or dist < best_dist:
                        # Extract depth
                        if depth_field and depth_field in feat.fields().names():
                            z = feat[depth_field]
                        else:
                            names = feat.fields().names()
                            z = feat[names[0]] if names else None
                        try:
                            zf = float(z)
                        except Exception:
                            continue
                        best_dist = dist
                        best_depth = zf

            return best_depth

        return None

    def shortHelpString(self):
        return self.tr(
            """
Creates a lay corridor buffer around a route line.

You can run it like a normal fixed buffer, or enable depth-based rules so the buffer distance adapts to water depth.

UI/workflow notes:
- “Fixed buffer distance” is used only when Buffer mode = Fixed buffer.
- When using contour layers, pick the depth field from the dropdown (like the Depth Profile dockwidget).

Default rule set (editable):
- depth ≥ 1000 m → 100 m
- 100 m ≤ depth < 1000 m → 30 m
- 25 m ≤ depth < 100 m → 10 m
- depth < 25 m → 5 m

Depth sources:
- Raster(s): samples band 1, using the first raster with valid data at each sample point.
- Contour(s): uses nearest contour feature (within search radius) and reads the selected depth field.

CRS handling:
- If the input line layer is geographic (degrees), the algorithm buffers in a local projected CRS (UTM where possible) so all buffer distances remain in meters.

Missing depth handling:
- If depth is missing at a segment, the algorithm uses the configured “missing depth buffer”.
"""
        )

    @staticmethod
    def _select_working_crs(
        source_crs: QgsCoordinateReferenceSystem,
        preview_geom: Optional[QgsGeometry],
    ) -> QgsCoordinateReferenceSystem:
        """Pick a projected CRS suitable for meter-based buffering.

        If the source CRS is already projected, use it.
        If geographic, prefer UTM zone based on WGS84 longitude/latitude if possible,
        otherwise fall back to Web Mercator.
        """
        if source_crs and not source_crs.isGeographic():
            return source_crs

        # Try to determine UTM zone using transformation to WGS84
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        try:
            to_wgs = QgsCoordinateTransform(source_crs, wgs84, QgsProject.instance())
            if preview_geom and not preview_geom.isEmpty():
                c = preview_geom.centroid().asPoint() if not preview_geom.centroid().isEmpty() else None
                src_pt = QgsPointXY(c) if c is not None else QgsPointXY(0.0, 0.0)
            else:
                src_pt = QgsPointXY(0.0, 0.0)

            p = to_wgs.transform(src_pt)
            lon = p.x()
            lat = p.y()
            zone = int((lon + 180.0) / 6.0) + 1
            zone = min(60, max(1, zone))
            epsg = (32600 + zone) if lat >= 0 else (32700 + zone)
            utm = QgsCoordinateReferenceSystem(f'EPSG:{epsg}')
            if utm.isValid():
                return utm
        except Exception:
            pass

        # Robust fallback: meters, global
        merc = QgsCoordinateReferenceSystem('EPSG:3857')
        return merc if merc.isValid() else source_crs

    def name(self):
        return 'dynamic_buffer_lay_corridor'

    def displayName(self):
        return self.tr('Dynamic Buffer (Lay Corridor)')

    def group(self):
        return self.tr('Other tools')

    def groupId(self):
        return 'other_tools'

    def tr(self, string):
        return QCoreApplication.translate('DynamicBufferLayCorridorAlgorithm', string)

    def createInstance(self):
        return DynamicBufferLayCorridorAlgorithm()
