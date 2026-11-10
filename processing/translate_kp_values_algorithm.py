"""Translate point and range KPs using the shared route model."""

import math

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
    QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessing, QgsProcessingException,
    QgsProcessingParameterBoolean, QgsProcessingParameterCrs, QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField, QgsProcessingParameterNumber, QgsProcessingParameterString,
    QgsWkbTypes,
)

from .algorithm_base import SubseaCableAlgorithm
from .kp_common import build_route, report_kp_basis
from .kp_translation import mapping_flags, parse_kp, pasted_rows, sample_kps
from ..kp_geo_utils import get_features_skip_invalid, ordered_route_features
from ..kp_range_utils import (
    GridDistanceArea, add_distance_mode_parameter, make_distance_area, read_distance_mode,
)
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING, PROCESSING_NUMBER_DOUBLE


class TranslateKPValuesAlgorithm(SubseaCableAlgorithm):
    def tr(self, text):
        return QCoreApplication.translate('Processing', text)

    def name(self):
        return 'translate_kp_values'

    def displayName(self):
        return 'Translate KPs and Ranges Between RPLs'

    def group(self):
        return 'RPL Tools'

    def groupId(self):
        return 'rpl_tools'

    def createInstance(self):
        return TranslateKPValuesAlgorithm()

    def initAlgorithm(self, config=None):
        for key, label in [('OLD', 'Old RPL'), ('NEW', 'New RPL')]:
            self.addParameter(QgsProcessingParameterFeatureSource(
                key, label, [QgsProcessing.TypeVectorLine]))
            self.addParameter(QgsProcessingParameterNumber(
                key + '_START', label + ' — route start KP (km)',
                type=PROCESSING_NUMBER_DOUBLE, defaultValue=0))
            add_distance_mode_parameter(self, key + '_MODE')
            self.parameterDefinition(key + '_MODE').setDescription(label + ' — distance mode')
            self.addParameter(QgsProcessingParameterCrs(
                key + '_GRID', label + ' — grid CRS (grid mode only; blank uses KP settings)', optional=True))
        self.addParameter(QgsProcessingParameterEnum('INPUT_MODE', 'Input basis', options=[
            'KP fields in a layer or table', 'Pasted KP list',
            'Geometry projected onto old RPL (single points or single-part lines)',
        ], defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSource(
            'INPUT', 'Input layer or table', [QgsProcessing.TypeVector], optional=True))
        self.addParameter(QgsProcessingParameterField(
            'START_FIELD', 'KP / start KP field', defaultValue='start_kp',
            parentLayerParameterName='INPUT', optional=True))
        self.addParameter(QgsProcessingParameterField(
            'END_FIELD', 'End KP field (leave empty for single KPs)',
            parentLayerParameterName='INPUT', optional=True))
        self.addParameter(QgsProcessingParameterString(
            'PASTE', 'Pasted KPs (one row per point or range; extra columns retained)',
            multiLine=True, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            'PASTE_TYPE', 'Pasted list type', options=['Single KPs', 'Start / end KP ranges'], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            'HEADER', 'Pasted list has a header row', defaultValue=False))
        for key, label, default, minimum in [
            ('MAX_OFFSET', 'Maximum route separation / geometry offset (m)', 25, 0),
            ('STEP', 'Diagnostic sample spacing (m)', 50, 1),
            ('STRETCH', 'Flag range length change above (%)', 10, 0),
            ('ROUNDTRIP', 'Maximum return-to-old-route KP difference (m)', 1, 0.01),
        ]:
            self.addParameter(QgsProcessingParameterNumber(
                key, label, type=PROCESSING_NUMBER_DOUBLE, defaultValue=default, minValue=minimum))
        self.addParameter(QgsProcessingParameterFeatureSink(
            'RESULTS', 'Translation results — all rows and review flags', type=QgsProcessing.TypeVector))
        self.addParameter(QgsProcessingParameterFeatureSink(
            'POINTS', 'Accepted points on new RPL', type=QgsProcessing.TypeVectorPoint, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            'RANGES', 'Accepted ranges on new RPL', type=QgsProcessing.TypeVectorLine, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            'ORIGINAL', 'Input geometry with translation results (layer input only)',
            optional=True, createByDefault=False))

    def _route(self, key, parameters, context, feedback):
        source = self.parameterAsSource(parameters, key, context)
        if source is None or not source.sourceCrs().isValid():
            raise QgsProcessingException(f'{key} RPL needs a valid line source and CRS')
        start = self.parameterAsDouble(parameters, key + '_START', context)
        if not math.isfinite(start):
            raise QgsProcessingException('Route start KP must be finite')
        mode = read_distance_mode(self, parameters, context, key + '_MODE')
        grid = self.parameterAsCrs(parameters, key + '_GRID', context)
        if mode == 'cartesian' and grid.isValid():
            if grid.isGeographic():
                raise QgsProcessingException('Grid KP requires a projected grid CRS')
            distance = GridDistanceArea(source.sourceCrs(), context.transformContext(), grid, 'explicit parameter')
        else:
            distance = make_distance_area(source.sourceCrs(), context.transformContext(),
                                          mode=mode, project=context.project())
        features = ordered_route_features(get_features_skip_invalid(source))
        if not features or any(not f.hasGeometry() or f.geometry().isEmpty() for f in features):
            raise QgsProcessingException(f'{key} RPL contains missing geometry or is empty')
        route = build_route([QgsGeometry(f.geometry()) for f in features], distance, start)
        feedback.pushInfo(f'{key} RPL: {source.sourceName()}')
        report_kp_basis(feedback, route, start)
        # extract_segment joins disconnected parts: reject these routes here,
        # rather than drawing a fictitious connecting leg in an accepted range.
        previous = None
        vertices = []
        for a, b, ka, kb in route.measured_segments():
            if feedback.isCanceled():
                raise QgsProcessingException('Cancelled')
            if previous is not None and (abs(a.x() - previous.x()) > 1e-9
                                         or abs(a.y() - previous.y()) > 1e-9):
                raise QgsProcessingException(f'{key} RPL is disconnected; select one continuous route')
            previous = b
            vertices.extend((ka, kb))
        if (not vertices or route.total_length_m <= 0 or route.point_at_kp(route.end_kp_km, clamp=True) is None
                or abs(vertices[-1] - route.end_kp_km) > 1e-8):
            raise QgsProcessingException(f'{key} RPL cannot be measured completely')
        return source, route, sorted(set(vertices)), distance

    @staticmethod
    def _hit(route, point):
        hit = route.kp_at_point(point)
        if hit.feature_index < 0 or hit.snapped_xy is None or not math.isfinite(hit.dcc_m):
            raise ValueError('No valid nearest position on route')
        return hit

    @staticmethod
    def _fields(original):
        fields = QgsFields(original)
        names = {}
        specs = [(name, FIELD_TYPE_DOUBLE) for name in (
            'old_start_kp', 'old_end_kp', 'new_start_kp', 'new_end_kp',
            'start_dcc_m', 'end_dcc_m', 'sample_max_dcc_m', 'length_change_m',
            'return_kp_error_m', 'input_max_dcc_m',
        )] + [(name, FIELD_TYPE_STRING) for name in (
            'input_id', 'old_rpl', 'new_rpl', 'translation_status', 'translation_flags',
        )]
        for name, kind in specs:
            candidate, suffix = name, 2
            while fields.indexFromName(candidate) >= 0:
                candidate = f'{name}_{suffix}'
                suffix += 1
            fields.append(QgsField(candidate, kind))
            names[name] = candidate
        return fields, names

    def processAlgorithm(self, parameters, context, feedback):
        mode = self.parameterAsEnum(parameters, 'INPUT_MODE', context)
        source = self.parameterAsSource(parameters, 'INPUT', context) if mode != 1 else None
        start_field = self.parameterAsString(parameters, 'START_FIELD', context)
        end_field = self.parameterAsString(parameters, 'END_FIELD', context)
        if mode != 1 and source is None:
            raise QgsProcessingException('Select an input layer or table')
        if mode == 0:
            for field in [start_field] + ([end_field] if end_field else []):
                if not field or source.fields().indexFromName(field) < 0:
                    raise QgsProcessingException(f'Select a valid KP field: {field!r}')
        if mode == 2 and (not source.sourceCrs().isValid()
                          or QgsWkbTypes.geometryType(source.wkbType()) not in (0, 1)):
            raise QgsProcessingException('Geometry mode requires a point or line layer with a valid CRS')
        ranges = self.parameterAsEnum(parameters, 'PASTE_TYPE', context) == 1
        if mode == 1:
            try:
                headers, rows = pasted_rows(self.parameterAsString(parameters, 'PASTE', context), ranges,
                                            self.parameterAsBool(parameters, 'HEADER', context))
            except ValueError as exc:
                raise QgsProcessingException(str(exc)) from exc
            original_fields = QgsFields()
            for i, header in enumerate(headers):
                name = header or f'column_{i + 1}'
                while original_fields.indexFromName(name) >= 0:
                    name += '_copy'
                original_fields.append(QgsField(name, FIELD_TYPE_STRING))
            records = ((str(i + 1), row, None) for i, row in enumerate(rows))
            count = len(rows)
            if parameters.get('ORIGINAL'):
                raise QgsProcessingException('Original geometry output requires layer input')
        else:
            original_fields = source.fields()
            records = ((str(f.id()), f.attributes(), f) for f in get_features_skip_invalid(source))
            count = source.featureCount()

        old_source, old, vertices, old_distance = self._route('OLD', parameters, context, feedback)
        new_source, new, _, _new_distance = self._route('NEW', parameters, context, feedback)
        forward = QgsCoordinateTransform(old_source.sourceCrs(), new_source.sourceCrs(), context.transformContext())
        backward = QgsCoordinateTransform(new_source.sourceCrs(), old_source.sourceCrs(), context.transformContext())
        input_transform = (QgsCoordinateTransform(source.sourceCrs(), old_source.sourceCrs(), context.transformContext())
                           if mode == 2 else None)
        step = self.parameterAsDouble(parameters, 'STEP', context)
        limit = self.parameterAsDouble(parameters, 'MAX_OFFSET', context)
        stretch = self.parameterAsDouble(parameters, 'STRETCH', context)
        return_limit = self.parameterAsDouble(parameters, 'ROUNDTRIP', context)
        if (not all(math.isfinite(v) for v in (step, limit, stretch, return_limit))
                or step < 1 or min(limit, stretch) < 0 or return_limit < 0.01):
            raise QgsProcessingException('Diagnostic settings must be finite; spacing must be at least 1 m')
        fields, names = self._fields(original_fields)
        feedback.pushInfo('Result fields: ' + ', '.join(f'{key}={value}' for key, value in names.items()))
        sinks, results = {}, {}
        for key, wkb, crs in [
            ('RESULTS', QgsWkbTypes.NoGeometry, QgsCoordinateReferenceSystem()),
            ('POINTS', QgsWkbTypes.Point, new_source.sourceCrs()),
            ('RANGES', QgsWkbTypes.LineString, new_source.sourceCrs()),
            ('ORIGINAL', source.wkbType() if source else QgsWkbTypes.NoGeometry,
             source.sourceCrs() if source else QgsCoordinateReferenceSystem()),
        ]:
            sink, dest = self.parameterAsSink(parameters, key, context, fields, wkb, crs)
            if sink is None and (key == 'RESULTS' or parameters.get(key)):
                raise QgsProcessingException(self.invalidSinkError(parameters, key))
            if sink is not None:
                sinks[key], results[key] = sink, dest

        def write(key, attrs, geometry=None):
            if key not in sinks:
                return
            feature = QgsFeature(fields)
            feature.setAttributes(attrs)
            if geometry is not None:
                feature.setGeometry(geometry)
            if not sinks[key].addFeature(feature, QgsFeatureSink.FastInsert):
                raise QgsProcessingException(f'Could not write {key} output')

        totals = {'accepted': 0, 'review': 0, 'invalid': 0}
        for index, (record_id, attrs, feature) in enumerate(records):
            if feedback.isCanceled():
                break
            data = dict(input_id=record_id, old_rpl=old_source.sourceName(), new_rpl=new_source.sourceName())
            flags = []
            output_geom = None
            try:
                input_dcc = None
                if mode == 1:
                    a, b = parse_kp(attrs[0]), parse_kp(attrs[1] if ranges else attrs[0])
                elif mode == 0:
                    a = parse_kp(feature[start_field])
                    b = parse_kp(feature[end_field]) if end_field else a
                else:
                    a, b, input_dcc = self._geometry_kps(
                        feature.geometry(), input_transform, old, old_distance, step, feedback)
                    if input_dcc > limit:
                        flags.append('input_off_old_route')
                data.update(old_start_kp=a, old_end_kp=b, input_max_dcc_m=input_dcc)
                if min(a, b) < old.start_kp_km or max(a, b) > old.end_kp_km:
                    raise ValueError('KP outside old RPL extent (no clamping)')
                stations = sample_kps(a, b, step, vertices)
                hits, return_errors = [], []
                for kp in stations:
                    if feedback.isCanceled():
                        break
                    # Extent was checked above. clamp handles only floating-point
                    # cancellation at the exact end KP (km -> metres).
                    point = old.point_at_kp(kp, clamp=True)
                    if point is None:
                        raise ValueError('Old KP has no measurable position')
                    hit = self._hit(new, forward.transform(point))
                    hits.append(hit)
                    back = self._hit(old, backward.transform(hit.snapped_xy))
                    return_errors.append(abs(back.kp_km - kp) * 1000)
                    if hit.dcc_m > 0.01 and min(abs(hit.kp_km - new.start_kp_km),
                                               abs(hit.kp_km - new.end_kp_km)) < 1e-8:
                        flags.append('target_endpoint')
                if feedback.isCanceled():
                    break
                offsets = [h.dcc_m for h in hits]
                mapped = [h.kp_km for h in hits]
                flags += mapping_flags(stations, mapped, offsets, limit, stretch)
                if max(return_errors) > return_limit:
                    flags.append('return_kp_mismatch')
                data.update(new_start_kp=mapped[0], new_end_kp=mapped[-1],
                            start_dcc_m=offsets[0], end_dcc_m=offsets[-1], sample_max_dcc_m=max(offsets),
                            length_change_m=(abs(mapped[-1] - mapped[0]) - abs(b - a)) * 1000,
                            return_kp_error_m=max(return_errors))
                if not flags:
                    output_geom = (QgsGeometry.fromPointXY(hits[0].snapped_xy) if a == b
                                   else new.extract_segment(mapped[0], mapped[-1]))
                    if output_geom is None or output_geom.isEmpty():
                        raise ValueError('No output geometry could be constructed')
                status = 'review' if flags else 'accepted'
            except Exception as exc:
                status = 'invalid'
                flags.append(str(exc))
            data.update(translation_status=status, translation_flags='; '.join(dict.fromkeys(flags)))
            totals[status] += 1
            out_attrs = list(attrs) + [data.get(key) for key in names]
            write('RESULTS', out_attrs)
            if feature is not None:
                write('ORIGINAL', out_attrs, feature.geometry())
            if status == 'accepted':
                write('POINTS' if a == b else 'RANGES', out_attrs, output_geom)
            if count > 0:
                feedback.setProgress(100 * (index + 1) / count)
        feedback.pushInfo(', '.join(f'{number} {status}' for status, number in totals.items()))
        feedback.pushInfo('Diagnostics are sampled; accepted means no configured check failed, not proven revision correspondence.')
        return results

    def _geometry_kps(self, geometry, transform, old, distance, step, feedback):
        if geometry.isNull() or geometry.isEmpty() or geometry.isMultipart():
            raise ValueError('Use KP fields for empty or multipart geometry; endpoint order is ambiguous')
        geometry = QgsGeometry(geometry)
        geometry.transform(transform)
        if QgsWkbTypes.geometryType(geometry.wkbType()) == 0:
            hit = self._hit(old, QgsPointXY(geometry.asPoint()))
            return hit.kp_km, hit.kp_km, hit.dcc_m
        # Sample the input line too: an off-route detour must not disappear
        # merely because both of its endpoints happen to lie on the old route.
        # Reuse the already resolved grid (including automatic UTM selection).
        line = build_route([geometry], distance)
        vertices = [v for _a, _b, ka, kb in line.measured_segments() for v in (ka, kb)]
        if not vertices or line.total_length_m <= 0:
            raise ValueError('Input line has no measurable length')
        hits = []
        for kp in sample_kps(0, line.end_kp_km, step, vertices):
            if feedback.isCanceled():
                raise ValueError('Cancelled')
            point = line.point_at_kp(kp, clamp=True)
            if point is None:
                raise ValueError('Input line cannot be measured completely')
            hits.append(self._hit(old, point))
        direction = 1 if hits[-1].kp_km >= hits[0].kp_km else -1
        if any((y.kp_km - x.kp_km) * direction < -1e-6 for x, y in zip(hits, hits[1:])):
            raise ValueError('Input line doubles back along old RPL; use explicit KP fields')
        if abs(hits[-1].kp_km - hits[0].kp_km) < 1e-9:
            raise ValueError('Input line collapses to a single old KP')
        return hits[0].kp_km, hits[-1].kp_km, max(h.dcc_m for h in hits)

    def shortHelpString(self):
        return '''<p>Translate single KPs or KP ranges from an old RPL to a new RPL.
        Each old KP is located on the old route, transformed to the new route CRS,
        then assigned the nearest new KP using the shared RouteFrame model.</p>
        <p><b>Input basis:</b> KP fields work with tables, points and lines, including
        grouped multipart lines. Geometry is ignored in this mode. Leave End KP field
        empty for single KPs. Geometry mode projects single points or single-part line
        endpoints onto the old RPL first; it also checks the line interior against that
        RPL. Multipart geometry requires explicit KP fields.</p>
        <p><b>Paste:</b> select Single KPs or Start/end ranges. Use one row per record,
        separated by tabs, commas, semicolons or whitespace. Decimal kilometres and
        12+345 notation are accepted; use a decimal point, not decimal commas or thousands
        separators. Extra columns are retained. Select the header checkbox explicitly.
        In range mode, equal endpoints produce a point.</p>
        <p><b>Outputs:</b> Results contains every row, original attributes, old/new KPs,
        endpoint DCC, sampled maximum DCC, length change, return KP error and status.
        Accepted points/ranges are rebuilt on the new RPL; rows needing review are
        retained only in Results and optional Original geometry output. Existing fields
        are never overwritten; conflicting result names receive suffixes.</p>
        <p><b>Checks:</b> route separation (default 25 m), length change (10%), return
        projection KP mismatch (1 m), reversed/collapsed ranges, non-monotone mapping,
        jumps greater than five times the source sample interval, and off-route snaps
        to target endpoints. Range interiors are checked every 50 m and at KP-path
        vertices. These sampled checks cannot prove unique engineering correspondence;
        inspect reroutes and nearby parallel branches. No gap interpolation or extrapolation.
        Disconnected RPLs are rejected. Reverse-directed input ranges retain endpoint
        correspondence; output lines run in increasing new KP order.</p>
        <p><b>KP basis:</b> measured chainage, not interpolation of printed RPL KP fields.
        Set each RPL's start KP and distance mode to match its source data (normally
        WGS84 geodesic). Grid mode supports separate explicit grid CRSs; otherwise it
        uses the existing KP settings/fallbacks. Features follow SeqNo when complete,
        otherwise layer order. Full calculation precision is retained.</p>'''
