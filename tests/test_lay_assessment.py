# -*- coding: utf-8 -*-
"""Unit tests for the Lay Assessment engine (``laydata.lay_assessment``).

Pure Python + numpy on synthetic data only. Each test returns True/False;
``run_all`` prints PASS / FAIL per check.
"""

from __future__ import annotations

import time
from typing import List

import numpy as np

from ..laydata import LayDataset
from ..laydata import lay_assessment as la
from ..laydata.qc_base import Severity

REQUIRES_QGIS = False

W = 10.0          # N/m submerged weight used by the synthetic cable
CABLE = la.CableProps("Test LW", weight_water_kg_m=W / la.G, weight_air_kg_m=2.0 * W / la.G,
                      cbl_kn=100.0, ntts_kn=80.0, nots_kn=50.0, npts_kn=20.0)


def _report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail and not ok else ""))
    return bool(ok)


def _iso(second: int) -> str:
    minute, sec = divmod(int(second), 60)
    hour, minute = divmod(minute, 60)
    return f"2024-01-05T{hour:02d}:{minute:02d}:{sec:02d}"


def _dataset(n=50, **overrides) -> LayDataset:
    """A tidy synthetic lay: 1 s records, 2 m of KP per record."""
    kp = np.arange(n) * 0.002
    columns = {
        "ISO_Time": [_iso(i) for i in range(n)],
        "TD KP": list(kp),
        "Bot.Tension": [0.0] * n,
        "Inst.Bot.Sl": [3.0] * n,
        "Meas.Top Tension": [10.0] * n,
        "TD Depth": [1000.0] * n,
        "Ship Speed": [1.0] * n,
        "Payout Speed": [1.03] * n,
        "Solution Valid": [1] * n,
        "TD_Lat_dd": [0.0] * n,
        "TD_Lon_dd": list(kp / 111.32),
        "source_file": ["synthetic.csv"] * n,
    }
    for key, values in overrides.items():
        columns[key] = values
    return LayDataset(columns)


def _records(ds, **kwargs) -> la.LayRecords:
    mapping = la.detect_roles(ds.field_names, ds.is_numeric_field)
    return la.build_records(ds, mapping, cable_for=lambda _t: CABLE, **kwargs)


# ---------------------------------------------------------------------------
def test_detect_roles() -> bool:
    names = ["Time", "Meas.Top Tension", "Ship KP", "Calc.Top Tension", "Bot.Tension", "TD KP",
             "Inst.Bot.Sl", "Avg.Bot.Sl", "TD Depth", "Ship Speed", "Payout Spe", "Solution Va",
             "TD_Lat_dd", "TD_Lon_dd", "Sheave He"]
    found = la.detect_roles(names, lambda name: name not in ("Time", "Solution Va"))
    expected = {"kp": "TD KP", "ship_kp": "Ship KP", "bottom_tension": "Bot.Tension",
                "bottom_slack": "Inst.Bot.Sl", "top_tension": "Meas.Top Tension",
                "top_tension_calc": "Calc.Top Tension", "td_depth": "TD Depth", "ship_speed": "Ship Speed",
                "payout_speed": "Payout Spe", "valid_flag": "Solution Va", "td_lat": "TD_Lat_dd",
                "td_lon": "TD_Lon_dd", "sheave_height": "Sheave He"}
    wrong = {k: (found.get(k), v) for k, v in expected.items() if found.get(k) != v}
    return _report("roles detected from MakaiLay-style (truncated) headers", not wrong, str(wrong))


