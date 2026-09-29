# -*- coding: utf-8 -*-
"""Cross-profile transect sampling checks (requires QGIS, NumPy and GDAL).

``DepthSnapshot.offset_profile_samples`` used to project every transect
sample geodesically and transform it to each raster's CRS one by one (up to
2001 of each per station). It now projects only nodes <= 50 m apart and
interpolates between them in the raster CRS (``transect_points``). These
checks pin the new positions to the old per-sample geodesic ones on a long
geographic route (sub-millimetre, in WGS84, Web Mercator and UTM), and the
sampled cross profiles to the old algorithm on real rasters.
"""

from __future__ import annotations

import math
import os
import tempfile
import time

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
)

from ..bathymetry_sampling import PREFIX
from ..burial import analysis_task
from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..slope_utils import cross_profile_metrics
from ..workbench.depth_service import DepthSourceConfig

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _distance():
    return make_distance_area(WGS84, QgsProject.instance().transformContext())


def _station_frame(route, kp, distance):
    """(center, starboard bearing) exactly as offset_profile_samples."""
    center = route.point_at_kp(kp, clamp=True)
    a = route.point_at_kp(max(route.start_kp_km, kp - .005), clamp=True)
    b = route.point_at_kp(min(route.end_kp_km, kp + .005), clamp=True)
    return center, distance.bearing(a, b) + math.pi / 2


def _old_point(center, bearing, t, distance):
    return distance.computeSpheroidProject(
        center, abs(t), bearing if t >= 0 else bearing + math.pi)


