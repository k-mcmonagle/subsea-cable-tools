# -*- coding: utf-8 -*-
"""Shared plot/report specification, segmented series and vector page rendering."""
from __future__ import annotations

import csv
import html
import io
import json
import math
from datetime import datetime, timezone

from . import burial_data as data

DEFAULT_TEMPLATE = {
    'name': 'Burial graph', 'title': 'Burial graph', 'width_mm': 297, 'height_mm': 210,
    'margin_mm': 8, 'axis': 'kp', 'page_span': 20, 'timezone': 'UTC',
    'panels': [
        {'channels': ['depth'], 'label': 'Burial Depth (m)', 'height': 2, 'invert': True},
        {'channels': ['pitch', 'roll'], 'label': 'Pitch / Roll (degrees)', 'height': 1},
        {'channels': ['tension'], 'label': 'Tension', 'height': 1}],
    'max_gap_m': 25, 'max_gap_s': 300, 'show_events': True,
}
COLORS = ['#1565c0', '#e65100', '#6a1b9a', '#00897b', '#795548', '#c2185b']


def validate_template(template):
    if template.get('axis') not in ('kp', 'time'):
        raise ValueError('Plot axis must be KP or time')
    w, h, margin = (float(template[k]) for k in ('width_mm', 'height_mm', 'margin_mm'))
    if min(w, h) < 100 or margin < 0 or margin > min(w, h) / 4:
        raise ValueError('Page size/margins leave insufficient plot space')
    panels = template.get('panels', [])
    if not 1 <= len(panels) <= 8:
        raise ValueError('Choose between one and eight plot panels')
    for panel in panels:
        if not panel.get('channels') or float(panel.get('height', 1)) <= 0:
            raise ValueError('Each panel needs channels and a positive height')
        if panel.get('min') is not None and panel.get('max') is not None and panel['min'] >= panel['max']:
            raise ValueError('Panel maximum must exceed minimum')
    if float(template.get('page_span', 0)) < 0:
        raise ValueError('Page span cannot be negative')
    data.time_zone(template.get('timezone', 'UTC'))


def series(rows, template, label=''):
    """Identical series for interactive and export renderers. Never bridge gaps."""
    axis = template['axis']
    result = []
    channels = list(dict.fromkeys(c for p in template['panels'] for c in p['channels']))
    runs = data.continuous_runs(rows, template.get('max_gap_m', 25), template.get('max_gap_s', 300))
    colors = {}
    for run in runs:
        # Composite samples can change source inside one interval grid. Retain run identity.
        parts = []
        for row in run:
            identity = (row.get('processing_id'), row.get('pass_id'), row.get('run_id'))
            if not parts or parts[-1][0] != identity:
                parts.append((identity, []))
            parts[-1][1].append(row)
        for identity, part in parts:
            for channel in channels:
                color = colors.setdefault((*identity[:2], channel), COLORS[len(colors) % len(COLORS)])
                x, y, ids = [], [], []
                for row in part:
                    xx = row.get('time') if axis == 'time' else row.get('kp')
                    yy = row['channels'].get(channel)
                    x.append(xx)
                    y.append(yy)
                    ids.append(row.get('observation_id', ''))
                result.append(dict(channel=channel, label=label or part[0].get('pass_name', ''),
                                   color=color, x=x, y=y, ids=ids))
    if template.get('show_events', True):
        seen = set()
        for row in rows:
            at = row.get('time') if axis == 'time' else row.get('kp')
            key = (at, row.get('event'))
            if at is not None and row.get('event') and key not in seen:
                seen.add(key)
                result.append(dict(channel='event', label=row['event'], color='#666666', x=[at], y=[0], ids=[], event=True))
    return result


def target_series(rows, template):
    result = []
    for run in data.continuous_runs(rows, template.get('max_gap_m', 25), template.get('max_gap_s', 300)):
        # Step target over each station's supported interval, not between passes.
        x, y = [], []
        for row in run:
            if template['axis'] == 'kp' and row.get('target_segments'):
                for start, end, depth in row['target_segments']:
                    x.extend([start, end])
                    y.extend([depth, depth])
                continue
            at = row.get('time') if template['axis'] == 'time' else row.get('kp')
            x.append(at)
            y.append(row.get('target'))
            if template['axis'] == 'kp':
                x.append(row.get('support_end_kp', at))
                y.append(row.get('target'))
        result.append(dict(channel='depth', label='Target', color='#c62828', x=x, y=y, ids=[], dashed=True))
    return result


def plan_series(plan, template):
    if template['axis'] != 'kp':
        return []
    params = json.loads(plan.get('params_json') or '{}')
    return [dict(channel='plan', label='Proposed remedial range', color='#eceff1', x=[a, b], y=[0, 0], ids=[], planned=True)
            for a, b in params.get('remedial', {}).get('ranges', [])]


