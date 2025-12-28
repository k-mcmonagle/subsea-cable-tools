"""KPRangeCSVAlgorithm

This tool highlights sections of an RPL line based on KP ranges.

Ranges can come from:
- a table layer (e.g. a loaded CSV with no geometry), or
- a pasted text block (e.g. copied from Excel; typically tab-separated).

The first two columns are interpreted as start/end KP (km). Any additional
columns are carried through to the output.
"""

from __future__ import annotations

import csv
import io
import re
from typing import List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingException,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterField,
                       QgsProcessingParameterString,
                       QgsFeature,
                       QgsGeometry,
                       QgsFields,
                       QgsField,
                       QgsWkbTypes,
                       QgsDistanceArea)

from ..kp_range_utils import extract_line_segment, measure_total_length_m

class KPRangeCSVAlgorithm(QgsProcessingAlgorithm):
    INPUT_LAYER = 'INPUT_LAYER'
    INPUT_LINE = 'INPUT_LINE'
    START_KP_FIELD = 'START_KP_FIELD'
    END_KP_FIELD = 'END_KP_FIELD'
    PASTED_RANGES = 'PASTED_RANGES'
    OUTPUT = 'OUTPUT'

    _DEFAULT_COL_PREFIX = 'col_'

    @staticmethod
    def _safe_field_name(name: str, used: set) -> str:
        """Sanitize and de-duplicate field names for output layers."""

        name = (name or '').strip()
        if not name:
            name = 'col'
        # QGIS field names are typically best as ascii-ish identifiers.
        name = name.lower()
        name = re.sub(r"\s+", "_", name)
        name = re.sub(r"[^a-z0-9_]+", "", name)
        if not name:
            name = 'col'

        base = name
        i = 2
        while name in used:
            name = f"{base}_{i}"
            i += 1
        used.add(name)
        return name

    @staticmethod
    def _parse_kp(value) -> Optional[float]:
        """Parse KP values flexibly (handles Excel-style numbers)."""

        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None

        # Remove common noise
        text = text.replace("\u00a0", " ")  # NBSP
        text = text.strip()

        # Handle decimal comma (e.g. 12,3) vs thousands separators.
        # Heuristic:
        # - if it has a comma but no dot, treat comma as decimal separator
        # - if it has both, drop commas (thousands) and keep dot as decimal
        if "," in text and "." not in text:
            # Avoid turning "1,234" into 1.234 incorrectly in some locales,
            # but for KP values this is usually acceptable.
            text = text.replace(",", ".")
        elif "," in text and "." in text:
            text = text.replace(",", "")

        # Strip any remaining non-numeric characters except sign and dot
        cleaned = re.sub(r"[^0-9+\-\.]+", "", text)
        if cleaned in {"", ".", "+", "-"}:
            return None

        try:
            return float(cleaned)
        except Exception:
            return None

    @classmethod
    def _parse_pasted_ranges(cls, raw_text: str) -> Tuple[List[str], List[Tuple[float, float, List[str]]]]:
        """Parse pasted rows into (extra_column_names, rows).

        Returns:
            extra_column_names: list of names for columns 3..N
            rows: list of tuples (start_kp, end_kp, extras)
        """

        if raw_text is None:
            return ([], [])

        text = str(raw_text).strip()
        if not text:
            return ([], [])

        # Normalize newlines
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if not lines:
            return ([], [])

        sample = "\n".join(lines[:10])
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=["\t", ",", ";", "|"])
        except Exception:
            dialect = csv.excel_tab

        reader = csv.reader(io.StringIO(text), dialect=dialect)
        parsed_rows: List[List[str]] = []
        for row in reader:
            if not row:
                continue
            # Trim cells
            trimmed = [str(c).strip() for c in row]
            # Skip entirely blank rows
            if not any(trimmed):
                continue
            parsed_rows.append(trimmed)

        if not parsed_rows:
            return ([], [])

        # Detect header: first row where first two columns are not parseable as numbers
        header: Optional[List[str]] = None
        first = parsed_rows[0]
        first_c1 = first[0] if len(first) > 0 else ""
        first_c2 = first[1] if len(first) > 1 else ""
        if cls._parse_kp(first_c1) is None or cls._parse_kp(first_c2) is None:
            header = first
            data_rows = parsed_rows[1:]
        else:
            data_rows = parsed_rows

        # Determine max columns to align extras
        max_cols = max(len(r) for r in ([header] if header else []) + data_rows) if data_rows else (len(header) if header else 2)
        max_cols = max(max_cols, 2)
        extra_count = max(0, max_cols - 2)

        used_names = set()
        extra_names: List[str] = []
        for idx in range(extra_count):
            if header and len(header) >= (idx + 3) and header[idx + 2].strip():
                candidate = header[idx + 2]
            else:
                candidate = f"{cls._DEFAULT_COL_PREFIX}{idx + 3}"
            extra_names.append(cls._safe_field_name(candidate, used_names))

        rows: List[Tuple[float, float, List[str]]] = []
        for r in data_rows:
            if len(r) < 2:
                continue
            start = cls._parse_kp(r[0])
            end = cls._parse_kp(r[1])
            if start is None or end is None:
                # Skip non-numeric rows silently; the main algorithm will report counts.
                continue

            extras: List[str] = []
            for idx in range(extra_count):
                extras.append(r[idx + 2] if len(r) >= (idx + 3) else "")
            rows.append((float(start), float(end), extras))

        return (extra_names, rows)

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LAYER,
                self.tr('Input Table of KP Ranges'),
                [QgsProcessing.TypeVector],
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.PASTED_RANGES,
                self.tr('Paste KP ranges (Excel/CSV text)'),
                multiLine=True,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LINE,
                self.tr('Input RPL Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.START_KP_FIELD,
                self.tr('Start KP Field'),
                parentLayerParameterName=self.INPUT_LAYER,
                type=QgsProcessingParameterField.Numeric,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.END_KP_FIELD,
                self.tr('End KP Field'),
                parentLayerParameterName=self.INPUT_LAYER,
                type=QgsProcessingParameterField.Numeric,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT_LINE, context)
        pasted_text = self.parameterAsString(parameters, self.PASTED_RANGES, context)

        use_pasted = bool(pasted_text and pasted_text.strip())
        input_layer = None
        start_kp_field = None
        end_kp_field = None
        additional_fields: List[str] = []
        extra_names: List[str] = []

        if use_pasted:
            extra_names, pasted_rows = self._parse_pasted_ranges(pasted_text)
            if not pasted_rows:
                feedback.reportError('No valid KP rows parsed from pasted text.')
        else:
            input_layer = self.parameterAsSource(parameters, self.INPUT_LAYER, context)
            if input_layer is None:
                raise QgsProcessingException('Provide either an input table layer or pasted KP ranges text.')
            start_kp_field = self.parameterAsString(parameters, self.START_KP_FIELD, context)
            end_kp_field = self.parameterAsString(parameters, self.END_KP_FIELD, context)
            if not start_kp_field or not end_kp_field:
                raise QgsProcessingException('Start KP Field and End KP Field are required when using an input table layer.')

            # Default to all fields except the KP fields
            all_field_names = input_layer.fields().names()
            additional_fields = [name for name in all_field_names if name not in [start_kp_field, end_kp_field]]

        # Create fields for the output layer
        fields = QgsFields()
        fields.append(QgsField('start_kp', QVariant.Double))
        fields.append(QgsField('end_kp', QVariant.Double))

        if use_pasted:
            for name in extra_names:
                fields.append(QgsField(name, QVariant.String))
        else:
            for field_name in additional_fields:
                # Get the field from the input layer to preserve its type
                input_field = input_layer.fields().field(field_name)
                fields.append(input_field)

        fields.append(QgsField('source_table', QVariant.String))
        fields.append(QgsField('source_line', QVariant.String))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.LineString,
            source.sourceCrs(),
        )

        # Combine all features from the line layer into a single geometry
        geometries = [f.geometry() for f in source.getFeatures()]
        if not geometries:
            return {self.OUTPUT: dest_id}

        combined_geom = QgsGeometry.unaryUnion(geometries)

        if combined_geom.isEmpty():
            feedback.pushInfo("Input line layer is empty or invalid.")
            return {self.OUTPUT: dest_id}

        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(source.sourceCrs(), context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

        # Pre-calculate the total length of the line
        total_length = float(measure_total_length_m(combined_geom, distance_calculator))
        feedback.pushInfo(f"Total length of dissolved input line: {total_length} meters")

        if use_pasted:
            total_rows = len(pasted_rows)
            for current, (start_kp, end_kp, extras) in enumerate(pasted_rows):
                if feedback.isCanceled():
                    break

                if (start_kp * 1000) > total_length or (end_kp * 1000) > total_length:
                    feedback.reportError(
                        f"KP range {start_kp}-{end_kp} exceeds total line length of {total_length/1000:.2f} km. Skipping."
                    )
                    continue

                seg_geom = extract_line_segment(combined_geom, start_kp, end_kp, distance_calculator)
                if seg_geom and not seg_geom.isEmpty():
                    feat = QgsFeature(fields)
                    feat.setGeometry(seg_geom)
                    attributes = [start_kp, end_kp]
                    attributes.extend(extras)
                    attributes.append('pasted_text')
                    attributes.append(source.sourceName())
                    feat.setAttributes(attributes)
                    sink.addFeature(feat, QgsFeatureSink.FastInsert)
                else:
                    feedback.reportError(f"Could not extract line segment for KP range {start_kp}-{end_kp}. Skipping.")

                feedback.setProgress(int((current + 1) / max(total_rows, 1) * 100))
        else:
            input_features = list(input_layer.getFeatures())
            total_rows = len(input_features)

            for current, feature in enumerate(input_features):
                if feedback.isCanceled():
                    break
                try:
                    start_kp = float(feature[start_kp_field])
                    end_kp = float(feature[end_kp_field])
                except (ValueError, KeyError, TypeError):
                    feedback.reportError(f"Invalid KP values in row {current + 1}. Skipping.")
                    continue

                if (start_kp * 1000) > total_length or (end_kp * 1000) > total_length:
                    feedback.reportError(
                        f"KP range {start_kp}-{end_kp} exceeds total line length of {total_length/1000:.2f} km. Skipping."
                    )
                    continue

                seg_geom = extract_line_segment(combined_geom, start_kp, end_kp, distance_calculator)

                if seg_geom and not seg_geom.isEmpty():
                    feat = QgsFeature(fields)
                    feat.setGeometry(seg_geom)
                    attributes = [start_kp, end_kp]
                    for field_name in additional_fields:
                        attributes.append(feature[field_name])
                    attributes.append(input_layer.sourceName())
                    attributes.append(source.sourceName())
                    feat.setAttributes(attributes)
                    sink.addFeature(feat, QgsFeatureSink.FastInsert)
                else:
                    feedback.reportError(f"Could not extract line segment for KP range {start_kp}-{end_kp}. Skipping.")
                feedback.setProgress(int((current + 1) / max(total_rows, 1) * 100))
        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr("""
This tool highlights sections of a line based on KP ranges.

You can provide KP ranges either:

1) From a table layer (like a loaded CSV with no geometry), OR
2) By pasting rows directly from Excel/CSV into the 'Paste KP ranges' box.

For pasted text, the first two columns are interpreted as Start KP and End KP (in km). Any other columns are carried through to the output as text fields.

All carried-through columns will be included in the output layer, along with fields for the source table and line layer names.

**Instructions:**

**Option A: Table layer**

1.  **Load your CSV:** Load your CSV into QGIS ("Add Delimited Text Layer..." -> "No geometry").
2.  **Select Input Layer:** Choose the table layer in 'Input Table of KP Ranges'.
3.  **Map Fields:** Select which columns are Start/End KP.
4.  **Select Line Layer:** Choose the RPL line layer.
5.  **Run**

**Option B: Paste from Excel**

1.  Copy two or more columns from Excel (Start KP, End KP, then any extra columns).
2.  Paste into 'Paste KP ranges (Excel/CSV text)'.
3.  Select the RPL line layer.
4.  Run.
""")

    def name(self):
        return 'kp_range_csv_processor'

    def displayName(self):
        return self.tr('KP Range Highlighter from CSV')

    def group(self):
        # Updated to return the desired group name.
        return self.tr('KP Ranges')

    def groupId(self):
        # Updated to return a unique id for the desired group.
        return 'kp_ranges'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return KPRangeCSVAlgorithm()
