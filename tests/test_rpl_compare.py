# -*- coding: utf-8 -*-
"""Pure-Python checks for the RPL revision comparison (no QGIS).

Covers the position mapping (anchors, monotonicity, insertions, deletions,
renames and moves), the leg diff that follows it, the per-revision statistics
including alter-course and cable-type totals, and the presentation helpers the
panel and the CSV export share.

Run directly: ``python tests/run_pure_tests.py test_rpl_compare``.
"""

from __future__ import annotations

import sys

from ..workbench import rpl_compare


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" - {detail}"
    print(msg)
    return ok


# ---------------------------------------------------------------- fixtures --
def _point(seq, pos, event, kp, lat, lon, depth=None, cable_kp=None, remarks=""):
    return {
        "seq": seq, "pos": pos, "event": event, "kp": kp,
        "cable_kp": kp if cable_kp is None else cable_kp,
        "lat": lat, "lon": lon, "depth": depth, "remarks": remarks,
    }


def _leg(seq, route_km, cable_type, cable_km=None, slack=None, from_pos=None, to_pos=None):
    return {
        "seq": seq, "route_km": route_km,
        "cable_km": route_km if cable_km is None else cable_km,
        "cable_type": cable_type, "from_pos": from_pos, "to_pos": to_pos,
        "bearing": None, "slack": slack, "cable_code": "", "protection": "",
        "target_burial_m": None,
    }


def _base_revision():
    """Five positions on a straight east-west line, ~1.11 km apart."""
    points = [
        _point(0, 1, "BMH", 0.000, 50.0, 0.00, 5.0),
        _point(1, 2, "AC 1", 1.113, 50.0, 0.01, 40.0),
        _point(2, 3, "JT-1", 2.227, 50.0, 0.02, 80.0),
        _point(3, 4, "AC 2", 3.340, 50.0, 0.03, 120.0),
        _point(4, 5, "BU-1", 4.454, 50.0, 0.04, 160.0),
    ]
    legs = [
        _leg(0, 1.113, "DA"),
        _leg(1, 1.114, "SA"),
        _leg(2, 1.113, "SA"),
        _leg(3, 1.114, "LW"),
    ]
    return points, legs


# ------------------------------------------------------------------- tests --
def test_identical_revisions() -> bool:
    points, legs = _base_revision()
    comparison = rpl_compare.compare_revisions(points, legs, points, legs, "Rev 1", "Rev 2")
    counts = comparison.position_counts()
    ok = counts.get(rpl_compare.STATUS_UNCHANGED) == 5
    ok = ok and counts.get(rpl_compare.STATUS_CHANGED, 0) == 0
    ok = ok and counts.get(rpl_compare.STATUS_ADDED, 0) == 0
    ok = ok and counts.get(rpl_compare.STATUS_REMOVED, 0) == 0
    ok = ok and comparison.leg_counts().get(rpl_compare.STATUS_UNCHANGED) == 4
    return _result("identical revisions report no change", ok, str(counts))


def test_inserted_position_does_not_shift_the_tail() -> bool:
    """The whole point of matching: one insert must not mark everything after
    it as changed, which a PosNo/row-index diff would."""
    points_a, legs_a = _base_revision()
    points_b = list(points_a)
    points_b.insert(2, _point(99, 25, "AC 1A", 1.700, 50.0, 0.0153, 55.0))
    points_b = [dict(row, seq=index) for index, row in enumerate(points_b)]
    legs_b = [_leg(0, 1.113, "DA"), _leg(1, 0.650, "SA"), _leg(2, 0.464, "SA"),
              _leg(3, 1.113, "SA"), _leg(4, 1.114, "LW")]
    comparison = rpl_compare.compare_revisions(points_a, legs_a, points_b, legs_b)
    counts = comparison.position_counts()
    ok = counts.get(rpl_compare.STATUS_ADDED) == 1
    ok = ok and counts.get(rpl_compare.STATUS_UNCHANGED) == 5
    ok = ok and counts.get(rpl_compare.STATUS_REMOVED, 0) == 0
    added = [m for m in comparison.positions if m.status == rpl_compare.STATUS_ADDED]
    ok = ok and added and added[0].b.get("pos") == 25
    return _result("an inserted position is the only change", ok, str(counts))


