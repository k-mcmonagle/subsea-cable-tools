# -*- coding: utf-8 -*-
"""Time-series plots for operation runs (tensions, balance, BU descent).

Consumes ``timeline.Snapshot`` lists via the pure helper
:func:`snapshots_to_series` (unit-testable without Qt); renders through the
plugin's pyqtgraph-backed plot shim like the other 2D views.

The stacked panels share one x axis (pan/zoom on any panel moves them all),
carry a "Fit Y" action that rescales each panel to the data inside the
visible time window (optionally on every zoom), and a hover crosshair that
snaps to the nearest sample, marks the value on every trace and reports the
values in a readout line plus a tooltip. A separate grey cursor tracks the
timeline scrubber.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .views2d import _ShimHolder, make_canvas

try:
    from qgis.PyQt.QtWidgets import (
        QCheckBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
        QWidget,
    )
except Exception:  # pragma: no cover - standalone testing
    try:
        from PyQt5.QtWidgets import (
            QCheckBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
            QVBoxLayout, QWidget,
        )
    except Exception:  # pragma: no cover - no Qt at all (pure-helper tests)
        QWidget = None  # type: ignore

# Same palette as the plot shim's ``get_tab10_color`` (kept local so the
# panel builder stays importable without Qt/pyqtgraph).
_TAB10 = (
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
)

_CURSOR_COLOR = "#888888"
_HOVER_COLOR = "#c62828"


def snapshots_to_series(snapshots) -> dict:
    """Flatten a snapshot list into aligned time series.

    Returns a dict with ``t`` (s) and per-chain arrays (NaN where the chain
    does not exist at that time): ``top_tension[name]``, ``payout[name]``,
    ``tdp_tension[name]`` (tension at the first bed-contact node; NaN while
    fully suspended), plus ``leg_imbalance`` (leg1 - leg2 top tension),
    ``bu_z`` (BU elevation) and ``layback_bu`` (horizontal vessel-BU
    distance) where applicable.
    """
    n = len(snapshots)
    t = np.array([s.t_s for s in snapshots], dtype=float)
    names: List[str] = []
    for s in snapshots:
        for c in s.chains:
            if c.name not in names:
                names.append(c.name)
    top = {name: np.full(n, np.nan) for name in names}
    payout = {name: np.full(n, np.nan) for name in names}
    tdp = {name: np.full(n, np.nan) for name in names}
    imbalance = np.full(n, np.nan)
    bu_z = np.full(n, np.nan)
    layback = np.full(n, np.nan)
    for i, s in enumerate(snapshots):
        for c in s.chains:
            top[c.name][i] = c.top_tension_kN
            contact = np.asarray(c.contact, dtype=bool)
            if contact.any():
                k = int(np.argmax(contact))
                tens = np.asarray(c.tension_kN, dtype=float)
                tdp[c.name][i] = float(tens[min(k, tens.size - 1)])
        for name, r in (s.payout_mps or {}).items():
            if name in payout:
                payout[name][i] = r
        c1, c2 = s.chain("leg1"), s.chain("leg2")
        if c1 is not None and c2 is not None:
            imbalance[i] = c1.top_tension_kN - c2.top_tension_kN
        xyz = s.junction_xyz.get("BU")
        if xyz is not None:
            bu_z[i] = xyz[2]
            layback[i] = math.hypot(xyz[0] - s.vessel_xy[0], xyz[1] - s.vessel_xy[1])
    return {
        "t": t,
        "names": names,
        "top_tension": top,
        "tdp_tension": tdp,
        "payout": payout,
        "leg_imbalance": imbalance,
        "bu_z": bu_z,
        "layback_bu": layback,
        "labels": [s.label or "" for s in snapshots],
    }


def build_panels(series: Optional[dict]) -> List[dict]:
    """Describe the stacked panels for a series dict (pure, no Qt).

    Each panel is ``{"ylabel", "tag", "unit", "series": [{"label", "color",
    "y", "linewidth", "linestyle"}]}``; only traces with at least one finite
    sample are included, and empty panels are dropped.
    """
    if not series or len(series["t"]) == 0:
        return []

    def live(y) -> bool:
        return y is not None and bool(np.any(np.isfinite(np.asarray(y, dtype=float))))

    names = series["names"]
    panels: List[dict] = []

    top = [{"label": name, "color": _TAB10[i % len(_TAB10)], "y": series["top_tension"][name],
            "linewidth": 1.6, "linestyle": None}
           for i, name in enumerate(names) if live(series["top_tension"].get(name))]
    if live(series["leg_imbalance"]):
        top.append({"label": "|leg1-leg2|", "color": "#d62728",
                    "y": np.abs(np.asarray(series["leg_imbalance"], dtype=float)),
                    "linewidth": 1.2, "linestyle": "--"})
    if top:
        panels.append({"ylabel": "Top tension (kN)", "tag": "Top", "unit": "kN",
                       "series": top})

    tdp_ser = series.get("tdp_tension") or {}
    tdp = [{"label": name, "color": _TAB10[i % len(_TAB10)], "y": tdp_ser[name],
            "linewidth": 1.6, "linestyle": None}
           for i, name in enumerate(names) if live(tdp_ser.get(name))]
    if tdp:
        panels.append({"ylabel": "Bottom (TDP) tension (kN)", "tag": "TDP", "unit": "kN",
                       "series": tdp})

    payout = [{"label": name, "color": _TAB10[i % len(_TAB10)], "y": series["payout"][name],
               "linewidth": 1.4, "linestyle": None}
              for i, name in enumerate(names) if live(series["payout"].get(name))]
    if payout:
        panels.append({"ylabel": "Payout (m/s)", "tag": "Payout", "unit": "m/s",
                       "series": payout})

    bu = []
    if live(series["bu_z"]):
        bu.append({"label": "BU elevation", "color": "#2ca02c", "y": series["bu_z"],
                   "linewidth": 1.6, "linestyle": None})
    if live(series["layback_bu"]):
        bu.append({"label": "BU layback", "color": "#9467bd", "y": series["layback_bu"],
                   "linewidth": 1.2, "linestyle": "--"})
    if bu:
        panels.append({"ylabel": "BU z / layback (m)", "tag": "BU", "unit": "m",
                       "series": bu})

    return panels


def panels_signature(panels: Sequence[dict]) -> Tuple:
    """Layout fingerprint — equal signatures mean the plots can be updated
    in place (keeping the user's zoom) instead of rebuilt."""
    return tuple((p["ylabel"], tuple(s["label"] for s in p["series"])) for p in panels)


def fit_range(t, arrays: Sequence, x0: float, x1: float,
              pad: float = 0.06) -> Optional[Tuple[float, float]]:
    """Y limits covering every finite sample with ``x0 <= t <= x1``.

    Returns ``None`` when nothing finite falls in the window. Flat data get
    a symmetric absolute margin so the trace does not fill the panel.
    """
    t = np.asarray(t, dtype=float)
    if t.size == 0:
        return None
    lo_x, hi_x = (float(x0), float(x1)) if x0 <= x1 else (float(x1), float(x0))
    mask = (t >= lo_x) & (t <= hi_x)
    if not mask.any():
        return None
    lo = math.inf
    hi = -math.inf
    for y in arrays:
        yy = np.asarray(y, dtype=float)
        if yy.shape != t.shape:
            continue
        yy = yy[mask]
        yy = yy[np.isfinite(yy)]
        if yy.size:
            lo = min(lo, float(yy.min()))
            hi = max(hi, float(yy.max()))
    if not math.isfinite(lo) or not math.isfinite(hi):
        return None
    if hi - lo <= 1e-12:
        margin = max(1.0, abs(hi) * 0.05)
        return lo - margin, hi + margin
    margin = (hi - lo) * pad
    return lo - margin, hi + margin


def format_value(value: float, unit: str = "") -> str:
    """Compact fixed-point formatting for hover readouts."""
    if value is None or not math.isfinite(float(value)):
        return "-"
    value = float(value)
    digits = 3 if abs(value) < 10.0 else 2
    text = f"{value:,.{digits}f}"
    return f"{text} {unit}".strip()


class _PanelPlot:
    """Live plot items for one panel (kept so hover/fit need no redraw)."""

    def __init__(self, axis, spec: dict):
        self.axis = axis
        self.spec = spec
        self.lines: List = []       # shim line wrappers, one per series
        self.markers: List = []     # hover value dots, one per series
        self.hover_line = None
        self.cursor_line = None

    @property
    def arrays(self) -> List:
        return [s["y"] for s in self.spec["series"]]


class TimeSeriesView:
    """Stacked tension / balance / descent plots with linked axes, a Y-fit
    action and a hover crosshair."""

    def __init__(self):
        self.figure, self.canvas = make_canvas()
        self._series: Optional[dict] = None
        self._panels: List[dict] = []
        self._plots: List[_PanelPlot] = []
        self._signature: Tuple = ()
        self._t_cursor: Optional[float] = None
        self._x_full: Optional[Tuple[float, float]] = None
        self._fitting = False
        self._hover_index: Optional[int] = None

        self._widget = QWidget()
        outer = QVBoxLayout(self._widget)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 0)
        self.fit_btn = QPushButton("Fit Y")
        self.fit_btn.setToolTip("Rescale every panel's Y axis to the data "
                                "inside the visible time window.")
        self.fit_btn.clicked.connect(lambda: self.fit_y())
        self.reset_btn = QPushButton("Reset view")
        self.reset_btn.setToolTip("Show the whole run again (full time span, "
                                  "Y axes fitted).")
        self.reset_btn.clicked.connect(self.reset_view)
        self.autofit_cb = QCheckBox("Auto-fit Y")
        self.autofit_cb.setChecked(True)
        self.autofit_cb.setToolTip("Re-fit the Y axes whenever the time window "
                                   "changes (pan/zoom).")
        self.autofit_cb.toggled.connect(self._on_autofit_toggled)
        self.crosshair_cb = QCheckBox("Crosshair")
        self.crosshair_cb.setChecked(True)
        self.crosshair_cb.setToolTip("Track the nearest sample under the mouse "
                                     "and read off every trace's value.")
        self.crosshair_cb.toggled.connect(self._on_crosshair_toggled)
        self.readout = QLabel("")
        try:
            policy = getattr(QSizePolicy, "Policy", QSizePolicy)
            self.readout.setSizePolicy(policy.Ignored, policy.Preferred)
        except Exception:
            pass
        bar.addWidget(self.fit_btn)
        bar.addWidget(self.reset_btn)
        bar.addWidget(self.autofit_cb)
        bar.addWidget(self.crosshair_cb)
        bar.addWidget(self.readout, 1)
        outer.addLayout(bar)
        outer.addWidget(self.canvas, 1)

        try:
            self.canvas.mpl_connect("motion_notify_event", self._on_mouse_move)
        except Exception:
            pass
        self._rebuild([])

    # ---- public API -----------------------------------------------------

    def widget(self):
        return self._widget

    def set_snapshots(self, snapshots) -> None:
        self._series = snapshots_to_series(snapshots) if snapshots else None
        panels = build_panels(self._series)
        signature = panels_signature(panels)
        if panels and signature == self._signature and self._plots:
            self._update_data(panels)
        else:
            self._signature = signature
            self._rebuild(panels)

    def clear(self) -> None:
        self._series = None
        self._signature = ()
        self._rebuild([])

    def set_time(self, t_s: Optional[float]) -> None:
        """Move the scrubber cursor (no replot — keeps the current zoom)."""
        self._t_cursor = None if t_s is None else float(t_s)
        for plot in self._plots:
            line = plot.cursor_line
            if line is None:
                continue
            try:
                if self._t_cursor is None:
                    line.set_visible(False)
                else:
                    line.set_xdata([self._t_cursor, self._t_cursor])
                    line.set_visible(True)
            except Exception:
                pass
        self.canvas.draw_idle()

    def fit_y(self, force: bool = True) -> None:
        """Rescale each panel's Y axis to the data in the visible window."""
        if not self._plots or self._series is None:
            return
        if self._fitting:
            return
        t = self._series["t"]
        try:
            x0, x1 = self._plots[0].axis.get_xlim()
        except Exception:
            return
        self._fitting = True
        try:
            for plot in self._plots:
                limits = fit_range(t, plot.arrays, x0, x1)
                if limits is None:
                    continue
                try:
                    plot.axis.set_ylim(limits[0], limits[1])
                except Exception:
                    pass
        finally:
            self._fitting = False

    def reset_view(self) -> None:
        """Zoom back out to the whole run and fit the Y axes."""
        if not self._plots or self._series is None:
            return
        t = self._series["t"]
        if t.size == 0:
            return
        t0, t1 = float(t[0]), float(t[-1])
        if t1 <= t0:
            t1 = t0 + 1.0
        self._set_x_full(t0, t1)
        self.fit_y()

    # ---- build / update -------------------------------------------------

    def _set_x_full(self, t0: float, t1: float) -> None:
        self._fitting = True
        try:
            for plot in self._plots:
                try:
                    plot.axis.set_xlim(t0, t1)
                except Exception:
                    pass
        finally:
            self._fitting = False
        self._x_full = (t0, t1)

    def _rebuild(self, panels: List[dict]) -> None:
        self.figure.clear()
        self._panels = panels
        self._plots = []
        self._hover_index = None
        self._x_full = None
        self.readout.setText("")
        if not panels:
            ax = self.figure.add_subplot(111)
            ax.set_title("Run an operation simulation to see time series")
            self.canvas.draw()
            return
        t = self._series["t"]
        n_rows = len(panels)
        first = None
        for i, spec in enumerate(panels):
            ax = self.figure.add_subplot(n_rows * 100 + 10 + i + 1, sharex=first)
            if first is None:
                first = ax
            plot = _PanelPlot(ax, spec)
            for s in spec["series"]:
                lines = ax.plot(t, s["y"], color=s["color"], linewidth=s["linewidth"],
                                linestyle=s["linestyle"], label=s["label"])
                plot.lines.append(lines[0])
                marker = ax.scatter([], [], s=34, color=s["color"], label="_nolegend_")
                try:
                    marker.setVisible(False)
                except Exception:
                    pass
                plot.markers.append(marker)
            ax.set_ylabel(spec["ylabel"])
            try:
                ax.grid(True)
            except Exception:
                pass
            try:
                ax.legend()
            except Exception:
                pass
            plot.cursor_line = self._make_vline(ax, _CURSOR_COLOR, ":", 1.0)
            plot.hover_line = self._make_vline(ax, _HOVER_COLOR, "--", 1.0)
            self._plots.append(plot)
        self._plots[-1].axis.set_xlabel("Time (s)")
        self._connect_xrange()
        self.reset_view()
        self.set_time(self._t_cursor)
        self.canvas.draw()

    @staticmethod
    def _make_vline(ax, color: str, style: str, width: float):
        try:
            line = ax.axvline(0.0, color=color, linestyle=style, linewidth=width)
            line.set_visible(False)
            return line
        except Exception:
            return None

    def _connect_xrange(self) -> None:
        if not self._plots:
            return
        try:
            view_box = self._plots[0].axis.plot_item.vb
            view_box.sigXRangeChanged.connect(self._on_xrange_changed)
        except Exception:
            pass

    def _on_xrange_changed(self, *args) -> None:
        if self._fitting or not self.autofit_cb.isChecked():
            return
        self.fit_y()

    def _on_autofit_toggled(self, on: bool) -> None:
        if on:
            self.fit_y()

    def _on_crosshair_toggled(self, on: bool) -> None:
        if not on:
            self._hide_hover()

    def _update_data(self, panels: List[dict]) -> None:
        """Same layout, new samples — refresh in place, keeping the zoom."""
        t = self._series["t"]
        following = self._is_following_x()
        self._panels = panels
        for plot, spec in zip(self._plots, panels):
            plot.spec = spec
            for line, s in zip(plot.lines, spec["series"]):
                try:
                    line.item.setData(np.asarray(t, dtype=float),
                                      np.asarray(s["y"], dtype=float))
                except Exception:
                    pass
        self._hide_hover()
        if following and t.size:
            t0, t1 = float(t[0]), float(t[-1])
            self._set_x_full(t0, t1 if t1 > t0 else t0 + 1.0)
        self.fit_y()
        self.set_time(self._t_cursor)
        self.canvas.draw_idle()

    def _is_following_x(self) -> bool:
        """True while the view still shows the whole run (nobody zoomed)."""
        if self._x_full is None or not self._plots:
            return True
        try:
            x0, x1 = self._plots[0].axis.get_xlim()
        except Exception:
            return True
        span = max(abs(self._x_full[1] - self._x_full[0]), 1e-9)
        return (abs(x0 - self._x_full[0]) <= 1e-3 * span
                and abs(x1 - self._x_full[1]) <= 1e-3 * span)

    # ---- hover crosshair ------------------------------------------------

    def _hide_hover(self) -> None:
        self._hover_index = None
        for plot in self._plots:
            if plot.hover_line is not None:
                try:
                    plot.hover_line.set_visible(False)
                except Exception:
                    pass
            for marker in plot.markers:
                try:
                    marker.setVisible(False)
                except Exception:
                    pass
        self.readout.setText("")
        self._set_tooltip("")

    def _set_tooltip(self, text: str) -> None:
        try:
            self.canvas.setToolTip(text)
        except Exception:
            pass
        for plot in self._plots:
            try:
                plot.axis.plot_widget.setToolTip(text)
            except Exception:
                pass

    def _on_mouse_move(self, event) -> None:
        if not self._plots or self._series is None:
            return
        if not self.crosshair_cb.isChecked():
            return
        axes = [plot.axis for plot in self._plots]
        if event.inaxes is None or event.inaxes not in axes or event.xdata is None:
            if self._hover_index is not None:
                self._hide_hover()
                self.canvas.draw_idle()
            return
        t = self._series["t"]
        if t.size == 0:
            return
        index = int(np.argmin(np.abs(t - float(event.xdata))))
        if index == self._hover_index:
            return
        self._hover_index = index
        t_hit = float(t[index])
        for plot in self._plots:
            if plot.hover_line is not None:
                try:
                    plot.hover_line.set_xdata([t_hit, t_hit])
                    plot.hover_line.set_visible(True)
                except Exception:
                    pass
            for marker, s in zip(plot.markers, plot.spec["series"]):
                value = self._value_at(s["y"], index)
                try:
                    if value is None:
                        marker.setVisible(False)
                    else:
                        marker.setData(x=[t_hit], y=[value])
                        marker.setVisible(True)
                except Exception:
                    pass
        self.readout.setText(self._readout_text(index))
        self._set_tooltip(self._tooltip_text(index))
        self.canvas.draw_idle()

    @staticmethod
    def _value_at(y, index: int) -> Optional[float]:
        try:
            value = float(np.asarray(y, dtype=float)[index])
        except Exception:
            return None
        return value if math.isfinite(value) else None

    def _time_label(self, index: int) -> str:
        t = float(self._series["t"][index])
        text = f"t = {t:,.1f} s"
        labels = self._series.get("labels") or []
        if index < len(labels) and labels[index]:
            text += f" — {labels[index]}"
        return text

    def _readout_text(self, index: int) -> str:
        parts = [self._time_label(index)]
        for plot in self._plots:
            unit = plot.spec.get("unit", "")
            tag = plot.spec.get("tag", "")
            for s in plot.spec["series"]:
                value = self._value_at(s["y"], index)
                if value is None:
                    continue
                label = s["label"]
                name = label if label.lower().startswith(tag.lower()) else f"{tag} {label}"
                parts.append(f"{name.strip()} {format_value(value, unit)}")
        return "  |  ".join(parts)

    def _tooltip_text(self, index: int) -> str:
        lines = [self._time_label(index)]
        for plot in self._plots:
            unit = plot.spec.get("unit", "")
            rows = []
            for s in plot.spec["series"]:
                value = self._value_at(s["y"], index)
                if value is None:
                    continue
                rows.append(f"    {s['label']}: {format_value(value, unit)}")
            if rows:
                lines.append(plot.spec["ylabel"])
                lines.extend(rows)
        return "\n".join(lines)
