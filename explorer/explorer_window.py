# -*- coding: utf-8 -*-
"""The standalone Cable Lay Data Explorer window.

A ``QMainWindow`` (top-level, so it can live on a second monitor alongside the
QGIS map) whose central widget is a virtual data table, with dockable QC and
plot panels. It loads a cable-lay data layer into a :class:`LayDataset` once and
shares that dataset across all panels; clicking records / findings highlights
them on the QGIS map canvas via :class:`MapSyncController`.
"""

from __future__ import annotations

import json
import math
import os
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

import numpy as np

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QGuiApplication, QKeySequence
from qgis.PyQt.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QTabWidget,
    QToolBar,
    QWidget,
)
from qgis.core import QgsApplication, QgsProject

from ..qgis_compat import QAction
from .layer_select_dialog import LayerSelectDialog
from .layer_loader import LayerLoadTask, build_spec
from .map_sync import MapSyncController
from .panels.data_table_panel import DataTablePanel
from .panels.inspection_panel import InspectionPanel
from .panels.manage_panel import ManagePanel
from .panels.plot_panel import PlotPanel
from .panels.processing_panel import ProcessingPanel
from .panels.qc_panel import QcPanel
from ..laydata import LayDataset

_WT = getattr(Qt, "WindowType", Qt)
_WINDOW_FLAG = getattr(_WT, "Window")
_HINT_MIN = getattr(_WT, "WindowMinimizeButtonHint")
_HINT_MAX = getattr(_WT, "WindowMaximizeButtonHint")
_HINT_CLOSE = getattr(_WT, "WindowCloseButtonHint")
_WAIT_CURSOR = getattr(getattr(Qt, "CursorShape", Qt), "WaitCursor")
_DOCK_RIGHT = getattr(getattr(Qt, "DockWidgetArea", Qt), "RightDockWidgetArea")
_DOCK_BOTTOM = getattr(getattr(Qt, "DockWidgetArea", Qt), "BottomDockWidgetArea")
_ORIENT_VERTICAL = getattr(getattr(Qt, "Orientation", Qt), "Vertical")
_ORIENT_HORIZONTAL = getattr(getattr(Qt, "Orientation", Qt), "Horizontal")
_WINDOW_MODAL = getattr(getattr(Qt, "WindowModality", Qt), "WindowModal")

_SETTINGS_GROUP = "SubseaCableTools/CableLayExplorer"
_PLOT_LAYOUTS = ("Tabbed", "Rows", "Columns", "Grid")
_X_MODES = ("(individual)", "Time", "KP", "Record order")


