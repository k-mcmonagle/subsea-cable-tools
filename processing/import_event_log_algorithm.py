# import_event_log_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportEventLogAlgorithm
Import a cable lay event log as a point layer (geometry optional).
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List, Tuple

from qgis.core import QgsField, QgsFields, QgsWkbTypes

from . import cable_lay_parsers as clp
from .cable_lay_import_base import CableLayImportAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING


class ImportEventLogAlgorithm(CableLayImportAlgorithm):
    LAYER_TYPE = "event_logs"
    OUTPUT_WKB = QgsWkbTypes.Point
    INPUT_LABEL = "Event Log File(s)"
    NEEDS_START_DATE = True

    def createInstance(self):
        return ImportEventLogAlgorithm()

    def name(self):
        return "import_event_log"

    def displayName(self):
        return self.tr("Import Event Log")

    def shortHelpString(self):
        return self.tr(
            """
<h3>Import Event Log</h3>
<p>Imports a cable lay event log as a <b>point</b> layer. Rows that carry a
latitude/longitude pair are placed as points; rows without coordinates are still
imported with no geometry so the full event history is preserved.</p>

<h4>Expected format</h4>
<p>A delimited text file with a header. Several styles are accepted: a leading
comment header (<code># ...</code>), a CSV header whose first cell is
<code>#</code> (<code>#,Time,Event Description,...</code>), a tab-separated
equivalent, or whitespace separation. A <code>Time</code> (or
<code>Event Time</code>) column in <code>day,HH:MM:SS</code> format is required.</p>

<h4>Inputs &amp; building up a layer</h4>
<ul>
  <li><b>Event Log File(s)</b>: one or more files, parsed and merged in a single
  run.</li>
  <li><b>Project Start Date</b> (YYYY-MM-DD): the calendar date of day count 1,
  used to turn the relative times into ISO timestamps.</li>
  <li><b>Existing layer to add to</b>: select a pre-created <code>event_logs</code>
  layer (e.g. from <i>Create Cable Lay GeoPackage</i>) to append to.</li>
  <li><b>... or a Target GeoPackage</b>: create/append to a file; the
  <code>event_logs</code> layer is created if missing. Either way, running again
  grows the layer and drops duplicates (same file and timestamp).</li>
</ul>

<h4>Output</h4>
<p>A point layer named <code>event_logs</code> (prefixed with the GeoPackage file
name, e.g. <code>ProjectX_event_logs</code>) in WGS 84 (EPSG:4326) with an
<code>ISO_Time</code> field, the original columns, and an <code>event_file</code>
column for traceability.</p>
"""
        )

    def parse_rows(self, path, parameters, context, feedback) -> Tuple[List[Dict], QgsFields]:
        start_date = self.read_start_date(parameters, context)
        source_name = os.path.basename(path)

        raw_lines = [ln.rstrip("\r\n") for ln in clp.read_lines(path)]
        if not raw_lines:
            raise self._error("Event log file is empty.")
        first = raw_lines[0].lstrip("﻿")

        header_like_csv = first.startswith("#,")
        header_like_tab = first.startswith("#\t")
        legacy = first.lstrip().startswith("#") and not (header_like_csv or header_like_tab)
        header_probe = first[1:].strip() if legacy else first

        if "," in header_probe:
            delimiter = ","
        elif "\t" in header_probe:
            delimiter = "\t"
        else:
            delimiter = None  # whitespace

        def split_row(text: str) -> List[str]:
            if delimiter is None:
                return clp.tokenize(text)
            if delimiter == ",":
                return next(csv.reader([text]))
            return [cell.strip() for cell in text.split("\t")]

        header_source = header_probe if legacy else first
        headers = split_row(header_source)
        if not legacy:
            headers = ["Record" if h.strip() == "#" else h for h in headers]

        data_split = [split_row(ln) for ln in raw_lines[1:] if ln.strip()]
        max_cols = max([len(headers)] + [len(r) for r in data_split]) if data_split else len(headers)
        for i in range(len(headers), max_cols):
            headers.append(f"Col_{i + 1}")
        headers = [clp.normalize_column_name(h) or f"Col_{i + 1}" for i, h in enumerate(headers)]

        records: List[Dict[str, str]] = [
            dict(zip(headers, row + [""] * (max_cols - len(row)))) for row in data_split
        ]
        if not records:
            raise self._error("Event log contained no data rows.")

        time_col = "Time" if "Time" in headers else ("Event Time" if "Event Time" in headers else None)
        if time_col is None:
            raise self._error("Event log missing a 'Time' or 'Event Time' column.")

        lat_col = next((c for c in headers if "Lat" in c and c != "Lat_dd"), None)
        lon_col = next((c for c in headers if "Lon" in c and c != "Lon_dd"), None)
        has_geometry = bool(lat_col and lon_col)

        col_types = clp.infer_column_types(records, text_columns=[time_col])

        fields = QgsFields()
        fields.append(QgsField("ISO_Time", FIELD_TYPE_STRING))
        for col in headers:
            fields.append(clp.qgis_field_for(col_types.get(col, "str"), col))
        if has_geometry:
            fields.append(QgsField("Lat_dd", FIELD_TYPE_DOUBLE))
            fields.append(QgsField("Lon_dd", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("event_file", FIELD_TYPE_STRING))

        rows: List[Dict] = []
        skipped = 0
        for record in records:
            iso = clp.parse_day_time(record.get(time_col, ""), start_date)
            if iso is None:
                skipped += 1
                continue
            row: Dict = {"ISO_Time": clp.iso_str(iso)}
            for col in headers:
                row[col] = clp.coerce_value(record.get(col, ""), col_types.get(col, "str"))
            if has_geometry:
                lat_dd = clp.parse_dms_to_dd(record.get(lat_col, ""))
                lon_dd = clp.parse_dms_to_dd(record.get(lon_col, ""))
                row["Lat_dd"] = lat_dd
                row["Lon_dd"] = lon_dd
                row[clp.WKT_KEY] = (
                    f"POINT ({lon_dd} {lat_dd})" if None not in (lat_dd, lon_dd) else None
                )
            else:
                row[clp.WKT_KEY] = None
            rows.append(row)

        if skipped:
            feedback.pushInfo(
                f"Skipped {skipped} row(s) whose time did not parse "
                f"(check the Project Start Date)."
            )
        feedback.pushInfo(f"Parsed {len(rows)} event-log record(s).")
        return rows, fields

    def _error(self, message: str):
        from qgis.core import QgsProcessingException

        return QgsProcessingException(self.tr(message))
