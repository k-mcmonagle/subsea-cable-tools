# -*- coding: utf-8 -*-

"""
NearestKP
NearestKP identifies the nearest KP on specified paths for each point feature in a points layer.
 It outputs a new points layer with attributes for the distance to the path and the nearest KP,
 along with a line layer showing connections to the nearest paths. Optionally, it can also
 create a Point on Line layer that places a point directly on the path, carrying additional
 attributes.

 Note:
 Points in a different CRS are reprojected to the Paths CRS for measurement.
"""

from qgis.PyQt.QtCore import QCoreApplication
from .algorithm_base import SubseaCableAlgorithm
from ..kp_range_utils import (
    make_distance_area,
    add_distance_mode_parameter,
    read_distance_mode,
)
from qgis.core import (
    QgsProcessing,
    QgsFeatureSink,
    QgsProcessingException,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterBoolean,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsWkbTypes,
    QgsFields,
)
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_STRING

import math
from ..kp_geo_utils import RouteFrame, get_features_skip_invalid, ordered_route_features
from .rpl_comparison_utils import signed_dcc


class NearestKPAlgorithm(SubseaCableAlgorithm):
    """
    NearestKP Algorithm.

    This algorithm identifies the closest Kilometer Point (KP) on specified paths for each point feature in a points layer.
    It calculates the distance along the path from its start to the nearest KP and outputs a new points layer with these attributes,
    along with a line layer showing the connections to the nearest paths. Optionally, it can also create a Point on Line layer
    that places a point directly on the path, carrying the attributes of the input points and additional range and bearing information.

    Note:
    Points in a different CRS are reprojected to the Paths CRS for measurement.
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
                self.tr('Reference Line Layer'),
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

        add_distance_mode_parameter(self)

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

        # If the input layers use different CRSes, reproject the input points
        # to the paths CRS before measuring (instead of rejecting outright).
        # Output point features remain in the input points CRS.
        points_crs = points_source.sourceCrs()
        paths_crs = paths_source.sourceCrs()
        points_to_paths_xform = None
        if points_crs != paths_crs:
            feedback.pushInfo(
                self.tr(
                    'CRS mismatch: input Points layer is "{points_crs}" and Paths layer is "{paths_crs}". '
                    'Input points will be reprojected to the Paths CRS for measurement.'
                ).format(
                    points_crs=points_crs.authid() or points_crs.description(),
                    paths_crs=paths_crs.authid() or paths_crs.description(),
                )
            )
            points_to_paths_xform = QgsCoordinateTransform(points_crs, paths_crs, context.transformContext())

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

        # Initialize QgsDistanceArea for accurate distance measurements (paths CRS).
        # Helper handles the WGS84 fallback when the project ellipsoid is unset.
        distance_mode = read_distance_mode(self, parameters, context)
        try:
            distance_calculator = make_distance_area(
                paths_source.sourceCrs(), context.transformContext(),
                mode=distance_mode, project=context.project(),
            )
        except ValueError as exc:
            raise QgsProcessingException(str(exc))

        # One indexed route over the path features in chainage order (SeqNo,
        # else layer order), so KP is continuous across a multi-feature RPL
        # and uses the plugin's single KP definition (kp_geo_utils.RouteFrame).
        path_features = ordered_route_features(get_features_skip_invalid(paths_source))
        path_ids = [f.id() for f in path_features]
        route = RouteFrame.from_source(
            [QgsGeometry(f.geometry()) for f in path_features], distance_calculator)
        path_geoms = route.geometries

        total_features = points_source.featureCount()
        processed_features = 0

        # Iterate through each point feature
        for point_feature in get_features_skip_invalid(points_source):
            if feedback.isCanceled():
                break

            point_geom = point_feature.geometry()
            if point_geom.isEmpty():
                continue  # Skip empty geometries

            # Geometry used for measurement (in paths CRS); the original
            # point_geom is preserved for the output point feature.
            measure_point_geom = QgsGeometry(point_geom)
            if points_to_paths_xform is not None:
                try:
                    measure_point_geom.transform(points_to_paths_xform)
                except QgsCsException:
                    feedback.pushWarning(
                        self.tr('Failed to reproject point id={fid} to Paths CRS; skipping.').format(
                            fid=point_feature.id()
                        )
                    )
                    continue

            point_xy = QgsPointXY(measure_point_geom.asPoint())
            hit = route.kp_at_point(point_xy)

            # If a nearest point is found, create new features in both output layers
            if hit.snapped_xy is not None:
                nearest_path_id = path_ids[hit.feature_index]
                nearest_kp = hit.kp_km
                nearest_dist = hit.dcc_m
                nearest_dist_signed = signed_dcc(
                    path_geoms[hit.feature_index], point_xy, hit.snapped_xy, hit.dcc_m)
                snapped_xy = QgsPointXY(hit.snapped_xy)

                # === Create Output Point Feature ===
                new_point_feature = QgsFeature()
                new_point_feature.setGeometry(point_geom)

                # Prepare attributes: copy all original attributes
                attrs = point_feature.attributes()

                # Append new attributes: path_id, distance, kp, kp_ref
                attrs.append(nearest_path_id)
                attrs.append(round(float(nearest_dist_signed), 3))  # Signed DCC (m)
                attrs.append(round(nearest_kp, 3))    # Rounded to 3 decimal places
                attrs.append(paths_layer_name)        # kp_ref

                new_point_feature.setAttributes(attrs)
                points_sink.addFeature(new_point_feature, QgsFeatureSink.FastInsert)

                # === Create Output Line Feature ===
                line_geom = QgsGeometry.fromPolylineXY([point_xy, snapped_xy])
                new_line_feature = QgsFeature()
                new_line_feature.setGeometry(line_geom)

                # Set attributes for the line: point_id, path_id, distance, kp, kp_ref
                line_attrs = [
                    point_feature.id(),
                    nearest_path_id,
                    round(float(nearest_dist_signed), 3),
                    round(nearest_kp, 3),
                    paths_layer_name
                ]
                new_line_feature.setAttributes(line_attrs)
                lines_sink.addFeature(new_line_feature, QgsFeatureSink.FastInsert)

                # === Create Point on Line Feature (if requested) ===
                if add_point_on_line and point_on_line_sink:
                    new_polin_feature = QgsFeature()
                    new_polin_feature.setGeometry(QgsGeometry.fromPointXY(snapped_xy))

                    # Prepare attributes: copy all original attributes
                    polin_attrs = point_feature.attributes()

                    # Range is always positive (absolute distance back to the point, m)
                    range_to_target = float(nearest_dist)

                    # Calculate bearing (absolute bearing clockwise from north as 0 degrees)
                    bearing_to_target = self.calculate_bearing(snapped_xy, point_xy)
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

<p><b>CRS:</b> points in a different CRS from the line layer are reprojected to the line layer's CRS for measurement; the output points keep their own CRS.</p>

<p><b>KP and DCC:</b> KP is continuous across a multi-feature line layer (features in SeqNo order when the layer has one, otherwise layer order), measured with the plugin's shared KP definition. <i>distance_to_path_m</i> is signed: positive to starboard (right of increasing KP), negative to port.</p>

<p><b>Instructions:</b></p>

<p><b>1. Select Input Layers:</b><ul>
<li><b>Input Points Layer:</b> Choose the point layer for which you want to find the nearest KP.</li>
<li><b>Input Paths Layer:</b> Select the line layer representing the network or route.</li>
</ul></p>

<p><b>2. Configure Outputs:</b><ul>
<li><b>Output Points Layer:</b> A new point layer will be created with all original attributes plus fields for <i>path_id</i>, <i>distance_to_path_m</i>, <i>kp_km</i>, and <i>kp_ref</i>.</li>
<li><b>Output Lines Layer:</b> This layer will contain lines connecting each input point to its calculated nearest point on the path.</li>
<li><b>Add Point on Line Layer (Optional):</b> Check this box to generate a third layer containing points snapped directly onto the line. This layer includes all original attributes plus fields for <i>kp_ref</i>, <i>range_to_target_m</i>, <i>bearing_to_target_deg</i>, and <i>kp_km</i>.</li></ul></p>

<p><b>3. Run:</b> Execute the tool.</p>

<p><b>Note:</b> The nearest point is found segment by segment (spatially indexed), so it is the true nearest point even on complex, multi-part line geometries.</p>
""")

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
        fields.append(QgsField('path_id', FIELD_TYPE_INT))
        fields.append(QgsField('distance_to_path_m', FIELD_TYPE_DOUBLE))
        fields.append(QgsField('kp_km', FIELD_TYPE_DOUBLE))
        fields.append(QgsField('kp_ref', FIELD_TYPE_STRING))  # Added kp_ref
        return fields

    def _createLineOutputFields(self):
        """
        Define the fields for the output lines layer.

        Returns:
            QgsFields: The fields for the output lines layer.
        """
        fields = QgsFields()
        fields.append(QgsField('point_id', FIELD_TYPE_INT))
        fields.append(QgsField('path_id', FIELD_TYPE_INT))
        fields.append(QgsField('distance_to_path_m', FIELD_TYPE_DOUBLE))
        fields.append(QgsField('kp_km', FIELD_TYPE_DOUBLE))
        fields.append(QgsField('kp_ref', FIELD_TYPE_STRING))  # Added kp_ref
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
        fields.append(QgsField('kp_ref', FIELD_TYPE_STRING))               # Add kp_ref
        fields.append(QgsField('range_to_target_m', FIELD_TYPE_DOUBLE))     # Add range_to_target
        fields.append(QgsField('bearing_to_target_deg', FIELD_TYPE_DOUBLE)) # Add bearing_to_target
        fields.append(QgsField('kp_km', FIELD_TYPE_DOUBLE))                 # Add kp_km
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
