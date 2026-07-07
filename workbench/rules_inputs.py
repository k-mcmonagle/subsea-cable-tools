# -*- coding: utf-8 -*-
"""Acquisition layer for the route-suitability rules engine.

Turns survey data into ``RuleHit`` intervals (the KP ranges where each rule's
condition is TRUE) by sampling along an RPL route, then hands the ordered stack
to the pure ``rules_engine`` for resolution. Uses only ``qgis.core`` so it can
run headless like ``rpl_engine`` / ``depth_service``.

Sampling strategy: build one ``RouteSampler`` per run (route geometry + the KP
stations + their coordinates), then reuse those shared stations for every rule
so a 1000 km route is only walked once.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from . import rules_engine as eng
from . import schema
from .depth_service import DepthService, DepthSourceConfig
from .rules_engine import Interval, Rule, RuleHit

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

ProgressFn = Optional[Callable[[str], None]]


class RuleInputError(Exception):
    """Raised when a rule's inputs cannot be resolved; converted to a warning."""


# ---------------------------------------------------------------------------
# Route sampling
# ---------------------------------------------------------------------------


class RouteSampler:
    """Shared route geometry + KP stations for one assessment run."""

    def __init__(self, route: RouteFrame, stations_km: List[float],
                 coords: List[Optional[QgsPointXY]], distance):
        self.route = route
        self.stations_km = stations_km
        self.coords = coords  # parallel to stations_km; (x=lon, y=lat) or None
        self.distance = distance
        self.total_km = route.total_length_km

    @property
    def domain(self) -> Interval:
        return Interval(0.0, max(self.total_km, 1e-9))

    @classmethod
    def for_rpl(cls, store, rpl_id: str, project: Optional[QgsProject] = None,
                sample_step_m: float = 50.0) -> "RouteSampler":
        project = project or QgsProject.instance()
        rpl = store.get_rpl(rpl_id)
        if not rpl:
            raise RuleInputError(f"RPL {rpl_id} not found in the workbench store.")
        lines_layer = store.open_layer(rpl.get("lines_layer") or "")
        if lines_layer is None or not lines_layer.isValid():
            raise RuleInputError("RPL route (lines) layer could not be opened.")

        ordered = []
        for feat in lines_layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            try:
                seq = int(feat["SeqNo"])
            except (KeyError, TypeError, ValueError):
                seq = len(ordered)
            ordered.append((seq, QgsGeometry(geom)))
        ordered.sort(key=lambda t: t[0])
        geoms = [g for _, g in ordered]
        if not geoms:
            raise RuleInputError("RPL route has no usable line geometry.")

        distance = make_distance_area(WGS84, project.transformContext())
        route = RouteFrame.from_source(geoms, distance)

        stations = _build_stations(route, sample_step_m)
        coords = [route.point_at_kp(kp, clamp=True) for kp in stations]
        return cls(route, stations, coords, distance)


def _build_stations(route: RouteFrame, sample_step_m: float) -> List[float]:
    total_km = route.total_length_km
    step_km = max(float(sample_step_m), 1.0) / 1000.0
    marks = [0.0, total_km]
    # route vertices (feature boundaries) keep kinks in the depth/slope profile
    for off_m in route.feature_offsets_m:
        marks.append(off_m / 1000.0)
    kp = 0.0
    while kp < total_km:
        marks.append(kp)
        kp += step_km
    marks = [min(max(m, 0.0), total_km) for m in marks]
    marks.sort()
    unique: List[float] = []
    for m in marks:
        if not unique or m - unique[-1] > 1e-9:
            unique.append(m)
    return unique


# ---------------------------------------------------------------------------
# Feature helpers
# ---------------------------------------------------------------------------


def _resolve_layer(project: QgsProject, config: Dict) -> QgsVectorLayer:
    layer = None
    layer_id = config.get("layer_id")
    if layer_id:
        layer = project.mapLayer(layer_id)
    if layer is None and config.get("layer_source"):
        # Fallback for projects copied without stable layer ids.
        for cand in project.mapLayers().values():
            if isinstance(cand, QgsVectorLayer) and cand.source() == config.get("layer_source"):
                layer = cand
                break
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        raise RuleInputError("feature layer for the rule is missing from the project.")
    return layer


