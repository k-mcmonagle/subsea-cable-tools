# -*- coding: utf-8 -*-
"""QGIS checks: persisted Exclusions / Risk results survive a reload and
report what changed; content-based bathymetry fingerprints; relinking by
source; KP-bar hover helpers; the multi-layer Add inputs picker; profile
overlay filtering; KP-ranged targets in the ground overlay; and a headless
dock build + project-reload smoke."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from ..burial import analysis_state, generation, map_layers
from ..burial import schema as burial_schema
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..qgis_compat import (ITEM_DATA_USER_ROLE, VECTOR_WRITER_NO_ERROR,
                           VECTOR_WRITER_OVERWRITE_FILE,
                           VECTOR_WRITER_OVERWRITE_LAYER)
from ..workbench.depth_service import DepthSourceConfig
from ..workbench.rules_engine import Interval
from .test_burial_task import _route


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _store() -> BurialStore:
    store = BurialStore(os.path.join(tempfile.mkdtemp(prefix="bp_persist_"),
                                     "plans.gpkg"))
    store.migrate()
    return store


def _rule(rule_id: str, distance: float = 100.0) -> dict:
    return {"rule_id": rule_id, "name": f"Rule {rule_id}", "enabled": 1,
            "kind": "manual", "action": "exclude", "risk_level": 0,
            "criterion_class": "project", "source_ref": "",
            "methods_json": "[]", "notes": "",
            "config_json": json.dumps({"ranges": [[1.0, 2.0]],
                                       "distance_m": distance})}


def test_analysis_persists_across_reload() -> bool:
    route, _da = _route()
    store = _store()
    model = PlanModel(store)
    plan_id = model.create_plan("Persist", "plough")
    model.route = route
    ok = model.update_plan({"scope_start_kp": 0.0, "scope_end_kp": 10.0},
                           reason="scope")
    ok = ok and model.save_rules([_rule("r1"), _rule("r2")])
    params = model.gen_params()
    ctx = generation.ResolutionContext(
        rule_hits={"r1": [Interval(1.0, 2.0)]},
        rule_nodata={"r2": [Interval(4.0, 5.0)]})
    ok = ok and model.save_analysis(ctx, [Interval(4.0, 5.0)], params,
                                    model.rules, "evaluated", ["w1"])

    # A fresh model on the same file (= reopening the project).
    again = PlanModel(store)
    ok = ok and again.load_plan(plan_id)
    shown = again.display_context()
    ok = ok and [(iv.start_km, iv.end_km) for iv in shown.rule_hits["r1"]] \
        == [(1.0, 2.0)]
    ok = ok and [(iv.start_km, iv.end_km) for iv in shown.insufficient] \
        == [(4.0, 5.0)]
    status = again.analysis_status()
    ok = ok and status["source"] == "recompute"
    ok = ok and status["rule_state"] == {"r1": "current", "r2": "current"}
    ok = ok and status["reasons"] == [] and status["warnings"] == ["w1"]
    # The generation context (section derivation) is untouched.
    ok = ok and not again.context.rule_hits

    # Route restored -> geometry fingerprint compared, still current.
    again.route = route
    again._route_geom_fp = ""
    ok = ok and again.analysis_status()["reasons"] == []
    # Criterion edited -> only that bar is out of date.
    edited = [dict(again.rules[0], config_json=json.dumps(
        {"ranges": [[1.0, 3.0]]})), again.rules[1]]
    ok = ok and again.save_rules(edited)
    ok = ok and again.analysis_status()["rule_state"]["r1"] == "changed"
    ok = ok and again.analysis_status()["rule_state"]["r2"] == "current"
    # Scope changed -> everything out of date, with the reason named.
    ok = ok and again.update_plan({"scope_end_kp": 12.0}, reason="scope")
    ok = ok and "the scope" in again.analysis_status()["reasons"]
    # Resolving the no-data range in Plan Builder hides it from the
    # displayed analysis without a recompute.
    stored = json.loads(again.plan.get("params_json") or "{}")
    stored["dismissed_insufficient"] = [[4.0, 5.0, "skip"]]
    again.plan["params_json"] = json.dumps(stored)
    ok = ok and again.display_context().insufficient == []

    # Risk scan records.
    check = {"check_id": "c1", "plan_id": plan_id, "name": "Rocks",
             "enabled": 1, "config_json": json.dumps({"distance_m": 50}),
             "source_ref": "", "notes": ""}
    ok = ok and again.save_risk_checks([check])
    ok = ok and again.record_risk_run(["c1"], {}, {}, "Scanned 1 check(s).")
    third = PlanModel(store)
    third.load_plan(plan_id)
    state = third.risk_status()["c1"]
    ok = ok and state["state"] == "current" and state["count"] == 0
    ok = ok and third.risk_runs()["message"] == "Scanned 1 check(s)."
    # The exclusion snapshot survived the risk write.
    ok = ok and bool(third.display_context().rule_hits)
    edited_check = dict(third.risk_checks[0], config_json=json.dumps(
        {"distance_m": 80}))
    ok = ok and third.save_risk_checks([edited_check])
    ok = ok and third.risk_status()["c1"]["state"] == "changed"

    store.delete_plan(plan_id)
    ok = ok and store.get_analysis(plan_id) is None
    return _result("exclusion + risk results persist across reload, "
                   "report what changed", ok)


def test_legacy_generation_status() -> bool:
    """Plans generated before bp_analysis: bars come from the generation's
    context and are judged against the generation's own rules and scope."""
    store = _store()
    model = PlanModel(store)
    plan_id = model.create_plan("Legacy", "plough")
    ok = model.update_plan({"scope_start_kp": 0.0, "scope_end_kp": 10.0},
                           reason="scope")
    ok = ok and model.save_rules([_rule("r1")])
    params = model.gen_params()
    store.save_generation({
        "generation_id": "g1", "plan_id": plan_id, "active": 1,
        "rules_snapshot_json": generation.rules_snapshot(model.rules),
        "params_json": json.dumps(params.to_dict()),
        "inputs_fingerprint_json": "{}",
        "summary_json": json.dumps({"context": {
            "rule_hits": {"r1": [[1.0, 2.0]]}}}),
        "proposal_diff_json": "{}"})
    again = PlanModel(store)
    again.load_plan(plan_id)
    status = again.analysis_status()
    ok = ok and status["source"] == "generation"
    ok = ok and status["rule_state"] == {"r1": "current"}
    ok = ok and status["reasons"] == []
    ok = ok and bool(again.display_context().rule_hits)
    ok = ok and again.update_plan({"scope_end_kp": 15.0}, reason="scope")
    ok = ok and "the scope" in again.analysis_status()["reasons"]
    return _result("legacy generation context judged against its own run", ok)


