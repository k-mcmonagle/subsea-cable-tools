# -*- coding: utf-8 -*-
"""Config -> engine -> displayable results, off the UI thread.

The dialog builds a :class:`V3Config` from its widgets; :class:`SolveWorker`
(QThread) runs the requested solve and returns a :class:`RunOutput` with a
ready-to-render :class:`SceneData` (or one per timeline snapshot), summary
facts for the results panel, and warnings. No engine module imports Qt; this
module is the only bridge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import traceback
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    from qgis.PyQt.QtCore import QThread, pyqtSignal
except Exception:  # pragma: no cover - standalone tests
    from PyQt5.QtCore import QThread, pyqtSignal

from ..engine import bathymetry as bathy_mod
from ..engine import cable_system as cs
from ..engine import control as ctl
from ..engine import hydrodynamics as hyd
from ..engine import scenarios as scen
from ..engine import schedule_opt as sopt
from ..engine import solver3d as s3d
from ..engine import steady_lay as sl
from ..engine import timeline as tl
from .scene import (
    BedGrid, CablePath, Marker, SceneData, VesselGlyph, compass_to_math_deg,
)

KNOT = 0.514444
KMH = 1.0 / 3.6      # km/h -> m/s


@dataclass
class V3Config:
    """Everything the engine needs, as plain data (JSON-serialisable-ish)."""

    mode: str = "static"                     # static | steady | operation | optimize
    # Environment ---------------------------------------------------------
    bathymetry: dict = field(default_factory=lambda: {"kind": "flat", "depth_m": 100.0})
    current_layers: List[dict] = field(default_factory=list)  # depth/speed/dir
    rho_water: float = 1025.0
    # Cable ---------------------------------------------------------------
    assembly: List[dict] = field(default_factory=list)        # V2-compatible JSON
    default_q_water_npm: float = 200.0
    default_diameter_m: float = 0.035
    default_cd_normal: float = 1.2
    default_cd_tangential: float = 0.01
    default_mu: float = 0.3
    default_EI_kNm2: float = 0.0
    default_mbr_m: float = 0.0
    # Vessel / lay --------------------------------------------------------
    # All bearings in the config are COMPASS degrees (clockwise from north);
    # they are converted to the engine's math frame (0 = +x/east, CCW) here.
    chute_height_m: float = 5.0
    lay_azimuth_deg: float = 0.0             # ship course; cable trails behind
    ship_speed_mps: float = 11.1 * KMH       # SI; the UI edits km/h
    slack_percent: float = 2.0
    # Parametric ship shape (drawn geometry; chute stays the engine anchor).
    # Default vessel: 127 m x 27 m with the cable departing at the aft end.
    ship_length_m: float = 127.0
    ship_beam_m: float = 27.0
    crp_fwd_m: float = 0.0                   # CRP forward of midship
    crp_stbd_m: float = 0.0                  # CRP starboard of centreline
    chute_fwd_m: float = -63.5               # chute forward of CRP (aft end)
    chute_stbd_m: float = 0.0                # chute starboard of CRP
    chute_radius_m: float = 0.0              # overboarding chute radius (drawn)
    # Static & steady solve mode ------------------------------------------
    solve_mode: str = "bottom_tension"       # + top_tension/exit_angle/layback/suspended_length
    solve_value: float = 5.0                 # kN for tensions, deg, m
    on_bed_tail_m: float = 150.0
    chute_mu: float = 0.3
    chute_wrap_deg: float = 0.0              # 0 -> derive from exit angle
    # Static configuration: a single cable span, or a held BU / bight state.
    static_config: str = "single"            # single | bu | bight
    bu_depth_m: float = 20.0                 # held BU depth below surface
    trunk_slack_pct: float = 2.0             # trunk length margin over chute-BU distance
    apex_depth_m: float = 10.0               # held bight-apex depth below surface
    # Operation scenario ----------------------------------------------------
    scenario: str = "bu_deployment"          # straight_lay | bu_deployment | final_bight | bu_full
    op: dict = field(default_factory=dict)   # scenario-specific parameters
    # Two-sheave geometry (bu_full): offsets in the vessel frame.
    sheave_fwd_m: float = -63.5
    sheave_spacing_m: float = 12.0
    # Solver ----------------------------------------------------------------
    target_ds_m: float = 5.0
    n_nodes_static: int = 300
    dr_tol: float = 2e-3


@dataclass
class RunOutput:
    mode: str
    scene: Optional[SceneData] = None                 # static/steady
    snapshots: Optional[list] = None                  # operation (timeline.Snapshot)
    scene_builder: Optional[Callable[[int], SceneData]] = None
    facts: Dict[str, str] = field(default_factory=dict)
    quick: Dict[str, str] = field(default_factory=dict)   # Zajac quick answers
    warnings: List[str] = field(default_factory=list)
    error: str = ""                                    # short, user-facing
    error_details: str = ""                            # traceback, for the log
    # Optimised deployment schedule (mode="optimize"): PhaseRow dicts plus
    # the translated set-up geometry for the dialog to adopt.
    schedule: Optional[List[dict]] = None
    optimized_setup: Optional[dict] = None


# ---------------------------------------------------------------------------
# Config -> engine objects
# ---------------------------------------------------------------------------

def build_bathymetry(cfg: V3Config):
    return bathy_mod.bathymetry_from_dict(cfg.bathymetry)


def build_current(cfg: V3Config) -> Optional[hyd.CurrentProfile]:
    layers = [
        hyd.CurrentLayer(float(l.get("depth_m", 0.0)), float(l.get("speed_mps", 0.0)),
                         compass_to_math_deg(float(l.get("direction_deg", 0.0))))
        for l in cfg.current_layers
        if float(l.get("speed_mps", 0.0)) != 0.0
    ]
    prof = hyd.CurrentProfile(layers)
    return None if prof.is_zero else prof


def build_defaults(cfg: V3Config) -> cs.Defaults:
    return cs.Defaults(
        q_water_npm=cfg.default_q_water_npm,
        q_air_npm=0.0,
        mu=cfg.default_mu,
        EI_Nm2=cfg.default_EI_kNm2 * 1000.0,
        mbr_m=cfg.default_mbr_m,
        diameter_m=cfg.default_diameter_m,
        cd_normal=cfg.default_cd_normal,
        cd_tangential=cfg.default_cd_tangential,
    )


def build_assembly(cfg: V3Config) -> List[cs.AssemblyItem]:
    items = cs.parse_assembly(cfg.assembly)
    if not any(isinstance(i, cs.SegmentSpec) and i.length_m > 0 for i in items):
        items = cs.uniform_assembly(
            50000.0, cfg.default_q_water_npm, diameter_m=cfg.default_diameter_m,
            cd_normal=cfg.default_cd_normal, cd_tangential=cfg.default_cd_tangential,
            mu=cfg.default_mu, EI_kNm2=cfg.default_EI_kNm2, name="Cable",
        )
    return items


def _representative_segment(items: Sequence[cs.AssemblyItem], defaults: cs.Defaults) -> cs.SegmentSpec:
    """First real segment — used for steady-lay (uniform-cable) inputs."""
    for it in items:
        if isinstance(it, cs.SegmentSpec) and it.length_m > 0:
            seg = it
            return cs.SegmentSpec(
                name=seg.name,
                length_m=seg.length_m,
                q_water_npm=seg.q_water_npm or defaults.q_water_npm,
                q_air_npm=seg.q_air_npm,
                friction_mu=seg.friction_mu if seg.friction_mu is not None else defaults.mu,
                diameter_m=seg.diameter_m or defaults.diameter_m,
                cd_normal=seg.cd_normal or defaults.cd_normal,
                cd_tangential=seg.cd_tangential,
                mass_kgpm=seg.mass_kgpm,
            )
    return cs.SegmentSpec(
        name="Cable", length_m=1e5, q_water_npm=defaults.q_water_npm,
        diameter_m=defaults.diameter_m, cd_normal=defaults.cd_normal,
        cd_tangential=defaults.cd_tangential,
    )


def _steady_input(cfg: V3Config, depth_m: float, ship_speed_mps: float) -> sl.SteadyLayInput:
    defaults = build_defaults(cfg)
    seg = _representative_segment(build_assembly(cfg), defaults)
    rho_c = seg.mass_kgpm
    if rho_c <= 0:
        rho_c = max(seg.q_air_npm / 9.80665 if seg.q_air_npm else 0.0,
                    seg.q_water_npm / 9.80665 + 1025.0 * math.pi / 4 * (seg.diameter_m ** 2))
    return sl.SteadyLayInput(
        depth_m=depth_m,
        q_water_npm=seg.q_water_npm,
        q_air_npm=seg.q_air_npm,
        diameter_m=seg.diameter_m,
        cd_normal=seg.cd_normal,
        cd_tangential=seg.cd_tangential,
        rho_c_kgpm=rho_c,
        ship_speed_mps=ship_speed_mps,
        payout_speed_mps=ship_speed_mps * (1.0 + cfg.slack_percent / 100.0),
        current=build_current(cfg),
        chute_height_m=cfg.chute_height_m,
        rho_water=cfg.rho_water,
    )


def _solve_value_si(cfg: V3Config) -> float:
    if cfg.solve_mode in ("bottom_tension", "top_tension"):
        return cfg.solve_value * 1000.0  # kN -> N
    return cfg.solve_value


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def _solver_progress_adapter(progress) -> Optional[Callable[[int, float], None]]:
    """Wrap the worker's ``(frac, label) -> bool`` progress callable into the
    solver's ``(iterations, residual)`` hook. frac < 0 = indeterminate."""
    if progress is None:
        return None

    def hook(iters: int, residual: float) -> None:
        progress(-1.0, f"Relaxing — iteration {iters:,}, residual {residual:.1e}")

    return hook


