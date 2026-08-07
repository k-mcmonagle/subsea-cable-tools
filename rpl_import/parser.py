# -*- coding: utf-8 -*-
"""SourceGrid + confirmed ImportProfile -> neutral :class:`ImportedRpl`.

Source-row integrity rules (the core fix over the legacy importer):

- Alternating layouts are parsed as logical *slots*: point ``i`` lives on row
  ``start + 2i``, the segment joining points ``i`` and ``i+1`` on row
  ``start + 2i + 1``. Points and segments are never collected into two
  independent lists and zipped after dropping invalid rows, so a bad middle
  point can never shift segment attributes onto the wrong geometry — it
  produces a blocking diagnostic instead.
- Flat layouts parse one position per row; segment fields on the row describe
  the arriving or departing span per ``profile.flat_semantics`` — explicit,
  never assumed.
- A failed optional conversion (bearing, depth, …) yields a diagnostic and
  ``None``; it never terminates the table or zeroes the value.
- The user-confirmed data range is authoritative. Blank rows inside it are
  reported; rows outside it are never read.

Projected coordinates: the pure parser stores raw easting/northing in
``point.extras`` and leaves lat/lon ``None``; the QGIS commit layer runs the
CRS transform and back-fills, because pure Python has no CRS registry.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import coords as C
from .model import (
    COORD_DDM_TEXT, COORD_DECIMAL_DEGREES, COORD_PROJECTED, COORD_SPLIT_DDM,
    Diagnostic, FLAT_ARRIVING, ImportedRpl, ImportPoint, ImportProfile,
    ImportSegment, LAYOUT_ALTERNATING, LAYOUT_FLAT,
    PF_CABLE_DIST_CUM, PF_CHART_NO, PF_DEPTH, PF_DIST_CUM, PF_EASTING,
    PF_EVENT, PF_LAT_DEG, PF_LAT_HEMI, PF_LAT_MIN, PF_LAT_TEXT, PF_LON_DEG,
    PF_LON_HEMI, PF_LON_MIN, PF_LON_TEXT, PF_NORTHING, PF_POS_NO, PF_REMARKS,
    SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING,
    SF_BEARING, SF_BURIAL, SF_CABLE_CODE, SF_CABLE_DIST, SF_CABLE_TYPE,
    SF_DATE_INSTALLED, SF_DIST, SF_EEZ, SF_FIBER_PAIR, SF_LAY_DIRECTION,
    SF_LAY_VESSEL, SF_PROTECTION, SF_SLACK, SF_TARGET_BURIAL, SF_TERRITORIAL,
    extra_field_name, normalise_header_text, slack_to_percent, to_km, to_m,
)
from .reader import SourceGrid

_POINT_TEXT_FIELDS = {PF_EVENT: "event", PF_REMARKS: "remarks"}
_SEG_TEXT_FIELDS = {
    SF_CABLE_CODE: "cable_code", SF_FIBER_PAIR: "fiber_pair",
    SF_CABLE_TYPE: "cable_type", SF_LAY_DIRECTION: "lay_direction",
    SF_LAY_VESSEL: "lay_vessel", SF_PROTECTION: "protection_method",
    SF_TERRITORIAL: "territorial_water", SF_EEZ: "eez",
}


def _cell_text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "nan") else text


def _cell_float(value) -> Tuple[Optional[float], bool]:
    """(value, ok). ok is False only when a non-empty cell fails to convert."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, True
    if isinstance(value, bool):
        return None, False
    if isinstance(value, (int, float)):
        return float(value), True
    text = str(value).strip().replace(",", ".")
    try:
        return float(text), True
    except ValueError:
        return None, False


def parse_point_coords(row_values: List[object], profile: ImportProfile
                       ) -> Tuple[Optional[float], Optional[float], List[str]]:
    """(lat, lon, reasons) for one row under the profile's encoding."""

    def cell(field_key):
        col = profile.mapping.get(field_key) or 0
        return row_values[col - 1] if 1 <= col <= len(row_values) else None

    reasons: List[str] = []
    encoding = profile.coord_encoding
    if encoding == COORD_SPLIT_DDM:
        lat, r1 = C.parse_split_ddm(
            cell(PF_LAT_DEG), cell(PF_LAT_MIN), cell(PF_LAT_HEMI), C.AXIS_LAT)
        lon, r2 = C.parse_split_ddm(
            cell(PF_LON_DEG), cell(PF_LON_MIN), cell(PF_LON_HEMI), C.AXIS_LON)
    elif encoding == COORD_DDM_TEXT:
        lat, r1 = C.parse_ddm_text(cell(PF_LAT_TEXT), C.AXIS_LAT)
        lon, r2 = C.parse_ddm_text(cell(PF_LON_TEXT), C.AXIS_LON)
    elif encoding == COORD_DECIMAL_DEGREES:
        lat, r1 = C.parse_decimal_degrees(cell(PF_LAT_TEXT), C.AXIS_LAT)
        lon, r2 = C.parse_decimal_degrees(cell(PF_LON_TEXT), C.AXIS_LON)
    elif encoding == COORD_PROJECTED:
        east, ok_e = _cell_float(cell(PF_EASTING))
        north, ok_n = _cell_float(cell(PF_NORTHING))
        if east is None or north is None or not (ok_e and ok_n):
            return None, None, ["projected_not_numeric"]
        # transformed later by the QGIS layer; raw values signal success here
        return None, None, []
    else:
        return None, None, ["unknown_encoding"]
    if r1:
        reasons.append("lat:" + r1)
    if r2:
        reasons.append("lon:" + r2)
    if reasons:
        return None, None, reasons
    return lat, lon, reasons


