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

Candidate features are pre-filtered with a spatial index over the route's
bounding box expanded by the search distance; exact distances are geodesic
(``QgsDistanceArea``). Nearest-point *selection* is planar in WGS84 —
adequate at proximity-check scales; the reported distance is measured
geodesically.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import QgsGeometry, QgsPointXY, QgsProject, QgsRectangle

from ..qgis_compat import GEOMETRY_LINE, GEOMETRY_POINT, GEOMETRY_POLYGON
from ..workbench.rules_inputs import _filter_expression, _load_features_wgs84
from ..workbench.rules_engine import Interval
from . import risk, schema


class RiskScanError(ValueError):
    """Raised when a check cannot be evaluated at all."""


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
                      distance) -> Optional[Tuple[float, float, QgsPointXY]]:
    """(kp_km, offset_m, feature-side point) for a non-crossing feature."""
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
    return hit.kp_km, offset, feat_pt


def scan_check(plan_id: str, check_row: Dict, layer, route, distance,
               scope: Optional[Interval] = None,
               project: Optional[QgsProject] = None,
               progress: Optional[Callable[[str], None]] = None
               ) -> Tuple[List[Dict], List[str]]:
    """Run one check over its input layer; returns (hazards, warnings)."""
    config = risk.check_config(check_row)
    check_id = str(check_row.get("check_id") or "")
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

    route_geom = QgsGeometry.collectGeometry(
        [QgsGeometry(g) for g in route.geometries])
    candidates = index.intersects(_expanded_rect(route_geom.boundingBox(),
                                                 search_m))
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

    def attributes_for(feat) -> Dict:
        out: Dict = {}
        for name in {label_attribute, risk_attribute}:
            if not name:
                continue
            try:
                value = feat[name]
            except KeyError:
                continue
            out[name] = None if value is None else value
        return out

    def label_for(feat, fid) -> str:
        if label_attribute:
            try:
                value = feat[label_attribute]
                if value is not None and str(value).strip():
                    return str(value).strip()
            except KeyError:
                pass
        return f"{layer.name()} #{fid}"

    def add(feat, fid, part: int, kp: float, end_kp: Optional[float],
            offset_m: float, crossing: bool, angle: Optional[float],
            lat: Optional[float], lon: Optional[float]) -> None:
        lo = min(kp, end_kp if end_kp is not None else kp)
        hi = max(kp, end_kp if end_kp is not None else kp)
        if not in_scope(lo, hi):
            return
        attributes = attributes_for(feat)
        auto = risk.evaluate_risk(config, offset_m, attributes)
        hazards.append(risk.new_hazard_row(
            plan_id, check_id, f"{fid}#{part}", label_for(feat, fid),
            lo, hi, offset_m, crossing, angle, lat, lon, auto, attributes))

    for done, candidate in enumerate(candidates):
        if progress is not None and done % 200 == 0:
            progress(f"{check_name}: {done}/{len(candidates)} features…")
        stored = feats.get(candidate)
        if stored is None:
            continue
        geom, feat = stored
        fid = feat.id()
        if expr is not None:
            ctx.setFeature(feat)
            if not bool(expr.evaluate(ctx)):
                continue
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
                add(feat, fid, part, hit.kp_km, None, hit.dcc_m,
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
                    add(feat, fid, part, hit.kp_km, None, 0.0, True, angle,
                        point.y(), point.x())
                continue
            near = _nearest_approach(route, route_geom, geom, distance)
            if near is not None and near[1] <= search_m:
                kp, offset, feat_pt = near
                add(feat, fid, 0, kp, None, offset, False, None,
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
                    add(feat, fid, emitted, lo, hi, 0.0, True, None,
                        mid.y() if mid else None, mid.x() if mid else None)
                    emitted += 1
                continue
            near = _nearest_approach(route, route_geom, geom, distance)
            if near is not None and near[1] <= search_m:
                kp, offset, feat_pt = near
                add(feat, fid, 0, kp, None, offset, False, None,
                    feat_pt.y(), feat_pt.x())
            continue

        warnings.append(
            f"Check '{check_name}': feature {fid} has an unsupported "
            "geometry type and was skipped.")

    return risk.sort_hazards(hazards), warnings
