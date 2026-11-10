"""Input parsing and diagnostics for KP translation (no QGIS dependencies)."""

import csv
import io
import math
import re


def parse_kp(value):
    """Finite decimal kilometres or conventional kilometre+metre notation."""
    text = str(value).strip()
    match = re.fullmatch(r"([+-]?\d+)\+(\d+(?:\.\d+)?)", text)
    if match:
        metres = float(match[2])
        if metres >= 1000:
            raise ValueError("Metres after '+' must be less than 1000")
        value = abs(int(match[1])) + metres / 1000.0
        return -value if match[1].startswith('-') else value
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError("KP must be a number in kilometres") from None
    if not math.isfinite(value):
        raise ValueError("KP must be finite")
    return value


def _kp_heading(value, end=False):
    """Recognise KP headings, not arbitrary text that happens to be invalid data."""
    value = re.sub(r'\([^)]*\)|\[[^]]*\]', '', value.casefold())
    value = re.sub(r'[^a-z0-9]', '', value)
    if value.endswith('km'):
        value = value[:-2]
    if end:
        return value in {'end', 'to', 'endkp', 'kpend', 'tokp', 'kpto', 'endchainage', 'chainageend'}
    return value in {'kp', 'chainage', 'start', 'from', 'startkp', 'kpstart', 'fromkp', 'kpfrom',
                     'startchainage', 'chainagestart', 'kilometrepoint', 'kilometerpoint'}


def pasted_rows(text, ranges=False, header=None, *, details=None):
    """Return column names and raw rows, retaining malformed data for review.

    header=None detects recognised KP headings; True/False force header/no header.
    Invalid first rows are retained unless they match the expected KP headings.
    Point/range type stays explicit so a numeric label is never an end KP.
    """
    text = text.lstrip('\ufeff').strip()
    if not text:
        raise ValueError("Paste at least one KP or range")
    first = text.splitlines()[0]
    delimiter = next((d for d in ('\t', ';', ',', '|') if d in first), None)
    rows = ([list(map(str.strip, r)) for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
            if delimiter else [r.split() for r in text.splitlines()])
    rows = [r for r in rows if any(r)]
    if not rows:
        raise ValueError('Paste at least one KP or range')
    # A one-column point heading can contain spaces without being extra columns.
    if delimiter is None and not ranges and _kp_heading(first):
        rows[0] = [first]
    detected = (_kp_heading(rows[0][0]) and
                (not ranges or (len(rows[0]) > 1 and _kp_heading(rows[0][1], end=True))))
    use_header = detected if header is None else header
    names = rows.pop(0) if use_header else []
    if details is not None:
        details.update(header_used=use_header, header_detected=header is None and detected)
    if not rows:
        raise ValueError("No data rows after the header")
    width = max(2 if ranges else 1, len(names), max(map(len, rows)))
    names += [f"column_{i + 1}" for i in range(len(names), width)]
    return names, [r + [''] * (width - len(r)) for r in rows]


def sample_kps(start, end, step_m, vertices=()):
    """Endpoints, route vertices and evenly spaced stations, in input order."""
    if start == end:
        return [start]
    lo, hi = sorted((start, end))
    count = max(1, math.ceil((hi - lo) * 1000 / step_m))
    if count > 200000:
        raise ValueError("Too many diagnostic samples; increase the sample spacing")
    values = {lo, hi}
    values.update(lo + (hi - lo) * i / count for i in range(1, count))
    values.update(v for v in vertices if lo < v < hi)
    return sorted(values, reverse=start > end)


def mapping_flags(source_kps, target_kps, offsets, max_offset_m, stretch_percent):
    flags = []
    if max(offsets) > max_offset_m:
        flags.append('offset_exceeded')
    if len(source_kps) > 1:
        source_span = source_kps[-1] - source_kps[0]
        target_span = target_kps[-1] - target_kps[0]
        if abs(target_span) < 1e-9:
            flags.append('collapsed')
        elif source_span * target_span < 0:
            flags.append('reversed')
        if abs(abs(target_span) - abs(source_span)) > abs(source_span) * stretch_percent / 100:
            flags.append('length_changed')
        direction = 1 if source_span > 0 else -1
        for a, b, x, y in zip(source_kps, source_kps[1:], target_kps, target_kps[1:]):
            if (y - x) * direction < -1e-6:
                flags.append('non_monotone')
            if abs(y - x) > max(5 * abs(b - a), 0.001):
                flags.append('kp_jump')
    return list(dict.fromkeys(flags))
