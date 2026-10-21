# -*- coding: utf-8 -*-
"""Acquired burial data: worst-case 1 m bins, selection, statistics, overlaps (numpy, no QGIS).

Hand-calculated cases:

* Support: samples every metre cover from half a metre before the first to
  half a metre after the last; a gap wider than the bridging limit is no
  data; a survey every 5 m fills its 1 m bins, each bin taking the smallest
  value of the samples either side; the worst sample's own index is kept.
* Selection: latest time wins per bin (ties to the dataset listed first),
  worst case takes the smallest value, an override forces a dataset (no
  data where it has none).
* Status, length-weighted statistics in a scope that cuts bins, joined
  below-target ranges, no-data ranges and 5 m listings.
* Overlaps: found from time windows (touching files do not overlap, a shared
  boundary record does), and newer / existing / split / both rules.
"""

from __future__ import annotations


import numpy as np

from ..burial.acquired import bins as B
from ..burial.acquired import overlap as O

REQUIRES_QGIS = False


def _result(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)    # the runner wants plain bools (numpy comparisons give numpy.bool_)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _check(name: str, checks: dict) -> bool:
    failed = [k for k, v in checks.items() if not v]
    return _result(name, not failed, ", ".join(failed))


def test_support_and_worst_case() -> bool:
    kp = np.arange(11) / 1000.0                      # 0..10 m
    values = np.arange(11) / 10.0
    b = B.bin_samples(kp, values)
    gap_kp = np.r_[np.arange(6), np.arange(20, 26)] / 1000.0
    g = B.bin_samples(gap_kp, np.ones(gap_kp.size))
    survey_kp = np.arange(0, 51, 5) / 1000.0
    survey_v = np.array([1.0, 0.4, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0])
    s = B.bin_samples(survey_kp, survey_v)
    shuffled_kp = np.array([0.0105, 0.0, 0.0102, 0.001])
    shuffled_v = np.array([1.2, 2.0, 0.8, 2.0])
    w = B.bin_samples(shuffled_kp, shuffled_v, time=np.array([5.0, 1.0, 9.0, 2.0]))
    ten = int(np.flatnonzero(w.index == 10)[0])
    return _check("bins: support, bridging, worst case, sample identity", {
        "spacing and bridge": b.spacing_m == 1.0 and b.bridge_m == 3.0,
        "bins -1..10": b.index.tolist() == list(range(-1, 11)),
        "coverage 11 m": abs(b.cover_m.sum() - 11.0) < 1e-9 and b.cover_m[0] == 0.5 and b.cover_m[-1] == 0.5,
        "worst of neighbours": b.worst[1] == 0.0 and b.worst[2] == 0.1 and b.worst[-1] == 1.0,
        "gap is no data": 7 not in g.index.tolist() and 18 not in g.index.tolist()
        and abs(g.cover_m.sum() - 12.0) < 1e-9,
        "survey fills 1 m bins": s.index.tolist() == list(range(-3, 53)) and abs(s.cover_m.sum() - 55.0) < 1e-9,
        # the 5 m sample (0.4) supports 2.5-7.5 m: bins 2..7 see it, bin 8 only the 10 m sample
        "survey worst either side": s.worst[s.index.tolist().index(2)] == 0.4
        and s.worst[s.index.tolist().index(0)] == 1.0 and s.worst[s.index.tolist().index(7)] == 0.4
        and s.worst[s.index.tolist().index(8)] == 2.0,
        "two in a bin": w.worst[ten] == 0.8 and abs(w.mean[ten] - 1.0) < 1e-12 and w.count[ten] == 2,
        "sample is the input index": w.sample[ten] == 2,
        "latest time": w.time[ten] == 9.0,
        "empty": len(B.bin_samples(np.array([np.nan]), np.array([1.0]))) == 0,
    })


