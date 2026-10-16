"""CPT derived parameters and the position / summary table join (pure python)."""
import math
import unittest

from ..geotech import cpt, positions

REQUIRES_QGIS = False

TEST = {"depth": [1.0, 2.0], "channels": {"qc": [1.0, 0.3], "fs": [10.0, 9.0], "u2": [50.0, 150.0]}}


class DeriveTests(unittest.TestCase):
    def test_hand_checked_values(self):
        d = cpt.derive(TEST)
        # qt = qc + u2 (1 - a), a = 0.75; Rf = fs / qt
        self.assertAlmostEqual(d["qt"][0], 1.0 + 0.050 * 0.25)
        self.assertAlmostEqual(d["rf"][0], 10.0 / 1012.5 * 100)
        # σv0 = 18 kN/m3 x z, u0 = 10.05 z (zeroed at the seabed)
        self.assertEqual((d["sv0"][1], d["u0"][1]), (36.0, 20.1))
        self.assertAlmostEqual(d["sv0_eff"][1], 15.9)
        self.assertAlmostEqual(d["qnet"][0], 994.5)
        self.assertAlmostEqual(d["bq"][0], (50 - 10.05) / 994.5)
        self.assertAlmostEqual(d["fr"][0], 10 / 994.5 * 100)
        # Fine-grained second reading: Su = qnet / 15 and / 20; coarse first: Dr.
        self.assertEqual((d["zone"][0], d["zone"][1]), (5, 4))
        self.assertIsNone(d["su"][0])
        self.assertAlmostEqual(d["su"][1], 301.5 / 15)
        self.assertAlmostEqual(d["su_low"][1], 301.5 / 20)
        self.assertAlmostEqual(d["dr"][0], 100 * math.sqrt(d["qtn"][0] / 350))
        self.assertIsNone(d["dr"][1])

    def test_ic_satisfies_the_normalisation(self):
        d = cpt.derive(TEST)
        for i in (0, 1):
            ic, qtn, fr = d["ic"][i], d["qtn"][i], d["fr"][i]
            n = min(1.0, 0.381 * ic + 0.05 * d["sv0_eff"][i] / 100 - 0.15)
            self.assertAlmostEqual(qtn, d["qnet"][i] / 100 * (100 / d["sv0_eff"][i]) ** n, places=4)
            self.assertAlmostEqual(ic, math.sqrt((3.47 - math.log10(qtn)) ** 2 + (math.log10(fr) + 1.22) ** 2),
                                   places=6)

    def test_settings_change_the_results(self):
        none = cpt.derive(TEST, {"pore_channel": "none"})
        self.assertEqual(none["qt"], [1.0, 0.3])
        self.assertEqual(none["bq"], [None, None])
        heavier = cpt.derive(TEST, {"unit_weight": 20.0, "nkt": 10.0, "su_fine_only": False})
        self.assertEqual(heavier["sv0"][1], 40.0)
        self.assertAlmostEqual(heavier["su"][0], (1012.5 - 20) / 10)
        estimated = cpt.derive(TEST, {"unit_weight_mode": "estimated"})
        expected = 10.05 * (0.27 * math.log10(estimated["rf"][0]) + 0.36 * math.log10(1012.5 / 100) + 1.236)
        self.assertAlmostEqual(estimated["gamma"][0], min(max(expected, 14.0), 22.0))
        deck = cpt.derive(dict(TEST, zero_reference="deck"), None, water_depth=100.0)
        self.assertAlmostEqual(deck["u0"][0], 10.05 * 101.0)
        self.assertAlmostEqual(deck["sv0"][0], 10.05 * 100.0 + 18.0)
        self.assertEqual(cpt.derive(TEST, {"zeroed_at_seabed": False})["u0"][0], 10.05)  # no water depth
        bad = cpt.settings({"area_ratio": 2, "nkt": -1, "pore_channel": "u9", "unit_weight": "x"})
        self.assertEqual((bad["area_ratio"], bad["nkt"], bad["pore_channel"], bad["unit_weight"]),
                         (0.75, 15.0, "auto", 18.0))

    def test_missing_and_zero_readings_stay_missing(self):
        d = cpt.derive({"depth": [0.0, 0.5, 1.0], "channels": {"qc": [0.01, None, 0.2], "fs": [0.0, 1.0, 0.0]}})
        self.assertIsNone(d["qt"][1])
        self.assertEqual(d["rf"][2], 0.0)
        self.assertIsNone(d["ic"][2])            # Fr = 0: Ic undefined
        self.assertIsNone(d["ic"][0])            # σ'v0 = 0 at the seabed
        self.assertIsNotNone(d["su"][2])         # Ic unknown: Su still given
        self.assertEqual([cpt.ic_zone(v) for v in (1.0, 1.5, 2.3, 2.7, 3.2, 3.9, None)], [7, 6, 5, 4, 3, 2, None])

    def test_no_negative_su_and_strict_epsg_codes(self):
        d = cpt.derive({"depth": [1.0], "channels": {"qc": [0.001], "fs": [0.0]}})
        self.assertLess(d["qnet"][0], 0)
        self.assertEqual((d["su"][0], d["su_low"][0]), (None, None))
        self.assertEqual([cpt._epsg(v) for v in ("32631", "EPSG:32631", "epsg 4326", "ETRS89 / UTM zone 31N",
                                                 "ED50 UTM 31", "")],
                         ["EPSG:32631", "EPSG:32631", "EPSG:4326", "", "", ""])

    def test_depth_bins_average_without_interpolating(self):
        bins = cpt.bin_profile([0.0, 0.01, 0.01, 0.07, 0.31], [1.0, 2.0, None, 4.0, 5.0], 0.05)
        self.assertEqual(bins, [(0.0, 0.05, 1.5), (0.05, 0.1, 4.0), (0.3, 0.35, 5.0)])
        self.assertEqual(cpt.bin_profile([0.0], [None], 0.1), [(0.0, 0.1, None)])
        with self.assertRaises(ValueError):
            cpt.bin_profile([0.0], [1.0], 0)

    def test_fs_resolution_warning(self):
        from ..geotech import ags
        text = '"**STCN"\n"*HOLE_ID","*STCN_DPTH","*STCN_RES","*STCN_FRES"\n"<UNITS>","m","MPa","MPa"\n' + \
            "".join(f'"A","{i / 100:.2f}","0.1","{0.001 if i % 5 == 0 else 0.0:.3f}"\n' for i in range(20))
        (test,) = cpt.extract_tests(ags.parse_ags(text))
        self.assertTrue(any("fs is 0 for 80% of readings (smallest step 1 kPa)" in w for w in test["warnings"]))


