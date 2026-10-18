# -*- coding: utf-8 -*-
"""Span & Slack Analysis: where a planned cable spans, and where it may loop.

A 2D model of a cable laid along a route over a known seabed profile. The
inputs are the seabed (by KP), the cable makeup (types by KP and in-line
bodies), seabed friction and the lay control (bottom tension, or slack, by
KP). :func:`prepare` builds the stations once; :func:`solve` runs:

1. **Deposit tension.** Tension sections lay at their bottom tension. Slack
   sections lay the cable their slack gives; where that is less than the
   seabed needs within the friction reach, the cable is pulled taut
   (:func:`cable_seabed.shape.pull_taut`), else it lies at zero tension and
   the surplus is stored slack. One rule: tension and surplus are never both
   positive.
2. **Residual tension.** The lay history held by Coulomb friction
   (:mod:`cable_seabed.friction`): steps decay at ``mu w`` per metre, slopes
   steeper than the friction angle slide, crests grip (capstan).
3. **Rest shape.** The small-slope obstacle problem (Binda & Contro 1979),
   solved exactly with the weight per unit horizontal length corrected for
   the seabed slope (``w / (T cos^2 theta)``, the catenary curvature), bodies
   as extra weight and a standoff under their housing.
   Steps 2 and 3 are iterated: the reactions that carry friction come from
   the shape.
4. **Spans and supports** (:mod:`cable_seabed.spans`), slack accounting
   (planned, fill, used by the cable, surplus), and the highest tension that
   keeps spans within the reporting limits.
5. **Findings** as KP ranges, with the same terms and levels as the Lay
   Assessment. A span shorter than the seabed data can resolve
   (:data:`cable_seabed.sampling.RESOLVE_INTERVALS` data intervals) is
   indicative, not red or amber, and stretches where the data cannot resolve
   spans as short as the reporting length are flagged.

:func:`detail_window` solves a KP window with bending stiffness
(:mod:`cable_seabed.beam`); :func:`tension_sweep` repeats step 1-4 over a
range of bottom tensions; :func:`find_tension` finds the highest bottom
tension that keeps the spans within a target.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .beam import RIGID_EI_NM2, BeamResult, auto_spacing_m, solve_tensioned_beam
from .core import (
    ERROR,
    INFO,
    WARNING,
    RangeFinding,
    check_cancel,
    report,
    runs,
    status_bins,
    sub_progress,
)
from .features import SeabedFeature, apply_features
from .friction import friction_band, lay_history_tension, station_reactions
from .sampling import (
    MAX_FILTER_INTERVALS,
    NOISE_TO_GAP,
    RESOLVE_INTERVALS,
    SEABED_CONTOURS,
    SEABED_RPL,
    auto_filter_m,
    cell_noise_m,
    data_points,
    data_spacing_m,
    spike_indices,
)
from .shape import Shortfall, cable_rest_elevation, conformity_length_m, pull_taut, smooth_profile
from .spans import SPAN_END_TOL_M, Span, Support, allowable_tension_kn, find_spans, span_supports

# Lay control modes
TENSION = "tension"
BOTTOM_SLACK = "bottom_slack"
ROUTE_SLACK = "route_slack"
RPL_LEGS = "rpl_legs"
MODES = (TENSION, BOTTOM_SLACK, ROUTE_SLACK, RPL_LEGS)
MODE_LABELS = {
    TENSION: "Bottom tension (kN)",
    BOTTOM_SLACK: "Bottom slack (% over seabed)",
    ROUTE_SLACK: "Slack (% over route, RPL)",
    RPL_LEGS: "RPL slack per leg",
}
MODE_UNITS = {TENSION: "kN", BOTTOM_SLACK: "%", ROUTE_SLACK: "%", RPL_LEGS: "%"}

# Where a station's tension comes from (span "source").
SOURCE_ZERO, SOURCE_LAID, SOURCE_TAUT, SOURCE_SLOPE = 0, 1, 2, 3
SOURCE_NAMES = {
    SOURCE_ZERO: "laid at zero tension",
    SOURCE_LAID: "lay tension",
    SOURCE_TAUT: "cable short of the seabed",
    SOURCE_SLOPE: "tension held on a slope",
}

CONTACT_TOL_M = 0.01     # cable on the seabed (friction, span supports)
DEFAULT_MU = 0.5


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass
class CableType:
    name: str = "Cable"
    w_npm: float = math.nan           # submerged weight, N/m
    ei_knm2: float = math.nan         # bending stiffness
    mbr_m: float = math.nan
    diameter_m: float = math.nan
    npts_kn: float = math.nan

    def bending_length_m(self, tension_kn: float) -> float:
        """``sqrt(EI / T)``, nan when unknown."""
        if not (self.ei_knm2 > 0 and tension_kn > 0):
            return math.nan
        return math.sqrt(self.ei_knm2 / tension_kn)


@dataclass
class CableSection:
    kp_start: float
    kp_end: float
    cable: CableType


@dataclass
class Body:
    """An in-line body (repeater, joint, branching unit)."""

    name: str
    kp_km: float
    length_m: float = 0.0             # rigid length
    weight_kn: float = 0.0            # submerged weight
    diameter_m: float = math.nan
    kind: str = "Body"                # friction table key (e.g. Repeater, Joint)


@dataclass
class Protection:
    """Cable protection over a KP range (e.g. polymer half-shells or articulated pipe).

    It adds its weight in water to the cable's, may add bending stiffness,
    sets its own minimum bend (locking) radius when that is larger than the
    cable's, and its outside diameter. ``kind`` is the friction table key; a
    kind with no row there uses the cable's friction.
    """

    name: str
    kp_start: float
    kp_end: float
    weight_npm: float = 0.0           # added submerged weight, N/m
    diameter_m: float = math.nan      # outside diameter over the protection
    ei_knm2: float = math.nan         # added bending stiffness
    mbr_m: float = math.nan           # the protection's minimum bend radius
    kind: str = "Protection"


@dataclass
class SoilZone:
    kp_start: float
    kp_end: float
    soil: str


@dataclass
class FrictionEntry:
    item: str                          # cable type name or body kind; "*" = any
    soil: str                          # soil name; "*" = any
    mu_axial: float
    mu_lateral: float = math.nan


@dataclass
class FrictionModel:
    mu_axial: float = DEFAULT_MU
    mu_lateral: float = DEFAULT_MU
    zones: List[SoilZone] = field(default_factory=list)
    table: List[FrictionEntry] = field(default_factory=list)

    def soil_at(self, kp_km: float) -> str:
        found = ""
        for zone in self.zones:
            lo, hi = sorted((zone.kp_start, zone.kp_end))
            if lo <= kp_km <= hi:
                found = zone.soil
        return found

    def has_item(self, item: str) -> bool:
        """A table row names this cable type, body kind or protection kind."""
        key = _key(item)
        return bool(key) and any(_key(entry.item) == key for entry in self.table)

    def lookup(self, item: str, soil: str) -> Tuple[float, float]:
        """``(mu_axial, mu_lateral)``: exact item and soil first, then item on
        any soil, then any item on the soil, then the defaults."""
        item_key, soil_key = _key(item), _key(soil)
        best = None
        rank = 99
        for entry in self.table:
            e_item, e_soil = _key(entry.item), _key(entry.soil)
            item_ok = e_item in ("", "*") or e_item == item_key
            soil_ok = e_soil in ("", "*") or e_soil == soil_key
            if not (item_ok and soil_ok):
                continue
            r = (0 if e_item == item_key and item_key else 2) + (0 if e_soil == soil_key and soil_key else 1)
            if r < rank:
                best, rank = entry, r
        if best is None:
            return self.mu_axial, self.mu_lateral
        lateral = best.mu_lateral if math.isfinite(best.mu_lateral) else best.mu_axial
        return best.mu_axial, lateral


def _key(text: str) -> str:
    return "".join(ch for ch in str(text or "").upper() if ch.isalnum() or ch == "*")


@dataclass
class ControlSection:
    kp_start: float
    kp_end: float
    mode: str
    value: float


@dataclass
class LayControl:
    mode: str = TENSION
    value: float = 5.0
    sections: List[ControlSection] = field(default_factory=list)
    legs: List[Tuple[float, float, float]] = field(default_factory=list)   # RPL (kp0, kp1, slack %)
    direction: int = 1                 # +1 laid towards increasing KP

    def arrays(self, kp_km: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Per station ``(mode, value)``; RPL legs become route slack."""
        kp = np.asarray(kp_km, dtype=float)
        mode = np.full(len(kp), self.mode, dtype=object)
        value = np.full(len(kp), float(self.value))
        if self.mode == RPL_LEGS:
            mode[:] = ROUTE_SLACK
            for k0, k1, slack in self.legs:
                lo, hi = sorted((k0, k1))
                sel = (kp >= lo) & (kp <= hi)
                value[sel] = slack
        for section in self.sections:
            lo, hi = sorted((section.kp_start, section.kp_end))
            sel = (kp >= lo) & (kp <= hi)
            if section.mode == RPL_LEGS:
                mode[sel] = ROUTE_SLACK
                for k0, k1, slack in self.legs:
                    a, b = sorted((k0, k1))
                    value[sel & (kp >= a) & (kp <= b)] = slack
            else:
                mode[sel] = section.mode
                value[sel] = section.value
        return mode, value


