# -*- coding: utf-8 -*-
"""Ground-model plot: depth below seabed (down) vs KP (vendored pyqtgraph).

The KP axis is linked to the dock's persistent bathymetry profile through
``BurialProfileWidget.link_kp_plot`` — same fixed left-axis width, same
zoom/pan — so the soil units line up under the depth profile exactly like
the slope and DCC panels. Units paint from a single ``GraphicsObject``
(the ``RangeBandItem`` lesson: one item per unit does not scale to an
imported model with thousands of rows).

Signals:
    kpHovered(float)        crosshair moved to this KP
    kpClicked(float)        left click at this KP
    unitClicked(str)        left click landed inside this unit (unit_id)
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import pyqtgraph as pg

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPolygonF
from qgis.PyQt.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import ground_model, schema

_PEN_STYLE = getattr(Qt, "PenStyle", Qt)
_MOUSE_LEFT = getattr(Qt, "MouseButton", Qt).LeftButton

# How far an open-based unit is drawn below the deepest closed base.
_OPEN_EXTRA_M = 1.5
_MIN_DEPTH_SPAN_M = 3.0


class UnitPolygonItem(pg.GraphicsObject):
    """All ground-model units painted from one item, clipped to the view."""

    def __init__(self):
        super().__init__()
        self._units: List[Tuple] = []   # (lo, hi, top0, top1, base0, base1, brush, unit_id)
        self._starts: List[float] = []
        self._max_len = 0.0
        self._xmin = 0.0
        self._xmax = 0.0
        self._ymax = 0.0
        self._selected = ""
        self._edge_pen = pg.mkPen(QColor(60, 60, 60, 140), width=0.8)
        self._select_pen = pg.mkPen(QColor(20, 20, 20), width=2.2)

    def set_units(self, units: List[Dict], colors: Dict[str, str]) -> None:
        cleaned = []
        ymax = 0.0
        for unit in units:
            try:
                lo = float(unit.get("start_kp"))
                hi = float(unit.get("end_kp"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
                continue
            top0 = float(unit.get("top_m") or 0.0)
            top1 = unit.get("top_end_m")
            top1 = top0 if top1 is None else float(top1)
            base0 = unit.get("base_m")
            base1 = unit.get("base_end_m")
            base0 = None if base0 is None else float(base0)
            base1 = base0 if base1 is None else float(base1)
            for value in (top0, top1, base0, base1):
                if value is not None and math.isfinite(value):
                    ymax = max(ymax, value)
            code = str(unit.get("soil_class") or "")
            color = QColor(colors.get(code.casefold(), "#c8c8c8"))
            color.setAlpha(190)
            cleaned.append((lo, hi, top0, top1, base0, base1,
                            pg.mkBrush(color), str(unit.get("unit_id") or "")))
        cleaned.sort(key=lambda u: (u[0], u[2]))
        self._units = cleaned
        self._starts = [u[0] for u in cleaned]
        self._max_len = max((u[1] - u[0] for u in cleaned), default=0.0)
        self._xmin = cleaned[0][0] if cleaned else 0.0
        self._xmax = max((u[1] for u in cleaned), default=0.0)
        self._ymax = ymax
        self.prepareGeometryChange()
        self.update()

    def set_selected(self, unit_id: str) -> None:
        self._selected = str(unit_id or "")
        self.update()

    @property
    def depth_extent(self) -> float:
        return self._ymax

    def _visible(self, lo: float, hi: float):
        import bisect

        first = bisect.bisect_left(self._starts, lo - self._max_len)
        for index in range(first, len(self._units)):
            entry = self._units[index]
            if entry[0] > hi:
                break
            if entry[1] >= lo:
                yield entry

    def boundingRect(self):
        if not self._units:
            return QRectF()
        view = self.viewRect()
        if view is None:
            return QRectF(self._xmin, 0.0, self._xmax - self._xmin,
                          max(self._ymax + _OPEN_EXTRA_M, _MIN_DEPTH_SPAN_M))
        rect = QRectF(view)
        rect.setLeft(min(rect.left(), self._xmin))
        rect.setRight(max(rect.right(), self._xmax))
        return rect

    def dataBounds(self, axis, frac=1.0, orthoRange=None):
        if not self._units:
            return None
        if axis == 0:
            return (self._xmin, self._xmax)
        return (0.0, max(self._ymax + _OPEN_EXTRA_M, _MIN_DEPTH_SPAN_M))

    def viewRangeChanged(self) -> None:
        self.prepareGeometryChange()
        self.update()

    def _open_bottom(self, view) -> float:
        # Open units run to whichever is deeper: the view bottom or the
        # deepest closed base plus a margin.
        bottom = max(view.top(), view.bottom())
        return max(bottom, self._ymax + _OPEN_EXTRA_M)

    def paint(self, painter, *_args) -> None:
        if not self._units:
            return
        view = self.viewRect()
        if view is None:
            return
        open_bottom = self._open_bottom(view)
        painter.setPen(self._edge_pen)
        selected_poly = None
        for lo, hi, top0, top1, base0, base1, brush, unit_id in \
                self._visible(view.left(), view.right()):
            b0 = open_bottom if base0 is None else base0
            b1 = open_bottom if base1 is None else base1
            poly = QPolygonF([QPointF(lo, top0), QPointF(hi, top1),
                              QPointF(hi, b1), QPointF(lo, b0)])
            painter.setBrush(brush)
            painter.drawPolygon(poly)
            if unit_id and unit_id == self._selected:
                selected_poly = poly
        if selected_poly is not None:
            painter.setPen(self._select_pen)
            painter.setBrush(pg.mkBrush(None))
            painter.drawPolygon(selected_poly)


class GroundModelPlot(QWidget):
    kpHovered = pyqtSignal(float)
    kpClicked = pyqtSignal(float)
    unitClicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._units: List[Dict] = []
        self._classes_by_code: Dict[str, Dict] = {}
        self._scope = (0.0, 0.0)
        self._target_m: Optional[float] = None

        self.plot = pg.PlotWidget()
        self.plot.setBackground("w")
        self.plot.setMenuEnabled(True)
        self.plot.setLabel("bottom", "KP", units="km")
        self.plot.setLabel("left", "Depth below seabed", units="m")
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setMinimumHeight(120)
        item = self.plot.getPlotItem()
        item.showGrid(x=True, y=True, alpha=0.25)
        item.vb.invertY(True)
        item.vb.enableAutoRange(y=True)
        item.vb.setAutoVisible(y=True)
        for name in ("bottom", "left"):
            try:
                item.getAxis(name).enableAutoSIPrefix(False)
            except Exception:
                pass

        self._unit_item = UnitPolygonItem()
        self._unit_item.setZValue(5)
        item.addItem(self._unit_item)

        self._seabed_line = pg.InfiniteLine(
            angle=0, pos=0.0, movable=False,
            pen=pg.mkPen((90, 90, 90), width=1.4))
        item.addItem(self._seabed_line, ignoreBounds=True)
        # Target burial depth: a stepped line so KP-range targets (deeper
        # through a shipping lane, …) read directly against the units.
        self._target_line = item.plot(
            [], [], pen=pg.mkPen("#1b7f3b", width=1.6,
                                 style=_PEN_STYLE.DashLine),
            connect="finite")
        self._target_line.setZValue(15)
        self._target_label = pg.TextItem(color="#1b7f3b", anchor=(0, 1))
        self._target_label.setZValue(16)
        self._target_label.setVisible(False)
        item.addItem(self._target_label, ignoreBounds=True)
        self._target_runs: List = []

        self._vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen((120, 120, 120), width=1, style=_PEN_STYLE.DashLine))
        self._vline.setZValue(20)
        self._vline.setVisible(False)
        item.addItem(self._vline, ignoreBounds=True)
        self._readout = pg.TextItem(anchor=(0, 1), color=(30, 30, 30),
                                    fill=pg.mkBrush(255, 255, 255, 215))
        self._readout.setZValue(30)
        self._readout.setVisible(False)
        item.addItem(self._readout, ignoreBounds=True)

        self.legend = QLabel("")
        self.legend.setWordWrap(True)
        self.legend.setTextFormat(getattr(Qt, "TextFormat", Qt).RichText)
        self.legend.setContentsMargins(8, 2, 8, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.legend)
        layout.addWidget(self.plot, 1)

        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)
        self.plot.scene().sigMouseClicked.connect(self._mouse_clicked)

    # -- data ----------------------------------------------------------------
    def set_units(self, units: List[Dict], classes: List[Dict]) -> None:
        self._units = [ground_model.normalise_unit(u) for u in units or []]
        self._classes_by_code = ground_model.class_lookup(classes)
        codes = []
        for unit in self._units:
            code = unit.get("soil_class") or ""
            if code and code.casefold() not in [c.casefold() for c in codes]:
                codes.append(code)
        colors = {code.casefold(): ground_model.color_for(code, self._classes_by_code)
                  for code in codes}
        self._unit_item.set_units(self._units, colors)
        self._update_legend(codes, colors)
        self._fit_depth()

    def set_scope(self, start_kp: float, end_kp: float) -> None:
        lo, hi = sorted((float(start_kp), float(end_kp)))
        self._scope = (lo, hi)

    def set_target_depth(self, depth_m: Optional[float]) -> None:
        """A single target over the whole scope (see set_target_runs)."""
        try:
            value = None if depth_m is None else float(depth_m)
        except (TypeError, ValueError):
            value = None
        lo, hi = self._scope
        if hi <= lo:
            lo, hi = 0.0, 1.0
        self.set_target_runs([(lo, hi, value)])

    def set_target_runs(self, runs) -> None:
        """``(start_kp, end_kp, depth|None)`` runs → one stepped line."""
        xs: List[float] = []
        ys: List[float] = []
        cleaned = []
        nan = float("nan")
        for start, end, depth in runs or []:
            try:
                value = None if depth is None else float(depth)
            except (TypeError, ValueError):
                value = None
            if value is None or value <= 0:
                if xs:
                    xs.append(float(start))
                    ys.append(nan)  # break the line where there is no target
                continue
            cleaned.append((float(start), float(end), value))
            xs.extend([float(start), float(end)])
            ys.extend([value, value])
        self._target_runs = cleaned
        self._target_m = max((d for _s, _e, d in cleaned), default=None)
        self._target_line.setData(xs, ys, connect="finite")
        if cleaned:
            depths = sorted({d for _s, _e, d in cleaned})
            text = ("Target burial " + (f"{depths[0]:.2f} m" if len(depths) == 1
                    else f"{depths[0]:g}–{depths[-1]:g} m"))
            self._target_label.setText(text)
            self._target_label.setPos(cleaned[0][0], cleaned[0][2])
            self._target_label.setVisible(True)
        else:
            self._target_label.setVisible(False)
        self._fit_depth()

    def target_at(self, kp: float) -> Optional[float]:
        for start, end, depth in self._target_runs:
            if start - 1e-9 <= kp <= end + 1e-9:
                return depth
        return None

    def set_selected(self, unit_id: str) -> None:
        self._unit_item.set_selected(unit_id)

    def clear(self) -> None:
        self.set_units([], [])
        self._vline.setVisible(False)
        self._readout.setVisible(False)

    def _fit_depth(self) -> None:
        deepest = max(self._unit_item.depth_extent, self._target_m or 0.0)
        span = max(deepest + _OPEN_EXTRA_M, _MIN_DEPTH_SPAN_M)
        self.plot.getPlotItem().vb.setYRange(0.0, span, padding=0.04)

    def _update_legend(self, codes: List[str], colors: Dict[str, str]) -> None:
        if not codes:
            self.legend.setText("<span style='color:#777'>No ground-model "
                                "units for this plan.</span>")
            return
        parts = []
        for code in codes:
            color = colors.get(code.casefold(), "#c8c8c8")
            label = ground_model.label_for(code, self._classes_by_code)
            text = code if label.casefold() == code.casefold() else f"{code} — {label}"
            parts.append(f"<span style='color:{color}; font-size:15px'>■</span> "
                         f"<b>{_esc(text)}</b>")
        self.legend.setText("&nbsp;&nbsp;".join(parts))

    # -- interaction ---------------------------------------------------------
    def _view_pos(self, scene_pos):
        if not self.plot.sceneBoundingRect().contains(scene_pos):
            return None
        view = self.plot.getViewBox().mapSceneToView(scene_pos)
        return float(view.x()), float(view.y())

    def unit_at(self, kp: float, depth_m: float) -> Optional[Dict]:
        return ground_model.unit_at(self._units, kp, depth_m)

    def _readout_text(self, kp: float, depth: float) -> str:
        lines = [f"KP {schema.format_kp(kp)}   {max(0.0, depth):.2f} m bsb"]
        hit = self.unit_at(kp, depth) if depth >= 0 else None
        if hit is not None:
            code = hit.get("soil_class") or "(unclassified)"
            label = ground_model.label_for(code, self._classes_by_code)
            head = code if label.casefold() == code.casefold() else f"{code} — {label}"
            top, base = ground_model.unit_depths_at(hit, kp)
            span = f"{top:.2f}–{base:.2f} m" if base is not None else f"from {top:.2f} m (open)"
            lines.append(f"{head}  [{span}]")
            for key in ("description", "strength"):
                if hit.get(key):
                    lines.append(str(hit[key])[:80])
            if hit.get("rereference_flags"):
                lines.append(f"KP re-reference: {hit['rereference_flags']}")
        column = ground_model.units_at_kp(self._units, kp)
        if column and hit is None:
            lines.append("no unit at this depth")
        return "\n".join(lines)

    def show_kp(self, kp: float, depth: Optional[float] = None) -> None:
        self._vline.setPos(kp)
        self._vline.setVisible(True)
        probe = 0.0 if depth is None else depth
        self._readout.setText(self._readout_text(kp, probe))
        self._readout.setPos(kp, probe)
        self._readout.setVisible(True)

    def _mouse_moved(self, pos) -> None:
        hit = self._view_pos(pos)
        if hit is None:
            self._vline.setVisible(False)
            self._readout.setVisible(False)
            return
        kp, depth = hit
        self.show_kp(kp, depth)
        self.kpHovered.emit(kp)

    def _mouse_clicked(self, event) -> None:
        try:
            if event.button() != _MOUSE_LEFT:
                return
        except (AttributeError, TypeError):
            pass
        hit = self._view_pos(event.scenePos())
        if hit is None:
            return
        kp, depth = hit
        unit = self.unit_at(kp, depth)
        if unit is not None and unit.get("unit_id"):
            self.unitClicked.emit(str(unit["unit_id"]))
        self.kpClicked.emit(kp)


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
