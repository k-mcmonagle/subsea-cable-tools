# -*- coding: utf-8 -*-
"""*Add data*: the import wizard for acquired burial data.

1. **Files** — pick or drop deliveries; they import in the listed order
   (a later file is the newer delivery).
2. **Dataset** — add to an existing dataset or start one: name, type, who
   delivered it, and the RPL its KPs were quoted against.
3. **Columns** — the mapping screen (:mod:`.mapping_widget`) on a preview of
   one of the files. A dataset's saved mapping applies itself when the
   column names match.
4. **Review** — every file read through the mapping in the background:
   rows kept and rejected, files already imported (skipped), a current file
   of the same name (replaced, or keep both), overlaps in time with their
   rule (newer wins unless changed), and the acquisition date when a file
   has no time column. *Import* writes everything in one transaction in the
   background; Cancel leaves the data file as it was.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QDate, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
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
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from ...qgis_compat import (BUTTON_BOX_CLOSE, EDIT_TRIGGER_NONE, SELECTION_BEHAVIOR_SELECT_ROWS,
                            SELECTION_MODE_EXTENDED, SELECTION_MODE_SINGLE, qt_exec)
from ..ui_helpers import hint_style, status_style
from . import importer as I
from . import overlap as ov
from .assessment import utc_text
from .controller import file_dialog_filter
from .mapping_widget import ColumnMappingWidget
from .store import KINDS, StoreReader

_CHECKED = getattr(getattr(Qt, "CheckState", Qt), "Checked")
_UNCHECKED = getattr(getattr(Qt, "CheckState", Qt), "Unchecked")
_USER = getattr(getattr(Qt, "ItemDataRole", Qt), "UserRole")
_ITEM_CHECKABLE = getattr(getattr(Qt, "ItemFlag", Qt), "ItemIsUserCheckable")
_ITEM_ENABLED = getattr(getattr(Qt, "ItemFlag", Qt), "ItemIsEnabled")
_ITEM_SELECTABLE = getattr(getattr(Qt, "ItemFlag", Qt), "ItemIsSelectable")
_RULE_LABELS = [(ov.RULE_NEWER, "Newer delivery wins"), (ov.RULE_EXISTING, "Keep the older file's records"),
                (ov.RULE_BOTH, "Keep both (separate passes)"), (ov.RULE_SPLIT, "Split at a time…")]


class _FileList(QListWidget):
    """The file list: accepts files dropped from the file manager."""

    filesDropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):  # noqa: N802 (Qt API)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802 (Qt API)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802 (Qt API)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.filesDropped.emit([p for p in paths if os.path.isfile(p)])
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class FilesPage(QWizardPage):
    def __init__(self, wizard: "ImportWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Files")
        self.setSubTitle("Choose the deliveries to import, or drop them on the list. They import in this order: "
                         "a later file is the newer delivery.")
        layout = QVBoxLayout(self)
        self.list = _FileList()
        self.list.setSelectionMode(SELECTION_MODE_EXTENDED)
        self.list.filesDropped.connect(self.add_paths)
        layout.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        for text, slot in (("Add files…", self._browse), ("Remove", self._remove), ("Up", lambda: self._move(-1)),
                           ("Down", lambda: self._move(1)), ("Sort by name", self._sort)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.add_paths(wizard.initial_paths)

    def paths(self) -> List[str]:
        return [self.list.item(i).data(_USER) for i in range(self.list.count())]

    def add_paths(self, paths: List[str]) -> None:
        known = set(self.paths())
        for path in sorted(paths, key=lambda p: os.path.basename(p).casefold()):
            if path in known:
                continue
            item = QListWidgetItem(os.path.basename(path))
            self.list.addItem(item)
            item.setData(_USER, path)
            item.setToolTip(path)
            known.add(path)
        self.completeChanged.emit()

    def _browse(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(self, "Burial data files", self.wiz.last_folder(),
                                                      file_dialog_filter())
        if paths:
            self.wiz.remember_folder(paths[0])
            self.add_paths(paths)

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.completeChanged.emit()

    def _move(self, step: int) -> None:
        row = self.list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)

    def _sort(self) -> None:
        self.list.sortItems()

    def isComplete(self) -> bool:  # noqa: N802 (Qt API)
        return self.list.count() > 0

    def validatePage(self) -> bool:  # noqa: N802 (Qt API)
        self.wiz.paths = self.paths()
        return True


class DatasetPage(QWizardPage):
    def __init__(self, wizard: "ImportWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Dataset")
        self.setSubTitle("A dataset is one stream of data, such as one plough's log or one survey. Add these files "
                         "to one, or start a new one.")
        layout = QVBoxLayout(self)
        self.existing_radio = QRadioButton("Add to the dataset")
        self.new_radio = QRadioButton("Start a new dataset")
        group = QButtonGroup(self)
        group.addButton(self.existing_radio)
        group.addButton(self.new_radio)
        row = QHBoxLayout()
        row.addWidget(self.existing_radio)
        self.dataset_combo = QComboBox()
        row.addWidget(self.dataset_combo, 1)
        layout.addLayout(row)
        self.existing_note = QLabel("")
        self.existing_note.setStyleSheet(hint_style())
        self.existing_note.setWordWrap(True)
        layout.addWidget(self.existing_note)
        layout.addWidget(self.new_radio)
        self.new_box = QWidget()
        form = QFormLayout(self.new_box)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Plough log, main lay")
        self.kind = QComboBox()
        for key, text in KINDS.items():
            self.kind.addItem(text, key)
        self.originator = QLineEdit()
        self.originator.setPlaceholderText("who delivered it (optional)")
        form.addRow("Name", self.name)
        form.addRow("Type", self.kind)
        form.addRow("Delivered by", self.originator)
        self.picker = None
        try:
            from ..rpl_reference import RplReferencePicker
            self.picker = RplReferencePicker(wizard.model, label="")
            form.addRow("KPs referenced to", self.picker)
        except Exception:
            form.addRow("KPs referenced to", QLabel("this plan's RPL"))
        layout.addWidget(self.new_box)
        self.received = QDateEdit()
        self.received.setCalendarPopup(True)
        self.received.setDisplayFormat("yyyy-MM-dd")
        self.received.setDate(QDate.currentDate())
        received_row = QFormLayout()
        received_row.addRow("Received", self.received)
        layout.addLayout(received_row)
        layout.addStretch(1)
        for dataset in wizard.datasets:
            self.dataset_combo.addItem(dataset["name"], dataset["dataset_id"])
        if wizard.datasets:
            index = max(0, self.dataset_combo.findData(wizard.initial_dataset)) if wizard.initial_dataset else 0
            self.dataset_combo.setCurrentIndex(index)
            self.existing_radio.setChecked(True)
        else:
            self.existing_radio.setEnabled(False)
            self.dataset_combo.setEnabled(False)
            self.new_radio.setChecked(True)
        self.existing_radio.toggled.connect(self._update)
        self.dataset_combo.currentIndexChanged.connect(self._update)
        self.name.textChanged.connect(lambda *_: self.completeChanged.emit())
        self._update()

    def initializePage(self) -> None:  # noqa: N802 (Qt API)
        if not self.name.text().strip() and self.wiz.paths:
            stem = os.path.splitext(os.path.basename(self.wiz.paths[0]))[0]
            self.name.setText(stem.replace("_", " "))

    def _update(self, *_args) -> None:
        existing = self.existing_radio.isChecked()
        self.dataset_combo.setEnabled(existing)
        self.new_box.setEnabled(not existing)
        dataset = self.wiz.dataset_row(self.dataset_combo.currentData()) if existing else None
        if dataset is not None:
            files = self.wiz.file_count(dataset["dataset_id"])
            self.existing_note.setText(f"{KINDS.get(dataset.get('kind'), '')} · {files} file(s) · KPs referenced to "
                                       f"{dataset.get('rpl_label') or 'this plan RPL'}")
        else:
            self.existing_note.setText("")
        self.completeChanged.emit()

    def isComplete(self) -> bool:  # noqa: N802 (Qt API)
        if self.existing_radio.isChecked():
            return self.dataset_combo.currentData() is not None
        return bool(self.name.text().strip())

    def target(self) -> I.DatasetTarget:
        if self.existing_radio.isChecked():
            return I.DatasetTarget(dataset_id=self.dataset_combo.currentData())
        rpl_id, label, start = "", "", None
        if self.picker is not None:
            from ..rpl_reference import table_reference
            rpl_id, label, start = table_reference(self.wiz.model, self.picker.rpl_id())
        return I.DatasetTarget(name=self.name.text().strip(), kind=self.kind.currentData(),
                               originator=self.originator.text().strip(), rpl_id=rpl_id, rpl_label=label,
                               rpl_start_kp=start)

    def received_utc(self) -> str:
        return self.received.date().toString("yyyy-MM-dd") + "T00:00:00Z"


class ColumnsPage(QWizardPage):
    def __init__(self, wizard: "ImportWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Columns")
        self.setSubTitle("Give each column its role. The mapping applies to every file in this import.")
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Preview"))
        self.file_combo = QComboBox()
        self.file_combo.setToolTip("The file shown below (the mapping is the same for all).")
        self.file_combo.currentIndexChanged.connect(self._show_file)
        row.addWidget(self.file_combo, 1)
        row.addWidget(QLabel("Mapping name"))
        self.profile_name = QLineEdit()
        self.profile_name.setToolTip("Saved with the data file, for the next delivery in this format.")
        row.addWidget(self.profile_name)
        layout.addLayout(row)
        self.mapping = ColumnMappingWidget()
        self.mapping.changed.connect(self.completeChanged.emit)
        layout.addWidget(self.mapping, 1)
        self._for_target = None

    def initializePage(self) -> None:  # noqa: N802 (Qt API)
        target = self.wiz.dataset_page.target()
        self.mapping.set_saved_profiles(self.wiz.saved_profiles())
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        for path in self.wiz.paths:
            self.file_combo.addItem(os.path.basename(path), path)
        self.file_combo.blockSignals(False)
        key = (target.dataset_id, tuple(self.wiz.paths))
        if key == self._for_target:
            return                         # back and forth: keep the user's mapping
        self._for_target = key
        profile, note = None, ""
        dataset = self.wiz.dataset_row(target.dataset_id) if target.dataset_id else None
        if dataset is not None and dataset.get("profile_id"):
            saved = self.wiz.profile_row(dataset["profile_id"])
            if saved is not None:
                profile = saved["profile"]
                self.profile_name.setText(saved["name"])
                note = f"The dataset's mapping '{saved['name']}' (v{saved['version']})."
        if not self.profile_name.text().strip():
            self.profile_name.setText(f"{target.name or 'Burial data'} mapping")
        self._load(0, profile, note)

    def _load(self, index: int, profile: Optional[Dict], note: str = "") -> None:
        path = self.file_combo.itemData(index)
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            self.mapping.problem_label.setText(f"Could not read {os.path.basename(path)}: {exc.strerror or exc}")
            return
        if profile is not None:
            from . import profile as P
            from . import reader as rd
            try:
                headers = rd.read_table(content, os.path.basename(path), P.normalise(profile)["table"], 2).headers
            except Exception:
                headers = []
            if not P.matches(profile, headers):
                missing = P.differences(profile, headers)["missing"]
                note = (f"The dataset's mapping does not fit this file (missing: {', '.join(missing[:4])}); "
                        "roles suggested instead.") if missing else note
                profile = None if missing else profile
        self.mapping.set_file(os.path.basename(path), content, profile, suggest=profile is None, note=note)

    def _show_file(self, index: int) -> None:
        current = self.mapping.profile()
        self._load(index, current, "")

    def isComplete(self) -> bool:  # noqa: N802 (Qt API)
        return self.mapping.is_complete() and bool(self.profile_name.text().strip())

    def validatePage(self) -> bool:  # noqa: N802 (Qt API)
        self.mapping.confirm_suggestions()
        return True


class ReviewPage(QWizardPage):
    def __init__(self, wizard: "ImportWizard"):
        super().__init__()
        self.wiz = wizard
        self.setTitle("Review and import")
        self.setSubTitle("What importing will do. Nothing is written until you press Import.")
        self.setCommitPage(True)
        self.batch: List[I.BatchFile] = []
        self._existing_times = []
        self._ready = False
        self._importing = False
        layout = QVBoxLayout(self)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.files = QTableWidget(0, 7)
        self.files.setHorizontalHeaderLabels(["Import", "File", "Rows", "Rejected", "Time (UTC)", "KP", "What happens"])
        self.files.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.files.setSelectionMode(SELECTION_MODE_SINGLE)
        self.files.setEditTriggers(EDIT_TRIGGER_NONE)
        self.files.setFocusPolicy(getattr(getattr(Qt, "FocusPolicy", Qt), "NoFocus"))
        self.files.itemChanged.connect(self._include_changed)
        self.files.currentCellChanged.connect(lambda *_: self._show_details())
        layout.addWidget(self.files, 2)
        details = QHBoxLayout()
        self.replace_check = QCheckBox("")
        self.replace_check.toggled.connect(self._replace_toggled)
        details.addWidget(self.replace_check)
        details.addStretch(1)
        self.rejected_button = QPushButton("Rejected rows…")
        self.rejected_button.clicked.connect(self._show_rejected)
        details.addWidget(self.rejected_button)
        layout.addLayout(details)
        self.overlaps = QTableWidget(0, 4)
        self.overlaps.setHorizontalHeaderLabels(["Overlaps with", "From – to (UTC)", "Records (there / here)", "Rule"])
        self.overlaps.setEditTriggers(EDIT_TRIGGER_NONE)
        layout.addWidget(self.overlaps, 1)
        acquired = QFormLayout()
        self.acquired = QDateEdit()
        self.acquired.setCalendarPopup(True)
        self.acquired.setDisplayFormat("yyyy-MM-dd")
        self.acquired.setDate(QDate.currentDate())
        self.acquired_label = QLabel("Acquired on")
        self.acquired.setToolTip("Files without a time column: the date the data was acquired. Where datasets "
                                 "overlap, the latest data wins, so this date matters.")
        acquired.addRow(self.acquired_label, self.acquired)
        layout.addLayout(acquired)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

    # -- review ---------------------------------------------------------------------
    def initializePage(self) -> None:  # noqa: N802 (Qt API)
        self._ready = False
        self.batch = []
        self.files.setRowCount(0)
        self.overlaps.setRowCount(0)
        self.completeChanged.emit()
        target = self.wiz.dataset_page.target()
        profile = self.wiz.columns_page.mapping.profile()
        paths = list(self.wiz.paths)
        store_path = self.wiz.controller.store.path if self.wiz.controller.store is not None else None

        def work(cancelled, progress):
            if store_path and target.dataset_id:
                with StoreReader(store_path) as reader:
                    batch = I.review_files(reader, target.dataset_id, paths, profile, cancelled, progress)
                    return batch, reader.file_times(target.dataset_id)
            return I.review_files(None, None, paths, profile, cancelled, progress), []

        self.status.setText("Reading the files…")
        self.progress.setValue(0)
        self.progress.show()
        if not self.wiz.run("Reviewing burial data files", work, self._reviewed):
            self.status.setText("Another burial data task is running: wait for it, then go back and forward.")

    def _reviewed(self, task) -> None:
        self.progress.hide()
        if task.error or task.cancelled:
            self.status.setText(task.error or "The review was cancelled.")
            self.status.setStyleSheet(status_style("error"))
            return
        self.status.setStyleSheet("")
        self.status.setText("Untick a file to leave it out; choose a rule for each overlap below.")
        self.batch, self._existing_times = task.result
        self._ready = True
        self._fill()
        self.completeChanged.emit()

    def _fill(self) -> None:
        self.files.blockSignals(True)
        self.files.setRowCount(len(self.batch))
        for row, item in enumerate(self.batch):
            include = QTableWidgetItem("")
            include.setFlags(_ITEM_ENABLED | _ITEM_CHECKABLE | _ITEM_SELECTABLE)
            include.setCheckState(_CHECKED if item.include else _UNCHECKED)
            if item.error or item.duplicate is not None:
                include.setFlags(_ITEM_SELECTABLE)
            self.files.setItem(row, 0, include)
            time_text = (f"{utc_text(item.time_range[0])} … {utc_text(item.time_range[1])}" if item.has_time
                         else "no time column")
            kp_text = (f"{item.kp_range[0]:.3f} … {item.kp_range[1]:.3f}" if item.kp_range[0] is not None else "")
            for col, text in enumerate([item.name, f"{item.rows_kept:,}", f"{item.rows_rejected:,}", time_text,
                                        kp_text, self._what(item)], start=1):
                cell = QTableWidgetItem(text)
                cell.setFlags(_ITEM_ENABLED | _ITEM_SELECTABLE)
                if col == 6:
                    cell.setToolTip(text)
                self.files.setItem(row, col, cell)
        self.files.blockSignals(False)
        self.files.resizeColumnsToContents()
        if self.batch and self.files.currentRow() < 0:
            self.files.setCurrentCell(0, 1)
        self._show_details()
        self._update_summary()

    @staticmethod
    def _what(item: I.BatchFile) -> str:
        if item.error:
            return f"Not imported: {item.error}"
        if item.duplicate is not None:
            when = str(item.duplicate.get("imported_utc") or "")[:10]
            who = item.duplicate.get("author") or ""
            return f"Skipped: same bytes as {item.duplicate['name']}" + (f" (imported {when} by {who})" if when else "")
        if not item.include:
            return "Not imported (unticked)"
        if not item.rows_kept:
            return "Not imported: no rows could be read"
        parts = ["Import"]
        if item.replaces:
            parts.append(f"replacing {item.replaces_name} (kept, marked replaced)")
        elif item.same_name is not None:
            parts.append(f"beside the current {item.same_name['name']}")
        if item.overlaps:
            parts.append(f"{len(item.overlaps)} overlap(s)")
        if item.warnings:
            parts.append(item.warnings[0])
        return "; ".join(parts)

    def _include_changed(self, cell: QTableWidgetItem) -> None:
        if cell.column() != 0:
            return
        item = self.batch[cell.row()]
        item.include = cell.checkState() == _CHECKED
        self._replan()

    def _replan(self) -> None:
        I.plan_overlaps(self.batch, self._existing_times)
        row = self.files.currentRow()
        self._fill()
        if row >= 0:
            self.files.setCurrentCell(row, 1)

    def _current(self) -> Optional[I.BatchFile]:
        row = self.files.currentRow()
        return self.batch[row] if 0 <= row < len(self.batch) else None

    def _show_details(self) -> None:
        item = self._current()
        self.overlaps.setRowCount(0)
        self.replace_check.blockSignals(True)
        self.replace_check.setVisible(bool(item and item.same_name is not None and item.importable))
        if item is not None and item.same_name is not None:
            self.replace_check.setText(f"Replace the current {item.same_name['name']} (it is kept, marked replaced)")
            self.replace_check.setChecked(item.replaces is not None)
        self.replace_check.blockSignals(False)
        self.rejected_button.setEnabled(bool(item and item.rejected))
        if item is None:
            return
        self.overlaps.setRowCount(len(item.overlaps))
        for row, choice in enumerate(item.overlaps):
            for col, text in enumerate([choice.older_name, f"{utc_text(choice.t0)} – {utc_text(choice.t1)}",
                                        f"{choice.older_count:,} / {choice.newer_count:,}"]):
                self.overlaps.setItem(row, col, QTableWidgetItem(text))
            combo = QComboBox()
            for key, text in _RULE_LABELS:
                combo.addItem(text if key != ov.RULE_SPLIT or choice.split_utc is None
                              else f"Split at {utc_text(choice.split_utc)}", key)
            combo.setCurrentIndex(max(0, combo.findData(choice.rule)))
            combo.activated.connect(lambda _i, c=choice, b=combo: self._rule_chosen(c, b))
            self.overlaps.setCellWidget(row, 3, combo)
        self.overlaps.resizeColumnsToContents()

    def _rule_chosen(self, choice: I.OverlapChoice, combo: QComboBox) -> None:
        rule = combo.currentData()
        if rule == ov.RULE_SPLIT:
            default = utc_text((choice.t0 + choice.t1) / 2.0)
            text, ok = QInputDialog.getText(self, "Split the overlap",
                                            f"Older file before, this file from (UTC, between {utc_text(choice.t0)} "
                                            f"and {utc_text(choice.t1)}):", text=default)
            seconds = _utc_seconds(text) if ok else None
            if seconds is None or not choice.t0 <= seconds <= choice.t1:
                combo.setCurrentIndex(max(0, combo.findData(choice.rule)))
                return
            choice.split_utc = seconds
        choice.rule = rule
        self._show_details()

    def _replace_toggled(self, on: bool) -> None:
        item = self._current()
        if item is None or item.same_name is None:
            return
        item.replaces = item.same_name["file_id"] if on else None
        item.replaces_name = item.same_name["name"] if on else ""
        self._replan()

    def _show_rejected(self) -> None:
        item = self._current()
        if item is None:
            return
        show_rejected(self, item.name, item.rejected, item.rows_rejected)

    def _update_summary(self) -> None:
        counts = I.summary(self.batch)
        self.acquired.setVisible(counts["no_time"] > 0)
        self.acquired_label.setVisible(counts["no_time"] > 0)
        parts = [f"{counts['files']} file(s), {counts['rows']:,} rows to import"]
        if counts["rejected"]:
            parts.append(f"{counts['rejected']:,} rows rejected (kept in the originals)")
        if counts["duplicates"]:
            parts.append(f"{counts['duplicates']} already imported (skipped)")
        if counts["replaced"]:
            parts.append(f"{counts['replaced']} replacing a current file")
        if counts["overlaps"]:
            parts.append(f"{counts['overlaps']} overlap(s)")
        if counts["errors"]:
            parts.append(f"{counts['errors']} could not be read")
        self.summary.setText(" · ".join(parts) + ".")

    def isComplete(self) -> bool:  # noqa: N802 (Qt API)
        return self._ready and not self._importing and I.summary(self.batch)["files"] > 0

    # -- import ---------------------------------------------------------------------
    def validatePage(self) -> bool:  # noqa: N802 (Qt API)
        if self._importing:
            return False
        if self.wiz.finished_import:
            return True
        try:
            store = self.wiz.controller.ensure_store()
        except Exception as exc:
            self.status.setText(str(exc))
            self.status.setStyleSheet(status_style("error"))
            return False
        target = self.wiz.dataset_page.target()
        options = I.ImportOptions(
            profile=self.wiz.columns_page.mapping.profile(),
            profile_name=self.wiz.columns_page.profile_name.text().strip(),
            received_utc=self.wiz.dataset_page.received_utc(),
            acquired_utc=self.acquired.date().toString("yyyy-MM-dd") + "T00:00:00Z",
            originator=(self.wiz.dataset_page.originator.text().strip()
                        if not target.dataset_id else ""))
        batch = list(self.batch)
        path = store.path

        def work(cancelled, progress):
            return I.import_batch(path, target, batch, options, cancelled, progress)

        self._importing = True
        self.completeChanged.emit()
        self.progress.setValue(0)
        self.progress.show()
        self.status.setText("Importing…")
        if not self.wiz.run("Importing burial data", work, self._imported):
            self._importing = False
            self.status.setText("Another burial data task is running: wait for it and press Import again.")
            self.completeChanged.emit()
        return False

    def _imported(self, task) -> None:
        self._importing = False
        self.progress.hide()
        if task.error or task.cancelled:
            self.status.setText((task.error or "The import was cancelled.") + " Nothing was written.")
            self.status.setStyleSheet(status_style("error"))
            self.completeChanged.emit()
            return
        result: I.ImportResult = task.result
        self.wiz.result = result
        self.wiz.finished_import = True
        self.wiz.imported.emit(result.dataset_id)
        self.wiz.accept()


def _utc_seconds(text: str) -> Optional[float]:
    try:
        value = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def show_rejected(parent, name: str, rows, total: int) -> None:
    """A dialog listing rejected rows: line, reason and the row as delivered."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Rejected rows: {name}")
    dialog.resize(900, 420)
    layout = QVBoxLayout(dialog)
    note = QLabel(f"{total:,} row(s) rejected" + (f"; the first {len(rows):,} are listed" if total > len(rows)
                                                   else "") + ". The original file keeps them.")
    layout.addWidget(note)
    table = QTableWidget(len(rows), 3)
    table.setHorizontalHeaderLabels(["Line", "Reason", "Row as delivered"])
    table.setEditTriggers(EDIT_TRIGGER_NONE)
    for r, (line, reason, text) in enumerate(rows):
        for c, value in enumerate([str(line), reason, text]):
            table.setItem(r, c, QTableWidgetItem(value))
    table.resizeColumnsToContents()
    layout.addWidget(table, 1)
    buttons = QDialogButtonBox(BUTTON_BOX_CLOSE)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    qt_exec(dialog)


