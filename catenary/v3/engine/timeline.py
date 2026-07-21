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
    sagged_shape,
)
from .solver3d import SolveResult, solve_system


@dataclass
class Attachment:
    """How a chain end is held.

    kind:
      * ``vessel`` — follows the vessel chute (fixed each step).
      * ``sheave`` — follows the named sheave of the vessel geometry
        (offsets rotate with the vessel heading).
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
    sheave: str = ""                # for 'sheave'


@dataclass
class SheaveSpec:
    """An overboarding point in the vessel frame: ``fwd_m`` along the
    heading, ``stbd_m`` to starboard, ``height_m`` above the waterline."""

    fwd_m: float = 0.0
    stbd_m: float = 0.0
    height_m: float = 5.0


@dataclass
class VesselGeometry:
    """Named sheave positions. The default single sheave ``main`` at the
    vessel reference point reproduces the legacy chute behaviour."""

    sheaves: Dict[str, SheaveSpec] = field(
        default_factory=lambda: {"main": SheaveSpec()})

    def sheave_xyz(self, vessel_xy: Tuple[float, float], heading_deg: float,
                   name: str) -> Tuple[float, float, float]:
        try:
            sp = self.sheaves[name]
        except KeyError:
            raise KeyError(
                f"Unknown sheave {name!r}; defined: {sorted(self.sheaves)}")
        h = math.radians(float(heading_deg))
        ch_, sh_ = math.cos(h), math.sin(h)
        # Math frame (CCW positive): starboard is clockwise of the heading.
        x = vessel_xy[0] + sp.fwd_m * ch_ + sp.stbd_m * sh_
        y = vessel_xy[1] + sp.fwd_m * sh_ - sp.stbd_m * ch_
        return (x, y, sp.height_m)


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
    # A joint on the line as a MATERIAL coordinate: metres of cable from the
    # bottom (far/laid) end. Fixed for the whole operation — paying out moves
    # the joint outboard along the span, hauling in brings it back over the
    # sheave. None = no joint to track.
    joint_s_from_bottom_m: Optional[float] = None

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
class Event:
    """A discrete topology change applied at the start of a step.

    kind:
      * ``set_top`` — re-pin ``chain``'s top end to ``attachment``.
      * ``add_junction`` — spawn ``junction``; when ``at_sheave`` is given
        its position is resolved at apply time: horizontally under that
        sheave at ``depth_m`` below the surface.
      * ``add_chain`` — add ``chain_state``; an empty ``shape`` is
        synthesised from its resolved end positions.
      * ``release`` — drop ``chain`` from the system (cast off).
      * ``min_length`` — grow ``chain`` to at least ``length_m`` (no-op if
        already longer). Used when material that was on deck necessarily
        goes overboard with an event — e.g. the BU tails: overboarding the
        BU takes each leg's tail over the side whether or not it was paid
        out first. The label is reported only when length is actually
        added.
    """

    kind: str
    chain: str = ""
    attachment: Optional[Attachment] = None
    junction: Optional[JunctionState] = None
    chain_state: Optional["ChainState"] = None
    at_sheave: str = ""
    depth_m: float = 2.0
    length_m: Optional[float] = None
    label: str = ""


@dataclass
class SheaveTransfer:
    """Move a chain's top from one sheave to another, lerped across the
    step's substeps so the solver never sees a beam-width jump."""

    chain: str
    from_sheave: str
    to_sheave: str


@dataclass
class Step:
    """One scripted command interval."""

    duration_s: float
    vessel_course_deg: float = 0.0      # movement direction (0 = +x, CCW)
    vessel_speed_mps: float = 0.0
    payout_mps: Dict[str, float] = field(default_factory=dict)
    release_chains: List[str] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)
    transfer: Optional[SheaveTransfer] = None
    label: str = ""
    # When True the vessel translates without turning onto its course (a
    # crab / lateral move); heading is left unchanged. Default False keeps
    # the historic behaviour of steering onto the movement direction.
    keep_heading: bool = False


