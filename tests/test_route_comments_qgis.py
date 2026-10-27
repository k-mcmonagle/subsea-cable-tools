# -*- coding: utf-8 -*-
"""Route Comments in QGIS: capture, KPs, reference conditions, map, dock.

* KP correctness: comments read the KP Mouse Tool's route model (route
  order, non-zero start KP, geodesic or grid convention): a route point sits
  at ``point_at_kp``, a free location keeps its own position with its
  nearest KP and distance apart, a range's line runs between the KPs;
  capturing in a projected map CRS gives the same KPs; the convention token
  names the grid that was used.
* KP Mouse Tool: the *Add Comment* entries appear only with the comments
  hook; a KP range is previewed while picking, finished with a click and
  cancelled with Escape or a right click (canvas items removed).
* Reference conditions: unchanged, geometry edited, start KP changed, KP
  convention switched, layer removed (memory: unavailable; GeoPackage:
  opened from its file, then unavailable once deleted), newer Workbench
  revision. The stored KPs and geometry never change.
* Dense map: hundreds of comments on one spot are clustered, every one is
  listed by the pick search, numbers are the only (scale-limited) labels,
  selection works both ways, layers come back after a project reload.
* Dock: table/search/filters/sort, persistent column layout, map/table
  selection sync, capture through the hook, save with conflict detection,
  replies, image thumbnails, import with duplicate IDs and KPs on a route,
  export of visible rows and of the whole register (CSV and XLSX),
  external changes picked up.
* Review panel: the thread beside the images (stacked when narrow, the
  image pane small without images), images from files, a drop and the
  clipboard, one drawn in the map label with the comment text escaped,
  Send & close / reopen, quick status, previous / next with follow on the
  map, images let go when the project closes.

QSettings are redirected to a temporary folder.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
import time

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsExpression, QgsExpressionContext,
                       QgsExpressionContextUtils, QgsFeature, QgsGeometry, QgsPointClusterRenderer, QgsPointXY,
                       QgsProject, QgsRectangle, QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QPoint, QSettings, Qt
from qgis.PyQt.QtGui import QColor, QImage, QKeyEvent
from qgis.PyQt.QtWidgets import QMainWindow, QMenu

from .. import kp_range_utils
from ..route_comments import dock as dock_module
from ..route_comments import map_layers, model
from ..route_comments.route_ref import CaptureRoute, ReferenceChecker, kp_convention_token
from ..route_comments.store import CommentStore

REQUIRES_QGIS = True

WGS84 = "EPSG:4326"
ROUTE = ["LINESTRING(3.000 55.000, 3.020 55.005, 3.035 55.020, 3.050 55.018)",
         "LINESTRING(3.050 55.018, 3.070 55.045, 3.100 55.070)"]
START_KP = 12.345


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def _route_layer(wkts=ROUTE, name="route"):
    layer = QgsVectorLayer(f"LineString?crs={WGS84}", name, "memory")
    feats = []
    for wkt in wkts:
        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromWkt(wkt))
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    return layer


def _gpkg_route(folder, name="route_file"):
    layer = _route_layer(name=name)
    path = os.path.join(folder, f"{name}.gpkg")
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = name
    result = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, QgsProject.instance().transformContext(),
                                                       options)
    assert result[0] == QgsVectorFileWriter.WriterError.NoError, result
    out = QgsVectorLayer(f"{path}|layername={name}", name, "ogr")
    assert out.isValid()
    return out, path


class _Iface:
    def __init__(self, crs=WGS84):
        from qgis.gui import QgsMapCanvas, QgsMessageBar
        self.window = QMainWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.resize(800, 600)
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(crs))
        self.window.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar()

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def close(self):
        self.canvas.close()
        self.window.close()


def _close(a, b, tol):
    return abs(float(a) - float(b)) <= tol


def _pt(wkt):
    return QgsGeometry.fromWkt(wkt).asPoint()


# --------------------------------------------------------------------------- KP correctness
def test_capture_kps_match_route_model():
    from ..maptools.kp_mouse_maptool import build_reference_route
    layer = _route_layer()
    QgsProject.instance().addMapLayer(layer)
    try:
        capture = CaptureRoute.from_layer(layer, layer.crs(), start_kp_km=START_KP, use_cartesian=False)
        route, _d, _g = build_reference_route(layer, layer.crs(), False, start_kp_km=START_KP)
        checks = {"start": _close(capture.start_kp_km, START_KP, 1e-12),
                  "end": _close(capture.end_kp_km, route.end_kp_km, 1e-9)}
        kp = START_KP + 2.5
        point = capture.point_values(kp)
        expected = route.point_at_kp(kp)
        checks["route point at point_at_kp"] = point["kp_km"] == kp and \
            _pt(point["wkt"]).distance(expected) < 1e-9 and point["ref_kind"] == model.REF_ROUTE_POINT
        off = QgsPointXY(3.03, 55.03)
        loc = capture.location_values(off)
        hit = route.kp_at_point(off)
        checks["location keeps its position"] = _pt(loc["wkt"]).distance(off) < 1e-12
        checks["location nearest KP apart"] = _close(loc["kp_km"], hit.kp_km, 1e-9) and loc["dcc_m"] > 500 \
            and _close(loc["dcc_m"], hit.dcc_m, 1e-6)
        rng = capture.range_values(START_KP + 4.0, START_KP + 1.0)
        line = QgsGeometry.fromWkt(rng["wkt"]).asPolyline()
        checks["range ordered"] = rng["kp_km"] == START_KP + 1.0 and rng["kp_end_km"] == START_KP + 4.0
        checks["range ends at its KPs"] = line[0].distance(route.point_at_kp(START_KP + 1.0)) < 1e-9 and \
            line[-1].distance(route.point_at_kp(START_KP + 4.0)) < 1e-9
        checks["range follows the vertices"] = len(line) >= 3
        checks["range lat/lon = start"] = _close(rng["lat"], line[0].y(), 1e-12)
        ident = point
        checks["identity"] = ident["route_layer_id"] == layer.id() and ident["route_provider"] == "memory" \
            and ident["kp_convention"] == "geodesic:WGS84" and _close(ident["start_kp_km"], START_KP, 1e-12) \
            and len(ident["route_hash"]) == 40 and ident["geom_crs"] == WGS84
        clamped = capture.point_values(START_KP - 5.0)
        checks["KP clamped to the route"] = clamped["kp_km"] == START_KP
        try:
            capture.range_values(START_KP + 1.0, START_KP + 1.0)
            checks["empty range refused"] = False
        except ValueError:
            checks["empty range refused"] = True

        # Captured in a projected map CRS (as the KP Mouse Tool does): same KPs.
        utm = QgsCoordinateReferenceSystem("EPSG:32631")
        in_map = CaptureRoute.from_layer(layer, utm, start_kp_km=START_KP, use_cartesian=False)
        to_utm = QgsCoordinateTransform(layer.crs(), utm, QgsProject.instance())
        loc_map = in_map.location_values(to_utm.transform(off))
        checks["map CRS: same nearest KP"] = _close(loc_map["kp_km"], loc["kp_km"], 1e-6)
        checks["map CRS: 4326 copy + exact capture"] = _pt(loc_map["wkt"]).distance(off) < 1e-9 and \
            loc_map["geom_crs"] == "EPSG:32631"
        # Grid convention: the token names the grid actually used.
        grid = CaptureRoute.from_layer(layer, layer.crs(), start_kp_km=START_KP, use_cartesian=True)
        checks["grid token"] = grid.identity()["kp_convention"] == "grid:EPSG:32631"
        checks["grid KPs differ from geodesic"] = abs(grid.end_kp_km - capture.end_kp_km) > 1e-4
    finally:
        QgsProject.instance().removeMapLayer(layer.id())
    failed = [k for k, v in checks.items() if not v]
    return _result("capture: KPs, positions and identity from the KP route model", not failed, ", ".join(failed))


# --------------------------------------------------------------------------- KP Mouse Tool
def test_kp_mouse_tool_comment_entries():
    from qgis.gui import QgsMapMouseEvent
    from ..maptools.kp_mouse_maptool import KPMouseMapTool
    iface = _Iface()
    layer = _route_layer()
    QgsProject.instance().addMapLayer(layer)
    tool = KPMouseMapTool(iface.mapCanvas(), layer, iface, "m", True, False, start_kp_km=START_KP)
    calls = []
    checks = {}
    try:
        iface.canvas.setExtent(QgsRectangle(2.99, 54.99, 3.11, 55.08))
        iface.canvas.setMapTool(tool)
        click = QgsPointXY(3.03, 55.03)
        menu = QMenu()
        checks["no hook: nothing added"] = tool._add_comment_menu(menu, click) == {} and not menu.actions()
        tool.comment_hook = lambda kind, capture, data: calls.append((kind, capture, data))
        actions = tool._add_comment_menu(menu, click)
        texts = [a.text() for a in actions]
        checks["three entries"] = len(actions) == 3 and texts[0].startswith("At Nearest Route Point (KP ")
        callbacks = list(actions.values())
        callbacks[0]()
        callbacks[1]()
        kind, capture, data = calls[0]
        hit = tool._route.kp_at_point(click)
        checks["route point via hook"] = kind == "route_point" and _close(data["kp_km"], hit.kp_km, 1e-12) \
            and _close(capture.start_kp_km, START_KP, 1e-12)
        checks["location via hook"] = calls[1][0] == "location" and calls[1][2]["point"] == click

        # Range: preview while moving, finish with a left click.
        callbacks[2]()
        picker = tool._comment_range
        checks["picker started"] = picker is not None and _close(picker.start_kp, hit.kp_km, 1e-12)
        end_map = tool._route.point_at_kp(hit.kp_km + 1.5)
        pixel = iface.canvas.getCoordinateTransform().transform(end_map)
        pos = QPoint(int(round(pixel.x())), int(round(pixel.y())))
        tool.canvasMoveEvent(QgsMapMouseEvent(iface.canvas, QEvent.Type.MouseMove, pos, Qt.MouseButton.NoButton,
                                              Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        checks["preview drawn"] = picker.band.asGeometry().length() > 0 and \
            iface.window.statusBar().currentMessage().startswith("Comment range KP")
        band = picker.band
        tool.canvasPressEvent(QgsMapMouseEvent(iface.canvas, QEvent.Type.MouseButtonPress, pos,
                                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                               Qt.KeyboardModifier.NoModifier))
        kind, _capture, data = calls[-1]
        lo, hi = data["kps"]
        clicked = tool._route.kp_at_point(tool.toMapCoordinates(pos)).kp_km
        checks["range via click"] = kind == "kp_range" and _close(lo, hit.kp_km, 1e-9) and \
            _close(hi, clicked, 1e-9) and abs(hi - (hit.kp_km + 1.5)) < 0.2 and tool._comment_range is None \
            and band.scene() is None
        checks["left click did not start range/bearing"] = tool.range_bearing_origin is None

        # Escape cancels; so does a right click.
        tool._start_comment_range(hit.kp_km)
        band = tool._comment_range.band
        tool.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        checks["Escape cancels"] = tool._comment_range is None and band.scene() is None and len(calls) == 3
        tool._start_comment_range(hit.kp_km)
        tool.canvasPressEvent(QgsMapMouseEvent(iface.canvas, QEvent.Type.MouseButtonPress, pos,
                                               Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
                                               Qt.KeyboardModifier.NoModifier))
        checks["right click cancels"] = tool._comment_range is None and len(calls) == 3
        tool._start_comment_range(hit.kp_km)
        band = tool._comment_range.band
        tool.cleanup_resources()
        checks["cleanup removes the preview"] = band.scene() is None and tool.comment_hook is None
    finally:
        iface.canvas.unsetMapTool(tool)
        QgsProject.instance().removeMapLayer(layer.id())
        iface.close()
    failed = [k for k, v in checks.items() if not v]
    return _result("KP Mouse Tool: Add Comment entries, range preview, Esc/right-click cancel", not failed,
                   ", ".join(failed))


# --------------------------------------------------------------------------- conditions
class _Workbench:
    def __init__(self, latest=None):
        self.latest = latest

    def get_rpl(self, _rpl_id):
        return None

    def latest_revision(self, _route_id):
        return self.latest


def test_reference_conditions(folder):
    layer = _route_layer()
    file_layer, file_path = _gpkg_route(folder)
    project = QgsProject.instance()
    project.addMapLayers([layer, file_layer])
    store = CommentStore.create(os.path.join(folder, "conditions.gpkg"))
    checks = {}
    try:
        set_id = store.list_sets()[0]["set_id"]
        cap = CaptureRoute.from_layer(layer, layer.crs(), start_kp_km=START_KP, use_cartesian=False)
        cap_file = CaptureRoute.from_layer(file_layer, file_layer.crs(), start_kp_km=0.0, use_cartesian=False)
        comment = store.add_comment(set_id, dict(cap.point_values(START_KP + 1.0), text="on memory route"))
        on_file = store.add_comment(set_id, dict(cap_file.range_values(1.0, 2.0), text="on file route"))
        free = store.add_comment(set_id, {"text": "no route", "ref_kind": model.REF_LOCATION,
                                          "wkt": "POINT(3 55)", "lat": 55.0, "lon": 3.0})
        starts = {layer.id(): START_KP, file_layer.id(): 0.0}
        checker = ReferenceChecker(start_kp_for=lambda lyr: starts.get(lyr.id()), workbench_store=_Workbench)
        checks["ok"] = checker.check(comment)[0] == model.COND_OK and checker.check(on_file)[0] == model.COND_OK
        checks["no route"] = checker.check(free)[0] == model.COND_NONE

        # Start KP changed (e.g. set in KP Mouse configure).
        starts[layer.id()] = START_KP + 0.1
        checker.clear()
        code, notes = checker.check(comment)
        checks["start KP changed"] = code == model.COND_CHANGED and "starts at KP" in notes[0]
        starts[layer.id()] = START_KP

        # KP convention switched to grid.
        old = (kp_range_utils.kp_distance_mode(), kp_range_utils.kp_grid_crs_setting())
        kp_range_utils.set_kp_distance_settings(kp_range_utils.KP_MODE_CARTESIAN, "")
        checker.clear()
        code, notes = checker.check(comment)
        checks["convention changed"] = code == model.COND_CHANGED and "grid" in " ".join(notes)
        kp_range_utils.set_kp_distance_settings(*old)

        # Geometry edited.
        fid = next(layer.getFeatures()).id()
        layer.dataProvider().changeGeometryValues({fid: QgsGeometry.fromWkt(
            "LINESTRING(3.000 55.000, 3.021 55.006, 3.035 55.020, 3.050 55.018)")})
        checker.clear()
        code, notes = checker.check(comment)
        checks["geometry changed"] = code == model.COND_CHANGED and "geometry" in notes[0]
        again = store.get_comment(comment["comment_id"])
        checks["stored KPs/geometry untouched"] = again["kp_km"] == comment["kp_km"] and again["wkt"] == comment["wkt"]

        # Newer revision in the Workbench (route unchanged otherwise).
        superseded = dict(on_file, rpl_id="rpl-a", rpl_route_id="route-1")
        checker = ReferenceChecker(start_kp_for=lambda lyr: starts.get(lyr.id()),
                                   workbench_store=lambda: _Workbench({"rpl_id": "rpl-b", "rev_label": "Rev C"}))
        code, notes = checker.check(superseded)
        checks["newer revision"] = code == model.COND_SUPERSEDED and "Rev C" in notes[0]

        # Removed from the project: a memory route is gone; a file route is opened from its file.
        project.removeMapLayers([layer.id(), file_layer.id()])
        checker = ReferenceChecker(workbench_store=_Workbench)
        code, notes = checker.check(comment)
        checks["memory route unavailable"] = code == model.COND_UNAVAILABLE and "keeps the position" in notes[0]
        code, notes = checker.check(on_file)
        checks["file route opened from its file"] = code == model.COND_OK and "file" in notes[0]
        missing = os.path.join(folder, "moved_away.gpkg")
        gone = dict(on_file, route_source=f"{missing}|layername=route_file", route_layer_id="")
        checks["file gone: unavailable"] = checker.check(gone)[0] == model.COND_UNAVAILABLE
        checks["file path kept"] = file_path.endswith("route_file.gpkg")
    finally:
        for lyr in (layer, file_layer):
            try:
                project.removeMapLayer(lyr.id())
            except RuntimeError:
                pass
        store.close()
    failed = [k for k, v in checks.items() if not v]
    return _result("reference conditions: OK, changed (geometry/start KP/convention), unavailable, newer revision",
                   not failed, ", ".join(failed))


def _n(layer):
    """Features in ``layer``, counted by reading them (not QGIS's cached count)."""
    return sum(1 for _f in layer.getFeatures())


# --------------------------------------------------------------------------- map
def _dense_register(folder, n=300):
    store = CommentStore.create(os.path.join(folder, "dense.gpkg"))
    set_id = store.list_sets()[0]["set_id"]
    rows = [{"text": f"Comment {i} about the seabed here", "status": "Open", "ref_kind": model.REF_LOCATION,
             "wkt": "POINT(3.03 55.03)"} for i in range(n)]
    rows.append({"text": "A range", "status": "Closed", "ref_kind": model.REF_KP_RANGE, "kp_km": 1.0,
                 "kp_end_km": 2.0, "wkt": "LINESTRING(3.0 55.0, 3.02 55.005)"})
    added = store.add_comments(set_id, rows)
    other = store.create_set("Other")
    store.add_comment(other["set_id"], {"text": "other set", "ref_kind": model.REF_LOCATION,
                                        "wkt": "POINT(3.03 55.03)"})
    return store, set_id, other["set_id"], added


def _state(layer, feature):
    from qgis.core import QgsExpression, QgsExpressionContext, QgsExpressionContextUtils
    context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    context.setFeature(feature)
    return QgsExpression(map_layers.STATE_EXPRESSION).evaluate(context)


def test_dense_map_layers(folder):
    """Layers read the register: one copy of the data, filtered to the set."""
    iface = _Iface()
    layers = map_layers.CommentLayers()
    store, set_id, other_id, added = _dense_register(folder)
    checks = {}
    try:
        started = time.perf_counter()
        shown = layers.show(store.path, set_id, "Set A", {"closed"}, [added[1]["comment_id"]])
        elapsed = time.perf_counter() - started
        points, ranges = layers.find("points"), layers.find("ranges")
        checks["two layers on the register"] = len(shown) == 2 and points.providerType() == "ogr" and \
            points.source().startswith(store.path) and points.readOnly()
        checks["filtered to the set"] = _n(points) == 300 and _n(ranges) == 1
        # The first layer opened on a register used to lose its own reader when the second opened.
        checks["count and extent of both layers"] = points.featureCount() == 300 and ranges.featureCount() == 1 \
            and not points.extent().isNull() and not ranges.extent().isNull()
        checks["fast"] = elapsed < 3.0
        checks["clustered"] = isinstance(points.renderer(), QgsPointClusterRenderer)
        by_id = {f["comment_id"]: f for f in points.getFeatures()}
        checks["states via project variables"] = _state(points, by_id[added[1]["comment_id"]]) == "attention" \
            and _state(points, by_id[added[2]["comment_id"]]) == "open" \
            and _state(ranges, next(ranges.getFeatures())) == "closed"
        rules = points.labeling().rootRule().children()
        default = map_layers.LabelSettings()
        scales = [(r.minimumScale(), r.maximumScale()) for r in rules]
        checks["labels grow as you zoom in"] = [r.description() for r in rules] == ["Number", "Short", "Details"] \
            and scales == [(2000000, 250000), (250000, 50000), (50000, 0)] and \
            (default.number_scale, default.summary_scale, default.detail_scale) == (2000000, 250000, 50000)
        short = QgsExpression(default.summary_expression())
        context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(points))
        context.setFeature(next(points.getFeatures()))
        checks["short line"] = str(short.evaluate(context)).startswith("#") and len(str(short.evaluate(context))) <= 30
        # Keep each labeling while reading its rules: they belong to it (a dropped temporary frees them).
        older = map_layers.LabelSettings.from_json('{"numbers": true, "number_scale": 100000, "detail_scale": 20000}')
        older_labeling = map_layers.build_labeling(older, False)
        checks["labels set before keep their two steps"] = not older.summary and \
            [r.description() for r in older_labeling.rootRule().children()] == ["Number", "Details"]
        crossed = map_layers.LabelSettings(number_scale=100000, summary_scale=250000, detail_scale=20000)
        crossed_labeling = map_layers.build_labeling(crossed, False)
        checks["a step out of order is left out"] = \
            [r.description() for r in crossed_labeling.rootRule().children()] == ["Short", "Details"]
        checks["in a group"] = QgsProject.instance().layerTreeRoot().findGroup(map_layers.GROUP_NAME) is not None
        iface.canvas.setExtent(QgsRectangle(3.02, 55.02, 3.04, 55.04))
        hits = map_layers.comments_at(iface.canvas, QgsPointXY(3.03, 55.03), layers.layers())
        checks["pick lists all overlapping"] = len(hits) == 300
        pick = [added[5]["comment_id"], added[-1]["comment_id"]]
        layers.select(pick)
        checks["select"] = points.selectedFeatureCount() == 1 and ranges.selectedFeatureCount() == 1 and \
            set(layers.selected_ids()) == set(pick)

        # The user restyles a layer: showing again (new data, another set) keeps it.
        from qgis.core import QgsSingleSymbolRenderer, QgsMarkerSymbol
        mine = QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple({"color": "0,0,255"}))
        points.setRenderer(mine)
        store.add_comment(set_id, {"text": "new", "ref_kind": model.REF_LOCATION, "wkt": "POINT(3.03 55.03)"})
        layers.show(store.path, set_id, "Set A")
        checks["reloads new comments"] = _n(points) == 301
        layers.show(store.path, other_id, "Other")
        checks["switches set"] = _n(points) == 1 and points.name() == "Comments — Other"
        checks["user style kept"] = isinstance(points.renderer(), QgsSingleSymbolRenderer)
        points.setName("My comments")
        layers.show(store.path, set_id, "Set A")
        checks["user name kept"] = points.name() == "My comments"

        # Labels: chosen fields, a custom expression.
        settings = map_layers.LabelSettings(parts=["number", "kp", "status", "comment"], separator="dot",
                                            comment_chars=12)
        map_layers.apply_labels(layers.layers(), settings)
        feature = next(ranges.getFeatures())
        context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(ranges))
        checks["label expressions parse"] = not any(QgsExpression(e).hasParserError() for e in (
            map_layers.RANGE_ANCHOR, map_layers.RANGE_POSITIONS, settings.detail_expression()))
        context.setFeature(feature)
        text = QgsExpression(settings.detail_expression()).evaluate(context)
        checks["label text"] = text == f"#{feature['ref_no']} · KP 1.000–2.000 · Closed · A range"
        if not checks["label text"]:
            print("  label:", text)
        checks["label settings kept on the layer"] = map_layers.label_settings_of(points).parts == settings.parts
        custom = map_layers.LabelSettings(custom="upper(\"status\")")
        checks["custom expression"] = QgsExpression(custom.detail_expression()).evaluate(context) == "CLOSED"

        # Leader labels (the default): a callout on both tiers; a range is labelled from a
        # point on its part in view, with the tag across the range.
        tiers = [r.settings() for r in ranges.labeling().rootRule().children()]
        checks["leader labels"] = map_layers.label_settings_of(ranges).style == map_layers.STYLE_LEADER and all(
            s.callout() is not None and s.callout().enabled() and s.geometryGeneratorEnabled for s in tiers)
        no_map = QgsExpression(map_layers.RANGE_ANCHOR).evaluate(context)   # e.g. a preview: the middle
        checks["range anchor without a map"] = isinstance(no_map, QgsGeometry) and not no_map.isNull() and \
            QgsExpression(map_layers.RANGE_POSITIONS).evaluate(context) == "TR,TL,BR,BL,R,L,T,B"
        from qgis.core import QgsMapSettings
        from qgis.PyQt.QtCore import QSize
        view = QgsMapSettings()
        view.setOutputSize(QSize(800, 600))
        view.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:32631"))
        to_map = QgsCoordinateTransform(QgsCoordinateReferenceSystem(WGS84), view.destinationCrs(),
                                        QgsProject.instance())
        view.setExtent(to_map.transformBoundingBox(QgsRectangle(3.012, 55.0, 3.03, 55.01)))   # east half
        context.appendScope(QgsExpressionContextUtils.mapSettingsScope(view))
        anchor = QgsExpression(map_layers.RANGE_ANCHOR).evaluate(context)
        order = QgsExpression(map_layers.RANGE_POSITIONS).evaluate(context)
        checks["range anchor in view, on the range"] = isinstance(anchor, QgsGeometry) and \
            anchor.asPoint().x() > 3.012 and anchor.distance(feature.geometry()) < 1e-7
        # The range runs ENE: its first choice must not be along it (top right / bottom left).
        checks["range tag across the range"] = str(order).split(",")[0] not in ("TR", "BL")
        if not (checks["range anchor in view, on the range"] and checks["range tag across the range"]):
            print("  anchor:", anchor, "order:", order)
        tag = map_layers.LabelSettings(style=map_layers.STYLE_TAG)
        map_layers.apply_labels(layers.layers(), tag)
        checks["tag style: no leader"] = map_layers.label_settings_of(points).style == map_layers.STYLE_TAG and \
            all(r.settings().callout() is None or not r.settings().callout().enabled()
                for r in points.labeling().rootRule().children())

        highlight = map_layers.Highlight(iface.canvas)
        highlight.show(["POINT(3.03 55.03)", "LINESTRING(3.0 55.0, 3.02 55.005)"])
        bands = list(highlight.bands)
        shown = highlight.wkts_shown()
        highlight.clear()
        checks["highlight: ring, band and range end rings"] = len(bands) == 8 and shown == 2 and \
            all(b.scene() is None for b in bands)
    finally:
        layers.remove()
        store.close()
        iface.close()
    checks["removed"] = layers.find("points") is None and \
        QgsProject.instance().layerTreeRoot().findGroup(map_layers.GROUP_NAME) is None
    failed = [k for k, v in checks.items() if not v]
    return _result("map: register-backed layers, clustering, pickable overlaps, label tiers, user style kept",
                   not failed, ", ".join(failed))


