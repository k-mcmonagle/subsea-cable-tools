# -*- coding: utf-8 -*-
"""Guided "Import RPL..." wizard for the Cable Route Workbench.

Three pages:

1. Source & destination — pick a workbook/CSV, scan worksheets in the
   background, choose/create the Workbench segment (route), revision label
   and RPL kind.
2. Detection & mapping — the actual worksheet in a virtual table view with
   highlighted header/point/segment rows, correctable layout, data range,
    coordinate encoding, source CRS, units, and per-column mapping/exclusion,
    plus saved mapping profiles keyed by the header signature.
3. Review & import — counts, stated vs computed lengths, grouped navigable
   diagnostics, warning acknowledgement, targeted sign-flip fix, temporary
   map preview. Errors block import; the finish button names the target
   revision.

Orchestration only: reading/detection/parsing/validation live in the pure
``rpl_import`` package; geodesy/CRS/commit live in ``rpl_import_service``.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import (
    QAbstractTableModel, QModelIndex, QSettings, Qt, QTimer, pyqtSignal,
)
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QTableView, QTableWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWizard, QWizardPage, QWidget,
)

from ..rpl_import import model as im
from ..rpl_import import detect as idetect
from ..rpl_import import parser as iparser
from ..rpl_import import reader as ireader
from ..rpl_import import validate as ivalidate
from ..rpl_import.model import ImportProfile
from . import schema
from .rpl_import_service import (
    CommitError, CommitRequest, commit_import, default_slack_mode,
    geodesy_fns, make_wgs84_distance_area, measurement_config,
    reconcile_model, to_rpl_model, transform_projected,
)
from .store import WorkbenchStore

PROFILE_SETTINGS_GROUP = "SubseaCableTools/RplImport/profiles"
_INCLUDE_EXTRA = "__include_as_extra__"

_FIELD_LABELS = [
    (im.PF_POS_NO, "Position number"),
    (im.PF_EVENT, "Event"),
    (im.PF_LAT_DEG, "Latitude degrees"),
    (im.PF_LAT_MIN, "Latitude minutes"),
    (im.PF_LAT_HEMI, "Latitude hemisphere"),
    (im.PF_LON_DEG, "Longitude degrees"),
    (im.PF_LON_MIN, "Longitude minutes"),
    (im.PF_LON_HEMI, "Longitude hemisphere"),
    (im.PF_LAT_TEXT, "Latitude (text/decimal)"),
    (im.PF_LON_TEXT, "Longitude (text/decimal)"),
    (im.PF_EASTING, "Easting"),
    (im.PF_NORTHING, "Northing"),
    (im.PF_DIST_CUM, "KP / cumulative distance"),
    (im.PF_CABLE_DIST_CUM, "Cumulative cable distance"),
    (im.PF_DEPTH, "Depth"),
    (im.PF_REMARKS, "Remarks"),
    (im.PF_CHART_NO, "Chart number"),
    (im.SF_BEARING, "Bearing"),
    (im.SF_DIST, "Span distance"),
    (im.SF_SLACK, "Slack"),
    (im.SF_CABLE_DIST, "Cable span distance"),
    (im.SF_CABLE_CODE, "Cable code"),
    (im.SF_FIBER_PAIR, "Fibre pair"),
    (im.SF_CABLE_TYPE, "Cable type"),
    (im.SF_LAY_DIRECTION, "Lay direction"),
    (im.SF_LAY_VESSEL, "Lay vessel"),
    (im.SF_PROTECTION, "Protection method"),
    (im.SF_DATE_INSTALLED, "Date installed"),
    (im.SF_TARGET_BURIAL, "Target burial depth"),
    (im.SF_BURIAL, "Burial depth"),
    (im.SF_TERRITORIAL, "Territorial water"),
    (im.SF_EEZ, "EEZ"),
]

_ENCODINGS = [
    (im.COORD_SPLIT_DDM, "Degrees / minutes / hemisphere columns"),
    (im.COORD_DDM_TEXT, "Combined degrees-minutes text"),
    (im.COORD_DECIMAL_DEGREES, "Signed decimal degrees"),
    (im.COORD_PROJECTED, "Projected easting / northing"),
]


def _col_letter(index: int) -> str:
    """1-based column index -> Excel letters."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


# ---------------------------------------------------------------------------
# Grid preview model
# ---------------------------------------------------------------------------
class _GridModel(QAbstractTableModel):
    """Read-only view over a SourceGrid with row-role highlighting."""

    COLOR_HEADER = QColor(120, 160, 220, 90)
    COLOR_POINT = QColor(120, 200, 120, 60)
    COLOR_SEGMENT = QColor(240, 200, 100, 60)
    COLOR_OUTSIDE = QColor(128, 128, 128, 40)
    COLOR_ERROR = QColor(230, 90, 90, 110)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grid: Optional[ireader.SourceGrid] = None
        self._profile: Optional[ImportProfile] = None
        self._error_rows: set = set()

    def set_grid(self, grid, profile):
        self.beginResetModel()
        self._grid = grid
        self._profile = profile
        self._error_rows = set()
        self.endResetModel()

    def set_profile(self, profile, error_rows=None):
        self._profile = profile
        self._error_rows = set(error_rows or [])
        if self._grid is not None:
            top = self.index(0, 0)
            bottom = self.index(self.rowCount() - 1,
                                max(0, self.columnCount() - 1))
            self.dataChanged.emit(top, bottom, [])

    def rowCount(self, parent=QModelIndex()):
        return 0 if self._grid is None else self._grid.n_rows

    def columnCount(self, parent=QModelIndex()):
        return 0 if self._grid is None else self._grid.n_cols

    def headerData(self, section, orientation, role):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _col_letter(section + 1)
        return str(section + 1)

    def _row_role(self, row: int) -> Optional[QColor]:
        profile = self._profile
        if profile is None:
            return None
        if row in self._error_rows:
            return self.COLOR_ERROR
        if row in (profile.header_rows or []):
            return self.COLOR_HEADER
        start, end = profile.data_start_row, profile.data_end_row
        if not start or row < start or row > end:
            return self.COLOR_OUTSIDE
        if profile.layout == im.LAYOUT_ALTERNATING:
            return (self.COLOR_POINT if (row - start) % 2 == 0
                    else self.COLOR_SEGMENT)
        return self.COLOR_POINT

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if self._grid is None or not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            value = self._grid.cell(index.row() + 1, index.column() + 1)
            return "" if value is None else str(value)
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._row_role(index.row() + 1)
        return None


