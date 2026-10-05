"""Numeric Ground Model controls; source data and plan assignments stay separate."""
import json

from qgis.core import QgsProject
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QSpinBox, QTableView, QVBoxLayout, QWidget,
)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK, DIALOG_ACCEPTED, qt_exec
from . import numeric_profiles as numeric
from .numeric_profile_dialogs import NumericImportDialog, RowsModel, source_dialog
from .numeric_profile_geometry import polygon_assignments
from .numeric_profile_plot import RAMPS


class NumericProfilePanel(QWidget):
    def __init__(self, model, dock, plot, parent=None):
        super().__init__(parent)
        self.model, self.dock, self.plot = model, dock, plot
        self.profiles = []
        self.state = {}
        self.active = False
        self._loaded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for label, callback in (("Import profiles…", self._import_profiles),
                                ("Assign by KP table…", self._import_assignments),
                                ("Assign by polygons…", self._polygons),
                                ("Review assignments…", self._review),
                                ("Inspect source…", self._inspect)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        controls = QHBoxLayout()
        self.variable = QComboBox()
        self.variable.setMinimumWidth(120)
        self.ramp = QComboBox()
        self.ramp.addItems(list(RAMPS))
        self.auto_colour = QCheckBox("Auto colour limits")
        self.auto_colour.setChecked(True)
        self.colour_min, self.colour_max = self._spin(-1e12, 1e12, 0), self._spin(-1e12, 1e12, 1)
        self.depth_min, self.depth_max = self._spin(0, 1e6, 0), self._spin(0, 1e6, 3)
        self.bands = QSpinBox()
        self.bands.setRange(0, 32)
        self.bands.setSpecialValueText("Continuous")
        self.bands.setToolTip("0: continuous colours; 2–32: equal-width discrete bands")
        for label, widget in (("Variable", self.variable), ("Depth from", self.depth_min),
                              ("to (m)", self.depth_max), ("Ramp", self.ramp), ("Bands", self.bands)):
            controls.addWidget(QLabel(label))
            controls.addWidget(widget)
        layout.addLayout(controls)
        limits_row = QHBoxLayout()
        for widget in (self.auto_colour, QLabel("Colour min"), self.colour_min,
                       QLabel("max"), self.colour_max):
            limits_row.addWidget(widget)
        apply = QPushButton("Apply display")
        apply.clicked.connect(self._apply_display)
        limits_row.addWidget(apply)
        limits_row.addStretch()
        layout.addLayout(limits_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.variable.currentIndexChanged.connect(self._apply_display)
        self.ramp.currentIndexChanged.connect(self._apply_display)
        self.auto_colour.toggled.connect(self._apply_display)
        self.bands.valueChanged.connect(self._apply_display)
        self.plot.profileClicked.connect(self.inspect_source)

    @staticmethod
    def _spin(lo, hi, value):
        spin = QDoubleSpinBox()
        spin.setDecimals(4)
        spin.setRange(lo, hi)
        spin.setValue(value)
        spin.setKeyboardTracking(False)
        return spin

    def reload_sources(self):
        self.refresh(reload_sources=True)

    def refresh(self, reload_sources=False):
        if not self._loaded or reload_sources:
            self.profiles = self.model.store.list_numeric_profiles()
            self._loaded = True
        try:
            params = json.loads((self.model.plan or {}).get("params_json") or "{}")
        except (ValueError, TypeError):
            params = {}
        state = params.get("numeric_ground", {}) if isinstance(params, dict) else {}
        self.state = state if isinstance(state, dict) else {}
        settings = self.state.get("display", {})
        self.setEnabled(bool(self.model.plan))
        self.variable.blockSignals(True)
        self.variable.clear()
        for name, unit in sorted({(p["variable"], p["units"]) for p in self.profiles}):
            self.variable.addItem(f"{name} ({unit or 'unitless'})", [name, unit])
        selected = self.variable.findData(settings.get("variable"))
        if selected >= 0:
            self.variable.setCurrentIndex(selected)
        self.variable.blockSignals(False)
        for key, widget, default in (("depth_min", self.depth_min, 0), ("depth_max", self.depth_max, 3),
                                     ("colour_min", self.colour_min, 0), ("colour_max", self.colour_max, 1),
                                     ("bands", self.bands, 0)):
            widget.blockSignals(True)
            widget.setValue(settings.get(key, default))
            widget.blockSignals(False)
        self.ramp.blockSignals(True)
        self.ramp.setCurrentText(settings.get("ramp", "Viridis"))
        self.ramp.blockSignals(False)
        self.auto_colour.blockSignals(True)
        self.auto_colour.setChecked(settings.get("auto_colour", True))
        self.auto_colour.blockSignals(False)
        self.render()

    def settings(self):
        return {"variable": self.variable.currentData() or ["", ""],
                "ramp": self.ramp.currentText(), "auto_colour": self.auto_colour.isChecked(),
                "colour_min": self.colour_min.value(), "colour_max": self.colour_max.value(),
                "depth_min": self.depth_min.value(), "depth_max": self.depth_max.value(),
                "bands": self.bands.value()}

    def _save(self, updates, reason):
        state = dict(self.state)
        state.update(updates)
        if self.model.update_gen_params({"numeric_ground": state}, reason=reason, stale=False):
            self.state = state
            self.render()
            return True
        return False

    def _apply_display(self, *_args):
        if not self.model.plan:
            return
        settings = self.settings()
        if settings["depth_max"] <= settings["depth_min"]:
            self.status.setText("Depth maximum must exceed the minimum; display settings have not been saved.")
            return
        if not settings["auto_colour"] and settings["colour_max"] <= settings["colour_min"]:
            self.status.setText("Colour maximum must exceed the minimum; display settings have not been saved.")
            return
        if settings["bands"] == 1:
            self.status.setText("Choose at least two bands, or 0 for continuous colours.")
            return
        self._save({"display": settings}, "Numeric ground display")

    def render(self):
        assignments = self.state.get("assignments", [])
        route = self.model.route
        bounds = (route.start_kp_km, route.end_kp_km) if route else None
        issues = numeric.assignment_issues(assignments, self.profiles, bounds)
        issues += self.state.get("assignment_warnings", [])
        text = f"{len(self.profiles)} source variable profile(s); {len(assignments)} assignment interval(s)."
        if issues:
            text += " " + "; ".join(issues[:4])
            if len(issues) > 4:
                text += f" (+{len(issues) - 4} more; Review assignments)"
        self.status.setText(text)
        self.colour_min.setEnabled(not self.auto_colour.isChecked())
        self.colour_max.setEnabled(not self.auto_colour.isChecked())
        if self.active:
            settings = dict(self.state.get("display", self.settings()))
            if self.variable.findData(settings.get("variable")) < 0:
                settings["variable"] = self.variable.currentData() or ["", ""]
            index = numeric.ProfileIndex(self.profiles, assignments, settings.get("variable", self.variable.currentData() or ["", ""]))
            if settings.get("auto_colour", True):
                lo, hi = index.limits()
                self.colour_min.setValue(lo)
                self.colour_max.setValue(hi)
            self.plot.set_numeric(index, settings)

    def _import_profiles(self):
        dialog = NumericImportDialog(self.model, self.dock, parent=self)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        profiles = dialog.result_rows
        # Shared project measurements, like soil classes and tools, are not
        # rolled back by a plan's history (another plan may use them).
        ok, _ = self.model._store_transaction(
            "import numeric profiles", lambda: self.model.store.save_numeric_profiles(profiles))
        if ok:
            self.model.groundChanged.emit()

    def _import_assignments(self):
        dialog = NumericImportDialog(self.model, self.dock, assignments=True, parent=self)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            self._save({"assignments": dialog.result_rows, "assignment_warnings": []}, "Assign numeric ground profiles by KP table")

    def _polygons(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Assign investigations by polygons")
        layout = QVBoxLayout(dialog)
        note = QLabel("Replace this plan's assignments with each polygon's route crossings. "
                      "IDs must match the imported profiles. Overlaps and unmatched IDs are retained and flagged.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        layers, field = QComboBox(), QComboBox()
        for layer in QgsProject.instance().mapLayers().values():
            if hasattr(layer, "geometryType") and layer.geometryType() == 2:
                layers.addItem(layer.name(), layer.id())

        def fields(*_args):
            field.clear()
            layer = QgsProject.instance().mapLayer(layers.currentData())
            if layer is not None:
                field.addItems(layer.fields().names())

        layers.currentIndexChanged.connect(fields)
        fields()
        form.addRow("Polygon layer", layers)
        form.addRow("Investigation ID", field)
        layout.addLayout(form)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        layer = QgsProject.instance().mapLayer(layers.currentData())
        if layer is None:
            return
        try:
            rows, warnings = polygon_assignments(self.model.route, layer, field.currentText())
        except (ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Polygon assignment", str(exc))
            return
        self._save({"assignments": rows, "assignment_warnings": warnings}, "Assign numeric profiles by polygon")
        if warnings:
            QMessageBox.information(self, "Polygon assignment", "\n".join(warnings[:30]))

    def _review(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Numeric ground assignments")
        dialog.resize(850, 550)
        layout = QVBoxLayout(dialog)
        route = self.model.route
        issues = numeric.assignment_issues(self.state.get("assignments", []), self.profiles,
                                           (route.start_kp_km, route.end_kp_km) if route else None)
        issues += self.state.get("assignment_warnings", [])
        from qgis.PyQt.QtWidgets import QPlainTextEdit
        notes = QPlainTextEdit("\n".join(issues) or "No assignment issues.")
        notes.setReadOnly(True)
        layout.addWidget(notes)
        keys = ("source_id", "start_kp", "end_kp", "flags", "source_ref", "src_start_kp", "src_end_kp")
        table = QTableView()
        table_model = RowsModel(keys, [[f"{r[k]:.3f}" if k.endswith("_kp") and r.get(k) is not None else r.get(k, "")
                                       for k in keys] for r in self.state.get("assignments", [])], table)
        table.setModel(table_model)
        layout.addWidget(table)
        qt_exec(dialog)

    def _inspect(self):
        from qgis.PyQt.QtWidgets import QInputDialog
        sources = sorted({p["source_id"] for p in self.profiles})
        if not sources:
            return
        source, ok = QInputDialog.getItem(self, "Inspect source", "Investigation", sources, 0, False)
        if ok:
            self.inspect_source(source)

    def inspect_source(self, source):
        if not any(p["source_id"] == source for p in self.profiles):
            QMessageBox.information(self, "Source profile", f"No imported profile matches {source}.")
            return
        dialog = source_dialog(source, self.profiles, self)
        qt_exec(dialog)
