# -*- coding: utf-8 -*-
"""Beta 3D catenary / multi-span viewer for Catenary Calculator V2.

This dialog is intentionally separate from the production V2 dialog while the 3D
and multi-span workflow is being proven.  It uses the pure-Python backend in
``catenary_3d.py`` and pyqtgraph's OpenGL widget when available.  If PyOpenGL is
not installed in the QGIS Python environment, the dialog opens with a clear
message instead of breaking plugin startup.
"""

from __future__ import annotations

import csv
from typing import List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

# Importing plot_widget first ensures the plugin's vendored pyqtgraph path has
# been initialised before we try to import pyqtgraph.opengl.
try:  # pragma: no cover - depends on QGIS runtime packaging
    from .. import plot_widget as _plot_widget  # noqa: F401
except Exception:
    pass

_GL_IMPORT_ERROR: Optional[Exception] = None
try:  # pragma: no cover - exercised in QGIS rather than unit tests
    import pyqtgraph as pg
    import pyqtgraph.opengl as gl
except Exception as exc:  # pragma: no cover
    pg = None
    gl = None
    _GL_IMPORT_ERROR = exc

from .catenary_3d import (
    BodySpanConnection,
    Point3D,
    SpanSolution3D,
    chute_friction_bounds,
    seabed_contact_report,
    solve_body_equilibrium,
    solve_uniform_catenary_span_3d,
)


_ORIENTATION = getattr(Qt, "Orientation", Qt)
_TEXT_FORMAT = getattr(Qt, "TextFormat", Qt)