def test_layers_saved_with_project(folder):
    """The layers are ordinary project layers: saved, reopened with their data,
    and pointed at the register again when it moved."""
    import shutil
    store, set_id, _other, _added = _dense_register(folder, n=3)
    project = QgsProject.instance()
    layers = map_layers.CommentLayers()
    checks = {}
    try:
        layers.show(store.path, set_id, "Comments", {"closed"}, [])
        map_layers.set_project_register(store.path, set_id)
        qgz = os.path.join(folder, "project.qgz")
        checks["saved"] = project.write(qgz)
        project.clear()
        checks["read"] = project.read(qgz)
        points = layers.find("points")
        checks["reopened with data"] = points is not None and points.isValid() and _n(points) == 3 \
            and points.subsetString() == map_layers._subset(set_id)
        checks["variables saved"] = "|closed|" in (_project_var(project, "closed") or "")
        store.close()
        project.clear()
        moved = os.path.join(folder, "moved.gpkg")
        shutil.move(store.path, moved)
        project.read(qgz)
        layers = map_layers.CommentLayers()
        layers.show(moved, set_id, "Comments")
        points = layers.find("points")
        checks["repointed after a move"] = points.isValid() and _n(points) == 3 and \
            points.source().startswith(moved)
    finally:
        layers.remove()
        project.clear()
        map_layers.set_project_register("", "")
    failed = [k for k, v in checks.items() if not v]
    return _result("layers are saved with the project and repointed when the register moves", not failed,
                   ", ".join(failed))


