from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QSpinBox, QCheckBox, QFileDialog, QTabWidget, QFormLayout, QSizePolicy, QProgressDialog,
    QListWidget, QListWidgetItem, QDoubleSpinBox
)
from qgis.PyQt.QtCore import Qt, QSettings, QTimer
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer, QgsWkbTypes, QgsGeometry, QgsPointXY,
    QgsDistanceArea, QgsFeatureRequest, QgsCoordinateTransform, QgsSpatialIndex, QgsFeature
)
from qgis.gui import QgsVertexMarker, QgsRubberBand
from .maptools.temp_line_maptool import TempLineMapTool  # new temporary line drawing tool

# Added standard library & third-party imports
import math
import bisect
import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas, NavigationToolbar2QT as NavigationToolbar

# Simple sip deletion check fallback
try:  # sip is available in QGIS Python env; guard for static analysis
    import sip  # type: ignore

    _sip_isdeleted = sip.isdeleted
except Exception:  # pragma: no cover
    def _sip_isdeleted(_obj):
        return False


class DepthProfileDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Depth Profile", parent)
        self.iface = iface
        self.setObjectName("DepthProfileDockWidget")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        self.settings = QSettings()
        self._closing = False
        self._project_signals_connected = False
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
        # Side-slope (cross-profile) data
        self.side_slope_deg = []
        self.side_slope_pct = []
        self.side_port_depth = []
        self.side_starboard_depth = []
        self.side_cross_span_m = []
        # Segment-based data for CSV export
        self.segment_kp_from = []
        self.segment_kp_to = []
        self.segment_depth_from = []
        self.segment_depth_to = []
        self.segment_slope_deg = []
        self.segment_slope_pct = []
        self.segment_side_slope_deg = []
        self.segment_side_slope_pct = []
        self.segment_port_depth = []
        self.segment_starboard_depth = []
        self.segment_cross_span_m = []
        self.segment_seabed_length = []
        self.segment_euclidean_length = []
        self.segment_lat_from = []
        self.segment_lon_from = []
        self.segment_lat_to = []
        self.segment_lon_to = []
        self.segment_lat_from = []
        self.segment_lon_from = []
        self.segment_lat_to = []
        self.segment_lon_to = []
        # Temporary line drawing state
        self.temp_drawn_points = []  # list of QgsPointXY in project CRS
        self.temp_line_tool = None
        self.using_drawn_line = False
        self.current_line_crs = None
        self.temp_line_rubber = None  # persistent rubber band showing drawn line
        # Seabed length (3D) calculation
        self.seabed_length = 0.0
        self.sampled_xyz = []  # List of (x, y, z) tuples for 3D length

        # Cached stationing for fast interpolation along long routes
        self._route_seg_starts_m = None
        self._route_seg_ends_m = None
        self._route_seg_lens_m = None
        self._route_seg_p1 = None
        self._route_seg_p2 = None
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

        self.refresh_layers_btn = QPushButton("Refresh")
        self.refresh_layers_btn.setToolTip("Refresh layer lists")
        line_row.addWidget(self.refresh_layers_btn)

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
        raster_row.addWidget(QLabel("Raster Layer(s):"))
        self.raster_layer_list = QListWidget()
        self.raster_layer_list.setMinimumWidth(220)
        self.raster_layer_list.setMaximumHeight(80)
        self.raster_layer_list.setToolTip("Tick one or more rasters. Sampling uses the first raster with valid data at each point; missing coverage remains null.")
        raster_row.addWidget(self.raster_layer_list)
        raster_row.addStretch()
        form_layout.addRow(raster_row)
        
        # Contour Layer 1
        contour1_row = QHBoxLayout()
        contour1_row.addWidget(QLabel("Contour Layer 1:"))
        self.contour_layer_combo = QComboBox()
        self.contour_layer_combo.setMinimumWidth(120)
        contour1_row.addWidget(self.contour_layer_combo)
        contour1_row.addWidget(QLabel("Depth Field 1:"))
        self.depth_field_combo = QComboBox()
        self.depth_field_combo.setMinimumWidth(100)
        contour1_row.addWidget(self.depth_field_combo)
        contour1_row.addStretch()
        form_layout.addRow(contour1_row)
        
        # Contour Layer 2
        contour2_row = QHBoxLayout()
        contour2_row.addWidget(QLabel("Contour Layer 2 (optional):"))
        self.contour_layer_combo2 = QComboBox()
        self.contour_layer_combo2.setMinimumWidth(120)
        contour2_row.addWidget(self.contour_layer_combo2)
        contour2_row.addWidget(QLabel("Depth Field 2:"))
        self.depth_field_combo2 = QComboBox()
        self.depth_field_combo2.setMinimumWidth(100)
        contour2_row.addWidget(self.depth_field_combo2)
        contour2_row.addStretch()
        form_layout.addRow(contour2_row)
        
        # Row 3: Sampling and Options
        sampling_row = QHBoxLayout()
        sampling_row.addWidget(QLabel("Sampling Interval (m):"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 50000)
        self.interval_spin.setValue(int(self.settings.value("DepthProfile/interval_m", 50)))
        sampling_row.addWidget(self.interval_spin)

        self.adaptive_interval_chk = QCheckBox("Adaptive (Raster)")
        self.adaptive_interval_chk.setToolTip("Raster mode only: choose step size from the raster resolution covering each station (prefers highest-resolution raster with valid data).")
        self.adaptive_interval_chk.setChecked(bool(self.settings.value("DepthProfile/adaptive_interval", False, type=bool)))
        sampling_row.addWidget(self.adaptive_interval_chk)
        sampling_row.addWidget(QLabel("Factor:"))
        self.adaptive_interval_factor = QDoubleSpinBox()
        self.adaptive_interval_factor.setRange(0.25, 10.0)
        self.adaptive_interval_factor.setSingleStep(0.25)
        self.adaptive_interval_factor.setDecimals(2)
        self.adaptive_interval_factor.setValue(float(self.settings.value("DepthProfile/adaptive_interval_factor", 1.0)))
        self.adaptive_interval_factor.setToolTip("Step = factor × raster pixel size (meters). Interval spinbox acts as a minimum step.")
        sampling_row.addWidget(self.adaptive_interval_factor)
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

        # Readout: estimated processing size
        self.sample_estimate_label = QLabel("Estimated samples: —")
        self.sample_estimate_label.setToolTip(
            "Estimate based on current route length and sampling settings.\n"
            "Fixed: samples ≈ length / interval + 1\n"
            "Adaptive: lower bound uses the minimum interval.\n"
            "Worst-case raster probes ≈ samples × (# selected rasters)."
        )
        form_layout.addRow(self.sample_estimate_label)
        
        options_row = QHBoxLayout()
        self.interpolate_contours_chk = QCheckBox("Interpolate Between Contours")
        self.interpolate_contours_chk.setChecked(bool(self.settings.value("DepthProfile/interp_contours", True, type=bool)))
        options_row.addWidget(self.interpolate_contours_chk)
        # Side slope controls (cross-profile)
        self.side_slope_chk = QCheckBox("Side Slope")
        self.side_slope_chk.setChecked(bool(self.settings.value("DepthProfile/side_slope_enabled", False, type=bool)))
        options_row.addWidget(self.side_slope_chk)
        options_row.addWidget(QLabel("Cross Search (m):"))
        self.side_slope_search_spin = QSpinBox()
        self.side_slope_search_spin.setRange(1, 50000)
        self.side_slope_search_spin.setValue(int(self.settings.value("DepthProfile/side_slope_search_m", 200)))
        self.side_slope_search_spin.setToolTip("Maximum distance to search to port/starboard for side slope (half-width).")
        options_row.addWidget(self.side_slope_search_spin)
        self.side_slope_plot_chk = QCheckBox("Plot Side")
        self.side_slope_plot_chk.setChecked(bool(self.settings.value("DepthProfile/side_slope_plot", True, type=bool)))
        options_row.addWidget(self.side_slope_plot_chk)
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
            "    <li>For raster, select one or more <b>Raster Layer(s)</b>. For contours, select one or two <b>Contour Layers</b> (e.g., Minor and Major contours) and their corresponding <b>Depth Fields</b>.</li>"
            "    <li>Set the <b>Sampling Interval</b> (meters) and other options as needed.</li>"
            "    <li><b>Adaptive (Raster):</b> optionally derive the step size from raster resolution along the route (factor × pixel size, with Sampling Interval acting as a minimum).</li>"
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
        self.contour_layer_combo.currentIndexChanged.connect(self.populate_depth_fields_1)
        self.contour_layer_combo2.currentIndexChanged.connect(self.populate_depth_fields_2)
        self.refresh_layers_btn.clicked.connect(lambda: self.schedule_layer_combo_refresh(delay_ms=0))
        self.line_layer_combo.currentIndexChanged.connect(self.update_sample_estimate)
        self.use_drawn_chk.toggled.connect(self.update_sample_estimate)
        self.interval_spin.valueChanged.connect(self.update_sample_estimate)
        self.auto_limit_chk.toggled.connect(self.update_sample_estimate)
        self.max_samples_spin.valueChanged.connect(self.update_sample_estimate)
        if hasattr(self, 'adaptive_interval_chk'):
            self.adaptive_interval_chk.toggled.connect(self.update_sample_estimate)
        if hasattr(self, 'adaptive_interval_factor'):
            self.adaptive_interval_factor.valueChanged.connect(self.update_sample_estimate)
        try:
            self.raster_layer_list.itemChanged.connect(self.update_sample_estimate)
        except Exception:
            pass
        self.show_tooltips_chk.toggled.connect(self.toggle_tooltips)
        self.dual_plot_chk.toggled.connect(self.update_enable_states)
        self.side_slope_chk.toggled.connect(self.update_enable_states)
        self.adaptive_interval_chk.toggled.connect(self.update_enable_states)
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
        self.update_sample_estimate()
        # Project/layer signals: use a debounced scheduler so rapid batch adds only trigger one refresh
        self._connect_project_signals()
        # Dock placement (only once)
        try:
            main_win = self.iface.mainWindow(); main_win.removeDockWidget(self); main_win.addDockWidget(Qt.BottomDockWidgetArea, self)
        except Exception:
            pass

        # When dock becomes visible, refresh layer lists (helps when layers are added while dock is open)
        try:
            self.visibilityChanged.connect(lambda vis: self.schedule_layer_combo_refresh(delay_ms=0) if vis else None)
        except Exception:
            pass

    def _connect_project_signals(self):
        if self._project_signals_connected:
            return
        try:
            self.iface.projectRead.connect(self.populate_layer_combos)
        except Exception:
            pass
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
        self._project_signals_connected = True

    def _disconnect_project_signals(self):
        if not self._project_signals_connected:
            return
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
        self._project_signals_connected = False

    def showEvent(self, event):
        # When a dock is closed and later shown again, make sure it can refresh safely.
        self._closing = False
        self._connect_project_signals()
        try:
            self.schedule_layer_combo_refresh(delay_ms=0)
        except Exception:
            pass
        super().showEvent(event)

    # ---------------------- UI population ----------------------
    def populate_layer_combos(self):
        # Guard against late calls after close (e.g., debounced QTimer callbacks)
        try:
            if getattr(self, '_closing', False):
                return
            if getattr(self, 'line_layer_combo', None) is None or _sip_isdeleted(self.line_layer_combo):
                return
        except Exception:
            return

        prev_line = self.line_layer_combo.currentData()
        prev_rasters = set(self._get_selected_raster_layer_ids())
        prev_contour = self.contour_layer_combo.currentData()
        prev_contour2 = self.contour_layer_combo2.currentData()
        self.line_layer_combo.blockSignals(True)
        self.raster_layer_list.blockSignals(True)
        self.contour_layer_combo.blockSignals(True)
        self.contour_layer_combo2.blockSignals(True)
        try:
            self.line_layer_combo.clear()
            self.raster_layer_list.clear()
            self.contour_layer_combo.clear()
            self.contour_layer_combo2.clear()

            # Contour layer 2 is optional
            self.contour_layer_combo2.addItem("(None)", None)
            for layer in QgsProject.instance().mapLayers().values():
                if isinstance(layer, QgsVectorLayer) and layer.geometryType() == QgsWkbTypes.LineGeometry:
                    self.line_layer_combo.addItem(layer.name(), layer.id())
                if isinstance(layer, QgsRasterLayer):
                    item = QListWidgetItem(layer.name())
                    item.setData(Qt.UserRole, layer.id())
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked if layer.id() in prev_rasters else Qt.Unchecked)
                    self.raster_layer_list.addItem(item)
                if isinstance(layer, QgsVectorLayer) and layer.geometryType() == QgsWkbTypes.LineGeometry:
                    self.contour_layer_combo.addItem(layer.name(), layer.id())
                    self.contour_layer_combo2.addItem(layer.name(), layer.id())
        finally:
            self.line_layer_combo.blockSignals(False)
            self.raster_layer_list.blockSignals(False)
            self.contour_layer_combo.blockSignals(False)
            self.contour_layer_combo2.blockSignals(False)
        # Restore selections where possible
        if prev_line:
            idx = self.line_layer_combo.findData(prev_line)
            if idx != -1:
                self.line_layer_combo.setCurrentIndex(idx)
        # If nothing selected, default to first raster (previous behavior: single selection)
        if not self._get_selected_raster_layer_ids() and self.raster_layer_list.count() > 0:
            try:
                self.raster_layer_list.item(0).setCheckState(Qt.Checked)
            except Exception:
                pass
        if prev_contour:
            idx = self.contour_layer_combo.findData(prev_contour)
            if idx != -1:
                self.contour_layer_combo.setCurrentIndex(idx)
        if prev_contour2:
            idx = self.contour_layer_combo2.findData(prev_contour2)
            if idx != -1:
                self.contour_layer_combo2.setCurrentIndex(idx)
        self.populate_depth_fields_1()
        self.populate_depth_fields_2()
        self.update_sample_estimate()

    def populate_depth_fields_1(self):
        # Populate depth field combo for layer 1
        self.depth_field_combo.clear()
        layer_id = self.contour_layer_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if layer and isinstance(layer, QgsVectorLayer):
            for f in layer.fields():
                self.depth_field_combo.addItem(f.name())
    
    def populate_depth_fields_2(self):
        # Populate depth field combo for layer 2
        self.depth_field_combo2.clear()
        layer_id2 = self.contour_layer_combo2.currentData()
        layer2 = QgsProject.instance().mapLayer(layer_id2) if layer_id2 else None
        if layer2 and isinstance(layer2, QgsVectorLayer):
            self.depth_field_combo2.setEnabled(True)
            for f in layer2.fields():
                self.depth_field_combo2.addItem(f.name())
        else:
            # No second contour layer selected
            self.depth_field_combo2.setEnabled(False)

    def update_enable_states(self):
        # Guard against late calls after close (signals/timers)
        try:
            if getattr(self, '_closing', False):
                return
            if getattr(self, 'use_drawn_chk', None) is None or _sip_isdeleted(self.use_drawn_chk):
                return
        except Exception:
            return

        raster_mode = self.source_type_combo.currentText() == "Raster"
        self.raster_layer_list.setEnabled(raster_mode)
        self.interval_spin.setEnabled(raster_mode)
        if hasattr(self, 'adaptive_interval_chk'):
            self.adaptive_interval_chk.setEnabled(raster_mode)
        if hasattr(self, 'adaptive_interval_factor'):
            self.adaptive_interval_factor.setEnabled(raster_mode and bool(self.adaptive_interval_chk.isChecked()))
        if hasattr(self, 'auto_limit_chk'):
            self.auto_limit_chk.setEnabled(raster_mode)
        if hasattr(self, 'max_samples_spin'):
            self.max_samples_spin.setEnabled(raster_mode)
        self.contour_layer_combo.setEnabled(not raster_mode)
        self.contour_layer_combo2.setEnabled(not raster_mode)
        self.depth_field_combo.setEnabled(not raster_mode)
        # Depth field 2 only relevant when a second contour layer is selected
        if not raster_mode:
            self.depth_field_combo2.setEnabled(bool(self.contour_layer_combo2.currentData()))
        else:
            self.depth_field_combo2.setEnabled(False)
        self.interpolate_contours_chk.setEnabled(not raster_mode)
        # Side-slope inputs
        # - allow in both raster and contour modes
        side_enabled = self.side_slope_chk.isChecked() if hasattr(self, 'side_slope_chk') else False
        if hasattr(self, 'side_slope_search_spin'):
            self.side_slope_search_spin.setEnabled(side_enabled)
        if hasattr(self, 'side_slope_plot_chk'):
            self.side_slope_plot_chk.setEnabled(side_enabled)
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

        # Update readout whenever mode toggles
        self.update_sample_estimate()

    def _estimate_current_route_length_m(self):
        """Best-effort estimate of current route length (meters) based on UI selection.

        This is used for the live sample-count readout. It intentionally avoids the
        heavier geometry union used in full generation.
        """
        project = QgsProject.instance()

        # Drawn route
        try:
            if self.use_drawn_chk.isChecked() and self.temp_drawn_points and len(self.temp_drawn_points) >= 2:
                da = QgsDistanceArea()
                da.setSourceCrs(project.crs(), project.transformContext())
                da.setEllipsoid(project.ellipsoid())
                length = 0.0
                for a, b in zip(self.temp_drawn_points[:-1], self.temp_drawn_points[1:]):
                    length += float(da.measureLine(a, b))
                return length
        except Exception:
            pass

        # Layer route
        try:
            layer_id = self.line_layer_combo.currentData()
            line_layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
            if not line_layer or not isinstance(line_layer, QgsVectorLayer) or line_layer.geometryType() != QgsWkbTypes.LineGeometry:
                return None
            da = QgsDistanceArea()
            da.setSourceCrs(line_layer.sourceCrs(), project.transformContext())
            da.setEllipsoid(project.ellipsoid())
            total = 0.0
            req = QgsFeatureRequest()
            try:
                req.setNoAttributes()
            except Exception:
                pass
            for f in line_layer.getFeatures(req):
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                try:
                    total += float(da.measureLength(g))
                except Exception:
                    try:
                        total += float(g.length())
                    except Exception:
                        continue
            return total if total > 0 else None
        except Exception:
            return None

    def update_sample_estimate(self, *args):
        """Update the estimated sample-count readout."""
        if not hasattr(self, 'sample_estimate_label'):
            return

        # Only meaningful in raster mode (contours are intersection-driven)
        try:
            mode = self.source_type_combo.currentText()
        except Exception:
            mode = "Raster"
        if mode != "Raster":
            self.sample_estimate_label.setText("Estimated samples: (contours mode)")
            return

        length_m = self._estimate_current_route_length_m()
        if not length_m or length_m <= 0:
            self.sample_estimate_label.setText("Estimated samples: — (select a route)")
            return

        min_step_m = max(1, int(self.interval_spin.value()))
        adaptive = bool(getattr(self, 'adaptive_interval_chk', None) and self.adaptive_interval_chk.isChecked())
        raster_count = 0
        try:
            raster_count = len(self._get_selected_raster_layer_ids())
        except Exception:
            raster_count = 0

        max_samples = self.max_samples_spin.value() if hasattr(self, 'max_samples_spin') else None
        auto_limit = self.auto_limit_chk.isChecked() if hasattr(self, 'auto_limit_chk') else False

        # Samples estimate
        try:
            base = int(length_m / float(min_step_m)) + 1
        except Exception:
            base = None

        if base is None:
            self.sample_estimate_label.setText("Estimated samples: —")
            return

        probes = base * raster_count if raster_count else None
        if adaptive:
            msg = f"Estimated samples: ≥{base:,} (adaptive; min step {min_step_m} m)"
        else:
            msg = f"Estimated samples: {base:,} (interval {min_step_m} m)"

        if raster_count:
            msg += f" | Rasters: {raster_count} | Worst-case probes: {probes:,}"
        else:
            msg += " | Rasters: 0"

        if max_samples is not None:
            msg += f" | Max: {max_samples:,}{' (Auto Limit)' if auto_limit else ''}"

        self.sample_estimate_label.setText(msg)

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
                # Widget may have been closed/deleted before the timer fires
                try:
                    if getattr(self, '_closing', False):
                        return
                    if getattr(self, 'line_layer_combo', None) is None or _sip_isdeleted(self.line_layer_combo):
                        return
                except Exception:
                    return
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

        # Build stationing cache for faster interpolation on long routes
        self._build_route_stationing_cache()

        # 2. Sampling
        if self.source_type_combo.currentText() == "Raster":
            self._sample_raster_mode(ax)
        else:
            self._sample_contour_mode(ax)

        # 3. Derived metrics
        self._compute_slopes()
        # Side-slope (cross-profile) is optional
        if getattr(self, 'side_slope_chk', None) and self.side_slope_chk.isChecked():
            try:
                self._compute_side_slopes_with_progress()
            except Exception as e:
                self.iface.messageBar().pushMessage("Depth Profile", f"Side slope failed: {e}", level=1, duration=6)
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
            # Optional side slope overlay
            if getattr(self, 'side_slope_chk', None) and self.side_slope_chk.isChecked() and getattr(self, 'side_slope_plot_chk', None) and self.side_slope_plot_chk.isChecked():
                try:
                    if 'deg' in slope_unit:
                        y_side = self.side_slope_deg
                        side_label = 'Side Slope (deg)'
                    else:
                        y_side = self.side_slope_pct
                        side_label = 'Side Slope (%)'
                    if y_side and len(y_side) == len(x_vals):
                        y_side_clean = [np.nan if v is None else v for v in y_side]
                        if any(not np.isnan(v) for v in y_side_clean):
                            ax_slope.plot(x_vals, y_side_clean, color='tab:green', alpha=0.9, label=side_label)
                except Exception:
                    pass
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
        # Optional side slope overlay when plotting slope
        if getattr(self, 'side_slope_chk', None) and self.side_slope_chk.isChecked() and getattr(self, 'side_slope_plot_chk', None) and self.side_slope_plot_chk.isChecked():
            try:
                if "Slope" in var:
                    if "deg" in var:
                        y_side = self.side_slope_deg
                        side_label = 'Side Slope (deg)'
                    else:
                        y_side = self.side_slope_pct
                        side_label = 'Side Slope (%)'
                    if y_side and len(y_side) == len(x_vals):
                        y_side_clean = [np.nan if v is None else v for v in y_side]
                        if any(not np.isnan(v) for v in y_side_clean):
                            ax.plot(x_vals, y_side_clean, color='tab:green', alpha=0.9, label=side_label)
            except Exception:
                pass
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
        raster_layers = self._get_selected_raster_layers()
        if not raster_layers:
            ax.set_title("Select one or more raster layers")
            return

        line_crs = self.current_line_crs if self.current_line_crs else raster_layers[0].crs()
        raster_sources = self._prepare_raster_sources(line_crs, raster_layers)
        if not raster_sources:
            ax.set_title("Select valid raster layer(s)")
            return
        min_step_m = max(1, self.interval_spin.value())
        adaptive = bool(getattr(self, 'adaptive_interval_chk', None) and self.adaptive_interval_chk.isChecked())
        adaptive_factor = float(self.adaptive_interval_factor.value()) if hasattr(self, 'adaptive_interval_factor') else 1.0

        # Guard against excessive sample counts that can freeze UI
        # - fixed interval: based on interval
        # - adaptive: based on minimum step (best-case lower bound on spacing)
        expected_samples = int(self.line_length / (min_step_m if adaptive else min_step_m)) + 1 if self.line_length > 0 else 0
        max_samples = self.max_samples_spin.value() if hasattr(self, 'max_samples_spin') else 50000
        auto_limit = self.auto_limit_chk.isChecked() if hasattr(self, 'auto_limit_chk') else True
        if expected_samples > max_samples:
            if auto_limit:
                # Increase minimum step to cap samples to <= max_samples
                new_min_step = int(self.line_length / max_samples) + 1
                if new_min_step > min_step_m:
                    self.iface.messageBar().pushMessage(
                        "Depth Profile",
                        f"Auto limit: minimum step raised {min_step_m}m -> {new_min_step}m (expected {expected_samples:,} > max {max_samples:,}).",
                        level=1, duration=7
                    )
                    min_step_m = new_min_step
            else:
                self.iface.messageBar().pushMessage(
                    "Depth Profile",
                    f"Warning: high sample count ({expected_samples:,}) exceeds max preference ({max_samples:,}) but Auto Limit is off.",
                    level=1, duration=8
                )
        # Quick envelope overlap test (rough): if route bbox doesn't intersect ANY selected raster extent, early exit.
        try:
            if self.line_parts:
                xs = [pt.x() for part in self.line_parts for pt in part]
                ys = [pt.y() for part in self.line_parts for pt in part]
                if xs and ys:
                    minx, maxx = min(xs), max(xs)
                    miny, maxy = min(ys), max(ys)
                    corners = [QgsPointXY(minx, miny), QgsPointXY(minx, maxy), QgsPointXY(maxx, miny), QgsPointXY(maxx, maxy)]
                    any_overlap = False
                    for src in raster_sources:
                        extent = src.get('extent')
                        transform = src.get('transform')
                        if extent is None:
                            continue
                        # Transform route corners into this raster CRS
                        tx = []
                        for c in corners:
                            try:
                                tx.append(transform.transform(c) if transform else c)
                            except Exception:
                                pass
                        if not tx:
                            continue
                        minx_t = min(p.x() for p in tx); maxx_t = max(p.x() for p in tx)
                        miny_t = min(p.y() for p in tx); maxy_t = max(p.y() for p in tx)
                        if not (maxx_t < extent.xMinimum() or minx_t > extent.xMaximum() or maxy_t < extent.yMinimum() or miny_t > extent.yMaximum()):
                            any_overlap = True
                            break
                    if not any_overlap:
                        ax.set_title("Route outside raster extent")
                        self.iface.messageBar().pushMessage("Depth Profile", "Selected route does not overlap any selected raster extent.", level=1, duration=6)
                        return
        except Exception:
            pass
        # Build arrays
        self.kp_values = []
        self.depth_values = []
        dist = 0.0
        valid_count = 0
        missing_count = 0

        # Helper to convert pixel area to an approximate pixel size (meters)
        def _pixel_size_m_for_src(src_dict):
            try:
                a = src_dict.get('pixel_area_m2')
                if a is None:
                    return None
                a = float(a)
                if a <= 0:
                    return None
                return math.sqrt(a)
            except Exception:
                return None

        while dist <= self.line_length:
            point_geom = self._interpolate_point(dist)
            if point_geom is None or point_geom.isEmpty():
                break
            pt = point_geom.asPoint()

            # Sample and capture which raster provided the value (if any)
            val, src_used = self._sample_rasters_at_point_with_source(QgsPointXY(pt.x(), pt.y()), raster_sources)
            if val is None:
                missing_count += 1
            else:
                valid_count += 1
            self.kp_values.append(dist / 1000.0)
            self.depth_values.append(val)

            # Step: fixed or adaptive based on raster resolution at this station.
            if adaptive:
                step = None
                if src_used is not None:
                    px = _pixel_size_m_for_src(src_used)
                    if px is not None:
                        step = max(min_step_m, float(adaptive_factor) * float(px))
                # Fallback when no raster coverage (or unknown resolution)
                if step is None:
                    step = float(min_step_m)
                # Safety clamps
                step = max(1.0, step)
                dist += step
            else:
                dist += float(min_step_m)
        # Ensure last point exactly at end
        if self.kp_values and (self.kp_values[-1] * 1000.0) < self.line_length:
            point_geom = self._interpolate_point(self.line_length)
            if point_geom and not point_geom.isEmpty():
                pt = point_geom.asPoint()
                val, _ = self._sample_rasters_at_point_with_source(QgsPointXY(pt.x(), pt.y()), raster_sources)
                if val is None:
                    missing_count += 1
                else:
                    valid_count += 1
                self.kp_values.append(self.line_length / 1000.0)
                self.depth_values.append(val)
        # Coverage warnings
        try:
            if valid_count == 0 and self.kp_values:
                self.iface.messageBar().pushMessage(
                    "Depth Profile", "No raster coverage along selected route (all samples null).", level=1, duration=6)
                ax.set_title("No raster coverage along route")
            elif valid_count > 0 and self.depth_values:
                ratio = valid_count / float(len(self.depth_values))
                if missing_count > 0:
                    self.iface.messageBar().pushMessage(
                        "Depth Profile",
                        f"Partial raster coverage: {ratio*100:.1f}% of samples valid ({missing_count:,} missing).",
                        level=1, duration=7)
        except Exception:
            pass

        # Persist adaptive sampling preferences
        try:
            if hasattr(self, 'adaptive_interval_chk'):
                self.settings.setValue("DepthProfile/adaptive_interval", bool(self.adaptive_interval_chk.isChecked()))
            if hasattr(self, 'adaptive_interval_factor'):
                self.settings.setValue("DepthProfile/adaptive_interval_factor", float(self.adaptive_interval_factor.value()))
        except Exception:
            pass

    def _sample_rasters_at_point_with_source(self, point_xy_line_crs, raster_sources):
        """Sample multiple rasters at a point and return (value, src_dict_used).

        point_xy_line_crs is in line CRS.
        Returns (None, None) if no valid sample.
        """
        if not raster_sources:
            return None, None
        for src in raster_sources:
            provider = src.get('provider')
            extent = src.get('extent')
            transform = src.get('transform')
            nodata = src.get('nodata')
            sample_pt = QgsPointXY(point_xy_line_crs.x(), point_xy_line_crs.y())
            if transform:
                try:
                    sample_pt = transform.transform(sample_pt)
                except Exception:
                    continue
            try:
                if extent and (sample_pt.x() < extent.xMinimum() or sample_pt.x() > extent.xMaximum() or sample_pt.y() < extent.yMinimum() or sample_pt.y() > extent.yMaximum()):
                    continue
            except Exception:
                pass
            try:
                sample, ok = provider.sample(sample_pt, 1)
            except Exception:
                continue
            if not ok:
                continue
            try:
                val = float(sample)
            except Exception:
                continue
            try:
                if nodata is not None and float(nodata) == val:
                    continue
            except Exception:
                pass
            if np.isnan(val):
                continue
            return val, src
        return None, None

    def _get_selected_raster_layer_ids(self):
        ids = []
        try:
            for i in range(self.raster_layer_list.count()):
                item = self.raster_layer_list.item(i)
                if item and item.checkState() == Qt.Checked:
                    layer_id = item.data(Qt.UserRole)
                    if layer_id:
                        ids.append(layer_id)
        except Exception:
            return []
        return ids

    def _get_selected_raster_layers(self):
        layers = []
        for layer_id in self._get_selected_raster_layer_ids():
            lyr = QgsProject.instance().mapLayer(layer_id)
            if lyr and isinstance(lyr, QgsRasterLayer):
                layers.append(lyr)
        return layers

    def _prepare_raster_sources(self, line_crs, raster_layers):
        """Prepare per-raster provider/extent/transform/nodata for fast repeated sampling."""
        sources = []
        for raster_layer in (raster_layers or []):
            if not raster_layer or not isinstance(raster_layer, QgsRasterLayer):
                continue
            provider = raster_layer.dataProvider()
            if provider is None:
                continue
            raster_crs = raster_layer.crs()
            transform = None
            if raster_crs != line_crs:
                try:
                    transform = QgsCoordinateTransform(line_crs, raster_crs, QgsProject.instance())
                except Exception:
                    transform = None
            nodata = None
            try:
                if provider.sourceHasNoDataValue(1):
                    nodata = provider.sourceNoDataValue(1)
            except Exception:
                nodata = None

            # Approximate raster resolution for overlap preference.
            # For projected CRSs with meter-ish units this is close to m².
            # For geographic CRSs, we convert pixel size to meters at raster center.
            pixel_area_m2 = None
            try:
                rupx = float(raster_layer.rasterUnitsPerPixelX())
                rupy = float(raster_layer.rasterUnitsPerPixelY())
                # Some rasters report negative y pixel size
                rupx = abs(rupx)
                rupy = abs(rupy)
                if rupx > 0 and rupy > 0:
                    if raster_crs.isGeographic():
                        # Convert degrees to meters at raster center using ellipsoidal measurement.
                        da = QgsDistanceArea()
                        da.setSourceCrs(raster_crs, QgsProject.instance().transformContext())
                        da.setEllipsoid(QgsProject.instance().ellipsoid())
                        c = raster_layer.extent().center()
                        p0 = QgsPointXY(c.x(), c.y())
                        px = QgsPointXY(c.x() + rupx, c.y())
                        py = QgsPointXY(c.x(), c.y() + rupy)
                        dx_m = float(da.measureLine(p0, px))
                        dy_m = float(da.measureLine(p0, py))
                        if dx_m > 0 and dy_m > 0:
                            pixel_area_m2 = dx_m * dy_m
                    else:
                        # Best-effort: assumes CRS units are meters (common for bathy)
                        pixel_area_m2 = rupx * rupy
            except Exception:
                pixel_area_m2 = None
            sources.append({
                'layer': raster_layer,
                'provider': provider,
                'extent': raster_layer.extent(),
                'transform': transform,
                'nodata': nodata,
                'pixel_area_m2': pixel_area_m2,
            })

        # Prefer higher resolution rasters first (smaller pixel area).
        # Keep unknown-resolution rasters last but stable.
        try:
            sources.sort(key=lambda s: (s.get('pixel_area_m2') is None, s.get('pixel_area_m2') if s.get('pixel_area_m2') is not None else float('inf')))
        except Exception:
            pass
        return sources

    def _sample_rasters_at_point(self, point_xy_line_crs, raster_sources):
        """Sample multiple rasters at a point (point_xy is in line CRS).

        Returns the first valid sample found, else None.
        """
        if not raster_sources:
            return None
        for src in raster_sources:
            provider = src.get('provider')
            extent = src.get('extent')
            transform = src.get('transform')
            nodata = src.get('nodata')
            sample_pt = QgsPointXY(point_xy_line_crs.x(), point_xy_line_crs.y())
            if transform:
                try:
                    sample_pt = transform.transform(sample_pt)
                except Exception:
                    continue
            try:
                if extent and (sample_pt.x() < extent.xMinimum() or sample_pt.x() > extent.xMaximum() or sample_pt.y() < extent.yMinimum() or sample_pt.y() > extent.yMaximum()):
                    continue
            except Exception:
                pass
            try:
                sample, ok = provider.sample(sample_pt, 1)
            except Exception:
                continue
            if not ok:
                continue
            try:
                val = float(sample)
            except Exception:
                continue
            try:
                if nodata is not None and float(nodata) == val:
                    continue
            except Exception:
                pass
            if np.isnan(val):
                continue
            return val
        return None

    def _build_route_stationing_cache(self):
        """Precompute segment stationing along the route for fast interpolation."""
        self._route_seg_starts_m = []
        self._route_seg_ends_m = []
        self._route_seg_lens_m = []
        self._route_seg_p1 = []
        self._route_seg_p2 = []

        if not self.line_parts:
            return
        cum = 0.0
        for part in self.line_parts:
            if not part or len(part) < 2:
                continue
            for p1, p2 in zip(part[:-1], part[1:]):
                try:
                    seg_len = float(self.distance_area.measureLine(p1, p2))
                except Exception:
                    seg_len = math.hypot(p2.x() - p1.x(), p2.y() - p1.y())
                if seg_len <= 0:
                    continue
                self._route_seg_starts_m.append(cum)
                self._route_seg_lens_m.append(seg_len)
                self._route_seg_p1.append(QgsPointXY(p1.x(), p1.y()))
                self._route_seg_p2.append(QgsPointXY(p2.x(), p2.y()))
                cum += seg_len
                self._route_seg_ends_m.append(cum)

        # If route had no valid segments, clear cache
        if not self._route_seg_ends_m:
            self._route_seg_starts_m = None
            self._route_seg_ends_m = None
            self._route_seg_lens_m = None
            self._route_seg_p1 = None
            self._route_seg_p2 = None

    def _sample_contour_mode(self, ax):
        contour_layer_id = self.contour_layer_combo.currentData()
        contour_layer = QgsProject.instance().mapLayer(contour_layer_id) if contour_layer_id else None
        contour_layer_id2 = self.contour_layer_combo2.currentData()
        contour_layer2 = QgsProject.instance().mapLayer(contour_layer_id2) if contour_layer_id2 else None
        
        contour_layers = []
        depth_fields = []
        
        if contour_layer and isinstance(contour_layer, QgsVectorLayer):
            depth_field = self.depth_field_combo.currentText()
            if depth_field:
                contour_layers.append(contour_layer)
                depth_fields.append(depth_field)
        
        if contour_layer2 and isinstance(contour_layer2, QgsVectorLayer):
            depth_field2 = self.depth_field_combo2.currentText()
            if depth_field2:
                contour_layers.append(contour_layer2)
                depth_fields.append(depth_field2)
            
        if not contour_layers:
            ax.set_title("Select valid contour layer(s) and depth field(s)")
            return
        kps = []
        depths = []
        route_geom = QgsGeometry.collectGeometry([QgsGeometry.fromPolylineXY(part) for part in self.line_parts]) if len(self.line_parts) > 1 else QgsGeometry.fromPolylineXY(self.line_parts[0])
        request = QgsFeatureRequest()
        line_crs = self.current_line_crs if self.current_line_crs else contour_layers[0].crs()
        
        for layer_idx, contour_layer in enumerate(contour_layers):
            depth_field = depth_fields[layer_idx]
            contour_crs = contour_layer.crs()
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
                    depth_val = float(feat[depth_field])
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
                    kp_m = self._measure_along_route(p)
                    if kp_m is None:
                        continue
                    kp_km = kp_m / 1000.0
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

        # Fast path: use cached stationing if available
        if (
            self._route_seg_ends_m is not None
            and self._route_seg_starts_m is not None
            and self._route_seg_lens_m is not None
            and self._route_seg_p1 is not None
            and self._route_seg_p2 is not None
            and len(self._route_seg_ends_m) > 0
        ):
            try:
                idx = bisect.bisect_left(self._route_seg_ends_m, float(distance_m))
                if idx < 0:
                    idx = 0
                if idx >= len(self._route_seg_ends_m):
                    idx = len(self._route_seg_ends_m) - 1
                seg_start = self._route_seg_starts_m[idx]
                seg_len = self._route_seg_lens_m[idx]
                p1 = self._route_seg_p1[idx]
                p2 = self._route_seg_p2[idx]
                ratio = (float(distance_m) - float(seg_start)) / float(seg_len) if seg_len > 0 else 0.0
                ratio = max(0.0, min(1.0, ratio))
                x = p1.x() + ratio * (p2.x() - p1.x())
                y = p1.y() + ratio * (p2.y() - p1.y())
                return QgsGeometry.fromPointXY(QgsPointXY(x, y))
            except Exception:
                pass

        # Fallback (original logic)
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
        # Segment-based data for CSV export
        self.segment_kp_from = []
        self.segment_kp_to = []
        self.segment_depth_from = []
        self.segment_depth_to = []
        self.segment_slope_deg = []
        self.segment_slope_pct = []
        self.segment_seabed_length = []
        self.segment_side_slope_deg = []
        self.segment_side_slope_pct = []
        self.segment_port_depth = []
        self.segment_starboard_depth = []
        self.segment_cross_span_m = []
        
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
            vertical_for_slope = -vertical if self.invert_slope_chk.isChecked() else vertical
            slope_rad = math.atan2(vertical_for_slope, horiz_m) if horiz_m > 0 else 0.0
            self.slope_deg.append(math.degrees(slope_rad))
            self.slope_pct.append(100.0 * vertical_for_slope / horiz_m if horiz_m > 0 else 0.0)
            
            # Populate segment data for CSV export
            self.segment_kp_from.append(self.kp_values[i-1])
            self.segment_kp_to.append(self.kp_values[i])
            self.segment_depth_from.append(v1)
            self.segment_depth_to.append(v2)
            self.segment_slope_deg.append(math.degrees(slope_rad))
            self.segment_slope_pct.append(100.0 * vertical_for_slope / horiz_m if horiz_m > 0 else 0.0)
            
            # Calculate 3D seabed length for this segment using same method as _compute_seabed_length
            # Note: seabed length uses actual depth difference, not inverted
            try:
                geom1 = self._interpolate_point(self.kp_values[i-1] * 1000.0)
                geom2 = self._interpolate_point(self.kp_values[i] * 1000.0)
                if geom1 and not geom1.isEmpty() and geom2 and not geom2.isEmpty():
                    p1 = geom1.asPoint()
                    p2 = geom2.asPoint()
                    plan_dist = self.distance_area.measureLine(p1, p2)
                    dz = v2 - v1  # Always use actual depth difference for 3D length
                    seabed_dist = math.sqrt(plan_dist**2 + dz**2)
                    # Transform to lat/lon
                    if self.current_line_crs:
                        transform = QgsCoordinateTransform(self.current_line_crs, self.distance_area.sourceCrs(), self.iface.mapCanvas().mapSettings().transformContext())
                        p1_latlon = transform.transform(p1)
                        p2_latlon = transform.transform(p2)
                        lat_from = p1_latlon.y()
                        lon_from = p1_latlon.x()
                        lat_to = p2_latlon.y()
                        lon_to = p2_latlon.x()
                    else:
                        lat_from = lon_from = lat_to = lon_to = None
                else:
                    # Fallback to simple calculation if interpolation fails
                    seabed_dist = math.sqrt(horiz_m**2 + vertical**2)
                    lat_from = lon_from = lat_to = lon_to = None
            except Exception:
                # Fallback to simple calculation
                seabed_dist = math.sqrt(horiz_m**2 + vertical**2)
                lat_from = lon_from = lat_to = lon_to = None
            
            # Also calculate simple Euclidean distance for Excel verification
            euclidean_dist = math.sqrt(horiz_m**2 + (v2 - v1)**2)
            
            self.segment_seabed_length.append(seabed_dist)
            # Store Euclidean distance for potential future use
            if not hasattr(self, 'segment_euclidean_length'):
                self.segment_euclidean_length = []
            self.segment_euclidean_length.append(euclidean_dist)
            self.segment_lat_from.append(lat_from)
            self.segment_lon_from.append(lon_from)
            self.segment_lat_to.append(lat_to)
            self.segment_lon_to.append(lon_to)

            # Side slope segment columns (aligned to KP_to).
            try:
                if self.side_slope_deg and len(self.side_slope_deg) > i:
                    self.segment_side_slope_deg.append(self.side_slope_deg[i])
                    self.segment_side_slope_pct.append(self.side_slope_pct[i] if self.side_slope_pct and len(self.side_slope_pct) > i else None)
                    self.segment_port_depth.append(self.side_port_depth[i] if self.side_port_depth and len(self.side_port_depth) > i else None)
                    self.segment_starboard_depth.append(self.side_starboard_depth[i] if self.side_starboard_depth and len(self.side_starboard_depth) > i else None)
                    self.segment_cross_span_m.append(self.side_cross_span_m[i] if self.side_cross_span_m and len(self.side_cross_span_m) > i else None)
                else:
                    self.segment_side_slope_deg.append(None)
                    self.segment_side_slope_pct.append(None)
                    self.segment_port_depth.append(None)
                    self.segment_starboard_depth.append(None)
                    self.segment_cross_span_m.append(None)
            except Exception:
                self.segment_side_slope_deg.append(None)
                self.segment_side_slope_pct.append(None)
                self.segment_port_depth.append(None)
                self.segment_starboard_depth.append(None)
                self.segment_cross_span_m.append(None)

    # ---------------------- Side slope (cross-profile) ----------------------
    def _compute_side_slopes_with_progress(self):
        """Compute side slope (+ve = down to starboard) with a progress indicator.

                - Raster mode: samples bathymetry across the transect and fits a line (depth vs cross distance).
                    This is smoother and less noisy than a single end-point difference.
                - Contour mode: collects all contour intersections along the transect and fits a line.
                    This avoids tiny-span blowups (which can yield near-vertical slopes).
        """
        if not self.kp_values:
            return

        search_m = float(self.side_slope_search_spin.value()) if hasattr(self, 'side_slope_search_spin') else 200.0
        if search_m <= 0:
            return

        # Reset arrays
        n = len(self.kp_values)
        self.side_slope_deg = [None] * n
        self.side_slope_pct = [None] * n
        self.side_port_depth = [None] * n
        self.side_starboard_depth = [None] * n
        self.side_cross_span_m = [None] * n

        progress = QProgressDialog("Computing side slope…", "Cancel", 0, n, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(200)
        progress.setValue(0)

        # Prepare method based on depth source
        mode = self.source_type_combo.currentText() if hasattr(self, 'source_type_combo') else "Raster"

        contour_index = None
        contour_data = None
        if mode == "Contours":
            contour_index, contour_data = self._build_combined_contour_index_for_side_slope()
            if contour_index is None or contour_data is None:
                self.iface.messageBar().pushMessage("Depth Profile", "Side slope: no contour data available.", level=1, duration=5)
                return

        raster_sources = None
        if mode == "Raster":
            raster_layers = self._get_selected_raster_layers() if hasattr(self, '_get_selected_raster_layers') else []
            if not raster_layers:
                self.iface.messageBar().pushMessage("Depth Profile", "Side slope: select one or more raster layers.", level=1, duration=5)
                return
            line_crs = self.current_line_crs if self.current_line_crs else raster_layers[0].crs()
            raster_sources = self._prepare_raster_sources(line_crs, raster_layers)
            if not raster_sources:
                self.iface.messageBar().pushMessage("Depth Profile", "Side slope: select valid raster layer(s).", level=1, duration=5)
                return

        # Tangent sampling distance (meters along route) - tie to station spacing for stability
        tangent_delta_m = 10.0
        try:
            if len(self.kp_values) >= 3:
                diffs = np.diff(np.asarray(self.kp_values, dtype=float)) * 1000.0
                diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
                if diffs.size:
                    spacing_m = float(np.median(diffs))
                    tangent_delta_m = max(5.0, min(50.0, spacing_m / 2.0))
        except Exception:
            tangent_delta_m = 10.0

        # Cross-profile sampling resolution (odd count includes center)
        cross_sample_count = 11
        try:
            if search_m >= 500:
                cross_sample_count = 21
        except Exception:
            pass

        # Compute side slope station-by-station
        for i, kp in enumerate(self.kp_values):
            if progress.wasCanceled():
                self.iface.messageBar().pushMessage("Depth Profile", "Side slope canceled.", level=1, duration=4)
                break

            if i % 25 == 0:
                progress.setValue(i)
                QApplication.processEvents()

            try:
                dist_m = float(kp) * 1000.0
                center_geom = self._interpolate_point(dist_m)
                if center_geom is None or center_geom.isEmpty():
                    continue
                center = center_geom.asPoint()

                # Derive local tangent using points ahead/behind
                d0 = max(0.0, dist_m - tangent_delta_m)
                d1 = min(self.line_length, dist_m + tangent_delta_m)
                g0 = self._interpolate_point(d0)
                g1 = self._interpolate_point(d1)
                if g0 is None or g1 is None or g0.isEmpty() or g1.isEmpty():
                    continue
                p0 = g0.asPoint(); p1 = g1.asPoint()
                # Determine route normal (starboard) and build transect endpoints.
                # If CRS is geographic, offsets must be geodesic (meters), not planar in degrees.
                is_geo = False
                normal_bearing = None
                try:
                    is_geo = bool(self.current_line_crs and self.current_line_crs.isGeographic())
                except Exception:
                    is_geo = False

                if is_geo:
                    try:
                        bearing = float(self.distance_area.bearing(QgsPointXY(p0.x(), p0.y()), QgsPointXY(p1.x(), p1.y())))
                    except Exception:
                        continue
                    # Starboard is +90° from forward bearing
                    normal_bearing = bearing + (math.pi / 2.0)
                    if normal_bearing is None:
                        continue
                    try:
                        stbd_pt = self.distance_area.computeSpheroidProject(QgsPointXY(center.x(), center.y()), search_m, normal_bearing)
                        port_pt = self.distance_area.computeSpheroidProject(QgsPointXY(center.x(), center.y()), search_m, normal_bearing + math.pi)
                    except Exception:
                        continue
                    # Provide a unit normal in local tangent space for sign computations (only used for contour t)
                    nx = math.sin(normal_bearing)
                    ny = math.cos(normal_bearing)
                else:
                    dx = p1.x() - p0.x(); dy = p1.y() - p0.y()
                    mag = math.hypot(dx, dy)
                    if mag <= 0:
                        continue
                    ux = dx / mag; uy = dy / mag
                    # Starboard (right) normal: rotate clockwise
                    nx = uy
                    ny = -ux
                    port_pt = QgsPointXY(center.x() - nx * search_m, center.y() - ny * search_m)
                    stbd_pt = QgsPointXY(center.x() + nx * search_m, center.y() + ny * search_m)

                if mode == "Raster":
                    if not raster_sources:
                        continue
                    # Sample across transect and fit depth = a + b*t, where t is cross distance (+ starboard).
                    offsets = np.linspace(-search_m, search_m, cross_sample_count)
                    t_vals = []
                    z_vals = []
                    port_z = None
                    stbd_z = None

                    for t in offsets:
                        if is_geo:
                            # Move along the normal geodesically by |t|.
                            if normal_bearing is None:
                                continue
                            try:
                                if t >= 0:
                                    pt = self.distance_area.computeSpheroidProject(QgsPointXY(center.x(), center.y()), float(t), normal_bearing)
                                else:
                                    pt = self.distance_area.computeSpheroidProject(QgsPointXY(center.x(), center.y()), float(-t), normal_bearing + math.pi)
                            except Exception:
                                continue
                        else:
                            pt = QgsPointXY(center.x() + nx * float(t), center.y() + ny * float(t))
                        # Use first raster with valid data at this point
                        z = self._sample_rasters_at_point(pt, raster_sources)
                        if z is None:
                            continue
                        t_vals.append(float(t))
                        z_vals.append(float(z))
                        if abs(t + search_m) < 1e-6:
                            port_z = float(z)
                        if abs(t - search_m) < 1e-6:
                            stbd_z = float(z)

                    if len(t_vals) < 2:
                        continue

                    b = self._ols_slope(t_vals, z_vals)
                    if b is None:
                        continue

                    # Prefer endpoint samples if available; otherwise predict from fit.
                    a = self._ols_intercept(t_vals, z_vals, b)
                    if a is None:
                        a = 0.0
                    if port_z is None:
                        port_z = a + b * (-search_m)
                    if stbd_z is None:
                        stbd_z = a + b * (search_m)

                    span = 2.0 * search_m
                    dz = float(stbd_z) - float(port_z)
                    slope_rad = math.atan2(b, 1.0)  # b = dz/dt

                    self.side_port_depth[i] = float(port_z)
                    self.side_starboard_depth[i] = float(stbd_z)
                    self.side_cross_span_m[i] = span
                    self.side_slope_deg[i] = math.degrees(slope_rad)
                    self.side_slope_pct[i] = 100.0 * float(b)
                else:
                    # Contours: collect all intersections along transect and fit depth vs signed distance.
                    transect = QgsGeometry.fromPolylineXY([port_pt, stbd_pt])
                    hits = self._contour_intersections(transect, center, nx, ny, contour_index, contour_data)
                    if not hits:
                        continue

                    # Reduce duplicates: for same depth, keep the closest-to-center hit per side.
                    best_by_depth_side = {}
                    for t, z, px, py in hits:
                        side = 1 if t > 0 else (-1 if t < 0 else 0)
                        if side == 0:
                            continue
                        key = (side, float(z))
                        abs_t = abs(float(t))
                        prev = best_by_depth_side.get(key)
                        if prev is None or abs_t < prev[0]:
                            best_by_depth_side[key] = (abs_t, float(t), float(z))
                    pairs = [(v[1], v[2]) for v in best_by_depth_side.values()]

                    if len(pairs) < 2:
                        continue

                    t_vals = [p[0] for p in pairs]
                    z_vals = [p[1] for p in pairs]
                    b = self._ols_slope(t_vals, z_vals)
                    if b is None:
                        continue
                    a = self._ols_intercept(t_vals, z_vals, b)
                    if a is None:
                        a = 0.0

                    # Determine representative port/stbd depths at the transect ends.
                    port_z = a + b * (-search_m)
                    stbd_z = a + b * (search_m)
                    span = 2.0 * search_m

                    slope_rad = math.atan2(float(b), 1.0)
                    self.side_port_depth[i] = float(port_z)
                    self.side_starboard_depth[i] = float(stbd_z)
                    self.side_cross_span_m[i] = span
                    self.side_slope_deg[i] = math.degrees(slope_rad)
                    self.side_slope_pct[i] = 100.0 * float(b)
            except Exception:
                continue

        progress.setValue(n)

        # Persist side slope settings
        try:
            self.settings.setValue("DepthProfile/side_slope_enabled", self.side_slope_chk.isChecked())
            self.settings.setValue("DepthProfile/side_slope_search_m", int(search_m))
            self.settings.setValue("DepthProfile/side_slope_plot", self.side_slope_plot_chk.isChecked())
        except Exception:
            pass

        # Refresh segment-side columns (segment arrays are built in _compute_slopes; rebuild to include side slope)
        try:
            self._compute_slopes()
        except Exception:
            pass

    def _ols_slope(self, x_vals, y_vals):
        """Return OLS slope for y = a + b*x.

        x_vals/y_vals are iterables of floats.
        """
        try:
            if not x_vals or not y_vals or len(x_vals) != len(y_vals) or len(x_vals) < 2:
                return None
            x = np.asarray(x_vals, dtype=float)
            y = np.asarray(y_vals, dtype=float)
            mask = np.isfinite(x) & np.isfinite(y)
            x = x[mask]
            y = y[mask]
            if x.size < 2:
                return None
            x_mean = float(np.mean(x))
            y_mean = float(np.mean(y))
            denom = float(np.sum((x - x_mean) ** 2))
            if denom <= 0:
                return None
            num = float(np.sum((x - x_mean) * (y - y_mean)))
            return num / denom
        except Exception:
            return None

    def _ols_intercept(self, x_vals, y_vals, slope_b):
        try:
            x = np.asarray(x_vals, dtype=float)
            y = np.asarray(y_vals, dtype=float)
            mask = np.isfinite(x) & np.isfinite(y)
            x = x[mask]
            y = y[mask]
            if x.size == 0:
                return None
            return float(np.mean(y) - float(slope_b) * np.mean(x))
        except Exception:
            return None

    def _sample_raster_at_point(self, point_xy, provider, extent, transform_line_to_raster):
        """Sample raster at a point (point_xy is in line CRS)."""
        if provider is None:
            return None
        sample_pt = QgsPointXY(point_xy.x(), point_xy.y())
        if transform_line_to_raster:
            try:
                sample_pt = transform_line_to_raster.transform(sample_pt)
            except Exception:
                pass
        try:
            if extent and (sample_pt.x() < extent.xMinimum() or sample_pt.x() > extent.xMaximum() or sample_pt.y() < extent.yMinimum() or sample_pt.y() > extent.yMaximum()):
                return None
        except Exception:
            pass
        try:
            sample, ok = provider.sample(sample_pt, 1)
            if not ok:
                return None
            return float(sample)
        except Exception:
            return None

    def _build_combined_contour_index_for_side_slope(self):
        """Build a combined spatial index and geometry/depth cache for up to two contour layers.

        Returns (QgsSpatialIndex, dict[index_id] = (QgsGeometry, depth_float)).
        All geometries are transformed into the route CRS (line CRS).
        """
        contour_layers = []
        depth_fields = []

        layer_id1 = self.contour_layer_combo.currentData() if hasattr(self, 'contour_layer_combo') else None
        layer1 = QgsProject.instance().mapLayer(layer_id1) if layer_id1 else None
        if layer1 and isinstance(layer1, QgsVectorLayer):
            df1 = self.depth_field_combo.currentText() if hasattr(self, 'depth_field_combo') else ''
            if df1:
                contour_layers.append(layer1)
                depth_fields.append(df1)

        layer_id2 = self.contour_layer_combo2.currentData() if hasattr(self, 'contour_layer_combo2') else None
        layer2 = QgsProject.instance().mapLayer(layer_id2) if layer_id2 else None
        if layer2 and isinstance(layer2, QgsVectorLayer):
            df2 = self.depth_field_combo2.currentText() if hasattr(self, 'depth_field_combo2') else ''
            if df2:
                contour_layers.append(layer2)
                depth_fields.append(df2)

        if not contour_layers:
            return None, None

        line_crs = self.current_line_crs if self.current_line_crs else contour_layers[0].crs()
        index = QgsSpatialIndex()
        data = {}
        next_id = 1

        for layer_idx, layer in enumerate(contour_layers):
            depth_field = depth_fields[layer_idx]
            transform = None
            if layer.crs() != line_crs:
                try:
                    transform = QgsCoordinateTransform(layer.crs(), line_crs, QgsProject.instance())
                except Exception:
                    transform = None

            for feat in layer.getFeatures(QgsFeatureRequest()):
                try:
                    geom = feat.geometry()
                    if geom is None or geom.isEmpty():
                        continue
                    if transform:
                        geom = QgsGeometry(geom)
                        geom.transform(transform)
                    depth_val = feat[depth_field]
                    if depth_val is None:
                        continue
                    depth_f = float(depth_val)
                except Exception:
                    continue

                try:
                    f = QgsFeature()
                    f.setId(next_id)
                    f.setGeometry(geom)
                    index.addFeature(f)
                    data[next_id] = (geom, depth_f)
                    next_id += 1
                except Exception:
                    continue

        if not data:
            return None, None
        return index, data

    def _contour_intersections(self, transect, center_point, nx, ny, contour_index, contour_data):
        """Collect contour intersections along a transect.

        Returns list of tuples: (t, depth, x, y)
        where t is signed distance along normal (+ = starboard, - = port).
        """
        if transect is None or transect.isEmpty() or contour_index is None or contour_data is None:
            return []

        bbox = transect.boundingBox()
        candidate_ids = contour_index.intersects(bbox)
        if not candidate_ids:
            return []

        c = center_point
        cx = c.x(); cy = c.y()
        is_geo = False
        try:
            is_geo = bool(self.current_line_crs and self.current_line_crs.isGeographic())
        except Exception:
            is_geo = False

        out = []
        for cid in candidate_ids:
            item = contour_data.get(cid)
            if not item:
                continue
            geom, depth = item
            try:
                inter = transect.intersection(geom)
            except Exception:
                continue
            if inter is None or inter.isEmpty():
                continue

            points = []
            try:
                if inter.type() == QgsWkbTypes.PointGeometry:
                    points = inter.asMultiPoint() if inter.isMultipart() else [inter.asPoint()]
                elif inter.type() == QgsWkbTypes.LineGeometry:
                    # Overlap: use vertices (rare). This can still help build a fit.
                    if inter.isMultipart():
                        for part in inter.asMultiPolyline():
                            points.extend(part)
                    else:
                        points.extend(inter.asPolyline())
            except Exception:
                points = []

            for p in points:
                try:
                    if is_geo:
                        # Compute signed cross distance in meters using geodesic distance,
                        # with sign from dot product in coordinate space (good enough for sign).
                        sign_v = (p.x() - cx) * nx + (p.y() - cy) * ny
                        sign = 1.0 if sign_v > 0 else (-1.0 if sign_v < 0 else 0.0)
                        if sign == 0.0:
                            t = 0.0
                        else:
                            try:
                                dist_m = float(self.distance_area.measureLine(QgsPointXY(cx, cy), QgsPointXY(p.x(), p.y())))
                            except Exception:
                                dist_m = 0.0
                            t = sign * dist_m
                    else:
                        vx = p.x() - cx
                        vy = p.y() - cy
                        t = float(vx * nx + vy * ny)
                    out.append((t, float(depth), float(p.x()), float(p.y())))
                except Exception:
                    continue

        return out

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
        side_d = self.side_slope_deg[idx] if (self.side_slope_deg and idx < len(self.side_slope_deg)) else None
        side_p = self.side_slope_pct[idx] if (self.side_slope_pct and idx < len(self.side_slope_pct)) else None
        lines = [f"KP: {kp:.3f}"]
        if depth is not None: lines.append(f"Depth: {depth:.2f}")
        if slope_d is not None: lines.append(f"Slope°: {slope_d:.2f}")
        if slope_p is not None: lines.append(f"Slope%: {slope_p:.2f}")
        if side_d is not None: lines.append(f"SideSlope°: {side_d:.2f}")
        if side_p is not None: lines.append(f"SideSlope%: {side_p:.2f}")
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
        self.side_slope_deg = []
        self.side_slope_pct = []
        self.side_port_depth = []
        self.side_starboard_depth = []
        self.side_cross_span_m = []
        # Clear segment data
        self.segment_kp_from = []
        self.segment_kp_to = []
        self.segment_depth_from = []
        self.segment_depth_to = []
        self.segment_slope_deg = []
        self.segment_slope_pct = []
        self.segment_side_slope_deg = []
        self.segment_side_slope_pct = []
        self.segment_port_depth = []
        self.segment_starboard_depth = []
        self.segment_cross_span_m = []
        self.segment_seabed_length = []
        self.segment_euclidean_length = []
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
        self._closing = True
        try:
            self._pending_layer_refresh = False
        except Exception:
            pass
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
        self._disconnect_project_signals()
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
        """Export the current segment-based KP, Depth, and Slope data to a CSV file."""
        if not self.segment_kp_from:
            self.iface.messageBar().pushMessage("Depth Profile", "No profile data to export. Generate first.", level=1, duration=4)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", "depth_profile.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write("KP_from (km),KP_to (km),Lat_from,Lon_from,Lat_to,Lon_to,Depth_from (m),Depth_to (m),Slope (deg),Slope (%),SideSlope (deg),SideSlope (%),PortDepth (m),StbdDepth (m),CrossSpan (m),Seabed_Length (m),Euclidean_Length (m)\n")
                for kp_from, kp_to, lat_from, lon_from, lat_to, lon_to, depth_from, depth_to, slope_deg, slope_pct, side_deg, side_pct, port_z, stbd_z, span_m, seabed_len, euclidean_len in zip(
                    self.segment_kp_from, self.segment_kp_to, self.segment_lat_from, self.segment_lon_from, 
                    self.segment_lat_to, self.segment_lon_to, self.segment_depth_from, 
                    self.segment_depth_to, self.segment_slope_deg, self.segment_slope_pct,
                    self.segment_side_slope_deg if self.segment_side_slope_deg else [None] * len(self.segment_kp_from),
                    self.segment_side_slope_pct if self.segment_side_slope_pct else [None] * len(self.segment_kp_from),
                    self.segment_port_depth if self.segment_port_depth else [None] * len(self.segment_kp_from),
                    self.segment_starboard_depth if self.segment_starboard_depth else [None] * len(self.segment_kp_from),
                    self.segment_cross_span_m if self.segment_cross_span_m else [None] * len(self.segment_kp_from),
                    self.segment_seabed_length, self.segment_euclidean_length):
                    kp_from_str = f"{kp_from:.3f}" if kp_from is not None else ""
                    kp_to_str = f"{kp_to:.3f}" if kp_to is not None else ""
                    lat_from_str = f"{lat_from:.6f}" if lat_from is not None else ""
                    lon_from_str = f"{lon_from:.6f}" if lon_from is not None else ""
                    lat_to_str = f"{lat_to:.6f}" if lat_to is not None else ""
                    lon_to_str = f"{lon_to:.6f}" if lon_to is not None else ""
                    depth_from_str = f"{depth_from:.3f}" if depth_from is not None else ""
                    depth_to_str = f"{depth_to:.3f}" if depth_to is not None else ""
                    slope_deg_str = f"{slope_deg:.3f}" if slope_deg is not None else ""
                    slope_pct_str = f"{slope_pct:.3f}" if slope_pct is not None else ""
                    side_deg_str = f"{side_deg:.3f}" if side_deg is not None else ""
                    side_pct_str = f"{side_pct:.3f}" if side_pct is not None else ""
                    port_z_str = f"{port_z:.3f}" if port_z is not None else ""
                    stbd_z_str = f"{stbd_z:.3f}" if stbd_z is not None else ""
                    span_str = f"{span_m:.3f}" if span_m is not None else ""
                    seabed_len_str = f"{seabed_len:.3f}" if seabed_len is not None else ""
                    euclidean_len_str = f"{euclidean_len:.3f}" if euclidean_len is not None else ""
                    f.write(f"{kp_from_str},{kp_to_str},{lat_from_str},{lon_from_str},{lat_to_str},{lon_to_str},{depth_from_str},{depth_to_str},{slope_deg_str},{slope_pct_str},{side_deg_str},{side_pct_str},{port_z_str},{stbd_z_str},{span_str},{seabed_len_str},{euclidean_len_str}\n")
            self.iface.messageBar().pushMessage("Depth Profile", f"CSV exported: {path}", level=0, duration=5)
        except Exception as e:
            self.iface.messageBar().pushMessage("Depth Profile", f"Failed to write CSV: {e}", level=2, duration=6)