def row_has_coords(grid: SourceGrid, row: int, profile: ImportProfile) -> bool:
    values = grid.row_values(row)
    if profile.coord_encoding == COORD_PROJECTED:
        _, _, reasons = parse_point_coords(values, profile)
        return not reasons
    lat, lon, _ = parse_point_coords(values, profile)
    return lat is not None and lon is not None


def _row_is_blank(grid: SourceGrid, row: int) -> bool:
    return all(_cell_text(v) == "" for v in grid.row_values(row))


class _RowReader:
    """Field access over one source row with per-cell diagnostics."""

    def __init__(self, grid: SourceGrid, row: int, profile: ImportProfile,
                 diagnostics: List[Diagnostic]):
        self.grid = grid
        self.row = row
        self.profile = profile
        self.diagnostics = diagnostics
        self.values = grid.row_values(row)

    def raw(self, field_key: str):
        col = self.profile.mapping.get(field_key) or 0
        return self.values[col - 1] if 1 <= col <= len(self.values) else None

    def text(self, field_key: str) -> str:
        return _cell_text(self.raw(field_key))

    def number(self, field_key: str) -> Optional[float]:
        value, ok = _cell_float(self.raw(field_key))
        if not ok:
            col = self.profile.mapping.get(field_key)
            self.diagnostics.append(Diagnostic(
                rule_id="rpl_import.cell.not_numeric",
                severity=SEVERITY_WARNING,
                message=(f"'{_cell_text(self.raw(field_key))}' is not a number; "
                         f"the value was left empty (never converted to 0)."),
                sheet=self.grid.sheet, row=self.row, column=col, field=field_key,
            ))
        return value


def _mapped_columns(profile: ImportProfile) -> Dict[int, str]:
    return {col: field_key for field_key, col in profile.mapping.items() if col}


def _extras_plan(grid: SourceGrid, profile: ImportProfile
                 ) -> List[Tuple[int, str]]:
    """(column, attribute_name) for columns preserved as extras."""
    mapped = set(_mapped_columns(profile))
    excluded = set(profile.excluded_columns or [])
    taken: set = set()
    plan: List[Tuple[int, str]] = []
    for col in range(1, grid.n_cols + 1):
        if col in mapped or col in excluded:
            continue
        header_bits = [
            _cell_text(grid.cell(r, col)) for r in (profile.header_rows or [])
        ]
        header = " ".join(b for b in header_bits if b)
        if not header and not any(
                _cell_text(grid.cell(r, col))
                for r in range(profile.data_start_row,
                               min(profile.data_end_row,
                                   profile.data_start_row + 40) + 1)):
            continue  # entirely empty column: nothing to preserve
        plan.append((col, extra_field_name(header, col, taken)))
    return plan


def _collect_extras(reader: _RowReader, plan: List[Tuple[int, str]]) -> Dict[str, object]:
    extras: Dict[str, object] = {}
    for col, name in plan:
        value = reader.values[col - 1] if 1 <= col <= len(reader.values) else None
        if value is not None:
            if not isinstance(value, (int, float, bool)):
                value = _cell_text(value)
                if value == "":
                    continue
            extras[name] = value
    return extras


