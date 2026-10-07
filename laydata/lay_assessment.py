# -*- coding: utf-8 -*-
"""Lay performance checks: suspensions, loop risk and tension limits.

UI- and QGIS-free (numpy only) so every calculation is unit-testable. The
Explorer's *Lay Assessment* tab maps a dataset's columns onto *roles*
(:data:`ROLES`), builds :class:`LayRecords`, runs the record checks and,
when a seabed profile has been sampled, the seabed checks; each check
returns :class:`RangeFinding` KP ranges. The method is documented in
``docs/LAY_ASSESSMENT.md``.

Seabed model
------------
Cable resting on the seabed under horizontal tension ``H`` with submerged
weight ``w`` per metre satisfies ``H y'' = w - p`` (small slopes), where
``y`` is the cable elevation and ``p >= 0`` the seabed contact pressure.
The cable therefore lies on the *least* curve above the seabed whose
curvature never exceeds ``c = w / H``: free spans are parabolas of that
curvature (sag ``w L^2 / 8H``), and wherever ``c`` is smaller than the
seabed's concave curvature the cable bridges. With ``Phi'' = c`` the curve
``y - Phi`` is the upper concave hull of ``seabed - Phi`` (an obstacle
problem), computed in one O(n) pass by :func:`cable_rest_elevation`.
Bending stiffness, seabed friction and lateral movement are ignored, and
``H`` is taken as the logged (model) bottom tension at deposit.

Friction makes everything local. Cable resting on the seabed only slides
where the tension changes by more than ``mu w`` per metre, so a tension ``H``
can draw cable from at most ``H / (mu w)`` either side. The laid cable is
balanced against the seabed within that reach; where it is short, the
tension that makes its rest shape use exactly the cable available is found
(:func:`taut_tension_n`) and the cable rests at the higher of that and the
logged tension. The seabed is smoothed over the cable's conformity length
``2 (EI / w)^(1/3)``: shorter features are bridged by bending stiffness and
would otherwise inflate the seabed length with sounding noise.

The cable hanging in the water can span a change of cable type; the
makeup along the route (:class:`KpMakeup`) gives the type at every height,
so tension limits and the weight in the top-tension identity use the cable
actually there (:func:`apply_makeup`).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .qc_base import ParamSpec, Severity

G = 9.80665  # m/s^2: kgf -> N and kg/m -> N/m

# Tension unit conversions to kN.
TENSION_UNITS: Dict[str, float] = {
    "kN": 1.0,
    "te (tonne-force)": G,          # 1 tf = 9.80665 kN
    "kgf": G / 1000.0,
    "lbf": 0.0044482216152605,
}


# ---------------------------------------------------------------------------
# Roles: what each check needs from the dataset, and how to find it
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Role:
    key: str
    label: str
    patterns: Tuple[str, ...]   # regexes on the normalised column name, priority order
    numeric: bool = True
    help: str = ""


def normalise_name(name: str) -> str:
    """Lower-case alphanumerics only: ``"Bot.Tension (kN)"`` -> ``"bottensionkn"``."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


ROLES: Tuple[Role, ...] = (
    Role("kp", "KP (touchdown)", (r"^tdkp", r"^touchdownkp", r"^kp$", r"^kpkm$", r"^pathkp", r"^newkp", r"^shipkp"),
         help="KP of the cable on the seabed, in km. Seabed checks and KP ranges use it."),
    Role("ship_kp", "KP (ship)", (r"^shipkp",),
         help="Ship KP (km), used for ship-side checks (top tension, payout). Optional."),
    Role("bottom_tension", "Bottom tension", (r"^bottension", r"^bottomtension", r"^bottens"),
         help="Bottom (touchdown) tension. In MakaiLay data this is a model output."),
    Role("bottom_slack", "Bottom slack (%)", (r"^instbotsl", r"^instbottomsl", r"^botslack", r"^bottomslack",
                                              r"^avgbotsl", r"^seabedslack", r"^avgseabedslack"),
         help="Seabed slack in %, 0 when the cable exactly follows the seabed profile."),
    Role("planned_slack", "Planned bottom slack (%)", (r"^plannedbo?t?slack", r"^plannedbottomslack", r"^plannedslack"),
         help="Planned seabed slack in % (optional)."),
    Role("top_tension", "Top tension (measured)", (r"^meastoptension", r"^meastopt", r"^toptension", r"^toptens"),
         help="Measured top tension at the cable engine / sheave."),
    Role("top_tension_calc", "Top tension (calculated)", (r"^calctoptension", r"^calctopt"),
         help="The lay model's calculated top tension (optional)."),
    Role("td_depth", "Water depth at touchdown (m)", (r"^tddepth", r"^touchdowndepth", r"^waterdepth",
                                                      r"^depthm$", r"^depth$"),
         help="Water depth at the touchdown point, metres (positive down)."),
    Role("sheave_height", "Sheave height above sea (m)", (r"^sheaveh",),
         help="Height of the overboard sheave above the sea surface (optional)."),
    Role("layback", "Layback (m)", (r"^layback",),
         help="Horizontal distance from the sheave to touchdown (optional; gives the hanging cable length)."),
    Role("ship_speed", "Ship speed", (r"^shipspeed", r"^sog$", r"^speedoverground"),
         help="Ship speed over ground, same units as payout speed."),
    Role("payout_speed", "Payout speed", (r"^payoutspeed", r"^cablepayout", r"^payout"),
         help="Cable payout speed, same units as ship speed."),
    Role("cable_type", "Cable type", (r"^tdcabletype", r"^botcabletype", r"^botcable", r"^cabletype"),
         numeric=False, help="Cable type at touchdown, matched to the cable library by name or alias."),
    Role("valid_flag", "Solution valid flag", (r"^solutionva", r"^solvalid", r"^valid"),
         numeric=False, help="Rows whose flag reads 0 / no / false / invalid are left out (optional)."),
    Role("td_lat", "Touchdown latitude (dd)", (r"^tdlatdd", r"^tdlatitudedd"),
         help="Decimal-degree touchdown latitude; the record position is used when unset."),
    Role("td_lon", "Touchdown longitude (dd)", (r"^tdlondd", r"^tdlongitudedd"),
         help="Decimal-degree touchdown longitude; the record position is used when unset."),
)
ROLE_BY_KEY: Dict[str, Role] = {role.key: role for role in ROLES}


def detect_roles(field_names: Sequence[str], is_numeric: Callable[[str], bool]) -> Dict[str, str]:
    """Best-guess ``{role_key: field_name}`` from column names (first pattern wins)."""
    normalised = [(name, normalise_name(name)) for name in field_names]
    found: Dict[str, str] = {}
    for role in ROLES:
        for pattern in role.patterns:
            regex = re.compile(pattern)
            match = next((name for name, norm in normalised
                          if regex.search(norm) and (not role.numeric or is_numeric(name))), None)
            if match is not None:
                found[role.key] = match
                break
    # KP (touchdown) and KP (ship) should not both fall back to the same column.
    if found.get("kp") and found.get("kp") == found.get("ship_kp"):
        found.pop("ship_kp")
    return found


# ---------------------------------------------------------------------------
# Cable properties
# ---------------------------------------------------------------------------
@dataclass
class CableProps:
    """Mechanical properties used by the checks (see cable_library)."""

    name: str
    weight_water_kg_m: Optional[float] = None
    weight_air_kg_m: Optional[float] = None
    cbl_kn: Optional[float] = None
    ntts_kn: Optional[float] = None
    nots_kn: Optional[float] = None
    npts_kn: Optional[float] = None
    mbr_m: Optional[float] = None
    bending_stiffness_knm2: Optional[float] = None

    @property
    def w_water_npm(self) -> float:
        """Submerged weight, N/m (nan when unknown)."""
        return _pos(self.weight_water_kg_m) * G

    @property
    def w_air_npm(self) -> float:
        """Weight in air, N/m (nan when unknown)."""
        return _pos(self.weight_air_kg_m) * G


