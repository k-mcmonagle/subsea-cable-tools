# import_ship_outline_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportShipOutlineAlgorithm
Import a ship outline from a DXF file, with user-defined scale, rotation, and CRP offset.
"""

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterPoint,
    QgsProcessingParameterString,
    QgsProcessingParameterCrs,
    QgsVectorLayer,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsWkbTypes,
    QgsCoordinateReferenceSystem,
    QgsPointXY,
    QgsProcessingLayerPostProcessorInterface
)
import os

class Renamer(QgsProcessingLayerPostProcessorInterface):
    def __init__(self, layer_name):
        self.name = layer_name
        super().__init__()

    def postProcessLayer(self, layer, context, feedback):
        layer.setName(self.name)

class ImportShipOutlineAlgorithm(QgsProcessingAlgorithm):
    INPUT_DXF = 'INPUT_DXF'
    SCALE = 'SCALE'
    ROTATION = 'ROTATION'
    CRP_OFFSET_X = 'CRP_OFFSET_X'
    CRP_OFFSET_Y = 'CRP_OFFSET_Y'
    OUTPUT = 'OUTPUT'
    GEOM_TYPE = 'GEOM_TYPE'
    OUTPUT_CRS = 'OUTPUT_CRS'

    def tr(self, string):
        return QCoreApplication.translate('ImportShipOutlineAlgorithm', string)

    def createInstance(self):
        return ImportShipOutlineAlgorithm()

    def name(self):
        return 'import_ship_outline'

    def displayName(self):
        return self.tr('Import Ship Outline (DXF)')

    def group(self):
        return self.tr('Other Tools')

    def groupId(self):
        return 'other_tools'

    def shortHelpString(self):
        return self.tr("""
This tool imports a ship outline from a DXF file and creates a polygon or polyline layer in your QGIS project.

**Instructions:**

1. **Select Ship Outline DXF File:** Choose the DXF file containing the ship outline to import.
2. **Choose Geometry Type:** Select whether to import the outline as a Polyline (open line) or Polygon (closed shape).
3. **Set Scale (Optional):** Adjust the scale factor if the DXF units differ from your project units (e.g. mm to m).
4. **Set Rotation (Optional):** Enter a rotation angle (in degrees) to rotate the outline if needed.
5. **Set CRP Offset (Optional):** Specify an offset to shift the outline to the correct position (e.g., to align with a reference point such as stbd sheave/chute).
6. **Run:** Click 'Run' to import the outline as a new layer.

**Tips:**
- Ensure your DXF file contains a clean outline of the ship. Remove any unnecessary elements before import for best results.
- Use the scale and rotation options to match the outline to your project's coordinate system and orientation. 0 degree rotation assumes the ship is pointed North/Up in the DXF. Coordinate 0,0 in the DXF is the CRP by default.
- The imported layer can be styled, edited, or used in further analysis like any other QGIS vector layer.
- Import as a Polyline if the Polygon option does not work since this is senstive to the shape being closed.
- Use this to import a ship outline, then use the "Place Ship Outlines at Points" tool to place it at specific points in your project.

