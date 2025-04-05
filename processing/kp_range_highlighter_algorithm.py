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

        # Create new fields for the output layer
        fields = source.fields()
        fields.append(QgsField('start_kp', QVariant.Double))
        fields.append(QgsField('end_kp', QVariant.Double))
        fields.append(QgsField('source_layer', QVariant.String))
        fields.append(QgsField('custom_label', QVariant.String))

        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT,
                                               context, fields, source.wkbType(), source.sourceCrs())

        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(source.sourceCrs(), context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

        # Convert KP to meters
        start_kp_m = start_kp * 1000
        end_kp_m = end_kp * 1000

        total_features = source.featureCount()
        for current, feature in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break

            geom = feature.geometry()
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
            else:
                parts = [geom.asPolyline()]

            total_length = 0.0
            segment = []
            start_found = False
            end_found = False

            for part in parts:
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
                attributes = feature.attributes()
                attributes.extend([
                    start_kp,
                    end_kp,
                    source.sourceName(),
                    custom_label
                ])
                new_feature.setAttributes(attributes)
                sink.addFeature(new_feature, QgsFeatureSink.FastInsert)

            feedback.setProgress(int((current + 1) / total_features * 100))

        return {self.OUTPUT: dest_id}

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
