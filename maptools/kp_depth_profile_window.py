# -*- coding: utf-8 -*-
"""Modeless live depth/slope profile window for the KP Mouse tool's range line.

A "light" version of the Depth Profile tool: two x-linked plots (depth above,
slope in degrees below) that follow the range/bearing line as the mouse moves.
"""

from __future__ import annotations

import math

import pyqtgraph as pg
from qgis.PyQt.QtCore import QSettings, QTimer
from qgis.PyQt.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QVBoxLayout

from ..qgis_compat import WINDOW_HINT_CLOSE, WINDOW_HINT_TITLE, WINDOW_TYPE_TOOL
from .kp_profile_math import (
    composite_series, merged_contour_crossings, should_invert_depth_axis,
    slope_series,
)

_SERIES_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                  "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]

_UNIT_FACTORS = {"m": 1.0, "km": 1.0 / 1000.0,
                 "nautical miles": 1.0 / 1852.0, "miles": 1.0 / 1609.34}

# Refresh cadence for live mouse tracking; low enough to feel live, high
# enough that sampling several rasters stays cheap.
_UPDATE_INTERVAL_MS = 150

_SETTINGS_ORG = "SubseaCableTools"
_SETTINGS_APP = "KPMouseTool"
_GEOMETRY_KEY = "profileWindowGeometry"
_INVERT_KEY = "profileInvertDepth"  # "auto" | "on" | "off"


