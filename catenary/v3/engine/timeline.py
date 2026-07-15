# -*- coding: utf-8 -*-
"""Quasi-static operation simulation: scripted vessel moves and pay-out
solved as a sequence of warm-started static equilibria.

Pure Python + NumPy; no Qt/QGIS imports.

Each step advances the vessel, grows/shrinks deployed chain lengths, rebuilds
the discretised system (chains are re-sampled from the previous equilibrium,
so warm starts stay cheap), and relaxes to the next equilibrium with the 3D
DR solver. Rate-dependent drag is included by an inner iteration: solve with
zero cable velocity, estimate node velocities from the displacement over the
step, and re-solve with those velocities feeding the drag term.

Validity (documented for the user): quasi-static stepping is justified when
manoeuvre times are long compared with the longitudinal wave round-trip
``2h/c1`` (~20 s in deep water, Zajac Sec. IV) — the regime of BU
deployments and bight lay-downs. Inertia and wave loading are not modelled;
with seabed friction the result is lay-history dependent by design (that is
the point of stepping).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .cable_system import (
    AssemblyItem,
    AssemblyMapper,
    CableSystem,
    Defaults,
    SystemBuilder,
    resample_polyline,
)
from .solver3d import SolveResult, solve_system


@dataclass
class Attachment:
    """How a chain end is held.

    kind:
      * ``vessel`` — follows the vessel chute (fixed each step).
      * ``fixed``  — fixed at ``xyz`` (anchor / laid end).
      * ``junction`` — ties to the named junction node.
      * ``chain_point`` — ties to the node of ``chain`` nearest arc position
        ``s_from_bottom_m`` (e.g. a lowering rope hooked to a bight apex).
      * ``free`` — unconstrained end.
    """

    kind: str
    xyz: Optional[Tuple[float, float, float]] = None
    junction: str = ""
    chain: str = ""
    s_from_bottom_m: float = 0.0
    chute_height_m: float = 5.0     # for 'vessel'


@dataclass
class ChainState:
    """Scenario-level description of one cable/rope run (mutable state)."""

    name: str
    assembly: List[AssemblyItem]
    defaults: Defaults
    length_m: float
    top: Attachment
    bottom: Attachment
    shape: "np.ndarray"             # current polyline (m, 3), top -> bottom
    target_ds_m: float = 5.0
    min_elems: int = 24
    max_elems: int = 800
    mapper_direction: str = "from_bottom"
    transport_speed_mps: float = 0.0

    def n_elems(self) -> int:
        n = int(math.ceil(self.length_m / max(self.target_ds_m, 0.1)))
        return max(self.min_elems, min(self.max_elems, n))


@dataclass
class JunctionState:
    """A shared node (e.g. a branching unit) with an optional body load."""

    name: str
    xyz: Tuple[float, float, float]
    load_kN: float = 0.0
    cda_m2: float = 0.0


@dataclass
class Step:
    """One scripted command interval."""

    duration_s: float
    vessel_course_deg: float = 0.0      # movement direction (0 = +x, CCW)
    vessel_speed_mps: float = 0.0
    payout_mps: Dict[str, float] = field(default_factory=dict)
    release_chains: List[str] = field(default_factory=list)
    label: str = ""


@dataclass
class Scenario:
    chains: Dict[str, ChainState]
    junctions: Dict[str, JunctionState] = field(default_factory=dict)
    vessel_xy: Tuple[float, float] = (0.0, 0.0)
    vessel_heading_deg: float = 0.0
    steps: List[Step] = field(default_factory=list)
    # Auto-release: drop these chains when their max tension falls below the
    # threshold (kN) — e.g. cast off a lowering rope once it goes slack.
    auto_release_kN: Dict[str, float] = field(default_factory=dict)


@dataclass
class SimOptions:
    rho_water: float = 1025.0
    current_at: Optional[Callable[["np.ndarray"], "np.ndarray"]] = None
    max_move_m: float = 10.0            # substep displacement/payout cap
    rate_drag: bool = True              # inner velocity iteration
    tol: float = 3e-3
    max_iters: int = 40000
    settle_tol: float = 2e-3
    settle_max_iters: int = 120000
    # Optional UI hooks, forwarded into every inner solve: ``cancel()``
    # stops the relaxation early; ``solver_progress(iters, residual)``
    # reports within-solve progress.
    cancel: Optional[Callable[[], bool]] = None
    solver_progress: Optional[Callable[[int, float], None]] = None


@dataclass
class ChainSnapshot:
    name: str
    xyz: "np.ndarray"
    s: "np.ndarray"
    tension_kN: "np.ndarray"
    contact: "np.ndarray"
    seg_id: "np.ndarray"
    top_tension_kN: float
    end_tension_kN: float
    min_radius_m: float
    length_m: float


@dataclass
class Snapshot:
    t_s: float
    vessel_xy: Tuple[float, float]
    vessel_heading_deg: float
    chains: List[ChainSnapshot]
    junction_xyz: Dict[str, Tuple[float, float, float]]
    converged: bool
    residual_ratio: float
    warnings: List[str] = field(default_factory=list)
    label: str = ""

    def chain(self, name: str) -> Optional[ChainSnapshot]:
        for c in self.chains:
            if c.name == name:
                return c
        return None


@dataclass
class SimResult:
    snapshots: List[Snapshot] = field(default_factory=list)
    aborted: bool = False
    warnings: List[str] = field(default_factory=list)


class OperationSimulator:
    """Steps a Scenario through its script over a bathymetry."""

    def __init__(self, scenario: Scenario, bathy, options: Optional[SimOptions] = None):
        self.sc = scenario
        self.bathy = bathy
        self.opt = options or SimOptions()
        self._released: set = set()
        self._t = 0.0

    # -- system assembly ----------------------------------------------------

    def _build(self) -> Tuple[CableSystem, Dict[str, int]]:
        """Discretise the current scenario state into a solvable system."""
        b = SystemBuilder()
        jnode: Dict[str, int] = {}
        for name, j in self.sc.junctions.items():
            jnode[name] = b.add_node(j.xyz, label=name)
            if j.load_kN:
                b.add_point_force(jnode[name], (0.0, 0.0, -j.load_kN * 1000.0))
            if j.cda_m2:
                b.add_body_drag(jnode[name], j.cda_m2)

        chain_objs: Dict[str, object] = {}
        chain_node_ranges: Dict[str, "np.ndarray"] = {}

        def end_node_for(att: Attachment, endpoint_xyz) -> Tuple[Optional[int], bool]:
            """(existing node id or None, fixed?)"""
            if att.kind == "vessel":
                return None, True
            if att.kind == "fixed":
                return None, True
            if att.kind == "junction":
                return jnode[att.junction], False
            if att.kind == "chain_point":
                host = chain_node_ranges.get(att.chain)
                if host is None:
                    raise ValueError(
                        f"chain_point attachment references '{att.chain}' which is "
                        "not built yet — order chains so hosts come first."
                    )
                host_state = self.sc.chains[att.chain]
                s_from_top = host_state.length_m - att.s_from_bottom_m
                ds = host_state.length_m / (len(host) - 1)
                k = int(round(s_from_top / max(ds, 1e-9)))
                k = max(0, min(len(host) - 1, k))
                return int(host[k]), False
            return None, False  # free

        # Hosts (no chain_point deps) first, then dependents.
        ordered = sorted(
            [c for n, c in self.sc.chains.items() if n not in self._released],
            key=lambda c: (c.top.kind == "chain_point" or c.bottom.kind == "chain_point"),
        )
        for st in ordered:
            n = st.n_elems()
            shape = resample_polyline(st.shape, n)
            # Pin endpoint coordinates to their attachments.
            if st.top.kind == "vessel":
                shape[0] = self._chute_xyz(st.top)
            elif st.top.kind == "fixed" and st.top.xyz is not None:
                shape[0] = st.top.xyz
            if st.bottom.kind == "fixed" and st.bottom.xyz is not None:
                shape[-1] = st.bottom.xyz
            mapper = AssemblyMapper(st.assembly, st.defaults, st.mapper_direction)
            start_id, top_fixed = end_node_for(st.top, shape[0])
            end_id, bottom_fixed = end_node_for(st.bottom, shape[-1])
            ch = b.add_chain(
                st.name, mapper, st.length_m, n, shape,
                start_node=start_id, end_node=end_id,
                window_lo_m=0.0, transport_speed_mps=st.transport_speed_mps,
            )
            if top_fixed:
                b.set_fixed(int(ch.idx[0]))
            if bottom_fixed:
                b.set_fixed(int(ch.idx[-1]))
            chain_objs[st.name] = ch
            chain_node_ranges[st.name] = ch.idx
        sysm = b.build()
        # Ensure junction/fixed coordinates are exact after build.
        for name, j in self.sc.junctions.items():
            sysm.X[jnode[name]] = j.xyz
        return sysm, jnode

    def _chute_xyz(self, att: Optional[Attachment] = None):
        h = att.chute_height_m if att is not None else 5.0
        return np.array([self.sc.vessel_xy[0], self.sc.vessel_xy[1], h])

    # -- stepping -----------------------------------------------------------

    def settle(self) -> Snapshot:
        """Solve the initial equilibrium (no motion, no rate drag)."""
        sysm, jnode = self._build()
        res = solve_system(
            sysm, self.bathy,
            rho_water=self.opt.rho_water, current_at=self.opt.current_at,
            tol=self.opt.settle_tol, max_iters=self.opt.settle_max_iters,
            cancel=self.opt.cancel, progress=self.opt.solver_progress,
        )
        self._absorb(res, jnode)
        return self._snapshot(res, jnode, label="settle")

    def run(self, progress: Optional[Callable[[float, str], bool]] = None) -> SimResult:
        """Run the full script. ``progress(frac, label) -> continue?``."""
        out = SimResult()
        out.snapshots.append(self.settle())
        total_t = sum(s.duration_s for s in self.sc.steps) or 1.0
        done_t = 0.0
        for step in self.sc.steps:
            for name in step.release_chains:
                self._released.add(name)
            n_sub = self._substeps(step)
            dt = step.duration_s / n_sub
            for k in range(n_sub):
                snap = self._advance(step, dt)
                out.snapshots.append(snap)
                done_t += dt
                # Auto-release: cast off named chains once the load at their
                # *attached* (bottom) end drops below the threshold. Max
                # tension would never trigger for a lowering rope, whose top
                # always carries its own hanging weight.
                for cname, thresh_kN in list(self.sc.auto_release_kN.items()):
                    if cname in self._released:
                        continue
                    snap_c = snap.chain(cname)
                    if snap_c is not None and float(snap_c.end_tension_kN) < thresh_kN:
                        self._released.add(cname)
                        msg = (
                            f"'{cname}' released (max tension below {thresh_kN} kN) "
                            f"at t={self._t:.0f} s."
                        )
                        snap.warnings.append(msg)
                        out.warnings.append(msg)
                if progress is not None:
                    if not progress(min(1.0, done_t / total_t), step.label or ""):
                        out.aborted = True
                        return out
        return out

    def _substeps(self, step: Step) -> int:
        move = step.vessel_speed_mps * step.duration_s
        pay = max((abs(r) * step.duration_s for r in step.payout_mps.values()), default=0.0)
        n = int(math.ceil(max(move, pay) / max(self.opt.max_move_m, 1.0)))
        return max(1, n)

    def _advance(self, step: Step, dt: float) -> Snapshot:
        # Move vessel.
        c = math.radians(step.vessel_course_deg)
        vx = step.vessel_speed_mps * math.cos(c)
        vy = step.vessel_speed_mps * math.sin(c)
        self.sc.vessel_xy = (self.sc.vessel_xy[0] + vx * dt, self.sc.vessel_xy[1] + vy * dt)
        if step.vessel_speed_mps > 0:
            self.sc.vessel_heading_deg = step.vessel_course_deg
        # Pay out / haul in.
        for name, rate in step.payout_mps.items():
            if name in self._released or name not in self.sc.chains:
                continue
            st = self.sc.chains[name]
            st.length_m = max(st.target_ds_m * 2.0, st.length_m + rate * dt)
        self._t += dt

        prev_shapes = {
            n: (st.shape.copy(), st.length_m) for n, st in self.sc.chains.items()
            if n not in self._released
        }
        sysm, jnode = self._build()
        res = solve_system(
            sysm, self.bathy,
            rho_water=self.opt.rho_water, current_at=self.opt.current_at,
            tol=self.opt.tol, max_iters=self.opt.max_iters,
            cancel=self.opt.cancel,
        )
        if self.opt.rate_drag and dt > 0:
            v = self._estimate_velocities(sysm, res, prev_shapes, dt)
            if v is not None:
                res = solve_system(
                    sysm, self.bathy,
                    rho_water=self.opt.rho_water, current_at=self.opt.current_at,
                    node_velocity=v, tol=self.opt.tol, max_iters=self.opt.max_iters,
                    warm_X=res.X,
                    cancel=self.opt.cancel,
                )
        self._absorb(res, jnode)
        return self._snapshot(res, jnode, label=step.label)

    def _estimate_velocities(self, sysm: CableSystem, res: SolveResult,
                             prev_shapes: Dict[str, Tuple["np.ndarray", float]],
                             dt: float) -> Optional["np.ndarray"]:
        """Node velocities from material-consistent displacement over dt.

        Material points are matched by arc distance from the *bottom* end
        (stable under top-end pay-out); newly deployed material takes the
        chute velocity implicitly through its zero displacement."""
        v = np.zeros_like(res.X)
        any_set = False
        for cres in res.chains:
            prev = prev_shapes.get(cres.name)
            if prev is None:
                continue
            prev_shape, prev_len_unused = prev
            seg = np.linalg.norm(np.diff(prev_shape, axis=0), axis=1)
            s_prev = np.concatenate([[0.0], np.cumsum(seg)])
            prev_arc = float(s_prev[-1])
            if prev_arc <= 0:
                continue
            # Current node arc positions from the top (geometry arc).
            segn = np.linalg.norm(np.diff(cres.xyz, axis=0), axis=1)
            s_now = np.concatenate([[0.0], np.cumsum(segn)])
            now_arc = float(s_now[-1])
            # Match from the bottom: distance-from-bottom b in [0, min arc].
            b_now = now_arc - s_now
            sample = np.clip(prev_arc - b_now, 0.0, prev_arc)
            prev_pts = np.empty_like(cres.xyz)
            for c3 in range(3):
                prev_pts[:, c3] = np.interp(sample, s_prev, prev_shape[:, c3])
            disp = cres.xyz - prev_pts
            # New material (b_now > prev_arc) had no previous position.
            new_mask = b_now > prev_arc
            disp[new_mask] = 0.0
            v[cres.idx] = disp / dt
            any_set = True
        return v if any_set else None

    def _absorb(self, res: SolveResult, jnode: Dict[str, int]):
        """Write the solved geometry back into the scenario state."""
        for cres in res.chains:
            st = self.sc.chains.get(cres.name)
            if st is not None:
                st.shape = cres.xyz.copy()
        for name, nid in jnode.items():
            self.sc.junctions[name].xyz = tuple(res.X[nid])

    def _snapshot(self, res: SolveResult, jnode: Dict[str, int], label: str = "") -> Snapshot:
        chains = [
            ChainSnapshot(
                name=c.name,
                xyz=c.xyz.copy(),
                s=c.s.copy(),
                tension_kN=c.tension_kN.copy(),
                contact=c.contact.copy(),
                seg_id=c.seg_id.copy(),
                top_tension_kN=c.top_tension_kN,
                end_tension_kN=c.end_tension_kN,
                min_radius_m=c.min_radius_m,
                length_m=float(c.s[-1]),
            )
            for c in res.chains
        ]
        return Snapshot(
            t_s=self._t,
            vessel_xy=tuple(self.sc.vessel_xy),
            vessel_heading_deg=self.sc.vessel_heading_deg,
            chains=chains,
            junction_xyz={n: tuple(res.X[i]) for n, i in jnode.items()},
            converged=res.converged,
            residual_ratio=res.residual_ratio,
            warnings=list(res.warnings),
            label=label,
        )
