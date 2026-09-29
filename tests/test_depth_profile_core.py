"""Depth Profile computation core: golden-output equivalence and the task.

The Depth Profile dock used to sample, compute slopes/side slopes and seabed
length on the GUI thread. That computation now lives in
``depth_profile_core`` and runs in a QgsTask. These checks pin it to golden
outputs captured from the pre-refactor dock on synthetic inputs covering
every mode that affects numbers (raster composite / per-raster / adaptive /
auto-limit, contours with one or two layers, side slopes on projected and
geographic routes, slope window, invert slope sign, 'auto' elevation
inference, multi-part and drawn routes, no-coverage statuses).

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import json
import math
import os
from typing import Dict, List

REQUIRES_QGIS = True

_X0, _Y0 = 500000.0, 6000000.0
_SQRT2 = math.sqrt(2.0)
GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "depth_profile_golden.json")


# ---------------------------------------------------------------------------
# Synthetic seabed: contour levels run at 45 degrees to the route so both
# the route and its cross transects cut them obliquely.
# ---------------------------------------------------------------------------

def _w(u, v):
    return (u - v) / _SQRT2


def _contour_depth(w):
    return 100.0 + 0.05 * w + 4.0 * math.sin(w / 120.0)


def _level_w(level: float) -> float:
    """w at which the (monotonic) contour depth equals ``level``."""
    lo, hi = -4000.0, 6000.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _contour_depth(mid) < level:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _write_raster(path: str, pixel: float, bounds, sign=1.0, holes=()) -> None:
    import numpy as np
    from osgeo import gdal, osr
    u_min, u_max, v_min, v_max = bounds
    width, height = int(round((u_max - u_min) / pixel)), int(round((v_max - v_min) / pixel))
    ds = gdal.GetDriverByName("GTiff").Create(path, width, height, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((_X0 + u_min, pixel, 0.0, _Y0 + v_max, 0.0, -pixel))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32630)
    ds.SetProjection(srs.ExportToWkt())
    u = u_min + (np.arange(width) + 0.5) * pixel
    v = v_max - (np.arange(height) + 0.5) * pixel
    uu, vv = np.meshgrid(u, v)
    w = (uu - vv) / _SQRT2
    depth = 100.0 + 0.05 * w + 4.0 * np.sin(w / 120.0) + 1.5 * np.cos(vv / 45.0)
    data = (sign * depth).astype(np.float32)
    for hu0, hu1, hv0, hv1 in holes:
        data[(uu >= hu0) & (uu <= hu1) & (vv >= hv0) & (vv <= hv1)] = -9999.0
    band = ds.GetRasterBand(1)
    band.WriteArray(data)
    band.SetNoDataValue(-9999)
    ds = None


class SyntheticData:
    """Rasters, contours and routes shared by the golden capture and tests."""

    def __init__(self, temp: str):
        from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
                               QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer)
        from ..bathymetry_sampling import PREFIX

        self.project = QgsProject.instance()
        utm = QgsCoordinateReferenceSystem("EPSG:32630")
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")

        def raster(name, pixel, bounds, sign=1.0, holes=(), vertical="depth"):
            path = os.path.join(temp, f"{name}.tif")
            _write_raster(path, pixel, bounds, sign, holes)
            layer = QgsRasterLayer(path, f"dpc_{name}")
            assert layer.isValid(), path
            if vertical:
                layer.setCustomProperty(PREFIX + "vertical", vertical)
            return layer

        # Fine raster has a no-data hole on the route and ends mid-route, so
        # the composite switches source (seams) and has gaps.
        self.fine = raster("fine", 5.0, (-150, 900, -350, 350), holes=((300, 360, 40, 120),))
        self.coarse = raster("coarse", 20.0, (-400, 1700, -500, 500))
        # Negative elevations with the 'auto' convention (no property set).
        self.elev = raster("elev", 10.0, (-300, 1500, -400, 400), sign=-1.0, vertical=None)

        def line_layer(name, crs, lines):
            layer = QgsVectorLayer(f"LineString?crs={crs}&field=name:string", name, "memory")
            features = []
            for label, pts in lines:
                f = QgsFeature(layer.fields())
                f.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in pts]))
                f.setAttributes([label])
                features.append(f)
            layer.dataProvider().addFeatures(features)
            layer.updateExtents()
            return layer

        route_pts = [(_X0, _Y0), (_X0 + 600, _Y0 + 150), (_X0 + 1300, _Y0 - 50)]
        self.route_utm = line_layer("dpc_route_utm", "EPSG:32630", [("main", route_pts)])
        to_wgs = QgsCoordinateTransform(utm, wgs, self.project)
        geo_pts = [to_wgs.transform(QgsPointXY(x, y)) for x, y in route_pts]
        self.route_geo = line_layer("dpc_route_geo", "EPSG:4326",
                                    [("main", [(p.x(), p.y()) for p in geo_pts])])
        self.route_multi = line_layer("dpc_route_multi", "EPSG:32630", [
            ("a", [(_X0, _Y0), (_X0 + 500, _Y0 + 100)]),
            ("b", [(_X0 + 700, _Y0 + 50), (_X0 + 1200, _Y0 + 200)])])
        self.route_far = line_layer("dpc_route_far", "EPSG:32630",
                                    [("far", [(_X0 + 5000, _Y0 + 5000), (_X0 + 5500, _Y0 + 5000)])])
        self.drawn_points = [QgsPointXY(_X0 + 50, _Y0 - 100), QgsPointXY(_X0 + 400, _Y0 + 60),
                             QgsPointXY(_X0 + 850, _Y0 + 20)]

        def contours(name, levels):
            layer = QgsVectorLayer("LineString?crs=EPSG:32630&field=depth:double", name, "memory")
            features = []
            for level in levels:
                w = _level_w(level)
                cx, cy = _X0 + w / _SQRT2, _Y0 - w / _SQRT2
                pts = [QgsPointXY(cx + s / _SQRT2, cy + s / _SQRT2) for s in (-1600, -800, 0, 800, 1600)]
                f = QgsFeature(layer.fields())
                f.setGeometry(QgsGeometry.fromPolylineXY(pts))
                f.setAttributes([float(level)])
                features.append(f)
            layer.dataProvider().addFeatures(features)
            layer.updateExtents()
            layer.setCustomProperty(PREFIX + "vertical", "depth")
            return layer

        # Major levels coincide with minor ones: coincident crossings collapse.
        self.minor = contours("dpc_minor", range(66, 182, 2))
        self.major = contours("dpc_major", range(70, 181, 10))

        self.layers = [self.fine, self.coarse, self.elev, self.route_utm, self.route_geo,
                       self.route_multi, self.route_far, self.minor, self.major]
        self.project.addMapLayers(self.layers)

    def remove(self):
        for layer in self.layers:
            self.project.removeMapLayer(layer.id())
        self.layers = []


# Every configuration that changes the numbers. Keys map onto dock controls.
SCENARIOS: List[Dict] = [
    dict(name="raster_fine", rasters=("fine",)),
    dict(name="raster_composite", rasters=("fine", "coarse"), per_raster=False),
    dict(name="raster_per_raster_reverse", rasters=("fine", "coarse"), per_raster=True, reverse=True),
    dict(name="raster_adaptive", rasters=("fine", "coarse"), adaptive=True, factor=1.5, interval=4),
    dict(name="raster_autolimit", rasters=("coarse",), interval=1, max_samples=1000, auto_limit=True),
    dict(name="raster_no_autolimit", rasters=("coarse",), interval=1, max_samples=1000, auto_limit=False),
    dict(name="raster_window_invert", rasters=("fine", "coarse"), window=60, invert=True, dual=True),
    dict(name="raster_side", rasters=("fine", "coarse"), side=60),
    dict(name="raster_side_geo", route="route_geo", rasters=("fine", "coarse"), side=40, dual=True),
    dict(name="raster_elev_auto_side", rasters=("elev",), side=50, invert=True),
    dict(name="raster_side_wide", rasters=("coarse",), side=300, interval=25),
    dict(name="raster_side_uncovered", rasters=("fine", "coarse"), side=600, interval=50),
    dict(name="raster_multipart", route="route_multi", rasters=("coarse",)),
    dict(name="raster_drawn", drawn=True, rasters=("fine",), interval=7),
    dict(name="raster_outside", route="route_far", rasters=("coarse",)),
    dict(name="raster_none_selected", rasters=()),
    dict(name="contour_single", source="Contours", contours=("minor",)),
    dict(name="contour_two", source="Contours", contours=("minor", "major"), dual=True),
    dict(name="contour_side", source="Contours", contours=("minor", "major"), side=50),
    dict(name="contour_side_geo", route="route_geo", source="Contours", contours=("minor",),
         side=40, window=100, invert=True),
    dict(name="contour_none", route="route_far", source="Contours", contours=("minor",)),
]


def configure_dock(dock, data: SyntheticData, sc: Dict) -> None:
    """Drive the dock's Setup controls for one scenario."""
    from qgis.PyQt.QtCore import Qt
    d = dock
    drawn = bool(sc.get("drawn"))
    d.temp_drawn_points = list(data.drawn_points) if drawn else []
    d.use_drawn_chk.setChecked(drawn)
    route = getattr(data, sc.get("route", "route_utm"))
    d.line_layer_combo.setCurrentIndex(d.line_layer_combo.findData(route.id()))
    d.selected_only_chk.setChecked(False)
    d.source_type_combo.setCurrentText(sc.get("source", "Raster"))
    wanted = {getattr(data, name).id() for name in sc.get("rasters", ())}
    for i in range(d.raster_layer_list.count()):
        item = d.raster_layer_list.item(i)
        checked = item.data(Qt.ItemDataRole.UserRole) in wanted
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    d.plot_rasters_separately_chk.setChecked(sc.get("per_raster", True))
    names = list(sc.get("contours", ("minor",)))
    d.contour_layer_combo.setCurrentIndex(d.contour_layer_combo.findData(getattr(data, names[0]).id()))
    d.populate_depth_fields_1()
    d.depth_field_combo.setCurrentText("depth")
    if len(names) > 1:
        d.contour_layer_combo2.setCurrentIndex(d.contour_layer_combo2.findData(getattr(data, names[1]).id()))
        d.populate_depth_fields_2()
        d.depth_field_combo2.setCurrentText("depth")
    else:
        d.contour_layer_combo2.setCurrentIndex(0)
    d.interval_spin.setValue(sc.get("interval", 10))
    d.adaptive_interval_chk.setChecked(sc.get("adaptive", False))
    d.adaptive_interval_factor.setValue(sc.get("factor", 1.0))
    d.max_samples_spin.setValue(sc.get("max_samples", 50000))
    d.auto_limit_chk.setChecked(sc.get("auto_limit", True))
    d.slope_window_spin.setValue(sc.get("window", 0))
    d.side_slope_chk.setChecked(bool(sc.get("side")))
    d.side_slope_search_spin.setValue(sc.get("side") or 200)
    d.side_slope_plot_chk.setChecked(True)
    d.variable_combo.setCurrentText("Depth (m)")
    d.dual_plot_chk.setChecked(sc.get("dual", False))
    d.reverse_kp_chk.setChecked(sc.get("reverse", False))
    d.invert_kp_axis_chk.setChecked(False)
    d.invert_slope_chk.setChecked(sc.get("invert", False))
    d.kp_axis_combo.setCurrentIndex(d.kp_axis_combo.findData("line"))
    d.ve_spin.setValue(0)


