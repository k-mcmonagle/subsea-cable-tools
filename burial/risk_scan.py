# -*- coding: utf-8 -*-
"""Risk Profile feature scan (requires the QGIS API).

Feature-centric counterpart to the Exclusion stack's interval acquisition:
each check scans one registered input layer for features on or near the
route and yields one hazard row per interaction —

- point features: nearest approach KP + geodesic offset;
- line features: one hazard per route crossing (offset 0, crossing angle)
  or the nearest approach when not crossing;
- polygon features: the KP range where the route runs inside the polygon,
  or the nearest approach when not crossing.

Offsets are signed by the side of the route in the direction of travel:
positive to starboard, negative to port (falling back to positive when the
local bearing cannot be resolved). Risk bands apply to the magnitude.

A second check kind, ``route_turns``, scans the route geometry itself for
alter-courses: each vertex's course change is computed exactly like the
"Extract A/C Points" processing algorithm (signed ``alter_course``,
``turn_abs`` magnitude, small changes ignorable) and risk comes from the
check's attribute rules over ``turn_abs``.

Feature loading happens on the main thread (``snapshot_check_features``);
the geometry work runs either inline (``scan_check``) or on a worker
thread via ``RiskScanTask``, which owns cloned route geometries — the
``BurialAnalysisTask`` pattern. Nearest-point *selection* is planar in
WGS84 — adequate at proximity-check scales; reported distances are
geodesic (``QgsDistanceArea``).
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsTask,
)
from qgis.PyQt.QtCore import pyqtSignal

from ..qgis_compat import GEOMETRY_LINE, GEOMETRY_POINT, GEOMETRY_POLYGON
from ..workbench.rules_inputs import _filter_expression, _load_features_wgs84
from ..workbench.rules_engine import Interval
from . import risk, schema

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

CHECK_KIND_FEATURES = "features"
CHECK_KIND_ROUTE_TURNS = "route_turns"

# Attribute names exposed by the route-turns scan (mirrors the Extract A/C
# Points processing algorithm's output fields).
TURN_SIGNED_ATTR = "alter_course"
TURN_ABS_ATTR = "turn_abs"


def _task_flag(name: str, default: int = 0):
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


_CAN_CANCEL = _task_flag("CanCancel")


class RiskScanError(ValueError):
    """Raised when a check cannot be evaluated at all."""


def check_kind(config: Dict) -> str:
    kind = (config.get("kind") or "").strip()
    return kind if kind in (CHECK_KIND_FEATURES, CHECK_KIND_ROUTE_TURNS) \
        else CHECK_KIND_FEATURES


def _expanded_rect(rect: QgsRectangle, radius_m: float) -> QgsRectangle:
    """The WGS84 rectangle grown by ``radius_m`` (conservative)."""
    radius = max(float(radius_m), 1.0)
    deg_lat = radius / 110540.0 + 1e-6
    centre_lat = (rect.yMinimum() + rect.yMaximum()) / 2.0
    cos_lat = max(math.cos(math.radians(centre_lat)), 0.087)
    deg_lon = radius / (111320.0 * cos_lat) + 1e-6
    return QgsRectangle(rect.xMinimum() - deg_lon, rect.yMinimum() - deg_lat,
                        rect.xMaximum() + deg_lon, rect.yMaximum() + deg_lat)


def _acute_angle_deg(bearing_a: float, bearing_b: float) -> float:
    delta = abs(math.degrees(bearing_a) - math.degrees(bearing_b)) % 180.0
    return min(delta, 180.0 - delta)


def _route_bearing(route, distance, kp: float) -> Optional[float]:
    half_km = 0.02
    p1 = route.point_at_kp(max(kp - half_km, 0.0), clamp=True)
    p2 = route.point_at_kp(min(kp + half_km, route.total_length_km), clamp=True)
    if p1 is None or p2 is None or p1 == p2:
        return None
    try:
        return float(distance.bearing(p1, p2))
    except Exception:
        return None


def _feature_bearing(geom: QgsGeometry, at_point: QgsPointXY,
                     distance) -> Optional[float]:
    try:
        result = geom.closestSegmentWithContext(at_point)
        after = int(result[2])
        v0 = geom.vertexAt(after - 1)
        v1 = geom.vertexAt(after)
        p0 = QgsPointXY(v0.x(), v0.y())
        p1 = QgsPointXY(v1.x(), v1.y())
        if p0 == p1:
            return None
        return float(distance.bearing(p0, p1))
    except Exception:
        return None


def _side_sign(route, distance, kp: float, route_pt: Optional[QgsPointXY],
               feat_pt: Optional[QgsPointXY], direction: int) -> int:
    """+1 starboard / -1 port of the direction of travel; +1 when unknown."""
    if route_pt is None or feat_pt is None or route_pt == feat_pt:
        return 1
    route_bearing = _route_bearing(route, distance, kp)
    if route_bearing is None:
        return 1
    try:
        to_feature = float(distance.bearing(route_pt, feat_pt))
    except Exception:
        return 1
    side = math.sin(to_feature - route_bearing)
    sign = 1 if side >= 0 else -1
    return -sign if int(direction or 1) < 0 else sign


def _geometry_parts(geom: QgsGeometry) -> List[QgsGeometry]:
    if geom.isMultipart():
        try:
            return [QgsGeometry(part) for part in geom.asGeometryCollection()]
        except Exception:
            return [geom]
    return [geom]


def _dedupe_points(points: List[QgsPointXY], tol_deg: float = 1e-7
                   ) -> List[QgsPointXY]:
    unique: List[QgsPointXY] = []
    for point in points:
        if all(abs(point.x() - u.x()) > tol_deg
               or abs(point.y() - u.y()) > tol_deg for u in unique):
            unique.append(point)
    return unique


def _crossing_points(feat_geom: QgsGeometry, route_geom: QgsGeometry
                     ) -> List[QgsPointXY]:
    inter = feat_geom.intersection(route_geom)
    if inter is None or inter.isEmpty():
        return []
    points = []
    try:
        for vertex in inter.vertices():
            points.append(QgsPointXY(vertex.x(), vertex.y()))
    except Exception:
        pass
    return _dedupe_points(points)


def _nearest_approach(route, route_geom: QgsGeometry, feat_geom: QgsGeometry,
                      distance) -> Optional[Tuple[float, float,
                                                  QgsPointXY, QgsPointXY]]:
    """(kp_km, offset_m, route point, feature point) when not crossing."""
    try:
        on_route = route_geom.nearestPoint(feat_geom)
        route_pt = QgsPointXY(on_route.asPoint())
    except Exception:
        return None
    hit = route.kp_at_point(route_pt)
    if hit.snapped_xy is None:
        return None
    try:
        on_feat = feat_geom.nearestPoint(QgsGeometry.fromPointXY(route_pt))
        feat_pt = QgsPointXY(on_feat.asPoint())
        offset = float(distance.measureLine(route_pt, feat_pt))
    except Exception:
        return None
    return hit.kp_km, offset, route_pt, feat_pt


# ---------------------------------------------------------------------------
# Main-thread feature snapshot
# ---------------------------------------------------------------------------


def snapshot_check_features(check_row: Dict, layer, route_geom: QgsGeometry,
                            project: Optional[QgsProject] = None
                            ) -> List[Dict]:
    """Thread-safe snapshot of a check's candidate features (main thread).

    Applies the feature filter, pre-filters with a spatial index over the
    route's search-expanded bounding box, and clones each candidate's WGS84
    geometry plus the attributes the check uses. The result is safe to hand
    to a worker thread.
    """
    config = risk.check_config(check_row)
    check_name = check_row.get("name") or "check"
    search_m = float(config.get("distance_m") or 0.0)
    if search_m <= 0:
        raise RiskScanError(
            f"Check '{check_name}' has no search distance configured.")
    if layer is None or not layer.isValid():
        raise RiskScanError(
            f"Check '{check_name}': the input layer could not be opened.")
    project = project or QgsProject.instance()

    index, feats = _load_features_wgs84(layer, project)
    expr, ctx = _filter_expression(config.get("filter_expression", ""))
    label_attribute = (config.get("label_attribute") or "").strip()
    risk_attribute = (config.get("attribute") or "").strip()
    layer_name = layer.name()

    candidates = index.intersects(_expanded_rect(route_geom.boundingBox(),
                                                 search_m))
    out: List[Dict] = []
    for candidate in candidates:
        stored = feats.get(candidate)
        if stored is None:
            continue
        geom, feat = stored
        if expr is not None:
            ctx.setFeature(feat)
            if not bool(expr.evaluate(ctx)):
                continue
        attributes: Dict = {}
        for name in {label_attribute, risk_attribute}:
            if not name:
                continue
            try:
                value = feat[name]
            except KeyError:
                continue
            attributes[name] = None if value is None else value
        fid = feat.id()
        label = ""
        if label_attribute:
            value = attributes.get(label_attribute)
            if value is not None and str(value).strip():
                label = str(value).strip()
        out.append({
            "geom": QgsGeometry(geom),
            "attrs": attributes,
            "fid": fid,
            "label": label or f"{layer_name} #{fid}",
        })
    return out


# ---------------------------------------------------------------------------
# Worker-safe scans
# ---------------------------------------------------------------------------


def scan_snapshot(plan_id: str, check_row: Dict, features: List[Dict],
                  route, distance, scope: Optional[Interval] = None,
                  direction: int = 1,
                  progress: Optional[Callable[[str], None]] = None,
                  cancel: Optional[Callable[[], bool]] = None
                  ) -> Tuple[List[Dict], List[str]]:
    """Run one features check over snapshotted features (thread-safe)."""
    config = risk.check_config(check_row)
    check_id = str(check_row.get("check_id") or "")
    check_name = check_row.get("name") or "check"
    search_m = float(config.get("distance_m") or 0.0)

    route_geom = QgsGeometry.collectGeometry(
        [QgsGeometry(g) for g in route.geometries])
    scope_lo = scope_hi = None
    if scope is not None:
        scope_lo = min(scope.start_km, scope.end_km)
        scope_hi = max(scope.start_km, scope.end_km)

    def in_scope(lo_kp: float, hi_kp: float) -> bool:
        if scope_lo is None:
            return True
        return hi_kp >= scope_lo - 1e-9 and lo_kp <= scope_hi + 1e-9

    hazards: List[Dict] = []
    warnings: List[str] = []

    def add(entry: Dict, part: int, kp: float, end_kp: Optional[float],
            offset_m: float, crossing: bool, angle: Optional[float],
            lat: Optional[float], lon: Optional[float]) -> None:
        lo = min(kp, end_kp if end_kp is not None else kp)
        hi = max(kp, end_kp if end_kp is not None else kp)
        if not in_scope(lo, hi):
            return
        auto = risk.evaluate_risk(config, abs(offset_m), entry["attrs"])
        hazards.append(risk.new_hazard_row(
            plan_id, check_id, f"{entry['fid']}#{part}", entry["label"],
            lo, hi, offset_m, crossing, angle, lat, lon, auto,
            entry["attrs"]))

    for done, entry in enumerate(features):
        if cancel is not None and done % 50 == 0 and cancel():
            return hazards, warnings
        if progress is not None and done % 200 == 0:
            progress(f"{check_name}: {done}/{len(features)} features…")
        geom = entry["geom"]
        geom_type = int(geom.type())

        if geom_type == int(GEOMETRY_POINT):
            points = []
            try:
                points = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
            except Exception:
                points = []
            for part, point in enumerate(points):
                hit = route.kp_at_point(point)
                if hit.snapped_xy is None or hit.dcc_m > search_m:
                    continue
                sign = _side_sign(route, distance, hit.kp_km,
                                  hit.snapped_xy, point, direction)
                add(entry, part, hit.kp_km, None, sign * hit.dcc_m,
                    False, None, point.y(), point.x())
            continue

        if geom_type == int(GEOMETRY_LINE):
            crossings = _crossing_points(geom, route_geom)
            if crossings:
                for part, point in enumerate(crossings):
                    hit = route.kp_at_point(point)
                    if hit.snapped_xy is None:
                        continue
                    angle = None
                    route_bearing = _route_bearing(route, distance, hit.kp_km)
                    feat_bearing = _feature_bearing(geom, point, distance)
                    if route_bearing is not None and feat_bearing is not None:
                        angle = round(_acute_angle_deg(route_bearing,
                                                       feat_bearing), 1)
                    add(entry, part, hit.kp_km, None, 0.0, True, angle,
                        point.y(), point.x())
                continue
            near = _nearest_approach(route, route_geom, geom, distance)
            if near is not None and near[1] <= search_m:
                kp, offset, route_pt, feat_pt = near
                sign = _side_sign(route, distance, kp, route_pt, feat_pt,
                                  direction)
                add(entry, 0, kp, None, sign * offset, False, None,
                    feat_pt.y(), feat_pt.x())
            continue

        if geom_type == int(GEOMETRY_POLYGON):
            if geom.intersects(route_geom):
                inside = route_geom.intersection(geom)
                parts = _geometry_parts(inside) if inside is not None else []
                emitted = 0
                for part_geom in parts:
                    if part_geom is None or part_geom.isEmpty():
                        continue
                    points = []
                    try:
                        points = [QgsPointXY(v.x(), v.y())
                                  for v in part_geom.vertices()]
                    except Exception:
                        points = []
                    if not points:
                        continue
                    kp_values = []
                    for point in (points[0], points[-1]):
                        hit = route.kp_at_point(point)
                        if hit.snapped_xy is not None:
                            kp_values.append(hit.kp_km)
                    if not kp_values:
                        continue
                    lo, hi = min(kp_values), max(kp_values)
                    mid = route.point_at_kp((lo + hi) / 2.0, clamp=True)
                    add(entry, emitted, lo, hi, 0.0, True, None,
                        mid.y() if mid else None, mid.x() if mid else None)
                    emitted += 1
                continue
            near = _nearest_approach(route, route_geom, geom, distance)
            if near is not None and near[1] <= search_m:
                kp, offset, route_pt, feat_pt = near
                sign = _side_sign(route, distance, kp, route_pt, feat_pt,
                                  direction)
                add(entry, 0, kp, None, sign * offset, False, None,
                    feat_pt.y(), feat_pt.x())
            continue

        warnings.append(
            f"Check '{check_name}': feature {entry['fid']} has an "
            "unsupported geometry type and was skipped.")

    return risk.sort_hazards(hazards), warnings


def _route_vertices(route) -> List[QgsPointXY]:
    """Every route vertex in KP order, consecutive duplicates dropped."""
    points: List[QgsPointXY] = []
    for geom in route.geometries:
        try:
            vertices = [QgsPointXY(v.x(), v.y()) for v in geom.vertices()]
        except Exception:
            continue
        for point in vertices:
            if points and abs(point.x() - points[-1].x()) < 1e-12 \
                    and abs(point.y() - points[-1].y()) < 1e-12:
                continue
            points.append(point)
    return points


def scan_route_turns(plan_id: str, check_row: Dict, route, distance,
                     scope: Optional[Interval] = None, direction: int = 1
                     ) -> Tuple[List[Dict], List[str]]:
    """Alter-course scan over the route geometry (thread-safe).

    Mirrors the Extract A/C Points algorithm: per interior vertex the
    signed course change (positive = starboard turn in the direction of
    travel) and its magnitude; risk comes from the check's attribute rules
    over ``turn_abs`` (falling back to ``default_risk``). Only vertices at
    or above ``min_course_change_deg`` are considered at all.
    """
    config = risk.check_config(check_row)
    check_id = str(check_row.get("check_id") or "")
    try:
        min_cc = float(config.get("min_course_change_deg") or 0.5)
    except (TypeError, ValueError):
        min_cc = 0.5

    points = _route_vertices(route)
    scope_lo = scope_hi = None
    if scope is not None:
        scope_lo = min(scope.start_km, scope.end_km)
        scope_hi = max(scope.start_km, scope.end_km)

    hazards: List[Dict] = []
    warnings: List[str] = []
    cumulative_m = 0.0
    for i in range(1, len(points)):
        try:
            seg_m = float(distance.measureLine(points[i - 1], points[i]))
        except Exception:
            seg_m = 0.0
        if seg_m <= 0.0:
            continue
        cumulative_m += seg_m
        if i >= len(points) - 1:
            break
        p_prev, p_curr, p_next = points[i - 1], points[i], points[i + 1]
        try:
            if float(distance.measureLine(p_curr, p_next)) <= 0.0:
                continue
            b1 = math.degrees(distance.bearing(p_prev, p_curr))
            b2 = math.degrees(distance.bearing(p_curr, p_next))
        except Exception:
            continue
        turn = b2 - b1
        while turn > 180.0:
            turn -= 360.0
        while turn <= -180.0:
            turn += 360.0
        if int(direction or 1) < 0:
            turn = -turn
        turn_abs = abs(turn)
        if turn_abs < max(min_cc, 1e-4):
            continue
        kp = cumulative_m / 1000.0
        if scope_lo is not None and not (scope_lo - 1e-9 <= kp
                                         <= scope_hi + 1e-9):
            continue
        attributes = {TURN_SIGNED_ATTR: round(turn, 2),
                      TURN_ABS_ATTR: round(turn_abs, 2)}
        auto = risk.evaluate_risk(config, None, attributes)
        if not auto:
            continue  # compliant A/C — not a hazard
        side = "stbd" if turn >= 0 else "port"
        hazards.append(risk.new_hazard_row(
            plan_id, check_id, f"ac#{i}",
            f"A/C {turn_abs:.1f}° {side}", kp, None, 0.0, False, None,
            p_curr.y(), p_curr.x(), auto, attributes))
    return risk.sort_hazards(hazards), warnings


def scan_check(plan_id: str, check_row: Dict, layer, route, distance,
               scope: Optional[Interval] = None,
               project: Optional[QgsProject] = None,
               progress: Optional[Callable[[str], None]] = None,
               direction: int = 1) -> Tuple[List[Dict], List[str]]:
    """Synchronous scan of one check (used by tests and small runs)."""
    config = risk.check_config(check_row)
    if check_kind(config) == CHECK_KIND_ROUTE_TURNS:
        return scan_route_turns(plan_id, check_row, route, distance,
                                scope=scope, direction=direction)
    route_geom = QgsGeometry.collectGeometry(
        [QgsGeometry(g) for g in route.geometries])
    features = snapshot_check_features(check_row, layer, route_geom, project)
    return scan_snapshot(plan_id, check_row, features, route, distance,
                         scope=scope, direction=direction, progress=progress)


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


class RiskScanTask(QgsTask):
    """Run the prepared check jobs on a worker thread.

    Jobs are ``(check_row, features-or-None)`` — features come from
    ``snapshot_check_features`` on the main thread; route geometries are
    cloned so the worker owns everything it touches. ``finished()``
    invokes the completion callback on the main thread.
    """

    progressMessage = pyqtSignal(str)

    def __init__(self, plan_id: str, jobs: List[Tuple[Dict, Optional[List[Dict]]]],
                 route_geoms: List[QgsGeometry], transform_context,
                 scope: Optional[Interval], direction: int,
                 on_finished: Callable[["RiskScanTask"], None],
                 description: str = "Burial Planner risk scan"):
        super().__init__(description, _CAN_CANCEL)
        self.plan_id = plan_id
        self.jobs = jobs
        self._route_geoms = route_geoms
        self._transform_context = transform_context
        self.scope = scope
        self.direction = direction
        self.hazards: List[Dict] = []
        self.warnings: List[str] = []
        self.run_check_ids: List[str] = []
        self.error: Optional[str] = None
        self.cancelled = False
        self._on_finished = on_finished

    def run(self) -> bool:
        try:
            from ..kp_geo_utils import RouteFrame
            from ..kp_range_utils import make_distance_area

            distance = make_distance_area(WGS84, self._transform_context)
            route = RouteFrame.from_source(
                [QgsGeometry(g) for g in self._route_geoms], distance)
            total = max(len(self.jobs), 1)
            for i, (check_row, features) in enumerate(self.jobs):
                if self.isCanceled():
                    self.cancelled = True
                    return False
                name = check_row.get("name") or "check"
                self.progressMessage.emit(f"Scanning {name}…")
                config = risk.check_config(check_row)
                if check_kind(config) == CHECK_KIND_ROUTE_TURNS:
                    found, warnings = scan_route_turns(
                        self.plan_id, check_row, route, distance,
                        scope=self.scope, direction=self.direction)
                else:
                    found, warnings = scan_snapshot(
                        self.plan_id, check_row, features or [], route,
                        distance, scope=self.scope, direction=self.direction,
                        progress=self.progressMessage.emit,
                        cancel=self.isCanceled)
                    if self.isCanceled():
                        self.cancelled = True
                        return False
                self.hazards.extend(found)
                self.warnings.extend(warnings)
                self.run_check_ids.append(str(check_row.get("check_id") or ""))
                self.setProgress(100.0 * (i + 1) / total)
            return True
        except Exception as exc:  # surfaced via self.error on the main thread
            self.error = str(exc)
            return False

    def finished(self, _ok: bool) -> None:
        if self.isCanceled():
            self.cancelled = True
        self._on_finished(self)
