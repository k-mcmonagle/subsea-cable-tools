# -*- coding: utf-8 -*-
"""A searchable, sortable table for choosing depth sources (rasters and contour lines).

Projects often hold dozens of layers, so the table helps find the bathymetry:

* a search box (layer name or layer-tree group), a type filter (rasters /
  contour lines) and *Checked only*;
* columns sort on a click (layer, type, likely bathymetry, depth field, group);
* the suggested order (the initial one, restored by *Suggested order*) puts
  checked layers first, then layers whose names suggest bathymetry (bathy,
  MBES, depth, XYZ, DTM, contour, ...), then rasters before contour lines;
* *Check shown* / *Uncheck shown* act on the rows the filters leave visible.

The contour depth field is stored in the model (an editor opens on a click),
so filtering and sorting never lose a choice.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QSortFilterProxyModel, Qt
from qgis.PyQt.QtGui import QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
                                 QStyledItemDelegate, QTableView, QToolButton, QVBoxLayout, QWidget)

from ..qgis_compat import (CHECK_STATE_CHECKED, CHECK_STATE_UNCHECKED, EDIT_TRIGGER_DOUBLE_CLICKED,
                           EDIT_TRIGGER_SELECTED_CLICKED, FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_LONG_LONG,
                           HEADER_RESIZE_MODE_CONTENTS, HEADER_RESIZE_MODE_INTERACTIVE, HEADER_RESIZE_MODE_STRETCH,
                           ITEM_DATA_EDIT_ROLE, ITEM_DATA_USER_ROLE, ITEM_FLAG_EDITABLE, ITEM_FLAG_USER_CHECKABLE,
                           LAYER_RASTER, LAYER_VECTOR, SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_SINGLE,
                           _scoped_member)

COL_LAYER, COL_TYPE, COL_MATCH, COL_FIELD, COL_GROUP = range(5)
HEADERS = ("Layer", "Type", "Bathy?", "Depth field (contours)", "Group")

LAYER_ID_ROLE = ITEM_DATA_USER_ROLE
SORT_ROLE = int(ITEM_DATA_USER_ROLE) + 1
FIELDS_ROLE = int(ITEM_DATA_USER_ROLE) + 2
KIND_ROLE = int(ITEM_DATA_USER_ROLE) + 3

KIND_RASTER = "raster"
KIND_CONTOUR = "contour"

_TOOLTIP_ROLE = _scoped_member(Qt, "ItemDataRole", "ToolTipRole")
_ASCENDING = _scoped_member(Qt, "SortOrder", "AscendingOrder")
_EDIT_TRIGGER_CURRENT_CHANGED = _scoped_member(QAbstractItemView, "EditTrigger", "CurrentChanged")

# Names that suggest a depth source. Short tokens must stand alone ("dem",
# not "academic"); longer ones may be part of a word ("bathymetry", "contours").
_KEYWORD_PARTS = ("bathy", "mbes", "multibeam", "depth", "sounding", "contour", "isobath", "gebco",
                  "emodnet", "elevation", "seabed_dtm")
_KEYWORD_TOKENS = re.compile(r"(?<![a-z])(dtm|dem|xyz|sbes|z)(?![a-z])")
# Contour fields tried first for the depth value, in this order.
_DEPTH_FIELD_HINTS = ("depth", "elev", "contour", "value", "z")


def looks_like_bathymetry(*names: str) -> bool:
    """True when any of ``names`` (layer name, group, file) suggests bathymetry."""
    for name in names:
        text = str(name or "").lower()
        if any(part in text for part in _KEYWORD_PARTS) or _KEYWORD_TOKENS.search(text):
            return True
    return False


def numeric_fields(layer) -> List[str]:
    try:
        return [f.name() for f in layer.fields()
                if f.type() in (FIELD_TYPE_INT, FIELD_TYPE_DOUBLE, FIELD_TYPE_LONG_LONG)]
    except (AttributeError, RuntimeError):
        return []


def guess_depth_field(fields: Sequence[str]) -> str:
    """The numeric field most likely to hold contour depths ('' when none)."""
    for hint in _DEPTH_FIELD_HINTS:
        for name in fields:
            if hint == "z" and name.lower() != "z":
                continue
            if hint in name.lower():
                return name
    return fields[0] if fields else ""


def layer_group_path(layer, project=None) -> str:
    """The layer's group in the layer tree, e.g. ``Survey / MBES`` ('' at the root)."""
    try:
        node = (project or QgsProject.instance()).layerTreeRoot().findLayer(layer.id())
        names = []
        parent = node.parent() if node is not None else None
        while parent is not None and parent.parent() is not None:
            names.append(parent.name())
            parent = parent.parent()
        return " / ".join(reversed(names))
    except (AttributeError, RuntimeError):
        return ""


