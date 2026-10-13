# -*- coding: utf-8 -*-
"""Lay Assessment tab: suspension, loop-risk and tension checks as KP ranges.

The tab maps the active layer's columns onto the roles the checks need
(auto-detected, editable), picks the cable type from the cable library and,
optionally, a seabed source: the lay model's own touchdown depths, or a
raster / contour bathymetry sampled along the touchdown track with the
Depth Profile engine. A run reads the settings on the GUI thread, then does
the work (cable makeup, record checks, seabed sampling and model) in a
background task with progress in the tab and a Cancel button; the tab stays
usable meanwhile, and a result whose settings changed is marked out of date.
Findings are KP ranges listed in a table; selecting one highlights it on the
map and zooms the Seabed Profile dock. The method is in
``docs/LAY_ASSESSMENT.md``; the maths lives in :mod:`laydata.lay_assessment`.
"""

from __future__ import annotations

import csv
import html
import json
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineSymbol,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRuleBasedRenderer,
    QgsVectorLayer,
)
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox

from ... import depth_profile_core as dpc
from .. import workbench_makeup
from ..assessment_task import AssessmentTask
from ...cable_library import store as library
from ...kp_range_utils import make_distance_area
from ...laydata import lay_assessment as la
from ...laydata.qc_base import Severity
from ...plugin_log import log_exception
from ...qgis_compat import (
    EDIT_TRIGGER_NONE,
    FIELD_TYPE_DOUBLE,
    FIELD_TYPE_STRING,
    HEADER_RESIZE_MODE_STRETCH,
    MAP_LAYER_FILTER_LINE,
    MAP_LAYER_FILTER_RASTER,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
    layer_filters,
)

_SETTINGS = "SubseaCableTools/CableLayExplorer/Assessment"
_USER_ROLE = getattr(getattr(Qt, "ItemDataRole", Qt), "UserRole")
_VERTICAL = getattr(getattr(Qt, "Orientation", Qt), "Vertical")
_RICH_TEXT = getattr(getattr(Qt, "TextFormat", Qt), "RichText")

SEABED_NONE = "none"
SEABED_LAY_MODEL = "lay_model"
SEABED_RASTER = "raster"
SEABED_CONTOURS = "contours"
_SEABED_CHOICES = (
    (SEABED_NONE, "None (record checks only)"),
    (SEABED_LAY_MODEL, "Lay model touchdown depth"),
    (SEABED_RASTER, "Raster bathymetry (MBES grid)"),
    (SEABED_CONTOURS, "Depth contours"),
)
SEVERITY_TEXT = {Severity.ERROR: "Red", Severity.WARNING: "Amber", Severity.INFO: "Info"}
SEVERITY_COLOUR = {Severity.ERROR: "#d32f2f", Severity.WARNING: "#ff9800", Severity.INFO: "#1f77b4"}
_HEADERS = ["Level", "Check", "KP from", "KP to", "Length (m)", "Value", "Message"]
MAKEUP_DATA = "data"
MAKEUP_RPL = "rpl"
MAKEUP_FIT = "fit"
_MAKEUP_CHOICES = (
    (MAKEUP_DATA, "Cable type column in the lay data"),
    (MAKEUP_RPL, "Workbench RPL (cable type per leg)"),
    (MAKEUP_FIT, "Workbench assembly fitted to an RPL"),
)
_HOW_TEXT = {"mapped": "project mapping", "name": "library name", "alias": "library alias",
             "generic": "generic type", "ambiguous": "AMBIGUOUS: map it", "unknown": "not in library",
             "none": ""}
_SEABED_IDS = ("suspension", "length_balance")
_ZERO_TENSION_DEFAULT = 0.05
CONTOUR_LENGTH_LIMIT_M = 0.5   # coarser contours understate seabed length too much for the length check
COARSE_GRID_M = 1.0            # coarser grids blunt crests: bend radii at supports read high
_AUTO_STEP_RANGE = (0.5, 50.0)  # lay-model seabed bin: the touchdown record spacing, within this


def _metres(value: float) -> str:
    return f"{value:.2g} m" if value < 10 else f"{value:.0f} m"


def seabed_sampling_notes(mode, x_m, depth_m, kp_km, cells=None, sources=None, messages=(),
                          auto: bool = True) -> Tuple[List[str], Optional[str]]:
    """``(notes, length_check_skip_reason)`` on how the seabed was sampled.

    Says what spacing was used and what the source cannot resolve: a grid
    coarser than the cable's bending scale, contours (crests cut to the last
    contour; too coarse for the length check above
    :data:`CONTOUR_LENGTH_LIMIT_M`), rasters meeting along the route (a
    vertical datum step there reads as relief), plus the Depth Profile
    engine's own messages (spacing raised on long routes, partial coverage).
    """
    x = np.asarray(x_m, dtype=float)
    depth = np.asarray(depth_m, dtype=float)
    kp = np.asarray(kp_km, dtype=float)
    notes: List[str] = []
    skip = None
    finite = np.isfinite(depth) & np.isfinite(x)
    steps = np.diff(x[finite])
    steps = steps[steps > 1e-6]
    spacing = float(np.median(steps)) if steps.size else math.nan
    if mode == SEABED_RASTER:
        cell = np.array([c for c in (cells or []) if c is not None and np.isfinite(c) and c > 0], dtype=float)
        if cell.size:
            lo, hi = float(cell.min()), float(cell.max())
            cell_text = _metres(lo) if hi - lo < 0.01 * hi else f"{_metres(lo)} to {_metres(hi)}"
            if auto:
                notes.append(f"Seabed sampled at the grid's cell size ({cell_text}).")
            else:
                notes.append(f"Seabed sampled every {_metres(spacing)} (grid cells {cell_text}).")
                if np.isfinite(spacing) and spacing > 1.5 * lo:
                    notes.append("Sampling coarser than the grid misses crests and shortens the seabed: "
                                 "set Sample every to auto.")
            if hi > COARSE_GRID_M * 1.05:  # a nominal 1 m grid in degrees reads 1.00x m
                notes.append(f"Grid cells up to {_metres(hi)}: crests are blunted, so the bend radius at span "
                             "supports reads high and can hide an MBR breach.")
        if sources is not None and len(sources) == len(depth):
            change = [i for i in range(1, len(sources)) if sources[i] is not None and sources[i - 1] is not None
                      and sources[i] != sources[i - 1]]
            if change:
                where = ", ".join(f"{kp[i]:.3f}" for i in change[:5]) + (" …" if len(change) > 5 else "")
                notes.append(f"The seabed comes from {len({s for s in sources if s})} rasters, meeting at KP "
                             f"{where}: a vertical datum step where they meet reads as relief; check there.")
    elif mode == SEABED_CONTOURS:
        interval = la.contour_interval_m(depth)
        if np.isfinite(interval):
            notes.append(f"Contours every {_metres(interval)}: between crossings the seabed is a straight line, so "
                         "crests are cut to the last contour and span heights, bend radii and seabed length are "
                         "underestimated. Grid them, or use an MBES raster, for a firmer answer.")
            if interval > CONTOUR_LENGTH_LIMIT_M + 1e-9:
                skip = (f"contours every {_metres(interval)} understate the seabed length; grid them or use a "
                        "raster")
    elif mode == SEABED_LAY_MODEL and np.isfinite(spacing):
        notes.append(f"Lay model seabed binned every {_metres(spacing)}"
                     + (" (the touchdown record spacing)." if auto else "."))
    for message in messages or ():
        text = message[0] if isinstance(message, (tuple, list)) else str(message)
        if text and text not in notes:
            notes.append(str(text))
    return notes, skip
_INVALID_STYLE = "QLineEdit { border: 1px solid #d32f2f; }"
_STALE_STYLE = "QLabel { background: #fff3cd; color: #6d4c00; padding: 3px; border-radius: 2px; }"
_OPEN, _CLOSED = "▾", "▸"


