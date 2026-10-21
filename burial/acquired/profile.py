# -*- coding: utf-8 -*-
"""Import profiles: which delivered column takes which role, and how to read it.

A profile is a plain JSON-able dict, saved with every file imported through
it (so the reading can be repeated exactly) and reusable for the next
delivery of the same format::

    {"version": 1, "name": "Plough log v2",
     "table": {"delimiter": None, "header_line": 1, "units_line": 2,
               "decimal_comma": False, "encoding": None, "sheet": None,
               "missing": ["", "nan", ...]},
     "columns": [{"header": "KP_km", "role": "kp", "unit": "km"},
                 {"header": "DOL", "role": "dol", "unit": "m", "sign": "down"},
                 {"header": "TowTension", "role": "tension", "name": "Tow", "unit": "kN"},
                 {"header": "DateTime", "role": "time", "format": "auto"}, ...],
     "timezone": "UTC", "start_date": "", "crs": "", "keep_extra": True}

Columns are matched by header text, so a later file with its columns in a
different order still reads. Headers the profile does not mention are kept
as extra channels when they are numeric (``keep_extra``), never guessed
into a role. :func:`suggest` proposes roles from header aliases; callers
show the proposal for the user to confirm.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Dict, List, Optional, Sequence

from . import roles as R

VERSION = 1
DELIMITERS = (",", ";", "\t", "|", "whitespace")
DEFAULT_MISSING = ("", "nan", "null", "none", "n/a", "na", "-", "--", "-9999", "-99999")
TIME_FORMATS = ("auto", "iso", "epoch", "epoch_ms")


def new_profile(name: str = "") -> Dict:
    return {
        "version": VERSION,
        "name": name,
        "table": {"delimiter": None, "header_line": 1, "units_line": 0, "decimal_comma": False,
                  "encoding": None, "sheet": None, "missing": list(DEFAULT_MISSING)},
        "columns": [],
        "timezone": "UTC",
        "start_date": "",
        "crs": "",
        "keep_extra": True,
    }


def normalise(profile: Optional[Dict]) -> Dict:
    """A complete profile from a stored or partial one (unknown keys dropped)."""
    out = new_profile()
    if not isinstance(profile, dict):
        return out
    out["name"] = str(profile.get("name") or "")
    table = profile.get("table") if isinstance(profile.get("table"), dict) else {}
    for key in out["table"]:
        if key in table:
            out["table"][key] = table[key]
    delimiter = out["table"]["delimiter"]
    out["table"]["delimiter"] = delimiter if delimiter in DELIMITERS else None
    out["table"]["header_line"] = max(1, _int(out["table"]["header_line"], 1))
    out["table"]["units_line"] = max(0, _int(out["table"]["units_line"], 0))
    out["table"]["decimal_comma"] = bool(out["table"]["decimal_comma"])
    out["table"]["missing"] = [str(v) for v in (out["table"]["missing"] or [])]
    columns = []
    for entry in profile.get("columns") or []:
        if not isinstance(entry, dict) or R.find(str(entry.get("role") or "")) is None:
            continue
        clean = {"header": str(entry.get("header") or ""), "role": str(entry["role"])}
        for key in ("unit", "sign", "name", "format"):
            if entry.get(key) not in (None, ""):
                clean[key] = str(entry[key])
        columns.append(clean)
    out["columns"] = columns
    for key in ("timezone", "start_date", "crs"):
        out[key] = str(profile.get(key) or out[key])
    out["keep_extra"] = bool(profile.get("keep_extra", True))
    return out


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_json(profile: Dict) -> str:
    return json.dumps(normalise(profile), ensure_ascii=False, sort_keys=True, indent=1)


def from_json(text: str) -> Dict:
    return normalise(json.loads(text))


def fingerprint_text(profile: Dict) -> str:
    """Canonical text of the reading rules (the name excluded): equal text, equal reading."""
    body = normalise(profile)
    body.pop("name", None)
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def column(header: str, role_key: str, **options) -> Dict:
    """A column entry with the role's default unit and sign filled in."""
    role = R.role(role_key)
    entry = {"header": header, "role": role_key}
    if role.units:
        entry["unit"] = options.pop("unit", None) or role.default_unit
    if role.sign:
        entry["sign"] = options.pop("sign", None) or R.sign_choices(role_key)[0][0]
    if role.multiple and role_key != R.IGNORE:
        entry["name"] = options.pop("name", None) or (R.tension_name(header) if role_key == R.TENSION else header)
    if role.kind in (R.KIND_TIME, R.KIND_DATE):
        entry["format"] = options.pop("format", None) or "auto"
    for key, value in options.items():
        if value not in (None, ""):
            entry[key] = value
    return entry


