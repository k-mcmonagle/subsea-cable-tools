# -*- coding: utf-8 -*-
"""QGIS checks for the KP Mouse tool.

* Equivalence: the indexed route model (``RouteFrame``) against the
  previous per-move walk over every feature and vertex, on a synthetic
  multi-feature geographic route (null feature, multipart gap, duplicate
  vertex), in both KP modes. Snapped position, DCC and the multi-feature
  chainage assembly agree within 1 mm. One intended difference: inside a
  segment the old tool measured ``measureLine(vertex, position)``, while
  the plugin-wide KP (RouteFrame, ``point_at_kp``, Go to KP, Depth Profile,
  Burial Planner) is the segment fraction x measured segment length; the
  tool now reads the plugin-wide KP, and the test reports how far the old
  formula was off. Where the old tool ranked segments in planar degrees and
  missed a geodesically nearer leg, the new position must be nearer.
* Features are chained in SeqNo order when every feature has one (the
  order the Processing tools and KP Plotter use), else layer order.
* A real ``canvasMoveEvent`` uses that lookup and draws one rubber band.
* Range/bearing: cached WGS84 transforms (invalidated with the map CRS),
  the ring equals the previous per-vertex geodesic ring, and an unchanged
  radius does not rebuild it.
* The toolbar wrapper joins the plugin menu, uses the icon file, and its
  unload deletes the button and both actions (nothing left after reload).
"""

from __future__ import annotations

import math
import random
import time

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
                       QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRectangle,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QPoint, Qt
from qgis.PyQt.QtWidgets import QMainWindow, QToolBar

from ..kp_geo_utils import RouteFrame, iter_line_parts
from ..maptools import kp_mouse_maptool
from ..maptools.kp_mouse_maptool import KPMouseMapTool, KPMouseTool, _layer_has_features

_WGS84 = "EPSG:4326"

# ~55°N: typical RPL legs of 0.3–3 km, a duplicate vertex, a feature with no
# geometry, and a multipart feature with a gap between its parts.
_FEATURES = [
    "LINESTRING(3.000 55.000, 3.020 55.005, 3.020 55.005, 3.035 55.020, 3.050 55.018, 3.052 55.040)",
    None,
    "MULTILINESTRING((3.052 55.040, 3.070 55.045, 3.080 55.060),"
    "(3.085 55.062, 3.100 55.070, 3.100 55.090))",
    "LINESTRING(3.100 55.090, 3.120 55.085, 3.130 55.100)",
]
# One ~33 km deep-water leg: reported, not held to the 1 mm rule (see below).
_LONG_LEG = ["LINESTRING(3.0 55.0, 3.4 55.2, 3.5 55.3)"]


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def _layer(wkts, name="kp mouse route"):
    layer = QgsVectorLayer("MultiLineString?crs=%s" % _WGS84, name, "memory")
    feats = []
    for wkt in wkts:
        feat = QgsFeature()
        if wkt:
            feat.setGeometry(QgsGeometry.fromWkt(wkt))
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    return layer


class _Iface:
    """Just enough of QgisInterface for the map tool and its wrapper."""

    def __init__(self, crs=_WGS84):
        from qgis.gui import QgsMapCanvas, QgsMessageBar
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.canvas.resize(800, 600)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(crs))
        self.bar = QgsMessageBar()
        self.toolbar = QToolBar()
        self.window.addToolBar(self.toolbar)
        self.menu_calls = []

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def addToolBarWidget(self, widget):
        return self.toolbar.addWidget(widget)

    def removeToolBarIcon(self, action):
        self.toolbar.removeAction(action)

    def addPluginToMenu(self, name, action):
        self.menu_calls.append(("add", name, action))

    def removePluginMenu(self, name, action):
        self.menu_calls.append(("remove", name, action))

    def close(self):
        self.canvas.close()
        self.window.close()


def _tool(iface, layer, cartesian=False):
    return KPMouseMapTool(iface.mapCanvas(), layer, iface, "m", True, cartesian)


