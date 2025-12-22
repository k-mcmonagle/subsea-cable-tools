# -*- coding: utf-8 -*-

"""
NearestKP
NearestKP identifies the nearest KP on specified paths for each point feature in a points layer.
 It outputs a new points layer with attributes for the distance to the path and the nearest KP,
 along with a line layer showing connections to the nearest paths. Optionally, it can also
 create a Point on Line layer that places a point directly on the path, carrying additional
 attributes.

 Note:
 Both input layers (Points and Paths) must use the same Coordinate Reference System (CRS).
 This ensures accurate distance and bearing calculations.
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsFeatureSink,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterBoolean,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsDistanceArea,
    QgsField,
    QgsWkbTypes,
    QgsProject,
    QgsFields,
    QgsVectorLayer
)
from PyQt5.QtCore import QVariant
import math


class NearestKPAlgorithm(QgsProcessingAlgorithm):
    """
    NearestKP Algorithm.

    This algorithm identifies the closest Kilometer Point (KP) on specified paths for each point feature in a points layer.
    It calculates the distance along the path from its start to the nearest KP and outputs a new points layer with these attributes,
    along with a line layer showing the connections to the nearest paths. Optionally, it can also create a Point on Line layer
    that places a point directly on the path, carrying the attributes of the input points and additional range and bearing information.

    Note:
    Both input layers (Points and Paths) must use the same Coordinate Reference System (CRS).
    This ensures accurate distance and bearing calculations.
    """

    # Constants used to refer to parameters and outputs.
    INPUT_POINTS = 'INPUT_POINTS'
    INPUT_PATHS = 'INPUT_PATHS'
    OUTPUT_POINTS = 'OUTPUT_POINTS'
    OUTPUT_LINES = 'OUTPUT_LINES'
    ADD_POINT_ON_LINE = 'ADD_POINT_ON_LINE'
    OUTPUT_POINT_ON_LINE = 'OUTPUT_POINT_ON_LINE'

    def initAlgorithm(self, config=None):
        """
        Define the inputs and outputs of the algorithm.
        """
        # Input Points Layer
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_POINTS,
                self.tr('Input Points Layer'),
                [QgsProcessing.TypeVectorPoint]
            )
        )

        # Input Paths Layer
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_PATHS,
                self.tr('Input Paths Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )

        # Output Points Layer
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POINTS,
                self.tr('Output Points Layer')
            )
        )

        # Output Lines Layer
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_LINES,
                self.tr('Output Lines Layer')
            )
        )

        # Checkbox to Add Point on Line Layer
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ADD_POINT_ON_LINE,
                self.tr('Add Point on Line Layer'),
                defaultValue=False
            )
        )

        # Output Point on Line Layer (optional)
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POINT_ON_LINE,
                self.tr('Output Snapped Point to Line Layer'),
                optional=True
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        """
        Execute the algorithm.
        """
        # Retrieve the input layers
        points_source = self.parameterAsSource(parameters, self.INPUT_POINTS, context)
        paths_layer = self.parameterAsVectorLayer(parameters, self.INPUT_PATHS, context)

        if points_source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_POINTS))

        if paths_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_PATHS))

        paths_source = paths_layer

        # Check if both layers have the same CRS
        points_crs = points_source.sourceCrs()
        paths_crs = paths_source.sourceCrs()

        if points_crs != paths_crs:
            raise QgsProcessingException(
                self.tr(
                    'CRS Mismatch: The input Points layer has CRS "{points_crs}", '
                    'while the input Paths layer has CRS "{paths_crs}". Please ensure both layers use the same CRS.'
                ).format(
                    points_crs=points_crs.authid(),
                    paths_crs=paths_crs.authid()
                )
            )

        # Get the name of the input paths layer for kp_ref
        paths_layer_name = paths_layer.name()

        # Retrieve the output sinks
        (points_sink, points_dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT_POINTS, context,
            self._createOutputFields(points_source.fields()),
            QgsWkbTypes.Point,
            points_source.sourceCrs()
        )

        (lines_sink, lines_dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT_LINES, context,
            self._createLineOutputFields(),
            QgsWkbTypes.LineString,
            paths_source.sourceCrs()
        )

        # Initialize the Point on Line sink if the user opted to create it
        add_point_on_line = self.parameterAsBool(parameters, self.ADD_POINT_ON_LINE, context)
        if add_point_on_line:
            (point_on_line_sink, point_on_line_dest_id) = self.parameterAsSink(
                parameters, self.OUTPUT_POINT_ON_LINE, context,
                self._createPointOnLineFields(points_source.fields()),
                QgsWkbTypes.Point,
                paths_source.sourceCrs()
            )
            if point_on_line_sink is None:
                raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_POINT_ON_LINE))
        else:
            point_on_line_sink = None

        if points_sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_POINTS))

        if lines_sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_LINES))

        # Initialize QgsDistanceArea for accurate distance measurements
        # Keep behavior consistent with other plugin tools (fallback to WGS84 if project ellipsoid is unset).
        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(points_source.sourceCrs(), context.transformContext())
        ellipsoid = QgsProject.instance().ellipsoid() or 'WGS84'
        distance_calculator.setEllipsoid(ellipsoid)

        # Cache path features/geometries once (avoids re-iterating provider for every point)
        # and compute cumulative offsets so KP is relative to the whole input RPL layer
        # (matching KP Mouse Map Tool behaviour for multi-feature RPLs).
        path_features = list(paths_source.getFeatures())
        path_geoms = []
        path_ids = []
        path_offsets_m = []
        cumulative_m = 0.0
        for path_feature in path_features:
            geom = path_feature.geometry()
            if geom is None or geom.isEmpty():
                continue
            path_geoms.append(geom)
            path_ids.append(path_feature.id())
            path_offsets_m.append(cumulative_m)
            cumulative_m += float(distance_calculator.measureLength(geom))

        total_features = points_source.featureCount()
        processed_features = 0

        # Iterate through each point feature
        for point_feature in points_source.getFeatures():
            if feedback.isCanceled():
                break

            point_geom = point_feature.geometry()
            if point_geom.isEmpty():
                continue  # Skip empty geometries

            point_xy = point_geom.asPoint()
            
            nearest_dist = float('inf')
            nearest_dist_signed = None
            nearest_pt_geom = None
            nearest_path_id = None
            nearest_kp = None
            nearest_path_geom = None

            # Iterate through cached paths to find the nearest point
            for idx, path_geom in enumerate(path_geoms):
                # Improved segment-by-segment approach to find the truly nearest point
                result = self.find_nearest_point_on_path(
                    path_geom,
                    QgsPointXY(point_xy),
                    distance_calculator,
                    base_distance_m=path_offsets_m[idx],
                )

                if result['distance'] < nearest_dist:
                    nearest_dist = result['distance']
                    nearest_dist_signed = result.get('distance_signed', result['distance'])
                    nearest_pt_geom = result['point_geometry']
                    nearest_path_id = path_ids[idx]
                    nearest_kp = result['kp']
                    nearest_path_geom = path_geom

            # If a nearest point is found, create new features in both output layers
            if nearest_pt_geom:
                # === Create Output Point Feature ===
                new_point_feature = QgsFeature()
                new_point_feature.setGeometry(point_geom)

                # Prepare attributes: copy all original attributes
                attrs = point_feature.attributes()

                # Append new attributes: path_id, distance, kp, kp_ref
                attrs.append(nearest_path_id)
                attrs.append(round(float(nearest_dist_signed if nearest_dist_signed is not None else nearest_dist), 3))  # Signed DCC (m)
                attrs.append(round(nearest_kp, 3))    # Rounded to 3 decimal places
                attrs.append(paths_layer_name)        # kp_ref

                new_point_feature.setAttributes(attrs)
                points_sink.addFeature(new_point_feature, QgsFeatureSink.FastInsert)

                # === Create Output Line Feature ===
                line_geom = QgsGeometry.fromPolylineXY([
                    QgsPointXY(point_xy),
                    nearest_pt_geom.asPoint()
                ])
                new_line_feature = QgsFeature()
                new_line_feature.setGeometry(line_geom)

                # Set attributes for the line: point_id, path_id, distance, kp, kp_ref
                line_attrs = [
                    point_feature.id(),
                    nearest_path_id,
                    round(float(nearest_dist_signed if nearest_dist_signed is not None else nearest_dist), 3),
                    round(nearest_kp, 3),
                    paths_layer_name
                ]
                new_line_feature.setAttributes(line_attrs)
                lines_sink.addFeature(new_line_feature, QgsFeatureSink.FastInsert)

                # === Create Point on Line Feature (if requested) ===
                if add_point_on_line and point_on_line_sink:
                    new_polin_feature = QgsFeature()
                    new_polin_feature.setGeometry(nearest_pt_geom)

                    # Prepare attributes: copy all original attributes
                    polin_attrs = point_feature.attributes()

                    # Calculate range and bearing
                    original_point = point_xy
                    point_on_line = nearest_pt_geom.asPoint()

                    # Calculate range (distance back to original point in meters)
                    # Range is always positive (absolute distance)
                    range_to_target = float(nearest_dist)

                    # Calculate bearing (absolute bearing clockwise from north as 0 degrees)
                    bearing_to_target = self.calculate_bearing(
                        QgsPointXY(point_on_line),
                        QgsPointXY(original_point)
                    )
                    bearing_to_target = round(bearing_to_target, 3)  # Rounded to 3 decimal places

                    # Append kp_ref, range, bearing, and kp_km
                    polin_attrs.append(paths_layer_name)               # kp_ref
                    polin_attrs.append(range_to_target)                # range_to_target_m
                    polin_attrs.append(bearing_to_target)              # bearing_to_target_deg
                    polin_attrs.append(round(nearest_kp, 3))           # kp_km

                    new_polin_feature.setAttributes(polin_attrs)
                    point_on_line_sink.addFeature(new_polin_feature, QgsFeatureSink.FastInsert)

            # Update progress
            processed_features += 1
            if total_features > 0:
                feedback.setProgress(int((processed_features / total_features) * 100))

        # Prepare the return dictionary
        results = {
            self.OUTPUT_POINTS: points_dest_id,
            self.OUTPUT_LINES: lines_dest_id
        }

        if add_point_on_line:
            results[self.OUTPUT_POINT_ON_LINE] = point_on_line_dest_id

        return results

    def shortHelpString(self):
        return self.tr("""<p>This tool identifies the nearest Kilometer Point (KP) on a line layer for each point in a point layer. It produces a new point layer with KP and distance attributes, a line layer connecting points to their nearest location on the line, and an optional snapped point layer.</p>

