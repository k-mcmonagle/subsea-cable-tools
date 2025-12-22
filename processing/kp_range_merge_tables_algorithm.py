# kp_range_merge_tables_algorithm.py
# -*- coding: utf-8 -*-
"""KPRangeMergeTablesAlgorithm

Merges two attribute tables (or vector layers) containing KP ranges into a
canonical set of non-overlapping KP intervals.

Each output interval is created by splitting at every start/end breakpoint
from either input table. Attributes from each table are then assigned to the
output interval based on coverage.

Typical use-case:
- Table A: kp_from, kp_to, water_depth_from, water_depth_to
- Table B: kp_from, kp_to, burial_method
- Output: kp_from, kp_to, water_depth_from, water_depth_to, burial_method

"""

from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from typing import Any, Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsSettings,
    QgsWkbTypes,
)


@dataclass(frozen=True)
class _RangeFeature:
    start: float
    end: float
    order: int
    attrs: Dict[str, Any]


class _RangeSelector:
    """Efficiently selects a covering range for monotonically increasing segments.

    This avoids O(segments * rows) scans by using a sweep pointer (sorted by start)
    and a heap keyed according to the overlap rule.
    """

    def __init__(self, ranges: Sequence[_RangeFeature], tol: float, overlap_rule: int):
        self._ranges = sorted(ranges, key=lambda r: (r.start, r.end, r.order))
        self._i = 0
        self._tol = tol
        self._overlap_rule = overlap_rule
        self._heap: List[Tuple[Any, ...]] = []

    def choose(self, kp_from: float, kp_to: float, label: str) -> Optional[_RangeFeature]:
        # Add newly-active ranges
        while self._i < len(self._ranges) and self._ranges[self._i].start <= kp_from + self._tol:
            r = self._ranges[self._i]
            length = r.end - r.start
            if self._overlap_rule == 0:
                # Most specific
                heappush(self._heap, (length, r.start, r.end, r.order, r))
            else:
                # First match or Error (both use stable input order)
                heappush(self._heap, (r.order, r.end, r.start, length, r))
            self._i += 1

        def is_covering(rng: _RangeFeature) -> bool:
            return rng.end >= kp_to - self._tol

        # Drop heap entries which cannot cover the current segment
        while self._heap:
            top = self._heap[0][-1]
            if is_covering(top):
                break
            heappop(self._heap)

        if not self._heap:
            return None

        if self._overlap_rule != 2:
            return self._heap[0][-1]

        # Error if more than one covering range exists
        first_entry = heappop(self._heap)

        while self._heap:
            candidate = self._heap[0][-1]
            if is_covering(candidate):
                # Put the first back before raising
                heappush(self._heap, first_entry)
                raise QgsProcessingException(
                    QCoreApplication.translate(
                        'Processing',
                        f'Multiple rows in table {label} cover output interval {kp_from}-{kp_to}. '
                        'Resolve overlaps in the input table, or change the overlap rule.'
                    )
                )
            heappop(self._heap)

        heappush(self._heap, first_entry)
        return first_entry[-1]