def test_schema_v10_adds_analysis_table() -> bool:
    store = _store()
    conn = sqlite3.connect(store.gpkg_path)
    try:
        conn.execute("DROP TABLE IF EXISTS bp_analysis")
        conn.execute("DELETE FROM gpkg_contents WHERE table_name='bp_analysis'")
        conn.execute("UPDATE bp_meta SET value='9' WHERE key='schema_version'")
        conn.commit()
    finally:
        conn.close()
    reopened = BurialStore(store.gpkg_path)
    reopened.migrate()
    ok = reopened.read_meta().get("schema_version") == \
        str(burial_schema.SCHEMA_VERSION)
    ok = ok and reopened.get_analysis("nope") is None
    reopened.save_analysis({"plan_id": "p", "run_utc": "x",
                            "context_json": "{}"})
    ok = ok and (reopened.get_analysis("p") or {}).get("run_utc") == "x"
    reopened.close()
    return _result("schema v10 migration adds bp_analysis", ok)


def _write_gpkg_layer(path: str, name: str, wkt_rows, overwrite_file: bool):
    mem = QgsVectorLayer("LineString?crs=EPSG:4326&field=depth:double",
                         name, "memory")
    feats = []
    for depth, wkt in wkt_rows:
        feat = QgsFeature(mem.fields())
        feat.setAttributes([depth])
        feat.setGeometry(QgsGeometry.fromWkt(wkt))
        feats.append(feat)
    mem.dataProvider().addFeatures(feats)
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = name
    options.actionOnExistingFile = (
        VECTOR_WRITER_OVERWRITE_FILE if overwrite_file
        else VECTOR_WRITER_OVERWRITE_LAYER)
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        mem, path, QgsCoordinateTransformContext(), options)
    return result[0] == VECTOR_WRITER_NO_ERROR


