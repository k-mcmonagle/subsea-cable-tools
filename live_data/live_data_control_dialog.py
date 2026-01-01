"""
Live Data Control Dialog

Unified control center combining Connection management and Window visibility controls.
This single window replaces both the Live Data Manager Dialog and the Live Data Connection Dock Widget.

Features:
- Connection tab: Host, port, connect/disconnect controls
- Windows tab: Checkboxes to control visibility of data windows
- Connection status display
- Streamlined UI for easier management
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QCheckBox, QLabel, QFrame, QLineEdit, QTabWidget,
    QWidget, QMessageBox, QComboBox, QDoubleSpinBox, QGridLayout
)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QFont
from typing import Optional

import uuid
import json

from .message_parser import (
    MessageFormatConfig,
    SUPPORTED_FORMATS,
    FORMAT_CSV_HEADER,
    FORMAT_CSV_FIXED,
    FORMAT_KV,
    FORMAT_JSON,
    FORMAT_REGEX,
)


class LiveDataControlDialog(QDialog):
    """
    Unified control dialog for Live Data system.
    
    Replaces both the manager dialog and connection dock widget.
    Provides two tabs:
    1. Connection - configure and manage data server connection
    2. Windows - control visibility of data windows
    
    Signals:
        show_cards_widget: Show Live Data Cards window
        show_plots_widget: Show Live Data Plots window
        show_tables_widget: Show Live Data Tables window
        hide_cards_widget: Hide Live Data Cards window
        hide_plots_widget: Hide Live Data Plots window
        hide_tables_widget: Hide Live Data Tables window
        connect_requested: User clicked Connect button (host, port emitted)
        disconnect_requested: User clicked Disconnect button
    """
    
    # Signals for showing/hiding data windows
    show_cards_widget = pyqtSignal()
    show_plots_widget = pyqtSignal()
    show_tables_widget = pyqtSignal()
    
    hide_cards_widget = pyqtSignal()
    hide_plots_widget = pyqtSignal()
    hide_tables_widget = pyqtSignal()
    
    # Signals for connection events
    connect_requested = pyqtSignal(dict)  # stream config
    disconnect_requested = pyqtSignal(dict)

    # Signals for mock/testing
    mock_start_requested = pyqtSignal(dict)
    mock_stop_requested = pyqtSignal(dict)
    
    # Signal for overlays config
    overlays_config_changed = pyqtSignal(dict)  # {slot_id, configs}

    # Active slot (drives Cards/Plots/Tables)
    active_slot_changed = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Live Data Control")
        self.setModal(False)
        # With slots + format config, 450px is tight and squashes inputs.
        self.setMinimumWidth(650)
        self.setMinimumHeight(350)
        
        # Track widget visibility state
        self.visible_widgets = {
            'cards': False,
            'plots': False,
            'tables': False
        }
        
        # Remember position/size for minimize/restore
        self.saved_geometry = None
        
        # Connection state
        self.connected = False
        
        # Track available data fields from stream
        self.available_fields = []

        # Slot state (stored in-dialog so switching slots restores UI values)
        self.slots = {}
        self.current_slot_id = None
        self.active_slot_id = None
        
        # Overlays configuration
        self.overlays_config = []
        
        # Clear any corrupted Qt state from previous versions
        self._clear_corrupted_state()
        
        self.setup_ui()

        # Load saved settings (or create defaults)
        self._load_settings()
        if not self.slots:
            default_id = str(uuid.uuid4())
            self.slots[default_id] = {"slot_id": default_id, "slot_name": "Default"}
            self.current_slot_id = default_id
            self.active_slot_id = default_id
        if not self.current_slot_id:
            self.current_slot_id = next(iter(self.slots.keys()), None)
        if not self.active_slot_id:
            self.active_slot_id = self.current_slot_id

        self._refresh_slot_combo()
        if self.current_slot_id:
            idx = self.slot_combo.findData(self.current_slot_id)
            if idx >= 0:
                self.slot_combo.setCurrentIndex(idx)
            self._load_slot_to_ui(self.current_slot_id)
        self._update_active_slot_label()
    
    def _clear_corrupted_state(self):
        """Clear any corrupted window state from Qt settings that might cause crashes.
        
        This is necessary when UI structure changes (like removing the ship outline tab).
        Qt tries to restore old state which can cause access violations.
        """
        try:
            from qgis.PyQt.QtCore import QSettings
            settings = QSettings()
            
            # Clear Qt window state for this specific dialog class
            # This prevents restoreState() from trying to use corrupted tab indices
            state_key = f"{self.__class__.__name__}/geometry"
            state_key2 = f"{self.__class__.__name__}/windowState"
            
            if settings.contains(state_key):
                settings.remove(state_key)
            if settings.contains(state_key2):
                settings.remove(state_key2)
                
            settings.sync()
        except Exception as e:
            # If we can't clear settings, that's okay - just continue
            print(f"DEBUG: Could not clear corrupted Qt state: {e}")
    
    def setup_ui(self):
        """Build the control dialog UI with tabs."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        
        # Tab widget
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        
        # --- CONNECTION TAB ---
        conn_tab = QWidget()
        conn_layout = QVBoxLayout(conn_tab)
        conn_layout.setSpacing(12)

        # Slot selector
        slot_group = QGroupBox("Input Slot")
        slot_grid = QGridLayout(slot_group)

        self.slot_combo = QComboBox()
        self.slot_combo.currentIndexChanged.connect(self.on_slot_changed)

        self.slot_name_edit = QLineEdit()
        self.slot_name_edit.setPlaceholderText("e.g. Ship GPS")
        self.slot_name_edit.textEdited.connect(self.on_slot_name_edited)

        self.add_slot_btn = QPushButton("Add")
        self.add_slot_btn.clicked.connect(self.on_add_slot)
        self.remove_slot_btn = QPushButton("Remove")
        self.remove_slot_btn.clicked.connect(self.on_remove_slot)
        self.set_active_slot_btn = QPushButton("Set Active")
        self.set_active_slot_btn.clicked.connect(self.on_set_active_slot)
        self.active_slot_label = QLabel("")

        slot_grid.addWidget(QLabel("Slot:"), 0, 0)
        slot_grid.addWidget(self.slot_combo, 0, 1, 1, 3)
        slot_grid.addWidget(QLabel("Name:"), 1, 0)
        slot_grid.addWidget(self.slot_name_edit, 1, 1, 1, 3)
        slot_grid.addWidget(self.add_slot_btn, 2, 1)
        slot_grid.addWidget(self.remove_slot_btn, 2, 2)
        slot_grid.addWidget(self.set_active_slot_btn, 2, 3)
        slot_grid.addWidget(QLabel("Active:"), 3, 0)
        slot_grid.addWidget(self.active_slot_label, 3, 1, 1, 3)

        conn_layout.addWidget(slot_group)
        
        # Connection settings
        conn_group = QGroupBox("Connection Settings")
        conn_form = QFormLayout(conn_group)
        conn_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        
        self.host_edit = QLineEdit("localhost")
        self.port_edit = QLineEdit("12345")
        self.port_edit.setValidator(self._get_int_validator())
        
        conn_form.addRow("Host:", self.host_edit)
        conn_form.addRow("Port:", self.port_edit)
        
        conn_layout.addWidget(conn_group)
        
        # Control buttons
        button_layout = QHBoxLayout()
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.on_connect_clicked)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.on_disconnect_clicked)
        self.disconnect_btn.setEnabled(False)
        
        button_layout.addWidget(self.connect_btn)
        button_layout.addWidget(self.disconnect_btn)
        button_layout.addStretch()
        conn_layout.addLayout(button_layout)
        
        # Data settings
        data_group = QGroupBox("Data Configuration")
        data_layout = QFormLayout(data_group)
        data_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        # Use editable combos so users can either pick from headers or type.
        self.lat_field_combo = QComboBox()
        self.lat_field_combo.setEditable(True)
        self.lat_field_combo.addItem("Lat_dd")

        self.lon_field_combo = QComboBox()
        self.lon_field_combo.setEditable(True)
        self.lon_field_combo.addItem("Lon_dd")

        data_layout.addRow("Latitude Field:", self.lat_field_combo)
        data_layout.addRow("Longitude Field:", self.lon_field_combo)
        
        self.persist_chk = QCheckBox("Persist Points on Map")
        self.persist_chk.setChecked(True)
        self.persist_chk.setToolTip("If checked, all received points remain on map. If unchecked, only latest point shown.")
        data_layout.addRow(self.persist_chk)
        
        conn_layout.addWidget(data_group)

        # Message format settings
        fmt_group = QGroupBox("Message Format")
        fmt_layout = QFormLayout(fmt_group)
        fmt_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.format_combo = QComboBox()
        self.format_combo.addItems([
            FORMAT_CSV_HEADER,
            FORMAT_CSV_FIXED,
            FORMAT_KV,
            FORMAT_JSON,
            FORMAT_REGEX,
        ])
        self.format_combo.setCurrentText(FORMAT_CSV_HEADER)
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        fmt_layout.addRow("Format:", self.format_combo)

        # CSV options
        self.csv_delim_edit = QLineEdit(",")
        self.csv_delim_edit.setMaximumWidth(60)
        fmt_layout.addRow("CSV delimiter:", self.csv_delim_edit)

        self.csv_fixed_headers_edit = QLineEdit("")
        self.csv_fixed_headers_edit.setPlaceholderText("col1,col2,col3")
        fmt_layout.addRow("CSV fixed columns:", self.csv_fixed_headers_edit)

        # KV options
        self.kv_pair_delim_edit = QLineEdit(",")
        self.kv_pair_delim_edit.setMaximumWidth(60)
        fmt_layout.addRow("KV pair delimiter:", self.kv_pair_delim_edit)

        self.kv_kv_delim_edit = QLineEdit("=")
        self.kv_kv_delim_edit.setMaximumWidth(60)
        fmt_layout.addRow("KV key/value delimiter:", self.kv_kv_delim_edit)

        # Regex options
        self.regex_pattern_edit = QLineEdit("")
        self.regex_pattern_edit.setPlaceholderText(r"e.g. Lat=(?P<Lat_dd>-?\d+\.\d+),Lon=(?P<Lon_dd>-?\d+\.\d+)")
        fmt_layout.addRow("Regex pattern:", self.regex_pattern_edit)

        conn_layout.addWidget(fmt_group)
        
        # Status display
        status_group = QGroupBox("Connection Status")
        status_layout = QFormLayout(status_group)
        status_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        
        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("color: #ff6666; font-weight: bold;")
        status_layout.addRow("Status:", self.status_label)
        
        self.connection_info_label = QLabel("—")
        status_layout.addRow("Connected To:", self.connection_info_label)
        
        conn_layout.addWidget(status_group)
        conn_layout.addStretch()
        
        self.tabs.addTab(conn_tab, "Connection")
        
        # --- WINDOWS TAB ---
        windows_tab = QWidget()
        windows_layout = QVBoxLayout(windows_tab)
        windows_layout.setSpacing(12)
        
        # Info
        info_label = QLabel("Select which data windows to display:")
        info_font = QFont()
        info_font.setPointSize(10)
        info_label.setFont(info_font)
        windows_layout.addWidget(info_label)
        
        # Window toggles
        windows_group = QGroupBox("Data Windows")
        windows_form = QVBoxLayout(windows_group)
        windows_form.setSpacing(8)
        
        # Cards checkbox
        cards_layout = QHBoxLayout()
        self.cards_checkbox = QCheckBox("Live Data Cards")
        self.cards_checkbox.setChecked(False)
        self.cards_checkbox.setToolTip("Display single values in card grid layout")
        self.cards_checkbox.stateChanged.connect(self.on_cards_checkbox_changed)
        cards_layout.addWidget(self.cards_checkbox)
        cards_layout.addStretch()
        windows_form.addLayout(cards_layout)
        
        # Plots checkbox
        plots_layout = QHBoxLayout()
        self.plots_checkbox = QCheckBox("Live Data Plots")
        self.plots_checkbox.setChecked(False)
        self.plots_checkbox.setToolTip("Display time-series data as real-time graphs")
        self.plots_checkbox.stateChanged.connect(self.on_plots_checkbox_changed)
        plots_layout.addWidget(self.plots_checkbox)
        plots_layout.addStretch()
        windows_form.addLayout(plots_layout)
        
        # Tables checkbox
        tables_layout = QHBoxLayout()
        self.tables_checkbox = QCheckBox("Live Data Tables")
        self.tables_checkbox.setChecked(False)
        self.tables_checkbox.setToolTip("Display multiple fields in tabular format")
        self.tables_checkbox.stateChanged.connect(self.on_tables_checkbox_changed)
        tables_layout.addWidget(self.tables_checkbox)
        tables_layout.addStretch()
        windows_form.addLayout(tables_layout)
        
        windows_layout.addWidget(windows_group)
        
        windows_layout.addStretch()
        
        self.tabs.addTab(windows_tab, "Windows")

        # --- MOCK/TEST TAB ---
        mock_tab = QWidget()
        mock_layout = QVBoxLayout(mock_tab)
        mock_layout.setSpacing(12)

        mock_info = QLabel(
            "Replay a table layer as mock live data. "
            "This exercises the same string parser and UI pipeline as TCP data."
        )
        mock_info.setWordWrap(True)
        mock_layout.addWidget(mock_info)

        mock_group = QGroupBox("Mock Playback")
        mock_form = QFormLayout(mock_group)
        mock_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.mock_layer_combo = QComboBox()
        refresh_btn = QPushButton("Refresh Layers")
        refresh_btn.clicked.connect(self._refresh_mock_layers)
        layer_row = QHBoxLayout()
        layer_row.addWidget(self.mock_layer_combo, 1)
        layer_row.addWidget(refresh_btn)
        mock_form.addRow("Input Layer:", layer_row)

        self.mock_interval_spin = QDoubleSpinBox()
        self.mock_interval_spin.setRange(0.05, 60.0)
        self.mock_interval_spin.setSingleStep(0.1)
        self.mock_interval_spin.setValue(1.0)
        mock_form.addRow("Interval (s):", self.mock_interval_spin)

        self.mock_loop_chk = QCheckBox("Loop")
        self.mock_loop_chk.setChecked(True)
        mock_form.addRow(self.mock_loop_chk)

        btn_row = QHBoxLayout()
        self.mock_start_btn = QPushButton("Start Mock")
        self.mock_start_btn.clicked.connect(self.on_mock_start_clicked)
        self.mock_stop_btn = QPushButton("Stop Mock")
        self.mock_stop_btn.clicked.connect(self.on_mock_stop_clicked)
        btn_row.addWidget(self.mock_start_btn)
        btn_row.addWidget(self.mock_stop_btn)
        btn_row.addStretch()
        mock_form.addRow(btn_row)

        mock_layout.addWidget(mock_group)
        mock_layout.addStretch()

        self.tabs.addTab(mock_tab, "Mock/Test")
        
        # --- HELP TAB ---
        help_tab = QWidget()
        help_layout = QVBoxLayout(help_tab)
        help_layout.setSpacing(12)
        
        # Help content
        help_text = QLabel(
            "<b>Live Data Tool Overview</b><br><br>"
            "This tool connects to a live data server to display real-time information "
            "from subsea operations in multiple formats.<br><br>"
            "<b>How to Use:</b><br>"
            "1. Configure connection settings (host/port) in the Connection tab<br>"
            "2. Click Connect to establish the data stream<br>"
            "3. Select which data windows to display in the Windows tab<br>"
            "4. Monitor real-time data in the chosen display formats<br><br>"
            "<b>Data Windows:</b><br>"
            "• <b>Cards:</b> Single values in a grid layout<br>"
            "• <b>Plots:</b> Time-series graphs for trending data<br>"
            "• <b>Tables:</b> Tabular view of multiple data fields<br><br>"
            "<b>Tips:</b><br>"
            "• Windows can be rearranged by dragging title bars<br>"
            "• Data persists on map unless persistence is disabled<br>"
            "• Close this dialog while keeping data windows active"
        )
        help_text.setWordWrap(True)
        help_text.setTextFormat(Qt.RichText)
        help_text.setStyleSheet("color: #333333;")
        help_layout.addWidget(help_text)
        
        help_layout.addStretch()
        
        self.tabs.addTab(help_tab, "Help")
        
        # --- OVERLAYS TAB ---
        overlays_tab = QWidget()
        overlays_layout = QVBoxLayout(overlays_tab)
        overlays_layout.setSpacing(12)
        
        # Info
        overlays_info = QLabel("Configure geometry overlays to display on the map at live data positions:")
        overlays_info.setWordWrap(True)
        overlays_layout.addWidget(overlays_info)
        
        # Overlay config group
        overlay_group = QGroupBox("Overlay Configuration")
        overlay_form = QFormLayout(overlay_group)
        overlay_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        
        # DXF file
        dxf_layout = QHBoxLayout()
        self.dxf_edit = QLineEdit()
        self.dxf_edit.setPlaceholderText("Select DXF file...")
        dxf_browse_btn = QPushButton("Browse...")
        dxf_browse_btn.clicked.connect(self.on_dxf_browse)
        dxf_layout.addWidget(self.dxf_edit)
        dxf_layout.addWidget(dxf_browse_btn)
        overlay_form.addRow("DXF File:", dxf_layout)
        
        # Field mappings
        self.lat_combo = QComboBox()
        self.lon_combo = QComboBox()
        self.heading_combo = QComboBox()
        overlay_form.addRow("Latitude Field:", self.lat_combo)
        overlay_form.addRow("Longitude Field:", self.lon_combo)
        overlay_form.addRow("Heading Field:", self.heading_combo)
        
        # Parameters
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.0001, 1000.0)
        self.scale_spin.setValue(1.0)
        self.scale_spin.setSingleStep(0.001)
        self.scale_spin.setDecimals(4)
        overlay_form.addRow("Scale:", self.scale_spin)
        
        self.crp_x_spin = QDoubleSpinBox()
        self.crp_x_spin.setRange(-10000.0, 10000.0)
        self.crp_x_spin.setValue(0.0)
        overlay_form.addRow("CRP Offset X:", self.crp_x_spin)
        
        self.crp_y_spin = QDoubleSpinBox()
        self.crp_y_spin.setRange(-10000.0, 10000.0)
        self.crp_y_spin.setValue(0.0)
        overlay_form.addRow("CRP Offset Y:", self.crp_y_spin)
        
        self.rot_offset_spin = QDoubleSpinBox()
        self.rot_offset_spin.setRange(-360.0, 360.0)
        self.rot_offset_spin.setValue(0.0)
        overlay_form.addRow("Rotation Offset (°):", self.rot_offset_spin)
        
        overlays_layout.addWidget(overlay_group)
        
        # Buttons
        overlay_buttons = QHBoxLayout()
        self.apply_overlay_btn = QPushButton("Apply Overlay")
        self.apply_overlay_btn.clicked.connect(self.on_apply_overlay)
        self.clear_overlay_btn = QPushButton("Clear Overlay")
        self.clear_overlay_btn.clicked.connect(self.on_clear_overlay)
        overlay_buttons.addWidget(self.apply_overlay_btn)
        overlay_buttons.addWidget(self.clear_overlay_btn)
        overlay_buttons.addStretch()
        overlays_layout.addLayout(overlay_buttons)
        
        overlays_layout.addStretch()
        
        self.tabs.addTab(overlays_tab, "Overlays")
        
        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep2)
        
        # Bottom buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.close_btn = QPushButton("Close")
        self.close_btn.setMaximumWidth(100)
        self.close_btn.clicked.connect(self.close)
        button_layout.addWidget(self.close_btn)
        
        layout.addLayout(button_layout)

        self._on_format_changed(self.format_combo.currentText())
        self._refresh_mock_layers()
    
    def _get_int_validator(self):
        """Get validator for integer input (port number)."""
        from qgis.PyQt.QtGui import QIntValidator
        return QIntValidator(1, 65535)
    
    # Connection tab handlers
    def on_connect_clicked(self):
        """Handle Connect button click."""
        try:
            self._save_ui_to_slot(self.current_slot_id)
            cfg = self.get_stream_config()
            self.connect_requested.emit(cfg)
            self._save_settings()
        except ValueError:
            QMessageBox.warning(self, "Invalid Port", "Port must be a number between 1 and 65535")

    def on_mock_start_clicked(self):
        self._save_ui_to_slot(self.current_slot_id)
        cfg = self.get_stream_config()
        layer_id = self.mock_layer_combo.currentData()
        if not layer_id:
            QMessageBox.warning(self, "Mock Playback", "Please select an input layer.")
            return

        self.mock_start_requested.emit(
            {
                **cfg,
                "mock": {
                    "layer_id": layer_id,
                    "interval_seconds": float(self.mock_interval_spin.value()),
                    "loop": bool(self.mock_loop_chk.isChecked()),
                },
            }
        )
        self._save_settings()

    def on_mock_stop_clicked(self):
        if not self.current_slot_id:
            return
        self.mock_stop_requested.emit({"slot_id": self.current_slot_id})

    def get_stream_config(self) -> dict:
        """Collect a unified stream configuration dict from the UI."""
        host = self.host_edit.text().strip()
        port = int(self.port_edit.text())

        parser_cfg = MessageFormatConfig(kind=self.format_combo.currentText().strip() or FORMAT_CSV_HEADER)
        parser_cfg.csv_delimiter = (self.csv_delim_edit.text() or ",")[:1]
        parser_cfg.kv_pair_delimiter = (self.kv_pair_delim_edit.text() or ",")[:1]
        parser_cfg.kv_kv_delimiter = (self.kv_kv_delim_edit.text() or "=")[:1]
        parser_cfg.regex_pattern = self.regex_pattern_edit.text() or ""

        fixed_headers_txt = self.csv_fixed_headers_edit.text().strip()
        if fixed_headers_txt:
            parser_cfg.csv_fixed_headers = [h.strip() for h in fixed_headers_txt.split(",") if h.strip()]

        return {
            "slot_id": self.current_slot_id,
            "slot_name": (self.slots.get(self.current_slot_id, {}) or {}).get("slot_name", "Input"),
            "host": host,
            "port": port,
            "lat_field": self.lat_field_combo.currentText().strip(),
            "lon_field": self.lon_field_combo.currentText().strip(),
            "persist": bool(self.persist_chk.isChecked()),
            "parser": {
                "kind": parser_cfg.kind,
                "csv_delimiter": parser_cfg.csv_delimiter,
                "csv_quotechar": parser_cfg.csv_quotechar,
                "csv_fixed_headers": parser_cfg.csv_fixed_headers,
                "kv_pair_delimiter": parser_cfg.kv_pair_delimiter,
                "kv_kv_delimiter": parser_cfg.kv_kv_delimiter,
                "kv_strip_whitespace": parser_cfg.kv_strip_whitespace,
                "json_require_object": parser_cfg.json_require_object,
                "regex_pattern": parser_cfg.regex_pattern,
                "regex_flags": parser_cfg.regex_flags,
            },
        }

    def _on_format_changed(self, fmt: str):
        fmt = fmt or FORMAT_CSV_HEADER
        is_csv = fmt in (FORMAT_CSV_HEADER, FORMAT_CSV_FIXED)
        self.csv_delim_edit.setEnabled(is_csv)
        self.csv_fixed_headers_edit.setEnabled(fmt == FORMAT_CSV_FIXED)

        is_kv = fmt == FORMAT_KV
        self.kv_pair_delim_edit.setEnabled(is_kv)
        self.kv_kv_delim_edit.setEnabled(is_kv)

        self.regex_pattern_edit.setEnabled(fmt == FORMAT_REGEX)

    def _refresh_mock_layers(self):
        try:
            from qgis.core import QgsProject, QgsVectorLayer

            self.mock_layer_combo.clear()
            for layer in QgsProject.instance().mapLayers().values():
                if isinstance(layer, QgsVectorLayer):
                    self.mock_layer_combo.addItem(layer.name(), layer.id())
        except Exception as e:
            print(f"DEBUG: Failed to refresh mock layers: {e}")
    
    def on_disconnect_clicked(self):
        """Handle Disconnect button click."""
        if not self.current_slot_id:
            return
        self.disconnect_requested.emit({"slot_id": self.current_slot_id})
    
    def set_connected(self, connected: bool, host: Optional[str] = None, port: Optional[int] = None):
        """
        Update connection status display.
        
        Args:
            connected: Whether connected to data server
            host: Host address
            port: Port number
        """
        self.connected = connected
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        
        if connected:
            self.status_label.setText("Connected ✓")
            self.status_label.setStyleSheet("color: #66cc66; font-weight: bold;")
            if host and port:
                self.connection_info_label.setText(f"{host}:{port}")
            else:
                self.connection_info_label.setText("Active")
        else:
            self.status_label.setText("Not connected")
            self.status_label.setStyleSheet("color: #ff6666; font-weight: bold;")
            self.connection_info_label.setText("—")
    
    # Windows tab handlers
    def on_cards_checkbox_changed(self, state):
        """Handle Cards checkbox change."""
        is_checked = self.cards_checkbox.isChecked()
        print(f"DEBUG: on_cards_checkbox_changed - is_checked={is_checked}")
        if is_checked:
            self.visible_widgets['cards'] = True
            print(f"DEBUG: Emitting show_cards_widget")
            self.show_cards_widget.emit()
        else:
            self.visible_widgets['cards'] = False
            print(f"DEBUG: Emitting hide_cards_widget")
            self.hide_cards_widget.emit()
    
    def on_plots_checkbox_changed(self, state):
        """Handle Plots checkbox change."""
        is_checked = self.plots_checkbox.isChecked()
        print(f"DEBUG: on_plots_checkbox_changed - is_checked={is_checked}")
        if is_checked:
            self.visible_widgets['plots'] = True
            print(f"DEBUG: Emitting show_plots_widget")
            self.show_plots_widget.emit()
        else:
            self.visible_widgets['plots'] = False
            print(f"DEBUG: Emitting hide_plots_widget")
            self.hide_plots_widget.emit()
    
    def on_tables_checkbox_changed(self, state):
        """Handle Tables checkbox change."""
        is_checked = self.tables_checkbox.isChecked()
        print(f"DEBUG: on_tables_checkbox_changed - is_checked={is_checked}")
        if is_checked:
            self.visible_widgets['tables'] = True
            print(f"DEBUG: Emitting show_tables_widget")
            self.show_tables_widget.emit()
        else:
            self.visible_widgets['tables'] = False
            print(f"DEBUG: Emitting hide_tables_widget")
            self.hide_tables_widget.emit()
    
    # Overlay handlers
    def on_dxf_browse(self):
        """Handle DXF file browse."""
        from qgis.PyQt.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(self, "Select DXF File", "", "DXF Files (*.dxf)")
        if file_path:
            self.dxf_edit.setText(file_path)
    
    def on_apply_overlay(self):
        """Apply overlay configuration."""
        config = {
            'dxf_file': self.dxf_edit.text(),
            'lat_field': self.lat_combo.currentText(),
            'lon_field': self.lon_combo.currentText(),
            'heading_field': self.heading_combo.currentText(),
            'scale': self.scale_spin.value(),
            'crp_offset_x': self.crp_x_spin.value(),
            'crp_offset_y': self.crp_y_spin.value(),
            'rotation_offset': self.rot_offset_spin.value()
        }
        if not config['dxf_file'] or not all(config[f] for f in ['lat_field', 'lon_field', 'heading_field']):
            QMessageBox.warning(self, "Incomplete Config", "Please fill all required fields.")
            return
        self.overlays_config = [config]
        # Persist overlay config per slot
        if self.current_slot_id in self.slots:
            self.slots[self.current_slot_id]["overlays_config"] = self.overlays_config
        self.overlays_config_changed.emit({"slot_id": self.current_slot_id, "configs": self.overlays_config})
        self._save_settings()
    
    def on_clear_overlay(self):
        """Clear overlay configuration."""
        self.overlays_config = []
        if self.current_slot_id in self.slots:
            self.slots[self.current_slot_id]["overlays_config"] = self.overlays_config
        self.overlays_config_changed.emit({"slot_id": self.current_slot_id, "configs": self.overlays_config})
        self._save_settings()

    def _refresh_slot_combo(self):
        self.slot_combo.blockSignals(True)
        self.slot_combo.clear()
        for sid, slot in self.slots.items():
            self.slot_combo.addItem(slot.get("slot_name") or "Input", sid)
        self.slot_combo.blockSignals(False)

    def _update_active_slot_label(self):
        if not self.active_slot_id:
            self.active_slot_label.setText("")
            return
        self.active_slot_label.setText(self.slots.get(self.active_slot_id, {}).get("slot_name", ""))

    def on_add_slot(self):
        self._save_ui_to_slot(self.current_slot_id)
        sid = str(uuid.uuid4())
        self.slots[sid] = {"slot_id": sid, "slot_name": f"Input {len(self.slots) + 1}"}
        self._refresh_slot_combo()
        idx = self.slot_combo.findData(sid)
        if idx >= 0:
            self.slot_combo.setCurrentIndex(idx)
        self._save_settings()

    def on_remove_slot(self):
        if not self.current_slot_id:
            return
        if len(self.slots) <= 1:
            return
        remove_id = self.current_slot_id
        self.slots.pop(remove_id, None)

        if self.active_slot_id == remove_id:
            self.active_slot_id = next(iter(self.slots.keys()), None)
            if self.active_slot_id:
                self.active_slot_changed.emit(self.active_slot_id)
        self.current_slot_id = next(iter(self.slots.keys()), None)

        self._refresh_slot_combo()
        if self.current_slot_id:
            idx = self.slot_combo.findData(self.current_slot_id)
            if idx >= 0:
                self.slot_combo.setCurrentIndex(idx)
            self._load_slot_to_ui(self.current_slot_id)
        self._update_active_slot_label()
        self._save_settings()

    def on_set_active_slot(self):
        if not self.current_slot_id:
            return
        self.active_slot_id = self.current_slot_id
        self._update_active_slot_label()
        self.active_slot_changed.emit(self.current_slot_id)
        self._save_settings()

    def on_slot_changed(self, idx: int):
        sid = self.slot_combo.currentData()
        if not sid:
            return
        if self.current_slot_id == sid:
            return
        self._save_ui_to_slot(self.current_slot_id)
        self.current_slot_id = sid
        self._load_slot_to_ui(sid)
        self._save_settings()

    def on_slot_name_edited(self, text: str):
        if not self.current_slot_id or self.current_slot_id not in self.slots:
            return
        name = (text or "").strip() or "Input"
        self.slots[self.current_slot_id]["slot_name"] = name
        # Update combo display text
        idx = self.slot_combo.findData(self.current_slot_id)
        if idx >= 0:
            self.slot_combo.setItemText(idx, name)
        if self.active_slot_id == self.current_slot_id:
            self._update_active_slot_label()

    def _save_ui_to_slot(self, slot_id: Optional[str]):
        if not slot_id or slot_id not in self.slots:
            return
        self.slots[slot_id].update(self.get_stream_config())
        # Save overlay UI as well
        self.slots[slot_id]["overlays_config"] = list(self.overlays_config or [])

    def _load_slot_to_ui(self, slot_id: str):
        slot = self.slots.get(slot_id, {})

        # Slot name
        try:
            self.slot_name_edit.blockSignals(True)
            self.slot_name_edit.setText(str(slot.get("slot_name") or ""))
        finally:
            self.slot_name_edit.blockSignals(False)
        if slot.get("host") is not None:
            self.host_edit.setText(str(slot.get("host") or ""))
        if slot.get("port") is not None:
            try:
                self.port_edit.setText(str(int(slot.get("port") or 12345)))
            except Exception:
                pass
        if slot.get("persist") is not None:
            self.persist_chk.setChecked(bool(slot.get("persist")))

        # Parser
        self.apply_parser_config_dict(slot.get("parser") or {})

        # Mapping
        lat = slot.get("lat_field")
        lon = slot.get("lon_field")
        if lat:
            self.lat_field_combo.setEditText(str(lat))
        if lon:
            self.lon_field_combo.setEditText(str(lon))

        # Overlay config
        self.overlays_config = slot.get("overlays_config") or []
        if self.overlays_config:
            cfg = self.overlays_config[0]
            self.dxf_edit.setText(cfg.get("dxf_file", ""))
            self.scale_spin.setValue(float(cfg.get("scale", 1.0)))
            self.crp_x_spin.setValue(float(cfg.get("crp_offset_x", 0.0)))
            self.crp_y_spin.setValue(float(cfg.get("crp_offset_y", 0.0)))
            self.rot_offset_spin.setValue(float(cfg.get("rotation_offset", 0.0)))

        self._update_active_slot_label()

    def apply_parser_config_dict(self, parser: dict):
        try:
            kind = (parser or {}).get("kind") or FORMAT_CSV_HEADER
            self.format_combo.setCurrentText(kind)
            if (parser or {}).get("csv_delimiter") is not None:
                self.csv_delim_edit.setText(str((parser or {}).get("csv_delimiter") or ",")[:1])
            fixed = (parser or {}).get("csv_fixed_headers") or []
            if fixed:
                self.csv_fixed_headers_edit.setText(",".join([str(x) for x in fixed]))
            else:
                self.csv_fixed_headers_edit.setText("")
            if (parser or {}).get("kv_pair_delimiter") is not None:
                self.kv_pair_delim_edit.setText(str((parser or {}).get("kv_pair_delimiter") or ",")[:1])
            if (parser or {}).get("kv_kv_delimiter") is not None:
                self.kv_kv_delim_edit.setText(str((parser or {}).get("kv_kv_delimiter") or "=")[:1])
            if (parser or {}).get("regex_pattern") is not None:
                self.regex_pattern_edit.setText(str((parser or {}).get("regex_pattern") or ""))
            self._on_format_changed(self.format_combo.currentText())
        except Exception:
            pass

    def _settings(self):
        from qgis.PyQt.QtCore import QSettings
        return QSettings()

    def _load_settings(self):
        try:
            s = self._settings()
            raw = s.value("SubseaCableTools/LiveData/slots_json", "", type=str)
            active = s.value("SubseaCableTools/LiveData/active_slot_id", "", type=str)
            current = s.value("SubseaCableTools/LiveData/current_slot_id", "", type=str)
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict):
                    # Basic validation: each entry must have slot_id
                    slots = {}
                    for sid, cfg in data.items():
                        if not isinstance(cfg, dict):
                            continue
                        cfg = dict(cfg)
                        cfg.setdefault("slot_id", sid)
                        cfg.setdefault("slot_name", "Input")
                        slots[str(sid)] = cfg
                    self.slots = slots
            if active:
                self.active_slot_id = str(active)
            if current:
                self.current_slot_id = str(current)
        except Exception as e:
            print(f"DEBUG: Failed to load Live Data settings: {e}")

    def _save_settings(self):
        try:
            s = self._settings()
            s.setValue("SubseaCableTools/LiveData/slots_json", json.dumps(self.slots))
            s.setValue("SubseaCableTools/LiveData/active_slot_id", self.active_slot_id or "")
            s.setValue("SubseaCableTools/LiveData/current_slot_id", self.current_slot_id or "")
            s.sync()
        except Exception as e:
            print(f"DEBUG: Failed to save Live Data settings: {e}")
    
    def enforce_widget_visibility(self):
        """
        Ensure all windows match their checkbox state.
        Called when dialog is shown.
        """
        for widget_key, is_visible in self.visible_widgets.items():
            if widget_key == 'cards':
                if is_visible:
                    self.show_cards_widget.emit()
                else:
                    self.hide_cards_widget.emit()
            elif widget_key == 'plots':
                if is_visible:
                    self.show_plots_widget.emit()
                else:
                    self.hide_plots_widget.emit()
            elif widget_key == 'tables':
                if is_visible:
                    self.show_tables_widget.emit()
                else:
                    self.hide_tables_widget.emit()
    
    def set_available_fields(self, fields: list):
        """Set the available fields from the data stream."""
        self.available_fields = fields if fields else []
        print(f"DEBUG: LiveDataControlDialog - Available fields updated: {self.available_fields}")
        
        # Update overlay field combos
        self.lat_combo.clear()
        self.lon_combo.clear()
        self.heading_combo.clear()
        self.lat_combo.addItems(self.available_fields)
        self.lon_combo.addItems(self.available_fields)
        self.heading_combo.addItems(self.available_fields)

        # Populate the editable lat/lon combos (keep current text if set)
        try:
            current_lat = self.lat_field_combo.currentText()
            current_lon = self.lon_field_combo.currentText()

            self.lat_field_combo.blockSignals(True)
            self.lon_field_combo.blockSignals(True)
            self.lat_field_combo.clear()
            self.lon_field_combo.clear()
            self.lat_field_combo.addItems(self.available_fields)
            self.lon_field_combo.addItems(self.available_fields)
            self.lat_field_combo.setEditText(current_lat)
            self.lon_field_combo.setEditText(current_lon)
        finally:
            self.lat_field_combo.blockSignals(False)
            self.lon_field_combo.blockSignals(False)
    
    def showEvent(self, event):
        """Dialog is shown - enforce window visibility matches checkbox state."""
        super().showEvent(event)
        self.enforce_widget_visibility()
    
    def closeEvent(self, event):
        """Save geometry on close - with exception handling to prevent crashes."""
        try:
            self._save_ui_to_slot(self.current_slot_id)
            self._save_settings()
            self.saved_geometry = self.saveGeometry()
        except Exception as e:
            print(f"DEBUG: Could not save dialog geometry: {e}")
        super().closeEvent(event)
    
    def force_cleanup(self):
        """
        Force cleanup of dialog and all resources.
        Called during plugin unload.
        """
        try:
            print("DEBUG: LiveDataControlDialog.force_cleanup() called")
            
            # Block all signals
            self.blockSignals(True)
            
            # Disconnect all signals manually
            try:
                self.connect_requested.disconnect()
            except Exception:
                pass
            try:
                self.disconnect_requested.disconnect()
            except Exception:
                pass
            try:
                self.show_cards_widget.disconnect()
            except Exception:
                pass
            try:
                self.show_plots_widget.disconnect()
            except Exception:
                pass
            try:
                self.show_tables_widget.disconnect()
            except Exception:
                pass
            try:
                self.hide_cards_widget.disconnect()
            except Exception:
                pass
            try:
                self.hide_plots_widget.disconnect()
            except Exception:
                pass
            try:
                self.hide_tables_widget.disconnect()
            except Exception:
                pass
            
            print("DEBUG: LiveDataControlDialog cleanup complete")
            
        except Exception as e:
            print(f"DEBUG: Error during LiveDataControlDialog cleanup: {e}")
