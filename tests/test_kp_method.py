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
* Editing a Workbench RPL keeps the KPs before the edit exactly; its
  positions at a KP lie on the geodesic; RPL comparison measures on WGS84.
* A line layer becomes a route in SeqNo order wherever it is read.
* A segment KP cannot measure stops (or ends) the chainage, never counts 0 m.
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
    try:
        RouteFrame.from_source([QgsGeometry.fromPolylineXY(pts)], da)
        refused.append(False)
    except AntimeridianError as exc:
        refused.append("180" in str(exc))
    # The Depth Profile builders return the reason as the route status (shown on the plot).
    route, status = dpc.route_from_points(pts, WGS, da)
    refused.append(route is None and "180" in (status or ""))
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


def test_stated_kp_check_at_import() -> bool:
    """RPL import check: stated KPs equal to geodesic chainage agree; stated grid KPs are recognised."""
    from ..kp_datum import check_position_kps

    utm = QgsCoordinateReferenceSystem("EPSG:32630")
    to_wgs = QgsCoordinateTransform(utm, WGS, QgsProject.instance())
    grid_pts = [QgsPointXY(480000.0 + 20000.0 * i, 6300000.0 + 15000.0 * (i % 2)) for i in range(8)]
    lonlat = [to_wgs.transform(p) for p in grid_pts]
    geodesic = [5.0]
    for a, b in zip(lonlat[:-1], lonlat[1:]):
        geodesic.append(geodesic[-1] + REF.inv((a.x(), a.y()), (b.x(), b.y()))[1] / 1000.0)
    planar = [5.0]
    for a, b in zip(grid_pts[:-1], grid_pts[1:]):
        planar.append(planar[-1] + math.hypot(b.x() - a.x(), b.y() - a.y()) / 1000.0)
    same = check_position_kps([(p.x(), p.y(), k) for p, k in zip(lonlat, geodesic)])
    grid = check_position_kps([(p.x(), p.y(), k) for p, k in zip(lonlat, planar)])
    ok = same.level == "ok" and same.max_diff_m < 0.01 and grid.level != "ok" and grid.looks_grid
    return _report("RPL import KP check: geodesic KPs agree, grid-chained KPs are recognised", ok,
                   f"geodesic {same.max_diff_m * 1000:.2f} mm; grid {grid.max_diff_m:.1f} m ({grid.summary()[:80]})")


def test_workbench_position_on_geodesic() -> bool:
    """A position at a KP inside a 20 km Workbench leg lies on the geodesic (not straight in degrees)."""
    from ..workbench import rpl_engine as eng
    from ..workbench.compare_panel import _geodesic_m
    from ..workbench.rpl_engine import RplModel, RplPoint, RplSegment

    da = make_distance_area(WGS, CTX)
    a = (-6.0, 57.5)
    b = REF.fwd(a, 60.0, 20000.0)
    model = RplModel(points=[RplPoint(seq=0, pos_no=1, event="", lat=a[1], lon=a[0]),
                             RplPoint(seq=1, pos_no=2, event="", lat=b[1], lon=b[0])],
                     segments=[RplSegment(seq=0, slack_pct=1.0)])
    eng.recompute(model, da)
    worst = 0.0
    for f in (0.1, 0.25, 0.5, 0.75, 0.9):
        lat, lon = eng.point_at_kp(model, model.points[0].dist_cum_km + f * model.points[1].dist_cum_km, da)
        on = REF.fwd(a, REF.inv(a, b)[0], f * REF.inv(a, b)[1])
        worst = max(worst, REF.inv((lon, lat), on)[1])
    straight = REF.inv(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0), REF.fwd(a, REF.inv(a, b)[0], REF.inv(a, b)[1] / 2))[1]
    compare = abs(_geodesic_m(a[1], a[0], b[1], b[0]) - REF.inv(a, b)[1])
    ok = worst < 0.01 and straight > 1.0 and compare < 0.001
    return _report("Workbench: position at a KP on the geodesic; RPL compare distance on WGS84", ok,
                   f"worst {worst * 1000:.2f} mm (straight in degrees {straight:.1f} m); compare {compare * 1000:.3f} mm")