def build_core_request(data: SyntheticData, sc: Dict):
    """The same scenario as :func:`configure_dock`, built with the core API
    directly (no dock): route, parameter and source snapshot."""
    from ..depth_profile_core import (CONTOURS, RASTER, ProfileParams, build_request, route_from_features,
                                      route_from_points)
    from ..kp_range_utils import make_kp_distance_area
    project = data.project
    context = project.transformContext()
    if sc.get("drawn"):
        distance = make_kp_distance_area(project.crs(), context, project=project)
        route, status = route_from_points(data.drawn_points, project.crs(), distance)
    else:
        layer = getattr(data, sc.get("route", "route_utm"))
        features = [f for f in layer.getFeatures() if f.hasGeometry() and not f.geometry().isEmpty()]
        distance = make_kp_distance_area(layer.sourceCrs(), context, project=project)
        route, status = route_from_features(features, layer.sourceCrs(), distance)
    assert route is not None and status is None, status
    params = ProfileParams(
        mode=CONTOURS if sc.get("source") == "Contours" else RASTER,
        interval_m=sc.get("interval", 10), adaptive=sc.get("adaptive", False),
        adaptive_factor=float(sc.get("factor", 1.0)), max_samples=sc.get("max_samples", 50000),
        auto_limit=sc.get("auto_limit", True), per_raster=sc.get("per_raster", True),
        slope_window_m=float(sc.get("window", 0)), invert_slope=sc.get("invert", False),
        side_slopes=bool(sc.get("side")), side_search_m=float(sc.get("side") or 200))
    return build_request(route, params, context,
                         raster_layers=[getattr(data, n) for n in sc.get("rasters", ())],
                         contour_layers=[(getattr(data, n), "depth") for n in sc.get("contours", ("minor",))])


