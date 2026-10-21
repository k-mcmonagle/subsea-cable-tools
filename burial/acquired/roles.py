# -*- coding: utf-8 -*-
"""Column roles for acquired burial data: what a delivered column means.

A role fixes the stored (canonical) units, so every dataset reads alike:

* KP in km; DOL, water depth and DCC in m (DOL and water depth positive
  down; DCC positive to starboard looking towards increasing KP);
* time as UTC seconds since 1970; pitch, roll and heading in degrees;
  speed in m/s; tension in kN; positions as delivered (degrees, or metres
  in the stated CRS).

Each unit choice carries the factor to the canonical unit. *Tension* and
*Extra channel* may be given to several columns, each with its own name;
every other role to one column only. Aliases are matched against headers
normalised by :func:`normalise_header` (lower case, letters and digits).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

GROUP_NEEDED = "Needed"
GROUP_TIME = "Time"
GROUP_POSITION = "Position"
GROUP_CHANNELS = "Channels"
GROUP_TEXT = "Text"
GROUP_OTHER = "Other"
GROUPS = (GROUP_NEEDED, GROUP_TIME, GROUP_POSITION, GROUP_CHANNELS, GROUP_TEXT, GROUP_OTHER)

KIND_NUMBER = "number"
KIND_TIME = "time"          # a whole timestamp in one column
KIND_DATE = "date"          # calendar date (with a time-of-day column)
KIND_CLOCK = "clock"        # time of day (with a date or day-number column)
KIND_DAY = "day"            # day number counted from a start date
KIND_COORD = "coordinate"   # latitude / longitude: decimal or degrees-minutes
KIND_TEXT = "text"
KIND_IGNORE = "ignore"

SIGN_DEPTH = "depth"        # positive down (stored) or up (negated)
SIGN_SIDE = "side"          # + starboard (stored), + port (negated) or unsigned

# Roles
KP, DOL = "kp", "dol"
TIME, DATE, CLOCK, DAY = "time", "date", "clock", "day"
LAT, LON, EASTING, NORTHING, DCC = "lat", "lon", "e", "n", "dcc"
WATER_DEPTH, PITCH, ROLL, HEADING, SPEED, TENSION = (
    "water_depth", "pitch", "roll", "heading", "speed", "tension")
EVENT, PASS = "event", "pass"
EXTRA, IGNORE = "extra", "ignore"


@dataclass(frozen=True)
class Unit:
    key: str
    label: str
    factor: float           # value x factor = canonical units


@dataclass(frozen=True)
class Role:
    key: str
    label: str
    group: str
    kind: str
    units: Tuple[Unit, ...] = ()        # first = default; empty = none / as delivered
    multiple: bool = False
    aliases: Tuple[str, ...] = ()
    sign: Optional[str] = None
    tip: str = ""

    @property
    def default_unit(self) -> str:
        return self.units[0].key if self.units else ""

    def unit(self, key: str) -> Optional[Unit]:
        for unit in self.units:
            if unit.key == key:
                return unit
        return None


_LENGTH = (Unit("m", "m", 1.0), Unit("cm", "cm", 0.01), Unit("mm", "mm", 0.001), Unit("ft", "ft", 0.3048))
_ANGLE = (Unit("deg", "°", 1.0), Unit("rad", "rad", 180.0 / math.pi))

ROLES: Tuple[Role, ...] = (
    Role(KP, "KP", GROUP_NEEDED, KIND_NUMBER, (Unit("km", "km", 1.0), Unit("m", "m", 0.001)),
         aliases=("kp", "kpkm", "kpm", "designkp", "rplkp", "kilometrepost", "kilometerpost", "chainage",
                  "chainagekm", "chainagem", "kilometrage"),
         tip="Kilometre post along the RPL the dataset refers to."),
    Role(DOL, "DOL", GROUP_NEEDED, KIND_NUMBER, _LENGTH,
         aliases=("dol", "dolm", "depthoflowering", "burialdepth", "burial", "dob", "depthofburial",
                  "cableburial", "lowering", "buried", "burialdepthm"),
         sign=SIGN_DEPTH,
         tip="Depth of lowering: top of the cable below mean seabed level, positive down."),
    Role(TIME, "Timestamp", GROUP_TIME, KIND_TIME,
         aliases=("time", "datetime", "timestamp", "datetimeutc", "utc", "utctime", "isotime", "gmt",
                  "timeutc", "loggedtime", "recordtime"),
         tip="Date and time in one column. Without it, give the acquisition date on import."),
    Role(DATE, "Date", GROUP_TIME, KIND_DATE, aliases=("date", "utcdate", "surveydate", "dateutc"),
         tip="Calendar date, with the time of day in another column."),
    Role(CLOCK, "Time of day", GROUP_TIME, KIND_CLOCK,
         aliases=("timeofday", "clock", "hhmmss", "utctimeofday", "tod"),
         tip="Time of day, with a Date or Day column."),
    Role(DAY, "Day number", GROUP_TIME, KIND_DAY, aliases=("day", "dayno", "daynumber", "projectday"),
         tip="Day number counted from a start date (day 1 = the start date), with a Time of day column."),
    Role(LAT, "Latitude", GROUP_POSITION, KIND_COORD,
         aliases=("lat", "latitude", "latdd", "latdeg", "latwgs84", "ylat"),
         tip="Decimal degrees or degrees-minutes(-seconds) with N/S, on WGS 84."),
    Role(LON, "Longitude", GROUP_POSITION, KIND_COORD,
         aliases=("lon", "long", "longitude", "londd", "londeg", "lng", "lonwgs84", "xlon"),
         tip="Decimal degrees or degrees-minutes(-seconds) with E/W, on WGS 84."),
    Role(EASTING, "Easting", GROUP_POSITION, KIND_NUMBER,
         aliases=("e", "easting", "east", "x", "eastingm", "eastings"),
         tip="Projected X in the chosen CRS."),
    Role(NORTHING, "Northing", GROUP_POSITION, KIND_NUMBER,
         aliases=("n", "northing", "north", "y", "northingm", "northings"),
         tip="Projected Y in the chosen CRS."),
    Role(DCC, "DCC", GROUP_POSITION, KIND_NUMBER, _LENGTH,
         aliases=("dcc", "dccm", "distancecrosscourse", "crosscourse", "crosstrack", "xtrack", "offline",
                  "offset", "lateraloffset", "acrosstrack"),
         sign=SIGN_SIDE,
         tip="Distance cross course from the RPL. Stored positive to starboard, looking up KP."),
    Role(WATER_DEPTH, "Water depth", GROUP_CHANNELS, KIND_NUMBER, _LENGTH,
         aliases=("waterdepth", "wd", "wdm", "depth", "seabeddepth", "seabed", "sbd", "bathy", "bathymetry",
                  "depthm", "msbl"),
         sign=SIGN_DEPTH, tip="Seabed depth below the vertical datum, positive down."),
    Role(PITCH, "Pitch", GROUP_CHANNELS, KIND_NUMBER, _ANGLE, aliases=("pitch", "pitchdeg"),
         tip="Vehicle pitch."),
    Role(ROLL, "Roll", GROUP_CHANNELS, KIND_NUMBER, _ANGLE, aliases=("roll", "rolldeg"), tip="Vehicle roll."),
    Role(HEADING, "Heading", GROUP_CHANNELS, KIND_NUMBER, _ANGLE,
         aliases=("heading", "hdg", "gyro", "course", "headingdeg", "cog"), tip="Vehicle heading."),
    Role(SPEED, "Speed", GROUP_CHANNELS, KIND_NUMBER,
         (Unit("m/s", "m/s", 1.0), Unit("kn", "knots", 1852.0 / 3600.0), Unit("m/min", "m/min", 1.0 / 60.0),
          Unit("km/h", "km/h", 1.0 / 3.6), Unit("m/h", "m/h", 1.0 / 3600.0)),
         aliases=("speed", "spd", "sog", "velocity", "towspeed", "speedms", "speedkn", "speedkts", "progress"),
         tip="Speed over ground."),
    Role(TENSION, "Tension", GROUP_CHANNELS, KIND_NUMBER,
         (Unit("kN", "kN", 1.0), Unit("t", "t", 9.80665), Unit("kgf", "kgf", 0.00980665),
          Unit("lbf", "lbf", 0.0044482216152605), Unit("N", "N", 0.001)),
         multiple=True,
         aliases=("tension", "towtension", "cabletension", "winchtension", "towforce", "bollardpull",
                  "tensionkn", "towwire", "tow"),
         tip="A tension, named per column (e.g. Tow, Cable)."),
    Role(EVENT, "Event / comment", GROUP_TEXT, KIND_TEXT,
         aliases=("event", "events", "comment", "comments", "remark", "remarks", "note", "notes", "description",
                  "eventtext", "eventdescription"),
         tip="Event or comment text, shown on the profile."),
    Role(PASS, "Pass / run", GROUP_TEXT, KIND_TEXT,
         aliases=("pass", "passno", "passid", "run", "runno", "runid", "linename", "line"),
         tip="Pass or run label, kept with each record."),
    Role(EXTRA, "Extra channel", GROUP_OTHER, KIND_NUMBER, multiple=True,
         tip="Any other numeric column, kept under the name and unit you give it."),
    Role(IGNORE, "Ignore", GROUP_OTHER, KIND_IGNORE, multiple=True, tip="Not imported (the original keeps it)."),
)

_BY_KEY: Dict[str, Role] = {role.key: role for role in ROLES}
REQUIRED = (KP, DOL)
POSITION_PAIRS = ((LAT, LON), (EASTING, NORTHING))


def role(key: str) -> Role:
    """The role for ``key`` (KeyError for an unknown role)."""
    return _BY_KEY[key]


def find(key: str) -> Optional[Role]:
    return _BY_KEY.get(key)


def normalise_header(text: str) -> str:
    """Lower-case letters and digits only: ``"Tow Tension (kN)"`` -> ``"towtensionkn"``."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def slug(text: str) -> str:
    """A channel-key fragment: lower case, words joined by ``_``."""
    value = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return value or "unnamed"