def _multi_segment_notice(cfg: V3Config, out: RunOutput, context: str) -> None:
    """Warn when a multi-part assembly meets a uniform-cable calculation."""
    items = cs.parse_assembly(cfg.assembly)
    n_seg = sum(1 for i in items if isinstance(i, cs.SegmentSpec) and i.length_m > 0)
    n_body = sum(1 for i in items if not isinstance(i, cs.SegmentSpec))
    if n_seg > 1 or n_body > 0:
        out.warnings.append(
            f"The assembly has {n_seg} segment(s) and {n_body} body/bodies, but "
            f"{context} uses only the first segment's properties (uniform cable)."
        )


def run_static(cfg: V3Config, cancel: Optional[Callable[[], bool]] = None,
               progress=None) -> RunOutput:
    """Static hang: single-cable span (ODE placement + 3D DR refinement) or
    a held BU / bight equilibrium when ``static_config`` says so."""
    if cfg.static_config in ("bu", "bight"):
        return run_static_hold(cfg, cancel=cancel, progress=progress)
    bathy = build_bathymetry(cfg)
    depth0 = float(bathy.depth_at(0.0, 0.0))
    ode_in = _steady_input(cfg, depth0, 0.0)
    ode_in.payout_speed_mps = 0.0
    ode = sl.solve_steady_lay(ode_in, cfg.solve_mode, _solve_value_si(cfg))

    az = math.radians(compass_to_math_deg(cfg.lay_azimuth_deg) + 180.0)  # cable trails behind
    ux, uy = math.cos(az), math.sin(az)
    layback = ode.layback_m
    tail = max(10.0, cfg.on_bed_tail_m)
    total_len = ode.suspended_length_m + tail

    defaults = build_defaults(cfg)
    items = build_assembly(cfg)
    # span_m resolves any remainder ("fill") row and records the fit so the
    # short-assembly case can be warned about below.
    mapper = cs.AssemblyMapper(items, defaults, "from_top", span_m=total_len)

    n = max(60, min(800, int(total_len / max(cfg.target_ds_m, 0.5))))
    b = cs.SystemBuilder()
    # Seed from the ODE shape (rotated to azimuth), extended along the bed.
    shape = np.zeros((n + 1, 3))
    s_seed = np.linspace(0.0, total_len, n + 1)
    ode_s = ode.s[-1] - ode.s[::-1]  # from top (chute) toward TDP
    ode_xyz = ode.xyz[::-1]
    for k, sv in enumerate(s_seed):
        if sv <= ode.s[-1]:
            x2 = np.interp(sv, ode_s, ode_xyz[:, 0])
            z2 = np.interp(sv, ode_s, ode_xyz[:, 2])
            dx = ode_xyz[0, 0] - x2  # horizontal distance from chute
            shape[k] = [dx * ux, dx * uy, z2]
        else:
            r = layback + (sv - ode.s[-1])
            shape[k] = [r * ux, r * uy, -float(bathy.depth_at(r * ux, r * uy))]
    shape[0] = [0.0, 0.0, cfg.chute_height_m]
    chain = b.add_chain("cable", mapper, total_len, n, shape)
    b.set_fixed(int(chain.idx[0]))
    b.set_fixed(int(chain.idx[-1]))
    sysm = b.build()
    anchor_r = layback + tail
    sysm.X[chain.idx[-1]] = [anchor_r * ux, anchor_r * uy,
                             -float(bathy.depth_at(anchor_r * ux, anchor_r * uy))]

    current = build_current(cfg)
    res = s3d.solve_system(
        sysm, bathy, rho_water=cfg.rho_water,
        current_at=(current.velocity_at if current else None),
        tol=cfg.dr_tol,
        cancel=cancel, progress=_solver_progress_adapter(progress),
    )
    if cancel and cancel():
        return RunOutput(mode="static", error="cancelled")

    out = RunOutput(mode="static")
    out.scene = _result_scene(cfg, bathy, res, vessel_xy=(0.0, 0.0))
    out.warnings = list(res.warnings) + list(ode.warnings)
    _multi_segment_notice(
        cfg, out,
        "the solve-target placement (the 3D refinement itself models the "
        "full assembly; check the reported tensions against the target)")
    c = res.chains[0]
    if mapper.fit.short_by_m > 1.0:
        out.warnings.append(
            f"The assembly covers {mapper.fit.total_m:.0f} m but the modelled "
            f"cable is {total_len:.0f} m — the end segment's properties were "
            f"stretched over the last {mapper.fit.short_by_m:.0f} m. Add a "
            "remainder (fill) segment or lengthen the assembly."
        )
    i_tdp = int(np.argmax(c.contact)) if np.any(c.contact) else len(c.contact) - 1
    exit_deg = abs(c.top_angle_deg)
    wrap = math.radians(cfg.chute_wrap_deg) if cfg.chute_wrap_deg > 0 else math.radians(exit_deg)
    T_top_N = c.top_tension_kN * 1000.0
    out.facts = {
        "Top tension": f"{c.top_tension_kN:.2f} kN",
        "Tension at machinery (capstan mu={:.2f})".format(cfg.chute_mu):
            f"{hyd.capstan_tension(T_top_N, cfg.chute_mu, wrap, laying=True) / 1000.0:.2f} kN",
        "Bottom (TDP) tension": f"{c.tension_kN[i_tdp]:.2f} kN",
        "Exit angle (from horizontal)": f"{exit_deg:.1f} deg",
        "Layback (horizontal to TDP)": f"{math.hypot(*c.xyz[i_tdp, :2]):.1f} m",
        "Suspended length": f"{c.s[i_tdp]:.1f} m",
        "Min bend radius": _fmt_radius(c.min_radius_m, c.min_radius_s_m),
        "Water depth at vessel": f"{depth0:.1f} m",
        "Converged": "yes" if res.converged else "NO — treat as approximate",
    }
    _mbr_check(out, res, items, defaults)
    return out


def run_steady(cfg: V3Config, cancel: Optional[Callable[[], bool]] = None) -> RunOutput:
    bathy = build_bathymetry(cfg)
    depth0 = float(bathy.depth_at(0.0, 0.0))
    V = cfg.ship_speed_mps
    inp = _steady_input(cfg, depth0, V)
    res = sl.solve_steady_lay(inp, cfg.solve_mode, _solve_value_si(cfg))

    az = math.radians(compass_to_math_deg(cfg.lay_azimuth_deg))
    ca, sa = math.cos(az), math.sin(az)
    # ODE frame: TDP at origin, ship toward +x. Rotate/translate so the
    # vessel is at local (0, 0) with the cable trailing behind.
    xyz = res.xyz.copy()
    exit_xy = xyz[-1, :2].copy()
    xyz[:, 0] -= exit_xy[0]
    xyz[:, 1] -= exit_xy[1]
    rot = np.array([[ca, -sa], [sa, ca]])
    xyz[:, :2] = xyz[:, :2] @ rot.T

    scene = SceneData(title="Steady lay (ship frame)")
    scene.bed = _bed_grid(cfg, bathy, xyz)
    tension = res.tension_N / 1000.0
    scene.cables = [CablePath(
        xyz=xyz[::-1].copy(),          # top -> TDP ordering for readouts
        name="cable",
        tension_kN=tension[::-1].copy(),
        s_m=(res.s[-1] - res.s[::-1]).copy(),
        color="#1f77b4",
    )]
    scene.markers = [Marker(tuple(xyz[0]), "TDP", "tdp")]
    scene.vessel = _vessel_glyph(cfg, (0.0, 0.0), compass_to_math_deg(cfg.lay_azimuth_deg))

    H_c = res.hydrodynamic_constant_mps
    out = RunOutput(mode="steady", scene=scene)
    out.warnings = list(res.warnings)
    _multi_segment_notice(cfg, out, "steady-lay mode")
    wrap = math.radians(cfg.chute_wrap_deg) if cfg.chute_wrap_deg > 0 else math.radians(abs(res.exit_angle_deg))
    out.facts = {
        "Ship speed": f"{cfg.ship_speed_mps / KMH:.2f} km/h",
        "Pay-out": f"{cfg.ship_speed_mps * (1 + cfg.slack_percent / 100) / KMH:.2f} km/h ({cfg.slack_percent:.1f}% slack)",
        "Bottom tension": f"{res.T0_N / 1000.0:.2f} kN",
        "Top tension": f"{res.top_tension_N / 1000.0:.2f} kN",
        "Tension at machinery (capstan mu={:.2f})".format(cfg.chute_mu):
            f"{hyd.capstan_tension(res.top_tension_N, cfg.chute_mu, wrap, laying=True) / 1000.0:.2f} kN",
        "Exit angle (from horizontal)": f"{res.exit_angle_deg:.1f} deg",
        "Layback": f"{res.layback_m:.1f} m",
        "Suspended length": f"{res.suspended_length_m:.1f} m",
        "Lateral touchdown offset": f"{-res.lateral_offset_m:.1f} m (downstream of track)",
        "Min bend radius": _fmt_radius(res.min_radius_m, res.min_radius_s_m),
    }
    out.quick = _quick_facts(inp, H_c, V, depth0)
    # Surface the mass/length feeding the transport (centrifugal) term —
    # estimated from the weights when the assembly does not specify it.
    seg = _representative_segment(build_assembly(cfg), build_defaults(cfg))
    est = " (estimated from weights — set 'mass_kgpm' in the assembly to override)" \
        if seg.mass_kgpm <= 0 else ""
    out.quick["Cable mass per length (transport term)"] = f"{inp.rho_c_kgpm:.1f} kg/m{est}"
    return out


