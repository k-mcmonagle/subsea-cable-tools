# -*- coding: utf-8 -*-
"""Shared seabed-slope math (pure Python, QGIS-free, NumPy optional).

One home for the slope calculations that were previously duplicated across
the KP Mouse live profile, the Depth Profile tool, the KP Range Depth + Slope
Summary algorithm, the Burial Planner and the Workbench rules engine.

Plugin-wide conventions (README "Slope methodology"):

- Slope is measured **along the profile line** in degrees:
  ``atan2(Δup, Δchainage)``. Positive = shoaling with increasing
  KP/chainage (up-slope).
- The seabed datum is auto-detected where the caller does not know it:
  positive-down depths vs negative elevations (:func:`should_invert_depth_axis`).
- Two differencing schemes exist on purpose:

  * :func:`interval_slope_series` — raw slope of each sampling interval.
    Right when the station spacing *is* the analysis scale (a user-chosen
    sampling interval).
  * :func:`windowed_slope_series` — central difference of depths linearly
    interpolated at ``x ± half_window`` (the Burial Planner / Workbench
    method). Right when stations are irregular, or denser than the data
    resolution (e.g. sub-cell sampling of a coarse MBES grid), where raw
    interval differences turn each cell step into a near-vertical spike.
"""

from __future__ import annotations

import bisect
import math
from typing import List, Optional, Sequence, Tuple

try:
    import numpy as _np
except Exception:  # pragma: no cover - NumPy ships with QGIS
    _np = None


