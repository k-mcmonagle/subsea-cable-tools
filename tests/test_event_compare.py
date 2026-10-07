# -*- coding: utf-8 -*-
"""Pure-Python checks for the RPL event comparison (no QGIS).

Covers name similarity (typos, extra text, different numbers), the ordered
event matching (anchors, gaps, extra/missing events, reversed RPLs), the
editable mapping and its saved corrections, the along-track / cross-course /
radial offsets against known geometry, filters, statistics, and the SVG/HTML
report output.

Run directly: ``python tests/run_pure_tests.py test_event_compare``.
"""

from __future__ import annotations

import math
import sys
import xml.etree.ElementTree as ET

from ..workbench import event_compare as ec
from ..workbench import event_compare_report as report

REQUIRES_QGIS = False

LAT0 = 50.0


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def _offset(lat, lon, east_m, north_m):
    m_lat, m_lon = ec._local_scale(lat)
    return lat + math.degrees(north_m / m_lat), lon + math.degrees(east_m / m_lon)


def _route(events, step_deg=0.05, count=21):
    """Points heading due east along LAT0; ``events`` maps index -> text."""
    points = []
    m_lat, m_lon = ec._local_scale(LAT0)
    km_per_step = math.radians(step_deg) * m_lon / 1000.0
    for i in range(count):
        points.append({"seq": i + 1, "pos": i + 1, "event": events.get(i, ""),
                       "kp": round(i * km_per_step, 6), "cable_kp": round(i * km_per_step * 1.02, 6),
                       "lat": LAT0, "lon": i * step_deg, "depth": 100.0 + i, "remarks": ""})
    return points


DESIGN_EVENTS = {0: "BMH A", 3: "RPTR 1", 5: "AC 1", 8: "RPTR 2", 11: "JT-3", 14: "RPTR 3",
                 16: "AC 2", 20: "BMH B"}


def _as_laid(shift=None, events=None):
    """As-laid copy of the design with some events displaced (east, north m)."""
    points = _route(events if events is not None else DESIGN_EVENTS)
    for index, (east, north) in (shift or {}).items():
        lat, lon = _offset(points[index]["lat"], points[index]["lon"], east, north)
        points[index].update(lat=lat, lon=lon)
    return points


def _mapping(points_a, points_b, **options):
    return ec.EventMapping.suggest(ec.extract_events(points_a), ec.extract_events(points_b),
                                   ec.MatchOptions(**options))


def _pairs_by_name(mapping):
    return {mapping.events_a[a].event: (mapping.events_b[b].event, how)
            for a, (b, how, _s) in mapping.pairs.items()}


# ------------------------------------------------------------------ tests --
def test_text_similarity() -> bool:
    sim = ec.text_similarity
    ok = (sim("JT-3", "jt 3") == 1.0
          and sim("BU1", "BU1 (as laid)") >= 0.85
          and sim("Repeater 12", "RPT 012") >= 0.8
          and sim("RPT 12", "RTP 12") >= ec.FUZZY_TEXT_THRESHOLD     # typo
          and sim("RPT 12", "RPT 13") < ec.FUZZY_TEXT_THRESHOLD      # different repeater
          and sim("AC 5", "AC 15") < ec.FUZZY_TEXT_THRESHOLD
          and sim("", "RPT 1") == 0.0)
    return _result("name similarity: typos and extra text match, different numbers do not", ok,
                   f"RPT12/RPT13={sim('RPT 12', 'RPT 13'):.2f} typo={sim('RPT 12', 'RTP 12'):.2f}")


def test_classification_of_events() -> bool:
    events = ec.extract_events(_route(DESIGN_EVENTS))
    by_name = {e.event: e for e in events}
    ok = (len(events) == len(DESIGN_EVENTS)
          and by_name["RPTR 1"].type_key == "repeater"
          and by_name["JT-3"].type_key == "joint"
          and by_name["AC 1"].type_key == "alter_course"
          and by_name["BMH A"].type_key == "bmh")
    return _result("events are classified with the project's event rules", ok)


def test_identical_rpls_match_exactly() -> bool:
    design = _route(DESIGN_EVENTS)
    mapping = _mapping(design, _as_laid())
    ok = (len(mapping.pairs) == len(DESIGN_EVENTS)
          and all(how == ec.HOW_EXACT for _b, how, _s in mapping.pairs.values())
          and not mapping.reversed)
    return _result("identical RPLs: every event pairs by exact name", ok)