def _read_point(reader: _RowReader, seq: int, profile: ImportProfile,
                extras_plan, diagnostics: List[Diagnostic]) -> ImportPoint:
    grid, row = reader.grid, reader.row
    point = ImportPoint(seq=seq, source_row=row)

    lat, lon, reasons = parse_point_coords(reader.values, profile)
    if profile.coord_encoding == COORD_PROJECTED:
        east, _ = _cell_float(reader.raw(PF_EASTING))
        north, _ = _cell_float(reader.raw(PF_NORTHING))
        point.extras["_easting"] = east
        point.extras["_northing"] = north
        if reasons:
            diagnostics.append(Diagnostic(
                rule_id="rpl_import.point.invalid_coordinates",
                severity=SEVERITY_ERROR,
                message="Easting/northing could not be read as numbers.",
                sheet=grid.sheet, row=row))
    else:
        if lat is None or lon is None:
            diagnostics.append(Diagnostic(
                rule_id="rpl_import.point.invalid_coordinates",
                severity=SEVERITY_ERROR,
                message=("Coordinates could not be parsed (%s). A position row "
                         "inside the data range must have valid coordinates."
                         % ", ".join(reasons or ["empty"])),
                sheet=grid.sheet, row=row))
        point.lat, point.lon = lat, lon

    pos_raw = reader.raw(PF_POS_NO)
    if pos_raw is not None and _cell_text(pos_raw) != "":
        try:
            as_float = float(str(pos_raw).strip())
            if as_float.is_integer():
                point.pos_no = int(as_float)
            else:
                raise ValueError
        except (TypeError, ValueError):
            point.pos_no_raw = _cell_text(pos_raw)
            diagnostics.append(Diagnostic(
                rule_id="rpl_import.point.non_integer_pos_no",
                severity=SEVERITY_WARNING,
                message=(f"Position number '{point.pos_no_raw}' is not an "
                         f"integer; kept as text evidence, PosNo left empty."),
                sheet=grid.sheet, row=row, field=PF_POS_NO))

    point.event = reader.text(PF_EVENT)
    point.remarks = reader.text(PF_REMARKS)
    point.chart_no = reader.text(PF_CHART_NO)
    point.dist_cum_km = to_km(reader.number(PF_DIST_CUM), profile.distance_unit)
    point.cable_dist_cum_km = to_km(
        reader.number(PF_CABLE_DIST_CUM), profile.cable_distance_unit)
    point.depth_m = to_m(reader.number(PF_DEPTH), profile.depth_unit)
    point.extras.update(_collect_extras(reader, extras_plan))
    return point


def _read_segment(reader: _RowReader, seq: int, profile: ImportProfile,
                  extras_plan) -> ImportSegment:
    seg = ImportSegment(seq=seq, source_row=reader.row)
    seg.bearing_deg = reader.number(SF_BEARING)
    seg.dist_km = to_km(reader.number(SF_DIST), profile.distance_unit)
    seg.slack_pct = slack_to_percent(reader.number(SF_SLACK), profile.slack_is_ratio)
    seg.cable_dist_km = to_km(reader.number(SF_CABLE_DIST), profile.cable_distance_unit)
    seg.target_burial_depth_m = to_m(reader.number(SF_TARGET_BURIAL), profile.burial_unit)
    seg.burial_depth_m = to_m(reader.number(SF_BURIAL), profile.burial_unit)
    seg.date_installed = reader.text(SF_DATE_INSTALLED)
    for field_key, attr in _SEG_TEXT_FIELDS.items():
        setattr(seg, attr, reader.text(field_key))
    # Segment rows only contribute extras in alternating layouts; in flat
    # layouts the point on the same row already captured them.
    if profile.layout == LAYOUT_ALTERNATING:
        seg.extras.update(_collect_extras(reader, extras_plan))
    return seg


def parse(grid: SourceGrid, profile: ImportProfile
          ) -> Tuple[ImportedRpl, List[Diagnostic]]:
    """Parse the confirmed data range into an ordered neutral model."""
    diagnostics: List[Diagnostic] = []
    doc = ImportedRpl(sheet=grid.sheet)

    start, end = profile.data_start_row, profile.data_end_row
    if start < 1 or end < start:
        diagnostics.append(Diagnostic(
            rule_id="rpl_import.range.invalid",
            severity=SEVERITY_ERROR,
            message=f"Invalid data range rows {start}-{end}.",
            sheet=grid.sheet))
        return doc, diagnostics

    for (row, col) in grid.formula_gaps(start, end):
        diagnostics.append(Diagnostic(
            rule_id="rpl_import.cell.uncached_formula",
            severity=SEVERITY_WARNING,
            message=("Formula cell has no cached value (workbook saved "
                     "without recalculation); the value reads as empty."),
            sheet=grid.sheet, row=row, column=col))

    extras_plan = _extras_plan(grid, profile)

    if profile.layout == LAYOUT_ALTERNATING:
        _parse_alternating(grid, profile, doc, extras_plan, diagnostics)
    elif profile.layout == LAYOUT_FLAT:
        _parse_flat(grid, profile, doc, extras_plan, diagnostics)
    else:
        diagnostics.append(Diagnostic(
            rule_id="rpl_import.layout.unknown",
            severity=SEVERITY_ERROR,
            message=f"Unknown layout '{profile.layout}'.",
            sheet=grid.sheet))
    return doc, diagnostics


