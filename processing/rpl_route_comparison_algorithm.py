# rpl_route_comparison_algorithm.py
# -*- coding: utf-8 -*-
"""
RPL Route Comparison Algorithm
Compares design vs as-laid routes by calculating position offsets for matching events.

This algorithm:
1. Takes design and as-laid RPL point layers
2. Matches events between them (exact string matching, with manual review option)
3. Calculates offsets: along-track, cross-track (DCC), and radial distance
4. Outputs a line layer showing comparison results with offset fields
"""

__author__ = 'Kieran McMonagle'
__date__ = '2024-10-22'
__copyright__ = '(C) 2024 by Kieran McMonagle'

import math

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterFeatureSink,
    QgsProcessingException,
    QgsFeatureSink,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointXY,
    QgsWkbTypes,
)
from .algorithm_base import SubseaCableAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING, PROCESSING_FIELD_NUMERIC

from .rpl_comparison_utils import RPLComparator
from ..kp_range_utils import make_kp_distance_area
from ..kp_geo_utils import iter_line_parts


class RPLRouteComparisonAlgorithm(SubseaCableAlgorithm):
    """
    Compare design vs as-laid RPL routes and calculate position offsets for matching events.
    """

    # Parameter identifiers
    DESIGN_POINTS = 'DESIGN_POINTS'
    DESIGN_EVENTS_FIELD = 'DESIGN_EVENTS_FIELD'
    DESIGN_KP_FIELD = 'DESIGN_KP_FIELD'
    DESIGN_LINES = 'DESIGN_LINES'
    
    ASLAID_POINTS = 'ASLAID_POINTS'
    ASLAID_EVENTS_FIELD = 'ASLAID_EVENTS_FIELD'
    ASLAID_LINES = 'ASLAID_LINES'
    
    OUTPUT_COMPARISON = 'OUTPUT_COMPARISON'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return RPLRouteComparisonAlgorithm()

    def name(self):
        return 'rplroutecomparison'

    def displayName(self):
        return self.tr('Compare Design vs As-Laid Routes')

    def group(self):
        return self.tr('RPL Tools')

    def groupId(self):
        return 'rpl_tools'

    def shortHelpString(self):
        return self.tr("""
<h3>Compare Design vs As-Laid Routes</h3>
<p>This tool compares design and as-laid cable route positions by matching corresponding events 
and calculating position offsets.</p>

<h4>How it Works</h4>
<ol>
  <li>Takes design and as-laid RPL point layers with corresponding event identifiers</li>
  <li>Matches events between the two layers (exact string matching)</li>
  <li>For each matched event pair, calculates three offset measurements:
    <ul>
      <li><b>Along-track offset:</b> Signed distance along the design route from design event to 
          the perpendicular projection of the as-laid event
        <ul>
          <li><b>Positive (+):</b> As-laid event is ahead (further along route direction) than design</li>
          <li><b>Negative (-):</b> As-laid event is behind (earlier along route direction) than design</li>
        </ul>
      </li>
      <li><b>Cross-track offset (DCC):</b> Signed perpendicular distance from as-laid event to design route
        <ul>
          <li><b>Positive (+):</b> As-laid event is to Starboard (right) of design route when traveling forward</li>
          <li><b>Negative (-):</b> As-laid event is to Port (left) of design route when traveling forward</li>
        </ul>
      </li>
      <li><b>Radial distance:</b> Direct line distance between design and as-laid event positions (unsigned)</li>
      <li><b>Bearing:</b> Compass bearing from design to as-laid event (0-360°, 0=North)</li>
    </ul>
  </li>
  <li>Outputs a line layer with lines connecting corresponding events and offset attributes</li>
</ol>

<h4>Inputs</h4>
<ul>
  <li><b>Design RPL Points:</b> Point layer from design RPL (e.g., repeater events)</li>
  <li><b>Design Events Field:</b> Field containing event identifiers in design points</li>
  <li><b>Design KP/Distance Field (Optional):</b> Field containing cumulative distance or KP values (e.g., DistCumulative) for ordering results. If provided, this value will be included in the output layer.</li>
  <li><b>Design RPL Lines:</b> Line layer from design RPL (route path)</li>
  <li><b>As-Laid RPL Points:</b> Point layer from as-laid RPL</li>
  <li><b>As-Laid Events Field:</b> Field containing event identifiers in as-laid points</li>
  <li><b>As-Laid RPL Lines:</b> Line layer from as-laid RPL (route path)</li>
</ul>

<h4>Output</h4>
<p><b>Comparison Result:</b> A line layer with one line per matched event pair. Each line connects 
the design point to the as-laid point and includes the following attributes:
<ul>
  <li><b>design_layer:</b> Name of design points layer</li>
  <li><b>aslaid_layer:</b> Name of as-laid points layer</li>
  <li><b>design_event:</b> Design event name</li>
  <li><b>aslaid_event:</b> As-laid event name</li>
  <li><b>design_kp:</b> Design KP/cumulative distance value (if KP field provided)</li>
  <li><b>along_track_m:</b> Signed along-track offset (+ ahead, - behind)</li>
  <li><b>cross_track_m:</b> Signed cross-track offset (+ starboard, - port)</li>
  <li><b>radial_distance_m:</b> Direct distance between event positions</li>
  <li><b>bearing_deg:</b> Compass bearing from design to as-laid (0-360°)</li>
  <li><b>design_depth:</b> Design event depth (if available)</li>
  <li><b>aslaid_depth:</b> As-laid event depth (if available)</li>
  <li><b>prev_ac_distance_m:</b> Distance in meters to the previous alter course (bearing change) along the route</li>
  <li><b>next_ac_distance_m:</b> Distance in meters to the next alter course (bearing change) along the route</li>
</ul>
""")

    def initAlgorithm(self, config=None):
        # Design RPL inputs
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.DESIGN_POINTS,
                self.tr('Design RPL Points'),
                types=[QgsProcessing.TypeVectorPoint]
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.DESIGN_EVENTS_FIELD,
                self.tr('Design Events Field'),
                parentLayerParameterName=self.DESIGN_POINTS,
                type=QgsProcessingParameterField.String
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.DESIGN_KP_FIELD,
                self.tr('Design KP/Distance Field (Optional)'),
                parentLayerParameterName=self.DESIGN_POINTS,
                type=PROCESSING_FIELD_NUMERIC,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.DESIGN_LINES,
                self.tr('Design RPL Lines'),
                types=[QgsProcessing.TypeVectorLine]
            )
        )
        
        # As-laid RPL inputs
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.ASLAID_POINTS,
                self.tr('As-Laid RPL Points'),
                types=[QgsProcessing.TypeVectorPoint]
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.ASLAID_EVENTS_FIELD,
                self.tr('As-Laid Events Field'),
                parentLayerParameterName=self.ASLAID_POINTS,
                type=QgsProcessingParameterField.String
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.ASLAID_LINES,
                self.tr('As-Laid RPL Lines'),
                types=[QgsProcessing.TypeVectorLine]
            )
        )
        
        # Output
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_COMPARISON,
                self.tr('Comparison Result'),
                type=QgsProcessing.TypeVectorLine
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        """Main algorithm execution."""
        # Get input layers and parameters
        design_points_layer = self.parameterAsVectorLayer(
            parameters, self.DESIGN_POINTS, context
        )
        design_events_field = self.parameterAsString(
            parameters, self.DESIGN_EVENTS_FIELD, context
        )
        design_kp_field = self.parameterAsString(
            parameters, self.DESIGN_KP_FIELD, context
        )
        design_lines_layer = self.parameterAsVectorLayer(
            parameters, self.DESIGN_LINES, context
        )
        
        aslaid_points_layer = self.parameterAsVectorLayer(
            parameters, self.ASLAID_POINTS, context
        )
        aslaid_events_field = self.parameterAsString(
            parameters, self.ASLAID_EVENTS_FIELD, context
        )
        aslaid_lines_layer = self.parameterAsVectorLayer(
            parameters, self.ASLAID_LINES, context
        )
        
        # Validate inputs
        if design_points_layer is None or design_lines_layer is None:
            raise QgsProcessingException(
                self.tr('Design RPL layers not provided')
            )
        if aslaid_points_layer is None or aslaid_lines_layer is None:
            raise QgsProcessingException(
                self.tr('As-Laid RPL layers not provided')
            )
        
        # Get field indices
        design_event_idx = design_points_layer.fields().lookupField(design_events_field)
        aslaid_event_idx = aslaid_points_layer.fields().lookupField(aslaid_events_field)
        design_kp_idx = -1
        
        # Handle optional KP field
        if design_kp_field:
            design_kp_idx = design_points_layer.fields().lookupField(design_kp_field)
            if design_kp_idx < 0:
                feedback.pushWarning(
                    self.tr(f'Design KP field "{design_kp_field}" not found, will skip KP values')
                )
        
        if design_event_idx < 0:
            raise QgsProcessingException(
                self.tr(f'Design events field "{design_events_field}" not found')
            )
        if aslaid_event_idx < 0:
            raise QgsProcessingException(
                self.tr(f'As-Laid events field "{aslaid_events_field}" not found')
            )
        
        # Create CRS (use design CRS)
        crs = design_points_layer.crs()
        
        # Create output fields
        output_fields = QgsFields()
        output_fields.append(QgsField('design_layer', FIELD_TYPE_STRING))
        output_fields.append(QgsField('aslaid_layer', FIELD_TYPE_STRING))
        output_fields.append(QgsField('design_event', FIELD_TYPE_STRING))
        output_fields.append(QgsField('aslaid_event', FIELD_TYPE_STRING))
        output_fields.append(QgsField('design_kp', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('along_track_m', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('cross_track_m', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('radial_distance_m', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('bearing_deg', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('design_depth', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('aslaid_depth', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('prev_ac_distance_m', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('next_ac_distance_m', FIELD_TYPE_DOUBLE))
        
        # Create output sink
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT_COMPARISON, context,
            output_fields, QgsWkbTypes.LineString, crs
        )
        if sink is None:
            raise QgsProcessingException(self.tr('Failed to create output layer'))
        
        # Step 1: Extract and match events
        feedback.pushInfo('Matching events between design and as-laid RPLs...')
        design_events_data = self._extract_events(design_points_layer, design_event_idx)
        aslaid_events_data = self._extract_events(aslaid_points_layer, aslaid_event_idx)
        
        design_events = design_events_data['events']
        aslaid_events = aslaid_events_data['events']
        
        match_result = self._match_events(design_events_data, aslaid_events_data)
        matches = match_result['matches']
        duplicates_design = match_result['duplicates_design']
        duplicates_aslaid = match_result['duplicates_aslaid']
        unmatched_design = match_result['unmatched_design']
        unmatched_aslaid = match_result['unmatched_aslaid']
        
        # Report duplicates
        if duplicates_design:
            feedback.pushWarning(f'Design layer has duplicate events (case/whitespace variants): {", ".join(duplicates_design)} - omitting')
        if duplicates_aslaid:
            feedback.pushWarning(f'As-laid layer has duplicate events (case/whitespace variants): {", ".join(duplicates_aslaid)} - omitting')
        
        # Report unmatched
        if unmatched_design:
            feedback.pushWarning(f'Unmatched design events: {", ".join(unmatched_design)}')
        if unmatched_aslaid:
            feedback.pushWarning(f'Unmatched as-laid events: {", ".join(unmatched_aslaid)}')
        
        feedback.pushInfo(f'Matched {len(matches)} events (after duplicate/case-insensitive normalization)')
        
        # Step 2: Initialize comparator
        feedback.pushInfo('Initializing route comparator...')
        comparator = RPLComparator(design_lines_layer, aslaid_lines_layer, crs, context)
        
        # Step 3: Calculate offsets for each match
        feedback.pushInfo('Calculating offsets for matched events...')
        # Use the shared helper so the WGS84 fallback is applied when the
        # project ellipsoid is unset (matches every other KP-emitting tool).
        try:
            distance_calc = make_kp_distance_area(
                crs, context.transformContext(), project=context.project()
            )
        except ValueError as exc:
            raise QgsProcessingException(str(exc))
        feedback.pushInfo(f'Using ellipsoid: {distance_calc.ellipsoid()}')
        
        # Step 2.5: Extract alter course KPs from design route
        feedback.pushInfo('Extracting alter courses from design route...')
        ac_kps = self._extract_ac_kps(comparator, design_lines_layer, distance_calc, feedback)
        feedback.pushInfo(f'Detected {len(ac_kps)} alter courses in design route')
        
        total = len(matches)
        for idx, match in enumerate(matches):
            if feedback.isCanceled():
                break
            
            design_event_name = match['design']
            aslaid_event_name = match['aslaid']
            design_feature = design_events[design_event_name]['feature']
            aslaid_feature = aslaid_events[aslaid_event_name]['feature']
            
            design_point = design_feature.geometry().asPoint()
            aslaid_point = aslaid_feature.geometry().asPoint()

            design_point_xy = QgsPointXY(design_point.x(), design_point.y())
            aslaid_point_xy = QgsPointXY(aslaid_point.x(), aslaid_point.y())

            # Calculate offsets (KP of the design point comes with them)
            offsets = self._calculate_offsets(
                design_point_xy, aslaid_point_xy, comparator, distance_calc
            )
            design_kp = offsets['design_kp']

            # Calculate AC proximities (exclude ACs at the same KP as the design point)
            prev_ac_kp = max((k for k in ac_kps if k < design_kp), default=None)
            next_ac_kp = min((k for k in ac_kps if k > design_kp), default=None)
            prev_ac_distance = (design_kp - prev_ac_kp) * 1000 if prev_ac_kp is not None else None
            next_ac_distance = (next_ac_kp - design_kp) * 1000 if next_ac_kp is not None else None

            # Create output feature (line)
            line = QgsLineString([design_point, aslaid_point])
            output_feature = QgsFeature(output_fields)
            output_feature.setGeometry(QgsGeometry(line))
            output_feature['design_layer'] = design_points_layer.name()
            output_feature['aslaid_layer'] = aslaid_points_layer.name()
            output_feature['design_event'] = design_event_name
            output_feature['aslaid_event'] = aslaid_event_name
            
            # Add design KP if field was provided
            if design_kp_idx >= 0:
                output_feature['design_kp'] = design_feature[design_kp_idx]
            
            output_feature['along_track_m'] = offsets['along_track']
            output_feature['cross_track_m'] = offsets['cross_track']
            output_feature['radial_distance_m'] = offsets['radial_distance']
            output_feature['bearing_deg'] = offsets['bearing']
            output_feature['design_depth'] = design_feature['ApproxDepth'] if 'ApproxDepth' in [f.name() for f in design_feature.fields()] else None
            output_feature['aslaid_depth'] = aslaid_feature['ApproxDepth'] if 'ApproxDepth' in [f.name() for f in aslaid_feature.fields()] else None
            output_feature['prev_ac_distance_m'] = prev_ac_distance
            output_feature['next_ac_distance_m'] = next_ac_distance
            
            sink.addFeature(output_feature, QgsFeatureSink.FastInsert)
            
            feedback.setProgress(int((idx + 1) / total * 100))
        
        feedback.pushInfo(f'Comparison complete. Output: {dest_id}')
        return {self.OUTPUT_COMPARISON: dest_id}

    def _normalize_event_name(self, name):
        """
        Normalize event name for matching: lowercase and strip whitespace.
        
        Args:
            name: Event name (string)
        
        Returns: Normalized name (lowercase, stripped whitespace)
        """
        if not name:
            return ""
        return str(name).strip().lower()

    def _extract_events(self, layer, event_field_idx):
        """
        Extract event features from a point layer with duplicate detection.
        
        Returns dict: {
            'events': {original_name: {'feature': QgsFeature, 'geometry': QgsPointXY}},
            'normalized_map': {normalized_name: [original_names]},  # Track duplicates
            'duplicates': [list of normalized names with multiple features]
        }
        """
        events = {}
        normalized_map = {}  # normalized_name -> list of original names
        duplicates = []
        
        for feature in layer.getFeatures():
            if feature.geometry().isEmpty():
                continue
            
            event_name = feature[event_field_idx]
            if not event_name:
                continue
            
            event_name_str = str(event_name)
            normalized_name = self._normalize_event_name(event_name_str)
            
            # Store feature with original name
            events[event_name_str] = {
                'feature': feature,
                'geometry': feature.geometry().asPoint(),
                'normalized': normalized_name
            }
            
            # Track normalized names to detect duplicates
            if normalized_name not in normalized_map:
                normalized_map[normalized_name] = []
            normalized_map[normalized_name].append(event_name_str)
        
        # Identify duplicates (normalized names with multiple original names)
        for normalized_name, original_names in normalized_map.items():
            if len(original_names) > 1:
                duplicates.append(normalized_name)
        
        return {
            'events': events,
            'normalized_map': normalized_map,
            'duplicates': duplicates
        }

    def _match_events(self, design_events_data, aslaid_events_data):
        """
        Match events between design and as-laid using normalized matching.
        Omits matches where either side has duplicates.
        
        Args:
            design_events_data: Output from _extract_events (design layer)
            aslaid_events_data: Output from _extract_events (as-laid layer)
        
        Returns dict: {
            'matches': [{'design': original_name, 'aslaid': original_name}, ...],
            'duplicates_design': [list of normalized names with duplicates in design],
            'duplicates_aslaid': [list of normalized names with duplicates in as-laid],
            'unmatched_design': [list of original names with no match],
            'unmatched_aslaid': [list of original names with no match]
        }
        """
        design_events = design_events_data['events']
        design_normalized_map = design_events_data['normalized_map']
        design_duplicates = design_events_data['duplicates']
        
        aslaid_events = aslaid_events_data['events']
        aslaid_normalized_map = aslaid_events_data['normalized_map']
        aslaid_duplicates = aslaid_events_data['duplicates']
        
        matches = []
        matched_design = set()
        matched_aslaid = set()
        
        # Find matches based on normalized names
        design_normalized_set = set(design_normalized_map.keys())
        aslaid_normalized_set = set(aslaid_normalized_map.keys())
        matching_normalized = design_normalized_set & aslaid_normalized_set
        
        for normalized_name in matching_normalized:
            # Skip if either side has duplicates for this name
            if normalized_name in design_duplicates or normalized_name in aslaid_duplicates:
                continue
            
            # Get the single original names (duplicates already filtered)
            design_original_names = design_normalized_map[normalized_name]
            aslaid_original_names = aslaid_normalized_map[normalized_name]
            
            if len(design_original_names) == 1 and len(aslaid_original_names) == 1:
                design_name = design_original_names[0]
                aslaid_name = aslaid_original_names[0]
                matches.append({'design': design_name, 'aslaid': aslaid_name})
                matched_design.add(design_name)
                matched_aslaid.add(aslaid_name)
        
        # Identify unmatched events
        unmatched_design = set(design_events.keys()) - matched_design
        unmatched_aslaid = set(aslaid_events.keys()) - matched_aslaid
        
        return {
            'matches': matches,
            'duplicates_design': design_duplicates,
            'duplicates_aslaid': aslaid_duplicates,
            'unmatched_design': unmatched_design,
            'unmatched_aslaid': unmatched_aslaid
        }

    def _measure_distance(self, point1, point2, distance_calc):
        """
        Measure distance between two QgsPointXY objects in meters.
        
        Uses QgsDistanceArea.measureLine() which:
        - Returns meters when ellipsoid is properly configured
        - Handles geographic CRS correctly via the ellipsoid
        - Works with both geographic and projected CRS
        
        Args:
            point1: QgsPointXY
            point2: QgsPointXY
            distance_calc: QgsDistanceArea (must have CRS and ellipsoid set)
        
        Returns: Distance in meters (float)
        """
        # measureLine returns distance in the units of the CRS
        # With ellipsoid set, it returns meters
        distance = distance_calc.measureLine(point1, point2)
        return distance

    def _calculate_offsets(self, design_point, aslaid_point, comparator, distance_calc):
        """
        Calculate along-track, cross-track, and radial distance offsets.

        Both along- and cross-track come from the design route's
        ``RouteFrame`` (via ``comparator``): the same KP definition as every
        other KP tool, so they agree with Nearest KP / the KP Mouse tool.

        Args:
            design_point: QgsPointXY - design event location
            aslaid_point: QgsPointXY - as-laid event location
            comparator: RPLComparator - design route is its source line
            distance_calc: QgsDistanceArea - distance calculator

        Returns dict with 'design_kp', 'along_track', 'cross_track',
        'radial_distance', 'bearing'
        - design_kp: KP (km) of the design event on the design route
        - along_track: signed distance in meters along the design route from
          the design event's projection to the as-laid event's projection
          (+ ahead, - behind)
        - cross_track: signed perpendicular distance in meters from the
          as-laid event to the design route (+ starboard, - port)
        - radial_distance: direct distance in meters
        - bearing: compass bearing from design to as-laid in degrees (0-360)
        """
        # Radial distance: direct distance between points (using helper to ensure meters)
        radial = self._measure_distance(design_point, aslaid_point, distance_calc)

        design_hit = comparator.nearest_kp_hit(design_point, source=True)
        aslaid_hit, cross_track = comparator.signed_offset(aslaid_point, source=True)
        if design_hit.snapped_xy is None or aslaid_hit.snapped_xy is None:
            design_kp, along_track, cross_track = 0.0, 0.0, 0.0
        else:
            design_kp = design_hit.kp_km
            along_track = (aslaid_hit.kp_km - design_hit.kp_km) * 1000.0

        # Bearing: compass bearing from design point to aslaid point (0-360 degrees)
        bearing = self._calculate_bearing(design_point, aslaid_point)

        return {
            'design_kp': design_kp,
            'radial_distance': radial,
            'cross_track': cross_track,
            'along_track': along_track,
            'bearing': bearing
        }

    def _calculate_bearing(self, from_point, to_point):
        """
        Calculate compass bearing from one point to another.
        
        Args:
            from_point: QgsPointXY - starting point
            to_point: QgsPointXY - destination point
        
        Returns: Bearing in degrees (0-360, where 0=North, 90=East, 180=South, 270=West)
        """
        import math
        
        # Calculate differences in coordinates
        delta_lon = to_point.x() - from_point.x()
        delta_lat = to_point.y() - from_point.y()
        
        # Calculate bearing using atan2
        # Note: In geographic coordinates, X=longitude, Y=latitude
        # Bearing formula: atan2(east, north) = atan2(delta_lon, delta_lat)
        radians = math.atan2(delta_lon, delta_lat)
        
        # Convert to degrees
        bearing = math.degrees(radians)
        
        # Normalize to 0-360 range
        if bearing < 0:
            bearing += 360.0
        
        return bearing

    def _extract_ac_kps(self, comparator, lines_layer, distance_calc, feedback):
        """
        Extract alter course (AC) KP values from the design route.
        ACs are points where the bearing changes significantly.
        
        Args:
            comparator: RPLComparator instance
            lines_layer: Design lines layer
            distance_calc: QgsDistanceArea for bearing calculations
            feedback: For debug output
        
        Returns:
            List of KP values (in km) for alter courses, sorted
        """
        ac_kps = set()  # Use set to avoid duplicates
        
        feedback.pushInfo(f'Collected {len(comparator.source_geoms)} line geometries from design route')

        # Sort geometries by KP of their starting point
        geom_list = []
        for geom in comparator.source_geoms:
            parts = [part for part in iter_line_parts(geom) if len(part) >= 2]
            if not parts:
                continue
            # First and last vertex of the leg (multi-part legs span all parts).
            first_point = QgsPointXY(parts[0][0])
            last_point = QgsPointXY(parts[-1][-1])
            kp = comparator.calculate_kp_to_point(first_point, source=True)
            geom_list.append((kp, first_point, last_point))

        geom_list.sort(key=lambda x: x[0])  # Sort by start KP

        feedback.pushInfo(f'Sorted {len(geom_list)} geometries by start KP')

        # Check bearing changes between consecutive segments
        for i in range(len(geom_list) - 1):
            kp1, p1, p2 = geom_list[i]
            kp2, p3, p4 = geom_list[i+1]

            # Get bearing of first segment
            bearing1 = distance_calc.bearing(p1, p2)

            # Get bearing of second segment
            bearing2 = distance_calc.bearing(p3, p4)

            # Junction point KP (use end of first segment)
            junction_kp = comparator.calculate_kp_to_point(QgsPointXY(p2.x(), p2.y()), source=True)
            
            # Calculate bearing difference
            bearing_diff = abs(bearing1 - bearing2)
            bearing_diff = min(bearing_diff, 2 * math.pi - bearing_diff)
            
            # Debug: show first few
            if i < 3:
                feedback.pushInfo(f'Segment {i}: bearing1={math.degrees(bearing1):.1f}°, bearing2={math.degrees(bearing2):.1f}°, diff={math.degrees(bearing_diff):.3f}°')
            
            # Check for significant bearing change (more than 2 degrees)
            if bearing_diff > 0.0349:
                ac_kps.add(junction_kp)
        
        return sorted(list(ac_kps))

