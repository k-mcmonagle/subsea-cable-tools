# -*- coding: utf-8 -*-
"""Unit tests for the Lay Assessment engine (``laydata.lay_assessment``).

Pure Python + numpy on synthetic data only. Each test returns True/False;
``run_all`` prints PASS / FAIL per check.
"""

from __future__ import annotations

import math
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


def _zero_slack_lay(km, record_m=0.5, depth_noise=0.0, slack=None, seed=0):
    n = int(km * 1000 / record_m)
    kp = np.arange(n) * record_m / 1000.0
    rng = np.random.default_rng(seed)
    slack = np.zeros(n) if slack is None else slack(kp)
    columns = {"TD KP": list(kp), "Bot.Tension": [0.0] * n, "Inst.Bot.Sl": list(slack),
               "TD Depth": list(1000.0 + rng.normal(0, depth_noise, n)) if depth_noise else [1000.0] * n,
               "TD_Lat_dd": [0.0] * n, "TD_Lon_dd": list(kp / 111.32)}
    return _records(LayDataset(columns))


def _friction():
    return {"mu": 0.5, "max_reach_m": 1000.0, "shortfall_pct": 0.5}


def test_laid_cable_ignores_depth_noise() -> bool:
    # No slack over 40 m / 1.5 m sand waves the lay model did not see: short of the
    # seabed. 5 cm of touchdown-depth noise between 0.5 m records used to add 1 %
    # of "cable" and hide the shortfall entirely.
    x = np.arange(0.0, 3000.0, 2.0)
    depth = _waves(x)
    found = []
    for noise in (0.0, 0.05):
        rec = _zero_slack_lay(3.0, depth_noise=noise)
        m = la.model_seabed(x, x / 1000.0, depth, rec, rec.kp * 1000.0, 0.05, 0.3, 5.0, smoothing_m=None,
                            friction=_friction())
        found.append((float(np.nanmedian(m.balance_pct)), len(m.shortfalls), len(m.spans)))
    (clean_bal, clean_short, clean_spans), (noisy_bal, noisy_short, noisy_spans) = found
    ok = clean_short >= 1 and noisy_short >= 1 and abs(clean_bal - noisy_bal) < 0.1
    ok = ok and clean_spans > 20 and abs(noisy_spans - clean_spans) <= 0.1 * clean_spans
    return _report("touchdown-depth noise does not add laid cable", ok, str(found))


def test_taut_solver_station_spacing() -> bool:
    # Short stretches near the threshold: the answer must not depend on whether the
    # seabed is sampled every 1 m or 2 m (the laid cable was measured over the
    # window, the shape over the stations inside it: short windows looked slack).
    rec = _zero_slack_lay(10.0, slack=lambda kp: np.clip(0.9 + 0.6 * np.sin(kp * 1000 / 137.0), 0, None))
    counts = []
    for step in (2.0, 1.0):
        x = np.arange(0.0, 10000.0, step)
        m = la.model_seabed(x, x / 1000.0, _waves(x), rec, rec.kp * 1000.0, 0.05, 0.3, 5.0, smoothing_m=None,
                            friction=_friction())
        counts.append((len(m.shortfalls), round(float(np.median([s.tension_kn for s in m.shortfalls] or [0])), 2)))
        if any(s.reach_m > 1000.0 + 1e-6 for s in m.shortfalls):
            return _report("taut stretches independent of station spacing", False, "reach above the cap")
    (n2, t2), (n1, t1) = counts
    ok = n1 > 0 and abs(n2 - n1) <= max(1, 0.1 * n1) and abs(t2 - t1) <= 0.15 * max(t1, 1e-9)
    return _report("taut stretches independent of station spacing (reach within cap)", ok, str(counts))


def _ridges(x, height=2.0, slope=0.5, at=(150.0, 250.0), base=-1000.0):
    seabed = np.full_like(x, base)
    for c in at:
        seabed = np.maximum(seabed, base + height - slope * np.abs(x - c))
    return seabed


def test_crests_hold_the_cable() -> bool:
    # Two sharp 2 m rock ridges 100 m apart at 3 kN: 1.56 m gap on the raw seabed.
    # Resting the cable on the seabed smoothed over the conformity length lowered
    # the crests and hid most of it (0.32 m); the 2 m noise filter keeps 1.25 m.
    x = np.arange(0.0, 400.0, 1.0)
    depth = -_ridges(x)
    n = 800
    kp = np.arange(n) * 0.0005
    rec = _records(LayDataset({"TD KP": list(kp), "Bot.Tension": [3.0] * n, "TD Depth": [1000.0] * n,
                               "TD_Lat_dd": [0.0] * n, "TD_Lon_dd": list(kp / 111.32)}))
    m = la.model_seabed(x, x / 1000.0, depth, rec, rec.kp * 1000.0, 0.05, 0.3, 5.0, smoothing_m=None,
                        filter_m=None)
    gap = float(np.nanmax(m.gap_m))
    ok = gap > 1.2 and m.smoothing_m >= 10.0 and m.filter_m == la.AUTO_FILTER_M
    ok = ok and m.spans and max(s.length_m for s in m.spans) >= 15.0
    return _report("crests holding the cable survive the seabed filter", ok,
                   f"gap {gap:.2f} m spans {[round(s.length_m) for s in m.spans]}")


