# -*- coding: utf-8 -*-
"""Checks for the rules acquisition layer, store migration, and end-to-end run.

Builds a synthetic WGS84 route plus in-memory hazard/soil layers, exercises
each per-kind acquirer, then a full run_assessment round trip against a temp
GeoPackage store. Requires the QGIS API (run via run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile
import time

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..processing.cable_lay_parsers import WKT_KEY
from ..workbench import rules_engine as eng
from ..workbench import rules_inputs as ri
from ..workbench import schema
from ..workbench.store import WorkbenchStore

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
_COUNTER = [0]


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _tmp_gpkg() -> str:
    _COUNTER[0] += 1
    name = f"wb_rules_{os.getpid()}_{int(time.time()*1000)}_{_COUNTER[0]}.gpkg"
    return os.path.join(tempfile.gettempdir(), name)


def _sampler(step_m: float = 100.0) -> ri.RouteSampler:
    da = make_distance_area(WGS84, QgsProject.instance().transformContext())
    geoms = [QgsGeometry.fromWkt("LINESTRING(0 50, 0 50.1)")]
    route = RouteFrame.from_source(geoms, da)
    stations = ri._build_stations(route, step_m)
    coords = [route.point_at_kp(kp, clamp=True) for kp in stations]
    return ri.RouteSampler(route, stations, coords, da)


def _add_layer(defn: str, name: str) -> QgsVectorLayer:
    layer = QgsVectorLayer(defn, name, "memory")
    QgsProject.instance().addMapLayer(layer)
    return layer


def test_manual_and_kp_table() -> bool:
    sampler = _sampler()
    manual = ri._acquire_manual(sampler, {"ranges": [{"start_kp": 2.0, "end_kp": 4.0}]})
    ok = len(manual) == 1 and abs(manual[0].start_km - 2.0) < 1e-6 and abs(manual[0].end_km - 4.0) < 1e-6

    layer = _add_layer("None?field=start_kp:double&field=end_kp:double", "kp_table")
    pr = layer.dataProvider()
    feat = QgsFeature(layer.fields())
    feat.setAttributes([1.0, 3.0])
    pr.addFeature(feat)
    ivs = ri._acquire_kp_table(sampler, {"layer_id": layer.id(), "start_field": "start_kp",
                                         "end_field": "end_kp"}, QgsProject.instance())
    ok = ok and len(ivs) == 1 and abs(ivs[0].end_km - 3.0) < 1e-6
    QgsProject.instance().removeMapLayer(layer.id())
    return _result("manual + kp_range_table acquisition", ok)


def test_polygon_class() -> bool:
    sampler = _sampler()
    layer = _add_layer("Polygon?crs=EPSG:4326&field=SOIL:string", "soils")
    pr = layer.dataProvider()
    feat = QgsFeature(layer.fields())
    feat.setGeometry(QgsGeometry.fromWkt("POLYGON((-0.01 50.02, 0.01 50.02, 0.01 50.04, -0.01 50.04, -0.01 50.02))"))
    feat.setAttributes(["ROCK"])
    pr.addFeature(feat)
    ivs = ri._acquire_polygon_class(sampler, {"layer_id": layer.id(), "attribute": "SOIL",
                                              "match_values": ["ROCK"]}, QgsProject.instance())
    cov = eng.interval_length_km(ivs)
    # polygon spans ~0.02 deg latitude ~ 2.2 km of route
    ok = 1.5 < cov < 3.0
    # a non-matching value yields nothing
    empty = ri._acquire_polygon_class(sampler, {"layer_id": layer.id(), "attribute": "SOIL",
                                                "match_values": ["SAND"]}, QgsProject.instance())
    ok = ok and eng.interval_length_km(empty) < 1e-6
    QgsProject.instance().removeMapLayer(layer.id())
    return _result("polygon_class acquisition", ok, f"coverage={cov:.2f} km")


def test_proximity_point() -> bool:
    sampler = _sampler()
    layer = _add_layer("Point?crs=EPSG:4326", "hazards")
    pr = layer.dataProvider()
    feat = QgsFeature(layer.fields())
    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0.001, 50.05)))  # ~72 m east of route
    pr.addFeature(feat)
    ivs = ri._acquire_proximity(sampler, {"layer_id": layer.id(), "distance_m": 250.0,
                                          "mode": "distance"}, QgsProject.instance())
    cov = eng.interval_length_km(ivs)
    # chord half-width sqrt(250^2 - 72^2) ~ 239 m -> ~0.48 km interval
    ok = 0.2 < cov < 0.8
    QgsProject.instance().removeMapLayer(layer.id())
    return _result("proximity (point) acquisition", ok, f"coverage={cov:.3f} km")


def test_migrate_framework() -> bool:
    path = _tmp_gpkg()
    store = WorkbenchStore(path, QgsProject.instance().transformContext())
    store.migrate()
    ok = store.read_meta().get("schema_version") == str(schema.SCHEMA_VERSION)
    ok = ok and store._table_exists(schema.TABLE_RULE_SET)
    ok = ok and store._table_exists(schema.TABLE_ASSESSMENT_RANGE)
    # Simulate an older stamp and re-run: framework advances it back, backs up.
    store.write_meta("schema_version", "1")
    store.migrate()
    ok = ok and store.read_meta().get("schema_version") == "2"
    stem, ext = os.path.splitext(path)
    ok = ok and os.path.exists(f"{stem}.migrate_v1.bak{ext}")
    return _result("migration framework v1->v2 + backup", ok)


def test_store_rule_crud() -> bool:
    path = _tmp_gpkg()
    store = WorkbenchStore(path, QgsProject.instance().transformContext())
    store.migrate()
    rule_set_id = store.seed_default_rule_set()
    header = store.get_rule_set(rule_set_id)
    rules = store.list_rules(rule_set_id)
    ok = header is not None and len(rules) == len(schema.DEFAULT_RULES)
    ok = ok and [r["seq"] for r in rules] == list(range(len(rules)))
    # assessment + ranges round trip
    aid = store.save_assessment({"rpl_id": "rpl-x", "rule_set_id": rule_set_id,
                                 "name": "A1", "sample_step_m": 50.0, "min_range_km": 0.0,
                                 "status": "current"})
    store.save_assessment_ranges(aid, [
        {"method": "plough", "start_kp": 0.0, "end_kp": 5.0, "status": "excluded", "risk_level": 4},
    ])
    ok = ok and len(store.list_assessment_ranges(aid)) == 1
    ok = ok and len(store.list_assessments("rpl-x")) == 1
    store.mark_assessments_stale("rpl-x")
    ok = ok and store.get_assessment(aid)["status"] == "stale"
    store.delete_assessment(aid)
    ok = ok and store.get_assessment(aid) is None and len(store.list_assessment_ranges(aid)) == 0
    return _result("store rule/assessment CRUD + stale marking", ok)


def _build_rpl(store: WorkbenchStore) -> str:
    """Write a minimal RPL (points + lines) into the store; return rpl_id."""
    da = make_distance_area(WGS84, QgsProject.instance().transformContext())
    rid = schema.new_id()
    n = 11
    lats = [50.0 + 0.01 * i for i in range(n)]
    # cumulative distances along the meridian, consistent with the route frame
    cum = [0.0]
    for i in range(1, n):
        cum.append(cum[-1] + da.measureLine(QgsPointXY(0.0, lats[i - 1]),
                                             QgsPointXY(0.0, lats[i])) / 1000.0)
    depths = [100.0 * (i + 1) for i in range(n)]  # 100 .. 1100 m, positive-down

    point_rows = []
    for i in range(n):
        point_rows.append({
            "rpl_id": rid, "SeqNo": i, "PosNo": i + 1, "Event": "",
            "DistCumulative": cum[i], "CableDistCumulative": cum[i],
            "ApproxDepth": depths[i], "Latitude": lats[i], "Longitude": 0.0,
            WKT_KEY: f"POINT (0 {lats[i]})",
        })
    line_rows = []
    for i in range(n - 1):
        line_rows.append({
            "rpl_id": rid, "SeqNo": i, "FromPos": i + 1, "ToPos": i + 2,
            WKT_KEY: f"LINESTRING (0 {lats[i]}, 0 {lats[i + 1]})",
        })
    points_name = schema.rpl_points_layer_name("test")
    lines_name = schema.rpl_lines_layer_name("test")
    store.write_spatial_layer(points_name, schema.RPL_POINT_FIELDS, QgsWkbTypes.Point, point_rows)
    store.write_spatial_layer(lines_name, schema.RPL_LINE_FIELDS, QgsWkbTypes.LineString, line_rows)
    store.save_rpl({
        "rpl_id": rid, "name": "test", "kind": "planned",
        "points_layer": points_name, "lines_layer": lines_name,
        "slack_mode": "hold_slack", "depth_source_config": "",
    })
    return rid


def test_run_assessment_end_to_end() -> bool:
    path = _tmp_gpkg()
    store = WorkbenchStore(path, QgsProject.instance().transformContext())
    store.migrate()
    rid = _build_rpl(store)

    # one rule: ApproxDepth magnitude > 550 m excludes plough (deep half of route)
    import json
    rule_set_id = store.save_rule_set(
        {"name": "depth only", "methods_json": json.dumps(["plough", "jet"])},
        [{
            "name": "Depth > 550 m", "enabled": 1, "kind": schema.RULE_KIND_THRESHOLD,
            "action": schema.RULE_ACTION_EXCLUDE, "risk_level": 0,
            "methods_json": json.dumps(["plough"]),
            "config_json": json.dumps({"profile": "depth", "op": ">", "value": 550.0}),
        }],
    )
    result, sampler = ri.run_assessment(store, rid, rule_set_id, sample_step_m=100.0)
    plough = result.per_method["plough"]
    excluded = [v for v in plough if v.status == "excluded"]
    excl_km = sum(v.length_km for v in excluded)
    # depth crosses 550 m near the mid point -> roughly half the ~11 km route excluded
    ok = 3.0 < excl_km < 8.0
    # jet has no applicable rule -> fully allowed
    ok = ok and all(v.status == "allowed" for v in result.per_method["jet"])
    ok = ok and not result.warnings
    ok = ok and abs(sampler.total_km - sampler.route.total_length_km) < 1e-9
    return _result("run_assessment end-to-end (depth exclusion)", ok, f"excluded={excl_km:.2f} km")


def run_all() -> list:
    return [
        test_manual_and_kp_table(),
        test_polygon_class(),
        test_proximity_point(),
        test_migrate_framework(),
        test_store_rule_crud(),
        test_run_assessment_end_to_end(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)
