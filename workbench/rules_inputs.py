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
)

from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..qgis_compat import (
    GEOMETRY_POINT,
    GEOMETRY_POLYGON,
    WKB_POINT,
    WKB_POINT_M,
    WKB_POINT_Z,
)
from . import rules_engine as eng
from . import schema
from .depth_service import DepthService, DepthSourceConfig
from .rules_engine import Interval, Rule, RuleHit

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

ProgressFn = Optional[Callable[[str], None]]


class RuleInputError(Exception):
    """Raised when a rule's inputs cannot be resolved; converted to a warning."""


class AcquisitionCancelled(Exception):
    """Raised by compute functions when their ``cancel`` callback fires."""


_CANCEL_CHUNK = 2000  # stations between cooperative cancel checks


# ---------------------------------------------------------------------------
# Route sampling
# ---------------------------------------------------------------------------


class RouteSampler:
    """Shared route geometry + KP stations for one assessment run.

    When ``scope`` is given, stations are built only within the scoped KP
    window plus one coarse-step margin on each side (for slope differencing),
    so acquisition cost scales with the reviewed extent. Omitting ``scope``
    preserves the original whole-route behaviour.
    """

    def __init__(self, route: RouteFrame, stations_km: List[float],
                 coords: List[Optional[QgsPointXY]], distance,
                 scope: Optional[Interval] = None,
                 step_km: Optional[float] = None):
        self.route = route
        self.stations_km = stations_km
        self.coords = coords  # parallel to stations_km; (x=lon, y=lat) or None
        self.distance = distance
        self.total_km = route.total_length_km
        self.scope = scope
        self.step_km = step_km  # regular sampling step (slope half-window)
        self._depth_series_cache: Dict[str, List[Tuple[float, float]]] = {}

    @property
    def domain(self) -> Interval:
        return Interval(0.0, max(self.total_km, 1e-9))

    @property
    def scope_domain(self) -> Interval:
        """The scoped analysis window (falls back to the full route)."""
        if self.scope is None:
            return self.domain
        lo = max(0.0, min(self.scope.start_km, self.scope.end_km))
        hi = min(self.total_km, max(self.scope.start_km, self.scope.end_km))
        return Interval(lo, max(hi, lo + 1e-9))

    @classmethod
    def for_rpl(cls, store, rpl_id: str, project: Optional[QgsProject] = None,
                sample_step_m: float = 50.0,
                scope: Optional[Interval] = None) -> "RouteSampler":
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
        return cls.from_route(route, distance, sample_step_m, scope)

    @classmethod
    def from_route(cls, route: RouteFrame, distance, sample_step_m: float = 50.0,
                   scope: Optional[Interval] = None) -> "RouteSampler":
        """Build a sampler over an already-constructed RouteFrame.

        Useful for callers that assembled the route from cloned geometries
        (e.g. a background task's thread-safe snapshot).
        """
        stations = _build_stations(route, sample_step_m, scope)
        coords = [route.point_at_kp(kp, clamp=True) for kp in stations]
        return cls(route, stations, coords, distance, scope,
                   step_km=max(float(sample_step_m), 1.0) / 1000.0)


def _build_stations(route: RouteFrame, sample_step_m: float,
                    scope: Optional[Interval] = None) -> List[float]:
    total_km = route.total_length_km
    step_km = max(float(sample_step_m), 1.0) / 1000.0
    if scope is None:
        lo, hi = 0.0, total_km
    else:
        s = min(scope.start_km, scope.end_km)
        e = max(scope.start_km, scope.end_km)
        lo = max(0.0, s - step_km)   # one-step margin for slope differencing
        hi = min(total_km, e + step_km)
    marks = [lo, hi]
    # route vertices (feature boundaries) keep kinks in the depth/slope profile
    for off_m in route.feature_offsets_m:
        m = off_m / 1000.0
        if lo - 1e-9 <= m <= hi + 1e-9:
            marks.append(m)
    kp = lo
    while kp < hi:
        marks.append(kp)
        kp += step_km
    marks = [min(max(m, lo), hi) for m in marks]
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
        index.addFeature(idx_feat)
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


