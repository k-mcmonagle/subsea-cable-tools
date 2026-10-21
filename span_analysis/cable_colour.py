# -*- coding: utf-8 -*-
"""Colouring the Span & Slack cable by a quantity, in fixed classes.

The classes are fixed (engineering limits, not stretched to the data), so a
colour keeps its meaning as the lay control moves; red and amber stay with
the findings, except tension above NPTS (a red finding itself).

* **Slack state**: held by tension; at zero tension with little surplus;
  with surplus over half the loop limit; loop risk (the loop check's limit);
  not held by friction (slides).
* **Tension**: % of NPTS in quarters (kN bands where a cable has no NPTS).
* **What sets the tension**: the lay tension, pulled taut (slack short of
  the seabed), raised by a slope, or zero.

:func:`class_paths` builds one polyline set per class for the KPs in view,
thinned to the pixels with each pixel's highest and lowest point kept.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import numpy as np

from ..cable_seabed import planning as pl

PLAIN, SLACK_STATE, TENSION, SOURCE = "plain", "slack_state", "tension", "source"
MODES = (
    (PLAIN, "Nothing (plain cable)", "The cable in one colour."),
    (SLACK_STATE, "Slack state and loop risk", "Held by tension, lying at zero tension, surplus slack, loop risk "
     "(the loop check's limit) or not held by friction."),
    (TENSION, "Tension (% of NPTS)", "Residual tension in fixed bands: quarters of NPTS, or kN where a cable has "
     "no NPTS."),
    (SOURCE, "What sets the tension", "The lay tension, pulled taut (slack short of the seabed), raised by a slope, "
     "or zero."),
)
MAX_CLASSES = 8
Colour = Tuple[int, int, int]


def cable_classes(result: pl.PlanningResult, mode: str) -> Tuple[np.ndarray, List[Tuple[Colour, str]], str]:
    """``(class per station (-1: none), [(colour, label)] per class, note)``."""
    st, c = result.stations, result.criteria
    n = st.n
    t = np.asarray(result.tension_kn, dtype=float)
    classes = np.full(n, -1, dtype=int)
    note = ""
    if mode == SLACK_STATE:
        loop = float(c.loop_excess_pct)
        window = max(float(c.slack_window_m), 2.0 * st.spacing_m)
        surplus = np.asarray(result.excess_pct, dtype=float)   # over the route, as the loop check measures it
        held = t > c.loop_tension_kn
        legend = [((70, 130, 180), f"Held by tension (over {c.loop_tension_kn:g} kN)")]
        classes[:] = 0
        if loop > 0:
            half = 0.5 * loop
            legend += [((56, 142, 60), f"At zero tension, surplus under {half:g}%"),
                       ((142, 36, 170), f"At zero tension, surplus {half:g} to {loop:g}%"),
                       ((194, 24, 91), f"Loop risk: surplus {loop:g}% or more")]
            with np.errstate(invalid="ignore"):
                classes[~held] = 1
                classes[~held & (surplus >= half)] = 2
                classes[~held & (surplus >= loop)] = 3
        else:
            legend += [((56, 142, 60), "At zero tension")]
            classes[~held] = 1
        legend.append(((40, 40, 40), "Not held by friction (slides)"))
        classes[np.asarray(result.unheld, dtype=bool)] = len(legend) - 1
        note = (f"Surplus over a {window:,.0f} m window, over the route, as the loop-risk check measures it "
                "(Reporting limits).")
    elif mode == TENSION:
        npts = np.asarray(st.npts_kn, dtype=float)
        zero = t <= c.conform_below_kn
        top = pl.TAUT_CAP_KN * 0.999
        if np.all(np.isfinite(npts) & (npts > 0)):
            with np.errstate(invalid="ignore", divide="ignore"):
                pct = t / npts * 100.0
            edges = [25.0, 50.0, 75.0, 100.0]
            legend = [((150, 150, 150), f"Zero (under {c.conform_below_kn:g} kN)"),
                      ((0, 150, 136), "Under 25% of NPTS"), ((30, 136, 229), "25 to 50%"),
                      ((63, 81, 181), "50 to 75%"), ((142, 36, 170), "75 to 100%"),
                      ((211, 47, 47), "Above NPTS")]
            classes[:] = 1 + np.searchsorted(edges, np.where(np.isfinite(pct), pct, 0.0), side="right")
        else:
            edges = [1.0, 2.0, 5.0, 10.0, 20.0]
            legend = [((150, 150, 150), f"Zero (under {c.conform_below_kn:g} kN)"),
                      ((0, 150, 136), "Under 1 kN"), ((30, 136, 229), "1 to 2 kN"), ((63, 81, 181), "2 to 5 kN"),
                      ((94, 53, 177), "5 to 10 kN"), ((142, 36, 170), "10 to 20 kN"), ((194, 24, 91), "20 kN or more")]
            classes[:] = 1 + np.searchsorted(edges, np.where(np.isfinite(t), t, 0.0), side="right")
            note = "NPTS is not set for every cable here: tension in kN bands."
        classes[zero] = 0
        if np.any(t >= top):
            legend.append(((0, 0, 0), "No finite tension (cable short of the seabed)"))
            classes[t >= top] = len(legend) - 1
    elif mode == SOURCE:
        order = [(pl.SOURCE_LAID, (30, 136, 229), "Laid at the bottom tension"),
                 (pl.SOURCE_TAUT, (194, 24, 91), "Pulled taut: the slack laid is short of the seabed"),
                 (pl.SOURCE_SLOPE, (142, 36, 170), "Raised by a slope (friction holds it)"),
                 (pl.SOURCE_ZERO, (56, 142, 60), "At zero tension (lying slack)")]
        legend = [(colour, label) for _s, colour, label in order]
        source = np.asarray(result.source, dtype=int)
        for k, (value, _colour, _label) in enumerate(order):
            classes[source == value] = k
    else:
        return np.full(n, -1, dtype=int), [], ""
    classes[~np.isfinite(np.asarray(result.cable_m, dtype=float))] = -1
    return classes, legend[:MAX_CLASSES], note


def class_runs(classes: np.ndarray) -> List[Tuple[int, int, int]]:
    """``(first segment, last segment, class)`` runs; segment ``i`` joins stations ``i`` and ``i + 1``
    and takes station ``i``'s class."""
    seg = np.asarray(classes, dtype=int)[:-1]
    if not len(seg):
        return []
    change = np.nonzero(np.diff(seg))[0] + 1
    starts = np.concatenate(([0], change))
    ends = np.concatenate((change - 1, [len(seg) - 1]))
    return [(int(a), int(b), int(seg[a])) for a, b in zip(starts, ends) if seg[a] >= 0]


