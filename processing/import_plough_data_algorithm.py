# import_plough_data_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportPloughDataAlgorithm
Import a plough data CSV as a point layer.
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

from qgis.core import QgsField, QgsFields, QgsWkbTypes

from . import cable_lay_parsers as clp
from .cable_lay_import_base import CableLayImportAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING

_REQUIRED = ("Record", "Time", "Latitude", "Longitude")
_TEXT_COLUMNS = ("Time", "Latitude", "Longitude")


class ImportPloughDataAlgorithm(CableLayImportAlgorithm):
    LAYER_TYPE = "plough_data"
    OUTPUT_WKB = QgsWkbTypes.Point
    INPUT_LABEL = "Plough Data CSV File(s)"
    NEEDS_START_DATE = True

    def createInstance(self):
        return ImportPloughDataAlgorithm()

    def name(self):
        return "import_plough_data"

    def displayName(self):
        return self.tr("Import Plough Data")

    def shortHelpString(self):
        return self.tr(
            """
<h3>Import Plough Data</h3>
<p>Imports a plough data CSV as a <b>point</b> layer. The first row is the
header; a units row immediately below it is skipped automatically.</p>

<h4>Required columns</h4>
<p><code>Record</code>, <code>Time</code> (<code>day,HH:MM:SS</code>),
<code>Latitude</code> and <code>Longitude</code> (degrees and decimal minutes,
e.g. <code>17 09.7399N</code>).</p>

<h4>Inputs &amp; building up a layer</h4>
<ul>
  <li><b>Plough Data CSV File(s)</b>: one or more files, parsed and merged in a
  single run.</li>
  <li><b>Project Start Date</b> (YYYY-MM-DD): the calendar date of day count 1.</li>
  <li><b>Existing layer to add to</b>: select a pre-created <code>plough_data</code>
  layer (e.g. from <i>Create Cable Lay GeoPackage</i>) to append to.</li>
  <li><b>... or a Target GeoPackage</b>: create/append to a file; the
  <code>plough_data</code> layer is created if missing. Either way, running again
  grows the layer and drops duplicates (same file, timestamp and record number).</li>
</ul>

<h4>Output</h4>
<p>A point layer named <code>plough_data</code> (prefixed with the GeoPackage file
name, e.g. <code>ProjectX_plough_data</code>) in WGS 84 (EPSG:4326) with
<code>ISO_Time</code>, <code>Lat_dd</code>/<code>Lon_dd</code>, the plough
columns and a <code>source_file</code> column.</p>
"""
        )

    def parse_rows(self, path, parameters, context, feedback) -> Tuple[List[Dict], QgsFields]:
        start_date = self.read_start_date(parameters, context)
        source_name = os.path.basename(path)

        rows, _ = clp.read_csv_rows(path, delimiter=",")
        nonblank = clp.non_blank_rows(rows)
        if len(nonblank) < 3:
            raise self._error("Plough CSV is missing a header, units row or data.")

        headers = [clp.normalize_column_name(c) for c in nonblank[0]]
        # nonblank[1] is the units row and is skipped.
        records: List[Dict[str, str]] = [
            dict(zip(headers, row)) for row in nonblank[2:]
        ]

        missing = [c for c in _REQUIRED if c not in headers]
        if missing:
            raise self._error(
                "Plough CSV missing required columns: " + ", ".join(missing)
            )

        col_types = clp.infer_column_types(records, text_columns=_TEXT_COLUMNS)
        col_types["Record"] = "int"  # Record is an index even if stored as text

        fields = QgsFields()
        fields.append(QgsField("ISO_Time", FIELD_TYPE_STRING))
        for col in headers:
            fields.append(clp.qgis_field_for(col_types.get(col, "str"), col))
        fields.append(QgsField("Lat_dd", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("Lon_dd", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("source_file", FIELD_TYPE_STRING))

        out_rows: List[Dict] = []
        skipped = 0
        for record in records:
            iso = clp.parse_day_time(record.get("Time", ""), start_date)
            if iso is None:
                skipped += 1
                continue
            row: Dict = {"ISO_Time": clp.iso_str(iso)}
            for col in headers:
                row[col] = clp.coerce_value(record.get(col, ""), col_types.get(col, "str"))
            lat_dd = clp.parse_dms_to_dd(record.get("Latitude", ""))
            lon_dd = clp.parse_dms_to_dd(record.get("Longitude", ""))
            row["Lat_dd"] = lat_dd
            row["Lon_dd"] = lon_dd
            row["source_file"] = source_name
            row[clp.WKT_KEY] = f"POINT ({lon_dd} {lat_dd})" if None not in (lat_dd, lon_dd) else None
            out_rows.append(row)

        if skipped:
            feedback.pushInfo(
                f"Skipped {skipped} row(s) whose time did not parse "
                f"(check the Project Start Date)."
            )
        feedback.pushInfo(f"Parsed {len(out_rows)} plough record(s).")
        return out_rows, fields

    def _error(self, message: str):
        from qgis.core import QgsProcessingException

        return QgsProcessingException(self.tr(message))
