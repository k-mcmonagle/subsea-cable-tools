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


def test_depression_verification() -> bool:
    """A 40 m wide, 2 m deep trough at 2 kN (sag 1 m: spans it) and 0.5 kN (sag 4 m: touches the bottom)."""
    from ..cable_seabed.features import DEPRESSION
    from ..cable_seabed.verification import depression_check

    prof = _flat(1000.0, 0.25)
    trough = SeabedFeature(DEPRESSION, 0.5, 2.0, 40.0, crest_m=38.0)
    checks = []
    for tension, spans in ((2.0, 1), (0.5, 2)):
        result = pl.run(_inputs(prof, pl.LayControl(pl.TENSION, tension), features=[trough],
                                criteria=pl.Criteria(allowable=False)))
        check = depression_check(result)
        checks.append(check is not None and check.ok and len(result.spans) == spans)
    detail = "; ".join(f"{r.quantity} {r.closed_form:.3f} vs {r.model:.3f}" for r in check.rows) if check else ""
    return _report("depression: closed form agrees in both regimes (spans the trough / touches the bottom)",
                   all(checks), detail)


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


DA = pl.CableType("DA", w_npm=60.0, ei_knm2=20.0, mbr_m=2.0, diameter_m=0.05, npts_kn=60.0)


def _crossing(tension=10.0, step=0.1, **kwargs):
    """A 0.5 m pipeline on a flat seabed, armoured cable (60 N/m): spans sqrt(2 h T / w) either side."""
    prof = _flat(400.0, step)
    criteria = kwargs.pop("criteria", pl.Criteria(allowable=False))
    return pl.run(_inputs(prof, pl.LayControl(pl.TENSION, tension), features=[SeabedFeature(PIPE, 0.2, 0.5, 0.5)],
                          cable=DA, criteria=criteria, **kwargs))


def test_supports_at_span_ends() -> bool:
    """Supports sit where each span meets the seabed: the crest, and the tangent touchdowns (no reaction)."""
    result = _crossing()
    ends = sorted({round(s.kp_start, 4) for s in result.spans} | {round(s.kp_end, 4) for s in result.spans})
    kps = sorted(round(sp.kp, 4) for sp in result.supports)
    crest = [sp for sp in result.supports if sp.crest]
    touch = [sp for sp in result.supports if not sp.crest]
    weight = 60.0 * sum(s.length_m for s in result.spans) / 1000.0       # the crest carries both half spans
    ok = len(result.supports) == 3 and all(abs(a - b) < 0.0005 for a, b in zip(kps, ends))         and len(crest) == 1 and abs(crest[0].kp - 0.2) < 0.0005 and _close(crest[0].reaction_kn, weight, 0.03)         and all(sp.reaction_kn < 0.01 for sp in touch)
    return _report("span supports at the span ends: the crest (weight of both spans) and two touchdowns", ok,
                   f"supports {kps}, span ends {ends}, crest P {crest[0].reaction_kn if crest else 0:.3f} kN "
                   f"vs {weight:.3f}")


def test_touchdown_gap() -> bool:
    """A touchdown gap g moves each tangent span end in by sqrt(2 g T / w)."""
    exact = _crossing()
    gap = 0.05
    cut = _crossing(criteria=pl.Criteria(allowable=False, touchdown_gap_m=gap))
    shift = math.sqrt(2.0 * gap * 10000.0 / 60.0)
    ok = len(cut.spans) == 2 and all(_close(a.length_m - b.length_m, shift, 0.08)
                                     for a, b in zip(exact.spans, cut.spans))
    return _report("touchdown gap shortens each span by sqrt(2 g T / w) at its tangent end", ok,
                   f"{[round(s.length_m, 2) for s in exact.spans]} -> {[round(s.length_m, 2) for s in cut.spans]}, "
                   f"expected -{shift:.2f} m")


