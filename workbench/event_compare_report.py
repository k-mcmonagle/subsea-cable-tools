# -*- coding: utf-8 -*-
"""Radial plots and the standalone HTML report for an event comparison.

Pure Python: SVG is written as text so the same figures appear in the panel
preview (QSvgWidget), the HTML report and, via QSvgRenderer, a PDF page,
without any plotting dependency. One self-contained HTML file (inline CSS
and SVG), in the same style as the burial plan report.

Plots
-----
* **Radial plot** — offsets of B's event(s) about A's event at the centre,
  either route-relative (x = cross-course, + starboard right; y =
  along-track, + ahead up) or north-up (x = east, y = north). Range rings
  at round distances, the target radius dashed, the mean offset marked.
* **Offsets along the route** — cross-course and along-track against A's KP,
  as two stacked panels sharing the KP axis (never a dual-axis chart).

Colour: at most three event types get a categorical colour (the reference
palette's first three slots, the only ones distinguishable in a scatter for
colour-blind readers); further types fold into "Other". Every point has a
hover tooltip and every figure has a table beside it in the report.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .event_compare import (
    HOW_LABELS,
    EventOffset,
    OffsetStats,
    offset_stats,
    stats_by_type,
    type_label,
)

FRAME_ROUTE = "route"
FRAME_NORTH = "north"

SERIES = ("#2a78d6", "#eb6834", "#1baf7a")
OTHER_COLOUR = "#8a8984"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
TEXT_MUTED = "#8a8984"
GRID = "#e4e3df"
AXIS = "#bdbcb6"
SURFACE = "#fcfcfb"
TARGET = "#c0392b"
MEAN = "#0b0b0b"


def colour_map(rows: Sequence[EventOffset]) -> Dict[str, str]:
    """Type -> colour: the three most frequent types get a series colour,
    the rest share the 'Other' grey. Stable for a given selection."""
    counts: Dict[str, int] = {}
    for row in rows:
        if row.matched:
            counts[row.type_key] = counts.get(row.type_key, 0) + 1
    ordered = sorted(counts, key=lambda key: (-counts[key], type_label(key)))
    return {key: (SERIES[i] if i < len(SERIES) else OTHER_COLOUR)
            for i, key in enumerate(ordered)}


def nice_step(value: float) -> float:
    """A 1/2/5 x 10^n step for range rings / ticks."""
    if value <= 0 or not math.isfinite(value):
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / 10 ** exponent
    for nice in (1.0, 2.0, 5.0, 10.0):
        if fraction <= nice:
            return nice * 10 ** exponent
    return 10.0 * 10 ** exponent


def _xy(row: EventOffset, frame: str) -> Optional[Tuple[float, float]]:
    if frame == FRAME_NORTH:
        if row.east_m is None or row.north_m is None:
            return None
        return row.east_m, row.north_m
    if row.cross_m is None or row.along_m is None:
        return None
    return row.cross_m, row.along_m


def _esc(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def _fmt(value, decimals=1, signed=False, unit=" m") -> str:
    if value is None:
        return "–"
    return f"{value:+.{decimals}f}{unit}" if signed else f"{value:.{decimals}f}{unit}"


def point_tooltip(row: EventOffset) -> str:
    a, b = row.a, row.b
    lines = [f"{a.event if a else ''} → {b.event if b else ''}"]
    if a is not None and a.kp is not None:
        lines.append(f"A KP {a.kp:.3f} km")
    lines.append(f"Along {_fmt(row.along_m, signed=True)}, cross {_fmt(row.cross_m, signed=True)}")
    lines.append(f"Radial {_fmt(row.radial_m)}"
                 + (f" @ {row.bearing_deg:.0f}°" if row.bearing_deg is not None else ""))
    if row.kp_delta_m is not None:
        lines.append(f"ΔKP {row.kp_delta_m:+.1f} m")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Radial plot
# ---------------------------------------------------------------------------
def radial_plot_svg(rows: Sequence[EventOffset], frame: str = FRAME_ROUTE,
                    size: int = 360, target_radius_m: Optional[float] = None,
                    title: str = "", colours: Optional[Dict[str, str]] = None,
                    label_points: Optional[bool] = None, show_legend: bool = True,
                    range_m: Optional[float] = None) -> str:
    """SVG radial (bullseye) plot of offsets about A's event at the centre."""
    plotted = [(row, _xy(row, frame)) for row in rows if row.matched]
    plotted = [(row, xy) for row, xy in plotted if xy is not None]
    colours = colours if colours is not None else colour_map([r for r, _ in plotted])
    single = len(plotted) == 1
    if label_points is None:
        label_points = len(plotted) <= 8

    extent = max([math.hypot(x, y) for _, (x, y) in plotted] + [target_radius_m or 0.0, 0.0])
    if range_m:
        extent = max(extent, range_m)
    step = nice_step(max(extent, 1.0) / 3.0)
    radius_m = step * max(1, math.ceil(extent * 1.08 / step)) if extent > 0 else step * 3
    legend_h = 0
    legend_items = []
    if show_legend and not single:
        seen = []
        for row, _ in plotted:
            colour = colours.get(row.type_key, OTHER_COLOUR)
            name = type_label(row.type_key) if colour != OTHER_COLOUR else "Other"
            if (name, colour) not in seen:
                seen.append((name, colour))
        legend_items = seen
        legend_h = 22 if legend_items else 0
    title_h = 24 if title else 0
    pad = 34
    width = size
    height = size + title_h + legend_h
    cx = width / 2.0
    cy = title_h + size / 2.0
    r_px = size / 2.0 - pad
    scale = r_px / radius_m

    out: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="Segoe UI, Arial, sans-serif" '
        f'role="img" aria-label="{_esc(title or "Radial offset plot")}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{SURFACE}"/>',
    ]
    if title:
        out.append(f'<text x="{cx:.1f}" y="17" text-anchor="middle" font-size="13" '
                   f'font-weight="600" fill="{TEXT}">{_esc(title)}</text>')
    # Rings and labels.
    ring = step
    while ring <= radius_m + 1e-9:
        r = ring * scale
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" '
                   f'stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{cx + r * 0.707 + 2:.1f}" y="{cy - r * 0.707 - 2:.1f}" '
                   f'font-size="9" fill="{TEXT_MUTED}">{_ring_label(ring)}</text>')
        ring += step
    out.append(f'<line x1="{cx - r_px:.1f}" y1="{cy:.1f}" x2="{cx + r_px:.1f}" y2="{cy:.1f}" '
               f'stroke="{AXIS}" stroke-width="1"/>')
    out.append(f'<line x1="{cx:.1f}" y1="{cy - r_px:.1f}" x2="{cx:.1f}" y2="{cy + r_px:.1f}" '
               f'stroke="{AXIS}" stroke-width="1"/>')
    if frame == FRAME_NORTH:
        labels = (("N", cx, cy - r_px - 6, "middle"), ("S", cx, cy + r_px + 14, "middle"),
                  ("E", cx + r_px + 4, cy + 4, "start"), ("W", cx - r_px - 4, cy + 4, "end"))
    else:
        labels = (("Ahead", cx, cy - r_px - 6, "middle"), ("Behind", cx, cy + r_px + 14, "middle"),
                  ("Stbd", cx + r_px + 3, cy + 4, "start"), ("Port", cx - r_px - 3, cy + 4, "end"))
    for text, x, y, anchor in labels:
        out.append(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-size="10" '
                   f'fill="{TEXT_SECONDARY}">{text}</text>')
    if frame != FRAME_NORTH:
        # Route direction arrow through the centre.
        out.append(f'<path d="M {cx:.1f} {cy - r_px + 2:.1f} l -4 8 l 8 0 z" fill="{AXIS}"/>')
    if target_radius_m:
        r = target_radius_m * scale
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" '
                   f'stroke="{TARGET}" stroke-width="1.5" stroke-dasharray="5 4">'
                   f'<title>Target radius {target_radius_m:g} m</title></circle>')
    # Design position.
    out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="{SURFACE}" '
               f'stroke="{TEXT}" stroke-width="1.5"><title>A (reference) position</title></circle>')
    # Points (largest offsets drawn last so they stay visible).
    for row, (x, y) in sorted(plotted, key=lambda item: math.hypot(*item[1])):
        px, py = cx + x * scale, cy - y * scale
        colour = colours.get(row.type_key, OTHER_COLOUR)
        radius = 6 if single else 4.5
        if single:
            out.append(f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{px:.1f}" y2="{py:.1f}" '
                       f'stroke="{colour}" stroke-width="2"/>')
        out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{radius}" fill="{colour}" '
                   f'stroke="{SURFACE}" stroke-width="2"><title>{_esc(point_tooltip(row))}</title></circle>')
        if label_points and row.a is not None:
            anchor = "start" if px >= cx else "end"
            dx = 7 if px >= cx else -7
            out.append(f'<text x="{px + dx:.1f}" y="{py - 6:.1f}" text-anchor="{anchor}" '
                       f'font-size="10" fill="{TEXT}">{_esc(_short(row.a.event))}</text>')
    if len(plotted) >= 2:
        mean_x = sum(x for _, (x, _y) in plotted) / len(plotted)
        mean_y = sum(y for _, (_x, y) in plotted) / len(plotted)
        mx, my = cx + mean_x * scale, cy - mean_y * scale
        out.append(f'<path d="M {mx - 6:.1f} {my:.1f} H {mx + 6:.1f} M {mx:.1f} {my - 6:.1f} '
                   f'V {my + 6:.1f}" stroke="{MEAN}" stroke-width="2">'
                   f'<title>Mean offset ({mean_x:+.1f} m, {mean_y:+.1f} m)</title></path>')
    if legend_items:
        x = 10.0
        y = height - 8
        for name, colour in legend_items:
            out.append(f'<circle cx="{x + 4:.1f}" cy="{y - 4:.1f}" r="4" fill="{colour}"/>')
            out.append(f'<text x="{x + 12:.1f}" y="{y:.1f}" font-size="10" '
                       f'fill="{TEXT_SECONDARY}">{_esc(name)}</text>')
            x += 22 + 6.2 * len(name)
        if len(plotted) >= 2:
            out.append(f'<path d="M {x:.1f} {y - 4:.1f} h 8 M {x + 4:.1f} {y - 8:.1f} v 8" '
                       f'stroke="{MEAN}" stroke-width="2"/>')
            out.append(f'<text x="{x + 12:.1f}" y="{y:.1f}" font-size="10" '
                       f'fill="{TEXT_SECONDARY}">Mean</text>')
    out.append("</svg>")
    return "".join(out)


