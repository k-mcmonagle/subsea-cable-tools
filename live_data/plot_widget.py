"""
Live Data Plot Widget

Displays a real-time trend plot using Matplotlib (built into QGIS).
Updates in real-time as data is received from the stream.
Supports drag-and-drop for reordering.
"""

from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QMenu
from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer, QMimeData, QRect
from qgis.PyQt.QtGui import QFont, QColor, QDrag, QPixmap

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.pyplot as plt

from .plot_config import PlotConfig
from .plot_data_buffer import PlotDataBuffer
from typing import Optional, Dict, Any
import time
import json


class QPlotWidget(QFrame):
    """
    Display widget for a single live data plot.
    Shows a real-time line graph with statistics and styling options.
    Supports drag-and-drop for reordering.
    
    Right-click context menu:
    - Edit Plot
    - Delete Plot
    """
    
    data_updated = pyqtSignal()  # Emitted when new data is plotted
    plot_clicked = pyqtSignal(str)  # Emitted with plot_id when clicked
    plot_edit_requested = pyqtSignal(str)  # plot_id
    plot_delete_requested = pyqtSignal(str)  # plot_id
    drag_started = pyqtSignal(str)  # plot_id - for reordering
    drop_requested = pyqtSignal(str)  # plot_id - target plot_id when dropped on
    
    def __init__(self, config: PlotConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.data_buffers: Dict[str, PlotDataBuffer] = {}  # One buffer per field
        self.last_render_time = 0
        self.min_render_interval = 0.1  # 100ms minimum between renders
        self.draggable = False
        self.drag_start_pos = None
        
        # Initialize data buffers for each field
        for field_name in config.field_names:
            self.data_buffers[field_name] = PlotDataBuffer(
                max_points=config.max_points,
                time_window=config.time_window
            )
        
        self.setup_ui()
        self.apply_styling()
        
    def setup_ui(self):
        """Build the plot UI layout with compact spacing."""
        self.setFrameShape(QFrame.StyledPanel)
        self.setFrameShadow(QFrame.Raised)
        self.setLineWidth(1)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)  # Reduced from 12px
        layout.setSpacing(3)  # Reduced from 6px
        
        # Header: Plot name and field info (compact)
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(2)
        
        self.name_label = QLabel(self.config.name)
        name_font = QFont()
        name_font.setPointSize(9)  # Reduced from 10
        name_font.setBold(True)
        self.name_label.setFont(name_font)
        self.name_label.setAlignment(Qt.AlignLeft)
        self.name_label.setMaximumHeight(18)
        header_layout.addWidget(self.name_label)
        header_layout.addStretch()
        
        # Update indicator
        self.update_indicator = QLabel("●")
        self.update_indicator.setStyleSheet("color: #AAAAAA; font-size: 5px;")
        self.update_indicator.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.update_indicator.setMaximumWidth(8)
        header_layout.addWidget(self.update_indicator)
        
        layout.addLayout(header_layout)
        
        # Create Matplotlib figure with tight layout
        self.figure = Figure(figsize=(5, 3), dpi=100)
        self.figure.subplots_adjust(left=0.08, right=0.92, top=0.92, bottom=0.12)  # Tight margins
        self.canvas = FigureCanvas(self.figure)
        self.ax = self.figure.add_subplot(111)
        
        # Configure plot axes
        self.ax.set_xlabel('Time (s)', fontsize=8)  # Smaller font
        if self.config.units:
            self.ax.set_ylabel(f'{self.config.name} ({self.config.units})', fontsize=8)
        else:
            self.ax.set_ylabel('Value', fontsize=8)
        
        # Smaller tick labels
        self.ax.tick_params(labelsize=7)
        
        self.ax.grid(self.config.axis_config.show_grid, alpha=0.3)
        
        # Store plot lines for updating
        self.plot_lines = {}
        colors = [self.config.styling.line_color, self.config.styling.secondary_line_color]
        
        for i, field_name in enumerate(self.config.field_names):
            line, = self.ax.plot([], [], label=field_name, color=colors[i], 
                                linewidth=self.config.styling.line_width)
            self.plot_lines[field_name] = line
        
        # Add legend if needed
        if len(self.config.field_names) > 1 or self.config.axis_config.show_legend:
            self.ax.legend(loc='upper left', fontsize=7)
        
        layout.addWidget(self.canvas, stretch=1)
        
        # Statistics display (compact)
        stats_layout = QHBoxLayout()
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(2)
        
        stats_label = QLabel("Stats:")
        stats_label.setMaximumHeight(16)
        stats_layout.addWidget(stats_label)
        
        self.stats_label = QLabel("")
        stats_font = QFont()
        stats_font.setPointSize(7)  # Reduced from 8
        self.stats_label.setFont(stats_font)
        self.stats_label.setAlignment(Qt.AlignLeft)
        self.stats_label.setMaximumHeight(16)
        stats_layout.addWidget(self.stats_label)
        stats_layout.addStretch()
        
        layout.addLayout(stats_layout)
        
        # Set minimum size
        self.setMinimumWidth(350)  # Slightly reduced from 400
        self.setMinimumHeight(220)  # Reduced from 250
    
    def apply_styling(self):
        """Apply styling from config."""
        bg_color = self.config.styling.line_color or "#FFFFFF"
        text_color = self.config.styling.line_color or "#000000"
        
        # Try to parse hex color, fallback to defaults
        try:
            # Convert hex to RGB for stylesheet
            hex_bg = "FFFFFF"
            hex_text = "000000"
            
            # Apply basic frame styling
            self.setStyleSheet(f"""
                QFrame {{
                    background-color: #F5F5F5;
                    border: 1px solid #CCCCCC;
                    border-radius: 4px;
                }}
            """)
        except Exception as e:
            print(f"Error applying styling: {e}")
    
    def update_value(self, field_name: str, value: float, timestamp: Optional[float] = None) -> None:
        """
        Update a data value for this plot.
        
        Args:
            field_name: Name of the field being updated
            value: Numeric value to add
            timestamp: Unix timestamp (uses current time if not provided)
        """
        if field_name not in self.data_buffers:
            return
        
        if timestamp is None:
            timestamp = time.time()
        
        self.data_buffers[field_name].add_value(timestamp, value)
        self.update_indicator.setStyleSheet("color: #0066FF; font-size: 6px;")
        
        # Throttle rendering
        current_time = time.time()
        if current_time - self.last_render_time >= self.min_render_interval:
            self.refresh_plot()
            self.last_render_time = current_time
        
        self.data_updated.emit()
    
    def refresh_plot(self) -> None:
        """Refresh the plot display with current data."""
        if not hasattr(self, 'ax'):
            return
        
        try:
            # Get time range for x-axis
            min_time = None
            max_time = None
            
            for field_name, buffer in self.data_buffers.items():
                t_min, t_max = buffer.get_time_range()
                if t_min is not None:
                    if min_time is None or t_min < min_time:
                        min_time = t_min
                    if max_time is not None and t_max is not None and t_max > max_time:
                        max_time = t_max
                    elif max_time is None and t_max is not None:
                        max_time = t_max
            
            # Update each plot line
            for field_name, buffer in self.data_buffers.items():
                if field_name in self.plot_lines:
                    points = buffer.get_points()
                    
                    if points and min_time is not None and max_time is not None:
                        # Extract times and values
                        times_raw = [t - min_time for t, _ in points]
                        values = [v for _, v in points]
                        
                        # Apply right-to-left scrolling if enabled
                        if self.config.axis_config.scroll_right_to_left and (max_time - min_time) > 0:
                            # Invert times: 0 will be at the right (most recent)
                            time_window = max_time - min_time
                            times = [time_window - t for t in times_raw]
                        else:
                            # Standard left-to-right display
                            times = times_raw
                        
                        self.plot_lines[field_name].set_data(times, values)
            
            # Set up x-axis limits
            if max_time is not None and min_time is not None:
                # Check if x-axis auto-scaling is enabled
                if self.config.axis_config.x_axis_auto_scale:
                    # Auto-scale: use all available data (natural time range)
                    time_range = max_time - min_time
                else:
                    # Fixed: use the configured time window
                    time_range = self.config.time_window
                
                if self.config.axis_config.scroll_right_to_left:
                    # X-axis inverted: left=old (time_range), right=new (0)
                    # This creates intuitive left-to-right reading (historical→current)
                    self.ax.set_xlim(time_range, 0)  # Inverted range
                    self.ax.set_xlabel(f'Historical ← Time Ago (s) → Current')
                    # Move Y-axis to right side
                    self.ax.yaxis.tick_right()
                    self.ax.yaxis.set_label_position('right')
                else:
                    # Standard left-to-right
                    self.ax.set_xlim(0, time_range)
                    self.ax.set_xlabel('Time (s)')
                    # Keep Y-axis on left (standard matplotlib default)
                    self.ax.yaxis.tick_left()
                    self.ax.yaxis.set_label_position('left')
            
            # Auto-scale if enabled
            if self.config.axis_config.auto_scale:
                self.ax.relim()
                self.ax.autoscale_view()
            else:
                # Set fixed Y-axis range
                if self.config.axis_config.y_min is not None and self.config.axis_config.y_max is not None:
                    self.ax.set_ylim(
                        self.config.axis_config.y_min,
                        self.config.axis_config.y_max
                    )
            
            # Update statistics display
            self.update_statistics_display()
            
            # Redraw canvas
            self.canvas.draw()
            
        except Exception as e:
            print(f"Error refreshing plot: {e}")
    
    def update_statistics_display(self) -> None:
        """Update the statistics display label."""
        try:
            if len(self.data_buffers) == 1:
                # Single field
                field_name = list(self.data_buffers.keys())[0]
                buffer = self.data_buffers[field_name]
                stats = buffer.get_statistics()
                
                if stats['count'] > 0:
                    stats_text = (
                        f"n={stats['count']} | "
                        f"avg={stats['mean']:.2f} | "
                        f"min={stats['min']:.2f} | "
                        f"max={stats['max']:.2f}"
                    )
                else:
                    stats_text = "No data"
            else:
                # Multiple fields
                stats_items = []
                for field_name, buffer in self.data_buffers.items():
                    stats = buffer.get_statistics()
                    if stats['count'] > 0:
                        stats_items.append(f"{field_name[:8]}={stats['mean']:.2f}")
                
                stats_text = " | ".join(stats_items) if stats_items else "No data"
            
            self.stats_label.setText(stats_text)
        except Exception as e:
            print(f"Error updating statistics: {e}")
    
    def get_data_buffer(self, field_name: str) -> Optional[PlotDataBuffer]:
        """Get the data buffer for a specific field."""
        return self.data_buffers.get(field_name)
    
    def get_all_buffers(self) -> Dict[str, PlotDataBuffer]:
        """Get all data buffers."""
        return self.data_buffers.copy()
    
    def get_statistics(self) -> Dict[str, Dict[str, float]]:
        """
        Get statistics for all fields.
        
        Returns:
            Dictionary mapping field_name -> statistics dict
        """
        result = {}
        for field_name, buffer in self.data_buffers.items():
            result[field_name] = buffer.get_statistics()
        return result
    
    def clear_data(self) -> None:
        """Clear all data from all buffers."""
        for buffer in self.data_buffers.values():
            buffer.clear()
        self.refresh_plot()
    
    def update_config(self, new_config: PlotConfig) -> None:
        """
        Update the plot configuration and refresh display.
        
        Args:
            new_config: New PlotConfig to apply
        """
        self.config = new_config
        self.apply_styling()
        self.refresh_plot()
    
    def contextMenuEvent(self, event):
        """Show context menu on right-click."""
        menu = QMenu(self)
        
        edit_action = menu.addAction("Edit Plot")
        delete_action = menu.addAction("Delete Plot")
        
        action = menu.exec_(self.mapToGlobal(event.pos()))
        
        if action == edit_action:
            self.plot_edit_requested.emit(self.config.plot_id)
        elif action == delete_action:
            self.plot_delete_requested.emit(self.config.plot_id)
    
    def get_plot_bounds(self) -> Dict[str, float]:
        """
        Get the current plot bounds.
        
        Returns:
            Dictionary with min/max values and time range
        """
        result = {}
        
        for field_name, buffer in self.data_buffers.items():
            v_min, v_max = buffer.get_value_range()
            t_min, t_max = buffer.get_time_range()
            
            result[field_name] = {
                'value_min': v_min,
                'value_max': v_max,
                'time_min': t_min,
                'time_max': t_max,
            }
        
        return result
    
    def set_draggable(self, draggable: bool) -> None:
        """Enable or disable drag-and-drop reordering."""
        self.draggable = draggable
        if draggable:
            self.setAcceptDrops(True)
    
    def mousePressEvent(self, event):
        """Handle click on plot - left click for drag or selection, right click shows menu."""
        if event.button() == Qt.LeftButton:
            self.drag_start_pos = event.pos()
            # Don't emit clicked yet - wait to see if it's a drag
            return
        elif event.button() == Qt.RightButton:
            self.contextMenuEvent(event)
    
    def mouseMoveEvent(self, event):
        """Handle mouse move for drag initiation."""
        if not self.draggable or not self.drag_start_pos:
            super().mouseMoveEvent(event)
            return
        
        distance = (event.pos() - self.drag_start_pos).manhattanLength()
        
        # Start drag if moved more than 5 pixels
        if distance > 5:
            self.start_drag()
            self.drag_start_pos = None
    
    def start_drag(self) -> None:
        """Initiate drag-and-drop for reordering."""
        if not self.draggable:
            return
        
        mime_data = QMimeData()
        mime_data.setText(json.dumps({'plot_id': self.config.plot_id, 'action': 'reorder'}))
        
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        
        # Create a visual representation of the dragged plot
        pixmap = self.grab(QRect(0, 0, self.width(), self.height()))
        pixmap_scaled = pixmap.scaledToWidth(200, Qt.SmoothTransformation)
        drag.setPixmap(pixmap_scaled)
        drag.setHotSpot(pixmap_scaled.rect().center())
        
        # Emit signal
        self.drag_started.emit(self.config.plot_id)
        
        # Execute drag
        drag.exec_(Qt.MoveAction)
    
    def dragEnterEvent(self, event):
        """Accept drag enter if it's a plot reorder operation."""
        if self.draggable and event.mimeData().hasText():
            try:
                data = json.loads(event.mimeData().text())
                if data.get('action') == 'reorder' and data.get('plot_id'):
                    event.acceptProposedAction()
                    self.setStyleSheet("""
                        QFrame {
                            background-color: #E8F4F8;
                            border: 2px dashed #0066FF;
                            border-radius: 4px;
                        }
                    """)
                    return
            except (json.JSONDecodeError, AttributeError):
                pass
        event.ignore()
    
    def dragLeaveEvent(self, event):
        """Reset styling when drag leaves."""
        self.apply_styling()
    
    def dropEvent(self, event):
        """Handle plot drop for reordering."""
        self.apply_styling()
        
        if event.mimeData().hasText():
            try:
                data = json.loads(event.mimeData().text())
                if data.get('action') == 'reorder' and data.get('plot_id'):
                    event.acceptProposedAction()
                    # Emit signal to swap with this plot
                    self.drop_requested.emit(self.config.plot_id)
                    return
            except (json.JSONDecodeError, AttributeError):
                pass
        event.ignore()
    
    def mouseReleaseEvent(self, event):
        """Handle mouse release for non-drag clicks."""
        if event.button() == Qt.LeftButton and self.drag_start_pos:
            # This was a click, not a drag
            self.plot_clicked.emit(self.config.plot_id)
            self.drag_start_pos = None
        super().mouseReleaseEvent(event)
    
    def cleanup_matplotlib(self):
        """
        Cleanup matplotlib resources to prevent Qt access violations during plugin reload.
        Must be called before widget is deleted.
        """
        try:
            # Disconnect all signals to prevent them firing during cleanup
            self.blockSignals(True)
            
            # Clear plot lines
            if hasattr(self, 'plot_lines'):
                self.plot_lines.clear()
            
            # Clear axes
            if hasattr(self, 'ax') and self.ax:
                try:
                    self.ax.clear()
                    self.ax.cla()
                except Exception:
                    pass
            
            # Close figure and canvas
            if hasattr(self, 'canvas') and self.canvas:
                try:
                    self.canvas.figure.clear()
                    plt.close(self.canvas.figure)
                    self.canvas.close()
                except Exception:
                    pass
            
            if hasattr(self, 'figure') and self.figure:
                try:
                    plt.close(self.figure)
                except Exception:
                    pass
            
            # Clear data buffers
            if hasattr(self, 'data_buffers'):
                self.data_buffers.clear()
                
        except Exception as e:
            print(f"DEBUG: Error during matplotlib cleanup: {e}")
    
    def closeEvent(self, event):
        """Handle close event - cleanup matplotlib resources."""
        self.cleanup_matplotlib()
        super().closeEvent(event)