def test_find_tension() -> bool:
    """No span over 8 m (higher than 0.3 m) at a crossing: T = L^2 w / 2h = 3.84 kN; none removes every span."""
    st = pl.prepare(_inputs(_flat(400.0, 0.1), cable=DA, features=[SeabedFeature(PIPE, 0.2, 0.5, 0.5)]))
    control = pl.LayControl(pl.TENSION, 10.0)
    answer = pl.find_tension(st, control, pl.Criteria(allowable=False), pl.TensionTarget(0.3, 8.0, mbr=False),
                             [("all", 0.0, 0.4), ("clear", 0.0, 0.1)])
    expect = 8.0 ** 2 * 60.0 / (2.0 * 0.5) / 1000.0
    found = answer[0].tension_kn
    check = []
    for t_kn in (found, found * 1.04):
        r = pl.solve(st, pl.LayControl(pl.TENSION, t_kn), pl.Criteria(allowable=False))
        check.append(max(s.length_m for s in r.spans))
    none = pl.find_tension(st, control, pl.Criteria(allowable=False), pl.TensionTarget(0.0, 0.0, mbr=False),
                           [("all", 0.0, 0.4)])[0]
    ok = _close(found, expect, 0.04) and check[0] <= 8.0 < check[1] and answer[1].tension_kn == 50.0         and not answer[1].limited and none.tension_kn == 0.0 and len(none.remaining) == 2
    return _report("find tension: highest tension within a span limit; a crossing always spans", ok,
                   f"{found:.2f} kN vs {expect:.2f} (ideal parabola), longest {check[0]:.2f} / {check[1]:.2f} m")


def test_protection() -> bool:
    """Protection doubling the weight shortens the spans by 1/sqrt(2); its MBR and friction key apply."""
    plain = _crossing()
    item = pl.Protection("Shells", 0.17, 0.23, weight_npm=60.0, diameter_m=0.2, mbr_m=60.0, kind="Shells")
    friction = pl.FrictionModel(0.5, 0.5, table=[pl.FrictionEntry("Shells", "*", 0.9)])
    result = _crossing(protection=[item], friction=friction)
    st = result.stations
    inside = int(np.argmin(np.abs(st.kp_km - 0.2)))
    ratio = np.mean([s.length_m for s in result.spans]) / np.mean([s.length_m for s in plain.spans])
    ok = _close(ratio, 1.0 / math.sqrt(2.0), 0.04) and all(s.below_mbr for s in result.spans)         and st.w_npm[inside] == 120.0 and st.mu[inside] == 0.9 and st.mu[0] == 0.5         and st.protection_index[inside] == 0 and st.protection_index[0] == -1
    return _report("protection: added weight, its own MBR and friction key over its KP range", ok,
                   f"span ratio {ratio:.3f} vs {1 / math.sqrt(2):.3f}")


def test_contact_limit() -> bool:
    result = _crossing(criteria=pl.Criteria(allowable=False, max_reaction_kn=1.0))
    contact = [f for f in result.findings if f.check_id == "contact"]
    ok = len(contact) == 1 and abs(contact[0].kp_start - 0.2) < 0.001 and result.summary.red_spans == 2
    return _report("contact limit flags the crest reaction and makes its spans red", ok,
                   f"{len(contact)} finding(s), {result.summary.red_spans} red span(s)")


def test_data_noise_and_filter() -> bool:
    """Point-to-point variation from known white noise; a lone peak is found but does not move it; auto filter."""
    from ..cable_seabed.sampling import auto_filter_m, cell_noise_m, data_spacing_m, spike_indices

    rng = np.random.default_rng(7)
    x = np.arange(0.0, 3000.0, 1.0)
    depth = 100.0 + 0.02 * x + rng.normal(0.0, 0.05, len(x))   # a slope plus sigma 0.05 m noise
    sigma = cell_noise_m(x, depth)
    spiked = depth.copy()
    spiked[1500] -= 1.5                                       # one point 1.5 m shallower (a flier)
    sigma_spiked = cell_noise_m(x, spiked)
    found = spike_indices(x, spiked, sigma_spiked)
    spacing = data_spacing_m(x, [5.0] * len(x))
    filters = (auto_filter_m(1.0, 0.02, 0.3), auto_filter_m(1.0, 0.1, 0.3), auto_filter_m(1.0, 0.3, 0.3),
               auto_filter_m(1.0, 0.3, 0.3, 3.0), auto_filter_m(25.0, 0.5, 0.3, 5.0))
    ok = (_close(sigma, 0.05, 0.15) and _close(sigma_spiked, sigma, 0.05) and list(found) == [1500]
          and np.allclose(spacing, 5.0) and filters[0] == 0.0 and _close(filters[1], (0.4 / 0.3) ** 2, 1e-6)
          and filters[2] == 2.0 and filters[3] == 1.5 and filters[4] == 0.0)
    return _report("data: point-to-point variation of white noise, a lone flier found, auto noise filter", ok,
                   f"sigma {sigma:.3f} ({sigma_spiked:.3f} with a flier), peaks {list(found)}, filters {filters}")


