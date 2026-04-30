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
    """On a long east-west geographic segment, ``point_at_kp`` must return a
    point on the geodesic — not a planar lon/lat midpoint. The geodesic
    between (-30, 60) and (30, 60) bows northward and has its midpoint near
    (0, 62.6); the old planar code returned exactly (0, 60).
    """

    geom = _line("LINESTRING(-30 60, 30 60)")
    da = _da_geog()
    total_km = measure_total_length_m(geom, da) / 1000.0
    mid = point_at_kp(geom, total_km / 2.0, da)
    if mid is None:
        return _result("geodesic interpolation on long geographic segment", False, "None")

    geodesic_north = mid.y() - 60.0
    ok = abs(mid.x()) < 1e-3 and geodesic_north > 1.0
    return _result(
        "geodesic interpolation on long geographic segment",
        ok,
        f"mid=({mid.x():.6f}, {mid.y():.6f}) geodesic_north_of_60={geodesic_north:.3f}deg",
    )


def test_geodesic_interpolation_projected_unchanged() -> bool:
    """On a projected metre CRS, interpolation must remain exact-planar."""

    # 1000 m east-west line in EPSG:32631 (UTM 31N, metres).
    geom = QgsGeometry.fromWkt("LINESTRING(500000 4649776, 501000 4649776)")
    da = _da_proj(32631)
    pt = point_at_kp(geom, 0.5, da)
    ok = (
        pt is not None
        and abs(pt.x() - 500500.0) < 1e-6
        and abs(pt.y() - 4649776.0) < 1e-6
    )
    return _result(
        "projected-CRS planar interpolation unchanged",
        ok,
        f"pt={pt and (pt.x(), pt.y())}",
    )


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
        test_rplcomparator_ellipsoid_fallback(),
        test_geodesic_interpolation_long_geographic_segment(),
        test_geodesic_interpolation_projected_unchanged(),
    ]
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results
