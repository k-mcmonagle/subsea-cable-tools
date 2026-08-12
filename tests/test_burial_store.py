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

from ..burial import change_log, schema
from ..burial.store import BurialStore

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
            "rpl_name": "Route", "rpl_gpkg_path": "", "rpl_fingerprint": "fp",
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
    return _result("plan/input/rule/event/section/generation round trip", ok)


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
    return _result("change log append + rollback restores prior state", ok)


def run_all() -> list:
    return [
        test_create_and_migrate(),
        test_plan_round_trip(),
        test_duplicate_deep_copy(),
        test_change_log_and_rollback(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
