# place_ship_outlines_algorithm.py
# -*- coding: utf-8 -*-
"""
PlaceShipOutlinesAlgorithm
Place a ship outline geometry at each point in a point layer, rotated to a heading field.
"""

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterCrs,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsWkbTypes,
    QgsCoordinateTransform,
    QgsProject,
    QgsPointXY
)
import os

class PlaceShipOutlinesAlgorithm(QgsProcessingAlgorithm):
    SHIP_OUTLINE = 'SHIP_OUTLINE'
    POINTS = 'POINTS'
    HEADING_FIELD = 'HEADING_FIELD'
    CRP_OFFSET_X = 'CRP_OFFSET_X'
    CRP_OFFSET_Y = 'CRP_OFFSET_Y'
    ROTATION_OFFSET = 'ROTATION_OFFSET'
    POINT_INTERVAL = 'POINT_INTERVAL'
    OUTPUT = 'OUTPUT'

    def tr(self, string):
        return QCoreApplication.translate('PlaceShipOutlinesAlgorithm', string)

    def createInstance(self):
        return PlaceShipOutlinesAlgorithm()

    def name(self):
        return 'place_ship_outlines'

    def displayName(self):
        return self.tr('Place Ship Outlines at Points')

    def group(self):
        return self.tr('Other Tools')

    def groupId(self):
        return 'other_tools'

    def shortHelpString(self):
        return self.tr("""
This tool places a ship outline geometry at each point in a point layer, with optional rotation and offsets.

**Instructions:**

1. **Select Imported Ship Outline Layer:** Choose the ship outline layer you previously imported (polygon or polyline).
2. **Select Point Layer:** Choose the point layer where you want to place ship outlines (e.g., vessel positions, waypoints).
3. **Set Heading Field:** Select the field in the point layer that contains the heading (in degrees, 0 = North, increasing clockwise).
4. **Set CRP Offset (Optional):** Specify X and Y offsets to shift the outline relative to each point (e.g., to align a reference point on the ship with the point).
5. **Set Additional Rotation (Optional):** Apply an extra rotation (in degrees) to each outline, in addition to the heading field.
6. **Set Point Interval (Optional):** Place an outline only every Nth point (e.g. 5 = at points 1,6,11,...) to reduce output density. Leave as 1 to place at all points.
7. **Run:** Click 'Run' to create a new layer with ship outlines placed and rotated at each (Nth) point.

**Tips:**
- The heading field should be numeric and in degrees (0 = North, 90 = East, etc.).
- Use CRP offsets to align a specific part of the ship (e.g., stbd sheave/chute) with each point.
- The imported outline should be scaled and oriented correctly before using this tool (see the Import Ship Outline tool).
- The output layer can be styled, edited, or used in further analysis like any other QGIS vector layer.

**Output:**
- A new vector layer with a ship outline geometry placed and rotated at each input point.
""")

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.SHIP_OUTLINE,
                self.tr('Imported Ship Outline Layer (Polygon or Polyline)'),
                [QgsProcessing.TypeVectorPolygon, QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.POINTS,
                self.tr('Point Layer'),
                [QgsProcessing.TypeVectorPoint]
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.HEADING_FIELD,
                self.tr('Heading Field (degrees, 0 = North, clockwise)'),
                parentLayerParameterName=self.POINTS,
                type=QgsProcessingParameterField.Numeric
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CRP_OFFSET_X,
                self.tr('Additional CRP Offset X (output units)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CRP_OFFSET_Y,
                self.tr('Additional CRP Offset Y (output units)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.ROTATION_OFFSET,
                self.tr('Rotation Offset (degrees)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.POINT_INTERVAL,
                self.tr('Point Interval (place outline every Nth point; 1 = all)'),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=1,
                minValue=1
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output Layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        outline_layer = self.parameterAsVectorLayer(parameters, self.SHIP_OUTLINE, context)
        point_source = self.parameterAsSource(parameters, self.POINTS, context)
        heading_field = self.parameterAsFields(parameters, self.HEADING_FIELD, context)[0]
        crp_offset_x = self.parameterAsDouble(parameters, self.CRP_OFFSET_X, context)
        crp_offset_y = self.parameterAsDouble(parameters, self.CRP_OFFSET_Y, context)
        rotation_offset = self.parameterAsDouble(parameters, self.ROTATION_OFFSET, context)
        point_interval = self.parameterAsInt(parameters, self.POINT_INTERVAL, context)
        if point_interval < 1:
            point_interval = 1  # safety

        # Output geometry type matches ship outline
        geom_type = outline_layer.wkbType()
        output_crs = outline_layer.crs()

        # Prepare output fields (copy from points, plus ship outline attributes)
        fields = QgsFields(point_source.fields())
        added_outline_field_names = []
        for f in outline_layer.fields():
            if fields.indexFromName(f.name()) == -1:
                fields.append(f)
                added_outline_field_names.append(f.name())

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            geom_type,
            output_crs
        )

        # Prepare transformation if needed
        point_crs = point_source.sourceCrs()
        if point_crs != output_crs:
            transform = QgsCoordinateTransform(point_crs, output_crs, QgsProject.instance())
        else:
            transform = None

        # Get all ship outline geometries (should be one, but support multi)
        outline_features = [f for f in outline_layer.getFeatures() if not f.geometry().isEmpty()]
        if not outline_features:
            raise Exception('No valid geometry in ship outline layer.')
        outline_geoms = [f.geometry() for f in outline_features]
        # Cache the first outline feature's attributes (if any)
        outline_attrs = outline_features[0].attributes() if outline_features else []
    # Removed unused outline_field_names variable

        for i, point_feat in enumerate(point_source.getFeatures()):
            if feedback.isCanceled():
                break
            # Skip points based on interval (place at 0-based indices 0, interval, 2*interval, ...)
            if point_interval > 1 and (i % point_interval) != 0:
                continue
            pt_geom = point_feat.geometry()
            if pt_geom.isEmpty():
                continue
            pt = pt_geom.asPoint()
            if transform:
                pt = transform.transform(pt)
            heading = point_feat[heading_field] or 0.0
            # For North-pointing outline, rotate by -heading (nautical, 0=N, clockwise) to QGIS (0=E, CCW)
            total_rotation = -heading + rotation_offset
            for outline_geom in outline_geoms:
                # Transform outline: move to pt, rotate, apply CRP offset
                placed_geom = self._place_outline(
                    outline_geom,
                    pt,
                    total_rotation,
                    crp_offset_x,
                    crp_offset_y
                )
                out_feat = QgsFeature(fields)
                out_feat.setGeometry(placed_geom)
                # Copy point attributes first
                for f in point_source.fields():
                    out_feat.setAttribute(f.name(), point_feat[f.name()])
                # Copy outline attributes only for unique (non-colliding) outline fields we added
                for idx, f in enumerate(outline_layer.fields()):
                    if f.name() in added_outline_field_names and idx < len(outline_attrs):
                        out_feat.setAttribute(f.name(), outline_attrs[idx])
                sink.addFeature(out_feat)
        return {self.OUTPUT: dest_id}

    def _place_outline(self, geom, center_pt, rotation_deg, offset_x, offset_y):
        from math import radians, cos, sin
        rotation = radians(rotation_deg)
        def transform_point(pt):
            # Apply CRP offset, then rotate, then move to center_pt
            x = pt.x() - offset_x
            y = pt.y() - offset_y
            x_rot = x * cos(rotation) - y * sin(rotation)
            y_rot = x * sin(rotation) + y * cos(rotation)
            return QgsPointXY(center_pt.x() + x_rot, center_pt.y() + y_rot)
        if geom.isMultipart():
            if geom.type() == QgsWkbTypes.LineGeometry:
                parts = []
                for part in geom.asMultiPolyline():
                    new_part = [[transform_point(QgsPointXY(pt[0], pt[1])) for pt in ring] for ring in [part]]
                    parts.extend(new_part)
                return QgsGeometry.fromMultiPolylineXY(parts)
            else:
                parts = []
                for part in geom.asMultiPolygon():
                    new_part = []
                    for ring in part:
                        new_ring = [transform_point(QgsPointXY(pt[0], pt[1])) for pt in ring]
                        new_part.append(new_ring)
                    parts.append(new_part)
                return QgsGeometry.fromMultiPolygonXY(parts)
        else:
            if geom.type() == QgsWkbTypes.LineGeometry:
                line = geom.asPolyline()
                new_line = [transform_point(QgsPointXY(pt[0], pt[1])) for pt in line]
                return QgsGeometry.fromPolylineXY(new_line)
            else:
                poly = geom.asPolygon()
                new_poly = []
                for ring in poly:
                    new_ring = [transform_point(QgsPointXY(pt[0], pt[1])) for pt in ring]
                    new_poly.append(new_ring)
                return QgsGeometry.fromPolygonXY(new_poly)
