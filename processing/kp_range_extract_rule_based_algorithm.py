# kp_range_extract_rule_based_algorithm.py
# -*- coding: utf-8 -*-
"""ExtractKPRangesRuleBasedAlgorithm

MVP (simplified): Extract KP ranges by categorising an RPL line layer by a
chosen attribute field, similar to QGIS 'Categorized' symbology.

For each input RPL feature (in route order), the algorithm produces a KP range
covering that feature and carries the chosen field value as the class.
Optionally merges adjacent ranges with the same class value.

Outputs:
- KP ranges table (no geometry)
- Optional extracted segment geometries for map preview/styling

Notes:
- KPs are in km.
- Boundaries occur at input feature boundaries (this is intentional for the MVP).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsWkbTypes,
)

from ..kp_range_utils import extract_line_segment, measure_total_length_m


@dataclass
class _Range:
    start_kp: float
    end_kp: float
    attrs: Dict[str, Any]


class ExtractKPRangesRuleBasedAlgorithm(QgsProcessingAlgorithm):
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
                self.tr('Input RPL line layer'),
                [QgsProcessing.TypeVectorLine],
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.CATEGORY_FIELD,
                self.tr('Categorize by field'),
                parentLayerParameterName=self.INPUT_RPL,
                type=QgsProcessingParameterField.Any,
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
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_RANGE_KM,
                self.tr('Minimum output range length (km) (0 = keep all)'),
                type=QgsProcessingParameterNumber.Double,
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

        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(line_crs, context.transformContext())
        distance_area.setEllipsoid(context.project().ellipsoid())

        route_parts, per_feature_parts = self._collect_route_parts(rpl_source, feedback)
        if not route_parts:
            raise QgsProcessingException(self.tr('Input RPL has no valid line geometry.'))

        route_geom = self._route_geometry_from_parts(route_parts)
        if route_geom is None or route_geom.isEmpty():
            raise QgsProcessingException(self.tr('Failed to build a route geometry from input RPL.'))

        route_total_m = measure_total_length_m(route_geom, distance_area)
        if route_total_m <= 0:
            raise QgsProcessingException(self.tr('Route length is zero.'))

        # Output fields (canonical; keep stable for downstream tools)
        out_fields = QgsFields()
        out_fields.append(QgsField(self._FIELD_START_KP, QVariant.Double))
        out_fields.append(QgsField(self._FIELD_END_KP, QVariant.Double))
        out_fields.append(QgsField(self._FIELD_CLASS_FIELD, QVariant.String, '', 254, 0))
        out_fields.append(QgsField(self._FIELD_CLASS_VALUE, QVariant.String, '', 254, 0))

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
            per_feature_parts,
            category_field,
            distance_area,
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
                seg_geom = extract_line_segment(route_geom, r.start_kp, r.end_kp, distance_area)
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
        per_feature_parts: Sequence[Tuple[QgsFeature, List[List[QgsPointXY]]]],
        category_field: str,
        distance_area: QgsDistanceArea,
        feedback,
    ) -> List[_Range]:
        out: List[_Range] = []
        base_m = 0.0
        total = len(per_feature_parts)

        for idx, (feat, _parts) in enumerate(per_feature_parts):
            if feedback.isCanceled():
                break

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue

            length_m = float(measure_total_length_m(geom, distance_area) or 0.0)
            if length_m <= 0:
                continue

            try:
                v = feat[category_field]
            except Exception:
                v = None

            value_text = '<NULL>' if v is None else str(v)

            start_kp = base_m / 1000.0
            end_kp = (base_m + length_m) / 1000.0
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

            base_m += length_m
            if total > 0:
                feedback.setProgress(int((idx + 1) * 100 / total))

        return out

    # --------------------------- Geometry helpers ---------------------------

    @staticmethod
    def _as_line_parts(geom: QgsGeometry) -> List[List[QgsPointXY]]:
        if geom is None or geom.isEmpty():
            return []
        try:
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
                return [[QgsPointXY(p) for p in part] for part in parts if part]
            part = geom.asPolyline()
            return [[QgsPointXY(p) for p in part]] if part else []
        except Exception:
            return []

    def _collect_route_parts(
        self,
        source,
        feedback,
    ) -> Tuple[List[List[QgsPointXY]], List[Tuple[QgsFeature, List[List[QgsPointXY]]]]]:
        feats = list(source.getFeatures(QgsFeatureRequest()))

        route_parts: List[List[QgsPointXY]] = []
        per_feature: List[Tuple[QgsFeature, List[List[QgsPointXY]]]] = []

        for feat in feats:
            if feedback.isCanceled():
                break
            geom = feat.geometry()
            parts = self._as_line_parts(geom)
            if not parts:
                continue
            per_feature.append((feat, parts))
            route_parts.extend(parts)

        return route_parts, per_feature

    @staticmethod
    def _route_geometry_from_parts(parts: Sequence[List[QgsPointXY]]) -> QgsGeometry:
        if not parts:
            return QgsGeometry()
        if len(parts) == 1:
            return QgsGeometry.fromPolylineXY(list(parts[0]))
        return QgsGeometry.fromMultiPolylineXY([list(p) for p in parts])

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
- KPs are in km.
- Boundaries occur at input feature boundaries.

Route ordering
- Uses the input layer feature order.
"""
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return ExtractKPRangesRuleBasedAlgorithm()
