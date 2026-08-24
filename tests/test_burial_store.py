# -*- coding: utf-8 -*-
"""Checks for the Burial Planner GeoPackage store (requires the QGIS API).

gpkg round-trip, duplicate-plan deep copy, change-log append + rollback
restore, schema create/migrate/backup (spec §16).
"""

from __future__ import annotations

import json
import os
import tempfile
import time

from qgis.core import QgsProject

from ..burial import change_log, generation, schema
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..workbench.rules_engine import Interval

_COUNTER = [0]


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _store() -> BurialStore:
    _COUNTER[0] += 1
    name = f"bp_store_{os.getpid()}_{int(time.time() * 1000)}_{_COUNTER[0]}.gpkg"
    store = BurialStore(os.path.join(tempfile.gettempdir(), name),
                        QgsProject.instance().transformContext())
    store.migrate()
    return store


def _plan_row(name="Plan A"):
    return {"plan_id": schema.new_id(), "name": name, "description": "",
            "notes": "", "method": "plough", "rpl_id": "rpl-1",
            "rpl_name": "Route", "rpl_revision": "Rev 4",
            "rpl_gpkg_path": "", "rpl_fingerprint": "fp",
            "scope_start_kp": 0.0, "scope_end_kp": 10.0, "direction": 1,
            "target_burial_m": None, "params_json": "{}"}


def test_create_and_migrate() -> bool:
    store = _store()
    ok = store.exists()
    ok = ok and store.read_meta().get("schema_version") == str(schema.SCHEMA_VERSION)
    for table in schema.REGISTRY_TABLES:
        ok = ok and store._table_exists(table)
    # simulate an older stamp: migrate() re-advances and backs up
    store.write_meta("schema_version", "0")
    store.migrate()
    ok = ok and store.read_meta().get("schema_version") == str(schema.SCHEMA_VERSION)
    stem, ext = os.path.splitext(store.gpkg_path)
    ok = ok and os.path.exists(f"{stem}.migrate_v0.bak{ext}")
    return _result("create + migrate + backup-before-migrate", ok)


def test_plan_round_trip() -> bool:
    store = _store()
    plan_id = store.save_plan(_plan_row())
    plan = store.get_plan(plan_id)
    ok = plan is not None and plan.get("status") == schema.PLAN_STATUS_DRAFT
    ok = ok and plan.get("rpl_revision") == "Rev 4"
    ok = ok and abs(float(plan.get("scope_end_kp")) - 10.0) < 1e-9
    store.save_input({"input_id": schema.new_id(), "plan_id": plan_id,
                      "role": "soils_polygons", "layer_name": "soils",
                      "layer_source": "x.gpkg|layername=soils",
                      "layer_id_hint": "", "config_json": "{}",
                      "originator": "Org", "revision": "B", "status": "current",
                      "received_utc": "", "quality": "high", "notes": ""})
    store.save_rules(plan_id, [
        {"name": "r1", "enabled": 1, "kind": "manual", "action": "exclude",
         "risk_level": 0, "criterion_class": "project", "source_ref": "",
         "methods_json": "[]", "config_json": "{}", "notes": ""},
        {"name": "r2", "enabled": 1, "kind": "manual", "action": "exclude",
         "risk_level": 0, "criterion_class": "screening", "source_ref": "",
         "methods_json": "[]", "config_json": "{}", "notes": ""},
    ])
    rules = store.list_rules(plan_id)
    ok = ok and [r["seq"] for r in rules] == [0, 1]
    store.save_events(plan_id, [
        {"event_id": schema.new_id(), "generation_id": "", "seq": 0,
         "event_type": "BURIAL_START", "kp": 1.0, "end_kp": None, "lat": 50.0,
         "lon": 0.0, "depth_m": 10.0, "source": "auto", "status": "candidate",
         "locked": 0, "notes": ""},
    ])
    ok = ok and len(store.list_events(plan_id)) == 1
    store.save_sections(plan_id, [
        {"section_id": schema.new_id(), "kind": "burial", "start_kp": 1.0,
         "end_kp": 5.0, "length_km": 4.0, "start_event_id": "",
         "end_event_id": "", "state": "candidate", "conclusion": "",
         "confidence": "", "reason_json": "{}", "method": "",
         "grade_in_m": None, "grade_out_m": None, "target_burial_m": None,
         "notes": ""},
    ])
    ok = ok and len(store.list_sections(plan_id)) == 1
    gen_id = store.save_generation({"plan_id": plan_id, "active": 1,
                                    "rules_snapshot_json": "[]",
                                    "params_json": "{}",
                                    "inputs_fingerprint_json": "{}",
                                    "summary_json": "{}",
                                    "proposal_diff_json": "{}"})
    active = store.active_generation(plan_id)
    ok = ok and active is not None and active.get("generation_id") == gen_id
    # a second active generation deactivates the first
    gen2 = store.save_generation({"plan_id": plan_id, "active": 1,
                                  "rules_snapshot_json": "[]", "params_json": "{}",
                                  "inputs_fingerprint_json": "{}",
                                  "summary_json": "{}", "proposal_diff_json": "{}"})
    actives = [g for g in store.list_generations(plan_id) if int(g.get("active") or 0)]
    ok = ok and len(actives) == 1 and actives[0]["generation_id"] == gen2
    path_id = store.save_path_result({
        "plan_id": plan_id, "algorithm_version": "1",
        "config_json": "{}", "fingerprints_json": "{}",
        "summary_json": "{}", "tool_path_wkt": "LINESTRING (0 0, 1 1)",
        "barge_track_wkt": "", "diagnostics_json": "[]"})
    path = store.get_path_result(plan_id)
    ok = ok and path is not None and path.get("path_id") == path_id
    layback_id = store.save_layback_profile({
        "name": "constant", "points_json": "[[0,100]]",
        "outside_mode": "hold", "source_ref": "test", "notes": ""})
    ok = ok and store.get_layback_profile(layback_id).get("name") == "constant"
    vessel_id = store.save_vessel({
        "name": "CLV Test", "min_turn_radius_m": 950.0,
        "footprint_wkt": "", "source_ref": "test", "notes": ""})
    vessel = store.get_vessel(vessel_id)
    ok = ok and vessel is not None \
        and float(vessel.get("min_turn_radius_m")) == 950.0
    store.delete_vessel(vessel_id)
    ok = ok and store.get_vessel(vessel_id) is None
    return _result("plan/input/rule/event/section/generation round trip", ok)


