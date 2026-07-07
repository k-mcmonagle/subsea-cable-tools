# -*- coding: utf-8 -*-
"""Checks for assembly modelling and assembly -> route fitting.

Covers the catenary JSON round-trip, event classification defaults,
extract-from-RPL section grouping, and fit_assembly body landings against
hand-computed positions (including slack handling, reverse direction, and
over-run warnings).

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import json

from qgis.core import QgsCoordinateReferenceSystem, QgsProject

from ..kp_range_utils import make_distance_area
from ..workbench import rpl_engine as eng
from ..workbench import assembly_model as am
from ..workbench.fit import FitAnchor, fit_assembly
from ..workbench.rpl_engine import RplModel, RplPoint, RplSegment, SlackMode


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _da():
    return make_distance_area(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsProject.instance().transformContext(),
    )


def _route(n_points: int = 11, slack_pct: float = 0.0) -> RplModel:
    points = [
        RplPoint(seq=i, pos_no=i + 1, event="", lat=50.0 + 0.01 * i, lon=0.0)
        for i in range(n_points)
    ]
    segments = [RplSegment(seq=i, slack_pct=slack_pct) for i in range(n_points - 1)]
    model = RplModel(points=points, segments=segments)
    eng.recompute(model, _da(), slack_mode=SlackMode.HOLD_SLACK)
    return model


def test_catenary_json_round_trip() -> bool:
    assembly = am.Assembly(name="RT", items=[
        am.AssemblyItem(kind="section", name="LW", length_m=1000.0, q_water_npm=22.0,
                        q_air_npm=28.0, diameter_m=0.035, cd_normal=1.2, color_hex="#1f77b4"),
        am.AssemblyItem(kind="body", name="Joint 1", point_load_kN=2.5),
        am.AssemblyItem(kind="section", name="DA", length_m=2000.0, q_water_npm=30.0,
                        q_air_npm=40.0, friction_mu=0.3),
    ])
    raw = am.to_catenary_json(assembly)
    data = json.loads(raw)
    ok = [d["type"] for d in data] == ["segment", "body", "segment"]
    ok = ok and data[0]["diameter_m"] == 0.035 and data[0]["color"] == "#1f77b4"

    parsed = am.from_catenary_json(raw, name="back")
    ok = ok and len(parsed.items) == 3
    ok = ok and parsed.items[0].q_water_npm == 22.0
    ok = ok and parsed.items[1].kind == "body" and parsed.items[1].point_load_kN == 2.5
    ok = ok and parsed.total_length_m() == 3000.0
    return _result("catenary JSON round trip", ok)


def test_event_classifier_defaults() -> bool:
    classifier = am.EventClassifier.with_defaults()
    checks = [
        ("Branching Unit BU-1", "body", "bu"),
        ("Repeater R3", "body", "repeater"),
        ("Joint JT-2", "body", "joint"),
        ("Pipeline Crossing", "geographic", ""),
        ("Start of Burial", "installation", ""),
        ("PLDN", "installation", ""),
    ]
    ok = True
    for text, category, body_type in checks:
        cls = classifier.classify(text)
        ok = ok and cls.matched and cls.category == category and cls.body_type == body_type

    # real-world RPL vocabulary
    real_world = [
        ("AC9", "geographic"),            # alter-course points
        ("WD 1000", "geographic"),        # water depth marks
        ("Tr DAS/SA", "installation"),    # cable type transition
        ("S014BUJB001", "body"),          # BU joint box id
        ("BU 2(S000B002);LWS", "body"),
        ("RBP2", "geographic"),
    ]
    for text, category in real_world:
        cls = classifier.classify(text)
        ok = ok and cls.matched and cls.category == category

    # unmatched events default to installation, flagged unmatched — never a body
    cls = classifier.classify("Mystery Event XYZ")
    ok = ok and not cls.matched and cls.category == "installation"
    return _result("event classifier defaults + safe fallback", ok)


def test_extract_from_rpl() -> bool:
    model = _route(7, slack_pct=1.0)
    # segments 0-2 type LW, 3-5 type DA; joint event at point 3, crossing at point 1
    for i, seg in enumerate(model.segments):
        seg.attrs["CableType"] = "LW" if i < 3 else "DA"
    model.points[1].event = "Cable Crossing"
    model.points[3].event = "Joint JT-1"

    assembly, review = am.extract_from_rpl(model, am.EventClassifier.with_defaults(), name="X")
    kinds = [(i.kind, i.name) for i in assembly.items]
    # expected: LW section, joint body (at point 3, which splits nothing since
    # the type also changes there), DA section
    ok = len(assembly.items) == 3
    ok = ok and assembly.items[0].kind == "section" and assembly.items[0].cable_type == "LW"
    ok = ok and assembly.items[1].kind == "body" and "JT-1" in assembly.items[1].name
    ok = ok and assembly.items[2].kind == "section" and assembly.items[2].cable_type == "DA"
    # section lengths = summed cable distance (with 1% slack)
    lw_km = sum(s.cable_dist_km for s in model.segments[:3])
    ok = ok and abs(assembly.items[0].length_m - lw_km * 1000.0) < 1e-6
    # review contains both events, crossing not a body
    ok = ok and len(review) == 2
    crossing = next(r for r in review if "Crossing" in r["event"])
    ok = ok and crossing["category"] == "geographic"
    return _result("extract_from_rpl grouping + review", ok, f"items={kinds}")


def test_build_assembly_overrides_and_grouping() -> bool:
    model = _route(7, slack_pct=0.0)
    # cable type changes at segment 3, but the user says only bodies matter
    for i, seg in enumerate(model.segments):
        seg.attrs["CableType"] = "LW" if i < 3 else "DA"
    model.points[2].event = "MB"           # unmatched -> installation by default
    model.points[4].event = "AC1"          # geographic by default

    classifier = am.EventClassifier.with_defaults()
    review = am.classify_events(model, classifier)
    classifications = {e["seq"]: e["category"] for e in review}
    ok = classifications[2] == "installation" and classifications[4] == "geographic"

    # user overrides: MB is actually a body
    classifications[2] = "body"

    # cable-type grouping: LW(2 segs) | body | LW(1) then DA(3) => 3 sections + 1 body
    built = am.build_assembly_from_rpl(model, classifications, name="G1",
                                       grouping=am.GROUP_BY_CABLE_TYPE)
    kinds = [i.kind for i in built.items]
    ok = ok and kinds == ["section", "body", "section", "section"]

    # between-bodies grouping: one section before MB, one after => 2 sections + 1 body
    built2 = am.build_assembly_from_rpl(model, classifications, name="G2",
                                        grouping=am.GROUP_BETWEEN_BODIES)
    kinds2 = [i.kind for i in built2.items]
    ok = ok and kinds2 == ["section", "body", "section"]
    # dominant cable type names the merged run (4 of 4 remaining segs: 1 LW + 3 DA -> DA)
    ok = ok and built2.items[2].cable_type == "DA"
    # total cable length preserved under both groupings
    total = sum(s.cable_dist_km for s in model.segments) * 1000.0
    ok = ok and abs(built.total_length_m() - total) < 1e-6
    ok = ok and abs(built2.total_length_m() - total) < 1e-6
    return _result("build_assembly overrides + grouping modes", ok,
                   f"kinds={kinds} / {kinds2}")


def test_fit_assembly_basic() -> bool:
    da = _da()
    model = _route(11, slack_pct=0.0)
    seg_km = model.segments[0].dist_km  # ~1.112 km
    assembly = am.Assembly(name="F", items=[
        am.AssemblyItem(kind="section", name="S1", length_m=seg_km * 1000.0),
        am.AssemblyItem(kind="body", name="Joint 1"),
        am.AssemblyItem(kind="section", name="S2", length_m=seg_km * 1000.0),
    ])
    result = fit_assembly(assembly, model, FitAnchor(kp_km=0.0), da=da)
    ok = not result.warnings and len(result.bodies) == 1
    body = result.bodies[0]
    # zero slack: joint at cable dist = 1 segment => KP = seg_km => point 2's position
    ok = ok and body.on_route and abs(body.kp_km - seg_km) < 1e-9
    ok = ok and abs(body.lat - 50.01) < 1e-6
    ok = ok and len(result.sections) == 2
    ok = ok and abs(result.sections[1].kp_end_km - 2 * seg_km) < 1e-9
    return _result("fit_assembly zero slack landing", ok,
                   f"joint at KP {body.kp_km:.4f}, lat {body.lat:.5f}")


def test_fit_assembly_with_slack() -> bool:
    da = _da()
    model = _route(11, slack_pct=2.0)
    seg_km = model.segments[0].dist_km
    # one cable segment's worth of cable INCLUDING slack lands exactly at point 2
    cable_len_m = seg_km * 1.02 * 1000.0
    assembly = am.Assembly(name="F", items=[
        am.AssemblyItem(kind="section", name="S1", length_m=cable_len_m),
        am.AssemblyItem(kind="body", name="Joint 1"),
    ])
    result = fit_assembly(assembly, model, FitAnchor(kp_km=0.0), da=da)
    body = result.bodies[0]
    ok = body.on_route and abs(body.kp_km - seg_km) < 1e-9
    ok = ok and abs(body.lat - 50.01) < 1e-6
    return _result("fit_assembly 2% slack shortens ground run", ok,
                   f"joint at KP {body.kp_km:.4f}")


def test_fit_assembly_reverse_and_overrun() -> bool:
    da = _da()
    model = _route(11, slack_pct=0.0)
    seg_km = model.segments[0].dist_km
    end_kp = model.points[-1].dist_cum_km

    # reverse: anchored at route end, running back
    assembly = am.Assembly(name="R", items=[
        am.AssemblyItem(kind="section", name="S1", length_m=seg_km * 1000.0),
        am.AssemblyItem(kind="body", name="J"),
    ])
    result = fit_assembly(assembly, model, FitAnchor(kp_km=end_kp, direction=-1), da=da)
    body = result.bodies[0]
    ok = body.on_route and abs(body.kp_km - (end_kp - seg_km)) < 1e-9

    # over-run: assembly longer than the route
    big = am.Assembly(name="B", items=[
        am.AssemblyItem(kind="section", name="S", length_m=(end_kp + 5.0) * 1000.0),
        am.AssemblyItem(kind="body", name="OffEnd"),
    ])
    result2 = fit_assembly(big, model, FitAnchor(kp_km=0.0), da=da)
    ok = ok and result2.warnings
    ok = ok and not result2.bodies[0].on_route
    ok = ok and result2.sections[0].clipped
    return _result("fit_assembly reverse direction + over-run warnings", ok)


def test_extract_then_fit_round_trip() -> bool:
    """An assembly extracted from an RPL must fit back onto it exactly —
    bodies at the very ends of the route included (boundary tolerance)."""
    da = _da()
    model = _route(9, slack_pct=1.5)
    for i, seg in enumerate(model.segments):
        seg.attrs["CableType"] = "LW" if i < 4 else "DA"
    model.points[0].event = "BMH East"       # body at route start
    model.points[4].event = "Joint JT-1"     # body mid-route
    model.points[-1].event = "BU 1"          # body at route end

    assembly, _review = am.extract_from_rpl(model, am.EventClassifier.with_defaults(), name="RT")
    result = fit_assembly(assembly, model, FitAnchor(kp_km=model.start_kp_km()), da=da)

    ok = len(result.bodies) == 3
    ok = ok and all(b.on_route for b in result.bodies)
    ok = ok and not result.warnings
    # the mid-route joint must land back on the point it came from
    joint = next(b for b in result.bodies if "JT-1" in b.item.name)
    ok = ok and abs(joint.kp_km - model.points[4].dist_cum_km) < 1e-6
    ok = ok and abs(joint.lat - model.points[4].lat) < 1e-7
    # the end body lands at the route end (clamped, not rejected)
    end_body = next(b for b in result.bodies if "BU 1" in b.item.name)
    ok = ok and abs(end_body.kp_km - model.end_kp_km()) < 1e-6
    return _result("extract -> fit round trip (bodies land, incl. route ends)", ok,
                   f"warnings={result.warnings}")


def run_all() -> list:
    return [
        test_catenary_json_round_trip(),
        test_event_classifier_defaults(),
        test_extract_from_rpl(),
        test_build_assembly_overrides_and_grouping(),
        test_fit_assembly_basic(),
        test_fit_assembly_with_slack(),
        test_fit_assembly_reverse_and_overrun(),
        test_extract_then_fit_round_trip(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)
