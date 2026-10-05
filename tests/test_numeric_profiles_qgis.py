"""Numeric datasets in QGIS: polygons, storage, live KP layers, dialogs and the plot."""
import csv
import json
import tempfile
import time
import unittest
from pathlib import Path

from qgis.core import QgsCoordinateReferenceSystem, QgsDistanceArea, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer
from qgis.PyQt.QtWidgets import QApplication
from qgis.PyQt.QtCore import QPointF, Qt

from ..kp_geo_utils import RouteFrame
from ..burial import numeric_datasets as sources, numeric_profiles as n, schema
from ..burial.ground_plot import GroundModelPlot
from ..burial.numeric_profile_dialogs import ColoursWidget, DatasetDialog, check_dialog, source_dialog
from ..burial.numeric_profile_geometry import polygon_assignments
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..burial.tabs.ground_tab import GroundTab

REQUIRES_QGIS = True


def choose_columns(dialog, **roles):
    for role, column in roles.items():
        combo = dialog.column[role]
        combo.setCurrentIndex(combo.findData(column))


def kp_layer(rows, name="CPT ranges"):
    layer = QgsVectorLayer("None?field=CPT:string&field=KP_From:double&field=KP_To:double", name, "memory")
    features = []
    for values in rows:
        feature = QgsFeature(layer.fields())
        feature.setAttributes(list(values))
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return layer


KP_PLACEMENT = {"kind": "kp_table", "id_field": "CPT", "start_field": "KP_From", "end_field": "KP_To",
                "kp_unit": "km", "kp_rpl_id": "", "kp_rpl_label": "this plan's route"}


class NumericQgisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.layers = []
        self.store = BurialStore(str(Path(self.temp.name) / "numeric.gpkg"))
        self.store.migrate()
        self.plan_id = self.store.save_plan({"name": "Numeric", "method": "plough", "scope_start_kp": 10,
                                            "scope_end_kp": 14, "target_burial_m": 1.5})
        self.model = PlanModel(self.store)
        self.model.load_plan(self.plan_id)
        self.profiles = n.import_profiles([["A", 0, 0], ["A", .02, ""], ["A", .04, 250]],
                                         {"source_id": 0, "depth": 1, "value": 2}, dataset_id="d1",
                                         variable="su", units="kPa")
        self.rows = [{"source_id": "A", "start_kp": 10, "end_kp": 11},
                     {"source_id": "A", "start_kp": 12, "end_kp": 13}]

    def tearDown(self):
        for layer_id in self.layers:
            QgsProject.instance().removeMapLayer(layer_id)
        self.model.close_plan()
        self.store.close()
        QApplication.processEvents()
        try:
            self.temp.cleanup()
        except PermissionError:
            pass

    def route(self, wkt="LINESTRING(0 0, 4000 0)", start=10):
        distance = QgsDistanceArea()
        distance.setSourceCrs(QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance().transformContext())
        distance.setEllipsoid("NONE")
        return RouteFrame.from_source([QgsGeometry.fromWkt(wkt)], distance, start_kp_km=start)

    def polygon(self, wkt, source="A", crs="EPSG:3857"):
        layer = QgsVectorLayer(f"MultiPolygon?crs={crs}&field=Investigation:string", "Investigations", "memory")
        feature = QgsFeature(layer.fields())
        feature["Investigation"] = source
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        layer.dataProvider().addFeatures([feature])
        return layer

    def test_polygon_holes_disconnected_intervals_and_nonzero_datum(self):
        layer = self.polygon("POLYGON((500 -100,3500 -100,3500 100,500 100,500 -100),"
                             "(1500 -50,2500 -50,2500 50,1500 50,1500 -50))")
        rows, warnings = polygon_assignments(self.route(), layer, "Investigation")
        self.assertEqual(warnings, [])
        self.assertEqual([(r["start_kp"], r["end_kp"]) for r in rows], [(10.5, 11.5), (12.5, 13.5)])

    def test_retraced_route_does_not_snap_to_first_limb(self):
        layer = self.polygon("POLYGON((1000 -10,2000 -10,2000 10,1000 10,1000 -10))")
        rows, _ = polygon_assignments(self.route("LINESTRING(0 0,4000 0,0 0)"), layer, "Investigation")
        self.assertEqual([(r["start_kp"], r["end_kp"]) for r in rows], [(11, 12), (16, 17)])

    def test_polygon_crs_transformed_and_unknown_ids_flagged(self):
        layer = self.polygon("POLYGON((0.01 -0.001,0.02 -0.001,0.02 0.001,0.01 0.001,0.01 -0.001))",
                             source="unknown", crs="EPSG:4326")
        rows, _ = polygon_assignments(self.route(), layer, "Investigation")
        self.assertAlmostEqual(rows[0]["start_kp"], 11.1131949, places=5)
        self.assertEqual(n.unmatched_ids({r["source_id"] for r in rows}, {"A"}), ["unknown"])

    def test_polygon_assignment_uses_geodesic_and_grid_route_lengths(self):
        from ..kp_range_utils import GridDistanceArea
        crs = QgsCoordinateReferenceSystem("EPSG:4326")
        context = QgsProject.instance().transformContext()
        geodesic = QgsDistanceArea()
        geodesic.setSourceCrs(crs, context)
        geodesic.setEllipsoid("WGS84")
        grid = GridDistanceArea(crs, context, QgsCoordinateReferenceSystem("EPSG:3857"))
        layer = self.polygon("POLYGON((0.002 59.99,0.008 59.99,0.008 60.01,0.002 60.01,0.002 59.99))",
                             crs="EPSG:4326")
        lengths = []
        for distance in (geodesic, grid):
            route = RouteFrame.from_source([QgsGeometry.fromWkt("LINESTRING(0 60,0.01 60)")], distance, start_kp_km=25)
            rows, _ = polygon_assignments(route, layer, "Investigation")
            self.assertAlmostEqual(rows[0]["start_kp"], 25 + route.total_length_km * .2)
            self.assertAlmostEqual(rows[0]["end_kp"], 25 + route.total_length_km * .8)
            lengths.append(rows[0]["end_kp"] - rows[0]["start_kp"])
        self.assertGreater(lengths[1], 1.9 * lengths[0])

    def test_disconnected_route_parts_do_not_gain_a_joining_interval(self):
        layer = self.polygon("POLYGON((-100 -10,5000 -10,5000 10,-100 10,-100 -10))")
        rows, _ = polygon_assignments(self.route("MULTILINESTRING((0 0,1000 0),(3000 0,4000 0))"), layer, "Investigation")
        self.assertEqual([(r["start_kp"], r["end_kp"]) for r in rows], [(10, 11), (11, 12)])

    def dataset(self, **config):
        return {"dataset_id": "d1", "name": "CPT su", "variable": "su", "units": "kPa", "config": config}

    def add_layer(self, layer):
        QgsProject.instance().addMapLayer(layer)
        self.layers.append(layer.id())
        return layer

    def test_datasets_are_shared_replaced_atomically_and_removable(self):
        self.store.save_ground_dataset(self.dataset(placement={}), self.profiles)
        (row,) = self.store.list_ground_datasets()
        self.assertEqual((row["name"], row["config"]), ("CPT su", {"placement": {}}))
        self.assertEqual(self.store.list_numeric_profiles("d1")[0]["samples"], self.profiles[0]["samples"])
        points = {"source_id": 0, "depth": 1, "value": 2}
        other = n.import_profiles([["B", 0, 1]], points, dataset_id="d2")
        self.store.save_ground_dataset(dict(self.dataset(), dataset_id="d2", name="Other"), other)
        self.store.save_ground_dataset(self.dataset(), n.import_profiles([["C", 0, 5]], points, dataset_id="d1"))
        self.assertEqual([p["source_id"] for p in self.store.list_numeric_profiles("d1")], ["C"])
        self.assertEqual([p["source_id"] for p in self.store.list_numeric_profiles("d2")], ["B"])
        self.store.save_ground_dataset(dict(self.dataset(), name="Renamed"))  # measurements kept
        self.assertEqual(len(self.store.list_numeric_profiles("d1")), 1)
        # A plan stores only its choice; a duplicated plan shows the same dataset.
        self.model.update_gen_params({"numeric_ground": {"dataset_id": "d1"}})
        copy_id = self.store.duplicate_plan(self.plan_id, "Copy")
        copied = json.loads(self.store.get_plan(copy_id)["params_json"])["numeric_ground"]
        self.assertEqual(copied["dataset_id"], "d1")
        self.store.delete_ground_dataset("d1")
        self.assertEqual([d["dataset_id"] for d in self.store.list_ground_datasets()], ["d2"])
        self.assertEqual(self.store.list_numeric_profiles("d1"), [])
        self.assertEqual(int(self.store.read_meta()["schema_version"]), schema.SCHEMA_VERSION)

    def test_upgrade_groups_earlier_imports_into_removable_datasets(self):
        path = str(Path(self.temp.name) / "legacy.gpkg")
        legacy = BurialStore(path)
        legacy.migrate()
        old_fields = [f for f in schema.GROUND_PROFILE_FIELDS if f[0] != "dataset_id"]
        samples = json.dumps(self.profiles[0]["samples"])
        legacy._write_table_rows(schema.TABLE_GROUND_PROFILE, old_fields, [
            {"profile_id": "p1", "source_id": "A", "variable": "su", "units": "kPa",
             "samples_json": samples, "provenance_json": "{}"},
            {"profile_id": "p2", "source_id": "B", "variable": "qc", "units": "MPa",
             "samples_json": samples, "provenance_json": "{}"}])
        legacy.write_meta("schema_version", "11")
        legacy.close()
        legacy = BurialStore(path)
        legacy.migrate()
        datasets = legacy.list_ground_datasets()
        self.assertEqual(sorted(d["name"] for d in datasets),
                         ["qc (MPa) — earlier import", "su (kPa) — earlier import"])
        su = next(d for d in datasets if d["variable"] == "su")
        self.assertEqual([p["source_id"] for p in legacy.list_numeric_profiles(su["dataset_id"])], ["A"])
        self.assertEqual(legacy.read_meta()["schema_version"], str(schema.SCHEMA_VERSION))
        legacy.close()

    def test_kp_ranges_are_read_live_from_the_layer(self):
        layer = self.add_layer(kp_layer([("A", 10.0, 11.0), ("A", 13.0, 12.0), ("", 13.0, 13.5), ("Z", 14.0, 15.0)]))
        placement = dict(KP_PLACEMENT, layer=sources.layer_ref(layer))
        rows, notes, found = sources.read_placement(self.model, placement)
        self.assertIs(found, layer)
        self.assertEqual([(r["source_id"], r["start_kp"], r["end_kp"]) for r in rows],
                         [("A", 10, 11), ("A", 12, 13), ("Z", 14, 15)])
        self.assertIn("1 KP range row(s) have no investigation ID and were skipped", notes)
        first = next(layer.getFeatures())
        layer.dataProvider().changeAttributeValues({first.id(): {1: 10.5}})
        rows, _notes, _layer = sources.read_placement(self.model, placement)
        self.assertEqual(rows[0]["start_kp"], 10.5)
        rows, _notes, _layer = sources.read_placement(self.model, dict(placement, kp_unit="m"))
        self.assertAlmostEqual(rows[0]["start_kp"], .0105)
        with self.assertRaisesRegex(ValueError, "no field 'Nope'"):
            sources.read_placement(self.model, dict(placement, id_field="Nope"))
        self.assertEqual(sources.read_placement(self.model, {})[0], [])
        QgsProject.instance().removeMapLayer(self.layers.pop())
        with self.assertRaisesRegex(ValueError, "is not loaded"):
            sources.read_placement(self.model, placement)

    def test_dataset_dialog_maps_nothing_itself_and_reload_follows_the_source(self):
        path = Path(self.temp.name) / "cpt.csv"
        path.write_text("Hole;Top;Bottom;Reading\nP1;0;0,5;3,5\nP1;0,5;1;\n", encoding="utf-8")
        dialog = DatasetDialog(self.model)
        dialog.name.setText("CPT")
        dialog.variable.setText("su")
        dialog.units.setText("kPa")
        dialog.use_file(str(path))
        self.assertEqual({c.currentData() for c in dialog.column.values()}, {None})
        choose_columns(dialog, source_id=0, depth=1, base=2, value=3)
        dialog.decimal_comma.setChecked(True)
        self.assertFalse(dialog.support.isEnabled())
        dialog._check_measurements()
        self.assertEqual(dialog.measure_check.text(),
                         "✓ 1 investigation(s), 2 depth sample(s) (1 missing), depth 0–1 m, values 3.5–3.5 kPa")
        dialog.column["depth"].setCurrentIndex(0)
        dialog._check_measurements()
        self.assertEqual(dialog.measure_check.text(), "✗ Choose the depth column.")
        choose_columns(dialog, depth=1)
        dialog._accept()
        dataset, profiles = dialog.result()
        dialog.close()
        self.assertEqual(dataset["config"]["measurements"]["columns"],
                         {"source_id": "Hole", "depth": "Top", "base": "Bottom", "value": "Reading"})
        self.assertEqual(profiles[0]["samples"][0]["value"], 3.5)
        self.store.save_ground_dataset(dataset, profiles)
        stored = self.store.list_ground_datasets()[0]
        self.assertIsNone(sources.source_changed(stored))
        # Editing without touching the measurements keeps them; renaming relabels them.
        edit = DatasetDialog(self.model, stored, profiles)
        self.assertEqual(edit.column["value"].currentData(), 3)
        edit._accept()
        self.assertIsNone(edit.result()[1])
        edit = DatasetDialog(self.model, stored, profiles)
        edit.units.setText("MPa")
        edit._accept()
        self.assertEqual(edit.result()[1][0]["units"], "MPa")
        edit.close()
        path.write_text("Hole;Top;Bottom;Reading\nP1;0;0,5;3,5\nP1;0,5;1;4\nP2;0;1;9\n", encoding="utf-8")
        self.assertIn("has changed since it was imported", sources.source_changed(stored))
        fresh, config = sources.reload_measurements(stored)
        self.assertEqual([p["source_id"] for p in fresh], ["P1", "P2"])
        self.store.save_ground_dataset(dict(stored, config=config), fresh)
        self.assertIsNone(sources.source_changed(self.store.list_ground_datasets()[0]))
        path.write_text("Hole;Depth;Bottom;Reading\nP1;0;1;3\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no column named 'Top'"):
            sources.reload_measurements(stored)

    def test_dataset_dialog_checks_kp_layer_ids(self):
        self.store.save_ground_dataset(self.dataset(), self.profiles)
        layer = self.add_layer(kp_layer([("A", 10.0, 11.0), ("a ", 12.0, 13.0)]))
        dialog = DatasetDialog(self.model, self.store.list_ground_datasets()[0], self.profiles)
        dialog.place_layer.setCurrentIndex(dialog.place_layer.findData(layer.id()))
        for combo, name in ((dialog.id_field, "CPT"), (dialog.kp_form.start_field, "KP_From"),
                            (dialog.kp_form.end_field, "KP_To")):
            combo.setText(name)
        dialog._check_placement()
        self.assertIn("✓ 2 KP range(s) for 2 investigation(s), KP 10.000–13.000", dialog.place_check.text())
        self.assertIn("a (did you mean A?)", dialog.place_check.text())
        dialog._accept()
        placement = dialog.result()[0]["config"]["placement"]
        self.assertEqual((placement["kind"], placement["id_field"], placement["start_field"]),
                         ("kp_table", "CPT", "KP_From"))
        self.assertIn("kp_rpl_id", placement)
        dialog.close()

    def test_tab_live_layer_check_export_and_remove(self):
        class Dock:
            def workbench_store(self):
                return None
        layer = self.add_layer(kp_layer([("A", 10.0, 11.0), ("A", 12.0, 13.0), ("Q", 13.0, 14.0)]))
        classes = n.classes_from_breaks([5, 200], ["#ff0000", "#00ff00", "#0000ff"])
        dataset = self.dataset(placement=dict(KP_PLACEMENT, layer=sources.layer_ref(layer)),
                               colours={"mode": "classes", "classes": classes})
        self.store.save_ground_dataset(dataset, self.profiles)
        self.model.update_gen_params({"numeric_ground": {"mode": 1, "dataset_id": "d1", "depth_max": .1}})
        tab = GroundTab(self.model, Dock())
        panel = tab.numeric
        self.assertEqual(tab.display_mode.currentIndex(), 1)
        self.assertEqual(len(tab.plot._numeric_index.assignments), 3)
        self.assertEqual(len(tab.plot._numeric_classes), 3)
        self.assertIn("1 KP range ID(s) without measurements", panel.status.text())
        layer.startEditing()
        layer.deleteFeature(next(f.id() for f in layer.getFeatures() if f["CPT"] == "Q"))
        layer.commitChanges()
        self.assertTrue(wait_for(lambda: len(tab.plot._numeric_index.assignments) == 2))
        dialog = check_dialog(dataset, panel.profiles, panel.assignments, panel.notes, None, (10, 14), tab)
        self.assertEqual(dialog.table_rows[0][0], "A")
        self.assertEqual(dialog.table_rows[0][7], "10.000–11.000; 12.000–13.000")
        dialog.close()
        out = Path(self.temp.name) / "cells.csv"
        self.assertEqual(panel._export(str(out)), str(out))
        with out.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["investigation", "kp_from", "kp_to", "depth_top_m", "depth_base_m",
                                   "value_kPa", "class", "status"])
        self.assertEqual(rows[1], ["A", "10.000000", "11.000000", "0", "0.01", "0", "su < 5", "ok"])
        self.assertEqual(rows[2][5:], ["", "", "missing"])
        self.assertTrue(panel.remove_dataset("d1"))
        self.assertEqual(self.store.list_ground_datasets(), [])
        self.assertEqual(json.loads(self.model.plan["params_json"])["numeric_ground"]["dataset_id"], "")
        QApplication.processEvents()
        tab.close()

    def test_plot_classes_legend_hover_and_click(self):
        import numpy as np
        from ..burial.numeric_profile_plot import class_arrays, classify
        classes = n.normalise_classes([
            {"max": 5, "max_inclusive": True, "colour": "#ff0000"},
            {"min": 5, "max": 200, "min_inclusive": False, "max_inclusive": False, "colour": "#00ff00"},
            {"min": 200, "min_inclusive": True, "colour": "#0000ff"}])
        rgba = classify(np.array([0, 5, 5.5, 199.9, 250, np.nan]), class_arrays(classes))
        self.assertEqual([tuple(int(v) for v in c[:3]) for c in rgba[:5]],
                         [(255, 0, 0), (255, 0, 0), (0, 255, 0), (0, 255, 0), (0, 0, 255)])
        plot = GroundModelPlot()
        plot.resize(800, 350)
        index = n.ProfileIndex(self.profiles, self.rows)
        plot.set_numeric(index, {"name": "CPT su", "variable": "su", "units": "kPa", "depth_min": 0,
                                 "depth_max": .1, "colours": {"mode": "classes", "classes": classes}})
        plot.plot.setXRange(10, 14)
        plot.show()
        QApplication.processEvents()
        self.assertIn("200 ≤ su", plot.legend.text())
        self.assertIn("[su ≤ 5]", plot._readout_text(10.5, 0))
        self.assertIn("[200 ≤ su]", plot._readout_text(10.5, .04))
        self.assertIn("No reading at this depth", plot._readout_text(10.5, .5))
        self.assertIn("No KP range here", plot._readout_text(11.5, .02))
        plot._numeric_item._cache.clear()
        strip = plot._numeric_item._strip("A", 0, .1, 100)
        self.assertEqual(strip.pixelColor(4, 2).name(), "#ff0000")
        self.assertEqual(strip.pixelColor(4, 45).name(), "#0000ff")
        selected = []
        plot.profileClicked.connect(selected.append)
        position = plot.plot.getViewBox().mapViewToScene(QPointF(10.5, .02))

        class Click:
            def button(self):
                return getattr(Qt, "MouseButton", Qt).LeftButton

            def scenePos(self):
                return position
        plot._mouse_clicked(Click())
        self.assertEqual(selected, ["A"])
        plot.set_numeric(n.ProfileIndex(self.profiles, self.rows),
                         {"variable": "su", "colours": {"mode": "continuous", "auto": False, "min": 10, "max": 200}})
        self.assertEqual(plot._numeric_item.limits, (10, 200))
        plot.set_numeric(None)
        self.assertTrue(plot._unit_item.isVisible())
        source_dialog("A", self.profiles, plot).close()
        plot.close()

    def test_colours_widget_reuses_range_rows_and_summarises(self):
        widget = ColoursWidget()
        widget.set_colours({"mode": "classes"})
        widget.set_values([1, 5, 5, 7, 50, None], "su", "kPa")
        widget.breaks.setText("5, 10")
        widget._from_breaks()
        table = widget.table
        self.assertEqual(table.row_count(), 3)
        self.assertIn("5 ≤ su &lt; 10</b> kPa — 3 sample(s), 60%", widget.summary.text())
        lower = table.table.cellWidget(1, table.col_lower)
        lower.setCurrentIndex(lower.findData(False))
        self.assertIn("No class covers su = 5", widget.summary.text())
        self.assertIn("Outside every class: 2 sample(s), 40%", widget.summary.text())
        colours = widget.colours()
        self.assertEqual((colours["mode"], colours["classes"][1]["min_inclusive"]), ("classes", False))
        widget.mode.setCurrentIndex(widget.mode.findData("continuous"))
        widget.auto.setChecked(False)
        widget.minimum.setValue(5)
        widget.maximum.setValue(1)
        with self.assertRaisesRegex(ValueError, "greater than"):
            widget.colours()
        widget.close()


def wait_for(condition, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(.02)
    return condition()


if __name__ == "__main__":
    unittest.main()
