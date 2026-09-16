# -*- coding: utf-8 -*-
"""Editor for the user's cable-type colour palette.

Lists every cable type the workbench knows about (from the registered RPL
line layers, plus anything already in the palette), shows the colour each one
currently draws with and where that colour comes from, and lets the user
override any of them. Applying writes the palette to QGIS settings (global,
so it follows the user across projects) and restyles the workbench layers in
the current project, re-saving each layer's default style in the GeoPackage.

Types the user has not overridden keep the built-in colour, so an empty
palette behaves exactly as before.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QColorDialog, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from ..qgis_compat import DIALOG_ACCEPTED, MESSAGEBOX_YES, qt_exec
from . import layer_style

SOURCE_CUSTOM = "Custom"
SOURCE_STANDARD = "Standard"
SOURCE_AUTOMATIC = "Automatic"


def cable_types_in_store(store) -> List[str]:
    """Every distinct CableType value across the registry's RPL line layers."""
    values = set()
    if store is None:
        return []
    try:
        rpls = store.list_rpls()
    except Exception:
        return []
    for rpl in rpls:
        name = rpl.get("lines_layer") or ""
        if not name:
            continue
        try:
            from .rpl_summary import open_rpl_layer

            layer = open_rpl_layer(store, name)
        except Exception:
            continue
        if layer is None or not layer.isValid():
            continue
        values.update(
            layer_style.unique_field_values(layer, layer_style.CABLE_TYPE_FIELD))
    return sorted(values)


class CableTypeColourDialog(QDialog):
    """Edit the cable-type palette and apply it to the project's layers."""

    def __init__(self, store=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cable type colours")
        self.resize(520, 460)
        self._store = store
        # token -> hex; only overridden types are kept, so a type removed from
        # the palette falls straight back to its built-in colour.
        self._palette: Dict[str, str] = layer_style.user_cable_type_colours()
        # display label per token, so "SAH" shows as the user typed it.
        self._labels: Dict[str, str] = {}

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Cable types are coloured by your palette first, then by the "
            "built-in armour colours, then by a colour derived from the type "
            "name. Colours here are yours, not the project's: they apply to "
            "every project you open.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Cable type", "Colour", "Source"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setToolTip("Double-click a row to choose its colour.")
        self.table.cellDoubleClicked.connect(lambda *_a: self._choose_colour())
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        choose_btn = QPushButton("Choose colour...")
        choose_btn.clicked.connect(self._choose_colour)
        add_btn = QPushButton("Add cable type...")
        add_btn.setToolTip(
            "Add a type that no RPL uses yet, so it is already coloured when "
            "one does.")
        add_btn.clicked.connect(self._add_type)
        standard_btn = QPushButton("Use standard colour")
        standard_btn.setToolTip("Drop the override on the selected cable type.")
        standard_btn.clicked.connect(self._use_standard)
        reset_btn = QPushButton("Reset all")
        reset_btn.setToolTip("Drop every override and go back to the built-in colours.")
        reset_btn.clicked.connect(self._reset_all)
        for button in (choose_btn, add_btn, standard_btn, reset_btn):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#52606d;")
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._reload()

    # ------------------------------------------------------------- data --
    def _tokens(self) -> List[str]:
        tokens = set(self._palette)
        for value in cable_types_in_store(self._store):
            token = layer_style.normalise_cable_type(value)
            if token:
                tokens.add(token)
                self._labels.setdefault(token, str(value).strip())
        for token in layer_style.KNOWN_CABLE_TYPE_COLOURS:
            tokens.add(token)
        return sorted(tokens)

    def _reload(self) -> None:
        selected = self._selected_token()
        tokens = self._tokens()
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(tokens))
            for row, token in enumerate(tokens):
                colour = layer_style.colour_for_cable_type(token, self._palette)
                if token in self._palette:
                    source = SOURCE_CUSTOM
                elif token in layer_style.KNOWN_CABLE_TYPE_COLOURS:
                    source = SOURCE_STANDARD
                else:
                    source = SOURCE_AUTOMATIC
                name_item = QTableWidgetItem(self._labels.get(token, token))
                name_item.setData(Qt.ItemDataRole.UserRole, token)
                self.table.setItem(row, 0, name_item)
                swatch = QTableWidgetItem(colour)
                swatch.setBackground(QBrush(QColor(colour)))
                swatch.setForeground(QBrush(_readable_text_colour(colour)))
                self.table.setItem(row, 1, swatch)
                self.table.setItem(row, 2, QTableWidgetItem(source))
        finally:
            self.table.setUpdatesEnabled(True)
        if selected:
            self._select_token(selected)
        custom = len(self._palette)
        self.status.setText(
            f"{custom} custom colour{'' if custom == 1 else 's'} in your palette."
            if custom else "No custom colours yet; every type uses its built-in colour.")

    def _selected_token(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _select_token(self, token: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == token:
                self.table.setCurrentCell(row, 0)
                return

    # ---------------------------------------------------------- actions --
    def _choose_colour(self) -> None:
        token = self._selected_token()
        if not token:
            return
        current = QColor(layer_style.colour_for_cable_type(token, self._palette))
        chosen = QColorDialog.getColor(current, self, f"Colour for {token}")
        if not chosen.isValid():
            return
        self._palette[token] = chosen.name().lower()
        self._reload()

    def _add_type(self) -> None:
        text, ok = QInputDialog.getText(self, "Add cable type", "Cable type:")
        if not ok:
            return
        token = layer_style.normalise_cable_type(text)
        if not token:
            QMessageBox.warning(
                self, "Add cable type",
                "A cable type needs at least one letter or digit.")
            return
        self._labels[token] = str(text).strip()
        self._palette.setdefault(
            token, layer_style.standard_colour_for_cable_type(token))
        self._reload()
        self._select_token(token)

    def _use_standard(self) -> None:
        token = self._selected_token()
        if token:
            self._palette.pop(token, None)
            self._reload()

    def _reset_all(self) -> None:
        if not self._palette:
            return
        confirm = QMessageBox.question(
            self, "Reset all", "Drop every custom cable-type colour?")
        if confirm == MESSAGEBOX_YES:
            self._palette.clear()
            self._reload()

    # ------------------------------------------------------------ result --
    def palette(self) -> Dict[str, str]:
        return dict(self._palette)


def _readable_text_colour(hex_colour: str) -> QColor:
    colour = QColor(hex_colour)
    luminance = (0.299 * colour.red() + 0.587 * colour.green()
                 + 0.114 * colour.blue())
    return QColor("#111111") if luminance > 150 else QColor("#ffffff")


def edit_cable_type_colours(store=None, parent=None,
                            gpkg_path: str = "") -> Optional[int]:
    """Open the palette editor; on OK save it and restyle the project layers.

    Returns the number of layers restyled, or None if the user cancelled.
    """
    dialog = CableTypeColourDialog(store, parent)
    if qt_exec(dialog) != DIALOG_ACCEPTED:
        return None
    palette = layer_style.set_user_cable_type_colours(dialog.palette())
    if not gpkg_path and store is not None:
        gpkg_path = getattr(store, "gpkg_path", "") or ""
    return layer_style.restyle_workbench_layers(
        gpkg_path=gpkg_path, overrides=palette)
