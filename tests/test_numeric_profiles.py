"""Numeric datasets: measurement import, placement, checks, export and colour classes."""
import json
import unittest

from ..burial import numeric_profiles as n
from ..burial.kp_rereference import KpMap
from ..burial.kp_table import KpRange
from ..burial.plan_rereference import map_plan

REQUIRES_QGIS = False

POINTS = {"source_id": 0, "depth": 1, "value": 2}


class NumericProfilesTests(unittest.TestCase):
    def profiles(self):
        return n.import_profiles([["CPT-01", "0", "0"], ["CPT-01", "0.02", ""],
                                  ["CPT-01", "2", "150"], ["CPT-02", "0", "50"]],
                                 POINTS, dataset_id="d1", variable="su", units="kPa")

    def test_missing_zero_and_unmeasured_are_distinct(self):
        profiles = self.profiles()
        index = n.ProfileIndex(profiles, [{"source_id": "CPT-01", "start_kp": 10, "end_kp": 11}])
        self.assertEqual(index.at(10.5, 0)[0][1]["value"], 0)
        missing = index.at(10.5, .02)[0][1]
        self.assertIsNone(missing["value"])
        self.assertEqual(missing["flags"], "missing")
        self.assertIsNone(index.at(10.5, 1)[0][1])
        self.assertEqual(index.at(11, 0), [])
        self.assertEqual(index.limits(), (0, 150))

    def test_interval_rows_depth_unit_decimal_comma_and_missing_codes(self):
        (profile,) = n.import_profiles([["001", "100", "200", "1,5"], ["001", "200", "300", "-9999"]],
                                       {"source_id": 0, "depth": 1, "base": 2, "value": 3},
                                       variable="su", units="MPa", depth_scale=.01,
                                       decimal_comma=True, missing=["-9999"])
        self.assertEqual(profile["source_id"], "001")
        self.assertEqual([(s["top"], s["base"], s["value"]) for s in profile["samples"]],
                         [(1, 2, 1.5), (2, 3, None)])

    def test_profile_ids_belong_to_their_dataset(self):
        a = n.import_profiles([["A", "0", "1"]], POINTS, dataset_id="one")
        b = n.import_profiles([["A", "0", "1"]], POINTS, dataset_id="two")
        self.assertNotEqual(a[0]["profile_id"], b[0]["profile_id"])

    def test_bad_rows_and_missing_columns_are_rejected_with_row_numbers(self):
        for rows in ([["A", "-1", "2"]], [["A", "0", "bad"]], [["A", "nan", "2"]]):
            with self.assertRaises(ValueError):
                n.import_profiles(rows, POINTS)
        with self.assertRaisesRegex(ValueError, "appears twice .data rows 1 and 2"):
            n.import_profiles([["A", "0", "2"], ["A", "0", "3"]], POINTS)
        with self.assertRaisesRegex(ValueError, "overlap"):
            n.import_profiles([["A", "0", "2", "2"], ["A", "1", "4", "3"]], dict(POINTS, base=2, value=3))
        with self.assertRaisesRegex(ValueError, "Choose the value column"):
            n.import_profiles([["A", "0"]], {"source_id": 0, "depth": 1})
        self.assertEqual(n.column_indices(["x", "ID", "d"], {"source_id": "ID", "base": ""}), {"source_id": 1})
        with self.assertRaisesRegex(ValueError, "no column named 'Depth'"):
            n.column_indices(["x"], {"depth": "Depth"})

    def test_ranges_become_assignments_and_report_missing_ids(self):
        ranges = [KpRange("1", 10, 11, start=10, end=11), KpRange("2", 13, 12, start=13, end=12, flags=["gap"]),
                  KpRange("3", 14, 15, start=14, end=15)]
        rows, notes = n.ranges_to_assignments(ranges, {"1": "A", "2": " B ", "3": ""}, "ref")
        self.assertEqual([(r["source_id"], r["start_kp"], r["end_kp"], r["flags"]) for r in rows],
                         [("A", 10, 11, ""), ("B", 12, 13, "gap")])
        self.assertEqual(notes, ["1 KP range row(s) have no investigation ID and were skipped"])

    def test_overlaps_adjacency_and_disconnected_ranges(self):
        rows = [{"source_id": "A", "start_kp": 0, "end_kp": 1}, {"source_id": "B", "start_kp": 1, "end_kp": 2}]
        self.assertEqual(n.coverage_runs(rows), [(0, 1, (0,)), (1, 2, (1,))])
        rows = [{"source_id": "A", "start_kp": 0, "end_kp": 1}, {"source_id": "A", "start_kp": 2, "end_kp": 3},
                {"source_id": "X", "start_kp": .5, "end_kp": 2.5}]
        self.assertEqual(n.coverage_runs(rows), [(0, .5, (0,)), (.5, 1, (0, 2)), (1, 2, (2,)),
                                                 (2, 2.5, (1, 2)), (2.5, 3, (1,))])

    def test_check_lists_every_investigation_and_route_findings(self):
        assignments = [{"source_id": "CPT-01", "start_kp": 10, "end_kp": 11},
                       {"source_id": "CPT-01", "start_kp": 12, "end_kp": 13},
                       {"source_id": "cpt 02", "start_kp": 12.5, "end_kp": 15}]
        rows, notes = n.check_dataset(self.profiles(), assignments, bounds=(10, 14), scope=(10, 14))
        self.assertEqual([(r["source_id"], r["samples"], r["missing"], r["min"], r["max"], r["ranges"], r["status"])
                          for r in rows],
                         [("CPT-01", 3, 1, 0, 150, [(10, 11), (12, 13)], "placed"),
                          ("CPT-02", 1, 0, 50, 50, [], "no KP range")])
        text = "\n".join(notes)
        self.assertIn("1 KP range ID(s) have no measurements: cpt 02 (did you mean CPT-02?)", text)
        self.assertIn("1 investigation(s) have no KP range: CPT-02", text)
        self.assertIn("beyond the route: cpt 02", text)
        self.assertIn("1 overlapping stretch(es)", text)
        self.assertIn("cover 3.000 of 4.000 km of the plan scope (75%)", text)

    def test_exported_cells_carry_kp_depth_value_class_and_status(self):
        classes = n.normalise_classes([{"max": 100, "max_inclusive": False, "colour": "#ff0000"},
                                       {"min": 100, "colour": "#00ff00"}])
        assignments = [{"source_id": "CPT-01", "start_kp": 10, "end_kp": 11},
                       {"source_id": "CPT-02", "start_kp": 10.5, "end_kp": 12},
                       {"source_id": "none", "start_kp": 13, "end_kp": 14}]
        cells = n.ProfileIndex(self.profiles(), assignments).cells(classes, "su")
        first = cells[0]
        self.assertEqual((first["source_id"], first["kp_from"], first["kp_to"], first["value"],
                          first["class"], first["status"]), ("CPT-01", 10, 10.5, 0, "su < 100", "ok"))
        self.assertEqual(cells[1]["status"], "missing")
        self.assertAlmostEqual(cells[2]["depth_base_m"], 2.01)  # half the reading thickness
        self.assertEqual({c["status"] for c in cells if c["kp_from"] == 10.5}, {"overlap"})
        self.assertEqual(cells[-1]["status"], "no measurements")

    def test_summary_and_dense_profiles(self):
        self.assertEqual(n.summary_text(self.profiles(), "kPa"),
                         "2 investigation(s), 4 depth sample(s) (1 missing), depth 0–2.01 m, values 0–150 kPa")
        rows = [["CPT", i / 1000, i % 300] for i in range(100000)]
        profiles = n.import_profiles(rows, POINTS, variable="su", sample_support=.001)
        index = n.ProfileIndex(profiles, [{"source_id": "CPT", "start_kp": 0, "end_kp": 100}])
        self.assertEqual(index.at(50, 75.123)[0][1]["value"], 123)
        self.assertEqual(index.limits(), (0, 299))

    def test_plan_rereference_leaves_dataset_choice_alone(self):
        state = {"dataset_id": "d1", "depth_max": 2}
        plan = {"params_json": json.dumps({"numeric_ground": state})}
        mapped = map_plan(KpMap.shift(2), plan, [], [])
        self.assertEqual(json.loads(mapped["plan"]["params_json"])["numeric_ground"], state)

    def test_custom_classes_bounds_first_match_and_labels(self):
        classes = n.normalise_classes([
            {"max": 5, "max_inclusive": False, "colour": "#FF0000", "label": "Band A"},
            {"min": "5", "max": 10, "min_inclusive": True, "max_inclusive": False, "colour": "#ffff00"},
            {"min": 10, "max": 200, "min_inclusive": True, "max_inclusive": True, "colour": "#00ff00"},
            {"min": 200, "min_inclusive": False, "colour": "#ff0000"}])
        self.assertEqual(classes[0]["colour"], "#ff0000")
        self.assertEqual(n.class_of(-3, classes)["label"], "Band A")
        self.assertEqual(n.class_of(5, classes)["min"], 5)
        self.assertEqual(n.class_of(200, classes)["min"], 10)
        self.assertEqual(n.class_of(200.5, classes)["min"], 200)
        self.assertIsNone(n.class_of(None, classes))
        self.assertEqual([n.class_label(c, "su") for c in classes],
                         ["Band A", "5 ≤ su < 10", "10 ≤ su ≤ 200", "200 < su"])
        self.assertEqual(n.class_coverage(classes, "su"), [])
        overlap = n.normalise_classes([{"min": 0, "max": 6, "colour": "#000001"},
                                       {"min": 5, "max": 9, "colour": "#000002"}])
        self.assertEqual(n.class_of(5.5, overlap)["colour"], "#000001")
        self.assertEqual(n.class_coverage(overlap, "su"),
                         ["No class covers su < 0; 9 < su (drawn dark grey).",
                          "Overlapping rows 1 and 2: the first matching row's colour is used."])
        for bad in ([], [{"min": 6, "max": 5, "colour": "#000000"}], [{"colour": "#000000"}],
                    [{"min": "x", "max": 9, "colour": "#000000"}], [{"min": 0, "max": 9, "colour": "red"}]):
            with self.assertRaises(ValueError):
                n.normalise_classes(bad)

    def test_class_coverage_gaps_and_touching_bounds(self):
        def rows(*specs):
            return n.normalise_classes([dict(zip(("min", "max", "min_inclusive", "max_inclusive"), spec),
                                             colour="#000000") for spec in specs])
        self.assertEqual(n.class_coverage(rows((None, 5, True, False), (5, None, True, True))), [])
        self.assertEqual(n.class_coverage(rows((None, 5, True, False), (5, None, False, True))),
                         ["No class covers value = 5 (drawn dark grey)."])
        self.assertTrue(n.class_coverage(rows((None, 5, True, True), (5, None, True, True)))[0]
                        .startswith("Overlapping rows 1 "))
        self.assertEqual(n.class_coverage(rows((None, 5, True, False), (7, None, True, True))),
                         ["No class covers 5 ≤ value < 7 (drawn dark grey)."])

    def test_breaks_and_colour_settings(self):
        classes = n.classes_from_breaks([30, 1, 4, 1], ["#a00000", "#b00000"])
        self.assertEqual([(c["min"], c["max"]) for c in classes], [(None, 1), (1, 4), (4, 30), (30, None)])
        self.assertEqual(classes[2]["colour"], "#a00000")
        self.assertEqual(n.class_of(4, n.normalise_classes(classes))["min"], 4)
        defaults = n.colour_settings(None)
        self.assertEqual((defaults["mode"], defaults["bands"], defaults["auto"]), ("continuous", 5, True))
        self.assertEqual(len(n.display_classes({"mode": "classes", "classes": classes})), 4)
        self.assertIsNone(n.display_classes({"mode": "continuous", "classes": classes}))
        self.assertIsNone(n.display_classes({"mode": "classes", "classes": [{"min": 1}]}))


if __name__ == "__main__":
    unittest.main()