def _load_features_wgs84(layer: QgsVectorLayer, project: QgsProject
                         ) -> Tuple[QgsSpatialIndex, Dict[int, Tuple[QgsGeometry, QgsFeature]]]:
    xform = None
    if layer.crs() != WGS84:
        xform = QgsCoordinateTransform(layer.crs(), WGS84, project)
    index = QgsSpatialIndex()
    store: Dict[int, Tuple[QgsGeometry, QgsFeature]] = {}
    for i, feat in enumerate(layer.getFeatures()):
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        geom = QgsGeometry(geom)
        if xform is not None:
            try:
                geom.transform(xform)
            except Exception:
                continue
        store[i] = (geom, feat)
        idx_feat = QgsFeature()
        idx_feat.setId(i)
        idx_feat.setGeometry(geom)
        index.insertFeature(idx_feat)
    return index, store


def _search_rect(point: QgsPointXY, radius_m: float):
    from qgis.core import QgsRectangle
    deg = max(radius_m, 1.0) / 111320.0 + 1e-6
    return QgsRectangle(point.x() - deg, point.y() - deg, point.x() + deg, point.y() + deg)


def _distance_to_geom_m(distance, point: QgsPointXY, geom: QgsGeometry) -> float:
    try:
        nearest = geom.nearestPoint(QgsGeometry.fromPointXY(point))
        return float(distance.measureLine(point, nearest.asPoint()))
    except Exception:
        return float("inf")


def _filter_expression(expr_text: str):
    expr_text = (expr_text or "").strip()
    if not expr_text:
        return None, None
    expr = QgsExpression(expr_text)
    ctx = QgsExpressionContext()
    ctx.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(None))
    return expr, ctx


# ---------------------------------------------------------------------------
# Per-kind acquisition
# ---------------------------------------------------------------------------


def _depth_series(sampler: RouteSampler, store, rpl_id: str, project: QgsProject
                  ) -> List[Tuple[float, float]]:
    """(kp, depth-magnitude m) along the route. Prefers a configured bathy
    source; falls back to interpolating the RPL points' ApproxDepth."""
    config = DepthSourceConfig(store.rpl_depth_config(rpl_id))
    service = DepthService(config, project)
    series: List[Tuple[float, float]] = []
    if service.is_available():
        for kp, pt in zip(sampler.stations_km, sampler.coords):
            if pt is None:
                continue
            depth = service.sample(pt.y(), pt.x())
            if depth is not None:
                series.append((kp, abs(float(depth))))
    if not series:
        series = _rpl_depth_series(store, rpl_id)
    if not series:
        raise RuleInputError("no depth source configured and RPL has no ApproxDepth values.")
    return series


