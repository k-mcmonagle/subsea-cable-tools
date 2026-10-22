# -*- coding: utf-8 -*-
"""The Reporting tab: burial graphs as PDF (and PNG, SVG, listing CSV) from the current assessment.

The pages show the Assessment tab's result (its datasets, rule and range),
laid out by a template (:mod:`..acquired.report_template`): page size, title
and subtitle with tokens, a title block of fields, KP per page with an
overlap, and the panels in order with their heights and axis limits. The
preview is the same drawing as the PDF (:mod:`..acquired.graph_page`).

Templates: two built in (A3 and A4), any number saved in the burial data
file (each save a version), and JSON files to share between projects.
Exports run in the background and replace the target file only when
complete.
"""

from __future__ import annotations

import copy
import csv
import os
import re
from typing import Dict, List, Optional

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QSettings, Qt, QTimer
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...plotkit.widgets import entries_button
from ...plugin_log import log_exception
from ..acquired import assessment as A
from ..acquired import graph_page as G
from ..acquired import report_template as T
from ..acquired import roles as R
from ..acquired.store import AcquiredStoreError
from ..change_log import current_user
from ..ui_helpers import hint_style, status_style

_HORIZONTAL = getattr(getattr(Qt, "Orientation", Qt), "Horizontal")
_ALIGN_CENTER = getattr(getattr(Qt, "AlignmentFlag", Qt), "AlignCenter")
_SETTINGS = "SubseaCableTools/BurialPlanner/reporting"
BUILTIN = "builtin"
SAVED = "saved"
CHANNELS = [(R.WATER_DEPTH, "Water depth (m)", True), (R.PITCH, "Pitch (°)", False), (R.ROLL, "Roll (°)", False)]


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "")).strip("_") or "burial"


class _Section(QWidget):
    """A titled group of settings that folds away (click the title)."""

    def __init__(self, title: str, open_: bool = True):
        super().__init__()
        self._title = title
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        self.header = QToolButton()
        self.header.setAutoRaise(True)
        self.header.setStyleSheet("QToolButton { font-weight: 600; border: none; }")
        self.header.clicked.connect(lambda: self.set_open(not self.body.isVisible()))
        layout.addWidget(self.header)
        self.body = QWidget()
        layout.addWidget(self.body)
        self.set_open(open_)

    def set_open(self, on: bool) -> None:
        self.body.setVisible(on)
        self.header.setText(("▾ " if on else "▸ ") + self._title)