def test_typos_extra_text_and_renames() -> bool:
    laid = dict(DESIGN_EVENTS)
    laid[3] = "RPTR 1 S/N 4471"      # extra info
    laid[8] = "RTPR 2"                 # typo
    laid[11] = "Joint 3 (final)"      # renamed + note
    laid[6] = "AC 1A"                 # extra event in the as-laid
    del laid[16]                      # AC 2 missing from the as-laid
    mapping = _mapping(_route(DESIGN_EVENTS), _as_laid(events=laid))
    pairs = _pairs_by_name(mapping)
    ok = (pairs.get("RPTR 1", ("",))[0] == "RPTR 1 S/N 4471"
          and pairs.get("RPTR 2", ("",))[0] == "RTPR 2"
          and pairs.get("JT-3", ("",))[0] == "Joint 3 (final)"
          and pairs.get("RPTR 1")[1] == ec.HOW_FUZZY
          and "AC 2" not in pairs
          and pairs.get("AC 1", ("",))[0] == "AC 1"
          and [mapping.events_b[i].event for i in mapping.unmatched_b()] == ["AC 1A"])
    return _result("typos, appended notes and renames pair; extra/missing events do not", ok,
                   str(pairs))


def test_different_numbers_never_pair_when_true_partner_exists() -> bool:
    laid = dict(DESIGN_EVENTS)
    laid[8], laid[14] = "RPTR 3", "RPTR 2"      # numbering swapped, positions kept
    mapping = _mapping(_route(DESIGN_EVENTS), _as_laid(events=laid), respect_order=False)
    pairs = _pairs_by_name(mapping)
    ok = pairs.get("RPTR 2", ("",))[0] == "RPTR 2" and pairs.get("RPTR 3", ("",))[0] == "RPTR 3"
    return _result("unordered mode follows names, not neighbours", ok, str(pairs))


def test_type_mismatch_never_pairs_by_position() -> bool:
    laid = dict(DESIGN_EVENTS)
    laid[8] = "JT 9"                       # a joint where the design has a repeater
    mapping = _mapping(_route(DESIGN_EVENTS), _as_laid(events=laid))
    pairs = _pairs_by_name(mapping)
    ok = "RPTR 2" not in pairs and "JT 9" in [mapping.events_b[i].event for i in mapping.unmatched_b()]
    return _result("a repeater never pairs with a joint at the same place", ok, str(pairs))


def test_position_match_for_same_type_nearby() -> bool:
    laid = dict(DESIGN_EVENTS)
    laid[8] = "Repeater B"                # no number, same type, same place
    mapping = _mapping(_route(DESIGN_EVENTS), _as_laid(events=laid))
    pairs = _pairs_by_name(mapping)
    ok = pairs.get("RPTR 2") == ("Repeater B", ec.HOW_POSITION)
    return _result("same type at the same place pairs as a 'position' match", ok, str(pairs))


def test_reversed_rpl() -> bool:
    design = _route(DESIGN_EVENTS)
    laid = list(reversed(_as_laid(shift={8: (15.0, 0.0)})))
    for i, row in enumerate(laid):
        row["seq"] = i + 1
        row["kp"] = design[-1]["kp"] - row["kp"]
    mapping = _mapping(design, laid)
    rows = ec.compute_offsets(mapping, design)
    rpt2 = next(r for r in rows if r.a is not None and r.a.event == "RPTR 2")
    ok = (mapping.reversed and len(mapping.pairs) == len(DESIGN_EVENTS)
          and rpt2.kp_delta_m is None and abs(rpt2.along_m - 15.0) < 0.05)
    return _result("an RPL recorded in the opposite direction is detected", ok,
                   f"reversed={mapping.reversed} along={rpt2.along_m}")


