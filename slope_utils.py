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
    finite = [value for value in values if value is not None]
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
        if dx <= 0 or v1 is None or v2 is None:
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
    """Slope (°) per station from a central difference over ``x ± half_window``.

    Depths are linearly interpolated at the window edges (clamped to the
    valid-station range, so edge stations use the window that exists), which
    keeps a consistent physical measurement scale however irregular or dense
    the stations are. ``x_values`` must be sorted ascending; ``half_window``
    is in the same units, converted to metres via ``x_units_m`` (1000 for
    km-based series). ``positive_down=None`` auto-detects the datum.
    ``degenerate`` is returned where the clamped window collapses or no
    depth is available; ``mask_missing`` additionally forces ``degenerate``
    at stations whose own depth is missing (so coverage gaps stay visible).
    """
    n = len(x_values)
    if n == 0:
        return []
    if positive_down is None:
        positive_down = should_invert_depth_axis(y_values)
    sign = 1.0 if positive_down is False else -1.0
    half = max(float(half_window), 1e-9)
    if _np is not None:
        return _windowed_slope_series_np(
            x_values, y_values, half, x_units_m, sign, degenerate, mask_missing)
    xs: List[float] = []
    ys: List[float] = []
    for x, y in zip(x_values, y_values):
        if y is not None:
            xs.append(float(x))
            ys.append(float(y))
    out: List[Optional[float]] = []
    for index, x in enumerate(x_values):
        if not xs or (mask_missing and (index >= len(y_values)
                                        or y_values[index] is None)):
            out.append(degenerate)
            continue
        k0 = max(xs[0], x - half)
        k1 = min(xs[-1], x + half)
        dx_m = (k1 - k0) * float(x_units_m)
        if dx_m <= 1e-6:
            out.append(degenerate)
            continue
        d0 = _interp(xs, ys, k0)
        d1 = _interp(xs, ys, k1)
        if d0 is None or d1 is None:
            out.append(degenerate)
            continue
        out.append(math.degrees(math.atan2(sign * (d1 - d0), dx_m)))
    return out


def _windowed_slope_series_np(x_values, y_values, half, x_units_m, sign,
                              degenerate, mask_missing):
    """Vectorised twin of the pure-python loop (same semantics: linear
    interpolation across no-data gaps between valid stations, ``degenerate``
    where the clamped window collapses)."""
    x_arr = _np.asarray([float(x) for x in x_values], dtype=float)
    y_arr = _np.asarray(
        [float("nan") if y is None else float(y) for y in y_values],
        dtype=float)
    valid = ~_np.isnan(y_arr)
    if not bool(valid.any()):
        return [degenerate] * len(x_values)
    xs = x_arr[valid]
    ys = y_arr[valid]
    k0 = _np.clip(x_arr - half, xs[0], xs[-1])
    k1 = _np.clip(x_arr + half, xs[0], xs[-1])
    dx_m = (k1 - k0) * float(x_units_m)
    d0 = _np.interp(k0, xs, ys)
    d1 = _np.interp(k1, xs, ys)
    with _np.errstate(invalid="ignore"):
        slopes = _np.degrees(_np.arctan2(sign * (d1 - d0), dx_m))
    bad = dx_m <= 1e-6
    if mask_missing:
        bad = bad | ~valid
    values = slopes.tolist()
    flags = bad.tolist()
    return [degenerate if flag else value
            for value, flag in zip(values, flags)]


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
