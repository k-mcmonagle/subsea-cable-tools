# -*- coding: utf-8 -*-
"""Import AGS CPT Data — AGS 3 / AGS 4 cone penetration tests to layers.

Reads one or more AGS files (any extension) with the plugin's own reader
(``geotech``: STCN, SCPT/SCPG and the AGS 4.2 CPTT/CPTG groups), optionally
joins a position / summary table by CPT name, and writes:

- a point layer of CPT locations (WGS84) with water depth, penetration,
  readings, maximum qc, summary and warnings;
- a table of readings with the measured channels in m / MPa / kPa and the
  derived parameters (qt, Rf, qnet, Bq, Qtn, Fr, Ic, SBTn zone, Su, Dr);
- a table of strata (horizons from the position table, else AGS GEOL).

The Burial Planner's Ground Model ▸ CPTs imports the same files into the
plan GeoPackage instead; this algorithm is for use anywhere else in QGIS.
"""

from __future__ import annotations

import os

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingException,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
)
from qgis.PyQt.QtCore import QCoreApplication

from ..geotech import cpt as cpt_mod
from ..qgis_compat import (
    FIELD_TYPE_DOUBLE,
    FIELD_TYPE_INT,
    FIELD_TYPE_STRING,
    PROCESSING_NUMBER_DOUBLE,
    PROCESSING_SOURCE_FILE,
    WKB_NO_GEOMETRY,
    WKB_POINT,
)
from .algorithm_base import SubseaCableAlgorithm

_NAME_RULES = [cpt_mod.NAME_AUTO, cpt_mod.NAME_FILE, cpt_mod.NAME_LOCATION, cpt_mod.NAME_FILE_LOCATION]
_PORE = ["u2", "u1", "u3"]
_COLUMN_PARAMS = (
    ("ID_COLUMN", "id", "CPT name column of the table"),
    ("LAT_COLUMN", "lat", "Latitude column (decimal or degrees-minutes)"),
    ("LON_COLUMN", "lon", "Longitude column (decimal or degrees-minutes)"),
    ("X_COLUMN", "x", "Easting / X column (instead of latitude/longitude)"),
    ("Y_COLUMN", "y", "Northing / Y column"),
    ("WATER_DEPTH_COLUMN", "water_depth", "Water depth column"),
    ("PENETRATION_COLUMN", "penetration", "Penetration column"),
    ("DATE_COLUMN", "date", "Date column"),
    ("SUMMARY_COLUMN", "summary", "Summary column"),
    ("HORIZON_DESC_COLUMN", "horizon_desc", "First horizon description column (…1)"),
    ("HORIZON_CLASS_COLUMN", "horizon_class", "First horizon class column (…1)"),
    ("INTERFACE_COLUMN", "horizon_interface", "First interface depth column (…1)"),
)
_UNIT_SUFFIX = {"MPa": "mpa", "kPa": "kpa", "%": "pct", "°": "deg", "°C": "degc", "kN/m³": "kn_m3"}
_READING_KEYS = ("qc", "fs", "u1", "u2", "u3", "incl", "incl2", "temp", "cond", "delivered_qt",
                 "delivered_rf", "delivered_bq", "delivered_qnet", "delivered_u0", "qt", "rf", "u0",
                 "sv0", "sv0_eff", "qnet", "bq", "fr", "qtn", "ic", "zone", "su", "su_low", "dr", "gamma")


def _set_advanced(parameter) -> None:
    """The Advanced flag on QGIS 3.36+/4 (Qgis.ProcessingParameterFlag) and older 3.x."""
    try:
        from qgis.core import Qgis
        flag = Qgis.ProcessingParameterFlag.Advanced
    except AttributeError:
        flag = QgsProcessingParameterDefinition.FlagAdvanced
    parameter.setFlags(parameter.flags() | flag)


def reading_field(key: str) -> str:
    """``qc_mpa``, ``fs_kpa``, ``rf_pct``, ``zone`` → ``sbtn_zone`` …"""
    if key == "zone":
        return "sbtn_zone"
    suffix = _UNIT_SUFFIX.get(cpt_mod.channel_unit(key), "")
    return f"{key}_{suffix}" if suffix else key


def header_row(grid, name: str) -> int:
    """1-based row of the first row holding the column ``name``."""
    for index, row in enumerate(grid[:60], start=1):
        if any(str(cell).strip() == name for cell in row):
            return index
    raise QgsProcessingException(
        f"No row of the table has a column named '{name}'. The first row reads: "
        + ", ".join(str(c) for c in (grid[0] if grid else [])[:12]))


