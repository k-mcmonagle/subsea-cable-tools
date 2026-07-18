# -*- coding: utf-8 -*-
"""QGIS-backed GeoPackage checks for the Plan of Work store."""

from __future__ import annotations

import os
import tempfile

from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry

from ..planner import schema
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
    resources = store.list_resources(scenario_id)
    store.save_tasks(scenario_id, [{
        "task_id": schema.new_id(), "name": "Mobilise", "duration_mode": "manual",
        "duration_hours": 12.0, "resource_id": resources[0]["resource_id"],
    }])
    tasks = store.list_tasks(scenario_id)
    ok = store.exists() and store.read_meta().get("schema_version") == "3"
    ok = ok and len(resources) == 1 and resources[0]["name"] == "Vessel 1"
    ok = ok and float(resources[0]["start_offset_hours"] or 0.0) == 0.0
    ok = ok and len(tasks) == 1 and tasks[0]["name"] == "Mobilise"
    store.delete_scenario(scenario_id)
    ok = ok and not store.list_scenarios() and not store.list_resources(scenario_id)
    return _result("create/meta/scenario CRUD + cascade", ok)


def test_duplicate_independence_and_remap():
    store = _temp_store()
    original_id = store.create_scenario("Original", "2026-01-01T00:00")
    original_resource = store.list_resources(original_id)[0]
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
    store.save_tasks(original_id, original_tasks)
    copied_id = store.duplicate_scenario(original_id, "Copy")
    copied_resources = store.list_resources(copied_id)
    copied_tasks = store.list_tasks(copied_id)
    ok = store.get_scenario(copied_id)["duplicated_from_id"] == original_id
    ok = ok and copied_resources[0]["resource_id"] != original_resource["resource_id"]
    ok = ok and copied_tasks[0]["task_id"] != first_id
    ok = ok and copied_tasks[1]["predecessor_task_id"] == copied_tasks[0]["task_id"]
    ok = ok and copied_tasks[0]["resource_id"] == copied_resources[0]["resource_id"]
    copied_geometry = store.get_task_geometry(copied_tasks[0]["task_id"])
    ok = ok and copied_geometry is not None
    ok = ok and copied_tasks[0]["feature_id"] != reference["feature_id"]
    copied_tasks[0]["name"] = "Changed"
    store.save_tasks(copied_id, copied_tasks)
    ok = ok and store.list_tasks(original_id)[0]["name"] == "A"
    store.migrate()
    ok = ok and store.read_meta().get("schema_version") == "3"
    return _result("duplicate remapping + independence + migrate no-op", ok)


def test_v2_to_v3_phase_and_resource_migration():
    store = _temp_store()
    scenario_id = store.create_scenario("Legacy", "2026-01-01T00:00")
    resource = store.list_resources(scenario_id)[0]
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
    migrated_resource = store.list_resources(scenario_id)[0]
    migrated_task = store.list_tasks(scenario_id)[0]
    ok = store.read_meta().get("schema_version") == "3"
    ok = ok and float(migrated_resource.get("start_offset_hours") or 0.0) == 0.0
    ok = ok and int(migrated_task.get("is_phase") or 0) == 0
    ok = ok and int(migrated_task.get("outline_level") or 0) == 0
    return _result("v2→v3 phase/resource migration", ok)


def run_all():
    return [
        test_create_crud_and_meta(), test_duplicate_independence_and_remap(),
        test_v2_to_v3_phase_and_resource_migration(),
    ]