class _SourceFilter(QSortFilterProxyModel):
    """Search text (layer name or group), type and *checked only*; unsorted = suggested order."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ""
        self.kind = ""
        self.checked_only = False
        self.setSortRole(SORT_ROLE)

    def refresh(self):
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802 - Qt override
        model = self.sourceModel()
        layer_item = model.item(row, COL_LAYER)
        if layer_item is None:
            return False
        if self.kind and layer_item.data(KIND_ROLE) != self.kind:
            return False
        if self.checked_only and layer_item.checkState() != CHECK_STATE_CHECKED:
            return False
        if self.text:
            group_item = model.item(row, COL_GROUP)
            haystack = (layer_item.text() + " " + (group_item.text() if group_item else "")).lower()
            if not all(word in haystack for word in self.text.lower().split()):
                return False
        return True


class _FieldDelegate(QStyledItemDelegate):
    """A combo of the layer's numeric fields for the depth field column."""

    def createEditor(self, parent, option, index):  # noqa: N802 - Qt override
        fields = index.data(FIELDS_ROLE) or []
        if not fields:
            return None
        combo = QComboBox(parent)
        combo.addItems([str(f) for f in fields])
        combo.activated.connect(lambda _i, c=combo: self.commitData.emit(c))
        return combo

    def setEditorData(self, editor, index):  # noqa: N802 - Qt override
        position = editor.findText(str(index.data(ITEM_DATA_EDIT_ROLE) or ""))
        editor.setCurrentIndex(max(position, 0))

    def setModelData(self, editor, model, index):  # noqa: N802 - Qt override
        text = editor.currentText()
        model.setData(index, text, ITEM_DATA_EDIT_ROLE)
        model.setData(index, text.lower(), SORT_ROLE)


