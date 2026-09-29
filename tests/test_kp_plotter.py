# -*- coding: utf-8 -*-
"""QGIS checks for the KP Data Plotter dock.

* Hovering the plot moves the map marker without refreshing the map canvas
  (a full re-render of every layer per mouse move before).
* Nearest-KP snapping (bisect) and KP -> point interpolation (bisect on
  precomputed segment ends) give exactly what the linear scans gave,
  including duplicate KPs, reverse KPs and route ends.
* ``shutdown()`` removes the marker and disconnects the project signal,
  and is safe to call twice.

User settings are never written: the dock's ``save_user_settings`` is
stubbed out for the test.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtWidgets import QMainWindow

from ..kp_plotter_dockwidget import KpPlotterDockWidget


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


class _Iface(QObject):
    projectRead = pyqtSignal()

    def __init__(self):
        super().__init__()
        from qgis.gui import QgsMapCanvas
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.refreshes = 0
        real_refresh = self.canvas.refresh

        def counting_refresh():
            self.refreshes += 1
            real_refresh()
        self.canvas.refresh = counting_refresh

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas


def _layers(project):
    line = QgsVectorLayer("LineString?crs=EPSG:4326", "plotter route", "memory")
    feats = []
    for wkt in ("LINESTRING(3.00 55.00, 3.02 55.01, 3.02 55.01, 3.05 55.02)",
                "LINESTRING(3.05 55.02, 3.07 55.05, 3.10 55.06)"):
        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromWkt(wkt))
        feats.append(feat)
    line.dataProvider().addFeatures(feats)
    table = QgsVectorLayer("None?field=kp:double&field=depth:double", "plotter table", "memory")
    rng = random.Random(40)
    rows = []
    for kp in [0.0, 0.5, 0.5, 1.25, 2.0, 2.0, 3.3, 4.1, 5.0, 6.2, 7.0, 7.5]:
        feat = QgsFeature(table.fields())
        feat.setAttributes([kp, 100 + rng.uniform(-5, 5)])
        rows.append(feat)
    for bad in (None, float("nan")):      # no plottable KP
        feat = QgsFeature(table.fields())
        feat.setAttributes([bad, 1.0])
        rows.append(feat)
    table.dataProvider().addFeatures(rows)
    project.addMapLayers([line, table])
    return line, table


def _plotted_dock(iface, line, table, reverse=False):
    dock = KpPlotterDockWidget(iface)
    dock.save_user_settings = lambda: None      # never persist test choices
    dock.line_layer_combo.setCurrentIndex(dock.line_layer_combo.findData(line.id()))
    dock.table_layer_combo.setCurrentIndex(dock.table_layer_combo.findData(table.id()))
    dock.kp_field_combo.setCurrentText("kp")
    for i in range(dock.data_fields_list.count()):
        item = dock.data_fields_list.item(i)
        item.setSelected(item.text() == "depth")
    dock.reverse_kp_checkbox.setChecked(reverse)
    dock.plot_data()
    return dock


def _legacy_interpolate(dock, distance_m):
    """The dock's previous KP -> point walk."""
    if dock.reverse_kp_checkbox.isChecked():
        distance_m = dock.line_length - distance_m
    if distance_m <= 0:
        return QgsPointXY(dock.line_parts[0][0])
    if distance_m >= dock.line_length:
        return QgsPointXY(dock.line_parts[-1][-1])
    cumulative = 0.0
    for part in dock.line_parts:
        for i in range(len(part) - 1):
            p1, p2 = part[i], part[i + 1]
            seg = dock.distance_area.measureLine(p1, p2)
            if cumulative + seg >= distance_m:
                ratio = (distance_m - cumulative) / seg if seg > 0 else 0
                return QgsPointXY(p1.x() + ratio * (p2.x() - p1.x()), p1.y() + ratio * (p2.y() - p1.y()))
            cumulative += seg
    return None


def _legacy_nearest_index(values, x):
    best, index = float("inf"), None
    for i, v in enumerate(values):
        if abs(x - v) < best:
            best, index = abs(x - v), i
    return index