def test_offsets_against_known_geometry() -> bool:
    design = _route(DESIGN_EVENTS)
    # Heading east: ahead = east, starboard = south.
    laid = _as_laid(shift={3: (20.0, 30.0), 8: (-12.0, -45.0)})
    laid[3]["kp"] += 0.020
    laid[3]["depth"] += 3.0
    mapping = _mapping(design, laid)
    rows = {r.a.event: r for r in ec.compute_offsets(mapping, design) if r.a is not None}
    r1, r2 = rows["RPTR 1"], rows["RPTR 2"]
    ok = (abs(r1.along_m - 20.0) < 0.05 and abs(r1.cross_m + 30.0) < 0.05
          and abs(r1.radial_m - math.hypot(20, 30)) < 0.05
          and abs(r1.bearing_deg - math.degrees(math.atan2(20, 30))) < 0.1
          and abs(r1.east_m - 20.0) < 0.05 and abs(r1.north_m - 30.0) < 0.05
          and abs(r1.kp_delta_m - 20.0) < 1e-6 and abs(r1.depth_delta_m - 3.0) < 1e-9
          and abs(r2.along_m + 12.0) < 0.05 and abs(r2.cross_m - 45.0) < 0.05
          and rows["AC 1"].radial_m < 1e-6)
    return _result("along-track, cross-course, radial and bearing", ok,
                   f"RPT1 along={r1.along_m:.3f} cross={r1.cross_m:.3f} "
                   f"RPT2 along={r2.along_m:.3f} cross={r2.cross_m:.3f}")


def test_doubling_back_route_projects_on_the_right_pass() -> bool:
    # Out east along LAT0 then back west 150 m north: a point 20 m north of the
    # first pass must project onto the first pass, not the return leg.
    points = []
    for i in range(11):
        points.append({"seq": i, "event": "", "kp": None, "lat": LAT0, "lon": i * 0.01})
    north_lat = _offset(LAT0, 0.0, 0.0, 150.0)[0]
    for i in range(10, -1, -1):
        points.append({"seq": len(points), "event": "", "kp": None, "lat": north_lat, "lon": i * 0.01})
    points[5]["event"] = "RPTR 1"
    route = ec.RouteLine(points)
    a = ec.extract_events(points)[0]
    lat, lon = _offset(LAT0, 0.05, 0.0, 20.0)
    b = ec.EventInfo(index=0, event="RPTR 1", lat=lat, lon=lon)
    measured = ec.measure_pair(a, b, route)
    ok = abs(measured["cross_m"] + 20.0) < 0.05 and abs(measured["along_m"]) < 0.05
    return _result("projection searches near the event first (route doubling back)", ok,
                   str({k: measured[k] for k in ("along_m", "cross_m")}))


def test_manual_edits_and_saved_overrides() -> bool:
    design, laid = _route(DESIGN_EVENTS), _as_laid()
    mapping = _mapping(design, laid)
    names_b = [e.event for e in mapping.events_b]
    a_rpt1 = [e.event for e in mapping.events_a].index("RPTR 1")
    a_rpt2 = [e.event for e in mapping.events_a].index("RPTR 2")
    displaced = mapping.set_partner(a_rpt1, names_b.index("RPTR 2"))  # steal RPT 2
    ok = (displaced == a_rpt2 and mapping.partner(a_rpt2) is None
          and mapping.how(a_rpt1) == ec.HOW_MANUAL)
    a_ac2 = [e.event for e in mapping.events_a].index("AC 2")
    mapping.set_partner(a_ac2, None)
    saved = mapping.manual_overrides()
    fresh = _mapping(design, laid)
    applied = fresh.apply_overrides(saved)
    ok = ok and (applied == len(saved) and fresh.partner(a_rpt1) == names_b.index("RPTR 2")
                 and fresh.partner(a_rpt2) is None and fresh.partner(a_ac2) is None
                 and fresh.counts()["unmatched_a"] == 2)
    return _result("manual corrections displace, unmatch and survive a save/restore", ok,
                   f"saved={len(saved)} applied={applied}")


def test_filters_and_type_counts() -> bool:
    design = _route(DESIGN_EVENTS)
    rows = ec.compute_offsets(_mapping(design, _as_laid()), design)
    repeaters = ec.filter_rows(rows, ec.EventFilter.preset("repeaters"))
    no_ac = ec.filter_rows(rows, ec.EventFilter.preset("no_ac"))
    text = ec.filter_rows(rows, ec.EventFilter(text="rptr [12]"))
    counts = dict(ec.type_counts(rows))
    ok = ([r.a.event for r in repeaters] == ["RPTR 1", "RPTR 2", "RPTR 3"]
          and len(no_ac) == len(rows) - 2
          and [r.a.event for r in text] == ["RPTR 1", "RPTR 2"]
          and counts.get("repeater") == 3 and counts.get("alter_course") == 2)
    return _result("type presets, exclusions, text/regex filter and type counts", ok)


