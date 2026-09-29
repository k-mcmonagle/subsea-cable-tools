# seabed_length_algorithm.py
# -*- coding: utf-8 -*-
"""
Seabed Length Calculation Algorithm
Calculates the 3D seabed length (bottom length) for an RPL route using bathymetry data.

This algorithm:
1. Takes an RPL line layer and bathymetry (raster or contour layer)
2. Samples depths along the route at configurable intervals
3. Computes 3D length by summing distances between consecutive points
4. Optionally performs sensitivity analysis with multiple intervals
5. Outputs the seabed length and elongation ratio
"""

__author__ = 'Kieran McMonagle'
__date__ = '2024-10-23'
__copyright__ = '(C) 2024 by Kieran McMonagle'

import math
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QCoreApplication
from .algorithm_base import SubseaCableAlgorithm
from .depth_sampling import contour_feature_depth
from ..kp_range_utils import make_distance_area
from ..kp_geo_utils import RouteFrame, iter_line_parts, ordered_route_geometry
from qgis.core import (
    QgsProcessing,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer as ContourLayerParam,  # For contours
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterField,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterEnum,
    QgsProcessingException,
    QgsFeatureSink,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsDistanceArea,
    QgsCoordinateTransform,
    QgsCsException,
    QgsSpatialIndex,
)
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_STRING, PROCESSING_NUMBER_INTEGER

# Stations sampled between two cancellation checks.
_CANCEL_CHECK_EVERY = 500


class _RasterDepth:
    """Depth at a point (line CRS) from band 1 of a raster."""

    def __init__(self, raster_layer, line_crs, transform_context):
        provider = raster_layer.dataProvider()
        # A worker-owned clone: the layer's own provider belongs to the
        # main thread.
        self._provider = provider.clone() or provider
        self._transform = None
        if raster_layer.crs() != line_crs:
            self._transform = QgsCoordinateTransform(line_crs, raster_layer.crs(), transform_context)

    def depth(self, point: QgsPointXY) -> Optional[float]:
        sample_point = point
        if self._transform is not None:
            try:
                sample_point = self._transform.transform(point)
            except QgsCsException:
                return None
        sample, ok = self._provider.sample(sample_point, 1)
        return float(sample) if ok else None


class _ContourDepth:
    """Contour lines reprojected into the line CRS, in one spatial index.

    Built once per run: every station and every route used to scan (and
    intersect) the whole contour layer.
    """

    def __init__(self, contour_layer, depth_field, line_crs, transform_context):
        flag = getattr(QgsSpatialIndex, 'FlagStoreFeatureGeometries', None)
        if flag is None:
            flag = QgsSpatialIndex.Flag.FlagStoreFeatureGeometries
        self._index = QgsSpatialIndex(flag)
        self._depths: Dict[int, float] = {}
        self.skipped_transform = 0
        self.skipped_depth = 0
        transform = None
        if contour_layer.crs() != line_crs:
            transform = QgsCoordinateTransform(contour_layer.crs(), line_crs, transform_context)
        for feat in contour_layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            # The named depth field, else the first field (as before).
            depth = contour_feature_depth(feat, depth_field)
            if depth is None:
                self.skipped_depth += 1
                continue
            if transform is not None:
                geom = QgsGeometry(geom)
                try:
                    geom.transform(transform)
                except QgsCsException:
                    self.skipped_transform += 1
                    continue
                feat = QgsFeature(feat)
                feat.setGeometry(geom)
            self._index.addFeature(feat)
            self._depths[int(feat.id())] = depth

    def depth(self, point: QgsPointXY) -> Optional[float]:
        """Depth of the nearest contour (planar distance in the line CRS)."""
        if not self._depths:
            return None
        pt_geom = QgsGeometry.fromPointXY(point)
        best = None  # (distance, fid): ties go to the lowest feature id
        for fid in self._index.nearestNeighbor(point, 4):
            geom = self._index.geometry(fid)
            if geom is None or geom.isEmpty():
                continue
            candidate = (float(geom.distance(pt_geom)), fid)
            if best is None or candidate < best:
                best = candidate
        return self._depths[best[1]] if best is not None else None

    def crossings(self, line_geom: QgsGeometry) -> List[Tuple[QgsPointXY, float]]:
        """``(point, depth)`` where contours cross ``line_geom``."""
        out: List[Tuple[QgsPointXY, float]] = []
        for fid in self._index.intersects(line_geom.boundingBox()):
            geom = self._index.geometry(fid)
            if geom is None or geom.isEmpty():
                continue
            inter = line_geom.intersection(geom)
            if inter is None or inter.isEmpty():
                continue
            depth = self._depths[fid]
            out.extend((pt, depth) for pt in _point_parts(inter))
        return out


