# -*- coding: utf-8 -*-
"""The Assessment tab: acquired DOL against the plan's target depth, along the plan's KPs.

The plot (a :class:`...plotkit.row_stack.KpRowStack`):

* a **status bar**: meets target, below target, no data, no target;
* **DOL** (positive down): the governing value per metre (the worst case of
  the dataset that governs there, latest data by default), every chosen
  dataset's records faintly behind it, the plan's target as a stepped
  dashed line and the stretches below it shaded red;
* a bar naming **which dataset governs** each stretch;
* optional rows for water depth, pitch and roll, and tensions.

Hover names the record behind the value (dataset, file, line, time) and
marks the KP on the map; a click goes there. Below the plot: the summary,
the below-target ranges (click to zoom, double-click for the map), the
plan's sections, and the listing at 1, 5 or 10 m with CSV export.

Datasets referenced to another RPL are moved onto the plan's KPs first (see
:func:`..acquired.controller.dataset_kp_map`). The assessment runs in the
background whenever its inputs change and the tab is shown.
"""

from __future__ import annotations

import csv
import math
from typing import Dict, List, Optional, Set

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import QSettings, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...plotkit.row_stack import KpRowStack
from ...plotkit.widgets import StayOpenMenu, legend_html, menu_button, runs_path
from ...plugin_log import log_exception
from ...qgis_compat import EDIT_TRIGGER_NONE, SELECTION_BEHAVIOR_SELECT_ROWS, SELECTION_MODE_SINGLE
from ..acquired import assessment as A
from ..acquired import bins as B
from ..acquired import roles as R
from ..acquired.controller import dataset_kp_map
from ..acquired.help import show_help
from ..acquired.store import StoreReader
from ..ui_helpers import hint_style, status_style

_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
_SETTINGS = "SubseaCableTools/BurialPlanner/assessment"
DATASET_COLOURS = [(31, 119, 180), (255, 127, 14), (44, 160, 44), (148, 103, 189), (140, 86, 75),
                   (227, 119, 194), (127, 127, 127), (188, 189, 34), (23, 190, 207)]
GOVERNING = (13, 52, 120)
TARGET = (0, 128, 0)
BELOW_FILL = (211, 47, 47, 110)
STATUS_LEVELS = [(B.STATUS_MEETS, (76, 175, 80), "Meets target"), (B.STATUS_BELOW, (211, 47, 47), "Below target"),
                 (B.STATUS_NO_DATA, (200, 200, 200), "No data"), (B.STATUS_NO_TARGET, (144, 202, 249), "No target")]
MAX_DATASETS = len(DATASET_COLOURS)
LISTING_PREVIEW = 2000


def _faint(colour, alpha: int = 110):
    return (*colour[:3], alpha)


def _breaks(x: np.ndarray, y: np.ndarray, gap_km: float):
    """``(x, y)`` with a NaN between points further apart than ``gap_km`` (lines do not bridge gaps)."""
    if x.size < 2:
        return x, y
    cut = np.flatnonzero(np.diff(x) > gap_km) + 1
    if not cut.size:
        return x, y
    return np.insert(x, cut, np.nan), np.insert(y, cut, np.nan)


def _setting(key: str, default: float) -> float:
    try:
        return float(QSettings().value(f"{_SETTINGS}/{key}", default))
    except (TypeError, ValueError):
        return default


