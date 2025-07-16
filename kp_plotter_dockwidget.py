from qgis.PyQt.QtWidgets import QDockWidget, QVBoxLayout, QWidget, QComboBox, QLabel, QListWidget, QPushButton, QListWidgetItem, QAbstractItemView, QTabWidget, QHBoxLayout, QCheckBox
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject, QgsVectorLayer, QgsMapLayerProxyModel, QgsWkbTypes, QgsGeometry, QgsPointXY, QgsWkbTypes, QgsDistanceArea, QgsCoordinateTransform
from qgis.gui import QgsVertexMarker
# Matplotlib imports

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
import numpy as np

class KpPlotterDockWidget(QDockWidget):
    """
    Dockable widget for plotting KP-based data.
    """
    def __init__(self, iface, parent=None):
        super().__init__("KP Data Plotter", parent)
        self.iface = iface
        self.setObjectName("KpPlotterDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        # QSettings for persistence
        from qgis.PyQt.QtCore import QSettings
        self.settings = QSettings()

        self.canvas_cid = None  # Connection ID for canvas events
        self.marker = None  # The map marker instance
        self.vertical_line = None  # Crosshair on the plot
        self.features_geoms = []  # Store the geometries of the reference line features
        self.segment_lengths = [] # Store the lengths of the reference line segments
        self.line_length = 0 # Store the total length of the reference line
        self.distance_area = QgsDistanceArea() # For measurements

        # Main widget and tab layout
        self.tab_widget = QTabWidget()
        self.setWidget(self.tab_widget)

        # --- Setup Tab ---
        self.setup_tab = QWidget()
        self.setup_layout = QHBoxLayout(self.setup_tab)
        self.tab_widget.addTab(self.setup_tab, "Setup")

        # Left column: Data selection
        self.left_col_widget = QWidget()
        self.left_col_layout = QVBoxLayout(self.left_col_widget)
        self.left_col_layout.addWidget(QLabel("Reference Line Layer:"))
        self.line_layer_combo = QComboBox()
        self.left_col_layout.addWidget(self.line_layer_combo)
        self.left_col_layout.addWidget(QLabel("Data Table Layer:"))
        self.table_layer_combo = QComboBox()
        self.left_col_layout.addWidget(self.table_layer_combo)
        self.table_layer_combo.currentIndexChanged.connect(self.update_field_lists)
        self.left_col_layout.addWidget(QLabel("KP Field:"))
        self.kp_field_combo = QComboBox()
        self.left_col_layout.addWidget(self.kp_field_combo)
        self.left_col_layout.addWidget(QLabel("Data Fields (select one or more):"))
        self.data_fields_list = QListWidget()
        self.data_fields_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.left_col_layout.addWidget(self.data_fields_list)
        self.plot_button = QPushButton("Plot Data")
        self.left_col_layout.addWidget(self.plot_button)
        self.plot_button.clicked.connect(self.plot_data)

        # Right column: Settings
        self.right_col_widget = QWidget()
        self.right_col_layout = QVBoxLayout(self.right_col_widget)
        self.reverse_y_checkbox = QCheckBox("Reverse Y Axis")
        self.right_col_layout.addWidget(self.reverse_y_checkbox)
        self.reverse_kp_checkbox = QCheckBox("Reverse KP")
        self.reverse_kp_checkbox.setChecked(False)
        self.right_col_layout.addWidget(self.reverse_kp_checkbox)
        self.tooltip_checkbox = QCheckBox("Show Value Tooltips")
        self.tooltip_checkbox.setChecked(True)
        self.right_col_layout.addWidget(self.tooltip_checkbox)
        self.right_col_layout.addStretch(1)

        self.setup_layout.addWidget(self.left_col_widget)
        self.setup_layout.addWidget(self.right_col_widget)

        # --- Plot Tab ---
        self.plot_tab = QWidget()
        self.plot_layout = QVBoxLayout(self.plot_tab)
        self.tab_widget.addTab(self.plot_tab, "Plot")

        # Matplotlib canvas (maximized in plot tab)
        self.figure = Figure(figsize=(8, 5))
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, self)
        self.plot_layout.addWidget(self.toolbar)
        self.plot_layout.addWidget(self.canvas)

        # --- Help Tab ---
        self.help_tab = QWidget()
        self.help_layout = QVBoxLayout(self.help_tab)
        help_text = (
            "<b>Help & Instructions: KP Data Plotter</b>"
            "<ul>"
            "<li><b>Purpose:</b> Plot and explore KP-based (Kilometer Point) data along a reference line, with interactive map and chart tools.</li>"
            "<li><b>Workflow:</b>"
            "  <ol>"
            "    <li>Select a <b>Reference Line Layer</b> (must be a line geometry layer).</li>"
            "    <li>Select a <b>Data Table Layer</b> (table or vector layer with KP and data fields).</li>"
            "    <li>Choose the <b>KP Field</b> and one or more <b>Data Fields</b> to plot.</li>"
            "    <li>Click <b>Plot Data</b> to generate the chart and enable map/chart interactivity.</li>"
            "  </ol>"
            "</li>"
            "<li><b>Interactive Features:</b>"
            "  <ul>"
            "    <li>Hover over the plot to see a crosshair and map marker at the nearest KP.</li>"
            "    <li>Right-click the plot to zoom the map to the selected KP location.</li>"
            "    <li>Enable/disable value tooltips and reverse the Y axis using the checkboxes.</li>"
            "  </ul>"
            "</li>"
            "<li><b>Tips & Notes:</b>"
            "  <ul>"
            "    <li><b>CRS Requirement:</b> The <b>project CRS</b> and <b>reference line layer CRS</b> must match for correct marker placement. Reproject layers if needed.</li>"
            "    <li>All KP values are assumed to be in kilometers. The tool interpolates positions along the reference line using KP values.</li>"
            "    <li>If the plot or marker does not appear as expected, check that your data table contains valid numeric KP and data values.</li>"
            "    <li>Selections and settings are remembered between sessions for convenience.</li>"
            "  </ul>"
            "</li>"
            "<li><b>Troubleshooting:</b>"
            "  <ul>"
            "    <li>If no data appears, ensure you have selected valid layers and fields, and that your table contains data for the chosen KP and data fields.</li>"
            "    <li>If the marker is misaligned, verify that all layers use the same CRS as the project.</li>"
            "    <li>For large datasets, plotting may take a few seconds.</li>"
            "  </ul>"
            "</li>"
            "</ul>"
        )
        self.help_label = QLabel()
        self.help_label.setTextFormat(Qt.RichText)
        self.help_label.setWordWrap(True)
        self.help_label.setText(help_text)
        self.help_layout.addWidget(self.help_label)
        self.help_layout.addStretch(1)
        self.tab_widget.addTab(self.help_tab, "Help")


        self.populate_layer_combos()
        self.restore_user_settings()
        self.iface.projectRead.connect(self.populate_layer_combos)

        # Set default dock area to bottom if possible
        # This requires the main window reference from iface
        try:
            main_win = self.iface.mainWindow()
            # Remove from all dock areas first to avoid duplicate docking
            main_win.removeDockWidget(self)
            main_win.addDockWidget(Qt.BottomDockWidgetArea, self)
        except Exception:
            pass  # Fallback if iface.mainWindow() is not available

    def populate_layer_combos(self):
        """
        Populate the line and table layer combo boxes with current project layers.
        """
        self.line_layer_combo.clear()
        self.table_layer_combo.clear()
        table_ids = set()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                # Reference line: only line geometry
                if layer.geometryType() == QgsWkbTypes.LineGeometry:
                    self.line_layer_combo.addItem(layer.name(), layer.id())
                # Table: non-spatial (no geometry) or any vector layer
                is_table = (not layer.isSpatial()) or (layer.geometryType() == QgsWkbTypes.NullGeometry)
                if is_table and layer.id() not in table_ids:
                    self.table_layer_combo.addItem(layer.name(), layer.id())
                    table_ids.add(layer.id())
                # Optionally, also allow all vector layers (for flexibility, but avoid duplicates)
                elif layer.id() not in table_ids:
                    self.table_layer_combo.addItem(layer.name(), layer.id())
                    table_ids.add(layer.id())
        self.update_field_lists()
        # Restore selections if available
        self.restore_user_settings(layer_only=True)
    def save_user_settings(self):
        """Save user selections to QSettings."""
        self.settings.setValue("KpPlotter/line_layer_id", self.line_layer_combo.currentData())
        self.settings.setValue("KpPlotter/table_layer_id", self.table_layer_combo.currentData())
        self.settings.setValue("KpPlotter/kp_field", self.kp_field_combo.currentText())
        # Save selected data fields as a list
        data_fields = [item.text() for item in self.data_fields_list.selectedItems()]
        self.settings.setValue("KpPlotter/data_fields", data_fields)
        self.settings.setValue("KpPlotter/reverse_y", self.reverse_y_checkbox.isChecked())
        self.settings.setValue("KpPlotter/reverse_kp", self.reverse_kp_checkbox.isChecked())
        self.settings.setValue("KpPlotter/show_tooltips", self.tooltip_checkbox.isChecked())

    def restore_user_settings(self, layer_only=False):
        """Restore user selections from QSettings. If layer_only, only restore layer combos."""
        line_layer_id = self.settings.value("KpPlotter/line_layer_id", None)
        table_layer_id = self.settings.value("KpPlotter/table_layer_id", None)
        # Restore layer combos
        if line_layer_id:
            idx = self.line_layer_combo.findData(line_layer_id)
            if idx != -1:
                self.line_layer_combo.setCurrentIndex(idx)
        if table_layer_id:
            idx = self.table_layer_combo.findData(table_layer_id)
            if idx != -1:
                self.table_layer_combo.setCurrentIndex(idx)
        if layer_only:
            return
        # Restore KP field
        kp_field = self.settings.value("KpPlotter/kp_field", None)
        if kp_field:
            idx = self.kp_field_combo.findText(kp_field)
            if idx != -1:
                self.kp_field_combo.setCurrentIndex(idx)
        # Restore data fields
        data_fields = self.settings.value("KpPlotter/data_fields", [])
        if data_fields is None:
            data_fields = []
        if isinstance(data_fields, str):
            import ast
            try:
                data_fields = ast.literal_eval(data_fields)
            except Exception:
                data_fields = []
        if not isinstance(data_fields, (list, tuple)):
            data_fields = []
        found_any = False
        for i in range(self.data_fields_list.count()):
            item = self.data_fields_list.item(i)
            if item is not None and item.text() and item.text() in data_fields:
                item.setSelected(True)
                found_any = True
        # If no fields found, revert to default (select none)
        if not found_any and self.data_fields_list.count() > 0:
            for i in range(self.data_fields_list.count()):
                item = self.data_fields_list.item(i)
                if item is not None:
                    item.setSelected(False)
        # Restore checkboxes
        reverse_y = self.settings.value("KpPlotter/reverse_y", False, type=bool)
        self.reverse_y_checkbox.setChecked(reverse_y)
        reverse_kp = self.settings.value("KpPlotter/reverse_kp", False, type=bool)
        self.reverse_kp_checkbox.setChecked(reverse_kp)
        show_tooltips = self.settings.value("KpPlotter/show_tooltips", True, type=bool)
        self.tooltip_checkbox.setChecked(show_tooltips)

    def update_field_lists(self):
        """
        Update the field lists based on the selected table layer.
        """
        self.kp_field_combo.clear()
        self.data_fields_list.clear()

        layer_id = self.table_layer_combo.currentData()
        if not layer_id:
            return

        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer or not isinstance(layer, QgsVectorLayer):
            return

        for field in layer.fields():
            self.kp_field_combo.addItem(field.name())
            item = QListWidgetItem(field.name())
            self.data_fields_list.addItem(item)


    def plot_data(self):
        """
        Plot the selected KP-based data on the chart.
        Uses merged line geometry and interpolation logic matching the Place KP Points tool for consistency.
        """
        # Clear previous plot
        self.cleanup_plot_and_marker()
        ax = self.figure.add_subplot(111)

        # Get selected layers and fields
        line_layer_id = self.line_layer_combo.currentData()
        table_layer_id = self.table_layer_combo.currentData()
        kp_field = self.kp_field_combo.currentText()
        data_fields = [item.text() for item in self.data_fields_list.selectedItems()]

        if not table_layer_id or not kp_field or not data_fields or not line_layer_id:
            ax.set_title("Please select line, table, KP field, and at least one data field.")
            self.canvas.draw()
            return

        # Save user settings on plot
        self.save_user_settings()

        line_layer = QgsProject.instance().mapLayer(line_layer_id)
        if not line_layer or not isinstance(line_layer, QgsVectorLayer) or line_layer.geometryType() != QgsWkbTypes.LineGeometry:
            ax.set_title("Invalid reference line layer.")
            self.canvas.draw()
            return

        # Set up distance area
        project_crs = QgsProject.instance().crs()
        self.distance_area.setSourceCrs(project_crs, QgsProject.instance().transformContext())
        self.distance_area.setEllipsoid(QgsProject.instance().ellipsoid())

        # --- Merge all line features into a single geometry (like Place KP Points tool) ---
        line_features = [f for f in line_layer.getFeatures()]
        if not line_features:
            ax.set_title("Reference line layer has no features.")
            self.canvas.draw()
            return
        geometries = [f.geometry() for f in line_features]
        merged_geometry = QgsGeometry.unaryUnion(geometries)
        if merged_geometry.isEmpty():
            ax.set_title("Reference line geometry is empty after merging.")
            self.canvas.draw()
            return

        # Cache merged geometry and its total length
        self.merged_geometry = merged_geometry
        self.line_length = self.distance_area.measureLength(merged_geometry)

        # For interpolation, get all parts as polylines
        self.line_parts = merged_geometry.asMultiPolyline() if merged_geometry.isMultipart() else [merged_geometry.asPolyline()]

        table_layer = QgsProject.instance().mapLayer(table_layer_id)
        if not table_layer or not isinstance(table_layer, QgsVectorLayer):
            ax.set_title("Invalid table layer.")
            self.canvas.draw()
            return

        # Extract data efficiently
        kp_values = []
        series = {field: [] for field in data_fields}
        for feat in table_layer.getFeatures():
            try:
                kp = float(feat[kp_field])
            except Exception:
                continue
            kp_values.append(kp)
            for field in data_fields:
                try:
                    val = float(feat[field])
                except Exception:
                    val = None
                series[field].append(val)

        if not kp_values or not any(series.values()):
            ax.set_title("No valid data to plot.")
            self.canvas.draw()
            return

        # Sort by KP, and reverse if needed
        zipped = list(zip(kp_values, *[series[field] for field in data_fields]))
        zipped.sort()
        if self.reverse_kp_checkbox.isChecked():
            zipped = list(reversed(zipped))
        self.kp_sorted = [row[0] for row in zipped]  # Store for snapping
        for idx, field in enumerate(data_fields):
            y_sorted = [row[idx+1] for row in zipped]
            ax.plot(self.kp_sorted, y_sorted, label=field)

        self.vertical_line = ax.axvline(x=self.kp_sorted[0], color='k', linestyle='--', lw=1)

        ax.set_xlabel("KP")
        ax.set_ylabel("Value")
        ax.legend()
        ax.grid(True)
        if self.reverse_y_checkbox.isChecked():
            ax.invert_yaxis()
        try:
            self.figure.tight_layout()
        except Exception:
            pass
        self.canvas.draw()
        self.tab_widget.setCurrentWidget(self.plot_tab)
        self.connect_canvas_events()
        if self.tooltip_checkbox.isChecked():
            self.enable_tooltips()
        else:
            self.disable_tooltips()

    def enable_tooltips(self):
        if not hasattr(self, '_tooltip_cid') or self._tooltip_cid is None:
            self._tooltip_cid = self.canvas.mpl_connect('motion_notify_event', self.show_tooltip)

    def disable_tooltips(self):
        if hasattr(self, '_tooltip_cid') and self._tooltip_cid is not None:
            self.canvas.mpl_disconnect(self._tooltip_cid)
            self._tooltip_cid = None

    def show_tooltip(self, event):
        # Only show tooltip if in axes and data exists
        if not event.inaxes:
            self.canvas.setToolTip("")
            return
        ax = event.inaxes
        # Gather all lines and their data
        lines = ax.get_lines()
        if not lines:
            self.canvas.setToolTip("")
            return
        # Assume all lines share the same xdata (KP)
        # Find the closest KP to the mouse
        xdata = lines[0].get_xdata()
        if len(xdata) == 0:
            self.canvas.setToolTip("")
            return
        mouse_x = event.xdata
        min_dist = float('inf')
        idx_closest = None
        for i, x in enumerate(xdata):
            if x is None:
                continue
            dist = abs(mouse_x - x)
            if dist < min_dist:
                min_dist = dist
                idx_closest = i
        # Only show tooltip if close enough
        if idx_closest is None or min_dist > 0.05 * (ax.get_xlim()[1] - ax.get_xlim()[0]):
            self.canvas.setToolTip("")
            return
        # Build tooltip: KP and all series values
        tooltip_lines = [f"KP: {xdata[idx_closest]:.2f}"]
        for line in lines:
            label = line.get_label()
            ydata = line.get_ydata()
            if idx_closest < len(ydata):
                yval = ydata[idx_closest]
                if yval is not None:
                    tooltip_lines.append(f"{label}: {yval:.2f}")
        tooltip_text = "\n".join(tooltip_lines)
        self.canvas.setToolTip(tooltip_text)

    # Update tooltip event connection when checkbox is toggled
    def toggle_tooltip_checkbox(self):
        if self.tooltip_checkbox.isChecked():
            self.enable_tooltips()
        else:
            self.disable_tooltips()
        # Connect tooltip checkbox
        self.tooltip_checkbox.toggled.connect(self.toggle_tooltip_checkbox)

    def connect_canvas_events(self):
        """Connect mouse motion events to the canvas."""
        if not self.canvas_cid:
            self.canvas_cid = self.canvas.mpl_connect('motion_notify_event', self.on_mouse_move)
        # Connect right-click event for zooming map canvas to KP
        if not hasattr(self, '_right_click_cid') or self._right_click_cid is None:
            self._right_click_cid = self.canvas.mpl_connect('button_press_event', self.on_canvas_right_click)

    def disconnect_canvas_events(self):
        """Disconnect mouse motion events."""
        if self.canvas_cid:
            self.canvas.mpl_disconnect(self.canvas_cid)
            self.canvas_cid = None
        if hasattr(self, '_right_click_cid') and self._right_click_cid is not None:
            self.canvas.mpl_disconnect(self._right_click_cid)
            self._right_click_cid = None
    def on_canvas_right_click(self, event):
        """Handle right-click on the plot: zoom map canvas to the corresponding KP point on the line."""
        if event.button != 3:  # Right mouse button
            return
        if not event.inaxes:
            return
        mouse_kp = event.xdata
        if mouse_kp is None:
            return
        # Snap to nearest KP value
        if not hasattr(self, 'kp_sorted') or not self.kp_sorted:
            return
        nearest_kp = min(self.kp_sorted, key=lambda x: abs(x - mouse_kp))
        # Interpolate point on line
        if not hasattr(self, 'merged_geometry') or not hasattr(self, 'line_parts') or not self.line_parts:
            return
        distance_m = nearest_kp * 1000
        point_geom = self.interpolate_point_along_line(distance_m)
        if point_geom is None or point_geom.isEmpty():
            return
        point = point_geom.asPoint()
        # Zoom map canvas to this point
        canvas = self.iface.mapCanvas()
        canvas.setCenter(point)
        # Set a reasonable zoom scale (e.g., 1:5000) or keep current scale
        # Uncomment the next line to always zoom to a fixed scale:
        # canvas.zoomScale(5000)
        canvas.refresh()

    def on_mouse_move(self, event):
        """Handle mouse movement on the plot, snapping to nearest KP value."""
        # Hide marker and crosshair if mouse leaves plot area
        if not event.inaxes:
            if self.marker and self.marker.isVisible():
                self.marker.hide()
                self.iface.mapCanvas().refresh()
            if self.vertical_line and self.vertical_line.get_visible():
                self.vertical_line.set_visible(False)
                self.canvas.draw_idle()
            return

        # Show marker and crosshair if they were hidden
        if self.marker and not self.marker.isVisible():
            self.marker.show()
        if self.vertical_line and not self.vertical_line.get_visible():
            self.vertical_line.set_visible(True)

        # Use merged geometry for marker/crosshair logic
        if not hasattr(self, 'merged_geometry') or not hasattr(self, 'line_parts') or not self.line_parts or not hasattr(self, 'kp_sorted') or not self.kp_sorted:
            return

        mouse_kp = event.xdata
        if mouse_kp is None:
            return
        # Snap to nearest KP value
        nearest_kp = min(self.kp_sorted, key=lambda x: abs(x - mouse_kp))
        self.update_crosshair(nearest_kp)
        self.update_map_marker(nearest_kp)

    def update_crosshair(self, kp):
        """Update the vertical line on the plot."""
        if self.vertical_line:
            self.vertical_line.set_xdata([kp, kp])
            self.canvas.draw_idle()

    def interpolate_point_along_line(self, distance_m):
        """Interpolate a point along the merged reference line at the given measured distance in meters (matches Place KP Points logic). Supports reverse KP."""
        if not hasattr(self, 'merged_geometry') or not hasattr(self, 'line_parts') or not self.line_parts:
            return None

        # If reverse KP is checked, measure from the other end
        if self.reverse_kp_checkbox.isChecked():
            distance_m = self.line_length - distance_m

        # Handle boundary cases
        if distance_m <= 0:
            first_point = self.line_parts[0][0]
            return QgsGeometry.fromPointXY(first_point)

        if distance_m >= self.line_length:
            last_part = self.line_parts[-1]
            last_point = last_part[-1]
            return QgsGeometry.fromPointXY(last_point)

        cumulative_length = 0.0
        for part in self.line_parts:
            for i in range(len(part) - 1):
                p1, p2 = part[i], part[i+1]
                segment_length = self.distance_area.measureLine(p1, p2)
                if cumulative_length + segment_length >= distance_m:
                    dist_into_segment = distance_m - cumulative_length
                    ratio = dist_into_segment / segment_length if segment_length > 0 else 0
                    x = p1.x() + ratio * (p2.x() - p1.x())
                    y = p1.y() + ratio * (p2.y() - p1.y())
                    interp_xy = QgsPointXY(x, y)
                    return QgsGeometry.fromPointXY(interp_xy)
                cumulative_length += segment_length
        # If not found (should not happen)
        return None

    def update_map_marker(self, kp):
        """Update the marker on the map canvas using merged geometry."""
        if not hasattr(self, 'merged_geometry') or not hasattr(self, 'line_parts') or not self.line_parts:
            return

        # Convert KP (kilometers) to meters for distance calculation
        distance_m = kp * 1000

        # Do not proceed if the distance is outside the line's bounds
        if not (0 <= distance_m <= self.line_length):
            if self.marker and self.marker.isVisible():
                self.marker.hide()
                self.iface.mapCanvas().refresh()
            return

        # Create marker on first valid move and add it to the scene
        if not self.marker:
            self.marker = QgsVertexMarker(self.iface.mapCanvas())
            self.marker.setColor(Qt.red)
            self.marker.setIconSize(12)
            self.marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self.marker.setPenWidth(3)
            self.iface.mapCanvas().scene().addItem(self.marker)

        if not self.marker.isVisible():
            self.marker.show()

        point_on_line = self.interpolate_point_along_line(distance_m)
        if point_on_line is None or point_on_line.isEmpty():
            if self.marker.isVisible():
                self.marker.hide()
                self.iface.mapCanvas().refresh()
            return

        self.marker.setCenter(point_on_line.asPoint())
        self.iface.mapCanvas().refresh()

    def cleanup_plot_and_marker(self):
        """Clear the plot, disconnect events, and robustly remove all markers."""
        self.disconnect_canvas_events()

        # Remove the tracked marker if present
        if self.marker:
            try:
                self.iface.mapCanvas().scene().removeItem(self.marker)
            except Exception:
                pass
            self.marker = None
            self.iface.mapCanvas().refresh()

        # Robust: Remove any stray QgsVertexMarker from the scene
        scene = self.iface.mapCanvas().scene()
        items_to_remove = []
        for item in scene.items():
            # QgsVertexMarker has setCenter and setIconType methods
            if hasattr(item, 'setCenter') and hasattr(item, 'setIconType'):
                items_to_remove.append(item)
        for item in items_to_remove:
            try:
                scene.removeItem(item)
            except Exception:
                pass
        if items_to_remove:
            self.iface.mapCanvas().refresh()

        self.figure.clear()
        self.vertical_line = None
        self.features_geoms = []
        self.segment_lengths = []
        self.line_length = 0
        self.canvas.draw()

    def showEvent(self, event):
        """
        Handle the widget being shown.
        """
        super().showEvent(event)
        self.populate_layer_combos()
        
    def closeEvent(self, event):
        """Handle the widget being closed."""
        self.save_user_settings()
        self.cleanup_plot_and_marker()
        # Safely disconnect the signal
        try:
            self.iface.projectRead.disconnect(self.populate_layer_combos)
        except TypeError:
            pass  # Signal was not connected
        super().closeEvent(event)