@dataclass
class Criteria:
    min_gap_m: float = 0.3            # report spans higher than ...
    min_length_m: float = 5.0         # ... and longer than
    touchdown_gap_m: float = 0.0      # a span ends where the gap falls to this (0: exact lift-off / touchdown)
    max_reaction_kn: float = 0.0      # flag a support reaction above this (contact / crush; 0: off)
    red_gap_m: float = 1.0
    red_length_m: float = 50.0
    conform_below_kn: float = 0.05    # below this tension the cable follows the seabed
    loop_excess_pct: float = 8.0      # surplus slack at near-zero tension: loop risk
    loop_tension_kn: float = 0.1
    slack_window_m: float = 100.0     # window for local slack percentages
    max_slope_deg: float = 20.0       # small-slope model note above this
    shortfall_pct: float = 0.1        # flag laid cable short of the seabed by more than this
    max_reach_m: float = 1000.0
    allowable: bool = True            # find the highest tension that keeps spans within limits
    resolve_intervals: float = RESOLVE_INTERVALS   # spans shorter than this many data intervals: indicative (0 off)


def span_end_tol(criteria: Criteria) -> float:
    """The gap (m) below which the cable counts as touching at a span's end."""
    return max(float(criteria.touchdown_gap_m or 0.0), SPAN_END_TOL_M)


@dataclass
class Profile:
    """The seabed along the route: chainage (m), KP (km), depth (m, positive down)."""

    x_m: np.ndarray
    kp_km: np.ndarray
    depth_m: np.ndarray
    turns: List[Tuple[float, float]] = field(default_factory=list)   # alter courses (kp, heading change deg)
    notes: List[str] = field(default_factory=list)
    source: str = ""
    cell_m: Optional[np.ndarray] = None   # raster cell size per station (None: not a raster)
    data_m: Optional[np.ndarray] = None   # data spacing per station when the stations are not the data (RPL)
    kind: str = ""                        # SEABED_* of cable_seabed.sampling ("" unknown: a profile)


@dataclass
class PlanningInputs:
    profile: Profile
    cable: CableType
    sections: List[CableSection] = field(default_factory=list)
    bodies: List[Body] = field(default_factory=list)
    features: List[SeabedFeature] = field(default_factory=list)
    friction: FrictionModel = field(default_factory=FrictionModel)
    control: LayControl = field(default_factory=LayControl)
    criteria: Criteria = field(default_factory=Criteria)
    filter_m: Optional[float] = 2.0    # seabed noise filter the cable rests on (None: suited to the data)
    smoothing_m: Optional[float] = None   # seabed smoothing for lengths (None: conformity length)
    protection: List[Protection] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------
@dataclass
class BodyPlacement:
    body: Body
    i0: int
    i1: int
    standoff_m: float


@dataclass
class Stations:
    x_m: np.ndarray
    kp_km: np.ndarray
    raw_elev_m: np.ndarray            # sampled seabed + features (positive up)
    seabed_m: np.ndarray              # the seabed the cable rests on (filtered + features)
    length_seabed_m: np.ndarray       # seabed for lengths (smoothed + features)
    obstacle_m: np.ndarray            # seabed + body standoff
    feature_m: np.ndarray             # feature height above the seabed
    slope: np.ndarray                 # seabed slope dz/dx
    w_npm: np.ndarray                 # cable + protection + bodies
    w_cable_npm: np.ndarray           # cable + protection
    ei_knm2: np.ndarray
    mbr_m: np.ndarray
    diameter_m: np.ndarray
    npts_kn: np.ndarray
    mu: np.ndarray
    mu_lateral: np.ndarray
    cable_index: np.ndarray           # into cable_types
    body_index: np.ndarray            # into bodies (-1 none)
    soil: List[str]
    cable_types: List[CableType]
    bodies: List[BodyPlacement]
    spacing_m: float
    smoothing_m: float
    filter_m: float
    turns: List[Tuple[float, float]]
    notes: List[str]
    features: List[SeabedFeature] = field(default_factory=list)
    protection: List[Protection] = field(default_factory=list)
    protection_index: Optional[np.ndarray] = None   # into protection (-1 none)
    data_m: Optional[np.ndarray] = None   # how far apart the seabed data is around each station (m)
    noise_m: float = math.nan             # cell-to-cell variation of the data (robust standard deviation, m)
    filter_auto: bool = False             # the noise filter was suited to the data
    spikes_kp: List[float] = field(default_factory=list)   # possible fliers: single points above both neighbours

    @property
    def n(self) -> int:
        return len(self.x_m)

    def cable_at(self, i: int) -> CableType:
        return self.cable_types[int(self.cable_index[i])]