def test_selection() -> bool:
    a = B.bin_samples(np.arange(0, 10.5) / 1000.0, np.full(11, 1.5), time=np.full(11, 100.0))
    b = B.bin_samples(np.arange(5, 15.5) / 1000.0, np.full(11, 1.2), time=np.full(11, 200.0))
    tie = B.bin_samples(np.arange(5, 15.5) / 1000.0, np.full(11, 0.9), time=np.full(11, 100.0))
    latest = B.select([a, b])
    worst = B.select([a, b], B.MODE_WORST)
    ties = B.select([a, tie])
    forced = B.select([a, b], overrides=[(0.006, 0.008, 0)])
    nowhere = B.select([a, b], overrides=[(0.012, 0.014, 0)])
    at = latest.index.tolist()
    return _check("selection: latest, ties, worst case, overrides", {
        "union": at == list(range(-1, 16)),
        "a alone": latest.source[at.index(2)] == 0 and latest.others[at.index(2)] == 0,
        "b newer": latest.source[at.index(7)] == 1 and latest.worst[at.index(7)] == 1.2
        and latest.others[at.index(7)] == 1,
        "tie to first": ties.source[at.index(7)] == 0,
        "worst": worst.worst[at.index(7)] == 1.2 and worst.source[at.index(7)] == 1,
        "override": forced.source[forced.index.tolist().index(6)] == 0
        and forced.source[forced.index.tolist().index(7)] == 0
        and forced.source[forced.index.tolist().index(8)] == 1,
        "override without data": 12 not in nowhere.index.tolist() and 13 not in nowhere.index.tolist()
        and 11 in nowhere.index.tolist(),
    })


def test_assessment() -> bool:
    kp = np.arange(0, 20.5) / 1000.0
    values = np.full(kp.size, 1.4)
    values[3:6] = 0.7                                 # samples at 3, 4, 5 m
    values[8] = 0.9
    # bin j holds samples j and j + 1: bins 2..5 read 0.7, 7..8 read 0.9; the rest 1.4
    selection = B.select([B.bin_samples(kp, values)])
    runs = [(0.0, 0.010, 1.0), (0.010, 0.020, 1.5), (0.020, 0.030, None)]
    target = B.target_at(selection.centre_km, runs)
    status = B.classify(selection, target)
    stats = B.summarise(selection, status, (0.0, 0.020))
    ranges = B.below_ranges(selection, status, target)
    joined = B.below_ranges(selection, status, target, join_gap_m=2.0)
    holes = B.select([B.bin_samples(np.r_[np.arange(0, 6), np.arange(12, 21)] / 1000.0, np.ones(15))])
    gaps = B.no_data_ranges(holes, (0.0, 0.020))
    listing = B.listing(selection, 5.0, B.METHOD_WORST, target)
    mean_listing = B.listing(selection, 5.0, B.METHOD_MEAN)
    at = selection.index.tolist()
    return _check("assessment: status, statistics, ranges, gaps, listings", {
        "status": status[at.index(4)] == B.STATUS_BELOW and status[at.index(0)] == B.STATUS_MEETS
        and status[at.index(20)] == B.STATUS_NO_TARGET,
        "scope lengths": abs(stats["assessed_m"] - 20.0) < 1e-9 and abs(stats["no_data_m"]) < 1e-9,
        # bins 2..5 and 7..8 are below 1.0 (6 m); 10..19 are below 1.5 (10 m); 0, 1, 6, 9 meet
        "below length": abs(stats["below_m"] - 16.0) < 1e-9 and abs(stats["meets_m"] - 4.0) < 1e-9,
        "percent": abs(stats["meets_pct"] - 20.0) < 1e-9,
        "minimum": stats["min"] == 0.7 and abs(stats["min_kp"] - 0.0025) < 1e-12,
        "ranges": [(round(r["kp_from"], 3), round(r["kp_to"], 3)) for r in ranges]
        == [(0.002, 0.006), (0.007, 0.009), (0.010, 0.020)],
        "shortfall": abs(ranges[0]["max_shortfall"] - 0.3) < 1e-9 and abs(ranges[2]["max_shortfall"] - 0.1) < 1e-9,
        "joined": [(round(r["kp_from"], 3), round(r["kp_to"], 3)) for r in joined] == [(0.002, 0.020)],
        # samples to 5 m and from 12 m: covered to 5.5 m and from 11.5 m, so bins 6..10 are empty
        "no data": [(round(a, 4), round(b, 4)) for a, b in gaps] == [(0.006, 0.011)],
        "listing worst": listing["kp_from"].tolist()[:2] == [-0.005, 0.0] and listing["value"][1] == 0.7
        and listing["status"][1] == B.STATUS_BELOW,
        # bins 0..4 have means 1.4, 1.4, 1.05, 0.7, 0.7
        "listing mean": abs(mean_listing["value"][1] - 1.05) < 1e-9,
        "listing step": _raises(lambda: B.listing(selection, 2.5)),
    })


