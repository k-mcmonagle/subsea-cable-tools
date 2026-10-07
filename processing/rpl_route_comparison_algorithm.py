# rpl_route_comparison_algorithm.py
# -*- coding: utf-8 -*-
"""
RPL Route Comparison Algorithm
Compares design vs as-laid routes by calculating position offsets for matching events.

This algorithm:
1. Takes design and as-laid RPL point and line layers
2. Pairs events between them with the workbench's event matcher
   (workbench.event_compare): exact names, similar names (typos, appended
   notes), same type nearby, in route order — or exact names only
3. Calculates offsets on the design route: along-track, cross-track (DCC),
   radial distance and bearing, plus KP differences
4. Outputs a line layer (design -> as-laid per pair) and, optionally, the
   same HTML report the workbench produces (radial plots per event)
"""

__author__ = 'Kieran McMonagle'
__date__ = '2024-10-22'
__copyright__ = '(C) 2024 by Kieran McMonagle'

import math

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProcessing,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingException,
    QgsFeatureSink,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsLineString,
    QgsPointXY,
    QgsWkbTypes,
)
from .algorithm_base import SubseaCableAlgorithm
from ..qgis_compat import (
    FIELD_TYPE_DOUBLE,
    FIELD_TYPE_STRING,
    PROCESSING_FIELD_NUMERIC,
    PROCESSING_NUMBER_DOUBLE,
)

from .rpl_comparison_utils import RPLComparator
from ..kp_range_utils import make_kp_distance_area
from ..kp_geo_utils import iter_line_parts
from ..workbench import event_compare as ec
from ..workbench import event_compare_report as ec_report

MATCH_SMART = 0
MATCH_EXACT = 1


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
    ASLAID_KP_FIELD = 'ASLAID_KP_FIELD'
    ASLAID_LINES = 'ASLAID_LINES'

    MATCH_MODE = 'MATCH_MODE'
    SEARCH_RADIUS = 'SEARCH_RADIUS'
    EVENT_PRESET = 'EVENT_PRESET'
    EVENT_FILTER = 'EVENT_FILTER'
    TARGET_RADIUS = 'TARGET_RADIUS'

    OUTPUT_COMPARISON = 'OUTPUT_COMPARISON'
    OUTPUT_REPORT = 'OUTPUT_REPORT'

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
<p>Pairs the events of a design RPL with the events of an as-laid RPL (or any two RPLs) and
measures where each event ended up relative to the design route.</p>

<h4>Event matching</h4>
<p><b>Smart (default):</b> as-laid event names often differ from the design ("RPT 12" vs
"Repeater R12 S/N 4432", typos, appended notes). Pairs are found from:
<ul>
  <li><b>Exact names</b> (ignoring case, spaces and punctuation), used as anchors;</li>
  <li><b>Similar names</b>: typos and extra text are tolerated, but a different number is not
      (RPT 12 never pairs with RPT 13);</li>
  <li><b>Same type nearby</b>: the event rules classify both sides (repeater, BU, joint, alter
      course, ...) and a repeater only ever pairs with a repeater, within the search radius.</li>
</ul>
Pairs keep route order, so an extra or missing event does not shift every later pair, and an
as-laid RPL recorded in the opposite direction is detected. Each output feature records how it
was matched (<code>match_method</code>) and a confidence (<code>match_score</code>): check the
<i>fuzzy</i> and <i>position</i> pairs. To review and correct pairs interactively, use the Cable
Route Workbench: <i>Compare RPLs &gt; Events</i>.</p>
<p><b>Exact names only:</b> the previous behaviour &mdash; only names that are identical (ignoring
case, spaces and punctuation) and unique on both sides are paired.</p>

