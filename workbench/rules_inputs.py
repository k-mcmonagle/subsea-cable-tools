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
import math
from dataclasses import dataclass
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
    QgsTask,
    QgsVectorLayer,
    QgsVectorLayerFeatureSource,
)
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QTransform

from ..burial import attribute_rules
from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_kp_distance_area
from ..plugin_log import log_exception
from ..qgis_compat import (
    GEOMETRY_POINT,
    GEOMETRY_POLYGON,
    WKB_POINT,
    WKB_POINT_M,
    WKB_POINT_Z,
)
from . import rules_engine as eng
from . import schema
from .depth_service import DepthSourceConfig
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
        # KP domain: RPLs may start at a non-zero KP.
        self.start_km = float(getattr(route, "start_kp_km", 0.0) or 0.0)
        self.end_km = self.start_km + self.total_km
        self.scope = scope
        self.step_km = step_km  # regular sampling step (slope half-window)

    @property
    def domain(self) -> Interval:
        return Interval(self.start_km, max(self.end_km, self.start_km + 1e-9))

    @property
    def scope_domain(self) -> Interval:
        """The scoped analysis window (falls back to the full route)."""
        if self.scope is None:
            return self.domain
        lo = max(self.start_km, min(self.scope.start_km, self.scope.end_km))
        hi = min(self.end_km, max(self.scope.start_km, self.scope.end_km))
        return Interval(lo, max(hi, lo + 1e-9))

    @classmethod
    def for_rpl(cls, store, rpl_id: str, project: Optional[QgsProject] = None,
                sample_step_m: float = 50.0,
                scope: Optional[Interval] = None) -> "RouteSampler":
        route, distance = route_for_rpl(store, rpl_id, project)
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


def route_for_rpl(store, rpl_id: str, project: Optional[QgsProject] = None
                  ) -> Tuple[RouteFrame, object]:
    """(RouteFrame over cloned WGS84 geometries, distance area) of an RPL.

    Main thread (reads the store and the RPL lines layer); the returned
    route owns its geometries, so a worker thread may sample it.
    """
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
    from ..kp_geo_utils import crosses_antimeridian

    if crosses_antimeridian(geoms):
        raise RuleInputError(
            "the route crosses the ±180° antimeridian, which the "
            "assessment geometry does not support — positions and "
            "intersections would be silently wrong")

    distance = make_kp_distance_area(WGS84, project.transformContext())
    route = RouteFrame.from_source(geoms, distance)
    return route, distance


def _build_stations(route: RouteFrame, sample_step_m: float,
                    scope: Optional[Interval] = None) -> List[float]:
    first_km = float(getattr(route, "start_kp_km", 0.0) or 0.0)
    total_km = first_km + route.total_length_km   # route end KP
    step_km = max(float(sample_step_m), 1.0) / 1000.0
    if scope is None:
        lo, hi = first_km, total_km
    else:
        s = min(scope.start_km, scope.end_km)
        e = max(scope.start_km, scope.end_km)
        lo = max(first_km, s - step_km)   # one-step margin for slope differencing
        hi = min(total_km, e + step_km)
    marks = [lo, hi]
    # route vertices (feature boundaries) keep kinks in the depth/slope profile
    for off_m in route.feature_offsets_m:
        m = first_km + off_m / 1000.0
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


