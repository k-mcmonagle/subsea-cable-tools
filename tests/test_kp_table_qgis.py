"""KP-range tables in Exclusions and the Risk Profile, with RPL references."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,
                       QgsProject, QgsVectorLayer)
from qgis.PyQt.QtWidgets import QApplication, QFormLayout, QWidget

from ..burial import analysis_task, generation, kp_table, risk_scan
from ..burial.kp_rereference import KpMap
from ..burial.plan_model import PlanModel
from ..burial.rpl_reference import table_kp_map
from ..burial.store import BurialStore
from ..burial.tabs.inputs_tab import InputsTab
from ..burial.tabs.kp_table_form import KpTableForm
from ..burial.tabs.risk_tab import CheckEditorDialog
from ..burial.tabs.rules_tab import RuleEditorDialog
from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..workbench.depth_service import DepthSourceConfig
from ..workbench.rules_engine import Interval

REQUIRES_QGIS = True

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
REF = {kp_table.KP_REF_KEY: "rpl-a", kp_table.KP_REF_LABEL_KEY: "Route — Rev A"}


def shifted(delta, notes=("KPs translated from Route — Rev A",)):
    return lambda config: (KpMap.shift(delta).map_range, list(notes))


class KpTableQgisTests(unittest.TestCase):
    def setUp(self):
        self.project = QgsProject.instance()
        self.da = make_distance_area(WGS84, self.project.transformContext())
        self.route = RouteFrame.from_source(
            [QgsGeometry.fromWkt("LINESTRING(0 50, 0 50.2)")], self.da)  # ~22 km
        self.table = QgsVectorLayer(
            "None?field=KP_From:double&field=KP_To:double&field=hazard:string",
            "Desk study hazards", "memory")
        for start, end, name in ((2.0, 3.0, "Boulders"), (5.0, 6.0, "Wreck"),
                                 (None, 7.0, "Unlocated")):
            feat = QgsFeature(self.table.fields())
            feat.setAttributes([start, end, name])
            self.table.dataProvider().addFeature(feat)
        self.project.addMapLayer(self.table)
        self.inputs = [{"input_id": "in-t", "plan_id": "p1", "role": "other",
                        "layer_name": self.table.name(),
                        "layer_source": self.table.source(),
                        "layer_id_hint": self.table.id(), "config_json": "{}"}]

    def tearDown(self):
        self.project.removeMapLayer(self.table.id())
        QApplication.processEvents()

    # -- Exclusions ---------------------------------------------------------
    def rule(self, **config):
        base = {"input_id": "in-t", "start_field": "KP_From", "end_field": "KP_To"}
        base.update(config)
        return {"rule_id": "kp", "plan_id": "p1", "seq": 0, "name": "Desk study",
                "enabled": 1, "kind": "kp_range_table", "action": "exclude",
                "risk_level": 0, "criterion_class": "project", "source_ref": "DS",
                "methods_json": "[]", "config_json": json.dumps(base), "notes": ""}

    def build(self, rule, cache=None, kp_reference=None):
        params = generation.GenParams(0.0, 20.0, direction=1, method="plough",
                                      min_section_km=0.5, coarse_step_m=100.0)
        plan = {"plan_id": "p1", "scope_start_kp": 0.0, "scope_end_kp": 20.0}
        return analysis_task.build_work(
            self.route, self.da, plan, [rule], self.inputs, DepthSourceConfig({}),
            params, {} if cache is None else cache, "rpl-fp", self.project,
            kp_reference=kp_reference)

    def test_exclusion_ranges_are_translated_and_logged(self):
        work, warnings = self.build(self.rule(**REF), kp_reference=shifted(0.5))
        self.assertEqual(warnings, [])
        rule_work = work.rules[0]
        self.assertEqual(rule_work.table_ranges, [(2.5, 3.5), (5.5, 6.5)])
        notes = " ".join(rule_work.notes)
        self.assertIn("KPs translated from Route — Rev A", notes)
        self.assertIn("1 of 3 row(s) have an empty or non-numeric", notes)
        task = analysis_task.BurialAnalysisTask(work, lambda t: None)
        self.assertTrue(task.run(), task.error)
        result = task.results[0]
        self.assertFalse(result.error)
        self.assertEqual([(round(i.start_km, 6), round(i.end_km, 6))
                          for i in result.footprint], [(2.5, 3.5), (5.5, 6.5)])
        self.assertEqual(result.notes, rule_work.notes)

    def test_cache_follows_translation(self):
        cache = {}
        work, _ = self.build(self.rule(**REF), cache, shifted(0.5))
        task = analysis_task.BurialAnalysisTask(work, lambda t: None)
        self.assertTrue(task.run())
        cache[task.results[0].cache_key] = (task.results[0].footprint, [])
        again, _ = self.build(self.rule(**REF), cache, shifted(0.5))
        self.assertIsNotNone(again.rules[0].cached)
        moved, _ = self.build(self.rule(**REF), cache, shifted(0.7))
        self.assertIsNone(moved.rules[0].cached)

    def test_wrong_fields_explain_why_nothing_fires(self):
        work, _ = self.build(self.rule(start_field="start_kp", end_field="end_kp"))
        self.assertEqual(work.rules[0].table_ranges, [])
        self.assertIn("lack the fields 'start_kp'/'end_kp'", work.rules[0].notes[0])

    def test_unusable_reference_skips_the_rule(self):
        def broken(_config):
            raise ValueError("Route — Rev A is no longer in the Workbench")
        work, warnings = self.build(self.rule(**REF), kp_reference=broken)
        self.assertIn("KP reference RPL", work.rules[0].error)
        self.assertTrue(any("no longer in the Workbench" in w for w in warnings))

    # -- Risk Profile -------------------------------------------------------
    def check(self):
        config = {"kind": risk_scan.CHECK_KIND_KP_TABLE, "input_id": "in-t",
                  "start_field": "KP_From", "end_field": "KP_To",
                  "label_attribute": "hazard", "attribute": "hazard",
                  "attribute_rules": [{"match": "Wreck", "risk": "high"}],
                  "default_risk": "medium", **REF}
        return {"check_id": "c1", "name": "Desk study", "config_json": json.dumps(config)}

    def test_risk_hazards_from_table_rows(self):
        entries, notes = risk_scan.snapshot_kp_table(
            self.check(), self.table, shifted(0.5))
        self.assertEqual(len(entries), 2)
        self.assertTrue(any("1 of 3 row(s)" in n for n in notes))
        hazards, warnings = risk_scan.scan_kp_table(
            "p1", self.check(), entries, self.route, scope=Interval(0.0, 6.0))
        self.assertEqual(warnings, [])
        by_label = {h["label"]: h for h in hazards}
        self.assertEqual(by_label["Boulders"]["risk"], "medium")
        self.assertEqual((by_label["Boulders"]["kp"], by_label["Boulders"]["end_kp"]),
                         (2.5, 3.5))
        wreck = by_label["Wreck"]
        self.assertEqual(wreck["risk"], "high")
        self.assertEqual((wreck["kp"], wreck["end_kp"]), (5.5, 6.0))  # clipped to scope
        self.assertTrue(wreck["feature_ref"].startswith("row:"))
        quoted = json.loads(wreck["attributes_json"])["Quoted KP"]
        self.assertEqual(quoted, "KP 5.000–6.000 on Route — Rev A")
        self.assertIsNotNone(wreck["lat"])

    def test_risk_scan_task_runs_table_jobs(self):
        entries, _ = risk_scan.snapshot_kp_table(self.check(), self.table, None)
        done = []
        task = risk_scan.RiskScanTask(
            "p1", [(self.check(), entries)], [QgsGeometry(g) for g in self.route.geometries],
            self.project.transformContext(), None, 1, done.append)
        self.assertTrue(task.run(), task.error)
        self.assertEqual(sorted(h["kp"] for h in task.hazards), [2.0, 5.0])

    def test_check_dialog_round_trips_table_config(self):
        dialog = CheckEditorDialog(self.check(), self.inputs)
        self.assertTrue(dialog.kp_group.isVisibleTo(dialog))
        self.assertFalse(dialog.bands_group.isVisibleTo(dialog))
        self.assertFalse(dialog.distance_spin.isEnabled())
        out = json.loads(dialog.result_check()["config_json"])
        self.assertEqual(out["kind"], risk_scan.CHECK_KIND_KP_TABLE)
        self.assertEqual((out["start_field"], out["end_field"]), ("KP_From", "KP_To"))
        self.assertEqual(out[kp_table.KP_REF_KEY], "rpl-a")  # kept without a model
        self.assertEqual(out[kp_table.KP_UNIT_KEY], "km")
        dialog.deleteLater()

    def test_rule_dialog_validates_fields(self):
        dialog = RuleEditorDialog(self.rule(start_field="start_kp"), self.inputs, "plough")
        problems = dialog.kp_table_form.problems(self.table)
        self.assertTrue(problems and "'start_kp'" in problems[0])
        dialog.kp_table_form.start_field.setText("KP_From")
        dialog.kp_table_form.unit_combo.setCurrentIndex(
            dialog.kp_table_form.unit_combo.findData("m"))
        self.assertEqual(dialog.kp_table_form.problems(self.table), [])
        out = json.loads(dialog.result_rule()["config_json"])
        self.assertEqual(out[kp_table.KP_UNIT_KEY], "m")
        dialog.deleteLater()


class TableReferenceTests(unittest.TestCase):
    def model(self, start=10.0):
        return SimpleNamespace(resolved_rpl_id="rpl-1", plan={"rpl_name": "Route"},
                               route=SimpleNamespace(start_kp_km=start))

    def test_plan_rpl_is_identity_and_legacy_is_flagged(self):
        map_range, notes = table_kp_map(self.model(), {kp_table.KP_REF_KEY: "rpl-1"})
        self.assertIsNone(map_range)
        self.assertEqual(notes, [])
        map_range, notes = table_kp_map(self.model(), {})
        self.assertIsNone(map_range)
        self.assertIn("not recorded", notes[0])

    def test_renumbered_start_kp_shifts_quoted_kps(self):
        config = {kp_table.KP_REF_KEY: "rpl-1", kp_table.KP_REF_LABEL_KEY: "Route",
                  kp_table.KP_REF_START_KEY: 9.5}
        map_range, notes = table_kp_map(self.model(10.0), config)
        start, end, _flags = map_range(12.0, 13.0)
        self.assertAlmostEqual(start, 12.5)
        self.assertAlmostEqual(end, 13.5)
        self.assertIn("shifted by +500.0 m", notes[0])


class PlanFormReferenceTests(unittest.TestCase):
    """The form records the plan route when it is not a registered RPL."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BurialStore(str(Path(self.temp.name) / "plans.gpkg"))
        self.store.migrate()
        self.model = PlanModel(self.store)
        self.model.create_plan("Table refs", "plough")
        self.tab = InputsTab(self.model, lambda: None)
        self.line = QgsVectorLayer("LineString?crs=EPSG:4326", "Plain line", "memory")
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromWkt("LINESTRING(0 0,0.01 0)"))
        self.line.dataProvider().addFeatures([feature])
        QgsProject.instance().addMapLayer(self.line)
        self.tab.route_layer_radio.setChecked(True)
        self.tab.fallback_combo.setLayer(self.line)
        self.tab.apply_rpl_button.click()

    def tearDown(self):
        self.tab.close()
        self.tab.deleteLater()
        self.model.close_plan()
        QgsProject.instance().removeMapLayer(self.line.id())
        QApplication.processEvents()
        self.store.close()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def test_unregistered_plan_route_reference(self):
        self.assertIsNotNone(self.model.route)
        holder = QWidget()
        form = KpTableForm(QFormLayout(holder), {}, self.model)
        self.assertIsNotNone(form.picker)
        config = {}
        form.apply(config)
        self.assertEqual(config[kp_table.KP_REF_KEY], "")
        self.assertIn("not a registered RPL", config[kp_table.KP_REF_LABEL_KEY])
        self.assertEqual(config[kp_table.KP_REF_START_KEY], 0.0)
        holder.deleteLater()

    def test_resave_keeps_start_kp_recorded_at_reference(self):
        holder = QWidget()
        stored = {kp_table.KP_REF_KEY: "", kp_table.KP_REF_START_KEY: 0.25}
        form = KpTableForm(QFormLayout(holder), stored, self.model)
        config = {}  # dialogs rebuild their config from the widgets
        form.apply(config)
        self.assertEqual(config[kp_table.KP_REF_START_KEY], 0.25)
        holder.deleteLater()


if __name__ == "__main__":
    unittest.main()
