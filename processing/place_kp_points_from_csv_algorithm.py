# place_kp_points_from_csv_algorithm.py
# -*- coding: utf-8 -*-
"""
PlaceKpPointsFromCsvAlgorithm
This tool places KP points along a route from a CSV file.
"""

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterField,
                       QgsFeature,
                       QgsGeometry,
                       QgsPointXY,
                       QgsFields,
                       QgsField,
                       QgsWkbTypes,
                       QgsDistanceArea,
                       QgsProcessingException)

class PlaceKpPointsFromCsvAlgorithm(QgsProcessingAlgorithm):
    INPUT_TABLE = 'INPUT_TABLE'
    INPUT_LINE = 'INPUT_LINE'
    KP_FIELD = 'KP_FIELD'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_TABLE,
                self.tr('Input Table of KPs'),
                [QgsProcessing.TypeVector]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LINE,
                self.tr('Input Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.KP_FIELD,
                self.tr('KP Field'),
                parentLayerParameterName=self.INPUT_TABLE,
                type=QgsProcessingParameterField.Numeric
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output Point Layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        input_table = self.parameterAsSource(parameters, self.INPUT_TABLE, context)
        line_layer = self.parameterAsSource(parameters, self.INPUT_LINE, context)
        kp_field = self.parameterAsString(parameters, self.KP_FIELD, context)

        if input_table is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_TABLE))
        if line_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_LINE))

        # Create fields for the output layer, copying from the input table
        source_fields = input_table.fields()
        output_fields = QgsFields()
        for field in source_fields:
            output_fields.append(field)
        output_fields.append(QgsField('source_line', QVariant.String))
        output_fields.append(QgsField('kp_value', QVariant.Double))


        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, output_fields, QgsWkbTypes.Point, line_layer.sourceCrs()
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        # Dissolve line layer into a single geometry
        line_features = list(line_layer.getFeatures())
        if not line_features:
            raise QgsProcessingException(self.tr("Input line layer has no features."))
        
        geometries = [f.geometry() for f in line_features]
        merged_geometry = QgsGeometry.unaryUnion(geometries)
        
        if merged_geometry.isEmpty():
            raise QgsProcessingException(self.tr("Geometry is empty after merging features."))

        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(line_layer.sourceCrs(), context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

        total_length_m = distance_calculator.measureLength(merged_geometry)
        if total_length_m == 0:
            raise QgsProcessingException(self.tr("Line has no length."))

        line_parts = merged_geometry.asMultiPolyline() if merged_geometry.isMultipart() else [merged_geometry.asPolyline()]

        input_features = list(input_table.getFeatures())
        total_rows = len(input_features)
        points_placed = 0

        for current, feature in enumerate(input_features):
            if feedback.isCanceled():
                break
            
            try:
                kp_val = float(feature[kp_field])
            except (ValueError, KeyError):
                feedback.reportError(f"Invalid KP value in row {current + 1}. Skipping.")
                continue

            kp_dist_m = kp_val * 1000
            if kp_dist_m > total_length_m:
                feedback.reportError(f"KP value {kp_val} is beyond the line's total length of {total_length_m/1000:.3f} km. Skipping.")
                continue

            cumulative_length = 0.0
            point_found = False
            for part in line_parts:
                for i in range(len(part) - 1):
                    p1, p2 = part[i], part[i+1]
                    segment_length = distance_calculator.measureLine(p1, p2)
                    
                    if cumulative_length + segment_length >= kp_dist_m:
                        dist_into_segment = kp_dist_m - cumulative_length
                        ratio = dist_into_segment / segment_length if segment_length > 0 else 0
                        
                        x = p1.x() + ratio * (p2.x() - p1.x())
                        y = p1.y() + ratio * (p2.y() - p1.y())
                        point_geom = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                        
                        out_feat = QgsFeature(output_fields)
                        out_feat.setGeometry(point_geom)
                        
                        # Copy attributes from source feature
                        for i in range(len(source_fields)):
                            out_feat.setAttribute(i, feature.attribute(source_fields.at(i).name()))
                        
                        out_feat.setAttribute('source_line', line_layer.sourceName())
                        out_feat.setAttribute('kp_value', kp_val)
                        
                        sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                        points_placed += 1
                        point_found = True
                        break
                    
                    cumulative_length += segment_length
                if point_found:
                    break
            
            feedback.setProgress(int((current + 1) / total_rows * 100))

        feedback.pushInfo(self.tr(f"Placed {points_placed} points."))
        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr("""
This tool places points along a line layer based on KP values from a table (e.g., a CSV file). All columns from the input table will be included in the output layer.

**Instructions:**

1.  **Load your CSV:** First, load your CSV file into QGIS. Go to "Layer" -> "Add Layer" -> "Add Delimited Text Layer...". In the dialog, select your file and choose "No geometry (attribute only table)".
2.  **Input Table of KPs:** Select the newly loaded table layer from the 'Input Table of KPs' dropdown.
3.  **Input Line Layer:** Choose the line layer on which you want to place the KP points. The tool will treat all lines in this layer as a single, continuous route.
4.  **KP Field:** From the dropdown, select the column in your input table that contains the Kilometer Point (KP) values.
5.  **Run:** Execute the tool. The output will be a new point layer with points placed at the specified KPs. All other columns from your input table will be copied to the output layer.
""")

    def name(self):
        return 'placekppointsfromcsv'

    def displayName(self):
        return self.tr('Place KP Points from CSV')

    def group(self):
        return self.tr('KP Points')

    def groupId(self):
        return 'kppoints'

    def createInstance(self):
        return PlaceKpPointsFromCsvAlgorithm()

    def tr(self, string):
        return QCoreApplication.translate("PlaceKpPointsFromCsvAlgorithm", string)