def mapped(profile: Dict) -> List[Dict]:
    """Column entries that import something (not *Ignore*)."""
    return [c for c in profile.get("columns", []) if c.get("role") != R.IGNORE]


def by_role(profile: Dict) -> Dict[str, List[Dict]]:
    out: Dict[str, List[Dict]] = {}
    for entry in mapped(profile):
        out.setdefault(entry["role"], []).append(entry)
    return out


def entry_channel_key(entry: Dict) -> str:
    return R.channel_key(entry["role"], entry.get("name", ""))


def entry_label(entry: Dict) -> str:
    return R.channel_label(entry["role"], entry.get("name", ""))


def status(profile: Dict) -> Dict[str, bool]:
    """What the mapping provides: KP, DOL, a full timestamp, a position pair."""
    present = set(by_role(profile))
    return {
        "kp": R.KP in present,
        "dol": R.DOL in present,
        "time": R.time_roles_complete(present),
        "position": any(a in present and b in present for a, b in R.POSITION_PAIRS),
    }


def problems(profile: Dict, headers: Optional[Sequence[str]] = None) -> List[str]:
    """Plain-language reasons the profile cannot import (empty: it can).

    With ``headers`` it also checks the mapped columns exist in that file.
    """
    out: List[str] = []
    grouped = by_role(profile)
    if R.KP not in grouped:
        out.append("Map a KP column.")
    if R.DOL not in grouped:
        out.append("Map a DOL column.")
    for key, entries in grouped.items():
        role = R.role(key)
        if not role.multiple and len(entries) > 1:
            names = ", ".join(e["header"] for e in entries)
            out.append(f"{role.label} is given to more than one column ({names}).")
        for entry in entries:
            unit = entry.get("unit")
            if role.units and unit and role.unit(unit) is None:
                out.append(f"{entry['header']}: '{unit}' is not a unit for {role.label}.")
            sign = entry.get("sign")
            if role.sign and sign and sign not in dict(R.sign_choices(key)):
                out.append(f"{entry['header']}: '{sign}' is not a sign convention for {role.label}.")
            if role.kind in (R.KIND_TIME, R.KIND_DATE):
                fmt = entry.get("format", "auto")
                if fmt not in TIME_FORMATS and "%" not in fmt and role.kind == R.KIND_TIME:
                    out.append(f"{entry['header']}: '{fmt}' is not a time format.")
    keys = [entry_channel_key(e) for e in mapped(profile)]
    for key in sorted({k for k in keys if keys.count(k) > 1}):
        if ":" in key:
            out.append(f"Two columns would both be stored as '{key.split(':', 1)[1]}'; give them different names.")
    for a, b in R.POSITION_PAIRS:
        if (a in grouped) != (b in grouped):
            have, need = (a, b) if a in grouped else (b, a)
            out.append(f"{R.role(have).label} needs {R.role(need).label} too.")
    if R.EASTING in grouped and R.NORTHING in grouped and not str(profile.get("crs") or "").strip():
        out.append("Choose the CRS of Easting and Northing.")
    present = set(grouped)
    if R.TIME in present and present & {R.DATE, R.CLOCK, R.DAY}:
        out.append("Use either Timestamp, or Date / Day number with Time of day.")
    elif R.CLOCK in present and not present & {R.DATE, R.DAY}:
        out.append("Time of day needs a Date or Day number column.")
    elif present & {R.DATE, R.DAY} and R.CLOCK not in present:
        out.append("Date and Day number need a Time of day column (or map neither and give the "
                   "acquisition date).")
    if R.DATE in present and R.DAY in present:
        out.append("Use Date or Day number, not both.")
    if R.DAY in present and not _is_date(profile.get("start_date", "")):
        out.append("Day numbers need the start date (day 1), as YYYY-MM-DD.")
    zone = str(profile.get("timezone") or "UTC")
    if parse_utc_offset(zone) is None:
        out.append(f"'{zone}' is not UTC or a UTC offset such as +01:00.")
    if headers is not None:
        available = set(headers)
        for entry in mapped(profile):
            if entry["header"] not in available:
                out.append(f"Column '{entry['header']}' is not in this file.")
    return out


