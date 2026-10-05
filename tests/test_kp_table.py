"""KP-range tables: reading, units, unreadable rows and RPL translation."""
import json
import unittest

from ..burial import kp_table as kt
from ..burial.kp_rereference import KpMap
from ..burial.plan_rereference import map_plan

REQUIRES_QGIS = False


def rows(*pairs, start="start_kp", end="end_kp"):
    return [(str(i), {start: a, end: b, "hazard": f"H{i}"})
            for i, (a, b) in enumerate(pairs)]


class KpTableTests(unittest.TestCase):
    def test_reads_numbers_and_numeric_text(self):
        ranges, notes = kt.read_ranges(rows((1, 2), ("3.5", " 4.25 ")), {})
        self.assertEqual([(r.source_start, r.source_end) for r in ranges],
                         [(1.0, 2.0), (3.5, 4.25)])
        self.assertEqual(notes, [])

    def test_wrong_fields_are_reported_not_silent(self):
        ranges, notes = kt.read_ranges(
            rows((1, 2), (3, 4), start="KP_From", end="KP_To"), {})
        self.assertEqual(ranges, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("2 of 2 row(s) lack the fields 'start_kp'/'end_kp'", notes[0])

    def test_unreadable_values_are_counted(self):
        ranges, notes = kt.read_ranges(
            rows((1, 2), ("12,5", 13), ("KP 4", 5), (None, 6)), {})
        self.assertEqual(len(ranges), 1)
        self.assertIn("3 of 4 row(s) have an empty or non-numeric", notes[0])

    def test_metre_kps_convert_to_km(self):
        ranges, _ = kt.read_ranges(rows((1500, 2250)), {kt.KP_UNIT_KEY: "m"})
        self.assertEqual((ranges[0].source_start, ranges[0].source_end), (1.5, 2.25))

    def test_reversed_range_orders_on_route(self):
        ranges, _ = kt.read_ranges(rows((5, 3)), {})
        kt.translate(ranges, None)
        self.assertEqual((ranges[0].lo, ranges[0].hi), (3.0, 5.0))

    def test_translation_keeps_quoted_kps_and_flags(self):
        ranges, _ = kt.read_ranges(rows((1, 2), (10, 11)), {})
        kp_map = KpMap.from_anchors([(0.0, 0.5), (5.0, 5.5)])
        kt.translate(ranges, kp_map.map_range)
        self.assertEqual((ranges[0].start, ranges[0].end), (1.5, 2.5))
        self.assertEqual((ranges[0].source_start, ranges[0].source_end), (1.0, 2.0))
        self.assertIn("extrapolated", ranges[1].flags)
        notes = kt.flag_notes(ranges)
        self.assertEqual(len(notes), 1)
        self.assertIn("1 range(s) need checking", notes[0])
        self.assertIn("KP 10.000–11.000 (extrapolated)", notes[0])

    def test_scope_note_only_when_nothing_overlaps(self):
        ranges, _ = kt.read_ranges(rows((1, 2), (8, 9)), {})
        kt.translate(ranges, None)
        self.assertEqual(kt.scope_note(ranges, 1.5, 3.0), [])
        note = kt.scope_note(ranges, 20.0, 30.0)
        self.assertIn("none of its 2 range(s)", note[0])

    def test_reference_text_and_fingerprint(self):
        self.assertIn("not recorded", kt.reference_text({}))
        self.assertEqual(kt.reference_text({kt.KP_REF_KEY: "a", kt.KP_REF_LABEL_KEY: "Route A — Rev C"}),
                         "Route A — Rev C")
        a, _ = kt.read_ranges(rows((1, 2)), {})
        b, _ = kt.read_ranges(rows((1, 2.001)), {})
        kt.translate(a, None)
        kt.translate(b, None)
        self.assertNotEqual(kt.fingerprint(a), kt.fingerprint(b))


class RereferenceStampTests(unittest.TestCase):
    def test_legacy_kp_table_rule_records_previous_rpl(self):
        plan = {"rpl_id": "rpl-b", "rpl_name": "Route", "rpl_revision": "Rev B",
                "params_json": "{}"}
        legacy = {"name": "Hazards", "kind": "kp_range_table",
                  "config_json": json.dumps({"input_id": "t"})}
        recorded = {"name": "Other", "kind": "kp_range_table",
                    "config_json": json.dumps({"input_id": "t", kt.KP_REF_KEY: "rpl-a",
                                               kt.KP_REF_LABEL_KEY: "Route — Rev A"})}
        out = map_plan(KpMap.shift(0.1), plan, [], [], rules=[legacy, recorded])
        stamped = json.loads(out["rules"][0]["config_json"])
        self.assertEqual(stamped[kt.KP_REF_KEY], "rpl-b")
        self.assertEqual(stamped[kt.KP_REF_LABEL_KEY], "Route — Rev B")
        kept = json.loads(out["rules"][1]["config_json"])
        self.assertEqual(kept[kt.KP_REF_KEY], "rpl-a")


if __name__ == "__main__":
    unittest.main()
