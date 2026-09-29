"""Smoke / round-trip checks for the v1.6 ``kp_geo_utils`` module.

Mirrors the manual-runner pattern of ``test_distance_round_trip``: tests are
intended to be run from the QGIS Python console where the QGIS API is
available. There is no CI integration.

The plugin folder name contains hyphens (``subsea-cable-tools``) which Python
cannot import directly, so paste this runner into the QGIS Python console::

    import importlib.util, sys
    from pathlib import Path
    pkg_dir = Path(r'C:/Users/<you>/AppData/Roaming/QGIS/QGIS3/profiles/default/python/plugins/subsea-cable-tools')
    # Register the plugin folder under an importable alias so relative imports work.
    spec = importlib.util.spec_from_file_location(
        'subsea_cable_tools', pkg_dir / '__init__.py',
        submodule_search_locations=[str(pkg_dir)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules['subsea_cable_tools'] = pkg
    spec.loader.exec_module(pkg)
    from subsea_cable_tools.tests import test_kp_geo_utils
    test_kp_geo_utils.run_all()

Each check prints PASS / FAIL and returns ``True`` / ``False``.
"""

from __future__ import annotations

from typing import List

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)

from ..kp_geo_utils import (
    RouteFrame,
    extract_line_segment,
    iter_line_parts,
    kp_at_point,
    measure_total_length_m,
    point_at_kp,
    reproject_geoms_to,
)
from ..kp_range_utils import make_distance_area


# A simple ~111 km north-south line at the equator (lat 0..1, lon 0).
_GEOG_SINGLE = "LINESTRING(0 0, 0 1)"
# Two-feature route along the equator, lon 0..0.5 then 0.5..1, total ~111 km.
_GEOG_F1 = "LINESTRING(0 0, 0.5 0)"
_GEOG_F2 = "LINESTRING(0.5 0, 1 0)"
# Multi-part single feature.
_GEOG_MULTI = "MULTILINESTRING((0 0, 0.5 0),(0.5 0, 1 0))"


def _line(wkt: str) -> QgsGeometry:
    return QgsGeometry.fromWkt(wkt)


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _da_geog():
    return make_distance_area(
        QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateTransformContext()
    )


def _da_proj(epsg: int = 32631):
    return make_distance_area(
        QgsCoordinateReferenceSystem(f"EPSG:{epsg}"), QgsCoordinateTransformContext()
    )


# ---------------------------------------------------------------------------
# Single-geometry primitives
# ---------------------------------------------------------------------------


def test_iter_line_parts_single_and_multi() -> bool:
    single = iter_line_parts(_line(_GEOG_SINGLE))
    multi = iter_line_parts(_line(_GEOG_MULTI))
    ok = len(single) == 1 and len(multi) == 2
    return _result(
        "iter_line_parts single vs multipart", ok, f"single={len(single)} multi={len(multi)}"
    )


def test_point_at_kp_midpoint_single() -> bool:
    """Midpoint of the geographic line should be near (0, 0.5)."""

    geom = _line(_GEOG_SINGLE)
    da = _da_geog()
    total_m = measure_total_length_m(geom, da)
    pt = point_at_kp(geom, total_m / 2000.0, da)
    ok = pt is not None and abs(pt.x() - 0.0) < 1e-6 and abs(pt.y() - 0.5) < 1e-3
    return _result(
        "point_at_kp midpoint (single)",
        ok,
        f"pt={pt and (pt.x(), pt.y())} total_km={total_m/1000.0:.3f}",
    )


def test_round_trip_point_kp_point() -> bool:
    """``point_at_kp(kp_at_point(p)) ≈ p`` for a point on the line."""

    geom = _line(_GEOG_SINGLE)
    da = _da_geog()
    target = QgsPointXY(0.0, 0.42)
    hit = kp_at_point(geom, target, da)
    pt = point_at_kp(geom, hit.kp_km, da)
    ok = (
        pt is not None
        and abs(pt.x() - target.x()) < 1e-6
        and abs(pt.y() - target.y()) < 1e-3
        and hit.dcc_m < 1.0
    )
    return _result(
        "round-trip point→kp→point",
        ok,
        f"kp={hit.kp_km:.6f} dcc={hit.dcc_m:.3f} pt={pt and (pt.x(), pt.y())}",
    )


