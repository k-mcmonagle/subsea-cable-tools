# -*- coding: utf-8 -*-
"""Depth Profile computation core: stationing, sampling, slopes, seabed length.

Everything the Depth Profile dock computes lives here, free of widgets,
``iface`` and ``QgsProject.instance()``, so it can run in a QgsTask:

* :class:`RouteStationing` - chainage along the (possibly multi-part)
  profile line: the point at a distance, the distance of a point.
* :func:`build_request` - **main thread**: snapshots the inputs (cloned
  raster providers, ``QgsVectorLayerFeatureSource`` contours, layer
  options, CRS, transform context, parameters) into a :class:`ProfileRequest`.
* :func:`run_profile` - **worker safe**: samples the profile (raster or
  contours), side slopes, slopes and seabed length into a
  :class:`ProfileResult`. It never touches live layers or the project.
* :class:`DepthProfileTask` - the cancellable QgsTask around ``run_profile``.

The slope, crossing and cross-profile maths are the plugin-wide
``slope_utils`` engine; raster reads are the shared
``bathymetry_sampling.RasterSampler``. Results are identical to the
pre-refactor dock (pinned by tests/test_depth_profile_core.py).
"""

from __future__ import annotations

import bisect
import logging
import math
import traceback
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from qgis.PyQt import sip
from qgis.core import (
    QgsCoordinateTransform, QgsCsException, QgsFeature, QgsFeatureRequest, QgsGeometry,
    QgsPointXY, QgsRasterLayer, QgsRectangle, QgsSpatialIndex, QgsTask,
    QgsVectorLayerFeatureSource,
)

from .bathymetry_sampling import RasterSampler, expand_rasters, layer_options, metres_per_unit, normalise_depth
from .plugin_log import log_exception, log_warning
from .qgis_compat import GEOMETRY_LINE, GEOMETRY_POINT
from .slope_utils import clean_crossings, contiguous_runs, cross_profile_metrics, supported_slopes

RASTER = "Raster"
CONTOURS = "Contours"

# User message levels; the dock maps them onto message-bar levels.
INFO, WARNING, CRITICAL = "info", "warning", "critical"


class ProfileCancelled(Exception):
    """The along-route pass was cancelled; there is no result."""


class _Failures:
    """Per-item failures swallowed inside sampling loops.

    A loop over thousands of stations must not log per item, nor silently
    drop data: each kind is counted and logged once per run with its first
    traceback, and the user is told values were left blank.
    """

    def __init__(self):
        self._seen: Dict[str, List] = {}

    def note(self, what: str) -> None:
        entry = self._seen.get(what)
        if entry is None:
            self._seen[what] = [1, traceback.format_exc()]
        else:
            entry[0] += 1

    def report(self, messages: List[Tuple[str, str, int]]) -> None:
        total = 0
        for what, (count, detail) in self._seen.items():
            total += count
            log_warning(f"Depth profile: {what} ({count:,} time(s)). First failure:\n{detail.rstrip()}")
        if total:
            messages.append((f"{total:,} sample(s) or feature(s) could not be processed; the affected "
                             "values are blank. Details in the Subsea Cable Tools log.", WARNING, 8))


# ---------------------------------------------------------------------------
# Stationing
# ---------------------------------------------------------------------------

