# -*- coding: utf-8 -*-
"""Checks for Burial Planner acquisition plumbing (requires the QGIS API).

Scoped sampler == full sampler clipped to scope; threshold acquisition with
signed slope and no-data gaps; per-feature buffer override; cooperative
cancellation raising cleanly; direction mapping of slope limits.
"""

from __future__ import annotations

import json
import os
import tempfile

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsRubberBand, QgsVertexMarker

from ..burial import analysis_task, burial_dock, generation, map_layers
from ..burial import schema as burial_schema
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..workbench import rules_engine as eng
from ..workbench import rules_inputs as ri
from ..workbench import store as workbench_store_module
from ..workbench.rules_engine import Interval
from ..workbench.store import WorkbenchStore

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _route() -> tuple:
    da = make_distance_area(WGS84, QgsProject.instance().transformContext())
    geoms = [QgsGeometry.fromWkt("LINESTRING(0 50, 0 50.2)")]  # ~22 km
    return RouteFrame.from_source(geoms, da), da


def test_scoped_sampler_matches_full() -> bool:
    route, da = _route()
    full = ri.RouteSampler.from_route(route, da, 100.0)
    scope = Interval(5.0, 10.0)
    scoped = ri.RouteSampler.from_route(route, da, 100.0, scope)
    # scoped stations stay within scope + one step margin
    ok = all(scope.start_km - 0.11 <= kp <= scope.end_km + 0.11
             for kp in scoped.stations_km)
    ok = ok and len(scoped.stations_km) < len(full.stations_km) / 2
    ok = ok and abs(scoped.scope_domain.start_km - 5.0) < 1e-9

    # a manual rule acquired on both, clipped to scope, agrees
    config = {"ranges": [{"start_kp": 3.0, "end_kp": 7.0}]}
    full_ivs = eng.clip_intervals(ri._acquire_manual(full, config), scope)
    scoped_ivs = eng.clip_intervals(ri._acquire_manual(scoped, config), scope)
    ok = ok and len(full_ivs) == len(scoped_ivs) == 1
    ok = ok and abs(full_ivs[0].start_km - scoped_ivs[0].start_km) < 1e-9
    ok = ok and abs(full_ivs[0].end_km - scoped_ivs[0].end_km) < 1e-9
    return _result("scoped sampler == full sampler clipped to scope", ok,
                   f"{len(scoped.stations_km)} vs {len(full.stations_km)} stations")


def test_depth_series_gaps_and_threshold() -> bool:
    route, da = _route()
    sampler = ri.RouteSampler.from_route(route, da, 100.0, Interval(0.0, 20.0))

    def sample_fn(lat, lon):
        kp_m = (lat - 50.0) * 111000.0  # rough; monotone in lat is what matters
        kp = kp_m / 1000.0
        if 14.5 < kp < 16.5:
            return None  # a survey hole
        return 100.0 + 50.0 * kp  # deepening northwards

    series, gaps = ri.depth_series_with_gaps(sampler, sample_fn)
    ok = len(series) > 100
    ok = ok and len(gaps) == 1 and 13.5 < gaps[0].start_km < 15.5
    # unsigned threshold: depth > 600 m fires in the deep half
    ivs = ri.threshold_intervals(series, {"profile": "depth", "op": ">",
                                          "value": 600.0}, sampler.scope_domain)
    ok = ok and ivs and ivs[0].start_km > 8.0
    # signed slope: uniformly deepening -> downslope limit 0.5° fires everywhere,
    # upslope limit never fires
    down = ri.threshold_intervals(series, {"profile": "slope", "slope_signed": True,
                                           "downslope_max_deg": 0.5},
                                  sampler.scope_domain)
    up = ri.threshold_intervals(series, {"profile": "slope", "slope_signed": True,
                                         "upslope_max_deg": 0.5},
                                sampler.scope_domain)
    ok = ok and eng.interval_length_km(down) > 10.0
    ok = ok and eng.interval_length_km(up) < 3.0
    return _result("depth gaps -> no-data intervals; signed slope thresholds", ok)


def test_buffer_field_override() -> bool:
    route, da = _route()
    sampler = ri.RouteSampler.from_route(route, da, 100.0)
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=buf:double", "hazards", "memory")
    provider = layer.dataProvider()
    for lat, buf in ((50.05, 500.0), (50.15, 50.0)):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0.0, lat)))
        feat.setAttributes([buf])
        provider.addFeature(feat)
    index, feats = ri._load_features_wgs84(layer, QgsProject.instance())
    config = {"distance_m": 100.0, "mode": "distance", "buffer_field": "buf"}
    ivs = ri.proximity_intervals(sampler, index, feats, layer.geometryType(), config)
    lengths = sorted(iv.length_km for iv in ivs)
    # feature 1: 1 km chord; feature 2: 0.1 km chord
    ok = len(lengths) == 2 and 0.05 < lengths[0] < 0.2 and 0.8 < lengths[1] < 1.2
    return _result("per-feature buffer_field overrides blanket distance", ok,
                   f"lengths={[round(x, 3) for x in lengths]}")


