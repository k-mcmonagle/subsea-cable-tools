# -*- coding: utf-8 -*-
"""KP Processing algorithms follow the KP method (docs/KP_METHOD.md).

Synthetic data only: a 2-leg route in latitude/longitude at 57.5 N with
legs of ~60 km, so the geodesic and the line straight in degrees are tens of
metres apart mid-leg. For each converted algorithm:

* positions lie on the geodesic (the KP path), not on the line straight in degrees;
* the Start KP parameter (default 0) offsets every KP read and written;
* the log states the KP basis and the start KP.

Tools that build a route from several features are given the legs stored
second leg first with a SeqNo field, so they also show the route order rule.
No user settings are written (Place Single KP Point's remembered inputs go to
a temporary settings file).
"""

from __future__ import annotations

import contextlib
import math
import os
import struct
import tempfile
from typing import List, Optional, Tuple

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext,
                       QgsFeature, QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, QgsProject,
                       QgsRasterLayer, QgsVectorLayer)

from ..kp_range_utils import make_distance_area

REQUIRES_QGIS = True

WGS = QgsCoordinateReferenceSystem("EPSG:4326")
CORNERS = [(-3.0, 57.5), (-2.2, 57.85), (-1.1, 57.7)]
START = 12.345      # the start KP used throughout (km)
ON_PATH_M = 0.05    # a position on the geodesic: within 5 cm

_CACHE = {}
_TEMP = []


def _report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""))
    return bool(ok)


def _temp_dir() -> str:
    """This process's own temp folder (parallel test runs never share files)."""
    if not _TEMP:
        _TEMP.append(tempfile.mkdtemp(prefix="sct_kp_method_"))
    return _TEMP[0]


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.log = []

    def pushInfo(self, info):  # noqa: N802 (Qt API)
        self.log.append(str(info))
        super().pushInfo(info)

    def pushWarning(self, warning):  # noqa: N802 (Qt API)
        self.log.append(str(warning))
        super().pushWarning(warning)

    def reportError(self, error, fatalError=False):  # noqa: N802,N803 (Qt API)
        self.log.append(str(error))
        super().reportError(error, fatalError)


def run_algorithm(algorithm, parameters, outputs):
    """Run a Processing algorithm headless; returns (layers, log)."""
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = _Feedback()
    algorithm.initAlgorithm({})
    params = dict(parameters)
    for key in outputs:
        params.setdefault(key, "memory:")
    results, ok = algorithm.run(params, context, feedback)
    if not ok:
        return None, feedback.log
    layers = [context.takeResultLayer(results.get(key)) if isinstance(results.get(key), str) else results.get(key)
              for key in outputs]
    return layers, feedback.log


def _add(layer):
    QgsProject.instance().addMapLayer(layer)
    return layer


def _remove(*layers):
    for layer in layers:
        if layer is not None:
            QgsProject.instance().removeMapLayer(layer.id())


def route_layer(name="route"):
    layer = QgsVectorLayer("LineString?crs=EPSG:4326&field=SeqNo:integer", name, "memory")
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in CORNERS]))
    feature.setAttributes([1])
    layer.dataProvider().addFeatures([feature])
    return _add(layer)


def legs_layer(name="legs"):
    """The same route as two features, stored second leg first: SeqNo gives the order.

    Layer order would start the route at the second leg; every KP below
    assumes SeqNo order. ``cls`` is "A" on the first leg, "B" on the second.
    """
    layer = QgsVectorLayer("LineString?crs=EPSG:4326&field=SeqNo:integer&field=cls:string", name, "memory")
    features = []
    for seq, cls, (a, b) in ((2, "B", (CORNERS[1], CORNERS[2])), (1, "A", (CORNERS[0], CORNERS[1]))):
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*a), QgsPointXY(*b)]))
        feature.setAttributes([seq, cls])
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return _add(layer)


def _fid_by_seq(layer):
    return {int(f["SeqNo"]): f.id() for f in layer.getFeatures()}


def _da():
    if "da" not in _CACHE:
        _CACHE["da"] = make_distance_area(WGS, QgsCoordinateTransformContext())
    return _CACHE["da"]


def leg_lengths() -> List[float]:
    if "legs" not in _CACHE:
        da = _da()
        _CACHE["legs"] = [float(da.measureLine(QgsPointXY(*a), QgsPointXY(*b)))
                          for a, b in zip(CORNERS[:-1], CORNERS[1:])]
    return _CACHE["legs"]


def geodesic_position(chainage_m: float) -> QgsPointXY:
    """The position at ``chainage_m`` along the geodesic route through CORNERS."""
    da = _da()
    for (a, b), length in zip(zip(CORNERS[:-1], CORNERS[1:]), leg_lengths()):
        p, q = QgsPointXY(*a), QgsPointXY(*b)
        if chainage_m <= length:
            return da.computeSpheroidProject(p, chainage_m, da.bearing(p, q))
        chainage_m -= length
    return QgsPointXY(*CORNERS[-1])


def geodesic_offset_m(point: QgsPointXY, chainage_m: float) -> float:
    """Distance (m) from ``point`` to the geodesic route's position at ``chainage_m``."""
    return float(_da().measureLine(QgsPointXY(point), geodesic_position(chainage_m)))


def chainage_at_lon(lon: float) -> float:
    """Chainage (m) where the geodesic route reaches longitude ``lon`` (it runs eastwards)."""
    base = 0.0
    for (a, b), length in zip(zip(CORNERS[:-1], CORNERS[1:]), leg_lengths()):
        if a[0] <= lon <= b[0]:
            lo, hi = 0.0, length
            for _ in range(60):
                mid = (lo + hi) / 2.0
                if geodesic_position(base + mid).x() < lon:
                    lo = mid
                else:
                    hi = mid
            return base + (lo + hi) / 2.0
        base += length
    raise ValueError(lon)