def _load_features_wgs84_from_source(
        source, crs, transform_context,
        cancel: Optional[Callable[[], bool]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
        feature_count: int = 0,
) -> Tuple[QgsSpatialIndex, Dict[int, Tuple[QgsGeometry, QgsFeature]]]:
    """Worker-thread twin of ``_load_features_wgs84``.

    Takes a ``QgsVectorLayerFeatureSource`` snapshot + its CRS + the
    project's transform context (all captured on the main thread), so the
    expensive feature iteration, reprojection and spatial-index build can
    run inside a QgsTask with cooperative cancellation instead of freezing
    the UI before the task starts.
    """
    xform = None
    if crs != WGS84:
        xform = QgsCoordinateTransform(crs, WGS84, transform_context)
    index = QgsSpatialIndex()
    store: Dict[int, Tuple[QgsGeometry, QgsFeature]] = {}
    total = max(int(feature_count), 1)
    for i, feat in enumerate(source.getFeatures()):
        if i % 500 == 0:
            if cancel is not None and cancel():
                raise AcquisitionCancelled()
            if progress is not None:
                progress(min(i, total), total)
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
    """Candidate-search rectangle around a WGS84 point (exact tests follow).

    Longitude degrees shrink with latitude, so the metre->degree conversion
    must divide by cos(latitude) — the previous equatorial-only conversion
    made the window ~35% too narrow east-west at 50° N and could miss
    features lying near the buffer edge. Conservative (slightly oversized)
    by construction; the caller's distance test decides.
    """
    import math

    from qgis.core import QgsRectangle
    radius = max(radius_m, 1.0)
    deg_lat = radius / 110540.0 + 1e-6
    cos_lat = max(math.cos(math.radians(point.y())), 0.087)
    deg_lon = radius / (111320.0 * cos_lat) + 1e-6
    return QgsRectangle(point.x() - deg_lon, point.y() - deg_lat,
                        point.x() + deg_lon, point.y() + deg_lat)


def _isotropic_nearest(point: QgsPointXY, geom: QgsGeometry,
                       scaled_cache: Optional[Dict] = None) -> QgsPointXY:
    """Nearest point on ``geom`` (WGS84) to ``point``, anisotropy-corrected.

    A planar ``nearestPoint`` in raw lon/lat minimises in a frame where the
    east axis is stretched by 1/cos(latitude), so for any non-axis-aligned
    edge it picks the wrong point and the measured distance is always an
    overestimate (up to ~+17 % at 60°, ~+30 % at 70° for diagonal edges) —
    which silently *shrank* metre-threshold buffers. Minimising in a
    locally-isotropic frame (longitude × cos latitude at the geometry's own
    latitude) removes that bias; the geodesic measurement then happens on
    the corrected point.

    ``scaled_cache`` (keyed by ``id(geom)``, caller-scoped so entries never
    outlive the geometry objects) avoids rebuilding the scaled copy per
    station.
    """
    entry = scaled_cache.get(id(geom)) if scaled_cache is not None else None
    if entry is None:
        lat = geom.boundingBox().center().y()
        cos_lat = max(math.cos(math.radians(lat)), 1e-6)
        scaled = QgsGeometry(geom)
        scaled.transform(QTransform().scale(cos_lat, 1.0))
        entry = (scaled, cos_lat)
        if scaled_cache is not None:
            scaled_cache[id(geom)] = entry
    scaled, cos_lat = entry
    query = QgsGeometry.fromPointXY(QgsPointXY(point.x() * cos_lat,
                                               point.y()))
    nearest = scaled.nearestPoint(query).asPoint()
    return QgsPointXY(nearest.x() / cos_lat, nearest.y())


def _distance_to_geom_m(distance, point: QgsPointXY, geom: QgsGeometry,
                        scaled_cache: Optional[Dict] = None) -> float:
    """Geodesic metres from ``point`` to the nearest point of ``geom``."""
    try:
        nearest = _isotropic_nearest(point, geom, scaled_cache)
        return float(distance.measureLine(point, nearest))
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


class DepthInputs:
    """Depth source for threshold rules, captured on the main thread.

    Holds a thread-safe :class:`~burial.analysis_task.DepthSnapshot` of the
    RPL's configured bathymetry plus the RPL positions' ApproxDepth values
    (the fallback), so :meth:`series` can run on a worker thread without
    touching the store, the project or a live layer.
    """

    def __init__(self, snapshot, rpl_points: List[Tuple[float, float, Optional[QgsPointXY]]]):
        self.snapshot = snapshot
        # (stated KP, depth, WGS84 position or None) per RPL position
        self.rpl_points = rpl_points
        self._series: Optional[List[Tuple[float, float]]] = None
        self._series_sampler = None

    @classmethod
    def capture(cls, store, rpl_id: str, project: QgsProject) -> "DepthInputs":
        """Main thread: snapshot the depth sources of one RPL."""
        from ..burial.analysis_task import DepthSnapshot

        config = DepthSourceConfig(store.rpl_depth_config(rpl_id))
        return cls(DepthSnapshot(config, project), _rpl_depth_points(store, rpl_id))

    def series(self, sampler: RouteSampler,
               cancel: Optional[Callable[[], bool]] = None) -> List[Tuple[float, float]]:
        """(kp, depth-magnitude m) along the route; computed once per sampler.

        Prefers the configured bathymetry; falls back to interpolating the
        RPL points' ApproxDepth.
        """
        if self._series is not None and self._series_sampler is sampler:
            return self._series
        series: List[Tuple[float, float]] = []
        snapshot = self.snapshot
        if snapshot is not None and snapshot.is_available():
            if not snapshot.prepare(cancel=cancel):
                raise AcquisitionCancelled()
            series = snapshot.profile_samples(sampler.route, sampler.stations_km,
                                              cancel=cancel)
            sampler._depth_metadata = {'sources': snapshot.profile_sources,
                                       'cells': snapshot.profile_cells}
        if not series:
            series = _rpl_depth_series_from_points(self.rpl_points, sampler.route)
        if not series:
            raise RuleInputError("no depth source configured and RPL has no ApproxDepth values.")
        self._series, self._series_sampler = series, sampler
        return series


def _rpl_depth_points(store, rpl_id: str) -> List[Tuple[float, float, Optional[QgsPointXY]]]:
    """Main thread: (stated KP, depth, WGS84 point) of each RPL position."""
    rpl = store.get_rpl(rpl_id)
    if not rpl:
        return []
    points = store.open_layer(rpl.get("points_layer") or "")
    if points is None or not points.isValid():
        return []
    xform = None
    if points.crs() != WGS84:
        xform = QgsCoordinateTransform(points.crs(), WGS84, QgsProject.instance())
    out: List[Tuple[float, float, Optional[QgsPointXY]]] = []
    for feat in points.getFeatures():
        try:
            kp = float(feat["DistCumulative"])
            depth = float(feat["ApproxDepth"])
        except (KeyError, TypeError, ValueError):
            continue  # no KP, or no/non-numeric depth (NULL included)
        position = None
        geom = feat.geometry()
        if geom is not None and not geom.isEmpty():
            try:
                position = QgsPointXY(geom.asPoint())
                if xform is not None:
                    position = xform.transform(position)
            except Exception:  # noqa: BLE001 - non-point geometry: use stated KP
                position = None
        out.append((kp, depth, position))
    return out


def _rpl_depth_series_from_points(points, route=None) -> List[Tuple[float, float]]:
    """RPL ApproxDepth keyed by KP. With ``route`` (the sampler's WGS84
    RouteFrame) each position's KP is *measured* on the route, the same KP
    the stations use; the stated DistCumulative is only a fallback."""
    out: List[Tuple[float, float]] = []
    for kp, depth, position in points:
        if route is not None and position is not None:
            try:
                hit = route.kp_at_point(position)
                if hit.snapped_xy is not None:
                    kp = hit.kp_km
            except Exception:  # noqa: BLE001 - keep the stated KP
                pass
        out.append((kp, abs(depth)))
    out.sort()
    return out


def _rpl_depth_series(store, rpl_id: str, route=None) -> List[Tuple[float, float]]:
    """RPL ApproxDepth keyed by KP (see :func:`_rpl_depth_series_from_points`)."""
    return _rpl_depth_series_from_points(_rpl_depth_points(store, rpl_id), route)


def slope_half_window_km(config: Dict, step_km: Optional[float]) -> Optional[float]:
    """Half-window (km) for slope differencing at a rule's evaluation scale.

    ``slope_window_m`` in the rule config is the full evaluation length —
    typically the burial vehicle's bearing length — so slope is the depth
    difference across that footprint. Unset/0 falls back to the supplied
    profile station step (window = 2 × step). Workbench Assessment supplies
    its acquisition step; the Burial Planner supplies its denser persisted-
    profile step so short, steep terrain is not averaged over the unrelated
    coarse rule-search interval.
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
    return [(kp, None if slope is None else abs(slope))
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
                        step_km: Optional[float] = None,
                        prepared_slope_series: Optional[
                            List[Tuple[float, float]]] = None,
                        ) -> List[Interval]:
    """Threshold/slope intervals from a depth-magnitude series (thread-safe).

    Supports the original unsigned depth/slope comparison plus the signed
    directional slope (``slope_signed`` with ``downslope_max_deg`` /
    ``upslope_max_deg``; positive slope = shoaling/up-slope with KP) and optional
    WD-banded limits (``bands``: per-band ``limit`` or, for signed slope,
    ``downslope_limit`` / ``upslope_limit``). ``step_km`` is the acquisition
    profile sampling step, used as the local-slope half-window so acquisition
    and the boundary-refinement predicate measure slope at the same scale
    (median station spacing when omitted). ``prepared_slope_series`` lets a
    long-route caller derive a slope array once and reuse it across rules that
    share an evaluation length.
    """
    profile = (config.get("profile") or "depth").lower()
    op = config.get("op") or ">"
    signed = bool(config.get("slope_signed")) and profile == "slope"
    bands = config.get("bands") or []

    if profile == "slope":
        if prepared_slope_series is not None:
            series = prepared_slope_series
        else:
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
                if band is not None and slope is not None:
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


def _acquire_threshold(sampler, depth: DepthInputs, config,
                       cancel: Optional[Callable[[], bool]] = None) -> List[Interval]:
    # One route walk per run, not per rule: several threshold rules (depth +
    # slope limits) share the same stations, and each walk costs a provider
    # sample per station (DepthInputs caches the series per sampler).
    depth_series = depth.series(sampler, cancel)
    prepared = None
    if (config.get('profile') or '').lower() == 'slope':
        from ..burial.profile_data import long_slope_series
        metadata = getattr(sampler, '_depth_metadata', {})
        half = slope_half_window_km(config, getattr(sampler,'step_km',None)) or 0
        prepared = long_slope_series([kp for kp,z in depth_series], [z for kp,z in depth_series],
            half if config.get('slope_window_m') else 0, metadata.get('sources'),metadata.get('cells'))
        if not config.get('slope_signed'):
            prepared = [(kp,None if z is None else abs(z)) for kp,z in prepared]
    return threshold_intervals(depth_series, config, sampler.domain,
                               step_km=getattr(sampler, 'step_km', None), prepared_slope_series=prepared)



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
        # Multipoint features contribute one chord per constituent point —
        # collapsing them to a centroid placed the hit where nothing exists.
        for geom, feat in feats.values():
            if not passes_filter(feat):
                continue
            fb = _feature_buffer_m(feat, buffer_field, buffer_m)
            if geom.isMultipart():
                points = [QgsPointXY(p) for p in geom.asMultiPoint()]
            else:
                points = [QgsPointXY(geom.asPoint())]
            for pt in points:
                hit = sampler.route.kp_at_point(pt)
                if hit.snapped_xy is None:
                    continue
                if hit.dcc_m <= fb + 1e-6:
                    half = ((max(fb, 0.0) ** 2 - hit.dcc_m ** 2) ** 0.5) / 1000.0
                    intervals.append(Interval(hit.kp_km - half,
                                              hit.kp_km + half))
        return eng.clip_intervals(intervals, sampler.domain)

    # Largest buffer bounds the spatial-index search window.
    max_buffer_m = buffer_m
    if buffer_field:
        for _geom, feat in feats.values():
            max_buffer_m = max(max_buffer_m, _feature_buffer_m(feat, buffer_field, buffer_m))

    # Line / polygon: per-station distance test (captures within-buffer proximity)
    series: List[Tuple[float, bool]] = []
    scaled_cache: Dict = {}
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
            if _distance_to_geom_m(sampler.distance, pt, geom,
                                   scaled_cache) <= fb + 1e-6:
                flag = True
                break
        series.append((kp, flag))
    intervals = eng.intervals_from_bool_series(series, sampler.domain)

    # Exact crossings (thin features a coarse buffer might miss between stations).
    route_geoms = sampler.route.geometries
    route_boxes = [g.boundingBox() for g in route_geoms]
    for geom, feat in feats.values():
        if not passes_filter(feat):
            continue
        eps_km = max(_feature_buffer_m(feat, buffer_field, buffer_m), 1.0) / 1000.0
        feat_box = geom.boundingBox()
        for route_geom, route_box in zip(route_geoms, route_boxes):
            if not route_box.intersects(feat_box):
                continue
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


def polygon_route_buffer_m_at(config: Dict,
                              depth_at: Optional[Callable[[float], Optional[float]]]
                              ) -> Optional[Callable[[float], float]]:
    """Per-KP route-corridor half-width (m) for a polygon-class rule.

    ``route_buffer_mode``: "" (route centreline only — returns None),
    "fixed" (``route_buffer_m`` metres) or "wd" (``route_buffer_wd`` × the
    water depth at that KP via ``depth_at``; stations with no depth fall
    back to the centreline-only test).
    """
    mode = (config.get("route_buffer_mode") or "").strip().lower()
    if mode == "fixed":
        fixed = max(0.0, float(config.get("route_buffer_m") or 0.0))
        if fixed <= 0:
            return None
        return lambda _kp: fixed
    if mode == "wd":
        factor = max(0.0, float(config.get("route_buffer_wd") or 0.0))
        if factor <= 0 or depth_at is None:
            return None

        def at(kp: float) -> float:
            depth = depth_at(kp)
            return factor * abs(float(depth)) if depth is not None else 0.0

        return at
    return None


def polygon_feature_matcher(config: Dict) -> Callable[[QgsFeature], bool]:
    """The polygon-class rule's per-feature test, shared by acquisition
    and boundary refinement so both bisect the identical condition.

    ``match_expression`` (a QGIS expression over the feature) wins when
    set; otherwise the ``attribute`` value must equal one of
    ``match_values`` or fall in one of the ``match_rules`` ranges (see
    ``burial.attribute_rules``). No attribute at all = every polygon.
    """
    attribute = config.get("attribute") or ""
    rules = attribute_rules.polygon_match_rules(config)
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
        return attribute_rules.any_rule_matches(rules, val)

    return matches


def polygon_class_intervals(sampler: RouteSampler, index: QgsSpatialIndex,
                            feats: Dict[int, Tuple[QgsGeometry, QgsFeature]],
                            config: Dict,
                            cancel: Optional[Callable[[], bool]] = None,
                            depth_at: Optional[Callable[[float], Optional[float]]] = None
                            ) -> List[Interval]:
    """Polygon-class intervals over pre-loaded WGS84 features (thread-safe).

    By default a station fires when the route centreline lies inside a
    matching polygon. With a route-corridor buffer configured
    (``route_buffer_mode``: fixed metres or a water-depth multiple via
    ``depth_at``) a station also fires when a matching polygon comes within
    that distance of the route.
    """
    matches = polygon_feature_matcher(config)
    buffer_m_at = polygon_route_buffer_m_at(config, depth_at)

    series: List[Tuple[float, bool]] = []
    scaled_cache: Dict = {}
    for station_index, (kp, pt) in enumerate(zip(sampler.stations_km, sampler.coords)):
        if cancel is not None and station_index % _CANCEL_CHUNK == 0 and cancel():
            raise AcquisitionCancelled()
        if pt is None:
            series.append((kp, False))
            continue
        buffer_m = max(0.0, float(buffer_m_at(kp))) if buffer_m_at is not None else 0.0
        pt_geom = QgsGeometry.fromPointXY(pt)
        flag = False
        for fid in index.intersects(_search_rect(pt, max(buffer_m, 1.0))):
            geom, feat = feats[fid]
            if not matches(feat):
                continue
            if geom.contains(pt_geom):
                flag = True
                break
            if buffer_m > 0 and _distance_to_geom_m(
                    sampler.distance, pt, geom,
                    scaled_cache) <= buffer_m + 1e-6:
                flag = True
                break
        series.append((kp, flag))
    intervals = eng.intervals_from_bool_series(series, sampler.domain)

    # Exact crossings: a matching polygon narrower than the station spacing,
    # crossed transversely, was invisible to the per-station test (and
    # boundary refinement can only move existing boundaries, never recover a
    # missed polygon). Mirror the proximity rule's exact-crossing pass.
    route_geoms = sampler.route.geometries
    route_boxes = [g.boundingBox() for g in route_geoms]
    for geom, feat in feats.values():
        if not matches(feat):
            continue
        feat_box = geom.boundingBox()
        for route_geom, route_box in zip(route_geoms, route_boxes):
            if not route_box.intersects(feat_box):
                continue
            try:
                inter = route_geom.intersection(geom)
            except Exception:
                continue
            if inter is None or inter.isEmpty():
                continue
            kps: List[float] = []
            for pt in _iter_points(inter):
                hit = sampler.route.kp_at_point(QgsPointXY(pt))
                if hit.snapped_xy is not None:
                    kps.append(float(hit.kp_km))
            if not kps:
                continue
            # min/max over the intersection vertices covers non-monotone
            # parts; pad point crossings to a visible ±0.5 m.
            lo, hi = min(kps), max(kps)
            if hi - lo < 0.001:
                lo, hi = lo - 0.0005, hi + 0.0005
            intervals.append(Interval(lo, hi))
    return eng.clip_intervals(eng.normalize(intervals), sampler.domain)


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


@dataclass
class RuleWork:
    """One rule, resolved on the main thread into worker-safe inputs."""

    row: Dict
    rule: Rule
    config: Dict
    error: str = ""                       # input problem found on the main thread
    geom_type: object = None
    layer_snapshot: Optional[Dict] = None  # feature source + CRS for index rules
    table_snapshot: Optional[Dict] = None  # feature source + fields for KP tables


def _snapshot_layer(layer: QgsVectorLayer, project: QgsProject) -> Dict:
    return {
        "source": QgsVectorLayerFeatureSource(layer),
        "crs": layer.crs(),
        "transform_context": project.transformContext(),
        "feature_count": max(int(layer.featureCount()), 0),
    }


def build_rule_work(row: Dict, project: QgsProject) -> RuleWork:
    """Main thread: parse a rule row and snapshot the layer it reads."""
    try:
        config = json.loads(row.get("config_json") or "{}")
    except (ValueError, TypeError):
        config = {}
    work = RuleWork(row=row, rule=_rule_from_row(row), config=config)
    kind = work.rule.kind
    if kind in (schema.RULE_KIND_PROXIMITY, schema.RULE_KIND_POLYGON,
                schema.RULE_KIND_KP_TABLE):
        try:
            layer = _resolve_layer(project, config)
            snapshot = _snapshot_layer(layer, project)
        except RuleInputError as exc:
            work.error = str(exc)
            return work
        if kind == schema.RULE_KIND_KP_TABLE:
            snapshot["fields"] = [f.name() for f in layer.fields()]
            work.table_snapshot = snapshot
        else:
            work.layer_snapshot = snapshot
            work.geom_type = layer.geometryType()
    return work


def _table_rows(snapshot: Dict, config: Dict,
                cancel: Optional[Callable[[], bool]] = None) -> List[Dict]:
    expr, ctx = _filter_expression(config.get("filter_expression", ""))
    names = snapshot.get("fields") or []
    rows: List[Dict] = []
    for i, feat in enumerate(snapshot["source"].getFeatures()):
        if cancel is not None and i % 500 == 0 and cancel():
            raise AcquisitionCancelled()
        if expr is not None:
            ctx.setFeature(feat)
            if not bool(expr.evaluate(ctx)):
                continue
        rows.append({name: feat[name] for name in names})
    return rows


def _acquire_rule(sampler: RouteSampler, work: RuleWork, depth: Optional[DepthInputs],
                  cancel: Optional[Callable[[], bool]] = None) -> List[Interval]:
    """Worker-safe acquisition of one rule from its snapshot."""
    if work.error:
        raise RuleInputError(work.error)
    kind, config = work.rule.kind, work.config
    if kind == schema.RULE_KIND_THRESHOLD:
        if depth is None:
            raise RuleInputError("no depth source was captured for this run.")
        return _acquire_threshold(sampler, depth, config, cancel)
    if kind in (schema.RULE_KIND_PROXIMITY, schema.RULE_KIND_POLYGON):
        snap = work.layer_snapshot
        index, feats = _load_features_wgs84_from_source(
            snap["source"], snap["crs"], snap["transform_context"], cancel=cancel,
            feature_count=snap.get("feature_count", 0))
        if kind == schema.RULE_KIND_PROXIMITY:
            return proximity_intervals(sampler, index, feats, work.geom_type, config,
                                       cancel=cancel)
        return polygon_class_intervals(sampler, index, feats, config, cancel=cancel)
    if kind == schema.RULE_KIND_KP_TABLE:
        rows = _table_rows(work.table_snapshot, config, cancel)
        return kp_table_intervals(rows, config, sampler.domain)
    if kind == schema.RULE_KIND_MANUAL:
        return _acquire_manual(sampler, config)
    raise _UnknownRuleKind()


class _UnknownRuleKind(Exception):
    pass


def acquire_rule_hits(sampler: RouteSampler, rules: Sequence[RuleWork],
                      depth: Optional[DepthInputs], progress: ProgressFn = None,
                      cancel: Optional[Callable[[], bool]] = None
                      ) -> Tuple[List[RuleHit], List[str]]:
    """Evaluate prepared rules (thread-safe: snapshots only).

    A rule whose inputs fail becomes a warning and fires nowhere; only a
    cancel (``AcquisitionCancelled``) aborts the run.
    """
    hits: List[RuleHit] = []
    warnings: List[str] = []
    for work in rules:
        if cancel is not None and cancel():
            raise AcquisitionCancelled()
        rule = work.rule
        if progress:
            progress(f"Evaluating rule: {rule.name}")
        intervals: List[Interval] = []
        try:
            intervals = _acquire_rule(sampler, work, depth, cancel)
        except AcquisitionCancelled:
            raise
        except _UnknownRuleKind:
            warnings.append(f"Rule '{rule.name}': unknown kind '{rule.kind}' — skipped.")
            intervals = []
        except RuleInputError as exc:
            warnings.append(f"Rule '{rule.name}': {exc} — skipped.")
            intervals = []
        except Exception as exc:  # never let one rule crash the run
            log_exception(f"Workbench assessment: rule '{rule.name}' failed")
            warnings.append(f"Rule '{rule.name}': unexpected error ({exc}) — skipped.")
            intervals = []

        scope = _scope_intervals(work.config)
        if scope is not None:
            intervals = eng.intersect_intervals(intervals, scope)
        hits.append(RuleHit(rule, intervals))
    return hits, warnings


def acquire_hits(sampler: RouteSampler, store, rpl_id: str, rule_rows: Sequence[Dict],
                 project: QgsProject, progress: ProgressFn = None
                 ) -> Tuple[List[RuleHit], List[str]]:
    """Main-thread convenience: snapshot ``rule_rows`` and evaluate them."""
    rules = [build_rule_work(row, project) for row in rule_rows]
    depth = None
    if any(work.rule.kind == schema.RULE_KIND_THRESHOLD for work in rules):
        depth = DepthInputs.capture(store, rpl_id, project)
    return acquire_rule_hits(sampler, rules, depth, progress)


@dataclass
class AssessmentWork:
    """Everything one assessment run needs, captured on the main thread."""

    route: RouteFrame
    distance: object
    methods: List[str]
    rules: List[RuleWork]
    depth: Optional[DepthInputs]
    sample_step_m: float = 50.0
    min_range_km: float = 0.0


def build_assessment_work(store, rpl_id: str, rule_set_id: str, *,
                          sample_step_m: float = 50.0, min_range_km: float = 0.0,
                          project: Optional[QgsProject] = None) -> AssessmentWork:
    """Main thread: read the store and snapshot every input of a run.

    Raises ``RuleInputError`` when the rule set or the RPL route is unusable.
    """
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

    route, distance = route_for_rpl(store, rpl_id, project)
    rules = [build_rule_work(row, project) for row in store.list_rules(rule_set_id)]
    depth = None
    if any(work.rule.kind == schema.RULE_KIND_THRESHOLD for work in rules):
        depth = DepthInputs.capture(store, rpl_id, project)
    return AssessmentWork(route=route, distance=distance, methods=methods, rules=rules,
                          depth=depth, sample_step_m=sample_step_m,
                          min_range_km=min_range_km)


def execute_assessment(work: AssessmentWork, progress: ProgressFn = None,
                       cancel: Optional[Callable[[], bool]] = None
                       ) -> Tuple[eng.AssessmentResult, RouteSampler]:
    """Sample the route, evaluate the rule stack (thread-safe)."""
    if progress:
        progress("Building route stations…")
    sampler = RouteSampler.from_route(work.route, work.distance, work.sample_step_m)
    hits, warnings = acquire_rule_hits(sampler, work.rules, work.depth, progress, cancel)
    if cancel is not None and cancel():
        raise AcquisitionCancelled()
    result = eng.evaluate(sampler.domain, work.methods, hits, min_range_km=work.min_range_km)
    result.warnings = warnings
    return result, sampler


def run_assessment(store, rpl_id: str, rule_set_id: str, *, sample_step_m: float = 50.0,
                   min_range_km: float = 0.0, project: Optional[QgsProject] = None,
                   progress: ProgressFn = None
                   ) -> Tuple[eng.AssessmentResult, RouteSampler]:
    """Sample the route, evaluate the rule stack, return (result, sampler).

    Synchronous; the Workbench panel runs the same two steps through
    :class:`AssessmentTask` so the UI stays responsive.
    """
    work = build_assessment_work(store, rpl_id, rule_set_id, sample_step_m=sample_step_m,
                                 min_range_km=min_range_km, project=project)
    return execute_assessment(work, progress=progress)


def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


class AssessmentTask(QgsTask):
    """Runs :func:`execute_assessment` on a worker thread.

    ``result`` / ``sampler`` are set on success; ``error`` holds the message
    of a failure; ``cancelled`` marks a cancel. ``progressMessage`` is queued
    to the main thread. The completion signals are the caller's to handle.
    """

    progressMessage = pyqtSignal(str)

    def __init__(self, work: AssessmentWork, description: str = "Workbench assessment"):
        super().__init__(description, _task_flag("CanCancel"))
        self.work = work
        self.result: Optional[eng.AssessmentResult] = None
        self.sampler: Optional[RouteSampler] = None
        self.error: Optional[str] = None
        self.cancelled = False

    def run(self) -> bool:  # worker thread
        try:
            self.result, self.sampler = execute_assessment(
                self.work, progress=self.progressMessage.emit, cancel=self.isCanceled)
        except AcquisitionCancelled:
            self.cancelled = True
            return False
        except Exception as exc:  # reported to the user by the panel
            log_exception("Workbench assessment failed")
            self.error = str(exc) or exc.__class__.__name__
            return False
        return True


# ---------------------------------------------------------------------------
# Public acquisition API
#
# These helpers started life as module-private but are part of the Burial
# Planner's acquisition pipeline (burial/analysis_task.py). The public names
# below are the stable contract: keep them working (or deprecate loudly)
# when refactoring the underscore-prefixed implementations.
# ---------------------------------------------------------------------------
resolve_layer = _resolve_layer
load_features_wgs84 = _load_features_wgs84
load_features_wgs84_from_source = _load_features_wgs84_from_source
isotropic_nearest = _isotropic_nearest
search_rect = _search_rect
distance_to_geom_m = _distance_to_geom_m
filter_expression = _filter_expression
feature_buffer_m = _feature_buffer_m
acquire_manual = _acquire_manual
scope_intervals = _scope_intervals