def _legacy_locate(features_geoms, distance, mouse_point):
    """The KP Mouse tool's previous per-move algorithm, verbatim in effect:
    per-feature GEOS nearest point ranked by measured distance, then a walk
    of the chosen feature's segments (planar nearest segment, measured
    partial length ``measureLine(p1, projection)``).

    Returns ``(kp_km, dcc_m, position, plugin_kp_km)`` where
    ``plugin_kp_km`` is the same walk with the plugin-wide partial length
    (planar fraction x measured segment length) — same features, order,
    offsets and segment choice.
    """
    segment_lengths = [distance.measureLength(g) for g in features_geoms]
    mouse_geom = QgsGeometry.fromPointXY(mouse_point)
    min_dist, closest, closest_idx = float("inf"), None, -1
    for i, geom in enumerate(features_geoms):
        if geom.isEmpty():
            continue
        nearest = geom.nearestPoint(mouse_geom)
        if nearest.isEmpty():
            continue
        point = nearest.asPoint()
        dist = distance.measureLine(mouse_point, point)
        if dist < min_dist:
            min_dist, closest, closest_idx = dist, point, i
    if closest is None:
        return None
    chainage = sum(segment_lengths[:closest_idx]) if closest_idx > 0 else 0
    geom = features_geoms[closest_idx]
    parts = geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]
    min_seg, along, plugin_along, walked = float("inf"), 0.0, 0.0, 0.0
    for part in parts:
        for i in range(len(part) - 1):
            p1, p2 = QgsPointXY(part[i]), QgsPointXY(part[i + 1])
            segment = QgsGeometry.fromPolylineXY([p1, p2])
            to_mouse = segment.distance(mouse_geom)
            seg_len = distance.measureLine(p1, p2)
            if to_mouse < min_seg:
                min_seg = to_mouse
                located = segment.lineLocatePoint(QgsGeometry.fromPointXY(closest))
                projected = segment.interpolate(located)
                along = walked + distance.measureLine(p1, projected.asPoint())
                fraction = located / segment.length() if segment.length() > 0 else 0.0
                plugin_along = walked + fraction * seg_len
            walked += seg_len
    return ((chainage + along) / 1000.0, min_dist, QgsPointXY(closest),
            (chainage + plugin_along) / 1000.0)


def _probe_points(layer, seed):
    """Points beside every segment (3 / 40 / 250 m either side at three
    fractions), on every vertex, and scattered around the route."""
    points, near = [], []
    for feat in layer.getFeatures():
        geom = feat.geometry()
        if geom.isEmpty():
            continue
        for part in iter_line_parts(geom):
            for a, b in zip(part[:-1], part[1:]):
                points.append(QgsPointXY(a))
                lat = math.radians((a.y() + b.y()) / 2.0)
                dx_m = (b.x() - a.x()) * 111320.0 * math.cos(lat)
                dy_m = (b.y() - a.y()) * 111320.0
                length = math.hypot(dx_m, dy_m)
                if length <= 0:
                    continue
                nx, ny = -dy_m / length, dx_m / length
                for t in (0.13, 0.5, 0.87):
                    for offset in (3.0, -3.0, 40.0, -40.0, 250.0, -250.0):
                        p = QgsPointXY(a.x() + t * (b.x() - a.x()) + offset * nx / (111320.0 * math.cos(lat)),
                                       a.y() + t * (b.y() - a.y()) + offset * ny / 111320.0)
                        points.append(p)
                        if t == 0.5 and abs(offset) <= 40.0:
                            near.append(p)
    rng = random.Random(seed)
    extent = layer.extent()
    for _ in range(300):
        points.append(QgsPointXY(rng.uniform(extent.xMinimum() - .01, extent.xMaximum() + .01),
                                 rng.uniform(extent.yMinimum() - .01, extent.yMaximum() + .01)))
    return points, near


