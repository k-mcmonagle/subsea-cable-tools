# -*- coding: utf-8 -*-
"""Rows of plots on one KP axis, in a splitter: the shared shape of a KP profile.

* An optional **status bar** on top: a stretch per level (e.g. meets target,
  below target, no data), one filled path per level. Hover outlines the
  stretch; a click emits :attr:`KpRowStack.statusClicked`.
* **Data rows**, each a plot with a legend line above it, all linked to the
  first row's KP axis; the KP labels sit on the lowest visible row. Rows
  share the height by dragging the splitter handles.
* A **crosshair** on every row follows the cursor; :attr:`tip_provider` and
  :attr:`readout_provider` give the tooltip beside the cursor and the line
  below the plots for a KP. A click emits :attr:`KpRowStack.kpClicked`.
* **Fit** and the *A* button show the whole extent; **Plots** shows or hides
  each row and the legends, reverses KP, and restores the default view.

Items are built once and updated by the caller (``setData`` / ``setPath``):
removing pyqtgraph items is slow on long routes and noisy under QGIS 3.40.
Axes paint directly and the status bar is paths, so both draw correctly
under Qt5 display scaling (see :mod:`.widgets`). Splitter sizes, row
visibility, legends and reverse KP are remembered under
``SubseaCableTools/<settings_key>`` unless ``persist`` is False (tests).
"""

from __future__ import annotations

import html
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import QEvent, QRectF, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtGui import QPainterPath
from qgis.PyQt.QtWidgets import QGraphicsPathItem, QHBoxLayout, QLabel, QPushButton, QSplitter, QVBoxLayout, QWidget

from .widgets import StayOpenMenu, WrapLabel, level_paths, make_plot_widget, menu_button, merge_runs

_VERTICAL = getattr(getattr(Qt, "Orientation", Qt), "Vertical")
_POINTING = getattr(getattr(Qt, "CursorShape", Qt), "PointingHandCursor")
_NO_MOUSE = getattr(getattr(Qt, "WidgetAttribute", Qt), "WA_TransparentForMouseEvents")
_RICH = getattr(getattr(Qt, "TextFormat", Qt), "RichText")
_LEFT_BUTTON = getattr(getattr(Qt, "MouseButton", Qt), "LeftButton")
_SELECTABLE = getattr(getattr(Qt, "TextInteractionFlag", Qt), "TextSelectableByMouse")
_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
_LEAVE = getattr(getattr(QEvent, "Type", QEvent), "Leave")
STATUS_HEIGHT = 14
AXIS_WIDTH = 64
_SMALL = {"font-size": "11px"}   # pixels: point sizes scale with the screen DPI under Qt5
_TIP_STYLE = ("QLabel { background: rgba(255, 255, 255, 240); color: #222; border: 1px solid #9e9e9e; "
              "padding: 3px 5px; font-size: 11px; }")
_CAPTION_STYLE = (f"QLabel {{ background: white; color: #333; font-size: 11px; "
                  f"padding: 1px 4px 0px {AXIS_WIDTH + 4}px; }}")


