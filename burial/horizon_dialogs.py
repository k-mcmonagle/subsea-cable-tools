# -*- coding: utf-8 -*-
"""DXF section import and modest horizon editing using the existing Qt stack."""

from __future__ import annotations

import os

import pyqtgraph as pg
from qgis.core import QgsApplication, QgsProject
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from ..qgis_compat import (
    BUTTON_BOX_CANCEL, BUTTON_BOX_OK, CHECK_STATE_CHECKED, CHECK_STATE_UNCHECKED,
    DIALOG_ACCEPTED, ITEM_FLAG_USER_CHECKABLE, qt_exec,
)
from . import dxf_annotations, horizons, schema
from .annotation_overlay import AnnotationOverlay
from .ground_dialogs import KpReferenceWidget
from .ground_plot import GroundModelPlot
from .horizon_task import HorizonTask


def route_reference(model):
    """Identify both the physical route and its KP numbering, without writes."""
    return {"rpl": model.resolved_rpl_id or model.plan.get("rpl_id") or "",
            "geometry": model.route_geometry_fingerprint(),
            "datum": model.kp_datum_start(), "mode": list(model.kp_mode())}


def reference_notice(row, model, route=None):
    source = row.get("source") or {}
    if source.get("route") != (route if route is not None else route_reference(model)):
        return "Route/KP reference changed; re-import this overlay for the current route."
    sampled = source.get("profile_sampled_utc")
    if sampled is not None:
        profile = model.bathy_profile
        if profile is None or sampled != profile.sampled_utc:
            return "Seabed profile changed; depths retain the imported reference. Re-import to update."
    return ""


def _spin(value=0, positive=False):
    widget = QDoubleSpinBox()
    widget.setDecimals(6)
    widget.setRange(0.000001 if positive else -1e9, 1e9)
    widget.setValue(value)
    return widget


def _units():
    widget = QComboBox()
    for label, factor in (("Metres", 1.0), ("Kilometres", 1000.0), ("Millimetres", 0.001),
                          ("Centimetres", 0.01), ("Feet", 0.3048), ("Inches", 0.0254)):
        widget.addItem(label, factor)
    return widget


