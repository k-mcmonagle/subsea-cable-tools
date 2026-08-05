# -*- coding: utf-8 -*-
"""Modeless live depth-profile window for the KP Mouse tool's range line."""

from __future__ import annotations

import math
from typing import Optional

import pyqtgraph as pg
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QDialog, QLabel, QVBoxLayout

from ..qgis_compat import WINDOW_HINT_CLOSE, WINDOW_HINT_TITLE, WINDOW_TYPE_TOOL

_SERIES_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                  "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]

_UNIT_FACTORS = {"m": 1.0, "km": 1.0 / 1000.0,
                 "nautical miles": 1.0 / 1852.0, "miles": 1.0 / 1609.34}

# Refresh cadence for live mouse tracking; low enough to feel live, high
# enough that sampling several rasters stays cheap.
_UPDATE_INTERVAL_MS = 150


class KPDepthProfileWindow(QDialog):
    """Small always-on-top-of-QGIS window plotting depth along a moving line.

    The map tool calls :meth:`schedule` on every mouse move; sampling and
    redraw are coalesced onto a timer so canvas tracking stays smooth.
    """

    def __init__(self, parent=None, unit: str = "km"):
        super().__init__(parent)
        self.setWindowTitle("Range Line Depth Profile")
        self.setWindowFlags(WINDOW_TYPE_TOOL | WINDOW_HINT_CLOSE | WINDOW_HINT_TITLE)
        self.resize(520, 300)
        self.unit = unit if unit in _UNIT_FACTORS else "km"
        self.user_closed = False
        self._pending = None
        self._sampler = None
        self._distance_area = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("w")
        self.plot_item = self.plot_widget.getPlotItem()
        self.plot_item.showGrid(x=True, y=True, alpha=0.25)
        self.plot_item.setLabel("left", "Depth / elevation (m)")
        self.plot_item.setLabel("bottom", "Distance from origin (%s)" % self.unit)
        self._legend = self.plot_item.addLegend(offset=(10, 10))
        layout.addWidget(self.plot_widget, 1)
        self.status_label = QLabel("Move the mouse to sample depths along the range line.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._curves = {}
        self._scatters = {}

    def configure(self, sampler, distance_area):
        self._sampler = sampler
        self._distance_area = distance_area

    # -- live updates ------------------------------------------------------
    def schedule(self, origin, target):
        """Queue a profile refresh for the latest origin→mouse line."""
        self._pending = (origin, target)
        if not self._timer.isActive():
            self._timer.start(_UPDATE_INTERVAL_MS)

    def _refresh(self):
        if self._pending is None or self._sampler is None or not self.isVisible():
            return
        origin, target = self._pending
        self._pending = None
        try:
            profile = self._sampler.profile(origin, target, self._distance_area)
        except Exception as exc:
            self.status_label.setText("Depth sampling failed: %s" % exc)
            return
        factor = _UNIT_FACTORS[self.unit]
        seen = set()
        color_index = 0
        values = []
        for series in profile.get("rasters", []):
            key = ("raster", series["name"])
            seen.add(key)
            x_values = [value * factor for value in series["x"]]
            y_values = [math.nan if value is None else value for value in series["y"]]
            values.extend(value for value in series["y"] if value is not None)
            curve = self._curves.get(key)
            if curve is None:
                pen = pg.mkPen(_SERIES_COLORS[color_index % len(_SERIES_COLORS)], width=2)
                curve = self.plot_item.plot(
                    [], [], pen=pen, name=series["name"], connect="finite")
                self._curves[key] = curve
            curve.setData(x_values, y_values, connect="finite")
            color_index += 1
        for series in profile.get("contours", []):
            key = ("contour", series["name"])
            seen.add(key)
            values.extend(series["y"])
            scatter = self._scatters.get(key)
            if scatter is None:
                color = _SERIES_COLORS[color_index % len(_SERIES_COLORS)]
                scatter = pg.ScatterPlotItem(
                    size=7, pen=pg.mkPen(color), brush=pg.mkBrush(color),
                    symbol="o", name=series["name"])
                # PlotItem.addItem auto-registers named items in the legend.
                self.plot_item.addItem(scatter)
                self._scatters[key] = scatter
            scatter.setData([value * factor for value in series["x"]], series["y"])
            color_index += 1
        # Drop series whose layer no longer returned data (line moved off it).
        for key in [key for key in self._curves if key not in seen]:
            self._remove_item(self._curves.pop(key))
        for key in [key for key in self._scatters if key not in seen]:
            self._remove_item(self._scatters.pop(key))
        length = profile.get("length_m", 0.0) * factor
        if values:
            self.status_label.setText(
                "Range %.3f %s — depth min %.1f m, max %.1f m"
                % (length, self.unit, min(values), max(values)))
        else:
            self.status_label.setText(
                "Range %.3f %s — no depth data along the line."
                % (length, self.unit))

    def _remove_item(self, item):
        try:
            self._legend.removeItem(item)
        except Exception:
            pass
        try:
            self.plot_item.removeItem(item)
        except Exception:
            pass

    def clear_profile(self):
        self._pending = None
        for item in list(self._curves.values()) + list(self._scatters.values()):
            self._remove_item(item)
        self._curves = {}
        self._scatters = {}
        self.status_label.setText("Move the mouse to sample depths along the range line.")

    # -- lifecycle ---------------------------------------------------------
    def closeEvent(self, event):
        # Respect a manual close for the rest of this measurement; a new
        # range/bearing origin re-opens the window.
        self.user_closed = True
        self._timer.stop()
        super().closeEvent(event)

    def cleanup(self):
        try:
            self._timer.stop()
            self._timer.timeout.disconnect()
        except Exception:
            pass
        self._pending = None
        self._sampler = None
        try:
            self.close()
            self.deleteLater()
        except Exception:
            pass
