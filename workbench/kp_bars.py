# -*- coding: utf-8 -*-
"""Shared KP-aligned bar widgets: overview verdict strip + per-rule fire bars.

Factored out of ``assessment_panel.py`` so the Assessment panel and the
Burial Planner draw their rule stacks with the same widgets rather than
copy-pasted painting code.

Additions over the original: the painted domain may start at a non-zero KP
(``domain_start_km``) so a scoped Burial Planner window renders correctly;
the Assessment panel keeps passing a bare ``domain_km`` (start 0).

Hover feedback: ``FireBarHover`` draws one KP line across every fire bar in
a table (the same KP on each criterion's bar) and shows a tooltip with the
hovered KP and the ranges under it; ``VerdictStrip`` does the same for the
overview bar. ``intervals_by_distance`` orders a bar's ranges nearest-first
from a clicked KP, for right-click "go to" menus.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QEvent, QObject, QPoint, QRect, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import QStyledItemDelegate, QToolTip, QWidget

from . import schema

# Colours shared by the overview bar, fire-bars and (conceptually) the layers.
STATUS_COLORS = {
    schema.STATUS_ALLOWED: QColor("#2ca02c"),
    schema.STATUS_RISK: QColor("#ff8c00"),
    schema.STATUS_EXCLUDED: QColor("#d62728"),
}
ACTION_COLORS = {
    schema.RULE_ACTION_EXCLUDE: QColor("#d62728"),
    schema.RULE_ACTION_RISK: QColor("#ff8c00"),
    schema.RULE_ACTION_ALLOW: QColor("#2ca02c"),
}
EMPTY_BG = QColor(0, 0, 0, 18)

# Horizontal inset of a fire bar inside its table cell (paint and hit-test
# must agree or the hover KP drifts from the painted ranges).
BAR_INSET_PX = 2
# Qt5 exposes QEvent members flat; Qt6 scopes them under QEvent.Type.
_EVENT_SCOPE = getattr(QEvent, "Type", QEvent)
_EV_MOUSE_MOVE = getattr(_EVENT_SCOPE, "MouseMove")
_EV_LEAVE = getattr(_EVENT_SCOPE, "Leave")
_EV_TOOLTIP = getattr(_EVENT_SCOPE, "ToolTip")
_DASH_LINE = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
HOVER_PEN = QColor(20, 20, 20, 200)
NODATA_COLOR = QColor(150, 150, 150, 110)

# Hex twins for renderer/style code that wants strings not QColors.
STATUS_COLOR_HEX = {
    schema.STATUS_ALLOWED: "#2ca02c",
    schema.STATUS_RISK: "#ff8c00",
    schema.STATUS_EXCLUDED: "#d62728",
}


def paint_spans(painter: QPainter, rect, domain_km: float,
                spans: List, radius: int = 2,
                domain_start_km: float = 0.0) -> None:
    """spans: list of (start_km, end_km, QColor); domain is
    [domain_start_km, domain_start_km + domain_km].

    Overlapping sub-pixel spans are coalesced per pixel column, so dense
    interval sets (thousands of hazards) paint O(bar width) rectangles
    instead of one fill per span. NaN/inf spans are skipped rather than
    raising inside a Qt paint event.
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.fillRect(rect, EMPTY_BG)
    if domain_km <= 0:
        painter.restore()
        return
    x0, w = rect.x(), rect.width()
    lo = domain_start_km
    hi = domain_start_km + domain_km
    y, height = rect.y() + 2, rect.height() - 4
    last_sx = last_ex = None
    last_color = None
    for start_km, end_km, color in spans:
        try:
            sx = x0 + ((max(lo, float(start_km)) - lo) / domain_km) * w
            ex = x0 + ((min(hi, float(end_km)) - lo) / domain_km) * w
        except (TypeError, ValueError):
            continue
        if sx != sx or ex != ex:  # NaN guard
            continue
        if ex - sx < 1.0:
            ex = sx + 1.0
        sx_i, ex_i = int(sx), int(ex)
        if last_color is color and last_sx is not None \
                and sx_i <= last_ex and ex_i <= last_ex:
            continue  # fully covered by the previous same-colour fill
        painter.fillRect(sx_i, y, ex_i - sx_i, height, color)
        last_sx, last_ex, last_color = sx_i, ex_i, color
    painter.restore()


