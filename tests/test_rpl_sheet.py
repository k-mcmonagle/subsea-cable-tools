# -*- coding: utf-8 -*-
"""Tests for workbench/rpl_sheet.py (pure python — no QGIS needed).

Run directly: python tests/test_rpl_sheet.py
"""

import csv
import importlib.util
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "rpl_sheet", os.path.join(_HERE, "..", "workbench", "rpl_sheet.py"))
rpl_sheet = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rpl_sheet)


class _Point:
    def __init__(self, pos_no, event, lat, lon, kp, cable, depth, attrs):
        self.pos_no, self.event, self.lat, self.lon = pos_no, event, lat, lon
        self.dist_cum_km, self.cable_dist_cum_km = kp, cable
        self.depth_m, self.attrs = depth, attrs


class _Seg:
    def __init__(self, bearing, dist, slack, cable, attrs):
        self.bearing_deg, self.dist_km = bearing, dist
        self.slack_pct, self.cable_dist_km, self.attrs = slack, cable, attrs


class _Model:
    pass


def _model():
    model = _Model()
    model.points = [
        _Point(1, "BMH", 51.5, -4.1, 0.0, 0.0, None,
               {"Remarks": "Beach manhole", "SourceFile": "a.xlsx"}),
        _Point(2, "", 51.49, -4.2, 7.1, 7.18, 12.0, {"Remarks": ""}),
        _Point(3, "JT-01", 51.4, -4.4, 21.6, 21.9, 55.5, {"Remarks": "Joint"}),
    ]
    model.segments = [
        _Seg(255.5, 7.1, 1.0, 7.171, {"CableType": "DA", "SourceFile": "a.xlsx"}),
        _Seg(240.0, 14.5, 1.5, 14.7175, {"CableType": "LW"}),
    ]
    return model


class TestBuildSheet(unittest.TestCase):
    def test_alternates_and_ends_on_point(self):
        headers, rows, kinds = rpl_sheet.build_sheet(_model())
        self.assertEqual(kinds, ["point", "leg", "point", "leg", "point"])
        self.assertTrue(all(len(row) == len(headers) for row in rows))

    def test_cumulative_on_points_between_on_legs(self):
        headers, rows, _kinds = rpl_sheet.build_sheet(_model())
        kp = headers.index("KP (km)")
        dist = headers.index("Dist (km)")
        bearing = headers.index("Bearing (deg)")
        pos = headers.index("Pos")
        self.assertEqual(rows[0][kp], "0.000")
        self.assertEqual(rows[0][dist], "")
        self.assertEqual(rows[1][bearing], "255.5")
        self.assertEqual(rows[1][dist], "7.1000")
        self.assertEqual(rows[1][pos], "")
        self.assertEqual(rows[4][kp], "21.600")

    def test_shared_attr_labels_disambiguated(self):
        headers, _rows, _kinds = rpl_sheet.build_sheet(_model())
        self.assertIn("Source file (position)", headers)
        self.assertIn("Source file (leg)", headers)

    def test_empty_model(self):
        model = _Model()
        model.points, model.segments = [], []
        headers, rows, kinds = rpl_sheet.build_sheet(model)
        self.assertTrue(headers)
        self.assertEqual(rows, [])
        self.assertEqual(kinds, [])


class TestWriters(unittest.TestCase):
    def test_csv_round_trip(self):
        headers, rows, _kinds = rpl_sheet.build_sheet(_model())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sheet.csv")
            rpl_sheet.write_csv(path, headers, rows)
            with open(path, encoding="utf-8-sig", newline="") as handle:
                got = list(csv.reader(handle))
        self.assertEqual(got[0], headers)
        self.assertEqual(got[1:], [list(row) for row in rows])

    def test_xlsx_when_openpyxl_present(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed in this interpreter")
        headers, rows, kinds = rpl_sheet.build_sheet(_model())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sheet.xlsx")
            rpl_sheet.write_xlsx(path, headers, rows, kinds,
                                 title="Test/RPL: a very long name over 31 chars")
            book = openpyxl.load_workbook(path)
            sheet = book.active
            self.assertLessEqual(len(sheet.title), 31)
            self.assertNotIn("/", sheet.title)
            kp = headers.index("KP (km)") + 1
            self.assertEqual(sheet.cell(row=2, column=kp).value, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