# ---------------------------------------------------------------------------
# Page 1 — source & destination
# ---------------------------------------------------------------------------
class _SourcePage(QWizardPage):
    def __init__(self, wizard: "RplImportWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Source and destination")
        self.setSubTitle("Choose the RPL workbook and where to register it.")

        layout = QVBoxLayout(self)

        file_row = QHBoxLayout()
        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("Excel (.xlsx/.xlsm) or CSV file...")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(browse)
        layout.addLayout(file_row)

        self.scan_label = QLabel("")
        layout.addWidget(self.scan_label)

        self.sheet_list = QListWidget()
        self.sheet_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.sheet_list.itemSelectionChanged.connect(self._sheet_changed)
        layout.addWidget(self.sheet_list, 1)

        dest = QGroupBox("Destination")
        form = QFormLayout(dest)
        self.route_combo = QComboBox()
        self.route_combo.setEditable(True)
        self.route_combo.editTextChanged.connect(
            lambda _t: self._update_rev_default())
        form.addRow("Segment / route", self.route_combo)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Planned", "planned")
        self.kind_combo.addItem("As-laid", "as_laid")
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        form.addRow("RPL kind", self.kind_combo)
        self.rev_edit = QLineEdit()
        self.rev_edit.setPlaceholderText("blank = next Rev N")
        form.addRow("Revision label", self.rev_edit)
        self.slack_combo = QComboBox()
        self.slack_combo.addItem("Default for kind", "")
        self.slack_combo.addItem("Hold slack (planned behaviour)", "hold_slack")
        self.slack_combo.addItem("Hold cable distance (as-laid behaviour)",
                                 "hold_cable")
        form.addRow("Slack mode (advanced)", self.slack_combo)
        self.slack_hint = QLabel("")
        self.slack_hint.setWordWrap(True)
        form.addRow("", self.slack_hint)
        layout.addWidget(dest)

        self._scan_generation = 0
        self._results: List[idetect.DetectionResult] = []
        self.file_edit.editingFinished.connect(self._file_entered)
        self._kind_changed()

    # -- events ---------------------------------------------------------------
    def _browse(self):
        path, _f = QFileDialog.getOpenFileName(
            self, "Select RPL file", "",
            "RPL files (*.xlsx *.xlsm *.csv);;All files (*)")
        if path:
            self.file_edit.setText(path)
            self._start_scan(path)

    def _file_entered(self):
        path = self.file_edit.text().strip()
        if path and os.path.isfile(path):
            self._start_scan(path)

    def _start_scan(self, path: str):
        self._scan_generation += 1
        generation = self._scan_generation
        self.sheet_list.clear()
        self._results = []
        self.scan_label.setText("Scanning worksheets...")
        QTimer.singleShot(
            0, lambda: self._scan_on_gui_thread(path, generation))

        base = os.path.splitext(os.path.basename(path))[0]
        if not self.route_combo.currentText().strip():
            self.route_combo.setEditText(base)

    def _scan_on_gui_thread(self, path: str, generation: int):
        if generation != self._scan_generation:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            grids = ireader.load_sample_grids(path)
            results = idetect.score_sheets(grids)
        except Exception as exc:
            if generation == self._scan_generation:
                self._scan_failed(str(exc))
        else:
            if generation == self._scan_generation:
                self._scan_done(results)
        finally:
            QApplication.restoreOverrideCursor()

    def _scan_done(self, results):
        self._results = list(results)
        self.sheet_list.clear()
        for result in self._results:
            positions = result.position_count
            label = "%s — %d positions (%d%% confidence)" % (
                result.profile.sheet, positions, round(result.confidence * 100))
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, result.profile.sheet)
            self.sheet_list.addItem(item)
        self.scan_label.setText(
            "Select the worksheet holding the RPL (best candidate preselected)."
            if self._results else "No worksheets found.")
        if self._results:
            self.sheet_list.setCurrentRow(0)
        self.completeChanged.emit()

    def _scan_failed(self, message: str):
        self.scan_label.setText("Could not read the file: " + message)
        self.completeChanged.emit()

    def _sheet_changed(self):
        self.completeChanged.emit()

    def _kind_changed(self):
        kind = self.kind_combo.currentData()
        mode = default_slack_mode(kind)
        self.slack_hint.setText(
            "Future map edits will hold %s and recompute the other quantity. "
            "Import itself never rewrites stated values." % (
                "slack (planned)" if mode == "hold_slack"
                else "cable distance (as-laid)"))

    def _update_rev_default(self):
        route_name = self.route_combo.currentText().strip()
        store = self.wiz.store
        if not route_name or store is None:
            self.rev_edit.setPlaceholderText("blank = next Rev N")
            return
        try:
            route = next((r for r in store.list_routes()
                          if (r.get("name") or "").strip().lower()
                          == route_name.lower()), None)
            if route:
                next_label = schema.next_rev_label(
                    store.revisions_of_route(route["route_id"]))
                self.rev_edit.setPlaceholderText(f"blank = {next_label}")
            else:
                self.rev_edit.setPlaceholderText("blank = Rev 1 (new segment)")
        except Exception:
            self.rev_edit.setPlaceholderText("blank = next Rev N")

    # -- wizard hooks ---------------------------------------------------------
    def initializePage(self):
        self.route_combo.clear()
        if self.wiz.store is not None:
            try:
                for route in self.wiz.store.list_routes():
                    self.route_combo.addItem(route.get("name") or "")
            except Exception:
                pass
        self.route_combo.setEditText("")

    def isComplete(self):
        return bool(self.file_edit.text().strip()
                    and self.sheet_list.currentItem() is not None)

    def validatePage(self):
        if not self.route_combo.currentText().strip():
            base = os.path.splitext(
                os.path.basename(self.file_edit.text().strip()))[0]
            self.route_combo.setEditText(base or "Segment")
        sheet = self.sheet_list.currentItem().data(Qt.ItemDataRole.UserRole)
        result = next((r for r in self._results
                       if r.profile.sheet == sheet), None)
        self.wiz.set_source(self.file_edit.text().strip(), result,
                            self._results)
        return True