def _ring_label(value: float) -> str:
    if value >= 1000 and value % 1000 == 0:
        return f"{value / 1000:g} km"
    return f"{value:g} m"


def _short(text: str, limit: int = 18) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------------------------------------------------------------------
# Offsets along the route
# ---------------------------------------------------------------------------
def kp_offset_chart_svg(rows: Sequence[EventOffset], width: int = 860,
                        target_radius_m: Optional[float] = None,
                        colours: Optional[Dict[str, str]] = None) -> str:
    """Two stacked panels sharing the KP axis: cross-course and along-track."""
    plotted = [r for r in rows if r.matched and r.a is not None and r.a.kp is not None]
    colours = colours if colours is not None else colour_map(plotted)
    panel_h, gap, left, right, top, bottom = 130, 30, 58, 16, 22, 34
    height = top + 2 * panel_h + gap + bottom
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="Segoe UI, Arial, sans-serif" '
        'role="img" aria-label="Cross-course and along-track offsets against KP">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{SURFACE}"/>',
    ]
    if not plotted:
        out.append(f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
                   f'font-size="12" fill="{TEXT_MUTED}">No matched events with a KP</text></svg>')
        return "".join(out)
    kps = [r.a.kp for r in plotted]
    kp_min, kp_max = min(kps), max(kps)
    if kp_max - kp_min < 1e-6:
        kp_min, kp_max = kp_min - 1.0, kp_max + 1.0
    span = kp_max - kp_min
    kp_min -= span * 0.03
    kp_max += span * 0.03
    plot_w = width - left - right

    def x_of(kp):
        return left + (kp - kp_min) / (kp_max - kp_min) * plot_w

    panels = (("Cross-course (m)", "+ stbd / − port", lambda r: r.cross_m),
              ("Along-track (m)", "+ ahead / − behind", lambda r: r.along_m))
    for index, (label, sign_text, value_of) in enumerate(panels):
        y0 = top + index * (panel_h + gap)
        values = [value_of(r) for r in plotted if value_of(r) is not None]
        extent = max([abs(v) for v in values] + [1.0])
        if target_radius_m:
            extent = max(extent, target_radius_m)
        step = nice_step(extent / 2.0)
        limit = step * math.ceil(extent * 1.05 / step)
        mid = y0 + panel_h / 2.0

        def y_of(value, mid=mid, limit=limit):
            return mid - value / limit * (panel_h / 2.0)

        tick = -limit
        while tick <= limit + 1e-9:
            y = y_of(tick)
            out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
                       f'stroke="{AXIS if abs(tick) < 1e-9 else GRID}" stroke-width="1"/>')
            out.append(f'<text x="{left - 6}" y="{y + 3:.1f}" text-anchor="end" font-size="9" '
                       f'fill="{TEXT_MUTED}">{tick:+g}</text>')
            tick += step
        if target_radius_m:
            for bound in (target_radius_m, -target_radius_m):
                y = y_of(bound)
                out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
                           f'stroke="{TARGET}" stroke-width="1" stroke-dasharray="5 4"/>')
        out.append(f'<text x="{left}" y="{y0 - 8}" font-size="11" font-weight="600" '
                   f'fill="{TEXT}">{label}</text>')
        out.append(f'<text x="{width - right}" y="{y0 - 8}" text-anchor="end" font-size="10" '
                   f'fill="{TEXT_SECONDARY}">{sign_text}</text>')
        for row in plotted:
            value = value_of(row)
            if value is None:
                continue
            x = x_of(row.a.kp)
            colour = colours.get(row.type_key, OTHER_COLOUR)
            out.append(f'<line x1="{x:.1f}" y1="{mid:.1f}" x2="{x:.1f}" y2="{y_of(value):.1f}" '
                       f'stroke="{colour}" stroke-width="2"/>')
            out.append(f'<circle cx="{x:.1f}" cy="{y_of(value):.1f}" r="4" fill="{colour}" '
                       f'stroke="{SURFACE}" stroke-width="2"><title>{_esc(point_tooltip(row))}'
                       '</title></circle>')
    # KP axis.
    axis_y = top + 2 * panel_h + gap + 4
    step = nice_step((kp_max - kp_min) / 8.0)
    tick = math.ceil(kp_min / step) * step
    while tick <= kp_max:
        x = x_of(tick)
        out.append(f'<text x="{x:.1f}" y="{axis_y + 10}" text-anchor="middle" font-size="9" '
                   f'fill="{TEXT_MUTED}">{tick:g}</text>')
        tick += step
    out.append(f'<text x="{left + plot_w / 2:.1f}" y="{height - 4}" text-anchor="middle" '
               f'font-size="10" fill="{TEXT_SECONDARY}">A KP (km)</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
_CSS = """
body { font-family: Segoe UI, Arial, sans-serif; color: #1a1a1a; background: #fff;
       margin: 2.2em auto; max-width: 64em; padding: 0 1em; }
h1 { font-size: 1.5em; margin-bottom: 0.1em; }
h2 { font-size: 1.15em; border-bottom: 1px solid #ccc; padding-bottom: 0.15em;
     margin-top: 1.6em; }
table { border-collapse: collapse; width: 100%; font-size: 0.82em; margin: 0.6em 0; }
th, td { border: 1px solid #d0d0d0; padding: 3px 7px; text-align: left; vertical-align: top; }
td.n { text-align: right; font-variant-numeric: tabular-nums; }
th { background: #f2f2f2; }
tr:nth-child(even) td { background: #fafafa; }
.meta { color: #555; font-size: 0.9em; }
.summary { display: flex; flex-wrap: wrap; gap: 1.6em; margin: 0.8em 0; }
.summary div { min-width: 8em; }
.summary .big { font-size: 1.3em; font-weight: 600; }
.summary .lbl { color: #555; font-size: 0.82em; }
.figs { display: flex; flex-wrap: wrap; gap: 1em; align-items: flex-start; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(17em, 1fr)); gap: 0.9em; }
.card { border: 1px solid #ddd; border-radius: 6px; padding: 0.5em; break-inside: avoid; }
.card table { font-size: 0.78em; }
.card svg { width: 100%; height: auto; }
.ok { color: #1b5e20; font-weight: 600; }
.bad { color: #b71c1c; font-weight: 600; }
.muted { color: #777; font-size: 0.85em; }
.note { background: #fff8e1; border: 1px solid #f0e0a0; padding: 0.5em 0.8em; font-size: 0.85em;
        margin: 1em 0; }
@media print { .card, .figs { break-inside: avoid; } body { margin: 0; } }
"""


def _within_html(row: EventOffset, target):
    within = row.within(target)
    if within is None:
        return ""
    return '<span class="ok">✓ yes</span>' if within else '<span class="bad">✗ no</span>'


def _stats_tiles(stats: OffsetStats) -> str:
    tiles = [
        ("Matched events", f"{stats.count}"),
        ("Radial mean", _fmt(stats.radial_mean)),
        ("Radial RMS", _fmt(stats.radial_rms)),
        ("Radial 95th pct", _fmt(stats.radial_p95)),
        ("Radial max", _fmt(stats.radial_max)),
        ("Along-track mean", _fmt(stats.along_mean, signed=True)),
        ("Cross-course mean", _fmt(stats.cross_mean, signed=True)),
    ]
    if stats.within_target is not None:
        tiles.append((f"Within {stats.target_radius_m:g} m", f"{stats.within_target} / {stats.count}"))
    return '<div class="summary">' + "".join(
        f'<div><div class="big">{_esc(value)}</div><div class="lbl">{_esc(label)}</div></div>'
        for label, value in tiles) + "</div>"


def _type_table(rows, target) -> str:
    lines = ["<table><tr><th>Event type</th><th>Matched</th><th>Radial mean</th>"
             "<th>Radial max</th><th>Along mean ± sd</th><th>Cross mean ± sd</th>"
             + ("<th>Within target</th>" if target else "") + "</tr>"]
    for key, stats in stats_by_type(rows, target):
        if not stats.count:
            continue
        lines.append(
            f"<tr><td>{_esc(type_label(key))}</td><td class='n'>{stats.count}</td>"
            f"<td class='n'>{_fmt(stats.radial_mean)}</td><td class='n'>{_fmt(stats.radial_max)}</td>"
            f"<td class='n'>{_fmt(stats.along_mean, signed=True)} ± {_fmt(stats.along_std, unit='')}</td>"
            f"<td class='n'>{_fmt(stats.cross_mean, signed=True)} ± {_fmt(stats.cross_std, unit='')}</td>"
            + (f"<td class='n'>{stats.within_target} / {stats.count}</td>" if target else "")
            + "</tr>")
    lines.append("</table>")
    return "".join(lines)


def _events_table(rows, target) -> str:
    head = ("<table><tr><th>A event</th><th>A KP</th><th>B event</th><th>B KP</th>"
            "<th>Match</th><th>ΔKP</th><th>Along</th><th>Cross</th><th>Radial</th>"
            "<th>Bearing</th>" + ("<th>Within</th>" if target else "") + "</tr>")
    body = []
    for row in rows:
        a, b = row.a, row.b
        body.append(
            f"<tr><td>{_esc(a.event if a else '')}</td>"
            f"<td class='n'>{'' if not a or a.kp is None else f'{a.kp:.3f}'}</td>"
            f"<td>{_esc(b.event if b else '')}</td>"
            f"<td class='n'>{'' if not b or b.kp is None else f'{b.kp:.3f}'}</td>"
            f"<td>{_esc(HOW_LABELS.get(row.how, row.how) if row.matched else ('Only in A' if a else 'Only in B'))}</td>"
            f"<td class='n'>{_fmt(row.kp_delta_m, signed=True) if row.matched else ''}</td>"
            f"<td class='n'>{_fmt(row.along_m, signed=True) if row.matched else ''}</td>"
            f"<td class='n'>{_fmt(row.cross_m, signed=True) if row.matched else ''}</td>"
            f"<td class='n'>{_fmt(row.radial_m) if row.matched else ''}</td>"
            f"<td class='n'>{'' if row.bearing_deg is None else f'{row.bearing_deg:.0f}°'}</td>"
            + (f"<td>{_within_html(row, target)}</td>" if target else "") + "</tr>")
    return head + "".join(body) + "</table>"


def _card(row: EventOffset, frame, target, colours, range_m) -> str:
    a, b = row.a, row.b
    title = a.event if a else ""
    svg = radial_plot_svg([row], frame=frame, size=260, target_radius_m=target,
                          colours=colours, label_points=False, show_legend=False,
                          range_m=range_m)
    facts = [
        ("A event", _esc(a.event if a else "")),
        ("B event", _esc(b.event if b else "")),
        ("A KP / B KP", f"{'' if not a or a.kp is None else f'{a.kp:.3f}'} / "
                        f"{'' if not b or b.kp is None else f'{b.kp:.3f}'} km"),
        ("ΔKP (B − A)", _fmt(row.kp_delta_m, signed=True)),
        ("Along-track", _fmt(row.along_m, signed=True)),
        ("Cross-course", _fmt(row.cross_m, signed=True)),
        ("Radial", _fmt(row.radial_m) + (f" @ {row.bearing_deg:.0f}°"
                                          if row.bearing_deg is not None else "")),
    ]
    if row.depth_delta_m is not None:
        facts.append(("Δ depth", _fmt(row.depth_delta_m, signed=True)))
    if target:
        facts.append(("Within target", _within_html(row, target)))
    facts.append(("Match", _esc(HOW_LABELS.get(row.how, row.how))))
    rows_html = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in facts)
    return (f'<div class="card"><b>{_esc(title)}</b>'
            f'<div class="muted">{_esc(a.type_text if a else "")}</div>'
            f"{svg}<table>{rows_html}</table></div>")


