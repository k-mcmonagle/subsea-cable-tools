# -*- coding: utf-8 -*-
"""Pure 2D placement maths for body-fixed outlines (no QGIS imports).

The body frame is the one produced by the footprint/ship-outline importers:
metres, CRP at the origin, the vehicle's front along +Y. Placement happens
in a projected working CRS (metres, +X east / +Y north), so a *grid*
heading — clockwise from grid north — is the natural rotation input and
grid convergence is handled for free by measuring the heading between two
projected route points.

Unit-tested headless in ``tests/test_burial_tools.py``.
"""

from __future__ import annotations

from math import atan2, cos, degrees, hypot, radians, sin
from typing import Iterable, List, Sequence, Tuple

Point = Tuple[float, float]


def grid_heading_deg(p_before: Point, p_after: Point) -> float:
    """Heading of travel from ``p_before`` to ``p_after`` in a projected
    frame: degrees clockwise from grid north (+Y), normalised to [0, 360).

    Returns 0.0 when the two points coincide (caller should treat a
    zero-length step as "no heading available" if it matters).
    """
    dx = float(p_after[0]) - float(p_before[0])
    dy = float(p_after[1]) - float(p_before[1])
    if hypot(dx, dy) <= 0.0:
        return 0.0
    return degrees(atan2(dx, dy)) % 360.0


def place_points(points: Iterable[Point], heading_deg: float,
                 anchor: Point) -> List[Point]:
    """Rotate body-fixed points to ``heading_deg`` and translate to anchor.

    ``heading_deg`` is clockwise from grid north; the body +Y axis ends up
    pointing along the heading. E.g. the body point (0, 1) — one metre
    ahead of the CRP — lands at anchor + (sin H, cos H).
    """
    h = radians(float(heading_deg))
    cos_h, sin_h = cos(h), sin(h)
    ax, ay = float(anchor[0]), float(anchor[1])
    out: List[Point] = []
    for x, y in points:
        out.append((ax + x * cos_h + y * sin_h,
                    ay - x * sin_h + y * cos_h))
    return out


def parse_kp_list(text: str) -> List[float]:
    """Comma/semicolon/space-separated KP values -> sorted unique floats."""
    values: List[float] = []
    for chunk in (text or "").replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.append(float(chunk))
        except ValueError:
            continue
    return sorted(set(values))


def kp_series(start_km: float, end_km: float, interval_m: float
              ) -> List[float]:
    """KPs from start to end (inclusive) every ``interval_m`` metres."""
    lo, hi = sorted((float(start_km), float(end_km)))
    step_km = float(interval_m) / 1000.0
    if step_km <= 0 or hi <= lo:
        return [lo]
    out: List[float] = []
    kp = lo
    while kp < hi - 1e-9:
        out.append(round(kp, 6))
        kp += step_km
    out.append(hi)
    return out
