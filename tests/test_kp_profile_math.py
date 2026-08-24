# -*- coding: utf-8 -*-
"""Standalone checks for the KP Mouse live-profile composite/slope math."""

from ..maptools.kp_profile_math import (
    composite_series, composite_series_with_sources, merged_contour_crossings,
    profile_slope_series, should_invert_depth_axis, slope_series,
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


def test_profile_slope_masks_seams_and_gaps():
    # Two rasters: the fine one covers the first half, the coarse one (with
    # a 10 m vertical datum offset) the second half. The seam must yield
    # None slopes, never a fabricated near-vertical spike; each half's
    # slope window follows its own source's cell size.
    xs = [10.0 * i for i in range(11)]                       # 0..100 m
    fine = [-50.0 - 0.1 * x if x <= 50.0 else None for x in xs]
    coarse = [None if x <= 50.0 else -60.0 - 0.1 * x - 10.0 for x in xs]
    profile = {
        "rasters": [
            {"name": "fine", "x": xs, "y": fine, "pixel_size_m": 10.0},
            {"name": "coarse", "x": xs, "y": coarse, "pixel_size_m": 40.0},
        ],
        "contours": [],
    }
    out_x, slopes, max_half = profile_slope_series(profile)
    _cx, _cy, sources, cells = composite_series_with_sources(profile)
    ok = out_x == xs and sources[0] == 0 and sources[-1] == 1
    ok = ok and cells[0] == 10.0 and cells[-1] == 40.0
    # The seam-adjacent station (first coarse station) is blanked so the
    # plotted curve visibly breaks at the source transition.
    ok = ok and slopes[6] is None
    finite = [value for value in slopes if value is not None]
    # True gradient 0.1 m/m ≈ 5.7° everywhere else; the 10 m seam step over
    # one 10 m interval would read ≈ 47° if bridged.
    ok = ok and finite and all(abs(abs(v) - 5.71) < 0.6 for v in finite)
    ok = ok and max_half == 40.0
    # No-data gap in a single raster also masks instead of bridging.
    gappy = {"rasters": [{"name": "r", "x": xs,
                          "y": [-50.0, -51.0, -52.0, None, None,
                                -80.0, -81.0, -82.0, -83.0, -84.0, -85.0],
                          "pixel_size_m": 10.0}], "contours": []}
    _gx, gap_slopes, _gh = profile_slope_series(gappy)
    ok = ok and gap_slopes[3] is None and gap_slopes[4] is None
    # The 27 m step across the gap must not leak into neighbouring slopes.
    ok = ok and all(abs(v) < 8.0 for v in gap_slopes if v is not None)
    return _result("profile slope: seams and gaps mask, cell-scaled windows",
                   ok, str([None if v is None else round(v, 1)
                            for v in slopes]))


def run_all():
    return [test_composite_prefers_first_raster_and_fills_gaps(),
            test_composite_falls_back_to_sorted_contours(),
            test_merged_contours_interleave_major_minor(),
            test_merged_contours_collapse_duplicate_crossings(),
            test_slope_degrees_and_gaps(), test_invert_detection(),
            test_profile_slope_masks_seams_and_gaps()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