def test_noise_makes_no_false_spans() -> bool:
    # Flat MBES seabed with 10 cm sounding noise at 1 m: no reportable span at 3 kN.
    x = np.arange(0.0, 2000.0, 1.0)
    depth = 1000.0 + np.random.default_rng(5).normal(0, 0.1, len(x))
    n = 4000
    kp = np.arange(n) * 0.0005
    rec = _records(LayDataset({"TD KP": list(kp), "Bot.Tension": [3.0] * n, "TD Depth": [1000.0] * n,
                               "TD_Lat_dd": [0.0] * n, "TD_Lon_dd": list(kp / 111.32)}))
    m = la.model_seabed(x, x / 1000.0, depth, rec, rec.kp * 1000.0, 0.05, 0.3, 5.0, smoothing_m=None,
                        filter_m=None)
    return _report("sounding noise makes no false spans", not m.spans,
                   f"{len(m.spans)} spans, max gap {np.nanmax(m.gap_m):.2f} m")


def test_support_bend_radius_and_reaction() -> bool:
    # A sharp 2 m crest on a flat seabed, H = 5 kN, w = 10 N/m: the cable drapes
    # over it as two parabolas of curvature w/H, tangent to the seabed L = sqrt(2h/c)
    # away, turning through 2 atan(cL) at the crest. Reaction = weight of the two
    # half spans; bend radius 2 sqrt(EI/H) / turn.
    x = np.arange(0.0, 400.0, 0.5)
    seabed = _ridges(x, at=(200.0,))
    h, ei = 5000.0, 2.0
    c = W / h
    y = la.cable_rest_elevation(x, seabed, np.full_like(x, c))
    spans = la.find_spans(x, x / 1000.0, y - seabed, np.full_like(x, h / 1000.0), 0.3, 5.0)
    supports = la.span_supports(x, seabed, y, np.full_like(x, h / 1000.0), np.full_like(x, W),
                                np.full_like(x, ei), np.full_like(x, 8.0), spans)
    half = math.sqrt(2 * 2.0 / c)
    turn = 2 * math.atan(c * half)
    radius = 2 * math.sqrt(ei * 1000.0 / h) / turn
    crest = min(supports, key=lambda sp: abs(sp.kp - 0.2)) if supports else None
    ok = len(spans) == 2 and crest is not None
    ok = ok and abs(crest.reaction_kn - W * 2 * half / 1000.0) < 0.03 * W * 2 * half / 1000.0
    ok = ok and abs(crest.bend_radius_m - radius) < 0.05 * radius and crest.below_mbr
    ok = ok and all(s.below_mbr and abs(s.min_bend_radius_m - crest.bend_radius_m) < 1e-9 for s in spans)
    findings = la.seabed_span_findings(la.SeabedModel(x, x / 1000, -seabed, -y, np.full_like(x, 5.0),
                                                      np.full_like(x, W), spans=spans, supports=supports), {})
    ok = ok and all(f.severity == Severity.ERROR and "MBR" in f.message for f in findings)
    return _report("bend radius and reaction at a span support", ok,
                   f"R={crest.bend_radius_m if crest else None} expected {radius:.2f}; "
                   f"P={crest.reaction_kn if crest else None} expected {W * 2 * half / 1000:.3f}")


def test_allowable_tension() -> bool:
    # The highest tension with no reportable span: just below it the ridges are
    # spanned within the limits, a little above it they are not.
    x = np.arange(0.0, 400.0, 1.0)
    seabed = _ridges(x, height=1.0, slope=0.1)
    w = np.full_like(x, W)
    allowable = la.allowable_tension_kn(x, seabed, w, 0.3, 5.0, core_m=400.0, margin_m=0.0)
    value = float(np.nanmin(allowable))

    def spans_at(t_kn):
        y = la.cable_rest_elevation(x, seabed, w / (t_kn * 1000.0))
        return la.find_spans(x, x / 1000.0, y - seabed, np.full_like(x, t_kn), 0.3, 5.0)
    ok = 0.1 < value < 100.0 and not spans_at(0.95 * value) and spans_at(1.15 * value)
    return _report("allowable tension keeps spans within the limits", ok, f"{value:.3f} kN")


