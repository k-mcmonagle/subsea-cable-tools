# -*- coding: utf-8 -*-
"""Coordinate-system detection for GeoMedia Access warehouses (.mdb, .pthmdb).

GeoMedia records the coordinate system of every geometry field:

``GCoordSystem``
    One row per coordinate system, keyed by ``CSGUID``. ``BaseStorageType``
    says which space coordinates are stored in (0 projected, 1 geographic),
    ``Stor2CompMatrix1..16`` is the 4x4 storage-to-computation matrix (scale,
    rotation, offset), and the ellipsoid, datum and projection parameters
    describe the space itself.
``GeometryProperties``
    One row per geometry field (``IndexID``) naming its ``GCoordSystemGUID``.
``FieldLookup``
    Maps ``IndexID`` to ``FeatureName`` / ``FieldName``.

Warehouses often carry more than one GCoordSystem row (GeoMedia writes a
default placeholder next to the one actually used), so the row to trust is
the one the geometry fields reference, not simply the first.

Detection is deliberately conservative. A CRS is only reported when the
stored parameters identify it unambiguously:

* geographic degrees on WGS84 -> EPSG:4326
* UTM on WGS84 (from the Transverse Mercator parameters or the zone) ->
  EPSG:326zz / EPSG:327zz

Anything else (another datum, an unrecognised projection, scaled or offset
storage) is reported as *not detected* with a plain-language reason and,
where the parameters point to a likely CRS, a hint such as "looks like
ED50 / UTM zone 31N (EPSG:23031)". Callers then ask the user rather than
guess: a wrong CRS silently misplaces every feature.

QGIS-free so it runs in the MDB worker subprocess and the pure test suite.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

_DEG_TO_RAD = math.pi / 180.0

#: GeoMedia ``BaseStorageType`` values.
STORAGE_PROJECTED = 0
STORAGE_GEOGRAPHIC = 1

#: GeoMedia ``GeodeticDatum`` code for WGS84 (verified against warehouses
#: whose ellipsoid parameters are WGS84). Other codes are never trusted on
#: their own.
DATUM_WGS84 = 17

#: (name, semi-major axis m, inverse flattening, likely geographic CRS hints)
_ELLIPSOIDS = (
    ("WGS84", 6378137.0, 298.257223563, ()),
    ("GRS80", 6378137.0, 298.257222101,
     ("ETRS89 (EPSG:4258)", "NAD83 (EPSG:4269)", "GDA94 (EPSG:4283)",
      "GDA2020 (EPSG:7844)")),
    ("International 1924", 6378388.0, 297.0, ("ED50 (EPSG:4230)",)),
    ("Airy 1830", 6377563.396, 299.3249646, ("OSGB36 (EPSG:4277)",)),
    ("Clarke 1866", 6378206.4, 294.9786982, ("NAD27 (EPSG:4267)",)),
    ("Bessel 1841", 6377397.155, 299.1528128, ("Tokyo (EPSG:4301)", "DHDN (EPSG:4314)")),
    ("WGS72", 6378135.0, 298.26, ("WGS 72 (EPSG:4322)",)),
    ("Krassovsky 1940", 6378245.0, 298.3, ("Pulkovo 1942 (EPSG:4284)",)),
    ("Clarke 1880 (RGS)", 6378249.145, 293.465, ()),
    ("Australian National", 6378160.0, 298.25, ("AGD66 (EPSG:4202)", "SAD69 (EPSG:4618)")),
)

#: FieldLookup column naming the feature table, across GeoMedia versions.
_FEATURE_COLUMNS = ("FeatureName", "Table", "TableName", "FeatureClassName")

_UTM_SCALE = 0.9996
_UTM_FALSE_EASTING = 500000.0
_UTM_FALSE_NORTHING_SOUTH = 10000000.0


@dataclass
class CrsDetection:
    """What one GCoordSystem row says about stored coordinates."""

    auth_id: Optional[str] = None   # e.g. "EPSG:4326"; None when not detected
    description: str = ""           # e.g. "geographic degrees on WGS84"
    reason: str = ""                # why it was not detected
    hint: str = ""                  # likely CRS, for the user to confirm
    guid: str = ""

    @property
    def detected(self) -> bool:
        return bool(self.auth_id)

    def explain(self) -> str:
        """One line for logs and error messages."""
        if self.detected:
            return f"{self.auth_id} ({self.description})"
        text = self.reason or "coordinate system not recognised"
        if self.hint:
            text += f". {self.hint}"
        return text

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Mapping]) -> "CrsDetection":
        data = dict(data or {})
        return cls(**{key: data.get(key) or ("" if key != "auth_id" else None)
                      for key in ("auth_id", "description", "reason", "hint", "guid")})


@dataclass
class FileCrsReport:
    """The coordinate systems of one warehouse and which tables use which."""

    detections: Dict[str, CrsDetection] = field(default_factory=dict)
    table_guids: Dict[str, str] = field(default_factory=dict)
    referenced: List[str] = field(default_factory=list)
    note: str = ""
    error: str = ""

    # -- queries --------------------------------------------------------------
    def crs_for_table(self, table_name: str) -> Optional[CrsDetection]:
        """The detection for ``table_name``, falling back to the file's CRS."""
        guid = _lookup_ci(self.table_guids, table_name)
        if guid is not None:
            return self.detections.get(guid) or CrsDetection(
                reason=f"its coordinate system {guid} is missing from GCoordSystem",
                guid=guid)
        return self.file_crs()

    def file_crs(self) -> Optional[CrsDetection]:
        """The single coordinate system the file uses, or None if it uses
        none or several that disagree."""
        candidates = [self.detections[guid] for guid in self.referenced
                      if guid in self.detections]
        missing = [guid for guid in self.referenced if guid not in self.detections]
        if missing:
            return CrsDetection(
                reason="geometry refers to coordinate system(s) missing from "
                       "GCoordSystem: " + ", ".join(missing))
        if not candidates:
            return None
        detected = {c.auth_id for c in candidates if c.detected}
        if len(candidates) == 1 or (len(detected) == 1 and all(c.detected for c in candidates)):
            return candidates[0]
        if len(detected) > 1:
            return CrsDetection(
                reason="the file uses several coordinate systems ("
                       + ", ".join(sorted(detected)) + ")")
        undetected = [c for c in candidates if not c.detected]
        return undetected[0]

    def summary(self) -> str:
        """One line naming the file's CRS or why it is unknown."""
        if self.error:
            return f"coordinate system could not be read: {self.error}"
        crs = self.file_crs()
        if crs is None:
            return self.note or "no GCoordSystem table"
        return crs.explain()

    # -- serialisation (worker JSON) -------------------------------------------
    def to_dict(self) -> Dict:
        return {
            "detections": {guid: d.to_dict() for guid, d in self.detections.items()},
            "table_guids": dict(self.table_guids),
            "referenced": list(self.referenced),
            "note": self.note,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping]) -> "FileCrsReport":
        data = dict(data or {})
        return cls(
            detections={str(guid): CrsDetection.from_dict(d)
                        for guid, d in (data.get("detections") or {}).items()},
            table_guids={str(k): str(v) for k, v in (data.get("table_guids") or {}).items()},
            referenced=[str(g) for g in data.get("referenced") or []],
            note=str(data.get("note") or ""),
            error=str(data.get("error") or ""),
        )


