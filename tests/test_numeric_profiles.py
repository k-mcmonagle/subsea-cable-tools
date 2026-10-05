"""Numeric depth imports, missing data, assignment conflicts and KP changes."""
import json
import unittest

from ..burial import numeric_profiles as n
from ..burial.kp_rereference import KpMap
from ..burial.plan_rereference import map_plan

REQUIRES_QGIS = False


class NumericProfilesTests(unittest.TestCase):
    def profiles(self):
        return n.import_profiles([
            ["CPT-01", "0", "0", "good"], ["CPT-01", "0.02", "", "partial"],
            ["CPT-01", "2", "150", ""], ["CPT-02", "0", "50", ""]],
            {"source_id": 0, "depth": 1, "value": 2, "flags": 3}, variable="su", units="kPa")

    def test_missing_zero_and_unmeasured_are_distinct(self):
        profiles = self.profiles()
        assignments = [{"source_id": "CPT-01", "start_kp": 10, "end_kp": 11}]
        index = n.ProfileIndex(profiles, assignments, ("su", "kPa"))
        self.assertEqual(index.at(10.5, 0)[0][1]["value"], 0)
        missing = index.at(10.5, .02)[0][1]
        self.assertIsNone(missing["value"])
        self.assertIn("partial", missing["flags"])
        self.assertIn("missing", missing["flags"])
        self.assertIsNone(index.at(10.5, 1)[0][1])
        self.assertEqual(index.at(11, 0), [])
        self.assertEqual(index.limits(), (0, 150))
        index.at(10.5, .02)  # Panning/hover/depth selection cannot change limits.
        self.assertEqual(index.limits(), (0, 150))

    def test_wide_import_units_and_scaling(self):
        profiles = n.import_profiles([["001", "100", "200", "1,5", "-9999"]],
            {"source_id": 0, "depth": 1, "base": 2},
            variables=[(3, "su", "MPa"), (4, "qc", "kPa")], depth_scale=.01,
            decimal_comma=True, missing=["-9999"])
        by_name = {p["variable"]: p for p in profiles}
        self.assertEqual(by_name["su"]["source_id"], "001")
        self.assertEqual(by_name["su"]["samples"][0]["value"], 1.5)
        self.assertEqual(by_name["su"]["samples"][0]["top"], 1)
        self.assertEqual(by_name["su"]["samples"][0]["base"], 2)
        self.assertIsNone(by_name["qc"]["samples"][0]["value"])

    def test_long_format_and_unit_identity(self):
        profiles = n.import_profiles([["A", "0", "su", "kPa", "20"], ["A", "0", "su", "MPa", ".02"]],
            {"source_id": 0, "depth": 1, "variable": 2, "units": 3, "value": 4})
        self.assertEqual(len(profiles), 2)
        self.assertNotEqual(profiles[0]["profile_id"], profiles[1]["profile_id"])
        index = n.ProfileIndex(profiles, [{"source_id": "A", "start_kp": 0, "end_kp": 1}], ("su", "MPa"))
        self.assertEqual(index.at(.5, 0)[0][1]["value"], .02)

    def test_invalid_depth_duplicate_and_overlapping_samples_rejected(self):
        mapping = {"source_id": 0, "depth": 1, "value": 2}
        for rows in ([["A", "-1", "2"]], [["A", "0", "bad"]], [["A", "nan", "2"]],
                     [["A", "0", "2"], ["A", "0", "3"]]):
            with self.assertRaises(ValueError):
                n.import_profiles(rows, mapping, variable="qc")
        with self.assertRaises(ValueError):
            n.import_profiles([["A", "0", "2", "2"], ["A", "1", "4", "3"]],
                              dict(mapping, base=3), variable="qc")

    def test_disconnected_assignments_and_overlaps(self):
        assignments = n.import_assignments([["CPT-01", 0, 1], ["CPT-01", 2, 3], ["unknown", .5, 2.5]],
            {"source_id": 0, "start_kp": 1, "end_kp": 2}, KpMap.shift(10))
        self.assertEqual(assignments[1]["start_kp"], 12)
        self.assertEqual(assignments[1]["src_start_kp"], 2)
        runs = n.coverage_runs(assignments)
        self.assertEqual(runs, [(10, 10.5, (0,)), (10.5, 11, (0, 2)), (11, 12, (2,)),
                                (12, 12.5, (1, 2)), (12.5, 13, (1,))])
        issues = n.assignment_issues(assignments, self.profiles(), (10, 12))
        self.assertTrue(any("Unmatched ID" in s for s in issues))
        self.assertTrue(any("Overlap" in s for s in issues))
        self.assertTrue(any("beyond" in s for s in issues))
        self.assertTrue(any("Unassigned profile: CPT-02" in s for s in issues))
        index = n.ProfileIndex(self.profiles(), assignments[:2], ("su", "kPa"))
        self.assertEqual(index.at(11.5, 0), [])

    def test_route_rereference_preserves_source_measurements_and_delivery_kps(self):
        rows = n.import_assignments([["A", 10, 11], ["A", 12, 13]],
                                    {"source_id": 0, "start_kp": 1, "end_kp": 2})
        state = {"assignments": rows, "display": {"variable": ["su", "kPa"], "colour_max": 200}}
        plan = {"params_json": json.dumps({"numeric_ground": state})}
        mapped = map_plan(KpMap.shift(2), plan, [], [])
        new = json.loads(mapped["plan"]["params_json"])["numeric_ground"]
        self.assertEqual(new["assignments"][0]["start_kp"], 12)
        self.assertEqual(new["assignments"][0]["src_start_kp"], 10)
        self.assertEqual(new["display"], state["display"])
        self.assertEqual(rows[0]["start_kp"], 10)

    def test_dense_profiles_keep_gaps_and_have_stable_limits(self):
        rows = [["CPT", i / 1000, i % 300] for i in range(100000)]
        profiles = n.import_profiles(rows, {"source_id": 0, "depth": 1, "value": 2}, variable="su", sample_support=.001)
        index = n.ProfileIndex(profiles, [{"source_id": "CPT", "start_kp": 0, "end_kp": 100}], ("su", ""))
        self.assertEqual(index.at(50, 75.123)[0][1]["value"], 123)
        self.assertEqual(index.limits(), (0, 299))

    def test_adjacent_intervals_are_not_overlaps(self):
        rows = [{"source_id": "A", "start_kp": 0, "end_kp": 1},
                {"source_id": "B", "start_kp": 1, "end_kp": 2}]
        self.assertEqual(n.coverage_runs(rows), [(0, 1, (0,)), (1, 2, (1,))])


if __name__ == "__main__":
    unittest.main()
