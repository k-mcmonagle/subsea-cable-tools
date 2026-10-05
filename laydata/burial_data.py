# -*- coding: utf-8 -*-
"""Portable acquired burial data and pass processing; no QGIS/Qt dependency.

Original rows and channel definitions belong to imports. Processing returns new
rows; edits are recipes, never modifications of imported measurements.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import io
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

VERSION = 1
CHANNELS = {'depth': 'Burial Depth', 'pitch': 'Pitch', 'roll': 'Roll', 'tension': 'Tension'}


def serialise(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(serialise(value).encode('utf-8')).hexdigest()


def number(value):
    try:
        value = float(str(value).strip().replace(',', '.'))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def time_zone(name='UTC'):
    name = str(name or 'UTC').strip()
    if name.upper() in ('UTC', 'Z', 'GMT'):
        return timezone.utc
    if name.startswith(('UTC+', 'UTC-', '+', '-')):
        value = name.removeprefix('UTC')
        sign = -1 if value.startswith('-') else 1
        parts = value[1:].split(':')
        hours, minutes = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        if hours > 23 or minutes > 59:
            raise ValueError('Invalid UTC offset')
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def epoch(value, spec=None):
    """UTC seconds. Reject ambiguous/nonexistent local times unless offset supplied."""
    spec = spec or {}
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    fmt = spec.get('time_format', 'ISO')
    if fmt == 'epoch':
        return number(value)
    if fmt == 'day,time':
        day, clock = text.split(',', 1)
        origin = datetime.fromisoformat(spec['start_date'])
        hh, mm, ss = map(float, clock.split(':'))
        dt = origin + timedelta(days=int(day) - 1, hours=hh, minutes=mm, seconds=ss)
    elif fmt and fmt != 'ISO':
        dt = datetime.strptime(text, fmt)
    else:
        dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        zone = time_zone(spec.get('timezone', 'UTC'))
        first, second = dt.replace(tzinfo=zone, fold=0), dt.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError('Ambiguous or nonexistent local time; supply a UTC offset')
        dt = first
    return dt.astimezone(timezone.utc).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z') if value is not None else ''


def read_csv(content: bytes, spec=None):
    spec = spec or {}
    text = content.decode(spec.get('encoding', 'utf-8-sig'))
    delimiter = spec.get('delimiter')
    if not delimiter:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=',;\t|').delimiter
        except csv.Error:
            delimiter = ','
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    skip = int(spec.get('header_rows', 0))
    for _ in range(skip):
        next(reader, None)
    headers = [str(h).strip() for h in next(reader, [])]
    if not headers or len(headers) != len(set(headers)):
        raise ValueError('The header must contain unique column names')
    rows = []
    for index, values in enumerate(reader, start=skip + 2):
        if index == skip + 2 and spec.get('units_row'):
            continue
        if any(str(v).strip() for v in values):
            if len(values) != len(headers):
                rows.append({'_source_row': index, '_error': 'Column count differs from header', '_values': values})
            else:
                rows.append(dict(zip(headers, values), _source_row=index))
    return headers, rows


def coordinate(value):
    """Decimal degrees or the existing plough degrees/decimal-minutes convention."""
    numeric = number(value)
    if numeric is not None:
        return numeric
    text = str(value or '').strip().upper().replace('°', ' ').replace("'", ' ').replace('"', ' ')
    sign = -1 if text.endswith(('S', 'W')) or text.startswith('-') else 1
    parts = text.rstrip('NSEW').strip().lstrip('+-').split()
    try:
        result = float(parts[0]) + float(parts[1]) / 60
        if len(parts) > 2:
            result += float(parts[2]) / 3600
        return sign * result
    except (ValueError, IndexError):
        return None


def normalise(rows, spec):
    mapping = spec.get('mapping', {})
    if not mapping.get('depth'):
        raise ValueError('Map a burial depth channel')
    if not mapping.get('kp') and not (mapping.get('x') and mapping.get('y')):
        raise ValueError('Map KP or both position columns')
    if not str(spec.get('definition', '')).strip():
        raise ValueError('Describe what the depth measurement represents')
    output = []
    nulls = {str(v).strip() for v in spec.get('null_values', ['', 'NULL', 'NaN', '-9999'])}
    for index, raw in enumerate(rows):
        flags = []
        if raw.get('_error'):
            flags.append(raw['_error'])
        values = {}
        for channel, field in mapping.items():
            value = raw.get(field)
            if str(value).strip() in nulls:
                value = None
            values[channel] = value
        t = None
        try:
            t = epoch(values.get('time'), spec)
        except (ValueError, KeyError, OverflowError):
            flags.append('invalid_time')
        channels = {}
        for key in mapping:
            if key not in ('time', 'kp', 'x', 'y', 'pass', 'state', 'event'):
                v = number(values.get(key))
                factor = float(spec.get('factors', {}).get(key, 1.0))
                channels[key] = v * factor if v is not None else None
        if channels.get('depth') is None:
            flags.append('missing_depth')
        kp = number(values.get('kp'))
        if kp is not None:
            kp *= float(spec.get('kp_factor', 1.0))
        point_fn = coordinate if spec.get('crs', 'EPSG:4326') == 'EPSG:4326' else number
        output.append(dict(source_row=raw.get('_source_row', index + 1), time=t,
                           original_kp=kp, kp=kp, x=point_fn(values.get('x')), y=point_fn(values.get('y')),
                           channels=channels, pass_name=str(values.get('pass') or ''),
                           state=str(values.get('state') or 'burial').lower(), event=str(values.get('event') or ''),
                           flags=flags, raw=raw))
    return output


def _cancel(check):
    if check and check():
        raise InterruptedError('Cancelled; no revision was saved')


def process(observations, recipe, projector: Optional[Callable] = None, cancelled=None, progress=None):
    """New pass revision. Projector(x, y) -> (kp, cross-track metres, flags)."""
    if not recipe.get('route', {}).get('fingerprint'):
        raise ValueError('Select and identify a design RPL before processing')
    mode = recipe.get('kp_mode', 'position')
    if mode == 'position' and projector is None:
        raise ValueError('Position processing requires a route projector')
    if mode == 'supplied' and not recipe.get('supplied_kp_confirmed'):
        raise ValueError('Confirm that supplied KPs use this design RPL')
    gap = float(recipe.get('time_gap_s', 300))
    reverse = float(recipe.get('reversal_m', 10)) / 1000
    max_step = float(recipe.get('max_step_m', 0)) / 1000
    max_offset = float(recipe.get('max_offset_m', 0))
    window = int(recipe.get('smooth_window', 1))
    if gap <= 0 or reverse <= 0 or window < 1 or window > 301 or window % 2 == 0:
        raise ValueError('Positive gap/reversal and an odd smoothing window from 1 to 301 are required')
    exclusions = set(recipe.get('exclude_ids', []))
    edits = recipe.get('edits', [])
    if (edits or exclusions or recipe.get('time_offset_s') or window > 1) and not str(recipe.get('reason', '')).strip():
        raise ValueError('Enter a reason for corrections or exclusions')
    ordered = sorted(observations, key=lambda r: (r['time'] is None, r['time'] or 0, r['source_row']))
    # Smooth positions only inside the same explicitly labelled, continuous operation.
    groups = []
    for row in ordered:
        if (not groups or row['pass_name'] != groups[-1][-1]['pass_name'] or
                row['state'] != groups[-1][-1]['state'] or
                (row['time'] is None) != (groups[-1][-1]['time'] is None) or
                row['time'] is not None and row['time'] - groups[-1][-1]['time'] > gap):
            groups.append([])
        groups[-1].append(row)
    result = []
    pass_count = 0
    for group in groups:
        pass_count += 1
        direction, extremum, prev = 0, None, None
        for i, raw in enumerate(group):
            _cancel(cancelled)
            row = {k: v for k, v in raw.items() if k != 'raw'}
            row['flags'] = list(raw['flags'])
            row['channels'] = dict(raw['channels'])
            row['time'] = raw['time'] + float(recipe.get('time_offset_s', 0)) if raw['time'] is not None else None
            row['original_x'], row['original_y'] = raw['x'], raw['y']
            row['original_channels'] = dict(raw['channels'])
            if window > 1 and raw['x'] is not None and raw['y'] is not None:
                around = group[max(0, i - window // 2):i + window // 2 + 1]
                for axis in ('x', 'y'):
                    vals = [r[axis] for r in around if r[axis] is not None]
                    row[axis] = statistics.median(vals)
                row['flags'].append('position_filtered')
            row['cross_track_m'] = None
            if mode == 'position':
                row['kp'] = None
                if row['x'] is not None and row['y'] is not None:
                    row['kp'], row['cross_track_m'], flags = projector(row['x'], row['y'])
                    row['flags'].extend(flags)
            else:
                row['kp'] = row['original_kp']
            if row['kp'] is None:
                row['flags'].append('missing_kp')
            elif max_offset and row['cross_track_m'] is not None and abs(row['cross_track_m']) > max_offset:
                row['flags'].append('off_route')
            if prev and row['kp'] is not None and prev['kp'] is not None:
                delta = row['kp'] - prev['kp']
                if max_step and abs(delta) > max_step:
                    row['flags'].append('kp_jump')
                if extremum is None:
                    extremum = prev['kp']
                if not direction and abs(row['kp'] - extremum) >= reverse:
                    direction = 1 if row['kp'] > extremum else -1
                elif direction and direction * (row['kp'] - extremum) < -reverse:
                    pass_count += 1
                    direction = -direction
                    extremum = row['kp']
                    row['flags'].append('reversal')
                if direction:
                    extremum = max(extremum, row['kp']) if direction > 0 else min(extremum, row['kp'])
            row['pass_id'] = f"{raw.get('import_id', 'import')}:{pass_count}"
            row['pass_name'] = raw['pass_name'] or f'Pass {pass_count}'
            row['working'] = row['state'] in recipe.get('working_states', ['burial', 'working', '1'])
            row['excluded'] = raw.get('observation_id') in exclusions
            for edit in edits:
                if (row['kp'] is not None and float(edit['start_kp']) <= row['kp'] < float(edit['end_kp'])
                        and (not edit.get('pass_id') or edit['pass_id'] == row['pass_id'])
                        and (edit.get('time_start') is None or row['time'] is not None and row['time'] >= edit['time_start'])
                        and (edit.get('time_end') is None or row['time'] is not None and row['time'] < edit['time_end'])):
                    if edit.get('exclude'):
                        row['excluded'] = True
                    if edit.get('pass_name'):
                        row['pass_id'] = f"manual:{edit['pass_name']}"
                        row['pass_name'] = edit['pass_name']
                    channel = edit.get('channel', 'depth')
                    if row['channels'].get(channel) is not None and edit.get('offset'):
                        row['channels'][channel] += float(edit['offset'])
                        row['flags'].append('corrected')
            row['valid'] = (row['kp'] is not None and row['channels'].get('depth') is not None
                            and row['working'] and not row['excluded'] and not
                            set(row['flags']).intersection({'kp_jump', 'off_route', 'ambiguous_kp', 'invalid_time'}))
            result.append(row)
            prev = row
            if progress and len(result) % 1000 == 0:
                progress(100 * len(result) / max(1, len(ordered)))
    return result


def select_rows(rows, query):
    start, end = query.get('time_start'), query.get('time_end')
    lo, hi = query.get('kp_start'), query.get('kp_end')
    if start is not None and end is not None and start >= end:
        raise ValueError('Reporting end must be after start')
    if lo is not None and hi is not None and lo >= hi:
        raise ValueError('End KP must exceed start KP')
    passes = query.get('passes', [])
    result = []
    for row in rows:
        t, kp = row.get('time'), row.get('kp')
        if start is not None and (t is None or t < start):
            continue
        if end is not None and (t is None or t >= end):
            continue
        if lo is not None and (kp is None or kp < lo):
            continue
        if hi is not None and (kp is None or kp > hi):
            continue
        if passes and row.get('pass_id') not in passes:
            continue
        result.append(row)
    return result


def continuous_runs(rows, max_gap_m=25, max_gap_s=300, valid_only=True):
    """Keep acquisition order and break on invalid records, passes, gaps, reversals."""
    grouped = defaultdict(list)
    for row in rows:
        pass_id = row.get('pass_id', '')
        # An explicit shared manual pass name can stitch compatible deliveries.
        identity = '' if pass_id.startswith('manual:') else row.get('processing_id', '')
        grouped[(identity, pass_id, row.get('definition', ''))].append(row)
    runs = []
    for group in grouped.values():
        group.sort(key=lambda r: (r.get('time') is None, r.get('time') or 0, r.get('source_row', 0)))
        run, direction = [], 0
        for row in group:
            if row.get('kp') is None or (valid_only and not row.get('valid')):
                if run:
                    runs.append(run)
                run, direction = [], 0
                continue
            split = False
            if run:
                prev = run[-1]
                delta = row['kp'] - prev['kp']
                dt = abs(row['time'] - prev['time']) if row.get('time') is not None and prev.get('time') is not None else 0
                split = abs(delta) * 1000 > max_gap_m or dt > max_gap_s
                if row.get('support_end_kp') is not None and prev.get('support_end_kp') is not None:
                    lower = prev if delta >= 0 else row
                    split = split or lower['support_end_kp'] < max(row['kp'], prev['kp']) - 1e-9
                    split = split or row.get('run_id') != prev.get('run_id')
                if abs(delta) > 1e-9:
                    sign = 1 if delta > 0 else -1
                    split = split or (direction and sign != direction)
                    direction = sign
            if split:
                runs.append(run)
                run = []
            run.append(row)
        if run:
            runs.append(run)
    return runs


def stations(rows, query, interval_m=1.0, max_gap_m=25, max_gap_s=300, method='sample', cancelled=None):
    """Per-run station samples or half-open interval summaries, with support bounds."""
    if interval_m <= 0 or max_gap_m <= 0 or max_gap_s <= 0 or method not in ('sample', 'mean', 'minimum', 'median'):
        raise ValueError('Positive station/gap settings and a valid listing method are required')
    selected = select_rows(rows, query)
    result = []
    for run_index, run in enumerate(continuous_runs(selected, max_gap_m, max_gap_s)):
        _cancel(cancelled)
        run = sorted(run, key=lambda r: (r['kp'], r.get('time') or 0))
        # Repeated stationary positions contribute one measurement to station interpolation.
        by_kp = {r['kp']: r for r in run}
        ordered = list(by_kp.values())
        xs = list(by_kp)
        first = math.ceil((xs[0] * 1000 - 1e-7) / interval_m)
        last = math.floor((xs[-1] * 1000 + 1e-7) / interval_m)
        if last - first > 2000000:
            raise ValueError('Listing exceeds two million stations; narrow the range or increase spacing')
        for index in range(first, last + 1):
            if index % 1000 == 0:
                _cancel(cancelled)
            kp = index * interval_m / 1000
            right = min(bisect.bisect_left(xs, kp), len(xs) - 1)
            left = right if abs(xs[right] - kp) < 1e-9 else max(0, right - 1)
            a, b = ordered[left], ordered[right]
            fraction = (kp - xs[left]) / (xs[right] - xs[left]) if right != left else 0
            channels = {}
            chosen = a if fraction < 0.5 else b
            contributors = [a] if a is b else [a, b]
            support_end = min(kp + interval_m / 1000, xs[-1])
            if method != 'sample':
                stop = bisect.bisect_left(xs, kp + interval_m / 1000 - 1e-10)
                start = bisect.bisect_left(xs, kp - 1e-10)
                contributors = ordered[start:stop]
                if not contributors:
                    continue
                chosen = max(contributors, key=lambda r: r.get('time') or 0)
            keys = set().union(*(r['channels'] for r in contributors))
            for channel in keys:
                if method == 'sample':
                    va, vb = a['channels'].get(channel), b['channels'].get(channel)
                    channels[channel] = va + fraction * (vb - va) if va is not None and vb is not None else None
                else:
                    values = [r['channels'][channel] for r in contributors if r['channels'].get(channel) is not None]
                    fn = {'mean': statistics.mean, 'minimum': min, 'median': statistics.median}[method]
                    channels[channel] = fn(values) if values else None
            if channels.get('depth') is None:
                continue
            stamp = chosen.get('time')
            if method == 'sample' and a.get('time') is not None and b.get('time') is not None:
                stamp = a['time'] + fraction * (b['time'] - a['time'])
            row = {k: v for k, v in chosen.items() if k not in ('channels', 'raw', 'original_channels')}
            row.update(kp=kp, time=stamp, channels=channels, support_end_kp=support_end,
                       run_id=f"{chosen.get('processing_id', '')}:{run_index}",
                       interpolated=(method == 'sample' and left != right), count=len(contributors),
                       contributor_ids=[r.get('observation_id', '') for r in contributors], method=method)
            result.append(row)
    return result


def composite(samples, overrides=(), definition=None):
    """Latest acquired compatible measurement, with explicit half-open KP overrides."""
    groups = defaultdict(list)
    for row in samples:
        groups[round(row['kp'], 9)].append(row)
    result = []
    for kp, candidates in sorted(groups.items()):
        selected = [r for r in candidates if definition is None or r.get('definition') == definition]
        rule = next((o for o in reversed(list(overrides)) if o['start_kp'] <= kp < o['end_kp']), None)
        if rule:
            selected = [r for r in selected if r['pass_id'] == rule['pass_id'] and
                        (not rule.get('processing_id') or r.get('processing_id') == rule['processing_id'])]
        if not selected:
            continue
        row = dict(max(selected, key=lambda r: (r.get('time') is not None, r.get('time') or 0,
                                                r.get('processing_id', ''), r.get('source_row', 0))))
        row['selection'] = 'manual' if rule else 'latest acquisition'
        row['alternatives'] = len(candidates)
        result.append(row)
    return result


def assess(samples, target: Callable, scope=None, boundaries=()):
    """Length-weighted assessment of a unique composite; unknown is not failure."""
    lengths = defaultdict(float)
    ranges = []
    previous = None
    support = []
    for row in sorted(samples, key=lambda r: r['kp']):
        kp = row['kp']
        end = max(kp, row.get('support_end_kp', kp))
        if scope:
            kp, end = max(kp, scope[0]), min(end, scope[1])
        length = max(0, end - kp)
        value = row['channels'].get('depth')
        required = target(row['kp'])
        row['target'] = required
        row['shortfall'] = max(0, required - value) if required is not None else None
        row['assessment'] = 'unknown target' if required is None else ('met' if value >= required else 'shortfall')
        if not length:
            continue
        support.append((kp, end))
        cuts = [kp, *sorted(v for v in boundaries if kp < v < end), end]
        row['target_segments'] = []
        for start, stop in zip(cuts, cuts[1:]):
            required = target((start + stop) / 2)
            row['target_segments'].append([start, stop, required])
            status = 'unknown target' if required is None else ('met' if value >= required else 'shortfall')
            lengths[status] += stop - start
            shortfall = max(0, required - value) if required is not None else 0
            if status == 'shortfall':
                if previous and abs(previous['end_kp'] - start) < 1e-8:
                    previous['end_kp'] = stop
                    previous['max_shortfall_m'] = max(previous['max_shortfall_m'], shortfall)
                else:
                    previous = dict(start_kp=start, end_kp=stop, max_shortfall_m=shortfall, kind='shortfall')
                    ranges.append(previous)
            else:
                previous = None
    if scope:
        cursor = scope[0]
        for start, end in support:
            if start > cursor + 1e-8:
                ranges.append(dict(start_kp=cursor, end_kp=start, max_shortfall_m=None, kind='missing evidence'))
            cursor = max(cursor, end)
        if cursor < scope[1] - 1e-8:
            ranges.append(dict(start_kp=cursor, end_kp=scope[1], max_shortfall_m=None, kind='missing evidence'))
    ranges.sort(key=lambda r: r['start_kp'])
    covered = sum(lengths.values())
    total = scope[1] - scope[0] if scope else covered
    return dict(covered_km=covered, met_km=lengths['met'], shortfall_km=lengths['shortfall'],
                unknown_target_km=lengths['unknown target'], missing_km=max(0, total - covered), ranges=ranges)


def improvement(before, after):
    lookup = {round(r['kp'], 9): r for r in before}
    result = []
    for row in after:
        old = lookup.get(round(row['kp'], 9))
        if old and old.get('definition') == row.get('definition'):
            result.append(dict(kp=row['kp'], improvement_m=row['channels']['depth'] - old['channels']['depth'],
                               before=old.get('contributor_ids', []), after=row.get('contributor_ids', [])))
    return result