def _op_joints(op: dict, chain: str) -> Optional[List[Tuple[str, float]]]:
    """User-defined named joints for one chain from the op dict:
    ``op["joints"] = {chain: [[label, s_from_bottom_m], ...]}``."""
    rows = (op.get("joints") or {}).get(chain) or []
    out = [(str(r[0]), float(r[1])) for r in rows if len(r) >= 2]
    return out or None


def _op_count_refs(op: dict) -> Optional[Dict[str, float]]:
    """Cable-count references from the op dict:
    ``op["count_refs"] = {chain: count_at_bottom_end_m}``."""
    refs = {str(k): float(v) for k, v in (op.get("count_refs") or {}).items()
            if v is not None}
    return refs or None


def _op_leg_lengths(cfg_op: dict, bathy) -> Tuple[float, float]:
    """Per-leg deployed lengths from the op dict (leg 2 defaults to leg 1)."""
    lengths = cfg_op.get("leg_lengths_m")
    if lengths:
        L1 = float(lengths[0])
        L2 = float(lengths[1]) if len(lengths) > 1 and lengths[1] else L1
        return L1, L2
    L1 = float(cfg_op.get("leg_length_m", 2.0 * bathy.depth_at(0.0, 0.0)))
    return L1, L1


def _lowering_inputs(cfg: V3Config, op: dict, bathy) -> Optional[dict]:
    """The BU integration's compiled inputs for the lowering scenario, or
    None when no integration was supplied (legacy configs)."""
    integ_d = op.get("integration")
    if not integ_d:
        return None
    from ..engine import bu_integration as bi

    integ = bi.BUIntegration.from_dict(integ_d)
    L1, L2 = _op_leg_lengths(op, bathy)
    return integ.lowering_inputs(leg1_length_m=L1, leg2_length_m=L2)


def _build_scenario(cfg: V3Config, bathy, kind: str, *, static_only: bool = False,
                    hold_depth_m: Optional[float] = None):
    """Shared scenario construction for the operation and static-hold paths."""
    defaults = build_defaults(cfg)
    items = build_assembly(cfg)
    op = dict(cfg.op)
    V = float(op.get("ship_speed_mps", cfg.ship_speed_mps))

    if kind == "straight_lay":
        return scen.straight_lay(
            bathy, items, defaults,
            ship_speed_mps=V,
            slack_percent=float(op.get("slack_percent", cfg.slack_percent)),
            duration_s=float(op.get("duration_s", 1800.0)),
            chute_height_m=cfg.chute_height_m,
            target_ds_m=cfg.target_ds_m,
            course_deg=compass_to_math_deg(cfg.lay_azimuth_deg),
        )
    if kind == "bu_deployment":
        # Line make-up: the BU integration (one BU-datum description of the
        # whole Y) when present, else the legacy shared/leg assemblies.
        low = _lowering_inputs(cfg, op, bathy)
        if low is not None:
            trunk_items = low["trunk_assembly"]
            line_kwargs = dict(
                leg2_assembly=low["leg2_assembly"],
                leg_length_m=low["leg_length_m"],
                leg_lengths_m=low["leg_lengths_m"],
                leg1_joints=low["leg1_joints"],
                leg2_joints=low["leg2_joints"],
                trunk_joints=low["trunk_joints"],
                count_refs=low["count_refs"] or None,
                count_dirs=low["count_dirs"] or None,
            )
            leg_items = low["leg_assembly"]
        else:
            trunk_items = items
            leg_items = (cs.parse_assembly(op["leg_assembly"])
                         if op.get("leg_assembly") else items)
            lengths = op.get("leg_lengths_m")
            L1 = float(op.get("leg_length_m", 2.0 * bathy.depth_at(0.0, 0.0)))
            line_kwargs = dict(
                leg_length_m=L1,
                leg_lengths_m=(tuple(float(v) for v in lengths)
                               if lengths else None),
                leg1_joints=_op_joints(op, "leg1"),
                leg2_joints=_op_joints(op, "leg2"),
                trunk_joints=_op_joints(op, "trunk"),
                count_refs=_op_count_refs(op),
            )
        ends = op.get("leg_far_ends_xy")
        return scen.bu_deployment(
            bathy, trunk_items, leg_items,
            defaults,
            bu_weight_kN=float(op.get("bu_weight_kN", 15.0)),
            bu_cda_m2=float(op.get("bu_cda_m2", 1.5)),
            leg_azimuths_deg=(
                compass_to_math_deg(float(op.get("leg1_azimuth_deg", 150.0))),
                compass_to_math_deg(float(op.get("leg2_azimuth_deg", 210.0))),
            ),
            vessel_course_deg=compass_to_math_deg(cfg.lay_azimuth_deg),
            ship_speed_mps=V,
            payout_speed_mps=float(op.get("payout_mps", 0.4)),
            duration_s=op.get("duration_s"),
            chute_height_m=cfg.chute_height_m,
            target_ds_m=cfg.target_ds_m,
            bu_start_depth_m=hold_depth_m if static_only else op.get("bu_start_depth_m"),
            trunk_slack_pct=cfg.trunk_slack_pct,
            static_only=static_only,
            schedule=(None if static_only else op.get("schedule")),
            # Picked laid ends govern in every mode, static hold included.
            leg_far_ends_xy=(tuple(tuple(float(v) for v in e) for e in ends)
                             if ends else None),
            **line_kwargs,
        )
    if kind == "bu_full":
        return scen.bu_full_deployment(
            bathy,
            cs.parse_assembly(op["leg1_assembly"]) if op.get("leg1_assembly") else items,
            cs.parse_assembly(op["leg2_assembly"]) if op.get("leg2_assembly") else items,
            cs.parse_assembly(op["trunk_assembly"]) if op.get("trunk_assembly") else items,
            defaults,
            **_bu_full_kwargs(cfg, op),
        )
    if kind == "final_bight":
        half = float(op.get("end_separation_m", 120.0)) / 2.0
        # Laid ends run along the bight axis bearing (compass; default 90
        # keeps the historic +x/east orientation).
        axis = math.radians(compass_to_math_deg(float(op.get("bight_axis_deg", 90.0))))
        ux, uy = math.cos(axis), math.sin(axis)
        return scen.final_bight(
            bathy, items, scen.default_rope_assembly(), defaults,
            bight_length_m=float(op.get("bight_length_m", 300.0)),
            end_a_xy=(-half * ux, -half * uy), end_b_xy=(half * ux, half * uy),
            step_course_deg=compass_to_math_deg(float(op.get("step_course_deg", 90.0))),
            vessel_speed_mps=V,
            rope_payout_mps=float(op.get("payout_mps", 0.3)),
            duration_s=op.get("duration_s"),
            release_threshold_kN=float(op.get("release_threshold_kN", 2.0)),
            chute_height_m=cfg.chute_height_m,
            target_ds_m=cfg.target_ds_m,
            apex_start_depth_m=(hold_depth_m if hold_depth_m is not None
                                else float(op.get("apex_start_depth_m", 2.0))),
            static_only=static_only,
        )
    raise ValueError(f"Unknown scenario {kind!r}")


def _bu_full_vessel_geom(cfg: V3Config) -> tl.VesselGeometry:
    return scen.default_bu_vessel_geometry(
        sheave_fwd_m=cfg.sheave_fwd_m,
        sheave_height_m=cfg.chute_height_m,
        sheave_spacing_m=cfg.sheave_spacing_m,
    )


def _bu_full_kwargs(cfg: V3Config, op: dict) -> dict:
    """Shared keyword set for bu_full_deployment / the schedule optimiser.
    Positions are local-frame metres; courses arrive as compass degrees."""
    schedule = None
    if op.get("schedule"):
        schedule = [scen.PhaseRow.from_dict(d) for d in op["schedule"]]
    return dict(
        bu_weight_kN=float(op.get("bu_weight_kN", 15.0)),
        bu_cda_m2=float(op.get("bu_cda_m2", 1.5)),
        laid_end_1_xy=(float(op.get("laid_end_1_x", -100.0)), float(op.get("laid_end_1_y", 150.0))),
        laid_end_2_xy=(float(op.get("laid_end_2_x", -100.0)), float(op.get("laid_end_2_y", -150.0))),
        leg1_deployed_m=(float(op["leg1_deployed_m"]) if op.get("leg1_deployed_m") else None),
        leg2_deployed_m=(float(op["leg2_deployed_m"]) if op.get("leg2_deployed_m") else None),
        vessel_geom=_bu_full_vessel_geom(cfg),
        vessel_xy=(float(op.get("vessel_x", 0.0)), float(op.get("vessel_y", 0.0))),
        vessel_heading_deg=compass_to_math_deg(cfg.lay_azimuth_deg),
        schedule=schedule,
        tail_length_m=float(op.get("tail_length_m", 90.0)),
        tail_leg1_m=(float(op["tail_leg1_m"]) if op.get("tail_leg1_m") is not None else None),
        tail_leg2_m=(float(op["tail_leg2_m"]) if op.get("tail_leg2_m") is not None else None),
        tail_trunk_m=(float(op["tail_trunk_m"]) if op.get("tail_trunk_m") is not None else None),
        payout_mps=float(op.get("payout_mps", 0.4)),
        lay_speed_mps=float(op.get("ship_speed_mps", cfg.ship_speed_mps)),
        trunk_slack_pct=cfg.trunk_slack_pct,
        target_ds_m=cfg.target_ds_m,
        leg1_joints=_op_joints(op, "leg1"),
        leg2_joints=_op_joints(op, "leg2"),
        trunk_joints=_op_joints(op, "trunk"),
        count_refs=_op_count_refs(op),
    )


