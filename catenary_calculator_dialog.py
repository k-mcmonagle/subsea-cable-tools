# -*- coding: utf-8 -*-
"""
Catenary Calculator Dialog for Subsea Cable Tools QGIS Plugin

This dialog provides a catenary calculator for subsea cables, including plotting and export features.
No extra dependencies required beyond QGIS (PyQt5, matplotlib, shapely for DXF export).
"""
from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QPushButton, QTextEdit, QWidget, QFormLayout, QSizePolicy, QFileDialog, QDoubleSpinBox)
from PyQt5.QtCore import Qt, QSettings
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import numpy as np
import math
import io

class CatenaryCalculatorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Subsea Cable Catenary Calculator")
        # Increased window size for better usability
        self.resize(1400, 900)
        self.setMinimumWidth(1200)
        self.setMinimumHeight(800)
        self.settings = QSettings("subsea_cable_tools", "CatenaryCalculator")
        self.init_ui()
        self.restore_user_settings()
        self.update_input_fields()
        self.update_plot()
    def closeEvent(self, event):
        self.save_user_settings()
        super().closeEvent(event)

    def save_user_settings(self):
        # Save persistent user input values
        self.settings.setValue("water_depth", self.water_depth.value())
        self.settings.setValue("weight_in_water", self.weight_in_water.value())
        self.settings.setValue("weight_unit", self.weight_unit.currentIndex())
        self.settings.setValue("input_parameter", self.input_parameter.currentIndex())
        self.settings.setValue("bottom_tension", self.bottom_tension.value())
        self.settings.setValue("top_tension", self.top_tension.value())
        self.settings.setValue("exit_angle", self.exit_angle.value())
        self.settings.setValue("angle_reference", self.angle_reference.currentIndex())
        self.settings.setValue("catenary_length", self.catenary_length.value())
        self.settings.setValue("layback", self.layback.value())

    def restore_user_settings(self):
        # Restore persistent user input values if available, otherwise keep defaults
        val = self.settings.value("water_depth")
        if val is not None:
            self.water_depth.setValue(float(val))
        val = self.settings.value("weight_in_water")
        if val is not None:
            self.weight_in_water.setValue(float(val))
        val = self.settings.value("weight_unit")
        if val is not None:
            self.weight_unit.setCurrentIndex(int(val))
        val = self.settings.value("input_parameter")
        if val is not None:
            self.input_parameter.setCurrentIndex(int(val))
        val = self.settings.value("bottom_tension")
        if val is not None:
            self.bottom_tension.setValue(float(val))
        val = self.settings.value("top_tension")
        if val is not None:
            self.top_tension.setValue(float(val))
        val = self.settings.value("exit_angle")
        if val is not None:
            self.exit_angle.setValue(float(val))
        val = self.settings.value("angle_reference")
        if val is not None:
            self.angle_reference.setCurrentIndex(int(val))
        val = self.settings.value("catenary_length")
        if val is not None:
            self.catenary_length.setValue(float(val))
        val = self.settings.value("layback")
        if val is not None:
            self.layback.setValue(float(val))

    def init_ui(self):
        main_layout = QHBoxLayout(self)
        # Input section
        input_widget = QWidget()
        input_layout = QFormLayout(input_widget)
        input_widget.setMinimumWidth(320)
        self.water_depth = QDoubleSpinBox()
        self.water_depth.setRange(0, 1e6)
        self.water_depth.setDecimals(0)
        self.water_depth.setValue(100)
        self.weight_in_water = QDoubleSpinBox()
        self.weight_in_water.setRange(0, 1e5)
        self.weight_in_water.setDecimals(2)
        self.weight_in_water.setValue(22)
        self.weight_unit = QComboBox()
        self.weight_unit.addItems(["N/m", "kg/m", "lbf/ft"])
        self.input_parameter = QComboBox()
        self.input_parameter.addItems([
            "Bottom Tension", "Top Tension", "Exit Angle", "Catenary Length", "Layback"
        ])
        self.bottom_tension = QDoubleSpinBox()
        self.bottom_tension.setRange(0, 1e5)
        self.bottom_tension.setDecimals(2)
        self.bottom_tension.setValue(5)
        self.top_tension = QDoubleSpinBox()
        self.top_tension.setRange(0, 1e5)
        self.top_tension.setDecimals(2)
        self.top_tension.setValue(7)
        self.exit_angle = QDoubleSpinBox()
        self.exit_angle.setRange(0, 90)
        self.exit_angle.setDecimals(2)
        self.exit_angle.setValue(25)
        self.angle_reference = QComboBox()
        self.angle_reference.addItems(["from horizontal", "from vertical"])
        self.catenary_length = QDoubleSpinBox()
        self.catenary_length.setRange(0, 1e6)
        self.catenary_length.setDecimals(2)
        self.catenary_length.setValue(230)
        self.layback = QDoubleSpinBox()
        self.layback.setRange(0, 1e6)
        self.layback.setDecimals(2)
        self.layback.setValue(50)
        self.layback.setDisabled(True)
        input_layout.addRow(QLabel("<b>Input</b>"))
        input_layout.addRow("Water Depth (m):", self.water_depth)
        weight_layout = QHBoxLayout()
        weight_layout.addWidget(self.weight_in_water)
        weight_layout.addWidget(self.weight_unit)
        input_layout.addRow("Weight in Water:", weight_layout)
        input_layout.addRow("Select Input Parameter:", self.input_parameter)
        input_layout.addRow("Bottom Tension (kN):", self.bottom_tension)
        input_layout.addRow("Top Tension (kN):", self.top_tension)
        angle_layout = QHBoxLayout()
        angle_layout.addWidget(self.exit_angle)
        angle_layout.addWidget(self.angle_reference)
        input_layout.addRow("Exit Angle:", angle_layout)
        input_layout.addRow("Catenary Length (m):", self.catenary_length)
        input_layout.addRow("Layback Distance (m):", self.layback)
        note = QLabel("""<i>Note: This tool calculates the approximate catenary shape of a cable between sea level and the seabed. It models only the submerged portion of the cable and does not account for the in-air segment between sea level and the overboarding point or friction at the overboarding point. Please consider the results as estimations, suitable for preliminary assessments rather than detailed engineering designs.</i>""")
        note.setWordWrap(True)
        input_layout.addRow(note)
        main_layout.addWidget(input_widget)
        # Output section
        output_widget = QWidget()
        output_layout = QVBoxLayout(output_widget)
        output_layout.addWidget(QLabel("<b>Results</b>"))
        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setMinimumHeight(220)  # Increased height for better visibility
        output_layout.addWidget(self.results)
        self.figure = Figure(figsize=(5, 4))
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        output_layout.addWidget(self.canvas, stretch=1)
        button_layout = QHBoxLayout()
        self.export_svg_btn = QPushButton("Export SVG")
        self.export_dxf_btn = QPushButton("Export DXF")
        button_layout.addWidget(self.export_svg_btn)
        button_layout.addWidget(self.export_dxf_btn)
        output_layout.addLayout(button_layout)
        main_layout.addWidget(output_widget, stretch=1)
        # Connect signals
        for widget in [self.water_depth, self.weight_in_water, self.bottom_tension, self.top_tension, self.exit_angle, self.catenary_length, self.layback]:
            widget.valueChanged.connect(self.update_plot)
        self.weight_unit.currentIndexChanged.connect(self.update_plot)
        self.angle_reference.currentIndexChanged.connect(self.on_angle_reference_changed)
        self.input_parameter.currentIndexChanged.connect(self.update_input_fields)
        self.input_parameter.currentIndexChanged.connect(self.update_plot)
        self.export_svg_btn.clicked.connect(self.export_svg)
        self.export_dxf_btn.clicked.connect(self.export_dxf)

    def on_angle_reference_changed(self):
        # Always keep exit_angle value in sync with reference
        self._sync_exit_angle_with_reference()
        self.update_plot()

    def _sync_exit_angle_with_reference(self):
        # Always update exit_angle value to match the current reference
        curr_ref = self.angle_reference.currentIndex()
        prev_ref = getattr(self, '_prev_angle_ref', curr_ref)
        if prev_ref != curr_ref:
            val = self.exit_angle.value()
            new_val = 90 - val
            self.exit_angle.blockSignals(True)
            self.exit_angle.setValue(new_val)
            self.exit_angle.blockSignals(False)
        self._prev_angle_ref = curr_ref

    def showEvent(self, event):
        # Initialize the previous angle reference index
        self._prev_angle_ref = self.angle_reference.currentIndex()
        super().showEvent(event)

    def update_input_fields(self):
        param = self.input_parameter.currentText()
        self.bottom_tension.setDisabled(True)
        self.top_tension.setDisabled(True)
        self.exit_angle.setDisabled(True)
        self.catenary_length.setDisabled(True)
        self.layback.setDisabled(True)
        # Always enable angle_reference
        self.angle_reference.setDisabled(False)
        if param == "Bottom Tension":
            self.bottom_tension.setDisabled(False)
        elif param == "Top Tension":
            self.top_tension.setDisabled(False)
        elif param == "Exit Angle":
            self.exit_angle.setDisabled(False)
        elif param == "Catenary Length":
            self.catenary_length.setDisabled(False)
        elif param == "Layback":
            self.layback.setDisabled(False)
        # Always keep exit_angle value in sync with reference
        self._sync_exit_angle_with_reference()

    def get_config(self):
        try:
            param = self.input_parameter.currentText()
            # Helper for empty check
            def check_empty(val, label):
                if val.strip() == '':
                    raise ValueError(f"Please enter {label}.")
                return val

            # Check and parse all required fields
            water_depth = self.water_depth.value()
            check_empty(str(water_depth), "Water Depth")

            weight_val = self.weight_in_water.value()
            check_empty(str(weight_val), "Weight in Water")
            unit = self.weight_unit.currentText()
            if unit == "N/m":
                weight_n_per_m = weight_val
            elif unit == "kg/m":
                weight_n_per_m = weight_val * 9.80665
            elif unit == "lbf/ft":
                weight_n_per_m = weight_val * 14.5939
            else:
                raise ValueError("Unknown weight unit")

            config = {
                'waterDepth': water_depth,
                'weightInWater': weight_n_per_m,
                'inputParameter': param
            }

            if param == 'Bottom Tension':
                val = self.bottom_tension.value()
                check_empty(str(val), "Bottom Tension")
                config['bottomTension'] = val
            elif param == 'Top Tension':
                val = self.top_tension.value()
                check_empty(str(val), "Top Tension")
                config['topTension'] = val
            elif param == 'Exit Angle':
                val = self.exit_angle.value()
                check_empty(str(val), "Exit Angle")
                angle_val = val
                # Convert to degrees from horizontal if user entered from vertical
                if self.angle_reference.currentText() == "from vertical":
                    config['exitAngle'] = 90 - angle_val
                else:
                    config['exitAngle'] = angle_val
            elif param == 'Catenary Length':
                val = self.catenary_length.value()
                check_empty(str(val), "Catenary Length")
                config['catenaryLength'] = val
            elif param == 'Layback':
                val = self.layback.value()
                check_empty(str(val), "Layback Distance")
                config['layback'] = val

            # Validation
            if config['waterDepth'] <= 0:
                raise ValueError('Water Depth must be > 0')
            if weight_n_per_m <= 0:
                raise ValueError('Weight in Water must be > 0')
            if 'layback' in config and config['layback'] <= 0:
                raise ValueError('Layback Distance must be > 0')
            return config
        except Exception as e:
            self.results.setHtml(f'<span style="color:red;">{e}</span>')
            return None

    def update_plot(self):
        config = self.get_config()
        if not config:
            self.figure.clear()
            self.canvas.draw()
            return
        try:
            calc = CatenaryCalculator(config)
            calc.calculate()
            x, y = calc.get_catenary_shape()
            min_radius = calc.calculate_minimum_radius(x, y)
            self.update_calculated_fields(config['inputParameter'], calc)
            self.display_results(calc, min_radius)
            self.plot_catenary(x, y, config, calc.xDeck)
            self._last_calc = calc
        except Exception as e:
            msg = str(e)
            if 'Function does not change sign over interval' in msg:
                msg = (
                    'The input values are not possible. For example, the Top Tension is too low for the given water depth and cable weight. '
                    'Please increase the Top Tension or adjust other parameters.'
                )
            self.results.setHtml(f'<span style="color:red;">Error: {msg}</span>')
            self.figure.clear()
            self.canvas.draw()

    def update_calculated_fields(self, input_param, calc):
        if input_param != 'Bottom Tension':
            self.bottom_tension.blockSignals(True)
            self.bottom_tension.setValue(calc.bottomTension)
            self.bottom_tension.blockSignals(False)
        if input_param != 'Top Tension':
            self.top_tension.blockSignals(True)
            self.top_tension.setValue(calc.topTension)
            self.top_tension.blockSignals(False)
        if input_param != 'Catenary Length':
            self.catenary_length.blockSignals(True)
            self.catenary_length.setValue(calc.catenaryLength)
            self.catenary_length.blockSignals(False)
        if input_param != 'Layback':
            self.layback.blockSignals(True)
            self.layback.setValue(calc.xDeck)
            self.layback.blockSignals(False)
        # Always update exit_angle to the calculated value, adjusted for the current reference
        self.exit_angle.blockSignals(True)
        if self.angle_reference.currentText() == "from horizontal":
            self.exit_angle.setValue(calc.exitAngle)
        else:
            self.exit_angle.setValue(90 - calc.exitAngle)
        self.exit_angle.blockSignals(False)

    def display_results(self, calc, min_radius):
        flop_forward = calc.catenaryLength - calc.xDeck
        angle_from_vertical = 90 - calc.exitAngle
        # Show both references in results
        text = (
            f"Bottom Tension: {calc.bottomTension:.2f} kN<br>"
            f"Top Tension: {calc.topTension:.2f} kN<br>"
            f"Exit Angle: {calc.exitAngle:.2f}° from horizontal / {angle_from_vertical:.2f}° from vertical<br>"
            f"Catenary Length: {calc.catenaryLength:.2f} m<br>"
            f"Layback Distance: {calc.xDeck:.2f} m<br>"
            f"Flop Forward: {flop_forward:.2f} m<br>"
            f"Minimum Radius of Curvature: {min_radius:.2f} m<br>"
        )
        self.results.setHtml(text)

    def plot_catenary(self, x, y, config, xDeck):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        y_plot = [config['waterDepth'] - yi for yi in y]
        ax.plot(x, y_plot, label="Cable", color="blue")
        ax.plot([0, xDeck], [config['waterDepth'], config['waterDepth']], color="brown", label="Seabed", linewidth=2)
        ax.plot([0, xDeck], [0, 0], color="lightblue", label="Sea Level", linewidth=2)
        ax.set_xlabel("Horizontal Distance (m)")
        ax.set_ylabel("Depth (m)")
        ax.set_title("Cable Catenary")
        ax.set_xlim(0, max(x) * 1.1)
        ax.set_ylim(0, config['waterDepth'] * 1.1)
        ax.invert_yaxis()
        ax.set_aspect('equal', adjustable='datalim')
        ax.legend()
        self.figure.tight_layout()
        self.canvas.draw()

    def export_svg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save SVG", "catenary_plot.svg", "SVG Files (*.svg)")
        if path:
            self.figure.savefig(path, format='svg')

    def export_dxf(self):
        try:
            from shapely.geometry import LineString
        except ImportError:
            self.results.setHtml('<span style="color:red;">Shapely is required for DXF export (should be available in QGIS).</span>')
            return
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "Save DXF", "catenary.dxf", "DXF Files (*.dxf)")
        if not path:
            return
        calc = getattr(self, '_last_calc', None)
        if not calc:
            self.results.setHtml('<span style="color:red;">No catenary data to export.</span>')
            return
        x, y = calc.get_catenary_shape()
        # Scale to mm for DXF
        x_mm = [xi * 1000 for xi in x]
        y_mm = [yi * 1000 for yi in y]
        # Create DXF content
        dxf = self.generate_dxf(x_mm, y_mm)
        with open(path, 'w') as f:
            f.write(dxf)

    def generate_dxf(self, x, y):
        dxf = '0\nSECTION\n2\nENTITIES\n0\nPOLYLINE\n8\n0\n66\n1\n70\n0\n'
        for xi, yi in zip(x, y):
            dxf += f'0\nVERTEX\n8\n0\n10\n{xi}\n20\n{yi}\n30\n0.0\n'
        dxf += '0\nSEQEND\n0\nENDSEC\n0\nEOF\n'
        return dxf

