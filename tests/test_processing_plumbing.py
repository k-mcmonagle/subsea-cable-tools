# -*- coding: utf-8 -*-
"""Checks for the Processing provider plumbing and small algorithm fixes.

* every algorithm has a Help URL; the provider has the plugin icon;
* the legacy Excel RPL importer is flagged Deprecated (still runnable);
* GDAL child calls use the creation-options parameter the running QGIS
  defines (``CREATION_OPTIONS`` on QGIS 4, ``OPTIONS`` on 3.x);
* the MDB ODBC helpers close their connection (pyodbc's ``with`` does not,
  which left Access's ``.ldb`` lock until garbage collection);
* KP Range Depth + Slope Summary reports contours it had to leave out.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import types
from typing import List

from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingParameterString,
    QgsProcessingProvider,
    QgsProject,
    QgsVectorLayer,
)

from ..qgis_compat import FIELD_TYPE_DOUBLE
from ..processing import import_mdb_algorithm
from ..processing.algorithm_base import HELP_URL, deprecated_flag, gdal_creation_options
from ..processing.import_excel_rpl_algorithm import ImportExcelRPLAlgorithm
from ..processing.kp_range_depth_slope_summary_algorithm import KPRangeDepthSlopeSummaryAlgorithm
from ..processing.subsea_cable_processing_provider import SubseaCableProcessingProvider


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def test_help_urls_and_provider_icon() -> bool:
    provider = SubseaCableProcessingProvider()
    provider.loadAlgorithms()
    algorithms = list(provider.algorithms())
    missing = [a.name() for a in algorithms if not (a.helpUrl() or "").startswith(HELP_URL)]
    ok = len(algorithms) > 0 and not missing
    ok = ok and not provider.icon().isNull()
    ok = ok and provider.id() == "subsea_cable_processing" and provider.longName() != provider.name()
    return _result("provider: help URL on every algorithm, plugin icon, ids", ok,
                   f"{len(algorithms)} algorithms, missing help: {missing}")


def test_excel_import_deprecated() -> bool:
    algorithm = ImportExcelRPLAlgorithm()
    algorithm.initAlgorithm({})
    flags = algorithm.flags()
    ok = bool(flags & deprecated_flag())
    ok = ok and algorithm.name() == "importexcelrpl" and "deprecated" in algorithm.displayName().lower()
    return _result("import Excel RPL: flagged deprecated, id unchanged", ok, algorithm.displayName())


class _FakeGdalAlgorithm(QgsProcessingAlgorithm):
    def __init__(self, name, option_param):
        super().__init__()
        self._name = name
        self._option_param = option_param

    def name(self):
        return self._name

    def displayName(self):
        return self._name

    def createInstance(self):
        return _FakeGdalAlgorithm(self._name, self._option_param)

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterString(
            self._option_param, self._option_param, optional=True))

    def processAlgorithm(self, parameters, context, feedback):
        return {}


class _FakeGdalProvider(QgsProcessingProvider):
    def id(self):
        return "sct_fake_gdal"

    def name(self):
        return "sct fake gdal"

    def loadAlgorithms(self):
        self.addAlgorithm(_FakeGdalAlgorithm("qgis4style", "CREATION_OPTIONS"))
        self.addAlgorithm(_FakeGdalAlgorithm("qgis3style", "OPTIONS"))


def test_gdal_creation_options_key() -> bool:
    registry = QgsApplication.processingRegistry()
    provider = _FakeGdalProvider()
    registry.addProvider(provider)
    try:
        qgis4 = gdal_creation_options("sct_fake_gdal:qgis4style", "COMPRESS=LZW")
        qgis3 = gdal_creation_options("sct_fake_gdal:qgis3style", "COMPRESS=LZW")
        unknown = gdal_creation_options("sct_fake_gdal:missing", "COMPRESS=LZW")
    finally:
        registry.removeProvider(provider)
    real = gdal_creation_options("gdal:translate", "X")
    real_alg = registry.algorithmById("gdal:translate")
    real_ok = real_alg is None or real_alg.parameterDefinition(next(iter(real))) is not None
    ok = (qgis4 == {"CREATION_OPTIONS": "COMPRESS=LZW"}
          and qgis3 == {"OPTIONS": "COMPRESS=LZW"}
          and unknown == {"OPTIONS": "COMPRESS=LZW"}
          and real_ok)
    return _result("GDAL child calls: creation-options key follows the running QGIS", ok,
                   f"4-style={qgis4} 3-style={qgis3} gdal:translate -> {sorted(real)} "
                   f"(provider {'loaded' if real_alg else 'not loaded'})")


class _FakeCursor:
    description = [("ID",)]

    def tables(self):
        return []

    def execute(self, *args):
        return self

    def fetchone(self):
        return None


class _FakeConnection:
    def __init__(self):
        self.closed = False

    def cursor(self):
        return _FakeCursor()

    def close(self):
        self.closed = True

    # pyodbc semantics: the context manager commits/rolls back, never closes.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_mdb_odbc_connections_closed() -> bool:
    connections = []

    def connect(*_args, **_kwargs):
        connections.append(_FakeConnection())
        return connections[-1]

    fake = types.SimpleNamespace(
        connect=connect,
        drivers=lambda: [import_mdb_algorithm.ACCESS_ODBC_DRIVER_NAME],
        Error=RuntimeError,
    )
    saved = import_mdb_algorithm.pyodbc
    import_mdb_algorithm.pyodbc = fake
    feedback = QgsProcessingFeedback()
    try:
        import_mdb_algorithm.get_feature_tables("C:/nowhere/fake.mdb", feedback)
        import_mdb_algorithm.get_attribute_fields("C:/nowhere/fake.mdb", "T", feedback)
        import_mdb_algorithm.import_table_as_memory_layer(
            "C:/nowhere/fake.mdb", "T", "Geometry", 1, None, feedback)
    finally:
        import_mdb_algorithm.pyodbc = saved
    ok = len(connections) == 3 and all(c.closed for c in connections)
    return _result("MDB import: ODBC connections are closed, not left to GC", ok,
                   f"closed={[c.closed for c in connections]}")


class _WarningFeedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.warnings = []

    def pushWarning(self, warning):  # noqa: N802
        self.warnings.append(str(warning))
        super().pushWarning(warning)


def test_summary_reports_skipped_contours() -> bool:
    x0, y0 = 500000.0, 6000000.0
    ranges = QgsVectorLayer("LineString?crs=EPSG:32631", "ranges", "memory")
    feat = QgsFeature()
    feat.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x0, y0), QgsPointXY(x0, y0 + 2000.0)]))
    ranges.dataProvider().addFeatures([feat])
    contours = QgsVectorLayer("LineString?crs=EPSG:32631", "contours", "memory")
    contours.dataProvider().addAttributes([QgsField("depth", FIELD_TYPE_DOUBLE)])
    contours.updateFields()
    feats = []
    for k in range(21):
        c = QgsFeature(contours.fields())
        c.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(x0 - 500.0, y0 + k * 100.0), QgsPointXY(x0 + 500.0, y0 + k * 100.0)]))
        c.setAttributes([None if k == 7 else 100.0 + 2.0 * k])
        feats.append(c)
    contours.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayers([ranges, contours])

    algorithm = KPRangeDepthSlopeSummaryAlgorithm()
    algorithm.initAlgorithm({})
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = _WarningFeedback()
    results, ok = algorithm.run({
        "INPUT": ranges, "DEPTH_SOURCE": 2, "CONTOUR_LAYER_1": contours,
        "CONTOUR_DEPTH_FIELD_1": "depth", "SAMPLE_INTERVAL_M": 50.0, "OUTPUT": "memory:",
    }, context, feedback)
    detail = "run failed"
    if ok:
        out = context.takeResultLayer(results["OUTPUT"])
        row = next(out.getFeatures())
        warned = any("no usable depth" in w for w in feedback.warnings)
        ok = warned and abs(row["depth_min"] - 100.0) < 1e-6 and abs(row["depth_max"] - 140.0) < 1e-6
        detail = f"warnings={feedback.warnings} depth={row['depth_min']}..{row['depth_max']}"
    QgsProject.instance().removeMapLayers([ranges.id(), contours.id()])
    return _result("KP range summary: skipped contours are reported", ok, detail)


def run_all() -> List[bool]:
    return [
        test_help_urls_and_provider_icon(),
        test_excel_import_deprecated(),
        test_gdal_creation_options_key(),
        test_mdb_odbc_connections_closed(),
        test_summary_reports_skipped_contours(),
    ]


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(0 if all(run_all()) else 1)
