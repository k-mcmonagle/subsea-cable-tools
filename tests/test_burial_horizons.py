"""Pure checks for DXF geometry, coordinate calibration and gap-safe horizons."""

import copy
import json
import math
import hashlib
from unittest.mock import PropertyMock, patch
import tempfile
import unittest
from pathlib import Path

from ..burial import horizons as h, imported_inputs
from ..burial.kp_rereference import KpMap
from ..burial.profile_data import PlanProfile

REQUIRES_QGIS = False


def dxf(entities, units=6):
    return (f"0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n{units}\n0\nENDSEC\n"
            f"0\nSECTION\n2\nENTITIES\n{entities}0\nENDSEC\n0\nEOF\n")


def line(layer, x0, y0, x1, y1, extra=""):
    return f"0\nLINE\n8\n{layer}\n10\n{x0}\n20\n{y0}\n11\n{x1}\n21\n{y1}\n{extra}"


class HorizonsTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "section.dxf"
            path.write_text(text, encoding="utf-8")
            return h.read_dxf(str(path))

    def test_entities_and_gaps_are_preserved(self):
        drawing = self.read(dxf(line("H1", 0, 1, 1000, 2)
                                + line("H1", 2000, 3, 3000, 4)
                                + line("H1", 0, 10, 1000, 11)))
        self.assertEqual(drawing["units"], 6)
        rows, warnings = h.build_horizons(drawing, ["H1"], h.ImportOptions(), KpMap.identity())
        self.assertFalse(warnings)
        self.assertEqual(rows[0]["segments"], [[[0, 1], [1, 2]], [[2, 3], [3, 4]], [[0, 10], [1, 11]]])
        x, _ = h.plot_arrays(rows[0]["segments"])
        self.assertEqual(sum(math.isnan(v) for v in x), 3)

    def test_source_vertex_order_is_retained(self):
        drawing = self.read(dxf(line("H1", 1000, 2, 0, 1)))
        rows, _ = h.build_horizons(drawing, ["H1"], h.ImportOptions(), KpMap.identity())
        self.assertEqual(rows[0]["source_segments"][0]["points"], [[1000, 2], [0, 1]])
        self.assertEqual(rows[0]["segments"], [[[0, 1], [1, 2]]])

    def test_reference_densification_stops_at_vertex_budget(self):
        previous = h.MAX_POINTS
        h.MAX_POINTS = 3
        try:
            with self.assertRaisesRegex(ValueError, "too many vertices"):
                h.subtract_seabed([[[0, 5], [3, 5]]], [[[0, 1], [1, 2], [2, 1], [3, 2]]])
        finally:
            h.MAX_POINTS = previous

    def test_polylines_and_skipped_geometry(self):
        lw = "0\nLWPOLYLINE\n8\nH1\n90\n3\n10\n0\n20\n1\n10\n1\n20\n2\n10\n2\n20\n3\n"
        poly = ("0\nPOLYLINE\n8\nH2\n70\n0\n0\nVERTEX\n10\n0\n20\n4\n"
                "0\nVERTEX\n10\n2\n20\n5\n0\nSEQEND\n")
        drawing = self.read(dxf(lw + poly + lw.replace("8\nH1", "8\nCURVE") + "42\n0.5\n"
                                + line("3D", 0, 0, 1, 1, "30\n5\n")
                                + line("PAPER", 0, 0, 1, 1, "67\n1\n")
                                + "0\nINSERT\n8\nBLOCK\n2\nB1\n"))
        self.assertEqual(set(drawing["layers"]), {"H1", "H2"})
        self.assertEqual(len(drawing["warnings"]), 3)
        self.assertEqual(drawing["layers"]["H2"][0]["points"], [[0, 4], [2, 5]])

    def test_invalid_geometry_is_reported(self):
        for extra in ("210\n1\n", "30\nNaN\n"):
            with self.assertRaises(ValueError):
                self.read(dxf(line("bad", 0, 0, 1, 1, extra)))
        with self.assertRaisesRegex(ValueError, "Binary DXF"):
            self.read("AutoCAD Binary DXF\r\n\x1a\x00")
        with self.assertRaisesRegex(ValueError, "group-code pair"):
            self.read("0\nSECTION\n2\n")
        with self.assertRaisesRegex(ValueError, "No supported"):
            self.read(dxf(line("vertical", 0, 0, 0, 5)))

    def test_calibration_units_origins_sign_and_exaggeration(self):
        options = h.ImportOptions(x_unit_m=1000, x_origin=200, kp_origin=10, reverse_x=True,
                                  y_unit_m=0.001, y_origin=500, depth_offset_m=2,
                                  exaggeration=10, positive_up=True)
        self.assertEqual(options.transform([[200, -9500], [202, -19500]]), [[8, 4], [10, 3]])
        self.assertEqual(h.ImportOptions(x_unit_m=0.3048).transform([[0, 0], [1000, 2]])[-1][0], 0.3048)
        self.assertEqual(h.ImportOptions(x_scale=2).transform([[0, 0], [1000, 2]])[-1][0], 2)

    def test_folds_and_zero_scale_rejected(self):
        self.assertEqual(h.checked_segment([[2, 3], [1, 2], [1, 2], [0, 1]]), [[0, 1], [1, 2], [2, 3]])
        for points in ([[0, 0], [0, 1]], [[0, 0], [2, 1], [1, 2]], [[0, 0], [math.nan, 1]]):
            with self.assertRaises(ValueError):
                h.checked_segment(points)
        with self.assertRaises(ValueError):
            h.ImportOptions(exaggeration=0).transform([[0, 0], [1, 1]])
        with self.assertRaises(ValueError):
            h.ImportOptions(reference="datum").transform([[0, 0], [1, 1]])

    def test_kp_map_breakpoints_preserve_linear_interpretation(self):
        mapping = KpMap.from_anchors([(0, 0), (1, 2), (2, 3)])
        self.assertEqual(h.remap_segment([[0, 0], [2, 4]], mapping), [[0, 0], [2, 2], [3, 4]])

    def test_many_entities_only_use_anchors_inside_each_segment(self):
        mapping = KpMap.from_anchors([(i, i * 2) for i in range(1000)])
        drawing = self.read(dxf(line("H", 100500, 1, 102500, 5) + line("H", 200500, 2, 201500, 4)))
        with patch.object(KpMap, "anchors", new_callable=PropertyMock,
                          return_value=mapping.anchors) as anchors:
            rows, _ = h.build_horizons(drawing, ["H"], h.ImportOptions(), mapping)
            self.assertEqual(anchors.call_count, 2)  # one index, one saved KP-map record
        self.assertEqual(rows[0]["segments"], [[[201, 1], [202, 2], [204, 4], [205, 5]],
                                              [[401, 2], [402, 3], [403, 4]]])

    def test_datum_conversion_follows_seabed_bends_and_gaps(self):
        horizon = [[[0, 43], [10, 43]]]
        seabed = [[[1, 40], [2, 39], [3, 40]], [[7, 40], [8, 41]]]
        self.assertEqual(h.subtract_seabed(horizon, seabed),
                         [[[1, 3], [2, 4], [3, 3]], [[7, 3], [8, 2]]])
        self.assertEqual(h.subtract_seabed([[[20, 43], [21, 43]]], seabed), [])
        # Above-seabed picks stay negative so a datum/sign problem is visible.
        self.assertEqual(h.subtract_seabed([[[1, 38], [2, 38]]], seabed), [[[1, -2], [2, -1]]])

    def test_overlapping_seabed_is_ambiguous(self):
        for ref in ([[[0, 0], [2, 0]], [[1, 0], [3, 0]]],
                    [[[0, 0], [1, 0]], [[1, 1], [2, 1]]], []):
            with self.assertRaises(ValueError):
                h.subtract_seabed([[[0, 5], [4, 5]]], ref)

    def test_profile_nodata_and_source_seams_are_not_bridged(self):
        profile = PlanProfile(kps=list(range(9)), depths=[40, 40, None, 40, 40, 40, 40, 40, 40],
                              source_ids=["a"] * 5 + ["b"] * 4)
        self.assertEqual(h.reference_segments(profile),
                         [[[0, 40], [1, 40]], [[3, 40], [4, 40]], [[5, 40], [6, 40], [7, 40], [8, 40]]])

    def test_conversion_warnings_and_source_survive(self):
        drawing = self.read(dxf(line("H1", 0, -430, 10000, -430)))
        options = h.ImportOptions(reference="datum", datum="MSL", positive_up=True, exaggeration=10)
        rows, warnings = h.build_horizons(drawing, ["H1"], options, KpMap.identity(),
                                          [[[2, 40], [8, 40]]], {"file": "original.dxf"})
        self.assertEqual(rows[0]["segments"], [[[2, 3], [8, 3]]])
        self.assertIn("missing seabed", warnings[0])
        self.assertEqual(rows[0]["source_segments"][0]["points"][0][1], -430)
        self.assertEqual(rows[0]["source"]["options"]["datum"], "MSL")
        params = {"other_setting": {"keep": 7}, h.PARAMS_KEY: {"version": 1, "rows": rows}}
        plan = {"params_json": json.dumps(params)}
        original = copy.deepcopy(plan)
        self.assertEqual(h.plan_rows(plan), rows)
        self.assertEqual(plan, original)

    def test_large_polyline_keeps_every_vertex_and_progress_and_fingerprint(self):
        count = 60_001
        coords = "".join(f"10\n{i}\n20\n{1 + i % 7}\n" for i in range(count))
        text = dxf(f"0\nLWPOLYLINE\n8\nLarge\n90\n{count}\n" + coords)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "large.dxf"
            path.write_bytes(text.encode("utf-8"))
            updates = []
            drawing = h.read_dxf(path, lambda p, m: updates.append(p) if p is not None else None)
            self.assertEqual(drawing["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(drawing["vertex_count"], count)
        self.assertGreater(len(updates), 10)
        self.assertEqual(updates, sorted(updates))
        self.assertEqual(updates[-1], 100)
        rows, _ = h.build_horizons(drawing, ["Large"], h.ImportOptions(), KpMap.shift(2))
        self.assertEqual(len(rows[0]["segments"][0]), count)
        self.assertEqual(rows[0]["segments"][0][-1], [62.0, 1 + (count - 1) % 7])
        self.assertEqual(len(rows[0]["source_segments"][0]["points"]), count)
        self.assertEqual(h.plan_rows({"params_json": json.dumps({h.PARAMS_KEY: {"version": 1, "rows": rows}})}), rows)

    def test_cancel_inside_large_entity_and_conversion_leaves_no_result(self):
        coords = "".join(f"10\n{i}\n20\n1\n" for i in range(10_000))
        text = dxf("0\nLWPOLYLINE\n8\nH\n90\n10000\n" + coords)
        calls = []
        def cancel(percent, message):
            calls.append(percent)
            if len(calls) == 3:
                raise h.Cancelled()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cancel.dxf"
            path.write_text(text)
            with self.assertRaises(h.Cancelled):
                h.read_dxf(path, cancel)
            drawing = h.read_dxf(path)
        calls.clear()
        original = copy.deepcopy(drawing)
        with self.assertRaises(h.Cancelled):
            h.build_horizons(drawing, ["H"], h.ImportOptions(), KpMap.identity(), progress=cancel)
        self.assertEqual(drawing, original)

    def test_budgets_reject_before_unbounded_expansion(self):
        with patch.object(h, "MAX_POINTS", 4):
            with self.assertRaisesRegex(ValueError, "memory budget"):
                self.read(dxf(line("H", 0, 0, 1, 1) * 3))
            with self.assertRaisesRegex(ValueError, "memory budget"):
                self.read(dxf("0\nPOLYLINE\n8\nH\n" + "0\nVERTEX\n10\n0\n20\n1\n" * 5))
        with self.assertRaisesRegex(ValueError, "excessively long"):
            self.read("0\n" + "x" * 65537 + "\n")
        drawing = self.read(dxf(line("H", 0, 0, 1000, 1)))
        rows, _ = h.build_horizons(drawing, ["H"], h.ImportOptions(), KpMap.identity())
        more = copy.deepcopy(rows[0])
        more["id"] = "different"
        with patch.object(h, "MAX_POINTS", 3):
            with self.assertRaisesRegex(ValueError, "plan memory budget"):
                h.validate_rows(rows + [more])
            with self.assertRaisesRegex(ValueError, "memory budget"):
                h.build_horizons(drawing, ["H", "H"], h.ImportOptions(), KpMap.identity())

    def test_input_register_older_imports_and_superseded_campaigns(self):
        drawing = self.read(dxf(line("H1", 0, 1, 1000, 2) + line("H2", 0, 3, 1000, 4)))
        rows, _ = h.build_horizons(drawing, ["H1", "H2"], h.ImportOptions(), KpMap.identity(),
                                  source={"file": "example.dxf", "imported_utc": "example date"})
        entries = imported_inputs.horizon_inputs(rows)
        self.assertEqual(len(entries), 1)
        self.assertIn("2 horizon(s)", entries[0]["register_detail"])
        self.assertEqual(entries[0]["register_state"], "stored")
        self.assertNotIn("input_id", entries[0])
        self.assertEqual(imported_inputs.horizon_inputs(rows, lambda row: "Route changed")[0]["register_state"], "check")
        campaigns = [dict(campaign_id="old", name="Survey", status="superseded", config_json="bad")]
        entries = imported_inputs.cpt_inputs(campaigns, [{"campaign_id": "old"}])
        self.assertEqual(entries[0]["register_users"], [])
        self.assertEqual(entries[0]["register_state"], "superseded")
        self.assertIn("1 CPT(s)", entries[0]["register_detail"])

    def test_optional_state_and_future_formats(self):
        self.assertEqual(h.plan_rows({"params_json": '{"min_section_km":0.1}'}), [])
        self.assertEqual(h.plan_rows({}), [])
        for payload in ("bad", "[]", json.dumps({h.PARAMS_KEY: {"version": 2, "rows": []}})):
            with self.assertRaises(ValueError):
                h.plan_rows({"params_json": payload})

    def test_saved_coordinate_strings_are_rejected_before_plotting(self):
        drawing = self.read(dxf(line("H", 0, 1, 1000, 2)))
        rows, _ = h.build_horizons(drawing, ["H"], h.ImportOptions(), KpMap.identity())
        rows[0]["segments"][0][0][0] = "0"
        with self.assertRaisesRegex(ValueError, "numeric pairs"):
            h.validate_rows(rows)

    def test_vertex_editor_keeps_segments_and_validates(self):
        self.assertEqual(h.parse_vertices("0, 1\n1, 2\n\n3, 2\n4, 1"),
                         [[[0, 1], [1, 2]], [[3, 2], [4, 1]]])
        for text in ("0,1\n0,2", "0,1,2", "", "1,2\n\n3,4", "0,nan\n1,2"):
            with self.assertRaises(ValueError):
                h.parse_vertices(text)


if __name__ == "__main__":
    unittest.main()
