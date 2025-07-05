# place_single_kp_point_algorithm.py
# -*- coding: utf-8 -*-
"""
PlaceSingleKpPointAlgorithm
This tool places a single KP point along a route.
"""

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterFeatureSink,
                       QgsFeature,
                       QgsGeometry,
                       QgsPointXY,
                       QgsFields,
                       QgsField,
                       QgsWkbTypes,
                       QgsDistanceArea,
                       QgsProcessingException,
                       QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform)

class PlaceSingleKpPointAlgorithm(QgsProcessingAlgorithm):
    INPUT_LINE = 'INPUT_LINE'
    KP_VALUE = 'KP_VALUE'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LINE,
                self.tr('Input Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.KP_VALUE,
                self.tr('KP Value (Kilometers)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output Point Layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        line_layer = self.parameterAsSource(parameters, self.INPUT_LINE, context)
        kp_val = self.parameterAsDouble(parameters, self.KP_VALUE, context)

        if line_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_LINE))

        source_crs = line_layer.sourceCrs()
        dest_crs = QgsCoordinateReferenceSystem('EPSG:4326')
        transform = QgsCoordinateTransform(source_crs, dest_crs, context.project())

        output_fields = QgsFields()
        output_fields.append(QgsField('source_line', QVariant.String))
        output_fields.append(QgsField('kp_value', QVariant.Double))
        output_fields.append(QgsField('latitude', QVariant.Double))
        output_fields.append(QgsField('longitude', QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, output_fields, QgsWkbTypes.Point, line_layer.sourceCrs()
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

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

        kp_dist_m = kp_val * 1000
        if kp_dist_m > total_length_m:
            feedback.reportError(f"KP value {kp_val} is beyond the line's total length of {total_length_m/1000:.3f} km. Point not placed.")
            return {self.OUTPUT: None}

        line_parts = merged_geometry.asMultiPolyline() if merged_geometry.isMultipart() else [merged_geometry.asPolyline()]

        cumulative_length = 0.0
        point_found = False
        for part in line_parts:
            for i in range(len(part) - 1):
                p1, p2 = part[i], part[i+1]
                segment_length = distance_calculator.measureLine(p1, p2)
                
                if cumulative_length <= kp_dist_m < cumulative_length + segment_length:
                    dist_into_segment = kp_dist_m - cumulative_length
                    ratio = dist_into_segment / segment_length if segment_length > 0 else 0
                    
                    x = p1.x() + ratio * (p2.x() - p1.x())
                    y = p1.y() + ratio * (p2.y() - p1.y())
                    point_geom = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                    
                    # Transform point to get lat/lon
                    transformed_geom = QgsGeometry(point_geom)
                    transformed_geom.transform(transform)
                    lon = transformed_geom.asPoint().x()
                    lat = transformed_geom.asPoint().y()

                    out_feat = QgsFeature(output_fields)
                    out_feat.setGeometry(point_geom)
                    out_feat.setAttribute('source_line', line_layer.sourceName())
                    out_feat.setAttribute('kp_value', kp_val)
                    out_feat.setAttribute('latitude', lat)
                    out_feat.setAttribute('longitude', lon)
                    
                    sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                    point_found = True
                    break
                
                cumulative_length += segment_length
            if point_found:
                break
        
        if not point_found and kp_val == round(total_length_m / 1000, 3):
             # Handle case where KP is at the very end of the line
            last_part = line_parts[-1]
            last_point = last_part[-1]
            point_geom = QgsGeometry.fromPointXY(last_point)

            # Transform point to get lat/lon
            transformed_geom = QgsGeometry(point_geom)
            transformed_geom.transform(transform)
            lon = transformed_geom.asPoint().x()
            lat = transformed_geom.asPoint().y()

            out_feat = QgsFeature(output_fields)
            out_feat.setGeometry(point_geom)
            out_feat.setAttribute('source_line', line_layer.sourceName())
            out_feat.setAttribute('kp_value', kp_val)
            out_feat.setAttribute('latitude', lat)
            out_feat.setAttribute('longitude', lon)
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
            point_found = True

        if point_found:
            feedback.pushInfo(self.tr(f"Placed 1 point at KP {kp_val}."))
        else:
            feedback.reportError(self.tr(f"Could not place point at KP {kp_val}."), fatalError=True)


        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr("""
This tool places a single point at a specified Kilometer Point (KP) along a line layer.

**Instructions:**

1.  **Input Line Layer:** Choose the line layer on which you want to place the KP point. The tool will treat all lines in this layer as a single, continuous route.
2.  **KP Value (Kilometers):** Enter the exact KP value where you want the point to be placed.
3.  **Run:** Execute the tool. The output will be a new point layer containing the single point.
""")

    def name(self):
        return 'placesinglekppoint'

    def displayName(self):
        return self.tr('Place Single KP Point')

    def group(self):
        return self.tr('KP Points')

    def groupId(self):
        return 'kppoints'

    def createInstance(self):
        return PlaceSingleKpPointAlgorithm()

    def tr(self, string):
        return QCoreApplication.translate("PlaceSingleKpPointAlgorithm", string)