<h4>Offsets (all in metres, on the ellipsoid)</h4>
<ul>
  <li><b>along_track_m:</b> along the design route from the design event to the as-laid event's
      projection; + ahead (towards increasing KP), &minus; behind.</li>
  <li><b>cross_track_m:</b> perpendicular distance from the design route; + starboard (right when
      travelling forward), &minus; port.</li>
  <li><b>radial_distance_m / bearing_deg:</b> straight distance and true bearing (0&ndash;360&deg;)
      from design to as-laid.</li>
  <li><b>kp_delta_m:</b> as-laid KP minus design KP (each RPL's own chainage).</li>
</ul>

<h4>Inputs</h4>
<ul>
  <li><b>Design / As-laid RPL Points and Lines</b> with their <b>Events</b> fields.</li>
  <li><b>KP fields (optional):</b> each RPL's own KP (e.g. DistCumulative, km). Without them the KP
      is measured along that RPL's route line.</li>
  <li><b>Events to report:</b> a type preset (repeaters, cable bodies, transitions, everything but
      alter courses, ...) and/or a text/regex filter. Matching always uses every event; the
      filters only choose what is output.</li>
  <li><b>Search radius:</b> how far apart differently named events may be and still be paired
      (similar names may reach ten times this).</li>
  <li><b>Target radius (optional):</b> events within this radial distance count as on target.</li>
  <li><b>Report (optional):</b> an HTML report with summary statistics, a radial plot of all
      reported events, offsets along the route and one radial plot per event.</li>
</ul>

<h4>Output</h4>
<p>A line layer with one line per pair, from the design to the as-laid event, with
<code>design_layer, aslaid_layer, design_event, aslaid_event, design_kp, along_track_m,
cross_track_m, radial_distance_m, bearing_deg, design_depth, aslaid_depth, prev_ac_distance_m,
next_ac_distance_m</code> and the newer <code>aslaid_kp, kp_delta_m, match_method, match_score,
event_type, within_target</code>.</p>
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
            QgsProcessingParameterField(
                self.ASLAID_KP_FIELD,
                self.tr('As-Laid KP/Distance Field (Optional)'),
                parentLayerParameterName=self.ASLAID_POINTS,
                type=PROCESSING_FIELD_NUMERIC,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.ASLAID_LINES,
                self.tr('As-Laid RPL Lines'),
                types=[QgsProcessing.TypeVectorLine]
            )
        )

        # Matching and selection
        self.addParameter(QgsProcessingParameterEnum(
            self.MATCH_MODE, self.tr('Event matching'),
            options=[self.tr('Smart: exact, similar names and same type nearby, in route order'),
                     self.tr('Exact names only')],
            defaultValue=MATCH_SMART,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.SEARCH_RADIUS, self.tr('Search radius for differently named events (m)'),
            type=PROCESSING_NUMBER_DOUBLE, minValue=1.0,
            defaultValue=ec.DEFAULT_SEARCH_RADIUS_M,
        ))
        self.addParameter(QgsProcessingParameterEnum(
            self.EVENT_PRESET, self.tr('Events to report'),
            options=[self.tr(label) for _key, label, _inc, _exc in ec.FILTER_PRESETS],
            defaultValue=0,
        ))
        self.addParameter(QgsProcessingParameterString(
            self.EVENT_FILTER, self.tr('Event text filter (text or regex, optional)'),
            optional=True,
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.TARGET_RADIUS, self.tr('Target radius (m, 0 = none)'),
            type=PROCESSING_NUMBER_DOUBLE, minValue=0.0, defaultValue=0.0,
        ))

        # Outputs
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_COMPARISON,
                self.tr('Comparison Result'),
                type=QgsProcessing.TypeVectorLine
            )
        )
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_REPORT, self.tr('Comparison report (HTML)'),
            fileFilter=self.tr('HTML files (*.html)'), optional=True,
            createByDefault=False,
        ))

    def processAlgorithm(self, parameters, context, feedback):
        """Main algorithm execution."""
        design_points_layer = self.parameterAsVectorLayer(parameters, self.DESIGN_POINTS, context)
        design_events_field = self.parameterAsString(parameters, self.DESIGN_EVENTS_FIELD, context)
        design_kp_field = self.parameterAsString(parameters, self.DESIGN_KP_FIELD, context)
        design_lines_layer = self.parameterAsVectorLayer(parameters, self.DESIGN_LINES, context)

        aslaid_points_layer = self.parameterAsVectorLayer(parameters, self.ASLAID_POINTS, context)
        aslaid_events_field = self.parameterAsString(parameters, self.ASLAID_EVENTS_FIELD, context)
        aslaid_kp_field = self.parameterAsString(parameters, self.ASLAID_KP_FIELD, context)
        aslaid_lines_layer = self.parameterAsVectorLayer(parameters, self.ASLAID_LINES, context)

        match_mode = self.parameterAsEnum(parameters, self.MATCH_MODE, context)
        search_radius = self.parameterAsDouble(parameters, self.SEARCH_RADIUS, context) \
            or ec.DEFAULT_SEARCH_RADIUS_M
        preset_index = self.parameterAsEnum(parameters, self.EVENT_PRESET, context)
        text_filter = (self.parameterAsString(parameters, self.EVENT_FILTER, context) or '').strip()
        target_radius = self.parameterAsDouble(parameters, self.TARGET_RADIUS, context) or None
        report_path = self.parameterAsFileOutput(parameters, self.OUTPUT_REPORT, context) \
            if parameters.get(self.OUTPUT_REPORT) else ''

        # Validate inputs
        if design_points_layer is None or design_lines_layer is None:
            raise QgsProcessingException(self.tr('Design RPL layers not provided'))
        if aslaid_points_layer is None or aslaid_lines_layer is None:
            raise QgsProcessingException(self.tr('As-Laid RPL layers not provided'))

        design_event_idx = design_points_layer.fields().lookupField(design_events_field)
        aslaid_event_idx = aslaid_points_layer.fields().lookupField(aslaid_events_field)
        if design_event_idx < 0:
            raise QgsProcessingException(
                self.tr(f'Design events field "{design_events_field}" not found'))
        if aslaid_event_idx < 0:
            raise QgsProcessingException(
                self.tr(f'As-Laid events field "{aslaid_events_field}" not found'))
        design_kp_idx = self._optional_field(design_points_layer, design_kp_field, 'Design', feedback)
        aslaid_kp_idx = self._optional_field(aslaid_points_layer, aslaid_kp_field, 'As-laid', feedback)

        # All offsets are measured in the design CRS.
        crs = design_points_layer.crs()
        for layer in (design_lines_layer, aslaid_points_layer, aslaid_lines_layer):
            if layer.crs() != crs:
                feedback.pushWarning(
                    f'{layer.name()} is in {layer.crs().authid()} but the design points are in '
                    f'{crs.authid()}; points are reprojected, route lines are assumed to share '
                    'the design CRS.')

        output_fields = QgsFields()
        for name, field_type in (
                ('design_layer', FIELD_TYPE_STRING), ('aslaid_layer', FIELD_TYPE_STRING),
                ('design_event', FIELD_TYPE_STRING), ('aslaid_event', FIELD_TYPE_STRING),
                ('design_kp', FIELD_TYPE_DOUBLE), ('along_track_m', FIELD_TYPE_DOUBLE),
                ('cross_track_m', FIELD_TYPE_DOUBLE), ('radial_distance_m', FIELD_TYPE_DOUBLE),
                ('bearing_deg', FIELD_TYPE_DOUBLE), ('design_depth', FIELD_TYPE_DOUBLE),
                ('aslaid_depth', FIELD_TYPE_DOUBLE), ('prev_ac_distance_m', FIELD_TYPE_DOUBLE),
                ('next_ac_distance_m', FIELD_TYPE_DOUBLE), ('aslaid_kp', FIELD_TYPE_DOUBLE),
                ('kp_delta_m', FIELD_TYPE_DOUBLE), ('match_method', FIELD_TYPE_STRING),
                ('match_score', FIELD_TYPE_DOUBLE), ('event_type', FIELD_TYPE_STRING),
                ('within_target', FIELD_TYPE_STRING)):
            output_fields.append(QgsField(name, field_type))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT_COMPARISON, context,
            output_fields, QgsWkbTypes.LineString, crs
        )
        if sink is None:
            raise QgsProcessingException(self.tr('Failed to create output layer'))

        feedback.pushInfo('Initializing route comparator...')
        comparator = RPLComparator(design_lines_layer, aslaid_lines_layer, crs, context)
        try:
            distance_calc = make_kp_distance_area(
                crs, context.transformContext(), project=context.project()
            )
        except ValueError as exc:
            raise QgsProcessingException(str(exc))
        feedback.pushInfo(f'Using ellipsoid: {distance_calc.ellipsoid()}')

        # Step 1: read events (route order) and pair them.
        to_design = QgsCoordinateTransform(aslaid_points_layer.crs(), crs, context.transformContext())
        to_wgs84 = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem('EPSG:4326'),
                                          context.transformContext())
        design_events, design_features = self._read_events(
            design_points_layer, design_event_idx, design_kp_idx, comparator, True,
            None, to_wgs84)
        aslaid_events, aslaid_features = self._read_events(
            aslaid_points_layer, aslaid_event_idx, aslaid_kp_idx, comparator, False,
            to_design, to_wgs84)
        feedback.pushInfo(
            f'{len(design_events)} design and {len(aslaid_events)} as-laid events read.')

        mapping = self._pair_events(design_events, aslaid_events, match_mode, search_radius, feedback)
        if mapping.reversed:
            feedback.pushWarning(
                'The as-laid RPL runs in the opposite direction to the design: kp_delta_m '
                'is left empty.')

        # Step 2: alter courses on the design route (for AC proximity fields).
        feedback.pushInfo('Extracting alter courses from design route...')
        ac_kps = self._extract_ac_kps(comparator, design_lines_layer, distance_calc, feedback)
        feedback.pushInfo(f'Detected {len(ac_kps)} alter courses in design route')

        # Step 3: offsets for every pair; the event filter chooses what is output.
        preset_key = ec.FILTER_PRESETS[preset_index][0] if 0 <= preset_index < len(
            ec.FILTER_PRESETS) else 'all'
        event_filter = ec.EventFilter.preset(preset_key, text=text_filter, show_unmatched=True)
        report_rows = []
        pairs = sorted(mapping.pairs.items())
        written = 0
        for step, (design_index, (aslaid_index, how, score)) in enumerate(pairs):
            if feedback.isCanceled():
                break
            design_event = design_events[design_index]
            aslaid_event = aslaid_events[aslaid_index]
            design_point = design_features[design_index]['point']
            aslaid_point = aslaid_features[aslaid_index]['point']

            offsets = self._calculate_offsets(design_point, aslaid_point, comparator, distance_calc)
            design_route_kp = offsets['design_kp']
            east = north = None
            if offsets['bearing'] is not None:
                east = offsets['radial_distance'] * math.sin(math.radians(offsets['bearing']))
                north = offsets['radial_distance'] * math.cos(math.radians(offsets['bearing']))
            kp_delta = None
            if not mapping.reversed and design_event.kp is not None and aslaid_event.kp is not None:
                kp_delta = (aslaid_event.kp - design_event.kp) * 1000.0
            depth_delta = None
            if design_event.depth is not None and aslaid_event.depth is not None:
                depth_delta = aslaid_event.depth - design_event.depth
            row = ec.EventOffset(
                a=design_event, b=aslaid_event, how=how, score=score,
                along_m=offsets['along_track'], cross_m=offsets['cross_track'],
                radial_m=offsets['radial_distance'], bearing_deg=offsets['bearing'],
                east_m=east, north_m=north, kp_delta_m=kp_delta, depth_delta_m=depth_delta)
            if not event_filter.accepts(row):
                continue
            report_rows.append(row)

            prev_ac_kp = max((k for k in ac_kps if k < design_route_kp), default=None)
            next_ac_kp = min((k for k in ac_kps if k > design_route_kp), default=None)
            within = row.within(target_radius)

            output_feature = QgsFeature(output_fields)
            output_feature.setGeometry(QgsGeometry(QgsLineString(
                [QgsPointXY(design_point), QgsPointXY(aslaid_point)])))
            output_feature['design_layer'] = design_points_layer.name()
            output_feature['aslaid_layer'] = aslaid_points_layer.name()
            output_feature['design_event'] = design_event.event
            output_feature['aslaid_event'] = aslaid_event.event
            if design_kp_idx >= 0:
                output_feature['design_kp'] = design_features[design_index]['feature'][design_kp_idx]
            output_feature['along_track_m'] = offsets['along_track']
            output_feature['cross_track_m'] = offsets['cross_track']
            output_feature['radial_distance_m'] = offsets['radial_distance']
            output_feature['bearing_deg'] = offsets['bearing']
            output_feature['design_depth'] = design_features[design_index]['depth']
            output_feature['aslaid_depth'] = aslaid_features[aslaid_index]['depth']
            output_feature['prev_ac_distance_m'] = (
                (design_route_kp - prev_ac_kp) * 1000 if prev_ac_kp is not None else None)
            output_feature['next_ac_distance_m'] = (
                (next_ac_kp - design_route_kp) * 1000 if next_ac_kp is not None else None)
            output_feature['aslaid_kp'] = aslaid_event.kp
            output_feature['kp_delta_m'] = kp_delta
            output_feature['match_method'] = how
            output_feature['match_score'] = score
            output_feature['event_type'] = design_event.type_text
            output_feature['within_target'] = None if within is None else ('yes' if within else 'no')
            sink.addFeature(output_feature, QgsFeatureSink.FastInsert)
            written += 1
            feedback.setProgress(int((step + 1) / max(1, len(pairs)) * 100))

        # Unmatched events in the selection, for the log and the report.
        unmatched_design = [e for i, e in enumerate(design_events) if i not in mapping.pairs]
        used_aslaid = {b for b, _h, _s in mapping.pairs.values()}
        unmatched_aslaid = [e for i, e in enumerate(aslaid_events) if i not in used_aslaid]
        for event in unmatched_design:
            row = ec.EventOffset(a=event, b=None)
            if event_filter.accepts(row):
                report_rows.append(row)
        for event in unmatched_aslaid:
            row = ec.EventOffset(a=None, b=event)
            if event_filter.accepts(row):
                report_rows.append(row)
        self._report_unmatched(unmatched_design, unmatched_aslaid, event_filter, feedback)

        stats = ec.offset_stats(report_rows, target_radius)
        feedback.pushInfo(ec.summary_text(stats))
        counts = mapping.counts()
        feedback.pushInfo(
            f"Pairs: {counts.get(ec.HOW_EXACT, 0)} exact, {counts.get(ec.HOW_FUZZY, 0)} similar "
            f"name, {counts.get(ec.HOW_POSITION, 0)} same type nearby; {written} written after "
            "the event filter.")
        if counts.get(ec.HOW_FUZZY, 0) or counts.get(ec.HOW_POSITION, 0):
            feedback.pushWarning(
                'Some pairs were matched by similar name or by position: check rows whose '
                'match_method is "fuzzy" or "position".')

        results = {self.OUTPUT_COMPARISON: dest_id}
        if report_path:
            report_rows.sort(key=ec._route_order_key)
            page = ec_report.html_report(
                report_rows, design_points_layer.name(), aslaid_points_layer.name(),
                target_radius_m=target_radius,
                selection_text=self._selection_text(preset_index, text_filter),
                a_detail='design', b_detail='as-laid', reversed_b=mapping.reversed)
            try:
                with open(report_path, 'w', encoding='utf-8') as handle:
                    handle.write(page)
            except OSError as exc:
                raise QgsProcessingException(f'Could not write the report: {exc}')
            feedback.pushInfo(f'Report written to {report_path}')
            results[self.OUTPUT_REPORT] = report_path
        feedback.pushInfo(f'Comparison complete. Output: {dest_id}')
        return results

    # ----------------------------------------------------------- helpers --
    def _optional_field(self, layer, name, label, feedback):
        if not name:
            return -1
        index = layer.fields().lookupField(name)
        if index < 0:
            feedback.pushWarning(self.tr(f'{label} KP field "{name}" not found, will skip KP values'))
        return index

    @staticmethod
    def _value(feature, index):
        if index < 0:
            return None
        value = feature[index]
        if type(value).__name__ == 'QVariant':
            value = None if not value.isValid() or value.isNull() else value.value()
        return value

    def _read_events(self, layer, event_idx, kp_idx, comparator, design, to_design, to_wgs84):
        """EventInfo rows (route order) plus per-event feature/point/depth."""
        depth_idx = layer.fields().lookupField('ApproxDepth')
        records = []
        for feature in layer.getFeatures():
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            text = self._value(feature, event_idx)
            if text is None or not str(text).strip():
                continue
            point = QgsPointXY(geometry.asPoint())
            if to_design is not None and to_design.isValid():
                try:
                    point = to_design.transform(point)
                except Exception:  # noqa: BLE001 - unprojectable point: skip
                    continue
            if kp_idx >= 0:
                kp = _float(self._value(feature, kp_idx))
            else:
                kp = comparator.calculate_kp_to_point(point, source=design)
            try:
                wgs = to_wgs84.transform(point)
                lat, lon = wgs.y(), wgs.x()
            except Exception:  # noqa: BLE001
                lat = lon = None
            route_kp = comparator.calculate_kp_to_point(point, source=design)
            records.append({
                'point': point, 'feature': feature, 'text': str(text).strip(), 'kp': kp,
                'route_kp': route_kp, 'lat': lat, 'lon': lon,
                'depth': _float(self._value(feature, depth_idx)),
            })
        records.sort(key=lambda r: (r['route_kp'] if r['route_kp'] is not None else math.inf))
        points = [{'seq': i, 'event': r['text'], 'kp': r['kp'], 'lat': r['lat'], 'lon': r['lon'],
                   'depth': r['depth']} for i, r in enumerate(records)]
        return ec.extract_events(points), records

    def _pair_events(self, design_events, aslaid_events, match_mode, search_radius, feedback):
        if match_mode == MATCH_EXACT:
            mapping = ec.EventMapping(list(design_events), list(aslaid_events))
            for a, b, how in ec._exact_anchors(design_events, aslaid_events):
                mapping.pairs[a] = (b, how, 1.0)
            duplicates = self._duplicates(design_events) | self._duplicates(aslaid_events)
            if duplicates:
                feedback.pushWarning(
                    'Event names that occur more than once are not paired in exact mode: '
                    + ', '.join(sorted(duplicates)))
            return mapping
        return ec.EventMapping.suggest(
            design_events, aslaid_events, ec.MatchOptions(search_radius_m=search_radius))

    @staticmethod
    def _duplicates(events):
        seen, duplicated = set(), set()
        for event in events:
            key = ec.normalise_event(event.event)
            if key in seen:
                duplicated.add(event.event)
            seen.add(key)
        return duplicated

    @staticmethod
    def _report_unmatched(unmatched_design, unmatched_aslaid, event_filter, feedback):
        def names(events, side):
            kept = [e.event for e in events
                    if event_filter.accepts(ec.EventOffset(a=e if side == 'a' else None,
                                                           b=e if side == 'b' else None))]
            return kept

        design_names = names(unmatched_design, 'a')
        aslaid_names = names(unmatched_aslaid, 'b')
        if design_names:
            feedback.pushWarning(f'Unmatched design events: {", ".join(design_names)}')
        if aslaid_names:
            feedback.pushWarning(f'Unmatched as-laid events: {", ".join(aslaid_names)}')

    def _selection_text(self, preset_index, text_filter):
        label = ec.FILTER_PRESETS[preset_index][1] if 0 <= preset_index < len(
            ec.FILTER_PRESETS) else 'All events'
        return label + (f", matching '{text_filter}'" if text_filter else '')

    def _measure_distance(self, point1, point2, distance_calc):
        """Ellipsoidal distance in metres between two points in the design CRS."""
        return distance_calc.measureLine(point1, point2)

    def _calculate_offsets(self, design_point, aslaid_point, comparator, distance_calc):
        """
        Calculate along-track, cross-track, and radial distance offsets.

        Both along- and cross-track come from the design route's
        ``RouteFrame`` (via ``comparator``): the same KP definition as every
        other KP tool, so they agree with Nearest KP / the KP Mouse tool.

        Returns dict with 'design_kp', 'along_track', 'cross_track',
        'radial_distance', 'bearing'
        - design_kp: KP (km) of the design event on the design route
        - along_track: signed distance in meters along the design route from
          the design event's projection to the as-laid event's projection
          (+ ahead, - behind)
        - cross_track: signed perpendicular distance in meters from the
          as-laid event to the design route (+ starboard, - port)
        - radial_distance: direct distance in meters
        - bearing: true bearing from design to as-laid in degrees (0-360),
          None when the two points coincide
        """
        radial = self._measure_distance(design_point, aslaid_point, distance_calc)

        design_hit = comparator.nearest_kp_hit(design_point, source=True)
        aslaid_hit, cross_track = comparator.signed_offset(aslaid_point, source=True)
        if design_hit.snapped_xy is None or aslaid_hit.snapped_xy is None:
            design_kp, along_track, cross_track = 0.0, 0.0, 0.0
        else:
            design_kp = design_hit.kp_km
            along_track = (aslaid_hit.kp_km - design_hit.kp_km) * 1000.0

        bearing = self._calculate_bearing(design_point, aslaid_point, distance_calc)

        return {
            'design_kp': design_kp,
            'radial_distance': radial,
            'cross_track': cross_track,
            'along_track': along_track,
            'bearing': bearing
        }

    def _calculate_bearing(self, from_point, to_point, distance_calc=None):
        """True bearing (degrees, 0-360, 0 = north) from one point to another.

        Uses the ellipsoidal bearing of ``distance_calc`` (correct in
        geographic and projected CRSs alike); without one, falls back to the
        planar grid bearing.
        """
        if (abs(to_point.x() - from_point.x()) < 1e-12
                and abs(to_point.y() - from_point.y()) < 1e-12):
            return None
        if distance_calc is not None:
            try:
                radians = distance_calc.bearing(QgsPointXY(from_point), QgsPointXY(to_point))
                return (math.degrees(radians) + 360.0) % 360.0
            except Exception:  # noqa: BLE001 - fall back to the grid bearing
                pass
        radians = math.atan2(to_point.x() - from_point.x(), to_point.y() - from_point.y())
        return (math.degrees(radians) + 360.0) % 360.0

    def _extract_ac_kps(self, comparator, lines_layer, distance_calc, feedback):
        """
        Extract alter course (AC) KP values from the design route.
        ACs are points where the bearing changes significantly (> 2 degrees).

        Returns:
            List of KP values (in km) for alter courses, sorted
        """
        ac_kps = set()

        feedback.pushInfo(f'Collected {len(comparator.source_geoms)} line geometries from design route')

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

        geom_list.sort(key=lambda x: x[0])

        for i in range(len(geom_list) - 1):
            _kp1, p1, p2 = geom_list[i]
            _kp2, p3, p4 = geom_list[i + 1]
            bearing1 = distance_calc.bearing(p1, p2)
            bearing2 = distance_calc.bearing(p3, p4)
            junction_kp = comparator.calculate_kp_to_point(QgsPointXY(p2.x(), p2.y()), source=True)
            bearing_diff = abs(bearing1 - bearing2)
            bearing_diff = min(bearing_diff, 2 * math.pi - bearing_diff)
            if bearing_diff > 0.0349:
                ac_kps.add(junction_kp)

        return sorted(ac_kps)


def _float(value):
    try:
        number = None if value is None else float(value)
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None