class RouteStationing:
    """Chainage (metres, KP distance area) along the profile line.

    ``line_parts`` are polylines (lists of QgsPointXY) in ``crs``;
    ``line_length`` is the route's KP length. Segment stations are cached
    for fast interpolation and vectorised point-to-route projection.
    """

    def __init__(self, line_parts, line_length, crs, distance_area):
        self.line_parts = line_parts
        self.line_length = line_length
        self.crs = crs
        self.distance_area = distance_area
        self._seg_starts = None
        self._seg_ends = None
        self._seg_lens = None
        self._seg_p1 = None
        self._seg_p2 = None
        self._seg_np = None
        self._build_cache()

    def _build_cache(self):
        starts, ends, lens, p1s, p2s = [], [], [], [], []
        planar = 0
        cum = 0.0
        for part in self.line_parts:
            if not part or len(part) < 2:
                continue
            for p1, p2 in zip(part[:-1], part[1:]):
                try:
                    seg_len = float(self.distance_area.measureLine(p1, p2))
                except Exception:
                    # Planar length in CRS units: wrong on a geographic
                    # route, so never silent.
                    if not planar:
                        log_exception("Depth profile: KP segment length failed; using planar length")
                    planar += 1
                    seg_len = math.hypot(p2.x() - p1.x(), p2.y() - p1.y())
                if seg_len <= 0:
                    continue
                starts.append(cum)
                lens.append(seg_len)
                p1s.append(QgsPointXY(p1.x(), p1.y()))
                p2s.append(QgsPointXY(p2.x(), p2.y()))
                cum += seg_len
                ends.append(cum)
        if planar > 1:
            log_warning(f"Depth profile: planar length used for {planar} route segment(s)")
        if not ends:
            return
        self._seg_starts, self._seg_ends, self._seg_lens = starts, ends, lens
        self._seg_p1, self._seg_p2 = p1s, p2s
        # Planar segment arrays for vectorised point-to-route projection.
        x1 = np.array([p.x() for p in p1s], dtype=float)
        y1 = np.array([p.y() for p in p1s], dtype=float)
        dx = np.array([p.x() for p in p2s], dtype=float) - x1
        dy = np.array([p.y() for p in p2s], dtype=float) - y1
        self._seg_np = (x1, y1, dx, dy, dx * dx + dy * dy,
                        np.asarray(starts, dtype=float), np.asarray(lens, dtype=float))

    def point_at(self, distance_m) -> Optional[QgsPointXY]:
        """Point (route CRS) at a chainage; the ends clamp."""
        if distance_m <= 0:
            return QgsPointXY(self.line_parts[0][0])
        if distance_m >= self.line_length:
            return QgsPointXY(self.line_parts[-1][-1])
        if self._seg_ends:
            idx = bisect.bisect_left(self._seg_ends, float(distance_m))
            if idx >= len(self._seg_ends):
                idx = len(self._seg_ends) - 1
            seg_start = self._seg_starts[idx]
            seg_len = self._seg_lens[idx]
            p1 = self._seg_p1[idx]
            p2 = self._seg_p2[idx]
            ratio = (float(distance_m) - float(seg_start)) / float(seg_len) if seg_len > 0 else 0.0
            ratio = max(0.0, min(1.0, ratio))
            return QgsPointXY(p1.x() + ratio * (p2.x() - p1.x()), p1.y() + ratio * (p2.y() - p1.y()))
        cumulative = 0.0
        for part in self.line_parts:
            for i in range(len(part) - 1):
                p1 = part[i]; p2 = part[i + 1]
                seg_len = self.distance_area.measureLine(p1, p2)
                if cumulative + seg_len >= distance_m:
                    ratio = (distance_m - cumulative) / seg_len if seg_len > 0 else 0
                    return QgsPointXY(p1.x() + ratio * (p2.x() - p1.x()), p1.y() + ratio * (p2.y() - p1.y()))
                cumulative += seg_len
        return None

    def measure_along(self, pt_xy) -> Optional[float]:
        """Chainage (m) of the nearest point on the route to pt_xy (route CRS)."""
        if self._seg_np is not None:
            x1, y1, dx, dy, seg_sq, starts, lens = self._seg_np
            px, py = float(pt_xy.x()), float(pt_xy.y())
            t = np.clip(((px - x1) * dx + (py - y1) * dy) / seg_sq, 0.0, 1.0)
            dist_sq = (px - (x1 + t * dx)) ** 2 + (py - (y1 + t * dy)) ** 2
            best = int(np.argmin(dist_sq))
            return float(starts[best] + t[best] * lens[best])
        # Walk segments accumulating length until the projection point.
        cumulative = 0.0
        best_dist = None
        best_cum = None
        for part in self.line_parts:
            for i in range(len(part) - 1):
                p1 = part[i]; p2 = part[i + 1]
                seg_len = self.distance_area.measureLine(p1, p2)
                dx = p2.x() - p1.x(); dy = p2.y() - p1.y()
                seg_sq = dx * dx + dy * dy
                if seg_sq <= 0:
                    cumulative += seg_len
                    continue
                t = ((pt_xy.x() - p1.x()) * dx + (pt_xy.y() - p1.y()) * dy) / seg_sq
                t_clamped = max(0.0, min(1.0, t))
                proj_x = p1.x() + t_clamped * dx; proj_y = p1.y() + t_clamped * dy
                dist_sq = (pt_xy.x() - proj_x) ** 2 + (pt_xy.y() - proj_y) ** 2
                if best_dist is None or dist_sq < best_dist:
                    best_dist = dist_sq
                    best_cum = cumulative + t_clamped * seg_len
                cumulative += seg_len
        return best_cum

    def geometry(self) -> QgsGeometry:
        if len(self.line_parts) > 1:
            return QgsGeometry.collectGeometry([QgsGeometry.fromPolylineXY(part) for part in self.line_parts])
        return QgsGeometry.fromPolylineXY(self.line_parts[0])

    def filter_rect(self, target_crs, transform_context, buffer_m=0.0) -> Optional[QgsRectangle]:
        """Route bounding box (plus buffer_m) in target_crs, or None."""
        xs = [p.x() for part in self.line_parts for p in part]
        ys = [p.y() for part in self.line_parts for p in part]
        if not xs:
            return None
        crs = self.crs
        if buffer_m > 0:
            if crs is not None and crs.isGeographic():
                lat = max(abs(min(ys)), abs(max(ys)))
                buf = buffer_m / (111320.0 * max(math.cos(math.radians(min(lat, 89.0))), 0.05))
            else:
                buf = buffer_m / (metres_per_unit(crs) if crs is not None else 1.0)
        else:
            buf = 0.0
        rect = QgsRectangle(min(xs) - buf, min(ys) - buf, max(xs) + buf, max(ys) + buf)
        if crs is not None and target_crs is not None and crs != target_crs:
            try:
                rect = QgsCoordinateTransform(crs, target_crs, transform_context).transformBoundingBox(rect)
            except QgsCsException:
                # No spatial filter: every feature is read, same result.
                log_exception("Depth profile: route extent not transformable; reading all features",
                              level=logging.DEBUG)
                return None
        return rect


def route_from_points(points, crs, distance_area):
    """Drawn line (route CRS points) -> ``(RouteStationing, status)``."""
    if len(points) < 2:
        return None, "Drawn line must have at least 2 points"
    line_length = sum(distance_area.measureLine(a, b) for a, b in zip(points[:-1], points[1:]))
    if line_length <= 0:
        return None, "Drawn line length is zero"
    return RouteStationing([list(points)], line_length, crs, distance_area), None


def route_from_features(features, crs, distance_area):
    """Route line features -> ``(RouteStationing, status)``.

    One feature keeps its digitised vertex order (KP 0 = first vertex);
    several are joined by the shared route builder (SeqNo/layer order,
    touching features joined, never noded or re-ordered). A zero-length
    route is returned *with* its status so the caller can still report it.
    """
    geoms = [f.geometry() for f in features]
    if len(geoms) == 1:
        merged = QgsGeometry(geoms[0])
    else:
        from .kp_geo_utils import ordered_route_geometry
        merged = ordered_route_geometry(features)
    if merged.isEmpty():
        return None, "Merged route geometry empty"
    parts = merged.asMultiPolyline() if merged.isMultipart() else [merged.asPolyline()]
    line_parts = [part for part in parts if len(part) >= 2]
    if not line_parts:
        return None, "Route geometry has no line segments"
    route = RouteStationing(line_parts, distance_area.measureLength(merged), crs, distance_area)
    return route, ("Route length is zero" if route.line_length <= 0 else None)


