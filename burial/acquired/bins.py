# -*- coding: utf-8 -*-
"""DOL on design-KP bins: support, worst case per bin, latest wins, statistics, listings.

**Bins.** Bin ``j`` covers KP ``[j * step, (j + 1) * step)`` metres (1 m by
default). Each sample *supports* the route half way to each neighbour, so a
survey every 5 m still fills its 1 m bins, while a gap wider than the
*bridging limit* is no data: there the samples either side support only
``edge`` metres beyond themselves (half the typical spacing). A bin's value
is the **worst case**, the smallest DOL of the samples supporting it, and
``cover_m`` is how much of the bin they support (a run ending mid-bin
covers part of it). Samples are taken in KP order whatever the time order,
so several passes in one dataset combine on the worst case.

**Selection across datasets.** *Latest* takes, in each bin, the dataset with
the latest sample time there (a dataset without times uses its files'
acquisition dates, filled in by the caller); ties go to the dataset listed
first. *Worst* takes the smallest value. KP-range overrides force a dataset
over half-open ranges (by bin centre); where it has no data the bin has
none.

**Assessment.** Against the plan's target runs (by bin centre), a bin
meets the target, is below it, or has no target; bins without support are
no data. Lengths are covered metres (``cover_m``), so statistics are
length-weighted; a scope cuts bins it crosses in proportion.

Pure numpy; works on numpy 1.26 (QGIS 3.40) and 2.x (QGIS 4).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

STATUS_NO_DATA, STATUS_MEETS, STATUS_BELOW, STATUS_NO_TARGET = 0, 1, 2, 3
STATUS_NAMES = {STATUS_NO_DATA: "No data", STATUS_MEETS: "Meets target", STATUS_BELOW: "Below target",
                STATUS_NO_TARGET: "No target"}
MODE_LATEST, MODE_WORST = "latest", "worst"
MODES = {MODE_LATEST: "Latest", MODE_WORST: "Worst case"}
METHOD_WORST, METHOD_MEAN = "worst", "mean"


@dataclass
class BinSet:
    """One dataset on the bin grid (see the module notes)."""
    step_m: float
    index: np.ndarray        # int64, sorted, unique
    worst: np.ndarray
    mean: np.ndarray
    count: np.ndarray
    cover_m: np.ndarray
    sample: np.ndarray       # int64 index (into the arrays given) of each bin's worst sample
    time: np.ndarray         # latest sample time per bin (NaN when none is known)
    spacing_m: float = 0.0
    bridge_m: float = 0.0

    def __len__(self) -> int:
        return int(self.index.size)

    @property
    def kp_km(self) -> np.ndarray:
        return self.index * self.step_m / 1000.0


@dataclass
class Selection:
    """The governing value per bin over one or more datasets."""
    step_m: float
    index: np.ndarray
    worst: np.ndarray
    mean: np.ndarray
    cover_m: np.ndarray
    source: np.ndarray       # int: position of the governing dataset in the list given
    sample: np.ndarray
    time: np.ndarray
    others: np.ndarray       # how many other datasets also have data in the bin

    def __len__(self) -> int:
        return int(self.index.size)

    @property
    def kp_km(self) -> np.ndarray:
        return self.index * self.step_m / 1000.0

    @property
    def centre_km(self) -> np.ndarray:
        return (self.index + 0.5) * self.step_m / 1000.0


def _empty_binset(step_m: float, spacing: float = 0.0, bridge: float = 0.0) -> BinSet:
    i, f = np.zeros(0, dtype=np.int64), np.zeros(0)
    return BinSet(step_m, i, f, f.copy(), i.copy(), f.copy(), i.copy(), f.copy(), spacing, bridge)


def median_spacing_m(kp_km: np.ndarray) -> float:
    """Median distance between successive distinct KPs (m), ignoring repeats."""
    x = np.sort(np.asarray(kp_km, dtype=float)[np.isfinite(kp_km)]) * 1000.0
    d = np.diff(x)
    d = d[d > 1e-6]
    return float(np.median(d)) if d.size else 0.0


def default_bridge_m(spacing_m: float, step_m: float = 1.0) -> float:
    """Three times the typical spacing, and never below one bin."""
    return max(3.0 * float(spacing_m), float(step_m))


def _expand(first: np.ndarray, last: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """``(owner, bin)`` pairs for owners covering bins ``first..last`` inclusive."""
    counts = (last - first + 1).astype(np.int64)
    owner = np.repeat(np.arange(first.size, dtype=np.int64), counts)
    starts = np.cumsum(counts) - counts
    offset = np.arange(owner.size, dtype=np.int64) - np.repeat(starts, counts)
    return owner, first[owner] + offset


def bin_samples(kp_km: np.ndarray, values: np.ndarray, time: Optional[np.ndarray] = None, step_m: float = 1.0,
                bridge_m: Optional[float] = None) -> BinSet:
    """Worst case per bin of one dataset's samples (NaN KPs or values are left out)."""
    kp_km = np.asarray(kp_km, dtype=float)
    values = np.asarray(values, dtype=float)
    time = np.full(kp_km.size, np.nan) if time is None else np.asarray(time, dtype=float)
    ok = np.flatnonzero(np.isfinite(kp_km) & np.isfinite(values))
    spacing = median_spacing_m(kp_km[ok]) if ok.size else 0.0
    bridge = float(bridge_m) if bridge_m else default_bridge_m(spacing, step_m)
    if not ok.size:
        return _empty_binset(step_m, spacing, bridge)
    order = ok[np.argsort(kp_km[ok], kind="stable")]
    x, v, t = kp_km[order] * 1000.0, values[order], time[order]
    n = x.size
    edge = min((spacing if spacing > 0 else step_m) / 2.0, bridge / 2.0)
    d = np.diff(x)
    joined = d <= bridge
    mid = x[:-1] + d / 2.0
    left, right = np.empty(n), np.empty(n)
    left[0], right[-1] = x[0] - edge, x[-1] + edge
    left[1:] = np.where(joined, mid, x[1:] - np.minimum(edge, d / 2.0))
    right[:-1] = np.where(joined, mid, x[:-1] + np.minimum(edge, d / 2.0))

    first = np.floor(left / step_m).astype(np.int64)
    last = np.maximum(np.ceil(right / step_m).astype(np.int64) - 1, first)
    owner, bins = _expand(first, last)
    vv = v[owner]
    sort = np.lexsort((vv, bins))
    bins, owner, vv = bins[sort], owner[sort], vv[sort]
    starts = np.flatnonzero(np.r_[True, bins[1:] != bins[:-1]])
    count = np.diff(np.r_[starts, bins.size]).astype(np.int64)
    index = bins[starts]
    tt = t[owner]
    latest = np.fmax.reduceat(np.where(np.isfinite(tt), tt, -np.inf), starts)
    latest[np.isneginf(latest)] = np.nan

    # Coverage: each run of joined samples supports [left of its first, right of its last).
    breaks = np.flatnonzero(~joined)
    run_first = np.r_[0, breaks + 1]
    run_last = np.r_[breaks, n - 1]
    ra, rb = left[run_first], right[run_last]
    rfirst = np.floor(ra / step_m).astype(np.int64)
    rlast = np.maximum(np.ceil(rb / step_m).astype(np.int64) - 1, rfirst)
    rowner, rbins = _expand(rfirst, rlast)
    lo = np.maximum(ra[rowner], rbins * step_m)
    hi = np.minimum(rb[rowner], (rbins + 1) * step_m)
    cover = np.zeros(index.size)
    np.add.at(cover, np.searchsorted(index, rbins), np.maximum(hi - lo, 0.0))
    return BinSet(step_m=step_m, index=index, worst=vv[starts],
                  mean=np.add.reduceat(vv, starts) / count, count=count, cover_m=np.minimum(cover, step_m),
                  sample=order[owner[starts]].astype(np.int64), time=latest, spacing_m=spacing, bridge_m=bridge)


