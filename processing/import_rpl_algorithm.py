# -*- coding: utf-8 -*-
"""ImportRPLAlgorithm — batch/headless RPL import into the Workbench.

Runs the same pure import core as the guided "Import RPL..." wizard
(auto-detection, parsing, validation) and the same rollback-safe commit
service, so scripted/batch imports and interactive imports cannot drift.

Detection is automatic; a saved import-profile JSON (exported by the wizard
or a previous run of this algorithm) can be supplied for fully repeatable
mappings. Errors block the import; warnings are pushed to the Processing log
and accepted only when "Accept warnings" is checked.

The legacy ``importexcelrpl`` algorithm is kept for backwards compatibility
with existing models/scripts, but this algorithm plus the wizard are the
supported import path.
"""

from __future__ import annotations

import json
import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFile,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterString,
    QgsProject,
    QgsVectorLayer,
)

from ..rpl_import import model as im
from ..rpl_import import detect as idetect
from ..rpl_import import parser as iparser
from ..rpl_import import reader as ireader
from ..rpl_import import validate as ivalidate
from ..rpl_import.model import ImportProfile
from ..workbench.rpl_import_service import (
    CommitError, CommitRequest, commit_import, geodesy_fns,
    make_wgs84_distance_area, measurement_config, reconcile_model,
    to_rpl_model, transform_projected,
)
from ..workbench.store import (
    WorkbenchStore, default_project_gpkg_path, project_gpkg_path,
    set_project_gpkg_path,
)

WORKBENCH_GROUP = "Cable Route Workbench"


