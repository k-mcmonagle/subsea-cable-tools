# run_cable_lay_qc_algorithm.py
# -*- coding: utf-8 -*-
"""RunCableLayQcAlgorithm

Thin processing wrapper over the ``laydata`` QC engine. Loads a cable-lay data
layer, runs the selected quality-control checks and produces a point layer of
findings. When the input layer lives in a GeoPackage the findings can also be
written back into that file's ``qc_findings`` layer so results are persisted,
symbolisable and repeatable.

The heavy lifting (gap / duplicate / precision detection) lives in
``laydata.qc_checks`` so the exact same logic backs the Data Explorer window.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsWkbTypes,
)

from ..qgis_compat import (
    PROCESSING_FIELD_ANY,
    PROCESSING_NUMBER_DOUBLE,
    PROCESSING_NUMBER_INTEGER,
)
from ..laydata import LayDataset, QcRunner
from ..laydata.qc_checks import ALL_CHECKS
from . import cable_lay_parsers as clp


class RunCableLayQcAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    CHECKS = "CHECKS"
    EXPECTED_INTERVAL = "EXPECTED_INTERVAL"
    GAP_FACTOR = "GAP_FACTOR"
    MAX_SPACING_M = "MAX_SPACING_M"
    PRECISION_FIELD = "PRECISION_FIELD"
    EXPECTED_DP = "EXPECTED_DP"
    DUP_MODE = "DUP_MODE"
    WRITE_TO_GPKG = "WRITE_TO_GPKG"
    CLEAR_EXISTING = "CLEAR_EXISTING"
    OUTPUT = "OUTPUT"

    _DUP_MODES = ["time", "time_position"]

    def tr(self, string):
        return QCoreApplication.translate("RunCableLayQcAlgorithm", string)

    def createInstance(self):
        return RunCableLayQcAlgorithm()

    def name(self):
        return "run_cable_lay_qc"

    def displayName(self):
        return self.tr("Run Cable Lay QC")

    def group(self):
        return self.tr("Cable Lay QC & Analysis")

    def groupId(self):
        return "cable_lay_qc"

    def shortHelpString(self):
        return self.tr(
            """
<h3>Run Cable Lay QC</h3>
<p>Runs quality-control checks over an imported cable-lay data layer and outputs
a point layer of findings (one point per issue), which you can style by
<code>severity</code> or <code>check_id</code>.</p>
<p>Available checks:</p>
<ul>
  <li><b>Time gaps</b> - breaks larger than the expected logging interval
      (set interval to 0 to auto-detect the cadence, e.g. 1 s or 30 s).</li>
  <li><b>Distance gaps</b> - consecutive records more than a set distance apart.</li>
  <li><b>Decimal precision</b> - a chosen field (e.g. KP) carries the expected
      number of decimal places.</li>
  <li><b>Duplicates</b> - repeated timestamps (optionally same position).</li>