class ImportWizard(QWizard):
    """Add data: files, dataset, columns, review and import (see the module notes)."""

    imported = pyqtSignal(str)          # the dataset id

    SETTINGS_FOLDER = "SubseaCableTools/BurialPlanner/acquired/last_folder"

    def __init__(self, controller, model, dataset_id: Optional[str] = None, paths: Optional[List[str]] = None,
                 parent=None):
        super().__init__(parent)
        self.controller = controller
        self.model = model
        self.initial_paths = list(paths or [])
        self.initial_dataset = dataset_id
        self.paths: List[str] = list(self.initial_paths)
        self.result: Optional[I.ImportResult] = None
        self.finished_import = False
        self.datasets = controller.datasets()
        self.setWindowTitle("Add burial data")
        self.setWizardStyle(getattr(getattr(QWizard, "WizardStyle", QWizard), "ModernStyle"))
        self.resize(1100, 720)
        self.files_page = FilesPage(self)
        self.dataset_page = DatasetPage(self)
        self.columns_page = ColumnsPage(self)
        self.review_page = ReviewPage(self)
        for page in (self.files_page, self.dataset_page, self.columns_page, self.review_page):
            self.addPage(page)
        buttons = getattr(QWizard, "WizardButton", QWizard)
        # The review is the last page: its Finish button is the import.
        self.setButtonText(getattr(buttons, "CommitButton"), "Import")
        self.setButtonText(getattr(buttons, "FinishButton"), "Import")
        controller.progressChanged.connect(self._progress)

    def run(self, title: str, work, done) -> bool:
        return self.controller.run(title, work, done)

    def _progress(self, value: float, stage: str) -> None:
        page = self.review_page
        page.progress.setValue(int(value))
        if stage and page.progress.isVisible():
            page.status.setText(stage.rstrip(".…") + "…")

    def reject(self) -> None:  # noqa: N802 (Qt API)
        if self.review_page._importing or self.controller.busy:
            self.controller.cancel()
        super().reject()

    def done(self, result) -> None:  # noqa: N802 (Qt API)
        try:
            self.controller.progressChanged.disconnect(self._progress)
        except (TypeError, RuntimeError):
            pass
        super().done(result)

    # -- what the pages need from the data file --------------------------------------
    def dataset_row(self, dataset_id) -> Optional[Dict]:
        return next((d for d in self.datasets if d["dataset_id"] == dataset_id), None)

    def file_count(self, dataset_id: str) -> int:
        store = self.controller.store
        if store is None:
            return 0
        try:
            return len(store.list_files(dataset_id, include_inactive=False))
        except Exception:
            return 0

    def profile_row(self, profile_id: str) -> Optional[Dict]:
        store = self.controller.store
        try:
            return store.get_profile(profile_id) if store is not None else None
        except Exception:
            return None

    def saved_profiles(self):
        store = self.controller.store
        if store is None:
            return []
        out = []
        try:
            for row in store.list_profiles():
                full = store.get_profile(row["profile_id"])
                if full is not None:
                    out.append((f"{row['name']} (v{row['version']})", full["profile"]))
        except Exception:
            return []
        return out

    def last_folder(self) -> str:
        from qgis.PyQt.QtCore import QSettings
        try:
            return str(QSettings().value(self.SETTINGS_FOLDER, "", type=str))
        except Exception:
            return ""

    def remember_folder(self, path: str) -> None:
        from qgis.PyQt.QtCore import QSettings
        try:
            QSettings().setValue(self.SETTINGS_FOLDER, os.path.dirname(path))
        except Exception:
            pass