class KpRowStack(QWidget):
    """Plots on one KP axis (see the module notes)."""

    kpHovered = pyqtSignal(float)                    # nan: the cursor left the plots
    kpClicked = pyqtSignal(float)
    statusClicked = pyqtSignal(float, float, float)  # the stretch (KP from, KP to) and the KP clicked

    def __init__(self, settings_key: str, levels: Sequence[Tuple[int, Tuple[int, int, int], str]] = (),
                 parent=None, persist: bool = True):
        super().__init__(parent)
        self.settings_root = f"SubseaCableTools/{settings_key}"
        self.persist = persist
        self.tip_provider: Optional[Callable[[float], str]] = None
        self.readout_provider: Optional[Callable[[float], str]] = None
        self.status_text: Optional[Callable[[float, float, int, float], str]] = None
        self.level_names = {int(key): name for key, _colour, name in levels}
        self._extent: Optional[Tuple[float, float]] = None
        self._status_runs: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]] = None
        self._default_sizes: List[int] = []
        self.widgets: Dict[str, pg.PlotWidget] = {}
        self.plots: Dict[str, pg.PlotItem] = {}
        self.rows: Dict[str, QWidget] = {}
        self.captions: Dict[str, WrapLabel] = {}
        self.row_actions: Dict[str, object] = {}
        self._vlines: List[pg.InfiniteLine] = []
        self._anchor: Optional[pg.PlotItem] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar = QHBoxLayout()
        fit = QPushButton("Fit")
        fit.setToolTip("Show the whole extent (as the A at the bottom left of a plot does).")
        fit.clicked.connect(self.fit)
        self.toolbar.addWidget(fit)
        self.plots_menu = StayOpenMenu(self)
        self.plots_menu.setToolTipsVisible(True)
        self._rows_end = self.plots_menu.addSeparator()
        self.legend_action = self._toggle(self.plots_menu, "Legends", "The key above each plot.", "legends", True)
        self.reverse_action = self._toggle(self.plots_menu, "Reverse KP", "KP increasing from right to left (e.g. "
                                           "to match the installation direction).", "reverse_kp", False)
        self.plots_menu.addSeparator()
        self.plots_menu.addAction("Default view", self.default_view).setToolTip(
            "Every plot shown at its default height, and the whole extent.")
        self.toolbar.addWidget(menu_button("Plots", "Show or hide each plot, and reverse KP.", self.plots_menu))
        self.toolbar.addStretch(1)
        layout.addLayout(self.toolbar)

        self.splitter = QSplitter(_VERTICAL)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)
        self.readout = WrapLabel("")
        self.readout.setTextInteractionFlags(_SELECTABLE)
        layout.addWidget(self.readout)
        self.tip = QLabel(self)
        self.tip.setTextFormat(_RICH)
        self.tip.setAttribute(_NO_MOUSE)
        self.tip.setStyleSheet(_TIP_STYLE)
        self.tip.hide()

        self.status_plot: Optional[pg.PlotItem] = None
        self.status_paths: Dict[int, QGraphicsPathItem] = {}
        if levels:
            self._add_status_row(levels)
        self.legend_action.toggled.connect(self._update_legends)
        self.reverse_action.toggled.connect(self._apply_reverse)
        self.splitter.splitterMoved.connect(lambda *_: self._remember("splitter", None))

    # -- building ---------------------------------------------------------------
    def _add_status_row(self, levels) -> None:
        widget = make_plot_widget()
        widget.setBackground("w")
        widget.setAntialiasing(True)
        plot = widget.getPlotItem()
        axis = plot.getAxis("left")
        axis.setWidth(AXIS_WIDTH)
        axis.setStyle(showValues=False)
        axis.setPen(pg.mkPen(None))
        axis.setTicks([[], []])
        plot.hideAxis("bottom")
        plot.setMouseEnabled(x=True, y=False)
        plot.setYRange(0, 1, padding=0)
        plot.hideButtons()
        for key, colour, _name in levels:
            item = QGraphicsPathItem()
            item.setBrush(pg.mkBrush(*colour))
            item.setPen(pg.mkPen(None))
            plot.addItem(item)
            self.status_paths[int(key)] = item
        self.status_hover = QGraphicsPathItem()
        self.status_hover.setPen(pg.mkPen((20, 20, 20), width=1.5))
        self.status_hover.setZValue(10)
        plot.addItem(self.status_hover, ignoreBounds=True)
        line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen((20, 20, 20), width=1.5))
        line.hide()
        plot.addItem(line, ignoreBounds=True)
        self._vlines.append(line)
        widget.viewport().setCursor(_POINTING)
        widget.setFixedHeight(STATUS_HEIGHT)
        widget.scene().sigMouseMoved.connect(self._on_status_moved)
        widget.scene().sigMouseClicked.connect(self._on_status_clicked)
        widget.installEventFilter(self)
        self.widgets["status"] = widget
        self.status_plot = plot
        self.rows["status"] = widget
        self.splitter.addWidget(widget)
        self.splitter.setStretchFactor(self.splitter.count() - 1, 0)
        self._default_sizes.append(STATUS_HEIGHT)

    def add_row(self, key: str, title: str, tip: str = "", y_label: str = "", invert_y: bool = False,
                height: int = 150, min_height: int = 60, stretch: int = 2, legend: str = "") -> pg.PlotItem:
        """A data row (shown in *Plots* as ``title``); returns its plot for the caller's items."""
        if key in self.plots or key == "status":
            raise ValueError(f"Row '{key}' already exists")
        widget = make_plot_widget()
        widget.setBackground("w")
        widget.setAntialiasing(True)
        widget.setMinimumHeight(min_height)
        plot = widget.getPlotItem()
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.getAxis("left").setWidth(AXIS_WIDTH)
        plot.setAutoVisible(y=True)
        plot.invertY(bool(invert_y))
        if y_label:
            plot.setLabel("left", y_label, **_SMALL)
        try:
            plot.autoBtn.clicked.disconnect(plot.autoBtnClicked)
        except (TypeError, RuntimeError):
            pass
        plot.autoBtn.clicked.connect(lambda *_: self.fit())
        plot.autoBtn.setToolTip("Show the whole extent")
        if self._anchor is None:
            self._anchor = plot
            if self.status_plot is not None:
                self.status_plot.setXLink(plot)
        else:
            plot.setXLink(self._anchor)
        plot.invertX(self.reverse_action.isChecked())
        line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen((120, 120, 120), width=1, style=_DASH))
        line.hide()
        plot.addItem(line, ignoreBounds=True)
        self._vlines.append(line)

        row = QWidget()
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(0)
        caption = WrapLabel(legend)
        caption.setStyleSheet(_CAPTION_STYLE)
        caption.setVisible(bool(legend) and self.legend_action.isChecked())
        row_layout.addWidget(caption)
        row_layout.addWidget(widget, 1)
        self.captions[key] = caption
        self.widgets[key] = widget
        self.plots[key] = plot
        self.rows[key] = row
        self.splitter.addWidget(row)
        self.splitter.setStretchFactor(self.splitter.count() - 1, stretch)
        self._default_sizes.append(height)
        self.splitter.setSizes(list(self._default_sizes))

        action = self.plots_menu.addAction(title)
        self.plots_menu.insertAction(self._rows_end, action)
        action.setCheckable(True)
        action.setToolTip(tip)
        action.setChecked(self._setting(f"row_{key}", True))
        action.toggled.connect(lambda on, k=key: self._remember(f"row_{k}", on))
        action.toggled.connect(self._update_rows)
        self.row_actions[key] = action

        scene = widget.scene()
        scene.sigMouseMoved.connect(lambda pos, k=key: self._on_mouse_moved(k, pos))
        scene.sigMouseClicked.connect(lambda event, k=key: self._on_mouse_clicked(k, event))
        widget.installEventFilter(self)
        self._update_rows()
        return plot

    def restore_layout(self) -> None:
        """Bring back the remembered splitter sizes (call once every row is added)."""
        try:
            state = QSettings().value(f"{self.settings_root}/splitter")
            if state is not None:
                self.splitter.restoreState(state)
        except Exception:
            pass

    def curve(self, key: str, colour, width: float = 1.5, style=None, fast: bool = True,
              name: Optional[str] = None) -> pg.PlotDataItem:
        """An empty line on row ``key``; long lines thin to one min/max pair per pixel when ``fast``."""
        pen = pg.mkPen(colour, width=width) if style is None else pg.mkPen(colour, width=width, style=style)
        item = pg.PlotDataItem([], [], pen=pen, name=name, connect="finite", antialias=True,
                               autoDownsampleFactor=1.0)
        self.plots[key].addItem(item)
        if fast:
            # Downsampling and clip-to-view only once the item is in its plot (pyqtgraph noise otherwise).
            item.setDownsampling(auto=True, method="peak")
            item.setClipToView(True)
        return item

    def path_item(self, key: str, brush, pen=None, z: float = 0.0) -> QGraphicsPathItem:
        """An empty filled path on row ``key`` (kept out of the auto range)."""
        item = QGraphicsPathItem()
        item.setBrush(pg.mkBrush(*brush) if isinstance(brush, tuple) else pg.mkBrush(brush))
        item.setPen(pg.mkPen(None) if pen is None else pen)
        item.setZValue(z)
        self.plots[key].addItem(item, ignoreBounds=True)
        return item

    def set_legend(self, key: str, text: str) -> None:
        caption = self.captions[key]
        caption.setText(text)
        caption.setVisible(bool(text) and self.legend_action.isChecked())

    # -- settings -------------------------------------------------------------------
    def _setting(self, key: str, default: bool) -> bool:
        try:
            return bool(QSettings().value(f"{self.settings_root}/{key}", default, type=bool))
        except Exception:
            return default

    def _toggle(self, menu, text: str, tip: str, key: str, default: bool):
        action = menu.addAction(text)
        action.setCheckable(True)
        action.setToolTip(tip)
        action.setChecked(self._setting(key, default))
        action.toggled.connect(lambda on, k=key: self._remember(k, on))
        return action

    def _remember(self, key: str, value) -> None:
        if not self.persist:
            return
        try:
            if key == "splitter":
                QSettings().setValue(f"{self.settings_root}/splitter", self.splitter.saveState())
            else:
                QSettings().setValue(f"{self.settings_root}/{key}", bool(value))
        except Exception:
            pass

    # -- rows and view ----------------------------------------------------------------
    def visible_rows(self) -> List[str]:
        return [key for key, action in self.row_actions.items() if action.isChecked()]

    def _update_rows(self, *_args) -> None:
        if self.row_actions and not self.visible_rows():
            next(iter(self.row_actions.values())).setChecked(True)   # at least one plot stays
            return
        for key, action in self.row_actions.items():
            self.rows[key].setVisible(action.isChecked())
        visible = self.visible_rows()
        lowest = visible[-1] if visible else None
        for key, plot in self.plots.items():
            show = key == lowest
            plot.showAxis("bottom", show)
            if show:
                plot.setLabel("bottom", "KP (km)", **_SMALL)

    def _update_legends(self, *_args) -> None:
        for caption in self.captions.values():
            caption.setVisible(bool(caption.text()) and self.legend_action.isChecked())

    def _apply_reverse(self, on: bool) -> None:
        for plot in list(self.plots.values()) + ([self.status_plot] if self.status_plot is not None else []):
            plot.invertX(bool(on))

    def default_view(self) -> None:
        for action in self.row_actions.values():
            action.setChecked(True)
        self.splitter.setSizes(list(self._default_sizes))
        self._remember("splitter", None)
        self.fit()

    def set_extent(self, lo_km: Optional[float], hi_km: Optional[float]) -> None:
        """The KP extent *Fit* shows (None: nothing to show)."""
        if lo_km is None or hi_km is None or not (math.isfinite(lo_km) and math.isfinite(hi_km)):
            self._extent = None
            return
        lo, hi = sorted((float(lo_km), float(hi_km)))
        self._extent = (lo, hi if hi > lo else lo + 0.001)

    def fit(self) -> None:
        if self._anchor is None or self._extent is None:
            return
        self._anchor.setXRange(*self._extent, padding=0.01)
        for plot in self.plots.values():
            plot.enableAutoRange(axis="y")

    def zoom_to(self, kp_a: float, kp_b: float) -> None:
        if self._anchor is None:
            return
        lo, hi = sorted((kp_a, kp_b))
        pad = max((hi - lo) * 0.75, 0.02)
        self._anchor.setXRange(lo - pad, hi + pad, padding=0)
        for plot in self.plots.values():
            plot.enableAutoRange(axis="y")

    def visible_kp(self) -> Tuple[float, float]:
        if self._anchor is None:
            return (0.0, 0.0)
        lo, hi = self._anchor.viewRange()[0]
        return float(lo), float(hi)

    # -- status bar -------------------------------------------------------------------
    def set_status(self, lo: np.ndarray, hi: np.ndarray, level: np.ndarray) -> None:
        """Stretches ``lo..hi`` (km) at ``level``; empty arrays clear the bar."""
        if self.status_plot is None:
            return
        lo, hi, level = merge_runs(lo, hi, level)
        paths = level_paths(lo, hi, level, self.status_paths)
        for key, item in self.status_paths.items():
            item.setPath(paths[key])
        self._status_runs = (lo, hi, level) if lo.size else None
        self.status_hover.setPath(QPainterPath())

    def status_at(self, kp: float) -> Optional[Tuple[float, float, int]]:
        """The stretch under ``kp``: (KP from, KP to, level), or None."""
        if self._status_runs is None:
            return None
        lo, hi, level = self._status_runs
        j = int(np.searchsorted(lo, kp, side="right")) - 1
        if 0 <= j < lo.size and lo[j] <= kp <= hi[j]:
            return float(lo[j]), float(hi[j]), int(level[j])
        return None

    # -- hover and clicks --------------------------------------------------------------
    def _kp_at(self, key: str, pos) -> Optional[float]:
        plot = self.status_plot if key == "status" else self.plots[key]
        if not plot.vb.sceneBoundingRect().contains(pos):
            return None
        return float(plot.vb.mapSceneToView(pos).x())

    def eventFilter(self, watched, event):  # noqa: N802 (Qt API)
        if event.type() == _LEAVE and watched in self.widgets.values():
            self.end_hover()
        return super().eventFilter(watched, event)

    def end_hover(self) -> None:
        for line in self._vlines:
            line.hide()
        self.tip.hide()
        if self.status_plot is not None:
            self.status_hover.setPath(QPainterPath())
        self.kpHovered.emit(math.nan)

    def hover(self, kp: float, key: Optional[str] = None, scene_pos=None) -> None:
        """Show the crosshair, readout and (beside ``scene_pos``) the tooltip at ``kp``."""
        for line in self._vlines:
            line.setPos(kp)
            line.show()
        if self.readout_provider is not None:
            self.readout.setText(self.readout_provider(kp))
        text = self.tip_provider(kp) if self.tip_provider is not None else ""
        if key is not None and scene_pos is not None:
            self._show_tip(key, scene_pos, text)
        self.kpHovered.emit(kp)

    def _on_mouse_moved(self, key: str, pos) -> None:
        kp = self._kp_at(key, pos)
        if kp is None:
            self.end_hover()
            return
        if self.status_plot is not None:
            self.status_hover.setPath(QPainterPath())
        self.hover(kp, key, pos)

    def _on_mouse_clicked(self, key: str, event) -> None:
        if event.isAccepted() or event.button() != _LEFT_BUTTON:
            return
        kp = self._kp_at(key, event.scenePos())
        if kp is not None:
            self.kpClicked.emit(kp)

    def _on_status_moved(self, pos) -> None:
        kp = self._kp_at("status", pos)
        stretch = self.status_at(kp) if kp is not None else None
        if stretch is None:
            self.end_hover()
            return
        lo, hi, level = stretch
        path = QPainterPath()
        path.addRect(QRectF(lo, 0.06, hi - lo, 0.88))
        self.status_hover.setPath(path)
        for line in self._vlines:
            line.setPos(kp)
            line.show()
        if self.status_text is not None:
            text = self.status_text(lo, hi, level, kp)
        else:
            text = f"KP {lo:.3f} to {hi:.3f}: {self.level_names.get(level, '')}"
        self.readout.setText(text.replace("\n", "  |  "))
        self._show_tip("status", pos, "<br>".join(html.escape(line) for line in text.split("\n")))
        self.kpHovered.emit(kp)

    def _on_status_clicked(self, event) -> None:
        if event.isAccepted() or event.button() != _LEFT_BUTTON:
            return
        kp = self._kp_at("status", event.scenePos())
        stretch = self.status_at(kp) if kp is not None else None
        if stretch is not None:
            self.statusClicked.emit(stretch[0], stretch[1], kp)

    def _show_tip(self, key: str, scene_pos, text: str) -> None:
        """The tooltip beside the cursor, kept inside the plots."""
        if not text:
            self.tip.hide()
            return
        widget = self.widgets[key]
        at = widget.viewport().mapTo(self, widget.mapFromScene(scene_pos))
        self.tip.setText(text)
        self.tip.adjustSize()
        x, y = at.x() + 16, at.y() + 16
        if x + self.tip.width() > self.width():
            x = at.x() - 12 - self.tip.width()
        if y + self.tip.height() > self.height():
            y = at.y() - 12 - self.tip.height()
        self.tip.move(max(x, 0), max(y, 0))
        self.tip.raise_()
        self.tip.show()
