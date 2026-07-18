# -*- coding: utf-8 -*-
"""GeoPackage persistence for planning scenarios, resources, and tasks."""

from __future__ import annotations

import json
import os
import shutil
from typing import Dict, List, Optional, Sequence

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCoordinateTransformContext,
    QgsFeature, QgsGeometry, QgsProject, QgsVectorFileWriter, QgsVectorLayer,
)

from ..processing.cable_lay_parsers import fields_from_specs, open_gpkg_layer, write_layer_to_gpkg
from ..qgis_compat import (
    VECTOR_WRITER_NO_ERROR, VECTOR_WRITER_OVERWRITE_FILE,
    VECTOR_WRITER_OVERWRITE_LAYER, WKB_LINESTRING, WKB_NO_GEOMETRY, WKB_POINT,
)
from . import schema

PROJECT_SCOPE = "SubseaCableTools"
PROJECT_KEY_GPKG = "planner_gpkg"


def project_gpkg_path(project: Optional[QgsProject] = None) -> Optional[str]:
    project = project or QgsProject.instance()
    value, ok = project.readEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, "")
    return value if ok and value else None


def set_project_gpkg_path(path: str, project: Optional[QgsProject] = None) -> None:
    (project or QgsProject.instance()).writeEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, path)


def default_project_gpkg_path(project: Optional[QgsProject] = None) -> str:
    project = project or QgsProject.instance()
    return schema.default_gpkg_path(project.fileName(), project.title())


