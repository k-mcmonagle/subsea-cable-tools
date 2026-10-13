# -*- coding: utf-8 -*-
"""Seabed profile for the Lay Assessment: where the cable rests on the seabed.

Three linked rows sharing a KP axis:

* a status bar coloured by the worst finding per KP (green clear, amber
  warning, red error, blue info, grey no data);
* the depth profile: sampled seabed, the modelled cable (spans filled red),
  the span supports (red where the bend radius is below the MBR; hover for
  the reaction and bend radius) and the lay model's touchdown depths;
* bottom tension (logged, estimated from measured top tension, the tension
  the seabed model rests the cable at, and the highest tension that keeps
  spans within the limits);
* laid cable minus seabed length within friction reach (% of plan), when
  the length check ran: below zero the cable is short and pulled taut.

Long routes stay responsive: curves are downsampled to the view, all spans
are one filled path and dense touchdown scatters are thinned (peaks kept).
Two-click measurements reuse the depth tools' ProfileMeasureController.
Hovering / clicking links to the Explorer's records and the map.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QGraphicsPathItem,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...laydata.lay_assessment import RangeFinding, estimated_bottom_tension, status_bins
from ...maptools.profile_measure_controller import ProfileMeasureController
from ...plugin_log import log_exception

_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
LEVEL_COLOURS = {-1: (200, 200, 200), 0: (76, 175, 80), 1: (31, 119, 180), 2: (255, 152, 0), 3: (211, 47, 47)}
_SEABED = (121, 85, 72)
_CABLE = (25, 70, 160)
_SPAN_FILL = (211, 47, 47, 150)
_MAX_SCATTER = 20000


def thin_peaks(x: np.ndarray, y: np.ndarray, max_points: int = _MAX_SCATTER):
    """At most ~``max_points`` of a sorted series, keeping each bucket's min and max."""
    n = len(x)
    if n <= max_points:
        return x, y
    buckets = max(max_points // 2, 1)
    edges = np.linspace(0, n, buckets + 1).astype(int)
    keep = []
    for a, b in zip(edges[:-1], edges[1:]):
        if b > a:
            chunk = y[a:b]
            keep.extend((a + int(np.argmin(chunk)), a + int(np.argmax(chunk))))
    keep = np.unique(keep)
    return x[keep], y[keep]


def spans_path(kp: np.ndarray, top: np.ndarray, bottom: np.ndarray, spans):
    """One QPainterPath with a closed polygon per span (cable above, seabed below)."""
    xs, ys, connect = [], [], []
    for span in spans:
        sl = slice(span.i0, span.i1 + 1)
        k, t, b = kp[sl], top[sl], bottom[sl]
        ok = np.isfinite(k) & np.isfinite(t) & np.isfinite(b)
        if ok.sum() < 2:
            continue
        k, t, b = k[ok], t[ok], b[ok]
        xs.append(np.concatenate((k, k[::-1])))
        ys.append(np.concatenate((t, b[::-1])))
        link = np.ones(2 * len(k), dtype=bool)
        link[-1] = False  # each polygon is its own subpath (filled as closed)
        connect.append(link)
    if not xs:
        return None
    return pg.arrayToQPath(np.concatenate(xs), np.concatenate(ys), connect=np.concatenate(connect))


def _fast(item):
    """Mark a curve to be downsampled to the pixels in view (peaks kept) once
    it is in a plot: view clipping needs its ViewBox."""
    item._fast_view = True
    return item


def _support_tip(x, y, data):
    return data if isinstance(data, str) else f"KP {x:.3f}"


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
        self._td_item = None

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
        self.td_check.toggled.connect(self._on_td_toggled)
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
        self.balance_plot = self.graphics.addPlot(row=3, col=0)
        layout_ci = self.graphics.ci.layout
        layout_ci.setRowStretchFactor(0, 1)
        layout_ci.setRowStretchFactor(1, 9)
        layout_ci.setRowStretchFactor(2, 3)
        layout_ci.setRowStretchFactor(3, 3)
        self.status_plot.setMaximumHeight(42)
        self.status_plot.hideAxis("left")
        self.status_plot.hideAxis("bottom")
        self.status_plot.setMouseEnabled(x=True, y=False)
        self.status_plot.setYRange(0, 1, padding=0)
        self.status_plot.getAxis("left").setWidth(60)
        for plot in (self.depth_plot, self.tension_plot, self.balance_plot):
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.getAxis("left").setWidth(60)
        self.depth_plot.invertY(True)
        self.depth_plot.setLabel("left", "Depth (m)")
        self.tension_plot.setLabel("left", "Bottom T (kN)")
        self.balance_plot.setLabel("left", "Cable - seabed (%)")
        self.balance_plot.setLabel("bottom", "KP (km)")
        self.balance_plot.addItem(pg.InfiniteLine(pos=0.0, angle=0, pen=pg.mkPen((120, 120, 120), width=1)))
        self.balance_plot.setToolTip("Laid cable minus seabed length within the friction reach, % of plan "
                                     "length. Below zero the cable is short of the seabed and pulled taut.")
        self.balance_plot.hide()
        for plot in (self.depth_plot, self.tension_plot, self.balance_plot):
            plot.addLegend(offset=(10, 5), labelTextColor=(40, 40, 40),
                           brush=pg.mkBrush(255, 255, 255, 210), pen=pg.mkPen(200, 200, 200))
        self.status_plot.setXLink(self.depth_plot)
        self.tension_plot.setXLink(self.depth_plot)
        self.balance_plot.setXLink(self.depth_plot)

        self.info = QLabel("Run the Lay Assessment to see the seabed profile.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addWidget(self.measure.table)
        self.measure.table.hide()  # shown once there is a measurement
        self.measure.measurementsChanged.connect(
            lambda: self.measure.table.setVisible(bool(self.measure.measurements)))

        self._vlines = []
        for plot in (self.depth_plot, self.tension_plot, self.balance_plot):
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
            if model is not None:
                below = sum(1 for sp in model.supports if sp.below_mbr)
                spans = (f"{len(model.spans)} modelled span(s) in red; triangles mark their supports"
                         + (f" ({below} red: bend radius below the MBR)" if below else "")
                         + ", hover one for its reaction and bend radius. ")
            else:
                spans = "No seabed modelled (choose a seabed source). "
            self.info.setText(spans + "Status bar: green clear, amber / red flagged, blue info, grey no data. "
                              "Hover or click to find records; Measure for lengths and heights.")
        self.replot()
        self.fit()

    def clear(self) -> None:
        self.set_result(None, None, [])

    def _add(self, plot, item, ignore_bounds: bool = False) -> None:
        plot.addItem(item, ignoreBounds=ignore_bounds)
        if getattr(item, "_fast_view", False):
            item.setDownsampling(auto=True, method="peak")
            item.setClipToView(True)
        self._items.append((plot, item))

    def _on_td_toggled(self, on: bool) -> None:
        # Only the scatter changes: no need to redraw the whole profile.
        if self._td_item is not None and self._model is not None:
            self._td_item.setVisible(bool(on))
        else:
            self.replot()

    def replot(self, *_args) -> None:
        for plot, item in self._items:
            if isinstance(item, pg.PlotDataItem):
                # View clipping needs a ViewBox: off before the item leaves it.
                item.setClipToView(False)
                item.setDownsampling(auto=False)
            plot.removeItem(item)
        self._items = []
        self._td_item = None
        records = self._records
        if records is None:
            self.measure.set_series([])
            return
        try:
            self._plot_status()
            series = self._plot_depth()
            self._plot_tension()
            self._plot_balance()
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
            filtered = model.raw_depth_m is not None and model.filter_m > 0
            if filtered:
                self._add(self.depth_plot, _fast(pg.PlotDataItem(
                    kp, model.raw_depth_m, pen=pg.mkPen((190, 160, 150), width=1), name="Seabed (sampled)",
                    connect="finite")))
            name = f"Seabed (noise filter {model.filter_m:g} m)" if filtered else "Seabed"
            seabed = _fast(pg.PlotDataItem(kp, model.seabed_depth_m, pen=pg.mkPen(_SEABED, width=2), name=name,
                                           connect="finite"))
            cable = _fast(pg.PlotDataItem(kp, model.cable_depth_m, pen=pg.mkPen(_CABLE, width=1.5),
                                          name="Cable (model)", connect="finite"))
            self._add(self.depth_plot, seabed)
            self._add(self.depth_plot, cable)
            path = spans_path(kp, model.cable_depth_m, model.seabed_depth_m, model.spans)
            if path is not None:
                # One item for every span: adding hundreds of fills one by one
                # re-fitted the view each time and took seconds.
                fill = QGraphicsPathItem(path)
                fill.setBrush(pg.mkBrush(*_SPAN_FILL))
                fill.setPen(pg.mkPen(None))
                self._add(self.depth_plot, fill, ignore_bounds=True)
            self._plot_supports(model)
            ok = np.isfinite(model.seabed_depth_m)
            series.append({"name": "Seabed", "x": kp[ok] * 1000.0, "y": model.seabed_depth_m[ok]})
            ok = np.isfinite(model.cable_depth_m)
            series.append({"name": "Cable (model)", "x": kp[ok] * 1000.0, "y": model.cable_depth_m[ok]})
        if records is not None and records.has("td_depth"):
            order = self._record_order
            depth = np.abs(records.get("td_depth")[order])
            ok = np.isfinite(depth)
            if ok.any():
                tx, ty = thin_peaks(self._record_kp_sorted[ok], depth[ok])
                td = pg.ScatterPlotItem(tx, ty, size=3, pen=pg.mkPen(None), brush=pg.mkBrush(90, 90, 90, 160),
                                        name="Touchdown depth (lay model)")
                self._add(self.depth_plot, td)
                self._td_item = td
                td.setVisible(self.td_check.isChecked() or model is None)
                if model is None:
                    series.append({"name": "Touchdown depth (lay model)",
                                   "x": self._record_kp_sorted[ok] * 1000.0, "y": depth[ok]})
        return series

    def _plot_supports(self, model) -> None:
        if not model.supports:
            return
        kps, depths, brushes, tips = [], [], [], []
        for sp in model.supports:
            k = (sp.i0 + sp.i1) // 2
            kps.append(sp.kp)
            depths.append(float(model.seabed_depth_m[k]))
            brushes.append(pg.mkBrush(211, 47, 47) if sp.below_mbr else pg.mkBrush(60, 60, 60))
            radius = ("unknown (no EI)" if not (np.isfinite(sp.bend_radius_m) or sp.bend_radius_m == math.inf)
                      else ("over 1000 m" if sp.bend_radius_m > 1000 else f"{sp.bend_radius_m:.1f} m"))
            lines = [f"Span support at KP {sp.kp:.3f}",
                     f"Seabed reaction {sp.reaction_kn:.2f} kN" if np.isfinite(sp.reaction_kn) else "",
                     f"Bend radius {radius}" + (f" (MBR {sp.mbr_m:.1f} m)" if np.isfinite(sp.mbr_m) else ""),
                     f"Moment {sp.moment_knm:.2f} kNm" if np.isfinite(sp.moment_knm) else "",
                     f"Tension {sp.tension_kn:.2f} kN"]
            tips.append("\n".join(line for line in lines if line))
        item = pg.ScatterPlotItem(np.array(kps), np.array(depths), size=8, symbol="t1", pen=pg.mkPen(None),
                                  brush=brushes, data=tips, hoverable=True, tip=_support_tip,
                                  name="Span supports")
        self._add(self.depth_plot, item)

    def _plot_tension(self) -> None:
        records = self._records
        order = self._record_order
        kp = self._record_kp_sorted
        if records.has("bottom_tension"):
            values = records.get("bottom_tension")[order]
            self._add(self.tension_plot, _fast(pg.PlotDataItem(kp, values, pen=pg.mkPen((60, 60, 60), width=1),
                                                            name="Logged (model)", connect="finite")))
        if records.has("top_tension", "td_depth"):
            estimate = estimated_bottom_tension(records)[order]
            if np.isfinite(estimate).any():
                self._add(self.tension_plot, _fast(pg.PlotDataItem(
                    kp, estimate, pen=pg.mkPen((0, 150, 136), width=1, style=_DASH),
                    name="From top tension", connect="finite")))
        model = self._model
        if model is not None and model.logged_tension_kn is not None:
            raised = np.isfinite(model.tension_kn) & (model.tension_kn > np.nan_to_num(model.logged_tension_kn) + 1e-6)
            if raised.any():
                self._add(self.tension_plot, _fast(pg.PlotDataItem(
                    model.kp_km, np.where(raised, model.tension_kn, np.nan), pen=pg.mkPen((211, 47, 47), width=2),
                    name="Pulled taut (seabed model)", connect="finite")))
        if model is not None and model.allowable_kn is not None:
            # Hide "no span at any tension" (the search cap): it is not a limit.
            limit = np.where(model.allowable_kn < 99.9, model.allowable_kn, np.nan)
            if np.isfinite(limit).any():
                self._add(self.tension_plot, _fast(pg.PlotDataItem(
                    model.kp_km, limit, pen=pg.mkPen((123, 31, 162), width=1.5, style=_DASH),
                    name="Max for spans within limits", connect="finite")))
        npts = records.cable_array("npts_kn")
        finite = npts[np.isfinite(npts)]
        if finite.size and np.allclose(finite, finite[0]):
            line = pg.InfiniteLine(pos=float(finite[0]), angle=0, pen=pg.mkPen((211, 47, 47), width=1, style=_DASH),
                                   label="NPTS", labelOpts={"position": 0.03, "color": (211, 47, 47)})
            self._add(self.tension_plot, line)

    def _plot_balance(self) -> None:
        model = self._model
        has = model is not None and model.balance_pct is not None and np.isfinite(model.balance_pct).any()
        self.balance_plot.setVisible(bool(has))
        if not has:
            return
        balance = model.balance_pct
        self._add(self.balance_plot, _fast(pg.PlotDataItem(
            model.kp_km, balance, pen=pg.mkPen((90, 90, 90), width=1), name="Cable - seabed", connect="finite")))
        with np.errstate(invalid="ignore"):
            short = np.where(balance < 0, balance, np.nan)
        if np.isfinite(short).any():
            self._add(self.balance_plot, _fast(pg.PlotDataItem(
                model.kp_km, short, pen=pg.mkPen((211, 47, 47), width=2), name="Short of seabed",
                connect="finite")))

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
        self.balance_plot.enableAutoRange(axis="y")

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
        for plot in (self.depth_plot, self.tension_plot, self.balance_plot):
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
