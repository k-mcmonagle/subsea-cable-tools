# -*- coding: utf-8 -*-
"""Editor for the cable type library GeoPackage."""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..plugin_log import log_exception
from ..qgis_compat import (
    BUTTON_BOX_CLOSE,
    BUTTON_BOX_SAVE,
    MESSAGEBOX_NO,
    MESSAGEBOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
)
from . import store

_TITLE = "Cable Type Library"


class CableLibraryDialog(QDialog):
    """Edit the library rows; Save writes them back in one transaction."""

    librarySaved = pyqtSignal(str)

    def __init__(self, parent=None, path: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle(_TITLE)
        self.resize(1100, 480)
        self._path = ""
        self._dirty = False
        self._loading = False

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Cable and rope properties used by the Cable Lay Data Explorer's lay checks. "
            "The library is a GeoPackage on your computer; enter values from your own cable "
            "specifications. Aliases let a type match the names used in lay data "
            "(e.g. <i>LW, LWA</i>). Hover a column heading for its meaning.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Library:"))
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("No library chosen: create one or open an existing GeoPackage")
        path_row.addWidget(self.path_edit, 1)
        new_btn = QPushButton("New…")
        new_btn.clicked.connect(self._new_library)
        path_row.addWidget(new_btn)
        open_btn = QPushButton("Open…")
        open_btn.clicked.connect(self._open_library)
        path_row.addWidget(open_btn)
        layout.addLayout(path_row)

        self.table = QTableWidget(0, len(store.COLUMNS))
        self.table.setHorizontalHeaderLabels([column.label for column in store.COLUMNS])
        for index, column in enumerate(store.COLUMNS):
            header = self.table.horizontalHeaderItem(index)
            if header is not None and column.help:
                header.setToolTip(column.help)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, 1)

        edit_row = QHBoxLayout()
        for text, slot in (("Add", self._add_row), ("Duplicate", self._duplicate_row),
                           ("Delete", self._delete_rows), ("Import CSV…", self._import_csv),
                           ("Export CSV…", self._export_csv)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            edit_row.addWidget(button)
        edit_row.addStretch(1)
        layout.addLayout(edit_row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox(BUTTON_BOX_SAVE | BUTTON_BOX_CLOSE)
        self.buttons.button(BUTTON_BOX_SAVE).clicked.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.set_path(path if path is not None else store.current_path())

    # -- library file -------------------------------------------------------
    def set_path(self, path: str) -> None:
        self._path = path if store.is_library(path) else ""
        self.path_edit.setText(self._path)
        rows: List[Dict] = []
        if self._path:
            try:
                rows = store.read_rows(self._path)
            except Exception as exc:
                log_exception("Cable library: reading failed")
                self.status.setText(f"Could not read the library: {exc}")
        self._fill(rows)
        self._set_dirty(False)
        self.buttons.button(BUTTON_BOX_SAVE).setEnabled(bool(self._path))

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(self, _TITLE, "Discard unsaved changes?", MESSAGEBOX_YES | MESSAGEBOX_NO)
        return answer == MESSAGEBOX_YES

    def _new_library(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getSaveFileName(self, "New cable library", "cable_library.gpkg",
                                              "GeoPackage (*.gpkg)")
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        try:
            store.create_library(path)
        except Exception as exc:
            log_exception("Cable library: create failed")
            QMessageBox.critical(self, _TITLE, f"Could not create the library:\n{exc}")
            return
        store.set_current_path(path)
        self.set_path(path)
        self.status.setText("New library created. Add your cable types, then Save.")

    def _open_library(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open cable library", os.path.dirname(self._path or ""),
                                              "GeoPackage (*.gpkg)")
        if not path:
            return
        if not store.is_library(path):
            answer = QMessageBox.question(
                self, _TITLE, f"{os.path.basename(path)} has no cable library table. Add one?",
                MESSAGEBOX_YES | MESSAGEBOX_NO)
            if answer != MESSAGEBOX_YES:
                return
            try:
                store.create_library(path)
            except Exception as exc:
                log_exception("Cable library: adding the table failed")
                QMessageBox.critical(self, _TITLE, f"Could not add the library table:\n{exc}")
                return
        store.set_current_path(path)
        self.set_path(path)

    # -- table --------------------------------------------------------------
    def _fill(self, rows: List[Dict]) -> None:
        self._loading = True
        try:
            self.table.setRowCount(0)
            for row in rows:
                self._append(row)
        finally:
            self._loading = False
        self.table.resizeColumnsToContents()

    def _append(self, row: Dict) -> None:
        index = self.table.rowCount()
        self.table.insertRow(index)
        for col, column in enumerate(store.COLUMNS):
            value = row.get(column.name)
            text = "" if value is None else (f"{value:g}" if isinstance(value, float) else str(value))
            self.table.setItem(index, col, QTableWidgetItem(text))

    def rows(self) -> List[Dict]:
        out = []
        for r in range(self.table.rowCount()):
            row = {}
            for col, column in enumerate(store.COLUMNS):
                item = self.table.item(r, col)
                row[column.name] = item.text() if item is not None else ""
            out.append(store.clean_row(row))
        return out

    def _selected_rows(self) -> List[int]:
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def _add_row(self) -> None:
        self._append({"category": "cable"})
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)
        self._set_dirty(True)

    def _duplicate_row(self) -> None:
        rows = self.rows()
        for index in self._selected_rows():
            copy = dict(rows[index])
            copy["name"] = f"{copy.get('name') or ''} (copy)"
            self._append(copy)
            self._set_dirty(True)

    def _delete_rows(self) -> None:
        for index in reversed(self._selected_rows()):
            self.table.removeRow(index)
            self._set_dirty(True)

    def _on_item_changed(self, _item) -> None:
        if not self._loading:
            self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.setWindowTitle(_TITLE + (" *" if dirty else ""))

    # -- CSV ----------------------------------------------------------------
    def _import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import cable types", "", "CSV (*.csv)")
        if not path:
            return
        try:
            incoming = store.read_csv(path)
        except Exception as exc:
            QMessageBox.critical(self, _TITLE, f"Could not read the CSV:\n{exc}")
            return
        existing = {(row.get("name") or "").lower(): i for i, row in enumerate(self.rows())}
        added = updated = 0
        self._loading = True
        try:
            for row in incoming:
                key = (row.get("name") or "").lower()
                if key in existing:
                    target = existing[key]
                    for col, column in enumerate(store.COLUMNS):
                        value = row.get(column.name)
                        text = "" if value is None else (f"{value:g}" if isinstance(value, float) else str(value))
                        self.table.setItem(target, col, QTableWidgetItem(text))
                    updated += 1
                else:
                    self._append(row)
                    added += 1
        finally:
            self._loading = False
        self._set_dirty(True)
        self.status.setText(f"Imported {added} new and {updated} updated type(s). Save to keep them.")

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export cable types", "cable_types.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            store.write_csv(path, self.rows())
        except Exception as exc:
            QMessageBox.critical(self, _TITLE, f"Could not write the CSV:\n{exc}")
            return
        self.status.setText(f"Exported to {path}.")

    # -- save / close -------------------------------------------------------
    def save(self) -> bool:
        if not self._path:
            QMessageBox.information(self, _TITLE, "Create or open a library first.")
            return False
        rows = self.rows()
        problems = store.validate(rows)
        if problems:
            QMessageBox.warning(self, _TITLE, "Please fix:\n\n" + "\n".join(problems[:15]))
            return False
        try:
            store.write_rows(self._path, rows)
        except Exception as exc:
            log_exception("Cable library: saving failed")
            QMessageBox.critical(self, _TITLE, f"Could not save the library:\n{exc}")
            return False
        self._set_dirty(False)
        notes = store.warnings_for(rows)
        self.status.setText(f"Saved {len(rows)} type(s)."
                            + (("  Check: " + " ".join(notes[:5])) if notes else ""))
        self.librarySaved.emit(self._path)
        return True

    def _settle_changes(self) -> bool:
        """Offer to save unsaved edits; False keeps the dialog open."""
        if not self._dirty:
            return True
        answer = QMessageBox.question(self, _TITLE, "Save changes to the cable library?",
                                      MESSAGEBOX_YES | MESSAGEBOX_NO)
        if answer == MESSAGEBOX_YES:
            return self.save()
        self._set_dirty(False)
        return True

    def reject(self) -> None:  # Escape / Close button
        if self._settle_changes():
            super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._settle_changes():
            super().closeEvent(event)
        else:
            event.ignore()


def open_library_dialog(parent=None) -> CableLibraryDialog:
    dialog = CableLibraryDialog(parent)
    dialog.show()
    return dialog