def test_span_sag_matches_catenary() -> bool:
    # Two narrow peaks 200 m apart over a deep hollow: a free span of
    # curvature w/H has sag w L^2 / 8H at mid-span.
    x = np.arange(0.0, 400.0 + 0.5, 0.5)
    seabed = np.full_like(x, -110.0)
    seabed[x <= 80] = -100.0
    seabed[x >= 320] = -100.0
    for peak in (100.0, 300.0):
        seabed = np.maximum(seabed, -95.0 - 2.0 * np.abs(x - peak))
    h = 10_000.0  # N
    y = la.cable_rest_elevation(x, seabed, np.full_like(x, W / h))
    mid = int(np.argmin(np.abs(x - 200.0)))
    sag = -95.0 - y[mid]
    expected = W * 200.0 ** 2 / (8.0 * h)
    ok = abs(sag - expected) < 0.01 * expected and np.all(y >= seabed - 1e-9)
    spans = la.find_spans(x, x / 1000.0, y - seabed, np.full_like(x, h / 1000.0), 0.3, 5.0)
    main = max(spans, key=lambda s: s.length_m) if spans else None
    ok = ok and main is not None and 195.0 <= main.length_m <= 205.0
    ok = ok and abs((y[mid] - seabed[mid]) - 10.0) < 0.1  # cable 10 m above the hollow at mid-span
    return _report("free-span sag equals w L^2 / 8H", ok,
                   f"sag={sag:.3f} expected={expected:.3f} span={main}")


def test_zero_tension_conforms() -> bool:
    x = np.linspace(0, 500, 1001)
    seabed = -100.0 + 3.0 * np.sin(x / 7.0)
    y = la.cable_rest_elevation(x, seabed, np.full_like(x, np.inf))
    ok = np.allclose(y, seabed)
    # Mixed: tension only in the middle; ends stay pinned to the seabed.
    c = np.where((x > 200) & (x < 300), W / 5000.0, np.inf)
    y2 = la.cable_rest_elevation(x, seabed, c)
    ok = ok and np.allclose(y2[x <= 200], seabed[x <= 200]) and np.allclose(y2[x >= 300], seabed[x >= 300])
    ok = ok and np.nanmax(y2 - seabed) > 0.1 and np.all(y2 >= seabed - 1e-9)
    # Seabed gaps are not bridged.
    s3 = seabed.copy()
    s3[400:420] = np.nan
    y3 = la.cable_rest_elevation(x, s3, np.full_like(x, W / 5000.0))
    ok = ok and np.all(np.isnan(y3[400:420])) and np.all(np.isfinite(y3[:400]))
    return _report("zero tension conforms; tension spans; gaps not bridged", ok)


def test_group_ranges_merge_and_severity() -> bool:
    ds = _dataset(40)
    rec = _records(ds)
    level = np.zeros(rec.n, dtype=int)
    level[5:8] = 2
    level[9:11] = 3      # 1 record later (2 m): merges with the run above
    level[30:32] = 2     # far away: separate range
    value = np.arange(rec.n, dtype=float)
    out = la.group_ranges(rec, level, value, "x", lambda lvl, v: f"{lvl}:{v:g}", merge_m=5.0)
    ok = len(out) == 2 and out[0].severity == Severity.ERROR and out[0].value == 10.0
    ok = ok and abs(out[0].kp_start - 0.010) < 1e-9 and abs(out[0].kp_end - 0.020) < 1e-9
    ok = ok and out[1].severity == Severity.WARNING and len(out[1].rows) == 2
    return _report("ranges merge within the merge distance and keep the worst level", ok, str(out))


def test_tension_limits() -> bool:
    top = [10.0] * 30
    top[5] = 60.0      # > NOTS (50)
    top[20] = 90.0     # > NTTS (80)
    bottom = [0.0] * 30
    bottom[12] = 25.0  # > NPTS (20)
    rec = _records(_dataset(30, **{"Meas.Top Tension": top, "Bot.Tension": bottom}))
    findings, skipped = la.run_record_checks(rec, {"top_tension": {}, "bottom_tension": {}})
    levels = sorted((f.check_id, f.severity) for f in findings)
    ok = not skipped and levels == sorted([("bottom_tension", Severity.ERROR), ("top_tension", Severity.WARNING),
                                           ("top_tension", Severity.ERROR)])
    return _report("NOTS amber, NTTS red, NPTS red", ok, f"{levels} {skipped}")


