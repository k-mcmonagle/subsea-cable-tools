# -*- coding: utf-8 -*-
"""Pure math for the KP Mouse live depth/slope profile (QGIS-free, testable)."""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple


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
    """Per-interval slope in degrees at each station.

    ``x`` in metres, values in metres. Sign follows the plugin-wide
    convention: **positive = shoaling along the line** (up-slope),
    regardless of whether the source stores positive-down depths or
    negative elevations. ``positive_down`` overrides the datum; when None
    it is auto-detected from the data (median sign). ``None`` marks the
    first station (no preceding interval) and any interval with a missing
    endpoint.
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


def should_invert_depth_axis(values: List[Optional[float]]) -> Optional[bool]:
    """True when depths are positive-down (invert so deeper plots lower),
    False for negative elevations, None when there is no data to judge."""
    finite = [value for value in values if value is not None]
    if not finite:
        return None
    return sorted(finite)[len(finite) // 2] > 0