# ---------------------------------------------------------------------------
# Capture + fingerprints (the golden file stores fingerprints, not arrays)
# ---------------------------------------------------------------------------

STATION_ARRAYS = ("kp_values", "depth_values", "depth_cell_m", "slope_deg", "slope_pct", "slope_baseline_m",
                  "side_slope_deg", "side_local_max_deg", "side_slope_pct", "side_port_depth",
                  "side_starboard_depth", "side_cross_span_m")
SEGMENT_FIELDS = ("kp_from", "kp_to", "depth_from", "depth_to", "slope_deg", "slope_pct", "seabed_length",
                  "side_slope_deg", "side_slope_pct", "port_depth", "starboard_depth", "cross_span_m")
# A layer id: depends on which other layers the test process has loaded.
IGNORED_SETTINGS = ("DepthProfile/kp_ref_layer",)
REL_TOL = 1e-9


class RecordingBar:
    """Message bar stand-in recording (title, text, level, duration)."""

    def __init__(self):
        self.log = []

    def pushMessage(self, title, text, level=None, duration=None):  # noqa: N802 (Qt API name)
        from ..qgis_compat import MESSAGE_CRITICAL, MESSAGE_INFO, MESSAGE_WARNING
        name = {MESSAGE_INFO: "info", MESSAGE_WARNING: "warning", MESSAGE_CRITICAL: "critical"}.get(level, str(level))
        self.log.append([title, text, name, duration])