def _project_var(project, name):
    from qgis.core import QgsExpressionContextUtils
    return QgsExpressionContextUtils.projectScope(project).variable(f"route_comments_{name}")


# --------------------------------------------------------------------------- dock
class _AcceptingNewComment(dock_module.NewCommentDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form.text.setPlainText("Captured through the KP Mouse Tool")


def _png(path):
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(QColor(20, 120, 200))
    image.save(path, "PNG")
    return path


def test_dock(folder):
    iface = _Iface()
    layer = _route_layer()
    QgsProject.instance().addMapLayer(layer)
    register = os.path.join(folder, "dock.gpkg")
    store = CommentStore.create(register)
    set_id = store.list_sets()[0]["set_id"]
    store.save_fields(set_id, [model.CustomField("", "Discipline", model.FIELD_CHOICE, ["Route", "Geotech"])])
    cap = CaptureRoute.from_layer(layer, layer.crs(), start_kp_km=START_KP, use_cartesian=False)
    for i, kp in enumerate((3.0, 1.0, 2.0)):
        store.add_comment(set_id, dict(cap.point_values(START_KP + kp), text=f"Point {i} near boulder" if i == 1
                                       else f"Point {i}", status="Closed" if i == 2 else "Open"))
    store.close()
    map_layers.set_project_register(register, set_id)
    patches = [(dock_module, "NewCommentDialog", _AcceptingNewComment),
               (dock_module, "qt_exec", lambda obj, *a: 1),
               (dock_module.QMessageBox, "question", staticmethod(lambda *a, **k: dock_module.MESSAGEBOX_NO)),
               (dock_module.QMessageBox, "warning", staticmethod(lambda *a, **k: None))]
    saved = [(obj, name, getattr(obj, name)) for obj, name, _v in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    dock = dock_module.RouteCommentsDock(iface, start_kp_for=lambda lyr: START_KP, reference_layer=lambda: layer)
    dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    checks = {}
    try:
        iface.window.show()
        dock.show()
        checks["opens the project's register"] = dock.store is not None and dock.proxy.rowCount() == 3
        dock.table.sortByColumn(dock.table_model.column_keys().index("kp"), Qt.SortOrder.AscendingOrder)
        kps = [dock.by_id[cid]["kp_km"] for cid in dock.visible_ids()]
        checks["sort by KP"] = kps == sorted(kps)
        dock.search.setText("boulder")
        dock._apply_filter()
        checks["search"] = dock.proxy.rowCount() == 1
        dock.search.clear()
        dock.act_hide_closed.setChecked(True)
        checks["hide closed"] = dock.proxy.rowCount() == 2
        dock.clear_filters()
        checks["custom field column"] = "Discipline" in [c[1] for c in dock.table_model.columns]

        # Column layout persists.
        col = dock.table_model.column_keys().index("author")
        dock._set_column_shown(col, False)
        dock.table.horizontalHeader().moveSection(dock.table.horizontalHeader().visualIndex(1), 5)
        dock._save_header()
        other = dock_module.RouteCommentsDock(iface, start_kp_for=lambda lyr: START_KP)
        checks["columns persist"] = other.table.isColumnHidden(col) and \
            other.table.horizontalHeader().visualIndex(1) == 5
        other.shutdown()
        other.deleteLater()

        # Table -> map and map -> table.
        first = dock.visible_ids()[0]
        dock._select_ids([first])
        points = dock.layers.find("points")
        checks["table selects on map"] = dock.layers.selected_ids() == [first] and dock.current_id == first \
            and dock.highlight.wkts_shown() == 1
        second = dock.visible_ids()[1]
        fid = dock.layers.index[second][1]
        points.selectByIds([fid])
        checks["map selects in table"] = dock.selected_ids() == [second]

        # Capture through the hook -> register, table, map.
        added = dock.add_comment_from_capture("kp_range", cap, {"kps": (START_KP + 0.5, START_KP + 1.5)})
        checks["captured range saved"] = added is not None and added["ref_no"] == 4 and \
            added["kp_km"] == START_KP + 0.5 and dock.selected_ids() == [added["comment_id"]] and \
            _n(dock.layers.find("ranges")) == 1

        # Edit, save; a stale save is refused (answer No to overwrite).
        dock.form.text.setPlainText("Edited in the dock")
        dock.form.status.setCurrentText("Resolved")
        checks["dirty"] = dock._dirty and dock.save_button.isEnabled()
        checks["saved"] = dock.save_current() and dock.by_id[added["comment_id"]]["status"] == "Resolved" \
            and dock.by_id[added["comment_id"]]["version"] == 2
        dock._select_ids([added["comment_id"]])
        other_conn = sqlite3.connect(register, timeout=5)
        with other_conn:
            other_conn.execute("UPDATE rc_comment SET text = 'From another project', version = version + 1 "
                               "WHERE comment_id = ?", (added["comment_id"],))
        other_conn.close()
        dock.form.text.setPlainText("Stale edit")
        checks["conflict refused"] = dock.save_current() is False and \
            dock.store.get_comment(added["comment_id"])["text"] == "From another project"
        dock._dirty = False
        dock._data_version = -2
        dock._poll()
        checks["external change picked up"] = dock.by_id[added["comment_id"]]["text"] == "From another project"

        # In-place edits in the table (as the delegate commits them).
        def cell(cid, key):
            row = dock.table_model.row_of(cid)
            return dock.proxy.mapFromSource(dock.table_model.index(row, dock.table_model.column_keys().index(key)))

        cid = added["comment_id"]
        edited = dock.proxy.setData(cell(cid, "status"), "Closed", Qt.ItemDataRole.EditRole)
        QCoreApplication.processEvents()
        fid_key = "cf:" + dock.fields[0].field_id
        edited_cf = dock.proxy.setData(cell(cid, fid_key), "geotech", Qt.ItemDataRole.EditRole)
        QCoreApplication.processEvents()
        stored = dock.store.get_comment(cid)
        checks["inline edits saved"] = edited and edited_cf and stored["status"] == "Closed" and \
            model.load_custom(stored) == {dock.fields[0].field_id: "Geotech"}
        checks["editable columns only"] = bool(dock.table_model.flags(dock.table_model.index(0, 2))
                                               & Qt.ItemFlag.ItemIsEditable) and \
            bool(dock.table_model.flags(dock.table_model.index(0, 1)) & Qt.ItemFlag.ItemIsEditable) and \
            not bool(dock.table_model.flags(dock.table_model.index(0, 0)) & Qt.ItemFlag.ItemIsEditable)
        dock.proxy.setData(cell(cid, "text"), "   ", Qt.ItemDataRole.EditRole)
        checks["empty text refused"] = dock.store.get_comment(cid)["text"] == "From another project"

        # Reply and image.
        dock._select_ids([added["comment_id"]])
        dock.reply_text.setPlainText("Checked on the survey")
        dock.add_reply()
        image = _png(os.path.join(folder, "photo.png"))
        dock_module.QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([image], ""))
        dock._select_ids([added["comment_id"]])
        dock.add_images()
        dock._select_ids([added["comment_id"]])
        dock.pictures.flush()
        checks["thread: comment + reply"] = len(dock.thread.bubbles) == 2 and \
            dock.tabs.tabText(0) == "Thread (1)" and dock.thread.bubbles[1].text == "Checked on the survey"
        checks["image shown"] = len(dock.pictures.items) == 1 and not dock.pictures.view.image.isNull() and \
            not dock.pictures.strip.item(0).icon().isNull()

        # Import: KPs on the route, positions, duplicates.
        table = os.path.join(folder, "import.csv")
        with open(table, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["ID", "Comment", "KP", "KP to", "Latitude", "Longitude", "Discipline"])
            writer.writerow(["X-1", "Imported point", START_KP + 1.25, "", "", "", "geotech"])
            writer.writerow(["X-2", "Imported range", START_KP + 2, START_KP + 3, "", "", ""])
            writer.writerow(["X-3", "Imported position", "", "", "55.03", "3.03", ""])
            writer.writerow(["X-1", "Repeated", START_KP + 1, "", "", "", ""])
            writer.writerow([added["comment_id"], "Already there", START_KP + 1, "", "", "", ""])
        from ..route_comments.dialogs import ImportDialog
        captured = {}

        class _Import(ImportDialog):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.load_file(table)
                captured["dialog"] = self
                ImportDialog.accept(self)

        dock_module.ImportDialog = _Import
        try:
            before = len(dock.comments)
            dock.import_comments()
        finally:
            dock_module.ImportDialog = ImportDialog
        dialog = captured["dialog"]
        imported = [c for c in dock.comments if (c.get("ext_id") or "").startswith("X-")]
        by_ext = {c["ext_id"]: c for c in imported}
        checks["import counts"] = len(dock.comments) == before + 3 and \
            sum(1 for r in dialog.checked if not r.ok) == 2
        checks["import KPs on the route"] = _close(by_ext["X-1"]["kp_km"], START_KP + 1.25, 1e-9) and \
            _pt(by_ext["X-1"]["wkt"]).distance(cap.route.point_at_kp(START_KP + 1.25)) < 1e-9 and \
            by_ext["X-2"]["ref_kind"] == model.REF_KP_RANGE and by_ext["X-1"]["route_layer_id"] == layer.id()
        checks["import position + nearest KP"] = by_ext["X-3"]["ref_kind"] == model.REF_LOCATION and \
            by_ext["X-3"]["kp_km"] is not None and _close(by_ext["X-3"]["lat"], 55.03, 1e-9)
        checks["import custom field"] = model.load_custom(by_ext["X-1"]) == {dock.fields[0].field_id: "Geotech"}

        # Export: visible rows / whole register, CSV and XLSX.
        dock.search.setText("imported")
        dock._apply_filter()
        headers, rows, about = dock.export_data("visible")
        checks["export visible"] = len(rows) == 3 and "Discipline" in headers and about[2][1].startswith("Visible")
        dock.search.clear()
        dock._apply_filter()
        second_set = dock.store.create_set("Other set")
        dock.store.add_comment(second_set["set_id"], {"text": "elsewhere", "ref_kind": model.REF_LOCATION,
                                                      "wkt": "POINT(3 55)"})
        headers, rows, about = dock.export_data("register")
        checks["export register"] = len(rows) == len(dock.comments) + 1 and \
            {r[headers.index("Comment set")] for r in rows} == {"Comments", "Other set"}
        csv_path = os.path.join(folder, "out.csv")
        from ..route_comments.exchange import write_table
        write_table(csv_path, headers, rows, about)
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            read = list(csv.reader(handle))
        checks["csv"] = read[0] == headers and len(read) == len(rows) + 1
        try:
            xlsx_path = os.path.join(folder, "out.xlsx")
            write_table(xlsx_path, headers, rows, about)
            from openpyxl import load_workbook
            book = load_workbook(xlsx_path, read_only=True)
            names = book.sheetnames
            count = book["Comments"].max_row
            book.close()
            checks["xlsx"] = names == ["Comments", "About"] and count == len(rows) + 1
        except ImportError:
            print("  (openpyxl unavailable in this Python; XLSX not checked)")

        # Readable selection, floating window with minimise / maximise.
        from qgis.PyQt.QtGui import QPalette
        from ..qgis_compat import WINDOW_HINT_MIN_MAX
        palette = dock.table.palette()
        checks["selected text readable"] = all(
            palette.color(group, QPalette.ColorRole.HighlightedText) == QColor(255, 255, 255)
            for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive))
        dock.setFloating(True)
        QCoreApplication.processEvents()
        checks["floating window has min/max"] = bool(dock.windowFlags() & WINDOW_HINT_MIN_MAX) and \
            QSettings().value(dock_module.FLOATING_KEY, False, type=bool)
        dock.setFloating(False)
        QCoreApplication.processEvents()

        # Delete removes from table and map.
        dock._select_ids([added["comment_id"]])
        dock_module.QMessageBox.question = staticmethod(lambda *a, **k: dock_module.MESSAGEBOX_YES)
        dock.delete_selected()
        checks["deleted"] = added["comment_id"] not in dock.by_id and \
            added["comment_id"] not in dock.layers.index
    finally:
        dock.shutdown()
        dock.layers.remove()
        dock.deleteLater()
        for obj, name, value in saved:
            setattr(obj, name, value)
        if "getOpenFileNames" in vars(dock_module.QFileDialog):
            delattr(dock_module.QFileDialog, "getOpenFileNames")
        QgsProject.instance().removeMapLayer(layer.id())
        map_layers.set_project_register("", "")
        iface.close()
        QCoreApplication.processEvents()
    failed = [k for k, v in checks.items() if not v]
    return _result("dock: table, columns, map sync, capture, save/conflict, replies, images, import, export",
                   not failed, ", ".join(failed))


