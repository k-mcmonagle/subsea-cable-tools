"""Numeric dataset dialogs: define a dataset, check it, inspect a profile.

One dialog defines a dataset in three tabs — Measurements (file or layer,
four column choices), KP ranges (the layer read live to place them on the
route) and Colours. Nothing is mapped from column names; every choice is the
user's, and a check line under each tab reports the parsed result.
"""
import csv
import json
import re

import pyqtgraph as pg
from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QAbstractTableModel, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QTableView,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK, HEADER_RESIZE_MODE_STRETCH
from . import kp_table, numeric_datasets as sources, numeric_profiles as numeric, schema, ui_helpers
from .numeric_profile_plot import RAMPS, ramp_colours
from .plan_import import read_grid
from .tabs.attribute_widgets import AttributeRulesTable, FieldCombo
from .tabs.kp_table_form import KpTableForm

# Larger tables are checked when the dataset is saved rather than on every edit.
_LIVE_CHECK_ROWS = 200000
_DEPTH_UNITS = (("m", 1.0), ("cm", .01), ("mm", .001))


def _html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(ui_helpers.hint_style())
    return label


def _check_label():
    label = QLabel()
    label.setWordWrap(True)
    label.setTextFormat(getattr(Qt, "TextFormat", Qt).PlainText)
    return label


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


def _vector_layers(polygons_only=False):
    out = []
    for layer in QgsProject.instance().mapLayers().values():
        if isinstance(layer, QgsVectorLayer) and layer.isValid():
            if polygons_only and layer.geometryType() != 2:
                continue
            out.append(layer)
    return sorted(out, key=lambda layer: layer.name().casefold())


# -- colours -------------------------------------------------------------------------
class ColourRangeTable(AttributeRulesTable):
    """The Exclusions value-range rows (From ≥|> … To <|≤) plus a colour and label."""

    def __init__(self, parent=None):
        super().__init__(with_kind=False, with_risk=False, parent=parent)
        self.col_colour, self.col_label = self.col_upper + 1, self.col_upper + 2
        self.table.setColumnCount(self.col_label + 1)
        self.table.setHorizontalHeaderLabels(["From", "", "To", "", "Colour", "Label (optional)"])
        self.table.horizontalHeader().setSectionResizeMode(self.col_label, HEADER_RESIZE_MODE_STRETCH)
        self.table.setMaximumHeight(16777215)
        self.table.setToolTip("Each row is one colour class: From / To with ≥ or > and < or ≤ bounds; "
                              "leave a side blank for open-ended. The first matching row wins. "
                              "Double-click a colour to change it.")
        self.add_button.setText("＋ Add class")
        self.table.cellDoubleClicked.connect(self._pick_colour)

    def add_row(self, rule):
        row = super().add_row(rule)
        colour = QTableWidgetItem(rule.get("colour") or "#808080")
        colour.setFlags(colour.flags() & ~getattr(Qt, "ItemFlag", Qt).ItemIsEditable)
        colour.setBackground(QColor(colour.text()))
        colour.setToolTip("Double-click to choose a colour.")
        self.table.setItem(row, self.col_colour, colour)
        self.table.setItem(row, self.col_label, QTableWidgetItem(rule.get("label") or ""))
        for column in (self.col_lower, self.col_upper):
            self.table.cellWidget(row, column).currentIndexChanged.connect(self.changed)
        self.changed()
        return row

    def remove_current_row(self):
        super().remove_current_row()
        self.changed()

    def changed(self, *_args):
        """Replaced by the owner to refresh its summary."""

    def _pick_colour(self, row, column):
        if column != self.col_colour:
            return
        item = self.table.item(row, column)
        chosen = QColorDialog.getColor(QColor(item.text()), self, "Class colour")
        if chosen.isValid():
            item.setText(chosen.name())
            item.setBackground(chosen)
            self.changed()

    def _row_rule(self, row):
        rule = super()._row_rule(row)
        if rule is not None:
            rule["colour"] = self._cell_text(row, self.col_colour)
            rule["label"] = self._cell_text(row, self.col_label)
        return rule


