"""End-to-end checks for the Calculate Seabed Length algorithm.

Runs against a synthetic planar-slope GeoTIFF so the 3D length has a known
closed form: for a straight route on a constant slope m (dz per metre of
plan distance), seabed_length = plan_length * sqrt(1 + m^2).

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import math
import os
import tempfile
from typing import List

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

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


def _make_slope_raster() -> str:
    """Write a GeoTIFF where depth = _DEPTH0 + _SLOPE * (y - _Y0)."""
    from osgeo import gdal, osr

    path = os.path.join(tempfile.gettempdir(), "sct_test_slope_bathy.tif")
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


def run_all() -> List[bool]:
    results = [
        test_planar_slope_matches_closed_form(),
        test_kp_interval_output_mode_runs(),
    ]
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()
