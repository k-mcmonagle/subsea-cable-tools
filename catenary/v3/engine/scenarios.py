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
from .timeline import Attachment, ChainState, JunctionState, Scenario, Step


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
) -> Scenario:
    """A vessel steaming along +x paying out cable; the far end is anchored
    on the bed behind the start position."""
    depth0 = float(bathy.depth_at(0.0, 0.0))
    if initial_suspended_m <= 0:
        initial_suspended_m = 1.35 * depth0 + chute_height_m
    L0 = initial_suspended_m + on_bed_tail_m
    anchor = (-(initial_suspended_m * 0.35 + on_bed_tail_m), 0.0, _bed_z(bathy, -(initial_suspended_m * 0.35 + on_bed_tail_m), 0.0))

    # Seed: chute down-ramp to the bed, then a tail back to the anchor.
    chute = np.array([0.0, 0.0, chute_height_m])
    tdp_guess = np.array([-0.55 * initial_suspended_m, 0.0, -depth0])
    shape = np.vstack([
        straight_shape(chute, tdp_guess, 40),
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
    steps = [Step(duration_s=duration_s, vessel_course_deg=0.0,
                  vessel_speed_mps=ship_speed_mps, payout_mps={"cable": payout},
                  label=f"lay at {ship_speed_mps:.2f} m/s, {slack_percent:.1f}% slack")]
    return Scenario(chains={"cable": chain}, vessel_xy=(0.0, 0.0), steps=steps)


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
    pre-laid legs run along the bed; the vessel steams ahead (+x) paying out
    trunk until the BU lands.

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
        # Seed: from the BU position down to the bed, then along the azimuth.
        drop = np.array([0.15 * leg_length_m * ux, 0.15 * leg_length_m * uy,
                         _bed_z(bathy, 0.15 * leg_length_m * ux, 0.15 * leg_length_m * uy)])
        shape = np.vstack([
            straight_shape(np.asarray(bu_start), drop, 20),
            straight_shape(drop, np.asarray(end_xyz), 40)[1:],
        ])
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
            vessel_course_deg=0.0,
            vessel_speed_mps=ship_speed_mps,
            payout_mps={"trunk": payout_speed_mps},
            label="lower BU and steam ahead",
        )]
    return Scenario(
        chains=chains,
        junctions={"BU": junction},
        vessel_xy=(0.0, 0.0),
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
        straight_shape(np.asarray(end_a), apex, n_half),
        straight_shape(apex, np.asarray(end_b), n_half)[1:],
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
        steps=steps,
        auto_release_kN={} if static_only else {"rope": release_threshold_kN},
    )


def default_rope_assembly(length_hint_m: float = 3000.0) -> List[AssemblyItem]:
    """A generic lowering rope: 32 mm wire, ~3.5 kg/m in water."""
    return uniform_assembly(
        length_hint_m, 34.0, diameter_m=0.032, cd_normal=1.2,
        cd_tangential=0.008, mu=0.4, name="Lowering rope",
    )
