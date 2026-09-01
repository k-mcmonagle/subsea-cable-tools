# cable_lay_import_base.py
# -*- coding: utf-8 -*-
"""
Base class shared by the Cable Lay Data Import algorithms.

Each concrete importer only declares its file type and a ``parse_rows`` method
that turns a single input file into rows (see :mod:`cable_lay_parsers`). The base
handles the common flow:

* accept **multiple input files** at once and parse them all,
* resolve the destination - either an **existing layer picked from a dropdown**
  (typically one created by *Create Cable Lay GeoPackage*) or a **GeoPackage file**
  to create/append to,
* read the destination layer's existing rows if present,
* merge everything and **de-duplicate** on a per-type key,
* write the layer back into the GeoPackage (creating the file/layer if needed,
  preserving other layers), and refresh/load it in the project.

Because each importer always targets a fixed canonical layer (``cable_lay``,
``slack_logs`` ...), running it repeatedly grows that layer instead of creating
file-named scratch layers.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsFields,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingOutputVectorLayer,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProviderRegistry,
    QgsWkbTypes,
)

from . import cable_lay_parsers as clp


class CableLayImportAlgorithm(QgsProcessingAlgorithm):
    """Common scaffolding for the cable-lay file importers."""

    INPUT = "INPUT"
    START_DATE = "START_DATE"
    TARGET_LAYER = "TARGET_LAYER"
    GEOPACKAGE = "GEOPACKAGE"
    OUTPUT_LAYER = "OUTPUT_LAYER"

    # --- overridable class configuration -------------------------------------
    LAYER_TYPE = ""  # also the canonical GeoPackage layer name + dedupe key
    OUTPUT_WKB = QgsWkbTypes.Point
    TARGET_LAYER_TYPE = QgsProcessing.TypeVectorPoint  # filters the dropdown
    INPUT_LABEL = "Input File(s)"
    NEEDS_START_DATE = False

    # ------------------------------------------------------------------ helpers
    def tr(self, string):
        return QCoreApplication.translate(type(self).__name__, string)

    def group(self):
        return self.tr("Cable Lay Data Import")

    def groupId(self):
        return "cable_lay_data_import"

    # --------------------------------------------------------------- parameters
    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT,
                self.tr(self.INPUT_LABEL),
                layerType=QgsProcessing.TypeFile,
            )
        )
        if self.NEEDS_START_DATE:
            self.addParameter(
                QgsProcessingParameterString(
                    self.START_DATE,
                    self.tr("Project Start Date (YYYY-MM-DD) - date of day count 1"),
                    defaultValue="",
                    optional=True,
                )
            )
        self.add_extra_parameters()
        # Primary destination: pick an existing (pre-created) layer to add to.
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.TARGET_LAYER,
                self.tr("Existing layer to add to (e.g. from Create Cable Lay GeoPackage)"),
                types=[self.TARGET_LAYER_TYPE],
                optional=True,
            )
        )
        # Fallback: create / append to a GeoPackage file directly.
        gpkg = QgsProcessingParameterFileDestination(
            self.GEOPACKAGE,
            self.tr("... or a Target GeoPackage to create/append to"),
            fileFilter="GeoPackage (*.gpkg)",
            optional=True,
            createByDefault=False,
        )
        self.addParameter(gpkg)
        self.addOutput(
            QgsProcessingOutputVectorLayer(
                self.OUTPUT_LAYER,
                self.tr("Imported layer"),
            )
        )

    def add_extra_parameters(self):
        """Hook for subclasses that need extra parameters (e.g. a geometry source)."""

    def dedupe_key(self, parameters, context):
        """The per-type unique key used to drop duplicate rows on append.

        Subclasses may override to vary the key by parameter (e.g. fall back to
        the raw time column when ISO time parsing is disabled).
        """
        return clp.dedupe_key_for(self.LAYER_TYPE)

    # ------------------------------------------------------------------- helpers
    def read_start_date(self, parameters, context) -> str:
        start_date = self.parameterAsString(parameters, self.START_DATE, context).strip()
        if self.NEEDS_START_DATE and not start_date:
            raise QgsProcessingException(
                self.tr(
                    "Project Start Date (YYYY-MM-DD) is required to convert the "
                    "day-count times in this file."
                )
            )
        return start_date

    def log_params(self, parameters, context) -> Dict:
        """Scalar parameters recorded in ``import_log`` for reproducibility.

        Subclasses with extra parameters that affect parsing should extend the
        returned dict.
        """
        params: Dict = {}
        if self.parameterDefinition(self.START_DATE) is not None:
            params["start_date"] = self.parameterAsString(
                parameters, self.START_DATE, context
            ).strip()
        return params

    def _log_import(
        self, parameters, context, feedback, gpkg_path, layer_name, per_file_counts, duplicates
    ) -> None:
        """Record one ``import_log`` row per input file (never fails the import)."""
        try:
            params = self.log_params(parameters, context)
            start_date = params.get("start_date", "")
            extra = {k: v for k, v in params.items() if k != "start_date"}
            extra["run_duplicates_removed"] = duplicates
            params_json = json.dumps(extra, sort_keys=True)
            for source_file, rows_parsed in per_file_counts:
                clp.log_import(
                    gpkg_path,
                    context.transformContext(),
                    {
                        "layer_name": layer_name,
                        "source_file": source_file,
                        "algorithm": self.name(),
                        "start_date": start_date,
                        "params_json": params_json,
                        "rows_parsed": rows_parsed,
                    },
                )
        except Exception as exc:  # pragma: no cover - logging must never abort
            feedback.pushWarning(
                self.tr("Could not write to the import log: {error}").format(error=exc)
            )

    def _resolve_destination(self, parameters, context, feedback) -> Tuple[str, str]:
        """Return (gpkg_path, layer_name) from the chosen layer or GeoPackage."""
        target = self.parameterAsVectorLayer(parameters, self.TARGET_LAYER, context)
        gpkg_param = self.parameterAsFileOutput(parameters, self.GEOPACKAGE, context)

        if target is not None:
            if gpkg_param:
                feedback.pushInfo(
                    self.tr("Both a target layer and a GeoPackage were given; using the layer.")
                )
            decoded = QgsProviderRegistry.instance().decodeUri(target.providerType(), target.source())
            gpkg_path = decoded.get("path", "")
            layer_name = decoded.get("layerName") or ""
            if not gpkg_path.lower().endswith(".gpkg") or not layer_name:
                raise QgsProcessingException(
                    self.tr(
                        "The selected layer is not a GeoPackage layer. Pick a layer "
                        "created by 'Create Cable Lay GeoPackage', or use the Target "
                        "GeoPackage option instead."
                    )
                )
            if not layer_name.endswith(self.LAYER_TYPE):
                feedback.pushWarning(
                    self.tr(
                        "Target layer '{layer}' does not look like a '{type}' layer - "
                        "importing into it anyway."
                    ).format(layer=layer_name, type=self.LAYER_TYPE)
                )
            return gpkg_path, layer_name

        if gpkg_param:
            return gpkg_param, clp.prefixed_layer_name(gpkg_param, self.LAYER_TYPE)

        raise QgsProcessingException(
            self.tr(
                "Choose a destination: either an existing layer to add to, or a "
                "Target GeoPackage to create/append to."
            )
        )

    # ------------------------------------------------------------------- running
    def processAlgorithm(self, parameters, context, feedback):
        files = self.parameterAsFileList(parameters, self.INPUT, context)
        if not files:
            raise QgsProcessingException(self.tr("No input files were provided."))
        gpkg_path, layer_name = self._resolve_destination(parameters, context, feedback)

        # Parse every selected file, unioning the schema across them.
        new_rows: List[Dict] = []
        new_fields: QgsFields = None
        per_file_counts: List[Tuple[str, int]] = []
        for path in files:
            if feedback.isCanceled():
                break
            feedback.pushInfo(self.tr("Parsing {name} ...").format(name=os.path.basename(path)))
            rows, fields = self.parse_rows(path, parameters, context, feedback)
            new_rows.extend(rows)
            per_file_counts.append((os.path.basename(path), len(rows)))
            new_fields = fields if new_fields is None else clp.union_fields(new_fields, fields)
        if not new_rows:
            raise QgsProcessingException(
                self.tr("No valid records were parsed from the input file(s).")
            )

        key_fields = self.dedupe_key(parameters, context)
        existing_layer = clp.open_gpkg_layer(gpkg_path, layer_name)

        if existing_layer is not None and clp.fields_cover(existing_layer.fields(), new_fields):
            # Fast path: the stored schema already covers the new data, so
            # append through the provider — no full-table rewrite, which keeps
            # imports into multi-GB GeoPackages quick. Only the key columns of
            # the existing rows are read (for deduplication), never the table.
            existing_count = existing_layer.featureCount()
            feedback.pushInfo(
                self.tr("Appending to existing '{layer}' ({n} feature(s)).").format(
                    layer=layer_name, n=existing_count
                )
            )
            unique_new, _ = clp.merge_and_dedupe([], new_rows, key_fields)
            existing_keys = clp.read_key_set(existing_layer, key_fields)
            to_add = [
                row for row in unique_new
                if clp.row_key(row, key_fields) not in existing_keys
            ]
            duplicates = len(new_rows) - len(to_add)
            try:
                clp.append_features_to_layer(existing_layer, to_add)
            except RuntimeError as exc:
                raise QgsProcessingException(str(exc))
            written = existing_count + len(to_add)
        else:
            # Full write: fresh layer, or an existing layer whose schema must
            # grow to fit new columns (rare) — the original read-merge-rewrite.
            existing_rows: List[Dict] = []
            existing_fields = None
            if existing_layer is not None:
                existing_rows, existing_fields = clp.rows_from_source(existing_layer)
                feedback.pushInfo(
                    self.tr(
                        "New column(s) in this import - rewriting '{layer}' "
                        "({n} existing feature(s)) with the extended schema."
                    ).format(layer=layer_name, n=len(existing_rows))
                )

            out_fields = clp.union_fields(existing_fields, new_fields)
            merged, duplicates = clp.merge_and_dedupe(existing_rows, new_rows, key_fields)

            try:
                written = clp.write_layer_to_gpkg(
                    gpkg_path,
                    layer_name,
                    out_fields,
                    self.OUTPUT_WKB,
                    merged,
                    context.transformContext(),
                )
            except RuntimeError as exc:
                raise QgsProcessingException(str(exc))

        feedback.pushInfo(
            self.tr(
                "{files} file(s); {new} new record(s); removed {dups} duplicate(s); "
                "'{layer}' now holds {total} feature(s)."
            ).format(
                files=len(files),
                new=len(new_rows),
                dups=duplicates,
                layer=layer_name,
                total=written,
            )
        )

        self._log_import(
            parameters, context, feedback, gpkg_path, layer_name, per_file_counts, duplicates
        )

        layer_uri = clp.gpkg_layer_uri(gpkg_path, layer_name)
        # If the destination layer is already loaded in the project, refresh it
        # on completion (main thread) instead of adding a duplicate; otherwise
        # load it.
        self._reload_layer_ids = self._already_loaded_ids(context, gpkg_path, layer_name)
        if not self._reload_layer_ids and context.project() is not None:
            details = QgsProcessingContext.LayerDetails(layer_name, context.project(), layer_name)
            context.addLayerToLoadOnCompletion(layer_uri, details)

        return {self.GEOPACKAGE: gpkg_path, self.OUTPUT_LAYER: layer_uri}

    def postProcessAlgorithm(self, context, feedback):
        # Runs on the main thread: refresh any already-loaded copies of the
        # destination layer so the new features show without a duplicate.
        for layer_id in getattr(self, "_reload_layer_ids", []):
            layer = context.project().mapLayer(layer_id) if context.project() else None
            if layer is not None:
                layer.reload()
                layer.triggerRepaint()
        return {}

    @staticmethod
    def _already_loaded_ids(context, gpkg_path: str, layer_name: str) -> List[str]:
        project = context.project()
        if project is None:
            return []
        target = os.path.normcase(os.path.normpath(gpkg_path))
        ids = []
        registry = QgsProviderRegistry.instance()
        for layer_id, layer in project.mapLayers().items():
            try:
                decoded = registry.decodeUri(layer.providerType(), layer.source())
            except Exception:
                continue
            path = decoded.get("path", "")
            if not path:
                continue
            if os.path.normcase(os.path.normpath(path)) == target and decoded.get("layerName") == layer_name:
                ids.append(layer_id)
        return ids

    # ------------------------------------------------------------ to be provided
    def parse_rows(
        self, path: str, parameters, context, feedback
    ) -> Tuple[List[Dict], QgsFields]:
        """Parse a single ``path`` into (rows, fields). Implemented by each subclass."""
        raise NotImplementedError
