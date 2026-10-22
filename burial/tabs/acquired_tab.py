# -*- coding: utf-8 -*-
"""The Acquired Data tab: the project's burial data file, its datasets and their deliveries.

Left, the datasets (one stream of data each: a plough log, a survey); right,
the selected dataset's files as delivered: received, rows kept and rejected,
time and KP range, and whether each is current, replaced or withdrawn.
*Add data…* opens the import wizard; *Overlaps…* lists where deliveries
overlap and lets a rule change with a reason (the old decision is kept);
*File ▾* saves an original as delivered (checked against its checksum),
lists its rejected rows, or withdraws / restores it with a reason;
*History…* is the data file's log.

The tab reads nothing while a background task writes the file.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QBrush, QColor
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...plotkit.widgets import entries_button
from ...plugin_log import log_exception
from ...qgis_compat import (BUTTON_BOX_CANCEL, BUTTON_BOX_CLOSE, BUTTON_BOX_OK, DIALOG_ACCEPTED, EDIT_TRIGGER_NONE,
                            SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_SINGLE, qt_exec)
from ..acquired import location
from ..acquired import overlap as ov
from ..acquired.assessment import utc_text
from ..acquired.help import show_help
from ..acquired.import_wizard import ImportWizard, show_rejected
from ..acquired.store import KINDS, AcquiredStoreError
from ..ui_helpers import hint_style, status_style

_USER = getattr(getattr(Qt, "ItemDataRole", Qt), "UserRole")
_HORIZONTAL = getattr(getattr(Qt, "Orientation", Qt), "Horizontal")
_STATUS_TEXT = {"current": "Current", "replaced": "Replaced", "withdrawn": "Withdrawn"}


def _kp_text(lo, hi) -> str:
    return f"{lo:.3f} … {hi:.3f}" if lo is not None and hi is not None else ""


class AcquiredDataTab(QWidget):
    def __init__(self, controller, model, dock=None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.model = model
        self.dock = dock
        self._datasets: List[Dict] = []
        self._files: List[Dict] = []

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.file_label = QLabel("")
        self.file_label.setWordWrap(True)
        self.file_label.setTextInteractionFlags(getattr(getattr(Qt, "TextInteractionFlag", Qt),
                                                        "TextSelectableByMouse"))
        top.addWidget(self.file_label, 1)
        top.addWidget(entries_button("Data file", "The burial data file this project uses (beside the plan file).",
                                     [("Open another data file…", self._open_file),
                                      ("Start a new data file…", self._new_file)]))
        help_button = QPushButton("Help")
        help_button.clicked.connect(lambda: show_help(self))
        top.addWidget(help_button)
        layout.addLayout(top)

        splitter = QSplitter(_HORIZONTAL)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("<b>Datasets</b>"))
        self.dataset_list = QListWidget()
        self.dataset_list.setWordWrap(True)
        self.dataset_list.setToolTip("Each dataset is one stream of data. Where datasets overlap, the latest data "
                                     "wins in the Assessment (ties go to the dataset listed first).")
        self.dataset_list.currentRowChanged.connect(lambda *_: self._show_dataset())
        left_layout.addWidget(self.dataset_list, 1)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Add data…")
        self.add_button.setToolTip("Import deliveries: into an existing dataset or a new one.")
        self.add_button.clicked.connect(lambda: self.add_data(None))
        buttons.addWidget(self.add_button)
        self.dataset_menu = entries_button("Dataset", "Rename, reorder or archive the selected dataset.", [
            ("Rename…", self._rename), ("Change type…", self._change_type), None,
            ("Move up", lambda: self._move(-1)), ("Move down", lambda: self._move(1)), None,
            ("Archive…", self._archive)])
        buttons.addWidget(self.dataset_menu)
        buttons.addStretch(1)
        left_layout.addLayout(buttons)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel("")
        self.header.setWordWrap(True)
        right_layout.addWidget(self.header)
        self.files_table = QTableWidget(0, 7)
        self.files_table.setHorizontalHeaderLabels(["File", "Received", "Rows", "Rejected", "Time (UTC)", "KP",
                                                    "Status"])
        self.files_table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.files_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.files_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.files_table.cellDoubleClicked.connect(lambda *_: self._show_rejected())
        right_layout.addWidget(self.files_table, 1)
        actions = QHBoxLayout()
        self.add_files_button = QPushButton("Add files…")
        self.add_files_button.setToolTip("More deliveries for this dataset.")
        self.add_files_button.clicked.connect(lambda: self.add_data(self._dataset_id()))
        actions.addWidget(self.add_files_button)
        self.overlaps_button = QPushButton("Overlaps…")
        self.overlaps_button.setToolTip("Where this dataset's deliveries overlap in time, and the rule for each.")
        self.overlaps_button.clicked.connect(self._overlaps)
        actions.addWidget(self.overlaps_button)
        self.file_menu = entries_button("File", "The selected file.", [
            ("Save the original as delivered…", self._save_original), ("Rejected rows…", self._show_rejected),
            None, ("Withdraw…", lambda: self._set_status("withdrawn")),
            ("Restore…", lambda: self._set_status("current"))])
        actions.addWidget(self.file_menu)
        self.check_button = QPushButton("Check KPs")
        self.check_button.setToolTip("Compare the delivered KPs with the records' positions on the RPL they refer "
                                     "to (nothing is changed; the result goes in the history).")
        self.check_button.clicked.connect(self._check_kps)
        actions.addWidget(self.check_button)
        history = QPushButton("History…")
        history.setToolTip("Everything done to this dataset: imports, replacements, overlap rules, statuses.")
        history.clicked.connect(self._history)
        actions.addWidget(history)
        actions.addStretch(1)
        right_layout.addLayout(actions)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(hint_style())
        right_layout.addWidget(self.note)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([300, 800])
        layout.addWidget(splitter, 1)

        busy = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.stage = QLabel("")
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.controller.cancel)
        busy.addWidget(self.stage, 1)
        busy.addWidget(self.progress)
        busy.addWidget(self.cancel_button)
        self.busy_row = QWidget()
        self.busy_row.setLayout(busy)
        self.busy_row.hide()
        layout.addWidget(self.busy_row)

        self.controller.changed.connect(self.refresh)
        self.controller.busyChanged.connect(self._busy)
        self.controller.progressChanged.connect(self._progress)
        self.refresh()

    # -- state ------------------------------------------------------------------------
    def _busy(self, busy: bool) -> None:
        self.busy_row.setVisible(busy)
        for widget in (self.add_button, self.add_files_button, self.overlaps_button, self.dataset_menu,
                       self.file_menu, self.check_button):
            widget.setEnabled(not busy)
        if not busy:
            self.refresh()

    def _progress(self, value: float, stage: str) -> None:
        self.progress.setValue(int(value))
        self.stage.setText(stage)

    def refresh(self, *_args) -> None:
        controller = self.controller
        if controller.busy:
            return
        if controller.store is not None:
            self.file_label.setText(f"Burial data file: <b>{os.path.basename(controller.store.path)}</b> "
                                    f"<span style='{hint_style()}'>({os.path.dirname(controller.store.path)})</span>")
        elif controller.state == location.STATE_MISSING:
            self.file_label.setText(f"<span style='{status_style('error')}'>The project's burial data file is "
                                    f"missing: {controller.path}</span> Use Data file ▾ to find it.")
        elif controller.state == location.STATE_INVALID:
            self.file_label.setText(f"<span style='{status_style('error')}'>{controller.path} is not a burial "
                                    "data file.</span>")
        else:
            try:
                where = controller.default_path()
            except Exception:
                where = "beside the plan file"
            self.file_label.setText("No burial data in this project yet. <b>Add data…</b> starts the data file "
                                    f"<span style='{hint_style()}'>{where}</span>.")
        selected = self._dataset_id()
        self._datasets = controller.datasets()
        self.dataset_list.blockSignals(True)
        self.dataset_list.clear()
        for dataset in self._datasets:
            files = self._file_rows(dataset["dataset_id"])
            current = [f for f in files if f["status"] == "current"]
            lo = min((f["kp_min"] for f in current if f["kp_min"] is not None), default=None)
            hi = max((f["kp_max"] for f in current if f["kp_max"] is not None), default=None)
            latest = max((f["time_max"] for f in current if f["time_max"] is not None), default=None)
            when = utc_text(latest)[:10] if latest is not None else str(
                max((f.get("acquired_utc") or "" for f in current), default=""))[:10]
            item = QListWidgetItem(f"{dataset['name']}\n{KINDS.get(dataset.get('kind'), '')} · {len(current)} "
                                   f"file(s) · KP {_kp_text(lo, hi) or '–'}" + (f" · {when}" if when else ""))
            item.setData(_USER, dataset["dataset_id"])
            item.setToolTip(f"KPs referenced to {dataset.get('rpl_label') or 'this plan RPL'}")
            self.dataset_list.addItem(item)
        self.dataset_list.blockSignals(False)
        ids = [d["dataset_id"] for d in self._datasets]
        if ids:
            self.dataset_list.setCurrentRow(ids.index(selected) if selected in ids else 0)
        self._show_dataset()

    def _file_rows(self, dataset_id: str) -> List[Dict]:
        store = self.controller.store
        if store is None or self.controller.busy:
            return []
        try:
            return store.list_files(dataset_id)
        except AcquiredStoreError:
            log_exception("Burial Planner: reading the burial data file failed")
            return []

    def _dataset_id(self) -> Optional[str]:
        item = self.dataset_list.currentItem()
        return item.data(_USER) if item is not None else None

    def _dataset(self) -> Optional[Dict]:
        dataset_id = self._dataset_id()
        return next((d for d in self._datasets if d["dataset_id"] == dataset_id), None)

    def _show_dataset(self) -> None:
        dataset = self._dataset()
        has = dataset is not None
        for widget in (self.add_files_button, self.overlaps_button, self.file_menu, self.dataset_menu,
                       self.check_button):
            widget.setEnabled(has and not self.controller.busy)
        if not has:
            self.header.setText("")
            self.files_table.setRowCount(0)
            self.note.setText("")
            return
        self.header.setText(f"<b>{dataset['name']}</b> · {KINDS.get(dataset.get('kind'), '')} · DOL in m below mean "
                            f"seabed level, positive down · KPs referenced to "
                            f"{dataset.get('rpl_label') or 'this plan RPL'}")
        self._files = self._file_rows(dataset["dataset_id"])
        table = self.files_table
        table.setRowCount(len(self._files))
        for row, f in enumerate(self._files):
            if f.get("time_min") is not None:
                time_text = f"{utc_text(f['time_min'])} … {utc_text(f['time_max'])}"
            else:
                time_text = f"acquired {str(f.get('acquired_utc') or '')[:10]}"
            status = _STATUS_TEXT.get(f["status"], f["status"])
            if f["status"] != "current" and f.get("status_reason"):
                status += f": {f['status_reason']}"
            values = [f["name"], str(f.get("received_utc") or "")[:10], f"{f.get('rows_kept') or 0:,}",
                      f"{f.get('rows_rejected') or 0:,}", time_text, _kp_text(f.get("kp_min"), f.get("kp_max")),
                      status]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if f["status"] != "current":
                    item.setForeground(QBrush(QColor(140, 140, 140)))
                if col == 0:
                    item.setToolTip(f"sha256 {f.get('sha256')}\nimported {f.get('imported_utc')} by {f.get('author')}")
                table.setItem(row, col, item)
        table.resizeColumnsToContents()
        overlaps = self._overlap_rows(dataset["dataset_id"])
        notes = []
        if overlaps:
            changed = sum(1 for o in overlaps if o["rule"] != ov.RULE_NEWER)
            notes.append(f"{len(overlaps)} overlap(s) between deliveries"
                         + (f", {changed} not 'newer wins'" if changed else " (newer wins)"))
        rejected = sum(int(f.get("rows_rejected") or 0) for f in self._files if f["status"] == "current")
        if rejected:
            notes.append(f"{rejected:,} row(s) rejected on import (double-click a file to see them)")
        self.note.setText(" · ".join(notes))

    def _overlap_rows(self, dataset_id: str, include_superseded: bool = False) -> List[Dict]:
        store = self.controller.store
        if store is None or self.controller.busy:
            return []
        try:
            return store.list_overlaps(dataset_id, include_superseded)
        except AcquiredStoreError:
            return []

    def _file(self) -> Optional[Dict]:
        row = self.files_table.currentRow()
        return self._files[row] if 0 <= row < len(self._files) else None

    # -- actions ----------------------------------------------------------------------
    def add_data(self, dataset_id: Optional[str], paths: Optional[List[str]] = None) -> Optional[str]:
        if self.controller.busy:
            return None
        wizard = ImportWizard(self.controller, self.model, dataset_id, paths, parent=self)
        qt_exec(wizard)
        result = wizard.result
        self.refresh()
        if result is not None:
            ids = [d["dataset_id"] for d in self._datasets]
            if result.dataset_id in ids:
                self.dataset_list.setCurrentRow(ids.index(result.dataset_id))
            self.note.setText(f"Imported {len(result.imported)} file(s), {result.rows:,} rows."
                              + (f" Skipped: {', '.join(n for n, _w in result.skipped)}." if result.skipped else ""))
            self.controller.changed.emit()
            return result.dataset_id
        return None

    def _open_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Open a burial data file", os.path.dirname(
            self.controller.path or ""), "GeoPackage (*.gpkg)")
        if path:
            self._use_file(path)

    def _new_file(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(self, "Start a burial data file", self.controller.default_path(),
                                                    "GeoPackage (*.gpkg)")
        if path:
            self._use_file(path)

    def _use_file(self, path: str) -> None:
        try:
            self.controller.open_file(path)
        except AcquiredStoreError as exc:
            QMessageBox.warning(self, "Burial data file", str(exc))

    def _ask(self, title: str, prompt: str) -> Optional[str]:
        text, ok = QInputDialog.getText(self, title, prompt)
        if not ok:
            return None
        if not text.strip():
            QMessageBox.information(self, title, "Give a reason: it is kept in the data file's history.")
            return None
        return text.strip()

    def _write(self, action) -> None:
        try:
            action()
        except AcquiredStoreError as exc:
            QMessageBox.warning(self, "Burial data", str(exc))
        self.controller.changed.emit()

    def _rename(self) -> None:
        dataset = self._dataset()
        if dataset is None:
            return
        text, ok = QInputDialog.getText(self, "Rename dataset", "Name:", text=dataset["name"])
        if ok and text.strip() and text.strip() != dataset["name"]:
            self._write(lambda: self.controller.store.update_dataset(dataset["dataset_id"], {"name": text.strip()},
                                                                     reason="renamed"))

    def _change_type(self) -> None:
        dataset = self._dataset()
        if dataset is None:
            return
        labels = list(KINDS.values())
        current = labels.index(KINDS.get(dataset.get("kind"), labels[0])) if dataset.get("kind") in KINDS else 0
        text, ok = QInputDialog.getItem(self, "Dataset type", "Type:", labels, current, False)
        if ok:
            kind = next(k for k, v in KINDS.items() if v == text)
            self._write(lambda: self.controller.store.update_dataset(dataset["dataset_id"], {"kind": kind},
                                                                     reason="type changed"))

    def _move(self, step: int) -> None:
        dataset = self._dataset()
        if dataset is None:
            return
        ids = [d["dataset_id"] for d in self._datasets]
        index = ids.index(dataset["dataset_id"])
        target = index + step
        if not 0 <= target < len(ids):
            return
        ids[index], ids[target] = ids[target], ids[index]

        def reorder():
            with self.controller.store.batch("reorder the datasets"):
                for order, dataset_id in enumerate(ids):
                    self.controller.store.update_dataset(dataset_id, {"sort_order": order}, reason="reordered")
        self._write(reorder)
        self.dataset_list.setCurrentRow(target)

    def _archive(self) -> None:
        dataset = self._dataset()
        if dataset is None:
            return
        reason = self._ask("Archive dataset", f"Why archive {dataset['name']}? (It stays in the data file; it is "
                                              "no longer listed or assessed.)")
        if reason:
            self._write(lambda: self.controller.store.update_dataset(dataset["dataset_id"], {"status": "archived"},
                                                                     reason=reason))

    def _save_original(self) -> None:
        f = self._file()
        if f is None:
            return
        try:
            name, content = self.controller.store.original(f["file_id"])
        except AcquiredStoreError as exc:
            QMessageBox.warning(self, "Original file", str(exc))
            return
        path, _filter = QFileDialog.getSaveFileName(self, "Save the original as delivered", name)
        if not path:
            return
        try:
            with open(path, "wb") as handle:
                handle.write(content)
        except OSError as exc:
            QMessageBox.warning(self, "Original file", f"Could not write {path}: {exc.strerror or exc}")

    def _show_rejected(self) -> None:
        f = self._file()
        if f is None:
            return
        try:
            rows = self.controller.store.rejected_rows(f["file_id"])
        except AcquiredStoreError as exc:
            QMessageBox.warning(self, "Rejected rows", str(exc))
            return
        show_rejected(self, f["name"], rows, len(rows))

    def _set_status(self, status: str) -> None:
        f = self._file()
        if f is None or f["status"] == status:
            return
        verb = "Withdraw" if status == "withdrawn" else "Restore"
        reason = self._ask(f"{verb} {f['name']}", f"Why {verb.lower()} {f['name']}?")
        if not reason:
            return
        store = self.controller.store

        def apply():
            with store.batch(f"{verb.lower()} the file"):
                store.set_file_status(f["file_id"], status, reason)
                if status == "current":
                    found = restored_overlaps(store, f["dataset_id"], f["file_id"])
                    if found:
                        store.add_overlaps(f["dataset_id"], found, f"found when {f['name']} was restored")
        self._write(apply)

    def check_kps(self) -> Optional[object]:
        """Verify the selected dataset's KPs (see :mod:`..acquired.kp_check`); the verdict or None."""
        dataset = self._dataset()
        store = self.controller.store
        if dataset is None or store is None or self.controller.busy:
            return None
        from qgis.PyQt.QtWidgets import QApplication
        from ..acquired import kp_check
        QApplication.setOverrideCursor(getattr(getattr(Qt, "CursorShape", Qt), "WaitCursor"))
        try:
            verdict = kp_check.check_dataset(store, self.model, dataset)
        except (ValueError, AcquiredStoreError) as exc:
            verdict = kp_check.KpVerification(message=str(exc))
        finally:
            QApplication.restoreOverrideCursor()
        if verdict.compared:
            try:
                store.record("check_kps", dataset["dataset_id"], {
                    "route": verdict.route_label, "records": verdict.compared,
                    "median_m": round(verdict.median_diff_m, 3), "max_m": round(verdict.max_abs_diff_m, 3),
                    "dcc_median_m": round(verdict.dcc_median_m, 2), "level": verdict.level})
            except AcquiredStoreError:
                log_exception("Burial Planner: recording the KP check failed")
        self.note.setText(verdict.summary())
        self.note.setStyleSheet(status_style(verdict.level) if verdict.level != "ok" else status_style("ok"))
        return verdict

    def _check_kps(self) -> None:
        self.check_kps()

    def _overlaps(self) -> None:
        dataset = self._dataset()
        if dataset is None:
            return
        OverlapsDialog(self, self.controller.store, dataset).exec_or_show()
        self.controller.changed.emit()

    def _history(self) -> None:
        dataset = self._dataset()
        store = self.controller.store
        if store is None:
            return
        try:
            rows = store.log(dataset["dataset_id"] if dataset else None)
        except AcquiredStoreError as exc:
            QMessageBox.warning(self, "History", str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("History" + (f": {dataset['name']}" if dataset else ""))
        dialog.resize(900, 420)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(len(rows), 4)
        table.setHorizontalHeaderLabels(["When (UTC)", "Who", "What", "Details"])
        table.setEditTriggers(EDIT_TRIGGER_NONE)
        for r, row in enumerate(rows):
            detail = row.get("detail") or {}
            text = ", ".join(f"{k}: {v}" for k, v in detail.items() if v not in (None, "", [], {}))
            for c, value in enumerate([str(row.get("created_utc") or "").replace("T", " ").rstrip("Z"),
                                       row.get("author") or "", (row.get("action") or "").replace("_", " "), text]):
                table.setItem(r, c, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(BUTTON_BOX_CLOSE)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        qt_exec(dialog)


def restored_overlaps(store, dataset_id: str, file_id: str) -> List[ov.Overlap]:
    """Overlaps of a restored file with the dataset's other current files (the later delivery is the newer)."""
    times = {f.file_id: f for f in store.file_times(dataset_id)}
    mine = times.get(file_id)
    if mine is None:
        return []
    found = []
    for other in times.values():
        if other.file_id == file_id:
            continue
        older, newer = (other, mine) if other.seq < mine.seq else (mine, other)
        found.extend(ov.find_overlaps([older], newer))
    return found


class OverlapsDialog(QDialog):
    """Where a dataset's deliveries overlap, and the rule for each (changes need a reason)."""

    def __init__(self, parent, store, dataset: Dict):
        super().__init__(parent)
        self.store = store
        self.dataset = dataset
        self.setWindowTitle(f"Overlaps: {dataset['name']}")
        self.resize(1000, 420)
        layout = QVBoxLayout(self)
        intro = QLabel("Where two deliveries of this dataset hold records for the same times, a rule says which "
                       "are used. Changing a rule keeps the old decision in the history.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Older file", "Newer file", "From – to (UTC)", "Records (older / newer)",
                                              "Rule", "Why", "By / when"])
        self.table.setEditTriggers(EDIT_TRIGGER_NONE)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(BUTTON_BOX_CLOSE)
        change = QPushButton("Change the rule…")
        change.clicked.connect(self._change)
        buttons.addButton(change, getattr(getattr(QDialogButtonBox, "ButtonRole", QDialogButtonBox), "ActionRole"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._rows: List[Dict] = []
        self._fill()

    def exec_or_show(self) -> None:
        qt_exec(self)

    def _fill(self) -> None:
        names = {f["file_id"]: f["name"] for f in self.store.list_files(self.dataset["dataset_id"])}
        self._rows = self.store.list_overlaps(self.dataset["dataset_id"])
        self.table.setRowCount(len(self._rows))
        for r, row in enumerate(self._rows):
            rule = ov.RULES.get(row["rule"], row["rule"])
            if row["rule"] == ov.RULE_SPLIT:
                rule += f" ({utc_text(row['split_utc'])})"
            values = [names.get(row["older_file_id"], "?"), names.get(row["newer_file_id"], "?"),
                      f"{utc_text(row['t0'])} – {utc_text(row['t1'])}",
                      f"{row['older_count']:,} / {row['newer_count']:,}", rule, row.get("reason") or "",
                      f"{row.get('author') or ''} {str(row.get('created_utc') or '')[:16].replace('T', ' ')}"]
            for c, value in enumerate(values):
                self.table.setItem(r, c, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

    def _change(self) -> None:
        row_index = self.table.currentRow()
        if not 0 <= row_index < len(self._rows):
            return
        row = self._rows[row_index]
        dialog = QDialog(self)
        dialog.setWindowTitle("Change the overlap rule")
        form = QFormLayout(dialog)
        rule = QComboBox()
        for key, text in ov.RULES.items():
            rule.addItem(text, key)
        rule.setCurrentIndex(max(0, rule.findData(row["rule"])))
        split = QLineEdit(utc_text((row["t0"] + row["t1"]) / 2.0) if row["split_utc"] is None
                          else utc_text(row["split_utc"]))
        split.setToolTip(f"UTC, between {utc_text(row['t0'])} and {utc_text(row['t1'])}")
        reason = QLineEdit()
        reason.setPlaceholderText("why (kept in the history)")
        form.addRow("Rule", rule)
        form.addRow("Split at (UTC)", split)
        form.addRow("Reason", reason)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        from ..acquired.import_wizard import _utc_seconds
        split_utc = _utc_seconds(split.text()) if rule.currentData() == ov.RULE_SPLIT else None
        try:
            self.store.change_overlap(row["overlap_id"], rule.currentData(), reason.text(), split_utc)
        except AcquiredStoreError as exc:
            QMessageBox.warning(self, "Overlap rule", str(exc))
            return
        self._fill()
