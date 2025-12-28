# kp_range_group_adjacent_algorithm.py
# -*- coding: utf-8 -*-
"""KPRangeGroupAdjacentAlgorithm

Groups/merges adjacent KP ranges when a selected attribute value is unchanged.

Example input:
    start_kp,end_kp,burial_method
    1,2,plough
    2,3,plough
    3,4,skip
    4,5,skip
    5,6,plough

Output:
    1,3,plough
    3,5,skip
    5,6,plough

Notes about other fields:
- By default, non KP fields are copied from the first row in each merged group.
- Optionally, you can set non KP fields to NULL if a conflict exists within the
  merged group.

The algorithm supports both table inputs (no geometry) and vector layers.
If geometries exist, geometries are merged per group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsSettings,
    QgsWkbTypes,
)


@dataclass
class _Row:
    start: float
    end: float
    group_value: Any
    attrs: Dict[str, Any]
    geom: Optional[QgsGeometry]


class KPRangeGroupAdjacentAlgorithm(QgsProcessingAlgorithm):
    INPUT = 'INPUT'
    START_FIELD = 'START_FIELD'
    END_FIELD = 'END_FIELD'
    GROUP_FIELD = 'GROUP_FIELD'

    SORT_INPUT = 'SORT_INPUT'
    AUTO_SWAP = 'AUTO_SWAP'
    TOLERANCE = 'TOLERANCE'
    OTHER_FIELDS = 'OTHER_FIELDS'

    OUTPUT = 'OUTPUT'

    _SETTINGS_PREFIX = 'subsea_cable_tools/kp_range_group_adjacent/'

    def initAlgorithm(self, config=None):
        settings = QgsSettings()
        get_i = lambda k, d=0: int(settings.value(self._SETTINGS_PREFIX + k, d))
        get_b = lambda k, d=False: bool(settings.value(self._SETTINGS_PREFIX + k, d))
        get_f = lambda k, d='': str(settings.value(self._SETTINGS_PREFIX + k, d))
        get_d = lambda k, d=0.0: float(settings.value(self._SETTINGS_PREFIX + k, d))

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Input KP ranges (table or layer)'),
                [QgsProcessing.TypeVector],
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.START_FIELD,
                self.tr('Start KP field'),
                parentLayerParameterName=self.INPUT,
                type=QgsProcessingParameterField.Numeric,
                defaultValue=get_f(self.START_FIELD, ''),
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.END_FIELD,
                self.tr('End KP field'),
                parentLayerParameterName=self.INPUT,
                type=QgsProcessingParameterField.Numeric,
                defaultValue=get_f(self.END_FIELD, ''),
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.GROUP_FIELD,
                self.tr('Group by field (merge adjacent ranges with same value)'),
                parentLayerParameterName=self.INPUT,
                type=QgsProcessingParameterField.Any,
                defaultValue=get_f(self.GROUP_FIELD, ''),
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SORT_INPUT,
                self.tr('Sort input by Start/End KP before grouping'),
                defaultValue=get_b(self.SORT_INPUT, True),
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.AUTO_SWAP,
                self.tr('Auto-swap Start/End if Start > End'),
                defaultValue=get_b(self.AUTO_SWAP, True),
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.TOLERANCE,
                self.tr('Adjacency tolerance (km)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=get_d(self.TOLERANCE, 0.0),
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.OTHER_FIELDS,
                self.tr('How to handle other fields when merging'),
                options=[
                    self.tr('Keep values from first row in group'),
                    self.tr('Set to NULL if values differ within group'),
                ],
                defaultValue=get_i(self.OTHER_FIELDS, 0),
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Grouped KP ranges'),
                QgsProcessing.TypeVector,
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.tr('Invalid input layer/table.'))

        start_field = self.parameterAsString(parameters, self.START_FIELD, context)
        end_field = self.parameterAsString(parameters, self.END_FIELD, context)
        group_field = self.parameterAsString(parameters, self.GROUP_FIELD, context)

        sort_input = self.parameterAsBool(parameters, self.SORT_INPUT, context)
        auto_swap = self.parameterAsBool(parameters, self.AUTO_SWAP, context)
        tol = float(self.parameterAsDouble(parameters, self.TOLERANCE, context))
        other_mode = self.parameterAsEnum(parameters, self.OTHER_FIELDS, context)

        settings = QgsSettings()
        settings.setValue(self._SETTINGS_PREFIX + self.START_FIELD, start_field)
        settings.setValue(self._SETTINGS_PREFIX + self.END_FIELD, end_field)
        settings.setValue(self._SETTINGS_PREFIX + self.GROUP_FIELD, group_field)
        settings.setValue(self._SETTINGS_PREFIX + self.SORT_INPUT, sort_input)
        settings.setValue(self._SETTINGS_PREFIX + self.AUTO_SWAP, auto_swap)
        settings.setValue(self._SETTINGS_PREFIX + self.TOLERANCE, tol)
        settings.setValue(self._SETTINGS_PREFIX + self.OTHER_FIELDS, other_mode)

        in_fields: QgsFields = source.fields()
        if in_fields.indexFromName(start_field) < 0 or in_fields.indexFromName(end_field) < 0:
            raise QgsProcessingException(self.tr('Start/End KP fields not found in input.'))
        if in_fields.indexFromName(group_field) < 0:
            raise QgsProcessingException(self.tr('Group-by field not found in input.'))

        wkb_type = source.wkbType() if hasattr(source, 'wkbType') else QgsWkbTypes.NoGeometry
        out_wkb = wkb_type
        if out_wkb is None:
            out_wkb = QgsWkbTypes.NoGeometry

        sink_fields = QgsFields(in_fields)

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            sink_fields,
            out_wkb,
            source.sourceCrs(),
        )

        if sink is None:
            raise QgsProcessingException(self.tr('Could not create output sink.'))

        rows: List[_Row] = []
        total = source.featureCount() or 0
        for i, f in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break

            try:
                start_val = float(f[start_field])
                end_val = float(f[end_field])
            except Exception:
                feedback.reportError(self.tr(f"Skipping feature {f.id()} due to non-numeric KP values."))
                continue

            if auto_swap and start_val > end_val:
                start_val, end_val = end_val, start_val

            geom = f.geometry() if f.hasGeometry() else None
            attrs = {field.name(): f[field.name()] for field in in_fields}
            rows.append(_Row(start=start_val, end=end_val, group_value=f[group_field], attrs=attrs, geom=geom))

            if total:
                feedback.setProgress(int((i + 1) / total * 40))

        if not rows:
            feedback.pushInfo(self.tr('No valid rows found in input.'))
            return {self.OUTPUT: dest_id}

        if sort_input:
            rows.sort(key=lambda r: (r.start, r.end))

        def is_adjacent(prev_end: float, next_start: float) -> bool:
            return abs(prev_end - next_start) <= tol

        def equal_or_both_null(a: Any, b: Any) -> bool:
            if a is None and b is None:
                return True
            return a == b

        def merge_attrs(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
            if other_mode == 0:
                return base

            # other_mode == 1: null-out conflicts
            out = dict(base)
            for field in in_fields:
                name = field.name()
                if name in (start_field, end_field, group_field):
                    continue
                if not equal_or_both_null(out.get(name), incoming.get(name)):
                    out[name] = None
            return out

        groups: List[Tuple[float, float, Any, Dict[str, Any], List[QgsGeometry]]] = []

        current_start = rows[0].start
        current_end = rows[0].end
        current_group_val = rows[0].group_value
        current_attrs = dict(rows[0].attrs)
        current_geoms: List[QgsGeometry] = []
        if rows[0].geom is not None and not rows[0].geom.isEmpty():
            current_geoms.append(rows[0].geom)

        for idx, row in enumerate(rows[1:], start=1):
            if feedback.isCanceled():
                break

            can_merge = (row.group_value == current_group_val) and is_adjacent(current_end, row.start)
            if can_merge:
                current_end = max(current_end, row.end)
                current_attrs = merge_attrs(current_attrs, row.attrs)
                if row.geom is not None and not row.geom.isEmpty():
                    current_geoms.append(row.geom)
            else:
                groups.append((current_start, current_end, current_group_val, current_attrs, current_geoms))
                current_start = row.start
                current_end = row.end
                current_group_val = row.group_value
                current_attrs = dict(row.attrs)
                current_geoms = []
                if row.geom is not None and not row.geom.isEmpty():
                    current_geoms.append(row.geom)

            if total:
                feedback.setProgress(40 + int((idx + 1) / max(total, 1) * 30))

        groups.append((current_start, current_end, current_group_val, current_attrs, current_geoms))

        # Write output
        for j, (g_start, g_end, g_val, attrs, geoms) in enumerate(groups):
            if feedback.isCanceled():
                break

            out_feat = QgsFeature(sink_fields)
            if out_wkb != QgsWkbTypes.NoGeometry and geoms:
                try:
                    if len(geoms) == 1:
                        out_feat.setGeometry(geoms[0])
                    else:
                        out_feat.setGeometry(QgsGeometry.unaryUnion(geoms))
                except Exception:
                    # Fallback: keep the first geometry
                    out_feat.setGeometry(geoms[0])

            attrs = dict(attrs)
            attrs[start_field] = g_start
            attrs[end_field] = g_end
            attrs[group_field] = g_val

            out_feat.setAttributes([attrs.get(f.name()) for f in sink_fields])
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)

            feedback.setProgress(70 + int((j + 1) / max(len(groups), 1) * 30))

        feedback.pushInfo(self.tr(f'Created {len(groups)} grouped KP ranges (from {len(rows)} input rows).'))
        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr(
            """
Groups/merges adjacent KP ranges where the chosen "Group by" field value is the same.

The input can be a table (no geometry) or a vector layer.

**How it decides whether two rows can be merged**
- The group field value must be equal
- Row N end KP must match Row N+1 start KP (within the tolerance)
- Optionally, the input is sorted by Start/End KP before grouping

**Other fields**
- Keep first row values (default)
- Or set fields to NULL if they differ anywhere within the merged group
"""
        )

    def name(self):
        return 'kp_range_group_adjacent'

    def displayName(self):
        return self.tr('Group Adjacent KP Ranges by Field')

    def group(self):
        return self.tr('KP Ranges')

    def groupId(self):
        return 'kp_ranges'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return KPRangeGroupAdjacentAlgorithm()
