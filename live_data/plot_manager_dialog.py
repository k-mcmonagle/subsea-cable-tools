"""
Plot Manager Dialog

UI for creating, editing, and configuring live data plots.
Allows users to select fields, set formatting, and customize appearance.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QSpinBox, QDoubleSpinBox,
    QComboBox, QPushButton, QCheckBox, QColorDialog, QFormLayout, QGroupBox,
    QMessageBox, QTabWidget, QWidget, QSlider
)
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon
import uuid

from .plot_config import PlotConfig, PlotStyling, AxisConfig, AdvancedConfig


class PlotManagerDialog(QDialog):
    """
    Dialog for creating and editing plot configurations.
    Provides UI for field selection, formatting, styling, and validation.
    """
    
    plot_configured = pyqtSignal(PlotConfig)  # Emitted when plot is saved
    
    def __init__(self, available_fields: list, existing_config: PlotConfig = None, parent=None):
        """
        Initialize the plot manager dialog.
        
        Args:
            available_fields: List of available field names from data stream
            existing_config: If editing, the PlotConfig to edit. If None, create new plot.
            parent: Parent widget
        """
        super().__init__(parent)
        self.available_fields = available_fields
        self.existing_config = existing_config
        self.is_editing = existing_config is not None
        
        self.setWindowTitle("Edit Plot" if self.is_editing else "New Plot")
        self.setModal(True)
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)
        
        self.setup_ui()
        if self.is_editing:
            self.load_config(existing_config)
    
    def setup_ui(self):
        """Build the dialog UI."""
        layout = QVBoxLayout(self)
        
        # Create tabs
        tabs = QTabWidget()
        
        # Basic tab
        basic_widget = self.create_basic_tab()
        tabs.addTab(basic_widget, "Basic")
        
        # Fields tab
        fields_widget = self.create_fields_tab()
        tabs.addTab(fields_widget, "Fields")
        
        # Styling tab
        style_widget = self.create_style_tab()
        tabs.addTab(style_widget, "Styling")
        
        # Scaling tab
        scaling_widget = self.create_scaling_tab()
        tabs.addTab(scaling_widget, "Scaling")
        
        # Advanced tab
        advanced_widget = self.create_advanced_tab()
        tabs.addTab(advanced_widget, "Advanced")
        
        layout.addWidget(tabs)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.ok_btn = QPushButton("OK")
        self.ok_btn.clicked.connect(self.validate_and_save)
        btn_layout.addWidget(self.ok_btn)
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        layout.addLayout(btn_layout)
    
    def create_basic_tab(self) -> QWidget:
        """Create the Basic settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        layout = QFormLayout()
        
        # Plot name
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g., Depth Trend")
        layout.addRow("Plot Name:", self.name_edit)
        
        # Time window (seconds)
        time_window_layout = QHBoxLayout()
        self.time_window_spinbox = QSpinBox()
        self.time_window_spinbox.setMinimum(30)
        self.time_window_spinbox.setMaximum(3600)
        self.time_window_spinbox.setValue(300)
        self.time_window_spinbox.setSuffix(" seconds")
        time_window_layout.addWidget(self.time_window_spinbox)
        time_window_layout.addStretch()
        layout.addRow("Time Window:", time_window_layout)
        
        # Max points
        max_points_layout = QHBoxLayout()
        self.max_points_spinbox = QSpinBox()
        self.max_points_spinbox.setMinimum(100)
        self.max_points_spinbox.setMaximum(10000)
        self.max_points_spinbox.setValue(1000)
        self.max_points_spinbox.setToolTip("Maximum data points to keep in memory")
        max_points_layout.addWidget(self.max_points_spinbox)
        max_points_layout.addStretch()
        layout.addRow("Max Points:", max_points_layout)
        
        # Update interval
        update_interval_layout = QHBoxLayout()
        self.update_interval_spinbox = QSpinBox()
        self.update_interval_spinbox.setMinimum(100)
        self.update_interval_spinbox.setMaximum(5000)
        self.update_interval_spinbox.setValue(500)
        self.update_interval_spinbox.setSuffix(" ms")
        update_interval_layout.addWidget(self.update_interval_spinbox)
        update_interval_layout.addStretch()
        layout.addRow("Update Interval:", update_interval_layout)
        
        main_layout.addLayout(layout)
        main_layout.addStretch()
        return widget
    
    def create_fields_tab(self) -> QWidget:
        """Create the Fields settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        layout = QFormLayout()
        
        # Primary field
        self.primary_field_combo = QComboBox()
        self.primary_field_combo.addItems(self.available_fields)
        layout.addRow("Primary Field:", self.primary_field_combo)
        
        # Primary unit
        self.primary_unit_edit = QLineEdit()
        self.primary_unit_edit.setPlaceholderText("e.g., m, knots, °")
        layout.addRow("Primary Unit:", self.primary_unit_edit)
        
        # Secondary field (optional)
        self.secondary_field_chk = QCheckBox("Enable Secondary Field")
        layout.addRow(self.secondary_field_chk)
        
        # Secondary field combo
        self.secondary_field_combo = QComboBox()
        self.secondary_field_combo.addItems(self.available_fields)
        self.secondary_field_combo.setEnabled(False)
        layout.addRow("Secondary Field:", self.secondary_field_combo)
        
        # Secondary unit
        self.secondary_unit_edit = QLineEdit()
        self.secondary_unit_edit.setPlaceholderText("e.g., m, knots, °")
        self.secondary_unit_edit.setEnabled(False)
        layout.addRow("Secondary Unit:", self.secondary_unit_edit)
        
        # Wire up secondary field checkbox
        self.secondary_field_chk.stateChanged.connect(
            lambda: self.secondary_field_combo.setEnabled(self.secondary_field_chk.isChecked())
        )
        self.secondary_field_chk.stateChanged.connect(
            lambda: self.secondary_unit_edit.setEnabled(self.secondary_field_chk.isChecked())
        )
        
        main_layout.addLayout(layout)
        main_layout.addStretch()
        return widget
    
    def create_style_tab(self) -> QWidget:
        """Create the Styling tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        layout = QFormLayout()
        
        # Line color
        line_color_layout = QHBoxLayout()
        self.line_color_label = QLabel("")
        self.line_color_label.setMinimumWidth(30)
        self.line_color_label.setMinimumHeight(30)
        self.line_color_label.setStyleSheet("background-color: #0066FF; border: 1px solid black;")
        self.line_color_btn = QPushButton("Choose...")
        self.line_color_btn.clicked.connect(self.choose_line_color)
        line_color_layout.addWidget(self.line_color_label)
        line_color_layout.addWidget(self.line_color_btn)
        line_color_layout.addStretch()
        layout.addRow("Line Color:", line_color_layout)
        
        # Line width
        self.line_width_spinbox = QSpinBox()
        self.line_width_spinbox.setMinimum(1)
        self.line_width_spinbox.setMaximum(5)
        self.line_width_spinbox.setValue(2)
        self.line_width_spinbox.setSuffix(" px")
        layout.addRow("Line Width:", self.line_width_spinbox)
        
        # Fill under line
        self.fill_under_line_chk = QCheckBox("Fill area under line")
        layout.addRow(self.fill_under_line_chk)
        
        # Marker style
        self.marker_style_combo = QComboBox()
        self.marker_style_combo.addItems(["none", "circle", "square", "diamond", "cross"])
        layout.addRow("Marker Style:", self.marker_style_combo)
        
        # Marker size
        self.marker_size_spinbox = QSpinBox()
        self.marker_size_spinbox.setMinimum(1)
        self.marker_size_spinbox.setMaximum(20)
        self.marker_size_spinbox.setValue(4)
        self.marker_size_spinbox.setSuffix(" px")
        layout.addRow("Marker Size:", self.marker_size_spinbox)
        
        # Secondary line color
        secondary_color_layout = QHBoxLayout()
        self.secondary_color_label = QLabel("")
        self.secondary_color_label.setMinimumWidth(30)
        self.secondary_color_label.setMinimumHeight(30)
        self.secondary_color_label.setStyleSheet("background-color: #FF6600; border: 1px solid black;")
        self.secondary_color_btn = QPushButton("Choose...")
        self.secondary_color_btn.clicked.connect(self.choose_secondary_color)
        secondary_color_layout.addWidget(self.secondary_color_label)
        secondary_color_layout.addWidget(self.secondary_color_btn)
        secondary_color_layout.addStretch()
        layout.addRow("Secondary Line Color:", secondary_color_layout)
        
        main_layout.addLayout(layout)
        main_layout.addStretch()
        return widget
    
    def create_scaling_tab(self) -> QWidget:
        """Create the Scaling settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        layout = QFormLayout()
        
        # Y-AXIS SCALING
        y_axis_label = QLabel("Y-Axis Scaling:")
        y_axis_label.setStyleSheet("font-weight: bold;")
        layout.addRow(y_axis_label)
        
        # Auto-scale Y-axis checkbox
        self.auto_scale_chk = QCheckBox("Auto-scale Y-axis")
        self.auto_scale_chk.setChecked(True)
        layout.addRow(self.auto_scale_chk)
        
        # Y-min
        self.y_min_spinbox = QDoubleSpinBox()
        self.y_min_spinbox.setMinimum(-100000)
        self.y_min_spinbox.setMaximum(100000)
        self.y_min_spinbox.setValue(0)
        self.y_min_spinbox.setEnabled(False)
        layout.addRow("Y-Min:", self.y_min_spinbox)
        
        # Y-max
        self.y_max_spinbox = QDoubleSpinBox()
        self.y_max_spinbox.setMinimum(-100000)
        self.y_max_spinbox.setMaximum(100000)
        self.y_max_spinbox.setValue(100)
        self.y_max_spinbox.setEnabled(False)
        layout.addRow("Y-Max:", self.y_max_spinbox)
        
        # X-AXIS SCALING (NEW)
        x_axis_label = QLabel("X-Axis Scaling (Time Window):")
        x_axis_label.setStyleSheet("font-weight: bold;")
        layout.addRow(x_axis_label)
        
        # Auto-scale X-axis checkbox
        self.x_axis_auto_scale_chk = QCheckBox("Auto-scale time window")
        self.x_axis_auto_scale_chk.setChecked(True)
        self.x_axis_auto_scale_chk.setToolTip("When enabled, the time window expands to fit all data.\n"
                                               "When disabled, the time window is fixed (see Basic tab).")
        layout.addRow(self.x_axis_auto_scale_chk)
        
        # Show grid
        self.show_grid_chk = QCheckBox("Show Grid")
        self.show_grid_chk.setChecked(True)
        layout.addRow(self.show_grid_chk)
        
        # Show legend
        self.show_legend_chk = QCheckBox("Show Legend")
        layout.addRow(self.show_legend_chk)
        
        # Wire up auto-scale checkboxes
        self.auto_scale_chk.stateChanged.connect(
            lambda: self.y_min_spinbox.setEnabled(not self.auto_scale_chk.isChecked())
        )
        self.auto_scale_chk.stateChanged.connect(
            lambda: self.y_max_spinbox.setEnabled(not self.auto_scale_chk.isChecked())
        )
        
        main_layout.addLayout(layout)
        main_layout.addStretch()
        return widget
    
    def create_advanced_tab(self) -> QWidget:
        """Create the Advanced settings tab."""
        widget = QWidget()
        main_layout = QVBoxLayout(widget)
        
        # Advanced group
        adv_group = QGroupBox("Advanced Options")
        adv_layout = QFormLayout(adv_group)
        
        # Show average line
        self.show_average_chk = QCheckBox("Show Rolling Average Line")
        adv_layout.addRow(self.show_average_chk)
        
        # Average window
        self.average_window_spinbox = QSpinBox()
        self.average_window_spinbox.setMinimum(10)
        self.average_window_spinbox.setMaximum(600)
        self.average_window_spinbox.setValue(60)
        self.average_window_spinbox.setSuffix(" seconds")
        self.average_window_spinbox.setEnabled(False)
        adv_layout.addRow("Average Window:", self.average_window_spinbox)
        
        self.show_average_chk.stateChanged.connect(
            lambda: self.average_window_spinbox.setEnabled(self.show_average_chk.isChecked())
        )
        
        # Alert on threshold
        self.alert_threshold_chk = QCheckBox("Enable Threshold Alerts")
        adv_layout.addRow(self.alert_threshold_chk)
        
        # Alert min
        self.alert_min_spinbox = QDoubleSpinBox()
        self.alert_min_spinbox.setMinimum(-100000)
        self.alert_min_spinbox.setMaximum(100000)
        self.alert_min_spinbox.setValue(0)
        self.alert_min_spinbox.setEnabled(False)
        adv_layout.addRow("Alert Min:", self.alert_min_spinbox)
        
        # Alert max
        self.alert_max_spinbox = QDoubleSpinBox()
        self.alert_max_spinbox.setMinimum(-100000)
        self.alert_max_spinbox.setMaximum(100000)
        self.alert_max_spinbox.setValue(100)
        self.alert_max_spinbox.setEnabled(False)
        adv_layout.addRow("Alert Max:", self.alert_max_spinbox)
        
        # Alert color
        alert_color_layout = QHBoxLayout()
        self.alert_color_label = QLabel("")
        self.alert_color_label.setMinimumWidth(30)
        self.alert_color_label.setMinimumHeight(30)
        self.alert_color_label.setStyleSheet("background-color: #FF0000; border: 1px solid black;")
        self.alert_color_btn = QPushButton("Choose...")
        self.alert_color_btn.clicked.connect(self.choose_alert_color)
        self.alert_color_btn.setEnabled(False)
        alert_color_layout.addWidget(self.alert_color_label)
        alert_color_layout.addWidget(self.alert_color_btn)
        alert_color_layout.addStretch()
        adv_layout.addRow("Alert Color:", alert_color_layout)
        
        self.alert_threshold_chk.stateChanged.connect(
            lambda: self.alert_min_spinbox.setEnabled(self.alert_threshold_chk.isChecked())
        )
        self.alert_threshold_chk.stateChanged.connect(
            lambda: self.alert_max_spinbox.setEnabled(self.alert_threshold_chk.isChecked())
        )
        self.alert_threshold_chk.stateChanged.connect(
            lambda: self.alert_color_btn.setEnabled(self.alert_threshold_chk.isChecked())
        )
        
        main_layout.addWidget(adv_group)
        main_layout.addStretch()
        return widget
    
    def choose_line_color(self):
        """Open color picker for line color."""
        color = QColorDialog.getColor(
            QColor(self.line_color_label.palette().color(self.line_color_label.backgroundRole())),
            self,
            "Choose Line Color"
        )
        if color.isValid():
            self.line_color_label.setStyleSheet(f"background-color: {color.name()}; border: 1px solid black;")
    
    def choose_secondary_color(self):
        """Open color picker for secondary line color."""
        color = QColorDialog.getColor(
            QColor(self.secondary_color_label.palette().color(self.secondary_color_label.backgroundRole())),
            self,
            "Choose Secondary Line Color"
        )
        if color.isValid():
            self.secondary_color_label.setStyleSheet(f"background-color: {color.name()}; border: 1px solid black;")
    
    def choose_alert_color(self):
        """Open color picker for alert color."""
        color = QColorDialog.getColor(
            QColor(self.alert_color_label.palette().color(self.alert_color_label.backgroundRole())),
            self,
            "Choose Alert Color"
        )
        if color.isValid():
            self.alert_color_label.setStyleSheet(f"background-color: {color.name()}; border: 1px solid black;")
    
    def validate_and_save(self):
        """Validate inputs and save configuration."""
        # Validation
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Plot name cannot be empty")
            return
        
        # Get field names
        primary_field = self.primary_field_combo.currentText()
        field_names = [primary_field]
        
        if self.secondary_field_chk.isChecked():
            secondary_field = self.secondary_field_combo.currentText()
            if secondary_field and secondary_field != primary_field:
                field_names.append(secondary_field)
        
        # Create config
        plot_id = self.existing_config.plot_id if self.is_editing else str(uuid.uuid4())
        
        config = PlotConfig(
            plot_id=plot_id,
            name=name,
            field_names=field_names,
            time_window=self.time_window_spinbox.value(),
            max_points=self.max_points_spinbox.value(),
            update_interval=self.update_interval_spinbox.value(),
            units=self.primary_unit_edit.text(),
            secondary_units=self.secondary_unit_edit.text() if self.secondary_field_chk.isChecked() else "",
            enabled=True,
            styling=PlotStyling(
                line_color=self.line_color_label.palette().color(self.line_color_label.backgroundRole()).name(),
                line_width=self.line_width_spinbox.value(),
                fill_under_line=self.fill_under_line_chk.isChecked(),
                marker_style=self.marker_style_combo.currentText(),
                marker_size=self.marker_size_spinbox.value(),
                secondary_line_color=self.secondary_color_label.palette().color(self.secondary_color_label.backgroundRole()).name(),
            ),
            axis_config=AxisConfig(
                auto_scale=self.auto_scale_chk.isChecked(),
                y_min=self.y_min_spinbox.value() if not self.auto_scale_chk.isChecked() else None,
                y_max=self.y_max_spinbox.value() if not self.auto_scale_chk.isChecked() else None,
                show_grid=self.show_grid_chk.isChecked(),
                show_legend=self.show_legend_chk.isChecked(),
                x_axis_auto_scale=self.x_axis_auto_scale_chk.isChecked(),
            ),
            advanced=AdvancedConfig(
                show_average_line=self.show_average_chk.isChecked(),
                average_window=self.average_window_spinbox.value(),
                alert_on_threshold=self.alert_threshold_chk.isChecked(),
                alert_min=self.alert_min_spinbox.value() if self.alert_threshold_chk.isChecked() else None,
                alert_max=self.alert_max_spinbox.value() if self.alert_threshold_chk.isChecked() else None,
                alert_color=self.alert_color_label.palette().color(self.alert_color_label.backgroundRole()).name(),
            ),
        )
        
        self.plot_configured.emit(config)
        self.accept()
    
    def load_config(self, config: PlotConfig):
        """Load an existing plot configuration into the dialog."""
        self.name_edit.setText(config.name)
        self.time_window_spinbox.setValue(config.time_window)
        self.max_points_spinbox.setValue(config.max_points)
        self.update_interval_spinbox.setValue(config.update_interval)
        
        # Fields
        if len(config.field_names) > 0:
            idx = self.primary_field_combo.findText(config.field_names[0])
            if idx >= 0:
                self.primary_field_combo.setCurrentIndex(idx)
        
        self.primary_unit_edit.setText(config.units)
        
        if len(config.field_names) > 1:
            self.secondary_field_chk.setChecked(True)
            idx = self.secondary_field_combo.findText(config.field_names[1])
            if idx >= 0:
                self.secondary_field_combo.setCurrentIndex(idx)
            self.secondary_unit_edit.setText(config.secondary_units)
        
        # Styling
        self.line_color_label.setStyleSheet(f"background-color: {config.styling.line_color}; border: 1px solid black;")
        self.line_width_spinbox.setValue(config.styling.line_width)
        self.fill_under_line_chk.setChecked(config.styling.fill_under_line)
        idx = self.marker_style_combo.findText(config.styling.marker_style)
        if idx >= 0:
            self.marker_style_combo.setCurrentIndex(idx)
        self.marker_size_spinbox.setValue(config.styling.marker_size)
        self.secondary_color_label.setStyleSheet(f"background-color: {config.styling.secondary_line_color}; border: 1px solid black;")
        
        # Scaling
        self.auto_scale_chk.setChecked(config.axis_config.auto_scale)
        if config.axis_config.y_min is not None:
            self.y_min_spinbox.setValue(config.axis_config.y_min)
        if config.axis_config.y_max is not None:
            self.y_max_spinbox.setValue(config.axis_config.y_max)
        self.show_grid_chk.setChecked(config.axis_config.show_grid)
        self.show_legend_chk.setChecked(config.axis_config.show_legend)
        self.x_axis_auto_scale_chk.setChecked(config.axis_config.x_axis_auto_scale)
        
        # Advanced
        self.show_average_chk.setChecked(config.advanced.show_average_line)
        self.average_window_spinbox.setValue(config.advanced.average_window)
        self.alert_threshold_chk.setChecked(config.advanced.alert_on_threshold)
        if config.advanced.alert_min is not None:
            self.alert_min_spinbox.setValue(config.advanced.alert_min)
        if config.advanced.alert_max is not None:
            self.alert_max_spinbox.setValue(config.advanced.alert_max)
        self.alert_color_label.setStyleSheet(f"background-color: {config.advanced.alert_color}; border: 1px solid black;")