def _point_parts(geom: QgsGeometry) -> List[QgsPointXY]:
    """Point parts of an intersection (Point/PointZ/PointM, multi or mixed)."""
    points: List[QgsPointXY] = []
    for part in geom.constParts():
        flat = QgsWkbTypes.flatType(part.wkbType())
        if flat == QgsWkbTypes.Point:
            points.append(QgsPointXY(part.x(), part.y()))
        elif flat in (QgsWkbTypes.MultiPoint, QgsWkbTypes.GeometryCollection):
            points.extend(_point_parts(QgsGeometry(part.clone())))
    return points


class _SeabedSampler:
    """Samples depth along a route and sums the 3D (seabed) length.

    Chainage comes from the plugin's KP machinery (``RouteFrame``): one
    geodesic walk per line, then a bisect per station (the old per-station
    re-walk made long routes O(stations x vertices)).
    """

    def __init__(self, distance_area: QgsDistanceArea, depth_source, feedback):
        self._distance = distance_area
        self._source = depth_source
        self._feedback = feedback
        self._contours = isinstance(depth_source, _ContourDepth)

    def seabed_length(self, geom: QgsGeometry, interval_m) -> Tuple[float, List[Tuple[QgsPointXY, Optional[float]]]]:
        """Seabed length (m) of ``geom`` and the ``(point, depth)`` samples.

        Each part of a multi-part route is measured on its own: the gap
        between parts is not seabed.
        """
        total = 0.0
        samples: List[Tuple[QgsPointXY, Optional[float]]] = []
        for part in iter_line_parts(geom):
            points = [QgsPointXY(p) for p in part]
            if len(points) < 2:
                continue
            part_geom = QgsGeometry.fromPolylineXY(points)
            if self._contours:
                part_samples = self._contour_samples(part_geom, points)
            else:
                part_samples = self._raster_samples(part_geom, points, interval_m)
            samples.extend(part_samples)
            total += self._length_3d(part_samples)
        return total, samples

    def _raster_samples(self, part_geom, points, interval_m):
        route = RouteFrame.from_source(part_geom, self._distance)
        total_length = self._distance.measureLength(part_geom)
        samples = []
        station = 0
        while station * interval_m <= total_length:
            if station % _CANCEL_CHECK_EVERY == 0 and self._feedback.isCanceled():
                return samples
            point = route.point_at_kp(station * interval_m / 1000.0, clamp=True)
            if point is not None:
                samples.append((point, self._source.depth(point)))
            station += 1
        # Always end on the last vertex.
        if samples and samples[-1][0] != points[-1]:
            samples.append((points[-1], self._source.depth(points[-1])))
        return samples

    def _contour_samples(self, part_geom, points):
        """Start, every contour crossing and end, in chainage order.

        Chainage is geodesic metres along the line (``RouteFrame``) for every
        sample — the crossings used to be ordered by planar location while
        the end point used the geodesic length, which could misplace the end
        on a projected CRS with scale factor > 1.
        """
        route = RouteFrame.from_source(part_geom, self._distance)
        start, end = points[0], points[-1]
        ordered = [(start, self._source.depth(start), 0.0)]
        for point, depth in self._source.crossings(part_geom):
            ordered.append((point, depth, route.kp_at_point(point).kp_km * 1000.0))
        ordered.append((end, self._source.depth(end), route.total_length_m))
        ordered.sort(key=lambda sample: sample[2])
        return [(point, depth) for point, depth, _chainage in ordered]

    def _length_3d(self, samples) -> float:
        """Sum of ``sqrt(plan^2 + dz^2)`` between consecutive valid samples."""
        seabed_length = 0.0
        valid_pts = [(p, z) for p, z in samples if z is not None]
        for i in range(1, len(valid_pts)):
            p0, z0 = valid_pts[i - 1]
            p1, z1 = valid_pts[i]
            plan_dist = self._distance.measureLine(p0, p1)
            dz = z1 - z0
            seabed_length += math.sqrt(plan_dist ** 2 + dz ** 2)
        return seabed_length


