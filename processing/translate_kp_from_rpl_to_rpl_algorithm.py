# -*- coding: utf-8 -*-
"""
TranslateKPFromRPLToRPL
Translate KP (kilometer point) values from one RPL to another.

This algorithm takes point features from a source RPL (e.g., Design RPL) and
translates their KP values to a target RPL (e.g., As-Laid RPL) using spatial proximity.
Also calculates Distance Cross Course (DCC) from translated points to the source line.

Typical workflow:
  1. User selects source RPL point layer (e.g., Design events)
  2. User selects source RPL line layer (e.g., Design route)
  3. User selects target RPL line layer (e.g., As-Laid route)
  4. Algorithm translates each point's KP from source to target
  5. Output contains original attributes + translated_kp + dcc_to_source_line fields
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
    INPUT_SOURCE_POINTS = 'INPUT_SOURCE_POINTS'
    INPUT_SOURCE_LINE = 'INPUT_SOURCE_LINE'
    INPUT_TARGET_LINE = 'INPUT_TARGET_LINE'
    OUTPUT_POINTS = 'OUTPUT_POINTS'
    INCLUDE_SOURCE_KP = 'INCLUDE_SOURCE_KP'

    def initAlgorithm(self, config=None):
        """Define inputs and outputs."""
        
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_SOURCE_POINTS,
                self.tr('Source Point Layer (e.g., Design RPL Events)'),
                [QgsProcessing.TypeVectorPoint]
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_SOURCE_LINE,
                self.tr('Source Line Layer (e.g., Design Route)'),
                [QgsProcessing.TypeVectorLine]
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_TARGET_LINE,
                self.tr('Target Line Layer (e.g., As-Laid Route)'),
                [QgsProcessing.TypeVectorLine]
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.INCLUDE_SOURCE_KP,
                self.tr('Include Source KP Field (if DistCumulative exists)'),
                defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POINTS,
                self.tr('Output Translated Points Layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        """Execute the algorithm."""
        
        source_points = self.parameterAsSource(parameters, self.INPUT_SOURCE_POINTS, context)
        source_line = self.parameterAsVectorLayer(parameters, self.INPUT_SOURCE_LINE, context)
        target_line = self.parameterAsVectorLayer(parameters, self.INPUT_TARGET_LINE, context)
        include_source_kp = self.parameterAsBool(parameters, self.INCLUDE_SOURCE_KP, context)

        if source_points is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_SOURCE_POINTS))
        if source_line is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_SOURCE_LINE))
        if target_line is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_TARGET_LINE))

        # Check CRS match
        if source_points.sourceCrs() != source_line.sourceCrs():
            raise QgsProcessingException(
                self.tr('CRS Mismatch: Source Points and Source Line must use the same CRS.')
            )
        if source_line.sourceCrs() != target_line.sourceCrs():
            raise QgsProcessingException(
                self.tr('CRS Mismatch: Source Line and Target Line must use the same CRS.')
            )

        # Build output fields
        output_fields = QgsFields()
        
        # Copy all fields from source points
        for field in source_points.fields():
            output_fields.append(field)
        
        # Add new fields for translation results
        if include_source_kp:
            dist_cumul_idx = source_points.fields().lookupField('DistCumulative')
            if dist_cumul_idx < 0:
                # Field doesn't exist, we'll add it
                output_fields.append(QgsField('source_kp', QVariant.Double))
        
        output_fields.append(QgsField('translated_kp', QVariant.Double))
        output_fields.append(QgsField('spatial_offset_m', QVariant.Double))
        output_fields.append(QgsField('dcc_to_source_line', QVariant.Double))
        output_fields.append(QgsField('source_line_name', QVariant.String))
        output_fields.append(QgsField('target_line_name', QVariant.String))

        # Create output sink
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT_POINTS, context,
            output_fields,
            QgsWkbTypes.Point,
            source_points.sourceCrs()
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_POINTS))

        # Initialize RPL comparator
        try:
            comparator = RPLComparator(source_line, target_line, source_points.sourceCrs(), context)
        except Exception as e:
            raise QgsProcessingException(
                self.tr(f'Failed to initialize RPL Comparator: {str(e)}')
            )

        # Get layer names for traceability
        source_line_name = source_line.name()
        target_line_name = target_line.name()

        # Process each source point feature
        total_features = source_points.featureCount()
        features_processed = 0
        features_skipped = 0

        dist_cumul_idx = source_points.fields().lookupField('DistCumulative')
        
        for source_feature in source_points.getFeatures():
            if feedback.isCanceled():
                break

            # Get point geometry
            point_geom = source_feature.geometry()
            if point_geom.isEmpty() or point_geom.type() != QgsWkbTypes.PointGeometry:
                features_skipped += 1
                continue

            point_xy = point_geom.asPoint()

            try:
                # Translate KP from source to target
                translation = comparator.translate_kp_for_point(point_xy, dist_cumul_idx, source_feature)
                
                if translation is None:
                    features_skipped += 1
                    continue

                # Create output feature
                output_feature = QgsFeature(output_fields)
                
                # Copy geometry (point on target line)
                output_feature.setGeometry(QgsGeometry.fromPointXY(translation['target_point']))
                
                # Build attributes
                output_attrs = list(source_feature.attributes())
                
                # Add/update source_kp if requested
                if include_source_kp:
                    if dist_cumul_idx >= 0:
                        # Replace existing DistCumulative with source_kp field value
                        # It's already in the output_attrs
                        pass
                    else:
                        # Add new source_kp field
                        output_attrs.append(translation['source_kp'])
                
                # Add translation results
                output_attrs.append(round(translation['translated_kp'], 3))
                output_attrs.append(round(translation['spatial_offset_m'], 3))
                output_attrs.append(round(translation['dcc_to_source_line'], 3))
                output_attrs.append(source_line_name)
                output_attrs.append(target_line_name)
                
                output_feature.setAttributes(output_attrs)
                sink.addFeature(output_feature, QgsFeatureSink.FastInsert)
                
                features_processed += 1

            except Exception as e:
                feedback.pushWarning(
                    self.tr(f'Failed to process feature {source_feature.id()}: {str(e)}')
                )
                features_skipped += 1
                continue

            # Update progress
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
        return self.tr('RPL Comparison')

    def groupId(self):
        return 'rpl_comparison'

    def shortHelpString(self):
        return self.tr("""
