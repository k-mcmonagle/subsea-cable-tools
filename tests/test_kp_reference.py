# -*- coding: utf-8 -*-
"""QGIS checks: one KP definition and RPL-referenced burial plans.

* RouteFrame start-KP offset; shared ordered route builder; WGS84 always.
* A plan on an RPL starting at KP 10 is renumbered once (logged).
* Moving a plan to a new RPL revision keeps items at their seabed position.
* The background stated-KP check and the newer-revision notice.
"""

from __future__ import annotations

import os
import tempfile

from qgis.core import (QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject,
                       QgsVectorLayer)

from ..burial import schema
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..kp_geo_utils import RouteFrame, ordered_route_geometry
from ..kp_range_utils import make_distance_area
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_INT


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _da():
    from qgis.core import QgsCoordinateReferenceSystem
    return make_distance_area(QgsCoordinateReferenceSystem("EPSG:4326"),
                              QgsProject.instance().transformContext())


# ------------------------------------------------------------ fake Workbench
def _rpl_layers(name, vertices, start_kp=0.0, stated_bias_m=0.0):
    """Workbench-format points + lines memory layers for ``vertices``."""
    da = _da()
    points = QgsVectorLayer("Point?crs=EPSG:4326", name + "_points", "memory")
    points.dataProvider().addAttributes([QgsField("SeqNo", FIELD_TYPE_INT),
                                         QgsField("DistCumulative", FIELD_TYPE_DOUBLE)])
    points.updateFields()
    lines = QgsVectorLayer("LineString?crs=EPSG:4326", name + "_lines", "memory")
    lines.dataProvider().addAttributes([QgsField("SeqNo", FIELD_TYPE_INT)])
    lines.updateFields()
    kp, feats, legs = start_kp, [], []
    for i, (x, y) in enumerate(vertices):
        if i:
            kp += da.measureLine(QgsPointXY(*vertices[i - 1]), QgsPointXY(x, y)) / 1000.0
        f = QgsFeature(points.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f.setAttributes([i + 1, kp + (stated_bias_m / 1000.0 if i == len(vertices) - 1 else 0.0)])
        feats.append(f)
    # Legs added in reverse order: SeqNo, not layer order, defines the route.
    for i in reversed(range(len(vertices) - 1)):
        g = QgsFeature(lines.fields())
        g.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(*vertices[i]),
                                                  QgsPointXY(*vertices[i + 1])]))
        g.setAttributes([i + 1])
        legs.append(g)
    points.dataProvider().addFeatures(feats)
    lines.dataProvider().addFeatures(legs)
    return points, lines


class _FakeWorkbench:
    gpkg_path = ""

    def __init__(self):
        self.rows, self.layers = {}, {}

    def add(self, rpl_id, rev, vertices, start_kp=0.0, bias=0.0, route_id="route-1"):
        points, lines = _rpl_layers(rpl_id, vertices, start_kp, bias)
        self.layers[rpl_id + "_p"], self.layers[rpl_id + "_l"] = points, lines
        self.rows[rpl_id] = {"rpl_id": rpl_id, "name": "Test cable", "rev_label": rev,
                             "route_id": route_id, "points_layer": rpl_id + "_p",
                             "lines_layer": rpl_id + "_l", "modified_utc": "t"}
        return self.rows[rpl_id]

    def get_rpl(self, rpl_id):
        return self.rows.get(rpl_id)

    def list_rpls(self):
        return list(self.rows.values())

    def open_layer(self, name):
        return self.layers.get(name)

    def latest_revision(self, route_id):
        rows = [r for r in self.rows.values() if r.get("route_id") == route_id]
        return rows[-1] if rows else None


_ROUTE_A = [(0.0, 50.0), (0.0, 50.02), (0.01, 50.04), (0.01, 50.06)]
# Rev B: same corridor, the middle leg re-routed ~70 m east.
_ROUTE_B = [(0.0, 50.0), (0.0, 50.02), (0.011, 50.03), (0.01, 50.04), (0.01, 50.06)]


