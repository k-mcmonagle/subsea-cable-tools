# -*- coding: utf-8 -*-
"""Regression coverage for exact contacts, scored plans and register edits through a filtered view."""

import csv
import io
import json

import pytest
from qgis.core import QgsFeature, QgsGeometry, QgsVectorLayer
from qgis.PyQt.QtCore import Qt

from ..burial import io_csv, map_layers, report, risk, risk_scan, schema
from ..burial.hazard_register import ColumnFiltersDialog, HazardRegister, CONTACT, KP, NOTES, OFFSET, RISK, SCORE, STATUS
from ..burial.plan_model import PlanModel
from ..burial.risk_scale import DEFAULT_SCALE, RiskScale
from ..burial.risk_scale_dialog import RiskScaleDialog
from ..burial.store import BurialStore
from ..burial.tabs.risk_tab import CheckEditorDialog, ManualHazardDialog
from ..workbench.rules_engine import Interval
from .test_burial_task import _route

REQUIRES_QGIS = True


def _scale():
    return RiskScale(DEFAULT_SCALE.rows() + [dict(id="review", label="Escalate", score=20, color="#851a49")])


def _check(**options):
    return {"check_id": "c", "name": "Contacts", "enabled": 1,
            "config_json": json.dumps(dict(input_id="survey", distance_m=0.0, **options))}


def _layer(kind, *wkts):
    layer = QgsVectorLayer(f"{kind}?crs=EPSG:4326&field=likelihood:integer&field=impact:integer", kind, "memory")
    for wkt in wkts:
        feature = QgsFeature(layer.fields())
        feature.setAttributes([3, 5])
        feature.setGeometry(QgsGeometry.fromWkt(wkt))
        assert layer.dataProvider().addFeatures([feature])[0]
    layer.updateExtents()
    return layer


def test_exact_features_and_tight_offsets():
    route, distance = _route()
    check = _check(on_route_risk="review", default_risk="low")
    def scan(layer, scope=None):
        hazards, warnings = risk_scan.scan_check("p", check, layer, route, distance, scope=scope, scale=_scale())
        assert not warnings
        return hazards

    points = _layer("MultiPoint", "MULTIPOINT((0 50.07), (0.000001 50.07), (0 50.10))")
    hits = scan(points)
    assert [(risk.interaction(h), h["offset_m"], h["risk"]) for h in hits] == [("On route", 0, "review")] * 2
    assert hits[0]["feature_ref"].endswith("#0") and hits[1]["feature_ref"].endswith("#2")
    # The bounding-box prefilter may admit this point 7 cm away. Only positive proximity may register it.
    config = dict(risk.check_config(check), distance_m=1.0)
    check["config_json"] = json.dumps(config)
    hits = scan(points)
    near = [h for h in hits if risk.interaction(h) == "Nearby"]
    assert len(hits) == 3 and len(near) == 1 and 0 < near[0]["offset_m"] < 0.1 and near[0]["risk"] == "low"

    check["config_json"] = json.dumps(dict(config, distance_m=0))
    line = _layer("LineString", "LINESTRING(-0.01 50.05,0.01 50.05)",
                  "LINESTRING(0.000001 50.045,0.000001 50.08)")
    cross, = scan(line)
    assert cross["crossing"] == 1 and cross["risk"] == "review" and 89 < cross["crossing_angle_deg"] <= 90
    overlap, = scan(_layer("LineString", "LINESTRING(0 50.04,0 50.06,0 50.08)"), Interval(5.5, 7))
    assert (overlap["kp"], overlap["end_kp"], overlap["crossing_angle_deg"]) == (5.5, 7, None)
    assert risk.interaction(overlap) == "Overlap"
    mixed = _layer("MultiLineString", "MULTILINESTRING((0 50.04,0 50.08),(-0.01 50.10,0.01 50.10))")
    overlap, cross = scan(mixed)
    geom = next(mixed.getFeatures()).geometry().intersection(route.geometries[0])
    old_points = []
    for vertex in geom.vertices():
        xy = (vertex.x(), vertex.y())
        if xy not in old_points:
            old_points.append(xy)
    assert cross["feature_ref"].endswith(f"#{old_points.index((0.0, 50.10))}")
    assert "#overlap" in overlap["feature_ref"]
    polygon = _layer("Polygon", "POLYGON((-0.01 50.04,0.01 50.04,0.01 50.08,-0.01 50.08,-0.01 50.04))",
                     "POLYGON((0 50.12,0.01 50.13,0.01 50.11,0 50.12))",
                     "POLYGON((0.000001 50.15,0.01 50.15,0.01 50.16,0.000001 50.15))")
    area, touch = scan(polygon)
    assert area["end_kp"] > area["kp"] and risk.interaction(area) == "Overlap"
    assert touch["kp"] == touch["end_kp"] and risk.interaction(touch) == "Touches route"
    assert all(h["risk"] == "review" for h in (area, touch, overlap))
    check["config_json"] = json.dumps({"distance_m": 0, "band_high_m": 1})
    assert [h["risk"] for h in scan(points)] == ["high", "high"]