def test_cancellation_raises() -> bool:
    route, da = _route()
    sampler = ri.RouteSampler.from_route(route, da, 100.0)
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326&field=S:string", "soils", "memory")
    feat = QgsFeature(layer.fields())
    feat.setGeometry(QgsGeometry.fromWkt(
        "POLYGON((-0.01 50.05, 0.01 50.05, 0.01 50.1, -0.01 50.1, -0.01 50.05))"))
    feat.setAttributes(["ROCK"])
    layer.dataProvider().addFeature(feat)
    index, feats = ri._load_features_wgs84(layer, QgsProject.instance())
    try:
        ri.polygon_class_intervals(sampler, index, feats,
                                   {"attribute": "S", "match_values": ["ROCK"]},
                                   cancel=lambda: True)
        ok = False
    except ri.AcquisitionCancelled:
        ok = True
    # cancel=None (Assessment path) still works
    ivs = ri.polygon_class_intervals(sampler, index, feats,
                                     {"attribute": "S", "match_values": ["ROCK"]})
    ok = ok and eng.interval_length_km(ivs) > 1.0
    return _result("cooperative cancellation raises; default path unchanged", ok)


def test_direction_maps_slope_limits() -> bool:
    rule = {"rule_id": "r", "config_json": json.dumps({
        "profile": "slope", "slope_signed": True,
        "downslope_max_deg": 10.0, "upslope_max_deg": 4.0})}
    forward = analysis_task._effective_config(rule, 1)
    backward = analysis_task._effective_config(rule, -1)
    ok = forward["downslope_max_deg"] == 10.0 and forward["upslope_max_deg"] == 4.0
    ok = ok and backward["downslope_max_deg"] == 4.0 and backward["upslope_max_deg"] == 10.0
    return _result("direction -1 swaps down/up-slope limits", ok)


def test_contour_slope_uses_route_crossings() -> bool:
    """A gentle contour gradient must not become a nearest-contour step."""
    from ..workbench.depth_service import DepthSourceConfig

    project = QgsProject.instance()
    route, da = _route()
    contours = QgsVectorLayer("LineString?crs=EPSG:4326&field=depth:double",
                              "gentle-contours", "memory")
    provider = contours.dataProvider()
    for lat, depth in ((50.05, 100.0), (50.06, 150.0)):
        feat = QgsFeature(contours.fields())
        feat.setGeometry(QgsGeometry.fromWkt(
            f"LINESTRING(-0.05 {lat}, 0.05 {lat})"))
        feat.setAttributes([depth])
        provider.addFeature(feat)
    project.addMapLayer(contours)

    config = DepthSourceConfig({
        "mode": 2,
        "contour_layers": [{"layer_id": contours.id(),
                            "depth_field": "depth"}],
    })
    depth = analysis_task.DepthSnapshot(config, project)
    ok = depth.prepare()
    sampler = ri.RouteSampler.from_route(route, da, 50.0, Interval(5.4, 6.8))
    samples = depth.profile_samples(route, sampler.stations_km)
    series = [(kp, abs(float(value))) for kp, value in samples
              if value is not None]
    slopes = ri._slope_series(series)
    maximum = max((slope for _kp, slope in slopes), default=90.0)
    excluded = ri.threshold_intervals(
        series, {"profile": "slope", "op": ">", "value": 15.0,
                 "abs": True}, sampler.scope_domain)

    # This is the old nearest-contour representation: the full 50 m contour
    # interval jumps within one 50 m station and falsely exceeds 15 degrees.
    midpoint = sum(kp for kp, _depth in depth.contour_crossings(route)) / 2.0
    staircase = [(kp, 100.0 if kp < midpoint else 150.0)
                 for kp in sampler.stations_km]
    old_maximum = max(slope for _kp, slope in ri._slope_series(staircase))
    ok = ok and old_maximum > 15.0 and maximum < 3.5 and not excluded

    project.removeMapLayer(contours.id())
    return _result("contour slopes interpolate at route crossings", ok,
                   f"old={old_maximum:.2f}°, corrected={maximum:.2f}°")


def test_route_frame_builder() -> bool:
    layer = QgsVectorLayer("LineString?crs=EPSG:4326&field=SeqNo:integer",
                           "route", "memory")
    provider = layer.dataProvider()
    for seq, wkt in ((1, "LINESTRING(0 50.1, 0 50.2)"),
                     (0, "LINESTRING(0 50, 0 50.1)")):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromWkt(wkt))
        feat.setAttributes([seq])
        provider.addFeature(feat)
    route, da = analysis_task.build_route_frame(layer, QgsProject.instance())
    ok = 21.0 < route.total_length_km < 23.0
    # SeqNo ordering: KP 5 lies on the first (SeqNo 0) feature near lat 50.045
    point = route.point_at_kp(5.0, clamp=True)
    ok = ok and point is not None and 50.0 < point.y() < 50.1
    return _result("route frame from SeqNo-ordered line layer", ok)