class ColoursWidget(QWidget):
    """Continuous ramp, equal bands or custom classes, with a class summary."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values, self.name, self.units = [], "value", ""
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.mode = QComboBox()
        for key, label in (("continuous", "Continuous ramp"), ("bands", "Equal bands"),
                           ("classes", "Custom classes")):
            self.mode.addItem(label, key)
        self.ramp = QComboBox()
        self.ramp.addItems(list(RAMPS))
        self.bands = QSpinBox()
        self.bands.setRange(2, 32)
        self.bands.setValue(5)
        self.ramp_label, self.bands_label = QLabel("Ramp"), QLabel("Bands")
        for widget in (QLabel("Colours"), self.mode, self.ramp_label, self.ramp, self.bands_label, self.bands):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        limits = QHBoxLayout()
        self.auto = QCheckBox("Limits from the data")
        self.auto.setChecked(True)
        self.auto.setToolTip("Colour limits from every placed measurement of this dataset.")
        self.minimum, self.maximum = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.minimum, self.maximum):
            spin.setDecimals(4)
            spin.setRange(-1e12, 1e12)
        self.maximum.setValue(1)
        self.limit_widgets = (self.auto, QLabel("from"), self.minimum, QLabel("to"), self.maximum)
        for widget in self.limit_widgets:
            limits.addWidget(widget)
        limits.addStretch()
        layout.addLayout(limits)
        self.classes_box = QWidget()
        classes = QVBoxLayout(self.classes_box)
        classes.setContentsMargins(0, 0, 0, 0)
        classes.addWidget(_hint("Each row colours one range of values. Choose ≥ or > for From and < or ≤ "
                                "for To; leave a side blank for an open-ended class. The first matching "
                                "row wins. Values no class covers are drawn dark grey."))
        quick = QHBoxLayout()
        self.breaks = QLineEdit()
        self.breaks.setPlaceholderText("Break values, separated by commas")
        self.breaks.setToolTip("Replaces the rows with one class below the first break, one between "
                               "each pair (≥ lower, < upper) and one above the last.")
        make = QPushButton("Create classes")
        make.clicked.connect(self._from_breaks)
        self.breaks.returnPressed.connect(self._from_breaks)
        for widget in (QLabel("Breaks"), self.breaks, make):
            quick.addWidget(widget)
        classes.addLayout(quick)
        self.table = ColourRangeTable(self)
        self.table.set_attribute_name_provider(lambda: self.name)
        self.table.changed = self._summarise
        self.table.table.itemChanged.connect(self._summarise)
        classes.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(getattr(Qt, "TextFormat", Qt).RichText)
        classes.addWidget(self.summary)
        layout.addWidget(self.classes_box, 1)
        self.mode.currentIndexChanged.connect(self._update)
        self.auto.toggled.connect(self._update)
        self._update()

    def set_colours(self, colours):
        settings = numeric.colour_settings(colours)
        self.mode.setCurrentIndex(self.mode.findData(settings["mode"]))
        self.ramp.setCurrentText(settings["ramp"])
        self.bands.setValue(settings["bands"])
        self.auto.setChecked(settings["auto"])
        self.minimum.setValue(settings["min"])
        self.maximum.setValue(settings["max"])
        self.table.set_rules(settings["classes"])
        self._update()

    def set_values(self, values, name, units):
        self.values = [v for v in values if v is not None]
        self.name, self.units = name or "value", units or ""
        self._summarise()

    def _update(self, *_args):
        mode = self.mode.currentData()
        for widget in (self.ramp_label, self.ramp):
            widget.setVisible(mode != "classes")
        for widget in (self.bands_label, self.bands):
            widget.setVisible(mode == "bands")
        for widget in self.limit_widgets:
            widget.setVisible(mode != "classes")
        self.minimum.setEnabled(not self.auto.isChecked())
        self.maximum.setEnabled(not self.auto.isChecked())
        self.classes_box.setVisible(mode == "classes")
        self._summarise()

    def _from_breaks(self):
        try:
            values = [float(v) for v in re.split(r"[;,\s]+", self.breaks.text().strip()) if v]
            count = len(set(values)) + 1
            self.table.set_rules(numeric.classes_from_breaks(values, ramp_colours(self.ramp.currentText(), count)))
        except ValueError as exc:
            QMessageBox.warning(self, "Colour classes", f"Breaks must be numbers: {exc}")
        self._summarise()

    def _classes(self):
        problems = self.table.invalid_rows()
        if problems:
            raise ValueError("; ".join(problems))
        return numeric.normalise_classes(self.table.rules())

    def _summarise(self, *_args):
        if self.mode.currentData() != "classes":
            return
        try:
            classes = self._classes()
        except ValueError as exc:
            self.summary.setText(f"<span style='color:#b00020'>{_html(exc)}</span>")
            return
        counts = [0] * len(classes)
        outside = 0
        for value in self.values:
            hit = numeric.class_of(value, classes)
            if hit is None:
                outside += 1
            else:
                counts[next(i for i, c in enumerate(classes) if c is hit)] += 1
        total = len(self.values)

        def share(count):
            if not total:
                return "no measurements yet"
            percent = 100 * count / total
            return f"{count} sample(s), " + ("<1%" if 0 < percent < 1 else f"{percent:.0f}%")

        lines = [f"<span style='color:{c['colour']}; font-size:15px'>■</span> "
                 f"<b>{_html(numeric.class_label(c, self.name))}</b> {_html(self.units)} — {share(counts[i])}"
                 for i, c in enumerate(classes)]
        if total:
            lines.append(f"Outside every class: {share(outside)}")
        lines += [_html(note) for note in numeric.class_coverage(classes, self.name)]
        self.summary.setText("<br>".join(lines))

    def colours(self):
        """The colour settings; raises ValueError for invalid limits or classes."""
        mode = self.mode.currentData()
        out = {"mode": mode, "ramp": self.ramp.currentText(), "bands": self.bands.value(),
               "auto": self.auto.isChecked(), "min": self.minimum.value(), "max": self.maximum.value(),
               "classes": []}
        if mode != "classes" and not out["auto"] and out["max"] <= out["min"]:
            raise ValueError("The colour 'to' limit must be greater than 'from'.")
        if mode == "classes":
            out["classes"] = self._classes()
        elif self.table.row_count():
            try:
                out["classes"] = self._classes()  # kept for switching back
            except ValueError:
                pass
        return out


# -- dataset dialog ------------------------------------------------------------------
class DatasetDialog(QDialog):
    """Define or edit one numeric dataset (one variable, many investigations)."""

    def __init__(self, model, dataset=None, profiles=(), parent=None):
        super().__init__(parent)
        self.model = model
        self.dataset = dict(dataset or {})
        # A new dataset gets its id now: profile ids are derived from it.
        self.dataset.setdefault("dataset_id", schema.new_id())
        config = dict(self.dataset.get("config") or {})
        self.measurements = dict(config.get("measurements") or {})
        # A new placement starts on the plan's RPL (the picker's default), so
        # the "no reference recorded" prompt is kept for older saved tables.
        self.placement = dict(config.get("placement") or {kp_table.KP_REF_KEY: ""})
        self.stored_profiles = list(profiles)
        self.parsed = None          # profiles parsed from the source in this dialog
        self.measurements_dirty = False
        self.grid, self._headers = [], []
        self.source = dict(self.measurements.get("source") or {})
        self._result = None
        self.setWindowTitle("Edit numeric dataset" if dataset else "Add numeric dataset")
        self.resize(900, 780)
        layout = QVBoxLayout(self)
        head = QFormLayout()
        self.name = QLineEdit(self.dataset.get("name") or "")
        self.name.setToolTip("How the dataset is listed.")
        self.variable = QLineEdit(self.dataset.get("variable") or "")
        self.variable.setToolTip("Short name of the measured quantity, used in the legend, class labels and hover.")
        self.units = QLineEdit(self.dataset.get("units") or "")
        head.addRow("Dataset name", self.name)
        names = QHBoxLayout()
        names.addWidget(self.variable, 2)
        names.addWidget(QLabel("Units"))
        names.addWidget(self.units, 1)
        head.addRow("Variable", names)
        layout.addLayout(head)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._measurements_tab(), "1. Measurements")
        self.tabs.addTab(self._placement_tab(), "2. KP ranges")
        self.colours = ColoursWidget()
        self.colours.set_colours(config.get("colours"))
        self.tabs.addTab(self.colours, "3. Colours")
        layout.addWidget(self.tabs, 1)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.button(BUTTON_BOX_OK).setText("Save dataset")
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.variable.textChanged.connect(self._values_changed)
        self.units.textChanged.connect(self._values_changed)
        self._load_existing_source()
        self._values_changed()
        self._check_placement_soon()

    # -- measurements ------------------------------------------------------------
    def _measurements_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_hint(
            "A table with one row per depth reading of each investigation, all investigations "
            "together. Choose the column for each item below using the preview."))
        file_row = QHBoxLayout()
        browse = QPushButton("Choose file (CSV / TSV / XLSX)…")
        browse.clicked.connect(self._browse)
        self.file_label = QLabel("No source chosen")
        self.sheet = QComboBox()
        self.sheet.setVisible(False)
        self.sheet.currentTextChanged.connect(self._sheet_changed)
        self.header = QSpinBox()
        self.header.setRange(1, 10000)
        self.header.setValue(int(self.measurements.get("header_row") or 1))
        self.header.valueChanged.connect(self._columns)
        for widget in (browse, self.file_label, self.sheet, QLabel("Header row"), self.header):
            file_row.addWidget(widget)
        layout.addLayout(file_row)
        layer_row = QHBoxLayout()
        self.layer = QComboBox()
        self.layer.addItem("(none — use a file)", "")
        for layer in _vector_layers():
            self.layer.addItem(layer.name(), layer.id())
        self.layer.currentIndexChanged.connect(self._layer_chosen)
        layer_row.addWidget(QLabel("or a loaded layer / table:"))
        layer_row.addWidget(self.layer, 1)
        layout.addLayout(layer_row)
        self.preview = QTableView()
        self.preview.setMaximumHeight(150)
        layout.addWidget(self.preview)
        form = QFormLayout()
        self.column = {}
        for role, label, tip in (
                ("source_id", "Investigation ID", "Names each investigation; must match the IDs in the KP ranges."),
                ("depth", "Depth (or interval top)", "Depth below seabed of each reading, or the top of its interval."),
                ("base", "Interval base", "Only when each reading covers a depth interval (top and base)."),
                ("value", "Value", "The measured value; blank or missing-coded cells stay missing.")):
            combo = QComboBox()
            combo.setToolTip(tip)
            self.column[role] = combo
            form.addRow(label, combo)
            combo.currentIndexChanged.connect(self._measurements_edited)
        self.depth_unit = QComboBox()
        for label, scale in _DEPTH_UNITS:
            self.depth_unit.addItem(label, scale)
        scale = float(self.measurements.get("depth_scale") or 1.0)
        self.depth_unit.setCurrentIndex(max(0, self.depth_unit.findData(scale)))
        form.addRow("Depth unit", self.depth_unit)
        self.support = QDoubleSpinBox()
        self.support.setDecimals(4)
        self.support.setRange(.0001, 100)
        self.support.setSuffix(" m")
        self.support.setValue(float(self.measurements.get("point_support_m") or .02))
        self.support.setToolTip("Readings at single depths are drawn this thick at most (clipped halfway to "
                                "the neighbouring readings); set it to the reading spacing. Gaps stay blank.")
        form.addRow("Single-depth reading thickness", self.support)
        self.decimal_comma = QCheckBox("Numbers use a decimal comma")
        self.decimal_comma.setChecked(bool(self.measurements.get("decimal_comma")))
        form.addRow(self.decimal_comma)
        self.missing = QLineEdit(str(self.measurements.get("missing") or ""))
        self.missing.setPlaceholderText("Optional: codes meaning 'no value', separated by ;")
        form.addRow("Missing-value codes", self.missing)
        layout.addLayout(form)
        for widget, signal in ((self.depth_unit, "currentIndexChanged"), (self.support, "valueChanged"),
                               (self.decimal_comma, "toggled"), (self.missing, "textChanged")):
            getattr(widget, signal).connect(self._measurements_edited)
        self.measure_check = _check_label()
        layout.addWidget(self.measure_check)
        layout.addStretch()
        self._check_measurements_soon = ui_helpers.coalesced(self, self._check_measurements, 150)
        return page

    def _load_existing_source(self):
        stored = numeric.summary_text(self.stored_profiles, self.units.text())
        if not self.source:
            if self.stored_profiles:
                self.measure_check.setText(f"Stored: {stored}. Choose the source to import them again.")
            return
        self.file_label.setText(sources.source_label(self.source))
        if self.source.get("kind") == "layer":
            index = self.layer.findData(self.source.get("layer_id_hint") or "")
            if index > 0:
                self.layer.blockSignals(True)
                self.layer.setCurrentIndex(index)
                self.layer.blockSignals(False)
        try:
            self.grid, sheets = sources.read_source_grid(self.source)
        except (OSError, ValueError) as exc:
            self.measure_check.setText(f"Stored: {stored}. The source cannot be read ({exc}); "
                                       "choose it again to re-import.")
            return
        self._set_sheets(sheets, self.source.get("sheet") or "")
        self._columns(initial=True)
        self.measure_check.setText(f"Stored: {stored}.")

    def _set_sheets(self, sheets, selected=""):
        self.sheet.blockSignals(True)
        self.sheet.clear()
        self.sheet.addItems(sheets)
        if selected in sheets:
            self.sheet.setCurrentText(selected)
        self.sheet.setVisible(bool(sheets))
        self.sheet.blockSignals(False)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Measurements table", "",
                                              "Tables (*.csv *.tsv *.txt *.xlsx *.xlsm)")
        if path:
            self.layer.blockSignals(True)
            self.layer.setCurrentIndex(0)
            self.layer.blockSignals(False)
            self.use_file(path)

    def use_file(self, path, sheet=""):
        try:
            grid, sheets = read_grid(path, sheet or None, max_rows=sources.MAX_ROWS)
            if len(grid) >= sources.MAX_ROWS:
                raise ValueError("The table exceeds one million rows; split it.")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.source = {"kind": "file", "path": path, "sheet": sheet or (sheets[0] if sheets else "")}
        self.grid = grid
        self._set_sheets(sheets, self.source["sheet"])
        self.file_label.setText(sources.source_label(self.source))
        self._columns()

    def _sheet_changed(self, sheet):
        if self.source.get("kind") == "file" and sheet:
            self.use_file(self.source["path"], sheet)

    def _layer_chosen(self, *_args):
        layer = QgsProject.instance().mapLayer(self.layer.currentData() or "")
        if layer is None:
            return
        try:
            self.grid = sources.layer_grid(layer)
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.source = dict(sources.layer_ref(layer), kind="layer")
        self._set_sheets([])
        self.file_label.setText(sources.source_label(self.source))
        self.header.blockSignals(True)
        self.header.setValue(1)
        self.header.blockSignals(False)
        self._columns()

    def _columns(self, *_args, initial=False):
        i = self.header.value() - 1
        if i >= len(self.grid):
            return
        headers = list(self.grid[i])
        self.preview_model = RowsModel(headers, self.grid[i + 1:i + 21], self)
        self.preview.setModel(self.preview_model)
        # Nothing is guessed from column names. A choice is kept while the
        # same header exists (the stored choices on first load, else the last pick).
        stored = (self.measurements.get("columns") or {}) if initial else {}
        for role, combo in self.column.items():
            if initial:
                previous = stored.get(role)
            else:
                index = combo.currentData()
                previous = self._headers[index] if index is not None and index < len(self._headers) else None
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(single depths — no base column)" if role == "base" else "(choose a column)", None)
            for col, label in enumerate(headers):
                combo.addItem(f"{col + 1}: {label}", col)
            combo.setCurrentIndex(headers.index(previous) + 1 if previous in headers else 0)
            combo.blockSignals(False)
        self._headers = headers
        if not initial:
            self.measurements_dirty = True
        self._sync_support()
        self._check_measurements_soon()

    def _measurements_edited(self, *_args):
        self.measurements_dirty = True
        self._sync_support()
        self._check_measurements_soon()

    def _sync_support(self):
        self.support.setEnabled(self.column["base"].currentData() is None)

    def _measurement_settings(self):
        columns = {role: self._headers[combo.currentData()] for role, combo in self.column.items()
                   if combo.currentData() is not None}
        return {"source": dict(self.source), "header_row": self.header.value(), "columns": columns,
                "depth_scale": self.depth_unit.currentData(), "point_support_m": self.support.value(),
                "decimal_comma": self.decimal_comma.isChecked(), "missing": self.missing.text().strip()}

    def _parse(self):
        if not self.grid:
            raise ValueError("Choose a file or layer with the measurements.")
        return sources.parse_measurements(self.grid, self._measurement_settings(), self._dataset_row())

    def _check_measurements(self):
        if not self.grid or not self.measurements_dirty:
            return
        if len(self.grid) > _LIVE_CHECK_ROWS:
            self.measure_check.setText("Large table: it is checked when you save.")
            return
        try:
            self.parsed = self._parse()
            self.measure_check.setText("✓ " + numeric.summary_text(self.parsed, self.units.text()))
        except (ValueError, OSError, IndexError, TypeError) as exc:
            self.parsed = None
            self.measure_check.setText(f"✗ {exc}")
        self._values_changed()
        self._check_placement_soon()

    def current_profiles(self):
        return self.parsed if self.parsed is not None else self.stored_profiles

    def _values_changed(self, *_args):
        values = [s["value"] for p in self.current_profiles() for s in p["samples"]]
        self.colours.set_values(values, self.variable.text().strip(), self.units.text().strip())

    # -- placement ---------------------------------------------------------------
    def _placement_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_hint(
            "Where each investigation applies along the route. The layer is read live: edits to it "
            "update the plot (no re-import). Its IDs must match the measurement IDs exactly."))
        kinds = QHBoxLayout()
        self.kind_group = QButtonGroup(self)
        self.kind_table = QRadioButton("KP-range layer or table (start and end KP fields)")
        self.kind_polygons = QRadioButton("Polygon layer (where the route crosses each polygon)")
        for i, radio in enumerate((self.kind_table, self.kind_polygons)):
            self.kind_group.addButton(radio, i)
            kinds.addWidget(radio)
        kinds.addStretch()
        layout.addLayout(kinds)
        form = QFormLayout()
        self.place_layer = QComboBox()
        form.addRow("Layer", self.place_layer)
        self.id_field = FieldCombo(self.placement.get("id_field") or "", "field holding the investigation ID")
        form.addRow("Investigation ID field", self.id_field)
        layout.addLayout(form)
        self.kp_box = QWidget()
        kp_form = QFormLayout(self.kp_box)
        kp_form.setContentsMargins(0, 0, 0, 0)
        self.kp_form = KpTableForm(kp_form, self.placement, self.model)
        layout.addWidget(self.kp_box)
        self.place_check = _check_label()
        layout.addWidget(self.place_check)
        layout.addStretch()
        polygons = self.placement.get("kind") == sources.PLACEMENT_POLYGONS
        (self.kind_polygons if polygons else self.kind_table).setChecked(True)
        self._check_placement_soon = ui_helpers.coalesced(self, self._check_placement, 150)
        self._fill_place_layers()
        self.kind_group.buttonClicked.connect(self._kind_changed)
        self.place_layer.currentIndexChanged.connect(self._place_layer_changed)
        for widget in [self.id_field] + self.kp_form.widgets():
            widget.currentTextChanged.connect(self._check_placement_soon)
        self.kp_form.unit_combo.currentIndexChanged.connect(self._check_placement_soon)
        if self.kp_form.picker is not None:
            self.kp_form.picker.combo.currentIndexChanged.connect(self._check_placement_soon)
        self._place_layer_changed()
        return page

    def _fill_place_layers(self):
        stored = sources.resolve_layer(self.placement.get("layer"))
        current = self.place_layer.currentData() or (stored.id() if stored is not None else "")
        self.place_layer.blockSignals(True)
        self.place_layer.clear()
        self.place_layer.addItem("(choose a layer)", "")
        for layer in _vector_layers(polygons_only=self.kind_polygons.isChecked()):
            self.place_layer.addItem(layer.name(), layer.id())
        self.place_layer.setCurrentIndex(max(0, self.place_layer.findData(current)))
        self.place_layer.blockSignals(False)
        self.kp_box.setVisible(self.kind_table.isChecked())

    def _kind_changed(self, *_args):
        self._fill_place_layers()
        self._place_layer_changed()

    def _place_layer_changed(self, *_args):
        layer = QgsProject.instance().mapLayer(self.place_layer.currentData() or "")
        for widget in [self.id_field] + self.kp_form.widgets():
            widget.set_layer(layer)
        self._check_placement_soon()

    def placement_settings(self):
        layer = QgsProject.instance().mapLayer(self.place_layer.currentData() or "")
        if layer is None:
            return {}
        out = {"kind": sources.PLACEMENT_POLYGONS if self.kind_polygons.isChecked() else sources.PLACEMENT_KP_TABLE,
               "layer": sources.layer_ref(layer), "id_field": self.id_field.text()}
        if out["kind"] == sources.PLACEMENT_KP_TABLE:
            self.kp_form.apply(out)
        return out

    def _check_placement(self):
        settings = self.placement_settings()
        if not settings:
            self.place_check.setText("Choose the layer (you can also save without one and add it later).")
            return
        try:
            assignments, notes, _layer = sources.read_placement(self.model, settings)
        except (ValueError, RuntimeError) as exc:
            self.place_check.setText(f"✗ {exc}")
            return
        if not assignments:
            self.place_check.setText("✗ No usable KP ranges. " + "; ".join(notes))
            return
        ids = {a["source_id"] for a in assignments}
        text = (f"✓ {len(assignments)} KP range(s) for {len(ids)} investigation(s), KP "
                f"{min(a['start_kp'] for a in assignments):.3f}–{max(a['end_kp'] for a in assignments):.3f} "
                "on this plan's route.")
        known = {p["source_id"] for p in self.current_profiles()}
        unmatched = numeric.unmatched_ids(ids, known) if known else []
        if unmatched:
            text += (f" ⚠ {len(unmatched)} ID(s) have no measurements: " + ", ".join(unmatched[:6])
                     + (" …" if len(unmatched) > 6 else ""))
        if notes:
            text += " Notes: " + "; ".join(notes)
        self.place_check.setText(text)

    # -- result ------------------------------------------------------------------
    def _dataset_row(self):
        return {"dataset_id": self.dataset.get("dataset_id") or "", "name": self.name.text().strip(),
                "variable": self.variable.text().strip(), "units": self.units.text().strip()}

    def result(self):
        """``(dataset, profiles or None)`` — None keeps the stored measurements."""
        return self._result

    def _accept(self):
        try:
            row = self._dataset_row()
            if not row["name"] or not row["variable"]:
                raise ValueError("Enter a dataset name and a variable name.")
            profiles = None
            measurements = dict(self.measurements)
            if self.measurements_dirty or not self.stored_profiles:
                profiles = self._parse()
                measurements = self._measurement_settings()
                measurements["fingerprint"] = sources.source_fingerprint(self.source)
            elif (row["variable"], row["units"]) != (self.dataset.get("variable"), self.dataset.get("units")):
                profiles = [dict(p, variable=row["variable"], units=row["units"]) for p in self.stored_profiles]
            colours = self.colours.colours()
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        config = dict(self.dataset.get("config") or {})
        config.pop("legacy", None)
        config.update(measurements=measurements, placement=self.placement_settings(), colours=colours)
        self._result = (dict(self.dataset, **row, config=config), profiles)
        self.accept()


# -- checks and inspection -----------------------------------------------------------
def check_dialog(dataset, profiles, assignments, notes, bounds, scope, parent, on_open=None):
    rows, findings = numeric.check_dataset(profiles, assignments, bounds, scope)
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"Check dataset — {dataset.get('name')}")
    dialog.resize(980, 640)
    layout = QVBoxLayout(dialog)
    units = dataset.get("units") or ""
    report = QPlainTextEdit("\n".join([numeric.summary_text(profiles, units) + "."] + findings + notes))
    report.setReadOnly(True)
    report.setMaximumHeight(170)
    layout.addWidget(report)

    def fmt(value):
        return "" if value is None else f"{value:g}"

    headers = ["Investigation", "Depth from (m)", "Depth to (m)", "Samples", "Missing",
               f"Min ({units})", f"Max ({units})", "KP ranges on this route", "Status"]
    dialog.table_rows = [[r["source_id"], fmt(r["top"]), fmt(r["base"]), r["samples"], r["missing"],
                          fmt(r["min"]), fmt(r["max"]),
                          "; ".join(f"{a:.3f}–{b:.3f}" for a, b in r["ranges"]), r["status"]]
                         for r in rows]
    table = QTableView()
    table.setModel(RowsModel(headers, dialog.table_rows, table))
    if on_open is not None:
        table.doubleClicked.connect(lambda index: on_open(dialog.table_rows[index.row()][0]))
    layout.addWidget(_hint("Double-click a row to plot that investigation's measurements."))
    layout.addWidget(table, 1)
    buttons = QHBoxLayout()
    export = QPushButton("Export this table…")
    export.clicked.connect(lambda: export_csv(dialog, f"{dataset.get('name')} check.csv", headers,
                                              dialog.table_rows))
    buttons.addWidget(export)
    buttons.addStretch()
    close = QPushButton("Close")
    close.clicked.connect(dialog.accept)
    buttons.addWidget(close)
    layout.addLayout(buttons)
    return dialog


def export_csv(parent, suggested, headers, rows, path=None):
    if path is None:
        path, _ = QFileDialog.getSaveFileName(parent, "Export CSV", re.sub(r'[\\/:*?"<>|]', "_", suggested),
                                              "CSV (*.csv)")
    if not path:
        return ""
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)
    except OSError as exc:
        QMessageBox.warning(parent, "Export CSV", str(exc))
        return ""
    return path


def source_dialog(source, profiles, parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Measurements — " + source)
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
        table_model = RowsModel(["Depth (m)", "Drawn from (m)", "Drawn to (m)", "Value", "Flags", "Data row"],
                                [[s[k] for k in keys] for s in profile["samples"]], table)
        table.setModel(table_model)
        page_layout.addWidget(table, 1)
        tabs.addTab(page, f"{profile['variable']} ({profile['units']})")
    return dialog