def test_migrate_v1_adds_rpl_revision() -> bool:
    _COUNTER[0] += 1
    name = f"bp_v1_{os.getpid()}_{int(time.time() * 1000)}_{_COUNTER[0]}.gpkg"
    store = BurialStore(os.path.join(tempfile.gettempdir(), name),
                        QgsProject.instance().transformContext())
    old_plan_fields = [field for field in schema.PLAN_FIELDS
                       if field[0] != "rpl_revision"]
    plan = _plan_row()
    plan.pop("rpl_revision", None)
    store._write_table_rows(schema.TABLE_META, schema.META_FIELDS, [
        {"key": "schema_version", "value": "1"},
        {"key": "created_utc", "value": "2026-01-01T00:00:00Z"},
    ])
    store._write_table_rows(schema.TABLE_PLAN, old_plan_fields, [plan])
    store.migrate()
    migrated = store.get_plan(plan["plan_id"]) or {}
    stem, ext = os.path.splitext(store.gpkg_path)
    ok = store.read_meta().get("schema_version") == str(schema.SCHEMA_VERSION)
    ok = ok and "rpl_revision" in migrated
    ok = ok and os.path.exists(f"{stem}.migrate_v1.bak{ext}")
    return _result("migrate v1 adds RPL revision snapshot", ok)