def run_optimize(cfg: V3Config, progress: Optional[Callable[[float, str], bool]] = None,
                 cancel: Optional[Callable[[], bool]] = None) -> RunOutput:
    """Optimise the bu_full deployment set-up so the BU lands on target,
    using preview-quality simulations; returns the schedule + translated
    geometry for the dialog to adopt, plus the preview timeline."""
    bathy = build_bathymetry(cfg)
    op = dict(cfg.op)
    defaults = build_defaults(cfg)
    items = build_assembly(cfg)
    kwargs = _bu_full_kwargs(cfg, op)
    schedule = kwargs.pop("schedule")
    target = (float(op.get("target_x", 0.0)), float(op.get("target_y", 0.0)))
    params = dict(
        leg1_assembly=cs.parse_assembly(op["leg1_assembly"]) if op.get("leg1_assembly") else items,
        leg2_assembly=cs.parse_assembly(op["leg2_assembly"]) if op.get("leg2_assembly") else items,
        trunk_assembly=cs.parse_assembly(op["trunk_assembly"]) if op.get("trunk_assembly") else items,
        defaults=defaults,
        **kwargs,
    )
    limits = sopt.DeploymentLimits(
        max_tension_kN=float(op.get("limit_tension_kN", 0.0)),
        min_bend_radius_m=float(op.get("limit_mbr_m", cfg.default_mbr_m)),
        max_leg_imbalance_kN=float(op.get("limit_imbalance_kN", 0.0)),
    )
    current = build_current(cfg)
    quality = str(op.get("quality", "draft"))
    sim_factory = None
    if quality == "quick":
        from ..engine.quick_bu import QuickOperationSimulator
        sim_factory = QuickOperationSimulator
    popts = tl.SimOptions.preview(
        rho_water=cfg.rho_water,
        current_at=(current.velocity_at if current else None),
        cancel=cancel,
    )
    popts.controller = _operation_controller(cfg)
    try:
        res = sopt.optimize_bu_schedule(
            bathy, params, target, schedule=schedule, limits=limits,
            preview_options=popts, progress=progress,
            sim_factory=sim_factory,
        )
    except (ValueError, KeyError) as exc:
        return RunOutput(mode="optimize", error=str(exc))
    if cancel and cancel():
        return RunOutput(mode="optimize", error="cancelled")

    out = RunOutput(mode="optimize")
    out.schedule = [r.to_dict() for r in res.schedule]
    out.optimized_setup = {
        "vessel_x": res.vessel_start_xy[0], "vessel_y": res.vessel_start_xy[1],
        "laid_end_1_x": res.laid_end_1_xy[0], "laid_end_1_y": res.laid_end_1_xy[1],
        "laid_end_2_x": res.laid_end_2_xy[0], "laid_end_2_y": res.laid_end_2_xy[1],
    }
    out.warnings = list(res.warnings)
    out.facts = {
        "Predicted BU landing": f"({res.predicted_landing_xy[0]:.1f}, {res.predicted_landing_xy[1]:.1f}) m",
        "Landing error vs target": f"{res.landing_error_m:.1f} m",
        "Optimised vessel start": f"({res.vessel_start_xy[0]:.1f}, {res.vessel_start_xy[1]:.1f}) m",
        "Preview rounds": str(res.rounds),
        "Note": ("Quick analytic previews — run the simulation at Full "
                 "quality for final numbers." if quality == "quick" else
                 "Draft (preview) quality — run the simulation at Full "
                 "quality for final numbers."),
    }
    if res.preview is not None and res.preview.snapshots:
        out.snapshots = res.preview.snapshots
        bed = _bed_grid_for_snapshots(cfg, bathy, res.preview.snapshots)
        track = np.array([s.vessel_xy for s in res.preview.snapshots], dtype=float)

        def build_scene(i: int) -> SceneData:
            return snapshot_scene(res.preview.snapshots[i], bed,
                                  title=f"preview t = {res.preview.snapshots[i].t_s:.0f} s",
                                  cfg=cfg, trail=track[:i + 1])

        out.scene_builder = build_scene
        out.scene = build_scene(len(res.preview.snapshots) - 1)
    return out


def _mean_weight_npm(items: Sequence[cs.AssemblyItem], defaults: cs.Defaults) -> float:
    """Length-weighted mean submerged weight of an assembly (planner input)."""
    total_w = total_l = 0.0
    for it in items:
        if isinstance(it, cs.SegmentSpec) and it.length_m > 0:
            q = it.q_water_npm or defaults.q_water_npm
            total_w += q * it.length_m
            total_l += it.length_m
    return total_w / total_l if total_l > 0 else defaults.q_water_npm


