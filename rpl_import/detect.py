# -*- coding: utf-8 -*-
"""Detection: worksheet scoring, data range, layout, and column mapping.

Detection is content-first: coordinate columns are found from what the cells
actually contain (hemisphere letters, degree/minute magnitudes, parseable DDM
text), then the header vocabulary assigns the remaining typed fields, with
content checks breaking ties. Every assignment carries a human-readable
reason and the overall result carries a confidence so the wizard can show
*why* something was chosen and flag ambiguity instead of guessing silently.

Decimal-degree detection deliberately requires a latitude/longitude-ish
header (bare numeric columns are ambiguous); split-DDM and DDM-text are
recognised from content alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import coords as C
from .model import (
    COORD_DDM_TEXT, COORD_DECIMAL_DEGREES, COORD_SPLIT_DDM,
    FLAT_ARRIVING, ImportProfile, LAYOUT_ALTERNATING, LAYOUT_FLAT,
    PF_CABLE_DIST_CUM, PF_CHART_NO, PF_DEPTH, PF_DIST_CUM, PF_EVENT,
    PF_LAT_DEG, PF_LAT_HEMI, PF_LAT_MIN, PF_LAT_TEXT, PF_LON_DEG,
    PF_LON_HEMI, PF_LON_MIN, PF_LON_TEXT, PF_POS_NO, PF_REMARKS,
    SF_BEARING, SF_BURIAL, SF_CABLE_CODE, SF_CABLE_DIST, SF_CABLE_TYPE,
    SF_DATE_INSTALLED, SF_DIST, SF_EEZ, SF_FIBER_PAIR, SF_LAY_DIRECTION,
    SF_LAY_VESSEL, SF_PROTECTION, SF_SLACK, SF_TARGET_BURIAL, SF_TERRITORIAL,
    header_signature, normalise_header_text,
)
from .parser import parse_point_coords, row_has_coords
from .reader import SourceGrid

MAX_HEADER_ROWS = 12          # header block search depth above the data start
MAX_COLUMN_SCAN_ROWS = 4000   # bounded sample used to infer column meanings
GAP_TOLERANCE = 6             # rows of non-data tolerated inside the table


@dataclass
class DetectionResult:
    profile: ImportProfile
    position_count: int = 0
    confidence: float = 0.0            # 0..1
    reasons: Dict[str, str] = field(default_factory=dict)   # field/topic -> why
    header_texts: List[str] = field(default_factory=list)   # per column, 1-based-1


# ---------------------------------------------------------------------------
# Column content statistics
# ---------------------------------------------------------------------------
@dataclass
class _ColStats:
    non_empty: int = 0
    numeric: int = 0
    integers: int = 0
    lat_hemi: int = 0
    lon_hemi: int = 0
    ddm_lat: int = 0
    ddm_lon: int = 0
    min_val: float = float("inf")
    max_val: float = float("-inf")
    lat_range: int = 0     # numeric with abs <= 90 (plausible latitude degrees)
    lon_range: int = 0     # numeric with abs <= 180 (plausible longitude degrees)
    minute_range: int = 0  # numeric with abs < 60 (plausible decimal minutes)
    monotonic_pairs: int = 0
    numeric_pairs: int = 0

    @property
    def numeric_frac(self) -> float:
        return self.numeric / self.non_empty if self.non_empty else 0.0

    def mostly_within(self, counter: int) -> bool:
        """True when nearly all numeric values fall inside a plausibility
        counter — at most 10% (but always at least one) outliers allowed, so
        a stray footer number or annotation caught in the scan cannot veto an
        otherwise clear coordinate column, even in short tables.
        """
        if not self.numeric:
            return False
        return (self.numeric - counter) <= max(1, self.numeric // 10)


def _column_stats(grid: SourceGrid, rows: List[int]) -> Dict[int, _ColStats]:
    stats: Dict[int, _ColStats] = {c: _ColStats() for c in range(1, grid.n_cols + 1)}
    previous: Dict[int, float] = {}
    for row in rows:
        values = grid.row_values(row)
        for col in range(1, grid.n_cols + 1):
            value = values[col - 1]
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            s = stats[col]
            s.non_empty += 1
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                number = float(value)
            else:
                text = str(value).strip()
                upper = text.upper()
                if upper in ("N", "S"):
                    s.lat_hemi += 1
                elif upper in ("E", "W"):
                    s.lon_hemi += 1
                else:
                    if C.parse_ddm_text(text, C.AXIS_LAT)[0] is not None:
                        s.ddm_lat += 1
                    if C.parse_ddm_text(text, C.AXIS_LON)[0] is not None:
                        s.ddm_lon += 1
                try:
                    number = float(_strip_unit_symbols(text))
                except ValueError:
                    continue
            s.numeric += 1
            if number == int(number):
                s.integers += 1
            s.min_val = min(s.min_val, number)
            s.max_val = max(s.max_val, number)
            if abs(number) <= 90.0:
                s.lat_range += 1
            if abs(number) <= 180.0:
                s.lon_range += 1
            if abs(number) < 60.0:
                s.minute_range += 1
            if col in previous:
                s.numeric_pairs += 1
                if number >= previous[col]:
                    s.monotonic_pairs += 1
            previous[col] = number
    return stats


def _strip_unit_symbols(text: str) -> str:
    """Bare numbers with a trailing unit symbol (``9°``, ``36.1115'``) count
    as numeric; full DDM strings still fail float() and stay text."""
    return text.replace(",", ".").rstrip("°º'′\"″ ").strip()


def _cell_is_datalike(value) -> bool:
    """Numeric, hemisphere letter, or DDM-parseable — i.e. a body cell."""
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip()
    if not text:
        return False
    if text.upper() in ("N", "S", "E", "W"):
        return True
    try:
        float(_strip_unit_symbols(text))
        return True
    except ValueError:
        pass
    return (C.parse_ddm_text(text, C.AXIS_LAT)[0] is not None
            or C.parse_ddm_text(text, C.AXIS_LON)[0] is not None)


def _datalike_rows(grid: SourceGrid, limit: int) -> List[int]:
    """Rows that look like table body (headers/titles/banners excluded)."""
    rows = []
    for row in range(1, min(grid.n_rows, limit) + 1):
        values = [v for v in grid.row_values(row)
                  if v is not None and str(v).strip() != ""]
        if len(values) < 2:
            continue
        datalike = sum(1 for v in values if _cell_is_datalike(v))
        if datalike >= 2 and datalike / len(values) >= 0.5:
            rows.append(row)
    return rows


def _provisional_header_rows(grid: SourceGrid, limit: int) -> List[int]:
    """Header-like rows near the top, excluding rows dominated by values."""
    rows: List[int] = []
    for row in range(1, min(grid.n_rows, limit) + 1):
        values = [value for value in grid.row_values(row)
                  if value is not None and str(value).strip() != ""]
        if not values:
            continue
        headerlike = sum(1 for value in values if not _cell_is_datalike(value))
        if headerlike / len(values) >= 0.5:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Coordinate configuration detection
# ---------------------------------------------------------------------------
def _dominant_run(rows: List[int], tolerance: int = GAP_TOLERANCE) -> List[int]:
    """The longest contiguous-ish run of rows (gaps <= tolerance).

    Column statistics computed over every data-like row in a sheet get
    polluted by title blocks and footer summaries ("Total route length ...
    1234.5 km"); the table body is the longest run, so restrict to it.
    """
    if not rows:
        return rows
    runs: List[List[int]] = [[rows[0]]]
    for row in rows[1:]:
        if row - runs[-1][-1] <= tolerance:
            runs[-1].append(row)
        else:
            runs.append([row])
    return max(runs, key=len)


def _detect_coordinates(grid: SourceGrid, scan_rows: List[int],
                        header_texts: List[str],
                        reasons: Dict[str, str],
                        only: Optional[str] = None) -> Optional[Tuple[str, Dict[str, int]]]:
    """(encoding, coordinate-field mapping) or None.

    Preference order is deliberate: split degrees/decimal-minutes/hemisphere
    is the industry-standard RPL form and always wins when present, then
    combined DDM text, then signed decimal degrees under lat/lon headers.
    ``only`` restricts detection to a single encoding (used when the user
    picks an encoding manually and wants its columns found automatically).
    """
    stats = _column_stats(grid, _dominant_run(scan_rows))

    def hemi_cols(kind: str) -> List[int]:
        result = []
        for col, s in stats.items():
            count = s.lat_hemi if kind == "lat" else s.lon_hemi
            if s.non_empty >= 2 and count / s.non_empty >= 0.8:
                result.append(col)
        return result

    lat_hemis, lon_hemis = hemi_cols("lat"), hemi_cols("lon")

    # -- split DDM: (deg, min) numeric columns beside a hemisphere column ----
    def split_for(hemi_col: int, axis: str,
                  exclude: frozenset = frozenset()) -> Optional[Tuple[int, int]]:
        for pair in ((hemi_col - 2, hemi_col - 1),   # deg | min | hemi (usual)
                     (hemi_col + 1, hemi_col + 2)):  # hemi | deg | min
            candidates = [c for c in pair
                          if 1 <= c <= grid.n_cols and c not in exclude
                          and stats[c].numeric_frac >= 0.8]
            if len(candidates) < 2:
                continue
            deg_col, min_col = candidates[0], candidates[1]
            sd, sm = stats[deg_col], stats[min_col]
            # Some industry RPLs retain a signed degree value as well as an
            # explicit hemisphere column. The parser deliberately uses the
            # hemisphere as authoritative, so detection compares degree
            # magnitudes rather than rejecting these otherwise clear triples.
            if not sd.mostly_within(sd.lat_range if axis == "lat" else sd.lon_range):
                continue
            if not sm.mostly_within(sm.minute_range):
                continue
            # degrees are typically integers; minutes typically fractional
            if sd.integers < sd.numeric * 0.9 and sm.integers == sm.numeric:
                deg_col, min_col = min_col, deg_col
                sd, sm = stats[deg_col], stats[min_col]
            # A degrees column is integer-valued (same outlier allowance as
            # the range checks); a mostly-fractional "degrees" candidate is a
            # decimal/derived column, not part of a DDM triple.
            if not sd.mostly_within(sd.integers):
                continue
            return deg_col, min_col
        return None

    for lat_hemi in (lat_hemis if only in (None, COORD_SPLIT_DDM) else []):
        lat_pair = split_for(lat_hemi, "lat", frozenset(lon_hemis))
        if not lat_pair:
            continue
        for lon_hemi in lon_hemis:
            if lon_hemi == lat_hemi:
                continue
            lon_pair = split_for(
                lon_hemi, "lon", frozenset(lat_pair) | {lat_hemi})
            if not lon_pair:
                continue
            mapping = {
                PF_LAT_DEG: lat_pair[0], PF_LAT_MIN: lat_pair[1],
                PF_LAT_HEMI: lat_hemi,
                PF_LON_DEG: lon_pair[0], PF_LON_MIN: lon_pair[1],
                PF_LON_HEMI: lon_hemi,
            }
            reasons["coordinates"] = (
                "Split degrees/decimal-minutes/hemisphere columns detected "
                f"(hemisphere letters in columns {lat_hemi} and {lon_hemi}).")
            return COORD_SPLIT_DDM, mapping

    # -- DDM text -------------------------------------------------------------
    ddm_lat_cols = ddm_lon_cols = []
    if only in (None, COORD_DDM_TEXT):
        ddm_lat_cols = [c for c, s in stats.items()
                        if s.non_empty >= 2 and s.ddm_lat / s.non_empty >= 0.6]
        ddm_lon_cols = [c for c, s in stats.items()
                        if s.non_empty >= 2 and s.ddm_lon / s.non_empty >= 0.6
                        and c not in ddm_lat_cols]
    if ddm_lat_cols and ddm_lon_cols:
        mapping = {PF_LAT_TEXT: ddm_lat_cols[0], PF_LON_TEXT: ddm_lon_cols[0]}
        reasons["coordinates"] = (
            f"Combined degrees-minutes text detected in columns "
            f"{ddm_lat_cols[0]} (lat) and {ddm_lon_cols[0]} (lon).")
        return COORD_DDM_TEXT, mapping

    # -- decimal degrees: needs lat/lon headers + plausible numeric content ---
    # Group headers ("Latitude" spanning deg/min/hemi sub-columns) are
    # inherited by child columns, so several columns can carry a lat/lon-ish
    # header. Score candidates instead of taking the first: a genuine decimal
    # column has fractional values and often says "decimal"; columns that sit
    # immediately before a hemisphere-letter column are part of a split-DDM
    # group and are never decimal degrees; derived trigonometry columns
    # (radians/sin/cos) are penalised.
    hemi_all = set(lat_hemis) | set(lon_hemis)

    def in_ddm_group(col: int) -> bool:
        return (col + 1) in hemi_all or (col + 2) in hemi_all

    if only not in (None, COORD_DECIMAL_DEGREES):
        return None

    def decimal_candidate(col: int, header: str, axis: str) -> Optional[int]:
        s = stats[col]
        if not s.mostly_within(s.lat_range if axis == "lat" else s.lon_range):
            return None
        score = 0
        if re.search(r"decimal|\bdd\b|deg", header):
            score += 30
        if s.integers < s.numeric:
            score += 20          # decimal degrees are fractional in practice
        if re.search(r"radian|\brad\b|\bsin\b|\bcos\b|\btan\b", header):
            score -= 60
        return score

    best: Dict[str, Tuple[int, int]] = {}   # axis -> (score, col)
    for col in range(1, grid.n_cols + 1):
        header = header_texts[col - 1] if col <= len(header_texts) else ""
        s = stats[col]
        if s.numeric_frac < 0.8 or s.non_empty < 2 or in_ddm_group(col):
            continue
        if re.search(r"\blat", header):
            axis = "lat"
        elif re.search(r"\b(lon|lng|long)", header):
            axis = "lon"
        else:
            continue
        score = decimal_candidate(col, header, axis)
        if score is None:
            continue
        if axis not in best or score > best[axis][0]:
            best[axis] = (score, col)
    if "lat" in best and "lon" in best and best["lat"][1] != best["lon"][1]:
        lat_col, lon_col = best["lat"][1], best["lon"][1]
        reasons["coordinates"] = (
            f"Signed decimal degrees under latitude/longitude headers "
            f"(columns {lat_col} and {lon_col}).")
        return COORD_DECIMAL_DEGREES, {PF_LAT_TEXT: lat_col, PF_LON_TEXT: lon_col}
    return None


# ---------------------------------------------------------------------------
# Data range and layout
# ---------------------------------------------------------------------------
def _coordinate_rows(grid: SourceGrid, profile: ImportProfile,
                     limit: int) -> List[int]:
    rows = []
    for row in range(1, min(grid.n_rows, limit) + 1):
        if row_has_coords(grid, row, profile):
            rows.append(row)
    return rows


def _detect_range_and_layout(coord_rows: List[int],
                             reasons: Dict[str, str]) -> Tuple[int, int, str]:
    """(start, end, layout) from the rows where coordinates parse."""
    if not coord_rows:
        return 0, 0, LAYOUT_ALTERNATING
    # Trim isolated early hits (e.g. an example row in a title block) by
    # keeping the longest run whose internal gaps stay within tolerance.
    runs: List[List[int]] = [[coord_rows[0]]]
    for row in coord_rows[1:]:
        if row - runs[-1][-1] <= GAP_TOLERANCE:
            runs[-1].append(row)
        else:
            runs.append([row])
    best = max(runs, key=len)
    start, end = best[0], best[-1]

    gaps = [b - a for a, b in zip(best, best[1:])]
    if gaps:
        two = sum(1 for g in gaps if g == 2)
        one = sum(1 for g in gaps if g == 1)
        layout = LAYOUT_ALTERNATING if two >= one else LAYOUT_FLAT
        frac = (two if layout == LAYOUT_ALTERNATING else one) / len(gaps)
        reasons["layout"] = (
            f"{'Alternating point/segment rows' if layout == LAYOUT_ALTERNATING else 'One position per row'} "
            f"({frac:.0%} of row spacing matches).")
    else:
        layout = LAYOUT_ALTERNATING
        reasons["layout"] = "Single position row; layout defaulted to alternating."
    return start, end, layout


# ---------------------------------------------------------------------------
# Header block & vocabulary mapping
# ---------------------------------------------------------------------------
def _header_rows(grid: SourceGrid, data_start: int) -> List[int]:
    """Contiguous mostly-text rows directly above the data start."""
    rows: List[int] = []
    for row in range(data_start - 1, max(0, data_start - 1 - MAX_HEADER_ROWS), -1):
        values = [v for v in grid.row_values(row) if v is not None
                  and str(v).strip() != ""]
        if not values:
            if rows:
                break
            continue
        texty = sum(1 for v in values
                    if not isinstance(v, (int, float)) or isinstance(v, bool))
        if texty / len(values) >= 0.5:
            rows.append(row)
        else:
            break
    rows.reverse()
    return rows


def header_texts_for(grid: SourceGrid, header_rows: List[int]) -> List[str]:
    """Combined header text per column, including merged-like group labels.

    Excel merged cells are expanded by the reader. CSV exports cannot retain
    merges, so a group such as ``Latitude,,,Longitude,,`` appears as a label
    followed by blank child columns. Blank runs of at least two columns are
    treated as grouped children and inherit the preceding label. Single blank
    cells are left alone to avoid inventing context in ordinary flat headers.
    """
    rows: List[List[str]] = []
    for row in header_rows:
        values = [normalise_header_text(grid.cell(row, col))
                  for col in range(1, grid.n_cols + 1)]
        contextual = list(values)
        column = 0
        while column < grid.n_cols:
            if not values[column]:
                column += 1
                continue
            run_start = column + 1
            run_end = run_start
            while run_end < grid.n_cols and not values[run_end]:
                run_end += 1
            if run_end < grid.n_cols and run_end - run_start >= 2:
                for child in range(run_start, run_end):
                    contextual[child] = values[column]
            column = max(run_end, column + 1)
        rows.append(contextual)

    texts = []
    for column in range(grid.n_cols):
        bits: List[str] = []
        for row in rows:
            value = row[column]
            if value and value not in bits:
                bits.append(value)
        texts.append(" ".join(bits))
    return texts


#: Ordered vocabulary: (field, regex, needs_cable_context, reason). Earlier
#: entries are more specific and win. ``needs_cable_context`` distinguishes
#: the cable-distance group from the route-distance group in industry
#: headers where a "Cable"/"Route" group title sits above "Distance".
_VOCAB: List[Tuple[str, str, Optional[bool], str]] = [
    (PF_POS_NO, r"\b(pos|position|wpt|waypoint)\s*(no|num|number|id|#)?\b", None,
     "position-number header"),
    (PF_EVENT, r"\bevent\b|\bfeature\b|\bdescription\b", None, "event header"),
    (PF_CHART_NO, r"\bchart\b", None, "chart header"),
    (PF_REMARKS, r"remark|comment|note", None, "remarks header"),
    (PF_DEPTH, r"depth|\bwd\b", None, "depth header"),
    (SF_SLACK, r"slack", None, "slack header"),
    (SF_BEARING, r"bearing|\bbrg\b|course|heading", None, "bearing header"),
    (SF_FIBER_PAIR, r"fib(re|er)", None, "fibre-pair header"),
    (SF_CABLE_CODE, r"cable\s*code|\bcode\b", None, "cable-code header"),
    (SF_CABLE_TYPE, r"cable\s*type|\btype\b|armou?r", None, "cable-type header"),
    (SF_LAY_DIRECTION, r"lay\s*dir", None, "lay-direction header"),
    (SF_LAY_VESSEL, r"vessel|ship", None, "vessel header"),
    (SF_PROTECTION, r"protect|burial\s*method|install(ation)?\s*method", None,
     "protection-method header"),
    (SF_DATE_INSTALLED, r"date", None, "date header"),
    (SF_TARGET_BURIAL, r"target.*burial|burial.*target|target.*dob", None,
     "target-burial header"),
    (SF_BURIAL, r"burial|\bdob\b|depth\s*of\s*burial", None, "burial header"),
    (SF_TERRITORIAL, r"territorial|12\s*nm", None, "territorial-water header"),
    (SF_EEZ, r"\beez\b|exclusive\s*econ", None, "EEZ header"),
    # distance family — cable group first (more specific), then route/plain
    (PF_CABLE_DIST_CUM, r"cable.*(cum|total|kp)|(cum|total).*cable", True,
     "cumulative cable distance header"),
    (SF_CABLE_DIST, r"cable.*(dist|between|span|leg)", True,
     "cable span distance header"),
    (PF_DIST_CUM, r"(cum|total)\w*\s*(dist)?|\bkp\b|route\s*(dist)?.*(cum|total)", False,
     "cumulative route distance header"),
    (SF_DIST, r"dist(ance)?\s*(between|span|leg)?|\bspan\b|\bleg\b", False,
     "span distance header"),
]


def _map_remaining_columns(grid: SourceGrid, header_texts: List[str],
                           profile: ImportProfile, scan_rows: List[int],
                           reasons: Dict[str, str]) -> None:
    """Assign non-coordinate fields by header vocabulary + content checks."""
    stats = _column_stats(grid, scan_rows)
    taken = set(profile.mapping.values())

    def content_ok(field_key: str, col: int) -> bool:
        s = stats.get(col, _ColStats())
        if field_key in (PF_DIST_CUM, PF_CABLE_DIST_CUM):
            return s.numeric_frac >= 0.7 and (
                s.numeric_pairs == 0 or s.monotonic_pairs / s.numeric_pairs >= 0.7)
        if field_key in (SF_DIST, SF_CABLE_DIST):
            return s.numeric_frac >= 0.7 and s.min_val >= 0
        if field_key == SF_BEARING:
            return s.numeric_frac >= 0.7 and 0 <= s.min_val and s.max_val <= 360
        if field_key == SF_SLACK:
            return s.numeric_frac >= 0.6 and -50 <= s.min_val and s.max_val <= 200
        if field_key == PF_DEPTH:
            return s.numeric_frac >= 0.6
        if field_key == PF_POS_NO:
            # tolerate occasional annotated numbers ("2A") without unmapping
            return s.numeric_frac >= 0.6 and s.integers >= s.numeric * 0.9
        return True

    def header_score(field_key: str, header: str) -> int:
        """Prefer canonical source fields over nearby derived calculations."""
        score = 0
        if field_key == SF_BEARING:
            if re.search(r"\b(bearing|brg)\b", header):
                score += 40
            if re.search(r"\b(course|heading)\b", header):
                score += 10
            if re.search(r"radian|\brad\b", header):
                score -= 30
        if field_key in (SF_DIST, SF_CABLE_DIST):
            if re.search(r"between|span|\bleg\b", header):
                score += 40
            if re.search(r"\b(km|nm|m)\b", header):
                score += 10
            if re.search(r"mile|6087|equator|meridional", header):
                score -= 30
            if re.search(r"cum|total|\bkp\b", header):
                score -= 40
        if field_key in (PF_DIST_CUM, PF_CABLE_DIST_CUM):
            if re.search(r"cum|total|\bkp\b", header):
                score += 40
            if re.search(r"between|span|\bleg\b", header):
                score -= 40
        if field_key == PF_DEPTH:
            if re.search(r"approx|water|\bwd\b", header):
                score += 30
            if re.search(r"burial|\bdob\b", header):
                score -= 40
        return score

    for field_key, pattern, cable_context, why in _VOCAB:
        if profile.mapping.get(field_key):
            continue
        best: Optional[int] = None
        best_score: Optional[int] = None
        for col in range(1, grid.n_cols + 1):
            if col in taken:
                continue
            header = header_texts[col - 1] if col <= len(header_texts) else ""
            if not header or not re.search(pattern, header):
                continue
            has_cable = bool(re.search(r"cable", header))
            if cable_context is True and not has_cable:
                continue
            if cable_context is False and has_cable:
                continue
            if not content_ok(field_key, col):
                continue
            score = header_score(field_key, header)
            if best is None or score > best_score:
                best = col
                best_score = score
        if best is not None:
            profile.mapping[field_key] = best
            taken.add(best)
            reasons[field_key] = (
                f"Column {best} ('{header_texts[best - 1]}'): {why}.")

    # Some RPLs use a two-column cable-distance pair without repeating the
    # group label over the second column, for example ``Cable Distance`` then
    # simply ``Cumulative``.  The generic second header intentionally cannot
    # identify itself as cable distance, but its position is a strong
    # tie-breaker once both the ordinary route KP and cable span have already
    # been identified.  Do not infer from adjacency alone: the candidate must
    # still advertise cumulative/total meaning and have cumulative-looking
    # numeric content, and it must not have been claimed by another field.
    if (not profile.mapping.get(PF_CABLE_DIST_CUM)
            and profile.mapping.get(PF_DIST_CUM)
            and profile.mapping.get(SF_CABLE_DIST)):
        candidate = profile.mapping[SF_CABLE_DIST] + 1
        if candidate <= grid.n_cols and candidate not in taken:
            header = (header_texts[candidate - 1]
                      if candidate <= len(header_texts) else "")
            if (re.search(r"\b(cum\w*|total|running)\b", header)
                    and content_ok(PF_CABLE_DIST_CUM, candidate)):
                profile.mapping[PF_CABLE_DIST_CUM] = candidate
                taken.add(candidate)
                reasons[PF_CABLE_DIST_CUM] = (
                    f"Column {candidate} ('{header}'): cumulative cable "
                    f"distance immediately follows detected cable span "
                    f"distance column {profile.mapping[SF_CABLE_DIST]}.")


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
def _detect_units(header_texts: List[str], profile: ImportProfile,
                  reasons: Dict[str, str]) -> None:
    def unit_of(col: Optional[int], allowed: Tuple[str, ...]) -> Optional[str]:
        if not col or col > len(header_texts):
            return None
        header = header_texts[col - 1]
        for unit in allowed:
            if re.search(rf"\b{unit}\b", header):
                return unit
        return None

    dist_unit = (unit_of(profile.mapping.get(PF_DIST_CUM), ("km", "nm", "m"))
                 or unit_of(profile.mapping.get(SF_DIST), ("km", "nm", "m")))
    if dist_unit:
        profile.distance_unit = dist_unit
        reasons["distance_unit"] = f"Header states distances in {dist_unit}."
    cable_unit = (unit_of(profile.mapping.get(PF_CABLE_DIST_CUM), ("km", "nm", "m"))
                  or unit_of(profile.mapping.get(SF_CABLE_DIST), ("km", "nm", "m")))
    if cable_unit:
        profile.cable_distance_unit = cable_unit
        reasons["cable_distance_unit"] = f"Header states cable distances in {cable_unit}."
    depth_unit = unit_of(profile.mapping.get(PF_DEPTH), ("m", "ft"))
    if depth_unit:
        profile.depth_unit = depth_unit
        reasons["depth_unit"] = f"Header states depth in {depth_unit}."


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def detect_coordinate_columns(grid: SourceGrid,
                              encoding: str) -> Optional[Dict[str, int]]:
    """Coordinate-field mapping for one specific encoding, or None.

    Used by the wizard when the user manually switches the coordinate
    encoding: content detection re-runs constrained to that encoding so its
    columns (deg/min/hemisphere triples, DDM text, decimal degrees) are
    assigned automatically instead of being hunted down by hand. Projected
    easting/northing has no content signature and always returns None.
    """
    provisional_rows = _provisional_header_rows(grid, MAX_HEADER_ROWS)
    if not provisional_rows:
        provisional_rows = list(range(1, min(grid.n_rows, MAX_HEADER_ROWS) + 1))
    header_texts = header_texts_for(grid, provisional_rows)
    scan_rows = _datalike_rows(grid, MAX_COLUMN_SCAN_ROWS)
    if not scan_rows:
        scan_rows = list(range(1, min(grid.n_rows, MAX_COLUMN_SCAN_ROWS) + 1))
    found = _detect_coordinates(grid, scan_rows, header_texts, {}, only=encoding)
    return found[1] if found else None


def detect(grid: SourceGrid) -> DetectionResult:
    """Full detection for one sheet; the result profile is user-correctable."""
    reasons: Dict[str, str] = {}
    profile = ImportProfile(sheet=grid.sheet)

    # Provisional headers from the top of the sheet (refined once the data
    # start is known) so decimal-degree detection can see lat/lon words.
    provisional_rows = _provisional_header_rows(grid, MAX_HEADER_ROWS)
    if not provisional_rows:
        provisional_rows = list(
            range(1, min(grid.n_rows, MAX_HEADER_ROWS) + 1))
    provisional_headers = header_texts_for(grid, provisional_rows)

    scan_rows = _datalike_rows(grid, MAX_COLUMN_SCAN_ROWS)
    if not scan_rows:
        scan_rows = list(range(
            1, min(grid.n_rows, MAX_COLUMN_SCAN_ROWS) + 1))
    coord = _detect_coordinates(grid, scan_rows, provisional_headers, reasons)
    if coord is None:
        reasons["coordinates"] = "No coordinate columns could be detected."
        profile.excluded_columns = list(range(1, grid.n_cols + 1))
        return DetectionResult(profile=profile, reasons=reasons,
                               header_texts=provisional_headers)
    profile.coord_encoding, coord_mapping = coord
    profile.mapping.update(coord_mapping)

    # Column inference is sampled for speed, but the confirmed coordinate
    # mapping is cheap and reliable enough to apply to every loaded row. This
    # prevents long RPLs being silently capped while still letting the range
    # detector reject separate footer/notes blocks.
    coord_rows = _coordinate_rows(grid, profile, grid.n_rows)
    start, end, layout = _detect_range_and_layout(coord_rows, reasons)
    profile.data_start_row, profile.data_end_row = start, end
    profile.layout = layout
    profile.flat_semantics = FLAT_ARRIVING

    profile.header_rows = _header_rows(grid, start) if start else []
    header_texts = (header_texts_for(grid, profile.header_rows)
                    if profile.header_rows else provisional_headers)
    if profile.header_rows:
        reasons["headers"] = (
            "Combined header rows "
            + ", ".join(str(row) for row in profile.header_rows) + ".")
    profile.header_signature = header_signature(header_texts)

    data_rows = [r for r in coord_rows if start <= r <= end]
    seg_rows = []
    if layout == LAYOUT_ALTERNATING:
        seg_rows = [r + 1 for r in data_rows if r + 1 <= end]
    _map_remaining_columns(grid, header_texts, profile,
                           (data_rows + seg_rows) or scan_rows, reasons)
    _detect_units(header_texts, profile, reasons)
    mapped_columns = set(profile.mapping.values())
    profile.excluded_columns = [
        column for column in range(1, grid.n_cols + 1)
        if column not in mapped_columns
    ]

    position_count = len(data_rows)
    confidence = _confidence(profile, position_count, reasons)
    return DetectionResult(profile=profile, position_count=position_count,
                           confidence=confidence, reasons=reasons,
                           header_texts=header_texts)


def _confidence(profile: ImportProfile, positions: int,
                reasons: Dict[str, str]) -> float:
    if positions < 2:
        return 0.0
    score = 0.5
    score += min(positions, 50) / 250.0            # up to +0.2 for body size
    if profile.mapping.get(PF_POS_NO):
        score += 0.1
    if profile.mapping.get(PF_DIST_CUM):
        score += 0.1
    if "layout" in reasons and "100%" in reasons["layout"]:
        score += 0.1
    return min(score, 1.0)


def score_sheets(grids: List[SourceGrid]) -> List[DetectionResult]:
    """Detection for every sheet, best candidate first."""
    results = [detect(grid) for grid in grids]
    results.sort(key=lambda r: (r.position_count, r.confidence), reverse=True)
    return results