def test_duplicate_deep_copy() -> bool:
    store = _store()
    plan_id = store.save_plan(_plan_row("Original"))
    input_id = store.save_input({"plan_id": plan_id, "role": "soils_polygons",
                                 "layer_name": "soils", "layer_source": "s",
                                 "layer_id_hint": "", "config_json": "{}"})
    store.save_rules(plan_id, [{
        "name": "soils rule", "enabled": 1, "kind": "polygon_class",
        "action": "exclude", "risk_level": 0, "criterion_class": "project",
        "source_ref": "", "methods_json": "[]",
        "config_json": json.dumps({"input_id": input_id, "attribute": "S"}),
        "notes": ""}])
    store.save_events(plan_id, [{
        "event_id": "ev-1", "generation_id": "g", "seq": 0,
        "event_type": "BURIAL_START", "kp": 1.0, "end_kp": None,
        "lat": None, "lon": None, "depth_m": None, "source": "manual",
        "status": "confirmed", "locked": 1, "notes": ""}])
    store.save_sections(plan_id, [{
        "section_id": "sec-1", "kind": "burial", "start_kp": 1.0, "end_kp": 2.0,
        "length_km": 1.0, "start_event_id": "ev-1", "end_event_id": "",
        "state": "candidate", "conclusion": "", "confidence": "",
        "reason_json": "{}", "method": "", "grade_in_m": None,
        "grade_out_m": None, "target_burial_m": None, "notes": ""}])

    copy_id = store.duplicate_plan(plan_id, "Copy")
    ok = copy_id != plan_id
    copy = store.get_plan(copy_id)
    ok = ok and copy is not None and copy.get("supersedes_id") == plan_id
    copy_inputs = store.list_inputs(copy_id)
    copy_rules = store.list_rules(copy_id)
    copy_events = store.list_events(copy_id)
    copy_sections = store.list_sections(copy_id)
    ok = ok and len(copy_inputs) == 1 and copy_inputs[0]["input_id"] != input_id
    ok = ok and len(copy_rules) == 1
    config = json.loads(copy_rules[0]["config_json"])
    ok = ok and config.get("input_id") == copy_inputs[0]["input_id"]  # remapped
    ok = ok and len(copy_events) == 1 and copy_events[0]["event_id"] != "ev-1"
    ok = ok and int(copy_events[0]["locked"] or 0) == 1
    ok = ok and copy_sections[0]["start_event_id"] == copy_events[0]["event_id"]
    # originals untouched
    ok = ok and len(store.list_events(plan_id)) == 1
    ok = ok and store.list_events(plan_id)[0]["event_id"] == "ev-1"
    return _result("duplicate plan: deep copy, id remap, lineage", ok)


def test_change_log_and_rollback() -> bool:
    store = _store()
    plan_id = store.save_plan(_plan_row())
    event = {"event_id": "e1", "plan_id": plan_id, "generation_id": "",
             "seq": 0, "event_type": "BURIAL_START", "kp": 1.0, "end_kp": None,
             "lat": None, "lon": None, "depth_m": None, "source": "manual",
             "status": "candidate", "locked": 0, "notes": ""}
    store.save_events(plan_id, [event])
    store.append_change(plan_id, change_log.ACTION_ADD_EVENT, "e1",
                        before={schema.TABLE_EVENT: []},
                        after={schema.TABLE_EVENT: [event]})
    moved = dict(event)
    moved["kp"] = 2.5
    store.save_events(plan_id, [moved])
    move_entry = store.append_change(
        plan_id, change_log.ACTION_MOVE_EVENT, "e1",
        before={schema.TABLE_EVENT: [event]},
        after={schema.TABLE_EVENT: [moved]}, reason="test move")
    ok = abs(float(store.list_events(plan_id)[0]["kp"]) - 2.5) < 1e-9

    store.rollback_to(plan_id, move_entry["change_id"])
    events = store.list_events(plan_id)
    ok = ok and len(events) == 1 and abs(float(events[0]["kp"]) - 1.0) < 1e-9
    log = store.list_change_log(plan_id)
    ok = ok and log[-1]["action"] == change_log.ACTION_ROLLBACK  # appended, not erased
    ok = ok and len(log) == 3
    # The rolled-back move is no longer the latest effective action; a
    # conventional second Undo can therefore find the preceding add.
    latest = change_log.latest_effective_entry(log)
    ok = ok and latest is not None and latest["action"] == change_log.ACTION_ADD_EVENT
    return _result("change log append + rollback restores prior state", ok)


