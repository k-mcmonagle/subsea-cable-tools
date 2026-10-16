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
CONTOUR_LENGTH_LIMIT_M = 0.5   # coarser contours understate seabed length too much for the length check
COARSE_GRID_M = 1.0            # coarser grids blunt crests: bend radii at supports read high


def contour_interval_m(depths) -> float:
    """The contour interval of a crossing profile: median step between its distinct levels."""
    d = np.asarray([v for v in depths if v is not None], dtype=float)
    d = np.unique(np.round(d[np.isfinite(d)], 3))
    steps = np.diff(d)
    steps = steps[steps > 1e-6]
    return float(np.median(steps)) if steps.size else math.nan


def _metres(value: float) -> str:
    return f"{value:.2g} m" if value < 10 else f"{value:.0f} m"


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
