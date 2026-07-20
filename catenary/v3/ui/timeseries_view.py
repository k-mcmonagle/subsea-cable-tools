# -*- coding: utf-8 -*-
"""Time-series plots for operation runs (tensions, balance, BU descent).

Consumes ``timeline.Snapshot`` lists via the pure helper
:func:`snapshots_to_series` (unit-testable without Qt); renders through the
plugin's pyqtgraph-backed plot shim like the other 2D views. A vertical
cursor tracks the timeline scrubber.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

import numpy as np

from .views2d import _ShimHolder, make_canvas


def snapshots_to_series(snapshots) -> dict:
    """Flatten a snapshot list into aligned time series.

    Returns a dict with ``t`` (s) and per-chain arrays (NaN where the chain
    does not exist at that time): ``top_tension[name]``, ``payout[name]``,
    plus ``leg_imbalance`` (leg1 - leg2 top tension), ``bu_z`` (BU elevation)
    and ``layback_bu`` (horizontal vessel-BU distance) where applicable.
    """
    n = len(snapshots)
    t = np.array([s.t_s for s in snapshots], dtype=float)
    names: List[str] = []
    for s in snapshots:
        for c in s.chains:
            if c.name not in names:
                names.append(c.name)
    top = {name: np.full(n, np.nan) for name in names}
    payout = {name: np.full(n, np.nan) for name in names}
    imbalance = np.full(n, np.nan)
    bu_z = np.full(n, np.nan)
    layback = np.full(n, np.nan)
    for i, s in enumerate(snapshots):
        for c in s.chains:
            top[c.name][i] = c.top_tension_kN
        for name, r in (s.payout_mps or {}).items():
            if name in payout:
                payout[name][i] = r
        c1, c2 = s.chain("leg1"), s.chain("leg2")
        if c1 is not None and c2 is not None:
            imbalance[i] = c1.top_tension_kN - c2.top_tension_kN
        xyz = s.junction_xyz.get("BU")
        if xyz is not None:
            bu_z[i] = xyz[2]
            layback[i] = math.hypot(xyz[0] - s.vessel_xy[0], xyz[1] - s.vessel_xy[1])
    return {
        "t": t,
        "names": names,
        "top_tension": top,
        "payout": payout,
        "leg_imbalance": imbalance,
        "bu_z": bu_z,
        "layback_bu": layback,
        "labels": [s.label or "" for s in snapshots],
    }


class TimeSeriesView:
    """Stacked tension / balance / descent plots with a time cursor."""

    def __init__(self):
        self.figure, self.canvas = make_canvas()
        self._series: Optional[dict] = None
        self._t_cursor: Optional[float] = None

    def widget(self):
        return self.canvas

    def set_snapshots(self, snapshots) -> None:
        self._series = snapshots_to_series(snapshots) if snapshots else None
        self._redraw()

    def clear(self) -> None:
        self._series = None
        self._redraw()

    def set_time(self, t_s: Optional[float]) -> None:
        self._t_cursor = t_s
        self._redraw()

    def _redraw(self) -> None:
        self.figure.clear()
        ser = self._series
        if ser is None or len(ser["t"]) == 0:
            ax = self.figure.add_subplot(111)
            ax.set_title("Run an operation simulation to see time series")
            self.canvas.draw()
            return
        shim = _ShimHolder.mod()
        t = ser["t"]
        has_bu = bool(np.any(np.isfinite(ser["bu_z"])))
        n_rows = 3 if has_bu else 2

        ax1 = self.figure.add_subplot(n_rows * 100 + 11)
        for i, name in enumerate(ser["names"]):
            y = ser["top_tension"][name]
            if np.any(np.isfinite(y)):
                ax1.plot(t, y, color=shim.get_tab10_color(i), linewidth=1.6, label=name)
        if np.any(np.isfinite(ser["leg_imbalance"])):
            ax1.plot(t, np.abs(ser["leg_imbalance"]), color="#d62728", linewidth=1.2,
                     linestyle="--", label="|leg1-leg2|")
        ax1.set_ylabel("Top tension (kN)")
        try:
            ax1.legend()
        except Exception:
            pass

        ax2 = self.figure.add_subplot(n_rows * 100 + 12)
        for i, name in enumerate(ser["names"]):
            y = ser["payout"][name]
            if np.any(np.isfinite(y)):
                ax2.plot(t, y, color=shim.get_tab10_color(i), linewidth=1.4, label=name)
        ax2.set_ylabel("Payout (m/s)")
        try:
            ax2.legend()
        except Exception:
            pass

        axes = [ax1, ax2]
        if has_bu:
            ax3 = self.figure.add_subplot(n_rows * 100 + 13)
            ax3.plot(t, ser["bu_z"], color="#2ca02c", linewidth=1.6, label="BU elevation")
            if np.any(np.isfinite(ser["layback_bu"])):
                ax3.plot(t, ser["layback_bu"], color="#9467bd", linewidth=1.2,
                         linestyle="--", label="BU layback")
            ax3.set_ylabel("BU z / layback (m)")
            try:
                ax3.legend()
            except Exception:
                pass
            axes.append(ax3)

        axes[-1].set_xlabel("Time (s)")
        if self._t_cursor is not None:
            for ax in axes:
                try:
                    ax.axvline(float(self._t_cursor), color="#888888",
                               linewidth=1.0, linestyle=":")
                except Exception:
                    pass
        self.canvas.draw()