def bounds(plot_series):
    xs = [v for s in plot_series for v in s['x'] if v is not None and math.isfinite(v)]
    if not xs:
        raise ValueError('No supported data for the selected plot/filter')
    lo, hi = min(xs), max(xs)
    return lo, hi if hi > lo else lo + 0.001


def pages(plot_series, template, extent=None):
    validate_template(template)
    lo, hi = extent or bounds(plot_series)
    span = float(template.get('page_span', 0))
    if template['axis'] == 'time':
        span *= 3600  # UI units are hours for the time axis, km otherwise.
    span = span or hi - lo
    count = max(1, math.ceil((hi - lo) / span - 1e-9))
    if count > 1000:
        raise ValueError('More than 1000 pages requested; increase page coverage')
    return [(lo + i * span, min(hi, lo + (i + 1) * span)) for i in range(count)]


def y_bounds(plot_series, panel, extent):
    values = [y for s in plot_series if s['channel'] in panel['channels']
              for x, y in zip(s['x'], s['y']) if x is not None and y is not None]
    lo, hi = (min(values), max(values)) if values else (0, 1)
    padding = max((hi - lo) * 0.08, 0.05)
    return panel.get('min') if panel.get('min') is not None else lo - padding, \
        panel.get('max') if panel.get('max') is not None else hi + padding


def tick(value, template):
    if template['axis'] == 'time':
        return datetime.fromtimestamp(value, timezone.utc).astimezone(
            data.time_zone(template.get('timezone', 'UTC'))).strftime('%d %b %H:%M')
    return f'{value:.3f}'


def svg_page(plot_series, template, extent, subtitle='', footer='', page=1, page_count=1):
    """Complete vector page in physical mm units, aligned plotting rectangles."""
    validate_template(template)
    w, h, m = (float(template[k]) for k in ('width_mm', 'height_mm', 'margin_mm'))
    left, right, top, bottom = m + 20, w - m - 2, m + 20, h - m - 16
    esc = lambda s: html.escape(str(s), quote=True)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">',
           f'<rect width="{w}" height="{h}" fill="white"/>',
           '<g font-family="Arial, sans-serif" font-size="2.7" fill="#222">',
           f'<text x="{m}" y="{m+4}" font-size="4.5">{esc(template.get("title", "Burial graph"))}</text>',
           f'<text x="{m}" y="{m+10}">{esc(subtitle)}</text>']
    total = sum(float(p.get('height', 1)) for p in template['panels'])
    available = bottom - top - 9 * (len(template['panels']) - 1)
    x0, x1 = extent
    x1 = max(x1, x0 + 1e-9)
    px = lambda x: left + (x - x0) / (x1 - x0) * (right - left)
    ytop = top
    for index, panel in enumerate(template['panels']):
        ph = available * float(panel.get('height', 1)) / total
        ymin, ymax = y_bounds(plot_series, panel, extent)
        ymax = max(ymax, ymin + 1e-9)
        def py(y, ymin=ymin, ymax=ymax, ytop=ytop, ph=ph, invert=panel.get('invert')):
            fraction = (y - ymin) / (ymax - ymin)
            return ytop + ph * (fraction if invert else 1 - fraction)
        out.append(f'<text x="{left}" y="{ytop-2}">{esc(panel.get("label", ", ".join(panel["channels"])))}</text>')
        out.append(f'<defs><clipPath id="p{index}"><rect x="{left}" y="{ytop}" width="{right-left}" height="{ph}"/></clipPath></defs>')
        if index == 0:
            for item in plot_series:
                if item.get('planned'):
                    a, b = max(x0, item['x'][0]), min(x1, item['x'][1])
                    if b > a:
                        out.append(f'<rect x="{px(a)}" y="{ytop}" width="{px(b)-px(a)}" height="{ph}" fill="#eceff1"/>')
        for j in range(5):
            value = ymin + j * (ymax - ymin) / 4
            yy = py(value)
            out.extend([f'<path d="M{left},{yy} H{right}" fill="none" stroke="#ddd" stroke-width="0.15"/>',
                        f'<text x="{left-2}" y="{yy+1}" text-anchor="end">{value:.2f}</text>'])
        for j in range(6):
            value = x0 + j * (x1 - x0) / 5
            xx = px(value)
            out.append(f'<path d="M{xx},{ytop} V{ytop+ph}" fill="none" stroke="#ddd" stroke-width="0.15"/>')
            if index == len(template['panels']) - 1:
                out.append(f'<text x="{xx}" y="{ytop+ph+5}" text-anchor="middle">{esc(tick(value, template))}</text>')
        if template.get('show_events', True):
            for item in plot_series:
                if item.get('event') and x0 <= item['x'][0] <= x1:
                    xx = px(item['x'][0])
                    out.append(f'<path d="M{xx},{ytop} V{ytop+ph}" stroke="#999" stroke-width="0.2" stroke-dasharray="1,1"/>')
                    if index == 0:
                        out.append(f'<text x="{xx+1}" y="{ytop+3}" font-size="2">{esc(item["label"][:40])}</text>')
        legends = []
        for item in plot_series:
            if item['channel'] not in panel['channels']:
                continue
            segments, points = [], []
            for xx, yy in zip(item['x'], item['y']):
                if xx is None or yy is None:
                    if points:
                        segments.append(points)
                    points = []
                else:
                    points.append((px(xx), py(yy)))
            if points:
                segments.append(points)
            dash = ' stroke-dasharray="1.5,1"' if item.get('dashed') else ''
            for points in segments:
                points = compact_points(points, left, right)
                if not points:
                    continue
                if len(points) == 1:
                    out.append(f'<circle cx="{points[0][0]:.4f}" cy="{points[0][1]:.4f}" r="0.45" fill="{item["color"]}" clip-path="url(#p{index})"/>')
                else:
                    coords = ' '.join(f'{x:.4f},{y:.4f}' for x, y in points)
                    out.append(f'<polyline points="{coords}" fill="none" stroke="{item["color"]}" stroke-width="0.3"{dash} clip-path="url(#p{index})"/>')
            label = f'{item["label"]} {data.CHANNELS.get(item["channel"], item["channel"])}'
            if label not in [v[0] for v in legends]:
                legends.append((label, item['color']))
        legend = ''.join(f'<tspan fill="{color}">{esc(label[:45])} | </tspan>' for label, color in legends[:5])
        out.append(f'<text x="{right}" y="{ytop-2}" text-anchor="end" font-size="2.1">{legend}</text>')
        ytop += ph + 9
    axis_label = 'Design KP (km)' if template['axis'] == 'kp' else 'Time (' + template.get('timezone', 'UTC') + ')'
    out.append(f'<text x="{(left+right)/2}" y="{h-m-5}" text-anchor="middle">{esc(axis_label)}</text>')
    out.append(f'<text x="{m}" y="{h-m+1}" font-size="2">{esc(footer)}</text>')
    out.append(f'<text x="{w-m}" y="{h-m+1}" text-anchor="end">{page}/{page_count}</text>')
    out.append('</g></svg>')
    return '\n'.join(out)