def _num(value):
    return None if value is None else float(value)


def depth_profile_settings() -> Dict[str, str]:
    from qgis.PyQt.QtCore import QSettings
    settings = QSettings()
    return {key: str(settings.value(key)) for key in sorted(settings.allKeys())
            if key.startswith("DepthProfile/") and key not in IGNORED_SETTINGS}


def capture_dock(dock, messages) -> Dict:
    """Everything one generation produced, in the golden capture layout."""
    p = dock.profile
    arrays = {name: [_num(v) for v in getattr(p, name)] for name in STATION_ARRAYS}
    arrays["depth_source_ids"] = [None if v is None else os.path.basename(v) for v in p.depth_source_ids]
    arrays["display_kp"] = [float(v) for v in dock._display_kp()]
    segments = p.segments()
    for name in SEGMENT_FIELDS:
        arrays["segment_" + name] = [_num(getattr(s, name)) for s in segments]
    axes = dock.figure.get_axes()
    route = p.route
    return {
        "status": dock._status_msg,
        "plot_status": dock.plot_status_label.text(),
        "titles": [str(ax.plot_item.titleLabel.text) for ax in axes],
        "axes": len(axes),
        "depth_axis": dock._depth_axis is not None,
        "messages": [list(m) for m in messages],
        "settings": depth_profile_settings(),
        "line_length": route.line_length if route is not None else None,
        "parts": len(route.line_parts) if route is not None else 0,
        "arrays": arrays,
        "scalars": {k: getattr(p, k) for k in ("seabed_length", "seabed_covered_m", "seabed_elongation_ratio")},
        "raster_series": [[r["name"], [_num(v) for v in r["depths"]]] for r in p.raster_series],
        "measure_series": [m["name"] for m in dock.measure.series],
    }