class KPRangeMergeTablesAlgorithm(QgsProcessingAlgorithm):
    INPUT_A = 'INPUT_A'
    A_START_FIELD = 'A_START_FIELD'
    A_END_FIELD = 'A_END_FIELD'

    INPUT_B = 'INPUT_B'
    B_START_FIELD = 'B_START_FIELD'
    B_END_FIELD = 'B_END_FIELD'

    MERGE_MODE = 'MERGE_MODE'
    REQUIRE_BOTH = 'REQUIRE_BOTH'
    OVERLAP_RULE = 'OVERLAP_RULE'

    FIELD_NAMING = 'FIELD_NAMING'
    AUTO_SWAP = 'AUTO_SWAP'
    TOLERANCE = 'TOLERANCE'
    DISSOLVE_ADJACENT = 'DISSOLVE_ADJACENT'

    OUTPUT_MODE = 'OUTPUT_MODE'
    B_VALUE_FROM_FIELD = 'B_VALUE_FROM_FIELD'
    B_VALUE_TO_FIELD = 'B_VALUE_TO_FIELD'
    B_SINGLE_VALUE_FIELD = 'B_SINGLE_VALUE_FIELD'
    B_SINGLE_VALUE_AGG = 'B_SINGLE_VALUE_AGG'
    LOOKUP_B_FIELD = 'LOOKUP_B_FIELD'
    LOOKUP_RULE = 'LOOKUP_RULE'
    LOOKUP_MATCH_MODE = 'LOOKUP_MATCH_MODE'
    LOOKUP_OUTPUT_NAME = 'LOOKUP_OUTPUT_NAME'
    REQUIRE_B_COVERAGE = 'REQUIRE_B_COVERAGE'
    ADD_COVERAGE_FIELDS = 'ADD_COVERAGE_FIELDS'

    OUTPUT = 'OUTPUT'

    _SETTINGS_PREFIX = 'subsea_cable_tools/kp_range_merge_tables/'

    def initAlgorithm(self, config=None):
        settings = QgsSettings()
        get_i = lambda k, d=0: int(settings.value(self._SETTINGS_PREFIX + k, d))
        get_b = lambda k, d=False: bool(settings.value(self._SETTINGS_PREFIX + k, d))
        get_s = lambda k, d='': str(settings.value(self._SETTINGS_PREFIX + k, d))
        get_f = lambda k, d='': str(settings.value(self._SETTINGS_PREFIX + k, d))
        get_d = lambda k, d=0.0: float(settings.value(self._SETTINGS_PREFIX + k, d))

        self.addParameter(
            QgsProcessingParameterEnum(
                self.OUTPUT_MODE,
                self.tr('Output mode'),
                options=[
                    self.tr('Canonical segmentation (merge attributes from both tables)'),
                    self.tr('Summarise Table B values into Table A ranges (min/max)'),
                    self.tr('Lookup: copy one field from Table B into Table A'),
                ],
                defaultValue=get_i(self.OUTPUT_MODE, 0),
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_A,
                self.tr('Input KP Range Table A'),
                [QgsProcessing.TypeVector],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.A_START_FIELD,
                self.tr('Table A: Start KP field'),
                parentLayerParameterName=self.INPUT_A,
                type=QgsProcessingParameterField.Numeric,
                defaultValue=get_f(self.A_START_FIELD, ''),
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.A_END_FIELD,
                self.tr('Table A: End KP field'),
                parentLayerParameterName=self.INPUT_A,
                type=QgsProcessingParameterField.Numeric,
                defaultValue=get_f(self.A_END_FIELD, ''),
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_B,
                self.tr('Input KP Range Table B'),
                [QgsProcessing.TypeVector],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.B_START_FIELD,
                self.tr('Table B: Start KP field'),
                parentLayerParameterName=self.INPUT_B,
                type=QgsProcessingParameterField.Numeric,
                defaultValue=get_f(self.B_START_FIELD, ''),
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.B_END_FIELD,
                self.tr('Table B: End KP field'),
                parentLayerParameterName=self.INPUT_B,
                type=QgsProcessingParameterField.Numeric,
                defaultValue=get_f(self.B_END_FIELD, ''),
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.B_VALUE_FROM_FIELD,
                self.tr('Table B: Value-from field (e.g. depth_from)'),
                parentLayerParameterName=self.INPUT_B,
                type=QgsProcessingParameterField.Numeric,
                optional=True,
                defaultValue=get_f(self.B_VALUE_FROM_FIELD, ''),
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.B_VALUE_TO_FIELD,
                self.tr('Table B: Value-to field (e.g. depth_to)'),
                parentLayerParameterName=self.INPUT_B,
                type=QgsProcessingParameterField.Numeric,
                optional=True,
                defaultValue=get_f(self.B_VALUE_TO_FIELD, ''),
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.B_SINGLE_VALUE_FIELD,
                self.tr('Table B: Single value field (e.g. slope angle)'),
                parentLayerParameterName=self.INPUT_B,
                type=QgsProcessingParameterField.Numeric,
                optional=True,
                defaultValue=get_f(self.B_SINGLE_VALUE_FIELD, ''),
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.B_SINGLE_VALUE_AGG,
                self.tr('In summarise mode: aggregate single-value field by'),
                options=[
                    self.tr('Length-weighted mean over overlaps'),
                    self.tr('Minimum'),
                    self.tr('Maximum'),
                    self.tr('First match (table order)'),
                    self.tr('Most specific (smallest KP range)'),
                    self.tr('Error if conflicting values'),
                ],
                defaultValue=get_i(self.B_SINGLE_VALUE_AGG, 0),
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.REQUIRE_B_COVERAGE,
                self.tr('In summarise mode: require full coverage by Table B'),
                defaultValue=get_b(self.REQUIRE_B_COVERAGE, False),
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ADD_COVERAGE_FIELDS,
                self.tr('In summarise mode: add coverage/count fields'),
                defaultValue=get_b(self.ADD_COVERAGE_FIELDS, True),
            )
        )

        self.addParameter(
            QgsProcessingParameterField(
                self.LOOKUP_B_FIELD,
                self.tr('In lookup mode: Table B field to copy'),
                parentLayerParameterName=self.INPUT_B,
                type=QgsProcessingParameterField.Any,
                optional=True,
                defaultValue=get_f(self.LOOKUP_B_FIELD, ''),
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.LOOKUP_RULE,
                self.tr('In lookup mode: if multiple matches'),
                options=[
                    self.tr('First match (table order)'),
                    self.tr('Most specific (smallest KP range)'),
                    self.tr('Minimum value (numeric fields only)'),
                    self.tr('Maximum value (numeric fields only)'),
                    self.tr('Mean value (numeric fields only)'),
                    self.tr('Length-weighted mean value (numeric fields only)'),
                    self.tr('Error'),
                ],
                defaultValue=get_i(self.LOOKUP_RULE, 0),
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.LOOKUP_MATCH_MODE,
                self.tr('In lookup mode: match rows when Table B'),
                options=[
                    self.tr('Overlaps Table A range (any overlap)'),
                    self.tr('Fully covers Table A range'),
                ],
                defaultValue=get_i(self.LOOKUP_MATCH_MODE, 0),
            )
        )

        self.addParameter(
            QgsProcessingParameterString(
                self.LOOKUP_OUTPUT_NAME,
                self.tr('In lookup mode: output field name (optional)'),
                optional=True,
                defaultValue=get_s(self.LOOKUP_OUTPUT_NAME, ''),
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.MERGE_MODE,
                self.tr('Merge mode'),
                options=[
                    self.tr('Union (keep intervals covered by either table)'),
                    self.tr('Intersection (keep intervals covered by both tables)'),
                ],
                defaultValue=get_i(self.MERGE_MODE, 0),
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.REQUIRE_BOTH,
                self.tr('Require coverage from both tables (overrides Union)'),
                defaultValue=get_b(self.REQUIRE_BOTH, False),
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.OVERLAP_RULE,
                self.tr('When multiple rows cover the same output interval'),
                options=[
                    self.tr('Most specific (smallest KP range)'),
                    self.tr('First match (table order)'),
                    self.tr('Error'),
                ],
                defaultValue=get_i(self.OVERLAP_RULE, 0),
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.FIELD_NAMING,
                self.tr('Output field naming'),
                options=[
                    self.tr('Keep original field names (auto-resolve duplicates)'),
                    self.tr('Prefix all fields with layer1_/layer2_'),
                ],
                defaultValue=get_i(self.FIELD_NAMING, 0),
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.AUTO_SWAP,
                self.tr('Auto-swap start/end when start > end'),
                defaultValue=get_b(self.AUTO_SWAP, True),
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.TOLERANCE,
                self.tr('KP tolerance'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=get_d(self.TOLERANCE, 1e-9),
                minValue=0.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.DISSOLVE_ADJACENT,
                self.tr('Merge adjacent output intervals with identical attributes'),
                defaultValue=get_b(self.DISSOLVE_ADJACENT, True),
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Merged KP range table'),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        output_mode = self.parameterAsEnum(parameters, self.OUTPUT_MODE, context)
        src_a = self.parameterAsSource(parameters, self.INPUT_A, context)
        src_b = self.parameterAsSource(parameters, self.INPUT_B, context)
        if src_a is None or src_b is None:
            raise QgsProcessingException(self.tr('Missing input source(s).'))

        a_start_field = self.parameterAsString(parameters, self.A_START_FIELD, context)
        a_end_field = self.parameterAsString(parameters, self.A_END_FIELD, context)
        b_start_field = self.parameterAsString(parameters, self.B_START_FIELD, context)
        b_end_field = self.parameterAsString(parameters, self.B_END_FIELD, context)

        merge_mode = self.parameterAsEnum(parameters, self.MERGE_MODE, context)
        require_both = self.parameterAsBool(parameters, self.REQUIRE_BOTH, context)
        overlap_rule = self.parameterAsEnum(parameters, self.OVERLAP_RULE, context)
        field_naming = self.parameterAsEnum(parameters, self.FIELD_NAMING, context)
        auto_swap = self.parameterAsBool(parameters, self.AUTO_SWAP, context)
        tol = float(self.parameterAsDouble(parameters, self.TOLERANCE, context))
        dissolve_adjacent = self.parameterAsBool(parameters, self.DISSOLVE_ADJACENT, context)

        b_value_from_field = self.parameterAsString(parameters, self.B_VALUE_FROM_FIELD, context)
        b_value_to_field = self.parameterAsString(parameters, self.B_VALUE_TO_FIELD, context)
        b_single_value_field = self.parameterAsString(parameters, self.B_SINGLE_VALUE_FIELD, context)
        b_single_value_agg = self.parameterAsEnum(parameters, self.B_SINGLE_VALUE_AGG, context)
        require_b_coverage = self.parameterAsBool(parameters, self.REQUIRE_B_COVERAGE, context)
        add_coverage_fields = self.parameterAsBool(parameters, self.ADD_COVERAGE_FIELDS, context)

        lookup_b_field = self.parameterAsString(parameters, self.LOOKUP_B_FIELD, context)
        lookup_rule = self.parameterAsEnum(parameters, self.LOOKUP_RULE, context)
        lookup_match_mode = self.parameterAsEnum(parameters, self.LOOKUP_MATCH_MODE, context)
        lookup_output_name = self.parameterAsString(parameters, self.LOOKUP_OUTPUT_NAME, context)

        # Persist parameters for next time the dialog opens
        settings = QgsSettings()
        def setv(k: str, v: Any) -> None:
            settings.setValue(self._SETTINGS_PREFIX + k, v)

        setv(self.OUTPUT_MODE, output_mode)
        setv(self.A_START_FIELD, a_start_field)
        setv(self.A_END_FIELD, a_end_field)
        setv(self.B_START_FIELD, b_start_field)
        setv(self.B_END_FIELD, b_end_field)
        setv(self.MERGE_MODE, merge_mode)
        setv(self.REQUIRE_BOTH, require_both)
        setv(self.OVERLAP_RULE, overlap_rule)
        setv(self.FIELD_NAMING, field_naming)
        setv(self.AUTO_SWAP, auto_swap)
        setv(self.TOLERANCE, tol)
        setv(self.DISSOLVE_ADJACENT, dissolve_adjacent)
        setv(self.B_VALUE_FROM_FIELD, b_value_from_field)
        setv(self.B_VALUE_TO_FIELD, b_value_to_field)
        setv(self.B_SINGLE_VALUE_FIELD, b_single_value_field)
        setv(self.B_SINGLE_VALUE_AGG, b_single_value_agg)
        setv(self.REQUIRE_B_COVERAGE, require_b_coverage)
        setv(self.ADD_COVERAGE_FIELDS, add_coverage_fields)
        setv(self.LOOKUP_B_FIELD, lookup_b_field)
        setv(self.LOOKUP_RULE, lookup_rule)
        setv(self.LOOKUP_MATCH_MODE, lookup_match_mode)
        setv(self.LOOKUP_OUTPUT_NAME, lookup_output_name)

        if a_start_field == a_end_field or b_start_field == b_end_field:
            raise QgsProcessingException(self.tr('Start and end fields must be different.'))

        # Shared: discover non-KP fields
        a_extra_fields = [name for name in src_a.fields().names() if name not in (a_start_field, a_end_field)]
        b_extra_fields = [name for name in src_b.fields().names() if name not in (b_start_field, b_end_field)]

        if output_mode == 2:
            # Lookup: copy one field from B into A
            if not lookup_b_field:
                raise QgsProcessingException(self.tr('In lookup mode, you must choose the Table B field to copy.'))

            fields = QgsFields()
            fields.append(QgsField('kp_from', QVariant.Double))
            fields.append(QgsField('kp_to', QVariant.Double))

            used = set(['kp_from', 'kp_to'])
            for n in a_extra_fields:
                out_name = self._unique_field_name(n, used)
                f = src_a.fields().field(n)
                fields.append(QgsField(out_name, f.type(), f.typeName(), f.length(), f.precision()))

            out_lookup_name = (lookup_output_name or '').strip() or lookup_b_field
            out_lookup_name = self._unique_field_name(out_lookup_name, used)

            numeric_required = lookup_rule in (2, 3, 4, 5)
            b_f = src_b.fields().field(lookup_b_field)
            if numeric_required:
                # For mean/min/max we emit a Double to avoid integer truncation
                fields.append(QgsField(out_lookup_name, QVariant.Double))
            else:
                fields.append(QgsField(out_lookup_name, b_f.type(), b_f.typeName(), b_f.length(), b_f.precision()))
            fields.append(QgsField('b_match_count', QVariant.Int))

            (sink, dest_id) = self.parameterAsSink(
                parameters,
                self.OUTPUT,
                context,
                fields,
                QgsWkbTypes.NoGeometry,
                src_a.sourceCrs(),
            )
            if sink is None:
                raise QgsProcessingException(self.tr('Could not create output sink.'))

            ranges_a = self._read_ranges(
                src_a,
                start_field=a_start_field,
                end_field=a_end_field,
                extra_fields=a_extra_fields,
                auto_swap=auto_swap,
                feedback=feedback,
                label='Table A',
            )
            ranges_b = self._read_ranges(
                src_b,
                start_field=b_start_field,
                end_field=b_end_field,
                extra_fields=[lookup_b_field],
                auto_swap=auto_swap,
                feedback=feedback,
                label='Table B',
            )

            a_sorted = sorted(ranges_a, key=lambda r: (r.start, r.end, r.order))
            b_sorted = sorted(ranges_b, key=lambda r: (r.start, r.end, r.order))

            b_idx = 0
            total_a = len(a_sorted)
            for idx, a_row in enumerate(a_sorted):
                if feedback.isCanceled():
                    break

                a_start = a_row.start
                a_end = a_row.end

                while b_idx < len(b_sorted) and b_sorted[b_idx].end < a_start - tol:
                    b_idx += 1

                scan_idx = b_idx
                candidates: List[Tuple[_RangeFeature, float]] = []
                while scan_idx < len(b_sorted) and b_sorted[scan_idx].start <= a_end + tol:
                    b_row = b_sorted[scan_idx]
                    scan_idx += 1

                    if lookup_match_mode == 1:
                        if (b_row.start <= a_start + tol) and (b_row.end >= a_end - tol):
                            candidates.append((b_row, float(a_end - a_start)))
                    else:
                        ov_start = max(a_start, b_row.start)
                        ov_end = min(a_end, b_row.end)
                        if ov_end > ov_start + tol:
                            candidates.append((b_row, float(ov_end - ov_start)))

                chosen_value = None
                if candidates:
                    chosen_value = self._lookup_value(
                        candidates,
                        rule=lookup_rule,
                        field_name=lookup_b_field,
                        tol=tol,
                        kp_from=a_start,
                        kp_to=a_end,
                    )

                feat = QgsFeature(fields)
                out_attrs: List[Any] = [a_start, a_end]
                out_attrs.extend([a_row.attrs.get(n) for n in a_extra_fields])
                out_attrs.append(chosen_value)
                out_attrs.append(len(candidates))
                feat.setAttributes(out_attrs)
                sink.addFeature(feat, QgsFeatureSink.FastInsert)

                if total_a:
                    feedback.setProgress(int((idx + 1) / total_a * 100))

            return {self.OUTPUT: dest_id}

        if output_mode == 1:
            # Summarise values from B into A
            has_pair = bool(b_value_from_field and b_value_to_field)
            has_single = bool(b_single_value_field)

            if has_pair and has_single:
                raise QgsProcessingException(
                    self.tr('In summarise mode, choose either (value-from/value-to) OR a single value field, not both.')
                )

            if not has_pair and not has_single:
                raise QgsProcessingException(
                    self.tr('In summarise mode, choose either a value-from/value-to pair or a single value field from Table B.')
                )

            fields = QgsFields()
            fields.append(QgsField('kp_from', QVariant.Double))
            fields.append(QgsField('kp_to', QVariant.Double))

            # Keep A fields as-is (exclude KP fields)
            used = set(['kp_from', 'kp_to'])
            a_out_names: List[str] = []
            for n in a_extra_fields:
                out_name = self._unique_field_name(n, used)
                a_out_names.append(out_name)
                f = src_a.fields().field(n)
                fields.append(QgsField(out_name, f.type(), f.typeName(), f.length(), f.precision()))

            if has_pair:
                fields.append(QgsField('b_value_min', QVariant.Double))
                fields.append(QgsField('b_value_max', QVariant.Double))
                fields.append(QgsField('b_value_avg', QVariant.Double))
            else:
                fields.append(QgsField('b_value', QVariant.Double))

            if add_coverage_fields:
                fields.append(QgsField('b_overlap_count', QVariant.Int))
                fields.append(QgsField('b_coverage_ratio', QVariant.Double))

            (sink, dest_id) = self.parameterAsSink(
                parameters,
                self.OUTPUT,
                context,
                fields,
                QgsWkbTypes.NoGeometry,
                src_a.sourceCrs(),
            )
            if sink is None:
                raise QgsProcessingException(self.tr('Could not create output sink.'))

            ranges_a = self._read_ranges(
                src_a,
                start_field=a_start_field,
                end_field=a_end_field,
                extra_fields=a_extra_fields,
                auto_swap=auto_swap,
                feedback=feedback,
                label='Table A',
            )
            ranges_b = self._read_ranges(
                src_b,
                start_field=b_start_field,
                end_field=b_end_field,
                extra_fields=[b_value_from_field, b_value_to_field] if has_pair else [b_single_value_field],
                auto_swap=auto_swap,
                feedback=feedback,
                label='Table B',
            )

            # Sort for efficient scanning
            a_sorted = sorted(ranges_a, key=lambda r: (r.start, r.end, r.order))
            b_sorted = sorted(ranges_b, key=lambda r: (r.start, r.end, r.order))

            b_idx = 0
            total_a = len(a_sorted)
            for idx, a_row in enumerate(a_sorted):
                if feedback.isCanceled():
                    break

                a_start = a_row.start
                a_end = a_row.end

                # Advance B pointer past ranges that end before A starts
                while b_idx < len(b_sorted) and b_sorted[b_idx].end < a_start - tol:
                    b_idx += 1

                # Scan overlapping B ranges
                scan_idx = b_idx
                overlap_count = 0
                min_val: Optional[float] = None
                max_val: Optional[float] = None
                single_val: Optional[float] = None
                single_weight_sum = 0.0
                single_weighted_sum = 0.0
                single_candidates: List[Tuple[float, float, float, int]] = []  # (value, overlap_len, range_len, order)

                pair_weight_sum = 0.0
                pair_weighted_mid_sum = 0.0

                covered_len = 0.0
                last_cov_end: Optional[float] = None

                while scan_idx < len(b_sorted) and b_sorted[scan_idx].start <= a_end + tol:
                    b_row = b_sorted[scan_idx]
                    scan_idx += 1

                    # Overlap interval within A
                    ov_start = max(a_start, b_row.start)
                    ov_end = min(a_end, b_row.end)
                    if ov_end <= ov_start + tol:
                        continue

                    overlap_count += 1

                    if has_pair:
                        # min/max across B rows using both endpoints to avoid inventing values
                        try:
                            v_from_raw = b_row.attrs.get(b_value_from_field)
                            v_to_raw = b_row.attrs.get(b_value_to_field)
                            if v_from_raw is not None and v_to_raw is not None:
                                v_from = float(v_from_raw)
                                v_to = float(v_to_raw)
                                v_lo = min(v_from, v_to)
                                v_hi = max(v_from, v_to)
                                min_val = v_lo if min_val is None else min(min_val, v_lo)
                                max_val = v_hi if max_val is None else max(max_val, v_hi)

                                # Length-weighted average of midpoints.
                                # This does not fill gaps; it only averages across overlaps.
                                mid = 0.5 * (v_from + v_to)
                                w = float(ov_end - ov_start)
                                if w > 0:
                                    pair_weight_sum += w
                                    pair_weighted_mid_sum += mid * w
                        except Exception:
                            # Skip bad numeric rows for min/max computation
                            pass
                    else:
                        try:
                            v_raw = b_row.attrs.get(b_single_value_field)
                            if v_raw is not None:
                                v = float(v_raw)
                                overlap_len = float(ov_end - ov_start)
                                range_len = float(b_row.end - b_row.start)
                                single_candidates.append((v, overlap_len, range_len, b_row.order))
                                # Pre-compute weighted sum for mean option
                                single_weighted_sum += v * overlap_len
                                single_weight_sum += overlap_len
                        except Exception:
                            pass

                    # Coverage union length within A (handles overlaps)
                    if last_cov_end is None:
                        covered_len += (ov_end - ov_start)
                        last_cov_end = ov_end
                    else:
                        if ov_start <= last_cov_end + tol:
                            if ov_end > last_cov_end:
                                covered_len += (ov_end - last_cov_end)
                                last_cov_end = ov_end
                        else:
                            covered_len += (ov_end - ov_start)
                            last_cov_end = ov_end

                a_len = max(0.0, a_end - a_start)
                coverage_ratio = (covered_len / a_len) if a_len > tol else (1.0 if overlap_count else 0.0)

                if require_b_coverage and a_len > tol and covered_len < a_len - tol:
                    min_val = None
                    max_val = None
                    single_candidates = []
                    single_weight_sum = 0.0
                    single_weighted_sum = 0.0
                    pair_weight_sum = 0.0
                    pair_weighted_mid_sum = 0.0

                if not has_pair:
                    single_val = self._aggregate_single_value(single_candidates, b_single_value_agg, tol=tol, kp_from=a_start, kp_to=a_end)
                pair_avg: Optional[float] = None
                if has_pair and pair_weight_sum > tol:
                    pair_avg = pair_weighted_mid_sum / pair_weight_sum

                feat = QgsFeature(fields)
                out_attrs: List[Any] = [a_start, a_end]
                # Append A attributes
                out_attrs.extend([a_row.attrs.get(n) for n in a_extra_fields])
                # Append summary stats
                if has_pair:
                    out_attrs.append(min_val)
                    out_attrs.append(max_val)
                    out_attrs.append(pair_avg)
                else:
                    out_attrs.append(single_val)
                if add_coverage_fields:
                    out_attrs.append(overlap_count)
                    out_attrs.append(coverage_ratio)
                feat.setAttributes(out_attrs)
                sink.addFeature(feat, QgsFeatureSink.FastInsert)

                if total_a:
                    feedback.setProgress(int((idx + 1) / total_a * 100))

            return {self.OUTPUT: dest_id}

        # Output mode 0: canonical merge of both tables
        # Build output fields
        fields = QgsFields()
        fields.append(QgsField('kp_from', QVariant.Double))
        fields.append(QgsField('kp_to', QVariant.Double))

        output_a_names, output_b_names = self._build_output_field_names(
            a_extra_fields,
            b_extra_fields,
            field_naming=field_naming,
        )

        # Preserve field types from inputs where possible
        for in_name, out_name in zip(a_extra_fields, output_a_names):
            f = src_a.fields().field(in_name)
            fields.append(QgsField(out_name, f.type(), f.typeName(), f.length(), f.precision()))

        for in_name, out_name in zip(b_extra_fields, output_b_names):
            f = src_b.fields().field(in_name)
            fields.append(QgsField(out_name, f.type(), f.typeName(), f.length(), f.precision()))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.NoGeometry,
            src_a.sourceCrs(),
        )
        if sink is None:
            raise QgsProcessingException(self.tr('Could not create output sink.'))

        # Read ranges
        ranges_a = self._read_ranges(
            src_a,
            start_field=a_start_field,
            end_field=a_end_field,
            extra_fields=a_extra_fields,
            auto_swap=auto_swap,
            feedback=feedback,
            label='Table A',
        )
        ranges_b = self._read_ranges(
            src_b,
            start_field=b_start_field,
            end_field=b_end_field,
            extra_fields=b_extra_fields,
            auto_swap=auto_swap,
            feedback=feedback,
            label='Table B',
        )

        breakpoints = self._collect_breakpoints(ranges_a, ranges_b)
        breakpoints = self._unique_sorted(breakpoints, tol=tol)

        if len(breakpoints) < 2:
            feedback.pushInfo(self.tr('No valid KP ranges found to merge.'))
            return {self.OUTPUT: dest_id}

        selector_a = _RangeSelector(ranges_a, tol=tol, overlap_rule=overlap_rule)
        selector_b = _RangeSelector(ranges_b, tol=tol, overlap_rule=overlap_rule)

        # Build atomic segments
        segments: List[Tuple[float, float, Optional[_RangeFeature], Optional[_RangeFeature]]] = []
        total_segments = max(0, len(breakpoints) - 1)
        for i in range(total_segments):
            if feedback.isCanceled():
                break

            kp_from = breakpoints[i]
            kp_to = breakpoints[i + 1]
            if kp_to <= kp_from + tol:
                continue

            chosen_a = selector_a.choose(kp_from, kp_to, label='A')
            chosen_b = selector_b.choose(kp_from, kp_to, label='B')

            if require_both or merge_mode == 1:
                if chosen_a is None or chosen_b is None:
                    continue
            else:
                if chosen_a is None and chosen_b is None:
                    continue

            segments.append((kp_from, kp_to, chosen_a, chosen_b))

            if total_segments:
                feedback.setProgress(int((i + 1) / total_segments * 40))

        if dissolve_adjacent and segments:
            segments = self._dissolve_adjacent(segments, a_extra_fields, b_extra_fields, tol=tol)

        # Write output
        total = len(segments)
        for idx, (kp_from, kp_to, row_a, row_b) in enumerate(segments):
            if feedback.isCanceled():
                break

            feat = QgsFeature(fields)
            attrs: List[Any] = [kp_from, kp_to]

            if row_a is None:
                attrs.extend([None] * len(a_extra_fields))
            else:
                attrs.extend([row_a.attrs.get(name) for name in a_extra_fields])

            if row_b is None:
                attrs.extend([None] * len(b_extra_fields))
            else:
                attrs.extend([row_b.attrs.get(name) for name in b_extra_fields])

            feat.setAttributes(attrs)
            sink.addFeature(feat, QgsFeatureSink.FastInsert)

            if total:
                # Remaining 60% of progress bar
                feedback.setProgress(40 + int((idx + 1) / total * 60))

        return {self.OUTPUT: dest_id}

    @staticmethod
    def _unique_field_name(name: str, used: set) -> str:
        if name not in used:
            used.add(name)
            return name
        base = name
        k = 2
        while f'{base}_{k}' in used:
            k += 1
        out = f'{base}_{k}'
        used.add(out)
        return out

    @staticmethod
    def _aggregate_single_value(
        candidates: Sequence[Tuple[float, float, float, int]],
        agg: int,
        tol: float,
        kp_from: float,
        kp_to: float,
    ) -> Optional[float]:
        """Aggregate a single numeric value over overlapping B ranges.

        candidates: (value, overlap_len, range_len, order)
        agg:
          0 length-weighted mean
          1 min
          2 max
          3 first match
          4 most specific
          5 error if conflicting values
        """
        if not candidates:
            return None

        if agg == 0:
            wsum = 0.0
            vsum = 0.0
            for v, w, _range_len, _order in candidates:
                if w > 0:
                    wsum += w
                    vsum += v * w
            return (vsum / wsum) if wsum > tol else candidates[0][0]

        if agg == 1:
            return min(v for v, _w, _rl, _o in candidates)
        if agg == 2:
            return max(v for v, _w, _rl, _o in candidates)
        if agg == 3:
            return min(candidates, key=lambda t: t[3])[0]
        if agg == 4:
            return min(candidates, key=lambda t: (t[2], t[3]))[0]

        # Error on conflicting values
        # Consider values equal if within tolerance
        values_sorted = sorted(v for v, _w, _rl, _o in candidates)
        distinct = [values_sorted[0]]
        for v in values_sorted[1:]:
            if abs(v - distinct[-1]) > tol:
                distinct.append(v)
                if len(distinct) > 1:
                    raise QgsProcessingException(
                        QCoreApplication.translate(
                            'Processing',
                            f'Conflicting Table B values within output interval {kp_from}-{kp_to}. '
                            'Change aggregation rule or resolve overlaps in Table B.'
                        )
                    )
        return distinct[0]

    @staticmethod
    def _choose_lookup_candidate(
        candidates: Sequence[_RangeFeature],
        rule: int,
        field_name: str,
        tol: float,
        kp_from: float,
        kp_to: float,
    ) -> Optional[_RangeFeature]:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        # 0 first, 1 most specific, 2 min value, 3 max value, 4 error
        if rule == 0:
            return min(candidates, key=lambda r: r.order)
        if rule == 1:
            return min(candidates, key=lambda r: (r.end - r.start, r.order))

        if rule in (2, 3):
            numeric: List[Tuple[float, _RangeFeature]] = []
            for r in candidates:
                v = r.attrs.get(field_name)
                if v is None:
                    continue
                try:
                    numeric.append((float(v), r))
                except Exception:
                    raise QgsProcessingException(
                        QCoreApplication.translate('Processing', 'Minimum/Maximum lookup requires a numeric field.')
                    )
            if not numeric:
                return None
            return min(numeric, key=lambda t: t[0])[1] if rule == 2 else max(numeric, key=lambda t: t[0])[1]

        raise QgsProcessingException(
            QCoreApplication.translate(
                'Processing',
                f'Multiple rows in table B match output interval {kp_from}-{kp_to}. '
                'Change the lookup rule or resolve overlaps in Table B.'
            )
        )

    @staticmethod
    def _lookup_value(
        candidates: Sequence[Tuple[_RangeFeature, float]],
        rule: int,
        field_name: str,
        tol: float,
        kp_from: float,
        kp_to: float,
    ) -> Any:
        """Compute a single lookup value from matching B rows.

        candidates: list of (range_feature, overlap_len)
        rule:
          0 first match
          1 most specific
          2 min numeric
          3 max numeric
          4 mean numeric
          5 length-weighted mean numeric
          6 error
        """
        if not candidates:
            return None

        if rule == 0:
            chosen = min((r for r, _w in candidates), key=lambda r: r.order)
            return chosen.attrs.get(field_name)
        if rule == 1:
            chosen = min((r for r, _w in candidates), key=lambda r: (r.end - r.start, r.order))
            return chosen.attrs.get(field_name)

        if rule in (2, 3, 4, 5):
            vals: List[Tuple[float, float, int]] = []  # (value, weight, order)
            for r, w in candidates:
                v = r.attrs.get(field_name)
                if v is None:
                    continue
                try:
                    vals.append((float(v), float(w), r.order))
                except Exception:
                    raise QgsProcessingException(
                        QCoreApplication.translate('Processing', 'This lookup rule requires a numeric field.')
                    )

            if not vals:
                return None

            if rule == 2:
                return min(vals, key=lambda t: t[0])[0]
            if rule == 3:
                return max(vals, key=lambda t: t[0])[0]
            if rule == 4:
                return sum(v for v, _w, _o in vals) / float(len(vals))

            wsum = sum(w for _v, w, _o in vals if w > 0)
            if wsum <= tol:
                return min(vals, key=lambda t: t[2])[0]
            return sum(v * w for v, w, _o in vals if w > 0) / wsum

        raise QgsProcessingException(
            QCoreApplication.translate(
                'Processing',
                f'Multiple rows in table B match output interval {kp_from}-{kp_to}. '
                'Change the lookup rule or resolve overlaps in Table B.'
            )
        )

    def _read_ranges(
        self,
        src,
        start_field: str,
        end_field: str,
        extra_fields: Sequence[str],
        auto_swap: bool,
        feedback,
        label: str = '',
    ) -> List[_RangeFeature]:
        out: List[_RangeFeature] = []
        total = 0
        skipped_null = 0
        skipped_invalid = 0
        skipped_reversed = 0
        swapped = 0
        skipped_zero = 0
        reported_errors = 0
        max_reported_errors = 20

        for i, f in enumerate(src.getFeatures()):
            if feedback.isCanceled():
                break

            total += 1

            try:
                start_raw = f[start_field]
                end_raw = f[end_field]
                if start_raw is None or end_raw is None:
                    skipped_null += 1
                    if reported_errors < max_reported_errors:
                        feedback.reportError(self.tr(f'{label} row {i + 1}: NULL KP values; skipping.'))
                        reported_errors += 1
                    continue
                start = float(start_raw)
                end = float(end_raw)
            except Exception:
                skipped_invalid += 1
                if reported_errors < max_reported_errors:
                    feedback.reportError(self.tr(f'{label} row {i + 1}: Invalid KP values; skipping.'))
                    reported_errors += 1
                continue

            if start > end:
                if auto_swap:
                    start, end = end, start
                    swapped += 1
                else:
                    skipped_reversed += 1
                    if reported_errors < max_reported_errors:
                        feedback.reportError(self.tr(f'{label} row {i + 1}: Start KP > End KP; skipping.'))
                        reported_errors += 1
                    continue

            if start == end:
                skipped_zero += 1
                continue

            attrs = {name: f[name] for name in extra_fields}
            out.append(_RangeFeature(start=start, end=end, order=i, attrs=attrs))

        if label:
            suppressed = max(0, (skipped_null + skipped_invalid + skipped_reversed) - reported_errors)
            msg = (
                f'{label}: read {len(out)} ranges from {total} rows '
                f'(skipped null={skipped_null}, invalid={skipped_invalid}, reversed={skipped_reversed}, zero_len={skipped_zero}, swapped={swapped})'
            )
            feedback.pushInfo(msg)
            if suppressed > 0:
                feedback.pushInfo(self.tr(f'{label}: {suppressed} additional row errors suppressed.'))
        return out

    @staticmethod
    def _collect_breakpoints(a: Sequence[_RangeFeature], b: Sequence[_RangeFeature]) -> List[float]:
        pts: List[float] = []
        for r in a:
            pts.append(r.start)
            pts.append(r.end)
        for r in b:
            pts.append(r.start)
            pts.append(r.end)
        return pts

    @staticmethod
    def _unique_sorted(values: Sequence[float], tol: float) -> List[float]:
        if not values:
            return []
        vals = sorted(values)
        out = [vals[0]]
        for v in vals[1:]:
            if abs(v - out[-1]) > tol:
                out.append(v)
        return out

    @staticmethod
    def _build_output_field_names(
        a_fields: Sequence[str],
        b_fields: Sequence[str],
        field_naming: int,
    ) -> Tuple[List[str], List[str]]:
        if field_naming == 1:
            return [f'layer1_{n}' for n in a_fields], [f'layer2_{n}' for n in b_fields]

        # Keep original names, but ensure uniqueness between A and B sets
        used = set(['kp_from', 'kp_to'])
        out_a: List[str] = []
        out_b: List[str] = []

        def unique(name: str) -> str:
            if name not in used:
                used.add(name)
                return name
            base = name
            k = 2
            while f'{base}_{k}' in used:
                k += 1
            new_name = f'{base}_{k}'
            used.add(new_name)
            return new_name

        for n in a_fields:
            out_a.append(unique(n))
        for n in b_fields:
            out_b.append(unique(n))
        return out_a, out_b

    @staticmethod
    def _dissolve_adjacent(
        segments: List[Tuple[float, float, Optional[_RangeFeature], Optional[_RangeFeature]]],
        a_extra_fields: Sequence[str],
        b_extra_fields: Sequence[str],
        tol: float,
    ) -> List[Tuple[float, float, Optional[_RangeFeature], Optional[_RangeFeature]]]:
        """Merge adjacent segments when attributes from both tables are identical."""
        if not segments:
            return segments

        def key(seg: Tuple[float, float, Optional[_RangeFeature], Optional[_RangeFeature]]):
            _, __, ra, rb = seg
            a_vals = tuple(None if ra is None else ra.attrs.get(n) for n in a_extra_fields)
            b_vals = tuple(None if rb is None else rb.attrs.get(n) for n in b_extra_fields)
            return (a_vals, b_vals)

        out = [segments[0]]
        for kp_from, kp_to, ra, rb in segments[1:]:
            prev_from, prev_to, prev_ra, prev_rb = out[-1]
            if abs(prev_to - kp_from) <= tol and key((prev_from, prev_to, prev_ra, prev_rb)) == key((kp_from, kp_to, ra, rb)):
                out[-1] = (prev_from, kp_to, prev_ra, prev_rb)
            else:
                out.append((kp_from, kp_to, ra, rb))
        return out

    def name(self):
        return 'kp_range_merge_tables'

    def displayName(self):
        return self.tr('Merge KP Range Tables')

    def group(self):
        return self.tr('KP Ranges')

    def groupId(self):
        return 'kp_ranges'

    def shortHelpString(self):
        return self.tr(
            """
Merges and/or joins KP range tables where KP intervals don’t necessarily line up.

This algorithm supports 3 output modes (see **Output mode**):

1) **Canonical segmentation (merge attributes from both tables)**
     - Output is a *canonical* set of non-overlapping KP intervals.
     - The output is split at every KP breakpoint from either input (all starts/ends).
     - For each output interval, attributes from A and/or B are assigned only if an input row covers that interval.

     Example:
     - A: 600–700 (burial_method=plough)
     - B: 650–750 (water_depth=…)
     Output intervals: 600–650, 650–700, 700–750

2) **Summarise Table B values into Table A ranges (min/max / aggregate)**
     - Output follows Table A’s KP ranges.
     - Choose either:
         - **Value-from/value-to** from B (e.g. depth_from/depth_to) → outputs `b_value_min`/`b_value_max`, or
         - **Single value field** from B (e.g. slope angle) → outputs `b_value` using the chosen aggregation.
    - When using a value-from/value-to pair, the output also includes `b_value_avg` (a length-weighted average of the per-row midpoint (from+to)/2 over overlaps).
     - Optional: **require full coverage by Table B** (otherwise results may be based on partial overlap).

3) **Lookup: copy one field from Table B into Table A**
     - Output follows Table A’s KP ranges.
     - Copies one chosen field from B into A, using a simple rule when multiple B rows match:
         first / most-specific / min / max / mean / length-weighted mean / error.
     - Choose how matches are found:
         - “Any overlap” (typical), or
         - “Fully covers A range” (strict).

Important behaviour ("don’t make up data"):
- The algorithm does **not** fill gaps and does **not** interpolate values.
- If a table does not cover an output interval/range, its fields are left **NULL**.
- If your Table B values represent endpoints only (e.g. depth_from/depth_to), true min/max *between* endpoints cannot be inferred.

Input quality + overlaps:
- Start/end order does not matter if **Auto-swap start/end** is enabled (default).
- Rows with NULL/invalid KP values are skipped.
- If multiple rows from the same table could apply to the same place, use the overlap/lookup rule:
    - **Error** is the strictest option.

Tips:
- The dialog remembers last-used parameters between runs (within the same QGIS profile).
- For very large CSVs, prefer Lookup/Summarise modes when you only need A-shaped output.
- If you see unexpected NULLs, check **KP tolerance**, and whether B truly overlaps/covers the A ranges.
"""
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return KPRangeMergeTablesAlgorithm()