def nearest_on_route(point: QgsPointXY) -> Tuple[float, float]:
    """``(distance m, chainage m)`` of the nearest point of the geodesic route to ``point``."""
    da = _da()
    total = sum(leg_lengths())

    def dist(s):
        return float(da.measureLine(QgsPointXY(point), geodesic_position(min(max(s, 0.0), total))))

    _d, s0 = min((dist(s), s) for s in [k * 500.0 for k in range(int(total // 500.0) + 1)] + [total])
    lo, hi = max(0.0, s0 - 500.0), min(total, s0 + 500.0)
    g = (math.sqrt(5.0) - 1.0) / 2.0
    for _ in range(80):
        a, b = hi - g * (hi - lo), lo + g * (hi - lo)
        if dist(a) < dist(b):
            hi = b
        else:
            lo = a
    s = (lo + hi) / 2.0
    return dist(s), s


def worst_off_route(points, step: int = 1) -> float:
    """Largest distance (m) of ``points[::step]`` (and the last) from the geodesic route."""
    sample = list(points[::step]) + [points[-1]]
    return max(nearest_on_route(QgsPointXY(p))[0] for p in sample)


def _azimuth_deg(chainage_m: float) -> float:
    """Route course (deg) at ``chainage_m``, along the geodesic."""
    da = _da()
    return math.degrees(da.bearing(geodesic_position(chainage_m), geodesic_position(chainage_m + 10.0)))


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _basis_logged(log, start: Optional[float] = START) -> bool:
    ok = any("geodesic" in line for line in log)
    if start is not None:
        ok = ok and any(f"Start KP {start:.3f}" in line for line in log)
    return ok


@contextlib.contextmanager
def _temp_settings(project_crs: Optional[str] = None):
    """Settings in a temporary file (none of the tester's are read or written),
    optionally with another project CRS; both restored afterwards."""
    from qgis.PyQt.QtCore import QSettings

    project = QgsProject.instance()
    old_crs = project.crs()
    old_format = QSettings.defaultFormat()
    probe = QSettings()
    old_file = probe.fileName()
    old_is_ini = probe.format() == QSettings.Format.IniFormat
    del probe
    folder = os.path.join(_temp_dir(), f"settings_{len(os.listdir(_temp_dir()))}")
    os.makedirs(folder, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, folder)
    if project_crs:
        project.setCrs(QgsCoordinateReferenceSystem(project_crs))
    try:
        yield
    finally:
        project.setCrs(old_crs)
        QSettings.setDefaultFormat(old_format)
        if old_is_ini:
            # Qt has no getter for the INI path: put back the folder the
            # settings file was in (<path>/<organisation>/<application>.ini),
            # so later tests in this process use their usual settings.
            def same(a, b):
                return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))

            for candidate in (os.path.dirname(os.path.dirname(old_file)), os.path.dirname(old_file)):
                QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, candidate)
                if same(QSettings().fileName(), old_file):
                    break


def _write_raster(name: str, x0: float, y0: float, cell: float, width: int, height: int, value_at) -> str:
    """A float32 GeoTIFF in EPSG:4326 (top-left x0, y0) with ``value_at(lon, lat)`` per cell centre."""
    from osgeo import gdal, osr

    path = os.path.join(_temp_dir(), name)
    ds = gdal.GetDriverByName("GTiff").Create(path, width, height, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([x0, cell, 0.0, y0, 0.0, -cell])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    for row in range(height):
        lat = y0 - (row + 0.5) * cell
        values = [value_at(x0 + (col + 0.5) * cell, lat) for col in range(width)]
        band.WriteRaster(0, row, width, 1, struct.pack(f"{width}f", *values))
    band.FlushCache()
    ds = None
    return path


# --------------------------------------------------------------------------
# KP Points
# --------------------------------------------------------------------------


def test_place_kp_points() -> List[bool]:
    from ..processing.place_kp_points_algorithm import PlaceKpPointsAlgorithm

    layer = route_layer()
    try:
        (points,), log = run_algorithm(PlaceKpPointsAlgorithm(), {
            "INPUT_LINE": layer, "INTERVAL_1KM": False, "INTERVAL_50KM": False, "INTERVAL_100KM": False,
            "INTERVAL_CUSTOM": 10.0, "START_KP": 12.345}, ["OUTPUT"])
        feats = sorted(points.getFeatures(), key=lambda f: f["kp"])
        kps = [round(f["kp"], 3) for f in feats]
        worst = max(geodesic_offset_m(f.geometry().asPoint(), (f["kp"] - 12.345) * 1000.0) for f in feats)
        ok_kp = kps[0] == 12.345 and kps[1] == 20.0 and all(b - a == 10.0 for a, b in zip(kps[1:-1], kps[2:]))
        ok_basis = any("geodesic" in line for line in log) and any("Start KP 12.345" in line for line in log)
        return [
            _report("Place KP Points: start KP, then round KPs (12.345, 20, 30 ...)", ok_kp, str(kps[:5])),
            _report("Place KP Points: points on the geodesic (60 km legs at 57.5 N)", worst < 0.05,
                    f"furthest {worst * 1000:.1f} mm"),
            _report("Place KP Points: the log states the KP basis and the start KP", ok_basis),
        ]
    finally:
        QgsProject.instance().removeMapLayer(layer.id())


def test_place_kp_points_from_csv() -> List[bool]:
    from ..processing.place_kp_points_from_csv_algorithm import PlaceKpPointsFromCsvAlgorithm

    layer = route_layer()
    pasted = "KP\tName\n12.0\tbefore\n12.345\tstart\n42.345\tleg1\n100\tleg2\n500\tpast"
    try:
        layers, log = run_algorithm(PlaceKpPointsFromCsvAlgorithm(), {
            "INPUT_LINE": layer, "PASTED_KPS": pasted, "START_KP": START}, ["OUTPUT"])
        if layers is None:
            return [_report("Place KP Points from CSV runs", False, " | ".join(log[-3:]))]
        feats = {f["name"]: f for f in layers[0].getFeatures()}
        worst = max(geodesic_offset_m(f.geometry().asPoint(), (f["kp_value"] - START) * 1000.0)
                    for f in feats.values())
        return [
            _report("Place KP Points from CSV: KPs before the start KP or past the end are skipped",
                    sorted(feats) == ["leg1", "leg2", "start"], str(sorted(feats))),
            _report("Place KP Points from CSV: each point at start KP + chainage, on the geodesic",
                    worst < ON_PATH_M, f"furthest {worst * 1000:.1f} mm"),
            _report("Place KP Points from CSV: the log states the KP basis and the start KP", _basis_logged(log)),
        ]
    finally:
        _remove(layer)


def test_place_single_kp_point() -> List[bool]:
    """KP 42.345 with start KP 12.345 (30 km along the first leg), DCC 100 m starboard.

    The project CRS is set to Web Mercator for the run: KP and DCC must not
    depend on it (they used to be measured in the project CRS).
    """
    from ..processing.place_single_kp_point_algorithm import PlaceSingleKpPointAlgorithm

    layer = route_layer()
    try:
        with _temp_settings("EPSG:3857"):
            layers, log = run_algorithm(PlaceSingleKpPointAlgorithm(), {
                "INPUT_LINE": layer, "KP_VALUE": 42.345, "DCC_VALUE": 100.0, "DCC_UNITS": 0,
                "ADD_DCC_LINE": True, "EXTEND_DCC_LINE": True, "DCC_LINE_EXTEND": 50.0,
                "ADD_POINT_ON_RPL": True, "START_KP": START},
                ["OUTPUT", "OUTPUT_DCC_LINE", "OUTPUT_POINT_ON_RPL"])
    finally:
        _remove(layer)
    if layers is None or any(lyr is None for lyr in layers):
        return [_report("Place Single KP Point runs", False, " | ".join(log[-3:]))]
    da = _da()
    point = next(layers[0].getFeatures())
    offset = point.geometry().asPoint()
    base = next(layers[2].getFeatures()).geometry().asPoint()
    line = next(layers[1].getFeatures()).geometry().asPolyline()
    on_path = geodesic_offset_m(base, 30000.0)
    range_m = float(da.measureLine(base, offset))
    dcc_route, foot = nearest_on_route(offset)
    course = _azimuth_deg(30000.0)
    bearing_from = float(point["bearing_from_line_deg"])
    extension = float(da.measureLine(base, QgsPointXY(line[-1])))
    ext_bearing = math.degrees(da.bearing(base, QgsPointXY(line[-1])))
    return [
        _report("Place Single KP Point: the point on the RPL is at start KP + chainage, on the geodesic",
                on_path < ON_PATH_M and point["kp_value"] == 42.345, f"{on_path * 1000:.1f} mm off"),
        _report("Place Single KP Point: DCC 100 m perpendicular: its nearest point on the route is the KP",
                abs(range_m - 100.0) < 0.01 and abs(dcc_route - 100.0) < 0.05 and abs(foot - 30000.0) < 1.0
                and abs(float(point["range_to_line_m"]) - 100.0) < 0.01,
                f"range {range_m:.3f} m, DCC to route {dcc_route:.3f} m at chainage {foot:.1f} m"),
        _report("Place Single KP Point: +DCC is starboard (true bearing = course + 90), the project CRS "
                "does not matter", _angle_diff(bearing_from, course + 90.0) < 0.05,
                f"bearing {bearing_from:.3f} vs course {course:.3f} + 90"),
        _report("Place Single KP Point: the DCC line extends 50 m to port past the route",
                len(line) == 3 and abs(extension - 50.0) < 0.01 and _angle_diff(ext_bearing, course - 90.0) < 0.05,
                f"{len(line)} vertices, extension {extension:.3f} m"),
        _report("Place Single KP Point: the log states the KP basis and the start KP", _basis_logged(log)),
    ]


def test_place_single_kp_point_grid() -> List[bool]:
    """Cartesian KP: the KP path and the DCC are straight in the grid.

    * a route in UTM 31N measured in its own CRS (planar);
    * the lat/lon route measured in the project CRS UTM 30N (grid KP, no
      grid set in KP settings: the settings are a temporary empty file).
    """
    from ..processing.place_single_kp_point_algorithm import PlaceSingleKpPointAlgorithm

    def run(layer, kp, project_crs):
        with _temp_settings(project_crs):
            return run_algorithm(PlaceSingleKpPointAlgorithm(), {
                "INPUT_LINE": layer, "KP_VALUE": kp, "DCC_VALUE": 100.0, "DCC_UNITS": 0,
                "ADD_POINT_ON_RPL": True, "DISTANCE_MODE": 1, "START_KP": 2.0},
                ["OUTPUT", "OUTPUT_POINT_ON_RPL"])

    utm = _add(QgsVectorLayer("LineString?crs=EPSG:32631", "utm_route", "memory"))
    feature = QgsFeature(utm.fields())
    feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(500000.0, 6000000.0),
                                                    QgsPointXY(520000.0, 6000000.0)]))
    utm.dataProvider().addFeatures([feature])
    lonlat = route_layer("grid_route")
    try:
        planar, log1 = run(utm, 12.0, None)
        grid, log2 = run(lonlat, 32.0, "EPSG:32630")
    finally:
        _remove(utm, lonlat)
    if planar is None or grid is None:
        return [_report("Place Single KP Point (grid KP) runs", False, " | ".join((log1 + log2)[-3:]))]
    offset = next(planar[0].getFeatures()).geometry().asPoint()
    base = next(planar[1].getFeatures()).geometry().asPoint()
    ok_planar = (abs(base.x() - 510000.0) < 1e-6 and abs(base.y() - 6000000.0) < 1e-6
                 and abs(offset.x() - 510000.0) < 1e-6 and abs(offset.y() - 5999900.0) < 1e-6)
    # Grid: KP 32 = 30 km along the first leg, straight in UTM 30N; DCC 100 grid metres to starboard.
    to_grid = QgsCoordinateTransform(WGS, QgsCoordinateReferenceSystem("EPSG:32630"), QgsProject.instance())
    a, b = to_grid.transform(QgsPointXY(*CORNERS[0])), to_grid.transform(QgsPointXY(*CORNERS[1]))
    length = math.hypot(b.x() - a.x(), b.y() - a.y())
    ux, uy = (b.x() - a.x()) / length, (b.y() - a.y()) / length
    want_base = QgsPointXY(a.x() + 30000.0 * ux, a.y() + 30000.0 * uy)
    want_offset = QgsPointXY(want_base.x() + 100.0 * uy, want_base.y() - 100.0 * ux)
    got_base = to_grid.transform(next(grid[1].getFeatures()).geometry().asPoint())
    got_offset = to_grid.transform(next(grid[0].getFeatures()).geometry().asPoint())
    err = max(math.hypot(got_base.x() - want_base.x(), got_base.y() - want_base.y()),
              math.hypot(got_offset.x() - want_offset.x(), got_offset.y() - want_offset.y()))
    return [
        _report("Place Single KP Point (planar KP in a projected route CRS): KP and DCC straight in the CRS",
                ok_planar, f"base {base.x():.3f},{base.y():.3f} offset {offset.x():.3f},{offset.y():.3f}"),
        _report("Place Single KP Point (grid KP in the project's UTM zone): KP and DCC straight in the grid",
                err < 0.01 and any("grid (Cartesian) in EPSG:32630" in line for line in log2),
                f"{err * 1000:.1f} mm from the grid construction"),
    ]