def prepare(inputs: PlanningInputs) -> Stations:
    """Stations with the seabed, makeup, bodies and friction per station."""
    prof = inputs.profile
    x = np.asarray(prof.x_m, dtype=float)
    kp = np.asarray(prof.kp_km, dtype=float)
    depth = np.asarray(prof.depth_m, dtype=float)
    ok = np.isfinite(x) & np.isfinite(kp)
    x, kp, depth = x[ok], kp[ok], depth[ok]
    order = np.argsort(x, kind="stable")
    x, kp, depth = x[order], kp[order], depth[order]
    keep = np.concatenate(([True], np.diff(x) > 1e-9))
    x, kp, depth = x[keep], kp[keep], depth[keep]
    if len(x) < 3 or np.isfinite(depth).sum() < 3:
        raise ValueError("The seabed profile needs at least three stations with a depth.")
    if np.any(np.diff(kp) < 0):
        raise ValueError("KP must increase with distance along the profile.")
    notes = list(prof.notes)

    # What the data resolves (before features, which are exact): how far apart the seabed is known around
    # each station, its cell-to-cell variation and single points standing above both neighbours.
    def aligned(values) -> Optional[np.ndarray]:
        if values is None or len(values) != len(ok):
            return None
        arr = np.array([np.nan if v is None else float(v) for v in values], dtype=float)
        return arr[ok][order][keep]

    given = aligned(prof.data_m)
    data0 = given if given is not None else data_spacing_m(x, aligned(prof.cell_m))
    data_typical = float(np.nanmedian(data0)) if np.isfinite(data0).any() else math.nan
    noise = math.nan
    spikes_kp: List[float] = []
    if prof.kind not in (SEABED_CONTOURS, SEABED_RPL):   # contours and RPL depths are not point samples
        noise = cell_noise_m(x, depth, data0)
        px, pd = data_points(x, depth, data0)
        spikes_kp = [float(np.interp(px[i], x, kp)) for i in spike_indices(px, pd, noise)]
    x0 = x.copy()
    x, kp, height, (depth,) = apply_features(x, kp, inputs.features, depth)
    data_m = np.interp(x, x0, data0) if len(x0) else np.full(len(x), np.nan)
    n = len(x)
    steps = np.diff(x)
    spacing = float(np.median(steps[steps > 0])) if (steps > 0).any() else 1.0
    raw = -depth

    # Cable makeup per station.
    types: List[CableType] = [inputs.cable]
    index = np.zeros(n, dtype=int)
    for section in inputs.sections:
        if section.cable is None:
            continue
        name = section.cable.name
        slot = next((i for i, t in enumerate(types) if t.name == name and t is section.cable), None)
        if slot is None:
            slot = len(types)
            types.append(section.cable)
        lo, hi = sorted((section.kp_start, section.kp_end))
        index[(kp >= lo) & (kp <= hi)] = slot
    used = sorted(set(index.tolist()))
    for slot in used:
        if not (types[slot].w_npm > 0):
            raise ValueError(f"Cable type '{types[slot].name}' has no submerged weight.")
    w_cable = np.array([types[i].w_npm for i in index], dtype=float)
    ei = np.array([types[i].ei_knm2 for i in index], dtype=float)
    mbr = np.array([types[i].mbr_m for i in index], dtype=float)
    diam = np.array([types[i].diameter_m for i in index], dtype=float)
    npts = np.array([types[i].npts_kn for i in index], dtype=float)

    # Protection: added weight and stiffness, its own MBR and outside diameter.
    protection: List[Protection] = []
    protection_index = np.full(n, -1, dtype=int)
    for item in inputs.protection:
        lo, hi = sorted((float(item.kp_start), float(item.kp_end)))
        sel = (kp >= lo) & (kp <= hi)
        if not sel.any():
            notes.append(f"Protection '{item.name}' (KP {lo:.3f} to {hi:.3f}) is outside the profile; left out.")
            continue
        added = float(item.weight_npm) if math.isfinite(item.weight_npm) else 0.0
        if np.any(w_cable[sel] + added <= 0.0):
            raise ValueError(f"Protection '{item.name}' makes the cable float: the model needs a weight in water.")
        protection_index[sel] = len(protection)
        protection.append(item)
        w_cable[sel] += added
        if math.isfinite(item.ei_knm2) and item.ei_knm2 > 0:
            ei[sel] = np.where(np.isfinite(ei[sel]), ei[sel], 0.0) + item.ei_knm2
        if math.isfinite(item.mbr_m) and item.mbr_m > 0:
            mbr[sel] = np.fmax(mbr[sel], item.mbr_m)
        if math.isfinite(item.diameter_m) and item.diameter_m > 0:
            diam[sel] = np.fmax(diam[sel], item.diameter_m)

    gap = float(inputs.criteria.min_gap_m)
    if inputs.filter_m is None:
        filter_m = auto_filter_m(data_typical, noise, gap, float(inputs.criteria.min_length_m))
        if math.isfinite(noise):
            if filter_m > 0:
                why = f"{filter_m:.2g} m"
            elif (NOISE_TO_GAP * noise / gap) ** 2 < 1.0:
                why = "none needed"
            else:   # too coarse to tell noise from relief: averaging would remove relief
                why = (f"none, as the data is coarser than half the {inputs.criteria.min_length_m:g} m reporting "
                       "length (its variation is relief, and its cells are already averages)")
            notes.append(f"Noise filter suited to the data: {why} (data every {data_typical:.2g} m, point-to-point "
                         f"variation σ {noise:.2f} m, against the {gap:g} m reporting gap).")
    else:
        filter_m = max(float(inputs.filter_m or 0.0), 0.0)
    resolves = (inputs.criteria.resolve_intervals or 0.0) * data_typical <= inputs.criteria.min_length_m
    if math.isfinite(noise) and math.isfinite(data_typical) and data_typical > 0 and gap > 0 and resolves:
        # (On data too coarse for the reporting length the variation is relief; the resolution findings say so.)
        residual = noise / math.sqrt(max(filter_m / data_typical, 1.0))
        if NOISE_TO_GAP * residual > gap:
            notes.append(f"The seabed varies by σ {noise:.2f} m from point to point (sounding noise or rough "
                         f"ground), {residual:.2f} m after the noise filter: spans under about "
                         f"{NOISE_TO_GAP * residual:.2f} m high may come from it. Check them against the soundings"
                         + ("." if filter_m >= MAX_FILTER_INTERVALS * data_typical * 0.99
                            else ", or set the noise filter to auto."))
    if spikes_kp:
        where = ", ".join(f"{k:.3f}" for k in spikes_kp[:6]) + (" …" if len(spikes_kp) > 6 else "")
        notes.append(f"{len(spikes_kp)} single point(s) stand well above both neighbours (KP {where}): possible "
                     "fliers in the soundings, or small objects. The cable can rest on each, with a span either "
                     "side; check them in the survey data (nothing is removed).")
    if inputs.smoothing_m is None:
        lengths = [conformity_length_m(types[i].ei_knm2, types[i].w_npm) for i in used]
        lengths = [v for v in lengths if math.isfinite(v)]
        smoothing = max(min(lengths) if lengths else 10.0, 3.0 * spacing)
    else:
        smoothing = max(float(inputs.smoothing_m), 0.0)
    seabed = (smooth_profile(x, raw, filter_m) if filter_m > 0 else raw.copy()) + height
    length_seabed = (smooth_profile(x, raw, smoothing) if smoothing > 0 else raw.copy()) + height

    # Bodies: weight spread over their length, standoff under the housing.
    trib = np.empty(n)
    trib[0], trib[-1] = 0.5 * steps[0], 0.5 * steps[-1]
    trib[1:-1] = 0.5 * (steps[1:] + steps[:-1])
    w = w_cable.copy()
    standoff = np.zeros(n)
    body_index = np.full(n, -1, dtype=int)
    placements: List[BodyPlacement] = []
    for j, body in enumerate(inputs.bodies):
        if not math.isfinite(body.kp_km) or body.kp_km < kp[0] or body.kp_km > kp[-1]:
            notes.append(f"Body '{body.name}' at KP {body.kp_km:.3f} is outside the profile; left out.")
            continue
        centre = float(np.interp(body.kp_km, kp, x))
        half = 0.5 * max(float(body.length_m or 0.0), 0.0)
        sel = (x >= centre - half - 1e-9) & (x <= centre + half + 1e-9)
        if not sel.any():
            sel = np.zeros(n, dtype=bool)
            sel[int(np.argmin(np.abs(x - centre)))] = True
        idx = np.nonzero(sel)[0]
        load = max(float(body.weight_kn or 0.0), 0.0) * 1000.0
        w[sel] += load / max(float(trib[sel].sum()), 1e-6)
        cable_d = float(np.nanmax(diam[sel])) if np.isfinite(diam[sel]).any() else math.nan
        off = 0.5 * (body.diameter_m - cable_d) if math.isfinite(body.diameter_m) and math.isfinite(cable_d) \
            else 0.0
        off = max(off, 0.0)
        standoff[sel] = np.maximum(standoff[sel], off)
        body_index[sel] = j
        placements.append(BodyPlacement(body, int(idx[0]), int(idx[-1]), off))

    # Slope of the seabed (smoothed over at least 5 m) for the slope corrections and friction angle.
    slope_seabed = smooth_profile(x, seabed, max(5.0, 2.0 * spacing))
    slope = np.zeros(n)
    finite = np.isfinite(slope_seabed)
    if finite.sum() >= 2:
        slope[finite] = np.gradient(slope_seabed[finite], x[finite])

    # Friction per station.
    fr = inputs.friction
    soil = [fr.soil_at(float(k)) for k in kp] if fr.zones else [""] * n
    mu = np.empty(n)
    mu_lat = np.empty(n)
    cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for i in range(n):
        j = body_index[i]
        item = inputs.bodies[j].kind if j >= 0 else types[index[i]].name
        p = protection_index[i]
        if j < 0 and p >= 0 and fr.has_item(protection[p].kind):
            item = protection[p].kind
        key = (item, soil[i])
        if key not in cache:
            cache[key] = fr.lookup(item, soil[i])
        mu[i], mu_lat[i] = cache[key]
    return Stations(x, kp, raw + height, seabed, length_seabed, seabed + standoff, height, slope, w, w_cable,
                    ei, mbr, diam, npts, mu, mu_lat, index, body_index, soil, types, placements, spacing,
                    smoothing, filter_m, list(prof.turns), notes, [f for f in inputs.features if f.valid()],
                    protection, protection_index, data_m=data_m, noise_m=noise,
                    filter_auto=inputs.filter_m is None, spikes_kp=spikes_kp)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class BodyResult:
    body: Body
    kp_km: float
    max_gap_m: float
    in_span: bool
    standoff_m: float
    tension_kn: float


@dataclass
class Summary:
    route_m: float = 0.0
    seabed_m: float = 0.0
    cable_shape_m: float = 0.0
    cable_laid_m: float = 0.0
    spans: int = 0                     # spans the seabed data resolves (the indicative ones are counted apart)
    red_spans: int = 0
    indicative_spans: int = 0          # shorter than the seabed data resolves
    span_length_m: float = 0.0
    longest_span_m: float = 0.0
    max_gap_m: float = 0.0
    min_bend_radius_m: float = math.inf
    below_mbr: int = 0
    max_reaction_kn: float = 0.0
    max_tension_kn: float = 0.0
    mean_tension_kn: float = 0.0
    over_npts: bool = False

    @property
    def fill_pct(self) -> float:
        return (self.seabed_m / self.route_m - 1.0) * 100.0 if self.route_m > 0 else math.nan

    @property
    def laid_slack_pct(self) -> float:
        return (self.cable_laid_m / self.route_m - 1.0) * 100.0 if self.route_m > 0 else math.nan


