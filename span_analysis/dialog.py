# -*- coding: utf-8 -*-
"""Span & Slack Analysis: the tool window.

Left: the setup steps and the Run button. Right: the profile (status bar,
seabed and cable, tension, slack, slope and friction) above the results
(findings, spans, bodies, the detail model, the tension sweep and a summary).

A run reads the setup on the GUI thread, then samples the seabed (the first
time, or when the route or seabed changes) and solves the model in a
background task. The stations are kept, so a change of the lay control (the
slider) solves again in about a second ("live"). Scenarios save in the
project's Span Analysis GeoPackage (:mod:`span_analysis.store`).
"""

from __future__ import annotations

import csv
import html
import json
import math
import os
from typing import Dict, List, Optional

import numpy as np

from qgis.PyQt.QtCore import QSettings, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsApplication,
    QgsLineSymbol,
    QgsProject,
    QgsRuleBasedRenderer,
    QgsVectorLayer,
)

from ..cable_seabed import planning as pl
from ..cable_seabed.core import ERROR, INFO, WARNING, sub_progress
from ..cable_seabed.profiles import SyntheticSeabed, read_profile_csv, synthetic_profile
from ..cable_seabed.verification import depression_check
from ..plugin_log import log_exception
from ..qgis_compat import (
    BUTTON_BOX_CANCEL,
    BUTTON_BOX_OK,
    DIALOG_ACCEPTED,
    MESSAGEBOX_NO,
    MESSAGEBOX_YES,
    TOOLBUTTON_POPUP_MODE_INSTANT,
    WINDOW_HINT_CLOSE,
    WINDOW_HINT_MIN_MAX,
    WINDOW_TYPE_WINDOW,
    qt_exec,
)
from . import dxf_io, sources, store as span_store
from .help import HELP_HTML
from .map_link import KpPickTool, MapLink
from .plot_panel import ProfilePlot
from .examples import EXAMPLES, example_settings
from .results_panel import ResultsPanel, summary_html
from .setup_panel import CABLE_FIT, CABLE_RPL, CABLE_SECTIONS, SetupPanel
from .task import WorkTask

TITLE = "Span & Slack Analysis"
_SETTINGS = "SubseaCableTools/SpanAnalysis"
_HORIZONTAL = getattr(getattr(Qt, "Orientation", Qt), "Horizontal")
_VERTICAL = getattr(getattr(Qt, "Orientation", Qt), "Vertical")
_STALE_STYLE = "QLabel { background: #fff3cd; color: #6d4c00; padding: 3px; border-radius: 2px; }"
_LEVEL_COLOURS = {ERROR: "#d32f2f", WARNING: "#ff9800", INFO: "#1f77b4"}
_CANCEL_BUTTON = getattr(getattr(QMessageBox, "StandardButton", QMessageBox), "Cancel")
_DISCARD_BUTTON = getattr(getattr(QMessageBox, "StandardButton", QMessageBox), "Discard")
_SAVE_BUTTON = getattr(getattr(QMessageBox, "StandardButton", QMessageBox), "Save")


_ROUTE_KEY = "|route:"


def _base_key(key: str) -> str:
    """A sampled-seabed key without its route fingerprint (the settings part)."""
    return str(key or "").split(_ROUTE_KEY)[0]


def _profile_dict(prof: pl.Profile) -> Dict:
    return {"x_m": [float(v) for v in prof.x_m], "kp_km": [float(v) for v in prof.kp_km],
            "depth_m": [None if not math.isfinite(v) else float(v) for v in prof.depth_m],
            "notes": list(prof.notes), "source": prof.source}


def run_job(job: Dict, cancel=None, progress=None) -> Dict:
    """Worker: sample the seabed if needed, prepare the stations and solve."""
    profile = job.get("profile")
    base = 0.0
    if profile is None:
        profile = sources.sample_profile(job["request"], job["kp0"], job["seabed_mode"], job["auto"], cancel,
                                         sub_progress(progress, 0.0, 0.5))
        base = 0.5
    prof = pl.Profile(np.asarray(profile["x_m"], dtype=float), np.asarray(profile["kp_km"], dtype=float),
                      np.array([np.nan if v is None else float(v) for v in profile["depth_m"]]),
                      turns=list(job.get("turns") or []), notes=list(profile.get("notes") or []),
                      source=profile.get("source", ""))
    inputs = pl.PlanningInputs(prof, job["cable"], job["sections"], job["bodies"], job["features"], job["friction"],
                               job["control"], job["criteria"], job["filter_m"], job["smoothing_m"],
                               job.get("protection") or [])
    stations = pl.prepare(inputs)
    result = pl.solve(stations, inputs.control, inputs.criteria, cancel=cancel,
                      progress=sub_progress(progress, base, 1.0))
    return {"profile": profile, "stations": stations, "result": result}