def test_nearest_kp() -> List[bool]:
    from ..processing.nearest_kp_algorithm import NearestKPAlgorithm

    route = route_layer()
    da = make_distance_area(WGS, QgsCoordinateTransformContext())
    a, b = QgsPointXY(*CORNERS[0]), QgsPointXY(*CORNERS[1])
    length = float(da.measureLine(a, b))
    azimuth = da.bearing(a, b)
    foot = da.computeSpheroidProject(a, length / 2.0, azimuth)       # on the geodesic, mid-leg
    beside = da.computeSpheroidProject(foot, 100.0, azimuth + 1.5707963267948966)
    points = QgsVectorLayer("Point?crs=EPSG:4326", "targets", "memory")
    feature = QgsFeature(points.fields())
    feature.setGeometry(QgsGeometry.fromPointXY(beside))
    points.dataProvider().addFeatures([feature])
    QgsProject.instance().addMapLayer(points)
    try:
        layers, log = run_algorithm(NearestKPAlgorithm(), {
            "INPUT_POINTS": points, "INPUT_PATHS": route, "START_KP": 5.0}, ["OUTPUT_POINTS", "OUTPUT_LINES"])
        if layers is None:
            return [_report("Nearest KP runs", False, " | ".join(log[-3:]))]
        hit = next(layers[0].getFeatures())
        kp = float(hit["kp_km"])
        expect = 5.0 + length / 2000.0
        return [_report("Nearest KP: start KP + KP of the geodesic foot (100 m beside a 60 km leg, kp_km to 1 m)",
                        abs(kp - round(expect, 3)) < 1e-9, f"{kp:.3f} vs {expect:.6f} km")]
    finally:
        for layer in (route, points):
            QgsProject.instance().removeMapLayer(layer.id())