def test_removed_position() -> bool:
    points_a, legs_a = _base_revision()
    points_b = [row for row in points_a if row["pos"] != 3]
    legs_b = [_leg(0, 1.113, "DA"), _leg(1, 2.227, "SA"), _leg(2, 1.114, "LW")]
    comparison = rpl_compare.compare_revisions(points_a, legs_a, points_b, legs_b)
    counts = comparison.position_counts()
    ok = counts.get(rpl_compare.STATUS_REMOVED) == 1
    ok = ok and counts.get(rpl_compare.STATUS_UNCHANGED) == 4
    removed = [m for m in comparison.positions if m.status == rpl_compare.STATUS_REMOVED]
    ok = ok and removed and removed[0].a.get("event") == "JT-1"
    return _result("a dropped position is reported once", ok, str(counts))


def test_moved_and_renamed_positions() -> bool:
    points_a, legs_a = _base_revision()
    points_b = [dict(row) for row in points_a]
    points_b[1]["lon"] = 0.0110          # ~71 m east
    points_b[1]["kp"] = 1.184
    points_b[3]["event"] = "AC 2A"       # renamed in place
    comparison = rpl_compare.compare_revisions(points_a, legs_a, points_b, legs_a)
    by_pos = {m.a.get("pos"): m for m in comparison.positions if m.a}
    moved = by_pos.get(2)
    renamed = by_pos.get(4)
    ok = moved is not None and rpl_compare.CHANGE_POSITION in moved.changes
    ok = ok and moved.distance_m is not None and 50.0 < moved.distance_m < 100.0
    ok = ok and rpl_compare.CHANGE_KP in moved.changes
    ok = ok and renamed is not None and renamed.changes == (rpl_compare.CHANGE_EVENT,)
    ok = ok and comparison.position_counts().get(rpl_compare.STATUS_CHANGED) == 2
    return _result("a move and a rename are matched, not re-listed", ok)


def test_matching_stays_in_route_order() -> bool:
    """Two positions carrying the same event text must not cross-match."""
    points_a = [
        _point(0, 1, "BMH", 0.0, 50.0, 0.00),
        _point(1, 2, "AC", 1.0, 50.0, 0.01),
        _point(2, 3, "AC", 2.0, 50.0, 0.02),
        _point(3, 4, "BU-1", 3.0, 50.0, 0.03),
    ]
    points_b = [dict(row) for row in points_a]
    comparison = rpl_compare.compare_revisions(points_a, [], points_b, [])
    pairs = [(m.a_index, m.b_index) for m in comparison.positions if m.matched]
    ok = pairs == [(0, 0), (1, 1), (2, 2), (3, 3)]
    ok = ok and all(a < b for (a, _x), (b, _y) in zip(pairs, pairs[1:]))
    return _result("duplicate event text still maps in order", ok, str(pairs))


def test_leg_diff_follows_the_positions() -> bool:
    points_a, legs_a = _base_revision()
    legs_b = [dict(row) for row in legs_a]
    legs_b[1]["cable_type"] = "DA"       # re-armoured
    legs_b[2]["route_km"] = 1.200        # longer
    comparison = rpl_compare.compare_revisions(points_a, legs_a, points_a, legs_b)
    changed = [m for m in comparison.legs if m.status == rpl_compare.STATUS_CHANGED]
    ok = len(changed) == 2
    ok = ok and "cable_type" in changed[0].changes
    ok = ok and "route_km" in changed[1].changes
    ok = ok and comparison.leg_counts().get(rpl_compare.STATUS_UNCHANGED) == 2
    return _result("leg attribute changes are reported per leg", ok)


