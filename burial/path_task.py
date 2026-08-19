# -*- coding: utf-8 -*-
"""Cancellable QGIS adapter for Installation Paths.

The path solver itself lives in :mod:`path_geometry` and works in metres.
This module unwraps the scoped WGS84 RPL into a route-relative engineering
plane which preserves every leg's ellipsoidal length and bearing.  Generated
points are then placed back on the spheroid relative to their nearest RPL
leg.  This avoids the scale and convergence error of choosing one projected
CRS for a potentially very long subsea route.

Only cloned geometries, a dedicated ``QgsDistanceArea`` and cloned depth
providers enter the worker.  Project, layer and registry writes remain on the
main thread in the completion callback.
"""

from __future__ import annotations

import bisect
import json
import math
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsTask,
)
from qgis.PyQt.QtCore import pyqtSignal

from ..kp_geo_utils import RouteFrame, iter_line_parts
from . import path_data, path_geometry, schema
from .analysis_task import DepthSnapshot, _log_callback_failure

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


_CAN_CANCEL = _task_flag("CanCancel")
Point = Tuple[float, float]


@dataclass
class PathWork:
    """Thread-safe snapshot consumed by :class:`InstallationPathTask`."""

    plan_id: str
    geometries: List[QgsGeometry]
    distance: QgsDistanceArea
    scope_start_kp: float
    scope_end_kp: float
    direction: int
    radius_m: float
    mode: str
    max_deviation_m: float
    chord_tolerance_m: float
    tool_display: str
    tool_type: str
    fingerprints: Dict[str, str]
    config: Dict
    layback_profile: Optional[Dict] = None
    depth: Optional[DepthSnapshot] = None
    # Current persisted profile as (KP, depth/None).  It is much faster than
    # re-reading a raster for every densely sampled arc vertex.
    depth_samples: Optional[List[Tuple[float, Optional[float]]]] = None
    # Water-depth-banded minimum turning radius, sanitised and ordered by
    # path_data.sanitise_radius_rules.  Empty = constant tool radius.
    radius_rules: List[Dict] = field(default_factory=list)
    # Manual path adjustments [{"kp": ..., "dcc_m": ...}] (sanitised);
    # each becomes a mandatory off-route control for the solver.
    adjustments: List[Dict] = field(default_factory=list)


@dataclass
class PathTaskResult:
    tool_wgs84: List[Point] = field(default_factory=list)
    barge_wgs84: List[Point] = field(default_factory=list)
    diagnostics: List[Dict] = field(default_factory=list)
    summary: Dict = field(default_factory=dict)

    def registry_row(self, work: PathWork) -> Dict:
        return {
            "path_id": schema.new_id(),
            "plan_id": work.plan_id,
            "generated_utc": schema.utc_now_iso(),
            "algorithm_version": path_data.ALGORITHM_VERSION,
            "config_json": json.dumps(work.config, sort_keys=True),
            "fingerprints_json": json.dumps(work.fingerprints, sort_keys=True),
            "summary_json": json.dumps(self.summary, sort_keys=True),
            "tool_path_wkt": path_data.linestring_wkt(self.tool_wgs84),
            "barge_track_wkt": path_data.linestring_wkt(self.barge_wgs84),
            "diagnostics_json": json.dumps(self.diagnostics, sort_keys=True),
        }


