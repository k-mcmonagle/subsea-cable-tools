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
``H`` is the logged (model) bottom tension at deposit, raised where the
laid cable is short (below). The span shapes ignore bending stiffness, and
lateral movement is not modelled.

Friction makes everything local. Cable resting on the seabed only slides
where the tension changes by more than ``mu w`` per metre, so a tension ``H``
can draw cable from at most ``H / (mu w)`` either side. The laid cable is
balanced against the seabed within that reach; where it is short, the
tension that makes its rest shape use exactly the cable available is found
(:func:`taut_tension_n`) and the cable rests at the higher of that and the
logged tension. Lengths (seabed, and the lay model's seabed the slack is
relative to) use profiles smoothed over the cable's conformity length
``2 (EI / w)^(1/3)``, since sounding noise inflates length; the cable itself
rests on a lightly filtered seabed that keeps the crests holding it up.

At each end of a span the cable bears on the seabed: the reaction there,
and the bend radius once bending stiffness spreads the turn over
``sqrt(EI / H)``, are checked against the cable's MBR (:func:`span_supports`).
:func:`allowable_tension_kn` gives the highest tension that keeps spans
within the reporting limits along the route.

The cable hanging in the water can span a change of cable type; the
makeup along the route (:class:`KpMakeup`) gives the type at every height,
so tension limits and the weight in the top-tension identity use the cable
actually there (:func:`apply_makeup`).

The seabed mechanics are shared with *Span & Slack Analysis* and live in
:mod:`cable_seabed` (re-exported here).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .qc_base import ParamSpec, Severity

from ..cable_seabed.core import (  # noqa: F401 (re-exported for the Explorer and tests)
    G,
    LEVEL_SEVERITY,
    SEVERITY_LEVEL,
    Cancelled,
    RangeFinding,
    status_bins,
)
from ..cable_seabed.core import check_cancel as _check_cancel  # noqa: F401
from ..cable_seabed.core import pos as _pos  # noqa: F401
from ..cable_seabed.core import report as _report  # noqa: F401
from ..cable_seabed.core import runs as _runs  # noqa: F401
from ..cable_seabed.core import upper_hull as _upper_hull  # noqa: F401
from ..cable_seabed.shape import (  # noqa: F401
    Shortfall,
    cable_rest_elevation,
    conformity_length_m,
    length_balance_pct,
    pull_taut,
    seabed_cumulative,
    smooth_profile,
    taut_tension_n,
)
from ..cable_seabed.spans import (  # noqa: F401
    SPAN_END_TOL_M,
    Span,
    Support,
    allowable_tension_kn,
    find_spans,
    span_supports,
)
from ..cable_seabed.sampling import contour_interval_m  # noqa: F401

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
         help="Bottom (touchdown) tension. In lay software logs this is usually a model output."),
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
    _cable_lookup: Optional[tuple] = field(default=None, repr=False, compare=False)

    @property
    def n(self) -> int:
        return len(self.rows)

    def has(self, *keys: str) -> bool:
        return all(key in self.mapped for key in keys)

    def get(self, key: str) -> np.ndarray:
        return self.values.get(key, np.full(self.n, np.nan))

    def cable_array(self, attribute: str) -> np.ndarray:
        """Per-record cable property (nan where unknown)."""
        index, distinct = self._cable_index()
        values = np.array([_pos(getattr(props, attribute, None)) for props in distinct] + [math.nan])
        return values[index]

    def _cable_index(self) -> Tuple[np.ndarray, list]:
        """Per record, an index into the distinct cables (-1 = none); cached
        until ``cable`` is replaced."""
        cached = self._cable_lookup
        if cached is not None and cached[0] is self.cable and cached[1] == len(self.cable):
            return cached[2], cached[3]
        slots: Dict[int, int] = {}
        distinct: list = []
        index = np.full(self.n, -1, dtype=int)
        for i, props in enumerate(self.cable[:self.n]):
            if props is not None:
                slot = slots.get(id(props))
                if slot is None:
                    slot = slots[id(props)] = len(distinct)
                    distinct.append(props)
                index[i] = slot
        self._cable_lookup = (self.cable, len(self.cable), index, distinct)
        return index, distinct

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

    def threshold(i):
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
        tol = 1e-6
        last = len(self.starts) - 1
        index = np.searchsorted(self.starts, kp, side="right") - 1
        here = np.clip(index, 0, last)
        after = np.clip(index + 1, 0, last)
        finite = np.isfinite(kp)
        with np.errstate(invalid="ignore"):
            inside = finite & (index >= 0) & (kp <= self.ends[here] + tol)
            at_next = finite & ~inside & (index < last) & (np.abs(self.starts[after] - kp) <= tol)
        labels = np.array(self.labels, dtype=object)
        out[inside] = labels[here[inside]]
        out[at_next] = labels[after[at_next]]
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
            t[-1] = top[i]  # the sheave end carries the full measured tension
            nots = np.array([_pos(q.nots_kn) if q else np.nan for q in p])
            ntts = np.array([_pos(q.ntts_kn) if q else np.nan for q in p])
            with np.errstate(invalid="ignore", divide="ignore"):
                r_ots = np.where(np.isfinite(nots) & (nots > 0), t / nots, -np.inf)
                r_tts = np.where(np.isfinite(ntts) & (ntts > 0), t / ntts, -np.inf)
            # Governing height: the worst level first (above NTTS, then above NOTS),
            # then the highest ratio to that level's limit. Each limit is judged
            # on its own, so an NTTS breach is never hidden by another type's NOTS.
            level = np.where(r_tts > 1.0, 3, np.where(r_ots > 1.0, 2, 0))
            score = np.where(level == 3, r_tts, r_ots)
            j = int(np.lexsort((score, level))[-1])  # ties (no limits known): the sheave
            hang_t[i], hang_nots[i], hang_ntts[i], hang_label[i] = t[j], nots[j], ntts[j], labels[j]
    records.values["hang_w_npm"] = hang_w
    records.values["hang_tension_kn"] = hang_t
    records.values["hang_nots_kn"] = hang_nots
    records.values["hang_ntts_kn"] = hang_ntts
    records.values["hang_transition"] = transition.astype(float)
    records.hang_labels = list(hang_label)