def test_out_of_range_returns_none_by_default() -> bool:
    geom = _line(_GEOG_SINGLE)
    da = _da_geog()
    total_km = measure_total_length_m(geom, da) / 1000.0
    pt_over = point_at_kp(geom, total_km + 100.0, da)
    pt_neg = point_at_kp(geom, -1.0, da)
    ok = pt_over is None and pt_neg is None
    return _result("out-of-range returns None", ok)


def test_clamp_returns_endpoints() -> bool:
    geom = _line(_GEOG_SINGLE)
    da = _da_geog()
    total_km = measure_total_length_m(geom, da) / 1000.0
    pt_over = point_at_kp(geom, total_km + 100.0, da, clamp=True)
    pt_neg = point_at_kp(geom, -1.0, da, clamp=True)
    ok = (
        pt_over is not None
        and abs(pt_over.y() - 1.0) < 1e-9
        and pt_neg is not None
        and abs(pt_neg.y() - 0.0) < 1e-9
    )
    return _result(
        "clamp=True returns endpoints",
        ok,
        f"over={pt_over and (pt_over.x(), pt_over.y())} neg={pt_neg and (pt_neg.x(), pt_neg.y())}",
    )


# ---------------------------------------------------------------------------
# Multi-feature continuity
# ---------------------------------------------------------------------------


def test_multi_feature_continuous_kp() -> bool:
    """KP at the join of two features must equal the length of feature 1."""

    geoms = [_line(_GEOG_F1), _line(_GEOG_F2)]
    da = _da_geog()
    f1_km = measure_total_length_m(geoms[0], da) / 1000.0
    pt = point_at_kp(geoms, f1_km, da)
    ok = pt is not None and abs(pt.x() - 0.5) < 1e-6 and abs(pt.y() - 0.0) < 1e-9
    return _result(
        "multi-feature KP continuity at join",
        ok,
        f"join_kp={f1_km:.6f} pt={pt and (pt.x(), pt.y())}",
    )


def test_multipart_matches_multi_feature() -> bool:
    """A multipart geometry should give the same KP→point as two equivalent features."""

    da = _da_geog()
    multi_pt = point_at_kp(_line(_GEOG_MULTI), 30.0, da)
    feat_pt = point_at_kp([_line(_GEOG_F1), _line(_GEOG_F2)], 30.0, da)
    ok = (
        multi_pt is not None
        and feat_pt is not None
        and abs(multi_pt.x() - feat_pt.x()) < 1e-9
        and abs(multi_pt.y() - feat_pt.y()) < 1e-9
    )
    return _result(
        "multipart KP matches multi-feature KP",
        ok,
        f"multi={multi_pt and (multi_pt.x(), multi_pt.y())} feat={feat_pt and (feat_pt.x(), feat_pt.y())}",
    )


# ---------------------------------------------------------------------------
# CRS handling
# ---------------------------------------------------------------------------


def test_ellipsoidal_vs_cartesian_on_projected_crs() -> bool:
    """For a short projected line, ellipsoidal and cartesian midpoints should agree."""

    geog_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    proj_crs = QgsCoordinateReferenceSystem("EPSG:32631")
    xform = QgsCoordinateTransform(geog_crs, proj_crs, QgsProject.instance())
    geom = _line(_GEOG_SINGLE)
    geom.transform(xform)

    ctx = QgsCoordinateTransformContext()
    da_ell = make_distance_area(proj_crs, ctx, mode="ellipsoidal")
    da_car = make_distance_area(proj_crs, ctx, mode="cartesian")

    total_ell_km = measure_total_length_m(geom, da_ell) / 1000.0
    total_car_km = measure_total_length_m(geom, da_car) / 1000.0

    pt_ell = point_at_kp(geom, total_ell_km / 2.0, da_ell)
    pt_car = point_at_kp(geom, total_car_km / 2.0, da_car)
    ok = (
        pt_ell is not None
        and pt_car is not None
        and abs(pt_ell.x() - pt_car.x()) < 5.0
        and abs(pt_ell.y() - pt_car.y()) < 5.0
    )
    return _result(
        "ellipsoidal vs cartesian midpoint parity (projected)",
        ok,
        f"ell={pt_ell and (round(pt_ell.x(), 3), round(pt_ell.y(), 3))} car={pt_car and (round(pt_car.x(), 3), round(pt_car.y(), 3))}",
    )


