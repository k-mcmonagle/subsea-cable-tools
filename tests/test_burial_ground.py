# -*- coding: utf-8 -*-
"""Checks for the Burial Planner ground model + KP re-referencing (pure).

Covers unit normalisation/lookup/validation/coverage, the CSV header
guesser and interval/horizon converters, the CSV export, and the KpMap
builders (anchors, shift, geometry samples with a re-routed gap) plus
range mapping flags and unit re-referencing provenance.
"""

from __future__ import annotations

import os
import tempfile

from ..burial import ground_model as gm
from ..burial import kp_rereference as kr


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _unit(start, end, top, base, code, **extra):
    row = {"start_kp": start, "end_kp": end, "top_m": top, "base_m": base,
           "soil_class": code}
    row.update(extra)
    return row


def test_normalise_and_lookup() -> bool:
    swapped = gm.normalise_unit(_unit(5.0, 2.0, 0.0, 1.0, "SAND", top_end_m=0.5))
    ok = swapped["start_kp"] == 2.0 and swapped["end_kp"] == 5.0
    ok = ok and swapped["top_m"] == 0.5 and swapped["top_end_m"] == 0.0
    units = [_unit(0.0, 10.0, 0.0, 1.0, "SAND"),
             _unit(0.0, 10.0, 1.0, None, "CLAY"),
             _unit(10.0, 20.0, 0.0, 2.0, "ROCK", top_end_m=0.0, base_end_m=4.0)]
    ok = ok and gm.unit_at(units, 5.0, 0.5)["soil_class"] == "SAND"
    ok = ok and gm.unit_at(units, 5.0, 7.0)["soil_class"] == "CLAY"  # open base
    ok = ok and gm.unit_at(units, 15.0, 2.9)["soil_class"] == "ROCK"  # sloping base = 3
    ok = ok and gm.unit_at(units, 15.0, 3.1) is None
    ok = ok and gm.unit_at(units, 25.0, 0.5) is None
    top, base = gm.unit_depths_at(units[2], 15.0)
    ok = ok and abs(base - 3.0) < 1e-9 and top == 0.0
    return _result("normalise + unit_at + trapezoid depths", ok)


def test_validate_and_coverage() -> bool:
    units = [_unit(0.0, 10.0, 0.0, 1.0, "SAND"),
             _unit(5.0, 15.0, 0.5, 2.0, "CLAY"),          # overlaps SAND 0.5-1.0
             _unit(20.0, 20.0, 0.0, 1.0, "X"),            # zero length
             _unit(30.0, 40.0, 2.0, 1.0, "Y"),            # inverted
             {"start_kp": None, "end_kp": 1.0, "soil_class": "Z"}]
    issues = gm.validate_units(units)
    ok = any("overlap" in i for i in issues)
    ok = ok and any("greater than" in i for i in issues)
    ok = ok and any("deeper than top" in i for i in issues)
    ok = ok and any("required" in i for i in issues)
    clean = [_unit(0.0, 10.0, 0.0, 1.0, "SAND"), _unit(12.0, 20.0, 0.0, 3.0, "CLAY")]
    ok = ok and not gm.validate_units(clean)
    gaps = gm.coverage_gaps(clean, 0.0, 20.0, depth_m=1.5)
    # SAND stops at 1.0 m, so 0-10 has no unit at 1.5 m; 10-12 has nothing.
    ok = ok and len(gaps) == 1 and abs(gaps[0][0] - 0.0) < 1e-9 \
        and abs(gaps[0][1] - 12.0) < 0.011
    summary = gm.summarise_by_class(clean, depth_m=0.5, start_kp=0.0, end_kp=20.0)
    by_code = {r["soil_class"]: r["length_km"] for r in summary}
    ok = ok and abs(by_code.get("SAND", 0) - 10.0) < 0.05 \
        and abs(by_code.get("CLAY", 0) - 8.0) < 0.05
    return _result("validate + coverage gaps + class summary", ok,
                   "; ".join(issues[:3]))


