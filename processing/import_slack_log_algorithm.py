# import_slack_log_algorithm.py
# -*- coding: utf-8 -*-
"""
ImportSlackLogAlgorithm
Import a cable lay slack log as a line layer (one segment per record).
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

from qgis.core import QgsFields, QgsProcessing, QgsWkbTypes

from . import cable_lay_parsers as clp
from .cable_lay_import_base import CableLayImportAlgorithm


class ImportSlackLogAlgorithm(CableLayImportAlgorithm):
    LAYER_TYPE = "slack_logs"
    OUTPUT_WKB = QgsWkbTypes.LineString
    TARGET_LAYER_TYPE = QgsProcessing.TypeVectorLine
    INPUT_LABEL = "Slack Log File(s)"
    NEEDS_START_DATE = False

    def createInstance(self):
        return ImportSlackLogAlgorithm()

    def name(self):
        return "import_slack_log"

    def displayName(self):
        return self.tr("Import Slack Log")

    def shortHelpString(self):
        return self.tr(
            """
<h3>Import Slack Log</h3>
<p>Imports a cable lay slack log as a <b>line</b> layer. Each record becomes a
short line segment between its two positions, carrying the KP, depth, cable
off-path and slack values as attributes.</p>

<h4>Expected format</h4>
<p>A whitespace-delimited text/log file. The first two lines are treated as a
header and skipped. Each data line provides at least 16 whitespace-separated
values: <code>KP1 KP2 lat1 lon1 lat2 lon2 off_path1 off_path2 depth1 depth2
seabed_slack ship_slack ...labels</code>, where each latitude/longitude is given
as degrees and decimal minutes (e.g. <code>17 09.7399 N</code>).</p>

<h4>Inputs &amp; building up a layer</h4>
<p>Select one or more slack log files; they are parsed and merged in a single
run. For the destination, either pick an <b>Existing layer to add to</b> (a
pre-created <code>slack_logs</code> layer, e.g. from <i>Create Cable Lay
GeoPackage</i>) or choose a <b>Target GeoPackage</b> to create/append to (the
<code>slack_logs</code> layer is created if missing). Running again with more
files grows that same layer; duplicates (same file, KP1 and KP2) are dropped, so
re-importing a file is safe.</p>

<h4>Output</h4>
<p>A LineString layer named <code>slack_logs</code> (prefixed with the GeoPackage
file name, e.g. <code>ProjectX_slack_logs</code>) in WGS 84 (EPSG:4326). A
<code>slack_file</code> column records the source file for traceability.</p>
"""
        )

    def parse_rows(self, path, parameters, context, feedback) -> Tuple[List[Dict], QgsFields]:
        fields = clp.fields_from_specs(clp.SLACK_FIELD_SPECS)
        source_name = os.path.basename(path)
        lines = clp.read_lines(path)[2:]  # skip the two header lines
        rows: List[Dict] = []
        skipped = 0

        for line in lines:
            tokens = clp.tokenize(line)
            if len(tokens) < 16:
                continue
            try:
                kp1, kp2 = float(tokens[0]), float(tokens[1])
                lat1 = f"{tokens[2]} {tokens[3]}"
                lon1 = f"{tokens[4]} {tokens[5]}"
                lat2 = f"{tokens[6]} {tokens[7]}"
                lon2 = f"{tokens[8]} {tokens[9]}"
                off1, off2 = float(tokens[10]), float(tokens[11])
                depth1, depth2 = float(tokens[12]), float(tokens[13])
                seabed_slack, ship_slack = float(tokens[14]), float(tokens[15])
            except (ValueError, IndexError):
                skipped += 1
                continue
            labels = " ".join(tokens[16:])

            row = {
                "KP1": kp1,
                "KP2": kp2,
                "lat1": lat1,
                "lon1": lon1,
                "lat2": lat2,
                "lon2": lon2,
                "Cable_off_path1": off1,
                "Cable_off_path2": off2,
                "Depth1": depth1,
                "Depth2": depth2,
                "Seabed_slack": seabed_slack,
                "Ship_slack": ship_slack,
                "Cable_and_Body_Labels": labels,
                "slack_file": source_name,
            }

            lat1_dd = clp.parse_dms_to_dd(lat1)
            lon1_dd = clp.parse_dms_to_dd(lon1)
            lat2_dd = clp.parse_dms_to_dd(lat2)
            lon2_dd = clp.parse_dms_to_dd(lon2)
            if None not in (lat1_dd, lon1_dd, lat2_dd, lon2_dd):
                row[clp.WKT_KEY] = (
                    f"LINESTRING ({lon1_dd} {lat1_dd}, {lon2_dd} {lat2_dd})"
                )
            else:
                row[clp.WKT_KEY] = None
            rows.append(row)

        if skipped:
            feedback.pushInfo(f"Skipped {skipped} malformed slack-log line(s).")
        feedback.pushInfo(f"Parsed {len(rows)} slack-log record(s).")
        return rows, fields
