# -*- coding: utf-8 -*-
"""Straight Line Diagram (SLD) widget.

Renders an assembly in the cable-distance domain using the bundled
pyqtgraph: sections as coloured bars, bodies as markers with labels, and an
optional secondary lane of route events (crossings, burial transitions, ...)
when a fit ties the assembly to an RPL.

Built to stay responsive with hundreds of sections/bodies: bars render as a
single BarGraphItem, bodies as one ScatterPlotItem, and text labels decimate
with the visible range (hidden when more than ~80 are in view).

Signals:
    itemClicked(int)      -- index into the assembly's items list
    cableDistClicked(float) -- cable distance (m) of any click on the diagram
"""

from __future__ import annotations

from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPen
from qgis.PyQt.QtWidgets import QVBoxLayout, QWidget

import pyqtgraph as pg

from .assembly_model import Assembly

SECTION_LANE_Y = 1.0
SECTION_LANE_HEIGHT = 0.6
BODY_LANE_Y = 1.85
EVENT_LANE_Y = 0.25
MAX_VISIBLE_LABELS = 80

DEFAULT_SECTION_COLOR = "#4477aa"
EVENT_COLORS = {
    "geographic": "#cc6677",
    "installation": "#888888",
    "body": "#117733",
}


class KpAxisItem(pg.AxisItem):
    """Top axis showing route KP for the cable distance below it.

    Tick *positions* stay linear in cable distance (they share the ViewBox);
    the labels are converted through the fit's cable->KP mapping, which is
    piecewise-linear in slack — so unequal KP spacing between equal cable
    ticks is real information (it shows where slack is concentrated).
    """

    def __init__(self):
        super().__init__(orientation="top")
        self.mapping = None  # Callable[[cable_m], Optional[kp_km]]
        self.setStyle(textFillLimits=[(0, 0.7)])

    def tickStrings(self, values, scale, spacing):
        if self.mapping is None:
            return ["" for _ in values]
        out = []
        previous = None
        for value in values:
            try:
                kp = self.mapping(value * 1000.0)
            except Exception:
                kp = None
            label = "" if kp is None else f"{kp:.2f}"
            if label and label == previous:
                label = ""
            if label:
                previous = label
            out.append(label)
        return out