def test_depth_fingerprint_is_content_based() -> bool:
    project = QgsProject.instance()
    folder = tempfile.mkdtemp(prefix="bp_depthfp_")
    path = os.path.join(folder, "survey.gpkg")
    ok = _write_gpkg_layer(path, "contours", [
        (10.0, "LINESTRING(-0.1 50.05, 0.1 50.05)"),
        (20.0, "LINESTRING(-0.1 50.1, 0.1 50.1)")], True)
    ok = ok and _write_gpkg_layer(path, "other", [
        (1.0, "LINESTRING(1 1, 2 2)")], False)
    layer = QgsVectorLayer(path + "|layername=contours", "Contours", "ogr")
    project.addMapLayer(layer)
    config_data = {"mode": 2, "contour_layers": [
        {"layer_id": layer.id(), "source": layer.source(),
         "depth_field": "depth"}]}
    config = DepthSourceConfig(config_data)
    fp1 = map_layers.depth_config_fingerprint(project, config)
    legacy1 = map_layers.legacy_depth_config_fingerprint(project, config)
    stamp1 = map_layers.layer_content_stamp(layer)
    ok = ok and stamp1.startswith("gpkg:")
    # Editing ANOTHER table in the same GeoPackage (newer mtime) must not
    # change the contour layer's fingerprint.
    time.sleep(1.2)
    ok = ok and _write_gpkg_layer(path, "other", [
        (2.0, "LINESTRING(1 1, 3 3)")], False)
    fp2 = map_layers.depth_config_fingerprint(project, config)
    legacy2 = map_layers.legacy_depth_config_fingerprint(project, config)
    ok = ok and fp2 == fp1

    # Removed and re-added (new layer id): relinked by source, same
    # fingerprint; a layer that is simply gone is reported missing.
    project.removeMapLayer(layer.id())
    missing = map_layers.depth_layer_fingerprints(project, config)
    ok = ok and any(key.startswith("missing:") for key in missing)
    readded = QgsVectorLayer(path + "|layername=contours", "Contours 2", "ogr")
    project.addMapLayer(readded)
    relinked, notes = map_layers.relink_depth_config(project, config_data)
    ok = ok and relinked["contour_layers"][0]["layer_id"] == readded.id()
    ok = ok and notes == ["contours 'Contours 2'"]
    fp3 = map_layers.depth_config_fingerprint(
        project, DepthSourceConfig(relinked))
    ok = ok and fp3 == fp1

    # Editing the contour table itself does change it.
    time.sleep(1.2)
    feat = QgsFeature(readded.fields())
    feat.setAttributes([None, 30.0])
    feat.setGeometry(QgsGeometry.fromWkt("LINESTRING(-0.1 50.15, 0.1 50.15)"))
    readded.dataProvider().addFeatures([feat])
    readded.dataProvider().reloadData() if hasattr(
        readded.dataProvider(), "reloadData") else None
    fp4 = map_layers.depth_config_fingerprint(
        project, DepthSourceConfig(relinked))
    ok = ok and fp4 != fp1
    project.removeMapLayer(readded.id())
    return _result("bathymetry fingerprint: per-table content, relink by "
                   "source", ok,
                   "legacy mtime fingerprint "
                   + ("changed" if legacy1 != legacy2 else "unchanged")
                   + " when another table was written")


def test_model_relinks_bathymetry_and_reports_reasons() -> bool:
    project = QgsProject.instance()
    folder = tempfile.mkdtemp(prefix="bp_relink_")
    path = os.path.join(folder, "survey.gpkg")
    ok = _write_gpkg_layer(path, "contours", [
        (10.0, "LINESTRING(-0.1 50.05, 0.1 50.05)")], True)
    layer = QgsVectorLayer(path + "|layername=contours", "Contours", "ogr")
    project.addMapLayer(layer)
    model = PlanModel(object(), None)
    model.plan = {"plan_id": "p1", "scope_start_kp": 0.0,
                  "scope_end_kp": 1.0, "params_json": "{}"}
    model.inputs = [{"role": burial_schema.INPUT_ROLE_BATHY,
                     "config_json": json.dumps({"mode": 2, "contour_layers": [
                         {"layer_id": "gone-id", "source": layer.source(),
                          "depth_field": "depth"}]})}]
    config = model.depth_config()
    ok = ok and config.contour_layers[0]["layer_id"] == layer.id()
    ok = ok and model.depth_relinks == ["contours 'Contours'"]
    identity = model.profile_identity()
    ok = ok and identity["depth_layer_names"] and not any(
        key.startswith("missing:") for key in identity["depth_layers"])
    project.removeMapLayer(layer.id())
    model.invalidate_depth_cache()
    from ..burial.profile_data import PlanProfile

    model.bathy_profile = PlanProfile(
        step_m=model.resolve_profile_step_m(),
        cross_offset_m=model.resolve_cross_offset_m(),
        scope_start_kp=0.0, scope_end_kp=1.0,
        kps=[0.0, 1.0], depths=[10.0, 11.0], **identity)
    reasons = model.profile_stale_reasons()
    ok = ok and any("not in the project" in r for r in reasons)
    ok = ok and model.profile_state() == "stale"
    return _result("model: bathymetry relinked by source; stale reasons "
                   "name the missing layer", ok, "; ".join(reasons)[:120])


