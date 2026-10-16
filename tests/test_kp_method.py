# -*- coding: utf-8 -*-
"""The plugin's KP method on long routes (see docs/KP_METHOD.md).

Synthetic routes only. The reference is an independent geodesic solver
(Karney 2013, through pyproj when it is installed with QGIS; otherwise the
QGIS spheroid functions):

* Geodesic KP: KP at every vertex and at the far end of a 6,000 km route;
  positions mid-leg on the geodesic, and the KP of a point on (and beside)
  the geodesic, in RouteFrame and in the Depth Profile's RouteStationing.
* Grid KP: straight in the grid between vertices, the planar sum at the
  end, and a warning when the route leaves the grid's area of use.
* A route across +/-180 deg is refused with a clear message.
* Editing a Workbench RPL keeps the KPs before the edit exactly.
"""

from __future__ import annotations

import math
import random
from typing import List

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext,
                       QgsGeometry, QgsPointXY, QgsProject)

from .. import depth_profile_core as dpc
from ..kp_geo_utils import AntimeridianError, RouteFrame
from ..kp_range_utils import GridDistanceArea, make_distance_area

REQUIRES_QGIS = True

WGS = QgsCoordinateReferenceSystem("EPSG:4326")
CTX = QgsCoordinateTransformContext()


def _report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""))
    return bool(ok)


class _Reference:
    """Geodesic inverse / direct problems: pyproj (Karney 2013) when available."""

    def __init__(self):
        try:
            import pyproj

            self._geod = pyproj.Geod(ellps="WGS84")
            self.name = "Karney (pyproj)"
        except Exception:  # noqa: BLE001 - the QGIS spheroid functions instead
            self._geod = None
            self._da = make_distance_area(WGS, CTX)
            self.name = "QGIS spheroid"

    def inv(self, a, b):
        """(azimuth deg, distance m) from a to b, (lon, lat) tuples."""
        if self._geod is not None:
            az, _back, dist = self._geod.inv(a[0], a[1], b[0], b[1])
            return az, dist
        p, q = QgsPointXY(*a), QgsPointXY(*b)
        return math.degrees(self._da.bearing(p, q)), float(self._da.measureLine(p, q))

    def fwd(self, a, az, dist):
        if self._geod is not None:
            lon, lat, _ = self._geod.fwd(a[0], a[1], az, dist)
            return lon, lat
        p = self._da.computeSpheroidProject(QgsPointXY(*a), dist, math.radians(az))
        return p.x(), p.y()


REF = _Reference()


def _route(start, end, legs, wiggle_km, seed):
    """Positions on the geodesic start->end pushed sideways: alter courses, legs 5-320 km."""
    rng = random.Random(seed)
    az, total = REF.inv(start, end)
    points = [start]
    for f in sorted(rng.uniform(0.0, 1.0) for _ in range(legs - 1)):
        lon, lat = REF.fwd(start, az, f * total)
        back, _ = REF.inv((lon, lat), end)
        points.append(REF.fwd((lon, lat), back + 90.0, rng.uniform(-wiggle_km, wiggle_km) * 1000.0))
    points.append(end)
    return points


def _cumulative(points) -> List[float]:
    out = [0.0]
    for a, b in zip(points[:-1], points[1:]):
        out.append(out[-1] + REF.inv(a, b)[1])
    return out


TRANSATLANTIC = _route((-9.4, 38.6), (-73.9, 40.4), 30, 60, 1)


def test_geodesic_kp_long_route() -> bool:
    """6,000 km, 30 legs: KP at every vertex and the far end within 1 mm; mid-leg positions on the geodesic
    and the KP of a point on it within 5 cm (RouteFrame and RouteStationing); the KP of a point 100 m
    beside it within 1 cm, 1 km beside it within 20 cm."""
    pts = TRANSATLANTIC
    cum = _cumulative(pts)
    q = [QgsPointXY(*p) for p in pts]
    da = make_distance_area(WGS, CTX)
    frame = RouteFrame.from_source([QgsGeometry.fromPolylineXY(q)], da)
    station, _ = dpc.route_from_points(q, WGS, da)
    vertex = max(abs(frame.kp_at_point(p).kp_km * 1000.0 - c) for p, c in zip(q, cum))
    far = max(abs(frame.total_length_m - cum[-1]), abs(station.line_length - cum[-1]))
    pos = kp_on = kp_near = kp_off = 0.0
    for i, (a, b) in enumerate(zip(pts[:-1], pts[1:])):
        az, length = REF.inv(a, b)
        for f in (0.25, 0.5, 0.75):
            true = REF.fwd(a, az, f * length)
            kp_m = cum[i] + f * length
            for p in (frame.point_at_kp(kp_m / 1000.0), station.point_at(kp_m)):
                pos = max(pos, REF.inv((p.x(), p.y()), true)[1])
            kp_on = max(kp_on, abs(frame.kp_at_point(QgsPointXY(*true)).kp_km * 1000.0 - kp_m),
                        abs(station.measure_along(QgsPointXY(*true)) - kp_m))
            if length >= 20000.0:      # 1 km beside a short leg near a sharp turn is nearer the other leg
                for offset in (100.0, 1000.0):
                    beside = REF.fwd(true, REF.inv(true, b)[0] + 90.0, offset)
                    error = abs(frame.kp_at_point(QgsPointXY(*beside)).kp_km * 1000.0 - kp_m)
                    if offset < 500.0:
                        kp_near = max(kp_near, error)
                    else:
                        kp_off = max(kp_off, error)
    ok = vertex < 0.001 and far < 0.001 and pos < 0.05 and kp_on < 0.05 and kp_near < 0.01 and kp_off < 0.2
    return _report(f"geodesic KP on a {cum[-1] / 1e6:,.0f},000 km route vs {REF.name}", ok,
                   f"vertex {vertex * 1000:.3f} mm, far end {far * 1000:.3f} mm, mid-leg position {pos:.3f} m, "
                   f"KP of a point on / 100 m / 1 km beside the geodesic {kp_on:.3f} / {kp_near:.3f} / "
                   f"{kp_off:.3f} m")


