# -*- coding: utf-8 -*-
"""Ground Model dialogs: soil classes, CSV/XLSX import with column
mapping, and KP re-referencing against another RPL revision.

The KP-reference widget is shared by the import and the re-reference
dialog so both offer the same four ways of stating what the source KPs
refer to (see ``kp_rereference``).
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from qgis.core import QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    CHECK_STATE_CHECKED,
    CHECK_STATE_UNCHECKED,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    ITEM_FLAG_USER_CHECKABLE,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
)
from . import ground_model, kp_rereference, rereference_qgis, ui_helpers

_PREVIEW_ROWS = 6


# -- KP reference widget -----------------------------------------------------
class KpReferenceWidget(QGroupBox):
    """Which route the source KPs refer to, and how to bring them across."""

    def __init__(self, model, dock, parent=None):
        super().__init__("KP reference of the source data", parent)
        self.model = model
        self.dock = dock
        self._map_cache: Optional[kp_rereference.KpMap] = None
        self._cache_key = None

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Documents quote KPs against the RPL revision that was current "
            "when they were written. Say which one, and the KPs are brought "
            "onto the plan's route; every mapped unit keeps its delivered KPs "
            "and a flag where the mapping is an assumption.")
        hint.setWordWrap(True)
        hint.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(hint)

        self.group = QButtonGroup(self)
        current = (f"{model.plan.get('rpl_name') or 'the plan route'} "
                   f"{model.plan.get('rpl_revision') or ''}").strip()
        self.radio_identity = QRadioButton(
            f"KPs already reference the plan route ({current})")
        self.radio_geometry = QRadioButton("Another Workbench RPL revision:")
        self.radio_shift = QRadioButton("Constant KP shift:")
        self.radio_anchors = QRadioButton("Matched KP pairs (source, target):")
        for i, radio in enumerate((self.radio_identity, self.radio_geometry,
                                   self.radio_shift, self.radio_anchors)):
            self.group.addButton(radio, i)
        self.radio_identity.setChecked(True)

        layout.addWidget(self.radio_identity)

        geometry_row = QHBoxLayout()
        geometry_row.addWidget(self.radio_geometry)
        self.rpl_combo = QComboBox()
        self.rpl_combo.setToolTip(
            "The RPL revision the source KPs were quoted against. Its route "
            "is walked and projected onto the plan route; stations further "
            "than the tolerance from the plan route are on a re-routed "
            "stretch and are bridged by interpolation.")
        geometry_row.addWidget(self.rpl_combo, 1)
        layout.addLayout(geometry_row)
        geometry_opts = QHBoxLayout()
        geometry_opts.addSpacing(24)
        geometry_opts.addWidget(QLabel("Offset tolerance:"))
        self.tol_spin = QDoubleSpinBox()
        self.tol_spin.setRange(1.0, 5000.0)
        self.tol_spin.setDecimals(0)
        self.tol_spin.setSuffix(" m")
        self.tol_spin.setValue(25.0)
        self.tol_spin.setToolTip(
            "Stations of the source route further than this from the plan "
            "route are not used as anchors (re-routed stretches).")
        geometry_opts.addWidget(self.tol_spin)
        geometry_opts.addWidget(QLabel("Sample step:"))
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(5.0, 1000.0)
        self.step_spin.setDecimals(0)
        self.step_spin.setSuffix(" m")
        self.step_spin.setValue(50.0)
        geometry_opts.addWidget(self.step_spin)
        geometry_opts.addStretch(1)
        layout.addLayout(geometry_opts)

        shift_row = QHBoxLayout()
        shift_row.addWidget(self.radio_shift)
        self.shift_spin = QDoubleSpinBox()
        self.shift_spin.setRange(-10000.0, 10000.0)
        self.shift_spin.setDecimals(3)
        self.shift_spin.setSuffix(" km")
        self.shift_spin.setToolTip(
            "Added to every source KP (e.g. +0.250 when the new revision "
            "starts 250 m further back).")
        shift_row.addWidget(self.shift_spin)
        shift_row.addStretch(1)
        layout.addLayout(shift_row)

        layout.addWidget(self.radio_anchors)
        self.anchor_edit = QPlainTextEdit()
        self.anchor_edit.setPlaceholderText(
            "One pair per line: source_kp, target_kp\n"
            "e.g. 0.000, 0.000\n     12.350, 12.410\n     48.900, 49.275")
        self.anchor_edit.setMaximumHeight(90)
        self.anchor_edit.setToolTip(
            "Positions that name the same physical place in both revisions "
            "(BMH, crossings, alter-courses from the RPL change note). KPs "
            "between pairs interpolate linearly; beyond the last pair the "
            "last offset is carried forward and flagged.")
        layout.addWidget(self.anchor_edit)

        self._refresh_rpls()
        self.rpl_combo.currentIndexChanged.connect(self._invalidate)
        self.tol_spin.valueChanged.connect(self._invalidate)
        self.step_spin.valueChanged.connect(self._invalidate)
        self.shift_spin.valueChanged.connect(self._invalidate)
        self.anchor_edit.textChanged.connect(self._invalidate)
        self.group.buttonClicked.connect(self._invalidate)

    def _invalidate(self, *_args) -> None:
        self._map_cache = None

    def _refresh_rpls(self) -> None:
        self.rpl_combo.clear()
        store = self.dock.workbench_store() if self.dock is not None else None
        current = str(self.model.resolved_rpl_id or self.model.plan.get("rpl_id") or "")
        if store is None:
            self.radio_geometry.setEnabled(False)
            self.rpl_combo.setEnabled(False)
            self.rpl_combo.addItem("(no Cable Workbench registry)", "")
            return
        try:
            rpls = store.list_rpls()
        except Exception:
            rpls = []
        # Same route's other revisions first, then everything else.
        route_id = ""
        for rpl in rpls:
            if str(rpl.get("rpl_id") or "") == current:
                route_id = str(rpl.get("route_id") or "")
        ordered = sorted(rpls, key=lambda r: (
            0 if route_id and str(r.get("route_id") or "") == route_id else 1,
            str(r.get("name") or "").casefold(), str(r.get("rev_label") or "")))
        for rpl in ordered:
            rpl_id = str(rpl.get("rpl_id") or "")
            if rpl_id == current:
                continue
            self.rpl_combo.addItem(rereference_qgis.rpl_label(rpl), rpl_id)
        if self.rpl_combo.count() == 0:
            self.rpl_combo.addItem("(no other RPL revisions registered)", "")
            self.radio_geometry.setEnabled(False)

    def method(self) -> str:
        checked = self.group.checkedId()
        return (kp_rereference.METHOD_IDENTITY, kp_rereference.METHOD_GEOMETRY,
                kp_rereference.METHOD_SHIFT, kp_rereference.METHOD_ANCHORS)[
            max(0, checked)]

    def source_label(self) -> str:
        method = self.method()
        if method == kp_rereference.METHOD_GEOMETRY:
            return str(self.rpl_combo.currentText() or "")
        if method == kp_rereference.METHOD_IDENTITY:
            return (f"{self.model.plan.get('rpl_name') or ''} "
                    f"{self.model.plan.get('rpl_revision') or ''}").strip()
        return ""

    def build_map(self) -> kp_rereference.KpMap:
        """The KpMap for the chosen method; raises ValueError."""
        method = self.method()
        if method == kp_rereference.METHOD_IDENTITY:
            return kp_rereference.KpMap.identity()
        if method == kp_rereference.METHOD_SHIFT:
            return kp_rereference.KpMap.shift(self.shift_spin.value())
        if method == kp_rereference.METHOD_ANCHORS:
            pairs = kp_rereference.parse_anchor_text(self.anchor_edit.toPlainText())
            if len(pairs) < 1:
                raise ValueError("Enter at least one 'source_kp, target_kp' pair.")
            return kp_rereference.KpMap.from_anchors(
                pairs, target_label=self.source_label())
        rpl_id = str(self.rpl_combo.currentData() or "")
        if not rpl_id:
            raise ValueError("Select the RPL revision the source KPs refer to.")
        key = (rpl_id, self.tol_spin.value(), self.step_spin.value(),
               str(self.model.resolved_rpl_id or self.model.plan.get("rpl_id")))
        if self._map_cache is not None and self._cache_key == key:
            return self._map_cache
        if self.model.route is None:
            raise ValueError("The plan has no usable route; set one in Inputs first.")
        store = self.dock.workbench_store()
        src_route, label = rereference_qgis.open_route_for_rpl(
            store, rpl_id, QgsProject.instance())
        target = (f"{self.model.plan.get('rpl_name') or ''} "
                  f"{self.model.plan.get('rpl_revision') or ''}").strip()
        kp_map = rereference_qgis.geometry_map(
            src_route, self.model.route, step_km=self.step_spin.value() / 1000.0,
            offset_tol_m=self.tol_spin.value(), source_label=label,
            target_label=target)
        if not kp_map.anchors:
            raise ValueError("No part of the selected revision lies within the "
                             "offset tolerance of the plan route: "
                             + kp_map.diagnostics.summary())
        self._map_cache = kp_map
        self._cache_key = key
        return kp_map


# -- classes ---------------------------------------------------------------
class ClassesDialog(QDialog):
    """Project-scoped soil-class vocabulary: code, label, group, colour."""

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.setWindowTitle("Soil classes")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        hint = QLabel(
            "Classes are shared by every plan in this project. Codes are what "
            "the units and imports use; the label, group and colour are how "
            "they are shown. Codes used by units but missing here are drawn "
            "with an automatic colour.")
        hint.setWordWrap(True)
        hint.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Code", "Label", "Group", "Colour", "Notes"])
        self.table.horizontalHeader().setSectionResizeMode(HEADER_RESIZE_MODE_STRETCH)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        self._delegate = ui_helpers.ComboColumnDelegate(
            self.table, self._combo_options, self._combo_commit)
        self.table.setItemDelegateForColumn(2, self._delegate)
        self.table.cellDoubleClicked.connect(self._cell_double_clicked)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        add = QPushButton("Add class")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove)
        add_used = QPushButton("Add codes used by this plan")
        add_used.setToolTip("Create a class row for every code the plan's "
                            "units use that is not listed yet.")
        add_used.clicked.connect(self._add_used)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addWidget(add_used)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self._rows: List[Dict] = [dict(c) for c in model.ground_classes]
        self._rebuild()

    def _combo_options(self, index):
        if index.column() != 2:
            return None
        return [(g, ground_model.GROUP_LABELS[g]) for g in ground_model.GROUPS]

    def _combo_commit(self, index, value) -> None:
        row = index.row()
        if 0 <= row < len(self._rows):
            self._rows[row]["group"] = value
            if not self._rows[row].get("color_user"):
                self._rows[row]["color"] = ground_model.auto_color(
                    self._rows[row].get("code") or "", value)
                self._paint_color(row)

    def _rebuild(self) -> None:
        with ui_helpers.silent_rebuild(self.table):
            self.table.setRowCount(len(self._rows))
            for i, row in enumerate(self._rows):
                self.table.setItem(i, 0, QTableWidgetItem(str(row.get("code") or "")))
                self.table.setItem(i, 1, QTableWidgetItem(str(row.get("label") or "")))
                group_item = QTableWidgetItem()
                group = str(row.get("group") or ground_model.GROUP_UNKNOWN)
                ui_helpers.ComboColumnDelegate.mark_item(
                    group_item, group, ground_model.GROUP_LABELS.get(group, group))
                self.table.setItem(i, 2, group_item)
                color_item = QTableWidgetItem("")
                color_item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                    | Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(i, 3, color_item)
                self._paint_color(i)
                self.table.setItem(i, 4, QTableWidgetItem(str(row.get("notes") or "")))

    def _paint_color(self, row: int) -> None:
        item = self.table.item(row, 3)
        if item is None:
            return
        color = str(self._rows[row].get("color") or "#c8c8c8")
        item.setBackground(QColor(color))
        item.setText(color)
        item.setToolTip("Double-click to choose a colour.")

    def _cell_double_clicked(self, row: int, column: int) -> None:
        if column != 3 or not (0 <= row < len(self._rows)):
            return
        current = QColor(str(self._rows[row].get("color") or "#c8c8c8"))
        chosen = QColorDialog.getColor(current, self, "Class colour")
        if chosen.isValid():
            self._rows[row]["color"] = chosen.name()
            self._rows[row]["color_user"] = True
            self._paint_color(row)

    def _sync_from_table(self) -> None:
        for i, row in enumerate(self._rows):
            row["code"] = (self.table.item(i, 0).text() if self.table.item(i, 0) else "").strip()
            row["label"] = (self.table.item(i, 1).text() if self.table.item(i, 1) else "").strip()
            row["notes"] = (self.table.item(i, 4).text() if self.table.item(i, 4) else "").strip()

    def _add(self) -> None:
        self._sync_from_table()
        self._rows.append({"class_id": None, "code": "", "label": "",
                           "group": ground_model.GROUP_UNKNOWN,
                           "color": ground_model.GROUP_COLORS[ground_model.GROUP_UNKNOWN],
                           "notes": ""})
        self._rebuild()
        self.table.setCurrentCell(len(self._rows) - 1, 0)
        self.table.editItem(self.table.item(len(self._rows) - 1, 0))

    def _remove(self) -> None:
        self._sync_from_table()
        row = self.table.currentRow()
        if 0 <= row < len(self._rows):
            del self._rows[row]
            self._rebuild()

    def _add_used(self) -> None:
        self._sync_from_table()
        for created in ground_model.missing_classes(self.model.ground_units, self._rows):
            self._rows.append(created)
        self._rebuild()

    def _save(self) -> None:
        self._sync_from_table()
        seen = set()
        cleaned = []
        for row in self._rows:
            code = row.get("code") or ""
            if not code:
                continue
            if code.casefold() in seen:
                QMessageBox.warning(self, "Soil classes",
                                    f"Duplicate class code '{code}'.")
                return
            seen.add(code.casefold())
            out = {k: row.get(k) for k in ("class_id", "code", "label", "group",
                                            "color", "notes")}
            out["label"] = out["label"] or code
            if not out.get("class_id"):
                out.pop("class_id")
            cleaned.append(out)
        if self.model.save_ground_classes(cleaned):
            self.accept()


# -- import ----------------------------------------------------------------
class GroundImportDialog(QDialog):
    """CSV/XLSX → units with column mapping, format choice and KP reference."""

    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self.setWindowTitle("Import ground model")
        self.resize(760, 720)
        self._headers: List[str] = []
        self._rows: List[List[str]] = []
        self._sheets: List[str] = []
        self.units: List[Dict] = []
        self.kp_map: Optional[kp_rereference.KpMap] = None
        self.replace_existing = True
        self.source_ref = ""
        self._problems: List[str] = []
        self._tally: Dict[str, int] = {}

        layout = QVBoxLayout(self)

        file_box = QGroupBox("File")
        file_form = QFormLayout(file_box)
        file_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        file_row.addWidget(self.path_edit, 1)
        file_row.addWidget(browse)
        file_form.addRow("File:", file_row)
        self.sheet_combo = QComboBox()
        self.sheet_combo.setVisible(False)
        self.sheet_combo.currentIndexChanged.connect(self._reload_sheet)
        self.sheet_label = QLabel("Sheet:")
        self.sheet_label.setVisible(False)
        file_form.addRow(self.sheet_label, self.sheet_combo)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Document and revision, e.g. 'Ground model report 1234-GM Rev C'")
        file_form.addRow("Source reference:", self.source_edit)
        self.file_summary = QLabel("No file loaded.")
        self.file_summary.setWordWrap(True)
        self.file_summary.setStyleSheet(ui_helpers.hint_style())
        file_form.addRow("", self.file_summary)
        layout.addWidget(file_box)

        format_box = QGroupBox("Layout of the table")
        format_layout = QVBoxLayout(format_box)
        self.radio_intervals = QRadioButton(
            "Intervals — one row per unit: start KP, end KP, top, base, class")
        self.radio_horizons = QRadioButton(
            "Horizons — one row per KP station, one depth column per horizon top")
        self.radio_intervals.setChecked(True)
        self.radio_intervals.toggled.connect(self._format_changed)
        format_layout.addWidget(self.radio_intervals)
        format_layout.addWidget(self.radio_horizons)
        opts = QHBoxLayout()
        opts.addWidget(QLabel("KP unit in file:"))
        self.kp_unit = QComboBox()
        self.kp_unit.addItem("km", False)
        self.kp_unit.addItem("m", True)
        opts.addWidget(self.kp_unit)
        self.thickness_check = QCheckBox("'Base' column is a thickness")
        self.thickness_check.setToolTip(
            "Tick when the base column holds the unit thickness rather than "
            "the depth of its base; base = top + thickness.")
        opts.addWidget(self.thickness_check)
        opts.addStretch(1)
        format_layout.addLayout(opts)
        layout.addWidget(format_box)

        self.mapping_stack = QStackedWidget()
        # Intervals: one combo per target.
        interval_page = QWidget()
        self.mapping_form = QFormLayout(interval_page)
        self.mapping_combos: Dict[str, QComboBox] = {}
        for key, label, required in ground_model.IMPORT_TARGETS:
            combo = QComboBox()
            combo.currentIndexChanged.connect(self._mapping_changed)
            self.mapping_combos[key] = combo
            self.mapping_form.addRow(f"{label}{' *' if required else ''}:", combo)
        self.mapping_stack.addWidget(interval_page)
        # Horizons: KP column + a checklist of horizon columns.
        horizon_page = QWidget()
        horizon_layout = QVBoxLayout(horizon_page)
        kp_row = QHBoxLayout()
        kp_row.addWidget(QLabel("KP column:"))
        self.kp_combo = QComboBox()
        kp_row.addWidget(self.kp_combo, 1)
        horizon_layout.addLayout(kp_row)
        horizon_hint = QLabel(
            "Tick the depth columns that hold horizon tops, in order shallow "
            "→ deep, and give each the class of the unit *below* it. The last "
            "ticked horizon starts an open unit.")
        horizon_hint.setWordWrap(True)
        horizon_hint.setStyleSheet(ui_helpers.hint_style())
        horizon_layout.addWidget(horizon_hint)
        self.horizon_table = QTableWidget(0, 2)
        self.horizon_table.setHorizontalHeaderLabels(["Column (tick = horizon top)",
                                                      "Class of unit below"])
        self.horizon_table.horizontalHeader().setSectionResizeMode(HEADER_RESIZE_MODE_STRETCH)
        self.horizon_table.setMaximumHeight(170)
        horizon_layout.addWidget(self.horizon_table)
        self.mapping_stack.addWidget(horizon_page)
        mapping_box = QGroupBox("Column mapping")
        mapping_box_layout = QVBoxLayout(mapping_box)
        mapping_box_layout.addWidget(self.mapping_stack)
        layout.addWidget(mapping_box)

        self.kp_ref = KpReferenceWidget(model, dock, self)
        layout.addWidget(self.kp_ref)

        mode_row = QHBoxLayout()
        self.replace_check = QCheckBox("Replace the plan's existing units")
        self.replace_check.setChecked(True)
        self.replace_check.setToolTip("Untick to append the imported units "
                                      "to those already in the plan.")
        mode_row.addWidget(self.replace_check)
        mode_row.addStretch(1)
        preview = QPushButton("Preview")
        preview.clicked.connect(self._preview)
        mode_row.addWidget(preview)
        layout.addLayout(mode_row)

        self.preview_label = QLabel("Load a file, check the mapping, then Preview.")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        self.ok_button = box.button(BUTTON_BOX_OK)
        self.ok_button.setText("Import")
        self.ok_button.setEnabled(False)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    # -- file --------------------------------------------------------------
    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Import ground model", "",
            "Tables (*.csv *.txt *.tsv *.xlsx *.xlsm);;CSV (*.csv *.txt *.tsv);;"
            "Excel (*.xlsx *.xlsm);;All files (*)")
        if not path:
            return
        self.path_edit.setText(path)
        if not self.source_edit.text().strip():
            self.source_edit.setText(os.path.splitext(os.path.basename(path))[0])
        self._load(path, None)

    def _load(self, path: str, sheet: Optional[str]) -> None:
        try:
            headers, rows, sheets = ground_model.read_table_file(path, sheet)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Import ground model", f"Could not read the file:\n{exc}")
            return
        self._headers, self._rows, self._sheets = headers, rows, sheets
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        for name in sheets:
            self.sheet_combo.addItem(name)
        if sheet and sheet in sheets:
            self.sheet_combo.setCurrentIndex(sheets.index(sheet))
        self.sheet_combo.blockSignals(False)
        self.sheet_combo.setVisible(bool(sheets))
        self.sheet_label.setVisible(bool(sheets))
        self.file_summary.setText(
            f"{len(rows)} data row(s), {len(headers)} column(s): "
            + ", ".join(h or "(blank)" for h in headers[:12])
            + (" …" if len(headers) > 12 else ""))
        self._populate_mapping()
        self.ok_button.setEnabled(False)
        self.preview_label.setText("Check the mapping, then Preview.")

    def _reload_sheet(self, *_args) -> None:
        if self._sheets and self.path_edit.text():
            self._load(self.path_edit.text(), self.sheet_combo.currentText())

    def _populate_mapping(self) -> None:
        guessed = ground_model.guess_mapping(self._headers)
        for key, combo in self.mapping_combos.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— none —", -1)
            for i, header in enumerate(self._headers):
                combo.addItem(header or f"(column {i + 1})", i)
            index = combo.findData(guessed.get(key, -1))
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)
        self.kp_combo.clear()
        for i, header in enumerate(self._headers):
            self.kp_combo.addItem(header or f"(column {i + 1})", i)
        kp_index = guessed.get(ground_model.TARGET_KP, guessed.get(ground_model.TARGET_START, 0))
        self.kp_combo.setCurrentIndex(max(0, kp_index))
        self.horizon_table.setRowCount(len(self._headers))
        for i, header in enumerate(self._headers):
            item = QTableWidgetItem(header or f"(column {i + 1})")
            item.setFlags(item.flags() | ITEM_FLAG_USER_CHECKABLE)
            numeric = _column_is_numeric(self._rows, i) and i != kp_index
            item.setCheckState(CHECK_STATE_CHECKED if numeric else CHECK_STATE_UNCHECKED)
            item.setData(ITEM_DATA_USER_ROLE, i)
            self.horizon_table.setItem(i, 0, item)
            self.horizon_table.setItem(i, 1, QTableWidgetItem(header or ""))
        # "thickness" header → tick the thickness box automatically.
        base_index = guessed.get(ground_model.TARGET_BASE)
        if base_index is not None and "thick" in (self._headers[base_index] or "").casefold():
            self.thickness_check.setChecked(True)

    def _format_changed(self, *_args) -> None:
        self.mapping_stack.setCurrentIndex(0 if self.radio_intervals.isChecked() else 1)
        self.ok_button.setEnabled(False)

    def _mapping_changed(self, *_args) -> None:
        self.ok_button.setEnabled(False)

    # -- preview / accept --------------------------------------------------
    def _build_units(self) -> List[Dict]:
        if not self._rows:
            raise ValueError("Load a file first.")
        kp_in_m = bool(self.kp_unit.currentData())
        source = self.source_edit.text().strip()
        if self.radio_intervals.isChecked():
            mapping = {key: combo.currentData() for key, combo in self.mapping_combos.items()
                       if combo.currentData() is not None and combo.currentData() >= 0}
            for key, label, required in ground_model.IMPORT_TARGETS:
                if required and key not in mapping:
                    raise ValueError(f"Map the '{label}' column.")
            units, problems = ground_model.rows_to_units(
                self._rows, mapping, kp_in_metres=kp_in_m,
                thickness_as_base=self.thickness_check.isChecked(),
                default_source=source)
        else:
            horizons = []
            for i in range(self.horizon_table.rowCount()):
                item = self.horizon_table.item(i, 0)
                if item is None or item.checkState() != CHECK_STATE_CHECKED:
                    continue
                code_item = self.horizon_table.item(i, 1)
                code = (code_item.text() if code_item else "").strip() or f"H{i + 1}"
                horizons.append((int(item.data(ITEM_DATA_USER_ROLE)), code))
            if not horizons:
                raise ValueError("Tick at least one horizon column.")
            units, problems = ground_model.horizons_to_units(
                self._rows, int(self.kp_combo.currentData() or 0), horizons,
                kp_in_metres=kp_in_m, default_source=source)
        self._problems = problems
        if not units:
            raise ValueError("No units could be read from the file"
                             + (f": {problems[0]}" if problems else "."))
        return units

    def _compute(self) -> None:
        units = self._build_units()
        kp_map = self.kp_ref.build_map()
        label = self.kp_ref.source_label()
        mapped, tally = ground_model.rereference_units(
            units, kp_map, source_label=label, use_source_kps=False)
        self.units = mapped
        self.kp_map = kp_map
        self._tally = tally

    def _preview(self) -> None:
        try:
            self._compute()
        except ValueError as exc:
            self.preview_label.setText(f"<b style='color:{ui_helpers.color('error')}'>"
                                       f"{_esc(str(exc))}</b>")
            self.ok_button.setEnabled(False)
            return
        except Exception as exc:  # route/layer failures surface, never hang
            self.preview_label.setText(f"<b style='color:{ui_helpers.color('error')}'>"
                                       f"Preview failed: {_esc(str(exc))}</b>")
            self.ok_button.setEnabled(False)
            return
        units = self.units
        lo = min(u["start_kp"] for u in units)
        hi = max(u["end_kp"] for u in units)
        codes = sorted({u["soil_class"] for u in units if u["soil_class"]}, key=str.casefold)
        lines = [f"<b>{len(units)} unit(s)</b>, KP {lo:.3f}–{hi:.3f} on the plan "
                 f"route, {len(codes)} class(es): {_esc(', '.join(codes[:12]))}"
                 + (" …" if len(codes) > 12 else "")]
        scope = self.model.gen_params().scope
        if scope.length_km > 0 and (hi < scope.start_km or lo > scope.end_km):
            lines.append(f"<span style='color:{ui_helpers.color('warn')}'>The imported "
                         f"KP range does not overlap the plan scope "
                         f"(KP {scope.start_km:.3f}–{scope.end_km:.3f}) — check the "
                         "KP unit and reference.</span>")
        if self.kp_map is not None and not self.kp_map.is_identity:
            lines.append(_esc(self.kp_map.diagnostics.summary()))
            if self._tally:
                flags = ", ".join(f"{k}: {v}" for k, v in sorted(self._tally.items()))
                lines.append(f"Flags — {_esc(flags)}")
        issues = ground_model.validate_units(units)
        if issues:
            lines.append(f"<span style='color:{ui_helpers.color('warn')}'>"
                         f"{len(issues)} validation note(s): "
                         f"{_esc('; '.join(issues[:3]))}"
                         + (" …" if len(issues) > 3 else "") + "</span>")
        if self._problems:
            lines.append(f"<span style='color:{ui_helpers.color('warn')}'>"
                         f"{len(self._problems)} row(s) skipped: "
                         f"{_esc('; '.join(self._problems[:3]))}"
                         + (" …" if len(self._problems) > 3 else "") + "</span>")
        self.preview_label.setText("<br>".join(lines))
        self.ok_button.setEnabled(True)

    def _accept(self) -> None:
        if not self.ok_button.isEnabled():
            self._preview()
            if not self.ok_button.isEnabled():
                return
        self.replace_existing = self.replace_check.isChecked()
        self.source_ref = self.source_edit.text().strip()
        self.accept()


# -- re-reference existing units ---------------------------------------------
class RereferenceDialog(QDialog):
    """Bring the plan's existing units onto the current route from the
    revision their KPs were delivered against."""

    def __init__(self, model, dock, units: List[Dict], parent=None,
                 mapper=None, title: str = "Re-reference ground model KPs"):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._source = [dict(u) for u in units]
        self._mapper = mapper or ground_model.rereference_units
        self.units: List[Dict] = []
        self.kp_map: Optional[kp_rereference.KpMap] = None
        self.setWindowTitle(title)
        self.resize(680, 520)
        layout = QVBoxLayout(self)

        src_labels = sorted({str(u.get("src_rpl") or "") for u in units if u.get("src_rpl")})
        with_src = sum(1 for u in units if u.get("src_start_kp") is not None)
        intro = QLabel(
            f"{len(units)} row(s) selected. {with_src} carry delivered source "
            "KPs" + (f" (referenced to: {_esc(', '.join(src_labels))})" if src_labels else "")
            + ". The plan route is "
            f"<b>{_esc(model.plan.get('rpl_name') or '')} "
            f"{_esc(model.plan.get('rpl_revision') or '')}</b>.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.kp_ref = KpReferenceWidget(model, dock, self)
        layout.addWidget(self.kp_ref)
        self.use_src_check = QCheckBox(
            "Map from the delivered source KPs where a unit has them "
            "(otherwise from its current KPs)")
        self.use_src_check.setChecked(with_src > 0)
        self.use_src_check.setEnabled(with_src > 0)
        layout.addWidget(self.use_src_check)

        row = QHBoxLayout()
        row.addStretch(1)
        preview = QPushButton("Preview")
        preview.clicked.connect(self._preview)
        row.addWidget(preview)
        layout.addLayout(row)
        self.preview_label = QLabel("Choose the reference, then Preview.")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)

        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        self.ok_button = box.button(BUTTON_BOX_OK)
        self.ok_button.setText("Apply")
        self.ok_button.setEnabled(False)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _preview(self) -> None:
        try:
            kp_map = self.kp_ref.build_map()
            mapped, tally = self._mapper(
                self._source, kp_map, source_label=self.kp_ref.source_label(),
                use_source_kps=self.use_src_check.isChecked())
        except Exception as exc:
            self.preview_label.setText(f"<b style='color:{ui_helpers.color('error')}'>"
                                       f"{_esc(str(exc))}</b>")
            self.ok_button.setEnabled(False)
            return
        self.units, self.kp_map = mapped, kp_map
        moved = 0
        biggest = 0.0
        for before, after in zip(self._source, mapped):
            try:
                delta = max(abs(float(after["start_kp"]) - float(before["start_kp"])),
                            abs(float(after["end_kp"]) - float(before["end_kp"])))
            except (TypeError, ValueError):
                continue
            if delta > 5e-4:
                moved += 1
            biggest = max(biggest, delta)
        lines = [f"<b>{moved} of {len(mapped)} row(s) move</b>; largest KP "
                 f"change {biggest * 1000.0:.0f} m.",
                 _esc(kp_map.diagnostics.summary())]
        if tally:
            lines.append("Flags — " + _esc(", ".join(f"{k}: {v}" for k, v in sorted(tally.items()))))
        self.preview_label.setText("<br>".join(lines))
        self.ok_button.setEnabled(True)

    def _accept(self) -> None:
        if not self.ok_button.isEnabled():
            self._preview()
            if not self.ok_button.isEnabled():
                return
        self.accept()


# -- helpers -----------------------------------------------------------------
def _column_is_numeric(rows: List[List[str]], index: int) -> bool:
    seen = 0
    for row in rows[:40]:
        cell = row[index] if index < len(row) else ""
        if not cell:
            continue
        seen += 1
        try:
            float(str(cell).replace(",", "."))
        except ValueError:
            return False
    return seen > 0


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
