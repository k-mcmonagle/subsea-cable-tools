# -*- coding: utf-8 -*-
"""What a sampled seabed profile can resolve: notes shared by both seabed tools.

The bend radius and reaction at span supports come from changes of seabed
slope, so they are the most sensitive to how the seabed is sampled. Coarse
grids blunt crests (bend radii read high and can hide an MBR breach),
contours cut crests to the last contour, and rasters meeting along a route
can carry a vertical datum step that reads as relief.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np

SEABED_NONE = "none"
SEABED_LAY_MODEL = "lay_model"
SEABED_RASTER = "raster"
SEABED_CONTOURS = "contours"
SEABED_RPL = "rpl"             # an RPL's approximate depths (straight lines between positions)
SEABED_PROFILE = "profile"     # a profile given point by point (CSV, DXF)
SEABED_SYNTHETIC = "synthetic"
CONTOUR_LENGTH_LIMIT_M = 0.5   # coarser contours understate seabed length too much for the length check
COARSE_GRID_M = 1.0            # coarser grids blunt crests: bend radii at supports read high

# What a sampled seabed can resolve (see the Help: "Seabed data: what it can resolve").
RESOLVE_INTERVALS = 3.0        # a span needs this many data intervals: two to show a sag, and its supports
#                                (crests) are each placed only to within half an interval
NOISE_TO_GAP = 4.0             # auto noise filter: residual noise at most 1/4 of the reporting gap
MAX_FILTER_INTERVALS = 2.0     # ... averaged over at most two data intervals (finer than what spans need)
SPIKE_SIGMAS = 8.0             # a single point this far above its neighbours: a possible flier


def contour_interval_m(depths) -> float:
    """The contour interval of a crossing profile: median step between its distinct levels."""
    d = np.asarray([v for v in depths if v is not None], dtype=float)
    d = np.unique(np.round(d[np.isfinite(d)], 3))
    steps = np.diff(d)
    steps = steps[steps > 1e-6]
    return float(np.median(steps)) if steps.size else math.nan


def _metres(value: float) -> str:
    return f"{value:.2g} m" if value < 10 else f"{value:.0f} m"


def data_spacing_m(x_m, cell_m=None) -> np.ndarray:
    """How far apart the seabed is known around each station (m).

    The larger of the gaps to the neighbouring stations (the stations are the
    data: profile points, contour crossings) or, for a raster, its cell size
    where that is larger (a grid sampled finer than its cells holds no more
    detail than the cells).
    """
    x = np.asarray(x_m, dtype=float)
    n = len(x)
    if n < 2:
        return np.full(n, np.nan)
    gaps = np.abs(np.diff(x))
    spacing = np.maximum(np.concatenate(([gaps[0]], gaps)), np.concatenate((gaps, [gaps[-1]])))
    if cell_m is not None and len(cell_m) == n:
        cells = np.array([np.nan if c is None else float(c) for c in cell_m], dtype=float)
        spacing = np.where(np.isfinite(cells) & (cells > spacing), cells, spacing)
    return spacing


def data_points(x: np.ndarray, depth: np.ndarray, spacing: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """The stations thinned to about one per data interval (a finely sampled grid repeats its cells)."""
    ok = np.isfinite(x) & np.isfinite(depth)
    x, depth, spacing = x[ok], depth[ok], spacing[ok]
    if len(x) < 3:
        return x, depth
    steps = np.diff(x)
    step = float(np.median(steps[steps > 0])) if (steps > 0).any() else 0.0
    every = int(round(float(np.nanmedian(spacing)) / step)) if step > 0 else 1
    return (x[::every], depth[::every]) if every > 1 else (x, depth)


def cell_noise_m(x_m, depth_m, spacing_m=None) -> float:
    """Cell-to-cell variation of a sampled seabed (m, a robust standard deviation).

    The median absolute second difference of the depths, one data interval
    apart, scaled to the standard deviation of white noise that would give it
    (``1.4826 MAD / sqrt(6)``). Sounding noise and real relief at the scale of
    the data both add to it, so it is an upper bound on the noise; sparse
    features (a boulder, a flier) do not move it. Nan with too few points.
    """
    x = np.asarray(x_m, dtype=float)
    depth = np.asarray(depth_m, dtype=float)
    spacing = data_spacing_m(x) if spacing_m is None else np.asarray(spacing_m, dtype=float)
    px, pd = data_points(x, depth, spacing)
    if len(px) < 5:
        return math.nan
    h = np.diff(px)
    even = np.abs(h[1:] - h[:-1]) <= 0.1 * np.maximum(h[1:], h[:-1])   # second differences on even steps only
    d2 = (pd[:-2] - 2.0 * pd[1:-1] + pd[2:])[even]
    if d2.size < 3:
        return math.nan
    return float(1.4826 * np.median(np.abs(d2)) / math.sqrt(6.0))


def spike_indices(x_m, depth_m, noise_m: float, factor: float = SPIKE_SIGMAS) -> np.ndarray:
    """Stations standing alone well above both neighbours (depth positive down): possible fliers.

    A single sounding (or cell) shallower than both neighbours by more than
    ``factor`` noise standard deviations (in second-difference terms). The
    cable can rest on it, making a support and a span either side. It may be
    a flier or a real small object; nothing is removed.
    """
    x = np.asarray(x_m, dtype=float)
    depth = np.asarray(depth_m, dtype=float)
    if len(x) < 3 or not (math.isfinite(noise_m) and noise_m > 0):
        return np.zeros(0, dtype=int)
    d2 = depth[:-2] - 2.0 * depth[1:-1] + depth[2:]
    limit = factor * max(noise_m, 0.005) * math.sqrt(6.0)
    with np.errstate(invalid="ignore"):
        peak = (d2 > limit) & (depth[1:-1] < depth[:-2]) & (depth[1:-1] < depth[2:])
    return np.nonzero(peak)[0] + 1


def auto_filter_m(spacing_m: float, noise_m: float, gap_m: float, length_m: float = math.inf) -> float:
    """The noise filter (running mean, m) suited to the data, 0 when none is needed.

    Averaging ``n`` data intervals cuts independent noise by ``sqrt(n)``; ``n``
    is chosen so the residual noise is at most a quarter of the reporting gap
    (a noise peak then rarely reaches the height of a reportable span). The
    mean never spans more than :data:`MAX_FILTER_INTERVALS` intervals (relief
    the data resolves, three intervals and more, is kept) nor more than half
    the reporting length ``length_m`` (relief at the scale of the spans
    reported is kept). On a grid coarser than that the variation measured is
    relief, not noise, and the cells are already averages: no filter.
    """
    if not (math.isfinite(spacing_m) and spacing_m > 0 and math.isfinite(noise_m) and gap_m > 0):
        return 0.0
    n = (NOISE_TO_GAP * noise_m / gap_m) ** 2
    if n < 1.0:
        return 0.0
    width = min(n, MAX_FILTER_INTERVALS) * spacing_m
    if math.isfinite(length_m) and length_m > 0:
        width = min(width, 0.5 * length_m)
    return float(width) if width >= spacing_m else 0.0


def seabed_sampling_notes(mode, x_m, depth_m, kp_km, cells=None, sources=None, messages=(),
                          auto: bool = True) -> Tuple[List[str], Optional[str]]:
    """``(notes, length_check_skip_reason)`` on how the seabed was sampled.

    Says what spacing was used and what the source cannot resolve: a grid
    coarser than the cable's bending scale, contours (crests cut to the last
    contour; too coarse for the length check above
    :data:`CONTOUR_LENGTH_LIMIT_M`), rasters meeting along the route (a
    vertical datum step there reads as relief), plus the Depth Profile
    engine's own messages (spacing raised on long routes, partial coverage).
    """
    x = np.asarray(x_m, dtype=float)
    depth = np.asarray(depth_m, dtype=float)
    kp = np.asarray(kp_km, dtype=float)
    notes: List[str] = []
    skip = None
    finite = np.isfinite(depth) & np.isfinite(x)
    steps = np.diff(x[finite])
    steps = steps[steps > 1e-6]
    spacing = float(np.median(steps)) if steps.size else math.nan
    if mode == SEABED_RASTER:
        cell = np.array([c for c in (cells or []) if c is not None and np.isfinite(c) and c > 0], dtype=float)
        if cell.size:
            lo, hi = float(cell.min()), float(cell.max())
            cell_text = _metres(lo) if hi - lo < 0.01 * hi else f"{_metres(lo)} to {_metres(hi)}"
            if auto:
                notes.append(f"Seabed sampled at the grid's cell size ({cell_text}).")
            else:
                notes.append(f"Seabed sampled every {_metres(spacing)} (grid cells {cell_text}).")
                if np.isfinite(spacing) and spacing > 1.5 * lo:
                    notes.append("Sampling coarser than the grid misses crests and shortens the seabed: "
                                 "set Sample every to auto.")
            if hi > COARSE_GRID_M * 1.05:  # a nominal 1 m grid in degrees reads 1.00x m
                notes.append(f"Grid cells up to {_metres(hi)}: crests are blunted, so the bend radius at span "
                             "supports reads high and can hide an MBR breach.")
        if sources is not None and len(sources) == len(depth):
            change = [i for i in range(1, len(sources)) if sources[i] is not None and sources[i - 1] is not None
                      and sources[i] != sources[i - 1]]
            if change:
                where = ", ".join(f"{kp[i]:.3f}" for i in change[:5]) + (" …" if len(change) > 5 else "")
                notes.append(f"The seabed comes from {len({s for s in sources if s})} rasters, meeting at KP "
                             f"{where}: a vertical datum step where they meet reads as relief; check there.")
    elif mode == SEABED_CONTOURS:
        interval = contour_interval_m(depth)
        if np.isfinite(interval):
            notes.append(f"Contours every {_metres(interval)}: between crossings the seabed is a straight line, so "
                         "crests are cut to the last contour and span heights, bend radii and seabed length are "
                         "underestimated. Grid them, or use an MBES raster, for a firmer answer.")
            if interval > CONTOUR_LENGTH_LIMIT_M + 1e-9:
                skip = (f"contours every {_metres(interval)} understate the seabed length; grid them or use a "
                        "raster")
    elif mode == SEABED_LAY_MODEL and np.isfinite(spacing):
        notes.append(f"Lay model seabed binned every {_metres(spacing)}"
                     + (" (the touchdown record spacing)." if auto else "."))
    for message in messages or ():
        text = message[0] if isinstance(message, (tuple, list)) else str(message)
        if text and text not in notes:
            notes.append(str(text))
    return notes, skip