# --------------------------------------------------------------------------
# Row helpers
# --------------------------------------------------------------------------
def _lookup_ci(mapping: Mapping, key):
    if key in mapping:
        return mapping[key]
    upper = str(key).upper()
    for name, value in mapping.items():
        if str(name).upper() == upper:
            return value
    return None


def _get(row: Mapping, name: str):
    return _lookup_ci(row, name)


def _number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _close(value, expected, tolerance) -> bool:
    number = _number(value)
    return number is not None and abs(number - expected) <= tolerance


def _zero_or_missing(value, tolerance=1e-9) -> bool:
    number = _number(value)
    return number is None or abs(number) <= tolerance


def _guid(value) -> str:
    return str(value or "").strip().upper()


def _text(value) -> str:
    return str(value or "").replace("\x00", "").strip()


# --------------------------------------------------------------------------
# One coordinate system
# --------------------------------------------------------------------------
def identify_ellipsoid(radius, inverse_flattening) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """``(name, geographic hints)`` for a stored ellipsoid, or None."""
    a, inv_f = _number(radius), _number(inverse_flattening)
    if a is None or inv_f is None:
        return None
    for name, ref_a, ref_inv_f, hints in _ELLIPSOIDS:
        # WGS84 and GRS80 differ only at 1/f ~ 1.5e-6, so the flattening
        # tolerance must stay well below that.
        if abs(a - ref_a) <= 0.5 and abs(inv_f - ref_inv_f) <= 1e-7:
            return name, hints
    return None