def test_plan_route_follows_stored_geometry() -> bool:
    """Generated plan geometry must sit on the RPL as drawn in QGIS."""
    layer = QgsVectorLayer("LineString?crs=EPSG:4326&field=SeqNo:integer",
                           "sparse-route", "memory")
    feat = QgsFeature(layer.fields())
    feat.setGeometry(QgsGeometry.fromWkt("LINESTRING(-10 60, 10 60)"))
    feat.setAttributes([0])
    layer.dataProvider().addFeature(feat)

    route, _da = analysis_task.build_route_frame(layer, QgsProject.instance())
    midpoint = route.point_at_kp(route.total_length_km / 2.0, clamp=True)
    segment = route.extract_segment(route.total_length_km * 0.25,
                                    route.total_length_km * 0.75)
    vertices = list(segment.vertices()) if segment is not None else []
    ok = midpoint is not None and abs(midpoint.y() - 60.0) < 1e-10
    ok = ok and abs(midpoint.x()) < 1e-8
    ok = ok and len(vertices) >= 2
    ok = ok and all(abs(point.y() - 60.0) < 1e-10 for point in vertices)
    return _result("plan route follows stored RPL geometry", ok)


def test_section_style_has_no_cartographic_offset() -> bool:
    """A display offset can become a many-metre error at small map scales."""
    layer = QgsVectorLayer("LineString?crs=EPSG:4326&field=kind:string",
                           "plan-sections", "memory")
    map_layers.apply_sections_style(layer)
    renderer = layer.renderer()
    children = renderer.rootRule().children() if renderer is not None else []
    offsets = []
    for child in children:
        symbol = child.symbol()
        if symbol is not None and symbol.symbolLayerCount():
            offsets.append(float(symbol.symbolLayer(0).offset()))
    ok = len(offsets) == 3 and all(abs(offset) < 1e-12 for offset in offsets)
    return _result("section styling has no map offset", ok,
                   f"offsets={offsets}")


def test_canvas_items_close_without_qobject_api() -> bool:
    """Closing must support QGIS canvas items which lack deleteLater()."""
    canvas = QgsMapCanvas()
    marker = QgsVertexMarker(canvas)
    band = QgsRubberBand(canvas, burial_dock.GEOMETRY_LINE)
    marker_had_no_delete_later = not hasattr(marker, "deleteLater")
    burial_dock._remove_canvas_item(marker)
    burial_dock._remove_canvas_item(band)
    burial_dock._remove_canvas_item(marker)  # repeated shutdown is harmless
    ok = marker.scene() is None and band.scene() is None
    # QGIS 3 exercises the exact reported compatibility case. QGIS versions
    # which expose QObject APIs still have to pass the scene-detachment check.
    detail = f"QGIS3 marker lacks deleteLater={marker_had_no_delete_later}"
    return _result("canvas overlays close without QObject-only API", ok, detail)


def test_existing_plan_file_open_is_non_destructive() -> bool:
    """Open validates the registry and never creates a missing selected file."""
    # OGR can retain pooled Windows handles until QGIS exits, so this follows
    # the other store tests and leaves the OS temporary directory to cleanup.
    folder = tempfile.mkdtemp(prefix="burial_open_test_")
    missing = os.path.join(folder, "not-a-plan.gpkg")
    rejected = False
    try:
        burial_dock.BurialPlannerDock._open_existing_store(missing)
    except ValueError:
        rejected = True
    ok = rejected and not os.path.exists(missing)

    valid = os.path.join(folder, "existing-plans.gpkg")
    store = BurialStore(valid)
    store.migrate()
    plan_id = store.save_plan({"name": "Recovered plan", "method": "plough"})
    reopened = burial_dock.BurialPlannerDock._open_existing_store(valid)
    plans = reopened.list_plans()
    ok = ok and len(plans) == 1 and plans[0].get("plan_id") == plan_id
    return _result("existing plan file is validated before opening", ok)