<p><b>Important:</b> Both input layers must have the same Coordinate Reference System (CRS) to ensure accurate distance calculations. The tool will show an error and stop if the CRSs do not match.</p>

<p><b>Instructions:</b></p>

<p><b>1. Select Input Layers:</b><ul>
<li><b>Input Points Layer:</b> Choose the point layer for which you want to find the nearest KP.</li>
<li><b>Input Paths Layer:</b> Select the line layer representing the network or route.</li>
<li>Ensure both layers share the same Coordinate Reference System (CRS) for accurate calculations.</li></ul></p>

<p><b>2. Configure Outputs:</b><ul>
<li><b>Output Points Layer:</b> A new point layer will be created with all original attributes plus fields for <i>path_id</i>, <i>distance_to_path_m</i>, <i>kp_km</i>, and <i>kp_ref</i>.</li>
<li><b>Output Lines Layer:</b> This layer will contain lines connecting each input point to its calculated nearest point on the path.</li>
<li><b>Add Point on Line Layer (Optional):</b> Check this box to generate a third layer containing points snapped directly onto the line. This layer includes all original attributes plus fields for <i>kp_ref</i>, <i>range_to_target_m</i>, <i>bearing_to_target_deg</i>, and <i>kp_km</i>.</li></ul></p>

<p><b>3. Run:</b> Execute the tool.</p>