def select(binsets: Sequence[BinSet], mode: str = MODE_LATEST,
           overrides: Sequence[Tuple[float, float, int]] = ()) -> Selection:
    """The governing dataset per bin (see the module notes); overrides are ``(KP from, KP to, dataset)``."""
    if not binsets:
        raise ValueError("No datasets to select from")
    step = binsets[0].step_m
    if any(b.step_m != step for b in binsets):
        raise ValueError("Datasets must share the bin size")
    union = np.unique(np.concatenate([b.index for b in binsets])) if any(len(b) for b in binsets) \
        else np.zeros(0, dtype=np.int64)
    m, u = len(binsets), union.size
    has = np.zeros((m, u), dtype=bool)
    pos = np.zeros((m, u), dtype=np.int64)
    for k, b in enumerate(binsets):
        if not len(b):
            continue
        p = np.minimum(np.searchsorted(b.index, union), len(b) - 1)
        has[k] = b.index[p] == union
        pos[k] = p
    if mode == MODE_WORST:
        key = np.full((m, u), np.inf)
        for k, b in enumerate(binsets):
            if len(b):
                key[k, has[k]] = b.worst[pos[k, has[k]]]
        choice = np.argmin(key, axis=0)
    elif mode == MODE_LATEST:
        key = np.full((m, u), -np.inf)
        for k, b in enumerate(binsets):
            if len(b):
                t = b.time[pos[k, has[k]]]
                key[k, has[k]] = np.where(np.isfinite(t), t, -1e300)
        choice = np.argmax(key, axis=0)          # first maximum: ties go to the dataset listed first
    else:
        raise ValueError(f"Unknown selection mode '{mode}'")
    centre = (union + 0.5) * step / 1000.0
    for start, end, k in overrides:
        if not 0 <= int(k) < m:
            raise ValueError(f"Override names dataset {k}, which is not in the list")
        inside = (centre >= min(start, end)) & (centre < max(start, end))
        choice[inside] = int(k)
    present = has[choice, np.arange(u)]
    union, choice = union[present], choice[present]
    columns = np.arange(u)[present]
    out = {name: np.empty(union.size) for name in ("worst", "mean", "cover_m", "time")}
    sample = np.empty(union.size, dtype=np.int64)
    for k, b in enumerate(binsets):
        mine = choice == k
        if not mine.any():
            continue
        p = pos[k, columns[mine]]
        for name in out:
            out[name][mine] = getattr(b, name)[p]
        sample[mine] = b.sample[p]
    others = has[:, columns].sum(axis=0) - 1
    return Selection(step, union, out["worst"], out["mean"], out["cover_m"], choice.astype(np.int64), sample,
                     out["time"], others.astype(np.int64))