</ul>
<p>Checks run independently per logging source (<code>source_file</code> etc.)
so primary and backup systems are assessed separately.</p>
<p>When the input layer is in a GeoPackage, enable <i>Write findings to the
GeoPackage</i> to persist results in its <code>qc_findings</code> layer for a
repeatable workflow that travels with the project.</p>
"""
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT,
                self.tr("Cable lay data layer"),
                [QgsProcessing.TypeVectorAnyGeometry],
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.CHECKS,
                self.tr("Checks to run"),
                options=[cls.name for cls in ALL_CHECKS],
                allowMultiple=True,
                defaultValue=list(range(len(ALL_CHECKS))),
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.EXPECTED_INTERVAL,
                self.tr("Time gaps: expected interval (s, 0 = auto)"),
                type=PROCESSING_NUMBER_DOUBLE,
                minValue=0.0,
                defaultValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.GAP_FACTOR,
                self.tr("Time gaps: gap factor (x interval)"),
                type=PROCESSING_NUMBER_DOUBLE,
                minValue=1.0,
                defaultValue=1.5,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_SPACING_M,
                self.tr("Distance gaps: maximum spacing (m)"),
                type=PROCESSING_NUMBER_DOUBLE,
                minValue=0.0,
                defaultValue=50.0,
            )
        )
        precision_field = QgsProcessingParameterField(
            self.PRECISION_FIELD,
            self.tr("Decimal precision: field to check (optional)"),
            parentLayerParameterName=self.INPUT,
            type=PROCESSING_FIELD_ANY,
            optional=True,
        )
        self.addParameter(precision_field)
        self.addParameter(
            QgsProcessingParameterNumber(
                self.EXPECTED_DP,
                self.tr("Decimal precision: expected decimal places"),
                type=PROCESSING_NUMBER_INTEGER,
                minValue=0,
                maxValue=12,
                defaultValue=3,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.DUP_MODE,
                self.tr("Duplicates: definition"),
                options=self._DUP_MODES,
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.WRITE_TO_GPKG,
                self.tr("Write findings to the GeoPackage (qc_findings layer)"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.CLEAR_EXISTING,
                self.tr("Replace previous findings in the GeoPackage"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("QC findings"),
                type=QgsProcessing.TypeVectorPoint,
                optional=True,
                createByDefault=True,
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(self.tr("Invalid input layer."))

        selected = self.parameterAsEnums(parameters, self.CHECKS, context)
        if not selected:
            raise QgsProcessingException(self.tr("Select at least one check to run."))
        precision_field = self.parameterAsString(parameters, self.PRECISION_FIELD, context)
        dup_mode = self._DUP_MODES[self.parameterAsEnum(parameters, self.DUP_MODE, context)]

        params_by_id = {
            "time_gap": {
                "expected_interval_s": self.parameterAsDouble(parameters, self.EXPECTED_INTERVAL, context),
                "gap_factor": self.parameterAsDouble(parameters, self.GAP_FACTOR, context),
            },
            "distance_gap": {
                "max_spacing_m": self.parameterAsDouble(parameters, self.MAX_SPACING_M, context),
            },
            "decimal_precision": {
                "field": precision_field or "",
                "expected_dp": self.parameterAsInt(parameters, self.EXPECTED_DP, context),
            },
            "duplicate": {"mode": dup_mode},
        }

        feedback.pushInfo(self.tr("Loading features..."))
        dataset = LayDataset.from_qgis_layer(layer)
        feedback.pushInfo(
            self.tr("Loaded {n} record(s); source field: {src}; time field: {t}.").format(
                n=dataset.row_count,
                src=dataset.source_field or "-",
                t=dataset.time_field or "-",
            )
        )

        checks = []
        for index in selected:
            check_cls = ALL_CHECKS[index]
            checks.append((check_cls(), params_by_id.get(check_cls.check_id)))

        runner = QcRunner(dataset)
        findings = runner.run(checks)
        feedback.pushInfo(self.tr("QC produced {n} finding(s).").format(n=len(findings)))

        run_id = uuid.uuid4().hex[:12]
        run_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        rows = QcRunner.findings_to_rows(findings, layer.name(), run_id, run_time, clp.WKT_KEY)

        # Feature sink output.
        fields = clp.fields_from_specs(clp.QC_FINDINGS_SPECS)
        sink, dest_id = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.Point,
            layer.crs() if layer.crs().isValid() else None,
        )
        if sink is not None:
            for row in rows:
                feature = QgsFeature(fields)
                for i, field in enumerate(fields):
                    feature.setAttribute(i, row.get(field.name()))
                wkt = row.get(clp.WKT_KEY)
                if wkt:
                    geom = QgsGeometry.fromWkt(wkt)
                    if geom is not None and not geom.isEmpty():
                        feature.setGeometry(geom)
                sink.addFeature(feature, QgsFeatureSink.FastInsert)

        results = {self.OUTPUT: dest_id}

        # Persist into the GeoPackage when requested and possible.
        if self.parameterAsBool(parameters, self.WRITE_TO_GPKG, context):
            gpkg_path = self._gpkg_path(layer)
            if not gpkg_path:
                feedback.pushWarning(
                    self.tr("Input is not a GeoPackage layer - skipping qc_findings write.")
                )
            else:
                clear = self.parameterAsBool(parameters, self.CLEAR_EXISTING, context)
                self._write_findings_to_gpkg(gpkg_path, rows, clear, context, feedback)
                results["QC_FINDINGS_LAYER"] = clp.gpkg_layer_uri(
                    gpkg_path, clp.prefixed_layer_name(gpkg_path, "qc_findings")
                )

        return results

    @staticmethod
    def _gpkg_path(layer):
        source = layer.source() or ""
        path = source.split("|", 1)[0]
        if path.lower().endswith(".gpkg") and os.path.exists(path):
            return path
        return None

    def _write_findings_to_gpkg(self, gpkg_path, rows, clear, context, feedback):
        transform_context = context.transformContext()
        clp.ensure_qc_layers(gpkg_path, transform_context)
        layer_name = clp.prefixed_layer_name(gpkg_path, "qc_findings")
        fields = clp.fields_from_specs(clp.QC_FINDINGS_SPECS)

        existing_rows = []
        if not clear:
            existing = clp.open_gpkg_layer(gpkg_path, layer_name)
            if existing is not None:
                existing_rows, _ = clp.rows_from_source(existing)

        clp.write_layer_to_gpkg(
            gpkg_path,
            layer_name,
            fields,
            QgsWkbTypes.Point,
            existing_rows + rows,
            transform_context,
        )
        feedback.pushInfo(
            self.tr("Wrote {n} finding(s) to '{layer}'.").format(
                n=len(existing_rows) + len(rows), layer=layer_name
            )
        )
