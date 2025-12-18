from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QCheckBox, QGroupBox, QFormLayout, QTextEdit
)
from qgis.PyQt.QtCore import QThread, pyqtSignal, Qt
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsField,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform
)
from qgis.PyQt.QtCore import QVariant

import socket
import csv


class LiveDataWorker(QThread):
    data_received = pyqtSignal(dict)
    status_changed = pyqtSignal(str)
    headers_received = pyqtSignal(list)
    raw_data_received = pyqtSignal(str)

    def __init__(self, host, port, lat_field, lon_field, persist):
        super().__init__()
        self.host = host
        self.port = port
        self.lat_field = lat_field
        self.lon_field = lon_field
        self.persist = persist
        self.running = True
        self.headers = []

    def get_persist(self):
        """Return the persist setting for this worker."""
        return self.persist

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.host, self.port))
            self.status_changed.emit("Connected")
            buffer = ""

            # Receive headers first
            while '\n' not in buffer and self.running:
                data = sock.recv(1024)
                if not data:
                    break
                buffer += data.decode('utf-8')
            
            if '\n' in buffer:
                header_line, buffer = buffer.split('\n', 1)
                header_reader = csv.reader([header_line])
                self.headers = list(header_reader)[0]
                self.headers_received.emit(self.headers)
            else:
                self.status_changed.emit("Error: No headers received")
                return

            # Then receive data
            while self.running:
                data = sock.recv(1024)
                if not data:
                    break
                buffer += data.decode('utf-8')
                lines = buffer.split('\n')
                buffer = lines[-1]  # Keep incomplete line
                for line in lines[:-1]:
                    if line.strip():
                        self.raw_data_received.emit(line)
                        # Parse CSV line
                        reader = csv.reader([line])
                        row = next(reader)
                        data_dict = dict(zip(self.headers, row))
                        print(f"Parsed line: {line}, row: {row}, data_dict: {data_dict}")
                        self.data_received.emit(data_dict)
        except Exception as e:
            self.status_changed.emit(f"Error: {str(e)}")
        finally:
            self.status_changed.emit("Disconnected")

    def stop(self):
        self.running = False
        self.quit()
        self.wait()


class LiveDataDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Live Data", parent)
        self.iface = iface
        self.setObjectName("LiveDataDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        self.layer = None
        self.worker = None
        self.connected = False
        self.headers = None
        self.project_crs = QgsProject.instance().crs()
        if not self.project_crs.isValid():
            self.project_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        self.setup_ui()

    def setup_ui(self):
        self.widget = QWidget()
        self.setWidget(self.widget)
        layout = QVBoxLayout(self.widget)

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

        # Received data display
        received_group = QGroupBox("Received Data")
        received_layout = QVBoxLayout(received_group)
        
        received_layout.addWidget(QLabel("Headers:"))
        self.headers_text = QTextEdit()
        self.headers_text.setMaximumHeight(100)
        self.headers_text.setReadOnly(True)
        received_layout.addWidget(self.headers_text)
        
        received_layout.addWidget(QLabel("Latest Data String:"))
        self.received_text = QTextEdit()
        self.received_text.setMaximumHeight(60)
        self.received_text.setReadOnly(True)
        received_layout.addWidget(self.received_text)
        
        layout.addWidget(received_group)

        layout.addStretch()

    def connect_server(self):
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
        if self.worker:
            # Disconnect signals before stopping to prevent data_received callbacks
            self.worker.data_received.disconnect(self.on_data_received)
            self.worker.stop()
            self.worker = None
        self.connected = False
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.status_label.setText("Disconnected")

    def on_status_changed(self, status):
        self.status_label.setText(status)
        if "Connected" in status:
            self.connected = True
        elif "Disconnected" in status or "Error" in status:
            self.connected = False
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)

    def on_headers_received(self, headers):
        self.headers = headers
        self.headers_text.setText('\n'.join(headers))
        print(f"Received headers ({len(headers)} fields): {headers}")
        self.create_layer()

    def on_raw_data_received(self, line):
        self.received_text.setText(line)

    def on_data_received(self, data_dict):
        print(f"Received data: {data_dict}")
        if not self.layer or not self.worker:
            return

        # Clear if not persisting (use worker's persist setting, not checkbox)
        if not self.worker.get_persist():
            self.layer.dataProvider().truncate()

        # Add feature
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

    def create_layer(self):
        if not self.headers:
            return
        self.layer = QgsVectorLayer(f"Point?crs={self.project_crs.authid()}", "Live Data Points", "memory")
        provider = self.layer.dataProvider()
        fields = [QgsField(h, QVariant.String) for h in self.headers]
        provider.addAttributes(fields)
        self.layer.updateFields()
        QgsProject.instance().addMapLayer(self.layer)