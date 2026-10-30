"""Total/sole route coverage: overlaps, clipping and criterion semantics."""

import random
import unittest

from ..burial import coverage, risk, schema
from ..workbench.rules_engine import Interval

REQUIRES_QGIS = False


class CoverageTests(unittest.TestCase):
    def test_overlaps_duplicates_and_nonzero_scope(self):
        result = coverage.route_coverage({
            "a": [(8, 14), (11, 14), (18, 22)],
            "b": [(12, 16)], "empty": [],
        }, Interval(10, 20))
        self.assertEqual(result["a"], coverage.Coverage(6, 4))
        self.assertEqual(result["b"], coverage.Coverage(4, 2))
        self.assertEqual(result["empty"], coverage.Coverage())
        self.assertEqual(coverage.length_label(result["a"].unique_km, 10), "4.000 km · 40.0%")

    def test_identical_nested_and_touching_ranges(self):
        result = coverage.route_coverage({
            "a": [(0, 5)], "b": [(0, 5)], "c": [(1, 4)], "d": [(5, 8)],
        }, Interval(0, 10))
        for key in ("a", "b", "c"):
            self.assertEqual(result[key].unique_km, 0)
        self.assertEqual(result["d"], coverage.Coverage(3, 3))

    def test_empty_zero_reversed_and_outside_ranges(self):
        spans = {"a": [(6, 2), (3, 3), (20, 30)]}
        self.assertEqual(coverage.route_coverage(spans, Interval(0, 10))["a"],
                         coverage.Coverage(4, 4))
        self.assertEqual(coverage.route_coverage(spans, Interval(5, 5))["a"], coverage.Coverage())
        self.assertEqual(coverage.route_coverage({}, Interval(0, 10)), {})

    def test_allow_exceptions_and_sliver_extensions_are_not_sole_coverage(self):
        spans = {"a": [(2, 6)], "b": [(5, 8)]}
        result = coverage.route_coverage(spans, Interval(0, 10), unique_within=[(3, 9)])
        self.assertEqual(result["a"], coverage.Coverage(4, 2))
        self.assertEqual(result["b"], coverage.Coverage(3, 2))
        cleared = coverage.route_coverage(spans, Interval(0, 10), unique_within=[])
        self.assertEqual(cleared["a"], coverage.Coverage(4, 0))

    def test_screening_coverage_allow_and_method_filters(self):
        self.assertTrue(coverage.is_exclusion({"criterion_class": "project", "action": "exclude"}))
        self.assertTrue(coverage.is_exclusion({"criterion_class": "non_deviable"}))
        for rule in ({"criterion_class": "screening", "action": "exclude"},
                     {"kind": schema.RULE_KIND_COVERAGE}, {"action": "risk"}, {"action": "allow"}):
            self.assertFalse(coverage.is_exclusion(rule))
        self.assertTrue(coverage.applies_to_method({"methods_json": '["jet"]'}, schema.METHOD_TRENCHER))
        self.assertFalse(coverage.applies_to_method({"methods_json": '["jet"]'}, "plough"))
        self.assertTrue(coverage.applies_to_method({}, "plough"))

    def test_hazard_display_extents_clipped_at_scope_boundary(self):
        spans = risk.hazard_spans([{"kp": 10, "end_kp": None}, {"kp": 20, "end_kp": None}])
        result = coverage.route_coverage({"a": [(s, e) for s, e, _ in spans]}, Interval(10, 20))
        self.assertAlmostEqual(result["a"].total_km, 0.05)
        self.assertAlmostEqual(result["a"].unique_km, 0.05)

    def test_sweep_matches_independent_unit_segment_oracle(self):
        rng = random.Random(128)
        for _ in range(100):
            ranges = {str(i): [(rng.randrange(-5, 25), rng.randrange(-5, 25))
                               for _ in range(rng.randrange(6))] for i in range(8)}
            actual = coverage.route_coverage(ranges, Interval(0, 20))
            expected = {key: coverage.Coverage() for key in ranges}
            for start in range(20):
                owners = {key for key, spans in ranges.items()
                          if any(min(a, b) <= start and max(a, b) >= start + 1 for a, b in spans)}
                for key in owners:
                    expected[key].total_km += 1
                if len(owners) == 1:
                    expected[next(iter(owners))].unique_km += 1
            self.assertEqual(actual, expected)
