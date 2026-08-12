# -*- coding: utf-8 -*-
"""BurialStore — GeoPackage persistence for the Burial Planner.

One GeoPackage per QGIS project (default ``<project>_burial_plans.gpkg``
beside the project file, path remembered in project custom properties and
user-overridable), auto-created on first plan — the ``planner/store.py``
pattern. Registry tables are geometryless GPKG layers read/written whole;
per-plan spatial layers (sections/events) are written via
``write_spatial_layer``. Never deletes registry rows on project teardown;
backs the file up before every migration step.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.core import QgsCoordinateTransformContext, QgsProject, QgsVectorLayer

from ..processing.cable_lay_parsers import (
    fields_from_specs,
    open_gpkg_layer,
    write_layer_to_gpkg,
)
from ..qgis_compat import WKB_NO_GEOMETRY
from . import change_log, schema

PROJECT_SCOPE = "SubseaCableTools"
PROJECT_KEY_GPKG = "burial_gpkg"


def project_gpkg_path(project: Optional[QgsProject] = None) -> Optional[str]:
    """Burial Planner GeoPackage path stored in the QGIS project, if any."""
    project = project or QgsProject.instance()
    value, ok = project.readEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, "")
    return value if ok and value else None


def set_project_gpkg_path(path: str, project: Optional[QgsProject] = None) -> None:
    project = project or QgsProject.instance()
    project.writeEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, path)


def default_project_gpkg_path(project: Optional[QgsProject] = None) -> str:
    project = project or QgsProject.instance()
    return schema.default_gpkg_path(project.fileName(), project.title())


class BurialStore:
    """CRUD access to one Burial Planner GeoPackage."""

    def __init__(self, gpkg_path: str,
                 transform_context: Optional[QgsCoordinateTransformContext] = None):
        self.gpkg_path = gpkg_path
        self.transform_context = transform_context or QgsProject.instance().transformContext()

    # -- lifecycle ----------------------------------------------------------
    def exists(self) -> bool:
        return os.path.exists(self.gpkg_path) and self._table_exists(schema.TABLE_META)

    def ensure_created(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.gpkg_path))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        for table, specs in schema.REGISTRY_TABLES.items():
            if not self._table_exists(table):
                self._write_table_rows(table, specs, [])
        meta = self.read_meta()
        if "schema_version" not in meta:
            self.write_meta("schema_version", str(schema.SCHEMA_VERSION))
            self.write_meta("created_utc", schema.utc_now_iso())

    def migrate(self) -> None:
        """Upgrade to the current SCHEMA_VERSION step-wise, backup before each."""
        self.ensure_created()
        current = int(self.read_meta().get("schema_version", str(schema.SCHEMA_VERSION)))
        while current < schema.SCHEMA_VERSION:
            self.backup_before(f"migrate_v{current}")
            migrator = MIGRATIONS.get(current)
            if migrator is not None:
                migrator(self)
            current += 1
            self.write_meta("schema_version", str(current))

    def backup_before(self, label: str) -> Optional[str]:
        if not os.path.exists(self.gpkg_path):
            return None
        stem, ext = os.path.splitext(self.gpkg_path)
        target = f"{stem}.{schema.sanitize_slug(label)}.bak{ext}"
        try:
            shutil.copy2(self.gpkg_path, target)
            return target
        except OSError:
            return None

    # -- generic table access ------------------------------------------------
    def _table_exists(self, table: str) -> bool:
        if not os.path.exists(self.gpkg_path):
            return False
        return open_gpkg_layer(self.gpkg_path, table) is not None

    def read_table(self, table: str) -> List[Dict]:
        layer = open_gpkg_layer(self.gpkg_path, table)
        if layer is None:
            return []
        names = [f.name() for f in layer.fields() if f.name().lower() != "fid"]
        return [_normalise_row({name: feat[name] for name in names})
                for feat in layer.getFeatures()]

    def write_table(self, table: str, rows: Sequence[Dict]) -> None:
        self._write_table_rows(table, schema.REGISTRY_TABLES[table], list(rows))

    def _write_table_rows(self, table: str, specs, rows: List[Dict]) -> None:
        write_layer_to_gpkg(self.gpkg_path, table, fields_from_specs(specs),
                            WKB_NO_GEOMETRY, rows, self.transform_context)

    def upsert_rows(self, table: str, rows: Sequence[Dict]) -> None:
        key = schema.TABLE_KEYS[table]
        existing = self.read_table(table)
        incoming = {str(r[key]): r for r in rows}
        merged = [r for r in existing if str(r.get(key)) not in incoming]
        merged.extend(rows)
        self.write_table(table, merged)

    def delete_rows(self, table: str, keys: Sequence[str]) -> None:
        key_field = schema.TABLE_KEYS[table]
        drop = {str(k) for k in keys}
        remaining = [r for r in self.read_table(table)
                     if str(r.get(key_field)) not in drop]
        self.write_table(table, remaining)

    # -- meta ----------------------------------------------------------------
    def read_meta(self) -> Dict[str, str]:
        return {r["key"]: r["value"]
                for r in self.read_table(schema.TABLE_META) if r.get("key")}

    def write_meta(self, key: str, value: str) -> None:
        rows = [r for r in self.read_table(schema.TABLE_META) if r.get("key") != key]
        rows.append({"key": key, "value": value})
        self._write_table_rows(schema.TABLE_META, schema.META_FIELDS, rows)

    # -- plans ---------------------------------------------------------------
    def list_plans(self) -> List[Dict]:
        return sorted(self.read_table(schema.TABLE_PLAN),
                      key=lambda r: (r.get("name") or ""))

    def get_plan(self, plan_id: str) -> Optional[Dict]:
        return next((r for r in self.read_table(schema.TABLE_PLAN)
                     if r.get("plan_id") == plan_id), None)

    def save_plan(self, row: Dict) -> str:
        row = dict(row)
        row.setdefault("plan_id", schema.new_id())
        row.setdefault("created_utc", schema.utc_now_iso())
        row.setdefault("status", schema.PLAN_STATUS_DRAFT)
        row.setdefault("rev_label", "Rev 1")
        row.setdefault("rpl_revision", "")
        row.setdefault("direction", 1)
        row["modified_utc"] = schema.utc_now_iso()
        self.upsert_rows(schema.TABLE_PLAN, [row])
        return row["plan_id"]

    def delete_plan(self, plan_id: str) -> None:
        """Remove the plan and all of its child rows (inputs, rules,
        generations, events, sections, change log). Spatial layers stay in
        the gpkg (they may be loaded in the project); callers remove them
        from the layer tree and may overwrite them later."""
        self.delete_rows(schema.TABLE_PLAN, [plan_id])
        for table in (schema.TABLE_INPUT, schema.TABLE_RULE,
                      schema.TABLE_GENERATION, schema.TABLE_EVENT,
                      schema.TABLE_SECTION, schema.TABLE_CHANGE_LOG):
            remaining = [r for r in self.read_table(table)
                         if r.get("plan_id") != plan_id]
            self.write_table(table, remaining)

    def duplicate_plan(self, plan_id: str, new_name: str) -> str:
        """Deep copy of inputs/rules/events/sections with new ids; the copy
        records its lineage via ``supersedes_id``. Generations and the change
        log start fresh (they describe the original's history, not the copy's)."""
        plan = self.get_plan(plan_id)
        if plan is None:
            raise ValueError("Plan not found.")
        new_plan_id = schema.new_id()
        now = schema.utc_now_iso()
        copy = dict(plan)
        copy.update({
            "plan_id": new_plan_id,
            "name": new_name,
            "status": schema.PLAN_STATUS_DRAFT,
            "supersedes_id": plan_id,
            "created_utc": now,
            "modified_utc": now,
        })
        self.upsert_rows(schema.TABLE_PLAN, [copy])

        input_id_map: Dict[str, str] = {}
        new_inputs = []
        for row in self.list_inputs(plan_id):
            new_row = dict(row)
            new_row["input_id"] = schema.new_id()
            input_id_map[str(row.get("input_id"))] = new_row["input_id"]
            new_row["plan_id"] = new_plan_id
            new_inputs.append(new_row)
        if new_inputs:
            self.upsert_rows(schema.TABLE_INPUT, new_inputs)

        new_rules = []
        for row in self.list_rules(plan_id):
            new_row = dict(row)
            new_row["rule_id"] = schema.new_id()
            new_row["plan_id"] = new_plan_id
            # Re-point registered-input references inside the config payload.
            try:
                config = json.loads(new_row.get("config_json") or "{}")
            except (ValueError, TypeError):
                config = {}
            if isinstance(config, dict) and config.get("input_id") in input_id_map:
                config["input_id"] = input_id_map[config["input_id"]]
                new_row["config_json"] = json.dumps(config)
            new_rules.append(new_row)
        if new_rules:
            self.upsert_rows(schema.TABLE_RULE, new_rules)

        new_events = []
        event_id_map: Dict[str, str] = {}
        for row in self.list_events(plan_id):
            new_row = dict(row)
            new_row["event_id"] = schema.new_id()
            event_id_map[str(row.get("event_id"))] = new_row["event_id"]
            new_row["plan_id"] = new_plan_id
            new_row["generation_id"] = ""
            new_events.append(new_row)
        if new_events:
            self.upsert_rows(schema.TABLE_EVENT, new_events)

        new_sections = []
        for row in self.list_sections(plan_id):
            new_row = dict(row)
            new_row["section_id"] = schema.new_id()
            new_row["plan_id"] = new_plan_id
            for col in ("start_event_id", "end_event_id"):
                new_row[col] = event_id_map.get(str(row.get(col) or ""), "")
            new_sections.append(new_row)
        if new_sections:
            self.upsert_rows(schema.TABLE_SECTION, new_sections)
        return new_plan_id

    # -- inputs --------------------------------------------------------------
    def list_inputs(self, plan_id: str) -> List[Dict]:
        rows = [r for r in self.read_table(schema.TABLE_INPUT)
                if r.get("plan_id") == plan_id]
        rows.sort(key=lambda r: (r.get("role") or "", r.get("layer_name") or ""))
        return rows

    def get_input(self, input_id: str) -> Optional[Dict]:
        return next((r for r in self.read_table(schema.TABLE_INPUT)
                     if r.get("input_id") == input_id), None)

    def save_input(self, row: Dict) -> str:
        row = dict(row)
        row.setdefault("input_id", schema.new_id())
        self.upsert_rows(schema.TABLE_INPUT, [row])
        return row["input_id"]

    def delete_input(self, input_id: str) -> None:
        self.delete_rows(schema.TABLE_INPUT, [input_id])

    # -- rules ---------------------------------------------------------------
    def list_rules(self, plan_id: str) -> List[Dict]:
        rules = [r for r in self.read_table(schema.TABLE_RULE)
                 if r.get("plan_id") == plan_id]
        rules.sort(key=lambda r: int(r.get("seq") or 0))
        return rules

    def save_rules(self, plan_id: str, rules: Sequence[Dict]) -> None:
        """Replace the plan's rule stack (seq-normalised, list order wins)."""
        others = [r for r in self.read_table(schema.TABLE_RULE)
                  if r.get("plan_id") != plan_id]
        normalised = []
        for seq, rule in enumerate(rules):
            rule = dict(rule)
            rule["plan_id"] = plan_id
            rule["seq"] = seq
            rule.setdefault("rule_id", schema.new_id())
            normalised.append(rule)
        self.write_table(schema.TABLE_RULE, others + normalised)

    # -- generations ---------------------------------------------------------
    def list_generations(self, plan_id: str) -> List[Dict]:
        rows = [r for r in self.read_table(schema.TABLE_GENERATION)
                if r.get("plan_id") == plan_id]
        rows.sort(key=lambda r: (r.get("run_utc") or ""))
        return rows

    def active_generation(self, plan_id: str) -> Optional[Dict]:
        rows = [r for r in self.list_generations(plan_id) if int(r.get("active") or 0)]
        return rows[-1] if rows else None

    def save_generation(self, row: Dict) -> str:
        row = dict(row)
        row.setdefault("generation_id", schema.new_id())
        row.setdefault("run_utc", schema.utc_now_iso())
        if int(row.get("active") or 0):
            others = []
            for other in self.list_generations(row.get("plan_id") or ""):
                if other.get("generation_id") != row["generation_id"] \
                        and int(other.get("active") or 0):
                    other = dict(other)
                    other["active"] = 0
                    others.append(other)
            if others:
                self.upsert_rows(schema.TABLE_GENERATION, others)
        self.upsert_rows(schema.TABLE_GENERATION, [row])
        return row["generation_id"]

    # -- events --------------------------------------------------------------
    def list_events(self, plan_id: str) -> List[Dict]:
        rows = [r for r in self.read_table(schema.TABLE_EVENT)
                if r.get("plan_id") == plan_id]
        rows.sort(key=lambda r: int(r.get("seq") or 0))
        return rows

    def save_events(self, plan_id: str, rows: Sequence[Dict]) -> None:
        """Replace all events for one plan."""
        others = [r for r in self.read_table(schema.TABLE_EVENT)
                  if r.get("plan_id") != plan_id]
        normalised = []
        for row in rows:
            row = dict(row)
            row["plan_id"] = plan_id
            row.setdefault("event_id", schema.new_id())
            normalised.append(row)
        self.write_table(schema.TABLE_EVENT, others + normalised)

    # -- sections ------------------------------------------------------------
    def list_sections(self, plan_id: str) -> List[Dict]:
        rows = [r for r in self.read_table(schema.TABLE_SECTION)
                if r.get("plan_id") == plan_id]
        rows.sort(key=lambda r: float(r.get("start_kp") or 0.0))
        return rows

    def save_sections(self, plan_id: str, rows: Sequence[Dict]) -> None:
        """Replace all sections for one plan (sections are derived data)."""
        others = [r for r in self.read_table(schema.TABLE_SECTION)
                  if r.get("plan_id") != plan_id]
        normalised = []
        for row in rows:
            row = dict(row)
            row["plan_id"] = plan_id
            row.setdefault("section_id", schema.new_id())
            normalised.append(row)
        self.write_table(schema.TABLE_SECTION, others + normalised)

    # -- change log ----------------------------------------------------------
    def list_change_log(self, plan_id: str) -> List[Dict]:
        rows = [r for r in self.read_table(schema.TABLE_CHANGE_LOG)
                if r.get("plan_id") == plan_id]
        rows.sort(key=lambda r: int(r.get("seq") or 0))
        return rows

    def append_change(self, plan_id: str, action: str, target_id: str = "",
                      before: Optional[Dict] = None, after: Optional[Dict] = None,
                      reason: str = "") -> Dict:
        entries = self.list_change_log(plan_id)
        entry = change_log.make_entry(
            plan_id, change_log.next_seq(entries), action, target_id,
            before, after, reason)
        self.upsert_rows(schema.TABLE_CHANGE_LOG, [entry])
        return entry

    def rollback_to(self, plan_id: str, change_id: str) -> Dict:
        """Restore state to just before ``change_id``; append the rollback.

        Returns the appended rollback entry. History is never deleted.
        """
        entries = self.list_change_log(plan_id)
        ops, undone = change_log.rollback_operations(entries, change_id)
        for table, op, payload in ops:
            if op == "delete":
                self.delete_rows(table, list(payload))
            elif op == "upsert":
                self.upsert_rows(table, list(payload))
        return self.append_change(
            plan_id, change_log.ACTION_ROLLBACK, target_id=change_id,
            after={"undone_change_ids": [e.get("change_id") for e in undone]})

    # -- spatial layers ------------------------------------------------------
    def write_spatial_layer(self, layer_name: str, field_specs, wkb_type,
                            rows: List[Dict]) -> int:
        """Create/overwrite a spatial layer (EPSG:4326) from row dicts with
        WKT geometry under ``cable_lay_parsers.WKT_KEY``."""
        fields = fields_from_specs(field_specs)
        return write_layer_to_gpkg(self.gpkg_path, layer_name, fields, wkb_type,
                                   rows, self.transform_context)

    def open_layer(self, layer_name: str) -> Optional[QgsVectorLayer]:
        return open_gpkg_layer(self.gpkg_path, layer_name)


def _migrate_v1_to_v2(store: BurialStore) -> None:
    """Add the Workbench RPL revision snapshot to existing plan rows."""
    rows = store.read_table(schema.TABLE_PLAN)
    for row in rows:
        row.setdefault("rpl_revision", "")
    store._write_table_rows(schema.TABLE_PLAN, schema.PLAN_FIELDS, rows)


# Maps a starting schema version to the function upgrading it one step.
MIGRATIONS: Dict[int, object] = {1: _migrate_v1_to_v2}


def _normalise_row(row: Dict) -> Dict:
    """Convert QVariant-ish NULLs to plain None for dict-level comparisons."""
    out = {}
    for key, value in row.items():
        if value is None:
            out[key] = None
            continue
        if type(value).__name__ == "QVariant":
            out[key] = None if not value.isValid() or value.isNull() else value.value()
        else:
            out[key] = value
    return out