def test_statistics() -> bool:
    points, legs = _base_revision()
    stats = rpl_compare.revision_stats(points, legs, label="Rev 1")
    ok = stats.position_count == 5 and stats.leg_count == 4
    ok = ok and stats.event_count == 5
    ok = ok and stats.alter_course_count == 2
    ok = ok and stats.body_count == 3          # BMH, JT-1, BU-1
    ok = ok and abs((stats.route_length_km or 0) - 4.454) < 1e-9
    ok = ok and stats.section_count == 4
    types = dict((name, route) for name, route, _cable in stats.cable_type_lengths)
    ok = ok and abs(types.get("SA", 0) - 2.227) < 1e-9
    ok = ok and abs(types.get("DA", 0) - 1.113) < 1e-9
    ok = ok and abs(types.get("LW", 0) - 1.114) < 1e-9
    return _result("revision statistics", ok, str(stats.cable_type_lengths))


def test_statistics_incomplete_lengths() -> bool:
    """A missing leg length must not produce a confident partial total."""
    points, legs = _base_revision()
    legs = [dict(row) for row in legs]
    legs[1]["route_km"] = None
    stats = rpl_compare.revision_stats(points, legs)
    # Falls back to the KP span rather than summing an incomplete set.
    ok = abs((stats.route_length_km or 0) - 4.454) < 1e-9
    types = dict((name, route) for name, route, _cable in stats.cable_type_lengths)
    ok = ok and types.get("SA") is None
    ok = ok and types.get("DA") is not None
    return _result("incomplete lengths report None, not a partial sum", ok)


def test_statistic_rows_and_summary() -> bool:
    points_a, legs_a = _base_revision()
    points_b = [dict(row) for row in points_a]
    points_b.append(_point(5, 6, "BMH 2", 5.567, 50.0, 0.05, 200.0))
    legs_b = legs_a + [_leg(4, 1.113, "DA")]
    comparison = rpl_compare.compare_revisions(
        points_a, legs_a, points_b, legs_b, "Rev 1", "Rev 2")
    rows = {row[0]: row for row in rpl_compare.statistic_rows(comparison)}
    ok = rows["Positions"][1] == "5" and rows["Positions"][2] == "6"
    ok = ok and rows["Positions"][3] == "+1"
    ok = ok and rows["Legs"][3] == "+1"
    ok = ok and rows["Route length"][3].startswith("+1.113")
    ok = ok and "DA (route)" in rows
    summary = rpl_compare.change_summary(comparison)
    ok = ok and "1 added" in summary
    return _result("statistic rows and summary text", ok, summary)


def test_haversine() -> bool:
    # One degree of longitude at 50N is ~71.7 km.
    distance = rpl_compare.haversine_m(50.0, 0.0, 50.0, 1.0)
    ok = distance is not None and 71000 < distance < 72000
    ok = ok and rpl_compare.haversine_m(50.0, 0.0, 50.0, 0.0) == 0.0
    ok = ok and rpl_compare.haversine_m(None, 0.0, 50.0, 0.0) is None
    return _result("haversine distance", ok, f"{distance:.0f} m")


def test_normalise_event() -> bool:
    cases = {"BU-1": "BU1", "bu 1": "BU1", " AC  12 ": "AC12", "": "", None: ""}
    ok = all(rpl_compare.normalise_event(k) == v for k, v in cases.items())
    return _result("event text normalisation", ok)


def test_empty_revision() -> bool:
    points, legs = _base_revision()
    comparison = rpl_compare.compare_revisions(points, legs, [], [])
    counts = comparison.position_counts()
    ok = counts.get(rpl_compare.STATUS_REMOVED) == 5
    ok = ok and comparison.leg_counts().get(rpl_compare.STATUS_REMOVED) == 4
    ok = ok and comparison.stats_b.position_count == 0
    return _result("comparison against an empty revision", ok)


def run_all():
    return [
        test_identical_revisions(),
        test_inserted_position_does_not_shift_the_tail(),
        test_removed_position(),
        test_moved_and_renamed_positions(),
        test_matching_stays_in_route_order(),
        test_leg_diff_follows_the_positions(),
        test_statistics(),
        test_statistics_incomplete_lengths(),
        test_statistic_rows_and_summary(),
        test_haversine(),
        test_normalise_event(),
        test_empty_revision(),
    ]


def main() -> int:
    results = run_all()
    failures = results.count(False)
    print(f"{len(results) - failures}/{len(results)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
