# kp_range_csv_algorithm.py
# -*- coding: utf-8 -*-
"""
KPRangeCSVAlgorithm
This tool processes a CSV file and extracts KP ranges from a given RPL line.
"""

import csv
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterField,
                       QgsProcessingParameterString,
                       QgsFeature,
                       QgsGeometry,
                       QgsPoint,
                       QgsPointXY,
                       QgsFields,
                       QgsField,
                       QgsWkbTypes,
                       QgsDistanceArea)

class KPRangeCSVAlgorithm(QgsProcessingAlgorithm):
    INPUT_LAYER = 'INPUT_LAYER'
    INPUT_LINE = 'INPUT_LINE'
    START_KP_FIELD = 'START_KP_FIELD'
    END_KP_FIELD = 'END_KP_FIELD'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LAYER,
                self.tr('Input Table of KP Ranges'),
                [QgsProcessing.TypeVector]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LINE,
                self.tr('Input RPL Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.START_KP_FIELD,
                self.tr('Start KP Field'),
                parentLayerParameterName=self.INPUT_LAYER,
                type=QgsProcessingParameterField.Numeric
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.END_KP_FIELD,
                self.tr('End KP Field'),
                parentLayerParameterName=self.INPUT_LAYER,
                type=QgsProcessingParameterField.Numeric
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        input_layer = self.parameterAsSource(parameters, self.INPUT_LAYER, context)
        source = self.parameterAsSource(parameters, self.INPUT_LINE, context)
        start_kp_field = self.parameterAsString(parameters, self.START_KP_FIELD, context)
        end_kp_field = self.parameterAsString(parameters, self.END_KP_FIELD, context)

        # Default to all fields except the KP fields
        all_field_names = input_layer.fields().names()
        additional_fields = [name for name in all_field_names if name not in [start_kp_field, end_kp_field]]

        # Create fields for the output layer
        fields = QgsFields()
        fields.append(QgsField('start_kp', QVariant.Double))
        fields.append(QgsField('end_kp', QVariant.Double))
        for field_name in additional_fields:
            # Get the field from the input layer to preserve its type
            input_field = input_layer.fields().field(field_name)
            fields.append(input_field)
        fields.append(QgsField('source_table', QVariant.String))
        fields.append(QgsField('source_line', QVariant.String))

        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT,
                                               context, fields, QgsWkbTypes.LineString, source.sourceCrs())

        # Combine all features from the line layer into a single geometry
        geometries = [f.geometry() for f in source.getFeatures()]
        if not geometries:
            return {self.OUTPUT: dest_id}

        combined_geom = QgsGeometry.unaryUnion(geometries)

        if combined_geom.isEmpty():
            feedback.pushInfo("Input line layer is empty or invalid.")
            return {self.OUTPUT: dest_id}

        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(source.sourceCrs(), context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

        # Pre-calculate the total length of the line
        total_length = 0
        if not combined_geom.isEmpty():
            if combined_geom.isMultipart():
                parts = combined_geom.asMultiPolyline()
            else:
                parts = [combined_geom.asPolyline()]
            for part in parts:
                for i in range(len(part) - 1):
                    total_length += distance_calculator.measureLine(part[i], part[i+1])
        feedback.pushInfo(f"Total length of dissolved input line: {total_length} meters")

        input_features = list(input_layer.getFeatures())
        total_rows = len(input_features)

        for current, feature in enumerate(input_features):
            if feedback.isCanceled():
                break
            try:
                start_kp = float(feature[start_kp_field])
                end_kp = float(feature[end_kp_field])
            except (ValueError, KeyError):
                feedback.reportError(f"Invalid KP values in row {current + 1}. Skipping.")
                continue

            if (start_kp * 1000) > total_length or (end_kp * 1000) > total_length:
                feedback.reportError(f"KP range {start_kp}-{end_kp} exceeds total line length of {total_length/1000:.2f} km. Skipping.")
                continue
            
            segment = self.extractLineSegment(combined_geom, start_kp, end_kp, distance_calculator, feedback)
            
            if segment:
                feat = QgsFeature(fields)
                feat.setGeometry(QgsGeometry.fromPolyline(segment))
                attributes = [start_kp, end_kp]
                for field_name in additional_fields:
                    attributes.append(feature[field_name])
                attributes.append(input_layer.sourceName())
                attributes.append(source.sourceName())
                feat.setAttributes(attributes)
                sink.addFeature(feat, QgsFeatureSink.FastInsert)
            else:
                feedback.reportError(f"Could not extract line segment for KP range {start_kp}-{end_kp}. Skipping.")
            feedback.setProgress(int((current + 1) / total_rows * 100))
        return {self.OUTPUT: dest_id}

    def extractLineSegment(self, line_geometry, start_kp, end_kp, distance_calculator, feedback):
        start_kp_m = start_kp * 1000
        end_kp_m = end_kp * 1000

        if line_geometry.isMultipart():
            parts = line_geometry.asMultiPolyline()
        else:
            parts = [line_geometry.asPolyline()]

        segment = []
        cumulative_length = 0.0
        start_found = False

        for part in parts:
            for i in range(len(part) - 1):
                point1 = part[i]
                point2 = part[i + 1]
                segment_length = distance_calculator.measureLine(point1, point2)
                next_cumulative_length = cumulative_length + segment_length

                if not start_found and next_cumulative_length >= start_kp_m:
                    ratio = (start_kp_m - cumulative_length) / segment_length
                    start_point = QgsPoint(
                        point1.x() + ratio * (point2.x() - point1.x()),
                        point1.y() + ratio * (point2.y() - point1.y())
                    )
                    segment.append(start_point)
                    start_found = True

                if start_found:
                    if next_cumulative_length <= end_kp_m:
                        segment.append(QgsPoint(point2))
                    else:
                        ratio = (end_kp_m - cumulative_length) / segment_length
                        end_point = QgsPoint(
                            point1.x() + ratio * (point2.x() - point1.x()),
                            point1.y() + ratio * (point2.y() - point1.y())
                        )
                        segment.append(end_point)
                        return segment
                
                cumulative_length = next_cumulative_length
                if cumulative_length >= end_kp_m:
                    return segment
        return segment if segment else None

    def shortHelpString(self):
        return self.tr("""
This tool highlights sections of a line based on KP ranges from a table layer (like a CSV). All columns from the input table will be included in the output layer, along with fields for the source table and line layer names.

**Instructions:**

1.  **Load your CSV:** First, load your CSV file into QGIS. Go to "Layer" -> "Add Layer" -> "Add Delimited Text Layer...". In the dialog, select your file and choose "No geometry (attribute only table)".
2.  **Select Input Layer:** Choose the newly loaded table layer from the 'Input Table of KP Ranges' dropdown.
3.  **Select Line Layer:** Choose the RPL line layer you want to process.
4.  **Map Fields:** Select the columns from your table that contain the 'Start KP' and 'End KP' values.
5.  **Run:** Execute the tool.
""")

    def name(self):
        return 'kp_range_csv_processor'

    def displayName(self):
        return self.tr('KP Range Highlighter from CSV')

    def group(self):
        # Updated to return the desired group name.
        return self.tr('KP Ranges')

    def groupId(self):
        # Updated to return a unique id for the desired group.
        return 'kp_ranges'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return KPRangeCSVAlgorithm()