# ---------------------------------------------------------------------------
# Seabed model
# ---------------------------------------------------------------------------
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


def _station_means(x_m: np.ndarray, record_x_m: np.ndarray, *values: np.ndarray) -> List[np.ndarray]:
    """Mean of each record array per station cell (nan where a cell holds no record)."""
    x = np.asarray(x_m, dtype=float)
    edges = 0.5 * (x[1:] + x[:-1])
    cell = np.searchsorted(edges, record_x_m, side="right")
    counts = np.bincount(cell, minlength=len(x)).astype(float)
    out = []
    for v in values:
        sums = np.bincount(cell, weights=v, minlength=len(x))
        with np.errstate(invalid="ignore", divide="ignore"):
            out.append(np.where(counts > 0, sums / np.maximum(counts, 1.0), np.nan))
    return out


def laid_cable_cumulative(x_m, record_x_m, slack_pct, td_depth_m,
                          smoothing_m: float = 0.0) -> Tuple[np.ndarray, np.ndarray]:
    """``(x, cumulative cable laid)`` on the profile stations within the records' extent.

    Each step lays ``(1 + slack / 100)`` times its length over the lay model's
    own seabed (touchdown depths), since bottom slack is relative to that
    seabed. The records are averaged per station and the lay model's seabed
    is smoothed like the sampled seabed: chords between neighbouring records
    would turn touchdown-depth noise into cable length (5 cm of noise at
    0.5 m record spacing adds 1 %), hiding a shortfall.
    """
    x = np.asarray(x_m, dtype=float)
    rx = np.asarray(record_x_m, dtype=float)
    slack = np.asarray(slack_pct, dtype=float)
    depth = np.abs(np.asarray(td_depth_m, dtype=float))
    ok = np.isfinite(rx) & np.isfinite(slack) & np.isfinite(depth)
    if ok.sum() < 2 or len(x) < 2:
        return np.array([]), np.array([])
    rx, slack, depth = rx[ok], slack[ok], depth[ok]
    slack_s, depth_s = _station_means(x, rx, slack, depth)
    has = np.isfinite(slack_s)
    inside = (x >= rx.min() - 1e-9) & (x <= rx.max() + 1e-9)
    inside |= has
    xs = x[inside]
    if xs.size < 2:
        return np.array([]), np.array([])
    # Stations between records (sparser records than stations) take the neighbours' values.
    slack_s = np.interp(xs, x[has], slack_s[has])
    depth_s = smooth_profile(xs, np.interp(xs, x[has], depth_s[has]), smoothing_m)
    step = np.hypot(np.diff(xs), np.diff(depth_s)) * (1.0 + 0.5 * (slack_s[1:] + slack_s[:-1]) / 100.0)
    return xs, np.concatenate(([0.0], np.cumsum(step)))


