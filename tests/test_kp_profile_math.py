# -*- coding: utf-8 -*-
"""Standalone checks for the KP Mouse live-profile composite/slope math."""

from ..maptools.kp_profile_math import (
    composite_series, merged_contour_crossings, should_invert_depth_axis,
    slope_series,
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


def test_merged_contours_interleave_major_minor():
    # Bathy major/minor sets interleave into one distance-sorted seabed line.
    profile = {
        "contours": [
            {"name": "Bathy_Major", "x": [200.0, 0.0], "y": [-100.0, -50.0]},
            {"name": "Bathy_Minor", "x": [100.0, 300.0], "y": [-75.0, -125.0]},
        ],
    }
    x_values, y_values = merged_contour_crossings(profile)
    ok = (x_values == [0.0, 100.0, 200.0, 300.0]
          and y_values == [-50.0, -75.0, -100.0, -125.0])
    ok = ok and merged_contour_crossings({"contours": []}) == ([], [])
    return _result("contour layers merge into one sorted series", ok,
                   str(list(zip(x_values, y_values))))


def test_slope_degrees_and_gaps():
    # 100 m horizontal, 100 m deeper -> -45° (up-slope positive), whatever
    # the source datum; gaps propagate None; first station has no interval.
    elev = slope_series([0.0, 100.0, 200.0, 300.0],
                        [-100.0, -200.0, None, -200.0])
    ok = (len(elev) == 4 and elev[0] is None
          and abs(elev[1] + 45.0) < 1e-9
          and elev[2] is None and elev[3] is None)
    down = slope_series([0.0, 100.0, 200.0, 300.0],
                        [100.0, 200.0, None, 200.0])
    ok = ok and abs(down[1] + 45.0) < 1e-9  # same seabed, same sign
    forced = slope_series([0.0, 100.0], [-100.0, -200.0], positive_down=True)
    ok = ok and abs(forced[1] - 45.0) < 1e-9  # explicit datum override wins
    ok = ok and slope_series([], []) == []
    ok = ok and slope_series([0.0], [None]) == [None]
    return _result("slope: -45° deepening for both datums, None gaps", ok,
                   str(elev))


def test_merged_contours_collapse_duplicate_crossings():
    # The same contour level in major+minor layers must not create a
    # zero-width interval (which would gap or spike the slope series).
    profile = {
        "contours": [
            {"name": "major", "x": [100.0, 200.0], "y": [-50.0, -100.0]},
            {"name": "minor", "x": [100.0, 150.0], "y": [-50.0, -75.0]},
        ],
    }
    x_values, y_values = merged_contour_crossings(profile)
    ok = (x_values == [100.0, 150.0, 200.0]
          and y_values == [-50.0, -75.0, -100.0])
    slopes = slope_series(x_values, y_values)
    ok = ok and all(s is not None for s in slopes[1:])
    return _result("contour duplicates collapse; slope stays continuous", ok,
                   str(list(zip(x_values, y_values))))


def test_invert_detection():
    ok = (should_invert_depth_axis([120.0, 130.0, None]) is True      # positive-down
          and should_invert_depth_axis([-120.0, -130.0]) is False     # elevations
          and should_invert_depth_axis([None, None]) is None
          and should_invert_depth_axis([]) is None)
    return _result("invert axis auto-detection from data sign", ok)


def run_all():
    return [test_composite_prefers_first_raster_and_fills_gaps(),
            test_composite_falls_back_to_sorted_contours(),
            test_merged_contours_interleave_major_minor(),
            test_merged_contours_collapse_duplicate_crossings(),
            test_slope_degrees_and_gaps(), test_invert_detection()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
