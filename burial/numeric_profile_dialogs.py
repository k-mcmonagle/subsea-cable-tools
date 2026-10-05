"""Column-mapped numeric profile imports and source inspection."""
import json
import os
import re

import pyqtgraph as pg
from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QAbstractTableModel, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableView, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK, HEADER_RESIZE_MODE_STRETCH
from . import numeric_profiles as numeric, ui_helpers
from .ground_dialogs import KpReferenceWidget
from .numeric_profile_plot import RAMPS, ramp_colours
from .plan_import import read_grid
from .tabs.attribute_widgets import AttributeRulesTable


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


def _attribute_text(value):
    """A layer attribute as table text; NULL becomes blank (missing)."""
    if value is None:
        return ""
    if type(value).__name__ == "QVariant":
        if not value.isValid() or value.isNull():
            return ""
        value = value.value()
    return str(value).strip()


# Larger tables are checked when Import is pressed rather than on every edit.
_LIVE_CHECK_ROWS = 200000


class NumericImportDialog(QDialog):
    def __init__(self, model, dock, *, assignments=False, known_ids=(), parent=None):
        super().__init__(parent)
        self.assignments = assignments
        self.known_ids = set(known_ids)
        self.result_rows = []
        self.grid, self.path, self.layer_name = [], "", ""
        self._headers = []
        self.setWindowTitle("Assign investigations to KP ranges" if assignments else "Import numeric depth profiles")
        self.resize(850, 760)
        layout = QVBoxLayout(self)
        hint = QLabel("Choose a file or a loaded layer, then map its columns using the preview; the check "
                      "line below reports the result as you go. Investigation IDs are matched exactly. "
                      "Blank/NA values stay missing. Imports replace matching source / variable / unit "
                      "profiles across this project; route assignments are saved separately."
                      if not assignments else
                      "Each row applies one investigation's profile from its start KP to its end KP. Choose "
                      "a file or a loaded layer/table (geometry is not needed). The IDs must match the "
                      "imported profiles. This replaces the plan's assignment table.")
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
        layer_row = QHBoxLayout()
        self.layer = QComboBox()
        self.layer.addItem("(none — use a file)", "")
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                self.layer.addItem(layer.name(), layer.id())
        self.layer.currentIndexChanged.connect(self._load_layer)
        layer_row.addWidget(QLabel("or a loaded layer / table:"))
        layer_row.addWidget(self.layer, 1)
        layout.addLayout(layer_row)
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
            self.name.setPlaceholderText("Variable name (when not mapped)")
            self.units = QLineEdit()
            self.units.setPlaceholderText("Units (when not mapped)")
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
            self.missing.setPlaceholderText("Additional missing-value codes, separated by ;")
            for widget in (QLabel("Point sample support"), self.support, self.missing):
                point_row.addWidget(widget)
            layout.addLayout(point_row)
        self.check = QLabel()
        self.check.setWordWrap(True)
        self.check.setTextFormat(getattr(Qt, "TextFormat", Qt).PlainText)
        layout.addWidget(self.check)
        self._check_soon = ui_helpers.coalesced(self, self._check, 150)
        for combo in list(self.mapping.values()) + [self.scale]:
            combo.currentIndexChanged.connect(self._check_soon)
        self.decimal_comma.toggled.connect(self._check_soon)
        if not assignments:
            self.format.currentIndexChanged.connect(self._check_soon)
            self.name.textChanged.connect(self._check_soon)
            self.units.textChanged.connect(self._check_soon)
            self.support.valueChanged.connect(self._check_soon)
            self.missing.textChanged.connect(self._check_soon)
            self.wide.itemChanged.connect(self._check_soon)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.button(BUTTON_BOX_OK).setText("Assign" if assignments else "Import")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Investigation table", "", "Tables (*.csv *.tsv *.txt *.xlsx *.xlsm)")
        if path:
            self.path, self.layer_name = path, ""
            self.layer.blockSignals(True)
            self.layer.setCurrentIndex(0)
            self.layer.blockSignals(False)
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

    def _load_layer(self, *_args):
        layer = QgsProject.instance().mapLayer(self.layer.currentData() or "")
        if layer is None:
            return
        grid = [layer.fields().names()]
        for feature in layer.getFeatures():
            grid.append([_attribute_text(v) for v in feature.attributes()])
            if len(grid) >= 1000002:
                QMessageBox.warning(self, self.windowTitle(), "Layer exceeds one million features.")
                return
        self.grid, self.path, self.layer_name = grid, "", layer.name()
        self.sheet.blockSignals(True)
        self.sheet.clear()
        self.sheet.blockSignals(False)
        self.file_label.setText("Layer: " + layer.name())
        self.header.blockSignals(True)
        self.header.setValue(1)
        self.header.blockSignals(False)
        self._columns()

    def source_text(self):
        return f"layer {self.layer_name}" if self.layer_name else self.path

    def _columns(self, *_args):
        i = self.header.value() - 1
        if i >= len(self.grid):
            return
        headers = self.grid[i]
        self.preview_model = RowsModel(headers, self.grid[i + 1:i + 21], self)
        self.preview.setModel(self.preview_model)
        # Nothing is guessed from column names: the user maps every role.
        # A role keeps its column when the same header is still present.
        for combo in self.mapping.values():
            previous = combo.currentData()
            previous = self._headers[previous] if previous is not None and previous < len(self._headers) else None
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(unmapped)", None)
            for col, label in enumerate(headers):
                combo.addItem(f"{col + 1}: {label}", col)
            combo.setCurrentIndex(headers.index(previous) + 1 if previous in headers else 0)
            combo.blockSignals(False)
        self._headers = list(headers)
        if not self.assignments:
            self.wide.blockSignals(True)
            self.wide.setRowCount(len(headers))
            for col, label in enumerate(headers):
                box = QCheckBox()
                box.toggled.connect(self._check_soon)
                self.wide.setCellWidget(col, 0, box)
                for j, text in enumerate((label, label, ""), 1):
                    item = QTableWidgetItem(text)
                    if j == 1:
                        item.setFlags(item.flags() & ~getattr(Qt, "ItemFlag", Qt).ItemIsEditable)
                    self.wide.setItem(col, j, item)
            self.wide.blockSignals(False)
        self._check_soon()

    def _parse(self, kp_map=None):
        if not self.grid:
            raise ValueError("Choose a table or layer first.")
        rows = self.grid[self.header.value():]
        mapping = {key: combo.currentData() for key, combo in self.mapping.items()}
        if self.assignments:
            return numeric.import_assignments(
                rows, mapping, kp_map, kp_scale=self.scale.currentData(),
                decimal_comma=self.decimal_comma.isChecked(),
                source_ref=f"{self.source_text()} — {self.reference.source_label()}")
        variables = [(i, self.wide.item(i, 2).text(), self.wide.item(i, 3).text())
                     for i in range(self.wide.rowCount()) if self.wide.cellWidget(i, 0).isChecked()]
        if self.format.currentIndex() == 1 and not variables:
            raise ValueError("Include at least one numeric variable column.")
        return numeric.import_profiles(
            rows, mapping, variables=variables if self.format.currentIndex() else (),
            variable=self.name.text(), units=self.units.text(), depth_scale=self.scale.currentData(),
            sample_support=self.support.value(), decimal_comma=self.decimal_comma.isChecked(),
            missing=self.missing.text().split(";"),
            provenance={"file": self.source_text(), "sheet": self.sheet.currentText(),
                        "header_row": self.header.value(), "mapping": mapping,
                        "variables": variables, "depth_scale": self.scale.currentData(),
                        "sample_support_m": self.support.value(),
                        "missing_tokens": self.missing.text(), "decimal_comma": self.decimal_comma.isChecked()})

    def summary(self, result):
        if self.assignments:
            ids = {r["source_id"] for r in result}
            text = (f"✓ {len(result)} KP range(s) for {len(ids)} investigation(s), "
                    f"KP {min(r['start_kp'] for r in result):g}–{max(r['end_kp'] for r in result):g} as delivered.")
            unmatched = numeric.unmatched_ids(ids, self.known_ids)
            if unmatched:
                text += (f" ⚠ {len(unmatched)} ID(s) have no imported profile: "
                         + ", ".join(unmatched[:6]) + (" …" if len(unmatched) > 6 else ""))
            return text
        names = sorted({f"{p['variable']} ({p['units'] or 'unitless'})" for p in result})
        samples = sum(len(p["samples"]) for p in result)
        measured = sum(s["value"] is not None for p in result for s in p["samples"])
        return (f"✓ {len({p['source_id'] for p in result})} investigation(s); {', '.join(names)}; "
                f"{samples} depth sample(s), {samples - measured} missing.")

    def _check(self):
        if not self.grid:
            self.check.setText("")
            return
        if len(self.grid) > _LIVE_CHECK_ROWS:
            self.check.setText("Large table: it is checked when you press Import.")
            return
        try:
            self.check.setText(self.summary(self._parse()))
        except (ValueError, OSError, IndexError, TypeError) as exc:
            self.check.setText(f"✗ {exc}")

    def _accept(self):
        try:
            self.result_rows = self._parse(self.reference.build_map() if self.assignments else None)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.accept()


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
        """Overridden by the dialog to refresh its summary."""

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