# ---------------------------------------------------------------------------
# Input snapshot (main thread)
# ---------------------------------------------------------------------------

@dataclass
class RasterSource:
    """A prepared raster: worker-safe sampler (cloned provider) + transform."""
    name: str
    sampler: RasterSampler
    extent: QgsRectangle
    transform: Optional[QgsCoordinateTransform]  # route CRS -> raster CRS
    pixel_area_m2: float

    @property
    def cell_m(self) -> float:
        return self.sampler.cell_m

    @property
    def source_id(self) -> str:
        return self.sampler.source_id

    def sample(self, point_line_crs) -> Optional[float]:
        """Depth (m, positive down) at a route-CRS point, or None."""
        point = QgsPointXY(point_line_crs)
        if self.transform is not None:
            try:
                point = self.transform.transform(point)
            except QgsCsException:
                return None  # outside the raster CRS's valid area
        return self.sampler.sample(point)


@dataclass
class ContourSource:
    """A contour layer snapshot: thread-safe feature source + options."""
    name: str
    source: QgsVectorLayerFeatureSource
    crs: object
    depth_field: str
    options: Dict  # layer_options(); copied per pass ('auto' is inferred per pass)
    feature_count: int = 0


@dataclass
class ProfileParams:
    mode: str = RASTER
    interval_m: int = 50
    adaptive: bool = False
    adaptive_factor: float = 1.0
    max_samples: int = 50000
    auto_limit: bool = True
    per_raster: bool = True
    slope_window_m: float = 0.0
    invert_slope: bool = False
    side_slopes: bool = False
    side_search_m: float = 200.0


@dataclass
class ProfileRequest:
    route: RouteStationing
    params: ProfileParams
    transform_context: object
    rasters: List[RasterSource] = field(default_factory=list)
    side_rasters: List[RasterSource] = field(default_factory=list)
    contours: List[ContourSource] = field(default_factory=list)
    status: Optional[str] = None  # set when there is nothing to sample
    messages: List[Tuple[str, str, int]] = field(default_factory=list)


def snapshot_rasters(raster_layers, line_crs, transform_context) -> List[RasterSource]:
    """Main thread: rasters prepared for worker-side sampling, finest first.

    Native MBES sources are expanded and providers cloned. Raises
    ValueError/OSError when a mosaic's native source is missing.
    """
    sources = []
    for raster_layer in expand_rasters(raster_layers or []):
        if not raster_layer or not isinstance(raster_layer, QgsRasterLayer):
            continue
        if raster_layer.dataProvider() is None:
            continue
        raster_crs = raster_layer.crs()
        transform = None
        if raster_crs != line_crs:
            try:
                transform = QgsCoordinateTransform(line_crs, raster_crs, transform_context)
            except Exception:
                log_exception(f"Depth profile: no transform to raster '{raster_layer.name()}'; raster skipped")
                continue
        sampler = RasterSampler(raster_layer, clone=True)
        # PyQGIS leaves clone() C++-owned: without this the cloned provider,
        # and its open GDAL file handle, would outlive the profile (Windows
        # then keeps the raster file locked until QGIS exits).
        if not sip.ispyowned(sampler.provider):
            sip.transferback(sampler.provider)
        sources.append(RasterSource(raster_layer.name(), sampler, raster_layer.extent(), transform,
                                    sampler.cell_m ** 2))
    # Prefer higher resolution rasters first (smaller pixel area); stable.
    sources.sort(key=lambda s: s.pixel_area_m2)
    return sources


def snapshot_contours(layer_fields) -> List[ContourSource]:
    """Main thread: ``[(QgsVectorLayer, depth_field)]`` -> contour snapshots."""
    return [ContourSource(layer.name(), QgsVectorLayerFeatureSource(layer), layer.crs(), depth_field,
                          layer_options(layer), max(int(layer.featureCount()), 0))
            for layer, depth_field in layer_fields]


def build_request(route, params: ProfileParams, transform_context, raster_layers=(), contour_layers=()):
    """Main thread: snapshot everything the worker needs.

    ``raster_layers`` are the selected QgsRasterLayers; ``contour_layers``
    ``(layer, depth_field)`` pairs. A request with ``status`` set has
    nothing to sample (run_profile returns at once).
    """
    request = ProfileRequest(route, params, transform_context)
    if params.mode == RASTER:
        raster_layers = list(raster_layers or [])
        if not raster_layers:
            request.status = "Select one or more raster layers"
            return request
        line_crs = route.crs if route.crs is not None else raster_layers[0].crs()
        try:
            request.rasters = snapshot_rasters(raster_layers, line_crs, transform_context)
            if params.side_slopes:
                # Side slopes read through their own samplers, as before:
                # fresh cell caches and a per-pass 'auto' datum inference.
                request.side_rasters = snapshot_rasters(raster_layers, line_crs, transform_context)
        except (ValueError, OSError) as exc:
            request.rasters = request.side_rasters = []
            request.status = "Bathymetry source unavailable"
            request.messages.append((str(exc), CRITICAL, 10))
            return request
        if not request.rasters:
            request.status = "Select valid raster layer(s)"
    else:
        request.contours = snapshot_contours(contour_layers or [])
        if not request.contours:
            request.status = "Select valid contour layer(s) and depth field(s)"
    return request


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

