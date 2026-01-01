"""
Live Data Dock Widget

Main UI container for the live data tool.
Handles data streaming, map layer management, and coordinates with cards dockwidget.
"""

from __future__ import annotations

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
from .mock_data_worker import MockDataWorker
from .message_parser import MessageFormatConfig, FORMAT_CSV_HEADER

import sip

from dataclasses import dataclass, field
from typing import Dict, Optional, List

import uuid
import time
from datetime import datetime
import math

@dataclass
class _LiveDataSlot:
    slot_id: str
    name: str

    # Config
    lat_field: str = "Lat_dd"
    lon_field: str = "Lon_dd"
    persist: bool = True
    parser_config: MessageFormatConfig = field(default_factory=lambda: MessageFormatConfig(kind=FORMAT_CSV_HEADER))

    # Runtime
    headers: Optional[List[str]] = None
    last_raw: str = ""
    status: str = "Disconnected"
    connected: bool = False
    worker: Optional[LiveDataWorker] = None
    mock_worker: Optional[MockDataWorker] = None
    layer: Optional[QgsVectorLayer] = None

    # Overlays (per slot)
    overlays_config: List[dict] = field(default_factory=list)
    overlay_geometries: Dict[int, QgsGeometry] = field(default_factory=dict)
    overlay_pivots: Dict[int, QgsPointXY] = field(default_factory=dict)
    rubber_bands: Dict[int, QgsRubberBand] = field(default_factory=dict)