<p><b>Note:</b> The tool performs a segment-by-segment analysis to ensure it finds the true nearest point, even on complex, multi-part line geometries.</p>
""")

    def find_nearest_point_on_path(self, path_geom, point_xy, distance_calculator, base_distance_m: float = 0.0):
        """
        Find the nearest point on a path to the given point using a segment-by-segment approach.
        
        Parameters:
            path_geom (QgsGeometry): The path geometry
            point_xy (QgsPointXY): The point to find the nearest point to
            distance_calculator (QgsDistanceArea): Distance calculator for accurate measurements
            
        Returns:
            dict: A dictionary containing the nearest point geometry, the distance, and the KP value
        """
        min_distance = float('inf')
        nearest_point = None
        best_kp_m = float(base_distance_m) if base_distance_m is not None else 0.0
        best_signed_distance = None
        
        # Get all the points that make up the path (handling multi-part geometries)
        all_points = []
        if path_geom.isMultipart():
            lines = path_geom.asMultiPolyline()
            for line in lines:
                all_points.append(line)
        else:
            all_points = [path_geom.asPolyline()]
        
        # Walk segments once, tracking cumulative distance along the geometry.
        # This correctly handles multipart polylines and avoids the "part index" bug.
        cumulative_distance_m = 0.0
        for line in all_points:
            if len(line) < 2:
                continue
            for i in range(len(line) - 1):
                segment_start = line[i]
                segment_end = line[i + 1]

                segment_length = float(distance_calculator.measureLine(segment_start, segment_end))
                if segment_length <= 0:
                    continue

                segment_geom = QgsGeometry.fromPolylineXY([segment_start, segment_end])
                nearest_on_segment = segment_geom.nearestPoint(QgsGeometry.fromPointXY(point_xy))
                if nearest_on_segment.isEmpty():
                    cumulative_distance_m += segment_length
                    continue

                nearest_on_segment_xy = nearest_on_segment.asPoint()
                distance = float(distance_calculator.measureLine(point_xy, nearest_on_segment_xy))

                if distance < min_distance:
                    min_distance = distance
                    nearest_point = nearest_on_segment
                    distance_along_segment = float(
                        distance_calculator.measureLine(segment_start, nearest_on_segment_xy)
                    )
                    if distance_along_segment < 0:
                        distance_along_segment = 0.0
                    if distance_along_segment > segment_length:
                        distance_along_segment = segment_length

                    best_kp_m = float(base_distance_m) + cumulative_distance_m + distance_along_segment

                    # Signed distance (port/stbd) relative to the direction of ascending KP
                    # Segment direction = segment_start -> segment_end.
                    # Using 2D cross product: cross(v, w) > 0 means point is left of the segment.
                    v_x = float(segment_end.x() - segment_start.x())
                    v_y = float(segment_end.y() - segment_start.y())
                    w_x = float(point_xy.x() - nearest_on_segment_xy.x())
                    w_y = float(point_xy.y() - nearest_on_segment_xy.y())
                    cross = (v_x * w_y) - (v_y * w_x)
                    if abs(cross) < 1e-12:
                        best_signed_distance = 0.0
                    elif cross > 0:
                        # Left/port side => negative
                        best_signed_distance = -abs(min_distance)
                    else:
                        # Right/stbd side => positive
                        best_signed_distance = abs(min_distance)

                cumulative_distance_m += segment_length

        # Convert to kilometers
        kp = best_kp_m / 1000.0
        
        return {
            'point_geometry': nearest_point,
            'distance': min_distance,
            'distance_signed': best_signed_distance if best_signed_distance is not None else min_distance,
            'kp': kp
        }

    def calculate_kp(self, line_geom, nearest_pt_geom, distance_calculator):
        """
        Calculate the linear reference distance along the line geometry from the start to the nearest point.

        Parameters:
            line_geom (QgsGeometry): The geometry of the line.
            nearest_pt_geom (QgsGeometry): The geometry of the nearest point on the line.
            distance_calculator (QgsDistanceArea): Initialized QgsDistanceArea object.

        Returns:
            float: Distance in kilometers from the start of the line to the nearest point.
        """
        if line_geom.isEmpty() or nearest_pt_geom.isEmpty():
            return 0.0

        # Extract the QgsPoint from the nearest point geometry
        nearest_pt = nearest_pt_geom.asPoint()
        nearest_pt_xy = QgsPointXY(nearest_pt.x(), nearest_pt.y())

        # Initialize cumulative distance
        cumulative_distance = 0.0

        # Handle multipart geometries
        if line_geom.isMultipart():
            lines = line_geom.asMultiPolyline()
        else:
            lines = [line_geom.asPolyline()]

        for line_part in lines:
            for i in range(len(line_part) - 1):
                p1 = line_part[i]
                p2 = line_part[i + 1]
                segment_start = QgsPointXY(p1.x(), p1.y())
                segment_end = QgsPointXY(p2.x(), p2.y())

                # Create a QgsGeometry for the current segment
                segment_geom = QgsGeometry.fromPolylineXY([segment_start, segment_end])

                # Calculate the distance from the segment to the nearest point
                distance_to_nearest = segment_geom.distance(QgsGeometry.fromPointXY(nearest_pt_xy))

                if distance_to_nearest < 1e-6:
                    # Nearest point lies on this segment
                    # Calculate partial distance along the segment to the nearest point
                    partial_length = distance_calculator.measureLine(segment_start, nearest_pt_xy)
                    cumulative_distance += partial_length
                    return cumulative_distance / 1000.0  # Convert meters to kilometers
                else:
                    # Add the full length of the segment to the cumulative distance
                    segment_length = distance_calculator.measureLine(segment_start, segment_end)
                    cumulative_distance += segment_length

        # If the nearest point was not found on any segment, return the total length
        return cumulative_distance / 1000.0  # Convert meters to kilometers

    def calculate_bearing(self, pointA, pointB):
        """
        Calculate the absolute bearing from pointA to pointB, measured clockwise from north as 0 degrees.

        Parameters:
            pointA (QgsPointXY): The starting point.
            pointB (QgsPointXY): The ending point.

        Returns:
            float: Bearing in degrees from North, clockwise.
        """
        dx = pointB.x() - pointA.x()
        dy = pointB.y() - pointA.y()

        angle_rad = math.atan2(dx, dy)  # Note: dx first to get clockwise from north
        angle_deg = math.degrees(angle_rad)
        compass_bearing = (angle_deg + 360) % 360  # Normalize to [0, 360)

        return compass_bearing

    def _createOutputFields(self, input_fields):
        """
        Create the fields for the output points layer by appending new fields.

        Parameters:
            input_fields (QgsFields): The fields from the input points layer.

        Returns:
            QgsFields: The new fields for the output points layer.
        """
        fields = QgsFields(input_fields)  # Correctly duplicate QgsFields
        fields.append(QgsField('path_id', QVariant.Int))
        fields.append(QgsField('distance_to_path_m', QVariant.Double))
        fields.append(QgsField('kp_km', QVariant.Double))
        fields.append(QgsField('kp_ref', QVariant.String))  # Added kp_ref
        return fields

    def _createLineOutputFields(self):
        """
        Define the fields for the output lines layer.

        Returns:
            QgsFields: The fields for the output lines layer.
        """
        fields = QgsFields()
        fields.append(QgsField('point_id', QVariant.Int))
        fields.append(QgsField('path_id', QVariant.Int))
        fields.append(QgsField('distance_to_path_m', QVariant.Double))
        fields.append(QgsField('kp_km', QVariant.Double))
        fields.append(QgsField('kp_ref', QVariant.String))  # Added kp_ref
        return fields

    def _createPointOnLineFields(self, input_fields):
        """
        Create the fields for the Point on Line layer by appending kp_ref, range, bearing, and kp_km.

        Parameters:
            input_fields (QgsFields): The fields from the input points layer.

        Returns:
            QgsFields: The new fields for the Point on Line layer.
        """
        fields = QgsFields(input_fields)  # Duplicate input fields
        fields.append(QgsField('kp_ref', QVariant.String))               # Add kp_ref
        fields.append(QgsField('range_to_target_m', QVariant.Double))     # Add range_to_target
        fields.append(QgsField('bearing_to_target_deg', QVariant.Double)) # Add bearing_to_target
        fields.append(QgsField('kp_km', QVariant.Double))                 # Add kp_km
        return fields

    def name(self):
        """
        Returns the algorithm name, used for identifying the algorithm.
        This string should be fixed for the algorithm, and must not be localised.
        The name should be unique within each provider.
        """
        return 'nearest_kp'

    def displayName(self):
        """
        Returns the translated algorithm name, which should be used for any
        user-visible display of the algorithm name.
        """
        return self.tr('Nearest KP')

    def group(self):
        """
        Returns the name of the group this algorithm belongs to. This string
        should be localised.
        """
        return self.tr('KP Points')

    def groupId(self):
        """
        Returns the unique ID of the group this algorithm belongs to. This
        string should be fixed for the algorithm, and must not be localised.
        The group id should be unique within each provider. Group id should
        contain lowercase alphanumeric characters only and no spaces or other
        formatting characters.
        """
        return 'kppoints'

    def tr(self, string):
        """
        Get the translation for a string using Qt translation API.

        We implement this ourselves since the plugin is not being loaded by the plugin manager.
        """
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        """
        Creates and returns a new instance of the algorithm.
        """
        return NearestKPAlgorithm()