def test_kp_bar_helpers_and_hover() -> bool:
    from qgis.PyQt.QtCore import QRect
    from qgis.PyQt.QtGui import QColor
    from qgis.PyQt.QtWidgets import QTableWidget, QTableWidgetItem

    from ..workbench import kp_bars

    rect = QRect(10, 0, 100, 20)
    ok = abs(kp_bars.kp_at_x(rect, 60, 10.0, 5.0) - 10.0) < 1e-9
    ok = ok and kp_bars.kp_at_x(rect, 200, 10.0, 5.0) is None
    ok = ok and kp_bars.x_at_kp(rect, 10.0, 10.0, 5.0) == 60
    ranked = kp_bars.intervals_by_distance([(1, 2), (5, 6), (8, 9)], 5.5)
    ok = ok and [r[1:] for r in ranked] == [(5.0, 6.0), (8.0, 9.0),
                                            (1.0, 2.0)]
    ok = ok and ranked[0][0] == 0.0 and abs(ranked[1][0] - 2.5) < 1e-9
    ok = ok and kp_bars.intervals_at([(1, 2), (5, 6)], 2.01, 0.02) == [(1.0, 2.0)]
    payload = kp_bars.fire_payload((10.0, [(1, 2)], QColor("red"), 0.0,
                                    {"stale": True}))
    ok = ok and payload[4]["stale"] is True
    ok = ok and kp_bars.fire_payload((10.0, [], QColor("red")))[3] == 0.0
    ok = ok and kp_bars.fire_payload(None) is None

    table = QTableWidget(1, 2)
    table.resize(420, 120)
    table.setColumnWidth(0, 100)
    table.setColumnWidth(1, 204)
    delegate = kp_bars.FireBarDelegate(table)
    table.setItemDelegateForColumn(1, delegate)
    seen = []
    hover = kp_bars.FireBarHover(table, 1, delegate,
                                 lambda row, kp, px: seen.append(kp) or "")
    item = QTableWidgetItem()
    item.setData(ITEM_DATA_USER_ROLE, (10.0, [(2.0, 3.0)], QColor("red"), 0.0))
    table.setItem(0, 1, item)
    cell = table.visualRect(table.model().index(0, 1))
    hit = hover.kp_at(cell.center())
    ok = ok and hit is not None and abs(hit[1] - 5.0) < 0.2
    ok = ok and hover.kp_at(table.visualRect(
        table.model().index(0, 0)).center()) is None
    table.deleteLater()
    return _result("KP bars: hover KP, nearest-first ranges, payloads", ok)


def _memory_layer(kind: str, name: str) -> QgsVectorLayer:
    uri = {"point": "Point?crs=EPSG:4326", "line": "LineString?crs=EPSG:4326",
           "polygon": "Polygon?crs=EPSG:4326", "table": "None"}[kind]
    layer = QgsVectorLayer(uri, name, "memory")
    QgsProject.instance().addMapLayer(layer)
    return layer


