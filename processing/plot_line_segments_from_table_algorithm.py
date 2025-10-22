# plot_line_segments_from_table_algorithm.py
# -*- coding: utf-8 -*-
"""
PlotLineSegmentsFromTableAlgorithm
This tool plots line segments from a table layer with start and end lat/lon columns.
"""

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsPointXY,
    QgsGeometry,
    QgsWkbTypes,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProcessingException,
    QgsFeatureSink,
    QgsProcessingContext
)

class PlotLineSegmentsFromTableAlgorithm(QgsProcessingAlgorithm):
    INPUT_TABLE = 'INPUT_TABLE'
    START_LAT_FIELD = 'START_LAT_FIELD'
    START_LON_FIELD = 'START_LON_FIELD'
    END_LAT_FIELD = 'END_LAT_FIELD'
    END_LON_FIELD = 'END_LON_FIELD'
    CREATE_POINT_LAYER = 'CREATE_POINT_LAYER'
    OUTPUT_LINE_LAYER = 'OUTPUT_LINE_LAYER'
    OUTPUT_POINT_LAYER = 'OUTPUT_POINT_LAYER'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_TABLE,
                self.tr('Input Table Layer'),
                [QgsProcessing.TypeVector]
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.START_LAT_FIELD,
                self.tr('Start Latitude Field'),
                parentLayerParameterName=self.INPUT_TABLE,
                type=QgsProcessingParameterField.Numeric
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.START_LON_FIELD,
                self.tr('Start Longitude Field'),
                parentLayerParameterName=self.INPUT_TABLE,
                type=QgsProcessingParameterField.Numeric
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.END_LAT_FIELD,
                self.tr('End Latitude Field'),
                parentLayerParameterName=self.INPUT_TABLE,
                type=QgsProcessingParameterField.Numeric
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.END_LON_FIELD,
                self.tr('End Longitude Field'),
                parentLayerParameterName=self.INPUT_TABLE,
                type=QgsProcessingParameterField.Numeric
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CREATE_POINT_LAYER,
                self.tr('Create Point Layer for Endpoints'),
                defaultValue=False
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_LINE_LAYER,
                self.tr('Output Line Layer')
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POINT_LAYER,
                self.tr('Output Point Layer'),
                optional=True
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        table_layer = self.parameterAsSource(parameters, self.INPUT_TABLE, context)
        start_lat_field = self.parameterAsString(parameters, self.START_LAT_FIELD, context)
        start_lon_field = self.parameterAsString(parameters, self.START_LON_FIELD, context)
        end_lat_field = self.parameterAsString(parameters, self.END_LAT_FIELD, context)
        end_lon_field = self.parameterAsString(parameters, self.END_LON_FIELD, context)
        create_point_layer = self.parameterAsBoolean(parameters, self.CREATE_POINT_LAYER, context)

        if table_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_TABLE))

        # Prepare output fields: copy all from input plus source_table
        output_fields = QgsFields(table_layer.fields())
        output_fields.append(QgsField('source_table', QVariant.String))

        # Assume WGS84 for lat/lon
        wgs84_crs = QgsCoordinateReferenceSystem('EPSG:4326')
        source_crs = table_layer.sourceCrs()
        if source_crs != wgs84_crs:
            transform = QgsCoordinateTransform(source_crs, wgs84_crs, context.project())
        else:
            transform = None

        # Line layer sink
        (line_sink, line_dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT_LINE_LAYER, context, output_fields, QgsWkbTypes.LineString, wgs84_crs
        )
        if line_sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_LINE_LAYER))

        # Set default output name
        details = QgsProcessingContext.LayerDetails(f"{table_layer.sourceName()}_Lines", context.project())
        context.addLayerToLoadOnCompletion(line_dest_id, details)

        point_sink = None
        point_dest_id = None
        point_fields = None
        if create_point_layer:
            point_fields = QgsFields(table_layer.fields())
            point_fields.append(QgsField('source_table', QVariant.String))
            point_fields.append(QgsField('point_type', QVariant.String))
            (point_sink, point_dest_id) = self.parameterAsSink(
                parameters, self.OUTPUT_POINT_LAYER, context, point_fields, QgsWkbTypes.Point, wgs84_crs
            )
            if point_sink is None:
                raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_POINT_LAYER))

            # Set default output name
            details = QgsProcessingContext.LayerDetails(f"{table_layer.sourceName()}_Points", context.project())
            context.addLayerToLoadOnCompletion(point_dest_id, details)

        total_features = table_layer.featureCount()
        feedback.pushInfo(self.tr(f"Processing {total_features} features..."))

        skipped_count = 0
        for current, feature in enumerate(table_layer.getFeatures()):
            if feedback.isCanceled():
                break

            start_lat = feature[start_lat_field]
            start_lon = feature[start_lon_field]
            end_lat = feature[end_lat_field]
            end_lon = feature[end_lon_field]

            # Convert to double, handling both QVariant and direct numeric types
            def get_double_value(val):
                if val is None:
                    return 0.0, False
                if isinstance(val, (int, float)):
                    return float(val), True
                else:
                    # Assume QVariant
                    if val.isNull() or not val.isValid():
                        return 0.0, False
                    return val.toDouble()

            start_lat_val, ok1 = get_double_value(start_lat)
            start_lon_val, ok2 = get_double_value(start_lon)
            end_lat_val, ok3 = get_double_value(end_lat)
            end_lon_val, ok4 = get_double_value(end_lon)

            if not (ok1 and ok2 and ok3 and ok4):
                skipped_count += 1
                continue

            # Create line geometry
            start_point = QgsPointXY(start_lon_val, start_lat_val)
            end_point = QgsPointXY(end_lon_val, end_lat_val)
            line_geom = QgsGeometry.fromPolylineXY([start_point, end_point])

            # Transform if needed
            if transform:
                line_geom.transform(transform)

            # Create line feature
            line_feat = QgsFeature(output_fields)
            line_feat.setGeometry(line_geom)
            # Copy all attributes
            for field in table_layer.fields():
                line_feat.setAttribute(field.name(), feature[field.name()])
            line_feat.setAttribute('source_table', table_layer.sourceName())
            line_sink.addFeature(line_feat, QgsFeatureSink.FastInsert)

            # Create point features if enabled
            if create_point_layer:
                # Start point
                start_geom = QgsGeometry.fromPointXY(start_point)
                if transform:
                    start_geom.transform(transform)
                start_feat = QgsFeature(point_fields)
                start_feat.setGeometry(start_geom)
                for field in table_layer.fields():
                    start_feat.setAttribute(field.name(), feature[field.name()])
                start_feat.setAttribute('source_table', table_layer.sourceName())
                start_feat.setAttribute('point_type', 'start')
                point_sink.addFeature(start_feat, QgsFeatureSink.FastInsert)

                # End point
                end_geom = QgsGeometry.fromPointXY(end_point)
                if transform:
                    end_geom.transform(transform)
                end_feat = QgsFeature(point_fields)
                end_feat.setGeometry(end_geom)
                for field in table_layer.fields():
                    end_feat.setAttribute(field.name(), feature[field.name()])
                end_feat.setAttribute('source_table', table_layer.sourceName())
                end_feat.setAttribute('point_type', 'end')
                point_sink.addFeature(end_feat, QgsFeatureSink.FastInsert)

            feedback.setProgress(int((current + 1) / total_features * 100))

        if skipped_count > 0:
            feedback.pushInfo(self.tr(f"Skipped {skipped_count} features with invalid or missing lat/lon values"))

        results = {self.OUTPUT_LINE_LAYER: line_dest_id}
        if create_point_layer:
            results[self.OUTPUT_POINT_LAYER] = point_dest_id

        return results

    def shortHelpString(self):
        return self.tr("""
<h3>Plot Line Segments from Table</h3>
<p>This tool creates line segments from a table layer containing start and end latitude/longitude coordinates.</p>

<h4>Parameters</h4>
<ul>
  <li><b>Input Table Layer:</b> The table layer containing the data.</li>
  <li><b>Start Latitude Field:</b> The field containing the starting latitude values.</li>
  <li><b>Start Longitude Field:</b> The field containing the starting longitude values.</li>
  <li><b>End Latitude Field:</b> The field containing the ending latitude values.</li>
  <li><b>End Longitude Field:</b> The field containing the ending longitude values.</li>
  <li><b>Create Point Layer for Endpoints:</b> Optionally create a point layer with the start and end points.</li>
</ul>

<h4>Output</h4>
<ul>
  <li><b>Line Layer:</b> A line layer with segments connecting start and end points, including all original attributes and a source_table field.</li>
  <li><b>Point Layer (optional):</b> A point layer with start and end points, including point_type attribute.</li>
</ul>
""")

    def name(self):
        return 'plotlinesegmentsfromtable'

    def displayName(self):
        return self.tr('Plot Line Segments from Table')

    def group(self):
        return self.tr('Other Tools')

    def groupId(self):
        return 'other_tools'

    def createInstance(self):
        return PlotLineSegmentsFromTableAlgorithm()

    def tr(self, string):
        return QCoreApplication.translate('PlotLineSegmentsFromTableAlgorithm', string)