# ------------------------------------------------------------ core KP tests
def test_route_frame_start_kp() -> bool:
    da = _da()
    geom = QgsGeometry.fromPolylineXY([QgsPointXY(0.0, 50.0), QgsPointXY(0.0, 50.1)])
    base = RouteFrame.from_source([geom], da)
    frame = RouteFrame.from_source([geom], da, start_kp_km=10.0)
    length = base.total_length_km
    start = frame.point_at_kp(10.0)
    mid = frame.point_at_kp(10.0 + length / 2)
    ok = start is not None and abs(start.y() - 50.0) < 1e-9 and frame.point_at_kp(9.0) is None
    ok = ok and abs(frame.kp_at_point(mid).kp_km - (10.0 + length / 2)) < 1e-6
    ok = ok and abs(frame.end_kp_km - (10.0 + length)) < 1e-9 and frame.clamp_kp(99) == frame.end_kp_km
    seg = frame.extract_segment(10.5, 11.0)
    ok = ok and seg is not None and abs(da.measureLength(seg) - 500.0) < 0.5
    checks = {
        "start": start is not None and abs(start.y() - 50.0) < 1e-9,
        "before_start": frame.point_at_kp(9.0) is None,
        "mid": abs(frame.kp_at_point(mid).kp_km - (10.0 + length / 2)) < 1e-6,
        "end": abs(frame.end_kp_km - (10.0 + length)) < 1e-9 and frame.clamp_kp(99) == frame.end_kp_km,
        "segment": seg is not None and abs(da.measureLength(seg) - 500.0) < 0.5,
        "offset": abs(base.kp_at_point(mid).kp_km - (frame.kp_at_point(mid).kp_km - 10.0)) < 1e-9,
    }
    ok = all(checks.values())
    return _result("RouteFrame start KP: point/KP/segment/clamp are offset consistently", ok,
                   f"end={frame.end_kp_km:.3f} {checks}")


def test_ordered_route_and_wgs84() -> bool:
    _points, lines = _rpl_layers("ord", _ROUTE_A)
    geom = ordered_route_geometry(list(lines.getFeatures()))
    line = geom.asPolyline() if not geom.isMultipart() else []
    ok = len(line) == len(_ROUTE_A) and all(
        abs(p.x() - x) < 1e-12 and abs(p.y() - y) < 1e-12 for p, (x, y) in zip(line, _ROUTE_A))
    project = QgsProject.instance()
    saved = project.ellipsoid()
    try:
        project.setEllipsoid("NONE")
        planar_setting = _da().ellipsoid()
        project.setEllipsoid("EPSG:7008")   # Clarke 1866
        clarke_setting = _da().ellipsoid()
    finally:
        project.setEllipsoid(saved)
    ok = ok and planar_setting == "WGS84" and clarke_setting == "WGS84"
    return _result("shared route builder follows SeqNo and joins legs; KP always on WGS84",
                   ok, f"{len(line)} vertices, ellipsoid {clarke_setting}")


# ------------------------------------------------------------ burial plans
def _plan_on(model, wb, rpl_id, datum=True):
    row = wb.get_rpl(rpl_id)
    model.workbench_store = wb
    model.update_plan({"rpl_id": rpl_id, "rpl_name": row["name"], "rpl_revision": row["rev_label"],
                       "rpl_gpkg_path": "", "rpl_fingerprint": ""}, reason="route")
    return model


def test_legacy_plan_renumbered_to_rpl_start(temp: str) -> bool:
    wb = _FakeWorkbench()
    wb.add("A", "Rev A", _ROUTE_A, start_kp=10.0)
    store = BurialStore(os.path.join(temp, "legacy.gpkg"))
    store.migrate()
    model = PlanModel(store)
    plan_id = model.create_plan("Legacy", "plough")
    model.workbench_store = wb
    # A plan stored before start-KP support: KPs are chainage from 0 and
    # there is no kp_datum. Write it directly, then reopen.
    model.plan.update({"rpl_id": "A", "rpl_name": "Test cable", "rpl_revision": "Rev A",
                       "scope_start_kp": 0.0, "scope_end_kp": 6.0})
    store.save_plan(model.plan)
    store.save_events(plan_id, [
        {"event_id": "e1", "plan_id": plan_id, "generation_id": "", "seq": 0,
         "event_type": schema.EVENT_BURIAL_START, "kp": 1.0, "end_kp": None, "lat": None,
         "lon": None, "depth_m": None, "source": "manual", "status": "candidate",
         "locked": 0, "notes": ""},
        {"event_id": "e2", "plan_id": plan_id, "generation_id": "", "seq": 1,
         "event_type": schema.EVENT_BURIAL_END, "kp": 3.0, "end_kp": None, "lat": None,
         "lon": None, "depth_m": None, "source": "manual", "status": "candidate",
         "locked": 0, "notes": ""}])
    reopened = PlanModel(store)
    reopened.workbench_store = wb
    ok = reopened.load_plan(plan_id)
    kps = sorted(float(e["kp"]) for e in reopened.events)
    ok = ok and kps == [11.0, 13.0] and reopened.route.start_kp_km == 10.0
    ok = ok and (reopened.plan["scope_start_kp"], reopened.plan["scope_end_kp"]) == (10.0, 16.0)
    ok = ok and reopened.kp_datum_start() == 10.0
    ok = ok and any("renumbered" in text for text, _lv in reopened.route_messages())
    # Positions follow: KP 11.000 is 1 km from the RPL start.
    first = next(e for e in reopened.events if e["event_id"] == "e1")
    ok = ok and abs(_da().measureLine(QgsPointXY(0.0, 50.0),
                                      QgsPointXY(first["lon"], first["lat"])) - 1000.0) < 0.5
    # Reopening again changes nothing (idempotent).
    again = PlanModel(store)
    again.workbench_store = wb
    again.load_plan(plan_id)
    ok = ok and sorted(float(e["kp"]) for e in again.events) == [11.0, 13.0]
    return _result("legacy plan on an RPL starting at KP 10 is renumbered once (logged, idempotent)",
                   bool(ok), f"kps={kps}")


