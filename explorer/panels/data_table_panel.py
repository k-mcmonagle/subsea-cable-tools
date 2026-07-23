# -*- coding: utf-8 -*-
"""Data table panel: a filtered, virtual view of the loaded dataset."""

from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    EDIT_TRIGGER_DOUBLE_CLICKED,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
    qt_exec,
)
from ..dataset_model import LayDatasetTableModel


class DataTablePanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.model = LayDatasetTableModel(parent=self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        top = QHBoxLayout()
        top.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("All sources", None)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        top.addWidget(self.source_combo, 1)
        self.count_label = QLabel("0 records")
        top.addWidget(self.count_label)
        layout.addLayout(top)

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.view.setSelectionMode(SELECTION_MODE_SINGLE)
        self.view.setEditTriggers(EDIT_TRIGGER_DOUBLE_CLICKED)
        self.view.setAlternatingRowColors(True)
        self.view.setSortingEnabled(True)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.selectionModel().selectionChanged.connect(self._on_row_selected)
        self.view.doubleClicked.connect(self._on_double_clicked)
        custom_policy = getattr(getattr(Qt, "ContextMenuPolicy", Qt), "CustomContextMenu")
        self.view.setContextMenuPolicy(custom_policy)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.view, 1)

    def set_dataset(self, dataset) -> None:
        self.model.set_dataset(dataset)
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("All sources", None)
        if dataset is not None and dataset.source_field is not None:
            for source in dataset.sources():
                label = source if source else "(blank)"
                self.source_combo.addItem(label, source)
        self.source_combo.blockSignals(False)
        self._update_count()

    def select_source_row(self, source_row: int) -> None:
        view_row = self.model.view_row_for_source(source_row)
        if view_row is None:
            return
        self.view.selectRow(view_row)
        self.view.scrollTo(self.model.index(view_row, 0))

    def _on_source_changed(self) -> None:
        self.model.set_source_filter(self.source_combo.currentData())
        self._update_count()

    def _update_count(self) -> None:
        self.count_label.setText(f"{self.model.rowCount()} records")

    def _on_row_selected(self, *_args) -> None:
        indexes = self.view.selectionModel().selectedRows()
        if not indexes:
            return
        source_row = self.model.source_row(indexes[0].row())
        if source_row is not None:
            self.controller.highlight_record(source_row, from_table=True)

    def _on_double_clicked(self, index) -> None:
        source_row = self.model.source_row(index.row())
        if source_row is not None:
            self.controller.go_to_record(source_row)

    def _on_context_menu(self, pos) -> None:
        index = self.view.indexAt(pos)
        if not index.isValid():
            return
        source_row = self.model.source_row(index.row())
        if source_row is None:
            return
        menu = QMenu(self.view)
        action = menu.addAction("Go to (map + plots)")
        chosen = qt_exec(menu, self.view.viewport().mapToGlobal(pos))
        if chosen is action:
            self.controller.go_to_record(source_row)