def _equivalence(cartesian):
    iface = _Iface()
    layer = _layer(_FEATURES)
    tool = _tool(iface, layer, cartesian)
    mm = 1e-3
    try:
        points, near = _probe_points(layer, 21 if cartesian else 20)
        near_keys = {(p.x(), p.y()) for p in near}
        same = ties = nearer = 0
        worse, near_bad = [], 0
        worst_kp = worst_dcc = worst_formula = 0.0
        t_old = t_new = 0.0
        for p in points:
            start = time.perf_counter()
            old_kp, old_dcc, old_pos, plugin_kp = _legacy_locate(
                tool.features_geoms, tool.distanceArea, p)
            t_old += time.perf_counter() - start
            start = time.perf_counter()
            hit = tool._locate(p)
            t_new += time.perf_counter() - start
            d_dcc = hit.dcc_m - old_dcc
            moved = tool.distanceArea.measureLine(old_pos, hit.snapped_xy)
            if abs(d_dcc) <= mm and moved <= mm:
                d_kp = abs(hit.kp_km - plugin_kp) * 1000.0
                if d_kp > mm:
                    worse.append(("kp", p.x(), p.y(), plugin_kp, hit.kp_km))
                same += 1
                worst_kp = max(worst_kp, d_kp)
                worst_dcc = max(worst_dcc, abs(d_dcc))
                worst_formula = max(worst_formula, abs(hit.kp_km - old_kp) * 1000.0)
            elif abs(d_dcc) <= mm:
                ties += 1          # another position equally near (within 1 mm)
            elif d_dcc < -mm:
                nearer += 1        # legacy ranked segments in planar degrees
            else:
                worse.append(("dcc", p.x(), p.y(), old_dcc, hit.dcc_m))
            if (p.x(), p.y()) in near_keys and not (abs(d_dcc) <= mm and moved <= mm):
                near_bad += 1
        total_km = tool.total_length_meters / 1000.0
        legacy_total = sum(tool.distanceArea.measureLength(g) for g in tool.features_geoms) / 1000.0
        ok = not worse and near_bad == 0 and abs(total_km - legacy_total) < 1e-9
        detail = ("%d points: %d same position (KP worst %.2e m, DCC worst %.2e m; old in-segment "
                  "formula off by up to %.1f mm), %d equidistant, %d nearer than legacy, %d worse; "
                  "per move legacy %.3f ms vs indexed %.3f ms"
                  % (len(points), same, worst_kp, worst_dcc, worst_formula * 1000.0, ties, nearer,
                     len(worse), 1000 * t_old / len(points), 1000 * t_new / len(points)))
        if worse:
            detail += "; first worse: %s" % (worse[0],)
        return _result("indexed KP == legacy walk (%s)" % ("Cartesian" if cartesian else "geodesic"),
                       ok, detail)
    finally:
        tool.cleanup_resources()
        iface.close()


def test_equivalence_geodesic():
    return _equivalence(False)


def test_equivalence_cartesian():
    return _equivalence(True)


def test_long_leg_uses_plugin_kp():
    """On a 33 km geographic leg the legacy partial length
    ``measureLine(p1, projection)`` departs from the plugin-wide KP
    (planar fraction x segment length, which ``point_at_kp`` inverts); the
    tool now reads the plugin-wide KP. Reported, and round-trip checked."""
    iface = _Iface()
    layer = _layer(_LONG_LEG, "long leg")
    tool = _tool(iface, layer)
    try:
        worst_legacy = worst_trip = 0.0
        for i in range(1, 40):
            t = i / 40.0
            p = QgsPointXY(3.0 + t * 0.4, 55.0 + t * 0.2 + 0.002)
            hit = tool._locate(p)
            old_kp = _legacy_locate(tool.features_geoms, tool.distanceArea, p)[0]
            worst_legacy = max(worst_legacy, abs(hit.kp_km - old_kp) * 1000.0)
            back = tool._route.point_at_kp(hit.kp_km)
            worst_trip = max(worst_trip, tool.distanceArea.measureLine(back, hit.snapped_xy))
        ok = worst_trip < 1e-3
        return _result("long leg: KP round-trips with point_at_kp", ok,
                       "round trip worst %.2e m; legacy formula differed by up to %.3f m"
                       % (worst_trip, worst_legacy))
    finally:
        tool.cleanup_resources()
        iface.close()


def test_seqno_route_order():
    """An RPL line layer whose SeqNo order differs from its feature order
    is chained in SeqNo order — the order the Processing tools and the KP
    Plotter use (``ordered_route_geometry``), so all report the same KP."""
    from ..kp_geo_utils import ordered_route_geometry
    layer = QgsVectorLayer("LineString?crs=%s&field=SeqNo:integer" % _WGS84, "rpl", "memory")
    feats = []
    for seq, wkt in ((2, "LINESTRING(3.02 55.0, 3.04 55.0)"), (1, "LINESTRING(3.00 55.0, 3.02 55.0)")):
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromWkt(wkt))
        feat.setAttributes([seq])
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    iface = _Iface()
    tool = _tool(iface, layer)
    try:
        probe = QgsPointXY(3.01, 55.0001)
        hit = tool._locate(probe)
        shared = RouteFrame.from_source([ordered_route_geometry(list(layer.getFeatures()))],
                                        tool.distanceArea).kp_at_point(probe)
        ok = hit is not None and abs(hit.kp_km - shared.kp_km) < 1e-9 and hit.kp_km < 1.0
        return _result("KP Mouse chains features in SeqNo order like the other KP tools", ok,
                       "KP %.4f (shared route %.4f)" % (hit.kp_km if hit else float("nan"), shared.kp_km))
    finally:
        tool.cleanup_resources()
        iface.close()