def test_reproject_geoms_to_changes_coords() -> bool:
    geog_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    proj_crs = QgsCoordinateReferenceSystem("EPSG:32631")
    out = list(reproject_geoms_to([_line(_GEOG_SINGLE)], geog_crs, proj_crs))
    ok = len(out) == 1 and out[0].asPolyline()[0].x() > 100000.0
    return _result(
        "reproject_geoms_to 4326→32631 changes coords",
        ok,
        f"first_x={out[0].asPolyline()[0].x() if out else None}",
    )


def test_reproject_geoms_to_passthrough_when_same_crs() -> bool:
    crs = QgsCoordinateReferenceSystem("EPSG:4326")
    src = _line(_GEOG_SINGLE)
    out = list(reproject_geoms_to([src], crs, crs))
    ok = len(out) == 1 and out[0].equals(src)
    return _result("reproject_geoms_to is a no-op for same CRS", ok)


# ---------------------------------------------------------------------------
# extract_line_segment
# ---------------------------------------------------------------------------


def test_extract_line_segment_basic() -> bool:
    geom = _line(_GEOG_SINGLE)
    da = _da_geog()
    total_km = measure_total_length_m(geom, da) / 1000.0
    sub = extract_line_segment(geom, total_km * 0.25, total_km * 0.75, da)
    if sub is None:
        return _result("extract_line_segment basic", False, "returned None")
    sub_len_km = measure_total_length_m(sub, da) / 1000.0
    ok = abs(sub_len_km - total_km * 0.5) < 0.01
    return _result(
        "extract_line_segment basic",
        ok,
        f"sub_km={sub_len_km:.6f} expected≈{total_km * 0.5:.6f}",
    )


def test_extract_line_segment_out_of_range() -> bool:
    geom = _line(_GEOG_SINGLE)
    da = _da_geog()
    total_km = measure_total_length_m(geom, da) / 1000.0
    sub = extract_line_segment(geom, total_km + 10.0, total_km + 20.0, da)
    return _result("extract_line_segment out of range returns None", sub is None)


# ---------------------------------------------------------------------------
# RouteFrame
# ---------------------------------------------------------------------------


def test_routeframe_total_length_and_extract() -> bool:
    geoms = [_line(_GEOG_F1), _line(_GEOG_F2)]
    da = _da_geog()
    rf = RouteFrame(geoms, [measure_total_length_m(g, da) for g in geoms], da)

    total_km = rf.total_length_km
    pt = rf.point_at_kp(total_km / 2.0)
    sub = rf.extract_segment(total_km * 0.25, total_km * 0.75)
    if pt is None or sub is None:
        return _result("RouteFrame point_at_kp and extract_segment", False, "None returned")
    sub_km = measure_total_length_m(sub, da) / 1000.0
    ok = (
        abs(pt.x() - 0.5) < 1e-6
        and abs(pt.y() - 0.0) < 1e-9
        and abs(sub_km - total_km * 0.5) < 0.01
    )
    return _result(
        "RouteFrame point_at_kp and extract_segment",
        ok,
        f"pt={(pt.x(), pt.y())} sub_km={sub_km:.6f} expected≈{total_km * 0.5:.6f}",
    )


