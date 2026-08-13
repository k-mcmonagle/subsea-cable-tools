# -*- coding: utf-8 -*-
"""Checks for the persisted plan profile + slope series (pure python)."""

from __future__ import annotations

import math

from ..burial.profile_data import (
    PlanProfile,
    absolute_slope_series,
    cross_slope_series,
    long_slope_series,
)


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _profile() -> PlanProfile:
    kps = [0.0, 0.1, 0.2, 0.3, 0.4]
    return PlanProfile(
        step_m=100.0, cross_offset_m=50.0,
        scope_start_kp=0.0, scope_end_kp=0.4,
        route_fingerprint="route-fp", depth_fingerprint="depth-fp",
        sampled_utc="2026-08-13T10:00:00Z",
        kps=kps,
        depths=[100.0, 110.0, None, 130.0, 140.0],
        port_depths=[100.0, 105.0, None, 120.0, 130.0],
        stbd_depths=[102.0, 109.0, None, 128.0, 130.0],
    )


def test_row_round_trip() -> bool:
    profile = _profile()
    row = profile.to_row("plan-1")
    ok = row["plan_id"] == "plan-1" and row["sample_count"] == 5
    back = PlanProfile.from_row(row)
    ok = ok and back is not None
    ok = ok and back.kps == profile.kps
    ok = ok and back.depths == profile.depths
    ok = ok and back.port_depths == profile.port_depths
    ok = ok and back.stbd_depths == profile.stbd_depths
    ok = ok and back.route_fingerprint == "route-fp"
    ok = ok and back.step_m == 100.0 and back.cross_offset_m == 50.0
    ok = ok and back.has_cross()
    ok = ok and back.series() == [(0.0, 100.0), (0.1, 110.0),
                                  (0.3, 130.0), (0.4, 140.0)]
    ok = ok and PlanProfile.from_row(None) is None
    ok = ok and PlanProfile.from_row({"params_json": "not json"}) is None
    return _result("plan profile row round trip (gaps preserved)", ok)


def test_currency() -> bool:
    profile = _profile()
    base = ("route-fp", "depth-fp", 0.0, 0.4, 50.0)
    ok = profile.is_current(*base)
    ok = ok and not profile.is_current("other", "depth-fp", 0.0, 0.4, 50.0)
    ok = ok and not profile.is_current("route-fp", "other", 0.0, 0.4, 50.0)
    ok = ok and not profile.is_current("route-fp", "depth-fp", 0.0, 0.5, 50.0)
    ok = ok and not profile.is_current("route-fp", "depth-fp", 0.0, 0.4, 60.0)
    empty = PlanProfile(route_fingerprint="route-fp",
                        depth_fingerprint="depth-fp", scope_end_kp=0.4,
                        cross_offset_m=50.0)
    ok = ok and not empty.is_current(*base)
    return _result("profile currency: fingerprints, scope, cross offset", ok)


def test_long_slope_sign_and_gaps() -> bool:
    kps = [0.0, 0.1, 0.2, 0.3, 0.4]
    deepening = [100.0, 110.0, 120.0, 130.0, 140.0]   # magnitudes increase
    series = long_slope_series(kps, deepening, half_window_km=0.1)
    values = [v for _kp, v in series]
    # Deepening with KP = down-slope = negative (plugin convention).
    ok = all(v is not None and v < 0 for v in values)
    expected = math.degrees(math.atan2(-10.0, 100.0))  # 10 m per 100 m
    ok = ok and abs(values[2] - expected) < 0.15
    shoaling = list(reversed(deepening))
    up = long_slope_series(kps, shoaling, half_window_km=0.1)
    ok = ok and all(v is not None and v > 0 for _kp, v in up)
    # Interior gaps are bridged by interpolation (same as the crosshair);
    # the bridged slope still reports the deepening trend.
    gappy = long_slope_series(kps, [100.0, None, None, None, 140.0], 0.05)
    ok = ok and gappy[2][1] is not None and gappy[2][1] < 0
    # A single data point offers no window at all -> None everywhere.
    lone = long_slope_series(kps, [None, None, 120.0, None, None], 0.05)
    ok = ok and all(v is None for _kp, v in lone)
    ok = ok and long_slope_series([], [], 0.1) == []
    return _result("longitudinal slope: +ve = up-slope, gaps -> None", ok)


def test_cross_slope_sign_and_direction() -> bool:
    kps = [0.0, 0.1]
    port = [100.0, 100.0]
    stbd = [102.0, 98.0]   # deeper to starboard at kp 0, shallower at kp 0.1
    ahead = cross_slope_series(kps, port, stbd, cross_offset_m=50.0, direction=1)
    ok = ahead[0][1] > 0 and ahead[1][1] < 0
    expected = math.degrees(math.atan2(2.0, 100.0))
    ok = ok and abs(ahead[0][1] - expected) < 1e-9
    reverse = cross_slope_series(kps, port, stbd, cross_offset_m=50.0, direction=-1)
    ok = ok and abs(reverse[0][1] + ahead[0][1]) < 1e-12
    gappy = cross_slope_series(kps, [None, 100.0], stbd, 50.0)
    ok = ok and gappy[0][1] is None and gappy[1][1] is not None
    return _result("cross slope: +ve = deeper to starboard; direction flips", ok)


def test_absolute_slope() -> bool:
    long_series = [(0.0, 3.0), (0.1, -4.0), (0.2, None), (0.3, 5.0)]
    cross_series = [(0.0, 4.0), (0.1, 3.0)]
    abs_series = absolute_slope_series(long_series, cross_series)
    combined = math.degrees(math.atan(math.hypot(
        math.tan(math.radians(3.0)), math.tan(math.radians(4.0)))))
    ok = abs(abs_series[0][1] - combined) < 1e-9
    ok = ok and abs_series[0][1] >= 4.0 and abs_series[1][1] > 0
    ok = ok and abs_series[2][1] is None
    # No cross data at kp 0.3 -> longitudinal magnitude as a lower bound.
    ok = ok and abs_series[3][1] == 5.0
    return _result("absolute slope combines components, never negative", ok)


def run_all() -> list:
    return [
        test_row_round_trip(),
        test_currency(),
        test_long_slope_sign_and_gaps(),
        test_cross_slope_sign_and_direction(),
        test_absolute_slope(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