Segment = namedtuple("Segment", "kp_from kp_to depth_from depth_to slope_deg slope_pct seabed_length "
                                "side_slope_deg side_slope_pct port_depth starboard_depth cross_span_m")


@dataclass
class ProfileResult:
    """One generated profile: per-station series aligned to ``kp_values``."""
    route: Optional[RouteStationing] = None
    params: Optional[ProfileParams] = None
    kp_values: List[float] = field(default_factory=list)            # km along the line
    depth_values: List[Optional[float]] = field(default_factory=list)  # m, positive down
    depth_source_ids: List[Optional[str]] = field(default_factory=list)  # raster per station
    depth_cell_m: List[Optional[float]] = field(default_factory=list)
    raster_series: List[Dict] = field(default_factory=list)          # [{'name', 'depths'}]
    slope_deg: List[Optional[float]] = field(default_factory=list)
    slope_pct: List[Optional[float]] = field(default_factory=list)
    slope_baseline_m: List[Optional[float]] = field(default_factory=list)
    side_slope_deg: List[Optional[float]] = field(default_factory=list)
    side_local_max_deg: List[Optional[float]] = field(default_factory=list)
    side_slope_pct: List[Optional[float]] = field(default_factory=list)
    side_port_depth: List[Optional[float]] = field(default_factory=list)
    side_starboard_depth: List[Optional[float]] = field(default_factory=list)
    side_cross_span_m: List[Optional[float]] = field(default_factory=list)
    seabed_length: float = 0.0
    seabed_covered_m: float = 0.0
    seabed_elongation_ratio: Optional[float] = None
    status: Optional[str] = None
    messages: List[Tuple[str, str, int]] = field(default_factory=list)
    # Which stages ran (the dock persists their settings only then).
    raster_sampled: bool = False
    side_slopes_ran: bool = False
    side_slopes_cancelled: bool = False

    def segments(self) -> List[Segment]:
        """Station-to-station segments for CSV export (slope at KP_to).

        Segments with a missing end, no chainage step or a change of
        supplying raster are omitted.
        """
        out: List[Segment] = []
        kp = self.kp_values
        if len(kp) < 2:
            return out
        x_m = [k * 1000.0 for k in kp]
        source_ids = self.depth_source_ids
        if not source_ids or len(source_ids) != len(kp):
            source_ids = None

        def side(values, i):
            return values[i] if values and len(values) > i else None

        for i in range(1, len(kp)):
            v1 = self.depth_values[i - 1]
            v2 = self.depth_values[i]
            horiz_m = x_m[i] - x_m[i - 1]
            if horiz_m <= 0 or v1 is None or v2 is None:
                continue
            if source_ids is not None and source_ids[i] != source_ids[i - 1]:
                continue
            # Chainage step, not the chord between stations, so segments sum
            # to the plotted seabed length even across route bends.
            out.append(Segment(kp[i - 1], kp[i], v1, v2, self.slope_deg[i], self.slope_pct[i],
                               math.hypot(horiz_m, v2 - v1),
                               side(self.side_slope_deg, i), side(self.side_slope_pct, i),
                               side(self.side_port_depth, i), side(self.side_starboard_depth, i),
                               side(self.side_cross_span_m, i)))
        return out


# ---------------------------------------------------------------------------
# Worker-side computation
# ---------------------------------------------------------------------------

class _Progress:
    """Maps a stage's 0..1 fraction onto its share of the overall progress."""

    def __init__(self, callback, start, span):
        self._callback = callback
        self._start = start
        self._span = span

    def __call__(self, fraction):
        if self._callback is not None:
            self._callback(self._start + self._span * min(max(fraction, 0.0), 1.0))


def _first_valid(point, sources):
    """(value, source) from the finest raster with valid data, else (None, None)."""
    for src in sources or []:
        val = src.sample(point)
        if val is not None:
            return val, src
    return None, None


def _route_overlaps(route, sources) -> bool:
    """Rough envelope test: does the route bbox touch any raster extent?"""
    xs = [pt.x() for part in route.line_parts for pt in part]
    ys = [pt.y() for part in route.line_parts for pt in part]
    if not (xs and ys):
        return True
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    corners = [QgsPointXY(minx, miny), QgsPointXY(minx, maxy), QgsPointXY(maxx, miny), QgsPointXY(maxx, maxy)]
    for src in sources:
        extent = src.extent
        if extent is None:
            continue
        tx = []
        for c in corners:
            try:
                tx.append(src.transform.transform(c) if src.transform is not None else c)
            except QgsCsException:
                pass
        if not tx:
            continue
        minx_t = min(p.x() for p in tx); maxx_t = max(p.x() for p in tx)
        miny_t = min(p.y() for p in tx); maxy_t = max(p.y() for p in tx)
        if not (maxx_t < extent.xMinimum() or minx_t > extent.xMaximum()
                or maxy_t < extent.yMinimum() or miny_t > extent.yMaximum()):
            return True
    return False


