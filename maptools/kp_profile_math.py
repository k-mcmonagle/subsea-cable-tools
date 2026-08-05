# -*- coding: utf-8 -*-
"""Pure math for the KP Mouse live depth/slope profile (QGIS-free, testable)."""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple


def merged_contour_crossings(profile: Dict) -> Tuple[List[float], List[float]]:
    """All contour-layer crossings merged into one distance-sorted series.

    Bathy data often splits contours across layers (major/minor); a single
    merged seabed line reads better than overlapping per-layer lines.
    """
    crossings = []
    for series in profile.get("contours", []):
        crossings.extend(zip(series["x"], series["y"]))
    crossings.sort(key=lambda pair: pair[0])
    return [x for x, _y in crossings], [y for _x, y in crossings]


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


def slope_series(x_values: List[float], y_values: List[Optional[float]]) -> List[Optional[float]]:
    """Per-interval slope in degrees at each station (first station 0).

    ``x`` in metres, values in metres; ``None`` where either endpoint is
    missing, matching the Depth Profile tool's convention.
    """
    if not x_values:
        return []
    slopes: List[Optional[float]] = [0.0 if y_values and y_values[0] is not None else None]
    for index in range(1, len(x_values)):
        dx = x_values[index] - x_values[index - 1]
        v1 = y_values[index - 1] if index - 1 < len(y_values) else None
        v2 = y_values[index] if index < len(y_values) else None
        if dx <= 0 or v1 is None or v2 is None:
            slopes.append(None)
            continue
        slopes.append(math.degrees(math.atan2(v2 - v1, dx)))
    return slopes


def should_invert_depth_axis(values: List[Optional[float]]) -> Optional[bool]:
    """True when depths are positive-down (invert so deeper plots lower),
    False for negative elevations, None when there is no data to judge."""
    finite = [value for value in values if value is not None]
    if not finite:
        return None
    return sorted(finite)[len(finite) // 2] > 0