class DepthSourceTable(QWidget):
    """Choose depth sources: ``selected_sources()`` -> ``[(layer_id, field)]``.

    ``layers``: candidate layers (rasters, line layers); ``selected``:
    ``[(layer_id, field)]`` already chosen.
    """

    def __init__(self, layers: Iterable, selected: Optional[Sequence[Tuple[str, str]]] = None, parent=None):
        super().__init__(parent)
        chosen: Dict[str, str] = {str(layer_id): str(field or "") for layer_id, field in (selected or [])}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search layers or groups…")
        self.search.setClearButtonEnabled(True)
        bar.addWidget(self.search, 1)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("All types", "")
        self.kind_combo.addItem("Rasters", KIND_RASTER)
        self.kind_combo.addItem("Contour lines", KIND_CONTOUR)
        bar.addWidget(self.kind_combo)
        self.checked_only = QCheckBox("Checked only")
        bar.addWidget(self.checked_only)
        layout.addLayout(bar)

        self.model = QStandardItemModel(0, len(HEADERS), self)
        self.model.setHorizontalHeaderLabels(list(HEADERS))
        self.filter = _SourceFilter(self)
        self.filter.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.filter)
        self.view.verticalHeader().setVisible(False)
        self.view.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.view.setSelectionMode(SELECTION_MODE_SINGLE)
        self.view.setEditTriggers(_EDIT_TRIGGER_CURRENT_CHANGED | EDIT_TRIGGER_SELECTED_CLICKED
                                  | EDIT_TRIGGER_DOUBLE_CLICKED)
        self.view.setItemDelegateForColumn(COL_FIELD, _FieldDelegate(self.view))
        self.view.setMinimumHeight(180)
        self.view.setWordWrap(False)
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(HEADER_RESIZE_MODE_INTERACTIVE)
        header.setSectionResizeMode(COL_LAYER, HEADER_RESIZE_MODE_STRETCH)
        header.setSectionResizeMode(COL_TYPE, HEADER_RESIZE_MODE_CONTENTS)
        header.setSectionResizeMode(COL_MATCH, HEADER_RESIZE_MODE_CONTENTS)
        header.setSortIndicatorShown(True)
        header.setSortIndicator(-1, _ASCENDING)      # no sort: the suggested order
        self.view.setSortingEnabled(True)
        layout.addWidget(self.view, 1)

        actions = QHBoxLayout()
        self.count_label = QLabel("")
        actions.addWidget(self.count_label, 1)
        self.check_shown = QToolButton()
        self.check_shown.setText("Check shown")
        self.check_shown.setToolTip("Check every layer the search and filters leave in the table.")
        self.uncheck_shown = QToolButton()
        self.uncheck_shown.setText("Uncheck shown")
        self.suggested = QToolButton()
        self.suggested.setText("Suggested order")
        self.suggested.setToolTip("Checked layers first, then names that suggest bathymetry (bathy, MBES, depth, "
                                  "XYZ, DTM, contour…), then rasters before contour lines.")
        for button in (self.check_shown, self.uncheck_shown, self.suggested):
            actions.addWidget(button)
        layout.addLayout(actions)

        self._populate(list(layers or []), chosen)
        self.view.setColumnWidth(COL_FIELD, 170)
        self.view.setColumnWidth(COL_GROUP, 150)

        self.search.textChanged.connect(self._apply_filters)
        self.kind_combo.currentIndexChanged.connect(self._apply_filters)
        self.checked_only.toggled.connect(self._apply_filters)
        self.check_shown.clicked.connect(lambda: self._check_visible(True))
        self.uncheck_shown.clicked.connect(lambda: self._check_visible(False))
        self.suggested.clicked.connect(self.suggested_order)
        self.model.itemChanged.connect(self._on_item_changed)
        self._update_count()

    # -- building ----------------------------------------------------------
    def _populate(self, layers, chosen: Dict[str, str]) -> None:
        rows = []
        for layer in layers:
            try:
                is_raster = layer.type() == LAYER_RASTER
                if not is_raster and layer.type() != LAYER_VECTOR:
                    continue
                layer_id, name = layer.id(), layer.name()
                source = layer.source()
            except (AttributeError, RuntimeError):
                continue
            group = layer_group_path(layer)
            match = looks_like_bathymetry(name, group, source.split("|")[0].replace("\\", "/").split("/")[-1])
            checked = layer_id in chosen
            fields = [] if is_raster else numeric_fields(layer)
            field = "" if is_raster else (chosen.get(layer_id) if chosen.get(layer_id) in fields
                                          else guess_depth_field(fields))
            rank = (0 if checked else 1, 0 if match else 1, 0 if is_raster else 1, name.lower())
            rows.append((rank, layer_id, name, source, is_raster, match, checked, fields, field, group))
        rows.sort(key=lambda r: r[0])
        for _rank, layer_id, name, source, is_raster, match, checked, fields, field, group in rows:
            layer_item = QStandardItem(name)
            layer_item.setData(layer_id, LAYER_ID_ROLE)
            layer_item.setData(name.lower(), SORT_ROLE)
            layer_item.setData(KIND_RASTER if is_raster else KIND_CONTOUR, KIND_ROLE)
            layer_item.setData(source, _TOOLTIP_ROLE)
            layer_item.setFlags(layer_item.flags() & ~ITEM_FLAG_EDITABLE)
            if is_raster or fields:
                layer_item.setFlags(layer_item.flags() | ITEM_FLAG_USER_CHECKABLE)
                layer_item.setCheckState(CHECK_STATE_CHECKED if checked else CHECK_STATE_UNCHECKED)
            else:
                layer_item.setData("No numeric field to read contour depths from.", _TOOLTIP_ROLE)
            type_item = QStandardItem("Raster" if is_raster else "Contour lines")
            type_item.setData(0 if is_raster else 1, SORT_ROLE)
            match_item = QStandardItem("★" if match else "")
            match_item.setData(0 if match else 1, SORT_ROLE)
            match_item.setData("The name or group suggests bathymetry." if match else "", _TOOLTIP_ROLE)
            if is_raster:
                field_item = QStandardItem("—")
                field_item.setData("", SORT_ROLE)
            elif fields:
                field_item = QStandardItem(field)
                field_item.setData(field.lower(), SORT_ROLE)
                field_item.setData(list(fields), FIELDS_ROLE)
                field_item.setData("Click to choose the field that holds the contour depth.", _TOOLTIP_ROLE)
            else:
                field_item = QStandardItem("no numeric field")
                field_item.setData("~", SORT_ROLE)
            group_item = QStandardItem(group)
            group_item.setData(group.lower(), SORT_ROLE)
            for item in (type_item, match_item, group_item):
                item.setFlags(item.flags() & ~ITEM_FLAG_EDITABLE)
            if not fields:
                field_item.setFlags(field_item.flags() & ~ITEM_FLAG_EDITABLE)
            self.model.appendRow([layer_item, type_item, match_item, field_item, group_item])

    # -- behaviour ---------------------------------------------------------
    def _apply_filters(self, *_args) -> None:
        self.filter.text = self.search.text().strip()
        self.filter.kind = self.kind_combo.currentData() or ""
        self.filter.checked_only = self.checked_only.isChecked()
        self.filter.refresh()
        self._update_count()

    def _on_item_changed(self, item) -> None:
        if item.column() == COL_LAYER:
            if self.filter.checked_only:
                self.filter.refresh()
            self._update_count()

    def _check_visible(self, checked: bool) -> None:
        state = CHECK_STATE_CHECKED if checked else CHECK_STATE_UNCHECKED
        rows = [self.filter.mapToSource(self.filter.index(r, COL_LAYER)).row()
                for r in range(self.filter.rowCount())]
        for row in rows:
            item = self.model.item(row, COL_LAYER)
            if item is not None and item.flags() & ITEM_FLAG_USER_CHECKABLE:
                item.setCheckState(state)

    def suggested_order(self) -> None:
        """Back to the suggested (initial) order."""
        self.view.horizontalHeader().setSortIndicator(-1, _ASCENDING)
        self.filter.sort(-1)

    def _update_count(self) -> None:
        total = self.model.rowCount()
        checked = sum(1 for r in range(total)
                      if self.model.item(r, COL_LAYER).checkState() == CHECK_STATE_CHECKED)
        shown = self.filter.rowCount()
        text = f"{checked} checked" + (f" · {shown} of {total} layers shown" if shown != total
                                       else f" · {total} layers")
        self.count_label.setText(text)

    # -- result ------------------------------------------------------------
    def selected_sources(self) -> List[Tuple[str, str]]:
        """``[(layer_id, field)]`` of the checked layers, in the model's (suggested) order."""
        out = []
        for row in range(self.model.rowCount()):
            item = self.model.item(row, COL_LAYER)
            if item is None or item.checkState() != CHECK_STATE_CHECKED:
                continue
            field_item = self.model.item(row, COL_FIELD)
            field = "" if item.data(KIND_ROLE) == KIND_RASTER else str(
                field_item.data(ITEM_DATA_EDIT_ROLE) or field_item.text() or "")
            out.append((str(item.data(LAYER_ID_ROLE)), field.strip()))
        return out