def html_report(rows: Sequence[EventOffset], a_label: str, b_label: str,
                frame: str = FRAME_ROUTE, target_radius_m: Optional[float] = None,
                selection_text: str = "", title: str = "RPL event comparison",
                a_detail: str = "", b_detail: str = "", reversed_b: bool = False,
                per_event: bool = True, shared_scale: bool = False) -> str:
    """The full self-contained report for the selected rows."""
    rows = list(rows)
    matched = [r for r in rows if r.matched]
    unmatched = [r for r in rows if not r.matched]
    colours = colour_map(matched)
    stats = offset_stats(rows, target_radius_m)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    frame_text = ("route-relative (up = ahead along A, right = starboard)"
                  if frame != FRAME_NORTH else "north-up (up = north, right = east)")
    range_m = None
    if shared_scale and matched:
        range_m = max((r.radial_m or 0.0) for r in matched)

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body>",
        f"<h1>{_esc(title)}</h1>",
        f"<p class='meta'><b>A (reference):</b> {_esc(a_label)}"
        + (f" &middot; {_esc(a_detail)}" if a_detail else "")
        + f"<br><b>B (compared):</b> {_esc(b_label)}"
        + (f" &middot; {_esc(b_detail)}" if b_detail else "")
        + f"<br>Selection: {_esc(selection_text or 'all events')}"
        + (f" &middot; target radius {target_radius_m:g} m" if target_radius_m else "")
        + f" &middot; generated {generated}</p>",
    ]
    if reversed_b:
        parts.append("<p class='note'>B runs in the opposite direction to A, so ΔKP "
                     "(B's own chainage minus A's) is not reported; along-track and "
                     "cross-course are measured on route A as usual.</p>")
    parts.append("<h2>Summary</h2>")
    parts.append(_stats_tiles(stats))
    if matched:
        parts.append('<div class="figs">')
        parts.append(radial_plot_svg(matched, frame=frame, size=380,
                                     target_radius_m=target_radius_m, colours=colours,
                                     title="All selected events"))
        parts.append("</div>")
        parts.append(kp_offset_chart_svg(matched, target_radius_m=target_radius_m,
                                         colours=colours))
        parts.append("<h2>By event type</h2>")
        parts.append(_type_table(matched, target_radius_m))
    parts.append("<h2>Events</h2>")
    parts.append(_events_table(rows, target_radius_m))
    if per_event and matched:
        parts.append("<h2>Event detail</h2>")
        parts.append('<div class="cards">')
        parts.extend(_card(row, frame, target_radius_m, colours, range_m) for row in matched)
        parts.append("</div>")
    if unmatched:
        parts.append(f"<p class='muted'>{len(unmatched)} selected event(s) have no partner "
                     "and are listed in the table above only.</p>")
    parts.append(
        "<h2>Conventions</h2><ul class='muted'>"
        "<li><b>Along-track</b>: KP on route A of B's event minus A's event KP; "
        "+ ahead (towards increasing KP), − behind.</li>"
        "<li><b>Cross-course</b>: perpendicular distance from route A; + starboard "
        "(right when facing increasing KP), − port.</li>"
        "<li><b>Radial</b>: straight distance between the two events; bearing from A to B.</li>"
        "<li><b>ΔKP</b>: B's own RPL KP minus A's (chainage difference).</li>"
        f"<li>Radial plots are {frame_text}; rings are at round distances, the dashed red "
        "ring is the target radius, + marks the mean offset.</li>"
        "<li>Distances on the WGS84 ellipsoid (local tangent plane per event).</li></ul>")
    parts.append("</body></html>")
    return "".join(parts)