AUTO_FILTER_M = 2.0  # default seabed noise filter the cable rests on (m): clean for ~10 cm noise


@dataclass
class SeabedModel:
    """Profile stations with the modelled cable (arrays per station)."""

    x_m: np.ndarray               # chainage along the touchdown track
    kp_km: np.ndarray
    seabed_depth_m: np.ndarray    # filtered seabed the cable rests on, positive down
    cable_depth_m: np.ndarray     # positive down
    tension_kn: np.ndarray        # tension the cable rests at
    w_npm: np.ndarray
    raw_depth_m: Optional[np.ndarray] = None       # as sampled
    length_depth_m: Optional[np.ndarray] = None    # smoothed over the conformity length (seabed length)
    logged_tension_kn: Optional[np.ndarray] = None
    balance_pct: Optional[np.ndarray] = None
    reach_m: Optional[np.ndarray] = None
    allowable_kn: Optional[np.ndarray] = None
    smoothing_m: float = 0.0
    filter_m: float = 0.0
    mu: float = math.nan
    spans: List[Span] = field(default_factory=list)
    supports: List[Support] = field(default_factory=list)
    shortfalls: List[Shortfall] = field(default_factory=list)

    @property
    def gap_m(self) -> np.ndarray:
        return self.seabed_depth_m - self.cable_depth_m


def auto_smoothing_m(records: "LayRecords", spacing_m: float) -> float:
    """Conformity length of the most flexible cable present (min over types);
    10 m when no bending stiffness is known. Never below 3 stations."""
    _index, distinct = records._cable_index()
    lengths = [conformity_length_m(p.bending_stiffness_knm2, p.w_water_npm) for p in distinct]
    lengths = [v for v in lengths if np.isfinite(v)]
    value = min(lengths) if lengths else 10.0
    return max(value, 3.0 * spacing_m)


