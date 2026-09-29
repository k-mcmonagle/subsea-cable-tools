"""End-to-end checks for the Calculate Seabed Length algorithm.

Runs against a synthetic planar-slope GeoTIFF so the 3D length has a known
closed form: for a straight route on a constant slope m (dz per metre of
plan distance), seabed_length = plan_length * sqrt(1 + m^2).

Also: a long, dense route against an independent reference computation
(the per-station re-walk this replaced was O(stations x vertices)); contour
mode with PointZ crossings (3D contours used to be dropped) and with the
contours in another CRS (they used to be intersected unprojected); and a
multi-part route, whose gap between parts is not seabed.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import bisect
import math
import os
import struct
import tempfile
import time
from typing import List

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineString,
    QgsPoint,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from ..kp_range_utils import make_distance_area
from ..qgis_compat import FIELD_TYPE_DOUBLE
from ..processing.seabed_length_algorithm import SeabedLengthAlgorithm


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


_SLOPE = 0.05  # dz per metre northing
_DEPTH0 = 100.0
_X0, _Y0 = 500000.0, 4000000.0
_ROUTE_LEN = 2000.0  # metres of planar northing


_TEMP_DIR = []


def _temp_path(name: str) -> str:
    """A path in this process's own temp folder.

    A fixed name in the shared temp folder clashed with other test processes
    (and with layers of earlier tests) holding the same GeoTIFF open, which
    Windows refuses to overwrite.
    """
    if not _TEMP_DIR:
        _TEMP_DIR.append(tempfile.mkdtemp(prefix="sct_seabed_"))
    return os.path.join(_TEMP_DIR[0], name)


def _make_slope_raster() -> str:
    """Write a GeoTIFF where depth = _DEPTH0 + _SLOPE * (y - _Y0) (once per run)."""
    from osgeo import gdal, osr

    path = _temp_path("slope_bathy.tif")
    if os.path.exists(path):
        return path
    pixel = 10.0
    pad = 200.0
    width = int((2 * pad + 200.0) / pixel)            # 200 m wide strip
    height = int((2 * pad + _ROUTE_LEN) / pixel)
    origin_x = _X0 - pad - 100.0
    origin_y = _Y0 + _ROUTE_LEN + pad                  # top edge (north)

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, width, height, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([origin_x, pixel, 0.0, origin_y, 0.0, -pixel])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32631)
    ds.SetProjection(srs.ExportToWkt())

    import struct
    band = ds.GetRasterBand(1)
    for row in range(height):
        y_centre = origin_y - (row + 0.5) * pixel
        depth = _DEPTH0 + _SLOPE * (y_centre - _Y0)
        band.WriteRaster(0, row, width, 1, struct.pack("f", depth) * width)
    band.FlushCache()
    ds = None
    return path


def _make_route_layer() -> QgsVectorLayer:
    layer = QgsVectorLayer("LineString?crs=EPSG:32631", "route", "memory")
    f = QgsFeature()
    f.setGeometry(
        QgsGeometry.fromPolylineXY(
            [QgsPointXY(_X0, _Y0), QgsPointXY(_X0, _Y0 + _ROUTE_LEN)]
        )
    )
    layer.dataProvider().addFeatures([f])
    return layer


def _run_algorithm(params_extra: dict) -> List[QgsFeature]:
    raster_path = _make_slope_raster()
    raster = QgsRasterLayer(raster_path, "bathy")
    assert raster.isValid(), "synthetic raster failed to load"
    route = _make_route_layer()

    alg = SeabedLengthAlgorithm()
    alg.initAlgorithm()
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()

    params = {
        "INPUT_LINE": route,
        "BATHY_TYPE": 0,
        "INPUT_RASTER": raster,
        "SAMPLING_INTERVAL": 10,
        "SENSITIVITY_ANALYSIS": False,
        "SENSITIVITY_INTERVALS": "1,5,10",
        "OUTPUT_INTERVALS": False,
        "KP_INTERVAL": 1,
        "OUTPUT": "memory:",
    }
    params.update(params_extra)
    results = alg.processAlgorithm(params, context, feedback)
    out_layer = context.getMapLayer(results["OUTPUT"])
    if out_layer is None:
        from qgis.core import QgsProcessingUtils

        out_layer = QgsProcessingUtils.mapLayerFromString(results["OUTPUT"], context)
    assert out_layer is not None, "no output layer"
    return list(out_layer.getFeatures())


def test_planar_slope_matches_closed_form() -> bool:
    """seabed_length must be ~ plan_length * sqrt(1 + m^2) on a planar slope."""
    try:
        feats = _run_algorithm({})
    except Exception as exc:
        return _result("seabed length planar slope", False, repr(exc))
    if not feats:
        return _result("seabed length planar slope", False, "no output features")
    f = feats[0]
    plan = float(f["plan_length_m"])
    seabed = float(f["seabed_length_m"])
    ratio = float(f["elongation_ratio"])
    # The slope is defined per planar metre; ellipsoidal plan metres differ by
    # the UTM scale factor (~0.9996), so allow a slightly loose tolerance.
    expected_ratio = math.sqrt(1.0 + _SLOPE * _SLOPE)
    ok = (
        abs(plan - _ROUTE_LEN) < 0.5 * _ROUTE_LEN * 1e-2
        and abs(ratio - expected_ratio) < 5e-4
        and abs(seabed - plan * expected_ratio) < plan * 5e-4
    )
    return _result(
        "seabed length planar slope",
        ok,
        f"plan={plan:.2f} seabed={seabed:.2f} ratio={ratio:.6f} expected≈{expected_ratio:.6f}",
    )


def test_kp_interval_output_mode_runs() -> bool:
    """Regression: KP-interval mode used to crash because a duplicated sink
    was created without the kp_start/kp_end fields."""
    try:
        feats = _run_algorithm({"OUTPUT_INTERVALS": True, "KP_INTERVAL": 1})
    except Exception as exc:
        return _result("seabed length KP-interval mode", False, repr(exc))
    # 2 km planar route at 1 km intervals -> 2 full rows, plus possibly a tiny
    # trailing sliver because the ellipsoidal plan length of a 2000 m planar
    # UTM line is slightly over 2000 m (scale factor).
    ok = len(feats) in (2, 3)
    detail = f"rows={len(feats)}"
    if ok and len(feats) == 3:
        sliver = float(feats[2]["segment_length_m"])
        ok = sliver < 5.0
        detail += f" sliver={sliver:.2f} m"
    if ok:
        f0 = feats[0]
        names = [fld.name() for fld in f0.fields()]
        ok = "kp_start" in names and "kp_end" in names
        if ok:
            seg_plan = float(f0["segment_length_m"])
            seg_seabed = float(f0["seabed_segment_length_m"])
            expected_ratio = math.sqrt(1.0 + _SLOPE * _SLOPE)
            ok = (
                abs(seg_plan - 1000.0) < 10.0
                and abs(seg_seabed / seg_plan - expected_ratio) < 1e-3
            )
            detail += f" seg_plan={seg_plan:.2f} seg_ratio={seg_seabed / seg_plan:.6f}"
    return _result("seabed length KP-interval mode", ok, detail)


def _run_on(route: QgsVectorLayer, params_extra: dict) -> List[QgsFeature]:
    alg = SeabedLengthAlgorithm()
    alg.initAlgorithm()
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    params = {
        "INPUT_LINE": route,
        "BATHY_TYPE": 0,
        "SAMPLING_INTERVAL": 10,
        "SENSITIVITY_ANALYSIS": False,
        "SENSITIVITY_INTERVALS": "1,5,10",
        "OUTPUT_INTERVALS": False,
        "KP_INTERVAL": 1,
        "OUTPUT": "memory:",
    }
    params.update(params_extra)
    results = alg.processAlgorithm(params, context, QgsProcessingFeedback())
    return list(context.getMapLayer(results["OUTPUT"]).getFeatures())


# --- long route ------------------------------------------------------------

_LONG_KM = 60.0
_LONG_PIXEL = 50.0


def _long_depth(x, y):
    return 1500.0 + 40.0 * math.sin((x - _X0) / 700.0) + 25.0 * math.cos((y - _Y0) / 450.0)


def _make_long_raster() -> str:
    from osgeo import gdal, osr

    path = _temp_path("long_bathy.tif")
    x_min, y_max = _X0 - 1000.0, _Y0 + 3000.0
    width = int((_LONG_KM * 1000.0 + 2000.0) / _LONG_PIXEL)
    height = int(6000.0 / _LONG_PIXEL)
    ds = gdal.GetDriverByName("GTiff").Create(path, width, height, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([x_min, _LONG_PIXEL, 0.0, y_max, 0.0, -_LONG_PIXEL])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32631)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    for row in range(height):
        yc = y_max - (row + 0.5) * _LONG_PIXEL
        values = [_long_depth(x_min + (col + 0.5) * _LONG_PIXEL, yc) for col in range(width)]
        band.WriteRaster(0, row, width, 1, struct.pack(f"{width}f", *values))
    band.FlushCache()
    ds = None
    return path


def _reference_seabed_length(points, raster, interval_m) -> float:
    """Independent re-implementation of the documented method.

    Stations every ``interval_m`` of geodesic chainage from the start plus
    the last vertex, each interpolated along its stored segment; depth from
    band 1; seabed = sum of sqrt(chord^2 + dz^2) between valid stations.
    """
    distance = make_distance_area(QgsCoordinateReferenceSystem("EPSG:32631"))
    cumulative = [0.0]
    for a, b in zip(points[:-1], points[1:]):
        cumulative.append(cumulative[-1] + distance.measureLine(a, b))
    total = cumulative[-1]
    provider = raster.dataProvider()

    def depth(pt):
        value, ok = provider.sample(pt, 1)
        return float(value) if ok else None

    stations = []
    k = 0
    while k * interval_m <= total:
        d = k * interval_m
        j = max(1, bisect.bisect_left(cumulative, d))
        seg = cumulative[j] - cumulative[j - 1]
        r = (d - cumulative[j - 1]) / seg
        a, b = points[j - 1], points[j]
        stations.append(QgsPointXY(a.x() + r * (b.x() - a.x()), a.y() + r * (b.y() - a.y())))
        k += 1
    if stations[-1] != points[-1]:
        stations.append(points[-1])
    samples = [(p, depth(p)) for p in stations]
    valid = [(p, z) for p, z in samples if z is not None]
    return sum(math.hypot(distance.measureLine(p0, p1), z1 - z0)
               for (p0, z0), (p1, z1) in zip(valid[:-1], valid[1:]))


def test_long_route_matches_reference() -> bool:
    raster = QgsRasterLayer(_make_long_raster(), "long_bathy")
    if not raster.isValid():
        return _result("seabed length: long route vs reference", False, "raster failed to load")
    # 60 km zig-zag route, one vertex every 100 m (601 vertices).
    points = [QgsPointXY(_X0 + i * 100.0, _Y0 + 120.0 * math.sin(i / 7.0))
              for i in range(int(_LONG_KM * 10) + 1)]
    route = QgsVectorLayer("LineString?crs=EPSG:32631", "long_route", "memory")
    feat = QgsFeature()
    feat.setGeometry(QgsGeometry.fromPolylineXY(points))
    route.dataProvider().addFeatures([feat])

    started = time.perf_counter()
    feats = _run_on(route, {"INPUT_RASTER": raster, "SAMPLING_INTERVAL": 10})
    elapsed = time.perf_counter() - started
    expected = _reference_seabed_length(points, raster, 10)
    got = float(feats[0]["seabed_length_m"]) if feats else float("nan")
    ok = bool(feats) and abs(got - expected) <= 1e-6 * expected
    return _result("seabed length: 60 km, 601-vertex route matches reference computation", ok,
                   f"seabed={got:.3f} reference={expected:.3f} ({elapsed:.2f} s)")


# --- contour mode ----------------------------------------------------------

_ZIGZAG_DZ = 20.0


def _contour_layer(crs: str = "EPSG:32631", z: bool = False) -> QgsVectorLayer:
    """East-west contours every 100 m of northing, depth alternating 100/120 m.

    The alternating depth makes the seabed length depend on every crossing:
    dropping them (as the old code did for PointZ intersections) collapses
    it to the plan length.
    """
    kind = "LineStringZ" if z else "LineString"
    layer = QgsVectorLayer(f"{kind}?crs={crs}", "contours", "memory")
    layer.dataProvider().addAttributes([QgsField("depth", FIELD_TYPE_DOUBLE)])
    layer.updateFields()
    to_crs = None
    if crs != "EPSG:32631":
        to_crs = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:32631"),
                                        QgsCoordinateReferenceSystem(crs), QgsProject.instance())
    feats = []
    for k in range(0, 21):
        y = _Y0 + k * 100.0
        depth = 100.0 + (_ZIGZAG_DZ if k % 2 else 0.0)
        xy = [QgsPointXY(_X0 - 500.0 + j * 100.0, y) for j in range(11)]
        if to_crs is not None:
            xy = [to_crs.transform(p) for p in xy]
        feat = QgsFeature(layer.fields())
        if z:
            feat.setGeometry(QgsGeometry(QgsLineString([QgsPoint(p.x(), p.y(), -depth) for p in xy])))
        else:
            feat.setGeometry(QgsGeometry.fromPolylineXY(xy))
        feat.setAttributes([depth])
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    return layer


def _contour_run(contours) -> float:
    feats = _run_on(_make_route_layer(), {
        "BATHY_TYPE": 1, "INPUT_CONTOURS": contours, "DEPTH_FIELD": "depth"})
    return float(feats[0]["seabed_length_m"]) if feats else float("nan")


def test_contour_modes() -> bool:
    """2D, 3D (PointZ crossings) and other-CRS contours give the same length."""
    plain = _contour_run(_contour_layer())
    three_d = _contour_run(_contour_layer(z=True))
    other_crs = _contour_run(_contour_layer(crs="EPSG:4326"))
    # 20 x 100 m steps each rising/falling 20 m (plan metres are geodesic,
    # ~0.04% longer than UTM grid metres).
    expected = 20 * math.hypot(100.0 * 1.0004, _ZIGZAG_DZ)
    ok = (abs(plain - expected) < 1.0
          and abs(three_d - plain) < 1e-6
          and abs(other_crs - plain) < 1e-3 * plain)
    return _result("seabed length: contours 2D / PointZ / other CRS agree", ok,
                   f"2D={plain:.3f} Z={three_d:.3f} EPSG:4326={other_crs:.3f} expected~{expected:.1f}")


def test_multipart_route_skips_gap() -> bool:
    """Two disjoint parts: seabed length is the sum of the parts, not the gap."""
    raster = QgsRasterLayer(_make_slope_raster(), "bathy")
    route = QgsVectorLayer("LineString?crs=EPSG:32631", "two_parts", "memory")
    parts = [
        [QgsPointXY(_X0, _Y0), QgsPointXY(_X0, _Y0 + 800.0)],
        [QgsPointXY(_X0 + 50.0, _Y0 + 1200.0), QgsPointXY(_X0 + 50.0, _Y0 + 2000.0)],
    ]
    feats = []
    for part in parts:
        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPolylineXY(part))
        feats.append(feat)
    route.dataProvider().addFeatures(feats)
    out = _run_on(route, {"INPUT_RASTER": raster})
    ok = bool(out)
    detail = ""
    if ok:
        plan = float(out[0]["plan_length_m"])
        seabed = float(out[0]["seabed_length_m"])
        expected_ratio = math.sqrt(1.0 + _SLOPE * _SLOPE)
        ok = abs(plan - 1600.0) < 2.0 and abs(seabed / plan - expected_ratio) < 5e-4
        detail = f"plan={plan:.2f} seabed={seabed:.2f} ratio={seabed / plan:.6f}"
    return _result("seabed length: multi-part route excludes the gap", ok, detail)


def run_all() -> List[bool]:
    results = [
        test_planar_slope_matches_closed_form(),
        test_kp_interval_output_mode_runs(),
        test_long_route_matches_reference(),
        test_contour_modes(),
        test_multipart_route_skips_gap(),
    ]
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()
