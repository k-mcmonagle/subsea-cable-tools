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

import os
import sys
plugin_dir = os.path.dirname(__file__)
lib_dir = os.path.join(plugin_dir, 'lib')
if lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
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
    QgsLineString,
    QgsPoint,
    QgsPointXY,
    QgsWkbTypes,
    QgsDistanceArea,
    QgsCoordinateTransform,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

import math


class SeabedLengthAlgorithm(QgsProcessingAlgorithm):
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
                type=QgsProcessingParameterNumber.Integer,
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
                type=QgsProcessingParameterNumber.Integer,
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
            except:
                sensitivity_intervals = [1, 5, 10, 25, 50, 100]

        # Prepare output fields (always include all possible fields)
        fields = QgsFields()
        fields.append(QgsField('route_id', QVariant.String))
        fields.append(QgsField('plan_length_m', QVariant.Double))
        fields.append(QgsField('seabed_length_m', QVariant.Double))
        fields.append(QgsField('elongation_ratio', QVariant.Double))
        fields.append(QgsField('sampling_interval_m', QVariant.Int))
        if do_sensitivity:
            fields.append(QgsField('sensitivity_results', QVariant.String))
        if output_intervals:
            fields.append(QgsField('kp_start', QVariant.Double))
            fields.append(QgsField('kp_end', QVariant.Double))
            fields.append(QgsField('segment_length_m', QVariant.Double))
            fields.append(QgsField('seabed_segment_length_m', QVariant.Double))
            # elongation_ratio already added

        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT, context, fields, QgsWkbTypes.NoGeometry, line_layer.crs())

        # Group features by route_id
        routes = {}
        for feature in line_layer.getFeatures():
            route_id = feature['route_id'] if 'route_id' in feature.fields().names() else 'default_route'
            if route_id not in routes:
                routes[route_id] = []
            routes[route_id].append(feature)

        # Prepare output fields
        fields = QgsFields()
        fields.append(QgsField('route_id', QVariant.String))
        fields.append(QgsField('plan_length_m', QVariant.Double))
        fields.append(QgsField('seabed_length_m', QVariant.Double))
        fields.append(QgsField('elongation_ratio', QVariant.Double))
        fields.append(QgsField('sampling_interval_m', QVariant.Int))
        if do_sensitivity:
            fields.append(QgsField('sensitivity_results', QVariant.String))

        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT, context, fields, QgsWkbTypes.NoGeometry, line_layer.crs())

        total_routes = len(routes)
        for route_idx, (route_id, features) in enumerate(routes.items()):
            if feedback.isCanceled():
                break

            feedback.setProgress((route_idx / total_routes) * 100)

            # Merge geometries for the route
            merged_geom = None
            for feature in features:
                geom = feature.geometry()
                if geom and not geom.isEmpty():
                    if merged_geom is None:
                        merged_geom = geom
                    else:
                        merged_geom = merged_geom.combine(geom)

            if not merged_geom or merged_geom.isEmpty():
                continue

            # Calculate plan length
            distance_area = QgsDistanceArea()
            distance_area.setSourceCrs(line_layer.sourceCrs(), context.transformContext())
            distance_area.setEllipsoid(context.project().ellipsoid())
            plan_length = distance_area.measureLength(merged_geom)

            # Calculate seabed length
            seabed_length, sampled_points = self._calculate_seabed_length(
                merged_geom, raster_layer, contour_layer, bathy_type, sampling_interval, line_layer.crs(), context, depth_field
            )

            # Check coverage and warn if incomplete
            valid_depths = [p for p in sampled_points if (p[1] if len(p) == 2 else p[1]) is not None]
            total_samples = len(sampled_points)
            valid_count = len(valid_depths)
            
            if valid_count == 0:
                feedback.pushWarning(f"Route '{route_id}': No bathymetry coverage. Seabed length equals plan length.")
            elif valid_count < total_samples:
                coverage_ratio = valid_count / total_samples
                feedback.pushWarning(f"Route '{route_id}': Partial bathymetry coverage ({coverage_ratio*100:.1f}% valid). Seabed length calculated only for covered segments.")

            if output_intervals:
                # Output at regular KP intervals
                kp_interval_m = kp_interval_km * 1000
                current_kp = 0.0
                while current_kp < plan_length:
                    end_kp = min(current_kp + kp_interval_m, plan_length)
                    
                    # Extract segment geometry using straight line approximation
                    segment_geom = self._extract_segment(merged_geom, current_kp, end_kp, distance_area)
                    if segment_geom and not segment_geom.isEmpty():
                        segment_plan_length = distance_area.measureLength(segment_geom)
                        segment_seabed_length, _ = self._calculate_seabed_length(
                            segment_geom, raster_layer, contour_layer, bathy_type, sampling_interval, line_layer.crs(), context, depth_field
                        )
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
                        length, _ = self._calculate_seabed_length(
                            merged_geom, raster_layer, contour_layer, bathy_type, interval, line_layer.crs(), context, depth_field
                        )
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

    def _calculate_seabed_length(self, geom, raster_layer, contour_layer, bathy_type, interval_m, line_crs, context, depth_field):
        """Calculate seabed length by sampling depths along the geometry."""
        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(line_crs, context.transformContext())
        distance_area.setEllipsoid(context.project().ellipsoid())
        total_length = distance_area.measureLength(geom)
        sampled_points = []

        if bathy_type == 0:  # Raster
            # Get line vertices
            line = geom.constGet()
            if isinstance(line, QgsLineString):
                points = [QgsPointXY(pt.x(), pt.y()) for pt in line.points()]
            else:
                # Handle multi-part geometries
                points = []
                for part in line:
                    points.extend([QgsPointXY(pt.x(), pt.y()) for pt in part.points()])

            if len(points) < 2:
                return 0.0, []

            # Sample points along the line
            sampled_points = []
            dist = 0.0
            while dist <= total_length:
                # Interpolate point at distance
                point = self._interpolate_point_along_line(points, dist, distance_area)
                if point:
                    # Sample depth
                    depth = self._sample_depth(point, raster_layer, contour_layer, bathy_type, line_crs, depth_field)
                    sampled_points.append((point, depth))
                dist += interval_m

            # Ensure last point
            if sampled_points and sampled_points[-1][0] != points[-1]:
                depth = self._sample_depth(points[-1], raster_layer, contour_layer, bathy_type, line_crs, depth_field)
                sampled_points.append((points[-1], depth))

        elif bathy_type == 1:  # Contour
            # Find intersection points with contours
            sampled_points = []
            points_with_distance = []
            start_point = self._get_first_point(geom)
            end_point = self._get_last_point(geom)
            
            # Add start point
            start_depth = self._sample_depth(start_point, raster_layer, contour_layer, bathy_type, line_crs, depth_field)
            points_with_distance.append((start_point, start_depth, 0.0))
            
            # Add intersection points
            for contour_feat in contour_layer.getFeatures():
                contour_geom = contour_feat.geometry()
                if contour_geom:
                    intersections = geom.intersection(contour_geom)
                    if intersections and not intersections.isEmpty():
                        if depth_field in contour_feat.fields().names():
                            depth = contour_feat[depth_field]
                        else:
                            # Fallback to first field if specified field doesn't exist
                            depth = contour_feat[contour_feat.fields().names()[0]]
                        for part in intersections.parts():
                            if part.wkbType() == QgsWkbTypes.Point:
                                pt = QgsPointXY(part.x(), part.y())
                                dist_along = geom.lineLocatePoint(QgsGeometry.fromPointXY(pt))
                                points_with_distance.append((pt, depth, dist_along))
            
            # Add end point
            end_depth = self._sample_depth(end_point, raster_layer, contour_layer, bathy_type, line_crs, depth_field)
            points_with_distance.append((end_point, end_depth, total_length))
            
            # Sort by distance and extract just (point, depth)
            points_with_distance.sort(key=lambda x: x[2])
            sampled_points = [(p, z) for p, z, d in points_with_distance]

        # Calculate 3D length using only valid consecutive samples
        seabed_length = 0.0
        valid_pts = [(p, z) for p, z in sampled_points if z is not None]
        for i in range(1, len(valid_pts)):
            p0, z0 = valid_pts[i-1]
            p1, z1 = valid_pts[i]
            plan_dist = distance_area.measureLine(p0, p1)
            dz = z1 - z0
            seabed_length += math.sqrt(plan_dist**2 + dz**2)

        return seabed_length, sampled_points

    def _extract_segment(self, geom, start_dist, end_dist, distance_area):
        """Extract a segment of the geometry between start_dist and end_dist using straight line approximation."""
        if start_dist >= end_dist:
            return None
        
        # Get points at start and end distances
        start_point = self._interpolate_point_along_line_geom(geom, start_dist, distance_area)
        end_point = self._interpolate_point_along_line_geom(geom, end_dist, distance_area)
        
        if not start_point or not end_point:
            return None
        
        # Create a line segment
        return QgsGeometry.fromPolylineXY([start_point, end_point])

    def _interpolate_point_along_line_geom(self, geom, distance, distance_area):
        """Interpolate a point along the geometry at given distance."""
        total_length = distance_area.measureLength(geom)
        if distance > total_length:
            return None
        
        # For simplicity, assume single linestring
        line = geom.constGet()
        if isinstance(line, QgsLineString):
            points = [QgsPointXY(pt.x(), pt.y()) for pt in line.points()]
        else:
            # Handle multi-part geometries
            points = []
            for part in line:
                points.extend([QgsPointXY(pt.x(), pt.y()) for pt in part.points()])
        
        cumulative_dist = 0.0
        for i in range(len(points) - 1):
            p0 = points[i]
            p1 = points[i+1]
            seg_dist = distance_area.measureLine(p0, p1)
            
            if cumulative_dist + seg_dist >= distance:
                remaining = distance - cumulative_dist
                ratio = remaining / seg_dist if seg_dist > 0 else 0
                x = p0.x() + ratio * (p1.x() - p0.x())
                y = p0.y() + ratio * (p1.y() - p0.y())
                return QgsPointXY(x, y)
            
            cumulative_dist += seg_dist
        
        return points[-1] if points else None

    def _get_first_point(self, geom):
        """Get the first point of the geometry."""
        line = geom.constGet()
        if isinstance(line, QgsLineString):
            return QgsPointXY(line.pointN(0))
        else:
            # Multi-part, get first part's first point
            for part in line:
                if isinstance(part, QgsLineString) and part.numPoints() > 0:
                    return QgsPointXY(part.pointN(0))
        return None

    def _get_last_point(self, geom):
        """Get the last point of the geometry."""
        line = geom.constGet()
        if isinstance(line, QgsLineString):
            return QgsPointXY(line.pointN(line.numPoints() - 1))
        else:
            # Multi-part, get last part's last point
            parts = list(line)
            if parts:
                last_part = parts[-1]
                if isinstance(last_part, QgsLineString) and last_part.numPoints() > 0:
                    return QgsPointXY(last_part.pointN(last_part.numPoints() - 1))
        return None

    def _interpolate_point_along_line(self, points, distance, distance_area):
        """Interpolate a point along the line at given distance."""
        if not points:
            return None

        cumulative_dist = 0.0
        for i in range(len(points) - 1):
            p0 = points[i]
            p1 = points[i+1]
            seg_dist = distance_area.measureLine(p0, p1)

            if cumulative_dist + seg_dist >= distance:
                # Interpolate within this segment
                remaining = distance - cumulative_dist
                ratio = remaining / seg_dist if seg_dist > 0 else 0
                x = p0.x() + ratio * (p1.x() - p0.x())
                y = p0.y() + ratio * (p1.y() - p0.y())
                return QgsPointXY(x, y)

            cumulative_dist += seg_dist

        return points[-1]  # Return last point if distance exceeds total length

    def _sample_depth(self, point, raster_layer, contour_layer, bathy_type, line_crs, depth_field):
        """Sample depth at a point from raster or contours."""
        if bathy_type == 0 and raster_layer:  # Raster
            # Transform to raster CRS if needed
            sample_point = point
            if raster_layer.crs() != line_crs:
                transform = QgsCoordinateTransform(line_crs, raster_layer.crs(), QgsProject.instance())
                try:
                    sample_point = transform.transform(point)
                except:
                    return None

            # Sample raster
            provider = raster_layer.dataProvider()
            sample, ok = provider.sample(sample_point, 1)
            return float(sample) if ok else None

        elif bathy_type == 1 and contour_layer:  # Contours
            # For contours, find nearest contour and interpolate depth
            # This is a simplified implementation - could be improved
            min_dist = float('inf')
            nearest_depth = None

            for feature in contour_layer.getFeatures():
                geom = feature.geometry()
                if geom:
                    dist = geom.distance(QgsGeometry.fromPointXY(point))
                    if dist < min_dist:
                        min_dist = dist
                        # Use the specified depth field
                        if depth_field in feature.fields().names():
                            nearest_depth = feature[depth_field]
                        else:
                            # Fallback to first field if specified field doesn't exist
                            nearest_depth = feature[feature.fields().names()[0]]

            return nearest_depth

        return None

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
        return self.tr('Other Tools')

    def groupId(self):
        return 'other_tools'

    def tr(self, string):
        return QCoreApplication.translate('SeabedLengthAlgorithm', string)

    def createInstance(self):
        return SeabedLengthAlgorithm()