# -*- coding: utf-8 -*-
"""Persistent per-plan bathymetry samples + slope series (pure python).

The plan profile is sampled once — depth along the scope plus optional
cross-offset depths either side of the route — and persisted in the plan
GeoPackage (``bp_profile``). The profile pane, slope panel and threshold-rule
analysis all read the stored samples; nothing resamples silently. Currency
is judged by fingerprints (route, bathymetry inputs) plus the scope and
cross offset the samples were built with: any mismatch marks the profile
stale and the user chooses when to resample.

Slope conventions (plugin-wide, see README "Slope methodology"):

- longitudinal: signed, positive = shoaling with increasing KP (up-slope);
- cross: signed, positive = deeper to starboard of the direction of
  installation (direction −1 flips the sign, because the vehicle's starboard
  is the other side of the route);
- absolute: magnitude of the combined gradient
  (``atan(sqrt(tan²long + tan²cross))``), never negative.

Depths are stored as magnitudes (the Burial Planner convention); ``None``
marks stations with no data.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import schema

Sample = Tuple[float, Optional[float]]


@dataclass
class PlanProfile:
    """One sampling pass over the plan scope (+ one-step margin)."""

    step_m: float = 0.0
    cross_offset_m: float = 0.0        # 0 = cross depths not sampled
    scope_start_kp: float = 0.0
    scope_end_kp: float = 0.0
    route_fingerprint: str = ""
    depth_fingerprint: str = ""
    sampled_utc: str = ""
    kps: List[float] = field(default_factory=list)
    depths: List[Optional[float]] = field(default_factory=list)
    port_depths: List[Optional[float]] = field(default_factory=list)
    stbd_depths: List[Optional[float]] = field(default_factory=list)
    # Memoised slope series per (half window, direction) — profiles can hold
    # hundreds of thousands of stations, so recomputing per refresh would
    # stall the UI. Not persisted.
    _slope_cache: Dict = field(default_factory=dict, repr=False, compare=False)

    # -- content ------------------------------------------------------------
    @property
    def sample_count(self) -> int:
        return len(self.kps)

    def has_cross(self) -> bool:
        return (self.cross_offset_m > 0
                and any(v is not None for v in self.port_depths)
                and any(v is not None for v in self.stbd_depths))

    def series(self) -> List[Tuple[float, float]]:
        """(kp, depth magnitude) for stations with data — display/analysis."""
        return [(kp, d) for kp, d in zip(self.kps, self.depths) if d is not None]

    def samples(self) -> List[Sample]:
        """(kp, depth|None) for every station — no-data gap detection."""
        return list(zip(self.kps, self.depths))

    def depth_at(self, kp: float) -> Optional[float]:
        """Interpolated depth magnitude at a KP (None outside sampled data).

        The (kp, depth) arrays are cached on first use — the lookup serves
        per-boundary queries (e.g. water-depth-scaled Exclusion Area
        extensions) without rescanning hundreds of thousands of stations.
        """
        cached = self._slope_cache.get("_depth_xy")
        if cached is None:
            cached = _valid_pairs(self.kps, self.depths)
            self._slope_cache["_depth_xy"] = cached
        xs, ys = cached
        return _interp(xs, ys, float(kp))

    def slope_series(self, half_window_km: float, direction: int
                     ) -> Tuple[List[Sample], List[Sample], List[Sample]]:
        """(longitudinal, cross, absolute) slope series, memoised."""
        key = (round(float(half_window_km), 9),
               1 if int(direction or 1) >= 0 else -1)
        cached = self._slope_cache.get(key)
        if cached is not None:
            return cached
        long_series = long_slope_series(self.kps, self.depths, half_window_km)
        cross_series = cross_slope_series(
            self.kps, self.port_depths, self.stbd_depths,
            self.cross_offset_m, direction) if self.has_cross() else []
        abs_series = absolute_slope_series(long_series, cross_series)
        result = (long_series, cross_series, abs_series)
        self._slope_cache[key] = result
        return result

    # -- currency -------------------------------------------------------------
    def is_current(self, route_fingerprint: str, depth_fingerprint: str,
                   scope_start_kp: float, scope_end_kp: float,
                   cross_offset_m: float) -> bool:
        return (bool(self.kps)
                and self.route_fingerprint == (route_fingerprint or "")
                and self.depth_fingerprint == (depth_fingerprint or "")
                and abs(self.scope_start_kp - float(scope_start_kp)) < 1e-6
                and abs(self.scope_end_kp - float(scope_end_kp)) < 1e-6
                and abs(self.cross_offset_m - float(cross_offset_m)) < 1e-6)

    # -- persistence ----------------------------------------------------------
    def to_row(self, plan_id: str, profile_id: str = "") -> Dict:
        params = {
            "step_m": self.step_m,
            "cross_offset_m": self.cross_offset_m,
            "scope_start_kp": self.scope_start_kp,
            "scope_end_kp": self.scope_end_kp,
            "route_fingerprint": self.route_fingerprint,
            "depth_fingerprint": self.depth_fingerprint,
            "sampled_utc": self.sampled_utc,
        }

        def compact(values: List[Optional[float]], places: int) -> List:
            return [None if v is None else round(float(v), places)
                    for v in values]

        samples = {
            "kps": compact(self.kps, 6),
            "depths": compact(self.depths, 3),
            "port": compact(self.port_depths, 3),
            "stbd": compact(self.stbd_depths, 3),
        }
        return {
            "profile_id": profile_id or schema.new_id(),
            "plan_id": plan_id,
            "created_utc": self.sampled_utc or schema.utc_now_iso(),
            "params_json": json.dumps(params, separators=(",", ":")),
            "samples_json": json.dumps(samples, separators=(",", ":")),
            "sample_count": len(self.kps),
        }

    @classmethod
    def from_row(cls, row: Optional[Dict]) -> Optional["PlanProfile"]:
        if not row:
            return None
        try:
            params = json.loads(row.get("params_json") or "{}")
            samples = json.loads(row.get("samples_json") or "{}")
        except (ValueError, TypeError):
            return None
        if not isinstance(params, dict) or not isinstance(samples, dict):
            return None
        kps = [float(v) for v in (samples.get("kps") or [])]

        def floats(key: str) -> List[Optional[float]]:
            values = samples.get(key) or []
            out = [None if v is None else float(v) for v in values]
            out.extend([None] * (len(kps) - len(out)))
            return out[:len(kps)]

        return cls(
            step_m=float(params.get("step_m") or 0.0),
            cross_offset_m=float(params.get("cross_offset_m") or 0.0),
            scope_start_kp=float(params.get("scope_start_kp") or 0.0),
            scope_end_kp=float(params.get("scope_end_kp") or 0.0),
            route_fingerprint=str(params.get("route_fingerprint") or ""),
            depth_fingerprint=str(params.get("depth_fingerprint") or ""),
            sampled_utc=str(params.get("sampled_utc") or ""),
            kps=kps,
            depths=floats("depths"),
            port_depths=floats("port"),
            stbd_depths=floats("stbd"),
        )


# ---------------------------------------------------------------------------
# Slope series
# ---------------------------------------------------------------------------


def _valid_pairs(kps: List[float], depths: List[Optional[float]]
                 ) -> Tuple[List[float], List[float]]:
    xs, ys = [], []
    for kp, depth in zip(kps, depths):
        if depth is not None:
            xs.append(float(kp))
            ys.append(float(depth))
    return xs, ys


def _interp(xs: List[float], ys: List[float], kp: float) -> Optional[float]:
    """Linear interpolation; None outside the sampled range."""
    if not xs or kp < xs[0] - 1e-9 or kp > xs[-1] + 1e-9:
        return None
    index = bisect.bisect_left(xs, kp)
    if index < len(xs) and abs(xs[index] - kp) <= 1e-9:
        return ys[index]
    if index == 0 or index >= len(xs):
        return None
    x0, x1 = xs[index - 1], xs[index]
    if x1 - x0 <= 1e-12:
        return ys[index]
    t = (kp - x0) / (x1 - x0)
    return ys[index - 1] + t * (ys[index] - ys[index - 1])


def long_slope_series(kps: List[float], depths: List[Optional[float]],
                      half_window_km: float) -> List[Sample]:
    """Signed longitudinal slope (°) per station; +ve = up-slope.

    Central difference of interpolated depth magnitudes at kp ± the half
    window (the analysis-step / vehicle-footprint convention), clamped to
    the sampled range so edge stations use the available window.
    """
    xs, ys = _valid_pairs(kps, depths)
    out: List[Sample] = []
    half = max(float(half_window_km), 1e-9)
    for kp in kps:
        if not xs:
            out.append((kp, None))
            continue
        k0 = max(xs[0], kp - half)
        k1 = min(xs[-1], kp + half)
        dx_m = (k1 - k0) * 1000.0
        if dx_m <= 1e-6:
            out.append((kp, None))
            continue
        d0 = _interp(xs, ys, k0)
        d1 = _interp(xs, ys, k1)
        if d0 is None or d1 is None:
            out.append((kp, None))
            continue
        # Depth magnitudes: negate so positive = shoaling (up-slope).
        out.append((kp, math.degrees(math.atan2(-(d1 - d0), dx_m))))
    return out


def cross_slope_series(kps: List[float],
                       port_depths: List[Optional[float]],
                       stbd_depths: List[Optional[float]],
                       cross_offset_m: float,
                       direction: int = 1) -> List[Sample]:
    """Signed cross slope (°); +ve = deeper to starboard of travel.

    Two-point difference across ± the cross offset. Depths are magnitudes,
    so starboard deeper than port gives a positive slope for direction +1;
    installing against KP swaps the vehicle's port/starboard, so the sign
    flips for direction −1.
    """
    out: List[Sample] = []
    span_m = 2.0 * max(float(cross_offset_m), 1e-9)
    sign = -1.0 if int(direction or 1) < 0 else 1.0
    for kp, port, stbd in zip(kps, port_depths, stbd_depths):
        if port is None or stbd is None:
            out.append((kp, None))
            continue
        out.append((kp, sign * math.degrees(
            math.atan2(float(stbd) - float(port), span_m))))
    return out


SLOPE_COMPONENT_LONG = "long"
SLOPE_COMPONENT_CROSS = "cross"
SLOPE_COMPONENT_ABSOLUTE = "absolute"
SLOPE_COMPONENTS = (SLOPE_COMPONENT_LONG, SLOPE_COMPONENT_CROSS,
                    SLOPE_COMPONENT_ABSOLUTE)
SLOPE_COMPONENT_LABELS = {
    SLOPE_COMPONENT_LONG: "Longitudinal (along route)",
    SLOPE_COMPONENT_CROSS: "Cross (across route)",
    SLOPE_COMPONENT_ABSOLUTE: "Absolute (combined gradient)",
}


def slope_component_series(kps: List[float], depths: List[Optional[float]],
                           port_depths: List[Optional[float]],
                           stbd_depths: List[Optional[float]],
                           cross_offset_m: float, direction: int,
                           component: str,
                           half_window_km: float) -> List[Sample]:
    """(kp, value|None) series for one slope component, for criteria checks.

    ``long`` is signed (+ve = up-slope) so directional limits apply; ``cross``
    is reported as a magnitude (a limit catches leaning either way);
    ``absolute`` is the combined-gradient magnitude, matching the profile
    pane (|longitudinal| where cross samples are missing — a lower bound).
    ``half_window_km`` scales the longitudinal difference; cross is always
    the two-point difference across the sampled ± cross offset.
    """
    long_series = long_slope_series(kps, depths, half_window_km)
    if component == SLOPE_COMPONENT_LONG:
        return long_series
    cross_series = cross_slope_series(kps, port_depths, stbd_depths,
                                      cross_offset_m, direction)
    if component == SLOPE_COMPONENT_CROSS:
        return [(kp, None if value is None else abs(value))
                for kp, value in cross_series]
    if component == SLOPE_COMPONENT_ABSOLUTE:
        return absolute_slope_series(long_series, cross_series)
    raise ValueError(f"unknown slope component '{component}'")


def absolute_slope_series(long_series: List[Sample],
                          cross_series: List[Sample]) -> List[Sample]:
    """Magnitude of the combined gradient per station (°), never negative.

    Where cross slope is unavailable the longitudinal magnitude is reported
    (a lower bound on the true absolute slope).
    """
    cross_by_kp = {kp: value for kp, value in cross_series}
    out: List[Sample] = []
    for kp, long_deg in long_series:
        if long_deg is None:
            out.append((kp, None))
            continue
        cross_deg = cross_by_kp.get(kp)
        if cross_deg is None:
            out.append((kp, abs(long_deg)))
            continue
        gradient = math.hypot(math.tan(math.radians(long_deg)),
                              math.tan(math.radians(cross_deg)))
        out.append((kp, math.degrees(math.atan(gradient))))
    return out