def _event_point(event) -> QPoint:
    """Widget-local position of a mouse event (Qt5 ``pos`` / Qt6
    ``position``)."""
    if hasattr(event, "position"):
        return event.position().toPoint()
    return event.pos()


def _event_global(event) -> QPoint:
    if hasattr(event, "globalPosition"):
        return event.globalPosition().toPoint()
    return event.globalPos()


def kp_at_x(rect, x: float, domain_km: float,
            domain_start_km: float = 0.0) -> Optional[float]:
    """KP under pixel ``x`` of a bar painted into ``rect`` (None outside)."""
    width = float(rect.width())
    if domain_km <= 0 or width <= 0:
        return None
    frac = (float(x) - float(rect.x())) / width
    if frac < -1e-9 or frac > 1.0 + 1e-9:
        return None
    frac = min(1.0, max(0.0, frac))
    return domain_start_km + frac * domain_km


def x_at_kp(rect, kp: float, domain_km: float,
            domain_start_km: float = 0.0) -> Optional[int]:
    if domain_km <= 0:
        return None
    frac = (float(kp) - domain_start_km) / domain_km
    if frac < 0.0 or frac > 1.0:
        return None
    return int(round(rect.x() + frac * rect.width()))


def intervals_by_distance(intervals: Sequence[Tuple[float, float]],
                          kp: float) -> List[Tuple[float, float, float]]:
    """``(distance_km, start, end)`` nearest-first; 0 when ``kp`` is inside.

    Ties (e.g. several ranges containing the KP) keep KP order.
    """
    out = []
    for index, (start, end) in enumerate(intervals or []):
        try:
            lo, hi = sorted((float(start), float(end)))
        except (TypeError, ValueError):
            continue
        if lo <= kp <= hi:
            distance = 0.0
        else:
            distance = min(abs(kp - lo), abs(kp - hi))
        out.append((distance, index, lo, hi))
    out.sort(key=lambda item: (item[0], item[1]))
    return [(distance, lo, hi) for distance, _i, lo, hi in out]


def intervals_at(intervals: Sequence[Tuple[float, float]], kp: float,
                 tolerance_km: float = 0.0) -> List[Tuple[float, float]]:
    """Ranges containing ``kp`` (± a tolerance, e.g. one pixel in km)."""
    hits = []
    for start, end in intervals or []:
        try:
            lo, hi = sorted((float(start), float(end)))
        except (TypeError, ValueError):
            continue
        if lo - tolerance_km <= kp <= hi + tolerance_km:
            hits.append((lo, hi))
    return hits


def _paint_hover_line(painter: QPainter, rect, x: Optional[int]) -> None:
    if x is None:
        return
    painter.save()
    painter.setPen(QPen(HOVER_PEN, 1))
    painter.drawLine(x, rect.top(), x, rect.bottom())
    painter.restore()