def test_tension_consistency() -> bool:
    # Top tension 10 kN at 1000 m with w = 10 N/m: estimated bottom 0 kN.
    rec = _records(_dataset(20))
    estimate = la.estimated_bottom_tension(rec)
    ok = np.allclose(estimate, 0.0)
    bottom = [0.0] * 20
    bottom[4:7] = [6.0, 6.0, 6.0]
    rec = _records(_dataset(20, **{"Bot.Tension": bottom}))
    findings, _ = la.run_record_checks(rec, {"tension_consistency": {"tolerance_kn": 2.0}})
    ok = ok and len(findings) == 1 and abs(findings[0].value + 6.0) < 1e-9
    return _report("top tension minus w x depth matches the bottom tension", ok, str(findings))


def test_loop_and_payout_checks() -> bool:
    slack = [3.0] * 40
    slack[10:14] = [12.0] * 4
    ship = [1.0] * 40
    ship[25:28] = [0.0] * 3
    rec = _records(_dataset(40, **{"Inst.Bot.Sl": slack, "Ship Speed": ship}))
    findings, _ = la.run_record_checks(rec, {"loop_risk": {}, "payout_stopped": {}})
    ids = sorted(f.check_id for f in findings)
    ok = ids == ["loop_risk", "payout_stopped"]
    loop = next(f for f in findings if f.check_id == "loop_risk")
    ok = ok and loop.value == 12.0 and len(loop.rows) == 4
    # Loop risk needs near-zero bottom tension: under tension it is not flagged.
    rec2 = _records(_dataset(40, **{"Inst.Bot.Sl": slack, "Bot.Tension": [2.0] * 40}))
    findings2, _ = la.run_record_checks(rec2, {"loop_risk": {}})
    ok = ok and not findings2
    return _report("loop risk (slack at zero tension) and payout while stopped", ok, str(ids))


def test_td_reversal() -> bool:
    kp = list(np.arange(40) * 0.002)
    kp[20:24] = [kp[19] - 0.004, kp[19] - 0.006, kp[19] - 0.008, kp[19] - 0.001]
    rec = _records(_dataset(40, **{"TD KP": kp}))
    findings, _ = la.run_record_checks(rec, {"td_reversal": {"min_back_m": 5.0}})
    ok = len(findings) == 1 and abs(findings[0].value - 8.0) < 1e-6
    return _report("touchdown moving back is flagged", ok, str(findings))


def test_build_records_units_and_flags() -> bool:
    valid = [1] * 10
    valid[3] = 0
    kp = list(np.arange(10) * 0.002)
    kp[7] = None
    ds = _dataset(10, **{"Solution Valid": valid, "TD KP": kp, "Meas.Top Tension": [2.0] * 10})
    rec = _records(ds, tension_unit="te (tonne-force)")
    ok = rec.n == 8 and rec.excluded == 2 and 3 not in rec.rows.tolist()
    ok = ok and np.allclose(rec.get("top_tension"), 2.0 * la.G)
    return _report("invalid / KP-less rows left out; tonnes converted to kN", ok)


def test_missing_inputs_reported() -> bool:
    ds = _dataset(10)
    mapping = la.detect_roles(ds.field_names, ds.is_numeric_field)
    rec = la.build_records(ds, mapping, cable_for=lambda _t: None)  # no library
    _findings, skipped = la.run_record_checks(rec, {"top_tension": {}, "planned_slack": {}})
    ok = "NOTS" in skipped.get("top_tension", "") and "Planned" in skipped.get("planned_slack", "")
    return _report("checks without their inputs are skipped with a reason", ok, str(skipped))


def _waves(x, amplitude=1.5, wavelength=40.0):
    return 1000.0 + amplitude * np.sin(2 * np.pi * x / wavelength)


