# -*- coding: utf-8 -*-
"""Background acquisition for the Burial Planner (QgsTask).

Thread-safety contract (spec §14.1): the task never touches ``QgsProject``,
GUI objects or live layers. On the main thread, ``build_work`` resolves
inputs and clones everything into worker-safe snapshots — feature geometries
via the ``_load_features_wgs84`` pattern, vector feature sources via
``QgsVectorLayerFeatureSource``, raster providers via ``provider.clone()``,
and rule configs as dicts. The task consumes snapshots only; results come
back as plain interval data; store/layer writes happen in ``finished()`` on
the main thread (wired by the dock).

Caching (spec §14.4): each rule's acquisition (footprint + no-data +
1 m-refined boundaries) is cached in memory per open plan, keyed over the
canonicalised config, resolved input fingerprints, scope, step, RPL
fingerprint and (for direction-aware conditions) direction. Editing one rule
re-acquires one rule; a cancelled run leaves the cache warm and resumes.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsSpatialIndex,
    QgsTask,
    QgsVectorLayer,
    QgsVectorLayerFeatureSource,
)
from qgis.PyQt.QtCore import pyqtSignal

from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..workbench import rules_engine as eng
from ..workbench import rules_inputs as ri
from ..workbench import schema as wb_schema
from ..workbench.depth_service import DepthSourceConfig
from ..workbench.rules_engine import Interval
from . import generation, map_layers, schema

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


_CAN_CANCEL = _task_flag("CanCancel")


# ---------------------------------------------------------------------------
# Thread-safe depth snapshot
# ---------------------------------------------------------------------------


class DepthSnapshot:
    """Depth sampler safe to use on a worker thread.

    Built on the main thread from a ``DepthSourceConfig``: raster providers
    are cloned (``provider.clone()``) and contour feature-source snapshots are
    captured. Contours are materialised in the worker thread.
    ``sample(lat, lon)`` mirrors DepthService.
    """

    def __init__(self, config: DepthSourceConfig, project: Optional[QgsProject] = None):
        project = project or QgsProject.instance()
        self.mode = int(config.mode)
        self.band = max(1, int(config.raster_band or 1))
        self.search_radius_m = float(config.contour_search_radius_m or 0.0)
        self._rasters: List[Tuple[object, Optional[QgsCoordinateTransform]]] = []
        for layer_id in config.raster_layer_ids:
            layer = project.mapLayer(layer_id)
            if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
                continue
            try:
                provider = layer.dataProvider().clone()
            except Exception:
                continue
            transform = None
            if layer.crs() != WGS84:
                try:
                    transform = QgsCoordinateTransform(WGS84, layer.crs(), project)
                except Exception:
                    continue
            self._rasters.append((provider, transform))

        self._contours: List[Tuple[QgsGeometry, float]] = []
        self._contour_index: Optional[QgsSpatialIndex] = None
        self._contour_sources: List[Dict] = []
        self._contours_prepared = False
        self._transform_context = project.transformContext()
        for entry in config.contour_layers:
            layer = project.mapLayer(entry.get("layer_id", ""))
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            depth_field = entry.get("depth_field", "")
            names = layer.fields().names()
            field_name = depth_field if depth_field in names else (names[0] if names else "")
            if not field_name:
                continue
            # QgsVectorLayerFeatureSource is the thread-safe snapshot used by
            # QGIS for background feature iteration.  Creating it here is
            # cheap; potentially large contour reads and spatial-index builds
            # are deferred until the worker task starts.
            self._contour_sources.append({
                "source": QgsVectorLayerFeatureSource(layer),
                "crs": layer.crs(),
                "field_name": field_name,
                "feature_count": max(int(layer.featureCount()), 0),
            })
        self._distance = make_distance_area(WGS84, self._transform_context,
                                            project=project)

    def prepare(self, cancel: Optional[Callable[[], bool]] = None,
                progress: Optional[Callable[[int, int], None]] = None) -> bool:
        """Materialise contour snapshots on the calling worker thread.

        Returns ``False`` when cooperative cancellation was requested.
        Raster-only configurations complete immediately.
        """
        if self._contours_prepared:
            return True
        if self.mode == 1:  # raster only
            self._contours_prepared = True
            return True
        contour_feats = []
        total = sum(max(int(s["feature_count"]), 1)
                    for s in self._contour_sources) or 1
        done = 0
        for spec in self._contour_sources:
            xform = None
            if spec["crs"] != WGS84:
                xform = QgsCoordinateTransform(
                    spec["crs"], WGS84, self._transform_context)
            count = max(int(spec["feature_count"]), 1)
            for idx, feat in enumerate(spec["source"].getFeatures()):
                if idx % 500 == 0:
                    if cancel is not None and cancel():
                        return False
                    if progress is not None:
                        progress(done + min(idx, count), total)
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                try:
                    value = float(feat[spec["field_name"]])
                except (KeyError, TypeError, ValueError):
                    continue
                geom = QgsGeometry(geom)
                if xform is not None:
                    try:
                        geom.transform(xform)
                    except Exception:
                        continue
                contour_feats.append((geom, value))
            done += count
            if progress is not None:
                progress(done, total)
        if contour_feats:
            self._contour_index = QgsSpatialIndex()
            for i, (geom, _value) in enumerate(contour_feats):
                from qgis.core import QgsFeature

                idx_feat = QgsFeature()
                idx_feat.setId(i)
                idx_feat.setGeometry(geom)
                self._contour_index.insertFeature(idx_feat)
            self._contours = contour_feats
        self._contours_prepared = True
        return True

    def is_available(self) -> bool:
        return bool(self._rasters or self._contour_sources or self._contours)

    def sample(self, lat: float, lon: float) -> Optional[float]:
        if not self._contours_prepared and not self.prepare():
            return None
        point = QgsPointXY(lon, lat)
        want_raster = self.mode in (0, 1)
        want_contours = self.mode in (0, 2)
        if want_raster:
            for provider, transform in self._rasters:
                sample_pt = point
                if transform is not None:
                    try:
                        sample_pt = transform.transform(point)
                    except Exception:
                        continue
                try:
                    value, ok = provider.sample(sample_pt, self.band)
                except Exception:
                    continue
                if ok and value is not None and value == value:
                    return float(value)
        if want_contours and self._contours:
            # 0 = unlimited (DepthService semantics): scan every contour.
            if self.search_radius_m > 0 and self._contour_index is not None:
                rect = ri._search_rect(point, self.search_radius_m)
                candidates = self._contour_index.intersects(rect)
            else:
                candidates = range(len(self._contours))
            best = None
            best_dist = None
            pt_geom = QgsGeometry.fromPointXY(point)
            for i in candidates:
                geom, value = self._contours[i]
                try:
                    closest = geom.closestPoint(pt_geom) if hasattr(geom, "closestPoint") \
                        else geom.nearestPoint(pt_geom)
                    if closest is None or closest.isEmpty():
                        continue
                    dist = float(self._distance.measureLine(
                        point, QgsPointXY(closest.asPoint())))
                except Exception:
                    continue
                if self.search_radius_m > 0 and dist > self.search_radius_m:
                    continue
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = value
            return best
        return None


# ---------------------------------------------------------------------------
# Work snapshot (built on the main thread)
# ---------------------------------------------------------------------------


@dataclass
class RuleWork:
    rule_row: Dict
    kind: str = ""
    config: Dict = field(default_factory=dict)  # effective (direction-mapped)
    cache_key: str = ""
    cached: Optional[Tuple[List[Interval], List[Interval]]] = None
    feats: Optional[Tuple[QgsSpatialIndex, Dict]] = None
    geom_type: object = None
    table_rows: Optional[List[Dict]] = None
    error: str = ""


@dataclass
class AnalysisWork:
    route: RouteFrame
    distance: object
    scope: Interval
    step_m: float
    direction: int
    method: str
    refine_tol_m: float
    depth: Optional[DepthSnapshot]
    rules: List[RuleWork] = field(default_factory=list)


@dataclass
class RuleResult:
    rule_row: Dict
    cache_key: str = ""
    footprint: List[Interval] = field(default_factory=list)
    nodata: List[Interval] = field(default_factory=list)
    error: str = ""
    from_cache: bool = False


def _effective_config(rule_row: Dict, direction: int) -> Dict:
    """Rule config with travel direction mapped onto signed-slope limits.

    Positive signed slope = deepening with increasing KP. Installing against
    KP (direction -1) swaps which physical limit applies to which sign.
    """
    config = generation.rule_config(rule_row)
    if config.get("slope_signed") and int(direction) < 0:
        config = dict(config)
        config["downslope_max_deg"], config["upslope_max_deg"] = (
            config.get("upslope_max_deg"), config.get("downslope_max_deg"))
        bands = config.get("bands")
        if bands:
            swapped = []
            for band in bands:
                band = dict(band)
                band["downslope_limit"], band["upslope_limit"] = (
                    band.get("upslope_limit"), band.get("downslope_limit"))
                swapped.append(band)
            config["bands"] = swapped
    return config


def build_route_frame(lines_layer: QgsVectorLayer,
                      project: Optional[QgsProject] = None
                      ) -> Tuple[RouteFrame, object]:
    """Clone a route (lines layer in Workbench RPL format) into a RouteFrame
    over WGS84 geometries. Main thread only."""
    project = project or QgsProject.instance()
    ordered = []
    xform = None
    if lines_layer.crs() != WGS84:
        xform = QgsCoordinateTransform(lines_layer.crs(), WGS84, project)
    for feat in lines_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        geom = QgsGeometry(geom)
        if xform is not None:
            try:
                geom.transform(xform)
            except Exception:
                continue
        try:
            seq = int(feat["SeqNo"])
        except (KeyError, TypeError, ValueError):
            seq = len(ordered)
        ordered.append((seq, geom))
    ordered.sort(key=lambda t: t[0])
    geoms = [g for _, g in ordered]
    if not geoms:
        raise ri.RuleInputError("The route layer has no usable line geometry.")
    distance = make_distance_area(WGS84, project.transformContext(), project=project)
    return RouteFrame.from_source(geoms, distance), distance


def build_work(route: RouteFrame, distance, plan: Dict, rule_rows: List[Dict],
               inputs: List[Dict], depth_config: DepthSourceConfig,
               params: generation.GenParams,
               cache: Dict[str, Tuple[List[Interval], List[Interval]]],
               rpl_fp: str,
               project: Optional[QgsProject] = None) -> Tuple["AnalysisWork", List[str]]:
    """Snapshot everything the task needs (main thread). Returns (work, warnings)."""
    project = project or QgsProject.instance()
    warnings: List[str] = []
    inputs_by_id = {str(r.get("input_id")): r for r in inputs}

    depth = DepthSnapshot(depth_config, project) if depth_config.is_configured() else None

    scope = params.scope
    work = AnalysisWork(
        route=route, distance=distance, scope=scope,
        step_m=params.coarse_step_m, direction=params.direction,
        method=params.method, refine_tol_m=params.refine_tol_m, depth=depth)

    for row in rule_rows:
        if not int(row.get("enabled") or 0):
            continue
        try:
            methods = json.loads(row.get("methods_json") or "[]")
        except (ValueError, TypeError):
            methods = []
        if methods and params.method not in methods:
            continue
        rule_work = RuleWork(rule_row=dict(row))
        rule_work.kind = row.get("kind") or ""
        rule_work.config = _effective_config(row, params.direction)

        input_row = inputs_by_id.get(str(rule_work.config.get("input_id") or ""))
        layer = None
        input_fp = ""
        needs_layer = rule_work.kind in (wb_schema.RULE_KIND_PROXIMITY,
                                         wb_schema.RULE_KIND_POLYGON,
                                         wb_schema.RULE_KIND_KP_TABLE)
        if needs_layer:
            if input_row is not None:
                layer = map_layers.resolve_input_layer(project, input_row)
            elif rule_work.config.get("layer_id") or rule_work.config.get("layer_source"):
                # Direct layer reference (e.g. a rule copied from an Assessment).
                try:
                    layer = ri._resolve_layer(project, rule_work.config)
                except ri.RuleInputError:
                    layer = None
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                rule_work.error = "input layer could not be resolved"
            else:
                input_fp = map_layers.layer_fingerprint(layer)
        elif rule_work.kind == wb_schema.RULE_KIND_THRESHOLD:
            if depth is None or not depth.is_available():
                rule_work.error = "no bathymetry source configured"
            else:
                input_fp = "|".join(
                    map_layers.layer_fingerprint(project.mapLayer(i))
                    for i in depth_config.raster_layer_ids
                ) + "|" + "|".join(
                    map_layers.layer_fingerprint(project.mapLayer(
                        c.get("layer_id", ""))) for c in depth_config.contour_layers)

        rule_work.cache_key = generation.rule_cache_key(
            row, input_fp, scope, params.coarse_step_m, rpl_fp, params.direction)
        cached = cache.get(rule_work.cache_key)
        if cached is not None and not rule_work.error:
            rule_work.cached = cached
        elif not rule_work.error and needs_layer and layer is not None:
            if rule_work.kind == wb_schema.RULE_KIND_KP_TABLE:
                expr, ctx = ri._filter_expression(
                    rule_work.config.get("filter_expression", ""))
                names = [f.name() for f in layer.fields()]
                rows = []
                for feat in layer.getFeatures():
                    if expr is not None:
                        ctx.setFeature(feat)
                        if not bool(expr.evaluate(ctx)):
                            continue
                    rows.append({name: feat[name] for name in names})
                rule_work.table_rows = rows
            else:
                index, feats = ri._load_features_wgs84(layer, project)
                rule_work.feats = (index, feats)
                rule_work.geom_type = layer.geometryType()
        if rule_work.error:
            warnings.append(
                f"Rule '{row.get('name') or rule_work.kind}': {rule_work.error} — skipped.")
        work.rules.append(rule_work)
    return work, warnings


# ---------------------------------------------------------------------------
# Predicates for 1 m boundary refinement (§14.3)
# ---------------------------------------------------------------------------

_SLOPE_DELTA_KM = 0.010  # 10 m central difference for slope predicates


def _threshold_predicate(work: AnalysisWork, config: Dict) -> Optional[Callable[[float], bool]]:
    depth = work.depth
    route = work.route
    if depth is None:
        return None

    def depth_at(kp: float) -> Optional[float]:
        pt = route.point_at_kp(kp, clamp=True)
        if pt is None:
            return None
        value = depth.sample(pt.y(), pt.x())
        return abs(float(value)) if value is not None else None

    profile = (config.get("profile") or "depth").lower()

    def value_at(kp: float) -> Optional[float]:
        if profile != "slope":
            return depth_at(kp)
        d0 = depth_at(max(work.scope.start_km, kp - _SLOPE_DELTA_KM))
        d1 = depth_at(min(work.scope.end_km, kp + _SLOPE_DELTA_KM))
        if d0 is None or d1 is None:
            return None
        dx_m = 2.0 * _SLOPE_DELTA_KM * 1000.0
        dz = d1 - d0
        if config.get("slope_signed"):
            return math.degrees(math.atan2(dz, dx_m))
        return math.degrees(math.atan2(abs(dz), dx_m))

    signed = bool(config.get("slope_signed")) and profile == "slope"
    bands = config.get("bands") or []
    op = config.get("op") or ">"

    def predicate(kp: float) -> bool:
        value = value_at(kp)
        if value is None:
            return False
        if bands:
            wd = depth_at(kp)
            if wd is None:
                return False
            band = eng.select_band(bands, wd)
            if band is None:
                return False
            if signed:
                down = band.get("downslope_limit", band.get("limit"))
                up = band.get("upslope_limit", band.get("limit"))
                return ((down is not None and value > float(down))
                        or (up is not None and value < -abs(float(up))))
            limit = band.get("limit")
            if limit is None:
                return False
            lo, hi = eng._cond_bounds(op, float(limit), None)
            return lo <= value <= hi
        if signed:
            down = config.get("downslope_max_deg")
            up = config.get("upslope_max_deg")
            return ((down is not None and value > float(down))
                    or (up is not None and value < -abs(float(up))))
        value2 = config.get("value2")
        lo, hi = eng._cond_bounds(op, float(config.get("value", 0.0)),
                                  float(value2) if value2 is not None else None)
        if bool(config.get("abs", False)):
            value = abs(value)
        return lo <= value <= hi

    return predicate


def _geometry_predicate(work: AnalysisWork, rule_work: RuleWork
                        ) -> Optional[Callable[[float], bool]]:
    if rule_work.feats is None:
        return None
    index, feats = rule_work.feats
    config = rule_work.config
    route = work.route
    distance = work.distance
    kind = rule_work.kind
    buffer_m = float(config.get("distance_m", 0.0)) \
        if config.get("mode", "distance") == "distance" else 0.0
    buffer_field = (config.get("buffer_field") or "").strip()
    from ..qgis_compat import GEOMETRY_POLYGON

    attribute = config.get("attribute") or ""
    match_values = {str(v).strip().lower() for v in (config.get("match_values") or [])}
    expr, ctx = ri._filter_expression(
        config.get("match_expression" if kind == wb_schema.RULE_KIND_POLYGON
                   else "filter_expression", ""))

    def matches(feat) -> bool:
        if expr is not None:
            ctx.setFeature(feat)
            return bool(expr.evaluate(ctx))
        if kind != wb_schema.RULE_KIND_POLYGON or not attribute:
            return True
        try:
            return str(feat[attribute]).strip().lower() in match_values
        except KeyError:
            return False

    def predicate(kp: float) -> bool:
        pt = route.point_at_kp(kp, clamp=True)
        if pt is None:
            return False
        pt_geom = QgsGeometry.fromPointXY(pt)
        if kind == wb_schema.RULE_KIND_POLYGON:
            for fid in index.intersects(ri._search_rect(pt, 1.0)):
                geom, feat = feats[fid]
                if geom.contains(pt_geom) and matches(feat):
                    return True
            return False
        max_buffer = buffer_m
        if buffer_field:
            for _geom, feat in feats.values():
                max_buffer = max(max_buffer,
                                 ri._feature_buffer_m(feat, buffer_field, buffer_m))
        for fid in index.intersects(ri._search_rect(pt, max(max_buffer, 1.0))):
            geom, feat = feats[fid]
            if not matches(feat):
                continue
            fb = ri._feature_buffer_m(feat, buffer_field, buffer_m)
            if rule_work.geom_type == GEOMETRY_POLYGON and geom.contains(pt_geom):
                return True
            if ri._distance_to_geom_m(distance, pt, geom) <= fb + 1e-6:
                return True
        return False

    return predicate


# ---------------------------------------------------------------------------
# The task
# ---------------------------------------------------------------------------


class BurialAnalysisTask(QgsTask):
    """Acquire (and refine) every enabled rule's intervals in the background.

    Emits ``progressMessage`` (queued to the main thread) with the current
    rule; ``results`` carries per-rule interval data back; ``finished()``
    invokes the completion callback on the main thread.
    """

    progressMessage = pyqtSignal(str)

    def __init__(self, work: AnalysisWork,
                 on_finished: Callable[["BurialAnalysisTask"], None],
                 description: str = "Burial Planner analysis"):
        super().__init__(description, _CAN_CANCEL)
        self.work = work
        self.results: List[RuleResult] = []
        self.error: Optional[str] = None
        self.cancelled = False
        self._on_finished = on_finished
        self._sampler: Optional[ri.RouteSampler] = None
        self._depth_series: Optional[List[Tuple[float, float]]] = None
        self._depth_gaps: Optional[List[Interval]] = None

    # -- worker thread -------------------------------------------------------
    def run(self) -> bool:  # noqa: C901 — one linear pipeline, clearer inline
        try:
            work = self.work
            total = max(len(work.rules), 1)
            if work.depth is not None and work.depth.is_available():
                self.progressMessage.emit("Preparing bathymetry sources…")
                if not work.depth.prepare(cancel=self.isCanceled):
                    self.cancelled = True
                    return False
            self.progressMessage.emit("Building route stations…")
            sampler = ri.RouteSampler.from_route(
                work.route, work.distance, work.step_m, work.scope)
            self._sampler = sampler
            if self.isCanceled():
                self.cancelled = True
                return False

            tol_km = max(work.refine_tol_m, 0.001) / 1000.0
            coarse_step_km = max(work.step_m, 1.0) / 1000.0

            for i, rule_work in enumerate(work.rules):
                if self.isCanceled():
                    self.cancelled = True
                    return False
                name = rule_work.rule_row.get("name") or rule_work.kind
                self.progressMessage.emit(f"Evaluating rule: {name}")
                result = RuleResult(rule_work.rule_row, rule_work.cache_key)
                if rule_work.error:
                    result.error = rule_work.error
                elif rule_work.cached is not None:
                    result.footprint, result.nodata = rule_work.cached
                    result.from_cache = True
                else:
                    try:
                        result.footprint, result.nodata = self._acquire(
                            sampler, rule_work, coarse_step_km, tol_km)
                    except ri.AcquisitionCancelled:
                        self.cancelled = True
                        return False
                    except ri.RuleInputError as exc:
                        result.error = str(exc)
                    except Exception as exc:  # never let one rule crash the run
                        result.error = f"unexpected error ({exc})"
                self.results.append(result)
                self.setProgress(100.0 * (i + 1) / total)
            self.setProgress(100.0)
            return True
        except Exception as exc:  # pragma: no cover — task-level fail-safe
            self.error = str(exc)
            return False

    def _acquire(self, sampler: ri.RouteSampler, rule_work: RuleWork,
                 coarse_step_km: float, tol_km: float
                 ) -> Tuple[List[Interval], List[Interval]]:
        work = self.work
        config = rule_work.config
        kind = rule_work.kind
        cancel = self.isCanceled
        nodata: List[Interval] = []
        predicate: Optional[Callable[[float], bool]] = None

        if kind == wb_schema.RULE_KIND_THRESHOLD:
            if self._depth_series is None:
                if work.depth is None:
                    raise ri.RuleInputError("no bathymetry source configured")
                self.progressMessage.emit("Sampling bathymetry along the scope…")
                self._depth_series, self._depth_gaps = ri.depth_series_with_gaps(
                    sampler, work.depth.sample, cancel=cancel)
            if not self._depth_series:
                raise ri.RuleInputError("bathymetry has no coverage in the scope")
            intervals = ri.threshold_intervals(
                self._depth_series, config, sampler.scope_domain)
            nodata = list(self._depth_gaps or [])
            predicate = _threshold_predicate(work, config)
        elif kind in (wb_schema.RULE_KIND_PROXIMITY, wb_schema.RULE_KIND_POLYGON):
            if rule_work.feats is None:
                raise ri.RuleInputError("input layer could not be resolved")
            index, feats = rule_work.feats
            if kind == wb_schema.RULE_KIND_PROXIMITY:
                intervals = ri.proximity_intervals(
                    sampler, index, feats, rule_work.geom_type, config, cancel=cancel)
            else:
                intervals = ri.polygon_class_intervals(
                    sampler, index, feats, config, cancel=cancel)
            predicate = _geometry_predicate(work, rule_work)
        elif kind == wb_schema.RULE_KIND_KP_TABLE:
            intervals = ri.kp_table_intervals(
                rule_work.table_rows or [], config, sampler.scope_domain)
        elif kind == wb_schema.RULE_KIND_MANUAL:
            intervals = ri._acquire_manual(sampler, config)
        else:
            raise ri.RuleInputError(f"unknown rule kind '{kind}'")

        scope_ranges = ri._scope_intervals(config)
        intervals = eng.clip_intervals(intervals, sampler.scope_domain)
        if scope_ranges is not None:
            intervals = eng.intersect_intervals(intervals, scope_ranges)

        if predicate is not None and intervals:
            self.progressMessage.emit(
                f"Refining boundaries: {rule_work.rule_row.get('name') or kind}")
            intervals = generation.refine_intervals(
                intervals, predicate, coarse_step_km, sampler.scope_domain, tol_km)
        return intervals, nodata

    # -- main thread ---------------------------------------------------------
    def finished(self, ok: bool) -> None:
        if not ok and not self.cancelled and self.error is None:
            self.error = "Analysis task failed."
        try:
            self._on_finished(self)
        except Exception:  # never crash QGIS from a completion callback
            pass


class ProfileSamplingTask(QgsTask):
    """Cancellable background depth sampling for the persistent profile."""

    progressMessage = pyqtSignal(str)

    def __init__(self, route: RouteFrame, depth: DepthSnapshot,
                 start_kp: float, end_kp: float, step_m: float,
                 on_finished: Callable[["ProfileSamplingTask"], None]):
        super().__init__("Burial Planner profile", _CAN_CANCEL)
        self.route = route
        self.depth = depth
        self.start_kp = min(float(start_kp), float(end_kp))
        self.end_kp = max(float(start_kp), float(end_kp))
        self.step_m = max(float(step_m), 1.0)
        self.series: List[Tuple[float, float]] = []
        self.error: Optional[str] = None
        self.cancelled = False
        self._on_finished = on_finished

    def run(self) -> bool:
        try:
            if not self.depth.is_available():
                self.error = "No configured bathymetry layer is available in the project."
                return False
            self.progressMessage.emit("Preparing bathymetry…")

            def prep_progress(done: int, total: int) -> None:
                self.setProgress(25.0 * float(done) / max(float(total), 1.0))

            if not self.depth.prepare(self.isCanceled, prep_progress):
                self.cancelled = True
                return False
            if self.isCanceled():
                self.cancelled = True
                return False

            self.progressMessage.emit("Sampling bathymetry profile…")
            length_m = max((self.end_kp - self.start_kp) * 1000.0, 0.0)
            count = max(int(math.ceil(length_m / self.step_m)) + 1, 1)
            for i in range(count):
                if i % 25 == 0 and self.isCanceled():
                    self.cancelled = True
                    return False
                kp = min(self.start_kp + i * self.step_m / 1000.0, self.end_kp)
                point = self.route.point_at_kp(kp, clamp=True)
                if point is not None:
                    value = self.depth.sample(point.y(), point.x())
                    if value is not None and value == value:
                        self.series.append((kp, abs(float(value))))
                self.setProgress(25.0 + 75.0 * float(i + 1) / count)
            self.setProgress(100.0)
            return True
        except Exception as exc:  # pragma: no cover - surfaced in the dock
            self.error = str(exc)
            return False

    def finished(self, ok: bool) -> None:
        if not ok and not self.cancelled and self.error is None:
            self.error = "Profile sampling task failed."
        try:
            self._on_finished(self)
        except Exception:
            pass