def test_groups_and_classes() -> bool:
    ok = gm.guess_group("Dense SAND") == gm.GROUP_SAND
    ok = ok and gm.guess_group("soft CLAY") == gm.GROUP_CLAY
    ok = ok and gm.guess_group("sandy clay") == gm.GROUP_MIXED
    ok = ok and gm.guess_group("chalk bedrock") == gm.GROUP_ROCK
    ok = ok and gm.guess_group("???") == gm.GROUP_UNKNOWN
    ok = ok and gm.auto_color("abc") == gm.auto_color("ABC")
    units = [_unit(0, 1, 0, 1, "SAND"), _unit(1, 2, 0, 1, "sand"),
             _unit(2, 3, 0, 1, "CLAY", description="soft clay")]
    created = gm.missing_classes(units, [{"code": "Sand", "color": "#fff"}])
    ok = ok and len(created) == 1 and created[0]["code"] == "CLAY" \
        and created[0]["group"] == gm.GROUP_CLAY
    by_code = gm.class_lookup([{"code": "Sand", "color": "#123456", "label": "Fine sand"}])
    ok = ok and gm.color_for("SAND", by_code) == "#123456"
    ok = ok and gm.label_for("sand", by_code) == "Fine sand"
    return _result("soil groups + class registry helpers", ok)


def test_csv_import_intervals() -> bool:
    text = ("# comment line\n"
            "KP From (km),KP To (km),Top (m),Thickness (m),Soil Unit,Description,Su (kPa)\n"
            "0.000,2.500,0,1.2,SAND,Dense sand,\n"
            "2.500,4.000,0,0.8,CLAY,Soft clay,15\n"
            "4.000,5.000,,,,,\n")
    headers, rows = gm.parse_delimited_text(text)
    mapping = gm.guess_mapping(headers)
    ok = mapping.get(gm.TARGET_START) == 0 and mapping.get(gm.TARGET_END) == 1
    ok = ok and mapping.get(gm.TARGET_TOP) == 2 and mapping.get(gm.TARGET_BASE) == 3
    ok = ok and mapping.get(gm.TARGET_CLASS) == 4 and mapping.get(gm.TARGET_STRENGTH) == 6
    units, problems = gm.rows_to_units(rows, mapping, thickness_as_base=True,
                                       default_source="GM Rev B")
    ok = ok and len(units) == 3 and units[0]["base_m"] == 1.2 \
        and units[1]["strength"] == "15" and units[0]["source_ref"] == "GM Rev B"
    ok = ok and any("no soil class" in p for p in problems)
    # metres in file
    units_m, _p = gm.rows_to_units([["1500", "2500", "", "", "SAND"]],
                                   {gm.TARGET_START: 0, gm.TARGET_END: 1,
                                    gm.TARGET_CLASS: 4}, kp_in_metres=True)
    ok = ok and abs(units_m[0]["start_kp"] - 1.5) < 1e-9
    # Semicolon CSV with decimal commas
    headers2, rows2 = gm.parse_delimited_text("start_kp;end_kp;class\n0,5;1,5;Sand\n")
    mapping2 = gm.guess_mapping(headers2)
    units2, _p2 = gm.rows_to_units(rows2, mapping2)
    ok = ok and len(units2) == 1 and abs(units2[0]["end_kp"] - 1.5) < 1e-9
    return _result("CSV intervals: header guessing + thickness + units", ok,
                   str(mapping))


def test_csv_import_horizons() -> bool:
    rows = [["0.0", "0", "1.0", "3.0"],
            ["1.0", "0", "1.5", "3.0"],
            ["2.0", "0", "", "3.5"]]
    horizons = [(1, "SAND"), (2, "CLAY"), (3, "ROCK")]
    units, problems = gm.horizons_to_units(rows, 0, horizons)
    # Station 0->1: SAND 0-1.0/1.5 (sloping base), CLAY 1.0/1.5-3.0, ROCK open
    # Station 1->2: CLAY pick missing at KP 2 -> SAND has no base pair; the
    # CLAY unit is skipped; ROCK open from 3.0->3.5.
    ok = len(units) == 5 and not problems
    sand01 = [u for u in units if u["soil_class"] == "SAND" and u["start_kp"] == 0.0][0]
    ok = ok and sand01["base_m"] == 1.0 and sand01["base_end_m"] == 1.5
    rock12 = [u for u in units if u["soil_class"] == "ROCK" and u["start_kp"] == 1.0][0]
    ok = ok and rock12["top_m"] == 3.0 and rock12["top_end_m"] == 3.5 \
        and rock12["base_m"] is None
    return _result("horizon table -> trapezoid units", ok, f"{len(units)} units")


