"""Numeric Ground Model controls: choose, define, check and remove datasets.

Datasets are project-scoped (shared by every plan, like soil classes and
not part of a plan's history). A plan stores only which dataset it shows and
its depth window. KP placement is read live from the dataset's layer on every
refresh, so edits to that layer show without re-importing.
"""
import json

from qgis.PyQt.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from ..qgis_compat import DIALOG_ACCEPTED, MESSAGEBOX_NO, MESSAGEBOX_YES, qt_exec
from . import numeric_datasets as sources, numeric_profiles as numeric, ui_helpers
from .numeric_profile_dialogs import DatasetDialog, check_dialog, export_csv, source_dialog


class NumericProfilePanel(QWidget):
    def __init__(self, model, dock, plot, parent=None):
        super().__init__(parent)
        self.model, self.dock, self.plot = model, dock, plot
        self.datasets, self.profiles, self.assignments, self.notes = [], [], [], []
        self.state = {}
        self.active = False
        self._profiles_key = None
        self._watched = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.dataset = QComboBox()
        self.dataset.setMinimumWidth(220)
        self.dataset.setToolTip("Numeric datasets are shared by every plan in this project.")
        row.addWidget(QLabel("Dataset"))
        row.addWidget(self.dataset, 1)
        self.buttons = {}
        for key, label, tip, callback in (
                ("add", "Add…", "Define a dataset: measurements, KP ranges and colours.", self._add),
                ("edit", "Edit…", "Change the dataset's measurements, KP ranges or colours.", self._edit),
                ("reload", "Reload", "Re-read the measurements from their source with the saved choices.",
                 self._reload),
                ("remove", "Remove…", "Delete the dataset and its measurements from this project.", self._remove),
                ("check", "Check…", "Per-investigation table and coverage findings.", self._check),
                ("export", "Export cells…", "Write every plotted cell (KP, depth, value, class) to CSV.",
                 self._export)):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(callback)
            self.buttons[key] = button
            row.addWidget(button)
        layout.addLayout(row)
        depth_row = QHBoxLayout()
        self.depth_min, self.depth_max = self._spin(0), self._spin(3)
        for widget in (QLabel("Depth from"), self.depth_min, QLabel("to"), self.depth_max, QLabel("m")):
            depth_row.addWidget(widget)
        apply = QPushButton("Apply depth")
        apply.clicked.connect(self._apply_depth)
        depth_row.addWidget(apply)
        depth_row.addStretch()
        layout.addLayout(depth_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._refresh_soon = ui_helpers.coalesced(self, self.render, 300)
        self.dataset.currentIndexChanged.connect(self._dataset_chosen)
        self.plot.profileClicked.connect(self.inspect_source)

    @staticmethod
    def _spin(value):
        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setRange(0, 1e5)
        spin.setValue(value)
        spin.setSuffix("")
        spin.setKeyboardTracking(False)
        return spin

    # -- state -------------------------------------------------------------------
    def selected(self):
        dataset_id = self.dataset.currentData()
        return next((d for d in self.datasets if d["dataset_id"] == dataset_id), None)

    def reload_sources(self):
        self._profiles_key = None
        self.refresh()

    def refresh(self):
        self.datasets = self.model.store.list_ground_datasets()
        try:
            params = json.loads((self.model.plan or {}).get("params_json") or "{}")
        except (ValueError, TypeError):
            params = {}
        state = params.get("numeric_ground", {}) if isinstance(params, dict) else {}
        self.state = state if isinstance(state, dict) else {}
        self.setEnabled(bool(self.model.plan))
        wanted = self.state.get("dataset_id") or self.dataset.currentData()
        self.dataset.blockSignals(True)
        self.dataset.clear()
        for dataset in self.datasets:
            self.dataset.addItem(f"{dataset['name']} — {dataset['variable']} ({dataset['units'] or 'unitless'})",
                                 dataset["dataset_id"])
        self.dataset.setCurrentIndex(max(0, self.dataset.findData(wanted)))
        self.dataset.blockSignals(False)
        for key, widget, default in (("depth_min", self.depth_min, 0), ("depth_max", self.depth_max, 3)):
            widget.blockSignals(True)
            widget.setValue(float(self.state.get(key, default)))
            widget.blockSignals(False)
        self.render()

    def _save(self, updates, reason):
        state = dict(self.state)
        state.update(updates)
        if self.model.update_gen_params({"numeric_ground": state}, reason=reason, stale=False):
            self.state = state
            self.render()
            return True
        return False

    def _dataset_chosen(self, *_args):
        if self.model.plan:
            self._save({"dataset_id": self.dataset.currentData() or ""}, "Ground model dataset")

    def _apply_depth(self):
        if self.depth_max.value() <= self.depth_min.value():
            self.status.setText("The depth 'to' must be greater than 'from'; not applied.")
            return
        self._save({"depth_min": self.depth_min.value(), "depth_max": self.depth_max.value()},
                   "Ground model depth window")

    # Edits (live, during an edit session), commits, rollbacks and provider reloads.
    _LAYER_SIGNALS = ("featureAdded", "featureDeleted", "attributeValueChanged", "geometryChanged",
                      "afterCommitChanges", "afterRollBack", "dataChanged")

    def _watch(self, layer):
        """Refresh when the placement layer's features change (live link)."""
        if layer is self._watched:
            return
        for name in self._LAYER_SIGNALS if self._watched is not None else ():
            try:
                getattr(self._watched, name).disconnect(self._refresh_soon)
            except (TypeError, RuntimeError, AttributeError):
                pass
        self._watched = layer
        for name in self._LAYER_SIGNALS if layer is not None else ():
            signal = getattr(layer, name, None)
            if signal is not None:
                signal.connect(self._refresh_soon)

    def _load(self, dataset):
        """Measurements (cached per dataset version) and live placement."""
        key = (dataset["dataset_id"], dataset.get("updated_utc")) if dataset else None
        if key != self._profiles_key:
            self.profiles = self.model.store.list_numeric_profiles(dataset["dataset_id"]) if dataset else []
            self._profiles_key = key
        self.assignments, self.notes, layer = [], [], None
        if dataset is None:
            self._watch(None)
            return
        placement = (dataset.get("config") or {}).get("placement") or {}
        try:
            self.assignments, self.notes, layer = sources.read_placement(self.model, placement)
        except (ValueError, RuntimeError) as exc:
            self.notes = [str(exc)]
        self._watch(layer)
        changed = sources.source_changed(dataset)
        if changed:
            self.notes.insert(0, changed)

    def render(self):
        dataset = self.selected()
        for key in ("edit", "reload", "remove", "check", "export"):
            self.buttons[key].setEnabled(dataset is not None)
        self.buttons["add"].setEnabled(bool(self.model.plan))
        self._load(dataset)
        if dataset is None:
            self.status.setText("No numeric dataset yet. Add… defines one: a measurements table "
                                "(ID, depth, value), the layer giving each ID's KP range, and colours.")
            if self.active:
                self.plot.set_numeric(numeric.ProfileIndex([], []), self.display_settings(None))
            return
        ids = {a["source_id"] for a in self.assignments}
        known = {p["source_id"] for p in self.profiles}
        text = (f"{len(self.profiles)} investigation(s) from "
                f"{sources.source_label(((dataset.get('config') or {}).get('measurements') or {}).get('source') or {})}; "
                f"{len(self.assignments)} KP range(s), {len(ids & known)} placed.")
        problems = list(self.notes)
        unmatched = numeric.unmatched_ids(ids, known)
        if unmatched:
            problems.append(f"{len(unmatched)} KP range ID(s) without measurements")
        unplaced = len(known - ids)
        if unplaced and self.assignments:
            problems.append(f"{unplaced} investigation(s) without a KP range")
        if problems:
            text += " ⚠ " + "; ".join(problems[:3]) + (" — Check… lists all" if len(problems) > 3 or unmatched
                                                         or unplaced else "")
        self.status.setText(text)
        if self.active:
            try:
                index = numeric.ProfileIndex(self.profiles, self.assignments)
            except ValueError as exc:
                self.status.setText(text + f" ✗ {exc}")
                index = numeric.ProfileIndex(self.profiles, [])
            self.plot.set_numeric(index, self.display_settings(dataset))

    def display_settings(self, dataset):
        dataset = dataset or {}
        return {"name": dataset.get("name") or "", "variable": dataset.get("variable") or "",
                "units": dataset.get("units") or "",
                "depth_min": float(self.state.get("depth_min", 0)), "depth_max": float(self.state.get("depth_max", 3)),
                "colours": (dataset.get("config") or {}).get("colours") or {}}

    # -- dataset actions -----------------------------------------------------------
    def _write(self, action, func):
        # Shared project data, like soil classes and tools: not rolled back
        # by a plan's history (another plan may show the same dataset).
        ok, result = self.model._store_transaction(action, func)
        if ok:
            self._profiles_key = None
            self.model.groundChanged.emit()
        return ok, result

    def _add(self):
        self._open_dialog(None)

    def _edit(self):
        dataset = self.selected()
        if dataset is not None:
            self._open_dialog(dataset)

    def _open_dialog(self, dataset):
        profiles = self.model.store.list_numeric_profiles(dataset["dataset_id"]) if dataset else []
        dialog = DatasetDialog(self.model, dataset, profiles, self)
        if qt_exec(dialog) != DIALOG_ACCEPTED or dialog.result() is None:
            return
        self.save_dataset(*dialog.result())

    def save_dataset(self, dataset, profiles):
        if not any(d["dataset_id"] == dataset["dataset_id"] for d in self.datasets):
            dataset = dict(dataset, seq=len(self.datasets))
        ok, dataset_id = self._write("save numeric dataset",
                                     lambda: self.model.store.save_ground_dataset(dataset, profiles))
        if ok:
            self._save({"dataset_id": dataset_id}, "Ground model dataset")
        return ok

    def _reload(self):
        dataset = self.selected()
        if dataset is None:
            return
        try:
            profiles, config = sources.reload_measurements(dataset)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Reload measurements", str(exc))
            return
        before = numeric.summary_text(self.profiles, dataset.get("units"))
        ok, _ = self._write("reload numeric dataset",
                            lambda: self.model.store.save_ground_dataset(dict(dataset, config=config), profiles))
        if ok:
            QMessageBox.information(self, "Reload measurements",
                                    f"Before: {before}.\nNow: {numeric.summary_text(profiles, dataset.get('units'))}.")

    def _remove(self):
        dataset = self.selected()
        if dataset is None:
            return
        users = []
        for plan in self.model.store.list_plans():
            try:
                params = json.loads(plan.get("params_json") or "{}")
            except (ValueError, TypeError):
                continue
            if (params.get("numeric_ground") or {}).get("dataset_id") == dataset["dataset_id"]:
                users.append(plan.get("name") or plan.get("plan_id"))
        text = (f"Delete the dataset '{dataset['name']}' and its {len(self.profiles)} investigation(s) "
                "from this project? The source file and KP layer are not touched.")
        if users:
            text += "\n\nPlans showing it: " + ", ".join(users)
        if QMessageBox.question(self, "Remove dataset", text, MESSAGEBOX_YES | MESSAGEBOX_NO) != MESSAGEBOX_YES:
            return
        self.remove_dataset(dataset["dataset_id"])

    def remove_dataset(self, dataset_id):
        ok, _ = self._write("remove numeric dataset", lambda: self.model.store.delete_ground_dataset(dataset_id))
        if ok and self.state.get("dataset_id") == dataset_id:
            self._save({"dataset_id": ""}, "Ground model dataset removed")
        return ok

    def _bounds(self):
        route = self.model.route
        return (route.start_kp_km, route.end_kp_km) if route else None

    def _scope(self):
        scope = self.model.gen_params().scope
        return (scope.start_km, scope.end_km) if scope else None

    def _check(self):
        dataset = self.selected()
        if dataset is None:
            return
        dialog = check_dialog(dataset, self.profiles, self.assignments, self.notes, self._bounds(),
                              self._scope(), self, on_open=self.inspect_source)
        qt_exec(dialog)

    def plotted_cells(self):
        dataset = self.selected() or {}
        classes = numeric.display_classes((dataset.get("config") or {}).get("colours"))
        index = numeric.ProfileIndex(self.profiles, self.assignments)
        units = dataset.get("units") or ""
        headers = ["investigation", "kp_from", "kp_to", "depth_top_m", "depth_base_m",
                   f"value_{units}" if units else "value", "class", "status"]
        rows = [[c["source_id"], f"{c['kp_from']:.6f}", f"{c['kp_to']:.6f}",
                 "" if c["depth_top_m"] is None else f"{c['depth_top_m']:g}",
                 "" if c["depth_base_m"] is None else f"{c['depth_base_m']:g}",
                 "" if c["value"] is None else f"{c['value']:g}", c["class"], c["status"]]
                for c in index.cells(classes, dataset.get("variable") or "value")]
        return headers, rows

    def _export(self, path=None):
        dataset = self.selected()
        if dataset is None:
            return ""
        headers, rows = self.plotted_cells()
        return export_csv(self, f"{dataset['name']} plotted cells.csv", headers, rows, path)

    def inspect_source(self, source):
        if not any(p["source_id"] == source for p in self.profiles):
            QMessageBox.information(self, "Measurements", f"No measurements match {source}.")
            return
        qt_exec(source_dialog(source, self.profiles, self))