def run_plan(cfg: V3Config, progress: Optional[Callable[[float, str], bool]] = None,
             cancel: Optional[Callable[[], bool]] = None) -> RunOutput:
    """Inverse planning for the full BU deployment: derive the lowering
    schedule (vessel track + trunk payout) from per-leg touchdown-tension
    targets and the landing target, then verify it with the quick
    simulator. Returns the schedule and set-up for the dialog to adopt."""
    from ..engine import bu_plan
    from ..engine.quick_bu import QuickOperationSimulator

    bathy = build_bathymetry(cfg)
    op = dict(cfg.op)
    defaults = build_defaults(cfg)
    items = build_assembly(cfg)
    h1 = float(op.get("leg1_bottom_tension_kN", 0.0) or 0.0)
    h2 = float(op.get("leg2_bottom_tension_kN", 0.0) or 0.0)
    if h1 <= 0.0 or h2 <= 0.0:
        return RunOutput(
            mode="plan",
            error="Planning from tension targets needs a positive touchdown-"
                  "tension target for each leg (see the BU full-deployment "
                  "controls).")
    leg1_items = cs.parse_assembly(op["leg1_assembly"]) if op.get("leg1_assembly") else items
    leg2_items = cs.parse_assembly(op["leg2_assembly"]) if op.get("leg2_assembly") else items
    trunk_items = cs.parse_assembly(op["trunk_assembly"]) if op.get("trunk_assembly") else items
    w1 = _mean_weight_npm(leg1_items, defaults)
    w2 = _mean_weight_npm(leg2_items, defaults)
    wt = _mean_weight_npm(trunk_items, defaults)
    A1 = (float(op.get("laid_end_1_x", -100.0)), float(op.get("laid_end_1_y", 150.0)))
    A2 = (float(op.get("laid_end_2_x", -100.0)), float(op.get("laid_end_2_y", -150.0)))
    target = (float(op.get("target_x", 0.0)), float(op.get("target_y", 0.0)))
    payout = float(op.get("payout_mps", 0.4)) or 0.4
    tail = float(op.get("tail_length_m", 90.0))
    t1 = float(op.get("tail_leg1_m") or tail)
    t2 = float(op.get("tail_leg2_m") or tail)
    joint_margin = 20.0     # matches default_bu_schedule's pay-over margin

    def make_plan(aim_xy):
        return bu_plan.plan_bu_descent(
            bathy, A1, A2, aim_xy,
            w_leg1_npm=w1, w_leg2_npm=w2, w_trunk_npm=wt,
            bu_weight_N=float(op.get("bu_weight_kN", 15.0)) * 1000.0,
            H1_target_N=h1 * 1000.0, H2_target_N=h2 * 1000.0,
            sheave_height_m=cfg.chute_height_m,
            spawn_depth_m=float(op.get("bu_spawn_depth_m", 2.0)),
        )

    def build_run(plan):
        """Quick-simulate a plan; returns (SimResult, schedule rows)."""
        if not plan.feasible:
            return None, []
        v0 = plan.states[0].vessel_xy
        setup = scen.default_bu_schedule(
            depth_m=float(bathy.depth_at(*v0)), tail_length_m=tail,
            tail_leg1_m=op.get("tail_leg1_m"), tail_leg2_m=op.get("tail_leg2_m"),
            tail_trunk_m=op.get("tail_trunk_m"), payout_mps=payout,
            course_deg=plan.states[0].course_deg,
        )[:3]                     # hold / pay joints / transfer
        rows = setup + bu_plan.plan_to_schedule(
            plan, payout_mps=payout, trunk_slack_pct=cfg.trunk_slack_pct)
        scn = scen.bu_full_deployment(
            bathy, leg1_items, leg2_items, trunk_items, defaults,
            bu_weight_kN=float(op.get("bu_weight_kN", 15.0)),
            bu_cda_m2=float(op.get("bu_cda_m2", 1.5)),
            laid_end_1_xy=A1, laid_end_2_xy=A2,
            # The pay-joints phase adds tail + margin per leg before the
            # overboard; start short so the legs reach the planned length.
            leg1_deployed_m=max(50.0, plan.leg_lengths_m["leg1"] - t1 - joint_margin),
            leg2_deployed_m=max(50.0, plan.leg_lengths_m["leg2"] - t2 - joint_margin),
            vessel_geom=_bu_full_vessel_geom(cfg),
            vessel_xy=v0, vessel_heading_deg=plan.states[0].course_deg,
            schedule=rows,
            tail_length_m=tail,
            tail_leg1_m=op.get("tail_leg1_m"), tail_leg2_m=op.get("tail_leg2_m"),
            tail_trunk_m=op.get("tail_trunk_m"),
            payout_mps=payout, trunk_slack_pct=cfg.trunk_slack_pct,
            target_ds_m=cfg.target_ds_m,
            leg1_joints=_op_joints(op, "leg1"),
            leg2_joints=_op_joints(op, "leg2"),
            trunk_joints=_op_joints(op, "trunk"),
            count_refs=_op_count_refs(op),
        )
        sim = QuickOperationSimulator(scn, bathy)
        return sim.run(), rows

    def simulate(plan):
        res, _rows = build_run(plan)
        if res is None:
            return None
        for snap in reversed(res.snapshots):
            xyz = snap.junction_xyz.get("BU")
            if xyz is not None:
                return (xyz[0], xyz[1])
        return None

    if progress is not None:
        progress(-1.0, "Planning descent from tension targets…")
    plan = bu_plan.refine_landing(make_plan, simulate, target, rounds=2)
    if cancel and cancel():
        return RunOutput(mode="plan", error="cancelled")
    if not plan.feasible:
        return RunOutput(
            mode="plan",
            error="No feasible descent plan for these tension targets.",
            error_details="\n".join(plan.warnings))
    result, rows = build_run(plan)

    out = RunOutput(mode="plan")
    out.warnings = list(plan.warnings)
    out.warnings.append(
        "Planned with the quick analytic model — verify the adopted "
        "schedule with a Full-quality simulation run.")
    out.schedule = [r.to_dict() for r in rows]
    v0 = plan.states[0].vessel_xy
    out.optimized_setup = {
        "vessel_x": v0[0], "vessel_y": v0[1],
        "leg1_deployed_m": max(50.0, plan.leg_lengths_m["leg1"] - t1 - joint_margin),
        "leg2_deployed_m": max(50.0, plan.leg_lengths_m["leg2"] - t2 - joint_margin),
        # Initial heading for the planned run (compass, for the UI spin).
        "lay_az_degN": (90.0 - plan.states[0].course_deg) % 360.0,
    }
    landed = simulate(plan)
    miss = (math.hypot(landed[0] - target[0], landed[1] - target[1])
            if landed is not None else float("nan"))
    course_compass = (90.0 - plan.states[-1].course_deg) % 360.0
    out.facts = {
        "Leg 1 required length (at overboard)": f"{plan.leg_lengths_m['leg1']:.0f} m",
        "Leg 2 required length (at overboard)": f"{plan.leg_lengths_m['leg2']:.0f} m",
        "Leg TDP tension targets": f"{h1:.2f} / {h2:.2f} kN",
        "Lay-away course at landing": f"{course_compass:.0f} degN",
        "Predicted landing (quick sim)": (
            f"({landed[0]:.1f}, {landed[1]:.1f}) m" if landed else "—"),
        "Landing error vs target": f"{miss:.1f} m" if np.isfinite(miss) else "—",
        "Trunk top tension at landing": f"{plan.states[-1].trunk_top_N / 1e3:.1f} kN",
        "Note": "Quick analytic plan — confirm at Full quality.",
    }
    if result is not None and result.snapshots:
        out.snapshots = result.snapshots
        bed = _bed_grid_for_snapshots(cfg, bathy, result.snapshots)
        track = np.array([s.vessel_xy for s in result.snapshots], dtype=float)

        def build_scene(i: int) -> SceneData:
            return snapshot_scene(result.snapshots[i], bed,
                                  title=f"plan t = {result.snapshots[i].t_s:.0f} s",
                                  cfg=cfg, trail=track[:i + 1])

        out.scene_builder = build_scene
        out.scene = build_scene(len(result.snapshots) - 1)
    return out


def run_static_hold(cfg: V3Config, cancel: Optional[Callable[[], bool]] = None,
                    progress=None) -> RunOutput:
    """Static equilibrium of a held BU or bight state (no time stepping)."""
    bathy = build_bathymetry(cfg)
    kind = "bu_deployment" if cfg.static_config == "bu" else "final_bight"
    hold_depth = cfg.bu_depth_m if cfg.static_config == "bu" else cfg.apex_depth_m
    try:
        scn = _build_scenario(cfg, bathy, kind, static_only=True, hold_depth_m=hold_depth)
    except ValueError as exc:
        return RunOutput(mode="static", error=str(exc))
    current = build_current(cfg)
    sim = tl.OperationSimulator(scn, bathy, tl.SimOptions(
        rho_water=cfg.rho_water,
        current_at=(current.velocity_at if current else None),
        settle_tol=cfg.dr_tol,
        cancel=cancel,
        solver_progress=_solver_progress_adapter(progress),
    ))
    snap = sim.settle()
    if cancel and cancel():
        return RunOutput(mode="static", error="cancelled")

    bed = _bed_grid_for_snapshots(cfg, bathy, [snap])
    title = "Static hold — branching unit" if cfg.static_config == "bu" else "Static hold — final bight"
    scene = snapshot_scene(snap, bed, title=title, cfg=cfg)
    out = RunOutput(mode="static", scene=scene)
    out.warnings = list(snap.warnings)
    if not snap.converged:
        out.warnings.append("Equilibrium did not fully converge — treat as approximate.")

    facts: Dict[str, str] = {}
    for c in snap.chains:
        facts[f"{c.name}: top / TDP / end tension"] = _fmt_tensions(c)
        facts[f"{c.name}: min bend radius"] = _fmt_radius(c.min_radius_m, None)
    if cfg.static_config == "bu":
        xyz = snap.junction_xyz.get("BU")
        if xyz is not None:
            depth0 = float(bathy.depth_at(xyz[0], xyz[1]))
            facts["BU position"] = f"({xyz[0]:.1f}, {xyz[1]:.1f}, {xyz[2]:.1f}) m"
            facts["BU depth / clearance to bed"] = f"{-xyz[2]:.1f} m / {depth0 + xyz[2]:.1f} m"
    else:
        cable = snap.chain("cable")
        rope = snap.chain("rope")
        if cable is not None:
            apex_z = float(np.max(cable.xyz[:, 2]))
            i_apex = int(np.argmax(cable.xyz[:, 2]))
            depth_apex = float(bathy.depth_at(cable.xyz[i_apex, 0], cable.xyz[i_apex, 1]))
            facts["Bight apex depth / clearance"] = f"{-apex_z:.1f} m / {depth_apex + apex_z:.1f} m"
        if rope is not None:
            facts["Hook load (rope lower end)"] = f"{rope.end_tension_kN:.2f} kN"
            facts["Rope tension at vessel"] = f"{rope.top_tension_kN:.2f} kN"
    facts["Converged"] = "yes" if snap.converged else "NO — treat as approximate"
    out.facts = facts
    _mbr_check_snapshot(out, snap, build_assembly(cfg), build_defaults(cfg))
    return out


def build_manual_controller(cfg: V3Config):
    """Construct a :class:`manual.ManualBUController` over the quick analytic
    backend for interactive driving. Returns ``(controller, bathy)``.

    Uses the same scenario builder as the scripted operation run; the manual
    controller extracts the deployment's discrete events (overboard BU / leg-2
    transfer) from the scenario and then disarms the schedule. No balance
    controller is attached — manual payout stays exactly as the operator
    commands it."""
    from ..engine.manual import ManualBUController
    from ..engine.quick_bu import QuickOperationSimulator

    if cfg.scenario not in ("bu_deployment", "bu_full"):
        raise ValueError("Manual mode supports the BU deployment scenarios "
                         "(choose 'BU deployment' or 'BU deployment — full').")
    bathy = build_bathymetry(cfg)
    scn = _build_scenario(cfg, bathy, cfg.scenario)
    current = build_current(cfg)
    opts = tl.SimOptions(
        rho_water=cfg.rho_water,
        current_at=(current.velocity_at if current else None),
    )
    sim = QuickOperationSimulator(scn, bathy, opts)
    op = dict(cfg.op)
    target = None
    if cfg.scenario == "bu_full":
        target = (float(op.get("target_x", 0.0)), float(op.get("target_y", 0.0)))
    nominal = float(op.get("payout_mps", 0.4)) or 0.4
    controller = ManualBUController(
        sim, nominal_speed_mps=max(nominal, 0.1), target_xy=target)
    return controller, bathy


def manual_bed_grid(cfg: V3Config, bathy, snap, target_xy=None) -> BedGrid:
    """Bed grid covering the manual scene (chains + vessel + target)."""
    xyz_list = [c.xyz for c in snap.chains]
    xyz_list.append(np.array([[snap.vessel_xy[0], snap.vessel_xy[1], 0.0]]))
    if target_xy is not None:
        xyz_list.append(np.array([[target_xy[0], target_xy[1], 0.0]]))
    (x0, x1), (y0, y1) = _bed_extent(xyz_list)
    gx, gy, Z = bathy_mod.sample_grid(bathy, (x0, x1), (y0, y1), n=70)
    return BedGrid(x=gx, y=gy, z=Z)