def test_add_inputs_picker_and_model_batch() -> bool:
    from ..burial.tabs import input_picker as picker

    points = _memory_layer("point", "Cable crossings KP")
    lines = _memory_layer("line", "Telecom cable as-laid")
    polys = _memory_layer("polygon", "Seabed sediments rev B")
    table = _memory_layer("table", "Lookup")
    ok = picker.geometry_kind(points) == picker.GEOM_POINT
    ok = ok and picker.geometry_kind(lines) == picker.GEOM_LINE
    ok = ok and picker.geometry_kind(polys) == picker.GEOM_POLYGON
    ok = ok and picker.geometry_kind(table) == picker.GEOM_TABLE
    ok = ok and picker.guess_role("Seabed sediments", picker.GEOM_POLYGON) == \
        burial_schema.INPUT_ROLE_SOILS
    ok = ok and picker.guess_role("Telecom cable", picker.GEOM_LINE) == \
        burial_schema.INPUT_ROLE_CROSSINGS_LINES
    ok = ok and picker.guess_role("Boulders", picker.GEOM_POINT) == \
        burial_schema.INPUT_ROLE_OTHER
    ok = ok and picker.role_problem(burial_schema.INPUT_ROLE_SOILS,
                                    picker.GEOM_LINE) != ""
    ok = ok and picker.matches_filter("Telecom cable", "Survey / Cables",
                                      "cable survey", picker.GEOM_LINE, "")
    ok = ok and not picker.matches_filter("Telecom cable", "", "rock",
                                          picker.GEOM_LINE, "")
    ok = ok and not picker.matches_filter("Telecom cable", "", "",
                                          picker.GEOM_LINE, picker.GEOM_POINT)

    dialog = picker.AddInputsDialog(
        [{"layer_id_hint": points.id(), "layer_source": points.source()}])
    visible = [dialog.layer_tree.topLevelItem(i).data(0, ITEM_DATA_USER_ROLE)
               for i in range(dialog.layer_tree.topLevelItemCount())
               if not dialog.layer_tree.topLevelItem(i).isHidden()]
    ok = ok and points.id() not in visible and lines.id() in visible
    dialog.stage_layers([lines, polys])
    ok = ok and dialog.ok_button.isEnabled()
    dialog.stage_table.selectAll()
    dialog.bulk_originator.setText("Fugro")
    dialog._apply_bulk()
    rows = dialog.result_rows()
    ok = ok and [r["role"] for r in rows] == [
        burial_schema.INPUT_ROLE_CROSSINGS_LINES, burial_schema.INPUT_ROLE_SOILS]
    ok = ok and all(r["originator"] == "Fugro" for r in rows)
    # A role that does not fit the geometry blocks OK.
    combo = dialog.stage_table.cellWidget(1, 1)
    combo.setCurrentIndex(combo.findData(
        burial_schema.INPUT_ROLE_CROSSINGS_POINTS))
    ok = ok and not dialog.ok_button.isEnabled()
    dialog.deleteLater()

    store = _store()
    model = PlanModel(store)
    plan_id = model.create_plan("Inputs", "plough")
    log_before = len(store.list_change_log(plan_id))
    ok = ok and model.save_inputs(rows)
    ok = ok and len(model.inputs) == 2
    ok = ok and len(store.list_change_log(plan_id)) == log_before + 1
    soils = next(r for r in model.inputs
                 if r["role"] == burial_schema.INPUT_ROLE_SOILS)
    rule = _rule("r-soil")
    rule["config_json"] = json.dumps({"input_id": soils["input_id"]})
    rule["name"] = "Soft clay"
    ok = ok and model.save_rules([rule])
    usage = model.input_usage()
    ok = ok and usage.get(soils["input_id"]) == ["Exclusion: Soft clay"]
    ok = ok and model.input_status(soils)[0] == "ok"
    QgsProject.instance().removeMapLayer(polys.id())
    ok = ok and model.input_status(soils)[0] == "missing"
    readded = _memory_layer("polygon", "Seabed sediments rev C")
    ok = ok and model.relink_input(soils["input_id"], readded)
    soils = next(r for r in model.inputs if r["input_id"] == soils["input_id"])
    ok = ok and model.input_status(soils)[0] == "ok"
    for layer in (points, lines, table, readded):
        QgsProject.instance().removeMapLayer(layer.id())
    return _result("Add inputs picker: filter, stage, bulk, roles; batch "
                   "register, usage, status, relink", ok)


