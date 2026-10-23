# -*- coding: utf-8 -*-
"""The Span & Slack Analysis setup pane: six steps, top to bottom.

1. Route and KP: a Workbench RPL, a line layer, a CSV profile or a synthetic seabed.
2. Seabed: raster(s) or contours sampled along the route, the noise filter,
   and seabed features (boulder, pipeline crossing, berm).
3. Cable and bodies: one library type, library types by KP, or a Workbench
   assembly fitted to the RPL; in-line bodies by KP.
4. Friction: one coefficient, or soil zones by KP with a cable / body x soil table.
5. Lay control: bottom tension or slack, by KP section, and the lay direction;
   sections can hold the cable (buried by a plough, or pinned at an anchor).
6. Criteria: the reporting limits (the Lay Assessment's defaults).

:meth:`SetupPanel.settings` / :meth:`apply_settings` round-trip everything
as JSON-ready data (stored per scenario).
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from qgis.PyQt import sip
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
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
from qgis.core import QgsProject
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox

from ..cable_seabed import planning as pl
from ..cable_seabed.features import FEATURE_KINDS, FEATURE_LABELS
from ..cable_seabed.sampling import RESOLVE_INTERVALS, SEABED_CONTOURS, SEABED_RASTER
from ..maptools.depth_source_table import layer_group_path
from ..qgis_compat import (
    CHECK_STATE_CHECKED,
    CHECK_STATE_UNCHECKED,
    ITEM_DATA_USER_ROLE,
    ITEM_FLAG_USER_CHECKABLE,
    MAP_LAYER_FILTER_LINE,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    layer_filters,
)
from . import dxf_io, sources

_HORIZONTAL = getattr(getattr(Qt, "Orientation", Qt), "Horizontal")
_OPEN, _CLOSED = "▾", "▸"
_GROUP_ROLE = int(getattr(ITEM_DATA_USER_ROLE, "value", ITEM_DATA_USER_ROLE)) + 1   # PyQt6 roles are enums
_NO_RPLS = "(no Workbench RPLs in this project)"

CABLE_LIBRARY = "library"
CABLE_SECTIONS = "sections"
CABLE_FIT = "fit"
CABLE_RPL = "rpl"
CABLE_MANUAL = "manual"
CABLE_CHOICES = (
    (CABLE_LIBRARY, "One cable type (library)"),
    (CABLE_SECTIONS, "Cable types by KP (library)"),
    (CABLE_RPL, "Workbench RPL cable type per leg (library)"),
    (CABLE_FIT, "Workbench assembly fitted to the RPL"),
    (CABLE_MANUAL, "Enter the cable properties"),
)
DIRECTIONS = ((1, "Increasing KP"), (-1, "Decreasing KP"))
# Short labels for the lay control sections table (its Control column is narrow).
SECTION_MODE_LABELS = {
    pl.TENSION: "Tension (kN)",
    pl.BOTTOM_SLACK: "Bottom slack (%)",
    pl.ROUTE_SLACK: "Route slack (%)",
    pl.RPL_LEGS: "RPL leg slack",
    pl.HELD: "Held (kN)",
}


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


def bar_button(text: str, tip: str, slot) -> QToolButton:
    """A compact text button for a row of table actions."""
    button = QToolButton()
    button.setText(text)
    button.setToolButtonStyle(getattr(getattr(Qt, "ToolButtonStyle", Qt), "ToolButtonTextOnly"))
    if tip:
        button.setToolTip(tip)
    button.clicked.connect(lambda *_: slot())
    return button


class EditTable(QWidget):
    """A small editable table with Add / Remove; rows are dicts keyed by column."""

    changed = pyqtSignal()

    def __init__(self, cols: Sequence[Col], add_text: str = "Add", parent=None, height: int = 120,
                 io: bool = True, title: str = "table"):
        super().__init__(parent)
        self.cols = list(cols)
        self.title = title
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
        # Tool buttons, not push buttons: the Windows style gives a push button
        # at least 75 px, and five of them make the setup pane scroll sideways.
        self.bar = QHBoxLayout()
        self.bar.setSpacing(4)
        self.add_button = bar_button(add_text, "", lambda: self.add_row())
        self.bar.addWidget(self.add_button)
        self.bar.addWidget(bar_button("Remove", "Remove the selected rows.", self.remove_selected))
        if io:
            for text, tip, slot in (
                    ("Paste", "Add rows copied from a spreadsheet (tab separated, same column order, header "
                              "optional).", self.paste),
                    ("Import…", "Add rows from a CSV file (a header row of the column names, or the same order).",
                     self.import_csv),
                    ("Export…", "Save the rows to a CSV file.", self.export_csv)):
                self.bar.addWidget(bar_button(text, tip, slot))
        self.bar.addStretch(1)
        layout.addLayout(self.bar)

    # -- import / export / paste ------------------------------------------------------
    def _value(self, col: "Col", text: str):
        text = str(text or "").strip()
        if col.kind == "float":
            try:
                return float(text.replace(",", ".")) if text else None
            except ValueError:
                return None
        if col.kind == "choice":
            low = text.lower()
            for data, label in col.choices:
                if str(data).lower() == low or str(label).lower() == low:
                    return data
        return text

    def rows_from_records(self, records: Sequence[Sequence[str]]) -> List[Dict]:
        """Rows from text records: a header of column keys or labels maps the columns, else their order."""
        records = [list(r) for r in records if any(str(c).strip() for c in r)]
        if not records:
            return []
        names = {_norm(c.key): i for i, c in enumerate(self.cols)}
        names.update({_norm(c.label): i for i, c in enumerate(self.cols)})
        first = [_norm(cell) for cell in records[0]]
        matched = [names.get(cell) for cell in first]
        if sum(m is not None for m in matched) >= max(1, len(first) // 2):
            order, body = matched, records[1:]
        else:
            order, body = list(range(len(self.cols))), records
        out = []
        for record in body:
            row = {}
            for position, cell in enumerate(record):
                index = order[position] if position < len(order) else None
                if index is not None and index < len(self.cols):
                    col = self.cols[index]
                    row[col.key] = self._value(col, cell)
            if row:
                out.append(row)
        return out

    def add_rows(self, rows: Sequence[Dict]) -> None:
        for row in rows:
            self.add_row(row)

    def paste(self) -> None:
        from qgis.PyQt.QtWidgets import QApplication

        text = QApplication.clipboard().text()
        if "\t" in text:
            records = [line.split("\t") for line in text.splitlines()]
        else:
            records = list(csv.reader(io.StringIO(text)))
        self.add_rows(self.rows_from_records(records))

    def import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"Import {self.title}", "", "CSV files (*.csv *.txt);;All files (*)")
        if not path:
            return
        with open(path, newline="", encoding="utf-8-sig") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            self.add_rows(self.rows_from_records(list(csv.reader(handle, dialect))))

    def export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, f"Export {self.title}", "", "CSV files (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([c.key for c in self.cols])
            for row in self.rows():
                writer.writerow(["" if row.get(c.key) is None else row.get(c.key) for c in self.cols])

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


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _source_key(source: str) -> str:
    """A layer source for matching (file paths compare without case on Windows)."""
    return os.path.normcase(str(source or "").strip())


MU_SLIDER_MAX = 1.5   # the friction slider's top (higher values are typed: rock can hold far more)


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
    frictionChanged = pyqtSignal()   # seabed friction only (live update: new friction on the same stations)
    libraryRequested = pyqtSignal()
    tensionFinderRequested = pyqtSignal()
    holdsRequested = pyqtSignal()    # fill held sections from the RPL's burial events or the burial plan

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = True    # no change signals while the pane is built
        self._library_rows: List[Dict] = []
        self._workbench = None
        self._mapping_labels: List[str] = []
        self._rpl_cache: Dict = {}
        self._wanted: Dict[str, str] = {}   # a scenario's rasters not in the project: layer id -> source
        self._kept_rpl = (None, None)       # (RPL id, fit id) chosen when a project last closed
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.route_section = Section("1  Route and KP", self._build_route(), True)
        self.seabed_section = Section("2  Seabed", self._build_seabed(), True)
        self.cable_section = Section("3  Cable and bodies", self._build_cable(), True)
        self.friction_section = Section("4  Seabed friction", self._build_friction())
        self.control_section = Section("5  Lay control (tension / slack / holds)", self._build_control(), True)
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
        refresh.setToolTip("Reload the Workbench RPLs and fits, the cable library and the project's layers (also "
                           "done each time the window opens).")
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
        self.line_start_kp = _spin(-100000.0, 100000.0, 0.0, 3, 0.1, " km",
                                   "KP at the route's first vertex. For a Workbench RPL it is filled from the RPL "
                                   "(its first position's stated KP); check it. KP = start KP + chainage.")
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
        self.dxf_box = QWidget()
        dxf_form = QFormLayout(self.dxf_box)
        dxf_form.setContentsMargins(0, 0, 0, 0)
        dxf_row = QWidget()
        dxf_row_layout = QHBoxLayout(dxf_row)
        dxf_row_layout.setContentsMargins(0, 0, 0, 0)
        self.dxf_path = QLineEdit()
        self.dxf_path.setPlaceholderText("A profile drawing: X along the route, Y elevation or depth")
        dxf_browse = QToolButton()
        dxf_browse.setText("…")
        dxf_browse.clicked.connect(self._browse_dxf)
        dxf_row_layout.addWidget(self.dxf_path, 1)
        dxf_row_layout.addWidget(dxf_browse)
        dxf_form.addRow("DXF file", dxf_row)
        self.dxf_layer = QComboBox()
        self.dxf_layer.setToolTip("The layer holding the seabed line (all its lines and polylines are joined).")
        dxf_form.addRow("Layer", self.dxf_layer)
        self.dxf_x = QComboBox()
        for key, label in dxf_io.X_CHOICES:
            self.dxf_x.addItem(label, key)
        dxf_form.addRow("X axis", self.dxf_x)
        self.dxf_start_kp = _spin(-100000.0, 100000.0, 0.0, 3, 0.1, " km", "KP at the profile's first point.")
        dxf_form.addRow("Start KP", self.dxf_start_kp)
        self.dxf_y = QComboBox()
        for key, label in dxf_io.Y_CHOICES:
            self.dxf_y.addItem(label, key)
        dxf_form.addRow("Y axis", self.dxf_y)
        self.dxf_ve = _spin(0.01, 1000.0, 1.0, 2, 1.0, "", "Vertical exaggeration of the drawing (removed).")
        dxf_form.addRow("Vertical exaggeration", self.dxf_ve)
        self.dxf_offset = _spin(-20000.0, 20000.0, 0.0, 2, 1.0, " m",
                                "Added to the elevation (or depth) after the exaggeration is removed: the drawing's "
                                "datum offset.")
        dxf_form.addRow("Datum offset", self.dxf_offset)
        self.dxf_message = QLabel("")
        self.dxf_message.setWordWrap(True)
        self.dxf_message.setStyleSheet("color: #d32f2f;")
        dxf_form.addRow(self.dxf_message)
        form.addRow(self.dxf_box)
        self.dxf_path.editingFinished.connect(self._load_dxf_layers)
        for widget in (self.dxf_layer, self.dxf_x, self.dxf_y):
            widget.currentIndexChanged.connect(self._emit_changed)
        for widget in (self.dxf_start_kp, self.dxf_ve, self.dxf_offset):
            widget.valueChanged.connect(self._emit_changed)
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

    def _browse_dxf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Seabed profile (DXF)", self.dxf_path.text(), "DXF (*.dxf)")
        if path:
            self.dxf_path.setText(path)
            self._load_dxf_layers()

    def _load_dxf_layers(self, current: str = "") -> None:
        from .dxf_dialogs import fill_layers

        self.dxf_layer.blockSignals(True)
        self.dxf_message.setText(fill_layers(self.dxf_path.text().strip(), self.dxf_layer, current))
        self.dxf_layer.blockSignals(False)
        self._emit_changed()

    def dxf_options(self):
        return dxf_io.ProfileDxfOptions(self.dxf_layer.currentData() or "", self.dxf_x.currentData(),
                                        self.dxf_start_kp.value(), self.dxf_y.currentData(), self.dxf_ve.value(),
                                        self.dxf_offset.value())

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
        self.raster_search = QLineEdit()
        self.raster_search.setPlaceholderText("Search rasters or groups…")
        self.raster_search.setClearButtonEnabled(True)
        self.raster_search.textChanged.connect(self._filter_rasters)
        self.raster_list = QListWidget()
        self.raster_list.setToolTip("Tick the MBES grids to sample. Where several cover a station the finest is "
                                    "used (as in the Depth Profile tool). The list follows the project's layers.")
        self.raster_list.itemChanged.connect(self._on_raster_ticked)
        raster_bar = QHBoxLayout()
        raster_bar.setSpacing(4)
        raster_bar.addWidget(bar_button("Along the route", "Tick the rasters whose extent the route crosses (within "
                                                           "the KP window) and untick the rest.",
                                        self.tick_rasters_along_route))
        raster_bar.addWidget(bar_button("All", "Tick every raster listed (the search narrows the list).",
                                        lambda: self._set_raster_ticks(lambda _layer_id: True, shown_only=True)))
        raster_bar.addWidget(bar_button("None", "Untick every raster listed (the search narrows the list).",
                                        lambda: self._set_raster_ticks(lambda _layer_id: False, shown_only=True)))
        raster_bar.addStretch(1)
        self.raster_count = QLabel("")
        self.raster_count.setStyleSheet("color: #555;")
        raster_bar.addWidget(self.raster_count)
        self.raster_box = QWidget()
        raster_layout = QVBoxLayout(self.raster_box)
        raster_layout.setContentsMargins(0, 0, 0, 0)
        raster_layout.setSpacing(2)
        raster_layout.addWidget(self.raster_search)
        raster_layout.addWidget(self.raster_list)
        raster_layout.addLayout(raster_bar)
        form.addRow("Rasters", self.raster_box)
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
        sampling_body = QWidget()
        sampling = QFormLayout(sampling_body)
        sampling.setContentsMargins(14, 0, 0, 0)
        self.interval = QSpinBox()
        self.interval.setRange(0, 500)
        self.interval.setSpecialValueText("auto (grid cell size)")
        self.interval.setSuffix(" m")
        self.interval.setToolTip("Station spacing along the route. Auto samples each raster at its own cell size "
                                 "(at least 1 m): coarser sampling misses crests and shortens the seabed.")
        self.interval.valueChanged.connect(self._emit_changed)
        sampling.addRow("Sample every", self.interval)
        self.filter_m = _spin(0.0, 50.0, 2.0, 2, 0.5, " m",
                              "Running mean the cable rests on: removes sounding noise (which would make false "
                              "spans between noise peaks) but keeps the crests. 0 for clean or synthetic data.")
        self.filter_m.valueChanged.connect(self._emit_changed)
        self.filter_auto = QCheckBox("auto")
        self.filter_auto.setChecked(True)
        self.filter_auto.setToolTip("Suit the filter to the data: average just enough data points that the "
                                    "point-to-point variation measured in the data, once averaged, is at most a "
                                    "quarter of the reporting gap, over at most two data intervals and half the "
                                    "reporting length, so relief at the scale of the spans you report is kept. None "
                                    "when the data is smooth enough, or coarser than that (its cells are already "
                                    "averages). The run states the value used.")
        self.filter_auto.toggled.connect(self._on_filter_auto)
        filter_row = QWidget()
        filter_layout = QHBoxLayout(filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.addWidget(self.filter_auto)
        filter_layout.addWidget(self.filter_m, 1)
        sampling.addRow("Noise filter", filter_row)
        self.smoothing = QLineEdit()
        self.smoothing.setPlaceholderText("auto: the cable's conformity length 2 (EI/w)^(1/3)")
        self.smoothing.setToolTip("Smoothing of the seabed for lengths (fill and slack): noise adds length. "
                                  "Blank: the conformity length (10 m without EI).")
        self.smoothing.textChanged.connect(self._emit_changed)
        sampling.addRow("Smoothing for lengths", self.smoothing)
        self.resolve = _spin(0.0, 20.0, RESOLVE_INTERVALS, 1, 0.5, " data intervals",
                             "A span needs at least this many intervals of the seabed data (raster cells, contour "
                             "crossings, profile points) to be resolved: two to show a sag between its supports, "
                             "and each support (a crest) is placed only to within half an interval. Shorter spans "
                             "are indicative (light blue), not red or amber, and stretches where the data "
                             "cannot resolve spans as short as the reporting length are flagged. Spans at a seabed "
                             "feature or in-line body you entered are exact. 0: off.")
        self.resolve.setSpecialValueText("off")
        self.resolve.valueChanged.connect(self._emit_changed)
        sampling.addRow("Spans need at least", self.resolve)
        self.data_note = QLabel("")
        self.data_note.setWordWrap(True)
        self.data_note.setStyleSheet("color: #555;")
        sampling.addRow(self.data_note)
        suit = bar_button("Suit the data", "Sample every: auto, noise filter: auto, smoothing for lengths: auto, "
                                           f"spans need {RESOLVE_INTERVALS:g} data intervals. The reporting limits "
                                           "(your project's criteria) are not changed.", self.suit_the_data)
        suit_row = QWidget()
        suit_layout = QHBoxLayout(suit_row)
        suit_layout.setContentsMargins(0, 0, 0, 0)
        suit_layout.addWidget(suit)
        suit_layout.addStretch(1)
        sampling.addRow(suit_row)
        self.sampling_section = Section("Sampling and filtering", sampling_body, False)
        form.addRow(self.sampling_section)
        self._on_filter_auto(True)
        kinds = [(k, FEATURE_LABELS[k]) for k in FEATURE_KINDS]
        features_body = QWidget()
        features_layout = QVBoxLayout(features_body)
        features_layout.setContentsMargins(14, 0, 0, 0)
        self.features = EditTable([
            Col("kind", "Feature", "choice", "boulder", kinds),
            Col("kp", "KP", "float", None, tip="Centre KP (km)."),
            Col("height", "Height / depth (m)", "float", 1.0,
                tip="Boulder / berm: height above the seabed. Pipeline: top of pipe above the seabed. "
                    "Depression: depth below the seabed."),
            Col("length", "Length / OD (m)", "float", 2.0,
                tip="Boulder / berm: base length along the route. Pipeline: outside diameter (with coating). "
                    "Depression: width at the seabed."),
            Col("crest", "Crest / bottom (m)", "float", 0.0,
                tip="Berm / mattress: length of the flat top. Depression: width of the flat bottom."),
            Col("shape", "Custom shape (m)", "text", "",
                tip="Other (custom shape): points 'offset height' from the KP along the route, separated by ';', "
                    "e.g. '-10 0; -5.5 1.5; -2 1.8; 2 1.8; 5.5 1.5; 10 0' for a rock berm. Straight lines between "
                    "points; negative heights cut into the seabed. Height and length are not used."),
            Col("name", "Name", "text", ""),
        ], "Add feature", height=90, title="seabed features")
        self.features.changed.connect(self._emit_changed)
        note = QLabel("Features sit on the seabed at their KP. Several features combine (the highest point, or "
                      "the lowest for depressions). For a complex shape such as a rock berm, choose Other and give "
                      "its points, paste them from a spreadsheet, or take them from a DXF cross-section.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        features_layout.addWidget(note)
        features_layout.addWidget(self.features)
        dxf_shape = bar_button("Shape from DXF…", "Set the selected feature (or a new one) to a cross-section "
                                                  "drawn in a DXF file.", self._shape_from_dxf)
        shape_row = QHBoxLayout()
        shape_row.addWidget(dxf_shape)
        shape_row.addStretch(1)
        features_layout.addLayout(shape_row)
        self.features_section = Section("Additional features", features_body, False)
        form.addRow(self.features_section)
        self.seabed_source.currentIndexChanged.connect(self._on_route_changed)
        return body

    def _shape_from_dxf(self) -> None:
        from .dxf_dialogs import DxfShapeDialog
        from ..cable_seabed.features import format_shape
        from ..qgis_compat import DIALOG_ACCEPTED, qt_exec

        dialog = DxfShapeDialog(self)
        if qt_exec(dialog) != DIALOG_ACCEPTED or not dialog.points:
            return
        table = self.features.table
        rows = sorted({index.row() for index in table.selectedIndexes()})
        if not rows:
            self.features.add_row({"kind": "custom", "name": "DXF shape"})
            rows = [table.rowCount() - 1]
        row = rows[0]
        combo = table.cellWidget(row, 0)
        if combo is not None:
            combo.setCurrentIndex(max(combo.findData("custom"), 0))
        shape_col = next(i for i, c in enumerate(self.features.cols) if c.key == "shape")
        table.setItem(row, shape_col, QTableWidgetItem(format_shape(dialog.points)))

    def _on_filter_auto(self, on: bool) -> None:
        self.filter_m.setEnabled(not on)
        self._emit_changed()

    def filter_setting(self) -> Optional[float]:
        """The noise filter for the engine: None when it is suited to the data."""
        return None if self.filter_auto.isChecked() else float(self.filter_m.value())

    def suit_the_data(self) -> None:
        """Sampling and filtering back to the settings suited to the data (the reporting limits stay)."""
        self.interval.setValue(0)
        self.filter_auto.setChecked(True)
        self.smoothing.setText("")
        self.resolve.setValue(RESOLVE_INTERVALS)
        self.sampling_section.button.setChecked(True)
        self._emit_changed()

    def show_data(self, stations) -> None:
        """After a run: what the seabed data resolves and the filter used (shown under the settings)."""
        if stations is None or stations.data_m is None or not np.isfinite(stations.data_m).any():
            self.data_note.setText("")
            return
        spacing = float(np.nanmedian(stations.data_m))
        parts = [f"Last run: data every {spacing:.2g} m"]
        if math.isfinite(stations.noise_m):
            parts.append(f"point-to-point variation σ {stations.noise_m:.2f} m")
        filtered = f"{stations.filter_m:.2g} m" if stations.filter_m > 0 else "none"
        parts.append(f"noise filter {filtered}" + (" (auto)" if stations.filter_auto else ""))
        if self.resolve.value() > 0:
            parts.append(f"spans resolved from about {self.resolve.value() * spacing:.2g} m")
        self.data_note.setText("; ".join(parts) + ".")
        if stations.filter_auto:
            self.filter_m.blockSignals(True)
            self.filter_m.setValue(float(stations.filter_m))   # shows the value used (disabled while auto)
            self.filter_m.blockSignals(False)

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
        ], "Add section", height=80, title="cable sections")
        self.cable_sections.changed.connect(self._emit_changed)
        form.addRow(self.cable_sections)
        self.fit_combo = QComboBox()
        self.fit_combo.setToolTip("An assembly fitted to the RPL in the Workbench: its sections' cable types and "
                                  "its bodies land at KPs through the RPL's slack.")
        self.fit_combo.currentIndexChanged.connect(self._on_fit_changed)
        form.addRow("Assembly fit", self.fit_combo)
        self.mapping_label = QLabel("Cable type labels in the Workbench and the library type each means in this "
                                    "project (saved with the project; the Lay Assessment uses the same mapping). "
                                    "Map a label that several library types could mean.")
        self.mapping_label.setWordWrap(True)
        self.mapping_label.setStyleSheet("color: #555;")
        form.addRow(self.mapping_label)
        self.mapping = QTableWidget(0, 3)
        self.mapping.setHorizontalHeaderLabels(["Label", "Found by", "Library type (this project)"])
        self.mapping.verticalHeader().setVisible(False)
        self.mapping.horizontalHeader().setStretchLastSection(True)
        self.mapping.setMinimumHeight(70)
        self.mapping.setMaximumHeight(150)
        form.addRow(self.mapping)
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
        ], "Add body", height=80, title="bodies")
        self.bodies.changed.connect(self._emit_changed)
        bodies_body = QWidget()
        bodies_layout = QVBoxLayout(bodies_body)
        bodies_layout.setContentsMargins(14, 0, 0, 0)
        note = QLabel("Repeaters, joints and branching units by KP (in addition to a fitted assembly's): weight in "
                      "water, rigid length and housing diameter.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        bodies_layout.addWidget(note)
        bodies_layout.addWidget(self.bodies)
        self.bodies_section = Section("In-line bodies", bodies_body, False)
        form.addRow(self.bodies_section)
        protection_body = QWidget()
        protection_layout = QVBoxLayout(protection_body)
        protection_layout.setContentsMargins(14, 0, 0, 0)
        note = QLabel("Protection over a KP range (e.g. polymer half-shells or articulated pipe) adds its weight in "
                      "water to the cable's, can add bending stiffness, and sets its own minimum bend radius where "
                      "that is larger. Take the values from the protection's data sheet.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        protection_layout.addWidget(note)
        self.protection = EditTable([
            Col("name", "Name", "text", "Protection"),
            Col("kind", "Kind", "text", "Shells", tip="Friction table key (e.g. Shells, Articulated pipe). With no "
                                                      "row for it there, the cable's friction applies."),
            Col("kp0", "KP from", "float"),
            Col("kp1", "KP to", "float"),
            Col("weight", "Added weight in water (kg/m)", "float", None),
            Col("diameter", "Outside diameter (mm)", "float", None,
                tip="Over the protection. Shown on the plot; a body's housing standoff is taken from it."),
            Col("ei", "Added EI (kN·m²)", "float", None,
                tip="Blank: no added stiffness (e.g. shells that turn on each other)."),
            Col("mbr", "MBR (m)", "float", None,
                tip="The protection's own minimum bend (locking) radius. Blank: the cable's."),
        ], "Add protection", height=70, title="protection")
        self.protection.changed.connect(self._emit_changed)
        protection_layout.addWidget(self.protection)
        self.protection_section = Section("Protection", protection_body, False)
        form.addRow(self.protection_section)
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
        self.mu_slider = QSlider(_HORIZONTAL)
        self.mu_slider.setRange(0, int(round(MU_SLIDER_MAX * 100)))
        self.mu_slider.setToolTip(tip + f" The slider covers 0 to {MU_SLIDER_MAX:g}; type a higher value.")
        self.mu_slider.setValue(int(round(pl.DEFAULT_MU * 100)))
        mu_row = QHBoxLayout()
        mu_row.setContentsMargins(0, 0, 0, 0)
        mu_row.addWidget(self.mu)
        mu_row.addWidget(self.mu_slider, 1)
        self.mu_row = QWidget()
        self.mu_row.setLayout(mu_row)
        self.mu_lat = _spin(0.0, 5.0, pl.DEFAULT_MU, 2, 0.05, "",
                            "Lateral friction. Used only for the alter-course note (the turn radius the cable holds "
                            "at its tension): the model is 2D, in the vertical plane along the route, and never "
                            "moves the cable sideways.")
        self.mu.valueChanged.connect(self._on_mu)
        self.mu_slider.valueChanged.connect(lambda position: self.mu.setValue(position / 100.0))
        self.mu_lat.valueChanged.connect(self._emit_friction)
        form.addRow("Axial μ (all)", self.mu_row)
        form.addRow("Lateral μ (alter-course note)", self.mu_lat)
        # The same setting as the lay control's box: friction updates the results as it changes too.
        self.live_friction = QCheckBox("Update the results as values change")
        self.live_friction.setChecked(True)
        self.live_friction.setToolTip("Solve again as μ, the soil zones or the friction table change (and the lay "
                                      "control), without sampling the seabed again.")
        form.addRow(self.live_friction)
        self.use_zones = QCheckBox("Soil zones and friction table")
        self.use_zones.toggled.connect(self._update_visibility)
        self.use_zones.toggled.connect(self._emit_friction)
        form.addRow(self.use_zones)
        self.zones = EditTable([Col("kp0", "KP from", "float"), Col("kp1", "KP to", "float"),
                                Col("soil", "Soil", "text", "Sand")], "Add zone", height=70, title="soil zones")
        self.friction_table = EditTable([
            Col("item", "Cable type / body kind", "text", "*", tip="A library cable type name, a body kind or a "
                                                                   "protection kind; * for any."),
            Col("soil", "Soil", "text", "Sand", tip="A soil name used in the zones; * for any."),
            Col("mu", "Axial μ", "float", 0.5), Col("mu_lat", "Lateral μ", "float", None),
        ], "Add row", height=70, title="friction table")
        for table in (self.zones, self.friction_table):
            table.changed.connect(self._emit_friction)
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
        self.live = QCheckBox("Update the results as values change")
        self.live.setChecked(True)
        self.live.setToolTip("Solve again as the lay control or the seabed friction changes, without sampling the "
                             "seabed again (about a second).")
        form.addRow(self.live)
        self.live.toggled.connect(self.live_friction.setChecked)
        self.live_friction.toggled.connect(self.live.setChecked)
        self.direction = QComboBox()
        for key, label in DIRECTIONS:
            self.direction.addItem(label, key)
        self.direction.setToolTip("The direction the cable is laid. Friction holds the tension each stretch was "
                                  "laid at, so on slopes the result depends on it.")
        self.direction.currentIndexChanged.connect(self._emit_control)
        form.addRow("Lay direction", self.direction)
        modes = [(k, SECTION_MODE_LABELS[k]) for k in pl.SECTION_MODES]
        self.control_sections = EditTable(
            [Col("kp0", "KP from", "float"), Col("kp1", "KP to", "float"),
             Col("mode", "Control", "choice", pl.TENSION, modes,
                 "Bottom tension or slack, as above; or Held: the cable is held there (buried by a plough, "
                 "clamped or anchored)."),
             Col("value", "Value", "float", 5.0, tip="kN for a tension or a hold (the tension it is laid and held "
                                                      "at, e.g. the plough tension at PLDN); % for slack."),
             Col("name", "Name", "text", "", tip="Optional, e.g. Plough 1 or First-end anchor: shown on the "
                                                 "profile and in the Holds tab.")],
            "Add section", height=80, title="lay control sections")
        metrics = self.control_sections.table.fontMetrics()
        for column, sample in enumerate(("KP from", "KP from", "Bottom slack (%)", "Value")):
            # Sized to their content, so all five columns fit the pane (a combo needs room for its arrow).
            pad = 40 if column == 2 else 16
            self.control_sections.table.setColumnWidth(column, metrics.horizontalAdvance(sample) + pad)
        self.control_sections.changed.connect(self._emit_control)
        holds_row = QHBoxLayout()
        holds_row.addWidget(bar_button("Hold plough sections…", "Add held sections over the plough (PLDN to PLUP) "
                                       "sections of the RPL's events or of the Burial Planner plan, at a tension "
                                       "you choose.", self.holdsRequested.emit))
        holds_row.addStretch(1)
        sections_body = QWidget()
        sections_layout = QVBoxLayout(sections_body)
        sections_layout.setContentsMargins(14, 0, 0, 0)
        note = QLabel("A tension or slack over a KP range, in place of the value above (e.g. less tension over "
                      "rough ground). <b>Held</b>: the cable is held over the range, e.g. buried by a plough (KP to "
                      "= KP from for a point, e.g. an anchor). It is laid at the tension in Value, which pulls back "
                      "on the cable laid before it; it does not span or slide, and nothing passes through it.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        sections_layout.addWidget(note)
        sections_layout.addWidget(self.control_sections)
        sections_layout.addLayout(holds_row)
        self.control_sections_section = Section("By KP section", sections_body, False)
        form.addRow(self.control_sections_section)
        finder = bar_button("Find the highest tension within limits…",
                            "Search the bottom tension that keeps the spans within a target (Find tension tab).",
                            self.tensionFinderRequested.emit)
        form.addRow(finder)
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

    def _on_mu(self, value: float) -> None:
        self.mu_slider.blockSignals(True)
        self.mu_slider.setValue(int(round(min(max(value, 0.0), MU_SLIDER_MAX) * 100)))
        self.mu_slider.blockSignals(False)
        self._emit_friction()

    # -- criteria ---------------------------------------------------------------------------
    def _build_criteria(self) -> QWidget:
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(14, 0, 0, 0)
        c = pl.Criteria()
        self.crit = {
            "min_gap_m": _spin(0.0, 50.0, c.min_gap_m, 2, 0.05, " m"),
            "min_length_m": _spin(0.0, 1000.0, c.min_length_m, 1, 1.0, " m"),
            "touchdown_gap_m": _spin(0.0, 1.0, c.touchdown_gap_m, 3, 0.01, " m"),
            "red_gap_m": _spin(0.0, 50.0, c.red_gap_m, 2, 0.1, " m"),
            "red_length_m": _spin(0.0, 5000.0, c.red_length_m, 1, 5.0, " m"),
            "max_reaction_kn": _spin(0.0, 10000.0, c.max_reaction_kn, 2, 0.5, " kN"),
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
            "touchdown_gap_m": ("Span ends where the gap is under",
                                "Where a span starts and ends. 0: the model's exact lift-off and touchdown (the span "
                                "length of pipeline practice, DNV-RP-F105). A larger value counts the cable as "
                                "touching where it is within this gap of the seabed: shorter spans, e.g. to compare "
                                "with a survey's gap detection limit. Near a touchdown the gap grows as w d² / 2T, so "
                                "each end moves in by about √(2 gap T / w)."),
            "red_gap_m": ("Red when higher than", ""),
            "red_length_m": ("…or longer than", "Spans are also red where the bend at a support is below the MBR."),
            "max_reaction_kn": ("Contact limit at a support",
                                "Flag supports where the seabed pushes on the cable harder than this (and make their "
                                "spans red). The reaction is a point load from the 2D model on a rigid seabed: compare "
                                "it with the crush rating of the cable (or its protection) over the bearing length "
                                "the data sheet uses (often per 100 mm). Detail (EI) gives a closer value. 0: off."),
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
        self.crit["touchdown_gap_m"].setSpecialValueText("exact (0 m)")
        self.crit["max_reaction_kn"].setSpecialValueText("off")
        for key, spin in self.crit.items():
            label, tip = labels[key]
            if tip:
                spin.setToolTip(tip)
            spin.valueChanged.connect(self._emit_changed)
            form.addRow(label, spin)
        self.allowable = QCheckBox("Find the highest tension that keeps spans within limits")
        self.allowable.setChecked(True)
        self.allowable.setToolTip("Bisection on a constant tension in windows of 1/40 of the route, 25 to 250 m "
                                  "(frictionless seabed).")
        self.allowable.toggled.connect(self._emit_changed)
        form.addRow(self.allowable)
        return body

    # -- sources ------------------------------------------------------------------------------
    def refresh_sources(self) -> None:
        """Reload the Workbench RPLs and fits, the library types and the raster layers.

        Emits ``changed`` when that changes a setting (e.g. a ticked raster left the project).
        """
        before = json.dumps(self.settings(), sort_keys=True, default=str)
        self._loading = True
        try:
            self._workbench = sources.open_workbench()
            kept_rpl, kept_fit = self._kept_rpl
            current = self.rpl_combo.currentData() or kept_rpl
            current_fit = self.fit_combo.currentData() or kept_fit
            self.rpl_combo.clear()
            if self._workbench is not None:
                for rpl_id, label in sources.rpl_choices(self._workbench):
                    self.rpl_combo.addItem(label, rpl_id)
            if self.rpl_combo.count() == 0:
                self.rpl_combo.addItem(_NO_RPLS, None)
            index = self.rpl_combo.findData(current)
            self.rpl_combo.setCurrentIndex(max(index, 0))
            self._refresh_fits(current_fit)
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
            self._rpl_cache = {}
            self.refresh_mapping()
        finally:
            self._loading = False
        if json.dumps(self.settings(), sort_keys=True, default=str) != before:
            self._emit_changed()
        else:
            self.update_status()

    def forget_project(self) -> None:
        """The project is closing: drop its Workbench and the RPLs and fits listed from it, which
        cannot be used any more. The next project's are listed by refresh_sources, which keeps
        the choice when the same RPL is there (the same project opened again)."""
        self._workbench = None
        self._rpl_cache = {}
        self._loading = True
        try:
            if self.rpl_combo.currentData():
                self._kept_rpl = (self.rpl_combo.currentData(), self.fit_combo.currentData())
            self.rpl_combo.clear()
            self.rpl_combo.addItem(_NO_RPLS, None)
            self._refresh_fits()
            self.refresh_mapping()
        finally:
            self._loading = False
        self.update_status()

    def refresh_layers(self) -> None:
        """The project's layers changed: list its rasters again."""
        before = sorted(self._raster_setting())
        self._refresh_rasters()
        if sorted(self._raster_setting()) != before:
            self._emit_changed()
        else:
            self.update_status()

    def _refresh_fits(self, wanted=None) -> None:
        current = wanted or self.fit_combo.currentData()
        self.fit_combo.clear()
        rpl_id = self.rpl_combo.currentData()
        if self._workbench is not None and rpl_id:
            for fit_id, label in sources.fit_choices(self._workbench, rpl_id):
                self.fit_combo.addItem(label, fit_id)
        if self.fit_combo.count() == 0:
            self.fit_combo.addItem("(no assembly fitted to this RPL)", None)
        index = self.fit_combo.findData(current)
        self.fit_combo.setCurrentIndex(max(index, 0))

    def _refresh_rasters(self, checked: Optional[Sequence[str]] = None,
                         saved_sources: Optional[Sequence[str]] = None) -> None:
        """List the project's rasters (Layers panel order), keeping the ticks.

        ``checked`` / ``saved_sources``: a scenario's rasters, by layer id and
        source. A raster removed and added again (a new layer id) is found by
        its source; one not in the project is kept in the settings, and ticked
        when it is added, until the ticks are changed.
        """
        if checked is None:
            checked = self.checked_raster_ids()
        else:
            sources_ = list(saved_sources or [])
            self._wanted = {str(i): str(sources_[n]) if n < len(sources_) else ""
                            for n, i in enumerate(checked) if i}
        ticked = set(checked)
        by_source = {_source_key(s): i for i, s in self._wanted.items() if s}
        self.raster_list.blockSignals(True)
        self.raster_list.clear()
        for layer in sources.raster_layers():
            on = layer.id() in ticked or layer.id() in self._wanted
            self._wanted.pop(layer.id(), None)
            match = by_source.pop(_source_key(layer.source()), None)
            if match is not None:
                on = True
                self._wanted.pop(match, None)
            item = QListWidgetItem(layer.name())
            item.setData(ITEM_DATA_USER_ROLE, layer.id())
            group = layer_group_path(layer)
            item.setData(_GROUP_ROLE, group.lower())
            item.setToolTip("\n".join(t for t in (group, layer.publicSource()) if t))
            item.setFlags(item.flags() | ITEM_FLAG_USER_CHECKABLE)
            item.setCheckState(CHECK_STATE_CHECKED if on else CHECK_STATE_UNCHECKED)
            self.raster_list.addItem(item)
        self.raster_list.blockSignals(False)
        self._filter_rasters()

    def _filter_rasters(self, *_args) -> None:
        """Show the rasters the search matches; size the list to them (up to eight rows)."""
        count = self.raster_list.count()
        self.raster_search.setVisible(count > 8)
        words = self.raster_search.text().lower().split() if count > 8 else []
        shown = 0
        for i in range(count):
            item = self.raster_list.item(i)
            text = f"{item.text().lower()} {item.data(_GROUP_ROLE) or ''}"
            item.setHidden(not all(word in text for word in words))
            shown += not item.isHidden()
        row = self.raster_list.sizeHintForRow(0) if count else 18
        self.raster_list.setFixedHeight(min(max(shown, 2), 8) * max(row, 14) + 2 * self.raster_list.frameWidth() + 2)
        self._update_raster_count()

    def _update_raster_count(self) -> None:
        count = self.raster_list.count()
        ticked = len(self.checked_raster_ids())
        self.raster_count.setText(f"{ticked} of {count} ticked" if count else "no rasters in the project")

    def _on_raster_ticked(self, *_args) -> None:
        self._wanted = {}   # the user chose: a scenario's missing rasters are dropped
        self._update_raster_count()
        self._emit_changed()

    def _set_raster_ticks(self, tick, shown_only: bool = False) -> None:
        """Tick the listed rasters for which ``tick(layer_id)`` is true, untick the rest."""
        self.raster_list.blockSignals(True)
        for i in range(self.raster_list.count()):
            item = self.raster_list.item(i)
            if not (shown_only and item.isHidden()):
                item.setCheckState(CHECK_STATE_CHECKED if tick(item.data(ITEM_DATA_USER_ROLE))
                                   else CHECK_STATE_UNCHECKED)
        self.raster_list.blockSignals(False)
        self._on_raster_ticked()

    def tick_rasters_along_route(self) -> None:
        """Tick the rasters the route (within the KP window) crosses."""
        try:
            route = self.route_line()
        except ValueError as exc:
            self.seabed_section.set_status(str(exc), False)
            return
        listed = [QgsProject.instance().mapLayer(self.raster_list.item(i).data(ITEM_DATA_USER_ROLE))
                  for i in range(self.raster_list.count())]
        crossed = set(sources.rasters_crossed(route, [layer for layer in listed if layer is not None]))
        self._set_raster_ticks(lambda layer_id: layer_id in crossed)
        if crossed:
            self.seabed_section.set_status(f"{len(crossed)} of {len(listed)} raster(s) cross the route", True)
        else:
            self.seabed_section.set_status("No raster crosses the route" + (" in the KP window"
                                                                            if self.kp_window() else ""), False)

    def route_line(self) -> sources.RouteGeo:
        """The route on the map (an RPL or a line layer), cut to the KP window (raises ValueError)."""
        source = self.route_source.currentData()
        if source == sources.ROUTE_RPL:
            model = self.rpl_model()
            if model is None:
                raise ValueError("Choose a Workbench RPL first.")
            route = sources.route_from_rpl_model(sources.with_start_kp(model, self.line_start_kp.value()))
        elif source == sources.ROUTE_LINE:
            route = sources.route_from_layer(self.line_combo.currentLayer(), self.line_selected.isChecked(),
                                             self.line_start_kp.value())
        else:
            raise ValueError("The route is not on the map.")
        window = self.kp_window()
        if window is not None:
            route = route.sub_route(max(window[0], route.kp_range[0]), min(window[1], route.kp_range[1]))
        return route

    def checked_raster_ids(self) -> List[str]:
        out = []
        for i in range(self.raster_list.count()):
            item = self.raster_list.item(i)
            if item.checkState() == CHECK_STATE_CHECKED:
                out.append(item.data(ITEM_DATA_USER_ROLE))
        return out

    def _raster_setting(self) -> List[str]:
        """The rasters in the settings: the ticked ones, then a scenario's that are not in the project."""
        ticked = self.checked_raster_ids()
        return ticked + [i for i in self._wanted if i not in ticked]

    def _raster_source(self, layer_id: str) -> str:
        layer = QgsProject.instance().mapLayer(layer_id)
        return layer.source() if layer is not None else self._wanted.get(layer_id, "")

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

    def _emit_friction(self, *_args) -> None:
        if self._loading:
            return
        self.update_status()
        self.changed.emit()
        self.frictionChanged.emit()

    def _on_route_changed(self, *_args) -> None:
        if self.sender() is self.rpl_combo:
            self._refresh_fits()
            self.refresh_mapping()
            self._prefill_rpl_start_kp()
        elif self.sender() is self.route_source and self.route_source.currentData() == sources.ROUTE_RPL:
            self._prefill_rpl_start_kp()
        self._update_visibility()
        self._emit_changed()

    def rpl_model(self):
        """The chosen Workbench RPL's model (cached), or None."""
        store, rpl_id = self._workbench, self.rpl_combo.currentData()
        if store is None or not rpl_id:
            return None
        if self._rpl_cache.get("id") != rpl_id:
            try:
                self._rpl_cache = {"id": rpl_id, "model": sources.load_rpl_model(store, rpl_id)}
            except Exception:
                return None
        return self._rpl_cache.get("model")

    def _prefill_rpl_start_kp(self) -> None:
        """An RPL route starts at the RPL's start KP (its first position's stated KP) unless changed."""
        if self._loading:
            return
        model = self.rpl_model()
        if model is not None:
            self.line_start_kp.setValue(float(model.start_kp_km()))

    def _on_cable_source(self, *_args) -> None:
        self._update_visibility()
        self.refresh_mapping()
        self._emit_changed()

    def _on_fit_changed(self, *_args) -> None:
        self.refresh_mapping()
        self._emit_changed()

    # -- cable type mapping ----------------------------------------------------------------
    def _labels(self) -> List[str]:
        """Cable type labels of the chosen Workbench source (RPL legs or the fitted assembly)."""
        cable = self.cable_source.currentData()
        store = self._workbench
        if store is None:
            return []
        try:
            if cable == CABLE_FIT and self.fit_combo.currentData():
                return sources.fit_labels(store, self.fit_combo.currentData())
            if cable == CABLE_RPL and self.rpl_combo.currentData():
                labels: List[str] = []
                for _k0, _k1, label in sources.rpl_leg_labels(self.rpl_model()):
                    if label not in labels:
                        labels.append(label)
                return labels
        except Exception:
            return []
        return []

    def _unmapped_labels(self) -> List[str]:
        return [label for label in self._mapping_labels
                if sources.resolve_label(self._library_rows, label)[0] is None]

    def refresh_mapping(self) -> None:
        """Rebuild the label -> library type table for the current Workbench source."""
        labels = self._labels() if self.cable_source.currentData() in (CABLE_FIT, CABLE_RPL) else []
        self._mapping_labels = labels
        how_text = {"mapped": "project mapping", "name": "library name", "alias": "library alias",
                    "generic": "generic type", "ambiguous": "AMBIGUOUS: choose a type",
                    "unknown": "not in the library", "none": ""}
        mapping = sources.project_mapping()
        names = [row.get("name") for row in self._library_rows if row.get("name")]
        self.mapping.setRowCount(len(labels))
        for r, label in enumerate(labels):
            row, how = sources.resolve_label(self._library_rows, label)
            self.mapping.setItem(r, 0, QTableWidgetItem(label))
            text = how_text.get(how, how)
            if row is not None and how != "mapped":
                text += f": {row.get('name')}"
            found = QTableWidgetItem(text)
            if row is None:
                found.setForeground(QColor("#d32f2f"))
            self.mapping.setItem(r, 1, found)
            combo = QComboBox()
            combo.addItem("(automatic)", None)
            first = sources.candidates(self._library_rows, label)
            for name in first + [n for n in names if n not in first]:
                combo.addItem(name + ("  (candidate)" if name in first else ""), name)
            current = mapping.get(_token(label))
            index = combo.findData(current) if current else 0
            combo.setCurrentIndex(max(index, 0))
            combo.currentIndexChanged.connect(lambda _i, lab=label, c=combo: self._on_mapping(lab, c))
            self.mapping.setCellWidget(r, 2, combo)
        self.mapping.resizeColumnsToContents()

    def _on_mapping(self, label: str, combo: QComboBox) -> None:
        sources.set_mapping(label, combo.currentData())
        self.refresh_mapping()
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
            (self.line_start_kp, route in (sources.ROUTE_LINE, sources.ROUTE_RPL)),
            (self.csv_row, route == sources.ROUTE_CSV),
            (self.dxf_box, route == sources.ROUTE_DXF),
            (self.syn_box, route == sources.ROUTE_SYNTHETIC),
            (self.window_row, on_map),
            (self.seabed_source, on_map),
            (self.raster_box, on_map and seabed == SEABED_RASTER),
            (self.contour_row, on_map and seabed == SEABED_CONTOURS),
            (self.interval, on_map and seabed in (SEABED_RASTER, SEABED_CONTOURS)),
            (self.type_row, cable in (CABLE_LIBRARY, CABLE_SECTIONS, CABLE_FIT, CABLE_RPL)),
            (self.cable_sections, cable == CABLE_SECTIONS),
            (self.fit_combo, cable == CABLE_FIT),
            (self.mapping_label, cable in (CABLE_FIT, CABLE_RPL)),
            (self.mapping, cable in (CABLE_FIT, CABLE_RPL)),
            (self.bodies_from_fit, cable == CABLE_FIT),
            (self.manual_box, cable == CABLE_MANUAL),
            (self.zones, zones), (self.friction_table, zones), (self.zones_label, zones), (self.table_label, zones),
        )
        for widget, visible in rows:
            _set_row_visible(widget, visible)
        label = _form_label(self.type_row)
        if label is not None:
            label.setText({CABLE_LIBRARY: "Cable type", CABLE_SECTIONS: "Default cable type",
                           CABLE_FIT: "Cable type outside the fit",
                           CABLE_RPL: "Default cable type"}.get(cable, "Cable type"))

    # -- readiness -----------------------------------------------------------------------------
    def update_status(self) -> None:
        route = self.route_source.currentData()
        if route == sources.ROUTE_RPL:
            ok = bool(self.rpl_combo.currentData())
            start = self.line_start_kp.value()
            self.route_section.set_status((self.rpl_combo.currentText() + (f", start KP {start:.3f}" if start else ""))
                                          if ok else "Needs a Workbench RPL", ok)
        elif route == sources.ROUTE_LINE:
            layer = self.line_combo.currentLayer()
            self.route_section.set_status(layer.name() if layer else "Needs a line layer", layer is not None)
        elif route == sources.ROUTE_CSV:
            ok = bool(self.csv_path.text().strip())
            self.route_section.set_status(self.csv_path.text().strip() if ok else "Needs a CSV file", ok)
        elif route == sources.ROUTE_DXF:
            ok = bool(self.dxf_path.text().strip()) and bool(self.dxf_layer.currentData())
            self.route_section.set_status(f"DXF layer {self.dxf_layer.currentData()}" if ok
                                          else "Needs a DXF file and layer", ok)
        else:
            self.route_section.set_status(f"Synthetic, {self.syn['length_m'].value():.0f} m", True)
        if route in (sources.ROUTE_RPL, sources.ROUTE_LINE):
            seabed = self.seabed_source.currentData()
            if seabed == SEABED_RASTER:
                n = len(self.checked_raster_ids())
                missing = len(self._wanted)
                text = f"{n} raster(s)" if n else "Tick the rasters (Along the route ticks those it crosses)"
                if missing:
                    text += f"; {missing} raster(s) of this scenario not in the project"
                self.seabed_section.set_status(text, n > 0 and not missing)
            elif seabed == SEABED_CONTOURS:
                ok = self.contour_combo.currentLayer() is not None and bool(self.contour_field.currentField())
                self.seabed_section.set_status("Contours" if ok else "Needs a contour layer and depth field", ok)
            else:
                self.seabed_section.set_status("RPL approximate depth (coarse: no relief)", True)
        else:
            self.seabed_section.set_status("From the profile", True)
        sampling = []
        if route in (sources.ROUTE_RPL, sources.ROUTE_LINE) and \
                self.seabed_source.currentData() in (SEABED_RASTER, SEABED_CONTOURS):
            sampling.append("auto spacing" if self.interval.value() == 0 else f"every {self.interval.value()} m")
        if self.filter_auto.isChecked():
            sampling.append("noise filter auto")
        else:
            sampling.append(f"noise filter {self.filter_m.value():g} m" if self.filter_m.value() > 0
                            else "no noise filter")
        if self.resolve.value() > 0:
            sampling.append(f"spans from {self.resolve.value():g} data intervals")
        else:
            sampling.append("resolution check off")
        if self.smoothing_m() is not None:
            sampling.append(f"smoothing {self.smoothing_m():g} m")
        self.sampling_section.set_status(", ".join(sampling), True)
        count = len(self.features.rows())
        self.features_section.set_status(f"{count} feature(s)" if count else "none", True)
        if count and route in (sources.ROUTE_RPL, sources.ROUTE_LINE):
            self.seabed_section.status.setText(self.seabed_section.status.text() + f", {count} feature(s)")
        cable = self.cable_source.currentData()
        if cable == CABLE_MANUAL:
            w = self.manual["w_kg_m"].value()
            self.cable_section.set_status(f"{w:g} kg/m in water" if w > 0 else "Needs the weight in water", w > 0)
        elif cable == CABLE_FIT:
            ok = bool(self.fit_combo.currentData())
            unmapped = self._unmapped_labels()
            text = self.fit_combo.currentText() if ok else "Needs a fitted assembly"
            if ok and unmapped:
                text += f"; map {', '.join(unmapped)}"
            self.cable_section.set_status(text, ok and not unmapped)
        elif cable == CABLE_RPL:
            ok = bool(self.rpl_combo.currentData()) and self.route_source.currentData() == sources.ROUTE_RPL
            unmapped = self._unmapped_labels()
            text = "RPL cable types" if ok else "Needs a Workbench RPL route"
            if ok and unmapped:
                text += f"; map {', '.join(unmapped)}"
            self.cable_section.set_status(text, ok and not unmapped)
        else:
            ok = bool(self.cable_type.currentData())
            self.cable_section.set_status(self.cable_type.currentText() if ok else "Needs a library cable type", ok)
        bodies = len(self.bodies.rows())
        self.bodies_section.set_status(f"{bodies} body(ies)" if bodies else "none", True)
        if bodies:
            self.cable_section.status.setText(self.cable_section.status.text() + f", {bodies} body(ies)")
        protected = len(self.protection.rows())
        self.protection_section.set_status(f"{protected} range(s)" if protected else "none", True)
        if protected:
            self.cable_section.status.setText(self.cable_section.status.text() + f", {protected} protected range(s)")
        self.friction_section.set_status(f"μ {self.mu.value():.2f}" + (", soil zones" if self.use_zones.isChecked()
                                                                        else ""), True)
        mode = self.mode.currentData()
        unit = pl.MODE_UNITS[mode]
        rows = self.control_sections.rows()
        sections = len(rows)
        held = sum(1 for row in rows if row.get("mode") == pl.HELD)
        text = f"{sections} section(s)" + (f", {held} held" if held else "")
        self.control_sections_section.set_status(text if sections else "none", True)
        self.control_section.set_status(f"{pl.MODE_LABELS[mode].split(' (')[0]} {self.value.value():.2f} {unit}"
                                        + (f", {text}" if sections else ""), True)
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
            sections.append(pl.ControlSection(row["kp0"], row["kp1"], row.get("mode") or pl.TENSION, row["value"],
                                              str(row.get("name") or "").strip()))
        return pl.LayControl(self.mode.currentData(), float(self.value.value()), sections, list(legs),
                             int(self.direction.currentData() or 1))

    def criteria(self) -> pl.Criteria:
        values = {key: float(spin.value()) for key, spin in self.crit.items()}
        return pl.Criteria(allowable=self.allowable.isChecked(), resolve_intervals=float(self.resolve.value()),
                           **values)

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
        """The seabed features (raises ValueError on a custom shape that does not read)."""
        from ..cable_seabed.features import CUSTOM, SeabedFeature, parse_shape

        out = []
        for number, row in enumerate(self.features.rows(), start=1):
            kind = row.get("kind") or "boulder"
            if row.get("kp") is None:
                continue
            if kind == CUSTOM:
                try:
                    points = parse_shape(row.get("shape") or "")
                except ValueError as exc:
                    raise ValueError(f"Additional feature {number}: {exc}") from exc
                if len(points) < 2:
                    raise ValueError(f"Additional feature {number} (custom) needs at least two shape points.")
                out.append(SeabedFeature(kind, row["kp"], 0.0, 0.0, 0.0, row.get("name") or "", points))
                continue
            if row.get("height") is None or row.get("length") is None:
                continue
            out.append(SeabedFeature(kind, row["kp"], row["height"], row["length"], row.get("crest") or 0.0,
                                     row.get("name") or ""))
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

    def protection_list(self) -> List[pl.Protection]:
        out = []
        for row in self.protection.rows():
            if row.get("kp0") is None or row.get("kp1") is None:
                continue

            def opt(key, scale=1.0, row=row):
                value = row.get(key)
                return float(value) * scale if value is not None and value > 0 else math.nan

            out.append(pl.Protection(row.get("name") or "Protection", row["kp0"], row["kp1"],
                                     weight_npm=float(row.get("weight") or 0.0) * 9.80665,
                                     diameter_m=opt("diameter", 0.001), ei_knm2=opt("ei"), mbr_m=opt("mbr"),
                                     kind=row.get("kind") or "Protection"))
        return out

    def apply_tension(self, value_kn: float, sections: Sequence[Tuple[float, float, float]] = ()) -> None:
        """Lay at a bottom tension (from the tension finder); ``sections`` as (KP from, KP to, kN).
        Held sections are kept."""
        _set_combo(self.mode, pl.TENSION)
        self.value.setValue(float(value_kn))
        if sections:
            held = [row for row in self.control_sections.rows() if row.get("mode") == pl.HELD]
            self.control_sections.set_rows(held + [{"kp0": a, "kp1": b, "mode": pl.TENSION, "value": v}
                                                   for a, b, v in sections])
            self.control_sections_section.button.setChecked(True)   # show what was set
        self.update_status()

    def add_holds(self, rows: Sequence[Dict], replace: bool = True) -> None:
        """Add held sections (``kp0``, ``kp1``, ``value``, ``name``); ``replace`` the held ones there."""
        kept = [row for row in self.control_sections.rows() if not (replace and row.get("mode") == pl.HELD)]
        self.control_sections.set_rows(kept + [dict(row, mode=pl.HELD) for row in rows])
        self.control_sections_section.button.setChecked(True)   # show what was added
        self.update_status()

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
        rasters = self._raster_setting()
        return {
            "version": 1,
            "route": {
                "source": self.route_source.currentData(),
                "rpl_id": self.rpl_combo.currentData(),
                "line_layer": line.id() if line else None,
                "line_selected": self.line_selected.isChecked(),
                "start_kp": self.line_start_kp.value(),
                "start_kp_set": True,
                "csv_path": self.csv_path.text().strip(),
                "dxf": {"path": self.dxf_path.text().strip(), "layer": self.dxf_layer.currentData(),
                        "x_mode": self.dxf_x.currentData(), "start_kp": self.dxf_start_kp.value(),
                        "y_mode": self.dxf_y.currentData(), "ve": self.dxf_ve.value(),
                        "offset": self.dxf_offset.value()},
                "synthetic": {**{k: w.value() for k, w in self.syn.items()}, "seed": self.syn_seed.value()},
                "kp_from": self.kp_from.text().strip(),
                "kp_to": self.kp_to.text().strip(),
            },
            "seabed": {
                "source": self.seabed_source.currentData(),
                "rasters": rasters,
                "raster_sources": [self._raster_source(i) for i in rasters],
                "contour_layer": contour.id() if contour else None,
                "contour_field": self.contour_field.currentField(),
                "interval": self.interval.value(),
                "filter_m": self.filter_m.value(),
                "filter_auto": self.filter_auto.isChecked(),
                "resolve_intervals": self.resolve.value(),
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
                "protection": self.protection.rows(),
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
        rpl_start_from_rpl = False
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
            rpl_start_from_rpl = route.get("source") == sources.ROUTE_RPL and not route.get("start_kp_set")
            self.csv_path.setText(route.get("csv_path") or "")
            dxf = route.get("dxf") or {}
            self.dxf_path.setText(dxf.get("path") or "")
            _set_combo(self.dxf_x, dxf.get("x_mode"))
            _set_combo(self.dxf_y, dxf.get("y_mode"))
            self.dxf_start_kp.setValue(float(dxf.get("start_kp") or 0.0))
            self.dxf_ve.setValue(float(dxf.get("ve") or 1.0))
            self.dxf_offset.setValue(float(dxf.get("offset") or 0.0))
            if dxf.get("path"):
                from .dxf_dialogs import fill_layers

                self.dxf_message.setText(fill_layers(dxf["path"], self.dxf_layer, dxf.get("layer") or ""))
            syn = route.get("synthetic") or {}
            for key, widget in self.syn.items():
                if key in syn:
                    widget.setValue(float(syn[key]))
            self.syn_seed.setValue(int(syn.get("seed", 1)))
            self.kp_from.setText(route.get("kp_from") or "")
            self.kp_to.setText(route.get("kp_to") or "")
            seabed = s.get("seabed", {})
            _set_combo(self.seabed_source, seabed.get("source"))
            self._refresh_rasters(seabed.get("rasters") or [], seabed.get("raster_sources") or [])
            contour = QgsProject.instance().mapLayer(seabed.get("contour_layer") or "")
            if contour is not None:
                self.contour_combo.setLayer(contour)
                self.contour_field.setLayer(contour)
                self.contour_field.setField(seabed.get("contour_field") or "")
            self.interval.setValue(int(seabed.get("interval") or 0))
            self.filter_m.setValue(float(seabed.get("filter_m", 2.0)))
            # Scenarios saved before the auto filter keep their value (results unchanged).
            self.filter_auto.setChecked(bool(seabed.get("filter_auto", False)))
            self.resolve.setValue(float(seabed.get("resolve_intervals", RESOLVE_INTERVALS)))
            self.data_note.setText("")
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
            self.protection.set_rows(cable.get("protection") or [])
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
        if rpl_start_from_rpl:
            # A scenario saved before the RPL start KP could be set: the RPL's own.
            self._prefill_rpl_start_kp()
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
        for item in self.protection_list():
            parts = [f"+{item.weight_npm / 9.80665:g} kg/m in water"]
            if math_ok(item.diameter_m):
                parts.append(f"OD {item.diameter_m * 1000:g} mm")
            if math_ok(item.ei_knm2):
                parts.append(f"+EI {item.ei_knm2:g} kN·m²")
            if math_ok(item.mbr_m):
                parts.append(f"MBR {item.mbr_m:g} m")
            lines.append(f"Protection {item.name} ({item.kind}), KP {item.kp_start:.3f} to {item.kp_end:.3f}: "
                         + ", ".join(parts))
        lines.append(f"Friction: {self.friction_section.status.text()}")
        lines.append(f"Lay control: {self.control_section.status.text()}, laid towards "
                     f"{self.direction.currentText().lower()}")
        holds = self.control().holds()
        for section in holds[:12]:
            lo, hi = sorted((section.kp_start, section.kp_end))
            where = f"KP {lo:.3f}" if hi - lo < 1e-9 else f"KP {lo:.3f} to {hi:.3f}"
            lines.append(f"Held{' ' + section.name if section.name else ''}: {where} at {section.value:g} kN")
        if len(holds) > 12:
            lines.append(f"… and {len(holds) - 12} more held section(s)")
        return lines


def _token(label: str) -> str:
    from ..cable_library.store import type_token

    return type_token(label)


def _form_label(widget: QWidget):
    parent = widget.parentWidget()
    layout = parent.layout() if parent is not None else None
    return layout.labelForField(widget) if isinstance(layout, QFormLayout) else None


_FORM_ROLE = getattr(QFormLayout, "ItemRole", QFormLayout)
_ROLES = (_FORM_ROLE.LabelRole, _FORM_ROLE.FieldRole, _FORM_ROLE.SpanningRole)


class _FormRows:
    """The rows of a form, so a hidden row can leave it.

    Qt 5 keeps the spacing of a form row whose widgets are hidden, so hiding
    several rows left gaps in the setup pane (QGIS 3). Hidden rows are taken
    out of the layout and put back in their place when shown. Every row holds
    widgets (no layouts), so taking a row out never deletes anything.

    The layout items taken out are deleted (not the widgets): a widget stays
    tied to its first layout item while that item lives, and only that item
    hears the widget's size change. A section opened in a rebuilt form then
    kept its closed height, squashed into a sliver.
    """

    def __init__(self, layout: QFormLayout):
        self.layout = layout
        self.rows = []
        for r in range(layout.rowCount()):
            parts = []
            for role in _ROLES:
                item = layout.itemAt(r, role)
                parts.append(item.widget() if item is not None else None)
            self.rows.append(tuple(parts))
        self.hidden = set()

    def set_visible(self, widget: QWidget, visible: bool) -> bool:
        index = next((i for i, row in enumerate(self.rows) if any(part is widget for part in row)), None)
        if index is None:
            return False
        if (index not in self.hidden) != bool(visible):
            if visible:
                self.hidden.discard(index)
            else:
                self.hidden.add(index)
            self._rebuild()
        return True

    def _rebuild(self) -> None:
        layout = self.layout
        while layout.rowCount():
            taken = layout.takeRow(0)
            for item in (taken.labelItem, taken.fieldItem):
                if item is not None:
                    sip.delete(item)
        for index, (label, value, span) in enumerate(self.rows):
            shown = index not in self.hidden
            for part in (label, value, span):
                if part is not None:
                    part.setVisible(shown)
            if not shown:
                continue
            if span is not None:
                layout.addRow(span)
            elif label is not None:
                layout.addRow(label, value)
            elif value is not None:
                layout.addRow(value)


def _set_row_visible(widget: QWidget, visible: bool) -> None:
    """Show or hide a form row (its label too), leaving no gap."""
    parent = widget.parentWidget()
    layout = parent.layout() if parent is not None else None
    if isinstance(layout, QFormLayout):
        if hasattr(layout, "setRowVisible"):      # Qt 6.4 and later
            layout.setRowVisible(widget, bool(visible))
            return
        rows = getattr(layout, "_span_rows", None)
        if rows is None:
            rows = _FormRows(layout)
            layout._span_rows = rows
        if rows.set_visible(widget, visible):
            return
    widget.setVisible(visible)
    label = _form_label(widget)
    if label is not None:
        label.setVisible(visible)


def _set_combo(combo: QComboBox, data) -> None:
    index = combo.findData(data)
    if index >= 0:
        combo.setCurrentIndex(index)


def math_ok(value) -> bool:
    return value is not None and math.isfinite(value)
