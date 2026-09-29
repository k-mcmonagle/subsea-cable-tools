# -*- coding: utf-8 -*-
"""QGIS checks for Transit Measure teardown.

The dialog draws with 3-5 rubber bands (points, legs, the live leg, the
waypoint highlight, the buffer preview) plus the tool's snap marker. They
belong to the map canvas scene, so deleting the dialog left them there;
plugin unload never cleaned the tool up at all. ``cleanup()`` must remove
every canvas item and delete the dialog, after deactivation too, and be
safe to call twice.
"""

from __future__ import annotations

from qgis.core import QgsPointXY
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QEvent
from qgis.PyQt.QtWidgets import QMainWindow

from ..maptools.transit_measure_tool import TransitMeasureTool


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


class _Iface:
    def __init__(self):
        from qgis.gui import QgsMapCanvas, QgsMessageBar
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.bar = QgsMessageBar()

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar


def _draw(tool):
    """Use every overlay the tool can create."""
    from qgis.gui import QgsVertexMarker
    dialog = tool.dialog
    for x, y in ((2.0, 55.0), (2.2, 55.1), (2.3, 55.3)):
        dialog.add_point(QgsPointXY(x, y))
    dialog.update_motion(QgsPointXY(2.5, 55.4))
    dialog.highlight_waypoint(1)
    dialog.buffer_enable_chk.setChecked(True)
    tool.vertex_marker = QgsVertexMarker(tool.canvas)
    return dialog


def _cleanup_case(deactivate_first):
    iface = _Iface()
    scene = iface.canvas.scene()
    before = len(scene.items())
    tool = TransitMeasureTool(iface)
    try:
        iface.canvas.setMapTool(tool)
        dialog = _draw(tool)
        drawn = len(scene.items())
        if deactivate_first:            # the shell unsets the tool, then cleanup()
            iface.canvas.unsetMapTool(tool)
        tool.cleanup()
        tool.cleanup()                  # second call is harmless
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        after = len(scene.items())
        ok = (drawn >= before + 5 and after == before and tool.dialog is None
              and sip.isdeleted(dialog))
        return ok, "scene items %d -> %d -> %d, dialog deleted=%s" % (
            before, drawn, after, sip.isdeleted(dialog))
    finally:
        if iface.canvas.mapTool() is tool:
            iface.canvas.unsetMapTool(tool)
        iface.canvas.close()
        iface.window.close()


def test_cleanup_removes_everything():
    ok, detail = _cleanup_case(False)
    return _result("cleanup() removes every canvas item and deletes the dialog", ok, detail)


def test_cleanup_after_deactivate():
    ok, detail = _cleanup_case(True)
    return _result("deactivate then cleanup() (plugin unload order)", ok, detail)


def run_all():
    return [test_cleanup_removes_everything(), test_cleanup_after_deactivate()]
