# import_3d_model_solutions_algorithm.py
# -*- coding: utf-8 -*-
"""
Import3DModelSolutionsAlgorithm
Import a 3D model solutions CSV/TSV as a point layer.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from qgis.core import (
    QgsField,
    QgsFields,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsWkbTypes,
)

from . import cable_lay_parsers as clp
from .cable_lay_import_base import CableLayImportAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING

_TEXT_COLUMNS = (
    "Time", "ISO_Time", "ISO Time",
    "Ship Latitude", "Ship Longitude", "TD Latitude", "TD Longitude",
)


class Import3DModelSolutionsAlgorithm(CableLayImportAlgorithm):
    GEOM_SOURCE = "GEOM_SOURCE"

    LAYER_TYPE = "model_solutions"
    OUTPUT_WKB = QgsWkbTypes.Point
    INPUT_LABEL = "3D Model Solutions File(s)"
    NEEDS_START_DATE = False  # added manually below so it can stay optional

    _GEOM_CHOICES = ["Touchdown (TD) position", "Ship position"]

    def createInstance(self):
        return Import3DModelSolutionsAlgorithm()

    def name(self):
        return "import_3d_model_solutions"

    def displayName(self):
        return self.tr("Import 3D Model Solutions")

    def add_extra_parameters(self):
        self.addParameter(
            QgsProcessingParameterString(
                self.START_DATE,
                self.tr("Project Start Date (YYYY-MM-DD) - needed only for day-count times"),
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.GEOM_SOURCE,
                self.tr("Geometry from"),
                options=self._GEOM_CHOICES,
                defaultValue=0,
            )
        )

    def shortHelpString(self):
        return self.tr(
            """
<h3>Import 3D Model Solutions</h3>
<p>Imports a 3D model solutions export (CSV or TSV - the delimiter is detected
automatically) as a <b>point</b> layer. Points can be placed at either the
touchdown (TD) position or the ship position.</p>

<h4>Times and coordinates</h4>
<p>The tool accepts a <code>Time</code> column in <code>day,HH:MM:SS</code>
format (supply the <b>Project Start Date</b> to convert it) or an existing
<code>ISO_Time</code> column. Coordinates may be given as
<code>Ship Latitude</code>/<code>Ship Longitude</code> (and optional
<code>TD Latitude</code>/<code>TD Longitude</code>) in degrees and decimal
minutes, or as ready-made <code>Lat_dd</code>/<code>Lon_dd</code> columns.</p>

<h4>Building up a layer</h4>
<p>Select one or more files. For the destination, either pick an <b>Existing
layer to add to</b> (a pre-created <code>model_solutions</code> layer, e.g. from
<i>Create Cable Lay GeoPackage</i>) or choose a <b>Target GeoPackage</b> to
create/append to (the <code>model_solutions</code> layer is created if missing).
Running again grows that layer; duplicates (same file and timestamp) are dropped.</p>

