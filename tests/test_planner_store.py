# -*- coding: utf-8 -*-
"""QGIS-backed GeoPackage checks for the Plan of Work store."""

from __future__ import annotations

import os
import json
import tempfile

from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry

from ..planner import schema
from ..planner.feature_ref import shared_owner_task_id, shared_reference
from ..planner.store import PlannerStore


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def _temp_store():
    folder = tempfile.mkdtemp(prefix="pow_store_test_")
    store = PlannerStore(os.path.join(folder, "planner.gpkg"))
    store.ensure_created()
    return store


def test_create_crud_and_meta():
    store = _temp_store()
    scenario_id = store.create_scenario("Cable lay", "2026-08-14T03:20")
    resources = store.list_resources()
    store.save_tasks(scenario_id, [{
        "task_id": schema.new_id(), "name": "Mobilise", "duration_mode": "manual",
        "duration_hours": 12.0, "resource_id": resources[0]["resource_id"],
    }])
    tasks = store.list_tasks(scenario_id)
    ok = store.exists() and store.read_meta().get("schema_version") == "7"
    ok = ok and len(resources) == 1 and resources[0]["name"] == "Vessel 1"
    ok = ok and float(resources[0]["start_offset_hours"] or 0.0) == 0.0
    ok = ok and resources[0]["fuel_unit"] == schema.DEFAULT_FUEL_UNIT
    ok = ok and float(resources[0]["fuel_start"] or 0.0) == 0.0
    ok = ok and len(tasks) == 1 and tasks[0]["name"] == "Mobilise"
    ok = ok and (tasks[0].get("dependency_type") or "FS") == "FS"
    store.delete_scenario(scenario_id)
    # Tasks cascade with the scenario; project-level resources survive.
    ok = ok and not store.list_scenarios() and len(store.list_resources()) == 1
    return _result("create/meta/scenario CRUD + cascade", ok)


def test_duplicate_independence_and_remap():
    store = _temp_store()
    original_id = store.create_scenario("Original", "2026-01-01T00:00")
    original_resource = store.list_resources()[0]
    first_id, second_id = schema.new_id(), schema.new_id()
    store.save_tasks(original_id, [
        {"task_id": first_id, "name": "A", "duration_mode": "manual",
         "duration_hours": 1.0, "resource_id": original_resource["resource_id"]},
        {"task_id": second_id, "name": "B", "duration_mode": "manual",
         "duration_hours": 2.0, "resource_id": original_resource["resource_id"],
         "predecessor_task_id": first_id},
    ])
    reference = store.set_task_geometry(
        first_id, original_id, 0, "A", QgsGeometry.fromWkt("LINESTRING(0 0, 0.01 0)"),
        "line", source_crs=QgsCoordinateReferenceSystem("EPSG:4326"),
        resource_id=original_resource["resource_id"], speed_knots=1.0,
        duration_hours=1.0, source_kind="test")
    original_tasks = store.list_tasks(original_id)
    original_tasks[0].update(reference)
    original_tasks[0].update({
        "progress_status": "completed", "percent_complete": 100.0,
        "actual_log_json": '[{"note":"done"}]',
    })
    store.save_tasks(original_id, original_tasks)
    original_scenario = store.get_scenario(original_id)
    original_scenario["settings_json"] = json.dumps({
        "schedule_mode": "backward", "baseline": {"tasks": []},
    })
    store.save_scenario(original_scenario)
    copied_id = store.duplicate_scenario(original_id, "Copy")
    copied_resources = store.list_resources()
    copied_tasks = store.list_tasks(copied_id)
    ok = store.get_scenario(copied_id)["duplicated_from_id"] == original_id
    copied_settings = json.loads(store.get_scenario(copied_id)["settings_json"])
    ok = ok and copied_settings.get("schedule_mode") == "backward"
    ok = ok and "baseline" not in copied_settings
    # Resources are shared, so duplication reuses them rather than copying.
    ok = ok and len(copied_resources) == 1
    ok = ok and copied_resources[0]["resource_id"] == original_resource["resource_id"]
    ok = ok and copied_tasks[0]["task_id"] != first_id
    ok = ok and copied_tasks[1]["predecessor_task_id"] == copied_tasks[0]["task_id"]
    ok = ok and copied_tasks[0]["resource_id"] == copied_resources[0]["resource_id"]
    ok = ok and copied_tasks[0]["progress_status"] == "not_started"
    ok = ok and float(copied_tasks[0]["percent_complete"] or 0.0) == 0.0
    copied_geometry = store.get_task_geometry(copied_tasks[0]["task_id"])
    ok = ok and copied_geometry is not None
    ok = ok and copied_tasks[0]["feature_id"] != reference["feature_id"]
    copied_tasks[0]["name"] = "Changed"
    store.save_tasks(copied_id, copied_tasks)
    ok = ok and store.list_tasks(original_id)[0]["name"] == "A"
    store.migrate()
    ok = ok and store.read_meta().get("schema_version") == "7"
    return _result("duplicate remapping + independence + migrate no-op", ok)