def _parse_alternating(grid: SourceGrid, profile: ImportProfile,
                       doc: ImportedRpl, extras_plan,
                       diagnostics: List[Diagnostic]) -> None:
    start, end = profile.data_start_row, profile.data_end_row
    row = start
    while row <= end:
        if _row_is_blank(grid, row):
            diagnostics.append(Diagnostic(
                rule_id="rpl_import.range.blank_row",
                severity=SEVERITY_WARNING,
                message="Blank row inside the data range was skipped.",
                sheet=grid.sheet, row=row))
            row += 1
            continue
        reader = _RowReader(grid, row, profile, diagnostics)
        point = _read_point(reader, len(doc.points), profile, extras_plan,
                            diagnostics)
        doc.points.append(point)
        # The paired segment row (if any) joins this point to the next one.
        seg_row = row + 1
        if seg_row <= end:
            if _row_is_blank(grid, seg_row):
                diagnostics.append(Diagnostic(
                    rule_id="rpl_import.range.blank_row",
                    severity=SEVERITY_WARNING,
                    message="Blank segment row inside the data range.",
                    sheet=grid.sheet, row=seg_row))
                doc.segments.append(ImportSegment(
                    seq=len(doc.segments), source_row=seg_row))
            else:
                seg_reader = _RowReader(grid, seg_row, profile, diagnostics)
                doc.segments.append(_read_segment(
                    seg_reader, len(doc.segments), profile, extras_plan))
        row += 2

    # An alternating table must end on a point row: n points, n-1 segments.
    if doc.points and len(doc.segments) == len(doc.points):
        trailing = doc.segments.pop()
        if any([trailing.bearing_deg, trailing.dist_km, trailing.slack_pct,
                trailing.cable_dist_km]):
            diagnostics.append(Diagnostic(
                rule_id="rpl_import.layout.trailing_segment",
                severity=SEVERITY_WARNING,
                message=("The data range ends on a segment row; its values "
                         "were discarded because there is no following "
                         "position. Check the data end row."),
                sheet=grid.sheet, row=trailing.source_row))


def _parse_flat(grid: SourceGrid, profile: ImportProfile, doc: ImportedRpl,
                extras_plan, diagnostics: List[Diagnostic]) -> None:
    arriving = (profile.flat_semantics or FLAT_ARRIVING) == FLAT_ARRIVING
    pending: List[Tuple[ImportSegment, int]] = []  # departing spans awaiting next point
    for row in range(profile.data_start_row, profile.data_end_row + 1):
        if _row_is_blank(grid, row):
            diagnostics.append(Diagnostic(
                rule_id="rpl_import.range.blank_row",
                severity=SEVERITY_WARNING,
                message="Blank row inside the data range was skipped.",
                sheet=grid.sheet, row=row))
            continue
        reader = _RowReader(grid, row, profile, diagnostics)
        point = _read_point(reader, len(doc.points), profile, extras_plan,
                            diagnostics)
        doc.points.append(point)
        seg_reader = _RowReader(grid, row, profile, diagnostics)
        seg = _read_segment(seg_reader, 0, profile, extras_plan)
        if arriving:
            # Span data on this row joins the PREVIOUS point to this one.
            if len(doc.points) >= 2:
                seg.seq = len(doc.segments)
                doc.segments.append(seg)
            elif _segment_has_data(seg):
                diagnostics.append(Diagnostic(
                    rule_id="rpl_import.layout.leading_span",
                    severity=SEVERITY_INFO,
                    message=("First row carries arriving-span values with no "
                             "previous position; they were ignored."),
                    sheet=grid.sheet, row=row))
        else:
            # Span data joins THIS point to the next one; attach when it exists.
            if pending:
                prev_seg, _ = pending.pop()
                prev_seg.seq = len(doc.segments)
                doc.segments.append(prev_seg)
            pending.append((seg, row))
    if not arriving and pending:
        seg, row = pending.pop()
        if _segment_has_data(seg):
            diagnostics.append(Diagnostic(
                rule_id="rpl_import.layout.trailing_segment",
                severity=SEVERITY_INFO,
                message=("Last row carries departing-span values with no "
                         "following position; they were ignored."),
                sheet=grid.sheet, row=row))


def _segment_has_data(seg: ImportSegment) -> bool:
    return any(v is not None for v in (
        seg.bearing_deg, seg.dist_km, seg.slack_pct, seg.cable_dist_km,
        seg.target_burial_depth_m, seg.burial_depth_m,
    )) or any(getattr(seg, attr) for attr in (
        "cable_code", "fiber_pair", "cable_type", "lay_direction",
        "lay_vessel", "protection_method", "date_installed",
        "territorial_water", "eez",
    ))