def manual_scene(snap, cfg: V3Config, bathy, bed: BedGrid,
                 target_xy=None, title: str = "") -> SceneData:
    """Render one manual snapshot, adding the BU landing target marker."""
    scene = snapshot_scene(snap, bed, title=title, cfg=cfg)
    if target_xy is not None:
        tz = -float(bathy.depth_at(target_xy[0], target_xy[1]))
        scene.markers.append(
            Marker((float(target_xy[0]), float(target_xy[1]), tz), "Target", "target"))
    return scene


def _plan_for_lowering(cfg: V3Config, bathy):
    """Tension-target plan for the lowering-only BU scenario.

    Derives the vessel track and trunk payout that hold both legs at the
    target touchdown tension, from the scenario's own geometry (leg lead
    bearings + lengths from the jointing position at the local origin).

    The start state is made CONSISTENT with the targets: each leg's
    layback at the start depth is computed from the target bottom tension
    (tangent catenary), its bed route runs away from that touchdown along
    the lead bearing, and the anchored far end sits where the remaining
    length ends. The run therefore starts already balanced at target —
    the vessel only ever moves ahead and pays out (no back-tracking to
    relieve an over-tight start).

    Returns ``(schedule_rows, far_ends, facts, warnings)`` or
    ``(None, None, {}, [...])`` when planning is impossible.
    """
    from ..engine import bu_plan

    op = dict(cfg.op)
    target_kN = float(op.get("leg_bottom_tension_kN", 0.0) or 0.0)
    if target_kN <= 0.0:
        return None, None, {}, ["Planned lowering needs a positive target "
                                "leg TDP tension."]
    defaults = build_defaults(cfg)
    items = build_assembly(cfg)
    low = _lowering_inputs(cfg, op, bathy)
    if low is not None:
        leg1_items, leg2_items = low["leg_assembly"], low["leg2_assembly"]
        trunk_items = low["trunk_assembly"]
    else:
        leg1_items = leg2_items = (cs.parse_assembly(op["leg_assembly"])
                                   if op.get("leg_assembly") else items)
        trunk_items = items
    w1 = _mean_weight_npm(leg1_items, defaults)
    w2 = _mean_weight_npm(leg2_items, defaults)
    w_trunk = _mean_weight_npm(trunk_items, defaults)
    L1, L2 = _op_leg_lengths(op, bathy)
    depth0 = float(bathy.depth_at(0.0, 0.0))
    spawn = op.get("bu_start_depth_m")
    if spawn is None:
        spawn = min(10.0, 0.1 * depth0)     # scenario default
    spawn = max(1.0, min(float(spawn), depth0 - 0.5))
    payout = float(op.get("payout_mps", 0.4)) or 0.4

    picked = op.get("leg_far_ends_xy")
    if picked:
        # Positions govern: the laid ends came off the as-laid route (map
        # picks / typed coordinates); the entered lengths are the cable
        # that exists — any mismatch surfaces in the planned/simulated
        # tensions rather than being silently re-geometried away.
        anchors = [(float(picked[0][0]), float(picked[0][1])),
                   (float(picked[1][0]), float(picked[1][1]))]
    else:
        # Layback-consistent geometry: TDP at the target-tension layback
        # from the start position, bed route leading away along the lead
        # bearing, anchor at the end of the remaining length.
        anchors = []
        for key, default, w_leg, L in (
                ("leg1_azimuth_deg", 150.0, w1, L1),
                ("leg2_azimuth_deg", 210.0, w2, L2)):
            a_cat = target_kN * 1000.0 / w_leg
            az = math.radians(compass_to_math_deg(float(op.get(key, default))))
            ux, uy = math.cos(az), math.sin(az)
            D0 = 0.0
            h = depth0 - spawn
            for _ in range(3):      # iterate TDP depth on non-flat beds
                h = float(bathy.depth_at(D0 * ux, D0 * uy)) - spawn
                if h <= 0.5:
                    return None, None, {}, [
                        "Start depth leaves no water column for the legs."]
                D0 = a_cat * math.acosh(1.0 + h / a_cat)
            s0 = a_cat * math.sinh(D0 / a_cat)
            bed_len = L - s0
            if bed_len < 10.0:
                return None, None, {}, [
                    f"Leg length {L:.0f} m is (nearly) all suspended at the "
                    f"start ({s0:.0f} m in the water column at the "
                    f"{target_kN:.1f} kN target) — increase the leg length or "
                    "the target tension."]
            anchors.append(((D0 + bed_len) * ux, (D0 + bed_len) * uy))

    plan = bu_plan.plan_bu_descent_fixed_lengths(
        bathy, anchors[0], anchors[1], L1, L2,
        w_leg1_npm=w1, w_leg2_npm=w2, w_trunk_npm=w_trunk,
        bu_weight_N=float(op.get("bu_weight_kN", 15.0)) * 1000.0,
        H1_target_N=target_kN * 1000.0, H2_target_N=target_kN * 1000.0,
        sheave_height_m=cfg.chute_height_m, spawn_depth_m=spawn)
    if not plan.feasible:
        return None, None, {}, list(plan.warnings)

    trunk_susp0 = cfg.chute_height_m + spawn
    rows = bu_plan.plan_to_schedule(
        plan, payout_mps=payout, trunk_slack_pct=cfg.trunk_slack_pct,
        overboard_event=False, start_xy=(0.0, 0.0),
        start_trunk_susp_m=trunk_susp0)
    # Lay ahead on the trunk after landing, along the final balanced
    # course (the trunk bottom-tension controller, if set, trims this).
    V = float(op.get("ship_speed_mps", cfg.ship_speed_mps)) or 0.3
    t_on = float(op.get("duration_s") or 0.0)
    t_on = t_on if t_on > 0.0 else 300.0
    last = plan.states[-1]
    rows.append(scen.PhaseRow(
        label="Lay ahead on trunk",
        duration_s=t_on, course_deg=last.course_deg, speed_mps=V,
        payout_mps={"trunk": V * 1.02}, distance_m=V * t_on,
    ))
    facts = {
        "Planned landing (BU)": f"({plan.landing_xy[0]:.1f}, {plan.landing_xy[1]:.1f}) m",
        "Planned lay-away course": f"{(90.0 - last.course_deg) % 360.0:.0f} degN",
        "Leg TDP tension target": f"{target_kN:.2f} kN (both legs)",
        "Planned trunk top tension at landing": f"{last.trunk_top_N / 1e3:.1f} kN",
    }
    return rows, anchors, facts, list(plan.warnings)


def _operation_controller(cfg: V3Config):
    """Payout controller stack for the BU operation scenarios.

    * ``bu_full``: relative leg balance and/or absolute per-leg touchdown
      tension targets (``leg1_bottom_tension_kN`` / ``leg2_...``; absolute
      targets supersede the balance) plus the trunk touchdown target.
    * ``bu_deployment``: only the trunk pays out, so only the trunk
      touchdown target applies.
    """
    op = cfg.op
    btt = float(op.get("bottom_tension_target_kN", 0.0) or 0.0)
    if cfg.scenario == "bu_full":
        leg_targets = {}
        for name in ("leg1", "leg2"):
            v = float(op.get(f"{name}_bottom_tension_kN", 0.0) or 0.0)
            if v > 0.0:
                leg_targets[name] = v
        return ctl.bu_payout_controllers(
            balance_legs=bool(op.get("balance", True)),
            leg_bottom_targets_kN=leg_targets or None,
            trunk_bottom_target_kN=(btt if btt > 0.0 else None),
        )
    if cfg.scenario == "bu_deployment" and btt > 0.0:
        return ctl.BottomTensionController("trunk", btt)
    return None