# ---------------------------------------------------------------------------
# Page 2 — detection & mapping
# ---------------------------------------------------------------------------
class _MappingPage(QWizardPage):
    def __init__(self, wizard: "RplImportWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Check what was detected")
        self.setSubTitle(
            "Correct the layout, data range, coordinates, units, and column "
            "mapping. Choose Include as extra for any additional columns "
            "you want to retain.")
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._loading = False
        self._syncing_sections = False
        self._applied_signatures: set = set()

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter = splitter
        splitter.setChildrenCollapsible(False)
        splitter.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter, 1)

        self.grid_model = _GridModel(self)
        self.table = QTableView()
        self.table.setModel(self.grid_model)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setDefaultSectionSize(130)
        self.table.verticalHeader().setFixedWidth(44)
        self.table.verticalHeader().setDefaultSectionSize(20)
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.setMinimumHeight(360)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(2)
        mapping_title = QLabel(
            "<b>Column mapping</b> &nbsp; Choose what each source column "
            "contains. Unidentified columns start excluded.")
        mapping_title.setWordWrap(True)
        preview_layout.addWidget(mapping_title)

        self.mapping_table = QTableWidget(1, 0)
        self.mapping_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mapping_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self.mapping_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.mapping_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.mapping_table.horizontalHeader().setVisible(False)
        self.mapping_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self.mapping_table.horizontalHeader().setDefaultSectionSize(130)
        self.mapping_table.verticalHeader().setFixedWidth(44)
        self.mapping_table.verticalHeader().setDefaultSectionSize(32)
        self.mapping_table.setVerticalHeaderLabels([""])
        self.mapping_table.setFixedHeight(36)
        preview_layout.addWidget(self.mapping_table)
        preview_layout.addWidget(self.table, 1)
        splitter.addWidget(preview_panel)

        self.table.horizontalScrollBar().valueChanged.connect(
            self.mapping_table.horizontalScrollBar().setValue)
        self.table.horizontalHeader().sectionResized.connect(
            self._preview_section_resized)
        self.mapping_table.horizontalHeader().sectionResized.connect(
            self._mapping_section_resized)

        panel = QWidget()
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(390)
        panel_layout = QVBoxLayout(panel)

        structure = QGroupBox("Structure")
        form = QGridLayout(structure)
        self.layout_combo = QComboBox()
        self.layout_combo.addItem("Alternating point / segment rows",
                                  im.LAYOUT_ALTERNATING)
        self.layout_combo.addItem("One position per row (flat)", im.LAYOUT_FLAT)
        form.addWidget(QLabel("Layout"), 0, 0)
        form.addWidget(self.layout_combo, 0, 1)
        self.flat_combo = QComboBox()
        self.flat_combo.addItem("Span fields describe the arriving span",
                                im.FLAT_ARRIVING)
        self.flat_combo.addItem("Span fields describe the departing span",
                                im.FLAT_DEPARTING)
        self.flat_label = QLabel("Span values")
        self.flat_combo.setItemText(
            0, "Apply to the span arriving at this position")
        self.flat_combo.setItemText(
            1, "Apply to the span departing from this position")
        self.flat_combo.setToolTip(
            "For one-position-per-row files, choose whether span values on "
            "a row connect from the previous position or to the next one.")
        form.addWidget(self.flat_label, 1, 0)
        form.addWidget(self.flat_combo, 1, 1)
        self.start_spin = QSpinBox()
        self.start_spin.setRange(1, 1000000)
        self.end_spin = QSpinBox()
        self.end_spin.setRange(1, 1000000)
        self.header_rows_label = QLabel("Not detected")
        self.header_rows_label.setToolTip(
            "Header rows are combined per source column for field detection.")
        form.addWidget(QLabel("Header rows"), 2, 0)
        form.addWidget(self.header_rows_label, 2, 1)
        form.addWidget(QLabel("Data rows"), 3, 0)
        range_row = QHBoxLayout()
        range_row.addWidget(self.start_spin)
        range_row.addWidget(QLabel("to"))
        range_row.addWidget(self.end_spin)
        form.addLayout(range_row, 3, 1)
        self.encoding_combo = QComboBox()
        for key, label in _ENCODINGS:
            self.encoding_combo.addItem(label, key)
        form.addWidget(QLabel("Coordinates"), 4, 0)
        form.addWidget(self.encoding_combo, 4, 1)
        self.crs_widget = self._make_crs_widget()
        form.addWidget(QLabel("Source CRS"), 5, 0)
        form.addWidget(self.crs_widget, 5, 1)
        self.redetect_btn = QPushButton("Re-run detection")
        self.redetect_btn.setToolTip(
            "Discard the current mapping (including any applied saved "
            "profile) and re-detect everything from the sheet contents.")
        self.redetect_btn.clicked.connect(self._redetect)
        form.addWidget(self.redetect_btn, 6, 0, 1, 2)
        self.detect_reason_label = QLabel("")
        self.detect_reason_label.setWordWrap(True)
        self.detect_reason_label.setStyleSheet("color: gray;")
        form.addWidget(self.detect_reason_label, 7, 0, 1, 2)
        panel_layout.addWidget(structure)

        units = QGroupBox("Units")
        units_form = QGridLayout(units)
        self.dist_unit = QComboBox()
        self.cable_unit = QComboBox()
        for combo in (self.dist_unit, self.cable_unit):
            for unit in im.DISTANCE_UNITS:
                combo.addItem(unit, unit)
        self.depth_unit = QComboBox()
        for unit in im.DEPTH_UNITS:
            self.depth_unit.addItem(unit, unit)
        self.slack_unit = QComboBox()
        self.slack_unit.addItem("percent (%)", False)
        self.slack_unit.addItem("ratio (1.02 style)", True)
        units_form.addWidget(QLabel("Route distances"), 0, 0)
        units_form.addWidget(self.dist_unit, 0, 1)
        units_form.addWidget(QLabel("Cable distances"), 0, 2)
        units_form.addWidget(self.cable_unit, 0, 3)
        units_form.addWidget(QLabel("Depth"), 1, 0)
        units_form.addWidget(self.depth_unit, 1, 1)
        units_form.addWidget(QLabel("Slack"), 1, 2)
        units_form.addWidget(self.slack_unit, 1, 3)
        panel_layout.addWidget(units)

        mapping_profile = QGroupBox("Mapping profile")
        mapping_profile_layout = QVBoxLayout(mapping_profile)
        self.profile_label = QLabel("")
        self.profile_label.setWordWrap(True)
        mapping_profile_layout.addWidget(self.profile_label)
        profile_buttons = QHBoxLayout()
        save_profile = QPushButton("Save...")
        save_profile.setToolTip("Save this mapping for matching headers")
        save_profile.clicked.connect(self._save_profile)
        delete_profile = QPushButton("Delete")
        delete_profile.setToolTip("Delete the saved mapping for these headers")
        delete_profile.clicked.connect(self._delete_profile)
        profile_buttons.addWidget(save_profile)
        profile_buttons.addWidget(delete_profile)
        mapping_profile_layout.addLayout(profile_buttons)
        panel_layout.addWidget(mapping_profile)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        panel_layout.addStretch(1)
        panel_layout.addWidget(self.status_label)

        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([1000, 340])

        for widget in (self.layout_combo, self.flat_combo, self.encoding_combo,
                       self.dist_unit, self.cable_unit, self.depth_unit,
                       self.slack_unit):
            widget.currentIndexChanged.connect(self._controls_changed)
        self.start_spin.valueChanged.connect(self._controls_changed)
        self.end_spin.valueChanged.connect(self._controls_changed)
        # After the generic handler has synced the profile: try to auto-find
        # the coordinate columns for a manually chosen encoding.
        self.encoding_combo.currentIndexChanged.connect(self._encoding_changed)

    def _make_crs_widget(self):
        try:
            from qgis.gui import QgsProjectionSelectionWidget
            widget = QgsProjectionSelectionWidget()
            widget.crsChanged.connect(lambda _c: self._controls_changed())
            return widget
        except Exception:
            edit = QLineEdit()
            edit.setPlaceholderText("EPSG:4326")
            edit.editingFinished.connect(self._controls_changed)
            return edit

    def _crs_authid(self) -> str:
        widget = self.crs_widget
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        try:
            return widget.crs().authid()
        except Exception:
            return ""

    def _set_crs_authid(self, authid: str):
        authid = authid or "EPSG:4326"
        if isinstance(self.crs_widget, QLineEdit):
            self.crs_widget.setText(authid)
            return
        try:
            from qgis.core import QgsCoordinateReferenceSystem
            self.crs_widget.setCrs(QgsCoordinateReferenceSystem(authid))
        except Exception:
            pass

    def _preview_section_resized(self, section: int, _old: int, size: int):
        self._sync_section_width(
            self.mapping_table.horizontalHeader(), section, size)

    def _mapping_section_resized(self, section: int, _old: int, size: int):
        self._sync_section_width(self.table.horizontalHeader(), section, size)

    def _sync_section_width(self, target: QHeaderView, section: int, size: int):
        if self._syncing_sections or section >= target.count():
            return
        self._syncing_sections = True
        try:
            target.resizeSection(section, size)
        finally:
            self._syncing_sections = False

    # -- profile <-> controls -------------------------------------------------
    def _profile_from_controls(self) -> ImportProfile:
        profile = self.wiz.profile
        profile.layout = self.layout_combo.currentData()
        profile.flat_semantics = self.flat_combo.currentData()
        profile.data_start_row = self.start_spin.value()
        profile.data_end_row = self.end_spin.value()
        profile.coord_encoding = self.encoding_combo.currentData()
        profile.source_crs = self._crs_authid()
        profile.distance_unit = self.dist_unit.currentData()
        profile.cable_distance_unit = self.cable_unit.currentData()
        profile.depth_unit = self.depth_unit.currentData()
        profile.slack_is_ratio = bool(self.slack_unit.currentData())
        mapping: Dict[str, int] = {}
        excluded_columns: List[int] = []
        for column in range(self.mapping_table.columnCount()):
            combo = self.mapping_table.cellWidget(0, column)
            field_key = combo.currentData() if combo is not None else ""
            if field_key == _INCLUDE_EXTRA:
                continue
            if field_key:
                mapping[field_key] = column + 1
            else:
                excluded_columns.append(column + 1)
        profile.mapping = mapping
        profile.excluded_columns = excluded_columns
        return profile

    def _controls_to_profile_widgets(self, profile: ImportProfile):
        self._loading = True
        try:
            self.layout_combo.setCurrentIndex(
                max(0, self.layout_combo.findData(profile.layout)))
            self.flat_combo.setCurrentIndex(
                max(0, self.flat_combo.findData(profile.flat_semantics)))
            self.start_spin.setValue(max(1, profile.data_start_row))
            self.end_spin.setValue(max(1, profile.data_end_row))
            self.header_rows_label.setText(
                self._row_list_text(profile.header_rows))
            self.encoding_combo.setCurrentIndex(
                max(0, self.encoding_combo.findData(profile.coord_encoding)))
            self.dist_unit.setCurrentIndex(
                max(0, self.dist_unit.findData(profile.distance_unit)))
            self.cable_unit.setCurrentIndex(
                max(0, self.cable_unit.findData(profile.cable_distance_unit)))
            self.depth_unit.setCurrentIndex(
                max(0, self.depth_unit.findData(profile.depth_unit)))
            self.slack_unit.setCurrentIndex(1 if profile.slack_is_ratio else 0)
            self._set_crs_authid(profile.source_crs)
            self.detect_reason_label.setText(
                self.wiz.detection_reasons.get("coordinates", ""))
            self._update_layout_controls()
            self._rebuild_mapping_table(profile)
        finally:
            self._loading = False

    @staticmethod
    def _row_list_text(rows: List[int]) -> str:
        if not rows:
            return "Not detected"
        if rows == list(range(rows[0], rows[-1] + 1)):
            return (f"{rows[0]}" if len(rows) == 1
                    else f"{rows[0]}-{rows[-1]} (combined)")
        return ", ".join(str(row) for row in rows) + " (combined)"

    def _rebuild_mapping_table(self, profile: ImportProfile):
        headers = self.wiz.header_texts
        reasons = self.wiz.detection_reasons
        n_cols = self.wiz.grid.n_cols if self.wiz.grid else 0
        self.mapping_table.clearContents()
        self.mapping_table.setColumnCount(n_cols)
        self.mapping_table.setRowCount(1)
        for column in range(1, n_cols + 1):
            header = headers[column - 1] if column - 1 < len(headers) else ""
            combo = QComboBox()
            combo.addItem("Exclude", "")
            combo.addItem("Include as extra", _INCLUDE_EXTRA)
            for field_key, label in _FIELD_LABELS:
                combo.addItem(label, field_key)
            current = next(
                (field_key for field_key, mapped_column in profile.mapping.items()
                 if mapped_column == column), None)
            if current is None:
                current = ("" if column in profile.excluded_columns
                           else _INCLUDE_EXTRA)
            combo.setCurrentIndex(max(0, combo.findData(current)))
            source = _col_letter(column) + (f" · {header}" if header else "")
            reason = reasons.get(current, reasons.get("coordinates", ""))
            combo.setToolTip(source + (f"\n{reason}" if reason else ""))
            combo.currentIndexChanged.connect(
                lambda _index, col=column: self._mapping_changed(col))
            self.mapping_table.setCellWidget(0, column - 1, combo)
        for section in range(n_cols):
            self.mapping_table.setColumnWidth(
                section, self.table.columnWidth(section))

    def _mapping_changed(self, column: int):
        if self._loading:
            return
        changed = self.mapping_table.cellWidget(0, column - 1)
        field_key = changed.currentData() if changed is not None else ""
        if field_key and field_key != _INCLUDE_EXTRA:
            self._loading = True
            try:
                for other_column in range(self.mapping_table.columnCount()):
                    if other_column == column - 1:
                        continue
                    combo = self.mapping_table.cellWidget(0, other_column)
                    if combo is not None and combo.currentData() == field_key:
                        combo.setCurrentIndex(0)
            finally:
                self._loading = False
        reason = self.wiz.detection_reasons.get(
            field_key, self.wiz.detection_reasons.get("coordinates", ""))
        if changed is not None:
            changed.setToolTip(reason)
        self._controls_changed()

    def _update_layout_controls(self):
        is_flat = self.layout_combo.currentData() == im.LAYOUT_FLAT
        self.flat_label.setVisible(is_flat)
        self.flat_combo.setVisible(is_flat)

    # -- reactions -------------------------------------------------------------
    def _controls_changed(self, *args):
        if self._loading:
            return
        self._update_layout_controls()
        profile = self._profile_from_controls()
        self._refresh_preview(profile)

    def _redetect(self):
        """Fresh full autodetect on the current sheet, discarding the current
        mapping and any auto-applied saved profile."""
        wiz = self.wiz
        try:
            if wiz.grid is None or wiz.grid.sheet != wiz.profile.sheet:
                wiz.grid = None
                wiz.load_full_grid()   # loads and runs detection
            else:
                result = idetect.detect(wiz.grid)
                wiz.profile = result.profile
                wiz.header_texts = result.header_texts
                wiz.detection_reasons = dict(result.reasons)
        except Exception as exc:
            self.profile_label.setText(f"Detection failed: {exc}")
            return
        # Block the saved profile from silently re-applying over this run.
        self._applied_signatures.add(wiz.profile.header_signature)
        self.grid_model.set_grid(wiz.grid, wiz.profile)
        self._controls_to_profile_widgets(wiz.profile)
        if wiz.profile.data_start_row:
            self.scroll_to_row(wiz.profile.data_start_row)
        self._refresh_preview(wiz.profile)
        self.profile_label.setText(
            "Detection re-run from the sheet contents "
            "(saved mapping profile not applied).")

    def _encoding_changed(self, *_args):
        """User picked a coordinate encoding: find its columns automatically.

        Runs after _controls_changed has synced the profile, and only fills
        in when the required coordinate columns for the chosen encoding are
        not already assigned — manual assignments are never overwritten.
        """
        if self._loading or self.wiz.grid is None:
            return
        profile = self.wiz.profile
        if not profile.required_missing():
            return
        encoding = self.encoding_combo.currentData()
        mapping = idetect.detect_coordinate_columns(self.wiz.grid, encoding)
        if not mapping:
            self.profile_label.setText(
                "Coordinate columns for this encoding were not found "
                "automatically — assign them in the mapping row above the "
                "table.")
            return
        all_coord_fields = {
            field for fields in im.REQUIRED_COORD_FIELDS.values()
            for field in fields
        }
        freed = {column for field, column in profile.mapping.items()
                 if field in all_coord_fields} - set(mapping.values())
        for field in all_coord_fields:
            profile.mapping.pop(field, None)
        profile.mapping.update(mapping)
        excluded = set(profile.excluded_columns) | freed
        excluded.difference_update(mapping.values())
        profile.excluded_columns = sorted(excluded)
        self._controls_to_profile_widgets(profile)
        self._refresh_preview(profile)
        self.profile_label.setText(
            "Coordinate columns auto-assigned for this encoding: "
            + ", ".join(f"{field} → {_col_letter(column)}"
                        for field, column in sorted(mapping.items())))

    def _refresh_preview(self, profile: ImportProfile):
        doc, diags = self.wiz.reparse()
        error_rows = {d.row for d in diags
                      if d.severity == im.SEVERITY_ERROR and d.row}
        self.grid_model.set_profile(profile, error_rows)
        errors, warnings, _infos = im.split_diagnostics(diags)
        dupes = profile.duplicate_assignments()
        missing = profile.required_missing()
        bits = [f"{len(doc.points)} positions, {len(doc.segments)} segments"]
        if missing:
            bits.append("missing required coordinate columns: "
                        + ", ".join(missing))
        if dupes:
            bits.append("one column is assigned to multiple fields: "
                        + ", ".join(f"column {_col_letter(c)} → {fields}"
                                    for c, fields in dupes.items()))
        if errors:
            bits.append(f"{len(errors)} error(s)")
        if warnings:
            bits.append(f"{len(warnings)} warning(s)")
        included_extras = (self.mapping_table.columnCount()
                           - len(profile.mapping)
                           - len(profile.excluded_columns))
        bits.append(f"{len(profile.excluded_columns)} column(s) excluded")
        if included_extras:
            bits.append(f"{included_extras} extra column(s) included")
        self.status_label.setText("; ".join(bits))
        self.completeChanged.emit()

    def scroll_to_row(self, row: int):
        if row and row <= self.grid_model.rowCount():
            index = self.grid_model.index(row - 1, 0)
            self.table.scrollTo(
                index, QAbstractItemView.ScrollHint.PositionAtCenter)
            self.table.selectRow(row - 1)

    # -- mapping profiles -------------------------------------------------------
    def _profiles_store(self) -> Dict[str, Dict]:
        settings = QSettings()
        raw = settings.value(PROFILE_SETTINGS_GROUP, "")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _save_profiles_store(self, profiles: Dict[str, Dict]):
        QSettings().setValue(PROFILE_SETTINGS_GROUP, json.dumps(profiles))

    def _save_profile(self):
        profile = self._profile_from_controls()
        signature = profile.header_signature
        if not signature:
            QMessageBox.information(self, "Save mapping profile",
                                    "No header signature for this sheet.")
            return
        name, ok = QInputDialog.getText(
            self, "Save mapping profile", "Profile name:",
            text=self.wiz.profile_name_hint())
        if not ok or not name.strip():
            return
        profiles = self._profiles_store()
        profiles[signature] = {"name": name.strip(),
                               "profile": profile.to_json()}
        self._save_profiles_store(profiles)
        self.profile_label.setText(f"Saved profile '{name.strip()}'.")

    def _delete_profile(self):
        signature = self.wiz.profile.header_signature
        profiles = self._profiles_store()
        if signature in profiles:
            name = profiles[signature].get("name", "")
            del profiles[signature]
            self._save_profiles_store(profiles)
            self.profile_label.setText(f"Deleted profile '{name}'.")
        else:
            self.profile_label.setText("No saved profile for this layout.")

    def apply_saved_profile_if_any(self):
        """Apply a stored mapping when the header signature matches AND the
        required coordinate columns still resolve; otherwise leave detection."""
        signature = self.wiz.profile.header_signature
        if signature in self._applied_signatures:
            return  # don't clobber user corrections on Back/Next
        entry = self._profiles_store().get(signature)
        if not entry:
            self.profile_label.setText("")
            return
        self._applied_signatures.add(signature)
        try:
            saved = ImportProfile.from_json(entry["profile"])
        except Exception:
            return
        saved.sheet = self.wiz.profile.sheet
        # Keep the freshly detected data range — the same layout may hold a
        # different number of rows in this workbook.
        saved.data_start_row = self.wiz.profile.data_start_row
        saved.data_end_row = self.wiz.profile.data_end_row
        saved.header_rows = self.wiz.profile.header_rows
        saved.header_signature = signature
        if saved.required_missing():
            self.profile_label.setText(
                f"Saved profile '{entry.get('name', '')}' no longer matches "
                "the required columns and was not applied.")
            return
        self.wiz.profile = saved
        self.profile_label.setText(
            f"Applied saved mapping profile '{entry.get('name', '')}'. "
            "Review before continuing.")

    # -- wizard hooks -----------------------------------------------------------
    def initializePage(self):
        wiz = self.wiz
        wiz.load_full_grid()
        self.apply_saved_profile_if_any()
        self.grid_model.set_grid(wiz.grid, wiz.profile)
        self._controls_to_profile_widgets(wiz.profile)
        if wiz.profile.data_start_row:
            self.scroll_to_row(wiz.profile.data_start_row)
        self._refresh_preview(wiz.profile)

    def isComplete(self):
        profile = self.wiz.profile
        return not profile.required_missing() and not profile.duplicate_assignments()

    def validatePage(self):
        self._profile_from_controls()
        return True