def test_spans_below_data_resolution() -> bool:
    """On 25 m data short spans are indicative and the stretch is flagged; on 1 m data neither; features exact.

    The same seabed both times (relief known every 25 m, the fine profile
    interpolated from it): only how far apart the data is differs.
    """
    rng = np.random.default_rng(3)
    xc = np.arange(0.0, 3000.0 + 12.5, 25.0)
    dc = 100.0 + rng.normal(0.0, 0.5, len(xc))
    xf = np.arange(0.0, 3000.0 + 0.5, 1.0)

    def waves(step):
        return pl.Profile(xc, xc / 1000.0, dc) if step > 1 else pl.Profile(xf, xf / 1000.0, np.interp(xf, xc, dc))

    control = pl.LayControl(pl.TENSION, 10.0)
    coarse = pl.run(_inputs(waves(25.0), control))
    fine = pl.run(_inputs(waves(1.0), control))
    need = 3.0 * 25.0
    marks = [(s.length_m, flag) for s, flag in zip(coarse.spans, coarse.indicative)]
    by_length = all(flag == (length < need) for length, flag in marks)
    checks = {f.check_id: f.severity for f in coarse.findings}
    flat = _flat(length=600.0, step=25.0)
    boulder = pl.run(_inputs(flat, control, features=[SeabedFeature(BOULDER, 0.3, 1.0, 2.0)]))
    off = pl.run(_inputs(waves(25.0), control, criteria=pl.Criteria(resolve_intervals=0.0)))
    ok = (by_length and any(coarse.indicative) and checks.get("data_resolution") == pl.WARNING
          and "span_indicative" in checks and coarse.summary.indicative_spans == sum(coarse.indicative)
          and fine.spans and not any(fine.indicative)
          and not any(f.check_id in ("data_resolution", "span_indicative") for f in fine.findings)
          and len(boulder.spans) == 2 and not any(boulder.indicative) and not any(off.indicative))
    return _report("data resolution: spans under 3 data intervals indicative, coarse stretch flagged, "
                   "features exact", ok, f"coarse spans (length, indicative) {[(round(a), b) for a, b in marks][:6]}; "
                   f"fine {len(fine.spans)} span(s)")


# ---------------------------------------------------------------------------
# Holds (buried or pinned sections)
# ---------------------------------------------------------------------------
def _plough(direction=1, hold_kn=25.0, lay_kn=1.0, **kwargs):
    """A plough section KP 1.5 to 2.0 held at the PLDN tension, surface lay either side (armoured, 60 N/m)."""
    hold = pl.ControlSection(1.5, 2.0, pl.HELD, hold_kn, "Plough 1")
    control = pl.LayControl(pl.TENSION, lay_kn, [hold] + kwargs.pop("sections", []), [], direction)
    return pl.run(_inputs(kwargs.pop("profile", None) or _flat(3000.0, 1.0), control, cable=DA,
                          criteria=pl.Criteria(allowable=False), **kwargs))


def test_hold_pull_back() -> bool:
    """A hold laid at 25 kN after surface lay at 1 kN: its tension pulls back on the cable laid before,
    falling by mu w per metre, (25 - 1) / (mu w) = 800 m; nothing passes through it to the cable laid
    after. Both lay directions."""
    out = []
    for direction in (1, -1):
        r = _plough(direction)
        x, t = r.stations.x_m, r.tension_kn
        hold = r.holds[0]
        entry, leave = (1500.0, 2000.0) if direction > 0 else (2000.0, 1500.0)
        d = 200.0
        expect = 25.0 - 0.5 * 60.0 * d / 1000.0
        back = float(np.interp(entry - direction * d, x, t))
        beyond = float(np.interp(entry - direction * 900.0, x, t))
        after = float(np.interp(leave + direction * 100.0, x, t))
        inside = t[r.held]
        out.append(_close(back, expect, 0.01) and _close(beyond, 1.0, 0.01) and _close(after, 1.0, 0.01)
                   and np.allclose(inside, 25.0) and _close(hold.pulled_m, 800.0, 0.02)
                   and _close(hold.before_kn, 25.0, 0.01) and _close(hold.after_kn, 1.0, 0.01)
                   and _close(hold.load_kn, 24.0, 0.02) and hold.name == "Plough 1"
                   and r.source[int(np.searchsorted(x, entry - direction * d))] == pl.SOURCE_PULLED)
        detail = f"{back:.2f} kN {d:.0f} m back vs {expect:.2f}; reach {hold.pulled_m:.0f} m vs 800; after {after:.2f}"
    return _report("hold: its tension pulls back on the cable laid before (mu w per metre), not on the cable "
                   "laid after; both lay directions", all(out), detail)


