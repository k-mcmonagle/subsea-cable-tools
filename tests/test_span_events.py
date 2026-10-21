# -*- coding: utf-8 -*-
"""Span & Slack Analysis: bottom slack, live friction, events on the profile and the cable colour.

Synthetic data only. The engine checks are closed-form style (a cable
lying on the seabed has 0% bottom slack; a span is shorter than the seabed
under it); the event checks build RPL, comment and burial rows in memory.
"""

from __future__ import annotations

import math
import time
from types import SimpleNamespace
from typing import List

import numpy as np
from qgis.core import QgsApplication  # noqa: F401  (the runner boots QGIS)
from qgis.PyQt.QtCore import QCoreApplication

from ..cable_seabed import planning as pl
from ..cable_seabed.core import INFO
from ..cable_seabed.features import SeabedFeature
from ..span_analysis import cable_colour as cc
from ..span_analysis import events as ev

REQUIRES_QGIS = True

LW = pl.CableType("LW", w_npm=10.0, ei_knm2=1.0, mbr_m=1.5, diameter_m=0.02, npts_kn=20.0)
DA = pl.CableType("DA", w_npm=60.0, ei_knm2=20.0, mbr_m=2.0, diameter_m=0.05, npts_kn=60.0)


def _report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""))
    return bool(ok)


def _wait(condition, timeout=90.0) -> bool:
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            return False
        QCoreApplication.processEvents()
        time.sleep(0.01)
    QCoreApplication.processEvents()
    return True


def _depression(mode=pl.TENSION, value=2.0, mu=0.5):
    x = np.arange(0.0, 1000.0 + 1e-9, 0.25)
    profile = pl.Profile(x, x / 1000.0, np.full(len(x), 100.0))
    feature = SeabedFeature("depression", 0.5, 2.0, 40.0, 38.0, name="Depression")
    return pl.PlanningInputs(profile, LW, features=[feature], control=pl.LayControl(mode, value),
                             criteria=pl.Criteria(allowable=False), filter_m=0.0,
                             friction=pl.FrictionModel(mu, mu))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def test_bottom_slack() -> List[bool]:
    out = []
    r = pl.run(_depression())
    span = r.spans[0]
    i = (span.i0 + span.i1) // 2
    out.append(_report("a span reads below 0 as bottom slack, while over the route it reads above 0",
                       r.bottom_slack_pct[i] < -1.0 and r.laid_pct[i] > 0.0 and r.used_bottom_pct[i] < -1.0,
                       f"bottom {r.bottom_slack_pct[i]:+.2f}%, route {r.laid_pct[i]:+.3f}%"))
    sl = slice(span.i0, span.i1 + 1)
    xs = r.stations.x_m[sl]
    sea = np.sum(np.hypot(np.diff(xs), np.diff(r.stations.obstacle_m[sl])))
    cab = np.sum(np.hypot(np.diff(xs), np.diff(r.cable_m[sl])))
    out.append(_report("the span's shortfall is the seabed length less the cable length under it",
                       abs(r.span_short_m[0] - (sea - cab)) < 1e-9 and r.span_short_m[0] > 1.0,
                       f"{r.span_short_m[0]:.3f} m"))
    far = int(np.argmin(np.abs(r.stations.kp_km - 0.1)))
    out.append(_report("away from the trough the cable lies on the seabed: 0% bottom slack",
                       abs(r.bottom_slack_pct[far]) < 1e-6 and abs(r.used_bottom_pct[far]) < 1e-6))
    ratio_ok = np.allclose((1.0 + r.fill_pct / 100.0), r.seabed_over_route, equal_nan=True)
    out.append(_report("seabed over route matches the fill", bool(ratio_ok)))
    slack = pl.run(_depression(pl.BOTTOM_SLACK, 0.5))
    k = int(np.argmin(np.abs(slack.stations.kp_km - 0.2)))
    out.append(_report("0.5% bottom slack reads 0.5% laid, about 0 used, 0.5% surplus on flat seabed",
                       abs(slack.bottom_slack_pct[k] - 0.5) < 1e-6 and abs(slack.used_bottom_pct[k]) < 1e-6
                       and abs(slack.surplus_bottom_pct[k] - 0.5) < 1e-6))
    out.append(_report("summary: bottom slack of the whole route",
                       abs(r.summary.bottom_slack_pct - (r.summary.cable_laid_m / r.summary.seabed_m - 1) * 100) < 1e-9))
    return out