def _g(value):
    return None if value is None else float(f"{float(value):.12g}")


def _fp_numbers(values) -> List:
    """[count, None-mask runs, sum, index-weighted sum, sum of squares]."""
    runs, prev, count = [], None, 0
    for v in values:
        flag = "n" if v is None else "v"
        if flag == prev:
            count += 1
        else:
            if prev is not None:
                runs.append(f"{prev}{count}")
            prev, count = flag, 1
    if prev is not None:
        runs.append(f"{prev}{count}")
    nums = [(i, v) for i, v in enumerate(values) if v is not None]
    return [len(values), "".join(runs), _g(sum(v for _i, v in nums)),
            _g(sum((i + 1) * v for i, v in nums)), _g(sum(v * v for _i, v in nums))]


def _fp_strings(values) -> List:
    runs = []
    for v in values:
        if runs and runs[-1][0] == v:
            runs[-1][1] += 1
        else:
            runs.append([v, 1])
    return runs


def fingerprint(capture: Dict, previous_settings: Dict) -> Dict:
    """Compact, tolerance-comparable summary of a capture.

    Settings are stored as the change from the previous scenario, so the
    file pins exactly which settings each run persisted.
    """
    settings = {k: v for k, v in capture["settings"].items() if k not in IGNORED_SETTINGS}
    out = {k: capture[k] for k in ("status", "plot_status", "titles", "axes", "depth_axis", "messages",
                                   "parts", "measure_series")}
    out["line_length"] = _g(capture["line_length"])
    out["scalars"] = {k: _g(v) for k, v in capture["scalars"].items()}
    out["arrays"] = {k: _fp_strings(v) if k == "depth_source_ids" else _fp_numbers(v)
                     for k, v in sorted(capture["arrays"].items())}
    out["raster_series"] = [[name, _fp_numbers(depths)] for name, depths in capture["raster_series"]]
    out["settings_changed"] = {k: v for k, v in settings.items() if previous_settings.get(k) != v}
    return out