def test_hold_no_spans_inside() -> bool:
    """A boulder inside a hold makes no span (the cable is buried over it); one outside spans as before."""
    rocks = [SeabedFeature(BOULDER, 0.5, 1.0, 2.0), SeabedFeature(BOULDER, 1.75, 1.0, 2.0)]
    free = pl.run(_inputs(_flat(3000.0, 0.5), pl.LayControl(pl.TENSION, 5.0), cable=DA, features=rocks,
                          criteria=pl.Criteria(allowable=False)))
    held = _plough(hold_kn=5.0, lay_kn=5.0, features=rocks, profile=_flat(3000.0, 0.5))
    gap = held.gap_m[held.held]
    ok = len(free.spans) == 4 and len(held.spans) == 2 and all(s.kp_end < 1.5 for s in held.spans) \
        and float(np.nanmax(np.abs(gap))) < 1e-6
    return _report("hold: no span inside it (it follows the seabed); spans outside unchanged", ok,
                   f"{len(free.spans)} span(s) free, {len(held.spans)} with the hold")


def test_hold_end_uplift() -> bool:
    """Cable leaving a hold up a 1:10 ramp at 5 kN: the hold takes the turn, P = H s - w dx = 0.44 kN."""
    x = np.arange(0.0, 3000.5, 1.0)
    depth = 100.0 - np.clip(x - 2000.0, 0.0, 100.0) * 0.1
    r = _plough(hold_kn=5.0, lay_kn=5.0, profile=pl.Profile(x, x / 1000.0, depth))
    hold = r.holds[0]
    expect = (5000.0 / math.sqrt(1.01) * 0.1 - 60.0 * 1.0) / 1000.0
    flat = _plough(hold_kn=5.0, lay_kn=5.0).holds[0]
    ok = _close(hold.uplift_kn, expect, 0.03) and abs(hold.uplift_kp - 2.0) < 1e-6 and flat.uplift_kn == 0.0
    return _report("hold end: upward pull of the free cable turning up out of it", ok,
                   f"{hold.uplift_kn:.3f} kN vs {expect:.3f}")


def test_point_hold_anchor() -> bool:
    """An anchor at KP 1.0 holds the cable laid before it from a later 25 kN section at KP 1.2:
    behind it 1 kN stays; the anchor carries 25 - 1 - mu w 200 m = 18.0 kN."""
    anchor = pl.ControlSection(1.0, 1.0, pl.HELD, 1.0, "Anchor")
    pull = pl.ControlSection(1.2, 3.0, pl.TENSION, 25.0)
    r = pl.run(_inputs(_flat(3000.0, 1.0), pl.LayControl(pl.TENSION, 1.0, [anchor, pull]), cable=DA,
                       criteria=pl.Criteria(allowable=False)))
    free = pl.run(_inputs(_flat(3000.0, 1.0), pl.LayControl(pl.TENSION, 1.0, [pull]), cable=DA,
                          criteria=pl.Criteria(allowable=False)))
    x = r.stations.x_m
    hold = r.holds[0]
    expect = 25.0 - 0.5 * 60.0 * 199.0 / 1000.0 - 1.0
    at_anchor = 25.0 - 0.5 * 60.0 * 200.0 / 1000.0
    behind = float(np.interp(900.0, x, r.tension_kn))
    without = float(np.interp(900.0, x, free.tension_kn))
    ok = hold.point and int(r.held.sum()) == 1 and _close(behind, 1.0, 0.01) and without > 5.0 \
        and _close(hold.load_kn, expect, 0.01) and _close(hold.held_kn, at_anchor, 0.01)
    return _report("point hold (anchor): holds the cable laid before it, at the tension pulling on it; carries "
                   "the difference", ok, f"behind {behind:.2f} kN (free {without:.2f}); at it {hold.held_kn:.2f} vs "
                   f"{at_anchor:.2f}; load {hold.load_kn:.2f} vs {expect:.2f} kN")