def run_operation(cfg: V3Config, progress: Optional[Callable[[float, str], bool]] = None,
                  cancel: Optional[Callable[[], bool]] = None) -> RunOutput:
    bathy = build_bathymetry(cfg)
    # Planned lowering: solve the vessel track + trunk payout that hold the
    # leg touchdown-tension targets, and run that schedule instead of the
    # single straight-line step.
    plan_facts: Dict[str, str] = {}
    plan_warnings: List[str] = []
    if (cfg.scenario == "bu_deployment"
            and bool(cfg.op.get("plan_from_tension"))):
        rows, far_ends, plan_facts, plan_warnings = _plan_for_lowering(cfg, bathy)
        if rows is not None:
            cfg.op = dict(cfg.op)
            cfg.op["schedule"] = rows
            cfg.op["leg_far_ends_xy"] = far_ends
        elif plan_warnings:
            return RunOutput(
                mode="operation",
                error="Could not plan the lowering from the tension "
                      "targets: " + "; ".join(plan_warnings))
    try:
        scn = _build_scenario(cfg, bathy, cfg.scenario)
    except (ValueError, KeyError) as exc:
        return RunOutput(mode="operation", error=str(exc))

    current = build_current(cfg)
    quality = str(cfg.op.get("quality", "full"))
    if quality == "draft":
        opts = tl.SimOptions.preview(
            rho_water=cfg.rho_water,
            current_at=(current.velocity_at if current else None),
            rate_drag=bool(cfg.op.get("rate_drag", True)),
            cancel=cancel,
        )
    else:
        opts = tl.SimOptions(
            rho_water=cfg.rho_water,
            current_at=(current.velocity_at if current else None),
            rate_drag=bool(cfg.op.get("rate_drag", True)),
            cancel=cancel,
        )
    opts.controller = _operation_controller(cfg)
    if quality == "quick":
        if cfg.scenario not in ("bu_deployment", "bu_full"):
            return RunOutput(
                mode="operation",
                error="The quick analytic model supports the BU scenarios "
                      "only — choose Draft or Full for this scenario.")
        from ..engine.quick_bu import QuickOperationSimulator

        sim = QuickOperationSimulator(scn, bathy, opts)
    else:
        sim = tl.OperationSimulator(scn, bathy, opts)
    result = sim.run(progress)

    out = RunOutput(mode="operation", snapshots=result.snapshots)
    out.warnings = plan_warnings + list(result.warnings)
    if quality == "quick":
        out.warnings.append(
            "Quick analytic model: closed-form catenaries with a frozen-lay "
            "seabed (laid cable keeps its as-laid position and tension; only "
            "a friction slip zone at the touchdown re-adjusts), no "
            "hydrodynamic drag — confirm with the full solver.")
    elif quality == "draft":
        out.warnings.append(
            "Draft quality: coarse mesh and loose tolerances — re-run at "
            "Full quality for final numbers.")
    if result.aborted:
        out.warnings.append("Simulation cancelled — snapshots up to the stop are shown.")
    bed = _bed_grid_for_snapshots(cfg, bathy, result.snapshots)
    track = np.array([s.vessel_xy for s in result.snapshots], dtype=float)

    def build_scene(i: int) -> SceneData:
        return snapshot_scene(result.snapshots[i], bed,
                              title=f"t = {result.snapshots[i].t_s:.0f} s",
                              cfg=cfg, trail=track[:i + 1])

    out.scene_builder = build_scene
    if result.snapshots:
        out.scene = build_scene(len(result.snapshots) - 1)
        last = result.snapshots[-1]
        out.facts = {"Steps": str(len(result.snapshots)), "End time": f"{last.t_s:.0f} s"}
        out.facts.update(plan_facts)
        for c in last.chains:
            out.facts[f"{c.name}: top / TDP / end tension"] = _fmt_tensions(c)
            out.facts[f"{c.name}: min bend radius"] = _fmt_radius(c.min_radius_m, None)
            count = getattr(c, "count_top_m", None)
            if count is not None:
                out.facts[f"{c.name}: cable count at sheave"] = f"{count:.0f} m"
        for name, xyz in last.junction_xyz.items():
            out.facts[f"{name} position"] = f"({xyz[0]:.1f}, {xyz[1]:.1f}, {xyz[2]:.1f}) m"
        c1, c2 = last.chain("leg1"), last.chain("leg2")
        if c1 is not None and c2 is not None:
            imb = [abs(s.chain("leg1").top_tension_kN - s.chain("leg2").top_tension_kN)
                   for s in result.snapshots
                   if s.chain("leg1") is not None and s.chain("leg2") is not None]
            if imb:
                out.facts["Leg imbalance (final / peak)"] = (
                    f"{imb[-1]:.2f} / {max(imb):.2f} kN"
                )
        if not last.converged:
            out.warnings.append("Final step did not fully converge — treat as approximate.")
    return out


# ---------------------------------------------------------------------------
# Scene builders and helpers
# ---------------------------------------------------------------------------

_CHAIN_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]


def _vessel_glyph(cfg: V3Config, xy, heading_math_deg: float) -> VesselGlyph:
    return VesselGlyph(
        xy=(float(xy[0]), float(xy[1])),
        heading_deg=float(heading_math_deg),
        length_m=cfg.ship_length_m,
        beam_m=cfg.ship_beam_m,
        height_m=cfg.chute_height_m,
        crp_fwd_m=cfg.crp_fwd_m,
        crp_stbd_m=cfg.crp_stbd_m,
        chute_fwd_m=cfg.chute_fwd_m,
        chute_stbd_m=cfg.chute_stbd_m,
        chute_radius_m=cfg.chute_radius_m,
    )


def _vessel_glyph_sheaves(cfg: V3Config, vessel_xy, heading_math_deg: float) -> VesselGlyph:
    """Vessel glyph for the two-sheave (bu_full) scenes.

    The glyph convention anchors the hull on the cable DEPARTURE point
    (``VesselGlyph.xy``); for bu_full the engine's departure points are the
    port/stbd sheaves at ``sheave_fwd_m`` from the vessel reference — not the
    chute — so the anchor is placed on the sheave-pair centre and the hull is
    offset by the sheave (not chute) lead. Without this the hull draws a full
    ship-length away from the cable tops."""
    h = math.radians(float(heading_math_deg))
    ax = float(vessel_xy[0]) + cfg.sheave_fwd_m * math.cos(h)
    ay = float(vessel_xy[1]) + cfg.sheave_fwd_m * math.sin(h)
    # Individual sheave positions (same frame math as VesselGeometry.
    # sheave_xyz: starboard is clockwise of the heading).
    ch_, sh_ = math.cos(h), math.sin(h)
    half = 0.5 * abs(cfg.sheave_spacing_m)

    def sheave_pt(stbd_m: float) -> Tuple[float, float]:
        return (float(vessel_xy[0]) + cfg.sheave_fwd_m * ch_ + stbd_m * sh_,
                float(vessel_xy[1]) + cfg.sheave_fwd_m * sh_ - stbd_m * ch_)

    port = sheave_pt(-half)
    stbd = sheave_pt(half)
    return VesselGlyph(
        xy=(ax, ay),
        heading_deg=float(heading_math_deg),
        length_m=cfg.ship_length_m,
        beam_m=cfg.ship_beam_m,
        height_m=cfg.chute_height_m,
        crp_fwd_m=cfg.crp_fwd_m,
        crp_stbd_m=cfg.crp_stbd_m,
        chute_fwd_m=cfg.sheave_fwd_m,
        chute_stbd_m=0.0,
        chute_radius_m=0.0,          # no overboarding-chute arc at the sheaves
        departure_label="sheaves",
        sheaves_xy=[("port", port[0], port[1]), ("stbd", stbd[0], stbd[1])],
    )


def _bed_extent(xyz_list: List["np.ndarray"], margin: float = 0.25) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    xs = np.concatenate([a[:, 0] for a in xyz_list]) if xyz_list else np.array([0.0])
    ys = np.concatenate([a[:, 1] for a in xyz_list]) if xyz_list else np.array([0.0])
    dx = max(50.0, float(xs.max() - xs.min()))
    dy = max(50.0, float(ys.max() - ys.min()))
    span = max(dx, dy)
    mx = margin * span
    return ((float(xs.min()) - mx, float(xs.max()) + mx),
            (float(ys.min()) - mx, float(ys.max()) + mx))


def _bed_grid(cfg: V3Config, bathy, xyz: "np.ndarray") -> BedGrid:
    (x0, x1), (y0, y1) = _bed_extent([xyz])
    gx, gy, Z = bathy_mod.sample_grid(bathy, (x0, x1), (y0, y1), n=70)
    return BedGrid(x=gx, y=gy, z=Z)


def _bed_grid_for_snapshots(cfg: V3Config, bathy, snapshots) -> BedGrid:
    xyz_list = []
    for s in snapshots:
        for c in s.chains:
            xyz_list.append(c.xyz)
        xyz_list.append(np.array([[s.vessel_xy[0], s.vessel_xy[1], 0.0]]))
    (x0, x1), (y0, y1) = _bed_extent(xyz_list)
    gx, gy, Z = bathy_mod.sample_grid(bathy, (x0, x1), (y0, y1), n=70)
    return BedGrid(x=gx, y=gy, z=Z)


def _result_scene(cfg: V3Config, bathy, res: s3d.SolveResult, vessel_xy=(0.0, 0.0)) -> SceneData:
    scene = SceneData(title="Static hang")
    scene.bed = _bed_grid(cfg, bathy, res.chains[0].xyz)
    items = build_assembly(cfg)
    colors = {}
    for i, it in enumerate(items):
        if isinstance(it, cs.SegmentSpec):
            colors[i] = it.color or _CHAIN_COLORS[len(colors) % len(_CHAIN_COLORS)]
    seg_colors = [colors.get(i, "#1f77b4") for i in range(len(items))]
    for k, c in enumerate(res.chains):
        scene.cables.append(CablePath(
            xyz=c.xyz.copy(),
            name=c.name,
            tension_kN=c.tension_kN.copy(),
            s_m=c.s.copy(),
            segment_index=np.concatenate([[c.seg_id[0]], c.seg_id]).clip(0),
            segment_colors=seg_colors or None,
            contact=c.contact.copy(),
            color=_CHAIN_COLORS[k % len(_CHAIN_COLORS)],
        ))
        if np.any(c.contact):
            i_tdp = int(np.argmax(c.contact))
            scene.markers.append(Marker(tuple(c.xyz[i_tdp]), "TDP", "tdp"))
    scene.vessel = _vessel_glyph(cfg, vessel_xy, compass_to_math_deg(cfg.lay_azimuth_deg))
    return scene


_JOINT_LABELS = {"leg1": "Leg 1 joint", "leg2": "Leg 2 joint",
                 "trunk": "Trunk joint"}