def test_plan_builder_merge_insert_and_undo() -> bool:
    store = _store()
    plan_id = store.save_plan(_plan_row())
    params = generation.GenParams(0.0, 10.0, direction=1, method="plough")
    rule = {
        "rule_id": "r", "name": "excluded", "enabled": 1,
        "kind": "manual", "action": "exclude", "risk_level": 0,
        "criterion_class": "project", "methods_json": "[]",
        "config_json": "{}",
    }
    out = generation.generate(
        params,
        [generation.RuleAcquisition(rule, [Interval(2.0, 4.0),
                                           Interval(6.0, 8.0)])],
        plan_id=plan_id)
    store.save_events(plan_id, out.events)
    store.save_sections(plan_id, out.sections)

    model = PlanModel(store)
    ok = model.load_plan(plan_id)
    skip_ids = [section["section_id"] for section in model.sections
                if section["kind"] == schema.SECTION_SKIP]
    ok = ok and model.merge_sections(skip_ids, "join nearby skips")
    merged_skips = [section for section in model.sections
                    if section["kind"] == schema.SECTION_SKIP]
    ok = ok and len(merged_skips) == 1
    ok = ok and abs(merged_skips[0]["start_kp"] - 2.0) < 1e-9
    ok = ok and abs(merged_skips[0]["end_kp"] - 8.0) < 1e-9

    undone = model.undo_last_builder_edit()
    ok = ok and undone is not None and undone["action"] == change_log.ACTION_MERGE_SECTIONS
    restored_skips = [section for section in model.sections
                      if section["kind"] == schema.SECTION_SKIP]
    ok = ok and len(restored_skips) == 2

    first_skip = restored_skips[0]
    ok = ok and model.insert_opposite_section(
        first_skip["section_id"], 2.5, 3.0, "manual plough candidate")
    inserted = [section for section in model.sections
                if section["kind"] == schema.SECTION_BURIAL
                and abs(section["start_kp"] - 2.5) < 1e-9
                and abs(section["end_kp"] - 3.0) < 1e-9]
    ok = ok and len(inserted) == 1
    undone = model.undo_last_builder_edit()
    ok = ok and undone is not None and undone["action"] == change_log.ACTION_INSERT_SECTION
    ok = ok and len([section for section in model.sections
                     if section["kind"] == schema.SECTION_SKIP]) == 2
    return _result("Plan Builder merge/insert persist and undo atomically", ok)


def test_dismiss_insufficient_persist_and_undo() -> bool:
    """Deleting an II section dismisses its no-data range: it becomes a
    tagged skip, survives reopening the plan, and undoes atomically."""
    store = _store()
    plan_id = store.save_plan(_plan_row())
    params = generation.GenParams(0.0, 10.0, direction=1, method="plough")
    rule = {
        "rule_id": "r", "name": "excluded", "enabled": 1,
        "kind": "manual", "action": "exclude", "risk_level": 0,
        "criterion_class": "project", "methods_json": "[]",
        "config_json": "{}",
    }
    out = generation.generate(
        params,
        [generation.RuleAcquisition(rule, [Interval(2.0, 4.0)],
                                    nodata=[Interval(6.0, 8.0)])],
        plan_id=plan_id)
    summary = dict(out.summary)
    summary["context"] = generation.context_to_dict(out)
    store.save_generation({"plan_id": plan_id, "active": 1,
                           "rules_snapshot_json": "[]",
                           "params_json": json.dumps(params.to_dict()),
                           "inputs_fingerprint_json": "{}",
                           "summary_json": json.dumps(summary),
                           "proposal_diff_json": "{}"})
    store.save_events(plan_id, out.events)
    store.save_sections(plan_id, out.sections)

    model = PlanModel(store)
    ok = model.load_plan(plan_id)
    ii = [s for s in model.sections
          if s["kind"] == schema.SECTION_INSUFFICIENT]
    ok = ok and len(ii) == 1
    events_before = len(model.events)
    ok = ok and model.delete_section(ii[0]["section_id"], "no bathy coverage")
    ok = ok and not any(s["kind"] == schema.SECTION_INSUFFICIENT
                        for s in model.sections)
    ok = ok and len(model.events) == events_before  # no events touched
    dismissed_skip = next(
        (s for s in model.sections if s["kind"] == schema.SECTION_SKIP
         and abs(float(s["start_kp"]) - 6.0) < 1e-6
         and abs(float(s["end_kp"]) - 8.0) < 1e-6), None)
    ok = ok and dismissed_skip is not None
    ok = ok and json.loads(
        dismissed_skip["reason_json"]).get("insufficient_dismissed") is True
    ok = ok and "dismissed" in (dismissed_skip.get("notes") or "")
    # Sections still tile the scope exactly.
    ordered = sorted(model.sections, key=lambda s: float(s["start_kp"]))
    ok = ok and abs(float(ordered[0]["start_kp"]) - 0.0) < 1e-6
    ok = ok and abs(float(ordered[-1]["end_kp"]) - 10.0) < 1e-6
    ok = ok and all(abs(float(a["end_kp"]) - float(b["start_kp"])) < 1e-6
                    for a, b in zip(ordered, ordered[1:]))
    # The dismissal is persisted with the plan and the stored context:
    # a reopened plan shows no II section and remembers the dismissal.
    stored = json.loads(model.plan.get("params_json") or "{}")
    ok = ok and stored.get("dismissed_insufficient") == [[6.0, 8.0]]
    model2 = PlanModel(store)
    ok = ok and model2.load_plan(plan_id)
    ok = ok and not any(s["kind"] == schema.SECTION_INSUFFICIENT
                        for s in model2.sections)
    ok = ok and not model2.context.insufficient
    ok = ok and model2.gen_params().dismissed_insufficient == \
        [(6.0, 8.0, schema.SECTION_SKIP)]

    # Undo restores the II section, the plan params and the context.
    undone = model.undo_last_builder_edit()
    ok = ok and undone is not None
    ok = ok and undone["action"] == change_log.ACTION_DISMISS_INSUFFICIENT
    restored = [s for s in model.sections
                if s["kind"] == schema.SECTION_INSUFFICIENT]
    ok = ok and len(restored) == 1
    ok = ok and abs(float(restored[0]["start_kp"]) - 6.0) < 1e-6
    stored_after = json.loads(model.plan.get("params_json") or "{}")
    ok = ok and not stored_after.get("dismissed_insufficient")
    ok = ok and len(model.context.insufficient) == 1
    return _result("dismiss II persists, reopens dismissed and undoes", ok)