class HorizonImportDialog(QDialog):
    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model, self.dock = model, dock
        self.rows, self.drawing = [], None
        self.annotations = []
        self._raw_items = []
        self._task = None
        self._closed = False
        self._preview_context = None
        self._loading = False
        self._plan_id = model.plan_id
        self._route = route_reference(model)
        self.setWindowTitle("Import DXF horizons and labels")
        self.resize(1000, 760)
        layout = QVBoxLayout(self)
        file_row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setReadOnly(True)
        browse = self.browse_button = QPushButton("Open DXF…")
        browse.clicked.connect(self._browse)
        file_row.addWidget(self.path, 1)
        file_row.addWidget(browse)
        layout.addLayout(file_row)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        drawing_page = QWidget()
        drawing_layout = QVBoxLayout(drawing_page)
        self.file_note = QLabel("Choose a 2D section: X = KP/chainage, Y = depth/elevation. Tick horizon layers to import.")
        self.file_note.setWordWrap(True)
        drawing_layout.addWidget(self.file_note)
        split = QSplitter(getattr(Qt, "Orientation", Qt).Horizontal)
        self.layers = QListWidget()
        self.layers.itemChanged.connect(self._selection_changed)
        layer_panel = QWidget()
        layer_layout = QVBoxLayout(layer_panel)
        layer_layout.addWidget(QLabel("Horizon layers"))
        layer_layout.addWidget(self.layers, 1)
        self.import_labels = QCheckBox("Import labels (annotations only)")
        self.import_labels.setToolTip("Import selected TEXT/MTEXT layers as plain, upright labels. "
                                      "They do not create soil classes or ground-model units.")
        self.import_labels.toggled.connect(self._selection_changed)
        layer_layout.addWidget(self.import_labels)
        self.label_layers = QListWidget()
        self.label_layers.setEnabled(False)
        self.import_labels.toggled.connect(self.label_layers.setEnabled)
        self.label_layers.itemChanged.connect(self._selection_changed)
        layer_layout.addWidget(self.label_layers, 1)
        split.addWidget(layer_panel)
        self.raw_plot = pg.PlotWidget(background="w")
        self.raw_plot.setLabel("bottom", "Drawing X")
        self.raw_plot.setLabel("left", "Drawing Y")
        self.raw_annotations = AnnotationOverlay(self.raw_plot)
        self.raw_label_notice = QLabel()
        self.raw_annotations.noticeChanged.connect(self.raw_label_notice.setText)
        split.addWidget(self.raw_plot)
        split.setStretchFactor(1, 2)
        drawing_layout.addWidget(split, 1)
        drawing_layout.addWidget(self.raw_label_notice)
        self.tabs.addTab(drawing_page, "1. Drawing & layers")

        coords = QWidget()
        coords_layout = QVBoxLayout(coords)
        boxes = QHBoxLayout()
        horizontal = QGroupBox("Horizontal coordinates")
        form = QFormLayout(horizontal)
        self.x_units = _units()
        self.x_scale = _spin(1, True)
        self.x_scale.setToolTip("Metres × 10 means 10 m per drawing X unit. Keep 1 if coordinates are already metres.")
        self.x_origin = _spin()
        self.kp_origin = _spin()
        self.reverse = QCheckBox("KP decreases as drawing X increases")
        for label, control in (("X units:", self.x_units), ("Horizontal scale multiplier:", self.x_scale),
                               ("Drawing X origin:", self.x_origin), ("KP at that origin (km):", self.kp_origin)):
            form.addRow(label, control)
        form.addRow(self.reverse)
        boxes.addWidget(horizontal)
        vertical = QGroupBox("Vertical coordinates")
        form = QFormLayout(vertical)
        self.vertical_ref = QComboBox()
        self.vertical_ref.addItem("Depth below seabed", "seabed")
        self.vertical_ref.addItem("Relative to a vertical datum", "datum")
        self.y_units = _units()
        self.y_origin = _spin()
        self.y_offset = _spin()
        self.exaggeration = _spin(1, True)
        self.exaggeration.setToolTip("Divides Y after converting units. With Metres, 0.1 gives 10 m per drawing Y unit.")
        self.positive_up = QCheckBox("Y increases upwards (elevations)")
        for label, control in (("Reference:", self.vertical_ref), ("Y units:", self.y_units),
                               ("Drawing Y origin:", self.y_origin), ("Depth at Y origin (m, +down):", self.y_offset),
                               ("Vertical exaggeration (divide by):", self.exaggeration)):
            form.addRow(label, control)
        form.addRow(self.positive_up)
        boxes.addWidget(vertical)
        coords_layout.addLayout(boxes)
        self.datum_box = QGroupBox("Seabed for datum-relative horizons")
        form = QFormLayout(self.datum_box)
        self.datum = QLineEdit()
        self.datum.setPlaceholderText("e.g. MSL or LAT — the same datum as the seabed")
        self.seabed = QComboBox()
        self.seabed.addItem("Plan's sampled bathymetry", None)
        self.datum_confirm = QCheckBox("I have checked that the horizon and seabed use the same vertical datum")
        form.addRow("Datum:", self.datum)
        form.addRow("Seabed reference:", self.seabed)
        form.addRow(self.datum_confirm)
        coords_layout.addWidget(self.datum_box)
        note = QLabel("KP = origin KP ± (X − drawing origin) × X units × scale.\n"
                      "Depth = origin depth ± (Y − drawing origin) × Y units ÷ exaggeration.\n"
                      "For a datum reference, seabed depth is then subtracted at each KP. Missing coverage stays blank.")
        note.setWordWrap(True)
        coords_layout.addWidget(note)
        coords_layout.addStretch()
        self.tabs.addTab(coords, "2. Coordinates")

        self.kp_ref = KpReferenceWidget(model, dock, self)
        self.tabs.addTab(self.kp_ref, "3. KP reference")
        self.preview_plot = GroundModelPlot()
        self.tabs.addTab(self.preview_plot, "4. Preview")
        self.message = QPlainTextEdit()
        self.message.setReadOnly(True)
        self.message.setMaximumHeight(105)
        self.message.setPlainText("Open a DXF, select layers and check coordinates, then Preview.")
        layout.addWidget(self.message)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        self.cancel_work = QPushButton("Cancel loading")
        self.cancel_work.clicked.connect(self._cancel_work)
        self.cancel_work.hide()
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.cancel_work)
        layout.addLayout(progress_row)
        buttons_row = QHBoxLayout()
        preview = self.preview_button = QPushButton("Preview")
        preview.clicked.connect(self._preview)
        buttons_row.addWidget(preview)
        buttons_row.addStretch()
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        self.import_button = buttons.button(BUTTON_BOX_OK)
        self.import_button.setText("Import")
        self.import_button.setEnabled(False)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        buttons_row.addWidget(buttons)
        layout.addLayout(buttons_row)
        for control in self.findChildren(QDoubleSpinBox):
            control.valueChanged.connect(self._invalidate)
        for control in self.findChildren(QComboBox):
            control.currentIndexChanged.connect(self._invalidate)
        for control in (self.reverse, self.positive_up, self.datum_confirm):
            control.toggled.connect(self._invalidate)
        self.datum.textChanged.connect(self._invalidate)
        self.kp_ref.anchor_edit.textChanged.connect(self._invalidate)
        self.kp_ref.group.buttonClicked.connect(self._invalidate)
        self.vertical_ref.currentIndexChanged.connect(self._reference_changed)
        self._reference_changed()
        model.planChanged.connect(self._check_plan)
        QgsProject.instance().cleared.connect(self.reject)

    def _check_plan(self):
        if self.model.plan_id != self._plan_id or route_reference(self.model) != self._route:
            self.reject()

    def done(self, result):
        self._closed = True
        self._cancel_work()
        super().done(result)

    def _cancel_work(self):
        if self._task is not None:
            self._task.cancel()
            self.cancel_work.setEnabled(False)
            self.message.setPlainText("Cancelling… No changes have been saved.")

    def _busy(self, busy):
        self.tabs.setEnabled(not busy)
        self.browse_button.setEnabled(not busy)
        self.preview_button.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.cancel_work.setVisible(busy)
        self.cancel_work.setEnabled(busy)
        if busy:
            self.progress.setValue(0)
            self.import_button.setEnabled(False)

    def _start_task(self, label, operation, kind):
        if self._task is not None or self._closed:
            return
        self._task_kind = kind
        task = self._task = HorizonTask(label, operation)
        task.completed.connect(self._completed)
        task.progressChanged.connect(self._progress_changed)
        task.message.connect(self._progress_message)
        self.destroyed.connect(task.owner_destroyed)
        self._busy(True)
        self.message.setPlainText(label)
        QgsApplication.taskManager().addTask(task)

    def _progress_changed(self, value):
        if self.sender() is self._task and not self._closed:
            self.progress.setValue(round(value))

    def _progress_message(self, message):
        if self.sender() is self._task and not self._closed and self.cancel_work.isEnabled():
            self.message.setPlainText(message)

    def _completed(self, result, error, cancelled):
        if self.sender() is not self._task:
            return
        self._task = None
        if self._closed:
            return
        self._busy(False)
        self._check_plan()
        if self._closed:
            return
        if cancelled or error:
            self.message.setPlainText("Cancelled. No changes have been saved." if cancelled else error)
            return
        if self._task_kind == "read":
            self._loaded(result)
        else:
            self._show_preview(result)

    def _reference_changed(self, *_args):
        self.datum_box.setEnabled(self.vertical_ref.currentData() == "datum")

    def _invalidate(self, *_args):
        if self._task is not None:
            self._cancel_work()
        self.rows = []
        self.annotations = []
        self.import_button.setEnabled(False)
        if not self._loading:
            self.message.setPlainText("Settings changed — Preview again before importing.")
            self.preview_plot.set_horizons([])
            self.preview_plot.set_annotations([])

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import interpreted horizons", "", "DXF section (*.dxf)")
        if path:
            self.load_file(path)

    def load_file(self, path):
        if self._task is not None or self._closed:
            return
        self._invalidate()
        self.drawing = None
        self.layers.clear()
        self.label_layers.clear()
        self._draw_raw()
        self.path.setText(path)
        self.cancel_work.setText("Cancel loading")
        self._start_task("Reading DXF…", lambda progress: horizons.read_dxf(path, progress), "read")

    def _loaded(self, drawing):
        self.drawing = drawing
        self._loading = True
        self.seabed.clear()
        self.seabed.addItem("Plan's sampled bathymetry", None)
        for name, entities in sorted(drawing["layers"].items()):
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | ITEM_FLAG_USER_CHECKABLE)
            item.setCheckState(CHECK_STATE_UNCHECKED)
            item.setToolTip(f"{len(entities)} separate segment(s), {sum(len(e['points']) for e in entities):,} vertices")
            self.layers.addItem(item)
            self.seabed.addItem(f"DXF layer: {name}", name)
        for name, labels in sorted(drawing.get("annotations", {}).items()):
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | ITEM_FLAG_USER_CHECKABLE)
            item.setCheckState(CHECK_STATE_UNCHECKED)
            item.setToolTip(f"{len(labels):,} annotation(s). Display only; no soil interpretation.")
            self.label_layers.addItem(item)
        factor = horizons.DXF_UNITS.get(drawing["units"], 1.0)
        for control in (self.x_units, self.y_units):
            control.setCurrentIndex(max(0, control.findData(factor)))
        hint = (f"Header units suggest {self.x_units.currentText().lower()}; check each axis."
                if drawing["units"] in horizons.DXF_UNITS else "Drawing units unspecified/unsupported; set both axes explicitly.")
        self.file_note.setText(f"{len(drawing['layers'])} supported layers, {drawing['vertex_count']:,} vertices. "
                               f"{len(drawing.get('annotations', {}))} text layer(s). {hint} Tick layers to import.")
        self._loading = False
        self.message.setPlainText("\n".join(drawing["warnings"]) or "Select horizon layers, then check coordinates and Preview.")
        self._draw_raw()
        self.tabs.setCurrentIndex(0)

    def _selection_changed(self, *_args):
        self._invalidate()
        if not self._loading:
            self._draw_raw()

    def _selected(self):
        return [self.layers.item(i).text() for i in range(self.layers.count())
                if self.layers.item(i).checkState() == CHECK_STATE_CHECKED]

    def _selected_labels(self):
        return [self.label_layers.item(i).text() for i in range(self.label_layers.count())
                if self.import_labels.isChecked() and self.label_layers.item(i).checkState() == CHECK_STATE_CHECKED]

    def _draw_raw(self):
        # Keep and reuse the items (QGIS 3's Qt5 can fail on scene removal).
        for item in self._raw_items:
            item.setData([], [])
            item.hide()
        raw_groups = []
        if self.drawing:
            for name in self._selected_labels():
                raw_groups.append(dict(labels=[dict(label, kp=label["point"][0], depth=label["point"][1])
                                                for label in self.drawing["annotations"][name]]))
            selected = set(self._selected())
            for i, (name, entities) in enumerate(sorted(self.drawing["layers"].items())):
                colour = horizons.COLOURS[i % len(horizons.COLOURS)] if name in selected else "#cccccc"
                x, y = horizons.plot_arrays([e["points"] for e in entities])
                if i == len(self._raw_items):
                    self._raw_items.append(self.raw_plot.plot([], [], connect="finite"))
                item = self._raw_items[i]
                # Thin, non-antialiased lines avoid Qt stroking hundreds of
                # thousands of dense bends; no vertices or gaps are removed.
                item.setPen(pg.mkPen(colour, width=1 if len(x) > 10000 else 1.5))
                item.setData(x, y, connect="finite", antialias=False)
                item.show()
            self.raw_plot.autoRange()
            if raw_groups:
                points = [label for group in raw_groups for label in group["labels"]]
                xs, ys = [p["kp"] for p in points], [p["depth"] for p in points]
                if self.drawing["layers"]:
                    xr, yr = self.raw_plot.viewRange()
                    xs.extend(xr)
                    ys.extend(yr)
                self.raw_plot.setXRange(min(xs), max(xs), padding=0.05)
                self.raw_plot.setYRange(min(ys), max(ys), padding=0.05)
        self.raw_annotations.set_groups(raw_groups)

    def _options(self):
        return horizons.ImportOptions(
            x_unit_m=self.x_units.currentData(), x_scale=self.x_scale.value(),
            x_origin=self.x_origin.value(), kp_origin=self.kp_origin.value(), reverse_x=self.reverse.isChecked(),
            y_unit_m=self.y_units.currentData(), y_origin=self.y_origin.value(), depth_offset_m=self.y_offset.value(),
            exaggeration=self.exaggeration.value(), positive_up=self.positive_up.isChecked(),
            reference=self.vertical_ref.currentData(), datum=self.datum.text().strip())

    def _preview(self):
        if self._task is not None or self._closed:
            return
        self._invalidate()
        try:
            if self.model.plan_id != self._plan_id or route_reference(self.model) != self._route:
                raise ValueError("The active plan or route changed. Close this dialog and reopen the importer.")
            if not self.drawing:
                raise ValueError("Open a supported DXF section first.")
            options = self._options()
            kp_map = self.kp_ref.build_map()
            reference = None
            reference_entities = None
            source = {"file": os.path.basename(self.path.text()), "imported_utc": schema.utc_now_iso(),
                      "route": self._route, "source_rpl": self.kp_ref.source_label(),
                      "import_id": schema.new_id(), "sha256": self.drawing["sha256"],
                      "size_bytes": self.drawing["size_bytes"]}
            if options.reference == "datum":
                if not self.datum_confirm.isChecked():
                    raise ValueError("Check and confirm the shared vertical datum before converting depths.")
                layer = self.seabed.currentData()
                source["seabed"] = layer or "Plan bathymetry"
                if layer is not None:
                    reference_entities = self.drawing["layers"][layer]
                else:
                    profile = self.model.bathy_profile
                    if profile is None or self.model.profile_stale_reasons():
                        raise ValueError("Sample current plan bathymetry first, or select a seabed layer in the DXF.")
                    # Snapshot only the arrays consumed by the worker.
                    from types import SimpleNamespace
                    reference = SimpleNamespace(kps=list(profile.kps), depths=list(profile.depths),
                                                source_ids=list(profile.source_ids or []))
                    source["profile_sampled_utc"] = profile.sampled_utc
            drawing, selected = self.drawing, self._selected()
            selected_labels = self._selected_labels()
            if not selected and not selected_labels:
                raise ValueError("Tick at least one horizon layer or enable and select a text layer first.")
            # Captures are plain geometry/numeric values, never self or QObjects.
            def convert(progress):
                ref = reference
                def reference_progress(_percent, _message):
                    progress(None, "Preparing seabed reference…")
                if reference_entities is not None:
                    anchor_xs = [a[0] for a in kp_map.anchors]
                    ref = [horizons.remap_segment(options.transform(e["points"], reference_progress),
                                                 kp_map, reference_progress, anchor_xs)
                           for e in reference_entities]
                elif ref is not None:
                    ref = horizons.reference_segments(ref, reference_progress)
                def horizon_progress(percent, message):
                    progress(None if percent is None else percent * (0.85 if selected_labels else 1), message)
                def label_progress(percent, message):
                    progress(85 + percent * 0.15 if selected else percent, message)
                rows, warnings = (horizons.build_horizons(drawing, selected, options, kp_map, ref, source, horizon_progress)
                                  if selected else ([], list(drawing["warnings"])))
                annotations, notices = dxf_annotations.build(
                    drawing, selected_labels, options, kp_map, ref, source, label_progress)
                if not rows and not annotations:
                    raise ValueError("No usable horizons or annotations. " + "\n".join(warnings + notices))
                return rows, annotations, warnings + notices

            self._preview_context = source, kp_map.diagnostics.summary()
            self.cancel_work.setText("Cancel conversion")
            self._start_task("Converting horizons…", convert, "convert")
        except (ValueError, OSError) as exc:
            self.message.setPlainText(str(exc))

    def _show_preview(self, result):
        source, diagnostics = self._preview_context
        if source.get("profile_sampled_utc") is not None and (
                self.model.bathy_profile is None or self.model.profile_stale_reasons()
                or self.model.bathy_profile.sampled_utc != source["profile_sampled_utc"]):
            self.message.setPlainText("Seabed profile changed during conversion. Preview again.")
            return
        rows, annotations, warnings = result
        endpoints = [kp for row in rows for s in row["segments"] for kp in (s[0][0], s[-1][0])]
        endpoints.extend(label["kp"] for group in annotations for label in group["labels"])
        lo, hi = min(endpoints), max(endpoints)
        scope_lo = float(self.model.plan.get("scope_start_kp") or 0)
        scope_hi = float(self.model.plan.get("scope_end_kp") or 0)
        if scope_hi > scope_lo and (lo < scope_lo or hi > scope_hi):
            warnings.append("Some horizon KPs are outside the plan scope; check units, origin and KP reference.")
        self.preview_plot.set_horizons(rows)
        self.preview_plot.set_annotations(annotations)
        self.preview_plot.set_target_runs(self.model.target_runs())
        self.preview_plot.plot.setXRange(lo, hi, padding=0.03)
        self.message.setPlainText(f"{len(rows)} horizon(s), KP {lo:.6f}–{hi:.6f}. Depths are metres below seabed.\n"
                                  + diagnostics + "\n" + "\n".join(warnings))
        if annotations:
            self.message.appendPlainText(f"{sum(len(g['labels']) for g in annotations)} annotation(s). "
                "Plain upright text at converted anchor points; CAD fonts/rotation are not reproduced. No soil interpretation.")
        self.rows, self.annotations = rows, annotations
        self.import_button.setEnabled(True)
        self.tabs.setCurrentIndex(3)

    def _accept(self):
        self._check_plan()
        if self._closed or self._task is not None or not (self.rows or self.annotations):
            return
        # Control changes invalidate rows. Check external reference changes too,
        # without repeating a potentially long conversion on the GUI thread.
        first = (self.rows + self.annotations)[0]
        notice = reference_notice(first, self.model)
        if (first["source"].get("profile_sampled_utc") is not None
                and self.model.profile_stale_reasons()):
            notice = "Seabed profile is stale. Sample it again before importing."
        if notice:
            self._invalidate()
            self.message.setPlainText(notice)
            return
        self.accept()


