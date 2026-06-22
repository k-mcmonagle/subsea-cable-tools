# import_body_log_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportBodyLogAlgorithm
Import a cable lay body / touchdown log as a point layer.
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

from qgis.core import QgsFields, QgsWkbTypes

from . import cable_lay_parsers as clp
from .cable_lay_import_base import CableLayImportAlgorithm


class ImportBodyLogAlgorithm(CableLayImportAlgorithm):
    LAYER_TYPE = "body_logs"
    OUTPUT_WKB = QgsWkbTypes.Point
    INPUT_LABEL = "Body Log File(s)"
    NEEDS_START_DATE = False

    def createInstance(self):
        return ImportBodyLogAlgorithm()

    def name(self):
        return "import_body_log"

    def displayName(self):
        return self.tr("Import Body Log")

    def shortHelpString(self):
        return self.tr(
            """
<h3>Import Body Log</h3>
<p>Imports a cable lay body / touchdown log as a <b>point</b> layer, with one
point per body at its touchdown position.</p>

<h4>Expected format</h4>
<p>A whitespace-delimited text/log file. The first four lines are treated as a
header and skipped. Each data line begins with a (possibly multi-word) body
label, followed by the touchdown cable distance, the touchdown latitude and
longitude (degrees and decimal minutes, e.g. <code>17 09.7399 N</code>), water
depth, KP, distance off path, side, average seabed slack, deviation from target
and inline offset.</p>

<h4>Inputs &amp; building up a layer</h4>
<p>Select one or more body log files; they are parsed and merged in a single
run. For the destination, either pick an <b>Existing layer to add to</b> (a
pre-created <code>body_logs</code> layer, e.g. from <i>Create Cable Lay
GeoPackage</i>) or choose a <b>Target GeoPackage</b> to create/append to (the
<code>body_logs</code> layer is created if missing). Running again with more
files grows that same layer; duplicates (same file and body label) are dropped.</p>

<h4>Output</h4>
<p>A point layer named <code>body_logs</code> (prefixed with the GeoPackage file
name, e.g. <code>ProjectX_body_logs</code>) in WGS 84 (EPSG:4326). A
<code>body_file</code> column records the source file.</p>
"""
        )

    def parse_rows(self, path, parameters, context, feedback) -> Tuple[List[Dict], QgsFields]:
        fields = clp.fields_from_specs(clp.BODY_FIELD_SPECS)
        source_name = os.path.basename(path)
        lines = clp.read_lines(path)[4:]  # skip four header lines
        rows: List[Dict] = []
        skipped = 0

        for line in lines:
            tokens = clp.tokenize(line)
            if len(tokens) < 15:
                continue

            # The leading body label is everything up to the first numeric token.
            idx = 0
            label_parts: List[str] = []
            while idx < len(tokens) and not clp.is_float_like(tokens[idx]):
                label_parts.append(tokens[idx])
                idx += 1
            if idx >= len(tokens):
                skipped += 1
                continue

            try:
                touchdown_cable_dist = float(tokens[idx]); idx += 1
                lat = " ".join(tokens[idx:idx + 3]); idx += 3
                lon = " ".join(tokens[idx:idx + 3]); idx += 3
                water_depth = float(tokens[idx]); idx += 1
                kp = float(tokens[idx]); idx += 1
                td_distance_off_path = float(tokens[idx]); idx += 1
                side = tokens[idx]; idx += 1
                avg_seabed_slack = float(tokens[idx]); idx += 1
                deviation_from_target = float(tokens[idx]); idx += 1
                inline_offset = float(tokens[idx]); idx += 1
            except (ValueError, IndexError):
                skipped += 1
                continue

            row = {
                "Body_Label": " ".join(label_parts),
                "Touchdown_Cable_Dist_m": touchdown_cable_dist,
                "Touchdown_Latitude": lat,
                "Touchdown_Longitude": lon,
                "Water_Depth_m": water_depth,
                "Touchdown_KP_km": kp,
                "Td_Distance_Off_Path_m": td_distance_off_path,
                "Side": side,
                "Avg_Seabed_Slack_pct": avg_seabed_slack,
                "Deviation_From_Target_m": deviation_from_target,
                "Inline_Offset_m": inline_offset,
                "body_file": source_name,
            }

            lat_dd = clp.parse_dms_to_dd(lat)
            lon_dd = clp.parse_dms_to_dd(lon)
            row[clp.WKT_KEY] = f"POINT ({lon_dd} {lat_dd})" if None not in (lat_dd, lon_dd) else None
            rows.append(row)

        if skipped:
            feedback.pushInfo(f"Skipped {skipped} malformed body-log line(s).")
        feedback.pushInfo(f"Parsed {len(rows)} body-log record(s).")
        return rows, fields