def test_routeframe_extract_matches_walking_slice() -> bool:
    """Indexed ``RouteFrame.extract_segment`` reproduces the vertex walk.

    Regression for the chainage-indexed slice: same interpolated ends and
    the same interior vertices as ``extract_line_segment`` on a dense
    single-feature route, including a range that ends exactly on a vertex
    and a range that starts at KP 0.
    """
    import math

    da = _da_geog()
    # 200-vertex zig-zag so segments differ in length and bearing.
    points = [f"{i * 0.01} {0.002 * (i % 3)}" for i in range(200)]
    geom = _line("LINESTRING(" + ", ".join(points) + ")")
    rf = RouteFrame([geom], [measure_total_length_m(geom, da)], da)
    total_km = rf.total_length_km
    # KP of vertex 50 (exact chainage), for the on-vertex end case.
    vertex_kp = sum(float(da.measureLine(QgsPointXY(float(points[i].split()[0]),
                                                    float(points[i].split()[1])),
                                         QgsPointXY(float(points[i + 1].split()[0]),
                                                    float(points[i + 1].split()[1]))))
                    for i in range(50)) / 1000.0
    ok = True
    detail = []
    for start_km, end_km in ((total_km * 0.31, total_km * 0.77),
                             (0.0, total_km * 0.2),
                             (total_km * 0.1, vertex_kp),
                             (total_km * 0.9, total_km)):
        indexed = rf.extract_segment(start_km, end_km)
        walked = extract_line_segment(geom, start_km, end_km, da)
        if indexed is None or walked is None:
            ok = False
            detail.append(f"None for {start_km:.3f}-{end_km:.3f}")
            continue
        a = [tuple(p) for p in iter_line_parts(indexed)[0]]
        b = [tuple(p) for p in iter_line_parts(walked)[0]]
        # The walk may duplicate a vertex when a range ends exactly on it.
        dedup = []
        for pt in b:
            if not dedup or (abs(pt[0] - dedup[-1][0]) > 1e-12
                             or abs(pt[1] - dedup[-1][1]) > 1e-12):
                dedup.append(pt)
        same = len(a) == len(dedup) and all(
            math.hypot(pa[0] - pb[0], pa[1] - pb[1]) < 1e-9
            for pa, pb in zip(a, dedup))
        if not same:
            ok = False
            detail.append(f"{start_km:.3f}-{end_km:.3f}: {len(a)} vs "
                          f"{len(dedup)} vertices")
    # Ends coincide with point_at_kp (shared chainage index).
    sub = rf.extract_segment(total_km * 0.4, total_km * 0.6)
    pts = iter_line_parts(sub)[0]
    p_start = rf.point_at_kp(total_km * 0.4)
    p_end = rf.point_at_kp(total_km * 0.6)
    ok = ok and math.hypot(pts[0].x() - p_start.x(),
                           pts[0].y() - p_start.y()) < 1e-9
    ok = ok and math.hypot(pts[-1].x() - p_end.x(),
                           pts[-1].y() - p_end.y()) < 1e-9
    # Degenerate inputs never raise or return a slice.
    ok = ok and rf.extract_segment(float("nan"), 1.0) is None
    ok = ok and rf.extract_segment(2.0, 2.0) is None
    ok = ok and rf.extract_segment(total_km + 1.0, total_km + 2.0) is None
    return _result("RouteFrame.extract_segment == vertex walk (indexed)", ok,
                   "; ".join(detail))


def test_routeframe_extract_multi_feature_gap_keeps_jump_vertex() -> bool:
    """Across a gap between features the slice keeps both gap vertices."""
    da = _da_geog()
    geoms = [_line("LINESTRING(0 0, 0.5 0)"), _line("LINESTRING(0.6 0, 1 0)")]
    rf = RouteFrame(geoms, [measure_total_length_m(g, da) for g in geoms], da)
    total_km = rf.total_length_km
    sub = rf.extract_segment(total_km * 0.1, total_km * 0.9)
    pts = [(round(p.x(), 9), round(p.y(), 9)) for p in iter_line_parts(sub)[0]]
    ok = (0.5, 0.0) in pts and (0.6, 0.0) in pts
    sub_km = measure_total_length_m(sub, da) / 1000.0
    # Chainage does not count the gap, but the slice geometry spans it.
    ok = ok and sub_km > total_km * 0.8
    return _result("RouteFrame.extract_segment keeps feature-gap vertices",
                   ok, f"vertices={pts}")