def test_workbench_path_recovers_after_project_move() -> bool:
    """A stale absolute Workbench path falls back beside the moved project."""
    project = QgsProject.instance()
    old_filename = project.fileName()
    old_path = workbench_store_module.project_gpkg_path(project)
    folder = tempfile.mkdtemp(prefix="burial_wb_move_test_")
    try:
        project.setFileName(os.path.join(folder, "moved-project.qgz"))
        expected = workbench_store_module.default_project_gpkg_path(project)
        workbench = WorkbenchStore(expected)
        workbench.ensure_created()
        missing = os.path.join(folder, "old-machine", "workbench.gpkg")
        workbench_store_module.set_project_gpkg_path(missing, project)
        dock_like = type("DockLike", (), {
            "store": type(
                "Store", (), {"gpkg_path": os.path.join(folder, "plans.gpkg")})(),
            "model": type("Model", (), {"plan": {}})(),
        })()
        recovered = burial_dock.BurialPlannerDock.workbench_store(dock_like)
        ok = recovered is not None
        ok = ok and os.path.normcase(recovered.gpkg_path) == os.path.normcase(expected)
        ok = ok and workbench_store_module.project_gpkg_path(project) == expected
    finally:
        project.setFileName(old_filename)
        if old_path:
            workbench_store_module.set_project_gpkg_path(old_path, project)
        else:
            project.removeEntry(workbench_store_module.PROJECT_SCOPE,
                                workbench_store_module.PROJECT_KEY_GPKG)
    return _result("Workbench path recovers after project move", ok)


def test_reopened_plan_matches_unique_rpl_snapshot() -> bool:
    """A re-imported RPL can be offered by its saved name and revision."""
    route_layer = QgsVectorLayer(
        "LineString?crs=EPSG:4326&field=SeqNo:integer", "route", "memory")
    feature = QgsFeature(route_layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt("LINESTRING(0 50, 0 50.1)"))
    feature.setAttributes([0])
    route_layer.dataProvider().addFeature(feature)
    replacement = {
        "rpl_id": "new-rpl-id", "name": "Test Route", "rev_label": "C03",
        "lines_layer": "rpl_lines", "modified_utc": "now",
    }

    class _Workbench:
        gpkg_path = "moved-workbench.gpkg"

        def get_rpl(self, rpl_id):
            return replacement if rpl_id == replacement["rpl_id"] else None

        def list_rpls(self):
            return [replacement]

        def open_layer(self, layer_name):
            return route_layer if layer_name == "rpl_lines" else None

    folder = tempfile.mkdtemp(prefix="burial_rpl_relink_test_")
    store = BurialStore(os.path.join(folder, "plans.gpkg"))
    store.migrate()
    plan_id = store.save_plan({
        "name": "Existing plan", "method": "plough", "rpl_id": "old-rpl-id",
        "rpl_name": "Test Route", "rpl_revision": "C03",
        "rpl_gpkg_path": "old-workbench.gpkg",
    })
    model = PlanModel(store, _Workbench())
    ok = model.load_plan(plan_id)
    ok = ok and model.route is not None
    ok = ok and model.resolved_rpl_id == "new-rpl-id"
    ok = ok and bool(model.route_notice)
    ok = ok and model.plan.get("rpl_id") == "old-rpl-id"  # user confirms relink
    return _result("reopened plan matches unique RPL name + revision", ok)


