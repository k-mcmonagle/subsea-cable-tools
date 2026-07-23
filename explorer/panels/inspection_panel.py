# -*- coding: utf-8 -*-
"""Inspection panel: ad-hoc queries over the active dataset.

Provides two families of tools:

* **Value threshold checks** (repeatable) — flag records where a numeric field
  crosses a limit (e.g. bottom tension > 40 kN, seabed slack outside a band).
* **Off-line distance (DCC)** — flag records more than a given distance from a
  chosen reference route/RPL line layer.

Results are listed in a findings table; selecting a row highlights it on the
map, double-clicking focuses the map and every plot on it.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject, QgsPointXY, QgsCoordinateReferenceSystem

from ...qgis_compat import (
    EDIT_TRIGGER_DOUBLE_CLICKED,
    GEOMETRY_LINE,
    HEADER_RESIZE_MODE_STRETCH,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
)

_MAX_FINDINGS = 5000
_OPERATORS = (">", ">=", "<", "<=", "outside [a, b]", "inside [a, b]")


class _ThresholdRow:
    """A single repeatable value-threshold check row."""

    def __init__(self, panel: "InspectionPanel"):
        self.widget = QWidget()
        row = QHBoxLayout(self.widget)
        row.setContentsMargins(0, 0, 0, 0)

        self.field_combo = QComboBox()
        self.field_combo.setMinimumWidth(140)
        row.addWidget(self.field_combo, 2)

        self.op_combo = QComboBox()
        self.op_combo.addItems(list(_OPERATORS))
        self.op_combo.currentTextChanged.connect(self._sync_second)
        row.addWidget(self.op_combo, 1)

        self.value1 = QLineEdit()
        self.value1.setPlaceholderText("value")
        self.value1.setMaximumWidth(90)
        row.addWidget(self.value1)

        self.value2 = QLineEdit()
        self.value2.setPlaceholderText("b")
        self.value2.setMaximumWidth(70)
        row.addWidget(self.value2)

        remove = QPushButton("\u2715")
        remove.setMaximumWidth(28)
        remove.setToolTip("Remove this check")
        remove.clicked.connect(lambda: panel.remove_threshold_row(self))
        row.addWidget(remove)

        self._sync_second(self.op_combo.currentText())

    def _sync_second(self, op: str) -> None:
        self.value2.setVisible("[a, b]" in op)

    def populate_fields(self, fields: List[str]) -> None:
        current = self.field_combo.currentText()
        self.field_combo.clear()
        self.field_combo.addItems(fields)
        if current:
            idx = self.field_combo.findText(current)
            if idx >= 0:
                self.field_combo.setCurrentIndex(idx)

    def evaluate(self, dataset) -> Optional[tuple]:
        """Return (label, field, mask) or None if not runnable."""
        field = self.field_combo.currentText()
        if not field or field not in dataset.columns:
            return None
        try:
            a = float(self.value1.text())
        except (TypeError, ValueError):
            return None
        y = dataset.numeric(field)
        op = self.op_combo.currentText()
        finite = np.isfinite(y)
        if op == ">":
            mask = finite & (y > a)
            label = f"{field} > {a:g}"
        elif op == ">=":
            mask = finite & (y >= a)
            label = f"{field} >= {a:g}"
        elif op == "<":
            mask = finite & (y < a)
            label = f"{field} < {a:g}"
        elif op == "<=":
            mask = finite & (y <= a)
            label = f"{field} <= {a:g}"
        elif "outside" in op or "inside" in op:
            try:
                b = float(self.value2.text())
            except (TypeError, ValueError):
                return None
            lo, hi = min(a, b), max(a, b)
            if "outside" in op:
                mask = finite & ((y < lo) | (y > hi))
                label = f"{field} outside [{lo:g}, {hi:g}]"
            else:
                mask = finite & (y >= lo) & (y <= hi)
                label = f"{field} inside [{lo:g}, {hi:g}]"
        else:
            return None
        return label, field, mask


class InspectionPanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._dataset = None
        self._rows: List[_ThresholdRow] = []
        self._findings: List[dict] = []
        self._user_role = getattr(getattr(Qt, "ItemDataRole", Qt), "UserRole")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(getattr(getattr(Qt, "Orientation", Qt), "Vertical"))
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        # -- configuration (top) ------------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        config = QWidget()
        config_layout = QVBoxLayout(config)
        config_layout.setContentsMargins(2, 2, 2, 2)

        threshold_group = QGroupBox("Value threshold checks")
        tg_layout = QVBoxLayout(threshold_group)
        self._rows_container = QVBoxLayout()
        tg_layout.addLayout(self._rows_container)
        add_button = QPushButton("Add threshold check")
        add_button.clicked.connect(lambda: self.add_threshold_row())
        tg_layout.addWidget(add_button)
        config_layout.addWidget(threshold_group)

        self._dcc_group = QGroupBox("Off-line distance (DCC)")
        self._dcc_group.setCheckable(True)
        self._dcc_group.setChecked(False)
        dcc_form = QFormLayout(self._dcc_group)
        self.route_combo = QComboBox()
        dcc_form.addRow("Reference route:", self.route_combo)
        self.dcc_threshold = QLineEdit("50")
        dcc_form.addRow("Max off-line (m):", self.dcc_threshold)
        config_layout.addWidget(self._dcc_group)

        config_layout.addStretch(1)
        scroll.setWidget(config)
        splitter.addWidget(scroll)

        # -- results (bottom) ---------------------------------------------
        results = QWidget()
        results_layout = QVBoxLayout(results)
        results_layout.setContentsMargins(0, 0, 0, 0)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("Run inspection")
        self.run_button.clicked.connect(self.run_inspection)
        buttons.addWidget(self.run_button)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_results)
        buttons.addWidget(self.clear_button)
        results_layout.addLayout(buttons)

        self.status_label = QLabel("")
        results_layout.addWidget(self.status_label)

        self.findings_table = QTableWidget(0, 4)
        self.findings_table.setHorizontalHeaderLabels(["Check", "Message", "Time", "Value"])
        self.findings_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.findings_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.findings_table.setEditTriggers(EDIT_TRIGGER_DOUBLE_CLICKED)
        self.findings_table.setSortingEnabled(True)
        self.findings_table.horizontalHeader().setSectionResizeMode(1, HEADER_RESIZE_MODE_STRETCH)
        self.findings_table.itemSelectionChanged.connect(self._on_finding_selected)
        self.findings_table.cellDoubleClicked.connect(self._on_finding_double_clicked)
        results_layout.addWidget(self.findings_table, 1)

        splitter.addWidget(results)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        self.add_threshold_row()

    # -- threshold rows ----------------------------------------------------
    def add_threshold_row(self) -> _ThresholdRow:
        row = _ThresholdRow(self)
        self._rows.append(row)
        self._rows_container.addWidget(row.widget)
        if self._dataset is not None:
            row.populate_fields(self._numeric_fields())
        return row

    def remove_threshold_row(self, row: _ThresholdRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
            row.widget.setParent(None)
            row.widget.deleteLater()

    def _numeric_fields(self) -> List[str]:
        if self._dataset is None:
            return []
        return [n for n in self._dataset.field_names if self._dataset.is_numeric_field(n)]

    # -- data binding ------------------------------------------------------
    def set_dataset(self, dataset) -> None:
        self._dataset = dataset
        fields = self._numeric_fields()
        for row in self._rows:
            row.populate_fields(fields)
        self._populate_route_combo()
        self.clear_results()

    def _populate_route_combo(self) -> None:
        current = self.route_combo.currentData()
        self.route_combo.blockSignals(True)
        self.route_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if not hasattr(layer, "geometryType"):
                continue
            try:
                is_line = layer.geometryType() == GEOMETRY_LINE
            except Exception:
                is_line = False
            if is_line:
                self.route_combo.addItem(layer.name(), layer.id())
        self.route_combo.blockSignals(False)
        if current is not None:
            idx = self.route_combo.findData(current)
            if idx >= 0:
                self.route_combo.setCurrentIndex(idx)

    # -- run ---------------------------------------------------------------
    def run_inspection(self) -> None:
        if self._dataset is None:
            self.status_label.setText("Load a data layer first.")
            return
        findings: List[dict] = []
        for row in self._rows:
            result = row.evaluate(self._dataset)
            if result is None:
                continue
            label, field, mask = result
            y = self._dataset.numeric(field)
            for source_row in np.nonzero(mask)[0]:
                findings.append(self._make_finding(label, int(source_row), float(y[source_row])))
                if len(findings) >= _MAX_FINDINGS:
                    break

        if self._dcc_group.isChecked() and len(findings) < _MAX_FINDINGS:
            findings.extend(self._run_dcc(_MAX_FINDINGS - len(findings)))

        self._findings = findings
        self._populate_findings()
        self.status_label.setText(f"{len(findings)} finding(s).")

    def _run_dcc(self, budget: int) -> List[dict]:
        dataset = self._dataset
        if not dataset.has_geometry:
            return []
        layer_id = self.route_combo.currentData()
        route_layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if route_layer is None:
            return []
        try:
            threshold = abs(float(self.dcc_threshold.text()))
        except (TypeError, ValueError):
            return []
        try:
            from ...kp_geo_utils import RouteFrame
            from ...kp_range_utils import make_distance_area

            crs4326 = QgsCoordinateReferenceSystem("EPSG:4326")
            distance = make_distance_area(crs4326, self.controller.transform_context())
            route = RouteFrame.from_source(
                route_layer, distance, target_crs=crs4326, project=QgsProject.instance()
            )
        except Exception as exc:  # pragma: no cover - geometry/CRS failure
            self.status_label.setText(f"DCC route error: {exc}")
            return []

        out: List[dict] = []
        lon = dataset.lon
        lat = dataset.lat
        for source_row in range(dataset.row_count):
            x = lon[source_row]
            y = lat[source_row]
            if not (np.isfinite(x) and np.isfinite(y)):
                continue
            try:
                hit = route.kp_at_point(QgsPointXY(float(x), float(y)))
            except Exception:
                continue
            dcc = abs(hit.dcc_m)
            if dcc > threshold:
                out.append(self._make_finding(f"off-line > {threshold:g} m", source_row, dcc))
                if len(out) >= budget:
                    break
        return out

    def _make_finding(self, label: str, source_row: int, value: float) -> dict:
        dataset = self._dataset
        lat = lon = None
        if dataset.has_geometry:
            lat = dataset.lat[source_row]
            lon = dataset.lon[source_row]
        return {
            "check": label,
            "message": f"row {source_row}",
            "time": dataset.iso_time_at(source_row) or "",
            "value": value,
            "source_row": source_row,
            "lat": lat,
            "lon": lon,
        }

    def _populate_findings(self) -> None:
        self.findings_table.setSortingEnabled(False)
        self.findings_table.setRowCount(0)
        for idx, finding in enumerate(self._findings):
            row = self.findings_table.rowCount()
            self.findings_table.insertRow(row)
            value = finding.get("value")
            cells = [
                finding.get("check", ""),
                finding.get("message", ""),
                finding.get("time", ""),
                "" if value is None else f"{value:g}",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if col == 0:
                    item.setData(self._user_role, idx)
                self.findings_table.setItem(row, col, item)
        self.findings_table.setSortingEnabled(True)

    def clear_results(self) -> None:
        self._findings = []
        self.findings_table.setRowCount(0)
        self.status_label.setText("")

    # -- navigation --------------------------------------------------------
    def _on_finding_selected(self) -> None:
        rows = self.findings_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        item = self.findings_table.item(row, 0)
        if item is None:
            return
        index = item.data(self._user_role)
        if index is None:
            return
        index = int(index)
        if 0 <= index < len(self._findings):
            self.controller.highlight_record(self._findings[index]["source_row"])

    def _on_finding_double_clicked(self, row: int, _col: int) -> None:
        item = self.findings_table.item(row, 0)
        if item is None:
            return
        index = item.data(self._user_role)
        if index is None:
            return
        index = int(index)
        if 0 <= index < len(self._findings):
            self.controller.go_to_record(self._findings[index]["source_row"])