def diff(expected, actual, path="") -> List[str]:
    """Paths where two fingerprints differ (floats within REL_TOL)."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        out = []
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                out.append(f"{path}/{key}: missing")
            else:
                out.extend(diff(expected[key], actual[key], f"{path}/{key}"))
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: length {len(expected)} != {len(actual)}"]
        out = []
        for i, (a, b) in enumerate(zip(expected, actual)):
            out.extend(diff(a, b, f"{path}[{i}]"))
        return out
    if isinstance(expected, float) and isinstance(actual, (int, float)) and not isinstance(actual, bool):
        if abs(expected - actual) <= REL_TOL * max(1.0, abs(expected), abs(actual)):
            return []
        return [f"{path}: {expected!r} != {actual!r}"]
    return [] if expected == actual else [f"{path}: {expected!r} != {actual!r}"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


class _Env:
    def __init__(self, temp: str):
        from types import SimpleNamespace
        from qgis.core import QgsCoordinateReferenceSystem
        from qgis.gui import QgsMapCanvas
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtWidgets import QApplication, QMainWindow
        from ..depth_profile_dockwidget import DepthProfileDockWidget
        self.temp = temp
        self.data = SyntheticData(temp)
        self.project = self.data.project
        self.old_crs = self.project.crs()
        # Drawn lines live in the project CRS.
        self.project.setCrs(QgsCoordinateReferenceSystem("EPSG:32630"))
        self.root = QMainWindow()
        self.root.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:32630"))
        self.bar = RecordingBar()
        self.iface = SimpleNamespace(mainWindow=lambda: self.root, mapCanvas=lambda: self.canvas,
                                     messageBar=lambda: self.bar)
        self.dock = DepthProfileDockWidget(self.iface)
        self.root.resize(1400, 900)
        self.root.show()
        self.dock.show()
        self.dock.populate_layer_combos()
        QApplication.processEvents()
        self.results = {}

    def close(self):
        from qgis.PyQt.QtWidgets import QApplication
        self.dock.close()
        self.dock.deleteLater()
        self.root.close()
        self.canvas.close()
        self.data.remove()
        self.project.setCrs(self.old_crs)
        QApplication.processEvents()


def test_dock_matches_golden(env: _Env) -> bool:
    """The background-task dock reproduces the pre-refactor dock exactly:
    arrays, statuses, messages, plot titles and persisted settings."""
    from .test_depth_profile_dock import wait_for_generation
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        golden = json.load(f)["scenarios"]
    previous = {}
    failures = []
    for sc in SCENARIOS:
        configure_dock(env.dock, env.data, sc)
        env.bar.log.clear()
        env.dock.generate_profile()
        if not wait_for_generation(env.dock):
            failures.append((sc["name"], ["timed out"]))
            continue
        capture = capture_dock(env.dock, env.bar.log)
        env.results[sc["name"]] = env.dock.profile
        mismatches = diff(golden.get(sc["name"], {}), fingerprint(capture, previous))
        previous = capture["settings"]
        if mismatches:
            failures.append((sc["name"], mismatches[:4]))
    return _result(f"dock output matches the pre-refactor golden for {len(SCENARIOS)} scenarios",
                   not failures and len(golden) == len(SCENARIOS), str(failures[:3]))


def test_core_direct_equals_dock(env: _Env) -> bool:
    """run_profile on a directly built request equals the dock's result
    bit for bit: the dock only orchestrates, all numbers come from the core."""
    from ..depth_profile_core import run_profile
    fields = STATION_ARRAYS + ("depth_source_ids", "raster_series", "seabed_length", "seabed_covered_m",
                               "seabed_elongation_ratio", "status")
    failures = []
    for sc in SCENARIOS:
        dock_result = env.results.get(sc["name"])
        if dock_result is None:
            failures.append((sc["name"], "no dock result"))
            continue
        result = run_profile(build_core_request(env.data, sc))
        bad = [f for f in fields if getattr(result, f) != getattr(dock_result, f)]
        if result.segments() != dock_result.segments():
            bad.append("segments")
        # The dock adds only its own route warning (multi-part) before these.
        if result.messages != dock_result.messages:
            bad.append("messages")
        if bad:
            failures.append((sc["name"], bad))
    return _result("core run_profile == dock result (exact) for every scenario", not failures, str(failures[:3]))


def test_cancel_semantics(env: _Env) -> bool:
    """Along-route cancel raises (no result); side-slope cancel stops early,
    keeps the stations done and the along-route profile, and says so."""
    from ..depth_profile_core import ProfileCancelled, run_profile
    sc = next(s for s in SCENARIOS if s["name"] == "raster_side")
    try:
        run_profile(build_core_request(env.data, sc), cancel=lambda: True)
        along_ok = False
    except ProfileCancelled:
        along_ok = True
    full = run_profile(build_core_request(env.data, sc))
    state = {"side": False}

    def progress(fraction):
        state["side"] = state["side"] or fraction > 0.6  # well into the side-slope half

    partial = run_profile(build_core_request(env.data, sc), cancel=lambda: state["side"], progress=progress)
    done = [i for i, v in enumerate(partial.side_slope_pct) if v is not None]
    ok = along_ok and partial.side_slopes_cancelled and partial.side_slopes_ran
    ok = ok and ("Side slope canceled.", "warning", 4) in partial.messages
    ok = ok and partial.kp_values == full.kp_values and partial.depth_values == full.depth_values
    ok = ok and partial.slope_deg == full.slope_deg and bool(done) and max(done) < len(full.kp_values) // 2
    ok = ok and all(partial.side_slope_deg[i] == full.side_slope_deg[i] for i in range(max(done) + 1))
    return _result("cancel: along-route raises, side slopes keep the stations done", ok,
                   f"along={along_ok} side stations done={len(done)}/{len(full.kp_values)}")


def test_snapshot_survives_layer_removal(env: _Env) -> bool:
    """The worker reads only the snapshot: removing (deleting) every layer
    after snapshotting changes nothing, and the raster files are released
    once the snapshot is dropped (cloned providers are freed)."""
    import gc
    import tempfile
    from ..depth_profile_core import run_profile
    temp = tempfile.mkdtemp(prefix="dpc_snapshot_")
    data = SyntheticData(temp)
    names = ("raster_side_geo", "contour_side_geo", "raster_elev_auto_side")
    scenarios = [s for s in SCENARIOS if s["name"] in names]
    try:
        before = [run_profile(build_core_request(data, sc)) for sc in scenarios]
        requests = [build_core_request(data, sc) for sc in scenarios]
    finally:
        data.remove()
    gc.collect()
    after = [run_profile(request) for request in requests]
    same = all(a.kp_values == b.kp_values and a.depth_values == b.depth_values
               and a.side_slope_deg == b.side_slope_deg and a.slope_deg == b.slope_deg
               and a.seabed_length == b.seabed_length for a, b in zip(before, after))
    same = same and all(r.kp_values and any(v is not None for v in r.side_slope_deg) for r in after)
    del requests, before, after
    gc.collect()
    locked = []
    for name in os.listdir(temp):
        path = os.path.join(temp, name)
        try:
            os.rename(path, path + ".x")
            os.rename(path + ".x", path)
        except OSError:
            locked.append(name)
    return _result("snapshot runs after its layers are deleted; raster files released afterwards",
                   same and not locked, f"same={same} locked={locked}")


def test_task_progress(env: _Env) -> bool:
    """DepthProfileTask reports throttled, increasing progress up to 100 %."""
    from ..depth_profile_core import DepthProfileTask
    sc = next(s for s in SCENARIOS if s["name"] == "raster_side")
    task = DepthProfileTask(build_core_request(env.data, sc), lambda _t: None)
    seen = []
    task.progressChanged.connect(seen.append)
    ok = task.run() and task.result is not None and not task.cancelled and task.error is None
    ok = ok and seen and seen[-1] == 100.0 and all(b >= a for a, b in zip(seen, seen[1:]))
    ok = ok and 10 <= len(seen) <= 205
    task.request = task.result = None
    return _result("task progress: monotonic, throttled, ends at 100", bool(ok),
                   f"updates={len(seen)} last={seen[-1] if seen else None}")


def test_seams_and_segments() -> bool:
    """Slope and seabed length never bridge a change of supplying raster;
    the CSV segment table omits the seam and sums to the seabed length."""
    from ..depth_profile_core import ProfileResult, compute_seabed_length, compute_slopes
    r = ProfileResult(kp_values=[i / 1000 for i in range(0, 181, 10)],
                      depth_values=[109.25 + i * .1 for i in range(0, 181, 10)],
                      depth_source_ids=['a'] * 10 + ['b'] * 9, depth_cell_m=[10] * 19)
    compute_slopes(r, 0.0, False)
    compute_seabed_length(r)
    segments = r.segments()
    ok = len(r.slope_deg) == 19 and r.slope_deg[10] is None and r.slope_deg[5] is not None
    ok = ok and abs(r.seabed_covered_m - 170) < 1e-6 and len(segments) == 17
    ok = ok and all(not (s.kp_from < 0.095 < s.kp_to) for s in segments)
    ok = ok and abs(sum(s.seabed_length for s in segments) - r.seabed_length) < 1e-9
    compute_slopes(r, 0.0, True)
    ok = ok and r.slope_deg[5] is not None and r.slope_deg[5] > 0  # inverted: deepening is +ve
    return _result("seams: no slope or seabed length across a source change; segments sum", ok)


def run_all() -> List[bool]:
    import gc
    import tempfile
    from qgis.PyQt.QtCore import QSettings
    results = [test_seams_and_segments()]
    old_format = QSettings.defaultFormat()
    temp = tempfile.mkdtemp(prefix="dpc_")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
    env = None
    try:
        env = _Env(temp)
        for test in (test_dock_matches_golden, test_core_direct_equals_dock, test_cancel_semantics,
                     test_snapshot_survives_layer_removal, test_task_progress):
            try:
                results.append(test(env))
            except Exception as exc:  # report, keep going
                import traceback
                traceback.print_exc()
                results.append(_result(test.__name__, False, repr(exc)))
    finally:
        if env is not None:
            env.close()
        QSettings.setDefaultFormat(old_format)
        del env
        gc.collect()
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()