def _is_date(text: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(text or "").strip()))


def parse_utc_offset(text: str) -> Optional[int]:
    """Seconds east of UTC for ``UTC``, ``Z``, ``+01:00``, ``UTC-05``, ``-0330``; None if not one.

    Named zones (with daylight saving) are deliberately not offered: QGIS 4
    ships no time-zone database for Python, and a delivery logged in a fixed
    offset reads the same in every QGIS.
    """
    value = str(text or "").strip().upper().replace(" ", "")
    if value in ("UTC", "Z", "GMT", "UTC+0", "UTC0", "+00:00", "-00:00", "UTC+00:00"):
        return 0
    if value.startswith(("UTC", "GMT")):
        value = value[3:]
    match = re.fullmatch(r"([+-])(\d{1,2})(?::?(\d{2}))?", value)
    if not match:
        return None
    hours, minutes = int(match.group(2)), int(match.group(3) or 0)
    if hours > 14 or minutes > 59:
        return None
    seconds = hours * 3600 + minutes * 60
    return -seconds if match.group(1) == "-" else seconds


def matches(profile: Dict, headers: Sequence[str]) -> bool:
    """Whether a saved profile reads this file as is (its mapped columns all present)."""
    return not problems(profile, headers)


def differences(profile: Dict, headers: Sequence[str]) -> Dict[str, List[str]]:
    """Mapped columns this file lacks, and columns it has that the profile does not mention."""
    known = {c["header"] for c in profile.get("columns", [])}
    available = list(headers)
    return {"missing": [c["header"] for c in mapped(profile) if c["header"] not in set(available)],
            "new": [h for h in available if h not in known]}


def suggest(headers: Sequence[str], units: Optional[Sequence[str]] = None) -> List[Dict]:
    """Column entries proposed from header aliases (and a units line if any).

    One column per single-use role (the first that matches); a Date column
    turns a matching *Time* header into *Time of day*. Unmatched headers are
    left out (they become extra channels when numeric).
    """
    units = list(units or [])
    taken: Dict[str, int] = {}
    entries: List[Dict] = []
    for index, header in enumerate(headers):
        key = R.match_role(header)
        if key is None:
            continue
        role = R.role(key)
        if not role.multiple and key in taken:
            continue
        taken[key] = index
        unit_text = units[index] if index < len(units) else ""
        unit = R.unit_from_text(key, unit_text) or R.unit_from_text(key, header)
        entries.append(column(header, key, unit=unit))
    keys = {e["role"] for e in entries}
    if R.TIME in keys and (R.DATE in keys or R.DAY in keys) and R.CLOCK not in keys:
        for entry in entries:
            if entry["role"] == R.TIME:
                entry["role"] = R.CLOCK
                entry.pop("format", None)
    return entries


def apply_suggestions(profile: Dict, headers: Sequence[str], units: Optional[Sequence[str]] = None) -> Dict:
    """A copy of ``profile`` with its columns replaced by :func:`suggest`'s proposal."""
    out = copy.deepcopy(normalise(profile))
    out["columns"] = suggest(headers, units)
    return out
