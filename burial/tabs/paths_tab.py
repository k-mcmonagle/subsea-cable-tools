# -*- coding: utf-8 -*-
"""Installation Paths tab: tool path, layback profile and barge track."""

from __future__ import annotations

import json
import math
from typing import Dict, Optional

from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtWidgets import (
    QCheckBox,
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
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    DIALOG_ACCEPTED,
    HEADER_RESIZE_MODE_STRETCH,
    ITEM_DATA_USER_ROLE,
    MESSAGE_BOX_NO,
    MESSAGE_BOX_YES,
    SELECTION_BEHAVIOR_SELECT_ROWS,
    SELECTION_MODE_SINGLE,
    qt_exec,
)
from .. import footprint as footprint_mod
from .. import path_data, path_layers, schema
from .. import tools as tools_mod
from ..analysis_task import DepthSnapshot
from ..path_task import InstallationPathTask, build_path_work
from .. import ui_helpers


class LaybackProfileDialog(QDialog):
    """Edit a reusable horizontal-layback versus water-depth profile."""

    def __init__(self, row: Optional[Dict] = None, parent=None):
        super().__init__(parent)
        self.row = dict(row or {})
        self.setWindowTitle("Layback profile")
        self.setMinimumSize(540, 420)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(self.row.get("name") or "")
        self.name_edit.setPlaceholderText("e.g. Plough tow profile - normal lay")
        form.addRow("Name:", self.name_edit)
        self.source_edit = QLineEdit(self.row.get("source_ref") or "")
        self.source_edit.setPlaceholderText("document / analysis / revision")
        form.addRow("Source reference:", self.source_edit)
        self.outside_combo = QComboBox()
        self.outside_combo.addItem("Stop outside the entered depth range", "error")
        self.outside_combo.addItem("Hold the nearest end value", "hold")
        self.outside_combo.setCurrentIndex(max(
            0, self.outside_combo.findData(
                self.row.get("outside_mode") or "error")))
        self.outside_combo.setToolTip(
            "Controls a depth shallower or deeper than this profile. "
            "Stopping is safer; holding an end value must be justified by "
            "the source reference.")
        form.addRow("Outside profile range:", self.outside_combo)
        layout.addLayout(form)

        note = QLabel(
            "Enter horizontal layback at water depth. One row defines a "
            "constant layback (its depth is ignored); two or more rows are "
            "linearly interpolated. Values are user-defined engineering "
            "inputs, not defaults supplied by this plugin.")
        note.setWordWrap(True)
        note.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(note)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Water depth (m)",
                                               "Horizontal layback (m)"])
        self.table.horizontalHeader().setSectionResizeMode(
            HEADER_RESIZE_MODE_STRETCH)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        layout.addWidget(self.table, 1)
        row_buttons = QHBoxLayout()
        add = QPushButton("Add row")
        remove = QPushButton("Remove row")
        add.clicked.connect(self._add_row)
        remove.clicked.connect(self._remove_row)
        row_buttons.addWidget(add)
        row_buttons.addWidget(remove)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)

        self.notes_edit = QPlainTextEdit(self.row.get("notes") or "")
        self.notes_edit.setMaximumHeight(75)
        form2 = QFormLayout()
        form2.addRow("Notes:", self.notes_edit)
        layout.addLayout(form2)

        for depth, layback in path_data.layback_points(self.row):
            self._add_row(depth, layback)
        if self.table.rowCount() == 0:
            self._add_row(0.0, 0.0)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_row(self, depth=0.0, layback=0.0) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(f"{float(depth):g}"))
        self.table.setItem(row, 1, QTableWidgetItem(f"{float(layback):g}"))

    def _remove_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0 and self.table.rowCount() > 1:
            self.table.removeRow(row)

    def _accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Layback profile",
                                "Enter a profile name.")
            return
        points = []
        for row in range(self.table.rowCount()):
            try:
                depth = float(self.table.item(row, 0).text())
                layback = float(self.table.item(row, 1).text())
            except (AttributeError, TypeError, ValueError):
                QMessageBox.warning(
                    self, "Layback profile",
                    f"Row {row + 1} needs numeric depth and layback values.")
                return
            if not math.isfinite(depth) or not math.isfinite(layback) \
                    or depth < 0.0 or layback < 0.0:
                QMessageBox.warning(
                    self, "Layback profile",
                    "Depth and horizontal layback must be finite and cannot "
                    "be negative.")
                return
            points.append((depth, layback))
        if len({point[0] for point in points}) != len(points):
            QMessageBox.warning(self, "Layback profile",
                                "Water-depth values must be unique.")
            return
        self.accept()

    def payload(self) -> Dict:
        points = []
        for row in range(self.table.rowCount()):
            points.append({
                "depth_m": float(self.table.item(row, 0).text()),
                "layback_m": float(self.table.item(row, 1).text()),
            })
        return {
            "layback_id": self.row.get("layback_id") or schema.new_id(),
            "name": self.name_edit.text().strip(),
            "points_json": json.dumps(points, separators=(",", ":")),
            "outside_mode": self.outside_combo.currentData() or "error",
            "source_ref": self.source_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "created_utc": (self.row.get("created_utc")
                            or schema.utc_now_iso()),
        }


