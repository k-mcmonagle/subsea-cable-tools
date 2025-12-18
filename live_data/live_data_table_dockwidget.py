"""
Live Data Table Dock Widget

Separate dockable widget for displaying live data in table format.
Receives updates from LiveDataWorker and manages table display.
"""

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox, QLabel, QScrollArea
)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from typing import Dict, Optional

from .table_config import TableConfig
from .live_data_table_widget import LiveDataTableWidget
from .table_manager_dialog import LiveDataTableManagerDialog
from .utils import TableConfigManager


class LiveDataTableDockWidget(QDockWidget):
    """
    Separate dockable widget for displaying live data in table format.
    
    Receives:
    - Table configurations to display
    - Data updates to refresh values
    
    Provides:
    - Table display of live data
    - Add/edit/delete table management
    - Field selection and ordering
    """
    
    table_added = pyqtSignal(TableConfig)       # New table created
    table_edited = pyqtSignal(TableConfig)      # Existing table modified
    table_deleted = pyqtSignal(str)             # table_id deleted
    
    def __init__(self, parent=None):
        super().__init__("Live Data Table", parent)
        self.setObjectName("LiveDataTableDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        
        self.tables: Dict[str, TableConfig] = {}  # table_id -> config
        self.table_widgets: Dict[str, LiveDataTableWidget] = {}  # table_id -> widget
        self.available_headers: list = []
        self.connected = False
        
        self.setup_ui()
        self.load_tables()
    
    def setup_ui(self):
        """Build the dock widget UI."""
        self.widget = QWidget()
        self.setWidget(self.widget)
        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Toolbar
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(4, 4, 4, 4)
        toolbar_layout.setSpacing(4)
        
        self.add_table_btn = QPushButton("+ Add Table")
        self.add_table_btn.clicked.connect(self.add_table)
        self.add_table_btn.setEnabled(False)  # Disabled until connected
        toolbar_layout.addWidget(self.add_table_btn)
        
        self.status_label = QLabel("No connection")
        self.status_label.setStyleSheet("color: #666666; font-size: 10px;")
        toolbar_layout.addWidget(self.status_label)
        
        toolbar_layout.addStretch()
        layout.addLayout(toolbar_layout)
        
        # Scrollable container for tables
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        self.tables_container = QWidget()
        self.tables_layout = QVBoxLayout(self.tables_container)
        self.tables_layout.setContentsMargins(4, 4, 4, 4)
        self.tables_layout.setSpacing(8)
        scroll_area.setWidget(self.tables_container)
        layout.addWidget(scroll_area, 1)  # Give it stretch factor to fill available space
    
    def set_available_headers(self, headers: list):
        """
        Set available data fields (called when headers received).
        
        Args:
            headers: List of field names from data stream
        """
        self.available_headers = headers
        self.set_connected(True)
        # Display any loaded tables once headers are known
        self.display_loaded_tables()
    
    def set_connected(self, connected: bool):
        """Update connection status."""
        self.connected = connected
        self.add_table_btn.setEnabled(connected and len(self.available_headers) > 0)
        
        if connected:
            self.status_label.setText(f"Connected ({len(self.available_headers)} fields)")
            self.status_label.setStyleSheet("color: #00AA00; font-size: 10px;")
        else:
            self.status_label.setText("Disconnected")
            self.status_label.setStyleSheet("color: #666666; font-size: 10px;")
    
    def add_table(self):
        """Open dialog to add new table."""
        if not self.available_headers:
            QMessageBox.warning(self, "No Data", "Connect to data stream first")
            return
        
        dialog = LiveDataTableManagerDialog(self.available_headers, parent=self)
        dialog.table_configured.connect(self.on_table_configured)
        dialog.exec_()
    
    def edit_table(self, table_id: str):
        """Open dialog to edit existing table."""
        if table_id not in self.tables:
            return
        
        config = self.tables[table_id]
        dialog = LiveDataTableManagerDialog(
            self.available_headers, 
            existing_config=config, 
            parent=self
        )
        dialog.table_configured.connect(self.on_table_configured)
        dialog.exec_()
    
    def on_table_configured(self, config: TableConfig):
        """Handle table created or edited."""
        # Check if editing existing table
        is_new = config.table_id not in self.tables
        
        # Update our dict
        self.tables[config.table_id] = config
        
        if is_new:
            # Add to display
            table_widget = self.add_table_to_display(config)
            self.table_added.emit(config)
        else:
            # Update existing widget in display
            table_widget = self.table_widgets.get(config.table_id)
            if table_widget:
                # Remove and re-add
                self.remove_table_from_display(config.table_id)
                table_widget = self.add_table_to_display(config)
            self.table_edited.emit(config)
        
        # Save to project
        self.save_tables()
    
    def add_table_to_display(self, config: TableConfig) -> LiveDataTableWidget:
        """Add table widget to display."""
        table_widget = LiveDataTableWidget(config)
        table_widget.table_edit_requested.connect(self.edit_table)
        table_widget.table_delete_requested.connect(self.on_table_deleted_from_context_menu)
        
        # Insert before stretch
        self.tables_layout.insertWidget(len(self.table_widgets), table_widget)
        self.table_widgets[config.table_id] = table_widget
        
        return table_widget
    
    def remove_table_from_display(self, table_id: str):
        """Remove table widget from display."""
        if table_id in self.table_widgets:
            widget = self.table_widgets[table_id]
            widget.setParent(None)
            del self.table_widgets[table_id]
    
    def on_table_deleted_from_context_menu(self, table_id: str):
        """Handle table deletion from context menu."""
        # Show confirmation dialog
        reply = QMessageBox.question(
            self, 
            "Delete Table",
            f"Are you sure you want to delete this table?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            if table_id in self.tables:
                del self.tables[table_id]
                self.remove_table_from_display(table_id)
                self.save_tables()
                self.table_deleted.emit(table_id)
    
    def update_all_values(self, data_dict: dict):
        """
        Update all tables with values from data dictionary.
        
        Args:
            data_dict: Dictionary mapping field_name -> value
        """
        for table_id, config in self.tables.items():
            table_widget = self.table_widgets.get(table_id)
            if table_widget:
                table_widget.update_all_values(data_dict)
    
    def display_loaded_tables(self):
        """
        Display previously saved tables (call after headers received).
        Only shows tables with valid field references.
        """
        if not self.available_headers:
            return
        
        for config in self.tables.values():
            # Check if all fields exist
            for field_config in config.field_configs:
                if field_config.field_name not in self.available_headers:
                    # Skip tables with invalid fields
                    continue
            
            self.add_table_to_display(config)
    
    def save_tables(self):
        """Save table configs to project."""
        tables_list = list(self.tables.values())
        TableConfigManager.save_tables_to_project(tables_list)
    
    def load_tables(self):
        """Load saved table configs from project."""
        loaded = TableConfigManager.load_tables_from_project()
        for config in loaded:
            self.tables[config.table_id] = config
    
    def clear_all_tables(self):
        """Clear all tables from display and storage."""
        for table_id in list(self.table_widgets.keys()):
            self.remove_table_from_display(table_id)
        self.tables.clear()
        self.save_tables()
    
    def closeEvent(self, event):
        """
        Handle widget close request.
        Hide instead of closing to allow reopening via checkbox.
        """
        # Save current state
        self.save_tables()
        # Hide the widget instead of closing it
        self.hide()
        event.ignore()  # Don't actually close/destroy the widget
    
    def force_cleanup(self):
        """
        Force cleanup of all resources.
        Called during plugin unload.
        """
        try:
            print("DEBUG: LiveDataTableDockWidget.force_cleanup() called")
            # Block all signals
            self.blockSignals(True)
            # Clear tables
            self.clear_all_tables()
            print("DEBUG: LiveDataTableDockWidget cleanup complete")
        except Exception as e:
            print(f"DEBUG: Error during LiveDataTableDockWidget cleanup: {e}")
