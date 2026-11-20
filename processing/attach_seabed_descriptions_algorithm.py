"""Optional annotation assembly and conservative leader-to-polygon association."""

from collections import Counter, defaultdict
import json
import math
import os
import re
from uuid import uuid4

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    NULL, QgsCoordinateTransform, QgsFeature,
    QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessing,
    QgsProcessingException, QgsProcessingLayerPostProcessorInterface,
    QgsProcessingParameterBoolean, QgsProcessingParameterCrs, QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
    QgsProcessingParameterMultipleLayers, QgsProcessingParameterNumber,
    QgsVectorLayerSimpleLabeling,
    QgsPalLayerSettings, QgsTextFormat, QgsWkbTypes, QgsCategorizedSymbolRenderer,
    QgsMarkerSymbol, QgsRendererCategory, QgsProperty,
    QgsProviderRegistry, Qgis, QgsProcessingParameterDefinition,
)

from .algorithm_base import SubseaCableAlgorithm
from .annotation_blocks import TextBlock, TextLine, assemble_blocks, estimate_line_gap, normalized_text
from .annotation_geodesy import (
    BOUNDARY_MARGIN, EllipsoidalMetric, GlobalPointIndex, GlobalPolygonIndex, LocalPolygon, geographic,
)
from ..qgis_compat import (
    FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_STRING,
    GEOMETRY_LINE, GEOMETRY_POLYGON, PROCESSING_FIELD_ANY,
    PROCESSING_FIELD_NUMERIC, PROCESSING_NUMBER_DOUBLE, WKB_POINT, WKB_LINESTRING,
)


MAX_FEATURES = 100000
MAX_PAIRS = 500000
MAX_TEXT_CHARS = 20000000
POLYGON_FIELDS = (
    'soil_description', 'candidate_text', 'description_status', 'match_reason', 'polygon_ref',
    'source_layer', 'source_layer_id', 'source_feature_id', 'block_ids', 'leader_refs',
)
REVIEW_FIELDS = ('association_run', 'review_status')


def _present(value):
    return value is not None and value != NULL


def _string(value):
    return str(value) if _present(value) else ''


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _safe_coordinates(geometry, cancel):
    for i, vertex in enumerate(geometry.vertices()):
        if i % 1000 == 0:
            cancel()
        if not all(math.isfinite(value) and abs(value) <= 1e12 for value in (vertex.x(), vertex.y())):
            return False
    return True


def _transform_geometry(geometry, transform, cancel):
    if not _safe_coordinates(geometry, cancel):
        raise ValueError('Unsafe or non-finite geometry coordinates; output geometry omitted.')
    if geometry.transform(transform) != 0 or not _safe_coordinates(geometry, cancel):
        raise ValueError('Geometry could not be transformed safely; output geometry omitted.')


def _fields(specification):
    fields = QgsFields()
    for name, kind in specification:
        fields.append(QgsField(name, kind))
    return fields


def _new_field(fields, name, kind):
    used = {field.name().casefold() for field in fields}
    base, suffix = name, 2
    while name.casefold() in used:
        name, suffix = f'{base}_{suffix}', suffix + 1
    fields.append(QgsField(name, kind))
    return name


class _LabelPoints(QgsProcessingLayerPostProcessorInterface):
    """Applied by Processing on the GUI thread after the output is loaded."""

    def postProcessLayer(self, layer, context, feedback):
        settings = QgsPalLayerSettings()
        settings.fieldName = 'assembled_text'
        settings.isExpression = False
        settings.wrapChar = '\n'
        text_format = QgsTextFormat()
        text_format.setSize(9)
        settings.setFormat(text_format)
        properties = settings.dataDefinedProperties()
        scope = getattr(QgsPalLayerSettings, 'Property', QgsPalLayerSettings)
        properties.setProperty(scope.LabelRotation, QgsProperty.fromExpression('-"label_rotation"'))
        settings.setDataDefinedProperties(properties)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
        categories = []
        for value, caption, color in [
                ('supported', 'Supported by layout and leader', '#269956'),
                ('single', 'Original single text', '#397ec2'),
                ('layout_only', 'Layout only — check assembly', '#d39725'),
                ('review', 'Review — candidate text only', '#d34242')]:
            symbol = QgsMarkerSymbol.createSimple({'name': 'circle', 'color': color, 'size': '2.2'})
            categories.append(QgsRendererCategory(value, symbol, caption))
        layer.setRenderer(QgsCategorizedSymbolRenderer('text_status', categories))
        layer.setDisplayExpression('coalesce("assembled_text", "candidate_text")')
        layer.setCustomProperty('subsea/annotation_certainty',
                                'Geometric evidence only; review layout_only and review statuses.')
        layer.triggerRepaint()


