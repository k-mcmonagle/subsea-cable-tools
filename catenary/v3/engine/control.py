# -*- coding: utf-8 -*-
"""Leg-balance control for two-line deployments (BU legs on sheaves).

Pure Python + NumPy; no Qt/QGIS imports.

Two tiers, mirroring how a lay crew actually works:

* :class:`TensionBalanceController` — the in-loop "winch operator": each
  substep it redistributes the step's total payout between two chains in
  proportion to their sheave-tension error. Costs no extra solves.
* :func:`balance_leg_lengths` — the initial trim: a secant root-find on the
  deployed-length skew that equalises the two sheave tensions before the
  operation starts (the "vessel balances the legs" set-up move).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np

from .timeline import OperationSimulator, Snapshot


@dataclass
class TensionBalanceController:
    """Proportional payout-skew controller between two chains.

    When both chains appear in a step's payout dict, the step's *total*
    payout is preserved and split so the higher-tension chain pays out
    faster (paying out slackens a leg). The skew is rate-limited to
    ``max_skew_frac`` of the per-chain base rate so the controller can trim
    but never reverse a payout.
    """

    chain_a: str
    chain_b: str
    gain_mps_per_kN: float = 0.02
    max_skew_frac: float = 0.3

    def rates(self, base: Dict[str, float], last: Optional[Snapshot]) -> Dict[str, float]:
        if last is None or self.chain_a not in base or self.chain_b not in base:
            return base
        ca = last.chain(self.chain_a)
        cb = last.chain(self.chain_b)
        if ca is None or cb is None:
            return base
        total = base[self.chain_a] + base[self.chain_b]
        half = 0.5 * total
        err_kN = float(ca.top_tension_kN) - float(cb.top_tension_kN)
        skew = self.gain_mps_per_kN * err_kN
        lim = abs(self.max_skew_frac * half)
        skew = max(-lim, min(lim, skew))
        out = dict(base)
        out[self.chain_a] = half + skew
        out[self.chain_b] = half - skew
        return out


def tdp_tension_kN(snap: Optional[Snapshot], chain: str) -> Optional[float]:
    """Touchdown tension of ``chain`` in a snapshot: the tension at its
    first bed-contact node. None if the chain is absent or fully suspended."""
    if snap is None:
        return None
    c = snap.chain(chain)
    if c is None:
        return None
    contact = np.asarray(c.contact, dtype=bool)
    if not contact.any():
        return None
    i = int(np.argmax(contact))
    t = np.asarray(c.tension_kN, dtype=float)
    return float(t[min(i, len(t) - 1)])


@dataclass
class BottomTensionController:
    """Proportional payout trim holding a chain's touchdown tension.

    Each substep the chain's payout rate is trimmed around the step's base
    rate: touchdown tension above target pays out faster (slackening the
    span), below target pays out slower (letting the span tighten). The trim
    is limited to ``max_trim_frac`` of the base rate, so the controller can
    modulate a scripted payout but never reverse or stop it. Idle until the
    chain has bed contact (e.g. the trunk before the BU nears the bed).
    """

    chain: str
    target_kN: float
    gain_mps_per_kN: float = 0.02
    max_trim_frac: float = 0.5

    def rates(self, base: Dict[str, float], last: Optional[Snapshot]) -> Dict[str, float]:
        if self.chain not in base:
            return base
        tdp = tdp_tension_kN(last, self.chain)
        if tdp is None:
            return base
        rate = base[self.chain]
        trim = self.gain_mps_per_kN * (tdp - float(self.target_kN))
        lim = abs(self.max_trim_frac * rate)
        trim = max(-lim, min(lim, trim))
        out = dict(base)
        out[self.chain] = rate + trim
        return out


@dataclass
class CompositeController:
    """Chain several payout controllers; each sees the previous one's rates.

    Controllers acting on disjoint chains compose exactly (e.g. a leg
    :class:`TensionBalanceController` plus a trunk
    :class:`BottomTensionController`).
    """

    controllers: list

    def rates(self, base: Dict[str, float], last: Optional[Snapshot]) -> Dict[str, float]:
        out = base
        for c in self.controllers:
            out = c.rates(out, last)
        return out


def balance_leg_lengths(
    sim: OperationSimulator,
    chain_a: str,
    chain_b: str,
    *,
    tol_kN: float = 0.5,
    max_iters: int = 6,
    mps_per_kN: float = 0.5,
    max_shift_m: float = 50.0,
) -> Snapshot:
    """Trim the deployed length of ``chain_a`` (pay out / haul in at its
    sheave) until the two chains' sheave tensions match within ``tol_kN``.

    Secant iteration on ``f(d) = T_a(d) - T_b(d)`` where ``d`` is the extra
    length given to ``chain_a``; each evaluation is a warm-started settle
    (the scenario keeps the previous equilibrium shapes). Returns the final
    balanced snapshot; the scenario's chain lengths are left trimmed.
    """
    sc = sim.sc

    def err(snap: Snapshot) -> float:
        ca = snap.chain(chain_a)
        cb = snap.chain(chain_b)
        if ca is None or cb is None:
            raise ValueError("balance_leg_lengths: both chains must exist")
        return float(ca.top_tension_kN) - float(cb.top_tension_kN)

    snap = sim.settle()
    f_prev = err(snap)
    if abs(f_prev) < tol_kN:
        return snap
    # Coarse settle already did its job on the first call; later trims are
    # warm and should go straight to the fine mesh.
    coarse_saved = sim.opt.coarse_settle
    sim.opt.coarse_settle = False
    try:
        d_prev = 0.0
        # Higher tension on A -> lengthen A (paying out slackens it).
        d = max(-max_shift_m, min(max_shift_m, mps_per_kN * f_prev))
        for _ in range(int(max_iters)):
            sc.chains[chain_a].length_m += (d - d_prev)
            snap = sim.settle()
            f = err(snap)
            if abs(f) < tol_kN:
                break
            if abs(f - f_prev) < 1e-9:
                break
            d_next = d - f * (d - d_prev) / (f - f_prev)
            d_next = max(-max_shift_m, min(max_shift_m, d_next))
            d_prev, f_prev = d, f
            d = d_next
    finally:
        sim.opt.coarse_settle = coarse_saved
    return snap