class _Clipboard:
    def __init__(self, mime):
        self.mime = mime

    def mimeData(self):  # noqa: N802 - Qt API
        return self.mime


def _red_pixels(image):
    count = 0
    for x in range(0, image.width(), 2):
        for y in range(0, image.height(), 2):
            c = QColor(image.pixel(x, y))
            if c.red() > 200 and c.green() < 60 and c.blue() < 60:
                count += 1
    return count


def _render_comment(layer, wkt, scale=5000, size=500):
    """The layer drawn around a comment at ``scale`` (96 dpi, Web Mercator)."""
    from qgis.core import (QgsExpressionContext, QgsExpressionContextUtils, QgsMapRendererCustomPainterJob,
                           QgsMapSettings)
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QPainter
    mercator = QgsCoordinateReferenceSystem("EPSG:3857")
    xform = QgsCoordinateTransform(QgsCoordinateReferenceSystem(WGS84), mercator, QgsProject.instance())
    centre = xform.transform(_pt(wkt))
    half = size / 96.0 * 0.0254 * scale / 2.0
    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setDestinationCrs(mercator)
    settings.setOutputDpi(96)
    settings.setOutputSize(QSize(size, size))
    settings.setExtent(QgsRectangle(centre.x() - half, centre.y() - half, centre.x() + half, centre.y() + half))
    context = QgsExpressionContext()
    context.appendScope(QgsExpressionContextUtils.globalScope())
    context.appendScope(QgsExpressionContextUtils.projectScope(QgsProject.instance()))
    settings.setExpressionContext(context)
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(QColor(255, 255, 255))
    painter = QPainter(image)
    job = QgsMapRendererCustomPainterJob(settings, painter)
    job.start()
    job.waitForFinished()
    painter.end()
    return image