def test_dense_route_timing():
    """A realistic RPL (12 features, ~3600 vertices): per-move cost of the
    legacy walk vs the indexed lookup, with the same position/KP rule."""
    rng = random.Random(30)
    wkts, x, y = [], 3.0, 55.0
    for _feature in range(12):
        pts = ["%.7f %.7f" % (x, y)]
        for _ in range(300):
            x += rng.uniform(0.0005, 0.003)
            y += rng.uniform(-0.0015, 0.0015)
            pts.append("%.7f %.7f" % (x, y))
        wkts.append("LINESTRING(%s)" % ", ".join(pts))
    iface = _Iface()
    layer = _layer(wkts, "dense")
    tool = _tool(iface, layer)
    try:
        extent = layer.extent()
        probes = [QgsPointXY(rng.uniform(extent.xMinimum(), extent.xMaximum()),
                             rng.uniform(extent.yMinimum(), extent.yMaximum())) for _ in range(15)]
        start = time.perf_counter()
        legacy = [_legacy_locate(tool.features_geoms, tool.distanceArea, p) for p in probes]
        t_old = (time.perf_counter() - start) / len(probes)
        start = time.perf_counter()
        hits = [tool._locate(p) for p in probes]
        t_new = (time.perf_counter() - start) / len(probes)
        ok = True
        for old, hit in zip(legacy, hits):
            same = (abs(hit.dcc_m - old[1]) <= 1e-3
                    and tool.distanceArea.measureLine(old[2], hit.snapped_xy) <= 1e-3)
            ok = ok and (hit.dcc_m < old[1] - 1e-3 or (same and abs(hit.kp_km - old[3]) <= 1e-6))
        return _result("dense route (%d vertices): same answers, faster per move" % (12 * 301),
                       ok and t_new < t_old,
                       "legacy %.1f ms vs indexed %.3f ms per move (x%.0f)"
                       % (1000 * t_old, 1000 * t_new, t_old / max(t_new, 1e-9)))
    finally:
        tool.cleanup_resources()
        iface.close()


def test_canvas_move_event():
    from qgis.gui import QgsMapMouseEvent
    iface = _Iface()
    layer = _layer(_FEATURES)
    tool = _tool(iface, layer)
    try:
        iface.canvas.setExtent(layer.extent())
        pos = QPoint(400, 300)
        event = QgsMapMouseEvent(iface.canvas, QEvent.Type.MouseMove, pos,
                                 Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                 Qt.KeyboardModifier.NoModifier)
        tool.canvasMoveEvent(event)
        expected = tool._locate(tool.toMapCoordinates(pos))
        ok = (expected is not None and tool.last_chainage == expected.kp_km
              and tool.rubberBand.numberOfVertices() == 2
              and "KP: %.3f" % expected.kp_km in tool.last_message)
        return _result("canvasMoveEvent reports the indexed KP and draws one band", ok,
                       "KP %.6f" % (tool.last_chainage or float("nan")))
    finally:
        tool.cleanup_resources()
        iface.close()


