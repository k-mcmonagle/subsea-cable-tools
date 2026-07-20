# -*- coding: utf-8 -*-
"""Deployment-schedule optimisation for the full BU deployment.

Pure Python + NumPy; no Qt/QGIS imports.

Strategy (pragmatic, engineering-grade):

* The nominal five-phase schedule comes from :func:`scenarios.
  default_bu_schedule` (payout : speed ratio sets the laydown slack).
* The whole operation translates with the vessel start position (exactly on
  a flat bed, to first order on real bathymetry), so the BU landing target
  is hit by simulating a **preview-quality** run, measuring the landing
  error vector and shifting the start (and the pre-laid leg ends, which are
  defined relative to the jointing position) by minus that error. One or
  two rounds land within tolerance.
* Tension / bend-radius limits are checked over the preview snapshots and
  reported as warnings on the result — the final ops run should always be
  re-simulated at full quality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

from .control import TensionBalanceController
from .scenarios import PhaseRow, bu_full_deployment, default_bu_schedule
from .timeline import OperationSimulator, SimOptions, SimResult


@dataclass
class DeploymentLimits:
    """Operational limits checked over the preview run (0 = don't check)."""

    max_tension_kN: float = 0.0
    min_bend_radius_m: float = 0.0
    max_leg_imbalance_kN: float = 0.0


@dataclass
class OptimizeResult:
    schedule: List[PhaseRow]
    vessel_start_xy: Tuple[float, float]
    laid_end_1_xy: Tuple[float, float]
    laid_end_2_xy: Tuple[float, float]
    predicted_landing_xy: Tuple[float, float]
    landing_error_m: float
    rounds: int
    warnings: List[str] = field(default_factory=list)
    preview: Optional[SimResult] = None


def _landing_xy(res: SimResult) -> Optional[Tuple[float, float]]:
    for snap in reversed(res.snapshots):
        if "BU" in snap.junction_xyz:
            x, y, _ = snap.junction_xyz["BU"]
            return (float(x), float(y))
    return None


def _check_limits(res: SimResult, limits: DeploymentLimits, warnings: List[str]):
    t_max = 0.0
    t_max_chain = ""
    r_min = float("inf")
    imb_max = 0.0
    for snap in res.snapshots:
        for c in snap.chains:
            if c.top_tension_kN > t_max:
                t_max, t_max_chain = float(c.top_tension_kN), c.name
            r_min = min(r_min, float(c.min_radius_m))
        c1, c2 = snap.chain("leg1"), snap.chain("leg2")
        if c1 is not None and c2 is not None:
            imb_max = max(imb_max, abs(c1.top_tension_kN - c2.top_tension_kN))
    if limits.max_tension_kN > 0 and t_max > limits.max_tension_kN:
        warnings.append(
            f"Peak top tension {t_max:.1f} kN on '{t_max_chain}' exceeds the "
            f"{limits.max_tension_kN:.1f} kN limit."
        )
    if limits.min_bend_radius_m > 0 and r_min < limits.min_bend_radius_m:
        warnings.append(
            f"Minimum bend radius {r_min:.1f} m violates the "
            f"{limits.min_bend_radius_m:.1f} m limit."
        )
    if limits.max_leg_imbalance_kN > 0 and imb_max > limits.max_leg_imbalance_kN:
        warnings.append(
            f"Peak leg imbalance {imb_max:.2f} kN exceeds the "
            f"{limits.max_leg_imbalance_kN:.2f} kN tolerance."
        )
    if any(not s.converged for s in res.snapshots):
        n_bad = sum(1 for s in res.snapshots if not s.converged)
        warnings.append(
            f"{n_bad} preview substep(s) did not fully converge — re-run the "
            "final schedule at full quality."
        )


def optimize_bu_schedule(
    bathy,
    params: Dict,
    target_landing_xy: Tuple[float, float],
    *,
    schedule: Optional[List[PhaseRow]] = None,
    limits: Optional[DeploymentLimits] = None,
    balance: bool = True,
    tol_m: float = 10.0,
    max_rounds: int = 3,
    preview_options: Optional[SimOptions] = None,
    progress=None,
) -> OptimizeResult:
    """Place the operation so the BU lands on ``target_landing_xy``.

    ``params`` are the keyword arguments of
    :func:`scenarios.bu_full_deployment` **except** ``schedule`` (the
    positional assemblies/defaults must be included: ``leg1_assembly``,
    ``leg2_assembly``, ``trunk_assembly``, ``defaults``). The vessel start
    and both laid ends are translated together between rounds. Returns the
    translated geometry, the (possibly default) schedule and preview-run
    warnings; simulate the returned set-up at full quality for final
    numbers.
    """
    limits = limits or DeploymentLimits()
    p = dict(params)
    leg1_asm = p.pop("leg1_assembly")
    leg2_asm = p.pop("leg2_assembly")
    trunk_asm = p.pop("trunk_assembly")
    defaults = p.pop("defaults")

    start_xy = np.asarray(p.pop("vessel_xy", target_landing_xy), dtype=float)
    end1 = np.asarray(p.pop("laid_end_1_xy"), dtype=float)
    end2 = np.asarray(p.pop("laid_end_2_xy"), dtype=float)
    target = np.asarray(target_landing_xy, dtype=float)

    warnings: List[str] = []
    res = None
    landing = None
    err = float("inf")
    rounds = 0
    for rounds in range(1, int(max_rounds) + 1):
        rows = schedule
        if rows is None:
            rows = default_bu_schedule(
                depth_m=float(bathy.depth_at(*start_xy)),
                tail_length_m=float(p.get("tail_length_m", 90.0)),
                tail_leg1_m=p.get("tail_leg1_m"),
                tail_leg2_m=p.get("tail_leg2_m"),
                tail_trunk_m=p.get("tail_trunk_m"),
                payout_mps=float(p.get("payout_mps", 0.4)),
                lay_speed_mps=float(p.get("lay_speed_mps", 0.3)),
                course_deg=float(p.get("vessel_heading_deg", 0.0)),
            )
        scn = bu_full_deployment(
            bathy, leg1_asm, leg2_asm, trunk_asm, defaults,
            vessel_xy=(float(start_xy[0]), float(start_xy[1])),
            laid_end_1_xy=(float(end1[0]), float(end1[1])),
            laid_end_2_xy=(float(end2[0]), float(end2[1])),
            schedule=rows,
            **p,
        )
        opts = preview_options or SimOptions.preview()
        if balance and opts.controller is None:
            opts.controller = TensionBalanceController("leg1", "leg2")
        sub_progress = None
        if progress is not None:
            base = (rounds - 1) / float(max_rounds)
            span = 1.0 / float(max_rounds)
            sub_progress = (lambda f, lbl, _b=base, _s=span:
                            progress(_b + _s * f, f"preview {rounds}: {lbl}"))
        res = OperationSimulator(scn, bathy, opts).run(sub_progress)
        landing = _landing_xy(res)
        if landing is None:
            warnings.append(
                "Preview run never overboarded the BU — check the schedule "
                "(no 'overboard_bu' phase?)."
            )
            break
        err_vec = np.asarray(landing) - target
        err = float(np.hypot(err_vec[0], err_vec[1]))
        if err <= tol_m:
            break
        start_xy = start_xy - err_vec
        end1 = end1 - err_vec
        end2 = end2 - err_vec
    if landing is not None and err > tol_m:
        warnings.append(
            f"Landing error {err:.1f} m still exceeds the {tol_m:.0f} m "
            f"tolerance after {rounds} preview round(s)."
        )
    if res is not None:
        _check_limits(res, limits, warnings)
    if schedule is None and res is not None:
        # Report the schedule actually used in the last round.
        schedule = rows
    return OptimizeResult(
        schedule=list(schedule or []),
        vessel_start_xy=(float(start_xy[0]), float(start_xy[1])),
        laid_end_1_xy=(float(end1[0]), float(end1[1])),
        laid_end_2_xy=(float(end2[0]), float(end2[1])),
        predicted_landing_xy=(float(landing[0]), float(landing[1])) if landing else (float("nan"), float("nan")),
        landing_error_m=err if landing else float("nan"),
        rounds=rounds,
        warnings=warnings,
        preview=res,
    )
