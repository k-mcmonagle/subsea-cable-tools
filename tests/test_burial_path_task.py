# -*- coding: utf-8 -*-
"""QGIS adapter checks for Installation Paths (geodesy + task boundary)."""

from __future__ import annotations

import json
import os
import tempfile
import time

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)

from ..burial import footprint, map_layers, path_data, path_layers, schema
from ..burial.path_task import InstallationPathTask, build_path_work
from ..burial.store import BurialStore
from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _route():
    # Sparse WGS84 route at 50 N with two labelled-or-unlabelled 90-degree
    # geometry turns. Each leg is long enough for a 20 m local fillet.
    points = [
        QgsPointXY(-1.0000, 50.0000),
        QgsPointXY(-0.9985, 50.0000),
        QgsPointXY(-0.9985, 50.0015),
        QgsPointXY(-0.9970, 50.0015),
    ]
    geometry = QgsGeometry.fromPolylineXY(points)
    project = QgsProject.instance()
    distance = make_distance_area(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        project.transformContext(), project=project)
    route = RouteFrame.from_source(
        [geometry], distance, follow_stored_geometry=True)
    return route, distance, points


def test_task_georeferences_tool_and_constant_layback() -> bool:
    route, distance, source = _route()
    plan = {
        "plan_id": "plan-path-test", "scope_start_kp": 0.0,
        "scope_end_kp": route.total_length_km, "direction": 1,
    }
    config = dict(path_data.DEFAULT_CONFIG,
                  mode=path_data.MODE_FILLET,
                  layback_id="layback-1", generate_barge=True)
    layback = {
        "layback_id": "layback-1", "name": "Constant 50 m",
        "points_json": json.dumps([[0.0, 50.0]]),
        "outside_mode": "error", "source_ref": "test",
    }
    work = build_path_work(
        route, distance, plan, 20.0, path_data.MODE_FILLET, 0.0,
        "Test plough — Normal", schema.METHOD_PLOUGH,
        {"tool": "tool-fp", "barge": "barge-fp"}, config,
        layback_profile=layback)
    task = InstallationPathTask(work, lambda _task: None)
    ok = task.run()
    result = task.result
    ok = ok and task.error is None and result is not None
    if result is None:
        return _result("task georeferencing + constant layback", False,
                       task.error or "no result")
    ok = ok and len(result.tool_wgs84) >= len(source)
    ok = ok and len(result.barge_wgs84) == len(result.tool_wgs84)
    ok = ok and len(result.diagnostics) == 2
    ok = ok and result.summary.get("course_change_count") == 2
    ok = ok and result.summary.get("barge_generated") is True
    ok = ok and abs(result.summary.get("layback_min_m") - 50.0) < 1e-9
    ok = ok and all(-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0
                    for lon, lat in result.tool_wgs84)
    row = result.registry_row(work)
    ok = ok and row.get("tool_path_wkt", "").startswith("LINESTRING")
    ok = ok and row.get("barge_track_wkt", "").startswith("LINESTRING")
    return _result("task georeferencing + constant layback", ok,
                   f"vertices={len(result.tool_wgs84)}")


def test_task_reverse_direction_passes_controls() -> bool:
    route, distance, _source = _route()
    plan = {
        "plan_id": "plan-path-reverse", "scope_start_kp": 0.0,
        "scope_end_kp": route.total_length_km, "direction": -1,
    }
    config = dict(path_data.DEFAULT_CONFIG, mode=path_data.MODE_THROUGH)
    work = build_path_work(
        route, distance, plan, 20.0, path_data.MODE_THROUGH, 0.0,
        "Test plough — Normal", schema.METHOD_PLOUGH,
        {"tool": "tool-fp", "barge": "barge-fp"}, config)
    task = InstallationPathTask(work, lambda _task: None)
    ok = task.run() and task.result is not None
    if task.result is None:
        return _result("reverse-direction exact-through task", False,
                       task.error or "no result")
    diagnostics = task.result.diagnostics
    ok = ok and len(diagnostics) == 2
    ok = ok and diagnostics[0]["kp"] > diagnostics[-1]["kp"]
    ok = ok and all(item["miss_m"] == 0.0 for item in diagnostics)
    return _result("reverse-direction exact-through task", ok)