class ImportAgsCptAlgorithm(SubseaCableAlgorithm):
    INPUT = "INPUT"
    NAME_RULE = "NAME_RULE"
    AGS3_PORE = "AGS3_PORE"
    GRID_CRS = "GRID_CRS"
    POSITIONS = "POSITIONS"
    SHEET = "SHEET"
    TABLE_CRS = "TABLE_CRS"
    AREA_RATIO = "AREA_RATIO"
    UNIT_WEIGHT = "UNIT_WEIGHT"
    NKT = "NKT"
    NKT_HIGH = "NKT_HIGH"
    OUTPUT_LOCATIONS = "OUTPUT_LOCATIONS"
    OUTPUT_READINGS = "OUTPUT_READINGS"
    OUTPUT_STRATA = "OUTPUT_STRATA"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT, self.tr("AGS file(s) (AGS 3.x or 4.x, any extension)"),
            layerType=PROCESSING_SOURCE_FILE))
        self.addParameter(QgsProcessingParameterEnum(
            self.NAME_RULE, self.tr("Name each CPT by"),
            options=[self.tr("Automatic (file name when each file holds one CPT)"),
                     self.tr("File name"), self.tr("AGS location ID"),
                     self.tr("File name + location ID")], defaultValue=0))
        self.addParameter(QgsProcessingParameterFile(
            self.POSITIONS, self.tr("Position / summary table (optional, CSV or XLSX)"),
            optional=True, fileFilter="Tables (*.csv *.tsv *.txt *.xlsx *.xlsm)"))
        for name, _role, label in _COLUMN_PARAMS:
            self.addParameter(QgsProcessingParameterString(name, self.tr(label), optional=True))
        self.addParameter(QgsProcessingParameterCrs(
            self.TABLE_CRS, self.tr("CRS of the table's easting / northing"), optional=True))
        advanced = []
        advanced.append(QgsProcessingParameterString(
            self.SHEET, self.tr("Worksheet (XLSX; blank = first)"), optional=True))
        advanced.append(QgsProcessingParameterCrs(
            self.GRID_CRS, self.tr("CRS of AGS easting / northing when the file gives none"),
            optional=True))
        advanced.append(QgsProcessingParameterEnum(
            self.AGS3_PORE, self.tr("AGS 3 pore pressure STCN_PWP1 is"),
            options=[self.tr("u2 (shoulder, the usual piezocone position)"), self.tr("u1 (face)"),
                     self.tr("u3 (behind the sleeve)")], defaultValue=0))
        for name, label, value, low, high in (
                (self.AREA_RATIO, "Cone area ratio a (when the file gives none)", 0.75, 0.01, 1.0),
                (self.UNIT_WEIGHT, "Soil unit weight (kN/m³)", 18.0, 10.0, 25.0),
                (self.NKT, "Nkt for Su", 15.0, 1.0, 50.0),
                (self.NKT_HIGH, "Nkt for the lower Su estimate", 20.0, 1.0, 50.0)):
            advanced.append(QgsProcessingParameterNumber(
                name, self.tr(label), type=PROCESSING_NUMBER_DOUBLE, defaultValue=value,
                minValue=low, maxValue=high))
        for parameter in advanced:
            _set_advanced(parameter)
            self.addParameter(parameter)
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_LOCATIONS, self.tr("CPT locations")))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_READINGS, self.tr("CPT readings"), optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_STRATA, self.tr("CPT strata"), optional=True))

    # -- processing -------------------------------------------------------------------
    def _config(self, parameters, context):
        paths = [str(p).strip() for p in (self.parameterAsFileList(parameters, self.INPUT, context) or [])
                 if str(p).strip()]
        if not paths:
            raise QgsProcessingException(self.tr("Choose at least one AGS file."))
        config = {"files": [{"path": p} for p in paths],
                  "name_rule": _NAME_RULES[self.parameterAsEnum(parameters, self.NAME_RULE, context)],
                  "ags3_pore": _PORE[self.parameterAsEnum(parameters, self.AGS3_PORE, context)],
                  "positions": {}}
        grid_crs = self.parameterAsCrs(parameters, self.GRID_CRS, context)
        if grid_crs.isValid():
            config["grid_crs"] = grid_crs.authid() or grid_crs.toWkt()
        table = self.parameterAsFile(parameters, self.POSITIONS, context)
        columns = {role: self.parameterAsString(parameters, name, context).strip()
                   for name, role, _label in _COLUMN_PARAMS}
        columns = {role: value for role, value in columns.items() if value}
        grid = None
        if table:
            if not os.path.exists(table):
                raise QgsProcessingException(self.tr("The position table does not exist: ") + table)
            if not columns.get("id"):
                raise QgsProcessingException(self.tr("Give the CPT name column of the position table."))
            from ..burial.cpt_register import read_table_grid
            sheet = self.parameterAsString(parameters, self.SHEET, context).strip()
            try:
                grid, _sheets = read_table_grid(table, sheet or None)
            except Exception as exc:  # noqa: BLE001 - damaged workbooks raise zipfile/openpyxl errors
                raise QgsProcessingException(f"The position table cannot be read: {exc}") from exc
            table_crs = self.parameterAsCrs(parameters, self.TABLE_CRS, context)
            grid_mode = "x" in columns and "lat" not in columns
            config["positions"] = {"path": table, "sheet": sheet, "header_row": header_row(grid, columns["id"]),
                                   "columns": columns, "coords": "grid" if grid_mode else "latlon",
                                   "crs": (table_crs.authid() or table_crs.toWkt()) if table_crs.isValid() else "",
                                   "loose": True}
        return config, grid

    def processAlgorithm(self, parameters, context, feedback):
        from ..burial import cpt_register
        config, grid = self._config(parameters, context)
        tests, reports = cpt_register.read_files([f["path"] for f in config["files"]], config["ags3_pore"])
        for report in reports:
            if report["error"]:
                feedback.reportError(f"{report['file']}: {report['error']}", False)
            else:
                feedback.pushInfo(f"{report['file']}: {report['version']}, groups {report['groups']}, "
                                  f"{report['tests']} CPT(s), {report['readings']} readings")
        if not tests:
            raise QgsProcessingException(self.tr("No CPT readings were found in the chosen files."))
        try:
            built = cpt_register.build(config, tests=tests, grid=grid, campaign_id="processing")
        except ValueError as exc:
            raise QgsProcessingException(str(exc)) from exc
        for note in built["notes"]:
            feedback.pushWarning(note)
        settings = cpt_mod.settings({
            "area_ratio": self.parameterAsDouble(parameters, self.AREA_RATIO, context),
            "unit_weight": self.parameterAsDouble(parameters, self.UNIT_WEIGHT, context),
            "nkt": self.parameterAsDouble(parameters, self.NKT, context),
            "nkt_high": self.parameterAsDouble(parameters, self.NKT_HIGH, context)})
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

        location_fields = QgsFields()
        for name, kind in (("name", FIELD_TYPE_STRING), ("location_id", FIELD_TYPE_STRING),
                           ("test_ref", FIELD_TYPE_STRING), ("source_file", FIELD_TYPE_STRING),
                           ("ags_version", FIELD_TYPE_STRING), ("lat", FIELD_TYPE_DOUBLE),
                           ("lon", FIELD_TYPE_DOUBLE), ("position_source", FIELD_TYPE_STRING),
                           ("water_depth_m", FIELD_TYPE_DOUBLE), ("final_depth_m", FIELD_TYPE_DOUBLE),
                           ("max_depth_m", FIELD_TYPE_DOUBLE), ("readings", FIELD_TYPE_INT),
                           ("max_qc_mpa", FIELD_TYPE_DOUBLE), ("tested", FIELD_TYPE_STRING),
                           ("summary", FIELD_TYPE_STRING), ("termination", FIELD_TYPE_STRING),
                           ("warnings", FIELD_TYPE_STRING)):
            location_fields.append(QgsField(name, kind))
        (locations, locations_id) = self.parameterAsSink(parameters, self.OUTPUT_LOCATIONS, context,
                                                         location_fields, WKB_POINT, wgs84)
        if locations is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_LOCATIONS))
        derived = {}
        present = []
        for cpt in built["cpts"]:
            data = built["readings"][cpt["cpt_id"]]
            meta = cpt.get("meta") or {}
            test = {"depth": data["depth"], "channels": data["channels"], "area_ratio": meta.get("area_ratio"),
                    "zero_reference": meta.get("zero_reference") or ""}
            channels = cpt_mod.all_channels(test, settings, cpt.get("water_depth_m"))
            derived[cpt["cpt_id"]] = channels
            for key in _READING_KEYS:
                if key not in present and any(v is not None for v in channels.get(key) or []):
                    present.append(key)
        present.sort(key=_READING_KEYS.index)

        readings_fields = QgsFields()
        readings_fields.append(QgsField("cpt", FIELD_TYPE_STRING))
        readings_fields.append(QgsField("depth_m", FIELD_TYPE_DOUBLE))
        for key in present:
            readings_fields.append(QgsField(reading_field(key), FIELD_TYPE_INT if key == "zone" else FIELD_TYPE_DOUBLE))
        (readings, readings_id) = self.parameterAsSink(parameters, self.OUTPUT_READINGS, context,
                                                       readings_fields, WKB_NO_GEOMETRY, wgs84)
        strata_fields = QgsFields()
        for name, kind in (("cpt", FIELD_TYPE_STRING), ("top_m", FIELD_TYPE_DOUBLE),
                           ("base_m", FIELD_TYPE_DOUBLE), ("description", FIELD_TYPE_STRING),
                           ("class", FIELD_TYPE_STRING), ("source", FIELD_TYPE_STRING)):
            strata_fields.append(QgsField(name, kind))
        (strata, strata_id) = self.parameterAsSink(parameters, self.OUTPUT_STRATA, context,
                                                   strata_fields, WKB_NO_GEOMETRY, wgs84)
        total = max(1, len(built["cpts"]))
        for n, cpt in enumerate(built["cpts"]):
            if feedback.isCanceled():
                break
            meta = cpt.get("meta") or {}
            feature = QgsFeature(location_fields)
            if cpt.get("lat") is not None:
                feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(cpt["lon"], cpt["lat"])))
            feature.setAttributes([
                cpt["name"], cpt["location_id"], cpt["test_ref"], cpt["source_file"], cpt["ags_version"],
                cpt["lat"], cpt["lon"], cpt["position_source"], cpt["water_depth_m"], cpt["final_depth_m"],
                cpt["max_depth_m"], cpt["readings"], cpt["max_qc_mpa"], cpt["tested"], cpt["summary"],
                cpt.get("termination") or "", "; ".join(meta.get("warnings") or [])[:1000]])
            locations.addFeature(feature, QgsFeatureSink.FastInsert)
            if readings is not None:
                channels = derived[cpt["cpt_id"]]
                rows = []
                for i, depth in enumerate(built["readings"][cpt["cpt_id"]]["depth"]):
                    row = QgsFeature(readings_fields)
                    values = [cpt["name"], depth]
                    for key in present:
                        series = channels.get(key) or []
                        value = series[i] if i < len(series) else None
                        values.append(int(value) if key == "zone" and value is not None else value)
                    row.setAttributes(values)
                    rows.append(row)
                readings.addFeatures(rows, QgsFeatureSink.FastInsert)
            if strata is not None:
                for layer in cpt.get("layers") or []:
                    row = QgsFeature(strata_fields)
                    row.setAttributes([cpt["name"], layer.get("top"), layer.get("base"),
                                       layer.get("description") or "", layer.get("soil_class") or "",
                                       meta.get("layer_source") or ""])
                    strata.addFeature(row, QgsFeatureSink.FastInsert)
            feedback.setProgress(100.0 * (n + 1) / total)
        feedback.pushInfo(f"{len(built['cpts'])} CPT(s); "
                          f"{sum(1 for c in built['cpts'] if c['lat'] is not None)} with a position.")
        out = {self.OUTPUT_LOCATIONS: locations_id}
        if readings is not None:
            out[self.OUTPUT_READINGS] = readings_id
        if strata is not None:
            out[self.OUTPUT_STRATA] = strata_id
        return out

    # -- metadata --------------------------------------------------------------------
    def name(self):
        return "import_ags_cpt"

    def displayName(self):
        return self.tr("Import AGS CPT Data")

    def group(self):
        return self.tr("Geotechnical")

    def groupId(self):
        return "geotechnical"

    def shortHelpString(self):
        return self.tr(
            "Reads cone penetration tests from AGS 3.x (STCN) and AGS 4.x (SCPT/SCPG; AGS 4.2 CPTT/CPTG) "
            "files of any extension, and writes CPT locations, readings and strata.\n\n"
            "<b>Positions</b> come from the optional position / summary table when given (joined by CPT "
            "name; latitude/longitude as decimal degrees or degrees and minutes such as 52° 20.220' N, or "
            "easting/northing with their CRS), else from the AGS LOCA / HOLE group. The table's header row "
            "is the first row containing the CPT name column. Numbered horizon columns (…1, …2) give the "
            "strata: horizon k runs from interface k-1 to interface k.\n\n"
            "<b>Readings</b> are converted to m, MPa (qc, qt) and kPa (fs, pore pressure). Derived values "
            "follow Robertson & Cabal (2022): qt = qc + u2(1 − a), Rf, qnet, Bq, Qtn and Fr with the "
            "iterated stress exponent, Ic, SBTn zones 2–7, Su = qnet/Nkt (not where Ic < 2.6) and "
            "Dr = √(Qtn/350) (Ic < 2.6), with readings zeroed at the seabed unless the file says the zero "
            "was taken on deck. They are screening values — check them against the contractor's "
            "interpretation.\n\n"
            "AGS 3 does not record where its pore pressure was measured; STCN_PWP1 is read as u2 unless "
            "you choose otherwise.")

    def tr(self, string):
        return QCoreApplication.translate("ImportAgsCptAlgorithm", string)

    def createInstance(self):
        return ImportAgsCptAlgorithm()
