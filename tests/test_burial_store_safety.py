# -*- coding: utf-8 -*-
"""Checks that the Burial Planner store is safe with files it does not own
and never leaves half-written plans (requires the QGIS API).

- probing an ordinary GeoPackage (Open existing plans, project-open layer
  repair) leaves it byte-for-byte in its journal mode, with no sidecar files
  and no open handle;
- delete / duplicate cover every plan-keyed table (schema.PLAN_CHILD_TABLES)
  in SQL mode and in the legacy writer mode, and the legacy mode orders its
  writes so an interrupted delete / duplicate is retryable or invisible;
- a lock held by another session raises instead of silently switching the
  store to the non-atomic legacy mode;
- creating / editing a plan commits the header and its change-log entry
  together, and a failure is reported through storeError.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from qgis.core import QgsProject

from ..burial import burial_dock, gpkg_sql, schema
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..processing.cable_lay_parsers import fields_from_specs, write_layer_to_gpkg
from ..qgis_compat import WKB_NO_GEOMETRY


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _folder() -> str:
    # OGR may keep pooled handles until QGIS exits: leave the folder to the
    # OS temp cleanup rather than failing on Windows file locks.
    return tempfile.mkdtemp(prefix="bp_safety_")


def _store(folder: str = "") -> BurialStore:
    store = BurialStore(os.path.join(folder or _folder(), "plans.gpkg"),
                        QgsProject.instance().transformContext())
    store.migrate()
    return store


def _journal_mode(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


def _sidecars(path: str) -> list:
    return [s for s in ("-wal", "-shm", "-journal")
            if os.path.exists(path + s)]


def test_probing_a_plain_geopackage_changes_nothing() -> bool:
    folder = _folder()
    path = os.path.join(folder, "survey_layers.gpkg")
    write_layer_to_gpkg(path, "soundings", fields_from_specs(
        [("name", "str"), ("depth", "float")]), WKB_NO_GEOMETRY,
        [{"name": "a", "depth": 12.5}], QgsProject.instance().transformContext())
    before = (_journal_mode(path), _sidecars(path))
    with open(path, "rb") as handle:
        header = handle.read(100)
    exists = BurialStore(path).exists()
    rejected = False
    try:
        burial_dock.BurialPlannerDock._open_existing_store(path)
    except ValueError:
        rejected = True
    after = (_journal_mode(path), _sidecars(path))
    with open(path, "rb") as handle:
        header_after = handle.read(100)
    ok = not exists and rejected and before == after == ("delete", [])
    # Bytes 18/19 are the WAL read/write format versions (1 = rollback).
    ok = ok and header_after == header
    ok = ok and gpkg_sql._key(path) not in gpkg_sql._connections
    try:
        moved = path + ".moved"
        os.rename(path, moved)  # fails on Windows while a handle is open
        os.rename(moved, path)
        unlocked = True
    except OSError:
        unlocked = False
    ok = ok and unlocked
    return _result("plain GeoPackage: exists()/Open existing leave it "
                   "untouched and unlocked", ok,
                   f"before={before} after={after} unlocked={unlocked}")


def test_exists_on_a_registry_is_a_pure_query() -> bool:
    store = _store()
    path = store.gpkg_path
    store.close()
    probe = BurialStore(path)
    ok = probe.exists() and probe._sql_mode is None
    ok = ok and gpkg_sql._key(path) not in gpkg_sql._connections
    # Real use opens the cached WAL connection as before.
    ok = ok and probe.list_plans() == [] and probe._sql_mode is True
    ok = ok and gpkg_sql._key(path) in gpkg_sql._connections
    ok = ok and _journal_mode(path) == "wal"
    probe.close()
    return _result("registry exists() opens no cached connection; use does",
                   ok)


def test_new_registry_meta_written_through_sql() -> bool:
    store = _store()
    meta = store.read_meta()
    ok = store._sql_mode is True
    ok = ok and meta.get("schema_version") == str(schema.SCHEMA_VERSION)
    store.write_meta("probe_key", "1")
    store.write_meta("probe_key", "2")
    rows = [r for r in store.read_table(schema.TABLE_META)
            if r.get("key") == "probe_key"]
    ok = ok and len(rows) == 1 and rows[0]["value"] == "2"
    ok = ok and store.exists()
    store.close()
    return _result("bp_meta updates are targeted upserts in SQL mode", ok)


def test_lock_does_not_force_legacy_mode() -> bool:
    store = _store()
    path = store.gpkg_path
    store.close()
    fresh = BurialStore(path)
    original = gpkg_sql.has_table

    def locked(_path, _table):
        raise sqlite3.OperationalError("database is locked")

    gpkg_sql.has_table = locked
    try:
        try:
            fresh.list_plans()
            raised = False
        except sqlite3.OperationalError:
            raised = True
    finally:
        gpkg_sql.has_table = original
    ok = raised and fresh._sql_mode is None
    ok = ok and fresh.list_plans() == [] and fresh._sql_mode is True
    fresh.close()
    return _result("a busy file raises; the store stays in SQL mode", ok)


def _populate_plan(store: BurialStore, plan_id: str) -> None:
    """One row in every plan-keyed table (generic, from the schema)."""
    store.save_plan({"plan_id": plan_id, "name": "Original",
                     "method": schema.METHOD_PLOUGH, "params_json": "{}"})
    input_id = "in-" + plan_id
    event_id = "ev-" + plan_id
    check_id = "ck-" + plan_id
    for table in schema.PLAN_CHILD_TABLES:
        key = schema.TABLE_KEYS[table]
        row = {"plan_id": plan_id, key: f"{table}-{plan_id}"}
        if table == schema.TABLE_INPUT:
            row[key] = input_id
        elif table == schema.TABLE_EVENT:
            row[key] = event_id
        elif table == schema.TABLE_RISK_CHECK:
            row[key] = check_id
            row["config_json"] = '{"input_id": "%s"}' % input_id
        elif table == schema.TABLE_RULE:
            row["config_json"] = '{"input_id": "%s"}' % input_id
        elif table == schema.TABLE_SECTION:
            row.update(start_event_id=event_id, end_event_id=event_id)
        elif table == schema.TABLE_HAZARD:
            row["check_id"] = check_id
        elif table == schema.TABLE_CHANGE_LOG:
            row["seq"] = 0
        store.upsert_rows(table, [row])


def _counts(store: BurialStore, plan_id: str) -> dict:
    return {table: len(store.read_plan_table(table, plan_id))
            for table in schema.PLAN_CHILD_TABLES}


def _coverage_check(store: BurialStore) -> tuple:
    plan_id = schema.new_id()
    other_id = schema.new_id()
    _populate_plan(store, plan_id)
    _populate_plan(store, other_id)
    copy_id = store.duplicate_plan(plan_id, "Copy")
    copied = _counts(store, copy_id)
    ok = all(copied[t] == (1 if mode == schema.PLAN_TABLE_COPY else 0)
             for t, mode in schema.PLAN_CHILD_TABLES.items())
    # References inside the copy point at the copy's own rows.
    section = store.list_sections(copy_id)[0]
    event = store.list_events(copy_id)[0]
    hazard = store.list_hazards(copy_id)[0]
    check = store.list_risk_checks(copy_id)[0]
    new_input = store.list_inputs(copy_id)[0]["input_id"]
    ok = ok and section["start_event_id"] == event["event_id"]
    ok = ok and hazard["check_id"] == check["check_id"]
    ok = ok and new_input in (check.get("config_json") or "")
    ok = ok and new_input in (store.list_rules(copy_id)[0].get("config_json")
                              or "")
    store.delete_plan(plan_id)
    gone = _counts(store, plan_id)
    ok = ok and store.get_plan(plan_id) is None and not any(gone.values())
    ok = ok and copied == _counts(store, copy_id)
    ok = ok and all(_counts(store, other_id).values())
    return ok, {t: n for t, n in gone.items() if n}


def test_delete_and_duplicate_cover_every_plan_table() -> bool:
    store = _store()
    ok, leftovers = _coverage_check(store)
    store.close()
    return _result("SQL mode: duplicate copies / delete removes every "
                   "plan-keyed table", ok, f"leftovers={leftovers}")


def test_legacy_mode_delete_and_duplicate() -> bool:
    store = _store()
    store.close()
    store._sql_mode = False  # the whole-table writer path
    ok, leftovers = _coverage_check(store)
    return _result("legacy mode: duplicate copies / delete removes every "
                   "plan-keyed table", ok, f"leftovers={leftovers}")


def test_legacy_interruptions_are_retryable_or_invisible() -> bool:
    store = _store()
    store.close()
    store._sql_mode = False
    plan_id = schema.new_id()
    _populate_plan(store, plan_id)
    real_write = store.write_table
    real_upsert = store.upsert_rows
    calls = {"n": 0}

    def failing_write(table, rows):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk full")
        return real_write(table, rows)

    store.write_table = failing_write
    try:
        store.delete_plan(plan_id)
        interrupted = False
    except RuntimeError:
        interrupted = True
    store.write_table = real_write
    # Interrupted delete: the plan is still listed, so Delete can be retried.
    ok = interrupted and store.get_plan(plan_id) is not None
    store.delete_plan(plan_id)
    ok = ok and store.get_plan(plan_id) is None \
        and not any(_counts(store, plan_id).values())

    source_id = schema.new_id()
    _populate_plan(store, source_id)
    before = {p["plan_id"] for p in store.list_plans()}

    def failing_upsert(table, rows):
        if table == schema.TABLE_HAZARD:
            raise RuntimeError("disk full")
        return real_upsert(table, rows)

    store.upsert_rows = failing_upsert
    try:
        store.duplicate_plan(source_id, "Half copy")
        interrupted = False
    except RuntimeError:
        interrupted = True
    store.upsert_rows = real_upsert
    # Interrupted duplicate: no half-copied plan appears in the list.
    ok = ok and interrupted \
        and {p["plan_id"] for p in store.list_plans()} == before
    return _result("legacy mode: interrupted delete is retryable, "
                   "interrupted duplicate is invisible", ok)


def test_plan_header_and_change_log_commit_together() -> bool:
    store = _store()
    model = PlanModel(store)
    errors = []
    model.storeError.connect(errors.append)
    real_append = store.append_change

    def failing_append(*_args, **_kwargs):
        raise RuntimeError("log write failed")

    store.append_change = failing_append
    try:
        created = model.create_plan("Atomic", schema.METHOD_PLOUGH)
    finally:
        store.append_change = real_append
    ok = created is None and store.list_plans() == [] and len(errors) == 1

    plan_id = model.create_plan("Atomic", schema.METHOD_PLOUGH)
    ok = ok and bool(plan_id) and len(store.list_change_log(plan_id)) == 1
    store.append_change = failing_append
    try:
        saved = model.update_plan({"name": "Renamed"}, reason="rename")
    finally:
        store.append_change = real_append
    ok = ok and saved is False and len(errors) == 2
    ok = ok and model.plan.get("name") == "Atomic"
    ok = ok and store.get_plan(plan_id)["name"] == "Atomic"
    ok = ok and len(store.list_change_log(plan_id)) == 1
    ok = ok and model.update_plan({"name": "Renamed"}, reason="rename")
    ok = ok and store.get_plan(plan_id)["name"] == "Renamed"
    ok = ok and len(store.list_change_log(plan_id)) == 2
    store.close()
    return _result("create/update plan: header + change log are atomic; "
                   "failures reach storeError", ok, f"{len(errors)} error(s)")


def run_all() -> list:
    return [
        test_probing_a_plain_geopackage_changes_nothing(),
        test_exists_on_a_registry_is_a_pure_query(),
        test_new_registry_meta_written_through_sql(),
        test_lock_does_not_force_legacy_mode(),
        test_delete_and_duplicate_cover_every_plan_table(),
        test_legacy_mode_delete_and_duplicate(),
        test_legacy_interruptions_are_retryable_or_invisible(),
        test_plan_header_and_change_log_commit_together(),
    ]


if __name__ == "__main__":  # pragma: no cover
    run_all()
