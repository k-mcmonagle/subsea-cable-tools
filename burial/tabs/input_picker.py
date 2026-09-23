# -*- coding: utf-8 -*-
"""Add inputs — pick several project layers at once and register them.

A two-pane picker for projects with many layers: the left pane lists the
project's vector layers with a search box, a geometry filter (points /
lines / polygons / tables), the Layers-panel group each layer sits in and
whether it is already registered; the right pane stages the chosen layers
with their role (guessed from geometry and name, editable) and the optional
Input Data Register details, which can be set per row or for all selected
rows at once. OK registers everything in one change-log entry.

Pure helpers (``guess_role``, ``role_problem``, ``matches_filter``) are
separate from the widgets so they can be tested without a dialog.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)

from ...qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    EDIT_TRIGGER_DOUBLE_CLICKED,
    EDIT_TRIGGER_EDIT_KEY_PRESSED,
    EDIT_TRIGGER_SELECTED_CLICKED,
    HEADER_RESIZE_MODE_CONTENTS,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
)
from .. import map_layers, schema, ui_helpers

GEOM_POINT = "point"
GEOM_LINE = "line"
GEOM_POLYGON = "polygon"
GEOM_TABLE = "table"
GEOM_OTHER = "other"

GEOM_LABELS = {
    GEOM_POINT: "Point",
    GEOM_LINE: "Line",
    GEOM_POLYGON: "Polygon",
    GEOM_TABLE: "Table",
    GEOM_OTHER: "Other",
}

# The geometry each role needs (roles missing here accept any vector).
ROLE_GEOMETRY = {
    schema.INPUT_ROLE_CROSSINGS_POINTS: GEOM_POINT,
    schema.INPUT_ROLE_CROSSINGS_LINES: GEOM_LINE,
    schema.INPUT_ROLE_SOILS: GEOM_POLYGON,
}

_SOIL_WORDS = ("soil", "sediment", "geolog", "seabed", "substrate", "sbp",
               "grab", "cpt", "facies", "rock", "boulder field")
_CROSSING_WORDS = ("cable", "pipeline", "pipe", "crossing", "telecom",
                   "power", "infield", "umbilical")

STAGE_COLUMNS = ["Layer", "Role", "Originator", "Revision", "Status",
                 "Quality", "Notes"]
_COL_LAYER, _COL_ROLE, _COL_ORIG, _COL_REV, _COL_STATUS, _COL_QUALITY, \
    _COL_NOTES = range(len(STAGE_COLUMNS))
_QUALITIES = ["", "high", "moderate", "low", "insufficient"]
_STATUSES = ["current", "superseded"]


def _enum_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(getattr(value, "value", -1))


def geometry_kind(layer) -> str:
    """point / line / polygon / table / other for a vector layer."""
    if not isinstance(layer, QgsVectorLayer):
        return GEOM_OTHER
    try:
        code = _enum_int(layer.geometryType())
    except Exception:
        return GEOM_OTHER
    # Point=0, Line=1, Polygon=2, Unknown=3, Null=4 in QGIS 3 and 4.
    return {0: GEOM_POINT, 1: GEOM_LINE, 2: GEOM_POLYGON,
            4: GEOM_TABLE}.get(code, GEOM_OTHER)


def guess_role(name: str, geom: str) -> str:
    """A sensible starting role from geometry and layer name."""
    text = (name or "").lower()
    if geom == GEOM_POLYGON and any(w in text for w in _SOIL_WORDS):
        return schema.INPUT_ROLE_SOILS
    if geom == GEOM_LINE and any(w in text for w in _CROSSING_WORDS):
        return schema.INPUT_ROLE_CROSSINGS_LINES
    if geom == GEOM_POINT and any(w in text for w in _CROSSING_WORDS):
        return schema.INPUT_ROLE_CROSSINGS_POINTS
    return schema.INPUT_ROLE_OTHER


def role_problem(role: str, geom: str) -> str:
    """Why a role does not suit a layer's geometry ("" = fine)."""
    need = ROLE_GEOMETRY.get(role)
    if need and geom != need:
        return (f"{schema.INPUT_ROLE_LABELS.get(role, role)} needs a "
                f"{GEOM_LABELS[need].lower()} layer, this is "
                f"{GEOM_LABELS.get(geom, geom).lower()}")
    return ""