class VerdictStrip(QWidget):
    """Overview bar: paints the combined verdict spans for one method.

    Hovering shows a KP line and a tooltip with the KP and the span(s)
    under the cursor; spans may carry a 4th element (a label) for it.
    """

    kpClicked = pyqtSignal(float)
    kpHovered = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(30)
        self.setMouseTracking(True)
        self._domain_km = 0.0
        self._domain_start_km = 0.0
        self._spans: List = []
        self._method_name = ""
        self._hover_x: Optional[int] = None
        self._base_tip = ("Combined suitability for the selected method. "
                          "Click to locate on the map.")
        self.setToolTip(self._base_tip)

    def set_spans(self, domain_km: float, spans: List, method_name: str = "",
                  domain_start_km: float = 0.0) -> None:
        if (domain_km == self._domain_km
                and domain_start_km == self._domain_start_km
                and spans == self._spans
                and (method_name or "") == self._method_name):
            return  # unchanged — skip the repaint
        self._domain_km = domain_km
        self._domain_start_km = domain_start_km
        self._spans = spans
        self._method_name = method_name or ""
        self.update()

    def _kp_at(self, x: int) -> Optional[float]:
        return kp_at_x(self.rect(), x, self._domain_km, self._domain_start_km)

    def hover_text(self, kp: float) -> str:
        px_km = self._domain_km / max(1, self.width())
        lines = [f"KP {kp:.3f}"]
        for span in self._spans:
            if len(span) < 2:
                continue
            try:
                lo, hi = sorted((float(span[0]), float(span[1])))
            except (TypeError, ValueError):
                continue
            if lo - px_km <= kp <= hi + px_km:
                label = str(span[3]) if len(span) > 3 and span[3] else "range"
                lines.append(f"{label}: KP {lo:.3f}–{hi:.3f} "
                             f"({hi - lo:.3f} km)")
        if len(lines) == 1:
            lines.append("clear")
        return "\n".join(lines[:8] + (["…"] if len(lines) > 8 else []))

    def mouseMoveEvent(self, event):
        point = _event_point(event)
        kp = self._kp_at(point.x())
        self._hover_x = point.x() if kp is not None else None
        self.update()
        if kp is not None:
            self.kpHovered.emit(kp)
            QToolTip.showText(_event_global(event), self.hover_text(kp), self)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_x = None
        self.update()
        super().leaveEvent(event)

    def event(self, event):
        # The dynamic hover tooltip replaces the static one while the
        # domain is set; keep Qt from popping the static text over it.
        if event.type() == _EV_TOOLTIP and self._domain_km > 0:
            return True
        return super().event(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        rect = self.rect().adjusted(0, 0, -1, -1)
        paint_spans(painter, rect, self._domain_km,
                    [tuple(span[:3]) for span in self._spans],
                    domain_start_km=self._domain_start_km)
        _paint_hover_line(painter, rect, self._hover_x)
        painter.setPen(QPen(QColor(120, 120, 120)))
        painter.drawRect(rect)
        painter.setPen(QPen(QColor(40, 40, 40)))
        if self._method_name:
            painter.drawText(rect.adjusted(6, 0, -6, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             self._method_name)
        if self._domain_km > 0:
            lo = self._domain_start_km
            painter.drawText(rect.adjusted(6, 0, -6, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             f"{lo:.1f} - {lo + self._domain_km:.1f} km")

    def mousePressEvent(self, event):
        if self._domain_km > 0 and self.width() > 0:
            kp = self._domain_start_km + (_event_point(event).x() / self.width()) * self._domain_km
            self.kpClicked.emit(
                max(self._domain_start_km,
                    min(self._domain_start_km + self._domain_km, kp)))


def fire_payload(data) -> Optional[Tuple[float, list, QColor, float, dict]]:
    """Normalise a fire-bar cell payload.

    ``(domain_km, intervals, color[, domain_start_km[, options]])`` where
    ``options`` may hold ``stale`` (bool: paint faded + dashed outline, the
    result is out of date) and ``nodata`` (intervals painted grey behind the
    fired ranges — where the criterion could not be evaluated).
    """
    if not data or len(data) < 3:
        return None
    domain_km, intervals, color = data[:3]
    domain_start_km = data[3] if len(data) >= 4 else 0.0
    options = data[4] if len(data) >= 5 and isinstance(data[4], dict) else {}
    return (float(domain_km or 0.0), list(intervals or []), color,
            float(domain_start_km or 0.0), options)


class FireBarDelegate(QStyledItemDelegate):
    """Paints a rule's fire intervals in its table cell (domain-aligned).

    Cell payload (item UserRole): see ``fire_payload``. A ``FireBarHover``
    installed on the table sets ``hover_kp`` so every bar in the column
    shows the same KP line.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover_kp: Optional[float] = None

    @staticmethod
    def bar_rect(cell_rect) -> QRect:
        return cell_rect.adjusted(BAR_INSET_PX, 0, -BAR_INSET_PX, 0)

    def paint(self, painter, option, index):
        try:
            payload = fire_payload(index.data(Qt.ItemDataRole.UserRole))
            if payload is None:
                super().paint(painter, option, index)
                return
            domain_km, intervals, color, domain_start_km, options = payload
            rect = self.bar_rect(option.rect)
            stale = bool(options.get("stale"))
            if stale:
                color = QColor(color)
                color.setAlpha(max(40, int(color.alpha() * 0.4)))
            spans = [(s, e, NODATA_COLOR) for (s, e) in options.get("nodata") or []]
            spans += [(s, e, color) for (s, e) in intervals]
            paint_spans(painter, rect, domain_km, spans,
                        domain_start_km=domain_start_km)
            if stale:
                painter.save()
                pen = QPen(QColor(120, 120, 120))
                pen.setStyle(_DASH_LINE)
                painter.setPen(pen)
                painter.drawRect(rect.adjusted(0, 1, -1, -2))
                painter.restore()
            if self.hover_kp is not None:
                _paint_hover_line(painter, rect, x_at_kp(
                    rect, self.hover_kp, domain_km, domain_start_km))
        except Exception:
            # A malformed payload must never raise inside a Qt paint event
            # (paint exceptions are noisy and can loop).
            try:
                super().paint(painter, option, index)
            except Exception:
                pass


class FireBarHover(QObject):
    """KP hover feedback for a table's fire-bar column.

    Tracks the mouse over the column, sets the delegate's ``hover_kp`` (one
    line across every bar) and shows ``tooltip_fn(row, kp, px_km)`` as a
    tooltip. ``kp_at(pos)`` gives the KP under a viewport position — used
    by right-click menus to order ranges nearest-first.
    """

    kpHovered = pyqtSignal(float)

    def __init__(self, table, column: int, delegate: FireBarDelegate,
                 tooltip_fn: Callable[[int, float, float], str]):
        super().__init__(table)
        self._table = table
        self._column = column
        self._delegate = delegate
        self._tooltip_fn = tooltip_fn
        table.setMouseTracking(True)
        table.viewport().setMouseTracking(True)
        table.viewport().installEventFilter(self)

    def kp_at(self, pos) -> Optional[Tuple[int, float, float]]:
        """``(row, kp, km_per_pixel)`` under a viewport position, or None."""
        index = self._table.indexAt(pos)
        if not index.isValid() or index.column() != self._column:
            return None
        payload = fire_payload(index.data(Qt.ItemDataRole.UserRole))
        if payload is None:
            return None
        domain_km, _intervals, _color, start_km, _options = payload
        rect = FireBarDelegate.bar_rect(self._table.visualRect(index))
        kp = kp_at_x(rect, pos.x(), domain_km, start_km)
        if kp is None:
            return None
        return index.row(), kp, domain_km / max(1, rect.width())

    def _set_hover(self, kp: Optional[float]) -> None:
        if self._delegate.hover_kp == kp:
            return
        self._delegate.hover_kp = kp
        self._table.viewport().update()

    def eventFilter(self, obj, event):
        try:
            kind = event.type()
            if kind == _EV_MOUSE_MOVE:
                hit = self.kp_at(_event_point(event))
                if hit is None:
                    if self._delegate.hover_kp is not None:
                        # Leaving the bar: drop its tooltip, but never
                        # another cell's ordinary tooltip.
                        QToolTip.hideText()
                    self._set_hover(None)
                else:
                    row, kp, px_km = hit
                    self._set_hover(kp)
                    self.kpHovered.emit(kp)
                    text = self._tooltip_fn(row, kp, px_km) or f"KP {kp:.3f}"
                    QToolTip.showText(_event_global(event), text,
                                      self._table.viewport())
            elif kind == _EV_LEAVE:
                self._set_hover(None)
            elif kind == _EV_TOOLTIP:
                pos = event.pos()
                if self.kp_at(pos) is not None:
                    return True  # the live hover tooltip owns this column
        except Exception:
            pass  # hover feedback must never break the table
        return False
