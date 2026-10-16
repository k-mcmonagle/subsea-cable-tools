"""AGS 3.x / 4.x reader and CPT extraction (pure python, synthetic files)."""
import os
import tempfile
import unittest

from ..geotech import ags, cpt

REQUIRES_QGIS = False

# AGS 3.1 laid out like an offshore seabed CPT delivery: no position, a generic
# HOLE_ID, units in MPa, a single pore pressure in PWP1 and repeated depths.
AGS3 = "\r\n".join([
    '"**PROJ"',
    '"*PROJ_ID","*PROJ_NAME","*PROJ_LOC","*PROJ_CLNT","*PROJ_AGS"',
    '"<UNITS>","","","",""',
    '"","Route","1-A","Client","3.1"',
    "",
    '"**UNIT"',
    '"*UNIT_UNIT","*UNIT_DESC"',
    '"m","Meter"',
    '"MPa","Megapascal"',
    "",
    '"**HOLE"',
    '"*HOLE_ID","*HOLE_LOCZ","*HOLE_LOCX","*HOLE_LOCY","*?HOLE_REMK"',
    '"<UNITS>","m","m","m",""',
    '"0001","","","","long remark split"',
    '"<CONT>","","","",", continued"',
    "",
    '"**STCN"',
    '"*HOLE_ID","*STCN_DPTH","*STCN_RES","*STCN_FRES",',
    '"*STCN_PWP1","*STCN_SLP1"',
    '"<UNITS>","m","MPa","MPa","MPa","deg"',
    '"0001","0.00","0.010","0.000","0.0010","0.0"',
    '"0001","0.01","0.020","0.001","0.0020","0.0"',
    '"0001","0.01","0.030","0.001","0.0030","0.1"',
    '"0001","1.00","0.500","0.010","0.0500","1.0"',
    '"0001","2.00","","0.020","0.0600","1.0"',
    ""])

AGS40 = "\r\n".join([
    '"GROUP","PROJ"', '"HEADING","PROJ_ID","PROJ_NAME"', '"UNIT","",""', '"TYPE","ID","X"',
    '"DATA","P1","Wind farm export"',
    "",
    '"GROUP","TRAN"', '"HEADING","TRAN_AGS","TRAN_RCON"', '"UNIT","",""', '"TYPE","X","X"',
    '"DATA","4.1","+"',
    "",
    '"GROUP","LOCA"',
    '"HEADING","LOCA_ID","LOCA_TYPE","LOCA_NATE","LOCA_NATN","LOCA_GREF","LOCA_GL","LOCA_FDEP",'
    '"LOCA_LAT","LOCA_LON","LOCA_WDEP"',
    '"UNIT","","","m","m","","m","m","","","m"',
    '"TYPE","ID","PA","2DP","2DP","PA","2DP","2DP","DMS","DMS","1DP"',
    '"DATA","CPT01","SCP","500000.00","5800000.00","UTM31","-25.0","4.50","52:20:13.2N","3:12:30.0E","25.4"',
    '"DATA","CPT02","SCP","500100.00","5800050.00","UTM31","","3.00","","",""',
    "",
    '"GROUP","SCPG"',
    '"HEADING","LOCA_ID","SCPG_TESN","SCPG_TYPE","SCPG_CAR"',
    '"UNIT","","","",""', '"TYPE","ID","X","PA","3DP"',
    '"DATA","CPT01","1","PC","0.800"',
    '"DATA","CPT02","1","PC",""',
    '"DATA","CPT02","2","PC",""',
    "",
    '"GROUP","SCPT"',
    '"HEADING","LOCA_ID","SCPG_TESN","SCPT_DPTH","SCPT_RES","SCPT_FRES","SCPT_PWP2","SCPT_QT"',
    '"UNIT","","","m","MN/m2","kN/m2","kN/m2","MPa"',
    '"TYPE","ID","X","2DP","2DP","1DP","1DP","2DP"',
    '"DATA","CPT01","1","0.50","1.00","10.0","50.0","1.01"',
    '"DATA","CPT01","1","1.00","2.00","20.0","60.0","2.02"',
    '"DATA","CPT02","1","0.50","0.50","5.0","20.0",""',
    '"DATA","CPT02","2","3.00","4.00","40.0","90.0",""',
    "",
    '"GROUP","GEOL"', '"HEADING","LOCA_ID","GEOL_TOP","GEOL_BASE","GEOL_DESC","GEOL_LEG"',
    '"UNIT","","m","m","",""', '"TYPE","ID","2DP","2DP","X","PA"',
    '"DATA","CPT01","0.00","2.00","Very soft grey CLAY","201"',
    "",
    '"GROUP","SCPP"', '"HEADING","LOCA_ID","SCPG_TESN","SCPP_TOP","SCPP_BASE","SCPP_REF","SCPP_CSU"',
    '"UNIT","","","m","m","","kPa"', '"TYPE","ID","X","2DP","2DP","X","0DP"',
    '"DATA","CPT01","1","0.00","1.00","Nkt 15","12"',
    ""])