class ImportRPLAlgorithm(QgsProcessingAlgorithm):
    INPUT_FILE = "INPUT_FILE"
    SHEET = "SHEET"
    PROFILE_JSON = "PROFILE_JSON"
    ROUTE_NAME = "ROUTE_NAME"
    REV_LABEL = "REV_LABEL"
    RPL_KIND = "RPL_KIND"
    ACCEPT_WARNINGS = "ACCEPT_WARNINGS"
    GPKG_PATH = "GPKG_PATH"
    LOAD_LAYERS = "LOAD_LAYERS"

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT_FILE, self.tr("RPL file (.xlsx / .xlsm / .csv)"),
            fileFilter="RPL files (*.xlsx *.xlsm *.csv);;All files (*)"))
        self.addParameter(QgsProcessingParameterString(
            self.SHEET, self.tr("Worksheet (blank = best detected)"),
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterFile(
            self.PROFILE_JSON,
            self.tr("Import profile JSON (blank = auto-detect)"),
            optional=True, fileFilter="JSON (*.json);;All files (*)"))
        self.addParameter(QgsProcessingParameterString(
            self.ROUTE_NAME,
            self.tr("Segment name (blank = file name)"),
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.REV_LABEL, self.tr("Revision label (blank = next Rev N)"),
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.RPL_KIND, self.tr("RPL kind"),
            options=[self.tr("Planned"), self.tr("As-laid")], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ACCEPT_WARNINGS,
            self.tr("Accept warnings (they are still logged and audited)"),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.GPKG_PATH,
            self.tr("Workbench GeoPackage (blank = project default)"),
            fileFilter="GeoPackage (*.gpkg)", optional=True,
            createByDefault=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.LOAD_LAYERS,
            self.tr("Load registered layers into the project"),
            defaultValue=True))

    def processAlgorithm(self, parameters, context, feedback):
        path = self.parameterAsFile(parameters, self.INPUT_FILE, context)
        sheet = (self.parameterAsString(parameters, self.SHEET, context) or "").strip()
        profile_path = (self.parameterAsString(
            parameters, self.PROFILE_JSON, context) or "").strip()
        accept_warnings = self.parameterAsBool(
            parameters, self.ACCEPT_WARNINGS, context)
        kind = ["planned", "as_laid"][
            self.parameterAsEnum(parameters, self.RPL_KIND, context)]
        route_name = (self.parameterAsString(
            parameters, self.ROUTE_NAME, context) or "").strip()
        rev_label = (self.parameterAsString(
            parameters, self.REV_LABEL, context) or "").strip()

        # -- profile: supplied or detected ---------------------------------
        if profile_path:
            try:
                with open(profile_path, "r", encoding="utf-8") as handle:
                    profile = ImportProfile.from_json(handle.read())
            except (OSError, ValueError) as exc:
                raise QgsProcessingException(
                    self.tr(f"Could not read profile JSON: {exc}"))
            detection_note = f"profile from {os.path.basename(profile_path)}"
            auto_detected = False
        else:
            try:
                results = idetect.score_sheets(ireader.load_sample_grids(path))
            except ireader.ReaderError as exc:
                raise QgsProcessingException(str(exc))
            if sheet:
                result = next((r for r in results
                               if r.profile.sheet == sheet), None)
                if result is None:
                    raise QgsProcessingException(self.tr(
                        f"Sheet '{sheet}' not found. Available: "
                        f"{[r.profile.sheet for r in results]}"))
            else:
                result = results[0] if results else None
            if result is None or result.position_count < 2:
                raise QgsProcessingException(self.tr(
                    "No RPL-like worksheet could be detected. Use the "
                    "Import RPL wizard to inspect the file."))
            profile = result.profile
            detection_note = "auto-detected"
            auto_detected = True
            for topic, reason in sorted(result.reasons.items()):
                feedback.pushInfo(f"[detect] {topic}: {reason}")

        # -- read + parse ---------------------------------------------------
        try:
            grid = ireader.load_grid(
                path, sheet=profile.sheet if ireader.is_excel(path) else None)
        except ireader.ReaderError as exc:
            raise QgsProcessingException(str(exc))
        if auto_detected:
            result = idetect.detect(grid)
            profile = result.profile
            feedback.pushInfo(
                f"[detect] full data range: rows {profile.data_start_row}-"
                f"{profile.data_end_row} ({result.position_count} positions)")
        elif not profile.data_end_row:
            profile.data_end_row = grid.n_rows
        doc, parse_diags = iparser.parse(grid, profile)

        convert_diags = []
        if profile.coord_encoding == im.COORD_PROJECTED:
            convert_diags = transform_projected(
                doc, profile, context.transformContext())

        da = make_wgs84_distance_area(context.transformContext())
        dist_fn, bear_fn = geodesy_fns(da)
        validate_diags = ivalidate.validate(doc, dist_fn, bear_fn)

        all_diags = parse_diags + convert_diags + validate_diags
        errors, warnings, infos = im.split_diagnostics(all_diags)
        for diag in warnings:
            feedback.pushWarning(self._diag_text(diag))
        for diag in infos:
            feedback.pushInfo(self._diag_text(diag))
        if errors:
            raise QgsProcessingException(self.tr(
                "Import blocked by %d error(s):\n%s" % (
                    len(errors),
                    "\n".join(self._diag_text(d) for d in errors[:20]))))
        if warnings and not accept_warnings:
            raise QgsProcessingException(self.tr(
                "%d warning(s) present and 'Accept warnings' is unchecked."
                % len(warnings)))

        # -- build + commit -------------------------------------------------
        model, conv = to_rpl_model(doc, source_file=os.path.basename(path))
        if im.has_errors(conv):
            raise QgsProcessingException(self.tr(
                "\n".join(d.message for d in conv
                          if d.severity == im.SEVERITY_ERROR)))
        report = reconcile_model(model, da, derive_missing=True)

        gpkg_path = (self.parameterAsString(
            parameters, self.GPKG_PATH, context) or "").strip()
        if not gpkg_path or gpkg_path.lower() in ("temporary_output",
                                                  "temporary output"):
            gpkg_path = (project_gpkg_path(context.project())
                         or default_project_gpkg_path(context.project()))

        try:
            fingerprint = ireader.file_fingerprint(path)
        except Exception:
            fingerprint = {"path": path, "filename": os.path.basename(path)}
        audit = {
            "source": fingerprint,
            "sheet": profile.sheet,
            "data_rows": [profile.data_start_row, profile.data_end_row],
            "profile": json.loads(profile.to_json()),
            "parser_version": im.PARSER_VERSION,
            "measurement": measurement_config(da),
            "accepted_warnings": [d.to_dict() for d in warnings],
            "information": [d.to_dict() for d in infos],
            "derivation": report.to_dict(),
            "entry_point": f"processing:import_rpl ({detection_note})",
        }

        store = WorkbenchStore(gpkg_path, context.transformContext())
        request = CommitRequest(
            route_name=route_name or os.path.splitext(
                os.path.basename(path))[0],
            kind=kind, rev_label=rev_label,
            source_file=os.path.basename(path), audit=audit)
        try:
            result = commit_import(store, model, request)
        except CommitError as exc:
            raise QgsProcessingException(str(exc))

        feedback.pushInfo(self.tr(
            f"Registered '{result.registered_name}' "
            f"({len(model.points)} positions, {len(model.segments)} "
            f"segments) into {os.path.basename(result.gpkg_path)}."))

        self._gpkg_path = result.gpkg_path
        self._layer_names = [result.points_layer, result.lines_layer]
        self._load_layers = self.parameterAsBool(
            parameters, self.LOAD_LAYERS, context)
        return {
            "RPL_ID": result.rpl_id,
            "ROUTE_ID": result.route_id,
            "REV_LABEL": result.rev_label,
            "GPKG_PATH": result.gpkg_path,
            "POINTS_LAYER": result.points_layer,
            "LINES_LAYER": result.lines_layer,
            "PROFILE_JSON": profile.to_json(),
        }

    @staticmethod
    def _diag_text(diag) -> str:
        where = f" (row {diag.row})" if diag.row else ""
        return f"[{diag.rule_id}]{where} {diag.message}"

    def postProcessAlgorithm(self, context, feedback):
        if not getattr(self, "_load_layers", False):
            return {}
        project = context.project() or QgsProject.instance()
        set_project_gpkg_path(self._gpkg_path, project)
        root = project.layerTreeRoot()
        group = (root.findGroup(WORKBENCH_GROUP)
                 or root.insertGroup(0, WORKBENCH_GROUP))
        from .cable_lay_parsers import gpkg_layer_uri

        for layer_name in getattr(self, "_layer_names", []):
            layer = QgsVectorLayer(
                gpkg_layer_uri(self._gpkg_path, layer_name), layer_name, "ogr")
            if layer.isValid():
                project.addMapLayer(layer, False)
                group.addLayer(layer)
        return {}

    def name(self):
        return "import_rpl"

    def displayName(self):
        return self.tr("Import RPL to Workbench (auto-detect)")

    def group(self):
        return self.tr("RPL Tools")

    def groupId(self):
        return "rpl_tools"

    def shortHelpString(self):
        return self.tr(
            """
Imports an RPL workbook/CSV straight into the Cable Route Workbench in one step, using the same detection, parsing, validation, and rollback-safe registration as the guided "Import RPL..." wizard.

- Scans worksheets and auto-detects the layout (alternating or flat rows), coordinate encoding (split degrees/minutes/hemisphere, combined DDM text, decimal degrees, or projected easting/northing with a stated CRS), units, and column mapping.
- Alternatively accepts an import-profile JSON for fully repeatable batch runs; the confirmed profile is also returned as the PROFILE_JSON output.
- Stated engineering values (KP, distances, bearing, slack, cable distances) are imported verbatim; only missing values are derived from geometry, and everything derived is recorded in the import audit.
- Errors block the import. Warnings are logged and, when accepted, recorded in the audit stored inside the workbench GeoPackage.
- On failure nothing is left behind: spatial layers, registry rows, and audit entries from the failed attempt are removed.

Use the "Import RPL..." button in the Cable Route Workbench for the interactive, correctable version of this workflow.
"""
        )

    def tr(self, string):
        return QCoreApplication.translate("ImportRPLAlgorithm", string)

    def createInstance(self):
        return ImportRPLAlgorithm()
