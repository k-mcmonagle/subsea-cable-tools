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
    auto_half_window_m, contiguous_runs, interval_slope_series, supported_slopes, clean_crossings, terrace_baseline_m,
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
    merged = clean_crossings(crossings)
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


def profile_slope_series(profile: Dict, positive_down: Optional[bool] = None):
    """Shared supported slopes; full native-resolution windows, no seams."""
    xs, ys, sources, cells = composite_series_with_sources(profile)
    profile["terrace_baseline_m"] = max((terrace_baseline_m(xs[a:b+1], ys[a:b+1],
        max((c or 0 for c in cells[a:b+1]), default=0))
        for a,b in contiguous_runs(xs, ys, group_ids=sources)
        if any(cells[a:b+1])), default=0)
    slopes, widths = supported_slopes(xs, ys, cells, sources,
                                     profile.get("slope_window_m", 0), positive_down)
    profile["slope_baseline_m"] = widths
    return xs, slopes, max((w / 2 for w in widths if w), default=None)