def model_seabed(x_m, kp_km, seabed_depth_m, records: "LayRecords", record_x_m: np.ndarray,
                 zero_tension_kn: float = 0.05, min_gap_m: float = 0.3, min_length_m: float = 5.0,
                 smoothing_m: Optional[float] = 0.0, friction: Optional[Dict[str, float]] = None,
                 filter_m: Optional[float] = 0.0, allowable: bool = False,
                 progress: Optional[Callable] = None,
                 cancel: Optional[Callable[[], bool]] = None) -> SeabedModel:
    """Rest the cable on the seabed (see the module docstring).

    * Two seabeds come from the sampled one. The cable rests on it filtered
      over ``filter_m`` (None = :data:`AUTO_FILTER_M`), which removes
      sounding noise but keeps the crests that hold the cable up. Seabed
      *length* uses it smoothed over ``smoothing_m`` (None = the cable's
      conformity length, :func:`auto_smoothing_m`), since noise adds length.
    * Each station rests at the highest logged bottom tension of the records
      depositing cable in it, with the lightest cable there (both
      conservative: more spans).
    * With ``friction`` (``mu``, ``max_reach_m``, ``shortfall_pct``) and
      bottom slack + touchdown depth logged, the laid cable is balanced
      against the seabed within the friction reach ``H / (mu w)``; where it
      is short, the tension that makes it cover the seabed
      (:func:`taut_tension_n`) is applied over that reach if higher.
    * Each span's supports get a seabed reaction and bend radius
      (:func:`span_supports`); with ``allowable`` the highest tension that
      keeps spans within the reporting limits is found along the route.

    ``progress(fraction, text)`` reports 0..1; ``cancel()`` is polled and
    raises :class:`Cancelled`.
    """
    x = np.asarray(x_m, dtype=float)
    raw = np.asarray(seabed_depth_m, dtype=float)
    rx = np.asarray(record_x_m, dtype=float)
    _report(progress, 0.0, "Preparing the seabed")
    spacing = float(np.nanmedian(np.diff(x))) if len(x) > 1 else 1.0
    step = float(np.nanmax(np.diff(x))) if len(x) > 1 else 0.0
    with np.errstate(invalid="ignore"):
        # Records beyond the profile (outside a KP window) must not pile into its end stations.
        rx = np.where((rx >= x[0] - step) & (rx <= x[-1] + step), rx, np.nan)
    logged = binned_max(x, rx, records.get("bottom_tension"))
    w = binned_min(x, rx, records.cable_array("w_water_npm"))
    npts = binned_min(x, rx, records.cable_array("npts_kn"))
    ei = binned_min(x, rx, records.cable_array("bending_stiffness_knm2"))
    mbr = binned_max(x, rx, records.cable_array("mbr_m"))
    if smoothing_m is None:
        smoothing_m = auto_smoothing_m(records, spacing)
    if filter_m is None:
        filter_m = AUTO_FILTER_M
    length_depth = smooth_profile(x, raw, smoothing_m)
    depth = smooth_profile(x, raw, filter_m)
    tension = np.where(np.isfinite(logged), logged, 0.0)
    source = np.where(tension > zero_tension_kn, 1, 0)
    w_fill = np.where(np.isfinite(w), w, np.nanmean(w) if np.isfinite(w).any() else np.nan)
    model = SeabedModel(x, np.asarray(kp_km, dtype=float), depth, depth.copy(), tension, w,
                        raw_depth_m=raw, length_depth_m=length_depth, logged_tension_kn=logged,
                        smoothing_m=float(smoothing_m or 0.0), filter_m=float(filter_m or 0.0))
    _check_cancel(cancel)
    if friction and records.has("bottom_slack", "td_depth") and np.isfinite(w).any():
        _report(progress, 0.05, "Balancing laid cable against the seabed")
        mu = float(friction.get("mu", 0.5))
        max_reach = float(friction.get("max_reach_m", 1000.0))
        tolerance = float(friction.get("shortfall_pct", 0.5))
        min_reach = max(float(smoothing_m or 0.0), 3.0 * spacing)
        cable_x, cable_cum = laid_cable_cumulative(x, rx, records.get("bottom_slack"), records.get("td_depth"),
                                                   float(smoothing_m or 0.0))
        tension, source, balance, reach, shortfalls = pull_taut(
            x, model.kp_km, length_depth, w_fill, tension, source, cable_x, cable_cum, mu, max_reach, tolerance,
            min_reach, npts, zero_tension_kn, cancel=cancel,
            progress=lambda f, text=None: _report(progress, 0.1 + 0.45 * f, text))
        model.balance_pct, model.reach_m, model.mu = balance, reach, mu
        model.shortfalls = shortfalls
    _check_cancel(cancel)
    _report(progress, 0.55, "Resting the cable on the seabed")
    with np.errstate(divide="ignore", invalid="ignore"):
        curvature = np.where((tension > zero_tension_kn) & np.isfinite(w) & (w > 0),
                             w / (tension * 1000.0), np.inf)
    model.cable_depth_m = -cable_rest_elevation(x, -depth, curvature)
    model.tension_kn = tension
    model.spans = find_spans(x, model.kp_km, model.gap_m, tension, min_gap_m, min_length_m, source,
                             contact_tol_m=SPAN_END_TOL_M)
    _report(progress, 0.62, "Bend radius and seabed reaction at span supports")
    model.supports = span_supports(x, -depth, -model.cable_depth_m, tension, w_fill, ei, mbr, model.spans,
                                   model.kp_km)
    if allowable:
        model.allowable_kn = allowable_tension_kn(
            x, -depth, w_fill, min_gap_m, min_length_m, cancel=cancel,
            progress=lambda f, *_: _report(progress, 0.65 + 0.35 * f,
                                           "Finding the tension that keeps spans within limits"),
            contact_tol_m=SPAN_END_TOL_M)
        for span in model.spans:
            values = model.allowable_kn[span.i0:span.i1 + 1]
            span.allowable_kn = float(np.nanmin(values)) if np.isfinite(values).any() else math.nan
    _report(progress, 1.0, "Seabed model done")
    return model