# --------------------------------------------------------------------------
# KP Ranges
# --------------------------------------------------------------------------


def test_kp_range_highlighter() -> List[bool]:
    from ..processing.kp_range_highlighter_algorithm import KPRangeHighlighterAlgorithm

    layer = route_layer()
    try:
        layers, log = run_algorithm(KPRangeHighlighterAlgorithm(), {
            "INPUT": layer, "START_KP": 22.345, "END_KP": 52.345, "ROUTE_START_KP": START}, ["OUTPUT"])
        if layers is None:
            return [_report("KP Range Highlighter runs", False, " | ".join(log[-3:]))]
        feat = next(layers[0].getFeatures())
        line = feat.geometry().asPolyline()
        ends = max(geodesic_offset_m(line[0], 10000.0), geodesic_offset_m(line[-1], 40000.0))
        worst = worst_off_route(line, step=10)
        return [
            _report("KP Range Highlighter: the section runs from start KP + chainage to end KP + chainage",
                    ends < ON_PATH_M and feat["start_kp"] == 22.345 and feat["end_kp"] == 52.345,
                    f"ends {ends * 1000:.1f} mm off"),
            _report("KP Range Highlighter: the section follows the geodesic between vertices",
                    len(line) > 10 and worst < ON_PATH_M, f"{len(line)} vertices, furthest {worst * 1000:.1f} mm"),
            _report("KP Range Highlighter: the log states the KP basis and the start KP", _basis_logged(log)),
        ]
    finally:
        _remove(layer)


def test_kp_range_csv() -> List[bool]:
    from ..processing.kp_range_csv_algorithm import KPRangeCSVAlgorithm

    layer = route_layer()
    pasted = "Start\tEnd\tLabel\n22.345\t52.345\trange\n30\t30\tpoint\n5\t10\tbefore\n100\t300\tpast"
    try:
        layers, log = run_algorithm(KPRangeCSVAlgorithm(), {
            "INPUT_LINE": layer, "PASTED_RANGES": pasted, "START_KP": START}, ["OUTPUT", "OUTPUT_POINTS"])
        if layers is None:
            return [_report("KP Range Highlighter from CSV runs", False, " | ".join(log[-3:]))]
        ranges = list(layers[0].getFeatures())
        points = list(layers[1].getFeatures())
        ok_rows = ([f["label"] for f in ranges] == ["range"] and [f["label"] for f in points] == ["point"])
        detail = f"ranges={[f['label'] for f in ranges]} points={[f['label'] for f in points]}"
        if not ok_rows:
            return [_report("KP Range Highlighter from CSV: rows before the start KP / past the end skipped",
                            False, detail)]
        line = ranges[0].geometry().asPolyline()
        ends = max(geodesic_offset_m(line[0], 10000.0), geodesic_offset_m(line[-1], 40000.0))
        worst = worst_off_route(line, step=10)
        point_off = geodesic_offset_m(points[0].geometry().asPoint(), (30.0 - START) * 1000.0)
        return [
            _report("KP Range Highlighter from CSV: rows before the start KP / past the end skipped", True, detail),
            _report("KP Range Highlighter from CSV: ranges and positions at start KP + chainage on the geodesic",
                    ends < ON_PATH_M and worst < ON_PATH_M and point_off < ON_PATH_M,
                    f"ends {ends * 1000:.1f} mm, furthest vertex {worst * 1000:.1f} mm, point {point_off * 1000:.1f} mm"),
            _report("KP Range Highlighter from CSV: the log states the KP basis and the start KP", _basis_logged(log)),
        ]
    finally:
        _remove(layer)


