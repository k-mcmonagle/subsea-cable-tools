# -*- coding: utf-8 -*-
"""Pure math for the KP Mouse live depth/slope profile (QGIS-free, testable).

The slope/datum primitives live in the plugin-wide ``slope_utils`` module;
this module keeps the KP Mouse specific composite/contour handling and
re-exports the shared functions under their historical names.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import bisect
import math

from ..slope_utils import (  # noqa: F401  (re-exported API)
    auto_half_window_m, contiguous_runs, interval_slope_series,
    should_invert_depth_axis, windowed_slope_series,
)


def merged_contour_crossings(profile: Dict) -> Tuple[List[float], List[float]]:
    """All contour-layer crossings merged into one distance-sorted series.

    Bathy data often splits contours across layers (major/minor); a single
    merged seabed line reads better than overlapping per-layer lines. The same
    contour level frequently exists in both layers, so coincident crossings
    (same distance, same depth) collapse to one point — otherwise the zero
    spacing poisons the slope series with gaps or near-vertical spikes.
    """
    crossings = []
    for series in profile.get("contours", []):
        crossings.extend(zip(series["x"], series["y"]))
    crossings.sort(key=lambda pair: pair[0])
    merged: List[Tuple[float, float]] = []
    for x, y in crossings:
        if merged and abs(x - merged[-1][0]) <= 1e-6 and y == merged[-1][1]:
            continue
        merged.append((x, y))
    return [x for x, _y in merged], [y for _x, y in merged]


def composite_series(profile: Dict) -> Tuple[List[float], List[Optional[float]]]:
    """First-valid depth per station across rasters (resolution order),
    falling back to merged contour crossings when no raster covers the line.
    """
    rasters = profile.get("rasters", [])
    if rasters:
        x_values = rasters[0]["x"]
        y_values = []
        for index in range(len(x_values)):
            value = None
            for series in rasters:
                candidate = series["y"][index] if index < len(series["y"]) else None
                if candidate is not None:
                    value = candidate
                    break
            y_values.append(value)
        if any(value is not None for value in y_values):
            return x_values, y_values
    return merged_contour_crossings(profile)


def slope_series(x_values: List[float], y_values: List[Optional[float]],
                 positive_down: Optional[bool] = None) -> List[Optional[float]]:
    """Per-interval slope in degrees at each station (shared implementation).

    ``x`` in metres, values in metres. Sign follows the plugin-wide
    convention: **positive = shoaling along the line** (up-slope). ``None``
    marks the first station and any interval with a missing endpoint.
    """
    return interval_slope_series(x_values, y_values, positive_down)


def composite_series_with_sources(profile: Dict
                                  ) -> Tuple[List[float],
                                             List[Optional[float]],
                                             List[Optional[int]],
                                             List[Optional[float]]]:
    """:func:`composite_series` plus per-station provenance.

    Returns ``(x, y, source_index, cell_size_m)`` where ``source_index`` is
    the index of the raster series that supplied each station (None for
    no-data stations and for the contour fallback) and ``cell_size_m`` is
    that raster's cell size, so slope evaluation can scale to — and refuse
    to cross — the data that actually supplied each value.
    """
    rasters = profile.get("rasters", [])
    if rasters:
        x_values = rasters[0]["x"]
        y_values: List[Optional[float]] = []
        sources: List[Optional[int]] = []
        cells: List[Optional[float]] = []
        for index in range(len(x_values)):
            value, source, cell = None, None, None
            for r_index, series in enumerate(rasters):
                candidate = series["y"][index] if index < len(series["y"]) else None
                if candidate is not None:
                    value, source = candidate, r_index
                    cell = series.get("pixel_size_m")
                    break
            y_values.append(value)
            sources.append(source)
            cells.append(cell)
        if any(value is not None for value in y_values):
            return x_values, y_values, sources, cells
    x_values, y_values = merged_contour_crossings(profile)
    return x_values, y_values, [None] * len(x_values), [None] * len(x_values)


def profile_slope_series(profile: Dict,
                         positive_down: Optional[bool] = None
                         ) -> Tuple[List[float], List[Optional[float]],
                                    Optional[float]]:
    """Slope for the live profile: cell-scaled windows, no bridged seams.

    Slope at each station is a central difference over ``x ± half`` where
    ``half`` is the larger of that station's own source-raster cell size and
    the median station spacing — sub-cell nearest-neighbour sampling cannot
    read as a staircase of near-vertical spikes, and a fine grid is not
    over-smoothed just because a coarser raster exists elsewhere on the
    line. The window is evaluated strictly within one contiguous run of
    same-source valid stations: a window touching a no-data gap or a raster
    seam yields None (a datum offset between two grids must surface as a
    visible gap, never as a fabricated slope). Returns
    ``(x, slopes, max_half_window_m)`` aligned to the composite stations.
    """
    xs, ys, sources, cells = composite_series_with_sources(profile)
    n = len(xs)
    if n < 2:
        return xs, [None] * n, None
    if positive_down is None:
        positive_down = should_invert_depth_axis(ys)
    sign = 1.0 if positive_down is False else -1.0
    gaps = sorted(xs[i + 1] - xs[i] for i in range(n - 1)
                  if xs[i + 1] > xs[i])
    if not gaps:
        return xs, [None] * n, None
    spacing = gaps[len(gaps) // 2]
    slopes: List[Optional[float]] = [None] * n
    max_half = None
    seam_breaks: List[int] = []
    for start, end in contiguous_runs(xs, ys, group_ids=sources):
        # A run starting right after another valid station broke on a source
        # change, not a gap — remember it so the seam gets a visible break.
        if start > 0 and start - 1 < len(ys) and ys[start - 1] is not None:
            seam_breaks.append(start)
        run_x = xs[start:end + 1]
        run_y = ys[start:end + 1]
        for offset, x in enumerate(run_x):
            index = start + offset
            half = max(cells[index] or 0.0, spacing)
            k0 = max(run_x[0], x - half)
            k1 = min(run_x[-1], x + half)
            if k1 - k0 <= 1e-6:
                continue
            d0 = _interp_run(run_x, run_y, k0)
            d1 = _interp_run(run_x, run_y, k1)
            slopes[index] = math.degrees(math.atan2(sign * (d1 - d0), k1 - k0))
            if max_half is None or half > max_half:
                max_half = half
    # Each side of a raster seam reports its own within-run slope, but the
    # transition itself is unmeasurable (the grids may disagree on datum):
    # blank the seam-adjacent station so the plotted curve visibly breaks
    # instead of joining two sources as if the slope were continuous.
    for index in seam_breaks:
        slopes[index] = None
    return xs, slopes, max_half


def _interp_run(run_x: List[float], run_y: List[float], x: float) -> float:
    """Linear interpolation inside one contiguous all-valid run."""
    index = bisect.bisect_left(run_x, x)
    if index <= 0:
        return run_y[0]
    if index >= len(run_x):
        return run_y[-1]
    x0, x1 = run_x[index - 1], run_x[index]
    if x1 - x0 <= 1e-12:
        return run_y[index]
    t = (x - x0) / (x1 - x0)
    return run_y[index - 1] + t * (run_y[index] - run_y[index - 1])