class RadiusRulesDialog(QDialog):
    """Edit water-depth-banded minimum turning radius rules."""

    def __init__(self, rules=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Depth-based turning radius")
        self.setMinimumSize(480, 360)
        layout = QVBoxLayout(self)
        note = QLabel(
            "Each row is a band: course changes in water up to the entered "
            "depth use that minimum turning radius (e.g. up to 100 m → "
            "950 m, up to 1000 m → 1150 m). The tool configuration's own "
            "radius remains a hard floor — the larger value always applies. "
            "Depth deeper than the last band stops the generation; leave "
            "the table empty to use the constant tool radius everywhere.")
        note.setWordWrap(True)
        note.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(note)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(
            ["Water depth up to (m)", "Minimum turn radius (m)"])
        self.table.horizontalHeader().setSectionResizeMode(
            HEADER_RESIZE_MODE_STRETCH)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        layout.addWidget(self.table, 1)
        row_buttons = QHBoxLayout()
        add = QPushButton("Add band")
        remove = QPushButton("Remove band")
        add.clicked.connect(lambda: self._add_row())
        remove.clicked.connect(self._remove_row)
        row_buttons.addWidget(add)
        row_buttons.addWidget(remove)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)
        for rule in path_data.sanitise_radius_rules(rules or []):
            self._add_row(rule["max_depth_m"], rule["radius_m"])
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_row(self, depth=100.0, radius=500.0) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(f"{float(depth):g}"))
        self.table.setItem(row, 1, QTableWidgetItem(f"{float(radius):g}"))

    def _remove_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _rows(self):
        out = []
        for row in range(self.table.rowCount()):
            depth_item = self.table.item(row, 0)
            radius_item = self.table.item(row, 1)
            out.append((row + 1,
                        depth_item.text() if depth_item else "",
                        radius_item.text() if radius_item else ""))
        return out

    def _accept(self) -> None:
        depths = set()
        for number, depth_text, radius_text in self._rows():
            try:
                depth, radius = float(depth_text), float(radius_text)
            except (TypeError, ValueError):
                QMessageBox.warning(
                    self, "Depth-based turning radius",
                    f"Band {number} needs numeric depth and radius values.")
                return
            if not math.isfinite(depth) or not math.isfinite(radius) \
                    or depth <= 0.0 or radius <= 0.0:
                QMessageBox.warning(
                    self, "Depth-based turning radius",
                    "Depth and radius must be finite and greater than zero.")
                return
            if depth in depths:
                QMessageBox.warning(self, "Depth-based turning radius",
                                    "Band depths must be unique.")
                return
            depths.add(depth)
        self.accept()

    def rules(self):
        out = []
        for _number, depth_text, radius_text in self._rows():
            try:
                out.append({"max_depth_m": float(depth_text),
                            "radius_m": float(radius_text)})
            except (TypeError, ValueError):
                continue
        return path_data.sanitise_radius_rules(out)


