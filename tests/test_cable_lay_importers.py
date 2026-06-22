"""Checks for the Cable Lay Data Import algorithms and their shared parsers.

Covers the pure-Python helpers (coordinate / time parsing, type inference,
deduplication) and runs the importers end-to-end against tiny temp fixtures,
writing into a GeoPackage and exercising the multi-file merge, the
append-and-deduplicate-on-re-run behaviour, and the GeoPackage setup tool.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile
from typing import List

from qgis.core import (
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..processing import cable_lay_parsers as clp
from ..processing.create_cable_lay_geopackage_algorithm import CreateCableLayGeoPackageAlgorithm
from ..processing.import_body_log_algorithm import ImportBodyLogAlgorithm
from ..processing.import_cable_lay_algorithm import ImportCableLayAlgorithm
from ..processing.import_plough_data_algorithm import ImportPloughDataAlgorithm
from ..processing.import_slack_log_algorithm import ImportSlackLogAlgorithm


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
_SLACK_FILE = """Slack log header line 1
KP1 KP2 lat1 lon1 lat2 lon2 off1 off2 d1 d2 ss sh labels
0.000 0.100 17 09.7399N 169 30.1234W 17 09.8000N 169 30.2000W 0.1 0.2 1500 1501 5.0 6.0 BodyA
0.100 0.200 17 09.8000N 169 30.2000W 17 09.9000N 169 30.3000W 0.1 0.2 1502 1503 5.1 6.1 BodyB
"""

_BODY_FILE = """Body log header 1
header 2
header 3
header 4
Body Alpha 123.4 17 09.7399 N 169 30.1234 W 1500.0 12.345 0.5 P 5.0 0.1 0.2
Body Bravo 130.0 17 09.8000 N 169 30.2000 W 1502.0 12.500 0.6 S 5.1 0.2 0.3
"""

_PLOUGH_FILE = (
    "Record,Time,Latitude,Longitude,Depth\n"
    "#,units,dms,dms,m\n"
    '1,"1,14:00:00","17 09.7399N","169 30.1234W",1500\n'
    '2,"1,14:00:01","17 09.8000N","169 30.2000W",1501\n'
)

_CABLE_LAY_FILE = (
    "Time,Ship Latitude,Ship Longitude,Ship KP\n"
    "dd:hh:mm:ss,dms,dms,km\n"
    '"1,14:00:00","17 09.7399N","169 30.1234W",0.000\n'
    '"1,14:00:01","17 09.8000N","169 30.2000W",0.025\n'
    '"1,14:00:02","17 09.9000N","169 30.3000W",0.050\n'
)


def _write_temp(name: str, content: str) -> str:
    path = os.path.join(tempfile.gettempdir(), name)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return path


def _fresh_gpkg(name: str) -> str:
    path = os.path.join(tempfile.gettempdir(), name)
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass
    return path


def _open(gpkg_path: str, layer_name: str):
    vl = QgsVectorLayer(clp.gpkg_layer_uri(gpkg_path, layer_name), layer_name, "ogr")
    return vl if vl.isValid() else None


def _run(alg, files: List[str], gpkg_path: str, extra: dict = None):
    alg.initAlgorithm()
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    params = {"INPUT": files, "GEOPACKAGE": gpkg_path}
    if extra:
        params.update(extra)
    alg.processAlgorithm(params, context, feedback)
    return _open(gpkg_path, clp.prefixed_layer_name(gpkg_path, alg.LAYER_TYPE))


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------
def test_parse_dms() -> bool:
    cases = {
        "17 09.7399N": 17.0 + 9.7399 / 60.0,
        "17 09.7399 N": 17.0 + 9.7399 / 60.0,
        "169 30.1234W": -(169.0 + 30.1234 / 60.0),
        "01 19.4445189S": -(1.0 + 19.4445189 / 60.0),
    }
    ok = all(
        clp.parse_dms_to_dd(text) is not None and abs(clp.parse_dms_to_dd(text) - expected) < 1e-9
        for text, expected in cases.items()
    )
    ok = ok and clp.parse_dms_to_dd("not a coord") is None
    return _result("parse_dms_to_dd (both spacings, hemispheres)", ok)


def test_parse_day_time() -> bool:
    dt = clp.parse_day_time("12,14:23:45", "2024-08-14")
    ok = dt is not None and clp.iso_str(dt) == "2024-08-25T14:23:45"
    ok = ok and clp.parse_day_time("bad", "2024-08-14") is None
    ok = ok and clp.parse_day_time("1,00:00:00", "") is None
    ok = ok and clp.looks_like_day_time("3,01:02:03") and not clp.looks_like_day_time("x")
    return _result("parse_day_time + iso_str (day offset)", ok)


def test_type_inference() -> bool:
    records = [
        {"a": "1", "b": "1.5", "c": "foo"},
        {"a": "2", "b": "2.0", "c": "bar"},
        {"a": "3", "b": "3.25", "c": "baz"},
    ]
    types = clp.infer_column_types(records)
    ok = types == {"a": "int", "b": "float", "c": "str"}
    ok = ok and clp.coerce_value("12%", "int") == 12
    ok = ok and abs(clp.coerce_value("3,5", "float") - 3.5) < 1e-9  # comma decimal
    ok = ok and clp.coerce_value("n/a", "float") is None
    return _result("infer_column_types + coerce_value", ok)


def test_merge_and_dedupe() -> bool:
    key = clp.dedupe_key_for("slack_logs")
    existing = [{"slack_file": "a.log", "KP1": 0.0, "KP2": 0.1}]
    incoming = [
        {"slack_file": "a.log", "KP1": 0.0, "KP2": 0.1},  # duplicate
        {"slack_file": "a.log", "KP1": 0.1, "KP2": 0.2},  # new
    ]
    merged, dups = clp.merge_and_dedupe(existing, incoming, key)
    ok = len(merged) == 2 and dups == 1
    return _result("merge_and_dedupe (per-type key)", ok, f"merged={len(merged)} dups={dups}")


# ---------------------------------------------------------------------------
# End-to-end importer tests
# ---------------------------------------------------------------------------
def test_setup_geopackage() -> bool:
    gpkg = _fresh_gpkg("sct_test_setup.gpkg")
    try:
        alg = CreateCableLayGeoPackageAlgorithm()
        alg.initAlgorithm()
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        alg.processAlgorithm({"GEOPACKAGE": gpkg}, context, QgsProcessingFeedback())
    except Exception as exc:
        return _result("create cable lay geopackage", False, repr(exc))
    expected_geom = {
        "cable_lay": QgsWkbTypes.PointGeometry,
        "event_logs": QgsWkbTypes.PointGeometry,
        "slack_logs": QgsWkbTypes.LineGeometry,
        "body_logs": QgsWkbTypes.PointGeometry,
        "model_solutions": QgsWkbTypes.PointGeometry,
        "as_laid": QgsWkbTypes.PointGeometry,
        "plough_data": QgsWkbTypes.PointGeometry,
    }
    missing = []
    wrong = []
    for name, geom in expected_geom.items():
        physical = clp.prefixed_layer_name(gpkg, name)
        layer = _open(gpkg, physical)
        if layer is None:
            missing.append(physical)
        elif layer.geometryType() != geom:
            wrong.append(physical)
    ok = not missing and not wrong
    return _result("create cable lay geopackage (prefixed names)", ok, f"missing={missing} wrong_geom={wrong}")


def test_slack_importer() -> bool:
    path = _write_temp("sct_test_slack.log", _SLACK_FILE)
    gpkg = _fresh_gpkg("sct_test_slack.gpkg")
    try:
        layer = _run(ImportSlackLogAlgorithm(), [path], gpkg)
    except Exception as exc:
        return _result("slack importer (lines)", False, repr(exc))
    ok = (
        layer is not None
        and layer.featureCount() == 2
        and layer.geometryType() == QgsWkbTypes.LineGeometry
    )
    return _result("slack importer (lines)", ok, "" if ok else "unexpected result")


def test_body_importer() -> bool:
    path = _write_temp("sct_test_body.log", _BODY_FILE)
    gpkg = _fresh_gpkg("sct_test_body.gpkg")
    try:
        layer = _run(ImportBodyLogAlgorithm(), [path], gpkg)
    except Exception as exc:
        return _result("body importer (multi-word label)", False, repr(exc))
    ok = layer is not None and layer.featureCount() == 2
    if ok:
        labels = sorted(f["Body_Label"] for f in layer.getFeatures())
        ok = labels == ["Body Alpha", "Body Bravo"]
    return _result("body importer (multi-word label)", ok)


def test_cable_lay_importer() -> bool:
    path = _write_temp("sct_test_cable.csv", _CABLE_LAY_FILE)
    gpkg = _fresh_gpkg("sct_test_cable.gpkg")
    try:
        layer = _run(ImportCableLayAlgorithm(), [path], gpkg, {"START_DATE": "2024-01-01"})
    except Exception as exc:
        return _result("cable lay importer (points + ISO_Time)", False, repr(exc))
    ok = (
        layer is not None
        and layer.featureCount() == 3
        and layer.geometryType() == QgsWkbTypes.PointGeometry
        and "ISO_Time" in [fld.name() for fld in layer.fields()]
    )
    return _result("cable lay importer (points + ISO_Time)", ok)


def test_multi_file_and_append_dedupe() -> bool:
    a = _write_temp("sct_test_plough_a.csv", _PLOUGH_FILE)
    b = _write_temp("sct_test_plough_b.csv", _PLOUGH_FILE)
    gpkg = _fresh_gpkg("sct_test_plough.gpkg")
    extra = {"START_DATE": "2024-01-01"}
    try:
        # Two distinct files in one run -> 4 features (different source_file each).
        layer = _run(ImportPloughDataAlgorithm(), [a, b], gpkg, extra)
    except Exception as exc:
        return _result("plough multi-file + re-run dedupe", False, repr(exc))
    if layer is None or layer.featureCount() != 4:
        return _result(
            "plough multi-file + re-run dedupe",
            False,
            f"first count={None if layer is None else layer.featureCount()} (expected 4)",
        )
    if "ISO_Time" not in [fld.name() for fld in layer.fields()]:
        return _result("plough multi-file + re-run dedupe", False, "no ISO_Time field")
    try:
        # Re-import the same two files into the same GeoPackage -> still 4 (dedupe).
        layer2 = _run(ImportPloughDataAlgorithm(), [a, b], gpkg, extra)
    except Exception as exc:
        return _result("plough multi-file + re-run dedupe", False, f"re-run: {exc!r}")
    ok = layer2 is not None and layer2.featureCount() == 4
    return _result(
        "plough multi-file + re-run dedupe",
        ok,
        f"after re-run count={None if layer2 is None else layer2.featureCount()} (expected 4)",
    )


def test_target_layer_dropdown_append() -> bool:
    """Selecting an existing GeoPackage layer (the dropdown path) appends to it."""
    a = _write_temp("sct_test_tgt_a.csv", _PLOUGH_FILE)
    b = _write_temp("sct_test_tgt_b.csv", _PLOUGH_FILE)
    gpkg = _fresh_gpkg("sct_test_target.gpkg")
    name = clp.prefixed_layer_name(gpkg, "plough_data")
    extra = {"START_DATE": "2024-01-01"}
    try:
        # Populate the layer first via the GeoPackage option.
        first = _run(ImportPloughDataAlgorithm(), [a], gpkg, extra)
        if first is None or first.featureCount() != 2:
            return _result("target-layer dropdown append", False, "initial populate failed")
        # Append a second file by selecting the existing layer (passed as its URI,
        # which is how parameterAsVectorLayer resolves a chosen layer).
        alg = ImportPloughDataAlgorithm()
        alg.initAlgorithm()
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        alg.processAlgorithm(
            {"INPUT": [b], "TARGET_LAYER": clp.gpkg_layer_uri(gpkg, name), **extra},
            context,
            QgsProcessingFeedback(),
        )
        result = _open(gpkg, name)
        ok = result is not None and result.featureCount() == 4
    except Exception as exc:
        return _result("target-layer dropdown append", False, repr(exc))
    return _result(
        "target-layer dropdown append",
        ok,
        f"count={None if result is None else result.featureCount()} (expected 4)",
    )


def run_all() -> List[bool]:
    results = [
        test_parse_dms(),
        test_parse_day_time(),
        test_type_inference(),
        test_merge_and_dedupe(),
        test_setup_geopackage(),
        test_slack_importer(),
        test_body_importer(),
        test_cable_lay_importer(),
        test_multi_file_and_append_dedupe(),
        test_target_layer_dropdown_append(),
    ]
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()
