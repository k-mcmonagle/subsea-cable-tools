# -*- coding: utf-8 -*-
"""
Catenary Calculator Dialog for Subsea Cable Tools QGIS Plugin

Upgraded version:
- Models a full cable system from TDP (touchdown at seabed) up to the chute top reference:
  * Submerged + in-air cable (different weights in each medium)
  * Optional components (bodies like repeaters / joints) as:
      - short heavy sections (delta distributed weight over a length)
      - or point loads (vertical lump load, causes a kink)
    * Optional quarter-circle "chute" geometry (rendered) with radius and top height above waterline
- Plotting uses the bundled pyqtgraph package so QGIS does not need to ship matplotlib.

Coordinate convention (internal):
- Sea level: y = 0
- Above sea: y > 0
- Below sea: y < 0
- Seabed at y = -water_depth
- TDP starts at (x=0, y=-water_depth)
- Chute top reference is at (x=layback, y=+chute_exit_height)
- With a chute radius, the free span ends at the tangent/contact point on the chute arc.

Plot convention:
- Depth = -y (so seabed is +depth, above sea is negative depth)
- Horizontal distance can be rendered/exported from either the TDP or the chute top reference.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QTextEdit, QWidget, QFormLayout, QSizePolicy, QFileDialog, QDoubleSpinBox,
    QScrollArea, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QMessageBox
    , QCheckBox, QColorDialog
)
from qgis.PyQt.QtCore import Qt, QSettings, QTimer
from qgis.PyQt.QtGui import QColor
from ..qgis_compat import (
    EDIT_TRIGGER_DOUBLE_CLICKED,
    EDIT_TRIGGER_EDIT_KEY_PRESSED,
    EDIT_TRIGGER_SELECTED_CLICKED,
    HEADER_RESIZE_MODE_INTERACTIVE,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
    SIZE_POLICY_EXPANDING,
    qt_exec,
)
from .catenary_plot_widget import (
    CatenaryLineCollection as LineCollection,
    CatenaryPlotCanvas as FigureCanvas,
    CatenaryPlotFigure as Figure,
    get_tab10_color,
)
from .catenary_solver import (
    AssemblyItem,
    CatenarySystemCalculator,
    Component,
    _parse_components,
)

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None
import math
import json
from html import escape


# ---------------------------
# Dialog / UI
# ---------------------------

class CatenaryCalculatorV2Dialog(QDialog):
    ASM_COL_TYPE = 0
    ASM_COL_NAME = 1
    ASM_COL_LENGTH = 2
    ASM_COL_Q_WATER = 3
    ASM_COL_Q_AIR = 4
    ASM_COL_BODY_LOAD = 5
    ASM_COL_COLOR = 6

    _DEFAULT_SEGMENT_COLORS = [
        "#1f77b4",  # tab:blue
        "#ff7f0e",  # tab:orange
        "#2ca02c",  # tab:green
        "#d62728",  # tab:red
        "#9467bd",  # tab:purple
        "#8c564b",  # tab:brown
        "#e377c2",  # tab:pink
        "#7f7f7f",  # tab:gray
        "#bcbd22",  # tab:olive
        "#17becf",  # tab:cyan
    ]
    _DEFAULT_BODY_COLORS = [
        "#000000",
        "#d62728",
        "#9467bd",
        "#2ca02c",
        "#8c564b",
        "#1f77b4",
    ]
    _DEFAULT_Q_WATER_NPM = 22.0
    _DEFAULT_Q_AIR_NPM = 28.0

    def __init__(self, parent=None):
        super().__init__(parent)
        if np is None:
            QMessageBox.critical(
                self,
                'Missing dependency',
                'NumPy is required for the catenary calculator but could not be imported. '
                'Please install/enable NumPy for your QGIS Python environment.'
            )
            self.setEnabled(False)
            return
        self.setWindowTitle("Subsea Cable Catenary Calculator V2")
        self._fit_initial_size_to_screen()

        self.settings = QSettings("subsea_cable_tools", "CatenaryCalculatorUpgraded")

        self._prev_angle_ref = 0
        self._last_calc: Optional[CatenarySystemCalculator] = None
        self._initializing = True
        self._fallback_q_water_npm = self._DEFAULT_Q_WATER_NPM
        self._fallback_q_air_npm = self._DEFAULT_Q_AIR_NPM
        self._syncing_assembly_json = False
        self._last_plot_x_reference = None
        self._crosshair_cid = None
        self._plot_click_cid = None
        self._crosshair_vline = None
        self._crosshair_hline = None
        self._hover_cache = {}
        self._last_hover_signature = None
        self._collapsible_sections = {}

        # Debounce heavy recalculations while editing.
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self.update_plot)

        self._assembly_json_timer = QTimer(self)
        self._assembly_json_timer.setSingleShot(True)
        self._assembly_json_timer.timeout.connect(self._apply_assembly_json_text)

        self.init_ui()
        self.restore_user_settings()
        self.update_input_fields()
        self._initializing = False
        self.results.setHtml("Ready. Preparing plot...")
        QTimer.singleShot(0, self.update_plot)

    def schedule_update_plot(self):
        """Debounced wrapper around `update_plot` for high-frequency UI signals."""
        if getattr(self, "_initializing", False):
            return
        # 150ms feels responsive but avoids dozens of solves while typing.
        self._update_timer.start(150)

    def closeEvent(self, a0):
        self.save_user_settings()
        super().closeEvent(a0)

    def _fit_initial_size_to_screen(self):
        min_w = 760
        min_h = 540
        width = 1180
        height = 760
        try:
            screen = self.screen() or QApplication.primaryScreen()
            available = screen.availableGeometry() if screen is not None else None
            if available is not None:
                width = min(width, max(min_w, int(available.width() * 0.92)))
                height = min(height, max(min_h, int(available.height() * 0.88)))
        except Exception:
            pass
        self.setMinimumSize(min_w, min_h)
        self.resize(width, height)

    def _create_collapsible_section(self, title: str, settings_key: str) -> Tuple[QPushButton, QWidget, QFormLayout]:
        button = QPushButton()
        button.setCheckable(True)
        button.setToolTip(f"Show/hide {title} options.")

        container = QWidget()
        layout = QFormLayout(container)
        layout.setContentsMargins(16, 0, 0, 0)

        self._collapsible_sections[settings_key] = (button, container, title)
        button.toggled.connect(lambda checked, key=settings_key: self._set_collapsible_section_expanded(key, checked))
        self._set_collapsible_section_expanded(settings_key, False)
        return button, container, layout

    def _set_collapsible_section_expanded(self, settings_key: str, expanded: bool):
        section = self._collapsible_sections.get(settings_key)
        if section is None:
            return
        button, container, title = section
        try:
            button.blockSignals(True)
            button.setChecked(bool(expanded))
            button.setText(("[-] " if expanded else "[+] ") + title)
        finally:
            button.blockSignals(False)
        container.setVisible(bool(expanded))

    @staticmethod
    def _settings_bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return bool(default)
        try:
            return str(value).lower() in ("1", "true", "yes")
        except Exception:
            return bool(default)

    # ---- Persistent settings

    def save_user_settings(self):
        self.settings.setValue("water_depth", self.water_depth.value())
        self.settings.setValue("chute_exit_height", self.chute_exit_height.value())
        self.settings.setValue("chute_radius", self.chute_radius.value())
        self.settings.setValue("ds_step", self.ds_step.value())

        self.settings.setValue("weight_water", self._fallback_q_water_npm)
        self.settings.setValue("weight_air", self._fallback_q_air_npm)
        self.settings.setValue("weight_unit", 0)

        self.settings.setValue("input_parameter", self.input_parameter.currentIndex())
        self.settings.setValue("bottom_tension", self.bottom_tension.value())
        self.settings.setValue("top_tension", self.top_tension.value())
        self.settings.setValue("exit_angle", self.exit_angle.value())
        self.settings.setValue("angle_reference", self.angle_reference.currentIndex())
        self.settings.setValue("catenary_length", self.catenary_length.value())
        self.settings.setValue("layback", self.layback.value())

        self.settings.setValue("assembly_input_tab", self.assembly_tabs.currentIndex())
        self.settings.setValue("assembly_table_json", self._assembly_table_to_json())
        self.settings.setValue("show_full_assembly_seabed", bool(self.show_full_assembly_seabed.isChecked()))
        self.settings.setValue("show_legend", bool(self.show_legend.isChecked()))
        self.settings.setValue("show_plot_labels", bool(self.show_plot_labels.isChecked()))
        self.settings.setValue("show_crosshair_values", bool(self.show_crosshair_values.isChecked()))
        self.settings.setValue("show_kp_axis", bool(self.show_kp_axis.isChecked()))
        self.settings.setValue("x_axis_reference", self.x_axis_reference.currentIndex())
        self.settings.setValue("cable_count_top", self.cable_count_top.value())
        self.settings.setValue("cable_count_direction", self.cable_count_direction.currentIndex())
        self.settings.setValue("kp_top", self.kp_top.value())
        self.settings.setValue("kp_direction", self.kp_direction.currentIndex())
        for settings_key, section in self._collapsible_sections.items():
            button = section[0]
            self.settings.setValue(settings_key, bool(button.isChecked()))
        self.settings.remove("assembly_table_col_widths")

    def restore_user_settings(self):
        def _get_float(key, default=None):
            val = self.settings.value(key)
            if val is None:
                return default
            try:
                return float(val)
            except Exception:
                return default

        def _get_int(key, default=None):
            val = self.settings.value(key)
            if val is None:
                return default
            try:
                return int(val)
            except Exception:
                return default

        if (v := _get_float("water_depth")) is not None:
            self.water_depth.setValue(v)
        if (v := _get_float("chute_exit_height")) is not None:
            self.chute_exit_height.setValue(v)
        if (v := _get_float("chute_radius")) is not None:
            self.chute_radius.setValue(v)
        if (v := _get_float("ds_step")) is not None:
            self.ds_step.setValue(v)

        weight_unit_idx = _get_int("weight_unit", 0)
        weight_units = ["N/m", "kg/m", "lbf/ft"]
        weight_unit = weight_units[weight_unit_idx] if 0 <= int(weight_unit_idx or 0) < len(weight_units) else "N/m"
        if (v := _get_float("weight_water")) is not None:
            try:
                self._fallback_q_water_npm = CatenarySystemCalculator._unit_to_npm(v, weight_unit)
            except Exception:
                pass
        if (v := _get_float("weight_air")) is not None:
            try:
                self._fallback_q_air_npm = CatenarySystemCalculator._unit_to_npm(v, weight_unit)
            except Exception:
                pass

        if (v := _get_int("input_parameter")) is not None:
            self.input_parameter.setCurrentIndex(v)
        if (v := _get_float("bottom_tension")) is not None:
            self.bottom_tension.setValue(v)
        if (v := _get_float("top_tension")) is not None:
            self.top_tension.setValue(v)
        if (v := _get_float("exit_angle")) is not None:
            self.exit_angle.setValue(v)
        if (v := _get_int("angle_reference")) is not None:
            self.angle_reference.setCurrentIndex(v)
        if (v := _get_float("catenary_length")) is not None:
            self.catenary_length.setValue(v)
        if (v := _get_float("layback")) is not None:
            self.layback.setValue(v)

        if (v := _get_int("x_axis_reference")) is not None:
            try:
                self.x_axis_reference.setCurrentIndex(max(0, min(1, int(v))))
            except Exception:
                pass

        tab_idx = self.settings.value("assembly_input_tab")
        if tab_idx is not None:
            try:
                self.assembly_tabs.setCurrentIndex(int(tab_idx))
            except Exception:
                pass

        table_json = self.settings.value("assembly_table_json")
        if table_json is not None:
            self._assembly_table_from_json(str(table_json))
        self._sync_json_from_table()

        v = self.settings.value("show_full_assembly_seabed")
        if v is not None:
            try:
                self.show_full_assembly_seabed.setChecked(str(v).lower() in ("1", "true", "yes"))
            except Exception:
                pass

        v = self.settings.value("show_legend")
        if v is not None:
            try:
                self.show_legend.setChecked(str(v).lower() in ("1", "true", "yes"))
            except Exception:
                pass

        v = self.settings.value("show_plot_labels")
        if v is not None:
            try:
                self.show_plot_labels.setChecked(str(v).lower() in ("1", "true", "yes"))
            except Exception:
                pass

        v = self.settings.value("show_crosshair_values")
        if v is not None:
            try:
                self.show_crosshair_values.setChecked(self._settings_bool(v))
            except Exception:
                pass

        v = self.settings.value("show_kp_axis")
        if v is not None:
            try:
                self.show_kp_axis.setChecked(self._settings_bool(v))
            except Exception:
                pass

        if (v := _get_float("cable_count_top")) is not None:
            self.cable_count_top.setValue(v)

        if (v := _get_int("cable_count_direction")) is not None:
            try:
                self.cable_count_direction.setCurrentIndex(max(0, min(1, int(v))))
            except Exception:
                pass

        if (v := _get_float("kp_top")) is not None:
            self.kp_top.setValue(v)

        if (v := _get_int("kp_direction")) is not None:
            try:
                self.kp_direction.setCurrentIndex(max(0, min(1, int(v))))
            except Exception:
                pass

        for settings_key in self._collapsible_sections:
            self._set_collapsible_section_expanded(settings_key, self._settings_bool(self.settings.value(settings_key), False))

        self.assembly_table.resizeColumnsToContents()

    # ---- UI init

    def init_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)

        horizontal_orientation = getattr(getattr(Qt, "Orientation", Qt), "Horizontal")
        self.main_splitter = QSplitter(horizontal_orientation)

        # Left: Inputs
        input_widget = QWidget()
        input_layout = QFormLayout(input_widget)
        input_widget.setMinimumWidth(420)

        input_scroll = QScrollArea()
        input_scroll.setWidgetResizable(True)
        input_scroll.setMinimumWidth(420)
        input_scroll.setMaximumWidth(540)
        input_scroll.setWidget(input_widget)

        # Geometry
        self.water_depth = QDoubleSpinBox()
        self.water_depth.setRange(0, 1e6)
        self.water_depth.setDecimals(1)
        self.water_depth.setValue(100.0)

        self.chute_exit_height = QDoubleSpinBox()
        self.chute_exit_height.setRange(0, 1e5)
        self.chute_exit_height.setDecimals(1)
        self.chute_exit_height.setValue(0.0)  # height above sea level
        self.chute_exit_height.setToolTip(
            "Height above waterline of the top reference point of the chute. "
            "With a chute radius, the free-span cable contacts the chute lower on the arc."
        )

        self.chute_radius = QDoubleSpinBox()
        self.chute_radius.setRange(0, 1e4)
        self.chute_radius.setDecimals(1)
        self.chute_radius.setValue(0.0)
        self.chute_radius.setToolTip(
            "Chute radius used for geometry AND for optional chute-contact coupling. "
            "Set to 0 to ignore chute contact (free-span goes to the Top of Chute point)."
        )

        self.ds_step = QDoubleSpinBox()
        self.ds_step.setRange(0.05, 10.0)
        self.ds_step.setDecimals(1)
        self.ds_step.setSingleStep(0.1)
        self.ds_step.setValue(0.5)

        # Solve mode
        self.input_parameter = QComboBox()
        self.input_parameter.addItems([
            "Bottom Tension",      # H is input (kN)
            "Contact Tension",     # contact T is input (kN)
            "Tangent Angle",       # tangent angle at chute contact
            "Catenary Length",     # total cable length (m) from TDP to chute top via chute arc
            "Layback"              # horizontal distance from TDP to chute top
        ])

        # Inputs depending on mode
        self.bottom_tension = QDoubleSpinBox()
        self.bottom_tension.setRange(0, 1e6)
        self.bottom_tension.setDecimals(1)
        self.bottom_tension.setValue(50.0)

        self.top_tension = QDoubleSpinBox()
        self.top_tension.setRange(0, 1e6)
        self.top_tension.setDecimals(1)
        self.top_tension.setValue(80.0)
        self.top_tension.setToolTip(
            "Tension at the free-span/chute contact point. The chute arc is drawn geometrically; chute friction is not modeled."
        )

        self.exit_angle = QDoubleSpinBox()
        self.exit_angle.setRange(0.01, 89.99)
        self.exit_angle.setDecimals(1)
        self.exit_angle.setValue(25.0)

        self.angle_reference = QComboBox()
        self.angle_reference.addItems(["from horizontal", "from vertical"])
        self.angle_reference.setMinimumContentsLength(16)

        self.catenary_length = QDoubleSpinBox()
        self.catenary_length.setRange(0, 1e7)
        self.catenary_length.setDecimals(1)
        self.catenary_length.setValue(230.0)

        self.layback = QDoubleSpinBox()
        self.layback.setRange(0, 1e7)
        self.layback.setDecimals(1)
        self.layback.setValue(150.0)

        self.x_axis_reference = QComboBox()
        self.x_axis_reference.addItems(["Touchdown point", "Top of Chute position"])
        self.x_axis_reference.setToolTip(
            "Controls only the rendered/exported horizontal coordinate origin. Calculations remain referenced to the TDP."
        )

        # Assembly (ordered from chute top down)
        self.assembly_tabs = QTabWidget()

        self.assembly_table = QTableWidget(0, 7)
        self.assembly_table.setHorizontalHeaderLabels([
            "Type",
            "Name",
            "Length (m)",
            "Weight in Water (N/m)",
            "Weight in Air (N/m)",
            "Point Load (kN)",
            "Color",
        ])
        header_tooltips = [
            "Segment or Body.",
            "Display name for this item.",
            "Used for Segment rows only.",
            "Used for Segment rows only (submerged section).",
            "Used for Segment rows only (in-air section).",
            "Used for Body rows only (+downward, -buoyant/upward).",
            "Optional display colour for segment lines and body markers.",
        ]
        for i, tip in enumerate(header_tooltips):
            item = self.assembly_table.horizontalHeaderItem(i)
            if item is not None:
                item.setToolTip(tip)
        self.assembly_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.assembly_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.assembly_table.setEditTriggers(
            EDIT_TRIGGER_DOUBLE_CLICKED | EDIT_TRIGGER_SELECTED_CLICKED | EDIT_TRIGGER_EDIT_KEY_PRESSED
        )
        header = self.assembly_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(HEADER_RESIZE_MODE_INTERACTIVE)
            header.setStretchLastSection(False)
        self.assembly_table.setMinimumHeight(150)

        a_btn_row = QHBoxLayout()
        self.asm_add_seg_btn = QPushButton("Add Segment")
        self.asm_add_body_btn = QPushButton("Add Body")
        self.asm_del_btn = QPushButton("Delete")
        self.asm_up_btn = QPushButton("Move Up")
        self.asm_down_btn = QPushButton("Move Down")
        a_btn_row.addWidget(self.asm_add_seg_btn)
        a_btn_row.addWidget(self.asm_add_body_btn)
        a_btn_row.addWidget(self.asm_del_btn)
        a_btn_row.addWidget(self.asm_up_btn)
        a_btn_row.addWidget(self.asm_down_btn)

        asm_tab = QWidget()
        asm_tab_layout = QVBoxLayout(asm_tab)
        asm_tab_layout.addWidget(QLabel("Assembly is ordered from the Top of Chute down along the cable."))
        asm_tab_layout.addWidget(self.assembly_table)
        asm_tab_layout.addLayout(a_btn_row)
        self.assembly_tabs.addTab(asm_tab, "Assembly")

        self.assembly_json_text = QTextEdit()
        self.assembly_json_text.setAcceptRichText(False)
        self.assembly_json_text.setMinimumHeight(120)
        self.assembly_json_text.setPlaceholderText(
            "[\n"
            "  {\"type\": \"segment\", \"name\": \"Cable\", \"length_m\": 1000, \"q_water_npm\": 22, \"q_air_npm\": 28, \"color\": \"#1f77b4\"}\n"
            "]"
        )
        json_tab = QWidget()
        json_tab_layout = QVBoxLayout(json_tab)
        json_tab_layout.addWidget(self.assembly_json_text)
        self.assembly_tabs.addTab(json_tab, "JSON")

        # Layout entries
        input_layout.addRow(QLabel("<b>Geometry</b>"))
        input_layout.addRow("Water Depth (m):", self.water_depth)
        input_layout.addRow("Chute Top Height above Waterline (m):", self.chute_exit_height)
        input_layout.addRow("Chute Radius (m):", self.chute_radius)

        input_layout.addRow(QLabel("<b>Solve Mode</b>"))
        input_layout.addRow("Select Input Parameter:", self.input_parameter)
        input_layout.addRow("Bottom Tension (kN):", self.bottom_tension)
        input_layout.addRow("Tension at Contact (kN):", self.top_tension)

        ang_layout = QHBoxLayout()
        ang_layout.addWidget(self.exit_angle)
        ang_layout.addWidget(self.angle_reference)
        ang_layout.setStretch(0, 1)
        ang_layout.setStretch(1, 1)
        input_layout.addRow("Tangent Angle at Chute:", ang_layout)

        input_layout.addRow("Total Cable Length (m):", self.catenary_length)
        input_layout.addRow("Layback to Chute Top (m):", self.layback)
        input_layout.addRow("Integration Step (m):", self.ds_step)

        input_layout.addRow(QLabel("<b>Cable Assembly</b>"))
        input_layout.addRow(self.assembly_tabs)

        display_header, display_widget, display_layout = self._create_collapsible_section("Display", "section_display_expanded")
        input_layout.addRow(display_header)
        input_layout.addRow(display_widget)
        display_layout.addRow("X-axis zero:", self.x_axis_reference)

        self.show_full_assembly_seabed = QCheckBox("Show full assembly on seabed")
        self.show_full_assembly_seabed.setToolTip(
            "Extends the plot x-axis and draws any remaining assembly length beyond the suspended span as a straight line on the seabed."
        )
        display_layout.addRow("", self.show_full_assembly_seabed)

        self.show_plot_labels = QCheckBox("Show segment/body labels")
        self.show_plot_labels.setChecked(False)
        self.show_plot_labels.setToolTip("Label assembly segments and bodies on the plot.")
        display_layout.addRow("", self.show_plot_labels)

        self.show_crosshair_values = QCheckBox("Show crosshair values")
        self.show_crosshair_values.setChecked(False)
        self.show_crosshair_values.setToolTip("Show cursor coordinates and nearest cable values while hovering over the plot.")
        display_layout.addRow("", self.show_crosshair_values)

        self.show_kp_axis = QCheckBox("Show KP x-axis")
        self.show_kp_axis.setChecked(False)
        self.show_kp_axis.setToolTip("Show a second x-axis below the plot using the Route KP Reference settings.")
        display_layout.addRow("", self.show_kp_axis)

        count_header, count_widget, count_layout = self._create_collapsible_section("Cable Count", "section_count_expanded")
        input_layout.addRow(count_header)
        input_layout.addRow(count_widget)
        self.cable_count_top = QDoubleSpinBox()
        self.cable_count_top.setRange(-1e9, 1e9)
        self.cable_count_top.setDecimals(1)
        self.cable_count_top.setSingleStep(1.0)
        self.cable_count_top.setSuffix(" m")
        self.cable_count_top.setValue(0.0)
        self.cable_count_top.setToolTip("Cable count at the Top of Chute reference point.")

        self.cable_count_direction = QComboBox()
        self.cable_count_direction.addItems(["Increases outboard from chute", "Increases inboard toward vessel"])
        self.cable_count_direction.setToolTip(
            "Outboard is from the chute toward the deployed cable/TDP; inboard is toward the vessel."
        )
        count_layout.addRow("Count at Top of Chute:", self.cable_count_top)
        count_layout.addRow("Count direction:", self.cable_count_direction)

        kp_header, kp_widget, kp_layout = self._create_collapsible_section("Route KP Reference", "section_kp_expanded")
        input_layout.addRow(kp_header)
        input_layout.addRow(kp_widget)
        self.kp_top = QDoubleSpinBox()
        self.kp_top.setRange(-1e9, 1e9)
        self.kp_top.setDecimals(3)
        self.kp_top.setSingleStep(0.001)
        self.kp_top.setSuffix(" km")
        self.kp_top.setValue(0.0)
        self.kp_top.setToolTip("Route KP at the Top of Chute reference point.")

        self.kp_direction = QComboBox()
        self.kp_direction.addItems(["Increases outboard from chute", "Increases inboard toward vessel"])
        self.kp_direction.setToolTip(
            "KP is calculated from horizontal distance. Outboard is from the chute toward the TDP/seabed."
        )
        kp_layout.addRow("KP at Top of Chute:", self.kp_top)
        kp_layout.addRow("KP direction:", self.kp_direction)

        note = QLabel(
            "<i>"
            "Notes:<br>"
            "• The system is solved as a suspended cable from TDP (seabed touchdown) to the chute contact point, then along the chute arc to the Top of Chute reference point.<br>"
            "• Submerged vs in-air is determined automatically when the curve crosses sea level (y=0).<br>"
            "• Assembly is defined from the Top of Chute down (Segment rows set distributed weight; Body rows add lumped load/buoyancy).<br>"
            "• Point loads create a mathematical kink (angle discontinuity). Prefer short sections if you care about curvature/MBR.<br>"
            "• Chute contact is modeled by enforcing the free-span tangent to match the chute arc tangent; contact length depends on tangent angle."
            "</i>"
        )
        note.setWordWrap(True)
        input_layout.addRow(note)

        # Right: Outputs + plot
        output_widget = QWidget()
        output_layout = QVBoxLayout(output_widget)

        results_header = QHBoxLayout()
        results_header.addWidget(QLabel("<b>Results</b>"))
        results_header.addStretch(1)
        self.solver_diagnostics_btn = QPushButton("Solver diagnostics...")
        self.solver_diagnostics_btn.setEnabled(False)
        self.solver_diagnostics_btn.setToolTip("Open numerical residuals and convergence checks for the current result.")
        results_header.addWidget(self.solver_diagnostics_btn)
        output_layout.addLayout(results_header)
        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setMinimumHeight(130)
        output_layout.addWidget(self.results)

        self.figure = Figure(figsize=(6, 5))
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(SIZE_POLICY_EXPANDING, SIZE_POLICY_EXPANDING)
        output_layout.addWidget(self.canvas, stretch=1)

        self.hover_readout = QLabel("")
        self.hover_readout.setWordWrap(False)
        hover_height = max(28, int(self.hover_readout.fontMetrics().lineSpacing() * 2 + 8))
        self.hover_readout.setMinimumHeight(hover_height)
        self.hover_readout.setMaximumHeight(hover_height)
        try:
            fixed_policy = getattr(QSizePolicy, "Fixed", None)
            if fixed_policy is None:
                fixed_policy = getattr(getattr(QSizePolicy, "Policy", QSizePolicy), "Fixed")
            self.hover_readout.setSizePolicy(SIZE_POLICY_EXPANDING, fixed_policy)
        except Exception:
            pass
        self.hover_readout.setVisible(True)
        output_layout.addWidget(self.hover_readout)

        btns = QHBoxLayout()
        self.export_svg_btn = QPushButton("Export Plot...")
        self.export_dxf_btn = QPushButton("Export DXF")
        btns.addWidget(self.export_svg_btn)
        btns.addWidget(self.export_dxf_btn)
        output_layout.addLayout(btns)

        self.show_legend = QCheckBox("Show legend")
        self.show_legend.setChecked(False)
        self.show_legend.setToolTip("Show/hide the plot legend. Keeping it hidden avoids covering the plot.")
        output_layout.addWidget(self.show_legend)

        self.main_splitter.addWidget(input_scroll)
        self.main_splitter.addWidget(output_widget)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setCollapsible(0, False)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([460, 800])
        main_layout.addWidget(self.main_splitter)

        # signals
        for w in [
            self.water_depth, self.chute_exit_height, self.chute_radius, self.ds_step,
            self.bottom_tension, self.top_tension, self.exit_angle,
            self.catenary_length, self.layback
        ]:
            w.valueChanged.connect(self.schedule_update_plot)

        self.input_parameter.currentIndexChanged.connect(self.update_input_fields)
        self.input_parameter.currentIndexChanged.connect(self.schedule_update_plot)
        self.angle_reference.currentIndexChanged.connect(self.on_angle_reference_changed)
        self.assembly_json_text.textChanged.connect(self._on_assembly_json_text_changed)
        self.assembly_table.cellChanged.connect(self._on_assembly_table_cell_changed)
        self.assembly_table.cellDoubleClicked.connect(self._on_assembly_table_cell_double_clicked)
        self.asm_add_seg_btn.clicked.connect(self._on_asm_add_segment)
        self.asm_add_body_btn.clicked.connect(self._on_asm_add_body)
        self.asm_del_btn.clicked.connect(self._on_asm_delete)
        self.asm_up_btn.clicked.connect(self._on_asm_move_up)
        self.asm_down_btn.clicked.connect(self._on_asm_move_down)

        self.x_axis_reference.currentIndexChanged.connect(self.schedule_update_plot)
        self.show_full_assembly_seabed.toggled.connect(self.schedule_update_plot)
        self.show_plot_labels.toggled.connect(self.schedule_update_plot)
        self.show_crosshair_values.toggled.connect(self._on_crosshair_toggled)
        self.show_kp_axis.toggled.connect(self.schedule_update_plot)
        self.cable_count_top.valueChanged.connect(self.schedule_update_plot)
        self.cable_count_direction.currentIndexChanged.connect(self.schedule_update_plot)
        self.kp_top.valueChanged.connect(self.schedule_update_plot)
        self.kp_direction.currentIndexChanged.connect(self.schedule_update_plot)

        self.show_legend.toggled.connect(self.schedule_update_plot)

        self.export_svg_btn.clicked.connect(self.export_svg)
        self.export_dxf_btn.clicked.connect(self.export_dxf)
        self.solver_diagnostics_btn.clicked.connect(self.show_solver_diagnostics)
        self._plot_click_cid = self.canvas.mpl_connect("button_press_event", self._on_plot_click)

    def showEvent(self, a0):
        self._prev_angle_ref = self.angle_reference.currentIndex()
        super().showEvent(a0)

    # ---- Angle reference sync

    def on_angle_reference_changed(self):
        self._sync_exit_angle_with_reference()
        self.update_plot()

    def _sync_exit_angle_with_reference(self):
        curr_ref = self.angle_reference.currentIndex()
        prev_ref = getattr(self, "_prev_angle_ref", curr_ref)
        if prev_ref != curr_ref:
            val = self.exit_angle.value()
            self.exit_angle.blockSignals(True)
            self.exit_angle.setValue(90.0 - val)
            self.exit_angle.blockSignals(False)
        self._prev_angle_ref = curr_ref

    # ---- Enable/disable inputs by mode

    def update_input_fields(self):
        mode = self.input_parameter.currentText()

        for w in [self.bottom_tension, self.top_tension, self.exit_angle, self.catenary_length, self.layback]:
            w.setDisabled(True)
        self.angle_reference.setDisabled(False)

        if mode == "Bottom Tension":
            self.bottom_tension.setDisabled(False)
        elif mode in ("Contact Tension", "Top Tension"):
            self.top_tension.setDisabled(False)
        elif mode in ("Tangent Angle", "Exit Angle"):
            self.exit_angle.setDisabled(False)
        elif mode == "Catenary Length":
            self.catenary_length.setDisabled(False)
        elif mode == "Layback":
            self.layback.setDisabled(False)

        self._sync_exit_angle_with_reference()

    # ---- Build config for calculator

    def get_config(self) -> Optional[dict]:
        try:
            D = float(self.water_depth.value())
            if D <= 0:
                raise ValueError("Water depth must be > 0.")

            c = float(self.chute_exit_height.value())
            if c < 0:
                raise ValueError("Top of Chute height must be >= 0.")

            R = float(self.chute_radius.value())
            if R < 0:
                raise ValueError("Chute radius must be >= 0.")

            ds = float(self.ds_step.value())
            if ds <= 0:
                raise ValueError("Integration step must be > 0.")

            q_w = float(self._fallback_q_water_npm)
            q_a = float(self._fallback_q_air_npm)

            if q_w <= 0 or q_a <= 0:
                raise ValueError("Internal fallback cable weights must be > 0.")

            mode = self.input_parameter.currentText()

            # Angle handling: always store "from horizontal"
            if self.angle_reference.currentText() == "from horizontal":
                exit_angle_from_h = float(self.exit_angle.value())
            else:
                exit_angle_from_h = 90.0 - float(self.exit_angle.value())

            assembly = self._assembly_from_table()
            comps: List[Component] = []

            cfg = {
                "water_depth_m": D,
                "chute_exit_height_m": c,
                "chute_radius_m": R,
                "ds_m": ds,
                "max_integration_steps": 25000,

                "q_water_npm": q_w,
                "q_air_npm": q_a,

                "assembly": assembly,
                "components": comps,
                "input_mode": mode,

                # guesses
                "H_guess_N": max(1.0, self.bottom_tension.value() * 1000.0),
                "S_guess_m": max(D + c + 1.0, self.catenary_length.value())
            }

            if mode == "Bottom Tension":
                cfg["H_input_N"] = float(self.bottom_tension.value()) * 1000.0
            elif mode in ("Contact Tension", "Top Tension"):
                cfg["Ttop_input_N"] = float(self.top_tension.value()) * 1000.0
            elif mode in ("Tangent Angle", "Exit Angle"):
                cfg["exit_angle_from_h_deg"] = exit_angle_from_h
            elif mode == "Catenary Length":
                S_in = float(self.catenary_length.value())
                # Hard lower bound: with positive weights the shortest suspended length is essentially
                # a vertical hang from TDP to the lowest possible departure height, plus any chute contact.
                # For the chute quarter-circle: at theta=pi/2 => Lc=R*pi/2 and y_dep=c-R.
                # If c<R, the lowest departure is below sea level; vertical distance is then just D.
                if R > 0:
                    S_min = D + max(c - R, 0.0) + (math.pi / 2.0) * R
                else:
                    S_min = D + c
                if S_in < S_min - 1e-6:
                    raise ValueError(
                        f"Total cable length ({S_in:.1f} m) is too short for the geometry. "
                        f"Minimum feasible length is about {S_min:.1f} m (given water depth={D:.1f} m, Top of Chute height={c:.1f} m, chute radius={R:.1f} m)."
                    )
                cfg["S_input_m"] = S_in
            elif mode == "Layback":
                cfg["layback_input_m"] = float(self.layback.value())
            else:
                raise ValueError("Invalid solve mode.")

            return cfg

        except Exception as e:
            self.results.setHtml(f'<span style="color:red;">{e}</span>')
            return None

    # ---- Main update

    def update_plot(self):
        cfg = self.get_config()
        if not cfg:
            self.figure.clear()
            self.canvas.draw()
            self._last_calc = None
            self._hover_cache = {}
            self._hide_crosshair()
            self.solver_diagnostics_btn.setEnabled(False)
            return

        try:
            calc = CatenarySystemCalculator(cfg)
            calc.solve()
            self._last_calc = calc
            self.solver_diagnostics_btn.setEnabled(True)

            # Update displayed "calculated" fields (soft sync)
            self._sync_calculated_fields(calc)

            # Results
            self._display_results(calc)

            # Plot
            self._plot(calc)

        except Exception as e:
            # Keep errors readable in the results pane.
            msg = str(e)
            msg = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            msg = msg.replace("\n", "<br>")
            self.results.setHtml(f'<span style="color:red;"><b>Error</b><br>{msg}</span>')
            self.figure.clear()
            self.canvas.draw()
            self._last_calc = None
            self._hover_cache = {}
            self._hide_crosshair()
            self.solver_diagnostics_btn.setEnabled(False)

    def _sync_calculated_fields(self, calc: CatenarySystemCalculator):
        # Don't fight the user's chosen input; only overwrite the others
        mode = self.input_parameter.currentText()

        if mode != "Bottom Tension" and calc.bottom_tension_kN is not None:
            self.bottom_tension.blockSignals(True)
            self.bottom_tension.setValue(calc.bottom_tension_kN)
            self.bottom_tension.blockSignals(False)

        if mode not in ("Contact Tension", "Top Tension") and calc.top_tension_kN is not None:
            self.top_tension.blockSignals(True)
            self.top_tension.setValue(calc.top_tension_kN)
            self.top_tension.blockSignals(False)

        if mode != "Catenary Length" and calc.S_total is not None:
            self.catenary_length.blockSignals(True)
            self.catenary_length.setValue(calc.S_total)
            self.catenary_length.blockSignals(False)

        if mode != "Layback" and calc.layback is not None:
            self.layback.blockSignals(True)
            self.layback.setValue(calc.layback)
            self.layback.blockSignals(False)

        # Tangent angle always updated to computed, respecting current reference display.
        if calc.exit_angle_deg_from_h is not None:
            self.exit_angle.blockSignals(True)
            if self.angle_reference.currentText() == "from horizontal":
                self.exit_angle.setValue(calc.exit_angle_deg_from_h)
            else:
                self.exit_angle.setValue(90.0 - calc.exit_angle_deg_from_h)
            self.exit_angle.blockSignals(False)

    def _display_results(self, calc: CatenarySystemCalculator):
        D = self.water_depth.value()
        c = self.chute_exit_height.value()
        flop_forward = (calc.S_total - calc.layback) if (calc.S_total is not None and calc.layback is not None) else None
        flop_forward_txt = f"{flop_forward:.1f} m" if flop_forward is not None else "N/A"
        angle_from_vertical = 90.0 - (calc.exit_angle_deg_from_h or 0.0)

        assembly: List[AssemblyItem] = calc.cfg.get("assembly", [])
        asm_seg_total = sum(max(0.0, it.length_m) for it in assembly if it.kind == "segment") if assembly else 0.0
        warn_lines: List[str] = []
        if assembly and calc.S_total is not None:
            # S_total includes chute-contact + free-span. Assembly segments are defined from chute top down.
            # If assembly is shorter than S_total, remaining length uses internal fallback weights.
            if calc.S_total > asm_seg_total + 1e-6:
                warn_lines.append(
                    f"Assembly segments total ({asm_seg_total:.1f} m) is shorter than modeled cable length ({calc.S_total:.1f} m). "
                    "Remaining length uses the internal fallback cable weight values."
                )
            # If assembly is much longer than S_total, some defined items are not in the suspended span.
            if asm_seg_total > calc.S_total + 1e-6:
                warn_lines.append(
                    f"Assembly segments total ({asm_seg_total:.1f} m) exceeds modeled cable length ({calc.S_total:.1f} m). "
                    "Lower assembly items may be on seabed (not in the suspended span) and bodies there will not affect the catenary."
                )

        sea_s = calc.s_sea_surface
        sea_txt = f"{sea_s:.1f} m" if sea_s is not None else "N/A"

        contact_txt = "N/A"
        if calc.x is not None and calc.y is not None and len(calc.x) and len(calc.y):
            contact_txt = f"x={float(calc.x[-1]):.1f} m, height={float(calc.y[-1]):.1f} m above waterline"

        chute_contact_txt = "N/A"
        if float(self.chute_radius.value()) > 0:
            chute_contact_txt = f"{float(getattr(calc, 'chute_contact_len_m', 0.0)):.1f} m"

        asm_txt = "N/A"
        if assembly:
            asm_txt = f"{asm_seg_total:.1f} m (segments only)"

        warn_txt = ""
        if warn_lines:
            warn_txt = "<br><br><b>Warnings</b><br>" + "<br>".join(f"• {escape(str(w))}" for w in warn_lines)

        txt = (
            f"Water Depth: {D:.1f} m<br>"
            f"Chute Top Height: {c:.1f} m above waterline<br><br>"
            f"Bottom Tension: {calc.bottom_tension_kN:.1f} kN<br>"
            f"Tension at Contact: {calc.top_tension_kN:.1f} kN<br>"
            f"Tangent Angle at Contact: {calc.exit_angle_deg_from_h:.1f}° from horizontal / {angle_from_vertical:.1f}° from vertical<br>"
            f"Total Cable Length (Touchdown to Top of Chute): {calc.S_total:.1f} m<br>"
            f"Layback (Touchdown to Top of Chute): {calc.layback:.1f} m<br>"
            f"Top of Chute KP: {self._format_kp(0.0)}<br>"
            f"TDP KP: {self._format_kp(calc.layback)}<br>"
            f"Flop Forward (Length - Layback): {flop_forward_txt}<br>"
            f"Chute contact/tangent point: {contact_txt}<br>"
            f"Cable on chute arc: {chute_contact_txt}<br>"
            f"Sea Surface Crossing Distance Along Cable: {sea_txt}<br>"
            f"Minimum Radius of Curvature (including chute): {calc.min_radius_m:.1f} m<br>"
            f"Assembly length: {asm_txt}<br>"
            f"{warn_txt}"
        )
        self.results.setHtml(txt)

    @staticmethod
    def _format_diag_value(value: Optional[float], units: str = "") -> str:
        if value is None:
            return "N/A"
        try:
            numeric = float(value)
        except Exception:
            return "N/A"
        if not math.isfinite(numeric):
            return "N/A"
        suffix = f" {escape(units)}" if units else ""
        return f"{numeric:.4g}{suffix}"

    def show_solver_diagnostics(self):
        calc = self._last_calc
        diagnostics = getattr(calc, "diagnostics", None) if calc is not None else None
        if calc is None or diagnostics is None:
            QMessageBox.information(self, "Solver diagnostics", "No solved catenary result is available yet.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Solver diagnostics")
        dialog.resize(620, 520)

        layout = QVBoxLayout(dialog)
        body = QTextEdit()
        body.setReadOnly(True)
        body.setHtml(self._solver_diagnostics_html(calc))
        layout.addWidget(body)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        qt_exec(dialog)

    def _solver_diagnostics_html(self, calc: CatenarySystemCalculator) -> str:
        diagnostics = calc.diagnostics

        chute_iteration_txt = "N/A"
        if getattr(diagnostics, "chute_contact_iterations", 0):
            chute_iteration_txt = (
                f"{diagnostics.chute_contact_iterations} iteration(s), "
                f"residual {self._format_diag_value(diagnostics.chute_contact_residual_m, 'm')}"
            )

        warning_rows = ""
        warnings = getattr(diagnostics, "warnings", []) or []
        if warnings:
            warning_rows = "".join(f"<li>{escape(str(warning))}</li>" for warning in warnings)
        else:
            warning_rows = "<li>No solver-specific warnings for this result.</li>"

        return (
            "<h2>Solver diagnostics</h2>"
            "<p>These values are for checking numerical quality of the current catenary result. "
            "They do not certify the model for every installation case; they help identify whether this solve is behaving consistently under the current static 2D assumptions.</p>"
            "<h3>How to read this</h3>"
            "<p>Residuals close to zero mean the solved curve is matching the selected input and chute boundary. "
            "The half-step replay repeats the same solved state with half the integration step; smaller deltas mean the result is less sensitive to step size. "
            "If the deltas are large, reduce Integration Step and treat the result as screening until validated externally.</p>"
            "<h3>Current solve</h3>"
            "<table cellspacing='4' cellpadding='2'>"
            f"<tr><td><b>Solve mode</b></td><td>{escape(str(diagnostics.input_mode))}</td></tr>"
            f"<tr><td><b>Integration step</b></td><td>requested {self._format_diag_value(diagnostics.ds_requested_m, 'm')}; "
            f"effective {self._format_diag_value(diagnostics.ds_effective_m, 'm')} over {diagnostics.integration_steps} steps</td></tr>"
            f"<tr><td><b>Free-span length</b></td><td>{self._format_diag_value(diagnostics.free_span_length_m, 'm')}</td></tr>"
            f"<tr><td><b>Chute contact length</b></td><td>{self._format_diag_value(diagnostics.chute_contact_length_m, 'm')}</td></tr>"
            f"<tr><td><b>Boundary height residual</b></td><td>{self._format_diag_value(diagnostics.boundary_residual_m, 'm')}</td></tr>"
            f"<tr><td><b>{escape(str(diagnostics.input_residual_label))}</b></td>"
            f"<td>{self._format_diag_value(diagnostics.input_residual, diagnostics.input_residual_units)}</td></tr>"
            f"<tr><td><b>Chute contact iteration</b></td><td>{chute_iteration_txt}</td></tr>"
            f"<tr><td><b>Half-step position delta</b></td><td>{self._format_diag_value(diagnostics.refinement_position_delta_m, 'm')}</td></tr>"
            f"<tr><td><b>Half-step angle delta</b></td><td>{self._format_diag_value(diagnostics.refinement_angle_delta_deg, 'deg')}</td></tr>"
            f"<tr><td><b>Half-step tension delta</b></td><td>{self._format_diag_value(diagnostics.refinement_top_tension_delta_kN, 'kN')}</td></tr>"
            "</table>"
            "<h3>Warnings and interpretation</h3>"
            f"<ul>{warning_rows}</ul>"
        )

    def _capture_plot_view(self) -> Optional[Tuple[float, float, float, float]]:
        try:
            axes = self.figure.get_axes()
            if not axes:
                return None
            ax = axes[0]
            x0, x1 = ax.get_xlim()
            y0, y1 = ax.get_ylim()
            values = (float(x0), float(x1), float(y0), float(y1))
            if any(not math.isfinite(v) for v in values):
                return None
            if abs(values[1] - values[0]) < 1e-9 or abs(values[3] - values[2]) < 1e-9:
                return None
            return values
        except Exception:
            return None

    def _x_axis_reference_key(self) -> str:
        try:
            return "chute_top" if self.x_axis_reference.currentIndex() == 1 else "tdp"
        except Exception:
            return "tdp"

    def _x_axis_origin_offset(self, calc: CatenarySystemCalculator) -> float:
        if self._x_axis_reference_key() == "chute_top" and calc.layback is not None:
            return float(calc.layback)
        return 0.0

    def _x_axis_label(self) -> str:
        if self._x_axis_reference_key() == "chute_top":
            return "Horizontal Distance from Top of Chute (m)"
        return "Horizontal Distance from TDP (m)"

    @staticmethod
    def _direction_sign(combo: QComboBox) -> float:
        try:
            return -1.0 if combo.currentIndex() == 1 else 1.0
        except Exception:
            return 1.0

    def _cable_count_sign(self) -> float:
        return self._direction_sign(self.cable_count_direction)

    def _cable_count_at_distance_from_top(self, distance_from_top_m: float) -> float:
        return float(self.cable_count_top.value()) + self._cable_count_sign() * float(distance_from_top_m)

    def _format_cable_count(self, distance_from_top_m: Optional[float]) -> str:
        if distance_from_top_m is None:
            return "N/A"
        return f"{self._cable_count_at_distance_from_top(distance_from_top_m):.1f} m"

    def _kp_sign(self) -> float:
        return self._direction_sign(self.kp_direction)

    def _kp_at_horizontal_from_top(self, horizontal_from_top_m: float) -> float:
        return float(self.kp_top.value()) + self._kp_sign() * (float(horizontal_from_top_m) / 1000.0)

    def _format_kp(self, horizontal_from_top_m: Optional[float]) -> str:
        if horizontal_from_top_m is None:
            return "N/A"
        try:
            return f"{self._kp_at_horizontal_from_top(horizontal_from_top_m):.3f}"
        except Exception:
            return "N/A"

    def _horizontal_from_top_for_plot_x(self, plot_x_m: float) -> Optional[float]:
        layback_plot = self._hover_cache.get("layback_plot_m")
        if layback_plot is None:
            return None
        try:
            return float(layback_plot) - float(plot_x_m)
        except Exception:
            return None

    @staticmethod
    def _hover_readout_text(tooltip_text: str) -> str:
        lines = [line.strip() for line in str(tooltip_text or "").splitlines() if line.strip()]
        if not lines:
            return ""
        if len(lines) == 1:
            return lines[0]
        first_line = " | ".join(lines[:2])
        second_line = " | ".join(lines[2:6])
        return f"{first_line}\n{second_line}" if second_line else first_line

    @staticmethod
    def _segment_at_distance_from_top(assembly: List[AssemblyItem], distance_from_top_m: float) -> Optional[AssemblyItem]:
        cursor = 0.0
        last_segment = None
        for item in assembly:
            if item.kind != "segment":
                continue
            start = cursor
            end = cursor + max(0.0, item.length_m)
            if start <= distance_from_top_m <= end:
                return item
            if distance_from_top_m >= end:
                last_segment = item
            cursor = end
        return last_segment

    @staticmethod
    def _local_tension_kN(calc: CatenarySystemCalculator, s_from_tdp_m: Optional[float]) -> Optional[float]:
        if s_from_tdp_m is None or calc.s is None or calc.tension_kN is None:
            return None
        if len(calc.s) == 0 or len(calc.tension_kN) != len(calc.s):
            return None
        try:
            s_value = max(float(calc.s[0]), min(float(calc.s[-1]), float(s_from_tdp_m)))
            return float(np.interp(s_value, calc.s, calc.tension_kN))
        except Exception:
            return None

    def _on_crosshair_toggled(self):
        self._sync_crosshair_connection()
        if not self.show_crosshair_values.isChecked():
            self._hide_crosshair()
            self.canvas.setToolTip("")
        self.schedule_update_plot()

    def _sync_crosshair_connection(self):
        if self.show_crosshair_values.isChecked():
            if self._crosshair_cid is None:
                self._crosshair_cid = self.canvas.mpl_connect("motion_notify_event", self._on_plot_mouse_move)
        elif self._crosshair_cid is not None:
            try:
                self.canvas.mpl_disconnect(self._crosshair_cid)
            except Exception:
                pass
            self._crosshair_cid = None

    def _hide_crosshair(self):
        for line in (self._crosshair_vline, self._crosshair_hline):
            try:
                if line is not None:
                    line.set_visible(False)
            except Exception:
                pass
        if hasattr(self, "hover_readout"):
            self.hover_readout.setText("")
            self.hover_readout.setVisible(True)
        self._last_hover_signature = None
        self.canvas.draw_idle()

    def _axis_spans_for_hover(self) -> Tuple[float, float]:
        axes = self.figure.get_axes()
        if not axes:
            return 1.0, 1.0
        try:
            x0, x1 = axes[0].get_xlim()
            y0, y1 = axes[0].get_ylim()
            return max(abs(x1 - x0), 1e-9), max(abs(y1 - y0), 1e-9)
        except Exception:
            return 1.0, 1.0

    def _nearest_body_hit(self, x_value: float, depth_value: float) -> Optional[dict]:
        body_points = self._hover_cache.get("body_points") or []
        if not body_points:
            return None
        x_span, y_span = self._axis_spans_for_hover()
        best_body = None
        best_distance = float("inf")
        for body in body_points:
            try:
                distance = ((float(body["x"]) - x_value) / x_span) ** 2 + ((float(body["depth"]) - depth_value) / y_span) ** 2
            except Exception:
                continue
            if distance < best_distance:
                best_distance = distance
                best_body = body
        if best_body is not None and best_distance <= 0.0025:
            return best_body
        return None

    def _body_detail_lines(self, body: dict, compact: bool = False) -> List[str]:
        tension = body.get("tension_kN")
        tension_txt = f"{float(tension):.2f} kN" if tension is not None else "N/A"
        load = float(body.get("point_load_kN", 0.0) or 0.0)
        load_note = "downward" if load >= 0 else "buoyant/upward"
        horizontal_from_top = body.get("horizontal_from_top_m")
        horizontal_txt = f"{float(horizontal_from_top):.2f} m" if horizontal_from_top is not None else "N/A"
        lines = [
            f"Body: {body.get('name', 'Body')}",
            f"Position: {body.get('position', 'N/A')}",
            f"KP: {body.get('kp', 'N/A')}",
            f"Cable count: {body.get('cable_count', 'N/A')}",
            f"Tension: {tension_txt}",
        ]
        if not compact:
            lines.extend([
                f"Distance from Top of Chute: {float(body.get('distance_from_top_m', 0.0)):.2f} m",
                f"Horizontal from Top of Chute: {horizontal_txt}",
                f"s from TDP: {body.get('s_from_tdp_txt', 'N/A')}",
            ])
        segment_name = body.get("segment_name")
        if segment_name:
            lines.append(f"Segment: {segment_name}")
            if not compact:
                lines.append(f"Segment weights: {body.get('segment_weight_txt', 'N/A')}")
        lines.append(f"Point load: {abs(load):.2f} kN {load_note}")
        return lines

    def _show_body_details(self, body: dict):
        QMessageBox.information(self, "Body details", "\n".join(self._body_detail_lines(body, compact=False)))

    def _on_plot_click(self, event):
        if event.inaxes is None or event.xdata is None or event.ydata is None:
            return
        try:
            if event.button not in (None, 1):
                return
            body = self._nearest_body_hit(float(event.xdata), float(event.ydata))
            if body is not None:
                self._show_body_details(body)
        except Exception:
            return

    def _on_plot_mouse_move(self, event):
        if not self.show_crosshair_values.isChecked() or event.inaxes is None:
            self._hide_crosshair()
            self.canvas.setToolTip("")
            return
        if event.xdata is None or event.ydata is None:
            self._hide_crosshair()
            self.canvas.setToolTip("")
            return
        axes = self.figure.get_axes()
        if not axes or event.inaxes is not axes[0]:
            self._hide_crosshair()
            self.canvas.setToolTip("")
            return

        try:
            x_value = float(event.xdata)
            y_value = float(event.ydata)
        except Exception:
            self._hide_crosshair()
            self.canvas.setToolTip("")
            return

        if self._crosshair_vline is not None:
            self._crosshair_vline.set_xdata([x_value, x_value])
            self._crosshair_vline.set_visible(True)
        if self._crosshair_hline is not None:
            self._crosshair_hline.set_ydata([y_value, y_value])
            self._crosshair_hline.set_visible(True)

        tooltip_text, signature = self._hover_tooltip_text(x_value, y_value)
        if signature != self._last_hover_signature:
            self._last_hover_signature = signature
            self.canvas.setToolTip(tooltip_text)
            self.hover_readout.setText(self._hover_readout_text(tooltip_text))
            self.hover_readout.setVisible(True)
        self.canvas.draw_idle()

    def _hover_tooltip_text(self, x_value: float, depth_value: float) -> Tuple[str, Tuple[Any, ...]]:
        cursor_horizontal_from_top = self._horizontal_from_top_for_plot_x(x_value)
        lines = [
            f"X: {x_value:.2f} m",
            f"KP: {self._format_kp(cursor_horizontal_from_top)}",
            f"Depth: {depth_value:.2f} m",
            f"Height above waterline: {-depth_value:.2f} m",
        ]

        body = self._nearest_body_hit(x_value, depth_value)
        if body is not None:
            body_lines = self._body_detail_lines(body, compact=True)
            return "\n".join(body_lines), ("body", body.get("name"), round(float(body.get("distance_from_top_m", 0.0)), 2))

        curve_x = self._hover_cache.get("curve_x")
        curve_depth = self._hover_cache.get("curve_depth")
        curve_s = self._hover_cache.get("curve_s")
        curve_tension = self._hover_cache.get("curve_tension_kN")
        curve_horizontal = self._hover_cache.get("curve_horizontal_from_top_m")
        if curve_x is None or curve_depth is None or curve_s is None or len(curve_x) == 0:
            return "\n".join(lines), ("free", round(x_value, 2), round(depth_value, 2))

        try:
            x_span, y_span = self._axis_spans_for_hover()
            distances = ((curve_x - x_value) / x_span) ** 2 + ((curve_depth - depth_value) / y_span) ** 2
            idx = int(np.argmin(distances))
            if float(distances[idx]) <= 0.0064:
                distance_from_top = self._hover_cache.get("chute_contact_len_m", 0.0) + self._hover_cache.get("free_span_length_m", 0.0) - float(curve_s[idx])
                horizontal_from_top = None
                if curve_horizontal is not None and len(curve_horizontal) == len(curve_s):
                    horizontal_from_top = float(curve_horizontal[idx])
                tension_txt = "N/A"
                if curve_tension is not None and len(curve_tension) == len(curve_s):
                    tension_txt = f"{float(curve_tension[idx]):.2f} kN"
                segment = self._segment_at_distance_from_top(self._hover_cache.get("assembly", []), distance_from_top)
                segment_name = getattr(segment, "name", "") if segment is not None else ""
                lines.extend([
                    "",
                    "Nearest cable point:",
                    f"s from TDP: {float(curve_s[idx]):.2f} m",
                    f"Distance from Top of Chute: {distance_from_top:.2f} m",
                    f"Horizontal from Top of Chute: {horizontal_from_top:.2f} m" if horizontal_from_top is not None else "Horizontal from Top of Chute: N/A",
                    f"KP: {self._format_kp(horizontal_from_top)}",
                    f"Cable count: {self._format_cable_count(distance_from_top)}",
                    f"Segment: {segment_name}" if segment_name else "Segment: N/A",
                    f"Cable depth: {float(curve_depth[idx]):.2f} m",
                    f"Tension: {tension_txt}",
                ])
                return "\n".join(lines), ("cable", int(idx), round(x_value, 1), round(depth_value, 1))
        except Exception:
            pass
        return "\n".join(lines), ("free", round(x_value, 2), round(depth_value, 2))

    def _build_hover_cache(
        self,
        calc: CatenarySystemCalculator,
        assembly: List[AssemblyItem],
        x_origin_offset: float,
        layback_plot: float,
        D: float,
        c: float,
        R: float,
    ):
        self._hover_cache = {}
        self._last_hover_signature = None
        if calc.x is None or calc.y is None or calc.s is None:
            return

        S_free = float(calc.s[-1]) if len(calc.s) else 0.0
        Lc = float(getattr(calc, "chute_contact_len_m", 0.0))
        body_points: List[dict] = []
        cursor = 0.0
        previous_segment: Optional[AssemblyItem] = None

        for item in assembly:
            if item.kind == "segment":
                previous_segment = item
                cursor += max(0.0, item.length_m)
                continue
            if item.kind != "body":
                continue

            distance_from_top = float(cursor)
            x_body = None
            depth_body = None
            s_body = None
            position = "outside modeled span"
            tension_kN = None

            if R > 0 and distance_from_top < Lc:
                phi = (math.pi / 2.0) + (distance_from_top / R)
                x_body = layback_plot + R * math.cos(phi)
                y_body = c - R + R * math.sin(phi)
                depth_body = -y_body
                position = "on chute arc"
                tension_kN = calc.top_tension_kN
            elif distance_from_top <= Lc + S_free:
                s_body = S_free - (distance_from_top - Lc)
                x_body = float(np.interp(s_body, calc.s, calc.x)) - x_origin_offset
                y_body = float(np.interp(s_body, calc.s, calc.y))
                depth_body = -y_body
                position = "free span"
                tension_kN = self._local_tension_kN(calc, s_body)
            elif self.show_full_assembly_seabed.isChecked() and calc.S_total is not None and distance_from_top > float(calc.S_total):
                x_body = -(distance_from_top - float(calc.S_total)) - x_origin_offset
                depth_body = D
                position = "on seabed"

            if x_body is None or depth_body is None:
                continue

            horizontal_from_top = None
            if calc.layback is not None:
                try:
                    horizontal_from_top = float(calc.layback) - (float(x_body) + float(x_origin_offset))
                except Exception:
                    horizontal_from_top = None

            segment = previous_segment or self._segment_at_distance_from_top(assembly, distance_from_top)
            segment_weight_txt = "N/A"
            segment_name = ""
            if segment is not None:
                segment_name = segment.name
                segment_weight_txt = f"water {float(segment.q_water_npm):.2f} N/m, air {float(segment.q_air_npm):.2f} N/m"

            body_points.append({
                "name": item.name or "Body",
                "x": float(x_body),
                "depth": float(depth_body),
                "position": position,
                "distance_from_top_m": distance_from_top,
                "horizontal_from_top_m": horizontal_from_top,
                "kp": self._format_kp(horizontal_from_top),
                "cable_count": self._format_cable_count(distance_from_top),
                "s_from_tdp_m": s_body,
                "s_from_tdp_txt": f"{float(s_body):.2f} m" if s_body is not None else "N/A",
                "tension_kN": tension_kN,
                "point_load_kN": float(item.point_load_kN),
                "segment_name": segment_name,
                "segment_weight_txt": segment_weight_txt,
            })

        layback_plot = None
        if calc.layback is not None:
            try:
                layback_plot = float(calc.layback) - float(x_origin_offset)
            except Exception:
                layback_plot = None

        self._hover_cache = {
            "curve_x": np.array(calc.x - x_origin_offset, dtype=float),
            "curve_depth": np.array(-calc.y, dtype=float),
            "curve_s": np.array(calc.s, dtype=float),
            "curve_tension_kN": np.array(calc.tension_kN, dtype=float) if calc.tension_kN is not None else None,
            "curve_horizontal_from_top_m": float(calc.layback) - np.array(calc.x, dtype=float) if calc.layback is not None else None,
            "body_points": body_points,
            "assembly": assembly,
            "layback_plot_m": layback_plot,
            "free_span_length_m": S_free,
            "chute_contact_len_m": Lc,
        }

    def _add_plot_labels(
        self,
        ax,
        calc: CatenarySystemCalculator,
        assembly: List[AssemblyItem],
        x_origin_offset: float,
        layback_plot: float,
        D: float,
        c: float,
        R: float,
    ):
        if not self.show_plot_labels.isChecked() or calc.s is None or calc.x is None or calc.y is None:
            return

        S_free = float(calc.s[-1])
        Lc = float(getattr(calc, "chute_contact_len_m", 0.0))
        plot_x = np.array(calc.x - x_origin_offset, dtype=float)
        plot_depth = np.array(-calc.y, dtype=float)
        x_span = max(1.0, float(np.max(plot_x) - np.min(plot_x))) if len(plot_x) else 1.0
        y_span = max(1.0, float(np.max(plot_depth) - np.min(plot_depth)), float(D), abs(float(c)) + float(R))
        label_offset = max(0.75, 0.018 * max(x_span, y_span))
        label_count = 0
        max_labels = 80

        def add_label(
            x_value: float,
            depth_value: float,
            label: str,
            color: str = "#111827",
            ha: str = "center",
            va: str = "bottom",
        ):
            nonlocal label_count
            if label_count >= max_labels or not label:
                return
            ax.text(
                x_value,
                depth_value,
                label,
                color=color or "#111827",
                fontsize=8,
                ha=ha,
                va=va,
                background=True,
            )
            label_count += 1

        tdp_x = -x_origin_offset
        tdp_horizontal = float(calc.layback) if calc.layback is not None else None
        tdp_count = self._format_cable_count(calc.S_total if calc.S_total is not None else Lc + S_free)
        add_label(tdp_x + label_offset, float(D) - label_offset, f"TDP\nCC {tdp_count}\nKP {self._format_kp(tdp_horizontal)}", "#111827", ha="left", va="bottom")
        add_label(layback_plot, -float(c) - label_offset, f"Top of Chute\nCC {self._format_cable_count(0.0)}\nKP {self._format_kp(0.0)}", "#111827", ha="center", va="bottom")

        def curve_label_position(s_label: float, offset_multiplier: float) -> Tuple[float, float]:
            x_base = float(np.interp(s_label, calc.s, plot_x))
            depth_base = float(np.interp(s_label, calc.s, plot_depth))
            idx = int(np.searchsorted(calc.s, s_label))
            idx0 = max(0, idx - 2)
            idx1 = min(len(calc.s) - 1, idx + 2)
            dx = float(plot_x[idx1] - plot_x[idx0])
            dy = float(plot_depth[idx1] - plot_depth[idx0])
            mag = math.hypot(dx, dy)
            if mag <= 1e-12:
                return x_base, depth_base - label_offset * offset_multiplier
            normal_x = -dy / mag
            normal_y = dx / mag
            if normal_y > 0:
                normal_x = -normal_x
                normal_y = -normal_y
            return x_base + normal_x * label_offset * offset_multiplier, depth_base + normal_y * label_offset * offset_multiplier

        body_offsets = [
            (0.0, -1.4),
            (1.3, -1.1),
            (-1.3, -1.1),
            (0.0, 1.2),
            (1.2, 1.0),
            (-1.2, 1.0),
        ]

        cursor = 0.0
        body_index = 0
        for item in assembly:
            if item.kind == "segment":
                length = max(0.0, item.length_m)
                d0 = cursor
                d1 = cursor + length
                overlap0 = max(d0, Lc)
                overlap1 = min(d1, Lc + S_free)
                if overlap1 > overlap0 + 1e-6:
                    d_label = 0.5 * (overlap0 + overlap1)
                    s_label = S_free - (d_label - Lc)
                    offset_multiplier = 1.0 + 0.28 * (label_count % 3)
                    x_label, depth_label = curve_label_position(s_label, offset_multiplier)
                    add_label(x_label, depth_label, item.name, self._normalize_color_hex(getattr(item, "color_hex", "")) or "#111827")
                cursor = d1
                continue

            if item.kind != "body":
                continue

            body_color = self._normalize_color_hex(getattr(item, "color_hex", "")) or "#111827"
            d_body = cursor
            offset_x, offset_y = body_offsets[body_index % len(body_offsets)]
            body_index += 1
            if R > 0 and d_body < Lc:
                phi = (math.pi / 2.0) + (float(d_body) / R)
                xb = layback_plot + R * math.cos(phi)
                yb = c - R + R * math.sin(phi)
                add_label(xb + offset_x * label_offset, -yb + offset_y * label_offset, item.name, body_color)
            elif d_body <= Lc + S_free:
                s_body = S_free - (d_body - Lc)
                xb = float(np.interp(s_body, calc.s, calc.x)) - x_origin_offset
                yb = float(np.interp(s_body, calc.s, calc.y))
                add_label(xb + offset_x * label_offset, -yb + offset_y * label_offset, item.name, body_color)
            elif self.show_full_assembly_seabed.isChecked() and calc.S_total is not None:
                x_body_physical = -(d_body - float(calc.S_total))
                xb = x_body_physical - x_origin_offset
                add_label(xb + offset_x * label_offset, D + offset_y * label_offset, item.name, body_color)

    def _plot(self, calc: CatenarySystemCalculator):
        plot_x_reference = self._x_axis_reference_key()
        previous_view = self._capture_plot_view()
        if getattr(self, "_last_plot_x_reference", None) != plot_x_reference:
            previous_view = None
        self.figure.clear()
        ax = self.figure.add_subplot(111)

        if calc.x is None or calc.y is None:
            self._hover_cache = {}
            self.canvas.draw()
            return

        x_internal = calc.x
        y = calc.y

        # Convert to depth for plotting (depth positive down)
        depth = -y

        D = self.water_depth.value()
        c = self.chute_exit_height.value()
        R = self.chute_radius.value()
        layback = float(calc.layback) if calc.layback is not None else float(x_internal[-1])
        x_origin_offset = self._x_axis_origin_offset(calc)
        x = x_internal - x_origin_offset
        layback_plot = layback - x_origin_offset

        # Cable (color by assembly segment if available)
        assembly: List[AssemblyItem] = calc.cfg.get("assembly", [])
        if assembly and calc.s is not None:
            s = calc.s
            S_free = float(s[-1])
            Lc = float(getattr(calc, "chute_contact_len_m", 0.0))

            # Determine segment index for each midpoint between points
            seg_items = [it for it in assembly if it.kind == "segment"]
            seg_lengths = [max(0.0, it.length_m) for it in seg_items]
            seg_colors = [self._normalize_color_hex(getattr(it, "color_hex", "")) for it in seg_items]
            seg_starts: List[float] = []
            cursor = 0.0
            for L in seg_lengths:
                seg_starts.append(cursor)
                cursor += L

            def segment_index_for_d(d_from_top: float) -> int:
                if not seg_lengths:
                    return 0
                cursor2 = 0.0
                for idx, L in enumerate(seg_lengths):
                    if cursor2 <= d_from_top <= (cursor2 + L):
                        return idx
                    cursor2 += L
                return max(0, len(seg_lengths) - 1)

            # Build colored line segments
            pts = np.column_stack([x, depth])
            segs = [pts[i : i + 2] for i in range(len(pts) - 1)]

            s_mid = 0.5 * (s[:-1] + s[1:])
            d_mid = Lc + (S_free - s_mid)
            idxs = [segment_index_for_d(float(d)) for d in d_mid]

            # Prefer user-selected colours from the assembly table (per segment).
            # If not provided, fall back to tab10.
            colors: List[Any] = []
            for i in idxs:
                if 0 <= int(i) < len(seg_colors) and seg_colors[int(i)]:
                    colors.append(seg_colors[int(i)])
                else:
                    colors.append(get_tab10_color(int(i)))

            lc = LineCollection(segs, colors=colors, linewidths=2)
            ax.add_collection(lc)
            # Add a legend handle
            ax.plot([], [], color=colors[0] if colors else "k", linewidth=2, label="Cable (by segment)")
        else:
            ax.plot(x, depth, label="Cable", linewidth=2)

        # Sea level and seabed
        ax.axhline(0, linewidth=2, label="Sea Level")
        ax.axhline(D, linewidth=2, label="Seabed")

        marker_size = 16

        # Mark key endpoints without overpowering the catenary line.
        ax.scatter([float(x[0])], [float(depth[0])], s=marker_size, color="black", label="Touchdown point")
        ax.scatter([layback_plot], [-c], s=marker_size, color="black", label="Top of Chute reference")

        # With no radius, the free-span endpoint is the chute top.
        x_dep = float(x[-1])
        y_dep = float(y[-1])
        if R <= 0:
            ax.scatter([x_dep], [-y_dep], s=marker_size, color="black", label="Cable endpoint")

        # Body markers (if any) positioned along free-span by s interpolation
        if assembly and calc.s is not None and calc.x is not None and calc.y is not None:
            s = calc.s
            S_free = float(s[-1])
            Lc = float(getattr(calc, "chute_contact_len_m", 0.0))

            # Bodies placed after cumulative segment length from top
            d_cursor = 0.0
            body_entries: List[Tuple[float, str]] = []
            for it in assembly:
                if it.kind == "segment":
                    d_cursor += max(0.0, it.length_m)
                elif it.kind == "body":
                    color_hex = self._normalize_color_hex(getattr(it, "color_hex", "")) or "#000000"
                    body_entries.append((d_cursor, color_hex))

            body_points: List[Tuple[float, float, str]] = []
            for d_body, body_color in body_entries:
                if R > 0 and d_body < Lc:
                    phi = (math.pi / 2.0) + (float(d_body) / R)
                    xb = layback_plot + R * math.cos(phi)
                    yb = c - R + R * math.sin(phi)
                    body_points.append((xb, -yb, body_color))
                    continue
                if d_body > (Lc + S_free):
                    continue
                s_body = S_free - (d_body - Lc)
                # Interpolate x,y at s_body
                xb = float(np.interp(s_body, s, calc.x)) - x_origin_offset
                yb = float(np.interp(s_body, s, calc.y))
                body_points.append((xb, -yb, body_color))

            if body_points:
                for i, (xb, bd, body_color) in enumerate(body_points):
                    label = "Body" if i == 0 else None
                    ax.scatter([xb], [bd], marker="D", s=36, facecolors="none", edgecolors=body_color, label=label)

        # Render chute as a filled upper-left quadrant, then draw the cable arc from contact to top.
        chute_x = None
        chute_y = None
        seabed_x = None
        seabed_y = None
        if R > 0:
            theta_end = math.radians(calc.exit_angle_deg_from_h or 0.0)
            theta_end = max(0.0, min(math.pi / 2.0, theta_end))

            x_top = layback_plot
            y_top = c
            center_x = x_top
            center_y = y_top - R

            phi0 = math.pi / 2.0
            phi_full = math.pi
            phis_full = np.linspace(phi0, phi_full, 160)
            chute_x = center_x + R * np.cos(phis_full)
            chute_y = center_y + R * np.sin(phis_full)

            body_x = np.concatenate(([center_x], chute_x, [center_x]))
            body_y = np.concatenate(([center_y], chute_y, [center_y]))
            ax.fill(
                body_x,
                -body_y,
                facecolor="#c8ccd2",
                edgecolor="#6b7280",
                linewidth=1,
                alpha=0.65,
                zorder=-10,
            )
            ax.plot(chute_x, -chute_y, color="#6b7280", linewidth=1.5, label="Chute body")
            ax.plot([center_x, x_top], [-center_y, -y_top], color="#6b7280", linewidth=1)
            ax.plot([center_x, center_x - R], [-center_y, -center_y], color="#6b7280", linewidth=1)

            # Draw the cable section that is assumed to be in contact with the chute (top -> tangent point).
            phi1 = math.pi / 2.0 + theta_end
            phis_contact = np.linspace(phi0, phi1, 90)
            contact_x = center_x + R * np.cos(phis_contact)
            contact_y = center_y + R * np.sin(phis_contact)

            if assembly and len(contact_x) >= 2:
                seg_items_arc = [it for it in assembly if it.kind == "segment"]
                seg_lengths_arc = [max(0.0, it.length_m) for it in seg_items_arc]
                seg_colors_arc = [self._normalize_color_hex(getattr(it, "color_hex", "")) for it in seg_items_arc]

                def segment_index_for_chute_d(d_from_top: float) -> int:
                    if not seg_lengths_arc:
                        return 0
                    cursor2 = 0.0
                    for idx, L in enumerate(seg_lengths_arc):
                        if cursor2 <= d_from_top <= (cursor2 + L):
                            return idx
                        cursor2 += L
                    return max(0, len(seg_lengths_arc) - 1)

                pts_arc = np.column_stack([contact_x, -contact_y])
                segs_arc = [pts_arc[i : i + 2] for i in range(len(pts_arc) - 1)]
                d_vals = np.linspace(0.0, float(getattr(calc, "chute_contact_len_m", 0.0)), len(contact_x))
                d_mid = 0.5 * (d_vals[:-1] + d_vals[1:])
                idxs_arc = [segment_index_for_chute_d(float(d)) for d in d_mid]
                colors_arc: List[Any] = []
                for i in idxs_arc:
                    if 0 <= int(i) < len(seg_colors_arc) and seg_colors_arc[int(i)]:
                        colors_arc.append(seg_colors_arc[int(i)])
                    else:
                        colors_arc.append(get_tab10_color(int(i)))
                ax.add_collection(LineCollection(segs_arc, colors=colors_arc, linewidths=3))
                ax.plot([], [], color=colors_arc[0] if colors_arc else "k", linewidth=3, label="Cable on chute")
            else:
                ax.plot(contact_x, -contact_y, color="#ff7f0e", linewidth=3, label="Cable on chute")

            x_contact = center_x + R * math.cos(phi1)
            y_contact = center_y + R * math.sin(phi1)
            ax.scatter([x_contact], [-y_contact], s=marker_size, color="black", label="Chute contact/tangent")

        # Optional: draw full assembly laid out on seabed beyond TDP

        if self.show_full_assembly_seabed.isChecked() and assembly and calc.S_total is not None:
            asm_seg_total = sum(max(0.0, it.length_m) for it in assembly if it.kind == "segment")
            seabed_len = max(0.0, asm_seg_total - float(calc.S_total))
            if seabed_len > 1e-6:
                # Build a polyline from x=0 at TDP to negative x away from vessel
                n_pts = max(2, int(min(600, max(2, seabed_len / max(ds_step := float(self.ds_step.value()), 0.25)))))
                xs_physical = np.linspace(0.0, -seabed_len, n_pts)
                xs = xs_physical - x_origin_offset
                ys = np.full_like(xs, D)
                seabed_x = xs
                seabed_y = ys

                # Color seabed line by segment, using d_from_top = S_total + distance_from_tdp_on_seabed
                seg_items2 = [it for it in assembly if it.kind == "segment"]
                seg_lengths = [max(0.0, it.length_m) for it in seg_items2]
                seg_colors2 = [self._normalize_color_hex(getattr(it, "color_hex", "")) for it in seg_items2]

                def segment_index_for_d(d_from_top: float) -> int:
                    if not seg_lengths:
                        return 0
                    cursor2 = 0.0
                    for idx, L in enumerate(seg_lengths):
                        if cursor2 <= d_from_top <= (cursor2 + L):
                            return idx
                        cursor2 += L
                    return max(0, len(seg_lengths) - 1)

                pts2 = np.column_stack([xs, ys])
                segs2 = [pts2[i : i + 2] for i in range(len(pts2) - 1)]
                x_mid = 0.5 * (xs_physical[:-1] + xs_physical[1:])
                d_mid = float(calc.S_total) + (-x_mid)
                idxs2 = [segment_index_for_d(float(d)) for d in d_mid]

                colors2: List[Any] = []
                for i in idxs2:
                    if 0 <= int(i) < len(seg_colors2) and seg_colors2[int(i)]:
                        colors2.append(seg_colors2[int(i)])
                    else:
                        colors2.append(get_tab10_color(int(i)))

                lc2 = LineCollection(segs2, colors=colors2, linewidths=2, alpha=0.9)
                ax.add_collection(lc2)
                ax.plot([], [], color=colors2[0] if colors2 else "k", linewidth=2, label="Assembly on seabed")

                # Bodies that are on seabed
                d_cursor = 0.0
                seabed_body_points: List[Tuple[float, float, str]] = []
                for it in assembly:
                    if it.kind == "segment":
                        d_cursor += max(0.0, it.length_m)
                        continue
                    if it.kind != "body":
                        continue
                    body_color = self._normalize_color_hex(getattr(it, "color_hex", "")) or "#000000"
                    d_body = d_cursor
                    if d_body <= float(calc.S_total):
                        continue
                    x_body_physical = -(d_body - float(calc.S_total))
                    if x_body_physical < -seabed_len - 1e-6:
                        continue
                    x_body = x_body_physical - x_origin_offset
                    seabed_body_points.append((x_body, D, body_color))

                if seabed_body_points:
                    for i, (bx, by, body_color) in enumerate(seabed_body_points):
                        label = "Body (seabed)" if i == 0 else None
                        ax.scatter([bx], [by], marker="D", s=36, facecolors="none", edgecolors=body_color, label=label)

                end_x = -seabed_len - x_origin_offset
                ax.scatter([end_x], [D], marker="s", s=36, color="#111827", label="End of cable")
                if self.show_plot_labels.isChecked():
                    label_offset = max(0.75, 0.018 * max(abs(float(seabed_len)), float(D), 1.0))
                    end_horizontal_from_top = float(layback) + float(seabed_len)
                    ax.text(
                        end_x,
                        D - label_offset,
                        f"End of cable\nCC {self._format_cable_count(asm_seg_total)}\nKP {self._format_kp(end_horizontal_from_top)}",
                        color="#111827",
                        fontsize=8,
                        ha="center",
                        va="bottom",
                        background=True,
                    )

        self._build_hover_cache(calc, assembly, x_origin_offset, layback_plot, D, c, R)
        self._add_plot_labels(ax, calc, assembly, x_origin_offset, layback_plot, D, c, R)

        ax.set_xlabel(self._x_axis_label())
        ax.set_ylabel("Depth (m)")
        ax.set_title("Cable Catenary")
        # Bounds: compute from all drawn geometry so equal-aspect plots still show everything.
        x_candidates: List[float] = [float(np.min(x)), float(np.max(x)), float(x[0]), float(layback_plot), 0.0]
        y_candidates: List[float] = [float(np.min(depth)), float(np.max(depth)), 0.0, float(D), float(-c)]

        if isinstance(chute_x, np.ndarray) and isinstance(chute_y, np.ndarray) and chute_x.size and chute_y.size:
            x_candidates.extend([float(np.min(chute_x)), float(np.max(chute_x))])
            y_candidates.extend([float(np.min(-chute_y)), float(np.max(-chute_y))])

        if isinstance(seabed_x, np.ndarray) and isinstance(seabed_y, np.ndarray) and seabed_x.size and seabed_y.size:
            x_candidates.extend([float(np.min(seabed_x)), float(np.max(seabed_x))])
            y_candidates.extend([float(np.min(seabed_y)), float(np.max(seabed_y))])

        x_min = float(min(x_candidates))
        x_max = float(max(x_candidates))
        y_min = float(min(y_candidates))
        y_max = float(max(y_candidates))

        # Add a small padding so objects don't sit on the frame.
        pad_x = max(1.0, 0.05 * (x_max - x_min))
        pad_y = max(1.0, 0.05 * (y_max - y_min))

        full_xlim = (x_min - pad_x, x_max + pad_x)
        full_ylim = (y_min - pad_y, y_max + pad_y)

        if previous_view is not None:
            old_x0, old_x1, old_y0, old_y1 = previous_view
            ax.set_xlim(old_x0, old_x1)
            ax.set_ylim(old_y0, old_y1)
        else:
            ax.set_xlim(full_xlim[0], full_xlim[1])
            ax.set_ylim(full_ylim[0], full_ylim[1])

        # Critical: enforce true proportions so the chute radius looks correct.
        # This will often "zoom out" visually when depth span dwarfs horizontal span.
        ax.set_aspect("equal", adjustable="box")
        ax.invert_yaxis()  # conventional: depth downwards
        ax.grid(True, alpha=0.25)

        if self.show_kp_axis.isChecked() and hasattr(ax, "set_secondary_xaxis"):
            ax.set_secondary_xaxis("KP", lambda plot_x, top_x=layback_plot: self._format_kp(float(top_x) - float(plot_x)))

        self._crosshair_vline = None
        self._crosshair_hline = None
        self._sync_crosshair_connection()
        if self.show_crosshair_values.isChecked():
            self._crosshair_vline = ax.axvline(0.0, color="#111827", linestyle="--", linewidth=0.8)
            self._crosshair_hline = ax.axhline(0.0, color="#111827", linestyle="--", linewidth=0.8)
            self._crosshair_vline.set_visible(False)
            self._crosshair_hline.set_visible(False)
        else:
            self.canvas.setToolTip("")

        if self.show_legend.isChecked():
            ax.legend(fontsize="small")

        self._last_plot_x_reference = plot_x_reference
        self.figure.tight_layout()
        self.canvas.draw()

    # ---- Export

    def export_svg(self):
        self.canvas.show_export_dialog()

    def export_dxf(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save DXF", "catenary.dxf", "DXF Files (*.dxf)")
        if not path:
            return

        calc = self._last_calc
        if not calc or calc.x is None or calc.y is None:
            self.results.setHtml('<span style="color:red;">No catenary data to export.</span>')
            return

        # DXF expects planar coords; export in mm.
        assembly: List[AssemblyItem] = calc.cfg.get("assembly", [])
        D = float(self.water_depth.value())
        c = float(self.chute_exit_height.value())
        R = float(self.chute_radius.value())
        layback = float(calc.layback) if calc.layback is not None else float(calc.x[-1])
        x_origin_offset = self._x_axis_origin_offset(calc)
        x_export = calc.x - x_origin_offset
        layback_export = layback - x_origin_offset

        # Scale-dependent defaults for label sizes/offsets (mm)
        x_span_mm = float((np.max(calc.x) - np.min(calc.x)) * 1000.0)
        y_span_mm = float((np.max(calc.y) - np.min(calc.y)) * 1000.0)
        span_mm = max(1.0, x_span_mm, y_span_mm, float(D * 1000.0))
        text_h = max(200.0, 0.015 * span_mm)
        text_off = 1.2 * text_h

        entities: List[str] = []

        def segment_items() -> List[AssemblyItem]:
            return [it for it in assembly if it.kind == "segment"]

        seg_items = segment_items()
        seg_lengths = [max(0.0, it.length_m) for it in seg_items]

        def segment_index_for_d(d_from_top_m: float) -> int:
            if not seg_lengths:
                return 0
            cursor = 0.0
            for idx, L in enumerate(seg_lengths):
                if cursor <= d_from_top_m <= (cursor + L):
                    return idx
                cursor += L
            return max(0, len(seg_lengths) - 1)

        def seg_layer(idx: int) -> str:
            if 0 <= idx < len(seg_items):
                base = f"SEG{idx+1:02d}_{seg_items[idx].name}"
            else:
                base = f"SEG{idx+1:02d}"
            return self._dxf_sanitize_layer(base)

        def seg_label(idx: int) -> str:
            if not (0 <= idx < len(seg_items)):
                return f"Segment {idx+1}"
            it = seg_items[idx]
            qw = float(it.q_water_npm)
            qa = float(it.q_air_npm)
            qw_txt = f"{qw:.3f} N/m" if qw > 0 else "(global)"
            qa_txt = f"{qa:.3f} N/m" if qa > 0 else "(global)"
            return f"{it.name} | qW={qw_txt} qA={qa_txt}"

        # 1) Export cable along the free-span, split by assembly segment where possible.
        if assembly and calc.s is not None and calc.x is not None and calc.y is not None:
            s = calc.s
            S_free = float(s[-1])
            Lc = float(getattr(calc, "chute_contact_len_m", 0.0))

            # Determine segment index for each line segment (between points) using midpoint mapping.
            if len(s) >= 2:
                s_mid = 0.5 * (s[:-1] + s[1:])
                d_mid = Lc + (S_free - s_mid)
                idxs = [segment_index_for_d(float(d)) for d in d_mid]

                # Split into runs of equal idx
                run_start = 0
                curr = idxs[0] if idxs else 0
                for i, idx in enumerate(idxs):
                    if idx != curr:
                        xs = (x_export[run_start : i + 1] * 1000.0).tolist()
                        ys = (calc.y[run_start : i + 1] * 1000.0).tolist()
                        layer = seg_layer(curr)
                        entities.append(self._dxf_polyline_entity(xs, ys, layer=layer))
                        mid = max(0, len(xs) // 2)
                        entities.append(self._dxf_text_entity(xs[mid], ys[mid] + text_off, seg_label(curr), height=text_h, layer=layer))
                        run_start = i
                        curr = idx

                # last run
                if idxs:
                    xs = (x_export[run_start:] * 1000.0).tolist()
                    ys = (calc.y[run_start:] * 1000.0).tolist()
                    layer = seg_layer(curr)
                    entities.append(self._dxf_polyline_entity(xs, ys, layer=layer))
                    mid = max(0, len(xs) // 2)
                    entities.append(self._dxf_text_entity(xs[mid], ys[mid] + text_off, seg_label(curr), height=text_h, layer=layer))

            # 2) Export chute geometry (full quadrant) and cable-on-chute (contact arc) split by segment.
            if R > 0 and calc.exit_angle_deg_from_h is not None:
                x_top = layback_export
                y_top = c
                center_x = x_top
                center_y = y_top - R

                # Full quadrant geometry (upper-left)
                phis_full = np.linspace(math.pi / 2.0, math.pi, 160)
                chute_x = (center_x + R * np.cos(phis_full)) * 1000.0
                chute_y = (center_y + R * np.sin(phis_full)) * 1000.0
                entities.append(self._dxf_polyline_entity(chute_x.tolist(), chute_y.tolist(), layer=self._dxf_sanitize_layer("CHUTE_GEOM")))

                # Chute label (leader-style)
                chute_layer = self._dxf_sanitize_layer("CHUTE")
                x_label = (x_top * 1000.0) + (2.5 * text_off)
                y_label = (y_top * 1000.0) + (2.5 * text_off)
                entities.append(self._dxf_line_entity(x_top * 1000.0, y_top * 1000.0, x_label, y_label, layer=chute_layer))
                entities.append(self._dxf_text_entity(x_label, y_label, f"Chute | R={R:.3f} m | top={c:.3f} m", height=text_h, layer=chute_layer))

                # Contact portion: d in [0, Lc]
                theta_end = max(0.0, min(math.pi / 2.0, math.radians(float(calc.exit_angle_deg_from_h))))
                Lc = float(getattr(calc, "chute_contact_len_m", 0.0))
                if Lc > 1e-9:
                    n = 120
                    d_vals = np.linspace(0.0, Lc, n)
                    phis = (math.pi / 2.0) + (d_vals / R)
                    cx = center_x + R * np.cos(phis)
                    cy = center_y + R * np.sin(phis)

                    # split contact arc by segment index using midpoint distance from top
                    d_mid2 = 0.5 * (d_vals[:-1] + d_vals[1:])
                    idxs2 = [segment_index_for_d(float(d)) for d in d_mid2]
                    run_start = 0
                    curr = idxs2[0] if idxs2 else 0
                    for i, idx in enumerate(idxs2):
                        if idx != curr:
                            xs = (cx[run_start : i + 1] * 1000.0).tolist()
                            ys = (cy[run_start : i + 1] * 1000.0).tolist()
                            entities.append(self._dxf_polyline_entity(xs, ys, layer=seg_layer(curr)))
                            run_start = i
                            curr = idx

                    if idxs2:
                        xs = (cx[run_start:] * 1000.0).tolist()
                        ys = (cy[run_start:] * 1000.0).tolist()
                        entities.append(self._dxf_polyline_entity(xs, ys, layer=seg_layer(curr)))

            # 3) Export any remaining assembly on seabed (if assembly longer than suspended span)
            if calc.S_total is not None:
                asm_seg_total = sum(max(0.0, it.length_m) for it in seg_items)
                seabed_len = max(0.0, asm_seg_total - float(calc.S_total))
                if seabed_len > 1e-6:
                    n_pts = max(2, int(min(800, max(2, seabed_len / max(float(self.ds_step.value()), 0.25)))))
                    xs_physical = np.linspace(0.0, -seabed_len, n_pts)
                    xs = xs_physical - x_origin_offset
                    ys = np.full_like(xs, -D)

                    # split by segment index using d_from_top = S_total + distance from TDP on seabed
                    x_mid = 0.5 * (xs_physical[:-1] + xs_physical[1:])
                    d_mid3 = float(calc.S_total) + (-x_mid)
                    idxs3 = [segment_index_for_d(float(d)) for d in d_mid3]
                    run_start = 0
                    curr = idxs3[0] if idxs3 else 0
                    for i, idx in enumerate(idxs3):
                        if idx != curr:
                            px = (xs[run_start : i + 1] * 1000.0).tolist()
                            py = (ys[run_start : i + 1] * 1000.0).tolist()
                            entities.append(self._dxf_polyline_entity(px, py, layer=seg_layer(curr)))
                            run_start = i
                            curr = idx
                    if idxs3:
                        px = (xs[run_start:] * 1000.0).tolist()
                        py = (ys[run_start:] * 1000.0).tolist()
                        entities.append(self._dxf_polyline_entity(px, py, layer=seg_layer(curr)))

            # 4) Export bodies as POINT + TEXT labels
            d_cursor = 0.0
            for it in assembly:
                if it.kind == "segment":
                    d_cursor += max(0.0, it.length_m)
                    continue
                if it.kind != "body":
                    continue

                d_body = float(d_cursor)
                x_body_m: Optional[float] = None
                y_body_m: Optional[float] = None

                S_free = float(calc.s[-1]) if calc.s is not None else 0.0
                Lc = float(getattr(calc, "chute_contact_len_m", 0.0))

                # Body on chute contact
                if R > 0 and d_body < Lc:
                    x_top = layback
                    y_top = c
                    center_x = x_top
                    center_y = y_top - R
                    phi = (math.pi / 2.0) + (d_body / R)
                    x_body_m = center_x + R * math.cos(phi)
                    y_body_m = center_y + R * math.sin(phi)
                # Body on free-span
                elif calc.s is not None and d_body <= (Lc + S_free):
                    s_body = S_free - (d_body - Lc)
                    x_body_m = float(np.interp(s_body, calc.s, calc.x))
                    y_body_m = float(np.interp(s_body, calc.s, calc.y))
                # Body on seabed beyond TDP
                elif calc.S_total is not None and d_body > float(calc.S_total):
                    x_body_m = -(d_body - float(calc.S_total))
                    y_body_m = -D

                if x_body_m is None or y_body_m is None:
                    continue

                layer = self._dxf_sanitize_layer(f"BODY_{it.name}")
                xb = (x_body_m - x_origin_offset) * 1000.0
                yb = y_body_m * 1000.0

                # Visible body marker geometry (small square) + point
                body_size = max(250.0, 0.9 * text_h)
                entities.append(self._dxf_point_entity(xb, yb, layer=layer))
                entities.append(self._dxf_rectangle_entity(xb, yb, body_size, body_size, layer=layer))

                # Leader-style label
                x_text = xb + (2.0 * text_off)
                y_text = yb + (1.0 * text_off)
                entities.append(self._dxf_line_entity(xb, yb, x_text, y_text, layer=layer))
                entities.append(self._dxf_text_entity(x_text, y_text, f"{it.name} | load={float(it.point_load_kN):.3f} kN", height=text_h, layer=layer))

        else:
            # No assembly: export a single cable polyline on a single layer.
            x_mm = (x_export * 1000.0).tolist()
            y_mm = (calc.y * 1000.0).tolist()
            entities.append(self._dxf_polyline_entity(x_mm, y_mm, layer=self._dxf_sanitize_layer("CABLE")))

            # Chute geometry and contact arc (optional)
            if R > 0 and calc.layback is not None and calc.exit_angle_deg_from_h is not None:
                x_top = layback_export
                y_top = c
                center_x = x_top
                center_y = y_top - R

                phis_full = np.linspace(math.pi / 2.0, math.pi, 160)
                chute_x = (center_x + R * np.cos(phis_full)) * 1000.0
                chute_y = (center_y + R * np.sin(phis_full)) * 1000.0
                entities.append(self._dxf_polyline_entity(chute_x.tolist(), chute_y.tolist(), layer=self._dxf_sanitize_layer("CHUTE_GEOM")))

                chute_layer = self._dxf_sanitize_layer("CHUTE")
                x_label = (x_top * 1000.0) + (2.5 * text_off)
                y_label = (y_top * 1000.0) + (2.5 * text_off)
                entities.append(self._dxf_line_entity(x_top * 1000.0, y_top * 1000.0, x_label, y_label, layer=chute_layer))
                entities.append(self._dxf_text_entity(x_label, y_label, f"Chute | R={R:.3f} m | top={c:.3f} m", height=text_h, layer=chute_layer))

                theta_end = math.radians(float(calc.exit_angle_deg_from_h))
                theta_end = max(0.0, min(math.pi / 2.0, theta_end))
                phi0 = math.pi / 2.0
                phi1 = math.pi / 2.0 + theta_end
                phis = np.linspace(phi0, phi1, 90)
                arc_x = (center_x + R * np.cos(phis)) * 1000.0
                arc_y = (center_y + R * np.sin(phis)) * 1000.0
                entities.append(self._dxf_polyline_entity(arc_x.tolist(), arc_y.tolist(), layer=self._dxf_sanitize_layer("CHUTE_CONTACT")))

        # Reference lines: sea level and seabed
        # Use an x-span that covers all likely exported geometry (cable + chute + optional seabed).
        x_min_m = float(np.min(x_export))
        x_max_m = float(np.max(x_export))
        if R > 0:
            x_min_m = min(x_min_m, layback_export - R)
            x_max_m = max(x_max_m, layback_export)
        if assembly and calc.S_total is not None:
            seg_items2 = [it for it in assembly if it.kind == "segment"]
            asm_seg_total = sum(max(0.0, it.length_m) for it in seg_items2)
            seabed_len = max(0.0, asm_seg_total - float(calc.S_total))
            if seabed_len > 1e-6:
                x_min_m = min(x_min_m, -seabed_len - x_origin_offset)

        pad_m = max(5.0, 0.05 * max(1.0, x_max_m - x_min_m))
        x0 = (x_min_m - pad_m) * 1000.0
        x1 = (x_max_m + pad_m) * 1000.0

        sea_layer = self._dxf_sanitize_layer("REF_SEA")
        seabed_layer = self._dxf_sanitize_layer("REF_SEABED")

        # Sea level at y=0
        entities.append(self._dxf_line_entity(x0, 0.0, x1, 0.0, layer=sea_layer))
        entities.append(self._dxf_text_entity(x1, 0.0 + text_off, "Sea level (y=0)", height=text_h, layer=sea_layer))

        # Seabed at y=-D (internal coordinates)
        y_seabed = (-D) * 1000.0
        entities.append(self._dxf_line_entity(x0, y_seabed, x1, y_seabed, layer=seabed_layer))
        entities.append(self._dxf_text_entity(x1, y_seabed + text_off, f"Seabed (y=-{D:.3f} m)", height=text_h, layer=seabed_layer))

        dxf = self._dxf_build(entities)
        with open(path, "w") as f:
            f.write(dxf)

    # ---- Components table helpers

    def _table_get_float(self, table: QTableWidget, row: int, col: int, default: float = 0.0) -> float:
        item = table.item(row, col)
        if item is None:
            return default
        try:
            return float(item.text())
        except Exception:
            return default

    def _table_get_str(self, table: QTableWidget, row: int, col: int, default: str = "") -> str:
        item = table.item(row, col)
        if item is None:
            return default
        val = str(item.text()).strip()
        return val if val else default

    # ---- Assembly table helpers

    def _assembly_from_table(self) -> List[AssemblyItem]:
        items: List[AssemblyItem] = []
        for r in range(self.assembly_table.rowCount()):
            kind_raw = self._table_get_str(self.assembly_table, r, self.ASM_COL_TYPE, default="segment").lower()
            kind = "segment" if kind_raw.startswith("seg") else "body"
            name = self._table_get_str(self.assembly_table, r, self.ASM_COL_NAME, default=("Segment" if kind == "segment" else "Body"))
            length = self._table_get_float(self.assembly_table, r, self.ASM_COL_LENGTH, 0.0)
            q_w = self._table_get_float(self.assembly_table, r, self.ASM_COL_Q_WATER, 0.0)
            q_a = self._table_get_float(self.assembly_table, r, self.ASM_COL_Q_AIR, 0.0)
            p_kN = self._table_get_float(self.assembly_table, r, self.ASM_COL_BODY_LOAD, 0.0)
            color_hex = self._table_get_str(self.assembly_table, r, self.ASM_COL_COLOR, default="")
            color_hex = self._normalize_color_hex(color_hex)

            if kind == "segment":
                if length <= 0:
                    continue
                if q_w <= 0 or q_a <= 0:
                    # allow blank to mean "use global" by keeping 0s
                    pass
                items.append(AssemblyItem(kind=kind, name=name, length_m=length, q_water_npm=q_w, q_air_npm=q_a, point_load_kN=0.0, color_hex=color_hex))
            else:
                items.append(AssemblyItem(kind=kind, name=name, length_m=0.0, q_water_npm=0.0, q_air_npm=0.0, point_load_kN=p_kN, color_hex=color_hex))

        return items

    def _on_asm_add_segment(self):
        self.assembly_table.blockSignals(True)
        try:
            r = self.assembly_table.rowCount()
            self.assembly_table.insertRow(r)
            defaults: List[Tuple[int, Any]] = [
                (0, "Segment"),
                (1, "Cable"),
                (2, 10.0),
                (3, self._fallback_q_water_npm),
                (4, self._fallback_q_air_npm),
                (5, ""),
            ]
            for col, val in defaults:
                self.assembly_table.setItem(r, col, QTableWidgetItem(str(val)))

            self._ensure_assembly_color_cell(r)
            self.assembly_table.setCurrentCell(r, 1)
        finally:
            self.assembly_table.blockSignals(False)
        self._sync_json_from_table()
        self.update_plot()

    def _on_asm_add_body(self):
        self.assembly_table.blockSignals(True)
        try:
            r = self.assembly_table.rowCount()
            self.assembly_table.insertRow(r)
            defaults: List[Tuple[int, Any]] = [
                (0, "Body"),
                (1, "Body"),
                (2, ""),
                (3, ""),
                (4, ""),
                (5, 5.0),
            ]
            for col, val in defaults:
                self.assembly_table.setItem(r, col, QTableWidgetItem(str(val)))

            self._ensure_assembly_color_cell(r)
            self.assembly_table.setCurrentCell(r, 1)
        finally:
            self.assembly_table.blockSignals(False)
        self._sync_json_from_table()
        self.update_plot()

    def _on_asm_delete(self):
        r = self.assembly_table.currentRow()
        if r < 0:
            return
        self.assembly_table.removeRow(r)
        self._sync_json_from_table()
        self.update_plot()

    def _asm_swap_rows(self, r1: int, r2: int):
        if r1 < 0 or r2 < 0:
            return
        if r1 >= self.assembly_table.rowCount() or r2 >= self.assembly_table.rowCount():
            return
        self.assembly_table.blockSignals(True)
        try:
            for c in range(self.assembly_table.columnCount()):
                i1 = self.assembly_table.takeItem(r1, c)
                i2 = self.assembly_table.takeItem(r2, c)
                self.assembly_table.setItem(r1, c, i2)
                self.assembly_table.setItem(r2, c, i1)
        finally:
            self.assembly_table.blockSignals(False)

    def _on_asm_move_up(self):
        r = self.assembly_table.currentRow()
        if r <= 0:
            return
        self._asm_swap_rows(r, r - 1)
        self.assembly_table.setCurrentCell(r - 1, 1)
        self._sync_json_from_table()
        self.update_plot()

    def _on_asm_move_down(self):
        r = self.assembly_table.currentRow()
        if r < 0 or r >= self.assembly_table.rowCount() - 1:
            return
        self._asm_swap_rows(r, r + 1)
        self.assembly_table.setCurrentCell(r + 1, 1)
        self._sync_json_from_table()
        self.update_plot()

    def _assembly_table_to_json(self) -> str:
        return json.dumps(self._assembly_table_to_json_data(), indent=2)

    def _assembly_table_to_json_data(self) -> List[dict]:
        data: List[dict] = []
        for r in range(self.assembly_table.rowCount()):
            kind = "segment" if self._is_assembly_row_segment(r) else "body"
            name = self._table_get_str(self.assembly_table, r, self.ASM_COL_NAME, default=("Cable" if kind == "segment" else "Body"))
            if kind == "segment":
                entry = {
                    "type": "segment",
                    "name": name,
                    "length_m": self._table_get_float(self.assembly_table, r, self.ASM_COL_LENGTH, 0.0),
                    "q_water_npm": self._table_get_float(self.assembly_table, r, self.ASM_COL_Q_WATER, 0.0),
                    "q_air_npm": self._table_get_float(self.assembly_table, r, self.ASM_COL_Q_AIR, 0.0),
                }
                color_hex = self._normalize_color_hex(self._table_get_str(self.assembly_table, r, self.ASM_COL_COLOR, default=""))
                if color_hex:
                    entry["color"] = color_hex
            else:
                entry = {
                    "type": "body",
                    "name": name,
                    "point_load_kN": self._table_get_float(self.assembly_table, r, self.ASM_COL_BODY_LOAD, 0.0),
                }
                color_hex = self._normalize_color_hex(self._table_get_str(self.assembly_table, r, self.ASM_COL_COLOR, default=""))
                if color_hex:
                    entry["color"] = color_hex
            data.append(entry)
        return data

    def _assembly_table_from_json(self, raw: str) -> bool:
        try:
            data = json.loads(raw or "[]")
            if isinstance(data, dict):
                data = data.get("assembly", [])
            if not isinstance(data, list):
                return False
        except Exception:
            return False
        self.assembly_table.blockSignals(True)
        try:
            self.assembly_table.setRowCount(0)
            for row_data in data:
                if isinstance(row_data, dict):
                    row = self._assembly_json_entry_to_row(row_data)
                elif isinstance(row_data, list):
                    row = [str(v) for v in row_data]
                else:
                    continue
                r = self.assembly_table.rowCount()
                self.assembly_table.insertRow(r)
                for c in range(min(len(row), self.assembly_table.columnCount())):
                    self.assembly_table.setItem(r, c, QTableWidgetItem(str(row[c])))

                # Ensure colour cell exists / has reasonable defaults for older saved tables.
                self._ensure_assembly_color_cell(r)
        finally:
            self.assembly_table.blockSignals(False)
        return True

    def _assembly_json_entry_to_row(self, entry: dict) -> List[str]:
        kind_raw = str(entry.get("type", entry.get("kind", "segment"))).strip().lower()
        is_body = kind_raw.startswith("body")
        name = str(entry.get("name", "Body" if is_body else "Cable"))

        def number_text(*keys: str, default: float = 0.0) -> str:
            for key in keys:
                if key in entry:
                    try:
                        return f"{float(entry.get(key)):.12g}"
                    except Exception:
                        return f"{float(default):.12g}"
            return f"{float(default):.12g}"

        color_hex = self._normalize_color_hex(str(entry.get("color", entry.get("color_hex", ""))))

        if is_body:
            return [
                "Body",
                name,
                "",
                "",
                "",
                number_text("point_load_kN", "load_kN", default=0.0),
                color_hex,
            ]

        return [
            "Segment",
            name,
            number_text("length_m", "length", default=0.0),
            number_text("q_water_npm", "q_water", "weight_water_npm", default=self._fallback_q_water_npm),
            number_text("q_air_npm", "q_air", "weight_air_npm", default=self._fallback_q_air_npm),
            "",
            color_hex,
        ]

    def _sync_json_from_table(self):
        if not hasattr(self, "assembly_json_text"):
            return
        if getattr(self, "_syncing_assembly_json", False):
            return
        self._syncing_assembly_json = True
        try:
            self.assembly_json_text.blockSignals(True)
            self.assembly_json_text.setPlainText(self._assembly_table_to_json())
            self.assembly_json_text.blockSignals(False)
            self.assembly_json_text.setStyleSheet("")
            self.assembly_json_text.setToolTip("Assembly JSON is synced with the table.")
        finally:
            self._syncing_assembly_json = False

    def _on_assembly_json_text_changed(self):
        if getattr(self, "_syncing_assembly_json", False):
            return
        if getattr(self, "_initializing", False):
            return
        self._assembly_json_timer.start(350)

    def _apply_assembly_json_text(self):
        if getattr(self, "_syncing_assembly_json", False):
            return
        raw = self.assembly_json_text.toPlainText().strip()
        self._syncing_assembly_json = True
        try:
            ok = self._assembly_table_from_json(raw or "[]")
        finally:
            self._syncing_assembly_json = False

        if not ok:
            self.assembly_json_text.setStyleSheet("border: 1px solid #c0392b;")
            self.assembly_json_text.setToolTip("JSON is not valid yet; the table has not been updated.")
            return

        self.assembly_json_text.setStyleSheet("")
        self.assembly_json_text.setToolTip("Assembly JSON is synced with the table.")
        self._sync_json_from_table()
        self.schedule_update_plot()

    def _normalize_color_hex(self, value: str) -> str:
        s = (value or "").strip()
        if not s:
            return ""
        if not s.startswith("#"):
            s = "#" + s
        if len(s) != 7:
            return ""
        try:
            _ = int(s[1:], 16)
        except Exception:
            return ""
        return s.lower()

    def _is_assembly_row_segment(self, row: int) -> bool:
        kind_raw = self._table_get_str(self.assembly_table, row, self.ASM_COL_TYPE, default="segment").lower()
        return kind_raw.startswith("seg")

    def _next_default_segment_color_hex(self) -> str:
        # Choose based on the count of existing segment rows (not total rows).
        seg_count = 0
        for r in range(self.assembly_table.rowCount()):
            if self._is_assembly_row_segment(r):
                seg_count += 1
        return self._DEFAULT_SEGMENT_COLORS[seg_count % len(self._DEFAULT_SEGMENT_COLORS)]

    def _next_default_body_color_hex(self) -> str:
        body_count = 0
        for r in range(self.assembly_table.rowCount()):
            if not self._is_assembly_row_segment(r):
                body_count += 1
        return self._DEFAULT_BODY_COLORS[body_count % len(self._DEFAULT_BODY_COLORS)]

    def _set_assembly_color_cell(self, row: int, color_hex: str):
        # Colour applies to both segment lines and body markers. Keep the cell visually plain.
        item = self.assembly_table.item(row, self.ASM_COL_COLOR)
        if item is None:
            item = QTableWidgetItem("")
            self.assembly_table.setItem(row, self.ASM_COL_COLOR, item)

        color_hex = self._normalize_color_hex(color_hex)
        item.setText(color_hex)
        item.setToolTip("Double-click to pick a colour")

        flags = item.flags()
        flags = flags | Qt.ItemFlag.ItemIsEnabled
        flags = flags & ~Qt.ItemFlag.ItemIsEditable
        item.setFlags(flags)
        role_enum = getattr(Qt, "ItemDataRole", Qt)
        item.setData(getattr(role_enum, "BackgroundRole"), None)
        item.setData(getattr(role_enum, "ForegroundRole"), None)

    def _ensure_assembly_color_cell(self, row: int):
        # Ensure the colour cell exists with a sensible default for row type.
        is_seg = self._is_assembly_row_segment(row)
        type_item = self.assembly_table.item(row, self.ASM_COL_TYPE)
        if type_item is not None:
            normalized_type = "Segment" if is_seg else "Body"
            if type_item.text().strip() != normalized_type:
                type_item.setText(normalized_type)
        current = self._table_get_str(self.assembly_table, row, self.ASM_COL_COLOR, default="")
        current = self._normalize_color_hex(current)
        if not current:
            current = self._next_default_segment_color_hex() if is_seg else self._next_default_body_color_hex()
        self._set_assembly_color_cell(row, current)

    def _on_assembly_table_cell_changed(self, row: int, col: int):
        # Keep the colour column consistent when users change the Type cell.
        if col == self.ASM_COL_TYPE:
            self.assembly_table.blockSignals(True)
            try:
                self._ensure_assembly_color_cell(row)
            finally:
                self.assembly_table.blockSignals(False)
        self._sync_json_from_table()
        self.schedule_update_plot()

    def _on_assembly_table_cell_double_clicked(self, row: int, col: int):
        if col != self.ASM_COL_COLOR:
            return
        if row < 0 or row >= self.assembly_table.rowCount():
            return

        current = self._table_get_str(self.assembly_table, row, self.ASM_COL_COLOR, default="")
        current = self._normalize_color_hex(current)
        initial = QColor(current) if current else QColor("#1f77b4")
        chosen = QColorDialog.getColor(initial, self, "Select assembly item colour")
        if not chosen.isValid():
            return

        self.assembly_table.blockSignals(True)
        try:
            self._set_assembly_color_cell(row, chosen.name())
        finally:
            self.assembly_table.blockSignals(False)
        self._sync_json_from_table()
        self.update_plot()


    def _dxf_sanitize_layer(self, name: str) -> str:
        # Conservative layer naming for broad DXF compatibility.
        raw = (name or "0").strip().upper().replace(" ", "_")
        cleaned = "".join(ch for ch in raw if (ch.isalnum() or ch in ("_", "-")))
        return (cleaned[:31] or "0")

    def _dxf_polyline_entity(self, x: List[float], y: List[float], layer: str = "0") -> str:
        layer = self._dxf_sanitize_layer(layer)
        ent = f"0\nPOLYLINE\n8\n{layer}\n66\n1\n70\n0\n"
        for xi, yi in zip(x, y):
            ent += f"0\nVERTEX\n8\n{layer}\n10\n{xi}\n20\n{yi}\n30\n0.0\n"
        ent += "0\nSEQEND\n"
        return ent

    def _dxf_point_entity(self, x: float, y: float, layer: str = "0") -> str:
        layer = self._dxf_sanitize_layer(layer)
        return f"0\nPOINT\n8\n{layer}\n10\n{x}\n20\n{y}\n30\n0.0\n"

    def _dxf_line_entity(self, x1: float, y1: float, x2: float, y2: float, layer: str = "0") -> str:
        layer = self._dxf_sanitize_layer(layer)
        return (
            f"0\nLINE\n8\n{layer}\n"
            f"10\n{x1}\n20\n{y1}\n30\n0.0\n"
            f"11\n{x2}\n21\n{y2}\n31\n0.0\n"
        )

    def _dxf_rectangle_entity(self, x_center: float, y_center: float, width: float, height: float, layer: str = "0") -> str:
        # Axis-aligned rectangle polyline centered at (x_center, y_center)
        layer = self._dxf_sanitize_layer(layer)
        hw = 0.5 * float(width)
        hh = 0.5 * float(height)
        xs = [x_center - hw, x_center + hw, x_center + hw, x_center - hw, x_center - hw]
        ys = [y_center - hh, y_center - hh, y_center + hh, y_center + hh, y_center - hh]
        return self._dxf_polyline_entity(xs, ys, layer=layer)

    def _dxf_text_entity(self, x: float, y: float, text: str, height: float, layer: str = "0") -> str:
        layer = self._dxf_sanitize_layer(layer)
        safe = (text or "").replace("\n", " ").replace("\r", " ")
        # TEXT entity (single-line)
        return (
            f"0\nTEXT\n8\n{layer}\n"
            f"10\n{x}\n20\n{y}\n30\n0.0\n"
            f"40\n{height}\n1\n{safe}\n7\nSTANDARD\n"
        )

    def _dxf_build(self, entities: List[str]) -> str:
        # Minimal ASCII DXF with a single ENTITIES section.
        body = "".join(entities or [])
        return f"0\nSECTION\n2\nENTITIES\n{body}0\nENDSEC\n0\nEOF\n"

    def generate_dxf_polyline(self, x: List[float], y: List[float]) -> str:
        # Backwards-compatible wrapper.
        return self._dxf_build([self._dxf_polyline_entity(x, y, layer="0")])
