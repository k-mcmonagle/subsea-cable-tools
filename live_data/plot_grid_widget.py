"""
Plot Grid Widget

Display plots in a responsive grid layout with zoom control.
Auto-wraps columns based on available width.
Supports drag-and-drop reordering of plots.
"""

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QSlider, QLabel, QPushButton, QScrollArea, QMessageBox
)
from qgis.PyQt.QtCore import Qt, pyqtSignal, QMimeData
from qgis.PyQt.QtGui import QDrag

from .plot_widget import QPlotWidget
from .plot_config import PlotConfig
from typing import Dict, List, Optional
import json


class PlotGridWidget(QWidget):
    """
    Grid display of plots with zoom control.
    
    Features:
    - Auto-wrapping grid layout (2 plots per row)
    - Zoom slider (50% - 150%)
    - Plots adjust size based on zoom
    - Smooth reflow when resizing
    - Right-click context menu for edit/delete
    """
    
    plot_removed = pyqtSignal(str)  # plot_id
    plot_clicked = pyqtSignal(str)  # plot_id
    plot_edit_requested = pyqtSignal(str)  # plot_id
    plot_delete_requested = pyqtSignal(str)  # plot_id
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.plots: Dict[str, QPlotWidget] = {}  # plot_id -> widget
        self.zoom_level = 100  # Percent
        self.cols_per_row = 2  # 2 plots per row
        
        self.setup_ui()
    
    def setup_ui(self):
        """Build the grid with zoom controls."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)
        
        # Zoom controls (top toolbar)
        zoom_layout = QHBoxLayout()
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(4)
        
        zoom_label = QLabel("Zoom:")
        zoom_layout.addWidget(zoom_label)
        
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setMinimum(50)
        self.zoom_slider.setMaximum(150)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setTickPosition(QSlider.TicksBelow)
        self.zoom_slider.setTickInterval(25)
        self.zoom_slider.setMaximumWidth(200)
        self.zoom_slider.valueChanged.connect(self.on_zoom_changed)
        zoom_layout.addWidget(self.zoom_slider)
        
        self.zoom_percent_label = QLabel("100%")
        self.zoom_percent_label.setMinimumWidth(40)
        zoom_layout.addWidget(self.zoom_percent_label)
        
        self.reorder_hint_label = QLabel("(Drag plots to reorder)")
        self.reorder_hint_label.setStyleSheet("color: #999999; font-size: 9px;")
        zoom_layout.addWidget(self.reorder_hint_label)
        
        zoom_layout.addStretch()
        main_layout.addLayout(zoom_layout)
        
        # Scrollable area for plots
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea { background-color: transparent; }")
        
        # Grid layout for plots (2 columns, strict)
        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(6)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.grid_layout.setColumnStretch(0, 1)
        self.grid_layout.setColumnStretch(1, 1)
        
        scroll_area.setWidget(self.grid_widget)
        main_layout.addWidget(scroll_area)
    
    def add_plot(self, config: PlotConfig) -> QPlotWidget:
        """
        Add a plot to the grid in strict 2-column layout.
        
        Args:
            config: PlotConfig for the new plot
            
        Returns:
            The created QPlotWidget
        """
        plot_widget = QPlotWidget(config)
        plot_widget.plot_clicked.connect(self.plot_clicked.emit)
        plot_widget.plot_edit_requested.connect(self.plot_edit_requested.emit)
        plot_widget.plot_delete_requested.connect(self.on_plot_delete_requested)
        
        # Enable drag for reordering
        plot_widget.set_draggable(True)
        plot_widget.drag_started.connect(self.on_plot_drag_started)
        plot_widget.drop_requested.connect(self.on_plot_drop)
        
        # Store reference - maintain insertion order
        self.plots[config.plot_id] = plot_widget
        
        # Calculate position in strict 2-column grid
        plot_index = len(self.plots) - 1
        row = plot_index // 2  # Integer division for row
        col = plot_index % 2   # Modulo 2 for column (always 0 or 1)
        
        self.grid_layout.addWidget(plot_widget, row, col)
        
        return plot_widget
    
    def remove_plot(self, plot_id: str) -> None:
        """
        Remove a plot from the grid.
        
        Args:
            plot_id: ID of the plot to remove
        """
        if plot_id not in self.plots:
            return
        
        plot_widget = self.plots[plot_id]
        plot_widget.deleteLater()
        del self.plots[plot_id]
        
        # Re-layout remaining plots
        self.reflow_grid()
    
    def get_plot(self, plot_id: str) -> Optional[QPlotWidget]:
        """Get a plot widget by ID."""
        return self.plots.get(plot_id)
    
    def reflow_grid(self) -> None:
        """
        Reflow the grid after removing/adding/reordering plots.
        Ensures strict 2-column layout.
        """
        # Clear layout
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)
        
        # Re-add plots in current order with strict 2-column layout
        for i, (plot_id, plot_widget) in enumerate(self.plots.items()):
            row = i // 2      # Integer division for row
            col = i % 2       # Modulo 2 for column (always 0 or 1)
            self.grid_layout.addWidget(plot_widget, row, col)
    
    def update_plot_value(self, plot_id: str, field_name: str, value: float):
        """
        Update a plot value from data stream.
        
        Args:
            plot_id: ID of the plot
            field_name: Field name to update
            value: New value
        """
        plot_widget = self.plots.get(plot_id)
        if plot_widget:
            import time
            plot_widget.update_value(field_name, value, time.time())
    
    def clear_all_plots(self) -> None:
        """Clear all plots from the grid."""
        for plot_widget in self.plots.values():
            try:
                # Clean up matplotlib resources before deletion
                if hasattr(plot_widget, 'cleanup_matplotlib'):
                    plot_widget.cleanup_matplotlib()
            except Exception as e:
                print(f"DEBUG: Error cleaning up plot widget: {e}")
            try:
                plot_widget.deleteLater()
            except Exception:
                pass
        self.plots.clear()
    
    def cleanup_all(self):
        """
        Complete cleanup of all plots and resources.
        Call this before the widget is destroyed during plugin reload.
        """
        try:
            # Block all signals to prevent callbacks during cleanup
            self.blockSignals(True)
            
            # Clean up all plots
            for plot_widget in self.plots.values():
                try:
                    plot_widget.blockSignals(True)
                    if hasattr(plot_widget, 'cleanup_matplotlib'):
                        plot_widget.cleanup_matplotlib()
                    plot_widget.deleteLater()
                except Exception as e:
                    print(f"DEBUG: Error during plot cleanup: {e}")
            
            self.plots.clear()
            
        except Exception as e:
            print(f"DEBUG: Error during plot grid cleanup: {e}")
    
    def on_zoom_changed(self, value: int) -> None:
        """Handle zoom slider change."""
        self.zoom_level = value
        self.zoom_percent_label.setText(f"{value}%")
        
        # Update minimum size of all plots based on zoom
        for plot_widget in self.plots.values():
            base_width = 400
            base_height = 250
            new_width = int(base_width * value / 100)
            new_height = int(base_height * value / 100)
            plot_widget.setMinimumSize(new_width, new_height)
    
    def on_plot_clicked(self, plot_id: str) -> None:
        """Handle plot clicked."""
        self.plot_clicked.emit(plot_id)
    
    def on_plot_delete_requested(self, plot_id: str) -> None:
        """Handle plot deletion request from context menu."""
        self.plot_delete_requested.emit(plot_id)
    
    def get_all_plot_ids(self) -> List[str]:
        """Get list of all plot IDs in the grid."""
        return list(self.plots.keys())
    
    def get_plot_count(self) -> int:
        """Get number of plots in the grid."""
        return len(self.plots)
    
    def reorder_plot(self, plot_id: str, new_index: int) -> None:
        """
        Move a plot to a new position in the grid.
        
        Args:
            plot_id: ID of the plot to move
            new_index: New position (0-based)
        """
        if plot_id not in self.plots:
            return
        
        plot_ids = list(self.plots.keys())
        if new_index < 0 or new_index >= len(plot_ids):
            return
        
        # Remove from current position
        plot_ids.remove(plot_id)
        # Insert at new position
        plot_ids.insert(new_index, plot_id)
        
        # Rebuild plots dict with new order
        new_plots = {}
        for pid in plot_ids:
            new_plots[pid] = self.plots[pid]
        self.plots = new_plots
        
        # Reflow grid
        self.reflow_grid()
    
    def swap_plots(self, plot_id_1: str, plot_id_2: str) -> None:
        """
        Swap positions of two plots.
        
        Args:
            plot_id_1: First plot ID
            plot_id_2: Second plot ID
        """
        if plot_id_1 not in self.plots or plot_id_2 not in self.plots:
            return
        
        plot_ids = list(self.plots.keys())
        idx_1 = plot_ids.index(plot_id_1)
        idx_2 = plot_ids.index(plot_id_2)
        
        # Swap
        plot_ids[idx_1], plot_ids[idx_2] = plot_ids[idx_2], plot_ids[idx_1]
        
        # Rebuild plots dict with new order
        new_plots = {}
        for pid in plot_ids:
            new_plots[pid] = self.plots[pid]
        self.plots = new_plots
        
        # Reflow grid
        self.reflow_grid()
    
    def get_plot_index(self, plot_id: str) -> int:
        """Get the current index of a plot."""
        plot_ids = list(self.plots.keys())
        return plot_ids.index(plot_id) if plot_id in plot_ids else -1
    
    def on_plot_drag_started(self, plot_id: str) -> None:
        """Handle plot drag start for reordering."""
        self.dragged_plot_id = plot_id
    
    def on_plot_drop(self, target_plot_id: str) -> None:
        """Handle plot drop on another plot to swap positions."""
        if not hasattr(self, 'dragged_plot_id'):
            return
        
        if self.dragged_plot_id != target_plot_id and self.dragged_plot_id in self.plots:
            self.swap_plots(self.dragged_plot_id, target_plot_id)
            self.dragged_plot_id = None