# ---------------------------------------------------------------------------
# Page 3 — review & import
# ---------------------------------------------------------------------------
class _ReviewPage(QWizardPage):
    def __init__(self, wizard: "RplImportWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Review and import")
        self.setCommitPage(True)

        layout = QVBoxLayout(self)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.diag_tree = QTreeWidget()
        self.diag_tree.setHeaderLabels(["Finding", "Row", "Rule"])
        self.diag_tree.itemDoubleClicked.connect(self._navigate)
        layout.addWidget(self.diag_tree, 1)

        actions = QHBoxLayout()
        self.flip_button = QPushButton("Apply sign flip to flagged rows")
        self.flip_button.clicked.connect(self._apply_sign_flips)
        self.flip_button.setEnabled(False)
        actions.addWidget(self.flip_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.ack_check = QCheckBox(
            "I have reviewed the warnings above and want to import anyway")
        self.ack_check.toggled.connect(lambda _c: self.completeChanged.emit())
        layout.addWidget(self.ack_check)

    # -- helpers ---------------------------------------------------------------
    def _navigate(self, item, _column):
        row = item.data(1, Qt.ItemDataRole.UserRole)
        if row:
            wiz = self.wiz
            wiz.back()          # to mapping page
            page = wiz.mapping_page
            page.scroll_to_row(int(row))

    def _apply_sign_flips(self):
        flagged = [d for d in self.wiz.validate_diags
                   if d.rule_id == "rpl_import.coordinate_sign_outlier"]
        if not flagged:
            return
        rows = sorted({d.row for d in flagged if d.row})
        answer = QMessageBox.question(
            self, "Flip coordinate signs",
            "Flip the flagged coordinate sign on rows %s? This is recorded "
            "in the import audit and can be undone by re-running the wizard."
            % ", ".join(str(r) for r in rows))
        if answer != QMessageBox.StandardButton.Yes:
            return
        by_row = {}
        for diag in flagged:
            axis = "lat" if "Lat" in diag.message else "lon"
            by_row.setdefault(diag.row, set()).add(axis)
        for point in self.wiz.doc.points:
            axes = by_row.get(point.source_row)
            if not axes:
                continue
            if "lat" in axes and point.lat is not None:
                point.lat = -point.lat
            if "lon" in axes and point.lon is not None:
                point.lon = -point.lon
            self.wiz.user_fixes.append({
                "fix": "sign_flip", "row": point.source_row,
                "axes": sorted(axes)})
        self.wiz.revalidate()
        self._populate()

    def _populate(self):
        wiz = self.wiz
        doc = wiz.doc
        errors, warnings, infos = im.split_diagnostics(
            wiz.parse_diags + wiz.convert_diags + wiz.validate_diags)

        stated_route = doc.stated_route_km()
        stated_cable = doc.stated_cable_km()
        computed = wiz.computed_route_km()
        bits = [
            f"<b>{len(doc.points)}</b> positions / "
            f"<b>{len(doc.segments)}</b> segments from sheet "
            f"'{doc.sheet}', rows {wiz.profile.data_start_row}-"
            f"{wiz.profile.data_end_row}.",
        ]
        if doc.start_kp_km() is not None and doc.end_kp_km() is not None:
            bits.append(f"Stated KP {doc.start_kp_km():.3f} → "
                        f"{doc.end_kp_km():.3f} km.")
        if computed is not None:
            line = f"Computed geodesic length <b>{computed:.3f} km</b>"
            if stated_route is not None:
                line += (f" vs stated route length {stated_route:.3f} km "
                         f"(Δ {abs(computed - stated_route):.3f} km)")
            bits.append(line + ".")
        if stated_cable is not None:
            bits.append(f"Stated cable length {stated_cable:.3f} km.")
        kind = wiz.source_page.kind_combo.currentData()
        mode = wiz.source_page.slack_combo.currentData() or default_slack_mode(kind)
        bits.append("Stated values are imported verbatim; QGIS-computed "
                    "values are used only for the comparisons above. Future "
                    "edits will %s." % (
                        "hold slack and recompute cable distance"
                        if mode == "hold_slack" else
                        "hold cable distance and recompute slack"))
        self.summary.setText("<br>".join(bits))

        self.diag_tree.clear()
        groups = (("Errors — import blocked", errors),
                  ("Warnings — need acknowledgement", warnings),
                  ("Information", infos))
        for title, items in groups:
            if not items:
                continue
            top = QTreeWidgetItem([f"{title} ({len(items)})", "", ""])
            self.diag_tree.addTopLevelItem(top)
            for diag in items:
                child = QTreeWidgetItem([
                    diag.message, str(diag.row or ""), diag.rule_id])
                child.setData(1, Qt.ItemDataRole.UserRole, diag.row)
                top.addChild(child)
            top.setExpanded(True)

        self.flip_button.setEnabled(any(
            d.rule_id == "rpl_import.coordinate_sign_outlier"
            for d in wiz.validate_diags))
        self.ack_check.setVisible(bool(warnings))
        if not warnings:
            self.ack_check.setChecked(True)
        route = wiz.source_page.route_combo.currentText().strip()
        rev = (wiz.source_page.rev_edit.text().strip()
               or wiz.source_page.rev_edit.placeholderText().replace(
                   "blank = ", ""))
        wiz.setButtonText(QWizard.WizardButton.CommitButton,
                          f"Import as {route} · {rev}")
        self.completeChanged.emit()
        wiz.show_map_preview()

    # -- wizard hooks -----------------------------------------------------------
    def initializePage(self):
        self.ack_check.setChecked(False)
        self.wiz.reparse(full=True)
        self._populate()

    def isComplete(self):
        errors, warnings, _ = im.split_diagnostics(
            self.wiz.parse_diags + self.wiz.convert_diags
            + self.wiz.validate_diags)
        if errors:
            return False
        return self.ack_check.isChecked() or not warnings

    def validatePage(self):
        return self.wiz.do_commit()


# ---------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------
class RplImportWizard(QWizard):
    """Import an RPL workbook straight into the Workbench."""

    imported = pyqtSignal(str)      # rpl_id

    def __init__(self, store: Optional[WorkbenchStore], iface=None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import RPL")
        self.resize(1400, 820)
        self.store = store
        self.iface = iface

        self.path: str = ""
        self.scan_results: List[idetect.DetectionResult] = []
        self.profile: ImportProfile = ImportProfile()
        self.header_texts: List[str] = []
        self.detection_reasons: Dict[str, str] = {}
        self.grid: Optional[ireader.SourceGrid] = None
        self.doc: im.ImportedRpl = im.ImportedRpl()
        self.parse_diags: List[im.Diagnostic] = []
        self.convert_diags: List[im.Diagnostic] = []
        self.validate_diags: List[im.Diagnostic] = []
        self.user_fixes: List[Dict] = []
        self.commit_result = None
        self._preview_items = []
        self._da = make_wgs84_distance_area()

        self.source_page = _SourcePage(self)
        self.mapping_page = _MappingPage(self)
        self.review_page = _ReviewPage(self)
        self.addPage(self.source_page)
        self.addPage(self.mapping_page)
        self.addPage(self.review_page)

        self.finished.connect(self._cleanup_preview)

    # -- state ------------------------------------------------------------------
    def set_source(self, path: str,
                   result: Optional[idetect.DetectionResult],
                   all_results: List[idetect.DetectionResult]):
        if path != self.path:
            self.grid = None
        self.path = path
        self.scan_results = all_results
        if result is not None:
            if result.profile.sheet != self.profile.sheet or not self.profile.mapping:
                self.profile = result.profile
                self.header_texts = result.header_texts
                self.detection_reasons = dict(result.reasons)
                self.grid = None
        self.user_fixes = []

    def profile_name_hint(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]

    def load_full_grid(self):
        if self.grid is not None and self.grid.sheet == self.profile.sheet:
            return
        self.grid = ireader.load_grid(self.path, sheet=(
            self.profile.sheet if ireader.is_excel(self.path) else None))
        result = idetect.detect(self.grid)
        self.profile = result.profile
        self.header_texts = result.header_texts
        self.detection_reasons = dict(result.reasons)

    def reparse(self, full: bool = False):
        """Parse + (projected transform) + validate under the current profile."""
        if self.grid is None:
            self.load_full_grid()
        self.doc, self.parse_diags = iparser.parse(self.grid, self.profile)
        self.convert_diags = []
        if self.profile.coord_encoding == im.COORD_PROJECTED:
            from qgis.core import QgsProject
            self.convert_diags = transform_projected(
                self.doc, self.profile, QgsProject.instance().transformContext())
        self.revalidate()
        return self.doc, (self.parse_diags + self.convert_diags
                          + self.validate_diags)

    def revalidate(self):
        dist_fn, bear_fn = geodesy_fns(self._da)
        self.validate_diags = ivalidate.validate(self.doc, dist_fn, bear_fn)

    def computed_route_km(self) -> Optional[float]:
        dist_fn, _ = geodesy_fns(self._da)
        points = [p for p in self.doc.points
                  if p.lat is not None and p.lon is not None]
        if len(points) < 2:
            return None
        total = 0.0
        for a, b in zip(points, points[1:]):
            total += dist_fn(a.lat, a.lon, b.lat, b.lon)
        return total

    # -- map preview -------------------------------------------------------------
    def show_map_preview(self):
        self._cleanup_preview()
        if self.iface is None:
            return
        try:
            from qgis.core import (QgsCoordinateReferenceSystem,
                                   QgsCoordinateTransform, QgsGeometry,
                                   QgsPointXY, QgsProject)
            from qgis.gui import QgsRubberBand, QgsVertexMarker
            from ..qgis_compat import GEOMETRY_LINE
            canvas = self.iface.mapCanvas()
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            to_canvas = QgsCoordinateTransform(
                wgs84, canvas.mapSettings().destinationCrs(),
                QgsProject.instance().transformContext())
            points = [p for p in self.doc.points
                      if p.lat is not None and p.lon is not None]
            if len(points) < 2:
                return
            band = QgsRubberBand(canvas, GEOMETRY_LINE)
            band.setColor(QColor(30, 120, 255, 200))
            band.setWidth(3)
            band.setToGeometry(QgsGeometry.fromPolylineXY(
                [to_canvas.transform(QgsPointXY(p.lon, p.lat))
                 for p in points]), None)
            self._preview_items.append(band)
            suspect_rows = {d.row for d in self.validate_diags
                            if d.severity != im.SEVERITY_INFO and d.row}
            for point in points:
                if point.source_row in suspect_rows:
                    marker = QgsVertexMarker(canvas)
                    marker.setCenter(
                        to_canvas.transform(QgsPointXY(point.lon, point.lat)))
                    marker.setColor(QColor(230, 60, 60))
                    marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
                    marker.setPenWidth(3)
                    self._preview_items.append(marker)
        except Exception:
            self._cleanup_preview()

    def _cleanup_preview(self, *args):
        if not self._preview_items:
            return
        try:
            canvas = self.iface.mapCanvas() if self.iface else None
            from ..qgis_compat import GEOMETRY_LINE
            for item in self._preview_items:
                try:
                    if hasattr(item, "reset"):
                        item.reset(GEOMETRY_LINE)
                    if canvas is not None:
                        canvas.scene().removeItem(item)
                except Exception:
                    pass
            if canvas is not None:
                canvas.refresh()
        except Exception:
            pass
        self._preview_items = []

    # -- commit -------------------------------------------------------------------
    def _build_audit(self) -> Dict:
        errors, warnings, infos = im.split_diagnostics(
            self.parse_diags + self.convert_diags + self.validate_diags)
        try:
            fingerprint = ireader.file_fingerprint(self.path)
        except Exception:
            fingerprint = {"path": self.path,
                           "filename": os.path.basename(self.path)}
        return {
            "source": fingerprint,
            "sheet": self.profile.sheet,
            "data_rows": [self.profile.data_start_row,
                          self.profile.data_end_row],
            "profile": json.loads(self.profile.to_json()),
            "parser_version": im.PARSER_VERSION,
            "measurement": measurement_config(self._da),
            "accepted_warnings": [d.to_dict() for d in warnings],
            "information": [d.to_dict() for d in infos],
            "user_fixes": list(self.user_fixes),
        }

    def do_commit(self) -> bool:
        if self.store is None:
            QMessageBox.critical(self, "Import RPL",
                                 "No workbench store is available.")
            return False
        source = self.source_page
        model, conv = to_rpl_model(
            self.doc, source_file=os.path.basename(self.path))
        if im.has_errors(conv):
            QMessageBox.critical(
                self, "Import RPL",
                "\n".join(d.message for d in conv
                          if d.severity == im.SEVERITY_ERROR))
            return False
        report = reconcile_model(model, self._da, derive_missing=True)
        audit = self._build_audit()
        audit["derivation"] = report.to_dict()
        audit["chart_no_text_rows"] = [
            d.row for d in conv
            if d.rule_id == "rpl_import.point.chart_no_text"]
        request = CommitRequest(
            route_name=source.route_combo.currentText().strip(),
            kind=source.kind_combo.currentData(),
            rev_label=source.rev_edit.text().strip(),
            slack_mode=source.slack_combo.currentData() or "",
            source_file=os.path.basename(self.path),
            audit=audit,
        )
        try:
            self.commit_result = commit_import(self.store, model, request)
        except CommitError as exc:
            QMessageBox.critical(self, "Import RPL", str(exc))
            return False
        self._cleanup_preview()
        self._load_committed_layers()
        self.imported.emit(self.commit_result.rpl_id)
        return True

    def _load_committed_layers(self):
        result = self.commit_result
        if result is None:
            return
        try:
            from qgis.core import QgsProject
            from .project_layers import ensure_layer
            from .store import set_project_gpkg_path
            project = QgsProject.instance()
            set_project_gpkg_path(result.gpkg_path, project)
            extent = None
            lines = ensure_layer(project, result.gpkg_path, result.lines_layer)
            ensure_layer(project, result.gpkg_path, result.points_layer)
            if lines is not None:
                extent = lines.extent()
            if extent is not None and self.iface is not None:
                from qgis.core import (QgsCoordinateReferenceSystem,
                                       QgsCoordinateTransform)
                canvas = self.iface.mapCanvas()
                transform = QgsCoordinateTransform(
                    QgsCoordinateReferenceSystem("EPSG:4326"),
                    canvas.mapSettings().destinationCrs(),
                    project.transformContext())
                try:
                    extent = transform.transformBoundingBox(extent)
                except Exception:
                    pass
                extent.scale(1.15)
                canvas.setExtent(extent)
                canvas.refresh()
            # Make sure QGIS prompts to save: the layers only survive a
            # restart if the project (or at least the gpkg entry) is saved.
            project.setDirty(True)
            if self.iface is not None:
                try:
                    self.iface.messageBar().pushMessage(
                        "Import RPL",
                        "RPL imported into the workbench GeoPackage. "
                        "Save the project to keep the layers in this workspace.",
                        duration=8)
                except Exception:
                    pass
        except Exception:
            pass