def build_path_work(route: RouteFrame, distance: QgsDistanceArea, plan: Dict,
                    radius_m: float, mode: str, max_deviation_m: float,
                    tool_display: str, tool_type: str,
                    fingerprints: Dict[str, str], config: Dict,
                    layback_profile: Optional[Dict] = None,
                    depth: Optional[DepthSnapshot] = None,
                    depth_samples: Optional[Sequence[Tuple[float, Optional[float]]]] = None,
                    chord_tolerance_m: float = 0.25,
                    radius_rules: Optional[Sequence[Dict]] = None) -> PathWork:
    """Clone main-thread plan state into a worker-owned snapshot."""
    geoms = [QgsGeometry(geom) for geom in route.geometries]
    # QgsDistanceArea has a copy constructor in supported QGIS versions.  A
    # dedicated instance ensures its lazy geodesic state is never shared with
    # canvas/main-thread queries while this task is running.
    try:
        worker_distance = QgsDistanceArea(distance)
    except Exception:
        worker_distance = distance
    return PathWork(
        plan_id=str(plan.get("plan_id") or ""),
        geometries=geoms,
        distance=worker_distance,
        scope_start_kp=float(plan.get("scope_start_kp") or 0.0),
        scope_end_kp=float(plan.get("scope_end_kp") or 0.0),
        direction=1 if int(plan.get("direction") or 1) >= 0 else -1,
        radius_m=float(radius_m), mode=str(mode or path_data.MODE_FILLET),
        max_deviation_m=max(0.0, float(max_deviation_m or 0.0)),
        chord_tolerance_m=max(0.05, float(chord_tolerance_m or 0.25)),
        tool_display=str(tool_display or ""), tool_type=str(tool_type or ""),
        fingerprints=dict(fingerprints or {}), config=dict(config or {}),
        layback_profile=(dict(layback_profile) if layback_profile else None),
        depth=depth,
        depth_samples=(list(depth_samples) if depth_samples is not None else None),
        radius_rules=path_data.sanitise_radius_rules(radius_rules or []),
        adjustments=path_data.sanitise_adjustments(
            (config or {}).get("adjustments")),
    )


def _clean_scoped_vertices(geometry: QgsGeometry) -> List[QgsPointXY]:
    points: List[QgsPointXY] = []
    for part in iter_line_parts(geometry):
        for raw in part:
            point = QgsPointXY(raw)
            if not points or abs(point.x() - points[-1].x()) > 1e-13 \
                    or abs(point.y() - points[-1].y()) > 1e-13:
                points.append(point)
    return points