AGS42 = "\n".join([
    '"GROUP","TRAN"', '"HEADING","TRAN_AGS"', '"UNIT",""', '"TYPE","X"', '"DATA","4.2"',
    '"GROUP","LOCA"', '"HEADING","LOCA_ID","LOCA_NATE","LOCA_NATN","LOCA_NSRI"',
    '"UNIT","","m","m",""', '"TYPE","ID","2DP","2DP","X"', '"DATA","S1","400000","6000000","32631"',
    '"GROUP","CPTG"', '"HEADING","LOCA_ID","CPTG_TESN","CPTG_RLOC","CPTG_WAT","CPTG_CAR","CPTG_TERM"',
    '"UNIT","","","","m","",""', '"TYPE","ID","X","PA","2DP","3DP","PA"',
    '"DATA","S1","A","S","-40.00","0.700","MAXIMUM CONE VALUE"',
    '"GROUP","CPTT"', '"HEADING","LOCA_ID","CPTG_TESN","CPTT_REDN","CPTT_DPTH","CPTT_QC","CPTT_FS","CPTT_U2"',
    '"UNIT","","","","m","MPa","MPa","MPa"', '"TYPE","ID","X","0DP","2DP","2DP","3DP","3DP"',
    '"DATA","S1","A","1","0.10","0.20","0.002","0.401"',
    '"DATA","S1","A","2","0.10","0.21","0.002","0.402"',
    '"DATA","S1","A","3","0.20","0.30","0.003","0.403"',
    '"GROUP","CPTP"', '"HEADING","LOCA_ID","CPTP_DPTH","CPTP_BASE","CPTP_SU1","CPTP_SU2"',
    '"UNIT","","m","m","kPa","kPa"', '"TYPE","ID","2DP","2DP","0DP","0DP"',
    '"DATA","S1","0.00","0.20","8","11"',
    ""])


def write(text, name="file"):
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, name)
    with open(path, "w", encoding="cp1252", newline="") as handle:
        handle.write(text)
    return path


