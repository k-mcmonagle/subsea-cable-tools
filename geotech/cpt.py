# -*- coding: utf-8 -*-
"""Cone penetration tests from AGS files, and their derived parameters.

Pure python (no numpy), so it runs in every QGIS Python and in the plain
test suite.

**Tests from AGS.** Three generations of CPT groups are read into one model:

========  ======================  ================================  ==================
Edition   Readings                Test details / key                Interpretation
========  ======================  ================================  ==================
AGS 3.x   STCN (HOLE_ID)          — (one push per HOLE_ID)          —
AGS 4.0+  SCPT (LOCA_ID+TESN)     SCPG (area ratio SCPG_CAR)        SCPP (per layer)
AGS 4.2   CPTT (LOCA_ID+TESN)     CPTG (CPTG_CAR, CPTG_RLOC, TERM)  CPTP (per depth)
========  ======================  ================================  ==================

AGS 4.2 (December 2025) deprecates SCPG/SCPT/SCPP in favour of CPTG/CPTT/
CPTP; files of either kind are read. Locations come from LOCA (AGS 4) or
HOLE (AGS 3). Readings are converted to one set of units — depth m, qc and
qt MPa, fs and pore pressures kPa — from the unit stated for each heading,
or the edition's default unit when none is stated (AGS 3: MN/m2 for qc,
kN/m2 for fs and pore pressure; AGS 4: MPa), with a warning.

AGS 3 does not define where its pore pressures were measured (STCN_PWP1/2/3
are "porewater pressure", "second" and "third"); AGS 4 fixes PWP1 = u1
(face), PWP2 = u2 (shoulder), PWP3 = u3. A standard piezocone measures u2,
so an AGS 3 STCN_PWP1 is read as u2 unless the caller says otherwise
(``extract_tests(..., ags3_pore=...)``); the assumption is listed with the
test's warnings.

Values the contractor delivered as derived (qt, Rf, Bq, qnet, u0 and the
SCPP/CPTP interpretation) are kept under ``delivered_*`` / ``interpreted``
so they can be compared with the values computed here.

**Derived parameters** follow Robertson & Cabal, *Guide to Cone Penetration
Testing* (7th ed., 2022), with the assumptions in ``DEFAULT_SETTINGS``:

- qt = qc + u2 (1 − a) (u2 by default; qt = qc when no pore pressure is used)
- Rf = fs / qt · 100 %
- σv0 = Σ γ Δz, u0 = γw z for readings zeroed at the seabed (offshore
  practice); readings zeroed on deck also carry the water column γw · d_w
- qnet = qt − σv0, Bq = (u2 − u0) / qnet, Fr = fs / qnet · 100 %
- Qtn = (qnet / pa)(pa / σ'v0)^n, n = 0.381 Ic + 0.05 σ'v0/pa − 0.15 ≤ 1
  (iterated), Ic = √((3.47 − log Qtn)² + (log Fr + 1.22)²)
- SBTn zone 2–7 from Ic (Robertson 2009); zones 1, 8 and 9 need the full
  chart and are not assigned
- Su = qnet / Nkt with Nkt = 15 (``su``) and a lower estimate with Nkt = 20
  (``su_low``), where qnet > 0; left out where Ic < 2.6 (coarse-grained) by
  default, and still given where Ic cannot be found (fs = 0)
- Dr = √(Qtn / 350) (young clean sand), coarse-grained readings only
- γ constant, or γ/γw = 0.27 log Rf + 0.36 log(qt/pa) + 1.236 (Robertson &
  Cabal 2010), clamped to 14–22 kN/m³

They are screening values: the contractor's interpretation and laboratory
data take precedence.
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Dict, List, Optional, Sequence, Tuple

from . import ags as ags_mod
from .ags import AgsFile, number
from .positions import parse_coordinate

PA_KPA = 100.0   # atmospheric pressure (kPa) used for normalisation

# -- channels -----------------------------------------------------------------------
# key: (label, symbol, unit)
CHANNELS: Dict[str, Tuple[str, str, str]] = {
    "qc": ("Cone resistance", "qc", "MPa"),
    "fs": ("Sleeve friction", "fs", "kPa"),
    "u1": ("Pore pressure, face", "u1", "kPa"),
    "u2": ("Pore pressure, shoulder", "u2", "kPa"),
    "u3": ("Pore pressure, sleeve top", "u3", "kPa"),
    "incl": ("Inclination", "i", "°"),
    "incl2": ("Inclination 2", "i2", "°"),
    "temp": ("Temperature", "T", "°C"),
    "cond": ("Conductivity", "EC", ""),
    "delivered_qt": ("Corrected cone resistance (delivered)", "qt (deliv.)", "MPa"),
    "delivered_rf": ("Friction ratio (delivered)", "Rf (deliv.)", "%"),
    "delivered_bq": ("Pore pressure ratio (delivered)", "Bq (deliv.)", ""),
    "delivered_qnet": ("Net cone resistance (delivered)", "qnet (deliv.)", "MPa"),
    "delivered_u0": ("In situ pore pressure (delivered)", "u0 (deliv.)", "kPa"),
}

DERIVED: Dict[str, Tuple[str, str, str]] = {
    "qt": ("Corrected cone resistance", "qt", "MPa"),
    "rf": ("Friction ratio", "Rf", "%"),
    "u": ("Pore pressure used", "u", "kPa"),
    "gamma": ("Unit weight", "γ", "kN/m³"),
    "sv0": ("Total vertical stress", "σv0", "kPa"),
    "u0": ("Hydrostatic pore pressure", "u0", "kPa"),
    "sv0_eff": ("Effective vertical stress", "σ'v0", "kPa"),
    "qnet": ("Net cone resistance", "qnet", "kPa"),
    "bq": ("Pore pressure ratio", "Bq", ""),
    "fr": ("Normalised friction ratio", "Fr", "%"),
    "qtn": ("Normalised cone resistance", "Qtn", ""),
    "ic": ("Soil behaviour type index", "Ic", ""),
    "zone": ("SBTn zone", "SBTn", ""),
    "su": ("Undrained shear strength", "Su", "kPa"),
    "su_low": ("Undrained shear strength, lower estimate", "Su low", "kPa"),
    "dr": ("Relative density", "Dr", "%"),
}

# Heading per channel: (AGS 3 STCN, AGS 4.0–4.1 SCPT, AGS 4.2 CPTT)
_HEADINGS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]] = {
    "depth": (("STCN_DPTH",), ("SCPT_DPTH",), ("CPTT_DPTH", "CPTT_PLEN")),
    "qc": (("STCN_RES",), ("SCPT_RES",), ("CPTT_QC",)),
    "fs": (("STCN_FRES",), ("SCPT_FRES",), ("CPTT_FS",)),
    "u1": ((), ("SCPT_PWP1",), ("CPTT_U1",)),
    "u2": ((), ("SCPT_PWP2",), ("CPTT_U2",)),
    "u3": ((), ("SCPT_PWP3",), ("CPTT_U3",)),
    "incl": (("STCN_SLP1",), ("SCPT_SLP1",), ("CPTT_INCX",)),
    "incl2": (("STCN_SLP2",), ("SCPT_SLP2",), ("CPTT_INCY",)),
    "temp": (("STCN_TEMP",), ("SCPT_TEMP",), ("CPTT_TEMP",)),
    "cond": (("STCN_CON",), ("SCPT_CON",), ("CPTT_CON",)),
    "delivered_qt": (("STCN_QT",), ("SCPT_QT",), ("CPTT_QT",)),
    "delivered_rf": (("STCN_FRR",), ("SCPT_FRR",), ("CPTT_RF",)),
    "delivered_bq": (("STCN_BQ",), ("SCPT_BQ",), ("CPTT_BQ",)),
    "delivered_qnet": ((), ("SCPT_QNET",), ("CPTT_QNET",)),
    "delivered_u0": ((), ("SCPT_ISPP",), ("CPTT_ISPP",)),
}
# Default units when a file states none: AGS 3 (MN/m2, kN/m2) and AGS 4 (MPa).
_DEFAULT_UNITS = {
    "STCN_DPTH": "m", "STCN_RES": "MN/m2", "STCN_FRES": "kN/m2", "STCN_PWP1": "kN/m2",
    "STCN_PWP2": "kN/m2", "STCN_PWP3": "kN/m2", "STCN_QT": "MN/m2",
}
_EDITION_DEFAULT = {"depth": "m", "qc": "MPa", "fs": "MPa", "u1": "MPa", "u2": "MPa", "u3": "MPa",
                    "delivered_qt": "MPa", "delivered_qnet": "MPa", "delivered_u0": "MPa"}
_AGS3_PORE = ("STCN_PWP1", "STCN_PWP2", "STCN_PWP3")
_PRESSURE_MPA = ("qc", "delivered_qt", "delivered_qnet")
_PRESSURE_KPA = ("fs", "u1", "u2", "u3", "delivered_u0")

# Pressure unit -> kPa
_TO_KPA = {
    "kpa": 1.0, "kn/m2": 1.0, "kn/m²": 1.0, "kn/m^2": 1.0, "mpa": 1000.0, "mn/m2": 1000.0,
    "mn/m²": 1000.0, "mn/m^2": 1000.0, "pa": 0.001, "n/m2": 0.001, "bar": 100.0, "mbar": 0.1,
    "kg/cm2": 98.0665, "kgf/cm2": 98.0665, "tsf": 95.7605, "psi": 6.894757, "ksf": 47.88026,
}
_TO_M = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048}

SBT_ZONES: Dict[int, Tuple[str, str]] = {
    2: ("Organic soils – clay", "#7b4a2d"),
    3: ("Clays – silty clay to clay", "#3d6fa8"),
    4: ("Silt mixtures – clayey silt to silty clay", "#4fa3a5"),
    5: ("Sand mixtures – silty sand to sandy silt", "#9cc46b"),
    6: ("Sands – clean sand to silty sand", "#e3c25a"),
    7: ("Gravelly sand to dense sand", "#e08a3c"),
}
# Ic boundaries between zones 7|6|5|4|3|2 (Robertson 2009).
IC_BOUNDARIES = (1.31, 2.05, 2.60, 2.95, 3.60)

# Reference bands for the log plots (display only, never used in a result):
# undrained strength terms of BS 5930:2015 / BS EN ISO 14688-2 (kPa), and
# cone-resistance density terms often quoted in burial assessments (MPa;
# indicative — density from qc depends on stress level).
SU_TERMS: Tuple[Tuple[float, Optional[float], str], ...] = (
    (0, 10, "Extremely low"), (10, 20, "Very low"), (20, 40, "Low"), (40, 75, "Medium"),
    (75, 150, "High"), (150, 300, "Very high"), (300, None, "Extremely high"))
QC_TERMS: Tuple[Tuple[float, Optional[float], str], ...] = (
    (0, 2.5, "Very loose"), (2.5, 5, "Loose"), (5, 10, "Medium dense"), (10, 20, "Dense"),
    (20, None, "Very dense"))

DEFAULT_SETTINGS: Dict = {
    "area_ratio": 0.75,           # net area ratio a when the file gives none
    "pore_channel": "auto",       # auto (u2 if delivered) | u1 | u2 | u3 | none
    "unit_weight_mode": "constant",  # constant | estimated
    "unit_weight": 18.0,          # kN/m³, total (bulk) unit weight
    "water_unit_weight": 10.05,   # kN/m³, sea water
    "zeroed_at_seabed": True,     # readings referenced to the seabed (offshore)
    "nkt": 15.0,                  # Su = qnet / Nkt
    "nkt_high": 20.0,             # lower Su estimate = qnet / Nkt_high
    "su_fine_only": True,         # no Su where Ic < 2.6 (coarse-grained)
    "dr_coarse_only": True,       # Dr only where Ic < 2.6 (when Ic is known)
}


def settings(values: Optional[Dict] = None) -> Dict:
    """Processing settings with defaults filled and numbers validated."""
    out = dict(DEFAULT_SETTINGS)
    for key, value in (values or {}).items():
        if key in out and value is not None:
            out[key] = value
    for key in ("area_ratio", "unit_weight", "water_unit_weight", "nkt", "nkt_high"):
        try:
            out[key] = float(out[key])
        except (TypeError, ValueError):
            out[key] = DEFAULT_SETTINGS[key]
        if not math.isfinite(out[key]):
            out[key] = DEFAULT_SETTINGS[key]
    if not 0.0 < out["area_ratio"] <= 1.0:
        out["area_ratio"] = DEFAULT_SETTINGS["area_ratio"]
    for key in ("unit_weight", "water_unit_weight", "nkt", "nkt_high"):
        if not out[key] > 0.0:
            out[key] = DEFAULT_SETTINGS[key]
    if out["pore_channel"] not in ("auto", "u1", "u2", "u3", "none"):
        out["pore_channel"] = "auto"
    if out["unit_weight_mode"] not in ("constant", "estimated"):
        out["unit_weight_mode"] = "constant"
    for key in ("zeroed_at_seabed", "su_fine_only", "dr_coarse_only"):
        out[key] = bool(out[key])
    return out


def channel_label(key: str, with_unit: bool = True) -> str:
    label, symbol, unit = CHANNELS.get(key) or DERIVED.get(key) or (key, key, "")
    text = f"{symbol} — {label}" if symbol and symbol != label else label
    return f"{text} ({unit})" if with_unit and unit else text


def channel_unit(key: str) -> str:
    return (CHANNELS.get(key) or DERIVED.get(key) or ("", "", ""))[2]


def channel_symbol(key: str) -> str:
    return (CHANNELS.get(key) or DERIVED.get(key) or ("", key, ""))[1]


# -- AGS → tests -----------------------------------------------------------------------
def _unit_factor(key: str, unit: str) -> Tuple[Optional[float], str]:
    """``(factor to the canonical unit, problem)``; factor None = unknown unit."""
    text = str(unit or "").strip().replace(" ", "").casefold()
    if key == "depth":
        factor = _TO_M.get(text)
        return (factor, "") if factor is not None else (None, f"depth unit '{unit}' is not known")
    if key in _PRESSURE_MPA or key in _PRESSURE_KPA:
        kpa = _TO_KPA.get(text)
        if kpa is None:
            return None, f"pressure unit '{unit}' is not known"
        return (kpa / 1000.0 if key in _PRESSURE_MPA else kpa), ""
    return 1.0, ""


def _row_value(row: Optional[Dict[str, str]], *headings: str) -> str:
    for heading in headings:
        value = str((row or {}).get(heading) or "").strip()
        if value:
            return value
    return ""


_EPSG = re.compile(r"^\s*(?:EPSG\s*:?\s*)?(\d{4,6})\s*$", re.IGNORECASE)


def _epsg(text: str) -> str:
    """``EPSG:32631`` from ``32631`` / ``EPSG:32631`` / ``epsg 32631``; free text
    such as "ETRS89 / UTM zone 31N" is not a code and gives ""."""
    match = _EPSG.match(str(text or ""))
    return f"EPSG:{match.group(1)}" if match else ""


def _location_record(row: Optional[Dict[str, str]], prefix: str) -> Dict:
    """Position and level fields of a LOCA (AGS 4) or HOLE (AGS 3) row.

    ``water_depth`` comes from a user-defined LOCA_WDEP / HOLE_WDEP when
    present (``water_depth_source`` says which field).
    """
    if not row:
        return {}
    other = "HOLE" if prefix == "LOCA" else "LOCA"
    out: Dict = {}

    def field(name):
        return _row_value(row, f"{prefix}_{name}", f"{other}_{name}")

    for key, name in (("easting", "NATE"), ("northing", "NATN"), ("ground_level", "GL"),
                      ("final_depth", "FDEP"), ("local_x", "LOCX"), ("local_y", "LOCY"),
                      ("local_z", "LOCZ"), ("water_depth", "WDEP")):
        out[key] = number(field(name))
    out["water_depth_source"] = f"{prefix}_WDEP" if out.get("water_depth") is not None else ""
    out["grid"] = field("GREF")
    out["grid_crs"] = _epsg(field("NSRI"))
    out["latlon_crs"] = _epsg(field("LLSI"))
    out["local_grid"] = field("LREF") or field("LOCM")
    out["type"] = field("TYPE")
    out["date"] = field("STAR") or field("ENDD")
    out["termination"] = field("TERM")
    out["latlon_datum"] = field("LLZ")
    out["vessel"] = field("VSSL")
    for key, name, axis in (("lat", "LAT", "lat"), ("lon", "LON", "lon")):
        text = field(name)
        try:
            out[key] = parse_coordinate(text, axis) if text else None
        except ValueError:
            out[key] = None
            out.setdefault("problems", []).append(f"{prefix}_{name} '{text}' is not a coordinate")
    if (out.get("lat") is None) != (out.get("lon") is None):
        out["lat"] = out["lon"] = None
    return out


def _keyed(group, key_headings: Sequence[str], test_headings: Sequence[str]) -> Dict:
    """``{(location id, test ref): [rows]}`` of a group."""
    out: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    if group is None:
        return out
    key = next((h for h in key_headings if group.has(h)), "")
    test = next((h for h in test_headings if group.has(h)), "")
    for row in group.rows:
        out.setdefault((row.get(key, "").strip() if key else "",
                        row.get(test, "").strip() if test else ""), []).append(row)
    return out


def _interpretation(ags: AgsFile) -> Dict[Tuple[str, str], List[Dict]]:
    """Delivered interpretation: SCPP layers (AGS 4.0) or CPTP depths (4.2)."""
    out: Dict[Tuple[str, str], List[Dict]] = {}
    scpp = ags.group("SCPP")
    if scpp is not None:
        for (location, test), rows in _keyed(scpp, ("LOCA_ID",), ("SCPG_TESN",)).items():
            for row in rows:
                top = number(row.get("SCPP_TOP"))
                if top is None:
                    continue
                out.setdefault((location, test), []).append({
                    "top": top, "base": number(row.get("SCPP_BASE")), "ref": row.get("SCPP_REF", ""),
                    "soil": row.get("SCPP_CSBT", ""), "su": number(row.get("SCPP_CSU")),
                    "su_low": None, "dr": number(row.get("SCPP_CRD")), "dr_low": None,
                    "ic": number(row.get("SCPP_CIC")), "phi": number(row.get("SCPP_CPHI"))})
    cptp = ags.group("CPTP")
    if cptp is not None:
        # CPTP is keyed by location and depth only; it applies to every test there.
        for (location, _test), rows in _keyed(cptp, ("LOCA_ID",), ()).items():
            for row in rows:
                top = number(row.get("CPTP_DPTH"))
                if top is None:
                    continue
                su = [v for v in (number(row.get("CPTP_SU1")), number(row.get("CPTP_SU2"))) if v is not None]
                dr = [v for v in (number(row.get("CPTP_DR1")), number(row.get("CPTP_DR2"))) if v is not None]
                out.setdefault((location, "*"), []).append({
                    "top": top, "base": number(row.get("CPTP_BASE")), "ref": "",
                    "soil": row.get("CPTP_SBT1", ""), "su": max(su) if su else None,
                    "su_low": min(su) if len(su) > 1 else None, "dr": max(dr) if dr else None,
                    "dr_low": min(dr) if len(dr) > 1 else None, "ic": number(row.get("CPTP_IC1")),
                    "phi": number(row.get("CPTP_PHI1"))})
    for rows in out.values():
        rows.sort(key=lambda r: r["top"])
    return out


def _ags3_pore_map(data, choice: str) -> Dict[str, str]:
    """``{AGS 3 heading: channel}`` for the pore pressures present.

    ``choice`` is the channel STCN_PWP1 is read as (u2 by default); the
    second and third readings take the remaining AGS 4 positions in order.
    """
    present = [h for h in _AGS3_PORE if data.has(h)]
    if not present:
        return {}
    first = choice if choice in ("u1", "u2", "u3") else "u2"
    remaining = [c for c in ("u1", "u2", "u3") if c != first]
    mapping = {}
    for heading in present:
        if heading == "STCN_PWP1":
            mapping[heading] = first
        else:
            mapping[heading] = remaining.pop(0)
    return mapping


def extract_tests(ags: AgsFile, ags3_pore: str = "u2") -> List[Dict]:
    """Every CPT test in an AGS file, readings in the canonical units.

    Each test is a dict: ``location_id``, ``test_ref``, ``file``, ``path``,
    ``stem``, ``ags_version``, ``group`` (CPTT/SCPT/STCN), ``depth`` (list),
    ``channels`` ({key: list}), ``units``, ``headings`` (the AGS heading of
    each channel), ``location`` (see ``_location_record``), ``general``
    (the CPTG/SCPG row), ``geology`` (GEOL layers), ``interpreted``
    (SCPP/CPTP), ``project`` (PROJ), ``area_ratio``, ``zero_reference``
    ("seabed", "borehole", "deck" or ""), ``termination`` and ``warnings``.
    An AGS 3 location whose depths restart (a second push) is split into
    tests "1", "2", ….
    """
    data = ags.group("CPTT", "SCPT", "STCN")
    if data is None:
        return []
    edition = {"CPTT": 2, "SCPT": 1, "STCN": 0}[data.name]
    id_heading = next((h for h in ("LOCA_ID", "HOLE_ID") if data.has(h)), "")
    test_heading = next((h for h in ("CPTG_TESN", "SCPG_TESN", "STCN_TESN") if data.has(h)), "")
    location_group = ags.group("LOCA", "HOLE")
    prefix = location_group.name if location_group is not None else ("HOLE" if edition == 0 else "LOCA")
    locations: Dict[str, Dict[str, str]] = {}
    if location_group is not None:
        key = "LOCA_ID" if location_group.has("LOCA_ID") else "HOLE_ID"
        for row in location_group.rows:
            locations.setdefault(row.get(key, "").strip(), row)
    general_group = ags.group("CPTG", "SCPG", "STCG")
    general = {k: rows[0] for k, rows in _keyed(general_group, ("LOCA_ID", "HOLE_ID"),
                                                 ("CPTG_TESN", "SCPG_TESN", "STCG_TESN")).items()}
    geology: Dict[str, List[Dict]] = {}
    for (location, _test), rows in _keyed(ags.group("GEOL"), ("LOCA_ID", "HOLE_ID"), ()).items():
        for row in rows:
            top, base = number(row.get("GEOL_TOP")), number(row.get("GEOL_BASE"))
            if top is None:
                continue
            geology.setdefault(location, []).append({
                "top": top, "base": base, "description": row.get("GEOL_DESC", ""),
                "soil_class": row.get("GEOL_LEG", "") or row.get("GEOL_GEOL", ""),
                "legend": row.get("GEOL_LEG", ""), "geol": row.get("GEOL_GEOL", "")})
    interpreted = _interpretation(ags)
    project = ags.groups["PROJ"].rows[0] if "PROJ" in ags.groups and ags.groups["PROJ"].rows else {}

    # Which heading feeds each channel, and its unit factor.
    columns: Dict[str, Tuple[str, float]] = {}
    file_warnings: List[str] = []
    for key, by_edition in _HEADINGS.items():
        heading = next((h for h in by_edition[edition] if data.has(h)), None)
        if heading is None:
            continue
        columns[key] = (heading, 0.0)
    if edition == 0:
        for heading, channel in _ags3_pore_map(data, ags3_pore).items():
            columns[channel] = (heading, 0.0)
            file_warnings.append(f"{heading} read as {channel} (AGS 3 does not say where the pore "
                                 "pressure was measured)")
    if edition == 2 and columns.get("depth", ("",))[0] == "CPTT_PLEN":
        file_warnings.append("CPTT_DPTH is missing; depth is the penetration length CPTT_PLEN "
                             "(not corrected for inclination)")
    for key, (heading, _f) in list(columns.items()):
        unit = data.unit(heading)
        if not unit and key in ("depth",) + _PRESSURE_MPA + _PRESSURE_KPA:
            unit = _DEFAULT_UNITS.get(heading) or _EDITION_DEFAULT.get(key, "")
            file_warnings.append(f"{heading} has no unit; the AGS default {unit} was assumed")
        factor, problem = _unit_factor(key, unit)
        if factor is None:
            if key == "depth":
                raise ags_mod.AgsError(f"{ags.file_name}: {problem}")
            file_warnings.append(f"{heading}: {problem}; the channel was not imported")
            del columns[key]
            continue
        columns[key] = (heading, factor)
    if "depth" not in columns:
        raise ags_mod.AgsError(f"{ags.file_name}: the {data.name} group has no depth heading")
    if "qc" not in columns:
        file_warnings.append(f"the {data.name} group has no cone resistance")

    def new_test(location_id: str, test_ref: str) -> Dict:
        location_row = locations.get(location_id)
        general_row = general.get((location_id, test_ref)) or general.get((location_id, "")) or {}
        location = _location_record(location_row, prefix)
        test = {
            "location_id": location_id, "test_ref": test_ref,
            "file": ags.file_name, "path": ags.path, "stem": ags.stem,
            "ags_version": ags.version_label, "group": data.name, "depth": [],
            "channels": {k: [] for k in columns if k != "depth"},
            "units": {k: channel_unit(k) for k in columns if k != "depth"},
            "headings": {k: h for k, (h, _f) in columns.items()},
            "location": location, "general": dict(general_row),
            "geology": sorted(geology.get(location_id, []), key=lambda g: g["top"]),
            "interpreted": interpreted.get((location_id, test_ref)) or interpreted.get((location_id, "*")) or [],
            "project": dict(project), "warnings": list(file_warnings),
        }
        area = number(_row_value(general_row, "CPTG_CAR", "SCPG_CAR"))
        test["area_ratio"] = area if area is not None and 0.0 < area <= 1.0 else None
        zero = _row_value(general_row, "CPTG_RLOC").upper()
        test["zero_reference"] = {"SB": "seabed", "BB": "borehole", "S": "deck"}.get(zero, "")
        test["termination"] = _row_value(general_row, "CPTG_TERM") or location.get("termination", "")
        test["predrilled_m"] = number(_row_value(general_row, "CPTG_PED"))
        level = number(_row_value(general_row, "CPTG_WAT"))
        if location.get("water_depth") is None and level is not None and level < 0 \
                and test["zero_reference"] in ("seabed", ""):
            location["water_depth"] = -level
            location["water_depth_source"] = "CPTG_WAT"
        if location_row is None and location_group is not None:
            test["warnings"].append(f"{location_id or '(blank)'} is not in the {prefix} group")
        for problem in location.get("problems", []):
            test["warnings"].append(problem)
        return test

    tests: Dict[Tuple[str, str], Dict] = {}
    pushes: Dict[str, Dict] = {}
    depth_heading, depth_factor = columns["depth"]
    for line, row in zip(data.row_lines, data.rows):
        location_id = row.get(id_heading, "").strip() if id_heading else ""
        test_ref = row.get(test_heading, "").strip() if test_heading else ""
        depth = number(row.get(depth_heading))
        if not test_heading:
            # AGS 3 has one push per HOLE_ID; depths that restart (by more
            # than 0.1 m) begin another push of the same location.
            state = pushes.setdefault(location_id, {"push": 1, "last": None})
            if depth is not None:
                metres = depth * depth_factor
                if state["last"] is not None and metres < state["last"] - 0.1:
                    state["push"] += 1
                state["last"] = metres
            test_ref = str(state["push"])
        key = (location_id, test_ref)
        test = tests.get(key)
        if test is None:
            test = tests[key] = new_test(location_id, test_ref)
            if not test_heading and test_ref != "1":
                test["warnings"].append(f"the depths of {location_id} restart at line {line}: "
                                        f"read as push {test_ref}")
        if depth is None:
            test["warnings"].append(f"line {line}: no depth; the reading was skipped")
            continue
        test["depth"].append(round(depth * depth_factor, 6))
        for channel, (heading, factor) in columns.items():
            if channel == "depth":
                continue
            value = number(row.get(heading))
            test["channels"][channel].append(None if value is None else value * factor)
    if not test_heading:
        for location_id, state in pushes.items():
            if state["push"] == 1 and (location_id, "1") in tests:
                tests[(location_id, "1")]["test_ref"] = ""
    out = []
    for test in tests.values():
        if not test["depth"]:
            continue
        depths = test["depth"]
        if any(b < a - 1e-9 for a, b in zip(depths, depths[1:])):
            test["warnings"].append("depths are not in increasing order")
        fs = [v for v in test["channels"].get("fs") or [] if v is not None]
        zeros = sum(1 for v in fs if v == 0.0)
        if fs and zeros > 0.25 * len(fs):
            steps = sorted({round(abs(a - b), 6) for a, b in zip(fs, fs[1:]) if a != b})
            step = f" (smallest step {steps[0]:g} kPa)" if steps else ""
            test["warnings"].append(f"fs is 0 for {100 * zeros / len(fs):.0f}% of readings{step}: "
                                    "Rf, Fr and Ic are undefined there")
        # Channels with no reading at all are dropped (e.g. an empty column).
        for channel in [k for k, values in test["channels"].items() if all(v is None for v in values)]:
            del test["channels"][channel]
            del test["units"][channel]
            test["headings"].pop(channel, None)
        out.append(test)
    return out


def read_tests(path: str, ags3_pore: str = "u2") -> Tuple[List[Dict], AgsFile]:
    """``(tests, ags)`` for one AGS file."""
    ags = ags_mod.read_ags(path)
    return extract_tests(ags, ags3_pore), ags


# -- names ------------------------------------------------------------------------------
NAME_AUTO = "auto"
NAME_FILE = "file"
NAME_LOCATION = "location"
NAME_FILE_LOCATION = "file_location"
NAME_RULES = {
    NAME_FILE: "File name (one CPT per file)",
    NAME_LOCATION: "AGS location ID",
    NAME_FILE_LOCATION: "File name + location ID",
}


def default_name_rule(tests: Sequence[Dict]) -> str:
    """File name when every file holds one location, else the location ID."""
    per_file: Dict[str, set] = {}
    for test in tests:
        per_file.setdefault(test["path"] or test["file"], set()).add(test["location_id"])
    return NAME_FILE if per_file and all(len(n) == 1 for n in per_file.values()) else NAME_LOCATION


def assign_names(tests: List[Dict], rule: str = NAME_AUTO) -> List[str]:
    """Set each test's ``name`` by ``rule``; returns notes on repeated names.

    A test reference is appended when one location has several tests; a name
    still repeated (two files with the same location ID, say) gains " (2)".
    """
    if rule == NAME_AUTO or rule not in NAME_RULES:
        rule = default_name_rule(tests)
    per_location: Dict[Tuple[str, str], int] = {}
    for test in tests:
        key = (test["path"] or test["file"], test["location_id"])
        per_location[key] = per_location.get(key, 0) + 1
    seen: Dict[str, int] = {}
    notes: List[str] = []
    for test in tests:
        location = test["location_id"] or "(blank)"
        if rule == NAME_FILE:
            name = test["stem"] or location
        elif rule == NAME_FILE_LOCATION:
            name = f"{test['stem']} {location}".strip()
        else:
            name = location
        if test["test_ref"] and per_location[(test["path"] or test["file"], test["location_id"])] > 1:
            name = f"{name} #{test['test_ref']}"
        count = seen.get(name, 0) + 1
        seen[name] = count
        if count > 1:
            notes.append(f"{name} appears {count} times; the later one is named '{name} ({count})'")
            name = f"{name} ({count})"
        test["name"] = name
        test["name_rule"] = rule
    return notes


def cpt_id(campaign_id: str, name: str) -> str:
    """Stable id of a CPT within a campaign (re-imports keep it)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cpt␟{campaign_id}␟{name}"))


