# -*- coding: utf-8 -*-
"""Project New / Open / Close and the map tools and plot docks.

QGIS does not remove a plugin's canvas items, and opening a project leaves
a plugin map tool active, so each tool drops what belongs to the old
project itself on ``QgsProject.aboutToBeCleared``:

* KP Mouse Tool: turned off (no RuntimeError from its CRS hook on the next
  map CRS change), its graphics, sampler and live profile released; removing
  its reference line turns it off too. Reconfiguring, toggling off and
  unloading leave no orphaned map tool on the canvas. Its button follows
  the canvas's map tool.
* Transit Measure: disposed on Open; legs are measured in the map CRS.
* Depth Profile and KP Plot docks: plot, map marker and drawn line gone,
  layer lists refreshed for the new project, hooks alive after the dock is
  closed and shown again and disconnected by shutdown.

Uses the real ``QgsProject.instance().clear()`` / ``read()`` on projects
written to a temporary folder (layers created by the project reader, as in
QGIS). The app steps around them are mimicked: the canvas follows the
project CRS and ``newProjectCreated`` / ``projectRead`` are emitted.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
                       QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QObject, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QMainWindow, QMenu

REQUIRES_QGIS = True

_UTM = "EPSG:32631"
_MERCATOR = "EPSG:3857"
_ROUTE_WKT = "LINESTRING(500000 6000000, 505000 6003000, 510000 6003000)"


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def _pump(ms=0):
    """Run posted events, deferred deletes and timers for ``ms``."""
    end = time.monotonic() + ms / 1000.0
    while True:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        if time.monotonic() >= end:
            break
        time.sleep(0.005)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@contextmanager
def _slot_errors():
    """Collect exceptions raised in Qt slots (QGIS shows them as Python errors)."""
    errors = []
    previous = sys.excepthook
    sys.excepthook = lambda etype, value, tb: errors.append(
        "".join(traceback.format_exception_only(etype, value)).strip())
    try:
        yield errors
    finally:
        sys.excepthook = previous


# -- stub iface ----------------------------------------------------------------
class _TreeView:
    def refreshLayerSymbology(self, *_args):
        pass


class _Iface(QObject):
    """The parts of QgisInterface these tools use, on real widgets."""

    projectRead = pyqtSignal()
    newProjectCreated = pyqtSignal()

    def __init__(self):
        super().__init__()
        from qgis.gui import QgsMapCanvas, QgsMessageBar
        self.window = QMainWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar(self.window)
        self.toolbar = self.window.addToolBar("Plugins")
        self.menus = {}
        self.window.resize(900, 700)
        self.window.show()  # off screen; docks in it then get show events
        self.baseline = set(self.canvas.scene().items())

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def layerTreeView(self):
        return _TreeView()

    def addToolBarWidget(self, widget):
        return self.toolbar.addWidget(widget)

    def removeToolBarIcon(self, action):
        self.toolbar.removeAction(action)

    def addPluginToMenu(self, name, action):
        self.menus.setdefault(name, QMenu(name, self.window)).addAction(action)

    def removePluginMenu(self, name, action):
        menu = self.menus.get(name)
        if menu is not None:
            menu.removeAction(action)

    def plugin_items(self):
        """Canvas items added since the iface was built: (total, visible)."""
        items = [i for i in self.canvas.scene().items() if i not in self.baseline]
        return len(items), sum(1 for i in items if i.isVisible())

    def close(self):
        self.canvas.close()
        self.window.close()


# -- projects --------------------------------------------------------------------
def _write_data(folder):
    """A GeoPackage (route line + KP table) and a GeoTIFF depth grid, UTM 31N."""
    import numpy as np
    from osgeo import gdal, ogr, osr
    gpkg = os.path.join(folder, "data.gpkg")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32631)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)
    lines = ds.CreateLayer("route", srs, ogr.wkbLineString)
    feature = ogr.Feature(lines.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkt(_ROUTE_WKT))
    lines.CreateFeature(feature)
    feature = None
    table = ds.CreateLayer("kp_table", None, ogr.wkbNone)
    table.CreateField(ogr.FieldDefn("kp", ogr.OFTReal))
    table.CreateField(ogr.FieldDefn("val", ogr.OFTReal))
    for i in range(20):
        row = ogr.Feature(table.GetLayerDefn())
        row.SetField("kp", i * 0.5)
        row.SetField("val", 10.0 + i)
        table.CreateFeature(row)
        row = None
    ds = None
    tif = os.path.join(folder, "depth.tif")
    raster = gdal.GetDriverByName("GTiff").Create(tif, 120, 60, 1, gdal.GDT_Float32)
    raster.SetGeoTransform((499000, 100, 0, 6004000, 0, -100))
    raster.SetProjection(srs.ExportToWkt())
    raster.GetRasterBand(1).WriteArray(
        (np.arange(7200, dtype="float32").reshape(60, 120) % 50) + 20)
    raster = None
    return gpkg, tif


def _write_projects(folder):
    """Two projects on the same data: UTM 31N and Web Mercator project CRS."""
    gpkg, tif = _write_data(folder)
    project = QgsProject.instance()
    paths = []
    for name, crs in (("utm.qgs", _UTM), ("mercator.qgs", _MERCATOR)):
        project.clear()
        project.addMapLayers([
            QgsVectorLayer(gpkg + "|layername=route", "route", "ogr"),
            QgsVectorLayer(gpkg + "|layername=kp_table", "kp_table", "ogr"),
            QgsRasterLayer(tif, "depth", "gdal")])
        project.setCrs(QgsCoordinateReferenceSystem(crs))
        path = os.path.join(folder, name)
        if not project.write(path):
            raise RuntimeError("could not write " + path)
        paths.append(path)
    project.clear()
    return paths


def _new_project(iface):
    """File > New: clear, the default CRS on the canvas, newProjectCreated."""
    project = QgsProject.instance()
    project.clear()
    project.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    iface.canvas.setDestinationCrs(project.crs())
    iface.newProjectCreated.emit()
    _pump(250)


def _open_project(iface, path):
    """Open: read() (which clears first), the canvas follows, projectRead."""
    project = QgsProject.instance()
    if not project.read(path):
        raise RuntimeError("could not read " + path)
    layers = {layer.name(): layer for layer in project.mapLayers().values()}
    iface.canvas.setDestinationCrs(project.crs())
    iface.canvas.setLayers(list(layers.values()))
    iface.canvas.setExtent(iface.canvas.mapSettings().layerExtentToOutputExtent(
        layers["route"], layers["route"].extent()))
    iface.projectRead.emit()
    _pump(250)
    return layers


def _receivers(signal_owner, signal):
    return signal_owner.receivers(signal)


# -- KP Mouse Tool -----------------------------------------------------------------
@contextmanager
def _kp_wrapper(iface):
    from ..maptools import kp_mouse_maptool as kmm
    wrapper = kmm.KPMouseTool(iface)
    wrapper.save_settings = lambda: None      # never persist test choices
    wrapper.initGui()
    try:
        yield wrapper
    finally:
        wrapper.unload()
        _pump()


def _kp_tools_on_canvas(iface):
    from ..maptools.kp_mouse_maptool import KPMouseMapTool
    return sum(1 for child in iface.canvas.children()
               if isinstance(child, KPMouseMapTool) and not sip.isdeleted(child))


def _kp_tool_on(wrapper, layers, with_window=True):
    wrapper.referenceLayer = layers["route"]
    wrapper.showDepth = True
    wrapper.depthSourceRefs = [(layers["depth"].id(), "")]
    wrapper.toolButton.setChecked(True)
    tool = wrapper.mapTool
    point = QgsPointXY(505000, 6003100)
    hit = tool._locate(point)
    from qgis.core import QgsGeometry
    tool.rubberBand.setToGeometry(QgsGeometry.fromPolylineXY([point, hit.snapped_xy]), None)
    tool.closestPointMarker.setCenter(hit.snapped_xy)
    tool.range_bearing_origin = QgsPointXY(503000, 6002000)
    tool.rangeBearingOriginMarker.setCenter(tool.range_bearing_origin)
    tool.rangeBearingOriginMarker.show()
    tool._update_range_bearing_graphics(point, 2000.0)
    tool._hover_depth_lines(point, hit.snapped_xy, hit.dcc_m, timed=False)  # builds the sampler
    window = None
    if with_window:
        tool.showDepthProfile = True
        window = tool._ensure_profile_window()   # holds the sampler (never shown)
    return tool, window


def test_kp_tool_off_on_new_and_open(iface, utm, mercator):
    """New and Open (another CRS) with the tool on: turned off and released,
    and the map CRS change that follows raises nothing."""
    problems = []
    with _kp_wrapper(iface) as wrapper:
        for label, change in (("New", lambda: _new_project(iface)),
                              ("Open", lambda: _open_project(iface, mercator))):
            layers = _open_project(iface, utm)
            tool, window = _kp_tool_on(wrapper, layers)
            if iface.plugin_items()[1] < 3 or tool._depth_sampler is None or window is None:
                problems.append("%s: setup did not draw/sample" % label)
            with _slot_errors() as errors:
                change()
                # A later map CRS change (e.g. the next project) must not
                # reach the old tool either.
                iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(_UTM))
                _pump()
            state = {
                "slot errors": errors,
                "tool active": iface.canvas.mapTool() is not None,
                "button checked": wrapper.toolButton.isChecked(),
                "wrapper.mapTool": wrapper.mapTool is not None,
                "canvas items": iface.plugin_items()[0],
                "tool deleted": sip.isdeleted(tool),
                "window deleted": sip.isdeleted(window),
                "reference": wrapper.referenceLayer is not None,
            }
            expected = {"slot errors": [], "tool active": False, "button checked": False,
                        "wrapper.mapTool": False, "canvas items": 0, "tool deleted": True,
                        "window deleted": True, "reference": False}
            if state != expected:
                problems.append("%s: %s" % (label, state))
    return _result("KP Mouse Tool: New / Open turn the tool off, release it, no RuntimeError",
                   not problems, "; ".join(problems))


def test_kp_tool_layer_removed(iface, utm):
    """Removing the reference line turns the tool off; removing only a depth
    layer keeps it on and releases the sampler and live profile."""
    layers = _open_project(iface, utm)
    problems = []
    with _kp_wrapper(iface) as wrapper:
        tool, window = _kp_tool_on(wrapper, layers)
        with _slot_errors() as errors:
            QgsProject.instance().removeMapLayer(layers["depth"].id())
            _pump()
            if wrapper.mapTool is not tool or tool._depth_sampler is not None or tool.depthSources:
                problems.append("depth layer removal: tool=%s sampler=%s sources=%d" % (
                    wrapper.mapTool is tool, tool._depth_sampler is not None, len(tool.depthSources)))
            if not sip.isdeleted(window):
                problems.append("live profile window kept the removed layer")
            QgsProject.instance().removeMapLayer(layers["route"].id())
            iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(_MERCATOR))
            _pump()
        if errors or wrapper.mapTool is not None or wrapper.toolButton.isChecked() \
                or iface.plugin_items()[0] or not sip.isdeleted(tool):
            problems.append("route removal: errors=%s tool=%s checked=%s items=%s" % (
                errors, wrapper.mapTool is not None, wrapper.toolButton.isChecked(),
                iface.plugin_items()))
    return _result("KP Mouse Tool: removing its line turns it off; a depth layer is released",
                   not problems, "; ".join(problems))


class _AcceptedConfig:
    """Stands in for KPConfigDialog: OK with the given reference line."""
    layer = None

    def __init__(self, *_args, **_kwargs):
        self.start_kp = 0.0
        self.show_hover_depth = True

    def get_settings(self):
        return (self.layer, "km", False, False, False, [], False, False, False, "DD", "LABELLED")


def test_kp_tool_no_orphans(iface, utm):
    """First use (button -> configure -> OK), reconfiguring while on,
    toggling off and unloading leave no map tool or canvas item behind."""
    from ..maptools import kp_mouse_maptool as kmm
    layers = _open_project(iface, utm)
    saved = (kmm.KPConfigDialog, kmm.qt_exec, kmm.set_kp_distance_settings)
    _AcceptedConfig.layer = layers["route"]
    kmm.KPConfigDialog = _AcceptedConfig
    kmm.qt_exec = lambda *_args: True
    kmm.set_kp_distance_settings = lambda *_args, **_kwargs: None
    counts = {}
    try:
        with _slot_errors() as errors:
            with _kp_wrapper(iface) as wrapper:
                wrapper.referenceLayer = None              # first use / after a project change
                wrapper.toolButton.setChecked(True)        # -> configure dialog -> OK
                _pump()
                counts["first use"] = (_kp_tools_on_canvas(iface), iface.plugin_items()[0])
                wrapper.show_config_dialog()               # reconfigure while on, twice
                wrapper.show_config_dialog()
                _pump()
                counts["reconfigured"] = (_kp_tools_on_canvas(iface), iface.plugin_items()[0])
                wrapper.toolButton.setChecked(False)
                _pump()
                counts["toggled off"] = (_kp_tools_on_canvas(iface), iface.plugin_items()[0])
                wrapper.toolButton.setChecked(True)        # on again; unload while on
                _pump()
            counts["unloaded"] = (_kp_tools_on_canvas(iface), iface.plugin_items()[0])
            _new_project(iface)                            # nothing left to raise
    finally:
        kmm.KPConfigDialog, kmm.qt_exec, kmm.set_kp_distance_settings = saved
    one_tool = (1, 6)
    ok = (counts.get("first use") == one_tool and counts.get("reconfigured") == one_tool
          and counts.get("toggled off") == (0, 0) and counts.get("unloaded") == (0, 0)
          and not errors)
    return _result("KP Mouse Tool: no orphaned map tools (first use, reconfigure, off, unload)",
                   ok, "(tools, items) %s, errors %s" % (counts, errors))


def test_kp_button_follows_map_tool(iface, utm):
    """Picking another map tool unticks the button; a tool that borrowed the
    canvas restoring the KP tool ticks it again; clicking the button after
    another tool gives one working KP tool."""
    from qgis.gui import QgsMapToolPan
    layers = _open_project(iface, utm)
    pan = QgsMapToolPan(iface.canvas)
    problems = []
    with _slot_errors() as errors:
        with _kp_wrapper(iface) as wrapper:
            button = wrapper.toolButton
            tool, _window = _kp_tool_on(wrapper, layers, with_window=False)
            iface.canvas.setMapTool(pan)
            _pump()
            if button.isChecked() or tool.closestPointMarker.isVisible():
                problems.append("another tool: checked=%s marker shown=%s" % (
                    button.isChecked(), tool.closestPointMarker.isVisible()))
            iface.canvas.setMapTool(tool)              # handed back by a borrowing tool
            _pump()
            if not button.isChecked() or wrapper.mapTool is not tool:
                problems.append("restored: checked=%s same tool=%s" % (
                    button.isChecked(), wrapper.mapTool is tool))
            iface.canvas.setMapTool(pan)
            button.setChecked(True)                    # the user clicks the button again
            _pump()
            if not button.isChecked() or wrapper.mapTool is None \
                    or iface.canvas.mapTool() is not wrapper.mapTool or _kp_tools_on_canvas(iface) != 1:
                problems.append("clicked again: checked=%s active=%s tools=%d" % (
                    button.isChecked(), iface.canvas.mapTool() is wrapper.mapTool,
                    _kp_tools_on_canvas(iface)))
            button.setChecked(False)
            _pump()
            if iface.canvas.mapTool() is not None or _kp_tools_on_canvas(iface):
                problems.append("turned off: a KP tool is left")
    pan.deleteLater()
    _pump()
    if errors:
        problems.append("errors %s" % errors)
    return _result("KP Mouse Tool: the button follows the canvas's map tool",
                   not problems, "; ".join(problems))


# -- Transit Measure -------------------------------------------------------------
def test_transit_open_and_crs(iface, utm, mercator):
    """Open disposes the active tool's dialog and drawing; a leg drawn in the
    new project is measured in its CRS; cleanup() stops following projects."""
    from ..maptools.transit_measure_tool import TransitMeasureTool
    project = QgsProject.instance()
    _open_project(iface, utm)
    hooks_before = _receivers(project, project.aboutToBeCleared)
    tool = TransitMeasureTool(iface)
    problems = []
    try:
        iface.canvas.setMapTool(tool)
        dialog = tool.dialog
        for x, y in ((500000, 6000000), (504000, 6002000), (508000, 6003000)):
            dialog.add_point(QgsPointXY(x, y))
        drawn = iface.plugin_items()[1]
        with _slot_errors() as errors:
            _open_project(iface, mercator)
        if errors or iface.canvas.mapTool() is tool or tool.dialog is not None \
                or iface.plugin_items()[0] or not sip.isdeleted(dialog) or drawn < 2:
            problems.append("open: errors=%s active=%s dialog=%s items=%s drawn=%d" % (
                errors, iface.canvas.mapTool() is tool, tool.dialog is not None,
                iface.plugin_items(), drawn))
        # Activate again in the Web Mercator project: a ~10 km meridian leg.
        iface.canvas.setMapTool(tool)
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        to_map = QgsCoordinateTransform(wgs84, iface.canvas.mapSettings().destinationCrs(), project)
        a_ll, b_ll = QgsPointXY(3.0, 54.0), QgsPointXY(3.0, 54.09)
        tool.dialog.add_point(to_map.transform(a_ll))
        tool.dialog.add_point(to_map.transform(b_ll))
        truth = QgsDistanceArea()
        truth.setSourceCrs(wgs84, project.transformContext())
        truth.setEllipsoid("WGS84")
        expected = truth.measureLine(a_ll, b_ll)
        measured = sum(tool.dialog.distances_m)
        if abs(measured - expected) > 1.0:
            problems.append("leg %.1f m, expected %.1f m" % (measured, expected))
        # The map CRS changing under an open dialog: legs follow it.
        tool.dialog.new_path()
        iface.canvas.setDestinationCrs(QgsCoordinateReferenceSystem(_UTM))
        to_map = QgsCoordinateTransform(wgs84, iface.canvas.mapSettings().destinationCrs(), project)
        tool.dialog.add_point(to_map.transform(a_ll))
        tool.dialog.add_point(to_map.transform(b_ll))
        if abs(sum(tool.dialog.distances_m) - expected) > 1.0:
            problems.append("after a map CRS change: leg %.1f m" % sum(tool.dialog.distances_m))
    finally:
        if iface.canvas.mapTool() is tool:
            iface.canvas.unsetMapTool(tool)
        tool.cleanup()
        tool.cleanup()
        _pump()
    if _receivers(project, project.aboutToBeCleared) != hooks_before:
        problems.append("cleanup() left the project hook connected")
    return _result("Transit Measure: disposed on Open, legs measured in the map CRS", not problems,
                   "; ".join(problems))


# -- Depth Profile dock --------------------------------------------------------------
def _generate_depth_profile(dock, layers):
    dock.use_drawn_chk.setChecked(False)
    dock.line_layer_combo.setCurrentIndex(dock.line_layer_combo.findData(layers["route"].id()))
    dock.source_type_combo.setCurrentText("Raster")
    for i in range(dock.raster_layer_list.count()):
        item = dock.raster_layer_list.item(i)
        item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) == layers["depth"].id()
                           else Qt.CheckState.Unchecked)
    dock.interval_spin.setValue(50)
    dock.generate_profile()
    end = time.monotonic() + 60
    while (dock.is_generating() or dock._live_tasks) and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)
    _pump()
    dock.update_map_marker(3.0)


def _draw_line(dock, points):
    dock.activate_temp_line_tool()
    dock.temp_line_tool._finished_cb(points)
    dock.temp_line_tool.finish()


def _depth_state(dock, iface):
    return {"stations": len(dock.profile.kp_values), "axes": len(dock.figure.get_axes()),
            "marker": dock.marker is not None, "drawn points": len(dock.temp_drawn_points),
            "use drawn": dock.use_drawn_chk.isChecked(), "rubber": dock.temp_line_rubber is not None,
            "canvas items": iface.plugin_items()[0]}


_DEPTH_CLEAN = {"stations": 0, "axes": 0, "marker": False, "drawn points": 0, "use drawn": False,
                "rubber": False, "canvas items": 0}


def test_depth_profile_dock(iface, utm, mercator):
    from ..depth_profile_dockwidget import DepthProfileDockWidget
    project = QgsProject.instance()
    layers = _open_project(iface, utm)
    hooks_before = _receivers(project, project.aboutToBeCleared)
    dock = DepthProfileDockWidget(iface)
    dock.show()
    _pump(150)
    problems = []
    try:
        _generate_depth_profile(dock, layers)
        _draw_line(dock, [QgsPointXY(501000, 6001000), QgsPointXY(506000, 6002500)])
        before = _depth_state(dock, iface)
        if before["stations"] == 0 or not before["marker"] or not before["rubber"]:
            problems.append("setup: %s" % before)
        with _slot_errors() as errors:
            _new_project(iface)
        after = _depth_state(dock, iface)
        if errors or after != _DEPTH_CLEAN or dock.line_layer_combo.count() != 0:
            problems.append("New: %s, line layers %d, errors %s" % (
                after, dock.line_layer_combo.count(), errors))
        layers = _open_project(iface, utm)
        if dock.line_layer_combo.findData(layers["route"].id()) < 0:
            problems.append("Open: route not listed")
        # Drawing in progress when another project opens: tool unset.
        dock.activate_temp_line_tool()
        drawing = dock.temp_line_tool
        _generate_depth_profile(dock, layers)
        with _slot_errors() as errors:
            layers = _open_project(iface, mercator)
        after = _depth_state(dock, iface)
        if errors or after != _DEPTH_CLEAN or iface.canvas.mapTool() is drawing \
                or dock.line_layer_combo.findData(layers["route"].id()) < 0:
            problems.append("Open: %s, drawing tool active %s, errors %s" % (
                after, iface.canvas.mapTool() is drawing, errors))
        # Closed and shown again: hooks back, and they still clear and refresh.
        _draw_line(dock, [QgsPointXY(1, 1), QgsPointXY(2, 2)])
        dock.close()
        if dock.temp_drawn_points or _receivers(project, project.aboutToBeCleared) != hooks_before:
            problems.append("close: drawn points %d kept or hooks left connected" % len(dock.temp_drawn_points))
        dock.show()
        _pump(150)
        _draw_line(dock, [QgsPointXY(1, 1), QgsPointXY(2, 2)])
        layers = _open_project(iface, utm)
        if _depth_state(dock, iface) != _DEPTH_CLEAN \
                or dock.line_layer_combo.findData(layers["route"].id()) < 0:
            problems.append("after close + show: %s" % _depth_state(dock, iface))
    finally:
        dock.shutdown()
        dock.deleteLater()
        _pump()
    if _receivers(project, project.aboutToBeCleared) != hooks_before:
        problems.append("shutdown() left the project hook connected")
    return _result("Depth Profile: New / Open drop plot, marker, drawn line; lists refresh; "
                   "hooks survive close + show", not problems, "; ".join(problems))


# -- KP Plot dock ------------------------------------------------------------------
def _plot(dock, layers):
    dock.line_layer_combo.setCurrentIndex(dock.line_layer_combo.findData(layers["route"].id()))
    dock.table_layer_combo.setCurrentIndex(dock.table_layer_combo.findData(layers["kp_table"].id()))
    dock.kp_field_combo.setCurrentText("kp")
    for i in range(dock.data_fields_list.count()):
        item = dock.data_fields_list.item(i)
        item.setSelected(item.text() == "val")
    dock.plot_data()
    dock.update_map_marker(3.0)


def test_kp_plot_dock(iface, utm, mercator):
    from ..kp_plotter_dockwidget import KpPlotterDockWidget
    project = QgsProject.instance()
    layers = _open_project(iface, utm)
    hooks_before = (_receivers(project, project.aboutToBeCleared), _receivers(iface, iface.projectRead),
                    _receivers(iface, iface.newProjectCreated))

    def hooks():
        return (_receivers(project, project.aboutToBeCleared), _receivers(iface, iface.projectRead),
                _receivers(iface, iface.newProjectCreated))

    dock = KpPlotterDockWidget(iface)
    dock.save_user_settings = lambda: None      # never persist test choices
    dock.show()
    problems = []
    try:
        _plot(dock, layers)
        if dock.marker is None or not dock.marker.isVisible() or dock.route is None:
            problems.append("setup: no marker/route")
        with _slot_errors() as errors:
            _new_project(iface)
        dock.update_map_marker(5.0)               # hovering the (cleared) plot
        if errors or iface.plugin_items()[0] or dock.marker is not None or dock.route is not None \
                or dock.line_layer_combo.count() or dock.table_layer_combo.count() \
                or dock.figure.get_axes():
            problems.append("New: errors=%s items=%s marker=%s combos=%d/%d axes=%d" % (
                errors, iface.plugin_items(), dock.marker is not None, dock.line_layer_combo.count(),
                dock.table_layer_combo.count(), len(dock.figure.get_axes())))
        layers = _open_project(iface, utm)
        if dock.line_layer_combo.findData(layers["route"].id()) < 0:
            problems.append("Open: route not listed")
        _plot(dock, layers)
        dock.close()                              # X: shutdown()
        if hooks() != hooks_before:
            problems.append("close left hooks connected: %s vs %s" % (hooks(), hooks_before))
        dock.show()
        layers = _open_project(iface, mercator)
        if dock.line_layer_combo.findData(layers["route"].id()) < 0 or iface.plugin_items()[0]:
            problems.append("after close + show, Open did not refresh the lists")
        _plot(dock, layers)
        with _slot_errors() as errors:
            layers = _open_project(iface, utm)
        if errors or iface.plugin_items()[0] or dock.route is not None \
                or dock.line_layer_combo.findData(layers["route"].id()) < 0:
            problems.append("after close + show, Open did not clear the plot: items=%s errors=%s" % (
                iface.plugin_items(), errors))
    finally:
        dock.shutdown()
        dock.deleteLater()
        _pump()
    if hooks() != hooks_before:
        problems.append("shutdown() left hooks connected")
    return _result("KP Plot: New / Open drop plot and marker, lists refresh, hooks survive close + show",
                   not problems, "; ".join(problems))


def run_all():
    results = []
    old_format = QSettings.defaultFormat()
    temp = tempfile.mkdtemp(prefix="sct_project_switch_")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
    iface = _Iface()
    try:
        utm, mercator = _write_projects(tempfile.mkdtemp(dir=temp))
        for test in (lambda: test_kp_tool_off_on_new_and_open(iface, utm, mercator),
                     lambda: test_kp_tool_layer_removed(iface, utm),
                     lambda: test_kp_tool_no_orphans(iface, utm),
                     lambda: test_kp_button_follows_map_tool(iface, utm),
                     lambda: test_transit_open_and_crs(iface, utm, mercator),
                     lambda: test_depth_profile_dock(iface, utm, mercator),
                     lambda: test_kp_plot_dock(iface, utm, mercator)):
            try:
                results.append(test())
            except Exception as exc:  # report, keep going
                traceback.print_exc()
                results.append(_result("project switch test", False, repr(exc)))
    finally:
        QgsProject.instance().clear()
        iface.close()
        _pump()
        QSettings.setDefaultFormat(old_format)
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