def test_scale_controls_rules_and_usage_protection(tmp_path):
    store = BurialStore(str(tmp_path / "risk.gpkg"))
    store.migrate()
    model = PlanModel(store)
    model.refresh_layers = lambda *args, **kwargs: None
    plan_id = model.create_plan("Scored plan", "plough")
    assert model.update_gen_params({"unrelated_setting": 12})
    scale = _scale()
    assert model.set_risk_scale(scale.rows())
    check = _check(on_route_risk="low", default_risk="medium",
                   attribute_rules=[{"expression": '"likelihood" * "impact" >= 15', "risk": "review"}])
    assert model.save_risk_checks([dict(check, plan_id=plan_id)])
    editor = CheckEditorDialog(check, [], model=model)
    assert editor.distance_spin.value() == 0 and "intersections" in editor.distance_spin.text()
    assert editor.contact_combo.currentData() == "low"
    editor.distance_spin.setValue(50)
    editor.band_spins["band_review_m"].setValue(10)
    criteria = risk.check_config(editor.result_check())
    assert criteria["attribute_rules"][0]["risk"] == "review" and criteria["band_review_m"] == 10
    editor.deleteLater()
    route, distance = _route()
    layer = _layer("Point", "POINT(0 50.1)")
    found, warnings = risk_scan.scan_check(plan_id, check, layer, route, distance, scale=scale)
    assert not warnings and len(found) == 1 and found[0]["auto_risk"] == "review"
    entries = risk_scan.snapshot_check_features(check, layer, route.geometries[0])
    task = risk_scan.RiskScanTask(plan_id, [(check, entries)], route.geometries, store.transform_context,
                                 Interval(0, 22), 1, lambda _task: None, scale=scale)
    assert task.run() and [h["risk"] for h in task.hazards] == ["review"]
    assert model.apply_risk_scan(found, check_ids=["c"])
    hid = model.hazards[0]["hazard_id"]
    assert model.update_hazards([hid], {"risk": "medium", "notes": "reviewed"})
    assert model.apply_risk_scan(found, check_ids=["c"])
    assert model.record_risk_run(["c"], {"c": 1}, {}, "Scanned")
    with pytest.raises(ValueError, match="Escalate"):
        model.set_risk_scale(DEFAULT_SCALE.rows())
    # Renaming a level or changing its colour retains the level ID and scan currency.
    rows = scale.rows()
    rows[-1].update(label="Review & escalate", color="#402050")
    assert model.set_risk_scale(rows)
    assert model.risk_status()["c"]["state"] == "current"
    rows[-1]["score"] = 25
    assert model.set_risk_scale(rows)
    assert model.risk_status()["c"]["state"] == "changed"
    again = PlanModel(store)
    again.refresh_layers = lambda *args, **kwargs: None
    assert again.load_plan(plan_id)
    assert again.risk_scale().scores["review"] == 25
    assert json.loads(again.plan["params_json"])["unrelated_setting"] == 12
    assert (again.hazards[0]["hazard_id"], again.hazards[0]["notes"], again.hazards[0]["risk"],
            again.hazards[0]["auto_risk"]) == (hid, "reviewed", "medium", "review")

    dialog = RiskScaleDialog(again.risk_scale(), {"review"})
    dialog.table.item(3, 1).setText("30")
    dialog._accept()
    assert dialog.scale.scores["review"] == 30
    dialog.deleteLater()
    manual = ManualHazardDialog(None, scale=again.risk_scale())
    manual.risk_combo.setCurrentIndex(manual.risk_combo.findData("review"))
    assert manual.values()[1:4] == (None, "Manual hazard", "review")
    manual.deleteLater()
    assert again.add_manual_hazard(0.0, None, "Manual contact", "review")
    exported = io_csv.hazards_csv(again.plan, again.hazards, again.risk_checks)
    lines = exported.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("risk,status,"))
    by_feature = {r["feature"]: r for r in csv.DictReader(io.StringIO("\n".join(lines[header:])))}
    assert by_feature["Manual contact"]["risk_score"] == "25" and by_feature["Manual contact"]["offset_m"] == ""
    assert any(r["auto_risk"] == "Review & escalate" and r["risk_source"] == "user" for r in by_feature.values())
    html = report.build_report_html(again.plan, [], [], [], [], hazards=again.hazards)
    assert "25 · Review &amp; escalate" in html
    map_layers.write_plan_layers(store, again.plan, [], [], route, again.hazards, again.risk_checks,
                                parts=("hazards",))
    table_name = schema.hazards_layer_name(again.plan["name"], again.plan["rev_label"], plan_id)
    layer = QgsVectorLayer(f"{store.gpkg_path}|layername={table_name}", "test hazards", "ogr")
    saved = {f["label"]: (f["risk_label"], f["risk_score"]) for f in layer.getFeatures()}
    assert saved["Manual contact"] == ("Review & escalate", 25)
    map_layers.apply_hazards_style(layer, again.risk_scale())
    rules = layer.renderer().rootRule().children()
    assert any(r.label() == "25 · Review & escalate" and "review" in r.filterExpression() for r in rules)
    assert again.update_hazards([h["hazard_id"] for h in again.hazards], {"risk_source": schema.RISK_SOURCE_AUTO})
    assert all(h["risk"] == "review" for h in again.hazards)
    assert {h["source"]: h["risk_source"] for h in again.hazards} == {"check": "auto", "manual": "user"}


