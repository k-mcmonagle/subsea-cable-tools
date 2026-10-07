# -*- coding: utf-8 -*-
"""Lay Assessment tab: suspension, loop-risk and tension checks as KP ranges.

The tab maps the active layer's columns onto the roles the checks need
(auto-detected, editable), picks the cable type from the cable library and,
optionally, a seabed source: the lay model's own touchdown depths, or a
raster / contour bathymetry sampled along the touchdown track with the
Depth Profile engine (in a background task). Findings are KP ranges listed
in a table; selecting one highlights it on the map and zooms the Seabed
Profile dock. The method is in ``docs/LAY_ASSESSMENT.md``; the maths lives
in :mod:`laydata.lay_assessment`.
"""

from __future__ import annotations

import csv
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
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
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
_WINDOW_MODAL = getattr(getattr(Qt, "WindowModality", Qt), "WindowModal")
_VERTICAL = getattr(getattr(Qt, "Orientation", Qt), "Vertical")

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


class AssessmentPanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.persist = True        # False in tests: never touch the user's QSettings
        self.synchronous = False   # True in tests: sample the seabed on this thread
        self._dataset = None
        self._records: Optional[la.LayRecords] = None
        self._findings: List[la.RangeFinding] = []
        self._model: Optional[la.SeabedModel] = None
        self._track: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]] = None
        self._task = None
        self._progress = None
        self._pending = None
        self._library_dialog = None
        self._map_layer_id: Optional[str] = None
        self._role_combos: Dict[str, QComboBox] = {}
        self._check_boxes: Dict[str, QCheckBox] = {}
        self._editors: Dict[str, Dict[str, QLineEdit]] = {}
        self._labels = {check.check_id: check.label for check in la.all_checks()}
        self._library_rows: List[Dict] = []
        self._unmatched: set = set()

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
        config_layout.addWidget(self._build_cable_group())
        config_layout.addWidget(self._build_seabed_group())
        config_layout.addWidget(self._build_columns_group())
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
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        results_layout.addWidget(self.status_label)

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
        self._refresh_cable_types()

    # -- construction -------------------------------------------------------
    def _build_cable_group(self) -> QGroupBox:
        group = QGroupBox("Cable")
        form = QFormLayout(group)
        row = QHBoxLayout()
        self.library_label = QLabel("")
        self.library_label.setWordWrap(True)
        row.addWidget(self.library_label, 1)
        library_btn = QPushButton("Cable library…")
        library_btn.setToolTip("Create or edit your cable type library (weights, NPTS / NOTS / NTTS / CBL)")
        library_btn.clicked.connect(self.open_library)
        row.addWidget(library_btn)
        form.addRow(row)
        self.cable_combo = QComboBox()
        self.cable_combo.setToolTip("Used for every record, or for records whose cable type column "
                                    "does not match a library name or alias.")
        form.addRow("Default cable type", self.cable_combo)
        self.units_combo = QComboBox()
        self.units_combo.addItems(list(la.TENSION_UNITS))
        self.units_combo.setToolTip("Units of the tension columns in the data")
        form.addRow("Tension units in data", self.units_combo)
        return group

    def _build_seabed_group(self) -> QGroupBox:
        group = QGroupBox("Seabed")
        form = QFormLayout(group)
        self.seabed_combo = QComboBox()
        for key, text in _SEABED_CHOICES:
            self.seabed_combo.addItem(text, key)
        self.seabed_combo.setToolTip(
            "Seabed the cable is rested on for the Suspensions and Slack vs seabed checks. The lay "
            "model's touchdown depths need no other data; bathymetry is sampled along the "
            "touchdown track (grid XYZ soundings first with Create Raster from XYZ).")
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
        self.interval_spin.setRange(1, 1000)
        self.interval_spin.setValue(5)
        self.interval_spin.setSuffix(" m")
        self.interval_spin.setToolTip("Seabed sample spacing along the track (rasters and the lay model's "
                                      "depths; contours are used at their crossings)")
        form.addRow("Sample every", self.interval_spin)
        kp_row = QHBoxLayout()
        self.kp_from = QLineEdit()
        self.kp_from.setPlaceholderText("start")
        self.kp_to = QLineEdit()
        self.kp_to.setPlaceholderText("end")
        kp_row.addWidget(self.kp_from)
        kp_row.addWidget(QLabel("to"))
        kp_row.addWidget(self.kp_to)
        form.addRow("Seabed KP range", kp_row)
        self._update_seabed_widgets()
        return group

    def _build_columns_group(self) -> QGroupBox:
        group = QGroupBox("Columns (auto-detected)")
        group.setToolTip("Which column holds each quantity. Detected from the column names; "
                         "change any that are wrong, or set (none).")
        form = QFormLayout(group)
        for role in la.ROLES:
            combo = QComboBox()
            combo.setToolTip(role.help)
            label = QLabel(role.label)
            label.setToolTip(role.help)
            form.addRow(label, combo)
            self._role_combos[role.key] = combo
        return group

    def _build_check_group(self, check: la.CheckDef) -> QGroupBox:
        group = QGroupBox(check.label)
        group.setToolTip(check.description)
        form = QFormLayout(group)
        enable = QCheckBox("Enabled")
        enable.setChecked(check.default_enabled)
        enable.setToolTip(check.description)
        self._check_boxes[check.check_id] = enable
        form.addRow(enable)
        self._editors[check.check_id] = {}
        for spec in check.params:
            editor = QLineEdit("" if spec.default is None else f"{spec.default:g}")
            if spec.help:
                editor.setToolTip(spec.help)
            self._editors[check.check_id][spec.name] = editor
            form.addRow(spec.label, editor)
        return group

    # -- dataset / widgets ---------------------------------------------------
    def set_dataset(self, dataset) -> None:
        if dataset is self._dataset:
            return
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

    def _clear_results(self) -> None:
        """Forget results: their record indices belong to the previous dataset."""
        if self._task is not None:
            return  # the running assessment finishes on the data it started with
        self._records = None
        self._findings = []
        self._model = None
        self._track = None
        self.table.setRowCount(0)
        self._update_buttons()
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
        self.cable_combo.clear()
        self.cable_combo.addItem("(none)", "")
        for row in rows:
            self.cable_combo.addItem(row["name"], row["name"])
        index = self.cable_combo.findData(current)
        if index > 0:
            self.cable_combo.setCurrentIndex(index)
        elif len(rows) == 1:
            self.cable_combo.setCurrentIndex(1)
        if not path:
            self.library_label.setText("No cable library yet: weights and tension limits come from it.")
        elif not library.is_library(path):
            self.library_label.setText(f"Library not found: {path}")
        else:
            self.library_label.setText(f"{len(rows)} type(s) in {path}")
        self._library_rows = rows

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

    def _cable_resolver(self):
        rows = self._library_rows
        default = library.find_row(rows, self.cable_combo.currentData())
        self._unmatched = set()

        def resolve(text):
            row = library.find_row(rows, text)
            if row is None and text:
                self._unmatched.add(text)
            return library.to_props(row or default)
        return resolve

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
        def read(edit):
            text = edit.text().strip().replace(",", ".")
            return float(text) if text else None
        try:
            lo, hi = read(self.kp_from), read(self.kp_to)
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
            self.status_label.setText(f"Assessment failed: {exc}")
            self.run_button.setEnabled(True)

    def _run(self) -> None:
        if self._dataset is None:
            self.status_label.setText("Load a data layer first.")
            return
        if self._task is not None:
            return
        try:
            enabled = {check.check_id: self._params(check) for check in la.all_checks()
                       if self._check_boxes[check.check_id].isChecked()}
            window = self._kp_window()
            records = la.build_records(self._dataset, self.mapping(), self.units_combo.currentText(),
                                       self._cable_resolver())
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        if records.n == 0:
            self.status_label.setText("No records with a KP to assess.")
            return
        if self.persist:
            self._save_settings()
        findings, skipped = la.run_record_checks(records, enabled)
        self._records = records
        try:
            self._track = la.track_vertices(records)
        except ValueError:
            self._track = None
        seabed_checks = [c for c in la.SEABED_CHECKS if c.check_id in enabled]
        mode = self.seabed_combo.currentData()
        if not seabed_checks or mode == SEABED_NONE:
            for check in seabed_checks:
                skipped[check.check_id] = "needs a seabed source"
            self._finish(records, findings, skipped, None, window)
            return
        for check in seabed_checks:
            missing = la.missing_inputs(check, records)
            if missing:
                skipped[check.check_id] = "needs " + ", ".join(missing)
                enabled.pop(check.check_id)
        if "suspension" not in enabled and "terrain_slack" not in enabled:
            self._finish(records, findings, skipped, None, window)
            return
        if mode == SEABED_LAY_MODEL:
            if "terrain_slack" in enabled:
                skipped["terrain_slack"] = "compares against bathymetry, not the lay model's own seabed"
                enabled.pop("terrain_slack")
            if not records.has("td_depth"):
                skipped["suspension"] = "needs the touchdown water depth column"
                self._finish(records, findings, skipped, None, window)
                return
            x, kp, depth = self._lay_model_profile(records, window)
            self._model_and_finish(records, findings, skipped, enabled, x, kp, depth,
                                   records.kp * 1000.0, window)
            return
        try:
            request, vertex_chainage, vertex_kp = self._seabed_request(records, mode, window)
        except ValueError as exc:
            for check in seabed_checks:
                skipped.setdefault(check.check_id, str(exc))
            self._finish(records, findings, skipped, None, window)
            return
        self._pending = (records, findings, skipped, enabled, vertex_chainage, vertex_kp, window)
        if self.synchronous:
            self._apply_profile(dpc.run_profile(request))
            return
        self._start_task(request)

    def _lay_model_profile(self, records, window):
        step = float(self.interval_spin.value())
        kp = records.kp
        depth = np.abs(records.get("td_depth"))
        ok = np.isfinite(kp) & np.isfinite(depth)
        if window is not None:
            ok &= (kp >= window[0]) & (kp <= window[1])
        kp, depth = kp[ok], depth[ok]
        if kp.size < 2:
            return np.array([]), np.array([]), np.array([])
        key = np.floor(kp * 1000.0 / step).astype(np.int64)
        order = np.argsort(key, kind="stable")
        key, kp, depth = key[order], kp[order], depth[order]
        starts = np.concatenate(([0], np.nonzero(np.diff(key))[0] + 1, [len(key)]))
        kp_bins = np.array([np.median(kp[a:b]) for a, b in zip(starts[:-1], starts[1:])])
        depth_bins = np.array([np.median(depth[a:b]) for a, b in zip(starts[:-1], starts[1:])])
        return kp_bins * 1000.0, kp_bins, depth_bins

    def _seabed_request(self, records, mode, window):
        if self._track is None:
            raise ValueError("needs touchdown positions")
        lon, lat, kp = self._track
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
        params = dpc.ProfileParams(mode=dpc.RASTER if mode == SEABED_RASTER else dpc.CONTOURS,
                                   interval_m=int(self.interval_spin.value()), max_samples=200000,
                                   per_raster=False)
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

    def _start_task(self, request) -> None:
        self.run_button.setEnabled(False)
        self.status_label.setText("Sampling the seabed along the touchdown track…")
        self._progress = QProgressDialog("Sampling the seabed…", "Cancel", 0, 100, self)
        self._progress.setWindowTitle("Lay Assessment")
        self._progress.setWindowModality(_WINDOW_MODAL)
        self._progress.setMinimumDuration(0)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        task = dpc.DepthProfileTask(request, self._on_task_finished, "Lay Assessment seabed")
        self._task = task
        task.progressChanged.connect(lambda value: self._progress.setValue(int(value)) if self._progress else None)
        self._progress.canceled.connect(task.cancel)
        QgsApplication.taskManager().addTask(task)
        self._progress.show()

    def _on_task_finished(self, task) -> None:
        result, error, cancelled = task.result, task.error, task.cancelled
        task.request = None
        self._task = None
        if self._progress is not None:
            self._progress.reset()
            self._progress.deleteLater()
            self._progress = None
        self.run_button.setEnabled(True)
        if self._pending is None:  # shut down meanwhile
            return
        if result is None:
            records, findings, skipped, enabled, _c, _k, window = self._pending
            reason = "cancelled" if cancelled else f"seabed sampling failed: {error}"
            for check_id in ("suspension", "terrain_slack"):
                if check_id in enabled:
                    skipped[check_id] = reason
            self._pending = None
            self._finish(records, findings, skipped, None, window)
            return
        self._apply_profile(result)

    def _apply_profile(self, result) -> None:
        try:
            self._apply_profile_result(result)
        except Exception as exc:
            log_exception("Lay Assessment: modelling the seabed failed")
            self.status_label.setText(f"Assessment failed: {exc}")

    def _apply_profile_result(self, result) -> None:
        records, findings, skipped, enabled, vertex_chainage, vertex_kp, window = self._pending
        self._pending = None
        x = np.asarray(result.kp_values, dtype=float) * 1000.0
        depth = np.array([np.nan if v is None else float(v) for v in result.depth_values])
        if result.status and not np.isfinite(depth).any():
            for check_id in ("suspension", "terrain_slack"):
                if check_id in enabled:
                    skipped[check_id] = result.status
            self._finish(records, findings, skipped, None, window)
            return
        kp = la.chainage_to_kp(vertex_chainage, vertex_kp, x)
        record_x = la.kp_to_chainage(vertex_chainage, vertex_kp, records.kp)
        with np.errstate(invalid="ignore"):  # interp clamps; records off the track are not on it
            record_x[(records.kp < vertex_kp[0]) | (records.kp > vertex_kp[-1])] = np.nan
        self._model_and_finish(records, findings, skipped, enabled, x, kp, depth, record_x, window)

    def _model_and_finish(self, records, findings, skipped, enabled, x, kp, depth, record_x, window) -> None:
        if len(x) < 3:
            for check_id in ("suspension", "terrain_slack"):
                if check_id in enabled:
                    skipped[check_id] = "too few seabed samples"
            self._finish(records, findings, skipped, None, window)
            return
        params = enabled.get("suspension") or la.default_params(la.check_by_id("suspension"))
        model = la.model_seabed(x, kp, depth, records, record_x, params["zero_tension_kn"],
                                params["min_gap_m"], params["min_length_m"])
        seabed_findings: List[la.RangeFinding] = []
        if "suspension" in enabled:
            seabed_findings.extend(la.seabed_span_findings(model, enabled["suspension"]))
        if "terrain_slack" in enabled:
            terrain = enabled["terrain_slack"]
            windows = la.terrain_shortfall(model, records, record_x, terrain["window_m"])
            seabed_findings.extend(la.terrain_findings(model, windows, terrain))
        self._finish(records, findings + seabed_findings, skipped, model, window)

    def _finish(self, records, findings, skipped, model, window) -> None:
        findings = sorted(findings, key=lambda f: (f.kp_start, -la.SEVERITY_LEVEL.get(f.severity, 0)))
        self._findings = findings
        self._model = model
        self._populate()
        self._update_buttons()
        counts = {level: sum(1 for f in findings if f.severity == level) for level in SEVERITY_TEXT}
        parts = [f"{len(findings)} range(s): {counts[Severity.ERROR]} red, {counts[Severity.WARNING]} amber, "
                 f"{counts[Severity.INFO]} info, from {records.n:,} records"
                 + (f" ({records.excluded:,} left out: no KP or invalid)" if records.excluded else "") + "."]
        if model is not None:
            parts.append(f"Seabed: {len(model.spans)} modelled span(s).")
        if skipped:
            parts.append("Not run: " + "; ".join(f"{self._labels.get(k, k)} ({v})" for k, v in skipped.items()) + ".")
        unmatched = sorted(self._unmatched)
        if unmatched:
            parts.append("Cable types not in the library (default used): " + ", ".join(unmatched[:8]) + ".")
        if self._track is None:
            parts.append("No touchdown positions: ranges cannot be shown on the map.")
        elif not records.has("td_lat", "td_lon"):
            parts.append("Touchdown position columns not set: the record positions were used.")
        self.status_label.setText(" ".join(parts))
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
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2, getattr(getattr(Qt, "SortOrder", Qt), "AscendingOrder"))  # by KP
        self.table.resizeColumnsToContents()

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
                                  "(right-click > Make Permanent to keep it).")

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
        self.status_label.setText(f"Exported {len(self._findings)} range(s) to {path}.")

    def write_csv(self, path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["level", "check", "kp_start", "kp_end", "length_m", "value", "unit", "message"])
            for f in self._findings:
                writer.writerow([SEVERITY_TEXT.get(f.severity), self._labels.get(f.check_id),
                                 f"{f.kp_start:.4f}", f"{f.kp_end:.4f}", f"{f.length_m:.1f}",
                                 "" if f.value is None or not np.isfinite(f.value) else f"{f.value:.3f}",
                                 f.unit, f.message])

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
        settings.setValue(f"{_SETTINGS}/interval", self.interval_spin.value())
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
            self.interval_spin.setValue(int(self._setting("interval", 5)))
        except (TypeError, ValueError):
            pass
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
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
        if self._library_dialog is not None:
            self._library_dialog.close()
