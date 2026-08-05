# -*- coding: utf-8 -*-
"""Standalone checks for the KP Mouse live-profile composite/slope math."""

from ..maptools.kp_profile_math import (
    composite_series, should_invert_depth_axis, slope_series,
)


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def test_composite_prefers_first_raster_and_fills_gaps():
    profile = {
        "rasters": [
            {"name": "hi-res", "x": [0.0, 100.0, 200.0], "y": [-10.0, None, -30.0]},
            {"name": "lo-res", "x": [0.0, 100.0, 200.0], "y": [-11.0, -21.0, -31.0]},
        ],
        "contours": [],
    }
    x_values, y_values = composite_series(profile)
    ok = x_values == [0.0, 100.0, 200.0] and y_values == [-10.0, -21.0, -30.0]
    return _result("composite: first-valid raster wins, gaps filled", ok, str(y_values))


def test_composite_falls_back_to_sorted_contours():
    profile = {
        "rasters": [{"name": "r", "x": [0.0, 100.0], "y": [None, None]}],
        "contours": [
            {"name": "minor", "x": [150.0, 50.0], "y": [-15.0, -5.0]},
            {"name": "major", "x": [100.0], "y": [-10.0]},
        ],
    }
    x_values, y_values = composite_series(profile)
    ok = x_values == [50.0, 100.0, 150.0] and y_values == [-5.0, -10.0, -15.0]
    return _result("composite: contour crossings merged and sorted", ok, str(list(zip(x_values, y_values))))


def test_slope_degrees_and_gaps():
    # 100 m horizontal, 100 m deeper -> 45 degrees; gaps propagate None.
    slopes = slope_series([0.0, 100.0, 200.0, 300.0],
                          [-100.0, -200.0, None, -200.0])
    ok = (len(slopes) == 4 and slopes[0] == 0.0
          and abs(slopes[1] + 45.0) < 1e-9
          and slopes[2] is None and slopes[3] is None)
    ok = ok and slope_series([], []) == []
    ok = ok and slope_series([0.0], [None]) == [None]
    return _result("slope: 45° segment, None gaps, empty input", ok, str(slopes))


def test_invert_detection():
    ok = (should_invert_depth_axis([120.0, 130.0, None]) is True      # positive-down
          and should_invert_depth_axis([-120.0, -130.0]) is False     # elevations
          and should_invert_depth_axis([None, None]) is None
          and should_invert_depth_axis([]) is None)
    return _result("invert axis auto-detection from data sign", ok)


def run_all():
    return [test_composite_prefers_first_raster_and_fills_gaps(),
            test_composite_falls_back_to_sorted_contours(),
            test_slope_degrees_and_gaps(), test_invert_detection()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