@dataclass
class Scenario:
    chains: Dict[str, ChainState]
    junctions: Dict[str, JunctionState] = field(default_factory=dict)
    vessel_xy: Tuple[float, float] = (0.0, 0.0)
    vessel_heading_deg: float = 0.0
    vessel_geom: VesselGeometry = field(default_factory=VesselGeometry)
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
    # Skip the rate-drag re-solve when the fastest node moves slower than
    # this — quadratic drag at such speeds is far below the weight scale,
    # so the second solve would reproduce the first.
    rate_drag_min_v: float = 0.05
    tol: float = 3e-3
    max_iters: int = 40000
    settle_tol: float = 2e-3
    settle_max_iters: int = 120000
    # Cold-start acceleration: settle first on a ~3x coarser mesh and use
    # that equilibrium as the fine-mesh seed (identical converged physics —
    # the final solve always runs at full resolution and tolerance).
    coarse_settle: bool = True
    # Global element-length multiplier applied at build time (>1 = coarser).
    # Used by the coarse settle pass and by preview-quality runs.
    mesh_scale: float = 1.0
    # Optional payout controller (duck-typed, e.g. control.
    # TensionBalanceController): ``rates(base_dict, last_snapshot) -> dict``
    # called every substep to redistribute the step's payout rates.
    controller: Optional[object] = None
    # Optional UI hooks, forwarded into every inner solve: ``cancel()``
    # stops the relaxation early; ``solver_progress(iters, residual)``
    # reports within-solve progress.
    cancel: Optional[Callable[[], bool]] = None
    solver_progress: Optional[Callable[[int, float], None]] = None

    @classmethod
    def preview(cls, **over) -> "SimOptions":
        """Fast, coarse settings for schedule optimisation previews — NOT
        for final results (coarser mesh, looser tolerances, bigger steps)."""
        kw = dict(
            max_move_m=20.0, tol=5e-3, max_iters=20000,
            settle_tol=4e-3, settle_max_iters=60000,
            rate_drag_min_v=0.15, mesh_scale=3.0,
        )
        kw.update(over)
        return cls(**kw)


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
    # Interpolated position of the chain's joint (see ChainState.
    # joint_s_from_bottom_m); None while the joint is still inboard.
    joint_xyz: Optional[Tuple[float, float, float]] = None