def test_range_ring_cached_and_unchanged():
    iface = _Iface("EPSG:3857")
    layer = _layer(_FEATURES)
    tool = _tool(iface, layer)
    try:
        canvas = iface.canvas
        crs = canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem(_WGS84)
        to_merc = QgsCoordinateTransform(wgs84, crs, QgsProject.instance())
        origin = to_merc.transform(QgsPointXY(3.05, 55.03))
        mouse = to_merc.transform(QgsPointXY(3.08, 55.05))
        # ~25 m per pixel around the origin, so half a pixel is a few metres.
        canvas.setExtent(QgsRectangle(origin.x() - 10000, origin.y() - 7500,
                                      origin.x() + 10000, origin.y() + 7500))
        first = tool._wgs84_transforms()
        cached = tool._wgs84_transforms() is first
        tool.range_bearing_origin = origin
        range_m, _bearing = tool._compute_range_bearing(origin, mouse)
        calls = []
        real = tool._range_ring_geometry
        tool._range_ring_geometry = lambda *a, **k: calls.append(1) or real(*a, **k)
        tool._update_range_bearing_graphics(mouse, range_m)
        tool._update_range_bearing_graphics(mouse, range_m)          # same radius
        rebuilt_once = len(calls) == 1
        tool._update_range_bearing_graphics(mouse, range_m * 1.5)    # visibly larger
        rebuilt_again = len(calls) == 2
        del tool._range_ring_geometry
        # Legacy ring: per-vertex spheroid projection + per-point transform.
        spheroid = QgsDistanceArea()
        spheroid.setEllipsoid("WGS84")
        to_wgs = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
        origin_ll = to_wgs.transform(origin)
        legacy = [to_merc.transform(spheroid.computeSpheroidProject(origin_ll, range_m * 1.5,
                                                                    2.0 * math.pi * i / 180))
                  for i in range(181)]
        # Keep the geometry alive: vertices() of a temporary never ends.
        ring_geom = tool.rangeBearingCircle.asGeometry()
        ring = [QgsPointXY(v) for v in ring_geom.vertices()]
        worst = max(math.hypot(a.x() - b.x(), a.y() - b.y()) for a, b in zip(ring, legacy))
        same_ring = len(ring) == 181 and worst < 1e-6
        line_ok = tool.rangeBearingLine.numberOfVertices() == 2
        # A new map CRS invalidates the transforms (and rebuilds the route).
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:32631"))
        invalidated = tool._wgs84_transforms() is not first and tool.range_bearing_origin is None
        rebuilt_route = tool._route is not None and abs(
            tool.total_length_meters - RouteFrame.from_source(
                tool.features_geoms, tool.distanceArea).total_length_m) < 1e-9
        ok = cached and rebuilt_once and rebuilt_again and same_ring and line_ok \
            and invalidated and rebuilt_route
        return _result("range ring: cached transforms, legacy-identical ring, no rebuild at same radius",
                       ok, "cached=%s rebuilt=%s/%s ring worst %.2e m, CRS change invalidates=%s"
                       % (cached, rebuilt_once, rebuilt_again, worst, invalidated))
    finally:
        tool.cleanup_resources()
        iface.close()


def test_cleanup_detaches_canvas_items():
    iface = _Iface()
    layer = _layer(_FEATURES)
    before = len(iface.canvas.scene().items())
    tool = _tool(iface, layer)
    during = len(iface.canvas.scene().items())
    tool.cleanup_resources()
    tool.cleanup_resources()          # twice is harmless
    after = len(iface.canvas.scene().items())
    iface.close()
    return _result("cleanup removes every canvas item (twice is safe)",
                   during > before and after == before,
                   "items before %d, with tool %d, after cleanup %d" % (before, during, after))


def test_layer_has_features():
    empty = QgsVectorLayer("LineString?crs=%s" % _WGS84, "empty", "memory")
    ok = not _layer_has_features(empty) and _layer_has_features(_layer(_FEATURES))
    return _result("feature check reads at most one feature", ok)


def test_wrapper_menu_icon_and_unload():
    from qgis.PyQt import sip
    iface = _Iface()
    wrapper = KPMouseTool(iface)
    try:
        wrapper.initGui()
        button, widget_action = wrapper.toolButton, wrapper.toolButtonAction
        config, goto = wrapper.actionConfig, wrapper.actionGoToKP
        added = [c for c in iface.menu_calls if c[0] == "add"]
        menu_ok = (len(added) == 1 and added[0][1] == kp_mouse_maptool.plugin_menu_name()
                   and added[0][2] is config)
        icon_ok = not button.icon().isNull()
        in_toolbar = widget_action in iface.toolbar.actions()
        wrapper.unload()
        wrapper.unload()              # second unload is harmless
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        removed = [c for c in iface.menu_calls if c[0] == "remove"]
        deleted = all(sip.isdeleted(obj) for obj in (button, widget_action, config, goto))
        ok = (menu_ok and icon_ok and in_toolbar and deleted
              and len(removed) == 1 and removed[0][1] == added[0][1])
        return _result("wrapper: plugin menu, file icon, unload deletes button + actions", ok,
                       "menu=%s icon=%s deleted=%s" % (menu_ok, icon_ok, deleted))
    finally:
        iface.close()


def run_all():
    return [
        test_equivalence_geodesic(),
        test_equivalence_cartesian(),
        test_long_leg_uses_plugin_kp(),
        test_seqno_route_order(),
        test_dense_route_timing(),
        test_canvas_move_event(),
        test_range_ring_cached_and_unchanged(),
        test_cleanup_detaches_canvas_items(),
        test_layer_has_features(),
        test_wrapper_menu_icon_and_unload(),
    ]