def test_csv_export_round_trip() -> bool:
    units = [_unit(0.0, 2.5, 0.0, 1.2, "SAND", description="Dense sand"),
             _unit(2.5, 4.0, 0.0, None, "CLAY", src_start_kp=2.45,
                   src_end_kp=3.95, src_rpl="Rev B", rereference_flags="gap")]
    plan = {"name": "Plan A", "rpl_name": "Route", "rpl_revision": "Rev D"}
    text = gm.units_csv(plan, units, [{"code": "SAND", "label": "Sand", "group": "sand"}])
    ok = "# plan: Plan A" in text and "# rpl: Route Rev D" in text
    headers, rows = gm.parse_delimited_text(text)
    mapping = gm.guess_mapping(headers)
    parsed, _p = gm.rows_to_units(rows, mapping)
    ok = ok and len(parsed) == 2 and parsed[1]["base_m"] is None
    ok = ok and abs(parsed[0]["base_m"] - 1.2) < 1e-9
    ok = ok and rows[1][headers.index("src_rpl")] == "Rev B"
    # File reader (CSV path) agrees with the text parser
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "gm.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        h2, r2, sheets = gm.read_table_file(path)
        ok = ok and h2 == headers and len(r2) == 2 and sheets == []
    return _result("CSV export -> re-import round trip", ok)


def test_kpmap_anchors_and_shift() -> bool:
    m = kr.KpMap.from_anchors([(0.0, 0.0), (10.0, 10.5), (20.0, 20.5), (15.0, 9.0)])
    ok = m.diagnostics.dropped_monotone == 1 and len(m.anchors) == 3
    v, flags = m.map_kp(5.0)
    ok = ok and abs(v - 5.25) < 1e-9 and not flags
    v, flags = m.map_kp(25.0)
    ok = ok and abs(v - 25.5) < 1e-9 and kr.FLAG_EXTRAPOLATED in flags
    v, flags = m.map_kp(-1.0)
    ok = ok and abs(v + 1.0) < 1e-9 and kr.FLAG_EXTRAPOLATED in flags
    a, b, flags = m.map_range(0.0, 10.0)
    ok = ok and abs(b - 10.5) < 1e-9 and not flags  # 5 % stretch < 10 % tol
    a, b, flags = m.map_range(0.0, 10.0, stretch_tol=0.02)
    ok = ok and kr.FLAG_STRETCHED in flags
    s = kr.KpMap.shift(0.25)
    v, flags = s.map_kp(3.0)
    ok = ok and abs(v - 3.25) < 1e-9
    ident = kr.KpMap.identity()
    ok = ok and ident.is_identity and ident.map_kp(7.0) == (7.0, [])
    inv = m.inverse()
    v, _f = inv.map_kp(10.5)
    ok = ok and abs(v - 10.0) < 1e-9
    restored = kr.KpMap.from_dict(m.to_dict())
    ok = ok and restored.anchors == m.anchors and restored.method == kr.METHOD_ANCHORS
    return _result("KpMap anchors/shift/identity/inverse/serialise", ok)


def test_kpmap_geometry_with_reroute() -> bool:
    # Source route 0-30 km; the target coincides (offset 2 m) except for a
    # re-routed stretch 10-14 km (offsets 300 m) that is 0.6 km longer on
    # the new revision — so target KPs run +0.6 km beyond it.
    samples = []
    for i in range(301):
        kp = round(i * 0.1, 6)
        if 10.0 < kp < 14.0:
            samples.append((kp, 12.0 + (kp - 10.0) * 0.3, 300.0))  # garbage snaps
        elif kp >= 14.0:
            samples.append((kp, kp + 0.6, 2.0))
        else:
            samples.append((kp, kp, 2.0))
    m = kr.build_from_samples(samples, offset_tol_m=25.0)
    d = m.diagnostics
    ok = d.method == kr.METHOD_GEOMETRY and d.dropped_offset == 39
    ok = ok and len(d.gap_ranges) == 1 and abs(d.gap_ranges[0][0] - 10.0) < 1e-6 \
        and abs(d.gap_ranges[0][1] - 14.0) < 1e-6
    ok = ok and 2 <= len(m.anchors) <= 6  # thinned: two straight pieces
    v, flags = m.map_kp(5.0)
    ok = ok and abs(v - 5.0) < 1e-6 and not flags
    v, flags = m.map_kp(20.0)
    ok = ok and abs(v - 20.6) < 1e-6 and not flags
    v, flags = m.map_kp(12.0)   # inside the gap: linear 10->10, 14->14.6
    ok = ok and abs(v - 12.3) < 1e-6 and kr.FLAG_GAP in flags
    a, b, flags = m.map_range(9.0, 15.0)
    ok = ok and kr.FLAG_GAP in flags and kr.FLAG_STRETCHED not in flags
    # Reversed/near-parallel snaps (non-monotone) are dropped, not used.
    bad = [(0.0, 0.0, 1.0), (1.0, 1.0, 1.0), (2.0, 0.5, 1.0), (3.0, 3.0, 1.0)]
    m2 = kr.build_from_samples(bad, offset_tol_m=10.0)
    ok = ok and m2.diagnostics.dropped_monotone == 1
    empty = kr.build_from_samples([], offset_tol_m=10.0)
    ok = ok and not empty.anchors and empty.diagnostics.notes
    text = kr.parse_anchor_text("# a\n0, 0\n12.35;12.41\n48.9\t49.275\nbad line\n")
    ok = ok and text == [(0.0, 0.0), (12.35, 12.41), (48.9, 49.275)]
    return _result("geometry map with re-routed gap + thinning + parsing", ok,
                   d.summary())


