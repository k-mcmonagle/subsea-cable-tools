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
    cables: List[Optional[CableProps]] = []
    if cable_for is not None:
        type_field = mapping.get("cable_type")
        cache: Dict[Optional[str], Optional[CableProps]] = {}
        types = dataset.raw(type_field)[rows] if type_field and dataset.has_field(type_field) else [None] * len(rows)
        for value in types:
            text = None if value is None else (str(value).strip() or None)
            if text == "NULL":
                text = None
            if text not in cache:
                cache[text] = cable_for(text)
            cables.append(cache[text])
    else:
        cables = [None] * len(rows)
    return LayRecords(rows=rows, kp=kp[rows].astype(float), time=time, values=values,
                      mapped=tuple(mapped), lat=lat, lon=lon, cable=cables, excluded=int(n - len(rows)))


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


def check_top_tension(records: LayRecords, params) -> List[RangeFinding]:
    top = records.get("top_tension")
    nots = records.cable_array("nots_kn")
    ntts = records.cable_array("ntts_kn")
    level = np.zeros(records.n, dtype=int)
    with np.errstate(invalid="ignore"):
        level[(top > nots) & np.isfinite(nots)] = 2
        level[(top > ntts) & np.isfinite(ntts)] = 3
    kp = records.get("ship_kp") if records.has("ship_kp") else records.kp

    def describe(lvl, value):
        limit = "NTTS" if lvl == 3 else "NOTS"
        return f"Measured top tension {_fmt(value, 'kN', 1)} above {limit}"
    return group_ranges(records, level, top, "top_tension", describe, "max",
                        lambda i: float(ntts[i] if level[i] == 3 else nots[i]), "kN", kp=kp,
                        merge_m=params.get("merge_m", 20.0))


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

    The steady-lay identity (Zajac 1957; exact without tangential drag): the
    tension change between touchdown and the sheave equals the cable's
    submerged weight times the water depth, plus its in-air weight times the
    sheave height when that is known.
    """
    top = records.get("top_tension")
    depth = records.get("td_depth")
    w_water = records.cable_array("w_water_npm")
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


def find_spans(x_m: np.ndarray, kp_km: np.ndarray, gap_m: np.ndarray, tension_kn: np.ndarray,
               min_gap_m: float = 0.3, min_length_m: float = 5.0) -> List[Span]:
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
        out.append(Span(a0, b0, float(kp_km[a0]), float(kp_km[b0]), length, float(gap[k]), float(kp_km[k]),
                        float(np.nanmax(tension_kn[a:b + 1])) if np.isfinite(tension_kn[a:b + 1]).any() else math.nan))
    return out


def binned_max(x_query: np.ndarray, x_data: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Per query station, the max of the data falling in its cell; linear
    interpolation where a cell holds no data. Inputs need not be sorted."""
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
    filled = np.full(len(xq), -np.inf)
    np.maximum.at(filled, cell, vd)
    has = np.isfinite(filled)
    out[has] = filled[has]
    if (~has).any():
        out[~has] = np.interp(xq[~has], xd, vd)
    return out


@dataclass
class SeabedModel:
    """Profile stations with the modelled cable (all arrays per station)."""

    x_m: np.ndarray          # chainage along the touchdown track
    kp_km: np.ndarray
    seabed_depth_m: np.ndarray   # positive down
    cable_depth_m: np.ndarray    # positive down
    tension_kn: np.ndarray
    w_npm: np.ndarray
    spans: List[Span] = field(default_factory=list)

    @property
    def gap_m(self) -> np.ndarray:
        return self.seabed_depth_m - self.cable_depth_m


def model_seabed(x_m, kp_km, seabed_depth_m, records: LayRecords, record_x_m: np.ndarray,
                 zero_tension_kn: float = 0.05, min_gap_m: float = 0.3,
                 min_length_m: float = 5.0) -> SeabedModel:
    """Rest the cable on the sampled seabed with the logged bottom tension.

    ``record_x_m`` places each record on the profile chainage. Each station
    takes the highest bottom tension of the records depositing cable within
    it (conservative: more tension, more spans).
    """
    x = np.asarray(x_m, dtype=float)
    depth = np.asarray(seabed_depth_m, dtype=float)
    # Records beyond the profile (outside a KP window) must not pile into its end stations.
    rx = np.asarray(record_x_m, dtype=float)
    step = float(np.nanmax(np.diff(x))) if len(x) > 1 else 0.0
    with np.errstate(invalid="ignore"):
        rx = np.where((rx >= x[0] - step) & (rx <= x[-1] + step), rx, np.nan)
    tension = binned_max(x, rx, records.get("bottom_tension"))
    w = binned_max(x, rx, records.cable_array("w_water_npm"))
    with np.errstate(divide="ignore", invalid="ignore"):
        curvature = np.where((tension > zero_tension_kn) & np.isfinite(w) & (w > 0),
                             w / (tension * 1000.0), np.inf)
    cable = -cable_rest_elevation(x, -depth, curvature)
    model = SeabedModel(x, np.asarray(kp_km, dtype=float), depth, cable, tension, w)
    model.spans = find_spans(x, model.kp_km, model.gap_m, tension, min_gap_m, min_length_m)
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
                     f"above the seabed (bottom tension {_fmt(span.tension_kn, 'kN', 2)})"),
            value=span.max_gap_m, threshold=red_gap, unit="m",
            extra={"length_m": span.length_m, "kp_max_gap": span.kp_max_gap}))
    return out


