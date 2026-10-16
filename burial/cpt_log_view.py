# -*- coding: utf-8 -*-
"""CPT log window: one or more CPTs as side-by-side tracks on one depth axis.

The layout follows common CPT log presentation (e.g. CPeT-IT, gINT,
OpenGround): depth below seabed increasing downward on a shared, linked axis;
qc/qt, fs, pore pressure (with u0), Rf, Ic, Su (and optionally Dr,
inclination and σ'v0) as tracks; the interpreted horizons and an SBTn
colour strip on the left; the plan's target burial depth across every
track.

- One crosshair spans every track; the readout lists each shown CPT's
  values at the cursor depth.
- Drag to pan and wheel to zoom: depth zoom is shared by every track, value
  zoom is per track. *Fit* resets the view; each track can be logarithmic.
- *Compare* overlays other CPTs (one colour each). ◀ ▶ step along the route.
- Su is drawn as a band: Nkt 15 (solid) and the lower estimate with
  Nkt 20 (dashed), over the BS 5930 / ISO 14688-2 strength terms; qc is
  drawn over indicative density terms. Interpretations delivered in the
  AGS file (SCPP / CPTP) are drawn as orange steps for comparison.
- The *SBT chart* tab plots the readings on the Robertson (2009/2010)
  normalised chart (Qtn vs Fr, Ic boundaries); *Readings* lists the
  measured and derived values and exports them to CSV.

Plots and curves are built once and updated in place (``setData``): removing
pyqtgraph items raises "unable to convert a QVariant" under QGIS 3.x's
PyQt5, so stepping through CPTs never removes anything.
"""

from __future__ import annotations

import csv
import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

import pyqtgraph as pg
from qgis.PyQt.QtCore import QRectF, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QTextOption
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableView,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..geotech import cpt as cpt_mod
from ..qgis_compat import PEN_STYLE_DASH, TOOLBUTTON_POPUP_MODE_INSTANT, WINDOW_TYPE_WINDOW
from . import schema, ui_helpers
from .cpt_register import side_text
from .numeric_profile_dialogs import RowsModel

_SETTINGS = "SubseaCableTools/BurialPlanner/cpt_log"
_COLOURS = ["#1f4e9c", "#d1495b", "#2a9d8f", "#e9a23b", "#6a4c93", "#3d405b", "#8f2d56",
            "#1b998b"]
_MAX_COMPARE = 8

# key, title, channels drawn (first = main), log scale by default, shown by default
TRACKS: List[Tuple[str, str, Tuple[str, ...], bool, bool]] = [
    ("qc", "qc, qt (MPa)", ("qc", "qt"), False, True),
    ("fs", "fs (kPa)", ("fs",), False, True),
    ("u", "u, u0 (kPa)", ("u", "u0"), False, True),
    ("rf", "Rf (%)", ("rf",), False, True),
    ("ic", "Ic", ("ic",), False, True),
    ("su", "Su (kPa)", ("su", "su_low"), False, True),
    ("dr", "Dr (%)", ("dr",), False, False),
    ("bq", "Bq", ("bq",), False, False),
    ("incl", "Inclination (°)", ("incl",), False, False),
    ("sv0_eff", "σ'v0, σv0 (kPa)", ("sv0_eff", "sv0"), False, False),
]
_TRACK_KEYS = [t[0] for t in TRACKS]


def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def class_colour(code: str) -> str:
    """A stable pastel colour for a horizon class code."""
    text = str(code or "").strip()
    if not text:
        return "#d9d9d9"
    value = sum((i + 1) * ord(ch) for i, ch in enumerate(text))
    hue = (value * 47) % 360
    colour = QColor()
    colour.setHsv(hue, 70, 235)
    return colour.name()


_ALIGN = getattr(Qt, "AlignmentFlag", Qt)
_WRAP = getattr(QTextOption, "WrapMode", QTextOption).WordWrap
_UNLIMITED = 16777215


class StrataItem(pg.GraphicsObject):
    """A CPT's horizons as labelled boxes across x 0–1, painted by one item;
    descriptions wrap inside their box and are clipped to it."""

    def __init__(self):
        super().__init__()
        self._layers: List[Tuple[float, float, QColor, str, str]] = []
        self._deepest = 1.0

    def set_layers(self, layers, deepest: float) -> None:
        self.prepareGeometryChange()
        self._layers = list(layers)
        self._deepest = max(float(deepest or 0.0), 0.1)
        self.update()

    def layer_at(self, depth: float):
        return next((layer for layer in self._layers if layer[0] <= depth < layer[1]), None)

    def boundingRect(self):
        return QRectF(0.0, 0.0, 1.0, self._deepest)

    def paint(self, painter, *_args) -> None:
        if not self._layers:
            return
        transform = painter.transform()
        painter.setPen(pg.mkPen((90, 90, 90), width=0.6))
        for top, base, colour, _code, _text in self._layers:
            painter.setBrush(pg.mkBrush(colour))
            painter.drawRect(QRectF(0.0, top, 1.0, base - top))
        option = QTextOption()
        option.setWrapMode(_WRAP)
        option.setAlignment(_ALIGN.AlignLeft | _ALIGN.AlignTop)
        painter.save()
        painter.resetTransform()
        painter.setPen(pg.mkPen((25, 25, 25)))
        for top, base, _colour, code, text in self._layers:
            rect = transform.mapRect(QRectF(0.0, top, 1.0, base - top)).adjusted(3, 2, -2, -1)
            if rect.height() < 9 or rect.width() < 12:
                continue
            painter.setClipRect(rect)
            label = "\n".join(v for v in (code, text) if v)
            painter.drawText(rect, label, option)
        painter.restore()


