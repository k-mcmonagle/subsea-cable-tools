# -*- coding: utf-8 -*-
"""Validation/analysis of a parsed :class:`ImportedRpl`.

Every finding has a stable rule ID (``rpl_import.*``), severity, and source
row context so the wizard can navigate back to the offending cell. Errors
block import; warnings need visible acknowledgement.

Geodesy is injected: ``distance_km_fn(lat1, lon1, lat2, lon2)`` should be the
QGIS ellipsoidal measure when running inside QGIS. Without one, a mean-sphere
haversine is used — fine for the documented tolerances, which are sized for
rounded RPL documents, not for comparing geodesy libraries.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from .coords import circular_diff_deg, haversine_km, initial_bearing_deg
from .model import (
    Diagnostic, ImportedRpl, SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING,
)

# ---------------------------------------------------------------------------
# Named tolerances (documented, tested — not magic literals in UI code)
# ---------------------------------------------------------------------------
#: Stated vs computed span distance: flag when BOTH exceeded.
DIST_ABS_TOL_KM = 0.05
DIST_REL_TOL = 0.02
#: Stated vs computed bearing (circular difference).
BEARING_TOL_DEG = 5.0
#: Bearing comparison is meaningless over very short spans.
BEARING_MIN_SPAN_KM = 0.2
#: Segment slack sanity bounds (percent).
SLACK_MIN_PCT = -5.0
SLACK_MAX_PCT = 100.0
#: Consecutive positions closer than this are "coincident".
COINCIDENT_TOL_KM = 0.001
#: A single span longer than this is suspicious for a cable RPL.
JUMP_MAX_KM = 400.0
#: cable = route * (1 + slack/100) consistency check.
SLACK_CONSISTENCY_TOL_KM = 0.05
#: Cumulative KP vs sum-of-spans drift.
CUMULATIVE_TOL_KM = 0.1
#: Sign-outlier check: a coordinate whose sign differs from BOTH neighbours
#: while |value| exceeds this is flagged (legitimate equator/meridian
#: crossings have small magnitudes on at least one side).
SIGN_OUTLIER_MIN_DEG = 1.0

DistanceFn = Callable[[float, float, float, float], float]
BearingFn = Callable[[float, float, float, float], float]


def validate(doc: ImportedRpl,
             distance_km_fn: Optional[DistanceFn] = None,
             bearing_deg_fn: Optional[BearingFn] = None) -> List[Diagnostic]:
    """Run all checks; parser diagnostics are separate and come first."""
    dist_fn = distance_km_fn or haversine_km
    bear_fn = bearing_deg_fn or initial_bearing_deg
    out: List[Diagnostic] = []
    sheet = doc.sheet

    def add(rule: str, severity: str, message: str, row=None, field="",
            suggestion=""):
        out.append(Diagnostic(rule_id=rule, severity=severity, message=message,
                              sheet=sheet, row=row, field=field,
                              suggestion=suggestion))

    points, segments = doc.points, doc.segments

    if len([p for p in points if p.lat is not None and p.lon is not None]) < 2:
        add("rpl_import.too_few_points", SEVERITY_ERROR,
            "An RPL needs at least two positions with valid coordinates.")
        return out

    if len(segments) != len(points) - 1:
        add("rpl_import.segment_count", SEVERITY_ERROR,
            f"{len(points)} positions require {len(points) - 1} segments, "
            f"parsed {len(segments)}. Check the layout and data range.")

    _check_pos_numbers(points, add)
    _check_coordinates(points, add)
    _check_sign_outliers(points, add)
    _check_monotonic(points, add)
    _check_spans(doc, dist_fn, bear_fn, add)
    _check_cumulative(doc, add)
    return out


# ---------------------------------------------------------------------------
def _check_pos_numbers(points, add) -> None:
    seen = {}
    for point in points:
        if point.pos_no is None:
            if not point.pos_no_raw:
                add("rpl_import.pos_no.missing", SEVERITY_INFO,
                    "Position has no document position number.",
                    row=point.source_row, field="pos_no")
            continue
        if point.pos_no in seen:
            add("rpl_import.pos_no.duplicate", SEVERITY_WARNING,
                f"Position number {point.pos_no} appears more than once "
                f"(also row {seen[point.pos_no]}).",
                row=point.source_row, field="pos_no")
        seen.setdefault(point.pos_no, point.source_row)


def _check_coordinates(points, add) -> None:
    for point in points:
        if point.lat is None or point.lon is None:
            continue
        if not (-90.0 <= point.lat <= 90.0) or not (-180.0 <= point.lon <= 180.0):
            add("rpl_import.coordinate_range", SEVERITY_ERROR,
                f"Coordinates ({point.lat:.6f}, {point.lon:.6f}) are out of "
                f"range.", row=point.source_row)


def _check_sign_outliers(points, add) -> None:
    """Isolated sign flips relative to both neighbours (skip real crossings)."""
    for axis in ("lat", "lon"):
        values = [(getattr(p, axis), p.source_row) for p in points
                  if getattr(p, axis) is not None]
        for i in range(1, len(values) - 1):
            value, row = values[i]
            prev_v, next_v = values[i - 1][0], values[i + 1][0]
            if (abs(value) >= SIGN_OUTLIER_MIN_DEG
                    and abs(prev_v) >= SIGN_OUTLIER_MIN_DEG
                    and abs(next_v) >= SIGN_OUTLIER_MIN_DEG
                    and (value > 0) != (prev_v > 0)
                    and (value > 0) != (next_v > 0)):
                add("rpl_import.coordinate_sign_outlier", SEVERITY_WARNING,
                    f"{axis.capitalize()} sign differs from both neighbours "
                    f"({prev_v:.3f} → {value:.3f} → {next_v:.3f}); possible "
                    f"hemisphere typo.", row=row,
                    suggestion="Review the hemisphere/sign for this row; the "
                               "wizard can flip it for you.")


def _check_monotonic(points, add) -> None:
    for attr, rule, label in (
            ("dist_cum_km", "rpl_import.kp.non_monotonic", "Stated KP"),
            ("cable_dist_cum_km", "rpl_import.cable_cum.non_monotonic",
             "Stated cumulative cable distance")):
        prev = None
        for point in points:
            value = getattr(point, attr)
            if value is None:
                continue
            if prev is not None and value < prev[0] - 1e-9:
                add(rule, SEVERITY_WARNING,
                    f"{label} decreases from {prev[0]:.3f} to {value:.3f} km "
                    f"(row {prev[1]} → row {point.source_row}).",
                    row=point.source_row)
            prev = (value, point.source_row)


def _check_spans(doc, dist_fn, bear_fn, add) -> None:
    points, segments = doc.points, doc.segments
    n = min(len(segments), len(points) - 1)
    for i in range(n):
        a, b, seg = points[i], points[i + 1], segments[i]
        if None in (a.lat, a.lon, b.lat, b.lon):
            continue
        computed_km = dist_fn(a.lat, a.lon, b.lat, b.lon)

        if computed_km <= COINCIDENT_TOL_KM:
            add("rpl_import.coincident_points", SEVERITY_WARNING,
                f"Positions on rows {a.source_row} and {b.source_row} are "
                f"coincident ({computed_km * 1000:.1f} m apart).",
                row=b.source_row)
        elif computed_km > JUMP_MAX_KM:
            add("rpl_import.implausible_jump", SEVERITY_WARNING,
                f"Span between rows {a.source_row} and {b.source_row} is "
                f"{computed_km:.1f} km — check for a coordinate error.",
                row=b.source_row)

        if seg.dist_km is not None:
            diff = abs(seg.dist_km - computed_km)
            if diff > DIST_ABS_TOL_KM and (
                    computed_km <= 0 or diff / computed_km > DIST_REL_TOL):
                add("rpl_import.distance_mismatch", SEVERITY_WARNING,
                    f"Stated span distance {seg.dist_km:.3f} km differs from "
                    f"the computed geodesic {computed_km:.3f} km "
                    f"(Δ {diff:.3f} km).", row=seg.source_row, field="dist")

        if (seg.bearing_deg is not None and computed_km >= BEARING_MIN_SPAN_KM):
            computed_bearing = bear_fn(a.lat, a.lon, b.lat, b.lon)
            delta = circular_diff_deg(seg.bearing_deg, computed_bearing)
            if delta > BEARING_TOL_DEG:
                add("rpl_import.bearing_mismatch", SEVERITY_WARNING,
                    f"Stated bearing {seg.bearing_deg:.1f}° differs from the "
                    f"computed {computed_bearing:.1f}° (Δ {delta:.1f}°).",
                    row=seg.source_row, field="bearing")

        if seg.slack_pct is not None and not (
                SLACK_MIN_PCT <= seg.slack_pct <= SLACK_MAX_PCT):
            add("rpl_import.slack_implausible", SEVERITY_WARNING,
                f"Slack {seg.slack_pct:.2f}% is outside the plausible range "
                f"({SLACK_MIN_PCT}% to {SLACK_MAX_PCT}%).",
                row=seg.source_row, field="slack")

        if (seg.slack_pct is not None and seg.dist_km is not None
                and seg.cable_dist_km is not None):
            implied = seg.dist_km * (1.0 + seg.slack_pct / 100.0)
            if abs(implied - seg.cable_dist_km) > SLACK_CONSISTENCY_TOL_KM:
                add("rpl_import.slack_inconsistent", SEVERITY_WARNING,
                    f"Route {seg.dist_km:.3f} km with slack "
                    f"{seg.slack_pct:.2f}% implies cable "
                    f"{implied:.3f} km, but the document states "
                    f"{seg.cable_dist_km:.3f} km.",
                    row=seg.source_row, field="cable_dist")


def _check_cumulative(doc, add) -> None:
    points, segments = doc.points, doc.segments
    n = min(len(segments), len(points) - 1)
    for attr, seg_attr, rule, label in (
            ("dist_cum_km", "dist_km", "rpl_import.kp.cumulative_mismatch",
             "KP"),
            ("cable_dist_cum_km", "cable_dist_km",
             "rpl_import.cable_cum.cumulative_mismatch", "cable distance")):
        for i in range(n):
            a, b, seg = points[i], points[i + 1], segments[i]
            start, end = getattr(a, attr), getattr(b, attr)
            span = getattr(seg, seg_attr)
            if None in (start, end, span):
                continue
            if abs((end - start) - span) > CUMULATIVE_TOL_KM:
                add(rule, SEVERITY_WARNING,
                    f"Cumulative {label} step {end - start:.3f} km between "
                    f"rows {a.source_row} and {b.source_row} does not match "
                    f"the stated span {span:.3f} km.", row=b.source_row)