def test_statistics() -> bool:
    design = _route(DESIGN_EVENTS)
    laid = _as_laid(shift={3: (0.0, 30.0), 8: (40.0, 0.0), 14: (0.0, -10.0)})
    rows = ec.filter_rows(ec.compute_offsets(_mapping(design, laid), design),
                          ec.EventFilter.preset("repeaters"))
    stats = ec.offset_stats(rows, target_radius_m=25.0)
    ok = (stats.count == 3 and abs(stats.radial_max - 40.0) < 0.05
          and abs(stats.radial_mean - 80.0 / 3.0) < 0.05
          and abs(stats.radial_rms - math.sqrt((900 + 1600 + 100) / 3.0)) < 0.05
          and stats.within_target == 1 and stats.worst_event == "RPTR 2"
          and abs(stats.cross_mean - (-30.0 + 0.0 + 10.0) / 3.0) < 0.05
          and "3 matched" in ec.summary_text(stats))
    return _result("offset statistics and target count", ok, ec.summary_text(stats))


def test_csv_rows() -> bool:
    design = _route(DESIGN_EVENTS)
    laid = dict(DESIGN_EVENTS)
    laid[6] = "AC 1A"
    rows = ec.compute_offsets(_mapping(design, _as_laid(events=laid)), design)
    table = ec.csv_rows(rows, target_radius_m=10.0)
    only_b = [r for r in table if r[7] == "Only in B"]
    ok = (all(len(r) == len(ec.CSV_HEADER) for r in table)
          and len(only_b) == 1 and only_b[0][4] == "AC 1A"
          and table[0][-1] == "yes")
    return _result("CSV rows: one per event, unmatched B included in route order", ok)


def test_report_svg_and_html() -> bool:
    design = _route(DESIGN_EVENTS)
    laid = _as_laid(shift={3: (20.0, 30.0), 8: (-12.0, -45.0), 14: (5.0, 2.0)})
    laid_events = dict(DESIGN_EVENTS)
    laid_events[6] = "AC 1A"
    for i, text in laid_events.items():
        laid[i]["event"] = text
    rows = ec.compute_offsets(_mapping(design, laid), design)
    svgs = [report.radial_plot_svg(rows, target_radius_m=50, title="All <events>"),
            report.radial_plot_svg(rows[1:2], frame=report.FRAME_NORTH),
            report.kp_offset_chart_svg(rows, target_radius_m=50),
            report.kp_offset_chart_svg([])]
    ok = True
    for svg in svgs:
        try:
            ET.fromstring(svg)
        except ET.ParseError as exc:
            ok = False
            print("   SVG parse error:", exc)
    page = report.html_report(rows, "Design rev C", "As-laid", target_radius_m=50,
                              selection_text="Repeaters")
    ok = ok and (page.count("<svg") == 2 + sum(1 for r in rows if r.matched)
                 and "Design rev C" in page and "All selected events" in page
                 and "Only in B" in page and "&lt;events&gt;" in svgs[0])
    colours = report.colour_map(rows)
    ok = ok and sum(1 for c in colours.values() if c != report.OTHER_COLOUR) <= 3
    return _result("radial and KP charts are well-formed SVG; report embeds them", ok)


def test_nice_step() -> bool:
    ok = ([report.nice_step(v) for v in (0.7, 3, 7, 12, 45, 180)]
          == [1.0, 5.0, 10.0, 20.0, 50.0, 200.0])
    return _result("range-ring steps are round numbers", ok)


def run_all():
    return [
        test_text_similarity(),
        test_classification_of_events(),
        test_identical_rpls_match_exactly(),
        test_typos_extra_text_and_renames(),
        test_different_numbers_never_pair_when_true_partner_exists(),
        test_type_mismatch_never_pairs_by_position(),
        test_position_match_for_same_type_nearby(),
        test_reversed_rpl(),
        test_offsets_against_known_geometry(),
        test_doubling_back_route_projects_on_the_right_pass(),
        test_manual_edits_and_saved_overrides(),
        test_filters_and_type_counts(),
        test_statistics(),
        test_csv_rows(),
        test_report_svg_and_html(),
        test_nice_step(),
    ]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0 if all(run_all()) else 1)
