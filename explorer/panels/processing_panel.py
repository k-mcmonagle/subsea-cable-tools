# -*- coding: utf-8 -*-
"""Processing panel: generate as-laid listings and as-laid RPLs.

* **Listing** — points every 1 / 5 / 10 / 50 m (or a custom interval) along the
  as-laid path, with KP and sampled numeric attributes. Previewable, then
  addable as a project layer or exportable to CSV.
* **As-laid RPL** — a Douglas-Peucker fit of the as-laid path emitted in the
  same Points + Lines schema as imported RPLs, added to the project.

All heavy lifting lives in :mod:`explorer.processing_ops`; this panel is just
the UI around it.
"""

from __future__ import annotations

from typing import List, Optional

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject, QgsCoordinateReferenceSystem

from ...qgis_compat import (
    EDIT_TRIGGER_DOUBLE_CLICKED,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
)
from .. import processing_ops as ops

_INTERVAL_CHOICES = (("1 m", 1.0), ("5 m", 5.0), ("10 m", 10.0), ("50 m", 50.0), ("Custom\u2026", None))


class ProcessingPanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._dataset = None
        self._listing: List[dict] = []
        self._listing_fields: List[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # -- listing generator --------------------------------------------
        listing_group = QGroupBox("As-laid listing")
        listing_form = QFormLayout(listing_group)
        interval_row = QHBoxLayout()
        self.interval_combo = QComboBox()
        for label, value in _INTERVAL_CHOICES:
            self.interval_combo.addItem(label, value)
        self.interval_combo.currentIndexChanged.connect(self._sync_custom)
        interval_row.addWidget(self.interval_combo)
        self.custom_interval = QLineEdit("25")
        self.custom_interval.setMaximumWidth(80)
        self.custom_interval.setVisible(False)
        interval_row.addWidget(self.custom_interval)
        interval_row.addWidget(QLabel("m"))
        interval_row.addStretch(1)
        listing_form.addRow("Interval:", self._wrap(interval_row))

        listing_buttons = QHBoxLayout()
        self.generate_listing_button = QPushButton("Generate listing")
        self.generate_listing_button.clicked.connect(self.generate_listing)
        listing_buttons.addWidget(self.generate_listing_button)
        self.add_listing_button = QPushButton("Add as layer")
        self.add_listing_button.clicked.connect(self.add_listing_layer)
        self.add_listing_button.setEnabled(False)
        listing_buttons.addWidget(self.add_listing_button)
        self.export_listing_button = QPushButton("Export CSV\u2026")
        self.export_listing_button.clicked.connect(self.export_listing_csv)
        self.export_listing_button.setEnabled(False)
        listing_buttons.addWidget(self.export_listing_button)
        listing_form.addRow(self._wrap(listing_buttons))
        layout.addWidget(listing_group)

        self.listing_table = QTableWidget(0, 0)
        self.listing_table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.listing_table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.listing_table.setEditTriggers(EDIT_TRIGGER_DOUBLE_CLICKED)
        layout.addWidget(self.listing_table, 1)

        # -- as-laid RPL generator ----------------------------------------
        rpl_group = QGroupBox("As-laid RPL (best fit)")
        rpl_form = QFormLayout(rpl_group)
        self.tolerance = QLineEdit("25")
        self.tolerance.setMaximumWidth(90)
        rpl_form.addRow("Fit tolerance (m):", self.tolerance)
        rpl_buttons = QHBoxLayout()
        self.generate_rpl_button = QPushButton("Generate as-laid RPL")
        self.generate_rpl_button.clicked.connect(self.generate_rpl)
        rpl_buttons.addWidget(self.generate_rpl_button)
        rpl_form.addRow(self._wrap(rpl_buttons))
        layout.addWidget(rpl_group)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    @staticmethod
    def _wrap(inner_layout) -> QWidget:
        widget = QWidget()
        widget.setLayout(inner_layout)
        return widget

    def _sync_custom(self) -> None:
        self.custom_interval.setVisible(self.interval_combo.currentData() is None)

    # -- data binding ------------------------------------------------------
    def set_dataset(self, dataset) -> None:
        self._dataset = dataset
        self._listing = []
        self._listing_fields = []
        self.listing_table.clear()
        self.listing_table.setRowCount(0)
        self.listing_table.setColumnCount(0)
        self.add_listing_button.setEnabled(False)
        self.export_listing_button.setEnabled(False)
        self.status_label.setText("")

    def _interval_m(self) -> Optional[float]:
        value = self.interval_combo.currentData()
        if value is not None:
            return float(value)
        try:
            return float(self.custom_interval.text())
        except (TypeError, ValueError):
            return None

    def _distance_area(self):
        crs4326 = QgsCoordinateReferenceSystem("EPSG:4326")
        from ...kp_range_utils import make_distance_area

        return make_distance_area(crs4326, self.controller.transform_context())

    def _numeric_fields(self) -> List[str]:
        if self._dataset is None:
            return []
        skip = {"Lat_dd", "Lon_dd"}
        return [
            n for n in self._dataset.field_names
            if self._dataset.is_numeric_field(n) and n not in skip
        ]

    # -- listing -----------------------------------------------------------
    def generate_listing(self) -> None:
        if self._dataset is None:
            self.status_label.setText("Load a data layer first.")
            return
        interval = self._interval_m()
        if interval is None or interval <= 0:
            self.status_label.setText("Enter a valid interval.")
            return
        fields = self._numeric_fields()
        try:
            distance = self._distance_area()
            self._listing = ops.interval_listing(self._dataset, interval, distance, fields)
        except Exception as exc:  # pragma: no cover - geometry failure
            self.status_label.setText(f"Listing error: {exc}")
            return
        self._listing_fields = fields
        self._populate_listing_table()
        has_rows = bool(self._listing)
        self.add_listing_button.setEnabled(has_rows)
        self.export_listing_button.setEnabled(has_rows)
        self.status_label.setText(f"{len(self._listing)} station(s) at {interval:g} m.")

    def _populate_listing_table(self) -> None:
        columns = ["PosNo", "KP_km", "Lat_dd", "Lon_dd"] + self._listing_fields
        self.listing_table.clear()
        self.listing_table.setColumnCount(len(columns))
        self.listing_table.setHorizontalHeaderLabels(columns)
        self.listing_table.setRowCount(len(self._listing))
        for r, station in enumerate(self._listing):
            for c, name in enumerate(columns):
                value = station.get(name)
                if isinstance(value, float):
                    text = f"{value:.6g}"
                elif value is None:
                    text = ""
                else:
                    text = str(value)
                self.listing_table.setItem(r, c, QTableWidgetItem(text))

    def add_listing_layer(self) -> None:
        if not self._listing:
            return
        specs = ops.listing_specs(self._listing_fields)
        name = f"As-laid listing ({self.controller.layer_name() or 'data'})"
        try:
            layer = ops.build_memory_layer(name, "Point", specs, self._listing)
            QgsProject.instance().addMapLayer(layer)
        except Exception as exc:  # pragma: no cover
            QMessageBox.critical(self, "Add listing", f"Could not build layer:\n{exc}")
            return
        self.status_label.setText(f"Added '{name}' to the project.")

    def export_listing_csv(self) -> None:
        if not self._listing:
            return
        path, _flt = QFileDialog.getSaveFileName(self, "Export listing", "", "CSV files (*.csv)")
        if not path:
            return
        columns = ["PosNo", "KP_km", "Lat_dd", "Lon_dd"] + self._listing_fields
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(",".join(columns) + "\n")
                for station in self._listing:
                    cells = []
                    for name in columns:
                        value = station.get(name)
                        cells.append("" if value is None else str(value))
                    handle.write(",".join(cells) + "\n")
        except OSError as exc:
            QMessageBox.critical(self, "Export listing", f"Could not write file:\n{exc}")
            return
        self.status_label.setText(f"Exported {len(self._listing)} station(s).")

    # -- as-laid RPL -------------------------------------------------------
    def generate_rpl(self) -> None:
        if self._dataset is None:
            self.status_label.setText("Load a data layer first.")
            return
        try:
            tolerance = float(self.tolerance.text())
        except (TypeError, ValueError):
            self.status_label.setText("Enter a valid fit tolerance.")
            return
        source_name = f"as-laid {self.controller.layer_name() or ''}".strip()
        try:
            distance = self._distance_area()
            point_rows, line_rows = ops.build_aslaid_rpl(
                self._dataset, tolerance, distance, source_name
            )
        except Exception as exc:  # pragma: no cover
            self.status_label.setText(f"RPL error: {exc}")
            return
        if not point_rows:
            self.status_label.setText("No geometry available to build an RPL.")
            return
        try:
            base = self.controller.layer_name() or "data"
            points_layer = ops.build_memory_layer(
                f"As-laid RPL points ({base})", "Point", ops.POINT_RPL_SPECS, point_rows
            )
            lines_layer = ops.build_memory_layer(
                f"As-laid RPL lines ({base})", "LineString", ops.LINE_RPL_SPECS, line_rows
            )
            QgsProject.instance().addMapLayers([lines_layer, points_layer])
        except Exception as exc:  # pragma: no cover
            QMessageBox.critical(self, "Generate RPL", f"Could not build RPL layers:\n{exc}")
            return
        self.status_label.setText(
            f"Added as-laid RPL: {len(point_rows)} positions, {len(line_rows)} segments."
        )