def test_kp_range_rule_based() -> List[bool]:
    from ..processing.kp_range_extract_rule_based_algorithm import ExtractKPRangesRuleBasedAlgorithm

    layer = legs_layer()
    first, second = leg_lengths()
    try:
        layers, log = run_algorithm(ExtractKPRangesRuleBasedAlgorithm(), {
            "INPUT_RPL": layer, "CATEGORY_FIELD": "cls", "MERGE_ADJACENT": False, "CREATE_SEGMENTS": True,
            "START_KP": START}, ["OUTPUT_RANGES", "OUTPUT_SEGMENTS"])
        if layers is None:
            return [_report("Extract KP Ranges (Rule Based) runs", False, " | ".join(log[-3:]))]
        rows = sorted((f["start_kp"], f["end_kp"], f["class_value"]) for f in layers[0].getFeatures())
        expect = [(START, START + first / 1000.0, "A"),
                  (START + first / 1000.0, START + (first + second) / 1000.0, "B")]
        ok_rows = len(rows) == 2 and all(
            abs(r[0] - e[0]) < 1e-9 and abs(r[1] - e[1]) < 1e-9 and r[2] == e[2] for r, e in zip(rows, expect))
        segment_a = next(f for f in layers[1].getFeatures() if f["class_value"] == "A").geometry().asPolyline()
        worst = worst_off_route(segment_a, step=10)
        return [
            _report("Extract KP Ranges (Rule Based): ranges in SeqNo order from the start KP", ok_rows,
                    str([(round(a, 3), round(b, 3), c) for a, b, c in rows])),
            _report("Extract KP Ranges (Rule Based): segments follow the geodesic between vertices",
                    len(segment_a) > 10 and worst < ON_PATH_M,
                    f"{len(segment_a)} vertices, furthest {worst * 1000:.1f} mm"),
            _report("Extract KP Ranges (Rule Based): the log states the KP basis and the start KP",
                    _basis_logged(log)),
        ]
    finally:
        _remove(layer)


def test_kp_range_depth_slope_summary() -> List[bool]:
    """Depth = 100 + 1000 x (lat - 57.4) m: the average depth over the first leg tells
    the geodesic (which bulges north) from the line straight in degrees."""
    from ..processing.kp_range_depth_slope_summary_algorithm import KPRangeDepthSlopeSummaryAlgorithm

    path = _write_raster("depth_by_lat.tif", -3.05, 57.9, 0.001, 900, 450,
                         lambda _lon, lat: 100.0 + 1000.0 * (lat - 57.4))
    raster = _add(QgsRasterLayer(path, "depth_by_lat"))
    ranges = QgsVectorLayer("LineString?crs=EPSG:4326", "leg", "memory")
    feature = QgsFeature(ranges.fields())
    feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*CORNERS[0]), QgsPointXY(*CORNERS[1])]))
    ranges.dataProvider().addFeatures([feature])
    _add(ranges)
    try:
        layers, log = run_algorithm(KPRangeDepthSlopeSummaryAlgorithm(), {
            "INPUT": ranges, "DEPTH_SOURCE": 1, "RASTER_LAYERS": [raster], "SAMPLE_INTERVAL_M": 500.0},
            ["OUTPUT"])
        if layers is None:
            return [_report("KP Range Depth + Slope Summary runs", False, " | ".join(log[-3:]))]
        got = float(next(layers[0].getFeatures())["depth_avg"])
        length = leg_lengths()[0]
        stations = [k * 500.0 for k in range(int(length // 500.0) + 1)] + [length]
        geodesic = sum(100.0 + 1000.0 * (geodesic_position(s).y() - 57.4) for s in stations) / len(stations)
        y0, y1 = CORNERS[0][1], CORNERS[1][1]
        chord = sum(100.0 + 1000.0 * (y0 + (y1 - y0) * s / length - 57.4) for s in stations) / len(stations)
        return [_report("KP Range Depth + Slope Summary: stations on the geodesic, not the line straight in degrees",
                        abs(got - geodesic) < 0.02 and abs(geodesic - chord) > 0.3,
                        f"depth_avg {got:.3f}, geodesic {geodesic:.3f}, straight in degrees {chord:.3f}"),
                _report("KP Range Depth + Slope Summary: the log states the KP basis", _basis_logged(log, None))]
    finally:
        _remove(ranges, raster)


# --------------------------------------------------------------------------
# RPL Tools
# --------------------------------------------------------------------------


def test_extract_ac_points() -> List[bool]:
    from ..processing.extract_ac_points_algorithm import ExtractACPointsAlgorithm

    layer = route_layer()
    try:
        layers, log = run_algorithm(ExtractACPointsAlgorithm(), {"INPUT_RPL": layer, "START_KP": START}, ["OUTPUT"])
        if layers is None:
            return [_report("Extract A/C Points runs", False, " | ".join(log[-3:]))]
        rows = [(f["kp"], f.geometry().asPoint()) for f in layers[0].getFeatures()]
        expect = round(START + leg_lengths()[0] / 1000.0, 4)
        ok = (len(rows) == 1 and abs(rows[0][0] - expect) < 1e-9
              and rows[0][1].x() == CORNERS[1][0] and rows[0][1].y() == CORNERS[1][1])
        return [_report("Extract A/C Points: the A/C vertex at start KP + chainage", ok,
                        f"{[r[0] for r in rows]} vs {expect}"),
                _report("Extract A/C Points: the log states the KP basis and the start KP", _basis_logged(log))]
    finally:
        _remove(layer)


def test_crossing_points() -> List[bool]:
    from ..processing.identify_rpl_crossing_points_algorithm import IdentifyRPLCrossingPointsAlgorithm

    layer = legs_layer()
    assets = QgsVectorLayer("LineString?crs=EPSG:4326&field=name:string", "assets", "memory")
    features = []
    for name, lon in (("leg1", -2.6), ("leg2", -1.6)):
        feature = QgsFeature(assets.fields())
        feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(lon, 57.3), QgsPointXY(lon, 58.2)]))
        feature.setAttributes([name])
        features.append(feature)
    assets.dataProvider().addFeatures(features)
    _add(assets)
    fids = _fid_by_seq(layer)
    try:
        layers, log = run_algorithm(IdentifyRPLCrossingPointsAlgorithm(), {
            "INPUT_RPL": layer, "INPUT_ASSETS": [assets], "START_KP": START}, ["OUTPUT"])
        if layers is None:
            return [_report("Identify RPL Crossing Points runs", False, " | ".join(log[-3:]))]
        rows = {f["asset_name"]: f for f in layers[0].getFeatures()}
        ok_kp, ok_pos, details = len(rows) == 2, len(rows) == 2, []
        for name, lon, seq in (("leg1", -2.6, 1), ("leg2", -1.6, 2)):
            row = rows.get(name)
            if row is None:
                continue
            chainage = chainage_at_lon(lon)
            expect = round(START + chainage / 1000.0, 4)
            off = geodesic_offset_m(row.geometry().asPoint(), chainage)
            ok_kp = ok_kp and abs(row["kp"] - expect) < 1.5e-4 and row["rpl_fid"] == fids[seq]
            ok_pos = ok_pos and off < ON_PATH_M
            details.append(f"{name}: kp {row['kp']} vs {expect}, {off * 1000:.1f} mm off")
        return [
            _report("Identify RPL Crossing Points: KP = start KP + chainage, legs in SeqNo order", ok_kp,
                    "; ".join(details)),
            _report("Identify RPL Crossing Points: crossings where the geodesic crosses the asset", ok_pos,
                    "; ".join(details)),
            _report("Identify RPL Crossing Points: the log states the KP basis and the start KP", _basis_logged(log)),
        ]
    finally:
        _remove(layer, assets)


