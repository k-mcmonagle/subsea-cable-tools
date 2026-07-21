# -*- coding: utf-8 -*-
"""Manual (interactive) BU-deployment driver.

Pure Python + NumPy; no Qt/QGIS imports.

Where :class:`timeline.OperationSimulator` steps a *pre-built* schedule, this
module lets the operator drive the same simulator **one command at a time**:
translate the vessel (fore/aft/port/stbd, or a range + bearing), rotate the
heading, pay out / pick up each cable lead, and trigger the discrete events
(overboard the BU, transfer leg 2 to the port sheave) when they choose. Each
command re-solves to equilibrium and returns a snapshot; with the analytic
:class:`quick_bu.QuickOperationSimulator` backend every solve is sub-millisecond,
so a UI can re-solve synchronously on every button press.

The command handling reuses the simulator's own per-step machinery
(``_apply_events``, ``_advance`` / ``_equilibrate``, the sheave-transfer lerp),
so a manually driven deployment is physically identical to the scripted one —
only the source of the commands differs. History is kept so the run can be
undone, reset, replayed, and folded back into a :class:`scenarios.PhaseRow`
schedule for a full-quality re-simulation.

Frame: engine math degrees (0 = +x/east, CCW positive), heading-unit
``(cos h, sin h)`` and starboard-unit ``(sin h, -cos h)`` to match
:meth:`timeline.VesselGeometry.sheave_xyz`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import math
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .timeline import (
    Attachment,
    Event,
    OperationSimulator,
    SheaveTransfer,
    SimResult,
    Snapshot,
    Step,
)


@dataclass
class ManualCommand:
    """One operator action. A command may combine a translate, a payout and
    at most one event; a heading set is applied first (rotate in place).

    * ``fwd_m`` / ``stbd_m`` — vessel-frame translation (crab; heading is not
      changed by the move).
    * ``heading_set_deg`` — if given, set the vessel heading (math deg) before
      the move and re-solve.
    * ``payout_m`` — per-chain length change (+ pay out / − pick up), metres.
    * ``event`` — ``""``, ``"overboard_bu"`` or ``"transfer"``.
    """

    fwd_m: float = 0.0
    stbd_m: float = 0.0
    heading_set_deg: Optional[float] = None
    payout_m: Dict[str, float] = field(default_factory=dict)
    event: str = ""
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "fwd_m": float(self.fwd_m),
            "stbd_m": float(self.stbd_m),
            "heading_set_deg": (None if self.heading_set_deg is None
                                else float(self.heading_set_deg)),
            "payout_m": {k: float(v) for k, v in self.payout_m.items()},
            "event": self.event,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ManualCommand":
        h = d.get("heading_set_deg", None)
        return cls(
            fwd_m=float(d.get("fwd_m", 0.0)),
            stbd_m=float(d.get("stbd_m", 0.0)),
            heading_set_deg=None if h is None else float(h),
            payout_m={str(k): float(v) for k, v in (d.get("payout_m") or {}).items()},
            event=str(d.get("event", "")),
            label=str(d.get("label", "")),
        )


class ManualBUController:
    """Drive an :class:`OperationSimulator` interactively.

    ``sim`` should already be constructed over the manual scenario (e.g. a
    :class:`quick_bu.QuickOperationSimulator`). The scenario's scripted
    ``steps`` are consumed only to discover the deployment's discrete events
    (the overboard-BU event bundle and the leg-2 sheave transfer); manual mode
    then clears them, so nothing runs automatically. ``nominal_speed_mps`` sets
    the timescale a move/payout command is stepped over (only its ratio to the
    distance matters — it controls sub-stepping, not physics).
    """

    def __init__(self, sim: OperationSimulator, *,
                 nominal_speed_mps: float = 0.5,
                 target_xy: Optional[Tuple[float, float]] = None):
        self.sim = sim
        self.nominal_speed_mps = max(float(nominal_speed_mps), 1e-3)
        self.target_xy = tuple(target_xy) if target_xy is not None else None
        # Discover the scripted event bundles, then disarm the schedule.
        self._overboard_events: List[Event] = []
        self._transfer: Optional[SheaveTransfer] = None
        for st in sim.sc.steps:
            if st.events and not self._overboard_events:
                self._overboard_events = list(st.events)
            if st.transfer is not None and self._transfer is None:
                self._transfer = st.transfer
        sim.sc.steps = []
        # Pristine scenario snapshot: manual driving mutates the live scenario
        # (vessel moves, chains grow, BU/trunk spawn), so settle/reset/replay
        # restore this deep copy to return to the initial conditions.
        self._pristine = copy.deepcopy(sim.sc)
        self._overboarded = False
        self._transferred = False
        self.history: List[ManualCommand] = []
        self._length0: Dict[str, float] = {}
        self.count_offset: Dict[str, float] = {}
        self._settle_snap: Optional[Snapshot] = None

    # -- lifecycle ----------------------------------------------------------

    def settle(self) -> Snapshot:
        """Restore the initial conditions and solve them; resets history.

        The live scenario is replaced with a fresh deep copy of the pristine
        state so any moves / payout / spawned BU+trunk from a previous run are
        undone."""
        self.sim.sc = copy.deepcopy(self._pristine)
        self.sim._released = set()
        self.sim._t = 0.0
        self.sim._transfer = None
        self.sim._transfer_frac = 0.0
        self.sim._applied_payout = {}
        self.sim._last_snap = None
        if hasattr(self.sim, "_landed"):
            self.sim._landed = False
        if hasattr(self.sim, "reset_lay_history"):
            self.sim.reset_lay_history()
        self._overboarded = False
        self._transferred = False
        self.history = []
        # Length baselines are recomputed for the fresh state; the user's
        # count offsets are intentionally preserved across reset / replay.
        self._length0 = {}
        snap = self.sim.settle()
        self._settle_snap = snap
        self._capture_baselines()
        return snap

    def _capture_baselines(self):
        for name, st in self.sim.sc.chains.items():
            self._length0.setdefault(name, float(st.length_m))
            self.count_offset.setdefault(name, 0.0)

    # -- introspection ------------------------------------------------------

    @property
    def can_overboard(self) -> bool:
        return bool(self._overboard_events) and not self._overboarded

    @property
    def can_transfer(self) -> bool:
        return self._transfer is not None and not self._transferred

    def active_leads(self) -> List[str]:
        """Chains still in the operation (count displays), stable order
        (trunk last)."""
        names = [n for n in self.sim.sc.chains if n not in self.sim._released]
        return sorted(names, key=lambda n: (n == "trunk", n))

    def payable_leads(self) -> List[str]:
        """Chains a winch can actually pay out / haul in: their top end is
        still held on board. Once the BU is overboarded the legs are topped
        on the junction — only the trunk remains payable."""
        names = [n for n, st in self.sim.sc.chains.items()
                 if n not in self.sim._released
                 and st.top.kind in ("vessel", "sheave")]
        return sorted(names, key=lambda n: (n == "trunk", n))

    def cable_count(self, name: str) -> float:
        """Live roto-style count: offset + net cable deployed (m)."""
        st = self.sim.sc.chains.get(name)
        if st is None:
            return self.count_offset.get(name, 0.0)
        base = self._length0.get(name, float(st.length_m))
        return self.count_offset.get(name, 0.0) + (float(st.length_m) - base)

    def deployed_since_start(self, name: str) -> float:
        st = self.sim.sc.chains.get(name)
        if st is None:
            return 0.0
        return float(st.length_m) - self._length0.get(name, float(st.length_m))

    def set_offset(self, name: str, metres: float):
        self.count_offset[name] = float(metres)

    def vessel_xy(self) -> Tuple[float, float]:
        return tuple(self.sim.sc.vessel_xy)

    def heading_deg(self) -> float:
        return float(self.sim.sc.vessel_heading_deg)

    def bu_xyz(self) -> Optional[Tuple[float, float, float]]:
        j = self.sim.sc.junctions.get("BU")
        return tuple(j.xyz) if j is not None else None

    def target_error(self) -> Optional[Tuple[float, float]]:
        """(range_m, bearing_deg math) from the BU (or the vessel before
        overboarding) to the target; None if no target set."""
        if self.target_xy is None:
            return None
        ref = self.bu_xyz()
        rx, ry = (ref[0], ref[1]) if ref is not None else self.vessel_xy()
        dx, dy = self.target_xy[0] - rx, self.target_xy[1] - ry
        return float(math.hypot(dx, dy)), float(math.degrees(math.atan2(dy, dx)))

    # -- commands -----------------------------------------------------------

    def apply(self, cmd: ManualCommand) -> Snapshot:
        """Apply one command and return the resulting equilibrium snapshot."""
        if cmd.heading_set_deg is not None:
            self.sim.sc.vessel_heading_deg = float(cmd.heading_set_deg)

        step = self._step_for(cmd)
        snap = self._run_step(step)
        if cmd.event == "overboard_bu":
            self._overboarded = True
        elif cmd.event == "transfer":
            self._transferred = True
        self.history.append(cmd)
        self._capture_baselines()
        return snap

    def move_range_bearing(self, range_m: float, bearing_math_deg: float,
                           **over) -> ManualCommand:
        """Build (not apply) a translate command from an absolute range +
        bearing (math deg). Convert to the vessel frame so a later heading
        change does not move it."""
        b = math.radians(float(bearing_math_deg))
        dx, dy = range_m * math.cos(b), range_m * math.sin(b)
        h = math.radians(self.heading_deg())
        # Project the world displacement onto heading / starboard axes.
        fwd = dx * math.cos(h) + dy * math.sin(h)
        stbd = dx * math.sin(h) - dy * math.cos(h)
        return ManualCommand(fwd_m=fwd, stbd_m=stbd, **over)

    def undo(self) -> Snapshot:
        """Drop the last command and deterministically replay the rest."""
        if not self.history:
            return self._settle_snap or self.settle()
        cmds = self.history[:-1]
        return self.replay(cmds)

    def reset(self) -> Snapshot:
        return self.settle()

    def replay(self, cmds: List[ManualCommand]) -> Snapshot:
        snap = self.settle()
        for c in cmds:
            snap = self.apply(c)
        return snap

    # -- internals ----------------------------------------------------------

    def _world_disp(self, cmd: ManualCommand) -> Tuple[float, float]:
        h = math.radians(self.heading_deg())
        ch, sh = math.cos(h), math.sin(h)
        # heading-unit (ch, sh); starboard-unit (sh, -ch).
        dx = cmd.fwd_m * ch + cmd.stbd_m * sh
        dy = cmd.fwd_m * sh - cmd.stbd_m * ch
        return dx, dy

    def _step_for(self, cmd: ManualCommand) -> Step:
        dx, dy = self._world_disp(cmd)
        dist = math.hypot(dx, dy)
        payout = {k: float(v) for k, v in cmd.payout_m.items() if v != 0.0}
        # Timescale: long enough that neither the move nor any payout exceeds
        # the simulator's per-substep cap in one substep-free step (the
        # substep loop still refines it); ratios preserve distance / dL.
        span = max(dist, max((abs(v) for v in payout.values()), default=0.0))
        dt = max(span / self.nominal_speed_mps, 1e-3)
        course = math.degrees(math.atan2(dy, dx)) if dist > 1e-9 else self.heading_deg()
        step = Step(
            duration_s=dt,
            vessel_course_deg=course,
            vessel_speed_mps=(dist / dt) if dist > 1e-9 else 0.0,
            payout_mps={k: v / dt for k, v in payout.items()},
            label=cmd.label,
            keep_heading=True,           # crab; heading is set explicitly
        )
        if cmd.event == "overboard_bu":
            # Deep-copy so a replay/undo never reuses a mutated trunk template.
            step.events = copy.deepcopy(self._overboard_events)
        elif cmd.event == "transfer" and self._transfer is not None:
            step.transfer = self._transfer
        elif cmd.event:
            raise ValueError(f"Unknown manual event {cmd.event!r}")
        return step

    def _run_step(self, step: Step) -> Snapshot:
        """Mirror one iteration of :meth:`OperationSimulator.run`'s step loop."""
        sim = self.sim
        tmp = SimResult()
        sim._apply_events(step, tmp)
        for name in step.release_chains:
            sim._released.add(name)
        sim._transfer = step.transfer
        n_sub = sim._substeps(step)
        dt = step.duration_s / n_sub
        last: Optional[Snapshot] = None
        for k in range(n_sub):
            sim._transfer_frac = (k + 1) / n_sub
            last = sim._advance(step, dt)
        if step.transfer is not None:
            st = sim.sc.chains.get(step.transfer.chain)
            if st is not None:
                st.top = Attachment("sheave", sheave=step.transfer.to_sheave)
            sim._transfer = None
        if last is None:                 # empty step (heading-only): re-solve
            last = sim.settle()
        return last

    # -- export -------------------------------------------------------------

    def to_schedule(self, merge: bool = True,
                    course_tol_deg: float = 25.0) -> List["object"]:
        """Fold the history into :class:`scenarios.PhaseRow`s so the scripted
        full-quality simulator reproduces the manual plan.

        Move courses become the phase course (the scripted model steers onto
        the course, so a pure crab is approximated as a short course leg);
        pure heading-set commands with no move/payout are dropped with the
        heading carried into the next moving phase. Payout metres become rates
        over ``distance / nominal_speed`` (or a nominal hold time).

        With ``merge`` (default), consecutive event-free commands whose move
        directions agree within ``course_tol_deg`` (payout-only commands
        always agree) are folded into ONE phase that moves and pays out
        simultaneously — replaying alternating jog / payout clicks as a
        smooth combined lay rather than a jerky one-at-a-time script.
        """
        from .scenarios import PhaseRow

        # One entry per effective command: (dx, dy, payout, event, label).
        parts: List[tuple] = []
        for i, cmd in enumerate(self.history, start=1):
            dx, dy = self._world_disp(cmd)
            dist = math.hypot(dx, dy)
            payout = {k: float(v) for k, v in cmd.payout_m.items() if v != 0.0}
            if dist < 1e-9 and not payout and not cmd.event:
                continue  # pure heading change: nothing for the scripted model
            parts.append((dx, dy, payout, cmd.event,
                          cmd.label or f"Manual step {i}"))

        # Group runs of mergeable commands. Event rows always stand alone.
        groups: List[List[tuple]] = []
        for p in parts:
            dx, dy = p[0], p[1]
            g = groups[-1] if groups else None
            if merge and not p[3] and g is not None and not g[-1][3]:
                gx = sum(q[0] for q in g)
                gy = sum(q[1] for q in g)
                turn = 0.0
                if math.hypot(dx, dy) > 1e-9 and math.hypot(gx, gy) > 1e-9:
                    turn = abs(math.degrees(
                        math.atan2(gx * dy - gy * dx, gx * dx + gy * dy)))
                if turn <= course_tol_deg:
                    g.append(p)
                    continue
            groups.append([p])

        rows: List[PhaseRow] = []
        speed = self.nominal_speed_mps
        for g in groups:
            dx = sum(p[0] for p in g)
            dy = sum(p[1] for p in g)
            dist = math.hypot(dx, dy)
            payout: Dict[str, float] = {}
            for p in g:
                for k, v in p[2].items():
                    payout[k] = payout.get(k, 0.0) + v
            payout = {k: v for k, v in payout.items() if v != 0.0}
            label = g[0][4] if len(g) == 1 else f"{g[0][4]} (+{len(g) - 1} steps)"
            if dist > 1e-9:
                duration = dist / speed
                course = math.degrees(math.atan2(dy, dx))
                spd = speed
            else:
                # Hold / payout-only / event: time it by the largest payout.
                duration = max((abs(v) for v in payout.values()), default=30.0) / speed
                duration = max(duration, 5.0)
                course = None
                spd = 0.0
            rows.append(PhaseRow(
                label=label,
                duration_s=duration,
                course_deg=course,
                speed_mps=spd,
                payout_mps={k: v / duration for k, v in payout.items()},
                event=g[0][3],
                distance_m=dist,
            ))
        return rows