def test_no_finite_tension() -> List[bool]:
    x = np.arange(0.0, 1000.0 + 1e-9, 0.25)
    depth = 100.0 + 1.0 * np.exp(-((x - 500.0) / 6.0) ** 2)
    r = pl.run(pl.PlanningInputs(pl.Profile(x, x / 1000.0, depth), DA,
                                 control=pl.LayControl(pl.ROUTE_SLACK, 0.0), criteria=pl.Criteria(allowable=False),
                                 filter_m=0.0))
    npts = [f for f in r.findings if f.check_id == "npts"]
    spans = [f for f in r.findings if f.check_id == "span"]
    ok = (pl.unbounded(r.summary.max_tension_kn) and npts and "No finite tension" in npts[0].message
          and npts[0].value == math.inf and spans and "no finite tension" in spans[0].message)
    return [_report("a cable laid shorter than the seabed: no finite tension, not the search limit", bool(ok),
                    npts[0].message[:80] if npts else "no NPTS finding")]


def test_with_friction() -> List[bool]:
    x = np.arange(0.0, 2000.0, 0.5)
    depth = 100.0 + x * math.tan(math.radians(8.0)) + 0.5 * np.sin(2 * math.pi * x / 60.0)
    profile = pl.Profile(x, x / 1000.0, depth)
    zones = [pl.SoilZone(0.5, 1.0, "Clay")]
    table = [pl.FrictionEntry("*", "Clay", 0.2, 0.3)]
    base = pl.PlanningInputs(profile, LW, control=pl.LayControl(pl.TENSION, 1.0), filter_m=0.0,
                             criteria=pl.Criteria(allowable=False), friction=pl.FrictionModel(0.6, 0.6))
    changed = pl.FrictionModel(0.15, 0.4, zones, table)
    fresh = pl.prepare(pl.PlanningInputs(profile, LW, control=base.control, filter_m=0.0, criteria=base.criteria,
                                         friction=changed))
    rebuilt = pl.with_friction(pl.prepare(base), changed)
    same = (np.array_equal(rebuilt.mu, fresh.mu) and np.array_equal(rebuilt.mu_lateral, fresh.mu_lateral)
            and rebuilt.soil == fresh.soil)
    a = pl.solve(rebuilt, base.control, base.criteria)
    b = pl.solve(fresh, base.control, base.criteria)
    c = pl.solve(pl.prepare(base), base.control, base.criteria)
    return [_report("friction set on prepared stations equals preparing them with it", same and
                    np.allclose(a.tension_kn, b.tension_kn) and not np.allclose(a.tension_kn, c.tension_kn),
                    f"max T {np.nanmax(a.tension_kn):.2f} kN vs {np.nanmax(c.tension_kn):.2f} kN at μ 0.6")]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def _model_result():
    inputs = _depression()
    inputs.bodies = [pl.Body("R1", 0.3, 4.0, 4.0, 0.3, "Repeater")]
    inputs.features.append(SeabedFeature("pipe", 0.7, 0.5, 0.5, name="Pipeline"))
    return pl.run(inputs)


def _rpl(points):
    return SimpleNamespace(points=[SimpleNamespace(event=text, dist_cum_km=kp, pos_no=n + 1, attrs={"Remarks": rem})
                                   for n, (text, kp, rem) in enumerate(points)])


