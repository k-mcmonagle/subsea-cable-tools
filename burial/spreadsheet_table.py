# -*- coding: utf-8 -*-
"""A QTableWidget with spreadsheet habits.

Copy (Ctrl+C) puts the selection on the clipboard as tab-separated text;
paste (Ctrl+V) fills from the current cell, growing the table through
``add_rows_callback`` when the clipboard has more rows than remain; Delete
clears the selection; Ctrl+D fills the first selected row's values down
the selection; Ctrl+Z / Ctrl+Y undo and redo cell edits made through the
widget (typing, paste, clear, fill). Programmatic rebuilds go through
``rebuilding()`` so they neither enter the undo stack nor emit edits.

Every user edit ends up as a normal ``itemChanged`` signal, so the owner
keeps a single data path; ``editable_columns`` (when set) makes other
columns read-only for the keyboard actions too.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, List, Optional, Sequence, Set, Tuple

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QTableWidget,
                                 QTableWidgetItem)

_KEY = getattr(Qt, "Key", Qt)
_MOD = getattr(Qt, "KeyboardModifier", Qt)
_EDITING = getattr(QAbstractItemView, "State", QAbstractItemView).EditingState


def _has_ctrl(event) -> bool:
    try:
        return bool(event.modifiers() & _MOD.ControlModifier)
    except TypeError:
        return False


class SpreadsheetTable(QTableWidget):
    def __init__(self, rows: int = 0, columns: int = 0, parent=None):
        super().__init__(rows, columns, parent)
        self.add_rows_callback: Optional[Callable[[int], None]] = None
        self.editable_columns: Optional[Set[int]] = None
        self._undo: List[List[Tuple[int, int, str, str]]] = []
        self._redo: List[List[Tuple[int, int, str, str]]] = []
        self._snapshot: dict = {}
        self._rebuilding = 0
        self._applying = 0
        self.itemChanged.connect(self._track_change)

    # -- programmatic rebuilds --------------------------------------------------
    @contextmanager
    def rebuilding(self):
        """Suppress edit tracking while the owner fills the table."""
        self._rebuilding += 1
        self.blockSignals(True)
        try:
            yield
        finally:
            self.blockSignals(False)
            self._rebuilding -= 1
            if self._rebuilding == 0:
                self._resnapshot()

    def _resnapshot(self) -> None:
        self._snapshot = {}
        for r in range(self.rowCount()):
            for c in range(self.columnCount()):
                item = self.item(r, c)
                self._snapshot[(r, c)] = item.text() if item is not None else ""

    def clear_history(self) -> None:
        self._undo.clear()
        self._redo.clear()

    # -- edit tracking ------------------------------------------------------------
    def _track_change(self, item) -> None:
        if self._rebuilding or self._applying:
            return
        key = (item.row(), item.column())
        old = self._snapshot.get(key, "")
        new = item.text()
        self._snapshot[key] = new
        if old == new:
            return
        self._undo.append([(key[0], key[1], old, new)])
        self._redo.clear()

    def _editable(self, row: int, col: int) -> bool:
        if self.editable_columns is not None and col not in self.editable_columns:
            return False
        item = self.item(row, col)
        if item is None:
            return False
        try:
            return bool(item.flags() & Qt.ItemFlag.ItemIsEditable)
        except TypeError:
            return True

    def _apply_batch(self, changes: Sequence[Tuple[int, int, str, str]],
                     redo: bool = True) -> None:
        """Set cells (as one undo step); itemChanged fires per cell so the
        owner's model follows, but the tracker records the batch here."""
        self._applying += 1
        try:
            applied = []
            for row, col, old, new in changes:
                value = new if redo else old
                if row >= self.rowCount() or col >= self.columnCount():
                    continue
                item = self.item(row, col)
                if item is None:
                    item = QTableWidgetItem("")
                    self.setItem(row, col, item)
                if item.text() != value:
                    item.setText(value)  # emits itemChanged → owner model
                self._snapshot[(row, col)] = value
                applied.append((row, col, old, new))
        finally:
            self._applying -= 1
        if applied and redo:
            self._undo.append(list(applied))
            self._redo.clear()

    def amend_last(self, row: int, col: int, text: str) -> None:
        """Owner-side canonicalisation of the edit just made in a cell:
        rewrite the cell (silently) and let the undo entry record the
        canonical value — a rejected edit (text back to the old value)
        leaves no undo entry at all."""
        item = self.item(row, col)
        if item is None:
            return
        self._rebuilding += 1
        self.blockSignals(True)
        try:
            if item.text() != text:
                item.setText(text)
        finally:
            self.blockSignals(False)
            self._rebuilding -= 1
        self._snapshot[(row, col)] = text
        if self._undo:
            last = self._undo[-1]
            if len(last) == 1 and last[0][0] == row and last[0][1] == col:
                old = last[0][2]
                if old == text:
                    self._undo.pop()
                else:
                    self._undo[-1] = [(row, col, old, text)]

    def set_cells(self, changes: Sequence[Tuple[int, int, str]]) -> None:
        """Owner-driven batch edit ``[(row, col, new_text)]`` as one undo step."""
        batch = []
        for row, col, new in changes:
            item = self.item(row, col)
            old = item.text() if item is not None else ""
            if old != new:
                batch.append((row, col, old, new))
        if batch:
            self._apply_batch(batch)

    def undo(self) -> bool:
        if not self._undo:
            return False
        batch = self._undo.pop()
        self._apply_batch(batch, redo=False)
        self._redo.append(batch)
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        batch = self._redo.pop()
        self._applying += 1
        try:
            for row, col, _old, new in batch:
                item = self.item(row, col)
                if item is not None and item.text() != new:
                    item.setText(new)
                self._snapshot[(row, col)] = new
        finally:
            self._applying -= 1
        self._undo.append(batch)
        return True

    # -- selection helpers ---------------------------------------------------------
    def selection_bounds(self) -> Optional[Tuple[int, int, int, int]]:
        ranges = self.selectedRanges()
        if not ranges:
            current = self.currentIndex()
            if not current.isValid():
                return None
            return current.row(), current.column(), current.row(), current.column()
        top = min(r.topRow() for r in ranges)
        left = min(r.leftColumn() for r in ranges)
        bottom = max(r.bottomRow() for r in ranges)
        right = max(r.rightColumn() for r in ranges)
        return top, left, bottom, right

    def _visible_columns(self, left: int, right: int) -> List[int]:
        return [c for c in range(left, right + 1) if not self.isColumnHidden(c)]

    # -- actions -------------------------------------------------------------------
    def copy_selection(self) -> None:
        bounds = self.selection_bounds()
        if bounds is None:
            return
        top, left, bottom, right = bounds
        cols = self._visible_columns(left, right)
        lines = []
        for r in range(top, bottom + 1):
            cells = []
            for c in cols:
                item = self.item(r, c)
                cells.append(item.text() if item is not None else "")
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))

    def paste(self) -> None:
        text = QApplication.clipboard().text()
        if not text:
            return
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        grid = [line.split("\t") for line in lines]
        if not grid:
            return
        bounds = self.selection_bounds()
        if bounds is None:
            return
        top, left, bottom, right = bounds
        # A single clipboard value fills the whole selection.
        if len(grid) == 1 and len(grid[0]) == 1 and (bottom > top or right > left):
            value = grid[0][0]
            changes = [(r, c, value) for r in range(top, bottom + 1)
                       for c in self._visible_columns(left, right)
                       if self._editable(r, c)]
            self.set_cells(changes)
            return
        needed = top + len(grid) - self.rowCount()
        if needed > 0 and self.add_rows_callback is not None:
            self.add_rows_callback(needed)
        cols = [c for c in range(left, self.columnCount()) if not self.isColumnHidden(c)]
        changes = []
        for i, row_values in enumerate(grid):
            r = top + i
            if r >= self.rowCount():
                break
            for j, value in enumerate(row_values):
                if j >= len(cols):
                    break
                c = cols[j]
                if self._editable(r, c):
                    changes.append((r, c, value))
        self.set_cells(changes)

    def clear_selection_cells(self) -> None:
        changes = [(idx.row(), idx.column(), "") for idx in self.selectedIndexes()
                   if self._editable(idx.row(), idx.column())]
        self.set_cells(changes)

    def fill_down(self) -> None:
        bounds = self.selection_bounds()
        if bounds is None:
            return
        top, left, bottom, right = bounds
        if bottom <= top:
            return
        changes = []
        for c in self._visible_columns(left, right):
            source = self.item(top, c)
            value = source.text() if source is not None else ""
            for r in range(top + 1, bottom + 1):
                if self._editable(r, c):
                    changes.append((r, c, value))
        self.set_cells(changes)

    # -- keys ----------------------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        key = event.key()
        ctrl = _has_ctrl(event)
        if ctrl and key == _KEY.Key_C:
            self.copy_selection()
            return
        if ctrl and key == _KEY.Key_V:
            self.paste()
            return
        if ctrl and key == _KEY.Key_D:
            self.fill_down()
            return
        if ctrl and key == _KEY.Key_Z:
            self.undo()
            return
        if ctrl and key == _KEY.Key_Y:
            self.redo()
            return
        if key in (_KEY.Key_Delete, _KEY.Key_Backspace) and self.state() != _EDITING:
            self.clear_selection_cells()
            return
        super().keyPressEvent(event)
