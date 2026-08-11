# -*- coding: utf-8 -*-
"""Coordinate parsing for RPL import (pure Python).

Supported encodings:

- split DDM: separate degrees / decimal-minutes / hemisphere cells;
- DDM text: one combined string per axis (``50° 12.345' N``, ``N 50 12.345``,
  ``50-12.345N`` …);
- signed decimal degrees (numeric or text, optional hemisphere suffix).

Every parser returns ``(value, reason)`` where exactly one side is ``None``:
``value`` is the signed decimal-degrees float, ``reason`` a short stable
failure code for diagnostics. Projected coordinates are *not* handled here —
the QGIS layer transforms easting/northing via QgsCoordinateTransform.

Parsers reject rather than guess: minutes >= 60, impossible magnitudes,
hemisphere letters that contradict the axis, and ambiguous strings all fail
with a reason instead of producing plausible garbage.
"""

from __future__ import annotations

import math
import re
from typing import Optional, Tuple

AXIS_LAT = "lat"
AXIS_LON = "lon"

_HEMI_SIGNS = {"N": 1.0, "S": -1.0, "E": 1.0, "W": -1.0}
_AXIS_HEMIS = {AXIS_LAT: ("N", "S"), AXIS_LON: ("E", "W")}
_AXIS_MAX = {AXIS_LAT: 90.0, AXIS_LON: 180.0}

ParseResult = Tuple[Optional[float], Optional[str]]

# reasons (stable, used in diagnostics)
R_EMPTY = "empty"
R_NOT_NUMERIC = "not_numeric"
R_MINUTES_RANGE = "minutes_out_of_range"
R_BAD_HEMISPHERE = "bad_hemisphere"
R_WRONG_AXIS_HEMISPHERE = "wrong_axis_hemisphere"
R_OUT_OF_RANGE = "out_of_range"
R_UNRECOGNISED = "unrecognised_format"


#: Unit symbols tolerated after a bare number in a split deg/min cell
#: (``9°``, ``36.1115'``); full DDM strings still fail _to_float and are
#: handled by the text parsers.
_UNIT_SUFFIXES = "°º'′\"″"


