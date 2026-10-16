# -*- coding: utf-8 -*-
"""Unit tests for the shared cable-on-seabed engine (``cable_seabed``).

Pure numpy, synthetic data only. Validation cases, with sources:

* Zajac 1957 (BSTJ 36(5), section 5.3, Fig. 22a): residual suspension over a
  210 ft step: half-span 480 ft; the vertex tension 409 lb follows from that
  geometry (the quoted 525 lb came from a nomograph).
* Fazio & Iacono 2020 (arXiv:2003.05485, section 3): heavy string from a
  point 1 above a flat obstacle, theta = w / H = 0.1: exact touchdown 4.4357.
* Closed forms: span sag ``w L^2 / 8H`` (plus ``P L / 4H`` for a mid-span
  load), tensioned beam over a knife edge (``R = 2 lambda / dtheta``,
  ``M = dtheta / 2 sqrt(EI T)``, ``P = T dtheta``), Coulomb friction on a
  slope (``w (sin phi - mu cos phi)``), the capstan ``exp(mu dtheta)``, and
  the friction anchor length ``dT / (mu w)``.
"""

from __future__ import annotations

import math
import os
import tempfile
import time
from typing import List

import numpy as np

from ..cable_seabed import beam, friction, planning as pl
from ..cable_seabed.features import BERM, BOULDER, PIPE, SeabedFeature, apply_features
from ..cable_seabed.profiles import SyntheticSeabed, read_profile_csv, synthetic_profile
from ..cable_seabed.shape import cable_rest_elevation

REQUIRES_QGIS = False


def _report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""))
    return bool(ok)


def _close(value, target, rel) -> bool:
    return abs(value - target) <= rel * abs(target)