class ReportingTab(QWidget):
    def __init__(self, controller, model, assessment_tab, dock=None, parent=None, persist: bool = True):
        super().__init__(parent)
        self.controller = controller
        self.model = model
        self.assessment_tab = assessment_tab
        self.dock = dock
        self.persist = persist
        self.template: Dict = T.default_template()
        self.template_source = (BUILTIN, 0)
        self.report: Optional[G.Report] = None
        self.page = 0
        self.last_export = None
        self._loading = False
        self._dirty = True
        self._custom_range: Optional[tuple] = None

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Template:"))
        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(200)
        self.template_combo.activated.connect(self._template_chosen)
        bar.addWidget(self.template_combo)
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip("Save the template in the burial data file (a new version when it changed).")
        self.save_button.clicked.connect(lambda: self._save(False))
        bar.addWidget(self.save_button)
        bar.addWidget(entries_button("More", "More template actions",
                                     [("Save as…", lambda: self._save(True)), None,
                                      ("Import a template file…", self._import_template),
                                      ("Export the template to a file…", self._export_template)]))
        bar.addSpacing(12)
        bar.addWidget(QLabel("Range:"))
        self.range_combo = QComboBox()
        self.range_combo.addItem("Assessed range", "all")
        self.range_combo.addItem("Custom KP range…", "custom")
        self.range_combo.activated.connect(self._range_chosen)
        bar.addWidget(self.range_combo)
        bar.addSpacing(12)
        self.export_button = entries_button("Export", "Write the pages (and the listing behind them).",
                                            [("PDF…", self._export_pdf), ("PNG images…", self._export_png),
                                             ("SVG drawings…", self._export_svg), None,
                                             ("Listing CSV…", self._export_listing)])
        bar.addWidget(self.export_button)
        self.state = QLabel("")
        self.state.setStyleSheet(hint_style())
        bar.addWidget(self.state, 1)
        layout.addLayout(bar)

        splitter = QSplitter(_HORIZONTAL)
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        nav = QHBoxLayout()
        self.prev_button = QToolButton()
        self.prev_button.setText("◀")
        self.prev_button.clicked.connect(lambda: self._go(-1))
        self.next_button = QToolButton()
        self.next_button.setText("▶")
        self.next_button.clicked.connect(lambda: self._go(1))
        self.page_label = QLabel("")
        nav.addWidget(self.prev_button)
        nav.addWidget(self.page_label)
        nav.addWidget(self.next_button)
        nav.addStretch(1)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        nav.addWidget(self.message, 3)
        preview_layout.addLayout(nav)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(_ALIGN_CENTER)
        self.page_view = QLabel()
        self.page_view.setAlignment(_ALIGN_CENTER)
        self.scroll.setWidget(self.page_view)
        preview_layout.addWidget(self.scroll, 1)
        splitter.addWidget(preview)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings = QWidget()
        self.settings_layout = QVBoxLayout(settings)
        self._build_settings()
        self.settings_layout.addStretch(1)
        settings_scroll.setWidget(settings)
        splitter.addWidget(settings_scroll)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([800, 360])
        layout.addWidget(splitter, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._rebuild)
        self.assessment_tab.resultChanged.connect(self._schedule)
        self.controller.changed.connect(self._refresh_templates)
        self.controller.busyChanged.connect(lambda busy: self.export_button.setEnabled(not busy))
        self._refresh_templates()
        self._load_template(self.template)

    # -- settings widgets ---------------------------------------------------------------
    def _build_settings(self) -> None:
        title = _Section("Title")
        form = QFormLayout(title.body)
        self.title_edit = QLineEdit()
        self.subtitle_edit = QLineEdit()
        form.addRow("Title", self.title_edit)
        form.addRow("Subtitle", self.subtitle_edit)
        self.fields = QTableWidget(0, 2)
        self.fields.setHorizontalHeaderLabels(["Field", "Value"])
        self.fields.horizontalHeader().setStretchLastSection(True)
        self.fields.setMinimumHeight(150)
        form.addRow(self.fields)
        buttons = QHBoxLayout()
        add = QPushButton("Add field")
        add.clicked.connect(self._add_field)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_field)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        form.addRow(buttons)
        tokens = QLabel("Tokens: " + T.tokens_help())
        tokens.setWordWrap(True)
        tokens.setStyleSheet(hint_style())
        form.addRow(tokens)
        self.settings_layout.addWidget(title)

        page = _Section("Page")
        form = QFormLayout(page.body)
        self.size_combo = QComboBox()
        for name in list(T.PAGE_SIZES) + ["Custom"]:
            self.size_combo.addItem(name)
        self.orientation = QComboBox()
        self.orientation.addItem("Landscape", "landscape")
        self.orientation.addItem("Portrait", "portrait")
        self.custom_w = self._spin(100, 5000, 0, " mm")
        self.custom_h = self._spin(100, 5000, 0, " mm")
        self.margin = self._spin(0, 50, 1, " mm")
        self.kp_per_page = self._spin(0.01, 1000, 3, " km")
        self.overlap = self._spin(0, 50, 1, " %")
        self.kp_tick = self._spin(0, 100, 3, " km")
        self.kp_tick.setSpecialValueText("auto")
        self.reverse = QCheckBox("KP increasing right to left")
        self.legend = QCheckBox("Legend")
        form.addRow("Size", self.size_combo)
        form.addRow("Orientation", self.orientation)
        form.addRow("Width × height", self._pair(self.custom_w, self.custom_h))
        form.addRow("Margin", self.margin)
        form.addRow("KP per page", self.kp_per_page)
        form.addRow("Overlap", self.overlap)
        form.addRow("KP tick every", self.kp_tick)
        form.addRow(self.reverse)
        form.addRow(self.legend)
        self.settings_layout.addWidget(page)

        panels = _Section("Panels")
        box = QVBoxLayout(panels.body)
        self.panel_list = QListWidget()
        self.panel_list.setMaximumHeight(120)
        self.panel_list.currentRowChanged.connect(self._show_panel)
        box.addWidget(self.panel_list)
        row = QHBoxLayout()
        row.addWidget(entries_button("Add", "Add a panel", [(text, lambda k=key: self._add_panel(k))
                                                  for key, text in T.PANEL_KINDS.items()]))
        for text, slot in (("Remove", self._remove_panel), ("Up", lambda: self._move_panel(-1)),
                           ("Down", lambda: self._move_panel(1))):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)
        self.panel_form = QWidget()
        form = QFormLayout(self.panel_form)
        self.panel_channel = QComboBox()
        self.panel_label = QLineEdit()
        self.panel_weight = self._spin(0.2, 20, 1, "")
        self.panel_weight.setToolTip("The panel's share of the page height (bars have a fixed height).")
        self.panel_auto = QCheckBox("Axis from the data")
        self.panel_min = self._spin(-100000, 100000, 2, "")
        self.panel_max = self._spin(-100000, 100000, 2, "")
        self.panel_invert = QCheckBox("Values increase downwards (depth)")
        self.panel_datasets = QCheckBox("Each dataset (faint)")
        self.panel_target = QCheckBox("Target")
        self.panel_below = QCheckBox("Shade below target")
        form.addRow("Channel", self.panel_channel)
        form.addRow("Axis label", self.panel_label)
        form.addRow("Height share", self.panel_weight)
        form.addRow(self.panel_auto)
        form.addRow("Axis from / to", self._pair(self.panel_min, self.panel_max))
        form.addRow(self.panel_invert)
        form.addRow(self.panel_datasets)
        form.addRow(self.panel_target)
        form.addRow(self.panel_below)
        box.addWidget(self.panel_form)
        self.settings_layout.addWidget(panels)

        pages = _Section("Pages and footer", open_=False)
        form = QFormLayout(pages.body)
        self.summary_page = QCheckBox("Summary pages (figures, below-target ranges, sections)")
        self.sources_page = QCheckBox("Data sources page (every file and its checksum)")
        self.footer = QCheckBox("Footer naming the data, route, rule and maker")
        self.page_numbers = QCheckBox("Page numbers")
        self.footer_text = QLineEdit()
        for widget in (self.summary_page, self.sources_page, self.footer, self.page_numbers):
            form.addRow(widget)
        form.addRow("Footer text", self.footer_text)
        self.settings_layout.addWidget(pages)

        for widget in (self.title_edit, self.subtitle_edit, self.footer_text, self.panel_label):
            widget.editingFinished.connect(self._read_widgets)
        for widget in (self.size_combo, self.orientation, self.panel_channel):
            widget.currentIndexChanged.connect(self._read_widgets)
        for widget in (self.custom_w, self.custom_h, self.margin, self.kp_per_page, self.overlap, self.kp_tick,
                       self.panel_weight, self.panel_min, self.panel_max):
            widget.valueChanged.connect(self._read_widgets)
        for widget in (self.reverse, self.legend, self.panel_auto, self.panel_invert, self.panel_datasets,
                       self.panel_target, self.panel_below, self.summary_page, self.sources_page, self.footer,
                       self.page_numbers):
            widget.toggled.connect(self._read_widgets)
        self.fields.itemChanged.connect(self._read_widgets)

    @staticmethod
    def _spin(lo: float, hi: float, decimals: int, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        spin.setKeyboardTracking(False)
        return spin

    @staticmethod
    def _pair(a, b) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(a)
        layout.addWidget(b)
        return widget

    # -- templates -----------------------------------------------------------------------
    def _saved_templates(self) -> List[Dict]:
        store = self.controller.store
        if store is None or self.controller.busy:
            return []
        try:
            return store.list_templates()
        except AcquiredStoreError:
            return []

    def _refresh_templates(self, *_args) -> None:
        current = self.template_combo.currentData()
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        for index, template in enumerate(T.builtin_templates()):
            self.template_combo.addItem(f"{template['name']} (built in)", (BUILTIN, index))
        for row in self._saved_templates():
            self.template_combo.addItem(f"{row['name']} (v{row['version']})", (SAVED, row["template_id"]))
        index = self.template_combo.findData(current) if current is not None else -1
        if index < 0:
            index = self.template_combo.findData(self.template_source)
        self.template_combo.setCurrentIndex(max(0, index))
        self.template_combo.blockSignals(False)
        self.save_button.setEnabled(self.controller.store is not None)

    def _template_chosen(self, index: int) -> None:
        kind, key = self.template_combo.itemData(index)
        if kind == BUILTIN:
            template = T.builtin_templates()[key]
        else:
            template = next((r["template"] for r in self._saved_templates() if r["template_id"] == key), None)
            if template is None:
                return
        self.template_source = (kind, key)
        self._load_template(template)

    def _save(self, ask_name: bool) -> None:
        store = self.controller.store
        if store is None:
            return
        name = self.template.get("name") or "Burial graph"
        if ask_name or self.template_source[0] == BUILTIN:
            text, ok = QInputDialog.getText(self, "Save the template", "Name:",
                                            text=name if not name.endswith("(built in)") else "Burial graph")
            if not ok or not text.strip():
                return
            name = text.strip()
        self.template["name"] = name
        try:
            template_id = store.save_template(name, self.template)
        except AcquiredStoreError as exc:
            QMessageBox.warning(self, "Save the template", str(exc))
            return
        self.template_source = (SAVED, template_id)
        self._refresh_templates()
        self.template_combo.setCurrentIndex(max(0, self.template_combo.findData((SAVED, template_id))))
        self.state.setText(f"Saved '{name}'.")

    def _import_template(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Import a burial graph template", "", "Template (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                template = T.from_json(handle.read())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Import a template", f"Could not read {os.path.basename(path)}: {exc}")
            return
        self.template_source = (BUILTIN, -1)
        self._load_template(template)
        self.state.setText(f"Imported '{template['name']}': Save keeps it in this project.")

    def _export_template(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(self, "Export the template", f"{_safe(self.template['name'])}.json",
                                                    "Template (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(T.to_json(self.template))
        except OSError as exc:
            QMessageBox.warning(self, "Export the template", f"Could not write {path}: {exc.strerror or exc}")

    def _load_template(self, template: Dict) -> None:
        self.template = T.normalise(copy.deepcopy(template))
        self._loading = True
        try:
            t = self.template
            self.title_edit.setText(t["title"])
            self.subtitle_edit.setText(t["subtitle"])
            self.fields.setRowCount(len(t["fields"]))
            for row, (key, value) in enumerate(t["fields"]):
                self.fields.setItem(row, 0, QTableWidgetItem(key))
                self.fields.setItem(row, 1, QTableWidgetItem(value))
            page = t["page"]
            self.size_combo.setCurrentText(page["size"] if page["size"] in T.PAGE_SIZES else "Custom")
            self.orientation.setCurrentIndex(max(0, self.orientation.findData(page["orientation"])))
            self.custom_w.setValue(page["width_mm"])
            self.custom_h.setValue(page["height_mm"])
            self.margin.setValue(page["margin_mm"])
            self.kp_per_page.setValue(t["kp_per_page"])
            self.overlap.setValue(t["overlap_pct"])
            self.kp_tick.setValue(t["kp_tick"] or 0.0)
            self.reverse.setChecked(t["reverse"])
            self.legend.setChecked(t["legend"])
            self.summary_page.setChecked(t["summary_page"])
            self.sources_page.setChecked(t["sources_page"])
            self.footer.setChecked(t["footer"])
            self.page_numbers.setChecked(t["page_numbers"])
            self.footer_text.setText(t["footer_text"])
            self._fill_panels()
        finally:
            self._loading = False
        self._update_custom_size()
        self._schedule()

    # -- panels --------------------------------------------------------------------------
    def _fill_panels(self, select: int = 0) -> None:
        self.panel_list.blockSignals(True)
        self.panel_list.clear()
        for panel in self.template["panels"]:
            self.panel_list.addItem(T.panel_title(panel))
        self.panel_list.blockSignals(False)
        if self.template["panels"]:
            self.panel_list.setCurrentRow(min(select, len(self.template["panels"]) - 1))
        self._show_panel(self.panel_list.currentRow())

    def _channel_choices(self) -> List[tuple]:
        choices = [(key, label) for key, label, _d in CHANNELS]
        result = self.assessment_tab.result
        if result is not None:
            labels = A.channel_labels(result)
            for series in result.series:
                for key in series.channels:
                    label, unit = labels.get(key, (key, ""))
                    if key not in [c[0] for c in choices]:
                        choices.append((key, f"{label} ({unit})" if unit else label))
        return choices

    def _show_panel(self, row: int) -> None:
        panels = self.template["panels"]
        if not 0 <= row < len(panels):
            self.panel_form.setVisible(False)
            return
        panel = panels[row]
        bar = panel["kind"] in T.BAR_KINDS
        self.panel_form.setVisible(not bar)
        if bar:
            return
        was = self._loading
        self._loading = True
        try:
            self.panel_channel.clear()
            for key, label in self._channel_choices():
                self.panel_channel.addItem(label, key)
            channel = panel.get("channel")
            if channel and self.panel_channel.findData(channel) < 0:
                self.panel_channel.addItem(channel, channel)
            self.panel_channel.setCurrentIndex(max(0, self.panel_channel.findData(channel)))
            self.panel_channel.setEnabled(panel["kind"] == T.PANEL_CHANNEL)
            self.panel_label.setText(panel.get("label", ""))
            self.panel_weight.setValue(panel.get("weight", 1.0))
            auto = panel.get("y_min") is None or panel.get("y_max") is None
            self.panel_auto.setChecked(auto)
            self.panel_min.setValue(panel.get("y_min") or 0.0)
            self.panel_max.setValue(panel.get("y_max") or 0.0)
            self.panel_min.setEnabled(not auto)
            self.panel_max.setEnabled(not auto)
            self.panel_invert.setChecked(panel.get("invert", False))
            dol = panel["kind"] == T.PANEL_DOL
            for widget, key in ((self.panel_datasets, "datasets"), (self.panel_target, "target"),
                                (self.panel_below, "below")):
                widget.setVisible(dol)
                widget.setChecked(panel.get(key, True))
        finally:
            self._loading = was

    def _add_panel(self, kind: str) -> None:
        panel = {"kind": kind}
        if kind == T.PANEL_CHANNEL:
            panel.update(channel=R.WATER_DEPTH, label="Water depth (m)", weight=1.5, invert=True)
        elif kind == T.PANEL_DOL:
            panel.update(label="DOL (m)", weight=4.0, invert=True)
        self.template["panels"].append(panel)
        self.template = T.normalise(self.template)
        self._fill_panels(len(self.template["panels"]) - 1)
        self._schedule()

    def _remove_panel(self) -> None:
        row = self.panel_list.currentRow()
        if 0 <= row < len(self.template["panels"]):
            del self.template["panels"][row]
            self._fill_panels(row)
            self._schedule()

    def _move_panel(self, step: int) -> None:
        row = self.panel_list.currentRow()
        target = row + step
        panels = self.template["panels"]
        if 0 <= row < len(panels) and 0 <= target < len(panels):
            panels[row], panels[target] = panels[target], panels[row]
            self._fill_panels(target)
            self._schedule()

    def _add_field(self) -> None:
        row = self.fields.rowCount()
        self.fields.insertRow(row)
        self.fields.setItem(row, 0, QTableWidgetItem("Field"))
        self.fields.setItem(row, 1, QTableWidgetItem(""))

    def _remove_field(self) -> None:
        row = self.fields.currentRow()
        if row >= 0:
            self.fields.removeRow(row)
            self._read_widgets()

    def _update_custom_size(self) -> None:
        custom = self.size_combo.currentText() == "Custom"
        self.custom_w.setEnabled(custom)
        self.custom_h.setEnabled(custom)

    def _read_widgets(self, *_args) -> None:
        """The widgets into the working template, then a new preview."""
        if self._loading:
            return
        t = self.template
        t["title"], t["subtitle"] = self.title_edit.text(), self.subtitle_edit.text()
        fields = []
        for row in range(self.fields.rowCount()):
            key = self.fields.item(row, 0).text() if self.fields.item(row, 0) else ""
            value = self.fields.item(row, 1).text() if self.fields.item(row, 1) else ""
            fields.append([key, value])
        t["fields"] = fields
        size = self.size_combo.currentText()
        t["page"].update(size=size, orientation=self.orientation.currentData(), margin_mm=self.margin.value())
        if size == "Custom":
            t["page"].update(width_mm=self.custom_w.value(), height_mm=self.custom_h.value())
        self._update_custom_size()
        t["kp_per_page"] = self.kp_per_page.value()
        t["overlap_pct"] = self.overlap.value()
        t["kp_tick"] = self.kp_tick.value() or None
        t["reverse"], t["legend"] = self.reverse.isChecked(), self.legend.isChecked()
        t["summary_page"], t["sources_page"] = self.summary_page.isChecked(), self.sources_page.isChecked()
        t["footer"], t["page_numbers"] = self.footer.isChecked(), self.page_numbers.isChecked()
        t["footer_text"] = self.footer_text.text()
        row = self.panel_list.currentRow()
        if 0 <= row < len(t["panels"]) and t["panels"][row]["kind"] not in T.BAR_KINDS:
            panel = t["panels"][row]
            if panel["kind"] == T.PANEL_CHANNEL and self.panel_channel.currentData():
                old_label = panel.get("label", "")
                panel["channel"] = self.panel_channel.currentData()
                if not self.panel_label.isModified() and old_label in [c[1] for c in self._channel_choices()] + [""]:
                    panel["label"] = self.panel_channel.currentText()
                    self.panel_label.setText(panel["label"])
            if self.panel_label.isModified() or panel["kind"] != T.PANEL_CHANNEL:
                panel["label"] = self.panel_label.text()
            panel["weight"] = self.panel_weight.value()
            auto = self.panel_auto.isChecked()
            self.panel_min.setEnabled(not auto)
            self.panel_max.setEnabled(not auto)
            panel["y_min"] = None if auto else self.panel_min.value()
            panel["y_max"] = None if auto else self.panel_max.value()
            panel["invert"] = self.panel_invert.isChecked()
            if panel["kind"] == T.PANEL_DOL:
                panel.update(datasets=self.panel_datasets.isChecked(), target=self.panel_target.isChecked(),
                             below=self.panel_below.isChecked())
            item = self.panel_list.item(row)
            if item is not None:
                item.setText(T.panel_title(panel))
        self._schedule()

    # -- range ---------------------------------------------------------------------------
    def _range_chosen(self, index: int) -> None:
        if self.range_combo.itemData(index) != "custom":
            self._custom_range = None
            self._schedule()
            return
        result = self.assessment_tab.result
        lo, hi = (result.extent_km if result is not None and result.extent_km else (0.0, 1.0))
        text, ok = QInputDialog.getText(self, "KP range", "KP from and to (km), e.g. 10 15:",
                                        text=f"{lo:.3f} {hi:.3f}")
        numbers = re.findall(r"-?\d+(?:\.\d+)?", text) if ok else []
        if len(numbers) >= 2 and float(numbers[0]) != float(numbers[1]):
            self._custom_range = tuple(sorted((float(numbers[0]), float(numbers[1]))))
            self.range_combo.setItemText(1, f"KP {self._custom_range[0]:.3f} to {self._custom_range[1]:.3f}")
        else:
            self.range_combo.setCurrentIndex(0)
            self._custom_range = None
        self._schedule()

    # -- the report ----------------------------------------------------------------------
    def context(self) -> Dict[str, str]:
        """Plan and project values for the tokens."""
        project = QgsProject.instance()
        title = project.title() or os.path.splitext(os.path.basename(project.fileName() or ""))[0]
        plan = self.model.plan or {}
        default = self.model.target_default() if plan else None
        ranges = self.model.target_ranges() if plan else []
        target = "" if default is None else f"{default:.2f} m"
        if ranges:
            target = (target + " (varies by KP)").strip()
        return {"project": title, "plan": plan.get("name", ""), "rpl": plan.get("rpl_name", ""),
                "rpl_rev": plan.get("rpl_revision", ""), "target": target}

    def _schedule(self, *_args) -> None:
        if self.isVisible():
            self._timer.start()
        else:
            self._dirty = True

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        if self.assessment_tab.result is None and not self.controller.busy:
            self.assessment_tab.build_now()
        self._timer.start()

    def _rebuild(self) -> None:
        self._dirty = False
        result = self.assessment_tab.result
        problems = T.problems(self.template)
        if result is None:
            self.report = None
            self._show_message(self.assessment_tab.empty_message or "Assessing…")
            return
        if problems:
            self.report = None
            self._show_message(" ".join(problems))
            return
        try:
            data = G.report_data(result, self.context(), author=current_user())
            self.report = G.Report(data, self.template, self._custom_range)
        except ValueError as exc:
            self.report = None
            self._show_message(str(exc))
            return
        self.page = min(self.page, self.report.page_count - 1)
        self.message.setText("")
        self.message.setStyleSheet("")
        self._render_page()

    def _show_message(self, text: str) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(status_style("warn"))
        self.page_view.setPixmap(QPixmap())
        self.page_label.setText("")

    def _render_page(self) -> None:
        report = self.report
        if report is None:
            return
        width_px = max(self.scroll.viewport().width() - 12, 300)
        dpi = width_px / (report.width_mm / 25.4)
        try:
            image = G.render_image(report, self.page, dpi)
        except Exception:
            log_exception("Burial Planner: drawing the report page failed")
            return
        self.page_view.setPixmap(QPixmap.fromImage(image))
        self.page_view.resize(image.width(), image.height())
        kind, payload = report.pages[self.page]
        where = f"KP {payload[0]:.3f} to {payload[1]:.3f}" if kind == "graph" else kind
        self.page_label.setText(f"Page {self.page + 1} of {report.page_count} ({where})")
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page < report.page_count - 1)

    def _go(self, step: int) -> None:
        if self.report is None:
            return
        self.page = max(0, min(self.report.page_count - 1, self.page + step))
        self._render_page()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        if self.report is not None and self.isVisible():
            self._timer.start()

    # -- exports -------------------------------------------------------------------------
    def _default_name(self, extension: str) -> str:
        context = self.context()
        report = self.report
        span = f"_KP{report.extent[0]:.1f}-{report.extent[1]:.1f}" if report is not None else ""
        name = f"{_safe(context['plan'] or 'burial')}_burial_graph{span}.{extension}"
        folder = ""
        try:
            folder = str(QSettings().value(f"{_SETTINGS}/folder", "", type=str))
        except Exception:
            pass
        return os.path.join(folder, name) if folder else name

    def _remember_folder(self, path: str) -> None:
        if self.persist:
            QSettings().setValue(f"{_SETTINGS}/folder", os.path.dirname(path))

    def _export(self, title: str, extension: str, file_filter: str, writer) -> None:
        if self.report is None:
            QMessageBox.information(self, title, "There is nothing to export yet: build the assessment first.")
            return
        path, _filter = QFileDialog.getSaveFileName(self, title, self._default_name(extension), file_filter)
        if not path:
            return
        self._remember_folder(path)
        self.export_to(path, writer)

    def export_to(self, path: str, writer) -> bool:
        """Write the current report with ``writer(report, path, progress)`` in the background."""
        report = self.report
        if report is None:
            return False

        def work(cancelled, progress):
            return writer(report, path, lambda f: progress(f, "Writing pages"))

        def done(task):
            if task.error or task.cancelled:
                self.state.setText(task.error or "Export cancelled.")
                self.state.setStyleSheet(status_style("error"))
                return
            self.state.setStyleSheet(hint_style())
            self.state.setText(f"Wrote {os.path.basename(path)}.")
            self.last_export = task.result

        self.state.setText("Writing…")
        return self.controller.run("Exporting burial graph", work, done)

    def _export_pdf(self) -> None:
        self._export("Export burial graph PDF", "pdf", "PDF (*.pdf)",
                     lambda report, path, progress: G.export_pdf(report, path, progress))

    def _export_png(self) -> None:
        self._export("Export burial graph images", "png", "PNG (*.png)",
                     lambda report, path, progress: G.export_images(report, path, 200.0))

    def _export_svg(self) -> None:
        self._export("Export burial graph drawings", "svg", "SVG (*.svg)",
                     lambda report, path, progress: G.export_svgs(report, path))

    def _export_listing(self) -> None:
        result = self.assessment_tab.result
        if result is None:
            return
        steps = ["1 m", "5 m", "10 m", "20 m", "50 m", "100 m"]
        choice, ok = QInputDialog.getItem(self, "Listing", "One row every:", steps, 0, False)
        if not ok:
            return
        step = float(choice.split()[0])
        path, _filter = QFileDialog.getSaveFileName(self, "Export the listing",
                                                    self._default_name("csv").replace("_burial_graph",
                                                                                      f"_listing_{int(step)}m"),
                                                    "CSV (*.csv)")
        if not path:
            return
        channels = list(dict.fromkeys([R.WATER_DEPTH] + [k for s in result.series for k in s.channels
                                                         if k.startswith("tension:")]))
        header, rows = A.listing_rows(result, step, channels=channels)
        try:
            with open(path, "w", newline="", encoding="utf-8") as handle:
                out = csv.writer(handle)
                out.writerow(header)
                out.writerows(rows)
        except OSError as exc:
            QMessageBox.warning(self, "Export the listing", f"Could not write {path}: {exc.strerror or exc}")
            return
        self.state.setText(f"Wrote {os.path.basename(path)} ({len(rows):,} rows).")

    def shutdown(self) -> None:
        self._timer.stop()
