# -*- coding: utf-8 -*-
"""Small dialog: take a feature's cross-section from a DXF drawing."""

from __future__ import annotations

from typing import List, Optional, Tuple

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK
from . import dxf_io


def fill_layers(path: str, combo: QComboBox, current: str = "") -> str:
    """List the DXF's layers that hold lines in ``combo``; returns an error text or ''."""
    combo.clear()
    if not path:
        return ""
    try:
        layers = dxf_io.read_polylines(path)
    except (OSError, ValueError) as exc:
        return str(exc)
    for name in sorted(layers):
        count = sum(len(p) for p in layers[name])
        combo.addItem(f"{name} ({count} points)", name)
    index = combo.findData(current)
    combo.setCurrentIndex(max(index, 0))
    return ""


class DxfShapeDialog(QDialog):
    """Pick a DXF layer holding a feature cross-section (along the route, height up)."""

    def __init__(self, parent=None, path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Feature shape from DXF")
        layout = QVBoxLayout(self)
        note = QLabel("The cross-section along the route: X along the route, Y up. Heights are taken above the "
                      "straight line between the shape's two ends (the seabed it sits on), and offsets from its "
                      "middle, which goes at the feature's KP.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self.path = QLineEdit(path)
        browse = QToolButton()
        browse.setText("…")
        browse.clicked.connect(self._browse)
        row_layout.addWidget(self.path, 1)
        row_layout.addWidget(browse)
        form.addRow("DXF file", row)
        self.layer = QComboBox()
        form.addRow("Layer", self.layer)
        self.scale = QDoubleSpinBox()
        self.scale.setRange(0.0001, 10000.0)
        self.scale.setDecimals(4)
        self.scale.setValue(1.0)
        self.scale.setToolTip("Metres per drawing unit (1 for a drawing in metres, 0.001 for millimetres).")
        form.addRow("Metres per unit", self.scale)
        self.ve = QDoubleSpinBox()
        self.ve.setRange(0.01, 1000.0)
        self.ve.setValue(1.0)
        self.ve.setToolTip("Vertical exaggeration of the drawing (removed from the heights).")
        form.addRow("Vertical exaggeration", self.ve)
        layout.addLayout(form)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color: #d32f2f;")
        layout.addWidget(self.message)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.points: Optional[List[Tuple[float, float]]] = None
        self.path.editingFinished.connect(self._load)
        if path:
            self._load()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "DXF drawing", self.path.text(), "DXF (*.dxf)")
        if path:
            self.path.setText(path)
            self._load()

    def _load(self) -> None:
        self.message.setText(fill_layers(self.path.text().strip(), self.layer))

    def _accept(self) -> None:
        try:
            self.points = dxf_io.shape_from_dxf(self.path.text().strip(), self.layer.currentData() or "",
                                                self.scale.value(), self.ve.value())
        except (OSError, ValueError) as exc:
            self.message.setText(str(exc))
            return
        self.accept()
