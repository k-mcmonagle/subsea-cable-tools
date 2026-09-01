# recompute_iso_time_algorithm.py
# -*- coding: utf-8 -*-
"""
RecomputeIsoTimeAlgorithm
Rewrite the ISO_Time column of an imported cable-lay layer in place, e.g. after
the wrong Project Start Date was used at import.
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProviderRegistry,
)

from . import cable_lay_manage_ops as ops
from . import cable_lay_parsers as clp


class RecomputeIsoTimeAlgorithm(QgsProcessingAlgorithm):
    """Recompute ISO_Time from the stored day-count times, in place."""

    TARGET_LAYER = "TARGET_LAYER"
    START_DATE = "START_DATE"
    OLD_START_DATE = "OLD_START_DATE"
    SOURCE_FILES = "SOURCE_FILES"
    DEDUPE = "DEDUPE"

    def tr(self, string):
        return QCoreApplication.translate(type(self).__name__, string)

    def createInstance(self):
        return RecomputeIsoTimeAlgorithm()

    def name(self):
        return "recompute_iso_time"

    def displayName(self):
        return self.tr("Recompute ISO Time (fix start date)")

    def group(self):
        return self.tr("Cable Lay Data Import")

    def groupId(self):
        return "cable_lay_data_import"

    def shortHelpString(self):
        return self.tr(
            """
<h3>Recompute ISO Time (fix start date)</h3>
<p>Rewrites the <code>ISO_Time</code> column of an imported cable-lay layer
<b>in place</b> — no re-import needed — using the raw <code>day,HH:MM:SS</code>
time column the importers preserve (<code>Time</code>, <code>Event Time</code>
or <code>Lay Time</code>). Use it when the wrong <i>Project Start Date</i> was
entered at import.</p>

<h4>Inputs</h4>
<ul>
  <li><b>Layer</b>: a layer imported by the Cable Lay Data Import tools
  (must live in a GeoPackage).</li>
  <li><b>Corrected Project Start Date</b> (YYYY-MM-DD): the true date of day
  count 1.</li>
  <li><b>Previous start date</b> (optional): only needed for rows without a raw
  day-count column — their existing <code>ISO_Time</code> is then shifted by
  the difference between the two dates.</li>
  <li><b>Source file(s)</b> (optional): limit the fix to specific imported
  files (comma/semicolon-separated names, as stored in the layer's
  <code>source_file</code> column). Blank = every row.</li>
  <li><b>Remove resulting duplicates</b>: after recomputing, rows that now
  collide on the layer's duplicate key (e.g. same <code>ISO_Time</code> and
  <code>source_file</code>) are deleted, keeping the first. This catches data
  that was imported twice under different start dates.</li>
</ul>