def test_end_to_end_task_and_generation() -> bool:
    """build_work -> task.run() (synchronous) -> generate over memory layers."""
    from ..workbench.depth_service import DepthSourceConfig

    project = QgsProject.instance()
    route, da = _route()  # ~22 km along the meridian

    # Contour bathymetry: 100 m in the south, 1000 m in the north.
    contours = QgsVectorLayer("LineString?crs=EPSG:4326&field=depth:double",
                              "contours", "memory")
    provider = contours.dataProvider()
    for lat, depth in ((50.0, 100.0), (50.2, 1000.0)):
        feat = QgsFeature(contours.fields())
        feat.setGeometry(QgsGeometry.fromWkt(
            f"LINESTRING(-0.05 {lat}, 0.05 {lat})"))
        feat.setAttributes([depth])
        provider.addFeature(feat)
    project.addMapLayer(contours)

    # A crossing point near KP 5.55 (lat 50.05).
    crossings = QgsVectorLayer("Point?crs=EPSG:4326", "crossings", "memory")
    feat = QgsFeature(crossings.fields())
    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0.0, 50.05)))
    crossings.dataProvider().addFeature(feat)
    project.addMapLayer(crossings)

    plan = {"plan_id": "p1", "name": "e2e", "method": "plough",
            "scope_start_kp": 0.0, "scope_end_kp": 20.0, "direction": 1}
    inputs = [{"input_id": "in-x", "plan_id": "p1", "role": "crossings_points",
               "layer_name": "crossings", "layer_source": crossings.source(),
               "layer_id_hint": crossings.id(), "config_json": "{}"}]
    rules = [
        {"rule_id": "deep", "plan_id": "p1", "seq": 0, "name": "WD > 500 m",
         "enabled": 1, "kind": "threshold_profile", "action": "exclude",
         "risk_level": 0, "criterion_class": "project", "source_ref": "DOC",
         "methods_json": json.dumps(["plough"]),
         "config_json": json.dumps({"profile": "depth", "op": ">", "value": 500.0}),
         "notes": ""},
        {"rule_id": "xing", "plan_id": "p1", "seq": 1, "name": "Crossing 500 m",
         "enabled": 1, "kind": "proximity", "action": "exclude", "risk_level": 0,
         "criterion_class": "non_deviable", "source_ref": "DOC",
         "methods_json": json.dumps(["plough"]),
         "config_json": json.dumps({"input_id": "in-x", "distance_m": 500.0,
                                    "mode": "distance",
                                    "influence_before_m": 100.0,
                                    "influence_after_m": 100.0}),
         "notes": ""},
    ]
    depth_config = DepthSourceConfig({
        "mode": 2, "contour_layers": [{"layer_id": contours.id(),
                                       "depth_field": "depth"}],
        "contour_search_radius_m": 0.0})
    params = generation.GenParams(0.0, 20.0, direction=1, method="plough",
                                  min_section_km=0.5, coarse_step_m=100.0)
    cache = {}
    work, warnings = analysis_task.build_work(
        route, da, plan, rules, inputs, depth_config, params, cache, "rpl-fp",
        project)
    ok = not warnings and len(work.rules) == 2

    task = analysis_task.BurialAnalysisTask(work, lambda t: None)
    ok = ok and task.run() and not task.error
    results = task.results
    ok = ok and len(results) == 2 and not any(r.error for r in results)
    deep = next(r for r in results if r.rule_row["rule_id"] == "deep")
    xing = next(r for r in results if r.rule_row["rule_id"] == "xing")
    # Linear contour interpolation crosses 500 m at 4/9 of the route.
    ok = ok and deep.footprint and 9.0 < deep.footprint[0].start_km < 10.8
    ok = ok and xing.footprint and 4.5 < xing.footprint[0].start_km < 6.0
    ok = ok and eng.interval_length_km(xing.footprint) < 1.5

    acquisitions = [generation.RuleAcquisition(r.rule_row, r.footprint, r.nodata,
                                               r.error) for r in results]
    out = generation.generate(params, acquisitions, plan_id="p1",
                              generation_id="g1")
    kinds = [s["kind"] for s in out.sections]
    ok = ok and kinds.count("burial") == 2 and kinds.count("skip") == 2
    ok = ok and out.summary["burial_km"] > 8.0
    ok = ok and any("Constraint Influence Zone" in f.get("message", "")
                    for s in out.sections
                    for f in json.loads(s["reason_json"]).get("influence_flags", []))

    # warm-cache re-run: everything served from cache (the dock stores the
    # per-rule results back into the cache dict when a run lands)
    for r in results:
        cache[r.cache_key] = (r.footprint, r.nodata)
    work2, _w = analysis_task.build_work(route, da, plan, rules, inputs,
                                         depth_config, params, cache, "rpl-fp",
                                         project)
    ok = ok and all(rw.cached is not None for rw in work2.rules)

    project.removeMapLayer(contours.id())
    project.removeMapLayer(crossings.id())
    return _result("end-to-end: build_work -> task.run -> generate + warm cache",
                   ok, f"burial={out.summary['burial_km']:.2f} km")


def test_profile_sampling_task() -> bool:
    route, _da = _route()

    class _Depth:
        def is_available(self):
            return True

        def prepare(self, cancel=None, progress=None):
            if progress is not None:
                progress(1, 1)
            return not (cancel is not None and cancel())

        def sample(self, lat, lon):
            return -100.0 - lat

        def profile_samples(self, route, stations_km, cancel=None, progress=None):
            out = []
            total = len(stations_km)
            for i, kp in enumerate(stations_km):
                point = route.point_at_kp(kp, clamp=True)
                out.append((kp, self.sample(point.y(), point.x())))
                if progress is not None:
                    progress(i + 1, total)
            return out

    task = analysis_task.ProfileSamplingTask(
        route, _Depth(), 0.0, 0.3, 100.0, lambda _task: None)
    ok = task.run()
    ok = ok and len(task.series) == 4
    ok = ok and task.series[0][0] == 0.0 and task.series[-1][0] == 0.3
    ok = ok and all(depth > 0 for _kp, depth in task.series)
    return _result("background profile task samples scope with progress", ok)