class SpanAnalysisDialog(QDialog):
    def __init__(self, parent=None, iface=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle(f"{TITLE} (beta)")
        self.setWindowFlags(WINDOW_TYPE_WINDOW | WINDOW_HINT_MIN_MAX | WINDOW_HINT_CLOSE)
        self.resize(1500, 950)
        self._persist_ui = True
        self._task: Optional[WorkTask] = None
        self._task_kind = ""
        self._dropped: List[WorkTask] = []
        self._pending_live = False
        self._result: Optional[pl.PlanningResult] = None
        self._previous: Optional[pl.PlanningResult] = None
        self._stations = None
        self._stations_key = ""
        self._result_key = ""
        self._profile: Optional[Dict] = None
        self._profile_key = ""
        self._profile_saved = True
        self._route: Optional[sources.RouteGeo] = None
        self._detail = None
        self._rpl_model = None
        self._notes: List[str] = []
        self._store: Optional[span_store.SpanStore] = None
        self._scenario_id: Optional[str] = None
        self._dirty = False
        self._loading = False
        canvas = iface.mapCanvas() if iface is not None else None
        self.map = MapLink(canvas)
        self._pick_tool = None
        self._previous_tool = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        analysis = QWidget()
        self.tabs.addTab(analysis, "Analysis")
        help_view = QTextBrowser()
        help_view.setHtml(HELP_HTML)
        help_view.setOpenExternalLinks(True)
        self.tabs.addTab(help_view, "Help")

        layout = QHBoxLayout(analysis)
        layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(_HORIZONTAL)
        layout.addWidget(self.splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addLayout(self._build_scenario_bar())
        self.setup = SetupPanel()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.setup)
        left_layout.addWidget(scroll, 1)
        run_row = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.setToolTip("Sample the seabed (when the route or seabed changed) and solve. Runs in the "
                                   "background: keep working, or Cancel.")
        self.run_button.clicked.connect(self.run)
        run_row.addWidget(self.run_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        run_row.addWidget(self.progress, 1)
        left_layout.addLayout(run_row)
        self.stage = QLabel("")
        self.stage.setVisible(False)
        left_layout.addWidget(self.stage)
        self.stale = QLabel("The settings changed: Run to update the results.")
        self.stale.setStyleSheet(_STALE_STYLE)
        self.stale.setWordWrap(True)
        self.stale.setVisible(False)
        left_layout.addWidget(self.stale)
        self.status = QLabel("Set up the route, seabed, cable and lay control, then Run.")
        self.status.setWordWrap(True)
        left_layout.addWidget(self.status)
        self.splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.pick_button = QPushButton("Pick KP on map")
        self.pick_button.setCheckable(True)
        self.pick_button.setToolTip("Click near the route on the map to go to that KP in the profile.")
        self.pick_button.toggled.connect(self._toggle_pick)
        actions.addWidget(self.pick_button)
        self.map_button = QPushButton("Add to map")
        self.map_button.setToolTip("Write the findings as a line layer (red / amber / blue) in the Span Analysis "
                                   "GeoPackage and add it to the project.")
        self.map_button.clicked.connect(self.add_to_map)
        actions.addWidget(self.map_button)
        export = QToolButton()
        export.setText("Export")
        export.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        menu = QMenu(export)
        menu.addAction("Findings (CSV)…", lambda: self.export_csv("findings"))
        menu.addAction("Spans (CSV)…", lambda: self.export_csv("spans"))
        menu.addAction("Profile by station (CSV)…", lambda: self.export_csv("profile"))
        menu.addAction("Plot image (PNG)…", self.export_png)
        menu.addAction("Scene and results (DXF)…", self.export_dxf)
        export.setMenu(menu)
        actions.addWidget(export)
        actions.addStretch(1)
        right_layout.addLayout(actions)
        self.vsplit = QSplitter(_VERTICAL)
        self.plot = ProfilePlot()
        self.results = ResultsPanel()
        self.vsplit.addWidget(self.plot)
        self.vsplit.addWidget(self.results)
        self.vsplit.setSizes([560, 400])
        right_layout.addWidget(self.vsplit, 1)
        self.splitter.addWidget(right)
        self.splitter.setSizes([430, 1070])

        self.setup.changed.connect(self._on_settings_changed)
        self.setup.controlChanged.connect(self._on_control_changed)
        self.setup.libraryRequested.connect(self._open_library)
        self.setup.tensionFinderRequested.connect(self.results.show_find)
        self.plot.kpHovered.connect(self.map.hover_kp)
        self.plot.kpClicked.connect(lambda kp: self.map.show_kp(kp, pan="if_outside"))
        self.plot.kpDoubleClicked.connect(lambda kp: self.map.show_kp(kp, pan=True))
        self.plot.bodyClicked.connect(self._on_body_clicked)
        self.plot.reverse_check.toggled.connect(self.results.set_reverse)
        self.results.set_reverse(self.plot.reverse_check.isChecked())
        self.results.findingSelected.connect(lambda f: self._show_range(f.kp_start, f.kp_end, False))
        self.results.findingActivated.connect(lambda f: self._show_range(f.kp_start, f.kp_end, True))
        self.results.rangeSelected.connect(lambda a, b: self._show_range(a, b, False))
        self.results.detailRequested.connect(self.run_detail)
        self.results.sweepRequested.connect(self.run_sweep)
        self.results.findRequested.connect(self.run_find)
        self.results.useTension.connect(self._use_tension)
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(250)
        self._live_timer.timeout.connect(self._run_live)
        self._open_store()
        self._restore_ui()
        self._update_buttons()

    # ------------------------------------------------------------------------------
    # Scenarios and the GeoPackage
    # ------------------------------------------------------------------------------
    def _build_scenario_bar(self):
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Scenario"))
        self.scenario_combo = QComboBox()
        self.scenario_combo.setMinimumWidth(140)
        self.scenario_combo.currentIndexChanged.connect(self._on_scenario_selected)
        bar.addWidget(self.scenario_combo, 1)
        self.save_button = QPushButton("Save")
        self.save_button.setToolTip("Save the settings (and the sampled seabed) to this scenario.")
        self.save_button.clicked.connect(self.save_scenario)
        bar.addWidget(self.save_button)
        more = QToolButton()
        more.setText("…")
        more.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        menu = QMenu(more)
        menu.addAction("New scenario", self.new_scenario)
        menu.addAction("Save as a new scenario…", lambda: self.save_scenario(as_new=True))
        menu.addAction("Rename scenario…", self.rename_scenario)
        menu.addAction("Delete scenario", self.delete_scenario)
        menu.addSeparator()
        menu.addAction("Open another Span Analysis file…", self.open_store_file)
        menu.addAction("Start a new Span Analysis file…", self.new_store_file)
        menu.addAction("Show the file in its folder", self.show_store_folder)
        more.setMenu(menu)
        bar.addWidget(more)
        examples = QToolButton()
        examples.setText("Examples")
        examples.setToolTip("Load a synthetic example, including a verification case with a closed-form answer.")
        examples.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        examples_menu = QMenu(examples)
        for index, (title, tip, _builder) in enumerate(EXAMPLES):
            action = examples_menu.addAction(title, lambda i=index: self.load_example(i))
            action.setToolTip(tip)
        examples_menu.setToolTipsVisible(True)
        examples.setMenu(examples_menu)
        bar.addWidget(examples)
        return bar

    def load_example(self, index: int) -> None:
        """Load a built-in example as a new, unsaved scenario and run it."""
        if not self._confirm_discard():
            return
        self._scenario_id = None
        self._profile, self._profile_key = None, ""
        self._clear_results()
        self.setup.apply_settings(example_settings(index))
        self._dirty = False
        self._refresh_scenarios(None)
        self.status.setText(html.escape(EXAMPLES[index][1]))
        self.run()

    def _open_store(self) -> None:
        path = span_store.project_gpkg_path() or span_store.default_gpkg_path()
        store = span_store.SpanStore(path)
        try:
            if store.exists():
                store.ensure_created()
                self._store = store
        except Exception:
            log_exception("Span & Slack Analysis: opening the scenario file failed")
            self._store = None
        self._refresh_scenarios()

    def _ensure_store(self) -> Optional[span_store.SpanStore]:
        if self._store is not None:
            return self._store
        path = span_store.project_gpkg_path() or span_store.default_gpkg_path()
        try:
            store = span_store.SpanStore(path)
            store.ensure_created()
        except Exception as exc:
            log_exception("Span & Slack Analysis: creating the scenario file failed")
            QMessageBox.warning(self, TITLE, f"Could not create the scenario file:\n{path}\n\n{exc}")
            return None
        span_store.set_project_gpkg_path(path)
        self._store = store
        return store

    def _refresh_scenarios(self, select: Optional[str] = None) -> None:
        self._loading = True
        try:
            self.scenario_combo.clear()
            self.scenario_combo.addItem("(unsaved)", None)
            if self._store is not None:
                for row in self._store.list_scenarios():
                    self.scenario_combo.addItem(row.get("name") or "Scenario", row.get("scenario_id"))
            index = self.scenario_combo.findData(select if select is not None else self._scenario_id)
            self.scenario_combo.setCurrentIndex(max(index, 0))
        finally:
            self._loading = False
        self._update_title()

    def _update_title(self) -> None:
        name = self.scenario_combo.currentText() if self._scenario_id else "unsaved"
        path = self._store.gpkg_path if self._store is not None else "no file yet"
        self.setWindowTitle(f"{TITLE} (beta) - {name}{' *' if self._dirty else ''}")
        self.scenario_combo.setToolTip(f"Scenarios in {path}")

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(self, TITLE, "Save the changes to this scenario first?",
                                      _SAVE_BUTTON | _DISCARD_BUTTON | _CANCEL_BUTTON)
        if answer == _CANCEL_BUTTON:
            return False
        if answer == _SAVE_BUTTON:
            return self.save_scenario()
        return True

    def _on_scenario_selected(self, *_args) -> None:
        if self._loading:
            return
        scenario_id = self.scenario_combo.currentData()
        if scenario_id == self._scenario_id:
            return
        if not self._confirm_discard():
            self._refresh_scenarios(self._scenario_id)
            return
        self.load_scenario(scenario_id)

    def load_scenario(self, scenario_id: Optional[str]) -> None:
        self._scenario_id = scenario_id
        if scenario_id and self._store is not None:
            row = self._store.get_scenario(scenario_id)
            settings = self._store.settings_of(row)
            self.setup.apply_settings(settings)
            cached = self._store.load_profile(scenario_id)
            if cached and _base_key(cached.get("signature") or "") == self.setup.profile_signature():
                self._profile, self._profile_key = cached, cached["signature"]
                self._profile_saved = True
            else:
                self._profile, self._profile_key = None, ""
            self._store.write_meta("last_scenario", scenario_id)
        self._stations = None
        self._dirty = False
        self._clear_results()
        self._refresh_scenarios(scenario_id)
        if self._profile is not None:
            self.status.setText("Scenario loaded with its sampled seabed: Run to solve.")

    def new_scenario(self) -> None:
        if not self._confirm_discard():
            return
        self._scenario_id = None
        self._dirty = False
        self._refresh_scenarios(None)

    def save_scenario(self, as_new: bool = False) -> bool:
        store = self._ensure_store()
        if store is None:
            return False
        name = self.scenario_combo.currentText() if self._scenario_id else ""
        if as_new or not self._scenario_id:
            text, ok = QInputDialog.getText(self, TITLE, "Scenario name:", text=name or "Base case")
            if not ok or not text.strip():
                return False
            name = text.strip()
            scenario_id = None
        else:
            scenario_id = self._scenario_id
        summary = {}
        if self._result is not None:
            s = self._result.summary
            summary = {"spans": s.spans, "red_spans": s.red_spans, "span_length_m": s.span_length_m,
                       "max_tension_kn": s.max_tension_kn}
        try:
            scenario_id = store.save_scenario(name, self.setup.settings(), scenario_id, summary)
            if self._profile is not None and _base_key(self._profile_key) == self.setup.profile_signature():
                store.save_profile(scenario_id, self._profile_key,
                                   {k: v for k, v in self._profile.items() if k != "signature"})
                self._profile_saved = True
            store.write_meta("last_scenario", scenario_id)
        except Exception as exc:
            log_exception("Span & Slack Analysis: saving the scenario failed")
            QMessageBox.warning(self, TITLE, f"Could not save the scenario:\n{exc}")
            return False
        self._scenario_id = scenario_id
        self._dirty = False
        self._refresh_scenarios(scenario_id)
        return True

    def rename_scenario(self) -> None:
        if not self._scenario_id or self._store is None:
            return
        text, ok = QInputDialog.getText(self, TITLE, "New name:", text=self.scenario_combo.currentText())
        if ok and text.strip():
            row = self._store.get_scenario(self._scenario_id)
            self._store.save_scenario(text.strip(), self._store.settings_of(row), self._scenario_id)
            self._refresh_scenarios(self._scenario_id)

    def delete_scenario(self) -> None:
        if not self._scenario_id or self._store is None:
            return
        answer = QMessageBox.question(self, TITLE, f"Delete the scenario '{self.scenario_combo.currentText()}'?",
                                      MESSAGEBOX_YES | MESSAGEBOX_NO)
        if answer != MESSAGEBOX_YES:
            return
        self._store.delete_scenario(self._scenario_id)
        self._scenario_id = None
        self._dirty = True
        self._refresh_scenarios(None)

    def open_store_file(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open a Span Analysis file", self._store_folder(),
                                              "GeoPackage (*.gpkg)")
        if not path:
            return
        store = span_store.SpanStore(path)
        try:
            if not store.exists():
                raise ValueError("This GeoPackage holds no Span Analysis scenarios.")
            store.ensure_created()
        except Exception as exc:
            QMessageBox.warning(self, TITLE, f"Could not open the file:\n{exc}")
            return
        self._switch_store(store)

    def new_store_file(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getSaveFileName(self, "New Span Analysis file", self._store_folder(),
                                              "GeoPackage (*.gpkg)")
        if not path:
            return
        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        store = span_store.SpanStore(path)
        try:
            store.ensure_created()
        except Exception as exc:
            QMessageBox.warning(self, TITLE, f"Could not create the file:\n{exc}")
            return
        self._switch_store(store)

    def _switch_store(self, store: span_store.SpanStore) -> None:
        if self._store is not None:
            self._store.close()
        self._store = store
        span_store.set_project_gpkg_path(store.gpkg_path)
        self._scenario_id = None
        self._dirty = False
        self._refresh_scenarios(None)

    def _store_folder(self) -> str:
        path = self._store.gpkg_path if self._store is not None else span_store.default_gpkg_path()
        return os.path.dirname(path)

    def show_store_folder(self) -> None:
        folder = self._store_folder()
        if os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            QMessageBox.information(self, TITLE, f"The file is created on the first save, in:\n{folder}")

    # ------------------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------------------
    def _stations_signature(self) -> str:
        s = self.setup.settings()
        s.pop("control", None)
        s["criteria"] = {k: v for k, v in s["criteria"].items()}
        return json.dumps(s, sort_keys=True, default=str)

    def _on_settings_changed(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._update_title()
        self._update_stale()

    def _on_control_changed(self) -> None:
        if self._loading or not self.setup.live.isChecked():
            return
        if self._stations is not None and self._stations_key == self._stations_signature():
            self._live_timer.start()

    def _update_stale(self) -> None:
        stale = self._result is not None and self._result_key != json.dumps(self.setup.settings(), sort_keys=True,
                                                                           default=str)
        live_pending = self._live_timer.isActive() or self._task_kind == "live"
        self.stale.setVisible(bool(stale and not live_pending))

    def _build_job(self) -> Dict:
        """Read the setup on the GUI thread into a worker-safe job (raises ValueError)."""
        setup = self.setup
        s = setup.settings()
        key = setup.profile_signature()
        route_source = s["route"]["source"]
        job: Dict = {"profile": None, "turns": []}
        self._rpl_model = None
        route = None
        window = setup.kp_window()
        if route_source == sources.ROUTE_RPL:
            store = setup.workbench
            rpl_id = s["route"]["rpl_id"]
            if store is None or not rpl_id:
                raise ValueError("Choose a Workbench RPL (or another route source).")
            self._rpl_model = sources.load_rpl_model(store, rpl_id)
            route = sources.route_from_rpl_model(self._rpl_model, setup.rpl_combo.currentText())
        elif route_source == sources.ROUTE_LINE:
            route = sources.route_from_layer(setup.line_combo.currentLayer(), s["route"]["line_selected"],
                                             s["route"]["start_kp"])
        if route is not None:
            full = route
            if window is not None:
                lo = max(window[0], route.kp_range[0])
                hi = min(window[1], route.kp_range[1])
                route = route.sub_route(lo, hi)
            job["turns"] = [t for t in full.turns() if route.kp_range[0] <= t[0] <= route.kp_range[1]]
            # The sampled seabed belongs to this route geometry and KP setting: a moved vertex, another
            # selection or KP mode changes the length, so the seabed is sampled again.
            key += _ROUTE_KEY + f"{route.kp_range[0]:.6f},{route.length_m:.3f},{len(route.stationing.vertex_chainages())}"
        self._route = route
        if self._profile is not None and self._profile_key == key:
            job["profile"] = self._profile
        elif route_source == sources.ROUTE_CSV:
            path = s["route"]["csv_path"]
            if not path or not os.path.isfile(path):
                raise ValueError("Choose the CSV profile file.")
            job["profile"] = _profile_dict(read_profile_csv(path))
        elif route_source == sources.ROUTE_DXF:
            path = s["route"]["dxf"]["path"]
            if not path or not os.path.isfile(path):
                raise ValueError("Choose the DXF profile drawing.")
            if not setup.dxf_layer.currentData():
                raise ValueError("Choose the DXF layer that holds the seabed line.")
            job["profile"] = dxf_io.profile_from_dxf(path, setup.dxf_options())
        elif route_source == sources.ROUTE_SYNTHETIC:
            syn = s["route"]["synthetic"]
            spec = SyntheticSeabed(length_m=syn["length_m"], spacing_m=syn["spacing_m"], start_kp=syn["start_kp"],
                                   depth_m=syn["depth_m"], slope_deg=syn["slope_deg"],
                                   wave_height_m=syn["wave_height_m"], wave_length_m=syn["wave_length_m"],
                                   roughness_m=syn["roughness_m"], roughness_length_m=syn["roughness_length_m"],
                                   seed=int(syn["seed"]))
            job["profile"] = _profile_dict(synthetic_profile(spec))
        elif s["seabed"]["source"] == sources.SEABED_RPL_DEPTH:
            if self._rpl_model is None:
                raise ValueError("The RPL approximate depth needs a Workbench RPL route.")
            lo, hi = route.kp_range
            job["profile"] = sources.rpl_depth_profile(self._rpl_model, lo, hi)
        else:
            rasters = [QgsProject.instance().mapLayer(i) for i in s["seabed"]["rasters"]]
            contour = (setup.contour_combo.currentLayer(), setup.contour_field.currentField())
            job["request"] = sources.build_seabed_request(route, s["seabed"]["source"], rasters, contour,
                                                          s["seabed"]["interval"])
            job["kp0"] = route.kp0
            job["seabed_mode"] = s["seabed"]["source"]
            job["auto"] = int(s["seabed"]["interval"]) == 0
        job["profile_key"] = key

        # Cable makeup and bodies.
        notes: List[str] = []
        self._kp_basis = ""
        if route is not None:
            try:
                basis = route.stationing.kp_basis_notes()
                self._kp_basis = " ".join(text for _level, text in basis)
                notes.extend(text for level, text in basis if level == "warn")
            except Exception:
                log_exception("Span & Slack Analysis: KP basis check skipped")
        if route_source == sources.ROUTE_RPL and self._rpl_model is not None:
            kp_note = sources.rpl_kp_note(self._rpl_model)
            if kp_note:
                notes.append(kp_note)
        sections: List[pl.CableSection] = []
        bodies = setup.manual_bodies()
        cable_source = s["cable"]["source"]
        cable = setup.default_cable()
        if cable_source == CABLE_SECTIONS:
            sections = setup.cable_sections_list()
        elif cable_source == CABLE_RPL:
            if self._rpl_model is None:
                raise ValueError("RPL cable types need a Workbench RPL as the route.")
            sections, rpl_notes = sources.rpl_makeup(self._rpl_model, setup.library_rows)
            notes.extend(rpl_notes)
        elif cable_source == CABLE_FIT:
            fit_id = s["cable"]["fit_id"]
            if setup.workbench is None or not fit_id:
                raise ValueError("Choose an assembly fitted to the RPL.")
            sections, fit_bodies, warnings = sources.fit_makeup(setup.workbench, fit_id, setup.library_rows)
            notes.extend(warnings)
            if s["cable"]["bodies_from_fit"]:
                bodies = fit_bodies + bodies
            if cable is None and sections:
                cable = sections[0].cable
        if cable is None:
            raise ValueError("Choose a cable type (or enter the cable properties).")
        if not (cable.w_npm > 0):
            raise ValueError(f"Cable type '{cable.name}' has no weight in water in the library.")
        legs = sources.rpl_legs(self._rpl_model) if self._rpl_model is not None else []
        control = setup.control(legs)
        uses_legs = control.mode == pl.RPL_LEGS or any(sec.mode == pl.RPL_LEGS for sec in control.sections)
        if uses_legs and not legs:
            raise ValueError("RPL slack per leg needs a Workbench RPL route with slack on its legs.")
        job.update({
            "cable": cable, "sections": sections, "bodies": bodies, "features": setup.features_list(),
            "protection": setup.protection_list(),
            "friction": setup.friction(), "control": control, "criteria": setup.criteria(),
            "filter_m": float(s["seabed"]["filter_m"]), "smoothing_m": setup.smoothing_m(), "notes": notes,
        })
        return job

    def run(self) -> None:
        if self._task is not None and self._task_kind != "live":
            self._task.cancel()
            return
        self._cancel_live()
        try:
            job = self._build_job()
        except ValueError as exc:
            self._set_status(str(exc), error=True)
            return
        except Exception as exc:
            log_exception("Span & Slack Analysis: reading the setup failed")
            self._set_status(f"Could not read the setup: {exc}", error=True)
            return
        info = {"settings": json.dumps(self.setup.settings(), sort_keys=True, default=str),
                "stations_key": self._stations_signature(), "route": self._route, "notes": job["notes"],
                "profile_key": job["profile_key"], "sampled": job["profile"] is None}
        self._start("run", "Span & Slack Analysis", lambda cancel, progress: run_job(job, cancel, progress), info)

    def _cancel_live(self) -> None:
        self._live_timer.stop()
        self._pending_live = False
        if self._task is not None and self._task_kind == "live":
            self._task.cancel()

    def _run_live(self) -> None:
        if self._stations is None or self._stations_key != self._stations_signature():
            return
        if self._task is not None:
            if self._task_kind == "live":
                self._pending_live = True
                self._task.cancel()
            return
        stations = self._stations
        legs = sources.rpl_legs(self._rpl_model) if self._rpl_model is not None else []
        control = self.setup.control(legs)
        criteria = self.setup.criteria()
        allowable = self._result.allowable_kn if self._result is not None else None

        def work(cancel, progress):
            return {"result": pl.solve(stations, control, criteria, allowable=False, cancel=cancel, progress=progress,
                                       allowable_kn=allowable)}

        info = {"settings": json.dumps(self.setup.settings(), sort_keys=True, default=str)}
        self._start("live", "Span & Slack Analysis (update)", work, info)

    def run_detail(self, _a: float, _b: float, spacing: float) -> None:
        result = self._result
        if result is None:
            self._set_status("Run the analysis first.", error=True)
            return
        if self.results.detail_mode() == "span":
            span = self.results.selected_span()
            if span is None:
                self._set_status("Select a span in the Spans tab (or choose the visible window).", error=True)
                return
            cable = result.stations.cable_at(span.i0)
            lam = cable.bending_length_m(max(span.tension_kn, 0.01))
            pad = max(20.0, 0.25 * span.length_m, 6.0 * lam if math.isfinite(lam) else 0.0) / 1000.0
            lo, hi = span.kp_start - pad, span.kp_end + pad
        else:
            lo, hi = self.plot.visible_kp()
        kp = result.stations.kp_km
        lo, hi = max(lo, float(kp[0])), min(hi, float(kp[-1]))
        if hi - lo < 0.002:
            self._set_status("The detail window is too short.", error=True)
            return
        if self._task is not None:
            self._set_status("Wait for the current run to finish.", error=True)
            return
        self._start("detail", "Span & Slack Analysis (detail)",
                    lambda cancel, progress: {"detail": pl.detail_window(result, lo, hi, spacing or None, cancel)},
                    {"result": result})

    def run_sweep(self, request: Dict) -> None:
        if self._stations is None or self._result is None:
            self._set_status("Run the analysis first.", error=True)
            return
        if self._task is not None:
            self._set_status("Wait for the current run to finish.", error=True)
            return
        stations = self._stations
        control = self._result.control
        criteria = self.setup.criteria()
        window = self.plot.visible_kp() if request.get("window") == "view" else None
        tensions = request["tensions"]
        self._start("sweep", "Span & Slack Analysis (tension sweep)",
                    lambda cancel, progress: {"sweep": pl.tension_sweep(stations, control, criteria, tensions,
                                                                        window, cancel, progress)}, {})

    def run_find(self, request: Dict) -> None:
        if self._stations is None or self._result is None:
            self._set_status("Run the analysis first.", error=True)
            return
        if self._task is not None:
            self._set_status("Wait for the current run to finish.", error=True)
            return
        stations = self._stations
        control = self._result.control
        criteria = self.setup.criteria()
        key = request.get("target")
        if key == "red":
            target = pl.TensionTarget(red_only=True)
            text = f"no red span (higher than {criteria.red_gap_m:g} m or longer than {criteria.red_length_m:g} m)"
        elif key == "none":
            target = pl.TensionTarget(0.0, 0.0)
            text = "no span at all"
        elif key == "custom":
            target = pl.TensionTarget(float(request["gap_m"]), float(request["length_m"]))
            text = f"no span higher than {target.gap_m:g} m and longer than {target.length_m:g} m"
        else:
            target = pl.TensionTarget(criteria.min_gap_m, criteria.min_length_m)
            text = f"no span higher than {criteria.min_gap_m:g} m and longer than {criteria.min_length_m:g} m"
        target.mbr = bool(request.get("mbr", True))
        target.contact = bool(request.get("contact", True))
        if target.mbr:
            text += "; bends at supports within the MBR"
        if target.contact and criteria.max_reaction_kn > 0:
            text += f"; support reactions within {criteria.max_reaction_kn:g} kN"
        kp = stations.kp_km
        first, last = float(kp[0]), float(kp[-1])
        scope = request.get("scope") or "all"
        if scope == "view":
            lo, hi = sorted(self.plot.visible_kp())
            scopes = [("Visible part", max(lo, first), min(hi, last))]
        elif scope == "sections":
            sections = sorted(self.setup.control().sections, key=lambda sec: min(sec.kp_start, sec.kp_end))
            if not sections:
                self._set_status("Add lay control sections first (Lay control, By KP section).", error=True)
                return
            scopes = [(f"Section {k}", *sorted((sec.kp_start, sec.kp_end))) for k, sec in enumerate(sections, 1)]
        else:
            scopes = [("Whole profile", first, last)]
        hi_kn = float(request.get("hi_kn") or 50.0)
        self._start("find", "Span & Slack Analysis (find tension)",
                    lambda cancel, progress: {"found": pl.find_tension(stations, control, criteria, target, scopes,
                                                                       hi_kn, cancel=cancel, progress=progress)},
                    {"scope": scope, "target": text})

    def _use_tension(self, payload: Dict) -> None:
        def down(value: float) -> float:
            return max(math.floor(value / 0.05 + 1e-9) * 0.05, 0.05)

        answers = payload.get("answers") or []
        if not answers:
            return
        if payload.get("scope") == "sections":
            self.setup.apply_tension(self.setup.value.value(),
                                     [(a.kp_start, a.kp_end, down(a.tension_kn)) for a in answers])
        else:
            self.setup.apply_tension(down(answers[0].tension_kn))
        self._set_status("Lay control set from the tension finder." + ("" if self.setup.live.isChecked() else
                                                                      " Run to update the results."))

    def _start(self, kind: str, description: str, work, info: Dict) -> None:
        task = WorkTask(description, work, self._on_finished)
        task.info = info
        task.kind = kind
        self._task = task
        self._task_kind = kind
        task.progressChanged.connect(lambda value: self.progress.setValue(int(value)))
        task.stageChanged.connect(self.stage.setText)
        self._set_running(True, kind)
        QgsApplication.taskManager().addTask(task)

    def _set_running(self, running: bool, kind: str = "") -> None:
        full = running and kind != "live"
        self.run_button.setText("Cancel" if full else "Run")
        self.progress.setVisible(full)
        self.stage.setVisible(full)
        if full:
            self.progress.setValue(0)
            self.stage.setText("Starting…")
        self.results.detail_button.setEnabled(not running)
        self.results.sweep_button.setEnabled(not running)
        self.results.find_button.setEnabled(not running)

    def _on_finished(self, task: WorkTask) -> None:
        if task in self._dropped:
            self._dropped.remove(task)
            return
        kind = getattr(task, "kind", "")
        if task is not self._task:
            return  # superseded (a newer run replaced it): nothing to show
        self._task = None
        self._task_kind = ""
        self._set_running(False)
        if task.cancelled:
            if kind != "live":
                self._set_status("Cancelled. The results shown are from the previous run.")
        elif task.result is None:
            self._set_status(f"{task.description()} failed: {task.error}", error=True)
        elif kind in ("run", "live"):
            self._apply_run(task, kind)
        elif kind == "detail":
            detail = task.result["detail"]
            self._detail = detail
            self.results.set_detail(detail, task.info.get("result"))
            self.plot.set_detail(detail)
            self.results.setCurrentWidget(self.results.detail_page)
            self._set_status("Detail model done: the purple curve is the cable with bending stiffness.")
        elif kind == "sweep":
            rows = task.result["sweep"]
            self.results.set_sweep(rows)
            self.results.setCurrentWidget(self.results.sweep_page)
            self._set_status(f"Tension sweep done ({len(rows)} tensions).")
        elif kind == "find":
            answers = task.result["found"]
            info = task.info or {}
            self.results.set_found(answers, info.get("scope", "all"), info.get("target", ""))
            self.results.show_find()
            if len(answers) == 1:
                a = answers[0]
                if a.tension_kn <= 0:
                    text = "No tension meets the target: spans break it even at the lowest tension searched."
                elif not a.limited:
                    text = f"The target is met at every tension searched (up to {a.tension_kn:.1f} kN)."
                else:
                    text = f"Highest bottom tension within the target: {a.tension_kn:.2f} kN."
            else:
                text = f"Tension found for {len(answers)} sections."
            self._set_status(text)
        if self._pending_live:
            self._pending_live = False
            self._run_live()
        self._update_stale()

    def _apply_run(self, task: WorkTask, kind: str) -> None:
        out = task.result
        result: pl.PlanningResult = out["result"]
        info = task.info or {}
        if kind == "run":
            self._stations = out["stations"]
            self._stations_key = info.get("stations_key", "")
            profile = out["profile"]
            self._profile, self._profile_key = profile, info.get("profile_key", "")
            if info.get("sampled"):
                self._profile_saved = False
                self._save_profile_if_saved_scenario()
            self.map.set_route(info.get("route"))
            self._notes = list(info.get("notes") or [])
        self._previous = self._result if self._result is not None else None
        self._result = result
        self._result_key = info.get("settings", "")
        self.plot.set_result(result, self._previous, keep_view=(kind == "live"))
        self.plot.set_detail(None)
        self.results.set_detail(None, None)
        self._detail = None
        notes = list(dict.fromkeys(self._notes + list(result.notes)))
        inputs = self.setup.describe() + ([self._kp_basis] if getattr(self, "_kp_basis", "") else [])
        self.results.set_result(result, summary_html(result, notes, inputs, depression_check(result)))
        self._report(result, notes)
        self._update_buttons()

    def _save_profile_if_saved_scenario(self) -> None:
        if self._store is None or not self._scenario_id or self._profile is None:
            return
        try:
            self._store.save_profile(self._scenario_id, self._profile_key,
                                     {k: v for k, v in self._profile.items() if k != "signature"})
            self._profile_saved = True
        except Exception:
            log_exception("Span & Slack Analysis: saving the sampled seabed failed")

    def _report(self, result: pl.PlanningResult, notes: List[str]) -> None:
        s = result.summary
        reds = sum(1 for f in result.findings if f.severity == ERROR)
        ambers = sum(1 for f in result.findings if f.severity == WARNING)
        text = (f"{s.spans} span(s) ({s.red_spans} red), {s.span_length_m:,.0f} m in total; "
                f"{reds} red and {ambers} amber finding(s). Residual tension up to {s.max_tension_kn:.2f} kN; "
                f"cable laid {s.laid_slack_pct:.2f}% over the route (fill {s.fill_pct:.2f}%).")
        if notes:
            text += "<br><span style='color:#6d4c00'>" + "<br>".join(html.escape(n) for n in notes[:6]) + "</span>"
        self.status.setText(text)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status.setText(f"<span style='color:#d32f2f'>{html.escape(text)}</span>" if error else html.escape(text))

    def _clear_results(self) -> None:
        self._result = None
        self._previous = None
        self._stations = None
        self.plot.set_result(None)
        self.results.set_result(None, "")
        self.results.set_detail(None, None)
        self.results.clear_sweep()
        self.results.clear_found()
        self.map.clear()
        self._update_buttons()
        self._update_stale()

    def _update_buttons(self) -> None:
        has = self._result is not None
        self.map_button.setEnabled(has and self.map.route is not None)
        self.pick_button.setEnabled(self.map.route is not None and self.iface is not None)

    # ------------------------------------------------------------------------------
    # Map, exports
    # ------------------------------------------------------------------------------
    def _show_range(self, kp_a: float, kp_b: float, zoom_map: bool) -> None:
        self.plot.zoom_to(kp_a, kp_b)
        self.map.show_range(kp_a, kp_b, zoom=zoom_map)

    def _on_body_clicked(self, index: int) -> None:
        self.results.select_body(index)
        if self._result is not None and 0 <= index < len(self._result.bodies):
            self.map.show_kp(self._result.bodies[index].kp_km, pan="if_outside")

    def _toggle_pick(self, on: bool) -> None:
        if self.iface is None:
            return
        canvas = self.iface.mapCanvas()
        if on:
            self._previous_tool = canvas.mapTool()
            self._pick_tool = KpPickTool(canvas, self.map, self._on_map_kp)
            canvas.setMapTool(self._pick_tool)
        elif self._pick_tool is not None:
            canvas.unsetMapTool(self._pick_tool)
            if self._previous_tool is not None:
                canvas.setMapTool(self._previous_tool)
            self._pick_tool = None

    def _on_map_kp(self, kp: float) -> None:
        self.plot.zoom_to(kp - 0.05, kp + 0.05)
        self.plot.show_kp(kp)
        self.map.show_kp(kp, pan=False)

    def _finding_rows(self) -> List[Dict]:
        result, route = self._result, self.map.route
        name = self.scenario_combo.currentText() if self._scenario_id else "unsaved"
        rows = []
        for f in result.findings:
            lo, hi = f.kp_start, f.kp_end
            if hi - lo < 0.01:
                lo, hi = lo - 0.005, hi + 0.005
            points = route.polyline_lonlat(lo, hi, 200)
            if len(points) < 2:
                continue
            wkt = "LINESTRING(" + ", ".join(f"{x:.8f} {y:.8f}" for x, y in points) + ")"
            rows.append({"scenario": name, "check": pl.CHECK_LABELS.get(f.check_id, f.check_id),
                         "level": {ERROR: "red", WARNING: "amber", INFO: "info"}.get(f.severity, f.severity),
                         "kp_from": f.kp_start, "kp_to": f.kp_end, "length_m": f.length_m,
                         "value": f.value if f.value is not None and math.isfinite(f.value) else None,
                         "unit": f.unit, "message": f.message, span_store.WKT_KEY: wkt})
        return rows

    def add_to_map(self) -> None:
        if self._result is None or self.map.route is None:
            return
        store = self._ensure_store()
        if store is None:
            return
        scenario = self.scenario_combo.currentText() if self._scenario_id else "unsaved"
        try:
            name = store.write_results_layer(scenario, self._finding_rows())
        except Exception as exc:
            log_exception("Span & Slack Analysis: writing the findings layer failed")
            QMessageBox.warning(self, TITLE, f"Could not write the findings layer:\n{exc}")
            return
        uri = f"{store.gpkg_path}|layername={name}"
        project = QgsProject.instance()
        for layer in project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and os.path.normcase(layer.source()) == os.path.normcase(uri):
                layer.reload()
                layer.triggerRepaint()
                return
        layer = QgsVectorLayer(uri, f"Span analysis - {scenario}", "ogr")
        if not layer.isValid():
            QMessageBox.warning(self, TITLE, "The findings layer was written but could not be opened.")
            return
        _style_findings(layer)
        root = project.layerTreeRoot()
        group = root.findGroup(TITLE) or root.insertGroup(0, TITLE)
        project.addMapLayer(layer, False)
        group.addLayer(layer)

    def export_csv(self, what: str) -> None:
        if self._result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, f"Export {what}", "", "CSV files (*.csv)")
        if not path:
            return
        try:
            write_csv(self._result, what, path)
        except Exception as exc:
            log_exception("Span & Slack Analysis: CSV export failed")
            QMessageBox.warning(self, TITLE, f"Export failed:\n{exc}")

    def export_dxf(self) -> None:
        if self._result is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Export DXF")
        form = QFormLayout(dialog)
        window = QComboBox()
        window.addItem("Whole profile", "all")
        window.addItem("Visible part of the profile", "view")
        form.addRow("KP range", window)
        ve = QDoubleSpinBox()
        ve.setRange(0.1, 1000.0)
        ve.setValue(float(QSettings().value(f"{_SETTINGS}/dxf_ve", 10.0)))
        ve.setToolTip("Y is the elevation times this (1 = true scale).")
        form.addRow("Vertical exaggeration", ve)
        text = QDoubleSpinBox()
        text.setRange(0.0, 1000.0)
        text.setSpecialValueText("auto")
        text.setSuffix(" m")
        form.addRow("Text height", text)
        graphs = QCheckBox("Tension, slack and slope graphs below the profile")
        graphs.setChecked(True)
        form.addRow(graphs)
        note = QLabel("Layers: SA_SEABED, SA_SEABED_SAMPLED, SA_CABLE, SA_CABLE_DETAIL, SA_SPAN_RED, SA_SPAN_AMBER, "
                      "SA_CLEAR_UNDER_LIMITS, SA_SUPPORTS, SA_BODIES, SA_FEATURES, SA_SEA_LEVEL, SA_FRAME, SA_TEXT "
                      "and one per graph curve. Units: metres; X = KP x 1000.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if qt_exec(dialog) != DIALOG_ACCEPTED:
            return
        if self._persist_ui:
            QSettings().setValue(f"{_SETTINGS}/dxf_ve", ve.value())
        path, _ = QFileDialog.getSaveFileName(self, "Export DXF", "", "DXF (*.dxf)")
        if not path:
            return
        if not path.lower().endswith(".dxf"):
            path += ".dxf"
        name = self.scenario_combo.currentText() if self._scenario_id else "unsaved scenario"
        options = dxf_io.SceneOptions(self.plot.visible_kp() if window.currentData() == "view" else None,
                                      ve.value(), graphs.isChecked(), text.value(),
                                      f"Span & Slack Analysis - {name}")
        try:
            dxf_io.export_scene(self._result, path, options, self._detail)
        except Exception as exc:
            log_exception("Span & Slack Analysis: DXF export failed")
            QMessageBox.warning(self, TITLE, f"DXF export failed:\n{exc}")
            return
        self._set_status(f"DXF written: {path}")

    def export_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save the plot", "", "PNG image (*.png)")
        if path:
            self.plot.splitter.grab().save(path if path.lower().endswith(".png") else path + ".png")

    def _open_library(self) -> None:
        try:
            from ..cable_library.dialog import CableLibraryDialog

            dialog = CableLibraryDialog(self)
            qt_exec(dialog)
        except Exception:
            log_exception("Span & Slack Analysis: opening the cable library failed")
        self.setup.refresh_sources()

    # ------------------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------------------
    def _restore_ui(self) -> None:
        settings = QSettings()
        state = settings.value(f"{_SETTINGS}/splitter")
        if state is not None:
            try:
                self.splitter.restoreState(state)
            except Exception:
                pass
        if self._store is not None:
            last = self._store.read_meta().get("last_scenario")
            if last and self.scenario_combo.findData(last) >= 0:
                self.load_scenario(last)

    def _save_ui(self) -> None:
        self.plot.persist = self._persist_ui
        if self._persist_ui:
            QSettings().setValue(f"{_SETTINGS}/splitter", self.splitter.saveState())

    def shutdown(self) -> None:
        """Stop work and remove map graphics (window close / plugin unload)."""
        self._live_timer.stop()
        if self._task is not None:
            self._dropped.append(self._task)
            self._task.cancel()
            self._task = None
        if self._pick_tool is not None and self.iface is not None:
            self.iface.mapCanvas().unsetMapTool(self._pick_tool)
            self._pick_tool = None
        self.map.cleanup()
        self._save_ui()
        if self._store is not None:
            self._store.close()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._dirty and self._scenario_id:
            if not self._confirm_discard():
                event.ignore()
                return
        self._live_timer.stop()
        if self.pick_button.isChecked():
            self.pick_button.setChecked(False)
        self.map.clear()
        self._save_ui()
        super().closeEvent(event)


def _style_findings(layer: QgsVectorLayer) -> None:
    symbol = QgsLineSymbol.createSimple({"width": "1.2"})
    renderer = QgsRuleBasedRenderer(symbol)
    root = renderer.rootRule()
    for child in list(root.children()):
        root.removeChild(child)
    for level, colour, label in (("red", "#d32f2f", "Red"), ("amber", "#ff9800", "Amber"), ("info", "#1f77b4", "Info")):
        sym = QgsLineSymbol.createSimple({"width": "1.6" if level == "red" else "1.2"})
        sym.setColor(QColor(colour))
        rule = QgsRuleBasedRenderer.Rule(sym, 0, 0, f"\"level\" = '{level}'", label)
        root.appendChild(rule)
    layer.setRenderer(renderer)


def write_csv(result: pl.PlanningResult, what: str, path: str) -> None:
    """Findings, spans, or the profile by station, as CSV."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if what == "findings":
            writer.writerow(["level", "check", "kp_from", "kp_to", "length_m", "value", "unit", "message"])
            for f in result.findings:
                writer.writerow([f.severity, pl.CHECK_LABELS.get(f.check_id, f.check_id), f"{f.kp_start:.4f}",
                                 f"{f.kp_end:.4f}", f"{f.length_m:.1f}",
                                 "" if f.value is None else f"{f.value:.4g}", f.unit, f.message])
        elif what == "spans":
            writer.writerow(["kp_from", "kp_to", "length_m", "max_gap_m", "kp_max_gap", "tension_kn", "tension_from",
                             "min_bend_radius_m", "mbr_m", "max_reaction_kn", "allowable_kn"])
            for s in result.spans:
                writer.writerow([f"{s.kp_start:.4f}", f"{s.kp_end:.4f}", f"{s.length_m:.2f}", f"{s.max_gap_m:.3f}",
                                 f"{s.kp_max_gap:.4f}", f"{s.tension_kn:.3f}", s.source,
                                 _fmt(s.min_bend_radius_m), _fmt(s.mbr_m), _fmt(s.max_reaction_kn),
                                 _fmt(s.allowable_kn)])
        else:
            st = result.stations
            writer.writerow(["kp_km", "seabed_depth_m", "cable_depth_m", "gap_m", "residual_tension_kn",
                             "lay_tension_kn", "allowable_kn", "fill_pct", "laid_pct", "used_pct", "surplus_pct",
                             "slope_deg", "mu", "cable_type", "soil"])
            allowable = result.allowable_kn if result.allowable_kn is not None else np.full(st.n, np.nan)
            for i in range(st.n):
                writer.writerow([f"{st.kp_km[i]:.5f}", _fmt(-st.obstacle_m[i], 3), _fmt(-result.cable_m[i], 3),
                                 _fmt(result.gap_m[i], 3), _fmt(result.tension_kn[i], 3),
                                 _fmt(result.deposit_kn[i], 3), _fmt(allowable[i], 2), _fmt(result.fill_pct[i], 3),
                                 _fmt(result.laid_pct[i], 3), _fmt(result.shape_pct[i], 3),
                                 _fmt(result.excess_pct[i], 3),
                                 _fmt(math.degrees(math.atan(st.slope[i])), 2), _fmt(st.mu[i], 3),
                                 st.cable_at(i).name, st.soil[i]])


def _fmt(value, digits: int = 2) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v == math.inf:
        return "inf"
    return f"{v:.{digits}f}" if math.isfinite(v) else ""
