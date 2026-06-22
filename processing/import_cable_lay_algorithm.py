# import_cable_lay_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportCableLayAlgorithm
Import cable lay position CSV data to a GeoPackage point layer.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List, Tuple

from qgis.core import (
    QgsField,
    QgsFields,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsWkbTypes,
)

from . import cable_lay_parsers as clp
from .cable_lay_import_base import CableLayImportAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING, PROCESSING_NUMBER_INTEGER

# Columns that should always be treated as text even if they look numeric.
_TEXT_COLUMNS = (
    "Record", "Plow Status", "Roto Select", "Ship Latitude", "Ship Longitude",
    "Plow Latitude (USBL)", "Plow Longitude (USBL)", "Plow Latitude (Tow Wire)",
    "Plow Longitude (Tow Wire)", "Time",
)
_UNIT_HINTS = ("km", "hr", "deg", "m", "kn", "%", "dd,hh:mm:ss")
_REQUIRED = ("Time", "Ship Latitude", "Ship Longitude")


class ImportCableLayAlgorithm(CableLayImportAlgorithm):
    PARSE_TIME = "PARSE_TIME"
    DOWNSAMPLE = "DOWNSAMPLE"

    LAYER_TYPE = "cable_lay"
    OUTPUT_WKB = QgsWkbTypes.Point
    INPUT_LABEL = "Cable Lay CSV File(s)"
    NEEDS_START_DATE = False  # only needed when time parsing is enabled

    def createInstance(self):
        return ImportCableLayAlgorithm()

    def name(self):
        return "import_cable_lay"

    def displayName(self):
        return self.tr("Import Cable Lay Data (CSV)")

    def add_extra_parameters(self):
        self.addParameter(
            QgsProcessingParameterString(
                self.START_DATE,
                self.tr("Project Start Date (YYYY-MM-DD) - only needed if parsing time"),
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PARSE_TIME,
                self.tr("Parse Time Data (uncheck to skip, e.g. for date line crossings)"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.DOWNSAMPLE,
                self.tr("Downsample Factor (load every Nth record, 1 = all)"),
                type=PROCESSING_NUMBER_INTEGER,
                defaultValue=1,
                minValue=1,
            )
        )

    def dedupe_key(self, parameters, context):
        # With ISO time parsed, dedupe on the timestamp; otherwise fall back to
        # the raw day-count Time column so rows are not collapsed to one per file.
        if self.parameterAsBool(parameters, self.PARSE_TIME, context):
            return ["ISO_Time", "source_file"]
        return ["Time", "source_file"]

    def shortHelpString(self):
        return self.tr(
            """
<h3>Import Cable Lay Data (CSV)</h3>
<p>Imports cable lay position CSV files as a <b>point</b> layer. Ship positions
in degrees and decimal minutes (e.g. <code>01 19.4445N</code>) are converted to
decimal degrees, numeric columns are detected automatically, and a units row
(if present) is skipped.</p>

<h4>Required columns</h4>
<ul>
  <li><b>Time</b>: <code>day,HH:MM:SS</code> (e.g. <code>12,14:23:45</code>)</li>
  <li><b>Ship Latitude</b> / <b>Ship Longitude</b>: degrees and decimal minutes</li>
</ul>
<p>All other columns are imported and preserved with appropriate types.</p>

<h4>Inputs &amp; building up a layer</h4>
<ul>
  <li><b>Cable Lay CSV File(s)</b>: one or more files, parsed and merged in a
  single run.</li>
  <li><b>Project Start Date</b> (YYYY-MM-DD): needed only when <i>Parse Time
  Data</i> is on; converts the relative times into ISO timestamps.</li>
  <li><b>Parse Time Data</b>: uncheck to keep the original day-count format
  (useful for date-line crossings).</li>
  <li><b>Downsample Factor</b>: load every Nth record to thin large files.</li>
  <li><b>Existing layer to add to</b>: select a pre-created <code>cable_lay</code>
  layer (e.g. from <i>Create Cable Lay GeoPackage</i>) to append to.</li>
  <li><b>... or a Target GeoPackage</b>: create/append to a file; the
  <code>cable_lay</code> layer is created if missing. Either way, running again
  grows the layer and drops duplicates (same timestamp and source file).</li>
</ul>

<h4>Output</h4>
<p>A point layer named <code>cable_lay</code> (prefixed with the GeoPackage file
name, e.g. <code>ProjectX_cable_lay</code>) in WGS 84 (EPSG:4326) with
<code>ISO_Time</code> (when time parsing is on), <code>Lat_dd</code>/<code>Lon_dd</code>,
the original columns, and a <code>source_file</code> column.</p>
"""
        )

    def parse_rows(self, path, parameters, context, feedback) -> Tuple[List[Dict], QgsFields]:
        parse_time = self.parameterAsBool(parameters, self.PARSE_TIME, context)
        downsample = self.parameterAsInt(parameters, self.DOWNSAMPLE, context)
        start_date = self.parameterAsString(parameters, self.START_DATE, context).strip()
        if parse_time and not start_date:
            raise self._error("Project Start Date is required when time parsing is enabled.")
        source_name = os.path.basename(path)

        rows = clp.read_csv_rows(path, delimiter=",")[0]
        if len(rows) < 2:
            raise self._error("CSV file is empty or missing a header.")
        header = [clp.normalize_column_name(c) for c in rows[0]]

        # Skip a units row (starts with '#' or carries unit hints) if present.
        data_start = 1
        if len(rows) > 1:
            second = rows[1]
            if second and (
                str(second[0]).strip().startswith("#")
                or any(hint in str(cell).lower() for cell in second[:5] for hint in _UNIT_HINTS)
            ):
                data_start = 2
                feedback.pushInfo(f"Detected and skipped units row in {source_name}.")

        data_rows = rows[data_start:]
        if downsample > 1:
            data_rows = data_rows[::downsample]
        records = [dict(zip(header, row)) for row in data_rows if len(row) == len(header)]

        for col in _REQUIRED:
            if not records or col not in header:
                raise self._error(f"Missing required column: {col}")

        col_types = clp.infer_column_types(records, text_columns=_TEXT_COLUMNS)

        fields = QgsFields()
        if parse_time:
            fields.append(QgsField("ISO_Time", FIELD_TYPE_STRING))
        for col in header:
            fields.append(clp.qgis_field_for(col_types.get(col, "str"), col))
        for extra in ("Lat_dd", "Lon_dd"):
            if extra not in header:
                fields.append(QgsField(extra, FIELD_TYPE_DOUBLE))
        if "source_file" not in header:
            fields.append(QgsField("source_file", FIELD_TYPE_STRING))

        out_rows: List[Dict] = []
        skipped = 0
        for record in records:
            iso_val = None
            if parse_time:
                iso_dt = clp.parse_day_time(record.get("Time", ""), start_date)
                if iso_dt is None:
                    skipped += 1
                    continue
                iso_val = clp.iso_str(iso_dt)
            lat_dd = clp.parse_dms_to_dd(record.get("Ship Latitude", ""))
            lon_dd = clp.parse_dms_to_dd(record.get("Ship Longitude", ""))
            if lat_dd is None or lon_dd is None:
                skipped += 1
                continue
            row: Dict = {}
            if parse_time:
                row["ISO_Time"] = iso_val
            for col in header:
                row[col] = clp.coerce_value(record.get(col, ""), col_types.get(col, "str"))
            row["Lat_dd"] = lat_dd
            row["Lon_dd"] = lon_dd
            row["source_file"] = source_name
            row[clp.WKT_KEY] = f"POINT ({lon_dd} {lat_dd})"
            out_rows.append(row)

        if skipped:
            feedback.pushInfo(f"Skipped {skipped} row(s) with unparseable time or position.")
        feedback.pushInfo(f"Parsed {len(out_rows)} cable-lay record(s) from {source_name}.")
        return out_rows, fields

    def _error(self, message: str):
        from qgis.core import QgsProcessingException

        return QgsProcessingException(self.tr(message))
