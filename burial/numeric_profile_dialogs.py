"""Column-mapped numeric profile imports and source inspection."""
import json
import os

import pyqtgraph as pg
from qgis.PyQt.QtCore import QAbstractTableModel, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableView, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK
from . import numeric_profiles as numeric
from .ground_dialogs import KpReferenceWidget
from .plan_import import read_grid


class RowsModel(QAbstractTableModel):
    """Read-only tables without a widget or item for every measurement."""
    def __init__(self, headers, rows, parent=None):
        super().__init__(parent)
        self.headers, self.rows = headers, rows

    def rowCount(self, parent=None):
        return len(self.rows)

    def columnCount(self, parent=None):
        return len(self.headers)

    def data(self, index, role=0):
        if index.isValid() and role == getattr(Qt, "ItemDataRole", Qt).DisplayRole:
            value = self.rows[index.row()][index.column()]
            return "" if value is None else str(value)

    def headerData(self, section, orientation, role=0):
        if role == getattr(Qt, "ItemDataRole", Qt).DisplayRole:
            return self.headers[section] if orientation == getattr(Qt, "Orientation", Qt).Horizontal else str(section + 1)


class NumericImportDialog(QDialog):
    def __init__(self, model, dock, *, assignments=False, parent=None):
        super().__init__(parent)
        self.assignments = assignments
        self.result_rows = []
        self.grid, self.path = [], ""
        self.setWindowTitle("Import route assignments" if assignments else "Import numeric depth profiles")
        self.resize(850, 720)
        layout = QVBoxLayout(self)
        hint = QLabel("Map investigation IDs exactly (case-sensitive). Blank/NA values stay missing. "
                      "Imports replace matching source / variable / unit profiles across this project; "
                      "route assignments are saved separately." if not assignments else
                      "Assignments replace this plan's assignment table. KPs refer to the route chosen below.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        file_row = QHBoxLayout()
        browse = QPushButton("Choose CSV / TSV / XLSX…")
        browse.clicked.connect(self._browse)
        self.file_label = QLabel("No file selected")
        self.sheet = QComboBox()
        self.sheet.currentTextChanged.connect(self._reload)
        self.header = QSpinBox()
        self.header.setRange(1, 10000)
        self.header.valueChanged.connect(self._columns)
        for widget in (browse, self.file_label, self.sheet, QLabel("Header row"), self.header):
            file_row.addWidget(widget)
        layout.addLayout(file_row)
        self.preview = QTableView()
        self.preview.setMaximumHeight(140)
        layout.addWidget(self.preview)
        form = QFormLayout()
        self.mapping = {}
        roles = [("source_id", "Investigation ID"), ("start_kp", "Start KP"), ("end_kp", "End KP")] if assignments else [
            ("source_id", "Investigation ID"), ("depth", "Depth / interval top"),
            ("base", "Interval base (unmapped = point samples)"), ("flags", "Quality / coverage flags"),
            ("variable", "Variable name (long format)"), ("value", "Value (long format)"),
            ("units", "Units (long format)")]
        for key, label in roles:
            combo = QComboBox()
            self.mapping[key] = combo
            form.addRow(label, combo)
        self.scale = QComboBox()
        for label, scale in ([("km", 1.0), ("m", .001)] if assignments else [("m", 1.0), ("cm", .01), ("mm", .001)]):
            self.scale.addItem(label, scale)
        form.addRow("KP units" if assignments else "Depth units", self.scale)
        self.decimal_comma = QCheckBox("Comma decimal separator")
        form.addRow(self.decimal_comma)
        layout.addLayout(form)
        if assignments:
            self.reference = KpReferenceWidget(model, dock, self)
            layout.addWidget(self.reference)
        else:
            extra = QHBoxLayout()
            self.format = QComboBox()
            self.format.addItems(["Long format / one value column", "Wide format / multiple value columns"])
            self.name = QLineEdit()
            self.name.setPlaceholderText("Variable if not mapped, e.g. su")
            self.units = QLineEdit()
            self.units.setPlaceholderText("Units if not mapped, e.g. kPa")
            for widget in (self.format, self.name, self.units):
                extra.addWidget(widget)
            layout.addLayout(extra)
            self.wide = QTableWidget(0, 4)
            self.wide.setHorizontalHeaderLabels(["Include", "Column", "Variable", "Units"])
            self.wide.setMaximumHeight(150)
            self.wide.hide()
            self.format.currentIndexChanged.connect(lambda i: self.wide.setVisible(i == 1))
            layout.addWidget(self.wide)
            point_row = QHBoxLayout()
            self.support = QDoubleSpinBox()
            self.support.setDecimals(4)
            self.support.setRange(.0001, 100)
            self.support.setValue(.02)
            self.support.setSuffix(" m")
            self.support.setToolTip("Maximum depth support per point. Clipped at neighbouring midpoints; gaps stay blank.")
            self.missing = QLineEdit()
            self.missing.setPlaceholderText("Additional missing tokens, e.g. -9999;-999")
            for widget in (QLabel("Point sample support"), self.support, self.missing):
                point_row.addWidget(widget)
            layout.addLayout(point_row)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.button(BUTTON_BOX_OK).setText("Import")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Investigation table", "", "Tables (*.csv *.tsv *.txt *.xlsx *.xlsm)")
        if path:
            self.path = path
            self._reload()

    def _reload(self, *_args):
        if not self.path:
            return
        try:
            # Refuse truncation rather than silently importing only the first CPT rows.
            self.grid, sheets = read_grid(self.path, self.sheet.currentText() or None, max_rows=1000002)
            if len(self.grid) >= 1000002:
                raise ValueError("Table exceeds one million rows; split it into source files.")
        except (OSError, ValueError) as exc:
            self.grid = []
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        selected = self.sheet.currentText()
        self.sheet.blockSignals(True)
        self.sheet.clear()
        self.sheet.addItems(sheets)
        if selected in sheets:
            self.sheet.setCurrentText(selected)
        self.sheet.blockSignals(False)
        self.file_label.setText(os.path.basename(self.path))
        self._columns()

    def _columns(self, *_args):
        i = self.header.value() - 1
        if i >= len(self.grid):
            return
        headers = self.grid[i]
        self.preview_model = RowsModel(headers, self.grid[i + 1:i + 21], self)
        self.preview.setModel(self.preview_model)
        aliases = {"source_id": ["source_id", "id", "cpt", "cpt_id", "investigation_id"],
                   "depth": ["depth", "depth_m", "top", "top_m"], "base": ["base", "base_m"],
                   "start_kp": ["start_kp", "kp_start"], "end_kp": ["end_kp", "kp_end"],
                   "flags": ["flags", "coverage", "quality"]}
        for key, combo in self.mapping.items():
            previous = combo.currentText()
            combo.clear()
            combo.addItem("(unmapped)", None)
            for col, label in enumerate(headers):
                combo.addItem(f"{col + 1}: {label}", col)
                if label.lower().replace(" ", "_") in aliases.get(key, [key]):
                    combo.setCurrentIndex(col + 1)
            if previous and previous != "(unmapped)" and combo.findText(previous) >= 0:
                combo.setCurrentText(previous)
        if not self.assignments:
            self.wide.setRowCount(len(headers))
            for col, label in enumerate(headers):
                self.wide.setCellWidget(col, 0, QCheckBox())
                for j, text in enumerate((label, label, ""), 1):
                    item = QTableWidgetItem(text)
                    if j == 1:
                        item.setFlags(item.flags() & ~getattr(Qt, "ItemFlag", Qt).ItemIsEditable)
                    self.wide.setItem(col, j, item)

    def _accept(self):
        try:
            if not self.grid:
                raise ValueError("Choose a table first.")
            rows = self.grid[self.header.value():]
            mapping = {key: combo.currentData() for key, combo in self.mapping.items()}
            if self.assignments:
                self.result_rows = numeric.import_assignments(
                    rows, mapping, self.reference.build_map(), kp_scale=self.scale.currentData(),
                    decimal_comma=self.decimal_comma.isChecked(),
                    source_ref=f"{self.path} — {self.reference.source_label()}")
            else:
                variables = [(i, self.wide.item(i, 2).text(), self.wide.item(i, 3).text())
                             for i in range(self.wide.rowCount()) if self.wide.cellWidget(i, 0).isChecked()]
                if self.format.currentIndex() == 1 and not variables:
                    raise ValueError("Include at least one numeric variable column.")
                self.result_rows = numeric.import_profiles(
                    rows, mapping, variables=variables if self.format.currentIndex() else (),
                    variable=self.name.text(), units=self.units.text(), depth_scale=self.scale.currentData(),
                    sample_support=self.support.value(), decimal_comma=self.decimal_comma.isChecked(),
                    missing=self.missing.text().split(";"),
                    provenance={"file": self.path, "sheet": self.sheet.currentText(),
                                "header_row": self.header.value(), "mapping": mapping,
                                "variables": variables, "depth_scale": self.scale.currentData(),
                                "sample_support_m": self.support.value(),
                                "missing_tokens": self.missing.text(), "decimal_comma": self.decimal_comma.isChecked()})
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.accept()


def source_dialog(source, profiles, parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Source profile — " + source)
    dialog.resize(750, 600)
    layout = QVBoxLayout(dialog)
    tabs = QTabWidget()
    layout.addWidget(tabs)
    for profile in profiles:
        if profile["source_id"] != source:
            continue
        page = QWidget()
        page_layout = QVBoxLayout(page)
        provenance = QLabel(json.dumps(profile.get("provenance", {}), ensure_ascii=False))
        provenance.setTextFormat(getattr(Qt, "TextFormat", Qt).PlainText)
        provenance.setWordWrap(True)
        provenance.setMaximumHeight(70)
        page_layout.addWidget(provenance)
        plot = pg.PlotWidget()
        plot.setBackground("w")
        plot.setLabel("left", "Depth below seabed", units="m")
        plot.setLabel("bottom", profile["variable"], units=profile["units"])
        plot.invertY(True)
        # Separate support bars: no connecting across gaps or null values.
        xs, ys = [], []
        for sample in profile["samples"]:
            if sample["value"] is not None:
                xs.extend((sample["value"], sample["value"], float("nan")))
                ys.extend((sample["top"], sample["base"], float("nan")))
        plot.plot(xs, ys, connect="finite", pen=pg.mkPen("#268268", width=2))
        page_layout.addWidget(plot, 1)
        table = QTableView()
        keys = ("depth", "top", "base", "value", "flags", "row")
        table_model = RowsModel(["Depth (m)", "Support top (m)", "Support base (m)", "Value", "Flags", "Data row"],
                                [[s[k] for k in keys] for s in profile["samples"]], table)
        table.setModel(table_model)
        page_layout.addWidget(table, 1)
        tabs.addTab(page, f"{profile['variable']} ({profile['units']})")
    return dialog