def test_move_plan_to_new_revision(temp: str) -> bool:
    wb = _FakeWorkbench()
    wb.add("A", "Rev A", _ROUTE_A)
    store = BurialStore(os.path.join(temp, "move.gpkg"))
    store.migrate()
    model = PlanModel(store)
    model.create_plan("Move", "plough")
    _plan_on(model, wb, "A")
    model.update_plan({"scope_start_kp": 0.0, "scope_end_kp": model.route.end_kp_km}, reason="scope")
    model.add_event(1.0, schema.EVENT_BURIAL_START)
    model.add_event(5.5, schema.EVENT_BURIAL_END)
    before = {e["event_id"]: (e["lon"], e["lat"], float(e["kp"])) for e in model.events}
    wb.add("B", "Rev B", _ROUTE_B)
    model._load_route()
    ok = model.newer_rpl is not None and model.newer_rpl["rpl_id"] == "B"
    ok = ok and any("newer revision" in text for text, _lv in model.route_messages())
    row = wb.get_rpl("B")
    kp_map, report, new_route = model.preview_route_change(row)
    result = model.change_route({"rpl_id": "B", "rpl_name": row["name"],
                                 "rpl_revision": row["rev_label"]}, kp_map)
    ok = ok and bool(result) and model.plan.get("rpl_id") == "B" and model.newer_rpl is None
    da = _da()
    moved = 0.0
    shifted = 0.0
    for e in model.events:
        lon, lat, kp = before[e["event_id"]]
        moved = max(moved, da.measureLine(QgsPointXY(lon, lat), QgsPointXY(e["lon"], e["lat"])))
        shifted = max(shifted, abs(float(e["kp"]) - kp) * 1000.0)
    # The KP-5.5 event lies past the re-routed leg: its KP grows by the
    # detour length but it stays on the same seabed spot.
    ok = ok and moved < 1.0 and shifted > 10.0
    last = model.store.list_change_log(model.plan_id)[-1]
    ok = ok and last.get("action") == "rereference_plan"
    return _result("moving a plan to a new RPL revision keeps seabed positions, updates KPs",
                   bool(ok), f"moved {moved:.2f} m, KP change {shifted:.1f} m; {report.summary()}")


def test_stated_kp_check(temp: str) -> bool:
    wb = _FakeWorkbench()
    wb.add("A", "Rev A", _ROUTE_A)
    wb.add("C", "Rev C", _ROUTE_A, bias=8.0, route_id="route-2")
    store = BurialStore(os.path.join(temp, "check.gpkg"))
    store.migrate()
    model = PlanModel(store)
    model.create_plan("Check", "plough")
    _plan_on(model, wb, "A")
    ok = model.kp_check is not None and model.kp_check.level == "ok"
    _plan_on(model, wb, "C")
    ok = ok and model.kp_check.level == "warn" and 7.5 < model.kp_check.max_diff_m < 8.5
    ok = ok and any("differ from the measured KPs" in t for t, _lv in model.route_messages())
    return _result("background stated-KP check: agreement is silent, 8 m drift warns",
                   bool(ok), model.kp_check.summary()[:80])


# ------------------------------------------------------------ cartesian (grid) KP
def test_grid_kp_matches_utm_chainage() -> bool:
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform
    from ..kp_range_utils import (GridDistanceArea, KP_MODE_CARTESIAN,
                                  make_kp_distance_area, set_kp_distance_settings)
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    ctx = QgsProject.instance().transformContext()
    # Auto zone (no grid CRS, geographic project): UTM 31N at lon 1-2 E.
    old_crs = QgsProject.instance().crs()
    QgsProject.instance().setCrs(wgs)
    try:
        set_kp_distance_settings(KP_MODE_CARTESIAN, "")
        grid = make_kp_distance_area(wgs, ctx)
        pts = [QgsPointXY(1.0, 55.0), QgsPointXY(1.3, 55.1), QgsPointXY(1.6, 55.3)]
        geom = QgsGeometry.fromPolylineXY(pts)
        frame = RouteFrame.from_source([geom], grid)
        utm = QgsCoordinateTransform(wgs, QgsCoordinateReferenceSystem("EPSG:32631"), ctx)
        proj = [utm.transform(p) for p in pts]
        planar = sum(((b.x() - a.x()) ** 2 + (b.y() - a.y()) ** 2) ** 0.5
                     for a, b in zip(proj, proj[1:])) / 1000.0
        geodesic = RouteFrame.from_source([geom], _da()).total_length_km
        ok = isinstance(grid, GridDistanceArea) and abs(frame.total_length_km - planar) < 1e-9
        ok = ok and abs(frame.total_length_km - geodesic) > 0.001      # scale factor shows
        ok = ok and grid.grid_crs().authid() == "EPSG:32631"
        # A projected project CRS is used as the grid when no CRS is chosen.
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:32631"))
        ok = ok and make_kp_distance_area(wgs, ctx).grid_crs().authid() == "EPSG:32631"
    finally:
        QgsProject.instance().setCrs(old_crs)
        set_kp_distance_settings("ellipsoidal", "")
    return _result("Cartesian KP = planar chainage in the grid CRS (UTM auto / project CRS)",
                   bool(ok), f"grid {frame.total_length_km:.4f} km vs geodesic {geodesic:.4f} km")