def target_at(kp_km: np.ndarray, runs: Sequence[Tuple[float, float, Optional[float]]]) -> np.ndarray:
    """Target depth at each KP from ``(start, end, depth or None)`` half-open runs; NaN where none."""
    kp_km = np.asarray(kp_km, dtype=float)
    out = np.full(kp_km.size, np.nan)
    for start, end, depth in runs:
        if depth is None or not math.isfinite(float(depth)):
            continue
        lo, hi = sorted((float(start), float(end)))
        out[(kp_km >= lo) & (kp_km < hi)] = float(depth)
    return out


def classify(selection: Selection, target: np.ndarray, tolerance_m: float = 0.0) -> np.ndarray:
    """Status per selected bin: meets target, below target, or no target."""
    status = np.full(len(selection), STATUS_NO_TARGET, dtype=np.int64)
    known = np.isfinite(target)
    status[known & (selection.worst >= target - tolerance_m)] = STATUS_MEETS
    status[known & (selection.worst < target - tolerance_m)] = STATUS_BELOW
    return status


def _scope_factor(selection: Selection, scope_km: Optional[Tuple[float, float]]) -> np.ndarray:
    if scope_km is None:
        return np.ones(len(selection))
    lo, hi = sorted(scope_km)
    start = selection.index * selection.step_m
    overlap = np.clip(np.minimum(start + selection.step_m, hi * 1000.0) - np.maximum(start, lo * 1000.0), 0.0,
                      selection.step_m)
    return overlap / selection.step_m


def summarise(selection: Selection, status: np.ndarray, scope_km: Optional[Tuple[float, float]] = None) -> Dict:
    """Length-weighted statistics (metres) over ``scope_km`` (default: the selected bins' extent)."""
    weight = selection.cover_m * _scope_factor(selection, scope_km)
    inside = weight > 0
    if scope_km is not None:
        lo, hi = sorted(scope_km)
        scope_m = (hi - lo) * 1000.0
    elif len(selection):
        scope_m = float((selection.index[-1] - selection.index[0] + 1) * selection.step_m)
    else:
        scope_m = 0.0
    assessed = float(weight.sum())
    lengths = {s: float(weight[status == s].sum()) for s in (STATUS_MEETS, STATUS_BELOW, STATUS_NO_TARGET)}
    with_target = lengths[STATUS_MEETS] + lengths[STATUS_BELOW]
    out = {"scope_m": scope_m, "assessed_m": assessed, "no_data_m": max(0.0, scope_m - assessed),
           "meets_m": lengths[STATUS_MEETS], "below_m": lengths[STATUS_BELOW],
           "no_target_m": lengths[STATUS_NO_TARGET],
           "meets_pct": 100.0 * lengths[STATUS_MEETS] / with_target if with_target else None,
           "min": None, "min_kp": None, "mean": None, "sources": sorted(set(selection.source[inside].tolist()))}
    if inside.any():
        values = selection.worst[inside]
        k = int(np.argmin(values))
        out["min"] = float(values[k])
        out["min_kp"] = float(selection.centre_km[inside][k])
        out["mean"] = float(np.sum(selection.mean[inside] * weight[inside]) / weight[inside].sum())
    return out


