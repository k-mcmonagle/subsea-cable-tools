from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QSpinBox, QCheckBox, QFileDialog, QTabWidget, QFormLayout, QProgressBar,
    QListWidget, QListWidgetItem, QDoubleSpinBox, QInputDialog, QToolButton
)
from qgis.PyQt.QtCore import Qt, QSettings, QTimer
from qgis.core import (
    QgsApplication, QgsProject, QgsVectorLayer, QgsRasterLayer, QgsGeometry,
    QgsFeatureRequest, QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsCsException,
)
from .qgis_compat import SIZE_POLICY_EXPANDING, GEOMETRY_POINT, GEOMETRY_LINE, MESSAGE_INFO, MESSAGE_WARNING, MESSAGE_CRITICAL
from qgis.gui import QgsVertexMarker, QgsRubberBand
from .maptools.temp_line_maptool import TempLineMapTool  # new temporary line drawing tool
from .maptools.profile_measure_controller import HINT as MEASURE_HINT, ProfileMeasureController
from .maptools.profile_measurements import UNITS, write_measurements_csv
from .kp_range_utils import make_kp_distance_area
from .bathymetry_sampling import configure_layers
from .slope_utils import contiguous_runs
from .depth_profile_core import (
    CONTOURS, RASTER, DepthProfileTask, ProfileParams, ProfileResult, build_request,
    route_from_features, route_from_points, run_profile,
)
from .plugin_log import log_exception

# Added standard library & third-party imports
import logging
import math
import bisect
import numpy as np
from .plot_widget import Figure, FigureCanvas, NavigationToolbar

# Colour cycle used when one line per raster is drawn on the depth plot.
_RASTER_SERIES_COLORS = (
    'tab:blue', 'tab:orange', 'tab:green', 'tab:red', 'tab:purple',
    'tab:brown', 'tab:pink', 'tab:olive', 'tab:cyan', 'tab:gray',
)

# depth_profile_core message levels -> message bar levels.
_MESSAGE_LEVELS = {'info': MESSAGE_INFO, 'warning': MESSAGE_WARNING, 'critical': MESSAGE_CRITICAL}

# Simple sip deletion check fallback
try:  # sip is available in QGIS Python env; guard for static analysis
    from qgis.PyQt import sip  # type: ignore
    _sip_isdeleted = sip.isdeleted
except Exception:  # pragma: no cover
    try:
        import sip  # type: ignore
        _sip_isdeleted = sip.isdeleted
    except Exception:
        def _sip_isdeleted(_obj):
            return False


class DepthProfileDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("Depth Profile", parent)
        self.iface = iface
        self.setObjectName("DepthProfileDockWidget")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.settings = QSettings()
        self._closing = False
        self._shut_down = False
        self._project_signals_connected = False
        # Internal runtime state. The generated profile (route stationing and
        # per-station series) is one depth_profile_core.ProfileResult.
        self.profile = ProfileResult()
        # Background generation: the running task, a token that supersedes
        # older runs, and references keeping queued/cancelled tasks alive.
        self._task = None
        self._generation = 0
        self._live_tasks = set()
        self.marker = None
        self.vertical_line = None
        self.vertical_line2 = None  # for dual plot
        self._route_kp = None  # nearest-KP mapping onto a reference route
        self.canvas_cid = None
        self._right_click_cid = None
        self._tooltip_cid = None
        # Temporary line drawing state
        self.temp_drawn_points = []  # list of QgsPointXY in project CRS
        self.temp_line_tool = None
        self.temp_line_rubber = None  # persistent rubber band showing drawn line
        # Plotted X (displayed KP, km) per station, and a sorted view for
        # nearest-station lookup from the cursor. Reverse KP makes the plotted
        # X differ from the true distance along the route.
        self._plot_x = []
        self._hover_sorted_x = []
        self._hover_order = []
        self._context_station = None
        self._depth_axis = None
        self._marker_xform = None
        self._status_msg = None
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

        self.selected_only_chk = QCheckBox("Selected only")
        self.selected_only_chk.setToolTip(
            "Use only the selected features of the route layer, e.g. one route in a multi-route layer.\n"
            "A single feature keeps its digitised direction, so KP 0 is its first vertex.")
        self.selected_only_chk.setChecked(bool(self.settings.value("DepthProfile/selected_only", False, type=bool)))
        line_row.addWidget(self.selected_only_chk)

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
        settings_btn = QPushButton("Source conventions…")
        settings_btn.clicked.connect(lambda: configure_layers(self))
        source_row.addWidget(settings_btn)
        source_row.addStretch()
        form_layout.addRow(source_row)
        
        # Row 2: Raster and Contour Layers
        raster_row = QHBoxLayout()
        raster_row.addWidget(QLabel("Raster Layer(s):"))
        self.raster_layer_list = QListWidget()
        self.raster_layer_list.setMinimumWidth(220)
        self.raster_layer_list.setMaximumHeight(80)
        self.raster_layer_list.setToolTip("Tick one or more rasters. The composite profile uses the highest-resolution raster with valid data at each point; missing coverage remains null.")
        raster_row.addWidget(self.raster_layer_list)
        self.plot_rasters_separately_chk = QCheckBox("Plot each raster")
        self.plot_rasters_separately_chk.setToolTip(
            "Draw one depth line per selected raster (plus the composite used for slope and seabed length).\n"
            "Samples every raster at every station, so generation is slower with many rasters."
        )
        self.plot_rasters_separately_chk.setChecked(bool(self.settings.value("DepthProfile/plot_rasters_separately", True, type=bool)))
        raster_row.addWidget(self.plot_rasters_separately_chk)
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
        # Side slope controls (cross-profile)
        self.side_slope_chk = QCheckBox("Cross tilt / local max")
        self.side_slope_chk.setChecked(bool(self.settings.value("DepthProfile/side_slope_enabled", False, type=bool)))
        options_row.addWidget(self.side_slope_chk)
        options_row.addWidget(QLabel("Cross Search (m):"))
        self.side_slope_search_spin = QSpinBox()
        self.side_slope_search_spin.setRange(1, 50000)
        self.side_slope_search_spin.setValue(int(self.settings.value("DepthProfile/side_slope_search_m", 200)))
        self.side_slope_search_spin.setToolTip("Cross-profile half-width in metres. Endpoint tilt needs complete coverage; local maximum is reported separately. Contours search twice this distance for endpoint brackets.")
        options_row.addWidget(self.side_slope_search_spin)
        self.side_slope_plot_chk = QCheckBox("Plot Side")
        self.side_slope_plot_chk.setChecked(bool(self.settings.value("DepthProfile/side_slope_plot", True, type=bool)))
        options_row.addWidget(self.side_slope_plot_chk)
        options_row.addWidget(QLabel("Slope Window (m):"))
        self.slope_window_spin = QSpinBox()
        self.slope_window_spin.setRange(0, 100000)
        self.slope_window_spin.setValue(int(self.settings.value("DepthProfile/slope_window_m", 0)))
        self.slope_window_spin.setSpecialValueText("Automatic (native resolution)")
        self.slope_window_spin.setToolTip(
            "0: automatic native-resolution baseline (contours use exact crossing intervals). "
            "A positive length is a fixed physical baseline in metres. Unsupported windows, gaps and source seams give no slope.")
        options_row.addWidget(self.slope_window_spin)
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

        # X-axis KP labelling: distance along the profile line itself, or the
        # nearest KP on a reference route (drawn lines, cross-sections).
        kp_row = QHBoxLayout()
        kp_row.addWidget(QLabel("X-axis KP:"))
        self.kp_axis_combo = QComboBox()
        self.kp_axis_combo.addItem("Distance along profile line", "line")
        self.kp_axis_combo.addItem("Nearest KP on route", "route")
        self.kp_axis_combo.setToolTip(
            "Distance along profile line: the profile's own chainage (KP 0 = first vertex).\n"
            "Nearest KP on route: ticks at round KPs of the chosen route, placed where the\n"
            "profile line's nearest route KP crosses them. Spacing, slopes and measurements\n"
            "stay distance along the profile line; a line across the route can revisit KPs.")
        mode_idx = self.kp_axis_combo.findData(self.settings.value("DepthProfile/kp_axis_mode", "line"))
        self.kp_axis_combo.setCurrentIndex(max(0, mode_idx))
        kp_row.addWidget(self.kp_axis_combo)
        self.kp_ref_combo = QComboBox()
        self.kp_ref_combo.setMinimumWidth(120)
        self.kp_ref_combo.setToolTip("Route line layer whose KP labels the X axis")
        kp_row.addWidget(self.kp_ref_combo)
        self.kp_ref_selected_chk = QCheckBox("Selected only")
        self.kp_ref_selected_chk.setToolTip("Use only the selected features of the KP route layer")
        self.kp_ref_selected_chk.setChecked(bool(self.settings.value("DepthProfile/kp_ref_selected_only", False, type=bool)))
        kp_row.addWidget(self.kp_ref_selected_chk)
        kp_row.addStretch()
        form_layout.addRow(kp_row)
        
        # Checkboxes row
        checkboxes_row = QHBoxLayout()
        self.reverse_kp_chk = QCheckBox("Reverse KP")
        self.reverse_kp_chk.setChecked(bool(self.settings.value("DepthProfile/reverse_kp", False, type=bool)))
        checkboxes_row.addWidget(self.reverse_kp_chk)
        invert_kp_default = bool(self.settings.value("DepthProfile/reverse_x_axis", False, type=bool))
        self.invert_kp_axis_chk = QCheckBox("Invert KP Axis")
        self.invert_kp_axis_chk.setChecked(bool(self.settings.value("DepthProfile/invert_kp_axis", invert_kp_default, type=bool)))
        checkboxes_row.addWidget(self.invert_kp_axis_chk)
        self.invert_depth_axis_chk = QCheckBox("Invert Depth Axis")
        self.invert_depth_axis_chk.setChecked(True)
        checkboxes_row.addWidget(self.invert_depth_axis_chk)
        self.invert_slope_axis_chk = QCheckBox("Invert Slope Axis")
        self.invert_slope_axis_chk.setChecked(bool(self.settings.value("DepthProfile/invert_slope_axis", False, type=bool)))
        checkboxes_row.addWidget(self.invert_slope_axis_chk)
        # v2 key: the sign convention under this option changed, so stale
        # persisted values from the old semantics must not carry over.
        self.invert_slope_chk = QCheckBox("Invert Slope Sign")
        self.invert_slope_chk.setChecked(bool(self.settings.value("DepthProfile/invert_slope_v2", False, type=bool)))
        self.invert_slope_chk.setToolTip(
            "Unchecked (default): +ve slope = shoaling with increasing KP "
            "(up-slope), the plugin-wide convention shared by the KP Mouse "
            "profile, the Workbench and the Burial Planner. Checked: +ve "
            "slope = deepening (down-slope). The seabed datum (positive-down "
            "depths vs negative elevations) is detected automatically, so "
            "the sign does not depend on how the raster stores depth.")
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
        # Background generation progress (hidden while idle)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setMaximumWidth(160)
        self.progress_bar.setVisible(False)
        button_layout.addWidget(self.progress_bar)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Stop generating. Cross tilt stops early and keeps the stations done so far.")
        self.cancel_btn.setVisible(False)
        button_layout.addWidget(self.cancel_btn)
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
        # Plot area
        self.figure = Figure(figsize=(6, 4)); self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(SIZE_POLICY_EXPANDING, SIZE_POLICY_EXPANDING)
        self.toolbar = NavigationToolbar(self.canvas, self)
        view_row = QHBoxLayout()
        view_row.addWidget(self.toolbar)
        view_row.addWidget(QLabel("Vertical exaggeration:"))
        self.ve_spin = QDoubleSpinBox()
        self.ve_spin.setRange(0.0, 10000.0)
        self.ve_spin.setDecimals(1)
        self.ve_spin.setSingleStep(1.0)
        self.ve_spin.setSpecialValueText("Free")
        self.ve_spin.setValue(float(self.settings.value("DepthProfile/vertical_exaggeration", 0.0)))
        self.ve_spin.setToolTip(
            "Lock the depth plot's aspect: 1 = true scale (1 m across = 1 m down), 10 = depths drawn 10× taller.\n"
            "Free lets both axes zoom independently. Measurements always use true distances.")
        view_row.addWidget(self.ve_spin)
        self.save_png_btn = QPushButton("Save PNG…")
        self.save_png_btn.setToolTip("Save the plots, including measurements, as a PNG image")
        view_row.addWidget(self.save_png_btn)
        view_row.addStretch()
        profile_layout.addLayout(view_row)

        # On-plot measurements (shared with the KP Mouse quick profile)
        self.measure = ProfileMeasureController(
            self, plot_factors=lambda: (0.001, 1.0),
            text_units=lambda: (self.measure_units_combo.currentText(), 'm'),
            depth_down=self.invert_depth_axis_chk.isChecked,
            x_inverted=self.invert_kp_axis_chk.isChecked)
        measure_row = QHBoxLayout()
        self.measure_btn = QToolButton()
        self.measure_btn.setDefaultAction(self.measure.action)
        self.measure_btn.setToolTip("Measure on the depth plot: click two points (also in the plot's right-click menu)")
        measure_row.addWidget(self.measure_btn)
        measure_row.addWidget(self.measure.snap_check)
        measure_row.addWidget(self.measure.source_combo, 1)
        measure_row.addWidget(QLabel("Units:"))
        self.measure_units_combo = QComboBox()
        self.measure_units_combo.addItems([u for u in UNITS if u != 'miles'])
        self.measure_units_combo.setCurrentText(str(self.settings.value("DepthProfile/measure_units", "m")))
        self.measure_units_combo.setToolTip("Horizontal units for measurement lengths (depths stay in metres)")
        measure_row.addWidget(self.measure_units_combo)
        measure_row.addWidget(self.measure.delete_btn)
        measure_row.addWidget(self.measure.clear_btn)
        self.export_measurements_btn = QPushButton("Measurements CSV…")
        measure_row.addWidget(self.export_measurements_btn)
        profile_layout.addLayout(measure_row)
        profile_layout.addWidget(self.canvas, 1)
        self.measure.table.setMaximumHeight(120)
        profile_layout.addWidget(self.measure.table)
        self.plot_status_label = QLabel("Generate a profile on the Setup tab.")
        self.plot_status_label.setWordWrap(True)
        self.plot_status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        profile_layout.addWidget(self.plot_status_label)
        self._update_measure_controls()
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
            "    <li>Select a <b>Route Line Layer</b> (must be a line geometry layer representing the cable route). Tick <b>Selected only</b> to profile just the selected feature(s).</li>"
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
            "    <li>Interactive plot with map marker and crosshair synced to KP (correct with <b>Reverse KP</b> too). Right-click the plot and choose <b>Centre map on this KP</b> to pan the map there.</li>"
            "    <li><b>Measure</b> (Depth Profile tab, or the plot's right-click menu): click two points on the depth plot to measure length, horizontal (X) and vertical (Y) separation and angle. With <b>Snap to profile</b>, endpoints sit on the chosen profile line and the distance along the seabed is reported. Drag an endpoint to adjust it; Escape cancels, Delete removes. Export with <b>Measurements CSV…</b>.</li>"
            "    <li><b>Vertical exaggeration</b> locks the depth plot's aspect (1 = true scale); <b>Free</b> zooms each axis independently. <b>Save PNG…</b> captures the plots and measurements.</li>"
            "    <li>Export profile to DXF for CAD/GIS use (units selectable; optional KP marker ticks + labels; optional events from a point layer using a KP field).</li>"
            "  </ul>"
            "</li>"
            "<li><b>Tips & Notes:</b>"
            "  <ul>"
            "    <li>Ensure all layers use the same CRS as the project for correct marker placement.</li>"
            "    <li>KP (chainage) is measured geodesically on the WGS84 ellipsoid, the same way in every Subsea Cable Tools KP tool and the Burial Planner.</li>"
            "    <li><b>Reverse KP</b> re-numbers KP along the route, while <b>Invert KP Axis</b> only flips the displayed X-axis direction.</li>"
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
        help_label.setTextFormat(Qt.TextFormat.RichText)
        help_label.setWordWrap(True)
        help_label.setText(help_text)
        help_layout.addWidget(help_label)
        help_layout.addStretch(1)
        self.tab_widget.addTab(self.help_tab, "Help")
        # Connections
        self.generate_btn.clicked.connect(self.generate_profile)
        self.cancel_btn.clicked.connect(self._cancel_clicked)
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
        self.kp_axis_combo.currentIndexChanged.connect(self.update_enable_states)
        # DXF export
        self.export_dxf_btn.clicked.connect(self.export_dxf)
        # CSV export
        self.export_csv_btn.clicked.connect(self.export_csv)
        # Plot tab: view, measurement and image export
        self.selected_only_chk.toggled.connect(self.update_sample_estimate)
        self.ve_spin.valueChanged.connect(self._apply_aspect)
        self.save_png_btn.clicked.connect(self.export_png)
        self.export_measurements_btn.clicked.connect(self.export_measurements_csv)
        self.measure_units_combo.currentTextChanged.connect(self._measure_units_changed)
        self.measure.modeChanged.connect(self._on_measure_mode)
        self.measure.statusChanged.connect(self.plot_status_label.setText)
        self.measure.measurementsChanged.connect(self._update_measure_controls)
        # Populate & signals
        # Layer combo population + reactive updates
        self._pending_layer_refresh = False
        self.populate_layer_combos(); self.update_enable_states()
        self.update_sample_estimate()
        # Project/layer signals: use a debounced scheduler so rapid batch adds only trigger one refresh
        self._connect_project_signals()
        # Dock placement (only once)
        try:
            main_win = self.iface.mainWindow(); main_win.removeDockWidget(self); main_win.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self)
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
        except AttributeError:  # stub iface (tests) without projectRead
            log_exception("Depth profile: no projectRead signal", level=logging.DEBUG)
        proj = QgsProject.instance()
        # A failed connection silently stops the layer lists refreshing.
        try:
            proj.layerWasAdded.connect(self.on_layer_event)
        except Exception:
            log_exception("Depth profile: layer-added refresh unavailable")
        try:
            proj.layersAdded.connect(self.on_layers_added)
        except Exception:
            log_exception("Depth profile: layers-added refresh unavailable")
        try:
            proj.layerRemoved.connect(self.on_layer_event)
        except Exception:
            log_exception("Depth profile: layer-removed refresh unavailable")
        try:
            proj.layersRemoved.connect(self.on_layer_event)
        except Exception:
            log_exception("Depth profile: layers-removed refresh unavailable")
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
        self._shut_down = False
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
        prev_kp_ref = self.kp_ref_combo.currentData() or self.settings.value("DepthProfile/kp_ref_layer", "")
        prev_rasters = set(self._get_selected_raster_layer_ids())
        prev_contour = self.contour_layer_combo.currentData()
        prev_contour2 = self.contour_layer_combo2.currentData()
        self.line_layer_combo.blockSignals(True)
        self.kp_ref_combo.blockSignals(True)
        self.raster_layer_list.blockSignals(True)
        self.contour_layer_combo.blockSignals(True)
        self.contour_layer_combo2.blockSignals(True)
        try:
            self.line_layer_combo.clear()
            self.kp_ref_combo.clear()
            self.raster_layer_list.clear()
            self.contour_layer_combo.clear()
            self.contour_layer_combo2.clear()

            # Contour layer 2 is optional
            self.contour_layer_combo2.addItem("(None)", None)
            for layer in QgsProject.instance().mapLayers().values():
                if isinstance(layer, QgsVectorLayer) and layer.geometryType() == GEOMETRY_LINE:
                    self.line_layer_combo.addItem(layer.name(), layer.id())
                    self.kp_ref_combo.addItem(layer.name(), layer.id())
                if isinstance(layer, QgsRasterLayer):
                    item = QListWidgetItem(layer.name())
                    item.setData(Qt.ItemDataRole.UserRole, layer.id())
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if layer.id() in prev_rasters else Qt.CheckState.Unchecked)
                    self.raster_layer_list.addItem(item)
                if isinstance(layer, QgsVectorLayer) and layer.geometryType() == GEOMETRY_LINE:
                    self.contour_layer_combo.addItem(layer.name(), layer.id())
                    self.contour_layer_combo2.addItem(layer.name(), layer.id())
        finally:
            self.line_layer_combo.blockSignals(False)
            self.kp_ref_combo.blockSignals(False)
            self.raster_layer_list.blockSignals(False)
            self.contour_layer_combo.blockSignals(False)
            self.contour_layer_combo2.blockSignals(False)
        # Restore selections where possible
        if prev_line:
            idx = self.line_layer_combo.findData(prev_line)
            if idx != -1:
                self.line_layer_combo.setCurrentIndex(idx)
        if prev_kp_ref:
            idx = self.kp_ref_combo.findData(prev_kp_ref)
            if idx != -1:
                self.kp_ref_combo.setCurrentIndex(idx)
        # If nothing selected, default to first raster (previous behavior: single selection)
        if not self._get_selected_raster_layer_ids() and self.raster_layer_list.count() > 0:
            try:
                self.raster_layer_list.item(0).setCheckState(Qt.CheckState.Checked)
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
        if hasattr(self, 'plot_rasters_separately_chk'):
            self.plot_rasters_separately_chk.setEnabled(raster_mode)
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
        route_kp = self.kp_axis_combo.currentData() == "route"
        self.kp_ref_combo.setEnabled(route_kp)
        self.kp_ref_selected_chk.setEnabled(route_kp)
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
                da = make_kp_distance_area(
                    project.crs(), project.transformContext(), project=project
                )
                length = 0.0
                for a, b in zip(self.temp_drawn_points[:-1], self.temp_drawn_points[1:]):
                    length += float(da.measureLine(a, b))
                return length
        except Exception:
            log_exception("Depth profile: drawn-line length estimate failed", level=logging.DEBUG)

        # Layer route
        try:
            layer_id = self.line_layer_combo.currentData()
            line_layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
            if not line_layer or not isinstance(line_layer, QgsVectorLayer) or line_layer.geometryType() != GEOMETRY_LINE:
                return None
            da = make_kp_distance_area(
                line_layer.sourceCrs(), project.transformContext(), project=project
            )
            total = 0.0
            req = QgsFeatureRequest()
            try:
                req.setNoAttributes()
            except Exception:
                pass
            for f in self._route_features(line_layer, req):
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                try:
                    total += float(da.measureLength(g))
                except Exception:
                    # No planar fallback: on a geographic layer g.length()
                    # is DEGREES and would silently report a route length
                    # off by ~10^5. Skipping the part is the honest answer.
                    continue
            return total if total > 0 else None
        except Exception:
            log_exception("Depth profile: route length estimate failed", level=logging.DEBUG)
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
        1. Determine route geometry (layer or drawn line) and snapshot the
           depth sources (main thread).
        2. Sample depth values (raster or contours), side slopes, slope and
           seabed 3D length in a background QgsTask (depth_profile_core).
        3. Plot (single or dual) and update interactivity (_apply_result).
        Generating again, closing the dock or unloading the plugin cancels a
        run still in progress; its result is ignored.
        """
        self.clear_plot()
        self._status_msg = None
        ax = self.figure.add_subplot(111)

        # 1. Route geometry and input snapshot
        route = self._load_route()
        if route is None:
            self._finish_plot(ax, None, [], message=self._status_msg)
            return
        request = self._build_request(route)
        if request.status is not None:
            # Nothing to sample (no usable depth source): report it now.
            self._apply_result(run_profile(request))
            return

        # 2. Computation off the GUI thread
        self._generation += 1
        token = self._generation
        task = DepthProfileTask(request, lambda done, token=token: self._on_generation_finished(done, token))
        task.progressChanged.connect(lambda pct, token=token: self._on_generation_progress(token, pct))
        self._task = task
        self._live_tasks.add(task)  # keep the Python task alive until finished()
        self._set_generating(True)
        QgsApplication.taskManager().addTask(task)

    def is_generating(self):
        """True while a profile is being computed in the background."""
        return self._task is not None

    def _build_request(self, route):
        """Snapshot the depth sources and options for the worker (main thread)."""
        raster_mode = self.source_type_combo.currentText() == "Raster"
        params = ProfileParams(
            mode=RASTER if raster_mode else CONTOURS,
            interval_m=self.interval_spin.value(),
            adaptive=self.adaptive_interval_chk.isChecked(),
            adaptive_factor=float(self.adaptive_interval_factor.value()),
            max_samples=self.max_samples_spin.value(),
            auto_limit=self.auto_limit_chk.isChecked(),
            per_raster=self.plot_rasters_separately_chk.isChecked(),
            slope_window_m=float(self.slope_window_spin.value()),
            invert_slope=self.invert_slope_chk.isChecked(),
            side_slopes=self.side_slope_chk.isChecked(),
            side_search_m=float(self.side_slope_search_spin.value()))
        return build_request(route, params, QgsProject.instance().transformContext(),
                             raster_layers=self._get_selected_raster_layers() if raster_mode else (),
                             contour_layers=() if raster_mode else self._get_selected_contour_layers())

    def _on_generation_progress(self, token, pct):
        if token == self._generation and not self._closing and not _sip_isdeleted(self.progress_bar):
            self.progress_bar.setValue(int(pct))

    def _on_generation_finished(self, task, token):
        """Main thread: apply a finished run unless it was superseded."""
        self._live_tasks.discard(task)
        result, cancelled, error = task.result, task.cancelled, task.error
        # Release the cloned providers / feature sources here, not in the worker.
        task.request = None
        task.result = None
        if token != self._generation or self._closing:
            return  # superseded by a newer run, or the dock was closed/unloaded
        self._task = None
        self._set_generating(False)
        if result is not None:
            self._apply_result(result)
            return
        if error:
            self.iface.messageBar().pushMessage("Depth Profile", f"Profile generation failed: {error}",
                                                level=MESSAGE_CRITICAL, duration=8)
        self._status_msg = "Profile generation cancelled" if cancelled else "Profile generation failed"
        self.figure.clear()
        self._finish_plot(self.figure.add_subplot(111), None, [], message=self._status_msg)

    def _cancel_clicked(self):
        """Cancel button: stop the run. Side slopes stop early and keep the
        stations done; a cancelled along-route pass leaves no profile."""
        task = self._task
        if task is not None:
            self.plot_status_label.setText("Cancelling…")
            task.cancel()  # a task not started yet finishes (cancelled) right here

    def _cancel_generation(self):
        """Supersede any running generation: its result will be ignored."""
        self._generation += 1
        task, self._task = self._task, None
        if task is not None:
            try:
                task.cancel()
            except RuntimeError:  # task already deleted by the task manager
                pass
        self._set_generating(False)

    def _set_generating(self, active):
        """Show the progress bar and Cancel button while a run is active."""
        if _sip_isdeleted(self.progress_bar):
            return
        if active:
            # The old plot is gone: nothing to measure until the new one lands.
            self._depth_axis = None
            self.measure.attach(None)
            self._update_measure_controls()
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(active)
        self.cancel_btn.setVisible(active)
        if active:
            self.plot_status_label.setText("Generating profile…")

    def _apply_result(self, result):
        """Adopt a computed profile: messages, settings, interactivity, plot."""
        self.profile = result
        self._status_msg = result.status
        for text, level, duration in result.messages:
            self.iface.messageBar().pushMessage("Depth Profile", text, level=_MESSAGE_LEVELS[level],
                                                duration=duration)
        self._persist_computation_settings(result)
        dual = self.dual_plot_chk.isChecked() if hasattr(self, 'dual_plot_chk') else False
        self.connect_canvas_events()
        if self.show_tooltips_chk.isChecked():
            self.enable_tooltips()
        self._persist_settings(dual)
        self._plot_profile(dual)

    def _persist_computation_settings(self, result):
        """Settings the computation stages remember, only when they ran."""
        params = result.params
        if params is None:
            return
        if result.raster_sampled:
            self.settings.setValue("DepthProfile/adaptive_interval", bool(params.adaptive))
            self.settings.setValue("DepthProfile/adaptive_interval_factor", float(params.adaptive_factor))
            self.settings.setValue("DepthProfile/plot_rasters_separately", bool(params.per_raster))
        if result.side_slopes_ran:
            self.settings.setValue("DepthProfile/side_slope_enabled", bool(params.side_slopes))
            self.settings.setValue("DepthProfile/side_slope_search_m", int(params.side_search_m))
            self.settings.setValue("DepthProfile/side_slope_plot", self.side_slope_plot_chk.isChecked())
        if len(result.kp_values) >= 2:
            self.settings.setValue("DepthProfile/slope_window_m", int(params.slope_window_m))

    def _plot_profile(self, dual):
        """Plot the current profile (single variable or depth + slope)."""
        p = self.profile
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        x_vals = self._display_kp()
        has_depth = bool(x_vals) and any(v is not None for v in p.depth_values)
        if not has_depth:
            self._finish_plot(ax, None, [], message=self._status_msg or "No profile data generated")
            return
        line_length = p.route.line_length

        if dual:
            self.figure.clear()
            ax_depth = self.figure.add_subplot(211)
            ax_slope = self.figure.add_subplot(212, sharex=ax_depth)
            self._plot_depth_series(ax_depth, x_vals)
            ax_depth.set_ylabel("Depth (m)")
            if self.invert_depth_axis_chk.isChecked():
                ax_depth.invert_yaxis()
            if self.invert_kp_axis_chk.isChecked():
                ax_depth.invert_xaxis()
            ax_depth.grid(True); ax_depth.legend(loc='upper right')
            slope_unit = self.slope_unit_combo.currentText() if hasattr(self, 'slope_unit_combo') else 'Slope (deg)'
            if 'deg' in slope_unit:
                y_slope = p.slope_deg; slope_label = 'Slope (deg)'
            else:
                y_slope = p.slope_pct; slope_label = 'Slope (%)'
            ax_slope.plot(x_vals, y_slope, color='tab:orange', label=slope_label)
            self._plot_side_slopes(ax_slope, x_vals, 'deg' in slope_unit)
            ax_slope.set_ylabel(slope_label); ax_slope.set_xlabel("KP (km)")
            if self.invert_slope_axis_chk.isChecked():
                ax_slope.invert_yaxis()
            ax_slope.grid(True); ax_slope.legend(loc='upper right')
            # Length summary (plan vs seabed) on the top plot
            seabed_len = p.seabed_length
            if line_length and seabed_len:
                delta = seabed_len - p.seabed_covered_m
                ratio = seabed_len / p.seabed_covered_m if p.seabed_covered_m > 0 else 0
                ax_depth.set_title(f"Plan: {line_length:,.1f} m | Covered plan: {p.seabed_covered_m:,.1f} m | Seabed: {seabed_len:,.1f} m (Δ {delta:,.1f} m, {ratio:,.3f}x)")
            self._finish_plot(ax_depth, ax_depth, x_vals)
            return

        # Single variable
        var = self.variable_combo.currentText()
        if var.startswith("Depth"):
            ax.set_ylabel("Depth (m)")
            if self.invert_depth_axis_chk.isChecked(): ax.invert_yaxis()
            self._plot_depth_series(ax, x_vals, single_label=var)
        else:
            ax.set_ylabel("Slope (deg)" if "deg" in var else "Slope (%)")
            ax.plot(x_vals, p.slope_deg if "deg" in var else p.slope_pct, label=var)
            if self.invert_slope_axis_chk.isChecked():
                ax.invert_yaxis()
            self._plot_side_slopes(ax, x_vals, "deg" in var)
        if self.invert_kp_axis_chk.isChecked():
            ax.invert_xaxis()
        ax.set_xlabel("KP (km)"); ax.grid(True); ax.legend()
        ax.set_title(f"Plan: {line_length:,.1f} m | Covered plan: {p.seabed_covered_m:,.1f} m | Sampled seabed: {p.seabed_length:,.1f} m")
        self._finish_plot(ax, ax if var.startswith("Depth") else None, x_vals)

    def _load_route(self):
        """Route stationing from the drawn line or route layer (main thread).

        Returns None (with self._status_msg set) when no usable route exists.
        """
        project = QgsProject.instance()
        if self.use_drawn_chk.isChecked() and bool(self.temp_drawn_points):
            distance_area = make_kp_distance_area(
                project.crs(), project.transformContext(), project=project
            )
            route, self._status_msg = route_from_points(self.temp_drawn_points, project.crs(), distance_area)
            return route

        line_layer_id = self.line_layer_combo.currentData()
        line_layer = project.mapLayer(line_layer_id) if line_layer_id else None
        if not line_layer or not isinstance(line_layer, QgsVectorLayer) or line_layer.geometryType() != GEOMETRY_LINE:
            self._status_msg = "Select a valid route line layer or draw a line"
            return None
        route_features = [f for f in self._route_features(line_layer)
                          if f.hasGeometry() and not f.geometry().isEmpty()]
        if not route_features:
            self._status_msg = ("No selected route features" if self.selected_only_chk.isChecked()
                                else "Route layer empty")
            return None
        distance_area = make_kp_distance_area(
            line_layer.sourceCrs(), project.transformContext(), project=project
        )
        route, self._status_msg = route_from_features(route_features, line_layer.sourceCrs(), distance_area)
        if route is not None and len(route.line_parts) > 1:
            self.iface.messageBar().pushMessage(
                "Depth Profile",
                f"Route has {len(route.line_parts)} disconnected parts; KP runs through them in order without "
                "counting the gaps. Use 'Selected only' to profile one route.",
                level=MESSAGE_WARNING, duration=8)
        return route if self._status_msg is None else None

    def _route_features(self, line_layer, request=None):
        request = request or QgsFeatureRequest()
        if self.selected_only_chk.isChecked():
            return list(line_layer.getSelectedFeatures(request))
        return list(line_layer.getFeatures(request))

    def _build_route_kp(self):
        """Nearest-KP mapping of the profile line onto the chosen KP route.

        Returns a :class:`kp_axis.KPCrossings` over *plotted* X in metres
        (Reverse KP already applied), or None when the axis shows the
        profile line's own distance or no usable route is selected.
        """
        p = self.profile
        if self.kp_axis_combo.currentData() != "route" or not p.kp_values:
            return None
        project = QgsProject.instance()
        layer = project.mapLayer(self.kp_ref_combo.currentData() or "")
        if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != GEOMETRY_LINE:
            self.iface.messageBar().pushMessage(
                "Depth Profile", "Choose a route line layer for 'Nearest KP on route'.",
                level=MESSAGE_WARNING, duration=6)
            return None
        features = (layer.getSelectedFeatures() if self.kp_ref_selected_chk.isChecked()
                    else layer.getFeatures())
        geoms = [QgsGeometry(f.geometry()) for f in features
                 if f.hasGeometry() and not f.geometry().isEmpty()]
        if not geoms:
            self.iface.messageBar().pushMessage(
                "Depth Profile", "The KP route layer has no (selected) line features.",
                level=MESSAGE_WARNING, duration=6)
            return None
        from .kp_axis import KPCrossings
        from .kp_geo_utils import RouteFrame
        route = p.route
        # Cached in the profile line's CRS so both share one distance area.
        frame = RouteFrame.from_source(geoms, route.distance_area, target_crs=route.crs,
                                       source_crs=layer.sourceCrs(), project=project)
        end_m = p.kp_values[-1] * 1000.0
        reverse = self.reverse_kp_chk.isChecked()

        def route_kp(x_m):
            point = route.point_at(end_m - x_m if reverse else x_m)
            if point is None:
                return None
            hit = frame.kp_at_point(point)
            return hit.kp_km if hit.snapped_xy is not None else None

        samples = 240
        xs = [end_m * i / samples for i in range(samples + 1)]
        crossings = KPCrossings(xs, [route_kp(x) for x in xs], refine=route_kp)
        if not crossings:
            return None
        crossings.route_name = layer.name()
        crossings.route_kp = route_kp
        return crossings
    def _apply_kp_axes(self):
        """Round-KP ticks (3 dp) on every plot's X axis.

        Linear mode for the profile line's own KP; mapped mode for the
        nearest KP on a reference route.
        """
        from .kp_axis_item import KPAxisItem
        crossings = self._route_kp
        axes = self.figure.get_axes()
        for i, ax in enumerate(axes):
            plot_item = getattr(ax, 'plot_item', None)
            if plot_item is None:
                continue
            old = plot_item.getAxis('bottom')
            if not isinstance(old, KPAxisItem):
                grid, label, units = old.grid, old.labelText, old.labelUnits
                axis = KPAxisItem()
                plot_item.setAxisItems({'bottom': axis})
                axis.setGrid(grid)
                if label:
                    plot_item.setLabel('bottom', label, units=units or None)
            axis = plot_item.getAxis('bottom')
            if crossings:
                axis.set_mapped(crossings, metres_per_unit=1000.0)
                if i == len(axes) - 1:
                    plot_item.setLabel('bottom', "Nearest KP on %s (km) — spacing along profile line"
                                       % crossings.route_name)
            else:
                axis.set_linear(1.0)

    def _display_kp(self):
        """Plotted X (km) per station: KP, re-numbered from the end if Reverse KP."""
        kp_values = self.profile.kp_values
        if self.reverse_kp_chk.isChecked() and kp_values:
            end = kp_values[-1]
            return [end - kp for kp in kp_values]
        return list(kp_values)

    def _plot_side_slopes(self, ax, x_vals, degrees):
        """Overlay cross tilt / local max on a slope axis when enabled."""
        if not (self.side_slope_chk.isChecked() and self.side_slope_plot_chk.isChecked()):
            return
        p = self.profile
        y_side = p.side_slope_deg if degrees else p.side_slope_pct
        if not y_side or len(y_side) != len(x_vals):
            return
        y_clean = [np.nan if v is None else v for v in y_side]
        if any(not np.isnan(v) for v in y_clean):
            ax.plot(x_vals, y_clean, color='tab:green', alpha=0.9,
                    label='Cross tilt (deg)' if degrees else 'Cross tilt (%)')
        if p.side_local_max_deg and len(p.side_local_max_deg) == len(x_vals):
            ax.plot(x_vals, [np.nan if v is None else (v if degrees else 100 * math.tan(math.radians(v)))
                             for v in p.side_local_max_deg],
                    color='tab:red', linestyle=':', label='Max local cross slope')

    def _finish_plot(self, ax, depth_ax, x_vals, message=None):
        """Common tail of generate_profile: interactivity, measurements, status."""
        if message:
            ax.set_title(message)
        self._set_hover_axis(x_vals)
        self._depth_axis = depth_ax
        self.measure.attach(depth_ax.plot_item if depth_ax is not None else None,
                            self._measurement_series(x_vals) if depth_ax is not None else [])
        if depth_ax is None and self.measure.active:
            self.measure.action.setChecked(False)
        for axis in self.figure.get_axes():
            self._add_context_actions(axis)
        self._route_kp = None
        if x_vals:
            try:
                self._route_kp = self._build_route_kp()
            except Exception as e:
                self.iface.messageBar().pushMessage(
                    "Depth Profile", f"Route KP labels failed: {e}", level=MESSAGE_WARNING, duration=6)
        self._apply_kp_axes()
        self._apply_aspect()
        self._update_measure_controls()
        self._update_plot_status(message)
        try: self.figure.tight_layout()
        except Exception: pass
        self.canvas.draw()
        if x_vals:
            self.tab_widget.setCurrentWidget(self.profile_tab)

    def _measurement_series(self, x_vals):
        """Measurable depth lines in plotted order: x metres ascending."""
        if not x_vals:
            return []
        p = self.profile
        order = sorted(range(len(x_vals)), key=x_vals.__getitem__)
        xs = [x_vals[i] * 1000.0 for i in order]
        sources = p.depth_source_ids if len(p.depth_source_ids or []) == len(x_vals) else None
        series = []
        for s in p.raster_series or []:
            if len(s['depths']) == len(x_vals) and any(v is not None for v in s['depths']):
                series.append({'name': s['name'], 'x': xs, 'y': [s['depths'][i] for i in order]})
        composite = {'name': 'Composite (best resolution)' if len(series) > 1 else 'Seabed profile',
                     'x': xs, 'y': [p.depth_values[i] for i in order],
                     # Snapping never interpolates across a raster seam.
                     'sources': [sources[i] for i in order] if sources else None}
        return [composite] + (series if len(series) > 1 else [])

    def _add_context_actions(self, axis):
        try:
            menu = axis.plot_item.vb.getMenu(None)
        except Exception:
            log_exception("Depth profile: no plot context menu for 'Centre map on this KP'", level=logging.DEBUG)
            return
        if not any(a.text() == "Centre map on this KP" for a in menu.actions()):
            action = menu.addAction("Centre map on this KP")
            action.triggered.connect(self._centre_map_on_context_station)

    def _apply_aspect(self, *args):
        ax = self._depth_axis
        if ax is None or _sip_isdeleted(ax.plot_item):
            return
        ve = float(self.ve_spin.value())
        vb = ax.plot_item.vb
        if ve > 0:
            # ratio = x-scale / y-scale in pixels per plot unit; X is km, Y m.
            vb.setAspectLocked(True, ratio=1.0 / (ve * 0.001))
        else:
            vb.setAspectLocked(False)
        self.settings.setValue("DepthProfile/vertical_exaggeration", ve)

    def _on_measure_mode(self, checked):
        if checked and self._depth_axis is None:
            self.measure.action.setChecked(False)
            self.plot_status_label.setText("Measurements need the depth plot: choose 'Depth (m)' or 'Depth + Slope'.")
            return
        self._update_plot_status()

    def _measure_units_changed(self, unit):
        self.settings.setValue("DepthProfile/measure_units", unit)
        self.measure.refresh()

    def _update_measure_controls(self, *args):
        has_plot = self._depth_axis is not None
        has_measurements = bool(self.measure.measurements)
        self.measure.action.setEnabled(has_plot)
        for widget in (self.measure.snap_check, self.measure.source_combo):
            widget.setEnabled(has_plot)
        self.measure.delete_btn.setEnabled(has_measurements or self.measure.first_point is not None)
        self.measure.clear_btn.setEnabled(has_measurements)
        self.export_measurements_btn.setEnabled(has_measurements)
        self.measure.table.setVisible(has_measurements)

    def _update_plot_status(self, message=None):
        if message:
            self.plot_status_label.setText(message)
            return
        p = self.profile
        if not p.kp_values:
            self.plot_status_label.setText("Generate a profile on the Setup tab.")
            return
        parts = [f"Plan {p.route.line_length:,.1f} m",
                 f"covered {p.seabed_covered_m:,.1f} m",
                 f"seabed {p.seabed_length:,.1f} m"]
        valid = [(abs(v), i) for i, v in enumerate(p.slope_deg) if v is not None]
        if valid:
            peak, index = max(valid)
            parts.append(f"max |slope| {peak:.2f}° at KP {self._plot_x[index]:.3f}")
        widths = [w for w in (p.slope_baseline_m or []) if w]
        if widths:
            parts.append(f"slope baseline {min(widths):.1f}–{max(widths):.1f} m")
        text = " · ".join(parts)
        if self.measure.active:
            text += ". " + MEASURE_HINT
        elif self._depth_axis is not None:
            text += ". Use Measure to measure between two points on the depth plot."
        self.plot_status_label.setText(text)

    def _persist_settings(self, dual):
        self.settings.setValue("DepthProfile/interval_m", self.interval_spin.value())
        self.settings.setValue("DepthProfile/reverse_kp", self.reverse_kp_chk.isChecked())
        self.settings.setValue("DepthProfile/invert_kp_axis", self.invert_kp_axis_chk.isChecked())
        self.settings.setValue("DepthProfile/reverse_x_axis", self.invert_kp_axis_chk.isChecked())
        self.settings.setValue("DepthProfile/invert_slope_axis", self.invert_slope_axis_chk.isChecked())
        self.settings.setValue("DepthProfile/invert_slope_v2", self.invert_slope_chk.isChecked())
        self.settings.setValue("DepthProfile/selected_only", self.selected_only_chk.isChecked())
        if not dual:
            self.settings.setValue("DepthProfile/variable", self.variable_combo.currentText())
        self.settings.setValue("DepthProfile/dual_plot", dual)
        if hasattr(self, 'slope_unit_combo'):
            self.settings.setValue("DepthProfile/slope_unit", self.slope_unit_combo.currentText())
        if hasattr(self, 'auto_limit_chk'):
            self.settings.setValue("DepthProfile/auto_limit_samples", self.auto_limit_chk.isChecked())
        if hasattr(self, 'max_samples_spin'):
            self.settings.setValue("DepthProfile/max_samples", self.max_samples_spin.value())
        self.settings.setValue("DepthProfile/kp_axis_mode", self.kp_axis_combo.currentData())
        self.settings.setValue("DepthProfile/kp_ref_layer", self.kp_ref_combo.currentData() or "")
        self.settings.setValue("DepthProfile/kp_ref_selected_only", self.kp_ref_selected_chk.isChecked())

    def _plot_depth_series(self, ax, x_vals, single_label='Depth (m)'):
        """Draw the depth curve(s) on ax.

        When several rasters were sampled separately, one line per raster is drawn
        plus the composite series (dashed) that slope and seabed length derive from.
        Otherwise a single composite line is drawn, as before.
        """
        p = self.profile
        valid = [v for v in p.depth_values if v is not None]
        if valid:
            # Shade below the seabed, per contiguous run so gaps and raster
            # seams stay unshaded.
            floor = max(valid) + max(.1, (max(valid)-min(valid))*.05)
            for a,b in contiguous_runs([kp*1000 for kp in p.kp_values],p.depth_values,
                                       group_ids=p.depth_source_ids or None):
                ax.fill_between(x_vals[a:b+1],p.depth_values[a:b+1],floor,color='steelblue',alpha=.12)
        plottable = []
        for idx, s in enumerate(p.raster_series or []):
            if len(s['depths']) != len(x_vals):
                continue
            y_vals = [np.nan if v is None else v for v in s['depths']]
            if not any(not np.isnan(v) for v in y_vals):
                continue  # raster does not cover the route; keep it out of the legend
            plottable.append((idx, s['name'], y_vals))

        if len(plottable) < 2:
            ax.plot(x_vals, [np.nan if v is None else v for v in p.depth_values],
                    color='tab:blue', label=single_label)
            return

        for idx, name, y_vals in plottable:
            ax.plot(x_vals, y_vals, alpha=0.85, label=name,
                    color=_RASTER_SERIES_COLORS[idx % len(_RASTER_SERIES_COLORS)])
        composite = [np.nan if v is None else v for v in p.depth_values]
        ax.plot(x_vals, composite, color='black', linestyle='--', linewidth=0.9,
                alpha=0.6, label='Composite (best resolution)')

    def _get_selected_raster_layer_ids(self):
        ids = []
        try:
            for i in range(self.raster_layer_list.count()):
                item = self.raster_layer_list.item(i)
                if item and item.checkState() == Qt.CheckState.Checked:
                    layer_id = item.data(Qt.ItemDataRole.UserRole)
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

    def _get_selected_contour_layers(self):
        """(layer, depth field) for each chosen contour layer with a depth field."""
        pairs = []
        for combo, field_combo in ((self.contour_layer_combo, self.depth_field_combo),
                                   (self.contour_layer_combo2, self.depth_field_combo2)):
            layer_id = combo.currentData()
            layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
            if layer and isinstance(layer, QgsVectorLayer):
                depth_field = field_combo.currentText()
                if depth_field:
                    pairs.append((layer, depth_field))
        return pairs

    def _station_lonlat(self, kp_km, transform):
        """(lat, lon) of a chainage for CSV export, or (None, None)."""
        route = self.profile.route
        point = route.point_at(kp_km * 1000.0) if route is not None else None
        if transform is None or point is None:
            return None, None
        try:
            point = transform.transform(point)
        except QgsCsException:
            return None, None
        return point.y(), point.x()

    # ---------------------- Interactivity ----------------------
    def connect_canvas_events(self):
        if not self.canvas:
            return
        if self.canvas_cid is None:
            try:
                self.canvas_cid = self.canvas.mpl_connect('motion_notify_event', self.on_mouse_move)
            except Exception:
                log_exception("Depth profile: plot hover (map marker) unavailable")
        if self._right_click_cid is None:
            try:
                self._right_click_cid = self.canvas.mpl_connect('button_press_event', self.on_right_click)
            except Exception:
                log_exception("Depth profile: plot right-click (centre map) unavailable")

    def disconnect_canvas_events(self):
        if self.canvas and self.canvas_cid is not None:
            try: self.canvas.mpl_disconnect(self.canvas_cid)
            except Exception: log_exception("Depth profile: hover disconnect", level=logging.DEBUG)
            self.canvas_cid = None
        if self.canvas and self._right_click_cid is not None:
            try: self.canvas.mpl_disconnect(self._right_click_cid)
            except Exception: log_exception("Depth profile: right-click disconnect", level=logging.DEBUG)
            self._right_click_cid = None
        if self._tooltip_cid is not None and self.canvas:
            try: self.canvas.mpl_disconnect(self._tooltip_cid)
            except Exception: log_exception("Depth profile: tooltip disconnect", level=logging.DEBUG)
            self._tooltip_cid = None

    def enable_tooltips(self):
        if self.canvas and self._tooltip_cid is None:
            try:
                self._tooltip_cid = self.canvas.mpl_connect('motion_notify_event', self.show_tooltip)
            except Exception:
                log_exception("Depth profile: plot tooltips unavailable")

    def toggle_tooltips(self):
        if self.show_tooltips_chk.isChecked():
            self.enable_tooltips()
        else:
            if self._tooltip_cid is not None and self.canvas:
                try: self.canvas.mpl_disconnect(self._tooltip_cid)
                except Exception: log_exception("Depth profile: tooltip disconnect", level=logging.DEBUG)
                self._tooltip_cid = None

    def _set_hover_axis(self, x_vals):
        """Index plotted X for nearest-station lookup (works for Reverse KP)."""
        self._plot_x = list(x_vals)
        self._hover_order = sorted(range(len(self._plot_x)), key=self._plot_x.__getitem__)
        self._hover_sorted_x = [self._plot_x[i] for i in self._hover_order]

    def _station_for_plot_x(self, x):
        """Index of the station nearest plotted X (displayed KP), or None."""
        xs = self._hover_sorted_x
        if not xs or x is None or len(self._plot_x) != len(self.profile.kp_values):
            return None
        j = bisect.bisect_left(xs, x)
        if j >= len(xs):
            j = len(xs) - 1
        elif j > 0 and (x - xs[j - 1]) <= (xs[j] - x):
            j -= 1
        return self._hover_order[j]

    def show_tooltip(self, event):
        idx = self._station_for_plot_x(event.xdata) if event.inaxes else None
        if idx is None:
            self.canvas.setToolTip(""); return
        p = self.profile
        depth = p.depth_values[idx] if idx < len(p.depth_values) else None
        slope_d = p.slope_deg[idx] if idx < len(p.slope_deg) else None
        slope_p = p.slope_pct[idx] if idx < len(p.slope_pct) else None
        side_d = p.side_slope_deg[idx] if (p.side_slope_deg and idx < len(p.side_slope_deg)) else None
        side_p = p.side_slope_pct[idx] if (p.side_slope_pct and idx < len(p.side_slope_pct)) else None
        route_kp = self._route_kp.route_kp(self._plot_x[idx] * 1000.0) if self._route_kp else None
        if self._route_kp:
            lines = [f"Route KP: {route_kp:.3f}" if route_kp is not None else "Route KP: —",
                     f"Along line: {self._plot_x[idx]:.3f} km"]
        else:
            lines = [f"KP: {self._plot_x[idx]:.3f}"]
        if depth is not None: lines.append(f"Depth: {depth:.2f}")
        if slope_d is not None: lines.append(f"Slope°: {slope_d:.2f}")
        if slope_p is not None: lines.append(f"Slope%: {slope_p:.2f}")
        if side_d is not None: lines.append(f"SideSlope°: {side_d:.2f}")
        if side_p is not None: lines.append(f"SideSlope%: {side_p:.2f}")
        self.canvas.setToolTip("\n".join(lines))

    def on_mouse_move(self, event):
        idx = self._station_for_plot_x(event.xdata) if event.inaxes else None
        if idx is None:
            if self.marker and self.marker.isVisible():
                self.marker.hide()
            redraw = False
            if self.vertical_line and self.vertical_line.get_visible():
                self.vertical_line.set_visible(False); redraw = True
            if self.vertical_line2 and self.vertical_line2.get_visible():
                self.vertical_line2.set_visible(False); redraw = True
            if redraw:
                self.canvas.draw_idle()
            return
        # The crosshair sits at the station's plotted X; the map marker at its
        # true distance along the route (they differ when Reverse KP is on).
        plot_x = self._plot_x[idx]
        axes = self.figure.get_axes()
        if axes:
            if self.vertical_line is None:
                self.vertical_line = axes[0].axvline(x=plot_x, color='k', linestyle='--', lw=1)
            else:
                self.vertical_line.set_xdata([plot_x, plot_x])
                self.vertical_line.set_visible(True)
            if len(axes) > 1:
                if self.vertical_line2 is None:
                    self.vertical_line2 = axes[1].axvline(x=plot_x, color='k', linestyle='--', lw=1)
                else:
                    self.vertical_line2.set_xdata([plot_x, plot_x])
                    self.vertical_line2.set_visible(True)
        self.canvas.draw_idle()
        self.update_map_marker(self.profile.kp_values[idx])

    def on_right_click(self, event):
        # Remember where the context menu was opened; the menu's
        # "Centre map on this KP" action pans there.
        if event.button != 3:
            return
        self._context_station = self._station_for_plot_x(event.xdata) if event.inaxes else None

    def _centre_map_on_context_station(self):
        idx = self._context_station
        p = self.profile
        if idx is None or idx >= len(p.kp_values) or p.route is None:
            return
        point = p.route.point_at(p.kp_values[idx] * 1000.0)
        if point is not None:
            canvas = self.iface.mapCanvas()
            canvas.setCenter(self._to_canvas_crs(point))
            canvas.refresh()

    def _to_canvas_crs(self, point):
        """Line-CRS point -> map canvas CRS (cached transform)."""
        canvas = self.iface.mapCanvas()
        dest = canvas.mapSettings().destinationCrs()
        src = self.profile.route.crs if self.profile.route is not None else None
        if src is None or not src.isValid() or not dest.isValid() or src == dest:
            return point
        xform = self._marker_xform
        if xform is None or xform.sourceCrs() != src or xform.destinationCrs() != dest:
            xform = self._marker_xform = QgsCoordinateTransform(src, dest, QgsProject.instance())
        try:
            return xform.transform(point)
        except QgsCsException:  # per mouse move: no logging
            return point

    def update_map_marker(self, kp):
        route = self.profile.route
        point = route.point_at(kp * 1000.0) if route is not None else None
        if point is None:
            if self.marker and self.marker.isVisible():
                self.marker.hide()
            return
        if not self.marker:
            self.marker = QgsVertexMarker(self.iface.mapCanvas())
            self.marker.setColor(Qt.GlobalColor.blue)
            self.marker.setIconSize(10)
            self.marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self.marker.setPenWidth(2)
        # Canvas items repaint themselves; a full canvas refresh here
        # re-rendered every layer on each mouse move.
        self.marker.setCenter(self._to_canvas_crs(point))
        if not self.marker.isVisible():
            self.marker.show()

    def _remove_canvas_item(self, item):
        """Remove a map canvas item (vertex marker / rubber band) from the scene."""
        if item is None:
            return
        try:
            if _sip_isdeleted(item):
                return
            item.hide()
            scene = self.iface.mapCanvas().scene()
            if item.scene() is scene:
                scene.removeItem(item)
        except Exception:
            log_exception("Depth profile: map canvas item removal", level=logging.DEBUG)

    # ---------------------- Cleanup ----------------------
    def clear_plot(self):
        """Clear the plot and profile, cancelling a generation in progress,
        so a running task never applies its result after a clear, close or
        unload (shutdown)."""
        self._cancel_generation()
        self.disconnect_canvas_events()
        self._remove_canvas_item(self.marker)
        self.marker = None
        if self.figure:
            try: self.figure.clear()
            except Exception: log_exception("Depth profile: figure clear", level=logging.DEBUG)
        self.vertical_line = None
        self.vertical_line2 = None
        self._route_kp = None
        self.profile = ProfileResult()
        try:
            self.canvas.draw()
        except Exception:
            log_exception("Depth profile: canvas redraw", level=logging.DEBUG)

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
                # Fully dispose of the previous rubber band to avoid lingering graphics
                self._remove_canvas_item(self.temp_line_rubber)
                self.temp_line_rubber = QgsRubberBand(self.iface.mapCanvas(), GEOMETRY_LINE)
                self.temp_line_rubber.setColor(Qt.GlobalColor.yellow)
                self.temp_line_rubber.setWidth(2)
                for pt in points:
                    self.temp_line_rubber.addPoint(pt)
                self.temp_line_rubber.show()
            except Exception:
                log_exception("Depth profile: drawn line not shown on the map")
            self.iface.messageBar().pushMessage("Depth Profile", f"Temporary line captured ({len(points)} pts)", level=MESSAGE_INFO, duration=3)
        def canceled():
            self.iface.messageBar().pushMessage("Depth Profile", "Drawing canceled", level=MESSAGE_WARNING, duration=2)
        # Instantiate map tool (now expects iface for messaging) and activate
        self.temp_line_tool = TempLineMapTool(canvas, self.iface, finished, canceled)
        canvas.setMapTool(self.temp_line_tool)

    def clear_drawn_line(self):
        self.temp_drawn_points = []
        self.use_drawn_chk.setChecked(False)
        self.update_enable_states()
        # remove persistent rubber band
        self._remove_canvas_item(self.temp_line_rubber)
        self.temp_line_rubber = None
        self.iface.messageBar().pushMessage("Depth Profile", "Temporary line cleared", level=MESSAGE_INFO, duration=2)

    def keyPressEvent(self, event):
        # Escape cancels / leaves measuring; Delete removes a measurement.
        if self.measure.handle_key(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def shutdown(self):
        """Release what the dock holds on the map canvas and the project.

        Cancels a running generation (its result is ignored), removes the
        map marker, drawn-line rubber band and drawing tool, detaches
        measurements and disconnects project signals. Called by closeEvent
        and by the plugin's unload; idempotent until the dock is shown again.
        """
        if self._shut_down:
            return
        self._shut_down = True
        self._closing = True
        self._pending_layer_refresh = False
        self.clear_plot()
        # ensure temp line rubber removed
        self._remove_canvas_item(self.temp_line_rubber)
        self.temp_line_rubber = None
        if self.temp_line_tool is not None:
            try:
                canvas = self.iface.mapCanvas()
                if canvas.mapTool() is self.temp_line_tool:
                    canvas.unsetMapTool(self.temp_line_tool)
            except RuntimeError:  # canvas or tool already deleted
                pass
            self.temp_line_tool = None
        self.measure.attach(None)
        self._disconnect_project_signals()

    def closeEvent(self, event):  # noqa
        self.shutdown()
        super().closeEvent(event)

    def cleanup_matplotlib_resources_on_close(self):
        # Provided for parity with other dock widgets; already handled in clear_plot
        pass

    # ---------------------- DXF Export ----------------------
    def export_dxf(self):
        """Export the current depth (and optionally slope) profile to a simple DXF polyline.

        Strategy:
        - Only proceed if a profile has been generated (kp_values & depth_values populated).
        - Export X as KP (distance along route) and Y as depth.
        - Depth values exported with sea level at Y=0 and depths negative (e.g. 10 m depth -> y = -10 m).
        - Handle None gaps by splitting into multiple polylines.
        - Write a minimal DXF (POLYLINE + VERTEX records) in layer 0.

        Notes:
        - DXF coordinates are unitless unless a header is provided. This exporter writes $INSUNITS
          so CAD programs can interpret the intended units.
        - KP is derived from QGIS distance measurement settings (Project ellipsoid). If your
          "KP" in other systems is based on grid/projection distance, ensure your project
          measurement settings match.
        """
        p = self.profile
        if not p.kp_values or not p.depth_values:
            self.iface.messageBar().pushMessage("Depth Profile", "No profile data to export. Generate first.", level=MESSAGE_WARNING, duration=4)
            return
        line_length = p.route.line_length

        # Choose DXF units (persisted). Default to metres to match common CAD workflows.
        units_default = str(self.settings.value("DepthProfile/dxf_units", "Meters"))
        units_items = ["Meters", "Millimeters"]
        try:
            default_index = units_items.index(units_default) if units_default in units_items else 0
        except Exception:
            default_index = 0
        units_choice, ok = QInputDialog.getItem(
            self,
            "Export DXF",
            "DXF units (X=KP, Y=Depth):",
            units_items,
            default_index,
            False,
        )
        if not ok:
            return
        units_choice = str(units_choice)
        self.settings.setValue("DepthProfile/dxf_units", units_choice)

        # Optional KP markers
        add_markers_default = bool(self.settings.value("DepthProfile/dxf_kp_markers", True, type=bool))
        add_markers, ok = QInputDialog.getItem(
            self,
            "Export DXF",
            "Add KP markers + labels?",
            ["Yes", "No"],
            0 if add_markers_default else 1,
            False,
        )
        if not ok:
            return
        add_markers = (str(add_markers) == "Yes")
        self.settings.setValue("DepthProfile/dxf_kp_markers", bool(add_markers))

        marker_interval_km = None
        marker_height_m = None
        label_offset_m = None
        text_height_m = None
        if add_markers:
            marker_interval_km, ok = QInputDialog.getDouble(
                self,
                "Export DXF",
                "KP marker interval (km):",
                float(self.settings.value("DepthProfile/dxf_kp_marker_interval_km", 1.0)),
                0.001,
                100000.0,
                3,
            )
            if not ok:
                return
            marker_height_m, ok = QInputDialog.getDouble(
                self,
                "Export DXF",
                "KP marker height above Y=0 (m):",
                float(self.settings.value("DepthProfile/dxf_kp_marker_height_m", 100.0)),
                1.0,
                100000.0,
                1,
            )
            if not ok:
                return
            label_offset_m, ok = QInputDialog.getDouble(
                self,
                "Export DXF",
                "Label offset above marker (m):",
                float(self.settings.value("DepthProfile/dxf_kp_label_offset_m", 10.0)),
                0.0,
                100000.0,
                1,
            )
            if not ok:
                return
            text_height_m, ok = QInputDialog.getDouble(
                self,
                "Export DXF",
                "Label text height (m):",
                float(self.settings.value("DepthProfile/dxf_kp_text_height_m", 10.0)),
                0.1,
                100000.0,
                1,
            )
            if not ok:
                return
            self.settings.setValue("DepthProfile/dxf_kp_marker_interval_km", float(marker_interval_km))
            self.settings.setValue("DepthProfile/dxf_kp_marker_height_m", float(marker_height_m))
            self.settings.setValue("DepthProfile/dxf_kp_label_offset_m", float(label_offset_m))
            self.settings.setValue("DepthProfile/dxf_kp_text_height_m", float(text_height_m))

        # Optional events layer export (points with KP + label)
        add_events_default = bool(self.settings.value("DepthProfile/dxf_events", False, type=bool))
        add_events, ok = QInputDialog.getItem(
            self,
            "Export DXF",
            "Add events from a point layer?",
            ["Yes", "No"],
            0 if add_events_default else 1,
            False,
        )
        if not ok:
            return
        add_events = (str(add_events) == "Yes")
        self.settings.setValue("DepthProfile/dxf_events", bool(add_events))

        event_layer = None
        event_kp_field = None
        event_label_field = None
        event_kp_units = None
        event_marker_height_m = None
        event_label_offset_m = None
        event_text_height_m = None

        if add_events:
            # Choose a point layer
            point_layers = []
            try:
                for lyr in QgsProject.instance().mapLayers().values():
                    if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == GEOMETRY_POINT:
                        point_layers.append(lyr)
            except Exception:
                point_layers = []

            if not point_layers:
                self.iface.messageBar().pushMessage("Depth Profile", "No point layers found in project for events export.", level=MESSAGE_WARNING, duration=5)
                add_events = False
            else:
                # Make names unique-ish for selection
                items = [f"{lyr.name()} [{lyr.id()[-8:]}]" for lyr in point_layers]
                last_layer_id = str(self.settings.value("DepthProfile/dxf_events_layer_id", ""))
                default_index = 0
                if last_layer_id:
                    for i, lyr in enumerate(point_layers):
                        if lyr.id() == last_layer_id:
                            default_index = i
                            break
                chosen, ok = QInputDialog.getItem(
                    self,
                    "Export DXF",
                    "Events point layer:",
                    items,
                    default_index,
                    False,
                )
                if not ok:
                    return
                chosen = str(chosen)
                chosen_idx = 0
                try:
                    chosen_idx = items.index(chosen)
                except Exception:
                    chosen_idx = 0
                event_layer = point_layers[chosen_idx]
                self.settings.setValue("DepthProfile/dxf_events_layer_id", str(event_layer.id()))

                # Choose KP and label fields
                field_names = [f.name() for f in event_layer.fields()]
                if not field_names:
                    self.iface.messageBar().pushMessage("Depth Profile", "Selected events layer has no fields.", level=MESSAGE_WARNING, duration=5)
                    add_events = False
                else:
                    kp_default = str(self.settings.value("DepthProfile/dxf_events_kp_field", field_names[0] if field_names else ""))
                    kp_idx = field_names.index(kp_default) if kp_default in field_names else 0
                    event_kp_field, ok = QInputDialog.getItem(
                        self,
                        "Export DXF",
                        "Events KP field (distance along route):",
                        field_names,
                        kp_idx,
                        False,
                    )
                    if not ok:
                        return
                    event_kp_field = str(event_kp_field)
                    self.settings.setValue("DepthProfile/dxf_events_kp_field", event_kp_field)

                    label_options = ["(no label)", "<KP>"] + field_names
                    label_default = str(self.settings.value("DepthProfile/dxf_events_label_field", "<KP>"))
                    label_idx = label_options.index(label_default) if label_default in label_options else 1
                    event_label_field, ok = QInputDialog.getItem(
                        self,
                        "Export DXF",
                        "Events label field:",
                        label_options,
                        label_idx,
                        False,
                    )
                    if not ok:
                        return
                    event_label_field = str(event_label_field)
                    self.settings.setValue("DepthProfile/dxf_events_label_field", event_label_field)

                    # KP units
                    kp_units_options = ["Auto", "Kilometers", "Meters"]
                    units_default = str(self.settings.value("DepthProfile/dxf_events_kp_units", "Auto"))
                    units_idx = kp_units_options.index(units_default) if units_default in kp_units_options else 0
                    event_kp_units, ok = QInputDialog.getItem(
                        self,
                        "Export DXF",
                        "Events KP units:",
                        kp_units_options,
                        units_idx,
                        False,
                    )
                    if not ok:
                        return
                    event_kp_units = str(event_kp_units)
                    self.settings.setValue("DepthProfile/dxf_events_kp_units", event_kp_units)

                    # Styling: if KP markers were enabled, reuse their styling by default.
                    default_height = float(marker_height_m) if (add_markers and marker_height_m is not None) else float(self.settings.value("DepthProfile/dxf_events_marker_height_m", 50.0))
                    default_offset = float(label_offset_m) if (add_markers and label_offset_m is not None) else float(self.settings.value("DepthProfile/dxf_events_label_offset_m", 10.0))
                    default_text_h = float(text_height_m) if (add_markers and text_height_m is not None) else float(self.settings.value("DepthProfile/dxf_events_text_height_m", 5.0))

                    event_marker_height_m, ok = QInputDialog.getDouble(
                        self,
                        "Export DXF",
                        "Event marker height above Y=0 (m):",
                        default_height,
                        1.0,
                        100000.0,
                        1,
                    )
                    if not ok:
                        return
                    event_label_offset_m, ok = QInputDialog.getDouble(
                        self,
                        "Export DXF",
                        "Event label offset above marker (m):",
                        default_offset,
                        0.0,
                        100000.0,
                        1,
                    )
                    if not ok:
                        return
                    event_text_height_m, ok = QInputDialog.getDouble(
                        self,
                        "Export DXF",
                        "Event label text height (m):",
                        default_text_h,
                        0.1,
                        100000.0,
                        1,
                    )
                    if not ok:
                        return
                    self.settings.setValue("DepthProfile/dxf_events_marker_height_m", float(event_marker_height_m))
                    self.settings.setValue("DepthProfile/dxf_events_label_offset_m", float(event_label_offset_m))
                    self.settings.setValue("DepthProfile/dxf_events_text_height_m", float(event_text_height_m))

        path, _ = QFileDialog.getSaveFileName(self, "Save DXF", "depth_profile.dxf", "DXF Files (*.dxf)")
        if not path:
            return

        # Unit scaling and DXF header units.
        # AutoCAD INSUNITS: 4=millimeters, 6=meters
        if units_choice == "Millimeters":
            x_scale = 1_000_000.0  # KP (km) -> mm
            y_scale = 1_000.0      # depth (m) -> mm
            insunits = 4
            units_hint = "mm"
        else:
            x_scale = 1_000.0      # KP (km) -> m
            y_scale = 1.0          # depth (m) -> m
            insunits = 6
            units_hint = "m"

        # Build segments (skip None values)
        segments = []
        current_x = []
        current_y = []
        for kp, depth in zip(p.kp_values, p.depth_values):
            if depth is None:
                if current_x:
                    segments.append((current_x, current_y))
                    current_x, current_y = [], []
                continue
            # X: KP (km) -> selected units
            current_x.append(float(kp) * float(x_scale))
            # Y: depth positive-down in data -> negative in CAD so seabed below 0
            current_y.append(-float(depth) * float(y_scale))
        if current_x:
            segments.append((current_x, current_y))
        if not segments:
            self.iface.messageBar().pushMessage("Depth Profile", "All depth values are null; nothing to export.", level=MESSAGE_WARNING, duration=4)
            return

        def _dxf_line_entity(x1, y1, x2, y2, layer="0"):
            return [
                '0','LINE',
                '8',str(layer),
                '10',f'{float(x1)}','20',f'{float(y1)}','30','0.0',
                '11',f'{float(x2)}','21',f'{float(y2)}','31','0.0',
            ]

        def _dxf_text_entity(x, y, text, height, layer="0"):
            safe_text = str(text).replace('\n', ' ').replace('\r', ' ')
            return [
                '0','TEXT',
                '8',str(layer),
                '10',f'{float(x)}','20',f'{float(y)}','30','0.0',
                '40',f'{float(height)}',
                '1',safe_text,
            ]

        def _safe_float(v):
            if v is None:
                return None
            try:
                if isinstance(v, str):
                    s = v.strip()
                    if not s:
                        return None
                    # Common cleanup (e.g. "KP 12.345")
                    s = s.replace('KP', '').replace('kp', '').replace('Km', '').replace('KM', '').replace('km', '')
                    s = s.replace(':', ' ').replace(',', ' ')
                    # Keep the first token that looks numeric
                    parts = [p for p in s.split() if p]
                    if parts:
                        s = parts[0]
                    return float(s)
                return float(v)
            except (TypeError, ValueError):
                return None

        # Compose DXF content
        dxf_parts = [
            '0','SECTION','2','HEADER',
            '9','$MEASUREMENT','70','1',
            '9','$INSUNITS','70',str(int(insunits)),
            '0','ENDSEC',
            '0','SECTION','2','ENTITIES'
        ]

        # KP markers + labels (optional)
        if add_markers and marker_interval_km and marker_interval_km > 0:
            try:
                max_kp_km = max(float(k) for k in p.kp_values if k is not None)
            except ValueError:
                max_kp_km = None
            if max_kp_km is not None and max_kp_km >= 0:
                interval_km = float(marker_interval_km)
                h_u = float(marker_height_m) * float(y_scale)
                label_off_u = float(label_offset_m) * float(y_scale)
                text_h_u = float(text_height_m) * float(y_scale)
                kp = 0.0
                # Add a small epsilon to include the end marker if it falls exactly on interval.
                while kp <= (max_kp_km + 1e-9):
                    x = kp * float(x_scale)
                    dxf_parts.extend(_dxf_line_entity(x, 0.0, x, h_u, layer="KP_MARK"))
                    dxf_parts.extend(_dxf_text_entity(x, h_u + label_off_u, f"KP {kp:.3f}", height=text_h_u, layer="KP_TEXT"))
                    kp += interval_km

        # Events markers + labels (optional)
        if add_events and event_layer is not None and event_kp_field and event_kp_units:
            # Collect numeric KP values for auto-units detection
            raw_vals = []
            try:
                for feat in event_layer.getFeatures():
                    raw = feat[event_kp_field]
                    fv = _safe_float(raw)
                    if fv is not None and not math.isnan(fv):
                        raw_vals.append(float(fv))
            except Exception:
                log_exception("Depth profile DXF: could not read event KPs; KP units default to kilometres")
                raw_vals = []

            kp_mode = str(event_kp_units)
            if kp_mode == "Auto":
                # Heuristic: if KP values look like meters (similar magnitude to route length in meters), treat as meters.
                # Otherwise treat as kilometers.
                if raw_vals and line_length and line_length > 0:
                    try:
                        raw_vals_sorted = sorted(raw_vals)
                        median = raw_vals_sorted[len(raw_vals_sorted) // 2]
                    except Exception:
                        median = None
                    if median is not None:
                        # If median is large (>1000) and not tiny relative to route length, assume meters.
                        if median > max(1000.0, float(line_length) / 50.0):
                            kp_mode = "Meters"
                        else:
                            kp_mode = "Kilometers"
                else:
                    kp_mode = "Kilometers"

            def _kp_to_km(v):
                if v is None:
                    return None
                if kp_mode == "Meters":
                    return float(v) / 1000.0
                return float(v)

            h_u = float(event_marker_height_m) * float(y_scale) if event_marker_height_m is not None else (50.0 * float(y_scale))
            label_off_u = float(event_label_offset_m) * float(y_scale) if event_label_offset_m is not None else (10.0 * float(y_scale))
            text_h_u = float(event_text_height_m) * float(y_scale) if event_text_height_m is not None else (5.0 * float(y_scale))

            max_profile_kp_km = None
            try:
                max_profile_kp_km = max(float(k) for k in p.kp_values if k is not None)
            except ValueError:
                max_profile_kp_km = None

            try:
                for feat in event_layer.getFeatures():
                    kp_raw = _safe_float(feat[event_kp_field])
                    if kp_raw is None or math.isnan(kp_raw):
                        continue
                    kp_km = _kp_to_km(kp_raw)
                    if kp_km is None:
                        continue
                    # Skip events outside profile range
                    if max_profile_kp_km is not None and (kp_km < -1e-9 or kp_km > (max_profile_kp_km + 1e-9)):
                        continue
                    x = float(kp_km) * float(x_scale)
                    dxf_parts.extend(_dxf_line_entity(x, 0.0, x, h_u, layer="EVENT_MARK"))

                    label = ""
                    if event_label_field == "(no label)":
                        label = ""
                    elif event_label_field == "<KP>":
                        label = f"KP {float(kp_km):.3f}"
                    else:
                        try:
                            label = str(feat[event_label_field])
                        except KeyError:
                            label = ""

                    if label:
                        dxf_parts.extend(_dxf_text_entity(x, h_u + label_off_u, label, height=text_h_u, layer="EVENT_TEXT"))
            except Exception:
                # The profile is still written; say that events are missing.
                log_exception("Depth profile DXF: event export stopped early")
                self.iface.messageBar().pushMessage(
                    "Depth Profile", "Some events could not be exported to the DXF (see the Subsea Cable Tools log).",
                    level=MESSAGE_WARNING, duration=8)

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
            self.iface.messageBar().pushMessage(
                "Depth Profile",
                f"DXF exported ({units_hint}): {path} | X-axis: 1 km = {x_scale:.0f} {units_hint}",
                level=MESSAGE_INFO,
                duration=7,
            )
        except Exception as e:
            self.iface.messageBar().pushMessage("Depth Profile", f"Failed to write DXF: {e}", level=MESSAGE_CRITICAL, duration=6)

    def export_csv(self):
        """Export the current segment-based KP, Depth, and Slope data to a CSV file."""
        p = self.profile
        segments = p.segments()
        if not segments:
            self.iface.messageBar().pushMessage("Depth Profile", "No profile data to export. Generate first.", level=MESSAGE_WARNING, duration=4)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save CSV", "depth_profile.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            import csv
            to_wgs = None
            line_crs = p.route.crs
            if line_crs and line_crs.isValid():
                to_wgs = QgsCoordinateTransform(line_crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                                QgsProject.instance())
            station_by_kp = {round(kp, 9): i for i, kp in enumerate(p.kp_values)}
            widths = p.slope_baseline_m or []
            peaks = p.side_local_max_deg or []
            sources = p.depth_source_ids or []

            def fmt(value, digits=3):
                return "" if value is None else f"{value:.{digits}f}"

            with open(path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["KP_from (km)", "KP_to (km)", "Lat_from", "Lon_from", "Lat_to", "Lon_to",
                                 "Depth_from (m)", "Depth_to (m)", "Slope (deg)", "Slope (%)",
                                 "SideSlope (deg)", "SideSlope (%)", "PortDepth (m)", "StbdDepth (m)",
                                 "CrossSpan (m)", "Seabed_Length (m)", "Euclidean_Length (m)",
                                 "SlopeBaseline (m)", "MaxLocalCrossSlope (deg)", "DepthSource"])
                lonlat_to = None
                for n, seg in enumerate(segments):
                    kp_from, kp_to = seg.kp_from, seg.kp_to
                    # Consecutive segments share a station: reuse its position.
                    if lonlat_to is not None and n > 0 and kp_from == segments[n - 1].kp_to:
                        lat_from, lon_from = lonlat_to
                    else:
                        lat_from, lon_from = self._station_lonlat(kp_from, to_wgs)
                    lonlat_to = self._station_lonlat(kp_to, to_wgs)
                    lat_to, lon_to = lonlat_to
                    index = station_by_kp.get(round(kp_to, 9))
                    seabed_len = seg.seabed_length
                    writer.writerow([
                        fmt(kp_from), fmt(kp_to), fmt(lat_from, 6), fmt(lon_from, 6), fmt(lat_to, 6), fmt(lon_to, 6),
                        fmt(seg.depth_from), fmt(seg.depth_to),
                        fmt(seg.slope_deg), fmt(seg.slope_pct),
                        fmt(seg.side_slope_deg), fmt(seg.side_slope_pct),
                        fmt(seg.port_depth), fmt(seg.starboard_depth),
                        fmt(seg.cross_span_m), fmt(seabed_len),
                        # Retained column: identical to Seabed_Length (hypot of
                        # chainage step and depth change).
                        fmt(seabed_len),
                        widths[index] if index is not None and index < len(widths) else None,
                        peaks[index] if index is not None and index < len(peaks) else None,
                        sources[index] if index is not None and index < len(sources) else 'contours'])
            self.iface.messageBar().pushMessage("Depth Profile", f"CSV exported: {path}", level=MESSAGE_INFO, duration=5)
        except Exception as e:
            self.iface.messageBar().pushMessage("Depth Profile", f"Failed to write CSV: {e}", level=MESSAGE_CRITICAL, duration=6)

    def export_png(self):
        """Save the plot area (all axes and measurements) as a PNG image."""
        if not self.figure.get_axes() or not self.profile.kp_values:
            self.iface.messageBar().pushMessage("Depth Profile", "No profile to save. Generate first.", level=MESSAGE_WARNING, duration=4)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save plot image", "depth_profile.png", "PNG image (*.png)")
        if not path:
            return
        if not path.lower().endswith('.png'):
            path += '.png'
        if self.canvas.grab().save(path, 'PNG'):
            self.iface.messageBar().pushMessage("Depth Profile", f"Plot saved: {path}", level=MESSAGE_INFO, duration=5)
        else:
            self.iface.messageBar().pushMessage("Depth Profile", f"Could not write {path}", level=MESSAGE_CRITICAL, duration=6)

    def export_measurements_csv(self):
        if not self.measure.measurements:
            self.iface.messageBar().pushMessage("Depth Profile", "No measurements to export.", level=MESSAGE_WARNING, duration=4)
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save measurements", "profile_measurements.csv", "CSV Files (*.csv)")
        if not path:
            return
        try:
            # Endpoint X is the KP shown on the plot (re-numbered when Reverse KP is on).
            write_measurements_csv(path, self.measure.measurements, x_label='kp_km', x_factor=0.001)
            self.iface.messageBar().pushMessage("Depth Profile", f"Measurements exported: {path}", level=MESSAGE_INFO, duration=5)
        except Exception as e:
            self.iface.messageBar().pushMessage("Depth Profile", f"Failed to write CSV: {e}", level=MESSAGE_CRITICAL, duration=6)