def test_area_listing() -> List[bool]:
    from ..processing.identify_rpl_area_listing_algorithm import IdentifyRPLAreaListingAlgorithm

    layer = legs_layer()
    areas = QgsVectorLayer("Polygon?crs=EPSG:4326&field=name:string", "areas", "memory")
    feature = QgsFeature(areas.fields())
    feature.setGeometry(QgsGeometry.fromPolygonXY([[
        QgsPointXY(-2.7, 57.0), QgsPointXY(-2.5, 57.0), QgsPointXY(-2.5, 58.5), QgsPointXY(-2.7, 58.5),
        QgsPointXY(-2.7, 57.0)]]))
    feature.setAttributes(["band"])
    areas.dataProvider().addFeatures([feature])
    _add(areas)
    fids = _fid_by_seq(layer)
    try:
        layers, log = run_algorithm(IdentifyRPLAreaListingAlgorithm(), {
            "INPUT_RPL": layer, "INPUT_AREAS": [areas], "START_KP": START}, ["OUTPUT"])
        if layers is None:
            return [_report("Identify RPL Area Listing runs", False, " | ".join(log[-3:]))]
        rows = list(layers[0].getFeatures())
        if len(rows) != 1:
            return [_report("Identify RPL Area Listing: one section", False, f"{len(rows)} sections")]
        row = rows[0]
        s_in, s_out = chainage_at_lon(-2.7), chainage_at_lon(-2.5)
        e_in, e_out = START + s_in / 1000.0, START + s_out / 1000.0
        line = row.geometry().asPolyline()
        ends = max(geodesic_offset_m(line[0], s_in), geodesic_offset_m(line[-1], s_out))
        worst = worst_off_route(line, step=5)
        return [
            _report("Identify RPL Area Listing: section KPs = start KP + chainage where the geodesic enters/leaves",
                    abs(row["start_kp"] - e_in) < 6e-4 and abs(row["end_kp"] - e_out) < 6e-4
                    and row["rpl_fid"] == fids[1],
                    f"{row['start_kp']}-{row['end_kp']} vs {e_in:.4f}-{e_out:.4f}"),
            _report("Identify RPL Area Listing: the section follows the geodesic",
                    ends < ON_PATH_M and worst < ON_PATH_M,
                    f"ends {ends * 1000:.1f} mm, furthest vertex {worst * 1000:.1f} mm ({len(line)} vertices)"),
            _report("Identify RPL Area Listing: the log states the KP basis and the start KP", _basis_logged(log)),
        ]
    finally:
        _remove(layer, areas)