def _pos(value) -> float:
    """Float value, nan when missing / non-numeric."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return math.nan
    return out if math.isfinite(out) else math.nan


# ---------------------------------------------------------------------------
# Records: the role columns as aligned numeric arrays
# ---------------------------------------------------------------------------
_INVALID_FLAGS = {"0", "0.0", "no", "n", "false", "f", "invalid", "bad", "none"}


@dataclass
class LayRecords:
    """Per-record arrays (tensions in kN), restricted to valid rows.

    ``rows`` maps back to dataset row indices. Missing roles are all-nan
    arrays; :meth:`has` says which roles were mapped.
    """

    rows: np.ndarray
    kp: np.ndarray
    time: Optional[np.ndarray]
    values: Dict[str, np.ndarray]
    mapped: Tuple[str, ...]
    lat: Optional[np.ndarray] = None
    lon: Optional[np.ndarray] = None
    cable: List[Optional[CableProps]] = field(default_factory=list)
    excluded: int = 0
    labels: List[Optional[str]] = field(default_factory=list)       # cable type label at touchdown
    hang_labels: List[Optional[str]] = field(default_factory=list)  # governing cable in the water

    @property
    def n(self) -> int:
        return len(self.rows)

    def has(self, *keys: str) -> bool:
        return all(key in self.mapped for key in keys)

    def get(self, key: str) -> np.ndarray:
        return self.values.get(key, np.full(self.n, np.nan))

    def cable_array(self, attribute: str) -> np.ndarray:
        out = np.full(self.n, np.nan)
        for i, props in enumerate(self.cable):
            if props is not None:
                out[i] = _pos(getattr(props, attribute, None))
        return out

    def order(self) -> np.ndarray:
        """Indices in time order when timed, else KP order."""
        if self.time is not None and np.any(np.isfinite(self.time)):
            keys = np.where(np.isfinite(self.time), self.time, np.inf)
        else:
            keys = np.where(np.isfinite(self.kp), self.kp, np.inf)
        return np.argsort(keys, kind="stable")


_TENSION_ROLES = ("bottom_tension", "top_tension", "top_tension_calc")


def build_records(dataset, mapping: Dict[str, str], tension_unit: str = "kN",
                  cable_for: Optional[Callable[[Optional[str]], Optional[CableProps]]] = None) -> LayRecords:
    """Pull the mapped role columns out of a ``LayDataset``.

    Rows without a finite KP, and rows whose valid flag reads as invalid,
    are left out. ``cable_for(type_text)`` resolves each row's cable
    (``type_text`` is None when no cable-type column is mapped).
    """
    factor = TENSION_UNITS.get(tension_unit, 1.0)
    n = dataset.row_count
    keep = np.ones(n, dtype=bool)
    kp_field = mapping.get("kp")
    if not kp_field or not dataset.has_field(kp_field):
        raise ValueError("Map a KP (touchdown) column first.")
    kp = dataset.numeric(kp_field)
    keep &= np.isfinite(kp)
    flag_field = mapping.get("valid_flag")
    if flag_field and dataset.has_field(flag_field):
        raw = dataset.raw(flag_field)
        invalid = np.array([str(v).strip().lower() in _INVALID_FLAGS if v is not None else False for v in raw])
        keep &= ~invalid
    rows = np.nonzero(keep)[0]
    values: Dict[str, np.ndarray] = {}
    mapped: List[str] = []
    for role in ROLES:
        name = mapping.get(role.key)
        if not name or not dataset.has_field(name) or not role.numeric:
            continue
        array = dataset.numeric(name)[rows].astype(float)
        if role.key in _TENSION_ROLES:
            array = array * factor
        values[role.key] = array
        mapped.append(role.key)
    for key in ("cable_type", "valid_flag"):
        name = mapping.get(key)
        if name and dataset.has_field(name):
            mapped.append(key)
    time = dataset.time_epoch[rows] if dataset.time_epoch is not None else None
    lat = values.pop("td_lat", None)
    lon = values.pop("td_lon", None)
    if lat is None or lon is None:
        lat = dataset.lat[rows] if dataset.has_geometry else None
        lon = dataset.lon[rows] if dataset.has_geometry else None
    type_field = mapping.get("cable_type")
    types = dataset.raw(type_field)[rows] if type_field and dataset.has_field(type_field) else [None] * len(rows)
    labels: List[Optional[str]] = []
    for value in types:
        text = None if value is None else (str(value).strip() or None)
        labels.append(None if text == "NULL" else text)
    cables: List[Optional[CableProps]] = [None] * len(rows)
    if cable_for is not None:
        cache: Dict[Optional[str], Optional[CableProps]] = {}
        for i, text in enumerate(labels):
            if text not in cache:
                cache[text] = cable_for(text)
            cables[i] = cache[text]
    return LayRecords(rows=rows, kp=kp[rows].astype(float), time=time, values=values,
                      mapped=tuple(mapped), lat=lat, lon=lon, cable=cables, excluded=int(n - len(rows)),
                      labels=labels, hang_labels=list(labels))


# ---------------------------------------------------------------------------
# Findings and range grouping
# ---------------------------------------------------------------------------
SEVERITY_LEVEL = {Severity.INFO: 1, Severity.WARNING: 2, Severity.ERROR: 3}
LEVEL_SEVERITY = {level: severity for severity, level in SEVERITY_LEVEL.items()}


@dataclass
class RangeFinding:
    """One flagged KP range."""

    check_id: str
    severity: str
    kp_start: float
    kp_end: float
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    unit: str = ""
    rows: Tuple[int, ...] = ()          # record indices (LayRecords order), for map/table links
    time_start: Optional[float] = None
    time_end: Optional[float] = None
    extra: Dict[str, float] = field(default_factory=dict)

    @property
    def length_m(self) -> float:
        return abs(self.kp_end - self.kp_start) * 1000.0


def group_ranges(records: LayRecords, level: np.ndarray, value: np.ndarray, check_id: str,
                 describe: Callable[[int, float], str], worst: str = "max",
                 threshold_for: Optional[Callable[[int], Optional[float]]] = None,
                 unit: str = "", kp: Optional[np.ndarray] = None,
                 merge_m: float = 20.0) -> List[RangeFinding]:
    """Group flagged records (``level > 0``) into KP ranges.

    Records are walked in time order; consecutive flagged records form a
    run, then runs whose KP extents overlap or lie within ``merge_m`` merge.
    A range takes its records' highest level and their worst ``value``
    (``worst`` is "max" or "min"); ``describe(level, value)`` writes the
    message.
    """
    kp = records.kp if kp is None else kp
    order = records.order()
    runs: List[List[int]] = []
    current: List[int] = []
    for idx in order:
        if level[idx] > 0 and np.isfinite(kp[idx]):
            current.append(int(idx))
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    spans = []
    for run in runs:
        k = kp[run]
        spans.append([float(np.min(k)), float(np.max(k)), run])
    spans.sort(key=lambda item: item[0])
    merged: List[list] = []
    for start, end, run in spans:
        if merged and start <= merged[-1][1] + merge_m / 1000.0:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = merged[-1][2] + run
        else:
            merged.append([start, end, list(run)])
    out: List[RangeFinding] = []
    for start, end, run in merged:
        idx = np.asarray(run, dtype=int)
        lvl = int(np.max(level[idx]))
        vals = value[idx]
        finite = np.isfinite(vals)
        if finite.any():
            pick = idx[finite][int(np.argmax(vals[finite]) if worst == "max" else np.argmin(vals[finite]))]
            worst_value = float(value[pick])
        else:
            pick, worst_value = int(idx[0]), None
        times = records.time[idx] if records.time is not None else None
        t_ok = times[np.isfinite(times)] if times is not None else np.array([])
        out.append(RangeFinding(
            check_id=check_id, severity=LEVEL_SEVERITY.get(lvl, Severity.WARNING),
            kp_start=start, kp_end=end,
            message=describe(lvl, worst_value if worst_value is not None else math.nan),
            value=worst_value, threshold=threshold_for(pick) if threshold_for else None, unit=unit,
            rows=tuple(int(i) for i in idx),
            time_start=float(t_ok.min()) if t_ok.size else None,
            time_end=float(t_ok.max()) if t_ok.size else None))
    return out


# ---------------------------------------------------------------------------
# Record checks
# ---------------------------------------------------------------------------
@dataclass
class CheckDef:
    check_id: str
    label: str
    description: str
    params: Tuple[ParamSpec, ...]
    needs: Tuple[str, ...]             # roles that must be mapped
    needs_cable: Tuple[str, ...] = ()  # CableProps attributes needed
    needs_seabed: bool = False
    default_enabled: bool = True


def default_params(check: CheckDef) -> Dict[str, object]:
    return {spec.name: spec.default for spec in check.params}


def _fmt(value: float, unit: str = "", digits: int = 2) -> str:
    return "-" if value is None or not np.isfinite(value) else f"{value:.{digits}f}{(' ' + unit) if unit else ''}"


def _hanging(records: LayRecords):
    """(tension, NOTS, NTTS, labels) of the governing cable in the water column:
    from :func:`apply_makeup` when it ran, else the top tension on the
    touchdown cable."""
    if "hang_tension_kn" in records.values:
        return (records.get("hang_tension_kn"), records.get("hang_nots_kn"), records.get("hang_ntts_kn"),
                records.hang_labels)
    return (records.get("top_tension"), records.cable_array("nots_kn"), records.cable_array("ntts_kn"),
            records.labels)


def check_top_tension(records: LayRecords, params) -> List[RangeFinding]:
    tension, nots, ntts, labels = _hanging(records)
    level = np.zeros(records.n, dtype=int)
    with np.errstate(invalid="ignore"):
        level[(tension > nots) & np.isfinite(nots)] = 2
        level[(tension > ntts) & np.isfinite(ntts)] = 3
    kp = records.get("ship_kp") if records.has("ship_kp") else records.kp
    worst = {}

    def threshold(i):
        worst["label"] = labels[i] if i < len(labels) else None
        return float(ntts[i] if level[i] == 3 else nots[i])

    def describe(lvl, value):
        limit = "NTTS" if lvl == 3 else "NOTS"
        return f"Cable tension {_fmt(value, 'kN', 1)} above {limit}"
    findings = group_ranges(records, level, tension, "top_tension", describe, "max", threshold, "kN",
                            kp=kp, merge_m=params.get("merge_m", 20.0))
    for finding in findings:
        idx = np.asarray(finding.rows)
        pick = idx[int(np.nanargmax(tension[idx]))] if np.isfinite(tension[idx]).any() else idx[0]
        label = labels[pick] if pick < len(labels) else None
        limit = "NTTS" if finding.severity == Severity.ERROR else "NOTS"
        transition = records.get("hang_transition")[pick] > 0 if "hang_transition" in records.values else False
        where = " at the joint in the water" if transition and tension[pick] < records.get("top_tension")[pick]             else " at the sheave"
        finding.message = (f"Cable tension {_fmt(finding.value, 'kN', 1)}{where} above {limit}"
                           + (f" of {label}" if label else ""))
    return findings


def check_bottom_tension(records: LayRecords, params) -> List[RangeFinding]:
    bottom = records.get("bottom_tension")
    npts = records.cable_array("npts_kn")
    limit = float(params.get("max_bottom_kn") or 0.0)
    level = np.zeros(records.n, dtype=int)
    with np.errstate(invalid="ignore"):
        if limit > 0:
            level[bottom > limit] = 2
        level[(bottom > npts) & np.isfinite(npts)] = 3

    def describe(lvl, value):
        return (f"Bottom tension {_fmt(value, 'kN', 2)} above NPTS (permanent limit)" if lvl == 3
                else f"Bottom tension {_fmt(value, 'kN', 2)} above the {limit:g} kN target")
    return group_ranges(records, level, bottom, "bottom_tension", describe, "max",
                        lambda i: float(npts[i]) if level[i] == 3 else limit, "kN",
                        merge_m=params.get("merge_m", 20.0))


def estimated_bottom_tension(records: LayRecords) -> np.ndarray:
    """Bottom tension from measured top tension: ``T_top - w_water d - w_air h``.

    ``w_water`` is the mean submerged weight of the cable hanging in the water
    when :func:`apply_makeup` has run (a type change mid-water), else the
    touchdown cable's.

    The steady-lay identity (Zajac 1957; exact without tangential drag): the
    tension change between touchdown and the sheave equals the cable's
    submerged weight times the water depth, plus its in-air weight times the
    sheave height when that is known.
    """
    top = records.get("top_tension")
    depth = records.get("td_depth")
    w_water = records.get("hang_w_npm") if "hang_w_npm" in records.values else records.cable_array("w_water_npm")
    w_air = records.cable_array("w_air_npm")
    height = records.get("sheave_height") if records.has("sheave_height") else np.zeros(records.n)
    air = np.where(np.isfinite(w_air) & np.isfinite(height), w_air * height, 0.0)
    return top - (w_water * np.abs(depth) + air) / 1000.0


def check_top_tension_consistency(records: LayRecords, params) -> List[RangeFinding]:
    tolerance = float(params.get("tolerance_kn", 2.0))
    top = records.get("top_tension")
    if records.has("top_tension_calc"):
        diff = top - records.get("top_tension_calc")
        what = "measured vs calculated top tension"
    else:
        diff = estimated_bottom_tension(records) - records.get("bottom_tension")
        what = "bottom tension from measured top tension vs logged bottom tension"
    level = np.zeros(records.n, dtype=int)
    with np.errstate(invalid="ignore"):
        level[np.abs(diff) > tolerance] = 2

    def describe(_lvl, value):
        return f"Differs by {_fmt(value, 'kN', 1)} ({what}): the lay model may not match the cable"
    absdiff = np.abs(diff)
    findings = group_ranges(records, level, absdiff, "tension_consistency", describe, "max",
                            lambda i: tolerance, "kN", merge_m=params.get("merge_m", 20.0))
    for finding in findings:
        # Report the signed difference of the worst record.
        idx = np.asarray(finding.rows)
        worst = idx[int(np.nanargmax(absdiff[idx]))] if np.isfinite(absdiff[idx]).any() else None
        if worst is not None:
            finding.value = float(diff[worst])
    return findings


def check_laid_under_tension(records: LayRecords, params) -> List[RangeFinding]:
    threshold = float(params.get("tension_kn", 0.5))
    bottom = records.get("bottom_tension")
    level = np.zeros(records.n, dtype=int)
    with np.errstate(invalid="ignore"):
        level[bottom > threshold] = 2

    def describe(_lvl, value):
        return f"Laid with no bottom slack, bottom tension up to {_fmt(value, 'kN', 2)}: suspensions possible"
    return group_ranges(records, level, bottom, "laid_under_tension", describe, "max",
                        lambda i: threshold, "kN", merge_m=params.get("merge_m", 20.0))


def check_loop_risk(records: LayRecords, params) -> List[RangeFinding]:
    tension_max = float(params.get("tension_kn", 0.1))
    slack_min = float(params.get("slack_pct", 8.0))
    depth_max = float(params.get("max_depth_m", 0.0) or 0.0)
    bottom = records.get("bottom_tension")
    slack = records.get("bottom_slack")
    level = np.zeros(records.n, dtype=int)
    with np.errstate(invalid="ignore"):
        low_tension = (bottom <= tension_max) | ~np.isfinite(bottom) if records.has("bottom_tension") \
            else np.ones(records.n, dtype=bool)
        risky = low_tension & (slack >= slack_min)
        if depth_max > 0 and records.has("td_depth"):
            risky &= np.abs(records.get("td_depth")) <= depth_max
    level[risky] = 2

    def describe(_lvl, value):
        return f"Bottom slack up to {_fmt(value, '%', 1)} at near-zero bottom tension: loop / kink risk"
    return group_ranges(records, level, slack, "loop_risk", describe, "max",
                        lambda i: slack_min, "%", merge_m=params.get("merge_m", 20.0))


def check_payout_while_stopped(records: LayRecords, params) -> List[RangeFinding]:
    ship_max = float(params.get("ship_speed_max", 0.2))
    payout_min = float(params.get("payout_min", 0.2))
    ship = records.get("ship_speed")
    payout = records.get("payout_speed")
    level = np.zeros(records.n, dtype=int)
    with np.errstate(invalid="ignore"):
        level[(np.abs(ship) <= ship_max) & (payout >= payout_min)] = 2
    kp = records.get("ship_kp") if records.has("ship_kp") else records.kp

    def describe(_lvl, value):
        return f"Paying out ({_fmt(value, '', 2)}) while the ship is stopped: cable may pile up / loop"
    return group_ranges(records, level, payout, "payout_stopped", describe, "max",
                        lambda i: payout_min, "", kp=kp, merge_m=params.get("merge_m", 20.0))


def check_td_reversal(records: LayRecords, params) -> List[RangeFinding]:
    """Touchdown KP moving back over ground already laid (in time order)."""
    min_back_m = float(params.get("min_back_m", 5.0))
    if records.time is None:
        return []
    order = records.order()
    kp = records.kp[order]
    level_sorted = np.zeros(len(order), dtype=int)
    back_sorted = np.zeros(len(order))
    direction = np.sign(np.nanmedian(np.diff(kp))) if len(kp) > 1 else 1.0
    direction = direction or 1.0
    furthest = -np.inf
    for j, value in enumerate(kp * direction):
        if not np.isfinite(value):
            continue
        furthest = max(furthest, value)
        back = (furthest - value) * 1000.0
        back_sorted[j] = back
        if back >= min_back_m:
            level_sorted[j] = 2
    level = np.zeros(records.n, dtype=int)
    back = np.zeros(records.n)
    level[order] = level_sorted
    back[order] = back_sorted

    def describe(_lvl, value):
        return f"Touchdown moved back {_fmt(value, 'm', 0)} over cable already laid: loop risk"
    return group_ranges(records, level, back, "td_reversal", describe, "max",
                        lambda i: min_back_m, "m", merge_m=params.get("merge_m", 20.0))


def check_planned_slack(records: LayRecords, params) -> List[RangeFinding]:
    tolerance = float(params.get("tolerance_pct", 2.0))
    diff = records.get("bottom_slack") - records.get("planned_slack")
    level = np.zeros(records.n, dtype=int)
    with np.errstate(invalid="ignore"):
        level[np.abs(diff) > tolerance] = 1
    absdiff = np.abs(diff)

    def describe(_lvl, value):
        return f"Bottom slack off plan by up to {_fmt(value, '%', 1)}"
    return group_ranges(records, level, absdiff, "planned_slack", describe, "max",
                        lambda i: tolerance, "%", merge_m=params.get("merge_m", 20.0))


_MERGE = ParamSpec("merge_m", "Merge ranges closer than (m)", "float", 20.0, minimum=0.0)

RECORD_CHECKS: Tuple[Tuple[CheckDef, Callable], ...] = (
    (CheckDef("laid_under_tension", "Laid under bottom tension",
              "Records with no bottom slack (bottom tension above the threshold). The cable "
              "cannot follow seabed hollows there; run the seabed checks to see where it spans.",
              (ParamSpec("tension_kn", "Bottom tension above (kN)", "float", 0.5, minimum=0.0), _MERGE),
              ("bottom_tension",)), check_laid_under_tension),
    (CheckDef("bottom_tension", "Bottom tension limits",
              "Bottom (residual) tension above NPTS (red), or above an optional target (amber).",
              (ParamSpec("max_bottom_kn", "Amber above (kN, 0 = off)", "float", 0.0, minimum=0.0), _MERGE),
              ("bottom_tension",), ("npts_kn",)), check_bottom_tension),
    (CheckDef("top_tension", "Top tension limits",
              "Measured top tension above NOTS (amber) or NTTS (red).",
              (_MERGE,), ("top_tension",), ("nots_kn", "ntts_kn")), check_top_tension),
    (CheckDef("tension_consistency", "Top tension consistency",
              "Measured top tension against the model: calculated top tension when logged, else "
              "measured top tension minus submerged weight x depth against the logged bottom tension.",
              (ParamSpec("tolerance_kn", "Tolerance (kN)", "float", 2.0, minimum=0.0), _MERGE),
              ("top_tension",)), check_top_tension_consistency),
    (CheckDef("loop_risk", "Loop risk (excess slack)",
              "Near-zero bottom tension with high bottom slack: surplus cable that can form loops "
              "and kinks, especially in shallow water or with torque-unbalanced cable.",
              (ParamSpec("slack_pct", "Bottom slack at or above (%)", "float", 8.0),
               ParamSpec("tension_kn", "Bottom tension at or below (kN)", "float", 0.1, minimum=0.0),
               ParamSpec("max_depth_m", "Only shallower than (m, 0 = all)", "float", 0.0, minimum=0.0),
               _MERGE),
              ("bottom_slack",)), check_loop_risk),
    (CheckDef("payout_stopped", "Payout while stopped",
              "Cable paid out while the ship is (nearly) stopped: cable piles at touchdown.",
              (ParamSpec("ship_speed_max", "Ship speed at or below", "float", 0.2, minimum=0.0),
               ParamSpec("payout_min", "Payout speed at or above", "float", 0.2, minimum=0.0),
               _MERGE),
              ("ship_speed", "payout_speed")), check_payout_while_stopped),
    (CheckDef("td_reversal", "Touchdown moving back",
              "Touchdown KP moving back over cable already laid (time order): loop risk.",
              (ParamSpec("min_back_m", "Moved back at least (m)", "float", 5.0, minimum=0.0), _MERGE),
              ()), check_td_reversal),
    (CheckDef("planned_slack", "Slack off plan",
              "Bottom slack differing from the planned bottom slack.",
              (ParamSpec("tolerance_pct", "Tolerance (%)", "float", 2.0, minimum=0.0), _MERGE),
              ("bottom_slack", "planned_slack")), check_planned_slack),
)


# ---------------------------------------------------------------------------
# Cable makeup along the route, and the cable hanging in the water
# ---------------------------------------------------------------------------
@dataclass
class KpMakeup:
    """Cable type labels along route KP: sorted, non-overlapping ranges."""

    starts: np.ndarray
    ends: np.ndarray
    labels: List[str]
    source: str = ""

    @classmethod
    def from_ranges(cls, ranges, source: str = "") -> "KpMakeup":
        """``ranges``: iterable of ``(kp_a, kp_b, label)``; equal neighbours merge."""
        rows = []
        for kp_a, kp_b, label in ranges:
            if label is None or not str(label).strip():
                continue
            a, b = _pos(kp_a), _pos(kp_b)
            if not (np.isfinite(a) and np.isfinite(b)):
                continue
            rows.append((min(a, b), max(a, b), str(label).strip()))
        rows.sort(key=lambda r: r[0])
        merged: List[list] = []
        for a, b, label in rows:
            if merged and merged[-1][2] == label and a <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b, label])
        return cls(np.array([m[0] for m in merged], dtype=float), np.array([m[1] for m in merged], dtype=float),
                   [m[2] for m in merged], source)

    @property
    def empty(self) -> bool:
        return not self.labels

    def label_at(self, kp_km) -> np.ndarray:
        """Label per KP (object array; None outside every range)."""
        kp = np.atleast_1d(np.asarray(kp_km, dtype=float))
        out = np.full(kp.shape, None, dtype=object)
        if self.empty:
            return out
        index = np.searchsorted(self.starts, kp, side="right") - 1
        tol = 1e-6
        for i, (k, j) in enumerate(zip(kp, index)):
            if np.isfinite(k) and j >= 0 and k <= self.ends[j] + tol:
                out[i] = self.labels[j]
            elif np.isfinite(k) and j + 1 < len(self.starts) and abs(self.starts[j + 1] - k) <= tol:
                out[i] = self.labels[j + 1]
        return out

    def boundaries(self) -> np.ndarray:
        """KPs where the label changes."""
        return np.array([self.ends[i] for i in range(len(self.labels) - 1)
                         if self.labels[i] != self.labels[i + 1]], dtype=float)


def makeup_from_records(kp: np.ndarray, labels: Sequence) -> KpMakeup:
    """KP ranges of constant label from per-record labels (cut midway between runs)."""
    kp = np.asarray(kp, dtype=float)
    ok = np.array([np.isfinite(k) and lab is not None and str(lab).strip() not in ("", "NULL")
                   for k, lab in zip(kp, labels)], dtype=bool)
    if not ok.any():
        return KpMakeup(np.array([]), np.array([]), [], "lay data")
    order = np.argsort(kp[ok], kind="stable")
    k = kp[ok][order]
    labs = [str(labels[i]).strip() for i in np.nonzero(ok)[0][order]]
    ranges, start = [], 0
    for i in range(1, len(k) + 1):
        if i == len(k) or labs[i] != labs[start]:
            lo = k[start] if start == 0 else 0.5 * (k[start - 1] + k[start])
            hi = k[i - 1] if i == len(k) else 0.5 * (k[i - 1] + k[i])
            ranges.append((lo, hi, labs[start]))
            start = i
    return KpMakeup.from_ranges(ranges, "lay data")


def lay_direction(records: "LayRecords") -> int:
    """+1 when touchdown KP increases with time, -1 when it decreases."""
    order = records.order()
    kp = records.kp[order]
    kp = kp[np.isfinite(kp)]
    if kp.size < 2:
        return 1
    step = np.nanmedian(np.diff(kp))
    if step == 0 or not np.isfinite(step):
        step = kp[-1] - kp[0]
    return -1 if step < 0 else 1


def suspended_length_m(records: "LayRecords") -> np.ndarray:
    """Cable length hanging between the sheave and touchdown, per record.

    From the layback when logged (straight chord, a slight underestimate),
    else from the catenary's vertical balance ``s = sqrt(T_top^2 - H^2) / w``
    (measured top tension, bottom tension, submerged weight), else the depth.
    """
    depth = np.abs(records.get("td_depth"))
    height = records.get("sheave_height") if records.has("sheave_height") else np.zeros(records.n)
    height = np.where(np.isfinite(height), height, 0.0)
    out = depth.copy()
    w = records.cable_array("w_water_npm")
    if records.has("top_tension", "bottom_tension"):
        top = records.get("top_tension") * 1000.0
        bottom = np.where(np.isfinite(records.get("bottom_tension")), records.get("bottom_tension"), 0.0) * 1000.0
        with np.errstate(invalid="ignore", divide="ignore"):
            catenary = np.sqrt(np.maximum(top ** 2 - bottom ** 2, 0.0)) / w
        use = np.isfinite(catenary) & (catenary >= depth)
        out = np.where(use, catenary, out)
    if records.has("layback"):
        layback = np.abs(records.get("layback"))
        chord = np.hypot(layback, depth + height)
        out = np.where(np.isfinite(chord), chord, out)
    return out


def apply_makeup(records: "LayRecords", makeup: Optional[KpMakeup],
                 resolve: Callable[[Optional[str]], Optional[CableProps]], samples: int = 16) -> None:
    """Assign each record's touchdown cable and the cable hanging above it.

    Touchdown labels come from ``makeup`` at the record's KP when one is
    given (the record's own cable type column filling any gaps), else from
    that column, whose runs then form the makeup. The cable at height ``s`` above
    touchdown lands later at ``KP + dir * s / (1 + slack)``; where the hanging
    length spans a type change it is sampled, the tension at each sample
    found from the measured top tension downward (``dT = w dz``, depth taken
    proportional to cable length: the straight cable of slack lay), and the
    governing cable type is the one with the highest top tension / NOTS.
    Sets ``records.labels`` / ``records.cable`` and the ``hang_*`` values.
    """
    n = records.n
    if makeup is None or makeup.empty:
        makeup = makeup_from_records(records.kp, records.labels) if records.has("cable_type") else None
    elif makeup is not None:
        # A chosen makeup names the cable everywhere (one naming scheme top and
        # bottom); the data's own column only fills KPs the makeup does not cover.
        at_kp = makeup.label_at(records.kp)
        records.labels = [lab if lab is not None else own
                          for lab, own in zip(at_kp, records.labels or [None] * n)]
    cache: Dict[Optional[str], Optional[CableProps]] = {}

    def props(label):
        key = None if label is None else str(label)
        if key not in cache:
            cache[key] = resolve(key)
        return cache[key]

    records.cable = [props(label) for label in records.labels]
    direction = lay_direction(records)
    length = suspended_length_m(records)
    slack = records.get("bottom_slack")
    slack = np.where(np.isfinite(slack) & (slack > 0), slack, 0.0)
    ship_kp = records.kp + direction * length / (1.0 + slack / 100.0) / 1000.0
    records.values["ship_kp_landing"] = ship_kp
    top = records.get("top_tension")
    w_td = records.cable_array("w_water_npm")
    hang_w = w_td.copy()
    hang_t = top.copy()
    hang_nots = records.cable_array("nots_kn")
    hang_ntts = records.cable_array("ntts_kn")
    hang_label = np.array(records.labels, dtype=object)
    transition = np.zeros(n, dtype=bool)
    if makeup is not None and not makeup.empty:
        ship_labels = makeup.label_at(ship_kp)
        cuts = makeup.boundaries()
        lo, hi = np.minimum(records.kp, ship_kp), np.maximum(records.kp, ship_kp)
        crosses = np.zeros(n, dtype=bool)
        for cut in cuts:
            crosses |= (lo < cut) & (hi > cut)
        transition = crosses | np.array([a is not None and b is not None and str(a) != str(b)
                                         for a, b in zip(records.labels, ship_labels)])
        transition &= np.isfinite(length) & np.isfinite(records.kp)
        depth = np.abs(records.get("td_depth"))
        height = records.get("sheave_height") if records.has("sheave_height") else np.zeros(n)
        frac = np.linspace(0.0, 1.0, samples)
        for i in np.nonzero(transition)[0]:
            kps = records.kp[i] + direction * frac * length[i] / (1.0 + slack[i] / 100.0) / 1000.0
            labels = makeup.label_at(kps)
            labels = [lab if lab is not None else records.labels[i] for lab in labels]
            p = [props(lab) for lab in labels]
            w = np.array([_pos(q.w_water_npm) if q else np.nan for q in p])
            w = np.where(np.isfinite(w), w, np.nanmean(w) if np.isfinite(w).any() else np.nan)
            hang_w[i] = float(np.nanmean(w))
            if not np.isfinite(top[i]) or not np.isfinite(depth[i]):
                continue
            w_air = _pos(p[-1].w_air_npm) if p[-1] else np.nan
            h = height[i] if np.isfinite(height[i]) else 0.0
            dz = depth[i] / (samples - 1)
            below_top = np.concatenate((np.cumsum((0.5 * (w[1:] + w[:-1]) * dz)[::-1])[::-1], [0.0]))
            t = top[i] - ((w_air * h if np.isfinite(w_air) else 0.0) + below_top) / 1000.0
            nots = np.array([_pos(q.nots_kn) if q else np.nan for q in p])
            ntts = np.array([_pos(q.ntts_kn) if q else np.nan for q in p])
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.where(np.isfinite(nots) & (nots > 0), t / nots, -np.inf)
            j = int(np.argmax(ratio)) if np.isfinite(ratio).any() else len(t) - 1
            # The sheave end carries the full measured tension.
            j = j if ratio[j] > -np.inf else len(t) - 1
            hang_t[i], hang_nots[i], hang_ntts[i], hang_label[i] = (
                top[i] if j == len(t) - 1 else t[j], nots[j], ntts[j], labels[j])
    records.values["hang_w_npm"] = hang_w
    records.values["hang_tension_kn"] = hang_t
    records.values["hang_nots_kn"] = hang_nots
    records.values["hang_ntts_kn"] = hang_ntts
    records.values["hang_transition"] = transition.astype(float)
    records.hang_labels = list(hang_label)


# ---------------------------------------------------------------------------
# Seabed model
# ---------------------------------------------------------------------------
def _upper_hull(x: np.ndarray, y: np.ndarray) -> List[int]:
    """Indices of the upper concave hull of points sorted by ``x``."""
    hull: List[int] = []
    for i in range(len(x)):
        while len(hull) >= 2:
            o, a = hull[-2], hull[-1]
            cross = (x[a] - x[o]) * (y[i] - y[o]) - (y[a] - y[o]) * (x[i] - x[o])
            if cross >= 0.0:
                hull.pop()
            else:
                break
        hull.append(i)
    return hull


def _runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """Inclusive ``(start, end)`` index runs where ``mask`` is True."""
    out: List[Tuple[int, int]] = []
    start = None
    for i, value in enumerate(mask):
        if value and start is None:
            start = i
        elif not value and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def cable_rest_elevation(x_m: np.ndarray, seabed_elev_m: np.ndarray, curvature: np.ndarray) -> np.ndarray:
    """Cable elevation resting on a seabed profile (small-slope obstacle problem).

    ``x_m`` ascending stations, ``seabed_elev_m`` seabed elevation (positive
    up; nan = no data), ``curvature`` the allowed free-span curvature
    ``w / H`` per station (1/m). Stations with a non-finite or infinite
    curvature (no bottom tension) conform to the seabed and pin the cable.
    Gaps in the seabed are not bridged (the cable is nan there).
    """
    x = np.asarray(x_m, dtype=float)
    s = np.asarray(seabed_elev_m, dtype=float)
    c = np.asarray(curvature, dtype=float)
    y = np.full(len(x), np.nan)
    valid = np.isfinite(x) & np.isfinite(s)
    for a, b in _runs(valid):
        y[a:b + 1] = s[a:b + 1]
        pinned = ~np.isfinite(c[a:b + 1]) | (c[a:b + 1] <= 0)
        anchors = [a] + [a + i for i in np.nonzero(pinned)[0] if 0 < i < b - a] + [b]
        anchors = sorted(set(anchors))
        for i0, i1 in zip(anchors[:-1], anchors[1:]):
            if i1 - i0 < 2:
                continue
            xs = x[i0:i1 + 1] - x[i0]
            cs = c[i0:i1 + 1].copy()
            cs[~np.isfinite(cs) | (cs <= 0)] = 0.0  # only the end anchors can be pinned here
            dx = np.diff(xs)
            slope = np.concatenate(([0.0], np.cumsum(0.5 * (cs[1:] + cs[:-1]) * dx)))
            phi = np.concatenate(([0.0], np.cumsum(0.5 * (slope[1:] + slope[:-1]) * dx)))
            g = s[i0:i1 + 1] - phi
            hull = _upper_hull(xs, g)
            u = np.interp(xs, xs[hull], g[hull])
            y[i0:i1 + 1] = np.maximum(u + phi, s[i0:i1 + 1])
    return y


def smooth_profile(x_m: np.ndarray, values: np.ndarray, length_m: float) -> np.ndarray:
    """Centred running mean over ``length_m`` within each run of finite values.

    Works on unevenly spaced stations (contour crossings) through the running
    integral; windows shrink at run ends and gaps are never bridged.
    """
    x = np.asarray(x_m, dtype=float)
    v = np.asarray(values, dtype=float)
    if not length_m or length_m <= 0:
        return v.copy()
    out = np.full(len(v), np.nan)
    half = 0.5 * float(length_m)
    for a, b in _runs(np.isfinite(x) & np.isfinite(v)):
        xs, vs = x[a:b + 1], v[a:b + 1]
        if b == a:
            out[a] = vs[0]
            continue
        integral = np.concatenate(([0.0], np.cumsum(0.5 * (vs[1:] + vs[:-1]) * np.diff(xs))))
        lo = np.clip(xs - half, xs[0], xs[-1])
        hi = np.clip(xs + half, xs[0], xs[-1])
        width = hi - lo
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = (np.interp(hi, xs, integral) - np.interp(lo, xs, integral)) / width
        out[a:b + 1] = np.where(width > 0, mean, vs)
    return out


def conformity_length_m(ei_knm2: float, w_npm: float) -> float:
    """Shortest seabed feature a resting cable can follow: ``2 (EI / w)^(1/3)``.

    The bending length of a cable lying under its own weight; shorter
    features are bridged by bending stiffness (and are mostly sounding noise).
    """
    ei, w = _pos(ei_knm2), _pos(w_npm)
    if not (np.isfinite(ei) and np.isfinite(w)) or ei <= 0 or w <= 0:
        return math.nan
    return 2.0 * (ei * 1000.0 / w) ** (1.0 / 3.0)


def _binned(x_query, x_data, values, reduce: str) -> np.ndarray:
    xq = np.asarray(x_query, dtype=float)
    ok = np.isfinite(x_data) & np.isfinite(values)
    xd = np.asarray(x_data, dtype=float)[ok]
    vd = np.asarray(values, dtype=float)[ok]
    out = np.full(len(xq), np.nan)
    if xd.size == 0 or xq.size == 0:
        return out
    order = np.argsort(xd, kind="stable")
    xd, vd = xd[order], vd[order]
    edges = np.empty(len(xq) + 1)
    edges[1:-1] = 0.5 * (xq[1:] + xq[:-1])
    edges[0], edges[-1] = -np.inf, np.inf
    cell = np.searchsorted(edges, xd, side="right") - 1
    if reduce == "max":
        filled = np.full(len(xq), -np.inf)
        np.maximum.at(filled, cell, vd)
    else:
        filled = np.full(len(xq), np.inf)
        np.minimum.at(filled, cell, vd)
    has = np.isfinite(filled)
    out[has] = filled[has]
    if (~has).any():
        out[~has] = np.interp(xq[~has], xd, vd)
    return out


def binned_max(x_query: np.ndarray, x_data: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Per query station, the max of the data falling in its cell; linear
    interpolation where a cell holds no data. Inputs need not be sorted."""
    return _binned(x_query, x_data, values, "max")


def binned_min(x_query: np.ndarray, x_data: np.ndarray, values: np.ndarray) -> np.ndarray:
    """As :func:`binned_max`, taking the minimum."""
    return _binned(x_query, x_data, values, "min")


def laid_cable_cumulative(record_x_m, slack_pct, td_depth_m) -> Tuple[np.ndarray, np.ndarray]:
    """``(x, cumulative cable length)`` at the records, in chainage order.

    Each step lays ``(1 + slack / 100)`` times its length over the lay model's
    own seabed (touchdown depths): bottom slack is relative to that seabed.
    """
    rx = np.asarray(record_x_m, dtype=float)
    slack = np.asarray(slack_pct, dtype=float)
    depth = np.abs(np.asarray(td_depth_m, dtype=float))
    ok = np.isfinite(rx) & np.isfinite(slack) & np.isfinite(depth)
    order = np.argsort(rx[ok], kind="stable")
    rx, slack, depth = rx[ok][order], slack[ok][order], depth[ok][order]
    if rx.size < 2:
        return rx, np.zeros(rx.size)
    step = np.hypot(np.diff(rx), np.diff(depth)) * (1.0 + 0.5 * (slack[1:] + slack[:-1]) / 100.0)
    cumulative = np.concatenate(([0.0], np.cumsum(step)))
    keep = np.concatenate((np.diff(rx) > 0, [True]))  # one value per position for interpolation
    return rx[keep], cumulative[keep]


def seabed_cumulative(x_m, depth_m) -> Tuple[np.ndarray, np.ndarray]:
    """Cumulative 3D seabed length and covered plan length (gaps add nothing)."""
    x = np.asarray(x_m, dtype=float)
    d = np.asarray(depth_m, dtype=float)
    seg_ok = np.isfinite(d[1:]) & np.isfinite(d[:-1])
    dx = np.diff(x)
    length = np.where(seg_ok, np.hypot(dx, np.where(seg_ok, np.diff(d), 0.0)), 0.0)
    plan = np.where(seg_ok, dx, 0.0)
    return np.concatenate(([0.0], np.cumsum(length))), np.concatenate(([0.0], np.cumsum(plan)))


def length_balance_pct(x_m, seabed_cum, plan_cum, cable_x, cable_cum, half_window_m,
                       min_coverage: float = 0.8) -> np.ndarray:
    """Laid cable minus seabed length within ``x +- half_window``, % of plan.

    Positive: more cable than the seabed needs (surplus); negative: the cable
    is short of the seabed it has to cover. nan without enough data.
    """
    x = np.asarray(x_m, dtype=float)
    out = np.full(len(x), np.nan)
    if cable_x.size < 2 or x.size < 2:
        return out
    lo_limit = max(x[0], cable_x[0])
    hi_limit = min(x[-1], cable_x[-1])
    a = np.clip(x - half_window_m, lo_limit, hi_limit)
    b = np.clip(x + half_window_m, lo_limit, hi_limit)
    plan = b - a
    covered = np.interp(b, x, plan_cum) - np.interp(a, x, plan_cum)
    seabed = np.interp(b, x, seabed_cum) - np.interp(a, x, seabed_cum)
    cable = np.interp(b, cable_x, cable_cum) - np.interp(a, cable_x, cable_cum)
    with np.errstate(invalid="ignore", divide="ignore"):
        ok = (plan > 0) & (covered >= min_coverage * plan)
        balance = (cable - seabed * plan / covered) / plan * 100.0
    out[ok] = balance[ok]
    return out


def taut_tension_n(x_m, seabed_elev_m, w_npm, x0: float, x1: float, cable_x, cable_cum,
                   mu: float, min_reach_m: float, max_reach_m: float,
                   cap_n: float = 2.0e6) -> Tuple[float, float, float, bool]:
    """Tension that makes the cable's resting shape use exactly the cable laid.

    The shortfall between ``x0`` and ``x1`` draws on cable within the friction
    reach ``H / (mu w)`` either side, so the window grows with the tension.
    Bisection on ``H`` (the rest shape shortens as ``H`` grows). Returns
    ``(H, a, b, feasible)``: ``feasible`` is False when even taut at
    ``cap_n`` the cable cannot cover the seabed.
    """
    x = np.asarray(x_m, dtype=float)
    s = np.asarray(seabed_elev_m, dtype=float)
    w = np.asarray(w_npm, dtype=float)
    lo_x, hi_x = max(x[0], cable_x[0]), min(x[-1], cable_x[-1])

    def window(h):
        sel = (x >= x0) & (x <= x1)
        w_bar = float(np.nanmean(w[sel])) if np.isfinite(w[sel]).any() else float(np.nanmean(w))
        reach = float(np.clip(h / (mu * w_bar), min_reach_m, max_reach_m)) if mu > 0 and w_bar > 0 \
            else max_reach_m
        return max(x0 - reach, lo_x), min(x1 + reach, hi_x)

    def excess(h):
        a, b = window(h)
        sel = (x >= a) & (x <= b)
        xs, ss, ws = x[sel], s[sel], w[sel]
        if xs.size < 3:
            return -1.0
        y = cable_rest_elevation(xs, ss, np.where(np.isfinite(ws), ws, np.nanmean(ws)) / h)
        seg = np.isfinite(y[1:]) & np.isfinite(y[:-1])
        shape = float(np.sum(np.hypot(np.diff(xs), np.diff(y))[seg]))
        covered = float(np.sum(np.diff(xs)[seg]))
        span = xs[-1] - xs[0]
        laid = float(np.interp(b, cable_x, cable_cum) - np.interp(a, cable_x, cable_cum))
        laid *= covered / span if span > 0 else 1.0
        return shape - laid

    h_lo, h_hi = 10.0, float(cap_n)
    if excess(h_lo) <= 0:
        a, b = window(0.0)
        return 0.0, a, b, True
    if excess(h_hi) > 0:
        a, b = window(h_hi)
        return h_hi, a, b, False
    for _ in range(28):
        mid = math.sqrt(h_lo * h_hi)
        if excess(mid) > 0:
            h_lo = mid
        else:
            h_hi = mid
        if h_hi / h_lo < 1.01:
            break
    a, b = window(h_hi)
    return h_hi, a, b, True


@dataclass
class Span:
    i0: int
    i1: int
    kp_start: float
    kp_end: float
    length_m: float
    max_gap_m: float
    kp_max_gap: float
    tension_kn: float
    source: str = "lay model"


@dataclass
class Shortfall:
    """A stretch where the laid cable is shorter than the seabed it covers."""

    kp_start: float
    kp_end: float
    balance_pct: float        # most negative cable-vs-seabed balance (%)
    tension_kn: float         # tension that makes the cable cover it
    reach_m: float            # friction reach either side at that tension
    feasible: bool
    npts_kn: float = math.nan


def find_spans(x_m: np.ndarray, kp_km: np.ndarray, gap_m: np.ndarray, tension_kn: np.ndarray,
               min_gap_m: float = 0.3, min_length_m: float = 5.0,
               source: Optional[np.ndarray] = None) -> List[Span]:
    """Contiguous runs where the cable clears the seabed by more than ``min_gap_m``."""
    gap = np.asarray(gap_m, dtype=float)
    out: List[Span] = []
    with np.errstate(invalid="ignore"):
        mask = np.isfinite(gap) & (gap > min_gap_m)
    for a, b in _runs(mask):
        # Widen to the touch-down / lift-off stations either side.
        a0 = max(a - 1, 0)
        b0 = min(b + 1, len(gap) - 1)
        length = float(x_m[b0] - x_m[a0])
        if length < min_length_m:
            continue
        local = gap[a:b + 1]
        k = a + int(np.nanargmax(local))
        tension = tension_kn[a:b + 1]
        origin = "lay model"
        if source is not None and np.any(source[a:b + 1] == 2):
            origin = "cable short of the seabed"
        out.append(Span(a0, b0, float(kp_km[a0]), float(kp_km[b0]), length, float(gap[k]), float(kp_km[k]),
                        float(np.nanmax(tension)) if np.isfinite(tension).any() else math.nan, origin))
    return out


@dataclass
class SeabedModel:
    """Profile stations with the modelled cable (arrays per station)."""

    x_m: np.ndarray               # chainage along the touchdown track
    kp_km: np.ndarray
    seabed_depth_m: np.ndarray    # smoothed seabed the cable rests on, positive down
    cable_depth_m: np.ndarray     # positive down
    tension_kn: np.ndarray        # tension the cable rests at
    w_npm: np.ndarray
    raw_depth_m: Optional[np.ndarray] = None
    logged_tension_kn: Optional[np.ndarray] = None
    balance_pct: Optional[np.ndarray] = None
    reach_m: Optional[np.ndarray] = None
    smoothing_m: float = 0.0
    mu: float = math.nan
    spans: List[Span] = field(default_factory=list)
    shortfalls: List[Shortfall] = field(default_factory=list)

    @property
    def gap_m(self) -> np.ndarray:
        return self.seabed_depth_m - self.cable_depth_m


def auto_smoothing_m(records: "LayRecords", spacing_m: float) -> float:
    """Conformity length of the most flexible cable present (min over types);
    10 m when no bending stiffness is known. Never below 3 stations."""
    lengths = [conformity_length_m(p.bending_stiffness_knm2, p.w_water_npm)
               for p in {id(p): p for p in records.cable if p is not None}.values()]
    lengths = [v for v in lengths if np.isfinite(v)]
    value = min(lengths) if lengths else 10.0
    return max(value, 3.0 * spacing_m)


def model_seabed(x_m, kp_km, seabed_depth_m, records: "LayRecords", record_x_m: np.ndarray,
                 zero_tension_kn: float = 0.05, min_gap_m: float = 0.3, min_length_m: float = 5.0,
                 smoothing_m: Optional[float] = 0.0, friction: Optional[Dict[str, float]] = None) -> SeabedModel:
    """Rest the cable on the seabed (see the module docstring).

    * The seabed is smoothed over ``smoothing_m`` (None = the cable's
      conformity length, :func:`auto_smoothing_m`).
    * Each station rests at the highest logged bottom tension of the records
      depositing cable in it, with the lightest cable there (both
      conservative: more spans).
    * With ``friction`` (``mu``, ``max_reach_m``, ``shortfall_pct``) and
      bottom slack + touchdown depth logged, the laid cable is balanced
      against the seabed within the friction reach ``H / (mu w)``; where it
      is short, the tension that makes it cover the seabed
      (:func:`taut_tension_n`) is applied over that reach if higher.
    """
    x = np.asarray(x_m, dtype=float)
    raw = np.asarray(seabed_depth_m, dtype=float)
    rx = np.asarray(record_x_m, dtype=float)
    spacing = float(np.nanmedian(np.diff(x))) if len(x) > 1 else 1.0
    step = float(np.nanmax(np.diff(x))) if len(x) > 1 else 0.0
    with np.errstate(invalid="ignore"):
        # Records beyond the profile (outside a KP window) must not pile into its end stations.
        rx = np.where((rx >= x[0] - step) & (rx <= x[-1] + step), rx, np.nan)
    logged = binned_max(x, rx, records.get("bottom_tension"))
    w = binned_min(x, rx, records.cable_array("w_water_npm"))
    npts = binned_min(x, rx, records.cable_array("npts_kn"))
    if smoothing_m is None:
        smoothing_m = auto_smoothing_m(records, spacing)
    depth = smooth_profile(x, raw, smoothing_m)
    tension = np.where(np.isfinite(logged), logged, 0.0)
    source = np.where(tension > zero_tension_kn, 1, 0)
    model = SeabedModel(x, np.asarray(kp_km, dtype=float), depth, depth.copy(), tension, w,
                        raw_depth_m=raw, logged_tension_kn=logged, smoothing_m=float(smoothing_m or 0.0))
    if friction and records.has("bottom_slack", "td_depth") and np.isfinite(w).any():
        mu = float(friction.get("mu", 0.5))
        max_reach = float(friction.get("max_reach_m", 1000.0))
        tolerance = float(friction.get("shortfall_pct", 0.5))
        min_reach = max(float(smoothing_m or 0.0), 3.0 * spacing)
        cable_x, cable_cum = laid_cable_cumulative(rx, records.get("bottom_slack"), records.get("td_depth"))
        seabed_cum, plan_cum = seabed_cumulative(x, depth)
        w_fill = np.where(np.isfinite(w), w, np.nanmean(w))
        with np.errstate(invalid="ignore", divide="ignore"):
            reach = np.clip(tension * 1000.0 / (mu * w_fill), min_reach, max_reach) if mu > 0 \
                else np.full(len(x), max_reach)
        balance = length_balance_pct(x, seabed_cum, plan_cum, cable_x, cable_cum, reach)
        model.balance_pct, model.reach_m, model.mu = balance, reach, mu
        with np.errstate(invalid="ignore"):
            short = np.isfinite(balance) & (balance < -tolerance)
        window_end = -math.inf
        runs = []
        for a, b in _runs(short):
            # A long short stretch needs a local tension along it, not one average:
            # solve it in pieces no longer than twice the friction reach.
            start = a
            while start <= b:
                stop = int(np.searchsorted(x, x[start] + 2.0 * max_reach, side="right")) - 1
                stop = min(max(stop, start), b)
                runs.append((start, stop))
                start = stop + 1
        for a, b in runs:
            h, wa, wb, feasible = taut_tension_n(x, -depth, w_fill, x[a], x[b], cable_x, cable_cum,
                                                 mu, min_reach, max_reach)
            if h / 1000.0 <= zero_tension_kn:
                continue  # friction reach brings enough cable (or barely any tension): it conforms
            sel = (x >= wa) & (x <= wb)
            h_kn = h / 1000.0
            raise_ = sel & (h_kn > tension)
            tension = np.where(raise_, h_kn, tension)
            source = np.where(raise_, 2, source)
            item = Shortfall(
                float(model.kp_km[a]), float(model.kp_km[b]), float(np.nanmin(balance[a:b + 1])), h_kn,
                float(h / (mu * float(np.nanmean(w_fill[a:b + 1])))) if mu > 0 else max_reach, feasible,
                float(np.nanmin(npts[a:b + 1])) if np.isfinite(npts[a:b + 1]).any() else math.nan)
            last = model.shortfalls[-1] if model.shortfalls else None
            if last is not None and x[a] <= window_end:
                # Overlapping friction windows: one taut stretch.
                last.kp_end = max(last.kp_end, item.kp_end)
                last.balance_pct = min(last.balance_pct, item.balance_pct)
                last.feasible = last.feasible and item.feasible
                if item.tension_kn > last.tension_kn:
                    last.tension_kn, last.reach_m = item.tension_kn, item.reach_m
                last.npts_kn = float(np.nanmin([last.npts_kn, item.npts_kn])) \
                    if np.isfinite([last.npts_kn, item.npts_kn]).any() else math.nan
            else:
                model.shortfalls.append(item)
            window_end = max(window_end, wb)
    with np.errstate(divide="ignore", invalid="ignore"):
        curvature = np.where((tension > zero_tension_kn) & np.isfinite(w) & (w > 0),
                             w / (tension * 1000.0), np.inf)
    model.cable_depth_m = -cable_rest_elevation(x, -depth, curvature)
    model.tension_kn = tension
    model.spans = find_spans(x, model.kp_km, model.gap_m, tension, min_gap_m, min_length_m, source)
    return model


def seabed_span_findings(model: SeabedModel, params) -> List[RangeFinding]:
    red_gap = float(params.get("red_gap_m", 1.0))
    red_length = float(params.get("red_length_m", 50.0))
    out = []
    for span in model.spans:
        red = span.max_gap_m >= red_gap or span.length_m >= red_length
        out.append(RangeFinding(
            check_id="suspension", severity=Severity.ERROR if red else Severity.WARNING,
            kp_start=min(span.kp_start, span.kp_end), kp_end=max(span.kp_start, span.kp_end),
            message=(f"Modelled suspension {span.length_m:.0f} m long, up to {span.max_gap_m:.1f} m "
                     f"above the seabed at {_fmt(span.tension_kn, 'kN', 2)} ({span.source})"),
            value=span.max_gap_m, threshold=red_gap, unit="m",
            extra={"length_m": span.length_m, "kp_max_gap": span.kp_max_gap}))
    return out


def length_findings(model: SeabedModel, params, zero_tension_kn: float = 0.05) -> List[RangeFinding]:
    """Shortfalls (cable pulled taut) and surpluses (loop risk) against the seabed."""
    out = []
    for item in model.shortfalls:
        over_npts = np.isfinite(item.npts_kn) and item.tension_kn > item.npts_kn
        severity = Severity.ERROR if (not item.feasible or over_npts) else Severity.WARNING
        if not item.feasible:
            detail = "the cable cannot cover it even when taut: check the data"
        else:
            detail = (f"pulled taut to about {item.tension_kn:.2f} kN, drawing on cable within "
                      f"{item.reach_m:.0f} m by friction" + (" (above NPTS)" if over_npts else ""))
        out.append(RangeFinding(
            check_id="length_balance", severity=severity, kp_start=item.kp_start, kp_end=item.kp_end,
            message=f"Laid cable {-item.balance_pct:.1f}% short of the seabed: {detail}",
            value=item.balance_pct, threshold=-float(params.get("shortfall_pct", 0.5)), unit="%",
            extra={"tension_kn": item.tension_kn, "reach_m": item.reach_m}))
    excess = float(params.get("excess_pct", 8.0) or 0.0)
    if excess > 0 and model.balance_pct is not None:
        with np.errstate(invalid="ignore"):
            surplus = np.isfinite(model.balance_pct) & (model.balance_pct >= excess) \
                & (model.tension_kn <= zero_tension_kn)
        for a, b in _runs(surplus):
            peak = float(np.nanmax(model.balance_pct[a:b + 1]))
            out.append(RangeFinding(
                check_id="length_balance", severity=Severity.WARNING,
                kp_start=float(model.kp_km[a]), kp_end=float(model.kp_km[b]),
                message=f"{peak:.1f}% more cable than the seabed needs at zero tension: loops / snaking likely",
                value=peak, threshold=excess, unit="%"))
    return out


SEABED_CHECKS: Tuple[CheckDef, ...] = (
    CheckDef("suspension", "Suspensions (seabed model)",
             "Rests the cable on the seabed at the logged bottom tension (raised where the laid cable "
             "is short of the seabed, when the length check runs) with the cable's submerged weight; "
             "flags where it spans clear of the seabed.",
             (ParamSpec("min_gap_m", "Report spans higher than (m)", "float", 0.3, minimum=0.0),
              ParamSpec("min_length_m", "...and longer than (m)", "float", 5.0, minimum=0.0),
              ParamSpec("red_gap_m", "Red when higher than (m)", "float", 1.0, minimum=0.0),
              ParamSpec("red_length_m", "...or longer than (m)", "float", 50.0, minimum=0.0),
              ParamSpec("zero_tension_kn", "Cable conforms below tension (kN)", "float", 0.05, minimum=0.0)),
             ("bottom_tension",), ("weight_water_kg_m",), needs_seabed=True),
    CheckDef("length_balance", "Cable length vs seabed (friction)",
             "Laid cable (bottom slack over the lay model's seabed) against the sampled seabed within the "
             "distance friction lets the cable slide, H / (mu w). Short: the cable is pulled taut and the "
             "tension that makes it cover the seabed feeds the suspension model. Surplus at zero tension: "
             "loop risk.",
             (ParamSpec("mu", "Axial seabed friction coefficient", "float", 0.5, minimum=0.01),
              ParamSpec("max_reach_m", "Longest friction reach (m)", "float", 1000.0, minimum=10.0),
              ParamSpec("shortfall_pct", "Flag shortfall above (%)", "float", 0.5, minimum=0.0),
              ParamSpec("excess_pct", "Flag surplus above (%, 0 = off)", "float", 8.0, minimum=0.0)),
             ("bottom_slack", "td_depth"), ("weight_water_kg_m",), needs_seabed=True),
)


def all_checks() -> List[CheckDef]:
    return [check for check, _fn in RECORD_CHECKS] + list(SEABED_CHECKS)


def check_by_id(check_id: str) -> Optional[CheckDef]:
    return next((c for c in all_checks() if c.check_id == check_id), None)


def missing_inputs(check: CheckDef, records: LayRecords) -> List[str]:
    """Human-readable reasons a check cannot run (empty when it can)."""
    missing = [ROLE_BY_KEY[key].label for key in check.needs if not records.has(key)]
    if check.check_id == "td_reversal" and records.time is None:
        missing.append("time")
    if check.check_id == "tension_consistency" and not records.has("top_tension_calc"):
        if not records.has("bottom_tension", "td_depth"):
            missing.append("calculated top tension, or bottom tension + depth")
        elif not np.isfinite(records.cable_array("w_water_npm")).any():
            missing.append("cable weight in water (cable library)")
    for attribute in check.needs_cable:
        hanging = {"nots_kn": "hang_nots_kn", "ntts_kn": "hang_ntts_kn"}.get(attribute)
        values = records.get(hanging) if hanging in records.values else records.cable_array(attribute)
        if not np.isfinite(values).any():
            missing.append(f"cable {_CABLE_LABELS.get(attribute, attribute)} (cable library)")
    return missing


_CABLE_LABELS = {"weight_water_kg_m": "weight in water", "npts_kn": "NPTS", "nots_kn": "NOTS",
                 "ntts_kn": "NTTS", "cbl_kn": "CBL"}


def run_record_checks(records: LayRecords, enabled: Dict[str, Dict[str, object]]) -> Tuple[List[RangeFinding], Dict[str, str]]:
    """Run the enabled record checks. Returns ``(findings, skipped)`` where
    ``skipped`` maps check id -> reason."""
    findings: List[RangeFinding] = []
    skipped: Dict[str, str] = {}
    for check, fn in RECORD_CHECKS:
        if check.check_id not in enabled:
            continue
        missing = missing_inputs(check, records)
        if missing:
            skipped[check.check_id] = "needs " + ", ".join(missing)
            continue
        params = default_params(check)
        params.update(enabled[check.check_id] or {})
        findings.extend(fn(records, params))
    return findings, skipped


# ---------------------------------------------------------------------------
# Track: chainage along the touchdown positions and KP mapping
# ---------------------------------------------------------------------------
def track_vertices(records: LayRecords, spacing_m: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(lon, lat, kp_km)`` of a touchdown track ordered by KP.

    Records are binned every ``spacing_m`` of KP and each bin's median
    position kept, so stops and back-and-forth jitter do not zig-zag the
    track. Raises ValueError without positions.
    """
    if records.lat is None or records.lon is None:
        raise ValueError("The layer has no touchdown positions.")
    ok = np.isfinite(records.kp) & np.isfinite(records.lat) & np.isfinite(records.lon)
    if ok.sum() < 2:
        raise ValueError("Fewer than two records with a KP and a position.")
    kp = records.kp[ok]
    lat = records.lat[ok]
    lon = records.lon[ok]
    key = np.floor(kp * 1000.0 / max(spacing_m, 0.01)).astype(np.int64)
    order = np.argsort(key, kind="stable")
    key, kp, lat, lon = key[order], kp[order], lat[order], lon[order]
    starts = np.concatenate(([0], np.nonzero(np.diff(key))[0] + 1, [len(key)]))
    out_lon, out_lat, out_kp = [], [], []
    for a, b in zip(starts[:-1], starts[1:]):
        out_lon.append(float(np.median(lon[a:b])))
        out_lat.append(float(np.median(lat[a:b])))
        out_kp.append(float(np.median(kp[a:b])))
    return np.asarray(out_lon), np.asarray(out_lat), np.asarray(out_kp)


def chainage_to_kp(vertex_chainage_m: np.ndarray, vertex_kp_km: np.ndarray, chainage_m) -> np.ndarray:
    return np.interp(chainage_m, vertex_chainage_m, vertex_kp_km)


def kp_to_chainage(vertex_chainage_m: np.ndarray, vertex_kp_km: np.ndarray, kp_km) -> np.ndarray:
    """Inverse of :func:`chainage_to_kp` (vertex KPs are ascending)."""
    return np.interp(kp_km, vertex_kp_km, vertex_chainage_m)


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------
def status_bins(kp_min: float, kp_max: float, findings: Sequence[RangeFinding], record_kp: np.ndarray,
                bin_m: float = 10.0) -> Tuple[np.ndarray, np.ndarray]:
    """``(bin_edges_km, level)`` for the coloured status bar.

    ``level`` per bin: -1 no data, 0 assessed and clear, else the highest
    finding level (1 info, 2 warning, 3 error) touching the bin.
    """
    if not (np.isfinite(kp_min) and np.isfinite(kp_max)) or kp_max <= kp_min:
        return np.array([]), np.array([], dtype=int)
    step = max(bin_m, 0.1) / 1000.0
    count = int(min(math.ceil((kp_max - kp_min) / step), 200000))
    edges = kp_min + np.arange(count + 1) * step
    level = np.full(count, -1, dtype=int)
    kp = np.asarray(record_kp, dtype=float)
    kp = kp[np.isfinite(kp)]
    if kp.size:
        cells = np.clip(((kp - kp_min) / step).astype(int), 0, count - 1)
        level[np.unique(cells)] = 0
        # Bins between consecutive records closer than 5 bins count as covered.
        sorted_cells = np.unique(cells)
        for a, b in zip(sorted_cells[:-1], sorted_cells[1:]):
            if 1 < b - a <= 5:
                level[a:b] = np.maximum(level[a:b], 0)
    for finding in findings:
        lo = int(np.clip(math.floor((finding.kp_start - kp_min) / step), 0, count - 1))
        hi = int(np.clip(math.floor((finding.kp_end - kp_min) / step), 0, count - 1))
        level[lo:hi + 1] = np.maximum(level[lo:hi + 1], SEVERITY_LEVEL.get(finding.severity, 2))
    return edges, level
