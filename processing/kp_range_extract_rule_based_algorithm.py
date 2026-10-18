# kp_range_extract_rule_based_algorithm.py
# -*- coding: utf-8 -*-
"""ExtractKPRangesRuleBasedAlgorithm

MVP (simplified): Extract KP ranges by categorising an RPL line layer by a
chosen attribute field, similar to QGIS 'Categorized' symbology.

For each input RPL feature (in route order: SeqNo when every feature has one,
else layer order), the algorithm produces a KP range covering that feature and
carries the chosen field value as the class.
Optionally merges adjacent ranges with the same class value.

Outputs:
- KP ranges table (no geometry)
- Optional extracted segment geometries for map preview/styling

Notes:
- KPs are in km: start KP + chainage (docs/KP_METHOD.md).
- Boundaries occur at input feature boundaries (this is intentional for the MVP).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsWkbTypes,
)
from .algorithm_base import SubseaCableAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING, PROCESSING_FIELD_ANY, PROCESSING_NUMBER_DOUBLE

from ..kp_range_utils import (
    make_distance_area,
    add_distance_mode_parameter,
    read_distance_mode,
)
from ..kp_geo_utils import RouteFrame, get_features_skip_invalid, ordered_route_features
from .kp_common import START_KP_HELP, add_start_kp_parameter, build_route, read_start_kp, report_kp_basis


@dataclass
class _Range:
    start_kp: float
    end_kp: float
    attrs: Dict[str, Any]


class ExtractKPRangesRuleBasedAlgorithm(SubseaCableAlgorithm):
    INPUT_RPL = 'INPUT_RPL'

    CATEGORY_FIELD = 'CATEGORY_FIELD'

    # Output shaping
    MERGE_ADJACENT = 'MERGE_ADJACENT'
    ADJ_TOL_KM = 'ADJ_TOL_KM'
    MIN_RANGE_KM = 'MIN_RANGE_KM'
    CREATE_SEGMENTS = 'CREATE_SEGMENTS'

    OUTPUT_RANGES = 'OUTPUT_RANGES'
    OUTPUT_SEGMENTS = 'OUTPUT_SEGMENTS'

    _FIELD_START_KP = 'start_kp'
    _FIELD_END_KP = 'end_kp'
    _FIELD_CLASS_FIELD = 'class_field'
    _FIELD_CLASS_VALUE = 'class_value'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_RPL,
                self.tr('Reference Line Layer'),
                [QgsProcessing.TypeVectorLine],
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.CATEGORY_FIELD,
                self.tr('Categorize by field'),
                parentLayerParameterName=self.INPUT_RPL,
                type=PROCESSING_FIELD_ANY,
            )
        )

        # ---------------- Output shaping ----------------
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.MERGE_ADJACENT,
                self.tr('Merge adjacent ranges'),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.ADJ_TOL_KM,
                self.tr('Adjacency tolerance (km)'),
                type=PROCESSING_NUMBER_DOUBLE,
                minValue=0.0,
                defaultValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_RANGE_KM,
                self.tr('Minimum output range length (km) (0 = keep all)'),
                type=PROCESSING_NUMBER_DOUBLE,
                minValue=0.0,
                defaultValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CREATE_SEGMENTS,
                self.tr('Create segment geometry output (for map preview)'),
                defaultValue=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_RANGES,
                self.tr('KP ranges (table)'),
                QgsProcessing.TypeVector,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_SEGMENTS,
                self.tr('KP range segments (line) (optional)'),
                QgsProcessing.TypeVectorLine,
                optional=True,
            )
        )

        add_distance_mode_parameter(self)
        add_start_kp_parameter(self)

    # --------------------------- Core ---------------------------

    def processAlgorithm(self, parameters, context, feedback):
        rpl_source = self.parameterAsSource(parameters, self.INPUT_RPL, context)
        if rpl_source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_RPL))

        category_field = (self.parameterAsString(parameters, self.CATEGORY_FIELD, context) or '').strip()
        if not category_field:
            raise QgsProcessingException(self.tr('Please select a category field.'))
        if rpl_source.fields().indexFromName(category_field) < 0:
            raise QgsProcessingException(self.tr('Category field not found in input layer.'))

        merge_adjacent = bool(self.parameterAsBool(parameters, self.MERGE_ADJACENT, context))
        adj_tol_km = float(self.parameterAsDouble(parameters, self.ADJ_TOL_KM, context))
        min_range_km = float(self.parameterAsDouble(parameters, self.MIN_RANGE_KM, context))
        create_segments = bool(self.parameterAsBool(parameters, self.CREATE_SEGMENTS, context))

        line_crs = rpl_source.sourceCrs()

        distance_mode = read_distance_mode(self, parameters, context)
        try:
            distance_area = make_distance_area(
                line_crs, context.transformContext(),
                mode=distance_mode, project=context.project(),
            )
        except ValueError as exc:
            raise QgsProcessingException(str(exc))

        # Route features in chainage order (SeqNo when every feature has
        # one, else layer order), one RouteFrame over them with the start KP:
        # each feature's KP range is where it starts and ends on the route.
        route_features = [f for f in ordered_route_features(get_features_skip_invalid(rpl_source))
                          if self._has_line_parts(f.geometry())]
        if not route_features:
            raise QgsProcessingException(self.tr('Input RPL has no valid line geometry.'))

        start_kp = read_start_kp(self, parameters, context)
        route = build_route([QgsGeometry(f.geometry()) for f in route_features], distance_area, start_kp)
        report_kp_basis(feedback, route, start_kp)
        if route.total_length_m <= 0:
            raise QgsProcessingException(self.tr('Route length is zero.'))

        # Output fields (canonical; keep stable for downstream tools)
        out_fields = QgsFields()
        out_fields.append(QgsField(self._FIELD_START_KP, FIELD_TYPE_DOUBLE))
        out_fields.append(QgsField(self._FIELD_END_KP, FIELD_TYPE_DOUBLE))
        out_fields.append(QgsField(self._FIELD_CLASS_FIELD, FIELD_TYPE_STRING, '', 254, 0))
        out_fields.append(QgsField(self._FIELD_CLASS_VALUE, FIELD_TYPE_STRING, '', 254, 0))

        ranges_sink, ranges_dest_id = self.parameterAsSink(
            parameters,
            self.OUTPUT_RANGES,
            context,
            out_fields,
            QgsWkbTypes.NoGeometry,
            line_crs,
        )
        if ranges_sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_RANGES))

        segments_sink = None
        segments_dest_id = None
        if create_segments:
            segments_sink, segments_dest_id = self.parameterAsSink(
                parameters,
                self.OUTPUT_SEGMENTS,
                context,
                out_fields,
                QgsWkbTypes.LineString,
                line_crs,
            )

        extracted_ranges: List[_Range] = []

        extracted_ranges = self._extract_categorized_ranges(
            route_features,
            route,
            category_field,
            feedback,
        )

        if merge_adjacent and extracted_ranges:
            extracted_ranges = self._merge_adjacent(extracted_ranges, adj_tol_km)

        if min_range_km > 0:
            extracted_ranges = [r for r in extracted_ranges if (r.end_kp - r.start_kp) >= min_range_km]

        # Write outputs
        for r in extracted_ranges:
            attrs = r.attrs
            row = QgsFeature(out_fields)
            row.setAttributes(
                [
                    float(r.start_kp),
                    float(r.end_kp),
                    str(attrs.get(self._FIELD_CLASS_FIELD, '') or ''),
                    str(attrs.get(self._FIELD_CLASS_VALUE, '') or ''),
                ]
            )
            ranges_sink.addFeature(row, QgsFeatureSink.FastInsert)

            if segments_sink is not None:
                # The slice of the route's KP path (geodesic or grid line
                # between vertices), as the other KP tools slice it.
                seg_geom = route.extract_segment(r.start_kp, r.end_kp)
                if seg_geom is None or seg_geom.isEmpty():
                    continue
                seg_feat = QgsFeature(out_fields)
                seg_feat.setGeometry(seg_geom)
                seg_feat.setAttributes(row.attributes())
                segments_sink.addFeature(seg_feat, QgsFeatureSink.FastInsert)

        result = {self.OUTPUT_RANGES: ranges_dest_id}
        if segments_dest_id is not None:
            result[self.OUTPUT_SEGMENTS] = segments_dest_id
        return result

    # --------------------------- Categorized-by-field ---------------------------

    def _extract_categorized_ranges(
        self,
        route_features: Sequence[QgsFeature],
        route: RouteFrame,
        category_field: str,
        feedback,
    ) -> List[_Range]:
        """One range per route feature: from the KP where it starts on
        ``route`` (start KP + chainage) to where it ends."""
        out: List[_Range] = []
        total = len(route_features)
        offsets = route.feature_offsets_m + [route.total_length_m]

        for idx, feat in enumerate(route_features):
            if feedback.isCanceled():
                break

            base_m = float(offsets[idx])
            length_m = float(offsets[idx + 1]) - base_m
            if length_m <= 0:
                continue

            try:
                v = feat[category_field]
            except Exception:
                v = None

            value_text = '<NULL>' if v is None else str(v)

            start_kp = route.start_kp_km + base_m / 1000.0
            end_kp = route.start_kp_km + (base_m + length_m) / 1000.0
            out.append(
                _Range(
                    start_kp=float(start_kp),
                    end_kp=float(end_kp),
                    attrs={
                        self._FIELD_CLASS_FIELD: category_field,
                        self._FIELD_CLASS_VALUE: value_text,
                    },
                )
            )

            if total > 0:
                feedback.setProgress(int((idx + 1) * 100 / total))

        return out

    # --------------------------- Geometry helpers ---------------------------

    @staticmethod
    def _has_line_parts(geom: QgsGeometry) -> bool:
        """True when ``geom`` is a (multi)line with at least one non-empty part."""
        if geom is None or geom.isEmpty():
            return False
        try:
            if geom.isMultipart():
                return any(part for part in geom.asMultiPolyline())
            return bool(geom.asPolyline())
        except Exception:
            return False

    @staticmethod
    def _merge_adjacent(ranges: Sequence[_Range], tol_km: float) -> List[_Range]:
        if not ranges:
            return []
        tol = max(0.0, float(tol_km))
        items = sorted(ranges, key=lambda r: (float(r.start_kp), float(r.end_kp)))
        merged: List[_Range] = []

        def _key(r: _Range) -> Tuple[str, str]:
            a = r.attrs
            return (
                str(a.get(ExtractKPRangesRuleBasedAlgorithm._FIELD_CLASS_FIELD, '') or ''),
                str(a.get(ExtractKPRangesRuleBasedAlgorithm._FIELD_CLASS_VALUE, '') or ''),
            )

        cur = items[0]
        for nxt in items[1:]:
            if _key(cur) == _key(nxt) and float(nxt.start_kp) <= float(cur.end_kp) + tol:
                cur = _Range(
                    start_kp=float(cur.start_kp),
                    end_kp=max(float(cur.end_kp), float(nxt.end_kp)),
                    attrs={**cur.attrs},
                )
            else:
                merged.append(cur)
                cur = nxt
        merged.append(cur)
        return merged

    # --------------------------- Metadata ---------------------------

    def name(self):
        return 'kp_range_extract_rule_based'

    def displayName(self):
        return self.tr('Extract KP Ranges (Rule Based)')

    def group(self):
        return self.tr('KP Ranges')

    def groupId(self):
        return 'kp_ranges'

    def shortHelpString(self):
        return self.tr(
            """
Extract KP ranges by categorising an RPL by a chosen attribute field.

This works like QGIS 'Categorized' symbology:
- Choose a field
- Each input feature becomes a KP interval with that field value as the class
- Optionally merge adjacent intervals with the same class value

Outputs
- KP ranges (table): start_kp, end_kp, class_field, class_value
- Optional KP range segments: extracted line geometries for map preview/styling

Notes
- KPs are in km: start KP + chainage along the route.
- Boundaries occur at input feature boundaries.
- Segment geometries follow the route's KP path: between vertices along the geodesic (or the straight grid line for grid KP).

Route ordering
- SeqNo order when every feature has a SeqNo, else the input layer feature order (never noded or re-ordered otherwise).
"""
        ) + "\n\n" + self.tr(START_KP_HELP)

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return ExtractKPRangesRuleBasedAlgorithm()
