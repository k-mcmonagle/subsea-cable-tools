# -*- coding: utf-8 -*-
"""Standalone checks for the shared plugin-wide slope math."""

import math

from .. import slope_utils
from ..slope_utils import (
    auto_half_window_m, datum_sign, interval_slope_series, ols_slope,
    should_invert_depth_axis, windowed_slope_series,
)


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def _close(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


def test_interval_series_matches_legacy_contract():
    # 100 m horizontal, 100 m deeper -> -45° (up-slope positive) for both
    # datums; gaps propagate None; first station has no interval.
    elev = interval_slope_series([0.0, 100.0, 200.0, 300.0],
                                 [-100.0, -200.0, None, -200.0])
    ok = (len(elev) == 4 and elev[0] is None and _close(elev[1], -45.0)
          and elev[2] is None and elev[3] is None)
    down = interval_slope_series([0.0, 100.0], [100.0, 200.0])
    ok = ok and _close(down[1], -45.0)
    forced = interval_slope_series([0.0, 100.0], [-100.0, -200.0],
                                   positive_down=True)
    ok = ok and _close(forced[1], 45.0)
    ok = ok and interval_slope_series([], []) == []
    return _result("interval: legacy per-interval contract", ok, str(elev))


def test_windowed_flattens_nearest_cell_staircase():
    # A 1:50 seabed sampled from a 50 m grid at 5 m stations reads as a
    # staircase: flat runs with one 1 m step per cell edge. Interval slope
    # spikes to ~11.3° at each edge; the windowed slope at ± one cell
    # recovers the true ~1.15° gradient.
    xs = [5.0 * i for i in range(41)]                    # 0..200 m
    depths = [10.0 + float(int(x // 50.0)) for x in xs]  # positive-down steps
    raw = interval_slope_series(xs, depths)
    raw_max = max(abs(v) for v in raw if v is not None)
    windowed = windowed_slope_series(xs, depths, 50.0, positive_down=True)
    mid = [abs(v) for v in windowed[10:31] if v is not None]
    true_deg = math.degrees(math.atan2(1.0, 50.0))       # ≈ 1.146°
    ok = raw_max > 10.0 and mid and max(mid) < 2.0
    ok = ok and all(abs(v - true_deg) < 1.0 for v in mid)
    return _result("windowed: coarse-grid staircase reads ~true gradient", ok,
                   "raw max %.1f°, windowed mid max %.2f°"
                   % (raw_max, max(mid) if mid else float("nan")))


def test_windowed_gap_and_degenerate_handling():
    xs = [0.0, 10.0, 20.0, 30.0]
    depths = [10.0, None, 12.0, 13.0]
    masked = windowed_slope_series(xs, depths, 10.0, positive_down=True,
                                   degenerate=None, mask_missing=True)
    unmasked = windowed_slope_series(xs, depths, 10.0, positive_down=True,
                                     degenerate=None, mask_missing=False)
    ok = masked[1] is None and unmasked[1] is not None
    # Degenerate: no valid data at all.
    empty = windowed_slope_series(xs, [None] * 4, 10.0, degenerate=0.0)
    ok = ok and empty == [0.0, 0.0, 0.0, 0.0]
    # Single station: window collapses to zero width.
    single = windowed_slope_series([5.0], [10.0], 10.0, degenerate=None)
    ok = ok and single == [None]
    return _result("windowed: gaps, masking and degenerate windows", ok,
                   str(masked))


def test_windowed_numpy_and_pure_paths_agree():
    xs = [float(i) for i in range(0, 300, 7)]
    depths = [20.0 + 0.02 * x + 3.0 * math.sin(x / 40.0) for x in xs]
    depths[5] = None
    depths[6] = None
    with_np = windowed_slope_series(xs, depths, 25.0, positive_down=True,
                                    mask_missing=True)
    saved = slope_utils._np
    try:
        slope_utils._np = None
        pure = windowed_slope_series(xs, depths, 25.0, positive_down=True,
                                     mask_missing=True)
    finally:
        slope_utils._np = saved
    ok = len(with_np) == len(pure)
    for a, b in zip(with_np, pure):
        if a is None or b is None:
            ok = ok and a is None and b is None
        else:
            ok = ok and abs(a - b) < 1e-9
    return _result("windowed: NumPy path matches pure-python path", ok)


def test_windowed_km_units_matches_burial_convention():
    # Burial-style series: kps in km, depths magnitudes, half window 0.05 km.
    kps = [i * 0.01 for i in range(21)]           # 0..0.2 km at 10 m
    depths = [100.0 + 500.0 * kp for kp in kps]   # 0.5 m/m deepening
    values = windowed_slope_series(kps, depths, 0.05, x_units_m=1000.0,
                                   positive_down=True)
    expected = math.degrees(math.atan2(-0.5, 1.0))
    mid = values[10]
    ok = _close(mid, expected, tol=1e-6)
    return _result("windowed: km-based series (burial convention)", ok,
                   "%.3f° vs %.3f°" % (mid, expected))


def test_auto_half_window():
    ok = auto_half_window_m([0.0, 5.0, 10.0], 50.0) == 50.0     # cell wins
    ok = ok and auto_half_window_m([0.0, 40.0, 80.0], 10.0) == 40.0  # spacing wins
    ok = ok and auto_half_window_m([0.0, 10.0], None) == 10.0
    ok = ok and auto_half_window_m([3.0], 25.0) is None
    ok = ok and auto_half_window_m([], None) is None
    return _result("auto half window: max(cell, median spacing)", ok)


def test_datum_helpers():
    ok = (should_invert_depth_axis([120.0, 130.0, None]) is True
          and should_invert_depth_axis([-120.0, -130.0]) is False
          and should_invert_depth_axis([]) is None
          and datum_sign([120.0, 130.0]) == 1.0
          and datum_sign([-120.0, -130.0]) == -1.0
          and datum_sign([]) == 1.0)
    return _result("datum: detection and sign", ok)


def test_ols_slope():
    ok = _close(ols_slope([0.0, 1.0, 2.0], [1.0, 3.0, 5.0]), 2.0)
    ok = ok and ols_slope([1.0, 1.0], [0.0, 5.0]) is None      # zero variance
    ok = ok and ols_slope([0.0], [1.0]) is None                # too few
    ok = ok and _close(
        ols_slope([0.0, float("nan"), 2.0], [0.0, 9.0, 4.0]), 2.0)
    return _result("ols: slope fit and degenerate cases", ok)


def run_all():
    return [test_interval_series_matches_legacy_contract(),
            test_windowed_flattens_nearest_cell_staircase(),
            test_windowed_gap_and_degenerate_handling(),
            test_windowed_numpy_and_pure_paths_agree(),
            test_windowed_km_units_matches_burial_convention(),
            test_auto_half_window(),
            test_datum_helpers(),
            test_ols_slope()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
