# -*- coding: utf-8 -*-
"""End-to-end checks for KP Range Highlighter from CSV.

Focus: rows whose start and end KP are the same describe a position, not a
range. They are written to the algorithm's second output as points on the
reference line rather than being rejected as zero-length segments, with the
same attributes as the range output, from both the pasted-text and the
table-layer input paths.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
)

from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING
from ..processing.kp_range_csv_algorithm import KPRangeCSVAlgorithm

# 0.1 degrees of longitude at 50 N is ~7.16 km, so every KP used here is
# comfortably inside the reference line.
_LAT = 50.0
_LON_END = 0.1


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _route_layer() -> QgsVectorLayer:
    layer = QgsVectorLayer("LineString?crs=EPSG:4326", "route", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromPolylineXY(
        [QgsPointXY(0.0, _LAT), QgsPointXY(_LON_END, _LAT)]))
    layer.dataProvider().addFeatures([feature])
    QgsProject.instance().addMapLayer(layer)
    return layer


def _run(parameters):
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    algorithm = KPRangeCSVAlgorithm()
    algorithm.initAlgorithm({})
    parameters = dict(parameters)
    parameters.setdefault("OUTPUT", "memory:")
    parameters.setdefault("OUTPUT_POINTS", "memory:")
    results, ok = algorithm.run(parameters, context, feedback)
    if not ok:
        return None, None
    return (_layer(context, results.get("OUTPUT")),
            _layer(context, results.get("OUTPUT_POINTS")))


def _layer(context, value):
    return context.takeResultLayer(value) if isinstance(value, str) else value


def _values(layer, field):
    return [feature[field] for feature in layer.getFeatures()]


def test_equal_kps_become_points() -> bool:
    route = _route_layer()
    pasted = "\n".join([
        "Start KP\tEnd KP\tLabel",
        "1.0\t2.0\tRange one",
        "3.5\t3.5\tSingle position",
        "5.0\t5.0\tAnother position",
        "6.0\t7.0\tRange two",
    ])
    ranges, points = _run({"INPUT_LINE": route, "PASTED_RANGES": pasted})
    ok = ranges is not None and points is not None
    if not ok:
        return _result("equal start/end KP become points", False, "algorithm failed")
    ok = ranges.featureCount() == 2 and points.featureCount() == 2
    ok = ok and sorted(_values(points, "label")) == [
        "Another position", "Single position"]
    ok = ok and sorted(_values(ranges, "label")) == ["Range one", "Range two"]
    # Carried columns and the source fields are identical in both outputs.
    ok = ok and ranges.fields().names() == points.fields().names()
    # The point sits on the line at its KP: 3.5 km of ~7.16 km.
    single = next(f for f in points.getFeatures() if f["label"] == "Single position")
    xy = single.geometry().asPoint()
    ok = ok and abs(xy.y() - _LAT) < 1e-3 and 0.048 < xy.x() < 0.050
    ok = ok and single["start_kp"] == 3.5 and single["end_kp"] == 3.5
    QgsProject.instance().removeMapLayer(route.id())
    return _result("equal start/end KP become points", ok)


def test_point_tolerance() -> bool:
    """A tolerance sends near-zero-length ranges to the point output too."""
    route = _route_layer()
    ranges, points = _run({
        "INPUT_LINE": route,
        "PASTED_RANGES": "1.0\t1.0002\tNearly a point",
        "POINT_TOLERANCE": 1.0,          # 0.2 m span, inside 1 m
    })
    ok = ranges is not None and ranges.featureCount() == 0
    ok = ok and points is not None and points.featureCount() == 1

    # Without the tolerance the same row stays a (very short) range.
    ranges2, points2 = _run({
        "INPUT_LINE": route,
        "PASTED_RANGES": "1.0\t1.0002\tNearly a point",
    })
    ok = ok and ranges2 is not None and ranges2.featureCount() == 1
    ok = ok and points2 is not None and points2.featureCount() == 0
    QgsProject.instance().removeMapLayer(route.id())
    return _result("point tolerance widens what counts as a position", ok)


def test_table_layer_input() -> bool:
    route = _route_layer()
    table = QgsVectorLayer("None", "kp ranges", "memory")
    table.dataProvider().addAttributes([
        QgsField("start", FIELD_TYPE_DOUBLE),
        QgsField("end", FIELD_TYPE_DOUBLE),
        QgsField("label", FIELD_TYPE_STRING),
    ])
    table.updateFields()
    rows = [(0.5, 1.5, "Range"), (2.0, 2.0, "Position"), (4.0, 4.0, "Position 2")]
    features = []
    for start, end, label in rows:
        feature = QgsFeature(table.fields())
        feature.setAttributes([start, end, label])
        features.append(feature)
    table.dataProvider().addFeatures(features)
    QgsProject.instance().addMapLayer(table)

    ranges, points = _run({
        "INPUT_LINE": route, "INPUT_LAYER": table,
        "START_KP_FIELD": "start", "END_KP_FIELD": "end",
    })
    ok = ranges is not None and ranges.featureCount() == 1
    ok = ok and points is not None and points.featureCount() == 2
    ok = ok and sorted(_values(points, "label")) == ["Position", "Position 2"]
    QgsProject.instance().removeMapLayer(table.id())
    QgsProject.instance().removeMapLayer(route.id())
    return _result("table-layer input handles positions too", ok)


def run_all():
    return [
        test_equal_kps_become_points(),
        test_point_tolerance(),
        test_table_layer_input(),
    ]


def main() -> int:
    results = run_all()
    failures = results.count(False)
    print(f"{len(results) - failures}/{len(results)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