class SeabedLengthAlgorithm(SubseaCableAlgorithm):
    """
    Calculate seabed (3D) length for RPL routes using bathymetry.
    """

    INPUT_LINE = 'INPUT_LINE'
    BATHY_TYPE = 'BATHY_TYPE'
    INPUT_RASTER = 'INPUT_RASTER'
    INPUT_CONTOURS = 'INPUT_CONTOURS'
    DEPTH_FIELD = 'DEPTH_FIELD'
    SAMPLING_INTERVAL = 'SAMPLING_INTERVAL'
    SENSITIVITY_ANALYSIS = 'SENSITIVITY_ANALYSIS'
    SENSITIVITY_INTERVALS = 'SENSITIVITY_INTERVALS'
    OUTPUT_INTERVALS = 'OUTPUT_INTERVALS'
    KP_INTERVAL = 'KP_INTERVAL'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_LINE,
                self.tr('RPL Route Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.BATHY_TYPE,
                self.tr('Bathymetry Type'),
                options=[self.tr('Raster'), self.tr('Contour')],
                defaultValue=0
            )
        )

        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT_RASTER,
                self.tr('Bathymetry Raster Layer'),
                optional=True
            )
        )

        self.addParameter(
            ContourLayerParam(
                self.INPUT_CONTOURS,
                self.tr('Bathymetry Contour Layer'),
                [QgsProcessing.TypeVectorLine],
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.DEPTH_FIELD,
                self.tr('Depth Field Name (for contours)'),
                parentLayerParameterName=self.INPUT_CONTOURS,
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.SAMPLING_INTERVAL,
                self.tr('Sampling Interval (m) - used for Raster bathymetry'),
                type=PROCESSING_NUMBER_INTEGER,
                minValue=1,
                maxValue=1000,
                defaultValue=10
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SENSITIVITY_ANALYSIS,
                self.tr('Perform Sensitivity Analysis'),
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.SENSITIVITY_INTERVALS,
                self.tr('Sensitivity Intervals (comma-separated, m)'),
                defaultValue='1,5,10,25,50,100'
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.OUTPUT_INTERVALS,
                self.tr('Output at Regular KP Intervals'),
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.KP_INTERVAL,
                self.tr('KP Interval (km)'),
                type=PROCESSING_NUMBER_INTEGER,
                minValue=1,
                maxValue=100,
                defaultValue=10
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Seabed Length Results')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        line_layer = self.parameterAsVectorLayer(parameters, self.INPUT_LINE, context)
        if not line_layer:
            raise QgsProcessingException(self.tr('Invalid line layer'))

        bathy_type = self.parameterAsEnum(parameters, self.BATHY_TYPE, context)
        raster_layer = self.parameterAsRasterLayer(parameters, self.INPUT_RASTER, context) if bathy_type == 0 else None
        contour_layer = self.parameterAsVectorLayer(parameters, self.INPUT_CONTOURS, context) if bathy_type == 1 else None
        depth_field = self.parameterAsString(parameters, self.DEPTH_FIELD, context) or 'depth'

        if bathy_type == 0 and not raster_layer:
            raise QgsProcessingException(self.tr('Raster layer required for raster bathymetry'))
        if bathy_type == 1 and not contour_layer:
            raise QgsProcessingException(self.tr('Contour layer required for contour bathymetry'))

        sampling_interval = self.parameterAsInt(parameters, self.SAMPLING_INTERVAL, context)
        do_sensitivity = self.parameterAsBool(parameters, self.SENSITIVITY_ANALYSIS, context)
        sensitivity_intervals_str = self.parameterAsString(parameters, self.SENSITIVITY_INTERVALS, context)
        output_intervals = self.parameterAsBool(parameters, self.OUTPUT_INTERVALS, context)
        kp_interval_km = self.parameterAsInt(parameters, self.KP_INTERVAL, context)

        # Parse sensitivity intervals
        sensitivity_intervals = []
        if do_sensitivity:
            try:
                sensitivity_intervals = [int(x.strip()) for x in sensitivity_intervals_str.split(',')]
            except (ValueError, AttributeError):
                sensitivity_intervals = [1, 5, 10, 25, 50, 100]

        # Prepare output fields (always include all possible fields)
        fields = QgsFields()
        fields.append(QgsField('route_id', FIELD_TYPE_STRING))
        fields.append(QgsField('plan_length_m', FIELD_TYPE_DOUBLE))
        fields.append(QgsField('seabed_length_m', FIELD_TYPE_DOUBLE))
        fields.append(QgsField('elongation_ratio', FIELD_TYPE_DOUBLE))
        fields.append(QgsField('sampling_interval_m', FIELD_TYPE_INT))
        if do_sensitivity:
            fields.append(QgsField('sensitivity_results', FIELD_TYPE_STRING))
        if output_intervals:
            fields.append(QgsField('kp_start', FIELD_TYPE_DOUBLE))
            fields.append(QgsField('kp_end', FIELD_TYPE_DOUBLE))
            fields.append(QgsField('segment_length_m', FIELD_TYPE_DOUBLE))
            fields.append(QgsField('seabed_segment_length_m', FIELD_TYPE_DOUBLE))
            # elongation_ratio already added

        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT, context, fields, QgsWkbTypes.NoGeometry, line_layer.crs())

        # One distance calculator and one depth source for the whole run.
        line_crs = line_layer.crs()
        distance_area = make_distance_area(
            line_layer.sourceCrs(), context.transformContext(), project=context.project()
        )
        if bathy_type == 0:
            depth_source = _RasterDepth(raster_layer, line_crs, context.transformContext())
        else:
            depth_source = _ContourDepth(contour_layer, depth_field, line_crs, context.transformContext())
            if depth_source.skipped_transform:
                feedback.pushWarning(
                    f"{depth_source.skipped_transform} contour feature(s) could not be reprojected "
                    "into the route CRS and were ignored.")
            if depth_source.skipped_depth:
                feedback.pushWarning(
                    f"{depth_source.skipped_depth} contour feature(s) have no numeric value in "
                    f"'{depth_field}' and were ignored.")
        sampler = _SeabedSampler(distance_area, depth_source, feedback)

        # Group features by route_id
        routes = {}
        for feature in line_layer.getFeatures():
            route_id = feature['route_id'] if 'route_id' in feature.fields().names() else 'default_route'
            if route_id not in routes:
                routes[route_id] = []
            routes[route_id].append(feature)

        total_routes = len(routes)
        for route_idx, (route_id, features) in enumerate(routes.items()):
            if feedback.isCanceled():
                break

            feedback.setProgress((route_idx / total_routes) * 100)

            # Merge geometries for the route
            # Shared route builder (SeqNo/layer order, no noding).
            merged_geom = ordered_route_geometry(list(features))

            if not merged_geom or merged_geom.isEmpty():
                continue

            # Calculate plan length
            plan_length = distance_area.measureLength(merged_geom)

            # Calculate seabed length
            seabed_length, sampled_points = sampler.seabed_length(merged_geom, sampling_interval)

            # Check coverage and warn if incomplete
            valid_count = sum(1 for _point, depth in sampled_points if depth is not None)
            total_samples = len(sampled_points)

            if valid_count == 0:
                feedback.pushWarning(f"Route '{route_id}': No bathymetry coverage. Seabed length falls back to plan (2D) length.")
                seabed_length = plan_length
            elif valid_count < total_samples:
                coverage_ratio = valid_count / total_samples
                feedback.pushWarning(f"Route '{route_id}': Partial bathymetry coverage ({coverage_ratio*100:.1f}% valid). Seabed length calculated only for covered segments.")

            if output_intervals:
                # Output at regular KP intervals, each sampled along the
                # route itself (not a chord between its end points).
                route = RouteFrame.from_source(merged_geom, distance_area)
                kp_interval_m = kp_interval_km * 1000
                current_kp = 0.0
                while current_kp < plan_length:
                    if feedback.isCanceled():
                        break
                    end_kp = min(current_kp + kp_interval_m, plan_length)

                    segment_geom = route.extract_segment(current_kp / 1000.0, end_kp / 1000.0)
                    if segment_geom and not segment_geom.isEmpty():
                        segment_plan_length = distance_area.measureLength(segment_geom)
                        segment_seabed_length, _ = sampler.seabed_length(segment_geom, sampling_interval)
                        segment_elongation = segment_seabed_length / segment_plan_length if segment_plan_length > 0 else 0

                        out_feature = QgsFeature(fields)
                        out_feature.setAttribute('route_id', route_id)
                        out_feature.setAttribute('kp_start', current_kp / 1000)
                        out_feature.setAttribute('kp_end', end_kp / 1000)
                        out_feature.setAttribute('segment_length_m', segment_plan_length)
                        out_feature.setAttribute('seabed_segment_length_m', segment_seabed_length)
                        out_feature.setAttribute('elongation_ratio', segment_elongation)
                        sink.addFeature(out_feature, QgsFeatureSink.FastInsert)

                    current_kp = end_kp
            else:
                elongation_ratio = seabed_length / plan_length if plan_length > 0 else 0

                # Sensitivity analysis
                sensitivity_results = {}
                if do_sensitivity:
                    for interval in sensitivity_intervals:
                        if feedback.isCanceled():
                            break
                        length, _ = sampler.seabed_length(merged_geom, interval)
                        sensitivity_results[str(interval)] = length

                # Create output feature
                out_feature = QgsFeature(fields)
                out_feature.setAttribute('route_id', route_id)
                out_feature.setAttribute('plan_length_m', plan_length)
                out_feature.setAttribute('seabed_length_m', seabed_length)
                out_feature.setAttribute('elongation_ratio', elongation_ratio)
                out_feature.setAttribute('sampling_interval_m', sampling_interval)
                if do_sensitivity:
                    out_feature.setAttribute('sensitivity_results', str(sensitivity_results))

                sink.addFeature(out_feature, QgsFeatureSink.FastInsert)

        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr("""
This tool calculates the seabed (3D) length of RPL routes by sampling bathymetry data along the route. It accounts for seabed topography to provide more accurate cable length estimates compared to simple plan (2D) distances.

**Inputs:**
- **RPL Route Line Layer:** The line layer containing the route(s) to analyse.
- **Bathymetry Type:** Choose between raster (e.g., MBES) or contour line data for depth sampling.
- **Bathymetry Raster/Contour Layer:** The bathymetry data source (raster or vector contours).
- **Depth Field Name:** The field containing depth values in contour layers (dropdown populated from selected contour layer).
- **Sampling Interval:** Distance between depth samples (for raster bathymetry).
- **Optional:** Sensitivity analysis and regular KP interval outputs.

**Outputs:**
- A point layer with seabed length results, including plan length, seabed length, elongation ratio, and coverage statistics. If enabled, outputs at regular KP intervals or sensitivity analysis results.
""")

    def name(self):
        return 'seabedlength'

    def displayName(self):
        return self.tr('Calculate Seabed Length')

    def group(self):
        return self.tr('RPL Tools')

    def groupId(self):
        return 'rpl_tools'

    def tr(self, string):
        return QCoreApplication.translate('SeabedLengthAlgorithm', string)

    def createInstance(self):
        return SeabedLengthAlgorithm()