class VesselDialog(QDialog):
    """Edit a project-scoped installation vessel (turn radius + outline)."""

    def __init__(self, row: Optional[Dict] = None, parent=None):
        super().__init__(parent)
        self.row = dict(row or {})
        self.setWindowTitle("Installation vessel")
        self.setMinimumSize(540, 400)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(self.row.get("name") or "")
        self.name_edit.setPlaceholderText("e.g. CLV Example — plough tow")
        form.addRow("Name:", self.name_edit)
        self.radius_spin = QDoubleSpinBox()
        self.radius_spin.setRange(0.0, 1000000.0)
        self.radius_spin.setDecimals(1)
        self.radius_spin.setSuffix(" m")
        self.radius_spin.setSpecialValueText("Not entered")
        try:
            self.radius_spin.setValue(
                float(self.row.get("min_turn_radius_m") or 0.0))
        except (TypeError, ValueError):
            pass
        self.radius_spin.setToolTip(
            "The vessel's minimum turning radius while towing. The barge "
            "track is checked against it after generation — it never "
            "constrains the tool-path geometry itself.")
        form.addRow("Minimum turning radius:", self.radius_spin)
        self.source_edit = QLineEdit(self.row.get("source_ref") or "")
        self.source_edit.setPlaceholderText("document / trials / master input")
        form.addRow("Source reference:", self.source_edit)
        layout.addLayout(form)

        outline_box = QGroupBox("Outline (optional, for map display)")
        outline_form = QFormLayout(outline_box)
        dxf_row = QHBoxLayout()
        import_button = QPushButton("Import DXF…")
        import_button.clicked.connect(self._import_dxf)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._clear_outline)
        dxf_row.addWidget(import_button)
        dxf_row.addWidget(clear_button)
        dxf_row.addStretch(1)
        outline_form.addRow(dxf_row)
        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.000001, 1000000.0)
        self.scale_spin.setDecimals(6)
        self.scale_spin.setValue(
            float(self.row.get("footprint_scale") or 1.0))
        self.scale_spin.setToolTip(
            "Drawing units to metres (e.g. 0.001 for a millimetre GA).")
        outline_form.addRow("DXF scale to metres:", self.scale_spin)
        self.rotation_spin = QDoubleSpinBox()
        self.rotation_spin.setRange(-360.0, 360.0)
        self.rotation_spin.setDecimals(1)
        self.rotation_spin.setSuffix(" °")
        self.rotation_spin.setValue(
            float(self.row.get("footprint_rotation_deg") or 0.0))
        self.rotation_spin.setToolTip(
            "Rotation so the bow points along +Y in the drawing frame.")
        outline_form.addRow("Rotation:", self.rotation_spin)
        crp_row = QHBoxLayout()
        self.crp_x_spin = QDoubleSpinBox()
        self.crp_y_spin = QDoubleSpinBox()
        for spin, value in ((self.crp_x_spin,
                             self.row.get("footprint_crp_x")),
                            (self.crp_y_spin,
                             self.row.get("footprint_crp_y"))):
            spin.setRange(-10000000.0, 10000000.0)
            spin.setDecimals(3)
            spin.setValue(float(value or 0.0))
        crp_row.addWidget(QLabel("X:"))
        crp_row.addWidget(self.crp_x_spin)
        crp_row.addWidget(QLabel("Y:"))
        crp_row.addWidget(self.crp_y_spin)
        crp_row.addStretch(1)
        outline_form.addRow("CRP in drawing units:", crp_row)
        self.outline_label = QLabel("")
        self.outline_label.setWordWrap(True)
        outline_form.addRow(self.outline_label)
        layout.addWidget(outline_box)

        self.notes_edit = QPlainTextEdit(self.row.get("notes") or "")
        self.notes_edit.setMaximumHeight(60)
        notes_form = QFormLayout()
        notes_form.addRow("Notes:", self.notes_edit)
        layout.addLayout(notes_form)

        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_outline_label()

    def _refresh_outline_label(self) -> None:
        if self.row.get("footprint_wkt"):
            source = self.row.get("footprint_source") or "imported outline"
            try:
                dims = (f" ({float(self.row.get('length_m')):g} m × "
                        f"{float(self.row.get('width_m')):g} m)")
            except (TypeError, ValueError):
                dims = ""
            self.outline_label.setText(f"Outline: {source}{dims}")
        else:
            self.outline_label.setText(
                "No outline imported. The vessel track still generates; "
                "only the map outline overlay needs one.")

    def _import_dxf(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Vessel outline DXF", "", "DXF drawings (*.dxf)")
        if not path:
            return
        try:
            wkt, info = footprint_mod.load_dxf_outline(
                path, self.scale_spin.value(),
                self.crp_x_spin.value(), self.crp_y_spin.value(),
                self.rotation_spin.value())
        except footprint_mod.FootprintError as exc:
            QMessageBox.warning(self, "Installation vessel", str(exc))
            return
        self.row.update({
            "footprint_wkt": wkt,
            "footprint_source": info.get("source") or "",
            "footprint_scale": info.get("scale"),
            "footprint_crp_x": info.get("crp_x"),
            "footprint_crp_y": info.get("crp_y"),
            "footprint_rotation_deg": info.get("rotation_deg"),
            "length_m": info.get("length_m"),
            "width_m": info.get("width_m"),
        })
        self._refresh_outline_label()

    def _clear_outline(self) -> None:
        for key in ("footprint_wkt", "footprint_source", "length_m",
                    "width_m"):
            self.row[key] = ""
        self._refresh_outline_label()

    def _accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Installation vessel",
                                "Enter a vessel name.")
            return
        self.accept()

    def payload(self) -> Dict:
        return {
            "vessel_id": self.row.get("vessel_id") or schema.new_id(),
            "name": self.name_edit.text().strip(),
            "min_turn_radius_m": self.radius_spin.value(),
            "footprint_wkt": self.row.get("footprint_wkt") or "",
            "footprint_source": self.row.get("footprint_source") or "",
            "footprint_scale": self.scale_spin.value(),
            "footprint_crp_x": self.crp_x_spin.value(),
            "footprint_crp_y": self.crp_y_spin.value(),
            "footprint_rotation_deg": self.rotation_spin.value(),
            "length_m": self.row.get("length_m") or None,
            "width_m": self.row.get("width_m") or None,
            "source_ref": self.source_edit.text().strip(),
            "notes": self.notes_edit.toPlainText().strip(),
            "created_utc": (self.row.get("created_utc")
                            or schema.utc_now_iso()),
        }


