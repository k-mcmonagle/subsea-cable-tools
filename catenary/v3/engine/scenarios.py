# -*- coding: utf-8 -*-
"""Pre-built operation scenarios for the timeline simulator.

Pure Python + NumPy; no Qt/QGIS imports.

Each builder returns a :class:`timeline.Scenario` ready for
:class:`timeline.OperationSimulator`. Conventions: the lay/operation azimuth
is the +x axis unless stated; z = 0 at the surface, negative down; the
vessel chute sits ``chute_height_m`` above the waterline at the vessel
position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .cable_system import AssemblyItem, Defaults, sagged_shape, straight_shape, uniform_assembly
from .seeds import catenary_seed, hanging_leg_seed
from .timeline import (
    Attachment,
    ChainState,
    Event,
    JunctionState,
    Scenario,
    SheaveSpec,
    SheaveTransfer,
    Step,
    VesselGeometry,
)


def _bed_z(bathy, x: float, y: float) -> float:
    return -float(bathy.depth_at(x, y))


# ---------------------------------------------------------------------------
# Straight lay (transient counterpart of the steady-lay mode)
# ---------------------------------------------------------------------------

def straight_lay(
    bathy,
    assembly: List[AssemblyItem],
    defaults: Defaults,
    *,
    ship_speed_mps: float,
    slack_percent: float = 2.0,
    duration_s: float = 1800.0,
    initial_suspended_m: float = 0.0,
    chute_height_m: float = 5.0,
    on_bed_tail_m: float = 200.0,
    target_ds_m: float = 5.0,
    course_deg: float = 0.0,
) -> Scenario:
    """A vessel steaming along ``course_deg`` (math degrees; default +x)
    paying out cable; the far end is anchored on the bed behind the start
    position."""
    depth0 = float(bathy.depth_at(0.0, 0.0))
    if initial_suspended_m <= 0:
        initial_suspended_m = 1.35 * depth0 + chute_height_m
    L0 = initial_suspended_m + on_bed_tail_m
    c = math.radians(float(course_deg))
    ux, uy = math.cos(c), math.sin(c)
    anchor_r = initial_suspended_m * 0.35 + on_bed_tail_m
    anchor = (-anchor_r * ux, -anchor_r * uy,
              _bed_z(bathy, -anchor_r * ux, -anchor_r * uy))

    # Seed: hanging catenary chute -> touchdown, then a tail to the anchor.
    chute = np.array([0.0, 0.0, chute_height_m])
    tdp_guess = np.array([-0.55 * initial_suspended_m * ux,
                          -0.55 * initial_suspended_m * uy, -depth0])
    shape = np.vstack([
        catenary_seed(chute, tdp_guess, initial_suspended_m, 40),
        straight_shape(tdp_guess, np.asarray(anchor), 20)[1:],
    ])
    chain = ChainState(
        name="cable",
        assembly=assembly,
        defaults=defaults,
        length_m=L0,
        top=Attachment("vessel", chute_height_m=chute_height_m),
        bottom=Attachment("fixed", xyz=anchor),
        shape=shape,
        target_ds_m=target_ds_m,
    )
    payout = ship_speed_mps * (1.0 + slack_percent / 100.0)
    steps = [Step(duration_s=duration_s, vessel_course_deg=float(course_deg),
                  vessel_speed_mps=ship_speed_mps, payout_mps={"cable": payout},
                  label=f"lay at {ship_speed_mps:.2f} m/s, {slack_percent:.1f}% slack")]
    return Scenario(chains={"cable": chain}, vessel_xy=(0.0, 0.0),
                    vessel_heading_deg=float(course_deg), steps=steps)


# ---------------------------------------------------------------------------
# Branching-unit deployment
# ---------------------------------------------------------------------------

def bu_deployment(
    bathy,
    trunk_assembly: List[AssemblyItem],
    leg_assembly: List[AssemblyItem],
    defaults: Defaults,
    *,
    bu_weight_kN: float,
    bu_cda_m2: float = 1.0,
    leg_length_m: float,
    leg_azimuths_deg: Tuple[float, float] = (150.0, 210.0),
    vessel_course_deg: float = 0.0,
    ship_speed_mps: float = 0.5,
    payout_speed_mps: float = 0.4,
    duration_s: Optional[float] = None,
    trunk_total_m: Optional[float] = None,
    chute_height_m: float = 5.0,
    target_ds_m: float = 5.0,
    bu_start_depth_m: Optional[float] = None,
    trunk_slack_pct: float = 2.0,
    static_only: bool = False,
) -> Scenario:
    """Deploy a branching unit: the BU hangs from the trunk while its two
    pre-laid legs run along the bed; the vessel steams along
    ``vessel_course_deg`` paying out trunk until the BU lands.

    Initial state: BU at ``bu_start_depth_m`` below the vessel (default just
    below the surface); legs laid out on the bed along ``leg_azimuths_deg``
    from the point beneath the vessel (the legs were laid first; their far
    ends are anchored). With ``static_only`` the script is empty — the
    scenario is a single static equilibrium of that suspended state (the
    "hold" snapshot at a chosen BU depth).
    """
    depth0 = float(bathy.depth_at(0.0, 0.0))
    if bu_start_depth_m is None:
        bu_start_depth_m = min(10.0, 0.1 * depth0)
    bu_start_depth_m = max(1.0, min(float(bu_start_depth_m), depth0 - 0.5))
    bu_start = (0.0, 0.0, -bu_start_depth_m)
    junction = JunctionState("BU", bu_start, load_kN=bu_weight_kN, cda_m2=bu_cda_m2)

    trunk_len0 = (chute_height_m + abs(bu_start[2])) * (1.0 + max(0.0, trunk_slack_pct) / 100.0)
    if trunk_total_m is None:
        trunk_total_m = 1.6 * depth0 + chute_height_m
    chute = np.array([0.0, 0.0, chute_height_m])
    trunk = ChainState(
        name="trunk",
        assembly=trunk_assembly,
        defaults=defaults,
        length_m=trunk_len0,
        top=Attachment("vessel", chute_height_m=chute_height_m),
        bottom=Attachment("junction", junction="BU"),
        shape=sagged_shape(chute, np.asarray(bu_start), 30, slack_frac=0.02),
        target_ds_m=max(2.0, target_ds_m / 2.0),
        min_elems=30,
    )

    chains: Dict[str, ChainState] = {"trunk": trunk}
    for i, az in enumerate(leg_azimuths_deg, start=1):
        a = math.radians(az)
        ux, uy = math.cos(a), math.sin(a)
        end = (leg_length_m * 0.92 * ux, leg_length_m * 0.92 * uy)
        end_xyz = (end[0], end[1], _bed_z(bathy, end[0], end[1]))
        # Seed: hanging catenary from the BU to a touchdown guess, then a
        # bed-following tail along the azimuth (follows slopes and grids).
        shape = hanging_leg_seed(bu_start, az, leg_length_m, bathy, 60)
        shape[-1] = np.asarray(end_xyz)
        chains[f"leg{i}"] = ChainState(
            name=f"leg{i}",
            assembly=leg_assembly,
            defaults=defaults,
            length_m=leg_length_m,
            top=Attachment("junction", junction="BU"),
            bottom=Attachment("fixed", xyz=end_xyz),
            shape=shape,
            target_ds_m=target_ds_m,
        )

    if static_only:
        steps = []
    else:
        if duration_s is None:
            # Rough time to land the BU: descent ~ payout rate.
            duration_s = 1.4 * (depth0 - bu_start_depth_m + 10.0) / max(payout_speed_mps, 0.05)
        steps = [Step(
            duration_s=duration_s,
            vessel_course_deg=vessel_course_deg,
            vessel_speed_mps=ship_speed_mps,
            payout_mps={"trunk": payout_speed_mps},
            label="lower BU and steam ahead",
        )]
    return Scenario(
        chains=chains,
        junctions={"BU": junction},
        vessel_xy=(0.0, 0.0),
        vessel_heading_deg=vessel_course_deg,
        steps=steps,
    )


# ---------------------------------------------------------------------------
# Final bight lay-down
# ---------------------------------------------------------------------------

def final_bight(
    bathy,
    cable_assembly: List[AssemblyItem],
    rope_assembly: List[AssemblyItem],
    defaults: Defaults,
    *,
    bight_length_m: float,
    end_a_xy: Tuple[float, float],
    end_b_xy: Tuple[float, float],
    step_course_deg: float = 90.0,
    vessel_speed_mps: float = 0.3,
    rope_payout_mps: float = 0.35,
    duration_s: Optional[float] = None,
    release_threshold_kN: float = 2.0,
    chute_height_m: float = 5.0,
    target_ds_m: float = 5.0,
    apex_start_depth_m: float = 2.0,
    static_only: bool = False,
) -> Scenario:
    """Lower a final bight to the seabed from a stepping vessel.

    The joined cable runs from laid end A on the bed, up to a bight apex
    held by a lowering rope at the vessel, and back down to laid end B. The
    vessel steps along ``step_course_deg`` (default +y, i.e. perpendicular
    to the A-B axis) paying out the rope; the rope is released automatically
    once slack (max tension < ``release_threshold_kN``).

    ``apex_start_depth_m`` sets the initial apex depth (rope length is
    derived from it), so a mid-lowering hold can be modelled; with
    ``static_only`` the script is empty and the scenario is that single
    suspended equilibrium.
    """
    ax, ay = end_a_xy
    bx, by = end_b_xy
    end_a = (ax, ay, _bed_z(bathy, ax, ay))
    end_b = (bx, by, _bed_z(bathy, bx, by))
    mid_xy = (0.5 * (ax + bx), 0.5 * (ay + by))
    depth_mid = float(bathy.depth_at(*mid_xy))

    chord = math.hypot(bx - ax, by - ay)
    if bight_length_m < chord + 2.0 * depth_mid * 0.8:
        raise ValueError(
            "bight_length_m is too short to reach the surface between the "
            f"laid ends (chord {chord:.0f} m, depth {depth_mid:.0f} m)."
        )

    # Apex starts below the vessel (over the midpoint) at the chosen depth,
    # clamped to what the bight length can physically reach:
    # half-bight >= straight distance from a laid end to the apex.
    half_bight = bight_length_m / 2.0
    reach = math.sqrt(max(1.0, half_bight ** 2 - (chord / 2.0) ** 2))
    d_min = max(1.0, depth_mid - reach + 0.5)
    apex_depth = max(d_min, min(float(apex_start_depth_m), depth_mid - 0.5))
    apex = np.array([mid_xy[0], mid_xy[1], -apex_depth])
    n_half = 40
    shape = np.vstack([
        catenary_seed(np.asarray(end_a), apex, half_bight, n_half),
        catenary_seed(apex, np.asarray(end_b), half_bight, n_half)[1:],
    ])
    cable = ChainState(
        name="cable",
        assembly=cable_assembly,
        defaults=defaults,
        length_m=bight_length_m,
        top=Attachment("fixed", xyz=end_a),
        bottom=Attachment("fixed", xyz=end_b),
        shape=shape,
        target_ds_m=target_ds_m,
        mapper_direction="from_top",
    )

    chute = np.array([mid_xy[0], mid_xy[1], chute_height_m])
    rope_len0 = chute_height_m + apex_depth + 2.0
    rope = ChainState(
        name="rope",
        assembly=rope_assembly,
        defaults=defaults,
        length_m=rope_len0,
        top=Attachment("vessel", chute_height_m=chute_height_m),
        bottom=Attachment("chain_point", chain="cable", s_from_bottom_m=bight_length_m / 2.0),
        shape=straight_shape(chute, apex, 12),
        target_ds_m=max(2.0, target_ds_m / 2.0),
        min_elems=10,
    )

    if static_only:
        steps = []
    else:
        if duration_s is None:
            duration_s = 1.6 * (depth_mid - apex_depth + 5.0) / max(rope_payout_mps, 0.05)
        steps = [Step(
            duration_s=duration_s,
            vessel_course_deg=step_course_deg,
            vessel_speed_mps=vessel_speed_mps,
            payout_mps={"rope": rope_payout_mps},
            label="step and lower the bight",
        )]
    return Scenario(
        chains={"cable": cable, "rope": rope},
        vessel_xy=mid_xy,
        vessel_heading_deg=step_course_deg,
        steps=steps,
        auto_release_kN={} if static_only else {"rope": release_threshold_kN},
    )


def default_rope_assembly(length_hint_m: float = 3000.0) -> List[AssemblyItem]:
    """A generic lowering rope: 32 mm wire, ~3.5 kg/m in water."""
    return uniform_assembly(
        length_hint_m, 34.0, diameter_m=0.032, cd_normal=1.2,
        cd_tangential=0.008, mu=0.4, name="Lowering rope",
    )


# ---------------------------------------------------------------------------
# Full branching-unit deployment (two-sheave, on-deck jointing to laydown)
# ---------------------------------------------------------------------------

@dataclass
class PhaseRow:
    """One editable row of a deployment schedule.

    ``event`` tags the topology change applied at the start of the phase:
    ``""`` (none), ``"transfer"`` (move leg 2 to the other sheave, lerped
    across the phase) or ``"overboard_bu"`` (spawn the BU junction + trunk
    and re-top both legs onto it). Angles are engine math degrees
    (0 = +x, CCW positive); UI layers convert compass bearings.

    ``distance_m`` is the planned ship position change over the phase
    (the operator-facing measure, counted from the jointing position).
    The engine steps on ``duration_s``; for a moving phase the two are
    linked by ``duration_s = distance_m / speed_mps`` and the builders
    keep both populated. ``course_deg`` may be ``None`` meaning "follow
    the operation's lay course" — resolved by the consumer before the
    row reaches the engine.
    """

    label: str
    duration_s: float
    course_deg: Optional[float] = 0.0
    speed_mps: float = 0.0
    payout_mps: Dict[str, float] = field(default_factory=dict)
    event: str = ""
    distance_m: float = 0.0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "duration_s": float(self.duration_s),
            "course_deg": None if self.course_deg is None else float(self.course_deg),
            "speed_mps": float(self.speed_mps),
            "payout_mps": {k: float(v) for k, v in self.payout_mps.items()},
            "event": self.event,
            "distance_m": float(self.distance_m),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PhaseRow":
        course = d.get("course_deg", 0.0)
        return cls(
            label=str(d.get("label", "")),
            duration_s=float(d.get("duration_s", 0.0)),
            course_deg=None if course is None else float(course),
            speed_mps=float(d.get("speed_mps", 0.0)),
            payout_mps={str(k): float(v) for k, v in (d.get("payout_mps") or {}).items()},
            event=str(d.get("event", "")),
            distance_m=float(d.get("distance_m", 0.0)),
        )


def default_bu_vessel_geometry(
    sheave_fwd_m: float = 0.0,
    sheave_height_m: float = 5.0,
    sheave_spacing_m: float = 12.0,
) -> VesselGeometry:
    """Port and starboard sheaves symmetric about the centreline, plus the
    legacy ``main`` sheave on the centreline."""
    half = 0.5 * abs(sheave_spacing_m)
    return VesselGeometry(sheaves={
        "main": SheaveSpec(fwd_m=sheave_fwd_m, stbd_m=0.0, height_m=sheave_height_m),
        "port": SheaveSpec(fwd_m=sheave_fwd_m, stbd_m=-half, height_m=sheave_height_m),
        "stbd": SheaveSpec(fwd_m=sheave_fwd_m, stbd_m=half, height_m=sheave_height_m),
    })


def default_bu_schedule(
    *,
    depth_m: float,
    tail_length_m: float = 90.0,
    tail_leg1_m: Optional[float] = None,
    tail_leg2_m: Optional[float] = None,
    tail_trunk_m: Optional[float] = None,
    payout_mps: float = 0.4,
    lay_speed_mps: float = 0.3,
    course_deg: Optional[float] = 0.0,
    transfer_duration_s: float = 120.0,
    joint_margin_m: float = 20.0,
    lay_on_margin_s: float = 300.0,
) -> List[PhaseRow]:
    """The nominal five-phase deployment script (leg names ``leg1``/``leg2``,
    trunk ``trunk``).

    Each BU tail can differ (``tail_leg1_m`` / ``tail_leg2_m`` /
    ``tail_trunk_m``; all default to ``tail_length_m``): the joint pay-over
    phase is timed by the longer leg tail with the shorter leg's rate scaled
    so both joints go overboard together, and the lay-ahead phase is long
    enough to pay the trunk tail (plus margin) over the sheave.
    """
    t1 = float(tail_leg1_m if tail_leg1_m is not None else tail_length_m)
    t2 = float(tail_leg2_m if tail_leg2_m is not None else tail_length_m)
    tt = float(tail_trunk_m if tail_trunk_m is not None else tail_length_m)
    payout_mps = max(payout_mps, 0.01)
    t_joints = (max(t1, t2) + joint_margin_m) / payout_mps
    rate1 = (t1 + joint_margin_m) / t_joints
    rate2 = (t2 + joint_margin_m) / t_joints
    t_lower = 1.5 * (depth_m + 5.0) / payout_mps
    trunk_lay_rate = lay_speed_mps * 1.02
    t_lay_on = max(lay_on_margin_s,
                   (tt + joint_margin_m) / max(trunk_lay_rate, 0.01))

    def dist(speed: float, t: float) -> float:
        return speed * t

    return [
        PhaseRow("Hold and balance legs", 60.0, course_deg, 0.0, {}),
        PhaseRow(
            "Pay joints overboard", t_joints, course_deg, 0.05,
            {"leg1": rate1, "leg2": rate2},
            distance_m=dist(0.05, t_joints),
        ),
        PhaseRow(
            "Transfer leg 2 to port sheave", transfer_duration_s, course_deg, 0.0,
            {"leg1": 0.02, "leg2": 0.02}, event="transfer",
        ),
        PhaseRow(
            "Overboard BU and lower", t_lower, course_deg, lay_speed_mps,
            {"trunk": payout_mps}, event="overboard_bu",
            distance_m=dist(lay_speed_mps, t_lower),
        ),
        PhaseRow(
            "Lay ahead on trunk", t_lay_on, course_deg, lay_speed_mps,
            {"trunk": trunk_lay_rate},
            distance_m=dist(lay_speed_mps, t_lay_on),
        ),
    ]


def bu_full_deployment(
    bathy,
    leg1_assembly: List[AssemblyItem],
    leg2_assembly: List[AssemblyItem],
    trunk_assembly: List[AssemblyItem],
    defaults: Defaults,
    *,
    bu_weight_kN: float,
    bu_cda_m2: float = 1.0,
    laid_end_1_xy: Tuple[float, float],
    laid_end_2_xy: Tuple[float, float],
    leg1_deployed_m: Optional[float] = None,
    leg2_deployed_m: Optional[float] = None,
    vessel_geom: Optional[VesselGeometry] = None,
    vessel_xy: Tuple[float, float] = (0.0, 0.0),
    vessel_heading_deg: float = 0.0,
    schedule: Optional[List[PhaseRow]] = None,
    tail_length_m: float = 90.0,
    tail_leg1_m: Optional[float] = None,
    tail_leg2_m: Optional[float] = None,
    tail_trunk_m: Optional[float] = None,
    payout_mps: float = 0.4,
    lay_speed_mps: float = 0.3,
    bu_spawn_depth_m: float = 2.0,
    trunk_slack_pct: float = 2.0,
    target_ds_m: float = 5.0,
) -> Scenario:
    """The full BU deployment from the two-sheave jointing set-up.

    Initial state: the two legs are pre-laid on the bed to their fixed far
    ends (``laid_end_*_xy``), with their recovered ends held over the port
    (leg 1) and starboard (leg 2) sheaves. The script (``schedule``, or the
    :func:`default_bu_schedule`) then pays the joints overboard, transfers
    leg 2 to the port sheave, overboards the BU (spawning the junction and
    trunk), and lowers it to the bed while steaming ahead.

    The scenario carries no balance controller — hang a
    ``control.TensionBalanceController("leg1", "leg2")`` on
    ``SimOptions.controller`` to keep the legs balanced during payout.
    """
    geom = vessel_geom or default_bu_vessel_geometry()
    if "port" not in geom.sheaves or "stbd" not in geom.sheaves:
        raise ValueError("vessel_geom must define 'port' and 'stbd' sheaves")

    def sheave_xyz(name: str):
        return np.asarray(
            geom.sheave_xyz(vessel_xy, vessel_heading_deg, name), dtype=float)

    chains: Dict[str, ChainState] = {}
    for i, (asm, end_xy, dep) in enumerate(
        ((leg1_assembly, laid_end_1_xy, leg1_deployed_m),
         (leg2_assembly, laid_end_2_xy, leg2_deployed_m)), start=1,
    ):
        sheave = "port" if i == 1 else "stbd"
        top = sheave_xyz(sheave)
        end_xyz = (end_xy[0], end_xy[1], _bed_z(bathy, end_xy[0], end_xy[1]))
        chord = float(np.linalg.norm(np.asarray(end_xyz) - top))
        depth_here = float(bathy.depth_at(*end_xy))
        length = float(dep) if dep is not None else 1.05 * chord + 0.2 * depth_here
        az = math.degrees(math.atan2(end_xy[1] - top[1], end_xy[0] - top[0]))
        shape = hanging_leg_seed(top, az, length, bathy, 60)
        shape[-1] = np.asarray(end_xyz)
        chains[f"leg{i}"] = ChainState(
            name=f"leg{i}",
            assembly=asm,
            defaults=defaults,
            length_m=length,
            top=Attachment("sheave", sheave=sheave),
            bottom=Attachment("fixed", xyz=end_xyz),
            shape=shape,
            target_ds_m=target_ds_m,
        )

    depth0 = float(bathy.depth_at(*vessel_xy))
    rows = schedule if schedule is not None else default_bu_schedule(
        depth_m=depth0, tail_length_m=tail_length_m,
        tail_leg1_m=tail_leg1_m, tail_leg2_m=tail_leg2_m,
        tail_trunk_m=tail_trunk_m, payout_mps=payout_mps,
        lay_speed_mps=lay_speed_mps, course_deg=vessel_heading_deg,
    )

    # Trunk template, instantiated by the overboard event at the phase start
    # (its start position depends on the vessel position at that moment).
    sheave_h = geom.sheaves["port"].height_m
    trunk_len0 = (sheave_h + bu_spawn_depth_m) * (1.0 + max(0.0, trunk_slack_pct) / 100.0)

    def overboard_events() -> List[Event]:
        trunk = ChainState(
            name="trunk",
            assembly=trunk_assembly,
            defaults=defaults,
            length_m=trunk_len0,
            top=Attachment("sheave", sheave="port"),
            bottom=Attachment("junction", junction="BU"),
            shape=np.zeros((0, 3)),      # synthesised at apply time
            target_ds_m=max(2.0, target_ds_m / 2.0),
            min_elems=30,
        )
        return [
            Event(
                kind="add_junction",
                junction=JunctionState("BU", (0.0, 0.0, 0.0),
                                       load_kN=bu_weight_kN, cda_m2=bu_cda_m2),
                at_sheave="port", depth_m=bu_spawn_depth_m,
                label="BU overboarded",
            ),
            Event(kind="add_chain", chain_state=trunk),
            Event(kind="set_top", chain="leg1",
                  attachment=Attachment("junction", junction="BU")),
            Event(kind="set_top", chain="leg2",
                  attachment=Attachment("junction", junction="BU")),
        ]

    steps: List[Step] = []
    for row in rows:
        # A None course means "follow the operation's lay course".
        course = (vessel_heading_deg if row.course_deg is None
                  else float(row.course_deg))
        step = Step(
            duration_s=float(row.duration_s),
            vessel_course_deg=course,
            vessel_speed_mps=float(row.speed_mps),
            payout_mps=dict(row.payout_mps),
            label=row.label,
        )
        if row.event == "transfer":
            step.transfer = SheaveTransfer("leg2", "stbd", "port")
        elif row.event == "overboard_bu":
            step.events = overboard_events()
        elif row.event:
            raise ValueError(f"Unknown schedule event {row.event!r}")
        steps.append(step)

    return Scenario(
        chains=chains,
        junctions={},
        vessel_xy=vessel_xy,
        vessel_heading_deg=vessel_heading_deg,
        vessel_geom=geom,
        steps=steps,
    )