class SldWidget(QWidget):
    itemClicked = pyqtSignal(int)
    cableDistClicked = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._kp_axis = KpAxisItem()
        self.plot = pg.PlotWidget(axisItems={"top": self._kp_axis})
        self.plot.setBackground(None)
        self.plot.setMenuEnabled(False)
        self.plot.setLabel("bottom", "Cable distance", units="km")
        self.plot.getAxis("left").hide()
        self.plot.getPlotItem().showAxis("top", False)
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.setYRange(-0.2, 2.4, padding=0)
        layout.addWidget(self.plot)

        self._assembly: Optional[Assembly] = None
        self._starts_km: List[float] = []
        self._widths_km: List[float] = []
        self._item_indices: List[int] = []      # bar order -> assembly item index
        self._body_positions: List[float] = []  # km
        self._body_indices: List[int] = []
        self._body_labels: List[pg.TextItem] = []
        self._event_labels: List[pg.TextItem] = []
        self._bars: Optional[pg.BarGraphItem] = None
        self._bodies: Optional[pg.ScatterPlotItem] = None
        self._events_scatter: Optional[pg.ScatterPlotItem] = None
        self._highlight: Optional[pg.LinearRegionItem] = None
        self._kp_marker: Optional[pg.InfiniteLine] = None

        self.plot.scene().sigMouseClicked.connect(self._on_click)
        self.plot.getViewBox().sigRangeChanged.connect(self._decimate_labels)

    # ------------------------------------------------------------- render --
    def set_assembly(self, assembly: Optional[Assembly], events: Optional[List[Dict]] = None):
        """Render an assembly; ``events`` are optional route events already
        mapped to the cable domain: [{"cable_km", "category", "label"}, ...]."""
        self._clear_items()
        self._assembly = assembly
        if assembly is None or not assembly.items:
            return

        starts_m = assembly.cable_dist_starts_m()
        xs, widths, brushes = [], [], []
        self._starts_km, self._widths_km, self._item_indices = [], [], []
        self._body_positions, self._body_indices = [], []
        body_spots = []

        for i, item in enumerate(assembly.items):
            start_km = starts_m[i] / 1000.0
            if item.is_section:
                width_km = max((item.length_m or 0.0) / 1000.0, 1e-6)
                xs.append(start_km)
                widths.append(width_km)
                color = QColor(item.color_hex) if item.color_hex else QColor(DEFAULT_SECTION_COLOR)
                if not color.isValid():
                    color = QColor(DEFAULT_SECTION_COLOR)
                brushes.append(QBrush(color))
                self._starts_km.append(start_km)
                self._widths_km.append(width_km)
                self._item_indices.append(i)
            else:
                body_spots.append({
                    "pos": (start_km, BODY_LANE_Y),
                    "brush": pg.mkBrush(EVENT_COLORS["body"]),
                    "symbol": "d",
                    "size": 12,
                    "data": i,
                })
                self._body_positions.append(start_km)
                self._body_indices.append(i)

        if xs:
            self._bars = pg.BarGraphItem(
                x0=xs, width=widths, y=SECTION_LANE_Y, height=SECTION_LANE_HEIGHT,
                brushes=brushes, pen=pg.mkPen(QColor(40, 40, 40)),
            )
            self.plot.addItem(self._bars)

        if body_spots:
            self._bodies = pg.ScatterPlotItem(pxMode=True)
            self._bodies.addPoints(body_spots)
            self.plot.addItem(self._bodies)
            for spot, idx in zip(body_spots, self._body_indices):
                label = pg.TextItem(assembly.items[idx].name or "body", anchor=(0.5, 1.2))
                label.setPos(spot["pos"][0], BODY_LANE_Y)
                self.plot.addItem(label)
                self._body_labels.append(label)

        if events:
            spots = []
            for event in events:
                cable_km = event.get("cable_km")
                if cable_km is None:
                    continue
                category = event.get("category") or "installation"
                spots.append({
                    "pos": (cable_km, EVENT_LANE_Y),
                    "brush": pg.mkBrush(EVENT_COLORS.get(category, "#888888")),
                    "symbol": "t1",
                    "size": 9,
                })
                label = pg.TextItem(str(event.get("label") or ""), anchor=(0.5, -0.3), color="#666666")
                label.setPos(cable_km, EVENT_LANE_Y)
                self.plot.addItem(label)
                self._event_labels.append(label)
            if spots:
                self._events_scatter = pg.ScatterPlotItem(pxMode=True)
                self._events_scatter.addPoints(spots)
                self.plot.addItem(self._events_scatter)

        total_km = assembly.total_length_m() / 1000.0
        self.plot.setXRange(-0.02 * total_km, total_km * 1.02, padding=0)
        self._decimate_labels()

    def _clear_items(self):
        self.plot.clear()
        self._bars = None
        self._bodies = None
        self._events_scatter = None
        self._highlight = None
        self._kp_marker = None
        self._body_labels = []
        self._event_labels = []

    # ------------------------------------------------------------- KP axis --
    def set_kp_mapping(self, mapping):
        """Show/hide the top KP axis.

        ``mapping(cable_m) -> Optional[kp_km]`` when the assembly has an
        active fit onto an RPL; None hides the axis.
        """
        self._kp_axis.mapping = mapping
        plot_item = self.plot.getPlotItem()
        if mapping is not None:
            plot_item.showAxis("top", True)
            self._kp_axis.setLabel("Route KP", units="km")
        else:
            plot_item.showAxis("top", False)
        self._kp_axis.update()

    # ---------------------------------------------------------- selection --
    def highlight_item(self, item_index: Optional[int]):
        if self._highlight is not None:
            self.plot.removeItem(self._highlight)
            self._highlight = None
        if self._assembly is None or item_index is None:
            return
        starts = self._assembly.cable_dist_starts_m()
        if not (0 <= item_index < len(self._assembly.items)):
            return
        item = self._assembly.items[item_index]
        start_km = starts[item_index] / 1000.0
        if item.is_section:
            end_km = start_km + max((item.length_m or 0.0) / 1000.0, 1e-6)
        else:
            half = max(self._assembly.total_length_m() / 1000.0 * 0.002, 0.005)
            start_km, end_km = start_km - half, start_km + half
        self._highlight = pg.LinearRegionItem(
            values=(start_km, end_km), movable=False,
            brush=pg.mkBrush(QColor(255, 200, 0, 60)), pen=pg.mkPen(QColor(255, 160, 0)),
        )
        self.plot.addItem(self._highlight)

    def mark_cable_dist(self, cable_m: Optional[float]):
        """Vertical marker line (e.g. from a map/RPL selection)."""
        if self._kp_marker is not None:
            self.plot.removeItem(self._kp_marker)
            self._kp_marker = None
        if cable_m is None:
            return
        self._kp_marker = pg.InfiniteLine(
            pos=cable_m / 1000.0, angle=90,
            pen=pg.mkPen(QColor(0, 170, 255), width=2, style=Qt.PenStyle.DashLine),
        )
        self.plot.addItem(self._kp_marker)

    # -------------------------------------------------------------- events --
    def _on_click(self, mouse_event):
        if self._assembly is None:
            return
        pos = mouse_event.scenePos()
        if not self.plot.sceneBoundingRect().contains(pos):
            return
        view_pos = self.plot.getViewBox().mapSceneToView(pos)
        x_km, y = view_pos.x(), view_pos.y()
        self.cableDistClicked.emit(x_km * 1000.0)

        # body lane first (bigger targets win near the body row)
        if self._body_positions and y > (SECTION_LANE_Y + SECTION_LANE_HEIGHT / 2.0):
            view_range = self.plot.getViewBox().viewRange()[0]
            tolerance = (view_range[1] - view_range[0]) * 0.01
            best, best_d = None, None
            for pos_km, idx in zip(self._body_positions, self._body_indices):
                d = abs(pos_km - x_km)
                if d <= tolerance and (best_d is None or d < best_d):
                    best, best_d = idx, d
            if best is not None:
                self.itemClicked.emit(best)
                return

        # section bars (binary-search style scan; list is ordered)
        for start_km, width_km, idx in zip(self._starts_km, self._widths_km, self._item_indices):
            if start_km <= x_km <= start_km + width_km:
                self.itemClicked.emit(idx)
                return

    def _decimate_labels(self, *_args):
        view_range = self.plot.getViewBox().viewRange()[0]
        lo, hi = view_range

        def apply(labels: List[pg.TextItem]):
            visible = [l for l in labels if lo <= l.pos().x() <= hi]
            show = len(visible) <= MAX_VISIBLE_LABELS
            for label in labels:
                label.setVisible(show and lo <= label.pos().x() <= hi)

        apply(self._body_labels)
        apply(self._event_labels)