def test_inputs_dialog_lists_registered() -> bool:
    """Reopening a plan: the Inputs dialog shows the registered inputs on the
    right (editable, removable) and keeps their layers off the left list."""
    from ..burial.tabs import input_picker as picker

    lines = _memory_layer("line", "Telecom cable as-laid")
    polys = _memory_layer("polygon", "Seabed sediments rev B")
    points = _memory_layer("point", "Boulders")
    store = _store()
    model = PlanModel(store)
    model.create_plan("Inputs reopen", "plough")
    ok = model.save_inputs([
        {"role": burial_schema.INPUT_ROLE_CROSSINGS_LINES,
         "layer_name": lines.name(), "layer_source": lines.source(),
         "layer_id_hint": lines.id(), "originator": "Fugro"},
        {"role": burial_schema.INPUT_ROLE_SOILS,
         "layer_name": polys.name(), "layer_source": polys.source(),
         "layer_id_hint": polys.id()},
        {"role": burial_schema.INPUT_ROLE_OTHER,
         "layer_name": "Gone", "layer_source": "/nowhere/gone.shp",
         "layer_id_hint": "gone_id"}])
    reopened = PlanModel(store)
    reopened.load_plan(model.plan_id)
    dialog = picker.AddInputsDialog(reopened.inputs)
    ok = ok and dialog.stage_table.rowCount() == 3
    names = sorted(dialog.stage_table.item(r, 0).text()
                   for r in range(dialog.stage_table.rowCount()))
    ok = ok and names == ["✓ Gone", "✓ Seabed sediments rev B",
                          "✓ Telecom cable as-laid"]
    visible = [dialog.layer_tree.topLevelItem(i).data(0, ITEM_DATA_USER_ROLE)
               for i in range(dialog.layer_tree.topLevelItemCount())
               if not dialog.layer_tree.topLevelItem(i).isHidden()]
    ok = ok and lines.id() not in visible and polys.id() not in visible
    ok = ok and points.id() in visible
    # Nothing changed yet: OK has nothing to save.
    ok = ok and not dialog.ok_button.isEnabled()
    ok = ok and dialog.result_rows() == [] and dialog.removed_input_ids() == []
    # Edit one registered row, remove another, add a new layer.
    row_of = {dialog.stage_table.item(r, 0).text(): r
              for r in range(dialog.stage_table.rowCount())}
    dialog.stage_table.item(row_of["✓ Telecom cable as-laid"], 3).setText("B")
    dialog.stage_table.selectRow(row_of["✓ Gone"])
    dialog._remove_staged()
    dialog.stage_layers([points])
    ok = ok and dialog.ok_button.isEnabled()
    rows = dialog.result_rows()
    removed = dialog.removed_input_ids()
    updated = [r for r in rows if r.get("input_id")]
    ok = ok and len(rows) == 2 and len(updated) == 1
    ok = ok and updated[0]["revision"] == "B" and updated[0]["originator"] == "Fugro"
    ok = ok and len(removed) == 1
    dialog.deleteLater()
    log_before = len(store.list_change_log(reopened.plan_id))
    ok = ok and reopened.save_inputs(rows, remove_ids=removed)
    ok = ok and len(store.list_change_log(reopened.plan_id)) == log_before + 1
    ok = ok and sorted(r["layer_name"] for r in reopened.inputs) == [
        "Boulders", "Seabed sediments rev B", "Telecom cable as-laid"]
    cable = next(r for r in reopened.inputs if r["layer_name"] == lines.name())
    ok = ok and cable["revision"] == "B"
    for layer in (lines, polys, points):
        QgsProject.instance().removeMapLayer(layer.id())
    return _result("Inputs dialog: registered inputs listed on reopen, "
                   "edit/remove/add saved in one entry", ok)


def test_bathymetry_dialog_registers_input() -> bool:
    """Bathymetry is chosen in the two-pane dialog, saved as the plan's
    bathymetry input with register details, reopened as configured, and
    listed with the other registered inputs."""
    from ..burial.tabs.bathy_dialog import (BathymetryDialog, MODE_CONTOURS,
                                            guess_depth_field)
    from ..burial.tabs.inputs_tab import InputsTab

    ok = guess_depth_field(["name", "DEPTH_M"]) == "DEPTH_M"
    ok = ok and guess_depth_field(["id", "z"]) == "z"
    ok = ok and guess_depth_field(["id", "name"]) == ""
    minor = QgsVectorLayer("LineString?crs=EPSG:4326&field=name:string"
                           "&field=depth_m:double", "Contours minor", "memory")
    major = QgsVectorLayer("LineString?crs=EPSG:4326&field=elev:double",
                           "Contours major", "memory")
    points = _memory_layer("point", "Soundings")
    for layer in (minor, major):
        QgsProject.instance().addMapLayer(layer)
    store = _store()
    model = PlanModel(store)
    model.create_plan("Bathy", "plough")

    dialog = BathymetryDialog(None, model.depth_config())
    ok = ok and not dialog.ok_button.isEnabled()      # nothing chosen yet
    dialog.set_mode(MODE_CONTOURS)
    shown = [dialog.layer_tree.topLevelItem(i).text(0)
             for i in range(dialog.layer_tree.topLevelItemCount())
             if not dialog.layer_tree.topLevelItem(i).isHidden()]
    ok = ok and "Contours minor" in shown and "Soundings" not in shown
    dialog.use_layers([minor, major])
    ok = ok and dialog.slots[0].field.currentField() == "depth_m"
    ok = ok and dialog.slots[1].field.currentField() == "elev"
    ok = ok and dialog.ok_button.isEnabled()
    dialog.originator.setText("Fugro")
    dialog.revision.setText("C")
    row = dialog.result_row()
    dialog.deleteLater()
    config = json.loads(row["config_json"])
    ok = ok and row["role"] == burial_schema.INPUT_ROLE_BATHY
    ok = ok and config["mode"] == MODE_CONTOURS
    ok = ok and [c["depth_field"] for c in config["contour_layers"]] == [
        "depth_m", "elev"]
    ok = ok and model.save_input(row)
    depth = model.depth_config()
    ok = ok and [c["layer_id"] for c in depth.contour_layers] == [
        minor.id(), major.id()]

    saved = next(r for r in model.inputs
                 if r["role"] == burial_schema.INPUT_ROLE_BATHY)
    ok = ok and saved["originator"] == "Fugro" and saved["revision"] == "C"
    again = BathymetryDialog(saved, model.depth_config())
    ok = ok and again.contour_radio.isChecked()
    ok = ok and [s.layer.id() if s.layer else "" for s in again.slots] == [
        minor.id(), major.id()]
    ok = ok and again.originator.text() == "Fugro"
    again.deleteLater()

    tab = InputsTab(model, lambda: None)
    roles = [tab.inputs_table.item(r, 0).text()
             for r in range(tab.inputs_table.rowCount())]
    ok = ok and roles == ["Bathymetry"]
    ok = ok and "Contours (2 layer(s))" in tab.bathy_summary.text()
    tab.deleteLater()
    for layer in (minor, major, points):
        QgsProject.instance().removeMapLayer(layer.id())
    return _result("Bathymetry dialog: contour slots, depth-field guess, "
                   "register details, reopen, listed as an input", ok)