def listing_csv(rows, channels=None):
    channels = channels or list(dict.fromkeys(c for r in rows for c in r['channels']))
    headers = ['KP_km', 'ISO_Time', *channels, 'target', 'shortfall', 'assessment', 'pass_name', 'pass_id',
               'source_file', 'import_id', 'processing_id', 'definition', 'method', 'interpolated',
               'support_end_kp', 'target_segments', 'selection', 'flags', 'contributor_ids']
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(headers)
    for row in rows:
        values = dict(row, **row['channels'], KP_km=row['kp'], ISO_Time=data.iso(row.get('time')))
        writer.writerow([data.serialise(values.get(k)) if isinstance(values.get(k), (dict, list)) else values.get(k, '')
                         for k in headers])
    return out.getvalue()


def compact_points(points, left, right, bins=1600):
    """Clip to a page, retaining extrema/first/last per display bucket in order.

    Used only for rendering; observations, statistics and listings stay full
    resolution. Adjacent outside points are retained to clip crossing segments.
    """
    if not points:
        return []
    visible = [i for i, (x, _) in enumerate(points) if left <= x <= right]
    if visible:
        points = points[max(0, visible[0] - 1):visible[-1] + 2]
    elif len(points) >= 2 and min(points[0][0], points[-1][0]) <= left and max(points[0][0], points[-1][0]) >= right:
        # Monotonic continuous run crosses the whole page with sparse samples.
        points = [points[0], points[-1]]
    else:
        return []
    if len(points) <= bins * 4:
        return points
    output, bucket, key = [], [], None
    def flush(values):
        if not values:
            return []
        indexes = sorted({0, len(values)-1, min(range(len(values)), key=lambda i: values[i][1]),
                          max(range(len(values)), key=lambda i: values[i][1])})
        return [values[i] for i in indexes]
    for point in points:
        current = int((point[0] - left) / max(1e-9, right-left) * bins)
        if key is not None and current != key:
            output.extend(flush(bucket))
            bucket = []
        key = current
        bucket.append(point)
    output.extend(flush(bucket))
    return output


def view_extent(snapshot):
    """Honour explicit daily/distance windows, including unsupported edges."""
    query = snapshot['query']
    keys = ('time_start', 'time_end') if snapshot['template']['axis'] == 'time' else ('kp_start', 'kp_end')
    lo, hi = bounds(snapshot['plots'])
    lo = query.get(keys[0], lo)
    hi = query.get(keys[1], hi)
    return lo, hi if hi > lo else lo + .001
