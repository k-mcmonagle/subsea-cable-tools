# -*- coding: utf-8 -*-
"""Spans of a resting cable, their supports, and the tension that keeps them within limits.

A span is a run of stations where the cable clears the seabed by more than
a reporting gap. At each end the cable bears on the seabed: under
horizontal tension ``H`` it enters a contact at slope ``tan a_in`` and leaves
at ``tan a_out``, so the seabed pushes up with ``P = H (tan a_in - tan
a_out)``. Bending stiffness spreads that turn over the bending length
``lambda = sqrt(EI / H)`` either side. For a tensioned beam over a point
support the peak moment is ``M = P lambda / 2``, so the bend radius is about
``R = 2 lambda / turn`` (:func:`span_supports`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .core import check_cancel, report, runs
from .shape import cable_rest_elevation

# A span runs from lift-off to touchdown: the rest shape touches the seabed
# exactly, so a gap above this marks the cable clear of it.
SPAN_END_TOL_M = 1e-4


@dataclass
class Support:
    """Where the cable bears on the seabed at the end of a free span."""

    i0: int                     # first / last contact station
    i1: int
    kp: float
    reaction_kn: float          # concentrated seabed reaction on the cable
    bend_radius_m: float        # nan when the bending stiffness is unknown
    moment_knm: float           # bending moment EI / R (nan without EI)
    tension_kn: float
    mbr_m: float = math.nan
    crest: bool = False         # a short contact with spans both sides (else a touchdown / lift-off)

    @property
    def below_mbr(self) -> bool:
        return bool(np.isfinite(self.bend_radius_m) and np.isfinite(self.mbr_m)
                    and self.bend_radius_m < self.mbr_m)


@dataclass
class Span:
    i0: int
    i1: int
    kp_start: float
    kp_end: float
    length_m: float
    max_gap_m: float
    kp_max_gap: float
    tension_kn: float
    source: str = "lay model"
    supports: Tuple[int, ...] = ()          # indices into the model's supports
    min_bend_radius_m: float = math.nan     # tightest bend at its supports
    max_reaction_kn: float = math.nan
    mbr_m: float = math.nan
    allowable_kn: float = math.nan          # highest tension with no reportable span here

    @property
    def below_mbr(self) -> bool:
        return bool(np.isfinite(self.min_bend_radius_m) and np.isfinite(self.mbr_m)
                    and self.min_bend_radius_m < self.mbr_m)


def find_spans(x_m: np.ndarray, kp_km: np.ndarray, gap_m: np.ndarray, tension_kn: np.ndarray,
               min_gap_m: float = 0.3, min_length_m: float = 5.0,
               source: Optional[np.ndarray] = None,
               source_names: Optional[Dict[int, str]] = None,
               contact_tol_m: Optional[float] = None) -> List[Span]:
    """Contiguous runs where the cable clears the seabed by more than ``min_gap_m``.

    With ``contact_tol_m`` (both seabed tools pass :data:`SPAN_END_TOL_M`) a
    span is the whole free run from lift-off to touchdown (gap above
    ``contact_tol_m``), reported when it rises above ``min_gap_m`` and is at
    least ``min_length_m`` long: the span length of pipeline practice
    (DNV-RP-F105). Without it, only the part higher than ``min_gap_m``
    counts.
    ``source`` (per station flag) and ``source_names`` (flag -> text) name
    where each span's tension came from; by default flag 2 reads "cable short
    of the seabed" and anything else "lay model".
    """
    gap = np.asarray(gap_m, dtype=float)
    out: List[Span] = []
    with np.errstate(invalid="ignore"):
        if contact_tol_m is None:
            mask = np.isfinite(gap) & (gap > min_gap_m)
            candidates = runs(mask)
        else:
            free = np.isfinite(gap) & (gap > contact_tol_m)
            candidates = [(a, b) for a, b in runs(free) if np.nanmax(gap[a:b + 1]) > min_gap_m]
    for a, b in candidates:
        # Widen to the touch-down / lift-off stations either side.
        a0 = max(a - 1, 0)
        b0 = min(b + 1, len(gap) - 1)
        length = float(x_m[b0] - x_m[a0])
        if length < min_length_m:
            continue
        local = gap[a:b + 1]
        k = a + int(np.nanargmax(local))
        tension = tension_kn[a:b + 1]
        origin = "lay model"
        if source is not None:
            if source_names:
                flags = [int(v) for v in np.asarray(source[a:b + 1]) if np.isfinite(v)]
                if flags:
                    origin = source_names.get(max(flags), origin)
            elif np.any(source[a:b + 1] == 2):
                origin = "cable short of the seabed"
        out.append(Span(a0, b0, float(kp_km[a0]), float(kp_km[b0]), length, float(gap[k]), float(kp_km[k]),
                        float(np.nanmax(tension)) if np.isfinite(tension).any() else math.nan, origin))
    return out


def span_supports(x_m, seabed_elev_m, cable_elev_m, tension_kn, w_npm, ei_knm2, mbr_m,
                  spans: Sequence[Span], kp_km=None, contact_tol_m: Optional[float] = None) -> List[Support]:
    """The seabed contact at each end of each span: reaction, bend radius and moment.

    The cable bears on the seabed over contact stretches (cable within
    ``contact_tol_m`` of it, by default :data:`SPAN_END_TOL_M`). A span's
    support is the end of the stretch next to it: its touchdown or lift-off.
    A short stretch with spans both sides (a crest, no longer than the
    bending spread ``2 lambda``, ``lambda = sqrt(EI / H)``) is one support.
    The cable enters at slope ``tan a_in`` and leaves at ``tan a_out``; under
    horizontal tension ``H`` the seabed pushes up with ``P = H (tan a_in -
    tan a_out)``, plus on a crest the weight of the cable resting there. A
    touchdown where the cable meets the seabed tangentially has no
    concentrated reaction. Bending stiffness spreads the turn over ``lambda``
    either side (a tensioned beam over a point support: ``M = P lambda / 2``),
    so the bend radius is ``R = (L_contact + 2 lambda) / turn``; the turn
    within ``lambda`` of each contact end is also checked, so a rounded crest
    sharper than that spread is caught. Each span's ``supports``, tightest
    bend radius and largest reaction are filled in.
    """
    x = np.asarray(x_m, dtype=float)
    s = np.asarray(seabed_elev_m, dtype=float)
    y = np.asarray(cable_elev_m, dtype=float)
    kp = x / 1000.0 if kp_km is None else np.asarray(kp_km, dtype=float)
    tol = SPAN_END_TOL_M if contact_tol_m is None else max(float(contact_tol_m), SPAN_END_TOL_M)
    n = len(x)
    out: List[Support] = []
    if n < 3 or not spans:
        return out
    with np.errstate(invalid="ignore"):
        gap = y - s
        contact = np.isfinite(gap) & (gap <= tol)
    contact_idx = np.nonzero(contact)[0]
    if contact_idx.size == 0:
        return out
    dx = np.diff(x)
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = np.diff(y) / dx
    angle = np.arctan(slope)
    mid = 0.5 * (x[1:] + x[:-1])
    finite_angle = np.isfinite(angle)
    mid_ok, angle_ok = mid[finite_angle], angle[finite_angle]

    def angle_at(xq):
        return float(np.interp(xq, mid_ok, angle_ok)) if mid_ok.size else math.nan

    run_of = np.full(n, -1, dtype=int)
    contact_runs = runs(contact)
    for r, (a, b) in enumerate(contact_runs):
        run_of[a:b + 1] = r

    # The contact stretch at each end of each span, and the stretches with a span both sides.
    ends: List[Tuple[int, int]] = []
    span_after: Dict[int, bool] = {}      # stretch -> a span starts at its far end
    span_before: Dict[int, bool] = {}     # stretch -> a span ends at its near end
    for span in spans:
        left = int(np.searchsorted(contact_idx, span.i0, side="right")) - 1
        right = int(np.searchsorted(contact_idx, span.i1, side="left"))
        run_l = int(run_of[contact_idx[left]]) if 0 <= left < contact_idx.size else -1
        run_r = int(run_of[contact_idx[right]]) if 0 <= right < contact_idx.size else -1
        ends.append((run_l, run_r))
        if run_l >= 0:
            span_after[run_l] = True
        if run_r >= 0:
            span_before[run_r] = True

    def h_ei_lambda(c0: int, c1: int) -> Tuple[float, float, float]:
        tv = tension_kn[c0:c1 + 1]
        h_kn = float(np.nanmax(tv)) if np.isfinite(tv).any() else math.nan
        ev = ei_knm2[c0:c1 + 1]
        ei = float(np.nanmin(ev)) if np.isfinite(ev).any() else math.nan
        lam = math.sqrt(ei / h_kn) if np.isfinite(ei) and ei > 0 and np.isfinite(h_kn) and h_kn > 0 else math.nan
        return h_kn, ei, lam

    def is_crest(run: int) -> bool:
        if not (span_before.get(run) and span_after.get(run)):
            return False
        c0, c1 = contact_runs[run]
        _h, _ei, lam = h_ei_lambda(c0, c1)
        step = float(np.nanmedian(dx[max(c0 - 1, 0):min(c1 + 1, n - 1)]))
        return float(x[c1] - x[c0]) <= max(2.0 * lam if np.isfinite(lam) else 0.0, 3.0 * step)

    made: Dict[Tuple[int, int, int], Optional[int]] = {}

    def support_at(c0: int, c1: int, crest: bool) -> Optional[int]:
        """A support over contact stations ``c0 .. c1`` (one station for a span end)."""
        key = (c0, c1, int(crest))
        if key in made:
            return made[key]
        h_kn, ei, lam = h_ei_lambda(c0, c1)
        if not np.isfinite(h_kn) or h_kn <= 0:
            made[key] = None
            return None
        h_n = h_kn * 1000.0
        tan_in = slope[c0 - 1] if c0 > 0 else math.nan
        tan_out = slope[c1] if c1 < n - 1 else math.nan
        contact_len = float(x[c1] - x[c0])
        wv = w_npm[c0:c1 + 1]
        w_here = float(np.nanmin(wv)) if np.isfinite(wv).any() else 0.0
        reaction = h_n * (tan_in - tan_out) + (w_here * contact_len if crest else 0.0)
        if np.isfinite(reaction):
            reaction = max(reaction, 0.0)       # a tangent touchdown: no concentrated reaction
        radius = moment = math.nan
        if np.isfinite(lam):
            step = float(np.nanmedian(dx[max(c0 - 1, 0):min(c1 + 1, n - 1)]))
            half = max(lam, 0.5 * step)
            # The whole turn over the contact plus the bending length either side ...
            turn = angle_at(x[c0] - half) - angle_at(x[c1] + half)
            curvature = turn / (contact_len + 2.0 * lam) if turn > 0 else 0.0
            # ... and the turn within the bending length at each end of the contact.
            for xe in (x[c0], x[c1]):
                local = angle_at(xe - half) - angle_at(xe + half)
                if local > 0:
                    curvature = max(curvature, local / (2.0 * lam))
            radius = 1.0 / curvature if curvature > 0 else math.inf
            moment = ei * curvature
        mv = mbr_m[c0:c1 + 1]
        mbr = float(np.nanmax(mv)) if np.isfinite(mv).any() else math.nan
        made[key] = len(out)
        out.append(Support(int(c0), int(c1), float(kp[(c0 + c1) // 2]),
                           reaction / 1000.0 if np.isfinite(reaction) else math.nan,
                           radius, moment, h_kn, mbr, crest))
        return made[key]

    for span, (run_l, run_r) in zip(spans, ends):
        ids = []
        for run, side in ((run_l, -1), (run_r, 1)):
            if run < 0:
                continue
            c0, c1 = contact_runs[run]
            if is_crest(run):
                index = support_at(c0, c1, True)
            else:
                end = c1 if side < 0 else c0     # the stretch's end next to the span
                index = support_at(end, end, False)
            if index is not None and index not in ids:
                ids.append(index)
        span.supports = tuple(ids)
        chosen = [out[i] for i in ids]
        radii = [sp.bend_radius_m for sp in chosen if np.isfinite(sp.bend_radius_m) or sp.bend_radius_m == math.inf]
        span.min_bend_radius_m = float(min(radii)) if radii else math.nan
        reactions = [sp.reaction_kn for sp in chosen if np.isfinite(sp.reaction_kn)]
        span.max_reaction_kn = float(max(reactions)) if reactions else math.nan
        mbrs = [sp.mbr_m for sp in chosen if np.isfinite(sp.mbr_m)]
        span.mbr_m = float(max(mbrs)) if mbrs else math.nan
    return out


def allowable_tension_kn(x_m, seabed_elev_m, w_npm, min_gap_m: float = 0.3, min_length_m: float = 5.0,
                         core_m: float = 250.0, margin_m: float = 150.0, lo_kn: float = 0.1,
                         hi_kn: float = 100.0, cancel: Optional[Callable[[], bool]] = None,
                         progress: Optional[Callable] = None,
                         point_loads_n: Optional[np.ndarray] = None,
                         gap_floor_m: Optional[np.ndarray] = None,
                         contact_tol_m: Optional[float] = None) -> np.ndarray:
    """Per station, the highest bottom tension that rests the cable with no
    reportable span (higher than ``min_gap_m`` and longer than ``min_length_m``).

    Solved by bisection in windows of ``core_m`` (padded ``margin_m`` either
    side so spans across the window edge form). ``hi_kn`` where no span forms
    even at ``hi_kn``; 0 where one forms at ``lo_kn`` already (a hollow too
    deep and wide for any tension). The answer for planning a re-lay or a
    remedial survey: lay at or below it to keep spans within the limits.

    With uniform weight and a constant tension, spans only grow as the
    tension rises (the monotonicity of Binda & Contro's Minkowski-matrix LCP),
    so the bisection is well posed. ``point_loads_n`` (per station) adds
    point weights; ``gap_floor_m`` (per station) is the seabed the gap is
    measured from when it differs from the resting surface (body offsets).
    """
    x = np.asarray(x_m, dtype=float)
    s = np.asarray(seabed_elev_m, dtype=float)
    w = np.asarray(w_npm, dtype=float)
    loads = None if point_loads_n is None else np.asarray(point_loads_n, dtype=float)
    floor = s if gap_floor_m is None else np.asarray(gap_floor_m, dtype=float)
    out = np.full(len(x), np.nan)
    ok = np.isfinite(x) & np.isfinite(s)
    if ok.sum() < 3:
        return out
    w = np.where(np.isfinite(w), w, np.nanmean(w) if np.isfinite(w).any() else np.nan)
    if not np.isfinite(w).any():
        return out
    cores = np.arange(x[ok][0], x[ok][-1] + core_m, core_m)
    total = max(len(cores) - 1, 1)
    for k in range(len(cores) - 1):
        check_cancel(cancel)
        if k % 10 == 0:
            report(progress, k / total)
        c0, c1 = cores[k], cores[k + 1]
        k0, k1 = int(np.searchsorted(x, c0, side="left")), int(np.searchsorted(x, c1, side="left"))
        if k + 2 == len(cores):
            k1 = len(x)
        j0, j1 = int(np.searchsorted(x, c0 - margin_m, side="left")), int(np.searchsorted(x, c1 + margin_m))
        xs, ss, ws, fs = x[j0:j1], s[j0:j1], w[j0:j1], floor[j0:j1]
        ls = None if loads is None else loads[j0:j1]
        if k1 - k0 < 1 or np.isfinite(ss).sum() < 3:
            continue

        def spans_at(h_kn, xs=xs, ss=ss, ws=ws, fs=fs, ls=ls, c0=c0, c1=c1):
            h_n = h_kn * 1000.0
            y = cable_rest_elevation(xs, ss, ws / h_n, None if ls is None else ls / h_n)
            found = find_spans(xs, xs / 1000.0, y - fs, np.full(len(xs), h_kn), min_gap_m, min_length_m,
                               contact_tol_m=contact_tol_m)
            return any(xs[sp.i1] >= c0 and xs[sp.i0] < c1 for sp in found)

        if not spans_at(hi_kn):
            out[k0:k1] = hi_kn
            continue
        if spans_at(lo_kn):
            out[k0:k1] = 0.0
            continue
        lo, hi = lo_kn, hi_kn
        while hi / lo > 1.05:
            mid = math.sqrt(lo * hi)
            if spans_at(mid):
                hi = mid
            else:
                lo = mid
        out[k0:k1] = lo
    report(progress, 1.0)
    return out