class PositionTests(unittest.TestCase):
    def test_coordinates(self):
        for text, axis, value in (("52° 20.220' N", "lat", 52 + 20.22 / 60), ("3° 12.500' E", "lon", 3 + 12.5 / 60),
                                  ("52 20 13.2 S", "lat", -52.337), ("N 52 20.220", "lat", 52.337),
                                  ("52:20.22S", "lat", -52.337), ("-52.337", "lat", -52.337),
                                  ("52:20:13.2", "lat", 52.337), ("3º12.500'E", "lon", 3.208333333),
                                  ("3°10'00.0\"W", "lon", -3 - 10 / 60), ("", "lat", None), ("n/a", "lon", None)):
            if value is None:
                self.assertIsNone(positions.parse_coordinate(text, axis))
            else:
                self.assertAlmostEqual(positions.parse_coordinate(text, axis), value, places=6, msg=text)
        for text, axis in (("52° 20.220' E", "lat"), ("200", "lat"), ("6 61 0 N", "lat"), ("-6 N", "lat"),
                           ("abc", "lon"), ("1 2 3 4", "lon"), ("6.5 30", "lat")):
            with self.assertRaises(ValueError, msg=text):
                positions.parse_coordinate(text, axis)

    def test_numbered_families_and_horizons(self):
        headers = ["CPT ID", "Horizon description 1", "Horizon description 2", "Class 1",
                   "Class 2", "Interface depth (m) 1", "Notes", "Horizon description 3"]
        self.assertEqual(positions.family(headers, 1), [1, 2, 7])
        self.assertEqual(positions.family(headers, 5), [5])
        self.assertEqual(positions.family(headers, 6), [6])
        layers, problems = positions.horizons(["Soft CLAY", "SAND", "Firm CLAY", ""], ["A", "B", "A"],
                                              [1.2, 2.0], 3.0)
        self.assertEqual(problems, [])
        self.assertEqual([(l["top"], l["base"], l["soil_class"]) for l in layers],
                         [(0.0, 1.2, "A"), (1.2, 2.0, "B"), (2.0, 3.0, "A")])
        open_layers, _ = positions.horizons(["A", "B"], [], [1.0], None)
        self.assertIsNone(open_layers[-1]["base"])
        _layers, problems = positions.horizons(["A", "B", "C"], [], [1.0, 0.5], 2.0)
        self.assertIn("must increase", problems[0])
        _layers, problems = positions.horizons(["A", "B"], [], [None], 2.0)
        self.assertIn("no interface depth", problems[0])

    def test_table_with_title_rows_dmm_and_horizons(self):
        grid = [["Project name:", "Demo"], ["Project number:", "1"],
                ["CPT ID", "Date", "Water depth (m)", "Easting (m)", "Northing (m)", "Longitude",
                 "Latitude", "Penetration (m)", "Horizon description 1", "Horizon description 2",
                 "Class 1", "Class 2", "Interface depth (m) 1", "Summary"],
                ["CP-1", "2026-01-15 00:00:00", "35", "", "", "3° 12.500' E", "52° 20.220' N", "2.5",
                 "Soft CLAY", "SAND", "A", "B", "1.2", "Soft over sand"],
                ["CP-2", "", "-150", "", "", "bad", "6 N", "x", "", "", "", "", "", ""],
                ["CP-1", "", "", "", "", "", "", "", "", "", "", "", "", ""]]
        mapping = positions.suggest_mapping(grid[2])
        self.assertEqual(mapping["id"], "CPT ID")
        self.assertEqual((mapping["lat"], mapping["lon"], mapping["water_depth"], mapping["date"]),
                         ("Latitude", "Longitude", "Water depth (m)", "Date"))
        self.assertEqual((mapping["horizon_desc"], mapping["horizon_class"], mapping["horizon_interface"]),
                         ("Horizon description 1", "Class 1", "Interface depth (m) 1"))
        mapping = {k: v for k, v in mapping.items() if k not in ("x", "y")}
        records, problems = positions.read_table(grid, 3, mapping)
        one = records["CP-1"]
        self.assertAlmostEqual(one["lat"], 52.337)
        self.assertEqual((one["water_depth"], one["penetration"], one["summary"]), (35.0, 2.5, "Soft over sand"))
        self.assertEqual([(l["top"], l["base"]) for l in one["layers"]], [(0.0, 1.2), (1.2, 2.5)])
        self.assertEqual(records["CP-2"]["water_depth"], 150.0)   # sign of a depth ignored
        self.assertIsNone(records["CP-2"]["lat"])
        self.assertTrue(any("CP-2" in p and "position not read" in p for p in problems))
        self.assertTrue(any("penetration 'x'" in p for p in problems))
        self.assertTrue(any("appears again" in p for p in problems))
        with self.assertRaisesRegex(ValueError, "no column named 'Nope'"):
            positions.read_table(grid, 3, {"id": "Nope"})
        with self.assertRaisesRegex(ValueError, "2 columns named 'Depth'"):
            positions.read_table([["ID", "Depth", "Depth"], ["A", "1", "2"]], 1,
                                 {"id": "ID", "water_depth": "Depth"})
        with self.assertRaisesRegex(ValueError, "both coordinate columns"):
            positions.read_table(grid, 3, {"id": "CPT ID", "lat": "Latitude"})

    def test_name_matching(self):
        matches, unmatched, unused = positions.match_names(
            ["CP-1", "cp 2", "CP3", "X"], ["CP-1", "CP-2", "CP 3", "Cp3", "Z"])
        self.assertEqual(matches, {"CP-1": ("CP-1", "exact"), "cp 2": ("CP-2", "loose")})
        self.assertEqual(unmatched, ["CP3", "X"])          # CP3 is ambiguous (CP 3 / Cp3)
        self.assertEqual(unused, ["CP 3", "Cp3", "Z"])
        exact_only, _u, _k = positions.match_names(["cp 2"], ["CP-2"], loose=False)
        self.assertEqual(exact_only, {})


if __name__ == "__main__":
    unittest.main()
