# -*- coding: utf-8 -*-
"""Burial graph pages, painted with QPainter: one painter for the preview, PDF, PNG and SVG.

:class:`Report` lays an :class:`.assessment.Assessment` out on pages from a
template (:mod:`.report_template`): graph pages along KP windows (each
repeating a little of the last), then optional summary pages (key figures,
below-target ranges, per-section figures) and a data-sources page (every
file behind the assessment, with its checksum). :meth:`Report.paint` draws a
page on any paint device at any resolution, in millimetres, with fonts in
points, so the preview and the exports are the same drawing.

Graph pages: a title block (title, subtitle and a table of fields), a
legend, the panels in the template's order (status bar, DOL with the target
and the below-target fill, the governing-dataset bar, channels), one KP axis
with the same ticks on every panel, and a footer naming the data, the RPL,
the rule and who made the report when. Lines are thinned to the lowest and
highest value per 0.25 mm of paper, so a shallow spike is never lost.

Exports (:func:`export_pdf`, :func:`export_images`, :func:`export_svgs`)
write to a temporary file first and replace the target only when complete.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from qgis.PyQt.QtCore import QMarginsF, QPointF, QRectF, QSize, QSizeF, Qt
from qgis.PyQt.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage, QPageLayout, QPageSize, QPainter,
                             QPainterPath, QPdfWriter, QPen, QPolygonF)

from . import bins as B
from . import report_template as T
from . import style as S
from .assessment import Assessment, utc_text

_ALIGN_LEFT = getattr(getattr(Qt, "AlignmentFlag", Qt), "AlignLeft")
_ALIGN_RIGHT = getattr(getattr(Qt, "AlignmentFlag", Qt), "AlignRight")
_ALIGN_HCENTER = getattr(getattr(Qt, "AlignmentFlag", Qt), "AlignHCenter")
_ALIGN_VCENTER = getattr(getattr(Qt, "AlignmentFlag", Qt), "AlignVCenter")
_DASH = getattr(getattr(Qt, "PenStyle", Qt), "DashLine")
_NO_PEN = getattr(getattr(Qt, "PenStyle", Qt), "NoPen")
_FLAT = getattr(getattr(Qt, "PenCapStyle", Qt), "FlatCap")
_ANTIALIAS = getattr(getattr(QPainter, "RenderHint", QPainter), "Antialiasing")
_TEXT_ANTIALIAS = getattr(getattr(QPainter, "RenderHint", QPainter), "TextAntialiasing")
_ARGB32 = getattr(getattr(QImage, "Format", QImage), "Format_ARGB32")
_MM = getattr(QPageSize, "Unit", QPageSize).Millimeter
_LAYOUT_MM = getattr(QPageLayout, "Unit", QPageLayout).Millimeter

GRID = QColor(215, 215, 215)
INK = QColor(40, 40, 40)
HINT = QColor(110, 110, 110)
TITLE_BLOCK_MM = 22.0
LEGEND_MM = 5.0
FOOTER_MM = 7.0
AXIS_MM = 9.0
LEFT_MM = 19.0
RIGHT_MM = 3.0
GAP_MM = 1.5
ROW_MM = 4.6


@dataclass
class ReportData:
    """Everything a report shows besides the template: the assessment and its context."""
    assessment: Assessment
    tokens: Dict[str, str]
    provenance: str = ""
    sources: List[Dict] = field(default_factory=list)        # [{"dataset": name, "files": [file rows]}]
    channel_labels: Dict[str, Tuple[str, str]] = field(default_factory=dict)


def report_data(assessment: Assessment, context: Dict[str, str], author: str = "",
                made_utc: Optional[str] = None) -> ReportData:
    """Tokens, the provenance line and the data sources of a report on ``assessment``.

    ``context`` gives ``project``, ``plan``, ``rpl``, ``rpl_rev`` and ``target`` (text).
    """
    from datetime import datetime, timezone
    from .assessment import channel_labels
    names = [s.spec.name for s in assessment.series]
    mode = "latest data wins" if assessment.request.mode == B.MODE_LATEST else "worst case"
    now = made_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tokens = {key: str(context.get(key) or "") for key in ("project", "plan", "rpl", "rpl_rev", "target")}
    tokens.update(datasets=", ".join(names), mode=mode, date=now[:10])
    used = ", ".join(f"{s.spec.name} ({len(s.files)} file{'s' if len(s.files) != 1 else ''})"
                     for s in assessment.series)
    version = plugin_version()
    provenance = (f"Data: {used} · route {tokens['rpl']} {tokens['rpl_rev']}".rstrip()
                  + f" · target from plan {tokens['plan']} · {mode} · made {now}"
                  + (f" by {author}" if author else "") + f" with Subsea Cable Tools {version}".rstrip())
    sources = [{"dataset": s.spec.name, "files": list(s.files)} for s in assessment.series]
    return ReportData(assessment, tokens, provenance, sources, channel_labels(assessment))


def plugin_version() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "metadata.txt")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def decimate(x: np.ndarray, y: np.ndarray, lo: float, hi: float, columns: int) -> Tuple[np.ndarray, np.ndarray]:
    """Points of ``(x, y)`` (x ascending, NaN breaks kept) inside ``lo..hi``, at most a min and a max per column."""
    if x.size == 0:
        return x, y
    a = max(int(np.searchsorted(x, lo, side="left")) - 1, 0)
    b = min(int(np.searchsorted(x, hi, side="right")) + 1, x.size)
    xs, ys = x[a:b], y[a:b]
    if xs.size <= 2 * columns:
        return xs, ys
    finite = np.isfinite(ys) & np.isfinite(xs)
    col = np.floor((xs - lo) / max(hi - lo, 1e-12) * columns).astype(np.int64)
    # A new group where the column changes or a gap (NaN) starts: gaps stay gaps.
    breaks = np.r_[True, (col[1:] != col[:-1]) | (finite[1:] != finite[:-1])]
    starts = np.flatnonzero(breaks)
    out_x, out_y = [], []
    for s, e in zip(starts, np.r_[starts[1:], xs.size]):
        if not finite[s]:
            out_x.append(np.nan)
            out_y.append(np.nan)
            continue
        seg_y = ys[s:e]
        i_min, i_max = s + int(np.argmin(seg_y)), s + int(np.argmax(seg_y))
        for i in sorted({i_min, i_max}):
            out_x.append(xs[i])
            out_y.append(ys[i])
    return np.asarray(out_x), np.asarray(out_y)


class Report:
    """The pages of a burial graph report (see the module notes)."""

    def __init__(self, data: ReportData, template: Dict, extent: Optional[Tuple[float, float]] = None):
        self.data = data
        self.template = T.normalise(template)
        self.width_mm, self.height_mm = T.page_size_mm(self.template)
        self.extent = tuple(sorted(extent)) if extent is not None else data.assessment.extent_km
        if self.extent is None:
            raise ValueError("The assessment has no data to report.")
        self.windows = T.page_windows(self.extent, self.template["kp_per_page"], self.template["overlap_pct"])
        self.pages: List[Tuple[str, object]] = [("graph", w) for w in self.windows]
        if self.template["summary_page"]:
            self.pages += [("summary", chunk) for chunk in self._summary_chunks()]
        if self.template["sources_page"] and data.sources:
            self.pages += [("sources", chunk) for chunk in self._sources_chunks()]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    # -- tokens -------------------------------------------------------------------------
    def tokens(self, index: int) -> Dict[str, str]:
        out = dict(self.data.tokens)
        kind, payload = self.pages[index]
        lo, hi = payload if kind == "graph" else self.extent
        out.update(kp_start=T.kp_text(lo), kp_end=T.kp_text(hi), range_start=T.kp_text(self.extent[0]),
                   range_end=T.kp_text(self.extent[1]), page=str(index + 1), pages=str(self.page_count))
        return out

    # -- painting ------------------------------------------------------------------------
    def paint(self, painter: QPainter, index: int, dpi: float) -> None:
        """Draw page ``index`` on ``painter``'s device, whose page is this report's size at ``dpi``."""
        k = float(dpi) / 25.4
        painter.save()
        try:
            painter.setRenderHint(_ANTIALIAS, True)
            painter.setRenderHint(_TEXT_ANTIALIAS, True)
            painter.fillRect(QRectF(0, 0, self.width_mm * k, self.height_mm * k), QColor(255, 255, 255))
            kind, payload = self.pages[index]
            tokens = self.tokens(index)
            top = self._title_block(painter, k, tokens)
            bottom = self._footer(painter, k, tokens, index)
            if kind == "graph":
                self._graph(painter, k, payload, top, bottom)
            elif kind == "summary":
                self._table_page(painter, k, payload, top, bottom)
            else:
                self._table_page(painter, k, payload, top, bottom)
        finally:
            painter.restore()

    @staticmethod
    def _font(size_pt: float, bold: bool = False) -> QFont:
        font = QFont()
        font.setPointSizeF(size_pt)
        font.setBold(bold)
        return font

    def _text(self, painter, k, x, y, w, h, text, size, align=None, bold=False, colour=INK) -> None:
        painter.setFont(self._font(size, bold))
        painter.setPen(QPen(colour))
        flags = (align if align is not None else _ALIGN_LEFT) | _ALIGN_VCENTER
        metrics = QFontMetricsF(painter.font())
        shown = metrics.elidedText(str(text), getattr(getattr(Qt, "TextElideMode", Qt), "ElideRight"), w * k)
        painter.drawText(QRectF(x * k, y * k, w * k, h * k), int(flags), shown)

    def _line_pen(self, k, colour, width_mm: float, style=None) -> QPen:
        pen = QPen(QColor(*colour) if isinstance(colour, tuple) else colour)
        pen.setWidthF(max(width_mm * k, 0.5))
        pen.setCapStyle(_FLAT)
        if style is not None:
            pen.setStyle(style)
        return pen

    def _title_block(self, painter, k, tokens) -> float:
        t = self.template
        m = t["page"]["margin_mm"]
        width = self.width_mm - 2 * m
        fields = [(T.expand(a, tokens), T.expand(b, tokens)) for a, b in t["fields"] if a or b]
        has_title = bool(t["title"] or t["subtitle"])
        if not has_title and not fields:
            return m
        height = TITLE_BLOCK_MM
        field_w = min(150.0, width * 0.42) if fields else 0.0
        self._text(painter, k, m + 1, m + 1, width - field_w - 3, 8, T.expand(t["title"], tokens), 14, bold=True)
        self._text(painter, k, m + 1, m + 9.5, width - field_w - 3, 6, T.expand(t["subtitle"], tokens), 9,
                   colour=HINT)
        if fields:
            x0 = m + width - field_w
            row_h = min(height / len(fields), 5.0)
            key_w = min(32.0, field_w * 0.32)
            painter.setPen(self._line_pen(k, (150, 150, 150), 0.15))
            painter.setBrush(QBrush())
            for i, (key, value) in enumerate(fields):
                y = m + i * row_h
                painter.drawRect(QRectF(x0 * k, y * k, field_w * k, row_h * k))
                painter.drawLine(QPointF((x0 + key_w) * k, y * k), QPointF((x0 + key_w) * k, (y + row_h) * k))
                self._text(painter, k, x0 + 1, y, key_w - 2, row_h, key, 7, colour=HINT)
                self._text(painter, k, x0 + key_w + 1, y, field_w - key_w - 2, row_h, value, 7.5)
            height = max(height, row_h * len(fields))
        painter.setPen(self._line_pen(k, (60, 60, 60), 0.3))
        painter.drawLine(QPointF(m * k, (m + height + 1) * k), QPointF((self.width_mm - m) * k, (m + height + 1) * k))
        return m + height + 2.5

    def _footer(self, painter, k, tokens, index) -> float:
        t = self.template
        m = t["page"]["margin_mm"]
        if not (t["footer"] or t["page_numbers"] or t["footer_text"]):
            return self.height_mm - m
        y = self.height_mm - m - FOOTER_MM
        painter.setPen(self._line_pen(k, (150, 150, 150), 0.15))
        painter.drawLine(QPointF(m * k, y * k), QPointF((self.width_mm - m) * k, y * k))
        width = self.width_mm - 2 * m
        if t["footer"]:
            self._text(painter, k, m, y + 0.5, width * 0.78, FOOTER_MM / 2, self.data.provenance, 6.5, colour=HINT)
        if t["footer_text"]:
            self._text(painter, k, m, y + FOOTER_MM / 2, width * 0.78, FOOTER_MM / 2,
                       T.expand(t["footer_text"], tokens), 6.5, colour=HINT)
        if t["page_numbers"]:
            self._text(painter, k, m + width * 0.8, y + 0.5, width * 0.2, FOOTER_MM - 1,
                       f"Page {index + 1} of {self.page_count}", 8, align=_ALIGN_RIGHT)
        return y - 1.0

    # -- graph pages ---------------------------------------------------------------------
    def _graph(self, painter, k, window, top, bottom) -> None:
        t = self.template
        m = t["page"]["margin_mm"]
        lo, hi = window
        if t["legend"]:
            self._legend(painter, k, m + LEFT_MM, top, self.width_mm - 2 * m - LEFT_MM)
            top += LEGEND_MM + 1.0
        left, right = m + LEFT_MM, self.width_mm - m - RIGHT_MM
        plot_bottom = bottom - AXIS_MM
        panels = t["panels"]
        bars = T.bar_count(panels)
        weights = [p.get("weight", 1.0) for p in panels if p["kind"] not in T.BAR_KINDS]
        free = plot_bottom - top - bars * T.BAR_HEIGHT_MM - GAP_MM * max(len(panels) - 1, 0)
        if free <= 10 or not weights:
            self._text(painter, k, left, top, right - left, 10, "Not enough room for the panels on this page.", 9)
            return
        unit = free / sum(weights)
        step = t["kp_tick"] or None
        ticks = T.nice_ticks(lo, hi, max(4, int((right - left) / 35.0)), step)
        tick_step = ticks[1] - ticks[0] if len(ticks) > 1 else (hi - lo)

        def x_of(kp):
            frac = (np.asarray(kp, dtype=float) - lo) / (hi - lo)
            if t["reverse"]:
                frac = 1.0 - frac
            return left + frac * (right - left)

        y = top
        for panel in panels:
            height = T.BAR_HEIGHT_MM if panel["kind"] in T.BAR_KINDS else panel.get("weight", 1.0) * unit
            rect = (left, y, right - left, height)
            if panel["kind"] == T.PANEL_STATUS:
                self._bar(painter, k, rect, lo, hi, x_of, self.data.assessment.status_runs, S.STATUS_COLOURS)
            elif panel["kind"] == T.PANEL_SOURCE:
                colours = {i: S.dataset_colour(i) for i in range(len(self.data.assessment.series))}
                colours[-1] = S.NO_DATA_SOURCE
                self._bar(painter, k, rect, lo, hi, x_of, self.data.assessment.source_runs, colours)
            else:
                self._plot(painter, k, rect, panel, lo, hi, x_of, ticks)
            y += height + GAP_MM
        # The KP axis under the last panel.
        painter.setPen(self._line_pen(k, (60, 60, 60), 0.2))
        for tick in ticks:
            x = float(x_of(tick))
            painter.drawLine(QPointF(x * k, plot_bottom * k), QPointF(x * k, (plot_bottom + 1.2) * k))
            self._text(painter, k, x - 12, plot_bottom + 1.3, 24, 3.6, T.tick_label(tick, tick_step), 7,
                       align=_ALIGN_HCENTER)
        self._text(painter, k, left, plot_bottom + 4.8, right - left, 3.8, "KP (km)", 7.5, align=_ALIGN_HCENTER)

    def _legend(self, painter, k, x, y, width) -> None:
        a = self.data.assessment
        mode = "latest data" if a.request.mode == B.MODE_LATEST else "worst case"
        entries = [("line", S.GOVERNING, f"DOL used ({mode}, shallowest per metre)"), ("dash", S.TARGET, "Target"),
                   ("fill", S.BELOW_FILL, "Below target")]
        entries += [("line", S.faint(S.dataset_colour(i)), s.spec.name) for i, s in enumerate(a.series)]
        entries += [("fill", c, name) for _lv, c, name in S.STATUS_LEVELS[:3]]
        painter.setFont(self._font(7))
        metrics = QFontMetricsF(painter.font())
        cursor = x
        for kind, colour, text in entries:
            text_w = metrics.horizontalAdvance(text) / k if hasattr(metrics, "horizontalAdvance") \
                else metrics.width(text) / k
            if cursor + 8 + text_w > x + width:
                break
            mid = y + LEGEND_MM / 2
            if kind == "fill":
                painter.setPen(QPen(_NO_PEN))
                painter.setBrush(QBrush(QColor(*colour)))
                painter.drawRect(QRectF(cursor * k, (mid - 1.2) * k, 5 * k, 2.4 * k))
            else:
                painter.setPen(self._line_pen(k, colour, 0.45, _DASH if kind == "dash" else None))
                painter.drawLine(QPointF(cursor * k, mid * k), QPointF((cursor + 5) * k, mid * k))
            self._text(painter, k, cursor + 6, y, text_w + 2, LEGEND_MM, text, 7)
            cursor += 6 + text_w + 5

    def _bar(self, painter, k, rect, lo, hi, x_of, runs, colours) -> None:
        x0, y0, w, h = rect
        painter.setPen(QPen(_NO_PEN))
        painter.setRenderHint(_ANTIALIAS, False)     # crisp stretch edges, no blending with the page
        starts, ends, levels = runs
        inside = (ends > lo) & (starts < hi)
        for a, b, level in zip(np.maximum(starts[inside], lo), np.minimum(ends[inside], hi), levels[inside]):
            colour = colours.get(int(level))
            if colour is None:
                continue
            xa, xb = sorted((float(x_of(a)), float(x_of(b))))
            painter.setBrush(QBrush(QColor(*colour)))
            painter.drawRect(QRectF(xa * k, y0 * k, max(xb - xa, 0.05) * k, h * k))
        painter.setRenderHint(_ANTIALIAS, True)
        painter.setBrush(QBrush())
        painter.setPen(self._line_pen(k, (120, 120, 120), 0.15))
        painter.drawRect(QRectF(x0 * k, y0 * k, w * k, h * k))

    def _series_values(self, panel) -> List[Tuple[np.ndarray, np.ndarray, Tuple]]:
        """``[(kp, values, colour)]`` in KP order for a channel panel."""
        out = []
        for i, series in enumerate(self.data.assessment.series):
            values = series.channels.get(panel.get("channel", ""))
            if values is None or not np.isfinite(values).any():
                continue
            order = series.plot_order()
            out.append((series.kp[order], values[order], S.dataset_colour(i), series))
        return out

    def _plot(self, painter, k, rect, panel, lo, hi, x_of, ticks) -> None:
        a = self.data.assessment
        x0, y0, w, h = rect
        columns = max(int(w * 4), 50)
        lines: List[Tuple[np.ndarray, np.ndarray, Tuple, float, object]] = []
        if panel["kind"] == T.PANEL_DOL:
            if panel.get("datasets", True):
                for i, series in enumerate(a.series):
                    order = series.plot_order()
                    gap = max(series.binset.bridge_m, 1.0) / 1000.0
                    xs, ys = _breaks(series.kp[order], series.dol[order], gap)
                    lines.append((xs, ys, S.faint(S.dataset_colour(i)), 0.2, None))
            sel = a.selection
            gx, gy = _breaks(sel.centre_km, sel.worst, sel.step_m / 1000.0 * 1.5)
            lines.append((gx, gy, S.GOVERNING, 0.3, None))
        else:
            for xs, ys, colour, series in self._series_values(panel):
                gap = max(series.binset.bridge_m, 1.0) / 1000.0
                bx, by = _breaks(xs, ys, gap)
                lines.append((bx, by, colour, 0.25, None))
        thinned = [(*decimate(xs, ys, lo, hi, columns), colour, width, style) for xs, ys, colour, width, style in lines]
        values = [v[(px >= lo) & (px <= hi)] for px, v, *_rest in thinned]
        target_xy = self._target_steps(lo, hi) if panel["kind"] == T.PANEL_DOL and panel.get("target", True) else None
        if target_xy is not None:
            values.append(target_xy[1])
        y_min, y_max = panel.get("y_min"), panel.get("y_max")
        finite = np.concatenate([v[np.isfinite(v)] for v in values]) if values else np.zeros(0)
        if y_min is None or y_max is None:
            if finite.size:
                lo_v, hi_v = float(finite.min()), float(finite.max())
            else:
                lo_v, hi_v = 0.0, 1.0
            pad = max((hi_v - lo_v) * 0.06, 0.05)
            y_min = lo_v - pad if y_min is None else y_min
            y_max = hi_v + pad if y_max is None else y_max
        if y_max <= y_min:
            y_max = y_min + 1.0
        invert = panel.get("invert", False)

        def y_of(v):
            frac = (np.asarray(v, dtype=float) - y_min) / (y_max - y_min)
            return y0 + frac * h if invert else y0 + h - frac * h

        # Grid and labels.
        y_ticks = T.nice_ticks(y_min, y_max, max(2, int(h / 9.0)))
        y_step = y_ticks[1] - y_ticks[0] if len(y_ticks) > 1 else (y_max - y_min)
        grid = self._line_pen(k, GRID, 0.1)
        for tick in y_ticks:
            yy = float(y_of(tick))
            painter.setPen(grid)
            painter.drawLine(QPointF(x0 * k, yy * k), QPointF((x0 + w) * k, yy * k))
            self._text(painter, k, x0 - 10.5, yy - 1.6, 10, 3.2, T.tick_label(tick, y_step), 6.5, align=_ALIGN_RIGHT)
        painter.setPen(grid)
        for tick in ticks:
            xx = float(x_of(tick))
            painter.drawLine(QPointF(xx * k, y0 * k), QPointF(xx * k, (y0 + h) * k))
        painter.save()
        painter.translate((x0 - 16.5) * k, (y0 + h / 2) * k)
        painter.rotate(-90)
        self._text(painter, k, -h / 2, -2.2, h, 4.4, T.panel_title(panel), 7.5, align=_ALIGN_HCENTER)
        painter.restore()
        # Data, clipped to the panel.
        painter.save()
        painter.setClipRect(QRectF(x0 * k, y0 * k, w * k, h * k))
        if panel["kind"] == T.PANEL_DOL and panel.get("below", True):
            self._below_fill(painter, k, lo, hi, x_of, y_of)
        for px, py, colour, width, style in thinned:
            self._polyline(painter, k, x_of(px), y_of(py), colour, width, style)
        if target_xy is not None:
            self._polyline(painter, k, x_of(target_xy[0]), y_of(target_xy[1]), S.TARGET, 0.3, _DASH)
        painter.restore()
        painter.setBrush(QBrush())
        painter.setPen(self._line_pen(k, (90, 90, 90), 0.2))
        painter.drawRect(QRectF(x0 * k, y0 * k, w * k, h * k))

    def _target_steps(self, lo, hi) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        xs, ys = [], []
        for a, b, depth in self.data.assessment.request.target_runs:
            if depth is None or b <= lo or a >= hi:
                continue
            if xs and abs(xs[-1] - a) > 1e-9:
                xs.append(np.nan)
                ys.append(np.nan)
            xs += [max(a, lo), min(b, hi)]
            ys += [depth, depth]
        return (np.asarray(xs), np.asarray(ys)) if xs else None

    def _below_fill(self, painter, k, lo, hi, x_of, y_of) -> None:
        a = self.data.assessment
        sel = a.selection
        step = sel.step_m / 1000.0
        rows = np.flatnonzero((a.status == B.STATUS_BELOW) & (sel.index * step < hi) & ((sel.index + 1) * step > lo))
        if not rows.size:
            return
        path = QPainterPath()
        groups = np.split(rows, np.flatnonzero(np.diff(sel.index[rows]) > 1) + 1)
        for group in groups:
            starts = sel.index[group] * step
            xs = np.repeat(starts, 2)
            xs[1::2] += step
            top = np.repeat(a.target[group], 2)
            bottom = np.repeat(sel.worst[group], 2)
            px = x_of(np.concatenate([xs, xs[::-1]]))
            py = y_of(np.concatenate([top, bottom[::-1]]))
            path.addPolygon(QPolygonF([QPointF(float(u) * k, float(v) * k) for u, v in zip(px, py)]))
        painter.setPen(QPen(_NO_PEN))
        painter.setBrush(QBrush(QColor(*S.BELOW_FILL)))
        painter.drawPath(path)
        painter.setBrush(QBrush())

    def _polyline(self, painter, k, xs, ys, colour, width, style) -> None:
        path = QPainterPath()
        pen_down = False
        for u, v in zip(np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)):
            if not (math.isfinite(u) and math.isfinite(v)):
                pen_down = False
                continue
            if pen_down:
                path.lineTo(u * k, v * k)
            else:
                path.moveTo(u * k, v * k)
                pen_down = True
        painter.setBrush(QBrush())
        painter.setPen(self._line_pen(k, colour, width, style))
        painter.drawPath(path)

    # -- summary and sources pages -------------------------------------------------------
    def _rows_per_page(self) -> int:
        m = self.template["page"]["margin_mm"]
        usable = self.height_mm - 2 * m - TITLE_BLOCK_MM - 2.5 - FOOTER_MM - 12.0
        return max(int(usable / ROW_MM), 5)

    def _summary_chunks(self) -> List[List[Tuple]]:
        a = self.data.assessment
        s = a.stats
        names = [series.spec.name for series in a.series]
        lines = [("Assessed", f"{s['assessed_m'] / 1000.0:.3f} km of {s['scope_m'] / 1000.0:.3f} km "
                              f"(KP {T.kp_text(self.extent[0])} to {T.kp_text(self.extent[1])})"),
                 ("No data", f"{s['no_data_m'] / 1000.0:.3f} km"),
                 ("Meets target", "" if s["meets_pct"] is None else f"{s['meets_pct']:.1f} % of the assessed length"),
                 ("Below target", f"{s['below_m'] / 1000.0:.3f} km in {len(a.ranges)} range(s)"),
                 ("Minimum DOL", "" if s["min"] is None else f"{s['min']:.2f} m at KP {s['min_kp']:.3f}"),
                 ("Data", ", ".join(names)),
                 ("Where datasets overlap", "the latest data" if a.request.mode == B.MODE_LATEST else "the worst case"),
                 ("Method", "DOL per 1 m of KP is the shallowest of the records there; gaps wider than the "
                            "bridging limit are no data, not below target")]
        rows: List[Tuple] = [("heading", "Summary", None)] + [("pair", k, v) for k, v in lines]
        rows.append(("heading", "Below-target ranges", ["KP from", "KP to", "Length (m)", "Min DOL (m)", "at KP",
                                                        "Worst shortfall (m)", "Dataset"]))
        for r in a.ranges:
            rows.append(("row", None, [f"{r['kp_from']:.3f}", f"{r['kp_to']:.3f}", f"{r['length_m']:.1f}",
                                       f"{r['min']:.2f}", f"{r['min_kp']:.3f}", f"{r['max_shortfall']:.2f}",
                                       ", ".join(names[i] for i in r["sources"])]))
        if not a.ranges:
            rows.append(("pair", "", "None."))
        if a.sections:
            rows.append(("heading", "By plan section", ["Section", "KP from", "KP to", "Assessed (m)", "No data (m)",
                                                        "Meets target (%)", "Below (m)", "Ranges", "Min DOL (m)"]))
            for sec in a.sections:
                rows.append(("row", None, [sec.get("kind", "").replace("_", " "), f"{sec['start_kp']:.3f}",
                                           f"{sec['end_kp']:.3f}", f"{sec['assessed_m']:.1f}",
                                           f"{sec['no_data_m']:.1f}",
                                           "" if sec["meets_pct"] is None else f"{sec['meets_pct']:.1f}",
                                           f"{sec['below_m']:.1f}", str(sec.get("ranges", 0)),
                                           "" if sec["min"] is None else f"{sec['min']:.2f}"]))
        return _paginate(rows, self._rows_per_page())

    def _sources_chunks(self) -> List[List[Tuple]]:
        rows: List[Tuple] = []
        for source in self.data.sources:
            rows.append(("heading", f"Data source: {source['dataset']}",
                         ["File", "Received", "Rows", "Rejected", "Time or date (UTC)", "KP", "Status", "sha256"]))
            for f in source["files"]:
                when = (f"{utc_text(f.get('time_min'))} … {utc_text(f.get('time_max'))}" if f.get("time_min")
                        is not None else str(f.get("acquired_utc") or "")[:10])
                kp = (f"{f['kp_min']:.3f} … {f['kp_max']:.3f}" if f.get("kp_min") is not None else "")
                rows.append(("row", None, [f["name"], str(f.get("received_utc") or "")[:10],
                                           f"{f.get('rows_kept') or 0:,}", f"{f.get('rows_rejected') or 0:,}", when, kp,
                                           f["status"], str(f.get("sha256") or "")[:16]]))
        return _paginate(rows, self._rows_per_page())

    def _table_page(self, painter, k, rows, top, bottom) -> None:
        m = self.template["page"]["margin_mm"]
        left, width = m, self.width_mm - 2 * m
        y = top + 1.0
        widths: List[float] = []
        for kind, label, cells in rows:
            if kind == "heading":
                y += 1.5
                self._text(painter, k, left, y, width, ROW_MM + 0.6, label, 10, bold=True)
                y += ROW_MM + 1.0
                if cells:
                    widths = _column_widths(cells, width)
                    self._cells(painter, k, left, y, widths, cells, bold=True)
                    y += ROW_MM
            elif kind == "pair":
                self._text(painter, k, left, y, 45, ROW_MM, label, 8, colour=HINT)
                self._text(painter, k, left + 46, y, width - 46, ROW_MM, cells, 8)
                y += ROW_MM
            else:
                self._cells(painter, k, left, y, widths or _column_widths(cells, width), cells)
                y += ROW_MM
            if y > bottom:
                break

    def _cells(self, painter, k, x, y, widths, cells, bold=False) -> None:
        painter.setPen(self._line_pen(k, (200, 200, 200), 0.1))
        painter.drawLine(QPointF(x * k, (y + ROW_MM) * k), QPointF((x + sum(widths)) * k, (y + ROW_MM) * k))
        for w, cell in zip(widths, cells):
            self._text(painter, k, x + 0.8, y, w - 1.6, ROW_MM, cell, 7.5, bold=bold)
            x += w


def _column_widths(cells: Sequence[str], width: float) -> List[float]:
    weights = [max(len(str(c)), 6) for c in cells]
    weights[-1] = max(weights[-1], 14)
    total = float(sum(weights))
    return [width * wgt / total for wgt in weights]


def _paginate(rows: List[Tuple], per_page: int) -> List[List[Tuple]]:
    """Split rows into pages, repeating the current table's heading at the top of a continued page."""
    pages: List[List[Tuple]] = []
    page: List[Tuple] = []
    heading = None
    used = 0
    for row in rows:
        cost = 3 if row[0] == "heading" else 1
        if used + cost > per_page and page:
            pages.append(page)
            page = []
            used = 0
            if row[0] != "heading" and heading is not None:
                page.append(("heading", f"{heading[1]} (continued)", heading[2]))
                used = 3
        if row[0] == "heading":
            heading = row
        page.append(row)
        used += cost
    if page:
        pages.append(page)
    return pages


