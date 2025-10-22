from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QSpinBox, QCheckBox, QFileDialog, QTabWidget, QFormLayout, QSizePolicy
)
from qgis.PyQt.QtCore import Qt, QSettings, QTimer
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer, QgsWkbTypes, QgsGeometry, QgsPointXY,
    QgsDistanceArea, QgsFeatureRequest, QgsCoordinateTransform
)
from qgis.gui import QgsVertexMarker, QgsRubberBand
from .maptools.temp_line_maptool import TempLineMapTool  # new temporary line drawing tool

# Added standard library & third-party imports
import math
import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas, NavigationToolbar2QT as NavigationToolbar

# Simple sip deletion check fallback
def _sip_isdeleted(obj):  # pragma: no cover
    return False


class DepthProfileDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Depth Profile", parent)
        self.iface = iface
        self.setObjectName("DepthProfileDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        self.settings = QSettings()
        # Internal runtime state
        self.line_parts = []
        self.line_length = 0.0
        self.distance_area = QgsDistanceArea()
        self.marker = None
        self.vertical_line = None
        self.vertical_line2 = None  # for dual plot
        self.canvas_cid = None
        self._right_click_cid = None
        self._tooltip_cid = None
        self.kp_values = []
        self.depth_values = []
        self.slope_deg = []
        self.slope_pct = []
        # Temporary line drawing state
        self.temp_drawn_points = []  # list of QgsPointXY in project CRS
        self.temp_line_tool = None
        self.using_drawn_line = False
        self.current_line_crs = None
        self.temp_line_rubber = None  # persistent rubber band showing drawn line
        # Seabed length (3D) calculation
        self.seabed_length = 0.0
        self.sampled_xyz = []  # List of (x, y, z) tuples for 3D length
        # Tab widget structure
        self.tab_widget = QTabWidget()
        self.setWidget(self.tab_widget)
        # --- Setup Tab ---
        self.setup_tab = QWidget()
        setup_layout = QVBoxLayout(self.setup_tab)
        self.tab_widget.addTab(self.setup_tab, "Setup")
        
        # Use QFormLayout for better alignment
        form_layout = QFormLayout()
        setup_layout.addLayout(form_layout)
        
        # Row 1: Route Line Layer and Depth Source
        line_row = QHBoxLayout()
        line_row.addWidget(QLabel("Route Line Layer:"))
        self.line_layer_combo = QComboBox()
        self.line_layer_combo.setMinimumWidth(120)
        line_row.addWidget(self.line_layer_combo)
        self.use_drawn_chk = QCheckBox("Use Drawn")
        line_row.addWidget(self.use_drawn_chk)
        self.draw_line_btn = QPushButton("Draw Line")
        line_row.addWidget(self.draw_line_btn)
        self.clear_drawn_btn = QPushButton("Clear")
        self.clear_drawn_btn.setEnabled(False)
        line_row.addWidget(self.clear_drawn_btn)
        line_row.addStretch()
        form_layout.addRow(line_row)
        
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Depth Source:"))
        self.source_type_combo = QComboBox()
        self.source_type_combo.addItems(["Raster", "Contours"])
        self.source_type_combo.setMinimumWidth(80)
        source_row.addWidget(self.source_type_combo)
        source_row.addStretch()
        form_layout.addRow(source_row)
        
        # Row 2: Raster and Contour Layers
        raster_row = QHBoxLayout()
        raster_row.addWidget(QLabel("Raster Layer:"))
        self.raster_layer_combo = QComboBox()
        self.raster_layer_combo.setMinimumWidth(120)
        raster_row.addWidget(self.raster_layer_combo)
        raster_row.addStretch()
        form_layout.addRow(raster_row)
        
        contour_row = QHBoxLayout()
        contour_row.addWidget(QLabel("Contour Layer:"))
        self.contour_layer_combo = QComboBox()
        self.contour_layer_combo.setMinimumWidth(120)
        contour_row.addWidget(self.contour_layer_combo)
        contour_row.addWidget(QLabel("Elevation Field:"))
        self.elev_field_combo = QComboBox()
        self.elev_field_combo.setMinimumWidth(100)
        contour_row.addWidget(self.elev_field_combo)
        contour_row.addStretch()
        form_layout.addRow(contour_row)
        
        # Row 3: Sampling and Options
        sampling_row = QHBoxLayout()
        sampling_row.addWidget(QLabel("Sampling Interval (m):"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 50000)
        self.interval_spin.setValue(int(self.settings.value("DepthProfile/interval_m", 50)))
        sampling_row.addWidget(self.interval_spin)
        self.auto_limit_chk = QCheckBox("Auto Limit")
        self.auto_limit_chk.setChecked(bool(self.settings.value("DepthProfile/auto_limit_samples", True, type=bool)))
        sampling_row.addWidget(self.auto_limit_chk)
        sampling_row.addWidget(QLabel("Max Samples:"))
        self.max_samples_spin = QSpinBox()
        self.max_samples_spin.setRange(1000, 5000000)
        self.max_samples_spin.setSingleStep(1000)
        self.max_samples_spin.setValue(int(self.settings.value("DepthProfile/max_samples", 50000)))
        self.max_samples_spin.setToolTip("Maximum allowed sample points along route. If exceeded and Auto Limit is on, interval increases.")
        sampling_row.addWidget(self.max_samples_spin)
        sampling_row.addStretch()
        form_layout.addRow(sampling_row)
        
        options_row = QHBoxLayout()
        self.interpolate_contours_chk = QCheckBox("Interpolate Between Contours")
        self.interpolate_contours_chk.setChecked(bool(self.settings.value("DepthProfile/interp_contours", True, type=bool)))
        options_row.addWidget(self.interpolate_contours_chk)
        options_row.addStretch()
        form_layout.addRow(options_row)
        
        # Row 4: Plot Options
        plot_row = QHBoxLayout()
        plot_row.addWidget(QLabel("Plot Variable:"))
        self.variable_combo = QComboBox()
        self.variable_combo.addItems(["Depth (m)", "Slope (deg)", "Slope (%)"])
        self.variable_combo.setMinimumWidth(100)
        last_var = self.settings.value("DepthProfile/variable", "Depth (m)")
        idx = self.variable_combo.findText(last_var)
        if idx != -1:
            self.variable_combo.setCurrentIndex(idx)
        plot_row.addWidget(self.variable_combo)
        self.dual_plot_chk = QCheckBox("Depth + Slope")
        self.dual_plot_chk.setChecked(bool(self.settings.value("DepthProfile/dual_plot", False, type=bool)))
        plot_row.addWidget(self.dual_plot_chk)
        self.slope_unit_combo = QComboBox()
        self.slope_unit_combo.addItems(["Slope (deg)", "Slope (%)"])
        self.slope_unit_combo.setMinimumWidth(100)
        last_slope_unit = self.settings.value("DepthProfile/slope_unit", "Slope (deg)")
        si = self.slope_unit_combo.findText(last_slope_unit)
        if si != -1:
            self.slope_unit_combo.setCurrentIndex(si)
        plot_row.addWidget(self.slope_unit_combo)
        plot_row.addStretch()
        form_layout.addRow(plot_row)
        
        # Checkboxes row
        checkboxes_row = QHBoxLayout()
        self.reverse_kp_chk = QCheckBox("Reverse KP")
        self.reverse_kp_chk.setChecked(bool(self.settings.value("DepthProfile/reverse_kp", False, type=bool)))
        checkboxes_row.addWidget(self.reverse_kp_chk)
        self.invert_depth_axis_chk = QCheckBox("Invert Depth Axis")
        self.invert_depth_axis_chk.setChecked(True)
        checkboxes_row.addWidget(self.invert_depth_axis_chk)
        self.invert_slope_chk = QCheckBox("Invert Slope")
        self.invert_slope_chk.setChecked(bool(self.settings.value("DepthProfile/invert_slope", True, type=bool)))
        checkboxes_row.addWidget(self.invert_slope_chk)
        self.show_tooltips_chk = QCheckBox("Tooltips")
        self.show_tooltips_chk.setChecked(True)
        checkboxes_row.addWidget(self.show_tooltips_chk)
        checkboxes_row.addStretch()
        form_layout.addRow(checkboxes_row)
        
        # Add stretch to push buttons to bottom
        setup_layout.addStretch()
        
        # Buttons
        button_layout = QHBoxLayout()
        self.generate_btn = QPushButton("Generate Profile")
        button_layout.addWidget(self.generate_btn)
        self.export_dxf_btn = QPushButton("Export DXF")
        button_layout.addWidget(self.export_dxf_btn)
        self.export_csv_btn = QPushButton("Export CSV")
        button_layout.addWidget(self.export_csv_btn)
        button_layout.addStretch()
        setup_layout.addLayout(button_layout)
        # --- Depth Profile Tab ---
        self.profile_tab = QWidget()
        profile_layout = QVBoxLayout(self.profile_tab)
        self.tab_widget.addTab(self.profile_tab, "Depth Profile")
        # Matplotlib area
        self.figure = Figure(figsize=(6, 4)); self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.toolbar = NavigationToolbar(self.canvas, self)
        profile_layout.addWidget(self.toolbar); profile_layout.addWidget(self.canvas, 1)
        # --- Help Tab ---
        self.help_tab = QWidget()
        help_layout = QVBoxLayout(self.help_tab)
        help_text = (
            "<b>Help & Instructions: Depth Profile Tool</b>"
            "<ul>"
            "<li><b>Purpose:</b> Generate and plot a depth (or slope) profile along a cable route using raster or contour data."
            "</li>"
            "<li><b>Workflow:</b>"
            "  <ol>"
            "    <li>Select a <b>Route Line Layer</b> (must be a line geometry layer representing the cable route).</li>"
            "    <li>Or, to use a temporary line: check <b>Use Drawn</b> and click <b>Draw Line</b>. On the map, left-click to place points, right-click or double-click to finish. Then select a bathymetry layer and click <b>Generate Profile</b>.</li>"
            "    <li>Choose the <b>Depth Source</b>: either a raster (e.g., MBES) or a contour vector layer.</li>"
            "    <li>For raster, select the <b>Raster Layer</b>. For contours, select the <b>Contour Layer</b> and <b>Elevation Field</b>.</li>"
            "    <li>Set the <b>Sampling Interval</b> (meters) and other options as needed.</li>"
            "    <li>Click <b>Generate Profile</b> to plot depth, slope, or both along the route.</li>"
            "  </ol>"
            "</li>"
            "<li><b>Features:</b>"
            "  <ul>"
            "    <li>Supports both raster and contour-based depth sources.</li>"
            "    <li>Option to draw a temporary route line directly on the map.</li>"
            "    <li>Dual plot mode for depth and slope together.</li>"
            "    <li>Interactive plot with map marker and crosshair synced to KP.</li>"
            "    <li>Export profile to DXF for CAD/GIS use.</li>"
            "  </ul>"
            "</li>"
            "<li><b>Tips & Notes:</b>"
            "  <ul>"
            "    <li>Ensure all layers use the same CRS as the project for correct marker placement.</li>"
            "    <li>Sampling interval and max samples affect performance and detail.</li>"
            "    <li>For large datasets, plotting may take a few seconds.</li>"
            "    <li>Selections and settings are remembered between sessions.</li>"
            "  </ul>"
            "</li>"
            "<li><b>Troubleshooting:</b>"
            "  <ul>"
            "    <li>If no data appears, check that you have selected valid layers and fields, and that your data contains valid numeric values.</li>"
            "    <li>If the marker is misaligned, verify that all layers use the same CRS as the project.</li>"
            "  </ul>"
            "</li>"
            "</ul>"
        )
        help_label = QLabel()
        help_label.setTextFormat(Qt.RichText)
        help_label.setWordWrap(True)
        help_label.setText(help_text)
        help_layout.addWidget(help_label)
        help_layout.addStretch(1)
        self.tab_widget.addTab(self.help_tab, "Help")
        # Connections
        self.generate_btn.clicked.connect(self.generate_profile)
        self.source_type_combo.currentIndexChanged.connect(self.update_enable_states)
        self.contour_layer_combo.currentIndexChanged.connect(self.populate_elevation_fields)
        self.show_tooltips_chk.toggled.connect(self.toggle_tooltips)
        self.dual_plot_chk.toggled.connect(self.update_enable_states)
        # New connections for drawn line
        self.draw_line_btn.clicked.connect(self.activate_temp_line_tool)
        self.clear_drawn_btn.clicked.connect(self.clear_drawn_line)
        self.use_drawn_chk.toggled.connect(self.update_enable_states)
        # DXF export
        self.export_dxf_btn.clicked.connect(self.export_dxf)
        # CSV export
        self.export_csv_btn.clicked.connect(self.export_csv)
        # Populate & signals
        # Layer combo population + reactive updates
        self._pending_layer_refresh = False
        self.populate_layer_combos(); self.update_enable_states()
        # Project/layer signals: use a debounced scheduler so rapid batch adds only trigger one refresh
        self.iface.projectRead.connect(self.populate_layer_combos)
        proj = QgsProject.instance()
        try:
            proj.layerWasAdded.connect(self.on_layer_event)
        except Exception:
            pass
        try:
            proj.layersAdded.connect(self.on_layers_added)
        except Exception:
            pass
        try:
            proj.layerRemoved.connect(self.on_layer_event)
        except Exception:
            pass
        try:
            proj.layersRemoved.connect(self.on_layer_event)
        except Exception:
            pass
        # Dock placement (only once)
        try:
            main_win = self.iface.mainWindow(); main_win.removeDockWidget(self); main_win.addDockWidget(Qt.BottomDockWidgetArea, self)
        except Exception:
            pass

    # ---------------------- UI population ----------------------
    def populate_layer_combos(self):
        prev_line = self.line_layer_combo.currentData()
        prev_raster = self.raster_layer_combo.currentData()
        prev_contour = self.contour_layer_combo.currentData()
        self.line_layer_combo.blockSignals(True)
        self.raster_layer_combo.blockSignals(True)
        self.contour_layer_combo.blockSignals(True)
        try:
            self.line_layer_combo.clear()
            self.raster_layer_combo.clear()
            self.contour_layer_combo.clear()
            for layer in QgsProject.instance().mapLayers().values():
                if isinstance(layer, QgsVectorLayer) and layer.geometryType() == QgsWkbTypes.LineGeometry:
                    self.line_layer_combo.addItem(layer.name(), layer.id())
                if isinstance(layer, QgsRasterLayer):
                    self.raster_layer_combo.addItem(layer.name(), layer.id())
                if isinstance(layer, QgsVectorLayer) and layer.geometryType() == QgsWkbTypes.LineGeometry:
                    self.contour_layer_combo.addItem(layer.name(), layer.id())
        finally:
            self.line_layer_combo.blockSignals(False)
            self.raster_layer_combo.blockSignals(False)
            self.contour_layer_combo.blockSignals(False)
        # Restore selections where possible
        if prev_line:
            idx = self.line_layer_combo.findData(prev_line)
            if idx != -1:
                self.line_layer_combo.setCurrentIndex(idx)
        if prev_raster:
            idx = self.raster_layer_combo.findData(prev_raster)
            if idx != -1:
                self.raster_layer_combo.setCurrentIndex(idx)
        if prev_contour:
            idx = self.contour_layer_combo.findData(prev_contour)
            if idx != -1:
                self.contour_layer_combo.setCurrentIndex(idx)
        self.populate_elevation_fields()

    def populate_elevation_fields(self):
        self.elev_field_combo.clear()
        layer_id = self.contour_layer_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if layer and isinstance(layer, QgsVectorLayer):
            for f in layer.fields():
                # Heuristic: prefer numeric fields
                if f.typeName().lower() in ("real", "double", "float", "integer", "int"):
                    self.elev_field_combo.addItem(f.name())
                else:
                    self.elev_field_combo.addItem(f.name())

    def update_enable_states(self):
        raster_mode = self.source_type_combo.currentText() == "Raster"
        self.raster_layer_combo.setEnabled(raster_mode)
        self.interval_spin.setEnabled(raster_mode)
        if hasattr(self, 'auto_limit_chk'):
            self.auto_limit_chk.setEnabled(raster_mode)
        if hasattr(self, 'max_samples_spin'):
            self.max_samples_spin.setEnabled(raster_mode)
        self.contour_layer_combo.setEnabled(not raster_mode)
        self.elev_field_combo.setEnabled(not raster_mode)
        self.interpolate_contours_chk.setEnabled(not raster_mode)
        # Dual plot disables single variable picker
        dual = getattr(self, 'dual_plot_chk', None) and self.dual_plot_chk.isChecked()
        if getattr(self, 'variable_combo', None):
            self.variable_combo.setEnabled(not dual)
        if getattr(self, 'slope_unit_combo', None):
            self.slope_unit_combo.setEnabled(dual)
        # Drawn line usage controls & UX
        want_drawn = self.use_drawn_chk.isChecked()
        has_drawn = bool(self.temp_drawn_points)
        if want_drawn:
            # Disable line layer selection while in drawn mode
            self.line_layer_combo.setEnabled(False)
            self.line_layer_combo.setToolTip("Using temporary drawn line")
            # Draw button enabled to allow (re)draw; clear enabled only if a line exists
            self.draw_line_btn.setEnabled(True)
            self.clear_drawn_btn.setEnabled(has_drawn)
            if not has_drawn:
                self.use_drawn_chk.setToolTip("Checked: provide a drawn line. Click 'Draw Line' to digitize.")
            else:
                self.use_drawn_chk.setToolTip("Using drawn line (points: %d)" % len(self.temp_drawn_points))
            # Show rubber band if present (may have been hidden when unchecked)
            try:
                if self.temp_line_rubber:
                    self.temp_line_rubber.show()
            except Exception:
                pass
        else:
            # Normal mode: enable line layer, disable draw controls
            self.line_layer_combo.setEnabled(True)
            self.line_layer_combo.setToolTip("Select a route line layer")
            self.draw_line_btn.setEnabled(False)
            # Keep any previously drawn line available for reuse; allow user to Clear explicitly
            self.clear_drawn_btn.setEnabled(has_drawn)
            if has_drawn:
                self.use_drawn_chk.setToolTip("A drawn line is stored (%d pts). Re-check to use it or Clear to discard." % len(self.temp_drawn_points))
            else:
                self.use_drawn_chk.setToolTip("Check to use a temporary drawn line instead of a layer")
            # Hide (but do not delete) rubber band when not actively using drawn line to prevent visual clutter
            try:
                if self.temp_line_rubber:
                    self.temp_line_rubber.hide()
            except Exception:
                pass

    def schedule_layer_combo_refresh(self, delay_ms=0):
        """Debounce population so multiple layer signals in quick succession refresh once.

        delay_ms: small delay allows layer providers to finish initialization so
        geometry type & fields are ready (important for newly added layers).
        """
        if self._pending_layer_refresh:
            return
        self._pending_layer_refresh = True
        def _do():
            try:
                self.populate_layer_combos()
            finally:
                self._pending_layer_refresh = False
        QTimer.singleShot(delay_ms, _do)

    def on_layer_event(self, *args):  # generic add/remove event
        # Slight delay helps when adding from large sources
        self.schedule_layer_combo_refresh(delay_ms=100)

    def on_layers_added(self, layers):  # noqa: D401
        # For bulk adds trigger a single delayed refresh
        self.schedule_layer_combo_refresh(delay_ms=150)

    # ---------------------- Core generation ----------------------
    def generate_profile(self):
        """Generate depth/slope profile and plot.

        Steps:
        1. Determine route geometry (layer or drawn line).
        2. Sample depth values (raster or contours).
        3. Compute slope & seabed 3D length.
        4. Plot (single or dual) and update interactivity.
        """
        self.clear_plot()
        ax = self.figure.add_subplot(111)

        # 1. Route geometry
        self.using_drawn_line = self.use_drawn_chk.isChecked() and bool(self.temp_drawn_points)
        project = QgsProject.instance()
        if self.using_drawn_line:
            if len(self.temp_drawn_points) < 2:
                ax.set_title("Drawn line must have at least 2 points"); self.canvas.draw(); return
            self.line_parts = [self.temp_drawn_points]
            self.distance_area.setSourceCrs(project.crs(), project.transformContext())
            self.distance_area.setEllipsoid(project.ellipsoid())
            length = 0.0
            for a, b in zip(self.temp_drawn_points[:-1], self.temp_drawn_points[1:]):
                length += self.distance_area.measureLine(a, b)
            self.line_length = length
            self.current_line_crs = project.crs()
            if self.line_length <= 0:
                ax.set_title("Drawn line length is zero"); self.canvas.draw(); return
        else:
            line_layer_id = self.line_layer_combo.currentData()
            line_layer = QgsProject.instance().mapLayer(line_layer_id) if line_layer_id else None
            if not line_layer or not isinstance(line_layer, QgsVectorLayer) or line_layer.geometryType() != QgsWkbTypes.LineGeometry:
                ax.set_title("Select a valid route line layer or draw a line"); self.canvas.draw(); return
            feats = list(line_layer.getFeatures())
            if not feats:
                ax.set_title("Route layer empty"); self.canvas.draw(); return
            merged = QgsGeometry.unaryUnion([f.geometry() for f in feats])
            if merged.isEmpty():
                ax.set_title("Merged route geometry empty"); self.canvas.draw(); return
            self.line_parts = merged.asMultiPolyline() if merged.isMultipart() else [merged.asPolyline()]
            self.distance_area.setSourceCrs(line_layer.sourceCrs(), project.transformContext())
            self.distance_area.setEllipsoid(project.ellipsoid())
            self.line_length = self.distance_area.measureLength(merged)
            self.current_line_crs = line_layer.sourceCrs()
            if self.line_length <= 0:
                ax.set_title("Route length is zero"); self.canvas.draw(); return

        # 2. Sampling
        if self.source_type_combo.currentText() == "Raster":
            self._sample_raster_mode(ax)
        else:
            self._sample_contour_mode(ax)

        # 3. Derived metrics
        self._compute_slopes()
        dual = self.dual_plot_chk.isChecked() if hasattr(self, 'dual_plot_chk') else False
        self._compute_seabed_length(dual)

        # 4. Plotting
        if dual:
            self.figure.clear()
            ax_depth = self.figure.add_subplot(211)
            ax_slope = self.figure.add_subplot(212, sharex=ax_depth)
            x_vals = self.kp_values.copy()
            if self.reverse_kp_chk.isChecked() and x_vals:
                x_vals = [self.kp_values[-1] - kp for kp in self.kp_values]
            if not x_vals or not self.depth_values:
                ax_depth.set_title("No profile data generated"); self.canvas.draw(); return
            ax_depth.plot(x_vals, self.depth_values, color='tab:blue', label='Depth (m)')
            ax_depth.set_ylabel("Depth (m)")
            if self.invert_depth_axis_chk.isChecked():
                ax_depth.invert_yaxis()
            ax_depth.grid(True); ax_depth.legend(loc='upper right')
            slope_unit = self.slope_unit_combo.currentText() if hasattr(self, 'slope_unit_combo') else 'Slope (deg)'
            if 'deg' in slope_unit:
                y_slope = self.slope_deg; slope_label = 'Slope (deg)'
            else:
                y_slope = self.slope_pct; slope_label = 'Slope (%)'
            ax_slope.plot(x_vals, y_slope, color='tab:orange', label=slope_label)
            ax_slope.set_ylabel(slope_label); ax_slope.set_xlabel("KP (km)")
            ax_slope.grid(True); ax_slope.legend(loc='upper right')
            # Add length summary (plan vs seabed) to top plot
            try:
                plan_len = self.line_length
                seabed_len = self.seabed_length
                if plan_len and seabed_len:
                    delta = seabed_len - plan_len
                    ratio = seabed_len / plan_len if plan_len > 0 else 0
                    ax_depth.set_title(f"Plan: {plan_len:,.1f} m | Seabed: {seabed_len:,.1f} m (Δ {delta:,.1f} m, {ratio:,.3f}x)")
            except Exception:
                pass
            try: self.figure.tight_layout()
            except Exception: pass
            self.canvas.draw()
            # Switch to plot tab after generating
            self.tab_widget.setCurrentWidget(self.profile_tab)
            return

        # Single variable
        var = self.variable_combo.currentText()
        if var.startswith("Depth"):
            y_vals = self.depth_values; ax.set_ylabel("Depth (m)")
            if self.invert_depth_axis_chk.isChecked(): ax.invert_yaxis()
        elif "deg" in var:
            y_vals = self.slope_deg; ax.set_ylabel("Slope (deg)")
        else:
            y_vals = self.slope_pct; ax.set_ylabel("Slope (%)")
        x_vals = self.kp_values.copy()
        if self.reverse_kp_chk.isChecked() and x_vals:
            x_vals = [self.kp_values[-1] - kp for kp in self.kp_values]
        if not x_vals or not y_vals:
            ax.set_title("No profile data generated"); self.canvas.draw(); return
        ax.plot(x_vals, y_vals, label=var)
        ax.set_xlabel("KP (km)"); ax.grid(True); ax.legend()
        ax.set_title(f"Route Length (plan): {self.line_length:,.1f} m | Seabed Length (3D): {self.seabed_length:,.1f} m")
        try: self.figure.tight_layout()
        except Exception: pass
        self.canvas.draw()
        # Switch to plot tab after generating
        self.tab_widget.setCurrentWidget(self.profile_tab)
    
    def _compute_seabed_length(self, dual):
        """Compute seabed (3D) length using sampled depths.

        Uses geodesic/ellipsoid-aware plan distances via QgsDistanceArea instead of raw dx/dy.
        Falls back gracefully if insufficient valid samples.
        Stores:
          self.seabed_length
          self.sampled_xyz (list of (x,y,z) for debugging/export)
          self.seabed_elongation_ratio (seabed_length / plan_length) if possible
        """
        self.sampled_xyz = []
        self.seabed_length = 0.0
        self.seabed_elongation_ratio = None
        if not self.kp_values or not self.depth_values:
            return
        # Collect valid consecutive samples
        pts = []  # (QgsPointXY, depth)
        for i, kp in enumerate(self.kp_values):
            if i >= len(self.depth_values):
                break
            depth_val = self.depth_values[i]
            if depth_val is None:
                continue
            geom = self._interpolate_point(kp * 1000.0)
            if not geom or geom.isEmpty():
                continue
            pts.append((geom.asPoint(), float(depth_val)))
        if len(pts) < 2:
            return
        total = 0.0
        for (p0, z0), (p1, z1) in zip(pts[:-1], pts[1:]):
            try:
                plan = self.distance_area.measureLine(p0, p1)
            except Exception:
                # fallback Euclidean in layer units
                dx = p1.x()-p0.x(); dy = p1.y()-p0.y(); plan = math.hypot(dx, dy)
            dz = (z1 - z0)
            seg_3d = math.sqrt(plan*plan + dz*dz)
            total += seg_3d
            self.sampled_xyz.append((p0.x(), p0.y(), z0))
        # append last point
        last_pt, last_z = pts[-1]
        self.sampled_xyz.append((last_pt.x(), last_pt.y(), last_z))
        self.seabed_length = total
        if self.line_length > 0:
            try:
                self.seabed_elongation_ratio = self.seabed_length / self.line_length
            except Exception:
                pass
        self.connect_canvas_events()
        if self.show_tooltips_chk.isChecked():
            self.enable_tooltips()

        # Persist basic settings
        self.settings.setValue("DepthProfile/interval_m", self.interval_spin.value())
        self.settings.setValue("DepthProfile/reverse_kp", self.reverse_kp_chk.isChecked())
        self.settings.setValue("DepthProfile/invert_slope", self.invert_slope_chk.isChecked())
        if not dual:
            self.settings.setValue("DepthProfile/variable", self.variable_combo.currentText())
        self.settings.setValue("DepthProfile/dual_plot", dual)
        if hasattr(self, 'slope_unit_combo'):
            self.settings.setValue("DepthProfile/slope_unit", self.slope_unit_combo.currentText())
        self.settings.setValue("DepthProfile/interp_contours", self.interpolate_contours_chk.isChecked())
        # Persist auto sample limit preferences
        if hasattr(self, 'auto_limit_chk'):
            self.settings.setValue("DepthProfile/auto_limit_samples", self.auto_limit_chk.isChecked())
        if hasattr(self, 'max_samples_spin'):
            self.settings.setValue("DepthProfile/max_samples", self.max_samples_spin.value())

    def _sample_raster_mode(self, ax):
        raster_layer_id = self.raster_layer_combo.currentData()
        raster_layer = QgsProject.instance().mapLayer(raster_layer_id) if raster_layer_id else None
        if not raster_layer or not isinstance(raster_layer, QgsRasterLayer):
            ax.set_title("Select valid raster layer")
            return
        provider = raster_layer.dataProvider()
        # Prepare CRS transform if needed
        raster_crs = raster_layer.crs()
        line_crs = self.current_line_crs if self.current_line_crs else raster_crs
        transform_line_to_raster = None
        if raster_crs != line_crs:
            try:
                transform_line_to_raster = QgsCoordinateTransform(line_crs, raster_crs, QgsProject.instance())
            except Exception:
                transform_line_to_raster = None
        interval_m = max(1, self.interval_spin.value())
        # Guard against excessive sample counts that can freeze UI
        expected_samples = int(self.line_length / interval_m) + 1 if self.line_length > 0 else 0
        max_samples = self.max_samples_spin.value() if hasattr(self, 'max_samples_spin') else 50000
        auto_limit = self.auto_limit_chk.isChecked() if hasattr(self, 'auto_limit_chk') else True
        if expected_samples > max_samples:
            if auto_limit:
                # Increase interval to cap samples to <= max_samples
                new_interval = int(self.line_length / max_samples) + 1
                if new_interval > interval_m:
                    self.iface.messageBar().pushMessage(
                        "Depth Profile",
                        f"Auto limit: interval raised {interval_m}m -> {new_interval}m (expected {expected_samples:,} > max {max_samples:,}).",
                        level=1, duration=7
                    )
                    interval_m = new_interval
            else:
                self.iface.messageBar().pushMessage(
                    "Depth Profile",
                    f"Warning: high sample count ({expected_samples:,}) exceeds max preference ({max_samples:,}) but Auto Limit is off.",
                    level=1, duration=8
                )
        # Pre-compute raster extent for quick outside checks
        raster_extent = raster_layer.extent()
        # Quick envelope overlap test (rough) - transform route bbox to raster CRS and check intersection
        try:
            if self.line_parts:
                xs = [pt.x() for part in self.line_parts for pt in part]
                ys = [pt.y() for part in self.line_parts for pt in part]
                if xs and ys:
                    minx, maxx = min(xs), max(xs)
                    miny, maxy = min(ys), max(ys)
                    # Transform the 4 corners; build min/max in raster CRS
                    if transform_line_to_raster:
                        corners = [QgsPointXY(minx, miny), QgsPointXY(minx, maxy), QgsPointXY(maxx, miny), QgsPointXY(maxx, maxy)]
                        tx = []
                        for c in corners:
                            try:
                                tx.append(transform_line_to_raster.transform(c))
                            except Exception:
                                pass
                        if tx:
                            minx_t = min(p.x() for p in tx); maxx_t = max(p.x() for p in tx)
                            miny_t = min(p.y() for p in tx); maxy_t = max(p.y() for p in tx)
                        else:
                            minx_t=minx; maxx_t=maxx; miny_t=miny; maxy_t=maxy
                    else:
                        minx_t=minx; maxx_t=maxx; miny_t=miny; maxy_t=maxy
                    # If bounding boxes don't intersect, early exit
                    if (maxx_t < raster_extent.xMinimum() or minx_t > raster_extent.xMaximum() or
                        maxy_t < raster_extent.yMinimum() or miny_t > raster_extent.yMaximum()):
                        ax.set_title("Route outside raster extent")
                        self.iface.messageBar().pushMessage("Depth Profile", "Selected route does not overlap raster extent.", level=1, duration=5)
                        return
        except Exception:
            pass
        # Build arrays
        self.kp_values = []
        self.depth_values = []
        dist = 0.0
        valid_count = 0
        while dist <= self.line_length:
            point_geom = self._interpolate_point(dist)
            if point_geom is None or point_geom.isEmpty():
                break
            pt = point_geom.asPoint()
            sample_pt = QgsPointXY(pt.x(), pt.y())
            if transform_line_to_raster:
                try:
                    sample_pt = transform_line_to_raster.transform(sample_pt)
                except Exception:
                    pass
            # Skip expensive sample if point clearly outside extent
            if (sample_pt.x() < raster_extent.xMinimum() or sample_pt.x() > raster_extent.xMaximum() or
                sample_pt.y() < raster_extent.yMinimum() or sample_pt.y() > raster_extent.yMaximum()):
                val = None
            else:
                sample, ok = provider.sample(sample_pt, 1)
                if ok:
                    try:
                        val = float(sample)
                        valid_count += 1
                    except Exception:
                        val = None
                else:
                    val = None
            self.kp_values.append(dist / 1000.0)
            self.depth_values.append(val)
            dist += interval_m
        # Ensure last point exactly at end
        if self.kp_values and (self.kp_values[-1] * 1000.0) < self.line_length:
            point_geom = self._interpolate_point(self.line_length)
            if point_geom and not point_geom.isEmpty():
                pt = point_geom.asPoint()
                sample_pt = QgsPointXY(pt.x(), pt.y())
                if transform_line_to_raster:
                    try:
                        sample_pt = transform_line_to_raster.transform(sample_pt)
                    except Exception:
                        pass
                if (sample_pt.x() < raster_extent.xMinimum() or sample_pt.x() > raster_extent.xMaximum() or
                    sample_pt.y() < raster_extent.yMinimum() or sample_pt.y() > raster_extent.yMaximum()):
                    val = None
                else:
                    sample, ok = provider.sample(sample_pt, 1)
                    if ok:
                        try:
                            val = float(sample)
                            valid_count += 1
                        except Exception:
                            val = None
                    else:
                        val = None
                self.kp_values.append(self.line_length / 1000.0)
                self.depth_values.append(val)
        # Coverage warnings
        try:
            if valid_count == 0 and self.kp_values:
                self.iface.messageBar().pushMessage(
                    "Depth Profile", "No MBES coverage along selected route (all samples null).", level=1, duration=6)
                ax.set_title("No raster coverage along route")
            elif valid_count > 0:
                ratio = valid_count / float(len(self.depth_values)) if self.depth_values else 0
                if ratio < 0.8:
                    self.iface.messageBar().pushMessage(
                        "Depth Profile", f"Partial MBES coverage: {ratio*100:.1f}% of samples valid.", level=1, duration=6)
        except Exception:
            pass

    def _sample_contour_mode(self, ax):
        contour_layer_id = self.contour_layer_combo.currentData()
        contour_layer = QgsProject.instance().mapLayer(contour_layer_id) if contour_layer_id else None
        if not contour_layer or not isinstance(contour_layer, QgsVectorLayer):
            ax.set_title("Select valid contour layer")
            return
        elev_field = self.elev_field_combo.currentText()
        if not elev_field:
            ax.set_title("Select elevation field")
            return
        kps = []
        depths = []
        route_geom = QgsGeometry.collectGeometry([QgsGeometry.fromPolylineXY(part) for part in self.line_parts]) if len(self.line_parts) > 1 else QgsGeometry.fromPolylineXY(self.line_parts[0])
        request = QgsFeatureRequest()
        contour_crs = contour_layer.crs()
        line_crs = self.current_line_crs if self.current_line_crs else contour_crs
        transform_contour_to_line = None
        if contour_crs != line_crs:
            try:
                transform_contour_to_line = QgsCoordinateTransform(contour_crs, line_crs, QgsProject.instance())
            except Exception:
                transform_contour_to_line = None
        for feat in contour_layer.getFeatures(request):
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            if transform_contour_to_line:
                try:
                    geom = QgsGeometry(geom)
                    geom.transform(transform_contour_to_line)
                except Exception:
                    continue
            inter = route_geom.intersection(geom)
            if inter.isEmpty():
                continue
            try:
                depth_val = float(feat[elev_field])
            except Exception:
                continue
            points = []
            if inter.isMultipart():
                if inter.type() == QgsWkbTypes.LineGeometry:
                    for part in inter.asMultiPolyline():
                        points.extend(part)
                else:
                    for g in inter.asGeometryCollection():
                        if g.isEmpty():
                            continue
                        if g.type() == QgsWkbTypes.LineGeometry:
                            for part in g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]:
                                points.extend(part)
                        elif g.type() == QgsWkbTypes.PointGeometry:
                            pts = g.asMultiPoint() if g.isMultipart() else [g.asPoint()]
                            points.extend(pts)
            else:
                if inter.type() == QgsWkbTypes.LineGeometry:
                    for part in inter.asMultiPolyline() if inter.isMultipart() else [inter.asPolyline()]:
                        points.extend(part)
                elif inter.type() == QgsWkbTypes.PointGeometry:
                    pts = inter.asMultiPoint() if inter.isMultipart() else [inter.asPoint()]
                    points.extend(pts)
            for p in points:
                kp_km = self._measure_along_route(p) / 1000.0
                if kp_km is None:
                    continue
                kps.append(kp_km)
                depths.append(depth_val)
        if not kps:
            ax.set_title("No contour intersections")
            return
        pairs = sorted(zip(kps, depths))
        self.kp_values = [p[0] for p in pairs]
        self.depth_values = [p[1] for p in pairs]
        if self.interpolate_contours_chk.isChecked() and len(self.kp_values) >= 2:
            total_kp = self.kp_values[-1]
            sample_count = min(1000, max(50, int(total_kp * 20)))
            new_kp = np.linspace(0, total_kp, sample_count)
            new_depth = np.interp(new_kp, self.kp_values, self.depth_values)
            self.kp_values = list(new_kp)
            self.depth_values = list(new_depth)

    # ---------------------- Geometry helpers ----------------------
    def _interpolate_point(self, distance_m):
        if distance_m <= 0:
            return QgsGeometry.fromPointXY(self.line_parts[0][0])
        if distance_m >= self.line_length:
            last_part = self.line_parts[-1]
            return QgsGeometry.fromPointXY(last_part[-1])
        cumulative = 0.0
        for part in self.line_parts:
            for i in range(len(part) - 1):
                p1 = part[i]; p2 = part[i+1]
                seg_len = self.distance_area.measureLine(p1, p2)
                if cumulative + seg_len >= distance_m:
                    ratio = (distance_m - cumulative) / seg_len if seg_len > 0 else 0
                    x = p1.x() + ratio * (p2.x() - p1.x())
                    y = p1.y() + ratio * (p2.y() - p1.y())
                    return QgsGeometry.fromPointXY(QgsPointXY(x, y))
                cumulative += seg_len
        return None

    def _measure_along_route(self, pt_xy):
        # Walk segments accumulating length until projection point
        cumulative = 0.0
        test_point = QgsPointXY(pt_xy.x(), pt_xy.y())
        best_dist = None
        best_cum = None
        for part in self.line_parts:
            for i in range(len(part) - 1):
                p1 = part[i]; p2 = part[i+1]
                seg_len = self.distance_area.measureLine(p1, p2)
                # Project test_point onto segment (planar) - simple approach
                dx = p2.x() - p1.x(); dy = p2.y() - p1.y()
                seg_sq = dx*dx + dy*dy
                if seg_sq <= 0:
                    cumulative += seg_len
                    continue
                t = ((test_point.x()-p1.x())*dx + (test_point.y()-p1.y())*dy) / seg_sq
                t_clamped = max(0.0, min(1.0, t))
                proj_x = p1.x() + t_clamped * dx; proj_y = p1.y() + t_clamped * dy
                # Distance from test point to projection (screen/planar)
                dist_sq = (test_point.x()-proj_x)**2 + (test_point.y()-proj_y)**2
                if best_dist is None or dist_sq < best_dist:
                    best_dist = dist_sq
                    best_cum = cumulative + t_clamped * seg_len
                cumulative += seg_len
        return best_cum

    def _compute_slopes(self):
        self.slope_deg = []
        self.slope_pct = []
        if len(self.kp_values) < 2:
            return
        self.slope_deg.append(0.0)
        self.slope_pct.append(0.0)
        for i in range(1, len(self.kp_values)):
            d_km = self.kp_values[i] - self.kp_values[i-1]
            if d_km <= 0:
                self.slope_deg.append(0.0); self.slope_pct.append(0.0); continue
            v1 = self.depth_values[i-1]
            v2 = self.depth_values[i]
            if v1 is None or v2 is None:
                self.slope_deg.append(None); self.slope_pct.append(None); continue
            horiz_m = d_km * 1000.0
            vertical = v2 - v1
            if self.invert_slope_chk.isChecked():
                vertical = -vertical
            slope_rad = math.atan2(vertical, horiz_m) if horiz_m > 0 else 0.0
            self.slope_deg.append(math.degrees(slope_rad))
            self.slope_pct.append(100.0 * vertical / horiz_m if horiz_m > 0 else 0.0)

    # ---------------------- Interactivity ----------------------
    def connect_canvas_events(self):
        if not self.canvas:
            return
        if self.canvas_cid is None:
            try:
                self.canvas_cid = self.canvas.mpl_connect('motion_notify_event', self.on_mouse_move)
            except Exception:
                pass
        if self._right_click_cid is None:
            try:
                self._right_click_cid = self.canvas.mpl_connect('button_press_event', self.on_right_click)
            except Exception:
                pass

    def disconnect_canvas_events(self):
        if self.canvas and self.canvas_cid is not None:
            try: self.canvas.mpl_disconnect(self.canvas_cid)
            except Exception: pass
            self.canvas_cid = None
        if self.canvas and self._right_click_cid is not None:
            try: self.canvas.mpl_disconnect(self._right_click_cid)
            except Exception: pass
            self._right_click_cid = None
        if self._tooltip_cid is not None and self.canvas:
            try: self.canvas.mpl_disconnect(self._tooltip_cid)
            except Exception: pass
            self._tooltip_cid = None

    def enable_tooltips(self):
        if self.canvas and self._tooltip_cid is None:
            try:
                self._tooltip_cid = self.canvas.mpl_connect('motion_notify_event', self.show_tooltip)
            except Exception:
                pass

    def toggle_tooltips(self):
        if self.show_tooltips_chk.isChecked():
            self.enable_tooltips()
        else:
            if self._tooltip_cid is not None and self.canvas:
                try: self.canvas.mpl_disconnect(self._tooltip_cid)
                except Exception: pass
                self._tooltip_cid = None

    def show_tooltip(self, event):
        if not event.inaxes or not self.kp_values:
            self.canvas.setToolTip(""); return
        mouse_x = event.xdata
        if mouse_x is None:
            self.canvas.setToolTip(""); return
        # Find nearest KP
        idx = min(range(len(self.kp_values)), key=lambda i: abs(self.kp_values[i]-mouse_x))
        kp = self.kp_values[idx]
        depth = self.depth_values[idx] if idx < len(self.depth_values) else None
        slope_d = self.slope_deg[idx] if idx < len(self.slope_deg) else None
        slope_p = self.slope_pct[idx] if idx < len(self.slope_pct) else None
        lines = [f"KP: {kp:.3f}"]
        if depth is not None: lines.append(f"Depth: {depth:.2f}")
        if slope_d is not None: lines.append(f"Slope°: {slope_d:.2f}")
        if slope_p is not None: lines.append(f"Slope%: {slope_p:.2f}")
        self.canvas.setToolTip("\n".join(lines))

    def on_mouse_move(self, event):
        if not event.inaxes or not self.kp_values:
            if self.marker and self.marker.isVisible():
                self.marker.hide(); self.iface.mapCanvas().refresh()
            redraw = False
            if self.vertical_line and self.vertical_line.get_visible():
                self.vertical_line.set_visible(False); redraw = True
            if self.vertical_line2 and self.vertical_line2.get_visible():
                self.vertical_line2.set_visible(False); redraw = True
            if redraw:
                self.canvas.draw_idle()
            return
        kp = event.xdata
        if kp is None: return
        # Snap to nearest
        idx = min(range(len(self.kp_values)), key=lambda i: abs(self.kp_values[i]-kp))
        snap_kp = self.kp_values[idx]
        # Update crosshair(s)
        dual = self.dual_plot_chk.isChecked() if hasattr(self, 'dual_plot_chk') else False
        if dual:
            axes = self.figure.get_axes()
            if axes:
                # depth axis
                if self.vertical_line is None:
                    self.vertical_line = axes[0].axvline(x=snap_kp, color='k', linestyle='--', lw=1)
                else:
                    self.vertical_line.set_xdata([snap_kp, snap_kp])
                    if not self.vertical_line.get_visible():
                        self.vertical_line.set_visible(True)
                # slope axis
                if len(axes) > 1:
                    if self.vertical_line2 is None:
                        self.vertical_line2 = axes[1].axvline(x=snap_kp, color='k', linestyle='--', lw=1)
                    else:
                        self.vertical_line2.set_xdata([snap_kp, snap_kp])
                        if not self.vertical_line2.get_visible():
                            self.vertical_line2.set_visible(True)
        else:
            if self.vertical_line is None:
                ax = event.inaxes
                self.vertical_line = ax.axvline(x=snap_kp, color='k', linestyle='--', lw=1)
            else:
                self.vertical_line.set_xdata([snap_kp, snap_kp])
                if not self.vertical_line.get_visible():
                    self.vertical_line.set_visible(True)
        self.canvas.draw_idle()
        # Update map marker
        self.update_map_marker(snap_kp)

    def on_right_click(self, event):
        if event.button != 3 or not event.inaxes or not self.kp_values:
            return
        kp = event.xdata
        if kp is None: return
        idx = min(range(len(self.kp_values)), key=lambda i: abs(self.kp_values[i]-kp))
        snap_kp = self.kp_values[idx]
        dist_m = snap_kp * 1000.0
        point_geom = self._interpolate_point(dist_m)
        if point_geom and not point_geom.isEmpty():
            canvas = self.iface.mapCanvas()
            canvas.setCenter(point_geom.asPoint()); canvas.refresh()

    def update_map_marker(self, kp):
        dist_m = kp * 1000.0
        point_geom = self._interpolate_point(dist_m)
        if point_geom is None or point_geom.isEmpty():
            if self.marker and self.marker.isVisible():
                self.marker.hide(); self.iface.mapCanvas().refresh()
            return
        if not self.marker:
            self.marker = QgsVertexMarker(self.iface.mapCanvas())
            self.marker.setColor(Qt.blue)
            self.marker.setIconSize(10)
            self.marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self.marker.setPenWidth(2)
            self.iface.mapCanvas().scene().addItem(self.marker)
        if not self.marker.isVisible():
            self.marker.show()
        # Transform marker point to project CRS if different
        try:
            project_crs = QgsProject.instance().crs()
            line_layer_id = self.line_layer_combo.currentData()
            line_layer = QgsProject.instance().mapLayer(line_layer_id) if line_layer_id else None
            if line_layer and line_layer.crs() != project_crs:
                try:
                    to_project = QgsCoordinateTransform(line_layer.crs(), project_crs, QgsProject.instance())
                    marker_pt = to_project.transform(point_geom.asPoint())
                except Exception:
                    marker_pt = point_geom.asPoint()
            else:
                marker_pt = point_geom.asPoint()
        except Exception:
            marker_pt = point_geom.asPoint()
        self.marker.setCenter(marker_pt)
        self.iface.mapCanvas().refresh()

    # ---------------------- Cleanup ----------------------
    def clear_plot(self):
        self.disconnect_canvas_events()
        if self.marker:
            try:
                if not _sip_isdeleted(self.marker):
                    self.marker.hide(); self.marker.deleteLater()
            except Exception:
                pass
            self.marker = None
        if self.figure:
            try: self.figure.clear()
            except Exception: pass
        self.vertical_line = None
        self.vertical_line2 = None
        self.kp_values = []
        self.depth_values = []
        self.slope_deg = []
        self.slope_pct = []
        try:
            self.canvas.draw()
        except Exception:
            pass

    # ---------------- Temporary line drawing -----------------
    def activate_temp_line_tool(self):
        canvas = self.iface.mapCanvas()
        if self.temp_line_tool:
            try:
                canvas.unsetMapTool(self.temp_line_tool)
            except Exception:
                pass
            self.temp_line_tool = None
        def finished(points):
            self.temp_drawn_points = points
            self.use_drawn_chk.setChecked(True)
            self.update_enable_states()
            # draw/update persistent rubber band
            try:
                if self.temp_line_rubber:
                    # Fully dispose of previous rubber band to avoid lingering graphics
                    try: self.temp_line_rubber.reset(QgsWkbTypes.LineGeometry)
                    except Exception: pass
                    try: self.temp_line_rubber.hide()
                    except Exception: pass
                    try: self.temp_line_rubber.deleteLater()
                    except Exception: pass
                    self.temp_line_rubber = None
                else:
                    # QgsRubberBand already imported at top
                    self.temp_line_rubber = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.LineGeometry)
                    self.temp_line_rubber.setColor(Qt.yellow)
                    self.temp_line_rubber.setWidth(2)
                if self.temp_line_rubber is None:
                    self.temp_line_rubber = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.LineGeometry)
                    self.temp_line_rubber.setColor(Qt.yellow)
                    self.temp_line_rubber.setWidth(2)
                for pt in points:
                    try: self.temp_line_rubber.addPoint(pt)
                    except Exception: pass
                try: self.temp_line_rubber.show()
                except Exception: pass
            except Exception:
                pass
            self.iface.messageBar().pushMessage("Depth Profile", f"Temporary line captured ({len(points)} pts)", level=0, duration=3)
        def canceled():
            self.iface.messageBar().pushMessage("Depth Profile", "Drawing canceled", level=1, duration=2)
        # Instantiate map tool (now expects iface for messaging) and activate
        self.temp_line_tool = TempLineMapTool(canvas, self.iface, finished, canceled)
        canvas.setMapTool(self.temp_line_tool)

    def clear_drawn_line(self):
        self.temp_drawn_points = []
        self.use_drawn_chk.setChecked(False)
        self.update_enable_states()
        # remove persistent rubber band
        try:
            if self.temp_line_rubber:
                try: self.temp_line_rubber.reset(QgsWkbTypes.LineGeometry)
                except Exception: pass
                try: self.temp_line_rubber.hide()
                except Exception: pass
                try: self.temp_line_rubber.deleteLater()
                except Exception: pass
        except Exception:
            pass
        self.temp_line_rubber = None
        self.iface.messageBar().pushMessage("Depth Profile", "Temporary line cleared", level=0, duration=2)

    def closeEvent(self, event):  # noqa
        self.clear_plot()
        # ensure temp line rubber removed
        try:
            if self.temp_line_rubber:
                try: self.temp_line_rubber.reset(QgsWkbTypes.LineGeometry)
                except Exception: pass
                try: self.temp_line_rubber.hide()
                except Exception: pass
                try: self.temp_line_rubber.deleteLater()
                except Exception: pass
        except Exception:
            pass
        self.temp_line_rubber = None
        try:
            self.iface.projectRead.disconnect(self.populate_layer_combos)
        except Exception:
            pass
        try:
            QgsProject.instance().layerWasAdded.disconnect(self.on_layer_event)
        except Exception:
            pass
        try:
            QgsProject.instance().layersRemoved.disconnect(self.on_layer_event)
        except Exception:
            pass
        try:
            QgsProject.instance().layersAdded.disconnect(self.on_layers_added)
        except Exception:
            pass
        try:
            QgsProject.instance().layerRemoved.disconnect(self.on_layer_event)
        except Exception:
            pass
        super().closeEvent(event)

    def cleanup_matplotlib_resources_on_close(self):
        # Provided for parity with other dock widgets; already handled in clear_plot
        pass

    # ---------------------- DXF Export ----------------------
    def export_dxf(self):
        """Export the current depth (and optionally slope) profile to a simple DXF polyline.

        Strategy:
        - Only proceed if a profile has been generated (kp_values & depth_values populated).
        - Convert KP (km) -> metres -> millimetres (engineering drawing scale) like catenary tool.
    - Depth values exported with sea level at Y=0 and depths negative (e.g. 10 m depth -> y = -10000 mm).
        - Handle None gaps by splitting into multiple polylines.
        - Write a minimal DXF (POLYLINE + VERTEX records) in layer 0.
        """
        if not self.kp_values or not self.depth_values:
            self.iface.messageBar().pushMessage("Depth Profile", "No profile data to export. Generate first.", level=1, duration=4)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save DXF", "depth_profile.dxf", "DXF Files (*.dxf)")
        if not path:
            return
        # Build segments (skip None values)
        segments = []
        current_x = []
        current_y = []
        for kp, depth in zip(self.kp_values, self.depth_values):
            if depth is None:
                if current_x:
                    segments.append((current_x, current_y))
                    current_x, current_y = [], []
                continue
            current_x.append(kp * 1000.0 * 1000.0)  # km -> m -> mm
            # Depth positive down in data -> make negative for DXF so seabed below 0
            current_y.append(-depth * 1000.0)        # m -> mm (negated)
        if current_x:
            segments.append((current_x, current_y))
        if not segments:
            self.iface.messageBar().pushMessage("Depth Profile", "All depth values are null; nothing to export.", level=1, duration=4)
            return
        # Compose DXF content
        dxf_parts = ['0','SECTION','2','ENTITIES']
        for sx, sy in segments:
            dxf_parts.extend(['0','POLYLINE','8','0','66','1','70','0'])
            for xi, yi in zip(sx, sy):
                dxf_parts.extend(['0','VERTEX','8','0','10',f'{xi}','20',f'{yi}','30','0.0'])
            dxf_parts.extend(['0','SEQEND'])
        dxf_parts.extend(['0','ENDSEC','0','EOF'])
        dxf_text = '\n'.join(dxf_parts) + '\n'
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(dxf_text)
            self.iface.messageBar().pushMessage("Depth Profile", f"DXF exported: {path}", level=0, duration=5)
        except Exception as e:
            self.iface.messageBar().pushMessage("Depth Profile", f"Failed to write DXF: {e}", level=2, duration=6)

    def export_csv(self):
        """Export the current KP, Depth, and Slope data to a CSV file."""
        if not self.kp_values or not self.depth_values:
            self.iface.messageBar().pushMessage("Depth Profile", "No profile data to export. Generate first.", level=1, duration=4)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", "depth_profile.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write("KP (km),Depth (m),Slope (deg),Slope (%)\n")
                for kp, depth, slope_deg, slope_pct in zip(self.kp_values, self.depth_values, self.slope_deg, self.slope_pct):
                    if depth is not None:
                        f.write(f"{kp:.3f},{depth:.3f},{slope_deg:.3f},{slope_pct:.3f}\n")
                    else:
                        f.write(f"{kp:.3f},,{slope_deg:.3f},{slope_pct:.3f}\n")
            self.iface.messageBar().pushMessage("Depth Profile", f"CSV exported: {path}", level=0, duration=5)
        except Exception as e:
            self.iface.messageBar().pushMessage("Depth Profile", f"Failed to write CSV: {e}", level=2, duration=6)
