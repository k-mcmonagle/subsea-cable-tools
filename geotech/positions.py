# -*- coding: utf-8 -*-
"""Position and summary tables joined to CPT tests by name (pure python).

Offshore CPT deliveries often keep positions, water depth, penetration and
the interpreted layering in a spreadsheet beside the AGS files, one row per
CPT keyed by its name, instead of (or as well as) in the AGS location group.
This module reads such a table with the columns the user chose:

- ``id``: the CPT name (required);
- ``lat`` / ``lon`` (decimal degrees or degrees-minutes(-seconds) text such
  as ``52° 20.220' N``) or ``x`` / ``y`` (projected, the CRS is the caller's);
- ``water_depth``, ``penetration``, ``date``, ``time``, ``summary``;
- ``horizon_desc``, ``horizon_class``, ``horizon_interface``: the *first*
  column of a numbered family (``Horizon description 1`` …
  ``Horizon description 5``); its numbered siblings are found from the header.
  Horizon k runs from interface k-1 (the seabed for k = 1) to interface k
  (the penetration, if known, for the last).

Nothing is chosen from the column names here: callers pass the mapping, and
``suggest_mapping`` exists only for an explicit "suggest" button.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

ROLES = ("id", "lat", "lon", "x", "y", "water_depth", "penetration", "date", "time",
         "summary", "horizon_desc", "horizon_class", "horizon_interface")
ROLE_LABELS = {
    "id": "CPT name", "lat": "Latitude", "lon": "Longitude", "x": "Easting / X",
    "y": "Northing / Y", "water_depth": "Water depth", "penetration": "Penetration",
    "date": "Date", "time": "Time", "summary": "Summary",
    "horizon_desc": "Horizon descriptions (first column)",
    "horizon_class": "Horizon classes (first column)",
    "horizon_interface": "Interface depths (first column)",
}
FAMILY_ROLES = ("horizon_desc", "horizon_class", "horizon_interface")

_MISSING = {"", "-", "na", "n/a", "null", "none", "nan", "nodata"}
_COORD_SPLIT = re.compile(r"[°º˚:'′’\"″”\s]+")
_HEMISPHERE = re.compile(r"^\s*([NSEWnsew])\s*|\s*([NSEWnsew])\s*$")
_FAMILY = re.compile(r"^(.*?)(\d+)\s*$")


def is_missing(text) -> bool:
    return str(text if text is not None else "").strip().casefold() in _MISSING


def parse_number(text) -> Optional[float]:
    """A number with a decimal point; commas are not decimal separators."""
    value = str(text if text is not None else "").strip()
    if is_missing(value):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def parse_coordinate(text, axis: str = "") -> Optional[float]:
    """Decimal degrees from ``text``; raises ValueError when it is not one.

    Accepts decimal degrees (``-52.337``), degrees and decimal minutes
    (``52° 20.220' N``, ``N 52 20.220``, ``52:20.22S``) and degrees, minutes,
    seconds (``52°20'13.2"N``). ``axis`` ("lat"/"lon") rejects a hemisphere
    letter of the other axis and an out-of-range value. Blank gives None.
    """
    raw = str(text if text is not None else "").strip()
    if is_missing(raw):
        return None
    hemisphere = ""
    match = _HEMISPHERE.search(raw)
    if match:
        hemisphere = (match.group(1) or match.group(2)).upper()
        raw = (raw[:match.start()] + " " + raw[match.end():]).strip()
        if _HEMISPHERE.search(raw):
            raise ValueError(f"'{text}' has two hemisphere letters")
    sign = 1.0
    if raw.startswith(("-", "+")):
        sign = -1.0 if raw[0] == "-" else 1.0
        raw = raw[1:].strip()
    parts = [p for p in _COORD_SPLIT.split(raw) if p]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"'{text}' is not a coordinate")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"'{text}' is not a coordinate") from None
    if any(n < 0 or not math.isfinite(n) for n in numbers):
        raise ValueError(f"'{text}' is not a coordinate")
    if len(numbers) > 1 and (numbers[1] >= 60 or not float(numbers[0]).is_integer()):
        raise ValueError(f"'{text}': minutes must be below 60 after whole degrees")
    if len(numbers) > 2 and (numbers[2] >= 60 or not float(numbers[1]).is_integer()):
        raise ValueError(f"'{text}': seconds must be below 60 after whole minutes")
    value = numbers[0] + (numbers[1] / 60.0 if len(numbers) > 1 else 0.0) \
        + (numbers[2] / 3600.0 if len(numbers) > 2 else 0.0)
    if hemisphere:
        if sign < 0:
            raise ValueError(f"'{text}' has both a minus sign and a hemisphere")
        if axis == "lat" and hemisphere in "EW" or axis == "lon" and hemisphere in "NS":
            raise ValueError(f"'{text}' is a {'longitude' if hemisphere in 'EW' else 'latitude'}")
        if hemisphere in "SW":
            sign = -1.0
    value *= sign
    limit = 90.0 if axis == "lat" else 180.0 if axis == "lon" else 360.0
    if abs(value) > limit:
        raise ValueError(f"'{text}' is outside ±{limit:g}°")
    return value


# -- numbered column families -----------------------------------------------------
def family(headers: List[str], first: Optional[int]) -> List[int]:
    """Column indices of the numbered family that ``headers[first]`` starts.

    ``Horizon description 1`` → every ``Horizon description <n>``, ordered
    by n; ``Interface depth (m) 1`` → ``Interface depth (m) <n>``. A header without
    a trailing number is a family of one.
    """
    if first is None or not 0 <= first < len(headers):
        return []
    match = _FAMILY.match(str(headers[first]))
    if not match:
        return [first]
    stem = match.group(1).strip().casefold()
    found = []
    for index, header in enumerate(headers):
        other = _FAMILY.match(str(header))
        if other and other.group(1).strip().casefold() == stem:
            found.append((int(other.group(2)), index))
    found.sort()
    start = next((i for i, (_n, index) in enumerate(found) if index == first), 0)
    return [index for _n, index in found[start:]]


def horizons(descriptions: List[str], classes: List[str], interfaces: List[Optional[float]],
             penetration: Optional[float]) -> Tuple[List[Dict], List[str]]:
    """Layers from numbered description/class/interface cells.

    Returns ``(layers, problems)``; layers are ``{top, base, description,
    soil_class}`` with ``base`` None for an open last layer (penetration
    unknown). Empty trailing horizons are dropped.
    """
    count = max(len(descriptions), len(classes))
    rows = []
    for k in range(count):
        description = descriptions[k].strip() if k < len(descriptions) else ""
        soil_class = classes[k].strip() if k < len(classes) else ""
        rows.append((description, soil_class))
    while rows and not any(rows[-1]):
        rows.pop()
    problems: List[str] = []
    layers: List[Dict] = []
    top = 0.0
    for k, (description, soil_class) in enumerate(rows):
        if k < len(rows) - 1:
            base = interfaces[k] if k < len(interfaces) else None
            if base is None:
                problems.append(f"horizon {k + 1} has no interface depth below it")
                return layers, problems
        else:
            base = penetration
            extra = [v for v in interfaces[k:] if v is not None]
            if extra:
                if penetration is None or extra[0] < penetration - 1e-9:
                    base = extra[0]
        if base is not None and base <= top + 1e-9:
            problems.append(f"interface depths must increase (horizon {k + 1}: "
                            f"{top:g} to {base:g} m)")
            return layers, problems
        layers.append({"top": top, "base": base, "description": description,
                       "soil_class": soil_class})
        if base is None:
            break
        top = base
    return layers, problems


# -- the table ----------------------------------------------------------------------
def column_indices(headers: List[str], mapping: Dict[str, str]) -> Dict[str, int]:
    """``{role: index}`` from ``{role: header}``; an unknown header raises."""
    out = {}
    for role, name in (mapping or {}).items():
        if not name:
            continue
        if name not in headers:
            raise ValueError(f"The table has no column named '{name}' "
                             f"(the {ROLE_LABELS.get(role, role)} column).")
        if headers.count(name) > 1:
            raise ValueError(f"The table has {headers.count(name)} columns named '{name}'; "
                             "rename them so each column can be chosen.")
        out[role] = headers.index(name)
    return out


def read_table(grid: List[List[str]], header_row: int, mapping: Dict[str, str]
               ) -> Tuple[Dict[str, Dict], List[str]]:
    """``({name: record}, problems)`` from a sheet grid with the chosen columns.

    A record holds ``lat``/``lon`` or ``x``/``y`` (None when blank),
    ``water_depth``, ``penetration``, ``date``, ``time``, ``summary``,
    ``layers`` (see ``horizons``) and ``row`` (the 1-based sheet row).
    Rows whose coordinates cannot be read are kept without a position and
    reported; a repeated name is reported and the first row kept.
    """
    if header_row < 1 or header_row > len(grid):
        raise ValueError("The header row is beyond the end of the table.")
    headers = [str(h).strip() for h in grid[header_row - 1]]
    index = column_indices(headers, mapping)
    if "id" not in index:
        raise ValueError("Choose the column holding the CPT name.")
    if ("lat" in index) != ("lon" in index) or ("x" in index) != ("y" in index):
        raise ValueError("Choose both coordinate columns (latitude and longitude, "
                         "or easting and northing).")
    families = {role: family(headers, index.get(role)) for role in FAMILY_ROLES}
    records: Dict[str, Dict] = {}
    problems: List[str] = []

    def cell(row, role):
        i = index.get(role)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    for offset, row in enumerate(grid[header_row:], start=header_row + 1):
        if not any(str(v).strip() for v in row):
            continue
        name = cell(row, "id")
        if not name:
            continue
        if name in records:
            problems.append(f"row {offset}: {name} appears again (row {records[name]['row']} is used)")
            continue
        record: Dict = {"row": offset, "lat": None, "lon": None, "x": None, "y": None}
        try:
            if "lat" in index:
                record["lat"] = parse_coordinate(cell(row, "lat"), "lat")
                record["lon"] = parse_coordinate(cell(row, "lon"), "lon")
            if "x" in index:
                for role in ("x", "y"):
                    text = cell(row, role)
                    value = parse_number(text)
                    if value is None and not is_missing(text):
                        raise ValueError(f"'{text}' is not a number")
                    record[role] = value
        except ValueError as exc:
            problems.append(f"row {offset} ({name}): position not read — {exc}")
            record.update(lat=None, lon=None, x=None, y=None)
        if (record["lat"] is None) != (record["lon"] is None):
            problems.append(f"row {offset} ({name}): only one coordinate is given")
            record.update(lat=None, lon=None)
        if (record["x"] is None) != (record["y"] is None):
            problems.append(f"row {offset} ({name}): only one coordinate is given")
            record.update(x=None, y=None)
        for role in ("water_depth", "penetration"):
            text = cell(row, role)
            value = parse_number(text)
            if value is None and not is_missing(text):
                problems.append(f"row {offset} ({name}): {ROLE_LABELS[role].lower()} "
                                f"'{text}' is not a number")
            record[role] = abs(value) if value is not None and role == "water_depth" else value
        for role in ("date", "time", "summary"):
            record[role] = cell(row, role)
        descriptions = [str(row[i]).strip() if i < len(row) else ""
                        for i in families["horizon_desc"]]
        classes = [str(row[i]).strip() if i < len(row) else "" for i in families["horizon_class"]]
        interfaces = []
        for i in families["horizon_interface"]:
            text = str(row[i]).strip() if i < len(row) else ""
            value = parse_number(text)
            if value is None and not is_missing(text):
                problems.append(f"row {offset} ({name}): interface depth '{text}' is not a number")
            interfaces.append(value)
        layers, layer_problems = horizons(descriptions, classes, interfaces, record["penetration"])
        record["layers"] = layers
        problems.extend(f"row {offset} ({name}): {p}" for p in layer_problems)
        records[name] = record
    return records, problems


def suggest_mapping(headers: List[str]) -> Dict[str, str]:
    """Header names that look like each role (for an explicit Suggest button)."""
    patterns = (
        ("id", r"^(sample ?(number|no|id|name)|cpt ?(id|name|no)?|location ?id|loca_id|hole_id|"
               r"station|test ?(id|name)|name|id)$"),
        ("lat", r"^lat(itude)?\b"), ("lon", r"^(lon|long|longitude)\b"),
        ("x", r"^(easting|east|x)\b"), ("y", r"^(northing|north|y)\b"),
        ("water_depth", r"^(water ?depth|depth \(m\)|depth|wd)\b"),
        ("penetration", r"^(penetration|final ?depth|pen(etration)? depth)\b"),
        ("date", r"^(sample ?)?date$"), ("time", r"^(sample ?)?time$"),
        ("summary", r"^(summary|description|remarks?)$"),
        ("horizon_desc", r"^horizon ?desc(ription)? ?\(?.*\)? ?0*1$"),
        ("horizon_class", r"^(classification|class) ?0*1$"),
        ("horizon_interface", r"^interface ?depth.*\b0*1$"),
    )
    out: Dict[str, str] = {}
    used = set()
    for role, pattern in patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        for header in headers:
            text = str(header).strip()
            if text and text not in used and regex.search(text):
                out[role] = text
                used.add(text)
                break
    return out


# -- names --------------------------------------------------------------------------
def compact(text) -> str:
    """Name comparison key ignoring case, spaces and punctuation."""
    return re.sub(r"[^0-9a-z]", "", str(text).casefold())


def match_names(names: List[str], keys: List[str], loose: bool = True
                ) -> Tuple[Dict[str, Tuple[str, str]], List[str], List[str]]:
    """Pair CPT names with table keys.

    Returns ``(matches, unmatched_names, unused_keys)``; ``matches`` maps a
    name to ``(key, "exact" | "loose")``. Loose matching ignores case,
    spaces and punctuation and is used only when it is unambiguous.
    """
    key_set = set(keys)
    folded: Dict[str, List[str]] = {}
    for key in keys:
        folded.setdefault(compact(key), []).append(key)
    matches: Dict[str, Tuple[str, str]] = {}
    for name in names:
        if name in key_set:
            matches[name] = (name, "exact")
        elif loose:
            candidates = folded.get(compact(name), [])
            if len(candidates) == 1:
                matches[name] = (candidates[0], "loose")
    used = {key for key, _how in matches.values()}
    unmatched = [n for n in names if n not in matches]
    unused = [k for k in keys if k not in used]
    return matches, unmatched, unused
