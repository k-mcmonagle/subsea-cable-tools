# extract_ac_points_algorithm.py
# -*- coding: utf-8 -*-
"""
ExtractACPointsAlgorithm
This tool extracts Alter Course (A/C) points from an RPL line layer.
"""

import math
from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterString,
                       QgsProcessingParameterFeatureSink,
                       QgsFeature,
                       QgsGeometry,
                       QgsPointXY,
                       QgsFields,
                       QgsField,
                       QgsWkbTypes,
                       QgsDistanceArea,
                       QgsProcessingLayerPostProcessorInterface)

class ExtractACPointsAlgorithm(QgsProcessingAlgorithm):
    INPUT_RPL = 'INPUT_RPL'
    MIN_AC_DEG = 'MIN_AC_DEG'
    BIN_EDGES_DEG = 'BIN_EDGES_DEG'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_RPL,
                self.tr('Input RPL Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_AC_DEG,
                self.tr('Minimum absolute A/C to output (degrees)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.BIN_EDGES_DEG,
                self.tr('A/C bin edges in degrees (absolute), comma-separated (optional)'),
                defaultValue='',
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('AC Listing Output')
            )
        )

    @staticmethod
    def _parse_bin_edges(text: str):
        if not text:
            return []
        edges = []
        for part in text.split(','):
            part = part.strip()
            if not part:
                continue
            edges.append(float(part))
        # de-duplicate, sort, keep non-negative
        edges = sorted({e for e in edges if e >= 0.0})
        return edges

    @staticmethod
    def _bin_label(abs_turn_deg: float, edges):
        if not edges:
            return None
        for edge in edges:
            if abs_turn_deg < edge:
                return f"<{edge:g}"
        return f">={edges[-1]:g}"

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT_RPL, context)
        if source is None:
            return {}

        min_ac_deg = float(self.parameterAsDouble(parameters, self.MIN_AC_DEG, context) or 0.0)
        bin_edges_text = (self.parameterAsString(parameters, self.BIN_EDGES_DEG, context) or '').strip()
        try:
            bin_edges = self._parse_bin_edges(bin_edges_text)
        except Exception:
            feedback.pushWarning("Could not parse bin edges; expected comma-separated numbers (e.g. '5, 15, 30'). Binning disabled.")
            bin_edges = []

        fields = QgsFields()
        fields.append(QgsField('kp', QVariant.Double))
        fields.append(QgsField('alter_course', QVariant.Double))
        fields.append(QgsField('turn_abs', QVariant.Double))
        fields.append(QgsField('turn_bin', QVariant.String))
        fields.append(QgsField('source_layer', QVariant.String))
        
        (sink, dest_id) = self.parameterAsSink(parameters, self.OUTPUT,
                                               context, fields, QgsWkbTypes.Point, source.sourceCrs())

        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(source.sourceCrs(), context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

        # Collect all geometries and merge them into continuous lines
        # This is crucial if the input layer consists of many separate segments
        geometries = [f.geometry() for f in source.getFeatures() if f.hasGeometry()]
        if not geometries:
            return {self.OUTPUT: dest_id}

        # Combine all features into a single (multi)line geometry.
        # Prefer unaryUnion (dissolves boundaries) when available; fall back to collectGeometry.
        try:
            combined_geom = QgsGeometry.unaryUnion(geometries)
        except Exception:
            combined_geom = QgsGeometry.collectGeometry(geometries)

        # Ensure we have a line geometry before attempting line-merge.
        if combined_geom and combined_geom.type() != QgsWkbTypes.LineGeometry:
            try:
                combined_geom = combined_geom.convertToType(QgsWkbTypes.LineGeometry, True)
            except Exception:
                pass

        # QGIS 3.36 uses mergeLines(); lineMerge() is not available.
        merged_geom = combined_geom
        if merged_geom is not None:
            if hasattr(merged_geom, 'mergeLines'):
                try:
                    merged_geom = merged_geom.mergeLines()
                except Exception:
                    merged_geom = combined_geom
            elif hasattr(merged_geom, 'lineMerge'):
                try:
                    merged_geom = merged_geom.lineMerge()
                except Exception:
                    merged_geom = combined_geom
        
        if merged_geom.isEmpty():
            feedback.pushInfo("Input line layer is empty or invalid after merging.")
            return {self.OUTPUT: dest_id}

        # Extract points from the merged geometry
        if merged_geom.isMultipart():
            lines = merged_geom.asMultiPolyline()
        else:
            lines = [merged_geom.asPolyline()]

        feedback.pushInfo(f"Found {len(lines)} continuous line part(s).")
        
        total_ac_found = 0
        cumulative_dist = 0.0
        for part_idx, line in enumerate(lines):
            if len(line) < 2:
                continue
            
            feedback.pushInfo(f"Processing part {part_idx + 1} with {len(line)} vertices...")
            
            for i in range(1, len(line)):
                if feedback.isCanceled():
                    break
                
                p_prev = line[i-1]
                p_curr = line[i]
                
                # Measure segment length
                seg_len = distance_calculator.measureLine(p_prev, p_curr)

                # Skip degenerate segments (can appear after merges or from duplicated vertices)
                if seg_len <= 0.0:
                    continue
                
                # If this is not the last point of the part, we can check for an A/C
                if i < len(line) - 1:
                    p_next = line[i+1]
                    
                    # Calculate bearings in degrees
                    try:
                        # b1: bearing of the segment leading INTO the vertex
                        # b2: bearing of the segment leading OUT OF the vertex
                        if distance_calculator.measureLine(p_curr, p_next) <= 0.0:
                            cumulative_dist += seg_len
                            continue
                        b1 = math.degrees(distance_calculator.bearing(p_prev, p_curr))
                        b2 = math.degrees(distance_calculator.bearing(p_curr, p_next))
                        
                        # Calculate turn angle (relative change)
                        turn_angle = b2 - b1
                        
                        # Normalize to [-180, 180]
                        while turn_angle > 180:
                            turn_angle -= 360
                        while turn_angle <= -180:
                            turn_angle += 360
                        
                        # Only add point if there is an actual turn (angle != 0)
                        abs_turn = abs(turn_angle)
                        if abs_turn > 0.0001:
                            if abs_turn < min_ac_deg:
                                cumulative_dist += seg_len
                                continue
                            kp = (cumulative_dist + seg_len) / 1000.0
                            bin_label = self._bin_label(abs_turn, bin_edges)
                            feat = QgsFeature(fields)
                            feat.setGeometry(QgsGeometry.fromPointXY(p_curr))
                            feat.setAttributes([
                                round(kp, 4),
                                round(turn_angle, 4),
                                round(abs_turn, 4),
                                bin_label,
                                source.sourceName()
                            ])
                            sink.addFeature(feat, QgsFeatureSink.FastInsert)
                            total_ac_found += 1
                    except Exception as e:
                        feedback.pushWarning(f"Error calculating bearing at vertex {i}: {str(e)}")
                
                cumulative_dist += seg_len

        feedback.pushInfo(f"Successfully extracted {total_ac_found} A/C points.")
        
        # Set up dynamic renaming using post-processor
        input_layer_name = source.sourceName()
        self.renamer = Renamer(f"{input_layer_name}_AC_Listing")
        context.layerToLoadOnCompletionDetails(dest_id).setPostProcessor(self.renamer)

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'extract_ac_points'

    def displayName(self):
        return self.tr('Extract A/C Points from RPL')

    def group(self):
        return self.tr('RPL Tools')

    def groupId(self):
        return 'rpl_tools'

    def shortHelpString(self):
        return self.tr("""
This tool extracts Alter Course (A/C) points from an RPL line layer. 
It identifies points where the line changes direction and calculates the turn angle and KP.

You can optionally:
- Filter out small course changes by setting a minimum absolute A/C threshold (degrees).
- Write a simple bin label based on absolute turn size (e.g. '<15', '>=15') by providing comma-separated bin edges.

**Output:**
- A point layer containing the A/C points.
- 'kp' field: The Kilometre Point along the line (in km).
- 'alter_course' field: The turn angle in degrees (positive for right turns, negative for left turns).
- 'turn_abs' field: Absolute value of 'alter_course' (degrees).
- 'turn_bin' field: Optional bin label based on absolute A/C and provided bin edges.
- 'source_layer' field: The name of the input RPL layer.

The output layer is automatically named with an '_AC_Listing' suffix.
""")

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return ExtractACPointsAlgorithm()

class Renamer(QgsProcessingLayerPostProcessorInterface):
    def __init__(self, layer_name):
        self.name = layer_name
        super().__init__()

    def postProcessLayer(self, layer, context, feedback):
        layer.setName(self.name)
