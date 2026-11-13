# -*- coding: utf-8 -*-
"""Adding comments by clicking the map, from the Route Comments dock.

The dock's *Add* button turns this map tool on, so comments can be added
without the KP Mouse Tool. It measures KPs on the same route model (the KP
Mouse Tool's reference line, start KP and KP convention):

* **click**: a comment on the route at the nearest KP (the snapped position
  is marked while the mouse moves);
* **Shift+click**: a comment exactly where you click, with its nearest KP
  and distance off the route kept apart (as *At Cursor Location*);
* **drag along the route**: a comment over a KP range, previewed while
  dragging (as *Over KP Range…*);
* **Esc** or a right-click stops; another map tool stops it too.

Without a route, a click places a comment at that map position (no KP).
The tool stays on after each comment, like QGIS's own digitising tools, so
a review can add several in a row. The dock opens the usual new-comment
form for each one; nothing is written by this module.
"""

from __future__ import annotations

from typing import Callable, Optional

from qgis.core import QgsPointXY, QgsProject
from qgis.gui import QgsMapTool, QgsVertexMarker
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from ..maptools.canvas_items import remove_canvas_item
from ..plugin_log import log_exception
from . import model
from .capture import PREVIEW_COLOR, RangePicker

DRAG_PX = 6


class CommentPlaceTool(QgsMapTool):
    """Click the map to place a comment (see the module notes).

    ``route_factory()`` returns the route as a ``CaptureRoute`` in the map
    CRS, or None when there is no route (it may raise ``ValueError`` when
    the route cannot be measured: the message is kept in :attr:`problem`).
    ``status(text)`` shows a hint (the status bar)."""

    placed = pyqtSignal(str, object)        # kind, data (as the KP Mouse Tool's comment hook)
    stopped = pyqtSignal()

    def __init__(self, canvas, route_factory: Callable, status: Optional[Callable[[str], None]] = None):
        super().__init__(canvas)
        self._factory = route_factory
        self._status = status or (lambda _text: None)
        self.capture = None
        self.problem = ""
        self._marker: Optional[QgsVertexMarker] = None
        self._picker: Optional[RangePicker] = None
        self._press = None                   # (screen position, KP at the press or None)
        self._connected = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    # -- route ------------------------------------------------------------------
    def rebuild(self, *_args) -> None:
        """Measure the route again (on activation, a map CRS change, layers removed)."""
        self._cancel_drag()
        self.capture, self.problem = None, ""
        try:
            self.capture = self._factory()
        except Exception as exc:  # noqa: BLE001 - the tool still places map positions
            log_exception("Route Comments: the route for placing comments could not be measured")
            self.problem = str(exc)
        self._hint()

    def _hit(self, map_point):
        if self.capture is None:
            return None
        try:
            hit = self.capture.route.kp_at_point(QgsPointXY(map_point))
        except Exception:  # noqa: BLE001 - e.g. the route layer was just removed
            return None
        return hit if hit is not None and hit.snapped_xy is not None else None

    def hint_text(self) -> str:
        if self.capture is None:
            why = f" ({self.problem})" if self.problem else ""
            return ("Add comments: click the map to place one where you click. There is no KP reference line"
                    f"{why}, so they have no KP. Esc or right-click stops.")
        return ("Add comments: click puts one on the route at the nearest KP, Shift+click exactly where you click, "
                "a drag along the route over a KP range. Esc or right-click stops.")

    def _hint(self, text: str = "") -> None:
        self._status(text or self.hint_text())

    # -- QgsMapTool ------------------------------------------------------------------
    def activate(self):
        super().activate()
        if not self._connected:
            self.canvas().destinationCrsChanged.connect(self.rebuild)
            QgsProject.instance().layersRemoved.connect(self.rebuild)
            self._connected = True
        self.rebuild()

    def deactivate(self):
        self._disconnect()
        self._clear_items()
        self._status("")
        super().deactivate()

    def _disconnect(self) -> None:
        if not self._connected:
            return
        for signal in (self.canvas().destinationCrsChanged, QgsProject.instance().layersRemoved):
            try:
                signal.disconnect(self.rebuild)
            except (TypeError, RuntimeError):
                pass
        self._connected = False

    def _marker_at(self, point) -> None:
        if point is None:
            if self._marker is not None:
                self._marker.hide()
            return
        if self._marker is None:
            self._marker = QgsVertexMarker(self.canvas())
            self._marker.setColor(QColor(PREVIEW_COLOR))
            self._marker.setFillColor(QColor(255, 255, 255, 200))
            self._marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
            self._marker.setIconSize(12)
            self._marker.setPenWidth(3)
        self._marker.setCenter(QgsPointXY(point))
        self._marker.show()

    def _clear_items(self) -> None:
        self._cancel_drag()
        if self._marker is not None:
            remove_canvas_item(self._marker)
            self._marker = None

    def _cancel_drag(self) -> None:
        self._press = None
        if self._picker is not None:
            self._picker.dispose()
            self._picker = None

    def canvasMoveEvent(self, event):  # noqa: N802 - Qt API
        point = self.toMapCoordinates(event.pos())
        hit = self._hit(point)
        if self._press is not None and self._press[1] is not None and self._picker is None and \
                (event.pos() - self._press[0]).manhattanLength() > DRAG_PX:
            self._picker = RangePicker(self.canvas(), self.capture.route, self._press[1])
        if self._picker is not None:
            if hit is not None:
                self._hint(self._picker.update(hit.kp_km).replace("click to finish", "release to finish"))
            self._marker_at(hit.snapped_xy if hit is not None else None)
            return
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self._marker_at(None if (hit is None or shift) else hit.snapped_xy)
        if hit is not None:
            where = (f"exactly here (nearest KP {hit.kp_km:.3f}, {hit.dcc_m:,.0f} m off the route)" if shift else
                     f"on the route at KP {hit.kp_km:.3f} ({hit.dcc_m:,.0f} m away)")
            self._hint(f"Click: a comment {where}. Shift+click: exactly where you click. Drag along the route: "
                       "a KP range. Esc stops.")

    def canvasPressEvent(self, event):  # noqa: N802 - Qt API
        if event.button() != Qt.MouseButton.LeftButton:
            return
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        hit = None if shift else self._hit(self.toMapCoordinates(event.pos()))
        self._press = (event.pos(), float(hit.kp_km) if hit is not None else None)

    def canvasReleaseEvent(self, event):  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.RightButton:
            self._cancel_drag()
            self.stopped.emit()
            return
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        point = QgsPointXY(self.toMapCoordinates(event.pos()))
        picker = self._picker
        self._press = None
        if picker is not None:
            kps = picker.kps()
            self._picker = None
            picker.dispose()
            if kps is None:
                self._hint("A KP range needs two different KPs: drag further along the route.")
                return
            self.placed.emit(model.REF_KP_RANGE, {"kps": kps})
            return
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        hit = self._hit(point)
        if self.capture is None or shift or hit is None:
            self.placed.emit(model.REF_LOCATION, {"point": point})
        else:
            self.placed.emit(model.REF_ROUTE_POINT, {"kp_km": float(hit.kp_km)})

    def keyPressEvent(self, event):  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Escape:
            if self._press is not None or self._picker is not None:
                self._cancel_drag()
                self._hint()
            else:
                self.stopped.emit()
            event.accept()
            return
        event.ignore()

    def dispose(self) -> None:
        """Release the canvas items and signals (the dock is closing)."""
        self._disconnect()
        self._clear_items()