# -- derived parameters -------------------------------------------------------------------
def pore_channel(test: Dict, choice: str = "auto") -> Optional[str]:
    """The pore-pressure channel used for qt and Bq, or None."""
    channels = test.get("channels") or {}
    if choice == "none":
        return None
    if choice in ("u1", "u2", "u3"):
        return choice if choice in channels else None
    return "u2" if "u2" in channels else None


def ic_zone(ic: Optional[float]) -> Optional[int]:
    """SBTn zone 2–7 from Ic (Robertson 2009)."""
    if ic is None:
        return None
    for zone, limit in zip((7, 6, 5, 4, 3), IC_BOUNDARIES):
        if ic < limit:
            return zone
    return 2


def derive(test: Dict, values: Optional[Dict] = None,
           water_depth: Optional[float] = None) -> Dict[str, List[Optional[float]]]:
    """Derived channels (see the module notes), one value per reading.

    ``water_depth`` (m) is used only for readings zeroed on deck (the test's
    ``zero_reference``, else the ``zeroed_at_seabed`` setting). Values that
    cannot be computed are None.
    """
    opts = settings(values)
    depths = list(test.get("depth") or [])
    channels = test.get("channels") or {}
    n = len(depths)
    qc = channels.get("qc") or [None] * n
    fs = channels.get("fs") or [None] * n
    pore = pore_channel(test, opts["pore_channel"])
    u = channels.get(pore) if pore else None
    a = test.get("area_ratio") or opts["area_ratio"]
    gamma_w = opts["water_unit_weight"]
    reference = test.get("zero_reference") or ("seabed" if opts["zeroed_at_seabed"] else "deck")
    offset = gamma_w * abs(float(water_depth)) if reference == "deck" and water_depth else 0.0
    out: Dict[str, List[Optional[float]]] = {key: [None] * n for key in DERIVED}
    sv0 = offset
    previous_depth = 0.0
    for i, z in enumerate(depths):
        q = qc[i]
        f = fs[i]
        pressure = u[i] if u is not None else None
        qt = None
        if q is not None:
            qt = q + (pressure / 1000.0) * (1.0 - a) if pressure is not None else q
        rf = f / (qt * 1000.0) * 100.0 if qt and f is not None and qt > 0 else None
        gamma = opts["unit_weight"]
        if opts["unit_weight_mode"] == "estimated" and rf is not None and rf > 0 and qt and qt > 0:
            estimate = gamma_w * (0.27 * math.log10(rf) + 0.36 * math.log10(qt * 1000.0 / PA_KPA) + 1.236)
            gamma = min(max(estimate, 14.0), 22.0)
        dz = max(0.0, z - previous_depth)
        sv0 += gamma * dz
        previous_depth = max(previous_depth, z)
        u0 = offset + gamma_w * max(z, 0.0)
        sv0_eff = sv0 - u0
        qnet = qt * 1000.0 - sv0 if qt is not None else None
        bq = (pressure - u0) / qnet if pressure is not None and qnet and qnet > 0 else None
        fr = f / qnet * 100.0 if f is not None and qnet and qnet > 0 else None
        ic = qtn = None
        if qnet and qnet > 0 and sv0_eff > 0 and fr and fr > 0:
            exponent = 1.0
            for _ in range(60):
                qtn = (qnet / PA_KPA) * (PA_KPA / sv0_eff) ** exponent
                ic = math.sqrt((3.47 - math.log10(qtn)) ** 2 + (math.log10(fr) + 1.22) ** 2)
                new = min(1.0, 0.381 * ic + 0.05 * (sv0_eff / PA_KPA) - 0.15)
                if abs(new - exponent) < 1e-6:
                    exponent = new
                    break
                exponent = new
            qtn = (qnet / PA_KPA) * (PA_KPA / sv0_eff) ** exponent
            ic = math.sqrt((3.47 - math.log10(qtn)) ** 2 + (math.log10(fr) + 1.22) ** 2)
        # Su is left out where the reading behaves coarse-grained (Ic < 2.6);
        # where Ic cannot be found (fs = 0, σ'v0 = 0) it is still given.
        coarse = opts["su_fine_only"] and ic is not None and ic < IC_BOUNDARIES[2]
        usable = qnet is not None and qnet > 0 and not coarse
        su = qnet / opts["nkt"] if usable else None
        su_low = qnet / opts["nkt_high"] if usable else None
        dr = None
        if qtn is not None and qtn > 0 and not (opts["dr_coarse_only"] and (ic is None or ic >= IC_BOUNDARIES[2])):
            dr = 100.0 * math.sqrt(qtn / 350.0)
        for key, value in (("qt", qt), ("rf", rf), ("u", pressure), ("gamma", gamma), ("sv0", sv0),
                           ("u0", u0), ("sv0_eff", sv0_eff), ("qnet", qnet), ("bq", bq), ("fr", fr),
                           ("qtn", qtn), ("ic", ic), ("zone", ic_zone(ic)), ("su", su),
                           ("su_low", su_low), ("dr", dr)):
            out[key][i] = value
    return out