def _to_float(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = str(value).strip().replace(",", ".")
        text = text.rstrip(_UNIT_SUFFIXES).strip()
        if not text:
            return None
        try:
            result = float(text)
        except ValueError:
            return None
    return result if math.isfinite(result) else None


def _hemi_letter(value) -> Optional[str]:
    text = str(value or "").strip().upper()
    if not text:
        return None
    letter = text[0]
    return letter if letter in _HEMI_SIGNS else None


def parse_split_ddm(deg, minutes, hemi, axis: str) -> ParseResult:
    """Degrees + decimal minutes + hemisphere from three separate cells."""
    if deg is None and minutes is None and not str(hemi or "").strip():
        return None, R_EMPTY
    deg_f = _to_float(deg)
    min_f = _to_float(minutes)
    if deg_f is None or min_f is None:
        return None, R_NOT_NUMERIC
    letter = _hemi_letter(hemi)
    if letter is None:
        return None, R_BAD_HEMISPHERE
    if letter not in _AXIS_HEMIS[axis]:
        return None, R_WRONG_AXIS_HEMISPHERE
    if not (0.0 <= abs(min_f) < 60.0):
        return None, R_MINUTES_RANGE
    value = abs(deg_f) + abs(min_f) / 60.0
    if value > _AXIS_MAX[axis]:
        return None, R_OUT_OF_RANGE
    return value * _HEMI_SIGNS[letter], None


# ``50° 12.345' N`` / ``50 12.345N`` / ``N50-12.345`` / ``50d12.345m N``
_DDM_TEXT = re.compile(
    r"""^\s*
        (?P<hemi1>[NSEW])?\s*
        (?P<deg>\d{1,3})\s*[°d:\-\s]\s*
        (?P<min>\d{1,2}(?:[.,]\d+)?)\s*['m′]?\s*
        (?P<hemi2>[NSEW])?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# DMS variant kept deliberately strict: deg, min, sec all present.
_DMS_TEXT = re.compile(
    r"""^\s*
        (?P<hemi1>[NSEW])?\s*
        (?P<deg>\d{1,3})\s*[°d:\-\s]\s*
        (?P<min>\d{1,2})\s*['m′:\-\s]\s*
        (?P<sec>\d{1,2}(?:[.,]\d+)?)\s*["s″]?\s*
        (?P<hemi2>[NSEW])?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def parse_ddm_text(value, axis: str) -> ParseResult:
    """Combined DDM (or strict DMS) text for one axis."""
    if value is None:
        return None, R_EMPTY
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A bare number in a "DDM text" column is ambiguous; treat as decimal
        # degrees only if it is a plausible signed value for the axis.
        return parse_decimal_degrees(value, axis)
    text = str(value).strip()
    if not text:
        return None, R_EMPTY

    match = _DMS_TEXT.match(text)
    seconds = 0.0
    if match:
        seconds = float((match.group("sec") or "0").replace(",", "."))
    else:
        match = _DDM_TEXT.match(text)
    if not match:
        return None, R_UNRECOGNISED

    hemi = match.group("hemi1") or match.group("hemi2")
    if match.group("hemi1") and match.group("hemi2"):
        return None, R_UNRECOGNISED
    letter = _hemi_letter(hemi)
    if letter is None:
        return None, R_BAD_HEMISPHERE
    if letter not in _AXIS_HEMIS[axis]:
        return None, R_WRONG_AXIS_HEMISPHERE

    minutes = float(match.group("min").replace(",", "."))
    if minutes >= 60.0 or seconds >= 60.0:
        return None, R_MINUTES_RANGE
    value_dd = float(match.group("deg")) + minutes / 60.0 + seconds / 3600.0
    if value_dd > _AXIS_MAX[axis]:
        return None, R_OUT_OF_RANGE
    return value_dd * _HEMI_SIGNS[letter], None


_DD_TEXT = re.compile(
    r"^\s*(?P<sign>[+-])?\s*(?P<num>\d{1,3}(?:[.,]\d+)?)\s*(?:°)?\s*(?P<hemi>[NSEW])?\s*$",
    re.IGNORECASE,
)


def parse_decimal_degrees(value, axis: str) -> ParseResult:
    """Signed decimal degrees; optional trailing hemisphere letter."""
    if value is None:
        return None, R_EMPTY
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        dd = float(value)
        if not math.isfinite(dd):
            return None, R_NOT_NUMERIC
        if abs(dd) > _AXIS_MAX[axis]:
            return None, R_OUT_OF_RANGE
        return dd, None
    text = str(value).strip()
    if not text:
        return None, R_EMPTY
    match = _DD_TEXT.match(text)
    if not match:
        return None, R_UNRECOGNISED
    dd = float(match.group("num").replace(",", "."))
    hemi = match.group("hemi")
    if hemi:
        letter = _hemi_letter(hemi)
        if letter not in _AXIS_HEMIS[axis]:
            return None, R_WRONG_AXIS_HEMISPHERE
        if match.group("sign") == "-":
            return None, R_UNRECOGNISED  # "-50.1 N" is contradictory
        dd *= _HEMI_SIGNS[letter]
    elif match.group("sign") == "-":
        dd = -dd
    if abs(dd) > _AXIS_MAX[axis]:
        return None, R_OUT_OF_RANGE
    return dd, None


# ---------------------------------------------------------------------------
# Geodesy (advisory only — QGIS-side validation injects ellipsoidal measures)
# ---------------------------------------------------------------------------
EARTH_RADIUS_M = 6_371_008.8


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Mean-sphere great-circle distance; adequate for sanity tolerances."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a))) / 1000.0


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def circular_diff_deg(a: float, b: float) -> float:
    """Smallest absolute angular difference in degrees."""
    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)