def test_task_depth_banded_radius_from_profile() -> bool:
    route, distance, _source = _route()
    plan = {
        "plan_id": "plan-path-bands", "scope_start_kp": 0.0,
        "scope_end_kp": route.total_length_km, "direction": 1,
    }
    rules = [{"max_depth_m": 100.0, "radius_m": 30.0},
             {"max_depth_m": 1000.0, "radius_m": 45.0}]
    config = dict(path_data.DEFAULT_CONFIG, mode=path_data.MODE_FILLET,
                  radius_rules=rules)
    # Constant 50 m depth across the scope: every course change lands in
    # the first band (30 m), overriding the 20 m tool radius.
    samples = [(0.0, 50.0), (route.total_length_km, 50.0)]
    work = build_path_work(
        route, distance, plan, 20.0, path_data.MODE_FILLET, 0.0,
        "Test plough — Normal", schema.METHOD_PLOUGH,
        {"tool": "fp", "barge": "fp"}, config,
        depth_samples=samples, radius_rules=rules)
    task = InstallationPathTask(work, lambda _task: None)
    ok = task.run() and task.result is not None
    if task.result is None:
        return _result("depth-banded radius via profile samples", False,
                       task.error or "no result")
    ok = ok and all(item.get("radius_m") == 30.0
                    for item in task.result.diagnostics)
    ok = ok and all(abs(item.get("depth_m") - 50.0) < 1e-6
                    for item in task.result.diagnostics)
    summary = task.result.summary
    ok = ok and summary.get("radius_min_m") == 30.0 \
        and summary.get("radius_max_m") == 30.0 \
        and summary.get("radius_rules_count") == 2
    # No live sampler was supplied, so the off-route depth check is absent.
    ok = ok and all(item.get("depth_diff_m") is None
                    for item in task.result.diagnostics)

    # A scope deeper than every band must fail with a clear message.
    deep = [(0.0, 5000.0), (route.total_length_km, 5000.0)]
    work_deep = build_path_work(
        route, distance, plan, 20.0, path_data.MODE_FILLET, 0.0,
        "Test plough — Normal", schema.METHOD_PLOUGH,
        {"tool": "fp", "barge": "fp"}, config,
        depth_samples=deep, radius_rules=rules)
    task_deep = InstallationPathTask(work_deep, lambda _task: None)
    ok = ok and not task_deep.run() \
        and "deeper than every" in (task_deep.error or "")
    return _result("depth-banded radius via profile samples", ok)


def test_outlines_place_on_tool_path_and_barge_track() -> bool:
    """Plough and vessel outlines ride the generated tracks, metre-true."""
    route, distance, _source = _route()
    plan = {
        "plan_id": "plan-path-outline", "scope_start_kp": 0.0,
        "scope_end_kp": route.total_length_km, "direction": 1,
    }
    layback = {
        "layback_id": "layback-1", "name": "Constant 50 m",
        "points_json": json.dumps([[0.0, 50.0]]),
        "outside_mode": "error",
    }
    config = dict(path_data.DEFAULT_CONFIG, generate_barge=True,
                  layback_id="layback-1")
    work = build_path_work(
        route, distance, plan, 20.0, path_data.MODE_FILLET, 0.0,
        "Test plough — Normal", schema.METHOD_PLOUGH,
        {"tool": "fp", "barge": "fp"}, config, layback_profile=layback)
    task = InstallationPathTask(work, lambda _task: None)
    if not task.run() or task.result is None:
        return _result("outlines ride the generated tracks", False,
                       task.error or "no result")
    row = task.result.registry_row(work)
    tool_points = path_data.parse_linestring_wkt(row["tool_path_wkt"])
    barge_points = path_data.parse_linestring_wkt(row["barge_track_wkt"])
    ok = len(tool_points) == len(barge_points) >= 4

    # Body frame: 8 m x 4 m plough, 100 m x 20 m vessel (bow along +Y).
    plough = QgsGeometry.fromWkt(
        "POLYGON ((-2 -4, 2 -4, 2 4, -2 4, -2 -4))")
    vessel = QgsGeometry.fromWkt(
        "POLYGON ((-10 -50, 10 -50, 10 50, -10 50, -10 -50))")
    index = len(tool_points) // 2
    for outline, points, half_length in ((plough, tool_points, 4.0),
                                         (vessel, barge_points, 50.0)):
        anchor = QgsPointXY(points[index][0], points[index][1])
        before = QgsPointXY(*points[index - 1])
        after = QgsPointXY(*points[index + 1])
        geom, heading = footprint.place_outline_at(
            outline, anchor, before, after)
        ok = ok and geom is not None and heading is not None \
            and not geom.isEmpty()
        if geom is None:
            continue
        # The placed centroid stays on the anchor and the footprint keeps
        # its metre-true size (checked as a geodesic bound-to-bound span).
        centroid = geom.centroid().asPoint()
        offset = distance.measureLine(anchor, QgsPointXY(centroid))
        ok = ok and offset < 1.0
        box = geom.boundingBox()
        span = distance.measureLine(
            QgsPointXY(box.xMinimum(), box.yMinimum()),
            QgsPointXY(box.xMaximum(), box.yMaximum()))
        ok = ok and span > half_length  # sanity: not collapsed to a point
    return _result("outlines ride the generated tracks", ok,
                   f"vertices={len(tool_points)}")


