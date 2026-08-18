# -*- coding: utf-8 -*-
"""Shared KP-aligned bar widgets: overview verdict strip + per-rule fire bars.

Factored out of ``assessment_panel.py`` so the Assessment panel and the
Burial Planner draw their rule stacks with the same widgets rather than
copy-pasted painting code.

Additions over the original: the painted domain may start at a non-zero KP
(``domain_start_km``) so a scoped Burial Planner window renders correctly;
the Assessment panel keeps passing a bare ``domain_km`` (start 0).
"""

from __future__ import annotations

from typing import List

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import QStyledItemDelegate, QWidget

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


class VerdictStrip(QWidget):
    """Overview bar: paints the combined verdict spans for one method."""

    kpClicked = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(30)
        self._domain_km = 0.0
        self._domain_start_km = 0.0
        self._spans: List = []
        self._method_name = ""
        self.setToolTip("Combined suitability for the selected method. Click to locate on the map.")

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

    def paintEvent(self, _event):
        painter = QPainter(self)
        rect = self.rect().adjusted(0, 0, -1, -1)
        paint_spans(painter, rect, self._domain_km, self._spans,
                    domain_start_km=self._domain_start_km)
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
            kp = self._domain_start_km + (event.pos().x() / self.width()) * self._domain_km
            self.kpClicked.emit(
                max(self._domain_start_km,
                    min(self._domain_start_km + self._domain_km, kp)))


class FireBarDelegate(QStyledItemDelegate):
    """Paints a rule's fire intervals in its table cell (domain-aligned).

    Cell payload (item UserRole): ``(domain_km, intervals, color)`` or
    ``(domain_km, intervals, color, domain_start_km)``.
    """

    def paint(self, painter, option, index):
        try:
            data = index.data(Qt.ItemDataRole.UserRole)
            if not data:
                super().paint(painter, option, index)
                return
            if len(data) >= 4:
                domain_km, intervals, color, domain_start_km = data[:4]
            else:
                domain_km, intervals, color = data
                domain_start_km = 0.0
            spans = [(s, e, color) for (s, e) in intervals]
            paint_spans(painter, option.rect.adjusted(2, 0, -2, 0),
                        domain_km, spans, domain_start_km=domain_start_km)
        except Exception:
            # A malformed payload must never raise inside a Qt paint event
            # (paint exceptions are noisy and can loop).
            try:
                super().paint(painter, option, index)
            except Exception:
                pass