def test_review_panel(folder):
    """The selected comment's thread beside its images: side by side when the
    dock is wide, stacked when narrow, the image pane small without images.
    Images come from files, a drop and the clipboard; one is drawn in the
    comment's map label (the comment's own text escaped, labels made before
    images updated on request). Send & close / reopen, quick status,
    previous / next with follow on the map; the images are let go when the
    project closes."""
    from qgis.PyQt.QtCore import QMimeData
    iface = _Iface("EPSG:3857")
    layer = _route_layer()
    QgsProject.instance().addMapLayer(layer)
    register = os.path.join(folder, "review folder & co", "review.gpkg")
    os.makedirs(os.path.dirname(register))
    store = CommentStore.create(register)
    set_id = store.list_sets()[0]["set_id"]
    cap = CaptureRoute.from_layer(layer, layer.crs(), start_kp_km=START_KP, use_cartesian=False)
    for i, kp in enumerate((1.0, 2.0, 3.0)):
        store.add_comment(set_id, dict(cap.point_values(START_KP + kp), text=f"Review {i} <b>&</b> [x]",
                                       status="Open"))
    store.close()
    map_layers.set_project_register(register, set_id)
    asked = []
    patches = [(dock_module.QMessageBox, "question",
                staticmethod(lambda *a, **k: asked.append(a[2] if len(a) > 2 else "") or dock_module.MESSAGEBOX_YES)),
               (dock_module.QMessageBox, "warning", staticmethod(lambda *a, **k: None))]
    saved = [(obj, name, getattr(obj, name)) for obj, name, _v in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    dock = dock_module.RouteCommentsDock(iface, start_kp_for=lambda lyr: START_KP)
    dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    checks = {}

    def settle():
        for _i in range(3):
            QCoreApplication.processEvents()

    try:
        iface.window.show()
        dock.show()
        dock.resize(1100, 820)
        settle()
        ids = dock.visible_ids()
        dock._select_ids([ids[0]])
        settle()
        checks["side by side when wide"] = dock.review_split.side_by_side()
        checks["no priority shown as none, not the first choice"] = dock.quick_priority.currentText() == "" and \
            dock.quick_status.currentText() == "Open"
        checks["no image: the image pane is small"] = dock.review_split.compact and \
            dock.pictures.width() <= 240 and dock.tabs.width() > 600

        # Files (an unreadable one is left out), then the clipboard, then a drop.
        photo = _png(os.path.join(folder, "photo.png"))
        fake = os.path.join(folder, "fake.png")
        with open(fake, "wb") as handle:
            handle.write(b"not an image")
        dock.add_image_files([photo, fake])
        dock.pictures.flush()
        settle()
        checks["file added and shown"] = len(dock.pictures.items) == 1 and not dock.pictures.view.image.isNull() \
            and not dock.review_split.compact and dock.pictures.width() > 300
        red = QImage(160, 80, QImage.Format.Format_RGB32)
        red.fill(QColor(255, 0, 0))
        mime = QMimeData()
        mime.setImageData(red)
        real_app = dock_module.QApplication

        class _App:
            @staticmethod
            def clipboard():
                return _Clipboard(mime)

        dock_module.QApplication = _App
        try:
            dock.paste_image()
        finally:
            dock_module.QApplication = real_app
        dock.pictures.flush()
        items = dock.pictures.items
        pasted = items[-1]["attachment_id"] if items else ""
        checks["pasted image added and shown"] = len(items) == 2 and items[1]["name"].startswith("Pasted image") \
            and dock.pictures.current_id() == pasted and dock.pictures.view.image.width() == 160
        from qgis.PyQt.QtCore import QUrl
        from ..route_comments.pictures import pictures_from_mime
        drop = QMimeData()
        drop.setUrls([QUrl.fromLocalFile(photo), QUrl.fromLocalFile(os.path.join(folder, "notes.txt"))])
        paths, data = pictures_from_mime(drop)
        dock.pictures.files_dropped.emit(paths)
        checks["dropped file added"] = paths == [photo] and data is None and len(dock.pictures.items) == 3

        # The pasted (red) image in the map label; the label predates images, so it is updated on request.
        cid = dock.current_id
        points = dock.layers.find("points")
        old = map_layers.LabelSettings().to_json().replace('"images": true, ', "").replace('"image_mm": 30.0, ', "")
        for lyr in dock.layers.layers():
            lyr.setCustomProperty(map_layers.LABELS_PROP, old)
        dock._show_image(pasted)
        dock.pictures.on_map.setChecked(True)
        settle()
        comment = dock.by_id[cid]
        checks["map image saved"] = bool(comment["map_image"]) and comment["map_image"].endswith(".png") \
            and dock.pictures.on_map.isChecked() and dock.pictures.current_id() == pasted
        checks["old labels updated when asked"] = any("not set to show images" in str(q) for q in asked) and \
            not map_layers.labels_predate_images(points)
        settings = map_layers.label_settings_of(points)
        feature = next(f for f in points.getFeatures() if f["comment_id"] == cid)
        context = QgsExpressionContext(QgsExpressionContextUtils.globalProjectLayerScopes(points))
        context.setFeature(feature)
        html = str(QgsExpression(settings.label_expression()).evaluate(context) or "")
        checks["label HTML: image, text escaped"] = html.startswith('<div><img src="file:///') and \
            "%20%26%20co" in html and comment["map_image"] in html and "&lt;b&gt;&amp;&lt;/b&gt;" in html \
            and "<b>" not in html
        image = _render_comment(points, comment["wkt"])
        checks["image drawn in the map label"] = _red_pixels(image) > 300
        image.save(os.path.join(folder, "label.png"))
        dock.pictures.on_map.setChecked(False)
        settle()
        checks["map image off"] = (dock.by_id[cid]["map_image"] or "") == "" and \
            _red_pixels(_render_comment(points, comment["wkt"])) == 0

        # Send & close, then reopen with no text; quick status.
        dock.reply_text.setPlainText("Done on site")
        dock.send_and_toggle()
        closed = dock.by_id[cid]["status"]
        reopen_text = dock.close_button.text()
        dock.send_and_toggle()
        checks["send & close / reopen"] = closed == "Resolved" and reopen_text == "Send && reopen" and \
            dock.by_id[cid]["status"] == "Open" and [b.status_to for b in dock.thread.bubbles[1:]] == \
            ["Resolved", "Open"] and dock.thread.bubbles[1].text == "Done on site"
        dock.quick_status.setCurrentText("In progress")
        dock._quick_set("status")
        settle()
        checks["quick status saved"] = dock.store.get_comment(cid)["status"] == "In progress" and \
            dock.quick_status.currentText() == "In progress"

        # Edit and delete in the thread.
        done = dock.replies[cid][0]["reply_id"]          # "Done on site", set Resolved
        dock.thread.edit_reply(done)
        dock.thread._editing.editor.setPlainText("Done on site, photo added")
        dock.thread.finish_edit()
        settle()
        stored = next(r for r in dock.store.list_replies(cid) if r["reply_id"] == done)
        checks["reply edited in place"] = stored["text"] == "Done on site, photo added" and \
            bool(stored["edited_utc"]) and stored["status_to"] == "Resolved" and not dock.thread.is_editing() and \
            dock.thread.bubbles[1].text == "Done on site, photo added"
        dock.thread.edit_reply(None)
        dock.thread._editing.editor.setPlainText("   ")
        dock.thread.finish_edit()                        # refused: the edit stays open
        empty_kept_open = dock.thread.is_editing()
        dock.thread._editing.editor.setPlainText("Review 0, edited in the thread")
        dock.thread.finish_edit()
        settle()
        checks["comment edited in the thread"] = empty_kept_open and not dock.thread.is_editing() and \
            dock.store.get_comment(cid)["text"] == "Review 0, edited in the thread"
        save_answer = staticmethod(lambda *a, **k: dock_module.QMessageBox.StandardButton.Save)
        dock_module.QMessageBox.question = save_answer
        dock.thread.edit_reply(done)
        dock.thread._editing.editor.setPlainText("Left with an open edit")
        dock._select_ids([ids[1]])
        settle()
        dock_module.QMessageBox.question = patches[0][2]
        checks["open edit saved when leaving"] = dock.current_id == ids[1] and \
            next(r for r in dock.store.list_replies(cid) if r["reply_id"] == done)["text"] == "Left with an open edit"
        dock._select_ids([cid])
        settle()
        dock.send_and_toggle()                           # In progress -> Resolved
        asked_delete = []
        dock._ask_reply_delete = lambda to, back, now: asked_delete.append((to, back, now)) or "restore"
        dock.delete_reply(dock.replies[cid][-1]["reply_id"])
        settle()
        checks["Send & close deleted, status back"] = asked_delete[-1] == ("Resolved", "In progress", "Resolved") \
            and dock.by_id[cid]["status"] == "In progress"
        dock.delete_reply(done)                          # an older status change: nothing to put back
        settle()
        checks["older status reply deleted, status kept"] = asked_delete[-1] == ("Resolved", "", "In progress") \
            and dock.by_id[cid]["status"] == "In progress" and \
            done not in [r["reply_id"] for r in dock.replies.get(cid, [])]
        del dock._ask_reply_delete
        dock._select_ids([ids[2]])
        dock.thread.comment_delete_requested.emit()      # question patched to Yes
        settle()
        checks["comment deleted from the thread"] = ids[2] not in dock.by_id
        ids = ids[:2]

        # Previous / next, following the map.
        dock._select_ids([ids[0]])
        prev_at_first = dock.prev_button.isEnabled()
        canvas = iface.canvas
        canvas.setExtent(QgsRectangle(0, 0, 1000, 1000))   # far from the route
        dock.follow_button.setChecked(True)
        dock.step(1)
        settle()
        centre = QgsCoordinateTransform(QgsCoordinateReferenceSystem(WGS84), canvas.mapSettings().destinationCrs(),
                                        QgsProject.instance()).transform(_pt(dock.by_id[ids[1]]["wkt"]))
        checks["next comment, followed on the map"] = dock.current_id == ids[1] and not prev_at_first and \
            canvas.extent().contains(centre)
        dock.step(1)
        dock.step(1)   # stays on the last
        checks["stops at the last"] = dock.current_id == ids[-1] and not dock.next_button.isEnabled()
        dock.follow_button.setChecked(False)

        # Narrow: one above the other.
        dock.resize(430, 820)
        settle()
        checks["stacked when narrow"] = not dock.review_split.side_by_side()
        dock.resize(1100, 820)
        settle()
        checks["side by side again"] = dock.review_split.side_by_side()

        # The project closes: images and thumbnails let go.
        dock._select_ids([cid])
        dock.pictures.flush()
        had_thumbs = len(dock.thumb_cache) > 0
        QgsProject.instance().clear()
        settle()
        checks["project closed: images let go"] = had_thumbs and len(dock.thumb_cache) == 0 and \
            not dock.pictures.items and dock.pictures.view.image.isNull() and dock.store is None
    finally:
        dock.shutdown()
        dock.layers.remove()
        dock.deleteLater()
        for obj, name, value in saved:
            setattr(obj, name, value)
        QgsProject.instance().clear()
        map_layers.set_project_register("", "")
        iface.close()
        QCoreApplication.processEvents()
    failed = [k for k, v in checks.items() if not v]
    return _result("review panel: thread beside images, map image label, Send & close, step and follow",
                   not failed, ", ".join(failed))


def test_no_stray_windows(folder):
    """Clicking between comments, editing in the thread and opening the new
    comment form show no window of their own: a widget made visible before it
    has a parent flashes up on screen as a little window."""
    from qgis.PyQt.QtCore import QObject
    from qgis.PyQt.QtWidgets import QApplication, QWidget

    class _Shows(QObject):
        def __init__(self):
            super().__init__()
            self.windows = []

        def eventFilter(self, obj, event):  # noqa: N802 - Qt API
            if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
                self.windows.append(f"{type(obj).__name__} {getattr(obj, 'text', lambda: '')()!r}"[:80])
            return False

    iface = _Iface()
    layer = _route_layer()
    QgsProject.instance().addMapLayer(layer)
    register = os.path.join(folder, "stray.gpkg")
    store = CommentStore.create(register)
    set_id = store.list_sets()[0]["set_id"]
    cap = CaptureRoute.from_layer(layer, layer.crs(), start_kp_km=START_KP, use_cartesian=False)
    ids = []
    for i in range(3):
        cid = store.add_comment(set_id, dict(cap.point_values(START_KP + 1 + i), text=f"Comment {i}"))["comment_id"]
        store.add_reply(cid, "A", f"Reply to {i}")
        store.add_reply(cid, "B", "", status_to="Resolved")
        ids.append(cid)
    store.close()
    map_layers.set_project_register(register, set_id)
    dock = dock_module.RouteCommentsDock(iface, start_kp_for=lambda lyr: START_KP)
    dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    shows = _Shows()
    try:
        iface.window.show()
        dock.show()
        QCoreApplication.processEvents()
        QApplication.instance().installEventFilter(shows)
        for cid in ids + ids[::-1]:
            dock._select_ids([cid])
            QCoreApplication.processEvents()
        dock.thread.edit_reply(dock.replies[ids[0]][0]["reply_id"])
        dock.thread.cancel_edit()
        values = cap.point_values(START_KP + 1.5)
        form = dock_module.NewCommentDialog(dock, values, cap, dock.statuses(), dock.priorities(), dock.fields,
                                            "Open", "Normal", "Comments")
        QCoreApplication.processEvents()
        form.deleteLater()
    finally:
        QApplication.instance().removeEventFilter(shows)
        dock.shutdown()
        dock.layers.remove()
        dock.deleteLater()
        QgsProject.instance().removeMapLayer(layer.id())
        map_layers.set_project_register("", "")
        iface.close()
        QCoreApplication.processEvents()
    return _result("no stray windows when clicking between comments or opening the comment form",
                   not shows.windows, "; ".join(shows.windows[:6]))


def test_new_register_in_a_new_project(folder):
    """Every unsaved project suggests the same register name: New register…
    in a new project starts an empty register instead of reopening the last
    one (whose comments came back on the map); an existing register chosen
    by name is never overwritten and only opened when asked."""
    from qgis.PyQt.QtWidgets import QFileDialog
    iface = _Iface()
    project = QgsProject.instance()
    default = os.path.join(folder, "unnamed_comments.gpkg")
    calls, answers = [], {"paths": [], "choice": ""}

    class _SaveDialog:
        Option = QFileDialog.Option

        @staticmethod
        def getSaveFileName(_parent, _title, path, *_args, **kwargs):
            calls.append((path, kwargs.get("options")))
            return (answers["paths"].pop(0) if answers["paths"] else path), ""

    patches = [(dock_module, "default_register_path", lambda *_a: default),
               (dock_module, "QFileDialog", _SaveDialog),
               (dock_module.RouteCommentsDock, "_ask_existing_register", lambda _self, _path: answers["choice"])]
    saved = [(obj, name, getattr(obj, name)) for obj, name, _v in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    map_layers.set_project_register("", "")
    canvas_items = len(iface.canvas.scene().items())
    dock = dock_module.RouteCommentsDock(iface)
    checks = {}
    try:
        checks["first register at the default"] = dock.new_register() and dock.store.path == default and \
            calls[-1][1] == QFileDialog.Option.DontConfirmOverwrite
        dock.store.add_comment(dock.set_row["set_id"], {"text": "old", "ref_kind": model.REF_LOCATION,
                                                        "wkt": "POINT(3 55)"})
        dock.reload()
        dock._select_ids(dock.visible_ids())
        checks["old comment on the map, highlighted"] = _n(dock.layers.find("points")) == 1 and \
            len(iface.canvas.scene().items()) > canvas_items

        project.clear()   # File > New
        checks["new project: no register, layers or highlight"] = dock.store is None and \
            not dock.layers.layers() and len(iface.canvas.scene().items()) == canvas_items and \
            project.layerTreeRoot().findGroup(map_layers.GROUP_NAME) is None
        fresh = os.path.join(folder, "unnamed_comments_2.gpkg")
        checks["new register: a free name, empty"] = dock.new_register() and calls[-1][0] == fresh and \
            dock.store.path == fresh and dock.comments == [] and _n(dock.layers.find("points")) == 0

        project.clear()
        answers.update(paths=[default], choice="")
        checks["existing register picked, cancelled"] = not dock.new_register() and dock.store is None
        answers.update(paths=[default, ""], choice="rename")
        checks["choose another name: a free one offered"] = not dock.new_register() and \
            calls[-1][0] == os.path.join(folder, "unnamed_comments_3.gpkg") and dock.store is None
        answers.update(paths=[default], choice="open")
        checks["open it: the old comments"] = dock.new_register() and dock.store.path == default and \
            [c["text"] for c in dock.comments] == ["old"]
    finally:
        dock.shutdown()
        dock.layers.remove()
        dock.deleteLater()
        for obj, name, value in saved:
            setattr(obj, name, value)
        project.clear()
        map_layers.set_project_register("", "")
        iface.close()
        QCoreApplication.processEvents()
    failed = [k for k, v in checks.items() if not v]
    return _result("new register in a new project: never the last project's comments", not failed,
                   ", ".join(failed))


def test_convention_token_unmeasured_utm():
    """An automatic UTM grid is named once measured (as the KP Mouse Tool's is)."""
    layer = _route_layer()
    from ..route_comments.route_ref import current_convention, kp_tool_distance
    old = (kp_range_utils.kp_distance_mode(), kp_range_utils.kp_grid_crs_setting())
    try:
        kp_range_utils.set_kp_distance_settings(kp_range_utils.KP_MODE_CARTESIAN, "")
        project_crs = QgsProject.instance().crs()
        QgsProject.instance().setCrs(QgsCoordinateReferenceSystem(WGS84))
        fresh = kp_convention_token(kp_tool_distance(layer, layer.crs()))
        resolved = current_convention(layer)
        QgsProject.instance().setCrs(project_crs)
    finally:
        kp_range_utils.set_kp_distance_settings(*old)
    return _result("grid convention: automatic UTM zone resolved before comparing",
                   fresh == "grid:utm-auto" and resolved == "grid:EPSG:32631", f"{fresh} / {resolved}")


def test_register_opens_as_gpkg_layer(folder):
    """QGIS (OGR) reads the register's comments as an ordinary GeoPackage layer."""
    store = CommentStore.create(os.path.join(folder, "ogr.gpkg"))
    set_id = store.list_sets()[0]["set_id"]
    store.add_comment(set_id, {"text": "a point", "ref_kind": model.REF_LOCATION, "wkt": "POINT(3.5 55.25)"})
    store.add_comment(set_id, {"text": "a range", "ref_kind": model.REF_KP_RANGE,
                               "wkt": "LINESTRING(3 55, 3.1 55.1)"})
    store.close()
    points = QgsVectorLayer(f"{store.path}|layername=rc_comment|geometrytype=Point", "p", "ogr")
    lines = QgsVectorLayer(f"{store.path}|layername=rc_comment|geometrytype=LineString", "l", "ogr")
    ok = points.isValid() and lines.isValid() and points.featureCount() == 1 and lines.featureCount() == 1
    if ok:
        point = next(points.getFeatures())
        ok = point["text"] == "a point" and point.geometry().asPoint() == QgsPointXY(3.5, 55.25) \
            and points.crs().authid() == WGS84
    del points, lines
    return _result("the register opens in QGIS as a GeoPackage layer (EPSG:4326)", ok)


def run_all():
    results = []
    old_format = QSettings.defaultFormat()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        try:
            for test in (test_capture_kps_match_route_model, test_kp_mouse_tool_comment_entries,
                         lambda: test_reference_conditions(tempfile.mkdtemp(dir=temp)),
                         lambda: test_dense_map_layers(tempfile.mkdtemp(dir=temp)),
                         lambda: test_dock(tempfile.mkdtemp(dir=temp)),
                         lambda: test_review_panel(tempfile.mkdtemp(dir=temp)),
                         lambda: test_no_stray_windows(tempfile.mkdtemp(dir=temp)),
                         lambda: test_new_register_in_a_new_project(tempfile.mkdtemp(dir=temp)),
                         lambda: test_register_opens_as_gpkg_layer(tempfile.mkdtemp(dir=temp)),
                         test_convention_token_unmeasured_utm,
                         lambda: test_layers_saved_with_project(tempfile.mkdtemp(dir=temp))):
                try:
                    results.append(test())
                except Exception as exc:  # report, keep going
                    import traceback
                    traceback.print_exc()
                    results.append(_result(getattr(test, "__name__", "test"), False, repr(exc)))
        finally:
            from ..route_comments.store import close_connection
            close_connection()
            QSettings.setDefaultFormat(old_format)
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