def test_rpl_events_and_checks() -> List[bool]:
    from ..workbench.assembly_model import EventClassifier

    out = []
    r = _model_result()
    model = _rpl([("SOL", 0.0, ""), ("RPT 1", 0.31, "Repeater 1"), ("JT 2", 0.6, ""),
                  ("XING TAT-14", 0.7, "In service"), ("XING old telegraph", 0.9, ""), ("A/C 3", 0.4, ""),
                  ("PLDN", 0.05, "")])
    measured = [p.dist_cum_km for p in model.points]
    rpl = ev.rpl_events(model, measured, EventClassifier.with_defaults().classify)
    kinds = {e.label: (e.group, e.kind) for e in rpl}
    out.append(_report("RPL events sorted by the Workbench rules",
                       kinds["RPT 1"] == (ev.GROUP_BODIES, "repeater") and kinds["JT 2"][0] == ev.GROUP_BODIES
                       and kinds["XING TAT-14"] == (ev.GROUP_CROSSINGS, "crossing")
                       and kinds["A/C 3"][0] == ev.GROUP_ALTER and kinds["PLDN"][0] == ev.GROUP_OPERATIONS,
                       str(kinds)))
    events = ev.match_model(ev.model_events(r) + rpl)
    by = {e.label: e for e in events if e.source == ev.SRC_RPL}
    out.append(_report("an RPL repeater 10 m from the modelled body is that body; a joint with none is not",
                       by["RPT 1"].modelled is True and by["JT 2"].modelled is False
                       and "In the model as 'R1'" in by["RPT 1"].detail))
    out.append(_report("a crossing at the modelled pipeline is modelled; one with no feature is not",
                       by["XING TAT-14"].modelled is True and by["XING old telegraph"].modelled is False))
    out.append(_report("matching works on copies (no detail piles up when it runs again)",
                       "In the model" not in rpl[1].detail
                       and ev.match_model(ev.model_events(r) + rpl)[len(r.bodies) + 3].detail.count("In the model")
                       <= 1))
    checks = ev.check_findings(events, 0.0, 1.0)
    texts = [f.message for f in checks]
    out.append(_report("checks: the unmodelled joint and crossing are information findings",
                       len(checks) == 2 and all(f.severity == INFO and f.check_id == "event_check" for f in checks)
                       and any("'JT 2'" in t for t in texts) and any("old telegraph" in t for t in texts),
                       " | ".join(t[:60] for t in texts)))
    shown, outside = ev.visible_events(events, {k: True for k, *_r in ev.SOURCES},
                                       {k: on for k, _t, on in ev.GROUPS}, 0.0, 0.8)
    labels = [e.label for e in shown]
    out.append(_report("shown: the modelled repeater once (as the body), alter courses off by default, KPs "
                       "outside counted",
                       "R1" in labels and "RPT 1" not in labels and "A/C 3" not in labels and outside == 1,
                       f"{labels}, {outside} outside"))
    return out


def test_comments_and_burial() -> List[bool]:
    from ..burial import schema as bs

    out = []
    route = ev.RouteIdentity(rpl_id="R1", start_kp_km=0.0, convention="geodesic:WGS84", length_m=1000.0,
                             kp_mode="ellipsoidal")
    base = {"rpl_id": "R1", "start_kp_km": 0.0, "kp_convention": "geodesic:WGS84", "route_length_m": 1000.2,
            "ref_kind": "route_point", "status": "Open", "text": "Boulder field seen on the ROV video"}
    rows = [dict(base, comment_id="a", ref_no=1, kp_km=0.25),
            dict(base, comment_id="b", ref_no=2, kp_km=0.4, kp_end_km=0.6, ref_kind="kp_range"),
            dict(base, comment_id="c", ref_no=3, kp_km=0.3, status="Closed"),
            dict(base, comment_id="d", ref_no=4, kp_km=0.3, start_kp_km=0.1),
            dict(base, comment_id="e", ref_no=5, kp_km=0.3, route_length_m=1012.0),
            dict(base, comment_id="f", ref_no=6, kp_km=0.3, rpl_id="R2")]
    found, notes = ev.comment_events(rows, route, ["closed"], False)
    keys = sorted(e.key for e in found)
    out.append(_report("comments: same route and KP basis shown, a range kept, the rest left out with reasons",
                       keys == ["comment:a", "comment:b"] and any(e.is_range for e in found)
                       and any("start KP" in n for n in notes) and any("edited" in n for n in notes)
                       and any("closed" in n for n in notes), f"{keys}; {notes}"))
    found, _notes = ev.comment_events(rows, route, ["closed"], True)
    out.append(_report("closed comments show greyed when asked", any(e.key == "comment:c" and e.muted
                                                                       for e in found)))
    plan = {"plan_id": "P", "name": "Plan A", "method": bs.METHOD_PLOUGH, "rpl_fingerprint": "R1|t1|lines|x",
            "_datum": {"start_kp": 0.0, "mode": "ellipsoidal"}}
    events = [{"event_id": "e1", "event_type": bs.EVENT_BURIAL_START, "kp": 0.1, "status": "confirmed"},
              {"event_id": "e2", "event_type": bs.EVENT_BURIAL_END, "kp": 0.45, "status": "candidate"}]
    sections = [{"section_id": "s1", "kind": bs.SECTION_BURIAL, "start_kp": 0.1, "end_kp": 0.45,
                 "start_event_id": "e1", "end_event_id": "e2"},
                {"section_id": "s2", "kind": bs.SECTION_SKIP, "start_kp": 0.45, "end_kp": 0.8}]
    hazards = [{"hazard_id": "h1", "label": "TAT-14", "kp": 0.7, "end_kp": 0.7, "crossing": 1, "risk": "high"}]
    found, notes = ev.burial_events(plan, events, sections, hazards, route, "R1|t1|lines|y")
    labels = sorted(e.label for e in found)
    out.append(_report("burial plan on the same basis: PLDN / PLUP, sections as ranges, a crossing hazard",
                       "PLDN" in labels and "PLUP" in labels and sum(e.is_range for e in found) == 2
                       and any(e.group == ev.GROUP_CROSSINGS for e in found) and not notes, str(labels)))
    _f, moved = ev.burial_events(plan, events, sections, hazards, route, "R1|t2|lines|y")
    _f2, other = ev.burial_events(dict(plan, _datum={"start_kp": 5.0, "mode": "ellipsoidal"}), events, sections,
                                  hazards, route, "R1|t1|lines|y")
    out.append(_report("a plan made before an RPL edit, or on another start KP, is left out with the reason",
                       not _f and not _f2 and "edited" in moved[0] and "start KP" in other[0]))
    return out


