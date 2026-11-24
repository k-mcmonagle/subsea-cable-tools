# -*- coding: utf-8 -*-
"""Edit the burial plan's scoring scale."""

from qgis.gui import QgsColorButton
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK, HEADER_RESIZE_MODE_STRETCH
from . import schema
from .risk_scale import DEFAULT_SCALE, RiskScale


class RiskScaleDialog(QDialog):
    def __init__(self, scale, used, parent=None):
        super().__init__(parent)
        self.scale = scale
        self.used = used
        self.setWindowTitle("Plan risk scoring scale")
        self.resize(470, 350)
        layout = QVBoxLayout(self)
        hint = QLabel("Give each level a name, score and colour. Higher scores take priority "
                      "when several criteria match. Checks and manual hazards share this scale.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Risk level", "Score", "Colour"])
        self.table.horizontalHeader().setSectionResizeMode(0, HEADER_RESIZE_MODE_STRETCH)
        self.table.verticalHeader().hide()
        layout.addWidget(self.table)
        for row in scale.rows():
            self._add(row)
        bar = QHBoxLayout()
        for title, slot in (("Add level", self._add_level), ("Remove level", self._remove),
                            ("Restore defaults", self._restore)):
            button = QPushButton(title)
            button.clicked.connect(slot)
            bar.addWidget(button)
        layout.addLayout(bar)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add(self, data):
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = QTableWidgetItem(data["label"])
        item.setData(Qt.ItemDataRole.UserRole, data["id"])
        self.table.setItem(row, 0, item)
        self.table.setItem(row, 1, QTableWidgetItem(str(data["score"])))
        color = QgsColorButton(self.table)
        color.setColor(QColor(data["color"]))
        self.table.setCellWidget(row, 2, color)

    def _add_level(self):
        scores = [self.table.item(row, 1).text().strip() for row in range(self.table.rowCount())]
        score = max((int(s) for s in scores if s.isascii() and s.isdecimal()), default=0) + 1
        self._add(dict(id=schema.new_id(), label="", score=score, color="#8055a0"))
        item = self.table.item(self.table.rowCount() - 1, 0)
        self.table.setCurrentItem(item)
        self.table.editItem(item)

    def _remove(self):
        row = self.table.currentRow()
        if row >= 0:
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) in self.used:
                QMessageBox.information(self, self.windowTitle(),
                                        "A check or hazard uses this level. Reassign it before removal.")
                return
            self.table.removeRow(row)

    def _restore(self):
        if self.used - set(DEFAULT_SCALE.levels):
            QMessageBox.information(self, self.windowTitle(),
                                    "Reassign the custom levels used by checks or hazards before restoring defaults.")
            return
        self.table.setRowCount(0)
        for row in DEFAULT_SCALE.rows():
            self._add(row)

    def _accept(self):
        rows = []
        try:
            for i in range(self.table.rowCount()):
                name = self.table.item(i, 0)
                score = self.table.item(i, 1).text().strip()
                if not score.isascii() or not score.isdecimal() or int(score) > 2147483647:
                    raise ValueError(f"Enter a whole-number score from 0 to 2147483647 in row {i + 1}.")
                rows.append(dict(id=name.data(Qt.ItemDataRole.UserRole), label=name.text(),
                                 score=int(score), color=self.table.cellWidget(i, 2).color().name()))
            self.scale = RiskScale(rows)
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.accept()