class CableLayExplorerWindow(QMainWindow):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.setWindowFlags(_WINDOW_FLAG)
        self.setWindowTitle("Cable Lay Data Explorer")
        self.setObjectName("CableLayExplorerWindow")
        self.setDockNestingEnabled(True)

        self.iface = iface
        self.canvas = iface.mapCanvas()
        # Unified registry of loaded datasets keyed by QGIS layer id (load order
        # preserved). One layer is "active" at a time and drives the table, QC
        # and Inspection panels; every loaded layer is available to the plots.
        self._datasets: "OrderedDict[str, LayDataset]" = OrderedDict()
        # "Active rows only": an in-memory view (record_status active/empty)
        # served to the table, plots, QC and Inspection panels instead of the
        # full dataset. Views are derived lazily per layer and cached against
        # the full dataset object they came from; the Manage panel always
        # gets the full dataset so standby/excluded rows stay manageable.
        self._active_only = False
        self._views: Dict[str, Tuple[LayDataset, LayDataset]] = {}
        self._active_layer_id: Optional[str] = None
        self._load_task = None
        self._load_progress = None
        self._pending_order: Optional[List[str]] = None
        self._pending_active: Optional[str] = None
        self.map_sync = MapSyncController(self.canvas)
        self._plot_docks: List[QDockWidget] = []
        self._plot_count = 0
        self._syncing = False
        self._sync_crosshair = True
        self._last_hover_row = None
        self._current_row: Optional[int] = None  # last highlighted / stepped record
        self._selection_layer = None  # project layer whose selectionChanged we follow
        self._kp_field_cache: dict = {}
        self._plot_layout_mode = "Tabbed"
        self._lock_x = False
        self.settings = QSettings()

        self._build_toolbar()

        self.table_panel = DataTablePanel(self)
        self.setCentralWidget(self.table_panel)

        # Analysis dock: QC / Inspection / Processing / Manage tabs.
        self.qc_panel = QcPanel(self)
        self.inspection_panel = InspectionPanel(self)
        self.processing_panel = ProcessingPanel(self)
        self.manage_panel = ManagePanel(self)
        self.analysis_tabs = QTabWidget()
        self.analysis_tabs.addTab(self.manage_panel, "Manage")
        self.analysis_tabs.addTab(self.qc_panel, "QC")
        self.analysis_tabs.addTab(self.inspection_panel, "Inspection")
        self.analysis_tabs.addTab(self.processing_panel, "Processing")
        self.qc_dock = QDockWidget("Analysis", self)
        self.qc_dock.setObjectName("AnalysisDock")
        self.qc_dock.setWidget(self.analysis_tabs)
        self.addDockWidget(_DOCK_RIGHT, self.qc_dock)

        self._restore_plots()
        self._build_shortcuts()
        self.statusBar().showMessage("")

        self._fit_initial_size()
        self._restore_state()
        self.populate_layers()
        self._restore_loaded_layers()

    # -- active dataset / layer accessors ---------------------------------
    @property
    def dataset(self) -> Optional[LayDataset]:
        """The active layer's dataset as the panels should see it (view-aware)."""
        return self._view_for(self._active_layer_id)

    @property
    def full_dataset(self) -> Optional[LayDataset]:
        """The active layer's complete dataset, every record status included."""
        if self._active_layer_id is None:
            return None
        return self._datasets.get(self._active_layer_id)

    def _view_for(self, layer_id: Optional[str]) -> Optional[LayDataset]:
        if layer_id is None:
            return None
        full = self._datasets.get(layer_id)
        if full is None or not self._active_only:
            return full
        cached = self._views.get(layer_id)
        if cached is not None and cached[0] is full:
            return cached[1]
        view = full.active_view()
        self._views[layer_id] = (full, view)
        return view

    def active_only(self) -> bool:
        return self._active_only

    def set_active_only(self, enabled: bool) -> None:
        """Toggle the in-memory active-rows view (also drives the toolbar action)."""
        enabled = bool(enabled)
        if self.active_only_action.isChecked() != enabled:
            self.active_only_action.setChecked(enabled)  # re-enters via toggled
            return
        if enabled == self._active_only:
            return
        self._active_only = enabled
        self._views.clear()
        self._refresh_all()

    def _on_active_only_toggled(self, checked: bool) -> None:
        self.set_active_only(checked)

    @property
    def layer(self):
        if self._active_layer_id is None:
            return None
        return QgsProject.instance().mapLayer(self._active_layer_id)


    # -- construction ------------------------------------------------------
    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Explorer", self)
        toolbar.setObjectName("ExplorerToolbar")
        self.addToolBar(toolbar)

        choose_action = QAction("Choose layers\u2026", self)
        choose_action.setToolTip(
            "Select which project layers to load into the Explorer. Every loaded "
            "layer is available to the plots; one of them is the active layer."
        )
        choose_action.triggered.connect(self._choose_layers)
        toolbar.addAction(choose_action)

        toolbar.addWidget(QLabel(" Active layer: "))
        self.layer_combo = QComboBox()
        self.layer_combo.setMinimumWidth(240)
        self.layer_combo.setToolTip(
            "The loaded layer that the table, Manage, QC and Inspection panels "
            "work on, and the default source for new plot series. Plots can also "
            "add series from any other loaded layer."
        )
        self.layer_combo.currentIndexChanged.connect(self._on_table_layer_changed)
        toolbar.addWidget(self.layer_combo)

        reload_action = QAction("Reload data", self)
        reload_action.setToolTip("Re-read every loaded layer from the project")
        reload_action.triggered.connect(self.reload_dataset)
        toolbar.addAction(reload_action)

        self.active_only_action = QAction("Active rows only", self)
        self.active_only_action.setCheckable(True)
        self.active_only_action.setChecked(False)
        self.active_only_action.setToolTip(
            "Show only rows whose record_status is active (or empty) in the "
            "table, plots, QC and Inspection - in memory, nothing is written. "
            "Standby / excluded rows from the Manage tab's curation are hidden."
        )
        self.active_only_action.toggled.connect(self._on_active_only_toggled)
        toolbar.addAction(self.active_only_action)

        add_plot_action = QAction("Add plot panel", self)
        add_plot_action.triggered.connect(lambda: self.add_plot_panel())
        toolbar.addAction(add_plot_action)

        toolbar.addWidget(QLabel(" Layout: "))
        self.layout_combo = QComboBox()
        self.layout_combo.addItems(list(_PLOT_LAYOUTS))
        self.layout_combo.currentTextChanged.connect(self.set_plot_layout)
        toolbar.addWidget(self.layout_combo)

        toolbar.addWidget(QLabel(" All X: "))
        self.xmode_combo = QComboBox()
        self.xmode_combo.addItems(list(_X_MODES))
        self.xmode_combo.currentTextChanged.connect(self._on_all_x_changed)
        toolbar.addWidget(self.xmode_combo)

        self.lock_x_action = QAction("Lock X axes", self)
        self.lock_x_action.setCheckable(True)
        self.lock_x_action.setChecked(False)
        self.lock_x_action.toggled.connect(self._on_lock_x_toggled)
        toolbar.addAction(self.lock_x_action)

        self.sync_action = QAction("Sync crosshair", self)
        self.sync_action.setCheckable(True)
        self.sync_action.setChecked(True)
        self.sync_action.setToolTip(
            "Hovering a plot moves the crosshair on every other plot and a blue "
            "marker on the map to the same record. Click a plot to highlight the "
            "record (red) in the table and map; double-click or right-click to "
            "go to it (pans the map, centres the plots)."
        )
        self.sync_action.toggled.connect(self._on_sync_toggled)
        toolbar.addAction(self.sync_action)

        clear_action = QAction("Clear highlight", self)
        clear_action.triggered.connect(self._clear_highlight)
        toolbar.addAction(clear_action)

    def _build_shortcuts(self) -> None:
        """Window-wide keys: Ctrl+Left/Right step records, Esc clears."""
        self.step_back_action = QAction("Previous record", self)
        self.step_back_action.setShortcut(QKeySequence("Ctrl+Left"))
        self.step_back_action.triggered.connect(self._step_back)
        self.addAction(self.step_back_action)
        self.step_forward_action = QAction("Next record", self)
        self.step_forward_action.setShortcut(QKeySequence("Ctrl+Right"))
        self.step_forward_action.triggered.connect(self._step_forward)
        self.addAction(self.step_forward_action)
        self.escape_action = QAction("Clear highlight and selection", self)
        self.escape_action.setShortcut(QKeySequence("Esc"))
        self.escape_action.triggered.connect(self.escape)
        self.addAction(self.escape_action)

    def _step_back(self) -> None:
        self.step_record(-1)

    def _step_forward(self) -> None:
        self.step_record(1)

    def _fit_initial_size(self) -> None:
        try:
            screen = QGuiApplication.primaryScreen()
            geometry = screen.availableGeometry()
            self.resize(int(geometry.width() * 0.6), int(geometry.height() * 0.7))
        except Exception:
            self.resize(1100, 750)

    # -- layer / dataset registry -----------------------------------------
    def populate_layers(self) -> None:
        """Refresh the active-layer combo from the loaded-dataset registry."""
        current = self._active_layer_id
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        if not self._datasets:
            self.layer_combo.addItem("(no layers loaded)", None)
        for layer_id in self._datasets:
            layer = QgsProject.instance().mapLayer(layer_id)
            name = layer.name() if layer is not None else layer_id
            self.layer_combo.addItem(name, layer_id)
        self.layer_combo.blockSignals(False)
        if current is not None:
            index = self.layer_combo.findData(current)
            if index >= 0:
                self.layer_combo.setCurrentIndex(index)

    def _choose_layers(self) -> None:
        loaded = list(self._datasets.keys())
        dialog = LayerSelectDialog(self, preselected=loaded)
        if not dialog.exec_qt():
            return
        self.set_loaded_layers(dialog.selected_layer_ids())

    def set_loaded_layers(self, layer_ids: list) -> None:
        """Load newly-selected layers (in the background) and unload the rest."""
        wanted = [i for i in layer_ids if QgsProject.instance().mapLayer(i) is not None]
        # Unloading is cheap, so do it immediately on the main thread.
        for layer_id in list(self._datasets.keys()):
            if layer_id not in wanted:
                del self._datasets[layer_id]
        to_load = [i for i in wanted if i not in self._datasets]
        if not to_load:
            self._apply_loaded_order(wanted)
            return
        if self._load_task is not None:
            # A load is already running; ignore re-entrant requests.
            return
        self._start_background_load(to_load, wanted)

    def _apply_loaded_order(self, wanted: list) -> None:
        """Reorder the registry to the selection and refresh every panel."""
        self._datasets = OrderedDict(
            (i, self._datasets[i]) for i in wanted if i in self._datasets
        )
        preferred = self._pending_active
        self._pending_active = None
        if preferred and preferred in self._datasets:
            self._active_layer_id = preferred
        elif self._active_layer_id not in self._datasets:
            self._active_layer_id = next(iter(self._datasets), None)
        self.populate_layers()
        self._refresh_all()

    def _start_background_load(self, to_load: list, wanted: list) -> None:
        """Kick off a cancellable background load with a progress dialog."""
        specs = []
        try:
            for layer_id in to_load:
                layer = QgsProject.instance().mapLayer(layer_id)
                if layer is not None and hasattr(layer, "fields"):
                    specs.append(build_spec(layer))
        except Exception:
            specs = None
        if not specs:
            # Fall back to a synchronous load if the async path is unavailable.
            self._load_layers_sync(to_load)
            self._apply_loaded_order(wanted)
            return

        self._pending_order = wanted
        progress = QProgressDialog("Loading data layers\u2026", "Cancel", 0, 100, self)
        progress.setWindowTitle("Cable Lay Data Explorer")
        progress.setWindowModality(_WINDOW_MODAL)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        self._load_progress = progress

        task = LayerLoadTask(specs)
        self._load_task = task
        task.progressChanged.connect(self._on_load_progress)
        task.taskCompleted.connect(self._on_load_completed)
        task.taskTerminated.connect(self._on_load_terminated)
        progress.canceled.connect(task.cancel)
        QgsApplication.taskManager().addTask(task)
        progress.show()

    def _load_layers_sync(self, to_load: list) -> None:
        QApplication.setOverrideCursor(_WAIT_CURSOR)
        try:
            for layer_id in to_load:
                layer = QgsProject.instance().mapLayer(layer_id)
                if layer is not None and hasattr(layer, "fields"):
                    self._datasets[layer_id] = LayDataset.from_qgis_layer(layer)
        finally:
            QApplication.restoreOverrideCursor()

    def _on_load_progress(self, value) -> None:
        if self._load_progress is not None:
            self._load_progress.setValue(int(value))

    def _on_load_completed(self) -> None:
        self._absorb_loaded_datasets()
        wanted = self._pending_order or list(self._datasets.keys())
        self._pending_order = None
        self._teardown_load_ui()
        self._apply_loaded_order(wanted)

    def _on_load_terminated(self) -> None:
        error = getattr(self._load_task, "error", None) if self._load_task is not None else None
        self._absorb_loaded_datasets()
        wanted_all = self._pending_order or list(self._datasets.keys())
        self._pending_order = None
        self._teardown_load_ui()
        # Keep only layers that actually finished loading (cancel = partial).
        wanted = [i for i in wanted_all if i in self._datasets]
        self._apply_loaded_order(wanted)
        if error:
            QMessageBox.warning(self, "Load layers", f"Could not load all layers:\n{error}")

    def _absorb_loaded_datasets(self) -> None:
        if self._load_task is not None:
            for layer_id, dataset in self._load_task.datasets.items():
                self._datasets[layer_id] = dataset

    def _teardown_load_ui(self) -> None:
        if self._load_progress is not None:
            self._load_progress.reset()
            self._load_progress.deleteLater()
            self._load_progress = None
        self._load_task = None

    def _cancel_load(self) -> None:
        """Cancel and wait for any running load (used on window close/unload)."""
        task = self._load_task
        if task is not None:
            try:
                task.progressChanged.disconnect()
                task.taskCompleted.disconnect()
                task.taskTerminated.disconnect()
            except Exception:
                pass
            try:
                task.cancel()
                task.waitForFinished(3000)
            except Exception:
                pass
        self._pending_order = None
        self._teardown_load_ui()

    def _on_table_layer_changed(self) -> None:
        layer_id = self.layer_combo.currentData()
        if layer_id and layer_id in self._datasets:
            self.set_active_layer(layer_id)

    def set_active_layer(self, layer_id: Optional[str]) -> None:
        if layer_id not in self._datasets:
            return
        self._active_layer_id = layer_id
        index = self.layer_combo.findData(layer_id)
        if index >= 0 and self.layer_combo.currentIndex() != index:
            self.layer_combo.blockSignals(True)
            self.layer_combo.setCurrentIndex(index)
            self.layer_combo.blockSignals(False)
        self._refresh_all()

    def _refresh_all(self) -> None:
        """Push the active dataset to every panel and refresh plot sources."""
        # Drop cached views whose full dataset is gone (unloaded / reloaded).
        for layer_id in list(self._views.keys()):
            cached = self._views[layer_id]
            if self._datasets.get(layer_id) is not cached[0]:
                del self._views[layer_id]
        active = self.dataset
        self.map_sync.set_layer(self.layer)
        self._connect_selection(self.layer)
        self._current_row = None
        self.table_panel.set_dataset(active)
        self.qc_panel.set_dataset(active)
        self.inspection_panel.set_dataset(active)
        self.processing_panel.set_dataset(active)
        # Manage always works on the complete dataset (every record status).
        self.manage_panel.sync_active_only(self._active_only)
        self.manage_panel.set_dataset(self.full_dataset)
        self._update_view_hint()
        for dock in self._plot_docks:
            widget = dock.widget()
            if widget is not None:
                # set_dataset already rebuilds the (multi-layer) series menu and
                # replots, so an extra refresh_sources() here would replot twice.
                widget.set_dataset(active)

    def _update_view_hint(self) -> None:
        """Window title suffix so a filtered view is never mistaken for the data."""
        title = "Cable Lay Data Explorer"
        if self._active_only:
            full, view = self.full_dataset, self.dataset
            if full is not None and view is not None and view is not full:
                title += f" - active rows only ({view.row_count:,} of {full.row_count:,})"
            else:
                title += " - active rows only"
        self.setWindowTitle(title)

    def _refresh_plot_sources(self) -> None:
        for panel in self._plot_panels():
            panel.refresh_sources()

    def plot_sources(self) -> list:
        """Return [(layer_id, layer_name, dataset)] for every loaded layer.

        The active layer is listed first so plots default to it.
        """
        ordered = []
        if self._active_layer_id in self._datasets:
            ordered.append(self._active_layer_id)
        ordered.extend(i for i in self._datasets if i != self._active_layer_id)
        sources = []
        for layer_id in ordered:
            layer = QgsProject.instance().mapLayer(layer_id)
            name = layer.name() if layer is not None else layer_id
            sources.append((layer_id, name, self._view_for(layer_id)))
        return sources

    def dataset_for(self, layer_id: Optional[str]):
        if layer_id is None:
            return self.dataset
        return self._view_for(layer_id)

    def primary_layer_id(self) -> Optional[str]:
        return self._active_layer_id

    def event_records(self) -> list:
        """Collect timestamped events from any loaded event-style layer."""
        records = []
        for layer_id, name, dataset in self.plot_sources():
            if not self._is_event_dataset(dataset, name):
                continue
            times = dataset.time_epoch
            if times is None:
                continue
            label_field = self._event_label_field(dataset)
            has_geom = dataset.has_geometry
            for row in range(dataset.row_count):
                t = times[row]
                if not np.isfinite(t):
                    continue
                if label_field is not None:
                    raw = dataset.columns[label_field][row]
                    label = "" if raw is None else str(raw)
                else:
                    label = dataset.iso_time_at(row) or ""
                records.append(
                    {
                        "layer": layer_id,
                        "row": row,
                        "time": float(t),
                        "lon": float(dataset.lon[row]) if has_geom else None,
                        "lat": float(dataset.lat[row]) if has_geom else None,
                        "label": label,
                    }
                )
        return records

    @staticmethod
    def _is_event_dataset(dataset, name: str) -> bool:
        if getattr(dataset, "source_field", None) == "event_file":
            return True
        return "event" in (name or "").lower()

    @staticmethod
    def _event_label_field(dataset) -> Optional[str]:
        skip = {"iso_time", "lat_dd", "lon_dd", "event_file"}
        keywords = ("event", "desc", "comment", "remark", "message", "text", "activity")
        candidates = [f for f in dataset.field_names if f.lower() not in skip]
        for field in candidates:
            if any(key in field.lower() for key in keywords):
                return field
        for field in candidates:
            if not dataset.is_numeric_field(field):
                return field
        return None

    def reload_dataset(self) -> None:
        """Re-read every loaded layer from the project (in the background)."""
        if not self._datasets or self._load_task is not None:
            return
        wanted = [
            i for i in self._datasets.keys()
            if QgsProject.instance().mapLayer(i) is not None
        ]
        # Preserve the active layer, then drop cached copies so they re-read.
        self._pending_active = self._active_layer_id
        for layer_id in wanted:
            self._datasets.pop(layer_id, None)
        self.set_loaded_layers(wanted)

    def add_plot_panel(self, config: Optional[dict] = None) -> "PlotPanel":
        self._plot_count += 1
        panel = PlotPanel(self)
        title = f"Plot {self._plot_count}"
        if isinstance(config, dict) and config.get("name"):
            title = config["name"]
        dock = QDockWidget(title, self)
        dock.setObjectName(f"PlotDock{self._plot_count}")
        dock.setWidget(panel)
        dock.topLevelChanged.connect(lambda floating, d=dock: self._on_dock_floated(d, floating))
        self.addDockWidget(_DOCK_BOTTOM, dock)
        self._plot_docks.append(dock)
        if isinstance(config, dict):
            panel.apply_config(config)
            if not config.get("name"):
                panel.set_display_name(title)
        else:
            panel.set_display_name(title)
        if self.dataset is not None:
            panel.set_dataset(self.dataset)
        # New plots should immediately adopt the active layout rather than
        # always defaulting to a new tab.
        self.set_plot_layout(self._plot_layout_mode)
        self._apply_x_lock()
        dock.raise_()
        return panel

    # -- plot panels: layout + crosshair sync ------------------------------
    def _plot_panels(self) -> List["PlotPanel"]:
        return [dock.widget() for dock in self._plot_docks if dock.widget() is not None]

    def set_plot_layout(self, mode: str) -> None:
        self._plot_layout_mode = mode
        docks = [d for d in self._plot_docks if not d.isFloating()]
        if not docks:
            return
        for dock in docks:
            self.addDockWidget(_DOCK_BOTTOM, dock)
        first = docks[0]
        if mode == "Tabbed":
            for dock in docks[1:]:
                self.tabifyDockWidget(first, dock)
            first.raise_()
        elif mode == "Rows":
            previous = first
            for dock in docks[1:]:
                self.splitDockWidget(previous, dock, _ORIENT_VERTICAL)
                previous = dock
        elif mode == "Columns":
            previous = first
            for dock in docks[1:]:
                self.splitDockWidget(previous, dock, _ORIENT_HORIZONTAL)
                previous = dock
        elif mode == "Grid":
            cols = max(1, int(math.ceil(math.sqrt(len(docks)))))
            row_anchors: List[QDockWidget] = []
            previous_in_row: Optional[QDockWidget] = None
            for i, dock in enumerate(docks):
                row, col = divmod(i, cols)
                if col == 0:
                    if row > 0:
                        self.splitDockWidget(row_anchors[row - 1], dock, _ORIENT_VERTICAL)
                    row_anchors.append(dock)
                    previous_in_row = dock
                else:
                    self.splitDockWidget(previous_in_row, dock, _ORIENT_HORIZONTAL)
                    previous_in_row = dock

    def _on_sync_toggled(self, checked: bool) -> None:
        self._sync_crosshair = bool(checked)
        if not checked:
            for panel in self._plot_panels():
                panel.clear_hover()
            self.map_sync.clear_hover()

    def broadcast_hover(self, source_row: int, origin=None) -> None:
        if not self._sync_crosshair:
            return
        if source_row == self._last_hover_row:
            return
        self._last_hover_row = source_row
        for panel in self._plot_panels():
            if panel is origin:
                continue
            panel.set_hover(source_row)
        self._update_map_hover(source_row)
        self._update_status_readout(source_row)

    def _update_map_hover(self, source_row: int) -> None:
        """Move the blue map marker to the hovered record (never pans)."""
        dataset = self.dataset
        if dataset is None or not dataset.has_geometry:
            self.map_sync.clear_hover()
            return
        if not (0 <= source_row < dataset.row_count):
            self.map_sync.clear_hover()
            return
        lat = dataset.lat[source_row]
        lon = dataset.lon[source_row]
        if np.isfinite(lat) and np.isfinite(lon):
            self.map_sync.hover_point(float(lon), float(lat))
        else:
            self.map_sync.clear_hover()

    # -- X axis linking + global X mode ------------------------------------
    def _on_lock_x_toggled(self, checked: bool) -> None:
        self._lock_x = bool(checked)
        self._apply_x_lock()

    def _apply_x_lock(self) -> None:
        panels = [d.widget() for d in self._plot_docks if not d.isFloating() and d.widget() is not None]
        boxes = [p.view_box() for p in panels]
        boxes = [b for b in boxes if b is not None]
        if not boxes:
            return
        anchor = boxes[0]
        for box in boxes[1:]:
            try:
                box.setXLink(anchor if self._lock_x else None)
            except Exception:
                pass

    def _on_all_x_changed(self, mode: str) -> None:
        kind = {"Time": "time", "KP": "kp", "Record order": "record"}.get(mode)
        if kind is None:
            return
        for panel in self._plot_panels():
            panel.set_x_by_kind(kind)

    def on_panel_replotted(self, panel) -> None:
        # View boxes are recreated on every replot, so re-establish the link.
        self._apply_x_lock()

    # -- renaming / floating docks -----------------------------------------
    def rename_panel(self, panel) -> None:
        dock = self._dock_for_panel(panel)
        if dock is None:
            return
        current = panel.display_name() or dock.windowTitle()
        text, ok = QInputDialog.getText(self, "Rename plot", "Plot name:", text=current or "")
        if not ok:
            return
        name = text.strip() or current
        dock.setWindowTitle(name)
        panel.set_display_name(name)
        panel.replot()

    def _dock_for_panel(self, panel) -> Optional[QDockWidget]:
        for dock in self._plot_docks:
            if dock.widget() is panel:
                return dock
        return None

    def _on_dock_floated(self, dock, floating: bool) -> None:
        if floating:
            dock.setWindowFlags(_WINDOW_FLAG | _HINT_MIN | _HINT_MAX | _HINT_CLOSE)
            dock.show()
        else:
            self._apply_x_lock()

    def toggle_float_panel(self, panel) -> None:
        dock = self._dock_for_panel(panel)
        if dock is not None:
            dock.setFloating(not dock.isFloating())

    def is_panel_floating(self, panel) -> bool:
        dock = self._dock_for_panel(panel)
        return bool(dock is not None and dock.isFloating())

    # -- go-to: focus map + all plots on a record / event ------------------
    def go_to_record(self, source_row: int) -> None:
        self.focus_record(source_row, pan=True)

    def focus_record(self, source_row: int, pan=True) -> None:
        """Highlight a record everywhere and centre the plots on it.

        ``pan`` is True (always pan the map), False (never) or ``"if_outside"``
        (pan only when the record is off-screen - used by keyboard stepping).
        """
        if self.dataset is None or not (0 <= source_row < self.dataset.row_count):
            return
        self._current_row = int(source_row)
        if self.dataset.has_geometry:
            lat = self.dataset.lat[source_row]
            lon = self.dataset.lon[source_row]
            if np.isfinite(lat) and np.isfinite(lon):
                self.map_sync.highlight_point(float(lon), float(lat), pan=pan)
        self._syncing = True
        try:
            self.map_sync.select_feature(int(self.dataset.fids[source_row]))
            self.table_panel.select_source_row(source_row)
        finally:
            self._syncing = False
        for panel in self._plot_panels():
            panel.unpin()
            panel.center_on_record(source_row)
            panel.set_hover(source_row, force=True)
        self._last_hover_row = source_row
        self._update_map_hover(source_row)
        self._update_status_readout(source_row)

    def step_record(self, delta: int) -> None:
        """Move the current record by ``delta`` rows (keyboard stepping)."""
        if self.dataset is None or self.dataset.row_count == 0:
            return
        current = self._current_row if self._current_row is not None else -1 if delta > 0 else self.dataset.row_count
        target = max(0, min(self.dataset.row_count - 1, current + int(delta)))
        if target == self._current_row:
            return
        self.focus_record(target, pan="if_outside")

    def escape(self) -> None:
        """Esc: clear highlight, hover, pinned tooltips and the map selection."""
        self._clear_highlight()
        if self.layer is not None:
            self._syncing = True
            try:
                self.layer.removeSelection()
            except Exception:
                pass
            finally:
                self._syncing = False
        self.statusBar().clearMessage()

    def go_to_finding(self, finding) -> None:
        """Double-clicked QC finding: pan the map and centre every plot on it."""
        self.highlight_finding(finding)
        row = None
        fid = getattr(finding, "feature_fid", None)
        if fid is not None:
            row = self._row_for_fid(int(fid))
        if row is not None:
            self.focus_record(row, pan=True)
            return
        t = None
        start = getattr(finding, "time_start", None)
        if start:
            from ..laydata.dataset import parse_iso_epoch

            value = parse_iso_epoch(start)
            t = float(value) if np.isfinite(value) else None
        for panel in self._plot_panels():
            panel.center_on_time(t)

    # -- map -> explorer selection sync ------------------------------------
    def _connect_selection(self, layer) -> None:
        if layer is self._selection_layer:
            return
        if self._selection_layer is not None:
            try:
                self._selection_layer.selectionChanged.disconnect(self._on_map_selection_changed)
            except Exception:
                pass
        self._selection_layer = layer
        if layer is not None:
            try:
                layer.selectionChanged.connect(self._on_map_selection_changed)
            except Exception:
                self._selection_layer = None

    def _on_map_selection_changed(self, selected, _deselected, _clear_and_select) -> None:
        """A feature selected on the QGIS map (or attribute table) focuses the Explorer."""
        if self._syncing or not selected:
            return
        row = self._row_for_fid(int(selected[0]))
        if row is None:
            return
        self.focus_record(row, pan=False)

    # -- status bar readout --------------------------------------------------
    def _kp_field(self, dataset) -> Optional[str]:
        key = id(dataset)
        if key not in self._kp_field_cache:
            found = None
            for name in dataset.field_names:
                if "kp" in name.lower() and dataset.is_numeric_field(name):
                    found = name
                    break
            self._kp_field_cache = {key: found}
        return self._kp_field_cache[key]

    def _update_status_readout(self, source_row: int) -> None:
        dataset = self.dataset
        if dataset is None or not (0 <= source_row < dataset.row_count):
            self.statusBar().clearMessage()
            return
        parts = [f"Record {source_row + 1:,} of {dataset.row_count:,}"]
        if dataset.time_field is not None:
            parts.append(f"{dataset.time_field}: {dataset.iso_time_at(source_row) or '-'}")
        kp = self._kp_field(dataset)
        if kp is not None:
            value = dataset.numeric(kp)[source_row]
            parts.append(f"{kp}: {value:.4f}" if np.isfinite(value) else f"{kp}: -")
        if dataset.source_field is not None:
            parts.append(f"{dataset.source_field}: {dataset.source_at(source_row) or '-'}")
        if dataset.has_geometry:
            lat, lon = dataset.lat[source_row], dataset.lon[source_row]
            if np.isfinite(lat) and np.isfinite(lon):
                parts.append(f"{lat:.6f}, {lon:.6f}")
        self.statusBar().showMessage("   |   ".join(parts))

    def go_to_event(self, layer_id: str, row: int) -> None:
        dataset = self.dataset_for(layer_id)
        if dataset is None or not (0 <= row < dataset.row_count):
            return
        if dataset.has_geometry:
            lat = dataset.lat[row]
            lon = dataset.lon[row]
            if np.isfinite(lat) and np.isfinite(lon):
                self.map_sync.highlight_point(float(lon), float(lat), pan=True)
        t = None
        if dataset.time_epoch is not None:
            value = dataset.time_epoch[row]
            if np.isfinite(value):
                t = float(value)
        for panel in self._plot_panels():
            panel.center_on_time(t)

    # -- QC overlays + range selection -------------------------------------
    def current_findings(self) -> list:
        return list(getattr(self.qc_panel, "_findings", []) or [])

    def row_for_fid(self, fid: int) -> Optional[int]:
        return self._row_for_fid(fid)

    def refresh_plot_overlays(self) -> None:
        for panel in self._plot_panels():
            panel.replot()

    def select_rows(self, source_rows: List[int]) -> None:
        if self.dataset is None or not source_rows:
            return
        rows = [r for r in source_rows if 0 <= r < self.dataset.row_count]
        if not rows:
            return
        try:
            fids = [int(self.dataset.fids[r]) for r in rows]
            if self.layer is not None:
                self.layer.selectByIds(fids)
        except Exception:
            pass
        if self.dataset.has_geometry:
            r0, r1 = rows[0], rows[-1]
            lon0, lat0 = self.dataset.lon[r0], self.dataset.lat[r0]
            lon1, lat1 = self.dataset.lon[r1], self.dataset.lat[r1]
            if all(np.isfinite(v) for v in (lon0, lat0, lon1, lat1)):
                self.map_sync.highlight_span(float(lon0), float(lat0), float(lon1), float(lat1))
        self.table_panel.select_source_row(rows[0])


    # -- controller API used by panels ------------------------------------
    def highlight_record(self, source_row: int, from_table: bool = False, from_plot: bool = False) -> None:
        if self.dataset is None or self._syncing:
            return
        self._current_row = int(source_row)
        self._update_status_readout(source_row)
        self._syncing = True
        try:
            lat = lon = None
            if self.dataset.has_geometry:
                lat = self.dataset.lat[source_row]
                lon = self.dataset.lon[source_row]
            if lat is not None and lon is not None and np.isfinite(lat) and np.isfinite(lon):
                self.map_sync.highlight_point(float(lon), float(lat))
            self.map_sync.select_feature(int(self.dataset.fids[source_row]))
            if not from_table:
                self.table_panel.select_source_row(source_row)
        finally:
            self._syncing = False

    def highlight_finding(self, finding) -> None:
        if finding.lat is not None and finding.lon is not None:
            self.map_sync.highlight_point(float(finding.lon), float(finding.lat), pan=True)
        if finding.feature_fid is not None:
            self.map_sync.select_feature(int(finding.feature_fid))
            row = self._row_for_fid(int(finding.feature_fid))
            if row is not None:
                self._syncing = True
                try:
                    self.table_panel.select_source_row(row)
                finally:
                    self._syncing = False

    def _row_for_fid(self, fid: int) -> Optional[int]:
        if self.dataset is None:
            return None
        matches = np.nonzero(self.dataset.fids == fid)[0]
        return int(matches[0]) if matches.size else None

    def gpkg_path(self) -> Optional[str]:
        if self.layer is None:
            return None
        source = self.layer.source() or ""
        path = source.split("|", 1)[0]
        if path.lower().endswith(".gpkg") and os.path.exists(path):
            return path
        return None

    def layer_name(self) -> Optional[str]:
        return self.layer.name() if self.layer is not None else None

    def transform_context(self):
        return QgsProject.instance().transformContext()

    def refresh_findings_layer(self, gpkg_path: str, findings_layer: str) -> None:
        uri = f"{gpkg_path}|layername={findings_layer}"
        for layer in QgsProject.instance().mapLayers().values():
            if (layer.source() or "").split("|", 1)[0] == gpkg_path and findings_layer in (layer.source() or ""):
                layer.dataProvider().reloadData()
                layer.triggerRepaint()
                return
        self.iface.addVectorLayer(uri, findings_layer, "ogr")

    # -- persistence / lifecycle ------------------------------------------
    def _restore_plots(self) -> None:
        """Recreate the plot panels saved from a previous session (or one default)."""
        raw = self.settings.value(f"{_SETTINGS_GROUP}/plot_configs")
        configs: list = []
        if raw:
            try:
                configs = json.loads(raw)
            except (ValueError, TypeError):
                configs = []
        if not isinstance(configs, list) or not configs:
            configs = [None]
        for config in configs:
            self.add_plot_panel(config if isinstance(config, dict) else None)

        sync = self.settings.value(f"{_SETTINGS_GROUP}/sync_crosshair")
        if sync is not None:
            checked = str(sync).lower() in ("true", "1")
            self._sync_crosshair = checked
            self.sync_action.setChecked(checked)

        mode = self.settings.value(f"{_SETTINGS_GROUP}/plot_layout")
        if mode in _PLOT_LAYOUTS:
            self._plot_layout_mode = mode
            self.layout_combo.blockSignals(True)
            self.layout_combo.setCurrentText(mode)
            self.layout_combo.blockSignals(False)

        lock = self.settings.value(f"{_SETTINGS_GROUP}/lock_x")
        if lock is not None:
            checked = str(lock).lower() in ("true", "1")
            self._lock_x = checked
            self.lock_x_action.setChecked(checked)

        active_only = self.settings.value(f"{_SETTINGS_GROUP}/active_only")
        if active_only is not None:
            self.set_active_only(str(active_only).lower() in ("true", "1"))

    def _clear_highlight(self) -> None:
        self.map_sync.clear()
        self._last_hover_row = None
        for panel in self._plot_panels():
            panel.clear_hover()

    def _restore_state(self) -> None:
        geometry = self.settings.value(f"{_SETTINGS_GROUP}/geometry")
        state = self.settings.value(f"{_SETTINGS_GROUP}/state")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if state is not None:
            self.restoreState(state)
        tab = self.settings.value(f"{_SETTINGS_GROUP}/analysis_tab")
        try:
            index = int(tab)
        except (TypeError, ValueError):
            index = -1
        if 0 <= index < self.analysis_tabs.count():
            self.analysis_tabs.setCurrentIndex(index)

    def _save_state(self) -> None:
        self.settings.setValue(f"{_SETTINGS_GROUP}/geometry", self.saveGeometry())
        self.settings.setValue(f"{_SETTINGS_GROUP}/state", self.saveState())
        configs = [panel.get_config() for panel in self._plot_panels()]
        self.settings.setValue(f"{_SETTINGS_GROUP}/plot_configs", json.dumps(configs))
        self.settings.setValue(f"{_SETTINGS_GROUP}/plot_layout", self._plot_layout_mode)
        self.settings.setValue(f"{_SETTINGS_GROUP}/sync_crosshair", self._sync_crosshair)
        self.settings.setValue(f"{_SETTINGS_GROUP}/lock_x", self._lock_x)
        self.settings.setValue(f"{_SETTINGS_GROUP}/active_only", self._active_only)
        self.settings.setValue(f"{_SETTINGS_GROUP}/analysis_tab", self.analysis_tabs.currentIndex())
        self.settings.setValue(
            f"{_SETTINGS_GROUP}/loaded_layers", json.dumps(list(self._datasets.keys()))
        )
        self.settings.setValue(f"{_SETTINGS_GROUP}/active_layer", self._active_layer_id or "")

    def _restore_loaded_layers(self) -> None:
        """Reload the layers that were loaded in a previous session, if present."""
        raw = self.settings.value(f"{_SETTINGS_GROUP}/loaded_layers")
        if not raw:
            return
        try:
            layer_ids = json.loads(raw)
        except (ValueError, TypeError):
            return
        present = [
            i for i in layer_ids
            if isinstance(i, str) and QgsProject.instance().mapLayer(i) is not None
        ]
        if not present:
            return
        active = self.settings.value(f"{_SETTINGS_GROUP}/active_layer") or None
        # The active layer is applied once the background load finishes.
        self._pending_active = active if active in present else None
        self.set_loaded_layers(present)

    def closeEvent(self, event) -> None:
        self._cancel_load()
        self._save_state()
        self._connect_selection(None)
        self.map_sync.clear()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Full teardown for plugin unload: remove canvas graphics."""
        try:
            self._cancel_load()
        except Exception:
            pass
        try:
            self._connect_selection(None)
        except Exception:
            pass
        try:
            self._save_state()
        except Exception:
            pass
        try:
            self.map_sync.cleanup()
        except Exception:
            pass