def test_multipart_feature_obeys_reported_scan_limit(monkeypatch):
    route, distance = _route()
    monkeypatch.setattr(risk_scan, "MAX_HAZARDS_PER_CHECK", 1)
    points = _layer("MultiPoint", "MULTIPOINT((0 50.07), (0 50.09))")
    hazards, warnings = risk_scan.scan_check("p", _check(), points, route, distance)
    assert len(hazards) == len(warnings) == 1
    assert "cap" in warnings[0]


def test_register_filters_sort_and_review_beyond_2000():
    view = HazardRegister()
    scale = _scale()
    hazards = [dict(hazard_id=str(i), label=f"Feature {i}", kp=float(i), end_kp=float(i),
                    risk="low", offset_m=1.0, status="open") for i in range(2030)]
    hazards[-2].update(label="Cable A", risk="review", check_id="c", notes="Needs survey", kp=10.0, offset_m=-10.0)
    hazards[-1].update(label="Cable B", risk="high", check_id="c", notes="Needs survey", kp=2.0, offset_m=-2.0)
    view.replace(hazards, [_check()], scale, "p")
    view.search.setText("SURVEY cable")
    assert view.proxy.rowCount() == 2 and view.count.text() == "2 of 2,030"
    assert view.hazard_at(0)["label"] == "Cable B"
    view.table.sortByColumn(OFFSET, Qt.SortOrder.AscendingOrder)
    assert view.hazard_at(0)["label"] == "Cable A"
    view.table.sortByColumn(RISK, Qt.SortOrder.DescendingOrder)
    assert view.hazard_at(0)["risk"] == "review"
    dialog = ColumnFiltersDialog(view.source, {}, view)
    dialog.fields[KP][0].setText("2")
    dialog.fields[SCORE][0].setText("20")
    dialog.fields[CONTACT].setCurrentIndex(dialog.fields[CONTACT].findData("Nearby"))
    dialog.fields[STATUS].setCurrentIndex(dialog.fields[STATUS].findData("open"))
    dialog._accept()
    view.proxy.columns = dialog.filters
    view._apply_filters()
    assert view.proxy.rowCount() == 1 and view.hazard_at(0)["label"] == "Cable A"
    view.table.selectRow(0)
    edits = []
    view.source.editRequested.connect(lambda key, field, value: edits.append((key, field, value)))
    assert view.proxy.setData(view.proxy.index(0, NOTES), "checked")
    index = view.proxy.index(0, STATUS)
    editor = view.delegate.createEditor(view.table, None, index)
    editor.setCurrentIndex(editor.findData("noted"))
    view.delegate.setModelData(editor, view.proxy, index)
    editor.deleteLater()
    assert edits == [("2028", "notes", "checked"), ("2028", "status", "noted")]
    view.replace(list(reversed(hazards)), [_check()], scale, "p")
    assert view.selected_ids() == ["2028"]
    view.clear_filters()
    assert view.proxy.rowCount() == len(hazards)
    view.search.setText("Cable")
    view.replace([], [], DEFAULT_SCALE, "another-plan")
    assert not view.search.text() and not view.proxy.columns and not view.selected_ids()
    view.deleteLater()