def _radius_text(radius: float) -> str:
    if radius == math.inf or radius > 1000.0:
        return "over 1000 m"
    return f"{radius:.1f} m"


def seabed_span_findings(model: SeabedModel, params) -> List[RangeFinding]:
    red_gap = float(params.get("red_gap_m", 1.0))
    red_length = float(params.get("red_length_m", 50.0))
    out = []
    for span in model.spans:
        red = span.max_gap_m >= red_gap or span.length_m >= red_length or span.below_mbr
        parts = [f"Modelled suspension {span.length_m:.0f} m long, up to {span.max_gap_m:.1f} m above the seabed "
                 f"at {_fmt(span.tension_kn, 'kN', 2)} ({span.source})"]
        if np.isfinite(span.min_bend_radius_m) or span.min_bend_radius_m == math.inf:
            text = f"bend radius at its supports {_radius_text(span.min_bend_radius_m)}"
            if span.below_mbr:
                text += f", BELOW the {span.mbr_m:.1f} m MBR"
            elif np.isfinite(span.mbr_m):
                text += f" (MBR {span.mbr_m:.1f} m)"
            parts.append(text)
        if np.isfinite(span.max_reaction_kn):
            parts.append(f"seabed reaction up to {span.max_reaction_kn:.2f} kN")
        if np.isfinite(span.allowable_kn):
            parts.append("spans here even at the lowest tension" if span.allowable_kn <= 0
                         else f"no reportable span here below about {span.allowable_kn:.2f} kN")
        extra = {"length_m": span.length_m, "kp_max_gap": span.kp_max_gap}
        for key in ("min_bend_radius_m", "max_reaction_kn", "allowable_kn"):
            value = getattr(span, key)
            if np.isfinite(value):
                extra[key] = float(value)
        out.append(RangeFinding(
            check_id="suspension", severity=Severity.ERROR if red else Severity.WARNING,
            kp_start=min(span.kp_start, span.kp_end), kp_end=max(span.kp_start, span.kp_end),
            message="; ".join(parts), value=span.max_gap_m, threshold=red_gap, unit="m", extra=extra))
    return out


def length_findings(model: SeabedModel, params, zero_tension_kn: float = 0.05) -> List[RangeFinding]:
    """Shortfalls (cable pulled taut) and surpluses (loop risk) against the seabed."""
    out = []
    for item in model.shortfalls:
        over_npts = np.isfinite(item.npts_kn) and item.tension_kn > item.npts_kn
        severity = Severity.ERROR if (not item.feasible or over_npts) else Severity.WARNING
        if not item.feasible:
            detail = ("even fully taut the laid cable cannot cover it: check the bottom slack, the touchdown "
                      "depths and the bathymetry datum")
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
             "Rests the cable on the seabed at the logged bottom tension (raised where the laid cable is "
             "short of the seabed, when the length check runs) with the cable's submerged weight, and flags "
             "where it spans clear of the seabed. Each span reports the bend radius and seabed reaction at "
             "its supports (red below the MBR) and the highest tension that would keep spans within the "
             "limits there.",
             (ParamSpec("min_gap_m", "Report spans higher than (m)", "float", 0.3, minimum=0.0),
              ParamSpec("min_length_m", "...and longer than (m)", "float", 5.0, minimum=0.0),
              ParamSpec("red_gap_m", "Red when higher than (m)", "float", 1.0, minimum=0.0),
              ParamSpec("red_length_m", "...or longer than (m)", "float", 50.0, minimum=0.0)),
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
CABLE_LABELS = _CABLE_LABELS  # cable library attribute -> what the user calls it


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
TRACK_SMOOTHING_M = 25.0  # touchdown positions averaged over this much KP before sampling the seabed