class PlannerStore:
    def __init__(self, gpkg_path: str,
                 transform_context: Optional[QgsCoordinateTransformContext] = None):
        self.gpkg_path = gpkg_path
        self.transform_context = transform_context or QgsProject.instance().transformContext()

    def exists(self) -> bool:
        return os.path.exists(self.gpkg_path) and self._table_exists(schema.TABLE_META)

    def ensure_created(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.gpkg_path))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        for table, specs in schema.REGISTRY_TABLES.items():
            if not self._table_exists(table):
                self._write_table_rows(table, specs, [])
        for table, specs in schema.SPATIAL_TABLES.items():
            if not self._table_exists(table):
                wkb_type = WKB_POINT if table == schema.TABLE_TASK_POINT else WKB_LINESTRING
                self._create_spatial_table(table, specs, wkb_type)
        meta = self.read_meta()
        if "schema_version" not in meta:
            self.write_meta("schema_version", str(schema.SCHEMA_VERSION))
            self.write_meta("created_utc", schema.utc_now_iso())

    def migrate(self) -> None:
        self.ensure_created()
        current = int(self.read_meta().get("schema_version", schema.SCHEMA_VERSION))
        while current < schema.SCHEMA_VERSION:
            self.backup_before("migrate_v%s" % current)
            migrator = MIGRATIONS.get(current)
            if migrator is not None:
                migrator(self)
            current += 1
            self.write_meta("schema_version", str(current))

    def backup_before(self, label: str) -> Optional[str]:
        if not os.path.exists(self.gpkg_path):
            return None
        stem, ext = os.path.splitext(self.gpkg_path)
        target = "%s.%s.bak%s" % (stem, schema.sanitize_slug(label), ext)
        try:
            shutil.copy2(self.gpkg_path, target)
            return target
        except OSError:
            return None

    def _table_exists(self, table: str) -> bool:
        return os.path.exists(self.gpkg_path) and open_gpkg_layer(self.gpkg_path, table) is not None

    def read_table(self, table: str) -> List[Dict]:
        layer = open_gpkg_layer(self.gpkg_path, table)
        if layer is None:
            return []
        names = [field.name() for field in layer.fields() if field.name().lower() != "fid"]
        return [_normalise_row({name: feature[name] for name in names})
                for feature in layer.getFeatures()]

    def write_table(self, table: str, rows: Sequence[Dict]) -> None:
        self._write_table_rows(table, schema.REGISTRY_TABLES[table], list(rows))

    def _write_table_rows(self, table, specs, rows) -> None:
        write_layer_to_gpkg(
            self.gpkg_path, table, fields_from_specs(specs), WKB_NO_GEOMETRY,
            rows, self.transform_context,
        )

    def upsert_rows(self, table: str, rows: Sequence[Dict]) -> None:
        key = schema.TABLE_KEYS[table]
        incoming = {str(row[key]): dict(row) for row in rows}
        merged = [row for row in self.read_table(table)
                  if str(row.get(key)) not in incoming]
        merged.extend(incoming.values())
        self.write_table(table, merged)

    def delete_rows(self, table: str, keys: Sequence[str]) -> None:
        key = schema.TABLE_KEYS[table]
        dropped = {str(value) for value in keys}
        self.write_table(table, [row for row in self.read_table(table)
                                 if str(row.get(key)) not in dropped])

    def read_meta(self) -> Dict[str, str]:
        return {row["key"]: row["value"] for row in self.read_table(schema.TABLE_META)
                if row.get("key")}

    def write_meta(self, key: str, value: str) -> None:
        rows = [row for row in self.read_table(schema.TABLE_META) if row.get("key") != key]
        rows.append({"key": key, "value": value})
        self._write_table_rows(schema.TABLE_META, schema.META_FIELDS, rows)

    def list_scenarios(self) -> List[Dict]:
        return sorted(self.read_table(schema.TABLE_SCENARIO),
                      key=lambda row: ((row.get("name") or "").lower(), row.get("created_utc") or ""))

    def get_scenario(self, scenario_id: str) -> Optional[Dict]:
        return next((row for row in self.read_table(schema.TABLE_SCENARIO)
                     if row.get("scenario_id") == scenario_id), None)

    def create_scenario(self, name: str, start_datetime: str,
                        description: str = "", notes: str = "") -> str:
        now = schema.utc_now_iso()
        scenario_id = schema.new_id()
        resource_id = schema.new_id()
        self.upsert_rows(schema.TABLE_SCENARIO, [{
            "scenario_id": scenario_id, "name": name or "Planning Scenario",
            "description": description or "", "start_datetime": start_datetime,
            "duplicated_from_id": "", "settings_json": "{}", "created_utc": now,
            "modified_utc": now, "notes": notes or "",
        }])
        self.upsert_rows(schema.TABLE_RESOURCE, [{
            "resource_id": resource_id, "scenario_id": scenario_id,
            "name": schema.DEFAULT_RESOURCE_NAME, "kind": schema.DEFAULT_RESOURCE_KIND,
            "color_hex": schema.DEFAULT_RESOURCE_COLOR,
            "default_speed_kn": schema.DEFAULT_SPEED_KN, "start_offset_hours": 0.0,
            "seq": 0, "notes": "",
        }])
        return scenario_id

    def save_scenario(self, row: Dict) -> None:
        saved = dict(row)
        saved.setdefault("scenario_id", schema.new_id())
        saved.setdefault("created_utc", schema.utc_now_iso())
        saved["modified_utc"] = schema.utc_now_iso()
        self.upsert_rows(schema.TABLE_SCENARIO, [saved])

    def delete_scenario(self, scenario_id: str) -> None:
        task_ids = [row["task_id"] for row in self.list_tasks(scenario_id)]
        resource_ids = [row["resource_id"] for row in self.list_resources(scenario_id)]
        if task_ids:
            self.delete_task_geometries(task_ids)
            self.delete_rows(schema.TABLE_TASK, task_ids)
        if resource_ids:
            self.delete_rows(schema.TABLE_RESOURCE, resource_ids)
        self.delete_rows(schema.TABLE_SCENARIO, [scenario_id])

    def list_resources(self, scenario_id: str) -> List[Dict]:
        rows = [row for row in self.read_table(schema.TABLE_RESOURCE)
                if row.get("scenario_id") == scenario_id]
        return sorted(rows, key=lambda row: (int(row.get("seq") or 0), row.get("name") or ""))

    def save_resources(self, scenario_id: str, rows: Sequence[Dict]) -> None:
        old_ids = [row["resource_id"] for row in self.list_resources(scenario_id)]
        if old_ids:
            self.delete_rows(schema.TABLE_RESOURCE, old_ids)
        saved = []
        for seq, row in enumerate(rows):
            item = dict(row)
            item.setdefault("resource_id", schema.new_id())
            item["scenario_id"] = scenario_id
            item["seq"] = seq
            saved.append(item)
        if saved:
            self.upsert_rows(schema.TABLE_RESOURCE, saved)

    def list_tasks(self, scenario_id: str) -> List[Dict]:
        rows = [row for row in self.read_table(schema.TABLE_TASK)
                if row.get("scenario_id") == scenario_id]
        return sorted(rows, key=lambda row: (int(row.get("seq") or 0), row.get("name") or ""))

    def save_tasks(self, scenario_id: str, rows: Sequence[Dict]) -> None:
        old_ids = [row["task_id"] for row in self.list_tasks(scenario_id)]
        retained = {str(row.get("task_id")) for row in rows if row.get("task_id")}
        removed = [task_id for task_id in old_ids if str(task_id) not in retained]
        if removed:
            self.delete_task_geometries(removed)
        if old_ids:
            self.delete_rows(schema.TABLE_TASK, old_ids)
        now = schema.utc_now_iso()
        saved = []
        for seq, row in enumerate(rows):
            item = dict(row)
            item.setdefault("task_id", schema.new_id())
            item.setdefault("created_utc", now)
            item.update({"scenario_id": scenario_id, "seq": seq, "modified_utc": now})
            saved.append(item)
        if saved:
            self.upsert_rows(schema.TABLE_TASK, saved)
            self.sync_geometry_attributes(saved)

    def duplicate_scenario(self, scenario_id: str, new_name: str) -> str:
        original = self.get_scenario(scenario_id)
        if original is None:
            raise ValueError("Scenario not found.")
        now = schema.utc_now_iso()
        new_scenario_id = schema.new_id()
        copied_scenario = dict(original)
        copied_scenario.update({
            "scenario_id": new_scenario_id, "name": new_name,
            "duplicated_from_id": scenario_id, "created_utc": now, "modified_utc": now,
        })
        resource_map = {row["resource_id"]: schema.new_id()
                        for row in self.list_resources(scenario_id)}
        task_map = {row["task_id"]: schema.new_id() for row in self.list_tasks(scenario_id)}
        resources = []
        for row in self.list_resources(scenario_id):
            copied = dict(row)
            copied.update({"resource_id": resource_map[row["resource_id"]],
                           "scenario_id": new_scenario_id})
            resources.append(copied)
        tasks = []
        original_tasks = self.list_tasks(scenario_id)
        for row in original_tasks:
            copied = dict(row)
            copied.update({
                "task_id": task_map[row["task_id"]], "scenario_id": new_scenario_id,
                "resource_id": resource_map.get(row.get("resource_id"), ""),
                "predecessor_task_id": task_map.get(row.get("predecessor_task_id"), ""),
                "created_utc": now, "modified_utc": now,
            })
            geometry = self.get_task_geometry(row["task_id"])
            if geometry is not None:
                _layer, feature, kind = geometry
                reference = self.set_task_geometry(
                    task_map[row["task_id"]], new_scenario_id, copied.get("seq") or 0,
                    copied.get("name") or "Task", feature.geometry(), kind,
                    source_crs=_layer.crs(), resource_id=copied.get("resource_id") or "",
                    speed_knots=copied.get("speed_knots"),
                    duration_hours=copied.get("duration_hours"),
                    notes=copied.get("notes") or "", source_kind="scenario_copy",
                    source_ref={"duplicated_from_task_id": row["task_id"]},
                )
                copied.update(reference)
            tasks.append(copied)
        self.upsert_rows(schema.TABLE_SCENARIO, [copied_scenario])
        if resources:
            self.upsert_rows(schema.TABLE_RESOURCE, resources)
        if tasks:
            self.upsert_rows(schema.TABLE_TASK, tasks)
        return new_scenario_id

    # -- planner-owned spatial task geometry -------------------------------
    def _create_spatial_table(self, table, specs, wkb_type) -> None:
        crs = QgsProject.instance().crs()
        if crs is None or not crs.isValid():
            crs = QgsCoordinateReferenceSystem("EPSG:4326")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = table
        options.fileEncoding = "UTF-8"
        options.actionOnExistingFile = (
            VECTOR_WRITER_OVERWRITE_LAYER if os.path.exists(self.gpkg_path)
            else VECTOR_WRITER_OVERWRITE_FILE)
        writer = QgsVectorFileWriter.create(
            self.gpkg_path, fields_from_specs(specs), wkb_type, crs,
            self.transform_context, options)
        if writer.hasError() != VECTOR_WRITER_NO_ERROR:
            message = writer.errorMessage()
            del writer
            raise RuntimeError("Could not create planner geometry layer '%s': %s" % (table, message))
        del writer

    def geometry_layer(self, kind: str, add_to_project: bool = False) -> Optional[QgsVectorLayer]:
        table = schema.TABLE_TASK_POINT if kind == "point" else schema.TABLE_TASK_LINE
        uri_suffix = "|layername=%s" % table
        project = QgsProject.instance()
        for layer in project.mapLayers().values():
            try:
                if layer.source().endswith(uri_suffix) and os.path.normcase(self.gpkg_path) in os.path.normcase(layer.source()):
                    return layer
            except Exception:
                continue
        layer = open_gpkg_layer(self.gpkg_path, table)
        if layer is not None and add_to_project:
            root = project.layerTreeRoot()
            group = root.findGroup("Planner") or root.findGroup("Plan of Work Planner")
            if group is None:
                group = root.insertGroup(0, "Planner")
            elif group.name() != "Planner":
                group.setName("Planner")
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        return layer

    def load_geometry_layers(self) -> List[QgsVectorLayer]:
        layers = [layer for layer in (
            self.geometry_layer("point", True), self.geometry_layer("line", True)
        ) if layer is not None]
        for layer in layers:
            if layer.source().endswith("|layername=%s" % schema.TABLE_TASK_POINT):
                layer.setName("Planner Task Points")
            else:
                layer.setName("Planner Task Lines")
        return layers

    def get_task_geometry(self, task_id: str):
        for kind in ("point", "line"):
            layer = self.geometry_layer(kind)
            if layer is None:
                continue
            for feature in layer.getFeatures():
                if str(feature["task_id"]) == str(task_id):
                    return layer, feature, kind
        return None

    def delete_task_geometries(self, task_ids: Sequence[str]) -> None:
        dropped = {str(task_id) for task_id in task_ids}
        if not dropped:
            return
        for kind in ("point", "line"):
            layer = self.geometry_layer(kind)
            if layer is None:
                continue
            feature_ids = [feature.id() for feature in layer.getFeatures()
                           if str(feature["task_id"]) in dropped]
            if feature_ids:
                if layer.isEditable():
                    ok = all(layer.deleteFeature(feature_id) for feature_id in feature_ids)
                else:
                    ok = layer.dataProvider().deleteFeatures(feature_ids)
                if not ok:
                    raise RuntimeError("Could not delete planner task geometry.")
                layer.updateExtents()
                layer.triggerRepaint()

    def set_task_geometry(self, task_id: str, scenario_id: str, seq: int, name: str,
                          geometry: QgsGeometry, kind: str, source_crs=None,
                          resource_id: str = "", speed_knots=None, duration_hours=None,
                          notes: str = "", source_kind: str = "drawn",
                          source_ref: Optional[Dict] = None) -> Dict:
        if kind not in ("point", "line"):
            raise ValueError("Planner geometry must be point or line.")
        self.delete_task_geometries([task_id])
        layer = self.geometry_layer(kind, True)
        if layer is None:
            raise RuntimeError("Planner geometry layer is unavailable.")
        copied_geometry = QgsGeometry(geometry)
        if source_crs is not None and source_crs.isValid() and source_crs != layer.crs():
            copied_geometry.transform(QgsCoordinateTransform(
                source_crs, layer.crs(), QgsProject.instance()))
        geom_id = schema.new_id()
        now = schema.utc_now_iso()
        source_ref = dict(source_ref or {})
        feature = QgsFeature(layer.fields())
        values = {
            "geom_id": geom_id, "task_id": task_id, "scenario_id": scenario_id,
            "seq": int(seq), "name": name or "Task", "resource_id": resource_id or "",
            "speed_knots": speed_knots, "duration_hours": duration_hours,
            "source_kind": source_kind or "drawn",
            "source_ref_json": json.dumps(source_ref, sort_keys=True),
            "notes": notes or "", "created_utc": now, "modified_utc": now,
        }
        for field_name, value in values.items():
            feature.setAttribute(field_name, value)
        feature.setGeometry(copied_geometry)
        if layer.isEditable():
            ok = layer.addFeature(feature)
            added = [feature] if ok else []
        else:
            ok, added = layer.dataProvider().addFeatures([feature])
        if not ok or not added:
            raise RuntimeError("Could not save planner task geometry.")
        layer.updateExtents()
        layer.triggerRepaint()
        return {
            "layer_id": layer.id(), "layer_source": layer.source(),
            "layer_name": layer.name(), "feature_id": geom_id,
            "feature_label": name or "Task", "geom_kind": kind,
            "linked_ref_json": json.dumps({
                "owned_geometry": True, "source_kind": source_kind,
                "source_ref": source_ref,
            }, sort_keys=True),
        }

    def sync_geometry_attributes(self, tasks: Sequence[Dict]) -> None:
        by_id = {str(task.get("task_id")): task for task in tasks}
        now = schema.utc_now_iso()
        for kind in ("point", "line"):
            layer = self.geometry_layer(kind)
            if layer is None:
                continue
            changes = {}
            field_indices = {name: layer.fields().indexOf(name) for name in (
                "scenario_id", "seq", "name", "resource_id", "speed_knots",
                "duration_hours", "notes", "modified_utc")}
            for feature in layer.getFeatures():
                task = by_id.get(str(feature["task_id"]))
                if task is None:
                    continue
                changes[feature.id()] = {
                    field_indices["scenario_id"]: task.get("scenario_id") or "",
                    field_indices["seq"]: int(task.get("seq") or 0),
                    field_indices["name"]: task.get("name") or "Task",
                    field_indices["resource_id"]: task.get("resource_id") or "",
                    field_indices["speed_knots"]: task.get("speed_knots"),
                    field_indices["duration_hours"]: task.get("duration_hours"),
                    field_indices["notes"]: task.get("notes") or "",
                    field_indices["modified_utc"]: now,
                }
            if changes:
                if layer.isEditable():
                    for feature_id, attributes in changes.items():
                        for field_index, value in attributes.items():
                            layer.changeAttributeValue(feature_id, field_index, value)
                else:
                    layer.dataProvider().changeAttributeValues(changes)
                layer.triggerRepaint()