def seabed_job(records, enabled, x, kp, depth, record_x, smoothing, filter_m=None,
               zero_tension_kn=_ZERO_TENSION_DEFAULT, progress=None, cancel=None):
    """Seabed model and findings (worker safe: numpy only, no widgets)."""
    if len(x) < 3 or not np.isfinite(depth).any():
        return None, [], {cid: "too few seabed samples" for cid in _SEABED_IDS if cid in enabled}
    susp = enabled.get("suspension") or la.default_params(la.check_by_id("suspension"))
    friction = enabled.get("length_balance")
    model = la.model_seabed(x, kp, depth, records, record_x, zero_tension_kn, susp["min_gap_m"],
                            susp["min_length_m"], smoothing_m=smoothing, friction=friction, filter_m=filter_m,
                            allowable="suspension" in enabled, progress=progress, cancel=cancel)
    findings: List[la.RangeFinding] = []
    if "suspension" in enabled:
        findings.extend(la.seabed_span_findings(model, enabled["suspension"]))
    if friction is not None:
        findings.extend(la.length_findings(model, friction, zero_tension_kn))
    return model, findings, {}


def run_job(job: Dict, cancel=None, progress=None) -> Dict:
    """The whole assessment from settings read on the GUI thread (worker safe).

    ``job`` holds the records, makeup, resolver, enabled checks and the seabed
    inputs (a prepared Depth Profile request for rasters / contours).
    """
    records = job["records"]
    la._report(progress, 0.0, "Assigning cable types")
    la.apply_makeup(records, job["makeup"], job["resolver"])
    la._check_cancel(cancel)
    la._report(progress, 0.04, "Running record checks")
    findings, skipped = la.run_record_checks(records, job["enabled"])
    skipped.update(job["skipped"])
    seabed = dict(job["seabed"])
    for cid in list(seabed):
        missing = la.missing_inputs(la.check_by_id(cid), records)
        if missing:
            skipped[cid] = "needs " + ", ".join(missing)
            seabed.pop(cid)
    model = None
    notes: List[str] = []
    if seabed:
        la._check_cancel(cancel)
        mode = job["mode"]
        auto = job["interval"] is None
        base = 0.08
        if mode == SEABED_LAY_MODEL:
            step = job["interval"]
            if step is None:
                spacing = la.record_spacing_m(records)
                step = float(np.clip(spacing, *_AUTO_STEP_RANGE)) if np.isfinite(spacing) else 2.0
            x, kp, depth = la.lay_model_profile(records, step, job["window"])
            record_x = records.kp * 1000.0
            notes, _skip = seabed_sampling_notes(mode, x, depth, kp, auto=auto)
        else:
            request, vertex_chainage, vertex_kp = job["request"]
            result = dpc.run_profile(request, cancel=cancel,
                                     progress=lambda f: la._report(progress, base + 0.5 * f, "Sampling the seabed"))
            base = 0.6
            x = np.asarray(result.kp_values, dtype=float) * 1000.0
            depth = np.array([np.nan if v is None else float(v) for v in result.depth_values])
            if result.status and not np.isfinite(depth).any():
                for cid in seabed:
                    skipped[cid] = result.status
                seabed = {}
            kp = la.chainage_to_kp(vertex_chainage, vertex_kp, x)
            record_x = la.kp_to_chainage(vertex_chainage, vertex_kp, records.kp)
            with np.errstate(invalid="ignore"):  # interp clamps; records off the track are not on it
                record_x[(records.kp < vertex_kp[0]) | (records.kp > vertex_kp[-1])] = np.nan
            notes, skip = seabed_sampling_notes(
                mode, x, depth, kp, cells=list(result.depth_cell_m or []) or None,
                sources=list(result.depth_source_ids or []) or None, messages=result.messages, auto=auto)
            if skip and "length_balance" in seabed:
                skipped["length_balance"] = skip
                seabed.pop("length_balance")
        if seabed:
            model, more, seabed_skipped = seabed_job(
                records, seabed, x, kp, depth, record_x, job["smoothing"], job["filter"], job["zero_tension"],
                progress=lambda f, text=None: la._report(progress, base + (1.0 - base) * f, text), cancel=cancel)
            findings = findings + more
            skipped.update(seabed_skipped)
    la._report(progress, 1.0, "Done")
    return {"records": records, "findings": findings, "skipped": skipped, "model": model,
            "resolution": job["resolution"], "notes": notes}


class AssessmentPanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.persist = True        # False in tests: never touch the user's QSettings
        self.synchronous = False   # True in tests: run on this thread
        self._dataset = None
        self._records: Optional[la.LayRecords] = None
        self._findings: List[la.RangeFinding] = []
        self._model: Optional[la.SeabedModel] = None
        self._track: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]] = None
        self._task = None
        self._dropped_tasks: List[AssessmentTask] = []
        self._run_id = 0
        self._result_signature: Optional[str] = None
        self._library_dialog = None
        self._map_layer_id: Optional[str] = None
        self._role_combos: Dict[str, QComboBox] = {}
        self._check_boxes: Dict[str, QGroupBox] = {}
        self._ready_labels: Dict[str, QLabel] = {}
        self._editors: Dict[str, Dict[str, QLineEdit]] = {}
        self._labels = {check.check_id: check.label for check in la.all_checks()}
        self._library_rows: List[Dict] = []
        self._type_mapping: Dict[str, str] = {}       # label token -> library name (this project)
        self._resolution: Dict[str, Tuple[Optional[str], str]] = {}  # label -> (library name, how)
        self._makeup_cache: Dict[Tuple, la.KpMakeup] = {}
        self._mapping_combos: Dict[str, QComboBox] = {}
        self._notes: List[str] = []
        self._numeric: Dict[QLineEdit, Tuple[bool, Optional[float]]] = {}
        self._building = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        splitter = QSplitter(_VERTICAL)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        config = QWidget()
        config_layout = QVBoxLayout(config)
        config_layout.setContentsMargins(2, 2, 2, 2)
        intro = QLabel("Checks a lay for suspensions, loop risk and tension limits. Works best on "
                       "the 3D model solutions layer (bottom tension, slack, touchdown position). "
                       "Results are KP ranges: select one to see it on the map and the Seabed Profile.")
        intro.setWordWrap(True)
        config_layout.addWidget(intro)
        config_layout.addWidget(self._build_columns_group())
        config_layout.addWidget(self._build_cable_group())
        config_layout.addWidget(self._build_seabed_group())
        checks_label = QLabel("<b>Checks</b> (tick to run; ▸ for thresholds)")
        config_layout.addWidget(checks_label)
        for check in la.all_checks():
            config_layout.addWidget(self._build_check_group(check))
        config_layout.addStretch(1)
        scroll.setWidget(config)
        splitter.addWidget(scroll)

        results = QWidget()
        results_layout = QVBoxLayout(results)
        results_layout.setContentsMargins(0, 0, 0, 0)
        buttons = QHBoxLayout()
        self.run_button = QPushButton("Run assessment")
        self.run_button.setToolTip("Run the checks. They run in the background: keep working, or Cancel.")
        self.run_button.clicked.connect(self.run)
        buttons.addWidget(self.run_button)
        self.profile_button = QPushButton("Seabed profile")
        self.profile_button.setToolTip("Show the Seabed Profile dock")
        self.profile_button.clicked.connect(self._show_profile)
        buttons.addWidget(self.profile_button)
        self.map_button = QPushButton("Add ranges to map")
        self.map_button.setToolTip("Add the findings as a KP-range line layer, styled red / amber / blue")
        self.map_button.clicked.connect(self.add_ranges_to_map)
        buttons.addWidget(self.map_button)
        self.csv_button = QPushButton("Export CSV…")
        self.csv_button.clicked.connect(self.export_csv)
        buttons.addWidget(self.csv_button)
        results_layout.addLayout(buttons)
        progress_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(True)
        progress_row.addWidget(self.progress_bar, 1)
        self.stage_label = QLabel("")
        progress_row.addWidget(self.stage_label, 2)
        results_layout.addLayout(progress_row)
        self.progress_bar.hide()
        self.stage_label.hide()
        self.stale_label = QLabel("Settings changed since these results: Run assessment to update them.")
        self.stale_label.setStyleSheet(_STALE_STYLE)
        self.stale_label.setWordWrap(True)
        self.stale_label.hide()
        results_layout.addWidget(self.stale_label)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(_RICH_TEXT)
        results_layout.addWidget(self.status_label)
        show_row = QHBoxLayout()
        show_row.addWidget(QLabel("Show"))
        self.show_combo = QComboBox()
        self.show_combo.setToolTip("Show the ranges of one check only")
        self.show_combo.currentIndexChanged.connect(self._apply_filter)
        show_row.addWidget(self.show_combo, 1)
        self.red_only = QCheckBox("Red only")
        self.red_only.toggled.connect(self._apply_filter)
        show_row.addWidget(self.red_only)
        results_layout.addLayout(show_row)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(len(_HEADERS) - 1, HEADER_RESIZE_MODE_STRETCH)
        self.table.setToolTip("Click a range to highlight it on the map and the Seabed Profile; "
                              "double-click to zoom the map to it and select its records.")
        self.table.itemSelectionChanged.connect(self._on_selected)
        self.table.cellDoubleClicked.connect(self._on_double_clicked)
        results_layout.addWidget(self.table, 1)
        splitter.addWidget(results)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        self._update_buttons()
        self._restore_settings()
        self._type_mapping = library.project_mapping()
        self._refresh_cable_types()
        self._building = False
        self._connect_change_signals()
        self._on_settings_changed()

    # -- construction -------------------------------------------------------
    def _numeric_edit(self, text: str = "", placeholder: str = "", allow_blank: bool = True,
                      minimum: Optional[float] = None) -> QLineEdit:
        """A line edit for a number, outlined red while its text is not one."""
        editor = QLineEdit(text)
        if placeholder:
            editor.setPlaceholderText(placeholder)
        self._numeric[editor] = (allow_blank, minimum)
        editor.textChanged.connect(lambda _t, e=editor: self._validate(e))
        return editor

    def _validate(self, editor: QLineEdit) -> bool:
        allow_blank, minimum = self._numeric.get(editor, (True, None))
        text = editor.text().strip().replace(",", ".")
        ok = True
        if not text:
            ok = allow_blank
        else:
            try:
                value = float(text)
                ok = math.isfinite(value) and (minimum is None or value >= minimum)
            except ValueError:
                ok = False
        editor.setStyleSheet("" if ok else _INVALID_STYLE)
        return ok

    @staticmethod
    def _toggle_button(text: str, target: QWidget, expanded: bool = False) -> QToolButton:
        button = QToolButton()
        button.setCheckable(True)
        button.setAutoRaise(True)

        def apply(on):
            target.setVisible(on)
            button.setText(f"{_OPEN if on else _CLOSED} {text}")
        button.toggled.connect(apply)
        button.setChecked(expanded)
        apply(expanded)
        return button

    def _build_columns_group(self) -> QGroupBox:
        group = QGroupBox("Columns")
        group.setToolTip("Which column holds each quantity. Detected from the column names; "
                         "change any that are wrong, or set (none).")
        outer = QVBoxLayout(group)
        self.columns_summary = QLabel("")
        self.columns_summary.setWordWrap(True)
        outer.addWidget(self.columns_summary)
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        for role in la.ROLES:
            combo = QComboBox()
            combo.setToolTip(role.help)
            label = QLabel(role.label)
            label.setToolTip(role.help)
            form.addRow(label, combo)
            self._role_combos[role.key] = combo
        self.columns_toggle = self._toggle_button("Show / change columns", body)
        outer.addWidget(self.columns_toggle)
        outer.addWidget(body)
        return group

    def _build_cable_group(self) -> QGroupBox:
        group = QGroupBox("Cable")
        form = QFormLayout(group)
        row = QHBoxLayout()
        self.library_label = QLabel("")
        self.library_label.setWordWrap(True)
        row.addWidget(self.library_label, 1)
        library_btn = QPushButton("Cable library…")
        library_btn.setToolTip("Create or edit your cable type library (weights, NPTS / NOTS / NTTS / CBL, "
                               "MBR, EI)")
        library_btn.clicked.connect(self.open_library)
        row.addWidget(library_btn)
        form.addRow(row)
        self.makeup_combo = QComboBox()
        for key, text in _MAKEUP_CHOICES:
            self.makeup_combo.addItem(text, key)
        self.makeup_combo.setToolTip(
            "Where the cable type along the route comes from. It sets the cable at touchdown and the cable "
            "hanging above it (tension limits and weight where a type change is in the water). Workbench "
            "sources need the lay data's touchdown KPs quoted on that RPL.")
        self.makeup_combo.currentIndexChanged.connect(self._on_makeup_source)
        form.addRow("Cable types from", self.makeup_combo)
        self.rpl_combo = QComboBox()
        self.rpl_combo.currentIndexChanged.connect(self._on_rpl_changed)
        self.rpl_label = QLabel("RPL")
        form.addRow(self.rpl_label, self.rpl_combo)
        self.fit_combo = QComboBox()
        self.fit_combo.currentIndexChanged.connect(self._refresh_labels)
        self.fit_label = QLabel("Fitted assembly")
        form.addRow(self.fit_label, self.fit_combo)
        self.mapping_table = QTableWidget(0, 3)
        self.mapping_table.setHorizontalHeaderLabels(["Label", "Library type (this project)", "Matched by"])
        self.mapping_table.setToolTip(
            "Each cable type label found in the lay data or the Workbench source, and the library type it "
            "means in this project (saved with the project). Leave (automatic) to match by library name, "
            "a unique alias or a unique generic type; a label several library types could mean must be set.")
        self.mapping_table.horizontalHeader().setSectionResizeMode(1, HEADER_RESIZE_MODE_STRETCH)
        self.mapping_table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.mapping_table.setMaximumHeight(110)
        form.addRow(self.mapping_table)
        self.cable_combo = QComboBox()
        self.cable_combo.setToolTip("Used where no label is known, or a label matches no library type.")
        form.addRow("Default cable type", self.cable_combo)
        self.units_combo = QComboBox()
        self.units_combo.addItems(list(la.TENSION_UNITS))
        self.units_combo.setToolTip("Units of the tension columns in the data")
        form.addRow("Tension units in data", self.units_combo)
        self._update_makeup_widgets()
        return group

    def _build_seabed_group(self) -> QGroupBox:
        group = QGroupBox("Seabed")
        form = QFormLayout(group)
        self.seabed_combo = QComboBox()
        for key, text in _SEABED_CHOICES:
            self.seabed_combo.addItem(text, key)
        self.seabed_combo.setToolTip(
            "Seabed the cable is rested on for the Suspensions and Cable length vs seabed checks. The lay "
            "model's touchdown depths need no other data; bathymetry is sampled along the touchdown track "
            "(grid XYZ soundings first with Create Raster from XYZ).")
        self.seabed_combo.currentIndexChanged.connect(self._update_seabed_widgets)
        form.addRow("Seabed source", self.seabed_combo)
        self.raster_combo = QgsMapLayerComboBox()
        self.raster_combo.setFilters(layer_filters(MAP_LAYER_FILTER_RASTER))
        self.raster_combo.setAllowEmptyLayer(True)
        self.raster_label = QLabel("Raster")
        form.addRow(self.raster_label, self.raster_combo)
        self.contour_combo = QgsMapLayerComboBox()
        self.contour_combo.setFilters(layer_filters(MAP_LAYER_FILTER_LINE))
        self.contour_combo.setAllowEmptyLayer(True)
        self.contour_label = QLabel("Contours")
        form.addRow(self.contour_label, self.contour_combo)
        self.contour_field = QgsFieldComboBox()
        self.contour_field_label = QLabel("Depth field")
        form.addRow(self.contour_field_label, self.contour_field)
        self.contour_combo.layerChanged.connect(self._on_contour_layer)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(0, 1000)
        self.interval_spin.setSpecialValueText("auto")
        self.interval_spin.setValue(0)
        self.interval_spin.setSuffix(" m")
        self.interval_spin.setToolTip(
            "Seabed sample spacing along the touchdown track. Auto (recommended): each raster at its own "
            "cell size (at least 1 m), the lay model's depths at the touchdown record spacing. Coarser "
            "sampling misses crests and shortens the seabed; finer adds nothing the grid does not hold. "
            "Contours are always used at their crossings.")
        self.interval_label = QLabel("Sample every")
        form.addRow(self.interval_label, self.interval_spin)
        self.filter_edit = self._numeric_edit(placeholder=f"auto: {la.AUTO_FILTER_M:g} m", minimum=0.0)
        self.filter_edit.setToolTip(
            "The cable rests on the sampled seabed averaged over this length: it removes sounding noise "
            "(which would otherwise create false spans under tension) but keeps the crests that hold the "
            f"cable up. Blank = {la.AUTO_FILTER_M:g} m, enough for about 10 cm of noise; 3 m for noisier "
            "data; 0 = none.")
        form.addRow("Seabed noise filter (m)", self.filter_edit)
        self.smoothing_edit = self._numeric_edit(placeholder="auto: the cable's conformity length", minimum=0.0)
        self.smoothing_edit.setToolTip(
            "Seabed and lay-model lengths (the cable length check) use the seabed averaged over this length: "
            "a cable cannot follow features shorter than about 2 (EI / w)^(1/3) (from the library's bending "
            "stiffness, 10 m when unknown), and noise would otherwise add length. 0 = no smoothing.")
        form.addRow("Smoothing for lengths (m)", self.smoothing_edit)
        self.zero_edit = self._numeric_edit(f"{_ZERO_TENSION_DEFAULT:g}", minimum=0.0)
        self.zero_edit.setToolTip("Below this bottom tension the cable lies on the seabed and follows it "
                                  "(no spans); used by the suspension and length checks.")
        form.addRow("Cable conforms below (kN)", self.zero_edit)
        kp_row = QHBoxLayout()
        self.kp_from = self._numeric_edit(placeholder="start")
        self.kp_to = self._numeric_edit(placeholder="end")
        kp_row.addWidget(self.kp_from)
        kp_row.addWidget(QLabel("to"))
        kp_row.addWidget(self.kp_to)
        form.addRow("Seabed KP range", kp_row)
        self._update_seabed_widgets()
        return group

    def _build_check_group(self, check: la.CheckDef) -> QGroupBox:
        group = QGroupBox(check.label)
        group.setCheckable(True)
        group.setChecked(check.default_enabled)
        group.setToolTip(check.description)
        self._check_boxes[check.check_id] = group
        outer = QVBoxLayout(group)
        outer.setContentsMargins(6, 2, 6, 2)
        ready = QLabel("")
        ready.setWordWrap(True)
        self._ready_labels[check.check_id] = ready
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        description = QLabel(check.description)
        description.setWordWrap(True)
        form.addRow(description)
        self._editors[check.check_id] = {}
        for spec in check.params:
            editor = self._numeric_edit("" if spec.default is None else f"{spec.default:g}",
                                        minimum=spec.minimum)
            if spec.help:
                editor.setToolTip(spec.help)
            self._editors[check.check_id][spec.name] = editor
            form.addRow(spec.label, editor)
        head = QHBoxLayout()
        head.addWidget(ready, 1)
        head.addWidget(self._toggle_button("Thresholds", body))
        outer.addLayout(head)
        outer.addWidget(body)
        return group

    def _connect_change_signals(self) -> None:
        """Every setting re-checks readiness and whether the shown results are out of date."""
        changed = self._on_settings_changed
        for combo in list(self._role_combos.values()) + [self.makeup_combo, self.rpl_combo, self.fit_combo,
                                                          self.cable_combo, self.units_combo, self.seabed_combo]:
            combo.currentIndexChanged.connect(changed)
        for editor in self._numeric:
            editor.textChanged.connect(changed)
        for box in self._check_boxes.values():
            box.toggled.connect(changed)
        self.interval_spin.valueChanged.connect(changed)
        self.raster_combo.layerChanged.connect(changed)
        self.contour_combo.layerChanged.connect(changed)
        self.contour_field.fieldChanged.connect(changed)

    # -- dataset / widgets ---------------------------------------------------
    def set_dataset(self, dataset) -> None:
        if dataset is self._dataset:
            return
        if self._task is not None:
            # The running assessment belongs to the previous layer: drop it. Python
            # keeps a reference until it finishes (the task manager runs our run()).
            self._run_id += 1
            self._dropped_tasks.append(self._task)
            try:
                self._task.cancel()
            except RuntimeError:
                pass
            self._task = None
            self._set_running(False)
        self._dataset = dataset
        self._clear_results()
        names = list(dataset.field_names) if dataset is not None else []
        detected = la.detect_roles(names, dataset.is_numeric_field) if dataset is not None else {}
        saved = self._saved_mapping()
        for role in la.ROLES:
            combo = self._role_combos[role.key]
            previous = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(none)", "")
            for name in names:
                if role.numeric and not dataset.is_numeric_field(name):
                    continue
                combo.addItem(name, name)
            for choice in (previous, saved.get(role.key), detected.get(role.key)):
                index = combo.findData(choice) if choice else -1
                if index > 0:
                    combo.setCurrentIndex(index)
                    break
            combo.blockSignals(False)
        if dataset is not None and not self.mapping().get("kp"):
            self.columns_toggle.setChecked(True)  # nothing to run without a KP column: show the picks
        self._refresh_labels()
        self._on_settings_changed()

    def _clear_results(self) -> None:
        """Forget results: their record indices belong to the previous dataset."""
        self._records = None
        self._findings = []
        self._model = None
        self._track = None
        self._result_signature = None
        self.table.setRowCount(0)
        self.show_combo.clear()
        self.status_label.setText("")
        self._update_buttons()
        self._update_stale()
        panel = self._profile_panel(create=False)
        if panel is not None:
            panel.clear()

    def mapping(self) -> Dict[str, str]:
        return {key: combo.currentData() for key, combo in self._role_combos.items() if combo.currentData()}

    def _update_seabed_widgets(self, *_args) -> None:
        mode = self.seabed_combo.currentData()
        for widget in (self.raster_combo, self.raster_label):
            widget.setVisible(mode == SEABED_RASTER)
        for widget in (self.contour_combo, self.contour_label, self.contour_field, self.contour_field_label):
            widget.setVisible(mode == SEABED_CONTOURS)
        if hasattr(self, "interval_spin"):
            for widget in (self.interval_spin, self.interval_label):
                widget.setVisible(mode in (SEABED_RASTER, SEABED_LAY_MODEL))

    def _on_contour_layer(self, layer) -> None:
        self.contour_field.setLayer(layer)
        if layer is not None:
            from ...burial.tabs.bathy_dialog import guess_depth_field

            guess = guess_depth_field([f.name() for f in layer.fields()])
            if guess:
                self.contour_field.setField(guess)

    def _refresh_cable_types(self) -> None:
        rows = library.load_current()
        path = library.current_path()
        current = self.cable_combo.currentText() or self._setting("cable_type", "")
        self.cable_combo.blockSignals(True)
        self.cable_combo.clear()
        self.cable_combo.addItem("(none)", "")
        for row in rows:
            self.cable_combo.addItem(row["name"], row["name"])
        index = self.cable_combo.findData(current)
        if index > 0:
            self.cable_combo.setCurrentIndex(index)
        elif len(rows) == 1:
            self.cable_combo.setCurrentIndex(1)
        self.cable_combo.blockSignals(False)
        if not path:
            self.library_label.setText("No cable library yet: weights and tension limits come from it.")
        elif not library.is_library(path):
            self.library_label.setText(f"Library not found: {path}")
        else:
            self.library_label.setText(f"{len(rows)} type(s) in {path}")
        self._library_rows = rows
        self._refresh_labels()
        self._on_settings_changed()

    def open_library(self) -> None:
        from ...cable_library.dialog import CableLibraryDialog

        if self._library_dialog is None:
            self._library_dialog = CableLibraryDialog(self)
            self._library_dialog.librarySaved.connect(lambda _path: self._refresh_cable_types())
            self._library_dialog.finished.connect(lambda _code: self._refresh_cable_types())
        else:
            self._library_dialog.set_path(library.current_path())
        self._library_dialog.show()
        self._library_dialog.raise_()

    # -- readiness and out-of-date results ------------------------------------
    def _on_settings_changed(self, *_args) -> None:
        if self._building:
            return
        self._update_columns_summary()
        self._update_readiness()
        self._update_stale()

    def _update_columns_summary(self) -> None:
        mapping = self.mapping()
        if self._dataset is None:
            self.columns_summary.setText("Load a data layer to detect its columns.")
            return
        kp = mapping.get("kp")
        unset = [role.label for role in la.ROLES if role.key not in mapping]
        text = (f"{len(mapping)} of {len(la.ROLES)} set. KP (touchdown): "
                + (f"<b>{html.escape(kp)}</b>" if kp else "<b style='color:#d32f2f'>not set</b>"))
        self.columns_summary.setText(text)
        self.columns_summary.setToolTip("Not set: " + ", ".join(unset) if unset else "All columns set.")

    def _readiness(self, check: la.CheckDef) -> List[str]:
        """What a check still needs, judged from the settings (no data read)."""
        mapped = set(self.mapping())
        needs = [la.ROLE_BY_KEY[key].label for key in check.needs if key not in mapped]
        if "kp" not in mapped:
            needs.insert(0, la.ROLE_BY_KEY["kp"].label)
        if check.check_id == "td_reversal" and self._dataset is not None and self._dataset.time_epoch is None:
            needs.append("time")
        if check.check_id == "tension_consistency" and "top_tension_calc" not in mapped \
                and not {"bottom_tension", "td_depth"} <= mapped:
            needs.append("calculated top tension, or bottom tension + depth")
        for attribute in check.needs_cable:
            if not any(row.get(attribute) not in (None, "") for row in self._library_rows):
                needs.append(f"{la.CABLE_LABELS.get(attribute, attribute)} in the cable library")
        if check.needs_seabed:
            mode = self.seabed_combo.currentData()
            if mode == SEABED_NONE:
                needs.append("a seabed source")
            elif mode == SEABED_LAY_MODEL and check.check_id == "length_balance":
                needs.append("bathymetry (it compares against the lay model's own seabed)")
            elif mode == SEABED_RASTER and self.raster_combo.currentLayer() is None:
                needs.append("a raster layer")
            elif mode == SEABED_CONTOURS and (self.contour_combo.currentLayer() is None
                                              or not self.contour_field.currentField()):
                needs.append("a contour layer and its depth field")
        return needs

    def _update_readiness(self) -> None:
        for check in la.all_checks():
            label = self._ready_labels[check.check_id]
            if not self._check_boxes[check.check_id].isChecked():
                label.setText("<span style='color:#777'>Off</span>")
                continue
            needs = self._readiness(check)
            if needs:
                label.setText("<span style='color:#b26a00'>Needs " + html.escape(", ".join(needs)) + "</span>")
            else:
                label.setText("<span style='color:#2e7d32'>Ready</span>")

    def _signature(self) -> str:
        """The settings a result depends on, to tell when it is out of date."""
        state = {
            "mapping": self.mapping(), "units": self.units_combo.currentText(),
            "cable": self.cable_combo.currentData(), "makeup": self.makeup_combo.currentData(),
            "rpl": self.rpl_combo.currentData(), "fit": self.fit_combo.currentData(),
            "types": self._type_mapping, "seabed": self.seabed_combo.currentData(),
            "raster": self.raster_combo.currentLayer().id() if self.raster_combo.currentLayer() else None,
            "contours": self.contour_combo.currentLayer().id() if self.contour_combo.currentLayer() else None,
            "field": self.contour_field.currentField(), "interval": self.interval_spin.value(),
            "edits": [e.text() for e in (self.filter_edit, self.smoothing_edit, self.zero_edit,
                                         self.kp_from, self.kp_to)],
            "checks": {cid: [box.isChecked()] + [e.text() for e in self._editors[cid].values()]
                       for cid, box in self._check_boxes.items()},
            "library": library.fingerprint(self._library_rows),
        }
        return json.dumps(state, sort_keys=True, default=str)

    def _update_stale(self) -> None:
        stale = self._result_signature is not None and self._signature() != self._result_signature
        self.stale_label.setVisible(stale)

    # -- cable makeup and label mapping ---------------------------------------
    def _update_makeup_widgets(self) -> None:
        source = self.makeup_combo.currentData()
        for widget in (self.rpl_combo, self.rpl_label):
            widget.setVisible(source in (MAKEUP_RPL, MAKEUP_FIT))
        for widget in (self.fit_combo, self.fit_label):
            widget.setVisible(source == MAKEUP_FIT)

    def _on_makeup_source(self, *_args) -> None:
        self._update_makeup_widgets()
        if self.makeup_combo.currentData() in (MAKEUP_RPL, MAKEUP_FIT):
            self._fill_rpls()
        self._refresh_labels()

    def _fill_rpls(self) -> None:
        store = workbench_makeup.open_store()
        current = self.rpl_combo.currentData() or self._setting("makeup_rpl", "")
        self.rpl_combo.blockSignals(True)
        self.rpl_combo.clear()
        if store is None:
            self.rpl_combo.addItem("(no Workbench registry in this project)", "")
        else:
            for rpl_id, label in workbench_makeup.rpl_choices(store):
                self.rpl_combo.addItem(label, rpl_id)
        index = self.rpl_combo.findData(current)
        if index >= 0:
            self.rpl_combo.setCurrentIndex(index)
        self.rpl_combo.blockSignals(False)
        self._on_rpl_changed()

    def _on_rpl_changed(self, *_args) -> None:
        if self.makeup_combo.currentData() == MAKEUP_FIT:
            store = workbench_makeup.open_store()
            rpl_id = self.rpl_combo.currentData()
            current = self.fit_combo.currentData() or self._setting("makeup_fit", "")
            self.fit_combo.blockSignals(True)
            self.fit_combo.clear()
            if store is not None and rpl_id:
                for fit_id, label in workbench_makeup.fit_choices(store, rpl_id):
                    self.fit_combo.addItem(label, fit_id)
            if self.fit_combo.count() == 0:
                self.fit_combo.addItem("(no assembly fitted to this RPL)", "")
            index = self.fit_combo.findData(current)
            if index >= 0:
                self.fit_combo.setCurrentIndex(index)
            self.fit_combo.blockSignals(False)
        self._refresh_labels()

    def current_makeup(self) -> Optional[la.KpMakeup]:
        """The Workbench makeup chosen (cached), None for the lay data column."""
        source = self.makeup_combo.currentData()
        if source == MAKEUP_DATA:
            return None
        key = (source, self.rpl_combo.currentData(), self.fit_combo.currentData())
        if key not in self._makeup_cache:
            if not key[1] or (source == MAKEUP_FIT and not key[2]):
                raise ValueError("choose a Workbench RPL" + (" and fitted assembly" if source == MAKEUP_FIT else ""))
            makeup = workbench_makeup.makeup_for(workbench_makeup.open_store(), source, key[1], key[2])
            if makeup is None or makeup.empty:
                raise ValueError("the Workbench source has no cable types")
            self._makeup_cache = {key: makeup}
        return self._makeup_cache[key]

    def _labels_found(self) -> List[str]:
        labels = set()
        field = self._role_combos["cable_type"].currentData() if self._role_combos else None
        if self._dataset is not None and field and self._dataset.has_field(field):
            for value in set(self._dataset.raw(field).tolist()):
                text = "" if value is None else str(value).strip()
                if text and text != "NULL":
                    labels.add(text)
        try:
            makeup = self.current_makeup()
        except Exception:
            makeup = None
        if makeup is not None:
            labels.update(makeup.labels)
        return sorted(labels, key=library.type_token)

    def _refresh_labels(self, *_args) -> None:
        """Rebuild the label -> library type table (one row per distinct label token)."""
        if not hasattr(self, "mapping_table"):
            return
        seen, labels = set(), []
        for label in self._labels_found():
            token = library.type_token(label)
            if token and token not in seen:
                seen.add(token)
                labels.append(label)
        names = [row["name"] for row in self._library_rows]
        self.mapping_table.setRowCount(0)
        self._mapping_combos = {}
        for label in labels:
            token = library.type_token(label)
            r = self.mapping_table.rowCount()
            self.mapping_table.insertRow(r)
            self.mapping_table.setItem(r, 0, QTableWidgetItem(label))
            combo = QComboBox()
            combo.addItem("(automatic)", "")
            options = library.candidates(self._library_rows, label)
            for name in options + [n for n in names if n not in options]:
                combo.addItem(name, name)
            index = combo.findData(self._type_mapping.get(token, ""))
            combo.setCurrentIndex(max(index, 0))
            combo.currentIndexChanged.connect(lambda _i, t=token, c=combo: self._on_mapping_changed(t, c))
            self.mapping_table.setCellWidget(r, 1, combo)
            self._mapping_combos[token] = combo
            self._update_how(token, label)
        self.mapping_table.resizeColumnToContents(0)

    def _update_how(self, token: str, label: str) -> None:
        for r in range(self.mapping_table.rowCount()):
            item = self.mapping_table.item(r, 0)
            if item is not None and library.type_token(item.text()) == token:
                row, how = library.resolve(self._library_rows, label, self._type_mapping)
                text = _HOW_TEXT.get(how, how)
                cell = QTableWidgetItem(text if row is None or how == "mapped" else f"{text}: {row['name']}")
                if how in ("ambiguous", "unknown"):
                    cell.setForeground(QColor(SEVERITY_COLOUR[Severity.ERROR if how == "ambiguous"
                                                              else Severity.WARNING]))
                self.mapping_table.setItem(r, 2, cell)

    def _on_mapping_changed(self, token: str, combo: QComboBox) -> None:
        name = combo.currentData()
        if name:
            self._type_mapping[token] = name
        else:
            self._type_mapping.pop(token, None)
        if self.persist:
            library.set_project_mapping(self._type_mapping)
        label = next((self.mapping_table.item(r, 0).text() for r in range(self.mapping_table.rowCount())
                      if library.type_token(self.mapping_table.item(r, 0).text()) == token), token)
        self._update_how(token, label)
        self._on_settings_changed()

    def _cable_resolver(self):
        """``(resolve, resolution)``: a worker-safe resolver and the dict it fills
        with each label's (library name, how)."""
        rows = list(self._library_rows)
        mapping = dict(self._type_mapping)
        default = library.resolve(rows, self.cable_combo.currentData())[0]
        resolution: Dict[str, Tuple[Optional[str], str]] = {}

        def resolve(text):
            row, how = library.resolve(rows, text, mapping)
            if text:
                resolution[str(text)] = (row["name"] if row else None, how)
            return library.to_props(row or default)
        return resolve, resolution

    @staticmethod
    def _number(editor: QLineEdit, what: str, default=None, minimum: Optional[float] = 0.0) -> Optional[float]:
        text = editor.text().strip().replace(",", ".")
        if not text:
            return default
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"{what}: enter a number, or leave blank.")
        return value if minimum is None else max(value, minimum)

    def _smoothing(self) -> Optional[float]:
        return self._number(self.smoothing_edit, "Smoothing for lengths")

    def _params(self, check: la.CheckDef) -> Dict[str, float]:
        params: Dict[str, float] = {}
        for spec in check.params:
            text = self._editors[check.check_id][spec.name].text().strip().replace(",", ".")
            try:
                params[spec.name] = float(text) if text else spec.default
            except ValueError:
                raise ValueError(f"{check.label}: '{spec.label}' is not a number.")
        return params

    def _kp_window(self) -> Optional[Tuple[float, float]]:
        try:
            lo = self._number(self.kp_from, "Seabed KP range", minimum=None)
            hi = self._number(self.kp_to, "Seabed KP range", minimum=None)
        except ValueError:
            raise ValueError("Seabed KP range: enter KPs in km, or leave blank.")
        if lo is None and hi is None:
            return None
        lo = -math.inf if lo is None else lo
        hi = math.inf if hi is None else hi
        return (min(lo, hi), max(lo, hi))

    # -- run --------------------------------------------------------------------
    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # never lose an error inside a Qt slot
            log_exception("Lay Assessment: run failed")
            self.status_label.setText(f"Assessment failed: {html.escape(str(exc))}")
            self._set_running(False)

    def cancel_run(self) -> None:
        if self._task is not None:
            self.stage_label.setText("Cancelling…")
            try:
                self._task.cancel()
            except RuntimeError:
                pass

    def _run(self) -> None:
        if self._task is not None:
            self.cancel_run()   # the button reads Cancel while a run is going
            return
        if self._dataset is None:
            self.status_label.setText("Load a data layer first.")
            return
        notes: List[str] = []
        try:
            enabled = {check.check_id: self._params(check) for check in la.all_checks()
                       if self._check_boxes[check.check_id].isChecked()}
            window = self._kp_window()
            smoothing = self._smoothing()
            filter_m = self._number(self.filter_edit, "Seabed noise filter")
            zero_tension = self._number(self.zero_edit, "Cable conforms below", _ZERO_TENSION_DEFAULT)
            records = la.build_records(self._dataset, self.mapping(), self.units_combo.currentText())
        except ValueError as exc:
            self.status_label.setText(html.escape(str(exc)))
            return
        if records.n == 0:
            self.status_label.setText("No records with a KP to assess.")
            return
        try:
            makeup = self.current_makeup()
        except Exception as exc:
            makeup = None
            notes.append(f"Cable types from the lay data only ({exc}).")
        resolver, resolution = self._cable_resolver()
        if self.persist:
            self._save_settings()
        try:
            track = la.track_vertices(records)
        except ValueError:
            track = None
        skipped: Dict[str, str] = {}
        seabed = {cid: enabled[cid] for cid in _SEABED_IDS if cid in enabled}
        mode = self.seabed_combo.currentData()
        if seabed and mode == SEABED_NONE:
            for cid in seabed:
                skipped[cid] = "needs a seabed source"
            seabed = {}
        if mode == SEABED_LAY_MODEL and "length_balance" in seabed:
            skipped["length_balance"] = "compares against bathymetry, not the lay model's own seabed"
            seabed.pop("length_balance")
        if seabed and mode == SEABED_LAY_MODEL and not records.has("td_depth"):
            for cid in seabed:
                skipped[cid] = "needs the touchdown water depth column"
            seabed = {}
        request = None
        if seabed and mode in (SEABED_RASTER, SEABED_CONTOURS):
            try:
                request = self._seabed_request(track, mode, window)
            except ValueError as exc:
                for cid in seabed:
                    skipped.setdefault(cid, str(exc))
                seabed = {}
        job = {"records": records, "makeup": makeup, "resolver": resolver, "resolution": resolution,
               "enabled": enabled, "skipped": skipped, "seabed": seabed, "mode": mode, "window": window,
               "interval": float(self.interval_spin.value()) or None, "smoothing": smoothing, "filter": filter_m,
               "zero_tension": zero_tension, "request": request}
        self._run_id += 1
        info = {"id": self._run_id, "dataset": self._dataset, "signature": self._signature(), "track": track,
                "window": window, "notes": notes}
        if self.synchronous:
            self._apply_result(info, run_job(job))
            return
        self._start_task(job, info)

    def _seabed_request(self, track, mode, window):
        if track is None:
            raise ValueError("needs touchdown positions")
        lon, lat, kp = track
        if window is not None:
            margin = 0.05
            keep = (kp >= window[0] - margin) & (kp <= window[1] + margin)
            lon, lat, kp = lon[keep], lat[keep], kp[keep]
        if len(kp) < 2:
            raise ValueError("no touchdown track in the KP range")
        crs = QgsCoordinateReferenceSystem("EPSG:4326")
        context = QgsProject.instance().transformContext()
        distance = make_distance_area(crs, context)
        points = [QgsPointXY(float(x), float(y)) for x, y in zip(lon, lat)]
        chainage = [0.0]
        for p0, p1 in zip(points[:-1], points[1:]):
            chainage.append(chainage[-1] + float(distance.measureLine(p0, p1)))
        chainage = np.asarray(chainage)
        keep = np.concatenate(([True], np.diff(chainage) > 0))
        points = [p for p, k in zip(points, keep) if k]
        chainage, kp = chainage[keep], kp[keep]
        route = dpc.RouteStationing([points], float(chainage[-1]), crs, distance)
        spacing = int(self.interval_spin.value())
        # Auto: each raster at its own cell size (the engine's adaptive step, at least 1 m).
        params = dpc.ProfileParams(mode=dpc.RASTER if mode == SEABED_RASTER else dpc.CONTOURS,
                                   interval_m=spacing or 1, adaptive=spacing == 0, adaptive_factor=1.0,
                                   max_samples=200000, per_raster=False)
        if mode == SEABED_RASTER:
            layer = self.raster_combo.currentLayer()
            if not isinstance(layer, QgsRasterLayer):
                raise ValueError("choose a raster layer")
            request = dpc.build_request(route, params, context, raster_layers=[layer])
        else:
            layer = self.contour_combo.currentLayer()
            field = self.contour_field.currentField()
            if layer is None or not field:
                raise ValueError("choose a contour layer and its depth field")
            request = dpc.build_request(route, params, context, contour_layers=[(layer, field)])
        if request.status:
            raise ValueError(request.status)
        return request, chainage, kp

    def _set_running(self, running: bool) -> None:
        self.run_button.setText("Cancel" if running else "Run assessment")
        self.run_button.setToolTip("Stop this run; the results shown stay as they are." if running else
                                   "Run the checks. They run in the background: keep working, or Cancel.")
        self.progress_bar.setVisible(running)
        self.stage_label.setVisible(running)
        if running:
            self.progress_bar.setValue(0)
            self.stage_label.setText("Starting…")

    def _start_task(self, job: Dict, info: Dict) -> None:
        task = AssessmentTask("Lay Assessment", lambda cancel, progress: run_job(job, cancel, progress),
                              self._on_task_finished)
        task.info = info
        self._task = task
        task.progressChanged.connect(lambda value: self.progress_bar.setValue(int(value)))
        task.stageChanged.connect(self.stage_label.setText)
        self._set_running(True)
        if self._findings:
            self.status_label.setText("Running in the background; the results below are from the previous run.")
        QgsApplication.taskManager().addTask(task)

    def _on_task_finished(self, task) -> None:
        if task is self._task:
            self._task = None
            self._set_running(False)
        if task in self._dropped_tasks:
            self._dropped_tasks.remove(task)
        info = task.info or {}
        if info.get("id") != self._run_id or info.get("dataset") is not self._dataset:
            return  # superseded (another layer, or shut down): nothing to show
        if task.cancelled:
            self.status_label.setText("Cancelled." + (" The results below are from the previous run."
                                                      if self._findings else ""))
            return
        if task.result is None:
            self.status_label.setText(f"Assessment failed: {html.escape(str(task.error))}")
            return
        self._apply_result(info, task.result)

    def _apply_result(self, info: Dict, result: Dict) -> None:
        self._records = result["records"]
        self._track = info["track"]
        self._notes = list(info["notes"]) + list(result.get("notes") or [])
        self._resolution = result["resolution"]
        self._result_signature = info["signature"]
        self._finish(result["records"], result["findings"], result["skipped"], result["model"], info["window"])
        self._update_stale()

    def _finish(self, records, findings, skipped, model, window) -> None:
        findings = sorted(findings, key=lambda f: (f.kp_start, -la.SEVERITY_LEVEL.get(f.severity, 0)))
        self._findings = findings
        self._model = model
        self._populate()
        self._update_buttons()
        counts = {level: sum(1 for f in findings if f.severity == level) for level in SEVERITY_TEXT}
        lines = [f"<b>{len(findings)} range(s)</b>: "
                 f"<span style='color:{SEVERITY_COLOUR[Severity.ERROR]}'>{counts[Severity.ERROR]} red</span>, "
                 f"<span style='color:{SEVERITY_COLOUR[Severity.WARNING]}'>{counts[Severity.WARNING]} amber</span>, "
                 f"{counts[Severity.INFO]} info, from {records.n:,} records"
                 + (f" ({records.excluded:,} left out: no KP or invalid)" if records.excluded else "") + "."]
        if model is not None:
            text = f"Seabed: {len(model.spans)} modelled span(s)"
            below = sum(1 for span in model.spans if span.below_mbr)
            if below:
                text += f", {below} bending below the MBR at a support"
            if model.shortfalls:
                text += f"; {len(model.shortfalls)} stretch(es) where the laid cable is short of the seabed"
            lines.append(html.escape(text) + ".")
        if skipped:
            lines.append("Not run: " + "; ".join(html.escape(f"{self._labels.get(k, k)} ({v})")
                                                 for k, v in skipped.items()) + ".")
        problems = sorted(label for label, (_name, how) in self._resolution.items()
                          if how in ("ambiguous", "unknown"))
        if problems:
            lines.append(html.escape("Cable types not resolved (default used): "
                                     + ", ".join(f"{label} ({_HOW_TEXT[self._resolution[label][1]]})"
                                                 for label in problems[:8]) + ". Map them under Cable."))
        if any(p is None for p in records.cable):
            lines.append("Some records have no cable properties: set a default cable type.")
        lines.extend(html.escape(note) for note in self._notes)
        if self._track is None:
            lines.append("No touchdown positions: ranges cannot be shown on the map.")
        elif not records.has("td_lat", "td_lon"):
            lines.append("Touchdown position columns not set: the record positions were used.")
        self.status_label.setText("<br>".join(lines))
        panel = self._profile_panel(create=True)
        if panel is not None:
            panel.set_result(records, model, findings, window)

    # -- results --------------------------------------------------------------
    def _populate(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for index, finding in enumerate(self._findings):
            row = self.table.rowCount()
            self.table.insertRow(row)
            value = "" if finding.value is None or not np.isfinite(finding.value) else \
                f"{finding.value:.2f}{(' ' + finding.unit) if finding.unit else ''}"
            cells = [SEVERITY_TEXT.get(finding.severity, finding.severity), self._labels.get(finding.check_id),
                     finding.kp_start, finding.kp_end, finding.length_m, value, finding.message]
            for col, cell in enumerate(cells):
                item = QTableWidgetItem()
                if isinstance(cell, float):
                    item.setData(getattr(getattr(Qt, "ItemDataRole", Qt), "EditRole"),
                                 round(cell, 4 if col < 4 else 1))
                else:
                    item.setText(str(cell or ""))
                if col == 0:
                    item.setData(_USER_ROLE, index)
                    item.setForeground(QColor(SEVERITY_COLOUR.get(finding.severity, "#000000")))
                if col == len(cells) - 1:
                    item.setToolTip(finding.message)
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2, getattr(getattr(Qt, "SortOrder", Qt), "AscendingOrder"))  # by KP
        self.table.resizeColumnsToContents()
        current = self.show_combo.currentData()
        self.show_combo.blockSignals(True)
        self.show_combo.clear()
        self.show_combo.addItem(f"All checks ({len(self._findings)})", "")
        for check in la.all_checks():
            count = sum(1 for f in self._findings if f.check_id == check.check_id)
            if count:
                self.show_combo.addItem(f"{check.label} ({count})", check.check_id)
        self.show_combo.setCurrentIndex(max(self.show_combo.findData(current), 0) if current else 0)
        self.show_combo.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self, *_args) -> None:
        check_id = self.show_combo.currentData()
        red = self.red_only.isChecked()
        for row in range(self.table.rowCount()):
            finding = self._finding_at(row)
            hide = finding is not None and ((check_id and finding.check_id != check_id)
                                            or (red and finding.severity != Severity.ERROR))
            self.table.setRowHidden(row, bool(hide))

    def _finding_at(self, row: int) -> Optional[la.RangeFinding]:
        item = self.table.item(row, 0)
        index = item.data(_USER_ROLE) if item is not None else None
        if index is None or not (0 <= int(index) < len(self._findings)):
            return None
        return self._findings[int(index)]

    def _on_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            finding = self._finding_at(rows[0].row())
            if finding is not None:
                self.show_finding(finding, zoom_map=False)

    def _on_double_clicked(self, row: int, _col: int) -> None:
        finding = self._finding_at(row)
        if finding is None:
            return
        records = self._records
        if records is not None:
            if finding.rows:
                indices = np.asarray(finding.rows, dtype=int)
            else:  # seabed findings: the records depositing cable in the range
                with np.errstate(invalid="ignore"):
                    indices = np.nonzero((records.kp >= finding.kp_start) & (records.kp <= finding.kp_end))[0]
            if indices.size:
                self.controller.select_rows(sorted(int(records.rows[i]) for i in indices))
        # After select_rows, whose straight-line span highlight this replaces.
        self.show_finding(finding, zoom_map=True)

    def show_finding(self, finding: la.RangeFinding, zoom_map: bool = False) -> None:
        line = self.range_polyline(finding.kp_start, finding.kp_end)
        if line:
            self.controller.map_sync.highlight_polyline(line, zoom=zoom_map)
        panel = self._profile_panel(create=False)
        if panel is not None:
            panel.zoom_to(finding.kp_start, finding.kp_end)

    def range_polyline(self, kp_start: float, kp_end: float) -> List[Tuple[float, float]]:
        """WGS84 ``(lon, lat)`` vertices of the touchdown track between two KPs."""
        if self._track is None:
            return []
        lon, lat, kp = self._track
        lo, hi = min(kp_start, kp_end), max(kp_start, kp_end)
        if len(kp) < 2:
            return []
        inside = np.nonzero((kp > lo) & (kp < hi))[0]
        ends = [(float(np.interp(k, kp, lon)), float(np.interp(k, kp, lat))) for k in (lo, hi)]
        return [ends[0]] + [(float(lon[i]), float(lat[i])) for i in inside] + [ends[1]]

    def _update_buttons(self) -> None:
        has = bool(self._findings)
        self.map_button.setEnabled(has and self._track is not None)
        self.csv_button.setEnabled(has)

    def _profile_panel(self, create: bool):
        getter = getattr(self.controller, "seabed_profile_panel", None)
        return getter(create=create) if callable(getter) else None

    def _show_profile(self) -> None:
        panel = self._profile_panel(create=True)
        if panel is not None and self._records is not None and panel._records is None:
            panel.set_result(self._records, self._model, self._findings)

    # -- outputs ------------------------------------------------------------
    def build_ranges_layer(self, name: str) -> QgsVectorLayer:
        layer = QgsVectorLayer("LineString?crs=EPSG:4326", name, "memory")
        provider = layer.dataProvider()
        provider.addAttributes([
            QgsField("check", FIELD_TYPE_STRING), QgsField("level", FIELD_TYPE_STRING),
            QgsField("kp_start", FIELD_TYPE_DOUBLE), QgsField("kp_end", FIELD_TYPE_DOUBLE),
            QgsField("length_m", FIELD_TYPE_DOUBLE), QgsField("value", FIELD_TYPE_DOUBLE),
            QgsField("unit", FIELD_TYPE_STRING), QgsField("message", FIELD_TYPE_STRING),
        ])
        layer.updateFields()
        features = []
        for finding in self._findings:
            line = self.range_polyline(finding.kp_start, finding.kp_end)
            if len(line) < 2:
                continue
            if line[0] == line[-1] and len(line) == 2:
                # A single-record range: give it a few metres so it draws.
                line = self.range_polyline(finding.kp_start - 0.002, finding.kp_end + 0.002)
            feature = QgsFeature(layer.fields())
            feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in line]))
            value = finding.value if finding.value is not None and np.isfinite(finding.value) else None
            feature.setAttributes([self._labels.get(finding.check_id), SEVERITY_TEXT.get(finding.severity),
                                   finding.kp_start, finding.kp_end, finding.length_m, value,
                                   finding.unit, finding.message])
            features.append(feature)
        provider.addFeatures(features)
        layer.updateExtents()
        root = QgsRuleBasedRenderer.Rule(None)
        for severity in (Severity.INFO, Severity.WARNING, Severity.ERROR):
            symbol = QgsLineSymbol.createSimple({"color": SEVERITY_COLOUR[severity], "width": "1.2"})
            rule = QgsRuleBasedRenderer.Rule(symbol)
            rule.setLabel(SEVERITY_TEXT[severity])
            rule.setFilterExpression(f"\"level\" = '{SEVERITY_TEXT[severity]}'")
            root.appendChild(rule)
        layer.setRenderer(QgsRuleBasedRenderer(root))
        return layer

    def add_ranges_to_map(self) -> None:
        if not self._findings or self._track is None:
            return
        project = QgsProject.instance()
        if self._map_layer_id and project.mapLayer(self._map_layer_id) is not None:
            project.removeMapLayer(self._map_layer_id)
        source = self.controller.layer_name() or "lay data"
        layer = self.build_ranges_layer(f"Lay assessment - {source}")
        project.addMapLayer(layer)
        self._map_layer_id = layer.id()
        self.status_label.setText(f"Added {layer.featureCount()} range(s) to the map as a temporary layer "
                                  "(right-click &gt; Make Permanent to keep it).")

    def export_csv(self) -> None:
        if not self._findings:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export lay assessment", "lay_assessment.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            self.write_csv(path)
        except OSError as exc:
            QMessageBox.critical(self, "Lay Assessment", f"Could not write the CSV:\n{exc}")
            return
        self.status_label.setText(html.escape(f"Exported {len(self._findings)} range(s) to {path}."))

    def write_csv(self, path: str) -> None:
        extras = ("min_bend_radius_m", "max_reaction_kn", "allowable_kn", "tension_kn", "reach_m")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["level", "check", "kp_start", "kp_end", "length_m", "value", "unit"]
                            + list(extras) + ["message"])
            for f in self._findings:
                more = []
                for key in extras:
                    value = f.extra.get(key)
                    more.append("" if value is None or not np.isfinite(value) else f"{value:.3f}")
                writer.writerow([SEVERITY_TEXT.get(f.severity), self._labels.get(f.check_id),
                                 f"{f.kp_start:.4f}", f"{f.kp_end:.4f}", f"{f.length_m:.1f}",
                                 "" if f.value is None or not np.isfinite(f.value) else f"{f.value:.3f}",
                                 f.unit] + more + [f.message])

    # -- settings -----------------------------------------------------------
    def _setting(self, key: str, default=None):
        return QSettings().value(f"{_SETTINGS}/{key}", default)

    def _saved_mapping(self) -> Dict[str, str]:
        try:
            return dict(json.loads(self._setting("mapping", "{}") or "{}"))
        except (TypeError, ValueError):
            return {}

    def _save_settings(self) -> None:
        settings = QSettings()
        settings.setValue(f"{_SETTINGS}/mapping", json.dumps(self.mapping()))
        settings.setValue(f"{_SETTINGS}/units", self.units_combo.currentText())
        settings.setValue(f"{_SETTINGS}/cable_type", self.cable_combo.currentData() or "")
        settings.setValue(f"{_SETTINGS}/seabed", self.seabed_combo.currentData())
        settings.setValue(f"{_SETTINGS}/spacing", self.interval_spin.value())  # 0 = auto
        settings.setValue(f"{_SETTINGS}/smoothing", self.smoothing_edit.text())
        settings.setValue(f"{_SETTINGS}/filter", self.filter_edit.text())
        settings.setValue(f"{_SETTINGS}/zero_tension", self.zero_edit.text())
        settings.setValue(f"{_SETTINGS}/makeup", self.makeup_combo.currentData())
        settings.setValue(f"{_SETTINGS}/makeup_rpl", self.rpl_combo.currentData() or "")
        settings.setValue(f"{_SETTINGS}/makeup_fit", self.fit_combo.currentData() or "")
        checks = {check.check_id: {"enabled": self._check_boxes[check.check_id].isChecked(),
                                   "params": {name: editor.text() for name, editor
                                              in self._editors[check.check_id].items()}}
                  for check in la.all_checks()}
        settings.setValue(f"{_SETTINGS}/checks", json.dumps(checks))

    def _restore_settings(self) -> None:
        units = self._setting("units", "kN")
        index = self.units_combo.findText(str(units))
        if index >= 0:
            self.units_combo.setCurrentIndex(index)
        seabed = self._setting("seabed", SEABED_LAY_MODEL)
        index = self.seabed_combo.findData(seabed)
        self.seabed_combo.setCurrentIndex(index if index >= 0 else 1)
        try:
            # "spacing" (0 = auto) replaced "interval": earlier fixed spacings are not carried over.
            self.interval_spin.setValue(int(self._setting("spacing", 0)))
        except (TypeError, ValueError):
            pass
        self.smoothing_edit.setText(str(self._setting("smoothing", "") or ""))
        self.filter_edit.setText(str(self._setting("filter", "") or ""))
        self.zero_edit.setText(str(self._setting("zero_tension", f"{_ZERO_TENSION_DEFAULT:g}") or ""))
        index = self.makeup_combo.findData(self._setting("makeup", MAKEUP_DATA))
        self.makeup_combo.setCurrentIndex(max(index, 0))
        try:
            checks = json.loads(self._setting("checks", "{}") or "{}")
        except (TypeError, ValueError):
            checks = {}
        for check_id, state in (checks or {}).items():
            if check_id in self._check_boxes and isinstance(state, dict):
                self._check_boxes[check_id].setChecked(bool(state.get("enabled", True)))
                for name, text in (state.get("params") or {}).items():
                    editor = self._editors[check_id].get(name)
                    if editor is not None:
                        editor.setText(str(text))

    def shutdown(self) -> None:
        self._run_id += 1  # a late result is not applied to a closing window
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
        if self._library_dialog is not None:
            self._library_dialog.close()
