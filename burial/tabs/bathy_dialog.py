# -*- coding: utf-8 -*-
"""Bathymetry source — pick and register a plan's bathymetry input.

The same two-pane pattern as the Inputs dialog (``input_picker``): the left
pane lists the project layers that can serve as the chosen source type
(raster layers, or line layers for depth contours) with a search box and
the Layers-panel group each sits in; the right pane holds the plan's
source — one raster and its band, or up to two contour layers each with
its depth field — plus the Input Data Register details every input
carries. OK returns the input row (role ``bathymetry``) for the caller to
save; the configuration JSON matches what ``DepthSourceConfig`` reads.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
from qgis.gui import QgsFieldComboBox
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    ITEM_DATA_USER_ROLE,
    SELECTION_MODE_EXTENDED,
)
from .. import schema, ui_helpers
from .input_picker import (
    GEOM_LINE,
    _QUALITIES,
    _STATUSES,
    geometry_kind,
    layer_group_path,
    matches_filter,
)

MODE_RASTER = 1
MODE_CONTOURS = 2

_DEPTH_WORDS = ("depth", "elev", "contour", "level", "height", "value")


def guess_depth_field(names: List[str]) -> str:
    """The field most likely to hold contour depths ("" = no good guess)."""
    lowered = [(name, name.lower()) for name in names]
    for word in _DEPTH_WORDS:
        for name, low in lowered:
            if word in low:
                return name
    for name, low in lowered:
        if low in ("z", "d"):
            return name
    return ""


class _ContourSlot:
    """One contour layer slot: layer name, depth field, clear button."""

    def __init__(self, number: int, on_change):
        self.layer = None
        self.label = QLabel("—")
        self.label.setWordWrap(True)
        self.field = QgsFieldComboBox()
        self.field.setAllowEmptyFieldName(True)
        self.field.setToolTip(
            f"Attribute of contour layer {number} holding the depth.")
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(lambda: (self.set_layer(None),
                                                   on_change()))

    def set_layer(self, layer, field: str = "") -> None:
        self.layer = layer
        self.label.setText(layer.name() if layer is not None else "—")
        self.label.setToolTip(layer.source() if layer is not None else "")
        self.field.setLayer(layer)
        if layer is not None:
            names = [f.name() for f in layer.fields()]
            self.field.setField(field if field in names
                                else guess_depth_field(names))
        self.clear_button.setEnabled(layer is not None)


class BathymetryDialog(QDialog):
    """Two-pane bathymetry source picker (see module docstring)."""

    def __init__(self, row: Optional[Dict] = None, resolved=None, parent=None):
        """``row``: the plan's bathymetry input (or None). ``resolved``: the
        model's ``DepthSourceConfig`` (layer ids relinked by source)."""
        super().__init__(parent)
        self.setWindowTitle("Bathymetry source")
        self.resize(1000, 560)
        self._row = dict(row or {})
        self._layers: Dict[str, object] = {}
        try:
            config = json.loads(self._row.get("config_json") or "{}")
        except (TypeError, ValueError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        project = QgsProject.instance()

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose the bathymetry this plan's profile is sampled from — one "
            "raster, or up to two depth-contour line layers (e.g. minor and "
            "major contours, merged into one profile). Workbench RPL depth "
            "sources are not inherited. Pick layers on the left and add "
            "them; set the register details on the right.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Source type:"))
        self.raster_radio = QRadioButton("Raster")
        self.contour_radio = QRadioButton("Depth contours (up to two layers)")
        self._type_group = QButtonGroup(self)
        self._type_group.addButton(self.raster_radio, MODE_RASTER)
        self._type_group.addButton(self.contour_radio, MODE_CONTOURS)
        type_row.addWidget(self.raster_radio)
        type_row.addWidget(self.contour_radio)
        type_row.addStretch(1)
        layout.addLayout(type_row)

        splitter = QSplitter(getattr(Qt, "Orientation", Qt).Horizontal)
        layout.addWidget(splitter, 1)

        # -- left: candidate layers ------------------------------------------
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.left_title = QLabel("")
        left_layout.addWidget(self.left_title)
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search layer or group name…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search_edit, 1)
        self.count_label = QLabel("")
        search_row.addWidget(self.count_label)
        left_layout.addLayout(search_row)
        self.layer_tree = QTreeWidget()
        self.layer_tree.setColumnCount(2)
        self.layer_tree.setHeaderLabels(["Layer", "Group"])
        self.layer_tree.setRootIsDecorated(False)
        self.layer_tree.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.layer_tree.setSortingEnabled(True)
        self.layer_tree.sortByColumn(0, getattr(Qt, "SortOrder", Qt).AscendingOrder)
        self.layer_tree.itemDoubleClicked.connect(
            lambda _item, _col: self._add_selected())
        left_layout.addWidget(self.layer_tree, 1)
        splitter.addWidget(left)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.addStretch(1)
        self.add_button = QPushButton("Add →")
        self.add_button.setToolTip("Use the selected layer(s) (double-click "
                                   "a layer also adds it).")
        self.add_button.clicked.connect(self._add_selected)
        middle_layout.addWidget(self.add_button)
        middle_layout.addStretch(1)
        splitter.addWidget(middle)

        # -- right: the plan's source + register details -----------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("<b>Plan bathymetry</b>"))
        self.pages = QStackedWidget()
        raster_page = QWidget()
        raster_form = QFormLayout(raster_page)
        raster_row = QHBoxLayout()
        self.raster_label = QLabel("—")
        self.raster_label.setWordWrap(True)
        raster_row.addWidget(self.raster_label, 1)
        self.raster_clear = QPushButton("Clear")
        self.raster_clear.clicked.connect(lambda: self._set_raster(None))
        raster_row.addWidget(self.raster_clear)
        raster_form.addRow("Raster:", raster_row)
        self.raster_band = QSpinBox()
        self.raster_band.setRange(1, 99)
        raster_form.addRow("Band:", self.raster_band)
        self.pages.addWidget(raster_page)
        contour_page = QWidget()
        contour_form = QFormLayout(contour_page)
        self.slots = [_ContourSlot(1, self._slots_changed),
                      _ContourSlot(2, self._slots_changed)]
        for number, slot in enumerate(self.slots, start=1):
            slot_row = QHBoxLayout()
            slot_row.addWidget(slot.label, 1)
            slot_row.addWidget(slot.clear_button)
            contour_form.addRow(f"Contour layer {number}:" if number == 1
                                else "Contour layer 2 (optional):", slot_row)
            contour_form.addRow(f"Depth field {number}:", slot.field)
            slot.field.fieldChanged.connect(self._sync_ok)
        self.search_radius = QDoubleSpinBox()
        self.search_radius.setRange(1.0, 100000.0)
        self.search_radius.setSuffix(" m")
        self.search_radius.setToolTip(
            "How far from the route a contour crossing is looked for; depths "
            "are interpolated between the actual route crossings.")
        contour_form.addRow("Contour search radius:", self.search_radius)
        self.pages.addWidget(contour_page)
        right_layout.addWidget(self.pages)

        register = QGroupBox("Register details")
        register_form = QFormLayout(register)
        self.originator = QLineEdit(str(self._row.get("originator") or ""))
        self.revision = QLineEdit(str(self._row.get("revision") or ""))
        self.status = QComboBox()
        self.quality = QComboBox()
        for combo, choices, value in (
                (self.status, _STATUSES, self._row.get("status") or "current"),
                (self.quality, _QUALITIES, self._row.get("quality") or "")):
            combo.addItems(choices)
            if combo.findText(value) < 0:
                combo.addItem(value)
            combo.setCurrentIndex(combo.findText(value))
        self.notes = QLineEdit(str(self._row.get("notes") or ""))
        register_form.addRow("Originator:", self.originator)
        register_form.addRow("Revision:", self.revision)
        register_form.addRow("Status:", self.status)
        register_form.addRow("Quality:", self.quality)
        register_form.addRow("Notes:", self.notes)
        right_layout.addWidget(register)
        right_layout.addStretch(1)
        self.problem_label = QLabel("")
        self.problem_label.setWordWrap(True)
        right_layout.addWidget(self.problem_label)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(2, 5)

        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.ok_button = buttons.button(BUTTON_BOX_OK)
        self.ok_button.setText("Apply source")

        self._populate(project)
        self._load(config, resolved, project)
        self._type_group.buttonClicked.connect(lambda _b: self._mode_changed())
        self._mode_changed()

    # -- state ------------------------------------------------------------------
    def mode(self) -> int:
        return MODE_CONTOURS if self.contour_radio.isChecked() else MODE_RASTER

    def set_mode(self, mode: int) -> None:
        (self.contour_radio if mode == MODE_CONTOURS
         else self.raster_radio).setChecked(True)
        self._mode_changed()

    def _load(self, config: Dict, resolved, project) -> None:
        raster_ids = list(getattr(resolved, "raster_layer_ids", None) or []) \
            or list(config.get("raster_layer_ids") or [])
        contours = list(getattr(resolved, "contour_layers", None) or []) \
            or list(config.get("contour_layers") or [])
        mode = int(config.get("mode") or 0)
        if mode not in (MODE_RASTER, MODE_CONTOURS):
            mode = MODE_CONTOURS if contours and not raster_ids else MODE_RASTER
        (self.contour_radio if mode == MODE_CONTOURS
         else self.raster_radio).setChecked(True)
        self._set_raster(project.mapLayer(raster_ids[0]) if raster_ids else None)
        self.raster_band.setValue(int(config.get("raster_band") or 1))
        for slot, entry in zip(self.slots, contours):
            layer = project.mapLayer(entry.get("layer_id") or "")
            slot.set_layer(layer, entry.get("depth_field") or "")
        for slot in self.slots[len(contours):]:
            slot.set_layer(None)
        self.search_radius.setValue(
            float(config.get("contour_search_radius_m") or 500.0))

    def _set_raster(self, layer) -> None:
        self._raster = layer
        self.raster_label.setText(layer.name() if layer is not None else "—")
        self.raster_label.setToolTip(layer.source() if layer is not None else "")
        self.raster_clear.setEnabled(layer is not None)
        if hasattr(self, "ok_button"):
            self._slots_changed()

    def _slots_changed(self) -> None:
        self._apply_filter()
        self._sync_ok()

    # -- left pane ----------------------------------------------------------------
    def _populate(self, project) -> None:
        self.layer_tree.clear()
        for layer in project.mapLayers().values():
            if isinstance(layer, QgsRasterLayer) and layer.isValid():
                kind = MODE_RASTER
            elif (isinstance(layer, QgsVectorLayer) and layer.isValid()
                  and geometry_kind(layer) == GEOM_LINE):
                kind = MODE_CONTOURS
            else:
                continue
            item = QTreeWidgetItem([layer.name(),
                                    layer_group_path(project, layer)])
            item.setData(0, ITEM_DATA_USER_ROLE, layer.id())
            item.setData(1, ITEM_DATA_USER_ROLE, kind)
            item.setToolTip(0, layer.source())
            self.layer_tree.addTopLevelItem(item)
            self._layers[layer.id()] = layer
        for column in range(2):
            self.layer_tree.resizeColumnToContents(column)

    def _in_use(self) -> set:
        ids = set()
        if self.mode() == MODE_RASTER and self._raster is not None:
            ids.add(self._raster.id())
        if self.mode() == MODE_CONTOURS:
            ids.update(s.layer.id() for s in self.slots if s.layer is not None)
        return ids

    def _apply_filter(self, *_args) -> None:
        mode = self.mode()
        text = self.search_edit.text()
        used = self._in_use()
        shown = total = 0
        for index in range(self.layer_tree.topLevelItemCount()):
            item = self.layer_tree.topLevelItem(index)
            right_kind = item.data(1, ITEM_DATA_USER_ROLE) == mode
            total += int(right_kind)
            visible = (right_kind
                       and item.data(0, ITEM_DATA_USER_ROLE) not in used
                       and matches_filter(item.text(0), item.text(1), text,
                                          "", ""))
            item.setHidden(not visible)
            shown += int(visible)
        self.count_label.setText(f"{shown} of {total}")

    def _mode_changed(self) -> None:
        mode = self.mode()
        self.pages.setCurrentIndex(0 if mode == MODE_RASTER else 1)
        self.left_title.setText("<b>Project raster layers</b>"
                                if mode == MODE_RASTER
                                else "<b>Project line layers (contours)</b>")
        self._apply_filter()
        self._sync_ok()

    def _add_selected(self) -> None:
        layers = [self._layers.get(item.data(0, ITEM_DATA_USER_ROLE))
                  for item in self.layer_tree.selectedItems()
                  if not item.isHidden()]
        self.use_layers([l for l in layers if l is not None])

    def use_layers(self, layers) -> None:
        """Put layers into the right pane: a raster replaces the raster; line
        layers fill the empty contour slots in order."""
        if not layers:
            return
        if self.mode() == MODE_RASTER:
            self._set_raster(layers[0])
            if len(layers) > 1:
                self.problem_label.setText(
                    "A raster source uses one layer — the first selected "
                    "was used.")
        else:
            left_over = 0
            for layer in layers:
                slot = next((s for s in self.slots if s.layer is None), None)
                if slot is None:
                    left_over += 1
                    continue
                slot.set_layer(layer)
            if left_over:
                self.problem_label.setText(
                    "Both contour slots are in use — clear one to add "
                    "another layer.")
        self._apply_filter()
        self._sync_ok()

    # -- validation / result -------------------------------------------------------
    def problems(self) -> List[str]:
        if self.mode() == MODE_RASTER:
            return [] if self._raster is not None else ["Add a raster layer."]
        used = [s for s in self.slots if s.layer is not None]
        if not used:
            return ["Add at least one contour layer."]
        out = []
        for number, slot in enumerate(self.slots, start=1):
            if slot.layer is not None and not (slot.field.currentField() or ""):
                out.append(f"Pick the depth field for contour layer {number}.")
        return out

    def _sync_ok(self, *_args) -> None:
        problems = self.problems()
        self.ok_button.setEnabled(not problems)
        if problems:
            self.problem_label.setText(" ".join(problems))
            self.problem_label.setStyleSheet(ui_helpers.status_style("warn"))
        else:
            self.problem_label.setText("")
            self.problem_label.setStyleSheet("")

    def _accept(self) -> None:
        problems = self.problems()
        if problems:
            QMessageBox.warning(self, "Bathymetry source", "\n".join(problems))
            return
        self.accept()

    def result_row(self) -> Dict:
        """The bathymetry input row (existing ids and fields kept)."""
        mode = self.mode()
        config: Dict = {"mode": mode, "raster_layer_ids": [], "raster_band": 1,
                        "contour_layers": [], "contour_search_radius_m": 0.0,
                        "auto_resample": True}
        primary = None
        if mode == MODE_RASTER and self._raster is not None:
            primary = self._raster
            config["raster_layer_ids"] = [self._raster.id()]
            # Sources ride along so a removed-and-re-added layer (new id)
            # is found again instead of silently dropping the bathymetry.
            config["raster_sources"] = [self._raster.source()]
            config["raster_band"] = self.raster_band.value()
        if mode == MODE_CONTOURS:
            for slot in self.slots:
                if slot.layer is None:
                    continue
                primary = primary or slot.layer
                config["contour_layers"].append({
                    "layer_id": slot.layer.id(),
                    "source": slot.layer.source(),
                    "depth_field": slot.field.currentField() or "",
                })
            config["contour_search_radius_m"] = self.search_radius.value()
        row = dict(self._row)
        row.update({
            "role": schema.INPUT_ROLE_BATHY,
            "layer_name": primary.name() if primary is not None else "",
            "layer_source": primary.source() if primary is not None else "",
            "layer_id_hint": primary.id() if primary is not None else "",
            "config_json": json.dumps(config),
            "originator": self.originator.text().strip(),
            "revision": self.revision.text().strip(),
            "status": self.status.currentText(),
            "quality": self.quality.currentText(),
            "notes": self.notes.text().strip(),
        })
        row.setdefault("received_utc", schema.utc_now_iso())
        return row
