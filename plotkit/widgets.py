# -*- coding: utf-8 -*-
"""pyqtgraph pieces that draw alike under QGIS 3 (Qt5) and QGIS 4 (Qt6).

Under Qt5 with Windows display scaling other than 100 %, pyqtgraph's
QPicture-recorded items replay at the wrong size (axes shrink to two thirds
of the plot, bar items garble) and moved lines can leave trails. These
helpers avoid both: :class:`DirectAxis` paints straight to the screen,
:func:`make_plot_widget` repaints the whole view on Qt5, and filled
stretches are single :class:`QGraphicsPathItem` paths (:func:`level_paths`,
:func:`runs_path`). Legends are rich-text labels above a plot
(:func:`legend_html`, :class:`WrapLabel`), sized in pixels.
"""

from __future__ import annotations

from typing import Dict, Iterable, Sequence, Tuple

import numpy as np
import pyqtgraph as pg

from qgis.PyQt.QtCore import QT_VERSION_STR, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QPainterPath
from qgis.PyQt.QtWidgets import QGraphicsView, QLabel, QMenu, QToolButton

from ..plugin_log import log_exception
from ..qgis_compat import TOOLBUTTON_POPUP_MODE_INSTANT

_RIGHT_BUTTON = getattr(getattr(Qt, "MouseButton", Qt), "RightButton")
_FULL_UPDATE = getattr(getattr(QGraphicsView, "ViewportUpdateMode", QGraphicsView), "FullViewportUpdate")

GLYPH = {"solid": "━━", "dash": "╍╍", "dot": "┈┈", "fill": "■",
         "triangle": "▲", "diamond": "◆"}


def hex_colour(colour) -> str:
    if isinstance(colour, str):
        return colour
    return "#%02x%02x%02x" % tuple(int(c) for c in colour[:3])


def legend_html(entries: Iterable[Tuple[str, object, str]]) -> str:
    """Legend text from ``(glyph kind, colour, label)`` entries (see :data:`GLYPH`)."""
    parts = []
    for kind, colour, text in entries:
        glyph = GLYPH[kind]
        parts.append(f"<span style='color:{hex_colour(colour)}; font-weight:bold'>{glyph}</span>&nbsp;{text}")
    return "&nbsp;&nbsp;&nbsp;".join(parts)


def runs_path(kp: np.ndarray, top: np.ndarray, bottom: np.ndarray, extents: Sequence[Tuple[int, int]]) -> QPainterPath:
    """One path with a closed polygon per ``(i0, i1)`` run (``top`` above, ``bottom`` below)."""
    xs, ys, connect = [], [], []
    for i0, i1 in extents:
        sl = slice(i0, i1 + 1)
        k, t, b = kp[sl], top[sl], bottom[sl]
        ok = np.isfinite(k) & np.isfinite(t) & np.isfinite(b)
        if ok.sum() < 2:
            continue
        k, t, b = k[ok], t[ok], b[ok]
        xs.append(np.concatenate((k, k[::-1])))
        ys.append(np.concatenate((t, b[::-1])))
        link = np.ones(2 * len(k), dtype=bool)
        link[-1] = False
        connect.append(link)
    if not xs:
        return QPainterPath()
    return pg.arrayToQPath(np.concatenate(xs), np.concatenate(ys), connect=np.concatenate(connect))


def merge_runs(lo, hi, level) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stretches ``lo..hi`` at ``level`` (in KP order) with touching neighbours of one level merged."""
    lo, hi, level = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float), np.asarray(level, dtype=int)
    if not lo.size:
        return lo, hi, level
    new = np.r_[True, (level[1:] != level[:-1]) | (lo[1:] > hi[:-1] + 1e-12)]
    starts = np.flatnonzero(new)
    ends = np.r_[starts[1:], lo.size] - 1
    return lo[starts], hi[ends], level[starts]


def level_paths(lo, hi, level, levels: Iterable[int], y0: float = 0.0, y1: float = 1.0) -> Dict[int, QPainterPath]:
    """One path per level: a rectangle ``lo..hi`` x ``y0..y1`` per merged stretch (see :func:`merge_runs`)."""
    paths = {int(key): QPainterPath() for key in levels}
    for a, b, key in zip(*merge_runs(lo, hi, level)):
        if int(key) in paths:
            paths[int(key)].addRect(QRectF(float(a), y0, float(b - a), y1 - y0))
    return paths


class DirectAxis(pg.AxisItem):
    """An axis that paints straight to the screen.

    pyqtgraph records an axis (ticks, labels, grid lines) in a QPicture and
    replays it. Under Qt5 (QGIS 3) with display scaling a QPicture records at
    the screen's logical DPI (e.g. 144 at 150 %) while the plot paints at 96, so
    the replay shrank the axis and its grid to two thirds of the plot.
    """

    def paint(self, p, opt, widget):
        p.save()
        try:
            if self.style["tickFont"]:
                p.setFont(self.style["tickFont"])
            specs = self.generateDrawSpecs(p)
            if specs is not None:
                self.drawPicture(p, *specs)
        finally:
            p.restore()


class WrapLabel(QLabel):
    """A word-wrapped label that keeps the height its wrapped text needs.

    Layouts give a wrapping label one line's height as its minimum, so in a
    smaller window a legend wrapped to two lines pushed its plot down and the
    row cut the bottom of the plot off (the profile ran out of view).
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        need = self.heightForWidth(self.width())
        if need > 0 and need != self.minimumHeight():
            self.setMinimumHeight(need)


