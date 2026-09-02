# -*- coding: utf-8 -*-
"""Manage panel: per-source-file data management for the active layer.

The control surface for what is *in* the GeoPackage (the importers own getting
data in). One row per ``source_file`` shows row count, time range, the start
date recorded in ``import_log`` and the record-status breakdown; actions fix a
wrong start date (in place, via :mod:`processing.cable_lay_manage_ops`), set
the curation status, remove a file's rows, fill primary gaps from a secondary
source, filter the map to active rows, and compact the GeoPackage. Every
operation is recorded in the ``edit_log`` table.

Design notes
------------
* All data logic lives in ``cable_lay_manage_ops`` / ``cable_lay_parsers``;
  this panel is just the UI around it.
* Summaries and gap analysis run on the already loaded in-memory dataset
  (numpy, vectorised) and are computed lazily - only when the tab is actually
  visible - so switching layers or reloading never pays for a hidden panel.
* Edits never touch the project layer: they run in a background
  :class:`~explorer.manage_task.ManageEditTask` against a private layer on
  the same GeoPackage table (immune to the project layer's provider filter),
  with a cancellable progress dialog, and the project layer is reloaded
  afterwards.
* This panel always receives the *full* dataset (every record status), even
  when the Explorer's "Active rows only" view is on for the other panels, so
  standby / excluded rows stay manageable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from qgis.PyQt.QtCore import QDate
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsApplication, QgsProviderRegistry

from ...qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    EDIT_TRIGGER_NONE,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_DISPLAY_ROLE,
    MESSAGEBOX_NO,
    MESSAGEBOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
    qt_exec,
)
from ...processing import cable_lay_manage_ops as ops
from ...processing import cable_lay_parsers as clp
from ..manage_task import ManageEditTask, run_edit_sync

_EPOCH = datetime(1970, 1, 1)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WINDOW_MODAL = None
try:  # Qt5 / Qt6 scoped enum access
    from qgis.PyQt.QtCore import Qt as _Qt

    _WINDOW_MODAL = getattr(getattr(_Qt, "WindowModality", _Qt), "WindowModal")
except Exception:  # pragma: no cover
    _WINDOW_MODAL = None


def _epoch_to_iso(epoch: float) -> str:
    if epoch != epoch:  # NaN
        return ""
    return (_EPOCH + timedelta(seconds=float(epoch))).strftime("%Y-%m-%dT%H:%M:%S")


def _cell(value) -> QTableWidgetItem:
    """A read-only table cell; ints sort numerically, everything else as text."""
    item = QTableWidgetItem()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        item.setData(ITEM_DATA_DISPLAY_ROLE, value)
    else:
        item.setText("" if value is None else str(value))
    return item


def _qdate_for(text: str) -> Optional[QDate]:
    if not text or not _DATE_RE.match(text):
        return None
    parsed = QDate.fromString(text, "yyyy-MM-dd")
    return parsed if parsed.isValid() else None


class _StartDateDialog(QDialog):
    """Corrected (and optional previous) start date for the ISO-time fix."""

    def __init__(self, parent, sources: List[str], recorded: str, row_count: int):
        super().__init__(parent)
        self.setWindowTitle("Fix start date")
        form = QFormLayout(self)
        label = ", ".join(sources) if len(sources) <= 3 else f"{len(sources)} source files"
        intro = QLabel(
            f"Rewrite ISO_Time in place for {row_count:,} row(s) of: {label}\n"
            f"Recorded start date at import: {recorded or 'unknown'}"
        )
        intro.setWordWrap(True)
        form.addRow(intro)

        self.corrected = QDateEdit()
        self.corrected.setCalendarPopup(True)
        self.corrected.setDisplayFormat("yyyy-MM-dd")
        initial = _qdate_for(recorded) or QDate.currentDate()
        self.corrected.setDate(initial)
        self.corrected.setToolTip("The true calendar date of day count 1.")
        form.addRow("Corrected start date (day count 1):", self.corrected)

        self.previous = QLineEdit(recorded)
        self.previous.setPlaceholderText("YYYY-MM-DD (optional)")
        self.previous.setToolTip(
            "Only used for rows that have no raw day-count time column: their "
            "existing ISO_Time is shifted by (corrected - previous) days."
        )
        form.addRow("Previously used start date:", self.previous)

        self.dedupe = QCheckBox("Remove duplicates that result from the fix")
        self.dedupe.setChecked(True)
        self.dedupe.setToolTip(
            "After recomputing, rows that collide on the layer's duplicate key "
            "(e.g. same ISO_Time and source_file) are deleted, keeping the first. "
            "This cleans up a file imported twice under different start dates."
        )
        form.addRow(self.dedupe)

        note = QLabel(
            "This edits the GeoPackage directly and is recorded in its edit_log. "
            "It cannot be undone except by running the fix again with the old date."
        )
        note.setWordWrap(True)
        form.addRow(note)

        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _validate_and_accept(self) -> None:
        previous = self.previous.text().strip()
        if previous and _qdate_for(previous) is None:
            QMessageBox.warning(
                self, "Fix start date",
                "The previous start date must be YYYY-MM-DD (or left blank).",
            )
            return
        self.accept()

    def corrected_text(self) -> str:
        return self.corrected.date().toString("yyyy-MM-dd")

    def previous_text(self) -> str:
        return self.previous.text().strip()


class ManagePanel(QWidget):
    #: Tests set this to ``False`` to run edits inline on the calling thread.
    run_async = True

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._dataset = None
        self._gaps: List[Tuple[float, float]] = []
        self._recorded: Optional[Dict[str, str]] = None
        self._sources_dirty = True
        self._history_dirty = True
        self._saved_subsets: Dict[str, str] = {}
        self._edit_task: Optional[ManageEditTask] = None
        self._edit_progress: Optional[QProgressDialog] = None
        self._edit_finish: Optional[Callable] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_sources_tab(), "Sources")
        self.tabs.addTab(self._build_gaps_tab(), "Gap fill")
        self.tabs.addTab(self._build_history_tab(), "History")
        self.tabs.currentChanged.connect(self._on_subtab_changed)
        layout.addWidget(self.tabs, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._update_buttons()

    # -- UI construction ---------------------------------------------------
    def _build_sources_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)

        self.sources_table = QTableWidget(0, 8)
        headers = ["Source file", "Rows", "First time", "Last time", "Start date",
                   "Active", "Standby", "Excluded"]
        self.sources_table.setHorizontalHeaderLabels(headers)
        tips = {
            4: "Start date recorded in import_log for this file (latest import).",
            5: "Rows with record_status 'active' or empty (empty counts as active).",
            6: "Rows with record_status 'standby' (available for gap fill).",
            7: "Rows with record_status 'excluded' (curated out, not deleted).",
        }
        for col, tip in tips.items():
            item = self.sources_table.horizontalHeaderItem(col)
            if item is not None:
                item.setToolTip(tip)
        self.sources_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.sources_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.sources_table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.sources_table.setSortingEnabled(True)
        self.sources_table.horizontalHeader().setSectionResizeMode(0, HEADER_RESIZE_MODE_STRETCH)
        self.sources_table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.sources_table, 1)

        buttons = QGridLayout()
        self.fix_button = QPushButton("Fix start date…")
        self.fix_button.setToolTip(
            "Recompute ISO_Time in place for the selected source file(s) from "
            "their stored day-count times, then remove resulting duplicates."
        )
        self.fix_button.clicked.connect(self.fix_start_date)
        buttons.addWidget(self.fix_button, 0, 0)

        status_row = QHBoxLayout()
        self.status_combo = QComboBox()
        for status in ops.RECORD_STATUSES:
            self.status_combo.addItem(status)
        status_row.addWidget(self.status_combo)
        self.set_status_button = QPushButton("Set status")
        self.set_status_button.setToolTip(
            "Mark every row of the selected source file(s) as active / standby "
            "/ excluded (non-destructive - nothing is deleted)."
        )
        self.set_status_button.clicked.connect(self.set_status)
        status_row.addWidget(self.set_status_button)
        buttons.addLayout(status_row, 0, 1)

        self.remove_button = QPushButton("Remove rows…")
        self.remove_button.setToolTip(
            "Permanently delete every row of the selected source file(s). "
            "Prefer setting status to 'excluded' unless the import was a mistake."
        )
        self.remove_button.clicked.connect(self.remove_sources)
        buttons.addWidget(self.remove_button, 1, 0)

        self.compact_button = QPushButton("Compact GeoPackage…")
        self.compact_button.setToolTip(
            "Run SQLite VACUUM to reclaim free space (e.g. after removing rows)."
        )
        self.compact_button.clicked.connect(self.compact_gpkg)
        buttons.addWidget(self.compact_button, 1, 1)

        self.temporal_button = QPushButton("Enable QGIS temporal…")
        self.temporal_button.setToolTip(
            "Let the QGIS Temporal Controller play through this layer: adds a "
            "virtual ISO_DateTime field (project only, nothing written to the "
            "GeoPackage) and sets the layer's temporal properties to it."
        )
        self.temporal_button.clicked.connect(self.enable_temporal)
        buttons.addWidget(self.temporal_button, 2, 0, 1, 2)
        layout.addLayout(buttons)

        self.view_check = QCheckBox("Explorer view: active rows only")
        self.view_check.setToolTip(
            "Hide standby / excluded rows from the table, plots, QC and "
            "Inspection panels (in memory, instant, nothing written). The map "
            "and this Manage tab are unaffected. Same as the toolbar toggle."
        )
        self.view_check.toggled.connect(self._on_view_check_toggled)
        layout.addWidget(self.view_check)

        self.filter_check = QCheckBox("Map filter: show active rows only")
        self.filter_check.setToolTip(
            "Applies a provider filter (record_status active/empty) to the "
            "layer on the QGIS map. Any filter the layer already had is "
            "restored when you uncheck this."
        )
        self.filter_check.toggled.connect(self.toggle_active_filter)
        layout.addWidget(self.filter_check)
        return widget

    def _build_gaps_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)

        form = QFormLayout()
        self.primary_combo = QComboBox()
        self.primary_combo.setToolTip("The source whose active rows are the reference record.")
        form.addRow("Primary source:", self.primary_combo)
        self.secondary_combo = QComboBox()
        self.secondary_combo.setToolTip("The source whose rows may fill gaps in the primary.")
        form.addRow("Secondary source:", self.secondary_combo)
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(1.0, 1e7)
        self.threshold_spin.setDecimals(0)
        self.threshold_spin.setValue(60.0)
        self.threshold_spin.setSuffix(" s")
        self.threshold_spin.setMaximumWidth(110)
        self.threshold_spin.setToolTip(
            "A gap is a jump between consecutive active primary samples longer than this."
        )
        form.addRow("Gap threshold:", self.threshold_spin)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.find_gaps_button = QPushButton("Find gaps")
        self.find_gaps_button.setToolTip(
            "List time gaps in the primary source's active rows longer than "
            "the threshold, with how many secondary rows could fill each."
        )
        self.find_gaps_button.clicked.connect(self.find_gaps)
        buttons.addWidget(self.find_gaps_button)
        self.fill_gaps_button = QPushButton("Apply gap fill")
        self.fill_gaps_button.setToolTip(
            "Mark secondary rows inside primary gaps 'active' and the rest "
            "'standby' (excluded rows are left alone). Nothing is deleted."
        )
        self.fill_gaps_button.clicked.connect(self.apply_gap_fill)
        buttons.addWidget(self.fill_gaps_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.gaps_table = QTableWidget(0, 4)
        self.gaps_table.setHorizontalHeaderLabels(
            ["Gap start", "Gap end", "Duration (s)", "Secondary rows"]
        )
        self.gaps_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.gaps_table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.gaps_table.setSortingEnabled(True)
        self.gaps_table.horizontalHeader().setSectionResizeMode(0, HEADER_RESIZE_MODE_STRETCH)
        layout.addWidget(self.gaps_table, 1)
        return widget

    def _build_history_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)

        refresh = QPushButton("Refresh history")
        refresh.clicked.connect(self.refresh_history)
        layout.addWidget(refresh)

        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["Time", "Type", "What", "Details"])
        self.history_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.history_table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.history_table.setSortingEnabled(True)
        self.history_table.horizontalHeader().setSectionResizeMode(3, HEADER_RESIZE_MODE_STRETCH)
        layout.addWidget(self.history_table, 1)
        return widget

    # -- data binding (lazy) ----------------------------------------------
    def set_dataset(self, dataset) -> None:
        """Bind the *full* dataset of the active layer (every record status)."""
        self._dataset = dataset
        self._gaps = []
        self._recorded = None
        self.gaps_table.setRowCount(0)
        self._sources_dirty = True
        self._history_dirty = True
        self._sync_filter_check()
        self._sync_view_check()
        self._refresh_visible()

    def sync_active_only(self, active_only: bool) -> None:
        """Mirror the Explorer's in-memory active-rows toggle (from the controller)."""
        self.view_check.blockSignals(True)
        self.view_check.setChecked(bool(active_only))
        self.view_check.blockSignals(False)

    def refresh_now(self) -> None:
        """Recompute every tab immediately, visible or not (tests, callers)."""
        self._sources_dirty = True
        self._history_dirty = True
        self._populate_sources()
        self._populate_source_combos()
        self._update_buttons()
        self.refresh_history()
        self._sources_dirty = False

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self._refresh_visible()

    def _on_subtab_changed(self, _index: int) -> None:
        self._refresh_visible()

    def _refresh_visible(self) -> None:
        """Compute only what is on screen; hidden tabs stay dirty until shown."""
        if not self.isVisible():
            return
        if self._sources_dirty:
            self._sources_dirty = False
            self._populate_sources()
            self._populate_source_combos()
            self._update_buttons()
        if self._history_dirty and self.tabs.currentIndex() == 2:
            self.refresh_history()

    # -- shared accessors --------------------------------------------------
    def _physical_layer_name(self) -> Optional[str]:
        layer = self.controller.layer
        if layer is None:
            return None
        try:
            decoded = QgsProviderRegistry.instance().decodeUri(
                layer.providerType(), layer.source()
            )
            return decoded.get("layerName") or None
        except Exception:
            return None

    def _gpkg_bundle(self) -> Optional[Tuple[object, str, str]]:
        layer = self.controller.layer
        gpkg_path = self.controller.gpkg_path()
        name = self._physical_layer_name()
        if layer is None or self._dataset is None or not gpkg_path or not name:
            return None
        return layer, gpkg_path, name

    def _require_gpkg_layer(self):
        """Return (layer, gpkg_path, physical_name) or None with a status hint."""
        if self.controller.layer is None or self._dataset is None:
            self.status_label.setText("Load a data layer first.")
            return None
        bundle = self._gpkg_bundle()
        if bundle is None:
            self.status_label.setText(
                "The active layer is not a GeoPackage layer, so it cannot be managed here."
            )
            return None
        if self._edit_task is not None:
            self.status_label.setText("Another management edit is still running.")
            return None
        try:
            ops.check_not_editing(bundle[0])
        except RuntimeError as exc:
            QMessageBox.warning(self, "Manage", str(exc))
            return None
        return bundle

    def _selected_sources(self) -> List[str]:
        rows = self.sources_table.selectionModel().selectedRows()
        names = []
        for index in rows:
            item = self.sources_table.item(index.row(), 0)
            if item is not None and item.text():
                names.append(item.text())
        return names

    def _source_masks(self) -> Dict[str, np.ndarray]:
        dataset = self._dataset
        if dataset is None:
            return {}
        return dataset.source_masks()

    def _has_source_field(self) -> bool:
        return self._dataset is not None and self._dataset.source_field is not None

    def _update_buttons(self) -> None:
        manageable = self._gpkg_bundle() is not None and self._has_source_field()
        selected = manageable and bool(self._selected_sources())
        self.fix_button.setEnabled(selected)
        self.set_status_button.setEnabled(selected)
        self.status_combo.setEnabled(selected)
        self.remove_button.setEnabled(selected)
        self.compact_button.setEnabled(bool(self.controller.gpkg_path()))
        self.temporal_button.setEnabled(
            self.controller.layer is not None
            and self._dataset is not None and self._dataset.time_field is not None
        )
        two_sources = manageable and len(self._source_masks()) >= 2
        self.find_gaps_button.setEnabled(two_sources)
        self.fill_gaps_button.setEnabled(two_sources and bool(self._gaps))
        self.filter_check.setEnabled(self.controller.layer is not None)

    # -- sources table -----------------------------------------------------
    def _populate_sources(self) -> None:
        table = self.sources_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        dataset = self._dataset
        if dataset is None:
            table.setSortingEnabled(True)
            self.status_label.setText("Load a data layer first.")
            return
        if dataset.source_field is None:
            table.setSortingEnabled(True)
            self.status_label.setText(
                "This layer has no source-file column (source_file / event_file / "
                "slack_file / body_file), so per-file management is unavailable."
            )
            return
        masks = dataset.source_masks()
        statuses = dataset.status_array()
        recorded = self._recorded_start_dates()
        epochs = dataset.time_epoch

        for name, mask in masks.items():
            row = table.rowCount()
            table.insertRow(row)
            count = int(mask.sum())
            tmin = tmax = ""
            if epochs is not None:
                sub = epochs[mask]
                finite = sub[np.isfinite(sub)]
                if finite.size:
                    tmin = _epoch_to_iso(float(finite.min()))
                    tmax = _epoch_to_iso(float(finite.max()))
            if statuses is None:
                active, standby, excluded = count, 0, 0
            else:
                sub_status = statuses[mask]
                active = int(((sub_status == "") | (sub_status == ops.STATUS_ACTIVE)).sum())
                standby = int((sub_status == ops.STATUS_STANDBY).sum())
                excluded = int((sub_status == ops.STATUS_EXCLUDED).sum())
            cells = [name, count, tmin, tmax, recorded.get(name, ""), active, standby, excluded]
            for col, value in enumerate(cells):
                table.setItem(row, col, _cell(value))
        table.setSortingEnabled(True)
        summary = f"{len(masks)} source file(s), {dataset.row_count:,} row(s)."
        if statuses is None:
            summary += " No record_status column yet: every row counts as active."
        layer = self.controller.layer
        if layer is not None and (layer.subsetString() or ""):
            summary += (
                " A map filter is active, so these counts cover only the visible "
                "rows; edits still apply to every row of the selected files."
            )
        if self._gpkg_bundle() is None:
            summary += " Not a GeoPackage layer - read-only here."
        self.status_label.setText(summary)

    def _recorded_start_dates(self) -> Dict[str, str]:
        """Latest import_log start date per source file (cached per dataset)."""
        if self._recorded is not None:
            return self._recorded
        self._recorded = {}
        gpkg_path = self.controller.gpkg_path()
        name = self._physical_layer_name()
        if not gpkg_path or not name:
            return self._recorded
        log = clp.open_gpkg_layer(gpkg_path, clp.prefixed_layer_name(gpkg_path, "import_log"))
        if log is None:
            return self._recorded
        latest: Dict[str, Tuple[str, str]] = {}
        try:
            for feature in log.getFeatures():
                if str(feature["layer_name"]) != name:
                    continue
                source = str(feature["source_file"])
                stamp = str(feature["imported_at"] or "")
                start = str(feature["start_date"] or "")
                if source not in latest or stamp >= latest[source][0]:
                    latest[source] = (stamp, start)
        except Exception:
            return self._recorded
        self._recorded = {source: start for source, (_stamp, start) in latest.items()}
        return self._recorded

    def _populate_source_combos(self) -> None:
        names = list(self._source_masks().keys())
        for combo in (self.primary_combo, self.secondary_combo):
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            if current in names:
                combo.setCurrentText(current)
            combo.blockSignals(False)
        if len(names) >= 2 and self.secondary_combo.currentText() == self.primary_combo.currentText():
            self.secondary_combo.setCurrentIndex(1 if self.primary_combo.currentIndex() == 0 else 0)

    # -- background edit plumbing -----------------------------------------
    def _start_edit(
        self, description: str, gpkg_path: str, layer_name: str,
        work: Callable, finish: Callable,
    ) -> None:
        """Run ``work(private_layer, feedback)`` then ``finish(result)``.

        Asynchronously (QgsTask + cancellable progress dialog) in the GUI;
        inline when ``run_async`` is off or no task manager exists. The
        signal slots are bound methods so nothing is garbage-collected while
        the task runs.
        """
        manager = QgsApplication.taskManager() if self.run_async else None
        if manager is None:
            try:
                result = run_edit_sync(gpkg_path, layer_name, work)
            except Exception as exc:
                QMessageBox.critical(self, description, str(exc))
                return
            finish(result)
            return

        progress = QProgressDialog(description + "…", "Cancel", 0, 100, self.window())
        progress.setWindowTitle("Cable Lay Data Explorer")
        if _WINDOW_MODAL is not None:
            progress.setWindowModality(_WINDOW_MODAL)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        task = ManageEditTask(description, gpkg_path, layer_name, work)
        self._edit_task = task
        self._edit_progress = progress
        self._edit_finish = finish
        task.progressChanged.connect(self._on_edit_progress)
        task.taskCompleted.connect(self._on_edit_completed)
        task.taskTerminated.connect(self._on_edit_terminated)
        progress.canceled.connect(task.cancel)
        self._update_buttons()
        manager.addTask(task)
        progress.show()

    def _on_edit_progress(self, value) -> None:
        if self._edit_progress is not None:
            self._edit_progress.setValue(int(value))

    def _on_edit_completed(self) -> None:
        task, finish = self._edit_task, self._edit_finish
        self._teardown_edit()
        if task is not None and finish is not None:
            finish(task.result)

    def _on_edit_terminated(self) -> None:
        task = self._edit_task
        description = task.description() if task is not None else "Manage"
        error = getattr(task, "error", None) or "The edit did not complete."
        self._teardown_edit()
        if error == "Cancelled.":
            self.status_label.setText(f"{description}: cancelled (rows already written stay written).")
        else:
            QMessageBox.critical(self, description, error)
        # Whatever was written before the failure must be reflected.
        bundle = self._gpkg_bundle()
        if bundle is not None:
            ops.reload_project_layers(bundle[1], bundle[2])
        self.controller.reload_dataset()

    def _teardown_edit(self) -> None:
        if self._edit_progress is not None:
            self._edit_progress.reset()
            self._edit_progress.deleteLater()
            self._edit_progress = None
        self._edit_task = None
        self._edit_finish = None
        self._update_buttons()

    # -- actions: sources --------------------------------------------------
    def fix_start_date(self) -> None:
        bundle = self._require_gpkg_layer()
        if bundle is None:
            return
        layer, gpkg_path, name = bundle
        sources = self._selected_sources()
        if not sources:
            self.status_label.setText("Select one or more source files first.")
            return
        recorded = self._recorded_start_dates()
        dates = {recorded.get(s, "") for s in sources}
        if len(dates) > 1:
            lines = "\n".join(f"  {s}: {recorded.get(s, '') or 'unknown'}" for s in sources)
            QMessageBox.warning(
                self, "Fix start date",
                "The selected files were imported with different start dates:\n"
                f"{lines}\n\nFix them one group at a time so the 'previous start "
                "date' fallback is right for every row.",
            )
            return
        masks = self._source_masks()
        row_count = int(sum(int(masks[s].sum()) for s in sources if s in masks))
        dialog = _StartDateDialog(self, sources, recorded.get(sources[0], ""), row_count)
        if not qt_exec(dialog):
            return
        corrected = dialog.corrected_text()
        previous = dialog.previous_text()
        dedupe = dialog.dedupe.isChecked()
        key_fields = clp.dedupe_key_for(ops.layer_type_for_name(name) or "")

        def work(private_layer, feedback):
            counts = ops.recompute_iso_time(
                private_layer, corrected, old_start_date=previous,
                source_files=sources, feedback=feedback,
            )
            duplicates = 0
            if dedupe and not (feedback is not None and feedback.isCanceled()):
                duplicates = ops.dedupe_layer_in_place(
                    private_layer, key_fields, source_files=sources, feedback=feedback
                )
            return {
                "counts": counts, "duplicates": duplicates, "gpkg_path": gpkg_path,
                "name": name, "corrected": corrected, "previous": previous,
                "sources": sources, "dedupe": dedupe,
            }

        self._start_edit("Fix start date", gpkg_path, name, work, self._finish_fix_start_date)

    def _finish_fix_start_date(self, result: Dict) -> None:
        counts, duplicates = result["counts"], result["duplicates"]
        self._after_edit(
            result["gpkg_path"], result["name"], "recompute_iso_time",
            {
                "start_date": result["corrected"], "old_start_date": result["previous"],
                "source_files": result["sources"], "dedupe": result["dedupe"],
                "duplicates_removed": duplicates,
            },
            counts["updated"] + duplicates,
            f"updated={counts['updated']} unchanged={counts['unchanged']} "
            f"skipped={counts['skipped']} duplicates_removed={duplicates}",
        )
        text = (
            f"ISO_Time recomputed: {counts['updated']:,} updated, "
            f"{counts['unchanged']:,} already correct, {duplicates:,} duplicate(s) removed."
        )
        if counts["skipped"]:
            text += (
                f" {counts['skipped']:,} row(s) skipped: no parseable day-count time "
                "and no previous start date to shift from."
            )
        self.status_label.setText(text)

    def set_status(self) -> None:
        bundle = self._require_gpkg_layer()
        if bundle is None:
            return
        layer, gpkg_path, name = bundle
        sources = self._selected_sources()
        if not sources:
            self.status_label.setText("Select one or more source files first.")
            return
        status = self.status_combo.currentText()

        def work(private_layer, feedback):
            changed = ops.set_source_status(private_layer, status, sources, feedback=feedback)
            return {"changed": changed, "gpkg_path": gpkg_path, "name": name,
                    "status": status, "sources": sources}

        self._start_edit("Set status", gpkg_path, name, work, self._finish_set_status)

    def _finish_set_status(self, result: Dict) -> None:
        changed, status = result["changed"], result["status"]
        self._after_edit(
            result["gpkg_path"], result["name"], "set_record_status",
            {"status": status, "source_files": result["sources"]}, changed,
            f"{changed} row(s) set to {status}",
        )
        self.status_label.setText(f"{changed:,} row(s) set to '{status}'.")

    def remove_sources(self) -> None:
        bundle = self._require_gpkg_layer()
        if bundle is None:
            return
        layer, gpkg_path, name = bundle
        sources = self._selected_sources()
        if not sources:
            self.status_label.setText("Select one or more source files first.")
            return
        masks = self._source_masks()
        row_count = int(sum(int(masks[s].sum()) for s in sources if s in masks))
        answer = QMessageBox.question(
            self,
            "Remove rows",
            f"Permanently delete {row_count:,} row(s) of:\n  " + "\n  ".join(sources) +
            "\n\nThis cannot be undone (consider status 'excluded' instead). Continue?",
            MESSAGEBOX_YES | MESSAGEBOX_NO,
        )
        if answer != MESSAGEBOX_YES:
            return

        def work(private_layer, feedback):
            deleted = ops.delete_source_rows(private_layer, sources, feedback=feedback)
            return {"deleted": deleted, "gpkg_path": gpkg_path, "name": name, "sources": sources}

        self._start_edit("Remove rows", gpkg_path, name, work, self._finish_remove_sources)

    def _finish_remove_sources(self, result: Dict) -> None:
        deleted = result["deleted"]
        self._after_edit(
            result["gpkg_path"], result["name"], "delete_source_rows",
            {"source_files": result["sources"]}, deleted, f"{deleted} row(s) deleted",
        )
        self.status_label.setText(
            f"Deleted {deleted:,} row(s). Use 'Compact GeoPackage' to reclaim the space."
        )

    # -- view + map filter -------------------------------------------------
    def _on_view_check_toggled(self, checked: bool) -> None:
        setter = getattr(self.controller, "set_active_only", None)
        if setter is not None:
            setter(bool(checked))

    def _sync_view_check(self) -> None:
        getter = getattr(self.controller, "active_only", None)
        if getter is None:
            self.view_check.setEnabled(False)
            return
        self.sync_active_only(bool(getter()))

    def _layer_key(self) -> str:
        layer = self.controller.layer
        return layer.id() if layer is not None else ""

    def toggle_active_filter(self, checked: bool) -> None:
        layer = self.controller.layer
        if layer is None:
            return
        if checked and not (self._dataset is not None and self._dataset.has_status_field):
            self.status_label.setText(
                "No record_status column yet - set a status or apply a gap fill first."
            )
            self._sync_filter_check()
            return
        expression = ops.active_subset_expression()
        current = layer.subsetString() or ""
        key = self._layer_key()
        if checked:
            if current == expression:
                return
            if current:
                answer = QMessageBox.question(
                    self,
                    "Map filter",
                    "The layer already has a provider filter:\n"
                    f"{current}\n\nReplace it with the active-rows filter? "
                    "(It is restored when you uncheck the map filter.)",
                    MESSAGEBOX_YES | MESSAGEBOX_NO,
                )
                if answer != MESSAGEBOX_YES:
                    self._sync_filter_check()
                    return
            self._saved_subsets[key] = current
            target = expression
        else:
            target = self._saved_subsets.pop(key, "")
            if current != expression:
                # The user changed the filter elsewhere; leave it alone.
                self._sync_filter_check()
                return
        layer.setSubsetString(target)
        layer.triggerRepaint()
        self.controller.reload_dataset()
        self.status_label.setText(
            "Map filter applied (active rows only)." if checked
            else ("Map filter cleared." if not target else f"Previous map filter restored: {target}")
        )

    def _sync_filter_check(self) -> None:
        layer = self.controller.layer
        active = bool(layer is not None
                      and (layer.subsetString() or "") == ops.active_subset_expression())
        self.filter_check.blockSignals(True)
        self.filter_check.setChecked(active)
        self.filter_check.blockSignals(False)

    def compact_gpkg(self) -> None:
        gpkg_path = self.controller.gpkg_path()
        if not gpkg_path:
            self.status_label.setText("The active layer is not in a GeoPackage.")
            return
        if self._edit_task is not None:
            self.status_label.setText("Another management edit is still running.")
            return
        answer = QMessageBox.question(
            self,
            "Compact GeoPackage",
            "Run VACUUM to reclaim free space?\n\n"
            "Close this GeoPackage in any other application first; the file is "
            "rewritten in place and may take a while for large files.",
            MESSAGEBOX_YES | MESSAGEBOX_NO,
        )
        if answer != MESSAGEBOX_YES:
            return
        try:
            before, after = ops.vacuum_gpkg(gpkg_path)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Compact GeoPackage", str(exc))
            return
        ops.reload_project_layers(gpkg_path)
        saved = max(0, before - after) / (1024 * 1024)
        self.status_label.setText(
            f"Compacted: {before / (1024 * 1024):.1f} MB -> "
            f"{after / (1024 * 1024):.1f} MB ({saved:.1f} MB reclaimed)."
        )

    def enable_temporal(self) -> None:
        layer = self.controller.layer
        dataset = self._dataset
        if layer is None or dataset is None or dataset.time_field is None:
            self.status_label.setText("Load a layer with an ISO_Time column first.")
            return
        answer = QMessageBox.question(
            self,
            "Enable QGIS temporal",
            "Show every record up to the current time (accumulate)?\n\n"
            "Yes: lay data / tracks - the map fills in as time advances.\n"
            "No: events - only records inside the current time step show.",
            MESSAGEBOX_YES | MESSAGEBOX_NO,
        )
        try:
            field = ops.enable_temporal_navigation(
                layer, dataset.time_field, accumulate=(answer == MESSAGEBOX_YES)
            )
        except RuntimeError as exc:
            QMessageBox.critical(self, "Enable QGIS temporal", str(exc))
            return
        self.status_label.setText(
            f"Temporal navigation enabled on '{layer.name()}' via virtual field {field}. "
            "Open View > Panels > Temporal Controller and press play; the Explorer's "
            "own record stepping (Ctrl+Left/Right) works independently."
        )

    # -- actions: gap fill -------------------------------------------------
    def _gap_inputs(self):
        dataset = self._dataset
        if dataset is None or dataset.time_epoch is None:
            self.status_label.setText("Load a layer with ISO_Time data first.")
            return None
        primary = self.primary_combo.currentText()
        secondary = self.secondary_combo.currentText()
        if not primary or not secondary or primary == secondary:
            self.status_label.setText("Choose two different source files.")
            return None
        threshold = float(self.threshold_spin.value())
        if threshold <= 0:
            self.status_label.setText("Gap threshold must be positive.")
            return None
        masks = self._source_masks()
        if primary not in masks or secondary not in masks:
            self.status_label.setText("Source files not found in the loaded data.")
            return None
        return dataset, primary, secondary, threshold, masks

    def _compute_gaps(self, dataset, masks, primary, threshold) -> List[Tuple[float, float]]:
        primary_mask = masks[primary] & dataset.active_mask()
        return ops.find_gaps_in_epochs(dataset.time_epoch[primary_mask], threshold)

    def find_gaps(self) -> None:
        inputs = self._gap_inputs()
        if inputs is None:
            return
        dataset, primary, secondary, threshold, masks = inputs
        self._gaps = self._compute_gaps(dataset, masks, primary, threshold)
        fillers = ops.count_in_gaps(dataset.time_epoch[masks[secondary]], self._gaps)

        table = self.gaps_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        for (start, end), count in zip(self._gaps, fillers):
            row = table.rowCount()
            table.insertRow(row)
            cells = [_epoch_to_iso(start), _epoch_to_iso(end), int(round(end - start)), int(count)]
            for col, value in enumerate(cells):
                table.setItem(row, col, _cell(value))
        table.setSortingEnabled(True)
        self._update_buttons()
        self.status_label.setText(
            f"{len(self._gaps)} gap(s) > {threshold:.0f}s in '{primary}' (active rows); "
            f"{sum(fillers):,} secondary row(s) would fill them."
        )

    def apply_gap_fill(self) -> None:
        bundle = self._require_gpkg_layer()
        if bundle is None:
            return
        layer, gpkg_path, name = bundle
        inputs = self._gap_inputs()
        if inputs is None:
            return
        dataset, primary, secondary, threshold, masks = inputs
        gaps = self._compute_gaps(dataset, masks, primary, threshold)
        if not gaps:
            self.status_label.setText("No gaps to fill - nothing changed.")
            return

        secondary_mask = masks[secondary]
        secondary_indices = np.nonzero(secondary_mask)[0]
        wanted = np.array(
            ops.classify_gap_fill(dataset.time_epoch[secondary_mask], gaps), dtype=object
        )
        current = dataset.status_array()
        if current is None:
            # No column yet: only rows that need a non-default value are written.
            keep = wanted != ""
        else:
            sub = current[secondary_indices]
            keep = (sub != ops.STATUS_EXCLUDED) & (sub != wanted)  # excluded stays excluded
        fid_to_status: Dict[int, str] = {
            int(fid): str(status)
            for fid, status in zip(dataset.fids[secondary_indices][keep], wanted[keep])
        }
        activated = int(sum(1 for s in fid_to_status.values() if s == ops.STATUS_ACTIVE))

        def work(private_layer, feedback):
            changed = ops.apply_status(private_layer, fid_to_status, feedback=feedback)
            return {
                "changed": changed, "activated": activated, "gpkg_path": gpkg_path,
                "name": name, "primary": primary, "secondary": secondary,
                "threshold": threshold, "gaps": len(gaps),
            }

        self._start_edit("Apply gap fill", gpkg_path, name, work, self._finish_gap_fill)

    def _finish_gap_fill(self, result: Dict) -> None:
        changed, activated = result["changed"], result["activated"]
        self._after_edit(
            result["gpkg_path"], result["name"], "gap_fill",
            {
                "primary": result["primary"], "secondary": result["secondary"],
                "threshold_s": result["threshold"], "gaps": result["gaps"],
            },
            changed,
            f"{result['gaps']} gap(s); {activated} secondary row(s) activated, "
            f"{changed - activated} set to standby",
        )
        self.status_label.setText(
            f"Gap fill applied: {activated:,} row(s) from '{result['secondary']}' activated "
            f"across {result['gaps']} gap(s); {changed - activated:,} set to standby."
        )

    # -- history -----------------------------------------------------------
    def refresh_history(self) -> None:
        self._history_dirty = False
        table = self.history_table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        gpkg_path = self.controller.gpkg_path()
        if not gpkg_path:
            table.setSortingEnabled(True)
            return
        entries: List[Tuple[str, str, str, str]] = []
        imports = clp.open_gpkg_layer(
            gpkg_path, clp.prefixed_layer_name(gpkg_path, "import_log")
        )
        if imports is not None:
            for f in imports.getFeatures():
                entries.append((
                    str(f["imported_at"] or ""), "import",
                    f"{f['source_file']} -> {f['layer_name']}",
                    f"{f['rows_parsed']} row(s), start date {f['start_date'] or '-'} "
                    f"({f['algorithm']})",
                ))
        edits = clp.open_gpkg_layer(
            gpkg_path, clp.prefixed_layer_name(gpkg_path, "edit_log")
        )
        if edits is not None:
            for f in edits.getFeatures():
                entries.append((
                    str(f["edited_at"] or ""), "edit",
                    f"{f['operation']} on {f['layer_name']}",
                    str(f["details"] or ""),
                ))
        entries.sort(key=lambda e: e[0], reverse=True)
        for stamp, kind, what, details in entries:
            row = table.rowCount()
            table.insertRow(row)
            for col, text in enumerate((stamp, kind, what, details)):
                table.setItem(row, col, _cell(text))
        table.setSortingEnabled(True)

    # -- shared post-edit plumbing ----------------------------------------
    def _after_edit(
        self, gpkg_path: str, layer_name: str, operation: str,
        params: Dict, rows_affected: int, details: str,
    ) -> None:
        try:
            clp.log_edit(
                gpkg_path,
                self.controller.transform_context(),
                {
                    "layer_name": layer_name,
                    "operation": operation,
                    "params_json": json.dumps(params, sort_keys=True),
                    "rows_affected": int(rows_affected),
                    "details": details,
                },
            )
        except Exception:
            self.status_label.setText("Warning: could not write to the edit log.")
        # The edit went through a private connection: make the project layer
        # (and any other loaded copy of the table) see the new rows / fields.
        ops.reload_project_layers(gpkg_path, layer_name)
        self._recorded = None
        self._history_dirty = True
        self._gaps = []
        # Re-read the datasets so the table/plots/summary reflect the edit.
        self.controller.reload_dataset()
        self._refresh_visible()