def test_friction_length_balance() -> bool:
    # 1 km over 40 m / 1.5 m sand waves (1.4 % more seabed than plan) that the
    # lay model did not see (flat touchdown depth). First half: 3 % slack;
    # second half: no slack and no logged tension.
    n = 501
    kp = np.arange(n) * 0.002
    slack = [3.0] * 250 + [0.0] * 251
    rec = _records(_dataset(n, **{"TD KP": list(kp), "TD_Lon_dd": list(kp / 111.32),
                                  "Bot.Tension": [0.0] * n, "Inst.Bot.Sl": slack}))
    x = np.arange(0.0, 1000.0 + 1.0, 1.0)
    friction = {"mu": 0.5, "max_reach_m": 1000.0, "shortfall_pct": 0.5}
    model = la.model_seabed(x, x / 1000.0, _waves(x), rec, rec.kp * 1000.0, 0.05, 0.3, 5.0,
                            smoothing_m=0.0, friction=friction)
    # Overlapping friction windows merge: one taut stretch over the slack-free half.
    ok = len(model.shortfalls) == 1 and model.shortfalls[0].kp_start > 0.45
    ok = ok and model.shortfalls[0].tension_kn > 0.5 and model.shortfalls[0].feasible
    ok = ok and not [sp for sp in model.spans if sp.kp_end < 0.45] and len(model.spans) >= 5
    ok = ok and all(sp.source == "cable short of the seabed" for sp in model.spans)
    # The pulled-taut shape uses the cable laid: zero balance at the solved tension.
    found = la.length_findings(model, friction)
    ok = ok and found and all(f.check_id == "length_balance" for f in found)
    return _report("short cable is pulled taut and spans; slack-laid half conforms", ok,
                   f"shortfalls={[(round(f.kp_start, 3), round(f.tension_kn, 3)) for f in model.shortfalls]}")


def test_friction_keeps_slack_local() -> bool:
    # 2 km: 6 % slack on the first km, none on the second, over sand waves.
    # Overall the route has more cable than seabed (whole-route check: fine),
    # but friction stops the first km's spare cable reaching the far end.
    n = 1001
    kp = np.arange(n) * 0.002
    slack = [6.0] * 500 + [0.0] * 501
    rec = _records(_dataset(n, **{"TD KP": list(kp), "TD_Lon_dd": list(kp / 111.32),
                                  "Bot.Tension": [0.0] * n, "Inst.Bot.Sl": slack}))
    x = np.arange(0.0, 2000.0 + 1.0, 1.0)
    depth = _waves(x)
    seabed = float(np.sum(np.hypot(np.diff(x), np.diff(depth))))
    cable = float(np.sum(np.diff(x) * (1 + np.where(x[1:] <= 1000, 0.06, 0.0))))
    model = la.model_seabed(x, x / 1000.0, depth, rec, rec.kp * 1000.0, 0.05, 0.3, 5.0, smoothing_m=0.0,
                            friction={"mu": 0.5, "max_reach_m": 1000.0, "shortfall_pct": 0.5})
    far = [f for f in model.shortfalls if f.kp_end > 1.5]
    ok = cable > seabed and bool(far)
    # Without friction limits (one 2 km reach) the surplus would cover it all.
    lax = la.model_seabed(x, x / 1000.0, depth, rec, rec.kp * 1000.0, 0.05, 0.3, 5.0, smoothing_m=0.0,
                          friction={"mu": 1e-6, "max_reach_m": 2000.0, "shortfall_pct": 0.5})
    ok = ok and not [f for f in lax.shortfalls if f.kp_end > 1.5]
    return _report("friction keeps slack local (whole-route length would hide the shortfall)", ok,
                   f"cable {cable:.0f} vs seabed {seabed:.0f}; far shortfalls {len(far)}")


