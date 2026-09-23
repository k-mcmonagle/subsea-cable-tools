# -*- coding: utf-8 -*-
"""Inputs — see, edit and add a plan's registered inputs in one dialog.

A two-pane manager for projects with many layers: the left pane lists the
project's vector layers with a search box, a geometry filter (points /
lines / polygons / tables), the Layers-panel group each layer sits in and
whether it is already registered; the right pane lists the plan's
registered inputs (role and register details editable, *Remove*
unregisters) plus the newly chosen layers with their role (guessed from
geometry and name, editable) and the optional Input Data Register details,
which can be set per row or for all selected rows at once. OK saves the
registrations, edits and removals in one change-log entry.

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
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
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


_INPUT_ID_ROLE = int(ITEM_DATA_USER_ROLE) + 1
_EDITABLE_FIELDS = ("role", "originator", "revision", "status", "quality",
                    "notes")


class AddInputsDialog(QDialog):
    """Two-pane input manager (see module docstring).

    The right pane opens with the plan's registered inputs (bathymetry
    excepted — it has its own editor): their role and register details are
    editable and *Remove* unregisters them on OK. Layers added from the
    left are new registrations. ``result_rows()`` returns new and changed
    rows, ``removed_input_ids()`` the inputs to unregister.
    """

    def __init__(self, registered: Sequence[Dict],
                 selected_layers_fn: Optional[Callable[[], List]] = None,
                 parent=None, usage: Optional[Dict[str, List[str]]] = None):
        super().__init__(parent)
        self.setWindowTitle("Inputs")
        self.resize(1100, 560)
        self._registered = list(registered or [])
        self._selected_layers_fn = selected_layers_fn
        self._usage = dict(usage or {})
        self._layers: Dict[str, object] = {}
        # input_id -> stored row, for the registered inputs listed on the right.
        self._existing: Dict[str, Dict] = {}
        project = QgsProject.instance()

        layout = QVBoxLayout(self)
        intro = QLabel(
            "The right-hand list shows this plan's registered inputs (✓) — "
            "edit their role and register details there, or Remove to "
            "unregister. To register more, choose layers on the left "
            "(search, filter by geometry, or take the layers selected in the "
            "Layers panel) and add them. Select several rows to set details "
            "together. Bathymetry is set with Configure bathymetry…")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        splitter = QSplitter(getattr(Qt, "Orientation", Qt).Horizontal)
        layout.addWidget(splitter, 1)

        # -- left: available layers ------------------------------------------
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("<b>Project layers</b>"))
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
            "Hide layers already registered as inputs of this plan (they are "
            "listed on the right). Untick to register a layer again, e.g. "
            "under another role.")
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
        self.remove_button.setToolTip(
            "Take the selected rows off the list: new layers are simply "
            "not added; registered inputs (✓) are unregistered on OK.")
        self.remove_button.clicked.connect(self._remove_staged)
        middle_layout.addWidget(self.remove_button)
        middle_layout.addStretch(1)
        splitter.addWidget(middle)

        # -- right: registered + staged inputs --------------------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.summary_label = QLabel("")
        right_layout.addWidget(self.summary_label)
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
        self.stage_table.itemChanged.connect(self._sync_ok)
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
                              "row.")
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
        self._list_registered()
        self._apply_filter()
        self._sync_ok()

    # -- available layers -----------------------------------------------------
    def _registered_ids(self) -> set:
        """Layer ids / source identities of the registered inputs still on
        the list (a registered row marked for removal frees its layer)."""
        kept = set(self._listed_input_ids())
        keys = set()
        for row in self._registered:
            input_id = row.get("input_id")
            if input_id and input_id in self._existing and input_id not in kept:
                continue
            if row.get("layer_id_hint"):
                keys.add(row.get("layer_id_hint"))
            identity = map_layers.source_identity(row.get("layer_source") or "")
            if identity:
                keys.add(identity)
        return keys

    def _populate(self, project) -> None:
        self.layer_tree.clear()
        self._layers = {}
        for layer in project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            geom = geometry_kind(layer)
            group = layer_group_path(project, layer)
            item = QTreeWidgetItem([layer.name(), GEOM_LABELS.get(geom, geom),
                                    group])
            item.setData(0, ITEM_DATA_USER_ROLE, layer.id())
            item.setData(1, ITEM_DATA_USER_ROLE, geom)
            item.setData(2, ITEM_DATA_USER_ROLE,
                         map_layers.layer_identity(layer))
            item.setToolTip(0, layer.source())
            self.layer_tree.addTopLevelItem(item)
            self._layers[layer.id()] = layer
        for column in range(3):
            self.layer_tree.resizeColumnToContents(column)

    def _project_layer_for(self, row: Dict):
        """The project layer a registered row points at (id, then source)."""
        layer = self._layers.get(row.get("layer_id_hint") or "")
        if layer is not None:
            return layer
        identity = map_layers.source_identity(row.get("layer_source") or "")
        if not identity:
            return None
        return next((l for l in self._layers.values()
                     if map_layers.layer_identity(l) == identity), None)

    def _apply_filter(self, *_args) -> None:
        text = self.search_edit.text()
        wanted = self.geom_combo.currentData() or ""
        hide = self.hide_registered.isChecked()
        staged = set(self._staged_ids())
        keys = self._registered_ids()
        shown = 0
        for index in range(self.layer_tree.topLevelItemCount()):
            item = self.layer_tree.topLevelItem(index)
            layer_id = item.data(0, ITEM_DATA_USER_ROLE)
            registered = (layer_id in keys
                          or (item.data(2, ITEM_DATA_USER_ROLE) or "") in keys)
            visible = matches_filter(item.text(0), item.text(2), text,
                                     item.data(1, ITEM_DATA_USER_ROLE), wanted)
            if hide and registered:
                visible = False
            if layer_id in staged:
                visible = False
            brush = self.palette().mid() if registered else self.palette().text()
            for column in range(3):
                item.setForeground(column, brush)
            layer = self._layers.get(layer_id)
            source = layer.source() if layer is not None else ""
            item.setToolTip(0, ("Already registered for this plan.\n" + source)
                            if registered else source)
            item.setHidden(not visible)
            shown += int(visible)
        total = self.layer_tree.topLevelItemCount()
        self.count_label.setText(f"{shown} of {total} layers")

    # -- the list on the right -------------------------------------------------
    def _row_input_id(self, row: int) -> str:
        item = self.stage_table.item(row, _COL_LAYER)
        return str(item.data(_INPUT_ID_ROLE) or "") if item is not None else ""

    def _row_layer(self, row: int):
        item = self.stage_table.item(row, _COL_LAYER)
        if item is None:
            return None
        return self._layers.get(item.data(ITEM_DATA_USER_ROLE) or "")

    def _listed_input_ids(self) -> List[str]:
        return [i for i in (self._row_input_id(r)
                            for r in range(self.stage_table.rowCount())) if i]

    def _staged_ids(self) -> List[str]:
        """Layer ids of the new (not yet registered) rows."""
        ids = []
        for row in range(self.stage_table.rowCount()):
            item = self.stage_table.item(row, _COL_LAYER)
            if item is not None and not item.data(_INPUT_ID_ROLE):
                ids.append(item.data(ITEM_DATA_USER_ROLE))
        return ids

    def _list_registered(self) -> None:
        for stored in self._registered:
            input_id = stored.get("input_id")
            if not input_id or stored.get("role") == schema.INPUT_ROLE_BATHY:
                continue
            self._existing[input_id] = dict(stored)
            layer = self._project_layer_for(stored)
            name = layer.name() if layer is not None else (
                stored.get("layer_name") or "(unnamed layer)")
            if layer is not None:
                geom = geometry_kind(layer)
                tip = (f"Registered input ({GEOM_LABELS.get(geom, geom).lower()}"
                       f" layer) — changes here update it.\n{layer.source()}")
            else:
                tip = ("Registered input — its layer is not in the project "
                       "(relink it with Edit / relink in the Inputs tab).\n"
                       f"{stored.get('layer_source') or ''}")
            self._append_row(
                layer.id() if layer is not None else "", "✓ " + name, tip,
                stored.get("role") or "", stored, input_id)
        self.stage_table.resizeColumnToContents(_COL_ROLE)

    def _append_row(self, layer_id: str, text: str, tip: str, role: str,
                    details: Dict, input_id: str = "") -> None:
        self.stage_table.blockSignals(True)
        try:
            row = self.stage_table.rowCount()
            self.stage_table.insertRow(row)
            name_item = QTableWidgetItem(text)
            name_item.setData(ITEM_DATA_USER_ROLE, layer_id)
            name_item.setData(_INPUT_ID_ROLE, input_id)
            name_item.setFlags(name_item.flags()
                               & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setToolTip(tip)
            if input_id and not layer_id:
                name_item.setForeground(self.palette().mid())
            self.stage_table.setItem(row, _COL_LAYER, name_item)
            role_combo = QComboBox()
            for choice in schema.INPUT_ROLES:
                if choice != schema.INPUT_ROLE_BATHY:
                    role_combo.addItem(schema.INPUT_ROLE_LABELS[choice], choice)
            if role_combo.findData(role) < 0:
                # Keep an unknown stored role as it is (never silently change it).
                role_combo.addItem(schema.INPUT_ROLE_LABELS.get(role, role)
                                   or "(unset)", role)
            role_combo.setCurrentIndex(max(0, role_combo.findData(role)))
            role_combo.currentIndexChanged.connect(self._sync_ok)
            self.stage_table.setCellWidget(row, _COL_ROLE, role_combo)
            for column, choices, value in (
                    (_COL_STATUS, _STATUSES, details.get("status") or "current"),
                    (_COL_QUALITY, _QUALITIES, details.get("quality") or "")):
                combo = QComboBox()
                combo.addItems(choices)
                if combo.findText(value) < 0:
                    combo.addItem(value)
                combo.setCurrentIndex(combo.findText(value))
                combo.currentIndexChanged.connect(self._sync_ok)
                self.stage_table.setCellWidget(row, column, combo)
            for column, key in ((_COL_ORIG, "originator"),
                                (_COL_REV, "revision"), (_COL_NOTES, "notes")):
                self.stage_table.setItem(
                    row, column, QTableWidgetItem(str(details.get(key) or "")))
        finally:
            self.stage_table.blockSignals(False)

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
                "are set with Configure bathymetry…")

    def stage_layers(self, layers: Sequence) -> None:
        staged = set(self._staged_ids())
        for layer in layers:
            if layer is None or layer.id() in staged:
                continue
            staged.add(layer.id())
            self._layers.setdefault(layer.id(), layer)
            geom = geometry_kind(layer)
            self._append_row(
                layer.id(), layer.name(),
                f"New input: {GEOM_LABELS.get(geom, geom)} layer\n"
                f"{layer.source()}",
                guess_role(layer.name(), geom), {})
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
            self.problem_label.setText("Select rows on the right first.")
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

    def _row_values(self, row: int) -> Dict[str, str]:
        def text(column: int) -> str:
            item = self.stage_table.item(row, column)
            return item.text().strip() if item is not None else ""

        return {
            "role": self.stage_table.cellWidget(row, _COL_ROLE).currentData() or "",
            "originator": text(_COL_ORIG),
            "revision": text(_COL_REV),
            "status": self.stage_table.cellWidget(row, _COL_STATUS).currentText(),
            "quality": self.stage_table.cellWidget(row, _COL_QUALITY).currentText(),
            "notes": text(_COL_NOTES),
        }

    def _row_changed(self, row: int) -> bool:
        stored = self._existing.get(self._row_input_id(row))
        if stored is None:
            return False
        values = self._row_values(row)
        return any(values[key] != str(stored.get(key) or "")
                   for key in _EDITABLE_FIELDS
                   if not (key == "status" and not stored.get(key)
                           and values[key] == "current"))

    def _row_problems(self) -> List[str]:
        problems = []
        for row in range(self.stage_table.rowCount()):
            layer = self._row_layer(row)
            role = self.stage_table.cellWidget(row, _COL_ROLE).currentData()
            if layer is not None:
                problem = role_problem(role, geometry_kind(layer))
            elif self._row_input_id(row):
                problem = ""  # registered, layer not loaded: geometry unknown
            else:
                problem = "layer no longer in the project"
            if problem:
                name = self.stage_table.item(row, _COL_LAYER).text()
                problems.append(f"{name}: {problem}")
        return problems

    def removed_input_ids(self) -> List[str]:
        listed = set(self._listed_input_ids())
        return [i for i in self._existing if i not in listed]

    def _counts(self) -> Dict[str, int]:
        rows = range(self.stage_table.rowCount())
        return {
            "registered": sum(1 for r in rows if self._row_input_id(r)),
            "new": sum(1 for r in rows if not self._row_input_id(r)),
            "changed": sum(1 for r in rows if self._row_changed(r)),
            "removed": len(self.removed_input_ids()),
        }

    def _sync_ok(self, *_args) -> None:
        problems = self._row_problems()
        counts = self._counts()
        summary = [f"{counts['registered']} registered"]
        if counts["changed"]:
            summary.append(f"{counts['changed']} edited")
        if counts["new"]:
            summary.append(f"{counts['new']} new")
        if counts["removed"]:
            summary.append(f"{counts['removed']} to unregister")
        self.summary_label.setText("<b>Plan inputs</b> — " + " · ".join(summary))
        pending = counts["new"] + counts["changed"] + counts["removed"]
        self.ok_button.setEnabled(pending > 0 and not problems)
        if counts["new"] and not (counts["changed"] or counts["removed"]):
            self.ok_button.setText(f"Register {counts['new']} input(s)")
        elif pending:
            self.ok_button.setText("Save changes")
        else:
            self.ok_button.setText("No changes")
        if problems:
            self.problem_label.setText("Fix the role of: " + "; ".join(
                problems[:4]) + ("; …" if len(problems) > 4 else ""))
            self.problem_label.setStyleSheet(ui_helpers.status_style("error"))
        else:
            self.problem_label.setText("")
            self.problem_label.setStyleSheet("")

    def _accept(self) -> None:
        problems = self._row_problems()
        if problems:
            QMessageBox.warning(self, "Inputs", "\n".join(problems))
            return
        removed = self.removed_input_ids()
        if removed:
            names = [self._existing[i].get("layer_name") or i for i in removed]
            users = [u for i in removed for u in self._usage.get(str(i), [])]
            message = (f"Unregister {len(removed)} input(s)?\n• "
                       + "\n• ".join(names[:8])
                       + (f"\n… and {len(names) - 8} more" if len(names) > 8 else ""))
            if users:
                message += ("\n\nThese criteria/checks use them and will "
                            "report a missing input until re-pointed:\n• "
                            + "\n• ".join(users[:8])
                            + (f"\n… and {len(users) - 8} more"
                               if len(users) > 8 else ""))
            answer = QMessageBox.question(
                self, "Inputs", message, MESSAGE_BOX_YES | MESSAGE_BOX_NO,
                MESSAGE_BOX_NO)
            if answer != MESSAGE_BOX_YES:
                return
        self.accept()

    def result_rows(self) -> List[Dict]:
        """New registrations plus registered inputs whose details changed."""
        rows = []
        now = schema.utc_now_iso()
        for row in range(self.stage_table.rowCount()):
            input_id = self._row_input_id(row)
            if input_id:
                if self._row_changed(row):
                    updated = dict(self._existing[input_id])
                    updated.update(self._row_values(row))
                    rows.append(updated)
                continue
            layer = self._row_layer(row)
            if layer is None:
                continue
            new = {
                "layer_name": layer.name(),
                "layer_source": layer.source(),
                "layer_id_hint": layer.id(),
                "config_json": "{}",
                "received_utc": now,
            }
            new.update(self._row_values(row))
            rows.append(new)
        return rows