def test_route_frame_chainage_matches_walk() -> bool:
    """Chainage-indexed point_at_kp equals the original per-call walk."""
    from ..kp_geo_utils import point_at_kp as walk_point_at_kp

    da = make_distance_area(WGS84, QgsProject.instance().transformContext())
    geoms = [QgsGeometry.fromWkt("LINESTRING(0 50, 0 50.1, 0.05 50.15)"),
             QgsGeometry.fromWkt("LINESTRING(0.05 50.15, 0.1 50.2)")]
    ok = True
    for follow in (False, True):
        route = RouteFrame.from_source(geoms, da,
                                       follow_stored_geometry=follow)
        total = route.total_length_km
        probes = [-1.0, 0.0, 1e-9, total * 0.25, total * 0.5,
                  total * 0.75, total - 1e-6, total, total + 5.0]
        for kp in probes:
            for clamp in (False, True):
                fast = route.point_at_kp(kp, clamp=clamp)
                slow = walk_point_at_kp(geoms, kp, da, clamp=clamp,
                                        follow_stored_geometry=follow)
                if (fast is None) != (slow is None):
                    ok = False
                elif fast is not None:
                    ok = ok and abs(fast.x() - slow.x()) < 1e-9 \
                        and abs(fast.y() - slow.y()) < 1e-9
    return _result("RouteFrame chainage index matches walking point_at_kp", ok)


def test_profile_cross_offset_sampling() -> bool:
    """Cross-offset depths sample either side of the route with the right sign."""
    from ..burial import profile_data

    route, da = _route()  # due north along lon 0

    class _Depth:
        def is_available(self):
            return True

        def prepare(self, cancel=None, progress=None):
            return True

        def sample(self, lat, lon):
            # Deeper to the east: magnitude grows with +lon.
            return -(100.0 + lon * 1000.0)

        def profile_samples(self, route_, stations_km, cancel=None, progress=None):
            return [(kp, self.sample(route_.point_at_kp(kp, clamp=True).y(),
                                     route_.point_at_kp(kp, clamp=True).x()))
                    for kp in stations_km]

    task = analysis_task.ProfileSamplingTask(
        route, _Depth(), 0.0, 0.3, 100.0, lambda _task: None,
        distance=da, cross_offset_m=100.0)
    ok = task.run()
    ok = ok and len(task.kps) == len(task.port_depths) == len(task.stbd_depths)
    ok = ok and all(v is not None for v in task.port_depths)
    # Heading north: starboard = east = deeper (larger magnitude).
    ok = ok and all(s > p for p, s in zip(task.port_depths, task.stbd_depths))

    cross = profile_data.cross_slope_series(
        task.kps, task.port_depths, task.stbd_depths, 100.0, direction=1)
    ok = ok and all(v is not None and v > 0 for _kp, v in cross)
    flipped = profile_data.cross_slope_series(
        task.kps, task.port_depths, task.stbd_depths, 100.0, direction=-1)
    ok = ok and all(v is not None and v < 0 for _kp, v in flipped)

    # Without a distance area the task degrades to along-route sampling only.
    plain = analysis_task.ProfileSamplingTask(
        route, _Depth(), 0.0, 0.3, 100.0, lambda _task: None)
    ok = ok and plain.run() and all(v is None for v in plain.port_depths)
    return _result("cross-offset sampling: starboard side, sign, direction flip", ok)


def test_cross_offset_uses_contour_crossings() -> bool:
    """Cross-offset contour depths interpolate offset-line crossings.

    Regression: the cross phase used per-station nearest-contour scans —
    O(stations × contours), which stalled for hours on ~1000 km routes and
    invented values from distant contours on flat stretches. It now mirrors
    the along-route methodology: intersect the offset polylines with the
    contours and interpolate between bracketing crossings only.
    """
    from ..workbench.depth_service import DepthSourceConfig

    project = QgsProject.instance()
    route, da = _route()  # due north along lon 0, ~22 km
    contours = QgsVectorLayer("LineString?crs=EPSG:4326&field=depth:double",
                              "xoff-contours", "memory")
    provider = contours.dataProvider()
    # Contours tilted up to the north-east (lat rises 0.1° per 1° lon), so
    # the port offset line crosses each contour south of the starboard line.
    for base, depth in ((50.02, 100.0), (50.18, 1000.0)):
        feat = QgsFeature(contours.fields())
        feat.setGeometry(QgsGeometry.fromWkt(
            f"LINESTRING(-0.05 {base - 0.005}, 0.05 {base + 0.005})"))
        feat.setAttributes([depth])
        provider.addFeature(feat)
    project.addMapLayer(contours)
    config = DepthSourceConfig({
        "mode": 2,
        "contour_layers": [{"layer_id": contours.id(),
                            "depth_field": "depth"}],
        "contour_search_radius_m": 500.0})
    snapshot = analysis_task.DepthSnapshot(config, project)
    task = analysis_task.ProfileSamplingTask(
        route, snapshot, 0.0, route.total_length_km, 100.0, lambda _t: None,
        distance=da, cross_offset_m=100.0)
    ok = task.run() and not task.error
    ok = ok and len(task.kps) == len(task.port_depths) == len(task.stbd_depths)
    interior = [(p, s) for p, s in zip(task.port_depths, task.stbd_depths)
                if p is not None and s is not None]
    ok = ok and len(interior) > 100
    # Deepening northwards + NE tilt: at every station the port line sits
    # effectively further north relative to the contour field -> deeper.
    ok = ok and all(p > s for p, s in interior)
    # Outside the bracketing crossings nothing is invented, even though the
    # nearest contour is well within reach of a nearest-point scan.
    ok = ok and task.port_depths[0] is None and task.stbd_depths[0] is None
    ok = ok and task.port_depths[-1] is None and task.stbd_depths[-1] is None
    ok = ok and any(v is not None for v in task.depths)
    project.removeMapLayer(contours.id())
    return _result("cross-offset depths interpolate offset-line contour crossings",
                   ok, f"{len(interior)} interior stations")


