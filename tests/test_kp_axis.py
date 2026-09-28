# -*- coding: utf-8 -*-
"""Round-KP axis ticks (kp_axis, no QGIS): steps, labels and crossings."""

import math

from ..kp_axis import (KPCrossings, format_kp, kp_multiples, linear_kp_ticks,
                       nice_kp_step)


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def test_step_ladder():
    cases = {(0.004, 5): 0.001, (0.9, 5): 0.25, (2.0, 5): 0.5, (4.4, 5): 1.0,
             (0.45, 5): 0.1, (0.2, 8): 0.025, (60.0, 6): 10.0, (7.0, 3): 5.0}
    got = {key: nice_kp_step(*key) for key in cases}
    return _result("step ladder: 0.001…0.1/0.25/0.5/1/2/5/10", got == cases, str(got))


def test_format_and_multiples():
    ok = format_kp(12.3456) == "12.346" and format_kp(-0.0001) == "0.000" and format_kp(None) == ""
    values = kp_multiples(0.29, 1.01, 0.1)
    ok = ok and values == [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    return _result("3 dp labels; rounded multiples (no 0.30000000004)", ok, str(values))


def test_linear_ticks_are_round():
    # A view from KP 0.344 to 1.844 must not start its labels at 0.344.
    step, positions = linear_kp_ticks(0.344, 1.844, 500)
    ok = step == 0.25 and positions[0] == 0.5 and positions[-1] == 1.75
    ok = ok and all(abs(p / step - round(p / step)) < 1e-9 for p in positions)
    # Reversed numbering: x 0..2 km maps to KP 10..8.
    step2, pos2 = linear_kp_ticks(0.0, 2.0, 400, km_per_unit=-1.0, offset_km=10.0)
    labels = [format_kp(10.0 - p) for p in pos2]
    ok = ok and labels[0] == "10.000" and labels[-1] == "8.000"
    return _result("linear ticks on round KPs (also reversed)", ok, "%s %s / %s" % (step, positions, labels))


def test_mapped_crossings_offset_line():
    # A line whose nearest KP runs 12.3440 → 13.3440 over 1000 m: ticks must
    # sit at 12.500, 12.750, 13.000, 13.250 at their true positions.
    xs = [i * 10.0 for i in range(101)]
    kps = [12.344 + x / 1000.0 for x in xs]
    crossings = KPCrossings(xs, kps)
    step, ticks = crossings.ticks(0.0, 1000.0, px_per_m=0.4)
    labels = [format_kp(kp) for _x, kp in ticks]
    ok = step == 0.25 and labels == ["12.500", "12.750", "13.000", "13.250"]
    ok = ok and all(abs(x - (kp - 12.344) * 1000.0) < 1e-6 for x, kp in ticks)
    return _result("mapped ticks land where the nearest KP crosses round values", ok, str(labels))


def test_polish_uses_true_kp():
    # Coarse samples of a curved KP function; secant polishing makes each
    # tick's true KP match its label (to 0.02 m).
    def true_kp(x):
        return 5.0 + 0.8 * x / 1000.0 + 0.05 * math.sin(x / 150.0)
    xs = [i * 100.0 for i in range(11)]
    crossings = KPCrossings(xs, [true_kp(x) for x in xs], refine=true_kp)
    worst = max(abs(true_kp(x) - kp) for x, kp in crossings.crossings(0.1))
    ok = worst * 1000.0 < 0.02 and len(crossings.crossings(0.1)) >= 7
    return _result("crossings polished against the true KP function", ok, "worst %.4f m" % (worst * 1000))


def test_jump_and_gaps_skip():
    # Nearest segment switches at x=500 (KP jumps 3.1 → 7.9): no ticks may
    # be invented inside the jump; None marks off-route samples.
    xs = [i * 50.0 for i in range(21)]
    kps = [3.0 + x / 5000.0 if x < 500 else 7.9 + (x - 500) / 5000.0 for x in xs]
    kps[3] = None
    crossings = KPCrossings(xs, kps)
    values = [kp for _x, kp in crossings.crossings(0.1)]
    ok = all(kp < 3.2 or kp > 7.8 for kp in values) and 8.0 in values
    ok = ok and crossings.kp_at(125.0) is None
    return _result("KP jumps and off-route gaps never receive ticks", ok, str(values))


def test_cross_route_revisits_are_thinned():
    # A line across a U-bend revisits each KP on both arms; at 2 px/m the
    # 0.01 km crossings are 50 px apart, so every other one is dropped
    # rather than overprinted.
    xs = [i * 5.0 for i in range(41)]
    kps = [1.0 + 0.0004 * abs(x - 100.0) for x in xs]
    crossings = KPCrossings(xs, kps)
    raw = crossings.crossings(0.01)
    _step, kept = crossings.ticks(0.0, 200.0, px_per_m=2.0)
    gaps = [b[0] - a[0] for a, b in zip(kept, kept[1:])]
    ok = len(raw) > len(kept) >= 2 and all(g * 2.0 >= 70.0 - 1e-6 for g in gaps)
    return _result("repeated KPs on cross-route lines are thinned to legible spacing", ok,
                   "raw=%d kept=%d" % (len(raw), len(kept)))


def run_all():
    return [test_step_ladder(), test_format_and_multiples(), test_linear_ticks_are_round(),
            test_mapped_crossings_offset_line(), test_polish_uses_true_kp(),
            test_jump_and_gaps_skip(), test_cross_route_revisits_are_thinned()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