def slope_half_window_km(config: Dict, step_km: Optional[float]) -> Optional[float]:
    """Half-window (km) for slope differencing at a rule's evaluation scale.

    ``slope_window_m`` in the rule config is the full evaluation length —
    typically the burial vehicle's bearing length — so slope is the depth
    difference across that footprint. Unset/0 falls back to the acquisition
    step (window = 2 × step), the pre-existing behaviour.
    """
    try:
        window_m = float(config.get("slope_window_m") or 0.0)
    except (TypeError, ValueError):
        window_m = 0.0
    if window_m > 0:
        return max(window_m, 2.0) / 2000.0
    return step_km


def _slope_series(depth_series: List[Tuple[float, float]],
                  half_window_km: Optional[float] = None
                  ) -> List[Tuple[float, float]]:
    """Unsigned seabed slope (degrees): magnitude of the shared signed series."""
    return [(kp, abs(slope))
            for kp, slope in eng.signed_slope_series(depth_series, half_window_km)]


def depth_series_with_gaps(sampler: RouteSampler, sample_fn,
                           cancel: Optional[Callable[[], bool]] = None
                           ) -> Tuple[List[Tuple[float, float]], List[Interval]]:
    """(kp, depth-magnitude) series plus the KP intervals with no depth data.

    ``sample_fn(lat, lon) -> Optional[float]`` is the depth source. Gap
    intervals are derived by midpoint ownership over the no-data stations,
    clipped to the sampler's scoped domain. Callers may ignore the gaps
    (Assessment behaviour) or surface them as Insufficient Information.
    """
    series: List[Tuple[float, float]] = []
    flags: List[Tuple[float, bool]] = []
    for station_index, (kp, pt) in enumerate(zip(sampler.stations_km, sampler.coords)):
        if cancel is not None and station_index % _CANCEL_CHUNK == 0 and cancel():
            raise AcquisitionCancelled()
        depth = sample_fn(pt.y(), pt.x()) if pt is not None else None
        if depth is None:
            flags.append((kp, True))
        else:
            series.append((kp, abs(float(depth))))
            flags.append((kp, False))
    gaps = eng.intervals_from_bool_series(flags, sampler.scope_domain)
    return series, gaps


def threshold_intervals(depth_series: List[Tuple[float, float]], config: Dict,
                        domain: Interval,
                        step_km: Optional[float] = None) -> List[Interval]:
    """Threshold/slope intervals from a depth-magnitude series (thread-safe).

    Supports the original unsigned depth/slope comparison plus the signed
    directional slope (``slope_signed`` with ``downslope_max_deg`` /
    ``upslope_max_deg``; positive slope = shoaling/up-slope with KP) and optional
    WD-banded limits (``bands``: per-band ``limit`` or, for signed slope,
    ``downslope_limit`` / ``upslope_limit``). ``step_km`` is the acquisition
    sampling step, used as the slope half-window so the coarse series and
    the boundary-refinement predicate measure slope at the same scale
    (median station spacing when omitted).
    """
    profile = (config.get("profile") or "depth").lower()
    op = config.get("op") or ">"
    signed = bool(config.get("slope_signed")) and profile == "slope"
    bands = config.get("bands") or []

    if profile == "slope":
        half_km = slope_half_window_km(config, step_km)
        series = (eng.signed_slope_series(depth_series, half_km) if signed
                  else _slope_series(depth_series, half_km))
    else:
        series = depth_series

    if bands:
        if signed:
            wd_by_kp = {round(kp, 9): wd for kp, wd in depth_series}
            flags: List[Tuple[float, bool]] = []
            for kp, slope in series:
                wd = wd_by_kp.get(round(kp, 9))
                band = eng.select_band(bands, wd) if wd is not None else None
                fired = False
                if band is not None:
                    down = band.get("downslope_limit", band.get("limit"))
                    up = band.get("upslope_limit", band.get("limit"))
                    # +ve slope = shoaling: up-slope limit governs the
                    # positive side, down-slope limit the negative side.
                    if down is not None and slope < -abs(float(down)):
                        fired = True
                    if up is not None and slope > abs(float(up)):
                        fired = True
                flags.append((kp, fired))
            return eng.intervals_from_bool_series(flags, domain)
        return eng.intervals_from_banded_threshold(series, depth_series, bands, op, domain)

    if signed:
        return eng.intervals_from_signed_slope(
            series, config.get("downslope_max_deg"), config.get("upslope_max_deg"))

    value = float(config.get("value", 0.0))
    value2 = config.get("value2")
    value2 = float(value2) if value2 is not None else None
    # depth is already a magnitude; unsigned slope non-negative -> abs is a no-op.
    return eng.intervals_from_profile(series, op, value, value2,
                                      abs_value=bool(config.get("abs", False)))