def test_plan_keeps_its_kp_mode(temp: str) -> bool:
    from ..kp_range_utils import KP_MODE_CARTESIAN, set_kp_distance_settings
    wb = _FakeWorkbench()
    wb.add("A", "Rev A", _ROUTE_A)
    store = BurialStore(os.path.join(temp, "mode.gpkg"))
    store.migrate()
    set_kp_distance_settings(KP_MODE_CARTESIAN, "EPSG:32631")
    try:
        model = PlanModel(store)
        model.create_plan("Grid plan", "plough")
    finally:
        set_kp_distance_settings("ellipsoidal", "")
    _plan_on(model, wb, "A")
    ok = model.kp_mode() == ("cartesian", "EPSG:32631")
    ok = ok and "EPSG:32631" in model.kp_mode_text()
    grid_len = model.route.total_length_km
    model.update_plan({"scope_start_kp": 0.0, "scope_end_kp": model.route.end_kp_km}, reason="s")
    model.add_event(1.0, schema.EVENT_BURIAL_START)
    model.add_event(5.0, schema.EVENT_BURIAL_END)
    before = {e["event_id"]: (e["lon"], e["lat"]) for e in model.events}
    # The global setting is geodesic again, yet the plan stays grid.
    again = PlanModel(store)
    again.workbench_store = wb
    again.load_plan(model.plan_id)
    ok = ok and again.kp_mode()[0] == "cartesian" and abs(again.route.total_length_km - grid_len) < 1e-12
    report = again.change_kp_mode("ellipsoidal", "")
    moved = max(_da().measureLine(QgsPointXY(*before[e["event_id"]]), QgsPointXY(e["lon"], e["lat"]))
                for e in again.events)
    ok = ok and bool(report) and again.kp_mode()[0] == "ellipsoidal" and moved < 0.05
    ok = ok and abs(again.route.total_length_km - grid_len) > 1e-4
    return _result("burial plan keeps its KP mode; switching re-measures KPs, positions stay",
                   bool(ok), f"moved {moved:.3f} m; {getattr(report, 'summary', lambda: report)()}")


def test_processing_default_follows_setting() -> bool:
    from ..kp_range_utils import (KP_MODE_CARTESIAN, add_distance_mode_parameter,
                                  set_kp_distance_settings)

    class _Alg:
        def __init__(self):
            self.params = []

        def tr(self, text):
            return text

        def addParameter(self, param):  # noqa: N802
            self.params.append(param)

    set_kp_distance_settings(KP_MODE_CARTESIAN, "")
    try:
        cart = _Alg()
        add_distance_mode_parameter(cart)
    finally:
        set_kp_distance_settings("ellipsoidal", "")
    geo = _Alg()
    add_distance_mode_parameter(geo)
    ok = cart.params[0].defaultValue() == 1 and geo.params[0].defaultValue() == 0
    return _result("processing Distance mode defaults to the plugin KP setting", ok)


def run_all():
    from qgis.PyQt.QtCore import QSettings
    old_format = QSettings.defaultFormat()
    results = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        # KP settings are QSettings: keep the tester's profile untouched.
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        results += [test_route_frame_start_kp(), test_ordered_route_and_wgs84(),
                    test_grid_kp_matches_utm_chainage(), test_processing_default_follows_setting()]
        for test in (test_legacy_plan_renumbered_to_rpl_start, test_move_plan_to_new_revision,
                     test_stated_kp_check, test_plan_keeps_its_kp_mode):
            try:
                results.append(test(temp))
            except Exception as exc:  # report, keep going
                import traceback
                traceback.print_exc()
                results.append(_result(test.__name__, False, repr(exc)))
        QSettings.setDefaultFormat(old_format)
        import gc
        gc.collect()
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()
