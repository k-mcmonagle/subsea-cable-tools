# -*- coding: utf-8 -*-
"""Neutral RPL import model, import profile, and diagnostics.

The parser produces an :class:`ImportedRpl` — an ordered point/segment model
mirroring ``workbench.rpl_engine.RplModel`` semantics (point ``seq`` is
zero-based order, segment ``seq = i`` joins point ``i`` to ``i + 1``) but with
source-row provenance and *stated* engineering values kept distinct from
anything later computed by QGIS.

Conventions
-----------
- Absent values are ``None`` — never ``-1``, ``0`` or ``""`` sentinels.
- ``PosNo`` is document identity: preserved verbatim, never invented or
  renumbered. Non-integer document numbers keep their text in
  ``pos_no_raw`` with ``pos_no`` left ``None``.
- ``chart_no`` is text (chart references can be alphanumeric); the commit
  layer decides how to fit it into the canonical integer column without
  losing the original.
- Canonical units after parsing: kilometres (route/cable distances), metres
  (depth/burial), degrees (bearing), percent (slack). Source units are
  recorded on the profile for audit.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

#: Bump when parser/detection behaviour changes in a way that could alter
#: imported values; stored in the commit audit for traceability.
PARSER_VERSION = "1.0.0"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

LAYOUT_ALTERNATING = "alternating"
LAYOUT_FLAT = "flat"

#: Flat-layout segment semantics: do segment fields on a position row describe
#: the span *arriving at* this position (joins previous point) or *departing
#: from* it (joins next point)?
FLAT_ARRIVING = "arriving"
FLAT_DEPARTING = "departing"

COORD_SPLIT_DDM = "split_ddm"          # separate deg / decimal-minutes / hemisphere columns
COORD_DDM_TEXT = "ddm_text"            # one combined text column per axis
COORD_DECIMAL_DEGREES = "decimal_degrees"
COORD_PROJECTED = "projected"          # easting/northing + user-selected CRS

DISTANCE_UNITS = ("km", "m", "nm")
DEPTH_UNITS = ("m", "ft")

#: Point-level mapping field keys.
PF_POS_NO = "pos_no"
PF_EVENT = "event"
PF_LAT_DEG = "lat_deg"
PF_LAT_MIN = "lat_min"
PF_LAT_HEMI = "lat_hemi"
PF_LON_DEG = "lon_deg"
PF_LON_MIN = "lon_min"
PF_LON_HEMI = "lon_hemi"
PF_LAT_TEXT = "lat_text"               # DDM text / decimal degrees
PF_LON_TEXT = "lon_text"
PF_EASTING = "easting"
PF_NORTHING = "northing"
PF_DIST_CUM = "dist_cum"
PF_CABLE_DIST_CUM = "cable_dist_cum"
PF_DEPTH = "depth"
PF_REMARKS = "remarks"
PF_CHART_NO = "chart_no"

#: Segment-level mapping field keys.
SF_BEARING = "bearing"
SF_DIST = "dist"
SF_SLACK = "slack"
SF_CABLE_DIST = "cable_dist"
SF_CABLE_CODE = "cable_code"
SF_FIBER_PAIR = "fiber_pair"
SF_CABLE_TYPE = "cable_type"
SF_LAY_DIRECTION = "lay_direction"
SF_LAY_VESSEL = "lay_vessel"
SF_PROTECTION = "protection_method"
SF_DATE_INSTALLED = "date_installed"
SF_TARGET_BURIAL = "target_burial_depth"
SF_BURIAL = "burial_depth"
SF_TERRITORIAL = "territorial_water"
SF_EEZ = "eez"

POINT_FIELDS = (
    PF_POS_NO, PF_EVENT,
    PF_LAT_DEG, PF_LAT_MIN, PF_LAT_HEMI,
    PF_LON_DEG, PF_LON_MIN, PF_LON_HEMI,
    PF_LAT_TEXT, PF_LON_TEXT, PF_EASTING, PF_NORTHING,
    PF_DIST_CUM, PF_CABLE_DIST_CUM, PF_DEPTH, PF_REMARKS, PF_CHART_NO,
)
SEGMENT_FIELDS = (
    SF_BEARING, SF_DIST, SF_SLACK, SF_CABLE_DIST, SF_CABLE_CODE,
    SF_FIBER_PAIR, SF_CABLE_TYPE, SF_LAY_DIRECTION, SF_LAY_VESSEL,
    SF_PROTECTION, SF_DATE_INSTALLED, SF_TARGET_BURIAL, SF_BURIAL,
    SF_TERRITORIAL, SF_EEZ,
)
ALL_FIELDS = POINT_FIELDS + SEGMENT_FIELDS

#: Coordinate fields required per encoding.
REQUIRED_COORD_FIELDS = {
    COORD_SPLIT_DDM: (PF_LAT_DEG, PF_LAT_MIN, PF_LAT_HEMI,
                      PF_LON_DEG, PF_LON_MIN, PF_LON_HEMI),
    COORD_DDM_TEXT: (PF_LAT_TEXT, PF_LON_TEXT),
    COORD_DECIMAL_DEGREES: (PF_LAT_TEXT, PF_LON_TEXT),
    COORD_PROJECTED: (PF_EASTING, PF_NORTHING),
}


@dataclass
class Diagnostic:
    """One import finding with a stable rule ID and source context."""
    rule_id: str
    severity: str
    message: str
    sheet: str = ""
    row: Optional[int] = None       # 1-based source row
    column: Optional[int] = None    # 1-based source column
    field: str = ""                 # mapping field key, when applicable
    suggestion: str = ""

    def to_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


def has_errors(diagnostics: List[Diagnostic]) -> bool:
    return any(d.severity == SEVERITY_ERROR for d in diagnostics)


def split_diagnostics(diagnostics: List[Diagnostic]):
    """(errors, warnings, infos) in stable order."""
    errors = [d for d in diagnostics if d.severity == SEVERITY_ERROR]
    warnings = [d for d in diagnostics if d.severity == SEVERITY_WARNING]
    infos = [d for d in diagnostics if d.severity == SEVERITY_INFO]
    return errors, warnings, infos


@dataclass
class ImportPoint:
    """One RPL position with stated (source) values only."""
    seq: int
    source_row: Optional[int] = None       # 1-based worksheet row
    pos_no: Optional[int] = None
    pos_no_raw: str = ""                   # original text when non-integer
    event: str = ""
    lat: Optional[float] = None            # WGS84 after any transform
    lon: Optional[float] = None
    dist_cum_km: Optional[float] = None
    cable_dist_cum_km: Optional[float] = None
    depth_m: Optional[float] = None
    remarks: str = ""
    chart_no: str = ""
    extras: Dict[str, object] = field(default_factory=dict)


@dataclass
class ImportSegment:
    """The span joining point ``seq`` to point ``seq + 1`` (stated values)."""
    seq: int
    source_row: Optional[int] = None
    bearing_deg: Optional[float] = None
    dist_km: Optional[float] = None
    slack_pct: Optional[float] = None
    cable_dist_km: Optional[float] = None
    cable_code: str = ""
    fiber_pair: str = ""
    cable_type: str = ""
    lay_direction: str = ""
    lay_vessel: str = ""
    protection_method: str = ""
    date_installed: str = ""
    target_burial_depth_m: Optional[float] = None
    burial_depth_m: Optional[float] = None
    territorial_water: str = ""
    eez: str = ""
    extras: Dict[str, object] = field(default_factory=dict)


@dataclass
class ImportedRpl:
    """Ordered neutral model: ``len(segments) == len(points) - 1`` when valid.

    The parser may return an inconsistent pairing together with blocking
    diagnostics; consumers must check :func:`has_errors` before committing.
    """
    sheet: str = ""
    points: List[ImportPoint] = field(default_factory=list)
    segments: List[ImportSegment] = field(default_factory=list)

    def start_kp_km(self) -> Optional[float]:
        return self.points[0].dist_cum_km if self.points else None

    def end_kp_km(self) -> Optional[float]:
        return self.points[-1].dist_cum_km if self.points else None

    def stated_route_km(self) -> Optional[float]:
        if (self.points and self.points[0].dist_cum_km is not None
                and self.points[-1].dist_cum_km is not None):
            return self.points[-1].dist_cum_km - self.points[0].dist_cum_km
        return None

    def stated_cable_km(self) -> Optional[float]:
        if (self.points and self.points[0].cable_dist_cum_km is not None
                and self.points[-1].cable_dist_cum_km is not None):
            return self.points[-1].cable_dist_cum_km - self.points[0].cable_dist_cum_km
        return None


@dataclass
class ImportProfile:
    """Everything needed to repeat one import deterministically.

    ``mapping`` maps field keys (``PF_*`` / ``SF_*``) to 1-based column
    indices. Columns not mapped and not listed in ``excluded_columns`` are
    preserved as extra attributes.
    """
    sheet: str = ""
    data_start_row: int = 0                # 1-based, inclusive
    data_end_row: int = 0                  # 1-based, inclusive
    header_rows: List[int] = field(default_factory=list)
    layout: str = LAYOUT_ALTERNATING
    flat_semantics: str = FLAT_ARRIVING
    coord_encoding: str = COORD_SPLIT_DDM
    source_crs: str = "EPSG:4326"          # user-configurable source CRS
    mapping: Dict[str, int] = field(default_factory=dict)
    excluded_columns: List[int] = field(default_factory=list)
    distance_unit: str = "km"              # DistBetweenPos / cumulative KP
    cable_distance_unit: str = "km"
    depth_unit: str = "m"
    burial_unit: str = "m"
    slack_is_ratio: bool = False           # True: source slack is 1.02-style ratio
    header_signature: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ImportProfile":
        data = json.loads(text)
        profile = cls()
        for key, value in data.items():
            if hasattr(profile, key):
                setattr(profile, key, value)
        # JSON dict keys stay str; column indices must be ints
        profile.mapping = {k: int(v) for k, v in (profile.mapping or {}).items()}
        profile.excluded_columns = [int(v) for v in (profile.excluded_columns or [])]
        return profile

    def required_missing(self) -> List[str]:
        """Coordinate fields the current encoding requires but aren't mapped."""
        required = REQUIRED_COORD_FIELDS.get(self.coord_encoding, ())
        return [f for f in required if not self.mapping.get(f)]

    def duplicate_assignments(self) -> Dict[int, List[str]]:
        """Columns assigned to more than one typed field."""
        by_col: Dict[int, List[str]] = {}
        for field_key, col in self.mapping.items():
            if col:
                by_col.setdefault(col, []).append(field_key)
        return {col: fields for col, fields in by_col.items() if len(fields) > 1}