def sample_raster_profile(request: ProfileRequest, result: ProfileResult, cancel=None, progress=None) -> None:
    """Stations along the route sampled from the finest raster with data."""
    params = request.params
    route = request.route
    raster_sources = request.rasters
    line_length = route.line_length
    min_step_m = max(1, params.interval_m)
    adaptive = bool(params.adaptive)
    adaptive_factor = float(params.adaptive_factor)

    # Guard against excessive sample counts:
    # - fixed interval: based on interval
    # - adaptive: based on minimum step (best-case lower bound on spacing)
    expected_samples = int(line_length / min_step_m) + 1 if line_length > 0 else 0
    max_samples = params.max_samples
    if expected_samples > max_samples:
        if params.auto_limit:
            # Increase minimum step to cap samples to <= max_samples
            new_min_step = int(line_length / max_samples) + 1
            if new_min_step > min_step_m:
                result.messages.append((
                    f"Auto limit: minimum step raised {min_step_m}m -> {new_min_step}m "
                    f"(expected {expected_samples:,} > max {max_samples:,}).", WARNING, 7))
                min_step_m = new_min_step
        else:
            result.messages.append((
                f"Warning: high sample count ({expected_samples:,}) exceeds max preference "
                f"({max_samples:,}) but Auto Limit is off.", WARNING, 8))
    try:
        overlaps = _route_overlaps(route, raster_sources)
    except Exception:
        overlaps = True  # only an early exit; sampling decides coverage
        log_exception("Depth profile: raster extent pre-check failed", level=logging.DEBUG)
    if not overlaps:
        result.status = "Route outside raster extent"
        result.messages.append(("Selected route does not overlap any selected raster extent.", WARNING, 6))
        return

    kp_values, depth_values = result.kp_values, result.depth_values
    # Which raster supplied each station (source id or None): slope is never
    # evaluated across a change of source, so a vertical-datum offset between
    # two rasters cannot read as a slope spike.
    source_ids, cells = result.depth_source_ids, result.depth_cell_m
    valid_count = 0
    missing_count = 0
    # Per-raster series are only worth the extra sampling cost with 2+ rasters.
    per_raster = len(raster_sources) > 1 and bool(params.per_raster)
    per_raster_depths = [[] for _ in raster_sources] if per_raster else []

    def sample_station(point_xy):
        """Return (composite_value, source_used), recording per-raster values."""
        if not per_raster:
            return _first_valid(point_xy, raster_sources)
        best_val, best_src = None, None
        for i, src in enumerate(raster_sources):
            v = src.sample(point_xy)
            per_raster_depths[i].append(v)
            # raster_sources is ordered best-resolution-first, so the first
            # valid value matches the composite used elsewhere.
            if v is not None and best_val is None:
                best_val, best_src = v, src
        return best_val, best_src

    def record(dist_km, val, src_used):
        kp_values.append(dist_km)
        depth_values.append(val)
        source_ids.append(src_used.source_id if src_used is not None else None)
        cells.append(src_used.cell_m if src_used is not None else None)

    dist = 0.0
    while dist <= line_length:
        if cancel is not None and cancel():
            raise ProfileCancelled()
        if progress is not None:
            progress(dist / line_length if line_length > 0 else 1.0)
        pt = route.point_at(dist)
        if pt is None:
            break
        val, src_used = sample_station(QgsPointXY(pt.x(), pt.y()))
        if val is None:
            missing_count += 1
        else:
            valid_count += 1
        record(dist / 1000.0, val, src_used)
        # Step: fixed or adaptive based on raster resolution at this station.
        if adaptive:
            step = None
            if src_used is not None and src_used.pixel_area_m2 > 0:
                step = max(min_step_m, float(adaptive_factor) * float(math.sqrt(float(src_used.pixel_area_m2))))
            # Fallback when no raster coverage (or unknown resolution)
            if step is None:
                step = float(min_step_m)
            dist += max(1.0, step)
        else:
            dist += float(min_step_m)
    # Ensure last point exactly at end
    if kp_values and (kp_values[-1] * 1000.0) < line_length:
        pt = route.point_at(line_length)
        if pt is not None:
            val, src_used = sample_station(QgsPointXY(pt.x(), pt.y()))
            if val is None:
                missing_count += 1
            else:
                valid_count += 1
            record(line_length / 1000.0, val, src_used)
    if per_raster:
        result.raster_series = [{'name': src.name, 'depths': per_raster_depths[i]}
                                for i, src in enumerate(raster_sources)]
    result.raster_sampled = True
    # Coverage warnings
    if valid_count == 0 and kp_values:
        result.messages.append(("No raster coverage along selected route (all samples null).", WARNING, 6))
        result.status = "No raster coverage along route"
    elif valid_count > 0 and depth_values and missing_count > 0:
        ratio = valid_count / float(len(depth_values))
        result.messages.append((f"Partial raster coverage: {ratio*100:.1f}% of samples valid "
                                f"({missing_count:,} missing).", WARNING, 7))


def _intersection_points(inter) -> List[QgsPointXY]:
    """Vertices/points of a route x contour intersection geometry."""
    points = []
    if inter.isMultipart():
        if inter.type() == GEOMETRY_LINE:
            for part in inter.asMultiPolyline():
                points.extend(part)
        else:
            for g in inter.asGeometryCollection():
                if g.isEmpty():
                    continue
                if g.type() == GEOMETRY_LINE:
                    for part in g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]:
                        points.extend(part)
                elif g.type() == GEOMETRY_POINT:
                    points.extend(g.asMultiPoint() if g.isMultipart() else [g.asPoint()])
    else:
        if inter.type() == GEOMETRY_LINE:
            for part in inter.asMultiPolyline() if inter.isMultipart() else [inter.asPolyline()]:
                points.extend(part)
        elif inter.type() == GEOMETRY_POINT:
            points.extend(inter.asMultiPoint() if inter.isMultipart() else [inter.asPoint()])
    return points