def _rpl_depth_series(store, rpl_id: str) -> List[Tuple[float, float]]:
    rpl = store.get_rpl(rpl_id)
    if not rpl:
        return []
    points = store.open_layer(rpl.get("points_layer") or "")
    if points is None or not points.isValid():
        return []
    out: List[Tuple[float, float]] = []
    for feat in points.getFeatures():
        try:
            kp = float(feat["DistCumulative"])
            depth = feat["ApproxDepth"]
        except (KeyError, TypeError, ValueError):
            continue
        if depth is None:
            continue
        try:
            out.append((kp, abs(float(depth))))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def _slope_series(depth_series: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Central-difference seabed slope (degrees) from a depth-magnitude series."""
    import math
    pts = sorted(depth_series)
    n = len(pts)
    out: List[Tuple[float, float]] = []
    for i in range(n):
        if n < 2:
            out.append((pts[i][0], 0.0))
            continue
        lo = max(0, i - 1)
        hi = min(n - 1, i + 1)
        dz = pts[hi][1] - pts[lo][1]
        dx_m = (pts[hi][0] - pts[lo][0]) * 1000.0
        slope = math.degrees(math.atan2(abs(dz), dx_m)) if dx_m > 1e-9 else 0.0
        out.append((pts[i][0], slope))
    return out


def _acquire_threshold(sampler, store, rpl_id, config, project) -> List[Interval]:
    profile = (config.get("profile") or "depth").lower()
    op = config.get("op") or ">"
    value = float(config.get("value", 0.0))
    value2 = config.get("value2")
    value2 = float(value2) if value2 is not None else None
    depth_series = _depth_series(sampler, store, rpl_id, project)
    if profile == "slope":
        series = _slope_series(depth_series)
    else:
        series = depth_series
    # depth is already a magnitude; slope already non-negative -> abs is a no-op.
    return eng.intervals_from_profile(series, op, value, value2, abs_value=bool(config.get("abs", False)))


def _acquire_proximity(sampler, config, project) -> List[Interval]:
    layer = _resolve_layer(project, config)
    distance_m = float(config.get("distance_m", 0.0))
    mode = config.get("mode", "distance")
    buffer_m = distance_m if mode == "distance" else 0.0
    index, feats = _load_features_wgs84(layer, project)
    geom_type = layer.geometryType()
    intervals: List[Interval] = []

    expr, ctx = _filter_expression(config.get("filter_expression", ""))

    def passes_filter(feat) -> bool:
        if expr is None:
            return True
        ctx.setFeature(feat)
        return bool(expr.evaluate(ctx))

    if geom_type == QgsWkbTypes.PointGeometry:
        # Chord method: exact per-feature, independent of station spacing.
        for geom, feat in feats.values():
            if not passes_filter(feat):
                continue
            pt = geom.centroid().asPoint() if geom.isMultipart() else geom.asPoint()
            hit = sampler.route.kp_at_point(QgsPointXY(pt))
            if hit.snapped_xy is None:
                continue
            if hit.dcc_m <= buffer_m + 1e-6:
                half = ((max(buffer_m, 0.0) ** 2 - hit.dcc_m ** 2) ** 0.5) / 1000.0
                intervals.append(Interval(hit.kp_km - half, hit.kp_km + half))
        return eng.clip_intervals(intervals, sampler.domain)

    # Line / polygon: per-station distance test (captures within-buffer proximity)
    series: List[Tuple[float, bool]] = []
    for kp, pt in zip(sampler.stations_km, sampler.coords):
        if pt is None:
            series.append((kp, False))
            continue
        flag = False
        for fid in index.intersects(_search_rect(pt, max(buffer_m, 1.0))):
            geom, feat = feats[fid]
            if not passes_filter(feat):
                continue
            if geom_type == QgsWkbTypes.PolygonGeometry and geom.contains(QgsGeometry.fromPointXY(pt)):
                flag = True
                break
            if _distance_to_geom_m(sampler.distance, pt, geom) <= buffer_m + 1e-6:
                flag = True
                break
        series.append((kp, flag))
    intervals = eng.intervals_from_bool_series(series, sampler.domain)

    # Exact crossings (thin features a coarse buffer might miss between stations).
    eps_km = max(buffer_m, 1.0) / 1000.0
    for geom, feat in feats.values():
        if not passes_filter(feat):
            continue
        for route_geom in sampler.route.geometries:
            inter = route_geom.intersection(geom)
            if inter is None or inter.isEmpty():
                continue
            for pt in _iter_points(inter):
                hit = sampler.route.kp_at_point(QgsPointXY(pt))
                if hit.snapped_xy is not None:
                    intervals.append(Interval(hit.kp_km - eps_km, hit.kp_km + eps_km))
    return eng.clip_intervals(intervals, sampler.domain)


def _acquire_polygon_class(sampler, config, project) -> List[Interval]:
    layer = _resolve_layer(project, config)
    attribute = config.get("attribute") or ""
    match_values = {str(v).strip().lower() for v in (config.get("match_values") or [])}
    expr, ctx = _filter_expression(config.get("match_expression", ""))
    index, feats = _load_features_wgs84(layer, project)

    def matches(feat) -> bool:
        if expr is not None:
            ctx.setFeature(feat)
            return bool(expr.evaluate(ctx))
        if not attribute:
            return True
        try:
            val = feat[attribute]
        except KeyError:
            return False
        return str(val).strip().lower() in match_values

    series: List[Tuple[float, bool]] = []
    for kp, pt in zip(sampler.stations_km, sampler.coords):
        if pt is None:
            series.append((kp, False))
            continue
        pt_geom = QgsGeometry.fromPointXY(pt)
        flag = False
        for fid in index.intersects(_search_rect(pt, 1.0)):
            geom, feat = feats[fid]
            if geom.contains(pt_geom) and matches(feat):
                flag = True
                break
        series.append((kp, flag))
    return eng.intervals_from_bool_series(series, sampler.domain)


def _acquire_kp_table(sampler, config, project) -> List[Interval]:
    layer = _resolve_layer(project, config)
    start_field = config.get("start_field") or "start_kp"
    end_field = config.get("end_field") or "end_kp"
    expr, ctx = _filter_expression(config.get("filter_expression", ""))
    intervals: List[Interval] = []
    for feat in layer.getFeatures():
        if expr is not None:
            ctx.setFeature(feat)
            if not bool(expr.evaluate(ctx)):
                continue
        try:
            s = float(feat[start_field])
            e = float(feat[end_field])
        except (KeyError, TypeError, ValueError):
            continue
        intervals.append(Interval(s, e))
    return eng.clip_intervals(intervals, sampler.domain)


def _acquire_manual(sampler, config) -> List[Interval]:
    intervals = []
    for rng in config.get("ranges", []):
        try:
            intervals.append(Interval(float(rng["start_kp"]), float(rng["end_kp"])))
        except (KeyError, TypeError, ValueError):
            continue
    return eng.clip_intervals(intervals, sampler.domain)


def _iter_points(geom: QgsGeometry):
    try:
        if geom.wkbType() in (QgsWkbTypes.Point, QgsWkbTypes.PointZ, QgsWkbTypes.PointM):
            yield geom.asPoint()
            return
        if geom.isMultipart():
            for p in geom.asMultiPoint():
                yield p
            return
        # line/other intersection: fall back to vertices
        for v in geom.vertices():
            yield QgsPointXY(v)
    except Exception:
        return


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _rule_from_row(row: Dict) -> Rule:
    try:
        methods = json.loads(row.get("methods_json") or "[]")
    except (ValueError, TypeError):
        methods = []
    return Rule(
        rule_id=str(row.get("rule_id")),
        name=row.get("name") or "",
        seq=int(row.get("seq") or 0),
        action=row.get("action") or eng.ACTION_EXCLUDE,
        risk_level=int(row.get("risk_level") or 0),
        methods=list(methods),
        enabled=bool(int(row.get("enabled") or 0)),
        kind=row.get("kind") or "",
    )


def _scope_intervals(config: Dict) -> Optional[List[Interval]]:
    scope = config.get("scope_ranges")
    if not scope:
        return None
    out = []
    for rng in scope:
        try:
            out.append(Interval(float(rng["start_kp"]), float(rng["end_kp"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out or None


def acquire_hits(sampler: RouteSampler, store, rpl_id: str, rule_rows: Sequence[Dict],
                 project: QgsProject, progress: ProgressFn = None
                 ) -> Tuple[List[RuleHit], List[str]]:
    hits: List[RuleHit] = []
    warnings: List[str] = []
    for row in rule_rows:
        rule = _rule_from_row(row)
        try:
            config = json.loads(row.get("config_json") or "{}")
        except (ValueError, TypeError):
            config = {}
        if progress:
            progress(f"Evaluating rule: {rule.name}")
        intervals: List[Interval] = []
        try:
            if rule.kind == schema.RULE_KIND_THRESHOLD:
                intervals = _acquire_threshold(sampler, store, rpl_id, config, project)
            elif rule.kind == schema.RULE_KIND_PROXIMITY:
                intervals = _acquire_proximity(sampler, config, project)
            elif rule.kind == schema.RULE_KIND_POLYGON:
                intervals = _acquire_polygon_class(sampler, config, project)
            elif rule.kind == schema.RULE_KIND_KP_TABLE:
                intervals = _acquire_kp_table(sampler, config, project)
            elif rule.kind == schema.RULE_KIND_MANUAL:
                intervals = _acquire_manual(sampler, config)
            else:
                warnings.append(f"Rule '{rule.name}': unknown kind '{rule.kind}' — skipped.")
        except RuleInputError as exc:
            warnings.append(f"Rule '{rule.name}': {exc} — skipped.")
            intervals = []
        except Exception as exc:  # never let one rule crash the run
            warnings.append(f"Rule '{rule.name}': unexpected error ({exc}) — skipped.")
            intervals = []

        scope = _scope_intervals(config)
        if scope is not None:
            intervals = eng.intersect_intervals(intervals, scope)
        hits.append(RuleHit(rule, intervals))
    return hits, warnings


def run_assessment(store, rpl_id: str, rule_set_id: str, *, sample_step_m: float = 50.0,
                   min_range_km: float = 0.0, project: Optional[QgsProject] = None,
                   progress: ProgressFn = None
                   ) -> Tuple[eng.AssessmentResult, RouteSampler]:
    """Sample the route, evaluate the rule stack, return (result, sampler)."""
    project = project or QgsProject.instance()
    rule_set = store.get_rule_set(rule_set_id)
    if not rule_set:
        raise RuleInputError(f"Rule set {rule_set_id} not found.")
    try:
        methods = json.loads(rule_set.get("methods_json") or "[]")
    except (ValueError, TypeError):
        methods = list(schema.DEFAULT_ASSESSMENT_METHODS)
    if not methods:
        methods = list(schema.DEFAULT_ASSESSMENT_METHODS)

    sampler = RouteSampler.for_rpl(store, rpl_id, project, sample_step_m)
    rule_rows = store.list_rules(rule_set_id)
    hits, warnings = acquire_hits(sampler, store, rpl_id, rule_rows, project, progress)

    result = eng.evaluate(sampler.domain, methods, hits, min_range_km=min_range_km)
    result.warnings = warnings
    return result, sampler