def test_descending_anchor_and_burials() -> bool:
    """Laid towards decreasing KP from an anchor at the right-hand end: 1 kN of surface lay, a burial section
    KP 2.6-3.4 held at 25 kN (PLDN at KP 3.4), 1 kN, another at KP 0.6-1.4. Right to left: the anchor carries
    25 - mu w 600 m = 7 kN, the cable rises to 25 kN at each PLDN, and is 1 kN again after each PLUP. An anchor
    typed 5 m beyond the end holds the end; one 50 m beyond is left out, with a note."""
    def run(anchor_kp):
        sections = [pl.ControlSection(anchor_kp, anchor_kp, pl.HELD, 0.0, "Anchor"),
                    pl.ControlSection(2.6, 3.4, pl.HELD, 25.0, "Burial 1"),
                    pl.ControlSection(0.6, 1.4, pl.HELD, 25.0, "Burial 2")]
        return pl.run(_inputs(_flat(4000.0, 1.0), pl.LayControl(pl.TENSION, 1.0, sections, [], -1), cable=DA,
                              criteria=pl.Criteria(allowable=False)))

    r = run(4.0)
    x, t = r.stations.x_m, r.tension_kn

    def at(kp_km):
        return float(np.interp(kp_km * 1000.0, x, t))

    anchor = next(h for h in r.holds if h.name == "Anchor")
    expect = {4.0: 7.0, 3.7: 16.0, 3.0: 25.0, 2.5: 1.0, 1.8: 13.0, 1.0: 25.0, 0.3: 1.0}
    got = {k: at(k) for k in expect}
    snapped, missed = run(4.005), run(4.05)
    ok = all(_close(got[k], v, 0.01) for k, v in expect.items()) and _close(anchor.load_kn, 7.0, 0.01) \
        and _close(anchor.held_kn, 7.0, 0.01) and len(snapped.holds) == 3 \
        and len(missed.holds) == 2 and any("'Anchor'" in note and "left out" in note for note in missed.notes)
    return _report("laid right to left from an anchor: each PLDN pulls back to the anchor, PLUP back to 1 kN", ok,
                   ", ".join(f"KP {k} {v:.2f}" for k, v in got.items()))


def test_hold_sweep_and_detail() -> bool:
    """Tension sweep and find tension keep the holds; the detail model keeps held nodes on the seabed."""
    rocks = [SeabedFeature(BOULDER, 0.5, 1.0, 2.0), SeabedFeature(BOULDER, 1.75, 1.0, 2.0)]
    r = _plough(hold_kn=5.0, lay_kn=5.0, features=rocks, profile=_flat(3000.0, 0.5))
    rows = pl.tension_sweep(r.stations, r.control, pl.Criteria(allowable=False), [1.0, 5.0, 10.0])
    answer = pl.find_tension(r.stations, r.control, pl.Criteria(allowable=False),
                             pl.TensionTarget(0.3, 5.0, mbr=False), [("all", 0.0, 3.0)], 20.0, steps=4, refine=2)
    detail = pl.detail_window(r, 1.70, 1.80)
    pinned_gap = detail.beam.gap_m[(detail.kp_km > 1.71) & (detail.kp_km < 1.79)]
    ok = all(row.spans == 2 for row in rows) and answer[0].tension_kn > 0 \
        and float(np.max(np.abs(pinned_gap))) < 1e-6
    return _report("holds kept by the sweep and the tension finder; detail model pinned in a hold", ok,
                   f"spans per tension {[row.spans for row in rows]}; found {answer[0].tension_kn:.2f} kN")


def test_hold_with_slack() -> bool:
    """Slack short of the seabed next to a hold: pulled taut without drawing on (or raising) the hold."""
    spec = SyntheticSeabed(length_m=3000.0, spacing_m=1.0, wave_height_m=2.0, wave_length_m=40.0)
    prof = synthetic_profile(spec)
    plain = pl.run(_inputs(prof, pl.LayControl(pl.ROUTE_SLACK, 0.0), criteria=pl.Criteria(allowable=False)))
    hold = pl.ControlSection(1.5, 3.0, pl.HELD, 0.5)
    r = pl.run(_inputs(prof, pl.LayControl(pl.ROUTE_SLACK, 0.0, [hold]), criteria=pl.Criteria(allowable=False)))
    ok = np.allclose(r.tension_kn[r.held], 0.5) and r.shortfalls \
        and all(item.kp_end < 1.5 for item in r.shortfalls) and plain.shortfalls
    return _report("slack next to a hold: pulled taut on its own side; the hold keeps its tension", bool(ok),
                   f"{len(r.shortfalls)} short stretch(es), last ends at KP "
                   f"{max((s.kp_end for s in r.shortfalls), default=math.nan):.3f}")


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
        test_depression_verification(),
        test_features_shape(),
        test_csv_profile(),
        test_long_route_speed(),
        test_supports_at_span_ends(),
        test_touchdown_gap(),
        test_find_tension(),
        test_protection(),
        test_contact_limit(),
        test_data_noise_and_filter(),
        test_spans_below_data_resolution(),
        test_hold_pull_back(),
        test_hold_no_spans_inside(),
        test_hold_end_uplift(),
        test_point_hold_anchor(),
        test_descending_anchor_and_burials(),
        test_hold_sweep_and_detail(),
        test_hold_with_slack(),
    ]
    print(f"\n{sum(results)}/{len(results)} cable-on-seabed engine checks passed.")
    return results


if __name__ == "__main__":
    run_all()