def below_ranges(selection: Selection, status: np.ndarray, target: np.ndarray, join_gap_m: float = 0.0,
                 scope_km: Optional[Tuple[float, float]] = None) -> List[Dict]:
    """Below-target stretches: consecutive below bins, joined across gaps up to ``join_gap_m``."""
    weight = selection.cover_m * _scope_factor(selection, scope_km)
    below = np.flatnonzero((status == STATUS_BELOW) & (weight > 0))
    if not below.size:
        return []
    step = selection.step_m
    idx = selection.index[below]
    gap_bins = np.diff(idx) - 1
    split = np.flatnonzero(gap_bins * step > join_gap_m + 1e-9)
    groups = np.split(np.arange(below.size), split + 1)
    lo, hi = sorted(scope_km) if scope_km is not None else (-math.inf, math.inf)
    out = []
    for group in groups:
        rows = below[group]
        values = selection.worst[rows]
        k = int(np.argmin(values))
        shortfall = target[rows] - values
        out.append({
            "kp_from": max(lo, float(selection.index[rows[0]] * step / 1000.0)),
            "kp_to": min(hi, float((selection.index[rows[-1]] + 1) * step / 1000.0)),
            "length_m": float(weight[rows].sum()),
            "min": float(values[k]),
            "min_kp": float(selection.centre_km[rows][k]),
            "max_shortfall": float(np.nanmax(shortfall)),
            "sources": sorted(set(selection.source[rows].tolist())),
            "bins": int(rows.size),
        })
    return out


def no_data_ranges(selection: Selection, scope_km: Tuple[float, float], min_length_m: float = 0.0
                   ) -> List[Tuple[float, float]]:
    """KP stretches of the scope with no supported bin, at bin resolution."""
    lo, hi = sorted(scope_km)
    step = selection.step_m
    first, last = int(math.floor(lo * 1000.0 / step)), int(math.ceil(hi * 1000.0 / step)) - 1
    covered = selection.index[(selection.cover_m > 0) & (selection.index >= first) & (selection.index <= last)]
    edges = np.r_[first - 1, covered, last + 1]
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        if b - a > 1:
            start = max(lo, (a + 1) * step / 1000.0)
            end = min(hi, b * step / 1000.0)
            if (end - start) * 1000.0 >= min_length_m and end > start:
                out.append((start, end))
    return out


def listing(selection: Selection, step_out_m: float, method: str = METHOD_WORST,
            target: Optional[np.ndarray] = None) -> Dict[str, np.ndarray]:
    """Rows at ``step_out_m`` (a multiple of the bin size) from the selected bins.

    ``worst`` lists the smallest value in each interval, ``mean`` the
    cover-weighted mean; ``sample``/``source`` name the record behind the
    worst value (for positions and other channels).
    """
    step = selection.step_m
    ratio = step_out_m / step
    if ratio < 1 or abs(ratio - round(ratio)) > 1e-9:
        raise ValueError("The listing interval must be a whole number of bins")
    if method not in (METHOD_WORST, METHOD_MEAN):
        raise ValueError(f"Unknown listing method '{method}'")
    if not len(selection):
        empty = np.zeros(0)
        return {k: empty for k in ("kp_from", "kp_to", "value", "cover_m", "target", "status")} | {
            "source": np.zeros(0, dtype=np.int64), "sample": np.zeros(0, dtype=np.int64)}
    group = np.floor_divide(selection.index, int(round(ratio)))
    order = np.lexsort((selection.worst, group))
    g = group[order]
    starts = np.flatnonzero(np.r_[True, g[1:] != g[:-1]])
    worst_rows = order[starts]
    cover = np.add.reduceat(selection.cover_m[order], starts)
    if method == METHOD_WORST:
        value = selection.worst[worst_rows]
    else:
        value = np.add.reduceat((selection.mean * selection.cover_m)[order], starts) / np.where(cover > 0, cover, 1)
    out = {"kp_from": g[starts] * step_out_m / 1000.0, "kp_to": (g[starts] + 1) * step_out_m / 1000.0,
           "value": value, "cover_m": cover, "source": selection.source[worst_rows],
           "sample": selection.sample[worst_rows]}
    if target is not None:
        row_target = target[worst_rows]
        status = np.full(row_target.size, STATUS_NO_TARGET, dtype=np.int64)
        known = np.isfinite(row_target)
        status[known & (value >= row_target)] = STATUS_MEETS
        status[known & (value < row_target)] = STATUS_BELOW
        out["target"], out["status"] = row_target, status
    return out