def _ellipsoid_text(row) -> str:
    a = _number(_get(row, "EquatorialRadius"))
    inv_f = _number(_get(row, "InverseFlattening"))
    if a is None:
        return "an unrecorded ellipsoid"
    return f"an unrecognised ellipsoid (a={a:.3f} m, 1/f={inv_f if inv_f is not None else '?'})"


def _angle_degrees(value) -> List[float]:
    """Candidate readings (degrees) of a stored angle: radians first, as
    GeoMedia stores them, then degrees."""
    number = _number(value)
    if number is None:
        return []
    candidates = []
    if abs(number) <= 2.0 * math.pi + 1e-9:
        candidates.append(math.degrees(number))
    candidates.append(number)
    return candidates


def _utm_zone(row) -> Tuple[Optional[int], Optional[bool], str]:
    """``(zone, south, how)`` when the projection parameters are UTM."""
    scale = _get(row, "ScaleReductFact")
    false_x = _get(row, "FalseX")
    false_y = _get(row, "FalseY")
    lat0 = _get(row, "LatOfOrigin")
    tm_like = (_close(scale, _UTM_SCALE, 1e-7)
               and _close(false_x, _UTM_FALSE_EASTING, 0.01)
               and _zero_or_missing(lat0, 1e-9))
    south: Optional[bool] = None
    if _close(false_y, 0.0, 0.01):
        south = False
    elif _close(false_y, _UTM_FALSE_NORTHING_SOUTH, 0.01):
        south = True

    zone_from_cm = None
    if tm_like:
        for lon0 in _angle_degrees(_get(row, "LonOfOrigin")):
            zone = (lon0 + 183.0) / 6.0
            if abs(zone - round(zone)) < 1e-6 and 1 <= round(zone) <= 60:
                zone_from_cm = int(round(zone))
                break

    stored_zone = _number(_get(row, "Zone"))
    stored_zone = int(stored_zone) if stored_zone is not None and stored_zone.is_integer() \
        and 1 <= stored_zone <= 60 else None

    if zone_from_cm is not None:
        if stored_zone is not None and stored_zone != zone_from_cm:
            return None, south, (f"the stored zone ({stored_zone}) disagrees with the "
                                 f"central meridian (zone {zone_from_cm})")
        return zone_from_cm, south, "central meridian"
    if stored_zone is not None and (tm_like or _number(scale) is None):
        return stored_zone, south, "zone"
    return None, south, ""


def _utm_hint(ellipsoid_name: str, zone: int, south: Optional[bool]) -> str:
    hemisphere = "S" if south else "N"
    if south is None:
        hemisphere = "N or S"
    options = []
    if not south:
        if ellipsoid_name == "International 1924" and 28 <= zone <= 38:
            options.append(f"ED50 / UTM zone {zone}N (EPSG:230{zone:02d})")
        if ellipsoid_name == "GRS80":
            if 28 <= zone <= 38:
                options.append(f"ETRS89 / UTM zone {zone}N (EPSG:258{zone:02d})")
            if 1 <= zone <= 23:
                options.append(f"NAD83 / UTM zone {zone}N (EPSG:269{zone:02d})")
        if ellipsoid_name == "Clarke 1866" and 1 <= zone <= 22:
            options.append(f"NAD27 / UTM zone {zone}N (EPSG:267{zone:02d})")
    if ellipsoid_name == "GRS80" and south and 46 <= zone <= 59:
        options.append(f"GDA94 / MGA zone {zone} (EPSG:283{zone:02d})")
    text = f"Looks like UTM zone {zone}{hemisphere} on the {ellipsoid_name} ellipsoid"
    if options:
        text += ", e.g. " + " or ".join(options)
    return text


def _named_hint(row) -> str:
    """The file's own name/description of the CS, when it carries one."""
    bits = [b for b in (_text(_get(row, "Name")), _text(_get(row, "Description")))
            if b and b.lower() not in {"default", "none"}]
    if not bits:
        return ""
    text = " / ".join(dict.fromkeys(bits))
    epsg = re.search(r"EPSG\s*[:#]?\s*(\d{4,6})", text, re.IGNORECASE)
    if epsg:
        return f"The file names it '{text}' (EPSG:{epsg.group(1)})"
    return f"The file names it '{text}'"


