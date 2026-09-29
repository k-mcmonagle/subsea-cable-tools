# -*- coding: utf-8 -*-
"""Headless checks for the KP-producing point tools.

Nearest KP, Translate KP Between RPLs, Compare Design vs As-Laid Routes and
Place KP Points from CSV all measure KP with the plugin's one KP definition
(``kp_geo_utils.RouteFrame``: geodesic segment lengths, positions along the
stored segments, features in SeqNo order when present). The checks use
synthetic routes with closed-form answers and compare against RouteFrame
directly, plus the sign conventions: DCC / cross-track are positive to
starboard (right of increasing KP).

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import math
from typing import List

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
)

from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_STRING
from ..processing.nearest_kp_algorithm import NearestKPAlgorithm
from ..processing.place_kp_points_from_csv_algorithm import PlaceKpPointsFromCsvAlgorithm
from ..processing.rpl_route_comparison_algorithm import RPLRouteComparisonAlgorithm
from ..processing.translate_kp_from_rpl_to_rpl_algorithm import TranslateKPFromRPLToRPLAlgorithm

_CRS = "EPSG:32631"
_X0, _Y0 = 500000.0, 6000000.0


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _xy(dx, dy):
    return QgsPointXY(_X0 + dx, _Y0 + dy)


def _layer(kind, name, geoms, fields=(), attrs=None, crs=_CRS):
    layer = QgsVectorLayer(f"{kind}?crs={crs}", name, "memory")
    if fields:
        layer.dataProvider().addAttributes(list(fields))
        layer.updateFields()
    feats = []
    for i, geom in enumerate(geoms):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        if attrs is not None:
            feat.setAttributes(list(attrs[i]))
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(layer)
    return layer


def _run(algorithm, parameters, outputs):
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    algorithm.initAlgorithm({})
    params = dict(parameters)
    for key in outputs:
        params.setdefault(key, "memory:")
    results, ok = algorithm.run(params, context, feedback)
    if not ok:
        return None
    out = []
    for key in outputs:
        value = results.get(key)
        out.append(context.takeResultLayer(value) if isinstance(value, str) else value)
    return out


def _cleanup(*layers):
    for layer in layers:
        QgsProject.instance().removeMapLayer(layer.id())


def _route_layer(with_seqno=False):
    """20 km eastbound route along y=0 as two 10 km features.

    With ``with_seqno`` the features are stored second-half first, with a
    SeqNo field giving the true order.
    """
    first = QgsGeometry.fromPolylineXY([_xy(0, 0), _xy(10000, 0)])
    second = QgsGeometry.fromPolylineXY([_xy(10000, 0), _xy(20000, 0)])
    if not with_seqno:
        return _layer("LineString", "route", [first, second])
    return _layer("LineString", "route_seq", [second, first],
                  fields=[QgsField("SeqNo", FIELD_TYPE_INT)], attrs=[[2], [1]])


def _distance():
    return make_distance_area(QgsCoordinateReferenceSystem(_CRS))


def test_nearest_kp_values_and_side() -> bool:
    route = _route_layer()
    pts = _layer("Point", "pts", [QgsGeometry.fromPointXY(_xy(1500, 100)),
                                  QgsGeometry.fromPointXY(_xy(12500, -250))],
                 fields=[QgsField("name", FIELD_TYPE_STRING)], attrs=[["north"], ["south"]])
    out = _run(NearestKPAlgorithm(), {
        "INPUT_POINTS": pts, "INPUT_PATHS": route, "ADD_POINT_ON_LINE": True, "DISTANCE_MODE": 0,
    }, ["OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POINT_ON_LINE"])
    ok = out is not None and all(layer is not None for layer in out)
    detail = ""
    if ok:
        frame = RouteFrame.from_source(
            [QgsGeometry.fromPolylineXY([_xy(0, 0), _xy(10000, 0)]),
             QgsGeometry.fromPolylineXY([_xy(10000, 0), _xy(20000, 0)])], _distance())
        rows = {f["name"]: f for f in out[0].getFeatures()}
        north, south = rows["north"], rows["south"]
        exp_n = frame.kp_at_point(_xy(1500, 100))
        exp_s = frame.kp_at_point(_xy(12500, -250))
        ok = (abs(north["kp_km"] - round(exp_n.kp_km, 3)) < 1e-9
              and abs(south["kp_km"] - round(exp_s.kp_km, 3)) < 1e-9
              and abs(north["kp_km"] - 1.5) < 0.002 and abs(south["kp_km"] - 12.5) < 0.006
              # North of an eastbound route is port (negative), south starboard.
              and north["distance_to_path_m"] < 0 < south["distance_to_path_m"]
              and abs(abs(north["distance_to_path_m"]) - exp_n.dcc_m) < 1e-3
              and north["path_id"] != south["path_id"])
        snapped = {f["name"]: f.geometry().asPoint() for f in out[2].getFeatures()}
        ok = ok and abs(snapped["north"].y() - _Y0) < 1e-6 and abs(snapped["north"].x() - (_X0 + 1500)) < 1e-6
        ok = ok and out[1].featureCount() == 2
        detail = (f"north kp={north['kp_km']} dcc={north['distance_to_path_m']}; "
                  f"south kp={south['kp_km']} dcc={south['distance_to_path_m']}")
    _cleanup(route, pts)
    return _result("nearest KP: RouteFrame KP, signed DCC, snapped point", ok, detail)


def test_nearest_kp_seqno_order_and_crs() -> bool:
    """KP follows SeqNo, not storage order; points in another CRS are reprojected."""
    route = _route_layer(with_seqno=True)
    to_wgs = QgsCoordinateTransform(QgsCoordinateReferenceSystem(_CRS),
                                    QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    pts = _layer("Point", "pts_wgs", [QgsGeometry.fromPointXY(to_wgs.transform(_xy(4000, 50)))],
                 crs="EPSG:4326")
    out = _run(NearestKPAlgorithm(), {
        "INPUT_POINTS": pts, "INPUT_PATHS": route, "DISTANCE_MODE": 0,
    }, ["OUTPUT_POINTS", "OUTPUT_LINES"])
    ok = out is not None and out[0] is not None
    kp = None
    if ok:
        feat = next(out[0].getFeatures())
        kp = feat["kp_km"]
        # Stored second-half first: layer order would give KP ~14.
        ok = abs(kp - 4.0) < 0.003 and abs(abs(feat["distance_to_path_m"]) - 50.0) < 0.1
    _cleanup(route, pts)
    return _result("nearest KP: SeqNo order and CRS reprojection", ok, f"kp={kp}")


def test_translate_kp_matches_route_frame() -> bool:
    route = _route_layer()
    pts = _layer("Point", "aslaid", [QgsGeometry.fromPointXY(_xy(7000, -30)),
                                     QgsGeometry.fromPointXY(_xy(19000, 12))],
                 fields=[QgsField("PosNo", FIELD_TYPE_INT)], attrs=[[1], [2]])
    out = _run(TranslateKPFromRPLToRPLAlgorithm(), {
        "INPUT_SOURCE_LINE": route, "INPUT_TARGET_POINTS": pts,
    }, ["OUTPUT_POINTS"])
    ok = out is not None and out[0] is not None
    rows = []
    if ok:
        rows = sorted((f["PosNo"], f["design_route_kp"], f["design_route_dcc"], f["design_route_ref"])
                      for f in out[0].getFeatures())
        ok = (len(rows) == 2 and abs(rows[0][1] - 7.0) < 0.004 and abs(rows[0][2] - 30.0) < 0.05
              and abs(rows[1][1] - 19.0) < 0.009 and abs(rows[1][2] - 12.0) < 0.05
              and rows[0][3] == "route")
    _cleanup(route, pts)
    return _result("translate KP: design KP and DCC per point", ok, f"rows={rows}")


def test_route_comparison_offsets_and_signs() -> bool:
    """As-laid event 40 m south (starboard) and 25 m ahead of its design event."""
    design_lines = _route_layer()
    aslaid_lines = _layer("LineString", "aslaid_lines", [
        QgsGeometry.fromPolylineXY([_xy(0, -40), _xy(20000, -40)])])
    fields = [QgsField("Event", FIELD_TYPE_STRING), QgsField("DistCumulative", FIELD_TYPE_DOUBLE)]
    design_pts = _layer("Point", "design_pts", [QgsGeometry.fromPointXY(_xy(5000, 0)),
                                                QgsGeometry.fromPointXY(_xy(15000, 0))],
                        fields=fields, attrs=[["RPT1", 5.0], ["RPT2", 15.0]])
    aslaid_pts = _layer("Point", "aslaid_pts", [QgsGeometry.fromPointXY(_xy(5025, -40)),
                                                QgsGeometry.fromPointXY(_xy(14990, 60))],
                        fields=fields, attrs=[["rpt1", 5.0], ["RPT2 ", 15.0]])
    out = _run(RPLRouteComparisonAlgorithm(), {
        "DESIGN_POINTS": design_pts, "DESIGN_EVENTS_FIELD": "Event",
        "DESIGN_KP_FIELD": "DistCumulative", "DESIGN_LINES": design_lines,
        "ASLAID_POINTS": aslaid_pts, "ASLAID_EVENTS_FIELD": "Event", "ASLAID_LINES": aslaid_lines,
    }, ["OUTPUT_COMPARISON"])
    ok = out is not None and out[0] is not None
    rows = {}
    if ok:
        rows = {f["design_event"]: (f["along_track_m"], f["cross_track_m"], f["radial_distance_m"])
                for f in out[0].getFeatures()}
        r1, r2 = rows.get("RPT1"), rows.get("RPT2")
        # Geodesic metres on UTM grid metres: allow the ~0.04% scale factor.
        ok = (r1 is not None and r2 is not None
              and abs(r1[0] - 25.0) < 0.05 and abs(r1[1] - 40.0) < 0.05          # ahead, starboard
              and abs(r2[0] + 10.0) < 0.05 and abs(r2[1] + 60.0) < 0.05          # behind, port
              and abs(r1[2] - math.hypot(25.0, 40.0)) < 0.05)
    _cleanup(design_lines, aslaid_lines, design_pts, aslaid_pts)
    return _result("route comparison: along/cross-track values and signs", ok, f"rows={rows}")


def test_place_kp_points_from_csv() -> bool:
    route = _route_layer()
    pasted = "KP\tLabel\n2.5\ta\n12.25\tb\n-1\tbefore start\n25\tpast end"
    out = _run(PlaceKpPointsFromCsvAlgorithm(), {
        "INPUT_LINE": route, "PASTED_KPS": pasted, "DISTANCE_MODE": 0,
    }, ["OUTPUT"])
    ok = out is not None and out[0] is not None
    rows = []
    if ok:
        # The two features join into one line (0, 10, 20 km vertices).
        frame = RouteFrame.from_source(
            QgsGeometry.fromPolylineXY([_xy(0, 0), _xy(10000, 0), _xy(20000, 0)]), _distance())
        rows = sorted((f["kp_value"], f["label"], f.geometry().asPoint()) for f in out[0].getFeatures())
        ok = [r[1] for r in rows] == ["a", "b"]   # negative and past-the-end KPs are skipped
        for kp, _label, point in rows:
            expected = frame.point_at_kp(kp)
            ok = ok and abs(point.x() - expected.x()) < 1e-6 and abs(point.y() - expected.y()) < 1e-6
    _cleanup(route)
    return _result("place KP points from CSV: RouteFrame positions, out-of-range skipped",
                   ok, f"rows={[(r[0], r[1]) for r in rows]}")


def run_all() -> List[bool]:
    return [
        test_nearest_kp_values_and_side(),
        test_nearest_kp_seqno_order_and_crs(),
        test_translate_kp_matches_route_frame(),
        test_route_comparison_offsets_and_signs(),
        test_place_kp_points_from_csv(),
    ]


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(0 if all(run_all()) else 1)
