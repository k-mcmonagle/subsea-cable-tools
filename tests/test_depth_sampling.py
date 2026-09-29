# -*- coding: utf-8 -*-
"""Checks for processing/depth_sampling.py and the algorithms built on it.

``sample_contours`` now answers each query from one spatial index (with
stored geometries) built per contour layer, instead of a feature scan per
point. The first test compares it against a brute-force scan of every
feature for projected and geographic contour layers, with and without a
search radius. Geographic contour layers also carry a regression: the old
code called the non-existent ``QgsGeometry.closestPoint`` inside a blanket
``except`` and so never returned a depth from a lat/lon contour layer.

Also covers Dynamic Buffer (Lay Corridor) and Add Depth to Point Layer end to
end on synthetic data.

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

from ..kp_range_utils import make_distance_area
from ..qgis_compat import FIELD_TYPE_DOUBLE
from ..processing import depth_sampling
from ..processing.add_depth_to_point_layer_algorithm import AddDepthToPointLayerAlgorithm
from ..processing.dynamic_buffer_lay_corridor_algorithm import DynamicBufferLayCorridorAlgorithm

_UTM = "EPSG:32631"
_X0, _Y0 = 500000.0, 6000000.0


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


class _Lcg:
    def __init__(self, seed=4242):
        self.state = seed

    def rand(self):
        self.state = (1103515245 * self.state + 12345) % (2 ** 31)
        return self.state / float(2 ** 31)


def _to(crs):
    return QgsCoordinateTransform(QgsCoordinateReferenceSystem(_UTM),
                                  QgsCoordinateReferenceSystem(crs), QgsProject.instance())


def _contours(crs=_UTM, vertical=False, with_nulls=True) -> QgsVectorLayer:
    """Wavy contour lines 250 m apart. Every 5th has a NULL depth.

    ``vertical``: north-south lines whose depth grows eastward (0..40 m at
    x < 5 km, 40..160 m to 10 km, deeper beyond), else east-west lines.
    """
    layer = QgsVectorLayer(f"LineString?crs={crs}", "contours", "memory")
    layer.dataProvider().addAttributes([QgsField("depth", FIELD_TYPE_DOUBLE)])
    layer.updateFields()
    xform = _to(crs) if crs != _UTM else None
    feats = []
    for k in range(-4, 81):
        offset = k * 250.0
        if vertical:
            pts = [QgsPointXY(_X0 + offset + 30.0 * math.sin(j), _Y0 - 3000.0 + j * 500.0) for j in range(13)]
            depth = 8.0 * offset / 1000.0 + (0.0 if offset < 10000.0 else 1000.0)
        else:
            pts = [QgsPointXY(_X0 - 3000.0 + j * 500.0, _Y0 + offset + 40.0 * math.sin(j + k)) for j in range(13)]
            depth = 100.0 + k
        if xform is not None:
            pts = [xform.transform(p) for p in pts]
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPolylineXY(pts))
        feat.setAttributes([None if (with_nulls and k % 5 == 0) else depth])
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(layer)
    return layer


def _brute_force(point, layer, radius):
    """The nearest non-NULL contour by scanning every feature (old semantics)."""
    geographic = layer.crs().isGeographic()
    distance = make_distance_area(layer.crs()) if geographic else None
    pt_geom = QgsGeometry.fromPointXY(point)
    best = None
    for feat in layer.getFeatures():
        if feat["depth"] is None:
            continue
        try:
            depth = float(feat["depth"])
        except (TypeError, ValueError):
            continue
        geom = feat.geometry()
        if distance is None:
            dist = geom.distance(pt_geom)
        else:
            dist = distance.measureLine(point, QgsPointXY(geom.nearestPoint(pt_geom).asPoint()))
        if radius > 0 and dist > radius:
            continue
        if best is None or dist < best[0]:
            best = (dist, depth)
    return best


def test_sample_contours_matches_brute_force() -> bool:
    rng = _Lcg()
    mismatches = []
    checked = 0
    found_geo = 0
    for crs in (_UTM, "EPSG:4326"):
        layer = _contours(crs)
        xform = _to(crs) if crs != _UTM else None
        samplers = depth_sampling.build_contour_samplers([layer], ["depth"], layer.crs())
        for radius in (0.0, 180.0):
            for _ in range(120):
                p = QgsPointXY(_X0 - 2000.0 + rng.rand() * 9000.0, _Y0 - 1000.0 + rng.rand() * 20000.0)
                q = xform.transform(p) if xform is not None else p
                depth, _src, dist = depth_sampling.sample_contours(q, samplers, radius)
                expected = _brute_force(q, layer, radius)
                checked += 1
                if expected is None:
                    if depth is not None:
                        mismatches.append((crs, radius, "extra", depth))
                    continue
                if crs != _UTM and depth is not None:
                    found_geo += 1
                if depth != expected[1] or dist is None or abs(dist - expected[0]) > 1e-6:
                    mismatches.append((crs, radius, depth, expected, dist))
        QgsProject.instance().removeMapLayer(layer.id())
    ok = not mismatches and found_geo > 100
    return _result("sample_contours: indexed lookup equals brute-force scan (UTM + lat/lon)", ok,
                   f"{checked} queries, geographic hits={found_geo}, mismatches={mismatches[:3]}")


def _run(algorithm, parameters, outputs):
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    algorithm.initAlgorithm({})
    params = dict(parameters)
    for key in outputs:
        params.setdefault(key, "memory:")
    results, ok = algorithm.run(params, context, QgsProcessingFeedback())
    if not ok:
        return None
    value = results.get(outputs[0])
    return context.takeResultLayer(value) if isinstance(value, str) else value


def _route(crs=_UTM) -> QgsVectorLayer:
    pts = [QgsPointXY(_X0 + x, _Y0 + 200.0) for x in (0.0, 6000.0, 14000.0)]
    if crs != _UTM:
        xform = _to(crs)
        pts = [xform.transform(p) for p in pts]
    layer = QgsVectorLayer(f"LineString?crs={crs}", "route", "memory")
    feat = QgsFeature()
    feat.setGeometry(QgsGeometry.fromPolylineXY(pts))
    layer.dataProvider().addFeatures([feat])
    QgsProject.instance().addMapLayer(layer)
    return layer


def test_dynamic_buffer_depth_rules() -> bool:
    """Depth-based widths from contours, in a UTM and in a lat/lon project."""
    rows = {}
    ok = True
    for crs in (_UTM, "EPSG:4326"):
        route = _route(crs)
        contours = _contours(crs, vertical=True, with_nulls=False)
        layer = _run(DynamicBufferLayCorridorAlgorithm(), {
            "INPUT": route, "MODE": 1, "DEPTH_SOURCE": 2,
            "CONTOUR_LAYER_1": contours, "CONTOUR_DEPTH_FIELD_1": "depth",
            "CONTOUR_SEARCH_RADIUS_M": 400.0, "SAMPLE_INTERVAL_M": 100.0, "DISSOLVE": False,
        }, ["OUTPUT"])
        if layer is None or layer.featureCount() != 1:
            ok = False
            rows[crs] = None
        else:
            feat = next(layer.getFeatures())
            rows[crs] = (feat["buf_min_m"], feat["buf_max_m"], round(feat["depth_ok_pct"], 1))
            # Depth runs 0..~110 m along the route, then >1000 m: the default
            # rules give 5 m (< 25 m) up to 100 m (>= 1000 m); every station
            # finds a contour within the radius.
            ok = ok and rows[crs] == (5.0, 100.0, 100.0)
        QgsProject.instance().removeMapLayer(route.id())
        QgsProject.instance().removeMapLayer(contours.id())
    return _result("dynamic buffer: depth-rule widths from UTM and lat/lon contours", ok, f"{rows}")


def test_add_depth_from_geographic_contours() -> bool:
    """Regression: lat/lon contour layers returned no depth at all."""
    contours = _contours("EPSG:4326")
    xform = _to("EPSG:4326")
    points = QgsVectorLayer("Point?crs=EPSG:4326", "pts", "memory")
    feats = []
    for k in (1, 7, 23):
        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPointXY(xform.transform(QgsPointXY(_X0 + 800.0, _Y0 + k * 250.0 + 60.0))))
        feats.append(feat)
    points.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(points)
    layer = _run(AddDepthToPointLayerAlgorithm(), {
        "INPUT": points, "DEPTH_SOURCE": 2, "CONTOUR_LAYER_1": contours,
        "CONTOUR_DEPTH_FIELD_1": "depth", "CONTOUR_SEARCH_RADIUS_M": 0.0,
    }, ["OUTPUT"])
    depths = [f["depth"] for f in layer.getFeatures()] if layer is not None else []
    # Each point is ~60 m north of contour k (depth 100 + k); none of those is NULL.
    ok = depths == [101.0, 107.0, 123.0]
    QgsProject.instance().removeMapLayer(points.id())
    QgsProject.instance().removeMapLayer(contours.id())
    return _result("add depth: nearest depth from a lat/lon contour layer", ok, f"depths={depths}")


def run_all() -> List[bool]:
    return [
        test_sample_contours_matches_brute_force(),
        test_dynamic_buffer_depth_rules(),
        test_add_depth_from_geographic_contours(),
    ]


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(0 if all(run_all()) else 1)
