# -*- coding: utf-8 -*-
"""A virtual table model over a :class:`LayDataset`.

Backed directly by the dataset's column arrays (no per-cell copies), so it stays
responsive on large raw datasets. Supports an optional source-file filter that
maps visible rows to underlying dataset row indices via a small index array.
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt


class LayDatasetTableModel(QAbstractTableModel):
    def __init__(self, dataset=None, parent=None):
        super().__init__(parent)
        self._dataset = None
        self._fields: List[str] = []
        self._visible = np.empty(0, dtype=np.int64)
        self._sort_column = -1
        self._sort_order = getattr(getattr(Qt, "SortOrder", Qt), "AscendingOrder")
        if dataset is not None:
            self.set_dataset(dataset)

    def set_dataset(self, dataset) -> None:
        self.beginResetModel()
        self._dataset = dataset
        self._fields = dataset.field_names if dataset is not None else []
        self._visible = (
            np.arange(dataset.row_count, dtype=np.int64) if dataset is not None else np.empty(0, dtype=np.int64)
        )
        if dataset is not None and 0 <= self._sort_column < len(self._fields):
            self._sort_visible(self._sort_column, self._sort_order)
        self.endResetModel()

    def set_source_filter(self, source: Optional[str]) -> None:
        if self._dataset is None:
            return
        self.beginResetModel()
        if not source:
            self._visible = np.arange(self._dataset.row_count, dtype=np.int64)
        else:
            arr = self._dataset.source_array
            mask = np.array([("" if s is None else str(s)) == source for s in arr], dtype=bool)
            self._visible = np.nonzero(mask)[0]
        if 0 <= self._sort_column < len(self._fields):
            self._sort_visible(self._sort_column, self._sort_order)
        self.endResetModel()

    def sort(self, column: int, order) -> None:
        if self._dataset is None or not (0 <= column < len(self._fields)):
            return
        self.beginResetModel()
        self._sort_column = int(column)
        self._sort_order = order
        self._sort_visible(self._sort_column, self._sort_order)
        self.endResetModel()

    def _sort_visible(self, column: int, order) -> None:
        if self._visible.size == 0:
            return
        name = self._fields[column]
        source_rows = self._visible
        descending = order == getattr(getattr(Qt, "SortOrder", Qt), "DescendingOrder")
        if self._dataset.is_numeric_field(name):
            values = self._dataset.numeric(name)[source_rows]
            # Use +/-inf sentinels so NaN values sort to the end in both orders.
            sentinel = -np.inf if descending else np.inf
            keys = np.where(np.isfinite(values), values, sentinel)
            idx = np.argsort(keys, kind="stable")
            if descending:
                idx = idx[::-1]
        else:
            raw = self._dataset.raw(name)[source_rows]
            keys = np.array(["" if v is None else str(v).lower() for v in raw], dtype=object)
            idx = np.argsort(keys, kind="stable")
            if descending:
                idx = idx[::-1]
        self._visible = source_rows[idx]

    # -- Qt model API ------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else int(self._visible.size)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._fields)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self._dataset is None:
            return None
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        source_row = int(self._visible[index.row()])
        value = self._dataset.raw(self._fields[index.column()])[source_row]
        return "" if value is None else str(value)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._fields[section] if 0 <= section < len(self._fields) else None
        if 0 <= section < self._visible.size:
            return str(int(self._dataset.fids[int(self._visible[section])]))
        return None

    # -- helpers -----------------------------------------------------------
    def source_row(self, view_row: int) -> Optional[int]:
        if 0 <= view_row < self._visible.size:
            return int(self._visible[view_row])
        return None

    def view_row_for_source(self, source_row: int) -> Optional[int]:
        matches = np.nonzero(self._visible == source_row)[0]
        return int(matches[0]) if matches.size else None