def should_invert_depth_axis(values: Sequence[Optional[float]]) -> Optional[bool]:
    """True when depths are positive-down (invert so deeper plots lower),
    False for negative elevations, None when there is no data to judge."""
    finite = [value for value in values if is_finite(value)]
    if not finite:
        return None
    return sorted(finite)[len(finite) // 2] > 0


def datum_sign(values: Sequence[Optional[float]]) -> float:
    """+1.0 for positive-down depth data, -1.0 for negative elevations.

    Depth differences are normalised onto a positive-down basis before the
    plugin-wide up-slope-positive sign is applied. Unknown data (empty
    series) is treated as positive-down.
    """
    return -1.0 if should_invert_depth_axis(values) is False else 1.0


def interval_slope_series(x_values: Sequence[float],
                          y_values: Sequence[Optional[float]],
                          positive_down: Optional[bool] = None
                          ) -> List[Optional[float]]:
    """Per-interval slope in degrees at each station.

    ``x`` in metres, values in metres. Positive = shoaling along the line
    (up-slope) regardless of the source datum. ``positive_down`` overrides
    the datum; when None it is auto-detected (median sign). ``None`` marks
    the first station (no preceding interval) and any interval with a
    missing endpoint.
    """
    if not x_values:
        return []
    if positive_down is None:
        positive_down = should_invert_depth_axis(y_values)
    # Elevation data already carries the up-slope-positive sign; positive-down
    # depth data needs the difference negated.
    sign = 1.0 if positive_down is False else -1.0
    slopes: List[Optional[float]] = [None]
    for index in range(1, len(x_values)):
        dx = x_values[index] - x_values[index - 1]
        v1 = y_values[index - 1] if index - 1 < len(y_values) else None
        v2 = y_values[index] if index < len(y_values) else None
        if not is_finite(dx) or dx <= 0 or not is_finite(v1) or not is_finite(v2):
            slopes.append(None)
            continue
        slopes.append(math.degrees(math.atan2(sign * (v2 - v1), dx)))
    return slopes


def _interp(xs: List[float], ys: List[float], x: float) -> Optional[float]:
    """Linear interpolation; None outside the sampled range."""
    if not xs or x < xs[0] - 1e-9 or x > xs[-1] + 1e-9:
        return None
    index = bisect.bisect_left(xs, x)
    if index < len(xs) and abs(xs[index] - x) <= 1e-9:
        return ys[index]
    if index == 0 or index >= len(xs):
        return None
    x0, x1 = xs[index - 1], xs[index]
    if x1 - x0 <= 1e-12:
        return ys[index]
    t = (x - x0) / (x1 - x0)
    return ys[index - 1] + t * (ys[index] - ys[index - 1])


def windowed_slope_series(x_values: Sequence[float],
                          y_values: Sequence[Optional[float]],
                          half_window: float,
                          x_units_m: float = 1.0,
                          positive_down: Optional[bool] = True,
                          degenerate: Optional[float] = None,
                          mask_missing: bool = False) -> List[Optional[float]]:
    """Fixed-baseline slope within contiguous finite coverage, never across gaps.

    At outer edges the complete window shifts inward. A run shorter than
    the requested full baseline is unsupported, not silently shortened.
    ``mask_missing`` is retained for API compatibility; missing values are
    always masked. All horizontal units are converted via ``x_units_m``.
    """
    n = len(x_values)
    out = [degenerate] * n
    if not n:
        return out
    half = float(half_window)
    if not math.isfinite(half) or half <= 0 or x_units_m <= 0:
        return out
    if positive_down is None:
        positive_down = should_invert_depth_axis(y_values)
    sign = 1.0 if positive_down is False else -1.0
    width = 2.0 * half
    for start, end in contiguous_runs(x_values, y_values):
        xs = list(x_values[start:end + 1])
        ys = list(y_values[start:end + 1])
        if xs[-1] - xs[0] < width - 1e-9:
            continue
        if _np is not None:
            x = _np.asarray(xs, dtype=float)
            k0 = _np.clip(x - half, xs[0], xs[-1] - width)
            d0 = _np.interp(k0, xs, ys)
            d1 = _np.interp(k0 + width, xs, ys)
            out[start:end + 1] = _np.degrees(_np.arctan2(
                sign * (d1 - d0), width * x_units_m)).tolist()
        else:
            for index, x in enumerate(xs):
                k0 = max(xs[0], min(x - half, xs[-1] - width))
                d0 = _interp(xs, ys, k0)
                d1 = _interp(xs, ys, k0 + width)
                out[start + index] = math.degrees(math.atan2(
                    sign * (d1 - d0), width * x_units_m))
    return out


def contiguous_runs(x_values: Sequence[float],
                    y_values: Sequence[Optional[float]],
                    max_gap: Optional[float] = None,
                    group_ids: Optional[Sequence] = None
                    ) -> List[Tuple[int, int]]:
    """(start, end) index ranges (inclusive) of contiguous valid stations.

    A run breaks at a missing value, at a spacing jump larger than
    ``max_gap``, and at a change of ``group_ids`` (e.g. which raster
    supplied the station). Consumers that must never bridge no-data gaps or
    source seams evaluate slope per run. Nonfinite values and nonincreasing
    stationing also break coverage.
    """
    runs: List[Tuple[int, int]] = []
    start = None
    for index in range(len(x_values)):
        valid = (index < len(y_values) and is_finite(y_values[index])
                 and is_finite(x_values[index]))
        if not valid:
            if start is not None:
                runs.append((start, index - 1))
                start = None
            continue
        if start is not None:
            breaks = x_values[index] <= x_values[index - 1]
            if max_gap is not None and (
                    x_values[index] - x_values[index - 1]) > max_gap:
                breaks = True
            if group_ids is not None and group_ids[index] != group_ids[index - 1]:
                breaks = True
            if breaks:
                runs.append((start, index - 1))
                start = index
        else:
            start = index
    if start is not None:
        runs.append((start, len(x_values) - 1))
    return runs


def auto_half_window_m(x_values_m: Sequence[float],
                       pixel_size_m: Optional[float] = None
                       ) -> Optional[float]:
    """Half window for :func:`windowed_slope_series` on a metre-based profile.

    The full window (2 × the result) spans at least two raster cells and at
    least two station intervals, so nearest-cell sampling steps cannot read
    as near-vertical slopes. None when the series has no usable interval.
    """
    gaps = sorted(x_values_m[i + 1] - x_values_m[i]
                  for i in range(len(x_values_m) - 1)
                  if x_values_m[i + 1] > x_values_m[i])
    if not gaps:
        return None
    spacing = gaps[len(gaps) // 2]
    cell = float(pixel_size_m) if pixel_size_m else 0.0
    return max(cell, spacing)


def ols_slope(t_values: Sequence[float],
              z_values: Sequence[float]) -> Optional[float]:
    """Ordinary-least-squares slope dz/dt of z against t; None when the
    fit is degenerate (fewer than 2 points or zero t-variance)."""
    pairs = [(float(t), float(z)) for t, z in zip(t_values, z_values)
             if t is not None and z is not None
             and math.isfinite(float(t)) and math.isfinite(float(z))]
    if len(pairs) < 2:
        return None
    n = float(len(pairs))
    mean_t = sum(t for t, _z in pairs) / n
    mean_z = sum(z for _t, z in pairs) / n
    var_t = sum((t - mean_t) ** 2 for t, _z in pairs)
    if var_t <= 0.0:
        return None
    cov_tz = sum((t - mean_t) * (z - mean_z) for t, z in pairs)
    return cov_tz / var_t


def is_finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def terrace_baseline_m(xs, ys, cell_m=0.0):
    """Conservative averaging length for repeatedly terraced raster profiles.

    This detects a sampling pattern, NOT native resolution or bad soundings.
    Require three interior plateaus, each spanning at least four sample gaps,
    with most intervals flat. An isolated escarpment must remain untouched.
    Call separately for each contiguous source run. No depth values change.
    """
    if len(xs) < 16 or not all(is_finite(v) for v in ys):
        return 0.0
    gaps = [b-a for a,b in zip(xs, xs[1:])]
    if not gaps or min(gaps) <= 0:
        return 0.0
    ordered = sorted(gaps)
    gap = ordered[len(ordered)//2]
    tolerance = max(1e-7, max(abs(v) for v in ys)*1e-10)
    flat = [abs(b-a) <= tolerance for a,b in zip(ys, ys[1:])]
    if sum(flat) < .6*len(flat):
        return 0.0
    spans = []
    start = None
    for i, same in enumerate(flat + [False]):
        if same and start is None:
            start = i
        if not same and start is not None:
            span = xs[i]-xs[start]
            if start > 0 and i < len(xs)-1 and span >= 4*gap and span > 2*cell_m:
                spans.append(span + gap)
            start = None
    return 2*max(spans) if len(spans) >= 3 else 0.0


def supported_slopes(xs, ys, cells=None, sources=None, window_m=0.0,
                     positive_down=None):
    """Metre-based slopes and actual baselines, aligned to input stations.

    Auto: raster baseline >= two native cells and two median station gaps,
    widened for repeated terraces (averaging, not recovered native resolution);
    contours: exact crossing interval. Explicit windows below two native
    cells are unsupported. Never bridge missing data or source changes.
    """
    n = len(xs)
    slopes, baselines = [None] * n, [None] * n
    if positive_down is None:
        positive_down = should_invert_depth_axis(ys)
    for start, end in contiguous_runs(xs, ys, group_ids=sources):
        rx, ry = xs[start:end + 1], ys[start:end + 1]
        if len(rx) < 2:
            continue
        cell = max((float(c or 0) for c in (cells or [])[start:end + 1]), default=0.0)
        width = float(window_m)
        if width <= 0 and cell <= 0:
            vals = interval_slope_series(rx, ry, positive_down)
            widths = [None] + [rx[i] - rx[i - 1] for i in range(1, len(rx))]
        else:
            if width <= 0:
                width = max(2 * auto_half_window_m(rx, cell), terrace_baseline_m(rx, ry, cell))
            if width < 2 * cell - 1e-9:
                continue
            vals = windowed_slope_series(rx, ry, width / 2, positive_down=positive_down)
            widths = [width if v is not None else None for v in vals]
        slopes[start:end + 1], baselines[start:end + 1] = vals, widths
        # A visible break at a source transition, even when both fits exist.
        if start > 0:
            slopes[start] = baselines[start] = None
    return slopes, baselines


def clean_crossings(pairs, tolerance=1e-6):
    """Collapse coincident equal observations; conflicting depths become gaps."""
    ordered = sorted((float(x), float(y)) for x, y in pairs
                     if is_finite(x) and is_finite(y))
    groups = []
    for x, y in ordered:
        if groups and abs(x - groups[-1][0]) <= tolerance:
            groups[-1][1].append(y)
        else:
            groups.append([x, [y]])
    return [(x, values[0] if max(values) - min(values) <= tolerance else None)
            for x, values in groups]


def interpolate_covered(xs, ys, x, sources=None):
    """Interpolate only adjacent valid observations, without extrapolation."""
    i = bisect.bisect_left(xs, x)
    if i < len(xs) and abs(xs[i] - x) <= 1e-9:
        return ys[i] if is_finite(ys[i]) else None
    if i == 0 or i >= len(xs):
        return None
    if not is_finite(ys[i - 1]) or not is_finite(ys[i]):
        return None
    if sources is not None and sources[i - 1] != sources[i]:
        return None
    return _interp(xs[i - 1:i + 1], ys[i - 1:i + 1], x)


def cross_profile_metrics(xs, ys, half_width, positive_down=None,
                          cells=None, sources=None):
    """Overall endpoint tilt and maximum supported local absolute slope.

    Endpoint tilt requires complete coverage through the centre, both ends,
    and one source. No fitting/extrapolation from one side of the route.
    """
    port = interpolate_covered(xs, ys, -half_width, sources)
    stbd = interpolate_covered(xs, ys, half_width, sources)
    covered = False
    for a, b in contiguous_runs(xs, ys, group_ids=sources):
        if xs[a] <= -half_width and xs[b] >= half_width:
            covered = True
            break
    cell = max((float(c or 0) for c in cells or []), default=0)
    tilt = None
    if covered and port is not None and stbd is not None and half_width >= cell:
        sign = -1 if positive_down is False else 1
        tilt = math.degrees(math.atan2(sign * (stbd - port), 2 * half_width))
    # Local maximum belongs to the requested cross span, not its extended
    # contour search area. Preserve bracketing observations only for tilt.
    inside = [i for i,x in enumerate(xs) if -half_width <= x <= half_width]
    local_x = [-half_width] + [xs[i] for i in inside if -half_width < xs[i] < half_width] + [half_width]
    local_y = [interpolate_covered(xs,ys,x,sources) for x in local_x]
    if cells:
        import bisect
        indices = [min(bisect.bisect_left(xs,x),len(xs)-1) for x in local_x]
        local_cells = [cells[i] for i in indices]
        local_sources = [sources[i] for i in indices] if sources else None
    else:
        local_cells = local_sources = None
    slopes, _ = supported_slopes(local_x, local_y, local_cells, local_sources, positive_down=positive_down)
    peak = max((abs(v) for v in slopes if v is not None), default=None)
    if not covered or port is None or stbd is None:
        peak = None  # a partial maximum is not a safe criterion input
    return tilt, peak, port if covered else None, stbd if covered else None