def test_shared_geometry_adoption_and_duplication():
    """Sharers keep working when the owning task is deleted or duplicated."""
    store = _temp_store()
    scenario_id = store.create_scenario("Shared", "2026-01-01T00:00")
    resource = store.list_resources()[0]
    owner_id, sharer_a, sharer_b = schema.new_id(), schema.new_id(), schema.new_id()
    base = {"duration_mode": "manual", "duration_hours": 1.0,
            "resource_id": resource["resource_id"]}
    tasks = [
        dict(base, task_id=owner_id, name="Owner"),
        dict(base, task_id=sharer_a, name="Sharer A"),
        dict(base, task_id=sharer_b, name="Sharer B"),
    ]
    store.save_tasks(scenario_id, tasks)
    reference = store.set_task_geometry(
        owner_id, scenario_id, 0, "Owner", QgsGeometry.fromWkt("POINT(1 2)"),
        "point", source_crs=QgsCoordinateReferenceSystem("EPSG:4326"),
        resource_id=resource["resource_id"], source_kind="test")
    tasks[0].update(reference)
    shared = shared_reference(reference, owner_id)
    tasks[1].update(shared)
    tasks[2].update(shared)
    store.save_tasks(scenario_id, tasks)
    ok = shared_owner_task_id(tasks[1]) == owner_id

    # Duplicating repoints sharers at the duplicated owner, not the original.
    copied_id = store.duplicate_scenario(scenario_id, "Copy")
    copied = store.list_tasks(copied_id)
    copied_owner = next(row for row in copied if row["name"] == "Owner")
    copied_sharer = next(row for row in copied if row["name"] == "Sharer A")
    ok = ok and shared_owner_task_id(copied_sharer) == copied_owner["task_id"]
    ok = ok and copied_sharer["feature_id"] == copied_owner["feature_id"]
    ok = ok and copied_sharer["feature_id"] != reference["feature_id"]

    # Deleting the owner hands the geometry to the first surviving sharer.
    survivors = [dict(tasks[1]), dict(tasks[2])]
    repaired = store.save_tasks(scenario_id, survivors)
    ok = ok and set(repaired) == {sharer_a, sharer_b}
    ok = ok and not shared_owner_task_id(survivors[0])
    ok = ok and shared_owner_task_id(survivors[1]) == sharer_a
    adopted = store.get_task_geometry(sharer_a)
    ok = ok and adopted is not None
    if adopted is not None:
        point = adopted[1].geometry().asPoint()
        ok = ok and abs(point.x() - 1.0) < 1e-9 and abs(point.y() - 2.0) < 1e-9
    ok = ok and store.get_task_geometry(owner_id) is None
    return _result("shared geometry adoption on delete + duplicate remap", ok)


def test_v2_to_v3_phase_and_resource_migration():
    store = _temp_store()
    scenario_id = store.create_scenario("Legacy", "2026-01-01T00:00")
    resource = store.list_resources()[0]
    task_id = schema.new_id()
    old_resource_specs = [
        spec for spec in schema.RESOURCE_FIELDS if spec[0] != "start_offset_hours"]
    old_task_specs = [
        spec for spec in schema.TASK_FIELDS if spec[0] not in ("is_phase", "outline_level")]
    store._write_table_rows(schema.TABLE_RESOURCE, old_resource_specs, [resource])
    store._write_table_rows(schema.TABLE_TASK, old_task_specs, [{
        "task_id": task_id, "scenario_id": scenario_id, "seq": 0,
        "name": "Legacy task", "duration_mode": "manual", "duration_hours": 1.0,
        "resource_id": resource["resource_id"],
    }])
    store.write_meta("schema_version", "2")
    store.migrate()
    migrated_resource = store.list_resources()[0]
    migrated_task = store.list_tasks(scenario_id)[0]
    ok = store.read_meta().get("schema_version") == "7"
    ok = ok and float(migrated_resource.get("start_offset_hours") or 0.0) == 0.0
    ok = ok and int(migrated_task.get("is_phase") or 0) == 0
    ok = ok and int(migrated_task.get("outline_level") or 0) == 0
    return _result("v2→v3 phase/resource migration", ok)


FUEL_RESOURCE_FIELDS = ("fuel_unit", "fuel_rate_transit", "fuel_rate_dp",
                        "fuel_rate_anchor", "fuel_rate_port", "fuel_start",
                        "fuel_cost_per_unit")