# ---------------------------------------------------------------------------
# Regression: RPLComparator no longer silently falls back to planar metres
# when the project ellipsoid is unset (1.6 fix).
# ---------------------------------------------------------------------------


def test_rplcomparator_ellipsoid_fallback() -> bool:
    """When the project ellipsoid is unset, RPLComparator must still measure
    ellipsoidally (via the make_distance_area WGS84 fallback). Pre-1.6, the
    distance calculator silently degraded to planar metres, returning degrees
    on a geographic CRS.
    """

    from qgis.core import QgsVectorLayer, QgsFeature

    from ..processing.rpl_comparison_utils import RPLComparator

    project = QgsProject.instance()
    saved_ellipsoid = project.ellipsoid()

    class _Ctx:
        def __init__(self, project):
            self._project = project

        def project(self):
            return self._project

        def transformContext(self):
            return QgsCoordinateTransformContext()

    layer = QgsVectorLayer("LineString?crs=EPSG:4326", "rpl", "memory")
    f = QgsFeature()
    f.setGeometry(_line(_GEOG_SINGLE))
    layer.dataProvider().addFeatures([f])

    try:
        project.setEllipsoid("")
        comparator = RPLComparator(layer, layer, layer.crs(), _Ctx(project))
        # Total length must be in metres (~111 km), not degrees (~1).
        ok_total = abs(comparator.total_source_length_m - 111195.0) < 5000.0
        # KP at midpoint must be in km (~55), not in fractions of a degree.
        kp_mid_km = comparator.calculate_kp_to_point(QgsPointXY(0.0, 0.5), source=True)
        ok_kp = abs(kp_mid_km - 55.5) < 1.0
        ok = ok_total and ok_kp
        return _result(
            "RPLComparator ellipsoid fallback (no project ellipsoid)",
            ok,
            f"total_m={comparator.total_source_length_m:.1f} kp_mid_km={kp_mid_km:.3f}",
        )
    finally:
        project.setEllipsoid(saved_ellipsoid)


def test_geodesic_interpolation_long_geographic_segment() -> bool:
    """Long east-west geographic segment (-30,60)-(30,60).

    Default (``follow_stored_geometry=True``, the plugin's one KP rule): the
    point at mid-KP lies on the segment as drawn, (0, 60), and KP -> point
    -> KP round-trips exactly. Explicit ``follow_stored_geometry=False``
    still follows the geodesic, which bows north to ~(0, 62.6).
    """

    geom = _line("LINESTRING(-30 60, 30 60)")
    da = _da_geog()
    total_km = measure_total_length_m(geom, da) / 1000.0
    mid = point_at_kp(geom, total_km / 2.0, da)
    arc = point_at_kp(geom, total_km / 2.0, da, follow_stored_geometry=False)
    if mid is None or arc is None:
        return _result("geodesic interpolation on long geographic segment", False, "None")
    back = kp_at_point(geom, mid, da).kp_km
    ok = abs(mid.x()) < 1e-6 and abs(mid.y() - 60.0) < 1e-6
    ok = ok and abs(back - total_km / 2.0) < 1e-6
    ok = ok and abs(arc.x()) < 1e-3 and arc.y() - 60.0 > 1.0
    return _result(
        "long geographic segment: default follows the drawn line and round-trips; "
        "arc mode still available",
        ok,
        f"mid=({mid.x():.6f}, {mid.y():.6f}) arc_y={arc.y():.3f} round_trip={abs(back - total_km / 2.0) * 1e6:.3f} mm",
    )