**Output:**
- A new polyline or polygon layer representing the ship outline, ready for use in your QGIS project.
""")

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_DXF,
                self.tr('Ship Outline DXF File'),
                behavior=QgsProcessingParameterFile.File,
                extension='dxf'
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.GEOM_TYPE,
                self.tr('Geometry Type'),
                options=[self.tr('Polygon'), self.tr('Polyline')],
                defaultValue=1  # Default to Polyline
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SCALE,
                self.tr('DXF Drawing Scale (e.g. 0.001 for mm to m, 1 for m)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.ROTATION,
                self.tr('Default Rotation (degrees, 0 = North/up)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CRP_OFFSET_X,
                self.tr('CRP Offset X (drawing units)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CRP_OFFSET_Y,
                self.tr('CRP Offset Y (drawing units)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterCrs(
                self.OUTPUT_CRS,
                self.tr('Output CRS (should be a projected CRS, e.g. UTM or EPSG:3857)'),
                defaultValue='EPSG:3857'
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output Layer'),
                type=QgsProcessing.TypeVectorPolygon
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        dxf_path = self.parameterAsFile(parameters, self.INPUT_DXF, context)
        scale = self.parameterAsDouble(parameters, self.SCALE, context)
        rotation = self.parameterAsDouble(parameters, self.ROTATION, context)
        offset_x = self.parameterAsDouble(parameters, self.CRP_OFFSET_X, context)
        offset_y = self.parameterAsDouble(parameters, self.CRP_OFFSET_Y, context)

        geom_type_idx = self.parameterAsEnum(parameters, self.GEOM_TYPE, context)
        geom_type = QgsWkbTypes.Polygon if geom_type_idx == 0 else QgsWkbTypes.LineString
        output_crs = self.parameterAsCrs(parameters, self.OUTPUT_CRS, context)

        # Load the DXF as a temporary layer
        dxf_layer = QgsVectorLayer(dxf_path, 'ship_outline', 'ogr')
        if not dxf_layer.isValid():
            raise Exception('Failed to load DXF file.')

        # Reproject features if DXF CRS is different from output CRS
        dxf_crs = dxf_layer.crs() if dxf_layer.crs().isValid() else QgsCoordinateReferenceSystem('EPSG:3857')
        need_reproject = dxf_crs != output_crs

        # Prepare output fields
        fields = QgsFields()
        fields.append(QgsField('source_file', QVariant.String))
        fields.append(QgsField('import_scale', QVariant.Double))
        fields.append(QgsField('import_rotation_deg', QVariant.Double))
        fields.append(QgsField('import_crp_offset_x', QVariant.Double))
        fields.append(QgsField('import_crp_offset_y', QVariant.Double))
        fields.append(QgsField('import_output_crs', QVariant.String))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            geom_type,
            output_crs
        )

        # Transform and merge features
        from math import radians, cos, sin
        geoms = []
        for feat in dxf_layer.getFeatures():
            geom = feat.geometry()
            if geom.isEmpty():
                continue
            # Transform geometry: scale, rotate, offset
            geom = self._transform_geometry(geom, scale, rotation, offset_x, offset_y)
            if need_reproject:
                geom.transform(QgsCoordinateReferenceSystem(output_crs))
            geoms.append(geom)

        if not geoms:
            raise Exception('No valid geometry in DXF file.')

        # Merge all geometries into one
        merged_geom = geoms[0]
        for g in geoms[1:]:
            merged_geom = merged_geom.combine(g)

        out_feat = QgsFeature(fields)
        out_feat.setGeometry(merged_geom)
        out_feat.setAttribute('source_file', os.path.basename(dxf_path))
        out_feat.setAttribute('import_scale', scale)
        out_feat.setAttribute('import_rotation_deg', rotation)
        out_feat.setAttribute('import_crp_offset_x', offset_x)
        out_feat.setAttribute('import_crp_offset_y', offset_y)
        out_feat.setAttribute('import_output_crs', output_crs.authid())
        sink.addFeature(out_feat)

        # Set output layer name to input file name (without extension)
        filename = os.path.splitext(os.path.basename(dxf_path))[0]
        self._renamer = Renamer(filename)
        context.layerToLoadOnCompletionDetails(dest_id).setPostProcessor(self._renamer)

        return {self.OUTPUT: dest_id}

    def _transform_geometry(self, geom, scale, rotation_deg, offset_x, offset_y):
        from math import radians, cos, sin
        rotation = radians(rotation_deg)
        def transform_point(pt):
            # Apply offset (move CRP to 0,0), then scale, then rotate
            x = (pt.x() - offset_x) * scale
            y = (pt.y() - offset_y) * scale
            x_rot = x * cos(rotation) - y * sin(rotation)
            y_rot = x * sin(rotation) + y * cos(rotation)
            return QgsGeometry.fromPointXY(QgsPointXY(x_rot, y_rot))
        if geom.isMultipart():
            parts = []
            for part in geom.asMultiPolyline() if geom.type() == QgsWkbTypes.LineGeometry else geom.asMultiPolygon():
                new_part = []
                for ring in part:
                    new_ring = [transform_point(QgsPointXY(pt[0], pt[1])).asPoint() for pt in ring]
                    new_part.append(new_ring)
                parts.append(new_part)
            if geom.type() == QgsWkbTypes.LineGeometry:
                return QgsGeometry.fromMultiPolylineXY(parts)
            else:
                return QgsGeometry.fromMultiPolygonXY(parts)
        else:
            if geom.type() == QgsWkbTypes.LineGeometry:
                line = geom.asPolyline()
                new_line = [transform_point(QgsPointXY(pt[0], pt[1])).asPoint() for pt in line]
                return QgsGeometry.fromPolylineXY(new_line)
            else:
                poly = geom.asPolygon()
                new_poly = []
                for ring in poly:
                    new_ring = [transform_point(QgsPointXY(pt[0], pt[1])).asPoint() for pt in ring]
                    new_poly.append(new_ring)
                return QgsGeometry.fromPolygonXY(new_poly)