def channel_key(role_key: str, name: str = "") -> str:
    """The stored channel key: the role, or ``role:name`` for multiple roles."""
    found = find(role_key)
    if found is not None and found.multiple:
        return f"{role_key}:{slug(name)}"
    return role_key


def channel_label(role_key: str, name: str = "") -> str:
    found = find(role_key)
    if found is None:
        return name or role_key
    if role_key == TENSION:
        return f"{name} tension" if name and normalise_header(name) != "tension" else "Tension"
    if role_key == EXTRA:
        return name or "Extra"
    return found.label


_UNIT_WORDS: Dict[str, Tuple[str, ...]] = {
    # unit key -> normalised spellings found in headers or a units line
    "km": ("km", "kms", "kilometre", "kilometres", "kilometer", "kilometers"),
    "m": ("m", "metre", "metres", "meter", "meters", "mtr"),
    "cm": ("cm",),
    "mm": ("mm",),
    "ft": ("ft", "feet", "foot"),
    "deg": ("deg", "degree", "degrees", "dd"),
    "rad": ("rad", "radian", "radians"),
    "m/s": ("ms", "mps", "msec"),
    "kn": ("kn", "kt", "kts", "knot", "knots"),
    "m/min": ("mmin", "mpm"),
    "km/h": ("kmh", "kph"),
    "m/h": ("mh", "mhr"),
    "kN": ("kn",),
    "t": ("t", "te", "tonne", "tonnes", "ton", "tons"),
    "kgf": ("kgf", "kg"),
    "lbf": ("lbf", "lbs", "lb"),
    "N": ("n", "newton", "newtons"),
}


