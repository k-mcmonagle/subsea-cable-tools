"""Transit Measure Tool

Interactive map tool to measure cumulative geodesic (ellipsoidal) distance
and derive transit duration from a user supplied speed. Uses only QGIS
core (QgsDistanceArea) – no geographiclib dependency.

Left click: add vertex
Right click / Esc: finish current path

The dialog displays a per‑segment table (heading to / from, distance, duration)
plus totals. User can change distance & speed units on the fly and save the
measurement as a memory layer with attributes including distance, duration & s
"""
from __future__ import annotations
from __future__ import annotations

import csv
import math
from typing import List, Optional

from qgis.PyQt.QtCore import Qt, QCoreApplication, QVariant
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QDoubleSpinBox, QLineEdit, QTableWidget, QTableWidgetItem, QMessageBox,
    QFileDialog, QTabWidget, QWidget
)
from qgis.PyQt.QtGui import QColor

from qgis.core import (
    QgsProject, QgsPointXY, QgsDistanceArea, QgsWkbTypes,
    QgsGeometry, QgsVectorLayer, QgsFeature, QgsFields,
    QgsField, QgsUnitTypes, Qgis
)
from qgis.gui import QgsMapTool, QgsVertexMarker, QgsRubberBand


def tr(msg: str) -> str:
    return QCoreApplication.translate("TransitMeasureTool", msg)


DISTANCE_UNITS = [
    ("km", 1000.0),             # meters per displayed unit
    ("m", 1.0),
    ("nm", 1852.0),             # 1 nautical mile = 1852 meters
    ("mi", 1609.344),           # 1 mile = 1609.344 meters
]

SPEED_UNITS = [
    ("kn", 0.514444),    # knots to m/s
    ("km/h", 1/3.6),
    ("m/s", 1.0),
    ("mph", 0.44704),
    ("ft/s", 0.3048),
]

TIME_UNITS = [
    ("hh:mm:ss", "hms"),
    ("days", "d"),
]


class TransitMeasureTool(QgsMapTool):
    def __init__(self, iface):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.dialog: Optional[TransitMeasureDialog] = None
        self.vertex_marker: Optional[QgsVertexMarker] = None
        self.snap_color = QColor(Qt.magenta)
        
        # Editing mode properties
        self.selected_waypoint_idx = None
        self.is_dragging = False
        self.edit_cursor = Qt.OpenHandCursor
        self.drag_cursor = Qt.ClosedHandCursor

    def activate(self):  # noqa: D401
        self.canvas.setCursor(Qt.CrossCursor)
        if self.dialog is None:
            self.dialog = TransitMeasureDialog(self.iface, self)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        self.selected_waypoint_idx = None
        self.is_dragging = False
        # Reset continue button state
        if hasattr(self.dialog, 'continue_btn'):
            self.dialog.continue_btn.setEnabled(False)
        # Reset active status for new drawing session
        self.dialog.active = True

    def deactivate(self):
        self.remove_vertex_marker()
        self.selected_waypoint_idx = None
        self.is_dragging = False
        if self.dialog:
            self.dialog.clear_waypoint_highlight()
            self.dialog.close()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Escape,):
            if self.dialog and self.dialog.is_active():
                self.dialog.finish_path()

    def canvasPressEvent(self, event):
        if not self.dialog:
            return
        pt = self._snappoint(event.originalPixelPoint())
        
        # Check if clicking on existing waypoint
        if self.dialog.points:
            waypoint_idx = self._get_waypoint_at_position(pt)
            if waypoint_idx is not None:
                if event.button() == Qt.RightButton:
                    # Right-click on waypoint: delete it
                    self.dialog.delete_waypoint(waypoint_idx)
                    return
                else:
                    # Left-click on waypoint: start editing
                    self.selected_waypoint_idx = waypoint_idx
                    self.is_dragging = True
                    self.canvas.setCursor(self.drag_cursor)
                    self.dialog.highlight_waypoint(waypoint_idx)
                    return
        
        if event.button() == Qt.RightButton:
            if self.dialog.is_active():
                self.dialog.finish_path()
            return
        
        # Add new point if not clicking on existing waypoint and actively drawing
        if self.dialog.is_active():
            self.dialog.add_point(pt)

    def canvasReleaseEvent(self, event):
        if self.is_dragging and self.selected_waypoint_idx is not None:
            self.is_dragging = False
            self.canvas.setCursor(self.edit_cursor)
            # The position update is handled in canvasMoveEvent, so no need to do it again here
        elif self.selected_waypoint_idx is not None:
            # Clicked on waypoint but didn't drag - just select it
            pass

    def canvasMoveEvent(self, event):
        if not self.dialog:
            return
        
        # Check if hovering over a waypoint
        if self.dialog.points and not self.is_dragging:
            pt = self._snappoint(event.originalPixelPoint())
            hover_idx = self._get_waypoint_at_position(pt)
            if hover_idx is not None:
                if self.selected_waypoint_idx != hover_idx:
                    self.selected_waypoint_idx = hover_idx
                    self.canvas.setCursor(self.edit_cursor)
                    self.dialog.highlight_waypoint(hover_idx)
                return
            else:
                if self.selected_waypoint_idx is not None:
                    self.selected_waypoint_idx = None
                    self.canvas.setCursor(Qt.CrossCursor)
                    self.dialog.clear_waypoint_highlight()
        
        # Handle dragging
        if self.is_dragging and self.selected_waypoint_idx is not None:
            pt = self._snappoint(event.originalPixelPoint())
            self.dialog.update_waypoint_position(self.selected_waypoint_idx, pt)
        
        # Update motion line if actively drawing
        if not self.is_dragging and self.dialog.is_active() and self.dialog.points and not self.dialog.finished:
            pt = self._snappoint(event.originalPixelPoint())
            self.dialog.update_motion(pt)

    def _snappoint(self, qpoint):
        match = self.canvas.snappingUtils().snapToMap(qpoint)
        if match.isValid():
            if self.vertex_marker is None:
                vm = QgsVertexMarker(self.canvas)
                vm.setIconSize(12)
                vm.setPenWidth(2)
                vm.setColor(self.snap_color)
                vm.setIconType(QgsVertexMarker.ICON_BOX)
                self.vertex_marker = vm
            self.vertex_marker.setCenter(match.point())
            return match.point()
        else:
            self.remove_vertex_marker()
            return self.toMapCoordinates(qpoint)

    def remove_vertex_marker(self):
        if self.vertex_marker is not None:
            self.canvas.scene().removeItem(self.vertex_marker)
            self.vertex_marker = None

    def _get_waypoint_at_position(self, pt: QgsPointXY, tolerance_pixels: int = 10) -> Optional[int]:
        """Get the index of the waypoint at the given position within tolerance."""
        if not self.dialog or not self.dialog.points:
            return None
        
        # Convert tolerance from pixels to map units
        tolerance_map_units = tolerance_pixels * self.canvas.mapUnitsPerPixel()
        
        for idx, waypoint in enumerate(self.dialog.points):
            # Calculate distance between points
            dx = waypoint.x() - pt.x()
            dy = waypoint.y() - pt.y()
            distance = math.sqrt(dx*dx + dy*dy)
            if distance <= tolerance_map_units:
                return idx
        return None