class ZoneStripItem(pg.GraphicsObject):
    """SBTn zones as coloured runs across x 0–1, painted by one item."""

    def __init__(self):
        super().__init__()
        self._runs: List[Tuple[float, float, QColor]] = []
        self._deepest = 1.0

    def set_zones(self, depths: Sequence[float], zones: Sequence[Optional[int]]) -> None:
        runs = []
        start = 0
        for i in range(1, len(depths) + 1):
            if i < len(depths) and zones[i] == zones[start]:
                continue
            zone = zones[start]
            top = depths[start] if start == 0 else (depths[start - 1] + depths[start]) / 2.0
            base = depths[i - 1] if i == len(depths) else (depths[i - 1] + depths[i]) / 2.0
            if zone in cpt_mod.SBT_ZONES and base > top:
                runs.append((top, base, QColor(cpt_mod.SBT_ZONES[zone][1])))
            start = i
        self.prepareGeometryChange()
        self._runs = runs
        self._deepest = max([max(depths)] if depths else [1.0])
        self.update()

    def boundingRect(self):
        return QRectF(0.0, 0.0, 1.0, max(self._deepest, 0.1))

    def paint(self, painter, *_args) -> None:
        painter.setPen(pg.mkPen(None))
        for top, base, colour in self._runs:
            painter.setBrush(pg.mkBrush(colour))
            painter.drawRect(QRectF(0.0, top, 1.0, base - top))


