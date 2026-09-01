# -*- coding: utf-8 -*-
"""Manage panel: per-source-file data management for the active layer.

The control surface for what is *in* the GeoPackage (the importers own getting
data in). One row per ``source_file`` shows row count, time range, the start
date recorded in ``import_log`` and the record-status breakdown; actions fix a
wrong start date (in place, via :mod:`processing.cable_lay_manage_ops`), set
the curation status, remove a file's rows, fill primary gaps from a secondary
source, filter the map to active rows, and compact the GeoPackage. Every
operation is recorded in the ``edit_log`` table.

All data logic lives in ``cable_lay_manage_ops`` / ``cable_lay_parsers``; this
panel is just the UI around it. Summaries and gap analysis run on the already
loaded in-memory dataset (numpy), so they are instant even for huge layers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProviderRegistry

from ...qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    EDIT_TRIGGER_NONE,
    HEADER_RESIZE_MODE_STRETCH,
    MESSAGEBOX_NO,
    MESSAGEBOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_EXTENDED,
    qt_exec,
)
from ...processing import cable_lay_manage_ops as ops
from ...processing import cable_lay_parsers as clp

_EPOCH = datetime(1970, 1, 1)


def _epoch_to_iso(epoch: float) -> str:
    if epoch != epoch:  # NaN
        return ""
    return (_EPOCH + timedelta(seconds=float(epoch))).strftime("%Y-%m-%dT%H:%M:%S")


def _cell(text) -> QTableWidgetItem:
    return QTableWidgetItem("" if text is None else str(text))


class _StartDateDialog(QDialog):
    """Corrected (and optional previous) start date for the ISO-time fix."""

    def __init__(self, parent, sources: List[str], recorded: str):
        super().__init__(parent)
        self.setWindowTitle("Fix start date")
        form = QFormLayout(self)
        label = ", ".join(sources) if len(sources) <= 3 else f"{len(sources)} source files"
        form.addRow(QLabel(f"Recompute ISO_Time for: {label}"))
        self.corrected = QLineEdit()
        self.corrected.setPlaceholderText("YYYY-MM-DD")
        form.addRow("Corrected start date (day count 1):", self.corrected)
        self.previous = QLineEdit(recorded)
        self.previous.setPlaceholderText("YYYY-MM-DD (optional)")
        form.addRow("Previously used start date:", self.previous)
        form.addRow(
            QLabel(
                "The previous date is only needed for rows without a raw\n"
                "day-count time column (their ISO_Time is shifted instead)."
            )
        )
        buttons = QDialogButtonBox()
        buttons.setStandardButtons(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class ManagePanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._dataset = None
        self._gaps: List[Tuple[float, float]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_sources_tab(), "Sources")
        self.tabs.addTab(self._build_gaps_tab(), "Gap fill")
        self.tabs.addTab(self._build_history_tab(), "History")
        layout.addWidget(self.tabs, 1)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # -- UI construction ---------------------------------------------------
    def _build_sources_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)

        self.sources_table = QTableWidget(0, 8)
        self.sources_table.setHorizontalHeaderLabels(
            ["Source file", "Rows", "First time", "Last time", "Start date",
             "Active", "Standby", "Excluded"]
        )
        self.sources_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.sources_table.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.sources_table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.sources_table.horizontalHeader().setSectionResizeMode(0, HEADER_RESIZE_MODE_STRETCH)
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
        layout.addLayout(buttons)

        self.filter_check = QCheckBox("Map filter: show active rows only")
        self.filter_check.setToolTip(
            "Applies a provider filter (record_status active/empty) to the "
            "layer on the map. Uncheck to show everything again."
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
        form.addRow("Primary source:", self.primary_combo)
        self.secondary_combo = QComboBox()
        form.addRow("Secondary source:", self.secondary_combo)
        self.threshold_edit = QLineEdit("60")
        self.threshold_edit.setMaximumWidth(90)
        form.addRow("Gap threshold (s):", self.threshold_edit)
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
        self.history_table.horizontalHeader().setSectionResizeMode(3, HEADER_RESIZE_MODE_STRETCH)
        layout.addWidget(self.history_table, 1)
        return widget

    # -- data binding ------------------------------------------------------
    def set_dataset(self, dataset) -> None:
        self._dataset = dataset
        self._gaps = []
        self.gaps_table.setRowCount(0)
        self._populate_sources()
        self._populate_source_combos()
        self._sync_filter_check()
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

    def _require_gpkg_layer(self):
        """Return (layer, gpkg_path, physical_name) or None with a status hint."""
        layer = self.controller.layer
        gpkg_path = self.controller.gpkg_path()
        name = self._physical_layer_name()
        if layer is None or self._dataset is None:
            self.status_label.setText("Load a data layer first.")
            return None
        if not gpkg_path or not name:
            self.status_label.setText(
                "The active layer is not a GeoPackage layer, so it cannot be managed here."
            )
            return None
        return layer, gpkg_path, name

    def _selected_sources(self) -> List[str]:
        rows = self.sources_table.selectionModel().selectedRows()
        names = []
        for index in rows:
            item = self.sources_table.item(index.row(), 0)
            if item is not None and item.text():
                names.append(item.text())
        return names

    def _source_masks(self) -> Dict[str, np.ndarray]:
        """Boolean row mask per source-file value of the active dataset."""
        dataset = self._dataset
        if dataset is None or dataset.source_field is None:
            return {}
        values = np.array(
            [("" if v is None else str(v)) for v in dataset.source_array], dtype=object
        )
        return {name: values == name for name in sorted(set(values.tolist())) if name}

    def _status_array(self) -> Optional[np.ndarray]:
        dataset = self._dataset
        if dataset is None or ops.STATUS_FIELD not in dataset.columns:
            return None
        raw = dataset.columns[ops.STATUS_FIELD]
        return np.array([("" if v is None else str(v).strip()) for v in raw], dtype=object)

    def _active_mask(self) -> Optional[np.ndarray]:
        """Rows counting as active (status active, empty, NULL, or no column)."""
        dataset = self._dataset
        if dataset is None:
            return None
        statuses = self._status_array()
        if statuses is None:
            return np.ones(dataset.row_count, dtype=bool)
        return (statuses == "") | (statuses == "NULL") | (statuses == ops.STATUS_ACTIVE)

    # -- sources table -----------------------------------------------------
    def _populate_sources(self) -> None:
        self.sources_table.setRowCount(0)
        dataset = self._dataset
        if dataset is None:
            return
        masks = self._source_masks()
        statuses = self._status_array()
        recorded = self._recorded_start_dates()
        epochs = dataset.time_epoch

        for name, mask in masks.items():
            row = self.sources_table.rowCount()
            self.sources_table.insertRow(row)
            count = int(mask.sum())
            tmin = tmax = ""
            if epochs is not None:
                sub = epochs[mask]
                finite = sub[np.isfinite(sub)]
                if finite.size:
                    tmin = _epoch_to_iso(float(finite.min()))
                    tmax = _epoch_to_iso(float(finite.max()))
            active = standby = excluded = ""
            if statuses is not None:
                sub_status = statuses[mask]
                active = int(((sub_status == "") | (sub_status == "NULL")
                              | (sub_status == ops.STATUS_ACTIVE)).sum())
                standby = int((sub_status == ops.STATUS_STANDBY).sum())
                excluded = int((sub_status == ops.STATUS_EXCLUDED).sum())
            cells = [
                name, count, tmin, tmax, recorded.get(name, ""),
                active, standby, excluded,
            ]
            for col, text in enumerate(cells):
                self.sources_table.setItem(row, col, _cell(text))

    def _recorded_start_dates(self) -> Dict[str, str]:
        """Latest import_log start date per source file for the active layer."""
        gpkg_path = self.controller.gpkg_path()
        name = self._physical_layer_name()
        if not gpkg_path or not name:
            return {}
        log = clp.open_gpkg_layer(gpkg_path, clp.prefixed_layer_name(gpkg_path, "import_log"))
        if log is None:
            return {}
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
            return {}
        return {source: start for source, (_stamp, start) in latest.items()}

    def _populate_source_combos(self) -> None:
        names = list(self._source_masks().keys())
        for combo in (self.primary_combo, self.secondary_combo):
            current = combo.currentText()
            combo.clear()
            combo.addItems(names)
            if current in names:
                combo.setCurrentText(current)
        if len(names) >= 2 and self.secondary_combo.currentText() == self.primary_combo.currentText():
            self.secondary_combo.setCurrentIndex(1 if self.primary_combo.currentIndex() == 0 else 0)

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
        prefill = recorded.get(sources[0], "")
        dialog = _StartDateDialog(self, sources, prefill)
        if not qt_exec(dialog):
            return
        corrected = dialog.corrected.text().strip()
        previous = dialog.previous.text().strip()
        if not corrected:
            self.status_label.setText("A corrected start date is required.")
            return
        try:
            counts = ops.recompute_iso_time(
                layer, corrected, old_start_date=previous, source_files=sources
            )
            key_fields = clp.dedupe_key_for(ops.layer_type_for_name(name) or "")
            duplicates = ops.dedupe_layer_in_place(layer, key_fields, source_files=sources)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Fix start date", str(exc))
            return
        self._after_edit(
            gpkg_path, name, "recompute_iso_time",
            {"start_date": corrected, "old_start_date": previous, "source_files": sources},
            counts["updated"] + duplicates,
            f"updated={counts['updated']} unchanged={counts['unchanged']} "
            f"skipped={counts['skipped']} duplicates_removed={duplicates}",
        )
        self.status_label.setText(
            f"ISO_Time recomputed: {counts['updated']} updated, "
            f"{duplicates} duplicate(s) removed."
        )

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
        try:
            changed = ops.set_source_status(layer, status, sources)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Set status", str(exc))
            return
        self._after_edit(
            gpkg_path, name, "set_record_status",
            {"status": status, "source_files": sources}, changed,
            f"{changed} row(s) set to {status}",
        )
        self.status_label.setText(f"{changed} row(s) set to '{status}'.")

    def remove_sources(self) -> None:
        bundle = self._require_gpkg_layer()
        if bundle is None:
            return
        layer, gpkg_path, name = bundle
        sources = self._selected_sources()
        if not sources:
            self.status_label.setText("Select one or more source files first.")
            return
        answer = QMessageBox.question(
            self,
            "Remove rows",
            "Permanently delete every row of:\n  " + "\n  ".join(sources) +
            "\n\nThis cannot be undone (consider status 'excluded' instead). Continue?",
            MESSAGEBOX_YES | MESSAGEBOX_NO,
        )
        if answer != MESSAGEBOX_YES:
            return
        try:
            deleted = ops.delete_source_rows(layer, sources)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Remove rows", str(exc))
            return
        self._after_edit(
            gpkg_path, name, "delete_source_rows",
            {"source_files": sources}, deleted, f"{deleted} row(s) deleted",
        )
        self.status_label.setText(f"Deleted {deleted} row(s).")

    def toggle_active_filter(self, checked: bool) -> None:
        layer = self.controller.layer
        if layer is None:
            return
        if checked and self._status_array() is None:
            self.status_label.setText(
                "No record_status column yet - set a status or apply a gap fill first."
            )
            self.filter_check.blockSignals(True)
            self.filter_check.setChecked(False)
            self.filter_check.blockSignals(False)
            return
        expression = ops.active_subset_expression() if checked else ""
        current = layer.subsetString() or ""
        if current == expression:
            return
        if current and checked:
            answer = QMessageBox.question(
                self,
                "Map filter",
                "The layer already has a provider filter:\n"
                f"{current}\n\nReplace it with the active-rows filter?",
                MESSAGEBOX_YES | MESSAGEBOX_NO,
            )
            if answer != MESSAGEBOX_YES:
                self._sync_filter_check()
                return
        layer.setSubsetString(expression)
        layer.triggerRepaint()
        self.controller.reload_dataset()
        self.status_label.setText(
            "Map filter applied (active rows only)." if checked else "Map filter cleared."
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
        saved = max(0, before - after) / (1024 * 1024)
        self.status_label.setText(
            f"Compacted: {before / (1024 * 1024):.1f} MB -> "
            f"{after / (1024 * 1024):.1f} MB ({saved:.1f} MB reclaimed)."
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
        try:
            threshold = float(self.threshold_edit.text())
        except ValueError:
            self.status_label.setText("Gap threshold must be a number of seconds.")
            return None
        if threshold <= 0:
            self.status_label.setText("Gap threshold must be positive.")
            return None
        masks = self._source_masks()
        if primary not in masks or secondary not in masks:
            self.status_label.setText("Source files not found in the loaded data.")
            return None
        return dataset, primary, secondary, threshold, masks

    def _compute_gaps(self, dataset, masks, primary, threshold) -> List[Tuple[float, float]]:
        active = self._active_mask()
        primary_mask = masks[primary] & (active if active is not None else True)
        primary_epochs = dataset.time_epoch[primary_mask]
        return ops.find_gaps_in_epochs(primary_epochs.tolist(), threshold)

    def find_gaps(self) -> None:
        inputs = self._gap_inputs()
        if inputs is None:
            return
        dataset, primary, secondary, threshold, masks = inputs
        self._gaps = self._compute_gaps(dataset, masks, primary, threshold)
        secondary_epochs = dataset.time_epoch[masks[secondary]]

        self.gaps_table.setRowCount(0)
        for start, end in self._gaps:
            row = self.gaps_table.rowCount()
            self.gaps_table.insertRow(row)
            fillers = int(sum(1 for t in secondary_epochs if start < t < end))
            cells = [_epoch_to_iso(start), _epoch_to_iso(end), f"{end - start:.0f}", fillers]
            for col, text in enumerate(cells):
                self.gaps_table.setItem(row, col, _cell(text))
        self.status_label.setText(
            f"{len(self._gaps)} gap(s) > {threshold:.0f}s in '{primary}' (active rows)."
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
        secondary_epochs = dataset.time_epoch[secondary_mask]
        statuses = ops.classify_gap_fill(secondary_epochs.tolist(), gaps)

        current = self._status_array()
        fid_to_status: Dict[int, str] = {}
        for index, status in zip(secondary_indices, statuses):
            if current is not None and current[index] == ops.STATUS_EXCLUDED:
                continue  # curated-out rows stay excluded
            if current is not None and current[index] == status:
                continue
            if current is None and status == "":
                continue
            fid_to_status[int(dataset.fids[index])] = status

        activated = sum(1 for s in fid_to_status.values() if s == ops.STATUS_ACTIVE)
        try:
            changed = ops.apply_status(layer, fid_to_status)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Apply gap fill", str(exc))
            return
        self._after_edit(
            gpkg_path, name, "gap_fill",
            {
                "primary": primary, "secondary": secondary,
                "threshold_s": threshold, "gaps": len(gaps),
            },
            changed,
            f"{len(gaps)} gap(s); {activated} secondary row(s) activated, "
            f"{changed - activated} set to standby",
        )
        self.status_label.setText(
            f"Gap fill applied: {activated} row(s) from '{secondary}' activated "
            f"across {len(gaps)} gap(s); the rest set to standby."
        )

    # -- history -----------------------------------------------------------
    def refresh_history(self) -> None:
        self.history_table.setRowCount(0)
        gpkg_path = self.controller.gpkg_path()
        if not gpkg_path:
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
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            for col, text in enumerate((stamp, kind, what, details)):
                self.history_table.setItem(row, col, _cell(text))

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
        layer = self.controller.layer
        if layer is not None:
            layer.triggerRepaint()
        # Re-read the datasets so the table/plots/summary reflect the edit.
        self.controller.reload_dataset()
        self.refresh_history()