def _acquire_threshold(sampler, store, rpl_id, config, project) -> List[Interval]:
    # One route walk per run, not per rule: several threshold rules (depth +
    # slope limits) share the same stations, and each walk costs a provider
    # sample per station.
    cache = getattr(sampler, "_depth_series_cache", None)
    if cache is not None and rpl_id in cache:
        depth_series = cache[rpl_id]
    else:
        depth_series = _depth_series(sampler, store, rpl_id, project)
        if cache is not None:
            cache[rpl_id] = depth_series
    return threshold_intervals(depth_series, config, sampler.domain,
                               step_km=getattr(sampler, "step_km", None))


def _feature_buffer_m(feat, buffer_field: str, default_m: float) -> float:
    """Per-feature buffer override (``buffer_field``), else the blanket value."""
    if buffer_field:
        try:
            value = float(feat[buffer_field])
            if value == value and value >= 0.0:  # not NaN, not negative
                return value
        except (KeyError, TypeError, ValueError):
            pass
    return default_m


def proximity_intervals(sampler: RouteSampler, index: QgsSpatialIndex,
                        feats: Dict[int, Tuple[QgsGeometry, QgsFeature]],
                        geom_type, config: Dict,
                        cancel: Optional[Callable[[], bool]] = None) -> List[Interval]:
    """Proximity intervals over pre-loaded WGS84 features (thread-safe:
    touches only the supplied snapshot, never the project or live layers).
    ``cancel`` is checked every ~2000 stations and raises
    ``AcquisitionCancelled`` when it returns True."""
    distance_m = float(config.get("distance_m", 0.0))
    mode = config.get("mode", "distance")
    buffer_m = distance_m if mode == "distance" else 0.0
    buffer_field = (config.get("buffer_field") or "").strip()
    intervals: List[Interval] = []

    expr, ctx = _filter_expression(config.get("filter_expression", ""))

    def passes_filter(feat) -> bool:
        if expr is None:
            return True
        ctx.setFeature(feat)
        return bool(expr.evaluate(ctx))

    if geom_type == GEOMETRY_POINT:
        # Chord method: exact per-feature, independent of station spacing.
        for geom, feat in feats.values():
            if not passes_filter(feat):
                continue
            fb = _feature_buffer_m(feat, buffer_field, buffer_m)
            pt = geom.centroid().asPoint() if geom.isMultipart() else geom.asPoint()
            hit = sampler.route.kp_at_point(QgsPointXY(pt))
            if hit.snapped_xy is None:
                continue
            if hit.dcc_m <= fb + 1e-6:
                half = ((max(fb, 0.0) ** 2 - hit.dcc_m ** 2) ** 0.5) / 1000.0
                intervals.append(Interval(hit.kp_km - half, hit.kp_km + half))
        return eng.clip_intervals(intervals, sampler.domain)

    # Largest buffer bounds the spatial-index search window.
    max_buffer_m = buffer_m
    if buffer_field:
        for _geom, feat in feats.values():
            max_buffer_m = max(max_buffer_m, _feature_buffer_m(feat, buffer_field, buffer_m))

    # Line / polygon: per-station distance test (captures within-buffer proximity)
    series: List[Tuple[float, bool]] = []
    for station_index, (kp, pt) in enumerate(zip(sampler.stations_km, sampler.coords)):
        if cancel is not None and station_index % _CANCEL_CHUNK == 0 and cancel():
            raise AcquisitionCancelled()
        if pt is None:
            series.append((kp, False))
            continue
        flag = False
        for fid in index.intersects(_search_rect(pt, max(max_buffer_m, 1.0))):
            geom, feat = feats[fid]
            if not passes_filter(feat):
                continue
            fb = _feature_buffer_m(feat, buffer_field, buffer_m)
            if (geom_type == GEOMETRY_POLYGON and
                    geom.contains(QgsGeometry.fromPointXY(pt))):
                flag = True
                break
            if _distance_to_geom_m(sampler.distance, pt, geom) <= fb + 1e-6:
                flag = True
                break
        series.append((kp, flag))
    intervals = eng.intervals_from_bool_series(series, sampler.domain)

    # Exact crossings (thin features a coarse buffer might miss between stations).
    for geom, feat in feats.values():
        if not passes_filter(feat):
            continue
        eps_km = max(_feature_buffer_m(feat, buffer_field, buffer_m), 1.0) / 1000.0
        for route_geom in sampler.route.geometries:
            inter = route_geom.intersection(geom)
            if inter is None or inter.isEmpty():
                continue
            for pt in _iter_points(inter):
                hit = sampler.route.kp_at_point(QgsPointXY(pt))
                if hit.snapped_xy is not None:
                    intervals.append(Interval(hit.kp_km - eps_km, hit.kp_km + eps_km))
    return eng.clip_intervals(intervals, sampler.domain)


