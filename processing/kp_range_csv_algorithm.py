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
                       QgsProcessingParameterFile,
                       QgsProcessingParameterBoolean,
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
    INPUT_CSV = 'INPUT_CSV'
    INPUT_LINE = 'INPUT_LINE'
    HAS_HEADER = 'HAS_HEADER'
    START_KP_COLUMN = 'START_KP_COLUMN'
    END_KP_COLUMN = 'END_KP_COLUMN'
    ADDITIONAL_COLUMNS = 'ADDITIONAL_COLUMNS'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_CSV,
                self.tr('Input CSV file'),
                extension='csv'
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
            QgsProcessingParameterBoolean(
                self.HAS_HEADER,
                self.tr('CSV has header row'),
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.START_KP_COLUMN,
                self.tr('Start KP Column Name or Index'),
                defaultValue='0'
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.END_KP_COLUMN,
                self.tr('End KP Column Name or Index'),
                defaultValue='1'
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.ADDITIONAL_COLUMNS,
                self.tr('Additional Columns to Include (comma-separated names or indices)'),
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        csv_file = self.parameterAsFile(parameters, self.INPUT_CSV, context)
        source = self.parameterAsSource(parameters, self.INPUT_LINE, context)
        has_header = self.parameterAsBool(parameters, self.HAS_HEADER, context)
        start_kp_column = self.parameterAsString(parameters, self.START_KP_COLUMN, context)
        end_kp_column = self.parameterAsString(parameters, self.END_KP_COLUMN, context)
        additional_columns = self.parameterAsString(parameters, self.ADDITIONAL_COLUMNS, context)

        with open(csv_file, 'r') as f:
            reader = csv.reader(f)
            header = next(reader) if has_header else [f'Column_{i}' for i in range(len(next(reader)))]
        def get_column_index(column_identifier):
            if column_identifier.isdigit():
                return int(column_identifier)
            else:
                return header.index(column_identifier)
        start_kp_index = get_column_index(start_kp_column)
        end_kp_index = get_column_index(end_kp_column)
        additional_column_indices = []
        if additional_columns:
            for col in additional_columns.split(','):
                col = col.strip()
                additional_column_indices.append(get_column_index(col))
        fields = QgsFields()
        fields.append(QgsField('start_kp', QVariant.Double))
        fields.append(QgsField('end_kp', QVariant.Double))
        for idx in additional_column_indices:
            fields.append(QgsField(header[idx], QVariant.String))
        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT,
                                               context, fields, QgsWkbTypes.LineString, source.sourceCrs())
        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(source.sourceCrs(), context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())
        total_length = 0
        for feature in source.getFeatures():
            geom = feature.geometry()
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
            else:
                parts = [geom.asPolyline()]
            for part in parts:
                for i in range(len(part) - 1):
                    total_length += distance_calculator.measureLine(part[i], part[i+1])
        feedback.pushInfo(f"Total length of input line: {total_length} meters")
        with open(csv_file, 'r') as f:
            reader = csv.reader(f)
            if has_header:
                next(reader)
            total_rows = sum(1 for row in f)
            if has_header:
                total_rows -= 1
            f.seek(0)
            if has_header:
                next(reader)
            for current, row in enumerate(reader):
                if feedback.isCanceled():
                    break
                try:
                    start_kp = float(row[start_kp_index])
                    end_kp = float(row[end_kp_index])
                except (ValueError, IndexError):
                    feedback.reportError(f"Invalid KP values in row {current + 1}. Skipping.")
                    continue
                segment = self.extractLineSegment(source, start_kp, end_kp, total_length, distance_calculator, feedback)
                if segment:
                    feat = QgsFeature(fields)
                    feat.setGeometry(QgsGeometry.fromPolyline(segment))
                    attributes = [start_kp, end_kp]
                    for idx in additional_column_indices:
                        attributes.append(row[idx] if idx < len(row) else '')
                    feat.setAttributes(attributes)
                    sink.addFeature(feat, QgsFeatureSink.FastInsert)
                else:
                    feedback.reportError(f"Could not extract line segment for KP range {start_kp}-{end_kp}. Skipping.")
                feedback.setProgress(int((current + 1) / total_rows * 100))
        return {self.OUTPUT: dest_id}

    def extractLineSegment(self, source, start_kp, end_kp, total_length, distance_calculator, feedback):
        start_kp_m = start_kp * 1000
        end_kp_m = end_kp * 1000
        if start_kp_m > total_length or end_kp_m > total_length:
            feedback.reportError(f"KP range {start_kp}-{end_kp} exceeds total line length. Skipping.")
            return None
        segment = []
        cumulative_length = 0.0
        for feature in source.getFeatures():
            geom = feature.geometry()
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
            else:
                parts = [geom.asPolyline()]
            for part in parts:
                for i in range(len(part) - 1):
                    point1 = part[i]
                    point2 = part[i + 1]
                    segment_length = distance_calculator.measureLine(point1, point2)
                    next_cumulative_length = cumulative_length + segment_length
                    if not segment and next_cumulative_length >= start_kp_m:
                        ratio = (start_kp_m - cumulative_length) / segment_length
                        start_point = QgsPoint(
                            point1.x() + ratio * (point2.x() - point1.x()),
                            point1.y() + ratio * (point2.y() - point1.y())
                        )
                        segment.append(start_point)
                    if segment:
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