def _breaks(x: np.ndarray, y: np.ndarray, gap_km: float) -> Tuple[np.ndarray, np.ndarray]:
    if x.size < 2:
        return x, y
    cut = np.flatnonzero(np.diff(x) > gap_km) + 1
    if not cut.size:
        return x, y
    return np.insert(x, cut, np.nan), np.insert(y, cut, np.nan)


# --------------------------------------------------------------------------- output
def render_image(report: Report, index: int, dpi: float = 96.0) -> QImage:
    width = int(round(report.width_mm / 25.4 * dpi))
    height = int(round(report.height_mm / 25.4 * dpi))
    image = QImage(width, height, _ARGB32)
    image.setDotsPerMeterX(int(round(dpi / 0.0254)))
    image.setDotsPerMeterY(int(round(dpi / 0.0254)))
    image.fill(QColor(255, 255, 255))
    painter = QPainter(image)
    try:
        report.paint(painter, index, dpi)
    finally:
        painter.end()
    return image


def _replace(temporary: str, target: str) -> None:
    os.replace(temporary, target)


def export_pdf(report: Report, path: str, progress: Optional[Callable[[float], None]] = None) -> int:
    """Write every page to a vector PDF; returns the page count."""
    temporary = path + ".part"
    writer = QPdfWriter(temporary)
    writer.setResolution(600)
    writer.setPageSize(QPageSize(QSizeF(report.width_mm, report.height_mm), _MM))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), _LAYOUT_MM)
    writer.setTitle(T.expand(report.template["title"], report.tokens(0)))
    writer.setCreator("Subsea Cable Tools")
    painter = QPainter()
    if not painter.begin(writer):
        raise OSError(f"Could not write {path}")
    try:
        for index in range(report.page_count):
            if index and not writer.newPage():
                raise OSError("Could not start a new PDF page")
            report.paint(painter, index, writer.resolution())
            if progress:
                progress((index + 1) / report.page_count)
    finally:
        painter.end()
    del writer
    if not os.path.isfile(temporary) or os.path.getsize(temporary) == 0:
        raise OSError("The PDF came out empty.")
    _replace(temporary, path)
    return report.page_count