def terrain_shortfall(model: SeabedModel, records: LayRecords, record_x_m: np.ndarray,
                      window_m: float = 200.0, min_coverage: float = 0.8):
    """Laid cable length against seabed length per window along the track.

    Cable laid per record step is ``(1 + slack/100)`` times the step's 3D
    length over the lay model's own seabed (touchdown depths). The seabed
    length comes from the sampled bathymetry, so a positive shortfall means
    the finer seabed needs more cable than was laid: the cable bridges.
    Returns ``[(x0, x1, cable_m, seabed_m, shortfall_pct)]``.
    """
    order = np.argsort(record_x_m, kind="stable")
    rx = np.asarray(record_x_m, dtype=float)[order]
    slack = records.get("bottom_slack")[order]
    depth = np.abs(records.get("td_depth")[order])
    ok = np.isfinite(rx) & np.isfinite(slack) & np.isfinite(depth)
    rx, slack, depth = rx[ok], slack[ok], depth[ok]
    x = model.x_m
    sd = model.seabed_depth_m
    if rx.size < 2 or x.size < 2:
        return []
    start = max(rx[0], x[0])
    end = min(rx[-1], x[-1])
    out = []
    w0 = start
    step_mid = 0.5 * (rx[1:] + rx[:-1])
    step_len = np.hypot(np.diff(rx), np.diff(depth)) * (1.0 + 0.5 * (slack[1:] + slack[:-1]) / 100.0)
    seg_ok = np.isfinite(sd[1:]) & np.isfinite(sd[:-1])
    seg_mid = 0.5 * (x[1:] + x[:-1])
    seg_len = np.hypot(np.diff(x), np.diff(sd))
    seg_plan = np.diff(x)
    while w0 < end - 1e-6:
        w1 = min(w0 + window_m, end)
        sel = (step_mid >= w0) & (step_mid < w1)
        seg = (seg_mid >= w0) & (seg_mid < w1)
        covered = float(np.sum(seg_plan[seg & seg_ok]))
        plan = w1 - w0
        if plan > 0 and covered >= min_coverage * plan and sel.any():
            cable = float(np.sum(step_len[sel]))
            cable_plan = float(np.sum(np.diff(rx)[sel]))
            seabed = float(np.sum(seg_len[seg & seg_ok])) * (cable_plan / covered if covered > 0 else 1.0)
            shortfall = (seabed - cable) / cable_plan * 100.0 if cable_plan > 0 else math.nan
            out.append((w0, w1, cable, seabed, shortfall))
        w0 = w1
    return out


def terrain_findings(model: SeabedModel, windows, params) -> List[RangeFinding]:
    tolerance = float(params.get("shortfall_pct", 0.5))
    out = []
    for x0, x1, cable, seabed, shortfall in windows:
        if not np.isfinite(shortfall) or shortfall <= tolerance:
            continue
        k0, k1 = np.interp([x0, x1], model.x_m, model.kp_km)
        out.append(RangeFinding(
            check_id="terrain_slack", severity=Severity.WARNING,
            kp_start=float(min(k0, k1)), kp_end=float(max(k0, k1)),
            message=(f"Seabed needs {shortfall:.1f}% more cable than was laid "
                     f"({seabed:.0f} m seabed vs {cable:.0f} m cable): suspensions likely"),
            value=shortfall, threshold=tolerance, unit="%"))
    # Merge adjacent windows.
    merged: List[RangeFinding] = []
    for finding in sorted(out, key=lambda f: f.kp_start):
        if merged and finding.kp_start <= merged[-1].kp_end + 1e-9:
            last = merged[-1]
            last.kp_end = max(last.kp_end, finding.kp_end)
            if finding.value > last.value:
                last.value, last.message = finding.value, finding.message
        else:
            merged.append(finding)
    return merged


SEABED_CHECKS: Tuple[CheckDef, ...] = (
    CheckDef("suspension", "Suspensions (seabed model)",
             "Rests the cable on the sampled seabed using the logged bottom tension and the "
             "cable's submerged weight; flags where it spans clear of the seabed.",
             (ParamSpec("min_gap_m", "Report spans higher than (m)", "float", 0.3, minimum=0.0),
              ParamSpec("min_length_m", "...and longer than (m)", "float", 5.0, minimum=0.0),
              ParamSpec("red_gap_m", "Red when higher than (m)", "float", 1.0, minimum=0.0),
              ParamSpec("red_length_m", "...or longer than (m)", "float", 50.0, minimum=0.0),
              ParamSpec("zero_tension_kn", "Cable conforms below tension (kN)", "float", 0.05, minimum=0.0)),
             ("bottom_tension",), ("weight_water_kg_m",), needs_seabed=True),
    CheckDef("terrain_slack", "Slack vs seabed",
             "Cable laid (bottom slack over the lay model's seabed) against the length of the "
             "sampled seabed, per window: flags where the seabed needs more cable than was laid.",
             (ParamSpec("window_m", "Window (m)", "float", 200.0, minimum=10.0),
              ParamSpec("shortfall_pct", "Flag shortfall above (%)", "float", 0.5, minimum=0.0)),
             ("bottom_slack", "td_depth"), needs_seabed=True),
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
        if not np.isfinite(records.cable_array(attribute)).any():
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