def test_long_route_speed() -> bool:
    # 20 km laid with no slack over sand waves the lay model did not see:
    # one long short stretch, solved in friction-reach pieces.
    n = 10001
    kp = np.arange(n) * 0.002
    rec = _records(_dataset(n, **{"TD KP": list(kp), "TD_Lon_dd": list(kp / 111.32),
                                  "Bot.Tension": [0.0] * n, "Inst.Bot.Sl": [0.0] * n}))
    x = np.arange(0.0, 20000.0 + 2.0, 2.0)
    start = time.perf_counter()
    model = la.model_seabed(x, x / 1000.0, _waves(x, wavelength=60.0), rec, rec.kp * 1000.0, 0.05, 0.3, 5.0,
                            smoothing_m=None, friction={"mu": 0.5, "max_reach_m": 1000.0, "shortfall_pct": 0.5})
    elapsed = time.perf_counter() - start
    ok = elapsed < 60.0 and len(model.shortfalls) >= 1 and len(model.spans) > 50
    return _report("20 km short of seabed models in reasonable time", ok,
                   f"{elapsed:.1f} s, {len(model.spans)} spans, {len(model.shortfalls)} stretch(es)")


def test_smoothing_and_conformity() -> bool:
    x = np.arange(0.0, 500.0, 1.0)
    rng = np.random.default_rng(3)
    noisy = 1000.0 + rng.normal(0, 0.2, len(x))
    smooth = la.smooth_profile(x, noisy, 10.0)
    ok = np.nanstd(smooth) < 0.5 * np.nanstd(noisy)
    noisy[100:110] = np.nan
    smooth = la.smooth_profile(x, noisy, 10.0)
    ok = ok and np.all(np.isnan(smooth[100:110])) and np.isfinite(smooth[:100]).all()
    # 2 (EI / w)^(1/3): EI 2 kNm2, w 10 N/m -> 2 * 200^(1/3) = 11.7 m
    ok = ok and abs(la.conformity_length_m(2.0, 10.0) - 2.0 * 200.0 ** (1 / 3)) < 1e-9
    noise_len = float(np.sum(np.hypot(1.0, np.diff(1000.0 + rng.normal(0, 0.2, 500)))))
    ok = ok and noise_len > 500 * 1.02  # why smoothing matters: noise alone adds > 2 % "seabed"
    return _report("seabed smoothing (gaps kept) and conformity length", ok)


def test_makeup_ranges() -> bool:
    m = la.KpMakeup.from_ranges([(0.0, 1.0, "LW"), (1.0, 1.5, "LW"), (1.5, 3.0, "SA"), (3.0, 4.0, None)])
    ok = m.labels == ["LW", "SA"] and list(m.label_at([0.2, 1.2, 2.0, 5.0])) == ["LW", "LW", "SA", None]
    ok = ok and np.allclose(m.boundaries(), [1.5])
    r = la.makeup_from_records(np.array([0.0, 0.1, 0.2, 0.3]), ["LW", "LW", "SA", "SA"])
    ok = ok and r.labels == ["LW", "SA"] and abs(r.boundaries()[0] - 0.15) < 1e-9
    return _report("cable makeup along KP", ok, str(m.labels))