def test_profile_overlay_filters_and_hazards() -> bool:
    from ..burial.profile_widget import BurialProfileWidget

    widget = BurialProfileWidget()
    widget._overlay_visible = {key: True for key in widget._overlay_visible}
    widget._ii_hidden = set()
    widget._hazard_levels_hidden = set()
    widget._hazard_style = "full"  # never the persisting setter in tests
    widget._apply_overlay_visibility()
    widget.set_scope(0.0, 10.0)
    ctx = generation.ResolutionContext(
        insufficient=[Interval(1.0, 2.0), Interval(5.0, 6.0),
                      Interval(8.0, 8.5)],
        rule_nodata={"cross": [Interval(1.0, 2.0)],
                     "depth": [Interval(5.0, 6.0)]})
    widget.set_overlays(ctx, {"cross": "Cross slope", "depth": "Depth"})
    sources = widget.insufficient_sources()
    ok = [s[0] for s in sources] == ["cross", "depth", "__other__"]
    ok = ok and len(widget._regions["insufficient"].ranges()) == 3
    labels = widget.overlay_labels_at(1.5)
    ok = ok and "  no data: Cross slope" in labels
    # Hide the cross-slope criterion's no-data only.
    widget._ii_hidden = {"cross"}
    widget._apply_insufficient_filter()
    ranges = [(a, b) for a, b, _l in widget._regions["insufficient"].ranges()]
    ok = ok and ranges == [(5.0, 6.0), (8.0, 8.5)]
    ok = ok and "Insufficient Information" not in widget.overlay_labels_at(1.5)
    widget._ii_hidden = {"cross", "__other__"}
    widget._apply_insufficient_filter()
    ranges = [(a, b) for a, b, _l in widget._regions["insufficient"].ranges()]
    ok = ok and ranges == [(5.0, 6.0)]
    # Hiding an overlay kind removes it from the readout too.
    ctx.excluded = []
    widget._overlay_visible["insufficient"] = False
    widget._apply_overlay_visibility()
    ok = ok and "Insufficient Information" not in widget.overlay_labels_at(5.5)
    ok = ok and "hidden" in widget._overlay_button.text()
    # Hazards: strip ranges, level filter.
    widget.set_hazards([
        {"kp": 3.0, "end_kp": 3.0, "label": "Boulder", "risk": "high"},
        {"kp": 4.0, "end_kp": 4.5, "label": "Sandwave", "risk": "low"}])
    ok = ok and len(widget._hazard_band.ranges()) == 2
    widget._hazard_levels_hidden = {"low"}
    widget._apply_hazards()
    ok = ok and [r[2] for r in widget._hazard_band.ranges()] == [
        "Hazard: Boulder [High]"]
    # Display style: full-height bands (default) or the top strip.
    ok = ok and len(widget._hazard_full.ranges()) == 1
    ok = ok and widget._hazard_full.isVisible() is not False
    ok = ok and not widget._hazard_vb.isVisible()
    ok = ok and "Hazard: Boulder [High]" in widget.overlay_labels_at(3.0)
    widget._hazard_style = "strip"
    widget._apply_overlay_visibility()
    ok = ok and not widget._hazard_full.isVisible()
    ok = ok and "Hazard: Boulder [High]" in widget.overlay_labels_at(3.0)
    widget._hazard_style = "full"
    widget._apply_overlay_visibility()
    widget.clear()
    ok = ok and not widget._hazard_band.ranges()
    ok = ok and not widget._hazard_full.ranges()
    widget.deleteLater()
    return _result("profile overlays: per-criterion II filter, visibility, "
                   "hazard strip", ok)


