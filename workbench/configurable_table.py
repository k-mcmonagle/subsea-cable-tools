# -*- coding: utf-8 -*-
"""Small reusable table with persistent, user-configurable columns."""

from __future__ import annotations

import hashlib
from typing import Dict, Iterable, List, Sequence, Tuple

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import QMenu, QTableWidget

from ..qgis_compat import CONTEXT_MENU_POLICY_CUSTOM, qt_exec


# label, stable field key, visible in the default Essentials layout
ColumnSpec = Tuple[str, str, bool]


class ConfigurableTable(QTableWidget):
    """QTableWidget whose order, widths and visibility persist per schema."""

    def __init__(self, settings_name: str, parent=None):
        super().__init__(0, 0, parent)
        self._settings_name = settings_name
        self._columns: List[ColumnSpec] = []
        self._presets: Dict[str, set] = {}
        self._state_key = ""
        self._muted = False
        header = self.horizontalHeader()
        header.setSectionsMovable(True)
        header.setContextMenuPolicy(CONTEXT_MENU_POLICY_CUSTOM)
        header.customContextMenuRequested.connect(self._header_menu)
        header.sectionMoved.connect(self._header_changed)
        header.sectionResized.connect(self._header_changed)

    def configure_columns(self, columns: Sequence[ColumnSpec],
                          presets: Dict[str, Iterable[str]] = None) -> None:
        normalised = [(str(label), str(key), bool(visible))
                      for label, key, visible in columns]
        signature = "\x1f".join(key for _label, key, _visible in normalised)
        digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12]
        state_key = (f"SubseaCableTools/workbench/{self._settings_name}/"
                     f"header_{digest}")
        if normalised == self._columns and state_key == self._state_key:
            return
        self._columns = normalised
        self._presets = {name: set(keys) for name, keys in (presets or {}).items()}
        self._state_key = state_key
        header = self.horizontalHeader()
        self._muted = True
        header.blockSignals(True)
        try:
            self.setColumnCount(len(normalised))
            self.setHorizontalHeaderLabels([label for label, _key, _visible in normalised])
            saved = QSettings().value(state_key)
            restored = False
            if saved is not None:
                try:
                    restored = bool(header.restoreState(saved))
                except (TypeError, ValueError):
                    restored = False
            if not restored:
                for column, (_label, _key, visible) in enumerate(normalised):
                    self.setColumnHidden(column, not visible)
                self.resizeColumnsToContents()
                for column in range(self.columnCount()):
                    self.setColumnWidth(column, min(max(self.columnWidth(column), 68), 280))
        finally:
            header.blockSignals(False)
            self._muted = False

    def field_key(self, logical_column: int) -> str:
        if 0 <= logical_column < len(self._columns):
            return self._columns[logical_column][1]
        return ""

    def column_for_key(self, key: str) -> int:
        return next((i for i, (_label, field, _visible) in enumerate(self._columns)
                     if field == key), -1)

    def _header_changed(self, *_args) -> None:
        if not self._muted and self._state_key:
            QSettings().setValue(self._state_key, self.horizontalHeader().saveState())

    def _header_menu(self, pos) -> None:
        menu = QMenu(self)
        for column, (label, _key, _visible) in enumerate(self._columns):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not self.isColumnHidden(column))
            action.toggled.connect(
                lambda checked, c=column: self._set_column_visible(c, checked))
        if self._presets:
            menu.addSeparator()
            preset_menu = menu.addMenu("Column presets")
            for name, keys in self._presets.items():
                preset_menu.addAction(
                    name, lambda _checked=False, selected=set(keys): self._apply_preset(selected))
        menu.addSeparator()
        menu.addAction("Show all columns", lambda: self._apply_preset(
            {key for _label, key, _visible in self._columns}))
        menu.addAction("Size columns to contents", self._size_columns)
        menu.addAction("Reset column layout", self._reset_layout)
        qt_exec(menu, self.horizontalHeader().mapToGlobal(pos))

    def _set_column_visible(self, column: int, visible: bool) -> None:
        self.setColumnHidden(column, not visible)
        self._header_changed()

    def _apply_preset(self, visible_keys: set) -> None:
        self._muted = True
        try:
            for column, (_label, key, _visible) in enumerate(self._columns):
                self.setColumnHidden(column, key not in visible_keys)
            self._size_columns(save=False)
        finally:
            self._muted = False
        self._header_changed()

    def _size_columns(self, save: bool = True) -> None:
        self.resizeColumnsToContents()
        for column in range(self.columnCount()):
            self.setColumnWidth(column, min(max(self.columnWidth(column), 68), 320))
        if save:
            self._header_changed()

    def _reset_layout(self) -> None:
        if self._state_key:
            QSettings().remove(self._state_key)
        # Rebuild with an intentionally different cached value so defaults run.
        columns, presets = list(self._columns), dict(self._presets)
        self._columns = []
        self._state_key = ""
        self.configure_columns(columns, presets)

