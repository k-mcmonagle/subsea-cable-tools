# -*- coding: utf-8 -*-
"""Background acquisition for the Burial Planner (QgsTask).

Thread-safety contract (spec §14.1): the task never touches ``QgsProject``,
GUI objects or live layers. On the main thread, ``build_work`` resolves
inputs and clones everything into worker-safe snapshots — feature geometries
via the ``rules_inputs.load_features_wgs84`` pattern, vector feature sources via
``QgsVectorLayerFeatureSource``, raster providers via ``provider.clone()``,
and rule configs as dicts. The task consumes snapshots only; results come
back as plain interval data; store/layer writes happen in ``finished()`` on
the main thread (wired by the dock).

Caching (spec §14.4): each rule's acquisition (footprint + no-data +
0.1 m-refined boundaries) is cached in memory per open plan, keyed over the
canonicalised config, resolved input fingerprints, scope, step, RPL
fingerprint, boundary-refinement tolerance and (for direction-aware conditions)
direction. Editing one rule re-acquires one rule; a cancelled run leaves the
cache warm and resumes.
"""

from __future__ import annotations

import bisect
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
from . import generation, map_layers, profile_data, schema

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _log_callback_failure(context: str) -> None:
    """A completion callback failed on the main thread.

    The callback runs the whole post-analysis pipeline (generation + store
    writes), so the guard that keeps QGIS alive must not also make the
    failure invisible: log the traceback to the QGIS message log.
    """
    import traceback

    try:
        from qgis.core import QgsMessageLog

        from ..qgis_compat import MESSAGE_CRITICAL

        QgsMessageLog.logMessage(
            f"{context}: completion handler failed\n{traceback.format_exc()}",
            "Burial Planner", MESSAGE_CRITICAL)
    except Exception:  # pragma: no cover — logging must never raise
        pass


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
        # (provider, transform, cell) — cell = (x_min, y_max, upp_x, upp_y)
        # enables the per-cell memo in _sample_rasters.
        self._rasters: List[Tuple[object, Optional[QgsCoordinateTransform],
                                  Optional[Tuple[float, float, float, float]]]] = []
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
            cell = None
            try:
                extent = provider.extent()
                x_size, y_size = int(provider.xSize()), int(provider.ySize())
                if x_size > 0 and y_size > 0 and extent.width() > 0 \
                        and extent.height() > 0:
                    cell = (extent.xMinimum(), extent.yMaximum(),
                            extent.width() / x_size, extent.height() / y_size)
            except Exception:
                cell = None
            self._rasters.append((provider, transform, cell))
        # Last sampled cell per raster: consecutive stations finer than the
        # raster grid re-read the same cell, so memoising the previous cell
        # removes up to ~95% of the GDAL round-trips at fine profile steps.
        # Results are bit-identical: the key is the exact integer cell.
        self._raster_last: List[Optional[Tuple[Tuple[int, int], object]]] = \
            [None] * len(self._rasters)
        self._raster_miss = object()

        self._contours: List[Tuple[QgsGeometry, float]] = []
        self._contour_scaled_cache: Dict = {}
        self._contour_index: Optional[QgsSpatialIndex] = None
        self._contour_sources: List[Dict] = []
        self._contours_prepared = False
        self._crossing_route = None
        self._crossings: List[Tuple[float, float]] = []
        self._crossing_kps: List[float] = []
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
                self._contour_index.addFeature(idx_feat)
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
            value = self._sample_rasters(point)
            if value is not None:
                return value
        if want_contours and self._contours:
            # 0 = unlimited (DepthService semantics): scan every contour.
            if self.search_radius_m > 0 and self._contour_index is not None:
                rect = ri.search_rect(point, self.search_radius_m)
                candidates = self._contour_index.intersects(rect)
            else:
                candidates = range(len(self._contours))
            best = None
            best_dist = None
            for i in candidates:
                geom, value = self._contours[i]
                try:
                    # Isotropic-frame nearest point: the raw lon/lat
                    # minimisation overstated distances by cos(latitude),
                    # skewing which contour wins and the radius filter.
                    nearest = ri.isotropic_nearest(
                        point, geom, self._contour_scaled_cache)
                    dist = float(self._distance.measureLine(point, nearest))
                except Exception:
                    continue
                if self.search_radius_m > 0 and dist > self.search_radius_m:
                    continue
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best = value
            return best
        return None

    def _sample_rasters(self, point: QgsPointXY) -> Optional[float]:
        for idx, (provider, transform, cell) in enumerate(self._rasters):
            sample_pt = point
            if transform is not None:
                try:
                    sample_pt = transform.transform(point)
                except Exception:
                    continue
            key = None
            if cell is not None:
                x_min, y_max, upp_x, upp_y = cell
                key = (int(math.floor((sample_pt.x() - x_min) / upp_x)),
                       int(math.floor((y_max - sample_pt.y()) / upp_y)))
                last = self._raster_last[idx]
                if last is not None and last[0] == key:
                    cached = last[1]
                    if cached is self._raster_miss:
                        continue
                    return cached
            try:
                value, ok = provider.sample(sample_pt, self.band)
            except Exception:
                continue
            good = ok and value is not None and value == value
            if key is not None:
                self._raster_last[idx] = (
                    key, float(value) if good else self._raster_miss)
            if good:
                return float(value)
        return None

    def contour_crossings(
            self, route: RouteFrame,
            cancel: Optional[Callable[[], bool]] = None,
            progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[Tuple[float, float]]:
        """Return actual ``(KP, depth)`` intersections with the route.

        Longitudinal contour bathymetry must be based on where contours cross
        the route. Nearest-contour point sampling creates a staircase whose
        jumps can look like severe slopes even on widely spaced contours.
        """
        if self._crossing_route is route:
            return list(self._crossings)
        self._crossing_route = route
        self._crossings = []
        self._crossing_kps = []
        if not self._contours or self._contour_index is None:
            return []

        raw: List[Tuple[float, float]] = []
        candidates: List[Tuple[QgsGeometry, int]] = []
        for route_geom in route.geometries:
            if route_geom is None or route_geom.isEmpty():
                continue
            for idx in self._contour_index.intersects(route_geom.boundingBox()):
                candidates.append((route_geom, idx))
        total = max(len(candidates), 1)
        for done, (route_geom, idx) in enumerate(candidates):
            if done % 100 == 0:
                if cancel is not None and cancel():
                    raise ri.AcquisitionCancelled()
                if progress is not None:
                    progress(done, total)
            contour_geom, depth = self._contours[idx]
            try:
                intersection = route_geom.intersection(contour_geom)
            except Exception:
                continue
            if intersection is None or intersection.isEmpty():
                continue
            try:
                vertices = intersection.vertices()
            except Exception:
                continue
            for vertex in vertices:
                try:
                    hit = route.kp_at_point(QgsPointXY(vertex))
                except Exception:
                    continue
                if hit.snapped_xy is not None and hit.dcc_m <= 0.05:
                    raw.append((float(hit.kp_km), float(depth)))
        if progress is not None:
            progress(total, total)

        # Major/minor layers can repeat the same contour. Collapse identical
        # crossings while retaining genuinely different nearby contours.
        raw.sort()
        crossings: List[Tuple[float, float]] = []
        for kp, depth in raw:
            if crossings and abs(kp - crossings[-1][0]) <= 1e-8 \
                    and abs(depth - crossings[-1][1]) <= 1e-8:
                continue
            crossings.append((kp, depth))
        self._crossings = crossings
        self._crossing_kps = [kp for kp, _depth in crossings]
        return list(crossings)

    @staticmethod
    def _interpolate_crossings(crossings: List[Tuple[float, float]],
                               kp: float,
                               crossing_kps: Optional[List[float]] = None
                               ) -> Optional[float]:
        """Linearly interpolate between bracketing contour crossings.

        No extrapolation is made outside the surveyed crossing range.
        """
        if not crossings:
            return None
        kps = crossing_kps if crossing_kps is not None \
            else [item[0] for item in crossings]
        index = bisect.bisect_left(kps, float(kp))
        if index < len(crossings) and abs(crossings[index][0] - kp) <= 1e-9:
            # Multiple different contour values at exactly one KP are
            # geometrically ambiguous; report no data instead of inventing a
            # vertical face or averaging incompatible sources. The list is
            # sorted, so only the bisect neighbourhood can match — a full
            # scan here was O(crossings) per injected crossing station,
            # i.e. quadratic over dense contour data.
            values = [crossings[index][1]]
            for j in range(index - 1, -1, -1):
                if abs(crossings[j][0] - kp) > 1e-9:
                    break
                values.append(crossings[j][1])
            for j in range(index + 1, len(crossings)):
                if abs(crossings[j][0] - kp) > 1e-9:
                    break
                values.append(crossings[j][1])
            return values[0] if all(abs(v - values[0]) <= 1e-8
                                    for v in values) else None
        if index == 0 or index >= len(crossings):
            return None
        kp0, d0 = crossings[index - 1]
        kp1, d1 = crossings[index]
        if kp1 - kp0 <= 1e-9:
            return None
        ratio = (float(kp) - kp0) / (kp1 - kp0)
        return d0 + ratio * (d1 - d0)

    def _polyline_crossings(self, stations_km: List[float],
                            points: List[Optional[QgsPointXY]],
                            cancel: Optional[Callable[[], bool]] = None,
                            chunk_size: int = 512
                            ) -> List[Tuple[float, float]]:
        """(KP, depth) contour crossings along a station polyline.

        The polyline is parametrised by station KP: a crossing at fraction t
        of the segment between stations i and i+1 maps to
        ``kp_i + t * (kp_{i+1} - kp_i)``. Chunking keeps each candidate
        lookup's bounding box local, so stretches with no nearby contours
        cost one index query and nothing else.
        """
        raw: List[Tuple[float, float]] = []
        if not self._contours or self._contour_index is None:
            return raw
        n = len(points)
        start = 0
        while start < n - 1:
            if points[start] is None:
                start += 1
                continue
            end = start
            while (end + 1 < n and points[end + 1] is not None
                   and end - start < chunk_size):
                end += 1
            if end == start:
                start += 1
                continue
            if cancel is not None and cancel():
                raise ri.AcquisitionCancelled()
            seg_pts = points[start:end + 1]
            seg_kps = stations_km[start:end + 1]
            start = end  # chunks share their boundary station: no gap
            # Cumulative planar lengths in geometry units (degrees) — the
            # same metric lineLocatePoint reports, used only to locate a
            # crossing within the chunk, never as a distance.
            cum = [0.0]
            for a, b in zip(seg_pts, seg_pts[1:]):
                cum.append(cum[-1] + math.hypot(b.x() - a.x(), b.y() - a.y()))
            if cum[-1] <= 0.0:
                continue
            chunk_geom = QgsGeometry.fromPolylineXY(seg_pts)
            if chunk_geom is None or chunk_geom.isEmpty():
                continue
            for idx in self._contour_index.intersects(chunk_geom.boundingBox()):
                contour_geom, depth = self._contours[idx]
                try:
                    intersection = chunk_geom.intersection(contour_geom)
                except Exception:
                    continue
                if intersection is None or intersection.isEmpty():
                    continue
                try:
                    vertices = intersection.vertices()
                except Exception:
                    continue
                for vertex in vertices:
                    try:
                        loc = chunk_geom.lineLocatePoint(
                            QgsGeometry.fromPointXY(QgsPointXY(vertex)))
                    except Exception:
                        continue
                    if loc is None or loc < 0.0:
                        continue
                    j = min(max(bisect.bisect_right(cum, loc) - 1, 0),
                            len(seg_kps) - 2)
                    seg_len = cum[j + 1] - cum[j]
                    t = (loc - cum[j]) / seg_len if seg_len > 0 else 0.0
                    raw.append((seg_kps[j] + t * (seg_kps[j + 1] - seg_kps[j]),
                                float(depth)))
        raw.sort()
        crossings: List[Tuple[float, float]] = []
        for kp, depth in raw:
            if crossings and abs(kp - crossings[-1][0]) <= 1e-8 \
                    and abs(depth - crossings[-1][1]) <= 1e-8:
                continue
            crossings.append((kp, depth))
        return crossings

    def offset_profile_samples(
            self, route: RouteFrame, stations_km: List[float],
            offset_m: float, distance,
            cancel: Optional[Callable[[], bool]] = None,
            progress: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[List[Optional[float]], List[Optional[float]]]:
        """(port, starboard) depth values at ± ``offset_m`` per station KP.

        Rasters are point-sampled at the geodesically offset positions.
        Contours follow the along-route methodology: intersect each offset
        polyline with the contours and interpolate between bracketing
        crossings — never per-station nearest-point scans, which are both
        O(stations × contours) (hours on a long route) and a staircase whose
        nearest contour can be kilometres away in flat areas. Stations with
        no bracketing crossings return ``None`` instead of an invented value.
        Starboard is to the right of increasing KP.
        """
        n = len(stations_km)
        port: List[Optional[float]] = [None] * n
        stbd: List[Optional[float]] = [None] * n
        if n == 0 or offset_m <= 0 or distance is None:
            return port, stbd
        if not self._contours_prepared and not self.prepare(cancel):
            raise ri.AcquisitionCancelled()

        total_units = 3 * n  # one share for offset points, one per side

        def tick(units: int) -> None:
            if progress is not None:
                progress(min(units, total_units), total_units)

        station_pts = [route.point_at_kp(kp, clamp=True) for kp in stations_km]
        port_pts: List[Optional[QgsPointXY]] = [None] * n
        stbd_pts: List[Optional[QgsPointXY]] = [None] * n
        half_pi = math.pi / 2.0
        for i in range(n):
            if i % 500 == 0:
                if cancel is not None and cancel():
                    raise ri.AcquisitionCancelled()
                tick(i)
            point = station_pts[i]
            if point is None:
                continue
            p0 = station_pts[i - 1] if i > 0 else point
            p1 = station_pts[i + 1] if i + 1 < n else point
            if p0 is None or p1 is None \
                    or (p0.x() == p1.x() and p0.y() == p1.y()):
                continue
            try:
                azimuth = float(distance.bearing(p0, p1))
                port_pts[i] = distance.computeSpheroidProject(
                    point, float(offset_m), azimuth - half_pi)
                stbd_pts[i] = distance.computeSpheroidProject(
                    point, float(offset_m), azimuth + half_pi)
            except Exception:
                continue
        tick(n)

        for share, offset_pts, out in ((2, port_pts, port),
                                       (3, stbd_pts, stbd)):
            if self.mode in (0, 1) and self._rasters:
                base = (share - 1) * n
                for i, pt in enumerate(offset_pts):
                    if i % 500 == 0:
                        # These two loops used to run up to 1M provider
                        # samples with no cancel check and no progress.
                        if cancel is not None and cancel():
                            raise ri.AcquisitionCancelled()
                        tick(base + i)
                    if pt is not None:
                        out[i] = self._sample_rasters(pt)
            if self.mode in (0, 2) and self._contours:
                crossings = self._polyline_crossings(
                    stations_km, offset_pts, cancel)
                crossing_kps = [kp for kp, _depth in crossings]
                for i, kp in enumerate(stations_km):
                    if out[i] is None and offset_pts[i] is not None:
                        out[i] = self._interpolate_crossings(
                            crossings, kp, crossing_kps)
            tick(share * n)
        return port, stbd

    def sample_route(self, route: RouteFrame, kp: float) -> Optional[float]:
        """Depth at KP using raster samples or contour-crossing interpolation."""
        point = route.point_at_kp(kp, clamp=True)
        if point is None:
            return None
        if self.mode in (0, 1):
            value = self._sample_rasters(point)
            if value is not None:
                return value
        if self.mode in (0, 2):
            if self._crossing_route is not route:
                self.contour_crossings(route)
            return self._interpolate_crossings(
                self._crossings, kp, self._crossing_kps)
        return None

    def profile_samples(self, route: RouteFrame, stations_km: List[float],
                        cancel: Optional[Callable[[], bool]] = None,
                        progress: Optional[Callable[[int, int], None]] = None,
                        ) -> List[Tuple[float, Optional[float]]]:
        """Sample a route profile and include exact contour-crossing KPs."""
        marks = list(stations_km)
        crossing_phase = (self.mode in (0, 2)
                          and self._crossing_route is not route)
        added_crossings = False
        if self.mode in (0, 2):
            crossing_progress = None
            if progress is not None and crossing_phase:
                def crossing_progress(done: int, total: int) -> None:
                    progress(done, 2 * total)
            crossings = self.contour_crossings(
                route, cancel, crossing_progress)
            if marks:
                lo, hi = min(marks), max(marks)
                extra = [kp for kp, _depth in crossings if lo <= kp <= hi]
                if extra:
                    marks.extend(extra)
                    added_crossings = True
        if added_crossings:
            marks = sorted(set(round(float(kp), 12) for kp in marks))
        else:
            # Stations arrive sorted and unique — re-sorting 500k floats
            # per pass added nothing.
            marks = [round(float(kp), 12) for kp in marks]
        out: List[Tuple[float, Optional[float]]] = []
        total = max(len(marks), 1)
        for index, kp in enumerate(marks):
            if cancel is not None and index % 100 == 0 and cancel():
                raise ri.AcquisitionCancelled()
            out.append((kp, self.sample_route(route, kp)))
            if progress is not None and (index % 100 == 0 or index + 1 == total):
                if crossing_phase:
                    progress(total + index + 1, 2 * total)
                else:
                    progress(index + 1, total)
        return out


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
    # Thread-safe layer snapshots (QgsVectorLayerFeatureSource + CRS +
    # transform context), captured cheaply on the main thread; the actual
    # feature read + reprojection + spatial-index build happens in the
    # worker, cancellable — it used to freeze the UI before the task began.
    layer_snapshot: Optional[Dict] = None
    table_snapshot: Optional[Dict] = None
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
    # Resolution of the persisted/fallback bathymetry profile. This is
    # deliberately independent of ``step_m`` (the coarse rule-search step):
    # local slope must follow the terrain data resolution, not search spacing.
    depth_step_m: float = 0.0
    rules: List[RuleWork] = field(default_factory=list)
    # Persisted plan-profile samples (kp, depth magnitude | None), injected
    # when current so threshold acquisition skips resampling bathymetry.
    depth_samples: Optional[List[Tuple[float, Optional[float]]]] = None
    # Snapshot of the stored profile's cross-offset arrays for cross/absolute
    # slope criteria: {kps, depths, port, stbd, cross_offset_m} (plain lists —
    # copied on the main thread so the worker never shares live state).
    cross_profile: Optional[Dict] = None


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

    Positive signed slope = shoaling with increasing KP (up-slope). Installing
    against KP (direction -1) swaps which physical limit applies to which sign.
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
    from ..kp_geo_utils import crosses_antimeridian

    if crosses_antimeridian(geoms):
        raise ri.RuleInputError(
            "The route crosses the ±180° antimeridian, which the analysis "
            "geometry does not support — positions and intersections would "
            "be silently wrong. Split or shift the route first.")
    distance = make_distance_area(WGS84, project.transformContext(), project=project)
    # KP chainage remains ellipsoidal, but section/event geometry must follow
    # the RPL's stored line segments exactly. Great-circle interpolation
    # between sparse geographic vertices can otherwise render several metres
    # away from the source line.
    return RouteFrame.from_source(
        geoms, distance, follow_stored_geometry=True), distance


def build_work(route: RouteFrame, distance, plan: Dict, rule_rows: List[Dict],
               inputs: List[Dict], depth_config: DepthSourceConfig,
               params: generation.GenParams,
               cache: Dict[str, Tuple[List[Interval], List[Interval]]],
               rpl_fp: str,
               project: Optional[QgsProject] = None,
               depth_samples: Optional[List[Tuple[float, Optional[float]]]] = None,
               depth_step_m: Optional[float] = None,
               cross_profile: Optional[Dict] = None,
               ) -> Tuple["AnalysisWork", List[str]]:
    """Snapshot everything the task needs (main thread). Returns (work, warnings)."""
    project = project or QgsProject.instance()
    warnings: List[str] = []
    inputs_by_id = {str(r.get("input_id")): r for r in inputs}

    depth = DepthSnapshot(depth_config, project) if depth_config.is_configured() else None

    # The bathymetry fingerprint walks every configured layer (provider
    # timestamps, mtimes, feature counts); compute it once per build, not
    # once per threshold rule.
    _depth_fp: List[Optional[str]] = [None]

    def depth_fp() -> str:
        if _depth_fp[0] is None:
            _depth_fp[0] = map_layers.depth_config_fingerprint(
                project, depth_config)
        return _depth_fp[0]

    scope = params.scope
    work = AnalysisWork(
        route=route, distance=distance, scope=scope,
        step_m=params.coarse_step_m, direction=params.direction,
        method=params.method, refine_tol_m=params.refine_tol_m, depth=depth,
        depth_step_m=max(float(depth_step_m or params.coarse_step_m), 1.0),
        depth_samples=depth_samples, cross_profile=cross_profile)

    for row in rule_rows:
        if not int(row.get("enabled") or 0):
            continue
        try:
            methods = json.loads(row.get("methods_json") or "[]")
        except (ValueError, TypeError):
            methods = []
        methods = schema.normalise_methods(methods)
        if methods and schema.normalise_method(params.method) not in methods:
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
                    layer = ri.resolve_layer(project, rule_work.config)
                except ri.RuleInputError:
                    layer = None
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                rule_work.error = "input layer could not be resolved"
            else:
                input_fp = map_layers.layer_fingerprint(layer)
                if rule_work.kind == wb_schema.RULE_KIND_POLYGON and \
                        (rule_work.config.get("route_buffer_mode") or "").lower() == "wd":
                    if depth is None or not depth.is_available():
                        rule_work.error = ("a route corridor scaled by water "
                                           "depth needs a bathymetry source")
                    else:
                        # WD-scaled corridors also depend on the bathymetry:
                        # changing it must invalidate this rule's cache.
                        input_fp += "|" + depth_fp()
        elif rule_work.kind == wb_schema.RULE_KIND_THRESHOLD:
            if depth is None or not depth.is_available():
                rule_work.error = "no bathymetry source configured"
            else:
                input_fp = depth_fp()
                profile_kind = (rule_work.config.get("profile") or "depth").lower()
                component = (rule_work.config.get("slope_component") or "long") \
                    if profile_kind == "slope" else "long"
                if component in (profile_data.SLOPE_COMPONENT_CROSS,
                                 profile_data.SLOPE_COMPONENT_ABSOLUTE):
                    cross = cross_profile or {}
                    has_cross = bool(cross.get("kps")) \
                        and any(v is not None for v in cross.get("port") or []) \
                        and any(v is not None for v in cross.get("stbd") or [])
                    if not has_cross:
                        rule_work.error = (
                            "cross/absolute slope needs the stored bathymetry "
                            "profile with cross-offset samples — set a cross "
                            "offset and rebuild on the Bathymetry Profile tab")
                    else:
                        # The cross offset changes what the series measures.
                        input_fp += \
                            f"|cross={float(cross.get('cross_offset_m') or 0.0):g}"

        rule_work.cache_key = generation.rule_cache_key(
            row, input_fp, scope, params.coarse_step_m, rpl_fp, params.direction,
            profile_step_m=work.depth_step_m,
            refine_tol_m=params.refine_tol_m)
        cached = cache.get(rule_work.cache_key)
        if cached is not None and not rule_work.error:
            rule_work.cached = cached
        elif not rule_work.error and needs_layer and layer is not None:
            # Snapshot only (cheap): the feature read, reprojection and
            # index build run inside the task with cancellation, instead of
            # freezing the UI here for large constraint layers.
            try:
                snapshot = {
                    "source": QgsVectorLayerFeatureSource(layer),
                    "crs": layer.crs(),
                    "feature_count": max(int(layer.featureCount()), 0),
                    "transform_context": project.transformContext(),
                }
            except Exception as exc:
                rule_work.error = f"input layer could not be snapshotted ({exc})"
            else:
                if rule_work.kind == wb_schema.RULE_KIND_KP_TABLE:
                    snapshot["fields"] = [f.name() for f in layer.fields()]
                    snapshot["filter"] = rule_work.config.get(
                        "filter_expression", "")
                    rule_work.table_snapshot = snapshot
                else:
                    rule_work.layer_snapshot = snapshot
                    rule_work.geom_type = layer.geometryType()
        if rule_work.error:
            warnings.append(
                f"Rule '{row.get('name') or rule_work.kind}': {rule_work.error} — skipped.")
        work.rules.append(rule_work)
    return work, warnings


# ---------------------------------------------------------------------------
# Predicates for 0.1 m boundary refinement (§14.3)
# ---------------------------------------------------------------------------

def _profile_depth_lookup(
        samples: List[Tuple[float, Optional[float]]],
        ) -> Optional[Callable[[float], Optional[float]]]:
    """Linear depth lookup over the already-sampled profile.

    This keeps 0.1 m boundary refinement on the same immutable data used for
    acquisition and avoids returning to a raster provider for every bisection
    evaluation. Interpolation never bridges a no-data station.
    """
    ordered = [(float(kp), None if value is None else abs(float(value)))
               for kp, value in samples]
    if not ordered:
        return None
    if any(ordered[i][0] > ordered[i + 1][0]
           for i in range(len(ordered) - 1)):
        # Key on KP only: tied KPs would otherwise compare the Optional
        # depth values, and None < float raises TypeError.
        ordered.sort(key=lambda item: item[0])
    xs = [kp for kp, _value in ordered]
    values = [value for _kp, value in ordered]

    def lookup(kp: float) -> Optional[float]:
        target = float(kp)
        if target < xs[0] - 1e-9 or target > xs[-1] + 1e-9:
            return None
        index = bisect.bisect_left(xs, target)
        if index < len(xs) and abs(xs[index] - target) <= 1e-9:
            return values[index]
        if index == 0 or index >= len(xs):
            return None
        v0, v1 = values[index - 1], values[index]
        if v0 is None or v1 is None:
            return None
        dx = xs[index] - xs[index - 1]
        if dx <= 1e-12:
            return v1
        ratio = (target - xs[index - 1]) / dx
        return v0 + ratio * (v1 - v0)

    return lookup


def _threshold_predicate(
        work: AnalysisWork, config: Dict,
        slope_step_km: Optional[float] = None,
        sampled_depth_at: Optional[Callable[[float], Optional[float]]] = None,
        ) -> Optional[Callable[[float], bool]]:
    depth = work.depth
    route = work.route
    if depth is None:
        return None

    def depth_at(kp: float) -> Optional[float]:
        if sampled_depth_at is not None:
            return sampled_depth_at(kp)
        value = depth.sample_route(route, kp)
        return abs(float(value)) if value is not None else None

    profile = (config.get("profile") or "depth").lower()

    def value_at(kp: float) -> Optional[float]:
        if profile != "slope":
            return depth_at(kp)
        # Use the same scale as acquisition: the rule's explicit evaluation
        # length (vehicle footprint) when configured, else the persisted
        # bathymetry-profile step. The latter preserves local terrain that a
        # much wider coarse rule-search interval would average away.
        # A mismatched window here could turn raster noise (or contour steps)
        # into severe slopes the coarse pass never saw, silently defeating
        # refinement. Clamp to the route, not the scope: coarse stations carry
        # a one-step margin outside the scope, so clamping tighter here would
        # give the predicate a different (narrower) window at the scope edges.
        delta_km = ri.slope_half_window_km(config, slope_step_km)
        if delta_km is None:
            delta_km = max(float(work.depth_step_m or work.step_m), 1.0) / 1000.0
        kp0 = max(0.0, kp - delta_km)
        kp1 = min(route.total_length_km, kp + delta_km)
        d0 = depth_at(kp0)
        d1 = depth_at(kp1)
        if d0 is None or d1 is None:
            return None
        dx_m = (kp1 - kp0) * 1000.0
        dz = d1 - d0
        if config.get("slope_signed"):
            # Depth magnitudes: negate so +ve = shoaling (up-slope).
            return math.degrees(math.atan2(-dz, dx_m))
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
                # +ve slope = shoaling: up-slope limit on the positive side.
                return ((down is not None and value < -abs(float(down)))
                        or (up is not None and value > abs(float(up))))
            limit = band.get("limit")
            if limit is None:
                return False
            lo, hi = eng.cond_bounds(op, float(limit), None)
            return lo <= value <= hi
        if signed:
            down = config.get("downslope_max_deg")
            up = config.get("upslope_max_deg")
            return ((down is not None and value < -abs(float(down)))
                    or (up is not None and value > abs(float(up))))
        value2 = config.get("value2")
        lo, hi = eng.cond_bounds(op, float(config.get("value", 0.0)),
                                  float(value2) if value2 is not None else None)
        if bool(config.get("abs", False)):
            value = abs(value)
        return lo <= value <= hi

    return predicate


def _geometry_predicate(work: AnalysisWork, rule_work: RuleWork,
                        depth_at: Optional[Callable[[float], Optional[float]]] = None
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
    expr, ctx = ri.filter_expression(
        config.get("match_expression" if kind == wb_schema.RULE_KIND_POLYGON
                   else "filter_expression", ""))
    # Same per-KP route-corridor buffer as polygon acquisition, so boundary
    # refinement bisects the identical condition.
    route_buffer_at = (ri.polygon_route_buffer_m_at(config, depth_at)
                       if kind == wb_schema.RULE_KIND_POLYGON else None)

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

    # The widest per-feature buffer bounds the candidate search rect; compute
    # it once — the predicate is evaluated many times during bisection and
    # re-scanning every feature per call made refinement O(features) per step.
    max_buffer = buffer_m
    if buffer_field:
        for _geom, feat in feats.values():
            max_buffer = max(max_buffer,
                             ri.feature_buffer_m(feat, buffer_field, buffer_m))

    scaled_cache: Dict = {}  # isotropic-frame copies, shared across bisections
    def predicate(kp: float) -> bool:
        pt = route.point_at_kp(kp, clamp=True)
        if pt is None:
            return False
        pt_geom = QgsGeometry.fromPointXY(pt)
        if kind == wb_schema.RULE_KIND_POLYGON:
            corridor_m = max(0.0, float(route_buffer_at(kp))) \
                if route_buffer_at is not None else 0.0
            for fid in index.intersects(ri.search_rect(pt, max(corridor_m, 1.0))):
                geom, feat = feats[fid]
                if not matches(feat):
                    continue
                if geom.contains(pt_geom):
                    return True
                if corridor_m > 0 and ri.distance_to_geom_m(
                        distance, pt, geom, scaled_cache) <= corridor_m + 1e-6:
                    return True
            return False
        for fid in index.intersects(ri.search_rect(pt, max(max_buffer, 1.0))):
            geom, feat = feats[fid]
            if not matches(feat):
                continue
            fb = ri.feature_buffer_m(feat, buffer_field, buffer_m)
            if rule_work.geom_type == GEOMETRY_POLYGON and geom.contains(pt_geom):
                return True
            if ri.distance_to_geom_m(distance, pt, geom,
                                     scaled_cache) <= fb + 1e-6:
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
        self._sampled_depth_at: Optional[
            Callable[[float], Optional[float]]] = None
        # Signed slope is the common source for signed and magnitude rules.
        # Cache by physical half-window so a 500k-station route is derived
        # once for every distinct local/vehicle-footprint scale.
        self._signed_slope_cache: Dict[
            float, List[Tuple[float, float]]] = {}

    # -- worker thread -------------------------------------------------------
    def run(self) -> bool:  # noqa: C901 — one linear pipeline, clearer inline
        try:
            work = self.work

            def rule_needs_bathy(rw: RuleWork) -> bool:
                if rw.cached is not None or rw.error:
                    return False
                if rw.kind == wb_schema.RULE_KIND_POLYGON:
                    return (rw.config.get("route_buffer_mode")
                            or "").lower() == "wd"
                if rw.kind != wb_schema.RULE_KIND_THRESHOLD:
                    return False
                profile_kind = (rw.config.get("profile") or "depth").lower()
                component = (rw.config.get("slope_component") or "long") \
                    if profile_kind == "slope" else "long"
                # Cross/absolute slope reads the stored cross-profile
                # arrays, not the live bathymetry sources.
                return component not in (profile_data.SLOPE_COMPONENT_CROSS,
                                         profile_data.SLOPE_COMPONENT_ABSOLUTE)

            total = max(len(work.rules), 1)
            if (work.depth_samples is None and work.depth is not None
                    and work.depth.is_available()
                    and any(rule_needs_bathy(rw) for rw in work.rules)):
                self.progressMessage.emit("Preparing bathymetry sources…")
                if not work.depth.prepare(
                        cancel=self.isCanceled,
                        progress=lambda done, count: self.setProgress(
                            8.0 * float(done) / max(float(count), 1.0))):
                    self.cancelled = True
                    return False
            self.progressMessage.emit("Building route stations…")
            sampler = ri.RouteSampler.from_route(
                work.route, work.distance, work.step_m, work.scope)
            self._sampler = sampler
            self.setProgress(10.0)
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
                # Sub-progress inside the rule's slot, so long phases
                # (bathymetry sampling) advance the bar instead of stalling it.
                base = 10.0 + 90.0 * i / total
                span = 90.0 / total

                def sub_progress(fraction: float, _base=base, _span=span) -> None:
                    self.setProgress(_base + _span * min(max(fraction, 0.0), 1.0))

                if rule_work.error:
                    result.error = rule_work.error
                elif rule_work.cached is not None:
                    result.footprint, result.nodata = rule_work.cached
                    result.from_cache = True
                else:
                    try:
                        result.footprint, result.nodata = self._acquire(
                            sampler, rule_work, coarse_step_km, tol_km,
                            progress=sub_progress)
                    except ri.AcquisitionCancelled:
                        self.cancelled = True
                        return False
                    except ri.RuleInputError as exc:
                        result.error = str(exc)
                    except Exception as exc:  # never let one rule crash the run
                        result.error = f"unexpected error ({exc})"
                self.results.append(result)
                self.setProgress(10.0 + 90.0 * (i + 1) / total)
            self.setProgress(100.0)
            return True
        except Exception as exc:  # pragma: no cover — task-level fail-safe
            self.error = str(exc)
            return False

    def _ensure_depth_lookup(self, sampler: ri.RouteSampler,
                             sample_progress: Optional[Callable[[int, int], None]] = None
                             ) -> None:
        """Build (once per run) the depth series, no-data gaps and lookup.

        Shared by threshold rules and any rule needing water depth at a KP
        (polygon route-corridor ×WD buffers).
        """
        if self._depth_series is not None:
            return
        work = self.work
        if work.depth_samples is not None:
            # Reuse the persisted plan profile — no resampling.
            self.progressMessage.emit("Using stored plan profile samples…")
            samples = work.depth_samples
        else:
            if work.depth is None:
                raise ri.RuleInputError("no bathymetry source configured")
            self.progressMessage.emit("Sampling bathymetry along the scope…")
            # Threshold acquisition follows the persisted-profile
            # resolution even when no current stored profile was
            # available. The coarse sampler remains appropriate for
            # geometry rules and boundary brackets, but would miss or
            # flatten short terrain features.
            marks = sampler.stations_km
            if abs(float(work.depth_step_m) - float(work.step_m)) > 1e-9:
                # Build only scalar KPs here. RouteSampler would also
                # allocate hundreds of thousands of QgsPointXY objects
                # which profile_samples immediately recomputes.
                lo = min(sampler.stations_km)
                hi = max(sampler.stations_km)
                step_km = max(float(work.depth_step_m), 1.0) / 1000.0
                count = max(int(math.ceil((hi - lo) / step_km)) + 1, 1)
                marks = [min(lo + i * step_km, hi) for i in range(count)]
            samples = work.depth.profile_samples(
                work.route, marks, cancel=self.isCanceled,
                progress=sample_progress)
        self._depth_series = [
            (kp, abs(float(value))) for kp, value in samples
            if value is not None]
        flags = [(kp, value is None) for kp, value in samples]
        self._depth_gaps = eng.intervals_from_bool_series(
            flags, sampler.scope_domain)
        self._sampled_depth_at = _profile_depth_lookup(samples)

    def _component_slope_acquire(self, sampler: ri.RouteSampler, config: Dict,
                                 component: str
                                 ) -> Tuple[List[Interval], List[Interval]]:
        """Cross / absolute slope intervals from the stored profile arrays.

        The series is evaluated at the stored-profile stations and linearly
        interpolated between them (``intervals_from_profile``), so boundary
        precision follows the profile resolution. Stations where the
        component cannot be evaluated (no depth, or no cross sample for the
        cross component) surface as no-data -> Insufficient Information.
        """
        work = self.work
        cross = work.cross_profile or {}
        kps = cross.get("kps") or []
        if not kps:
            raise ri.RuleInputError(
                "cross/absolute slope needs the stored bathymetry profile "
                "with cross-offset samples — rebuild it on the Bathymetry "
                "Profile tab")
        slope_step_km = max(float(work.depth_step_m or work.step_m), 1.0) / 1000.0
        half_km = ri.slope_half_window_km(config, slope_step_km) or slope_step_km
        # require_cross: an absolute-slope *rule* must never pass on the
        # |longitudinal| lower bound the display pane shows where cross
        # samples are missing — those stations go no-data -> Insufficient.
        series = profile_data.slope_component_series(
            kps, cross.get("depths") or [], cross.get("port") or [],
            cross.get("stbd") or [], float(cross.get("cross_offset_m") or 0.0),
            work.direction, component, half_km, require_cross=True)
        valid = [(kp, value) for kp, value in series if value is not None]
        if not valid:
            raise ri.RuleInputError(
                "the stored profile has no evaluable cross-slope stations in "
                "the scope — rebuild it with a cross offset")
        flags = [(kp, value is None) for kp, value in series]
        nodata = eng.intervals_from_bool_series(flags, sampler.scope_domain)
        bands = config.get("bands") or []
        if bands:
            wd_series = [(kp, depth) for kp, depth
                         in zip(kps, cross.get("depths") or [])
                         if depth is not None]
            intervals = eng.intervals_from_banded_threshold(
                valid, wd_series, bands, config.get("op") or ">",
                sampler.scope_domain)
        else:
            value2 = config.get("value2")
            intervals = eng.intervals_from_profile(
                valid, config.get("op") or ">",
                float(config.get("value", 0.0)),
                float(value2) if value2 is not None else None,
                abs_value=True)
        return intervals, nodata

    def _materialise_rule_inputs(self, rule_work: RuleWork) -> None:
        """Build the rule's feature index / table rows from its snapshot.

        Runs on the worker thread with cooperative cancellation — the
        loading used to happen in ``build_work`` and froze the UI for large
        constraint layers before the progress bar even appeared.
        """
        snap = rule_work.layer_snapshot
        if snap is not None and rule_work.feats is None:
            name = rule_work.rule_row.get("name") or rule_work.kind
            self.progressMessage.emit(f"Loading features: {name}")
            index, feats = ri.load_features_wgs84_from_source(
                snap["source"], snap["crs"], snap["transform_context"],
                cancel=self.isCanceled,
                feature_count=snap.get("feature_count", 0))
            rule_work.feats = (index, feats)
        table_snap = rule_work.table_snapshot
        if table_snap is not None and rule_work.table_rows is None:
            expr, ctx = ri.filter_expression(table_snap.get("filter", ""))
            names = table_snap.get("fields") or []
            rows: List[Dict] = []
            for i, feat in enumerate(table_snap["source"].getFeatures()):
                if i % 500 == 0 and self.isCanceled():
                    raise ri.AcquisitionCancelled()
                if expr is not None:
                    ctx.setFeature(feat)
                    if not bool(expr.evaluate(ctx)):
                        continue
                rows.append({name: feat[name] for name in names})
            rule_work.table_rows = rows

    def _acquire(self, sampler: ri.RouteSampler, rule_work: RuleWork,
                 coarse_step_km: float, tol_km: float,
                 progress: Optional[Callable[[float], None]] = None
                 ) -> Tuple[List[Interval], List[Interval]]:
        work = self.work
        config = rule_work.config
        kind = rule_work.kind
        cancel = self.isCanceled
        nodata: List[Interval] = []
        predicate: Optional[Callable[[float], bool]] = None
        self._materialise_rule_inputs(rule_work)

        def sample_progress(done: int, count: int) -> None:
            if progress is not None:
                # Sampling dominates the rule's slot; keep 10% for the rest.
                progress(0.9 * float(done) / max(float(count), 1.0))

        profile_kind = (config.get("profile") or "depth").lower()
        component = (config.get("slope_component") or "long") \
            if profile_kind == "slope" else "long"
        if kind == wb_schema.RULE_KIND_THRESHOLD and component in (
                profile_data.SLOPE_COMPONENT_CROSS,
                profile_data.SLOPE_COMPONENT_ABSOLUTE):
            # Cross/absolute slope evaluates the stored profile's cross-offset
            # arrays; boundaries come interpolated from the series itself at
            # profile resolution, so no bisection predicate is needed.
            intervals, nodata = self._component_slope_acquire(
                sampler, config, component)
        elif kind == wb_schema.RULE_KIND_THRESHOLD:
            self._ensure_depth_lookup(sampler, sample_progress)
            if not self._depth_series:
                raise ri.RuleInputError("bathymetry has no coverage in the scope")
            slope_step_km = max(
                float(work.depth_step_m or work.step_m), 1.0) / 1000.0
            prepared_slope = None
            if profile_kind == "slope":
                half_km = ri.slope_half_window_km(config, slope_step_km)
                half_km = max(float(half_km or slope_step_km), 1e-9)
                cache_key = round(half_km, 12)
                signed_series = self._signed_slope_cache.get(cache_key)
                if signed_series is None:
                    signed_series = eng.signed_slope_series(
                        self._depth_series, half_km)
                    self._signed_slope_cache[cache_key] = signed_series
                prepared_slope = (signed_series if config.get("slope_signed")
                                  else [(kp, abs(value))
                                        for kp, value in signed_series])
            intervals = ri.threshold_intervals(
                self._depth_series, config, sampler.scope_domain,
                step_km=slope_step_km,
                prepared_slope_series=prepared_slope)
            nodata = list(self._depth_gaps or [])
            predicate = _threshold_predicate(
                work, config, slope_step_km=slope_step_km,
                sampled_depth_at=self._sampled_depth_at)
        elif kind in (wb_schema.RULE_KIND_PROXIMITY, wb_schema.RULE_KIND_POLYGON):
            if rule_work.feats is None:
                raise ri.RuleInputError("input layer could not be resolved")
            index, feats = rule_work.feats
            if kind == wb_schema.RULE_KIND_PROXIMITY:
                intervals = ri.proximity_intervals(
                    sampler, index, feats, rule_work.geom_type, config, cancel=cancel)
                predicate = _geometry_predicate(work, rule_work)
            else:
                depth_at = None
                if (config.get("route_buffer_mode") or "").lower() == "wd":
                    self._ensure_depth_lookup(sampler, sample_progress)
                    depth_at = self._sampled_depth_at
                intervals = ri.polygon_class_intervals(
                    sampler, index, feats, config, cancel=cancel,
                    depth_at=depth_at)
                predicate = _geometry_predicate(work, rule_work,
                                                depth_at=depth_at)
        elif kind == wb_schema.RULE_KIND_KP_TABLE:
            intervals = ri.kp_table_intervals(
                rule_work.table_rows or [], config, sampler.scope_domain)
        elif kind == wb_schema.RULE_KIND_MANUAL:
            intervals = ri.acquire_manual(sampler, config)
        else:
            raise ri.RuleInputError(f"unknown rule kind '{kind}'")

        scope_ranges = ri.scope_intervals(config)
        intervals = eng.clip_intervals(intervals, sampler.scope_domain)
        if scope_ranges is not None:
            intervals = eng.intersect_intervals(intervals, scope_ranges)

        if predicate is not None and intervals:
            self.progressMessage.emit(
                f"Refining boundaries: {rule_work.rule_row.get('name') or kind}")
            try:
                intervals = generation.refine_intervals(
                    intervals, predicate, coarse_step_km,
                    sampler.scope_domain, tol_km, cancel=cancel)
            except generation.RefinementCancelled:
                raise ri.AcquisitionCancelled()
        return intervals, nodata

    # -- main thread ---------------------------------------------------------
    def finished(self, ok: bool) -> None:
        if not ok and not self.cancelled and self.error is None:
            self.error = "Analysis task failed."
        try:
            self._on_finished(self)
        except Exception:  # never crash QGIS from a completion callback
            _log_callback_failure("Burial Planner analysis")


class ProfileSamplingTask(QgsTask):
    """Cancellable background depth sampling for the persistent profile.

    With ``distance`` and ``cross_offset_m`` set, each station additionally
    samples depth at ± the cross offset perpendicular to the route (geodesic
    offset via ``computeSpheroidProject``; rasters point-sampled, contours
    interpolated between offset-polyline crossings), feeding the
    cross/absolute slope series. Results: ``series`` (kp, depth magnitude —
    data stations only)
    plus the full raw arrays ``kps`` / ``depths`` / ``port_depths`` /
    ``stbd_depths`` (``None`` = no data) for persistence.
    """

    progressMessage = pyqtSignal(str)

    def __init__(self, route: RouteFrame, depth: DepthSnapshot,
                 start_kp: float, end_kp: float, step_m: float,
                 on_finished: Callable[["ProfileSamplingTask"], None],
                 distance=None, cross_offset_m: float = 0.0):
        super().__init__("Burial Planner profile", _CAN_CANCEL)
        self.route = route
        self.depth = depth
        self.start_kp = min(float(start_kp), float(end_kp))
        self.end_kp = max(float(start_kp), float(end_kp))
        self.step_m = max(float(step_m), 1.0)
        self.distance = distance
        self.cross_offset_m = max(float(cross_offset_m or 0.0), 0.0)
        self.series: List[Tuple[float, float]] = []
        self.kps: List[float] = []
        self.depths: List[Optional[float]] = []
        self.port_depths: List[Optional[float]] = []
        self.stbd_depths: List[Optional[float]] = []
        self.error: Optional[str] = None
        self.cancelled = False
        self._on_finished = on_finished

    def _cross_sample(self, kp: float) -> Tuple[Optional[float], Optional[float]]:
        """(port, starboard) depth magnitudes at ± the cross offset.

        Starboard is to the right of increasing KP; the direction −1 sign
        flip happens in the pure slope series, not here.
        """
        point = self.route.point_at_kp(kp, clamp=True)
        if point is None:
            return None, None
        delta_km = max(self.step_m / 2000.0, 1e-4)
        p0 = self.route.point_at_kp(max(kp - delta_km, 0.0), clamp=True)
        p1 = self.route.point_at_kp(
            min(kp + delta_km, self.route.total_length_km), clamp=True)
        if p0 is None or p1 is None:
            return None, None
        try:
            azimuth = float(self.distance.bearing(p0, p1))
        except Exception:
            return None, None
        out = []
        for side in (-1.0, 1.0):  # port first, then starboard
            try:
                offset_pt = self.distance.computeSpheroidProject(
                    point, self.cross_offset_m, azimuth + side * math.pi / 2.0)
                value = self.depth.sample(offset_pt.y(), offset_pt.x())
            except Exception:
                value = None
            out.append(abs(float(value))
                       if value is not None and value == value else None)
        return out[0], out[1]

    def run(self) -> bool:
        try:
            if not self.depth.is_available():
                self.error = "No configured bathymetry layer is available in the project."
                return False
            self.progressMessage.emit("Preparing bathymetry…")
            cross = self.cross_offset_m > 0 and self.distance is not None
            along_share = 50.0 if cross else 75.0

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
            marks = [min(self.start_kp + i * self.step_m / 1000.0,
                         self.end_kp) for i in range(count)]

            def sample_progress(done: int, total: int) -> None:
                self.setProgress(25.0 + along_share * float(done) /
                                 max(float(total), 1.0))

            samples = self.depth.profile_samples(
                self.route, marks, cancel=self.isCanceled,
                progress=sample_progress)
            self.kps = [kp for kp, _value in samples]
            self.depths = [abs(float(value))
                           if value is not None and value == value else None
                           for _kp, value in samples]
            self.series = [(kp, depth) for kp, depth
                           in zip(self.kps, self.depths) if depth is not None]

            if cross:
                self.progressMessage.emit(
                    f"Sampling cross-offset depths (±{self.cross_offset_m:.0f} m)…")
                offset_fn = getattr(self.depth, "offset_profile_samples", None)
                if callable(offset_fn):
                    # One pass per side: rasters point-sampled at the offset
                    # positions, contours intersected with the offset
                    # polylines and interpolated between crossings (the
                    # along-route methodology — per-station nearest-contour
                    # scans are O(stations × contours) and stall for hours
                    # on long routes).
                    def cross_progress(done: int, total_: int) -> None:
                        self.setProgress(75.0 + 25.0 * float(done) /
                                         max(float(total_), 1.0))

                    port_values, stbd_values = offset_fn(
                        self.route, self.kps, self.cross_offset_m,
                        self.distance, cancel=self.isCanceled,
                        progress=cross_progress)

                    def magnitudes(values):
                        return [abs(float(v))
                                if v is not None and v == v else None
                                for v in values]

                    self.port_depths = magnitudes(port_values)
                    self.stbd_depths = magnitudes(stbd_values)
                else:
                    total = max(len(self.kps), 1)
                    for index, kp in enumerate(self.kps):
                        if index % 50 == 0 and self.isCanceled():
                            self.cancelled = True
                            return False
                        port, stbd = self._cross_sample(kp)
                        self.port_depths.append(port)
                        self.stbd_depths.append(stbd)
                        if index % 100 == 0 or index + 1 == total:
                            self.setProgress(75.0 + 25.0 * (index + 1) / total)
            else:
                self.port_depths = [None] * len(self.kps)
                self.stbd_depths = [None] * len(self.kps)
            self.setProgress(100.0)
            return True
        except ri.AcquisitionCancelled:
            self.cancelled = True
            return False
        except Exception as exc:  # pragma: no cover - surfaced in the dock
            self.error = str(exc)
            return False

    def finished(self, ok: bool) -> None:
        if not ok and not self.cancelled and self.error is None:
            self.error = "Profile sampling task failed."
        try:
            self._on_finished(self)
        except Exception:  # never crash QGIS from a completion callback
            _log_callback_failure("Burial Planner profile")
