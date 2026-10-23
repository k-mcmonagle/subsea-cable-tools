# -*- coding: utf-8 -*-
"""The cable's rest shape on a rigid seabed, smoothing and lengths.

Rest shape (small slopes)
-------------------------
Cable resting on the seabed under horizontal tension ``H`` with submerged
weight ``w`` per metre satisfies ``H y'' = w - p``, where ``y`` is the cable
elevation and ``p >= 0`` the seabed contact pressure, with ``y >= seabed``
and ``p (y - seabed) = 0``. This is the unilateral contact problem of a
tensioned cable over a rigid, frictionless profile (Binda & Contro 1979,
*Engineering Structures* 1(3):125-130). Its solution is the *least* curve
above the seabed whose curvature never exceeds ``c = w / H``: free spans are
parabolas of that curvature (sag ``w L^2 / 8H``), and wherever ``c`` is
smaller than the seabed's concave curvature the cable bridges. With
``Phi'' = c`` the curve ``y - Phi`` is the upper concave hull of
``seabed - Phi``, computed exactly in one O(n) pass by
:func:`cable_rest_elevation`; a point load ``P`` (a repeater or joint) is a
slope jump ``P / H`` in ``Phi``.

Friction keeps cable local: a tension ``H`` can draw cable from at most the
friction reach ``H / (mu w)`` either side. :func:`pull_taut` balances laid
cable against the seabed within that reach and, where it is short, finds the
tension that makes the rest shape use exactly the cable available
(:func:`taut_tension_n`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

from .core import check_cancel, pos, report, runs, upper_hull


@dataclass
class Shortfall:
    """A stretch where the laid cable is shorter than the seabed it covers."""

    kp_start: float
    kp_end: float
    balance_pct: float        # most negative cable-vs-seabed balance (%)
    tension_kn: float         # tension that makes the cable cover it
    reach_m: float            # friction reach either side at that tension
    feasible: bool
    npts_kn: float = math.nan


def cable_rest_elevation(x_m: np.ndarray, seabed_elev_m: np.ndarray, curvature: np.ndarray,
                         jumps: Optional[np.ndarray] = None) -> np.ndarray:
    """Cable elevation resting on a seabed profile (small-slope obstacle problem).

    ``x_m`` ascending stations, ``seabed_elev_m`` seabed elevation (positive
    up; nan = no data), ``curvature`` the allowed free-span curvature
    ``w / H`` per station (1/m). Stations with a non-finite or infinite
    curvature (no bottom tension) conform to the seabed and pin the cable.
    Gaps in the seabed are not bridged (the cable is nan there).
    ``jumps`` (optional, per station) adds a slope jump ``P / H`` at a
    station carrying a point load ``P``.
    """
    x = np.asarray(x_m, dtype=float)
    s = np.asarray(seabed_elev_m, dtype=float)
    c = np.asarray(curvature, dtype=float)
    jump = None if jumps is None else np.asarray(jumps, dtype=float)
    y = np.full(len(x), np.nan)
    valid = np.isfinite(x) & np.isfinite(s)
    for a, b in runs(valid):
        y[a:b + 1] = s[a:b + 1]
        pinned = ~np.isfinite(c[a:b + 1]) | (c[a:b + 1] <= 0)
        anchors = [a] + [a + i for i in np.nonzero(pinned)[0] if 0 < i < b - a] + [b]
        anchors = sorted(set(anchors))
        for i0, i1 in zip(anchors[:-1], anchors[1:]):
            if i1 - i0 < 2:
                continue
            xs = x[i0:i1 + 1] - x[i0]
            cs = c[i0:i1 + 1].copy()
            cs[~np.isfinite(cs) | (cs <= 0)] = 0.0  # only the end anchors can be pinned here
            dx = np.diff(xs)
            slope = np.concatenate(([0.0], np.cumsum(0.5 * (cs[1:] + cs[:-1]) * dx)))
            steps = 0.5 * (slope[1:] + slope[:-1]) * dx
            if jump is not None:
                js = jump[i0:i1 + 1].copy()
                js[~np.isfinite(js)] = 0.0
                js[0] = js[-1] = 0.0  # a load on an anchor bears on the seabed there
                steps = steps + np.cumsum(js)[:-1] * dx
            phi = np.concatenate(([0.0], np.cumsum(steps)))
            g = s[i0:i1 + 1] - phi
            hull = upper_hull(xs, g)
            u = np.interp(xs, xs[hull], g[hull])
            y[i0:i1 + 1] = np.maximum(u + phi, s[i0:i1 + 1])
    return y


def smooth_profile(x_m: np.ndarray, values: np.ndarray, length_m: float, symmetric: bool = False) -> np.ndarray:
    """Centred running mean over ``length_m`` within each run of finite values.

    Works on unevenly spaced stations (contour crossings) through the running
    integral; windows shrink at run ends and gaps are never bridged. With
    ``symmetric`` a window near a run end shrinks on both sides (to the
    distance to that end), so a straight line is kept exactly, ends included.
    """
    x = np.asarray(x_m, dtype=float)
    v = np.asarray(values, dtype=float)
    if not length_m or length_m <= 0:
        return v.copy()
    out = np.full(len(v), np.nan)
    half = 0.5 * float(length_m)
    for a, b in runs(np.isfinite(x) & np.isfinite(v)):
        xs, vs = x[a:b + 1], v[a:b + 1]
        if b == a:
            out[a] = vs[0]
            continue
        integral = np.concatenate(([0.0], np.cumsum(0.5 * (vs[1:] + vs[:-1]) * np.diff(xs))))
        if symmetric:
            h = np.minimum(half, np.minimum(xs - xs[0], xs[-1] - xs))
            lo, hi = xs - h, xs + h
        else:
            lo = np.clip(xs - half, xs[0], xs[-1])
            hi = np.clip(xs + half, xs[0], xs[-1])
        width = hi - lo
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = (np.interp(hi, xs, integral) - np.interp(lo, xs, integral)) / width
        out[a:b + 1] = np.where(width > 0, mean, vs)
    return out


def conformity_length_m(ei_knm2: float, w_npm: float) -> float:
    """Shortest seabed feature a resting cable can follow: ``2 (EI / w)^(1/3)``.

    The bending length of a cable lying under its own weight; shorter
    features are bridged by bending stiffness (and are mostly sounding noise).
    """
    ei, w = pos(ei_knm2), pos(w_npm)
    if not (np.isfinite(ei) and np.isfinite(w)) or ei <= 0 or w <= 0:
        return math.nan
    return 2.0 * (ei * 1000.0 / w) ** (1.0 / 3.0)


def seabed_cumulative(x_m, depth_m) -> Tuple[np.ndarray, np.ndarray]:
    """Cumulative 3D seabed length and covered plan length (gaps add nothing)."""
    x = np.asarray(x_m, dtype=float)
    d = np.asarray(depth_m, dtype=float)
    seg_ok = np.isfinite(d[1:]) & np.isfinite(d[:-1])
    dx = np.diff(x)
    length = np.where(seg_ok, np.hypot(dx, np.where(seg_ok, np.diff(d), 0.0)), 0.0)
    plan = np.where(seg_ok, dx, 0.0)
    return np.concatenate(([0.0], np.cumsum(length))), np.concatenate(([0.0], np.cumsum(plan)))


def length_balance_pct(x_m, seabed_cum, plan_cum, cable_x, cable_cum, half_window_m,
                       min_coverage: float = 0.8, lo_m=None, hi_m=None) -> np.ndarray:
    """Laid cable minus seabed length within ``x +- half_window``, % of plan.

    Positive: more cable than the seabed needs (surplus); negative: the cable
    is short of the seabed it has to cover. nan without enough data.
    ``lo_m`` / ``hi_m`` (per station, optional) end the window where cable
    cannot be drawn from (a hold).
    """
    x = np.asarray(x_m, dtype=float)
    out = np.full(len(x), np.nan)
    if cable_x.size < 2 or x.size < 2:
        return out
    lo_limit = np.full(len(x), max(x[0], cable_x[0]))
    hi_limit = np.full(len(x), min(x[-1], cable_x[-1]))
    if lo_m is not None:
        lo_limit = np.minimum(np.maximum(lo_limit, lo_m), x)
    if hi_m is not None:
        hi_limit = np.maximum(np.minimum(hi_limit, hi_m), x)
    a = np.clip(x - half_window_m, lo_limit, hi_limit)
    b = np.clip(x + half_window_m, lo_limit, hi_limit)
    plan = b - a
    covered = np.interp(b, x, plan_cum) - np.interp(a, x, plan_cum)
    seabed = np.interp(b, x, seabed_cum) - np.interp(a, x, seabed_cum)
    cable = np.interp(b, cable_x, cable_cum) - np.interp(a, cable_x, cable_cum)
    with np.errstate(invalid="ignore", divide="ignore"):
        ok = (plan > 0) & (covered >= min_coverage * plan)
        balance = (cable - seabed * plan / covered) / plan * 100.0
    out[ok] = balance[ok]
    return out


def taut_tension_n(x_m, seabed_elev_m, w_npm, x0: float, x1: float, cable_x, cable_cum,
                   mu: float, min_reach_m: float, max_reach_m: float,
                   cap_n: float = 2.0e6, held=None, lo_m: float = -math.inf,
                   hi_m: float = math.inf) -> Tuple[float, float, float, bool, float]:
    """Tension that makes the cable's resting shape use exactly the cable laid.

    The shortfall between ``x0`` and ``x1`` draws on cable within the friction
    reach ``H / (mu w)`` either side, so the window grows with the tension.
    Bisection on ``H`` (the rest shape shortens as ``H`` grows). Returns
    ``(H, a, b, feasible, reach)``: ``feasible`` is False when even taut at
    ``cap_n`` the cable cannot cover the seabed; ``reach`` is the friction
    reach either side at ``H`` (within the min / max reach). ``held``
    stations (per station, optional) follow the seabed, and the window ends
    at ``lo_m`` / ``hi_m`` (a hold: no cable is drawn through it).
    """
    x = np.asarray(x_m, dtype=float)
    s = np.asarray(seabed_elev_m, dtype=float)
    w = np.asarray(w_npm, dtype=float)
    pinned = None if held is None else np.asarray(held, dtype=bool)
    lo_x, hi_x = max(x[0], cable_x[0], lo_m), min(x[-1], cable_x[-1], hi_m)
    i0, i1 = int(np.searchsorted(x, x0, side="left")), int(np.searchsorted(x, x1, side="right"))
    w_here = w[i0:i1]
    w_bar = float(np.nanmean(w_here)) if np.isfinite(w_here).any() else float(np.nanmean(w))

    def reach_at(h):
        return float(np.clip(h / (mu * w_bar), min_reach_m, max_reach_m)) if mu > 0 and w_bar > 0 \
            else max_reach_m

    def window(h):
        reach = reach_at(h)
        return max(x0 - reach, lo_x), min(x1 + reach, hi_x)

    def excess(h):
        a, b = window(h)
        j0, j1 = int(np.searchsorted(x, a, side="left")), int(np.searchsorted(x, b, side="right"))
        xs, ss, ws = x[j0:j1], s[j0:j1], w[j0:j1]
        if xs.size < 3:
            return -1.0
        curvature = np.where(np.isfinite(ws), ws, np.nanmean(ws)) / h
        if pinned is not None:
            curvature = np.where(pinned[j0:j1], np.inf, curvature)
        y = cable_rest_elevation(xs, ss, curvature)
        seg = np.isfinite(y[1:]) & np.isfinite(y[:-1])
        shape = float(np.sum(np.hypot(np.diff(xs), np.diff(y))[seg]))
        covered = float(np.sum(np.diff(xs)[seg]))
        span = xs[-1] - xs[0]
        # Cable laid over exactly the stations the shape spans (not the window
        # [a, b], up to two stations longer): otherwise short windows look slack.
        laid = float(np.interp(xs[-1], cable_x, cable_cum) - np.interp(xs[0], cable_x, cable_cum))
        laid *= covered / span if span > 0 else 1.0
        return shape - laid

    h_lo, h_hi = 10.0, float(cap_n)
    if excess(h_lo) <= 0:
        a, b = window(0.0)
        return 0.0, a, b, True, reach_at(0.0)
    if excess(h_hi) > 0:
        a, b = window(h_hi)
        return h_hi, a, b, False, reach_at(h_hi)
    for _ in range(28):
        mid = math.sqrt(h_lo * h_hi)
        if excess(mid) > 0:
            h_lo = mid
        else:
            h_hi = mid
        if h_hi / h_lo < 1.01:
            break
    a, b = window(h_hi)
    return h_hi, a, b, True, reach_at(h_hi)


def pull_taut(x_m, kp_km, length_elev_m, w_npm, tension_kn, source, cable_x, cable_cum,
              mu: float, max_reach_m: float, shortfall_pct: float, min_reach_m: float,
              npts_kn=None, zero_tension_kn: float = 0.05,
              cancel: Optional[Callable[[], bool]] = None, progress: Optional[Callable] = None,
              held=None) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, List[Shortfall]]:
    """Raise the tension where the laid cable is short of the seabed.

    ``length_elev_m`` is the seabed used for lengths (elevation or depth: only
    differences count), ``w_npm`` the submerged weight per station (no nan),
    ``tension_kn`` the tension the cable was laid at and ``source`` a per
    station flag (1 laid tension, 2 pulled taut). ``cable_x`` /
    ``cable_cum`` give the cumulative cable laid. The laid cable is balanced
    against the seabed within the friction reach ``H / (mu w)`` (clipped to
    ``min_reach_m .. max_reach_m``); where it is short by more than
    ``shortfall_pct`` the tension that makes the cable cover the seabed
    (:func:`taut_tension_n`) is applied over that reach if higher, falling by
    ``mu w`` per metre away from the stretch. ``held`` stations (a hold,
    optional) give no cable and keep their tension: the windows end at them.
    Returns ``(tension_kn, source, balance_pct, reach_m, shortfalls)``.
    ``progress(fraction, text)`` runs 0..1 over the short stretches.
    """
    x = np.asarray(x_m, dtype=float)
    kp = np.asarray(kp_km, dtype=float)
    length_elev = np.asarray(length_elev_m, dtype=float)
    w_fill = np.asarray(w_npm, dtype=float)
    tension = np.asarray(tension_kn, dtype=float).copy()
    source = np.asarray(source).copy()
    npts = np.full(len(x), np.nan) if npts_kn is None else np.asarray(npts_kn, dtype=float)
    seabed_cum, plan_cum = seabed_cumulative(x, length_elev)
    held = np.zeros(len(x), dtype=bool) if held is None else np.asarray(held, dtype=bool)
    # The nearest held station at or before / after each station: cable is not drawn through it.
    hold_lo = np.maximum.accumulate(np.where(held, x, -np.inf)) if held.any() else None
    hold_hi = np.minimum.accumulate(np.where(held, x, np.inf)[::-1])[::-1] if held.any() else None
    with np.errstate(invalid="ignore", divide="ignore"):
        reach = np.clip(tension * 1000.0 / (mu * w_fill), min_reach_m, max_reach_m) if mu > 0 \
            else np.full(len(x), max_reach_m)
    balance = length_balance_pct(x, seabed_cum, plan_cum, cable_x, cable_cum, reach, lo_m=hold_lo, hi_m=hold_hi)
    shortfalls: List[Shortfall] = []
    with np.errstate(invalid="ignore"):
        short = np.isfinite(balance) & (balance < -shortfall_pct) & ~held
    window_end = -math.inf
    pieces = []
    for a, b in runs(short):
        # A long short stretch needs a local tension along it, not one average:
        # solve it in pieces no longer than twice the friction reach.
        start = a
        while start <= b:
            stop = int(np.searchsorted(x, x[start] + 2.0 * max_reach_m, side="right")) - 1
            stop = min(max(stop, start), b)
            pieces.append((start, stop))
            start = stop + 1
    for k, (a, b) in enumerate(pieces):
        check_cancel(cancel)
        report(progress, k / len(pieces), f"Pulling short stretches taut ({k + 1} of {len(pieces)})")
        limits = {}
        if hold_lo is not None:
            limits = {"held": held, "lo_m": float(hold_lo[a]), "hi_m": float(hold_hi[b])}
        h, wa, wb, feasible, reach_h = taut_tension_n(x, -length_elev, w_fill, x[a], x[b], cable_x, cable_cum,
                                                      mu, min_reach_m, max_reach_m, **limits)
        if h / 1000.0 <= zero_tension_kn:
            continue  # friction reach brings enough cable (or barely any tension): it conforms
        sel = (x >= wa) & (x <= wb)
        h_kn = h / 1000.0
        # Friction takes mu w per metre off the tension away from the stretch.
        outside = np.where(x < x[a], x[a] - x, np.where(x > x[b], x - x[b], 0.0))
        pulled = h_kn - mu * w_fill * outside / 1000.0
        raise_ = sel & (pulled > tension) & ~held
        tension = np.where(raise_, pulled, tension)
        source = np.where(raise_, 2, source)
        item = Shortfall(
            float(kp[a]), float(kp[b]), float(np.nanmin(balance[a:b + 1])), h_kn,
            reach_h, feasible,
            float(np.nanmin(npts[a:b + 1])) if np.isfinite(npts[a:b + 1]).any() else math.nan)
        last = shortfalls[-1] if shortfalls else None
        if last is not None and x[a] <= window_end:
            # Overlapping friction windows: one taut stretch.
            last.kp_end = max(last.kp_end, item.kp_end)
            last.balance_pct = min(last.balance_pct, item.balance_pct)
            last.feasible = last.feasible and item.feasible
            if item.tension_kn > last.tension_kn:
                last.tension_kn, last.reach_m = item.tension_kn, item.reach_m
            last.npts_kn = float(np.nanmin([last.npts_kn, item.npts_kn])) \
                if np.isfinite([last.npts_kn, item.npts_kn]).any() else math.nan
        else:
            shortfalls.append(item)
        window_end = max(window_end, wb)
    return tension, source, balance, reach, shortfalls
