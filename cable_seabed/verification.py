# -*- coding: utf-8 -*-
"""Closed-form checks the user can see: a flat seabed with one depression.

A cable at constant horizontal tension ``T`` (submerged weight ``w``) over a
trough ``b`` wide at the seabed and ``d`` deep, with walls steeper than the
cable:

* the cable sags into the trough as a parabola from rim to rim, with
  mid-span sag ``s = w b^2 / 8T``;
* if ``s < d`` it spans the trough: one span ``b`` long, gap ``d - s`` at the centre;
* if ``s >= d`` it touches the bottom: two spans, each ``sqrt(2 d T / w)``
  from the rim to touchdown, and no gap at the centre.

The span grows with tension until it bridges the trough: the monotonicity of
Binda & Contro (1979). :func:`depression_check` compares a run of the
planning model with these values when the scenario is such a case (one
depression on a flat seabed, one cable type, no bodies, constant bottom
tension).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .features import DEPRESSION

TOLERANCE = 0.03


@dataclass
class CheckRow:
    quantity: str
    closed_form: float
    model: float
    unit: str

    @property
    def difference_pct(self) -> float:
        if not math.isfinite(self.model) or not math.isfinite(self.closed_form):
            return math.nan
        if abs(self.closed_form) < 1e-9:
            return 0.0 if abs(self.model) < 0.01 else math.inf
        return (self.model - self.closed_form) / abs(self.closed_form) * 100.0

    @property
    def ok(self) -> bool:
        if abs(self.closed_form) < 1e-9:
            return abs(self.model) < 0.02
        return abs(self.difference_pct) <= TOLERANCE * 100.0


@dataclass
class Verification:
    title: str
    regime: str
    rows: List[CheckRow]

    @property
    def ok(self) -> bool:
        return all(row.ok for row in self.rows)


def depression_check(result) -> Optional[Verification]:
    """Closed form against the model for one depression on a flat seabed, else None."""
    st = result.stations
    features = getattr(st, "features", []) or []
    if len(features) != 1 or features[0].kind != DEPRESSION or st.bodies:
        return None
    if len(set(int(i) for i in st.cable_index)) != 1:
        return None
    control = result.control
    if control.mode != "tension" or control.sections:
        return None
    base = st.raw_elev_m - st.feature_m
    if not np.isfinite(base).all() or float(np.ptp(base)) > 0.01:
        return None
    feature = features[0]
    t_n = float(control.value) * 1000.0
    w = float(st.w_npm[0])
    b, d = float(feature.length_m), float(feature.height_m)
    if not (t_n > 0 and w > 0 and b > 0 and d > 0):
        return None
    centre = int(np.argmin(np.abs(st.kp_km - feature.kp_km)))
    gap_centre = float(result.gap_m[centre])
    near = [s for s in result.spans if s.kp_end >= feature.kp_km - b / 2000.0 - 0.001
            and s.kp_start <= feature.kp_km + b / 2000.0 + 0.001]
    sag = w * b * b / (8.0 * t_n)
    rows = [CheckRow("Mid-span sag w b²/8T", sag, float(-(result.cable_m[centre] - base[centre])), "m")]
    if sag < d:
        regime = f"spans the trough (sag {sag:.2f} m < depth {d:.2f} m)"
        rows.append(CheckRow("Spans", 1, len(near), ""))
        rows.append(CheckRow("Span length (rim to rim)", b, near[0].length_m if near else math.nan, "m"))
        rows.append(CheckRow("Gap at the centre d - sag", d - sag, gap_centre, "m"))
    else:
        regime = f"touches the bottom (sag {sag:.2f} m ≥ depth {d:.2f} m)"
        half = math.sqrt(2.0 * d * t_n / w)
        rows[0] = CheckRow("Drop to the bottom", d, float(-(result.cable_m[centre] - base[centre])), "m")
        rows.append(CheckRow("Spans", 2, len(near), ""))
        for k, span in enumerate(near[:2]):
            rows.append(CheckRow(f"Span {k + 1} length sqrt(2dT/w)", half, span.length_m, "m"))
        rows.append(CheckRow("Gap at the centre", 0.0, gap_centre, "m"))
    title = (f"Flat seabed, depression {b:g} m wide and {d:g} m deep, cable {w:.1f} N/m at "
             f"{t_n / 1000.0:g} kN")
    return Verification(title, regime, rows)
