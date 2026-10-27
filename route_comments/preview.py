# -*- coding: utf-8 -*-
"""Temporary position previews. These never edit a layer or the register."""

from __future__ import annotations

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ..kp_geo_utils import geometry_is_finite
from ..qgis_compat import GEOMETRY_POINT
from . import model
from .map_layers import Highlight


class PositionPreview(QObject):
    """A distinct overlay owned by one editor, removed when that editor closes."""

    changed = pyqtSignal(bool)

    def __init__(self, canvas, parent):
        super().__init__(parent)
        self.canvas = canvas
        self.overlay = Highlight(canvas, color=QColor("#008bd2"), dashed=True)
        self.wkt = ""
        self.valid = False
        self._closed = False
        self._connections = []
        for signal, slot in ((canvas.destinationCrsChanged, self._redraw),
                             (QgsProject.instance().cleared, self.dispose),
                             (QgsProject.instance().readProject, self.dispose),
                             (parent.destroyed, self.dispose)):
            signal.connect(slot)
            self._connections.append((signal, slot))
        if hasattr(parent, "finished"):
            parent.finished.connect(self.dispose)
            self._connections.append((parent.finished, self.dispose))

    def _map_geometry(self):
        geom = QgsGeometry.fromWkt(self.wkt)
        if geom.isNull() or geom.isEmpty():
            raise ValueError("No position to preview")
        crs = self.canvas.mapSettings().destinationCrs()
        geom.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs,
                                              QgsProject.instance()))
        if not geometry_is_finite(geom):
            raise ValueError("The preview lies outside the map CRS")
        return geom

    def show(self, wkt):
        if self._closed:
            return
        self.wkt = str(wkt or "")
        self._redraw()

    def _redraw(self, *_args):
        if self._closed:
            return
        self.overlay.clear()
        self.valid = False
        if self.wkt:
            try:
                self._map_geometry()
                self.overlay.show([self.wkt])
                self.valid = True
            except Exception:  # noqa: BLE001 - a display/CRS failure must not affect the saved comment
                self.overlay.clear()
        self.changed.emit(self.valid)

    def clear(self):
        self.wkt = ""
        self.valid = False
        self.overlay.clear()
        self.changed.emit(False)

    def zoom_to(self):
        if self._closed or not self.valid:
            return
        try:
            geom = self._map_geometry()
            rect = geom.boundingBox()
            if geom.type() == GEOMETRY_POINT:
                # Keep the user's scale when previewing a point.
                self.canvas.setCenter(geom.asPoint())
            else:
                padding = max(rect.width(), rect.height(), self.canvas.mapUnitsPerPixel() * 50) * 0.15
                rect = rect.buffered(padding)
                self.canvas.setExtent(rect)
            self.canvas.refresh()
        except Exception:  # noqa: BLE001 - outside the map CRS, or canvas shutting down
            self.clear()

    def dispose(self, *_args):
        if self._closed:
            return
        self._closed = True
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections = []
        self.clear()


class PreviewControls(QWidget):
    """A short legend and an explicit zoom, without changing the map while typing."""

    def __init__(self, preview, parent):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("Blue overlay: unsaved position", self)
        label.setWordWrap(True)
        layout.addWidget(label, 1)
        self.zoom = QPushButton("Zoom to preview", self)
        self.zoom.setEnabled(False)
        self.zoom.clicked.connect(preview.zoom_to)
        preview.changed.connect(self.zoom.setEnabled)
        layout.addWidget(self.zoom)


class TableKpPreview(QObject):
    """Debounce typing and reuse the saved route model for one table editor."""

    def __init__(self, canvas, editor, comment, capture):
        super().__init__(editor)
        self.comment = dict(comment)
        self.capture = capture
        self.editor = editor
        self.overlay = PositionPreview(canvas, editor)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.update)
        self._closed = False
        editor.textChanged.connect(self.schedule)
        editor.destroyed.connect(self.dispose)

    def schedule(self, *_args):
        if not self._closed:
            self.timer.start()

    def update(self):
        if self._closed:
            return
        try:
            kp, end = model.parse_kp_edit(self.comment, self.editor.text())
            if (kp, end) == (self.comment.get("kp_km"), self.comment.get("kp_end_km")):
                values = self.comment
            else:
                values = self.capture.values_at_kps(self.comment["ref_kind"], kp, end)
            self.show_values(values)
        except Exception:  # noqa: BLE001 - invalid/incomplete typing only hides the preview
            self.overlay.clear()

    def show_values(self, values):
        self.timer.stop()
        if not self._closed:
            self.overlay.show(values.get("wkt"))

    def dispose(self, *_args):
        if not self._closed:
            self._closed = True
            self.timer.stop()
            self.overlay.dispose()
