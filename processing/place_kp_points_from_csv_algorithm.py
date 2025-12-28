"""PlaceKpPointsFromCsvAlgorithm

Places points along a route based on KP values.

KP inputs can come from:
- a table layer (e.g. a loaded CSV with no geometry), or
- a pasted text block (e.g. copied from Excel; typically tab-separated).

For pasted text, the first column is interpreted as KP (km). Any additional
columns are carried through to the output as text fields.
"""

from __future__ import annotations

import csv
import io
import re
from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterField,
                       QgsProcessingParameterString,
                       QgsFeature,
                       QgsGeometry,
                       QgsPointXY,
                       QgsFields,
                       QgsField,
                       QgsWkbTypes,
                       QgsDistanceArea,
                       QgsProcessingException)

class PlaceKpPointsFromCsvAlgorithm(QgsProcessingAlgorithm):
    INPUT_TABLE = 'INPUT_TABLE'
    INPUT_LINE = 'INPUT_LINE'
    KP_FIELD = 'KP_FIELD'
    PASTED_KPS = 'PASTED_KPS'
    OUTPUT = 'OUTPUT'

    _DEFAULT_COL_PREFIX = 'col_'

    @staticmethod
    def _safe_field_name(name: str, used: set) -> str:
        name = (name or '').strip()
        if not name:
            name = 'col'
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
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None

        text = text.replace("\u00a0", " ").strip()
        if "," in text and "." not in text:
            text = text.replace(",", ".")
        elif "," in text and "." in text:
            text = text.replace(",", "")

        cleaned = re.sub(r"[^0-9+\-\.]+", "", text)
        if cleaned in {"", ".", "+", "-"}:
            return None
        try:
            return float(cleaned)
        except Exception:
            return None

    @classmethod
    def _parse_pasted_kps(cls, raw_text: str) -> Tuple[List[str], List[Tuple[float, List[str]]]]:
        """Parse pasted rows into (extra_column_names, rows).

        Returns:
            extra_column_names: list of names for columns 2..N
            rows: list of tuples (kp_value, extras)
        """

        if raw_text is None:
            return ([], [])
        text = str(raw_text).strip()
        if not text:
            return ([], [])

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
            trimmed = [str(c).strip() for c in row]
            if not any(trimmed):
                continue
            parsed_rows.append(trimmed)

        if not parsed_rows:
            return ([], [])

        header = None
        first = parsed_rows[0]
        first_kp = first[0] if len(first) > 0 else ""
        if cls._parse_kp(first_kp) is None:
            header = first
            data_rows = parsed_rows[1:]
        else:
            data_rows = parsed_rows

        max_cols = max(len(r) for r in ([header] if header else []) + data_rows) if data_rows else (len(header) if header else 1)
        max_cols = max(max_cols, 1)
        extra_count = max(0, max_cols - 1)

        used_names = set()
        extra_names: List[str] = []
        for idx in range(extra_count):
            if header and len(header) >= (idx + 2) and header[idx + 1].strip():
                candidate = header[idx + 1]
            else:
                candidate = f"{cls._DEFAULT_COL_PREFIX}{idx + 2}"
            extra_names.append(cls._safe_field_name(candidate, used_names))

        rows: List[Tuple[float, List[str]]] = []
        for r in data_rows:
            if len(r) < 1:
                continue
            kp = cls._parse_kp(r[0])
            if kp is None:
                continue
            extras: List[str] = []
            for idx in range(extra_count):
                extras.append(r[idx + 1] if len(r) >= (idx + 2) else "")
            rows.append((float(kp), extras))

        return (extra_names, rows)

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_TABLE,
                self.tr('Input Table of KPs'),
                [QgsProcessing.TypeVector],
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.PASTED_KPS,
                self.tr('Paste KP points (Excel/CSV text)'),
                multiLine=True,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LINE,
                self.tr('Input Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.KP_FIELD,
                self.tr('KP Field'),
                parentLayerParameterName=self.INPUT_TABLE,
                type=QgsProcessingParameterField.Numeric,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output Point Layer')
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        line_layer = self.parameterAsSource(parameters, self.INPUT_LINE, context)

        pasted_text = self.parameterAsString(parameters, self.PASTED_KPS, context)
        use_pasted = bool(pasted_text and pasted_text.strip())

        input_table = None
        kp_field = None
        extra_names: List[str] = []
        pasted_rows: List[Tuple[float, List[str]]] = []

        if use_pasted:
            extra_names, pasted_rows = self._parse_pasted_kps(pasted_text)
            if not pasted_rows:
                feedback.reportError('No valid KP rows parsed from pasted text.')
        else:
            input_table = self.parameterAsSource(parameters, self.INPUT_TABLE, context)
            kp_field = self.parameterAsString(parameters, self.KP_FIELD, context)
            if input_table is None:
                raise QgsProcessingException('Provide either an input table of KPs or pasted KP points text.')
            if not kp_field:
                raise QgsProcessingException('KP Field is required when using an input table layer.')

        if line_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_LINE))

        # Create fields for the output layer
        output_fields = QgsFields()
        source_fields = None
        if use_pasted:
            # For pasted values, we only have extras as strings
            for name in extra_names:
                output_fields.append(QgsField(name, QVariant.String))
        else:
            # Copy all fields from the input table
            source_fields = input_table.fields()
            for field in source_fields:
                output_fields.append(field)

        output_fields.append(QgsField('source_line', QVariant.String))
        output_fields.append(QgsField('kp_value', QVariant.Double))


        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, output_fields, QgsWkbTypes.Point, line_layer.sourceCrs()
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        # Dissolve line layer into a single geometry
        line_features = list(line_layer.getFeatures())
        if not line_features:
            raise QgsProcessingException(self.tr("Input line layer has no features."))
        
        geometries = [f.geometry() for f in line_features]
        merged_geometry = QgsGeometry.unaryUnion(geometries)
        
        if merged_geometry.isEmpty():
            raise QgsProcessingException(self.tr("Geometry is empty after merging features."))

        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(line_layer.sourceCrs(), context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

        total_length_m = distance_calculator.measureLength(merged_geometry)
        if total_length_m == 0:
            raise QgsProcessingException(self.tr("Line has no length."))

        line_parts = merged_geometry.asMultiPolyline() if merged_geometry.isMultipart() else [merged_geometry.asPolyline()]


        def _place_point_at_kp(kp_val: float) -> Optional[QgsGeometry]:
            kp_dist_m = kp_val * 1000.0
            if kp_dist_m > total_length_m:
                return None

            cumulative_length = 0.0
            for part in line_parts:
                for i in range(len(part) - 1):
                    p1, p2 = part[i], part[i + 1]
                    segment_length = float(distance_calculator.measureLine(p1, p2))
                    if segment_length <= 0:
                        continue

                    if cumulative_length + segment_length >= kp_dist_m:
                        dist_into_segment = kp_dist_m - cumulative_length
                        ratio = dist_into_segment / segment_length
                        x = p1.x() + ratio * (p2.x() - p1.x())
                        y = p1.y() + ratio * (p2.y() - p1.y())
                        return QgsGeometry.fromPointXY(QgsPointXY(x, y))

                    cumulative_length += segment_length
            return None

        points_placed = 0
        if use_pasted:
            total_rows = len(pasted_rows)
            for current, (kp_val, extras) in enumerate(pasted_rows):
                if feedback.isCanceled():
                    break

                point_geom = _place_point_at_kp(kp_val)
                if point_geom is None:
                    feedback.reportError(
                        f"KP value {kp_val} is beyond the line's total length of {total_length_m/1000:.3f} km, or could not be placed. Skipping."
                    )
                    continue

                out_feat = QgsFeature(output_fields)
                out_feat.setGeometry(point_geom)

                # extras first (as defined by output_fields)
                for idx, val in enumerate(extras):
                    out_feat.setAttribute(idx, val)

                out_feat.setAttribute('source_line', line_layer.sourceName())
                out_feat.setAttribute('kp_value', kp_val)

                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                points_placed += 1

                feedback.setProgress(int((current + 1) / max(total_rows, 1) * 100))
        else:
            input_features = list(input_table.getFeatures())
            total_rows = len(input_features)

            for current, feature in enumerate(input_features):
                if feedback.isCanceled():
                    break

                try:
                    kp_val = float(feature[kp_field])
                except (ValueError, KeyError, TypeError):
                    feedback.reportError(f"Invalid KP value in row {current + 1}. Skipping.")
                    continue

                point_geom = _place_point_at_kp(kp_val)
                if point_geom is None:
                    feedback.reportError(
                        f"KP value {kp_val} is beyond the line's total length of {total_length_m/1000:.3f} km, or could not be placed. Skipping."
                    )
                    continue

                out_feat = QgsFeature(output_fields)
                out_feat.setGeometry(point_geom)

                # Copy attributes from source feature
                for i in range(len(source_fields)):
                    out_feat.setAttribute(i, feature.attribute(source_fields.at(i).name()))

                out_feat.setAttribute('source_line', line_layer.sourceName())
                out_feat.setAttribute('kp_value', kp_val)

                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                points_placed += 1

                feedback.setProgress(int((current + 1) / max(total_rows, 1) * 100))

        feedback.pushInfo(self.tr(f"Placed {points_placed} points."))
        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr("""
This tool places points along a line layer based on KP values.

You can provide KPs either:

1) From a table layer (like a loaded CSV with no geometry), OR
2) By pasting rows directly from Excel/CSV into the 'Paste KP points' box.

For pasted text, the first column is interpreted as KP (in km). Any other columns are carried through to the output as text fields.

**Instructions:**

**Option A: Table layer**

1.  **Load your CSV:** Load your CSV into QGIS ("Add Delimited Text Layer..." -> "No geometry").
2.  **Input Table of KPs:** Select the table layer.
3.  **KP Field:** Select the numeric KP column.
4.  **Input Line Layer:** Choose the line layer route.
5.  **Run**

**Option B: Paste from Excel**

1.  Copy one or more columns from Excel (KP, then any extra columns).
2.  Paste into 'Paste KP points (Excel/CSV text)'.
3.  Select the line layer route.
4.  Run.
""")

    def name(self):
        return 'placekppointsfromcsv'

    def displayName(self):
        return self.tr('Place KP Points from CSV')

    def group(self):
        return self.tr('KP Points')

    def groupId(self):
        return 'kppoints'

    def createInstance(self):
        return PlaceKpPointsFromCsvAlgorithm()

    def tr(self, string):
        return QCoreApplication.translate("PlaceKpPointsFromCsvAlgorithm", string)
