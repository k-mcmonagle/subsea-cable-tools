"""Minimal smoke / round-trip checks for the v1.5.1 distance-area helper.

These are intentionally light-weight and meant to be run from the QGIS Python
console (so the QGIS API is available). There is no CI integration.

The plugin folder name contains hyphens (``subsea-cable-tools``) which Python
cannot import directly. See ``test_kp_geo_utils`` for a paste-ready runner
snippet that registers the plugin under the importable alias
``subsea_cable_tools`` before importing this module.
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

from ..kp_range_utils import make_distance_area, measure_total_length_m


# A simple ~111 km north-south segment at the equator (lat 0..1, lon 0).
_GEOG_LINE_WKT = "LINESTRING(0 0, 0 1)"


def _line_geographic() -> QgsGeometry:
    return QgsGeometry.fromWkt(_GEOG_LINE_WKT)


def _line_projected(epsg: int) -> QgsGeometry:
    geog_crs = QgsCoordinateReferenceSystem("EPSG:4326")
    proj_crs = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
    xform = QgsCoordinateTransform(geog_crs, proj_crs, QgsProject.instance())
    geom = _line_geographic()
    geom.transform(xform)
    return geom


def _result(name: str, ok: bool, detail: str = "") -> str:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return msg


def test_round_trip_geographic_vs_projected() -> bool:
    """Same geometry in EPSG:4326 vs EPSG:32631 should yield matching ellipsoidal length (within 1 m)."""

    geog = _line_geographic()
    proj = _line_projected(32631)
    ctx = QgsCoordinateTransformContext()

    da_geog = make_distance_area(QgsCoordinateReferenceSystem("EPSG:4326"), ctx)
    da_proj = make_distance_area(QgsCoordinateReferenceSystem("EPSG:32631"), ctx)

    len_geog = measure_total_length_m(geog, da_geog)
    len_proj = measure_total_length_m(proj, da_proj)

    diff = abs(len_geog - len_proj)
    ok = diff < 1.0
    _result(
        "round-trip 4326 vs 32631 ellipsoidal length",
        ok,
        f"geog={len_geog:.3f} m, proj={len_proj:.3f} m, diff={diff:.3f} m",
    )
    return ok


def test_empty_ellipsoid_fallback() -> bool:
    """Helper must fall back to WGS84 when project ellipsoid is unset."""

    project = QgsProject.instance()
    saved = project.ellipsoid()
    try:
        project.setEllipsoid("")
        da = make_distance_area(QgsCoordinateReferenceSystem("EPSG:4326"), project=project)
        ok = bool(da.ellipsoid()) and da.ellipsoid().upper() != "NONE"
        _result("empty-ellipsoid fallback to WGS84", ok, f"da.ellipsoid()={da.ellipsoid()!r}")
        return ok
    finally:
        project.setEllipsoid(saved)


def test_cartesian_on_geographic_rejected() -> bool:
    """Cartesian mode must raise ValueError on a geographic CRS."""

    try:
        make_distance_area(QgsCoordinateReferenceSystem("EPSG:4326"), mode="cartesian")
    except ValueError:
        _result("cartesian on geographic CRS rejected", True)
        return True
    _result("cartesian on geographic CRS rejected", False, "no ValueError raised")
    return False


def test_cartesian_on_projected_ok() -> bool:
    """Cartesian on a projected CRS should give a sensible planar length."""

    proj = _line_projected(32631)
    da = make_distance_area(QgsCoordinateReferenceSystem("EPSG:32631"), mode="cartesian")
    length = float(da.measureLength(proj))
    # A 1-degree N-S step at the equator is ~110.6 km.
    ok = 100_000 < length < 120_000
    _result("cartesian planar length on EPSG:32631", ok, f"length={length:.1f} m")
    return ok


def run_all() -> List[str]:
    failures: List[str] = []
    for fn in (
        test_round_trip_geographic_vs_projected,
        test_empty_ellipsoid_fallback,
        test_cartesian_on_geographic_rejected,
        test_cartesian_on_projected_ok,
    ):
        try:
            if not fn():
                failures.append(fn.__name__)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[ERROR] {fn.__name__}: {exc!r}")
            failures.append(fn.__name__)
    print(f"\n{len(failures)} failure(s)." if failures else "\nAll checks passed.")
    return failures


if __name__ == "__main__":  # pragma: no cover
    run_all()