def _normalise_row(row: Dict) -> Dict:
    out = {}
    for key, value in row.items():
        if value is None:
            out[key] = None
        elif type(value).__name__ == "QVariant":
            out[key] = None if not value.isValid() or value.isNull() else value.value()
        else:
            out[key] = value
    return out


def _migrate_1_to_2(store: PlannerStore) -> None:
    """v1 -> v2: add planner-owned point and line task geometry layers."""
    for table, specs in schema.SPATIAL_TABLES.items():
        if not store._table_exists(table):
            wkb_type = WKB_POINT if table == schema.TABLE_TASK_POINT else WKB_LINESTRING
            store._create_spatial_table(table, specs, wkb_type)


def _migrate_2_to_3(store: PlannerStore) -> None:
    """v2 -> v3: task outlines and per-resource availability offsets."""
    resources = store.read_table(schema.TABLE_RESOURCE)
    for resource in resources:
        resource.setdefault("start_offset_hours", 0.0)
    store._write_table_rows(schema.TABLE_RESOURCE, schema.RESOURCE_FIELDS, resources)
    tasks = store.read_table(schema.TABLE_TASK)
    for task in tasks:
        task.setdefault("is_phase", 0)
        task.setdefault("outline_level", 0)
    store._write_table_rows(schema.TABLE_TASK, schema.TASK_FIELDS, tasks)


MIGRATIONS = {1: _migrate_1_to_2, 2: _migrate_2_to_3}