class PathsTab(QWidget):
    """Create, inspect and display installation path planning geometry."""

    def __init__(self, model, dock=None, parent=None):
        super().__init__(parent)
        self.model = model
        self.dock = dock
        self._loading = False
        self._task: Optional[InstallationPathTask] = None
        self._generation = 0
        self._radius_rules = []  # staged bands; persisted on Apply/Generate

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Generate a realistic burial-tool path from every course change "
            "in the RPL geometry, using the plan default tool configuration. "
            "The default uses tangent circular fillets; the pass-through "
            "mode finds forward-only turn-out/turn-in paths that intersect "
            "every course-change vertex. All burial analysis, sections and "
            "KP references continue to use the RPL — the installation path "
            "is a derived operational-geometry product for review, display "
            "and reporting.")
        intro.setWordWrap(True)
        intro.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(intro)

        settings = QGroupBox("Tool path settings")
        form = QFormLayout(settings)
        self.tool_label = QLabel("—")
        self.radius_label = QLabel("—")
        form.addRow("Plan default tool:", self.tool_label)
        form.addRow("Minimum turning radius:", self.radius_label)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Fillet corners (default)", path_data.MODE_FILLET)
        self.mode_combo.addItem("Pass through every course change",
                                path_data.MODE_THROUGH)
        self.mode_combo.setToolTip(
            "Fillet uses tangent radius arcs and may miss the original "
            "vertex. Pass-through optimises a shared heading at every route "
            "vertex, allowing turn-out then turn-in while retaining the "
            "minimum radius. Pass-through solves every course change as a "
            "compound cluster and can take several minutes on a route with "
            "many course changes; generation runs in the background and can "
            "be stopped at any time.")
        form.addRow("Path objective:", self.mode_combo)
        radius_rules_row = QHBoxLayout()
        self.radius_rules_label = QLabel("Constant tool radius")
        radius_rules_row.addWidget(self.radius_rules_label, 1)
        self.radius_rules_button = QPushButton("Edit…")
        self.radius_rules_button.clicked.connect(self._edit_radius_rules)
        radius_rules_row.addWidget(self.radius_rules_button)
        self.radius_rules_label.setToolTip(
            "Optional water-depth bands overriding the minimum turning "
            "radius (e.g. 950 m up to 100 m depth, 1150 m up to 1000 m). "
            "Each course change samples the registered bathymetry; the tool "
            "configuration's radius remains a hard floor.")
        form.addRow("Depth-based radius:", radius_rules_row)
        self.max_deviation_spin = QDoubleSpinBox()
        self.max_deviation_spin.setRange(0.0, 1000000.0)
        self.max_deviation_spin.setDecimals(1)
        self.max_deviation_spin.setSuffix(" m")
        self.max_deviation_spin.setSpecialValueText("Report only")
        self.max_deviation_spin.setToolTip(
            "Optional maximum distance from the RPL. Report only records "
            "the calculated excursion; a positive value rejects compound "
            "solutions or fillets outside that corridor.")
        form.addRow("Maximum route deviation:", self.max_deviation_spin)
        self.apply_button = QPushButton("Apply settings")
        self.apply_button.clicked.connect(self._apply_settings)
        form.addRow("", self.apply_button)
        layout.addWidget(settings)

        barge = QGroupBox("Barge track (plough plans)")
        barge_form = QFormLayout(barge)
        self.generate_barge_check = QCheckBox(
            "Generate the tow-point / vessel track with the tool path")
        self.generate_barge_check.toggled.connect(self._barge_toggled)
        barge_form.addRow(self.generate_barge_check)
        profile_row = QHBoxLayout()
        self.layback_combo = QComboBox()
        self.layback_combo.setMinimumWidth(180)
        self.layback_combo.currentIndexChanged.connect(
            lambda _index: self._profile_selection_changed())
        profile_row.addWidget(self.layback_combo, 1)
        add_profile = QPushButton("New…")
        self.edit_profile_button = QPushButton("Edit…")
        self.delete_profile_button = QPushButton("Delete")
        add_profile.clicked.connect(self._new_profile)
        self.edit_profile_button.clicked.connect(self._edit_profile)
        self.delete_profile_button.clicked.connect(self._delete_profile)
        profile_row.addWidget(add_profile)
        profile_row.addWidget(self.edit_profile_button)
        profile_row.addWidget(self.delete_profile_button)
        barge_form.addRow("Layback profile:", profile_row)
        vessel_row = QHBoxLayout()
        self.vessel_combo = QComboBox()
        self.vessel_combo.setMinimumWidth(180)
        self.vessel_combo.setToolTip(
            "Optional vessel: its minimum turning radius is checked against "
            "the generated track's tightest turn, and its imported outline "
            "is drawn at the tow point by the Tool outline map overlay.")
        self.vessel_combo.currentIndexChanged.connect(
            lambda _index: self._vessel_selection_changed())
        vessel_row.addWidget(self.vessel_combo, 1)
        add_vessel = QPushButton("New…")
        self.edit_vessel_button = QPushButton("Edit…")
        self.delete_vessel_button = QPushButton("Delete")
        add_vessel.clicked.connect(self._new_vessel)
        self.edit_vessel_button.clicked.connect(self._edit_vessel)
        self.delete_vessel_button.clicked.connect(self._delete_vessel)
        vessel_row.addWidget(add_vessel)
        vessel_row.addWidget(self.edit_vessel_button)
        vessel_row.addWidget(self.delete_vessel_button)
        barge_form.addRow("Vessel:", vessel_row)
        barge_note = QLabel(
            "The track is B(s) = tool(s) + horizontal layback(s) × forward "
            "tangent(s). A multi-point layback profile samples the registered "
            "bathymetry by water depth. No vessel dynamics, current, catenary "
            "or touchdown analysis is implied.")
        barge_note.setWordWrap(True)
        barge_note.setStyleSheet(ui_helpers.hint_style())
        barge_form.addRow(barge_note)
        layout.addWidget(barge)

        actions = QHBoxLayout()
        self.generate_button = QPushButton("Generate installation paths")
        self.stop_button = QPushButton("Stop")
        self.clear_button = QPushButton("Clear result")
        self.generate_button.clicked.connect(self._generate)
        self.stop_button.clicked.connect(self._stop)
        self.clear_button.clicked.connect(self._clear)
        self.stop_button.setVisible(False)
        actions.addWidget(self.generate_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.clear_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status_label = QLabel("No path generated.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        display = QHBoxLayout()
        display.addWidget(QLabel("Map display:"))
        self.show_tool = QCheckBox("Tool path")
        self.show_barge = QCheckBox("Barge track")
        self.show_issues = QCheckBox("Course-change points")
        self.show_tool.setChecked(True)
        self.show_barge.setChecked(True)
        self.show_issues.setChecked(True)
        self.show_tool.toggled.connect(
            lambda checked: self._set_visibility("tool", checked))
        self.show_barge.toggled.connect(
            lambda checked: self._set_visibility("barge", checked))
        self.show_issues.toggled.connect(
            lambda checked: self._set_visibility("issues", checked))
        display.addWidget(self.show_tool)
        display.addWidget(self.show_barge)
        display.addWidget(self.show_issues)
        display.addStretch(1)
        layout.addLayout(display)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels([
            "KP", "Course change (°)", "Side", "Solution", "Radius (m)",
            "Vertex miss (m)", "Cluster offset (m)", "Depth Δ (m)",
            "Status"])
        self.table.horizontalHeader().setSectionResizeMode(
            HEADER_RESIZE_MODE_STRETCH)
        self.table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        self.table.setSelectionMode(SELECTION_MODE_SINGLE)
        self.table.setToolTip(
            "One diagnostic for every non-collinear geometry vertex in the "
            "scoped RPL, independent of any A/C label or event threshold.")
        layout.addWidget(self.table, 1)

        model.planChanged.connect(self.refresh)
        model.toolsChanged.connect(self.refresh)
        model.inputsChanged.connect(self.refresh)
        model.pathsChanged.connect(self.refresh)
        model.laybacksChanged.connect(self.refresh)
        model.vesselsChanged.connect(self.refresh)
        self.refresh()

    def _profile_id(self) -> str:
        return str(self.layback_combo.currentData() or "")

    def _vessel_id(self) -> str:
        return str(self.vessel_combo.currentData() or "")

    def _current_config(self) -> Dict:
        return {
            "mode": self.mode_combo.currentData() or path_data.MODE_FILLET,
            "max_deviation_m": self.max_deviation_spin.value(),
            "layback_id": self._profile_id(),
            "generate_barge": self.generate_barge_check.isChecked(),
            "vessel_id": self._vessel_id(),
            "radius_rules": path_data.sanitise_radius_rules(
                self._radius_rules),
        }

    def _apply_settings(self) -> bool:
        if self._loading or not self.model.plan:
            return False
        config = self._current_config()
        if config["generate_barge"] and not config["layback_id"]:
            QMessageBox.warning(self, "Installation Paths",
                                "Select or create a layback profile.")
            return False
        if config == self.model.path_config():
            return True
        saved = self.model.update_gen_params(
            {"installation_paths": config},
            reason="Updated installation path settings", stale=False)
        return saved

    def _barge_toggled(self, checked: bool) -> None:
        self.layback_combo.setEnabled(bool(checked))

    def _profile_selection_changed(self) -> None:
        selected = bool(self._profile_id())
        self.edit_profile_button.setEnabled(selected)
        self.delete_profile_button.setEnabled(selected)

    def _new_profile(self) -> None:
        dialog = LaybackProfileDialog(parent=self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            layback_id = self.model.save_layback_profile(dialog.payload())
            if layback_id:
                self.refresh()
                self.layback_combo.setCurrentIndex(max(
                    0, self.layback_combo.findData(layback_id)))

    def _edit_profile(self) -> None:
        row = self.model.layback_profile(self._profile_id())
        if row is None:
            return
        dialog = LaybackProfileDialog(row, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            self.model.save_layback_profile(dialog.payload())

    def _delete_profile(self) -> None:
        row = self.model.layback_profile(self._profile_id())
        if row is None:
            return
        answer = QMessageBox.question(
            self, "Delete layback profile",
            f"Delete '{row.get('name') or 'this profile'}'? Existing path "
            "results are retained but will be marked stale.",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer == MESSAGE_BOX_YES:
            self.model.delete_layback_profile(row.get("layback_id") or "")

    def _edit_radius_rules(self) -> None:
        dialog = RadiusRulesDialog(self._radius_rules, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            self._radius_rules = dialog.rules()
            self._refresh_radius_rules_label()
            self._apply_settings()

    def _refresh_radius_rules_label(self) -> None:
        rules = self._radius_rules
        if not rules:
            self.radius_rules_label.setText("Constant tool radius")
            return
        bands = "; ".join(
            f"{rule['radius_m']:g} m ≤{rule['max_depth_m']:g} m"
            for rule in rules)
        self.radius_rules_label.setText(f"{len(rules)} band(s): {bands}")

    def _vessel_selection_changed(self) -> None:
        selected = bool(self._vessel_id())
        self.edit_vessel_button.setEnabled(selected)
        self.delete_vessel_button.setEnabled(selected)

    def _new_vessel(self) -> None:
        dialog = VesselDialog(parent=self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            vessel_id = self.model.save_vessel(dialog.payload())
            if vessel_id:
                self.refresh()
                self.vessel_combo.setCurrentIndex(max(
                    0, self.vessel_combo.findData(vessel_id)))
                self._apply_settings()

    def _edit_vessel(self) -> None:
        row = self.model.vessel(self._vessel_id())
        if row is None:
            return
        dialog = VesselDialog(row, self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            self.model.save_vessel(dialog.payload())

    def _delete_vessel(self) -> None:
        row = self.model.vessel(self._vessel_id())
        if row is None:
            return
        answer = QMessageBox.question(
            self, "Delete vessel",
            f"Delete '{row.get('name') or 'this vessel'}'? Existing path "
            "results are retained; the barge-track turn check and outline "
            "display simply lose their reference.",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer == MESSAGE_BOX_YES:
            self.model.delete_vessel(row.get("vessel_id") or "")

    def _generate(self) -> None:
        if self._task is not None:
            return
        if not self.model.plan or self.model.route is None:
            QMessageBox.warning(
                self, "Installation Paths",
                "Select a plan with a resolved RPL before generating a path.")
            return
        tool, config_row = path_data.effective_tool_and_config(
            self.model.plan, self.model.tools)
        if tool is None or config_row is None:
            QMessageBox.warning(
                self, "Installation Paths",
                "Choose a registered plan default tool and configuration on "
                "the Plan tab.")
            return
        try:
            radius = float(config_row.get("min_turn_radius_m"))
        except (TypeError, ValueError):
            radius = 0.0
        if radius <= 0.0:
            QMessageBox.warning(
                self, "Installation Paths",
                "The selected tool configuration needs a minimum turning "
                "radius greater than zero.")
            return
        if not self._apply_settings():
            return
        path_config = self.model.path_config()
        generate_barge = bool(path_config.get("generate_barge"))
        if generate_barge and schema.normalise_method(
                tool.get("tool_type") or "") != schema.METHOD_PLOUGH:
            QMessageBox.warning(
                self, "Installation Paths",
                "Barge-track generation is available for plough tools only.")
            return
        layback = self.model.layback_profile(
            path_config.get("layback_id") or "") if generate_barge else None
        if generate_barge and not path_data.layback_points(layback):
            QMessageBox.warning(self, "Installation Paths",
                                "The selected layback profile has no values.")
            return

        radius_rules = path_data.sanitise_radius_rules(
            path_config.get("radius_rules"))
        needs_depth = bool(radius_rules) or bool(
            layback and len(path_data.layback_points(layback)) > 1)
        depth_samples = None
        if self.model.profile_state() == "current" \
                and self.model.bathy_profile is not None:
            depth_samples = self.model.bathy_profile.samples()
        # A live sampler is also passed when available even if the profile
        # covers the KP lookups: only it can read depth off-route, which the
        # per-course-change depth-difference diagnostic needs.
        depth_snapshot = None
        try:
            snapshot = DepthSnapshot(self.model.depth_config(),
                                     QgsProject.instance())
            if snapshot.is_available():
                depth_snapshot = snapshot
        except Exception:
            depth_snapshot = None
        if needs_depth and depth_samples is None and depth_snapshot is None:
            QMessageBox.warning(
                self, "Installation Paths",
                "A depth-based turning radius or depth-varying layback "
                "profile is configured. Register bathymetry on Inputs or "
                "prepare a current bathymetry profile first.")
            return

        fingerprints = self.model.path_fingerprints(path_config)
        display = tools_mod.tool_display(
            self.model.tools, tool.get("tool_id") or "",
            config_row.get("config_id") or "")
        try:
            work = build_path_work(
                self.model.route, self.model.distance, self.model.plan,
                radius, path_config.get("mode") or path_data.MODE_FILLET,
                path_config.get("max_deviation_m") or 0.0,
                display, tool.get("tool_type") or "", fingerprints,
                path_config, layback_profile=layback,
                depth=depth_snapshot, depth_samples=depth_samples,
                radius_rules=radius_rules)
        except Exception as exc:
            QMessageBox.warning(self, "Installation Paths",
                                f"The task could not be prepared:\n{exc}")
            return
        self._generation += 1
        generation = self._generation
        plan_id = self.model.plan_id

        def finished(task: InstallationPathTask) -> None:
            if generation != self._generation:
                return
            self._task = None
            self.progress.setVisible(False)
            self.stop_button.setVisible(False)
            self.generate_button.setEnabled(True)
            if task.cancelled:
                self.status_label.setText("Path generation cancelled; the previous result was retained.")
                return
            if task.error or task.result is None:
                self.status_label.setText(
                    f"Path generation failed: {task.error or 'unknown error'}")
                QMessageBox.warning(
                    self, "Installation Paths",
                    task.error or "Installation path generation failed.")
                return
            if self.model.plan_id != plan_id:
                # The selected plan changed while the worker was running.
                # Discard the immutable result and restore the new plan's
                # status instead of leaving the old task message visible.
                self.refresh()
                return
            if self.model.save_path_result(task.result.registry_row(work)):
                self.status_label.setText("Installation paths generated and saved.")

        self._task = InstallationPathTask(work, finished)
        self._task.progressChanged.connect(
            lambda value: self.progress.setValue(int(value)))
        self._task.progressMessage.connect(self.status_label.setText)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.stop_button.setVisible(True)
        self.generate_button.setEnabled(False)
        QgsApplication.taskManager().addTask(self._task)

    def _stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.status_label.setText("Stopping path generation…")

    def _clear(self) -> None:
        if not self.model.path_result:
            return
        answer = QMessageBox.question(
            self, "Clear installation paths",
            "Clear the saved tool path, barge track and diagnostics for this "
            "plan? They can be regenerated from the saved settings.",
            MESSAGE_BOX_YES | MESSAGE_BOX_NO, MESSAGE_BOX_NO)
        if answer == MESSAGE_BOX_YES:
            self.model.delete_path_result()

    def _vessel_check_text(self, summary: Dict) -> str:
        """Barge tightest turn vs the selected vessel — computed at display
        time so changing the vessel never marks the geometry stale."""
        if not summary.get("barge_generated"):
            return ""
        try:
            track_radius = float(summary.get("barge_min_radius_m"))
        except (TypeError, ValueError):
            return "vessel track is straight (no turn to check)"
        vessel = self.model.vessel(
            str(self.model.path_config().get("vessel_id") or ""))
        try:
            vessel_radius = float((vessel or {}).get("min_turn_radius_m")
                                  or 0.0)
        except (TypeError, ValueError):
            vessel_radius = 0.0
        if vessel is None or vessel_radius <= 0.0:
            return (f"vessel track tightest turn {track_radius:,.0f} m "
                    f"(no vessel radius to check)")
        if track_radius + 1e-6 < vessel_radius:
            return (f"⚠ vessel track tightest turn {track_radius:,.0f} m is "
                    f"inside {vessel.get('name') or 'the vessel'}'s "
                    f"{vessel_radius:,.0f} m minimum")
        return (f"vessel track tightest turn {track_radius:,.0f} m ≥ "
                f"{vessel.get('name') or 'vessel'} minimum "
                f"{vessel_radius:,.0f} m ✓")

    def _set_visibility(self, part: str, visible: bool) -> None:
        if not self.model.plan:
            return
        path_layers.set_path_visibility(
            QgsProject.instance(), self.model.store.gpkg_path,
            self.model.plan, part, visible)

    def refresh(self) -> None:
        self._loading = True
        try:
            config = self.model.path_config()
            self.mode_combo.setCurrentIndex(max(
                0, self.mode_combo.findData(config.get("mode"))))
            self.max_deviation_spin.setValue(
                float(config.get("max_deviation_m") or 0.0))
            self._radius_rules = path_data.sanitise_radius_rules(
                config.get("radius_rules"))
            self._refresh_radius_rules_label()
            selected_vessel = str(config.get("vessel_id") or "")
            self.vessel_combo.clear()
            self.vessel_combo.addItem("(no vessel selected)", "")
            for row in self.model.vessels:
                bits = []
                try:
                    radius_value = float(row.get("min_turn_radius_m") or 0.0)
                except (TypeError, ValueError):
                    radius_value = 0.0
                if radius_value > 0.0:
                    bits.append(f"min turn {radius_value:g} m")
                if row.get("footprint_wkt"):
                    bits.append("outline")
                detail = f" ({', '.join(bits)})" if bits else ""
                self.vessel_combo.addItem(
                    f"{row.get('name') or 'Unnamed'}{detail}",
                    row.get("vessel_id") or "")
            self.vessel_combo.setCurrentIndex(max(
                0, self.vessel_combo.findData(selected_vessel)))
            self._vessel_selection_changed()
            selected = str(config.get("layback_id") or "")
            self.layback_combo.clear()
            self.layback_combo.addItem("(select a layback profile)", "")
            for row in self.model.layback_profiles:
                points = path_data.layback_points(row)
                kind = "constant" if len(points) == 1 else f"{len(points)} points"
                self.layback_combo.addItem(
                    f"{row.get('name') or 'Unnamed'} ({kind})",
                    row.get("layback_id") or "")
            self.layback_combo.setCurrentIndex(max(
                0, self.layback_combo.findData(selected)))
            effective_tool, _effective_config = \
                path_data.effective_tool_and_config(
                    self.model.plan, self.model.tools)
            is_plough = schema.normalise_method(
                (effective_tool or {}).get("tool_type") or "") \
                == schema.METHOD_PLOUGH
            self.generate_barge_check.setChecked(
                bool(config.get("generate_barge")) and is_plough)
            self.generate_barge_check.setEnabled(is_plough)
            self.generate_barge_check.setToolTip(
                "" if is_plough else
                "The tow-point track applies to towed (plough) tools only; "
                "the plan default tool is not a plough.")
            self._barge_toggled(self.generate_barge_check.isChecked())
            self.edit_profile_button.setEnabled(bool(self._profile_id()))
            self.delete_profile_button.setEnabled(bool(self._profile_id()))

            tool, tool_config = path_data.effective_tool_and_config(
                self.model.plan, self.model.tools)
            if tool is not None and tool_config is not None:
                self.tool_label.setText(tools_mod.tool_display(
                    self.model.tools, tool.get("tool_id") or "",
                    tool_config.get("config_id") or ""))
                radius = tool_config.get("min_turn_radius_m")
                try:
                    radius_text = f"{float(radius):g} m"
                except (TypeError, ValueError):
                    radius_text = "Not entered"
                self.radius_label.setText(radius_text)
            else:
                self.tool_label.setText("Not selected")
                self.radius_label.setText("Not available")

            state = self.model.path_state() if self.model.plan else {
                "tool": "missing", "barge": "missing"}
            row = self.model.path_result or {}
            summary = path_data.parse_json_field(row, "summary_json", {})
            if state["tool"] == "missing":
                status = "No path generated."
            else:
                status = (f"Tool path: {state['tool']}; barge track: "
                          f"{state['barge']}. Generated "
                          f"{row.get('generated_utc') or 'at an unknown time'}.")
            if self._task is None:
                self.status_label.setText(status)
            if summary:
                bits = [
                    f"{int(summary.get('course_change_count') or 0)} course "
                    f"changes",
                    f"{int(summary.get('compound_cluster_count') or 0)} "
                    f"compound clusters",
                    f"path {float(summary.get('length_m') or 0):,.1f} m",
                    f"maximum RPL offset "
                    f"{float(summary.get('max_offset_m') or 0):,.1f} m",
                    f"review {int(summary.get('review_count') or 0)}",
                ]
                if summary.get("radius_rules_count"):
                    bits.insert(2, (
                        f"applied radius "
                        f"{float(summary.get('radius_min_m') or 0):g}–"
                        f"{float(summary.get('radius_max_m') or 0):g} m "
                        f"(depth-based)"))
                depth_diff_worst = summary.get("depth_diff_worst_m")
                if depth_diff_worst is not None:
                    bits.append(
                        f"worst path-vs-RPL depth difference "
                        f"{float(depth_diff_worst):+.1f} m")
                bits.append(self._vessel_check_text(summary))
                self.summary_label.setText(
                    " | ".join(bit for bit in bits if bit))
            else:
                self.summary_label.setText("")
            diagnostics = path_data.parse_json_field(row, "diagnostics_json", [])
            self.table.setRowCount(0)
            for item in diagnostics if isinstance(diagnostics, list) else []:
                table_row = self.table.rowCount()
                self.table.insertRow(table_row)
                depth_diff = item.get("depth_diff_m")
                values = [
                    f"{float(item.get('kp') or 0):.3f}",
                    f"{float(item.get('turn_deg') or 0):.2f}",
                    item.get("side") or "", item.get("solution") or "",
                    f"{float(item.get('radius_m') or 0):g}",
                    f"{float(item.get('miss_m') or 0):.2f}",
                    f"{float(item.get('max_offset_m') or 0):.2f}",
                    ("—" if depth_diff is None
                     else f"{float(depth_diff):+.1f}"),
                    item.get("status") or "",
                ]
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    cell.setData(ITEM_DATA_USER_ROLE, item)
                    self.table.setItem(table_row, column, cell)
            available = bool(self.model.plan)
            self.apply_button.setEnabled(available and self._task is None)
            self.radius_rules_button.setEnabled(
                available and self._task is None)
            self.generate_button.setEnabled(available and self._task is None)
            self.clear_button.setEnabled(bool(row) and self._task is None)
        finally:
            self._loading = False

    def shutdown(self) -> None:
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def is_running(self) -> bool:
        return self._task is not None
