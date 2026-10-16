# -*- coding: utf-8 -*-
"""Shared basics: units, cancel / progress hooks, KP-range findings, status bins."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

G = 9.80665  # m/s^2: kgf -> N and kg/m -> N/m

# Finding severities (the same strings as laydata.qc_base.Severity).
INFO = "info"
WARNING = "warning"
ERROR = "error"
SEVERITY_LEVEL = {INFO: 1, WARNING: 2, ERROR: 3}
LEVEL_SEVERITY = {level: severity for severity, level in SEVERITY_LEVEL.items()}


class Cancelled(Exception):
    """Raised by a long calculation whose ``cancel()`` callback returned True."""


def check_cancel(cancel: Optional[Callable[[], bool]]) -> None:
    if cancel is not None and cancel():
        raise Cancelled()


def report(progress: Optional[Callable], fraction: float, text: Optional[str] = None) -> None:
    """Call ``progress(fraction, text)``; plain ``progress(fraction)`` callbacks work too."""
    if progress is None:
        return
    try:
        progress(fraction, text)
    except TypeError:
        progress(fraction)


def sub_progress(progress: Optional[Callable], start: float, end: float) -> Optional[Callable]:
    """A progress callback mapping 0..1 onto ``start..end`` of ``progress``."""
    if progress is None:
        return None

    def inner(fraction: float, text: Optional[str] = None) -> None:
        report(progress, start + (end - start) * max(0.0, min(1.0, fraction)), text)

    return inner


def pos(value) -> float:
    """Float value, nan when missing / non-numeric."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    return out if math.isfinite(out) else math.nan


def runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """Inclusive ``(start, end)`` index runs where ``mask`` is True."""
    out: List[Tuple[int, int]] = []
    start = None
    for i, value in enumerate(mask):
        if value and start is None:
            start = i
        elif not value and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def upper_hull(x: np.ndarray, y: np.ndarray) -> List[int]:
    """Indices of the upper concave hull of points sorted by ``x``."""
    hull: List[int] = []
    for i in range(len(x)):
        while len(hull) >= 2:
            o, a = hull[-2], hull[-1]
            cross = (x[a] - x[o]) * (y[i] - y[o]) - (y[a] - y[o]) * (x[i] - x[o])
            if cross >= 0.0:
                hull.pop()
            else:
                break
        hull.append(i)
    return hull


@dataclass
class RangeFinding:
    """One flagged KP range."""

    check_id: str
    severity: str
    kp_start: float
    kp_end: float
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    unit: str = ""
    rows: Tuple[int, ...] = ()          # record indices (LayRecords order), for map/table links
    time_start: Optional[float] = None
    time_end: Optional[float] = None
    extra: Dict[str, float] = field(default_factory=dict)

    @property
    def length_m(self) -> float:
        return abs(self.kp_end - self.kp_start) * 1000.0


def status_bins(kp_min: float, kp_max: float, findings: Sequence[RangeFinding], record_kp: np.ndarray,
                bin_m: float = 10.0) -> Tuple[np.ndarray, np.ndarray]:
    """``(bin_edges_km, level)`` for the coloured status bar.

    ``level`` per bin: -1 no data, 0 assessed and clear, else the highest
    finding level (1 info, 2 warning, 3 error) touching the bin.
    """
    if not (np.isfinite(kp_min) and np.isfinite(kp_max)) or kp_max <= kp_min:
        return np.array([]), np.array([], dtype=int)
    step = max(bin_m, 0.1) / 1000.0
    count = int(min(math.ceil((kp_max - kp_min) / step), 200000))
    edges = kp_min + np.arange(count + 1) * step
    level = np.full(count, -1, dtype=int)
    kp = np.asarray(record_kp, dtype=float)
    kp = kp[np.isfinite(kp)]
    if kp.size:
        cells = np.clip(((kp - kp_min) / step).astype(int), 0, count - 1)
        level[np.unique(cells)] = 0
        # Bins between consecutive records closer than 5 bins count as covered.
        sorted_cells = np.unique(cells)
        for a, b in zip(sorted_cells[:-1], sorted_cells[1:]):
            if 1 < b - a <= 5:
                level[a:b] = np.maximum(level[a:b], 0)
    for finding in findings:
        lo = int(np.clip(math.floor((finding.kp_start - kp_min) / step), 0, count - 1))
        hi = int(np.clip(math.floor((finding.kp_end - kp_min) / step), 0, count - 1))
        level[lo:hi + 1] = np.maximum(level[lo:hi + 1], SEVERITY_LEVEL.get(finding.severity, 2))
    return edges, level