def joint_point(st: Optional[ChainState], xyz: "np.ndarray",
                s: "np.ndarray") -> Optional[Tuple[float, float, float]]:
    """Where the chain's joint sits on the solved span.

    The joint is a fixed material point ``joint_s_from_bottom_m`` metres
    from the bottom end. The material coordinate is mapped proportionally
    onto the discretised arc (whose sampled length can fall slightly short
    of the true deployed length), so a joint exactly at the sheave shows at
    the top node rather than vanishing to rounding. Returns None while the
    joint is inboard (not yet paid past the sheave) or the chain has no
    joint.
    """
    if st is None or st.joint_s_from_bottom_m is None or len(s) < 2:
        return None
    L = float(st.length_m)
    if L <= 0.0:
        return None
    frac = 1.0 - float(st.joint_s_from_bottom_m) / L
    if frac < 0.0 or frac > 1.0:
        return None
    s_top = float(s[-1]) * frac
    s = np.asarray(s, dtype=float)
    xyz = np.asarray(xyz, dtype=float)
    return (float(np.interp(s_top, s, xyz[:, 0])),
            float(np.interp(s_top, s, xyz[:, 1])),
            float(np.interp(s_top, s, xyz[:, 2])))


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
    # Payout rates actually applied this substep (post-controller), m/s.
    payout_mps: Dict[str, float] = field(default_factory=dict)

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
        self._transfer: Optional[SheaveTransfer] = None
        self._transfer_frac = 0.0
        # Payout rates applied in the current substep (post-controller).
        self._applied_payout: Dict[str, float] = {}
        # Chains already warned about unactionable payout (no winch on top).
        self._payout_warned: set = set()
        self._last_snap: Optional[Snapshot] = None

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
            if att.kind == "sheave":
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
            if self.opt.mesh_scale > 1.0:
                n = max(st.min_elems, int(math.ceil(n / self.opt.mesh_scale)))
            shape = resample_polyline(st.shape, n)
            # Pin endpoint coordinates to their attachments.
            if st.top.kind in ("vessel", "sheave"):
                shape[0] = self._top_xyz(st)
            elif st.top.kind == "fixed" and st.top.xyz is not None:
                shape[0] = st.top.xyz
            if st.bottom.kind == "sheave":
                shape[-1] = self._sheave_xyz(st.bottom.sheave)
            elif st.bottom.kind == "fixed" and st.bottom.xyz is not None:
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

    def _sheave_xyz(self, name: str) -> "np.ndarray":
        return np.asarray(self.sc.vessel_geom.sheave_xyz(
            self.sc.vessel_xy, self.sc.vessel_heading_deg, name), dtype=float)

    def _top_xyz(self, st: ChainState) -> "np.ndarray":
        """Resolved top position for a vessel/sheave-held chain, including
        the in-progress sheave transfer lerp."""
        if st.top.kind == "vessel":
            return self._chute_xyz(st.top)
        tr = self._transfer
        if tr is not None and tr.chain == st.name:
            a = self._sheave_xyz(tr.from_sheave)
            b = self._sheave_xyz(tr.to_sheave)
            f = min(1.0, max(0.0, self._transfer_frac))
            return a + (b - a) * f
        return self._sheave_xyz(st.top.sheave)

    # -- stepping -----------------------------------------------------------

    def settle(self) -> Snapshot:
        """Solve the initial equilibrium (no motion, no rate drag).

        With ``coarse_settle`` a first pass runs on a ~3x coarser mesh at a
        relaxed tolerance; its equilibrium becomes the fine-mesh seed
        (through ``_absorb`` -> ``ChainState.shape``). The returned snapshot
        always comes from the full-resolution, full-tolerance solve.
        """
        if self.opt.coarse_settle and self.opt.mesh_scale == 1.0:
            self.opt.mesh_scale = 3.0
            try:
                sysm, jnode = self._build()
                res = solve_system(
                    sysm, self.bathy,
                    rho_water=self.opt.rho_water, current_at=self.opt.current_at,
                    tol=3.0 * self.opt.settle_tol,
                    max_iters=max(1000, self.opt.settle_max_iters // 3),
                    cancel=self.opt.cancel, progress=self.opt.solver_progress,
                )
                self._absorb(res, jnode)
            finally:
                self.opt.mesh_scale = 1.0
        sysm, jnode = self._build()
        res = solve_system(
            sysm, self.bathy,
            rho_water=self.opt.rho_water, current_at=self.opt.current_at,
            tol=self.opt.settle_tol, max_iters=self.opt.settle_max_iters,
            cancel=self.opt.cancel, progress=self.opt.solver_progress,
        )
        self._absorb(res, jnode)
        snap = self._snapshot(res, jnode, label="settle")
        self._last_snap = snap
        return snap

    def run(self, progress: Optional[Callable[[float, str], bool]] = None) -> SimResult:
        """Run the full script. ``progress(frac, label) -> continue?``."""
        out = SimResult()
        out.snapshots.append(self.settle())
        total_t = sum(s.duration_s for s in self.sc.steps) or 1.0
        done_t = 0.0
        for step in self.sc.steps:
            self._apply_events(step, out)
            for name in step.release_chains:
                self._released.add(name)
            self._transfer = step.transfer
            n_sub = self._substeps(step)
            dt = step.duration_s / n_sub
            for k in range(n_sub):
                self._transfer_frac = (k + 1) / n_sub
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
            if step.transfer is not None:
                # Transfer complete: the chain now lives on the destination
                # sheave (the lerp already walked its top there).
                st = self.sc.chains.get(step.transfer.chain)
                if st is not None:
                    st.top = Attachment("sheave", sheave=step.transfer.to_sheave)
                self._transfer = None
        return out

    def _apply_events(self, step: Step, out: SimResult):
        """Apply the step's discrete topology changes to the scenario."""
        for ev in step.events:
            if ev.kind == "release":
                self._released.add(ev.chain)
            elif ev.kind == "set_top":
                if ev.attachment is None:
                    raise ValueError("set_top event needs an attachment")
                self.sc.chains[ev.chain].top = ev.attachment
            elif ev.kind == "add_junction":
                if ev.junction is None:
                    raise ValueError("add_junction event needs a junction")
                j = ev.junction
                if ev.at_sheave:
                    sx, sy, _sz = self.sc.vessel_geom.sheave_xyz(
                        self.sc.vessel_xy, self.sc.vessel_heading_deg, ev.at_sheave)
                    j = JunctionState(j.name, (sx, sy, -abs(ev.depth_m)),
                                      load_kN=j.load_kN, cda_m2=j.cda_m2)
                self.sc.junctions[j.name] = j
            elif ev.kind == "add_chain":
                if ev.chain_state is None:
                    raise ValueError("add_chain event needs a chain_state")
                st = ev.chain_state
                if st.shape is None or len(st.shape) < 2:
                    st.shape = sagged_shape(
                        self._resolve_end_xyz(st.top),
                        self._resolve_end_xyz(st.bottom),
                        max(8, st.min_elems), slack_frac=0.02,
                    )
                self.sc.chains[st.name] = st
            elif ev.kind == "min_length":
                st = self.sc.chains.get(ev.chain)
                need = float(ev.length_m or 0.0)
                if st is not None and st.length_m < need - 1e-9:
                    st.length_m = need
                    if ev.label:
                        out.warnings.append(f"t={self._t:.0f} s: {ev.label}")
                continue    # label reported above only when length was added
            else:
                raise ValueError(f"Unknown event kind {ev.kind!r}")
            if ev.label:
                out.warnings.append(f"t={self._t:.0f} s: {ev.label}")

    def _resolve_end_xyz(self, att: Attachment) -> "np.ndarray":
        if att.kind == "vessel":
            return self._chute_xyz(att)
        if att.kind == "sheave":
            return self._sheave_xyz(att.sheave)
        if att.kind == "junction":
            return np.asarray(self.sc.junctions[att.junction].xyz, dtype=float)
        if att.kind == "fixed" and att.xyz is not None:
            return np.asarray(att.xyz, dtype=float)
        raise ValueError(f"Cannot resolve a position for attachment kind {att.kind!r}")

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
        if step.vessel_speed_mps > 0 and not step.keep_heading:
            self.sc.vessel_heading_deg = step.vessel_course_deg
        # Pay out / haul in. Only chains whose top end is held on board
        # (vessel / sheave) have a winch: once a chain has been re-topped
        # onto a junction (the BU legs after overboard) its scheduled payout
        # is ignored — there is nothing left holding it.
        self._applied_payout = {}
        ignored: List[str] = []
        for name, rate in self._payout_rates(step).items():
            if name in self._released or name not in self.sc.chains:
                continue
            st = self.sc.chains[name]
            if st.top.kind not in ("vessel", "sheave"):
                if abs(rate) > 1e-12 and name not in self._payout_warned:
                    self._payout_warned.add(name)
                    ignored.append(name)
                continue
            st.length_m = max(st.target_ds_m * 2.0, st.length_m + rate * dt)
            self._applied_payout[name] = float(rate)
        self._t += dt

        prev_shapes = {
            n: (st.shape.copy(), st.length_m) for n, st in self.sc.chains.items()
            if n not in self._released
        }
        snap = self._equilibrate(step, dt, prev_shapes)
        for name in ignored:
            snap.warnings.append(
                f"Payout for '{name}' ignored from t={self._t:.0f} s — its "
                f"top end is attached to "
                f"'{self.sc.chains[name].top.junction or self.sc.chains[name].top.kind}'"
                ", not a vessel winch (e.g. legs after the BU is overboard).")
        return snap

    def _equilibrate(self, step: Step, dt: float, prev_shapes) -> Snapshot:
        """Solve the current scenario state to equilibrium and snapshot it.
        Subclasses may substitute a different equilibrium backend (e.g. the
        analytic quick model) while reusing all the stepping machinery."""
        sysm, jnode = self._build()
        res = solve_system(
            sysm, self.bathy,
            rho_water=self.opt.rho_water, current_at=self.opt.current_at,
            tol=self.opt.tol, max_iters=self.opt.max_iters,
            cancel=self.opt.cancel, progress=self.opt.solver_progress,
        )
        if self.opt.rate_drag and dt > 0:
            v = self._estimate_velocities(sysm, res, prev_shapes, dt)
            if v is not None and float(np.max(np.linalg.norm(v, axis=1))) >= self.opt.rate_drag_min_v:
                res = solve_system(
                    sysm, self.bathy,
                    rho_water=self.opt.rho_water, current_at=self.opt.current_at,
                    node_velocity=v, tol=self.opt.tol, max_iters=self.opt.max_iters,
                    warm_X=res.X,
                    cancel=self.opt.cancel, progress=self.opt.solver_progress,
                )
        self._absorb(res, jnode)
        snap = self._snapshot(res, jnode, label=step.label)
        self._last_snap = snap
        return snap

    def _payout_rates(self, step: Step) -> Dict[str, float]:
        """Effective payout rates for this substep: the step's base rates,
        optionally redistributed by the balance controller."""
        ctrl = self.opt.controller
        if ctrl is None:
            return step.payout_mps
        return ctrl.rates(step.payout_mps, self._last_snap)

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
                joint_xyz=joint_point(self.sc.chains.get(c.name), c.xyz, c.s),
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
            payout_mps=dict(self._applied_payout),
        )
