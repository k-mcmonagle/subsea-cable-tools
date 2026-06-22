# import_as_laid_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportAsLaidAlgorithm
Import an as-laid CSV (fixed column schema) as a point layer.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List, Tuple

from qgis.core import QgsField, QgsFields, QgsWkbTypes

from . import cable_lay_parsers as clp
from .cable_lay_import_base import CableLayImportAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING

# Fixed as-laid column order (the file itself has no usable header row).
_COLUMN_NAMES = [
    "Lay Time", "Label", "Body Type", "Slack Change", "Bot Slack",
    "Latitude", "Longitude", "Bearing", "Altercourse", "Cable Type",
    "Cable Dist (km)", "Roto (km)", "New KP(km)", "Path KP(km)",
    "Path Offset(km)", "Path", "Depth (m)", "Bottom Tension(kN)",
]
_TEXT_COLUMNS = ("Lay Time", "Label", "Body Type", "Latitude", "Longitude", "Cable Type", "Path")


class ImportAsLaidAlgorithm(CableLayImportAlgorithm):
    LAYER_TYPE = "as_laid"
    OUTPUT_WKB = QgsWkbTypes.Point
    INPUT_LABEL = "As-Laid CSV File(s)"
    NEEDS_START_DATE = True

    def createInstance(self):
        return ImportAsLaidAlgorithm()

    def name(self):
        return "import_as_laid"

    def displayName(self):
        return self.tr("Import As-Laid")

    def shortHelpString(self):
        return self.tr(
            """
<h3>Import As-Laid</h3>
<p>Imports an as-laid CSV as a <b>point</b> layer. The tool locates the first
data row automatically (the first row whose first column is a
<code>day,HH:MM:SS</code> time) and applies the standard as-laid column
schema.</p>

<h4>Inputs &amp; building up a layer</h4>
<ul>
  <li><b>As-Laid CSV File(s)</b>: one or more files, parsed and merged in a
  single run.</li>
  <li><b>Project Start Date</b> (YYYY-MM-DD): the calendar date of day count 1.</li>
  <li><b>Existing layer to add to</b>: select a pre-created <code>as_laid</code>
  layer (e.g. from <i>Create Cable Lay GeoPackage</i>) to append to.</li>
  <li><b>... or a Target GeoPackage</b>: create/append to a file; the
  <code>as_laid</code> layer is created if missing. Either way, running again
  grows the layer and drops duplicates (same file and timestamp).</li>
</ul>

<h4>Output</h4>
<p>A point layer named <code>as_laid</code> (prefixed with the GeoPackage file
name, e.g. <code>ProjectX_as_laid</code>) in WGS 84 (EPSG:4326) with
<code>ISO_Time</code>, <code>Lat_dd</code>/<code>Lon_dd</code>, the as-laid
columns and a <code>source_file</code> column.</p>
"""
        )

    def parse_rows(self, path, parameters, context, feedback) -> Tuple[List[Dict], QgsFields]:
        start_date = self.read_start_date(parameters, context)
        source_name = os.path.basename(path)

        raw_lines = [ln.rstrip("\r\n") for ln in clp.read_lines(path)]
        data_start = 3
        for i, line in enumerate(raw_lines):
            if not line.strip():
                continue
            first_cell = line.split(",")[0].strip().strip('"')
            if clp.looks_like_day_time(first_cell):
                data_start = i
                break

        split_rows = [row for row in csv.reader(raw_lines[data_start:]) if any(c.strip() for c in row)]
        if not split_rows:
            raise self._error("As-laid file contained no data rows.")
        ncols = len(_COLUMN_NAMES)
        records: List[Dict[str, str]] = []
        for row in split_rows:
            if len(row) < ncols:
                continue
            records.append(dict(zip(_COLUMN_NAMES, row[:ncols])))
        if not records:
            raise self._error(
                f"As-laid rows have fewer than the expected {ncols} columns."
            )

        col_types = clp.infer_column_types(records, text_columns=_TEXT_COLUMNS)

        fields = QgsFields()
        fields.append(QgsField("ISO_Time", FIELD_TYPE_STRING))
        for col in _COLUMN_NAMES:
            fields.append(clp.qgis_field_for(col_types.get(col, "str"), col))
        fields.append(QgsField("Lat_dd", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("Lon_dd", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("source_file", FIELD_TYPE_STRING))

        rows: List[Dict] = []
        skipped = 0
        for record in records:
            iso = clp.parse_day_time(record.get("Lay Time", ""), start_date)
            if iso is None:
                skipped += 1
                continue
            row: Dict = {"ISO_Time": clp.iso_str(iso)}
            for col in _COLUMN_NAMES:
                row[col] = clp.coerce_value(record.get(col, ""), col_types.get(col, "str"))
            lat_dd = clp.parse_dms_to_dd(record.get("Latitude", ""))
            lon_dd = clp.parse_dms_to_dd(record.get("Longitude", ""))
            row["Lat_dd"] = lat_dd
            row["Lon_dd"] = lon_dd
            row["source_file"] = source_name
            row[clp.WKT_KEY] = f"POINT ({lon_dd} {lat_dd})" if None not in (lat_dd, lon_dd) else None
            rows.append(row)

        if skipped:
            feedback.pushInfo(
                f"Skipped {skipped} row(s) whose time did not parse "
                f"(check the Project Start Date)."
            )
        feedback.pushInfo(f"Parsed {len(rows)} as-laid record(s).")
        return rows, fields

    def _error(self, message: str):
        from qgis.core import QgsProcessingException

        return QgsProcessingException(self.tr(message))