class Catenary3DBetaDialog(QDialog):
    """Interactive beta 3D catenary and point-body multi-span viewer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Catenary V2 3D Beta")
        self.resize(1280, 820)
        self.setMinimumSize(900, 620)

        self._gl_items = []
        self._last_rows: List[Tuple[str, float, float, float, float, float]] = []
        self._last_body_result = None

        self._init_ui()
        self._update_gl_available_state()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _spin(
        value: float,
        minimum: float = -1e6,
        maximum: float = 1e6,
        decimals: int = 2,
        step: float = 1.0,
        suffix: str = "",
    ) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(float(minimum), float(maximum))
        w.setDecimals(int(decimals))
        w.setSingleStep(float(step))
        w.setValue(float(value))
        if suffix:
            w.setSuffix(suffix)
        return w

    @staticmethod
    def _point(x: QDoubleSpinBox, y: QDoubleSpinBox, z: QDoubleSpinBox) -> Point3D:
        return Point3D(float(x.value()), float(y.value()), float(z.value()))

    def _init_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(_ORIENTATION.Horizontal)
        root.addWidget(splitter)

        input_widget = QWidget()
        input_layout = QVBoxLayout(input_widget)
        input_layout.setContentsMargins(8, 8, 8, 8)

        input_scroll = QScrollArea()
        input_scroll.setWidgetResizable(True)
        input_scroll.setMinimumWidth(430)
        input_scroll.setMaximumWidth(560)
        input_scroll.setWidget(input_widget)
        splitter.addWidget(input_scroll)

        title = QLabel(
            "<b>Catenary V2 3D Beta</b><br>"
            "Static/quasi-static 3D plotting and point-body multi-span equilibrium. "
            "Currents, drag and time-domain lay transients are intentionally left for the future Cable Lay Simulator."
        )
        title.setTextFormat(_TEXT_FORMAT.RichText)
        title.setWordWrap(True)
        input_layout.addWidget(title)

        self.tabs = QTabWidget()
        input_layout.addWidget(self.tabs)

        self._init_single_span_tab()
        self._init_body_tab()
        self._init_chute_tab()

        display_group = QWidget()
        display_layout = QFormLayout(display_group)
        self.seabed_depth = self._spin(100.0, 0.0, 1e6, 1, 10.0, " m")
        self.show_seabed_grid = QCheckBox("Show seabed grid")
        self.show_seabed_grid.setChecked(True)
        self.show_sea_surface_grid = QCheckBox("Show sea-surface grid")
        self.show_sea_surface_grid.setChecked(True)
        display_layout.addRow("Reference seabed depth:", self.seabed_depth)
        display_layout.addRow("", self.show_seabed_grid)
        display_layout.addRow("", self.show_sea_surface_grid)
        input_layout.addWidget(QLabel("<b>3D display / contact reference</b>"))
        input_layout.addWidget(display_group)

        button_row = QHBoxLayout()
        self.solve_single_btn = QPushButton("Plot single span")
        self.solve_body_btn = QPushButton("Solve body / multi-span")
        button_row.addWidget(self.solve_single_btn)
        button_row.addWidget(self.solve_body_btn)
        input_layout.addLayout(button_row)

        button_row_2 = QHBoxLayout()
        self.reset_view_btn = QPushButton("Reset 3D view")
        self.export_csv_btn = QPushButton("Export 3D CSV…")
        button_row_2.addWidget(self.reset_view_btn)
        button_row_2.addWidget(self.export_csv_btn)
        input_layout.addLayout(button_row_2)

        note = QLabel(
            "<i>3D controls: left-drag rotates, wheel zooms, and right/middle drag pans "
            "in pyqtgraph's OpenGL view. If the view area is replaced by a dependency warning, "
            "install/enable PyOpenGL in the QGIS Python environment.</i>"
        )
        note.setWordWrap(True)
        input_layout.addWidget(note)
        input_layout.addStretch(1)

        output_widget = QWidget()
        output_layout = QVBoxLayout(output_widget)
        output_layout.setContentsMargins(8, 8, 8, 8)

        self.viewer_container = QWidget()
        viewer_layout = QVBoxLayout(self.viewer_container)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        if gl is not None:
            self.gl_view = gl.GLViewWidget()
            self.gl_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            viewer_layout.addWidget(self.gl_view)
        else:
            self.gl_view = None
            self.gl_warning = QTextEdit()
            self.gl_warning.setReadOnly(True)
            self.gl_warning.setHtml(
                "<h3>3D OpenGL viewer unavailable</h3>"
                "<p>pyqtgraph is present, but <b>pyqtgraph.opengl</b> could not be imported. "
                "This usually means PyOpenGL is missing from the QGIS Python environment.</p>"
                f"<p><b>Details:</b> {str(_GL_IMPORT_ERROR)}</p>"
            )
            viewer_layout.addWidget(self.gl_warning)
        output_layout.addWidget(self.viewer_container, stretch=1)

        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setMinimumHeight(170)
        output_layout.addWidget(self.results)

        splitter.addWidget(output_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([480, 820])

        self.solve_single_btn.clicked.connect(self.plot_single_span)
        self.solve_body_btn.clicked.connect(self.solve_body_multispan)
        self.reset_view_btn.clicked.connect(self.reset_view)
        self.export_csv_btn.clicked.connect(self.export_csv)
        self.seabed_depth.valueChanged.connect(self._replot_last_if_possible)
        self.show_seabed_grid.toggled.connect(self._replot_last_if_possible)
        self.show_sea_surface_grid.toggled.connect(self._replot_last_if_possible)

    def _init_single_span_tab(self) -> None:
        tab = QWidget()
        form = QFormLayout(tab)

        self.single_start_x = self._spin(0.0, suffix=" m")
        self.single_start_y = self._spin(0.0, suffix=" m")
        self.single_start_z = self._spin(-100.0, suffix=" m")
        self.single_end_x = self._spin(150.0, suffix=" m")
        self.single_end_y = self._spin(0.0, suffix=" m")
        self.single_end_z = self._spin(5.0, suffix=" m")
        self.single_length = self._spin(210.0, 0.001, 1e7, 2, 1.0, " m")
        self.single_q = self._spin(22.0, 0.001, 1e6, 3, 1.0, " N/m")
        self.single_samples = QSpinBox()
        self.single_samples.setRange(10, 5000)
        self.single_samples.setValue(180)

        form.addRow(QLabel("<b>Start / lower endpoint</b>"))
        form.addRow("Start east/local X:", self.single_start_x)
        form.addRow("Start north/local Y:", self.single_start_y)
        form.addRow("Start elevation Z:", self.single_start_z)
        form.addRow(QLabel("<b>End / upper endpoint</b>"))
        form.addRow("End east/local X:", self.single_end_x)
        form.addRow("End north/local Y:", self.single_end_y)
        form.addRow("End elevation Z:", self.single_end_z)
        form.addRow(QLabel("<b>Span properties</b>"))
        form.addRow("Arc length:", self.single_length)
        form.addRow("Effective weight:", self.single_q)
        form.addRow("Samples:", self.single_samples)

        self.tabs.addTab(tab, "Single span")

    def _init_body_tab(self) -> None:
        tab = QWidget()
        form = QFormLayout(tab)

        self.body_init_x = self._spin(0.0, suffix=" m")
        self.body_init_y = self._spin(45.0, suffix=" m")
        self.body_init_z = self._spin(-50.0, suffix=" m")
        self.body_load = self._spin(-3.0, -1e6, 1e6, 3, 0.5, " kN")
        self.body_tol = self._spin(25.0, 0.001, 1e6, 2, 5.0, " N")

        self.top_x = self._spin(0.0, suffix=" m")
        self.top_y = self._spin(0.0, suffix=" m")
        self.top_z = self._spin(5.0, suffix=" m")
        self.top_len = self._spin(85.0, 0.001, 1e7, 2, 1.0, " m")
        self.top_q = self._spin(20.0, 0.001, 1e6, 3, 1.0, " N/m")

        self.port_x = self._spin(-45.0, suffix=" m")
        self.port_y = self._spin(85.0, suffix=" m")
        self.port_z = self._spin(-100.0, suffix=" m")
        self.port_len = self._spin(125.0, 0.001, 1e7, 2, 1.0, " m")
        self.port_q = self._spin(22.0, 0.001, 1e6, 3, 1.0, " N/m")

        self.stbd_x = self._spin(45.0, suffix=" m")
        self.stbd_y = self._spin(85.0, suffix=" m")
        self.stbd_z = self._spin(-100.0, suffix=" m")
        self.stbd_len = self._spin(125.0, 0.001, 1e7, 2, 1.0, " m")
        self.stbd_q = self._spin(22.0, 0.001, 1e6, 3, 1.0, " N/m")

        form.addRow(QLabel("<b>Body initial guess / load</b>"))
        form.addRow("Body initial X:", self.body_init_x)
        form.addRow("Body initial Y:", self.body_init_y)
        form.addRow("Body initial Z:", self.body_init_z)
        form.addRow("Net submerged body load:", self.body_load)
        form.addRow("Residual tolerance:", self.body_tol)

        form.addRow(QLabel("<b>Upper line: ship/chute to body</b>"))
        form.addRow("Top fixed X:", self.top_x)
        form.addRow("Top fixed Y:", self.top_y)
        form.addRow("Top fixed Z:", self.top_z)
        form.addRow("Upper span length:", self.top_len)
        form.addRow("Upper span weight:", self.top_q)

        form.addRow(QLabel("<b>Lower leg A: body to seabed/support</b>"))
        form.addRow("Leg A fixed X:", self.port_x)
        form.addRow("Leg A fixed Y:", self.port_y)
        form.addRow("Leg A fixed Z:", self.port_z)
        form.addRow("Leg A length:", self.port_len)
        form.addRow("Leg A weight:", self.port_q)

        form.addRow(QLabel("<b>Lower leg B: body to seabed/support</b>"))
        form.addRow("Leg B fixed X:", self.stbd_x)
        form.addRow("Leg B fixed Y:", self.stbd_y)
        form.addRow("Leg B fixed Z:", self.stbd_z)
        form.addRow("Leg B length:", self.stbd_len)
        form.addRow("Leg B weight:", self.stbd_q)

        self.tabs.addTab(tab, "Body / multi-span")

    def _init_chute_tab(self) -> None:
        tab = QWidget()
        form = QFormLayout(tab)
        self.chute_contact_tension = self._spin(50.0, 0.0, 1e6, 2, 1.0, " kN")
        self.chute_wrap = self._spin(45.0, 0.0, 360.0, 2, 5.0, "°")
        self.chute_mu = self._spin(0.10, 0.0, 10.0, 3, 0.01, "")
        self.chute_btn = QPushButton("Calculate chute friction bounds")
        form.addRow("Contact tension:", self.chute_contact_tension)
        form.addRow("Wrap/contact angle:", self.chute_wrap)
        form.addRow("Friction coefficient μ:", self.chute_mu)
        form.addRow("", self.chute_btn)
        self.chute_btn.clicked.connect(self.calculate_chute_friction)
        self.tabs.addTab(tab, "Chute friction")

    def _update_gl_available_state(self) -> None:
        available = self.gl_view is not None and gl is not None and np is not None
        self.solve_single_btn.setEnabled(available)
        self.solve_body_btn.setEnabled(available)
        self.reset_view_btn.setEnabled(available)
        if np is None:
            self.results.setHtml("<span style='color:red;'>NumPy is required for the 3D viewer.</span>")
        elif not available:
            self.results.setHtml(
                "<span style='color:red;'>3D OpenGL viewer unavailable. "
                "Install/enable PyOpenGL for the QGIS Python environment to use orbit/pan/zoom.</span>"
            )
        else:
            self.results.setHtml("Ready. Plot a single span or solve the body/multi-span example.")

    # ------------------------------------------------------------------
    # Solvers / plotting
    # ------------------------------------------------------------------

    def plot_single_span(self) -> None:
        try:
            sol = solve_uniform_catenary_span_3d(
                name="Single span",
                start=self._point(self.single_start_x, self.single_start_y, self.single_start_z),
                end=self._point(self.single_end_x, self.single_end_y, self.single_end_z),
                length_m=float(self.single_length.value()),
                q_npm=float(self.single_q.value()),
                samples=int(self.single_samples.value()),
            )
            self._last_body_result = None
            self._plot_spans([sol], body_point=None)
            contact = self._combined_contact_report([sol])
            self._display_single_results(sol, contact)
        except Exception as exc:
            self._show_error("Single-span solve failed", exc)

    def solve_body_multispan(self) -> None:
        try:
            spans = [
                BodySpanConnection(
                    "Upper line to ship/chute",
                    self._point(self.top_x, self.top_y, self.top_z),
                    float(self.top_len.value()),
                    float(self.top_q.value()),
                ),
                BodySpanConnection(
                    "Lower leg A",
                    self._point(self.port_x, self.port_y, self.port_z),
                    float(self.port_len.value()),
                    float(self.port_q.value()),
                ),
                BodySpanConnection(
                    "Lower leg B",
                    self._point(self.stbd_x, self.stbd_y, self.stbd_z),
                    float(self.stbd_len.value()),
                    float(self.stbd_q.value()),
                ),
            ]
            result = solve_body_equilibrium(
                initial_body_position=self._point(self.body_init_x, self.body_init_y, self.body_init_z),
                spans=spans,
                submerged_weight_N=float(self.body_load.value()) * 1000.0,
                tolerance_N=float(self.body_tol.value()),
                max_iterations=35,
                finite_difference_step_m=0.25,
                samples_per_span=160,
            )
            self._last_body_result = result
            self._plot_spans(result.span_solutions, body_point=result.body_position)
            contact = self._combined_contact_report(result.span_solutions)
            self._display_body_results(result, contact)
        except Exception as exc:
            self._show_error("Body / multi-span solve failed", exc)

    def calculate_chute_friction(self) -> None:
        result = chute_friction_bounds(
            contact_tension_kN=float(self.chute_contact_tension.value()),
            wrap_angle_deg=float(self.chute_wrap.value()),
            friction_coefficient=float(self.chute_mu.value()),
        )
        self.results.setHtml(
            "<b>Chute friction bounds</b><br>"
            f"Contact tension: {result.contact_tension_kN:.2f} kN<br>"
            f"Wrap angle: {result.wrap_angle_deg:.2f}°<br>"
            f"Friction coefficient μ: {result.friction_coefficient:.3f}<br>"
            f"Capstan ratio e^(μθ): {result.capstan_ratio:.4f}<br><br>"
            "Depending on sliding direction:<br>"
            f"• Top/tensioner side high: {result.top_tension_if_top_side_high_kN:.2f} kN<br>"
            f"• Top/tensioner side low: {result.top_tension_if_top_side_low_kN:.2f} kN<br>"
            f"• Full high-low difference: {result.tension_difference_kN:.2f} kN"
        )

    def _combined_contact_report(self, spans: Sequence[SpanSolution3D]):
        points = []
        for sol in spans:
            points.extend(sol.points)
        if not points:
            return None
        return seabed_contact_report(
            points,
            seabed_depth_at_xy=lambda x, y: float(self.seabed_depth.value()),
            tolerance_m=0.25,
            penetration_tolerance_m=0.50,
        )

    def _plot_spans(self, spans: Sequence[SpanSolution3D], body_point: Optional[Point3D]) -> None:
        if self.gl_view is None or gl is None or np is None:
            return
        self._clear_gl()
        self._last_rows = []

        all_points: List[Point3D] = []
        for span_index, sol in enumerate(spans):
            all_points.extend(sol.points)
            pos = np.array([p.as_tuple() for p in sol.points], dtype=float)
            colour = self._span_colour(span_index)
            self._add_gl_item(gl.GLLinePlotItem(pos=pos, color=colour, width=2.0))

            for s_val, p, tension in zip(sol.s_m, sol.points, sol.tension_N):
                self._last_rows.append((sol.name, float(s_val), p.x, p.y, p.z, float(tension) / 1000.0))

            self._add_marker(sol.start, size=7, colour=(0.85, 0.85, 0.85, 1.0))
            self._add_marker(sol.end, size=8, colour=colour)

        if body_point is not None:
            all_points.append(body_point)
            self._add_marker(body_point, size=14, colour=(1.0, 0.9, 0.05, 1.0))

        if not all_points:
            return

        xs = [p.x for p in all_points]
        ys = [p.y for p in all_points]
        zs = [p.z for p in all_points]
        x_mid = 0.5 * (min(xs) + max(xs))
        y_mid = 0.5 * (min(ys) + max(ys))
        z_mid = 0.5 * (min(zs) + max(zs))
        extent = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 20.0)

        self._add_axes(x_mid, y_mid, z_mid, extent)
        if self.show_sea_surface_grid.isChecked():
            self._add_grid(z=0.0, size=max(50.0, extent * 1.3), spacing=max(10.0, extent / 8.0))
        if self.show_seabed_grid.isChecked():
            self._add_grid(z=-float(self.seabed_depth.value()), size=max(50.0, extent * 1.3), spacing=max(10.0, extent / 8.0))

        self.reset_view()

    def _span_colour(self, index: int) -> Tuple[float, float, float, float]:
        colours = [
            (0.20, 0.55, 1.00, 1.0),
            (1.00, 0.50, 0.15, 1.0),
            (0.20, 0.80, 0.35, 1.0),
            (0.85, 0.25, 0.25, 1.0),
            (0.65, 0.40, 1.00, 1.0),
        ]
        return colours[int(index) % len(colours)]

    def _clear_gl(self) -> None:
        if self.gl_view is None:
            return
        for item in list(self._gl_items):
            try:
                self.gl_view.removeItem(item)
            except Exception:
                pass
        self._gl_items = []

    def _add_gl_item(self, item) -> None:
        self.gl_view.addItem(item)
        self._gl_items.append(item)

    def _add_marker(self, point: Point3D, size: float, colour: Tuple[float, float, float, float]) -> None:
        if gl is None or np is None:
            return
        pos = np.array([[point.x, point.y, point.z]], dtype=float)
        self._add_gl_item(gl.GLScatterPlotItem(pos=pos, color=colour, size=float(size), pxMode=True))

    def _add_grid(self, z: float, size: float, spacing: float) -> None:
        if gl is None:
            return
        grid = gl.GLGridItem()
        try:
            grid.setSize(x=float(size), y=float(size), z=1.0)
            grid.setSpacing(x=float(spacing), y=float(spacing), z=1.0)
        except Exception:
            pass
        try:
            grid.translate(0.0, 0.0, float(z))
        except Exception:
            pass
        self._add_gl_item(grid)

    def _add_axes(self, x_mid: float, y_mid: float, z_mid: float, extent: float) -> None:
        if gl is None or np is None:
            return
        L = max(10.0, float(extent) * 0.55)
        axes = [
            (np.array([[x_mid - L, y_mid, z_mid], [x_mid + L, y_mid, z_mid]], dtype=float), (1.0, 0.2, 0.2, 1.0)),
            (np.array([[x_mid, y_mid - L, z_mid], [x_mid, y_mid + L, z_mid]], dtype=float), (0.2, 1.0, 0.2, 1.0)),
            (np.array([[x_mid, y_mid, z_mid - L], [x_mid, y_mid, z_mid + L]], dtype=float), (0.2, 0.4, 1.0, 1.0)),
        ]
        for pos, colour in axes:
            self._add_gl_item(gl.GLLinePlotItem(pos=pos, color=colour, width=1.0))

    def reset_view(self) -> None:
        if self.gl_view is None or not self._last_rows:
            return
        xs = [r[2] for r in self._last_rows]
        ys = [r[3] for r in self._last_rows]
        zs = [r[4] for r in self._last_rows]
        extent = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 20.0)
        try:
            if pg is not None:
                self.gl_view.opts["center"] = pg.Vector(
                    0.5 * (min(xs) + max(xs)),
                    0.5 * (min(ys) + max(ys)),
                    0.5 * (min(zs) + max(zs)),
                )
            self.gl_view.setCameraPosition(distance=extent * 2.4, elevation=22, azimuth=-45)
        except Exception:
            try:
                self.gl_view.setCameraPosition(distance=extent * 2.4)
            except Exception:
                pass

    def _replot_last_if_possible(self) -> None:
        # Replot the last successful result only for display-grid/contact-reference changes.
        if self._last_body_result is not None:
            self._plot_spans(self._last_body_result.span_solutions, self._last_body_result.body_position)
            contact = self._combined_contact_report(self._last_body_result.span_solutions)
            self._display_body_results(self._last_body_result, contact)

    # ------------------------------------------------------------------
    # Results / export
    # ------------------------------------------------------------------

    def _display_single_results(self, sol: SpanSolution3D, contact) -> None:
        contact_html = self._contact_html(contact)
        self.results.setHtml(
            "<b>Single 3D span</b><br>"
            f"Horizontal tension H: {sol.horizontal_tension_N / 1000.0:.2f} kN<br>"
            f"Start tension: {sol.start_tension_N / 1000.0:.2f} kN<br>"
            f"End tension: {sol.end_tension_N / 1000.0:.2f} kN<br>"
            f"Minimum smooth-span radius: {self._fmt_radius(sol.min_radius_m)}<br>"
            f"Samples: {len(sol.points)}<br>"
            f"{contact_html}"
        )

    def _display_body_results(self, result, contact) -> None:
        status = "converged" if result.converged else "not converged"
        body = result.body_position
        span_lines = []
        for sol in result.span_solutions:
            span_lines.append(
                f"• {sol.name}: H={sol.horizontal_tension_N / 1000.0:.2f} kN, "
                f"body-end tension={sol.end_tension_N / 1000.0:.2f} kN, "
                f"min radius={self._fmt_radius(sol.min_radius_m)}"
            )
        warn_html = ""
        if result.warnings:
            warn_html = "<br><b>Warnings</b><br>" + "<br>".join(str(w) for w in result.warnings)
        self.results.setHtml(
            "<b>Point-body multi-span equilibrium</b><br>"
            f"Status: <b>{status}</b> after {result.iterations} iteration(s)<br>"
            f"Residual force: {result.residual_norm_N:.2f} N "
            f"(Fx={result.residual_force_N.x:.1f}, Fy={result.residual_force_N.y:.1f}, Fz={result.residual_force_N.z:.1f})<br>"
            f"Body position: X={body.x:.2f} m, Y={body.y:.2f} m, Z={body.z:.2f} m<br><br>"
            "<b>Spans</b><br>"
            + "<br>".join(span_lines)
            + self._contact_html(contact)
            + warn_html
        )

    def _contact_html(self, contact) -> str:
        if contact is None:
            return ""
        html = (
            "<br><br><b>Seabed contact check</b><br>"
            f"Reference seabed depth: {float(self.seabed_depth.value()):.2f} m<br>"
            f"Minimum clearance: {contact.min_clearance_m:.2f} m<br>"
            f"Contact intervals: {len(contact.contact_intervals)}<br>"
            f"Penetration intervals: {len(contact.penetration_intervals)}"
        )
        if contact.first_touch is not None:
            html += (
                f"<br>First apparent touch: station {contact.first_touch.station_m:.2f} m, "
                f"clearance {contact.first_touch.clearance_m:.2f} m"
            )
        if contact.warnings:
            html += "<br><b>Contact warnings</b><br>" + "<br>".join(str(w) for w in contact.warnings)
        return html

    @staticmethod
    def _fmt_radius(value: float) -> str:
        if value == float("inf") or value > 1e12:
            return "∞"
        return f"{value:.2f} m"

    def _show_error(self, title: str, exc: Exception) -> None:
        msg = str(exc).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.results.setHtml(f"<span style='color:red;'><b>{title}</b><br>{msg}</span>")
        QMessageBox.warning(self, title, str(exc))

    def export_csv(self) -> None:
        if not self._last_rows:
            QMessageBox.information(self, "Catenary V2 3D Beta", "Plot or solve a 3D case first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export 3D catenary CSV",
            "catenary_3d_beta.csv",
            "CSV files (*.csv);;All files (*.*)",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["span", "station_m", "x_m", "y_m", "z_m", "tension_kN"])
                for row in self._last_rows:
                    writer.writerow(row)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Exported {len(self._last_rows)} rows to:\n{path}")
