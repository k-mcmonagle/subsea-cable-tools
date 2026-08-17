# -*- coding: utf-8 -*-
"""Checks for the route-line → RPL revision import (KML/any line layer).

Covers the pure model builder (vertex → position mapping, zero slack,
computed distances), line-feature extraction (selection rules, CRS
transform, multipart refusal, duplicate-vertex dedupe), a KML file round
trip, and end-to-end registration through the shared commit path.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer

from ..workbench.rpl_from_line import (
    RouteLineError,
    load_line_file,
    model_from_lonlat,
    vertices_lonlat,
)
from ..workbench.rpl_import_service import CommitRequest, commit_import
from ..workbench.store import WorkbenchStore


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _memory_line(coords, crs="EPSG:4326", extra_feature=None):
    layer = QgsVectorLayer(f"LineString?crs={crs}", "route", "memory")
    provider = layer.dataProvider()
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in coords]))
    features = [feature]
    if extra_feature is not None:
        other = QgsFeature()
        other.setGeometry(QgsGeometry.fromPolylineXY(
            [QgsPointXY(x, y) for x, y in extra_feature]))
        features.append(other)
    provider.addFeatures(features)
    layer.updateExtents()
    return layer


def test_model_from_lonlat() -> bool:
    coords = [(0.0, 50.0), (0.1, 50.0), (0.2, 50.05)]
    model = model_from_lonlat(coords)
    ok = len(model.points) == 3 and len(model.segments) == 2
    ok = ok and model.points[0].event == "A End" and model.points[-1].event == "B End"
    ok = ok and model.points[1].event == ""
    ok = ok and [p.pos_no for p in model.points] == [1, 2, 3]
    ok = ok and all(seg.slack_pct == 0.0 for seg in model.segments)
    ok = ok and all((seg.dist_km or 0) > 0 for seg in model.segments)
    # zero slack: cable distance equals route distance
    ok = ok and all(abs((seg.cable_dist_km or 0) - (seg.dist_km or 0)) < 1e-9
                    for seg in model.segments)
    kps = [p.dist_cum_km for p in model.points]
    ok = ok and kps[0] == 0.0 and kps == sorted(kps) and kps[-1] > 10.0
    return _result("model from lonlat: positions, zero slack, cumulatives",
                   ok, f"total={kps[-1]:.3f} km")


def test_vertices_extraction_rules() -> bool:
    # duplicate consecutive vertices collapse
    layer = _memory_line([(0.0, 50.0), (0.1, 50.0), (0.1, 50.0), (0.2, 50.0)])
    coords = vertices_lonlat(layer)
    ok = coords == [(0.0, 50.0), (0.1, 50.0), (0.2, 50.0)]

    # two features with no selection is ambiguous
    two = _memory_line([(0.0, 50.0), (0.1, 50.0)],
                       extra_feature=[(1.0, 51.0), (1.1, 51.0)])
    try:
        vertices_lonlat(two)
        ok = False
    except RouteLineError:
        pass
    # ...but selecting one resolves it
    first_id = next(two.getFeatures()).id()
    two.selectByIds([first_id])
    ok = ok and vertices_lonlat(two) == [(0.0, 50.0), (0.1, 50.0)]
    return _result("vertex extraction: dedupe + selection rules", ok)


def test_crs_transform() -> bool:
    # Web-Mercator route reprojects onto WGS84 lon/lat
    layer = _memory_line([(0.0, 6446275.84), (11131.95, 6446275.84)], crs="EPSG:3857")
    coords = vertices_lonlat(layer)
    ok = len(coords) == 2
    ok = ok and abs(coords[0][0] - 0.0) < 1e-6 and abs(coords[0][1] - 50.0) < 1e-6
    ok = ok and abs(coords[1][0] - 0.1) < 1e-6
    return _result("projected line reprojects to WGS84", ok,
                   f"coords={[(round(x, 4), round(y, 4)) for x, y in coords]}")


def test_kml_round_trip_and_commit() -> bool:
    folder = tempfile.mkdtemp(prefix="wb_lineimport_test_")
    kml_path = os.path.join(folder, "route.kml")
    with open(kml_path, "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            '<Placemark><name>Route</name><LineString><coordinates>'
            '0.0,50.0,0 0.1,50.0,0 0.2,50.05,0'
            '</coordinates></LineString></Placemark>'
            '</Document></kml>\n')

    layer = load_line_file(kml_path)
    coords = vertices_lonlat(layer)
    model = model_from_lonlat(coords)
    ok = len(model.points) == 3

    store = WorkbenchStore(os.path.join(folder, "workbench.gpkg"),
                           QgsProject.instance().transformContext())
    result = commit_import(store, model, CommitRequest(
        route_name="KML Segment", kind="planned", source_file=kml_path,
        audit={"method": "route_line"}))
    row = store.get_rpl(result.rpl_id)
    ok = ok and row is not None and row.get("rev_label") == "Rev 1"
    ok = ok and row.get("route_id") and store.get_route(row["route_id"]) is not None
    points_layer = store.open_layer(row.get("points_layer"))
    lines_layer = store.open_layer(row.get("lines_layer"))
    ok = ok and points_layer is not None and points_layer.featureCount() == 3
    ok = ok and lines_layer is not None and lines_layer.featureCount() == 2

    # a second line import of the same segment becomes Rev 2, superseding Rev 1
    second = commit_import(store, model_from_lonlat(coords), CommitRequest(
        route_name="KML Segment", kind="planned", source_file=kml_path))
    ok = ok and second.rev_label == "Rev 2"
    ok = ok and (store.get_rpl(second.rpl_id) or {}).get("supersedes_id") == result.rpl_id
    return _result("KML round trip registers revisions through commit path",
                   ok, f"rev={result.rev_label} then {second.rev_label}")


def run_all():
    return [
        test_model_from_lonlat(),
        test_vertices_extraction_rules(),
        test_crs_transform(),
        test_kml_round_trip_and_commit(),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(run_all()) else 1)
