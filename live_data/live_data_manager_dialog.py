"""
Live Data Manager Dialog

Central control panel for managing Live Data dock widgets.
Allows users to open/close individual windows and arrange them as needed.
This provides a single entry point while allowing flexible window management.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QPushButton, QCheckBox, QLabel, QFrame, QScrollArea, QWidget,
    QMessageBox
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QSize
from qgis.PyQt.QtGui import QFont, QIcon
from typing import Optional


class LiveDataManagerDialog(QDialog):
    """
    Central manager dialog for Live Data system.
    
    Allows users to:
    - Open/close individual dock widgets (Data, Cards, Plots, Tables)
    - View connection status
    - Access help/documentation
    - Minimize and restore the manager dialog itself
    
    Signals:
        show_data_widget: Emitted to show Live Data dock widget
        show_cards_widget: Emitted to show Cards dock widget
        show_plots_widget: Emitted to show Plots dock widget
        show_tables_widget: Emitted to show Tables dock widget
        hide_data_widget: Emitted to hide Live Data dock widget
        hide_cards_widget: Emitted to hide Cards dock widget
        hide_plots_widget: Emitted to hide Plots dock widget
        hide_tables_widget: Emitted to hide Tables dock widget
    """
    
    # Signals for showing/hiding individual widgets
    show_data_widget = pyqtSignal()
    show_cards_widget = pyqtSignal()
    show_plots_widget = pyqtSignal()
    show_tables_widget = pyqtSignal()
    
    hide_data_widget = pyqtSignal()
    hide_cards_widget = pyqtSignal()
    hide_plots_widget = pyqtSignal()
    hide_tables_widget = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Live Data Manager")
        self.setModal(False)
        self.setMinimumWidth(400)
        self.setMinimumHeight(300)
        
        # Track widget visibility state - initialize to False
        # (windows won't show until explicitly checked)
        self.visible_widgets = {
            'data': False,
            'cards': False,
            'plots': False,
            'tables': False
        }
        
        # Remember position/size for minimize/restore
        self.saved_geometry = None
        
        self.setup_ui()
        
    def setup_ui(self):
        """Build the manager dialog UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        
        # Header
        header_layout = QVBoxLayout()
        title = QLabel("Live Data Control Panel")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title.setFont(title_font)
        header_layout.addWidget(title)
        
        subtitle = QLabel("Manage which Live Data windows are visible")
        subtitle_font = QFont()
        subtitle_font.setPointSize(9)
        subtitle.setFont(subtitle_font)
        subtitle.setStyleSheet("color: #666666;")
        header_layout.addWidget(subtitle)
        layout.addLayout(header_layout)
        
        # Separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.HLine)
        sep1.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep1)
        
        # Connection status group
        status_group = QGroupBox("Connection Status")
        status_layout = QFormLayout(status_group)
        
        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("color: #ff6666; font-weight: bold;")
        status_layout.addRow("Status:", self.status_label)
        
        self.host_label = QLabel("—")
        status_layout.addRow("Connected to:", self.host_label)
        
        layout.addWidget(status_group)
        
        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep2)
        
        # Widget toggles group
        widgets_group = QGroupBox("Live Data Windows")
        widgets_layout = QVBoxLayout(widgets_group)
        widgets_layout.setSpacing(8)
        
        # Live Data Widget toggle
        data_layout = QHBoxLayout()
        self.data_checkbox = QCheckBox("Live Data Connection")
        self.data_checkbox.setChecked(False)
        self.data_checkbox.setToolTip("Connection settings and data stream configuration")
        self.data_checkbox.stateChanged.connect(self.on_data_checkbox_changed)
        data_layout.addWidget(self.data_checkbox)
        data_layout.addStretch()
        widgets_layout.addLayout(data_layout)
        
        # Cards Widget toggle
        cards_layout = QHBoxLayout()
        self.cards_checkbox = QCheckBox("Live Data Cards")
        self.cards_checkbox.setChecked(False)
        self.cards_checkbox.setToolTip("Display single values in card grid layout")
        self.cards_checkbox.stateChanged.connect(self.on_cards_checkbox_changed)
        cards_layout.addWidget(self.cards_checkbox)
        cards_layout.addStretch()
        widgets_layout.addLayout(cards_layout)
        
        # Plots Widget toggle
        plots_layout = QHBoxLayout()
        self.plots_checkbox = QCheckBox("Live Data Plots")
        self.plots_checkbox.setChecked(False)
        self.plots_checkbox.setToolTip("Display time-series data as real-time graphs")
        plots_layout.addWidget(self.plots_checkbox)
        plots_layout.addStretch()
        widgets_layout.addLayout(plots_layout)
        
        # Tables Widget toggle
        tables_layout = QHBoxLayout()
        self.tables_checkbox = QCheckBox("Live Data Tables")
        self.tables_checkbox.setChecked(False)
        self.tables_checkbox.setToolTip("Display multiple fields in tabular format")
        tables_layout.addWidget(self.tables_checkbox)
        tables_layout.addStretch()
        widgets_layout.addLayout(tables_layout)
        
        layout.addWidget(widgets_group)
        
        # Info box
        info_group = QGroupBox("Tips")
        info_layout = QVBoxLayout(info_group)
        info_text = QLabel(
            "• Uncheck any window to hide it\n"
            "• Minimize windows individually using QGIS dock controls\n"
            "• Dock windows can be rearranged by dragging their title bars\n"
            "• Re-open the manager to show/restore hidden windows\n"
            "• Close the manager dialog but windows remain active"
        )
        info_text.setFont(QFont("", 9))
        info_text.setStyleSheet("color: #444444; line-height: 1.4;")
        info_layout.addWidget(info_text)
        layout.addWidget(info_group)
        
        layout.addStretch()
        
        # Separator
        sep3 = QFrame()
        sep3.setFrameShape(QFrame.HLine)
        sep3.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep3)
        
        # Bottom buttons
        button_layout = QHBoxLayout()
        
        self.minimize_btn = QPushButton("Minimize Manager")
        self.minimize_btn.setMaximumWidth(150)
        self.minimize_btn.clicked.connect(self.minimize_manager)
        button_layout.addWidget(self.minimize_btn)
        
        button_layout.addStretch()
        
        self.close_btn = QPushButton("Close Manager")
        self.close_btn.setMaximumWidth(150)
        self.close_btn.clicked.connect(self.close_manager)
        button_layout.addWidget(self.close_btn)
        
        layout.addLayout(button_layout)
    
    def on_data_checkbox_changed(self, state):
        """Handle Live Data widget checkbox change."""
        is_checked = self.data_checkbox.isChecked()
        if is_checked:
            self.visible_widgets['data'] = True
            self.show_data_widget.emit()
        else:
            self.visible_widgets['data'] = False
            self.hide_data_widget.emit()
    
    def on_cards_checkbox_changed(self, state):
        """Handle Cards widget checkbox change."""
        is_checked = self.cards_checkbox.isChecked()
        if is_checked:
            self.visible_widgets['cards'] = True
            self.show_cards_widget.emit()
        else:
            self.visible_widgets['cards'] = False
            self.hide_cards_widget.emit()
    
    def on_plots_checkbox_changed(self, state):
        """Handle Plots widget checkbox change."""
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
        """Handle Tables widget checkbox change."""
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
    
    def set_connection_status(self, connected: bool, host: Optional[str] = None, port: Optional[int] = None):
        """
        Update the connection status display.
        
        Args:
            connected: Whether currently connected to data source
            host: Host address (optional)
            port: Port number (optional)
        """
        if connected:
            self.status_label.setText("Connected ✓")
            self.status_label.setStyleSheet("color: #66cc66; font-weight: bold;")
            if host and port:
                self.host_label.setText(f"{host}:{port}")
            else:
                self.host_label.setText("Active")
        else:
            self.status_label.setText("Not connected")
            self.status_label.setStyleSheet("color: #ff6666; font-weight: bold;")
            self.host_label.setText("—")
    
    def show_all_widgets(self):
        """Show all Live Data windows (called when manager is shown)."""
        for widget_key, is_visible in self.visible_widgets.items():
            if is_visible:
                if widget_key == 'data':
                    self.show_data_widget.emit()
                elif widget_key == 'cards':
                    self.show_cards_widget.emit()
                elif widget_key == 'plots':
                    self.show_plots_widget.emit()
                elif widget_key == 'tables':
                    self.show_tables_widget.emit()
    
    def minimize_manager(self):
        """Minimize the manager dialog while keeping windows open."""
        try:
            self.saved_geometry = self.saveGeometry()
        except Exception:
            pass
        self.hide()
    
    def close_manager(self):
        """Close the manager dialog."""
        try:
            self.saved_geometry = self.saveGeometry()
        except Exception:
            pass
        self.close()
    
    def restore_manager(self):
        """Restore the manager dialog to previous position."""
        if self.saved_geometry:
            try:
                self.restoreGeometry(self.saved_geometry)
            except Exception:
                pass
        self.show()
        self.raise_()
        self.activateWindow()
    
    def closeEvent(self, event):
        """Save geometry on close - with exception handling."""
        try:
            self.saved_geometry = self.saveGeometry()
        except Exception as e:
            print(f"DEBUG: Could not save manager dialog geometry: {e}")
        super().closeEvent(event)
    
    def showEvent(self, event):
        """Manager is shown - ensure widget visibility matches checkbox state."""
        super().showEvent(event)
        # Update widget visibility based on current checkbox state
        # This ensures that if QGIS has auto-restored widgets, we re-hide any that aren't checked
        self.enforce_widget_visibility()
    
    def enforce_widget_visibility(self):
        """
        Ensure that all dock widgets match their checkbox state.
        Called whenever the manager is shown to correct any auto-restored widgets.
        """
        for widget_key, is_visible in self.visible_widgets.items():
            if widget_key == 'data':
                if is_visible:
                    self.show_data_widget.emit()
                else:
                    self.hide_data_widget.emit()
            elif widget_key == 'cards':
                if is_visible:
                    self.show_cards_widget.emit()
                else:
                    self.hide_cards_widget.emit()
            elif widget_key == 'plots':
                if is_visible:
                    self.show_plots_widget.emit()
                    print(f"DEBUG: Emitted show_plots_widget signal")
                else:
                    self.hide_plots_widget.emit()
            elif widget_key == 'tables':
                if is_visible:
                    self.show_tables_widget.emit()
                    print(f"DEBUG: Emitted show_tables_widget signal")
                else:
                    self.hide_tables_widget.emit()