@dataclass
class PlanningResult:
    stations: Stations
    control: LayControl
    criteria: Criteria
    deposit_kn: np.ndarray            # tension at touchdown
    tension_kn: np.ndarray            # residual tension on the seabed
    source: np.ndarray                # SOURCE_* per station
    cable_m: np.ndarray               # cable elevation
    unheld: np.ndarray                # friction cannot hold the cable at zero tension
    reaction_npm: np.ndarray          # seabed reaction per metre
    fill_pct: np.ndarray              # seabed length over route, windowed
    shape_pct: np.ndarray             # cable used by the rest shape over route, windowed
    laid_pct: np.ndarray              # cable laid over route, windowed
    excess_pct: np.ndarray            # laid minus used, over route, windowed (stored slack)
    bottom_slack_pct: np.ndarray      # laid over seabed, windowed
    allowable_kn: Optional[np.ndarray]
    spans: List[Span]
    supports: List[Support]
    shortfalls: List[Shortfall]
    bodies: List[BodyResult]
    findings: List[RangeFinding]
    summary: Summary
    iterations: int
    notes: List[str] = field(default_factory=list)
    indicative: List[bool] = field(default_factory=list)   # per span: shorter than the seabed data resolves

    @property
    def gap_m(self) -> np.ndarray:
        return self.cable_m - self.stations.obstacle_m

    @property
    def kp_km(self) -> np.ndarray:
        return self.stations.kp_km

    def status(self, bin_m: float = 10.0) -> Tuple[np.ndarray, np.ndarray]:
        kp = self.stations.kp_km
        covered = kp[np.isfinite(self.stations.seabed_m)]
        return status_bins(float(kp[0]), float(kp[-1]), self.findings, covered, bin_m)


# ---------------------------------------------------------------------------
# Solve
# ---------------------------------------------------------------------------
def _curvature(st: Stations, tension_kn: np.ndarray, conform_kn: float,
               cable_slope: Optional[np.ndarray] = None) -> np.ndarray:
    """Allowed free-span curvature ``w / (T cos^2 phi)`` (the catenary's, with ``phi`` the
    cable's slope angle; ``w / T`` without a slope)."""
    cos2 = 1.0 if cable_slope is None else 1.0 / (1.0 + cable_slope ** 2)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(tension_kn > conform_kn, st.w_npm / (tension_kn * 1000.0 * cos2), np.inf)


def _shape(st: Stations, tension_kn: np.ndarray, conform_kn: float, cable_slope=None, surface=None) -> np.ndarray:
    base = st.obstacle_m if surface is None else surface
    return cable_rest_elevation(st.x_m, base, _curvature(st, tension_kn, conform_kn, cable_slope))


