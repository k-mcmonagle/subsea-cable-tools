"""DXF annotation wording, anchors, calibration and conservative seabed placement."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..burial import dxf_annotations as a, horizons as h, imported_inputs
from ..burial.kp_rereference import KpMap
from .test_burial_horizons import dxf, line

REQUIRES_QGIS = False


def text_entity(text="Fine sediment", x=1000, y=2, layer="Notes", extra="", kind="TEXT"):
    return f"0\n{kind}\n8\n{layer}\n10\n{x}\n20\n{y}\n1\n{text}\n{extra}"


class AnnotationTests(unittest.TestCase):
    def read(self, text, encoding="utf-8"):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "annotated.dxf"
            path.write_bytes(text.encode(encoding))
            return h.read_dxf(path)

    def test_text_only_and_mixed_drawing(self):
        drawing = self.read(dxf(text_entity()))
        self.assertFalse(drawing["layers"])
        self.assertEqual(drawing["annotations"]["Notes"][0]["text"], "Fine sediment")
        drawing = self.read(dxf(line("H1", 0, 1, 2000, 2) + text_entity()))
        self.assertEqual(set(drawing["layers"]), {"H1"})
        self.assertEqual(set(drawing["annotations"]), {"Notes"})
        self.assertFalse(drawing["warnings"])

    def test_justified_text_uses_alignment_point_and_mtext_uses_insertion(self):
        drawing = self.read(dxf(text_entity(extra="72\n1\n73\n2\n11\n2000\n21\n3\n")
                                + text_entity(kind="MTEXT", extra="71\n9\n11\n0\n21\n1\n")))
        first, second = drawing["annotations"]["Notes"]
        self.assertEqual((first["point"], first["anchor"]), ([2000, 3], [0.5, 0.5]))
        self.assertEqual((second["point"], second["anchor"]), ([1000, 2], [1, 1]))

    def test_mtext_chunks_and_plain_text_preserve_wording_and_symbols(self):
        entity = "0\nMTEXT\n8\nNotes\n10\n1000\n20\n2\n3\n{\\C1;Fine \n1\nsediment}\\P5 %%p 1 \\U+00B0C <sample>\n"
        drawing = self.read(dxf(entity))
        label = drawing["annotations"]["Notes"][0]
        self.assertEqual(label["text"], "Fine sediment\n5 ± 1 °C <sample>")
        self.assertIn("\\C1", label["raw_text"])
        self.assertEqual(a.plain_text(r"Literal \{braces\} and \\ slash", True), "Literal {braces} and \\ slash")
        self.assertEqual(a.plain_text(r"\U+D83D\U+DE00"), "😀")

    def test_legacy_code_page_and_utf8(self):
        content = dxf(text_entity("Sédiment Ø"))
        legacy = content.replace("2\nHEADER\n", "2\nHEADER\n9\n$ACADVER\n1\nAC1015\n9\n$DWGCODEPAGE\n3\nANSI_1252\n")
        self.assertEqual(self.read(legacy, "cp1252")["annotations"]["Notes"][0]["text"], "Sédiment Ø")
        modern = legacy.replace("AC1015", "AC1021")
        self.assertEqual(self.read(modern)["annotations"]["Notes"][0]["text"], "Sédiment Ø")

    def test_unsupported_annotations_are_reported_without_losing_horizons(self):
        entities = (text_entity(extra="72\n3\n") + text_entity(extra="71\n2\n")
                    + text_entity(extra="210\n1\n") + text_entity(extra="30\n1\n")
                    + text_entity("%<field>%") + text_entity(r"\S1/2;", kind="MTEXT")
                    + text_entity(extra="67\n1\n"))
        drawing = self.read(dxf(line("H", 0, 1, 1000, 2) + entities))
        self.assertFalse(drawing["annotations"])
        self.assertEqual(len(drawing["warnings"]), 6)
        self.assertTrue(drawing["layers"])

    def test_10_m_scale_reverse_origins_and_kp_map(self):
        drawing = self.read(dxf(text_entity(x=20, y=3)))
        options = h.ImportOptions(x_scale=10, x_origin=10, kp_origin=2, reverse_x=True,
                                  y_origin=1, depth_offset_m=5, exaggeration=0.1)
        groups, notices = a.build(drawing, ["Notes"], options, KpMap.shift(1))
        label = groups[0]["labels"][0]
        self.assertAlmostEqual(label["kp"], 2.9)
        self.assertEqual(label["depth"], 25)
        self.assertEqual(label["point"], [20, 3])
        self.assertFalse(notices)

    def test_datum_conversion_gaps_endpoints_and_conflicts(self):
        drawing = self.read(dxf("".join(text_entity(x=x, y=-43) for x in (0, 1000, 2000, 3000))))
        options = h.ImportOptions(reference="datum", datum="MSL", positive_up=True)
        groups, notices = a.build(drawing, ["Notes"], options, KpMap.identity(), [[[0, 40], [1, 41]], [[3, 42], [4, 42]]])
        self.assertEqual([(l["kp"], l["depth"]) for l in groups[0]["labels"]], [(0, 3), (1, 2), (3, 1)])
        self.assertIn("1 annotation(s) omitted", notices[0])
        groups, notices = a.build(drawing, ["Notes"], options, KpMap.identity(), [])
        self.assertFalse(groups)
        self.assertTrue(notices)
        with self.assertRaisesRegex(ValueError, "overlap"):
            a.build(drawing, ["Notes"], options, KpMap.identity(), [[[0, 40], [2, 40]], [[1, 41], [3, 41]]])

    def test_cancellation_and_annotation_budgets(self):
        with patch.object(a, "MAX_LABELS", 1):
            drawing = self.read(dxf(text_entity() * 2))
        self.assertEqual(len(drawing["annotations"]["Notes"]), 1)
        self.assertIn("memory budget", drawing["warnings"][0])
        def cancel(*_args):
            raise h.Cancelled()
        with self.assertRaises(h.Cancelled):
            a.build(drawing, ["Notes"], h.ImportOptions(), KpMap.identity(), progress=cancel)
        with self.assertRaisesRegex(ValueError, "text-length"):
            a.plain_text("x" * (a.MAX_TEXT_CHARS + 1))

    def test_optional_storage_and_import_register_grouping(self):
        self.assertEqual(a.plan_state({})["groups"], [])
        drawing = self.read(dxf(text_entity() + line("H", 0, 1, 2000, 2)))
        source = {"file": "example.dxf", "import_id": "example-import"}
        groups, _ = a.build(drawing, ["Notes"], h.ImportOptions(), KpMap.identity(), source=source)
        rows, _ = h.build_horizons(drawing, ["H"], h.ImportOptions(), KpMap.identity(), source=source)
        state = dict(version=1, groups=groups, visible=False)
        plan = {"params_json": json.dumps({a.PARAMS_KEY: state, h.PARAMS_KEY: {"version": 1, "rows": rows}})}
        self.assertEqual(a.plan_state(plan), state)
        self.assertEqual(h.plan_rows(plan), rows)
        entries = imported_inputs.horizon_inputs(rows, annotations=groups)
        self.assertEqual(len(entries), 1)
        self.assertIn("1 annotation(s)", entries[0]["register_detail"])
        self.assertEqual(imported_inputs.horizon_inputs([], annotations=groups)[0]["role"], "DXF annotations")
        for mutation in (dict(kp="1"), dict(depth=float("nan")), dict(anchor=[2, 0])):
            bad = copy.deepcopy(groups)
            bad[0]["labels"][0].update(mutation)
            with self.assertRaises(ValueError):
                a.validate(bad)


if __name__ == "__main__":
    unittest.main()