def test_geodesic_interpolation_projected_unchanged() -> bool:
    """On a projected metre CRS, interpolation must stay in the segment's
    plane (no spheroid forward-projection) and be KP-consistent.

    With a *cartesian* distance area the planar midpoint is exact. With an
    *ellipsoidal* distance area the point at KP 0.5 sits where the true
    (ellipsoidal) along-track distance is 500 m — at the UTM central
    meridian (scale factor 0.9996) that is planar x ≈ 500499.8, not 500500.
    The KP-consistency invariant is measureLine(start, pt) == 500 m.
    """

    # 1000 m east-west line in EPSG:32631 (UTM 31N, metres).
    geom = QgsGeometry.fromWkt("LINESTRING(500000 4649776, 501000 4649776)")

    # Cartesian: exact planar midpoint.
    da_cart = make_distance_area(
        QgsCoordinateReferenceSystem("EPSG:32631"),
        QgsCoordinateTransformContext(),
        mode="cartesian",
    )
    pt_cart = point_at_kp(geom, 0.5, da_cart)
    ok_cart = (
        pt_cart is not None
        and abs(pt_cart.x() - 500500.0) < 1e-6
        and abs(pt_cart.y() - 4649776.0) < 1e-6
    )

    # Ellipsoidal: point stays on the segment and true distance to it is 500 m.
    da_ell = _da_proj(32631)
    pt_ell = point_at_kp(geom, 0.5, da_ell)
    if pt_ell is None:
        return _result("projected-CRS planar interpolation unchanged", False, "None")
    dist_to_pt = float(da_ell.measureLine(QgsPointXY(500000, 4649776), pt_ell))
    ok_ell = (
        abs(pt_ell.y() - 4649776.0) < 1e-6
        and abs(dist_to_pt - 500.0) < 0.05
        and 500000.0 < pt_ell.x() < 501000.0
    )

    ok = ok_cart and ok_ell
    return _result(
        "projected-CRS planar interpolation unchanged",
        ok,
        f"cart={pt_cart and (pt_cart.x(), pt_cart.y())} "
        f"ell={(pt_ell.x(), pt_ell.y())} ell_dist={dist_to_pt:.3f} m",
    )


class _BrokenSegmentDistance(QgsDistanceArea):
    """Geodesic distance area that cannot measure the segment starting at
    ``bad_x`` (raises, or returns NaN) — a stand-in for a transform failure."""

    def __init__(self, bad_x: float, mode: str):
        super().__init__()
        self.setSourceCrs(QgsCoordinateReferenceSystem("EPSG:4326"),
                          QgsCoordinateTransformContext())
        self.setEllipsoid("WGS84")
        self._bad_x, self._mode = bad_x, mode

    def measureLine(self, *args):  # noqa: N802 (Qt API name)
        if len(args) == 2 and abs(float(args[0].x()) - self._bad_x) < 1e-12:
            if self._mode == "raise":
                raise RuntimeError("segment cannot be transformed")
            return float("nan")
        return super().measureLine(*args)


def test_point_at_kp_unmeasurable_segment_is_not_skipped() -> bool:
    """A segment that cannot be measured used to be skipped (``continue``),
    shifting every later KP onto the wrong segment, and a NaN length made
    every later KP None. Now KPs before it are exact, KPs at/after it are
    None (with or without clamp) and a warning is logged."""
    geom = _line("LINESTRING(0 0, 0.1 0, 0.2 0, 0.3 0)")   # ~11.1 km legs
    good = _da_geog()
    details = []
    ok = True
    for mode in ("raise", "nan"):
        broken = _BrokenSegmentDistance(0.1, mode)
        before = point_at_kp(geom, 5.0, broken)
        expected = point_at_kp(geom, 5.0, good)
        inside = point_at_kp(geom, 15.0, broken)          # on the broken leg
        after = point_at_kp(geom, 25.0, broken)           # beyond it
        clamped = point_at_kp(geom, 99.0, broken, clamp=True)
        case = (before is not None and expected is not None
                and abs(before.x() - expected.x()) < 1e-12
                and inside is None and after is None and clamped is None)
        details.append(f"{mode}: before={before is not None} inside={inside} after={after} clamp={clamped}")
        ok = ok and case
    # The chainage index behaves the same (NaN length: from_source measures
    # the route without raising, the index stops at the broken leg).
    frame = RouteFrame([geom], [0.0], _BrokenSegmentDistance(0.1, "nan"))
    frame_before = frame.point_at_kp(5.0)
    frame_ok = (frame_before is not None and frame.point_at_kp(25.0) is None
                and frame.point_at_kp(99.0, clamp=True) is None)
    details.append(f"RouteFrame ok={frame_ok}")
    return _result("point_at_kp: unmeasurable segment -> None, never a shifted point",
                   ok and frame_ok, "; ".join(details))