def _utm(lon: float, lat: float) -> QgsCoordinateReferenceSystem:
    zone = min(60, max(1, int((lon + 180.0) // 6.0) + 1))
    return QgsCoordinateReferenceSystem(
        f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")


def test_transect_positions_match_geodesic_projection() -> bool:
    project = QgsProject.instance()
    distance = _distance()
    # ~27,000 km zig-zag from 75°S to 75°N: every latitude band and many
    # bearings, where lon/lat interpolation distortion is largest.
    geom = QgsGeometry.fromWkt(
        "LINESTRING(-10 -75, 20 -30, 5 0, 40 45, 10 75, 60 70)")
    route = RouteFrame.from_source([geom], distance)
    mercator = QgsCoordinateReferenceSystem("EPSG:3857")
    worst = 0.0
    checked = 0
    kp = 1.0
    while kp < route.total_length_km:
        center, bearing = _station_frame(route, kp, distance)
        frames = [(None, None)]
        for crs in (mercator, _utm(center.x(), center.y())):
            frames.append((QgsCoordinateTransform(WGS84, crs, project),
                           QgsCoordinateTransform(crs, WGS84, project)))
        for offset in (20.0, 150.0, 500.0, 1000.0):
            xs = [-offset + j * 2 * offset / 2000 for j in range(2001)]
            probe = list(range(0, 2001, 37)) + [1000, 2000]
            old = {i: _old_point(center, bearing, xs[i], distance)
                   for i in probe}
            for forward, inverse in frames:
                new = analysis_task.transect_points(center, bearing, xs,
                                                    distance, forward)
                for i in probe:
                    point = new[i] if inverse is None \
                        else inverse.transform(new[i])
                    worst = max(worst, distance.measureLine(old[i], point))
                    checked += 1
        kp += 900.0
    # A transect straddling the antimeridian (lon wraps +180 -> -180).
    center = QgsPointXY(179.9995, 10.0)
    xs = [-500.0 + j for j in range(1001)]
    new = analysis_task.transect_points(center, math.pi / 2, xs, distance)
    for i in range(0, 1001, 50):
        old = _old_point(center, math.pi / 2, xs[i], distance)
        worst = max(worst, distance.measureLine(old, new[i]))
        checked += 1
    wrapped = all(-180.0 <= p.x() <= 180.0 for p in new)
    ok = worst < 1e-3 and checked > 1000 and wrapped
    return _result("transect nodes + interpolation match per-sample geodesic "
                   "projection (WGS84 / Web Mercator / UTM, 75°S-75°N, "
                   "antimeridian)", ok,
                   f"worst {worst * 1000:.4f} mm over {checked} samples")


def _write_raster(path, crs_epsg, origin, cell, shape, fn):
    import numpy as np
    from osgeo import gdal, osr

    gdal.UseExceptions()
    rows, cols = shape
    ds = gdal.GetDriverByName("GTiff").Create(path, cols, rows, 1,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform((origin[0], cell, 0, origin[1], 0, -cell))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(crs_epsg)
    ds.SetProjection(srs.ExportToWkt())
    xs = origin[0] + (np.arange(cols) + 0.5) * cell
    ys = origin[1] - (np.arange(rows) + 0.5) * cell
    grid_x, grid_y = np.meshgrid(xs, ys)
    band = ds.GetRasterBand(1)
    band.WriteArray(fn(grid_x, grid_y).astype(np.float32))
    band.SetNoDataValue(-9999)
    ds = None
    layer = QgsRasterLayer(path, os.path.basename(path))
    layer.setCustomProperty(PREFIX + "vertical", "depth")
    return layer


def _old_offset_profile_samples(snap, route, stations_km, offset_m, distance):
    """The pre-optimisation raster branch, verbatim in behaviour."""
    port, stbd, peaks = [], [], []
    cell = min((sam.cell_m for sam, _ in snap._native_samplers),
               default=offset_m)
    count = max(11, min(2001, int(math.ceil(2 * offset_m / max(cell, .1))) + 1))
    xs = [-offset_m + j * 2 * offset_m / (count - 1) for j in range(count)]
    for kp in stations_km:
        center, bearing = _station_frame(route, kp, distance)
        zs, sources, cells = [], [], []
        for t in xs:
            zs.append(snap._sample_rasters(
                _old_point(center, bearing, t, distance)))
            sources.append(snap.last_source)
            cells.append(snap.last_cell_m)
        pz = sz = peak = None
        tilt, candidate_peak, p, q = cross_profile_metrics(
            xs, zs, offset_m, True, cells, sources)
        if candidate_peak is not None:
            peak = max(peak or 0, candidate_peak)
        if tilt is not None:
            pz, sz = p, q
        port.append(pz)
        stbd.append(sz)
        peaks.append(peak)
    return port, stbd, peaks


def _close(a, b, tol) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


def test_cross_profiles_match_previous_sampling() -> bool:
    import numpy as np

    project = QgsProject.instance()
    folder = tempfile.mkdtemp(prefix="bp_transects_")
    # Coarse geographic survey (~14 x 22 m cells) and a fine UTM patch
    # (5 m) over part of it: the transect falls back from fine to coarse
    # where it leaves the patch, as with real overlapping surveys. Both
    # surfaces are rough so any position error shows up in the depths.
    coarse = _write_raster(
        os.path.join(folder, "coarse_wgs84.tif"), 4326, (2.95, 50.05),
        0.0002, (500, 500),
        lambda x, y: 60 + 200 * (x - 3.0) + 8 * np.sin(x * 900) * np.cos(y * 700))
    fine = _write_raster(
        os.path.join(folder, "fine_utm.tif"), 32631, (498000, 5541000),
        5.0, (300, 300),
        lambda e, n: 55 + 6 * np.sin(e / 23.0) * np.cos(n / 31.0)
        + (e - 498000) / 300.0)
    layers = [coarse, fine]
    ok = all(layer.isValid() for layer in layers)
    for layer in layers:
        project.addMapLayer(layer)
    distance = _distance()
    try:
        to_wgs = QgsCoordinateTransform(fine.crs(), WGS84, project)
        # Route north-south 120 m inside the fine patch's east edge.
        start = to_wgs.transform(QgsPointXY(499380, 5539700))
        end = to_wgs.transform(QgsPointXY(499380, 5540900))
        route = RouteFrame.from_source(
            [QgsGeometry.fromPolylineXY([start, end])], distance)
        stations = [i * 0.02 for i in range(int(route.total_length_km / 0.02))]
        worst_depth = worst_peak = 0.0
        mismatches = valued = 0
        timings = {}
        for name, ids in (("fine+coarse", [fine.id(), coarse.id()]),
                          ("coarse only", [coarse.id()])):
            config = DepthSourceConfig({"mode": 1, "raster_layer_ids": ids})
            for offset in (40.0, 150.0):
                old_snap = analysis_task.DepthSnapshot(config, project)
                t0 = time.perf_counter()
                old = _old_offset_profile_samples(old_snap, route, stations,
                                                  offset, distance)
                t1 = time.perf_counter()
                new_snap = analysis_task.DepthSnapshot(config, project)
                port, stbd = new_snap.offset_profile_samples(
                    route, stations, offset, distance)
                t2 = time.perf_counter()
                timings[f"{name} ±{offset:g}"] = (t1 - t0, t2 - t1)
                new = (port, stbd, new_snap.cross_max_deg)
                for i in range(len(stations)):
                    for series, tol in ((0, 1e-3), (1, 1e-3), (2, 1e-3)):
                        a, b = old[series][i], new[series][i]
                        if not _close(a, b, tol):
                            mismatches += 1
                        elif a is not None:
                            if series == 2:
                                worst_peak = max(worst_peak, abs(a - b))
                            else:
                                worst_depth = max(worst_depth, abs(a - b))
                # A transect mixing two surveys has no supported tilt (by
                # design), so only the total is required to be non-empty.
                valued += sum(v is not None for v in port)
        # Precedence: per-sample sources and cells as the old sampler gave.
        snap = analysis_task.DepthSnapshot(
            DepthSourceConfig({"mode": 1,
                               "raster_layer_ids": [fine.id(), coarse.id()]}),
            project)
        xs = [-150.0 + j * 3.0 for j in range(101)]
        center, bearing = _station_frame(route, route.total_length_km / 2,
                                         distance)
        zs, sources, cells = snap._sample_raster_transect(center, bearing, xs,
                                                          distance)
        same_sources = True
        for x, z, source, cell in zip(xs, zs, sources, cells):
            expected = snap._sample_rasters(
                _old_point(center, bearing, x, distance))
            same_sources = same_sources and _close(expected, z, 1e-3) \
                and (snap.last_source, snap.last_cell_m) == (source, cell)
        both = {s for s in sources if s}
        ok = ok and mismatches == 0 and valued > 0 and same_sources \
            and len(both) == 2
        detail = (f"worst depth {worst_depth:.2e} m, peak {worst_peak:.2e}°; "
                  + ", ".join(f"{k}: {a:.2f}s -> {b:.2f}s"
                              for k, (a, b) in timings.items()))
        return _result("cross profiles equal the per-sample geodesic "
                       "sampling; fine->coarse fallback preserved", ok,
                       detail)
    finally:
        for layer in layers:
            project.removeMapLayer(layer.id())


def run_all() -> list:
    return [
        test_transect_positions_match_geodesic_projection(),
        test_cross_profiles_match_previous_sampling(),
    ]


if __name__ == "__main__":  # pragma: no cover
    run_all()