def numbered(path: str, index: int, count: int) -> str:
    stem, ext = os.path.splitext(path)
    return path if count == 1 else f"{stem}_p{index + 1:0{max(2, len(str(count)))}d}{ext}"


def export_images(report: Report, path: str, dpi: float = 200.0, pages: Optional[Sequence[int]] = None) -> List[str]:
    """One PNG per page (``name_p01.png``…); returns the files written."""
    chosen = list(pages) if pages is not None else list(range(report.page_count))
    out = []
    for index in chosen:
        target = numbered(path, index, len(chosen))
        image = render_image(report, index, dpi)
        temporary = target + ".part.png"
        if not image.save(temporary, "PNG"):
            raise OSError(f"Could not write {target}")
        _replace(temporary, target)
        out.append(target)
    return out


def _write_svg(report: Report, index: int, path: str, dpi: float) -> None:
    """One page to ``path`` (the generator holds the file until it is gone: it goes with this call)."""
    from qgis.PyQt.QtSvg import QSvgGenerator
    generator = QSvgGenerator()
    generator.setFileName(path)
    width = int(round(report.width_mm / 25.4 * dpi))
    height = int(round(report.height_mm / 25.4 * dpi))
    generator.setSize(QSize(width, height))
    generator.setViewBox(QRectF(0, 0, width, height))
    generator.setResolution(int(dpi))
    generator.setTitle(T.expand(report.template["title"], report.tokens(index)))
    painter = QPainter()
    if not painter.begin(generator):
        raise OSError(f"Could not write {path}")
    try:
        report.paint(painter, index, dpi)
    finally:
        painter.end()


def export_svgs(report: Report, path: str, pages: Optional[Sequence[int]] = None) -> List[str]:
    """One SVG per page (for a print layout or a drawing); returns the files written."""
    chosen = list(pages) if pages is not None else list(range(report.page_count))
    out = []
    for index in chosen:
        target = numbered(path, index, len(chosen))
        temporary = target + ".part"
        _write_svg(report, index, temporary, 96.0)
        _replace(temporary, target)
        out.append(target)
    return out