def test_corridor_proximity_listing() -> List[bool]:
    """A short line across the geodesic, clear of the line straight in degrees:
    without a corridor it is listed because it intersects the KP path."""
    from ..processing.identify_rpl_lay_corridor_proximity_listing_algorithm import (
        IdentifyRPLLayCorridorProximityListingAlgorithm)

    layer = legs_layer()
    chainage = chainage_at_lon(-2.6)
    geo_lat = geodesic_position(chainage).y()
    (x0, y0), (x1, y1) = CORNERS[0], CORNERS[1]
    chord_lat = y0 + (y1 - y0) * (-2.6 - x0) / (x1 - x0)
    separation = float(_da().measureLine(QgsPointXY(-2.6, geo_lat), QgsPointXY(-2.6, chord_lat)))
    lines = QgsVectorLayer("LineString?crs=EPSG:4326&field=name:string", "short_lines", "memory")
    feature = QgsFeature(lines.fields())
    feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(-2.6, geo_lat - 0.0003),
                                                    QgsPointXY(-2.6, geo_lat + 0.0003)]))
    feature.setAttributes(["across"])
    lines.dataProvider().addFeatures([feature])
    _add(lines)
    try:
        layers, log = run_algorithm(IdentifyRPLLayCorridorProximityListingAlgorithm(), {
            "INPUT_RPL": layer, "INPUT_LINES": [lines], "TRIM_TO_CORRIDOR": False, "START_KP": START},
            ["OUTPUT_LINES"])
        if layers is None:
            return [_report("Identify Features Intersecting RPL runs", False, " | ".join(log[-3:]))]
        rows = list(layers[0].getFeatures())
        expect = round(START + chainage / 1000.0, 3)
        ok = (separation > 50.0 and len(rows) == 1 and abs(rows[0]["rpl_kp"] - expect) < 1e-9
              and rows[0]["rpl_dcc"] < 0.05)
        detail = (f"{len(rows)} listed, kp {rows[0]['rpl_kp'] if rows else None} vs {expect}, "
                  f"line straight in degrees {separation:.0f} m away")
        return [_report("Identify Features Intersecting RPL: intersects the geodesic; KP = start KP + chainage",
                        ok, detail),
                _report("Identify Features Intersecting RPL: the log states the KP basis and the start KP",
                        _basis_logged(log))]
    finally:
        _remove(layer, lines)


def _points_layer(name, rows):
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=Event:string", name, "memory")
    features = []
    for event, point in rows:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point)))
        feature.setAttributes([event])
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return _add(layer)


def test_translate_kp() -> List[bool]:
    from ..processing.translate_kp_from_rpl_to_rpl_algorithm import TranslateKPFromRPLToRPLAlgorithm

    route = legs_layer()
    half = leg_lengths()[0] / 2.0
    beside = _da().computeSpheroidProject(geodesic_position(half), 100.0,
                                          math.radians(_azimuth_deg(half) + 90.0))
    points = _points_layer("aslaid", [("P1", beside)])
    try:
        layers, log = run_algorithm(TranslateKPFromRPLToRPLAlgorithm(), {
            "INPUT_SOURCE_LINE": route, "INPUT_TARGET_POINTS": points, "START_KP": START}, ["OUTPUT_POINTS"])
        if layers is None:
            return [_report("Translate KP Between RPLs runs", False, " | ".join(log[-3:]))]
        row = next(layers[0].getFeatures())
        expect = round(START + half / 1000.0, 3)
        return [_report("Translate KP Between RPLs: start KP + KP of the geodesic foot, DCC 100 m",
                        abs(row["design_route_kp"] - expect) < 1e-9 and abs(row["design_route_dcc"] - 100.0) < 0.01,
                        f"kp {row['design_route_kp']} vs {expect}, dcc {row['design_route_dcc']}"),
                _report("Translate KP Between RPLs: the log states the KP basis and the start KP",
                        _basis_logged(log))]
    finally:
        _remove(route, points)


def test_route_comparison() -> List[bool]:
    """Design and as-laid on the same geodesic route; measured KPs carry each route's start KP."""
    from ..processing.rpl_route_comparison_algorithm import RPLRouteComparisonAlgorithm

    design_lines = route_layer("design_route")
    aslaid_lines = route_layer("aslaid_route")
    p1, p2 = geodesic_position(30000.0), geodesic_position(90000.0)
    starboard = _da().computeSpheroidProject(p1, 50.0, math.radians(_azimuth_deg(30000.0) + 90.0))
    design_pts = _points_layer("design_pts", [("RPT1", p1), ("RPT2", p2)])
    aslaid_pts = _points_layer("aslaid_pts", [("RPT1", starboard), ("RPT2", p2)])
    base = {"DESIGN_POINTS": design_pts, "DESIGN_EVENTS_FIELD": "Event", "DESIGN_LINES": design_lines,
            "ASLAID_POINTS": aslaid_pts, "ASLAID_EVENTS_FIELD": "Event", "ASLAID_LINES": aslaid_lines,
            "START_KP": START}
    try:
        same, log = run_algorithm(RPLRouteComparisonAlgorithm(), base, ["OUTPUT_COMPARISON"])
        other, _log2 = run_algorithm(RPLRouteComparisonAlgorithm(), dict(base, ASLAID_START_KP=12.0),
                                     ["OUTPUT_COMPARISON"])
        if same is None or other is None:
            return [_report("Compare Design vs As-Laid Routes runs", False, " | ".join(log[-3:]))]
        rows = {f["design_event"]: f for f in same[0].getFeatures()}
        rows2 = {f["design_event"]: f for f in other[0].getFeatures()}
        r1, r2, q2 = rows.get("RPT1"), rows.get("RPT2"), rows2.get("RPT2")
        if r1 is None or r2 is None or q2 is None:
            return [_report("Compare Design vs As-Laid Routes: both events paired", False, str(sorted(rows)))]
        ok_offsets = abs(r1["cross_track_m"] - 50.0) < 0.05 and abs(r1["along_track_m"]) < 0.05
        ok_kp = (abs(r2["aslaid_kp"] - (START + 90.0)) < 1e-3 and abs(r2["kp_delta_m"]) < 0.01
                 and abs(q2["aslaid_kp"] - 102.0) < 1e-3 and abs(q2["kp_delta_m"] + 345.0) < 0.01)
        return [
            _report("Compare Design vs As-Laid Routes: offsets on the geodesic design route", ok_offsets,
                    f"cross {r1['cross_track_m']:.3f} m, along {r1['along_track_m']:.3f} m"),
            _report("Compare Design vs As-Laid Routes: measured KPs = start KP + chainage (as-laid start KP "
                    "defaults to the design's)", ok_kp,
                    f"aslaid_kp {r2['aslaid_kp']:.4f} / {q2['aslaid_kp']:.4f}, "
                    f"kp_delta {r2['kp_delta_m']:.3f} / {q2['kp_delta_m']:.3f} m"),
            _report("Compare Design vs As-Laid Routes: the log states the KP basis and the start KP",
                    _basis_logged(log)),
        ]
    finally:
        _remove(design_lines, aslaid_lines, design_pts, aslaid_pts)