def test_label_layout() -> List[bool]:
    items = [(100.0, 60.0, 10), (110.0, 60.0, 20), (120.0, 60.0, 30), (500.0, 80.0, 10), (5.0, 50.0, 10)]
    slots = ev.layout_labels(items, 600.0, 2)
    placed = [s for s in slots if s.tier >= 0]
    clash = any(a.tier == b.tier and a.left < b.right and b.left < a.right
                for i, a in enumerate(placed) for b in placed[i + 1:])
    inside = all(0.0 <= s.left and s.right <= 600.0 for s in placed)
    rows = ev.range_rows([(0.0, 1.0), (0.5, 2.0), (1.5, 3.0), (2.5, 4.0)], 2)
    return [_report("labels: no overlaps, inside the lane, the first priority on the first row, the rest wait",
                    not clash and inside and slots[0].tier == 0 and slots[2].tier == -1 and slots[4].left == 0.0,
                    str([(s.tier, round(s.left)) for s in slots])),
            _report("KP ranges stack on rows without overlapping", rows == [0, 1, 0, 1], str(rows))]


# ---------------------------------------------------------------------------
# Cable colour
# ---------------------------------------------------------------------------
def test_cable_colour() -> List[bool]:
    out = []
    r = pl.run(_depression(pl.BOTTOM_SLACK, 9.0))
    classes, legend, note = cc.cable_classes(r, cc.SLACK_STATE)
    flat = int(np.argmin(np.abs(r.stations.kp_km - 0.2)))
    out.append(_report("slack state: 9% bottom slack at zero tension is loop risk (the 8% check)",
                       "Loop risk" in legend[classes[flat]][1] and "loop-risk" in note, legend[classes[flat]][1]))
    t = pl.run(_depression())
    classes, legend, _note = cc.cable_classes(t, cc.TENSION)
    i = int(np.argmin(np.abs(t.stations.kp_km - 0.5)))
    out.append(_report("tension: 2 kN of a 20 kN NPTS is under 25%", legend[classes[i]][1] == "Under 25% of NPTS"))
    classes, legend, _note = cc.cable_classes(t, cc.SOURCE)
    out.append(_report("what sets the tension: laid at the bottom tension", legend[classes[i]][1].startswith("Laid")))
    runs = cc.class_runs(np.array([0, 0, 1, 1, 1, -1, -1, 0, 0]))
    out.append(_report("class runs by segment, none where unclassed", runs == [(0, 1, 0), (2, 4, 1), (7, 7, 0)],
                       str(runs)))
    kp = np.linspace(0.0, 1.0, 10001)
    depth = 100.0 + np.where(np.abs(kp - 0.5) < 1e-4, 3.0, 0.0)       # one deep station
    depth[7000:7010] = np.nan
    paths = cc.class_paths(kp, depth, [(0, 9999, 0)], 0.0, 1.0, 200)
    xs, ys, connect = paths[0]
    out.append(_report("colour paths: thinned to the pixels, the peak kept, broken at a data gap",
                       len(xs) < 2000 and np.nanmax(ys) == 103.0 and not connect.all() and np.isfinite(ys).all(),
                       f"{len(xs)} points"))
    return out


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------
def test_window() -> List[bool]:
    from .test_span_analysis_qgis import _example, _make_dialog

    out = []
    dialog = _make_dialog()
    try:
        dialog.resize(1500, 950)
        dialog.show()
        dialog.load_example(_example("Sand waves"))
        _wait(lambda: dialog._task is None, 120.0)
        plot, setup, r = dialog.plot, dialog.setup, dialog._result
        out.append(_report("Slack row over the seabed by default", plot.slack_basis() == "seabed"
                           and np.allclose(plot.laid_curve.yData, r.bottom_slack_pct, equal_nan=True)
                           and "Bottom slack, over the seabed" in plot.captions["slack"].text()))
        plot.slack_basis_actions["route"].setChecked(True)
        route_ok = np.allclose(plot.laid_curve.yData, r.laid_pct, equal_nan=True)
        plot.slack_basis_actions["seabed"].setChecked(True)
        out.append(_report("Slack row over the route on request", route_ok))
        out.append(_report("the shortfall under the spans is shaded", not plot.short_fill.path().isEmpty()))

        # Live friction: no new sampling, the stations take the friction.
        stations = dialog._stations
        before = r
        setup.mu.setValue(0.15)
        _wait(lambda: dialog._result is not before and dialog._task is None, 60.0)
        after = dialog._result
        changed = after is not before and float(np.nanmax(after.stations.mu)) == 0.15
        out.append(_report("a friction change updates the results live, on the same seabed",
                           changed and after.stations.x_m is stations.x_m and not dialog.stale.isVisible()))
        dialog.run_sweep({"tensions": [1.0, 3.0], "window": "all"})
        _wait(lambda: dialog._task is None, 60.0)
        fresh = not dialog.results.tools_stale.isVisible()
        setup.mu.setValue(0.4)
        _wait(lambda: not dialog._live_timer.isActive() and dialog._task is None, 60.0)
        out.append(_report("Find tension / sweep results say when the friction changed after them",
                           fresh and dialog.results.tools_stale.isVisible()))

        # Events: the repeater is in the model; a marker shows and saves with the scenario.
        shown = plot.shown_events()
        out.append(_report("events lane: the modelled repeater, the lane shown", any(e.label == "R1" for e in shown)
                           and plot.lane_widget.isVisible()))
        dialog._markers.append({"kp": 1.5, "label": "Rock outcrop", "note": "From the ROV"})
        dialog._markers_changed()
        out.append(_report("a marker shows on the lane and in the Events tab, and the results stay current",
                           any(e.label == "Rock outcrop" for e in plot.shown_events())
                           and dialog.results.events_table.rowCount() >= 2 and not dialog.stale.isVisible()))
        settings = dialog._scenario_settings()
        out.append(_report("markers save with the scenario", settings.get("markers", [{}])[0].get("label")
                           == "Rock outcrop"))
        plot.depth_plot.setXRange(1.45, 1.55, padding=0)
        QCoreApplication.processEvents()
        plot.lane_widget.grab()
        layout = plot.event_layer.layout()
        slot = next((s for e, s in zip(plot.event_layer.events, layout.get("slots", []))
                     if e.label == "Rock outcrop"), None)
        hit = []
        if slot is not None and slot.tier >= 0:
            y = plot.lane_plot.vb.sceneBoundingRect().top() + 2 + (plot.event_layer.tiers - slot.tier) * 15 - 6
            hit = plot.event_layer.events_at(0.5 * (slot.left + slot.right), y,
                                             plot.lane_plot.vb.sceneBoundingRect().top())
        out.append(_report("hovering the marker's label finds it", any(e.label == "Rock outcrop" for e in hit),
                           f"slot {slot}"))
        actions = [text for text, _slot, _on in plot._context_actions(1.5, None) if text]
        out.append(_report("right-click: add a marker here, edit or delete the one under it",
                           any(t.startswith("Add a marker") for t in actions)
                           and any(t.startswith("Edit marker") for t in actions), str(actions)))

        # Cable colour.
        plot.colour_actions[cc.SLACK_STATE].setChecked(True)
        plot.fit()
        QCoreApplication.processEvents()
        plot._draw_colour_paths()
        drawn = sum(not item.path().isEmpty() for item in plot.colour_paths)
        out.append(_report("colour the cable by slack state: paths drawn, the key in the legend",
                           drawn >= 1 and "Cable colour" in plot.captions["depth"].text()
                           and plot.colour_text(10) != ""))
        plot.colour_actions[cc.PLAIN].setChecked(True)
        out.append(_report("plain again: no colour paths", all(item.path().isEmpty() for item in plot.colour_paths)))
    finally:
        dialog.hide()
        dialog.shutdown()
        dialog.deleteLater()
    return out