class KPDepthProfileWindow(QDialog):
    """Small tool window plotting depth and slope along a moving line.

    The map tool calls :meth:`schedule` on every mouse move; sampling and
    redraw are coalesced onto a timer so canvas tracking stays smooth. The
    window remembers its screen position between openings.
    """

    def __init__(self, parent=None, unit: str = "km"):
        super().__init__(parent)
        self.setWindowTitle("Range Line Depth Profile")
        self.setWindowFlags(WINDOW_TYPE_TOOL | WINDOW_HINT_CLOSE | WINDOW_HINT_TITLE)
        self.resize(560, 420)
        self.unit = unit if unit in _UNIT_FACTORS else "km"
        self.user_closed = False
        self._pending = None
        self._sampler = None
        self._distance_area = None
        self._auto_inverted = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._refresh)
        self._settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.depth_widget = pg.PlotWidget()
        self.depth_widget.setBackground("w")
        self.depth_item = self.depth_widget.getPlotItem()
        self.depth_item.showGrid(x=True, y=True, alpha=0.25)
        self.depth_item.setLabel("left", "Depth / elevation (m)")
        self._legend = self.depth_item.addLegend(offset=(10, 10))
        layout.addWidget(self.depth_widget, 3)

        self.slope_widget = pg.PlotWidget()
        self.slope_widget.setBackground("w")
        self.slope_item = self.slope_widget.getPlotItem()
        self.slope_item.showGrid(x=True, y=True, alpha=0.25)
        self.slope_item.setLabel("left", "Slope (°)")
        self.slope_item.setLabel("bottom", "Distance from origin (%s)" % self.unit)
        self.slope_widget.setXLink(self.depth_widget)
        layout.addWidget(self.slope_widget, 2)

        controls = QHBoxLayout()
        self.invert_check = QCheckBox("Invert depth axis")
        self.invert_check.setToolTip(
            "Plot larger depth values downward. Chosen automatically from the "
            "data (positive-down depths invert, negative elevations do not) "
            "until you set it yourself.")
        stored = str(self._settings.value(_INVERT_KEY, "auto") or "auto")
        if stored in ("on", "off"):
            self.invert_check.setChecked(stored == "on")
            self._apply_invert(stored == "on")
        self.invert_check.toggled.connect(self._invert_toggled)
        controls.addWidget(self.invert_check)
        self.status_label = QLabel("Move the mouse to sample depths along the range line.")
        self.status_label.setWordWrap(True)
        controls.addWidget(self.status_label, 1)
        layout.addLayout(controls)

        self._curves = {}
        self._slope_curve = None

    def configure(self, sampler, distance_area):
        self._sampler = sampler
        self._distance_area = distance_area

    # -- depth axis orientation --------------------------------------------
    def _invert_auto_mode(self) -> bool:
        return str(self._settings.value(_INVERT_KEY, "auto") or "auto") == "auto"

    def _apply_invert(self, inverted: bool):
        self.depth_item.vb.invertY(bool(inverted))

    def _invert_toggled(self, checked):
        # A manual toggle pins the choice; auto detection stops overriding it.
        self._settings.setValue(_INVERT_KEY, "on" if checked else "off")
        self._apply_invert(checked)

    def _auto_orient(self, values):
        """Positive-down depth data plots inverted; negative elevations do not."""
        if not self._invert_auto_mode() or self._auto_inverted:
            return
        invert = should_invert_depth_axis(values)
        if invert is None:
            return
        self._auto_inverted = True
        self.invert_check.blockSignals(True)
        self.invert_check.setChecked(invert)
        self.invert_check.blockSignals(False)
        self._apply_invert(invert)

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
        series_to_draw = list(profile.get("rasters", []))
        # Contour layers (e.g. bathy major + minor sets) merge into one
        # distance-sorted seabed line instead of overlapping per-layer lines.
        contour_layers = profile.get("contours", [])
        if contour_layers:
            merged_x, merged_y = merged_contour_crossings(profile)
            name = (contour_layers[0]["name"] if len(contour_layers) == 1
                    else "Contours (merged)")
            series_to_draw.append(
                {"name": name, "x": merged_x, "y": merged_y, "contour": True})
        for series in series_to_draw:
            key = ("contour" if series.get("contour") else "raster", series["name"])
            seen.add(key)
            x_values = [value * factor for value in series["x"]]
            y_values = [math.nan if value is None else value for value in series["y"]]
            values.extend(value for value in series["y"] if value is not None)
            curve = self._curves.get(key)
            if curve is None:
                pen = pg.mkPen(_SERIES_COLORS[color_index % len(_SERIES_COLORS)], width=2)
                curve = self.depth_item.plot([], [], pen=pen, name=series["name"],
                                             connect="finite")
                self._curves[key] = curve
            curve.setData(x_values, y_values, connect="finite")
            color_index += 1
        for key in [key for key in self._curves if key not in seen]:
            self._remove_curve(self._curves.pop(key))

        # Slope of the composite (best-resolution-first) profile.
        comp_x, comp_y = composite_series(profile)
        slopes = slope_series(comp_x, comp_y)
        slope_x = [value * factor for value in comp_x]
        slope_y = [math.nan if value is None else value for value in slopes]
        if self._slope_curve is None:
            self._slope_curve = self.slope_item.plot(
                [], [], pen=pg.mkPen("#444444", width=2), connect="finite")
        self._slope_curve.setData(slope_x, slope_y, connect="finite")

        self._auto_orient(values)
        length = profile.get("length_m", 0.0) * factor
        finite_slopes = [abs(value) for value in slopes if value is not None]
        if values:
            slope_text = (", max slope %.1f°" % max(finite_slopes)
                          if finite_slopes else "")
            self.status_label.setText(
                "Range %.3f %s — depth %.1f to %.1f m%s"
                % (length, self.unit, min(values), max(values), slope_text))
        else:
            self.status_label.setText(
                "Range %.3f %s — no depth data along the line."
                % (length, self.unit))

    def _remove_curve(self, item):
        try:
            self._legend.removeItem(item)
        except Exception:
            pass
        try:
            self.depth_item.removeItem(item)
        except Exception:
            pass

    def clear_profile(self):
        self._pending = None
        self._auto_inverted = False
        for item in list(self._curves.values()):
            self._remove_curve(item)
        self._curves = {}
        if self._slope_curve is not None:
            self._slope_curve.setData([], [])
        self.status_label.setText("Move the mouse to sample depths along the range line.")

    # -- lifecycle ---------------------------------------------------------
    def showEvent(self, event):
        geometry = self._settings.value(_GEOMETRY_KEY)
        if geometry is not None:
            try:
                self.restoreGeometry(geometry)
            except Exception:
                pass
        super().showEvent(event)

    def _save_geometry(self):
        try:
            self._settings.setValue(_GEOMETRY_KEY, self.saveGeometry())
        except Exception:
            pass

    def hideEvent(self, event):
        self._save_geometry()
        super().hideEvent(event)

    def closeEvent(self, event):
        # Respect a manual close for the rest of this measurement; toggling the
        # profile (right-click menu or D) or a new origin re-opens the window.
        self.user_closed = True
        self._save_geometry()
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