<p>The edit is applied directly in the GeoPackage (fast, even on very large
files) and recorded in the <code>edit_log</code> table so the change history
travels with the data.</p>
"""
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.TARGET_LAYER,
                self.tr("Layer to fix (from the Cable Lay Data Import tools)"),
                types=[QgsProcessing.TypeVector],
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.START_DATE,
                self.tr("Corrected Project Start Date (YYYY-MM-DD) - date of day count 1"),
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.OLD_START_DATE,
                self.tr("Previous start date (YYYY-MM-DD) - only for rows without a day-count column"),
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.SOURCE_FILES,
                self.tr("Only these source file(s) (comma/semicolon-separated; blank = all)"),
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.DEDUPE,
                self.tr("Remove resulting duplicates"),
                defaultValue=True,
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        target = self.parameterAsVectorLayer(parameters, self.TARGET_LAYER, context)
        if target is None:
            raise QgsProcessingException(self.tr("No layer was selected."))
        start_date = self.parameterAsString(parameters, self.START_DATE, context).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", start_date):
            raise QgsProcessingException(
                self.tr("Corrected Project Start Date must be YYYY-MM-DD.")
            )
        old_start_date = self.parameterAsString(parameters, self.OLD_START_DATE, context).strip()
        dedupe = self.parameterAsBool(parameters, self.DEDUPE, context)
        source_files = self._source_file_list(parameters, context)

        decoded = QgsProviderRegistry.instance().decodeUri(
            target.providerType(), target.source()
        )
        gpkg_path = decoded.get("path", "")
        layer_name = decoded.get("layerName") or ""
        if not gpkg_path.lower().endswith(".gpkg") or not layer_name:
            raise QgsProcessingException(
                self.tr("The selected layer is not a GeoPackage layer.")
            )

        # Work on a private connection so the edit is safe from the processing
        # thread; loaded project copies are refreshed in postProcessAlgorithm.
        layer = clp.open_gpkg_layer(gpkg_path, layer_name)
        if layer is None:
            raise QgsProcessingException(
                self.tr("Could not open '{layer}' in {path}.").format(
                    layer=layer_name, path=gpkg_path
                )
            )

        try:
            counts = ops.recompute_iso_time(
                layer,
                start_date,
                old_start_date=old_start_date,
                source_files=source_files,
                feedback=feedback,
            )
        except RuntimeError as exc:
            raise QgsProcessingException(str(exc))

        feedback.pushInfo(
            self.tr(
                "Examined {examined} row(s): {updated} updated, {unchanged} already "
                "correct, {skipped} skipped (no parseable time)."
            ).format(**counts)
        )

        duplicates = 0
        if dedupe:
            layer_type = ops.layer_type_for_name(layer_name)
            key_fields = clp.dedupe_key_for(layer_type or "")
            try:
                duplicates = ops.dedupe_layer_in_place(
                    layer, key_fields, source_files=source_files, feedback=feedback
                )
            except RuntimeError as exc:
                raise QgsProcessingException(str(exc))
            if duplicates:
                feedback.pushInfo(
                    self.tr("Removed {n} duplicate row(s) (key: {key}).").format(
                        n=duplicates, key=", ".join(key_fields)
                    )
                )

        try:
            clp.log_edit(
                gpkg_path,
                context.transformContext(),
                {
                    "layer_name": layer_name,
                    "operation": "recompute_iso_time",
                    "params_json": json.dumps(
                        {
                            "start_date": start_date,
                            "old_start_date": old_start_date,
                            "source_files": source_files,
                            "dedupe": dedupe,
                            "duplicates_removed": duplicates,
                        },
                        sort_keys=True,
                    ),
                    "rows_affected": counts["updated"] + duplicates,
                    "details": (
                        f"updated={counts['updated']} unchanged={counts['unchanged']} "
                        f"skipped={counts['skipped']} duplicates_removed={duplicates}"
                    ),
                },
            )
        except Exception as exc:  # logging must never abort the fix itself
            feedback.pushWarning(
                self.tr("Could not write to the edit log: {error}").format(error=exc)
            )

        self._refresh_paths = (gpkg_path, layer_name)
        return {
            "UPDATED": counts["updated"],
            "UNCHANGED": counts["unchanged"],
            "SKIPPED": counts["skipped"],
            "DUPLICATES_REMOVED": duplicates,
        }

    def postProcessAlgorithm(self, context, feedback):
        # Main thread: refresh any loaded copies of the edited layer.
        gpkg_path, layer_name = getattr(self, "_refresh_paths", (None, None))
        project = context.project()
        if not gpkg_path or project is None:
            return {}
        target = os.path.normcase(os.path.normpath(gpkg_path))
        registry = QgsProviderRegistry.instance()
        for layer in project.mapLayers().values():
            try:
                decoded = registry.decodeUri(layer.providerType(), layer.source())
            except Exception:
                continue
            path = decoded.get("path", "")
            if not path:
                continue
            if (
                os.path.normcase(os.path.normpath(path)) == target
                and decoded.get("layerName") == layer_name
            ):
                layer.reload()
                layer.triggerRepaint()
        return {}

    def _source_file_list(self, parameters, context) -> Optional[List[str]]:
        raw = self.parameterAsString(parameters, self.SOURCE_FILES, context).strip()
        if not raw:
            return None
        names = [part.strip() for part in re.split(r"[,;]", raw)]
        return [name for name in names if name] or None