class AssessmentTab(QWidget):
    def __init__(self, controller, model, dock=None, parent=None, persist: bool = True):
        super().__init__(parent)
        self.controller = controller
        self.model = model
        self.dock = dock
        self.persist = persist
        self.result: Optional[A.Assessment] = None
        self._unchecked: Set[str] = set()
        self._datasets: List[Dict] = []
        self._dirty = True
        self._pending = False
        self._fitted_extent = None
        self._curves: Dict[str, List] = {"dol": [], "wd": [], "pitch": [], "roll": [], "tension": []}

        layout = QVBoxLayout(self)
        bar: List[QWidget] = []
        self.datasets_menu = StayOpenMenu(self)
        self.datasets_menu.setToolTipsVisible(True)
        self.datasets_button = menu_button("Datasets", "The datasets to assess (where they overlap, the latest "
                                                       "data wins, or the worst case).", self.datasets_menu)
        bar.append(self.datasets_button)
        bar.append(QLabel("Data:"))
        self.mode = QComboBox()
        self.mode.addItem("Latest wins", B.MODE_LATEST)
        self.mode.addItem("Worst case", B.MODE_WORST)
        self.mode.setToolTip("Where datasets overlap, take the latest data there (by acquisition time), or the "
                             "shallowest DOL of any of them.")
        self.mode.currentIndexChanged.connect(self.request_build)
        bar.append(self.mode)
        bar.append(QLabel("Range:"))
        self.range = QComboBox()
        self.range.addItem("Plan scope", "scope")
        self.range.addItem("All data", "all")
        self.range.currentIndexChanged.connect(self.request_build)
        bar.append(self.range)
        settings = StayOpenMenu(self)
        settings.addAction("Tolerance…", lambda: self._ask_setting(
            "tolerance_m", "Tolerance (m)", "Count DOL within this much of the target as meeting it:", 0.0))
        settings.addAction("Join below-target ranges…", lambda: self._ask_setting(
            "join_gap_m", "Join ranges (m)", "Join below-target ranges separated by less than (m):", 0.0))
        settings.addAction("Bridging limit…", lambda: self._ask_setting(
            "bridge_m", "Bridging limit (m)", "Bridge gaps between records up to (m; 0 = 3x the typical spacing, "
            "at least 1 m):", 0.0))
        bar.append(menu_button("Settings", "Tolerance, joining of ranges, and the gap records bridge.", settings))
        self.state = QLabel("")
        self.state.setStyleSheet(hint_style())

        self.stack = KpRowStack("BurialPlanner/assessment_plot", levels=STATUS_LEVELS, persist=persist)
        # One toolbar row: the assessment's choices, then the plot's Fit and Plots.
        for index, widget in enumerate(bar):
            self.stack.toolbar.insertWidget(index, widget)
        self.stack.toolbar.insertWidget(self.stack.toolbar.count() - 1, self.state, 1)
        help_button = QPushButton("Help")
        help_button.clicked.connect(lambda: show_help(self))
        self.stack.toolbar.addWidget(help_button)
        self.stack.add_row("dol", "DOL", "Depth of lowering below mean seabed level (positive down).", "DOL (m)",
                           invert_y=True, height=300, min_height=120, stretch=5)
        self.stack.add_band("source", [(-1, (220, 220, 220), "No data")] + [
            (k, colour, f"Dataset {k + 1}") for k, colour in enumerate(DATASET_COLOURS)],
            title="Which dataset governs", tip="Which dataset's data is used along the route.")
        self.stack.add_row("wd", "Water depth", "Water depth from the datasets that have it.", "WD (m)",
                           invert_y=True, height=120)
        self.stack.add_row("attitude", "Pitch and roll", "Pitch (solid) and roll (dashed).", "Pitch / roll (°)",
                           height=110, visible=False)
        self.stack.add_row("tension", "Tension", "Tensions the datasets carry.", "T (kN)", height=110, visible=False)
        self.stack.restore_layout()
        dol_plot = self.stack.plots["dol"]
        self.below_fill = self.stack.path_item("dol", BELOW_FILL, z=1)
        self.target_curve = self.stack.curve("dol", TARGET, width=1.5, style=_DASH, fast=False)
        self.target_curve.setZValue(4)
        self.governing_curve = self.stack.curve("dol", GOVERNING, width=1.6)
        self.governing_curve.setZValue(5)
        dol_plot.vb.extra_menu = self._context_actions
        self.stack.readout_provider = self._readout
        self.stack.tip_provider = self._tip
        self.stack.band_text = self._band_text
        self.stack.kpHovered.connect(self._hovered)
        self.stack.kpClicked.connect(self._clicked)
        self.stack.bandClicked.connect(lambda _k, lo, hi, _kp: self._zoom_range(lo, hi))
        layout.addWidget(self.stack, 3)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet(hint_style())
        layout.addWidget(self.notes)

        self.tables = QTabWidget()
        self.ranges_table = self._table(["KP from", "KP to", "Length (m)", "Min DOL (m)", "at KP",
                                         "Worst shortfall (m)", "Dataset"])
        self.ranges_table.cellClicked.connect(lambda row, _c: self._range_clicked(row))
        self.ranges_table.cellDoubleClicked.connect(lambda row, _c: self._range_clicked(row, True))
        self.tables.addTab(self.ranges_table, "Below target")
        self.sections_table = self._table(["Section", "KP from", "KP to", "Assessed (m)", "No data (m)",
                                           "Meets target (%)", "Below (m)", "Ranges", "Min DOL (m)"])
        self.sections_table.cellDoubleClicked.connect(lambda row, _c: self._section_clicked(row))
        self.tables.addTab(self.sections_table, "By section")
        listing = QWidget()
        listing_layout = QVBoxLayout(listing)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Every"))
        self.listing_step = QComboBox()
        for step in (1, 5, 10, 20, 50, 100):
            self.listing_step.addItem(f"{step} m", float(step))
        self.listing_step.setCurrentIndex(0)
        controls.addWidget(self.listing_step)
        self.listing_method = QComboBox()
        self.listing_method.addItem("worst case (shallowest)", B.METHOD_WORST)
        self.listing_method.addItem("mean", B.METHOD_MEAN)
        controls.addWidget(self.listing_method)
        export = QPushButton("Export CSV…")
        export.clicked.connect(self._export_listing)
        controls.addWidget(export)
        self.listing_note = QLabel("")
        self.listing_note.setStyleSheet(hint_style())
        controls.addWidget(self.listing_note, 1)
        listing_layout.addLayout(controls)
        self.listing_table = self._table([])
        listing_layout.addWidget(self.listing_table, 1)
        self.listing_step.currentIndexChanged.connect(self._fill_listing)
        self.listing_method.currentIndexChanged.connect(self._fill_listing)
        self.tables.addTab(listing, "Listing")
        layout.addWidget(self.tables, 2)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._build)
        self.controller.changed.connect(self._datasets_changed)
        self.controller.busyChanged.connect(self._busy_changed)
        for signal in (model.planChanged, model.sectionsChanged):
            signal.connect(self.request_build)
        self._datasets_changed()

    @staticmethod
    def _table(headers: List[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(EDIT_TRIGGER_NONE)
        table.setSelectionBehavior(SELECTION_BEHAVIOR_SELECT_ROWS)
        table.setSelectionMode(SELECTION_MODE_SINGLE)
        return table

    # -- inputs -------------------------------------------------------------------------
    def _ask_setting(self, key: str, title: str, prompt: str, default: float) -> None:
        value, ok = QInputDialog.getDouble(self, title, prompt, _setting(key, default), 0.0, 100000.0, 3)
        if ok:
            if self.persist:
                QSettings().setValue(f"{_SETTINGS}/{key}", value)
            self.request_build()

    def _datasets_changed(self) -> None:
        self._datasets = self.controller.datasets() if not self.controller.busy else self._datasets
        self.datasets_menu.clear()
        for dataset in self._datasets[:MAX_DATASETS]:
            action = self.datasets_menu.addAction(f"{dataset['name']}")
            action.setCheckable(True)
            action.setChecked(dataset["dataset_id"] not in self._unchecked)
            action.setToolTip(f"KPs referenced to {dataset.get('rpl_label') or 'this plan RPL'}")
            action.toggled.connect(lambda on, d=dataset["dataset_id"]: self._toggle_dataset(d, on))
        if len(self._datasets) > MAX_DATASETS:
            note = self.datasets_menu.addAction(f"({len(self._datasets) - MAX_DATASETS} more: reorder the datasets "
                                                "in Acquired Data to choose them)")
            note.setEnabled(False)
        self._update_datasets_button()
        self.request_build()

    def _toggle_dataset(self, dataset_id: str, on: bool) -> None:
        if on:
            self._unchecked.discard(dataset_id)
        else:
            self._unchecked.add(dataset_id)
        self._update_datasets_button()
        self.request_build()

    def _chosen(self) -> List[Dict]:
        return [d for d in self._datasets[:MAX_DATASETS] if d["dataset_id"] not in self._unchecked]

    def _update_datasets_button(self) -> None:
        total = min(len(self._datasets), MAX_DATASETS)
        self.datasets_button.setText(f"Datasets ({len(self._chosen())} of {total})"
                                     + (" ▾" if self.datasets_button.text().endswith("▾") else ""))

    def _busy_changed(self, busy: bool) -> None:
        if not busy and self._pending:
            self._pending = False
            self.request_build()

    def request_build(self, *_args) -> None:
        self._dirty = True
        if self.isVisible():
            self._timer.start()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        if self._dirty:
            self._timer.start()

    # -- building ----------------------------------------------------------------------
    def _request(self) -> Optional[A.Request]:
        notes = []
        chosen = self._chosen()
        if self.controller.store is None or not chosen:
            self._show_empty("Add burial data in the Acquired Data tab to assess it." if not self._datasets
                             else "Choose a dataset to assess.")
            return None
        if getattr(self.model, "route", None) is None:
            self._show_empty("Open a plan with a route (its RPL) to assess the data on its KPs.")
            return None
        specs = []
        for dataset in chosen:
            try:
                kp_map, note = dataset_kp_map(self.model, dataset)
            except ValueError as exc:
                notes.append(f"{dataset['name']} left out: {exc}")
                continue
            bridge = _setting("bridge_m", 0.0)
            specs.append(A.DatasetSpec(dataset["dataset_id"], dataset["name"], kp_map,
                                       note if kp_map is not None else "", bridge or None))
        if not specs:
            self._show_empty(" ".join(notes))
            return None
        route = self.model.route
        scope = None
        if self.range.currentData() == "scope":
            params = self.model.gen_params().scope
            lo, hi = float(params.start_km), float(params.end_km)
            if hi - lo <= 1e-6:
                lo, hi = float(route.start_kp_km), float(route.start_kp_km + route.total_length_km)
            scope = (lo, hi)
        lo_t, hi_t = (scope if scope is not None else
                      (float(route.start_kp_km), float(route.start_kp_km + route.total_length_km)))
        target_runs = self.model.target_runs(lo_t, hi_t) if self.model.plan else []
        channels = [R.WATER_DEPTH, R.PITCH, R.ROLL] + self._tension_keys(chosen)
        sections = [{"section_id": s.get("section_id"), "kind": s.get("kind"), "start_kp": s.get("start_kp"),
                     "end_kp": s.get("end_kp")} for s in (self.model.sections or [])]
        return A.Request(specs, self.mode.currentData(), target_runs=target_runs, scope_km=scope,
                         tolerance_m=_setting("tolerance_m", 0.0), join_gap_m=_setting("join_gap_m", 0.0),
                         channels=channels, sections=sections, notes=notes)

    def _tension_keys(self, datasets: List[Dict]) -> List[str]:
        keys = []
        store = self.controller.store
        for dataset in datasets:
            try:
                for f in store.list_files(dataset["dataset_id"], include_inactive=False):
                    keys += [c["key"] for c in f.get("channels") or [] if str(c.get("key", "")).startswith("tension:")]
            except Exception:
                continue
        return list(dict.fromkeys(keys))

    def _build(self) -> None:
        if not self.isVisible():
            return
        if self.controller.busy:
            self._pending = True
            self.state.setText("Waiting for the running task…")
            return
        request = self._request()
        if request is None:
            self._dirty = False
            return
        path = self.controller.store.path
        self._dirty = False

        def work(cancelled, progress):
            with StoreReader(path) as reader:
                return A.build(reader, request, cancelled, progress)

        self.state.setText("Assessing…")
        if not self.controller.run("Assessing burial data", work, self._built):
            self._pending = True

    def _built(self, task) -> None:
        if task.cancelled:
            self.state.setText("Assessment stopped.")
            return
        if task.error:
            self.state.setText("")
            self._show_empty(task.error)
            return
        self.state.setText("")
        self.result = task.result
        try:
            self._draw()
        except Exception:
            log_exception("Burial Planner: drawing the assessment failed")

    # -- drawing -------------------------------------------------------------------------
    def _show_empty(self, message: str) -> None:
        self.result = None
        self.summary.setText(message)
        self.summary.setStyleSheet(status_style("warn"))
        self.notes.setText("")
        for curves in self._curves.values():
            for curve in curves:
                curve.setData([], [])
        for item in (self.governing_curve, self.target_curve):
            item.setData([], [])
        self.below_fill.setPath(runs_path(np.zeros(0), np.zeros(0), np.zeros(0), []))
        self.stack.set_status(np.zeros(0), np.zeros(0), np.zeros(0))
        self.stack.set_band("source", np.zeros(0), np.zeros(0), np.zeros(0))
        for table in (self.ranges_table, self.sections_table, self.listing_table):
            table.setRowCount(0)

    def _curve(self, row: str, index: int, colour, style=None, width: float = 1.0):
        """The ``index``-th line of ``row`` (made once, reused: items are never removed)."""
        curves = self._curves[row]
        while len(curves) <= index:
            plot_row = {"pitch": "attitude", "roll": "attitude"}.get(row, row)
            curves.append(self.stack.curve(plot_row, colour, width=width, style=style))
        curve = curves[index]
        curve.setPen(pg.mkPen(colour, width=width) if style is None else pg.mkPen(colour, width=width, style=style))
        return curve

    def _draw(self) -> None:
        result = self.result
        if result is None:
            return
        extent = result.extent_km
        self.stack.set_extent(*(extent or (None, None)))
        used = {key: 0 for key in self._curves}
        legend = [("solid", GOVERNING, "DOL used" + (" (latest data)" if result.request.mode == B.MODE_LATEST
                                                      else " (worst case)")),
                  ("dash", TARGET, "Target"), ("fill", BELOW_FILL, "Below target")]
        tension_index = 0
        for k, series in enumerate(result.series):
            colour = DATASET_COLOURS[k % len(DATASET_COLOURS)]
            order = series.plot_order()
            x = series.kp[order]
            gap = max(series.binset.bridge_m, 1.0) / 1000.0
            bx, by = _breaks(x, series.dol[order], gap)
            self._curve("dol", k, _faint(colour)).setData(bx, by)
            used["dol"] += 1
            legend.append(("solid", colour, series.spec.name))
            for key, row in ((R.WATER_DEPTH, "wd"), (R.PITCH, "pitch"), (R.ROLL, "roll")):
                values = series.channels.get(key)
                style = _DASH if key == R.ROLL else None
                curve = self._curve(row, used[row], colour, style=style)
                if values is not None and np.isfinite(values).any():
                    cx, cy = _breaks(x, values[order], gap)
                    curve.setData(cx, cy)
                else:
                    curve.setData([], [])
                used[row] += 1
            for key, values in series.channels.items():
                if not key.startswith("tension:"):
                    continue
                curve = self._curve("tension", tension_index, colour, style=_DASH if tension_index % 2 else None)
                cx, cy = _breaks(x, values[order], gap)
                curve.setData(cx, cy)
                tension_index += 1
        used["tension"] = tension_index
        for row, curves in self._curves.items():
            for curve in curves[used[row]:]:
                curve.setData([], [])
        sel = result.selection
        step = sel.step_m / 1000.0
        gx, gy = _breaks(sel.centre_km, sel.worst, step * 1.5)
        self.governing_curve.setData(gx, gy)
        tx, ty = [], []
        for a, b, depth in result.request.target_runs:
            if depth is None:
                continue
            if tx and abs(tx[-1] - a) > 1e-9:
                tx.append(np.nan)
                ty.append(np.nan)
            tx += [a, b]
            ty += [depth, depth]
        self.target_curve.setData(np.asarray(tx, dtype=float), np.asarray(ty, dtype=float))
        self._draw_below(result)
        self.stack.set_status(*result.status_runs)
        self.stack.set_band("source", *result.source_runs)
        self.stack.set_legend("dol", legend_html(legend))
        self.stack.set_legend("attitude", legend_html([("solid", (90, 90, 90), "Pitch"),
                                                       ("dash", (90, 90, 90), "Roll")]))
        if extent is not None and extent != self._fitted_extent:
            self._fitted_extent = extent
            self.stack.fit()
        self._fill_summary(result)
        self._fill_ranges(result)
        self._fill_sections(result)
        self._fill_listing()

    def _draw_below(self, result: A.Assessment) -> None:
        sel = result.selection
        rows = np.flatnonzero(result.status == B.STATUS_BELOW)
        if not rows.size:
            self.below_fill.setPath(runs_path(np.zeros(0), np.zeros(0), np.zeros(0), []))
            return
        step = sel.step_m / 1000.0
        start = sel.index[rows] * step
        x = np.repeat(start, 2)
        x[1::2] += step
        top = np.repeat(result.target[rows], 2)
        bottom = np.repeat(sel.worst[rows], 2)
        breaks = np.flatnonzero(np.diff(sel.index[rows]) > 1)
        firsts = np.r_[0, breaks + 1] * 2
        lasts = np.r_[breaks, rows.size - 1] * 2 + 1
        self.below_fill.setPath(runs_path(x, top, bottom, list(zip(firsts.tolist(), lasts.tolist()))))

    def _fill_summary(self, result: A.Assessment) -> None:
        s = result.stats
        parts = [f"Assessed <b>{s['assessed_m'] / 1000.0:.3f} km</b> of {s['scope_m'] / 1000.0:.3f} km",
                 f"no data {s['no_data_m'] / 1000.0:.3f} km"]
        if s["meets_pct"] is not None:
            parts.append(f"meets target <b>{s['meets_pct']:.1f} %</b>")
        if s["min"] is not None:
            parts.append(f"minimum DOL {s['min']:.2f} m at KP {s['min_kp']:.3f}")
        below = f"<b>below target {s['below_m'] / 1000.0:.3f} km</b> in {len(result.ranges)} range(s)"
        parts.append(f"<span style='{status_style('error')}'>{below}</span>" if result.ranges else below)
        self.summary.setText(" · ".join(parts))
        self.summary.setStyleSheet("")
        notes = list(result.request.notes) + result.notes
        names = [s.spec.name for s in result.series]
        governing = sorted(set(result.selection.source.tolist()))
        if len(names) > 1:
            notes.insert(0, "Governing: " + ", ".join(names[k] for k in governing) + ".")
        self.notes.setText(" ".join(notes))

    def _fill_ranges(self, result: A.Assessment) -> None:
        names = [s.spec.name for s in result.series]
        table = self.ranges_table
        table.setRowCount(len(result.ranges))
        for row, r in enumerate(result.ranges):
            values = [f"{r['kp_from']:.3f}", f"{r['kp_to']:.3f}", f"{r['length_m']:.1f}", f"{r['min']:.2f}",
                      f"{r['min_kp']:.3f}", f"{r['max_shortfall']:.2f}", ", ".join(names[k] for k in r["sources"])]
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(value))
        table.resizeColumnsToContents()

    def _fill_sections(self, result: A.Assessment) -> None:
        table = self.sections_table
        table.setRowCount(len(result.sections))
        for row, s in enumerate(result.sections):
            values = [s.get("kind", "").replace("_", " "), f"{s['start_kp']:.3f}", f"{s['end_kp']:.3f}",
                      f"{s['assessed_m']:.1f}", f"{s['no_data_m']:.1f}",
                      "" if s["meets_pct"] is None else f"{s['meets_pct']:.1f}", f"{s['below_m']:.1f}",
                      str(s.get("ranges", 0)), "" if s["min"] is None else f"{s['min']:.2f}"]
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(value))
        table.resizeColumnsToContents()

    def _listing(self, limit: Optional[int] = None):
        if self.result is None:
            return None, None
        channels = [R.WATER_DEPTH] + [k for s in self.result.series for k in s.channels if k.startswith("tension:")]
        channels = list(dict.fromkeys(channels))
        return A.listing_rows(self.result, self.listing_step.currentData(), self.listing_method.currentData(),
                              channels, limit)

    def _fill_listing(self, *_args) -> None:
        header, shown = self._listing(LISTING_PREVIEW)
        table = self.listing_table
        if header is None:
            table.setRowCount(0)
            return
        table.setColumnCount(len(header))
        table.setHorizontalHeaderLabels(header)
        table.setRowCount(len(shown))
        for r, row in enumerate(shown):
            for c, value in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(str(value)))
        table.resizeColumnsToContents()
        total = A.listing_count(self.result, self.listing_step.currentData())
        method = "the shallowest DOL" if self.listing_method.currentData() == B.METHOD_WORST else "the mean DOL"
        self.listing_note.setText(f"{total:,} rows ({len(shown):,} shown; Export CSV writes them all): each row "
                                  f"gives {method} over its interval and the record behind it.")

    def _export_listing(self) -> None:
        header, rows = self._listing()
        if header is None:
            return
        step = int(self.listing_step.currentData())
        path, _filter = QFileDialog.getSaveFileName(self, "Export the listing", f"burial_listing_{step}m.csv",
                                                    "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(rows)
        except OSError as exc:
            QMessageBox.warning(self, "Export the listing", f"Could not write {path}: {exc.strerror or exc}")

    # -- hover, clicks, links ---------------------------------------------------------------
    def _describe(self, kp: float) -> Optional[Dict]:
        return A.provenance(self.result, kp) if self.result is not None else None

    def _readout(self, kp: float) -> str:
        info = self._describe(kp)
        if info is None:
            return f"KP {kp:.3f}: no data"
        text = f"KP {kp:.3f}: DOL {info['dol']:.2f} m (shallowest in this metre)"
        if info["target"] is not None:
            diff = info["dol"] - info["target"]
            text += f" · target {info['target']:.2f} m ({'+' if diff >= 0 else ''}{diff:.2f} m)"
        text += (f" · {info['dataset']}: {info['file']} line {info['line']}"
                 + (f", {info['time']} UTC" if info["time"] else ""))
        if info["others"]:
            text += f" · {info['others']} other dataset(s) here"
        return text

    def _tip(self, kp: float) -> str:
        info = self._describe(kp)
        if info is None:
            return ""
        status = B.STATUS_NAMES.get(info["status"], "")
        target = f"<br>Target {info['target']:.2f} m · {status}" if info["target"] is not None else ""
        return (f"<b>KP {kp:.3f}</b> · DOL {info['dol']:.2f} m{target}<br>{info['dataset']}<br>"
                f"{info['file']} line {info['line']}" + (f"<br>{info['time']} UTC" if info["time"] else ""))

    def _band_text(self, key: str, lo: float, hi: float, level: int, _kp: float) -> str:
        length = (hi - lo) * 1000.0
        if key == "source":
            if level < 0 or self.result is None or level >= len(self.result.series):
                return f"KP {lo:.3f} to {hi:.3f}: no data ({length:,.0f} m)"
            return f"KP {lo:.3f} to {hi:.3f}: {self.result.series[level].spec.name} ({length:,.0f} m)"
        names = {lv: name for lv, _c, name in STATUS_LEVELS}
        return f"KP {lo:.3f} to {hi:.3f}: {names.get(level, '')} ({length:,.0f} m)\nClick to zoom to it"

    def _hovered(self, kp: float) -> None:
        if self.dock is None or not math.isfinite(kp):
            return
        try:
            self.dock.highlight_kp(kp)
        except Exception:
            log_exception("Burial Planner: assessment map link failed")

    def _clicked(self, kp: float) -> None:
        if self.dock is not None:
            try:
                self.dock.goto_kp(kp)
            except Exception:
                log_exception("Burial Planner: assessment map link failed")

    def _zoom_range(self, lo: float, hi: float) -> None:
        self.stack.zoom_to(lo, hi)
        if self.dock is not None:
            try:
                self.dock.highlight_range(lo, hi)
            except Exception:
                log_exception("Burial Planner: assessment map link failed")

    def _range_clicked(self, row: int, go: bool = False) -> None:
        if self.result is None or not 0 <= row < len(self.result.ranges):
            return
        r = self.result.ranges[row]
        self._zoom_range(r["kp_from"], r["kp_to"])
        if go and self.dock is not None:
            self.dock.goto_range(r["kp_from"], r["kp_to"])

    def _section_clicked(self, row: int) -> None:
        if self.result is None or not 0 <= row < len(self.result.sections):
            return
        s = self.result.sections[row]
        self.stack.zoom_to(s["start_kp"], s["end_kp"])
        if self.dock is not None:
            self.dock.goto_range(s["start_kp"], s["end_kp"])

    def _context_actions(self, kp: float, _scene_pos):
        info = self._describe(kp)
        actions = [(f"Go to KP {kp:.3f} on the map", lambda: self._clicked(kp), self.dock is not None)]
        if info is not None:
            actions.append((f"Record: {info['file']} line {info['line']}", None, False))
        return actions

    def shutdown(self) -> None:
        self._timer.stop()