def _join_hints(*hints: str) -> str:
    return ". ".join(h for h in hints if h)


def detect_coordinate_system(row: Mapping) -> CrsDetection:
    """Identify the CRS of coordinates stored under one GCoordSystem row."""
    row = dict(row or {})
    guid = _guid(_get(row, "CSGUID"))
    named = _named_hint(row)

    matrix = [_number(_get(row, f"Stor2CompMatrix{i}")) for i in range(1, 17)]
    m1, m2, m4, m5, m6, m8 = matrix[0], matrix[1], matrix[3], matrix[4], matrix[5], matrix[7]
    if not all(_zero_or_missing(v) for v in (m2, m5)):
        return CrsDetection(
            reason="the storage matrix rotates coordinates (GeoMedia local/rotated storage)",
            hint=named, guid=guid)
    if not all(_zero_or_missing(v, 1e-6) for v in (m4, m8)):
        return CrsDetection(
            reason="the storage matrix offsets coordinates (stored values are not plain "
                   "CRS coordinates)", hint=named, guid=guid)

    storage = _number(_get(row, "BaseStorageType"))
    degrees = (m1 is not None and abs(m1 - _DEG_TO_RAD) <= 1e-12
               and (m6 is None or abs(m6 - _DEG_TO_RAD) <= 1e-12))
    unit_scale = (m1 is not None and abs(m1 - 1.0) <= 1e-12
                  and (m6 is None or abs(m6 - 1.0) <= 1e-12))
    if storage is None:
        # Older/partial rows: infer the space from the storage scale.
        storage = STORAGE_GEOGRAPHIC if degrees else (
            STORAGE_PROJECTED if unit_scale else None)

    ellipsoid = identify_ellipsoid(_get(row, "EquatorialRadius"), _get(row, "InverseFlattening"))
    ellipsoid_name = ellipsoid[0] if ellipsoid else ""
    datum = _number(_get(row, "GeodeticDatum"))
    datum_known = datum is not None
    is_wgs84 = (ellipsoid_name == "WGS84" and (not datum_known or datum == DATUM_WGS84)) or (
        ellipsoid is None and _number(_get(row, "EquatorialRadius")) is None
        and datum == DATUM_WGS84)
    datum_reason = ""
    if ellipsoid_name == "WGS84" and datum_known and datum != DATUM_WGS84:
        datum_reason = (f"its datum code ({int(datum)}) is not WGS84 although the "
                        "ellipsoid is")

    if storage == STORAGE_GEOGRAPHIC:
        if not degrees:
            scale_text = "radians" if unit_scale else (
                f"an unusual unit (scale {m1})" if m1 is not None else "an unrecorded unit")
            return CrsDetection(
                reason=f"geographic coordinates are stored in {scale_text}, not degrees",
                hint=named, guid=guid)
        if is_wgs84:
            return CrsDetection(auth_id="EPSG:4326",
                                description="geographic degrees on WGS84", guid=guid)
        if datum_reason:
            return CrsDetection(reason=f"geographic degrees, but {datum_reason}",
                                hint=named, guid=guid)
        hints = ellipsoid[1] if ellipsoid else ()
        hint = (f"Likely {' or '.join(hints)}" if hints else "")
        where = (f"the {ellipsoid_name} ellipsoid" if ellipsoid else _ellipsoid_text(row))
        return CrsDetection(
            reason=f"geographic degrees on {where}, whose datum cannot be confirmed",
            hint=_join_hints(hint, named), guid=guid)

    if storage == STORAGE_PROJECTED:
        if not unit_scale:
            return CrsDetection(
                reason=("projected coordinates are stored with a scale factor "
                        f"({m1 if m1 is not None else 'unrecorded'}), not plain metres"),
                hint=named, guid=guid)
        zone, south, how = _utm_zone(row)
        algorithm = _number(_get(row, "ProjAlgorithm"))
        algorithm_text = f" (projection algorithm code {int(algorithm)})" if algorithm is not None else ""
        if zone is None:
            reason = "projected coordinates in a projection that is not recognised" + algorithm_text
            if how:
                reason = f"projected coordinates, but {how}"
            return CrsDetection(reason=reason, hint=named, guid=guid)
        if south is None:
            return CrsDetection(
                reason=f"UTM zone {zone}, but the hemisphere cannot be determined (no false northing)",
                hint=_join_hints(_utm_hint(ellipsoid_name or "unknown", zone, None), named),
                guid=guid)
        if is_wgs84:
            code = (32700 if south else 32600) + zone
            return CrsDetection(
                auth_id=f"EPSG:{code}",
                description=f"WGS 84 / UTM zone {zone}{'S' if south else 'N'} (from the {how})",
                guid=guid)
        if datum_reason:
            return CrsDetection(reason=f"UTM zone {zone}, but {datum_reason}",
                                hint=named, guid=guid)
        hint = (_utm_hint(ellipsoid_name, zone, south) if ellipsoid else
                f"Looks like UTM zone {zone}{'S' if south else 'N'} on {_ellipsoid_text(row)}")
        return CrsDetection(
            reason=f"UTM zone {zone}{'S' if south else 'N'} on a datum that cannot be confirmed",
            hint=_join_hints(hint, named), guid=guid)

    storage_text = "unrecorded" if storage is None else f"type {int(storage)}"
    return CrsDetection(
        reason=f"the storage space ({storage_text}) is neither geographic nor projected",
        hint=named, guid=guid)