def test_nan_kp_returns_none() -> bool:
    """NaN used to bisect to index 0 in RouteFrame and come back as the
    route start; the walking function returned the end with clamp."""
    geom = _line(_GEOG_SINGLE)
    da = _da_geog()
    frame = RouteFrame.from_source([geom], da)
    nan = float("nan")
    ok = (point_at_kp(geom, nan, da) is None and point_at_kp(geom, nan, da, clamp=True) is None
          and frame.point_at_kp(nan) is None and frame.point_at_kp(nan, clamp=True) is None
          and frame.point_at_kp(float("inf"), clamp=True) is not None)
    return _result("NaN KP -> None (inf still clamps)", ok)


def test_routeframe_nearest_is_true_nearest_not_bbox() -> bool:
    """15 long diagonal legs whose bounding boxes all contain the query
    point, plus a short leg 50 m away: a bounding-box index offered only
    the diagonals as its 12 candidates, so kp_at_point snapped ~460 m away.
    The geometry-storing index finds the short leg, like the full walk."""
    crs = QgsCoordinateReferenceSystem("EPSG:32631")
    da = make_distance_area(crs, QgsCoordinateTransformContext(), mode="cartesian")
    x0, y0 = 500000.0, 5000000.0
    geoms = [QgsGeometry.fromPolylineXY([QgsPointXY(x0, y0 - 10 * k),
                                         QgsPointXY(x0 + 1000, y0 + 1000 - 10 * k)])
             for k in range(15)]
    geoms.append(QgsGeometry.fromPolylineXY([QgsPointXY(x0 + 880, y0 + 50),
                                             QgsPointXY(x0 + 920, y0 + 50)]))
    query = QgsPointXY(x0 + 900, y0 + 100)
    frame = RouteFrame.from_source(geoms, da)
    hit = frame.kp_at_point(query)
    walk = kp_at_point(geoms, query, da)
    ok = (hit.snapped_xy is not None and abs(hit.dcc_m - 50.0) < 1e-6
          and abs(hit.kp_km - walk.kp_km) < 1e-9 and hit.feature_index == 15)
    return _result("RouteFrame.kp_at_point: true nearest segment, not bbox nearest", ok,
                   f"dcc={hit.dcc_m:.3f} m (walk {walk.dcc_m:.3f} m) feature={hit.feature_index}")


def test_routeframe_nearest_exact_under_geodesic_metric() -> bool:
    """At 70°N a degree of longitude is ~38 km but a degree of latitude
    ~111 km: 15 short E-W legs just north of the point are nearer in planar
    degrees than the N-S leg to the east, yet geodesically farther. The 12
    planar-nearest candidates alone miss the true nearest; the search must
    widen until nothing unseen can be nearer."""
    da = _da_geog()
    lon0, lat0 = 0.0, 70.0
    geoms = [QgsGeometry.fromPolylineXY([QgsPointXY(lon0 - 0.004, lat0 + dy),
                                         QgsPointXY(lon0 + 0.004, lat0 + dy)])
             for dy in [0.02 + 0.001 * i for i in range(15)]]
    geoms.append(QgsGeometry.fromPolylineXY([QgsPointXY(lon0 + 0.05, lat0 - 0.01),
                                             QgsPointXY(lon0 + 0.05, lat0 + 0.01)]))
    query = QgsPointXY(lon0, lat0)
    frame = RouteFrame.from_source(geoms, da)
    hit = frame.kp_at_point(query)
    walk = kp_at_point(geoms, query, da)
    ok = (hit.feature_index == 15 and abs(hit.dcc_m - walk.dcc_m) < 1e-6
          and abs(hit.kp_km - walk.kp_km) < 1e-9)
    return _result("RouteFrame.kp_at_point exact under the geodesic metric (70°N)", ok,
                   f"dcc={hit.dcc_m:.1f} m (walk {walk.dcc_m:.1f} m) feature={hit.feature_index}")


