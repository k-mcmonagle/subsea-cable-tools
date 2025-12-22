# -*- coding: utf-8 -*-
"""
TranslateKPFromRPLToRPL
Translate KP (kilometer point) values from a design RPL line onto an existing
points layer (e.g. As-Laid RPL points). The tool adds three fields to the
target points output: `design_route_kp`, `design_route_dcc`, and
`design_route_ref`.

Typical workflow:
    1. User selects source RPL line layer (Design route)
    2. User selects target RPL points layer (As-Laid points)
    3. Algorithm calculates, for each target point, the KP on the design route
         (design_route_kp), the Distance Cross Course to the design route
         (design_route_dcc) and a reference string for the design route
         (design_route_ref).
    4. Output is a copy of the target points layer with the three new fields
         populated.
"""

import os
import sys
plugin_dir = os.path.dirname(os.path.dirname(__file__))
lib_dir = os.path.join(plugin_dir, 'lib')
if lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsFeatureSink,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsProject
)

from .rpl_comparison_utils import RPLComparator


class TranslateKPFromRPLToRPLAlgorithm(QgsProcessingAlgorithm):
    """
    Translate KP values from source RPL to target RPL using spatial proximity matching.
    
    Input:
      - Source Point Layer: Point features with locations (e.g., Design RPL events)
      - Source Line Layer: Line layer for source RPL (defines KP values)
      - Target Line Layer: Line layer for target RPL (target for KP translation)
    
    Output:
      - Output Point Layer: Source points projected onto target line with:
        * All original attributes from source points
        * translated_kp: KP value on target line
        * spatial_offset_m: Distance from source point to target line (confidence metric)
        * dcc_to_source_line: Distance Cross Course to source line (perpendicular distance)
        * source_line_name: Name of source line layer (for traceability)
        * target_line_name: Name of target line layer (for traceability)
    """

    # Parameter identifiers
    INPUT_SOURCE_LINE = 'INPUT_SOURCE_LINE'
    INPUT_TARGET_POINTS = 'INPUT_TARGET_POINTS'
    OUTPUT_POINTS = 'OUTPUT_POINTS'

    def initAlgorithm(self, config=None):
        """Define inputs and outputs."""
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_SOURCE_LINE,
                self.tr('Source Line Layer (e.g., Design Route)'),
                [QgsProcessing.TypeVectorLine]
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_TARGET_POINTS,
                self.tr('Target Point Layer (e.g., As-Laid Points)'),
                [QgsProcessing.TypeVectorPoint]
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POINTS,
                self.tr('Output Point Layer (target points with design KP fields)')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        """Execute the algorithm."""
        
        source_line = self.parameterAsVectorLayer(parameters, self.INPUT_SOURCE_LINE, context)
        target_points = self.parameterAsSource(parameters, self.INPUT_TARGET_POINTS, context)

        if source_line is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_SOURCE_LINE))
        if target_points is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_TARGET_POINTS))

        # Check CRS match
        if target_points.sourceCrs() != source_line.sourceCrs():
            raise QgsProcessingException(
                self.tr('CRS Mismatch: Target Points and Source Line must use the same CRS.')
            )

        # Build output fields based on target point layer and add three new fields
        output_fields = QgsFields()
        for field in target_points.fields():
            output_fields.append(field)

        # Add new design route fields
        output_fields.append(QgsField('design_route_kp', QVariant.Double))
        output_fields.append(QgsField('design_route_dcc', QVariant.Double))
        output_fields.append(QgsField('design_route_ref', QVariant.String))

        # Create output sink
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT_POINTS, context,
            output_fields,
            QgsWkbTypes.Point,
            target_points.sourceCrs()
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_POINTS))

        # Initialize RPL comparator (we only need source_line geometries for design KP/DCC)
        try:
            # Pass the same source_line for both parameters; comparator will have source geoms loaded
            comparator = RPLComparator(source_line, source_line, target_points.sourceCrs(), context)
        except Exception as e:
            raise QgsProcessingException(
                self.tr(f'Failed to initialize RPL Comparator: {str(e)}')
            )

        source_line_name = source_line.name()

        # Process each target point feature
        total_features = target_points.featureCount()
        features_processed = 0
        features_skipped = 0

        for tgt_feature in target_points.getFeatures():
            if feedback.isCanceled():
                break

            point_geom = tgt_feature.geometry()
            if point_geom.isEmpty() or point_geom.type() != QgsWkbTypes.PointGeometry:
                features_skipped += 1
                continue

            point_xy = point_geom.asPoint()

            try:
                # Calculate KP on the design (source) route for this target point
                design_kp = comparator.calculate_kp_to_point(point_xy, source=True)
                # Distance Cross Course (perpendicular distance from the point to design route)
                design_dcc = comparator.distance_cross_course(point_xy, source=True)

                # Create output feature (keep original geometry)
                out_feat = QgsFeature(output_fields)
                out_feat.setGeometry(QgsGeometry.fromPointXY(point_xy))

                attrs = list(tgt_feature.attributes())
                # Append new design fields
                attrs.append(round(design_kp, 3) if design_kp is not None else None)
                attrs.append(round(design_dcc, 3) if design_dcc is not None else None)
                attrs.append(source_line_name)

                out_feat.setAttributes(attrs)
                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)

                features_processed += 1

            except Exception as e:
                feedback.pushWarning(self.tr(f'Failed to process feature {tgt_feature.id()}: {str(e)}'))
                features_skipped += 1
                continue

            feedback.setProgress(int((features_processed + features_skipped) / total_features * 100))

        # Report results
        feedback.pushInfo(
            self.tr(f'Successfully translated {features_processed} point(s).')
        )
        if features_skipped > 0:
            feedback.pushInfo(
                self.tr(f'Skipped {features_skipped} point(s) due to errors or invalid geometry.')
            )

        return {self.OUTPUT_POINTS: dest_id}

    def name(self):
        return 'translatekpfromrpl'

    def displayName(self):
        return self.tr('Translate KP Between RPLs (Points)')

    def group(self):
        return self.tr('RPL Tools')

    def groupId(self):
        return 'rpl_tools'

    def shortHelpString(self):
        return self.tr("""
<h3>Translate KP Between RPLs (Points)</h3>

<p>For each point in the target point layer, calculate its KP (kilometer point) on a design route line and the perpendicular distance to it.</p>

<p><b>Inputs:</b></p>
<ul>
  <li>Source Line Layer: The design route line (e.g., Design RPL)</li>
  <li>Target Point Layer: Points to translate (e.g., As-Laid RPL events)</li>
</ul>

<p><b>Outputs:</b> A copy of the target points with three new fields:</p>
<ul>
  <li><code>design_route_kp</code>: KP (km) on the source line, nearest to this point</li>
  <li><code>design_route_dcc</code>: Distance Cross Course (m) — perpendicular distance to the source line</li>
  <li><code>design_route_ref</code>: Name of the source line layer (for reference)</li>
</ul>
""")

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return TranslateKPFromRPLToRPLAlgorithm()