def sample_contour_profile(request: ProfileRequest, result: ProfileResult, cancel=None, progress=None,
                           failures: Optional[_Failures] = None) -> None:
    """Stations at the exact route x contour crossings.

    The crossings define the linear profile; the plot already interpolates
    between them, and resampling would lose detail.
    """
    failures = failures or _Failures()
    route = request.route
    contours = request.contours
    kps = []
    depths = []
    route_geom = route.geometry()
    line_crs = route.crs if route.crs is not None else contours[0].crs
    route_engine = QgsGeometry.createGeometryEngine(route_geom.constGet())
    route_engine.prepareGeometry()
    total = float(sum(max(c.feature_count, 1) for c in contours))
    done = 0
    for contour in contours:
        options = dict(contour.options)
        depth_field = contour.depth_field
        # Only contours near the route: large contour layers were
        # intersected feature-by-feature across their whole extent.
        feature_request = QgsFeatureRequest()
        rect = route.filter_rect(contour.crs, request.transform_context)
        if rect is not None:
            feature_request.setFilterRect(rect)
        transform = None
        if contour.crs != line_crs:
            transform = QgsCoordinateTransform(contour.crs, line_crs, request.transform_context)
        for feat in contour.source.getFeatures(feature_request):
            if cancel is not None and cancel():
                raise ProfileCancelled()
            done += 1
            if progress is not None and done % 50 == 0:
                progress(done / total)
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            if transform is not None:
                try:
                    geom = QgsGeometry(geom)
                    geom.transform(transform)
                except QgsCsException:
                    failures.note(f"contour in '{contour.name}' not transformable to the route CRS; skipped")
                    continue
            if not route_engine.intersects(geom.constGet()):
                continue
            inter = route_geom.intersection(geom)
            if inter.isEmpty():
                continue
            try:
                depth_val = normalise_depth(feat[depth_field], options)
            except (KeyError, TypeError, ValueError):
                failures.note(f"contour depth '{depth_field}' unreadable in '{contour.name}'; skipped")
                continue
            if depth_val is None:
                continue
            for p in _intersection_points(inter):
                kp_m = route.measure_along(p)
                if kp_m is None:
                    continue
                kps.append(kp_m / 1000.0)
                depths.append(depth_val)
    if not kps:
        result.status = "No contour intersections"
        return
    pairs = clean_crossings(zip(kps, depths), tolerance=1e-9)
    result.kp_values = [p[0] for p in pairs]
    result.depth_values = [p[1] for p in pairs]
    result.depth_source_ids = []
    result.depth_cell_m = []


def _side_contour_index(request: ProfileRequest, search_m: float, failures: _Failures):
    """Spatial index + ``{id: (geometry, depth)}`` over the contour layers.

    Geometries are in the route CRS; only contours within reach of the
    transects (2 x search either side) are read.
    """
    contours = request.contours
    if not contours:
        return None, None
    route = request.route
    line_crs = route.crs if route.crs is not None else contours[0].crs
    index = QgsSpatialIndex()
    data = {}
    next_id = 1
    for contour in contours:
        options = dict(contour.options)
        depth_field = contour.depth_field
        transform = None
        if contour.crs != line_crs:
            transform = QgsCoordinateTransform(contour.crs, line_crs, request.transform_context)
        # Transects reach 2 x search width either side of the route.
        feature_request = QgsFeatureRequest()
        rect = route.filter_rect(contour.crs, request.transform_context, 2.5 * float(search_m))
        if rect is not None:
            feature_request.setFilterRect(rect)
        for feat in contour.source.getFeatures(feature_request):
            try:
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                if transform is not None:
                    geom = QgsGeometry(geom)
                    geom.transform(transform)
                depth_val = feat[depth_field]
                if depth_val is None:
                    continue
                depth_f = normalise_depth(depth_val, options)
                if depth_f is None:
                    continue
            except (QgsCsException, KeyError, TypeError, ValueError):
                failures.note(f"side-slope contour in '{contour.name}' unusable; skipped")
                continue
            f = QgsFeature()
            f.setId(next_id)
            f.setGeometry(geom)
            index.addFeature(f)
            data[next_id] = (geom, depth_f)
            next_id += 1
    if not data:
        return None, None
    return index, data


def _contour_intersections(transect, center, nx, ny, contour_index, contour_data, is_geo, distance_area,
                           failures: _Failures):
    """Contour crossings along a transect: ``[(t, depth, x, y)]``.

    ``t`` is the signed distance along the normal (+ = starboard, - = port).
    """
    if transect is None or transect.isEmpty() or contour_index is None or contour_data is None:
        return []
    candidate_ids = contour_index.intersects(transect.boundingBox())
    if not candidate_ids:
        return []
    cx = center.x(); cy = center.y()
    out = []
    for cid in candidate_ids:
        item = contour_data.get(cid)
        if not item:
            continue
        geom, depth = item
        try:
            inter = transect.intersection(geom)
        except Exception:
            failures.note("cross transect x contour intersection failed; contour ignored")
            continue
        if inter is None or inter.isEmpty():
            continue
        points = []
        try:
            if inter.type() == GEOMETRY_POINT:
                points = inter.asMultiPoint() if inter.isMultipart() else [inter.asPoint()]
            elif inter.type() == GEOMETRY_LINE:
                # Overlap: use vertices (rare). This can still help build a fit.
                if inter.isMultipart():
                    for part in inter.asMultiPolyline():
                        points.extend(part)
                else:
                    points.extend(inter.asPolyline())
        except Exception:
            failures.note("cross transect x contour intersection unreadable; contour ignored")
            points = []
        for p in points:
            if is_geo:
                # Signed cross distance in metres (geodesic), sign from the
                # dot product in coordinate space (good enough for sign).
                sign_v = (p.x() - cx) * nx + (p.y() - cy) * ny
                sign = 1.0 if sign_v > 0 else (-1.0 if sign_v < 0 else 0.0)
                if sign == 0.0:
                    t = 0.0
                else:
                    try:
                        dist_m = float(distance_area.measureLine(QgsPointXY(cx, cy), QgsPointXY(p.x(), p.y())))
                    except Exception:
                        failures.note("cross distance to a contour crossing failed; used 0 m")
                        dist_m = 0.0
                    t = sign * dist_m
            else:
                vx = p.x() - cx
                vy = p.y() - cy
                t = float(vx * nx + vy * ny) / (nx * nx + ny * ny)
            out.append((t, float(depth), float(p.x()), float(p.y())))
    return out