class ProfileViewBox(pg.ViewBox):
    """A view box that says when a right-button drag starts, with extra right-click actions.

    A right-button drag (in the plot or on an axis) stretches the axes
    independently; a profile uses it to leave true scale.
    """

    stretchStarted = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ``extra_menu(kp, scene_pos)`` -> [(text, slot or None, enabled)]: actions put at the top of the
        # right-click menu (None: a separator).
        self.extra_menu = None
        self._extra_actions = []

    def mouseDragEvent(self, ev, axis=None):  # noqa: N802 (Qt API)
        if ev.isStart() and ev.button() == _RIGHT_BUTTON:
            self.stretchStarted.emit()
        super().mouseDragEvent(ev, axis)

    def raiseContextMenu(self, ev):  # noqa: N802 (pyqtgraph API)
        menu = self.menu
        for action in self._extra_actions:
            menu.removeAction(action)
        self._extra_actions = []
        if self.extra_menu is not None and menu is not None:
            try:
                kp = float(self.mapSceneToView(ev.scenePos()).x())
                items = self.extra_menu(kp, ev.scenePos())
            except Exception:
                log_exception("Building the profile's right-click menu failed")
                items = []
            first = menu.actions()[0] if menu.actions() else None
            for text, slot, enabled in items + ([(None, None, True)] if items else []):
                if text is None:
                    action = menu.addSeparator()
                else:
                    action = menu.addAction(text)
                    action.setEnabled(bool(enabled))
                    if slot is not None:
                        action.triggered.connect(lambda _checked=False, s=slot: s())
                if first is not None:
                    menu.insertAction(first, action)   # moves it to the top
                self._extra_actions.append(action)
        super().raiseContextMenu(ev)


def make_plot_widget() -> "pg.PlotWidget":
    """A plot widget with :class:`ProfileViewBox` and :class:`DirectAxis` axes."""
    widget = pg.PlotWidget(viewBox=ProfileViewBox(),
                           axisItems={"left": DirectAxis("left"), "bottom": DirectAxis("bottom")})
    if not QT_VERSION_STR.startswith("5."):
        return widget
    # Under Qt5 with fractional display scaling (e.g. 150 %), repainting only the
    # changed strip rounds to whole device pixels and can miss the edge of a moved
    # or hidden line: the hover crosshair left trails until the next pan. Repaint
    # the whole view instead (as panning does).
    widget.setViewportUpdateMode(_FULL_UPDATE)
    return widget


class StayOpenMenu(QMenu):
    """A menu that stays open while its check boxes are ticked (several in a row)."""

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        action = self.activeAction()
        if action is not None and action.isEnabled() and action.isCheckable():
            action.trigger()
            return
        super().mouseReleaseEvent(event)


def action_menu(entries, parent=None) -> QMenu:
    """A menu from ``(label, slot)`` pairs (``None`` for a separator)."""
    menu = QMenu(parent)
    for entry in entries:
        if entry is None:
            menu.addSeparator()
        else:
            label, slot = entry
            menu.addAction(label).triggered.connect(lambda _checked=False, s=slot: s())
    return menu


def entries_button(text: str, tip: str, entries) -> QToolButton:
    """:func:`menu_button` over :func:`action_menu` entries; the button owns its menu."""
    menu = action_menu(entries)
    button = menu_button(text, tip, menu)
    menu.setParent(button, menu.windowFlags())
    return button


def menu_button(text: str, tip: str, menu: QMenu) -> QToolButton:
    """A button that opens ``menu`` on a click."""
    button = QToolButton()
    if QT_VERSION_STR.startswith("5."):
        # Qt5's Windows style draws the menu arrow over the end of the text
        # ("Plots" read as "Plots,"): put a text arrow after it instead.
        button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        text += " ▾"
    button.setText(text)
    if tip:
        button.setToolTip(tip)
    button.setPopupMode(TOOLBUTTON_POPUP_MODE_INSTANT)
    button.setMenu(menu)
    return button
