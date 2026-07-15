# -*- coding: utf-8 -*-
"""A small modal dialog for choosing which project layers to load.

Lists every vector-like layer in the current QGIS project with a checkbox and
returns the ids the user ticked. Used by the Cable Lay Data Explorer to load
several layers at once into its dataset registry.
"""

from __future__ import annotations

from typing import List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)
from qgis.core import QgsProject

from ..qgis_compat import qt_exec, DIALOG_ACCEPTED, BUTTON_BOX_OK, BUTTON_BOX_CANCEL

_CHECK_STATE = getattr(Qt, "CheckState", Qt)
_CHECKED = getattr(_CHECK_STATE, "Checked")
_UNCHECKED = getattr(_CHECK_STATE, "Unchecked")
_ITEM_FLAG = getattr(Qt, "ItemFlag", Qt)
_FLAG_CHECKABLE = getattr(_ITEM_FLAG, "ItemIsUserCheckable")
_FLAG_ENABLED = getattr(_ITEM_FLAG, "ItemIsEnabled")
_FLAG_SELECTABLE = getattr(_ITEM_FLAG, "ItemIsSelectable")
_USER_ROLE = getattr(getattr(Qt, "ItemDataRole", Qt), "UserRole")


class LayerSelectDialog(QDialog):
    def __init__(self, parent=None, preselected: Optional[List[str]] = None):
        super().__init__(parent)
        self.setWindowTitle("Choose layers to load")
        self.resize(420, 460)
        preselected = set(preselected or [])

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Tick the layers to load into the Explorer:"))

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter layers\u2026")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._list = QListWidget()
        layout.addWidget(self._list, 1)

        for layer in QgsProject.instance().mapLayers().values():
            if not hasattr(layer, "fields"):
                continue
            item = QListWidgetItem(layer.name())
            item.setData(_USER_ROLE, layer.id())
            item.setFlags(_FLAG_ENABLED | _FLAG_SELECTABLE | _FLAG_CHECKABLE)
            item.setCheckState(_CHECKED if layer.id() in preselected else _UNCHECKED)
            self._list.addItem(item)

        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def selected_layer_ids(self) -> List[str]:
        ids: List[str] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == _CHECKED:
                ids.append(item.data(_USER_ROLE))
        return ids

    def exec_qt(self) -> bool:
        """Run the dialog modally; return True if accepted (Qt5/Qt6 safe)."""
        return qt_exec(self) == DIALOG_ACCEPTED
