# -*- coding: utf-8 -*-
"""Reusable snapping point/polyline sketch interaction for planner geometry."""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY,
    QgsProject, QgsSnappingConfig,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker

from ..qgis_compat import (
    GEOMETRY_LINE, GEOMETRY_POINT, SNAPPING_MODE_PER_LAYER,
    SNAPPING_TYPE_SEGMENT, SNAPPING_TYPE_VERTEX, SNAPPING_UNIT_PIXELS,
    snapping_type_flags,
)


class PlannerSketchTool(QgsMapTool):
    """Transit-Measure-style sketching with snapping and draggable vertices."""

    def __init__(self, canvas, mode, completed, cancelled, changed=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.mode = mode
        self.completed_callback = completed
        self.cancelled_callback = cancelled
        self.changed_callback = changed
        self.points = []
        self.drag_index = None
        self._done = False
        self.paused = False
        self.point_band = QgsRubberBand(canvas, GEOMETRY_POINT)
        self.point_band.setColor(QColor("#ff9900"))
        self.point_band.setIconSize(9)
        self.line_band = QgsRubberBand(canvas, GEOMETRY_LINE)
        self.line_band.setColor(QColor("#ff9900"))
        self.line_band.setWidth(3)
        self.motion_band = QgsRubberBand(canvas, GEOMETRY_LINE)
        self.motion_band.setColor(QColor("#6699ff"))
        self.motion_band.setWidth(2)
        self.snap_marker = None
        self.snap_enabled = True
        self.snap_vertices = True
        self.snap_segments = True
        self._original_snapping_config = QgsSnappingConfig(
            self.canvas.snappingUtils().config())
        self.set_snapping(True, True, True)

    def activate(self):
        self.canvas.setCursor(Qt.CursorShape.CrossCursor)

    def canvasPressEvent(self, event):
        point = self._snap(event.originalPixelPoint())
        vertex = self._vertex_at(point)
        if event.button() == Qt.MouseButton.RightButton:
            if vertex is not None:
                self.points.pop(vertex)
                self._refresh()
            elif self.mode == "line" and self.points:
                self.set_paused(True)
            else:
                self.cancel()
            return
        if vertex is not None:
            self.drag_index = vertex
            self.canvas.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if self.paused:
            return
        self.points.append(QgsPointXY(point))
        self._refresh()
        if self.mode == "point":
            self.finish()

    def canvasMoveEvent(self, event):
        point = self._snap(event.originalPixelPoint())
        if self.drag_index is not None:
            self.points[self.drag_index] = QgsPointXY(point)
            self._refresh()
        elif self.mode == "line" and self.points and not self.paused:
            self.motion_band.setToGeometry(
                QgsGeometry.fromPolylineXY([self.points[-1], QgsPointXY(point)]), None)

    def canvasReleaseEvent(self, _event):
        if self.drag_index is not None:
            self.drag_index = None
            self.canvas.setCursor(Qt.CursorShape.CrossCursor)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancel()
        elif event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and self.points:
            self.undo()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.finish()
        elif event.key() == Qt.Key.Key_Space and self.mode == "line":
            self.set_paused(not self.paused)

    def undo(self):
        if self.points:
            self.points.pop()
            self._refresh()

    def clear(self):
        self.points = []
        self.set_paused(False)
        self._refresh()

    def add_map_point(self, point):
        """Add an exact map-CRS coordinate, bypassing cursor snapping."""
        self.points.append(QgsPointXY(point))
        self._refresh()
        if self.mode == "point":
            self.finish()

    def add_wgs84(self, latitude, longitude):
        source = QgsCoordinateReferenceSystem("EPSG:4326")
        target = self.canvas.mapSettings().destinationCrs()
        point = QgsPointXY(float(longitude), float(latitude))
        if target.isValid() and target != source:
            point = QgsCoordinateTransform(
                source, target, QgsProject.instance()).transform(point)
        self.add_map_point(point)

    def set_snapping(self, enabled, vertices=True, segments=True):
        self.snap_enabled = bool(enabled)
        self.snap_vertices = bool(vertices)
        self.snap_segments = bool(segments)
        config = QgsSnappingConfig(self._original_snapping_config)
        active = self.snap_enabled and (self.snap_vertices or self.snap_segments)
        config.setEnabled(active)
        if active:
            config.setMode(SNAPPING_MODE_PER_LAYER)
            config.clearIndividualLayerSettings()
            for layer in self.canvas.layers():
                try:
                    geometry_type = layer.geometryType()
                except (AttributeError, RuntimeError):
                    continue
                snap_types = []
                if geometry_type == GEOMETRY_POINT and self.snap_vertices:
                    snap_types.append(SNAPPING_TYPE_VERTEX)
                elif geometry_type == GEOMETRY_LINE:
                    if self.snap_vertices:
                        snap_types.append(SNAPPING_TYPE_VERTEX)
                    if self.snap_segments:
                        snap_types.append(SNAPPING_TYPE_SEGMENT)
                if not snap_types:
                    continue
                settings = QgsSnappingConfig.IndividualLayerSettings(
                    True, snapping_type_flags(*snap_types), 12.0, SNAPPING_UNIT_PIXELS)
                config.setIndividualLayerSettings(layer, settings)
            config.setTolerance(12.0)
            config.setUnits(SNAPPING_UNIT_PIXELS)
            if hasattr(config, "setIntersectionSnapping"):
                config.setIntersectionSnapping(False)
        self.canvas.snappingUtils().setConfig(config)
        if not active and self.snap_marker is not None:
            self.snap_marker.hide()

    def set_paused(self, paused):
        self.paused = bool(paused)
        self.motion_band.reset(GEOMETRY_LINE)
        self._notify_changed()

    def finish(self):
        minimum = 1 if self.mode == "point" else 2
        if self._done or len(self.points) < minimum:
            return
        self._done = True
        self.completed_callback([QgsPointXY(point) for point in self.points])

    def cancel(self):
        if self._done:
            return
        self._done = True
        self.cancelled_callback()

    def deactivate(self):
        self._cleanup()
        super().deactivate()

    def _refresh(self):
        self.point_band.reset(GEOMETRY_POINT)
        for point in self.points:
            self.point_band.addPoint(point, True)
        self.line_band.reset(GEOMETRY_LINE)
        if len(self.points) >= 2:
            self.line_band.setToGeometry(QgsGeometry.fromPolylineXY(self.points), None)
        self.motion_band.reset(GEOMETRY_LINE)
        self._notify_changed()

    def _notify_changed(self):
        if self.changed_callback is not None:
            self.changed_callback(len(self.points), self.paused)

    def _snap(self, pixel_point):
        if not self.snap_enabled or not (self.snap_vertices or self.snap_segments):
            if self.snap_marker is not None:
                self.snap_marker.hide()
            return self.toMapCoordinates(pixel_point)
        match = self.canvas.snappingUtils().snapToMap(pixel_point)
        if match.isValid():
            if self.snap_marker is None:
                self.snap_marker = QgsVertexMarker(self.canvas)
                self.snap_marker.setColor(QColor("#ff00ff"))
                self.snap_marker.setIconSize(12)
                self.snap_marker.setPenWidth(2)
                self.snap_marker.setIconType(getattr(
                    QgsVertexMarker, "ICON_BOX", getattr(QgsVertexMarker, "ICON_X", 1)))
            self.snap_marker.setCenter(match.point())
            self.snap_marker.show()
            return match.point()
        if self.snap_marker is not None:
            self.snap_marker.hide()
        return self.toMapCoordinates(pixel_point)

    def _vertex_at(self, point, tolerance_pixels=10):
        tolerance = tolerance_pixels * self.canvas.mapUnitsPerPixel()
        for index, existing in enumerate(self.points):
            if ((existing.x() - point.x()) ** 2 + (existing.y() - point.y()) ** 2) ** 0.5 <= tolerance:
                return index
        return None

    def _cleanup(self):
        self.canvas.snappingUtils().setConfig(self._original_snapping_config)
        scene = self.canvas.scene()
        for item in (self.point_band, self.line_band, self.motion_band, self.snap_marker):
            if item is not None:
                scene.removeItem(item)
        self.snap_marker = None


class SketchControlDialog(QDialog):
    def __init__(self, tool, route_options=None, parent=None):
        super().__init__(parent)
        self.tool = tool
        self.route_options = list(route_options or [])
        self._programmatic_close = False
        self.setWindowTitle("Plan route sketch" if tool.mode == "line" else "Place task point")
        self.setModal(False)
        layout = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        snap_row = QHBoxLayout()
        self.snap_check = QCheckBox("Snap")
        self.snap_check.setChecked(True)
        self.snap_check.setToolTip(
            "Snap within 12 screen pixels, including Planner task geometry and other map layers.")
        self.vertex_check = QCheckBox("Points / vertices")
        self.vertex_check.setChecked(True)
        self.segment_check = QCheckBox("Line segments")
        self.segment_check.setChecked(True)
        for checkbox in (self.snap_check, self.vertex_check, self.segment_check):
            snap_row.addWidget(checkbox)
            checkbox.toggled.connect(self._snapping_changed)
        snap_row.addStretch(1)
        layout.addLayout(snap_row)

        exact_form = QFormLayout()
        coordinate_row = QHBoxLayout()
        self.lat_lon_edit = QLineEdit()
        self.lat_lon_edit.setPlaceholderText("e.g. 57.1497, -2.0943")
        coordinate_button = QPushButton("Add")
        coordinate_button.setToolTip(
            "Add this exact WGS84 coordinate as a route vertex or point task location.")
        coordinate_button.clicked.connect(self._add_lat_lon)
        coordinate_row.addWidget(self.lat_lon_edit, 1)
        coordinate_row.addWidget(coordinate_button)
        exact_form.addRow("Lat, lon (WGS84):", coordinate_row)

        kp_row = QHBoxLayout()
        self.route_combo = QComboBox()
        self.kp_spin = QDoubleSpinBox()
        self.kp_spin.setDecimals(3)
        self.kp_spin.setSuffix(" km")
        self.kp_spin.setRange(0.0, 0.0)
        self.kp_button = QPushButton("Add KP")
        self.kp_button.clicked.connect(self._add_route_kp)
        if self.route_options:
            for option in self.route_options:
                self.route_combo.addItem(option["label"], option)
        else:
            self.route_combo.addItem("No existing line task", None)
            self.route_combo.setEnabled(False)
            self.kp_spin.setEnabled(False)
            self.kp_button.setEnabled(False)
        self.route_combo.currentIndexChanged.connect(self._route_changed)
        kp_row.addWidget(self.route_combo, 1)
        kp_row.addWidget(self.kp_spin)
        kp_row.addWidget(self.kp_button)
        exact_form.addRow("Route task / KP:", kp_row)
        layout.addLayout(exact_form)

        buttons = QHBoxLayout()
        self.undo_btn = QPushButton("Undo vertex")
        self.undo_btn.clicked.connect(tool.undo)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(tool.set_paused)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(tool.clear)
        self.use_btn = QPushButton("Use route" if tool.mode == "line" else "Use point")
        self.use_btn.clicked.connect(tool.finish)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(tool.cancel)
        action_buttons = ((self.undo_btn, self.pause_btn, self.clear_btn, self.use_btn, cancel_btn)
                          if tool.mode == "line" else (self.clear_btn, cancel_btn))
        for button in action_buttons:
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.pause_btn.setVisible(tool.mode == "line")
        self.undo_btn.setVisible(tool.mode == "line")
        self.use_btn.setVisible(tool.mode == "line")
        self._route_changed()
        self._snapping_changed()
        self.update_state(0, False)

    def _snapping_changed(self, _checked=None):
        enabled = self.snap_check.isChecked()
        self.vertex_check.setEnabled(enabled)
        self.segment_check.setEnabled(enabled)
        self.tool.set_snapping(
            enabled, self.vertex_check.isChecked(), self.segment_check.isChecked())

    def _add_lat_lon(self):
        try:
            latitude, longitude = _parse_lat_lon(self.lat_lon_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Coordinate", str(exc))
            return
        self.tool.add_wgs84(latitude, longitude)

    def _route_changed(self, _index=None):
        option = self.route_combo.currentData()
        if not option:
            return
        minimum = min(float(option["kp_start"]), float(option["kp_end"]))
        maximum = max(float(option["kp_start"]), float(option["kp_end"]))
        self.kp_spin.setRange(minimum, maximum)
        self.kp_spin.setValue(float(option["kp_start"]))

    def _add_route_kp(self):
        option = self.route_combo.currentData()
        if not option:
            return
        entered = self.kp_spin.value()
        direction = 1.0 if option["kp_end"] >= option["kp_start"] else -1.0
        local_kp = (entered - float(option["kp_start"])) * direction
        point = option["frame"].point_at_kp(local_kp, clamp=True)
        if point is None:
            QMessageBox.warning(self, "Route KP", "Could not calculate that route position.")
            return
        self.tool.add_map_point(point)

    def update_state(self, count, paused):
        self.pause_btn.blockSignals(True)
        self.pause_btn.setChecked(paused)
        self.pause_btn.setText("Continue" if paused else "Pause")
        self.pause_btn.blockSignals(False)
        self.undo_btn.setEnabled(count > 0)
        self.clear_btn.setEnabled(count > 0)
        self.use_btn.setEnabled(count >= (2 if self.tool.mode == "line" else 1))
        if self.tool.mode == "point":
            self.status.setText(
                "Click the map, enter a latitude/longitude, or choose an existing route KP.")
            return
        if paused:
            self.status.setText(
                "%d vertices — paused. Drag or right-click existing vertices to adjust, "
                "then Continue or Use route." % count)
        else:
            self.status.setText(
                "%d vertices — click to add, drag to move, right-click a vertex to remove; "
                "right-click elsewhere pauses." % count)

    def close_programmatically(self):
        self._programmatic_close = True
        self.close()

    def closeEvent(self, event):
        if not self._programmatic_close:
            self.tool.cancel()
        super().closeEvent(event)


class SketchSession:
    def __init__(self, canvas, parent=None, fallback_tool=None):
        self.canvas = canvas
        self.parent = parent
        self.fallback_tool = fallback_tool
        self.tool = None
        self.previous_tool = None
        self.control = None
        self._closing = False

    def start(self, mode, completed, cancelled=None, route_options=None):
        self.cancel(False)
        self.previous_tool = self.canvas.mapTool()

        def done(points):
            if self._closing:
                return
            self._closing = True
            try:
                self._restore()
                completed(points)
            finally:
                self._closing = False

        def abandoned():
            if self._closing:
                return
            self._closing = True
            try:
                self._restore()
                if cancelled is not None:
                    cancelled()
            finally:
                self._closing = False

        self.tool = PlannerSketchTool(self.canvas, mode, done, abandoned)
        self.control = SketchControlDialog(self.tool, route_options, self.parent)
        self.tool.changed_callback = self.control.update_state
        self.control.show()
        self.control.raise_()
        self.canvas.setMapTool(self.tool)

    def cancel(self, notify=False):
        if self.tool is None:
            return
        tool = self.tool
        if notify:
            tool.cancel()
        else:
            self._restore()

    def _restore(self):
        tool = self.tool
        previous = self.previous_tool
        control = self.control
        self.tool = None
        self.previous_tool = None
        self.control = None
        if control is not None:
            control.close_programmatically()
        if tool is not None and self.canvas.mapTool() is tool:
            if previous is not None and _safe_to_restore(previous):
                self.canvas.setMapTool(previous)
            elif self.fallback_tool is not None:
                self.fallback_tool()
                if self.canvas.mapTool() is tool:
                    self.canvas.unsetMapTool(tool)
            else:
                self.canvas.unsetMapTool(tool)


def _safe_to_restore(tool):
    """Avoid reopening dialogs owned by map tools restored only as prior state."""
    tool_type = type(tool)
    return not (
        tool_type.__name__ == "TransitMeasureTool" or
        tool_type.__module__.endswith(".transit_measure_tool")
    )


def _parse_lat_lon(text):
    parts = str(text or "").replace(";", ",").split(",")
    if len(parts) != 2:
        parts = str(text or "").split()
    if len(parts) != 2:
        raise ValueError("Enter latitude and longitude as two numbers, for example 57.1497, -2.0943.")
    try:
        latitude, longitude = (float(part.strip()) for part in parts)
    except (TypeError, ValueError):
        raise ValueError("Latitude and longitude must be valid numbers.")
    if not -90.0 <= latitude <= 90.0:
        raise ValueError("Latitude must be between -90 and 90 degrees.")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError("Longitude must be between -180 and 180 degrees.")
    return latitude, longitude
