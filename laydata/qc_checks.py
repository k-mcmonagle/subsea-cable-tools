# -*- coding: utf-8 -*-
"""Phase-1 cable-lay QC checks.

Each check is a small, self-describing :class:`QcCheck` subclass. New checks
(tension limits, position jumps, roto jumps, ...) slot in here and register via
:data:`ALL_CHECKS`; the Explorer UI and the processing algorithm both build
their inputs from the checks' :meth:`param_specs`, so nothing else needs editing.

All bulk work is vectorised numpy so the checks stay fast on large raw datasets.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

import numpy as np

from .dataset import LayDataset, haversine_m
from .qc_base import Finding, ParamSpec, QcCheck, Severity


def _format_seconds(seconds: float) -> str:
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{minutes:.1f} min"
    return f"{minutes / 60.0:.2f} h"


class TimeGapCheck(QcCheck):
    """Flag jumps in the record timestamps larger than the expected interval."""

    check_id = "time_gap"
    name = "Time gaps"
    description = (
        "Detects breaks in logging where the time between consecutive records "
        "exceeds the expected interval. Set the interval to 0 to auto-detect it "
        "(median spacing) per source - handles 1 s, 30 s or any cadence."
    )

    @classmethod
    def param_specs(cls) -> List[ParamSpec]:
        return [
            ParamSpec("expected_interval_s", "Expected interval (s, 0 = auto)", "float", 0.0, minimum=0.0),
            ParamSpec("gap_factor", "Gap factor (x interval)", "float", 1.5, minimum=1.0),
            ParamSpec("min_gap_s", "Minimum gap to report (s)", "float", 0.0, minimum=0.0),
            ParamSpec("max_findings", "Max findings", "int", 1000, minimum=1),
        ]

    def applicable(self, dataset: LayDataset) -> bool:
        return dataset.has_time

    def run(self, dataset: LayDataset, params: Optional[Dict[str, Any]] = None) -> List[Finding]:
        p = self.resolve_params(params)
        expected = float(p["expected_interval_s"])
        factor = float(p["gap_factor"])
        min_gap = float(p["min_gap_s"])
        cap = int(p["max_findings"])

        epoch = dataset.time_epoch
        findings: List[Finding] = []
        for source, indices in dataset.iter_source_groups(order_by_time=True):
            if indices.size < 2:
                continue
            times = epoch[indices]
            deltas = np.diff(times)
            positive = deltas[deltas > 0]
            interval = expected if expected > 0 else (float(np.median(positive)) if positive.size else 0.0)
            if interval <= 0:
                continue
            threshold = max(interval * factor, min_gap)
            gap_positions = np.nonzero(deltas > threshold)[0]
            for pos in gap_positions:
                if len(findings) >= cap:
                    return findings
                before = int(indices[pos])
                after = int(indices[pos + 1])
                gap = float(deltas[pos])
                lat = dataset.lat[before] if dataset.has_geometry else None
                lon = dataset.lon[before] if dataset.has_geometry else None
                findings.append(
                    Finding(
                        check_id=self.check_id,
                        severity=Severity.WARNING,
                        message=(
                            f"Time gap of {_format_seconds(gap)} "
                            f"(expected ~{_format_seconds(interval)})"
                            + (f" in {source}" if source else "")
                        ),
                        value=gap,
                        threshold=threshold,
                        time_start=dataset.iso_time_at(before),
                        time_end=dataset.iso_time_at(after),
                        lat=None if lat is None else float(lat),
                        lon=None if lon is None else float(lon),
                        source_file=source or None,
                        feature_fid=int(dataset.fids[before]),
                    )
                )
        return findings


class DistanceGapCheck(QcCheck):
    """Flag consecutive records separated by more than a distance threshold."""

    check_id = "distance_gap"
    name = "Distance gaps"
    description = (
        "Detects breaks along track where the straight-line distance between "
        "consecutive records exceeds a limit - useful when logging is by "
        "distance rather than time, or to catch positional dropouts."
    )

    @classmethod
    def param_specs(cls) -> List[ParamSpec]:
        return [
            ParamSpec("max_spacing_m", "Maximum spacing (m)", "float", 50.0, minimum=0.0),
            ParamSpec("max_findings", "Max findings", "int", 1000, minimum=1),
        ]

    def applicable(self, dataset: LayDataset) -> bool:
        return dataset.has_geometry

    def run(self, dataset: LayDataset, params: Optional[Dict[str, Any]] = None) -> List[Finding]:
        p = self.resolve_params(params)
        limit = float(p["max_spacing_m"])
        cap = int(p["max_findings"])

        lat = dataset.lat
        lon = dataset.lon
        findings: List[Finding] = []
        order_by_time = dataset.has_time
        for source, indices in dataset.iter_source_groups(order_by_time=order_by_time):
            if indices.size < 2:
                continue
            la = lat[indices]
            lo = lon[indices]
            valid = np.isfinite(la) & np.isfinite(lo)
            if not np.all(valid):
                indices = indices[valid]
                la = la[valid]
                lo = lo[valid]
            if indices.size < 2:
                continue
            dist = haversine_m(la[:-1], lo[:-1], la[1:], lo[1:])
            gap_positions = np.nonzero(dist > limit)[0]
            for pos in gap_positions:
                if len(findings) >= cap:
                    return findings
                before = int(indices[pos])
                after = int(indices[pos + 1])
                spacing = float(dist[pos])
                findings.append(
                    Finding(
                        check_id=self.check_id,
                        severity=Severity.WARNING,
                        message=(
                            f"Position jump of {spacing:.1f} m between records"
                            + (f" in {source}" if source else "")
                        ),
                        value=spacing,
                        threshold=limit,
                        time_start=dataset.iso_time_at(before),
                        time_end=dataset.iso_time_at(after),
                        lat=float(la[pos]),
                        lon=float(lo[pos]),
                        source_file=source or None,
                        feature_fid=int(dataset.fids[before]),
                    )
                )
        return findings


class DecimalPrecisionCheck(QcCheck):
    """Verify a numeric field is stored to an expected number of decimal places."""

    check_id = "decimal_precision"
    name = "Decimal precision"
    description = (
        "Checks that a numeric field (e.g. KP) carries the expected number of "
        "decimal places. Records with more precision than expected are flagged."
    )

    @classmethod
    def param_specs(cls) -> List[ParamSpec]:
        return [
            ParamSpec("field", "Field to check", "field", "", help="Numeric field, e.g. a KP column."),
            ParamSpec("expected_dp", "Expected decimal places", "int", 3, minimum=0, maximum=12),
            ParamSpec("tolerance", "Match tolerance", "float", 1e-6, minimum=0.0),
            ParamSpec("max_findings", "Max per-record findings", "int", 50, minimum=0),
        ]

    def applicable(self, dataset: LayDataset) -> bool:
        return dataset.row_count > 0

    def run(self, dataset: LayDataset, params: Optional[Dict[str, Any]] = None) -> List[Finding]:
        p = self.resolve_params(params)
        field = str(p["field"] or "").strip()
        if not field or not dataset.has_field(field):
            return []
        dp = int(p["expected_dp"])
        tol = float(p["tolerance"])
        cap = int(p["max_findings"])

        values = dataset.numeric(field)
        finite = np.isfinite(values)
        if not np.any(finite):
            return []
        rounded = np.round(values, dp)
        excess = np.abs(values - rounded)
        flagged = finite & (excess > tol)
        flagged_positions = np.nonzero(flagged)[0]
        n_flagged = int(flagged_positions.size)

        findings: List[Finding] = []
        severity = Severity.WARNING if n_flagged else Severity.INFO
        findings.append(
            Finding(
                check_id=self.check_id,
                severity=severity,
                message=(
                    f"'{field}': {n_flagged} of {int(np.count_nonzero(finite))} value(s) "
                    f"have more than {dp} decimal place(s)"
                    if n_flagged
                    else f"'{field}': all values are within {dp} decimal place(s)"
                ),
                value=float(n_flagged),
                threshold=float(dp),
                count=max(1, n_flagged),
            )
        )
        for pos in flagged_positions[:cap]:
            idx = int(pos)
            lat = dataset.lat[idx] if dataset.has_geometry else None
            lon = dataset.lon[idx] if dataset.has_geometry else None
            findings.append(
                Finding(
                    check_id=self.check_id,
                    severity=Severity.WARNING,
                    message=f"'{field}' = {values[idx]!r} exceeds {dp} dp",
                    value=float(values[idx]),
                    threshold=float(dp),
                    time_start=dataset.iso_time_at(idx),
                    lat=None if lat is None else float(lat),
                    lon=None if lon is None else float(lon),
                    source_file=dataset.source_at(idx),
                    feature_fid=int(dataset.fids[idx]),
                )
            )
        return findings


class DuplicateCheck(QcCheck):
    """Flag duplicate records by timestamp (optionally also by position)."""

    check_id = "duplicate"
    name = "Duplicates"
    description = (
        "Detects repeated records within a source: identical timestamps, or "
        "identical timestamp and position. Duplicates are candidates for removal."
    )

    @classmethod
    def param_specs(cls) -> List[ParamSpec]:
        return [
            ParamSpec(
                "mode",
                "Duplicate definition",
                "choice",
                "time",
                choices=["time", "time_position"],
                help="'time' = same timestamp; 'time_position' = same timestamp and lat/lon.",
            ),
            ParamSpec("position_dp", "Position rounding (dp)", "int", 6, minimum=0, maximum=9),
            ParamSpec("max_findings", "Max findings", "int", 1000, minimum=1),
        ]

    def applicable(self, dataset: LayDataset) -> bool:
        return dataset.has_time

    def run(self, dataset: LayDataset, params: Optional[Dict[str, Any]] = None) -> List[Finding]:
        p = self.resolve_params(params)
        mode = str(p["mode"])
        pos_dp = int(p["position_dp"])
        cap = int(p["max_findings"])
        use_position = mode == "time_position" and dataset.has_geometry

        findings: List[Finding] = []
        for source, indices in dataset.iter_source_groups(order_by_time=True):
            if indices.size < 2:
                continue
            seen: Dict[Any, int] = {}
            for idx in indices:
                idx = int(idx)
                iso = dataset.iso_time_at(idx)
                if use_position:
                    lat = dataset.lat[idx]
                    lon = dataset.lon[idx]
                    key = (iso, round(float(lat), pos_dp), round(float(lon), pos_dp))
                else:
                    key = iso
                if key in seen:
                    if len(findings) >= cap:
                        return findings
                    lat = dataset.lat[idx] if dataset.has_geometry else None
                    lon = dataset.lon[idx] if dataset.has_geometry else None
                    findings.append(
                        Finding(
                            check_id=self.check_id,
                            severity=Severity.WARNING,
                            message=(
                                f"Duplicate record at {iso}"
                                + (" (same position)" if use_position else "")
                                + (f" in {source}" if source else "")
                            ),
                            time_start=iso,
                            lat=None if lat is None else float(lat),
                            lon=None if lon is None else float(lon),
                            source_file=source or None,
                            feature_fid=int(dataset.fids[idx]),
                            count=2,
                        )
                    )
                else:
                    seen[key] = idx
        return findings


# Registry -----------------------------------------------------------------
ALL_CHECKS: List[Type[QcCheck]] = [
    TimeGapCheck,
    DistanceGapCheck,
    DecimalPrecisionCheck,
    DuplicateCheck,
]


def checks_by_id() -> Dict[str, Type[QcCheck]]:
    return {cls.check_id: cls for cls in ALL_CHECKS}


def make_check(check_id: str) -> QcCheck:
    cls = checks_by_id().get(check_id)
    if cls is None:
        raise KeyError(f"Unknown QC check id: {check_id}")
    return cls()