class _RoutePlane:
    """Bidirectional bridge between the unrolled route and WGS84."""

    def __init__(self, wgs_points: Sequence[QgsPointXY],
                 kp_values: Sequence[float], distance: QgsDistanceArea,
                 source_route: RouteFrame, radius_m: float):
        if len(wgs_points) < 2 or len(wgs_points) != len(kp_values):
            raise path_geometry.PathGeometryError(
                "The scoped route needs at least two distinct points.")
        self.wgs = [QgsPointXY(point) for point in wgs_points]
        self.kps = [float(value) for value in kp_values]
        self.distance = distance
        self.source_route = source_route
        self.points: List[Point] = [(0.0, 0.0)]
        self.bearings: List[float] = []
        self.lengths: List[float] = []
        for first, second in zip(self.wgs, self.wgs[1:]):
            length = float(distance.measureLine(first, second))
            if not math.isfinite(length) or length <= 1e-6:
                continue
            bearing = float(distance.bearing(first, second))
            x, y = self.points[-1]
            self.points.append((x + length * math.sin(bearing),
                                y + length * math.cos(bearing)))
            self.bearings.append(bearing)
            self.lengths.append(length)
        if len(self.points) != len(self.wgs):
            # Input was cleaned already; reaching this branch means the
            # distance engine rejected a segment, so station correspondence
            # would be unsafe.
            raise path_geometry.PathGeometryError(
                "A scoped route leg has zero or invalid ellipsoidal length.")

        # Lightweight sampled grid.  It avoids O(route vertices x path
        # vertices) nearest-segment searches on dense RPLs.  A generous cell
        # keeps long-leg indexing bounded and includes turn-out loops.
        self.cell_m = max(1000.0, 6.0 * max(float(radius_m), 1.0))
        self.grid: Dict[Tuple[int, int], set] = {}
        for index, (a, b) in enumerate(zip(self.points, self.points[1:])):
            length = path_geometry.distance(a, b)
            count = max(1, int(math.ceil(length / (0.75 * self.cell_m))))
            for step in range(count + 1):
                t = step / count
                x = a[0] + t * (b[0] - a[0])
                y = a[1] + t * (b[1] - a[1])
                key = (int(math.floor(x / self.cell_m)),
                       int(math.floor(y / self.cell_m)))
                self.grid.setdefault(key, set()).add(index)

    def _candidate_segments(self, point: Point) -> Sequence[int]:
        cx = int(math.floor(point[0] / self.cell_m))
        cy = int(math.floor(point[1] / self.cell_m))
        candidates = set()
        for ring in range(0, 5):
            for ix in range(cx - ring, cx + ring + 1):
                for iy in range(cy - ring, cy + ring + 1):
                    if ring and abs(ix - cx) < ring and abs(iy - cy) < ring:
                        continue
                    candidates.update(self.grid.get((ix, iy), ()))
            # Include neighbouring cells even when the point's own cell has
            # a sampled segment. Otherwise two close/parallel RPL legs can
            # make the first arbitrary candidate win over the true nearest.
            if candidates and ring >= 2:
                return tuple(sorted(candidates))
        return range(len(self.points) - 1)

    def locate(self, point: Point) -> Tuple[int, float, float, float, float]:
        """Return segment, fraction, local residual east/north and route KP."""
        best = None
        for index in self._candidate_segments(point):
            a, b = self.points[index], self.points[index + 1]
            dx, dy = b[0] - a[0], b[1] - a[1]
            denom = dx * dx + dy * dy
            if denom <= 1e-12:
                continue
            t = ((point[0] - a[0]) * dx
                 + (point[1] - a[1]) * dy) / denom
            t = min(1.0, max(0.0, t))
            qx, qy = a[0] + t * dx, a[1] + t * dy
            square = (point[0] - qx) ** 2 + (point[1] - qy) ** 2
            if best is None or square < best[0]:
                residual_x, residual_y = point[0] - qx, point[1] - qy
                kp = self.kps[index] + t * (self.kps[index + 1]
                                             - self.kps[index])
                best = (square, index, t, residual_x, residual_y, kp)
        if best is None:
            raise path_geometry.PathGeometryError(
                "A generated point could not be located against the route.")
        return best[1], best[2], best[3], best[4], best[5]

    def _place(self, kp: float, residual_x: float, residual_y: float
               ) -> Point:
        base = self.source_route.point_at_kp(kp, clamp=True)
        if base is None:
            raise path_geometry.PathGeometryError(
                "A generated path station could not be placed on the RPL.")
        offset = math.hypot(residual_x, residual_y)
        if offset <= 1e-7:
            return (float(base.x()), float(base.y()))
        # The unrolled plane's +X/+Y axes are east/north because every leg
        # was accumulated from its true bearing. Retaining both residual
        # components also handles a compound path whose nearest location is
        # a clamped route endpoint (the displacement need not be perfectly
        # perpendicular to either adjoining leg).
        bearing = math.atan2(residual_x, residual_y)
        placed = self.distance.computeSpheroidProject(
            base, offset, bearing)
        return (float(placed.x()), float(placed.y()))

    def to_wgs84(self, point: Point) -> Tuple[Point, float]:
        _index, _fraction, residual_x, residual_y, kp = self.locate(point)
        return self._place(kp, residual_x, residual_y), kp

    def to_wgs84_full(self, point: Point) -> Tuple[Point, float, float]:
        """((lon, lat), route KP, signed cross-course offset in metres).

        Positive DCC lies to port of the direction of travel (the plane's
        points are already in travel order).
        """
        index, _fraction, residual_x, residual_y, kp = self.locate(point)
        a, b = self.points[index], self.points[index + 1]
        cross = (b[0] - a[0]) * residual_y - (b[1] - a[1]) * residual_x
        offset = math.hypot(residual_x, residual_y)
        dcc = offset if cross > 0.0 else (-offset if cross < 0.0 else 0.0)
        return self._place(kp, residual_x, residual_y), kp, dcc

    def station_control_for_kp(self, kp: float, dcc_m: float
                               ) -> Tuple[float, Point]:
        """(plane station, plane point) for a KP + signed cross-course
        offset — the solver-frame form of one manual path adjustment."""
        kps = self.kps
        if not hasattr(self, "_chainages"):
            self._chainages = path_geometry.cumulative_lengths(self.points)
        chainages = self._chainages
        ascending = kps[-1] >= kps[0]
        lo, hi = (kps[0], kps[-1]) if ascending else (kps[-1], kps[0])
        value = min(max(float(kp), lo), hi)
        segment = None
        for index in range(len(kps) - 1):
            a, b = kps[index], kps[index + 1]
            if (a - 1e-12 <= value <= b + 1e-12) if ascending \
                    else (b - 1e-12 <= value <= a + 1e-12):
                segment = index
                break
        if segment is None:
            segment = len(kps) - 2
        span = kps[segment + 1] - kps[segment]
        t = 0.0 if abs(span) <= 1e-12 else (value - kps[segment]) / span
        t = min(max(t, 0.0), 1.0)
        a, b = self.points[segment], self.points[segment + 1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            raise path_geometry.PathGeometryError(
                f"No usable route leg at KP {value:.3f} for a path "
                "adjustment.")
        base = (a[0] + t * dx, a[1] + t * dy)
        station = chainages[segment] + t * length
        # Port of travel = left of the leg direction.
        nx, ny = -dy / length, dx / length
        return station, (base[0] + float(dcc_m) * nx,
                         base[1] + float(dcc_m) * ny)


def _downsample_dcc(kps: Sequence[float], values: Sequence[float],
                    limit: int = 2400) -> Dict[str, List[float]]:
    """Bounded KP-vs-DCC series keeping each bucket's worst excursion.

    The persisted summary must stay a review artefact, not a bulk dataset:
    buckets keep the sample with the largest |DCC| so peaks are never
    smoothed away, and the endpoints always survive.
    """
    count = len(kps)
    if count == 0 or count != len(values):
        return {"kp": [], "m": []}
    keep: List[int] = []
    if count <= limit:
        keep = list(range(count))
    else:
        keep.append(0)
        buckets = max(limit - 2, 1)
        for bucket in range(buckets):
            lo = 1 + bucket * (count - 2) // buckets
            hi = 1 + (bucket + 1) * (count - 2) // buckets
            if hi <= lo:
                continue
            keep.append(max(range(lo, hi), key=lambda i: abs(values[i])))
        keep.append(count - 1)
        keep = sorted(set(keep))
    return {"kp": [round(float(kps[i]), 5) for i in keep],
            "m": [round(float(values[i]), 2) for i in keep]}


def _interpolated_depth(samples: Sequence[Tuple[float, Optional[float]]],
                        kp: float) -> Optional[float]:
    valid = [(float(x), None if y is None else abs(float(y)))
             for x, y in samples]
    if not valid:
        return None
    kps = [value[0] for value in valid]
    index = bisect.bisect_left(kps, float(kp))
    if index < len(valid) and abs(kps[index] - kp) <= 1e-9:
        return valid[index][1]
    if index <= 0 or index >= len(valid):
        return None
    x0, y0 = valid[index - 1]
    x1, y1 = valid[index]
    if y0 is None or y1 is None or x1 <= x0:
        return None
    return y0 + (float(kp) - x0) * (y1 - y0) / (x1 - x0)


class InstallationPathTask(QgsTask):
    """Generate the radius-constrained tool path and optional barge track."""

    progressMessage = pyqtSignal(str)

    def __init__(self, work: PathWork,
                 on_finished: Callable[["InstallationPathTask"], None],
                 description: str = "Generate installation paths"):
        super().__init__(description, _CAN_CANCEL)
        self.work = work
        self.result: Optional[PathTaskResult] = None
        self.error: Optional[str] = None
        self.cancelled = False
        self._on_finished = on_finished

    def _check_cancel(self) -> None:
        if self.isCanceled():
            self.cancelled = True
            raise InterruptedError()

    def _breathing_cancel(self) -> Callable[[], bool]:
        """Cancel probe that periodically yields the GIL.

        The solver is pure Python: without an explicit sleep it holds the
        GIL almost continuously, starving the main thread's Python (map
        tools, hover overlays, other plugins) and making the whole QGIS UI
        feel frozen while a path generates. Sleeping ~1 ms every 40 ms
        costs under 3% wall time and keeps the UI fluid.
        """
        state = {"next": 0.0}

        def probe() -> bool:
            now = time.monotonic()
            if now >= state["next"]:
                state["next"] = now + 0.04
                time.sleep(0.001)
            return self.isCanceled()

        return probe

    def run(self) -> bool:  # noqa: C901 - deliberately linear task pipeline
        try:
            work = self.work
            self.progressMessage.emit("Extracting every scoped route course change…")
            source_route = RouteFrame.from_source(
                work.geometries, work.distance, follow_stored_geometry=True)
            low = max(0.0, min(work.scope_start_kp, work.scope_end_kp))
            high = min(source_route.total_length_km,
                       max(work.scope_start_kp, work.scope_end_kp))
            if high <= low:
                raise path_geometry.PathGeometryError(
                    "The plan scope has no usable route length.")
            scoped = source_route.extract_segment(low, high)
            if scoped is None or scoped.isEmpty():
                raise path_geometry.PathGeometryError(
                    "The plan scope could not be extracted from the route.")
            wgs = _clean_scoped_vertices(scoped)
            if len(wgs) < 2:
                raise path_geometry.PathGeometryError(
                    "The scoped route needs at least two distinct points.")
            kps = [low]
            running = low * 1000.0
            for first, second in zip(wgs, wgs[1:]):
                running += float(work.distance.measureLine(first, second))
                kps.append(min(high, running / 1000.0))
            kps[-1] = high
            if work.direction < 0:
                wgs.reverse()
                kps.reverse()
            rules = list(work.radius_rules or [])
            max_rule_radius = max(
                [work.radius_m] + [float(r["radius_m"]) for r in rules])
            plane = _RoutePlane(wgs, kps, work.distance, source_route,
                                max_rule_radius)
            self._check_cancel()
            self.setProgress(8.0)

            # One lazy snapshot preparation shared by depth-based radius
            # selection, the depth-difference diagnostic and the layback.
            depth_state = {"prepared": False}

            def _prepare_snapshot() -> None:
                if depth_state["prepared"]:
                    return
                if not work.depth.prepare(cancel=self.isCanceled):
                    raise InterruptedError()
                depth_state["prepared"] = True

            snapshot_ok = work.depth is not None \
                and work.depth.is_available()

            def _water_depth(kp: float, lon: float, lat: float
                             ) -> Optional[float]:
                """Positive water depth from the profile, else the snapshot."""
                if work.depth_samples is not None:
                    value = _interpolated_depth(work.depth_samples, kp)
                    if value is not None:
                        return value
                if snapshot_ok:
                    _prepare_snapshot()
                    value = work.depth.sample(lat, lon)
                    if value is not None:
                        return abs(float(value))
                return None

            radius_for_vertex = None
            if rules:
                def radius_for_vertex(index: int, _station: float) -> float:
                    kp = kps[index]
                    depth_value = _water_depth(
                        kp, float(wgs[index].x()), float(wgs[index].y()))
                    if depth_value is None:
                        raise path_geometry.PathGeometryError(
                            f"No water depth is available at KP {kp:.3f} to "
                            "select a depth-based turning radius.")
                    band = path_data.radius_for_depth(rules, depth_value)
                    if band is None:
                        raise path_geometry.PathGeometryError(
                            f"Water depth {depth_value:.1f} m at KP {kp:.3f} "
                            "is deeper than every configured turning-radius "
                            "band. Add a band covering the full scope or "
                            "remove the depth-based radius table.")
                    # The tool configuration's own radius stays a hard floor;
                    # the larger (more restrictive) requirement always wins.
                    return max(band, work.radius_m)

            self.progressMessage.emit("Solving the bounded-curvature tool path…")
            solve_note = {"announced": False}

            def _solve_progress(done: int, total: int) -> None:
                if total <= 0:
                    return
                if not solve_note["announced"]:
                    solve_note["announced"] = True
                    self.progressMessage.emit(
                        f"Solving {total} turn group(s) at bounded curvature…")
                self.setProgress(10.0 + 45.0 * done / total)

            extra_controls = []
            for adjustment in work.adjustments:
                extra_controls.append(plane.station_control_for_kp(
                    adjustment.get("kp"), adjustment.get("dcc_m")))

            solution = path_geometry.generate_route_path(
                plane.points, work.radius_m, work.mode,
                (work.max_deviation_m if work.max_deviation_m > 0.0 else None),
                work.chord_tolerance_m, cancel=self._breathing_cancel(),
                progress=_solve_progress,
                radius_for_vertex=radius_for_vertex,
                extra_controls=extra_controls)
            self._check_cancel()
            self.setProgress(58.0)

            self.progressMessage.emit("Placing the tool path on the map…")
            tool_wgs: List[Point] = []
            tool_kps: List[float] = []
            tool_dcc: List[float] = []
            total = max(len(solution.points), 1)
            for index, point in enumerate(solution.points):
                if index % 256 == 0:
                    self._check_cancel()
                    self.setProgress(58.0 + 17.0 * index / total)
                mapped, kp, dcc = plane.to_wgs84_full(point)
                tool_wgs.append(mapped)
                tool_kps.append(kp)
                tool_dcc.append(dcc)

            # KP-windowed nearest tool-path vertex per course change: the
            # tool KPs are near-monotone (local turn-out loops excepted), so
            # a sorted-KP window bounds the nearest-point search.
            kp_order = sorted(range(len(tool_kps)),
                              key=tool_kps.__getitem__)
            sorted_tool_kps = [tool_kps[i] for i in kp_order]
            window_km = (4.0 * max_rule_radius + 500.0) / 1000.0

            def _nearest_tool_index(kp: float,
                                    local_vertex: Point) -> Optional[int]:
                if not tool_wgs:
                    return None
                lo = bisect.bisect_left(sorted_tool_kps, kp - window_km)
                hi = bisect.bisect_right(sorted_tool_kps, kp + window_km)
                candidates = kp_order[lo:hi] or range(len(tool_wgs))
                return min(candidates,
                           key=lambda i: path_geometry.distance(
                               solution.points[i], local_vertex))

            diagnostics = []
            depth_diff_worst = None
            for item in solution.diagnostics:
                vertex = wgs[item.vertex_index]
                kp = kps[item.vertex_index]
                depth_value = _water_depth(
                    kp, float(vertex.x()), float(vertex.y()))
                # Does the deviation matter for burial?  Compare the depth
                # at the RPL vertex with the depth at the tool path's actual
                # position; only a live sampler can read off-route, so the
                # check is omitted (None) when just a KP profile exists.
                depth_diff = None
                if snapshot_ok:
                    near = _nearest_tool_index(
                        kp, plane.points[item.vertex_index])
                    if near is not None:
                        _prepare_snapshot()
                        rpl_depth = work.depth.sample(
                            float(vertex.y()), float(vertex.x()))
                        lon_t, lat_t = tool_wgs[near]
                        tool_depth = work.depth.sample(lat_t, lon_t)
                        if rpl_depth is not None and tool_depth is not None:
                            depth_diff = abs(float(tool_depth)) \
                                - abs(float(rpl_depth))
                            if depth_diff_worst is None \
                                    or abs(depth_diff) > abs(depth_diff_worst):
                                depth_diff_worst = depth_diff
                diagnostics.append({
                    "control_no": item.control_no,
                    "vertex_index": item.vertex_index,
                    "kp": kp,
                    "lat": float(vertex.y()), "lon": float(vertex.x()),
                    "turn_deg": item.turn_deg, "side": item.side,
                    "solution": item.solution, "miss_m": item.miss_m,
                    "max_offset_m": item.max_offset_m,
                    "radius_m": item.radius_m,
                    "depth_m": depth_value,
                    "depth_diff_m": depth_diff,
                    "control_kind": item.control_kind,
                    "status": item.status, "message": item.message,
                })

            barge_wgs: List[Point] = []
            barge_length = 0.0
            barge_min_radius = None
            layback_values: List[float] = []
            profile_points = path_data.layback_points(work.layback_profile)
            if work.layback_profile and profile_points:
                self.progressMessage.emit("Applying the layback profile…")
                if len(profile_points) == 1:
                    layback_values = [profile_points[0][1]] * len(solution.points)
                else:
                    if work.depth_samples is None:
                        if work.depth is None or not work.depth.is_available():
                            raise path_geometry.PathGeometryError(
                                "This layback profile varies with water depth, "
                                "but no bathymetry source is available.")
                        if not work.depth.prepare(
                                cancel=self.isCanceled,
                                progress=lambda done, count: self.setProgress(
                                    75.0 + 5.0 * done / max(count, 1))):
                            self.cancelled = True
                            return False
                    outside = str(work.layback_profile.get("outside_mode")
                                  or "error")
                    for index, ((lon, lat), kp) in enumerate(
                            zip(tool_wgs, tool_kps)):
                        if index % 128 == 0:
                            self._check_cancel()
                            self.setProgress(80.0 + 10.0 * index / total)
                        depth = (_interpolated_depth(work.depth_samples, kp)
                                 if work.depth_samples is not None
                                 else work.depth.sample(lat, lon))
                        if depth is None:
                            raise path_geometry.PathGeometryError(
                                f"No water depth is available at KP {kp:.3f}; "
                                "the barge track was not generated.")
                        layback_values.append(path_geometry.interpolate_profile(
                            profile_points, abs(float(depth)), outside))

                local_barge = path_geometry.layback_track(
                    solution.points, layback_values)
                # The barge is the tow point: project forward from each mapped
                # tool point by the same local path tangent and layback.  This
                # correctly extends beyond the scoped tool-path endpoints.
                tangents = path_geometry.vertex_tangents(solution.points)
                for tool_point, tangent, layback in zip(
                        tool_wgs, tangents, layback_values):
                    heading = math.atan2(tangent[1], tangent[0])
                    bearing = math.pi / 2.0 - heading
                    placed = work.distance.computeSpheroidProject(
                        QgsPointXY(tool_point[0], tool_point[1]),
                        float(layback), bearing)
                    barge_wgs.append((float(placed.x()), float(placed.y())))
                barge_length = path_geometry.polyline_length(local_barge)
                barge_min_radius = path_geometry.minimum_polyline_radius(
                    local_barge)

            applied_radii = [item["radius_m"] for item in diagnostics
                             if item.get("radius_m")]
            dcc_max_abs = dcc_max_kp = None
            if tool_dcc:
                worst = max(range(len(tool_dcc)),
                            key=lambda i: abs(tool_dcc[i]))
                dcc_max_abs = abs(tool_dcc[worst])
                dcc_max_kp = tool_kps[worst]
            summary = {
                "mode": work.mode,
                "tool": work.tool_display,
                "tool_type": work.tool_type,
                "radius_m": work.radius_m,
                "radius_rules_count": len(rules),
                "radius_min_m": min(applied_radii) if applied_radii
                else work.radius_m,
                "radius_max_m": max(applied_radii) if applied_radii
                else work.radius_m,
                "depth_diff_worst_m": depth_diff_worst,
                "length_m": solution.length_m,
                "route_length_m": path_geometry.polyline_length(plane.points),
                "max_offset_m": solution.max_offset_m,
                "rms_offset_m": solution.rms_offset_m,
                # Signed cross-course deviation from the RPL by KP
                # (positive = port of travel), downsampled but keeping
                # every bucket's worst excursion.
                "dcc": _downsample_dcc(tool_kps, tool_dcc),
                "dcc_max_abs_m": dcc_max_abs,
                "dcc_max_kp": dcc_max_kp,
                "adjustment_count": len(work.adjustments),
                "best_fit_count": sum(
                    1 for item in diagnostics
                    if item.get("solution") == "best_fit"),
                "course_change_count": solution.course_change_count,
                "compound_cluster_count": solution.compound_cluster_count,
                "review_count": sum(1 for item in diagnostics
                                    if item.get("status") != "ok"),
                "barge_generated": bool(barge_wgs),
                "barge_length_m": barge_length,
                "barge_min_radius_m": barge_min_radius,
                "layback_name": str((work.layback_profile or {}).get("name") or ""),
                "layback_min_m": min(layback_values) if layback_values else None,
                "layback_max_m": max(layback_values) if layback_values else None,
            }
            self.result = PathTaskResult(tool_wgs, barge_wgs,
                                         diagnostics, summary)
            self.setProgress(100.0)
            self.progressMessage.emit("Installation paths generated.")
            return True
        except (InterruptedError, path_geometry.PathCancelled):
            self.cancelled = True
            return False
        except Exception as exc:  # task boundary: turn every failure into UI state
            self.error = str(exc)
            return False

    def finished(self, ok: bool) -> None:
        if not ok and not self.cancelled and self.error is None:
            self.error = "Installation path generation failed."
        try:
            self._on_finished(self)
        except Exception:
            _log_callback_failure("Burial Planner installation paths")
