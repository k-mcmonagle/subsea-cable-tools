"""
Live Data Plots Dock Widget

Separate dockable widget for displaying live data plots in grid layout.
Receives updates from LiveDataWorker and manages plot display.
"""

from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QMessageBox, QLabel
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer
from typing import Dict

from .plot_config import PlotConfig
from .plot_grid_widget import PlotGridWidget
from .plot_manager_dialog import PlotManagerDialog
from .utils import PlotConfigManager
import time


class LiveDataPlotsDockWidget(QDockWidget):
    """
    Separate dockable widget for displaying live data plots in grid.
    
    Receives:
    - Plot configurations to display
    - Data updates to refresh values
    
    Provides:
    - Grid display of plots
    - Zoom control
    - Add/edit/delete plot management
    """
    
    plot_added = pyqtSignal(PlotConfig)       # New plot created
    plot_edited = pyqtSignal(PlotConfig)      # Existing plot modified
    plot_deleted = pyqtSignal(str)            # plot_id deleted
    
    def __init__(self, parent=None):
        super().__init__("Live Data Plots", parent)
        self.setObjectName("LiveDataPlotsDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        
        self.plots: Dict[str, PlotConfig] = {}  # plot_id -> config
        self.available_headers: list = []
        self.connected = False
        self.last_data_timestamp = None
        
        self.setup_ui()
        self.load_plots()
    
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
        
        self.add_plot_btn = QPushButton("+ Add Plot")
        self.add_plot_btn.clicked.connect(self.add_plot)
        self.add_plot_btn.setEnabled(False)  # Disabled until connected
        toolbar_layout.addWidget(self.add_plot_btn)
        
        self.status_label = QLabel("No connection")
        self.status_label.setStyleSheet("color: #666666; font-size: 10px;")
        toolbar_layout.addWidget(self.status_label)
        
        toolbar_layout.addStretch()
        
        layout.addLayout(toolbar_layout)
        
        # Grid widget
        self.grid_widget = PlotGridWidget()
        self.grid_widget.plot_removed.connect(self.on_plot_removed_from_grid)
        self.grid_widget.plot_edit_requested.connect(self.edit_plot)
        self.grid_widget.plot_delete_requested.connect(self.on_plot_deleted_from_context_menu)
        layout.addWidget(self.grid_widget)
    
    def set_available_headers(self, headers: list):
        """
        Set available data fields (called when headers received).
        
        Args:
            headers: List of field names from data stream
        """
        self.available_headers = headers
        self.set_connected(True)
        # Display any loaded plots once headers are known
        self.display_loaded_plots()
    
    def set_connected(self, connected: bool):
        """Update connection status."""
        self.connected = connected
        self.add_plot_btn.setEnabled(connected and len(self.available_headers) > 0)
        
        if connected:
            self.status_label.setText(f"Connected ({len(self.available_headers)} fields)")
            self.status_label.setStyleSheet("color: #00AA00; font-size: 10px;")
        else:
            self.status_label.setText("Disconnected")
            self.status_label.setStyleSheet("color: #666666; font-size: 10px;")
    
    def add_plot(self):
        """Open dialog to add new plot."""
        if not self.available_headers:
            QMessageBox.warning(self, "No Data", "Connect to data stream first")
            return
        
        dialog = PlotManagerDialog(self.available_headers, parent=self)
        dialog.plot_configured.connect(self.on_plot_configured)
        dialog.exec_()
    
    def edit_plot(self, plot_id: str):
        """Open dialog to edit existing plot."""
        if plot_id not in self.plots:
            return
        
        config = self.plots[plot_id]
        dialog = PlotManagerDialog(self.available_headers, existing_config=config, parent=self)
        dialog.plot_configured.connect(self.on_plot_configured)
        dialog.exec_()
    
    def on_plot_configured(self, config: PlotConfig):
        """Handle plot created or edited."""
        # Check if editing existing plot
        is_new = config.plot_id not in self.plots
        
        # Update our dict
        self.plots[config.plot_id] = config
        
        if is_new:
            # Add to grid
            plot_widget = self.grid_widget.add_plot(config)
            self.plot_added.emit(config)
        else:
            # Update existing widget in grid with new config (preserves data!)
            plot_widget = self.grid_widget.get_plot(config.plot_id)
            if plot_widget:
                # Update config on existing widget instead of removing/re-adding
                # This preserves the existing data buffers
                plot_widget.update_config(config)
            self.plot_edited.emit(config)
        
        # Save to project
        self.save_plots()
    
    def on_plot_removed_from_grid(self, plot_id: str):
        """Handle plot deleted from grid (right-click delete)."""
        if plot_id in self.plots:
            del self.plots[plot_id]
            self.save_plots()
            self.plot_deleted.emit(plot_id)
    
    def on_plot_deleted_from_context_menu(self, plot_id: str):
        """Handle plot deletion from context menu."""
        # Show confirmation dialog
        reply = QMessageBox.question(
            self, 
            "Delete Plot",
            f"Are you sure you want to delete this plot?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.grid_widget.remove_plot(plot_id)
    
    def update_all_plots(self, data_dict: dict, timestamp: float = None):
        """
        Update all plots with values from data dictionary.
        
        Args:
            data_dict: Dictionary mapping field_name -> value
            timestamp: Unix timestamp of the data point (uses current time if None)
        """
        if timestamp is None:
            timestamp = time.time()
        
        self.last_data_timestamp = timestamp
        
        for plot_id, config in self.plots.items():
            plot_widget = self.grid_widget.get_plot(plot_id)
            if plot_widget:
                # Update all fields in this plot
                for field_name in config.field_names:
                    value = data_dict.get(field_name)
                    if value is not None:
                        try:
                            value = float(value)
                            plot_widget.update_value(field_name, value, timestamp)
                        except (ValueError, TypeError):
                            pass  # Skip non-numeric values
    
    def display_loaded_plots(self):
        """
        Display previously saved plots (call after headers received).
        Only shows plots with valid field references.
        """
        if not self.available_headers:
            return
        
        for config in self.plots.values():
            # Check all fields exist
            valid = all(field_name in self.available_headers for field_name in config.field_names)
            if valid:
                self.grid_widget.add_plot(config)
    
    def save_plots(self):
        """Save plot configs to project."""
        plots_list = list(self.plots.values())
        PlotConfigManager.save_plots_to_project(plots_list)
    
    def load_plots(self):
        """Load saved plot configs from project."""
        loaded = PlotConfigManager.load_plots_from_project()
        for config in loaded:
            self.plots[config.plot_id] = config
    
    def clear_all_plots(self):
        """Clear all plots from display and storage."""
        self.grid_widget.clear_all_plots()
        self.plots.clear()
        self.save_plots()
    
    def get_plot_statistics(self, plot_id: str) -> dict:
        """
        Get statistics for a specific plot.
        
        Args:
            plot_id: ID of the plot
            
        Returns:
            Dictionary with statistics for each field
        """
        plot_widget = self.grid_widget.get_plot(plot_id)
        if plot_widget:
            return plot_widget.get_statistics()
        return {}
    
    def get_all_plot_statistics(self) -> dict:
        """
        Get statistics for all plots.
        
        Returns:
            Dictionary mapping plot_id -> field_stats
        """
        result = {}
        for plot_id in self.plots.keys():
            result[plot_id] = self.get_plot_statistics(plot_id)
        return result
    
    def closeEvent(self, event):
        """
        Handle widget close request.
        Hide instead of closing to allow reopening via checkbox.
        """
        # Save current state
        self.save_plots()
        
        # Clean up matplotlib resources before closing
        try:
            if hasattr(self, 'grid_widget') and self.grid_widget:
                if hasattr(self.grid_widget, 'cleanup_all'):
                    self.grid_widget.cleanup_all()
        except Exception as e:
            print(f"DEBUG: Error during plot dockwidget cleanup: {e}")
        
        # Hide the widget instead of closing it
        self.hide()
        event.ignore()  # Don't actually close/destroy the widget
