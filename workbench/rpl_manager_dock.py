# -*- coding: utf-8 -*-
"""RPL Manager panel (hosted by the Cable Route Workbench dock).

Users deal with RPLs as entities: browse registered RPLs, inspect live
Positions/RPL Sections/Legs tables, drag-edit positions on the map (with automatic
recompute of distances, bearings, cable distance / slack and depth
resampling), edit slack in-table, and undo/redo/save through the paired
GeoPackage layers' edit buffers in lockstep.

Also hosts the Systems tab (CRA-style topology) and the Fit Assembly action.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt, QSettings, QTimer, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from ..kp_range_utils import make_distance_area
from ..qgis_compat import (
    DIALOG_ACCEPTED,
    WKB_LINESTRING,
    WKB_POINT,
    qt_exec,
)
from . import rpl_engine, schema
from .configurable_table import ConfigurableTable
from .depth_service import DepthService, DepthSourceConfig
from .rpl_engine import RplModel, SlackMode
from .rpl_layer_io import RplLayerSync
from .rpl_summary import invalidate_rpl_summary, rpl_summary
from .readonly import make_readonly_banner
from .store import (
    WorkbenchReadOnlyError,
    WorkbenchStore,
    default_project_gpkg_path,
    project_gpkg_path,
    set_project_gpkg_path,
)

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

# User-facing terminology: one managed route is a cable segment; within an
# RPL, sections run between labelled event positions. Point-to-point engine
# segments remain available as the advanced Legs table.
POINT_TABLE_COLUMNS = [
    ("PosNo", "pos", True), ("Event", "event", True),
    ("KP (km)", "kp", True), ("Latitude", "lat", True),
    ("Longitude", "lon", True), ("Cable (km)", "cable", False),
    ("Depth (m)", "depth", True),
]
SECTION_COLUMNS = [
    ("From event", "from_event", True), ("To event", "to_event", True),
    ("From Pos", "from", True), ("To Pos", "to", True),
    ("Start KP", "start_kp", True), ("End KP", "end_kp", True),
    ("Length (km)", "dist", True), ("Cable (km)", "cable", True),
    ("Slack (%)", "slack", True), ("Legs", "leg_count", False),
]
LEG_COLUMNS = [
    ("From Pos", "from", True), ("From event", "from_event", True),
    ("To Pos", "to", True), ("To event", "to_event", True),
    ("Start KP", "start_kp", True), ("End KP", "end_kp", True),
    ("Bearing (deg)", "bearing", True), ("Dist (km)", "dist", True),
    ("Slack (%)", "slack", True), ("Cable (km)", "cable", True),
]
_POINT_ATTR_ORDER = ["Remarks", "ChartNo", "PosNoText", "SourceFile"]
_LEG_ATTR_ORDER = [
    "CableType", "CableCode", "FiberPair", "LayDirection", "LayVessel",
    "ProtectionMethod", "DateInstalled", "TargetBurialDepth", "BurialDepth",
    "TerritorialWater", "EEZ", "SourceFile",
]
_ATTR_LABELS = {
    "CableType": "Cable type", "CableCode": "Cable code", "FiberPair": "Fibre pair",
    "LayDirection": "Lay direction", "LayVessel": "Lay vessel",
    "ProtectionMethod": "Protection method", "DateInstalled": "Date installed",
    "TargetBurialDepth": "Target burial depth", "BurialDepth": "Burial depth",
    "TerritorialWater": "Territorial water", "SourceFile": "Source file",
    "ChartNo": "Chart no.", "PosNoText": "Position text",
}


def _attribute_keys(attr_rows, preferred) -> List[str]:
    found = []
    for attrs in attr_rows:
        for key in attrs:
            if key not in found:
                found.append(key)
    ordered = [key for key in preferred if key in found]
    ordered.extend(sorted([key for key in found if key not in ordered],
                          key=lambda value: value.lower()))
    return ordered


def _attribute_columns(keys, default_visible=None):
    default_visible = set(default_visible or ())
    return [(_ATTR_LABELS.get(key, key), f"attr:{key}", key in default_visible)
            for key in keys]


def _text(value) -> str:
    return "" if value is None else str(value)


def _number(value, decimals: int) -> str:
    return "" if value is None else f"{float(value):.{decimals}f}"


def _leg_column_presets(columns):
    essentials = {key for _label, key, visible in columns if visible}
    installation = {
        "from_event", "to_event", "start_kp", "end_kp", "dist", "cable", "slack",
        "attr:CableType", "attr:CableCode", "attr:LayDirection", "attr:LayVessel",
        "attr:ProtectionMethod", "attr:DateInstalled", "attr:BurialDepth",
        "attr:TargetBurialDepth",
    }
    return {"Essentials": essentials, "Installation": installation}


class RplManagerPanel(QWidget):
    """Controller + view for registered RPLs.

    A plain widget so it can be embedded in the unified Workbench dock
    (``embedded=True`` hides its own RPL browser column — the dock's entity
    tree drives selection instead).
    """

    model_changed = pyqtSignal()  # emitted after any committed model change
    rpls_changed = pyqtSignal()   # emitted when RPLs are registered/deleted
    fits_changed = pyqtSignal()   # emitted when fits are created/refreshed
    extract_assembly_requested = pyqtSignal(str)  # rpl_id — "create assembly from this RPL"

    def __init__(self, iface, parent=None, embedded=False):
        super().__init__(parent)
        self.iface = iface
        self.embedded = embedded
        self.settings = QSettings()

        self.store: Optional[WorkbenchStore] = None
        self.current_rpl: Optional[Dict] = None
        self.model: Optional[RplModel] = None
        self.sync: Optional[RplLayerSync] = None
        self.edit_tool = None
        self._previous_map_tool = None
        self._preview_model: Optional[RplModel] = None
        self._table_timer = QTimer(self)
        self._table_timer.setSingleShot(True)
        self._table_timer.setInterval(50)
        self._table_timer.timeout.connect(self._refresh_tables_from_preview)
        self._pending_preview: Optional[RplModel] = None
        self._loading_tables = False
        self._table_dirty = {0, 1, 2}
        self._read_only = False

        self.da = make_distance_area(WGS84, QgsProject.instance().transformContext())

        self._build_ui()
        self._open_store()
        self.refresh_rpl_list()

        from .selection_bus import selection_bus

        selection_bus().kpSelected.connect(self._on_bus_kp_selected)
        self.points_table.itemSelectionChanged.connect(self._announce_point_selection)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter)

        # left: RPL browser (hidden when embedded in the Workbench dock —
        # the entity tree drives selection through the list programmatically)
        self.browser = QWidget()
        left_layout = QVBoxLayout(self.browser)
        left_layout.addWidget(QLabel("Registered RPLs"))
        self.rpl_list = QListWidget()
        self.rpl_list.currentItemChanged.connect(self._on_rpl_selected)
        left_layout.addWidget(self.rpl_list)

        row1 = QHBoxLayout()
        import_btn = QPushButton("Import RPL...")
        import_btn.setToolTip(
            "Import an RPL workbook/CSV straight into the workbench "
            "(guided detection, review, and registration)"
        )
        import_btn.clicked.connect(self._run_import_wizard)
        row1.addWidget(import_btn)
        register_btn = QPushButton("Add from layers...")
        register_btn.setToolTip(
            "Add an RPL point + line layer pair already loaded in the project "
            "to the workbench as a cable-segment revision"
        )
        register_btn.clicked.connect(self._run_register_algorithm)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_rpl_list)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete_current)
        row1.addWidget(register_btn)
        row1.addWidget(refresh_btn)
        row1.addWidget(delete_btn)
        left_layout.addLayout(row1)

        splitter.addWidget(self.browser)
        if self.embedded:
            self.browser.setVisible(False)

        # right: header + tables
        right = QWidget()
        right_layout = QVBoxLayout(right)

        self.readonly_banner = make_readonly_banner(right)
        right_layout.addWidget(self.readonly_banner)

        header = QHBoxLayout()
        header.addWidget(QLabel("Geometry edits preserve:"))
        self.slack_mode_combo = QComboBox()
        self.slack_mode_combo.addItem("Slack % (planning)", SlackMode.HOLD_SLACK.value)
        self.slack_mode_combo.addItem("Cable length (as-built)", SlackMode.HOLD_CABLE.value)
        self.slack_mode_combo.setToolTip(
            "How a geometry edit is absorbed. Point positions always define the "
            "ground (route) distance; this only decides which derived value moves:\n"
            "• Slack % (planning): hold the slack figure, recompute cable distance "
            "(cable = route x (1 + slack%)).\n"
            "• Cable length (as-built): hold the manufactured cable distance, "
            "recompute slack from the new ground distance.")
        self.slack_mode_combo.currentIndexChanged.connect(self._on_slack_mode_changed)
        header.addWidget(self.slack_mode_combo)
        self.auto_depth_check = QCheckBox("Auto depth")
        self.auto_depth_check.setToolTip("Resample water depth for moved/inserted positions on release")
        self.auto_depth_check.setChecked(True)
        header.addWidget(self.auto_depth_check)
        header.addStretch()

        self.edit_btn = QPushButton("Edit on map")
        self.edit_btn.setCheckable(True)
        self.edit_btn.toggled.connect(self._toggle_edit_tool)
        header.addWidget(self.edit_btn)
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.clicked.connect(self._undo)
        self.redo_btn = QPushButton("Redo")
        self.redo_btn.clicked.connect(self._redo)
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        self.discard_btn = QPushButton("Discard")
        self.discard_btn.clicked.connect(self._discard)
        for btn in (self.undo_btn, self.redo_btn, self.save_btn, self.discard_btn):
            header.addWidget(btn)
        right_layout.addLayout(header)

        metadata_row = QHBoxLayout()
        metadata_row.addWidget(QLabel("RPL revision:"))
        self.revision_edit = QLineEdit()
        self.revision_edit.setMaximumWidth(180)
        self.revision_edit.setPlaceholderText("e.g. Rev 3 or C02")
        self.revision_edit.setToolTip(
            "Revision label for this imported RPL. Issued revisions remain read-only.")
        metadata_row.addWidget(self.revision_edit)
        self.revision_save_btn = QPushButton("Save revision")
        self.revision_save_btn.clicked.connect(self._save_revision_label)
        metadata_row.addWidget(self.revision_save_btn)
        metadata_row.addStretch(1)
        right_layout.addLayout(metadata_row)

        row3 = QHBoxLayout()
        zoom_btn = QPushButton("Zoom to")
        zoom_btn.clicked.connect(self._zoom_to_current)
        row3.addWidget(zoom_btn)
        self.depth_btn = QPushButton("Depth sources…")
        self.depth_btn.clicked.connect(self._edit_depth_sources)
        row3.addWidget(self.depth_btn)
        self.resample_btn = QPushButton("Resample all depths")
        self.resample_btn.clicked.connect(self._resample_all_depths)
        row3.addWidget(self.resample_btn)
        create_asm_btn = QPushButton("Create assembly…")
        create_asm_btn.setToolTip("Extract an assembly from this RPL (event classification review + SLD preview)")
        create_asm_btn.clicked.connect(self._request_extract_assembly)
        row3.addWidget(create_asm_btn)
        fit_btn = QPushButton("Fit assembly…")
        fit_btn.setToolTip("Fit an assembly onto this RPL: body landing positions and section spans as map layers")
        fit_btn.clicked.connect(self._fit_assembly)
        row3.addWidget(fit_btn)
        self.status_label = QLabel("")
        row3.addWidget(self.status_label)
        row3.addStretch()
        right_layout.addLayout(row3)

        self.tabs = QTabWidget()
        self.points_table = ConfigurableTable("rpl_positions")
        self.points_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.points_table.itemChanged.connect(self._on_point_item_changed)
        self.sections_table = ConfigurableTable("rpl_sections")
        self.sections_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.segments_table = ConfigurableTable("rpl_legs")
        self.segments_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.segments_table.itemChanged.connect(self._on_segment_item_changed)
        self.tabs.addTab(self.points_table, "Positions")
        self.tabs.addTab(self.sections_table, "RPL sections")
        self.tabs.addTab(self.segments_table, "Legs (advanced)")
        self.tabs.addTab(self._build_systems_tab(), "Cable systems")
        self.tabs.setTabToolTip(
            0, "Every position in the RPL. Right-click the header to choose columns.")
        self.tabs.setTabToolTip(
            1, "Derived sections between consecutive event positions. Right-click the header to choose columns.")
        self.tabs.setTabToolTip(
            2, "Advanced point-to-point legs used for bearings, distances and slack. Right-click the header to choose columns.")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        right_layout.addWidget(self.tabs)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self._update_edit_buttons()

    def set_read_only(self, read_only: bool) -> None:
        self._read_only = bool(read_only)
        if self._read_only and self.edit_btn.isChecked():
            self.edit_btn.setChecked(False)
        self.readonly_banner.setVisible(self._read_only)
        self.edit_btn.setEnabled(not self._read_only and self.model is not None)
        self.slack_mode_combo.setEnabled(not self._read_only)
        self.auto_depth_check.setEnabled(not self._read_only)
        self.depth_btn.setEnabled(not self._read_only)
        self.resample_btn.setEnabled(not self._read_only)
        self.revision_edit.setEnabled(not self._read_only and self.current_rpl is not None)
        self.revision_save_btn.setEnabled(not self._read_only and self.current_rpl is not None)
        self._update_edit_buttons()

    def _can_edit_current(self) -> bool:
        return not self._read_only

    # ------------------------------------------------------------- store --
    def _open_store(self):
        # The saved entry is often an absolute path and may be stale after a
        # project is moved to another computer/profile. Use the same
        # project-side recovery as layer restoration before falling back to a
        # new default registry.
        expected = project_gpkg_path() or default_project_gpkg_path()
        if (self.store is not None
                and os.path.normcase(os.path.abspath(self.store.gpkg_path))
                == os.path.normcase(os.path.abspath(expected))
                and self.store.exists()):
            return

        from .project_layers import discover_gpkg_path

        path = discover_gpkg_path() or default_project_gpkg_path()
        if (self.store is None
                or os.path.normcase(os.path.abspath(self.store.gpkg_path))
                != os.path.normcase(os.path.abspath(path))):
            self.store = WorkbenchStore(path)
        if self.store.exists():
            set_project_gpkg_path(path)

    def refresh_rpl_list(self, rows=None):
        if rows is None:
            self._open_store()
        previous = self.current_rpl.get("rpl_id") if self.current_rpl else None
        self.rpl_list.blockSignals(True)
        self.rpl_list.clear()
        restore_row = -1
        if self.store and self.store.exists():
            for row in (rows if rows is not None else self.store.list_rpls()):
                item = QListWidgetItem(f"{row.get('name')}  ({row.get('kind')})")
                item.setData(Qt.ItemDataRole.UserRole, row.get("rpl_id"))
                self.rpl_list.addItem(item)
                if row.get("rpl_id") == previous:
                    restore_row = self.rpl_list.count() - 1
        if restore_row >= 0:
            # restore without reloading (would discard the edit session)
            self.rpl_list.setCurrentRow(restore_row)
        self.rpl_list.blockSignals(False)
        if restore_row < 0 and self.rpl_list.count():
            self.rpl_list.setCurrentRow(0)

    # ------------------------------------------------------ RPL selection --
    def select_rpl(self, rpl_id: str):
        """Programmatic selection (used by the Workbench entity tree)."""
        for i in range(self.rpl_list.count()):
            if self.rpl_list.item(i).data(Qt.ItemDataRole.UserRole) == rpl_id:
                if self.rpl_list.currentRow() != i:
                    self.rpl_list.setCurrentRow(i)
                return

    def _on_rpl_selected(self, current, _previous=None):
        if current is None:
            self.current_rpl = None
            self.model = None
            self.sync = None
            self.set_read_only(False)
            self.revision_edit.clear()
            self._refresh_tables()
            return
        if self.sync is not None and self.sync.is_dirty():
            answer = QMessageBox.question(
                self, "Unsaved changes",
                "The current RPL has unsaved edits. Save them?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Save:
                self.sync.commit()
            elif answer == QMessageBox.StandardButton.Discard:
                self.sync.rollback()
            else:
                return
        rpl_id = current.data(Qt.ItemDataRole.UserRole)
        self.load_rpl(rpl_id)

    def load_rpl(self, rpl_id: str):
        self.current_rpl = self.store.get_rpl(rpl_id) if self.store else None
        self.model = None
        self.sync = None
        if not self.current_rpl:
            self.set_read_only(False)
            self.revision_edit.clear()
            self._refresh_tables()
            return
        self.set_read_only(self.current_rpl.get("status") == schema.STATUS_ISSUED)
        self.revision_edit.setText(self.current_rpl.get("rev_label") or "")

        points_layer = self._find_or_load_layer(self.current_rpl.get("points_layer"))
        lines_layer = self._find_or_load_layer(self.current_rpl.get("lines_layer"))
        if points_layer is None or lines_layer is None:
            self._set_status("Could not open the RPL's layers from the workbench GeoPackage.")
            self._refresh_tables()
            return

        self.sync = RplLayerSync(points_layer, lines_layer, rpl_id)
        self.model = self.sync.load_model()

        mode = SlackMode.from_string(self.current_rpl.get("slack_mode"))
        self.slack_mode_combo.blockSignals(True)
        self.slack_mode_combo.setCurrentIndex(0 if mode is SlackMode.HOLD_SLACK else 1)
        self.slack_mode_combo.blockSignals(False)

        findings = rpl_engine.validate(self.model)
        self._set_status(
            f"{len(self.model.points)} positions, {len(rpl_engine.event_sections(self.model))} RPL sections, "
            f"{self.model.total_route_km():.3f} km cable segment, "
            f"{self.model.total_cable_km():.3f} km cable"
            + (f" — {len(findings)} validation notes" if findings else "")
        )
        self._refresh_tables()
        if self.edit_tool is not None:
            self.edit_tool.refresh_geometry()

    def _find_or_load_layer(self, layer_name: Optional[str]) -> Optional[QgsVectorLayer]:
        if not layer_name or not self.store:
            return None
        from .project_layers import ensure_layer

        return ensure_layer(QgsProject.instance(), self.store.gpkg_path, layer_name)

    # ------------------------------------------------------------- tables --
    def _refresh_tables(self, model: Optional[RplModel] = None, mark_dirty: bool = True):
        """Populate only the visible data tab; defer the other large tables."""
        model = model or self.model
        if mark_dirty:
            self._table_dirty.update((0, 1, 2))
        if model is None:
            for table in (self.points_table, self.sections_table, self.segments_table):
                table.setRowCount(0)
            self._update_edit_buttons()
            return
        index = self.tabs.currentIndex()
        if index not in (0, 1, 2):
            self._update_edit_buttons()
            return
        self._loading_tables = True
        try:
            if index == 0:
                self._populate_points_table(model)
            elif index == 1:
                self._populate_sections_table(model)
            else:
                self._populate_legs_table(model)
            self._table_dirty.discard(index)
        finally:
            self._loading_tables = False
        self._update_edit_buttons()

    def _populate_points_table(self, model: RplModel):
        point_attrs = _attribute_keys(
            (point.attrs for point in model.points), _POINT_ATTR_ORDER)
        columns = POINT_TABLE_COLUMNS + _attribute_columns(point_attrs, {"Remarks"})
        essentials = {key for _label, key, visible in columns if visible}
        self.points_table.configure_columns(columns, {"Essentials": essentials})
        self.points_table.setUpdatesEnabled(False)
        try:
            self.points_table.setRowCount(len(model.points))
            for row, point in enumerate(model.points):
                values = {
                    "pos": _text(point.pos_no), "event": point.event or "",
                    "lat": f"{point.lat:.6f}", "lon": f"{point.lon:.6f}",
                    "kp": _number(point.dist_cum_km, 3),
                    "cable": _number(point.cable_dist_cum_km, 3),
                    "depth": _number(point.depth_m, 1),
                }
                values.update({f"attr:{key}": _text(point.attrs.get(key)) for key in point_attrs})
                self._set_table_row(self.points_table, row, values, editable_key="event")
        finally:
            self.points_table.setUpdatesEnabled(True)

    def _populate_sections_table(self, model: RplModel):
        leg_attrs = _attribute_keys(
            (segment.attrs for segment in model.segments), _LEG_ATTR_ORDER)
        columns = SECTION_COLUMNS + _attribute_columns(leg_attrs, {"CableType"})
        self.sections_table.configure_columns(columns, _leg_column_presets(columns))
        sections = rpl_engine.event_sections(model)
        self.sections_table.setUpdatesEnabled(False)
        try:
            self.sections_table.setRowCount(len(sections))
            for row, section in enumerate(sections):
                values = {
                    "from_event": section.from_event or "(cable segment start)",
                    "to_event": section.to_event or "(cable segment end)",
                    "from": _text(section.from_pos), "to": _text(section.to_pos),
                    "start_kp": _number(section.start_kp_km, 3),
                    "end_kp": _number(section.end_kp_km, 3),
                    "dist": _number(section.dist_km, 4),
                    "cable": _number(section.cable_dist_km, 4),
                    "slack": _number(section.slack_pct, 3),
                    "leg_count": str(section.leg_count),
                }
                values.update({f"attr:{key}": _text(section.attrs.get(key)) for key in leg_attrs})
                self._set_table_row(
                    self.sections_table, row, values,
                    user_data=(section.start_point_index, section.end_point_index))
        finally:
            self.sections_table.setUpdatesEnabled(True)

    def _populate_legs_table(self, model: RplModel):
        leg_attrs = _attribute_keys(
            (segment.attrs for segment in model.segments), _LEG_ATTR_ORDER)
        columns = LEG_COLUMNS + _attribute_columns(leg_attrs, {"CableType"})
        self.segments_table.configure_columns(columns, _leg_column_presets(columns))
        self.segments_table.setUpdatesEnabled(False)
        try:
            self.segments_table.setRowCount(len(model.segments))
            for row, seg in enumerate(model.segments):
                p0, p1 = model.points[row], model.points[row + 1]
                values = {
                    "from": _text(p0.pos_no), "from_event": p0.event or "",
                    "to": _text(p1.pos_no), "to_event": p1.event or "",
                    "start_kp": _number(p0.dist_cum_km, 3),
                    "end_kp": _number(p1.dist_cum_km, 3),
                    "bearing": _number(seg.bearing_deg, 1),
                    "dist": _number(seg.dist_km, 4),
                    "slack": _number(seg.slack_pct, 3),
                    "cable": _number(seg.cable_dist_km, 4),
                }
                values.update({f"attr:{key}": _text(seg.attrs.get(key)) for key in leg_attrs})
                self._set_table_row(self.segments_table, row, values, editable_key="slack")
        finally:
            self.segments_table.setUpdatesEnabled(True)

    def _set_table_row(self, table, row, values, editable_key="", user_data=None):
        for column in range(table.columnCount()):
            key = table.field_key(column)
            item = QTableWidgetItem(values.get(key, ""))
            if self._read_only or key != editable_key:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if user_data is not None:
                item.setData(Qt.ItemDataRole.UserRole, user_data)
            table.setItem(row, column, item)

    def _refresh_tables_from_preview(self):
        if self._pending_preview is not None:
            self._refresh_tables(self._pending_preview)

    def _on_segment_item_changed(self, item: QTableWidgetItem):
        if (self._loading_tables or self.model is None
                or self.segments_table.field_key(item.column()) != "slack"
                or not self._can_edit_current()):
            return
        try:
            slack = float(item.text())
        except ValueError:
            self._refresh_tables()
            return
        seg_idx = item.row()
        if not (0 <= seg_idx < len(self.model.segments)):
            return
        self.model.segments[seg_idx].slack_pct = slack
        changed = rpl_engine.recompute(
            self.model, self.da, slack_mode=SlackMode.HOLD_SLACK, from_seg=seg_idx
        )
        changed.label = f"Edit slack (leg {seg_idx})"
        self._apply(changed)

    def _on_point_item_changed(self, item: QTableWidgetItem):
        if (self._loading_tables or self.model is None
                or self.points_table.field_key(item.column()) != "event"
                or not self._can_edit_current()):
            return
        idx = item.row()
        if not (0 <= idx < len(self.model.points)):
            return
        self.model.points[idx].event = item.text()
        changed = rpl_engine.ChangeSet(point_indices={idx}, label="Edit event")
        self._apply(changed)

    # -------------------------------------------------- controller for tool --
    def model_points_lonlat(self) -> List:
        if self.model is None:
            return []
        return [(p.lon, p.lat) for p in self.model.points]

    def slack_mode(self) -> SlackMode:
        return SlackMode.from_string(self.slack_mode_combo.currentData())

    def preview_move(self, idx: int, lat: float, lon: float):
        if self.model is None or not self._can_edit_current():
            return
        if self._preview_model is None:
            self._preview_model = self.model.copy()
        rpl_engine.move_point(self._preview_model, idx, lat, lon, self.da, self.slack_mode())
        self._pending_preview = self._preview_model
        if not self._table_timer.isActive():
            self._table_timer.start()

    def cancel_preview(self):
        self._preview_model = None
        self._pending_preview = None
        self._refresh_tables()

    def commit_move(self, idx: int, lat: float, lon: float):
        if self.model is None or self.sync is None or not self._can_edit_current():
            return
        self._preview_model = None
        self._pending_preview = None
        changed = rpl_engine.move_point(self.model, idx, lat, lon, self.da, self.slack_mode())
        changed = changed.merge(self._auto_depth(idx))
        self._apply(changed)

    def commit_insert(self, seg_idx: int, lat: float, lon: float):
        if self.model is None or self.sync is None or not self._can_edit_current():
            return
        changed = rpl_engine.insert_point(self.model, seg_idx, lat, lon, self.da, self.slack_mode())
        changed = changed.merge(self._auto_depth(seg_idx + 1))
        self._apply(changed)

    def commit_delete(self, idx: int):
        if self.model is None or self.sync is None or not self._can_edit_current():
            return
        try:
            changed = rpl_engine.delete_point(self.model, idx, self.da, self.slack_mode())
        except ValueError as exc:
            QMessageBox.warning(self, "Delete position", str(exc))
            return
        self._apply(changed)

    def edit_tool_escaped(self):
        self.edit_btn.setChecked(False)

    def _auto_depth(self, idx: int) -> rpl_engine.ChangeSet:
        if not self.auto_depth_check.isChecked():
            return rpl_engine.ChangeSet()
        service = self._depth_service()
        if service is None or not service.is_available():
            return rpl_engine.ChangeSet()
        return rpl_engine.apply_depths(self.model, service.sample, indices=[idx])

    def _apply(self, changed: rpl_engine.ChangeSet):
        if self.sync is None or self.model is None or not self._can_edit_current():
            return
        self.sync.apply(self.model, changed, changed.label)
        self._invalidate_current_summary()
        self._refresh_tables()
        if self.edit_tool is not None:
            self.edit_tool.refresh_geometry()
        self.model_changed.emit()

    # ------------------------------------------------------- edit session --
    def _toggle_edit_tool(self, on: bool):
        if on:
            if not self._can_edit_current():
                self._set_status("Issued RPL revisions are read-only.")
                self.edit_btn.setChecked(False)
                return
            if self.model is None:
                self._set_status("Select an RPL first.")
                self.edit_btn.setChecked(False)
                return
            from .rpl_edit_maptool import RplEditTool

            if self.edit_tool is None:
                self.edit_tool = RplEditTool(self.iface, self)
            self._previous_map_tool = self.iface.mapCanvas().mapTool()
            self.sync.begin_session()
            self.iface.mapCanvas().setMapTool(self.edit_tool)
        else:
            if self.edit_tool is not None and self.iface.mapCanvas().mapTool() is self.edit_tool:
                if self._previous_map_tool is not None:
                    self.iface.mapCanvas().setMapTool(self._previous_map_tool)
                else:
                    self.iface.mapCanvas().unsetMapTool(self.edit_tool)
        self._update_edit_buttons()

    def _undo(self):
        if self.sync is None or self._read_only:
            return
        self.sync.undo()
        self._invalidate_current_summary()
        self.model = self.sync.load_model()
        self._refresh_tables()
        if self.edit_tool is not None:
            self.edit_tool.refresh_geometry()

    def _redo(self):
        if self.sync is None or self._read_only:
            return
        self.sync.redo()
        self._invalidate_current_summary()
        self.model = self.sync.load_model()
        self._refresh_tables()
        if self.edit_tool is not None:
            self.edit_tool.refresh_geometry()

    def _save(self):
        if self.sync is None:
            return
        if not self._can_edit_current():
            self._set_status("Issued RPL revisions are read-only.")
            return
        if self.sync.commit():
            self._invalidate_current_summary()
            self._set_status("Saved.")
            try:
                from .layer_style import refresh_line_categories
                refresh_line_categories(self.sync.lines_layer)
            except Exception:
                pass
            if self.current_rpl:
                self.current_rpl["slack_mode"] = self.slack_mode().value
                try:
                    self.store.save_rpl(self.current_rpl)
                    # the route changed underneath any assessments of this RPL
                    self.store.mark_assessments_stale(self.current_rpl.get("rpl_id"))
                    refreshed = self._refresh_stored_fits()
                    if refreshed:
                        self._set_status(f"Saved. {refreshed} fit layer set(s) refreshed.")
                except WorkbenchReadOnlyError as exc:
                    self.iface.messageBar().pushWarning("RPL read-only", str(exc))
        else:
            self._set_status("Save failed — see message log.")
        self.sync.begin_session()

    def _discard(self):
        if self.sync is None or self._read_only:
            return
        self.sync.rollback()
        self._invalidate_current_summary()
        self.model = self.sync.load_model()
        self._refresh_tables()
        if self.edit_tool is not None:
            self.edit_tool.refresh_geometry()
        self._set_status("Changes discarded.")

    def _invalidate_current_summary(self):
        if self.current_rpl:
            invalidate_rpl_summary(self.current_rpl.get("rpl_id") or "")

    def _update_edit_buttons(self):
        has_sync = self.sync is not None
        for btn in (self.undo_btn, self.redo_btn, self.save_btn, self.discard_btn):
            btn.setEnabled(has_sync and not self._read_only)
        if hasattr(self, "edit_btn"):
            self.edit_btn.setEnabled(has_sync and not self._read_only)

    # ---------------------------------------------------------- utilities --
    def _set_status(self, text: str):
        self.status_label.setText(text)

    def _save_revision_label(self):
        if not self.current_rpl or not self.store or not self._can_edit_current():
            return
        label = self.revision_edit.text().strip()
        if not label:
            QMessageBox.warning(self, "RPL revision", "Enter a revision label.")
            return
        route_id = self.current_rpl.get("route_id") or ""
        siblings = self.store.revisions_of_route(route_id) if route_id else []
        duplicate = next((row for row in siblings
                          if row.get("rpl_id") != self.current_rpl.get("rpl_id")
                          and (row.get("rev_label") or "").strip().lower()
                          == label.lower()), None)
        if duplicate is not None:
            QMessageBox.warning(
                self, "RPL revision",
                f"This cable segment already has an RPL revision labelled '{label}'.")
            return

        old_label = self.current_rpl.get("rev_label") or ""
        self.current_rpl["rev_label"] = label
        route = self.store.get_route(route_id) if route_id else None
        if route and route.get("name"):
            self.current_rpl["name"] = f"{route.get('name')} {label}".strip()
        elif old_label and (self.current_rpl.get("name") or "").endswith(old_label):
            base = (self.current_rpl.get("name") or "")[:-len(old_label)].rstrip()
            self.current_rpl["name"] = f"{base} {label}".strip()
        try:
            self.store.save_rpl(self.current_rpl)
        except (WorkbenchReadOnlyError, ValueError) as exc:
            QMessageBox.warning(self, "RPL revision", str(exc))
            return
        self.revision_edit.setText(label)
        self._set_status(f"RPL revision updated to {label}.")
        self.refresh_rpl_list()
        self.rpls_changed.emit()

    def _zoom_to_current(self):
        if self.sync is None:
            return
        layer = self.sync.lines_layer
        canvas = self.iface.mapCanvas()
        extent = layer.extent()
        if extent.isEmpty():
            return
        transform = QgsCoordinateTransform(
            layer.crs(), canvas.mapSettings().destinationCrs(), QgsProject.instance()
        )
        try:
            extent = transform.transformBoundingBox(extent)
        except Exception:
            pass
        extent.scale(1.1)
        canvas.setExtent(extent)
        canvas.refresh()

    def _delete_current(self):
        if not self.current_rpl or not self.store:
            return
        name = self.current_rpl.get("name")
        answer = QMessageBox.question(
            self, "Delete RPL",
            f"Remove '{name}' from the workbench registry?\n\n"
            "The GeoPackage layers are kept on disk; only the registration, "
            "fits, and topology links are removed.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_rpl(self.current_rpl["rpl_id"])
        self.current_rpl = None
        self.model = None
        self.sync = None
        self.refresh_rpl_list()
        self._refresh_tables()
        self.rpls_changed.emit()

    def _run_import_wizard(self, route_name: str = "", system_id: str = ""):
        """Open the guided Import RPL wizard (detection -> review -> commit)."""
        try:
            from .rpl_import_wizard import RplImportWizard

            self._open_store()
            wizard = RplImportWizard(self.store, self.iface, parent=self)
            if route_name:
                wizard.source_page.route_combo.setEditText(route_name)

            def _imported(rpl_id: str):
                row = self.store.get_rpl(rpl_id) if self.store else None
                if row and system_id:
                    self.store.assign_route_to_system(row.get("route_id") or "", system_id)
                self.refresh_rpl_list()
                self.rpls_changed.emit()
                self.select_rpl(rpl_id)

            wizard.imported.connect(_imported)
            from ..qgis_compat import qt_exec

            qt_exec(wizard)
        except Exception as exc:
            QMessageBox.warning(
                self, "Import RPL", f"Could not open the import wizard:\n{exc}")

    def _run_register_algorithm(self, initial_parameters=None):
        try:
            import processing

            processing.execAlgorithmDialog(
                "subsea_cable_processing:register_rpl",
                dict(initial_parameters or {}),
            )
            # Processing constructs its own WorkbenchStore, so discard this
            # panel's registry/summary snapshots before showing its result.
            if self.store is not None:
                self.store.clear_cache()
            invalidate_rpl_summary()
            self.refresh_rpl_list()
            self.rpls_changed.emit()
        except Exception as exc:
            QMessageBox.warning(self, "Add RPL from layers", f"Could not open the algorithm dialog:\n{exc}")

    def _on_slack_mode_changed(self):
        # mode changes only affect what future edits preserve; persist on save
        if self.current_rpl:
            self._set_status(
                "Slack mode changed — applies to subsequent geometry edits (persisted on Save)."
            )

    def _resample_all_depths(self):
        if self.model is None or not self._can_edit_current():
            return
        service = self._depth_service()
        if service is None or not service.is_available():
            QMessageBox.information(
                self, "Depth sources",
                "No depth sources configured for this RPL. Use 'Depth sources…' first.")
            return
        changed = rpl_engine.apply_depths(self.model, service.sample)
        changed.label = "Resample all depths"
        if changed.point_indices:
            self._apply(changed)
            self._set_status(f"Resampled depth at {len(changed.point_indices)} positions.")
        else:
            self._set_status("Depths unchanged.")

    def _depth_service(self) -> Optional[DepthService]:
        if not self.current_rpl or not self.store:
            return None
        config = DepthSourceConfig(self.store.rpl_depth_config(self.current_rpl["rpl_id"]))
        service = DepthService(config)
        return service

    # ---------------------------------------------------------- systems tab --
    def _build_systems_tab(self) -> QWidget:
        from qgis.PyQt.QtWidgets import QTreeWidget

        container = QWidget()
        layout = QVBoxLayout(container)
        toolbar = QHBoxLayout()
        node_btn = QPushButton("New node…")
        node_btn.setToolTip("Add a BMH, branching unit, joint or other cable-system node")
        node_btn.clicked.connect(self._new_node)
        connect_btn = QPushButton("Connect endpoints…")
        connect_btn.clicked.connect(self._connect_ports)
        disconnect_btn = QPushButton("Disconnect")
        disconnect_btn.clicked.connect(self._disconnect_selected)
        rename_btn = QPushButton("Rename system…")
        rename_btn.clicked.connect(self._rename_system)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_systems_tree)
        for btn in (node_btn, connect_btn, disconnect_btn, rename_btn, refresh_btn):
            toolbar.addWidget(btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.systems_tree = QTreeWidget()
        self.systems_tree.setHeaderLabels(
            ["Cable system / component / endpoint", "Connected to / detail"])
        self.systems_tree.setColumnWidth(0, 280)
        self.systems_tree.itemDoubleClicked.connect(self._on_system_item_activated)
        layout.addWidget(self.systems_tree)
        self.systems_status = QLabel("")
        layout.addWidget(self.systems_status)
        return container

    def _on_tab_changed(self, index: int):
        if self.tabs.tabText(index) == "Cable systems":
            self._refresh_systems_tree()
        elif index in self._table_dirty:
            self._refresh_tables(self._pending_preview or self.model, mark_dirty=False)

    def _refresh_systems_tree(self):
        from qgis.PyQt.QtWidgets import QTreeWidgetItem
        from .system_topology import TopologyGraph, assign_system_ids

        self.systems_tree.clear()
        if self.store is None or not self.store.exists():
            self.systems_status.setText("No workbench GeoPackage yet.")
            return
        assign_system_ids(self.store)
        graph = TopologyGraph.from_store(self.store)
        systems_by_id = {s.get("system_id"): s for s in self.store.list_systems()}
        components = {c["component_id"]: c for c in self.store.list_components()}

        grouped = {}
        for component_id, component in components.items():
            grouped.setdefault(component.get("system_id") or "", []).append(component_id)

        for system_id, members in sorted(
                grouped.items(), key=lambda pair: (
                    not bool(pair[0]),
                    (systems_by_id.get(pair[0], {}).get("name") or "").lower())):
            members = sorted(members)
            system_row = systems_by_id.get(system_id, {})
            top = QTreeWidgetItem([
                system_row.get("name") or "Unassigned components",
                f"{len(members)} component(s)",
            ])
            top.setData(0, Qt.ItemDataRole.UserRole, ("system", system_id))
            for cid in members:
                component = components.get(cid, {})
                kind = component.get("kind") or ""
                detail = component.get("node_type") or kind
                child = QTreeWidgetItem([component.get("name") or cid, detail])
                child.setData(0, Qt.ItemDataRole.UserRole, ("component", cid))
                for port in graph.ports_of(cid):
                    conn = graph.connection_of_port(port["port_id"])
                    endpoint_text = self._port_display(component, port)
                    if conn is None:
                        port_text = "open"
                    else:
                        peer_port = graph.peer_port(port["port_id"])
                        peer_cid = peer_port.get("component_id") if peer_port else None
                        peer = components.get(peer_cid, {})
                        peer_endpoint = self._port_display(peer, peer_port or {})
                        port_text = f"→ {peer.get('name') or peer_cid} · {peer_endpoint}"
                    port_item = QTreeWidgetItem([endpoint_text, port_text])
                    port_item.setData(0, Qt.ItemDataRole.UserRole,
                                      ("port", port["port_id"],
                                       conn.get("connection_id") if conn else None))
                    if conn is None:
                        port_item.setForeground(1, QColor(200, 120, 0))
                    child.addChild(port_item)
                top.addChild(child)
            top.setExpanded(True)
            self.systems_tree.addTopLevelItem(top)

        findings = graph.validate()
        open_count = len(graph.open_ports())
        assigned_count = sum(1 for system_id in grouped if system_id)
        unassigned_count = len(grouped.get("", []))
        text = f"{assigned_count} cable system(s), {open_count} open endpoint(s)"
        if unassigned_count:
            text += f", {unassigned_count} unassigned component(s)"
        text += "."
        if findings:
            text += f"  ⚠ {len(findings)} topology issue(s): " + \
                    "; ".join(f["message"] for f in findings[:3])
        self.systems_status.setText(text)

    def select_topology_system(self, system_id: str) -> None:
        """Refresh and select the requested cable-system group."""
        self._refresh_systems_tree()
        for index in range(self.systems_tree.topLevelItemCount()):
            item = self.systems_tree.topLevelItem(index)
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "system" and (data[1] or "") == (system_id or ""):
                self.systems_tree.setCurrentItem(item)
                return

    def _port_display(self, component: Dict, port: Dict) -> str:
        """Human endpoint label; A/B remains a compact secondary convention."""
        label = str(port.get("label") or "?")
        if component.get("kind") not in ("route", "rpl"):
            return label.replace("_", " ").title()
        if component.get("kind") == "route":
            rpl = self.store.latest_revision(component.get("subject_id") or "")
        else:
            rpl = self.store.get_rpl(component.get("subject_id") or "")
        role = "Start" if label.strip().upper() == "A" else "End"
        summary = rpl_summary(self.store, rpl)
        is_start = label.strip().upper() == "A"
        kp = summary.start_kp_km if is_start else summary.end_kp_km
        pos = summary.start_pos if is_start else summary.end_pos
        event = summary.start_event if is_start else summary.end_event
        if not rpl:
            return f"{role} ({label})"
        bits = [f"{role} ({label})"]
        if kp is not None:
            bits.append(f"KP {kp:.3f}")
        if pos not in (None, ""):
            bits.append(f"Pos {pos}")
        if event:
            bits.append(f"“{event}”")
        return " · ".join(bits)

    def _rpl_endpoint(self, rpl: Optional[Dict], port_label: str):
        if not rpl or self.store is None:
            return None
        points = self.store.open_layer(rpl.get("points_layer") or "")
        lines = self.store.open_layer(rpl.get("lines_layer") or "")
        if points is None or lines is None:
            return None
        model = RplLayerSync(points, lines, rpl.get("rpl_id") or "").load_model()
        if not model.points:
            return None
        return model.points[0] if port_label.strip().upper() == "A" else model.points[-1]

    def _new_node(self, _checked=False, *, system_id: str = ""):
        from qgis.PyQt.QtWidgets import QInputDialog

        if self.store is None:
            return
        self.store.ensure_created()
        if not system_id:
            item = self.systems_tree.currentItem()
            while item is not None and item.parent() is not None:
                item = item.parent()
            data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
            if data and data[0] == "system":
                system_id = data[1] or ""
        name, ok = QInputDialog.getText(self, "New node", "Node name (e.g. BU-1, BMH East):")
        if not ok or not name.strip():
            return
        node_types = ["bu", "joint", "bmh", "other"]
        node_type, ok = QInputDialog.getItem(self, "New node", "Node type:", node_types, 0, False)
        if not ok:
            return
        port_labels = {"bu": ["Trunk", "Branch 1", "Branch 2"],
                       "joint": ["Side 1", "Side 2"],
                       "bmh": ["Cable"],
                       "other": ["Side 1", "Side 2"]}[node_type]
        self.store.save_component(
            {"component_id": schema.new_id(), "kind": "node", "name": name.strip(),
             "node_type": node_type, "system_id": system_id or ""},
            port_labels=port_labels,
        )
        self._refresh_systems_tree()
        self.rpls_changed.emit()

    def _connect_ports(self):
        from qgis.PyQt.QtWidgets import QInputDialog
        from .system_topology import TopologyGraph

        if self.store is None or not self.store.exists():
            return
        graph = TopologyGraph.from_store(self.store)
        components = {c["component_id"]: c for c in self.store.list_components()}
        open_ports = graph.open_ports()
        if len(open_ports) < 2:
            QMessageBox.information(
                self, "Connect endpoints", "Fewer than two open endpoints are available.")
            return

        def label(port):
            component = components.get(port.get("component_id"), {})
            return f"{component.get('name') or '?'} · {self._port_display(component, port)}"

        raw_labels = [label(p) for p in open_ports]
        labels = [
            value if raw_labels.count(value) == 1
            else f"{value} [{str(port.get('port_id') or '')[:8]}]"
            for value, port in zip(raw_labels, open_ports)
        ]
        first, ok = QInputDialog.getItem(
            self, "Connect endpoints", "First endpoint:", labels, 0, False)
        if not ok:
            return
        first_idx = labels.index(first)
        first_port = open_ports[first_idx]
        remaining_ports = [p for i, p in enumerate(open_ports) if i != first_idx]
        remaining_raw = [label(p) for p in remaining_ports]
        remaining_labels = [
            value if remaining_raw.count(value) == 1
            else f"{value} [{str(port.get('port_id') or '')[:8]}]"
            for value, port in zip(remaining_raw, remaining_ports)
        ]
        second, ok = QInputDialog.getItem(
            self, "Connect endpoints", "Second endpoint:", remaining_labels, 0, False)
        if not ok:
            return
        second_port = remaining_ports[remaining_labels.index(second)]
        try:
            self.store.connect_ports(first_port["port_id"], second_port["port_id"])
        except ValueError as exc:
            QMessageBox.warning(self, "Connect endpoints", str(exc))
            return
        self._refresh_systems_tree()
        self.rpls_changed.emit()

    def _disconnect_selected(self):
        item = self.systems_tree.currentItem()
        if item is None or self.store is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "port" or len(data) < 3 or not data[2]:
            self._set_status("Select a connected endpoint to disconnect.")
            return
        self.store.disconnect(data[2])
        self._refresh_systems_tree()
        self.rpls_changed.emit()

    def _rename_system(self):
        from qgis.PyQt.QtWidgets import QInputDialog

        item = self.systems_tree.currentItem()
        if item is None or self.store is None:
            return
        while item.parent() is not None:
            item = item.parent()
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "system" or not data[1]:
            return
        name, ok = QInputDialog.getText(self, "Rename system", "System name:", text=item.text(0))
        if not ok or not name.strip():
            return
        rows = self.store.list_systems()
        for row in rows:
            if row.get("system_id") == data[1]:
                row["name"] = name.strip()
        self.store.write_table(schema.TABLE_SYSTEM, rows)
        self._refresh_systems_tree()

    def _on_system_item_activated(self, item, _column):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or self.store is None:
            return
        if data[0] == "port":
            port = next((p for p in self.store.list_ports()
                         if p.get("port_id") == data[1]), None)
            component = next(
                (c for c in self.store.list_components()
                 if port and c.get("component_id") == port.get("component_id")), None)
            if component and component.get("kind") in ("route", "rpl"):
                rpl = (self.store.latest_revision(component.get("subject_id") or "")
                       if component.get("kind") == "route"
                       else self.store.get_rpl(component.get("subject_id") or ""))
                endpoint = self._rpl_endpoint(rpl, port.get("label") or "")
                if endpoint is not None:
                    self._flash_map_position(endpoint.lat, endpoint.lon)
            return
        if data[0] != "component":
            return
        component = next(
            (c for c in self.store.list_components() if c.get("component_id") == data[1]), None)
        if component is None:
            return
        rpl_id = ""
        if component.get("kind") == "route":
            latest = self.store.latest_revision(component.get("subject_id") or "")
            rpl_id = latest.get("rpl_id") if latest else ""
        elif component.get("kind") == "rpl":
            rpl_id = component.get("subject_id") or ""
        if rpl_id:
            for i in range(self.rpl_list.count()):
                if self.rpl_list.item(i).data(Qt.ItemDataRole.UserRole) == rpl_id:
                    self.rpl_list.setCurrentRow(i)
                    self.tabs.setCurrentIndex(0)
                    return
        if component.get("kind") == "node" and component.get("lat") is not None \
                and component.get("lon") is not None:
            self._flash_map_position(float(component["lat"]), float(component["lon"]))

    # ---------------------------------------------------------- assembly fit --
    def _request_extract_assembly(self):
        """Create an assembly from the current RPL (handled by the Workbench)."""
        if self.current_rpl is None:
            self._set_status("Select an RPL first.")
            return
        self.extract_assembly_requested.emit(self.current_rpl["rpl_id"])

    def _fit_assembly(self):
        if self.model is None or self.current_rpl is None or self.store is None:
            self._set_status("Select an RPL first.")
            return
        assemblies = self.store.list_assemblies() if self.store.exists() else []
        if not assemblies:
            QMessageBox.information(
                self, "Fit assembly",
                "No assemblies in the workbench yet. Create or import one in the Assembly Manager.")
            return

        dialog = FitAssemblyDialog(assemblies, self.model, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        assembly_row = dialog.selected_assembly()
        anchor = dialog.anchor()
        header, item_rows = self.store.get_assembly(assembly_row["assembly_id"])
        if header is None:
            return

        result = self._run_fit(header, item_rows, anchor, warn=True)
        self.store.save_fit({
            "fit_id": schema.new_id(),
            "assembly_id": header["assembly_id"],
            "rpl_id": self.current_rpl["rpl_id"],
            "anchor_kp_km": anchor.kp_km,
            "anchor_cable_dist_m": anchor.cable_dist_m,
            "direction": anchor.direction,
            "params_json": "{}",
        })
        landed = sum(1 for b in result.bodies if b.on_route)
        self._set_status(
            f"Fitted '{header.get('name')}': {landed}/{len(result.bodies)} bodies landed on route.")
        self.fits_changed.emit()

    def fit_assembly_to_current(self, assembly_id: str, anchor=None) -> Optional[str]:
        """Fit an assembly onto the current RPL without the dialog.

        Defaults the anchor to the route start (cable distance 0, running with
        increasing KP) — the natural fit for an assembly just extracted from
        this RPL. Returns the new fit_id, or None on failure.
        """
        if self.model is None or self.current_rpl is None or self.store is None:
            return None
        header, item_rows = self.store.get_assembly(assembly_id)
        if header is None:
            return None
        from .fit import FitAnchor

        if anchor is None:
            anchor = FitAnchor(kp_km=self.model.start_kp_km(), cable_dist_m=0.0, direction=1)
        result = self._run_fit(header, item_rows, anchor, warn=True)
        fit_id = schema.new_id()
        self.store.save_fit({
            "fit_id": fit_id,
            "assembly_id": assembly_id,
            "rpl_id": self.current_rpl["rpl_id"],
            "anchor_kp_km": anchor.kp_km,
            "anchor_cable_dist_m": anchor.cable_dist_m,
            "direction": anchor.direction,
            "params_json": "{}",
        })
        landed = sum(1 for b in result.bodies if b.on_route)
        self._set_status(
            f"Fitted '{header.get('name')}': {landed}/{len(result.bodies)} bodies landed on route.")
        self.fits_changed.emit()
        return fit_id

    def _run_fit(self, header: Dict, item_rows: List[Dict], anchor, warn: bool = False):
        """Fit an assembly onto the current model and (re)write its layers."""
        from . import assembly_model as am
        from .fit import fit_assembly

        assembly = am.assembly_from_rows(header, item_rows)
        depth_service = self._depth_service()
        depth_fn = depth_service.sample if depth_service and depth_service.is_available() else None
        result = fit_assembly(assembly, self.model, anchor, da=self.da, depth_fn=depth_fn)
        if warn:
            for warning in result.warnings:
                self.iface.messageBar().pushWarning("Fit assembly", warning)
        fit_name = f"{header.get('name')}_{self.current_rpl.get('name')}"
        self._write_fit_layers(fit_name, result)
        return result

    def _refresh_stored_fits(self) -> int:
        """Re-run every stored fit for the current RPL against the saved model.

        Keeps body-landing and section-span layers consistent after route
        edits, depth resampling, or slack changes. Returns the count refreshed.
        """
        if self.model is None or self.current_rpl is None or self.store is None:
            return 0
        from .fit import FitAnchor

        refreshed = 0
        for fit_row in self.store.list_fits(rpl_id=self.current_rpl["rpl_id"]):
            header, item_rows = self.store.get_assembly(fit_row.get("assembly_id") or "")
            if header is None:
                continue
            anchor = FitAnchor(
                kp_km=float(fit_row.get("anchor_kp_km") or 0.0),
                cable_dist_m=float(fit_row.get("anchor_cable_dist_m") or 0.0),
                direction=1 if int(fit_row.get("direction") or 1) >= 0 else -1,
            )
            try:
                self._run_fit(header, item_rows, anchor)
                refreshed += 1
            except Exception as exc:
                self.iface.messageBar().pushWarning(
                    "Refit", f"Could not refresh fit for '{header.get('name')}': {exc}")
        if refreshed:
            self.fits_changed.emit()
        return refreshed

    def _write_fit_layers(self, fit_name: str, result):
        from ..processing.cable_lay_parsers import WKT_KEY

        body_specs = [
            ("name", "str"), ("body_type", "str"), ("cable_dist_m", "float"),
            ("kp_km", "float"), ("depth_m", "float"), ("on_route", "str"),
        ]
        body_rows = []
        for body in result.bodies:
            if body.lat is None or body.lon is None:
                continue
            body_rows.append({
                "name": body.item.name,
                "body_type": body.item.remarks or "",
                "cable_dist_m": body.cable_dist_m,
                "kp_km": body.kp_km,
                "depth_m": body.depth_m,
                "on_route": "yes" if body.on_route else "no",
                WKT_KEY: f"POINT ({body.lon} {body.lat})",
            })

        section_specs = [
            ("name", "str"), ("cable_type", "str"), ("cable_start_m", "float"),
            ("cable_end_m", "float"), ("kp_start_km", "float"), ("kp_end_km", "float"),
            ("color_hex", "str"), ("clipped", "str"),
        ]
        section_rows = []
        for span in result.sections:
            if span.kp_start_km is None or span.kp_end_km is None:
                continue
            kp_lo, kp_hi = sorted((span.kp_start_km, span.kp_end_km))
            coords = self._section_coords(kp_lo, kp_hi)
            if len(coords) < 2:
                continue
            wkt = "LINESTRING (" + ", ".join(f"{lon} {lat}" for lat, lon in coords) + ")"
            section_rows.append({
                "name": span.item.name,
                "cable_type": span.item.cable_type,
                "cable_start_m": span.cable_start_m,
                "cable_end_m": span.cable_end_m,
                "kp_start_km": span.kp_start_km,
                "kp_end_km": span.kp_end_km,
                "color_hex": span.item.color_hex,
                "clipped": "yes" if span.clipped else "no",
                WKT_KEY: wkt,
            })

        bodies_layer = schema.fit_bodies_layer_name(fit_name)
        sections_layer = schema.fit_sections_layer_name(fit_name)
        if body_rows:
            self.store.write_spatial_layer(
                bodies_layer, body_specs, WKB_POINT, body_rows)
            self._reload_fit_layer(bodies_layer)
        if section_rows:
            self.store.write_spatial_layer(
                sections_layer, section_specs, WKB_LINESTRING, section_rows)
            self._reload_fit_layer(sections_layer)

    def _reload_fit_layer(self, layer_name: str):
        """Load a fit layer, or refresh it if it is already in the project."""
        layer = self._find_or_load_layer(layer_name)
        if layer is not None:
            layer.dataProvider().reloadData()
            layer.triggerRepaint()

    def _section_coords(self, kp_lo: float, kp_hi: float) -> List:
        """(lat, lon) chain along the route between two KPs, following RPL points."""
        coords = []
        start = rpl_engine.point_at_kp(self.model, kp_lo, self.da)
        if start:
            coords.append(start)
        for point in self.model.points:
            if point.dist_cum_km is not None and kp_lo < point.dist_cum_km < kp_hi:
                coords.append((point.lat, point.lon))
        end = rpl_engine.point_at_kp(self.model, kp_hi, self.da)
        if end:
            coords.append(end)
        return coords

    # ------------------------------------------------------- selection bus --
    def _on_bus_kp_selected(self, rpl_id: str, kp_km: float):
        """A body/section was clicked in the SLD — flash its landing on the map."""
        if self.current_rpl is None or self.current_rpl.get("rpl_id") != rpl_id:
            if self.store:
                row = self.store.get_rpl(rpl_id)
                if row is None:
                    return
                self.load_rpl(rpl_id)
        if self.model is None:
            return
        pos = rpl_engine.point_at_kp(self.model, kp_km, self.da)
        if pos is None:
            return
        lat, lon = pos
        self._flash_map_position(lat, lon)

    def _flash_map_position(self, lat: float, lon: float):
        from qgis.PyQt.QtCore import QTimer
        from qgis.gui import QgsVertexMarker

        canvas = self.iface.mapCanvas()
        transform = QgsCoordinateTransform(
            WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        try:
            pt = transform.transform(lon, lat)
        except Exception:
            return
        marker = QgsVertexMarker(canvas)
        marker.setIconSize(18)
        marker.setPenWidth(3)
        marker.setColor(QColor(0, 170, 255))
        marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        marker.setCenter(pt)
        if not canvas.extent().contains(pt):
            canvas.setCenter(pt)
            canvas.refresh()
        QTimer.singleShot(1500, lambda: canvas.scene().removeItem(marker))

    def _announce_point_selection(self):
        """Selected RPL position -> cable distance marker in the SLD."""
        if self.model is None or self.current_rpl is None or self.store is None:
            return
        row = self.points_table.currentRow()
        if not (0 <= row < len(self.model.points)):
            return
        kp = self.model.points[row].dist_cum_km
        if kp is None:
            return
        fits = self.store.list_fits(rpl_id=self.current_rpl["rpl_id"])
        if not fits:
            return
        fit_row = fits[0]
        anchor_cable_km = rpl_engine.cable_dist_from_kp(self.model, float(fit_row.get("anchor_kp_km") or 0.0))
        cable_km = rpl_engine.cable_dist_from_kp(self.model, kp)
        if anchor_cable_km is None or cable_km is None:
            return
        direction = 1 if int(fit_row.get("direction") or 1) >= 0 else -1
        cable_m = float(fit_row.get("anchor_cable_dist_m") or 0.0) + direction * (cable_km - anchor_cable_km) * 1000.0
        from .selection_bus import selection_bus

        selection_bus().cableDistSelected.emit(fit_row.get("assembly_id") or "", cable_m)

    def _edit_depth_sources(self):
        if not self.current_rpl or not self.store:
            return
        if not self._can_edit_current():
            self._set_status("Issued RPL revisions are read-only.")
            return
        config = DepthSourceConfig(self.store.rpl_depth_config(self.current_rpl["rpl_id"]))
        dialog = DepthSourcesDialog(config, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            self.current_rpl["depth_source_config"] = json.dumps(dialog.result_config().to_dict())
            try:
                self.store.save_rpl(self.current_rpl)
            except WorkbenchReadOnlyError as exc:
                self.iface.messageBar().pushWarning("RPL read-only", str(exc))
                return
            self.auto_depth_check.setChecked(dialog.result_config().auto_resample)
            self._set_status("Depth sources updated.")

    def closeEvent(self, event):
        if self.edit_btn.isChecked():
            self.edit_btn.setChecked(False)
        if self.sync is not None and self.sync.is_dirty():
            answer = QMessageBox.question(
                self, "Unsaved changes", "Save RPL edits before closing?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard,
            )
            if answer == QMessageBox.StandardButton.Save:
                self.sync.commit()
            else:
                self.sync.rollback()
        super().closeEvent(event)


class FitAssemblyDialog(QDialog):
    """Choose an assembly, anchor KP, anchor cable distance, and direction."""

    def __init__(self, assemblies: List[Dict], model: RplModel, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fit assembly onto RPL")
        self._assemblies = assemblies
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.assembly_combo = QComboBox()
        for row in assemblies:
            total_km = (row.get("total_cable_len_m") or 0.0) / 1000.0
            self.assembly_combo.addItem(
                f"{row.get('name')} ({total_km:.2f} km)", row.get("assembly_id"))
        form.addRow("Assembly", self.assembly_combo)

        self.kp_spin = QDoubleSpinBox()
        self.kp_spin.setDecimals(3)
        self.kp_spin.setRange(model.start_kp_km(), model.end_kp_km())
        self.kp_spin.setValue(model.start_kp_km())
        self.kp_spin.setSuffix(" km")
        form.addRow("Anchor KP", self.kp_spin)

        self.cable_spin = QDoubleSpinBox()
        self.cable_spin.setDecimals(1)
        self.cable_spin.setRange(0.0, 1e9)
        self.cable_spin.setSuffix(" m")
        self.cable_spin.setToolTip("Cable distance within the assembly that sits at the anchor KP (usually 0)")
        form.addRow("Anchor cable distance", self.cable_spin)

        self.direction_combo = QComboBox()
        self.direction_combo.addItem("With increasing KP", 1)
        self.direction_combo.addItem("Against KP (reverse)", -1)
        form.addRow("Direction", self.direction_combo)

        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_assembly(self) -> Dict:
        assembly_id = self.assembly_combo.currentData()
        return next(r for r in self._assemblies if r.get("assembly_id") == assembly_id)

    def anchor(self):
        from .fit import FitAnchor

        return FitAnchor(
            kp_km=self.kp_spin.value(),
            cable_dist_m=self.cable_spin.value(),
            direction=int(self.direction_combo.currentData()),
        )


class DepthSourcesDialog(QDialog):
    """Configure per-RPL depth sampling (rasters + contours)."""

    def __init__(self, config: DepthSourceConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Depth sources")
        self._config = config
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Auto (raster first, contour fallback)", "Raster only", "Contours only"])
        self.mode_combo.setCurrentIndex(config.mode)
        form.addRow("Mode", self.mode_combo)

        self.raster_list = QListWidget()
        project = QgsProject.instance()
        for layer in project.mapLayers().values():
            if isinstance(layer, QgsRasterLayer):
                item = QListWidgetItem(layer.name())
                item.setData(Qt.ItemDataRole.UserRole, layer.id())
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if layer.id() in config.raster_layer_ids
                    else Qt.CheckState.Unchecked
                )
                self.raster_list.addItem(item)
        self.raster_list.setMaximumHeight(120)
        form.addRow("Raster layers", self.raster_list)

        self.contour_combos = []
        self.contour_field_combos = []
        existing = {e.get("layer_id"): e.get("depth_field", "") for e in config.contour_layers}
        vector_layers = [
            layer for layer in project.mapLayers().values() if isinstance(layer, QgsVectorLayer)
        ]
        for slot in range(2):
            combo = QComboBox()
            field_combo = QComboBox()
            combo.addItem("(none)", "")
            for layer in vector_layers:
                combo.addItem(layer.name(), layer.id())

            def fill_fields(index, c=combo, fc=field_combo):
                fc.clear()
                layer = QgsProject.instance().mapLayer(c.currentData())
                if isinstance(layer, QgsVectorLayer):
                    for f in layer.fields():
                        fc.addItem(f.name())

            combo.currentIndexChanged.connect(fill_fields)
            slot_ids = list(existing.keys())
            if slot < len(slot_ids):
                idx = combo.findData(slot_ids[slot])
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                    fill_fields(idx)
                    fidx = field_combo.findText(existing[slot_ids[slot]])
                    if fidx >= 0:
                        field_combo.setCurrentIndex(fidx)
            self.contour_combos.append(combo)
            self.contour_field_combos.append(field_combo)
            form.addRow(f"Contour layer {slot + 1}", combo)
            form.addRow(f"Depth field {slot + 1}", field_combo)

        self.radius_spin = QDoubleSpinBox()
        self.radius_spin.setRange(0.0, 1e6)
        self.radius_spin.setSuffix(" m")
        self.radius_spin.setValue(config.contour_search_radius_m)
        self.radius_spin.setToolTip("0 = unlimited")
        form.addRow("Contour search radius", self.radius_spin)

        self.auto_check = QCheckBox("Resample automatically when positions move")
        self.auto_check.setChecked(config.auto_resample)
        form.addRow(self.auto_check)

        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_config(self) -> DepthSourceConfig:
        config = DepthSourceConfig()
        config.mode = self.mode_combo.currentIndex()
        config.raster_layer_ids = [
            self.raster_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.raster_list.count())
            if self.raster_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        contour_layers = []
        for combo, field_combo in zip(self.contour_combos, self.contour_field_combos):
            layer_id = combo.currentData()
            if layer_id:
                contour_layers.append({"layer_id": layer_id, "depth_field": field_combo.currentText()})
        config.contour_layers = contour_layers
        config.contour_search_radius_m = self.radius_spin.value()
        config.auto_resample = self.auto_check.isChecked()
        return config