class TransitMeasureDialog(QDialog):
    def __init__(self, iface, tool: TransitMeasureTool):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.tool = tool

        self.setWindowTitle(tr("Transit Measure"))
        self.resize(560, 420)

        self.points: List[QgsPointXY] = []
        self.distances_m: List[float] = []
        self.headings_fwd: List[float] = []
        self.headings_rev: List[float] = []
        self.motion_distance_m = 0.0
        self.motion_heading_fwd = None
        self.motion_heading_rev = None
        self.active = True
        self.finished = False
        
        # Editing properties
        self.highlighted_waypoint_idx = None
        self.original_point_color = QColor(255, 170, 0)
        self.highlight_color = QColor(255, 0, 0)

        self.da = QgsDistanceArea()
        proj = QgsProject.instance()
        self.da.setSourceCrs(proj.crs(), proj.transformContext())
        ellipsoid_id = proj.ellipsoid() or 'WGS84'
        self.da.setEllipsoid(ellipsoid_id)
        if hasattr(self.da, "setEllipsoidalMode"):
            self.da.setEllipsoidalMode(True)

        self.point_rb = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        self.point_rb.setColor(QColor(255, 170, 0))
        self.point_rb.setIconSize(8)
        self.line_rb = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.line_rb.setColor(QColor(255, 170, 0))
        self.line_rb.setWidth(2)
        self.temp_rb = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.temp_rb.setColor(QColor(255, 170, 0))
        self.temp_rb.setWidth(2)

        self._build_ui()
        self._refresh_unit_labels()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel(tr("Distance Units:")))
        self.dist_unit_combo = QComboBox()
        for u, _ in DISTANCE_UNITS:
            self.dist_unit_combo.addItem(u)
        self.dist_unit_combo.currentIndexChanged.connect(self._repopulate_distances)
        top_row.addWidget(self.dist_unit_combo)
        top_row.addWidget(QLabel(tr("Speed:")))
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setDecimals(2)
        self.speed_spin.setMinimum(0.0)
        self.speed_spin.setMaximum(1000.0)
        self.speed_spin.setValue(10.0)
        self.speed_spin.valueChanged.connect(self._recompute_durations)
        top_row.addWidget(self.speed_spin)
        self.speed_unit_combo = QComboBox()
        for u, _ in SPEED_UNITS:
            self.speed_unit_combo.addItem(u)
        self.speed_unit_combo.currentIndexChanged.connect(self._recompute_durations)
        top_row.addWidget(self.speed_unit_combo)
        top_row.addWidget(QLabel(tr("Time Units:")))
        self.time_unit_combo = QComboBox()
        for u, _ in TIME_UNITS:
            self.time_unit_combo.addItem(u)
        self.time_unit_combo.currentIndexChanged.connect(self._recompute_durations)
        top_row.addWidget(self.time_unit_combo)
        layout.addLayout(top_row)
        
        # Status label for edit mode
        self.status_label = QLabel(tr("Click on map to add waypoints. Hover over waypoints to edit (drag to move, right-click to delete). Right-click elsewhere to finish."))
        self.status_label.setStyleSheet("font-style: italic; color: #666;")
        layout.addWidget(self.status_label)
        
        self.tab_widget = QTabWidget()
        # Segments Tab
        self.segments_tab = QWidget()
        segments_layout = QVBoxLayout(self.segments_tab)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            tr("Bearing To"), tr("Bearing From"), tr("Distance"), tr("Duration"), tr("Cum Time")
        ])
        self.table.setSortingEnabled(False)
        segments_layout.addWidget(self.table)
        self.tab_widget.addTab(self.segments_tab, tr("Segments"))
        # Waypoints Tab
        self.waypoints_tab = QWidget()
        waypoints_layout = QVBoxLayout(self.waypoints_tab)
        self.waypoints_table = QTableWidget(0, 7)
        self.waypoints_table.setHorizontalHeaderLabels([
            tr("WP ID"), tr("Latitude"), tr("Longitude"), tr("Bearing To Next"), tr("Dist To Next"), tr("Cum Dist"), tr("Cum Time")
        ])
        self.waypoints_table.setSortingEnabled(False)
        waypoints_layout.addWidget(self.waypoints_table)
        self.tab_widget.addTab(self.waypoints_tab, tr("Waypoints"))
        layout.addWidget(self.tab_widget)
        totals_row = QHBoxLayout()
        totals_row.addWidget(QLabel(tr("Total Distance:")))
        self.total_dist_edit = QLineEdit(); self.total_dist_edit.setReadOnly(True)
        totals_row.addWidget(self.total_dist_edit)
        totals_row.addWidget(QLabel(tr("Total Time:")))
        self.total_time_edit = QLineEdit(); self.total_time_edit.setReadOnly(True)
        totals_row.addWidget(self.total_time_edit)
        layout.addLayout(totals_row)
        btn_row = QHBoxLayout()
        self.finish_btn = QPushButton(tr("Finish"))
        self.finish_btn.clicked.connect(self.finish_path)
        btn_row.addWidget(self.finish_btn)
        self.continue_btn = QPushButton(tr("Continue Drawing"))
        self.continue_btn.clicked.connect(self.continue_drawing)
        self.continue_btn.setEnabled(False)
        btn_row.addWidget(self.continue_btn)
        self.new_btn = QPushButton(tr("New"))
        self.new_btn.clicked.connect(self.new_path)
        btn_row.addWidget(self.new_btn)
        self.create_layer_btn = QPushButton(tr("Create Layer"))
        self.create_layer_btn.setEnabled(False)
        self.create_layer_btn.clicked.connect(self.create_layers)
        btn_row.addWidget(self.create_layer_btn)
        self.export_csv_btn = QPushButton(tr("Export CSV"))
        self.export_csv_btn.setEnabled(False)
        self.export_csv_btn.clicked.connect(self.export_csv)
        btn_row.addWidget(self.export_csv_btn)
        self.close_btn = QPushButton(tr("Close"))
        self.close_btn.clicked.connect(self.close)
        btn_row.addWidget(self.close_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def is_active(self):
        return self.active

    def add_point(self, pt: QgsPointXY):
        if self.points and pt == self.points[-1]:
            return
        self.points.append(pt)
        self.point_rb.addPoint(pt, True)
        if len(self.points) == 1:
            self._append_waypoint_row(1, pt, None, None, 0)
            self.export_csv_btn.setEnabled(True)  # Enable export for waypoints
            self._update_totals()
            return
        p1 = self.points[-2]
        p2 = self.points[-1]
        dist_m, h1, h2 = self._calc_segment(p1, p2)
        self.distances_m.append(dist_m)
        self.headings_fwd.append(h1)
        self.headings_rev.append(h2)
        self._append_row(dist_m, h1, h2)
        self._add_line_segment(p1, p2, dist_m)
        self.motion_distance_m = 0.0
        self.motion_heading_fwd = None
        self.motion_heading_rev = None
        self.create_layer_btn.setEnabled(True)
        self.export_csv_btn.setEnabled(True)
        self._update_totals()
        # Append waypoint for the last point
        last_idx = len(self.points) - 1
        bearing_next = h1 if last_idx > 0 else None
        dist_next = dist_m if last_idx > 0 else None
        cum_m = sum(self.distances_m[:last_idx]) if last_idx > 0 else 0
        self._append_waypoint_row(last_idx + 1, self.points[-1], dist_next, bearing_next, cum_m)

    def update_motion(self, pt: QgsPointXY):
        if not self.points:
            return
        p1 = self.points[-1]
        dist_m, h1, h2 = self._calc_segment(p1, pt)
        self.motion_distance_m = dist_m
        self.motion_heading_fwd = h1
        self.motion_heading_rev = h2
        self._update_motion_row(dist_m, h1, h2)
        self._update_temp_line(p1, pt, dist_m)
        self._update_totals()

    def highlight_waypoint(self, idx: int):
        """Highlight the waypoint at the given index."""
        if self.highlighted_waypoint_idx is not None:
            self.clear_waypoint_highlight()
        
        self.highlighted_waypoint_idx = idx
        # Create a separate rubber band for the highlighted point
        if not hasattr(self, 'highlight_rb'):
            self.highlight_rb = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
            self.highlight_rb.setColor(self.highlight_color)
            self.highlight_rb.setIconSize(10)
        
        if idx < len(self.points):
            self.highlight_rb.reset(QgsWkbTypes.PointGeometry)
            self.highlight_rb.addPoint(self.points[idx], True)
            self.status_label.setText(tr("Editing waypoint {} - drag to move, click elsewhere to finish.").format(idx + 1))
            self.status_label.setStyleSheet("font-weight: bold; color: #0066cc;")

    def clear_waypoint_highlight(self):
        """Clear the waypoint highlight."""
        if hasattr(self, 'highlight_rb'):
            self.highlight_rb.reset(QgsWkbTypes.PointGeometry)
        self.highlighted_waypoint_idx = None
        if self.points:
            self.status_label.setText(tr("Hover over waypoints to edit or click to add more points."))
        else:
            self.status_label.setText(tr("Click on map to add waypoints. Right-click to finish current segment."))
        self.status_label.setStyleSheet("font-style: italic; color: #666;")

    def update_waypoint_position(self, idx: int, new_pt: QgsPointXY):
        """Update the position of a waypoint and recalculate all affected segments."""
        if idx >= len(self.points):
            return
        
        # Prevent moving to the same position
        if self.points[idx] == new_pt:
            return
        
        old_pt = self.points[idx]
        self.points[idx] = new_pt
        
        # Update the point rubber band
        self.point_rb.reset(QgsWkbTypes.PointGeometry)
        for pt in self.points:
            self.point_rb.addPoint(pt, True)
        
        # Update highlight if this waypoint is highlighted
        if self.highlighted_waypoint_idx == idx:
            self.highlight_waypoint(idx)
        
        # Recalculate segments
        self._recalculate_segments()
        
        # Update tables
        self.table.setRowCount(0)
        for i, dist_m in enumerate(self.distances_m):
            h1 = self.headings_fwd[i]
            h2 = self.headings_rev[i]
            self._append_row(dist_m, h1, h2)
        
        self._repopulate_waypoints()
        self._update_totals()
        
        # Update line rubber bands
        self._update_line_rubber_bands()

    def delete_waypoint(self, idx: int):
        """Delete a waypoint and recalculate segments."""
        if idx >= len(self.points) or len(self.points) <= 1:
            return
        
        # Remove the point
        del self.points[idx]
        
        # Recalculate segments
        self._recalculate_segments()
        
        # Update rubber bands
        self.point_rb.reset(QgsWkbTypes.PointGeometry)
        for pt in self.points:
            self.point_rb.addPoint(pt, True)
        
        self._update_line_rubber_bands()
        
        # Update tables
        self.table.setRowCount(0)
        for i, dist_m in enumerate(self.distances_m):
            h1 = self.headings_fwd[i]
            h2 = self.headings_rev[i]
            self._append_row(dist_m, h1, h2)
        
        self._repopulate_waypoints()
        self._update_totals()
        
        # Clear any highlight
        self.clear_waypoint_highlight()

    def _recalculate_segments(self):
        """Recalculate all distances and headings based on current points."""
        if len(self.points) < 2:
            self.distances_m = []
            self.headings_fwd = []
            self.headings_rev = []
            return
        
        self.distances_m = []
        self.headings_fwd = []
        self.headings_rev = []
        
        for i in range(len(self.points) - 1):
            p1 = self.points[i]
            p2 = self.points[i + 1]
            dist_m, h1, h2 = self._calc_segment(p1, p2)
            self.distances_m.append(dist_m)
            self.headings_fwd.append(h1)
            self.headings_rev.append(h2)

    def _repopulate_waypoints(self):
        """Repopulate the waypoints table with current data."""
        self.waypoints_table.setRowCount(0)
        cum_m = 0.0
        for idx, pt in enumerate(self.points):
            dist_next = self.distances_m[idx] if idx < len(self.distances_m) else None
            bearing_next = self.headings_fwd[idx] if idx < len(self.headings_fwd) else None
            self._append_waypoint_row(idx + 1, pt, dist_next, bearing_next, cum_m)
            if idx < len(self.distances_m):
                cum_m += self.distances_m[idx]

    def _update_line_rubber_bands(self):
        """Update the line rubber bands to reflect current segments."""
        self.line_rb.reset(QgsWkbTypes.LineGeometry)
        for i in range(len(self.points) - 1):
            p1 = self.points[i]
            p2 = self.points[i + 1]
            dist_m = self.distances_m[i] if i < len(self.distances_m) else 0
            pts = self._densify(p1, p2, dist_m)
            self.line_rb.addGeometry(QgsGeometry.fromPolylineXY(pts), None)

    def finish_path(self):
        if not self.points:
            return
        self.temp_rb.reset(QgsWkbTypes.LineGeometry)
        self.motion_distance_m = 0.0
        self.motion_heading_fwd = None
        self.motion_heading_rev = None
        self._truncate_motion_row()
        self.finished = True
        self.clear_waypoint_highlight()
        self.status_label.setText(tr("Route finished. Hover over waypoints to edit (drag to move, right-click to delete) or click 'Continue Drawing' to add more points."))
        self.status_label.setStyleSheet("font-style: italic; color: #666;")
        self.continue_btn.setEnabled(True)
        self._update_totals()

    def continue_drawing(self):
        """Resume drawing mode to add more waypoints."""
        self.active = True
        self.finished = False
        self.continue_btn.setEnabled(False)
        self.status_label.setText(tr("Drawing mode resumed. Click on map to add waypoints. Hover over existing waypoints to edit (drag to move, right-click to delete). Right-click elsewhere to finish."))
        self.status_label.setStyleSheet("font-style: italic; color: #666;")
        # Reset motion state
        self.motion_distance_m = 0.0
        self.motion_heading_fwd = None
        self.motion_heading_rev = None

    def new_path(self):
        self.points = []
        self.distances_m = []
        self.headings_fwd = []
        self.headings_rev = []
        self.motion_distance_m = 0.0
        self.motion_heading_fwd = None
        self.motion_heading_rev = None
        self.active = True
        self.finished = False
        self.table.setRowCount(0)
        self.waypoints_table.setRowCount(0)
        self.point_rb.reset(QgsWkbTypes.PointGeometry)
        self.line_rb.reset(QgsWkbTypes.LineGeometry)
        self.temp_rb.reset(QgsWkbTypes.LineGeometry)
        self.clear_waypoint_highlight()
        self.create_layer_btn.setEnabled(False)
        self.export_csv_btn.setEnabled(False)
        self.status_label.setText(tr("Click on map to add waypoints. Hover over waypoints to edit."))
        self.status_label.setStyleSheet("font-style: italic; color: #666;")
        self._update_totals()

    def _calc_segment(self, p1: QgsPointXY, p2: QgsPointXY):
        dist = self.da.measureLine([p1, p2])
        b_fwd = math.degrees(self.da.bearing(p1, p2))
        b_rev = math.degrees(self.da.bearing(p2, p1))
        if b_fwd > 180:
            b_fwd -= 360
        if b_rev > 180:
            b_rev -= 360
        return dist, b_fwd, b_rev

    def _distance_factor(self):
        return DISTANCE_UNITS[self.dist_unit_combo.currentIndex()][1]

    def _speed_mps(self):
        val = self.speed_spin.value()
        factor = SPEED_UNITS[self.speed_unit_combo.currentIndex()][1]
        return val * factor

    def _format_distance(self, meters: float):
        if meters is None:
            return "-"
        factor = self._distance_factor()
        return f"{meters / factor:.3f}"

    def _format_duration(self, seconds: Optional[float], unit: str = "hms"):
        if seconds is None:
            return "-"
        if unit == "d":
            days = seconds / 86400.0
            return f"{days:.2f}d"
        else:
            if seconds < 1:
                return f"{seconds*1000:.0f} ms"
            h, rem = divmod(int(seconds), 3600)
            m, s = divmod(rem, 60)
            if h:
                return f"{h:d}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"

    def _segment_duration_s(self, meters: float) -> Optional[float]:
        v = self._speed_mps()
        if v <= 0:
            return None
        return meters / v

    def _append_row(self, dist_m: float, h1: float, h2: float):
        row = self.table.rowCount()
        self.table.insertRow(row)
        items = [
            QTableWidgetItem(f"{h1:.2f}"),
            QTableWidgetItem(f"{h2:.2f}"),
            QTableWidgetItem(self._format_distance(dist_m)),
            QTableWidgetItem(self._format_duration(self._segment_duration_s(dist_m), self._time_unit())),
            QTableWidgetItem(self._format_duration(self._cumulative_time_for_index(row), self._time_unit()))
        ]
        for col, it in enumerate(items):
            it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(row, col, it)

    def _append_waypoint_row(self, wp_id: int, pt: QgsPointXY, dist_to_next: Optional[float], bearing_to_next: Optional[float], cum_m: float = None):
        row = self.waypoints_table.rowCount()
        self.waypoints_table.insertRow(row)
        if cum_m is None:
            cum_m = sum(self.distances_m[:wp_id]) if wp_id <= len(self.distances_m) else sum(self.distances_m)
        factor = self._distance_factor()
        unit = self._time_unit()
        speed_mps = self._speed_mps()
        cum_t_s = cum_m / speed_mps if speed_mps > 0 else 0
        items = [
            QTableWidgetItem(str(wp_id)),
            QTableWidgetItem(f"{pt.y():.6f}"),
            QTableWidgetItem(f"{pt.x():.6f}"),
            QTableWidgetItem(f"{bearing_to_next:.2f}" if bearing_to_next is not None else "-"),
            QTableWidgetItem(self._format_distance(dist_to_next) if dist_to_next is not None else "-"),
            QTableWidgetItem(self._format_distance(cum_m)),
            QTableWidgetItem(self._format_duration(cum_t_s, unit) if speed_mps > 0 else "-")
        ]
        for col, it in enumerate(items):
            it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.waypoints_table.setItem(row, col, it)

    def _update_motion_row(self, dist_m: float, h1: float, h2: float):
        motion_row = len(self.distances_m)
        if self.table.rowCount() == motion_row:
            self.table.insertRow(motion_row)
        items = [
            QTableWidgetItem(f"{h1:.2f}"),
            QTableWidgetItem(f"{h2:.2f}"),
            QTableWidgetItem(self._format_distance(dist_m)),
            QTableWidgetItem(self._format_duration(self._segment_duration_s(dist_m))),
            QTableWidgetItem(self._format_duration(self._cumulative_time_for_index(motion_row, include_motion=True), self._time_unit()))
        ]
        for col, it in enumerate(items):
            it.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(motion_row, col, it)

    def _truncate_motion_row(self):
        motion_row = len(self.distances_m)
        if self.table.rowCount() > motion_row:
            self.table.removeRow(motion_row)

    def _repopulate_waypoints(self):
        self.waypoints_table.setRowCount(0)
        cum_m = 0.0
        for idx, pt in enumerate(self.points):
            dist_next = self.distances_m[idx] if idx < len(self.distances_m) else None
            bearing_next = self.headings_fwd[idx] if idx < len(self.headings_fwd) else None
            self._append_waypoint_row(idx + 1, pt, dist_next, bearing_next, cum_m)
            if idx < len(self.distances_m):
                cum_m += self.distances_m[idx]

    def _repopulate_distances(self):
        for i, dist_m in enumerate(self.distances_m):
            item = self.table.item(i, 2)
            if item:
                item.setText(self._format_distance(dist_m))
        if self.motion_distance_m and self.table.rowCount() == len(self.distances_m) + 1:
            mitem = self.table.item(len(self.distances_m), 2)
            if mitem:
                mitem.setText(self._format_distance(self.motion_distance_m))
        self._repopulate_waypoints()
        self._update_totals()

    def _recompute_durations(self):
        unit = self._time_unit()
        for i, dist_m in enumerate(self.distances_m):
            dur_item = self.table.item(i, 3)
            if dur_item:
                dur_item.setText(self._format_duration(self._segment_duration_s(dist_m), unit))
            cum_item = self.table.item(i, 4)
            if cum_item:
                cum_item.setText(self._format_duration(self._cumulative_time_for_index(i), unit))
        if self.motion_distance_m and self.table.rowCount() == len(self.distances_m) + 1:
            row = len(self.distances_m)
            dur_item = self.table.item(row, 3)
            if dur_item:
                dur_item.setText(self._format_duration(self._segment_duration_s(self.motion_distance_m), unit))
            cum_item = self.table.item(row, 4)
            if cum_item:
                cum_item.setText(self._format_duration(self._cumulative_time_for_index(row, include_motion=True), unit))
        self._repopulate_waypoints()
        self._update_totals()

    def _cumulative_time_for_index(self, idx: int, include_motion=False) -> Optional[float]:
        v = self._speed_mps()
        if v <= 0:
            return None
        total_m = sum(self.distances_m[: idx + 1])
        if include_motion:
            total_m += self.motion_distance_m
        return total_m / v

    def _add_line_segment(self, p1: QgsPointXY, p2: QgsPointXY, dist_m: float):
        pts = self._densify(p1, p2, dist_m)
        self.line_rb.addGeometry(QgsGeometry.fromPolylineXY(pts), None)

    def _update_temp_line(self, p1: QgsPointXY, p2: QgsPointXY, dist_m: float):
        pts = self._densify(p1, p2, dist_m)
        self.temp_rb.setToGeometry(QgsGeometry.fromPolylineXY(pts), None)

    def _densify(self, p1: QgsPointXY, p2: QgsPointXY, dist_m: float):
        if dist_m < 10000:
            return [p1, p2]
        n = min(20, int(math.ceil(dist_m / 10000.0)))
        seg_len = dist_m / n
        pts = [p1]
        bearing = self.da.bearing(p1, p2)
        if hasattr(self.da, 'computeSpheroidProject'):
            for i in range(1, n):
                s = seg_len * i
                inter = self.da.computeSpheroidProject(p1, s, bearing)
                pts.append(inter)
        else:
            for i in range(1, n):
                f = (seg_len * i) / dist_m
                x = p1.x() + f * (p2.x() - p1.x())
                y = p1.y() + f * (p2.y() - p1.y())
                pts.append(QgsPointXY(x, y))
        pts.append(p2)
        return pts

    def _update_totals(self):
        total_m = sum(self.distances_m) + (self.motion_distance_m if self.active else 0.0)
        self.total_dist_edit.setText(self._format_distance(total_m))
        v = self._speed_mps()
        if v > 0:
            total_s = total_m / v
            self.total_time_edit.setText(self._format_duration(total_s, self._time_unit()))
        else:
            self.total_time_edit.setText("-")

    def _refresh_unit_labels(self):
        pass

    def save_layer(self):
        if len(self.distances_m) == 0:
            QMessageBox.information(self, tr("Nothing to Save"), tr("Add at least two points."))
            return
        proj = QgsProject.instance()
        layer = QgsVectorLayer(f"LineString?crs={proj.crs().authid()}", tr("Transit Measurement"), "memory")
        fields = QgsFields()
        fields.append(QgsField("segment_id", QVariant.Int))
        fields.append(QgsField("dist", QVariant.Double))
        fields.append(QgsField("dist_unit", QVariant.String))
        fields.append(QgsField("duration_hr", QVariant.Double))
        fields.append(QgsField("speed_val", QVariant.Double))
        fields.append(QgsField("speed_unit", QVariant.String))
        fields.append(QgsField("head_to", QVariant.Double))
        fields.append(QgsField("head_from", QVariant.Double))
        fields.append(QgsField("cum_dist", QVariant.Double))
        fields.append(QgsField("cum_time_hr", QVariant.Double))
        layer.dataProvider().addAttributes(fields)
        layer.updateFields()
        factor = self._distance_factor()
        dist_unit = DISTANCE_UNITS[self.dist_unit_combo.currentIndex()][0]
        speed_val = self.speed_spin.value()
        speed_unit = SPEED_UNITS[self.speed_unit_combo.currentIndex()][0]
        speed_mps = self._speed_mps()
        cum_m = 0.0
        cum_t_s = 0.0
        for idx, dist_m in enumerate(self.distances_m):
            p1 = self.points[idx]
            p2 = self.points[idx + 1]
            geom_pts = self._densify(p1, p2, dist_m)
            duration_s = dist_m / speed_mps if speed_mps > 0 else None
            cum_m += dist_m
            if duration_s is not None:
                cum_t_s += duration_s
            feat = QgsFeature(layer.fields())
            feat.setAttribute("segment_id", idx + 1)
            feat.setAttribute("dist", dist_m / factor)
            feat.setAttribute("dist_unit", dist_unit)
            feat.setAttribute("duration_hr", (duration_s / 3600.0) if duration_s else None)
            feat.setAttribute("speed_val", speed_val)
            feat.setAttribute("speed_unit", speed_unit)
            feat.setAttribute("head_to", self.headings_fwd[idx])
            feat.setAttribute("head_from", self.headings_rev[idx])
            feat.setAttribute("cum_dist", cum_m / factor)
            feat.setAttribute("cum_time_hr", (cum_t_s / 3600.0) if speed_mps > 0 else None)
            feat.setGeometry(QgsGeometry.fromPolylineXY(geom_pts))
            layer.dataProvider().addFeature(feat)
        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self.iface.messageBar().pushMessage("", tr("Transit measurement layer added."), level=Qgis.Info, duration=4)

    def export_csv(self):
        if len(self.points) == 0:
            QMessageBox.information(self, tr("Nothing to Export"), tr("Add at least one point."))
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr("Export CSV"), "", "CSV files (*.csv)")
        if not filename:
            return
        
        # Check which tab is active
        current_tab = self.tab_widget.currentWidget()
        
        try:
            with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                
                if current_tab == self.segments_tab:
                    # Export segments data
                    if len(self.distances_m) == 0:
                        QMessageBox.information(self, tr("Nothing to Export"), tr("Add at least two points for segments export."))
                        return
                    # Write headers
                    headers = [tr("Bearing To"), tr("Bearing From"), tr("Distance"), tr("Duration"), tr("Cum Time")]
                    writer.writerow(headers)
                    # Write data rows
                    unit = self._time_unit()
                    for i, dist_m in enumerate(self.distances_m):
                        row = [
                            f"{self.headings_fwd[i]:.2f}",
                            f"{self.headings_rev[i]:.2f}",
                            self._format_distance(dist_m),
                            self._format_duration(self._segment_duration_s(dist_m), unit),
                            self._format_duration(self._cumulative_time_for_index(i), unit)
                        ]
                        writer.writerow(row)
                else:
                    # Export waypoints data
                    # Write headers
                    headers = [tr("WP ID"), tr("Latitude"), tr("Longitude"), tr("Bearing To Next"), tr("Dist To Next"), tr("Cum Dist"), tr("Cum Time")]
                    writer.writerow(headers)
                    # Write data rows
                    factor = self._distance_factor()
                    unit = self._time_unit()
                    speed_mps = self._speed_mps()
                    cum_m = 0.0
                    for idx, pt in enumerate(self.points):
                        dist_next = self.distances_m[idx] if idx < len(self.distances_m) else None
                        bearing_next = self.headings_fwd[idx] if idx < len(self.headings_fwd) else None
                        cum_t_s = cum_m / speed_mps if speed_mps > 0 else 0
                        row = [
                            str(idx + 1),
                            f"{pt.y():.6f}",
                            f"{pt.x():.6f}",
                            f"{bearing_next:.2f}" if bearing_next is not None else "-",
                            self._format_distance(dist_next) if dist_next is not None else "-",
                            self._format_distance(cum_m),
                            self._format_duration(cum_t_s, unit) if speed_mps > 0 else "-"
                        ]
                        writer.writerow(row)
                        if idx < len(self.distances_m):
                            cum_m += self.distances_m[idx]
            
            QMessageBox.information(self, tr("Export Successful"), tr("CSV exported successfully."))
        except Exception as e:
            QMessageBox.critical(self, tr("Export Failed"), tr("Failed to export CSV: {0}").format(str(e)))

    def create_waypoints_layer(self):
        if len(self.points) == 0:
            QMessageBox.information(self, tr("Nothing to Create"), tr("Add at least one point."))
            return
        proj = QgsProject.instance()
        layer = QgsVectorLayer(f"Point?crs={proj.crs().authid()}", tr("Transit Waypoints"), "memory")
        fields = QgsFields()
        fields.append(QgsField("wp_id", QVariant.Int))
        fields.append(QgsField("latitude", QVariant.Double))
        fields.append(QgsField("longitude", QVariant.Double))
        fields.append(QgsField("bearing_to_next", QVariant.Double))
        fields.append(QgsField("dist_to_next", QVariant.Double))
        fields.append(QgsField("dist_unit", QVariant.String))
        fields.append(QgsField("cum_dist", QVariant.Double))
        fields.append(QgsField("est_time_to_next", QVariant.String))
        fields.append(QgsField("cum_time", QVariant.String))
        layer.dataProvider().addAttributes(fields)
        layer.updateFields()
        factor = self._distance_factor()
        dist_unit = DISTANCE_UNITS[self.dist_unit_combo.currentIndex()][0]
        speed_mps = self._speed_mps()
        unit = self._time_unit()
        cum_m = 0.0
        cum_t_s = 0.0
        for idx, pt in enumerate(self.points):
            feat = QgsFeature(layer.fields())
            feat.setAttribute("wp_id", idx + 1)
            feat.setAttribute("latitude", pt.y())
            feat.setAttribute("longitude", pt.x())
            if idx < len(self.distances_m):
                dist_m = self.distances_m[idx]
                bearing = self.headings_fwd[idx]
                feat.setAttribute("bearing_to_next", bearing)
                feat.setAttribute("dist_to_next", dist_m / factor)
                feat.setAttribute("dist_unit", dist_unit)
                duration_s = dist_m / speed_mps if speed_mps > 0 else None
                feat.setAttribute("est_time_to_next", self._format_duration(duration_s, unit) if duration_s else "-")
                cum_m += dist_m
                if duration_s:
                    cum_t_s += duration_s
            else:
                feat.setAttribute("bearing_to_next", None)
                feat.setAttribute("dist_to_next", None)
                feat.setAttribute("est_time_to_next", "-")
            feat.setAttribute("cum_dist", cum_m / factor)
            feat.setAttribute("cum_time", self._format_duration(cum_t_s, unit) if speed_mps > 0 else "-")
            feat.setGeometry(QgsGeometry.fromPointXY(pt))
            layer.dataProvider().addFeature(feat)
        layer.updateExtents()
        QgsProject.instance().addMapLayer(layer)
        self.iface.messageBar().pushMessage("", tr("Waypoints layer added."), level=Qgis.Info, duration=4)

    def create_layers(self):
        """Create both the transit measurement layer and waypoints layer."""
        self.save_layer()
        self.create_waypoints_layer()

    def closeEvent(self, evt):
        try:
            self.point_rb.reset(QgsWkbTypes.PointGeometry)
            self.line_rb.reset(QgsWkbTypes.LineGeometry)
            self.temp_rb.reset(QgsWkbTypes.LineGeometry)
            self.clear_waypoint_highlight()
        except Exception:
            pass
        super().closeEvent(evt)

    def _time_unit(self):
        return TIME_UNITS[self.time_unit_combo.currentIndex()][1]