class HorizonsDialog(QDialog):
    """Working copy: import/manage/edit, save once through the existing plan log."""

    def __init__(self, model, dock, parent=None):
        super().__init__(parent)
        self.model, self.dock = model, dock
        # All edits replace segments; immutable vertex arrays need not be copied
        # several times just to open a large imported section.
        self._initial = horizons.plan_rows(model.plan)
        self.rows = [dict(row) for row in self._initial]
        self._annotations_initial = dxf_annotations.plan_state(model.plan)
        self.annotations = [dict(group) for group in self._annotations_initial["groups"]]
        self._plan_id, self._route = model.plan_id, route_reference(model)
        self._loading = False
        self.setWindowTitle("Sub-bottom horizons and labels")
        self.resize(1000, 700)
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        for label, callback in (("Add DXF…", self._import), ("Remove selected", self._remove),
                                ("Colour…", self._colour), ("Edit vertices…", self._edit_vertices)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
            if label == "Edit vertices…":
                self.edit_vertices_button = button
        toolbar.addStretch()
        self.show_labels = QCheckBox("Show labels")
        self.show_labels.setChecked(self._annotations_initial.get("visible", True))
        self.show_labels.setToolTip("Show imported DXF annotations. Saved with this plan; no soil interpretation.")
        self.show_labels.toggled.connect(self._render)
        toolbar.addWidget(self.show_labels)
        layout.addLayout(toolbar)
        split = QSplitter(getattr(Qt, "Orientation", Qt).Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._selected)
        self.list.itemChanged.connect(self._visibility)
        left_layout.addWidget(self.list, 1)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Selected horizon name")
        self.name.editingFinished.connect(self._rename)
        left_layout.addWidget(self.name)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(getattr(Qt, "TextFormat", Qt).PlainText)
        left_layout.addWidget(self.details)
        split.addWidget(left)
        self.plot = GroundModelPlot()
        split.addWidget(self.plot)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        hint = QLabel("Add DXF appends horizons and labels; repeat to combine files, with coordinates checked for each file. "
                      "Tick horizons to show them over any Ground Model display. Edits are saved with Apply; "
                      "Cancel discards this dialog's changes. Undo saved edits in Review & Export → change log.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.button(BUTTON_BOX_OK).setText("Apply")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._rebuild()
        model.planChanged.connect(self._check_plan)
        QgsProject.instance().cleared.connect(self.reject)

    def _check_plan(self):
        if self.model.plan_id != self._plan_id or route_reference(self.model) != self._route:
            self.reject()

    def done(self, result):
        for dialog in self.findChildren(HorizonImportDialog):
            dialog.reject()
        super().done(result)

    def _rebuild(self, index=0):
        self._loading = True
        self.list.clear()
        for row in self.rows + self.annotations:
            item = QListWidgetItem(("Labels — " if "labels" in row else "") + row["name"])
            item.setFlags(item.flags() | ITEM_FLAG_USER_CHECKABLE)
            item.setCheckState(CHECK_STATE_CHECKED if row.get("visible", True) else CHECK_STATE_UNCHECKED)
            item.setForeground(QColor(row["colour"]))
            self.list.addItem(item)
        self._loading = False
        self.list.setCurrentRow(min(index, len(self.rows) + len(self.annotations) - 1))
        self.show_labels.setEnabled(bool(self.annotations))
        self._selected()
        self._render()

    def _selected_row(self):
        index = self.list.currentRow()
        entries = self.rows + self.annotations
        return entries[index] if 0 <= index < len(entries) else None

    def _selected(self, *_args):
        row = self._selected_row()
        self.edit_vertices_button.setEnabled(row is not None and "labels" not in row)
        self.name.setEnabled(row is not None)
        self.name.setText(row["name"] if row else "")
        if row:
            source = row.get("source") or {}
            detail = (f"{len(row['labels'])} annotation(s); display only" if "labels" in row
                      else f"{len(row['segments'])} segment(s)")
            self.details.setText(f"{source.get('file', '')}\n{detail}\n" + reference_notice(row, self.model))
        else:
            self.details.setText("Import a DXF section to add interpreted horizons.")

    def _render(self):
        self.plot.set_units(self.model.ground_units, self.model.ground_classes)
        route = route_reference(self.model)
        self.plot.set_horizons([row for row in self.rows if (row.get("source") or {}).get("route") == route])
        self.plot.set_annotations([row for row in self.annotations
                                   if (row.get("source") or {}).get("route") == route]
                                  if self.show_labels.isChecked() else [])
        self.plot.set_target_runs(self.model.target_runs())
        scope = self.model.gen_params().scope
        if scope.length_km > 0:
            self.plot.plot.setXRange(scope.start_km, scope.end_km, padding=0.02)

    def _visibility(self, item):
        if not self._loading:
            (self.rows + self.annotations)[self.list.row(item)]["visible"] = item.checkState() == CHECK_STATE_CHECKED
            self._render()

    def _rename(self):
        index = self.list.currentRow()
        row = self._selected_row()
        if row is not None and self.name.text().strip():
            row["name"] = self.name.text().strip()
            self._loading = True
            self.list.item(index).setText(("Labels — " if "labels" in row else "") + row["name"])
            self._loading = False
            self._render()

    def _import(self):
        dialog = HorizonImportDialog(self.model, self.dock, self)
        try:
            if qt_exec(dialog) == DIALOG_ACCEPTED:
                combined = self.rows + dialog.rows
                combined_annotations = self.annotations + dialog.annotations
                try:
                    horizons.validate_rows(combined)
                    dxf_annotations.validate(combined_annotations)
                except ValueError as exc:
                    QMessageBox.warning(self, "Horizon import", str(exc))
                    return
                index = len(self.rows) if dialog.rows else len(combined) + len(self.annotations)
                self.rows, self.annotations = combined, combined_annotations
                self._rebuild(index)
        finally:
            dialog.deleteLater()

    def _remove(self):
        index = self.list.currentRow()
        if index >= 0:
            if index < len(self.rows):
                self.rows.pop(index)
            else:
                self.annotations.pop(index - len(self.rows))
            self._rebuild(index)

    def _colour(self):
        index = self.list.currentRow()
        row = self._selected_row()
        if row is not None:
            colour = QColorDialog.getColor(QColor(row["colour"]), self, "Overlay colour")
            if colour.isValid():
                row["colour"] = colour.name()
                self._rebuild(index)

    def _edit_vertices(self):
        index = self.list.currentRow()
        if index < 0 or index >= len(self.rows):
            return
        row = self.rows[index]
        if sum(len(s) for s in row["segments"]) > horizons.MAX_TEXT_EDIT_POINTS:
            QMessageBox.information(self, "Edit horizon", "This horizon is too large for the text editor. "
                                    "All imported vertices are retained. Edit the source DXF and re-import it to change picks.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Edit horizon — {row['name']}")
        dialog.resize(550, 550)
        layout = QVBoxLayout(dialog)
        label = QLabel("One KP (km), depth below seabed (m) pair per line.\nBlank lines separate segments. Original DXF vertices remain saved.")
        label.setWordWrap(True)
        layout.addWidget(label)
        text = QPlainTextEdit()
        text.setPlainText("\n\n".join("\n".join(f"{x:.12g}, {y:.12g}" for x, y in s) for s in row["segments"]))
        layout.addWidget(text, 1)
        error = QLabel()
        error.setWordWrap(True)
        layout.addWidget(error)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        layout.addWidget(buttons)

        def apply():
            try:
                segments = horizons.parse_vertices(text.toPlainText())
                candidate = dict(row, segments=segments)
                horizons.validate_rows(self.rows[:index] + [candidate] + self.rows[index + 1:])
            except ValueError as exc:
                error.setText(str(exc))
                return
            self.rows[index] = candidate
            dialog.accept()

        buttons.accepted.connect(apply)
        buttons.rejected.connect(dialog.reject)
        if qt_exec(dialog) == DIALOG_ACCEPTED:
            self._rebuild(index)

    def _save(self):
        self._rename()
        try:
            if self.model.plan_id != self._plan_id or route_reference(self.model) != self._route:
                raise ValueError("The active plan or route changed; close and reopen Horizons before saving.")
            if horizons.plan_rows(self.model.plan) != self._initial:
                raise ValueError("Saved horizons changed while this dialog was open; reopen it before editing.")
            if dxf_annotations.plan_state(self.model.plan) != self._annotations_initial:
                raise ValueError("Saved annotations changed while this dialog was open; reopen it before editing.")
            horizons.validate_rows(self.rows)
            dxf_annotations.validate(self.annotations)
            annotations = dict(self._annotations_initial, groups=self.annotations, visible=self.show_labels.isChecked())
            updates = {}
            if self.rows != self._initial:
                updates[horizons.PARAMS_KEY] = {"version": 1, "rows": self.rows}
            if annotations != self._annotations_initial:
                updates[dxf_annotations.PARAMS_KEY] = annotations
            if updates and not self.model.update_gen_params(
                    updates, reason="Edit sub-bottom horizons and annotations", stale=False):
                return
        except ValueError as exc:
            QMessageBox.warning(self, "Save horizons", str(exc))
            return
        self.accept()