def unit_from_text(role_key: str, text: str) -> Optional[str]:
    """The unit of ``role_key`` named in ``text`` (a units-line cell or a header
    suffix such as ``"KP (m)"`` / ``"Speed_kn"``), or None."""
    found = find(role_key)
    if found is None or not found.units or not text:
        return None
    raw = str(text).strip()
    candidates = [raw]
    bracket = re.search(r"[\[(]([^\])]+)[\])]", raw)
    if bracket:
        candidates.insert(0, bracket.group(1))
    tail = re.split(r"[\s_\-]+", raw)
    if len(tail) > 1:
        candidates.append(tail[-1])
    keys = {unit.key for unit in found.units}
    for candidate in candidates:
        word = normalise_header(candidate)
        if candidate.strip() in ("°", "º"):
            word = "deg"
        if not word:
            continue
        for key, spellings in _UNIT_WORDS.items():
            if key in keys and word in spellings:
                # "kn" is a speed in knots but a tension in kilonewtons.
                if word == "kn":
                    return "kn" if role_key == SPEED else ("kN" if role_key == TENSION else None)
                return key
    return None


def _strip_unit_suffix(header: str) -> str:
    """``"KP (km)"`` -> ``"KP"``; ``"Tension_t"`` -> ``"Tension"`` (for alias matching)."""
    text = re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", str(header or "")).strip()
    parts = re.split(r"[\s_\-]+", text)
    if len(parts) > 1:
        words = {w for spellings in _UNIT_WORDS.values() for w in spellings}
        if normalise_header(parts[-1]) in words:
            text = " ".join(parts[:-1])
    return text


def match_role(header: str) -> Optional[str]:
    """The role a header suggests (exact alias match after normalising), or None."""
    for text in (header, _strip_unit_suffix(header)):
        key = normalise_header(text)
        if not key:
            continue
        for candidate in ROLES:
            if key in candidate.aliases:
                return candidate.key
    return None


def tension_name(header: str) -> str:
    """A tension column's name from its header: ``"TowTension (kN)"`` -> ``"Tow"``."""
    text = _strip_unit_suffix(header)
    text = re.sub(r"(?i)[\s_\-]*tension[\s_\-]*", " ", text).strip()
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text).strip()
    return text.title() if text else "Tension"


def sign_choices(role_key: str) -> List[Tuple[str, str]]:
    """``[(key, label)]`` sign conventions a role can be delivered in (stored one first)."""
    found = find(role_key)
    if found is None or found.sign is None:
        return []
    if found.sign == SIGN_DEPTH:
        return [("down", "positive down"), ("up", "positive up")]
    return [("starboard", "+ starboard"), ("port", "+ port"), ("unsigned", "unsigned")]


def sign_factor(role_key: str, sign: str) -> float:
    """Multiplier that brings a delivered sign convention to the stored one."""
    found = find(role_key)
    if found is None or found.sign is None:
        return 1.0
    return -1.0 if sign in ("up", "port") else 1.0


def roles_in_group(group: str) -> List[Role]:
    return [r for r in ROLES if r.group == group]


def time_roles_complete(keys: Sequence[str]) -> bool:
    """Whether the mapped time roles give a full timestamp."""
    present = set(keys)
    return TIME in present or (CLOCK in present and (DATE in present or DAY in present))