def compute_side_slopes(request: ProfileRequest, result: ProfileResult, cancel=None, progress=None,
                        failures: Optional[_Failures] = None) -> None:
    """Cross tilt (+ve = deeper to starboard) and local maximum per station.

    - Raster: samples the transect at native resolution; endpoint tilt needs
      complete, single-source coverage through the centre.
    - Contours: all contour crossings on a transect twice the search width.
    A cancel stops early and keeps the stations done so far.
    """
    failures = failures or _Failures()
    kp_values = result.kp_values
    if not kp_values:
        return
    params = request.params
    route = request.route
    distance_area = route.distance_area
    search_m = float(params.side_search_m)
    if search_m <= 0:
        return
    n = len(kp_values)
    result.side_slope_deg = [None] * n
    result.side_local_max_deg = [None] * n
    result.side_slope_pct = [None] * n
    result.side_port_depth = [None] * n
    result.side_starboard_depth = [None] * n
    result.side_cross_span_m = [None] * n

    mode = params.mode
    contour_index = contour_data = None
    if mode == CONTOURS:
        contour_index, contour_data = _side_contour_index(request, search_m, failures)
        if contour_index is None or contour_data is None:
            result.messages.append(("Side slope: no contour data available.", WARNING, 5))
            return
    raster_sources = None
    offsets = None
    if mode == RASTER:
        raster_sources = request.side_rasters
        if not raster_sources:
            result.messages.append(("Side slope: select valid raster layer(s).", WARNING, 5))
            return
        cell = max((src.cell_m for src in raster_sources), default=1)
        # Resolve local features at native resolution, bounded per station.
        count = max(11, min(2001, int(math.ceil(2 * search_m / max(cell, .1))) + 1))
        offsets = list(np.linspace(-search_m, search_m, count))

    # Tangent sampling distance (metres along route) tied to station spacing.
    tangent_delta_m = 10.0
    if len(kp_values) >= 3:
        diffs = np.diff(np.asarray(kp_values, dtype=float)) * 1000.0
        diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
        if diffs.size:
            spacing_m = float(np.median(diffs))
            tangent_delta_m = max(5.0, min(50.0, spacing_m / 2.0))

    # Geographic routes offset geodesically (metres), not planar degrees.
    is_geo = bool(route.crs is not None and route.crs.isGeographic())
    unit_m = None if is_geo else metres_per_unit(route.crs)

    for i, kp in enumerate(kp_values):
        if cancel is not None and cancel():
            result.messages.append(("Side slope canceled.", WARNING, 4))
            result.side_slopes_cancelled = True
            break
        if progress is not None and i % 25 == 0:
            progress(i / n)
        try:
            dist_m = float(kp) * 1000.0
            center = route.point_at(dist_m)
            if center is None:
                continue
            # Local tangent from points ahead/behind.
            p0 = route.point_at(max(0.0, dist_m - tangent_delta_m))
            p1 = route.point_at(min(route.line_length, dist_m + tangent_delta_m))
            if p0 is None or p1 is None:
                continue
            normal_bearing = None
            if is_geo:
                bearing = float(distance_area.bearing(QgsPointXY(p0.x(), p0.y()), QgsPointXY(p1.x(), p1.y())))
                # Starboard is +90 degrees from the forward bearing; a unit
                # normal in coordinate space gives the contour-offset sign.
                normal_bearing = bearing + (math.pi / 2.0)
                nx = math.sin(normal_bearing)
                ny = math.cos(normal_bearing)
            else:
                dx = p1.x() - p0.x(); dy = p1.y() - p0.y()
                mag = math.hypot(dx, dy)
                if mag <= 0:
                    continue
                ux = dx / mag; uy = dy / mag
                # Starboard (right) normal: rotate clockwise
                nx = uy / unit_m
                ny = -ux / unit_m

            if mode == RASTER:
                z_vals, cells, sources = [], [], []
                for t in offsets:
                    if is_geo:
                        pt = distance_area.computeSpheroidProject(
                            QgsPointXY(center), abs(float(t)),
                            normal_bearing if t >= 0 else normal_bearing + math.pi)
                    else:
                        pt = QgsPointXY(center.x() + nx * t, center.y() + ny * t)
                    z, src = _first_valid(pt, raster_sources)
                    z_vals.append(z)
                    cells.append(src.cell_m if src is not None else None)
                    sources.append(src.source_id if src is not None else None)
                station_offsets = offsets
            else:
                if is_geo:
                    port_pt = distance_area.computeSpheroidProject(QgsPointXY(center), 2 * search_m,
                                                                   normal_bearing + math.pi)
                    stbd_pt = distance_area.computeSpheroidProject(QgsPointXY(center), 2 * search_m,
                                                                   normal_bearing)
                else:
                    port_pt = QgsPointXY(center.x() - nx * 2 * search_m, center.y() - ny * 2 * search_m)
                    stbd_pt = QgsPointXY(center.x() + nx * 2 * search_m, center.y() + ny * 2 * search_m)
                transect = QgsGeometry.fromPolylineXY([port_pt, stbd_pt])
                hits = _contour_intersections(transect, center, nx, ny, contour_index, contour_data,
                                              is_geo, distance_area, failures)
                pairs = clean_crossings((t, z) for t, z, _x, _y in hits)
                station_offsets = [t for t, z in pairs]
                z_vals = [z for t, z in pairs]
                cells = sources = None
            tilt, peak, port_z, stbd_z = cross_profile_metrics(
                station_offsets, z_vals, search_m, True, cells, sources)
            result.side_slope_deg[i] = tilt
            result.side_local_max_deg[i] = peak
            result.side_slope_pct[i] = None if tilt is None else 100 * math.tan(math.radians(tilt))
            result.side_port_depth[i] = port_z
            result.side_starboard_depth[i] = stbd_z
            result.side_cross_span_m[i] = 2 * search_m if tilt is not None else None
        except Exception:
            failures.note("side slope failed at a station; left blank")
            continue
    result.side_slopes_ran = True


