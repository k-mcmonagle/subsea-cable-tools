# -*- coding: utf-8 -*-
"""Pure math for the KP Mouse live depth/slope profile (QGIS-free, testable).

The slope/datum primitives live in the plugin-wide ``slope_utils`` module;
this module keeps the KP Mouse specific composite/contour handling and
re-exports the shared functions under their historical names.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..slope_utils import (  # noqa: F401  (re-exported API)
    auto_half_window_m, interval_slope_series, should_invert_depth_axis,
    windowed_slope_series,
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


def profile_slope_series(x_values: List[float],
                         y_values: List[Optional[float]],
                         pixel_size_m: Optional[float] = None,
                         positive_down: Optional[bool] = None
                         ) -> Tuple[List[Optional[float]], Optional[float]]:
    """Slope series for the live profile, robust to sub-cell sampling.

    Central difference over ``x ± half_window`` where the half window is the
    larger of the raster cell size and the median station spacing, so a
    nearest-cell staircase on a coarse grid cannot read as near-vertical
    spikes. Falls back to the per-interval series when no window can be
    derived (fewer than two stations). Returns ``(slopes, half_window_m)``;
    the half window is None on the fallback path.
    """
    half_window_m = auto_half_window_m(x_values, pixel_size_m)
    if half_window_m is None:
        return slope_series(x_values, y_values, positive_down), None
    slopes = windowed_slope_series(
        x_values, y_values, half_window_m,
        positive_down=positive_down, degenerate=None, mask_missing=True)
    return slopes, half_window_m
