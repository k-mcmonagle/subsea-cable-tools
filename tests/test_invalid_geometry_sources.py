"""Regression checks: processing algorithms must survive invalid input geometries.

The Processing framework wraps every ``parameterAsSource`` input in a
``QgsProcessingFeatureSource`` that applies the user's "Invalid features
filtering" setting. The QGIS default aborts the algorithm on the first
GEOS-invalid feature (e.g. a zero-length line), and the "skip" setting
silently drops such features — shifting every KP beyond the gap. The KP
tools measure along linework and never rely on OGC validity, so they read
features via ``kp_geo_utils.get_features_skip_invalid`` instead (mirroring
the KP mouse map tool, which reads the raw layer and is unaffected).

These tests run representative algorithms with a context configured to
abort on invalid geometries and a layer containing a valid line, a
degenerate (GEOS-invalid) line, and a null geometry.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

from typing import List

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
)

from ..kp_geo_utils import get_features_skip_invalid
from ..processing.place_kp_points_algorithm import PlaceKpPointsAlgorithm
from ..processing.place_single_kp_point_algorithm import PlaceSingleKpPointAlgorithm
from ..processing.kp_range_highlighter_algorithm import KPRangeHighlighterAlgorithm


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _abort_on_invalid_context() -> QgsProcessingContext:
    """A context matching the Processing GUI default (abort on invalid)."""
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    try:
        check = Qgis.InvalidGeometryCheck.AbortOnInvalid
    except AttributeError:  # QGIS 3 pre-scoped enum
        check = QgsFeatureRequest.GeometryAbortOnInvalid
    context.setInvalidGeometryCheck(check)
    return context


def _make_dirty_line_layer() -> QgsVectorLayer:
    """One valid line (~5.5 km), one degenerate line, one null geometry."""
    layer = QgsVectorLayer("LineString?crs=EPSG:4326", "dirty_route", "memory")
    good = QgsFeature()
    good.setGeometry(QgsGeometry.fromWkt("LINESTRING (0 0, 0 0.05)"))
    degenerate = QgsFeature()
    degenerate.setGeometry(QgsGeometry.fromWkt("LINESTRING (5 5, 5 5)"))
    null_geom = QgsFeature()
    layer.dataProvider().addFeatures([good, degenerate, null_geom])
    return layer


def test_degenerate_line_is_geos_invalid() -> bool:
    """Sanity: the fixture really is invalid to the framework's check."""
    bad = QgsGeometry.fromWkt("LINESTRING (5 5, 5 5)")
    return _result(
        "zero-length line is GEOS-invalid (fixture is representative)",
        not bad.isGeosValid(),
    )


def test_helper_returns_all_features() -> bool:
    layer = _make_dirty_line_layer()
    alg = PlaceKpPointsAlgorithm()
    alg.initAlgorithm()
    context = _abort_on_invalid_context()
    source = alg.parameterAsSource({"INPUT_LINE": layer}, "INPUT_LINE", context)
    feats = list(get_features_skip_invalid(source))
    return _result(
        "get_features_skip_invalid bypasses abort-on-invalid filtering",
        len(feats) == 3,
        f"got {len(feats)}/3 features",
    )


def test_place_kp_points_with_invalid_feature() -> bool:
    layer = _make_dirty_line_layer()
    alg = PlaceKpPointsAlgorithm()
    alg.initAlgorithm()
    params = {
        "INPUT_LINE": layer,
        "INTERVAL_1KM": True,
        "INTERVAL_50KM": False,
        "INTERVAL_100KM": False,
        "OUTPUT": "memory:",
        "DISTANCE_MODE": 0,
    }
    try:
        res = alg.processAlgorithm(params, _abort_on_invalid_context(), QgsProcessingFeedback())
        ok = res.get("OUTPUT") is not None
        detail = "" if ok else "no output produced"
    except Exception as exc:  # the pre-fix behaviour raised/aborted here
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return _result("Place KP Points runs despite invalid input feature", ok, detail)


def test_place_single_kp_point_with_invalid_feature() -> bool:
    layer = _make_dirty_line_layer()
    alg = PlaceSingleKpPointAlgorithm()
    alg.initAlgorithm()
    params = {
        "INPUT_LINE": layer,
        "KP_VALUE": 2.0,
        "OUTPUT": "memory:",
        "DISTANCE_MODE": 0,
    }
    try:
        res = alg.processAlgorithm(params, _abort_on_invalid_context(), QgsProcessingFeedback())
        ok = res.get("OUTPUT") is not None
        detail = "" if ok else "no output produced"
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return _result("Place Single KP Point runs despite invalid input feature", ok, detail)


def test_kp_range_highlighter_with_invalid_feature() -> bool:
    layer = _make_dirty_line_layer()
    alg = KPRangeHighlighterAlgorithm()
    alg.initAlgorithm()
    params = {
        "INPUT": layer,
        "START_KP": 0.5,
        "END_KP": 2.5,
        "OUTPUT": "memory:",
        "DISTANCE_MODE": 0,
    }
    try:
        res = alg.processAlgorithm(params, _abort_on_invalid_context(), QgsProcessingFeedback())
        ok = res.get("OUTPUT") is not None
        detail = "" if ok else "no output produced"
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return _result("KP Range Highlighter runs despite invalid input feature", ok, detail)


def run_all() -> List[bool]:
    results = [
        test_degenerate_line_is_geos_invalid(),
        test_helper_returns_all_features(),
        test_place_kp_points_with_invalid_feature(),
        test_place_single_kp_point_with_invalid_feature(),
        test_kp_range_highlighter_with_invalid_feature(),
    ]
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()