class ColourClassesDialog(QDialog):
    """Value classes for one variable, each with a colour, and a live summary."""

    def __init__(self, variable, classes=(), values=(), parent=None):
        super().__init__(parent)
        self.variable = list(variable)
        self.values = [v for v in values if v is not None]
        self.classes = []
        self.setWindowTitle(f"Colour classes — {variable[0]} ({variable[1] or 'unitless'})")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        hint = QLabel("Each row colours one range of values. Choose ≥ or > for the From value and < or ≤ "
                      "for the To value; leave a side blank for an open-ended class. The first matching row "
                      "wins. Values no class covers are drawn dark grey.")
        hint.setWordWrap(True)
        hint.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(hint)
        quick = QHBoxLayout()
        self.breaks = QLineEdit()
        self.breaks.setPlaceholderText("Break values, separated by commas")
        self.breaks.setToolTip("Replaces the rows with one class below the first break, one between each pair "
                               "(≥ lower, < upper) and one above the last.")
        self.ramp = QComboBox()
        self.ramp.addItems(list(RAMPS))
        make = QPushButton("Create classes")
        make.clicked.connect(self._from_breaks)
        self.breaks.returnPressed.connect(self._from_breaks)
        for widget in (QLabel("Breaks"), self.breaks, QLabel("colours"), self.ramp, make):
            quick.addWidget(widget)
        layout.addLayout(quick)
        self.table = ColourRangeTable(self)
        self.table.set_attribute_name_provider(lambda: self.variable[0])
        self.table.changed = self._summarise
        self.table.table.itemChanged.connect(self._summarise)
        layout.addWidget(self.table, 1)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(getattr(Qt, "TextFormat", Qt).RichText)
        layout.addWidget(self.summary)
        box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.table.set_rules(list(classes))
        self._summarise()

    def _from_breaks(self):
        try:
            values = [float(v) for v in re.split(r"[;,\s]+", self.breaks.text().strip()) if v]
            count = len(set(values)) + 1
            self.table.set_rules(numeric.classes_from_breaks(values, ramp_colours(self.ramp.currentText(), count)))
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), f"Breaks must be numbers: {exc}")
        self._summarise()

    def _parse(self):
        problems = self.table.invalid_rows()
        if problems:
            raise ValueError("; ".join(problems))
        return numeric.normalise_classes(self.table.rules())

    def _summarise(self, *_args):
        try:
            classes = self._parse()
        except ValueError as exc:
            self.summary.setText(f"<span style='color:#b00020'>{_html(exc)}</span>")
            return
        name, units = self.variable[0], self.variable[1]
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
                return "no assigned samples"
            percent = 100 * count / total
            return f"{count} sample(s), " + ("<1%" if 0 < percent < 1 else f"{percent:.0f}%")

        lines = [f"<span style='color:{c['colour']}; font-size:15px'>■</span> "
                 f"<b>{_html(numeric.class_label(c, name))}</b> {_html(units)} — {share(counts[i])}"
                 for i, c in enumerate(classes)]
        if total:
            lines.append(f"Outside every class: {share(outside)}")
        lines += [_html(note) for note in numeric.class_coverage(classes, name)]
        self.summary.setText("<br>".join(lines))

    def _accept(self):
        try:
            self.classes = self._parse()
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self.accept()


def _html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