<h4>Output</h4>
<p>A point layer named <code>model_solutions</code> (prefixed with the GeoPackage
file name, e.g. <code>ProjectX_model_solutions</code>) in WGS 84 (EPSG:4326) with
<code>ISO_Time</code>, the original columns, decimal-degree coordinate fields and
a <code>source_file</code> column.</p>
"""
        )

    # --------------------------------------------------------------------- parse
    def parse_rows(self, path, parameters, context, feedback) -> Tuple[List[Dict], QgsFields]:
        start_date = self.parameterAsString(parameters, self.START_DATE, context).strip()
        geom_source = self.parameterAsEnum(parameters, self.GEOM_SOURCE, context)  # 0=TD, 1=Ship
        source_name = os.path.basename(path)

        rows, _ = clp.read_csv_rows(path)
        nonblank = clp.non_blank_rows(rows)
        if len(nonblank) < 2:
            raise self._error("3D model solutions file has no data rows.")
        headers = [clp.normalize_column_name(c) for c in nonblank[0]]
        records: List[Dict[str, str]] = [dict(zip(headers, row)) for row in nonblank[1:]]

        def lookup(name: str) -> Optional[str]:
            wanted = name.casefold()
            return next((h for h in headers if h.casefold() == wanted), None)

        time_col = lookup("Time")
        iso_col = lookup("ISO_Time") or lookup("ISO Time")
        use_daytime = time_col is not None
        if not use_daytime and iso_col is None:
            # Fallback: treat the first column as day-time if most values match.
            candidate = headers[0] if headers else None
            sample = [
                record.get(candidate, "")
                for record in records[:50]
                if str(record.get(candidate, "")).strip()
            ]
            if sample and sum(clp.looks_like_day_time(v) for v in sample) / len(sample) >= 0.6:
                time_col = candidate
                use_daytime = True
            else:
                raise self._error("3D model solutions file missing a 'Time' or 'ISO_Time' column.")
        if use_daytime and not start_date:
            raise self._error(
                "This file uses day-count times; please supply the Project Start Date."
            )

        ship_lat = lookup("Ship Latitude")
        ship_lon = lookup("Ship Longitude")
        ship_lat_dd_col = lookup("Lat_dd") or lookup("Lat DD")
        ship_lon_dd_col = lookup("Lon_dd") or lookup("Lon DD")
        td_lat = lookup("TD Latitude")
        td_lon = lookup("TD Longitude")
        td_lat_dd_col = lookup("TD_Lat_dd") or lookup("TD Lat_dd")
        td_lon_dd_col = lookup("TD_Lon_dd") or lookup("TD Lon_dd")

        has_ship = bool((ship_lat and ship_lon) or (ship_lat_dd_col and ship_lon_dd_col))
        has_td = bool((td_lat and td_lon) or (td_lat_dd_col and td_lon_dd_col))
        if not has_ship and not has_td:
            raise self._error(
                "3D model solutions file has no usable coordinate columns "
                "(need Ship/ TD latitude & longitude, or Lat_dd/Lon_dd)."
            )

        col_types = clp.infer_column_types(records, text_columns=_TEXT_COLUMNS)

        fields = QgsFields()
        fields.append(QgsField("ISO_Time", FIELD_TYPE_STRING))
        for col in headers:
            fields.append(clp.qgis_field_for(col_types.get(col, "str"), col))
        if has_ship:
            fields.append(QgsField("Lat_dd", FIELD_TYPE_DOUBLE))
            fields.append(QgsField("Lon_dd", FIELD_TYPE_DOUBLE))
        if has_td:
            fields.append(QgsField("TD_Lat_dd", FIELD_TYPE_DOUBLE))
            fields.append(QgsField("TD_Lon_dd", FIELD_TYPE_DOUBLE))
        fields.append(QgsField("source_file", FIELD_TYPE_STRING))

        use_td_geom = (geom_source == 0 and has_td) or (geom_source == 1 and not has_ship)

        out_rows: List[Dict] = []
        skipped = 0
        for record in records:
            if use_daytime:
                iso_dt = clp.parse_day_time(record.get(time_col, ""), start_date)
                if iso_dt is None:
                    skipped += 1
                    continue
                iso_val = clp.iso_str(iso_dt)
            else:
                iso_val = str(record.get(iso_col, "")).strip()
                if not iso_val:
                    skipped += 1
                    continue

            row: Dict = {"ISO_Time": iso_val}
            for col in headers:
                row[col] = clp.coerce_value(record.get(col, ""), col_types.get(col, "str"))

            ship_lat_dd = ship_lon_dd = td_lat_dd = td_lon_dd = None
            if has_ship:
                if ship_lat and ship_lon:
                    ship_lat_dd = clp.parse_dms_to_dd(record.get(ship_lat, ""))
                    ship_lon_dd = clp.parse_dms_to_dd(record.get(ship_lon, ""))
                else:
                    ship_lat_dd = clp.coerce_value(record.get(ship_lat_dd_col, ""), "float")
                    ship_lon_dd = clp.coerce_value(record.get(ship_lon_dd_col, ""), "float")
                row["Lat_dd"] = ship_lat_dd
                row["Lon_dd"] = ship_lon_dd
            if has_td:
                if td_lat and td_lon:
                    td_lat_dd = clp.parse_dms_to_dd(record.get(td_lat, ""))
                    td_lon_dd = clp.parse_dms_to_dd(record.get(td_lon, ""))
                else:
                    td_lat_dd = clp.coerce_value(record.get(td_lat_dd_col, ""), "float")
                    td_lon_dd = clp.coerce_value(record.get(td_lon_dd_col, ""), "float")
                row["TD_Lat_dd"] = td_lat_dd
                row["TD_Lon_dd"] = td_lon_dd

            if use_td_geom:
                lat_dd, lon_dd = td_lat_dd, td_lon_dd
            else:
                lat_dd, lon_dd = ship_lat_dd, ship_lon_dd
            row["source_file"] = source_name
            row[clp.WKT_KEY] = f"POINT ({lon_dd} {lat_dd})" if None not in (lat_dd, lon_dd) else None
            out_rows.append(row)

        if skipped:
            feedback.pushInfo(f"Skipped {skipped} row(s) with no usable time value.")
        feedback.pushInfo(f"Parsed {len(out_rows)} model-solution record(s).")
        return out_rows, fields

    def _error(self, message: str):
        from qgis.core import QgsProcessingException

        return QgsProcessingException(self.tr(message))
