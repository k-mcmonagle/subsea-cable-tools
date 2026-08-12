# -*- coding: utf-8 -*-
"""Checks for Burial Planner acquisition plumbing (requires the QGIS API).

Scoped sampler == full sampler clipped to scope; threshold acquisition with
signed slope and no-data gaps; per-feature buffer override; cooperative
cancellation raising cleanly; direction mapping of slope limits.
"""

from __future__ import annotations

import json

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)

from ..burial import analysis_task, generation
from ..burial import schema as burial_schema
from ..burial.plan_model import PlanModel
from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..workbench import rules_engine as eng
from ..workbench import rules_inputs as ri
from ..workbench.rules_engine import Interval

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
        test_end_to_end_task_and_generation(),
        test_profile_sampling_task(),
        test_burial_depth_config_is_manual_only(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