def test_lookups_match_linear_scans(iface, line, table):
    ok, detail = True, []
    rng = random.Random(41)
    for reverse in (False, True):
        dock = _plotted_dock(iface, line, table, reverse)
        try:
            probes = [rng.uniform(-1.0, 9.0) for _ in range(400)]
            probes += list(dock.kp_sorted) + [0.25, 2.0, 6.6, 1.25 + 1e-12]   # exact / tie points
            idx_bad = sum(1 for x in probes
                          if dock._nearest_kp_index(x) != _legacy_nearest_index(dock.kp_sorted, x))
            dist_bad = 0
            for i in range(301):
                d = dock.line_length * (i / 300.0) * 1.02 - 0.01 * dock.line_length
                new = dock.interpolate_point_along_line(d)
                old = _legacy_interpolate(dock, d)
                new_xy = new.asPoint() if new is not None else None
                if (old is None) != (new_xy is None) or (
                        old is not None and (old.x() != new_xy.x() or old.y() != new_xy.y())):
                    dist_bad += 1
            finite = len(dock.kp_sorted) == 12          # NULL / NaN KPs dropped
            ok = ok and idx_bad == 0 and dist_bad == 0 and finite
            detail.append("reverse=%s: %d index + %d point mismatches, %d KPs"
                          % (reverse, idx_bad, dist_bad, len(dock.kp_sorted)))
        finally:
            dock.shutdown()
            dock.deleteLater()
    return _result("bisect lookups == linear scans (duplicates, reverse, ends)", ok, "; ".join(detail))


def test_hover_moves_marker_without_canvas_refresh(iface, line, table):
    dock = _plotted_dock(iface, line, table)
    try:
        iface.refreshes = 0
        for x in (0.3, 1.1, 2.6, 7.4, 3.3):
            dock.on_mouse_move(SimpleNamespace(inaxes=True, xdata=x))
        placed = dock.marker is not None and dock.marker.isVisible()
        expected = dock.interpolate_point_along_line(3.3 * 1000).asPoint()
        center = dock.marker.center() if placed else None
        on_kp = center is not None and abs(center.x() - expected.x()) < 1e-12
        dock.on_mouse_move(SimpleNamespace(inaxes=None, xdata=None))
        hidden = not dock.marker.isVisible()
        ok = placed and on_kp and hidden and iface.refreshes == 0
        return _result("hover moves the marker with no map-canvas refresh", ok,
                       "refreshes=%d placed=%s on_kp=%s hidden=%s" % (iface.refreshes, placed, on_kp, hidden))
    finally:
        dock.shutdown()
        dock.deleteLater()


def test_shutdown_is_complete_and_idempotent(iface, line, table):
    scene = iface.canvas.scene()
    before = len(scene.items())
    receivers = iface.receivers(iface.projectRead)
    dock = _plotted_dock(iface, line, table)
    connected = iface.receivers(iface.projectRead) - receivers
    dock.on_mouse_move(SimpleNamespace(inaxes=True, xdata=2.0))
    with_marker = len(scene.items())
    dock.shutdown()
    dock.shutdown()
    left = iface.receivers(iface.projectRead) - receivers
    after = len(scene.items())
    ok = (with_marker == before + 1 and after == before and dock.marker is None
          and connected == 1 and left == 0)
    dock.deleteLater()
    return _result("shutdown() removes the marker, disconnects projectRead, twice is safe", ok,
                   "scene items %d -> %d -> %d, projectRead receivers %d -> %d"
                   % (before, with_marker, after, connected, left))


def run_all():
    project = QgsProject.instance()
    iface = _Iface()
    line, table = _layers(project)
    try:
        return [
            test_lookups_match_linear_scans(iface, line, table),
            test_hover_moves_marker_without_canvas_refresh(iface, line, table),
            test_shutdown_is_complete_and_idempotent(iface, line, table),
        ]
    finally:
        project.removeMapLayers([line.id(), table.id()])
        iface.canvas.close()
        iface.window.close()
