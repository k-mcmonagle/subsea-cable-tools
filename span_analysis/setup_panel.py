# -*- coding: utf-8 -*-
"""The Span & Slack Analysis setup pane: six steps, top to bottom.

1. Route and KP: a Workbench RPL, a line layer, a CSV profile or a synthetic seabed.
2. Seabed: raster(s) or contours sampled along the route, the noise filter,
   and seabed features (boulder, pipeline crossing, berm).
3. Cable and bodies: one library type, library types by KP, or a Workbench
   assembly fitted to the RPL; in-line bodies by KP.
4. Friction: one coefficient, or soil zones by KP with a cable / body x soil table.
5. Lay control: bottom tension or slack, by KP section, and the lay direction.
6. Criteria: the reporting limits (the Lay Assessment's defaults).

:meth:`SetupPanel.settings` / :meth:`apply_settings` round-trip everything
as JSON-ready data (stored per scenario).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject, QgsRasterLayer
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox

from ..cable_seabed import planning as pl
from ..cable_seabed.features import FEATURE_KINDS, FEATURE_LABELS
from ..cable_seabed.sampling import SEABED_CONTOURS, SEABED_RASTER
from ..qgis_compat import (
    CHECK_STATE_CHECKED,
    CHECK_STATE_UNCHECKED,
    ITEM_DATA_USER_ROLE,
    ITEM_FLAG_USER_CHECKABLE,
    MAP_LAYER_FILTER_LINE,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    layer_filters,
)
from . import sources

_HORIZONTAL = getattr(getattr(Qt, "Orientation", Qt), "Horizontal")
_OPEN, _CLOSED = "▾", "▸"

CABLE_LIBRARY = "library"
CABLE_SECTIONS = "sections"
CABLE_FIT = "fit"
CABLE_MANUAL = "manual"
CABLE_CHOICES = (
    (CABLE_LIBRARY, "One cable type (library)"),
    (CABLE_SECTIONS, "Cable types by KP (library)"),
    (CABLE_FIT, "Workbench assembly fitted to the RPL"),
    (CABLE_MANUAL, "Enter the cable properties"),
)
DIRECTIONS = ((1, "Increasing KP"), (-1, "Decreasing KP"))


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------
@dataclass
class Col:
    key: str
    label: str
    kind: str = "float"            # float | text | choice
    default: object = None
    choices: Sequence[Tuple[object, str]] = field(default_factory=tuple)
    tip: str = ""


class EditTable(QWidget):
    """A small editable table with Add / Remove; rows are dicts keyed by column."""

    changed = pyqtSignal()

    def __init__(self, cols: Sequence[Col], add_text: str = "Add", parent=None, height: int = 120):
        super().__init__(parent)
        self.cols = list(cols)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(self.cols))
        self.table.setHorizontalHeaderLabels([c.label for c in self.cols])
        for i, c in enumerate(self.cols):
            if c.tip:
                self.table.horizontalHeaderItem(i).setToolTip(c.tip)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(height)
        self.table.setMaximumHeight(height + 90)
        self.table.itemChanged.connect(lambda *_: self.changed.emit())
        layout.addWidget(self.table)
        bar = QHBoxLayout()
        self.add_button = QPushButton(add_text)
        self.add_button.clicked.connect(lambda: self.add_row())
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_selected)
        bar.addWidget(self.add_button)
        bar.addWidget(remove)
        bar.addStretch(1)
        layout.addLayout(bar)

    def _combo(self, col: Col, value) -> QComboBox:
        combo = QComboBox()
        for data, label in col.choices:
            combo.addItem(label, data)
        index = combo.findData(value)
        if index < 0 and value is not None:
            index = combo.findText(str(value))
        if index < 0 and value not in (None, ""):
            combo.addItem(str(value), value)   # keep a value not in the list (e.g. a type no longer in the library)
            index = combo.count() - 1
        combo.setCurrentIndex(max(index, 0))
        combo.currentIndexChanged.connect(lambda *_: self.changed.emit())
        return combo

    def add_row(self, values: Optional[Dict] = None) -> None:
        values = values or {}
        self.table.blockSignals(True)
        row = self.table.rowCount()
        self.table.insertRow(row)
        for i, c in enumerate(self.cols):
            value = values.get(c.key, c.default)
            if c.kind == "choice":
                self.table.setCellWidget(row, i, self._combo(c, value))
            else:
                text = "" if value is None else (f"{value:g}" if isinstance(value, float) else str(value))
                self.table.setItem(row, i, QTableWidgetItem(text))
        self.table.blockSignals(False)
        self.changed.emit()

    def remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.rowCount():
            rows = [self.table.rowCount() - 1]
        for row in rows:
            self.table.removeRow(row)
        self.changed.emit()

    def rows(self) -> List[Dict]:
        out = []
        for r in range(self.table.rowCount()):
            row = {}
            for i, c in enumerate(self.cols):
                if c.kind == "choice":
                    combo = self.table.cellWidget(r, i)
                    row[c.key] = combo.currentData() if combo is not None else None
                else:
                    item = self.table.item(r, i)
                    text = item.text().strip() if item is not None else ""
                    if c.kind == "float":
                        try:
                            row[c.key] = float(text.replace(",", "."))
                        except ValueError:
                            row[c.key] = None
                    else:
                        row[c.key] = text
            out.append(row)
        return out

    def set_rows(self, rows: Sequence[Dict]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        for row in rows or []:
            self.add_row(row)
        self.changed.emit()

    def set_choices(self, key: str, choices: Sequence[Tuple[object, str]]) -> None:
        index = next((i for i, c in enumerate(self.cols) if c.key == key), None)
        if index is None:
            return
        self.cols[index].choices = list(choices)
        for r in range(self.table.rowCount()):
            combo = self.table.cellWidget(r, index)
            value = combo.currentData() if combo is not None else None
            self.table.setCellWidget(r, index, self._combo(self.cols[index], value))


class Section(QWidget):
    """A collapsible step with a status line in its header."""

    def __init__(self, title: str, body: QWidget, expanded: bool = False, parent=None):
        super().__init__(parent)
        self.title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        self.button = QToolButton()
        self.button.setCheckable(True)
        self.button.setAutoRaise(True)
        self.button.setToolButtonStyle(getattr(getattr(Qt, "ToolButtonStyle", Qt), "ToolButtonTextOnly"))
        self.button.setStyleSheet("QToolButton { font-weight: bold; text-align: left; }")
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #555;")
        header = QHBoxLayout()
        header.addWidget(self.button)
        header.addWidget(self.status, 1)
        layout.addLayout(header)
        self.body = body
        layout.addWidget(body)
        self.button.toggled.connect(self._apply)
        self.button.setChecked(expanded)
        self._apply(expanded)

    def _apply(self, on: bool) -> None:
        self.body.setVisible(on)
        self.button.setText(f"{_OPEN if on else _CLOSED} {self.title}")

    def set_status(self, text: str, ok: bool = True) -> None:
        self.status.setText(text)
        self.status.setStyleSheet("color: #555;" if ok else "color: #d32f2f;")


def _spin(lo: float, hi: float, value: float, decimals: int = 2, step: float = 0.1, suffix: str = "",
          tip: str = "") -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(lo, hi)
    spin.setDecimals(decimals)
    spin.setSingleStep(step)
    spin.setValue(value)
    if suffix:
        spin.setSuffix(suffix)
    if tip:
        spin.setToolTip(tip)
    return spin


def _opt_float(text: str) -> Optional[float]:
    try:
        return float(str(text).strip().replace(",", "."))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The pane
# ---------------------------------------------------------------------------
class SetupPanel(QWidget):
    changed = pyqtSignal()           # any setting
    controlChanged = pyqtSignal()    # lay control only (live update)
    libraryRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = True    # no change signals while the pane is built
        self._library_rows: List[Dict] = []
        self._workbench = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.route_section = Section("1  Route and KP", self._build_route(), True)
        self.seabed_section = Section("2  Seabed", self._build_seabed(), True)
        self.cable_section = Section("3  Cable and bodies", self._build_cable(), True)
        self.friction_section = Section("4  Seabed friction", self._build_friction())
        self.control_section = Section("5  Lay control (tension / slack)", self._build_control(), True)
        self.criteria_section = Section("6  Reporting limits", self._build_criteria())
        for section in (self.route_section, self.seabed_section, self.cable_section, self.friction_section,
                        self.control_section, self.criteria_section):
            layout.addWidget(section)
        layout.addStretch(1)
        self._loading = False
        self.refresh_sources()
        self._update_visibility()
        self.update_status()

    # -- route ------------------------------------------------------------------------
    def _build_route(self) -> QWidget:
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(14, 0, 0, 0)
        self.route_source = QComboBox()
        for key, label in sources.ROUTE_CHOICES:
            self.route_source.addItem(label, key)
        form.addRow("Route", self.route_source)
        rpl_row = QHBoxLayout()
        self.rpl_combo = QComboBox()
        self.rpl_combo.setToolTip("An RPL registered in the Cable Route Workbench. KP is measured along it from "
                                  "its start KP, as the Workbench does.")
        refresh = QToolButton()
        refresh.setText("↻")
        refresh.setToolTip("Reload the Workbench RPLs and fits.")
        refresh.clicked.connect(self.refresh_sources)
        rpl_row.addWidget(self.rpl_combo, 1)
        rpl_row.addWidget(refresh)
        self.rpl_row = QWidget()
        self.rpl_row.setLayout(rpl_row)
        rpl_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("RPL", self.rpl_row)
        self.line_combo = QgsMapLayerComboBox()
        self.line_combo.setFilters(layer_filters(MAP_LAYER_FILTER_LINE))
        self.line_selected = QCheckBox("Selected features only")
        self.line_start_kp = _spin(-100000.0, 100000.0, 0.0, 3, 0.1, " km", "KP at the first vertex of the line.")
        self.line_row = QWidget()
        line_layout = QHBoxLayout(self.line_row)
        line_layout.setContentsMargins(0, 0, 0, 0)
        line_layout.addWidget(self.line_combo, 1)
        line_layout.addWidget(self.line_selected)
        form.addRow("Line layer", self.line_row)
        form.addRow("Start KP", self.line_start_kp)
        self.csv_path = QLineEdit()
        self.csv_path.setPlaceholderText("KP (km) or Chainage (m), Depth (m) or Elevation (m)")
        browse = QToolButton()
        browse.setText("…")
        browse.clicked.connect(self._browse_csv)
        self.csv_row = QWidget()
        csv_layout = QHBoxLayout(self.csv_row)
        csv_layout.setContentsMargins(0, 0, 0, 0)
        csv_layout.addWidget(self.csv_path, 1)
        csv_layout.addWidget(browse)
        form.addRow("CSV file", self.csv_row)
        self.syn = {
            "length_m": _spin(10.0, 500000.0, 1000.0, 0, 100.0, " m"),
            "spacing_m": _spin(0.02, 50.0, 0.5, 2, 0.1, " m"),
            "start_kp": _spin(-10000.0, 100000.0, 0.0, 3, 1.0, " km"),
            "depth_m": _spin(0.0, 11000.0, 100.0, 1, 10.0, " m"),
            "slope_deg": _spin(-60.0, 60.0, 0.0, 2, 0.5, "°", "Positive: deeper with increasing KP."),
            "wave_height_m": _spin(0.0, 50.0, 0.0, 2, 0.5, " m", "Sand waves, crest to trough."),
            "wave_length_m": _spin(1.0, 5000.0, 50.0, 1, 5.0, " m"),
            "roughness_m": _spin(0.0, 20.0, 0.0, 2, 0.05, " m", "Random relief (standard deviation)."),
            "roughness_length_m": _spin(0.1, 500.0, 5.0, 1, 1.0, " m", "Correlation length of the random relief."),
        }
        self.syn_seed = QSpinBox()
        self.syn_seed.setRange(0, 100000)
        self.syn_seed.setValue(1)
        self.syn_box = QWidget()
        syn_form = QFormLayout(self.syn_box)
        syn_form.setContentsMargins(0, 0, 0, 0)
        for key, label in (("length_m", "Length"), ("spacing_m", "Station spacing"), ("start_kp", "Start KP"),
                           ("depth_m", "Depth"), ("slope_deg", "Slope"), ("wave_height_m", "Sand wave height"),
                           ("wave_length_m", "Sand wave length"), ("roughness_m", "Roughness"),
                           ("roughness_length_m", "Roughness length")):
            syn_form.addRow(label, self.syn[key])
        syn_form.addRow("Random seed", self.syn_seed)
        form.addRow(self.syn_box)
        window = QHBoxLayout()
        self.kp_from = QLineEdit()
        self.kp_to = QLineEdit()
        for edit, text in ((self.kp_from, "start"), (self.kp_to, "end")):
            edit.setPlaceholderText(f"route {text}")
            edit.setToolTip("Limit the analysis to a KP window (blank: the whole route). Long routes sample faster "
                            "in windows.")
            window.addWidget(edit)
        self.window_row = QWidget()
        self.window_row.setLayout(window)
        window.setContentsMargins(0, 0, 0, 0)
        form.addRow("KP window", self.window_row)
        for widget in (self.route_source, self.rpl_combo):
            widget.currentIndexChanged.connect(self._on_route_changed)
        self.line_combo.layerChanged.connect(self._on_route_changed)
        self.line_selected.toggled.connect(self._on_route_changed)
        for widget in (self.line_start_kp, *self.syn.values()):
            widget.valueChanged.connect(self._emit_changed)
        self.syn_seed.valueChanged.connect(self._emit_changed)
        for edit in (self.csv_path, self.kp_from, self.kp_to):
            edit.textChanged.connect(self._emit_changed)
        return body

    def _browse_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Seabed profile (CSV)", self.csv_path.text(),
                                              "CSV files (*.csv *.txt);;All files (*)")
        if path:
            self.csv_path.setText(path)

    # -- seabed --------------------------------------------------------------------------
    def _build_seabed(self) -> QWidget:
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(14, 0, 0, 0)
        self.seabed_source = QComboBox()
        for key, label in sources.SEABED_CHOICES:
            self.seabed_source.addItem(label, key)
        form.addRow("Seabed", self.seabed_source)
        self.raster_list = QListWidget()
        self.raster_list.setMaximumHeight(90)
        self.raster_list.setToolTip("Tick the MBES grids to sample. Where several cover a station the finest is "
                                    "used (as in the Depth Profile tool).")
        self.raster_list.itemChanged.connect(self._emit_changed)
        form.addRow("Rasters", self.raster_list)
        self.contour_combo = QgsMapLayerComboBox()
        self.contour_combo.setFilters(layer_filters(MAP_LAYER_FILTER_LINE))
        self.contour_field = QgsFieldComboBox()
        self.contour_combo.layerChanged.connect(self.contour_field.setLayer)
        self.contour_combo.layerChanged.connect(self._emit_changed)
        self.contour_field.fieldChanged.connect(self._emit_changed)
        self.contour_row = QWidget()
        contour = QHBoxLayout(self.contour_row)
        contour.setContentsMargins(0, 0, 0, 0)
        contour.addWidget(self.contour_combo, 1)
        contour.addWidget(self.contour_field)
        form.addRow("Contours", self.contour_row)
        self.interval = QSpinBox()
        self.interval.setRange(0, 500)
        self.interval.setSpecialValueText("auto (grid cell size)")
        self.interval.setSuffix(" m")
        self.interval.setToolTip("Station spacing along the route. Auto samples each raster at its own cell size "
                                 "(at least 1 m): coarser sampling misses crests and shortens the seabed.")
        self.interval.valueChanged.connect(self._emit_changed)
        form.addRow("Sample every", self.interval)
        self.filter_m = _spin(0.0, 50.0, 2.0, 1, 0.5, " m",
                              "Running mean the cable rests on: removes sounding noise (which would make false "
                              "spans between noise peaks) but keeps the crests. 0 for clean or synthetic data.")
        self.filter_m.valueChanged.connect(self._emit_changed)
        form.addRow("Noise filter", self.filter_m)
        self.smoothing = QLineEdit()
        self.smoothing.setPlaceholderText("auto: the cable's conformity length 2 (EI/w)^(1/3)")
        self.smoothing.setToolTip("Smoothing of the seabed for lengths (fill and slack): noise adds length. "
                                  "Blank: the conformity length (10 m without EI).")
        self.smoothing.textChanged.connect(self._emit_changed)
        form.addRow("Smoothing for lengths", self.smoothing)
        kinds = [(k, FEATURE_LABELS[k]) for k in FEATURE_KINDS]
        self.features = EditTable([
            Col("kind", "Feature", "choice", "boulder", kinds),
            Col("kp", "KP", "float", None, tip="Centre KP (km)."),
            Col("height", "Height (m)", "float", 1.0, tip="Boulder / berm: height above the seabed. Pipeline: top "
                                                          "of pipe above the seabed."),
            Col("length", "Length / OD (m)", "float", 2.0, tip="Boulder / berm: base length along the route. "
                                                               "Pipeline: outside diameter (with coating)."),
            Col("crest", "Crest (m)", "float", 0.0, tip="Berm / mattress: length of the flat top."),
            Col("name", "Name", "text", ""),
        ], "Add feature", height=90)
        self.features.changed.connect(self._emit_changed)
        form.addRow(QLabel("Seabed features (added to the profile)"))
        form.addRow(self.features)
        self.seabed_source.currentIndexChanged.connect(self._on_route_changed)
        return body

    # -- cable --------------------------------------------------------------------------
    def _build_cable(self) -> QWidget:
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(14, 0, 0, 0)
        self.cable_source = QComboBox()
        for key, label in CABLE_CHOICES:
            self.cable_source.addItem(label, key)
        self.cable_source.currentIndexChanged.connect(self._on_cable_source)
        form.addRow("Cable", self.cable_source)
        type_row = QHBoxLayout()
        self.cable_type = QComboBox()
        self.cable_type.currentIndexChanged.connect(self._emit_changed)
        library_button = QPushButton("Library…")
        library_button.setToolTip("Open the Cable Type Library (weights, EI, MBR, NPTS).")
        library_button.clicked.connect(self.libraryRequested.emit)
        type_row.addWidget(self.cable_type, 1)
        type_row.addWidget(library_button)
        self.type_row = QWidget()
        self.type_row.setLayout(type_row)
        type_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Cable type", self.type_row)
        self.cable_sections = EditTable([
            Col("kp0", "KP from", "float"), Col("kp1", "KP to", "float"),
            Col("type", "Cable type", "choice", "", ()),
        ], "Add section", height=80)
        self.cable_sections.changed.connect(self._emit_changed)
        form.addRow(self.cable_sections)
        self.fit_combo = QComboBox()
        self.fit_combo.setToolTip("An assembly fitted to the RPL in the Workbench: its sections' cable types and "
                                  "its bodies land at KPs through the RPL's slack.")
        self.fit_combo.currentIndexChanged.connect(self._emit_changed)
        form.addRow("Assembly fit", self.fit_combo)
        self.manual = {
            "w_kg_m": _spin(0.0, 1000.0, 1.0, 3, 0.1, " kg/m", "Submerged weight per metre."),
            "ei_knm2": _spin(0.0, 100000.0, 1.0, 2, 0.5, " kN·m²", "Bending stiffness EI (0: unknown)."),
            "mbr_m": _spin(0.0, 100.0, 1.5, 2, 0.1, " m", "Minimum bend radius (0: unknown)."),
            "diameter_mm": _spin(0.0, 1000.0, 25.0, 1, 1.0, " mm"),
            "npts_kn": _spin(0.0, 10000.0, 0.0, 1, 1.0, " kN", "Nominal permanent tensile strength (0: unknown)."),
        }
        self.manual_box = QWidget()
        manual_form = QFormLayout(self.manual_box)
        manual_form.setContentsMargins(0, 0, 0, 0)
        for key, label in (("w_kg_m", "Weight in water"), ("ei_knm2", "EI"), ("mbr_m", "MBR"),
                           ("diameter_mm", "Diameter"), ("npts_kn", "NPTS")):
            manual_form.addRow(label, self.manual[key])
            self.manual[key].valueChanged.connect(self._emit_changed)
        form.addRow(self.manual_box)
        self.cable_info = QLabel("")
        self.cable_info.setWordWrap(True)
        self.cable_info.setStyleSheet("color: #555;")
        form.addRow(self.cable_info)
        self.bodies_from_fit = QCheckBox("Add the fit's bodies")
        self.bodies_from_fit.setChecked(True)
        self.bodies_from_fit.setToolTip("Repeaters, joints and branching units of the fitted assembly, at their "
                                        "landing KPs.")
        self.bodies_from_fit.toggled.connect(self._emit_changed)
        form.addRow(self.bodies_from_fit)
        self.bodies = EditTable([
            Col("name", "Name", "text", "Repeater"),
            Col("kind", "Kind", "text", "Repeater", tip="Friction table key (Repeater, Joint, Branching unit …)."),
            Col("kp", "KP", "float"),
            Col("length", "Length (m)", "float", 3.0, tip="Rigid length of the housing."),
            Col("weight", "Weight in water (kN)", "float", 3.0),
            Col("diameter", "Diameter (mm)", "float", 300.0, tip="Housing diameter: the cable either side is held "
                                                                 "off the seabed by half the difference."),
        ], "Add body", height=80)
        self.bodies.changed.connect(self._emit_changed)
        form.addRow(QLabel("In-line bodies (in addition)"))
        form.addRow(self.bodies)
        return body

    # -- friction ------------------------------------------------------------------------
    def _build_friction(self) -> QWidget:
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(14, 0, 0, 0)
        tip = ("Coulomb friction between cable and seabed. No published axial value for cables on sand or clay was "
               "found; pipeline guidance gives 0.6 (sand) and 0.2 (clay) laterally (DNV-RP-F109), and on rock the "
               "axial resistance can be many times the interface friction (Griffiths et al. 2024). Run a low, "
               "best and high value. A higher value keeps cable more local and holds steeper slopes.")
        self.mu = _spin(0.0, 5.0, pl.DEFAULT_MU, 2, 0.05, "", tip)
        self.mu_lat = _spin(0.0, 5.0, pl.DEFAULT_MU, 2, 0.05, "", "Lateral friction, for the alter-course check.")
        for spin in (self.mu, self.mu_lat):
            spin.valueChanged.connect(self._emit_changed)
        form.addRow("Axial μ (all)", self.mu)
        form.addRow("Lateral μ (all)", self.mu_lat)
        self.use_zones = QCheckBox("Soil zones and friction table")
        self.use_zones.toggled.connect(self._update_visibility)
        self.use_zones.toggled.connect(self._emit_changed)
        form.addRow(self.use_zones)
        self.zones = EditTable([Col("kp0", "KP from", "float"), Col("kp1", "KP to", "float"),
                                Col("soil", "Soil", "text", "Sand")], "Add zone", height=70)
        self.friction_table = EditTable([
            Col("item", "Cable type / body kind", "text", "*", tip="A library cable type name or a body kind; * "
                                                                   "for any."),
            Col("soil", "Soil", "text", "Sand", tip="A soil name used in the zones; * for any."),
            Col("mu", "Axial μ", "float", 0.5), Col("mu_lat", "Lateral μ", "float", None),
        ], "Add row", height=70)
        for table in (self.zones, self.friction_table):
            table.changed.connect(self._emit_changed)
        self.zones_label = QLabel("Soil zones by KP (the rest uses the values above)")
        self.table_label = QLabel("Friction by cable type / body kind and soil (most specific row wins)")
        form.addRow(self.zones_label)
        form.addRow(self.zones)
        form.addRow(self.table_label)
        form.addRow(self.friction_table)
        return body

    # -- lay control ----------------------------------------------------------------------
    def _build_control(self) -> QWidget:
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(14, 0, 0, 0)
        self.mode = QComboBox()
        for key in pl.MODES:
            self.mode.addItem(pl.MODE_LABELS[key], key)
        self.mode.setToolTip("Bottom tension: the tension the cable is laid at on the seabed. Slack: extra cable "
                             "over the seabed (bottom) or over the route (RPL); where it is less than the seabed "
                             "needs, the cable is pulled taut.")
        self.mode.currentIndexChanged.connect(self._on_mode)
        form.addRow("Control", self.mode)
        value_row = QHBoxLayout()
        self.value = _spin(-50.0, 1000.0, 5.0, 2, 0.1)
        self.slider = QSlider(_HORIZONTAL)
        self.slider.setRange(0, 1000)
        value_row.addWidget(self.value)
        value_row.addWidget(self.slider, 1)
        self.value_row = QWidget()
        self.value_row.setLayout(value_row)
        value_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Value", self.value_row)
        self.value.valueChanged.connect(self._on_value)
        self.slider.valueChanged.connect(self._on_slider)
        self.live = QCheckBox("Update the results as the value changes")
        self.live.setChecked(True)
        form.addRow(self.live)
        self.direction = QComboBox()
        for key, label in DIRECTIONS:
            self.direction.addItem(label, key)
        self.direction.setToolTip("The direction the cable is laid. Friction holds the tension each stretch was "
                                  "laid at, so on slopes the result depends on it.")
        self.direction.currentIndexChanged.connect(self._emit_control)
        form.addRow("Lay direction", self.direction)
        modes = [(k, pl.MODE_LABELS[k]) for k in pl.MODES]
        self.control_sections = EditTable([Col("kp0", "KP from", "float"), Col("kp1", "KP to", "float"),
                                           Col("mode", "Control", "choice", pl.TENSION, modes),
                                           Col("value", "Value", "float", 5.0)], "Add section", height=80)
        self.control_sections.changed.connect(self._emit_control)
        form.addRow(QLabel("By KP section (overrides the value above)"))
        form.addRow(self.control_sections)
        self._on_mode()
        return body

    def _slider_range(self) -> Tuple[float, float]:
        return (0.0, 50.0) if self.mode.currentData() == pl.TENSION else (-2.0, 20.0)

    def _on_mode(self, *_args) -> None:
        mode = self.mode.currentData()
        self.value.setSuffix(" kN" if mode == pl.TENSION else " %")
        self.value.setToolTip("Bottom tension at touchdown." if mode == pl.TENSION else
                              ("Slack where an RPL leg has none." if mode == pl.RPL_LEGS else "Slack."))
        self._on_value(self.value.value())
        self._emit_control()

    def _on_value(self, value: float) -> None:
        lo, hi = self._slider_range()
        self.slider.blockSignals(True)
        self.slider.setValue(int(round((min(max(value, lo), hi) - lo) / (hi - lo) * 1000)))
        self.slider.blockSignals(False)
        self._emit_control()

    def _on_slider(self, position: int) -> None:
        lo, hi = self._slider_range()
        self.value.setValue(lo + (hi - lo) * position / 1000.0)

    # -- criteria ---------------------------------------------------------------------------
    def _build_criteria(self) -> QWidget:
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(14, 0, 0, 0)
        c = pl.Criteria()
        self.crit = {
            "min_gap_m": _spin(0.0, 50.0, c.min_gap_m, 2, 0.05, " m"),
            "min_length_m": _spin(0.0, 1000.0, c.min_length_m, 1, 1.0, " m"),
            "red_gap_m": _spin(0.0, 50.0, c.red_gap_m, 2, 0.1, " m"),
            "red_length_m": _spin(0.0, 5000.0, c.red_length_m, 1, 5.0, " m"),
            "conform_below_kn": _spin(0.0, 10.0, c.conform_below_kn, 3, 0.01, " kN"),
            "loop_excess_pct": _spin(0.0, 100.0, c.loop_excess_pct, 1, 0.5, " %"),
            "loop_tension_kn": _spin(0.0, 10.0, c.loop_tension_kn, 2, 0.05, " kN"),
            "slack_window_m": _spin(1.0, 10000.0, c.slack_window_m, 0, 10.0, " m"),
            "shortfall_pct": _spin(0.0, 10.0, c.shortfall_pct, 2, 0.05, " %"),
            "max_reach_m": _spin(10.0, 100000.0, c.max_reach_m, 0, 100.0, " m"),
            "max_slope_deg": _spin(1.0, 89.0, c.max_slope_deg, 0, 1.0, "°"),
        }
        labels = {
            "min_gap_m": ("Report spans higher than", "A span is reported when the cable rises this far above the "
                                                      "seabed. Its length runs from lift-off to touchdown."),
            "min_length_m": ("…and longer than", ""),
            "red_gap_m": ("Red when higher than", ""),
            "red_length_m": ("…or longer than", "Spans are also red where the bend at a support is below the MBR."),
            "conform_below_kn": ("Cable follows the seabed below", "Below this tension the cable lies on and "
                                                                   "follows the seabed."),
            "loop_excess_pct": ("Loop risk: surplus above", "Cable laid beyond what the seabed needs, over the "
                                                             "slack window, at near-zero tension (0: off)."),
            "loop_tension_kn": ("…at tension below", ""),
            "slack_window_m": ("Slack window", "Length over which slack percentages are taken."),
            "shortfall_pct": ("Cable short of the seabed by over", "Slack control: below this the cable is "
                                                                   "treated as covering the seabed."),
            "max_reach_m": ("Longest friction reach", "The furthest a pulled-taut stretch draws cable from."),
            "max_slope_deg": ("Small-slope note above", "The rest shape uses a small-slope model (with a slope "
                                                        "correction); a note marks steeper seabed."),
        }
        for key, spin in self.crit.items():
            label, tip = labels[key]
            if tip:
                spin.setToolTip(tip)
            spin.valueChanged.connect(self._emit_changed)
            form.addRow(label, spin)
        self.allowable = QCheckBox("Find the highest tension that keeps spans within limits")
        self.allowable.setChecked(True)
        self.allowable.setToolTip("Bisection on a constant tension in 250 m windows (frictionless seabed).")
        self.allowable.toggled.connect(self._emit_changed)
        form.addRow(self.allowable)
        return body

    # -- sources ------------------------------------------------------------------------------
    def refresh_sources(self) -> None:
        """Reload the Workbench RPLs and fits, the library types and the raster layers."""
        self._loading = True
        try:
            self._workbench = sources.open_workbench()
            current = self.rpl_combo.currentData()
            self.rpl_combo.clear()
            if self._workbench is not None:
                for rpl_id, label in sources.rpl_choices(self._workbench):
                    self.rpl_combo.addItem(label, rpl_id)
            if self.rpl_combo.count() == 0:
                self.rpl_combo.addItem("(no Workbench RPLs in this project)", None)
            index = self.rpl_combo.findData(current)
            self.rpl_combo.setCurrentIndex(max(index, 0))
            self._refresh_fits()
            self._library_rows = sources.library_rows()
            names = [(row.get("name"), row.get("name")) for row in self._library_rows if row.get("name")]
            current_type = self.cable_type.currentData()
            self.cable_type.clear()
            for data, label in names:
                self.cable_type.addItem(label, data)
            if not names:
                self.cable_type.addItem("(the cable library is empty: open Library…)", None)
            index = self.cable_type.findData(current_type)
            self.cable_type.setCurrentIndex(max(index, 0))
            self.cable_sections.set_choices("type", names)
            self._refresh_rasters()
        finally:
            self._loading = False
        self.update_status()

    def _refresh_fits(self) -> None:
        current = self.fit_combo.currentData()
        self.fit_combo.clear()
        rpl_id = self.rpl_combo.currentData()
        if self._workbench is not None and rpl_id:
            for fit_id, label in sources.fit_choices(self._workbench, rpl_id):
                self.fit_combo.addItem(label, fit_id)
        if self.fit_combo.count() == 0:
            self.fit_combo.addItem("(no assembly fitted to this RPL)", None)
        index = self.fit_combo.findData(current)
        self.fit_combo.setCurrentIndex(max(index, 0))

    def _refresh_rasters(self, checked: Optional[Sequence[str]] = None) -> None:
        if checked is None:
            checked = self.checked_raster_ids()
        self.raster_list.blockSignals(True)
        self.raster_list.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer):
                item = QListWidgetItem(layer.name())
                item.setData(ITEM_DATA_USER_ROLE, layer.id())
                item.setFlags(item.flags() | ITEM_FLAG_USER_CHECKABLE)
                item.setCheckState(CHECK_STATE_CHECKED if layer.id() in checked else CHECK_STATE_UNCHECKED)
                self.raster_list.addItem(item)
        self.raster_list.blockSignals(False)

    def checked_raster_ids(self) -> List[str]:
        out = []
        for i in range(self.raster_list.count()):
            item = self.raster_list.item(i)
            if item.checkState() == CHECK_STATE_CHECKED:
                out.append(item.data(ITEM_DATA_USER_ROLE))
        return out

    @property
    def workbench(self):
        return self._workbench

    @property
    def library_rows(self) -> List[Dict]:
        return self._library_rows

    # -- change handling ---------------------------------------------------------------------
    def _emit_changed(self, *_args) -> None:
        if self._loading:
            return
        self.update_status()
        self.changed.emit()

    def _emit_control(self, *_args) -> None:
        if self._loading:
            return
        self.update_status()
        self.changed.emit()
        self.controlChanged.emit()

    def _on_route_changed(self, *_args) -> None:
        if self.sender() is self.rpl_combo:
            self._refresh_fits()
        self._update_visibility()
        self._emit_changed()

    def _on_cable_source(self, *_args) -> None:
        self._update_visibility()
        self._emit_changed()

    def _update_visibility(self, *_args) -> None:
        route = self.route_source.currentData()
        on_map = route in (sources.ROUTE_RPL, sources.ROUTE_LINE)
        seabed = self.seabed_source.currentData()
        cable = self.cable_source.currentData()
        zones = self.use_zones.isChecked()
        rows = (
            (self.rpl_row, route == sources.ROUTE_RPL),
            (self.line_row, route == sources.ROUTE_LINE),
            (self.line_start_kp, route == sources.ROUTE_LINE),
            (self.csv_row, route == sources.ROUTE_CSV),
            (self.syn_box, route == sources.ROUTE_SYNTHETIC),
            (self.window_row, on_map),
            (self.seabed_source, on_map),
            (self.raster_list, on_map and seabed == SEABED_RASTER),
            (self.contour_row, on_map and seabed == SEABED_CONTOURS),
            (self.interval, on_map and seabed in (SEABED_RASTER, SEABED_CONTOURS)),
            (self.type_row, cable in (CABLE_LIBRARY, CABLE_SECTIONS, CABLE_FIT)),
            (self.cable_sections, cable == CABLE_SECTIONS),
            (self.fit_combo, cable == CABLE_FIT),
            (self.bodies_from_fit, cable == CABLE_FIT),
            (self.manual_box, cable == CABLE_MANUAL),
            (self.zones, zones), (self.friction_table, zones), (self.zones_label, zones), (self.table_label, zones),
        )
        for widget, visible in rows:
            _set_row_visible(widget, visible)
        label = _form_label(self.type_row)
        if label is not None:
            label.setText({CABLE_LIBRARY: "Cable type", CABLE_SECTIONS: "Default cable type",
                           CABLE_FIT: "Cable type outside the fit"}.get(cable, "Cable type"))

    # -- readiness -----------------------------------------------------------------------------
    def update_status(self) -> None:
        route = self.route_source.currentData()
        if route == sources.ROUTE_RPL:
            ok = bool(self.rpl_combo.currentData())
            self.route_section.set_status(self.rpl_combo.currentText() if ok else "Needs a Workbench RPL", ok)
        elif route == sources.ROUTE_LINE:
            layer = self.line_combo.currentLayer()
            self.route_section.set_status(layer.name() if layer else "Needs a line layer", layer is not None)
        elif route == sources.ROUTE_CSV:
            ok = bool(self.csv_path.text().strip())
            self.route_section.set_status(self.csv_path.text().strip() if ok else "Needs a CSV file", ok)
        else:
            self.route_section.set_status(f"Synthetic, {self.syn['length_m'].value():.0f} m", True)
        if route in (sources.ROUTE_RPL, sources.ROUTE_LINE):
            seabed = self.seabed_source.currentData()
            if seabed == SEABED_RASTER:
                n = len(self.checked_raster_ids())
                self.seabed_section.set_status(f"{n} raster(s)" if n else "Tick one or more rasters", n > 0)
            elif seabed == SEABED_CONTOURS:
                ok = self.contour_combo.currentLayer() is not None and bool(self.contour_field.currentField())
                self.seabed_section.set_status("Contours" if ok else "Needs a contour layer and depth field", ok)
            else:
                self.seabed_section.set_status("RPL approximate depth (coarse: no relief)", True)
        else:
            self.seabed_section.set_status("From the profile" + (f", {len(self.features.rows())} feature(s)"
                                                                 if self.features.rows() else ""), True)
        cable = self.cable_source.currentData()
        if cable == CABLE_MANUAL:
            w = self.manual["w_kg_m"].value()
            self.cable_section.set_status(f"{w:g} kg/m in water" if w > 0 else "Needs the weight in water", w > 0)
        elif cable == CABLE_FIT:
            ok = bool(self.fit_combo.currentData())
            self.cable_section.set_status(self.fit_combo.currentText() if ok else "Needs a fitted assembly", ok)
        else:
            ok = bool(self.cable_type.currentData())
            self.cable_section.set_status(self.cable_type.currentText() if ok else "Needs a library cable type", ok)
        bodies = len(self.bodies.rows())
        if bodies:
            self.cable_section.status.setText(self.cable_section.status.text() + f", {bodies} body(ies)")
        self.friction_section.set_status(f"μ {self.mu.value():.2f}" + (", soil zones" if self.use_zones.isChecked()
                                                                        else ""), True)
        mode = self.mode.currentData()
        unit = pl.MODE_UNITS[mode]
        sections = len(self.control_sections.rows())
        self.control_section.set_status(f"{pl.MODE_LABELS[mode].split(' (')[0]} {self.value.value():.2f} {unit}"
                                        + (f", {sections} section(s)" if sections else ""), True)
        self._update_cable_info()

    def _update_cable_info(self) -> None:
        try:
            cable = self.default_cable()
        except ValueError:
            self.cable_info.setText("")
            return
        if cable is None:
            self.cable_info.setText("")
            return
        tension = self.value.value() if self.mode.currentData() == pl.TENSION else math.nan
        parts = [f"{cable.w_npm:.1f} N/m in water" if math.isfinite(cable.w_npm) else "no weight in water"]
        if math.isfinite(cable.ei_knm2) and cable.ei_knm2 > 0:
            parts.append(f"EI {cable.ei_knm2:g} kN·m²")
            if tension > 0:
                parts.append(f"bending length √(EI/T) {cable.bending_length_m(tension):.2f} m at {tension:g} kN")
        if math.isfinite(cable.mbr_m):
            parts.append(f"MBR {cable.mbr_m:g} m")
        if math.isfinite(cable.npts_kn):
            parts.append(f"NPTS {cable.npts_kn:g} kN")
        if tension > 0 and math.isfinite(cable.w_npm) and cable.w_npm > 0:
            parts.append(f"follows hollows of radius over {tension * 1000.0 / cable.w_npm:,.0f} m (T/w)")
        self.cable_info.setText("; ".join(parts))

    # -- engine inputs -----------------------------------------------------------------------------
    def library_type(self, name: Optional[str]) -> Optional[pl.CableType]:
        row = next((r for r in self._library_rows if r.get("name") == name), None)
        return sources.cable_type_from_row(row) if row is not None else None

    def default_cable(self) -> Optional[pl.CableType]:
        if self.cable_source.currentData() == CABLE_MANUAL:
            m = self.manual

            def opt(value):
                return value if value > 0 else math.nan

            return pl.CableType("Cable", w_npm=m["w_kg_m"].value() * 9.80665, ei_knm2=opt(m["ei_knm2"].value()),
                                mbr_m=opt(m["mbr_m"].value()), diameter_m=opt(m["diameter_mm"].value()) / 1000.0,
                                npts_kn=opt(m["npts_kn"].value()))
        name = self.cable_type.currentData()
        return self.library_type(name) if name else None

    def kp_window(self) -> Optional[Tuple[float, float]]:
        lo, hi = _opt_float(self.kp_from.text()), _opt_float(self.kp_to.text())
        if lo is None and hi is None:
            return None
        return (lo if lo is not None else -math.inf, hi if hi is not None else math.inf)

    def control(self, legs: Sequence[Tuple[float, float, float]] = ()) -> pl.LayControl:
        sections = []
        for row in self.control_sections.rows():
            if row.get("kp0") is None or row.get("kp1") is None or row.get("value") is None:
                continue
            sections.append(pl.ControlSection(row["kp0"], row["kp1"], row.get("mode") or pl.TENSION, row["value"]))
        return pl.LayControl(self.mode.currentData(), float(self.value.value()), sections, list(legs),
                             int(self.direction.currentData() or 1))

    def criteria(self) -> pl.Criteria:
        values = {key: float(spin.value()) for key, spin in self.crit.items()}
        return pl.Criteria(allowable=self.allowable.isChecked(), **values)

    def friction(self) -> pl.FrictionModel:
        model = pl.FrictionModel(float(self.mu.value()), float(self.mu_lat.value()))
        if self.use_zones.isChecked():
            for row in self.zones.rows():
                if row.get("kp0") is not None and row.get("kp1") is not None and row.get("soil"):
                    model.zones.append(pl.SoilZone(row["kp0"], row["kp1"], row["soil"]))
            for row in self.friction_table.rows():
                if row.get("mu") is None:
                    continue
                model.table.append(pl.FrictionEntry(row.get("item") or "*", row.get("soil") or "*", row["mu"],
                                                    row["mu_lat"] if row.get("mu_lat") is not None else math.nan))
        return model

    def features_list(self):
        from ..cable_seabed.features import SeabedFeature

        out = []
        for row in self.features.rows():
            if row.get("kp") is None or row.get("height") is None or row.get("length") is None:
                continue
            out.append(SeabedFeature(row.get("kind") or "boulder", row["kp"], row["height"], row["length"],
                                     row.get("crest") or 0.0, row.get("name") or ""))
        return out

    def manual_bodies(self) -> List[pl.Body]:
        out = []
        for row in self.bodies.rows():
            if row.get("kp") is None:
                continue
            diameter = row.get("diameter")
            out.append(pl.Body(row.get("name") or "Body", row["kp"], row.get("length") or 0.0,
                               row.get("weight") or 0.0, diameter / 1000.0 if diameter else math.nan,
                               row.get("kind") or "Body"))
        return out

    def cable_sections_list(self) -> List[pl.CableSection]:
        out = []
        for row in self.cable_sections.rows():
            cable = self.library_type(row.get("type"))
            if cable is None or row.get("kp0") is None or row.get("kp1") is None:
                continue
            out.append(pl.CableSection(row["kp0"], row["kp1"], cable))
        return out

    def smoothing_m(self) -> Optional[float]:
        return _opt_float(self.smoothing.text())

    # -- settings round trip ------------------------------------------------------------------------
    def profile_signature(self) -> str:
        """What the sampled profile depends on (a change means sampling again)."""
        s = self.settings()
        route = dict(s["route"])
        seabed = {k: s["seabed"][k] for k in ("source", "rasters", "contour_layer", "contour_field", "interval")}
        return json.dumps({"route": route, "seabed": seabed}, sort_keys=True)

    def settings(self) -> Dict:
        line = self.line_combo.currentLayer()
        contour = self.contour_combo.currentLayer()
        return {
            "version": 1,
            "route": {
                "source": self.route_source.currentData(),
                "rpl_id": self.rpl_combo.currentData(),
                "line_layer": line.id() if line else None,
                "line_selected": self.line_selected.isChecked(),
                "start_kp": self.line_start_kp.value(),
                "csv_path": self.csv_path.text().strip(),
                "synthetic": {**{k: w.value() for k, w in self.syn.items()}, "seed": self.syn_seed.value()},
                "kp_from": self.kp_from.text().strip(),
                "kp_to": self.kp_to.text().strip(),
            },
            "seabed": {
                "source": self.seabed_source.currentData(),
                "rasters": self.checked_raster_ids(),
                "contour_layer": contour.id() if contour else None,
                "contour_field": self.contour_field.currentField(),
                "interval": self.interval.value(),
                "filter_m": self.filter_m.value(),
                "smoothing": self.smoothing.text().strip(),
                "features": self.features.rows(),
            },
            "cable": {
                "source": self.cable_source.currentData(),
                "type": self.cable_type.currentData(),
                "sections": self.cable_sections.rows(),
                "fit_id": self.fit_combo.currentData(),
                "bodies_from_fit": self.bodies_from_fit.isChecked(),
                "manual": {k: w.value() for k, w in self.manual.items()},
                "bodies": self.bodies.rows(),
            },
            "friction": {
                "mu": self.mu.value(), "mu_lat": self.mu_lat.value(), "zones_on": self.use_zones.isChecked(),
                "zones": self.zones.rows(), "table": self.friction_table.rows(),
            },
            "control": {
                "mode": self.mode.currentData(), "value": self.value.value(),
                "direction": self.direction.currentData(), "sections": self.control_sections.rows(),
                "live": self.live.isChecked(),
            },
            "criteria": {**{k: w.value() for k, w in self.crit.items()}, "allowable": self.allowable.isChecked()},
        }

    def apply_settings(self, s: Dict) -> None:
        if not s:
            return
        self._loading = True
        try:
            route = s.get("route", {})
            _set_combo(self.route_source, route.get("source"))
            _set_combo(self.rpl_combo, route.get("rpl_id"))
            self._refresh_fits()
            layer = QgsProject.instance().mapLayer(route.get("line_layer") or "")
            if layer is not None:
                self.line_combo.setLayer(layer)
            self.line_selected.setChecked(bool(route.get("line_selected")))
            self.line_start_kp.setValue(float(route.get("start_kp") or 0.0))
            self.csv_path.setText(route.get("csv_path") or "")
            syn = route.get("synthetic") or {}
            for key, widget in self.syn.items():
                if key in syn:
                    widget.setValue(float(syn[key]))
            self.syn_seed.setValue(int(syn.get("seed", 1)))
            self.kp_from.setText(route.get("kp_from") or "")
            self.kp_to.setText(route.get("kp_to") or "")
            seabed = s.get("seabed", {})
            _set_combo(self.seabed_source, seabed.get("source"))
            self._refresh_rasters(seabed.get("rasters") or [])
            contour = QgsProject.instance().mapLayer(seabed.get("contour_layer") or "")
            if contour is not None:
                self.contour_combo.setLayer(contour)
                self.contour_field.setLayer(contour)
                self.contour_field.setField(seabed.get("contour_field") or "")
            self.interval.setValue(int(seabed.get("interval") or 0))
            self.filter_m.setValue(float(seabed.get("filter_m", 2.0)))
            self.smoothing.setText(seabed.get("smoothing") or "")
            self.features.set_rows(seabed.get("features") or [])
            cable = s.get("cable", {})
            _set_combo(self.cable_source, cable.get("source"))
            _set_combo(self.cable_type, cable.get("type"))
            self.cable_sections.set_rows(cable.get("sections") or [])
            _set_combo(self.fit_combo, cable.get("fit_id"))
            self.bodies_from_fit.setChecked(bool(cable.get("bodies_from_fit", True)))
            for key, widget in self.manual.items():
                if key in (cable.get("manual") or {}):
                    widget.setValue(float(cable["manual"][key]))
            self.bodies.set_rows(cable.get("bodies") or [])
            friction = s.get("friction", {})
            self.mu.setValue(float(friction.get("mu", pl.DEFAULT_MU)))
            self.mu_lat.setValue(float(friction.get("mu_lat", pl.DEFAULT_MU)))
            self.use_zones.setChecked(bool(friction.get("zones_on")))
            self.zones.set_rows(friction.get("zones") or [])
            self.friction_table.set_rows(friction.get("table") or [])
            control = s.get("control", {})
            _set_combo(self.mode, control.get("mode"))
            self.value.setValue(float(control.get("value", 5.0)))
            _set_combo(self.direction, control.get("direction"))
            self.control_sections.set_rows(control.get("sections") or [])
            self.live.setChecked(bool(control.get("live", True)))
            crit = s.get("criteria", {})
            for key, widget in self.crit.items():
                if key in crit:
                    widget.setValue(float(crit[key]))
            self.allowable.setChecked(bool(crit.get("allowable", True)))
        finally:
            self._loading = False
        self._on_mode()
        self._update_visibility()
        self.update_status()

    def describe(self) -> List[str]:
        """Plain-language input lines for the summary and report."""
        s = self.settings()
        lines = []
        route = self.route_source.currentText()
        if s["route"]["source"] == sources.ROUTE_RPL:
            route += f": {self.rpl_combo.currentText()}"
        elif s["route"]["source"] == sources.ROUTE_LINE:
            layer = self.line_combo.currentLayer()
            route += f": {layer.name() if layer else ''}"
        lines.append(route)
        lines.append(f"Seabed: {self.seabed_section.status.text()}; noise filter {self.filter_m.value():g} m")
        lines.append(f"Cable: {self.cable_section.status.text()}")
        lines.append(f"Friction: {self.friction_section.status.text()}")
        lines.append(f"Lay control: {self.control_section.status.text()}, laid towards "
                     f"{self.direction.currentText().lower()}")
        return lines


def _form_label(widget: QWidget):
    parent = widget.parentWidget()
    layout = parent.layout() if parent is not None else None
    return layout.labelForField(widget) if isinstance(layout, QFormLayout) else None


def _set_row_visible(widget: QWidget, visible: bool) -> None:
    widget.setVisible(visible)
    label = _form_label(widget)
    if label is not None:
        label.setVisible(visible)


def _set_combo(combo: QComboBox, data) -> None:
    index = combo.findData(data)
    if index >= 0:
        combo.setCurrentIndex(index)