def test_layer_route_in_seqno_order() -> bool:
    """A lines layer whose features are stored out of order is still a route in SeqNo order."""
    from qgis.core import QgsFeature, QgsField, QgsVectorLayer

    from ..qgis_compat import FIELD_TYPE_INT

    layer = QgsVectorLayer("LineString?crs=EPSG:4326", "legs", "memory")
    layer.dataProvider().addAttributes([QgsField("SeqNo", FIELD_TYPE_INT)])
    layer.updateFields()
    legs = [(3, [(2.0, 50.0), (3.0, 50.0)]), (1, [(0.0, 50.0), (1.0, 50.0)]), (2, [(1.0, 50.0), (2.0, 50.0)])]
    feats = []
    for seq, coords in legs:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*c) for c in coords]))
        feat.setAttribute("SeqNo", seq)
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    route = RouteFrame.from_source(layer, make_distance_area(WGS, CTX))
    first = route.point_at_kp(0.0)
    kp_two = route.kp_at_point(QgsPointXY(2.0, 50.0)).kp_km
    expected = (REF.inv((0.0, 50.0), (1.0, 50.0))[1] + REF.inv((1.0, 50.0), (2.0, 50.0))[1]) / 1000.0
    ok = abs(first.x()) < 1e-9 and abs(kp_two - expected) < 1e-6
    return _report("Route from a layer follows SeqNo, not storage order", ok,
                   f"KP 0 at lon {first.x():.3f}; KP at lon 2 = {kp_two:.6f} (expected {expected:.6f})")


class _FailingDistance:
    """A geodesic distance area that cannot measure one segment (as an invalid transform would)."""

    def __init__(self, bad_lon):
        self._da = make_distance_area(WGS, CTX)
        self._bad = bad_lon

    def __getattr__(self, name):
        return getattr(self._da, name)

    def measureLine(self, a, b):  # noqa: N802 - the QgsDistanceArea name
        if abs(a.x() - self._bad) < 1e-9:
            raise RuntimeError("cannot measure")
        return self._da.measureLine(a, b)


def test_unmeasurable_segment_is_never_0_m() -> bool:
    """A segment that cannot be measured stops the Depth Profile and Explorer chainage, and is
    flagged by RouteFrame, instead of counting as 0 m (or a planar length)."""
    from ..explorer.processing_ops import cumulative_m

    points = [QgsPointXY(0.1 * i, 50.0) for i in range(6)]
    bad = _FailingDistance(points[2].x())
    station, status = dpc.route_from_points(points[:2], WGS, make_distance_area(WGS, CTX))
    stopped_profile = False
    try:
        dpc.RouteStationing([points], 30000.0, WGS, bad)
    except ValueError as exc:
        stopped_profile = "segment 3" in str(exc)
    stopped_listing = False
    try:
        cumulative_m([(p.x(), p.y(), i) for i, p in enumerate(points)], bad)
    except ValueError as exc:
        stopped_listing = "record 2" in str(exc)
    frame = RouteFrame([QgsGeometry.fromPolylineXY(points)], [0.0], bad)
    frame_kp = frame.kp_at_point(points[1]).kp_km
    notes = frame.kp_basis_notes()
    flagged = any(level == "warn" and "could not be measured" in text for level, text in notes)
    ok = station is not None and status is None and stopped_profile and stopped_listing and flagged \
        and abs(frame_kp - REF.inv((0.0, 50.0), (0.1, 50.0))[1] / 1000.0) < 1e-6
    return _report("Unmeasurable segment: profile and listing stop, RouteFrame flags it", ok,
                   f"profile stopped {stopped_profile}, listing stopped {stopped_listing}, flagged {flagged}")


def run_all() -> List[bool]:
    results = [
        test_geodesic_kp_long_route(),
        test_grid_kp_in_one_zone(),
        test_grid_kp_outside_its_zone_is_flagged(),
        test_antimeridian_refused(),
        test_workbench_edit_keeps_earlier_kps(),
        test_stated_kp_check_at_import(),
        test_workbench_position_on_geodesic(),
        test_layer_route_in_seqno_order(),
        test_unmeasurable_segment_is_never_0_m(),
    ]
    print(f"\n{sum(results)}/{len(results)} KP method checks passed.")
    return results


if __name__ == "__main__":
    run_all()
