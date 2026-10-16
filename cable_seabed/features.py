# -*- coding: utf-8 -*-
"""Seabed features added to a profile: boulder, pipeline (crossing) and berm.

Each feature sits on the seabed at a KP and adds height above it. The
profile gains stations through the feature (every ``length / 40``, between
2 cm and 0.5 m), so its shape is resolved at any route sampling. Used for
studies such as a cable crossing an exposed pipeline or a boulder in the
route, and how the span over it changes with tension.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

import numpy as np

BOULDER = "boulder"
PIPE = "pipe"
BERM = "berm"
FEATURE_KINDS = (BOULDER, PIPE, BERM)
FEATURE_LABELS = {BOULDER: "Boulder (rounded)", PIPE: "Pipeline / cable crossing", BERM: "Berm / mattress"}


@dataclass
class SeabedFeature:
    """A feature on the seabed.

    * boulder: half-ellipse, ``height_m`` high and ``length_m`` long at the base;
    * pipe: a circle of outside diameter ``length_m`` whose top is
      ``height_m`` above the seabed (``height_m = length_m``: lying on the seabed);
    * berm: trapezoid (rock berm, mattress), ``height_m`` high, ``length_m``
      at the base and ``crest_m`` on top.
    """

    kind: str
    kp_km: float
    height_m: float
    length_m: float
    crest_m: float = 0.0
    name: str = ""

    def valid(self) -> bool:
        return (self.kind in FEATURE_KINDS and math.isfinite(self.kp_km) and math.isfinite(self.height_m)
                and math.isfinite(self.length_m) and self.height_m > 0 and self.length_m > 0)

    @property
    def half_length_m(self) -> float:
        return 0.5 * float(self.length_m)

    def label(self) -> str:
        return self.name or FEATURE_LABELS.get(self.kind, self.kind)

    def height_at(self, offset_m: np.ndarray) -> np.ndarray:
        """Height above the seabed at ``offset_m`` from the centre (0 outside)."""
        d = np.abs(np.asarray(offset_m, dtype=float))
        half = self.half_length_m
        h = float(self.height_m)
        out = np.zeros(d.shape)
        inside = d <= half
        if self.kind == BOULDER:
            with np.errstate(invalid="ignore"):
                out = np.where(inside, h * np.sqrt(np.clip(1.0 - (d / half) ** 2, 0.0, 1.0)), 0.0)
        elif self.kind == PIPE:
            radius = half
            centre = h - radius
            with np.errstate(invalid="ignore"):
                top = centre + np.sqrt(np.clip(radius ** 2 - d ** 2, 0.0, None))
            out = np.where(inside, np.maximum(top, 0.0), 0.0)
        elif self.kind == BERM:
            crest = min(max(float(self.crest_m or 0.0), 0.0), 2.0 * half)
            flank = half - 0.5 * crest
            with np.errstate(invalid="ignore", divide="ignore"):
                ramp = np.where(flank > 0, h * (half - d) / flank, h)
            out = np.where(d <= 0.5 * crest, h, np.where(inside, np.clip(ramp, 0.0, h), 0.0))
        return out


def feature_spacing_m(feature: SeabedFeature) -> float:
    return float(np.clip(feature.length_m / 40.0, 0.02, 0.5))


def apply_features(x_m: np.ndarray, kp_km: np.ndarray, features: Sequence[SeabedFeature],
                   *values: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[np.ndarray]]:
    """Add stations through each feature and return its height above the seabed.

    ``values`` (per-station arrays, e.g. depth) are interpolated onto the new
    stations; gaps (nan) stay gaps. Returns ``(x, kp, height, values)``; add
    ``height`` to the seabed elevation (subtract from depth).
    """
    x = np.asarray(x_m, dtype=float)
    kp = np.asarray(kp_km, dtype=float)
    usable = [f for f in features if f.valid()]
    if not usable or len(x) < 2:
        return x, kp, np.zeros(len(x)), [np.asarray(v, dtype=float) for v in values]
    extra = []
    for feature in usable:
        centre = float(np.interp(feature.kp_km, kp, x))
        half = feature.half_length_m
        step = feature_spacing_m(feature)
        pad = max(4.0 * step, 0.5)
        count = int(math.ceil((2.0 * (half + pad)) / step)) + 1
        local = np.linspace(centre - half - pad, centre + half + pad, count)
        extra.append(local[(local > x[0]) & (local < x[-1])])
        extra.append(np.array([centre - half, centre, centre + half]))
    new_x = np.unique(np.round(np.concatenate([x] + extra), 6))
    new_x = new_x[(new_x >= x[0]) & (new_x <= x[-1])]
    new_kp = np.interp(new_x, x, kp)
    out_values = []
    for v in values:
        arr = np.asarray(v, dtype=float)
        finite = np.isfinite(arr)
        interp = np.interp(new_x, x[finite], arr[finite]) if finite.sum() >= 2 else np.full(len(new_x), np.nan)
        # Keep gaps: a new station inside a gap of the original stays a gap.
        gap = ~finite
        if gap.any():
            cell = np.clip(np.searchsorted(x, new_x, side="right") - 1, 0, len(x) - 2)
            bad = gap[cell] | gap[np.minimum(cell + 1, len(x) - 1)]
            exact = np.isin(new_x, x[finite])
            interp = np.where(bad & ~exact, np.nan, interp)
        out_values.append(interp)
    height = np.zeros(len(new_x))
    for feature in usable:
        centre = float(np.interp(feature.kp_km, kp, x))
        height = np.maximum(height, feature.height_at(new_x - centre))
    return new_x, new_kp, height, out_values
