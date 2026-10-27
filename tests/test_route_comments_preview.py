# -*- coding: utf-8 -*-
"""Unsaved position previews: geometry, live typing, CRS, zoom and disposal."""

from __future__ import annotations

import tempfile

from qgis.core import QgsCoordinateReferenceSystem, QgsProject, QgsRectangle
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, QSettings, Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QDialog

from ..route_comments.dialogs import EditPositionDialog, NewCommentDialog
from ..route_comments.preview import PositionPreview
from ..route_comments.route_ref import CaptureRoute, location_without_route
from .test_route_comments_qgis import START_KP, _Iface, _pt, _route_layer

REQUIRES_QGIS = True


def test_overlay_lifecycle():
    iface = _Iface()
    owner = QDialog()
    canvas = iface.canvas
    canvas.setExtent(QgsRectangle(2.9, 54.9, 3.2, 55.2))
    baseline = len(canvas.scene().items())
    preview = PositionPreview(canvas, owner)
    layer = _route_layer()
    capture = CaptureRoute.from_layer(layer, start_kp_km=START_KP, use_cartesian=False)
    point = capture.point_values(START_KP + 2)
    section = capture.range_values(START_KP + 1, START_KP + 5)
    try:
        preview.show(point["wkt"])
        assert preview.valid and len(preview.overlay.bands) == 2
        assert len(canvas.scene().items()) == baseline + 2
        preview.show(section["wkt"])
        assert preview.valid and len(preview.overlay.bands) == 6
        assert len(canvas.scene().items()) == baseline + 6
        for _ in range(5):
            preview.show(section["wkt"])
        assert len(canvas.scene().items()) == baseline + 6, "Redrawing leaked canvas items"

        # A visible preview follows map CRS changes rather than staying at old coordinates.
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:32631"))
        assert preview.valid and preview.wkt == section["wkt"]
        first = preview._map_geometry().vertexAt(0)
        drawn = preview.overlay.bands[0].asGeometry().vertexAt(0)
        assert abs(first.x() - drawn.x()) < 1e-6 and abs(first.y() - drawn.y()) < 1e-6
        preview.zoom_to()
        assert canvas.extent().contains(preview._map_geometry().boundingBox())
        preview.show(point["wkt"])
        width = canvas.extent().width()
        preview.zoom_to()
        assert canvas.extent().contains(preview._map_geometry().asPoint())
        assert abs(canvas.extent().width() - width) < 1e-6

        preview.show("")
        assert not preview.valid and len(canvas.scene().items()) == baseline
        preview.show(section["wkt"])
        owner.reject()
        assert not preview.valid and len(canvas.scene().items()) == baseline
        preview.show(point["wkt"])
        assert not preview.valid, "A finished editor resurrected its preview"
        preview.dispose()  # Idempotent disposal.

        another = QDialog()
        live = PositionPreview(canvas, another)
        live.show(point["wkt"])
        sip.delete(another)
        assert len(canvas.scene().items()) == baseline, "Destroying the editor left its preview behind"

        another = QDialog()
        live = PositionPreview(canvas, another)
        live.show(point["wkt"])
        QgsProject.instance().clear()
        live.show(point["wkt"])
        assert not live.valid and len(canvas.scene().items()) == baseline
        another.deleteLater()
    finally:
        preview.dispose()
        owner.deleteLater()
        iface.close()
        QCoreApplication.processEvents()


def test_dialog_previews():
    iface = _Iface()
    canvas = iface.canvas
    canvas.setExtent(QgsRectangle(2.9, 54.9, 3.2, 55.2))
    baseline = len(canvas.scene().items())
    layer = _route_layer()
    capture = CaptureRoute.from_layer(layer, start_kp_km=START_KP, use_cartesian=False)
    initial = [capture.point_values(START_KP + 1), capture.range_values(START_KP + 1, START_KP + 4),
               capture.location_from_wgs84(55.02, 3.02), location_without_route(55.02, 3.02)]
    try:
        for number, values in enumerate(initial):
            route = capture if number != 3 else None
            dialog = EditPositionDialog(None, values, route, canvas=canvas)
            try:
                assert dialog.map_preview.valid and dialog.map_preview.wkt == values["wkt"]
                assert dialog.preview_controls.zoom.isEnabled()
                if number < 2:
                    dialog.editors["kp_km"].setValue(START_KP + 2)
                else:
                    dialog.editors["lon"].setValue(3.05)
                assert dialog.map_preview.wkt == dialog.result_values()["wkt"]
                assert dialog.map_preview.wkt != values["wkt"]
                assert len(dialog.map_preview.overlay.bands) == (6 if number == 1 else 2)
                dialog.preview_controls.zoom.click()
                if number == 1:
                    assert canvas.extent().contains(dialog.map_preview._map_geometry().boundingBox())
                else:
                    assert canvas.extent().contains(_pt(dialog.map_preview.wkt))
                if number < 2:
                    dialog.editors["kp_km"].setValue(START_KP - 1)
                    assert not dialog.map_preview.valid and not dialog.preview_controls.zoom.isEnabled()
                dialog.reject()
                assert len(canvas.scene().items()) == baseline
            finally:
                dialog.map_preview.dispose()
                dialog.deleteLater()

        # Actual text entry previews before leaving the spin box or saving.
        dialog = EditPositionDialog(None, initial[0], capture, canvas=canvas)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        dialog.show()
        spin = dialog.editors["kp_km"]
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin.lineEdit(), "15.000")
        QTest.qWait(20)
        assert spin.value() == 15 and dialog.map_preview.wkt == capture.point_values(15)["wkt"]
        spin.lineEdit().clear()
        assert not dialog.map_preview.valid
        spin.lineEdit().setText("15.000 km")  # Restore an unchanged value after incomplete input.
        QTest.qWait(20)
        assert dialog.map_preview.valid
        dialog.accept()
        assert not dialog.map_preview.valid and len(canvas.scene().items()) == baseline
        dialog.deleteLater()

        for values in initial[:3]:
            dialog = NewCommentDialog(None, values, capture, ["Open"], ["Normal"], [], "Open", "Normal",
                                      "Test", canvas=canvas)
            try:
                assert dialog.map_preview.wkt == values["wkt"]
                if dialog.kp is not None:
                    dialog.kp.setValue(START_KP + 2)
                else:
                    dialog.lon.setValue(3.05)
                assert dialog.map_preview.wkt == dialog.location_values()["wkt"]
                dialog.form.text.setPlainText("New comment preview")
                dialog.accept()
                assert not dialog.map_preview.valid and len(canvas.scene().items()) == baseline
            finally:
                dialog.map_preview.dispose()
                dialog.deleteLater()
    finally:
        iface.close()
        QCoreApplication.processEvents()


def run_all():
    results = []
    old = QSettings.defaultFormat()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, folder)
        try:
            for test in (test_overlay_lifecycle, test_dialog_previews):
                try:
                    test()
                    results.append(True)
                    print("[PASS]", test.__name__)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    results.append(False)
        finally:
            QSettings.setDefaultFormat(old)
    return results
