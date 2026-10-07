# -*- coding: utf-8 -*-
"""Seabed profile for the Lay Assessment: where the cable rests on the seabed.

Three linked rows sharing a KP axis:

* a status bar coloured by the worst finding per KP (green clear, amber
  warning, red error, blue info, grey no data);
* the depth profile: sampled seabed, the modelled cable (spans filled red)
  and the lay model's touchdown depths;
* bottom tension (logged, and estimated from measured top tension).

Two-click measurements reuse the depth tools' ProfileMeasureController.
Hovering / clicking links to the Explorer's records and the map.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from ...laydata.lay_assessment import RangeFinding, estimated_bottom_tension, status_bins
from ...maptools.profile_measure_controller import ProfileMeasureController
from ...plugin_log import log_exception

_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
LEVEL_COLOURS = {-1: (200, 200, 200), 0: (76, 175, 80), 1: (31, 119, 180), 2: (255, 152, 0), 3: (211, 47, 47)}
_SEABED = (121, 85, 72)
_CABLE = (25, 70, 160)


class SeabedProfilePanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._records = None
        self._model = None
        self._findings: List[RangeFinding] = []
        self._row_to_kp: Dict[int, float] = {}
        self._record_kp_sorted = np.array([])
        self._record_order = np.array([], dtype=int)
        self._kp_window = None
        self._items = []  # (plot, item) drawn by replot, removed by the next one

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)

        bar = QHBoxLayout()
        self.measure = ProfileMeasureController(self, plot_factors=lambda: (0.001, 1.0),
                                                text_units=lambda: ("m", "m"), depth_down=lambda: True)
        self.measure.statusChanged.connect(lambda text: self.info.setText(text))
        measure_btn = QToolButton()
        measure_btn.setDefaultAction(self.measure.action)
        bar.addWidget(measure_btn)
        bar.addWidget(self.measure.snap_check)
        bar.addWidget(self.measure.source_combo)
        bar.addWidget(self.measure.delete_btn)
        bar.addWidget(self.measure.clear_btn)
        self.td_check = QCheckBox("Lay model touchdown depth")
        self.td_check.setToolTip("Show the water depth the lay model logged at touchdown, "
                                 "for comparison with the sampled seabed.")
        self.td_check.setChecked(True)
        self.td_check.toggled.connect(self.replot)
        bar.addWidget(self.td_check)
        fit = QPushButton("Fit")
        fit.clicked.connect(self.fit)
        bar.addWidget(fit)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.graphics = pg.GraphicsLayoutWidget()
        self.graphics.setBackground("w")
        layout.addWidget(self.graphics, 1)
        self.status_plot = self.graphics.addPlot(row=0, col=0)
        self.depth_plot = self.graphics.addPlot(row=1, col=0)
        self.tension_plot = self.graphics.addPlot(row=2, col=0)
        layout_ci = self.graphics.ci.layout
        layout_ci.setRowStretchFactor(0, 1)
        layout_ci.setRowStretchFactor(1, 9)
        layout_ci.setRowStretchFactor(2, 3)
        self.status_plot.setMaximumHeight(42)
        self.status_plot.hideAxis("left")
        self.status_plot.hideAxis("bottom")
        self.status_plot.setMouseEnabled(x=True, y=False)
        self.status_plot.setYRange(0, 1, padding=0)
        self.status_plot.getAxis("left").setWidth(60)
        for plot in (self.depth_plot, self.tension_plot):
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getAxis("left").setWidth(60)
        self.depth_plot.invertY(True)
        self.depth_plot.setLabel("left", "Depth (m)")
        self.tension_plot.setLabel("left", "Bottom T (kN)")
        self.tension_plot.setLabel("bottom", "KP (km)")
        for plot in (self.depth_plot, self.tension_plot):
            plot.addLegend(offset=(10, 5), labelTextColor=(40, 40, 40),
                           brush=pg.mkBrush(255, 255, 255, 210), pen=pg.mkPen(200, 200, 200))
        self.status_plot.setXLink(self.depth_plot)
        self.tension_plot.setXLink(self.depth_plot)

        self.info = QLabel("Run the Lay Assessment to see the seabed profile.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addWidget(self.measure.table)
        self.measure.table.hide()  # shown once there is a measurement
        self.measure.measurementsChanged.connect(
            lambda: self.measure.table.setVisible(bool(self.measure.measurements)))

        self._vlines = []
        for plot in (self.depth_plot, self.tension_plot):
            line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen((120, 120, 120), width=1, style=_DASH))
            line.hide()
            plot.addItem(line, ignoreBounds=True)
            self._vlines.append(line)
        self.graphics.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.graphics.scene().sigMouseClicked.connect(self._on_mouse_clicked)
        self.measure.attach(self.depth_plot)

    # -- data -----------------------------------------------------------------
    def set_result(self, records, model, findings: Sequence[RangeFinding], kp_window=None) -> None:
        """``model`` is a SeabedModel or None (no seabed sampled)."""
        self.measure.reset()  # measurements belong to the previous result
        self._records = records
        self._model = model
        self._findings = list(findings)
        self._kp_window = kp_window
        self._row_to_kp = {}
        if records is not None and records.n:
            ok = np.isfinite(records.kp)
            order = np.argsort(np.where(ok, records.kp, np.inf), kind="stable")
            order = order[ok[order]]
            self._record_order = order
            self._record_kp_sorted = records.kp[order]
            self._row_to_kp = {int(records.rows[i]): float(records.kp[i]) for i in np.nonzero(ok)[0]}
        else:
            self._record_order = np.array([], dtype=int)
            self._record_kp_sorted = np.array([])
        if records is None:
            self.info.setText("Run the Lay Assessment to see the seabed profile.")
        else:
            spans = f"{len(model.spans)} modelled span(s) in red. " if model is not None else                 "No seabed modelled (choose a seabed source). "
            self.info.setText(spans + "Status bar: green clear, amber / red flagged, blue info, grey no data. "
                              "Hover or click to find records; Measure for lengths and heights.")
        self.replot()
        self.fit()

    def clear(self) -> None:
        self.set_result(None, None, [])

    def _add(self, plot, item) -> None:
        plot.addItem(item)
        self._items.append((plot, item))

    def replot(self, *_args) -> None:
        for plot, item in self._items:
            plot.removeItem(item)
        self._items = []
        records = self._records
        if records is None:
            self.measure.set_series([])
            return
        try:
            self._plot_status()
            series = self._plot_depth()
            self._plot_tension()
            self.measure.set_series(series)
        except Exception:
            log_exception("Lay Assessment: drawing the seabed profile failed")

    def _kp_extent(self):
        kps = [self._record_kp_sorted]
        if self._model is not None:
            kps.append(self._model.kp_km)
        values = np.concatenate([k[np.isfinite(k)] for k in kps]) if kps else np.array([])
        if self._kp_window is not None:
            lo, hi = self._kp_window
            values = values[(values >= lo) & (values <= hi)]
        if values.size == 0:
            return None
        return float(values.min()), float(values.max())

    def _plot_status(self) -> None:
        extent = self._kp_extent()
        if extent is None:
            return
        lo, hi = extent
        bin_m = max((hi - lo) * 1000.0 / 4000.0, 1.0)
        covered = self._record_kp_sorted
        if self._model is not None:
            covered = np.concatenate([covered, self._model.kp_km[np.isfinite(self._model.seabed_depth_m)]])
        edges, level = status_bins(lo, hi, self._findings, covered, bin_m)
        if not len(level):
            return
        # Collapse equal neighbours so the bar is a handful of rectangles.
        change = np.nonzero(np.diff(level))[0] + 1
        starts = np.concatenate(([0], change))
        ends = np.concatenate((change, [len(level)]))
        x0 = edges[starts]
        x1 = edges[ends]
        brushes = [pg.mkBrush(*LEVEL_COLOURS.get(int(level[s]), (200, 200, 200))) for s in starts]
        bars = pg.BarGraphItem(x0=x0, x1=x1, y0=np.zeros(len(x0)), y1=np.ones(len(x0)),
                               brushes=brushes, pens=[pg.mkPen(None)] * len(x0))
        self._add(self.status_plot, bars)

    def _plot_depth(self) -> list:
        series = []
        records, model = self._records, self._model
        if model is not None:
            kp = model.kp_km
            seabed = pg.PlotDataItem(kp, model.seabed_depth_m, pen=pg.mkPen(_SEABED, width=2), name="Seabed",
                                     connect="finite")
            cable = pg.PlotDataItem(kp, model.cable_depth_m, pen=pg.mkPen(_CABLE, width=1.5), name="Cable (model)",
                                    connect="finite")
            self._add(self.depth_plot, seabed)
            self._add(self.depth_plot, cable)
            for span in model.spans:
                sl = slice(span.i0, span.i1 + 1)
                top = pg.PlotDataItem(kp[sl], model.cable_depth_m[sl])
                bottom = pg.PlotDataItem(kp[sl], model.seabed_depth_m[sl])
                fill = pg.FillBetweenItem(top, bottom, brush=pg.mkBrush(211, 47, 47, 150))
                self._add(self.depth_plot, fill)
            ok = np.isfinite(model.seabed_depth_m)
            series.append({"name": "Seabed", "x": kp[ok] * 1000.0, "y": model.seabed_depth_m[ok]})
            ok = np.isfinite(model.cable_depth_m)
            series.append({"name": "Cable (model)", "x": kp[ok] * 1000.0, "y": model.cable_depth_m[ok]})
        if records is not None and records.has("td_depth") and (self.td_check.isChecked() or model is None):
            order = self._record_order
            depth = np.abs(records.get("td_depth")[order])
            ok = np.isfinite(depth)
            if ok.any():
                td = pg.ScatterPlotItem(self._record_kp_sorted[ok], depth[ok], size=3,
                                        pen=pg.mkPen(None), brush=pg.mkBrush(90, 90, 90, 160),
                                        name="Touchdown depth (lay model)")
                self._add(self.depth_plot, td)
                if model is None:
                    series.append({"name": "Touchdown depth (lay model)",
                                   "x": self._record_kp_sorted[ok] * 1000.0, "y": depth[ok]})
        return series

    def _plot_tension(self) -> None:
        records = self._records
        order = self._record_order
        kp = self._record_kp_sorted
        if records.has("bottom_tension"):
            values = records.get("bottom_tension")[order]
            self._add(self.tension_plot, pg.PlotDataItem(kp, values, pen=pg.mkPen((60, 60, 60), width=1),
                                                      name="Logged (model)", connect="finite"))
        if records.has("top_tension", "td_depth"):
            estimate = estimated_bottom_tension(records)[order]
            if np.isfinite(estimate).any():
                self._add(self.tension_plot, pg.PlotDataItem(
                    kp, estimate, pen=pg.mkPen((0, 150, 136), width=1, style=_DASH),
                    name="From top tension", connect="finite"))
        npts = records.cable_array("npts_kn")
        finite = npts[np.isfinite(npts)]
        if finite.size and np.allclose(finite, finite[0]):
            line = pg.InfiniteLine(pos=float(finite[0]), angle=0, pen=pg.mkPen((211, 47, 47), width=1, style=_DASH),
                                   label="NPTS", labelOpts={"position": 0.03, "color": (211, 47, 47)})
            self._add(self.tension_plot, line)

    # -- navigation -----------------------------------------------------------
    def fit(self) -> None:
        extent = self._kp_extent()
        if extent is None:
            return
        lo, hi = extent
        pad = max((hi - lo) * 0.02, 0.001)
        self.depth_plot.setXRange(lo - pad, hi + pad, padding=0)
        self.depth_plot.enableAutoRange(axis="y")
        self.tension_plot.enableAutoRange(axis="y")

    def zoom_to(self, kp_start: float, kp_end: float) -> None:
        """Show a KP range with context either side, depth fitted to it."""
        span = max(abs(kp_end - kp_start), 0.05)
        lo = min(kp_start, kp_end) - span * 0.75
        hi = max(kp_start, kp_end) + span * 0.75
        self.depth_plot.setXRange(lo, hi, padding=0)
        model = self._model
        depths = []
        if model is not None:
            sel = (model.kp_km >= lo) & (model.kp_km <= hi)
            depths = [model.seabed_depth_m[sel], model.cable_depth_m[sel]]
        elif self._records is not None and self._records.has("td_depth"):
            sel = (self._record_kp_sorted >= lo) & (self._record_kp_sorted <= hi)
            depths = [np.abs(self._records.get("td_depth")[self._record_order][sel])]
        values = np.concatenate([d[np.isfinite(d)] for d in depths]) if depths else np.array([])
        if values.size:
            pad = max((values.max() - values.min()) * 0.15, 0.5)
            self.depth_plot.setYRange(values.min() - pad, values.max() + pad, padding=0)
        self.tension_plot.enableAutoRange(axis="y")

    def set_hover(self, source_row: int, force: bool = False) -> None:
        kp = self._row_to_kp.get(int(source_row))
        for line in self._vlines:
            if kp is None:
                line.hide()
            else:
                line.setPos(kp)
                line.show()

    def _row_at_kp(self, kp: float) -> Optional[int]:
        kps = self._record_kp_sorted
        if not kps.size:
            return None
        i = int(np.clip(np.searchsorted(kps, kp), 1, len(kps) - 1)) if len(kps) > 1 else 0
        if len(kps) > 1 and abs(kps[i - 1] - kp) < abs(kps[i] - kp):
            i -= 1
        return int(self._records.rows[self._record_order[i]])

    def _kp_at_scene(self, pos) -> Optional[float]:
        for plot in (self.depth_plot, self.tension_plot):
            if plot.sceneBoundingRect().contains(pos):
                return float(plot.vb.mapSceneToView(pos).x())
        return None

    def _on_mouse_moved(self, pos) -> None:
        if self._records is None or self.measure.active:
            return
        kp = self._kp_at_scene(pos)
        if kp is None:
            return
        row = self._row_at_kp(kp)
        if row is None:
            return
        self.set_hover(row)
        hover = getattr(self.controller, "broadcast_hover", None)
        if callable(hover):
            hover(row, origin=self)

    def _on_mouse_clicked(self, event) -> None:
        if self._records is None or self.measure.active:
            return
        kp = self._kp_at_scene(event.scenePos())
        if kp is None:
            return
        row = self._row_at_kp(kp)
        if row is None:
            return
        if event.double():
            self.controller.go_to_record(row)
        else:
            self.controller.highlight_record(row, from_plot=True)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self.measure.handle_key(event):
            return
        super().keyPressEvent(event)