class AgsSyntaxTests(unittest.TestCase):
    def test_ags3_groups_units_heading_continuation_cont_and_user_headings(self):
        parsed = ags.parse_ags(AGS3, "CPT-07")
        self.assertEqual(parsed.syntax, ags.AGS3)
        self.assertEqual(parsed.version_label, "AGS 3.1")
        self.assertEqual(list(parsed.groups), ["PROJ", "UNIT", "HOLE", "STCN"])
        stcn = parsed.groups["STCN"]
        self.assertEqual(stcn.headings, ["HOLE_ID", "STCN_DPTH", "STCN_RES", "STCN_FRES", "STCN_PWP1",
                                         "STCN_SLP1"])
        self.assertEqual(stcn.unit("STCN_RES"), "MPa")
        self.assertEqual(len(stcn.rows), 5)
        hole = parsed.groups["HOLE"]
        self.assertIn("HOLE_REMK", hole.headings)          # "?HOLE_REMK" normalised
        self.assertIn("?HOLE_REMK", hole.raw_headings)
        self.assertEqual(hole.rows[0]["HOLE_REMK"], "long remark split, continued")
        self.assertEqual(parsed.groups["UNIT"].rows[1]["UNIT_UNIT"], "MPa")  # no <UNITS> row
        self.assertEqual(parsed.warnings, [])

    def test_ags3_units_line_continued_with_a_trailing_comma(self):
        text = '"**STCN"\n"*HOLE_ID","*STCN_DPTH","*STCN_RES"\n"<UNITS>","m",\n"MPa"\n"A","1","2"\n'
        group = ags.parse_ags(text).groups["STCN"]
        self.assertEqual(group.units, {"STCN_DPTH": "m", "STCN_RES": "MPa"})
        self.assertEqual(group.rows, [{"HOLE_ID": "A", "STCN_DPTH": "1", "STCN_RES": "2"}])

    def test_ags4_descriptors_version_and_repeated_group(self):
        parsed = ags.parse_ags(AGS40 + '"GROUP","GEOL"\n"HEADING","LOCA_ID","GEOL_TOP"\n'
                                       '"UNIT","",""\n"TYPE","ID","2DP"\n"DATA","CPT02","0.50"\n')
        self.assertEqual(parsed.syntax, ags.AGS4)
        self.assertEqual(parsed.version_label, "AGS 4.1")
        self.assertEqual(parsed.groups["SCPT"].types["SCPT_DPTH"], "2DP")
        self.assertEqual([r["LOCA_ID"] for r in parsed.groups["GEOL"].rows], ["CPT01", "CPT02"])
        self.assertTrue(any("appears again" in w for w in parsed.warnings))

    def test_unescaped_quote_is_recovered_from_the_quoted_commas(self):
        text = ('"GROUP","LOCA"\n"HEADING","LOCA_ID","LOCA_LAT","LOCA_LON"\n"UNIT","","",""\n'
                '"TYPE","ID","X","X"\n"DATA","A","51°46\'47.4"","3°10\'00.0""\n')
        parsed = ags.parse_ags(text)
        row = parsed.groups["LOCA"].rows[0]
        self.assertEqual(row["LOCA_LAT"], "51°46'47.4\"")
        self.assertEqual(row["LOCA_LON"], "3°10'00.0\"")
        self.assertTrue(any("unescaped quote" in w for w in parsed.warnings))

    def test_doubled_quotes_and_numbers(self):
        text = ('"GROUP","GEOL"\n"HEADING","LOCA_ID","GEOL_DESC"\n"UNIT","",""\n"TYPE","ID","X"\n'
                '"DATA","A","a 2"" gravel"\n')
        self.assertEqual(ags.parse_ags(text).groups["GEOL"].rows[0]["GEOL_DESC"], 'a 2" gravel')
        self.assertEqual([ags.number(v) for v in ("1.5", "-2e-3", "", "<0.1", "NP", "nan")],
                         [1.5, -0.002, None, None, None, None])

    def test_files_without_extension_and_not_ags(self):
        path = write(AGS3, "CPT-07")
        self.assertTrue(ags.looks_like_ags(path))
        parsed = ags.read_ags(path)
        self.assertEqual((parsed.stem, parsed.encoding), ("CPT-07", "utf-8-sig"))
        self.assertEqual(ags.AgsFile(os.path.join("x", "CPT 1.ags")).stem, "CPT 1")
        other = write("CPT ID,Depth\nA,1\n", "summary.csv")
        self.assertFalse(ags.looks_like_ags(other))
        with self.assertRaisesRegex(ags.AgsError, "not an AGS file"):
            ags.read_ags(other)

    def test_byte_order_mark_and_version_label(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "bom.ags")
        with open(path, "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + AGS42.encode("utf-8"))
        self.assertTrue(ags.looks_like_ags(path))
        self.assertEqual(ags.read_ags(path).version_label, "AGS 4.2")
        labelled = ags.parse_ags(AGS42.replace('"DATA","4.2"', '"DATA","v4.1"'))
        self.assertEqual(labelled.version_label, "AGS 4.1")

    def test_windows_1252_text_is_read(self):
        path = write(AGS3.replace("Route", "Route °N"), "cp")
        with open(path, "rb") as handle:
            self.assertIn(b"\xb0", handle.read())
        parsed = ags.read_ags(path)
        self.assertEqual(parsed.encoding, "cp1252")
        self.assertEqual(parsed.groups["PROJ"].rows[0]["PROJ_NAME"], "Route °N")


class CptExtractionTests(unittest.TestCase):
    def test_ags3_readings_units_and_the_pore_pressure_assumption(self):
        (test,) = cpt.extract_tests(ags.parse_ags(AGS3, os.path.join("d", "CPT-07")))
        self.assertEqual((test["location_id"], test["test_ref"], test["group"]), ("0001", "", "STCN"))
        self.assertEqual(test["depth"], [0.0, 0.01, 0.01, 1.0, 2.0])
        self.assertEqual(test["channels"]["qc"], [0.01, 0.02, 0.03, 0.5, None])
        self.assertEqual(test["channels"]["fs"], [0.0, 1.0, 1.0, 10.0, 20.0])     # MPa -> kPa
        self.assertAlmostEqual(test["channels"]["u2"][3], 50.0)                    # PWP1 read as u2
        self.assertEqual(test["headings"]["u2"], "STCN_PWP1")
        self.assertTrue(any("STCN_PWP1 read as u2" in w for w in test["warnings"]))
        self.assertEqual(test["units"], {"qc": "MPa", "fs": "kPa", "u2": "kPa", "incl": "°"})
        self.assertIsNone(test["location"]["lat"])
        (face,) = cpt.extract_tests(ags.parse_ags(AGS3), ags3_pore="u1")
        self.assertIn("u1", face["channels"])

    def test_ags3_default_units_and_restarted_depths(self):
        text = ('"**STCN"\n"*HOLE_ID","*STCN_DPTH","*STCN_RES","*STCN_FRES"\n'
                '"A","0.5","1.5","20"\n"A","1.0","2.0","30"\n"A","0.2","0.5","5"\n"A","0.4","0.6","6"\n')
        first, second = cpt.extract_tests(ags.parse_ags(text))
        self.assertEqual((first["test_ref"], second["test_ref"]), ("1", "2"))
        self.assertEqual(first["channels"]["fs"], [20.0, 30.0])   # AGS 3 default kN/m2
        self.assertEqual(second["channels"]["qc"], [0.5, 0.6])    # AGS 3 default MN/m2
        self.assertTrue(any("restart" in w for w in second["warnings"]))
        self.assertTrue(any("no unit" in w for w in first["warnings"]))
        self.assertEqual(cpt.assign_names([first, second]), [])
        self.assertEqual([first["name"], second["name"]], ["A #1", "A #2"])

    def test_ags4_locations_tests_units_area_ratio_geology_and_interpretation(self):
        tests = cpt.extract_tests(ags.parse_ags(AGS40, "survey.ags"))
        self.assertEqual([(t["location_id"], t["test_ref"]) for t in tests],
                         [("CPT01", "1"), ("CPT02", "1"), ("CPT02", "2")])
        one = tests[0]
        self.assertEqual(one["channels"]["qc"], [1.0, 2.0])       # MN/m2 = MPa
        self.assertEqual(one["channels"]["fs"], [10.0, 20.0])     # kN/m2 = kPa
        self.assertEqual(one["channels"]["u2"], [50.0, 60.0])
        self.assertEqual(one["channels"]["delivered_qt"], [1.01, 2.02])
        self.assertEqual(one["area_ratio"], 0.8)
        location = one["location"]
        self.assertAlmostEqual(location["lat"], 52 + 20 / 60 + 13.2 / 3600)
        self.assertAlmostEqual(location["lon"], 3 + 12.5 / 60)
        self.assertEqual((location["easting"], location["grid"], location["water_depth"]),
                         (500000.0, "UTM31", 25.4))
        self.assertEqual(one["geology"][0]["description"], "Very soft grey CLAY")
        self.assertEqual(one["interpreted"][0]["su"], 12.0)
        self.assertEqual(tests[1]["interpreted"], [])
        self.assertEqual(cpt.default_name_rule(tests), cpt.NAME_LOCATION)
        cpt.assign_names(tests)
        self.assertEqual([t["name"] for t in tests], ["CPT01", "CPT02 #1", "CPT02 #2"])

    def test_ags42_groups_zero_reference_water_and_epsg(self):
        (test,) = cpt.extract_tests(ags.parse_ags(AGS42, "s1.ags"))
        self.assertEqual((test["group"], test["test_ref"], test["zero_reference"]), ("CPTT", "A", "deck"))
        self.assertEqual(test["depth"], [0.1, 0.1, 0.2])          # repeated depths kept (REDN key)
        self.assertEqual(test["channels"]["fs"], [2.0, 2.0, 3.0])
        self.assertEqual(test["area_ratio"], 0.7)
        self.assertEqual(test["termination"], "MAXIMUM CONE VALUE")
        self.assertEqual(test["location"]["grid_crs"], "EPSG:32631")
        self.assertIsNone(test["location"]["water_depth"])        # deck zero: CPTG_WAT not a depth
        self.assertEqual((test["interpreted"][0]["su"], test["interpreted"][0]["su_low"]), (11.0, 8.0))

    def test_names_by_rule_and_duplicates(self):
        tests = cpt.extract_tests(ags.parse_ags(AGS3, os.path.join("x", "CP-1")))
        tests += cpt.extract_tests(ags.parse_ags(AGS3, os.path.join("y", "CP-2")))
        self.assertEqual(cpt.default_name_rule(tests), cpt.NAME_FILE)
        cpt.assign_names(tests)
        self.assertEqual([t["name"] for t in tests], ["CP-1", "CP-2"])
        notes = cpt.assign_names(tests, cpt.NAME_LOCATION)
        self.assertEqual([t["name"] for t in tests], ["0001", "0001 (2)"])
        self.assertTrue(notes)
        cpt.assign_names(tests, cpt.NAME_FILE_LOCATION)
        self.assertEqual(tests[0]["name"], "CP-1 0001")
        self.assertNotEqual(cpt.cpt_id("c1", "A"), cpt.cpt_id("c2", "A"))
        self.assertEqual(cpt.cpt_id("c1", "A"), cpt.cpt_id("c1", "A"))

    def test_no_cpt_group_and_bad_depth_unit(self):
        self.assertEqual(cpt.extract_tests(ags.parse_ags(AGS3.split('"**STCN"')[0])), [])
        text = '"**STCN"\n"*HOLE_ID","*STCN_DPTH"\n"<UNITS>","furlong"\n"A","1"\n'
        with self.assertRaisesRegex(ags.AgsError, "depth unit"):
            cpt.extract_tests(ags.parse_ags(text))


if __name__ == "__main__":
    unittest.main()