def test_reproject_context_and_strict() -> bool:
    """An explicit transform context gives the project path's result; an
    untransformable geometry raises with strict=True and is skipped (and
    logged) otherwise."""
    src = QgsCoordinateReferenceSystem("EPSG:4326")
    dst = QgsCoordinateReferenceSystem("EPSG:32631")
    good = _line("LINESTRING(2 50, 3 51)")
    via_project = list(reproject_geoms_to([good], src, dst))
    via_context = list(reproject_geoms_to([good], src, dst,
                                          transform_context=QgsCoordinateTransformContext()))
    same = (len(via_project) == len(via_context) == 1
            and via_project[0].asWkt(3) == via_context[0].asWkt(3))
    bad = _line("LINESTRING(2 50, 3 95)")      # latitude 95: not transformable
    mercator = QgsCoordinateReferenceSystem("EPSG:3857")
    skipped = list(reproject_geoms_to([bad, good], src, mercator))
    # QGIS writes inf for latitude 95 instead of raising: still a failure.
    try:
        list(reproject_geoms_to([bad], src, mercator, strict=True))
        raised = False
    except ValueError:
        raised = True
    ok = same and raised and len(skipped) == 1
    return _result("reproject_geoms_to: transform context, strict raises, default skips", ok,
                   f"same={same} strict_raised={raised} kept={len(skipped)}")


def test_stored_geometry_index_nearest() -> bool:
    """The shared helper ranks by geometry distance on QGIS 3 and 4."""
    from qgis.core import QgsFeature
    from ..kp_geo_utils import stored_geometry_index
    index = stored_geometry_index()
    for fid, wkt in ((1, "LINESTRING(0 0, 1000 1000)"), (2, "LINESTRING(880 50, 920 50)")):
        feat = QgsFeature(fid)
        feat.setGeometry(QgsGeometry.fromWkt(wkt))
        index.addFeature(feat)
    nearest = index.nearestNeighbor(QgsPointXY(900, 100), 1)
    stored = index.geometry(2)
    ok = nearest == [2] and stored is not None and not stored.isEmpty()
    return _result("stored_geometry_index: nearest by geometry, geometries kept", ok, str(nearest))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all() -> List[bool]:
    results = [
        test_iter_line_parts_single_and_multi(),
        test_point_at_kp_midpoint_single(),
        test_round_trip_point_kp_point(),
        test_out_of_range_returns_none_by_default(),
        test_clamp_returns_endpoints(),
        test_multi_feature_continuous_kp(),
        test_multipart_matches_multi_feature(),
        test_ellipsoidal_vs_cartesian_on_projected_crs(),
        test_reproject_geoms_to_changes_coords(),
        test_reproject_geoms_to_passthrough_when_same_crs(),
        test_extract_line_segment_basic(),
        test_extract_line_segment_out_of_range(),
        test_routeframe_total_length_and_extract(),
        test_routeframe_extract_matches_walking_slice(),
        test_routeframe_extract_multi_feature_gap_keeps_jump_vertex(),
        test_rplcomparator_ellipsoid_fallback(),
        test_geodesic_interpolation_long_geographic_segment(),
        test_geodesic_interpolation_projected_unchanged(),
        test_point_at_kp_unmeasurable_segment_is_not_skipped(),
        test_nan_kp_returns_none(),
        test_routeframe_nearest_is_true_nearest_not_bbox(),
        test_routeframe_nearest_exact_under_geodesic_metric(),
        test_reproject_context_and_strict(),
        test_stored_geometry_index_nearest(),
    ]
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results