def test_resolve_insufficient_as_burial_persist_and_undo() -> bool:
    """Resolving an II section as burial merges it into the abutting burial
    sections, tags the result, persists the resolution (surviving a normal
    re-Generate) and undoes atomically."""
    store = _store()
    plan_id = store.save_plan(_plan_row())
    params = generation.GenParams(0.0, 10.0, direction=1, method="plough")
    rule = {
        "rule_id": "r", "name": "excluded", "enabled": 1,
        "kind": "manual", "action": "exclude", "risk_level": 0,
        "criterion_class": "project", "methods_json": "[]",
        "config_json": "{}",
    }
    acquisitions = [generation.RuleAcquisition(
        rule, [Interval(2.0, 4.0)], nodata=[Interval(6.0, 8.0)])]
    out = generation.generate(params, acquisitions, plan_id=plan_id)
    summary = dict(out.summary)
    summary["context"] = generation.context_to_dict(out)
    store.save_generation({"plan_id": plan_id, "active": 1,
                           "rules_snapshot_json": "[]",
                           "params_json": json.dumps(params.to_dict()),
                           "inputs_fingerprint_json": "{}",
                           "summary_json": json.dumps(summary),
                           "proposal_diff_json": "{}"})
    store.save_events(plan_id, out.events)
    store.save_sections(plan_id, out.sections)

    model = PlanModel(store)
    ok = model.load_plan(plan_id)
    ii = [s for s in model.sections
          if s["kind"] == schema.SECTION_INSUFFICIENT]
    ok = ok and len(ii) == 1
    ok = ok and model.resolve_insufficient_sections(
        [ii[0]["section_id"]], schema.SECTION_BURIAL, "engineer judgement")
    ok = ok and not any(s["kind"] == schema.SECTION_INSUFFICIENT
                        for s in model.sections)
    # [4,6] + [6,8] + [8,10] coalesce into one burial section, tagged.
    resolved = next(
        (s for s in model.sections if s["kind"] == schema.SECTION_BURIAL
         and abs(float(s["start_kp"]) - 4.0) < 1e-6
         and abs(float(s["end_kp"]) - 10.0) < 1e-6), None)
    ok = ok and resolved is not None
    reason = json.loads(resolved["reason_json"]) if resolved else {}
    ok = ok and reason.get("insufficient_override") == [[6.0, 8.0]]
    ok = ok and "resolved as" in (resolved.get("notes") or "")
    # Sections still tile the scope exactly.
    ordered = sorted(model.sections, key=lambda s: float(s["start_kp"]))
    ok = ok and abs(float(ordered[0]["start_kp"]) - 0.0) < 1e-6
    ok = ok and abs(float(ordered[-1]["end_kp"]) - 10.0) < 1e-6
    ok = ok and all(abs(float(a["end_kp"]) - float(b["start_kp"])) < 1e-6
                    for a, b in zip(ordered, ordered[1:]))
    # Persisted with the burial kind; a reopened plan keeps the resolution.
    stored = json.loads(model.plan.get("params_json") or "{}")
    ok = ok and stored.get("dismissed_insufficient") == \
        [[6.0, 8.0, schema.SECTION_BURIAL]]
    model2 = PlanModel(store)
    ok = ok and model2.load_plan(plan_id)
    ok = ok and not any(s["kind"] == schema.SECTION_INSUFFICIENT
                        for s in model2.sections)
    ok = ok and not model2.context.insufficient

    # A normal re-Generate honours the resolution: the range stays burial
    # (never II) and the tag survives.
    regen = generation.generate(
        model2.gen_params(), acquisitions,
        existing_events=[dict(e) for e in model2.events],
        previous_sections=[dict(s) for s in model2.sections],
        plan_id=plan_id)
    ok = ok and not any(s["kind"] == schema.SECTION_INSUFFICIENT
                        for s in regen.sections)
    regen_burial = next(
        (s for s in regen.sections if s["kind"] == schema.SECTION_BURIAL
         and float(s["start_kp"]) <= 6.5 <= float(s["end_kp"])), None)
    ok = ok and regen_burial is not None
    ok = ok and json.loads(regen_burial["reason_json"]).get(
        "insufficient_override") == [[6.0, 8.0]]

    # Undo restores the II section, the plan params and the context.
    undone = model.undo_last_builder_edit()
    ok = ok and undone is not None
    ok = ok and undone["action"] == change_log.ACTION_RESOLVE_INSUFFICIENT
    restored = [s for s in model.sections
                if s["kind"] == schema.SECTION_INSUFFICIENT]
    ok = ok and len(restored) == 1
    ok = ok and abs(float(restored[0]["start_kp"]) - 6.0) < 1e-6
    stored_after = json.loads(model.plan.get("params_json") or "{}")
    ok = ok and not stored_after.get("dismissed_insufficient")
    ok = ok and len(model.context.insufficient) == 1
    ok = ok and any(s["kind"] == schema.SECTION_BURIAL
                    and abs(float(s["end_kp"]) - 6.0) < 1e-6
                    for s in model.sections)
    return _result("resolve II as burial persists, regenerates and undoes", ok)