def _slope_of(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    out = np.zeros(len(x))
    ok = np.isfinite(y)
    if ok.sum() >= 2:
        out[ok] = np.gradient(y[ok], x[ok])
    return out


def _windowed(x: np.ndarray, seg_values: np.ndarray, window_m: float) -> np.ndarray:
    """Sum of per-segment values within ``x +- window / 2`` (partial segments pro rata)."""
    cum = np.concatenate(([0.0], np.cumsum(np.where(np.isfinite(seg_values), seg_values, 0.0))))
    half = 0.5 * max(window_m, 0.0)
    a = np.clip(x - half, x[0], x[-1])
    b = np.clip(x + half, x[0], x[-1])
    return np.interp(b, x, cum) - np.interp(a, x, cum)


def _range_runs(mask: np.ndarray, x: np.ndarray, merge_m: float = 20.0,
                min_length_m: float = 0.0) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for a, b in runs(mask):
        if out and x[a] - x[out[-1][1]] <= merge_m:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return [(a, b) for a, b in out if x[b] - x[a] >= min_length_m]


def allowable_window_m(length_m: float) -> float:
    """Bisection window for the highest tension within limits: 1/40 of the
    route, 25 to 250 m. One value covers a window, so a short, detailed
    study needs short windows to keep the limit local to its feature."""
    return min(250.0, max(25.0, length_m / 40.0))


def solve(st: Stations, control: LayControl, criteria: Criteria, allowable: Optional[bool] = None,
          cancel: Optional[Callable[[], bool]] = None, progress: Optional[Callable] = None,
          allowable_kn: Optional[np.ndarray] = None) -> PlanningResult:
    """Lay the cable (see the module docstring).

    The highest tension that keeps spans within the limits depends only on
    the stations and the reporting limits, not on the lay control: pass a
    previous result's ``allowable_kn`` to reuse it.
    """
    x, kp = st.x_m, st.kp_km
    n = st.n
    conform = float(criteria.conform_below_kn)
    report(progress, 0.0, "Deposit tension")
    mode, value = control.arrays(kp)
    tension_mode = mode == TENSION
    deposit = np.where(tension_mode, np.maximum(value, 0.0), 0.0)
    source = np.where(tension_mode & (deposit > conform), SOURCE_LAID, SOURCE_ZERO).astype(int)
    dx = np.diff(x)
    seg_ok = np.isfinite(st.length_seabed_m[1:]) & np.isfinite(st.length_seabed_m[:-1])
    d_seabed = np.where(seg_ok, np.hypot(dx, np.where(seg_ok, np.diff(st.length_seabed_m), 0.0)), 0.0)
    d_route = np.where(seg_ok, dx, 0.0)
    seg_mode = mode[:-1]
    seg_value = 0.5 * (value[1:] + value[:-1])
    seg_tension = seg_mode == TENSION
    d_laid_plan = np.where(seg_mode == BOTTOM_SLACK, d_seabed * (1.0 + seg_value / 100.0),
                           np.where(seg_tension, d_seabed, d_route * (1.0 + seg_value / 100.0)))
    shortfalls: List[Shortfall] = []
    notes: List[str] = list(st.notes)
    if (~tension_mode).any():
        check_cancel(cancel)
        cable_cum = np.concatenate(([0.0], np.cumsum(d_laid_plan)))
        mu_bar = float(np.nanmean(st.mu[~tension_mode])) if np.isfinite(st.mu[~tension_mode]).any() \
            else DEFAULT_MU
        min_reach = max(st.smoothing_m, 3.0 * st.spacing_m)
        deposit, source, _balance, _reach, shortfalls = pull_taut(
            x, kp, st.length_seabed_m, st.w_npm, deposit, source, x, cable_cum, mu_bar, criteria.max_reach_m,
            criteria.shortfall_pct, min_reach, st.npts_kn, conform, cancel=cancel,
            progress=sub_progress(progress, 0.02, 0.3))

    # Residual tension and rest shape, iterated (friction is carried by the shape's reactions).
    # The catenary curvature uses the cable's own slope, from the previous shape.
    tension = deposit.copy()
    unheld = np.zeros(n, dtype=bool)
    reaction = np.zeros(n)
    cable_slope = None
    iterations = 0
    for iterations in range(1, 7):
        check_cancel(cancel)
        report(progress, 0.3 + 0.05 * iterations, "Residual tension (friction) and rest shape")
        y = _shape(st, tension, conform, cable_slope)
        previous = cable_slope
        cable_slope = _slope_of(x, y)
        contact = np.isfinite(y) & ((y - st.obstacle_m) <= CONTACT_TOL_M)
        h_n = tension * 1000.0 / np.sqrt(1.0 + cable_slope ** 2)
        reaction = station_reactions(x, y, st.w_npm, h_n, contact)
        lower, upper, gravity = friction_band(x, y, st.w_npm, st.mu, reaction)
        new, unheld = lay_history_tension(deposit * 1000.0, lower, upper, gravity, control.direction)
        new = new / 1000.0
        change = float(np.nanmax(np.abs(new - tension))) if n else 0.0
        turn = float(np.max(np.abs(cable_slope - previous))) if previous is not None else math.inf
        tension = new
        if change < 0.005 + 0.002 * float(np.nanmax(tension) if n else 0.0) and turn < 0.01:
            break
    y = _shape(st, tension, conform, cable_slope)
    raised = tension > deposit + np.maximum(0.1, 0.05 * deposit)
    source = np.where(raised, SOURCE_SLOPE, source)
    source = np.where((tension <= conform) & (source != SOURCE_TAUT), SOURCE_ZERO, source)
    trib = np.empty(n)
    trib[0], trib[-1] = 0.5 * dx[0], 0.5 * dx[-1]
    trib[1:-1] = 0.5 * (dx[1:] + dx[:-1])
    reaction_npm = reaction / np.maximum(trib, 1e-9)

    # Spans and supports.
    report(progress, 0.62, "Spans and supports")
    gap = y - st.obstacle_m
    end_tol = span_end_tol(criteria)
    spans = find_spans(x, kp, gap, tension, criteria.min_gap_m, criteria.min_length_m, source, SOURCE_NAMES,
                       contact_tol_m=end_tol)
    supports = span_supports(x, st.obstacle_m, y, tension, st.w_npm, st.ei_knm2, st.mbr_m, spans, kp, end_tol)

    # Slack: planned (laid), fill (seabed), used by the rest shape, surplus.
    y_len = _shape(st, tension, conform, cable_slope, st.length_seabed_m)
    len_ok = seg_ok & np.isfinite(y_len[1:]) & np.isfinite(y_len[:-1])
    d_shape = np.where(len_ok, np.hypot(dx, np.where(len_ok, np.diff(y_len), 0.0)), 0.0)
    d_laid = np.where(seg_tension, d_shape, d_laid_plan)
    window = max(float(criteria.slack_window_m), 2.0 * st.spacing_m)
    route_w = _windowed(x, d_route, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        fill_pct = (_windowed(x, d_seabed, window) / route_w - 1.0) * 100.0
        shape_pct = (_windowed(x, d_shape, window) / route_w - 1.0) * 100.0
        laid_pct = (_windowed(x, d_laid, window) / route_w - 1.0) * 100.0
        excess_pct = (_windowed(x, d_laid - d_shape, window) / route_w) * 100.0
        bottom_slack_pct = (_windowed(x, d_laid, window) / _windowed(x, d_seabed, window) - 1.0) * 100.0
    for arr in (fill_pct, shape_pct, laid_pct, excess_pct, bottom_slack_pct):
        arr[~np.isfinite(route_w) | (route_w <= 0)] = np.nan

    if allowable_kn is not None and len(allowable_kn) != n:
        allowable_kn = None
    if allowable_kn is None and (criteria.allowable if allowable is None else allowable):
        check_cancel(cancel)
        allowable_kn = allowable_tension_kn(
            x, st.obstacle_m, st.w_npm, criteria.min_gap_m, criteria.min_length_m,
            core_m=allowable_window_m(float(x[-1] - x[0])), cancel=cancel,
            progress=sub_progress(progress, 0.65, 0.95), contact_tol_m=end_tol)
    if allowable_kn is not None:
        for span in spans:
            values = allowable_kn[span.i0:span.i1 + 1]
            span.allowable_kn = float(np.nanmin(values)) if np.isfinite(values).any() else math.nan

    bodies: List[BodyResult] = []
    for placement in st.bodies:
        sl = slice(placement.i0, placement.i1 + 1)
        g = gap[sl]
        in_span = any(span.i0 < placement.i1 and span.i1 > placement.i0 for span in spans)
        bodies.append(BodyResult(placement.body, placement.body.kp_km,
                                 float(np.nanmax(g)) if np.isfinite(g).any() else math.nan, in_span,
                                 placement.standoff_m, float(np.nanmax(tension[sl]))))

    indicative = [span.length_m < span_resolution_m(st, span, criteria) for span in spans]
    resolved = [span for span, below in zip(spans, indicative) if not below]
    summary = Summary()
    summary.route_m = float(d_route.sum())
    summary.seabed_m = float(d_seabed.sum())
    summary.cable_shape_m = float(d_shape.sum())
    summary.cable_laid_m = float(d_laid.sum())
    summary.spans = len(resolved)
    summary.red_spans = sum(1 for span in resolved if _span_red(span, criteria))
    summary.indicative_spans = len(spans) - len(resolved)
    summary.span_length_m = float(sum(span.length_m for span in resolved))
    summary.longest_span_m = float(max((span.length_m for span in resolved), default=0.0))
    summary.max_gap_m = float(max((span.max_gap_m for span in resolved), default=0.0))
    radii = [sp.bend_radius_m for sp in supports if not math.isnan(sp.bend_radius_m)]
    summary.min_bend_radius_m = float(min(radii)) if radii else math.inf
    summary.below_mbr = sum(1 for sp in supports if sp.below_mbr)
    summary.max_reaction_kn = float(max((sp.reaction_kn for sp in supports if math.isfinite(sp.reaction_kn)),
                                        default=0.0))
    summary.max_tension_kn = float(np.nanmax(tension)) if n else 0.0
    summary.mean_tension_kn = float(np.nanmean(tension)) if n else 0.0
    with np.errstate(invalid="ignore"):
        summary.over_npts = bool(np.any(np.isfinite(st.npts_kn) & (tension > st.npts_kn)))

    result = PlanningResult(st, control, criteria, deposit, tension, source, y, unheld, reaction_npm, fill_pct,
                            shape_pct, laid_pct, excess_pct, bottom_slack_pct, allowable_kn, spans, supports,
                            shortfalls, bodies, [], summary, iterations, notes, indicative)
    result.findings = findings(result)
    report(progress, 1.0, "Done")
    return result


def run(inputs: PlanningInputs, cancel=None, progress=None) -> PlanningResult:
    """:func:`prepare` then :func:`solve`."""
    stations = prepare(inputs)
    return solve(stations, inputs.control, inputs.criteria, cancel=cancel, progress=progress)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------
def _span_red(span: Span, criteria: Criteria) -> bool:
    return (span.max_gap_m >= criteria.red_gap_m or span.length_m >= criteria.red_length_m or span.below_mbr
            or _over_contact(span.max_reaction_kn, criteria))


def span_resolution_m(st: Stations, span: Span, criteria: Criteria) -> float:
    """The shortest span the seabed data resolves where ``span`` is (m), 0 when any is.

    ``resolve_intervals`` times the coarsest data spacing over the span and the
    stations either side. 0 where a seabed feature or an in-line body you
    entered carries the span (they are exact), or with the check off.
    """
    k = float(criteria.resolve_intervals or 0.0)
    if k <= 0 or st.data_m is None:
        return 0.0
    sl = slice(max(span.i0 - 1, 0), min(span.i1 + 2, st.n))
    if np.any(st.feature_m[sl] != 0) or np.any(st.body_index[sl] >= 0):
        return 0.0
    spacing = st.data_m[sl]
    spacing = spacing[np.isfinite(spacing)]
    return k * float(spacing.max()) if spacing.size else 0.0


def span_level(result: "PlanningResult", index: int) -> str:
    """``red``, ``amber`` or ``indicative`` (shorter than the seabed data resolves) for span ``index``."""
    if index < len(result.indicative) and result.indicative[index]:
        return "indicative"
    return "red" if _span_red(result.spans[index], result.criteria) else "amber"


def _over_contact(reaction_kn: float, criteria: Criteria) -> bool:
    limit = float(criteria.max_reaction_kn or 0.0)
    return limit > 0 and math.isfinite(reaction_kn) and reaction_kn > limit


def _radius_text(radius: float) -> str:
    if radius == math.inf or radius > 1000.0:
        return "over 1000 m"
    return f"{radius:.1f} m"


def findings(result: PlanningResult) -> List[RangeFinding]:
    st, c = result.stations, result.criteria
    x, kp = st.x_m, st.kp_km
    tension = result.tension_kn
    out: List[RangeFinding] = []

    for index, span in enumerate(result.spans):
        if index < len(result.indicative) and result.indicative[index]:
            need = span_resolution_m(st, span, c)
            out.append(RangeFinding(
                "span_indicative", INFO, min(span.kp_start, span.kp_end), max(span.kp_start, span.kp_end),
                f"Span {span.length_m:.0f} m long, up to {span.max_gap_m:.2f} m above the seabed at "
                f"{span.tension_kn:.2f} kN: indicative only. The seabed data here is every "
                f"{need / max(c.resolve_intervals, 1e-9):.2g} m, so spans shorter than about {need:.0f} m are not "
                "resolved: it may be longer, shorter or absent. Confirm it with finer bathymetry.",
                span.max_gap_m, c.red_gap_m, "m", extra={"length_m": span.length_m, "resolvable_m": need}))
            continue
        red = _span_red(span, c)
        parts = [f"Span {span.length_m:.0f} m long, up to {span.max_gap_m:.2f} m above the seabed at "
                 f"{span.tension_kn:.2f} kN ({span.source})"]
        if np.isfinite(span.min_bend_radius_m) or span.min_bend_radius_m == math.inf:
            text = f"bend radius at its supports {_radius_text(span.min_bend_radius_m)}"
            if span.below_mbr:
                text += f", BELOW the {span.mbr_m:.1f} m MBR"
            elif np.isfinite(span.mbr_m):
                text += f" (MBR {span.mbr_m:.1f} m)"
            parts.append(text)
        if np.isfinite(span.max_reaction_kn):
            parts.append(f"seabed reaction up to {span.max_reaction_kn:.2f} kN"
                         + (f", ABOVE the {c.max_reaction_kn:g} kN contact limit"
                            if _over_contact(span.max_reaction_kn, c) else ""))
        inside = [b.body.name for b in result.bodies if b.in_span and span.kp_start <= b.kp_km <= span.kp_end]
        if inside:
            parts.append("bodies in the span: " + ", ".join(inside))
        if np.isfinite(span.allowable_kn):
            parts.append("spans here even at the lowest tension" if span.allowable_kn <= 0
                         else f"no reportable span here below about {span.allowable_kn:.2f} kN")
        extra = {"length_m": span.length_m, "kp_max_gap": span.kp_max_gap, "tension_kn": span.tension_kn}
        for key in ("min_bend_radius_m", "max_reaction_kn", "allowable_kn"):
            value = getattr(span, key)
            if np.isfinite(value):
                extra[key] = float(value)
        out.append(RangeFinding("span", ERROR if red else WARNING, min(span.kp_start, span.kp_end),
                                max(span.kp_start, span.kp_end), "; ".join(parts), span.max_gap_m, c.red_gap_m,
                                "m", extra=extra))

    k = float(c.resolve_intervals or 0.0)
    if k > 0 and st.data_m is not None:
        need = k * st.data_m
        with np.errstate(invalid="ignore"):
            coarse = np.isfinite(st.seabed_m) & np.isfinite(need) & (st.feature_m == 0) & (need > c.min_length_m)
            severe = coarse & (need > c.red_length_m)
        for level, mask in ((WARNING, severe), (INFO, coarse & ~severe)):
            for a, b in _range_runs(mask, x, merge_m=50.0):
                spacing = st.data_m[a:b + 1]
                lo, hi = float(np.nanmin(spacing)), float(np.nanmax(spacing))
                every = f"{lo:.2g} m" if hi - lo < 0.05 * hi else f"{lo:.2g} to {hi:.2g} m"
                shortest = k * hi
                if level == WARNING:
                    text = (f"Seabed data every {every} here: spans shorter than about {shortest:.0f} m cannot be "
                            f"resolved, beyond the {c.red_length_m:g} m red length. Span risk here is not "
                            "assessed: spans shown are indicative and others may be missed. Use finer bathymetry.")
                else:
                    text = (f"Seabed data every {every} here: spans shorter than about {shortest:.0f} m cannot be "
                            f"resolved (you report from {c.min_length_m:g} m). Shorter spans shown are indicative, "
                            "and some may be missed.")
                out.append(RangeFinding("data_resolution", level, float(kp[a]), float(kp[b]), text, shortest,
                                        c.min_length_m, "m", extra={"data_spacing_m": hi}))

    for sp in result.supports:
        if _over_contact(sp.reaction_kn, c):
            where = "on a crest" if sp.crest else "where a span meets the seabed"
            out.append(RangeFinding(
                "contact", ERROR, float(kp[sp.i0]), float(kp[sp.i1]),
                f"Seabed reaction {sp.reaction_kn:.2f} kN {where}, above the {c.max_reaction_kn:g} kN contact "
                "limit: check the crush rating of the cable (or its protection) over the bearing length",
                sp.reaction_kn, c.max_reaction_kn, "kN"))

    with np.errstate(invalid="ignore"):
        over = np.isfinite(st.npts_kn) & (tension > st.npts_kn)
    for a, b in _range_runs(over, x):
        peak = float(np.nanmax(tension[a:b + 1]))
        limit = float(np.nanmin(st.npts_kn[a:b + 1]))
        out.append(RangeFinding("npts", ERROR, float(kp[a]), float(kp[b]),
                                f"Residual tension up to {peak:.2f} kN, above the cable's NPTS of {limit:.1f} kN",
                                peak, limit, "kN"))

    for item in result.shortfalls:
        over_npts = math.isfinite(item.npts_kn) and item.tension_kn > item.npts_kn
        if not item.feasible:
            text = (f"The slack laid here is {-item.balance_pct:.1f}% short of the seabed: even fully taut the "
                    "cable cannot cover it. Add slack.")
            level = ERROR
        else:
            text = (f"The slack laid here is {-item.balance_pct:.1f}% short of the seabed: the cable is pulled "
                    f"taut to about {item.tension_kn:.2f} kN, drawing on cable within {item.reach_m:.0f} m by "
                    "friction" + (" (above NPTS)" if over_npts else ""))
            level = ERROR if over_npts else INFO
        out.append(RangeFinding("short_cable", level, item.kp_start, item.kp_end, text, item.balance_pct,
                                -c.shortfall_pct, "%", extra={"tension_kn": item.tension_kn,
                                                              "reach_m": item.reach_m}))

    contact = np.isfinite(result.gap_m) & (result.gap_m <= CONTACT_TOL_M)
    with np.errstate(invalid="ignore"):
        steep = contact & (np.abs(st.slope) > st.mu)
    for a, b in _range_runs(steep, x, merge_m=20.0, min_length_m=2.0):
        slope_deg = float(np.degrees(np.arctan(np.nanmax(np.abs(st.slope[a:b + 1])))))
        angle = float(np.degrees(np.arctan(np.nanmin(st.mu[a:b + 1]))))
        change = float(np.nanmax(tension[a:b + 1] - result.deposit_kn[a:b + 1]))
        text = (f"Seabed slope up to {slope_deg:.0f}° is steeper than the friction angle {angle:.0f}° "
                f"(μ {np.nanmin(st.mu[a:b + 1]):.2f}): the cable slides until tension holds it")
        if change > 0.05:
            text += f"; residual tension here up to {change:.2f} kN above the lay tension"
        out.append(RangeFinding("sliding", INFO, float(kp[a]), float(kp[b]), text, slope_deg, angle, "°"))

    for a, b in _range_runs(result.unheld, x, merge_m=20.0, min_length_m=1.0):
        out.append(RangeFinding(
            "unheld", WARNING, float(kp[a]), float(kp[b]),
            "Friction cannot hold the cable on this slope even at zero tension: it slides down-slope and slack "
            "collects below (loop risk)"))

    with np.errstate(invalid="ignore"):
        loops = np.isfinite(result.excess_pct) & (result.excess_pct >= c.loop_excess_pct) \
            & (tension <= c.loop_tension_kn)
    if c.loop_excess_pct > 0:
        for a, b in _range_runs(loops, x):
            peak = float(np.nanmax(result.excess_pct[a:b + 1]))
            out.append(RangeFinding(
                "loop_risk", WARNING, float(kp[a]), float(kp[b]),
                f"{peak:.1f}% more cable than the seabed needs (over {c.slack_window_m:.0f} m) at zero tension: "
                "loops / snaking likely", peak, c.loop_excess_pct, "%"))

    for b in result.bodies:
        if np.isfinite(b.max_gap_m) and b.max_gap_m > c.min_gap_m:
            out.append(RangeFinding(
                "body", WARNING, b.kp_km, b.kp_km,
                f"{b.body.name} ({b.body.kind}) clear of the seabed by up to {b.max_gap_m:.2f} m"
                + (" in a span" if b.in_span else ""), b.max_gap_m, c.min_gap_m, "m"))

    with np.errstate(invalid="ignore"):
        steep_model = np.degrees(np.arctan(np.abs(st.slope))) > c.max_slope_deg
    for a, b in _range_runs(steep_model, x, merge_m=50.0, min_length_m=2.0):
        slope_deg = float(np.degrees(np.arctan(np.nanmax(np.abs(st.slope[a:b + 1])))))
        out.append(RangeFinding(
            "steep", INFO, float(kp[a]), float(kp[b]),
            f"Seabed slope up to {slope_deg:.0f}°: the small-slope shape model is less accurate above "
            f"{c.max_slope_deg:.0f}° (lengths stay exact); check spans here with the detail model",
            slope_deg, c.max_slope_deg, "°"))

    for turn_kp, turn_deg in st.turns:
        if not (kp[0] <= turn_kp <= kp[-1]) or abs(turn_deg) < 0.5:
            continue
        i = int(np.clip(np.searchsorted(kp, turn_kp), 0, len(kp) - 1))
        t_kn = float(tension[i])
        if t_kn <= c.conform_below_kn:
            continue
        w = float(st.w_npm[i])
        mu_l = float(st.mu_lateral[i])
        if not (w > 0 and mu_l > 0):
            continue
        radius = t_kn * 1000.0 / (mu_l * w)
        pull = t_kn * 1000.0 * math.radians(abs(turn_deg)) / (mu_l * w)
        out.append(RangeFinding(
            "alter_course", INFO, float(turn_kp), float(turn_kp),
            f"Alter course {turn_deg:+.1f}°: at {t_kn:.2f} kN the cable stays where laid only on a turn radius of "
            f"at least {radius:,.0f} m (lateral μ {mu_l:.2f}); a sharper turn lets the tension pull it across the "
            f"inside of the bend, over about {pull:,.0f} m. Not modelled in this 2D analysis.",
            radius, None, "m", extra={"turn_deg": float(turn_deg), "pull_m": pull}))

    out.sort(key=lambda f: (f.kp_start, -_LEVEL.get(f.severity, 0)))
    return out


_LEVEL = {INFO: 1, WARNING: 2, ERROR: 3}

CHECK_LABELS = {
    "span": "Span",
    "npts": "Tension above NPTS",
    "short_cable": "Cable short of the seabed",
    "sliding": "Slope steeper than friction",
    "unheld": "Cable not held (slides)",
    "loop_risk": "Loop risk (surplus slack)",
    "body": "Body clear of the seabed",
    "steep": "Steep slope (model note)",
    "contact": "Contact load at a support",
    "alter_course": "Alter course (lateral)",
    "span_indicative": "Span below the data's resolution",
    "data_resolution": "Seabed data too coarse",
}


# ---------------------------------------------------------------------------
# Detail model (bending stiffness) and tension sweep
# ---------------------------------------------------------------------------
@dataclass
class DetailSpan:
    span: Span
    min_bend_radius_m: float
    kp_min_radius: float
    max_moment_knm: float
    reaction_kn: Tuple[float, float]      # support reactions at the span's ends
    mbr_m: float

    @property
    def below_mbr(self) -> bool:
        return math.isfinite(self.mbr_m) and self.min_bend_radius_m < self.mbr_m


@dataclass
class DetailResult:
    beam: BeamResult
    kp_km: np.ndarray
    tension_kn: np.ndarray
    rigid: np.ndarray                     # body nodes
    mbr_m: np.ndarray
    spans: List[DetailSpan]
    bending_length_m: float
    notes: List[str]

    @property
    def min_bend_radius_m(self) -> float:
        radius = np.where(self.rigid, np.inf, self.beam.bend_radius_m)
        return float(np.min(radius)) if radius.size else math.inf


def detail_window(result: PlanningResult, kp0: float, kp1: float, spacing_m: Optional[float] = None,
                  cancel=None) -> DetailResult:
    """Solve ``kp0 .. kp1`` as a tensioned beam with bending stiffness.

    The residual tension and makeup come from the route model; the window's
    ends are held at the route model's shape. Bodies are stiff (rigid)
    elements.
    """
    st = result.stations
    x, kp = st.x_m, st.kp_km
    lo, hi = sorted((float(kp0), float(kp1)))
    x0, x1 = float(np.interp(lo, kp, x)), float(np.interp(hi, kp, x))
    if x1 - x0 < 1.0:
        raise ValueError("The detail window must be at least 1 m long.")
    sel = (x >= x0) & (x <= x1)
    ei_known = st.ei_knm2[sel & (st.body_index < 0)]
    ei_known = ei_known[np.isfinite(ei_known) & (ei_known > 0)]
    t_window = result.tension_kn[sel]
    t_max = float(np.nanmax(t_window)) if t_window.size and np.isfinite(t_window).any() else 0.0
    notes: List[str] = []
    ei_min_nm2 = float(ei_known.min()) * 1000.0 if ei_known.size else 0.0
    h_ref = max(t_max, criteria_floor(result.criteria)) * 1000.0
    lam = math.sqrt(ei_min_nm2 / h_ref) if ei_min_nm2 > 0 else math.nan
    if not ei_known.size:
        notes.append("No bending stiffness in the cable data: the detail model is a string (EI = 0).")
    step = float(spacing_m) if spacing_m else auto_spacing_m(ei_min_nm2, h_ref, x1 - x0)
    count = int(math.floor((x1 - x0) / step)) + 1
    if count < 6:
        count = 6
        step = (x1 - x0) / 5.0
    xg = x0 + np.arange(count) * step
    if math.isfinite(lam) and step > 0.5 * lam:
        notes.append(f"Grid {step:.2f} m is coarse against the bending length {lam:.2f} m: bend radii read high. "
                     "Narrow the window for a finer grid.")
    nearest = np.clip(np.searchsorted(x, xg), 0, len(x) - 1)
    left = np.clip(nearest - 1, 0, len(x) - 1)
    nearest = np.where(np.abs(x[left] - xg) < np.abs(x[nearest] - xg), left, nearest)
    obstacle = np.interp(xg, x, st.obstacle_m)
    w = np.interp(xg, x, st.w_npm)
    cos = 1.0 / np.sqrt(1.0 + np.interp(xg, x, _slope_of(x, result.cable_m)) ** 2)
    t_g = np.interp(xg, x, result.tension_kn)
    h_n = np.maximum(t_g, criteria_floor(result.criteria)) * 1000.0 * cos
    rigid = st.body_index[nearest] >= 0
    ei = np.where(np.isfinite(st.ei_knm2[nearest]), st.ei_knm2[nearest], 0.0) * 1000.0
    ei = np.where(rigid, RIGID_EI_NM2, ei)
    y_start = np.interp(xg, x, result.cable_m)
    ends = (y_start[0], y_start[1], y_start[-2], y_start[-1])
    beam = solve_tensioned_beam(xg, obstacle, w, h_n, ei, y_ends=ends, y_start=y_start, cancel=cancel)
    if not beam.converged:
        notes.append(f"The contact solver did not fully converge ({beam.method}, {beam.iterations} iterations).")
    kp_g = np.interp(xg, x, kp)
    mbr = st.mbr_m[nearest]
    spans = find_spans(xg, kp_g, beam.gap_m, t_g, result.criteria.min_gap_m, result.criteria.min_length_m,
                       contact_tol_m=span_end_tol(result.criteria))
    radius = np.where(rigid, np.inf, beam.bend_radius_m)
    moment = np.abs(beam.moment_nm) / 1000.0
    reach = int(max(3.0 * (lam if math.isfinite(lam) else 0.0), 2.0 * step) / step) + 1
    detail_spans: List[DetailSpan] = []
    for span in spans:
        a = max(span.i0 - reach, 0)
        b = min(span.i1 + reach, len(xg) - 1)
        local = radius[a:b + 1]
        k = a + int(np.argmin(local))
        reactions = []
        for end in (span.i0, span.i1):
            lo_i, hi_i = max(end - reach, 0), min(end + reach, len(xg) - 1)
            r = beam.reaction_n[lo_i:hi_i + 1]
            reactions.append(float(np.nansum(r)) / 1000.0 - float(np.sum(w[lo_i:hi_i + 1])) * step / 1000.0)
        cable_mbr = mbr[a:b + 1]
        detail_spans.append(DetailSpan(
            span, float(radius[k]), float(kp_g[k]), float(np.max(moment[a:b + 1])),
            (max(reactions[0], 0.0), max(reactions[1], 0.0)),
            float(np.nanmax(cable_mbr)) if np.isfinite(cable_mbr).any() else math.nan))
    return DetailResult(beam, kp_g, t_g, rigid, mbr, detail_spans, lam, notes)


def criteria_floor(criteria: Criteria) -> float:
    """Lowest tension (kN) the detail model uses: the conformity threshold."""
    return max(float(criteria.conform_below_kn), 0.001)


@dataclass
class SweepRow:
    tension_kn: float
    spans: int
    red_spans: int
    span_length_m: float
    longest_span_m: float
    max_gap_m: float
    min_bend_radius_m: float
    below_mbr: int
    max_tension_kn: float
    over_npts: bool


def tension_sweep(st: Stations, control: LayControl, criteria: Criteria, tensions_kn: Sequence[float],
                  kp_window: Optional[Tuple[float, float]] = None, cancel=None,
                  progress=None) -> List[SweepRow]:
    """Spans against a constant bottom tension (friction and slopes included).

    With a uniform cable on a frictionless seabed the span set only grows
    as the tension rises (Binda & Contro 1979); slopes, friction and bodies
    can bend that, so each tension is solved in full.
    """
    rows: List[SweepRow] = []
    base = replace(criteria, allowable=False)
    total = max(len(tensions_kn), 1)
    for k, t in enumerate(tensions_kn):
        check_cancel(cancel)
        report(progress, k / total, f"Bottom tension {t:.2f} kN ({k + 1} of {total})")
        result = solve(st, LayControl(TENSION, float(t), [], [], control.direction), base, allowable=False,
                       cancel=cancel)
        spans = result.spans
        supports = result.supports
        if kp_window is not None:
            lo, hi = sorted(kp_window)
            spans = [s for s in spans if s.kp_end >= lo and s.kp_start <= hi]
            ids = {i for s in spans for i in s.supports}
            supports = [sp for i, sp in enumerate(result.supports) if i in ids]
        radii = [sp.bend_radius_m for sp in supports if not math.isnan(sp.bend_radius_m)]
        rows.append(SweepRow(
            float(t), len(spans), sum(1 for s in spans if _span_red(s, base)),
            float(sum(s.length_m for s in spans)), float(max((s.length_m for s in spans), default=0.0)),
            float(max((s.max_gap_m for s in spans), default=0.0)),
            float(min(radii)) if radii else math.inf, sum(1 for sp in supports if sp.below_mbr),
            float(np.nanmax(result.tension_kn)), result.summary.over_npts))
    report(progress, 1.0, "Sweep done")
    return rows


@dataclass
class TensionTarget:
    """What :func:`find_tension` keeps the spans within.

    A span breaks the target when it is higher than ``gap_m`` and longer than
    ``length_m`` (with ``red_only``: when it is red by the criteria), when the
    bend at a support is below the MBR (``mbr``), or when a support reaction is
    above the criteria's contact limit (``contact``).
    """

    gap_m: float = 0.3
    length_m: float = 5.0
    red_only: bool = False
    mbr: bool = True
    contact: bool = True


@dataclass
class TensionAnswer:
    name: str
    kp_start: float
    kp_end: float
    tension_kn: float                 # highest constant bottom tension that meets the target (0: none)
    limited: bool                     # False: met at every tension searched
    governing: str = ""               # what breaks the target just above the answer
    governing_kp: float = math.nan
    remaining: List[Span] = field(default_factory=list)   # spans that break it even at the lowest tension
    fill_pct: float = math.nan        # seabed over route in the scope: the slack to follow the seabed
    note: str = ""


def _breaks(span: Span, target: TensionTarget, criteria: Criteria) -> str:
    """Why a span breaks the target ("" when it does not)."""
    reasons = []
    size = f"{span.length_m:.1f} m long, {span.max_gap_m:.2f} m high"
    if target.red_only:
        if span.max_gap_m >= criteria.red_gap_m or span.length_m >= criteria.red_length_m:
            reasons.append(size + " (red)")
    elif span.max_gap_m > target.gap_m and span.length_m > target.length_m:
        reasons.append(size)
    if target.mbr and span.below_mbr:
        reasons.append(f"bend at a support below the {span.mbr_m:.1f} m MBR")
    if target.contact and _over_contact(span.max_reaction_kn, criteria):
        reasons.append(f"support reaction {span.max_reaction_kn:.2f} kN above the contact limit")
    return "; ".join(reasons)


def scope_fill_pct(st: Stations, kp0: float, kp1: float) -> float:
    """Seabed length over route length in ``kp0 .. kp1`` (%): the slack the cable needs to follow the seabed."""
    lo, hi = sorted((kp0, kp1))
    sel = (st.kp_km >= lo) & (st.kp_km <= hi)
    idx = np.nonzero(sel)[0]
    if idx.size < 2:
        return math.nan
    x = st.x_m[idx[0]:idx[-1] + 1]
    z = st.length_seabed_m[idx[0]:idx[-1] + 1]
    ok = np.isfinite(z[1:]) & np.isfinite(z[:-1])
    dx = np.diff(x)[ok]
    route = float(dx.sum())
    return (float(np.hypot(dx, np.diff(z)[ok]).sum()) / route - 1.0) * 100.0 if route > 0 else math.nan


def find_tension(st: Stations, control: LayControl, criteria: Criteria, target: TensionTarget,
                 scopes: Sequence[Tuple[str, float, float]], hi_kn: float = 50.0, lo_kn: Optional[float] = None,
                 steps: int = 12, refine: int = 5, cancel=None, progress=None) -> List[TensionAnswer]:
    """The highest constant bottom tension that keeps the spans of each scope within ``target``.

    Spans only shorten as the tension falls (Binda & Contro 1979, for a
    uniform cable on a frictionless seabed), so the answer is a ceiling: lay
    at or below it. Each tension is solved in full (friction, slopes, bodies
    and protection included) on a geometric grid from ``lo_kn`` to
    ``hi_kn``, then bisected between the last grid tension that meets the
    target (with every lower one meeting it too) and the next. A crossing
    always spans: what remains at the lowest tension is reported, not hidden.
    """
    lo = max(float(lo_kn or 0.0), 2.0 * criteria_floor(criteria), 0.05)
    hi = max(float(hi_kn), 1.5 * lo)
    base = replace(criteria, allowable=False)
    if not target.red_only:
        base = replace(base, min_gap_m=min(criteria.min_gap_m, target.gap_m),
                       min_length_m=min(criteria.min_length_m, target.length_m))
    grid = [float(v) for v in np.geomspace(lo, hi, max(int(steps), 2))]
    total = len(grid) + refine * len(scopes)
    done = [0]

    def evaluate(t_kn: float) -> List[List[Tuple[Span, str]]]:
        check_cancel(cancel)
        report(progress, done[0] / total, f"Bottom tension {t_kn:.2f} kN")
        done[0] += 1
        result = solve(st, LayControl(TENSION, t_kn, [], [], control.direction), base, allowable=False,
                       cancel=cancel)
        out = []
        for _name, a, b in scopes:
            lo_kp, hi_kp = sorted((a, b))
            hits = [(s, _breaks(s, target, base)) for s in result.spans
                    if max(s.kp_start, s.kp_end) >= lo_kp and min(s.kp_start, s.kp_end) <= hi_kp]
            out.append([(s, why) for s, why in hits if why])
        return out

    table = [evaluate(t_kn) for t_kn in grid]
    answers: List[TensionAnswer] = []
    for j, (name, a, b) in enumerate(scopes):
        meets = [not row[j] for row in table]
        last = -1
        for k, ok in enumerate(meets):
            if not ok:
                break
            last = k
        note = ""
        if any(meets[k] for k in range(last + 2, len(meets))):
            note = ("A higher tension meets the target again (slopes, friction or bodies): the answer is the "
                    "highest tension below which every tension searched meets it.")
        fill = scope_fill_pct(st, a, b)
        if last < 0:
            hits = table[0][j]
            span, why = hits[0]
            answers.append(TensionAnswer(
                name, a, b, 0.0, True, f"KP {span.kp_start:.3f} to {span.kp_end:.3f}: {why} at {grid[0]:.2f} kN",
                span.kp_max_gap, [s for s, _ in hits], fill, note))
            continue
        if last == len(grid) - 1:
            answers.append(TensionAnswer(name, a, b, grid[-1], False, fill_pct=fill, note=note))
            continue
        good, bad = grid[last], grid[last + 1]
        hits = table[last + 1][j]
        for _ in range(refine):
            mid = math.sqrt(good * bad)
            verdict = evaluate(mid)[j]
            if verdict:
                bad, hits = mid, verdict
            else:
                good = mid
        span, why = hits[0]
        answers.append(TensionAnswer(
            name, a, b, good, True, f"KP {span.kp_start:.3f} to {span.kp_end:.3f}: {why} at {bad:.2f} kN",
            span.kp_max_gap, [], fill, note))
    report(progress, 1.0, "Done")
    return answers