def _joint_markers(snap) -> List[Marker]:
    """Joint markers from the engine's material-coordinate tracking.

    Each chain snapshot carries every currently-outboard joint in
    ``joints_xyz`` (the automatic splice joint plus any user-defined named
    joints); older snapshots may only have the scalar ``joint_xyz``."""
    out: List[Marker] = []
    for c in snap.chains:
        named = list(getattr(c, "joints_xyz", None) or [])
        if named:
            for label, xyz in named:
                text = (_JOINT_LABELS.get(c.name, f"{c.name} joint")
                        if label == "joint" else f"{c.name}: {label}")
                out.append(Marker(tuple(xyz), text, "joint",
                                  color="#9467bd", size=7.0))
            continue
        j = getattr(c, "joint_xyz", None)
        if j is not None:
            out.append(Marker(tuple(j), _JOINT_LABELS.get(c.name, f"{c.name} joint"),
                              "joint", color="#9467bd", size=7.0))
    return out


def snapshot_scene(snap, bed: BedGrid, title: str = "", cfg: Optional[V3Config] = None,
                   trail: Optional["np.ndarray"] = None) -> SceneData:
    scene = SceneData(title=title or (snap.label or ""))
    scene.bed = bed
    if trail is not None:
        scene.vessel_trail = np.asarray(trail, dtype=float).reshape(-1, 2)
    # Cable-over-sheave rendering (V2-style chute contact): chains departing
    # from the vessel anchor wrap the drawn sheave arc instead of leaving
    # from its top point. Applies wherever a sheave/chute radius is set and
    # the departure is the single vessel anchor (not the two-sheave rig).
    wrap_r = float(getattr(cfg, "chute_radius_m", 0.0) or 0.0) if cfg else 0.0
    if wrap_r > 0.0 and getattr(cfg, "scenario", "") == "bu_full":
        wrap_r = 0.0
    h_rad = math.radians(float(snap.vessel_heading_deg))
    aft_dir = (-math.cos(h_rad), -math.sin(h_rad))
    departure = np.array([snap.vessel_xy[0], snap.vessel_xy[1],
                          float(getattr(cfg, "chute_height_m", 0.0) or 0.0)])
    for k, c in enumerate(snap.chains):
        xyz, s_m, t_kN, contact = c.xyz.copy(), c.s.copy(), c.tension_kN.copy(), c.contact.copy()
        if (wrap_r > 0.0 and len(xyz)
                and float(np.linalg.norm(xyz[0] - departure)) < 1.0):
            from .scene import wrap_cable_over_sheave

            xyz, s_m, t_kN, contact = wrap_cable_over_sheave(
                xyz, s_m, t_kN, contact, wrap_r, aft_dir)
        scene.cables.append(CablePath(
            xyz=xyz,
            name=c.name,
            tension_kN=t_kN,
            s_m=s_m,
            contact=contact,
            color=_CHAIN_COLORS[k % len(_CHAIN_COLORS)],
        ))
    for name, xyz in snap.junction_xyz.items():
        scene.markers.append(Marker(tuple(xyz), name, "junction"))
    scene.markers.extend(_joint_markers(snap))
    # Live touchdown markers: one per chain with a suspended part meeting
    # the bed, labelled with the touchdown tension — these walk along as
    # cable lays down / peels off through the timeline.
    for c in snap.chains:
        contact = np.asarray(c.contact, dtype=bool)
        if not contact.any() or contact[0]:
            continue                       # fully suspended or no free span
        i = int(np.argmax(contact))
        s = np.asarray(c.s, dtype=float)
        # Once a line is essentially fully laid (e.g. the legs after the BU
        # has landed) the touchdown collapses onto its hang point — drop
        # the marker instead of stacking labels there.
        if i < len(s) and float(s[i]) < 5.0:
            continue
        t = np.asarray(c.tension_kN, dtype=float)
        t_tdp = float(t[min(i, len(t) - 1)])
        scene.markers.append(Marker(
            tuple(c.xyz[i]), f"{c.name} TDP {t_tdp:.1f} kN", "tdp"))
    # Timeline headings are already in the engine math frame.
    if cfg is not None:
        # Two-sheave scenes attach the cables at the sheaves, so the hull
        # must be anchored there rather than on the chute.
        if cfg.scenario == "bu_full" and cfg.mode in ("operation", "optimize"):
            scene.vessel = _vessel_glyph_sheaves(cfg, snap.vessel_xy,
                                                 snap.vessel_heading_deg)
        else:
            scene.vessel = _vessel_glyph(cfg, snap.vessel_xy, snap.vessel_heading_deg)
    else:
        scene.vessel = VesselGlyph(tuple(snap.vessel_xy), heading_deg=snap.vessel_heading_deg)
    return scene


def tdp_tension_kN(chain_snapshot) -> Optional[float]:
    """Tension at the touchdown point: the first bed-contact node from the
    top. None when the chain has no bed contact (fully suspended)."""
    try:
        contact = np.asarray(chain_snapshot.contact, dtype=bool)
        if not contact.any():
            return None
        i = int(np.argmax(contact))
        t = np.asarray(chain_snapshot.tension_kN, dtype=float)
        return float(t[min(i, len(t) - 1)])
    except Exception:
        return None


def _fmt_tensions(c) -> str:
    """'top / TDP / end' tension summary for one chain snapshot."""
    tdp = tdp_tension_kN(c)
    tdp_s = f"{tdp:.2f}" if tdp is not None else "—"
    return f"{c.top_tension_kN:.2f} / {tdp_s} / {c.end_tension_kN:.2f} kN"


def _fmt_radius(r: float, s_m: Optional[float]) -> str:
    if not np.isfinite(r) or r > 1e5:
        return "straight (no bend)"
    at = f" at s = {s_m:.0f} m" if s_m is not None else ""
    return f"{r:.1f} m{at}"


def _mbr_limit(items, defaults) -> float:
    mbr_default = defaults.mbr_m
    limits = [it.min_bend_radius_m for it in items
              if isinstance(it, cs.SegmentSpec) and it.min_bend_radius_m]
    return max([mbr_default] + limits) if (mbr_default or limits) else 0.0


def _mbr_check(out: RunOutput, res: s3d.SolveResult, items, defaults) -> None:
    limit = _mbr_limit(items, defaults)
    if limit <= 0:
        return
    worst = min(c.min_radius_m for c in res.chains)
    if worst < limit:
        out.warnings.append(
            f"MINIMUM BEND RADIUS VIOLATED: {worst:.1f} m modelled vs {limit:.1f} m limit."
        )


def _mbr_check_snapshot(out: RunOutput, snap, items, defaults) -> None:
    limit = _mbr_limit(items, defaults)
    if limit <= 0:
        return
    worst = min(c.min_radius_m for c in snap.chains)
    if worst < limit:
        out.warnings.append(
            f"MINIMUM BEND RADIUS VIOLATED: {worst:.1f} m modelled vs {limit:.1f} m limit."
        )


def _quick_facts(inp: sl.SteadyLayInput, H_c: float, V: float, depth: float) -> Dict[str, str]:
    """Zajac closed-form quick answers shown next to the numeric solve."""
    quick: Dict[str, str] = {}
    if H_c > 0:
        quick["Hydrodynamic constant H"] = f"{H_c:.2f} m/s ({H_c / KMH:.2f} km/h)"
        if V > 0:
            a = hyd.critical_angle_rad(H_c, V)
            quick["Critical angle (T0 = 0)"] = f"{math.degrees(a):.1f} deg"
            quick["Straight-line layback"] = f"{depth / math.tan(a):.0f} m"
        quick["Suspension-free speed on 10 deg upslope"] = (
            f"{hyd.suspension_speed_limit_mps(H_c, math.radians(10.0)) / KMH:.1f} km/h"
        )
        quick["Pay-out increment for 10 deg downslope"] = (
            f"{hyd.payout_increment_mps(H_c, math.radians(10.0)) / KMH:+.2f} km/h (any ship speed)"
        )
    quick["T_ship - T_bottom (w x h theorem)"] = f"{inp.q_water_npm * depth / 1000.0:.2f} kN"
    return quick


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class SolveWorker(QThread):
    """Runs one solve; emits ``finishedWith(RunOutput)``."""

    finishedWith = pyqtSignal(object)
    progressed = pyqtSignal(float, str)

    def __init__(self, cfg: V3Config, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _progress(self, frac: float, label: str) -> bool:
        self.progressed.emit(float(frac), str(label))
        return not self._cancel

    def run(self):  # noqa: D102 - QThread entry point
        try:
            # The solver detects and recovers from numerical blow-ups itself
            # (reporting them in the result's warnings), so the transient
            # overflow/NaN FP warnings on the way there are just log noise.
            with np.errstate(over="ignore", invalid="ignore"):
                if self.cfg.mode == "steady":
                    out = run_steady(self.cfg, cancel=lambda: self._cancel)
                elif self.cfg.mode == "operation":
                    out = run_operation(self.cfg, progress=self._progress,
                                        cancel=lambda: self._cancel)
                elif self.cfg.mode == "optimize":
                    out = run_optimize(self.cfg, progress=self._progress,
                                       cancel=lambda: self._cancel)
                elif self.cfg.mode == "plan":
                    out = run_plan(self.cfg, progress=self._progress,
                                   cancel=lambda: self._cancel)
                else:
                    out = run_static(self.cfg, cancel=lambda: self._cancel,
                                     progress=self._progress)
        except Exception as exc:  # surface, never crash the UI thread
            msg = str(exc).strip() or type(exc).__name__
            out = RunOutput(mode=self.cfg.mode, error=msg,
                            error_details=traceback.format_exc(limit=8))
        self.finishedWith.emit(out)