def test_ntts_breach_at_sheave_not_hidden() -> bool:
    # A joint in the water: LW (low NOTS, high NTTS) below, SA above. SA at the sheave
    # passes its NTTS while LW at the joint is "worse" against NOTS; the NTTS breach
    # must still be reported red.
    lw = la.CableProps("LW", weight_water_kg_m=10.0 / la.G, nots_kn=50.0, ntts_kn=300.0, npts_kn=20.0)
    sa = la.CableProps("SA", weight_water_kg_m=30.0 / la.G, nots_kn=150.0, ntts_kn=160.0, npts_kn=60.0)
    ds = _dataset(3, **{"TD KP": [0.880, 0.890, 0.900], "Meas.Top Tension": [170.0] * 3,
                        "Inst.Bot.Sl": [0.0] * 3, "Layback": [0.0] * 3, "TD_Lon_dd": [0.0] * 3})
    rec = la.build_records(ds, la.detect_roles(ds.field_names, ds.is_numeric_field))
    la.apply_makeup(rec, la.KpMakeup.from_ranges([(0.0, 1.0, "LW"), (1.0, 3.0, "SA")]),
                    lambda label: {"LW": lw, "SA": sa}.get(label))
    findings, _ = la.run_record_checks(rec, {"top_tension": {}})
    ok = len(findings) == 1 and findings[0].severity == Severity.ERROR and "SA" in findings[0].message
    return _report("NTTS breach at the sheave is not hidden by NOTS at a joint", ok,
                   str([(f.severity, f.message) for f in findings]))


def test_progress_and_cancel() -> bool:
    rec = _zero_slack_lay(2.0)
    x = np.arange(0.0, 2000.0, 2.0)
    seen = []
    la.model_seabed(x, x / 1000.0, _waves(x), rec, rec.kp * 1000.0, smoothing_m=None, friction=_friction(),
                    allowable=True, progress=lambda f, text=None: seen.append((f, text)))
    fractions = [f for f, _ in seen]
    ok = fractions and fractions[-1] == 1.0 and all(b >= a - 1e-9 for a, b in zip(fractions, fractions[1:]))
    ok = ok and any(text and "taut" in text for _, text in seen)
    try:
        la.model_seabed(x, x / 1000.0, _waves(x), rec, rec.kp * 1000.0, friction=_friction(),
                        cancel=lambda: True)
        cancelled = False
    except la.Cancelled:
        cancelled = True
    return _report("seabed model reports progress and can be cancelled", ok and cancelled,
                   f"{len(seen)} reports, cancelled={cancelled}")


def test_track_smoothing() -> bool:
    # Lateral jitter of the touchdown is averaged out of the sampling track; a
    # straight track (and its ends) is unchanged.
    n = 2000
    kp = np.arange(n) * 0.001
    deg = 1.0 / 111320.0
    jitter = np.random.default_rng(2).normal(0, 2.0, n) * deg
    rec = _records(_dataset(n, **{"TD KP": list(kp), "TD_Lon_dd": list(kp * 1000 * deg),
                                  "TD_Lat_dd": list(jitter)}))
    lon, lat, kp_v = la.track_vertices(rec)
    raw_lon, raw_lat, _ = la.track_vertices(rec, smoothing_m=0.0)
    ok = np.std(lat) / deg < 0.4 * np.std(raw_lat) / deg
    ok = ok and np.allclose(lon, raw_lon, atol=1e-12) and lon[0] == raw_lon[0] and lon[-1] == raw_lon[-1]
    return _report("touchdown track smoothed across, unchanged along", ok,
                   f"lateral std {np.std(raw_lat) / deg:.2f} -> {np.std(lat) / deg:.2f} m")


def test_sampling_helpers() -> bool:
    ok = abs(la.contour_interval_m([10.0, 10.5, 11.0, 10.5, 12.0, None]) - 0.5) < 1e-9
    ok = ok and abs(la.contour_interval_m([20.0, 21.0, 22.0, 21.0]) - 1.0) < 1e-9
    rec = _records(_dataset(50))
    ok = ok and abs(la.record_spacing_m(rec) - 2.0) < 1e-9
    return _report("contour interval and touchdown record spacing", ok)


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
        test_laid_cable_ignores_depth_noise(),
        test_taut_solver_station_spacing(),
        test_crests_hold_the_cable(),
        test_noise_makes_no_false_spans(),
        test_support_bend_radius_and_reaction(),
        test_allowable_tension(),
        test_ntts_breach_at_sheave_not_hidden(),
        test_progress_and_cancel(),
        test_track_smoothing(),
        test_sampling_helpers(),
    ]
    print(f"\n{sum(results)}/{len(results)} lay assessment checks passed.")
    return results


if __name__ == "__main__":
    run_all()
