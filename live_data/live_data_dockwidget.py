"""
Live Data Dock Widget

Main UI container for the live data tool.
Handles data streaming, map layer management, and coordinates with cards dockwidget.
"""

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QCheckBox, QGroupBox, QFormLayout, QTextEdit, QMessageBox,
    QTabWidget
)
from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsField,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform
)
from qgis.PyQt.QtCore import QVariant
from qgis.gui import QgsRubberBand

from .live_data_worker import LiveDataWorker

import uuid
import time
from datetime import datetime
import math


class LiveDataDockWidget(QDockWidget):
    """
    Main dockable widget for live data streaming.
    Handles connection, data receiving, and map layer management.
    Card display is handled by separate LiveDataCardsDockWidget.
    
    Signals:
        headers_received: Emitted when headers received (pass to cards widget)
        data_received_raw: Emitted with dict of data values
    """
    
    headers_received = pyqtSignal(list)      # Emit to cards widget
    data_received_raw = pyqtSignal(dict)     # Emit to cards widget
    connected_state_changed = pyqtSignal(bool)
    
    def __init__(self, iface, parent=None):
        super().__init__("Live Data", parent)
        self.iface = iface
        self.setObjectName("LiveDataDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        self.layer = None
        self.worker = None
        self.connected = False
        
        # Overlays
        self.overlays_config = []
        self.overlay_geometries = {}
        self.overlay_pivots = {}
        self.rubber_bands = {}
        self.headers = None
        self.project_crs = QgsProject.instance().crs()
        if not self.project_crs.isValid():
            self.project_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        
        # Connect to project signals to detect layer deletion
        QgsProject.instance().layersRemoved.connect(self.on_layers_removed)
        
        self.setup_ui()
    
    def set_overlays_config(self, configs):
        """Set overlay configurations and load geometries."""
        self.overlays_config = configs if configs else []
        self.load_overlay_geometries()
    
    def load_overlay_geometries(self):
        """Load geometries from DXF files and create rubber bands."""
        try:
            # Clear existing
            for rb in self.rubber_bands.values():
                try:
                    rb.reset()
                    rb.hide()
                    rb.deleteLater()
                except Exception:
                    pass
            self.rubber_bands.clear()
            self.overlay_geometries.clear()
            self.overlay_pivots.clear()
            
            for i, config in enumerate(self.overlays_config):
                try:
                    dxf_file = config.get('dxf_file')
                    if not dxf_file:
                        continue
                    
                    geom = self.load_dxf_geometry(
                        dxf_file, 
                        config.get('scale', 1.0), 
                        config.get('crp_offset_x', 0.0), 
                        config.get('crp_offset_y', 0.0)
                    )
                    if geom and not geom.isEmpty():
                        self.overlay_geometries[i] = geom
                        self.overlay_pivots[i] = QgsPointXY(0.0, 0.0)
                        rb = QgsRubberBand(self.iface.mapCanvas(), geom.type())
                        rb.setStrokeColor(Qt.red)
                        rb.setFillColor(Qt.transparent)
                        rb.setWidth(2)
                        rb.hide()
                        self.rubber_bands[i] = rb
                except Exception as e:
                    print(f"Error loading overlay {i}: {e}")
        except Exception as e:
            print(f"Error in load_overlay_geometries: {e}")
    
    def load_dxf_geometry(self, dxf_path, scale, crp_x, crp_y):
        """Load geometry from DXF file. Apply scale and CRP offset only (no rotation)."""
        try:
            layer = QgsVectorLayer(dxf_path, "temp", "ogr")
            if not layer.isValid():
                print(f"Failed to load DXF: {dxf_path}")
                return None
            
            geoms = []
            for feature in layer.getFeatures():
                geom = feature.geometry()
                if geom and not geom.isEmpty():
                    # Transform: offset (CRP) and scale only. Rotation happens in update_overlays per data point.
                    transformed = self._transform_geometry_for_overlay(geom, scale, 0.0, crp_x, crp_y)
                    if transformed and not transformed.isEmpty():
                        geoms.append(transformed)
            
            if not geoms:
                print(f"No valid geometries found in DXF: {dxf_path}")
                return None
            
            # Merge all geometries into one
            merged = geoms[0]
            for g in geoms[1:]:
                merged = merged.combine(g)
            
            print(f"Loaded DXF with {len(geoms)} geometries, merged into 1. Bounds: {merged.boundingBox()}")
            return merged
        except Exception as e:
            print(f"Error loading DXF geometry: {e}")
            return None
    
    def _transform_geometry_for_overlay(self, geom, scale, rotation_deg, offset_x, offset_y):
        """Transform geometry: offset (CRP), scale, rotate. Handles both multipart and single geometries."""
        try:
            rad = math.radians(rotation_deg)
            cos_a = math.cos(rad)
            sin_a = math.sin(rad)
            
            def transform_point(pt):
                """Apply offset, scale, then rotate."""
                x = (pt.x() - offset_x) * scale
                y = (pt.y() - offset_y) * scale
                x_rot = x * cos_a - y * sin_a
                y_rot = x * sin_a + y * cos_a
                return QgsPointXY(x_rot, y_rot)
            
            wkb_type = geom.wkbType()
            is_multipart = geom.isMultipart()
            
            # LineString (single or multi)
            if wkb_type % 1000 == 2:
                if is_multipart:
                    lines = []
                    for line in geom.asMultiPolyline():
                        new_line = [transform_point(pt) for pt in line]
                        lines.append(new_line)
                    return QgsGeometry.fromMultiPolylineXY(lines)
                else:
                    line = geom.asPolyline()
                    new_line = [transform_point(pt) for pt in line]
                    return QgsGeometry.fromPolylineXY(new_line)
            
            # Polygon (single or multi)
            elif wkb_type % 1000 == 3:
                if is_multipart:
                    try:
                        polys = []
                        for poly in geom.asMultiPolygon():
                            new_poly = []
                            for ring in poly:
                                new_ring = [transform_point(pt) for pt in ring]
                                new_poly.append(new_ring)
                            polys.append(new_poly)
                        return QgsGeometry.fromMultiPolygonXY(polys)
                    except Exception:
                        return geom
                else:
                    try:
                        poly = geom.asPolygon()
                        new_poly = []
                        for ring in poly:
                            new_ring = [transform_point(pt) for pt in ring]
                            new_poly.append(new_ring)
                        return QgsGeometry.fromPolygonXY(new_poly)
                    except Exception:
                        return geom
            
            return geom
        except Exception as e:
            print(f"Error transforming geometry: {e}")
            return geom
    
    def _rotate_geometry(self, geom, angle_degrees, pivot=None):
        """Rotate geometry around a pivot point (default 0,0) by angle in degrees."""
        try:
            if geom.isEmpty():
                return geom

            pivot_point = pivot if pivot is not None else QgsPointXY(0.0, 0.0)
            rad = math.radians(angle_degrees)
            cos_a = math.cos(rad)
            sin_a = math.sin(rad)
            
            wkb_type = geom.wkbType()
            
            # LineString
            if wkb_type % 1000 == 2:
                points = []
                for pt in geom.asPolyline():
                    dx = pt.x() - pivot_point.x()
                    dy = pt.y() - pivot_point.y()
                    new_x = pivot_point.x() + dx * cos_a - dy * sin_a
                    new_y = pivot_point.y() + dx * sin_a + dy * cos_a
                    points.append(QgsPointXY(new_x, new_y))
                return QgsGeometry.fromPolylineXY(points)
            
            # Polygon
            elif wkb_type % 1000 == 3:
                try:
                    rings = []
                    for ring in geom.asPolygon():
                        points = []
                        for pt in ring:
                            dx = pt.x() - pivot_point.x()
                            dy = pt.y() - pivot_point.y()
                            new_x = pivot_point.x() + dx * cos_a - dy * sin_a
                            new_y = pivot_point.y() + dx * sin_a + dy * cos_a
                            points.append(QgsPointXY(new_x, new_y))
                        rings.append(points)
                    return QgsGeometry.fromPolygonXY(rings)
                except Exception:
                    return geom
            
            return geom
        except Exception as e:
            print(f"Error rotating geometry: {e}")
            return geom
    
    def update_overlays(self, data_dict):
        """Update overlay positions based on data."""
        if not self.overlays_config or not self.overlay_geometries:
            return
        
        try:
            for i, config in enumerate(self.overlays_config):
                if i not in self.overlay_geometries:
                    continue
                
                try:
                    lat_field = config.get('lat_field')
                    lon_field = config.get('lon_field')
                    heading_field = config.get('heading_field')
                    
                    lat = data_dict.get(lat_field)
                    lon = data_dict.get(lon_field)
                    heading = data_dict.get(heading_field)
                    
                    if lat is None or lon is None or heading is None:
                        continue
                    
                    lat = float(lat)
                    lon = float(lon)
                    heading = float(heading)
                    
                    # Get base geometry (already has scale and CRP offset applied)
                    base_geom = self.overlay_geometries[i]
                    pivot = self.overlay_pivots.get(i, QgsPointXY(0.0, 0.0))

                    # Apply rotation around CRP. Heading is clockwise from north, so rotate by -heading.
                    total_rotation = -heading + config.get('rotation_offset', 0.0)
                    rotated_geom = self._rotate_geometry(base_geom, total_rotation, pivot)
                    if not rotated_geom or rotated_geom.isEmpty():
                        continue
                    
                    # Transform live data point to map CRS
                    point_geom = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
                    transform = QgsCoordinateTransform(
                        QgsCoordinateReferenceSystem("EPSG:4326"), 
                        self.project_crs, 
                        QgsProject.instance()
                    )
                    point_geom.transform(transform)
                    map_point = point_geom.asPoint()
                    
                    # Translate rotated geometry so its pivot (CRP) goes to the live data point
                    dx = map_point.x() - pivot.x()
                    dy = map_point.y() - pivot.y()

                    final_geom = rotated_geom.clone() if hasattr(rotated_geom, "clone") else QgsGeometry(rotated_geom)
                    final_geom.translate(dx, dy)
                    
                    # Update rubber band
                    rb = self.rubber_bands.get(i)
                    if rb:
                        rb.setToGeometry(final_geom, None)
                        rb.show()
                
                except (ValueError, TypeError, KeyError) as e:
                    continue
        except Exception as e:
            print(f"Error in update_overlays: {e}")

    def setup_ui(self):
        """Build the main UI layout with tabs."""
        print("[LIVE_DATA] DEBUG: setup_ui() called")
        
        # Create main container
        self.widget = QWidget()
        self.setWidget(self.widget)
        main_layout = QVBoxLayout(self.widget)
        main_layout.setContentsMargins(5, 5, 5, 5)

        # Create and setup tab widget
        self.tabs = QTabWidget()
        self.tabs.setObjectName("LiveDataTabs")
        print("[LIVE_DATA] DEBUG: Tab widget created")
        
        # ===== TAB 1: CONNECTION =====
        print("[LIVE_DATA] DEBUG: Creating Connection tab...")
        connection_widget = self._create_connection_tab()
        self.tabs.addTab(connection_widget, "Connection")
        print("[LIVE_DATA] DEBUG: Connection tab added")

        # ===== TAB 2: DATA MONITOR =====
        print("[LIVE_DATA] DEBUG: Creating Data Monitor tab...")
        monitor_widget = self._create_monitor_tab()
        self.tabs.addTab(monitor_widget, "Data Monitor")
        print("[LIVE_DATA] DEBUG: Data Monitor tab added")

        # Add tabs to main layout
        main_layout.addWidget(self.tabs)
        print(f"[LIVE_DATA] DEBUG: setup_ui() complete - {self.tabs.count()} tabs created")

    def _create_connection_tab(self) -> QWidget:
        """Create the Connection tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        # Connection settings
        conn_group = QGroupBox("Connection")
        conn_layout = QFormLayout(conn_group)

        self.host_edit = QLineEdit("localhost")
        self.port_edit = QLineEdit("12345")

        conn_layout.addRow("Host:", self.host_edit)
        conn_layout.addRow("Port:", self.port_edit)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.connect_server)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.disconnect_server)
        self.disconnect_btn.setEnabled(False)

        btn_layout = QHBoxLayout()
        btn_layout.addWidget(self.connect_btn)
        btn_layout.addWidget(self.disconnect_btn)
        btn_layout.addStretch()

        conn_layout.addRow(btn_layout)
        layout.addWidget(conn_group)

        # Data settings
        data_group = QGroupBox("Data Settings")
        data_layout = QFormLayout(data_group)

        self.lat_field_edit = QLineEdit("Lat_dd")
        self.lon_field_edit = QLineEdit("Lon_dd")

        data_layout.addRow("Latitude Field:", self.lat_field_edit)
        data_layout.addRow("Longitude Field:", self.lon_field_edit)

        self.persist_chk = QCheckBox("Persist Points")
        self.persist_chk.setChecked(True)
        self.persist_chk.setToolTip("If checked, all received points remain on the map. If unchecked, only the latest point is shown.")

        data_layout.addRow(self.persist_chk)
        layout.addWidget(data_group)
        
        # Status
        self.status_label = QLabel("Disconnected")
        layout.addWidget(self.status_label)
        
        layout.addStretch()
        return widget


    def _create_monitor_tab(self) -> QWidget:
        """Create the Data Monitor tab."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        received_group = QGroupBox("Received Data")
        received_layout = QVBoxLayout(received_group)
        
        received_layout.addWidget(QLabel("Headers:"))
        self.headers_text = QTextEdit()
        self.headers_text.setMaximumHeight(100)
        self.headers_text.setReadOnly(True)
        received_layout.addWidget(self.headers_text)
        
        received_layout.addWidget(QLabel("Latest Data String:"))
        self.received_text = QTextEdit()
        self.received_text.setMaximumHeight(100)
        self.received_text.setReadOnly(True)
        received_layout.addWidget(self.received_text)
        
        layout.addWidget(received_group)
        return widget

    def connect_server(self):
        """Connect to the data server."""
        if self.connected:
            return

        host = self.host_edit.text()
        port = int(self.port_edit.text())
        lat_field = self.lat_field_edit.text()
        lon_field = self.lon_field_edit.text()
        persist = self.persist_chk.isChecked()

        self.worker = LiveDataWorker(host, port, lat_field, lon_field, persist)
        self.worker.data_received.connect(self.on_data_received)
        self.worker.status_changed.connect(self.on_status_changed)
        self.worker.headers_received.connect(self.on_headers_received)
        self.worker.raw_data_received.connect(self.on_raw_data_received)
        self.worker.start()

        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)

    def disconnect_server(self):
        """Disconnect from the data server."""
        if self.worker:
            # Disconnect signals before stopping to prevent data_received callbacks
            self.worker.data_received.disconnect(self.on_data_received)
            self.worker.stop()
            self.worker = None
        self.connected = False
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.status_label.setText("Disconnected")
        self.connected_state_changed.emit(False)

    def on_status_changed(self, status):
        """Handle status changes from worker."""
        self.status_label.setText(status)
        if "Connected" in status:
            self.connected = True
            self.connected_state_changed.emit(True)
        elif "Disconnected" in status or "Error" in status:
            self.connected = False
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)
            self.connected_state_changed.emit(False)

    def on_headers_received(self, headers):
        """Handle headers from data stream."""
        self.headers = headers
        self.headers_text.setText('\n'.join(headers))
        print(f"Received headers ({len(headers)} fields): {headers}")
        self.create_layer()
        # Emit signal for cards display widget
        self.headers_received.emit(headers)

    def on_raw_data_received(self, line):
        """Handle raw data string display."""
        self.received_text.setText(line)

    def on_data_received(self, data_dict):
        """
        Handle new data from stream.
        Updates map points and emits signal for card display widget.
        """
        print(f"Received data: {data_dict}")
        
        # Check if layer still exists; recreate if needed
        if not self.layer and self.headers:
            print("Layer was deleted, recreating it...")
            self.create_layer()
        
        if not self.layer or not self.worker:
            return

        # Clear if not persisting (use worker's persist setting, not checkbox)
        if not self.worker.get_persist():
            self.layer.dataProvider().truncate()

        # Add feature to map
        try:
            lat_field = self.lat_field_edit.text()
            lon_field = self.lon_field_edit.text()
            lat_str = data_dict.get(lat_field)
            lon_str = data_dict.get(lon_field)
            print(f"Lat field '{lat_field}': {lat_str}, Lon field '{lon_field}': {lon_str}")
            if lat_str is None or lon_str is None:
                self.status_label.setText(f"Error: Latitude field '{lat_field}' or Longitude field '{lon_field}' not found in data")
                return
            lat = float(lat_str)
            lon = float(lon_str)
            print(f"Parsed lat: {lat}, lon: {lon}")
            point = QgsPointXY(lon, lat)
            geom = QgsGeometry.fromPointXY(point)
            # Transform from WGS84 to project CRS
            transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), self.project_crs, QgsProject.instance())
            geom.transform(transform)
            feat = QgsFeature()
            feat.setGeometry(geom)
            if self.headers:
                feat.setAttributes([data_dict.get(h, '') for h in self.headers])
            self.layer.dataProvider().addFeature(feat)
            self.layer.updateExtents()
            # Trigger layer repaint and canvas refresh
            self.layer.triggerRepaint()
            self.iface.mapCanvas().refresh()
            print("Feature added successfully")
        except (ValueError, KeyError) as e:
            self.status_label.setText(f"Error parsing data: {str(e)}")
            print(f"Error parsing data: {str(e)}")
        except RuntimeError as e:
            # Handle case where layer was deleted between checks
            print(f"Layer error (layer may have been deleted): {str(e)}")
            self.layer = None
            self.status_label.setText("Error: Live data layer was deleted. Reconnect to create a new layer.")
            return
        
        # Emit signal for cards display widget to update
        self.data_received_raw.emit(data_dict)
        
        # Update overlays
        self.update_overlays(data_dict)

    def create_layer(self):
        """Create the memory layer for map points."""
        if not self.headers:
            return
        self.layer = QgsVectorLayer(f"Point?crs={self.project_crs.authid()}", "Live Data Points", "memory")
        provider = self.layer.dataProvider()
        fields = [QgsField(h, QVariant.String) for h in self.headers]
        provider.addAttributes(fields)
        self.layer.updateFields()
        QgsProject.instance().addMapLayer(self.layer)

    def on_layers_removed(self, layer_ids):
        """Handle when layers are removed from the project."""
        if self.layer and self.layer.id() in layer_ids:
            print(f"Live Data Points layer was removed from project")
            self.layer = None
            self.status_label.setText("Live data layer deleted. Will recreate on next data point.")

    def closeEvent(self, event):
        """
        Handle widget close request.
        Hide instead of closing to preserve connection state.
        User can uncheck in manager or close entire tool to disconnect.
        """
        # Hide the widget instead of closing it
        # This preserves the connection to the server
        self.hide()
        event.ignore()  # Don't actually close/destroy the widget
    
    def force_cleanup(self):
        """
        Force cleanup of all resources.
        Called during plugin unload to ensure clean termination.
        """
        try:
            print("[LIVE_DATA] DEBUG: force_cleanup() called")
            
            # Block all signals to prevent callbacks during cleanup
            self.blockSignals(True)
            
            # Disconnect from project signals
            try:
                QgsProject.instance().layersRemoved.disconnect(self.on_layers_removed)
            except Exception:
                pass
            
            # Stop and cleanup the worker thread
            if self.worker:
                try:
                    print("[LIVE_DATA] DEBUG: Stopping worker thread...")
                    self.worker.blockSignals(True)
                    # Disconnect all signals first
                    try:
                        self.worker.data_received.disconnect(self.on_data_received)
                    except Exception:
                        pass
                    try:
                        self.worker.status_changed.disconnect(self.on_status_changed)
                    except Exception:
                        pass
                    try:
                        self.worker.headers_received.disconnect(self.on_headers_received)
                    except Exception:
                        pass
                    try:
                        self.worker.raw_data_received.disconnect(self.on_raw_data_received)
                    except Exception:
                        pass
                    # Now stop the thread
                    self.worker.stop()
                    print("[LIVE_DATA] DEBUG: Worker thread stopped")
                except Exception as e:
                    print(f"[LIVE_DATA] DEBUG: Error stopping worker: {e}")
                self.worker = None
            
            # Preserve the layer - don't delete it so users don't lose data
            # Just clear our reference to it so the widget can be safely destroyed
            # The layer will remain in the project for the user to examine/save
            if self.layer:
                try:
                    print(f"[LIVE_DATA] DEBUG: Preserving layer '{self.layer.name()}' in project")
                    # Stop listening to layer deletion events since we're preserving it
                    # but clearing our reference
                except Exception as e:
                    print(f"[LIVE_DATA] DEBUG: Error preserving layer: {e}")
                self.layer = None  # Clear reference but leave layer in project
            
            self.connected = False
            
        except Exception as e:
            print(f"[LIVE_DATA] DEBUG: Error during force_cleanup: {e}")
