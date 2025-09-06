from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import QgsWkbTypes, QgsPointXY


class TempLineMapTool(QgsMapTool):
    """Simple map tool to let user digitize a temporary polyline with dynamic preview.

    Usage:
      - Left click adds a vertex
      - Moving mouse shows a preview segment from last fixed vertex to cursor
      - Right click OR double left click finishes (requires >=2 points)
      - Esc cancels (doesn't call finish callback)
    """

    def __init__(self, canvas, iface, finished_callback, canceled_callback=None, color=QColor(255, 170, 0)):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface  # to push messages
        self._finished_cb = finished_callback
        self._canceled_cb = canceled_callback
        self._color = color

        self._rubber = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self._rubber.setColor(self._color)
        self._rubber.setWidth(2)
        self._rubber.setLineStyle(Qt.SolidLine)

        self._points = []  # committed vertices
        self._active = True
        self._has_preview = False  # indicates a provisional trailing vertex following mouse
        self.setCursor(Qt.CrossCursor)
        # Initial guidance message (single, longer duration)
        try:
            self.iface.messageBar().pushMessage(
                "Depth Profile",
                "Left-click to add points, right-click or double-click to finish. Then select a bathymetry layer and click Generate Profile.",
                level=0, duration=8
            )
        except Exception:
            pass

    # ---------------- Event handlers ---------------- #
    def keyPressEvent(self, event):  # noqa
        if event.key() == Qt.Key_Escape:
            self.cancel()

    def canvasPressEvent(self, event):  # noqa
        if not self._active:
            return
        if self._rubber is None:
            self._rubber = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            self._rubber.setColor(self._color)
            self._rubber.setWidth(2)
            self._rubber.setLineStyle(Qt.SolidLine)

        if event.button() == Qt.LeftButton:
            map_pt = self.toMapCoordinates(event.pos())
            if self._has_preview:
                # Commit the preview point and create a fresh preview vertex
                self._points.append(QgsPointXY(map_pt))
                try:
                    self._rubber.addPoint(map_pt, True)
                except Exception:
                    pass
            else:
                self._points.append(QgsPointXY(map_pt))
                try:
                    self._rubber.addPoint(map_pt, True)
                except Exception:
                    pass
            # No per-point feedback; only initial message
        elif event.button() == Qt.RightButton:
            if len(self._points) >= 2:
                self.finish()
            else:
                self.cancel()

    def canvasDoubleClickEvent(self, event):  # noqa
        if len(self._points) >= 2:
            self.finish()

    def canvasMoveEvent(self, event):  # noqa
        if not self._active or not self._points:
            return
        if self._rubber is None:
            return
        map_pt = self.toMapCoordinates(event.pos())
        if not self._has_preview:
            try:
                self._rubber.addPoint(map_pt, True)
                self._has_preview = True
            except Exception:
                return
        else:
            try:
                self._rubber.movePoint(map_pt)
            except Exception:
                pass

    # ---------------- Lifecycle helpers ---------------- #
    def finish(self):
        if not self._active:
            return
        if self._finished_cb and self._points:
            try:
                self._finished_cb(self._points)
            except Exception:
                pass
        try:
            self.iface.messageBar().pushMessage(
                "Depth Profile",
                f"Line finished with {len(self._points)} points",
                level=0, duration=4
            )
        except Exception:
            pass
        try:
            if self.canvas.mapTool() == self:
                self.canvas.unsetMapTool(self)
        except Exception:
            pass
        self.cleanup()

    def cancel(self):
        if not self._active:
            return
        if self._canceled_cb:
            try:
                self._canceled_cb()
            except Exception:
                pass
        try:
            self.iface.messageBar().pushMessage(
                "Depth Profile",
                "Drawing canceled (need at least 2 points to finish)",
                level=1, duration=4
            )
        except Exception:
            pass
        try:
            if self.canvas.mapTool() == self:
                self.canvas.unsetMapTool(self)
        except Exception:
            pass
        self.cleanup()

    def cleanup(self):
        if not self._active:
            return
        self._active = False
        try:
            if self._rubber:
                try:
                    self._rubber.reset(QgsWkbTypes.LineGeometry)
                except Exception:
                    pass
                try:
                    self._rubber.hide()
                except Exception:
                    pass
                try:
                    self._rubber.deleteLater()
                except Exception:
                    pass
        except Exception:
            pass
        self._rubber = None
        self._points = []
        self._has_preview = False

    def deactivate(self):  # noqa
        super().deactivate()
        self.cleanup()