def test_profile_widget_axes_crosshair_toggles() -> bool:
    """Depth/slope plots: aligned axes, no SI-prefixed KP, mirrored crosshair,
    per-series toggles, adjustable splitter."""
    from ..burial.profile_widget import BurialProfileWidget

    widget = BurialProfileWidget()
    plot_item = widget.plot.getPlotItem()
    slope_item = widget.slope_plot.getPlotItem()
    # No SI auto-prefix: a 1000 km route must label "KP (km)", not "KP (kkm)".
    ok = not plot_item.getAxis("bottom").autoSIPrefix
    ok = ok and not slope_item.getAxis("bottom").autoSIPrefix
    ok = ok and not plot_item.getAxis("left").autoSIPrefix
    # Same fixed left-axis width on both plots -> the x axes align exactly.
    ok = ok and plot_item.getAxis("left").fixedWidth == \
        slope_item.getAxis("left").fixedWidth is not None
    # x-link: zooming the depth plot moves the slope plot with it.
    widget.set_scope(0.0, 10.0)
    widget.plot.setXRange(2.0, 4.0, padding=0)
    lo, hi = slope_item.vb.viewRange()[0]
    ok = ok and abs(lo - 2.0) < 0.2 and abs(hi - 4.0) < 0.2
    # Crosshair mirrors onto the slope panel.
    widget.set_slope_visible(True)
    widget.set_profile([(0.0, 100.0), (10.0, 200.0)])
    widget.set_slope_series([(0.0, 1.0), (10.0, 1.0)],
                            [(0.0, -0.5), (10.0, -0.5)],
                            [(0.0, 1.1), (10.0, 1.1)])
    widget.focus_kp(3.0)
    ok = ok and widget._vline.isVisible() and widget._slope_vline.isVisible()
    ok = ok and abs(float(widget._vline.value()) - 3.0) < 1e-9
    ok = ok and abs(float(widget._slope_vline.value()) - 3.0) < 1e-9
    # Readout includes cross/absolute values from the stored series.
    text = widget._readout.textItem.toPlainText()
    ok = ok and "Cross" in text and "Abs" in text
    # Per-series toggles hide/show their curve.
    widget._series_toggles["cross"].setChecked(False)
    ok = ok and not widget._slope_curves["cross"].isVisible()
    widget._series_toggles["cross"].setChecked(True)
    ok = ok and widget._slope_curves["cross"].isVisible()
    # Depth and slope plots sit in a user-adjustable splitter; context menus
    # are enabled for export/axis options.
    ok = ok and widget._splitter.count() == 2
    ok = ok and plot_item.vb.menu is not None
    return _result("profile widget: axes, alignment, crosshair, toggles", ok)


def test_analysis_reuses_stored_depth_samples() -> bool:
    """Injected plan-profile samples bypass bathymetry sampling entirely."""

    class _Boom:
        def is_available(self):
            return True

        def prepare(self, cancel=None, progress=None):
            return True

        def profile_samples(self, *_args, **_kwargs):
            raise AssertionError("stored samples should bypass sampling")

        def sample(self, lat, lon):
            return -50.0

        def sample_route(self, route_, kp):
            return -50.0

    route, da = _route()
    scope = Interval(0.0, 10.0)
    stations = [round(0.05 * i, 6) for i in range(201)]
    depth_samples = [(kp, 100.0 + (400.0 if 4.0 <= kp <= 6.0 else 0.0))
                     for kp in stations]
    rule_row = {"rule_id": "r-depth", "name": "Deep", "seq": 0, "enabled": 1,
                "kind": "threshold_profile", "action": "exclude",
                "criterion_class": "project", "methods_json": "[]",
                "config_json": json.dumps({"profile": "depth", "op": ">",
                                           "value": 300.0})}
    work = analysis_task.AnalysisWork(
        route=route, distance=da, scope=scope, step_m=50.0, direction=1,
        method="plough", refine_tol_m=1.0, depth=_Boom(),
        depth_samples=depth_samples)
    rule_work = analysis_task.RuleWork(rule_row=rule_row,
                                       kind="threshold_profile",
                                       config=json.loads(rule_row["config_json"]))
    work.rules.append(rule_work)
    task = analysis_task.BurialAnalysisTask(work, lambda _t: None)
    ok = task.run()
    ok = ok and len(task.results) == 1 and not task.results[0].error
    footprint = task.results[0].footprint
    ok = ok and len(footprint) == 1
    ok = ok and abs(footprint[0].start_km - 4.0) < 0.1
    ok = ok and abs(footprint[0].end_km - 6.0) < 0.1
    return _result("analysis consumes stored plan-profile samples", ok)


