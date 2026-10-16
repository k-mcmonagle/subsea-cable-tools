# -*- coding: utf-8 -*-
"""Residual tension of a laid cable: lay history held by Coulomb seabed friction.

Axial equilibrium of the cable along its length ``s`` (Coulomb friction, a
rigid seabed):

    dT/ds = w dy/ds - f,      |f| <= mu N        on the seabed
    dT/ds = w dy/ds                              in a free span

``y`` is the cable elevation, so in a span ``T - w y`` is constant (the
catenary result ``T_top = T_bottom + w h``). ``N`` is the seabed reaction per
metre: ``w cos(theta)`` plus ``T kappa`` where the cable bends over a crest
(the capstan, or Euler-Eytelwein, term: over a turn ``dtheta`` with no
weight, ``T2 / T1 = exp(mu dtheta)``). Across each segment the tension
change therefore lies in a band ``[g - c, g + c]`` (gravity ``g``, friction
capacity ``c``).

The cable is laid progressively: at each touchdown the vessel deposits it
at the deposit (bottom) tension, and the cable already on the seabed keeps
the tension it was laid at unless friction cannot hold it (Zajac 1957,
residual tension on a rough bottom). This is a rigid-plastic process: each
new touchdown drags the tension behind it into the friction band reachable
from the touchdown, and leaves the rest. :func:`lay_history_tension` marches
the touchdown along the route in the lay direction. It captures:

* friction reach: a step in deposit tension decays at ``mu w`` per metre;
* slopes steeper than the friction angle (``tan theta > mu``): the cable
  slides; tension builds up-slope, and where the band would need a
  negative tension the cable cannot be held at all (slack collects there);
* the capstan grip over crests.

Tension never goes below zero: a cable cannot carry compression.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import numpy as np

_CHUNK = 512


def station_reactions(x_m: np.ndarray, y_m: np.ndarray, w_npm: np.ndarray, h_n: np.ndarray,
                      contact: np.ndarray) -> np.ndarray:
    """Seabed reaction per station (N) for a cable resting in shape ``y``.

    ``R_i = w_i L_i - H_i (slope_out - slope_in)``, with ``L_i`` the station's
    tributary length and ``H`` the horizontal tension (N). Zero off the
    seabed and never negative.
    """
    x = np.asarray(x_m, dtype=float)
    y = np.asarray(y_m, dtype=float)
    n = len(x)
    out = np.zeros(n)
    if n < 2:
        return out
    dx = np.diff(x)
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = np.where(dx > 0, np.diff(y) / dx, 0.0)
    slope = np.where(np.isfinite(slope), slope, 0.0)
    trib = np.empty(n)
    trib[0], trib[-1] = 0.5 * dx[0], 0.5 * dx[-1]
    trib[1:-1] = 0.5 * (dx[1:] + dx[:-1])
    turn = np.zeros(n)
    turn[1:-1] = slope[1:] - slope[:-1]
    h = np.where(np.isfinite(h_n), h_n, 0.0)
    w = np.where(np.isfinite(w_npm), w_npm, 0.0)
    reaction = w * trib - h * turn
    out = np.where(np.asarray(contact, dtype=bool), np.maximum(reaction, 0.0), 0.0)
    return out


def friction_band(x_m: np.ndarray, y_m: np.ndarray, w_npm: np.ndarray, mu: np.ndarray,
                  reaction_n: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(lower, upper, gravity)`` per segment (N): the band of ``T[i+1] - T[i]``.

    Gravity ``g = w (y[i+1] - y[i])``; friction capacity is ``mu R`` at each
    station, half to each neighbouring segment.
    """
    y = np.asarray(y_m, dtype=float)
    w = np.where(np.isfinite(w_npm), w_npm, 0.0)
    m = np.where(np.isfinite(mu), mu, 0.0)
    dy = np.diff(np.where(np.isfinite(y), y, 0.0))
    gravity = 0.5 * (w[1:] + w[:-1]) * dy
    capacity = m * np.where(np.isfinite(reaction_n), reaction_n, 0.0)
    seg = 0.5 * (capacity[1:] + capacity[:-1])
    return gravity - seg, gravity + seg, gravity


