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
    QWidget, QMessageBox, QComboBox, QDoubleSpinBox
)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QFont
from typing import Optional


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
    connect_requested = pyqtSignal(str, int)  # host, port
    disconnect_requested = pyqtSignal()
    
    # Signal for overlays config
    overlays_config_changed = pyqtSignal(list)  # list of overlay configs
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Live Data Control")
        self.setModal(False)
        self.setMinimumWidth(450)
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
        
        # Overlays configuration
        self.overlays_config = []
        
        # Clear any corrupted Qt state from previous versions
        self._clear_corrupted_state()
        
        self.setup_ui()
    
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
        
        # Connection settings
        conn_group = QGroupBox("Connection Settings")
        conn_form = QFormLayout(conn_group)
        
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
        
        self.lat_field_edit = QLineEdit("Lat_dd")
        self.lon_field_edit = QLineEdit("Lon_dd")
        
        data_layout.addRow("Latitude Field:", self.lat_field_edit)
        data_layout.addRow("Longitude Field:", self.lon_field_edit)
        
        self.persist_chk = QCheckBox("Persist Points on Map")
        self.persist_chk.setChecked(True)
        self.persist_chk.setToolTip("If checked, all received points remain on map. If unchecked, only latest point shown.")
        data_layout.addRow(self.persist_chk)
        
        conn_layout.addWidget(data_group)
        
        # Status display
        status_group = QGroupBox("Connection Status")
        status_layout = QFormLayout(status_group)
        
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
    
    def _get_int_validator(self):
        """Get validator for integer input (port number)."""
        from qgis.PyQt.QtGui import QIntValidator
        return QIntValidator(1, 65535)
    
    # Connection tab handlers
    def on_connect_clicked(self):
        """Handle Connect button click."""
        try:
            host = self.host_edit.text()
            port = int(self.port_edit.text())
            self.connect_requested.emit(host, port)
        except ValueError:
            QMessageBox.warning(self, "Invalid Port", "Port must be a number between 1 and 65535")
    
    def on_disconnect_clicked(self):
        """Handle Disconnect button click."""
        self.disconnect_requested.emit()
    
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
        self.overlays_config_changed.emit(self.overlays_config)
    
    def on_clear_overlay(self):
        """Clear overlay configuration."""
        self.overlays_config = []
        self.overlays_config_changed.emit(self.overlays_config)
    
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
    
    def showEvent(self, event):
        """Dialog is shown - enforce window visibility matches checkbox state."""
        super().showEvent(event)
        self.enforce_widget_visibility()
    
    def closeEvent(self, event):
        """Save geometry on close - with exception handling to prevent crashes."""
        try:
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