def compute_slopes(result: ProfileResult, window_m: float, invert: bool) -> None:
    """Station slope from the shared engine (+ve = shoaling with KP).

    Evaluated per contiguous run of valid stations (no-data gaps are never
    bridged); runs also break where the supplying raster changes, so a
    vertical-datum offset between two rasters shows as a slope break, never
    a spike. ``window_m`` 0 selects native-resolution automatic baselines;
    positive lengths require the full supported physical window.
    """
    result.slope_deg = []
    result.slope_pct = []
    result.slope_baseline_m = []
    if len(result.kp_values) < 2:
        return
    x_m = [kp * 1000.0 for kp in result.kp_values]
    source_ids = result.depth_source_ids
    if not source_ids or len(source_ids) != len(result.kp_values):
        source_ids = None
    station_slope, result.slope_baseline_m = supported_slopes(
        x_m, result.depth_values, result.depth_cell_m, source_ids, float(window_m), positive_down=True)
    for raw in station_slope:
        deg = None if raw is None else (-raw if invert else raw)
        result.slope_deg.append(deg)
        result.slope_pct.append(None if deg is None else 100.0 * math.tan(math.radians(deg)))


def compute_seabed_length(result: ProfileResult) -> None:
    """Seabed (3D) length from sampled depths along route chainage.

    Sums hypot(chainage step, depth step) within contiguous valid runs
    only, so no-data gaps and raster seams are never bridged. Also sets the
    plan length of those runs and the elongation ratio.
    """
    result.seabed_length = 0.0
    result.seabed_covered_m = 0.0
    result.seabed_elongation_ratio = None
    if not result.kp_values or not result.depth_values:
        return
    x_m = [kp * 1000 for kp in result.kp_values]
    depths = result.depth_values
    for a, b in contiguous_runs(x_m, depths, group_ids=result.depth_source_ids or None):
        for i in range(a + 1, b + 1):
            horizontal = x_m[i] - x_m[i - 1]
            result.seabed_covered_m += horizontal
            result.seabed_length += math.hypot(horizontal, depths[i] - depths[i - 1])
    if result.seabed_covered_m > 0:
        result.seabed_elongation_ratio = result.seabed_length / result.seabed_covered_m


def run_profile(request: ProfileRequest, cancel: Optional[Callable[[], bool]] = None,
                progress: Optional[Callable[[float], None]] = None) -> ProfileResult:
    """Compute a whole profile from a snapshot (worker safe).

    ``cancel()`` is polled per station/feature: during the along-route pass
    it raises :class:`ProfileCancelled`; during side slopes it stops early
    and keeps the stations done. ``progress(fraction)`` reports 0..1.
    """
    params = request.params
    result = ProfileResult(route=request.route, params=params, status=request.status,
                           messages=list(request.messages))
    failures = _Failures()
    side = bool(params.side_slopes)
    share = 0.5 if side else 1.0
    if request.status is None:
        if params.mode == RASTER:
            sample_raster_profile(request, result, cancel, _Progress(progress, 0.0, share))
        else:
            sample_contour_profile(request, result, cancel, _Progress(progress, 0.0, share), failures)
    if side:
        # Computed first so the segment table can carry the side columns.
        try:
            compute_side_slopes(request, result, cancel, _Progress(progress, share, 1.0 - share), failures)
        except Exception as exc:
            log_exception("Depth profile: side slope failed")
            result.messages.append((f"Side slope failed: {exc}", WARNING, 6))
    compute_slopes(result, params.slope_window_m, params.invert_slope)
    compute_seabed_length(result)
    failures.report(result.messages)
    if progress is not None:
        progress(1.0)
    return result


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


class DepthProfileTask(QgsTask):
    """Runs :func:`run_profile` off the GUI thread.

    On completion ``result`` holds the profile, or ``cancelled`` / ``error``
    say why not. ``finished()`` runs on the main thread and calls
    ``on_finished(task)``, which applies the result and releases the
    snapshot (``task.request = None``) there, not in the worker.
    """

    def __init__(self, request: ProfileRequest, on_finished: Callable[["DepthProfileTask"], None],
                 description: str = "Depth profile"):
        super().__init__(description, _task_flag("CanCancel"))
        self.request = request
        self.result: Optional[ProfileResult] = None
        self.error: Optional[str] = None
        self.cancelled = False
        self._on_finished = on_finished
        self._last_pct = -1.0

    def _progress(self, fraction: float) -> None:
        pct = 100.0 * fraction
        # Throttled: each setProgress is a queued signal to the GUI thread.
        if pct - self._last_pct >= 0.5 or (pct >= 100.0 > self._last_pct):
            self._last_pct = pct
            self.setProgress(pct)

    def run(self) -> bool:
        try:
            self.result = run_profile(self.request, cancel=self.isCanceled, progress=self._progress)
            return True
        except ProfileCancelled:
            self.cancelled = True
            return False
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            log_exception("Depth profile: generation failed", level=logging.ERROR)
            return False

    def finished(self, ok: bool) -> None:
        if not ok and not self.cancelled and self.error is None:
            # Cancelled before it started, or terminated by the manager.
            self.cancelled = self.isCanceled()
            if not self.cancelled:
                self.error = "Depth profile task failed."
        try:
            self._on_finished(self)
        except Exception:  # never crash QGIS from a completion callback
            log_exception("Depth profile: applying the result failed", level=logging.ERROR)