def _thin(values: np.ndarray, s0: int, s1: int, stride: int) -> np.ndarray:
    """Station indices ``s0 .. s1`` thinned to every ``stride``-th, keeping each chunk's highest and
    lowest value (and both ends)."""
    idx = np.arange(s0, s1 + 1)
    if stride <= 1 or len(idx) <= 2 * stride:
        return idx
    m = (len(idx) // stride) * stride
    body = idx[:m].reshape(-1, stride)
    v = values[body]
    rows = np.arange(len(body))
    lo = body[rows, np.argmin(np.where(np.isfinite(v), v, np.inf), axis=1)]
    hi = body[rows, np.argmax(np.where(np.isfinite(v), v, -np.inf), axis=1)]
    return np.unique(np.concatenate(([s0], lo, hi, idx[m:], [s1])))


def class_paths(kp: np.ndarray, depth: np.ndarray, runs: Sequence[Tuple[int, int, int]], lo: float, hi: float,
                width_px: int) -> Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """``{class: (x, y, connect)}`` for the runs in ``lo .. hi`` (``connect[i]``: join point i to i + 1)."""
    kp = np.asarray(kp, dtype=float)
    depth = np.asarray(depth, dtype=float)
    n = len(kp)
    if n < 2 or not runs:
        return {}
    j0 = max(int(np.searchsorted(kp, lo)) - 1, 0)
    j1 = min(int(np.searchsorted(kp, hi)) + 1, n - 1)
    stride = max(1, (j1 - j0) // max(2 * int(width_px), 1))
    parts: Dict[int, List[Tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}
    for a, b, k in runs:
        s0, s1 = max(a, j0), min(b + 1, j1)
        if s1 <= s0:
            continue
        idx = _thin(depth, s0, s1, stride)
        x, y = kp[idx], depth[idx]
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 2:
            continue
        # Join consecutive finite points only; a gap (no seabed) breaks the line.
        join = ok[:-1] & ok[1:]
        x, y = x[ok], y[ok]
        keep = np.nonzero(ok)[0]
        connect = np.zeros(len(x), dtype=bool)
        connect[:-1] = join[keep[:-1]] if len(keep) > 1 else False
        parts.setdefault(k, []).append((x, y, connect))
    out = {}
    for k, pieces in parts.items():
        out[k] = (np.concatenate([p[0] for p in pieces]), np.concatenate([p[1] for p in pieces]),
                  np.concatenate([p[2] for p in pieces]))
    return out