def test_profile_step_resolution_and_staleness() -> bool:
    """Profile step: manual override, auto fallback, clamps; step change -> stale."""
    from ..burial import profile_data

    model = PlanModel(object(), None)
    model.plan = {"plan_id": "p1", "scope_start_kp": 0.0, "scope_end_kp": 10.0,
                  "direction": 1, "method": "plough", "params_json": "{}"}
    # Auto with no rasters configured -> 5 m fallback (coarse step 50 allows it).
    ok = abs(model.resolve_profile_step_m() - 5.0) < 1e-9
    # Manual override wins.
    model.plan["params_json"] = json.dumps({"profile_step_m": 25.0})
    ok = ok and abs(model.resolve_profile_step_m() - 25.0) < 1e-9
    # Clamped to the analysis step so Generate can reuse the samples.
    model.plan["params_json"] = json.dumps({"profile_step_m": 100.0,
                                            "coarse_step_m": 50.0})
    ok = ok and abs(model.resolve_profile_step_m() - 50.0) < 1e-9
    # Floored at 2 m.
    model.plan["params_json"] = json.dumps({"profile_step_m": 0.5})
    ok = ok and abs(model.resolve_profile_step_m() - 2.0) < 1e-9
    # Station ceiling: a 2000 km scope cannot sample at 2 m.
    model.plan.update({"scope_end_kp": 2000.0})
    ok = ok and abs(model.resolve_profile_step_m() - 4.0) < 1e-9
    model.plan.update({"scope_end_kp": 10.0})

    # A stored profile whose step no longer matches the target goes stale.
    model.plan["params_json"] = "{}"
    profile = profile_data.PlanProfile(
        step_m=model.resolve_profile_step_m(), cross_offset_m=50.0,
        scope_start_kp=0.0, scope_end_kp=10.0,
        route_fingerprint=model.current_rpl_fingerprint(),
        depth_fingerprint=model.depth_fingerprint(),
        kps=[0.0, 5.0, 10.0], depths=[10.0, 20.0, 30.0])
    model.bathy_profile = profile
    ok = ok and model.profile_state() == "current"
    model.plan["params_json"] = json.dumps({"profile_step_m": 25.0})
    ok = ok and model.profile_state() == "stale"
    return _result("profile step: manual/auto/clamps; step change -> stale", ok)


def test_burial_depth_config_is_manual_only() -> bool:
    class _Workbench:
        def rpl_depth_config(self, _rpl_id):
            return {"mode": 1, "raster_layer_ids": ["workbench-raster"]}

    model = PlanModel(object(), _Workbench())
    model.plan = {"rpl_id": "rpl-1"}
    ok = not model.depth_config().is_configured()
    model.inputs = [{
        "role": burial_schema.INPUT_ROLE_BATHY,
        "config_json": json.dumps({
            "mode": 2,
            "contour_layers": [{"layer_id": "manual", "depth_field": "z"}],
        }),
    }]
    config = model.depth_config()
    ok = ok and config.mode == 2 and len(config.contour_layers) == 1
    return _result("Burial Planner bathymetry is manual-only", ok)


def run_all() -> list:
    return [
        test_scoped_sampler_matches_full(),
        test_depth_series_gaps_and_threshold(),
        test_buffer_field_override(),
        test_cancellation_raises(),
        test_direction_maps_slope_limits(),
        test_contour_slope_uses_route_crossings(),
        test_route_frame_builder(),
        test_plan_route_follows_stored_geometry(),
        test_section_style_has_no_cartographic_offset(),
        test_canvas_items_close_without_qobject_api(),
        test_existing_plan_file_open_is_non_destructive(),
        test_workbench_path_recovers_after_project_move(),
        test_reopened_plan_matches_unique_rpl_snapshot(),
        test_end_to_end_task_and_generation(),
        test_profile_sampling_task(),
        test_route_frame_chainage_matches_walk(),
        test_profile_cross_offset_sampling(),
        test_cross_offset_uses_contour_crossings(),
        test_profile_widget_axes_crosshair_toggles(),
        test_analysis_reuses_stored_depth_samples(),
        test_profile_step_resolution_and_staleness(),
        test_burial_depth_config_is_manual_only(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