def test_seabed_length_intervals() -> List[bool]:
    """KP-interval output: start KP to the next round KP, then interval by interval."""
    from ..processing.seabed_length_algorithm import SeabedLengthAlgorithm

    path = _write_raster("flat_100m.tif", -3.1, 57.95, 0.05, 42, 11, lambda _lon, _lat: 100.0)
    raster = _add(QgsRasterLayer(path, "flat"))
    layer = route_layer()
    total_km = sum(leg_lengths()) / 1000.0
    try:
        layers, log = run_algorithm(SeabedLengthAlgorithm(), {
            "INPUT_LINE": layer, "BATHY_TYPE": 0, "INPUT_RASTER": raster, "SAMPLING_INTERVAL": 1000,
            "SENSITIVITY_ANALYSIS": False, "OUTPUT_INTERVALS": True, "KP_INTERVAL": 50, "START_KP": START},
            ["OUTPUT"])
        if layers is None:
            return [_report("Calculate Seabed Length runs", False, " | ".join(log[-3:]))]
        rows = sorted((f["kp_start"], f["kp_end"], f["segment_length_m"]) for f in layers[0].getFeatures())
        expect = [(START, 50.0), (50.0, 100.0), (100.0, START + total_km)]
        ok = len(rows) == 3 and all(abs(r[0] - e[0]) < 1e-9 and abs(r[1] - e[1]) < 1e-9 for r, e in zip(rows, expect))
        ok = ok and abs(rows[0][2] - (50.0 - START) * 1000.0) < 0.5
        return [_report("Calculate Seabed Length: KP intervals from the start KP to round KPs", ok,
                        str([(round(a, 3), round(b, 3), round(c, 1)) for a, b, c in rows])),
                _report("Calculate Seabed Length: the log states the KP basis and the start KP", _basis_logged(log))]
    finally:
        _remove(layer, raster)


def test_export_chartlets() -> List[bool]:
    """KP extraction mode: the section is clipped to the start KP (file names show the KPs used)."""
    from ..processing.export_kp_section_chartlets_algorithm import ExportKPSectionChartletsAlgorithm

    layer = route_layer()
    table = QgsVectorLayer("None?field=start_kp:double&field=end_kp:double", "sections", "memory")
    features = []
    for start, end in ((10.0, 20.0), (30.0, 40.0)):
        feature = QgsFeature(table.fields())
        feature.setAttributes([start, end])
        features.append(feature)
    table.dataProvider().addFeatures(features)
    _add(table)
    folder = os.path.join(_temp_dir(), "chartlets")
    try:
        layers, log = run_algorithm(ExportKPSectionChartletsAlgorithm(), {
            "RANGES": table, "RPL_LINE": layer, "START_KP_FIELD": "start_kp", "END_KP_FIELD": "end_kp",
            "USE_PROJECT_LAYERS": False, "VECTOR_LAYERS": [layer], "OUT_FOLDER": folder,
            "IMAGE_WIDTH": 64, "IMAGE_HEIGHT": 64, "FILE_PREFIX": "t", "ADD_SCALEBAR": False,
            "ADD_NORTH_ARROW": False, "START_KP": START}, [])
        files = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        return [_report("Export KP section chartlets: sections clipped to the route's start KP",
                        layers is not None and files == ["t_12.345-20.png", "t_30-40.png"], str(files)),
                _report("Export KP section chartlets: the log states the KP basis and the start KP",
                        _basis_logged(log))]
    finally:
        _remove(layer, table)


# --------------------------------------------------------------------------
# Other Tools
# --------------------------------------------------------------------------


def test_place_outline_along_route() -> List[bool]:
    from ..processing.place_outline_along_route_algorithm import PlaceOutlineAlongRouteAlgorithm

    route = legs_layer()
    outline = QgsVectorLayer("Polygon?crs=EPSG:3857", "outline", "memory")
    feature = QgsFeature(outline.fields())
    feature.setGeometry(QgsGeometry.fromPolygonXY([[
        QgsPointXY(-10, -10), QgsPointXY(10, -10), QgsPointXY(10, 10), QgsPointXY(-10, 10), QgsPointXY(-10, -10)]]))
    outline.dataProvider().addFeatures([feature])
    _add(outline)
    try:
        layers, log = run_algorithm(PlaceOutlineAlongRouteAlgorithm(), {
            "OUTLINE": outline, "ROUTE": route, "KPS": "42.345", "START_KP": START}, ["OUTPUT"])
        if layers is None:
            return [_report("Place Outline Along Route runs", False, " | ".join(log[-3:]))]
        placed = next(layers[0].getFeatures())
        centre = placed.geometry().centroid().asPoint()
        off = geodesic_offset_m(centre, 30000.0)
        return [_report("Place Outline Along Route: placed at start KP + chainage on the geodesic, SeqNo order",
                        off < ON_PATH_M and abs(placed["kp"] - 42.345) < 1e-9, f"{off * 1000:.1f} mm off"),
                _report("Place Outline Along Route: the log states the KP basis and the start KP", _basis_logged(log))]
    finally:
        _remove(route, outline)


def run_all() -> List[bool]:
    results: List[bool] = []
    for test in (test_place_kp_points, test_place_kp_points_from_csv, test_place_single_kp_point,
                 test_place_single_kp_point_grid, test_nearest_kp,
                 test_kp_range_highlighter, test_kp_range_csv, test_kp_range_rule_based,
                 test_kp_range_depth_slope_summary, test_extract_ac_points, test_crossing_points, test_area_listing,
                 test_corridor_proximity_listing, test_translate_kp, test_route_comparison,
                 test_seabed_length_intervals, test_export_chartlets, test_place_outline_along_route):
        try:
            results.extend(test())
        except Exception as exc:  # report, keep going
            import traceback

            traceback.print_exc()
            results.append(_report(test.__name__, False, repr(exc)))
    print(f"\n{sum(results)}/{len(results)} KP Processing method checks passed.")
    return results


if __name__ == "__main__":
    run_all()
