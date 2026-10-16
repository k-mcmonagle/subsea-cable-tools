# -*- coding: utf-8 -*-
"""Import (or edit) a CPT campaign: AGS files, a position/summary table, checks.

Three tabs, each with a check line that reports the parsed result:

1. **AGS files** — AGS 3.x or 4.x files (any extension); each file's edition,
   groups and CPTs are listed, and how each CPT is named.
2. **Positions & summary** (optional) — a CSV/XLSX table with one row per
   CPT, joined by CPT name: coordinates, water depth, penetration, date,
   summary and numbered horizon columns. The columns are the user's choice;
   *Suggest* fills them from the headers only when pressed.
3. **Check & register** — every CPT with its position, where it came from,
   its KP and offset on this plan's route, plus Input Data Register details
   for the import.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from qgis.core import QgsCoordinateReferenceSystem
from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
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
    QSpinBox,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..geotech import ags as ags_mod
from ..geotech import cpt as cpt_mod
from ..geotech import positions as pos_mod
from ..qgis_compat import (BUTTON_BOX_CANCEL, BUTTON_BOX_OK, SELECTION_BEHAVIOR_SELECT_ROWS,
                           SELECTION_MODE_EXTENDED)
from . import cpt_register, schema, ui_helpers
from .numeric_profile_dialogs import RowsModel

_ROLES_ORDER = ("id", "lat", "lon", "x", "y", "water_depth", "penetration", "date", "time",
                "summary", "horizon_desc", "horizon_class", "horizon_interface")
_ROLE_TIPS = {
    "id": "The column naming each CPT; it must match the CPT names from the AGS files.",
    "lat": "Latitude in decimal degrees or as degrees and minutes, e.g. 52° 20.220' N.",
    "lon": "Longitude in decimal degrees or as degrees and minutes, e.g. 3° 12.500' E.",
    "x": "Easting (or X) in the CRS chosen above.",
    "y": "Northing (or Y) in the CRS chosen above.",
    "water_depth": "Water depth at the CPT (m, positive down).",
    "penetration": "Penetration achieved below the seabed (m).",
    "summary": "A one-line interpretation shown with the CPT.",
    "horizon_desc": "The first of the numbered description columns (…1, …2, …); "
                    "the others are found from the header.",
    "horizon_class": "The first of the numbered class columns, e.g. Class 1.",
    "horizon_interface": "The first of the numbered interface-depth columns: interface k is "
                         "the base of horizon k.",
}


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(ui_helpers.hint_style())
    return label


def _check_label() -> QLabel:
    label = QLabel()
    label.setWordWrap(True)
    label.setTextFormat(getattr(Qt, "TextFormat", Qt).PlainText)
    label.setTextInteractionFlags(getattr(Qt, "TextInteractionFlag", Qt).TextSelectableByMouse)
    return label


def _crs_widget():
    from qgis.gui import QgsProjectionSelectionWidget
    widget = QgsProjectionSelectionWidget()
    try:
        widget.setOptionVisible(QgsProjectionSelectionWidget.CrsOption.CrsNotSet, True)
    except AttributeError:
        try:
            widget.setOptionVisible(QgsProjectionSelectionWidget.CrsNotSet, True)
        except AttributeError:
            pass
    return widget


def ags_candidates(folder: str) -> List[str]:
    """Files in ``folder`` that start like an AGS file (any extension)."""
    out = []
    try:
        names = sorted(os.listdir(folder), key=str.casefold)
    except OSError:
        return out
    for name in names:
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() not in (
                ".xlsx", ".xlsm", ".xls", ".pdf", ".docx", ".png", ".jpg", ".zip") \
                and ags_mod.looks_like_ags(path):
            out.append(path)
    return out


class CptImportDialog(QDialog):
    """Define or edit one CPT campaign; ``result()`` gives what to store."""

    def __init__(self, model, campaign: Optional[Dict] = None, parent=None):
        super().__init__(parent)
        self.model = model
        self.campaign = dict(campaign or {})
        self.campaign.setdefault("campaign_id", schema.new_id())
        self.config = dict(self.campaign.get("config") or {})
        self._file_cache: Dict[str, tuple] = {}
        self.paths: List[str] = [f.get("path") or "" for f in self.config.get("files") or []]
        self.grid: List[List[str]] = []
        self.headers: List[str] = []
        self.built: Optional[Dict] = None
        self._result = None
        self.setWindowTitle("Edit CPT import" if campaign else "Import CPTs (AGS)")
        self.resize(1040, 780)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._files_tab(), "1. AGS files")
        self.tabs.addTab(self._positions_tab(), "2. Positions && summary")
        self.tabs.addTab(self._check_tab(), "3. Check && register")
        layout.addWidget(self.tabs, 1)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.button(BUTTON_BOX_OK).setText("Save" if campaign else "Import")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self._rebuild_soon = ui_helpers.coalesced(self, self._rebuild, 150)
        self._load_files()
        self._load_positions()
        self._rebuild()

    # -- 1. files ---------------------------------------------------------------
    def _files_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_hint(
            "AGS 3.x and AGS 4.x files are read (any file extension). CPT readings come from the "
            "CPTT (AGS 4.2), SCPT (AGS 4.0–4.1) or STCN (AGS 3) group; positions from LOCA / HOLE when present. "
            "Units are converted to m, MPa (qc) and kPa (fs, pore pressure) from each heading's unit."))
        row = QHBoxLayout()
        add = QPushButton("Add files…")
        add.clicked.connect(self._add_files)
        folder = QPushButton("Add folder…")
        folder.setToolTip("Add every file in a folder that starts like an AGS file.")
        folder.clicked.connect(self._add_folder)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_files)
        for widget in (add, folder, remove):
            row.addWidget(widget)
        row.addStretch(1)
        layout.addLayout(row)
        self.files_view = QTableView()
        self.files_view.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.files_view.setSelectionMode(SELECTION_MODE_EXTENDED)
        layout.addWidget(self.files_view, 1)
        form = QFormLayout()
        self.name_rule = QComboBox()
        self.name_rule.addItem("Automatic — file name when each file holds one CPT, "
                               "else the AGS location ID", cpt_mod.NAME_AUTO)
        for key, label in cpt_mod.NAME_RULES.items():
            self.name_rule.addItem(label, key)
        self.name_rule.setCurrentIndex(max(0, self.name_rule.findData(
            self.config.get("name_rule") or cpt_mod.NAME_AUTO)))
        self.name_rule.setToolTip("CPT names are matched to the position table and shown on the "
                                  "map and plots. AGS location IDs such as '0001' are often not "
                                  "the CPT name; the file name then is.")
        self.name_rule.currentIndexChanged.connect(lambda *_a: self._rebuild_soon())
        form.addRow("Name each CPT by", self.name_rule)
        self.ags3_pore = QComboBox()
        for key, label in (("u2", "u2 — shoulder, behind the cone (the usual piezocone position)"),
                           ("u1", "u1 — on the cone face"), ("u3", "u3 — behind the sleeve")):
            self.ags3_pore.addItem(label, key)
        self.ags3_pore.setCurrentIndex(max(0, self.ags3_pore.findData(self.config.get("ags3_pore") or "u2")))
        self.ags3_pore.setToolTip("AGS 3 names its pore pressures only 'first', 'second' and 'third' "
                                  "(STCN_PWP1/2/3) without the filter position; AGS 4 defines them. qt is "
                                  "corrected with u2. Check the contractor's report.")
        self.ags3_pore.currentIndexChanged.connect(self._pore_changed)
        self.ags3_pore_label = QLabel("AGS 3 pore pressure STCN_PWP1 is")
        form.addRow(self.ags3_pore_label, self.ags3_pore)
        layout.addLayout(form)
        self.files_check = _check_label()
        layout.addWidget(self.files_check)
        return page

    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "AGS files", self._last_folder(),
            "AGS files (*.ags *.AGS *.txt *.csv);;All files (*)")
        self._add_paths(paths)

    def _add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder of AGS files", self._last_folder())
        if not folder:
            return
        found = ags_candidates(folder)
        if not found:
            QMessageBox.information(self, self.windowTitle(), "No AGS files were found in that folder.")
            return
        self._add_paths(found)

    def _last_folder(self) -> str:
        return os.path.dirname(self.paths[-1]) if self.paths else ""

    def _add_paths(self, paths) -> None:
        known = {os.path.normcase(os.path.abspath(p)) for p in self.paths}
        for path in paths or []:
            if os.path.normcase(os.path.abspath(path)) not in known:
                self.paths.append(path)
                known.add(os.path.normcase(os.path.abspath(path)))
        self._load_files()
        self._rebuild_soon()

    def _remove_files(self) -> None:
        rows = sorted({i.row() for i in self.files_view.selectionModel().selectedRows()}, reverse=True) \
            if self.files_view.selectionModel() is not None else []
        for row in rows:
            if 0 <= row < len(self.paths):
                del self.paths[row]
        self._load_files()
        self._rebuild_soon()

    def _read(self, path: str):
        pore = self.ags3_pore.currentData() or "u2"
        key = (path, cpt_register.file_fingerprint(path), pore)
        if key not in self._file_cache:
            tests, reports = cpt_register.read_files([path], pore)
            self._file_cache[key] = (tests, reports[0])
        return self._file_cache[key]

    def _pore_changed(self, *_args) -> None:
        self._load_files()
        self._rebuild_soon()

    def tests(self) -> List[Dict]:
        out = []
        for path in self.paths:
            tests, _report = self._read(path)
            out.extend(tests)
        return out

    def _load_files(self) -> None:
        rows = []
        for path in self.paths:
            tests, report = self._read(path)
            depths = [d for t in tests for d in t["depth"]]
            channels = sorted({k for t in tests for k in t["channels"]},
                              key=lambda k: list(cpt_mod.CHANNELS).index(k) if k in cpt_mod.CHANNELS else 99)
            problems = report["error"] or "; ".join((report["warnings"] + [w for t in tests for w in t["warnings"]])[:3])
            rows.append([report["file"], report["version"], report["groups"], report["tests"],
                         report["readings"],
                         f"{min(depths):g}–{max(depths):g} m" if depths else "",
                         ", ".join(cpt_mod.channel_symbol(k) for k in channels), problems])
        self.files_model = RowsModel(["File", "Edition", "Groups", "CPTs", "Readings", "Depth",
                                      "Channels", "Problems"], rows, self)
        self.files_view.setModel(self.files_model)
        self.files_view.resizeColumnsToContents()
        ags3 = any(t["group"] == "STCN" and any(k in t["channels"] for k in ("u1", "u2", "u3"))
                   for path in self.paths for t in self._read(path)[0])
        self.ags3_pore.setVisible(ags3)
        self.ags3_pore_label.setVisible(ags3)
        self._update_grid_crs_visibility()

    # -- 2. positions ---------------------------------------------------------------
    def _positions_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_hint(
            "Optional. Many offshore deliveries give CPT positions, water depth, penetration and the "
            "interpreted layering in a separate table, one row per CPT. Choose it and its columns; rows "
            "are joined to the AGS CPTs by name. A position here is used instead of the AGS one."))
        file_row = QHBoxLayout()
        browse = QPushButton("Choose table (CSV / XLSX)…")
        browse.clicked.connect(self._browse_table)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_table)
        self.table_label = QLabel("No table chosen")
        self.sheet = QComboBox()
        self.sheet.setVisible(False)
        self.sheet.currentTextChanged.connect(self._sheet_changed)
        self.header_row = QSpinBox()
        self.header_row.setRange(1, 10000)
        self.header_row.valueChanged.connect(self._headers_changed)
        for widget in (browse, clear, self.table_label, self.sheet, QLabel("Header row"), self.header_row):
            file_row.addWidget(widget)
        file_row.addStretch(1)
        layout.addLayout(file_row)
        self.table_preview = QTableView()
        self.table_preview.setMaximumHeight(150)
        layout.addWidget(self.table_preview)
        coords = QHBoxLayout()
        self.coords_group = QButtonGroup(self)
        self.coords_latlon = QRadioButton("Latitude / longitude")
        self.coords_grid = QRadioButton("Easting / northing in")
        for i, radio in enumerate((self.coords_latlon, self.coords_grid)):
            self.coords_group.addButton(radio, i)
            coords.addWidget(radio)
        self.table_crs = _crs_widget()
        coords.addWidget(self.table_crs, 1)
        self.coords_group.buttonClicked.connect(lambda *_a: self._coords_changed())
        self.table_crs.crsChanged.connect(lambda *_a: self._rebuild_soon())
        self.coords_box = QWidget()
        self.coords_box.setLayout(coords)
        layout.addWidget(self.coords_box)
        columns = QHBoxLayout()
        self.column_form = QFormLayout()
        self.family_form = QFormLayout()
        self.column = {}
        for role in _ROLES_ORDER:
            combo = QComboBox()
            combo.setToolTip(_ROLE_TIPS.get(role, ""))
            combo.currentIndexChanged.connect(lambda *_a: self._rebuild_soon())
            self.column[role] = combo
            label = pos_mod.ROLE_LABELS[role] + (" *" if role == "id" else "")
            (self.family_form if role in pos_mod.FAMILY_ROLES or role == "summary"
             else self.column_form).addRow(label, combo)
        columns.addLayout(self.column_form, 1)
        columns.addLayout(self.family_form, 1)
        layout.addLayout(columns)
        options = QHBoxLayout()
        self.loose = QCheckBox("Also match names that differ only in case, spaces or punctuation")
        self.loose.setChecked(bool((self.config.get("positions") or {}).get("loose", True)))
        self.loose.toggled.connect(lambda *_a: self._rebuild_soon())
        suggest = QPushButton("Suggest columns from headers")
        suggest.setToolTip("Fill the empty choices with columns whose header looks right. Check them.")
        suggest.clicked.connect(self._suggest)
        options.addWidget(self.loose)
        options.addStretch(1)
        options.addWidget(suggest)
        layout.addLayout(options)
        self.grid_crs_box = QWidget()
        grid_row = QHBoxLayout(self.grid_crs_box)
        grid_row.setContentsMargins(0, 0, 0, 0)
        self.grid_crs_label = QLabel("AGS easting/northing CRS")
        self.grid_crs = _crs_widget()
        self.grid_crs.setToolTip("Used for CPTs that have LOCA_NATE / LOCA_NATN (HOLE_NATE / "
                                 "HOLE_NATN) but no latitude/longitude and no table position.")
        self.grid_crs.crsChanged.connect(lambda *_a: self._rebuild_soon())
        grid_row.addWidget(self.grid_crs_label)
        grid_row.addWidget(self.grid_crs, 1)
        layout.addWidget(self.grid_crs_box)
        self.table_check = _check_label()
        layout.addWidget(self.table_check)
        layout.addStretch(1)
        return page

    def _load_positions(self) -> None:
        positions = dict(self.config.get("positions") or {})
        (self.coords_grid if positions.get("coords") == "grid" else self.coords_latlon).setChecked(True)
        if positions.get("crs"):
            self.table_crs.setCrs(QgsCoordinateReferenceSystem(positions["crs"]))
        if self.config.get("grid_crs"):
            self.grid_crs.setCrs(QgsCoordinateReferenceSystem(self.config["grid_crs"]))
        self.table_path = positions.get("path") or ""
        self.header_row.blockSignals(True)
        self.header_row.setValue(int(positions.get("header_row") or 1))
        self.header_row.blockSignals(False)
        if self.table_path:
            self._open_table(self.table_path, positions.get("sheet") or "", positions.get("columns") or {})
        else:
            self._fill_columns({})
        self._coords_changed()

    def _browse_table(self) -> None:
        start = os.path.dirname(self.table_path or (self.paths[0] if self.paths else ""))
        path, _ = QFileDialog.getOpenFileName(self, "Position / summary table", start,
                                              "Tables (*.csv *.tsv *.txt *.xlsx *.xlsm)")
        if path:
            self.header_row.blockSignals(True)
            self.header_row.setValue(1)
            self.header_row.blockSignals(False)
            self._open_table(path, "", {})

    def _clear_table(self) -> None:
        self.table_path = ""
        self.grid, self.headers = [], []
        self.table_label.setText("No table chosen")
        self.sheet.setVisible(False)
        self.table_preview.setModel(None)
        self._fill_columns({})
        self._rebuild_soon()

    def _open_table(self, path: str, sheet: str, columns: Dict) -> None:
        try:
            grid, sheets = cpt_register.read_table_grid(path, sheet or None)
        except Exception as exc:  # noqa: BLE001 - a damaged workbook raises zipfile/openpyxl errors
            QMessageBox.warning(self, self.windowTitle(), f"The table cannot be read: {exc}")
            return
        self.table_path = path
        self.grid = grid
        self.table_label.setText(os.path.basename(path))
        self.sheet.blockSignals(True)
        self.sheet.clear()
        self.sheet.addItems(sheets)
        if sheet in sheets:
            self.sheet.setCurrentText(sheet)
        self.sheet.setVisible(bool(sheets))
        self.sheet.blockSignals(False)
        if not columns:
            # A starting point the preview shows: the fullest of the first
            # rows (title rows above a header are common). Change it freely.
            filled = [sum(1 for v in row if str(v).strip()) for row in grid[:30]]
            if filled and self.header_row.value() == 1:
                best = max(range(len(filled)), key=lambda i: (filled[i], -i))
                self.header_row.blockSignals(True)
                self.header_row.setValue(best + 1)
                self.header_row.blockSignals(False)
        self._headers_changed(columns=columns)

    def _sheet_changed(self, sheet: str) -> None:
        if self.table_path and sheet:
            self._open_table(self.table_path, sheet, self._columns())

    def _headers_changed(self, *_args, columns: Optional[Dict] = None) -> None:
        index = self.header_row.value() - 1
        if not self.grid or index >= len(self.grid):
            self.headers = []
            self._fill_columns({})
            return
        previous = columns if columns is not None else self._columns()
        self.headers = [str(h).strip() for h in self.grid[index]]
        self.preview_model = RowsModel(self.headers, self.grid[index + 1:index + 13], self)
        self.table_preview.setModel(self.preview_model)
        self._fill_columns(previous)
        self._rebuild_soon()

    def _fill_columns(self, chosen: Dict) -> None:
        for role, combo in self.column.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(choose)" if role == "id" else "(none)", "")
            for i, header in enumerate(self.headers):
                if header:
                    combo.addItem(f"{i + 1}: {header}", header)
            combo.setCurrentIndex(max(0, combo.findData(chosen.get(role) or "")))
            combo.setEnabled(bool(self.headers))
            combo.blockSignals(False)

    def _columns(self) -> Dict[str, str]:
        latlon = self.coords_latlon.isChecked()
        out = {}
        for role, combo in self.column.items():
            value = combo.currentData() or ""
            if not value or (role in ("lat", "lon") and not latlon) or (role in ("x", "y") and latlon):
                continue
            out[role] = value
        return out

    def _coords_changed(self) -> None:
        latlon = self.coords_latlon.isChecked()
        self.table_crs.setEnabled(not latlon)
        for role, visible in (("lat", latlon), ("lon", latlon), ("x", not latlon), ("y", not latlon)):
            self.column[role].setEnabled(visible and bool(self.headers))
            label = self.column_form.labelForField(self.column[role])
            for widget in (self.column[role], label):
                if widget is not None:
                    widget.setVisible(visible)
        self._rebuild_soon()

    def _suggest(self) -> None:
        suggested = pos_mod.suggest_mapping(self.headers)
        latlon_found = "lat" in suggested and "lon" in suggested
        if not latlon_found and "x" in suggested and "y" in suggested:
            self.coords_grid.setChecked(True)
            self._coords_changed()
        for role, header in suggested.items():
            combo = self.column[role]
            if not combo.currentData():
                combo.setCurrentIndex(max(0, combo.findData(header)))
        self._rebuild_soon()

    def _update_grid_crs_visibility(self) -> None:
        if not hasattr(self, "grid_crs_box"):
            return
        tests = self.tests()
        needs = [t for t in tests if t["location"].get("lat") is None
                 and not t["location"].get("grid_crs")
                 and (t["location"].get("easting") is not None or t["location"].get("local_x") is not None)]
        self.grid_crs_box.setVisible(bool(needs))
        refs = sorted({t["location"].get("grid") for t in needs if t["location"].get("grid")})
        self.grid_crs_label.setText("AGS easting/northing CRS" + (f" (grid reference: {', '.join(refs[:3])})"
                                                                   if refs else ""))

    # -- 3. check ---------------------------------------------------------------------
    def _check_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.name = QLineEdit(self.campaign.get("name") or "")
        self.name.setPlaceholderText("e.g. Route survey CPTs — Rev 0")
        self.name.setToolTip("How this import is listed in the Ground Model tab.")
        form.addRow("Import name", self.name)
        layout.addLayout(form)
        details = QGroupBox("Input Data Register details (optional)")
        detail_form = QFormLayout(details)
        self.originator = QLineEdit(self.campaign.get("originator") or "")
        self.revision = QLineEdit(self.campaign.get("revision") or "")
        self.status = QComboBox()
        self.status.addItems(["current", "superseded"])
        self.status.setCurrentIndex(1 if self.campaign.get("status") == "superseded" else 0)
        self.received = QDateEdit()
        self.received.setCalendarPopup(True)
        self.received.setDisplayFormat("yyyy-MM-dd")
        received = QDate.fromString(str(self.campaign.get("received_utc") or "")[:10], "yyyy-MM-dd")
        self.received.setDate(received if received.isValid() else QDate.currentDate())
        self.quality = QComboBox()
        self.quality.addItems(["", "high", "moderate", "low", "insufficient"])
        self.quality.setCurrentText(self.campaign.get("quality") or "")
        self.notes = QLineEdit(self.campaign.get("notes") or "")
        for label, widget in (("Originator", self.originator), ("Revision / report", self.revision),
                              ("Status", self.status), ("Received", self.received),
                              ("Quality", self.quality), ("Notes", self.notes)):
            detail_form.addRow(label, widget)
        layout.addWidget(details)
        self.preview = QTableView()
        self.preview.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        layout.addWidget(self.preview, 1)
        self.notes_box = QPlainTextEdit()
        self.notes_box.setReadOnly(True)
        self.notes_box.setMaximumHeight(110)
        layout.addWidget(self.notes_box)
        self.summary = _check_label()
        layout.addWidget(self.summary)
        return page

    # -- building ---------------------------------------------------------------------
    def current_config(self) -> Dict:
        positions = {}
        if self.table_path:
            positions = {"path": self.table_path, "sheet": self.sheet.currentText() if self.sheet.count() else "",
                         "header_row": self.header_row.value(), "columns": self._columns(),
                         "coords": "latlon" if self.coords_latlon.isChecked() else "grid",
                         "crs": self.table_crs.crs().authid() or self.table_crs.crs().toWkt()
                         if self.table_crs.crs().isValid() else "",
                         "loose": self.loose.isChecked()}
        grid_crs = self.grid_crs.crs()
        return {"files": [{"path": p} for p in self.paths],
                "name_rule": self.name_rule.currentData() or cpt_mod.NAME_AUTO,
                "ags3_pore": self.ags3_pore.currentData() or "u2",
                "positions": positions,
                "grid_crs": (grid_crs.authid() or grid_crs.toWkt()) if grid_crs.isValid() else ""}

    def _rebuild(self) -> None:
        tests = self.tests()
        reports = [self._read(p)[1] for p in self.paths]
        bad = [r for r in reports if r["error"]]
        names_text = ""
        if tests:
            config = self.current_config()
            try:
                self.built = cpt_register.build(config, tests=tests,
                                                grid=self.grid if self.table_path else None,
                                                campaign_id=self.campaign["campaign_id"])
                self.built["reports"] = reports
                self.built["notes"] = [f"{r['file']}: {r['error']}" for r in bad] + self.built["notes"]
                error = ""
            except (ValueError, OSError) as exc:
                self.built = None
                error = str(exc)
            rule = tests[0].get("name_rule") or ""
            label = cpt_mod.NAME_RULES.get(rule, rule)
            names_text = (f" Named by {label[:1].lower() + label[1:]}: "
                          + ", ".join(t["name"] for t in tests[:4]) + (" …" if len(tests) > 4 else ""))
        else:
            self.built, error = None, ""
        if not self.paths:
            self.files_check.setText("Add the AGS files of this delivery.")
        elif not tests:
            self.files_check.setText("✗ No CPT readings were found in the chosen files.")
        else:
            readings = sum(len(t["depth"]) for t in tests)
            text = f"✓ {len(tests)} CPT(s), {readings} readings in {len(self.paths) - len(bad)} file(s)."
            if bad:
                text += f" ✗ {len(bad)} file(s) could not be used."
            self.files_check.setText(text + names_text)
        self._table_check(error)
        self._preview()

    def _table_check(self, error: str) -> None:
        if not self.table_path:
            self.table_check.setText("No table: positions and water depth come from the AGS files when "
                                     "they have them.")
            return
        if error:
            self.table_check.setText(f"✗ {error}")
            return
        columns = self._columns()
        if "id" not in columns:
            self.table_check.setText("Choose the CPT name column.")
            return
        built = self.built or {"cpts": [], "unmatched_table": []}
        from_table = [c for c in built["cpts"] if c["position_source"] == "table"]
        matched = [c for c in built["cpts"] if (c.get("meta") or {}).get("table_row")]
        text = (f"✓ {len(matched)} of {len(built['cpts'])} CPT(s) matched a table row; "
                f"{len(from_table)} positioned from the table.")
        if built.get("unmatched_table"):
            unused = built["unmatched_table"]
            text += (f" {len(unused)} table row(s) match no CPT: " + ", ".join(unused[:5])
                     + (" …" if len(unused) > 5 else ""))
        if not any(r in columns for r in ("lat", "x")):
            text += " No coordinate columns chosen."
        self.table_check.setText(text)

    def _preview(self) -> None:
        built = self.built or {"cpts": [], "notes": []}
        cpts = built["cpts"]
        view = self.model.cpt_view() if getattr(self.model, "plan", None) else dict(
            getattr(self.model, "CPT_VIEW_DEFAULTS", {"max_offset_m": 250.0, "half_range_m": 100.0}))
        placements, place_notes = ({}, [])
        route = getattr(self.model, "route", None)
        if route is not None and cpts:
            rows, place_notes = cpt_register.place(route, cpts, float(view["max_offset_m"]),
                                                   float(view["half_range_m"]))
            placements = {r["cpt_id"]: r for r in rows}

        def fmt(value, digits=2):
            return "" if value is None else f"{value:.{digits}f}"

        rows = []
        for cpt in cpts:
            placement = placements.get(cpt["cpt_id"]) or {}
            meta = cpt.get("meta") or {}
            channels = ", ".join(cpt_mod.channel_symbol(k) for k in (meta.get("headings") or {}) if k != "depth")
            rows.append([cpt["name"], cpt["source_file"], fmt(cpt["lat"], 6), fmt(cpt["lon"], 6),
                         cpt["position_source"] or "—", fmt(cpt["water_depth_m"], 1),
                         fmt(cpt["final_depth_m"]), cpt["readings"], fmt(cpt["max_depth_m"]), channels,
                         len(cpt.get("layers") or []),
                         fmt(placement.get("kp"), 3), cpt_register.side_text(placement.get("offset_m")),
                         placement.get("status", "") if route is not None else "no route",
                         "; ".join(meta.get("warnings") or [])[:120]])
        self.preview_rows = rows
        self.preview.setModel(RowsModel(
            ["CPT", "File", "Latitude", "Longitude", "Position from", "Water depth (m)",
             "Penetration (m)", "Readings", "Max depth (m)", "Channels", "Layers", "KP", "Offset",
             "On this plan", "Warnings"], rows, self))
        self.preview.resizeColumnsToContents()
        notes = list(built.get("notes") or []) + list(place_notes)
        # Each CPT's own warnings (unit assumptions, the AGS 3 pore pressure
        # position, fs resolution): repeated texts are listed once with a count.
        counts: Dict[str, List[str]] = {}
        for cpt in cpts:
            for warning in (cpt.get("meta") or {}).get("warnings") or []:
                counts.setdefault(warning, []).append(cpt["name"])
        for warning, names in list(counts.items())[:40]:
            who = names[0] if len(names) == 1 else f"{len(names)} CPTs ({', '.join(names[:3])}{' …' if len(names) > 3 else ''})"
            notes.append(f"{who}: {warning}")
        self.notes_box.setPlainText("\n".join(notes) if notes else "No problems found.")
        if not self.name.text().strip() and cpts:
            self.name.setText(self._default_name())
        placed = sum(1 for p in placements.values() if p.get("status") == "placed")
        if not cpts:
            self.summary.setText("Nothing to import yet.")
        else:
            text = (f"{len(cpts)} CPT(s); {sum(1 for c in cpts if c['lat'] is not None)} with a position")
            if route is not None:
                text += (f"; {placed} within {float(view['max_offset_m']):g} m of this plan's route "
                         "(change in the CPT settings)")
            self.summary.setText(text + ".")

    def _default_name(self) -> str:
        tests = self.tests()
        project = (tests[0].get("project") if tests else {}) or {}
        parts = [project.get(k) for k in ("PROJ_NAME", "PROJ_LOC") if project.get(k)]
        return " ".join(parts + ["CPTs"]) if parts else "CPTs"

    # -- result -------------------------------------------------------------------------
    def result(self):
        """``(campaign, cpts, readings)`` to store, or None."""
        return self._result

    def _accept(self) -> None:
        if not self.paths:
            QMessageBox.warning(self, self.windowTitle(), "Add at least one AGS file.")
            return
        self._rebuild()
        if self.built is None or not self.built["cpts"]:
            QMessageBox.warning(self, self.windowTitle(), "No CPTs could be read from the chosen files.")
            return
        if self.table_path and "id" not in self._columns():
            QMessageBox.warning(self, self.windowTitle(),
                                "Choose the CPT name column of the position table (or Clear the table).")
            self.tabs.setCurrentIndex(1)
            return
        name = self.name.text().strip() or self._default_name()
        campaign = dict(self.campaign)
        campaign.update({
            "name": name, "config": cpt_register.stamp_fingerprints(self.current_config()),
            "originator": self.originator.text().strip(), "revision": self.revision.text().strip(),
            "status": self.status.currentText(),
            "received_utc": self.received.date().toString("yyyy-MM-dd"),
            "quality": self.quality.currentText(), "notes": self.notes.text().strip(),
            "imported_utc": schema.utc_now_iso()})
        self._result = (campaign, self.built["cpts"], self.built["readings"])
        self.accept()
