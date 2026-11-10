# -*- coding: utf-8 -*-
"""Review and repair original-file and live-layer links in the Ground Model."""
from __future__ import annotations

from copy import deepcopy
import os

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QMessageBox, QPushButton, QTableWidgetItem, QVBoxLayout,
)

from ..qgis_compat import (
    BUTTON_BOX_CANCEL, BUTTON_BOX_OK, EDIT_TRIGGER_NONE, HEADER_RESIZE_MODE_INTERACTIVE,
    SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_SINGLE,
)
from ..table_widgets import ResizableTableWidget
from . import numeric_datasets, source_links


class GroundSourcesDialog(QDialog):
    def __init__(self, model, parent=None, owner=None):
        super().__init__(parent)
        self.model, self._store = model, model.store
        campaigns, datasets = self._store.list_cpt_campaigns(), self._store.list_ground_datasets()
        self.originals = {("cpt", row["campaign_id"]): deepcopy(row) for row in campaigns}
        self.originals.update({("numeric", row["dataset_id"]): deepcopy(row) for row in datasets})
        self.links = source_links.inventory(campaigns, datasets)
        self.pending = {}
        self.setWindowTitle("Ground Model sources")
        self.resize(1150, 620)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "CPT readings and positions, numeric measurements, soil units and imported DXF horizons are saved "
            "in the GeoPackage. Original files are needed to reload measurements; KP-range and soil-polygon "
            "layers are read live. Repairing links here keeps the stored measurements and import choices. "
            "Use Reload separately when you want to read changed measurements.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = ResizableTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Import / dataset", "Source", "Used for", "Status", "Location"])
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.table.horizontalHeader().setSectionResizeMode(HEADER_RESIZE_MODE_INTERACTIVE)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selected)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.browse_button = QPushButton("Locate file…")
        self.layer_button = QPushButton("Choose layer…")
        self.folder_button = QPushButton("Find missing files in folder…")
        self.browse_button.clicked.connect(self._browse)
        self.layer_button.clicked.connect(self._choose_layer)
        self.folder_button.clicked.connect(self._folder)
        for button in (self.browse_button, self.layer_button, self.folder_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.details)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.button(BUTTON_BOX_OK).setText("Save links")
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        QgsProject.instance().aboutToBeCleared.connect(self.reject)
        model.planChanged.connect(self._context_changed)
        self._render()
        selected = next((i for i, link in enumerate(self.links)
                         if link.kind != "stored" and (owner is None or link.owner_key == owner)
                         and source_links.link_status(link) not in ("Available", "Stored")), None)
        if selected is None:
            selected = next((i for i, link in enumerate(self.links) if owner and link.owner_key == owner), 0)
        if self.links:
            self.table.selectRow(selected)
        self._selected()

    def _context_changed(self):
        if self.model.store is not self._store:
            self.reject()

    def _render(self):
        self.table.setRowCount(len(self.links))
        for row, link in enumerate(self.links):
            value = self.pending.get(link.key, link.value)
            status = source_links.link_status(link, value)
            if link.key in self.pending:
                status += " (pending)"
            texts = (link.owner_name, link.role, link.usage, status, source_links.link_location(link, value))
            for column, text in enumerate(texts):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(row, column, item)
        for column, width in enumerate((200, 220, 160, 155)):
            self.table.setColumnWidth(column, width)
        self._selected()

    def selected_link(self):
        row = self.table.currentRow()
        return self.links[row] if 0 <= row < len(self.links) else None

    def _selected(self):
        link = self.selected_link()
        self.browse_button.setEnabled(link is not None and link.kind != "stored")
        self.layer_button.setEnabled(link is not None and link.kind == "layer")
        if link is None:
            self.details.setText("No external source is selected.")
            return
        value = self.pending.get(link.key, link.value)
        note = ("Stored measurements remain available when an original file is missing."
                if link.usage == "Needed to reload" else
                "This layer controls live placement. Repairing its link can restore the plotted KP ranges."
                if link.kind == "layer" else "This data is already stored; its original import file is not needed to display it.")
        if link.kind == "file" and link.role != "AGS file":
            config = self.originals[link.owner_key].get("config") or {}
            choices = (config.get("positions") if link.owner_kind == "cpt" else config.get("measurements")) or {}
            source = choices if link.owner_kind == "cpt" else choices.get("source") or {}
            if source.get("sheet"):
                note += f"\nSaved worksheet: {source['sheet']}; header row: {choices.get('header_row') or 1}."
        self.details.setText(f"{link.owner_name} — {link.role}\n{source_links.link_location(link, value)}\n{note}")

    def stage(self, link, value):
        source_links.validate_link(link, value, self.originals[link.owner_key], self.model)
        self.pending[link.key] = deepcopy(value)
        self._render()
        self.message.setText(f"{len(self.pending)} link(s) ready to save. Stored measurements will be retained.")

    def _replacement_file(self, link, path):
        if link.kind == "file":
            return path
        old = self.pending.get(link.key, link.value) or {}
        if old.get("layer_provider") not in (None, "", "ogr"):
            raise ValueError("Add the replacement to QGIS and use Choose layer for this provider.")
        _previous, sep, options = str(old.get("layer_source") or "").partition("|")
        uri = path + (sep + options if sep else "")
        layer = QgsVectorLayer(uri, old.get("layer_name") or "Replacement", "ogr")
        if not layer.isValid():
            raise ValueError("The saved table could not be opened in this file. Add the desired layer to QGIS "
                             "and use Choose layer instead.")
        ref = numeric_datasets.layer_ref(layer)
        ref["layer_id_hint"] = ""  # a private validation layer is not a project layer
        return ref

    def _browse(self):
        link = self.selected_link()
        if link is None or link.kind == "stored":
            return
        current = source_links.link_location(link, self.pending.get(link.key, link.value)).split("|", 1)[0]
        path, _filter = QFileDialog.getOpenFileName(self, f"Locate {link.role.lower()}", current, "All files (*)")
        if not path:
            return
        try:
            self.stage(link, self._replacement_file(link, path))
        except (ValueError, OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "Repair source link", str(exc))

    def _choose_layer(self):
        link = self.selected_link()
        if link is None or link.kind != "layer":
            return
        layers = sorted((layer for layer in QgsProject.instance().mapLayers().values()
                         if isinstance(layer, QgsVectorLayer) and layer.isValid()), key=lambda layer: layer.name().casefold())
        labels = [f"{i + 1}. {layer.name()} — {layer.source().split('|', 1)[0]}" for i, layer in enumerate(layers)]
        if not labels:
            QMessageBox.information(self, "Repair source link", "Add the replacement layer or table to QGIS first.")
            return
        selected, ok = QInputDialog.getItem(self, "Choose replacement layer", link.role, labels, 0, False)
        if ok:
            try:
                self.stage(link, numeric_datasets.layer_ref(layers[labels.index(selected)]))
            except (ValueError, OSError, RuntimeError) as exc:
                QMessageBox.warning(self, "Repair source link", str(exc))

    def _folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Find missing source files in folder")
        if not folder:
            return
        found, problems = 0, []
        for link in self.links:
            if link.kind == "stored" or link.key in self.pending or source_links.link_status(link) == "Available":
                continue
            location = source_links.link_location(link).split("|", 1)[0]
            path = os.path.join(folder, os.path.basename(location))
            if not os.path.isfile(path):
                continue
            try:
                value = self._replacement_file(link, path)
                source_links.validate_link(link, value, self.originals[link.owner_key], self.model)
                self.pending[link.key] = value
                found += 1
            except (ValueError, OSError, RuntimeError) as exc:
                problems.append(f"{link.owner_name}: {exc}")
        self._render()
        self.message.setText(f"{found} link(s) found by filename; review them before saving. " + "\n".join(problems[:5]))

    def _save(self):
        if self.model.store is not self._store:
            self.reject()
            return
        changes = [(link, self.pending[link.key]) for link in self.links if link.key in self.pending]
        try:
            if source_links.save_links(self.model, self.originals, changes):
                self.accept()
        except (ValueError, OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "Repair source links", str(exc))


def open_sources(model, parent=None, owner=None):
    dialog = GroundSourcesDialog(model, parent, owner)
    from ..qgis_compat import qt_exec
    try:
        return qt_exec(dialog)
    finally:
        dialog.deleteLater()