def test_rereference_units() -> bool:
    m = kr.KpMap.from_anchors([(0.0, 0.0), (10.0, 10.5)])
    units = [_unit(2.0, 4.0, 0.0, 1.0, "SAND"),
             _unit(6.0, 8.0, 0.0, 1.0, "CLAY", src_start_kp=5.0, src_end_kp=7.0,
                   src_rpl="Rev A")]
    mapped, tally = gm.rereference_units(units, m, source_label="Rev B")
    ok = abs(mapped[0]["start_kp"] - 2.1) < 1e-9 and abs(mapped[0]["end_kp"] - 4.2) < 1e-9
    ok = ok and mapped[0]["src_start_kp"] == 2.0 and mapped[0]["src_rpl"] == "Rev B"
    # Unit with delivered source KPs maps from those, keeps its own label.
    ok = ok and abs(mapped[1]["start_kp"] - 5.25) < 1e-9 and mapped[1]["src_rpl"] == "Rev A"
    # use_source_kps=False maps from the current KPs instead.
    mapped2, _t = gm.rereference_units(units, m, use_source_kps=False)
    ok = ok and abs(mapped2[1]["start_kp"] - 6.3) < 1e-9
    ok = ok and isinstance(tally, dict)
    return _result("rereference_units provenance", ok)


def test_class_runs() -> bool:
    units = [_unit(0.0, 4.0, 0.0, 1.0, "SAND"),
             _unit(0.0, 4.0, 1.0, None, "CLAY"),
             _unit(4.0, 6.0, 0.0, 1.0, "SAND"),          # merges with the first at seabed
             _unit(7.0, 9.0, 0.0, 2.0, "ROCK", top_end_m=0.0, base_end_m=0.5)]
    seabed = gm.class_runs(units, 0.0, 0.0, 10.0)
    ok = [(a, b, c) for a, b, c, _n in seabed] == [(0.0, 6.0, "SAND"), (7.0, 9.0, "ROCK")]
    ok = ok and seabed[0][3] == 2
    at_1_5 = gm.class_runs(units, 1.5, 0.0, 10.0)
    # CLAY below the sand 0-4; nothing 4-6 at 1.5 m; ROCK base slopes from
    # 2.0 to 0.5 over 7-9, so it covers 1.5 m only until KP ~7.67.
    ok = ok and at_1_5[0][:3] == (0.0, 4.0, "CLAY")
    ok = ok and at_1_5[1][2] == "ROCK" and abs(at_1_5[1][0] - 7.0) < 1e-9 \
        and 7.6 <= at_1_5[1][1] <= 7.75
    # Window clipping and empty cases
    ok = ok and [r[:3] for r in gm.class_runs(units, 0.0, 2.0, 5.0)] == [(2.0, 5.0, "SAND")]
    ok = ok and gm.class_runs(units, 0.0, 20.0, 30.0) == []
    ok = ok and gm.class_runs([], 0.0, 0.0, 10.0) == []
    return _result("class_runs at seabed / depth with sloping base", ok, str(at_1_5))


def run_all():
    return [
        test_class_runs(),
        test_normalise_and_lookup(),
        test_validate_and_coverage(),
        test_groups_and_classes(),
        test_csv_import_intervals(),
        test_csv_import_horizons(),
        test_csv_export_round_trip(),
        test_kpmap_anchors_and_shift(),
        test_kpmap_geometry_with_reroute(),
        test_rereference_units(),
    ]


if __name__ == "__main__":
    import sys
    sys.exit(0 if all(run_all()) else 1)