def _raises(call) -> bool:
    try:
        call()
    except ValueError:
        return True
    return False


def test_overlaps() -> bool:
    day1 = O.FileTimes("d1", 1, np.arange(0.0, 101.0))
    day2 = O.FileTimes("d2", 2, np.arange(90.0, 201.0))
    touching = O.FileTimes("t", 3, np.arange(101.0, 150.0))
    shared = O.FileTimes("s", 4, np.arange(100.0, 120.0))
    found = O.find_overlaps([day1], day2)
    none = O.find_overlaps([day1], touching)
    boundary = O.find_overlaps([day1], shared)
    o = found[0]

    def kept(rule, split=None):
        rec = O.Overlap(o.older_id, o.newer_id, o.t0, o.t1, o.older_count, o.newer_count, rule, split)
        masks = O.keep_masks([day1, day2], [rec])
        return int(masks["d1"].sum()), int(masks["d2"].sum())

    bad_split = O.Overlap("d1", "d2", 90.0, 100.0, 11, 11, O.RULE_SPLIT, 150.0)
    chained = O.keep_masks([day1, day2, O.FileTimes("d3", 3, np.arange(195.0, 300.0))],
                           [o, O.find_overlaps([day2], O.FileTimes("d3", 3, np.arange(195.0, 300.0)))[0]])
    text = O.describe(o, "day1.csv", "day2.csv", lambda t: f"{t:.0f}")
    return _check("overlaps: windows and rules", {
        "window": (o.t0, o.t1, o.older_count, o.newer_count) == (90.0, 100.0, 11, 11) and o.rule == O.RULE_NEWER,
        "touching": none == [],
        "shared boundary record": len(boundary) == 1 and boundary[0].older_count == 1,
        "newer wins": kept(O.RULE_NEWER) == (90, 111),
        "keep existing": kept(O.RULE_EXISTING) == (101, 100),
        "split": kept(O.RULE_SPLIT, 95.0) == (95, 106),
        "both": kept(O.RULE_BOTH) == (101, 111),
        "bad split": bad_split.problems() != [] and int(O.keep_masks([day1, day2], [bad_split])["d1"].sum()) == 101,
        "chain": int(chained["d1"].sum()) == 90 and int(chained["d2"].sum()) == 105,
        "describe": text.startswith("11 records 90–100 overlap day1.csv") and "newer delivery wins" in text,
        "no times": O.find_overlaps([day1], O.FileTimes("x", 5, np.full(3, np.nan))) == [],
    })


def test_large_campaign_speed() -> bool:
    import time
    rng = np.random.default_rng(3)
    kp = np.cumsum(np.full(900_000, 0.115)) / 1000.0           # about 100 km at 0.115 m spacing
    values = 1.5 + rng.normal(0, 0.1, kp.size)
    started = time.perf_counter()
    b = B.bin_samples(kp, values, time=np.arange(kp.size, dtype=float))
    selection = B.select([b])
    target = B.target_at(selection.centre_km, [(0.0, 200.0, 1.3)])
    status = B.classify(selection, target)
    B.summarise(selection, status, (0.0, kp[-1]))
    B.listing(selection, 10.0, B.METHOD_WORST, target)
    elapsed = time.perf_counter() - started
    return _result("bins: 900k samples over 100 km", elapsed < 10.0 and abs(b.cover_m.sum() - kp[-1] * 1000) < 1.0,
                   f"{elapsed:.2f} s")


def run_all() -> list:
    return [
        test_support_and_worst_case(),
        test_selection(),
        test_assessment(),
        test_overlaps(),
        test_large_campaign_speed(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
