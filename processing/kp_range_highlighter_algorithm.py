# kp_range_highlighter_algorithm.py
# -*- coding: utf-8 -*-
"""
KPRangeHighlighterAlgorithm
This tool highlights KP ranges along a path.
"""

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterString,
                       QgsFeature,
                       QgsGeometry,
                       QgsPoint,
                       QgsDistanceArea,
                       QgsField,
                       QgsFields)

class KPRangeHighlighterAlgorithm(QgsProcessingAlgorithm):
    """
    This algorithm highlights sections of an RPL line based on user-defined
    Kilometer Points (KPs).
    """

    OUTPUT = 'OUTPUT'
    INPUT = 'INPUT'
    START_KP = 'START_KP'
    END_KP = 'END_KP'
    CUSTOM_LABEL = 'CUSTOM_LABEL'

    def initAlgorithm(self, config=None):
        # Input RPL line layer
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Input RPL Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        # Start KP
        self.addParameter(
            QgsProcessingParameterNumber(
                self.START_KP,
                self.tr('Start KP'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0
            )
        )
        # End KP
        self.addParameter(
            QgsProcessingParameterNumber(
                self.END_KP,
                self.tr('End KP'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0
            )
        )
        # Custom Label
        self.addParameter(
            QgsProcessingParameterString(
                self.CUSTOM_LABEL,
                self.tr('Custom Label'),
                optional=True
            )
        )
        # Output layer
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        start_kp = self.parameterAsDouble(parameters, self.START_KP, context)
        end_kp = self.parameterAsDouble(parameters, self.END_KP, context)
        custom_label = self.parameterAsString(parameters, self.CUSTOM_LABEL, context)

        # Only create new fields for the output layer (do not copy source fields)
        fields = QgsFields()
        fields.append(QgsField('start_kp', QVariant.Double))
        fields.append(QgsField('end_kp', QVariant.Double))
        fields.append(QgsField('length_km', QVariant.Double))
        include_custom_label = bool(custom_label and custom_label.strip())
        if include_custom_label:
            fields.append(QgsField('custom_label', QVariant.String))

        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT,
                                               context, fields, source.wkbType(), source.sourceCrs())

        # Combine all features into a single geometry
        geometries = [f.geometry() for f in source.getFeatures()]
        if not geometries:
            return {self.OUTPUT: dest_id}

        # Use unaryUnion to dissolve the geometries into a single line
        combined_geom = QgsGeometry.unaryUnion(geometries)

        if combined_geom.isEmpty() or not combined_geom.isMultipart():
            line_parts = [combined_geom.asPolyline()]
        else:
            line_parts = combined_geom.asMultiPolyline()

        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(source.sourceCrs(), context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

        # Convert KP to meters
        start_kp_m = start_kp * 1000
        end_kp_m = end_kp * 1000

        total_length = 0.0
        segment = []
        start_found = False
        end_found = False

        for part in line_parts:
            for i in range(len(part) - 1):
                point1 = part[i]
                point2 = part[i + 1]

                segment_length = distance_calculator.measureLine(point1, point2)
                next_total_length = total_length + segment_length

                if not start_found and next_total_length >= start_kp_m:
                    # Interpolate start point
                    ratio = (start_kp_m - total_length) / segment_length
                    start_point = QgsPoint(
                        point1.x() + ratio * (point2.x() - point1.x()),
                        point1.y() + ratio * (point2.y() - point1.y())
                    )
                    segment.append(start_point)
                    start_found = True

                if start_found and not end_found:
                    if next_total_length <= end_kp_m:
                        segment.append(QgsPoint(point2))
                    else:
                        # Interpolate end point
                        ratio = (end_kp_m - total_length) / segment_length
                        end_point = QgsPoint(
                            point1.x() + ratio * (point2.x() - point1.x()),
                            point1.y() + ratio * (point2.y() - point1.y())
                        )
                        segment.append(end_point)
                        end_found = True
                        break

                total_length = next_total_length

                if end_found:
                    break

            if end_found:
                break

        if segment:
            new_feature = QgsFeature(fields)
            new_feature.setGeometry(QgsGeometry.fromPolyline(segment))
            # Set start_kp, end_kp, length_km, and custom_label if provided
            length_km = end_kp - start_kp
            attrs = [start_kp, end_kp, length_km]
            if include_custom_label:
                attrs.append(custom_label)
            new_feature.setAttributes(attrs)
            sink.addFeature(new_feature, QgsFeatureSink.FastInsert)

        feedback.setProgress(100)

        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr("""
This tool highlights a specific section of a line layer based on start and end Kilometer Points (KPs).

**Instructions:**

1.  **Select Line Layer:** Choose the line layer you want to process from the 'Input RPL Line Layer' dropdown. The tool will automatically handle lines made of multiple segments.
2.  **Enter KP Range:**
    *   **Start KP:** Type the starting KP for the section you want to highlight.
    *   **End KP:** Type the ending KP for the section.
3.  **Add Label (Optional):** You can add a custom text label to the output feature. This is useful for identification.
4.  **Run:** Execute the tool. The output will be a new line layer containing only the highlighted segment.
""")

    def name(self):
        return 'kp_range_highlighter'

    def displayName(self):
        return self.tr('KP Range Highlighter')

    def group(self):
        # Updated to return the desired group name.
        return self.tr('KP Ranges')

    def groupId(self):
        # Updated to return a unique id for the desired group.
        return 'kp_ranges'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return KPRangeHighlighterAlgorithm()
