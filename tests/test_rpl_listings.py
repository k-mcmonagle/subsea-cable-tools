# -*- coding: utf-8 -*-
"""Headless checks for Identify RPL Crossing Points / Identify RPL Area Listing.

Synthetic data in UTM 31N (metres), so KPs have closed forms. Includes the
regression for intersections that come back as a GeometryCollection (a
crossing plus an overlapping stretch, or a route through an area that also
touches its corner): both algorithms used to call the non-existent
``QgsWkbTypes.isGeometryCollection`` there and crashed with AttributeError.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

from typing import List

from qgis.core import (
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..qgis_compat import FIELD_TYPE_STRING
from ..processing.identify_rpl_area_listing_algorithm import IdentifyRPLAreaListingAlgorithm
from ..processing.identify_rpl_crossing_points_algorithm import IdentifyRPLCrossingPointsAlgorithm

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


def _layer(kind: str, name: str, geoms, attrs=None) -> QgsVectorLayer:
    layer = QgsVectorLayer(f"{kind}?crs={_CRS}", name, "memory")
    if attrs is not None:
        layer.dataProvider().addAttributes([QgsField("name", FIELD_TYPE_STRING)])
        layer.updateFields()
    feats = []
    for i, geom in enumerate(geoms):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        if attrs is not None:
            feat.setAttributes([attrs[i]])
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(layer)
    return layer


def _route() -> QgsVectorLayer:
    """Eastbound 20 km route along y=0, split into two features at 10 km."""
    return _layer("LineString", "rpl", [
        QgsGeometry.fromPolylineXY([_xy(0, 0), _xy(10000, 0)]),
        QgsGeometry.fromPolylineXY([_xy(10000, 0), _xy(20000, 0)]),
    ])


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
    layers = []
    for key in outputs:
        value = results.get(key)
        layers.append(context.takeResultLayer(value) if isinstance(value, str) else value)
    return layers


def _cleanup(*layers):
    for layer in layers:
        QgsProject.instance().removeMapLayer(layer.id())


def test_crossings_kp_angle_and_lat_lon() -> bool:
    route = _route()
    assets = _layer("LineString", "assets", [
        QgsGeometry.fromPolylineXY([_xy(3000, -500), _xy(3000, 500)]),      # 90 deg at KP 3
        QgsGeometry.fromPolylineXY([_xy(14000, -500), _xy(15000, 500)]),    # 45 deg at KP 14.5
    ], attrs=["A", "B"])
    out = _run(IdentifyRPLCrossingPointsAlgorithm(), {
        "INPUT_RPL": route, "INPUT_ASSETS": [assets],
    }, ["OUTPUT"])
    ok = out is not None and out[0] is not None
    detail = ""
    if ok:
        rows = sorted((f["kp"], f["cross_ang"], f["asset_name"], f["lat"], f["lon"])
                      for f in out[0].getFeatures())
        detail = f"rows={[(r[0], r[1], r[2]) for r in rows]}"
        # KP is geodesic along a UTM line: within 0.1% of the grid distance.
        ok = (len(rows) == 2
              and abs(rows[0][0] - 3.0) < 3.0e-3 and abs(rows[0][1] - 90.0) < 0.01
              and rows[0][2] == "A"
              and abs(rows[1][0] - 14.5) < 14.5e-3 and abs(rows[1][1] - 45.0) < 0.01
              and rows[1][2] == "B"
              and all(r[3] is not None and 54.0 < r[3] < 54.3 for r in rows))
    _cleanup(route, assets)
    return _result("crossing points: KP, angle, lat/lon on a two-feature RPL", ok, detail)


def test_crossings_geometry_collection_regression() -> bool:
    """A crossing plus an overlapping stretch: GeometryCollection result."""
    route = _route()
    asset_geom = QgsGeometry.fromPolylineXY([
        _xy(5000, -500), _xy(5000, 500),      # crosses the route at KP 5
        _xy(12000, 500), _xy(12000, 0),       # comes down onto the route ...
        _xy(18000, 0),                        # ... and runs along it (overlap)
    ])
    assets = _layer("LineString", "assets_gc", [asset_geom], attrs=["overlap"])
    route_geom = QgsGeometry.fromPolylineXY([_xy(0, 0), _xy(10000, 0), _xy(20000, 0)])
    inter = route_geom.intersection(asset_geom)
    is_collection = QgsWkbTypes.flatType(inter.wkbType()) == QgsWkbTypes.GeometryCollection
    try:
        out = _run(IdentifyRPLCrossingPointsAlgorithm(), {
            "INPUT_RPL": route, "INPUT_ASSETS": [assets],
        }, ["OUTPUT"])
        error = ""
    except AttributeError as exc:  # the old isGeometryCollection crash
        out, error = None, repr(exc)
    ok = is_collection and out is not None and out[0] is not None
    kps = []
    if ok:
        kps = sorted(round(f["kp"], 1) for f in out[0].getFeatures())
        # The true crossing at KP 5 is listed; the overlap is not a crossing.
        ok = 5.0 in kps
    _cleanup(route, assets)
    return _result("crossing points: GeometryCollection intersection no longer crashes",
                   ok, f"collection={is_collection} kps={kps} {error}")


def test_area_listing_kp_ranges() -> bool:
    route = _route()
    areas = _layer("Polygon", "areas", [
        QgsGeometry.fromPolygonXY([[
            _xy(2000, -1000), _xy(4000, -1000), _xy(4000, 1000), _xy(2000, 1000), _xy(2000, -1000)]]),
        QgsGeometry.fromPolygonXY([[
            _xy(9000, -1000), _xy(11000, -1000), _xy(11000, 1000), _xy(9000, 1000), _xy(9000, -1000)]]),
    ], attrs=["Z1", "Z2"])
    out = _run(IdentifyRPLAreaListingAlgorithm(), {
        "INPUT_RPL": route, "INPUT_AREAS": [areas],
    }, ["OUTPUT"])
    ok = out is not None and out[0] is not None
    detail = ""
    if ok:
        rows = sorted((f["start_kp"], f["end_kp"], f["area_name"]) for f in out[0].getFeatures())
        detail = f"rows={rows}"
        # Z2 straddles the feature join at KP 10: one section per RPL feature,
        # with KP continuous across the join.
        ok = (len(rows) == 3
              and abs(rows[0][0] - 2.0) < 0.005 and abs(rows[0][1] - 4.0) < 0.005 and rows[0][2] == "Z1"
              and abs(rows[1][0] - 9.0) < 0.012 and abs(rows[1][1] - 10.0) < 0.012
              and abs(rows[2][0] - 10.0) < 0.012 and abs(rows[2][1] - 11.0) < 0.012
              and rows[1][2] == rows[2][2] == "Z2")
    _cleanup(route, areas)
    return _result("area listing: KP ranges per area and across a feature join", ok, detail)


def test_area_listing_geometry_collection_regression() -> bool:
    """Route runs through an area, leaves it, then touches one of its corners."""
    line = QgsGeometry.fromPolylineXY([
        _xy(-5000, 0), _xy(5000, 0),          # enters the square at KP 5
        _xy(15000, 10000),                    # leaves it at (10000, 5000)
        _xy(10000, 10000),                    # ends on its top-right corner
    ])
    # Square 0..10000 x -5000..10000: intersection = the stretch inside (a
    # line) + the corner touch (a point) = GeometryCollection.
    square = QgsGeometry.fromPolygonXY([[
        _xy(0, -5000), _xy(10000, -5000), _xy(10000, 10000), _xy(0, 10000), _xy(0, -5000)]])
    inter = line.intersection(square)
    is_collection = QgsWkbTypes.flatType(inter.wkbType()) == QgsWkbTypes.GeometryCollection
    route = _layer("LineString", "rpl_touch", [line])
    areas = _layer("Polygon", "square", [square], attrs=["S"])
    try:
        out = _run(IdentifyRPLAreaListingAlgorithm(), {
            "INPUT_RPL": route, "INPUT_AREAS": [areas],
        }, ["OUTPUT"])
        error = ""
    except AttributeError as exc:
        out, error = None, repr(exc)
    ok = is_collection and out is not None and out[0] is not None
    rows = []
    if ok:
        rows = sorted((round(f["start_kp"], 2), round(f["end_kp"], 2)) for f in out[0].getFeatures())
        # The stretch inside is listed: KP 5 to 5 + 5 + 5*sqrt(2) = 17.07;
        # the corner touch is a point, not a section.
        ok = len(rows) == 1 and abs(rows[0][0] - 5.0) < 0.01 and abs(rows[0][1] - 17.07) < 0.02
    _cleanup(route, areas)
    return _result("area listing: GeometryCollection intersection no longer crashes",
                   ok, f"collection={is_collection} rows={rows} {error}")


def run_all() -> List[bool]:
    return [
        test_crossings_kp_angle_and_lat_lon(),
        test_crossings_geometry_collection_regression(),
        test_area_listing_kp_ranges(),
        test_area_listing_geometry_collection_regression(),
    ]


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(0 if all(run_all()) else 1)