def _acquire_proximity(sampler, config, project) -> List[Interval]:
    layer = _resolve_layer(project, config)
    index, feats = _load_features_wgs84(layer, project)
    return proximity_intervals(sampler, index, feats, layer.geometryType(), config)


def polygon_class_intervals(sampler: RouteSampler, index: QgsSpatialIndex,
                            feats: Dict[int, Tuple[QgsGeometry, QgsFeature]],
                            config: Dict,
                            cancel: Optional[Callable[[], bool]] = None) -> List[Interval]:
    """Polygon-class intervals over pre-loaded WGS84 features (thread-safe)."""
    attribute = config.get("attribute") or ""
    match_values = {str(v).strip().lower() for v in (config.get("match_values") or [])}
    expr, ctx = _filter_expression(config.get("match_expression", ""))

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
    for station_index, (kp, pt) in enumerate(zip(sampler.stations_km, sampler.coords)):
        if cancel is not None and station_index % _CANCEL_CHUNK == 0 and cancel():
            raise AcquisitionCancelled()
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


def _acquire_polygon_class(sampler, config, project) -> List[Interval]:
    layer = _resolve_layer(project, config)
    index, feats = _load_features_wgs84(layer, project)
    return polygon_class_intervals(sampler, index, feats, config)


def kp_table_intervals(rows: Sequence[Dict], config: Dict, domain: Interval
                       ) -> List[Interval]:
    """KP-range intervals from plain row dicts (thread-safe)."""
    start_field = config.get("start_field") or "start_kp"
    end_field = config.get("end_field") or "end_kp"
    intervals: List[Interval] = []
    for row in rows:
        try:
            s = float(row[start_field])
            e = float(row[end_field])
        except (KeyError, TypeError, ValueError):
            continue
        intervals.append(Interval(s, e))
    return eng.clip_intervals(intervals, domain)


def _acquire_kp_table(sampler, config, project) -> List[Interval]:
    layer = _resolve_layer(project, config)
    expr, ctx = _filter_expression(config.get("filter_expression", ""))
    rows: List[Dict] = []
    names = [f.name() for f in layer.fields()]
    for feat in layer.getFeatures():
        if expr is not None:
            ctx.setFeature(feat)
            if not bool(expr.evaluate(ctx)):
                continue
        rows.append({name: feat[name] for name in names})
    return kp_table_intervals(rows, config, sampler.domain)


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
        if geom.wkbType() in (WKB_POINT, WKB_POINT_Z, WKB_POINT_M):
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