<h3>Translate KP Between RPLs (Points)</h3>

<p>Translate Kilometer Point (KP) values from one RPL to another using spatial proximity matching.</p>

<h4>Use Case</h4>
<p>You have a <b>Design RPL</b> (planned cable route) and an <b>As-Laid RPL</b> (actual cable route).
Both have their own KP values. This tool finds where Design events/positions are located on the As-Laid route
and calculates their new KP values.</p>

<h4>Inputs</h4>
<ul>
  <li><b>Source Point Layer:</b> Point features with locations (e.g., Design RPL events like Repeaters, Slack Points).
      Optionally contains a <code>DistCumulative</code> field with source KP values.</li>
  <li><b>Source Line Layer:</b> The line geometry defining the source route and its chainage (e.g., Design RPL route).</li>
  <li><b>Target Line Layer:</b> The line geometry defining the target route (e.g., As-Laid RPL route).</li>
  <li><b>Include Source KP:</b> If checked, preserves the original source KP value in the output (if <code>DistCumulative</code> exists).</li>
</ul>

<h4>Outputs</h4>
<p>A new point layer with:</p>
<ul>
  <li><b>All original attributes</b> from source points.</li>
  <li><b>translated_kp:</b> The KP value at this point's nearest location on the target line.</li>
  <li><b>spatial_offset_m:</b> Distance from the source point to the target line (perpendicular).
      Larger offsets suggest the point is far from the target route (possible routing deviation).</li>
  <li><b>dcc_to_source_line:</b> Distance Cross Course (DCC) from the translated point back to the source line.
      This shows how different the routes are at this location.</li>
  <li><b>source_line_name & target_line_name:</b> Layer names for traceability.</li>
</ul>

<h4>Example Workflow</h4>
<ol>
  <li>Load Design RPL (design_points + design_lines) and As-Laid RPL (aslaid_points + aslaid_lines).</li>
  <li>Run this tool with:
    <ul>
      <li>Source Points: design_points</li>
      <li>Source Line: design_lines</li>
      <li>Target Line: aslaid_lines</li>
    </ul>
  </li>
  <li>Output: design_points projected onto aslaid_lines with new KP values.</li>
  <li>Use <code>spatial_offset_m</code> to identify large routing deviations.</li>
  <li>Use <code>dcc_to_source_line</code> to see how different the two routes are.</li>
</ol>

<h4>Accuracy Notes</h4>
<ul>
  <li>Uses geodetic (ellipsoidal) distance calculations for accuracy.</li>
  <li>Handles multi-part line geometries correctly.</li>
  <li>Spatial offset and DCC are key metrics for validating translations.</li>
  <li>Large offsets (e.g., >1 km) may indicate significant routing changes.</li>
</ul>
""")

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return TranslateKPFromRPLToRPLAlgorithm()