class LiveDataDockWidget(QDockWidget):
    """Main dockable widget for live data streaming.

    Handles connection, data receiving, and map layer management.
    Card display is handled by separate LiveDataCardsDockWidget.
    """

    headers_received = pyqtSignal(list)      # Emit to cards widget
    data_received_raw = pyqtSignal(dict)     # Emit to cards widget
    connected_state_changed = pyqtSignal(bool)

    def __init__(self, iface, parent=None):
        super().__init__("Live Data", parent)
        self.iface = iface
        self.setObjectName("LiveDataDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        # Multi-slot state
        self.slots: Dict[str, _LiveDataSlot] = {}
        self.active_slot_id: Optional[str] = None

        # Active-slot convenience (mirrors)
        self.headers = None
        self.project_crs = QgsProject.instance().crs()
        if not self.project_crs.isValid():
            self.project_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        # Connect to project signals to detect layer deletion
        QgsProject.instance().layersRemoved.connect(self.on_layers_removed)

        # Create a default slot
        default_id = str(uuid.uuid4())
        self._ensure_slot(default_id, "Default")
        self.set_active_slot(default_id)

        self.setup_ui()
    
    def set_overlays_config(self, configs):
        """Set overlay configurations for the active slot and load geometries."""
        if not self.active_slot_id:
            return
        slot = self.slots.get(self.active_slot_id)
        if not slot:
            return
        slot.overlays_config = configs if configs else []
        self._load_overlay_geometries_for_slot(slot)

    def set_overlays_config_for_slot(self, slot_id: str, configs):
        """Set overlay configurations for a specific slot (without changing active slot)."""
        slot = self.slots.get(slot_id)
        if not slot:
            return
        slot.overlays_config = configs if configs else []
        self._load_overlay_geometries_for_slot(slot)
    
    def _load_overlay_geometries_for_slot(self, slot: _LiveDataSlot):
        """Load geometries from DXF files and create rubber bands for a slot."""
        try:
            for rb in slot.rubber_bands.values():
                try:
                    rb.reset()
                    rb.hide()
                    rb.deleteLater()
                except Exception:
                    pass
            slot.rubber_bands.clear()
            slot.overlay_geometries.clear()
            slot.overlay_pivots.clear()

            for i, config in enumerate(slot.overlays_config or []):
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
                        slot.overlay_geometries[i] = geom
                        slot.overlay_pivots[i] = QgsPointXY(0.0, 0.0)
                        rb = QgsRubberBand(self.iface.mapCanvas(), geom.type())
                        rb.setStrokeColor(Qt.red)
                        rb.setFillColor(Qt.transparent)
                        rb.setWidth(2)
                        rb.hide()
                        slot.rubber_bands[i] = rb
                except Exception as e:
                    print(f"Error loading overlay {i} for slot '{slot.name}': {e}")
        except Exception as e:
            print(f"Error in _load_overlay_geometries_for_slot: {e}")
    
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
    
    def update_overlays(self, slot: _LiveDataSlot, data_dict):
        """Update overlay positions for a slot based on data."""
        if not slot.overlays_config or not slot.overlay_geometries:
            return
        
        try:
            for i, config in enumerate(slot.overlays_config):
                if i not in slot.overlay_geometries:
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
                    base_geom = slot.overlay_geometries[i]
                    pivot = slot.overlay_pivots.get(i, QgsPointXY(0.0, 0.0))

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
                    rb = slot.rubber_bands.get(i)
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

        # ===== TAB 3: MOCK/TEST =====
        mock_widget = self._create_mock_tab()
        self.tabs.addTab(mock_widget, "Mock/Test")

        # Add tabs to main layout
        main_layout.addWidget(self.tabs)
        print(f"[LIVE_DATA] DEBUG: setup_ui() complete - {self.tabs.count()} tabs created")

    def _layer_is_valid(self) -> bool:
        """Return True if self.layer is a live QgsVectorLayer wrapper.

        QGIS can delete layers while background threads are still emitting signals.
        Accessing a deleted SIP-wrapped object raises RuntimeError; we treat that
        as "layer not valid" and recreate as needed.
        """
        # Backwards-compatible wrapper for active slot
        slot = self.slots.get(self.active_slot_id) if self.active_slot_id else None
        layer = slot.layer if slot else None
        return self._is_layer_valid(layer)

    def _is_layer_valid(self, layer: Optional[QgsVectorLayer]) -> bool:
        if layer is None:
            return False
        try:
            if sip.isdeleted(layer):
                return False
        except Exception:
            # If sip isn't available for some reason, fall back to RuntimeError checks.
            pass
        try:
            _ = layer.id()
            return True
        except RuntimeError:
            return False

    def _ensure_slot(self, slot_id: str, name: str) -> _LiveDataSlot:
        if slot_id in self.slots:
            return self.slots[slot_id]
        slot = _LiveDataSlot(slot_id=slot_id, name=name)
        self.slots[slot_id] = slot
        return slot

    def set_active_slot(self, slot_id: str):
        """Set which slot is treated as the active one for cards/plots/tables."""
        if not slot_id or slot_id not in self.slots:
            return

        self.active_slot_id = slot_id
        slot = self.slots[slot_id]

        # Update monitor UI
        try:
            self.headers = slot.headers
            self.headers_text.setText('\n'.join(slot.headers or []))
            self.received_text.setText(slot.last_raw or "")
            self.status_label.setText(slot.status or "Disconnected")
        except Exception:
            pass

        # Push state to other windows
        if slot.headers:
            self.headers_received.emit(slot.headers)
        self.connected_state_changed.emit(bool(slot.connected))

    def _create_mock_tab(self) -> QWidget:
        """Create the Mock/Test tab.

        This provides in-plugin testing without requiring an external TCP server.
        """
        from qgis.PyQt.QtWidgets import QDoubleSpinBox, QComboBox

        widget = QWidget()
        layout = QVBoxLayout(widget)

        group = QGroupBox("Mock Playback")
        form = QFormLayout(group)

        self.mock_layer_combo = QComboBox()
        self.mock_refresh_btn = QPushButton("Refresh Layers")
        self.mock_refresh_btn.clicked.connect(self._refresh_mock_layers)

        layer_row = QHBoxLayout()
        layer_row.addWidget(self.mock_layer_combo, 1)
        layer_row.addWidget(self.mock_refresh_btn)
        form.addRow("Input Layer:", layer_row)

        self.mock_interval_spin = QDoubleSpinBox()
        self.mock_interval_spin.setRange(0.05, 60.0)
        self.mock_interval_spin.setSingleStep(0.1)
        self.mock_interval_spin.setValue(1.0)
        form.addRow("Interval (s):", self.mock_interval_spin)

        self.mock_loop_chk = QCheckBox("Loop")
        self.mock_loop_chk.setChecked(True)
        form.addRow(self.mock_loop_chk)

        btn_row = QHBoxLayout()
        self.mock_start_btn = QPushButton("Start Mock")
        self.mock_start_btn.clicked.connect(self.start_mock)
        self.mock_stop_btn = QPushButton("Stop Mock")
        self.mock_stop_btn.clicked.connect(self.stop_mock)
        self.mock_stop_btn.setEnabled(False)
        btn_row.addWidget(self.mock_start_btn)
        btn_row.addWidget(self.mock_stop_btn)
        btn_row.addStretch()
        form.addRow(btn_row)

        layout.addWidget(group)
        layout.addStretch()

        self._refresh_mock_layers()
        return widget

    def _refresh_mock_layers(self):
        """Populate the mock layer dropdown with current project vector layers."""
        try:
            self.mock_layer_combo.clear()
            for layer in QgsProject.instance().mapLayers().values():
                if isinstance(layer, QgsVectorLayer):
                    self.mock_layer_combo.addItem(layer.name(), layer.id())
        except Exception as e:
            print(f"[LIVE_DATA] DEBUG: Error refreshing mock layers: {e}")

    def set_parser_config(self, config: MessageFormatConfig):
        """Set the parser configuration for the active slot."""
        if not self.active_slot_id:
            return
        slot = self.slots.get(self.active_slot_id)
        if not slot:
            return
        slot.parser_config = config or MessageFormatConfig(kind=FORMAT_CSV_HEADER)

    def connect_slot(self, config: dict):
        """Connect a specific slot to TCP based on config dict."""
        slot_id = (config or {}).get("slot_id")
        name = (config or {}).get("slot_name") or "Input"
        if not slot_id:
            return

        slot = self._ensure_slot(str(slot_id), str(name))

        # Apply mapping + parser
        slot.lat_field = (config or {}).get("lat_field") or slot.lat_field
        slot.lon_field = (config or {}).get("lon_field") or slot.lon_field
        slot.persist = bool((config or {}).get("persist", slot.persist))

        try:
            p = (config or {}).get("parser") or {}
            slot.parser_config = MessageFormatConfig(
                kind=p.get("kind") or FORMAT_CSV_HEADER,
                csv_delimiter=p.get("csv_delimiter", ","),
                csv_quotechar=p.get("csv_quotechar", '"'),
                csv_fixed_headers=p.get("csv_fixed_headers", []) or [],
                kv_pair_delimiter=p.get("kv_pair_delimiter", ","),
                kv_kv_delimiter=p.get("kv_kv_delimiter", "="),
                kv_strip_whitespace=bool(p.get("kv_strip_whitespace", True)),
                json_require_object=bool(p.get("json_require_object", True)),
                regex_pattern=p.get("regex_pattern", ""),
                regex_flags=int(p.get("regex_flags", 0) or 0),
            )
        except Exception:
            pass

        # Stop any existing workers for this slot
        self.disconnect_slot(str(slot_id))

        host = (config or {}).get("host") or "localhost"
        port = int((config or {}).get("port") or 12345)

        worker = LiveDataWorker(
            str(host),
            port,
            slot.lat_field,
            slot.lon_field,
            slot.persist,
            parser_config=slot.parser_config,
        )

        worker.data_received.connect(lambda d, sid=slot.slot_id: self.on_data_received(sid, d))
        worker.status_changed.connect(lambda s, sid=slot.slot_id: self.on_status_changed(sid, s))
        worker.headers_received.connect(lambda h, sid=slot.slot_id: self.on_headers_received(sid, h))
        worker.raw_data_received.connect(lambda r, sid=slot.slot_id: self.on_raw_data_received(sid, r))

        slot.worker = worker
        worker.start()

        # Ensure active slot follows selection in control dialog
        self.set_active_slot(slot.slot_id)

    def disconnect_slot(self, slot_id: str):
        slot = self.slots.get(slot_id)
        if not slot:
            return

        # TCP worker
        if slot.worker:
            try:
                slot.worker.stop()
            except Exception:
                pass
            slot.worker = None

        # Mock worker
        if slot.mock_worker:
            try:
                slot.mock_worker.stop()
            except Exception:
                pass
            slot.mock_worker = None

        slot.connected = False
        slot.status = "Disconnected"

        if self.active_slot_id == slot_id:
            self.status_label.setText(slot.status)
            self.connected_state_changed.emit(False)

    def start_mock_slot(self, config: dict):
        slot_id = (config or {}).get("slot_id")
        name = (config or {}).get("slot_name") or "Input"
        if not slot_id:
            return

        slot = self._ensure_slot(str(slot_id), str(name))

        # Apply mapping + parser
        slot.lat_field = (config or {}).get("lat_field") or slot.lat_field
        slot.lon_field = (config or {}).get("lon_field") or slot.lon_field
        slot.persist = bool((config or {}).get("persist", slot.persist))

        try:
            p = (config or {}).get("parser") or {}
            slot.parser_config = MessageFormatConfig(
                kind=p.get("kind") or FORMAT_CSV_HEADER,
                csv_delimiter=p.get("csv_delimiter", ","),
                csv_quotechar=p.get("csv_quotechar", '"'),
                csv_fixed_headers=p.get("csv_fixed_headers", []) or [],
                kv_pair_delimiter=p.get("kv_pair_delimiter", ","),
                kv_kv_delimiter=p.get("kv_kv_delimiter", "="),
                kv_strip_whitespace=bool(p.get("kv_strip_whitespace", True)),
                json_require_object=bool(p.get("json_require_object", True)),
                regex_pattern=p.get("regex_pattern", ""),
                regex_flags=int(p.get("regex_flags", 0) or 0),
            )
        except Exception:
            pass

        self.disconnect_slot(slot.slot_id)

        mock = (config or {}).get("mock") or {}
        layer_id = mock.get("layer_id")
        if not layer_id:
            return

        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            return

        # Build mock lines in the main thread
        try:
            import csv
            import io
            import json

            fields = [f.name() for f in layer.fields()]
            lines = []

            kind = slot.parser_config.kind
            if kind == FORMAT_CSV_HEADER:
                out = io.StringIO()
                writer = csv.writer(out, delimiter=slot.parser_config.csv_delimiter, quotechar=slot.parser_config.csv_quotechar)
                writer.writerow(fields)
                lines.append(out.getvalue().rstrip("\r\n"))

            for feat in layer.getFeatures():
                attrs = {name: feat[name] for name in fields}
                if kind == FORMAT_CSV_HEADER:
                    out = io.StringIO()
                    writer = csv.writer(out, delimiter=slot.parser_config.csv_delimiter, quotechar=slot.parser_config.csv_quotechar)
                    writer.writerow([attrs.get(n, "") for n in fields])
                    lines.append(out.getvalue().rstrip("\r\n"))
                elif kind == "csv_fixed":
                    cols = slot.parser_config.csv_fixed_headers or fields
                    out = io.StringIO()
                    writer = csv.writer(out, delimiter=slot.parser_config.csv_delimiter, quotechar=slot.parser_config.csv_quotechar)
                    writer.writerow([attrs.get(n, "") for n in cols])
                    lines.append(out.getvalue().rstrip("\r\n"))
                elif kind == "kv":
                    parts = []
                    for k, v in attrs.items():
                        parts.append(f"{k}{slot.parser_config.kv_kv_delimiter}{v}")
                    lines.append(slot.parser_config.kv_pair_delimiter.join(parts))
                else:
                    lines.append(json.dumps(attrs))

            if not lines:
                return

        except Exception as e:
            print(f"[LIVE_DATA] DEBUG: Failed to build mock data: {e}")
            return

        interval = float(mock.get("interval_seconds", 1.0))
        loop = bool(mock.get("loop", True))

        slot.headers = None
        slot.last_raw = ""

        w = MockDataWorker(
            lines=lines,
            parser_config=slot.parser_config,
            interval_seconds=interval,
            loop=loop,
        )
        w.data_received.connect(lambda d, sid=slot.slot_id: self.on_data_received(sid, d))
        w.status_changed.connect(lambda s, sid=slot.slot_id: self.on_status_changed(sid, s))
        w.headers_received.connect(lambda h, sid=slot.slot_id: self.on_headers_received(sid, h))
        w.raw_data_received.connect(lambda r, sid=slot.slot_id: self.on_raw_data_received(sid, r))

        slot.mock_worker = w
        w.start()
        self.set_active_slot(slot.slot_id)

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
        if not self.active_slot_id:
            return
        cfg = {
            "slot_id": self.active_slot_id,
            "slot_name": self.slots[self.active_slot_id].name,
            "host": self.host_edit.text(),
            "port": int(self.port_edit.text()),
            "lat_field": self.lat_field_edit.text(),
            "lon_field": self.lon_field_edit.text(),
            "persist": self.persist_chk.isChecked(),
            "parser": {
                "kind": self.slots[self.active_slot_id].parser_config.kind,
                "csv_delimiter": self.slots[self.active_slot_id].parser_config.csv_delimiter,
                "csv_quotechar": self.slots[self.active_slot_id].parser_config.csv_quotechar,
                "csv_fixed_headers": self.slots[self.active_slot_id].parser_config.csv_fixed_headers,
                "kv_pair_delimiter": self.slots[self.active_slot_id].parser_config.kv_pair_delimiter,
                "kv_kv_delimiter": self.slots[self.active_slot_id].parser_config.kv_kv_delimiter,
                "kv_strip_whitespace": self.slots[self.active_slot_id].parser_config.kv_strip_whitespace,
                "json_require_object": self.slots[self.active_slot_id].parser_config.json_require_object,
                "regex_pattern": self.slots[self.active_slot_id].parser_config.regex_pattern,
                "regex_flags": self.slots[self.active_slot_id].parser_config.regex_flags,
            },
        }
        self.connect_slot(cfg)

        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)

    def disconnect_server(self):
        """Disconnect from the data server."""
        if not self.active_slot_id:
            return
        self.disconnect_slot(self.active_slot_id)
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.status_label.setText("Disconnected")
        self.connected_state_changed.emit(False)

    def start_mock(self):
        """Start mock playback from the selected project layer."""
        if not self.active_slot_id:
            return
        slot = self.slots.get(self.active_slot_id)
        if not slot:
            return

        if slot.connected:
            self.disconnect_slot(slot.slot_id)

        if slot.mock_worker:
            return

        layer_id = self.mock_layer_combo.currentData()
        if not layer_id:
            QMessageBox.warning(self, "Mock Playback", "Please select an input layer.")
            return

        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            QMessageBox.warning(self, "Mock Playback", "Selected layer is not available.")
            return

        # Build mock lines in the main thread (avoid reading QGIS layers inside QThread).
        try:
            import csv
            import io
            import json

            fields = [f.name() for f in layer.fields()]
            lines = []

            kind = self.parser_config.kind
            if kind == FORMAT_CSV_HEADER:
                out = io.StringIO()
                writer = csv.writer(out, delimiter=self.parser_config.csv_delimiter, quotechar=self.parser_config.csv_quotechar)
                writer.writerow(fields)
                lines.append(out.getvalue().rstrip("\r\n"))

            for feat in layer.getFeatures():
                attrs = {name: feat[name] for name in fields}
                if kind == FORMAT_CSV_HEADER:
                    out = io.StringIO()
                    writer = csv.writer(out, delimiter=self.parser_config.csv_delimiter, quotechar=self.parser_config.csv_quotechar)
                    writer.writerow([attrs.get(n, "") for n in fields])
                    lines.append(out.getvalue().rstrip("\r\n"))
                elif kind == "csv_fixed":
                    cols = self.parser_config.csv_fixed_headers or fields
                    out = io.StringIO()
                    writer = csv.writer(out, delimiter=self.parser_config.csv_delimiter, quotechar=self.parser_config.csv_quotechar)
                    writer.writerow([attrs.get(n, "") for n in cols])
                    lines.append(out.getvalue().rstrip("\r\n"))
                elif kind == "kv":
                    parts = []
                    for k, v in attrs.items():
                        parts.append(f"{k}{self.parser_config.kv_kv_delimiter}{v}")
                    lines.append(self.parser_config.kv_pair_delimiter.join(parts))
                elif kind == "json":
                    lines.append(json.dumps(attrs))
                elif kind == "regex":
                    # Regex parsing expects a specific pattern; default to JSON to avoid confusion.
                    lines.append(json.dumps(attrs))
                else:
                    lines.append(json.dumps(attrs))

            if not lines:
                QMessageBox.warning(self, "Mock Playback", "No rows found in selected layer.")
                return

        except Exception as e:
            QMessageBox.critical(self, "Mock Playback", f"Failed to build mock data: {e}")
            return

        slot.headers = None
        w = MockDataWorker(
            lines=lines,
            parser_config=slot.parser_config,
            interval_seconds=self.mock_interval_spin.value(),
            loop=self.mock_loop_chk.isChecked(),
        )
        w.data_received.connect(lambda d, sid=slot.slot_id: self.on_data_received(sid, d))
        w.status_changed.connect(lambda s, sid=slot.slot_id: self.on_status_changed(sid, s))
        w.headers_received.connect(lambda h, sid=slot.slot_id: self.on_headers_received(sid, h))
        w.raw_data_received.connect(lambda r, sid=slot.slot_id: self.on_raw_data_received(sid, r))
        slot.mock_worker = w
        w.start()

        self.mock_start_btn.setEnabled(False)
        self.mock_stop_btn.setEnabled(True)

    def stop_mock(self):
        """Stop mock playback if running."""
        if not self.active_slot_id:
            return
        slot = self.slots.get(self.active_slot_id)
        if not slot or not slot.mock_worker:
            return
        try:
            slot.mock_worker.stop()
        except Exception:
            pass
        slot.mock_worker = None

        if hasattr(self, "mock_start_btn"):
            self.mock_start_btn.setEnabled(True)
        if hasattr(self, "mock_stop_btn"):
            self.mock_stop_btn.setEnabled(False)

    def on_status_changed(self, slot_id: str, status: str):
        """Handle status changes from worker for a slot."""
        slot = self.slots.get(slot_id)
        if not slot:
            return

        slot.status = status
        if "Connected" in status or "Mock: Running" in status or status.startswith("Mock"):
            slot.connected = True
        if "Disconnected" in status:
            slot.connected = False

        if self.active_slot_id == slot_id:
            self.status_label.setText(status)
            if "Connected" in status:
                self.connected_state_changed.emit(True)
            elif "Disconnected" in status or "Error" in status:
                self.connect_btn.setEnabled(True)
                self.disconnect_btn.setEnabled(False)
                self.connected_state_changed.emit(False)

    def on_headers_received(self, slot_id: str, headers: list):
        """Handle headers from data stream for a slot."""
        slot = self.slots.get(slot_id)
        if not slot or not headers:
            return

        if not slot.headers:
            slot.headers = list(headers)
        else:
            existing = set(slot.headers)
            for h in headers:
                if h not in existing:
                    slot.headers.append(h)
                    existing.add(h)

        # Create/update map layer for this slot
        if not self._is_layer_valid(slot.layer):
            slot.layer = None
            self._create_layer_for_slot(slot)
        else:
            try:
                provider = slot.layer.dataProvider()
                existing_fields = {f.name() for f in slot.layer.fields()}
                new_fields = [QgsField(h, QVariant.String) for h in (slot.headers or []) if h not in existing_fields]
                if new_fields:
                    provider.addAttributes(new_fields)
                    slot.layer.updateFields()
            except Exception as e:
                print(f"[LIVE_DATA] DEBUG: Failed to update layer fields for slot '{slot.name}': {e}")

        if self.active_slot_id == slot_id:
            self.headers = slot.headers
            self.headers_text.setText('\n'.join(slot.headers or []))
            print(f"Received headers ({len(slot.headers or [])} fields) for slot '{slot.name}': {slot.headers}")
            self.headers_received.emit(slot.headers)

    def on_raw_data_received(self, slot_id: str, line: str):
        slot = self.slots.get(slot_id)
        if not slot:
            return
        slot.last_raw = line
        if self.active_slot_id == slot_id:
            self.received_text.setText(line)

    def on_data_received(self, slot_id: str, data_dict: dict):
        """
        Handle new data from stream.
        Updates map points and emits signal for card display widget.
        """
        print(f"Received data: {data_dict}")
        
        slot = self.slots.get(slot_id)
        if not slot:
            return

        # If nothing is actively producing data for this slot, ignore any queued signals.
        # This prevents layer recreation after Stop/Disconnect when late signals arrive.
        if slot.worker is None and slot.mock_worker is None:
            return

        # Check if layer still exists; recreate if needed
        if not self._is_layer_valid(slot.layer):
            slot.layer = None
            if slot.headers:
                self._create_layer_for_slot(slot)

        if not self._is_layer_valid(slot.layer):
            return

        # Clear if not persisting
        try:
            persist = bool(slot.persist)
            if not persist:
                slot.layer.dataProvider().truncate()
        except RuntimeError:
            slot.layer = None
            return

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
            if slot.headers:
                feat.setAttributes([data_dict.get(h, '') for h in (slot.headers or [])])
            slot.layer.dataProvider().addFeature(feat)
            slot.layer.updateExtents()
            # Trigger layer repaint and canvas refresh
            slot.layer.triggerRepaint()
            self.iface.mapCanvas().refresh()
            print("Feature added successfully")
        except (ValueError, KeyError) as e:
            self.status_label.setText(f"Error parsing data: {str(e)}")
            print(f"Error parsing data: {str(e)}")
        except RuntimeError as e:
            # Handle case where layer was deleted between checks
            print(f"Layer error (layer may have been deleted): {str(e)}")
            slot.layer = None
            self.status_label.setText("Error: Live data layer was deleted. Reconnect to create a new layer.")
            return
        
        # Emit signal for cards display widget to update (active slot only)
        if self.active_slot_id == slot_id:
            self.data_received_raw.emit(data_dict)

        # Update overlays
        self.update_overlays(slot, data_dict)

    def create_layer(self):
        """Create layer for active slot (backwards compatible)."""
        if not self.active_slot_id:
            return
        slot = self.slots.get(self.active_slot_id)
        if not slot:
            return
        self._create_layer_for_slot(slot)

    def _create_layer_for_slot(self, slot: _LiveDataSlot):
        if not slot.headers:
            return
        if slot.layer is not None and not self._is_layer_valid(slot.layer):
            slot.layer = None
        layer_name = f"Live Data Points - {slot.name}" if slot.name else "Live Data Points"
        slot.layer = QgsVectorLayer(f"Point?crs={self.project_crs.authid()}", layer_name, "memory")
        provider = slot.layer.dataProvider()
        fields = [QgsField(h, QVariant.String) for h in (slot.headers or [])]
        provider.addAttributes(fields)
        slot.layer.updateFields()
        QgsProject.instance().addMapLayer(slot.layer)

    def on_layers_removed(self, layer_ids):
        """Handle when layers are removed from the project."""
        for slot in list(self.slots.values()):
            layer = slot.layer
            if layer is None:
                continue

            # Never evaluate truthiness of SIP-wrapped objects; it can raise if deleted.
            try:
                if sip.isdeleted(layer):
                    slot.layer = None
                    continue
            except Exception:
                slot.layer = None
                continue

            try:
                layer_id = layer.id()
            except RuntimeError:
                slot.layer = None
                continue

            if layer_id in layer_ids:
                print(f"Live Data layer for slot '{slot.name}' was removed from project")
                slot.layer = None
                if self.active_slot_id == slot.slot_id:
                    self.status_label.setText("Live data layer deleted.")

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
            
            # Stop all slot workers
            for sid in list(self.slots.keys()):
                try:
                    self.disconnect_slot(sid)
                except Exception:
                    pass
            
            # Preserve layers (don't delete) but clear references
            for slot in self.slots.values():
                if slot.layer and self._is_layer_valid(slot.layer):
                    try:
                        print(f"[LIVE_DATA] DEBUG: Preserving layer '{slot.layer.name()}' in project")
                    except Exception:
                        pass
                slot.layer = None

            self.headers = None
            self.active_slot_id = None
            
        except Exception as e:
            print(f"[LIVE_DATA] DEBUG: Error during force_cleanup: {e}")