# ---------------------------------------------------------------------------
# Header signatures & extra-column names
# ---------------------------------------------------------------------------
def normalise_header_text(text: object) -> str:
    """Lower-case, collapse whitespace/punctuation — stable mapping key."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def header_signature(header_texts: List[str]) -> str:
    """Stable signature of a header layout, for remembered mapping profiles."""
    joined = "|".join(normalise_header_text(t) for t in header_texts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


#: Canonical Workbench layer field names extras must never collide with.
RESERVED_FIELD_NAMES = {
    "fid", "rpl_id", "seqno", "posno", "event", "distcumulative",
    "cabledistcumulative", "approxdepth", "remarks", "chartno", "latitude",
    "longitude", "sourcefile", "frompos", "topos", "bearing",
    "distbetweenpos", "slack", "cabledistbetweenpos", "cablecode",
    "fiberpair", "cabletype", "laydirection", "layvessel",
    "protectionmethod", "dateinstalled", "targetburialdepth", "burialdepth",
    "territorialwater", "eez",
}


def extra_field_name(header_text: str, column: int, taken: set) -> str:
    """Deterministic, collision-safe attribute name for an extra column.

    ``taken`` is the (lower-cased) set of names already in use; the chosen
    name is added to it before returning.
    """
    base = re.sub(r"[^A-Za-z0-9]+", "_", str(header_text or "").strip()).strip("_")
    if not base:
        base = f"col_{column}"
    base = ("x_" + base) if base[0].isdigit() else base
    base = base[:40]
    candidate = base
    index = 2
    while candidate.lower() in taken or candidate.lower() in RESERVED_FIELD_NAMES:
        candidate = f"{base}_{index}"
        index += 1
    taken.add(candidate.lower())
    return candidate


# ---------------------------------------------------------------------------
# Unit conversion (canonical: km / m / % / deg)
# ---------------------------------------------------------------------------
_KM_PER = {"km": 1.0, "m": 0.001, "nm": 1.852}
_M_PER = {"m": 1.0, "ft": 0.3048}


def to_km(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    return value * _KM_PER.get(unit, 1.0)


def to_m(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    return value * _M_PER.get(unit, 1.0)


def slack_to_percent(value: Optional[float], is_ratio: bool) -> Optional[float]:
    """Normalise slack to percent. ``is_ratio`` means 1.02-style multipliers."""
    if value is None:
        return None
    return (value - 1.0) * 100.0 if is_ratio else value