class CptLogWindow(QDialog):
    """Shows CPTs from a ``CptRegister``; ``cptChosen`` follows the main CPT."""

    cptChosen = pyqtSignal(str)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.setWindowFlags(self.windowFlags() | WINDOW_TYPE_WINDOW)
        self.setWindowTitle("CPT logs")
        self.resize(1180, 820)
        self.order: List[str] = []           # cpt ids in route order (navigation)
        self.placements: Dict[str, Dict] = {}
        self.current = ""                    # main CPT id
        self.compare: List[str] = []         # extra CPT ids
        self.plots: List = []
        self.hlines: List = []
        self._track_plots: Dict[str, object] = {}
        self._widths: Dict[int, int] = {}
        settings = QSettings()
        visible = settings.value(f"{_SETTINGS}/tracks", None)
        self.visible_tracks = [k for k in (visible if isinstance(visible, list) else
                                           [t[0] for t in TRACKS if t[4]]) if k in _TRACK_KEYS] \
            or [t[0] for t in TRACKS if t[4]]
        logs = settings.value(f"{_SETTINGS}/log_tracks", None)
        self.log_tracks = {k for k in (logs if isinstance(logs, list) else []) if k in _TRACK_KEYS}
        self.show_bands = bool(settings.value(f"{_SETTINGS}/bands", True, type=bool))

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.choose = QComboBox()
        self.choose.setMinimumWidth(260)
        self.choose.setToolTip("The CPT shown; placed CPTs are listed in KP order.")
        self.choose.currentIndexChanged.connect(self._chosen)
        self.prev_button = QPushButton("◀")
        self.prev_button.setToolTip("Previous CPT along the route")
        self.prev_button.clicked.connect(lambda: self._step(-1))
        self.next_button = QPushButton("▶")
        self.next_button.setToolTip("Next CPT along the route")
        self.next_button.clicked.connect(lambda: self._step(1))
        for button in (self.prev_button, self.next_button):
            button.setMaximumWidth(34)
        self.compare_button = QToolButton()
        self.compare_button.setText("Compare ▾")
        self.compare_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        self.compare_button.setToolTip(f"Overlay up to {_MAX_COMPARE} other CPTs on the same tracks.")
        self.compare_menu = QMenu(self.compare_button)
        self.compare_menu.aboutToShow.connect(self._build_compare_menu)
        self.compare_button.setMenu(self.compare_menu)
        self.tracks_button = QToolButton()
        self.tracks_button.setText("Tracks ▾")
        self.tracks_button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
        self.tracks_button.setToolTip("Choose the tracks shown and which use a logarithmic scale.")
        self.tracks_menu = QMenu(self.tracks_button)
        self.tracks_menu.aboutToShow.connect(self._build_tracks_menu)
        self.tracks_button.setMenu(self.tracks_menu)
        fit = QPushButton("Fit")
        fit.setToolTip("Show every reading (depth and values).")
        fit.clicked.connect(self.fit)
        export = QPushButton("Export CSV…")
        export.setToolTip("Measured and derived values of the CPTs shown, one row per reading.")
        export.clicked.connect(lambda: self.export_csv())
        image = QPushButton("Save image…")
        image.clicked.connect(lambda: self.save_image())
        for widget in (QLabel("CPT"), self.choose, self.prev_button, self.next_button,
                       self.compare_button, self.tracks_button, fit):
            bar.addWidget(widget)
        bar.addStretch(1)
        bar.addWidget(export)
        bar.addWidget(image)
        layout.addLayout(bar)
        self.header = QLabel()
        self.header.setWordWrap(True)
        self.header.setTextFormat(getattr(Qt, "TextFormat", Qt).RichText)
        self.header.setTextInteractionFlags(getattr(Qt, "TextInteractionFlag", Qt).TextSelectableByMouse)
        layout.addWidget(self.header)
        self.tabs = QTabWidget()
        self.log_page = QWidget()
        log_layout = QVBoxLayout(self.log_page)
        log_layout.setContentsMargins(0, 0, 0, 0)
        self.graphics = pg.GraphicsLayoutWidget()
        self.graphics.setBackground("w")
        log_layout.addWidget(self.graphics, 1)
        self.readout = QLabel(" ")
        self.readout.setWordWrap(True)
        self.readout.setTextFormat(getattr(Qt, "TextFormat", Qt).RichText)
        self.readout.setMinimumHeight(36)
        log_layout.addWidget(self.readout)
        self.tabs.addTab(self.log_page, "Log")
        self.chart = pg.PlotWidget()
        self.chart.setBackground("w")
        self.tabs.addTab(self.chart, "SBT chart")
        self.table = QTableView()
        self.tabs.addTab(self.table, "Readings")
        layout.addWidget(self.tabs, 1)
        hint = QLabel("Drag to pan, wheel to zoom (depth zoom is shared by every track); right-click a "
                      "track for its axis options. Derived values use the CPT settings of this project "
                      "(Ground Model ▸ CPTs ▸ Settings…) and are screening values.")
        hint.setWordWrap(True)
        hint.setStyleSheet(ui_helpers.hint_style())
        layout.addWidget(hint)
        self.graphics.scene().sigMouseMoved.connect(self._mouse_moved)
        self._build_plots()
        self._build_chart()

    # -- data ------------------------------------------------------------------------
    @property
    def register(self):
        return self.model.cpts

    def set_order(self, order: Sequence[str], placements: Optional[Dict[str, Dict]] = None) -> None:
        """CPT ids in navigation order and their placements on the plan route."""
        self.order = list(order)
        self.placements = dict(placements or {})
        current = self.current
        self.choose.blockSignals(True)
        self.choose.clear()
        for cpt_id in self.order:
            cpt = self.register.by_id(cpt_id)
            if cpt is None:
                continue
            placement = self.placements.get(cpt_id) or {}
            label = cpt.get("name") or cpt_id
            if placement.get("kp") is not None:
                label += f"  —  KP {schema.format_kp(placement['kp'])}"
                if placement.get("status") != "placed":
                    label += " (off route)"
            self.choose.addItem(label, cpt_id)
        self.choose.setCurrentIndex(max(0, self.choose.findData(current)))
        self.choose.blockSignals(False)
        self._sync_nav()

    def show_cpts(self, cpt_ids: Sequence[str]) -> None:
        """Show ``cpt_ids[0]`` with the others overlaid."""
        ids = [i for i in cpt_ids if self.register.by_id(i) is not None]
        if not ids:
            return
        self.current = ids[0]
        self.compare = [i for i in ids[1:] if i != self.current][:_MAX_COMPARE]
        self.choose.blockSignals(True)
        self.choose.setCurrentIndex(max(0, self.choose.findData(self.current)))
        self.choose.blockSignals(False)
        self.redraw()
        self.cptChosen.emit(self.current)

    def channels(self, cpt_id: str) -> Dict[str, List]:
        cpt = self.register.by_id(cpt_id)
        if cpt is None:
            return {"depth": []}
        return self.register.channels(cpt, self.model.cpt_settings())

    def shown(self) -> List[str]:
        known = [i for i in ([self.current] if self.current else []) + self.compare
                 if self.register.by_id(i) is not None]
        return list(dict.fromkeys(known))

    def sync(self) -> None:
        """After the CPTs changed (re-import, removal, settings): drop CPTs
        that no longer exist and redraw."""
        if not self.isVisible():
            return
        self.compare = [i for i in self.compare if self.register.by_id(i) is not None]
        if self.register.by_id(self.current) is None:
            self.current = self.order[0] if self.order else ""
            self.choose.blockSignals(True)
            self.choose.setCurrentIndex(max(0, self.choose.findData(self.current)))
            self.choose.blockSignals(False)
        self.redraw()

    def _colour(self, index: int) -> str:
        return _COLOURS[index % len(_COLOURS)]

    def _target_depth(self, cpt_id: str) -> Optional[float]:
        placement = self.placements.get(cpt_id) or {}
        kp = placement.get("kp")
        if not getattr(self.model, "plan", None):
            return None
        if kp is not None:
            for start, end, depth in self.model.target_runs():
                if depth and start - 1e-9 <= kp <= end + 1e-9:
                    return float(depth)
        default = self.model.target_default()
        return float(default) if default else None

    # -- drawing ---------------------------------------------------------------------
    def redraw(self) -> None:
        self._channels_for_shown = {cpt_id: self.channels(cpt_id) for cpt_id in self.shown()}
        self._update_header()
        self._draw_log()
        self._draw_chart()
        self._fill_table()
        self._sync_nav()

    def _update_header(self) -> None:
        cpt = self.register.by_id(self.current)
        if cpt is None:
            self.header.setText("No CPT selected.")
            return
        placement = self.placements.get(self.current) or {}
        campaign = self.register.campaign(cpt.get("campaign_id") or "") or {}
        parts = [f"<b style='font-size:13px'>{_esc(cpt.get('name'))}</b>"]
        if placement.get("kp") is not None:
            parts.append(f"KP {schema.format_kp(placement['kp'])} ({_esc(side_text(placement.get('offset_m')))})")
        for label, value, unit in (("water depth", cpt.get("water_depth_m"), "m"),
                                   ("penetration", cpt.get("final_depth_m") or cpt.get("max_depth_m"), "m")):
            if value is not None:
                parts.append(f"{label} {value:.2f} {unit}")
        if cpt.get("tested"):
            parts.append(_esc(cpt["tested"]))
        parts.append(f"{_esc(cpt.get('source_file'))} ({_esc(cpt.get('ags_version'))})")
        if campaign.get("name"):
            parts.append(_esc(campaign["name"]) + (f" {_esc(campaign['revision'])}" if campaign.get("revision") else ""))
        text = " · ".join(parts)
        if cpt.get("summary"):
            text += f"<br><span style='color:#444'>{_esc(cpt['summary'])}</span>"
        warnings = (cpt.get("meta") or {}).get("warnings") or []
        if warnings:
            text += f"<br><span style='color:#a15c00'>⚠ {_esc('; '.join(warnings[:3]))}</span>"
        others = [self.register.by_id(i) for i in self.compare]
        if others:
            legend = " ".join(f"<span style='color:{self._colour(i + 1)}'>■ {_esc(o.get('name'))}</span>"
                              for i, o in enumerate(others) if o)
            text += f"<br><span style='color:{self._colour(0)}'>■ {_esc(cpt.get('name'))}</span> {legend}"
        self.header.setText(text)

    # -- persistent plots ----------------------------------------------------------
    def _new_plot(self, column: int, title: str, first_plot=None, width: Optional[int] = None):
        plot = self.graphics.addPlot(row=0, col=column)
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.showAxis("top")
        plot.hideAxis("bottom")
        plot.setLabel("top", title)
        plot.vb.invertY(True)
        for name in ("top", "left"):
            try:
                plot.getAxis(name).enableAutoSIPrefix(False)
            except Exception:
                pass
        if first_plot is None:
            plot.setLabel("left", "Depth below seabed", units="m")
            plot.getAxis("left").setWidth(56)
        else:
            plot.setYLink(first_plot)
            plot.getAxis("left").setStyle(showValues=False)
            plot.getAxis("left").setWidth(6)
        if width:
            self._widths[id(plot)] = width
            plot.setMinimumWidth(width)
            plot.setMaximumWidth(width)
            plot.vb.setMouseEnabled(x=False, y=True)
        line = pg.InfiniteLine(angle=0, movable=False,
                               pen=pg.mkPen((90, 90, 90), width=1, style=PEN_STYLE_DASH))
        line.setVisible(False)
        plot.addItem(line, ignoreBounds=True)
        self.plots.append(plot)
        self.hlines.append(line)
        return plot

    def _set_plot_visible(self, plot, visible: bool) -> None:
        width = self._widths.get(id(plot))
        plot.setVisible(bool(visible))
        plot.setMinimumWidth(width if visible and width else 0)
        plot.setMaximumWidth((width or _UNLIMITED) if visible else 0)

    def _build_plots(self) -> None:
        try:
            self.graphics.ci.layout.setHorizontalSpacing(3)
        except AttributeError:
            pass
        self.strata_plot = self._new_plot(0, "Strata", None, width=150)
        self.strata_plot.setXRange(0, 1, padding=0)
        self.strata_plot.getAxis("top").setStyle(showValues=False)
        self.strata_item = StrataItem()
        self.strata_plot.addItem(self.strata_item)
        self.target_label = pg.TextItem("", color="#1b7f3b", anchor=(0, 1))
        self.target_label.setVisible(False)
        self.strata_plot.addItem(self.target_label, ignoreBounds=True)
        self.zone_plot = self._new_plot(1, "SBTn", self.strata_plot, width=34)
        self.zone_plot.setXRange(0, 1, padding=0)
        self.zone_plot.getAxis("top").setStyle(showValues=False)
        self.zone_item = ZoneStripItem()
        self.zone_plot.addItem(self.zone_item)
        self._tracks: Dict[str, Dict] = {}
        for column, (key, title, _keys, _log, _default) in enumerate(TRACKS, start=2):
            plot = self._new_plot(column, title, self.strata_plot)
            entry = {"plot": plot, "title": title, "curves": {}, "interpreted": [], "log": False,
                     "regions": []}
            target = pg.InfiniteLine(angle=0, movable=False,
                                     pen=pg.mkPen("#1b7f3b", width=1.4, style=PEN_STYLE_DASH))
            target.setVisible(False)
            plot.addItem(target, ignoreBounds=True)
            entry["target"] = target
            if key == "ic":
                for boundary in cpt_mod.IC_BOUNDARIES:
                    plot.addItem(pg.InfiniteLine(pos=boundary, angle=90, movable=False,
                                                 pen=pg.mkPen((180, 180, 180), width=1, style=PEN_STYLE_DASH)),
                                 ignoreBounds=True)
            if key in ("su", "qc"):
                terms = cpt_mod.SU_TERMS if key == "su" else cpt_mod.QC_TERMS
                for index, (lo, hi, label) in enumerate(terms):
                    shade = QColor(232, 236, 244) if index % 2 else QColor(245, 246, 250)
                    region = pg.LinearRegionItem(values=(0, 1), orientation="vertical", movable=False,
                                                 brush=pg.mkBrush(shade), pen=pg.mkPen(None))
                    region.setZValue(-20)
                    region.setToolTip(f"{label}: {lo:g}" + (f"–{hi:g}" if hi is not None else "+")
                                      + f" {cpt_mod.channel_unit(key)}")
                    plot.addItem(region, ignoreBounds=True)
                    entry["regions"].append((region, lo, hi))
                self._place_regions(entry)
            if key in ("su", "dr", "ic"):
                for n in range(2):
                    style = {"style": PEN_STYLE_DASH} if n else {}
                    entry["interpreted"].append(plot.plot([], [], pen=pg.mkPen("#d9822b", width=2.0, **style),
                                                          connect="finite"))
            self._tracks[key] = entry

    @staticmethod
    def _place_regions(entry: Dict) -> None:
        log = entry["log"]
        for region, lo, hi in entry["regions"]:
            upper = hi if hi is not None else (lo * 3.0 if lo else 1.0)
            left, right = lo, upper
            if log:
                left, right = math.log10(max(lo, upper / 1000.0)), math.log10(upper)
            region.setRegion((left, right))

    def _curve(self, entry: Dict, slot: int, n: int):
        key = (slot, n)
        if key not in entry["curves"]:
            entry["curves"][key] = entry["plot"].plot([], [], connect="finite")
        return entry["curves"][key]

    def _draw_log(self) -> None:
        shown = self.shown()
        self._track_plots = {}
        deepest = max((max(self._channels_for_shown[i]["depth"] or [0]) for i in shown), default=1.0)
        target = self._target_depth(self.current) if shown else None
        self._draw_strata(deepest)
        main = self._channels_for_shown.get(self.current) if shown else None
        zones = (main or {}).get("zone") or []
        has_zones = any(z is not None for z in zones)
        self.zone_item.set_zones((main or {}).get("depth") or [], zones if has_zones else [])
        self._set_plot_visible(self.zone_plot, has_zones)
        opts = self.model.cpt_settings()
        interpreted = ((self.register.by_id(self.current) or {}).get("meta") or {}).get("interpreted") or []
        for key, _title, keys, _log, _default in TRACKS:
            entry = self._tracks[key]
            plot = entry["plot"]
            has = any(any(v is not None for v in (self._channels_for_shown[i].get(keys[0]) or []))
                      for i in shown)
            visible = has and key in self.visible_tracks
            self._set_plot_visible(plot, visible)
            used = set()
            if visible:
                self._track_plots[key] = plot
                log = key in self.log_tracks
                if entry["log"] != log:
                    entry["log"] = log
                    plot.setLogMode(x=log, y=False)
                    self._place_regions(entry)
                if key == "su":
                    plot.setLabel("top", f"Su (kPa) — Nkt {opts['nkt']:g} / {opts['nkt_high']:g}")
                for index, cpt_id in enumerate(shown):
                    data = self._channels_for_shown[cpt_id]
                    for n, channel in enumerate(keys):
                        values = data.get(channel) or []
                        if not any(v is not None for v in values):
                            continue
                        xs = [math.nan if v is None or (log and v <= 0) else float(v) for v in values]
                        if channel in ("u0", "sv0"):
                            pen = pg.mkPen((120, 120, 120), width=1.0, style=PEN_STYLE_DASH)
                        elif n:
                            pen = pg.mkPen(self._colour(index), width=1.0, style=PEN_STYLE_DASH)
                        else:
                            pen = pg.mkPen(self._colour(index), width=1.3)
                        curve = self._curve(entry, index, n)
                        curve.setPen(pen)
                        curve.setData(xs, list(data["depth"]), connect="finite")
                        curve.setVisible(True)
                        used.add((index, n))
            for slot, curve in entry["curves"].items():
                if slot not in used:
                    curve.setData([], [])
                    curve.setVisible(False)
            fields = {"su": ("su", "su_low"), "dr": ("dr", "dr_low"), "ic": ("ic",)}.get(key, ())
            for n, curve in enumerate(entry["interpreted"]):
                xs, ys = self._interpreted_steps(interpreted, fields[n] if n < len(fields) else "", deepest) \
                    if visible else ([], [])
                curve.setData(xs, ys, connect="finite")
                curve.setVisible(bool(xs))
            for region, _lo, _hi in entry["regions"]:
                region.setVisible(visible and self.show_bands)
            entry["target"].setVisible(bool(target) and visible)
            if target:
                entry["target"].setPos(target)
            if visible:
                plot.vb.enableAutoRange(x=True)
        self.target_label.setVisible(bool(target))
        if target:
            self.target_label.setText(f"Target {target:g} m")
            self.target_label.setPos(0.02, target)
        self.strata_plot.vb.setYRange(0.0, max(deepest, target or 0.0) * 1.03 + 0.02, padding=0)

    def _interpreted_steps(self, layers, name: str, deepest: float):
        """Delivered (SCPP / CPTP) values as steps."""
        xs, ys = [], []
        if not name:
            return xs, ys
        for layer in layers:
            value = layer.get(name)
            if value is None:
                continue
            top = float(layer["top"])
            base = layer.get("base")
            base = float(base) if base is not None else top
            if base <= top:
                base = min(top + 0.05, deepest)
            xs += [float(value), float(value), math.nan]
            ys += [top, base, math.nan]
        return xs, ys

    def _class_colours(self) -> Dict[str, str]:
        """Colours of the project's soil classes (Ground Model ▸ Classes…)."""
        out = {}
        for row in getattr(self.model, "ground_classes", None) or []:
            code = str(row.get("code") or "").strip()
            if code and row.get("color"):
                out[code.casefold()] = str(row["color"])
        return out

    def _draw_strata(self, deepest: float) -> None:
        cpt = self.register.by_id(self.current) or {}
        colours = self._class_colours()
        layers = []
        for layer in cpt.get("layers") or []:
            top = float(layer.get("top") or 0.0)
            base = layer.get("base")
            base = float(base) if base is not None else deepest
            if base <= top:
                continue
            code = str(layer.get("soil_class") or "")
            colour = QColor(colours.get(code.casefold()) or class_colour(code))
            layers.append((top, base, colour, code, str(layer.get("description") or "")))
        self.strata_item.set_layers(layers, deepest)
        self.strata_plot.setLabel("top", "Strata" if layers else "Strata (none)")

    def _build_chart(self) -> None:
        chart = self.chart.getPlotItem()
        chart.setLogMode(x=True, y=True)
        chart.setLabel("bottom", "Normalised friction ratio Fr (%)")
        chart.setLabel("left", "Normalised cone resistance Qtn")
        chart.showGrid(x=True, y=True, alpha=0.2)
        chart.setTitle("Robertson (2009/2010) SBTn chart — Ic boundaries")
        for ic in cpt_mod.IC_BOUNDARIES:
            xs, ys = [], []
            steps = 240
            for k in range(steps + 1):
                log_fr = -1.0 + 2.0 * k / steps
                arg = ic * ic - (log_fr + 1.22) ** 2
                if arg < 0:
                    continue
                log_q = 3.47 - math.sqrt(arg)
                if 0.0 <= log_q <= 3.0:
                    xs.append(10 ** log_fr)
                    ys.append(10 ** log_q)
            chart.plot(xs, ys, pen=pg.mkPen((110, 110, 110), width=1, style=PEN_STYLE_DASH))
        for zone, (x, y) in {7: (0.3, 400), 6: (0.6, 150), 5: (1.2, 45), 4: (2.0, 15),
                             3: (3.5, 5), 2: (7.0, 1.6)}.items():
            label = pg.TextItem(str(zone), color=cpt_mod.SBT_ZONES[zone][1], anchor=(0.5, 0.5))
            label.setToolTip(cpt_mod.SBT_ZONES[zone][0])
            label.setPos(math.log10(x), math.log10(y))
            chart.addItem(label)
        self._chart_points = []
        chart.setXRange(-1, 1, padding=0)
        chart.setYRange(0, 3, padding=0)

    def _draw_chart(self) -> None:
        chart = self.chart.getPlotItem()
        shown = self.shown()
        while len(self._chart_points) < len(shown):
            self._chart_points.append(chart.plot([], [], pen=None, symbol="o", symbolSize=4, symbolPen=None))
        for index, item in enumerate(self._chart_points):
            points = []
            if index < len(shown):
                data = self._channels_for_shown[shown[index]]
                points = [(fr, q) for fr, q in zip(data.get("fr") or [], data.get("qtn") or [])
                          if fr and q and fr > 0 and q > 0]
            item.setSymbolBrush(pg.mkBrush(QColor(self._colour(index))))
            item.setData([p[0] for p in points], [p[1] for p in points])
            item.setVisible(bool(points))

    def table_rows(self) -> Tuple[List[str], List[List]]:
        keys = ["qc", "fs", "u1", "u2", "u3", "incl", "qt", "rf", "u0", "sv0", "sv0_eff", "qnet",
                "bq", "fr", "qtn", "ic", "zone", "su", "su_low", "dr", "gamma"]
        shown = self.shown()
        present = [k for k in keys if any(any(v is not None for v in (self._channels_for_shown[i].get(k) or []))
                                          for i in shown)]
        headers = ["CPT", "Depth (m)"] + [cpt_mod.channel_label(k) for k in present]
        rows = []
        for cpt_id in shown:
            name = (self.register.by_id(cpt_id) or {}).get("name") or cpt_id
            data = self._channels_for_shown[cpt_id]
            for i, depth in enumerate(data["depth"]):
                row = [name, f"{depth:g}"]
                for key in present:
                    series = data.get(key) or []
                    value = series[i] if i < len(series) else None
                    row.append("" if value is None else (str(value) if key == "zone" else f"{value:.5g}"))
                rows.append(row)
        return headers, rows

    def _fill_table(self) -> None:
        headers, rows = self.table_rows()
        self.table.setModel(RowsModel(headers, rows, self.table))

    # -- interaction ------------------------------------------------------------------
    def _mouse_moved(self, pos) -> None:
        depth = None
        for plot in self.plots:
            if plot.isVisible() and plot.sceneBoundingRect().contains(pos):
                depth = plot.vb.mapSceneToView(pos).y()
                break
        if depth is None:
            for line in self.hlines:
                line.setVisible(False)
            return
        for line in self.hlines:
            line.setPos(depth)
            line.setVisible(True)
        self.readout.setText(self.readout_text(depth))

    def readout_text(self, depth: float) -> str:
        lines = [f"<b>{max(depth, 0.0):.2f} m</b> below seabed"]
        layer = self.strata_item.layer_at(depth)
        if layer is not None:
            lines[0] += (f" — stratum {_esc(layer[3] or '')} {layer[0]:g}–{layer[1]:g} m: "
                         f"{_esc(layer[4])}").rstrip(": ")
        for index, cpt_id in enumerate(self.shown()):
            data = self._channels_for_shown.get(cpt_id) or {}
            depths = data.get("depth") or []
            if not depths or depth < depths[0] - 0.05 or depth > depths[-1] + 0.05:
                continue
            i = min(range(len(depths)), key=lambda k: abs(depths[k] - depth))
            parts = []
            for key in ("qc", "qt", "fs", "u", "rf", "ic", "su", "su_low", "dr"):
                series = data.get(key) or []
                value = series[i] if i < len(series) else None
                if value is not None:
                    parts.append(f"{cpt_mod.channel_symbol(key)} {value:.3g} {cpt_mod.channel_unit(key)}".rstrip())
            su = (data.get("su") or [None] * len(depths))[i]
            term = next((label for lo, hi, label in cpt_mod.SU_TERMS
                         if su is not None and su >= lo and (hi is None or su < hi)), "")
            if term:
                parts.append(f"{term.lower()} strength")
            zone = (data.get("zone") or [None] * len(depths))[i]
            if zone in cpt_mod.SBT_ZONES:
                parts.append(f"SBTn {zone} ({cpt_mod.SBT_ZONES[zone][0]})")
            name = (self.register.by_id(cpt_id) or {}).get("name") or ""
            lines.append(f"<span style='color:{self._colour(index)}'>■ {_esc(name)} @ {depths[i]:.2f} m</span>: "
                         + _esc(", ".join(parts) or "no readings"))
        return "<br>".join(lines)

    def fit(self) -> None:
        if not self.plots:
            return
        deepest = max((max(self._channels_for_shown[i]["depth"] or [0]) for i in self.shown()), default=1.0)
        self.plots[0].vb.setYRange(0.0, deepest * 1.03 + 0.02, padding=0)
        for plot in self.plots[1:]:
            plot.vb.enableAutoRange(x=True)

    def _chosen(self, *_args) -> None:
        cpt_id = self.choose.currentData()
        if cpt_id and cpt_id != self.current:
            self.current = cpt_id
            self.compare = [i for i in self.compare if i != cpt_id]
            self.redraw()
            self.cptChosen.emit(cpt_id)

    def _step(self, direction: int) -> None:
        if not self.order or self.current not in self.order:
            return
        index = self.order.index(self.current) + direction
        if 0 <= index < len(self.order):
            self.show_cpts([self.order[index]] + self.compare)

    def _sync_nav(self) -> None:
        index = self.order.index(self.current) if self.current in self.order else -1
        self.prev_button.setEnabled(index > 0)
        self.next_button.setEnabled(0 <= index < len(self.order) - 1)

    def _build_compare_menu(self) -> None:
        self.compare_menu.clear()
        clear = self.compare_menu.addAction("Show only the selected CPT")
        clear.triggered.connect(lambda: self.show_cpts([self.current]))
        self.compare_menu.addSeparator()
        for cpt_id in self.order:
            if cpt_id == self.current:
                continue
            cpt = self.register.by_id(cpt_id) or {}
            action = self.compare_menu.addAction(cpt.get("name") or cpt_id)
            action.setCheckable(True)
            action.setChecked(cpt_id in self.compare)
            action.setEnabled(cpt_id in self.compare or len(self.compare) < _MAX_COMPARE)
            action.toggled.connect(lambda checked, i=cpt_id: self._toggle_compare(i, checked))

    def _toggle_compare(self, cpt_id: str, checked: bool) -> None:
        if checked and cpt_id not in self.compare:
            self.compare.append(cpt_id)
        elif not checked and cpt_id in self.compare:
            self.compare.remove(cpt_id)
        self.redraw()

    def _build_tracks_menu(self) -> None:
        self.tracks_menu.clear()
        for key, title, _keys, _log, _default in TRACKS:
            action = self.tracks_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(key in self.visible_tracks)
            action.toggled.connect(lambda checked, k=key: self.set_track_visible(k, checked))
        self.tracks_menu.addSeparator()
        bands = self.tracks_menu.addAction("Strength / density terms behind Su and qc")
        bands.setCheckable(True)
        bands.setChecked(self.show_bands)
        bands.setToolTip("Su: BS 5930 / ISO 14688-2 strength terms (extremely low < 10 kPa … ). qc: "
                         "indicative density terms (very loose < 2.5 MPa …). Display only.")
        bands.toggled.connect(self.set_bands_visible)
        logs = self.tracks_menu.addMenu("Logarithmic scale")
        for key, title, _keys, _log, _default in TRACKS:
            action = logs.addAction(title)
            action.setCheckable(True)
            action.setChecked(key in self.log_tracks)
            action.toggled.connect(lambda checked, k=key: self.set_track_log(k, checked))

    def set_track_visible(self, key: str, visible: bool, persist: bool = True) -> None:
        tracks = [k for k in _TRACK_KEYS if (k in self.visible_tracks and k != key) or (k == key and visible)]
        self.visible_tracks = tracks
        if persist:
            QSettings().setValue(f"{_SETTINGS}/tracks", tracks)
        self.redraw()

    def set_bands_visible(self, visible: bool, persist: bool = True) -> None:
        self.show_bands = bool(visible)
        if persist:
            QSettings().setValue(f"{_SETTINGS}/bands", self.show_bands)
        self.redraw()

    def set_track_log(self, key: str, log: bool, persist: bool = True) -> None:
        if log:
            self.log_tracks.add(key)
        else:
            self.log_tracks.discard(key)
        if persist:
            QSettings().setValue(f"{_SETTINGS}/log_tracks", sorted(self.log_tracks))
        self.redraw()

    # -- export -------------------------------------------------------------------------
    def export_csv(self, path: Optional[str] = None) -> str:
        if not self.shown():
            return ""
        if path is None:
            name = (self.register.by_id(self.current) or {}).get("name") or "cpt"
            path, _ = QFileDialog.getSaveFileName(self, "Export CPT readings",
                                                  re.sub(r'[\\/:*?"<>|]', "_", f"{name} readings.csv"),
                                                  "CSV (*.csv)")
        if not path:
            return ""
        headers, rows = self.table_rows()
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                writer.writerows(rows)
        except OSError as exc:
            QMessageBox.warning(self, "Export CPT readings", str(exc))
            return ""
        return path

    def save_image(self, path: Optional[str] = None) -> str:
        if path is None:
            name = (self.register.by_id(self.current) or {}).get("name") or "cpt"
            path, _ = QFileDialog.getSaveFileName(self, "Save CPT log image",
                                                  re.sub(r'[\\/:*?"<>|]', "_", f"{name} log.png"),
                                                  "PNG image (*.png)")
        if not path:
            return ""
        widget = self.tabs.currentWidget() or self.log_page
        if widget.grab().save(path):
            return path
        QMessageBox.warning(self, "Save CPT log image", f"The image could not be written to {path}.")
        return ""