# ---------------------------------------------------------------------------
# Rest shape (string)
# ---------------------------------------------------------------------------
def test_point_load_sag() -> bool:
    """Span between two sharp crests, with and without a mid-span load."""
    w, h, span = 10.0, 5000.0, 100.0
    x = np.arange(0.0, span + 0.05, 0.1)
    seabed = np.full(len(x), -50.0)
    seabed[0] = seabed[-1] = 0.0
    y0 = cable_rest_elevation(x, seabed, np.full(len(x), w / h))
    sag0 = -float(np.min(y0))
    jumps = np.zeros(len(x))
    load = 500.0
    jumps[len(x) // 2] = load / h
    y1 = cable_rest_elevation(x, seabed, np.full(len(x), w / h), jumps)
    sag1 = -float(np.min(y1))
    expect0 = w * span ** 2 / (8 * h)
    expect1 = expect0 + load * span / (4 * h)
    ok = _close(sag0, expect0, 0.01) and _close(sag1, expect1, 0.01)
    return _report("span sag w L^2/8H and point load P L/4H", ok,
                   f"{sag0:.3f} vs {expect0:.3f}; {sag1:.3f} vs {expect1:.3f}")


def test_zajac_residual_suspension() -> bool:
    """Zajac 1957 s5.3: a 210 ft step, cable 0.705 lb/ft; vertex tension 409 lb -> half-span 480 ft."""
    w, h0 = 0.705, 409.0     # lb/ft, lb (vertex tension from the quoted 480 ft half-span)
    x = np.arange(-300.0, 1200.0, 0.5)
    seabed = np.where(x <= 0.0, 210.0, np.maximum(0.0, 210.0 - x))  # 45 deg face below the edge
    y = cable_rest_elevation(x, seabed, np.full(len(x), w / h0))
    gap = y - seabed
    lifted = np.nonzero(gap > 1e-3)[0]
    touchdown = float(x[lifted[-1] + 1])
    top_tension = h0 + w * 210.0   # T - w y constant in a span (Zajac eq. 21)
    ok = _close(touchdown, 480.0, 0.05) and _close(top_tension, 525.0, 0.07)
    return _report("Zajac residual suspension: half-span 480 ft, top tension 525 lb", ok,
                   f"half-span {touchdown:.0f} ft, top tension {top_tension:.0f} lb")


def test_heavy_string_exact() -> bool:
    """Fazio & Iacono 2020: u'' = 0.1 sqrt(1 + u'^2), u(0) = 1 -> touchdown s = 4.4357."""
    x = np.arange(-20.0, 20.0001, 0.005)
    seabed = np.zeros(len(x))
    seabed[np.argmin(np.abs(x))] = 1.0
    y = cable_rest_elevation(x, seabed, np.full(len(x), 0.1))
    right = (x > 0) & (y - seabed > 1e-9)
    s = float(x[right][-1])
    return _report("heavy string over a point 1 above a flat bed: touchdown 4.4357 (small slope 0.8% long)",
                   _close(s, 4.4357, 0.015), f"{s:.4f}")


# ---------------------------------------------------------------------------
# Tensioned beam
# ---------------------------------------------------------------------------
def test_beam_knife_edge() -> bool:
    """EI 10 kNm2, T 10 kN, turn 0.1 rad: R 20 m, M 0.5 kNm, P 1 kN."""
    ei, t, turn = 10.0e3, 10.0e3, 0.1
    step = 0.02
    x = np.arange(-15.0, 15.0 + step / 2, step)
    n = len(x)
    obstacle = np.full(n, -100.0)
    k = int(np.argmin(np.abs(x)))
    obstacle[k] = 0.0
    lam = math.sqrt(ei / t)
    far = -0.5 * turn * np.abs(x) + 0.5 * turn * lam   # the straight part's asymptote
    result = beam.solve_tensioned_beam(x, obstacle, np.zeros(n), np.full(n, t), np.full(n, ei),
                                       y_ends=(far[0], far[1], far[-2], far[-1]))
    radius = float(np.min(result.bend_radius_m))
    moment = float(np.max(np.abs(result.moment_nm))) / 1000.0
    reaction = float(np.nansum(result.reaction_n)) / 1000.0
    ok = result.converged and _close(radius, 20.0, 0.04) and _close(moment, 0.5, 0.04) \
        and _close(reaction, 1.0, 0.03)
    return _report("tensioned beam over a knife edge: R = 2 lambda / turn, M, P = T turn", ok,
                   f"R {radius:.2f} m, M {moment:.3f} kNm, P {reaction:.3f} kN, {result.method}")


def test_beam_string_limit() -> bool:
    """With a tiny EI the beam rests like the string (the hull solution)."""
    w, t = 10.0, 3000.0
    x = np.arange(0.0, 200.0001, 0.25)
    seabed = -0.2 * np.abs(np.sin(x / 15.0)) * 4.0
    seabed[(x > 60) & (x < 62)] += 2.0
    string = cable_rest_elevation(x, seabed, np.full(len(x), w / t))
    result = beam.solve_tensioned_beam(x, seabed, np.full(len(x), w), np.full(len(x), t), np.full(len(x), 1e-3),
                                       y_ends=(string[0], string[1], string[-2], string[-1]), y_start=string)
    diff = float(np.max(np.abs(result.y_m - string)))
    return _report("beam with EI -> 0 matches the string solution", result.converged and diff < 0.02,
                   f"max difference {diff * 1000:.1f} mm")


def test_matches_drape_solver() -> bool:
    """Cross-check against the plugin's independent drape solver (dynamic relaxation, large rotation)."""
    from ..catenary.catenary_solver import PolylineSeabed
    from ..catenary.drape_solver import solve_drape

    w = 10.0
    xs = np.arange(0.0, 400.0001, 0.5)
    depth = np.full(len(xs), 100.0)
    for centre in (150.0, 250.0):
        depth -= np.clip(2.0 - np.abs(xs - centre) / 5.0, 0.0, None)
    seabed = PolylineSeabed(list(xs), list(depth), slope_smoothing_m=0.0)
    length = float(np.sum(np.hypot(np.diff(xs), np.diff(depth)))) - 0.4
    drape = solve_drape(seabed, (0.0, -100.0), length, w, bottom_anchor_xy=(400.0, -100.0), n_nodes=400)
    h_n = float(np.median(drape.tension_kN)) * 1000.0
    y = cable_rest_elevation(xs, -depth, np.full(len(xs), w / h_n))
    y_drape = np.interp(xs, drape.x, drape.y)
    core = (xs > 20.0) & (xs < 380.0)
    rms = float(np.sqrt(np.mean((y[core] - y_drape[core]) ** 2)))
    gap_ours = float(np.max(y + depth))
    gap_drape = float(np.max(drape.clearance_m))
    # The drape resolves contact to its node spacing (1 m) with millimetre penalty penetration.
    ok = drape.converged and rms < 0.05 and abs(gap_ours - gap_drape) < max(0.05, 0.1 * gap_drape)
    return _report("rest shape matches the drape solver at its tension", ok,
                   f"T {h_n / 1000:.2f} kN, RMS {rms * 1000:.0f} mm, max gap {gap_ours:.2f} vs {gap_drape:.2f} m")


def test_penta_solver() -> bool:
    rng = np.random.default_rng(3)
    n = 200
    d2 = rng.uniform(-1, 1, n - 2)
    d1 = rng.uniform(-1, 1, n - 1)
    d0 = np.abs(d1).sum() * 0 + 6.0 + rng.uniform(0, 1, n)
    a = np.diag(d0) + np.diag(d1, 1) + np.diag(d1, -1) + np.diag(d2, 2) + np.diag(d2, -2)
    b = rng.normal(size=n)
    xs = beam.penta_solve(d0, d1, d2, b)
    return _report("banded LDL^T solve", np.allclose(a @ xs, b, atol=1e-9))


# ---------------------------------------------------------------------------
# Friction and lay history
# ---------------------------------------------------------------------------
def _slope_band(phi_deg=20.0, mu=0.3, w=100.0, length=200.0, step=0.5):
    x = np.arange(0.0, length + step / 2, step)
    y = -x * math.tan(math.radians(phi_deg))       # deepening with x
    contact = np.ones(len(x), dtype=bool)
    reaction = friction.station_reactions(x, y, np.full(len(x), w), np.zeros(len(x)), contact)
    lower, upper, gravity = friction.friction_band(x, y, np.full(len(x), w), np.full(len(x), mu), reaction)
    return x, y, lower, upper, gravity


def test_friction_on_slope() -> bool:
    """20 deg slope, mu 0.3: laid down-slope at zero tension, tension builds up-slope at w(sin - mu cos)."""
    phi, mu, w = 20.0, 0.3, 100.0
    x, _y, lower, upper, gravity = _slope_band(phi, mu, w)
    t, unheld = friction.lay_history_tension(np.zeros(len(x)), lower, upper, gravity, direction=1)
    along = x[-1] / math.cos(math.radians(phi))
    expect = w * (math.sin(math.radians(phi)) - mu * math.cos(math.radians(phi))) * along
    ok_down = _close(float(t[0]), expect, 0.01) and not unheld.any()
    t_up, unheld_up = friction.lay_history_tension(np.zeros(len(x)), lower, upper, gravity, direction=-1)
    ok_up = unheld_up[1:].all() and float(np.max(t_up)) == 0.0
    return _report("slope steeper than friction: tension builds up-slope at 6.01 N/m; laid up-slope it slides",
                   ok_down and ok_up, f"top {t[0]:.0f} N vs {expect:.0f} N")


def test_friction_anchor_length() -> bool:
    """A 10 kN step on a flat seabed decays over dT / (mu w) = 100 m."""
    w, mu = 200.0, 0.5
    x = np.arange(0.0, 1000.0001, 1.0)
    y = np.zeros(len(x))
    reaction = friction.station_reactions(x, y, np.full(len(x), w), np.zeros(len(x)), np.ones(len(x), bool))
    lower, upper, gravity = friction.friction_band(x, y, np.full(len(x), w), np.full(len(x), mu), reaction)
    deposit = np.where(x >= 500.0, 10000.0, 0.0)
    t, _ = friction.lay_history_tension(deposit, lower, upper, gravity, 1)
    ok = _close(float(t[450]), 5000.0, 0.02) and float(t[399]) < 1.0 and _close(float(t[500]), 10000.0, 1e-6)
    return _report("friction anchor length dT / (mu w)", ok, f"T(450) {t[450]:.0f} N")


def test_capstan() -> bool:
    """Weightless cable over a 0.5 rad crest, mu 0.5: tension ratio exp(0.25) = 1.284."""
    radius, mu = 10.0, 0.5
    ang = np.linspace(-0.25, 0.25, 501)
    x = radius * np.sin(ang)
    y = radius * np.cos(ang)
    w = np.zeros(len(x))
    deposit = np.full(len(x), 1000.0)
    deposit[-1] = 5000.0
    t = deposit.copy()
    for _ in range(60):
        h = t * np.cos(ang)
        reaction = friction.station_reactions(x, y, w, h, np.ones(len(x), bool))
        lower, upper, gravity = friction.friction_band(x, y, w, np.full(len(x), mu), reaction)
        t, _ = friction.lay_history_tension(deposit, lower, upper, gravity, 1)
    ratio = float(t[-1] / t[0])
    return _report("capstan: exp(mu turn) over a crest", _close(ratio, math.exp(0.25), 0.02), f"{ratio:.4f}")


def test_coarse_march_matches_fine() -> bool:
    x, _y, lower, upper, gravity = _slope_band(10.0, 0.5, 50.0, length=3000.0, step=0.5)
    deposit = 2000.0 + 1500.0 * (np.sin(x / 300.0) > 0)
    fine, _ = friction.lay_history_tension(deposit, lower, upper, gravity, 1, max_nodes=10 ** 7)
    coarse, _ = friction.lay_history_tension(deposit, lower, upper, gravity, 1, max_nodes=600)
    diff = float(np.max(np.abs(fine - coarse)))
    # Steps in the deposit land to within one coarse cell: allow the friction gradient over a cell.
    cell = x[-1] / 599.0
    allowed = 2.0 * 0.5 * 50.0 * cell
    return _report("long-route march on coarse nodes matches the fine march", diff < allowed,
                   f"max difference {diff:.0f} N, allowed {allowed:.0f} N")


# ---------------------------------------------------------------------------
# Planning model
# ---------------------------------------------------------------------------
LW = pl.CableType("LW", w_npm=10.0, ei_knm2=1.0, mbr_m=1.5, diameter_m=0.02, npts_kn=20.0)


def _inputs(profile, control=None, **kwargs):
    return pl.PlanningInputs(profile, kwargs.pop("cable", LW), control=control or pl.LayControl(pl.TENSION, 3.0),
                             filter_m=kwargs.pop("filter_m", 0.0), **kwargs)


def _flat(length=600.0, step=0.5, depth=100.0):
    x = np.arange(0.0, length + step / 2, step)
    return pl.Profile(x, x / 1000.0, np.full(len(x), depth))


def test_boulder_span() -> bool:
    """A 1 m boulder on a flat seabed at 5 kN, w 10 N/m: spans either side sqrt(2 h T / w) = 31.6 m."""
    prof = _flat()
    feature = SeabedFeature(BOULDER, 0.3, 1.0, 2.0)
    result = pl.run(_inputs(prof, pl.LayControl(pl.TENSION, 5.0), features=[feature]))
    spans = result.spans
    expect = 2.0 * math.sqrt(2.0 * 1.0 * 5000.0 / 10.0)
    total = sum(s.length_m for s in spans)
    ok = len(spans) == 2 and _close(total, expect, 0.05)
    return _report("boulder: a span either side, each sqrt(2 h T / w) long (lift-off to touchdown)", ok,
                   f"{len(spans)} span(s), {total:.1f} m vs {expect:.1f}")


def test_monotone_in_tension() -> bool:
    """Binda & Contro: with constant tension, spans only grow as tension rises (gentle relief)."""
    spec = SyntheticSeabed(length_m=2000.0, spacing_m=0.5, roughness_m=0.1, roughness_length_m=8.0,
                           wave_height_m=0.5, wave_length_m=60.0, seed=4)
    st = pl.prepare(_inputs(synthetic_profile(spec)))
    rows = pl.tension_sweep(st, pl.LayControl(), pl.Criteria(min_gap_m=0.05, min_length_m=0.0),
                            [0.1, 0.25, 0.5, 1.0, 2.0, 4.0])
    lengths = [r.span_length_m for r in rows]
    ok = all(b >= a - 1e-6 for a, b in zip(lengths, lengths[1:])) and lengths[-1] > lengths[0]
    return _report("total span length rises monotonically with tension", ok,
                   ", ".join(f"{v:.0f}" for v in lengths))


def test_slack_rule() -> bool:
    """Sand waves: slack above the fill lays at zero tension (surplus); below it, pulled taut."""
    spec = SyntheticSeabed(length_m=3000.0, spacing_m=1.0, wave_height_m=2.0, wave_length_m=40.0)
    prof = synthetic_profile(spec)
    slack = pl.run(_inputs(prof, pl.LayControl(pl.ROUTE_SLACK, 4.0), criteria=pl.Criteria(allowable=False)))
    fill = slack.summary.fill_pct
    mid = slice(len(prof.x_m) // 4, 3 * len(prof.x_m) // 4)
    ok_slack = float(np.nanmax(slack.tension_kn[mid])) <= 0.05 and not slack.spans \
        and _close(float(np.nanmean(slack.excess_pct[mid])), 4.0 - fill, 0.15)
    taut = pl.run(_inputs(prof, pl.LayControl(pl.ROUTE_SLACK, 0.2 * fill), criteria=pl.Criteria(allowable=False)))
    ok_taut = float(np.nanmin(taut.tension_kn[mid])) > 0.05 and len(taut.spans) > 0 \
        and float(np.nanmax(np.abs(taut.excess_pct[mid]))) < 0.3
    return _report("slack above the fill: zero tension + surplus; below: pulled taut, spans, no surplus",
                   ok_slack and ok_taut, f"fill {fill:.2f}%, taut T {np.nanmean(taut.tension_kn[mid]):.2f} kN")


def test_bodies() -> bool:
    """A repeater in a span pulls it down; its housing standoff alone is not a span."""
    prof = _flat()
    feature = SeabedFeature(BERM, 0.2, 1.5, 4.0, crest_m=2.0)
    base = pl.run(_inputs(prof, pl.LayControl(pl.TENSION, 5.0), features=[feature]))
    body = pl.Body("R1", 0.215, length_m=3.0, weight_kn=3.0, diameter_m=0.3, kind="Repeater")
    loaded = pl.run(_inputs(prof, pl.LayControl(pl.TENSION, 5.0), features=[feature], bodies=[body]))
    gap0 = float(np.nanmax(base.gap_m[(base.kp_km > 0.21) & (base.kp_km < 0.22)]))
    gap1 = float(np.nanmax(loaded.gap_m[(loaded.kp_km > 0.21) & (loaded.kp_km < 0.22)]))
    flat_body = pl.Body("R2", 0.5, length_m=3.0, weight_kn=3.0, diameter_m=0.3, kind="Repeater")
    flat = pl.run(_inputs(prof, pl.LayControl(pl.TENSION, 5.0), bodies=[flat_body]))
    ok = gap1 < gap0 - 0.05 and not [s for s in flat.spans if 0.49 < s.kp_start < 0.51]
    return _report("repeater weight pulls a span down; housing standoff is no span", ok,
                   f"gap {gap0:.2f} -> {gap1:.2f} m")


def test_alter_course_and_npts() -> bool:
    prof = _flat()
    prof.turns = [(0.3, 15.0)]
    result = pl.run(_inputs(prof, pl.LayControl(pl.TENSION, 25.0)))
    turn = [f for f in result.findings if f.check_id == "alter_course"]
    npts = [f for f in result.findings if f.check_id == "npts"]
    expect = 25000.0 / (0.5 * 10.0)
    ok = len(turn) == 1 and _close(float(turn[0].value), expect, 0.01) and len(npts) == 1
    return _report("alter course turn radius T / (mu w) and NPTS finding", ok)


def test_pipe_crossing_detail() -> bool:
    """A pipeline crossing: the detail (EI) model converges and agrees with the route model."""
    prof = _flat(400.0, 0.5)
    pipe = SeabedFeature(PIPE, 0.2, 0.5, 0.5)
    cable = pl.CableType("DA", w_npm=60.0, ei_knm2=20.0, mbr_m=2.0, diameter_m=0.05, npts_kn=60.0)
    result = pl.run(_inputs(prof, pl.LayControl(pl.TENSION, 10.0), features=[pipe], cable=cable))
    span = result.spans[0] if result.spans else None
    detail = pl.detail_window(result, 0.12, 0.28)
    ok = span is not None and detail.beam.converged and len(detail.spans) == len(result.spans) \
        and _close(detail.spans[0].span.length_m, span.length_m, 0.15)
    lam = math.sqrt(20.0 / 10.0)
    return _report("pipe crossing: detail model with EI agrees with the route span", ok,
                   f"route {span.length_m if span else 0:.1f} m, detail "
                   f"{detail.spans[0].span.length_m if detail.spans else 0:.1f} m, lambda {lam:.2f} m, "
                   f"R min {detail.min_bend_radius_m:.1f} m")


def test_features_shape() -> bool:
    x = np.arange(0.0, 20.0001, 1.0)
    kp = x / 1000.0
    out = apply_features(x, kp, [SeabedFeature(PIPE, 0.010, 0.3, 0.6)], np.zeros(len(x)))
    xs, _k, height, _v = out
    peak = float(np.max(height))
    return _report("features add stations and height (pipe lying on the seabed)", _close(peak, 0.3, 1e-6)
                   and len(xs) > len(x))


def test_csv_profile() -> bool:
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "profile.csv")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("Chainage (m),Elevation (m)\n0,-100\n10,-101\n20,-102\n30,-101.5\n")
    prof = read_profile_csv(path)
    ok = np.allclose(prof.depth_m, [100, 101, 102, 101.5]) and np.allclose(prof.kp_km, [0, 0.01, 0.02, 0.03])
    return _report("CSV profile: chainage and elevation columns", ok)


def test_long_route_speed() -> bool:
    spec = SyntheticSeabed(length_m=50000.0, spacing_m=1.0, slope_deg=0.5, wave_height_m=0.8, wave_length_m=80.0,
                           roughness_m=0.2, roughness_length_m=5.0)
    started = time.perf_counter()
    result = pl.run(_inputs(synthetic_profile(spec), pl.LayControl(pl.TENSION, 4.0),
                            criteria=pl.Criteria(allowable=False)))
    took = time.perf_counter() - started
    return _report("50 km at 1 m solves in under 30 s", took < 30.0 and result.summary.route_m > 49000.0,
                   f"{took:.1f} s, {result.iterations} iterations, {len(result.spans)} spans")


def run_all() -> List[bool]:
    results = [
        test_point_load_sag(),
        test_zajac_residual_suspension(),
        test_heavy_string_exact(),
        test_beam_knife_edge(),
        test_beam_string_limit(),
        test_matches_drape_solver(),
        test_penta_solver(),
        test_friction_on_slope(),
        test_friction_anchor_length(),
        test_capstan(),
        test_coarse_march_matches_fine(),
        test_boulder_span(),
        test_monotone_in_tension(),
        test_slack_rule(),
        test_bodies(),
        test_alter_course_and_npts(),
        test_pipe_crossing_detail(),
        test_features_shape(),
        test_csv_profile(),
        test_long_route_speed(),
    ]
    print(f"\n{sum(results)}/{len(results)} cable-on-seabed engine checks passed.")
    return results


if __name__ == "__main__":
    run_all()