class AttachSeabedDescriptionsAlgorithm(SubseaCableAlgorithm):
    TEXT = 'TEXT'
    TEXT_FIELD = 'TEXT_FIELD'
    ROTATION_FIELD = 'ROTATION_FIELD'
    ALIGNMENT_FIELD = 'ALIGNMENT_FIELD'
    LEADERS = 'LEADERS'
    POLYGONS = 'POLYGONS'
    WORKING_CRS = 'WORKING_CRS'
    MAX_LINE_GAP = 'MAX_LINE_GAP'
    ANCHOR_TOLERANCE = 'ANCHOR_TOLERANCE'
    BOUNDARY_TOLERANCE = 'BOUNDARY_TOLERANCE'
    ALLOW_MULTIPLE_ANCHORS = 'ALLOW_MULTIPLE_ANCHORS'
    OUTPUT_LABELS = 'OUTPUT_LABELS'
    OUTPUT_POLYGONS = 'OUTPUT_POLYGONS'
    OUTPUT_CHECKS = 'OUTPUT_CHECKS'

    def name(self):
        return 'attach_seabed_descriptions'

    def displayName(self):
        return self.tr('Attach descriptions to seabed polygons (experimental)')

    def group(self):
        return self.tr('Other Tools')

    def groupId(self):
        return 'other_tools'

    def createInstance(self):
        return AttachSeabedDescriptionsAlgorithm()

    def tr(self, text):
        return QCoreApplication.translate('AttachSeabedDescriptionsAlgorithm', text)

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.TEXT, self.tr('Description text points'), [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.TEXT_FIELD, self.tr('Text field (leave empty to detect automatically)'),
            parentLayerParameterName=self.TEXT, type=PROCESSING_FIELD_ANY, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.ROTATION_FIELD, self.tr('Text rotation field (optional; auto-detect label_rotation, counter-clockwise degrees)'),
            parentLayerParameterName=self.TEXT, type=PROCESSING_FIELD_NUMERIC, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.ALIGNMENT_FIELD, self.tr('Text alignment field (optional; auto-detect label_alignment)'),
            parentLayerParameterName=self.TEXT, type=PROCESSING_FIELD_ANY, optional=True))
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.LEADERS, self.tr('Description leader layers (optional)'),
            layerType=QgsProcessing.TypeVectorLine, optional=True))
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.POLYGONS, self.tr('Soil polygon layers (optional)'),
            layerType=QgsProcessing.TypeVectorPolygon, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_LINE_GAP, self.tr('Maximum gap between consecutive text lines (metres; 0 = estimate)'),
            type=PROCESSING_NUMBER_DOUBLE, defaultValue=0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.ANCHOR_TOLERANCE, self.tr('Leader endpoint to text anchor tolerance (metres)'),
            type=PROCESSING_NUMBER_DOUBLE, defaultValue=1, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.BOUNDARY_TOLERANCE, self.tr('Flag targets this close to polygon boundaries for review (metres)'),
            type=PROCESSING_NUMBER_DOUBLE, defaultValue=0.5, minValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ALLOW_MULTIPLE_ANCHORS,
            self.tr('Allow leaders to start on different lines of one description'), defaultValue=False))
        self.addParameter(QgsProcessingParameterCrs(
            self.WORKING_CRS, self.tr('Legacy measurement CRS (ignored; measurements are now ellipsoidal)'), optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_LABELS, self.tr('Assembled description points'), type=QgsProcessing.TypeVectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_POLYGONS, self.tr('Soil polygons with description candidates'),
            type=QgsProcessing.TypeVectorPolygon, optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_CHECKS, self.tr('Leader association checks'),
            type=QgsProcessing.TypeVectorLine, optional=True, createByDefault=True))
        try:
            advanced = Qgis.ProcessingParameterFlag.Advanced
            hidden = Qgis.ProcessingParameterFlag.Hidden
        except AttributeError:
            advanced = QgsProcessingParameterDefinition.FlagAdvanced
            hidden = QgsProcessingParameterDefinition.FlagHidden
        for name in (self.ROTATION_FIELD, self.ALIGNMENT_FIELD, self.MAX_LINE_GAP,
                     self.ANCHOR_TOLERANCE, self.BOUNDARY_TOLERANCE, self.ALLOW_MULTIPLE_ANCHORS, self.WORKING_CRS):
            parameter = self.parameterDefinition(name)
            parameter.setFlags(parameter.flags() | advanced)
        legacy_crs = self.parameterDefinition(self.WORKING_CRS)
        legacy_crs.setFlags(legacy_crs.flags() | hidden)

    def shortHelpString(self):
        return '''<p><b>Quick start:</b> select the description point layer and run. The text field
is detected automatically for normal MDB imports. Leaders and soil polygons are optional: add
them to attach descriptions to polygons, or leave them empty to assemble labels only.
Source layers are never edited. Inputs may use geographic coordinates (such as EPSG:4326),
projected coordinates or different CRSs. Long routes, multiple UTM zones, date-line crossings
and polar regions are supported without splitting the input or selecting a measurement CRS.</p>
<p>If several possible text fields are present, choose the correct field explicitly. Rotation,
alignment, spacing and distance tolerances are under <b>Advanced parameters</b>.</p>
<p>Aligned lines with compatible rotations, alignment and consistent spacing are assembled in
reading order, with newlines preserved. Zero line gap estimates a limit from repeated local
spacing; small or mixed-scale samples require review or an explicit limit. The rule is geometric,
not a probability or confirmation of the survey interpretation. Inspect the first results at
the survey drawing scale.</p>
<p><b>Text status:</b> single = original text; supported = consistent layout with leader
anchor evidence; layout_only = consistent layout without leader evidence;
review = uncertain assembly. Accepted text is in <b>assembled_text</b>. Review rows keep the
proposal in <b>candidate_text</b>, with assembled_text empty so they are not labelled as accepted.
Source feature IDs and reasons are retained. A label point is placed at its first text line.</p>
<p>For polygon assignment, a leader must have one unambiguous text end and its other end
strictly inside one selected polygon, away from boundaries. Both line directions are supported.
Several leaders can share a text block and attach it to multiple polygons. If the drawing
allows leaders to start at different lines of one description, enable that option under
<b>Advanced parameters</b>. By default those groups require review because they can also
represent adjacent callouts. Split stems in one
leader layer can be followed through shared endpoints when they form a tree with one text end.
Junctions that could also be polygon targets require review. Overlaps,
boundary hits, invalid polygons and uncertain text are flagged. Invalid polygons are repaired
only in a working copy for candidate searches; output geometries preserve the originals.</p>
<p><b>Polygon status:</b> matched = one distinct accepted description; multiple_descriptions =
different descriptions target the same polygon; review = uncertain evidence; unmatched = no
association. Only matched polygons receive <b>soil_description</b>; proposals remain in
<b>candidate_text</b>. Identical repeated descriptions are deduplicated. All original polygon
attributes are copied; generated names gain a suffix if they would overwrite an existing field.
The source layer, feature IDs and block IDs permit tracing results back to the input.</p>
<p>Open <b>Seabed Description Review</b> from the plugin menu to inspect flagged polygons,
text blocks and leaders on the map and save decisions to the output layers. The leader-check
layer records the upstream part of a split path. Text spacing and leader-anchor distances are
measured on the WGS84 ellipsoid. Polygon containment and boundary distances use a separate local
projection centred on each leader target, with curved transformed edges subdivided to a 2 mm
numerical tolerance and a 4 mm boundary review margin. These tolerances describe the calculation,
not survey accuracy. Geographic polygon edges crossing the date line follow the short longitude
interval. Outputs keep the text layer's original CRS. Valid source CRS definitions and available
datum transformations are still necessary; ambiguous or unmeasurable geometry requires review.
There is no overall route-length or latitude restriction; feature-count and complexity safeguards
still apply. Old saved measurement CRS preferences are ignored.
Select only relevant soil and leader layers;
this tool does not classify soil, infer ground conditions, or change MDB import behaviour.
Use temporary layers or GeoPackage outputs; Shapefile cannot safely store the long text and field names.</p>'''

    @staticmethod
    def _cancel(feedback):
        if feedback.isCanceled():
            raise QgsProcessingException('Description association canceled.')

    @staticmethod
    def _text_field(source, selected):
        names = {field.name().casefold(): field.name() for field in source.fields()}
        if selected:
            if selected.casefold() not in names:
                raise QgsProcessingException(f'Text field {selected} does not exist in the description layer.')
            return names[selected.casefold()]
        # The MDB importer prefixes generated fields on collisions. Do not guess
        # whether the source label_text or a generated gm_label_text is intended.
        candidates = [name for key, name in names.items()
                      if key == 'label_text' or re.fullmatch(r'gm_label_text(?:_\d+)?', key)]
        if not candidates:
            if 'assembled_text' in names:
                return names['assembled_text']  # never auto-select review-only candidate_text
            candidates = [names[key] for key in ('text', 'description', 'label', 'notes') if key in names]
        if not candidates:
            metadata = {'source', 'source_layer', 'source_layer_id', 'geometry_source', 'fid', 'id', 'id1',
                        'candidate_text', 'text_status', 'text_reason', 'target_status', 'source_fids', 'polygon_refs'}
            candidates = [field.name() for field in source.fields()
                          if field.type() == FIELD_TYPE_STRING and field.name().casefold() not in metadata]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise QgsProcessingException('Several possible text fields were found: ' + ', '.join(candidates)
                                         + '. Choose the description text field explicitly.')
        raise QgsProcessingException('No description text field could be detected. Choose the text field explicitly.')

    @staticmethod
    def _optional_field(source, selected, automatic):
        names = {field.name().casefold(): field.name() for field in source.fields()}
        if selected and selected.casefold() not in names:
            raise QgsProcessingException(f'Field {selected} does not exist in the text layer.')
        return names.get((selected or automatic).casefold())

    def _read_text(self, source, text_field, rotation_field, alignment_field, transform, metric, feedback):
        records, lines, size = {}, [], 0
        for number, feature in enumerate(source.getFeatures()):
            self._cancel(feedback)
            if number >= MAX_FEATURES:
                raise QgsProcessingException('Too many text features; use a smaller selection or survey area.')
            key = feature.id()
            text = _string(feature[text_field]).replace('\r\n', '\n').replace('\r', '\n')
            size += len(text)
            if size > MAX_TEXT_CHARS:
                raise QgsProcessingException('Too much annotation text; use a smaller selection.')
            record = {'geometry': QgsGeometry(), 'text': text, 'rotation': 0.0, 'error': ''}
            records[key] = record
            try:
                geometry = feature.geometry()
                if geometry.isNull() or geometry.isEmpty():
                    raise ValueError('Missing text anchor geometry.')
                points = geometry.asMultiPoint() if geometry.isMultipart() else [geometry.asPoint()]
                if len(points) != 1:
                    raise ValueError('Text has several anchor points.')
                original = points[0]
                if not all(math.isfinite(v) and abs(v) <= 1e12 for v in (original.x(), original.y())):
                    raise ValueError('Unsafe text anchor coordinates.')
                point = geographic(transform.transform(original))
                if not all(math.isfinite(v) and abs(v) <= 1e12 for v in (point.x(), point.y())):
                    raise ValueError('Non-finite text anchor.')
                record['geometry'] = QgsGeometry.fromPointXY(original)
                if not text.strip() or '\x00' in text:
                    raise ValueError('Empty text or embedded NUL character.')
                rotation = feature[rotation_field] if rotation_field else 0.0
                if not _present(rotation):
                    rotation = 0.0
                rotation = float(rotation)
                if not math.isfinite(rotation):
                    raise ValueError('Invalid text rotation.')
                record['rotation'] = rotation
                # Transform the local text baseline along with its anchor; a
                # projected CRS can rotate the axes of geographic source data.
                step = 1e-5 if source.sourceCrs().isGeographic() else 1.0
                angle = math.radians(rotation)
                along = transform.transform(QgsPointXY(original.x() + step * math.cos(angle),
                                                       original.y() + step * math.sin(angle)))
                working_angle = metric.angle(point, geographic(along))
                alignment = _string(feature[alignment_field]) if alignment_field else ''
                lines.append(TextLine(key, point.x(), point.y(), text, working_angle, alignment))
            except Exception as exc:  # retain an explicit review row, never drop text
                record['error'] = str(exc) if isinstance(exc, ValueError) else 'Text anchor could not be transformed or read.'
        if not records:
            raise QgsProcessingException('The description point input contains no features.')
        return records, lines

    def _pairs(self, lines, index, gap, feedback, nearest=False):
        count = 0
        for line in lines:
            self._cancel(feedback)
            point = QgsPointXY(line.x, line.y)
            candidates = (index.nearest(point, 13) if nearest else index.near(point, gap))
            for other in candidates:
                if other == line.key or (not nearest and other < line.key):
                    continue
                count += 1
                if count > MAX_PAIRS:
                    raise QgsProcessingException('Too many competing text neighbors. Reduce the line-gap limit or select a smaller area.')
                yield line.key, other

    def _read_leaders(self, layers, metric, output_crs, text_index, anchor_positions, tolerance, context, feedback):
        leaders, anchored = [], set()
        for layer_number, layer in enumerate(layers, 1):
            if not layer.crs().isValid():
                raise QgsProcessingException(f'Leader layer {layer.name()} has no valid CRS.')
            transform = QgsCoordinateTransform(layer.crs(), metric.crs, context.transformContext())
            to_output = QgsCoordinateTransform(layer.crs(), output_crs, context.transformContext())
            for feature in layer.getFeatures():
                self._cancel(feedback)
                original = feature.geometry()
                geometry_error = ''
                if original.isNull() or original.isEmpty():
                    parts = [None]
                elif not _safe_coordinates(original, lambda: self._cancel(feedback)):
                    parts = [None]
                    geometry_error = 'Unsafe leader coordinates; output geometry omitted.'
                else:
                    if original.type() != GEOMETRY_LINE:
                        raise QgsProcessingException('Leader inputs must be line layers.')
                    original = QgsGeometry(original)
                    original.convertToStraightSegment()
                    parts = original.asMultiPolyline() if original.isMultipart() else [original.asPolyline()]
                for part_number, part in enumerate(parts or [None], 1):
                    if len(leaders) >= MAX_FEATURES:
                        raise QgsProcessingException('Too many leader parts; use a smaller area.')
                    row = {'ref': f'L{layer_number}:{feature.id()}:{part_number}', 'layer': layer.name(), 'layer_id': layer.id(),
                           'fid': feature.id(), 'part': part_number, 'geometry': QgsGeometry(),
                           'ends': [], 'nodes': [], 'near': [], 'error': ''}
                    leaders.append(row)
                    try:
                        if not part or len(part) < 2:
                            raise ValueError(geometry_error or 'Missing or empty leader geometry.')
                        output = QgsGeometry.fromPolylineXY(part)
                        _transform_geometry(output, to_output, lambda: self._cancel(feedback))
                        row['geometry'] = output
                        ends = [geographic(transform.transform(part[0])), geographic(transform.transform(part[-1]))]
                        if not all(math.isfinite(v) for p in ends for v in (p.x(), p.y())):
                            raise ValueError('Non-finite leader endpoint.')
                        if metric.metres(ends[0], ends[1]) <= 1e-6:
                            raise ValueError('Closed leader or arrowhead; no distinct text and target ends.')
                        row['ends'] = ends
                        row['nodes'] = [(layer_number, p.x(), p.y()) for p in (part[0], part[-1])]
                        for point in ends:
                            radius = max(tolerance, 1e-6)
                            close = text_index.near(point, radius)
                            row['near'].append(close)
                        # A seed is only evidence when one end has one distinct
                        # text position. Ambiguous seeds do not strengthen groups.
                        active = [close for close in row['near'] if close]
                        if len(active) == 1 and len({anchor_positions[key] for key in active[0]}) == 1:
                            anchored.update(active[0])
                    except QgsProcessingException:
                        raise
                    except Exception as exc:
                        row['error'] = str(exc) if isinstance(exc, ValueError) else 'Leader geometry could not be transformed.'
        return leaders, anchored

    def _read_polygons(self, layers, output_crs, context, feedback):
        fields, maps, records, index = QgsFields(), [], [], GlobalPolygonIndex(EllipsoidalMetric(context))
        # Preserve attributes, including case/type conflicts and existing soil
        # descriptions. Source fid columns must not become a sink's primary key.
        for layer in layers:
            mapping = []
            for field in layer.fields():
                name = 'source_fid' if field.name().casefold() == 'fid' else field.name()
                existing = next((i for i, output in enumerate(fields)
                                 if i not in mapping and output.name().casefold() == name.casefold()
                                 and output.type() == field.type()), None)
                if existing is None:
                    name = _new_field(fields, name, field.type())
                    existing = fields.indexFromName(name)
                mapping.append(existing)
            maps.append(mapping)
        for layer_number, (layer, mapping) in enumerate(zip(layers, maps), 1):
            if not layer.crs().isValid():
                raise QgsProcessingException(f'Polygon layer {layer.name()} has no valid CRS.')
            to_output = QgsCoordinateTransform(layer.crs(), output_crs, context.transformContext())
            for feature in layer.getFeatures():
                self._cancel(feedback)
                if len(records) >= MAX_FEATURES:
                    raise QgsProcessingException('Too many polygons; use a smaller area.')
                attrs = [None] * len(fields)
                for source_i, dest_i in enumerate(mapping):
                    attrs[dest_i] = feature[source_i]
                row = {'ref': f'P{layer_number}:{feature.id()}', 'layer': layer.name(), 'layer_id': layer.id(), 'fid': feature.id(),
                       'attrs': attrs, 'geometry': QgsGeometry(), 'local': None, 'error': '', 'blocks': set(), 'review': set(), 'leaders': set()}
                records.append(row)
                try:
                    original = feature.geometry()
                    if original.isNull() or original.isEmpty():
                        raise ValueError('Missing polygon geometry.')
                    if original.type() != GEOMETRY_POLYGON:
                        raise QgsProcessingException('Soil inputs must be polygon layers.')
                    if not _safe_coordinates(original, lambda: self._cancel(feedback)):
                        raise ValueError('Unsafe polygon coordinates; output geometry omitted.')
                    output = QgsGeometry(original)
                    output.convertToStraightSegment()
                    _transform_geometry(output, to_output, lambda: self._cancel(feedback))
                    output.convertToMultiType()
                    row['geometry'] = output
                    working = QgsGeometry(original)
                    working.convertToStraightSegment()
                    local = LocalPolygon(working, layer.crs(), context, lambda: self._cancel(feedback))
                    row['local'] = local
                    # Validate at a local point without interpreting a geographic
                    # date-line/polar ring as a flat longitude/latitude polygon.
                    first = next(working.vertices())
                    centre = geographic(local.to_global.transform(QgsPointXY(first.x(), first.y())))
                    local_geometry, repaired = local.at(centre)
                    local.cap_centre = centre
                    local.cap_radius = max(math.hypot(v.x(), v.y()) for v in local_geometry.vertices()) + BOUNDARY_MARGIN
                    if repaired:
                        row['error'] = 'Invalid polygon; repaired only for candidate search. Review geometry and match.'
                except QgsProcessingException:
                    raise
                except Exception as exc:
                    row['error'] = str(exc) if isinstance(exc, ValueError) else 'Polygon geometry could not be transformed.'
                finally:
                    if row['local'] is not None:
                        index.add(len(records) - 1, row['local'])
        return fields, records, index

    def _target_candidates(self, target, polygons, polygon_index, boundary_tolerance, feedback):
        point = QgsGeometry.fromPointXY(QgsPointXY(0, 0))
        radius = max(boundary_tolerance, 1e-6) + BOUNDARY_MARGIN
        inside, nearby, failed, distances = [], [], [], {}
        for key in polygon_index.candidates(target, radius):
            self._cancel(feedback)
            polygon = polygons[key]
            try:
                search, repaired = polygon['local'].at(target)
                if repaired:
                    polygon['error'] = 'Invalid polygon; repaired only for candidate search. Review geometry and match.'
                boundary = QgsGeometry(search.constGet().boundary())
                distances[key] = boundary.distance(point)
                if search.contains(point):
                    inside.append(key)
                elif search.distance(point) <= radius:
                    nearby.append(key)
            except QgsProcessingException:
                raise
            except Exception as exc:
                polygon['error'] = str(exc) if isinstance(exc, ValueError) else 'Polygon could not be measured at this target.'
                failed.append(key)
        candidates = sorted(set(inside + nearby + failed))
        reasons = []
        if failed:
            reasons.append('A candidate polygon could not be measured reliably at this target.')
        if candidates and (len(inside) != 1 or nearby):
            reasons.append('Target overlaps polygons or lies on/near a polygon boundary.')
        edge_distance = distances[inside[0]] if len(inside) == 1 else None
        if edge_distance is not None and edge_distance <= boundary_tolerance + BOUNDARY_MARGIN:
            reasons.append('Target is too close to the polygon boundary.')
        if any(polygons[key]['error'] for key in candidates):
            reasons.append('Polygon geometry requires review.')
        return candidates, edge_distance, reasons

    def _leader_trees(self, leaders, feedback):
        """Find tree components rooted at one text end, using exact same-layer endpoints."""
        adjacency = defaultdict(list)
        for i, leader in enumerate(leaders):
            self._cancel(feedback)
            if not leader['error']:
                for end, node in enumerate(leader['nodes']):
                    adjacency[node].append((i, end))
        visited = set()
        for start in adjacency:
            self._cancel(feedback)
            if start in visited:
                continue
            visited.add(start)
            component, pending = [], [start]
            while pending:
                self._cancel(feedback)
                node = pending.pop()
                component.append(node)
                for i, end in adjacency[node]:
                    other = leaders[i]['nodes'][1 - end]
                    if other not in visited:
                        visited.add(other)
                        pending.append(other)
            if len(component) < 3 or sum(len(adjacency[node]) for node in component) != 2 * (len(component) - 1):
                continue
            roots = [node for node in component if any(leaders[i]['near'][end] for i, end in adjacency[node])]
            if len(roots) != 1:
                continue
            root = roots[0]
            parent = {root: None}
            order = [root]
            for node in order:
                self._cancel(feedback)
                for i, end in adjacency[node]:
                    target_end = 1 - end
                    other = leaders[i]['nodes'][target_end]
                    if other not in parent:
                        parent[other] = (node, i, target_end)
                        order.append(other)
            if any(parent[node][0] != root for node in order[1:]):
                yield adjacency, root, parent, order

    @staticmethod
    def _link_candidates(leader, polygons, candidates, blocks, reason=''):
        for key in candidates:
            polygons[key]['blocks'].update(blocks)
            polygons[key]['leaders'].add(leader['ref'])
            if reason:
                polygons[key]['review'].add(reason)

    def _associate(self, leaders, blocks, polygons, polygon_index, boundary_tolerance, have_polygons, feedback):
        memberships = {key: i for i, block in enumerate(blocks) for key in block.members}
        targets = {}
        for leader in leaders:
            self._cancel(feedback)
            leader.update(status='review' if leader['error'] else 'unmatched', reason=leader['error'],
                          block=None, candidate_blocks=[], polygons=[], gap=None, target_gap=None, upstream=None)

        def target_at(leader, end):
            node = leader['nodes'][end]
            if node not in targets:
                targets[node] = self._target_candidates(leader['ends'][end], polygons, polygon_index,
                                                        boundary_tolerance, feedback)
            return targets[node]

        def assign(leader, target_end, endpoints, anchor_gap, extra_reason=''):
            leader['candidate_blocks'] = endpoints
            leader['gap'] = anchor_gap
            candidates, distance, reasons = target_at(leader, target_end)
            leader.update(polygons=candidates, target_gap=distance)
            reasons = list(reasons)
            if len(endpoints) == 1:
                leader['block'] = endpoints[0]
                if blocks[endpoints[0]].status in {'review', 'layout_only'}:
                    reasons.append('Text assembly lacks unambiguous leader support or requires review.')
            else:
                reasons.append('Competing text blocks at the text end.')
            if extra_reason:
                reasons.append(extra_reason)
            if not candidates:
                if len(endpoints) > 1 or extra_reason:
                    leader['status'] = 'review'
                else:
                    leader['status'] = 'unmatched' if have_polygons else 'not_requested'
                reasons.append('Target is outside the selected soil polygons.' if have_polygons else 'No polygon layers selected.')
                leader['reason'] = ' '.join(reasons)
                return
            reason = ' '.join(reasons)
            leader.update(status='review' if reason else 'matched',
                          reason=reason or 'One text block and one polygon containing the leader target.')
            self._link_candidates(leader, polygons, candidates, endpoints, reason)

        joined = set()
        if polygons:
            for adjacency, root, parent, order in self._leader_trees(leaders, feedback):
                i, end = adjacency[root][0]
                anchors = leaders[i]['near'][end]
                root_blocks = sorted({memberships[key] for key in anchors})
                anchor_gap = min(anchors.values())
                ambiguous_path = {root: False}
                for node in order[1:]:
                    self._cancel(feedback)
                    upstream, i, target_end = parent[node]
                    leader = leaders[i]
                    joined.add(i)
                    if parent[upstream] is not None:
                        leader['upstream'] = leaders[parent[upstream][1]]['ref']
                    is_junction = len(adjacency[node]) > 1
                    has_target = is_junction and bool(target_at(leader, target_end)[0])
                    ambiguous_path[node] = ambiguous_path[upstream] or has_target
                    if not is_junction or has_target:
                        reason = ('A connection along this leader could also be a polygon target.'
                                  if ambiguous_path[node] else '')
                        assign(leader, target_end, root_blocks, anchor_gap, reason)
                    else:
                        leader.update(status='connector', reason='Shared leader path to downstream endpoints.',
                                      candidate_blocks=root_blocks)
                        if len(root_blocks) == 1:
                            leader['block'] = root_blocks[0]

        for i, leader in enumerate(leaders):
            self._cancel(feedback)
            if leader['error'] or i in joined:
                continue
            first, last = leader['near']
            if first and last:
                reason = 'Text is near both leader ends; check which end points to each polygon.'
                candidates = set()
                for target_end, anchors in ((1, first), (0, last)):
                    ids, _, _ = target_at(leader, target_end)
                    self._link_candidates(leader, polygons, ids, {memberships[key] for key in anchors}, reason)
                    candidates.update(ids)
                leader.update(status='review', reason=reason, polygons=sorted(candidates),
                              candidate_blocks=sorted({memberships[key] for key in first.keys() | last.keys()}))
            elif first or last:
                anchors = first or last
                assign(leader, 1 if first else 0, sorted({memberships[key] for key in anchors}), min(anchors.values()))
            else:
                leader['reason'] = 'Neither endpoint is close to a text anchor or connected by a traceable leader path.'

    def _sink(self, parameters, name, fields, geometry_type, crs, context):
        destination = self.parameterAsOutputLayer(parameters, name, context)
        if destination.lower().split('|', 1)[0].endswith(('.shp', '.dbf')):
            raise QgsProcessingException('Choose a GeoPackage or temporary output: Shapefile/DBF would truncate description text and field names.')
        sink, destination = self.parameterAsSink(parameters, name, context, fields, geometry_type, crs)
        if sink is None:
            raise QgsProcessingException(f'Could not create output {name}. Choose an output destination.')
        return sink, destination

    def _validate_destinations(self, parameters, context, input_layers, output_names):
        def file_key(uri, provider='ogr'):
            decoded = QgsProviderRegistry.instance().decodeUri(provider, uri)
            path = decoded.get('path')
            if not path or str(path).startswith(('memory:', 'postgres:', '/vsi')):
                return None
            return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))

        inputs = {file_key(layer.source(), layer.providerType()) for layer in input_layers if layer is not None}
        destinations = set()
        for name in output_names:
            destination = self.parameterAsOutputLayer(parameters, name, context)
            if destination.lower().split('|', 1)[0].endswith(('.shp', '.dbf')):
                raise QgsProcessingException('Choose a GeoPackage or temporary output: Shapefile/DBF would truncate description text and field names.')
            key = file_key(destination)
            if key is None:
                continue
            if key in inputs:
                raise QgsProcessingException('An output points to an input data file. Choose a new GeoPackage to preserve the source layers.')
            if key in destinations:
                raise QgsProcessingException('Choose separate files or temporary layers for each output to avoid replacing another result.')
            destinations.add(key)

    @staticmethod
    def _write(sink, fields, attributes, geometry):
        feature = QgsFeature(fields)
        feature.setAttributes(attributes)
        if geometry and not geometry.isNull():
            feature.setGeometry(geometry)
        if not sink.addFeature(feature):
            raise QgsProcessingException('Could not write an association output feature: ' + sink.lastError())

    def processAlgorithm(self, parameters, context, feedback):
        self._cancel(feedback)
        source = self.parameterAsSource(parameters, self.TEXT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.TEXT))
        if not source.sourceCrs().isValid():
            raise QgsProcessingException('The description point layer must have a valid CRS.')
        text_field = self._text_field(source, self.parameterAsString(parameters, self.TEXT_FIELD, context))
        feedback.pushInfo('Description text field: ' + text_field)
        rotation = self._optional_field(source, self.parameterAsString(parameters, self.ROTATION_FIELD, context), 'label_rotation')
        alignment = self._optional_field(source, self.parameterAsString(parameters, self.ALIGNMENT_FIELD, context), 'label_alignment')
        if not rotation:
            feedback.pushInfo('No rotation field: text is assumed horizontal in its source CRS.')
        if not alignment:
            feedback.pushInfo('No alignment field: text grouping uses anchor positions without justification metadata.')
        metric = EllipsoidalMetric(context)
        output_crs = source.sourceCrs()
        feedback.pushInfo('Measurements use the WGS84 ellipsoid and local coordinates at each target; there is no route-extent limit. '
                          'Outputs use the description point CRS.')
        if self.parameterAsCrs(parameters, self.WORKING_CRS, context).isValid():
            feedback.pushInfo('The legacy measurement CRS preference is ignored; ellipsoidal measurements are automatic.')
        transform = QgsCoordinateTransform(output_crs, metric.crs, context.transformContext())
        records, lines = self._read_text(source, text_field, rotation, alignment, transform, metric, feedback)
        positions = {line.key: QgsPointXY(line.x, line.y) for line in lines}
        text_index = GlobalPointIndex(positions, metric, lambda: self._cancel(feedback))
        feedback.setProgress(15)
        gap = self.parameterAsDouble(parameters, self.MAX_LINE_GAP, context)
        anchor_tolerance = self.parameterAsDouble(parameters, self.ANCHOR_TOLERANCE, context)
        boundary_tolerance = self.parameterAsDouble(parameters, self.BOUNDARY_TOLERANCE, context)
        if any(not math.isfinite(value) or value < 0 for value in (gap, anchor_tolerance, boundary_tolerance)):
            raise QgsProcessingException('Tolerances must be finite, non-negative distances in metres.')
        reliable_gap = True
        if gap == 0:
            gap, reliable_gap, message = estimate_line_gap(lines, self._pairs(lines, text_index, 0, feedback, nearest=True), metric)
            feedback.pushInfo(message)
        leader_layers = self.parameterAsLayerList(parameters, self.LEADERS, context)
        polygon_layers = self.parameterAsLayerList(parameters, self.POLYGONS, context)
        output_names = {self.OUTPUT_LABELS}
        for name, layers in ((self.OUTPUT_CHECKS, leader_layers), (self.OUTPUT_POLYGONS, polygon_layers)):
            if layers and self.parameterAsOutputLayer(parameters, name, context) not in ('', 'SKIP_OUTPUT'):
                output_names.add(name)
        self._validate_destinations(parameters, context,
                                    [self.parameterAsVectorLayer(parameters, self.TEXT, context)] + leader_layers + polygon_layers,
                                    output_names)
        anchor_positions = {line.key: metric.position(line) for line in lines}
        leaders, anchored = self._read_leaders(leader_layers, metric, output_crs, text_index,
                                              anchor_positions, anchor_tolerance, context, feedback)
        blocks = assemble_blocks(
            lines, self._pairs(lines, text_index, max(gap, 1e-6), feedback), gap,
            anchored, reliable_gap, lambda: self._cancel(feedback), metric,
            allow_multiple_anchors=self.parameterAsBoolean(parameters, self.ALLOW_MULTIPLE_ANCHORS, context))
        for key, record in records.items():
            if record['error']:
                blocks.append(TextBlock((key,), key, record['text'], 'review', record['error']))
        blocks.sort(key=lambda block: min(block.members))
        feedback.setProgress(40)
        polygon_fields, polygons, polygon_index = self._read_polygons(
            polygon_layers, output_crs, context, feedback)
        feedback.setProgress(65)
        self._associate(leaders, blocks, polygons, polygon_index, boundary_tolerance, bool(polygon_layers), feedback)
        result = self._outputs(parameters, context, feedback, source, records, blocks, leaders, polygons,
                               polygon_fields, output_crs, bool(polygon_layers), output_names)
        feedback.pushInfo('Text blocks: ' + ', '.join(f'{name}={count}' for name, count in sorted(Counter(b.status for b in blocks).items())))
        feedback.pushInfo('Leader checks: ' + ', '.join(f'{name}={count}' for name, count in sorted(Counter(row['status'] for row in leaders).items())))
        feedback.pushWarning('Experimental: statuses describe geometric evidence, not verified soil interpretation. Review candidate_text and reasons before use.')
        feedback.setProgress(100)
        return result

    def postProcessAlgorithm(self, context, feedback):
        # QgsProcessingAlgorithm.run() destroys its execution clone before the
        # GUI loads outputs. C++ owns the postprocessor, but its Python override
        # also needs a live wrapper. Retain it on the final (main-thread) context,
        # not the temporary background context or a shared singleton.
        processor = getattr(self, '_label_postprocessor', None)
        if processor is not None:
            if not hasattr(context, '_subsea_annotation_postprocessors'):
                context._subsea_annotation_postprocessors = []
            context._subsea_annotation_postprocessors.append(processor)
        return {}

    def _outputs(self, parameters, context, feedback, source, records, blocks, leaders, polygons,
                 polygon_fields, crs, have_polygons, output_names):
        result = {}
        run_id = uuid4().hex
        review_fields = [(name, FIELD_TYPE_STRING) for name in REVIEW_FIELDS]
        block_names = {i: f'B{i + 1:06d}' for i in range(len(blocks))}
        block_links = defaultdict(list)
        for leader in leaders:
            for block_id in leader['candidate_blocks']:
                block_links[block_id].append(leader)
        fields = _fields([(name, FIELD_TYPE_STRING) for name in
                          ('block_id', 'assembled_text', 'candidate_text', 'text_status', 'text_reason',
                           'target_status', 'polygon_refs', 'source_layer', 'source_fids')]
                         + [('fragment_count', FIELD_TYPE_INT), ('label_rotation', FIELD_TYPE_DOUBLE)] + review_fields)
        sink, dest = self._sink(parameters, self.OUTPUT_LABELS, fields, WKB_POINT, crs, context)
        result[self.OUTPUT_LABELS] = dest
        for i, block in enumerate(blocks):
            self._cancel(feedback)
            links = block_links[i]
            refs = sorted({polygons[key]['ref'] for link in links for key in link['polygons']})
            target_status = ('not_requested' if not have_polygons else 'unmatched')
            if any(link['status'] == 'review' for link in links):
                target_status = 'review'
            elif any(link['status'] == 'matched' for link in links):
                target_status = 'matched' if len(refs) == 1 else 'multiple_targets'
                if any(link['status'] == 'unmatched' for link in links):
                    target_status = 'partial'
            record = records[block.anchor]
            self._write(sink, fields, [block_names[i], block.text if block.status != 'review' else None,
                                      block.text, block.status, block.reason, target_status, _json(refs),
                                      source.sourceName(), _json(list(block.members)), len(block.members), record['rotation'], run_id, None],
                        record['geometry'])
        if context.willLoadLayerOnCompletion(dest):
            # Retain the Python wrapper until Processing has applied it.
            self._label_postprocessor = _LabelPoints()
            context.layerToLoadOnCompletionDetails(dest).setPostProcessor(self._label_postprocessor)
        if self.OUTPUT_POLYGONS in output_names:
            generated = [_new_field(polygon_fields, name, FIELD_TYPE_STRING) for name in POLYGON_FIELDS + REVIEW_FIELDS]
            feedback.pushInfo('Polygon result fields: ' + ', '.join(generated))
            output_type = QgsWkbTypes.MultiPolygon
            if any(QgsWkbTypes.hasZ(row['geometry'].wkbType()) for row in polygons):
                output_type = QgsWkbTypes.addZ(output_type)
            if any(QgsWkbTypes.hasM(row['geometry'].wkbType()) for row in polygons):
                output_type = QgsWkbTypes.addM(output_type)
            sink, dest = self._sink(parameters, self.OUTPUT_POLYGONS, polygon_fields, output_type, crs, context)
            result[self.OUTPUT_POLYGONS] = dest
            statuses = Counter()
            for polygon in polygons:
                self._cancel(feedback)
                ids = sorted(polygon['blocks'])
                unique = {}
                for i in ids:
                    unique.setdefault(normalized_text(blocks[i].text), blocks[i].text)
                candidate = '\n\n'.join(unique.values()) or None
                notes = sorted(polygon['review'])
                if polygon['error']:
                    notes.append(polygon['error'])
                status = 'review' if notes else ('multiple_descriptions' if len(unique) > 1 else 'matched' if ids else 'unmatched')
                if status == 'multiple_descriptions':
                    notes.append('Different descriptions point to this polygon; review the candidate blocks.')
                elif status == 'unmatched':
                    notes.append('No accepted leader association; no nearest-label guess made.')
                statuses[status] += 1
                self._write(sink, polygon_fields, polygon['attrs'] + [candidate if status == 'matched' else None,
                            candidate, status, ' '.join(notes), polygon['ref'], polygon['layer'], polygon['layer_id'], str(polygon['fid']),
                            _json([block_names[i] for i in ids]), _json(sorted(polygon['leaders'])), run_id, None], polygon['geometry'])
            feedback.pushInfo('Polygon results: ' + ', '.join(f'{name}={count}' for name, count in sorted(statuses.items())))
        if self.OUTPUT_CHECKS in output_names:
            fields = _fields([(name, FIELD_TYPE_STRING) for name in
                              ('leader_ref', 'source_layer', 'source_layer_id', 'source_feature_id', 'block_id', 'candidate_block_ids',
                               'polygon_refs', 'match_status', 'match_reason', 'upstream_leader_ref')]
                             + [('part_number', FIELD_TYPE_INT), ('anchor_gap_m', FIELD_TYPE_DOUBLE), ('boundary_gap_m', FIELD_TYPE_DOUBLE)] + review_fields)
            sink, dest = self._sink(parameters, self.OUTPUT_CHECKS, fields, WKB_LINESTRING, crs, context)
            result[self.OUTPUT_CHECKS] = dest
            for row in leaders:
                self._cancel(feedback)
                self._write(sink, fields, [row['ref'], row['layer'], row['layer_id'], str(row['fid']), block_names.get(row['block']),
                            _json([block_names[i] for i in row['candidate_blocks']]),
                            _json([polygons[key]['ref'] for key in row['polygons']]), row['status'], row['reason'],
                            row['upstream'], row['part'], row['gap'], row['target_gap'], run_id, None], row['geometry'])
        return result
