# -*- coding: utf-8 -*-
"""Project panel: the project's cable-lay data file and what is in it.

One GeoPackage is the project's *data file* (remembered in the QGIS project's
custom properties, like the Burial Planner's plan file). This tab is the
file-level control surface: create, open, duplicate or delete the file, add
the standard layers a file is missing, compact it, and see an inventory of
its layers (row counts, whether each is in the QGIS project and loaded in the
Explorer, last import, schema warnings). Importing data and curating rows
live in the Manage tab; the per-layer *Import…* button just takes you there.

Reliability rules
-----------------
* Nothing is created or replaced silently. A saved path that no longer
  exists is reported with *Locate…* / *New…* offered; a relocated copy found
  beside the project is used but announced.
* The inventory is read with plain sqlite on a worker thread
  (:mod:`processing.cable_lay_gpkg_ops`), so a multi-GB file never freezes
  QGIS; the panel shows "Scanning…" and disables actions meanwhile.
* Layers are matched to the file by decoded provider URI (path + table),
  never by layer-tree name, so renaming a layer in the project is harmless;
  tables are matched by type suffix, so a renamed / duplicated file still
  resolves its layers.
* Destructive actions (delete) list their consequences, refuse while a load
  or edit is running, and release every handle QGIS holds on the file first.
* Every Qt slot is a bound method (closure slots crash QGIS).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject

from ...processing import cable_lay_gpkg_ops as gops
from ...processing import cable_lay_manage_ops as ops
from ...qgis_compat import (
    EDIT_TRIGGER_NONE,
    HEADER_RESIZE_MODE_CONTENTS,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MESSAGEBOX_NO,
    MESSAGEBOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
    TOOLBUTTON_POPUP_MODE_INSTANT,
)
from ..task_runner import TaskRunner

_FILE_FILTER = "GeoPackage (*.gpkg)"
_DONT_CONFIRM_OVERWRITE = getattr(
    getattr(QFileDialog, "Option", QFileDialog), "DontConfirmOverwrite", None
)
#: Layers added to the QGIS project when a file is created / opened: the data
#: layers and the QC findings. The log / config tables are provenance the
#: Manage tab's History shows; they stay out of the layer tree.
_TREE_TYPES = gops.IMPORTABLE_TYPES + ("qc_findings",)


def _cell(text, tooltip: str = "") -> QTableWidgetItem:
    item = QTableWidgetItem("" if text is None else str(text))
    if tooltip:
        item.setToolTip(tooltip)
    return item


def _human_size(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024:.0f} KB"


class ProjectPanel(QWidget):
    """See the module docstring. ``controller`` is the Explorer window."""

    #: Tests set this to ``False`` so scans and file jobs run inline.
    run_async = True

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._path: Optional[str] = None
        self._inventory: Optional[Dict] = None
        self._note = ""
        self._runner = TaskRunner(self)
        self._runner.run_async = self.run_async
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(300)
        self._refresh_timer.timeout.connect(self.refresh)
        self._connected_project = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        header = QHBoxLayout()
        title = QLabel("<b>Data file</b>")
        header.addWidget(title)
        self.path_label = QLabel("(none)")
        header.addWidget(self.path_label, 1)
        self.badge = QLabel("")
        header.addWidget(self.badge)
        layout.addLayout(header)

        file_buttons = QHBoxLayout()
        self.new_button = QPushButton("New…")
        self.new_button.setToolTip(
            "Create a new GeoPackage with the empty standard cable-lay layers "
            "and add them to the QGIS project."
        )
        self.new_button.clicked.connect(self.new_file)
        file_buttons.addWidget(self.new_button)
        self.open_button = QPushButton("Open…")
        self.open_button.setToolTip("Use an existing cable-lay GeoPackage as this project's data file.")
        self.open_button.clicked.connect(self.open_file)
        file_buttons.addWidget(self.open_button)

        self.more_button = QToolButton()
        self.more_button.setText("More…")
        self.more_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        menu = QMenu(self.more_button)
        self.duplicate_action = menu.addAction("Duplicate…", self.duplicate_file)
        self.add_missing_action = menu.addAction("Add missing standard layers", self.add_missing_layers)
        self.compact_action = menu.addAction("Compact (VACUUM)…", self.compact_file)
        menu.addSeparator()
        self.delete_action = menu.addAction("Delete file…", self.delete_file)
        menu.addSeparator()
        self.refresh_action = menu.addAction("Refresh", self.refresh)
        self.more_button.setMenu(menu)
        file_buttons.addWidget(self.more_button)
        file_buttons.addStretch(1)
        layout.addLayout(file_buttons)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Layer", "Rows", "Project", "Explorer", "Last import", "Status"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, HEADER_RESIZE_MODE_STRETCH)
        for col in range(1, 5):
            header.setSectionResizeMode(col, HEADER_RESIZE_MODE_CONTENTS)
        header.setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(self.table.fontMetrics().height() + 6)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table, 1)

        layer_buttons = QGridLayout()
        self.add_button = QPushButton("Add to project")
        self.add_button.setToolTip("Add the selected layer(s) to the QGIS project (in the file's group).")
        self.add_button.clicked.connect(self.add_selected_to_project)
        layer_buttons.addWidget(self.add_button, 0, 0)
        self.load_button = QPushButton("Load in Explorer")
        self.load_button.setToolTip(
            "Add the selected layer(s) to the project if needed and load them "
            "into the Explorer (background, cancellable)."
        )
        self.load_button.clicked.connect(self.load_selected)
        layer_buttons.addWidget(self.load_button, 0, 1)
        self.import_button = QPushButton("Import…")
        self.import_button.setToolTip("Go to Manage > Import for the selected layer.")
        self.import_button.clicked.connect(self.import_selected)
        layer_buttons.addWidget(self.import_button, 1, 0)
        self.remove_button = QPushButton("Remove from project")
        self.remove_button.setToolTip(
            "Unload the selected layer(s) from the Explorer and remove them from "
            "the QGIS project. Nothing in the file is changed."
        )
        self.remove_button.clicked.connect(self.remove_selected_from_project)
        layer_buttons.addWidget(self.remove_button, 1, 1)
        layout.addLayout(layer_buttons)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._update_buttons()

    # -- lifecycle -----------------------------------------------------------
    def attach_project_signals(self) -> None:
        """Follow project open/close and layer add/remove (idempotent)."""
        project = QgsProject.instance()
        if self._connected_project is project:
            return
        self._connected_project = project
        project.readProject.connect(self._on_project_changed)
        project.cleared.connect(self._on_project_changed)
        project.layersAdded.connect(self._on_layers_changed)
        project.layersRemoved.connect(self._on_layers_changed)

    def detach_project_signals(self) -> None:
        project = self._connected_project
        self._connected_project = None
        if project is None:
            return
        for signal, slot in (
            (project.readProject, self._on_project_changed),
            (project.cleared, self._on_project_changed),
            (project.layersAdded, self._on_layers_changed),
            (project.layersRemoved, self._on_layers_changed),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_project_changed(self, *_args) -> None:
        self._path = None
        self._inventory = None
        self.refresh_soon()

    def _on_layers_changed(self, *_args) -> None:
        if not self.isVisible():
            self._inventory = None  # rescan when the tab is next shown
            return
        self.refresh_soon()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        if self._inventory is None:
            self.refresh_soon()

    def refresh_soon(self) -> None:
        """Rescan shortly if on screen; otherwise mark stale for the next show."""
        if not self.isVisible():
            self._inventory = None
            return
        self._refresh_timer.start()

    # -- accessors -----------------------------------------------------------
    def current_path(self) -> Optional[str]:
        return self._path

    def inventory(self) -> Optional[Dict]:
        return self._inventory

    def entry_for_type(self, layer_type: str) -> Optional[Dict]:
        if not self._inventory:
            return None
        for entry in self._inventory.get("entries", []):
            if entry["type"] == layer_type:
                return entry
        return None

    def set_path(self, path: Optional[str], remember: bool = True) -> None:
        """Switch the managed file (and remember it in the QGIS project)."""
        self._path = path
        self._inventory = None
        if remember:
            gops.set_project_gpkg_path(path)
        self.refresh()

    # -- refresh -------------------------------------------------------------
    def refresh(self) -> None:
        """Resolve the data file and rescan it in the background."""
        if self._runner.busy:
            self.refresh_soon()  # a file job is running; look again after it
            return
        if self._path is None or not os.path.isfile(self._path):
            path, note = gops.discover_gpkg_path()
            self._note = note
            if path is not None:
                self._path = path
                if not gops.same_path(path, gops.project_gpkg_path()):
                    gops.set_project_gpkg_path(path)
            elif self._path is not None and not os.path.isfile(self._path):
                self._note = note or f"The data file was not found:\n{self._path}"
        self._render_header()
        path = self._path
        if not path:
            self._inventory = gops.inventory("")
            self._populate()
            return
        self.status_label.setText(f"Scanning {os.path.basename(path)}…")
        self._set_actions_enabled(False)
        started = self._runner.start(
            "Scanning data file",
            lambda feedback: gops.inventory(path, feedback.setProgress, feedback.isCanceled),
            self._on_scanned,
            on_error=self._on_scan_error,
            show_dialog=False,
        )
        if not started:
            self.refresh_soon()

    def _on_scanned(self, result: Dict) -> None:
        self._inventory = result
        self._set_actions_enabled(True)
        self._populate()

    def _on_scan_error(self, message: str) -> None:
        self._set_actions_enabled(True)
        self._inventory = {"path": self._path, "exists": False, "valid": False,
                           "error": message, "entries": [], "extras": [], "size_bytes": 0}
        self._populate()

    def _render_header(self) -> None:
        path = self._path
        if not path:
            self.path_label.setText("(no data file for this project)")
            self.path_label.setToolTip("")
            self.badge.setText("")
            return
        self.path_label.setText(os.path.basename(path))
        self.path_label.setToolTip(path)

    def _populate(self) -> None:
        inv = self._inventory or {}
        path = self._path
        table = self.table
        table.setRowCount(0)
        if not path:
            self.badge.setText("")
            self.status_label.setText(
                "No data file is set for this project. Use New… to create one or "
                "Open… to choose an existing cable-lay GeoPackage."
            )
            self._update_buttons()
            return
        if not inv.get("valid"):
            self.badge.setText("<span style='color:#b00'>unavailable</span>")
            message = inv.get("error") or "The file could not be read."
            if self._note:
                message = self._note + "\n\n" + message if message not in self._note else self._note
            self.status_label.setText(
                message + "\n\nUse Open… to locate the file or New… to create one."
            )
            self._update_buttons()
            return

        project_layers = {name: layer for layer, name in gops.project_layers_for(path)}
        loaded = set(self.controller.loaded_layer_ids())
        for entry in inv.get("entries", []):
            row = table.rowCount()
            table.insertRow(row)
            name = entry.get("name")
            label = entry["label"]
            tip = f"Table: {name}" if name else "No table of this type in the file."
            item = _cell(label, tip)
            item.setData(ITEM_DATA_USER_ROLE, entry["type"])
            table.setItem(row, 0, item)
            rows = entry.get("rows")
            table.setItem(row, 1, _cell("" if rows is None else f"{rows:,}"))
            layer = project_layers.get(name) if name else None
            table.setItem(row, 2, _cell("yes" if layer is not None else "", layer.name() if layer else ""))
            in_explorer = layer is not None and layer.id() in loaded
            table.setItem(row, 3, _cell("yes" if in_explorer else ""))
            stamp, source = entry.get("last_import") or ("", "")
            table.setItem(row, 4, _cell(stamp[:16].replace("T", " ") if stamp else "", source))
            table.setItem(row, 5, _cell(*self._status_for(entry, path, layer)))
        extras = inv.get("extras") or []
        parts = [f"{_human_size(inv.get('size_bytes', 0))}"]
        if extras:
            names = ", ".join(e["name"] for e in extras[:6])
            if len(extras) > 6:
                names += f" (+{len(extras) - 6} more)"
            parts.append(f"other tables: {names}")
        self.badge.setText("<span style='color:#080'>ok</span>")
        text = "; ".join(parts) + "."
        if self._note:
            text = self._note + "\n\n" + text
            self._note = ""  # announce a recovery once
        self.status_label.setText(text)
        self._update_buttons()

    @staticmethod
    def _status_for(entry: Dict, path: str, layer) -> tuple:
        if not entry.get("exists"):
            return "missing", "Use More > Add missing standard layers to create it."
        problems = []
        expected = None
        try:
            from ...processing import cable_lay_parsers as clp
            expected = clp.prefixed_layer_name(path, entry["type"])
        except Exception:
            pass
        if expected and entry.get("name") != expected:
            problems.append(f"table named {entry['name']}")
        if entry.get("missing_columns"):
            cols = ", ".join(entry["missing_columns"][:4])
            problems.append(f"missing columns: {cols}")
        if entry.get("duplicates"):
            problems.append(f"also: {', '.join(entry['duplicates'])}")
        if layer is not None and layer.isEditable():
            problems.append("in edit mode")
        if not problems:
            return "ok", ""
        return "; ".join(problems), (
            "The layer still works: tables are matched by their type suffix. "
            "Missing standard columns are added by the next import; a second "
            "table of the same type is ignored by the Explorer."
        )

    # -- selection helpers ---------------------------------------------------
    def _selected_entries(self) -> List[Dict]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        entries = []
        for row in rows:
            item = self.table.item(row, 0)
            if item is None:
                continue
            entry = self.entry_for_type(item.data(ITEM_DATA_USER_ROLE))
            if entry is not None:
                entries.append(entry)
        return entries

    def _set_actions_enabled(self, enabled: bool) -> None:
        self._actions_enabled = enabled
        self._update_buttons()

    def _update_buttons(self) -> None:
        enabled = getattr(self, "_actions_enabled", True) and not self._runner.busy
        have_file = bool(self._path) and bool((self._inventory or {}).get("valid"))
        self.new_button.setEnabled(enabled)
        self.open_button.setEnabled(enabled)
        self.more_button.setEnabled(enabled)
        for action in (self.duplicate_action, self.add_missing_action,
                       self.compact_action, self.delete_action):
            action.setEnabled(enabled and have_file)
        selected = self._selected_entries() if have_file else []
        existing = [e for e in selected if e.get("exists")]
        self.add_button.setEnabled(enabled and bool(existing))
        self.load_button.setEnabled(enabled and bool(existing))
        self.remove_button.setEnabled(enabled and bool(existing))
        self.import_button.setEnabled(
            enabled and len(selected) == 1 and bool(selected[0].get("importable"))
        )

    def _busy_reason(self) -> Optional[str]:
        if self._runner.busy:
            return "Another file operation is still running."
        if self.controller.is_busy():
            return "Wait for the current load or edit to finish."
        return None

    def _refuse_if_busy(self) -> bool:
        reason = self._busy_reason()
        if reason:
            self.status_label.setText(reason)
            return True
        return False

    def _start_dir(self) -> str:
        if self._path:
            return os.path.dirname(self._path)
        default = gops.default_project_gpkg_path()
        return os.path.dirname(default) if default else ""

    # -- file actions ---------------------------------------------------------
    def new_file(self) -> None:
        if self._refuse_if_busy():
            return
        suggested = gops.default_project_gpkg_path() or os.path.join(self._start_dir(), "cable_lay.gpkg")
        args = [self, "New cable-lay data file", suggested, _FILE_FILTER]
        if _DONT_CONFIRM_OVERWRITE is not None:
            path, _ = QFileDialog.getSaveFileName(*args, options=_DONT_CONFIRM_OVERWRITE)
        else:  # pragma: no cover
            path, _ = QFileDialog.getSaveFileName(*args)
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        self.create_file(path)

    def create_file(self, path: str) -> None:
        if os.path.exists(path):
            QMessageBox.warning(
                self, "New data file",
                f"{os.path.basename(path)} already exists.\n\nExisting files are never "
                "overwritten. Use Open… to work with it, or choose another name.",
            )
            return
        context = self.controller.transform_context()
        self._pending_path = path
        self._runner.start(
            "Creating data file",
            lambda feedback: gops.create_gpkg(path, context),
            self._after_create,
        )

    def _after_create(self, created: List[str]) -> None:
        path = self._pending_path
        added = self._add_tree_layers(path)
        self._path = path
        gops.set_project_gpkg_path(path)
        self.refresh()
        self.status_label.setText(
            f"Created {os.path.basename(path)} with {len(created)} layers; "
            f"{added} added to the project."
        )

    def _add_tree_layers(self, path: str) -> int:
        """Add every data layer of ``path`` to the QGIS project (main thread)."""
        tables = gops.list_tables(path) or []
        names = [name for name, _ in tables]
        added = 0
        for layer_type in _TREE_TYPES:
            table = gops.find_layer_for_type(path, names, layer_type)
            if table is None:
                continue
            if gops.add_layer_to_project(path, table) is not None:
                added += 1
        return added

    def open_file(self) -> None:
        if self._refuse_if_busy():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open cable-lay data file", self._start_dir(), _FILE_FILTER
        )
        if not path:
            return
        self.use_file(path)

    def use_file(self, path: str, ask: bool = True) -> None:
        """Adopt ``path`` after validation; offers to add missing layers."""
        if not gops.is_geopackage(path):
            QMessageBox.warning(
                self, "Open data file",
                f"{os.path.basename(path)} is not a readable GeoPackage.",
            )
            return
        if not gops.is_cable_lay_gpkg(path):
            if ask:
                answer = QMessageBox.question(
                    self, "Open data file",
                    f"{os.path.basename(path)} contains none of the standard cable-lay "
                    "layers.\n\nAdd the empty standard layers to it and use it as this "
                    "project's data file?",
                    MESSAGEBOX_YES | MESSAGEBOX_NO,
                )
                if answer != MESSAGEBOX_YES:
                    return
            self._path = path
            gops.set_project_gpkg_path(path)
            self.add_missing_layers()
            return
        self._path = path
        gops.set_project_gpkg_path(path)
        self._add_tree_layers(path)
        self.refresh()

    def duplicate_file(self) -> None:
        if self._refuse_if_busy() or not self._path:
            return
        src = self._path
        stem, _ = os.path.splitext(os.path.basename(src))
        suggested = os.path.join(os.path.dirname(src), f"{stem}_copy.gpkg")
        args = [self, "Duplicate data file", suggested, _FILE_FILTER]
        if _DONT_CONFIRM_OVERWRITE is not None:
            dst, _ = QFileDialog.getSaveFileName(*args, options=_DONT_CONFIRM_OVERWRITE)
        else:  # pragma: no cover
            dst, _ = QFileDialog.getSaveFileName(*args)
        if not dst:
            return
        if not dst.lower().endswith(".gpkg"):
            dst += ".gpkg"
        self.duplicate_to(dst)

    def duplicate_to(self, dst: str) -> None:
        src = self._path
        if os.path.exists(dst):
            QMessageBox.warning(
                self, "Duplicate data file",
                f"{os.path.basename(dst)} already exists. Existing files are never "
                "overwritten; choose another name.",
            )
            return
        self._pending_path = dst
        self._runner.start(
            f"Copying {os.path.basename(src)}",
            lambda feedback: gops.duplicate_gpkg(
                src, dst, feedback.setProgress, feedback.isCanceled
            ),
            self._after_duplicate,
            on_error=self._file_error,
        )

    def _after_duplicate(self, dst: str) -> None:
        answer = QMessageBox.question(
            self, "Duplicate data file",
            f"Copied to {os.path.basename(dst)}.\n\nSwitch this project to the copy? "
            "(Its layers keep their current table names; the original stays as it is.)",
            MESSAGEBOX_YES | MESSAGEBOX_NO,
        )
        if answer == MESSAGEBOX_YES:
            self._path = dst
            gops.set_project_gpkg_path(dst)
            self._add_tree_layers(dst)
        self.refresh()
        self.status_label.setText(f"Copied to {dst}.")

    def _file_error(self, message: str) -> None:
        self._update_buttons()
        if message == "Cancelled.":
            self.status_label.setText("Cancelled.")
            return
        QMessageBox.critical(self, "Data file", message)
        self.refresh()

    def add_missing_layers(self) -> None:
        if self._refuse_if_busy() or not self._path:
            return
        path = self._path
        context = self.controller.transform_context()
        self._runner.start(
            "Adding standard layers",
            lambda feedback: gops.add_missing_layers(path, context),
            self._after_add_missing,
            on_error=self._file_error,
        )

    def _after_add_missing(self, created: List[str]) -> None:
        if self._path:
            self._add_tree_layers(self._path)
            ops.reload_project_layers(self._path)
        self.refresh()
        if created:
            self.status_label.setText("Created: " + ", ".join(created) + ".")
        else:
            self.status_label.setText("Every standard layer is already present.")

    def compact_file(self) -> None:
        if self._refuse_if_busy() or not self._path:
            return
        path = self._path
        answer = QMessageBox.question(
            self, "Compact data file",
            "Run VACUUM to reclaim free space?\n\nClose the file in any other "
            "application first. The file is rewritten in place, which can take a "
            "while for large files; QGIS stays responsive meanwhile.",
            MESSAGEBOX_YES | MESSAGEBOX_NO,
        )
        if answer != MESSAGEBOX_YES:
            return
        self._runner.start(
            f"Compacting {os.path.basename(path)}",
            lambda feedback: ops.vacuum_gpkg(path),
            self._after_compact,
            on_error=self._file_error,
            indeterminate=True,
            cancellable=False,
        )

    def _after_compact(self, sizes) -> None:
        before, after = sizes
        if self._path:
            ops.reload_project_layers(self._path)
        self.refresh()
        saved = max(0, before - after)
        self.status_label.setText(
            f"Compacted: {_human_size(before)} -> {_human_size(after)} "
            f"({_human_size(saved)} reclaimed)."
        )

    def delete_file(self) -> None:
        if self._refuse_if_busy() or not self._path:
            return
        path = self._path
        referencing = gops.project_layers_for(path)
        size = _human_size((self._inventory or {}).get("size_bytes", 0))
        lines = [
            f"Delete {os.path.basename(path)} ({size})?",
            "",
            f"Folder: {os.path.dirname(path)}",
        ]
        if referencing:
            lines.append(
                f"{len(referencing)} layer(s) in the QGIS project use this file; "
                "they will be removed from the project first."
            )
        lines += ["", "This deletes the file from disk and cannot be undone."]
        answer = QMessageBox.question(
            self, "Delete data file", "\n".join(lines), MESSAGEBOX_YES | MESSAGEBOX_NO
        )
        if answer != MESSAGEBOX_YES:
            return
        self.delete_current(confirm=False)

    def delete_current(self, confirm: bool = True) -> bool:
        """Delete the managed file; returns True on success."""
        path = self._path
        if not path:
            return False
        if confirm:
            self.delete_file()
            return True
        ids = [layer.id() for layer, _ in gops.project_layers_for(path)]
        if ids:
            self.controller.unload_layers(ids)
            QgsProject.instance().removeMapLayers(ids)
        try:
            from ...burial import gpkg_sql
            gpkg_sql.close(path)
        except Exception:
            pass
        try:
            gops.delete_gpkg(path)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Delete data file", str(exc))
            self.refresh()
            return False
        gops.set_project_gpkg_path(None)
        self._path = None
        self._inventory = None
        self._note = ""
        self.refresh()
        self.status_label.setText(f"Deleted {os.path.basename(path)}.")
        return True

    # -- layer actions --------------------------------------------------------
    def _selected_tables(self) -> List[str]:
        return [e["name"] for e in self._selected_entries() if e.get("exists") and e.get("name")]

    def add_selected_to_project(self) -> None:
        if self._refuse_if_busy() or not self._path:
            return
        added = 0
        for table in self._selected_tables():
            if gops.add_layer_to_project(self._path, table) is not None:
                added += 1
        self.status_label.setText(f"{added} layer(s) in the project.")
        self.refresh()

    def load_selected(self) -> None:
        if self._refuse_if_busy() or not self._path:
            return
        ids = []
        for table in self._selected_tables():
            layer = gops.add_layer_to_project(self._path, table)
            if layer is not None:
                ids.append(layer.id())
        if not ids:
            self.status_label.setText("Nothing to load.")
            return
        self.controller.load_layers(ids)
        self.refresh_soon()

    def remove_selected_from_project(self) -> None:
        if self._refuse_if_busy() or not self._path:
            return
        ids = []
        for table in self._selected_tables():
            layer = gops.project_layer_for_table(self._path, table)
            if layer is not None:
                ids.append(layer.id())
        if ids:
            self.controller.unload_layers(ids)
            QgsProject.instance().removeMapLayers(ids)
        self.status_label.setText(f"Removed {len(ids)} layer(s) from the project.")
        self.refresh()

    def import_selected(self) -> None:
        entries = [e for e in self._selected_entries() if e.get("importable")]
        if len(entries) != 1:
            return
        self.controller.go_to_import(entries[0]["type"])

    def _on_row_double_clicked(self, item) -> None:
        entry = self.entry_for_type(self.table.item(item.row(), 0).data(ITEM_DATA_USER_ROLE))
        if entry is None:
            return
        if entry.get("exists"):
            self.table.selectRow(item.row())
            self.load_selected()
        elif entry.get("importable"):
            self.controller.go_to_import(entry["type"])