def all_channels(test: Dict, values: Optional[Dict] = None,
                 water_depth: Optional[float] = None) -> Dict[str, List[Optional[float]]]:
    """Measured and derived channels together (derived keys win on a clash)."""
    out = {key: list(vals) for key, vals in (test.get("channels") or {}).items()}
    out.update(derive(test, values, water_depth))
    return out


TRACE_CHANNELS = ("qc", "qt", "fs", "u", "rf", "qnet", "bq", "ic", "su", "su_low", "dr")


def value_at(depths: Sequence[float], values: Sequence[Optional[float]],
             depth: float) -> Optional[Tuple[float, Optional[float]]]:
    """``(reading depth, value)`` of the reading nearest ``depth``."""
    if not depths:
        return None
    best = min(range(len(depths)), key=lambda i: abs(depths[i] - depth))
    return depths[best], values[best]


def bin_profile(depths: Sequence[float], values: Sequence[Optional[float]],
                interval: float) -> List[Tuple[float, float, Optional[float]]]:
    """Mean value in each ``interval``-m depth bin: ``[(top, base, mean)]``.

    Bins whose readings are all missing keep None; bins without any reading
    are left out (no interpolation across gaps).
    """
    if not interval or interval <= 0:
        raise ValueError("The depth interval must be positive.")
    bins: Dict[int, List[Optional[float]]] = {}
    for depth, value in zip(depths, values):
        if depth is None or depth < 0:
            continue
        bins.setdefault(int(math.floor(depth / interval + 1e-9)), []).append(value)
    out = []
    for k in sorted(bins):
        present = [v for v in bins[k] if v is not None]
        mean = sum(present) / len(present) if present else None
        out.append((round(k * interval, 9), round((k + 1) * interval, 9), mean))
    return out