def test_v3_to_v4_fuel_migration():
    store = _temp_store()
    scenario_id = store.create_scenario("Legacy", "2026-01-01T00:00")
    resource = store.list_resources()[0]
    old_resource_specs = [spec for spec in schema.RESOURCE_FIELDS
                          if spec[0] not in FUEL_RESOURCE_FIELDS]
    old_task_specs = [spec for spec in schema.TASK_FIELDS
                      if spec[0] not in ("fuel_mode", "bunker_amount")]
    stripped = {key: value for key, value in resource.items()
                if key not in FUEL_RESOURCE_FIELDS}
    store._write_table_rows(schema.TABLE_RESOURCE, old_resource_specs, [stripped])
    store._write_table_rows(schema.TABLE_TASK, old_task_specs, [{
        "task_id": schema.new_id(), "scenario_id": scenario_id, "seq": 0,
        "name": "Legacy task", "duration_mode": "manual", "duration_hours": 1.0,
        "resource_id": resource["resource_id"],
    }])
    store.write_meta("schema_version", "3")
    store.migrate()
    migrated_resource = store.list_resources()[0]
    migrated_task = store.list_tasks(scenario_id)[0]
    ok = store.read_meta().get("schema_version") == "7"
    ok = ok and migrated_resource.get("fuel_unit") == schema.DEFAULT_FUEL_UNIT
    ok = ok and float(migrated_resource.get("fuel_rate_transit") or 0.0) == 0.0
    ok = ok and float(migrated_resource.get("fuel_start") or 0.0) == 0.0
    ok = ok and (migrated_task.get("fuel_mode") or "") == ""
    ok = ok and float(migrated_task.get("bunker_amount") or 0.0) == 0.0
    return _result("v3→v4 fuel profile migration", ok)


def test_v4_to_v5_shared_resource_migration():
    store = _temp_store()
    scenario_a = store.create_scenario("A", "2026-01-01T00:00")
    scenario_b = store.create_scenario("B", "2026-01-01T00:00")
    shared = store.list_resources()[0]
    twin_id = schema.new_id()
    legacy_a = dict(shared)
    legacy_a["scenario_id"] = scenario_a
    twin = dict(shared)
    twin.update({"resource_id": twin_id, "scenario_id": scenario_b})
    store._write_table_rows(schema.TABLE_RESOURCE, schema.RESOURCE_FIELDS,
                            [legacy_a, twin])
    store.save_tasks(scenario_a, [{
        "task_id": schema.new_id(), "name": "TA", "duration_mode": "manual",
        "duration_hours": 1.0, "resource_id": shared["resource_id"]}])
    store.save_tasks(scenario_b, [{
        "task_id": schema.new_id(), "name": "TB", "duration_mode": "manual",
        "duration_hours": 1.0, "resource_id": twin_id}])
    store.write_meta("schema_version", "4")
    store.migrate()
    resources = store.list_resources()
    ok = store.read_meta().get("schema_version") == "7"
    ok = ok and len(resources) == 1
    ok = ok and resources[0]["resource_id"] == shared["resource_id"]
    ok = ok and (resources[0].get("scenario_id") or "") == ""
    ok = ok and store.list_tasks(scenario_a)[0]["resource_id"] == shared["resource_id"]
    ok = ok and store.list_tasks(scenario_b)[0]["resource_id"] == shared["resource_id"]
    store.delete_scenario(scenario_b)
    ok = ok and len(store.list_resources()) == 1
    return _result("v4→v5 shared resources + duplicate merge + task remap", ok)


def test_v5_to_v6_advanced_task_migration():
    store = _temp_store()
    scenario_id = store.create_scenario("Legacy", "2026-01-01T00:00")
    resource = store.list_resources()[0]
    advanced_fields = {
        "operation_type", "dependency_type", "location_mode", "location_chainage_m",
        "constraint_type", "constraint_datetime", "is_milestone", "progress_status",
        "percent_complete", "actual_start_datetime", "actual_finish_datetime",
        "remaining_duration_hours", "progress_notes", "actual_log_json",
        "progress_updated_utc",
    }
    old_specs = [spec for spec in schema.TASK_FIELDS if spec[0] not in advanced_fields]
    store._write_table_rows(schema.TABLE_TASK, old_specs, [{
        "task_id": schema.new_id(), "scenario_id": scenario_id, "seq": 0,
        "name": "Legacy", "duration_mode": "manual", "duration_hours": 1.0,
        "resource_id": resource["resource_id"],
    }])
    store.write_meta("schema_version", "5")
    store.migrate()
    task = store.list_tasks(scenario_id)[0]
    ok = store.read_meta().get("schema_version") == "7"
    ok = ok and task.get("dependency_type") == "FS"
    ok = ok and task.get("location_mode") == "feature"
    ok = ok and task.get("progress_status") == "not_started"
    ok = ok and float(task.get("percent_complete") or 0.0) == 0.0
    return _result("v5→v6 advanced schedule/progress migration", ok)


def run_all():
    return [
        test_create_crud_and_meta(), test_duplicate_independence_and_remap(),
        test_shared_geometry_adoption_and_duplication(),
        test_v2_to_v3_phase_and_resource_migration(), test_v3_to_v4_fuel_migration(),
        test_v4_to_v5_shared_resource_migration(), test_v5_to_v6_advanced_task_migration(),
    ]