def lay_history_tension(deposit_n: np.ndarray, lower_n: np.ndarray, upper_n: np.ndarray,
                        gravity_n: Optional[np.ndarray] = None, direction: int = 1,
                        max_nodes: int = 20000) -> Tuple[np.ndarray, np.ndarray]:
    """Residual tension (N) after laying in ``direction`` (+1: increasing index).

    ``deposit_n`` is the tension at touchdown per station; ``lower_n`` /
    ``upper_n`` (per segment) bound ``T[i+1] - T[i]`` (:func:`friction_band`).
    Returns ``(tension_n, unheld)``: ``unheld`` marks stations where friction
    would need a negative tension to hold the cable (it slides and slack
    collects).

    Long routes march on at most ``max_nodes`` nodes with the band integrated
    exactly between them; between nodes the frictionless part (``gravity_n``,
    per segment) is restored, so spans keep ``T - w y`` constant.
    """
    td = np.asarray(deposit_n, dtype=float)
    lo = np.asarray(lower_n, dtype=float)
    hi = np.asarray(upper_n, dtype=float)
    n = len(td)
    if n == 0:
        return np.array([]), np.array([], dtype=bool)
    grav = np.zeros(max(n - 1, 0)) if gravity_n is None else np.asarray(gravity_n, dtype=float)
    td = np.where(np.isfinite(td), td, 0.0)
    if direction < 0:
        tension, unheld = lay_history_tension(td[::-1], -hi[::-1], -lo[::-1], -grav[::-1], 1, max_nodes)
        return tension[::-1].copy(), unheld[::-1].copy()
    a_cum = np.concatenate(([0.0], np.cumsum(lo)))
    b_cum = np.concatenate(([0.0], np.cumsum(hi)))
    g_cum = np.concatenate(([0.0], np.cumsum(grav)))
    stride = max(1, int(math.ceil(n / float(max_nodes))))
    nodes = np.arange(0, n, stride)
    if nodes[-1] != n - 1:
        nodes = np.append(nodes, n - 1)
    ac, bc, tdc = a_cum[nodes], b_cum[nodes], td[nodes]
    k_count = len(nodes)
    t = np.zeros(k_count)
    unheld = np.zeros(k_count, dtype=bool)
    tl = t  # alias for speed in the quick check
    for k in range(k_count):
        tk = float(tdc[k])
        tl[k] = tk
        if k == 0:
            continue
        # Quick check: the last node already within the band from this touchdown.
        low = tk - (bc[k] - bc[k - 1])
        high = tk - (ac[k] - ac[k - 1])
        prev = tl[k - 1]
        if low <= prev <= high and high >= 0.0:
            continue
        end = k
        while end > 0:
            start = max(0, end - _CHUNK)
            seg = t[start:end]
            low = tk - (bc[k] - bc[start:end])
            high = tk - (ac[k] - ac[start:end])
            new = np.minimum(np.maximum(seg, low), high)
            neg = new < 0.0
            if neg.any():
                unheld[start:end] |= neg
                new = np.maximum(new, 0.0)
            changed = np.abs(new - seg) > 1e-9
            same = np.nonzero(~changed)[0]
            if same.size:
                stop = int(same[-1])
                t[start + stop + 1:end] = new[stop + 1:]
                break
            t[start:end] = new
            end = start
    if stride == 1:
        return t, unheld
    # Between nodes: keep the march's tension, restoring the exact gravity part.
    psi = t - g_cum[nodes]
    index = np.arange(n, dtype=float)
    tension = np.maximum(np.interp(index, nodes.astype(float), psi) + g_cum, 0.0)
    fine_unheld = np.zeros(n, dtype=bool)
    for j, node in enumerate(nodes):
        if unheld[j]:
            a = nodes[j - 1] if j > 0 else node
            b = nodes[j + 1] if j + 1 < len(nodes) else node
            fine_unheld[a:b + 1] = True
    return tension, fine_unheld