def test_target_ranges_in_model_and_ground_overlay() -> bool:
    route, _da = _route()
    store = _store()
    model = PlanModel(store)
    plan_id = model.create_plan("Targets", "plough")
    model.route = route
    ok = model.update_plan({"scope_start_kp": 0.0, "scope_end_kp": 20.0},
                           reason="scope")
    ok = ok and model.update_targets(1.5, [
        {"start_kp": 5.0, "end_kp": 8.0, "depth_m": 3.0, "notes": "lane"}])
    ok = ok and model.target_default() == 1.5
    ok = ok and model.target_runs() == [(0.0, 5.0, 1.5), (5.0, 8.0, 3.0),
                                        (8.0, 20.0, 1.5)]
    again = PlanModel(store)
    again.load_plan(plan_id)
    ok = ok and again.target_ranges()[0]["notes"] == "lane"
    units = [{"unit_id": "u1", "start_kp": 0.0, "end_kp": 20.0,
              "top_m": 0.0, "base_m": 2.0, "soil_class": "SAND"},
             {"unit_id": "u2", "start_kp": 0.0, "end_kp": 20.0,
              "top_m": 2.0, "base_m": None, "soil_class": "CLAY"}]
    rows = map_layers._ground_layer_rows(again.plan, units, [], route, None)
    target = [(round(r["start_kp"], 3), round(r["end_kp"], 3), r["depth_m"],
               r["soil_class"]) for r in rows if r["horizon"] == "target"]
    ok = ok and (5.0, 8.0, 3.0, "CLAY") in target
    ok = ok and any(t[2] == 1.5 and t[3] == "SAND" for t in target)
    # Clearing the ranges drops the params key.
    ok = ok and model.update_targets(None, [])
    ok = ok and "target_burial_ranges" not in json.loads(
        model.plan["params_json"]) and model.target_default() is None
    return _result("target ranges: model storage + per-run ground horizon", ok,
                   str(target))


def test_dock_builds_and_survives_project_reload() -> bool:
    from qgis.gui import QgsMapCanvas
    from qgis.PyQt.QtCore import QCoreApplication
    from qgis.PyQt.QtWidgets import QMessageBox

    from ..burial import burial_dock
    from ..burial import store as store_module

    class _Iface:
        def __init__(self):
            self._canvas = QgsMapCanvas()

        def mapCanvas(self):
            return self._canvas

        def layerTreeView(self):
            return None

    saved = {name: getattr(QMessageBox, name)
             for name in ("warning", "information", "question")}
    for name in saved:
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: 0))
    try:
        store = _store()
        store_module.set_project_gpkg_path(store.gpkg_path)
        model = PlanModel(store)
        plan_id = model.create_plan("Dock", "plough")
        model.save_rules([_rule("r1")])
        dock = burial_dock.BurialPlannerDock(_Iface())
        QCoreApplication.processEvents()
        ok = dock.model.plan_id == plan_id
        # Every tab refreshes without raising on a plan with no route.
        for tab in (dock.inputs_tab, dock.rules_tab, dock.risk_tab,
                    dock.profile_tab):
            tab.refresh()
        ok = ok and "Not evaluated" in dock.rules_tab.status_label.text()
        dock.model.save_analysis(
            generation.ResolutionContext(rule_hits={"r1": [Interval(1, 2)]}),
            [], dock.model.gen_params(), dock.model.rules, "done")
        QCoreApplication.processEvents()
        ok = ok and "last recompute" in dock.rules_tab.status_label.text()
        dock._project_reloaded()
        QCoreApplication.processEvents()
        ok = ok and dock.model.plan_id == plan_id
        ok = ok and bool(dock.model.display_context().rule_hits)
        dock._project_layers_changed()
        QCoreApplication.processEvents()
        dock.shutdown()
        dock.deleteLater()
    finally:
        for name, value in saved.items():
            setattr(QMessageBox, name, value)
    return _result("dock builds, shows the stored run, survives project "
                   "reload", ok)


def run_all() -> list:
    return [
        test_analysis_persists_across_reload(),
        test_legacy_generation_status(),
        test_schema_v10_adds_analysis_table(),
        test_depth_fingerprint_is_content_based(),
        test_model_relinks_bathymetry_and_reports_reasons(),
        test_kp_bar_helpers_and_hover(),
        test_add_inputs_picker_and_model_batch(),
        test_inputs_dialog_lists_registered(),
        test_bathymetry_dialog_registers_input(),
        test_profile_overlay_filters_and_hazards(),
        test_target_ranges_in_model_and_ground_overlay(),
        test_dock_builds_and_survives_project_reload(),
    ]