def track_vertices(records: LayRecords, spacing_m: float = 2.0,
                   smoothing_m: float = TRACK_SMOOTHING_M) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(lon, lat, kp_km)`` of a touchdown track ordered by KP.

    Records are binned every ``spacing_m`` of KP and each bin's median
    position kept, so stops and back-and-forth jitter do not zig-zag the
    track. The positions are then averaged over ``smoothing_m`` of KP
    (symmetric windows: a straight track is unchanged, ends included):
    lateral jitter of the lay model's touchdown across a side slope would
    otherwise be sampled as seabed relief (1-2 m of jitter on a 10 degree
    slope adds false spans and bend radii below the MBR). Raises ValueError
    without positions.
    """
    if records.lat is None or records.lon is None:
        raise ValueError("The layer has no touchdown positions.")
    ok = np.isfinite(records.kp) & np.isfinite(records.lat) & np.isfinite(records.lon)
    if ok.sum() < 2:
        raise ValueError("Fewer than two records with a KP and a position.")
    kp = records.kp[ok]
    key = np.floor(kp * 1000.0 / max(spacing_m, 0.01)).astype(np.int64)
    lon, lat, kp = bin_medians(key, records.lon[ok], records.lat[ok], kp)
    if smoothing_m and smoothing_m > 0 and len(kp) > 2:
        x = kp * 1000.0
        lon = smooth_profile(x, lon, smoothing_m, symmetric=True)
        lat = smooth_profile(x, lat, smoothing_m, symmetric=True)
    return lon, lat, kp


def record_spacing_m(records: LayRecords) -> float:
    """Median KP step between neighbouring touchdown records (m), nan when unknown."""
    kp = np.sort(records.kp[np.isfinite(records.kp)])
    steps = np.diff(kp) * 1000.0
    steps = steps[steps > 1e-6]
    return float(np.median(steps)) if steps.size else math.nan




def bin_medians(key: np.ndarray, *arrays: np.ndarray) -> List[np.ndarray]:
    """Median of each array per distinct integer ``key``, in ascending key order."""
    key = np.asarray(key)
    if key.size == 0:
        return [np.array([]) for _ in arrays]
    order = np.argsort(key, kind="stable")
    k = key[order]
    starts = np.concatenate(([0], np.nonzero(np.diff(k))[0] + 1))
    counts = np.diff(np.concatenate((starts, [len(k)])))
    lo, hi = starts + (counts - 1) // 2, starts + counts // 2
    out = []
    for values in arrays:
        v = np.asarray(values, dtype=float)[order]
        v = v[np.lexsort((v, k))]  # sorted within each key; groups stay in place
        out.append(0.5 * (v[lo] + v[hi]))
    return out


def lay_model_profile(records: LayRecords, step_m: float,
                      window: Optional[Tuple[float, float]] = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(x_m, kp_km, depth_m)`` of the lay model's own seabed: touchdown
    depths binned every ``step_m`` of KP (median per bin)."""
    kp = records.kp
    depth = np.abs(records.get("td_depth"))
    ok = np.isfinite(kp) & np.isfinite(depth)
    if window is not None:
        ok &= (kp >= window[0]) & (kp <= window[1])
    if ok.sum() < 2:
        return np.array([]), np.array([]), np.array([])
    key = np.floor(kp[ok] * 1000.0 / max(float(step_m), 0.01)).astype(np.int64)
    kp_bins, depth_bins = bin_medians(key, kp[ok], depth[ok])
    return kp_bins * 1000.0, kp_bins, depth_bins


def chainage_to_kp(vertex_chainage_m: np.ndarray, vertex_kp_km: np.ndarray, chainage_m) -> np.ndarray:
    return np.interp(chainage_m, vertex_chainage_m, vertex_kp_km)


def kp_to_chainage(vertex_chainage_m: np.ndarray, vertex_kp_km: np.ndarray, kp_km) -> np.ndarray:
    """Inverse of :func:`chainage_to_kp` (vertex KPs are ascending)."""
    return np.interp(kp_km, vertex_kp_km, vertex_chainage_m)