def test_rpl_route_end_to_end() -> List[bool]:
    """A Workbench RPL with events and a comment register: the events and comments reach the lane at the
    analysis's KPs, and a comment made from the profile comes back at the KP it was made at."""
    import os
    import sqlite3
    import tempfile

    from qgis.core import QgsProject

    from ..processing.cable_lay_parsers import WKT_KEY
    from ..qgis_compat import WKB_LINESTRING, WKB_POINT
    from ..route_comments import model as cm
    from ..route_comments.map_layers import set_project_register
    from ..route_comments.store import CommentStore, close_connection
    from ..span_analysis.setup_panel import CABLE_MANUAL
    from ..workbench import schema
    from ..workbench.store import WorkbenchStore, set_project_gpkg_path
    from .test_span_analysis_qgis import _make_dialog

    out = []
    folder = tempfile.mkdtemp()
    store = WorkbenchStore(os.path.join(folder, "wb.gpkg"))
    store.ensure_created()
    rpl_id = schema.new_id()
    points, lines = schema.rpl_points_layer_name("Route E"), schema.rpl_lines_layer_name("Route E")
    lons = [0.0, 0.004, 0.008, 0.012, 0.016]
    events = ["BMH", "RPT 1", "XING TAT-14", "JOINT 2", "PLUP"]
    store.write_spatial_layer(points, schema.RPL_POINT_FIELDS, WKB_POINT, [
        {"rpl_id": rpl_id, "SeqNo": i, "PosNo": i + 1, "Event": text, "DistCumulative": lon * 111.32,
         "ApproxDepth": 50.0 + 5.0 * i, "Remarks": "test", "Latitude": 0.0, "Longitude": lon,
         WKT_KEY: f"POINT ({lon} 0)"} for i, (lon, text) in enumerate(zip(lons, events))])
    store.write_spatial_layer(lines, schema.RPL_LINE_FIELDS, WKB_LINESTRING, [
        {"rpl_id": rpl_id, "SeqNo": i, "FromPos": i + 1, "ToPos": i + 2, "Slack": 2.0, "CableType": "LW",
         WKT_KEY: f"LINESTRING ({a} 0, {b} 0)"} for i, (a, b) in enumerate(zip(lons[:-1], lons[1:]))])
    store.save_rpl({"rpl_id": rpl_id, "name": "Route E Rev 1", "kind": "rpl", "points_layer": points,
                    "lines_layer": lines})
    register = os.path.join(folder, "comments.gpkg")
    comments = CommentStore.create(register)
    set_id = comments.list_sets()[0]["set_id"]
    project = QgsProject.instance()
    set_project_gpkg_path(store.gpkg_path, project)
    set_project_register(register, set_id, project)
    dialog = _make_dialog()
    calls = []
    try:
        dialog.setup.refresh_sources()
        dialog.setup.apply_settings({
            "route": {"source": "rpl", "rpl_id": rpl_id, "start_kp": 0.0, "start_kp_set": True},
            "seabed": {"source": "rpl_depth", "filter_m": 0.0},
            "cable": {"source": CABLE_MANUAL, "manual": {"w_kg_m": 1.0, "ei_knm2": 1.0, "mbr_m": 1.5,
                                                         "diameter_mm": 20.0, "npts_kn": 20.0}, "bodies": []},
            "control": {"mode": pl.TENSION, "value": 2.0, "direction": 1, "sections": [], "live": True},
            "criteria": {"allowable": False}})
        dialog.comment_hook = lambda kind, capture, data: calls.append((kind, capture, data))
        dialog.run()
        _wait(lambda: dialog._task is None, 120.0)
        ident = dialog._route_identity
        out.append(_report("RPL route: its identity for the other tools (RPL, start KP, KP convention)",
                           ident is not None and ident.rpl_id == rpl_id and ident.start_kp_km == 0.0
                           and ident.convention.startswith("geodesic") and dialog._route_layer is not None,
                           f"{ident}"))
        labels = {e.label: e for e in dialog.plot.shown_events()}
        checks = [f for f in dialog._result.findings if f.check_id == "event_check"]
        out.append(_report("its events on the lane at measured KPs; unmodelled bodies and crossing checked",
                           {"BMH", "RPT 1", "XING TAT-14", "JOINT 2", "PLUP"} <= set(labels)
                           and abs(labels["PLUP"].kp - dialog._result.stations.kp_km[-1]) < 0.002
                           and len(checks) == 3, f"{sorted(labels)}; {len(checks)} check(s)"))
        # A comment from the profile: the KP Mouse Tool's route model on the RPL's lines layer.
        out.append(_report("Route Comments can take a comment on this route", dialog._can_comment()))
        dialog.add_comment(0.7, None)
        kind, capture, data = calls[-1] if calls else (None, None, None)
        values = capture.point_values(data["kp_km"]) if capture is not None else {}
        out.append(_report("a comment from the profile is captured on the RPL at the KP clicked",
                           kind == cm.REF_ROUTE_POINT and values.get("rpl_id") == rpl_id
                           and abs(values.get("kp_km", -1) - 0.7) < 1e-9, f"{kind} {values.get('kp_km')}"))
        values.update(text="Check the burial here", status="Open")
        comments.add_comment(set_id, values)
        dialog._refresh_events()
        back = [e for e in dialog.plot.shown_events() if e.source == ev.SRC_COMMENT]
        out.append(_report("... and comes back on the lane at that KP (same start KP, convention and length)",
                           len(back) == 1 and abs(back[0].kp - 0.7) < 1e-9 and "#1" in back[0].label,
                           f"{[(e.label, e.kp) for e in back]}"))
        # A burial file read as it is (no schema change, no journal change).
        burial = os.path.join(folder, "burial.gpkg")
        conn = sqlite3.connect(burial)
        conn.execute("CREATE TABLE bp_plan (plan_id TEXT, name TEXT, method TEXT, rpl_id TEXT, params_json TEXT, "
                     "status TEXT, modified_utc TEXT, rpl_fingerprint TEXT)")
        conn.execute("CREATE TABLE bp_event (event_id TEXT, plan_id TEXT, event_type TEXT, kp REAL, status TEXT, "
                     "notes TEXT)")
        conn.execute("INSERT INTO bp_plan VALUES ('p1', 'Old', 'plough', ?, '{}', 'draft', '2026-01-01', '')",
                     (rpl_id,))
        conn.execute("INSERT INTO bp_plan VALUES ('p2', 'Issued', 'plough', ?, "
                     "'{\"kp_datum\": {\"start_kp\": 0.0, \"mode\": \"ellipsoidal\"}}', 'issued', '2025-01-01', '')",
                     (rpl_id,))
        conn.execute("INSERT INTO bp_event VALUES ('e1', 'p2', 'BURIAL_START', 0.2, 'confirmed', '')")
        conn.commit()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        data = ev.read_burial(burial, rpl_id)
        plan = ev.choose_plan(data["plans"])
        conn = sqlite3.connect(burial)
        mode_after = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        found, _notes = ev.burial_events(plan, data["events"]["p2"], [], [], ident)
        out.append(_report("burial file: the issued plan for the RPL, its PLDN, the file left as it was",
                           plan["plan_id"] == "p2" and [e.label for e in found] == ["PLDN"] and mode == mode_after))
    finally:
        dialog.shutdown()
        dialog.deleteLater()
        close_connection(register)
        project.clear()
    return out


def run_all() -> List[bool]:
    results = (test_bottom_slack() + test_no_finite_tension() + test_with_friction() + test_rpl_events_and_checks()
               + test_comments_and_burial() + test_label_layout() + test_cable_colour() + test_window()
               + test_rpl_route_end_to_end())
    print(f"\n{sum(results)}/{len(results)} Span & Slack events, slack and colour checks passed.")
    return results