# --------------------------------------------------------------------------
# A whole warehouse
# --------------------------------------------------------------------------
def analyse_coordinate_systems(
        gcoordsystem_rows: Sequence[Mapping],
        geometry_properties_rows: Iterable[Mapping] = (),
        field_lookup_rows: Iterable[Mapping] = ()) -> FileCrsReport:
    """Resolve which coordinate system each feature table's geometry uses."""
    report = FileCrsReport()
    rows = [dict(r) for r in gcoordsystem_rows or []]
    if not rows:
        report.note = "the file has no GCoordSystem table"
        return report

    for index, row in enumerate(rows):
        guid = _guid(_get(row, "CSGUID")) or f"ROW{index + 1}"
        detection = detect_coordinate_system(row)
        detection.guid = guid
        report.detections[guid] = detection

    # Geometry field -> coordinate system (primary geometry fields first).
    properties = [dict(r) for r in geometry_properties_rows or []]
    index_guid: Dict[object, str] = {}
    primary_guids: List[str] = []
    any_guids: List[str] = []
    for prop in properties:
        guid = _guid(_get(prop, "GCoordSystemGUID"))
        if not guid:
            continue
        index_id = _get(prop, "IndexID")
        if index_id is not None:
            index_guid[_index_key(index_id)] = guid
        any_guids.append(guid)
        if _get(prop, "PrimaryGeometryFlag") in (True, 1, -1, "1", "True", "true"):
            primary_guids.append(guid)

    lookup = [dict(r) for r in field_lookup_rows or []]
    for entry in lookup:
        index_id = _get(entry, "IndexID")
        # GeoMedia versions name the column FeatureName or Table.
        feature = _text(next((_get(entry, name) for name in _FEATURE_COLUMNS
                              if _get(entry, name) is not None), None))
        if index_id is None or not feature:
            continue
        guid = index_guid.get(_index_key(index_id))
        if guid is None:
            continue
        # A table with several geometry fields keeps its primary one: the
        # primary flag is checked by preferring guids seen as primary.
        current = report.table_guids.get(feature)
        if current is None or (guid in primary_guids and current not in primary_guids):
            report.table_guids[feature] = guid

    referenced = primary_guids or any_guids
    if referenced:
        report.referenced = list(dict.fromkeys(referenced))
    elif len(report.detections) == 1:
        report.referenced = list(report.detections)
    else:
        # No linkage recorded: only safe when every row says the same thing.
        detected = {d.auth_id for d in report.detections.values() if d.detected}
        if len(detected) == 1:
            report.referenced = [guid for guid, d in report.detections.items() if d.detected][:1]
            report.note = ("GeometryProperties does not link geometry to a coordinate "
                           "system; the only detectable GCoordSystem row was used")
        else:
            report.referenced = list(report.detections)
    return report


def _index_key(value):
    number = _number(value)
    if number is not None and number.is_integer():
        return int(number)
    return str(value)
