# -*- coding: utf-8 -*-
"""Configurable, professional-grade plot panel backed by bundled pyqtgraph.

Each panel can plot several Y series (with per-series left/right axis, colour and
legend), against record order, time, KP or any numeric field. Extras built for
cable-lay analysis / investigation work:

* synced vertical crosshair + nearest-point value readout across every panel
  (matched *by record*, so it lines up even when panels use different X axes),
* live statistics for the visible X range (n / min / max / mean / std / slope),
* optional dY/dX slope series on a secondary axis,
* QC-findings overlay (vertical markers coloured by severity),
* per-source filtering,
* X-range selection that selects the underlying records on the map + table,
* CSV export of the plotted series and one-click record highlighting on click.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...plot_widget import Figure, FigureCanvas, NavigationToolbar, get_tab10_color

_MAX_PLOT_POINTS = 40000
_X_RECORD_ORDER = "(record order)"
_X_TIME = "__time__"
_DASH_LINE = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
_INSTANT_POPUP = getattr(getattr(QToolButton, "ToolButtonPopupMode", QToolButton), "InstantPopup")

_SEVERITY_COLORS = {"ERROR": (214, 39, 40), "WARNING": (255, 140, 0), "INFO": (31, 119, 180)}
_STRONG_FOCUS = getattr(getattr(Qt, "FocusPolicy", Qt), "StrongFocus")
_KEYS = getattr(Qt, "Key", Qt)
_KEY_LEFT = getattr(_KEYS, "Key_Left")
_KEY_RIGHT = getattr(_KEYS, "Key_Right")
_KEY_ESCAPE = getattr(_KEYS, "Key_Escape")


class PlotPanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._dataset = None

        # Configuration state (persisted).
        self._name: Optional[str] = None
        self._x_key = _X_RECORD_ORDER
        # Each series: {"layer": layer_id or None (primary), "field": str, "axis": "left"/"right"}
        self._series: List[Dict[str, object]] = []
        self._source_filter: Optional[str] = None
        self._show_derivative = False
        self._show_legend = True
        self._show_qc = False
        self._show_events = False
        self._select_mode = False
        # Extra (non-plotted) fields of the active layer shown in the hover
        # tooltip, e.g. KP while plotting depth against time.
        self._tooltip_fields: List[str] = []
        self._context_row: Optional[int] = None  # record under the last right-click
        self._pinned = False  # click pins the tooltip; click again / Esc releases

        # Render state (rebuilt on every replot).
        self._axis = None
        self._twin = None
        self._x_full: Optional[np.ndarray] = None  # reference X (primary dataset), sorted
        self._rows_full: Optional[np.ndarray] = None  # primary source rows, sorted by X
        self._pos_for_row: Optional[np.ndarray] = None
        self._t_sorted: Optional[np.ndarray] = None  # primary time (sorted) for time->X mapping
        self._x_at_t: Optional[np.ndarray] = None
        self._x_label = ""
        self._drawn: List[dict] = []  # per-series render info (may span layers)
        self._vline = None
        self._label = None
        self._region = None
        self._last_hover_row = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("X:"))
        self.x_combo = QComboBox()
        self.x_combo.currentIndexChanged.connect(self._on_x_changed)
        controls.addWidget(self.x_combo, 1)

        self.series_button = QToolButton()
        self.series_button.setText("Y series \u25be")
        self.series_button.setPopupMode(_INSTANT_POPUP)
        self.series_menu = QMenu(self.series_button)
        self.series_button.setMenu(self.series_menu)
        controls.addWidget(self.series_button)

        controls.addWidget(QLabel("Source:"))
        self.source_combo = QComboBox()
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        controls.addWidget(self.source_combo, 1)

        self.fit_y_button = QToolButton()
        self.fit_y_button.setText("Fit Y")
        self.fit_y_button.setToolTip("Rescale the Y axis to fit the data in the current X view")
        self.fit_y_button.clicked.connect(self._autoscale_y_visible)
        controls.addWidget(self.fit_y_button)

        self.options_button = QToolButton()
        self.options_button.setText("\u22ef")
        self.options_button.setPopupMode(_INSTANT_POPUP)
        self.options_button.setToolTip("Plot options")
        self.options_menu = QMenu(self.options_button)
        self.options_button.setMenu(self.options_menu)
        self._build_options_menu()
        controls.addWidget(self.options_button)
        layout.addLayout(controls)

        self.figure = Figure()
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, self)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.toolbar)

        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color:#444; font-size:11px;")
        layout.addWidget(self.stats_label)

        self.canvas.mpl_connect("button_press_event", self._on_click)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        # Keyboard: Left/Right step through records, Esc clears (see keyPressEvent).
        self.setFocusPolicy(_STRONG_FOCUS)
        self.canvas.setFocusPolicy(_STRONG_FOCUS)
        self.canvas.setFocusProxy(self)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        key = event.key()
        if key == _KEY_LEFT:
            self.controller.step_record(-1)
        elif key == _KEY_RIGHT:
            self.controller.step_record(1)
        elif key == _KEY_ESCAPE:
            self.controller.escape()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # -- options menu ------------------------------------------------------
    def _build_options_menu(self) -> None:
        menu = self.options_menu
        menu.clear()

        rename = menu.addAction("Rename plot\u2026")
        rename.triggered.connect(lambda: self.controller.rename_panel(self))

        self._legend_action = menu.addAction("Show legend")
        self._legend_action.setCheckable(True)
        self._legend_action.setChecked(self._show_legend)
        self._legend_action.toggled.connect(self._on_legend_toggled)

        self._deriv_action = menu.addAction("Show dY/dX slope")
        self._deriv_action.setCheckable(True)
        self._deriv_action.setChecked(self._show_derivative)
        self._deriv_action.toggled.connect(self._on_derivative_toggled)

        self._qc_action = menu.addAction("Show QC findings")
        self._qc_action.setCheckable(True)
        self._qc_action.setChecked(self._show_qc)
        self._qc_action.toggled.connect(self._on_qc_toggled)

        self._events_action = menu.addAction("Show events")
        self._events_action.setCheckable(True)
        self._events_action.setChecked(self._show_events)
        self._events_action.toggled.connect(self._on_events_toggled)

        self._select_action = menu.addAction("X-range select mode")
        self._select_action.setCheckable(True)
        self._select_action.setChecked(self._select_mode)
        self._select_action.toggled.connect(self._on_select_toggled)

        self.tooltip_menu = menu.addMenu("Tooltip fields")
        self.tooltip_menu.setToolTip(
            "Extra fields of the active layer to show in the hover tooltip "
            "without plotting them (the plotted series are always shown)."
        )
        self.tooltip_menu.triggered.connect(self._on_tooltip_field_toggled)
        self._rebuild_tooltip_menu()

        menu.addSeparator()
        self._float_action = menu.addAction("Pop out / dock this plot")
        self._float_action.triggered.connect(lambda: self.controller.toggle_float_panel(self))
        export = menu.addAction("Export plotted data to CSV\u2026")
        export.triggered.connect(self._export_csv)
        reset = menu.addAction("Reset view")
        reset.triggered.connect(self._reset_view)

    def _rebuild_tooltip_menu(self) -> None:
        """One checkable entry per field of the active layer (tooltip extras)."""
        menu = getattr(self, "tooltip_menu", None)
        if menu is None:
            return
        menu.clear()
        dataset = self._dataset
        if dataset is None:
            menu.addAction("(load a layer first)").setEnabled(False)
            return
        clear = menu.addAction("None (plotted series only)")
        clear.setData("__clear__")
        menu.addSeparator()
        for name in dataset.field_names:
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name in self._tooltip_fields)
            action.setData(name)

    def _on_tooltip_field_toggled(self, action) -> None:
        key = action.data()
        if key == "__clear__":
            self._tooltip_fields = []
        elif isinstance(key, str):
            if action.isChecked():
                if key not in self._tooltip_fields:
                    self._tooltip_fields.append(key)
            else:
                self._tooltip_fields = [f for f in self._tooltip_fields if f != key]
        self._rebuild_tooltip_menu()
        self._last_hover_row = None  # force the label to redraw on next hover

    def _tooltip_extra_lines(self, source_row: int) -> List[str]:
        dataset = self._dataset
        if dataset is None or not self._tooltip_fields:
            return []
        lines: List[str] = []
        for field in self._tooltip_fields:
            if field not in dataset.columns:
                continue
            if dataset.is_numeric_field(field):
                value = dataset.numeric(field)[source_row]
                text = f"{float(value):.6g}" if np.isfinite(value) else "-"
            else:
                raw = dataset.columns[field][source_row]
                text = "-" if raw is None else str(raw)
            lines.append(f"{field}: {text}")
        return lines

    # -- dataset / fields --------------------------------------------------
    def _numeric_fields(self) -> List[str]:
        if self._dataset is None:
            return []
        return [name for name in self._dataset.field_names if self._dataset.is_numeric_field(name)]

    def set_dataset(self, dataset) -> None:
        self._dataset = dataset
        numeric = self._numeric_fields()

        self.x_combo.blockSignals(True)
        self.x_combo.clear()
        self.x_combo.addItem(_X_RECORD_ORDER, _X_RECORD_ORDER)
        if dataset is not None and dataset.time_field is not None:
            self.x_combo.addItem(f"{dataset.time_field} (time)", _X_TIME)
        for name in numeric:
            self.x_combo.addItem(name, name)
        self._select_x_in_combo()
        self.x_combo.blockSignals(False)

        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("(all sources)", None)
        if dataset is not None:
            for source in dataset.sources():
                self.source_combo.addItem(source, source)
        idx = self.source_combo.findData(self._source_filter)
        self.source_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.source_combo.blockSignals(False)

        # Keep only series whose (primary) field still exists; default a Y.
        primary_id = self.controller.primary_layer_id()
        self._series = [s for s in self._series if self._series_still_valid(s, primary_id, numeric)]
        if not self._series and numeric:
            self._series = [{"layer": None, "field": self._default_y(numeric), "axis": "left"}]
        self._rebuild_series_menu()
        self._rebuild_tooltip_menu()
        self.replot()

    def refresh_sources(self) -> None:
        """Rebuild the multi-layer series menu after plot layers change."""
        self._rebuild_series_menu()
        self.replot()

    def _series_still_valid(self, series, primary_id, primary_numeric) -> bool:
        layer = series.get("layer")
        if layer is None or layer == primary_id:
            return series.get("field") in primary_numeric
        dataset = self.controller.dataset_for(layer)
        return dataset is not None and series.get("field") in dataset.columns

    def _select_x_in_combo(self) -> None:
        index = self.x_combo.findData(self._x_key)
        if index >= 0:
            self.x_combo.setCurrentIndex(index)
            return
        # Default X: prefer a KP field, then time, then record order.
        for i in range(self.x_combo.count()):
            data = self.x_combo.itemData(i)
            if isinstance(data, str) and "kp" in data.lower():
                self.x_combo.setCurrentIndex(i)
                self._x_key = data
                return
        time_index = self.x_combo.findData(_X_TIME)
        if time_index >= 0:
            self.x_combo.setCurrentIndex(time_index)
            self._x_key = _X_TIME
            return
        self.x_combo.setCurrentIndex(0)
        self._x_key = _X_RECORD_ORDER

    @staticmethod
    def _default_y(numeric: List[str]) -> str:
        preferred = ("Bottom Tension(kN)", "Depth (m)", "Water_Depth_m", "Seabed_slack")
        for name in preferred:
            if name in numeric:
                return name
        return numeric[0]

    # -- series menu -------------------------------------------------------
    def _rebuild_series_menu(self) -> None:
        self.series_menu.clear()
        sources = self.controller.plot_sources()
        primary_id = self.controller.primary_layer_id()
        axis_of = {(self._norm_layer(s.get("layer"), primary_id), s.get("field")): s.get("axis", "left")
                   for s in self._series}
        multi = len(sources) > 1
        for layer_id, name, dataset in sources:
            numeric = [f for f in dataset.field_names if dataset.is_numeric_field(f)]
            if not numeric:
                continue
            parent = self.series_menu.addMenu(name) if multi else self.series_menu
            key_layer = self._norm_layer(layer_id, primary_id)
            for field in numeric:
                sub = parent.addMenu(field)
                current = axis_of.get((key_layer, field))
                for label, value in (("Off", None), ("Left axis", "left"), ("Right axis", "right")):
                    action = sub.addAction(label)
                    action.setCheckable(True)
                    action.setChecked(current == value)
                    action.triggered.connect(
                        lambda _checked=False, l=key_layer, f=field, v=value: self._set_series_axis(l, f, v)
                    )
        self._update_series_button()

    @staticmethod
    def _norm_layer(layer_id, primary_id):
        """Store the primary layer as None so configs stay portable."""
        return None if (layer_id is None or layer_id == primary_id) else layer_id

    def _update_series_button(self) -> None:
        count = len(self._series)
        self.series_button.setText(f"Y series ({count}) \u25be" if count else "Y series \u25be")

    def _set_series_axis(self, layer, field: str, axis: Optional[str]) -> None:
        self._series = [
            s for s in self._series if not (s.get("layer") == layer and s.get("field") == field)
        ]
        if axis in ("left", "right"):
            self._series.append({"layer": layer, "field": field, "axis": axis})
        self._rebuild_series_menu()
        self.replot()

    # -- config persistence ------------------------------------------------
    def get_config(self) -> dict:
        return {
            "name": self._name,
            "x": self._x_key,
            "source": self._source_filter,
            "derivative": self._show_derivative,
            "legend": self._show_legend,
            "qc": self._show_qc,
            "events": self._show_events,
            "series": [dict(s) for s in self._series],
            "tooltip_fields": list(self._tooltip_fields),
        }

    def apply_config(self, config: Optional[dict]) -> None:
        config = config or {}
        self._name = config.get("name")
        self._x_key = config.get("x", self._x_key)
        self._source_filter = config.get("source")
        self._show_derivative = bool(config.get("derivative", False))
        self._show_legend = bool(config.get("legend", True))
        self._show_qc = bool(config.get("qc", False))
        self._show_events = bool(config.get("events", False))
        fields = config.get("tooltip_fields")
        self._tooltip_fields = [f for f in fields if isinstance(f, str)] if isinstance(fields, list) else []
        series = config.get("series")
        if series is None and config.get("y"):
            series = [{"layer": None, "field": config["y"], "axis": "left"}]
        if isinstance(series, list):
            self._series = [
                {"layer": s.get("layer"), "field": s.get("field"), "axis": s.get("axis", "left")}
                for s in series
                if isinstance(s, dict) and s.get("field")
            ]
        self._build_options_menu()

    def display_name(self) -> Optional[str]:
        return self._name

    def set_display_name(self, name: Optional[str]) -> None:
        self._name = name

    # -- option handlers ---------------------------------------------------
    def _on_x_changed(self, *_a) -> None:
        self._x_key = self.x_combo.currentData()
        self.replot()

    def _on_source_changed(self, *_a) -> None:
        self._source_filter = self.source_combo.currentData()
        self.replot()

    def _on_legend_toggled(self, checked: bool) -> None:
        self._show_legend = bool(checked)
        self.replot()

    def _on_derivative_toggled(self, checked: bool) -> None:
        self._show_derivative = bool(checked)
        self.replot()

    def _on_qc_toggled(self, checked: bool) -> None:
        self._show_qc = bool(checked)
        self.replot()

    def _on_events_toggled(self, checked: bool) -> None:
        self._show_events = bool(checked)
        self.replot()

    def _on_select_toggled(self, checked: bool) -> None:
        self._select_mode = bool(checked)
        self.replot()

    def set_x_by_kind(self, kind: str) -> None:
        """Set X to a shared kind (used by the window's 'All X' control)."""
        if kind == "time":
            target = _X_TIME
        elif kind == "record":
            target = _X_RECORD_ORDER
        elif kind == "kp":
            target = None
            for i in range(self.x_combo.count()):
                data = self.x_combo.itemData(i)
                if isinstance(data, str) and "kp" in data.lower():
                    target = data
                    break
            if target is None:
                return
        else:
            return
        index = self.x_combo.findData(target)
        if index >= 0:
            self.x_combo.setCurrentIndex(index)

    def view_box(self):
        if self._axis is None:
            return None
        try:
            return self._axis.plot_item.vb
        except Exception:
            return None

    # -- plotting ----------------------------------------------------------
    def replot(self, *_args) -> None:
        self.figure.clear()
        axis = self.figure.add_subplot(111)
        self._axis = axis
        self._twin = None
        self._x_full = None
        self._rows_full = None
        self._pos_for_row = None
        self._t_sorted = None
        self._x_at_t = None
        self._drawn = []
        self._vline = None
        self._label = None
        self._region = None
        self._last_hover_row = None

        dataset = self._dataset
        if dataset is None or not self._series:
            self.stats_label.setText("")
            self.canvas.draw_idle()
            self.controller.on_panel_replotted(self)
            return

        x, x_label = self._compute_x()
        mask = self._base_mask() & np.isfinite(x)
        rows = np.nonzero(mask)[0].astype(np.int64)
        if rows.size == 0:
            self.stats_label.setText("no records for this source/X")
            self.canvas.draw_idle()
            self.controller.on_panel_replotted(self)
            return
        xr = x[rows]
        order = np.argsort(xr, kind="stable")
        rows = rows[order]
        xr = xr[order]

        self._x_full = xr
        self._rows_full = rows
        self._x_label = x_label
        self._pos_for_row = np.full(dataset.row_count, -1, dtype=np.int64)
        self._pos_for_row[rows] = np.arange(rows.size, dtype=np.int64)
        self._build_time_map(dataset, rows, xr)

        has_right = any(s.get("axis") == "right" for s in self._series) or self._show_derivative
        twin = axis.twinx() if has_right else None
        self._twin = twin

        left_fields: List[str] = []
        right_fields: List[str] = []
        legend_entries: List[tuple] = []
        for i, series in enumerate(self._series):
            ds = self._series_dataset(series)
            field = series.get("field")
            if ds is None or field not in ds.columns:
                continue
            is_primary = ds is dataset
            if is_primary:
                ex = xr
                ey = ds.numeric(field)[rows]
            else:
                res = self._secondary_xy(ds, field)
                if res is None:
                    continue
                ex, ey = res
            color = get_tab10_color(i)
            target = twin if (series.get("axis") == "right" and twin is not None) else axis
            finite = np.isfinite(ex) & np.isfinite(ey)
            if np.any(finite):
                px, py = self._decimate(ex[finite], ey[finite])
                target.plot(px, py, color=color)
            dot = pg.ScatterPlotItem(size=11, pen=pg.mkPen("w", width=1), brush=pg.mkBrush(*self._rgb(color)))
            dot.setZValue(21)
            dot.setVisible(False)
            (twin.view_box if target is twin else axis.plot_item).addItem(dot)
            self._drawn.append({
                "layer": series.get("layer"), "field": field, "axis": series.get("axis"),
                "x": ex, "y": ey, "dot": dot, "is_primary": is_primary,
            })
            label = field if is_primary else f"{field} \u2020"
            (right_fields if target is twin else left_fields).append(label)
            legend_entries.append((label + ("  (R)" if target is twin else ""), color))

            if self._show_derivative and twin is not None:
                self._plot_derivative(twin, ex, ey, color, field, legend_entries)

        axis.set_xlabel(x_label)
        axis.set_time_axis(self._x_key == _X_TIME)
        if left_fields:
            axis.set_ylabel(", ".join(left_fields))
        if right_fields and twin is not None:
            twin.set_ylabel(", ".join(right_fields))
        axis.set_title(self._name or "")

        self._install_overlays(axis, legend_entries)
        if self._show_events:
            self._install_events_overlay(axis.plot_item)
        self._connect_range_signal(axis)
        self._update_stats()
        self.canvas.draw_idle()
        self.controller.on_panel_replotted(self)

    # -- cross-layer helpers ----------------------------------------------
    def _series_dataset(self, series):
        layer = series.get("layer")
        if layer is None:
            return self._dataset
        return self.controller.dataset_for(layer)

    def _build_time_map(self, dataset, rows, xr) -> None:
        """Build a primary time -> reference-X lookup for cross-layer alignment."""
        times = dataset.time_epoch
        if times is None:
            return
        t_ref = np.asarray(times, dtype=float)[rows]
        good = np.isfinite(t_ref)
        if np.count_nonzero(good) < 2:
            return
        ts = t_ref[good]
        xs = xr[good]
        o = np.argsort(ts, kind="stable")
        ts = ts[o]
        xs = xs[o]
        uniq = np.concatenate(([True], np.diff(ts) > 0))
        self._t_sorted = ts[uniq]
        self._x_at_t = xs[uniq]

    def _map_time_to_x(self, times):
        if self._x_key == _X_TIME:
            return np.asarray(times, dtype=float)
        if self._t_sorted is None or self._t_sorted.size < 2:
            return np.full(np.shape(times), np.nan)
        return np.interp(np.asarray(times, dtype=float), self._t_sorted, self._x_at_t,
                         left=np.nan, right=np.nan)

    def _event_x(self, t: float):
        if self._x_key == _X_TIME:
            return t
        if self._t_sorted is None or self._t_sorted.size < 2:
            return None
        if t < self._t_sorted[0] or t > self._t_sorted[-1]:
            return None
        return float(np.interp(t, self._t_sorted, self._x_at_t))

    def _secondary_xy(self, ds, field):
        times = ds.time_epoch
        if times is None:
            return None
        xm = self._map_time_to_x(times)
        y = ds.numeric(field)
        finite = np.isfinite(xm) & np.isfinite(y)
        if not np.any(finite):
            return None
        xx = xm[finite]
        yy = y[finite]
        o = np.argsort(xx, kind="stable")
        return xx[o], yy[o]

    def center_on_record(self, source_row: int) -> None:
        """Centre the X view on a primary-dataset record (from a go-to action)."""
        if self._pos_for_row is None or self._x_full is None:
            return
        try:
            pos = int(self._pos_for_row[source_row])
        except (IndexError, ValueError, TypeError):
            return
        if pos < 0:
            return
        self._center_x(float(self._x_full[pos]))

    def center_on_time(self, epoch: float) -> None:
        """Centre the X view on a timestamp (from an event go-to action)."""
        if epoch is None:
            return
        xv = self._event_x(float(epoch))
        if xv is None or not np.isfinite(xv):
            return
        self._center_x(float(xv))

    def _center_x(self, xval: float) -> None:
        vb = self.view_box()
        if vb is None:
            return
        try:
            x0, x1 = vb.viewRange()[0]
            half = (x1 - x0) / 2.0 or 1.0
            vb.setXRange(xval - half, xval + half, padding=0)
        except Exception:
            pass

    def _install_events_overlay(self, plot_item) -> None:
        try:
            events = self.controller.event_records()
        except Exception:
            events = []
        if not events:
            return
        for ev in events:
            t = ev.get("time")
            if t is None or not np.isfinite(float(t)):
                continue
            xv = self._event_x(float(t))
            if xv is None or not np.isfinite(xv):
                continue
            label = str(ev.get("label") or "event")
            try:
                line = pg.InfiniteLine(
                    pos=float(xv), angle=90, movable=False,
                    pen=pg.mkPen((200, 120, 0), width=1, style=_DASH_LINE),
                    label=label,
                    labelOpts={"position": 0.9, "color": (150, 85, 0), "movable": False},
                )
            except Exception:
                line = pg.InfiniteLine(
                    pos=float(xv), angle=90, movable=False,
                    pen=pg.mkPen((200, 120, 0), width=1, style=_DASH_LINE),
                )
            line.setZValue(3)
            plot_item.addItem(line, ignoreBounds=True)

    def _compute_x(self):
        dataset = self._dataset
        if self._x_key == _X_TIME and dataset.time_epoch is not None:
            return np.asarray(dataset.time_epoch, dtype=float), (dataset.time_field or "time")
        if self._x_key and self._x_key not in (_X_RECORD_ORDER, _X_TIME):
            return np.asarray(dataset.numeric(self._x_key), dtype=float), self._x_key
        return np.arange(dataset.row_count, dtype=float), "record order"

    def _base_mask(self) -> np.ndarray:
        dataset = self._dataset
        if not self._source_filter:
            return np.ones(dataset.row_count, dtype=bool)
        source = dataset.source_array
        return np.array([str(s) == self._source_filter for s in source], dtype=bool)

    def _plot_derivative(self, twin, x, y, color, field, legend_entries) -> None:
        finite = np.isfinite(x) & np.isfinite(y)
        if np.count_nonzero(finite) < 2:
            return
        xf = x[finite]
        yf = y[finite]
        dx = np.diff(xf)
        dx[dx == 0] = np.nan
        slope = np.diff(yf) / dx
        xs = (xf[:-1] + xf[1:]) / 2.0
        good = np.isfinite(slope)
        if not np.any(good):
            return
        px, py = self._decimate(xs[good], slope[good])
        twin.plot(px, py, color=color, linestyle="--")
        legend_entries.append((f"d({field})/dX", color))

    def _install_overlays(self, axis, legend_entries) -> None:
        plot_item = axis.plot_item
        self._vline = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen((120, 120, 120), width=1, style=_DASH_LINE)
        )
        self._vline.setZValue(20)
        self._vline.setVisible(False)
        plot_item.addItem(self._vline, ignoreBounds=True)

        self._label = pg.TextItem(color=(10, 10, 10), anchor=(0, 1), fill=pg.mkBrush(255, 255, 255, 220))
        self._label.setZValue(22)
        self._label.setVisible(False)
        plot_item.addItem(self._label)

        # Right-click: pyqtgraph's own view menu gains a "go to" entry for the
        # record under the cursor (recorded by _on_click on the right button).
        try:
            vb_menu = plot_item.vb.menu
            if vb_menu is not None and not getattr(vb_menu, "_sct_goto_added", False):
                vb_menu.addSeparator()
                goto = vb_menu.addAction("Go to this record (map + table + plots)")
                goto.triggered.connect(self._go_to_context_record)
                vb_menu._sct_goto_added = True
        except Exception:
            pass

        if self._show_legend and len(legend_entries) > 1:
            legend = plot_item.addLegend(offset=(10, 10))
            for name, color in legend_entries:
                legend.addItem(pg.PlotDataItem(pen=pg.mkPen(self._rgb(color), width=2)), name)

        if self._show_qc:
            self._install_qc_overlay(plot_item)

        if self._select_mode:
            self._region = pg.LinearRegionItem()
            self._region.setZValue(5)
            plot_item.addItem(self._region)
            self._region.sigRegionChangeFinished.connect(self._on_region_changed)

    def _install_qc_overlay(self, plot_item) -> None:
        findings = self.controller.current_findings()
        if not findings or self._pos_for_row is None:
            return
        for finding in findings:
            fid = getattr(finding, "feature_fid", None)
            if fid is None:
                continue
            row = self.controller.row_for_fid(int(fid))
            if row is None:
                continue
            pos = int(self._pos_for_row[row])
            if pos < 0:
                continue
            severity = getattr(getattr(finding, "severity", None), "name", str(getattr(finding, "severity", "")))
            color = _SEVERITY_COLORS.get(str(severity).upper(), (150, 150, 150))
            line = pg.InfiniteLine(pos=float(self._x_full[pos]), angle=90, pen=pg.mkPen(color, width=1))
            line.setZValue(4)
            plot_item.addItem(line, ignoreBounds=True)

    def _connect_range_signal(self, axis) -> None:
        try:
            axis.plot_item.vb.sigXRangeChanged.connect(self._update_stats)
        except Exception:
            pass

    @staticmethod
    def _rgb(color):
        qcolor = pg.mkColor(color)
        return qcolor.red(), qcolor.green(), qcolor.blue()

    @staticmethod
    def _decimate(x: np.ndarray, y: np.ndarray):
        if x.size <= _MAX_PLOT_POINTS:
            return x, y
        step = int(np.ceil(x.size / _MAX_PLOT_POINTS))
        return x[::step], y[::step]

    # -- statistics --------------------------------------------------------
    def _update_stats(self, *_a) -> None:
        if not self._drawn or self._x_full is None or self._axis is None:
            self.stats_label.setText("")
            return
        try:
            x0, x1 = self._axis.plot_item.vb.viewRange()[0]
        except Exception:
            x0, x1 = self._x_full.min(), self._x_full.max()
        primary = self._drawn[0]
        y = np.asarray(primary["y"], dtype=float)
        x = np.asarray(primary["x"], dtype=float)
        m = np.isfinite(y) & (x >= x0) & (x <= x1)
        ys = y[m]
        xs = x[m]
        if ys.size == 0:
            self.stats_label.setText(f"{primary['field']}: no points in view")
            return
        slope = np.nan
        if xs.size > 1 and np.ptp(xs) > 0:
            try:
                slope = float(np.polyfit(xs, ys, 1)[0])
            except Exception:
                slope = np.nan
        self.stats_label.setText(
            f"{primary['field']}:  n={ys.size}  min={ys.min():.4g}  max={ys.max():.4g}  "
            f"mean={ys.mean():.4g}  std={ys.std():.4g}  \u0394x={np.ptp(xs):.4g}  slope={slope:.4g}"
        )

    # -- selection / export ------------------------------------------------
    def _on_region_changed(self) -> None:
        if self._region is None or self._rows_full is None:
            return
        lo, hi = self._region.getRegion()
        m = (self._x_full >= lo) & (self._x_full <= hi)
        rows = [int(r) for r in self._rows_full[m]]
        if rows:
            self.controller.select_rows(rows)

    def _export_csv(self) -> None:
        if self._x_full is None or not self._drawn:
            return
        path, _flt = QFileDialog.getSaveFileName(self, "Export plotted data", "", "CSV files (*.csv)")
        if not path:
            return
        header = [self._x_label] + [s["field"] for s in self._drawn]
        columns = [self._x_full] + [self._value_at_ref(s) for s in self._drawn]
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(",".join(str(h) for h in header) + "\n")
                for i in range(self._x_full.size):
                    handle.write(",".join(self._csv_value(col, i) for col in columns) + "\n")
        except OSError:
            pass

    def _value_at_ref(self, entry):
        """Resample a series onto the reference-X grid (nearest neighbour)."""
        if entry.get("is_primary"):
            return np.asarray(entry["y"], dtype=float)
        ex = np.asarray(entry["x"], dtype=float)
        ey = np.asarray(entry["y"], dtype=float)
        if ex.size == 0:
            return np.full(self._x_full.size, np.nan)
        idx = np.clip(np.searchsorted(ex, self._x_full), 1, ex.size - 1)
        left = idx - 1
        choose_left = (self._x_full - ex[left]) <= (ex[idx] - self._x_full)
        nearest = np.where(choose_left, left, idx)
        return ey[nearest]

    @staticmethod
    def _csv_value(column, i) -> str:
        value = column[i]
        if isinstance(value, float) and not np.isfinite(value):
            return ""
        return str(value)

    def _reset_view(self) -> None:
        if self._axis is not None:
            try:
                self._axis.plot_item.vb.autoRange()
                if self._twin is not None:
                    self._twin.view_box.autoRange()
            except Exception:
                pass

    def _autoscale_y_visible(self) -> None:
        """Rescale each Y axis to fit the data within the current X view."""
        if self._axis is None or self._x_full is None or not self._drawn:
            return
        try:
            x0, x1 = self._axis.plot_item.vb.viewRange()[0]
        except Exception:
            return
        self._fit_axis_y(self._axis.view_box, [s for s in self._drawn if s.get("axis") != "right"], x0, x1)
        if self._twin is not None:
            self._fit_axis_y(self._twin.view_box, [s for s in self._drawn if s.get("axis") == "right"], x0, x1)
        self.canvas.draw_idle()

    @staticmethod
    def _fit_axis_y(view_box, series, x0, x1) -> None:
        values = []
        for entry in series:
            x = np.asarray(entry["x"], dtype=float)
            y = np.asarray(entry["y"], dtype=float)
            m = (x >= x0) & (x <= x1) & np.isfinite(y)
            values.append(y[m])
        if not values:
            return
        stacked = np.concatenate(values) if len(values) > 1 else values[0]
        if stacked.size == 0:
            return
        low = float(np.min(stacked))
        high = float(np.max(stacked))
        if low == high:
            pad = abs(low) * 0.05 or 1.0
            low, high = low - pad, high + pad
        try:
            view_box.setYRange(low, high, padding=0.05)
        except Exception:
            pass

    # -- interaction -------------------------------------------------------
    def _nearest_index(self, x_value: float) -> int:
        x = self._x_full
        pos = int(np.searchsorted(x, x_value))
        if pos <= 0:
            return 0
        if pos >= x.size:
            return x.size - 1
        before = pos - 1
        return before if (x_value - x[before]) <= (x[pos] - x_value) else pos

    @staticmethod
    def _nearest_in(arr, value: float):
        arr = np.asarray(arr, dtype=float)
        if arr.size == 0:
            return None
        pos = int(np.searchsorted(arr, value))
        if pos <= 0:
            return 0
        if pos >= arr.size:
            return arr.size - 1
        return pos - 1 if (value - arr[pos - 1]) <= (arr[pos] - value) else pos

    def _on_click(self, event) -> None:
        if event.xdata is None or self._x_full is None or self._x_full.size == 0:
            return
        idx = self._nearest_index(float(event.xdata))
        source_row = int(self._rows_full[idx])
        if getattr(event, "button", 1) == 3:
            # Right button: remember the record for the view menu's "Go to".
            self._context_row = source_row
            return
        self.setFocus()
        if getattr(event, "dblclick", False):
            self._pinned = False
            self.controller.go_to_record(source_row)
            return
        if self._pinned:
            # Second click releases the pinned tooltip; hover follows the mouse again.
            self._pinned = False
            self.set_hover(source_row, force=True)
            return
        self.controller.highlight_record(source_row, from_plot=True)
        self._pinned = True
        self.set_hover(source_row, force=True)
        self.controller.broadcast_hover(source_row, origin=self)

    def unpin(self) -> None:
        if self._pinned:
            self._pinned = False
            self._last_hover_row = None

    @property
    def pinned(self) -> bool:
        return self._pinned

    def _go_to_context_record(self) -> None:
        if self._context_row is not None:
            self.controller.go_to_record(int(self._context_row))

    def _on_motion(self, event) -> None:
        if self._pinned:
            return
        if event.xdata is None or self._x_full is None or self._x_full.size == 0:
            return
        idx = self._nearest_index(float(event.xdata))
        source_row = int(self._rows_full[idx])
        self.set_hover(source_row)
        self.controller.broadcast_hover(source_row, origin=self)

    def set_hover(self, source_row: int, force: bool = False) -> None:
        if self._pos_for_row is None or self._vline is None:
            return
        if self._pinned and not force:
            return  # a pinned tooltip ignores other panels' hover
        if source_row == self._last_hover_row and not force:
            return
        self._last_hover_row = source_row
        if source_row < 0 or source_row >= self._pos_for_row.size:
            self._set_overlay_visible(False)
            self.canvas.draw_idle()
            return
        pos = int(self._pos_for_row[source_row])
        if pos < 0:
            self._set_overlay_visible(False)
            self.canvas.draw_idle()
            return
        x = float(self._x_full[pos])
        self._vline.setPos(x)
        lines = [f"{self._x_label}: {self._format_x(x)}"]
        label_y = None
        for series in self._drawn:
            if series.get("is_primary"):
                raw = series["y"][pos]
            else:
                idx = self._nearest_in(series["x"], x)
                raw = series["y"][idx] if idx is not None else np.nan
            yv = float(raw) if np.isfinite(raw) else np.nan
            series["dot"].setData([x], [yv] if np.isfinite(yv) else [])
            series["dot"].setVisible(bool(np.isfinite(yv)))  # PyQt6 rejects numpy.bool
            lines.append(f"{series['field']}: {yv:.4g}" if np.isfinite(yv) else f"{series['field']}: -")
            if label_y is None and np.isfinite(yv) and series.get("axis") != "right":
                label_y = yv
        lines.extend(self._tooltip_extra_lines(source_row))
        if self._pinned:
            lines.append("(pinned - click to release)")
        self._label.setText("\n".join(lines))
        if label_y is None:
            label_y = 0.0
        self._label.setPos(x, label_y)
        self._vline.setVisible(True)
        self._label.setVisible(True)
        self.canvas.draw_idle()

    def clear_hover(self) -> None:
        self._pinned = False
        self._last_hover_row = None
        self._set_overlay_visible(False)
        self.canvas.draw_idle()

    def _set_overlay_visible(self, visible: bool) -> None:
        for item in (self._vline, self._label):
            if item is not None:
                item.setVisible(visible)
        for series in self._drawn:
            dot = series.get("dot")
            if dot is not None:
                dot.setVisible(False)

    def _format_x(self, x: float) -> str:
        if self._x_key == _X_TIME:
            try:
                return datetime.utcfromtimestamp(x).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OverflowError, OSError):
                return f"{x:.2f}"
        return f"{x:.6g}"