def test_joint_in_water_column() -> bool:
    # Touchdown at KP 0.9 on LW, 1000 m deep, laying towards SA from KP 1.0.
    # The hanging 1000 m is 100 m of LW (bottom) under 900 m of SA (30 N/m).
    # Top tension 80 kN passes SA's NOTS (150) at the sheave, but the joint
    # carries 80 - 30 x 900 / 1000 = 53 kN, above LW's NOTS (50).
    lw = la.CableProps("LW", weight_water_kg_m=10.0 / la.G, nots_kn=50.0, ntts_kn=80.0, npts_kn=20.0)
    sa = la.CableProps("SA", weight_water_kg_m=30.0 / la.G, nots_kn=150.0, ntts_kn=200.0, npts_kn=60.0)
    n = 3
    ds = _dataset(n, **{"TD KP": [0.880, 0.890, 0.900], "Meas.Top Tension": [80.0] * n,
                        "Inst.Bot.Sl": [0.0] * n, "Layback": [0.0] * n, "TD_Lon_dd": [0.0] * n})
    mapping = la.detect_roles(ds.field_names, ds.is_numeric_field)
    rec = la.build_records(ds, mapping)
    makeup = la.KpMakeup.from_ranges([(0.0, 1.0, "LW"), (1.0, 3.0, "SA")])
    la.apply_makeup(rec, makeup, lambda label: {"LW": lw, "SA": sa}.get(label))
    findings, _ = la.run_record_checks(rec, {"top_tension": {}})
    ok = rec.labels == ["LW", "LW", "LW"] and rec.values["hang_transition"].all()
    t = rec.get("hang_tension_kn")[2]
    ok = ok and abs(t - 53.0) < 1.5 and rec.hang_labels[2] == "LW"
    ok = ok and len(findings) == 1 and findings[0].severity == Severity.WARNING and "LW" in findings[0].message
    # Hanging weight in the top-tension identity: mean of 100 m LW + 900 m SA.
    ok = ok and abs(rec.get("hang_w_npm")[2] - 28.0) < 2.0
    return _report("tension at a joint hanging in the water vs that cable's NOTS", ok,
                   f"T={t:.1f} label={rec.hang_labels[2]} findings={[f.message for f in findings]}")


def test_status_bins() -> bool:
    finding = la.RangeFinding("x", Severity.ERROR, 0.100, 0.120, "m")
    edges, level = la.status_bins(0.0, 0.200, [finding], np.arange(0, 0.150, 0.002), 10.0)
    ok = len(level) == 20 and level[0] == 0 and level[10] == 3 and level[12] == 3
    ok = ok and level[13] == 0 and level[19] == -1
    return _report("status bar: clear, red finding, no data", ok, str(level.tolist()))


def test_track_vertices() -> bool:
    # Records out of order with jitter while stopped collapse to one vertex per KP bin.
    kp = np.array([0.0, 0.004, 0.002, 0.002, 0.002, 0.006])
    lon = kp / 111.32
    ds = _dataset(6, **{"TD KP": list(kp), "TD_Lon_dd": list(lon)})
    rec = _records(ds)
    lon_v, _lat_v, kp_v = la.track_vertices(rec, spacing_m=2.0)
    ok = len(kp_v) == 4 and np.all(np.diff(kp_v) > 0)
    chain = np.array([0.0, 2.0, 4.0, 6.0])
    ok = ok and abs(la.kp_to_chainage(chain, kp_v, 0.003) - 3.0) < 1e-9
    return _report("touchdown track ordered by KP", ok, str(kp_v))


def test_hull_speed() -> bool:
    x = np.arange(0.0, 100_000.0, 1.0)
    rng = np.random.default_rng(1)
    seabed = -1000.0 + np.cumsum(rng.normal(0, 0.05, len(x)))
    start = time.perf_counter()
    y = la.cable_rest_elevation(x, seabed, np.full_like(x, W / 2000.0))
    elapsed = time.perf_counter() - start
    ok = elapsed < 5.0 and np.all(y >= seabed - 1e-9)
    return _report("100 km at 1 m in a few seconds", ok, f"{elapsed:.2f} s")


def run_all() -> List[bool]:
    results = [
        test_detect_roles(),
        test_span_sag_matches_catenary(),
        test_zero_tension_conforms(),
        test_group_ranges_merge_and_severity(),
        test_tension_limits(),
        test_tension_consistency(),
        test_loop_and_payout_checks(),
        test_td_reversal(),
        test_build_records_units_and_flags(),
        test_missing_inputs_reported(),
        test_friction_length_balance(),
        test_friction_keeps_slack_local(),
        test_long_route_speed(),
        test_smoothing_and_conformity(),
        test_makeup_ranges(),
        test_joint_in_water_column(),
        test_status_bins(),
        test_track_vertices(),
        test_hull_speed(),
    ]
    print(f"\n{sum(results)}/{len(results)} lay assessment checks passed.")
    return results


if __name__ == "__main__":
    run_all()