def test_path_map_layers_round_trip() -> bool:
    path = os.path.join(
        tempfile.gettempdir(),
        f"bp_path_layers_{os.getpid()}_{int(time.time() * 1000)}.gpkg")
    store = BurialStore(path, QgsProject.instance().transformContext())
    store.migrate()
    plan = {
        "plan_id": schema.new_id(), "name": "Path layers", "rev_label": "R1",
        "method": schema.METHOD_PLOUGH, "scope_start_kp": 0.0,
        "scope_end_kp": 1.0, "direction": 1, "params_json": "{}",
    }
    store.save_plan(plan)
    result = {
        "path_id": schema.new_id(), "plan_id": plan["plan_id"],
        "generated_utc": schema.utc_now_iso(), "algorithm_version": "1",
        "config_json": json.dumps({"mode": "fillet"}),
        "fingerprints_json": json.dumps({"tool": "a", "barge": "b"}),
        "summary_json": json.dumps({
            "tool": "Plough", "radius_m": 20.0, "length_m": 100.0,
            "max_offset_m": 8.0, "rms_offset_m": 4.0,
            "layback_name": "Constant", "barge_length_m": 100.0}),
        "tool_path_wkt": "LINESTRING (-1 50, -0.999 50)",
        "barge_track_wkt": "LINESTRING (-0.9999 50, -0.9989 50)",
        "diagnostics_json": json.dumps([{
            "control_no": 1, "kp": 0.05, "turn_deg": 45.0,
            "side": "port", "solution": "fillet", "miss_m": 2.0,
            "max_offset_m": 2.0, "status": "ok", "message": "test",
            "lat": 50.0, "lon": -0.9995}]),
    }
    path_layers.write_path_layers(
        store, plan, result, {"tool": "current", "barge": "stale"})
    args = (plan["name"], plan["rev_label"], plan["plan_id"])
    tool = store.open_layer(schema.tool_path_layer_name(*args))
    barge = store.open_layer(schema.barge_track_layer_name(*args))
    issues = store.open_layer(schema.path_issues_layer_name(*args))
    ok = all(layer is not None and layer.isValid()
             for layer in (tool, barge, issues))
    ok = ok and tool.featureCount() == 1 and barge.featureCount() == 1
    ok = ok and issues.featureCount() == 1
    ok = ok and "max_offset_m" in tool.fields().names()
    ensured = path_layers.ensure_path_layers(
        QgsProject.instance(), store.gpkg_path, plan)
    ok = ok and all(layer is not None and layer.isValid()
                    for layer in ensured)
    map_layers.remove_plan_layers(QgsProject.instance(), store.gpkg_path, plan)
    store.close()
    return _result("path map layers round trip + styling", ok)


def run_all() -> list:
    return [
        test_task_georeferences_tool_and_constant_layback(),
        test_task_reverse_direction_passes_controls(),
        test_task_depth_banded_radius_from_profile(),
        test_outlines_place_on_tool_path_and_barge_track(),
        test_path_map_layers_round_trip(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