def matches_filter(name: str, group: str, text: str, geom: str,
                   wanted_geom: str) -> bool:
    """Search (all words, name or group) + geometry filter."""
    if wanted_geom and geom != wanted_geom:
        return False
    haystack = f"{name} {group}".lower()
    return all(word in haystack for word in (text or "").lower().split())


def layer_group_path(project, layer) -> str:
    """'Survey / MBES' style path of the Layers-panel groups above a layer."""
    try:
        node = project.layerTreeRoot().findLayer(layer.id())
    except Exception:
        return ""
    names = []
    parent = node.parent() if node is not None else None
    while parent is not None and parent.parent() is not None:
        names.append(parent.name())
        parent = parent.parent()
    return " / ".join(reversed(names))


class AddInputsDialog(QDialog):
    """Two-pane multi-layer registration (see module docstring)."""

    def __init__(self, registered: Sequence[Dict],
                 selected_layers_fn: Optional[Callable[[], List]] = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add inputs")
        self.resize(1100, 560)
        self._registered = list(registered or [])
        self._selected_layers_fn = selected_layers_fn
        self._layers: Dict[str, object] = {}
        project = QgsProject.instance()

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose layers on the left (search, filter by geometry, or take "
            "the layers selected in the Layers panel) and add them to the "
            "list on the right. Set each input's role and register details "
            "there — select several rows to set them together.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        splitter = QSplitter(getattr(Qt, "Orientation", Qt).Horizontal)
        layout.addWidget(splitter, 1)

        # -- left: available layers ------------------------------------------
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        filter_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search layer or group name…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.search_edit, 1)
        self.geom_combo = QComboBox()
        self.geom_combo.addItem("All geometries", "")
        for key in (GEOM_POINT, GEOM_LINE, GEOM_POLYGON, GEOM_TABLE):
            self.geom_combo.addItem(GEOM_LABELS[key] + "s", key)
        self.geom_combo.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.geom_combo)
        left_layout.addLayout(filter_row)
        options_row = QHBoxLayout()
        self.hide_registered = QCheckBox("Hide registered")
        self.hide_registered.setChecked(True)
        self.hide_registered.setToolTip(
            "Hide layers already registered as inputs of this plan.")
        self.hide_registered.toggled.connect(self._apply_filter)
        options_row.addWidget(self.hide_registered)
        options_row.addStretch(1)
        self.count_label = QLabel("")
        options_row.addWidget(self.count_label)
        left_layout.addLayout(options_row)

        self.layer_tree = QTreeWidget()
        self.layer_tree.setColumnCount(3)
        self.layer_tree.setHeaderLabels(["Layer", "Type", "Group"])
        self.layer_tree.setRootIsDecorated(False)
        self.layer_tree.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.layer_tree.setSortingEnabled(True)
        self.layer_tree.sortByColumn(0, getattr(Qt, "SortOrder", Qt).AscendingOrder)
        self.layer_tree.itemDoubleClicked.connect(
            lambda _item, _col: self._add_selected())
        left_layout.addWidget(self.layer_tree, 1)
        splitter.addWidget(left)

        # -- middle: move buttons --------------------------------------------
        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.addStretch(1)
        self.add_button = QPushButton("Add →")
        self.add_button.setToolTip("Stage the selected layers (double-click "
                                   "a layer also adds it).")
        self.add_button.clicked.connect(self._add_selected)
        middle_layout.addWidget(self.add_button)
        self.add_panel_button = QPushButton("Add Layers-panel\nselection →")
        self.add_panel_button.setToolTip(
            "Stage the layers currently selected in the QGIS Layers panel.")
        self.add_panel_button.clicked.connect(self._add_panel_selection)
        self.add_panel_button.setEnabled(selected_layers_fn is not None)
        middle_layout.addWidget(self.add_panel_button)
        self.remove_button = QPushButton("← Remove")
        self.remove_button.clicked.connect(self._remove_staged)
        middle_layout.addWidget(self.remove_button)
        middle_layout.addStretch(1)
        splitter.addWidget(middle)

        # -- right: staged inputs --------------------------------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.stage_table = QTableWidget(0, len(STAGE_COLUMNS))
        self.stage_table.setHorizontalHeaderLabels(STAGE_COLUMNS)
        self.stage_table.verticalHeader().setVisible(False)
        self.stage_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.stage_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.stage_table.setEditTriggers(
            EDIT_TRIGGER_DOUBLE_CLICKED | EDIT_TRIGGER_EDIT_KEY_PRESSED
            | EDIT_TRIGGER_SELECTED_CLICKED)
        header = self.stage_table.horizontalHeader()
        header.setSectionResizeMode(_COL_LAYER, HEADER_RESIZE_MODE_STRETCH)
        header.setSectionResizeMode(_COL_ROLE, HEADER_RESIZE_MODE_CONTENTS)
        header.setSectionResizeMode(_COL_NOTES, HEADER_RESIZE_MODE_STRETCH)
        right_layout.addWidget(self.stage_table, 1)

        bulk = QHBoxLayout()
        bulk.addWidget(QLabel("Set for selected rows:"))
        self.bulk_role = QComboBox()
        self.bulk_role.addItem("(role)", "")
        for role in schema.INPUT_ROLES:
            if role != schema.INPUT_ROLE_BATHY:
                self.bulk_role.addItem(schema.INPUT_ROLE_LABELS[role], role)
        bulk.addWidget(self.bulk_role)
        self.bulk_originator = QLineEdit()
        self.bulk_originator.setPlaceholderText("Originator")
        bulk.addWidget(self.bulk_originator)
        self.bulk_revision = QLineEdit()
        self.bulk_revision.setPlaceholderText("Revision")
        bulk.addWidget(self.bulk_revision)
        self.bulk_quality = QComboBox()
        self.bulk_quality.addItem("(quality)", None)
        for quality in _QUALITIES[1:]:
            self.bulk_quality.addItem(quality, quality)
        bulk.addWidget(self.bulk_quality)
        apply_bulk = QPushButton("Apply")
        apply_bulk.setToolTip("Copy the non-empty values to every selected "
                              "staged row.")
        apply_bulk.clicked.connect(self._apply_bulk)
        bulk.addWidget(apply_bulk)
        right_layout.addLayout(bulk)
        self.problem_label = QLabel("")
        self.problem_label.setWordWrap(True)
        right_layout.addWidget(self.problem_label)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(2, 6)

        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.ok_button = buttons.button(BUTTON_BOX_OK)

        self._populate(project)
        self._sync_ok()

    # -- available layers -----------------------------------------------------
    def _registered_ids(self) -> set:
        keys = set()
        for row in self._registered:
            if row.get("layer_id_hint"):
                keys.add(row.get("layer_id_hint"))
            identity = map_layers.source_identity(row.get("layer_source") or "")
            if identity:
                keys.add(identity)
        return keys

    def _populate(self, project) -> None:
        self.layer_tree.clear()
        self._layers = {}
        keys = self._registered_ids()
        for layer in project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            geom = geometry_kind(layer)
            group = layer_group_path(project, layer)
            registered = (layer.id() in keys
                          or map_layers.layer_identity(layer) in keys)
            item = QTreeWidgetItem([layer.name(), GEOM_LABELS.get(geom, geom),
                                    group])
            item.setData(0, ITEM_DATA_USER_ROLE, layer.id())
            item.setData(1, ITEM_DATA_USER_ROLE, geom)
            item.setData(2, ITEM_DATA_USER_ROLE, registered)
            tip = layer.source()
            if registered:
                tip = "Already registered for this plan.\n" + tip
                for column in range(3):
                    item.setForeground(column, self.palette().mid())
            item.setToolTip(0, tip)
            self.layer_tree.addTopLevelItem(item)
            self._layers[layer.id()] = layer
        for column in range(3):
            self.layer_tree.resizeColumnToContents(column)
        self._apply_filter()

    def _apply_filter(self, *_args) -> None:
        text = self.search_edit.text()
        wanted = self.geom_combo.currentData() or ""
        hide = self.hide_registered.isChecked()
        staged = set(self._staged_ids())
        shown = 0
        for index in range(self.layer_tree.topLevelItemCount()):
            item = self.layer_tree.topLevelItem(index)
            layer_id = item.data(0, ITEM_DATA_USER_ROLE)
            visible = matches_filter(item.text(0), item.text(2), text,
                                     item.data(1, ITEM_DATA_USER_ROLE), wanted)
            if hide and item.data(2, ITEM_DATA_USER_ROLE):
                visible = False
            if layer_id in staged:
                visible = False
            item.setHidden(not visible)
            shown += int(visible)
        total = self.layer_tree.topLevelItemCount()
        self.count_label.setText(f"{shown} of {total} layers")

    # -- staging ----------------------------------------------------------------
    def _staged_ids(self) -> List[str]:
        ids = []
        for row in range(self.stage_table.rowCount()):
            item = self.stage_table.item(row, _COL_LAYER)
            if item is not None:
                ids.append(item.data(ITEM_DATA_USER_ROLE))
        return ids

    def _add_selected(self) -> None:
        layers = [self._layers.get(item.data(0, ITEM_DATA_USER_ROLE))
                  for item in self.layer_tree.selectedItems()
                  if not item.isHidden()]
        self.stage_layers([l for l in layers if l is not None])

    def _add_panel_selection(self) -> None:
        if self._selected_layers_fn is None:
            return
        try:
            layers = list(self._selected_layers_fn() or [])
        except Exception:
            layers = []
        vectors = [l for l in layers if isinstance(l, QgsVectorLayer)]
        skipped = len(layers) - len(vectors)
        self.stage_layers(vectors)
        if not layers:
            self.problem_label.setText(
                "No layers are selected in the Layers panel.")
        elif skipped:
            self.problem_label.setText(
                f"{skipped} non-vector layer(s) skipped — bathymetry rasters "
                "are set in the Bathymetry source section.")

    def stage_layers(self, layers: Sequence) -> None:
        staged = set(self._staged_ids())
        for layer in layers:
            if layer is None or layer.id() in staged:
                continue
            staged.add(layer.id())
            geom = geometry_kind(layer)
            row = self.stage_table.rowCount()
            self.stage_table.insertRow(row)
            name_item = QTableWidgetItem(layer.name())
            name_item.setData(ITEM_DATA_USER_ROLE, layer.id())
            name_item.setFlags(name_item.flags()
                               & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setToolTip(f"{GEOM_LABELS.get(geom, geom)} layer\n"
                                 f"{layer.source()}")
            self.stage_table.setItem(row, _COL_LAYER, name_item)
            role_combo = QComboBox()
            for role in schema.INPUT_ROLES:
                if role != schema.INPUT_ROLE_BATHY:
                    role_combo.addItem(schema.INPUT_ROLE_LABELS[role], role)
            role_combo.setCurrentIndex(max(0, role_combo.findData(
                guess_role(layer.name(), geom))))
            role_combo.currentIndexChanged.connect(self._sync_ok)
            self.stage_table.setCellWidget(row, _COL_ROLE, role_combo)
            status_combo = QComboBox()
            status_combo.addItems(_STATUSES)
            self.stage_table.setCellWidget(row, _COL_STATUS, status_combo)
            quality_combo = QComboBox()
            quality_combo.addItems(_QUALITIES)
            self.stage_table.setCellWidget(row, _COL_QUALITY, quality_combo)
            for column in (_COL_ORIG, _COL_REV, _COL_NOTES):
                self.stage_table.setItem(row, column, QTableWidgetItem(""))
        self._apply_filter()
        self._sync_ok()

    def _remove_staged(self) -> None:
        rows = sorted({index.row() for index in
                       self.stage_table.selectionModel().selectedRows()},
                      reverse=True)
        for row in rows:
            self.stage_table.removeRow(row)
        self._apply_filter()
        self._sync_ok()

    def _apply_bulk(self) -> None:
        rows = sorted({index.row() for index in
                       self.stage_table.selectionModel().selectedRows()})
        if not rows:
            self.problem_label.setText("Select staged rows first.")
            return
        role = self.bulk_role.currentData() or ""
        quality = self.bulk_quality.currentData()
        for row in rows:
            if role:
                combo = self.stage_table.cellWidget(row, _COL_ROLE)
                combo.setCurrentIndex(max(0, combo.findData(role)))
            for column, edit in ((_COL_ORIG, self.bulk_originator),
                                 (_COL_REV, self.bulk_revision)):
                if edit.text().strip():
                    self.stage_table.setItem(
                        row, column, QTableWidgetItem(edit.text().strip()))
            if quality is not None:
                combo = self.stage_table.cellWidget(row, _COL_QUALITY)
                combo.setCurrentIndex(max(0, combo.findText(quality)))
        self._sync_ok()

    def _row_problems(self) -> List[str]:
        problems = []
        for row in range(self.stage_table.rowCount()):
            layer = self._layers.get(
                self.stage_table.item(row, _COL_LAYER).data(ITEM_DATA_USER_ROLE))
            role = self.stage_table.cellWidget(row, _COL_ROLE).currentData()
            problem = role_problem(role, geometry_kind(layer)) \
                if layer is not None else "layer no longer in the project"
            if problem:
                name = self.stage_table.item(row, _COL_LAYER).text()
                problems.append(f"{name}: {problem}")
        return problems

    def _sync_ok(self, *_args) -> None:
        problems = self._row_problems()
        count = self.stage_table.rowCount()
        self.ok_button.setEnabled(count > 0 and not problems)
        self.ok_button.setText(f"Register {count} input(s)" if count
                               else "Register")
        if problems:
            self.problem_label.setText("Fix the role of: " + "; ".join(
                problems[:4]) + ("; …" if len(problems) > 4 else ""))
            self.problem_label.setStyleSheet(ui_helpers.status_style("error"))
        else:
            self.problem_label.setText("")
            self.problem_label.setStyleSheet("")

    def _accept(self) -> None:
        if self.stage_table.rowCount() == 0:
            return
        problems = self._row_problems()
        if problems:
            QMessageBox.warning(self, "Add inputs", "\n".join(problems))
            return
        self.accept()

    def result_rows(self) -> List[Dict]:
        rows = []
        now = schema.utc_now_iso()
        for row in range(self.stage_table.rowCount()):
            layer = self._layers.get(
                self.stage_table.item(row, _COL_LAYER).data(ITEM_DATA_USER_ROLE))
            if layer is None:
                continue

            def text(column: int) -> str:
                item = self.stage_table.item(row, column)
                return item.text().strip() if item is not None else ""

            rows.append({
                "role": self.stage_table.cellWidget(row, _COL_ROLE).currentData(),
                "layer_name": layer.name(),
                "layer_source": layer.source(),
                "layer_id_hint": layer.id(),
                "config_json": "{}",
                "originator": text(_COL_ORIG),
                "revision": text(_COL_REV),
                "status": self.stage_table.cellWidget(row, _COL_STATUS).currentText(),
                "received_utc": now,
                "quality": self.stage_table.cellWidget(row, _COL_QUALITY).currentText(),
                "notes": text(_COL_NOTES),
            })
        return rows