def test_grid_kp_in_one_zone() -> bool:
    """Grid KP in a UTM zone: straight in the grid between vertices, KP = planar sum, no warning."""
    utm = QgsCoordinateReferenceSystem("EPSG:32630")
    to_wgs = QgsCoordinateTransform(utm, WGS, QgsProject.instance())
    corners = [QgsPointXY(480000.0, 6300000.0), QgsPointXY(510000.0, 6360000.0), QgsPointXY(470000.0, 6410000.0)]
    q = [to_wgs.transform(p) for p in corners]
    grid = GridDistanceArea(WGS, CTX, utm, "set in KP settings")
    frame = RouteFrame.from_source([QgsGeometry.fromPolylineXY(q)], grid)
    planar = sum(math.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in zip(corners[:-1], corners[1:]))
    to_utm = QgsCoordinateTransform(WGS, utm, QgsProject.instance())
    first = math.hypot(corners[1].x() - corners[0].x(), corners[1].y() - corners[0].y())
    off = 0.0
    for f in (0.25, 0.5, 0.75):
        p = to_utm.transform(frame.point_at_kp(f * first / 1000.0))
        dx, dy = corners[1].x() - corners[0].x(), corners[1].y() - corners[0].y()
        off = max(off, abs((p.x() - corners[0].x()) * dy - (p.y() - corners[0].y()) * dx) / math.hypot(dx, dy))
    notes = frame.kp_basis_notes()
    ok = abs(frame.total_length_m - planar) < 0.001 and off < 0.01 and not [n for n in notes if n[0] == "warn"]
    return _report("grid KP: straight in the grid, KP = planar sum, no warning inside the zone", ok,
                   f"end {frame.total_length_m - planar:+.4f} m, off the grid line {off * 1000:.1f} mm; "
                   + " ".join(t for _l, t in notes))


def test_grid_kp_outside_its_zone_is_flagged() -> bool:
    """One UTM grid over a 6,000 km route: the KP basis notes warn (outside the area of use, far from geodesic)."""
    q = [QgsPointXY(*p) for p in TRANSATLANTIC]
    frame = RouteFrame.from_source([QgsGeometry.fromPolylineXY(q)], GridDistanceArea(WGS, CTX, None))
    warnings = [t for level, t in frame.kp_basis_notes() if level == "warn"]
    ok = any("outside the area of use" in t for t in warnings) and any("from geodesic KP" in t for t in warnings)
    return _report("grid KP over several zones is flagged", ok, "; ".join(w[:90] for w in warnings))


def test_antimeridian_refused() -> bool:
    pts = [QgsPointXY(178.0, -16.0), QgsPointXY(179.6, -16.2), QgsPointXY(-179.5, -16.4)]
    da = make_distance_area(WGS, CTX)
    refused = []
    for build in (lambda: RouteFrame.from_source([QgsGeometry.fromPolylineXY(pts)], da),
                  lambda: dpc.route_from_points(pts, WGS, da)):
        try:
            build()
            refused.append(False)
        except AntimeridianError as exc:
            refused.append("180" in str(exc))
    return _report("a route across ±180° is refused with a clear message", all(refused) and len(refused) == 2)


def test_workbench_edit_keeps_earlier_kps() -> bool:
    """An RPL with rounded leg lengths: moving a position near the end changes only the KPs after it."""
    from ..workbench import rpl_engine as eng
    from ..workbench.rpl_engine import RplModel, RplPoint, RplSegment

    da = make_distance_area(WGS, CTX)
    points = [RplPoint(seq=i, pos_no=i + 1, event="", lat=50.0 + 0.1 * i, lon=0.3 * (i % 2)) for i in range(40)]
    segments = [RplSegment(seq=i, slack_pct=1.0) for i in range(39)]
    model = RplModel(points=points, segments=segments)
    eng.recompute(model, da)
    stated = [p.dist_cum_km for p in model.points]
    for seg in model.segments:          # the document's rounded leg lengths (1 m)
        seg.dist_km = round(seg.dist_km, 3)
    eng.move_point(model, 35, model.points[35].lat, model.points[35].lon + 0.01, da)
    before = [abs(p.dist_cum_km - s) for p, s in zip(model.points[:35], stated[:35])]
    moved = abs(model.points[36].dist_cum_km - stated[36]) > 1e-6
    ok = max(before) == 0.0 and moved
    return _report("Workbench edit: KPs before the edit unchanged (no rounding build-up)", ok,
                   f"largest change before the edit {max(before) * 1e6:.3f} mm")


def run_all() -> List[bool]:
    results = [
        test_geodesic_kp_long_route(),
        test_grid_kp_in_one_zone(),
        test_grid_kp_outside_its_zone_is_flagged(),
        test_antimeridian_refused(),
        test_workbench_edit_keeps_earlier_kps(),
    ]
    print(f"\n{sum(results)}/{len(results)} KP method checks passed.")
    return results


if __name__ == "__main__":
    run_all()