def test_plan_profile_persistence() -> bool:
    from ..burial.profile_data import PlanProfile

    store = _store()
    plan_id = store.save_plan(_plan_row("Profiled"))
    profile = PlanProfile(
        step_m=25.0, cross_offset_m=40.0,
        scope_start_kp=0.0, scope_end_kp=10.0,
        route_fingerprint="route-fp", depth_fingerprint="depth-fp",
        sampled_utc="2026-08-13T10:00:00Z",
        kps=[0.0, 5.0, 10.0], depths=[100.0, None, 140.0],
        port_depths=[99.0, None, 139.0], stbd_depths=[101.0, None, 141.0])
    store.save_plan_profile(profile.to_row(plan_id))
    loaded = PlanProfile.from_row(store.get_plan_profile(plan_id))
    ok = loaded is not None and loaded.kps == [0.0, 5.0, 10.0]
    ok = ok and loaded.depths == [100.0, None, 140.0]
    ok = ok and loaded.cross_offset_m == 40.0 and loaded.has_cross()

    # One profile per plan: saving again replaces, never accumulates.
    profile.step_m = 10.0
    store.save_plan_profile(profile.to_row(plan_id))
    rows = [r for r in store.read_table(schema.TABLE_PROFILE)
            if r.get("plan_id") == plan_id]
    ok = ok and len(rows) == 1
    ok = ok and PlanProfile.from_row(rows[0]).step_m == 10.0

    copy_id = store.duplicate_plan(plan_id, "Profiled copy")
    copied = PlanProfile.from_row(store.get_plan_profile(copy_id))
    ok = ok and copied is not None and copied.kps == loaded.kps

    store.delete_plan(plan_id)
    ok = ok and store.get_plan_profile(plan_id) is None
    ok = ok and store.get_plan_profile(copy_id) is not None
    return _result("plan profile persists, replaces, copies and deletes", ok)


def run_all() -> list:
    return [
        test_create_and_migrate(),
        test_migrate_v1_adds_rpl_revision(),
        test_plan_round_trip(),
        test_duplicate_deep_copy(),
        test_change_log_and_rollback(),
        test_plan_builder_merge_insert_and_undo(),
        test_dismiss_insufficient_persist_and_undo(),
        test_resolve_insufficient_as_burial_persist_and_undo(),
        test_plan_profile_persistence(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