class CatenaryCalculator:
    def __init__(self, config):
        self.config = config
        self.bottomTension = None
        self.topTension = None
        self.exitAngle = None
        self.catenaryLength = None
        self.xDeck = None

    def calculate(self):
        q = self.config['weightInWater']
        totalHeight = self.config['waterDepth']
        param = self.config['inputParameter']
        if param == 'Bottom Tension':
            H = self.config['bottomTension'] * 1000
            self._from_bottom_tension(H, q, totalHeight)
        elif param == 'Top Tension':
            Ts = self.config['topTension'] * 1000
            self._from_top_tension(Ts, q, totalHeight)
        elif param == 'Exit Angle':
            angleRad = math.radians(self.config['exitAngle'])
            self._from_exit_angle(angleRad, q, totalHeight)
        elif param == 'Catenary Length':
            L = self.config['catenaryLength']
            self._from_catenary_length(L, q, totalHeight)
        elif param == 'Layback':
            xDeck = self.config['layback']
            self._from_layback(xDeck, q, totalHeight)
        else:
            raise ValueError('Invalid input parameter')

    def _from_bottom_tension(self, H, q, totalHeight):
        self.xDeck = (H / q) * math.acosh((q * totalHeight / H) + 1)
        self.catenaryLength = (H / q) * math.sinh(q * self.xDeck / H)
        self.exitAngle = math.degrees(math.atan((q * self.catenaryLength) / H))
        self.topTension = math.sqrt(H ** 2 + (q * self.catenaryLength) ** 2) / 1000
        self.bottomTension = H / 1000

    def _from_top_tension(self, Ts_N, q, totalHeight):
        def to_solve(H):
            xDeck = (H / q) * math.acosh((q * totalHeight / H) + 1)
            s = (H / q) * math.sinh(q * xDeck / H)
            return H ** 2 + (q * s) ** 2 - Ts_N ** 2
        H = self._find_root_bisection(to_solve, 1e-3, Ts_N)
        self._from_bottom_tension(H, q, totalHeight)

    def _from_exit_angle(self, angleRad, q, totalHeight):
        cosTheta = math.cos(angleRad)
        if cosTheta >= 1.0:
            raise ValueError('Exit angle must be > 0 degrees')
        if cosTheta <= 0.0:
            raise ValueError('Exit angle must be < 90 degrees')
        H = (q * totalHeight * cosTheta) / (1 - cosTheta)
        self._from_bottom_tension(H, q, totalHeight)

    def _from_catenary_length(self, S, q, totalHeight):
        if S <= totalHeight:
            raise ValueError('Catenary length must be > water depth')
        def xDeck_func(H):
            return (H / q) * math.acosh((q * totalHeight / H) + 1)
        def to_solve(H):
            xDeck = xDeck_func(H)
            return (H / q) * math.sinh(q * xDeck / H) - S
        H = self._find_root_bisection(to_solve, 1e-3, q * S)
        self._from_bottom_tension(H, q, totalHeight)

    def _from_layback(self, xDeck, q, totalHeight):
        def to_solve(H):
            return (H / q) * math.acosh((q * totalHeight / H) + 1) - xDeck
        H = self._find_root_bisection(to_solve, 1e-3, q * totalHeight * 100)
        self._from_bottom_tension(H, q, totalHeight)

    def _find_root_bisection(self, func, lower, upper, tol=1e-7, max_iter=100):
        a, b = lower, upper
        fa, fb = func(a), func(b)
        if fa * fb > 0:
            raise ValueError('Function does not change sign over interval')
        for _ in range(max_iter):
            c = (a + b) / 2
            fc = func(c)
            if abs(fc) < tol or (b - a) / 2 < tol:
                return c
            if fa * fc < 0:
                b, fb = c, fc
            else:
                a, fa = c, fc
        raise ValueError('Root finding did not converge')

    def get_catenary_shape(self, num_points=100):
        H = self.bottomTension * 1000
        q = self.config['weightInWater']
        x = np.linspace(0, self.xDeck, num_points + 1)
        y = (H / q) * (np.cosh((q * x) / H) - 1)
        return x, y

    def calculate_minimum_radius(self, x, y):
        dx = np.gradient(x)
        dy = np.gradient(y)
        ddx = np.gradient(dx)
        ddy = np.gradient(dy)
        curvature = np.abs(dx * ddy - dy * ddx) / np.power(dx * dx + dy * dy, 1.5)
        max_curv = np.max(curvature)
        if max_curv == 0:
            return float('inf')
        return 1 / max_curv