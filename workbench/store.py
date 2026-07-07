# -*- coding: utf-8 -*-
"""WorkbenchStore — GeoPackage persistence for the Cable Route Workbench.

Wraps a single per-project GeoPackage holding the registry tables declared in
schema.py plus the per-RPL spatial layers. Registry tables are small and are
never loaded into the QGIS project, so they are read/written whole (the
spatial RPL layers, which ARE loaded, are only ever edited through QGIS edit
buffers — see rpl_layer_io.py).

Also enforces the CRA-core topology invariants on wb_component/wb_port/
wb_connection (see validate_topology).
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..processing.cable_lay_parsers import (
    WKT_KEY,
    fields_from_specs,
    open_gpkg_layer,
    write_layer_to_gpkg,
)
from . import schema

PROJECT_SCOPE = "SubseaCableTools"
PROJECT_KEY_GPKG = "workbench_gpkg"


def project_gpkg_path(project: Optional[QgsProject] = None) -> Optional[str]:
    """Workbench GeoPackage path stored in the QGIS project, if any."""
    project = project or QgsProject.instance()
    value, ok = project.readEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, "")
    return value if ok and value else None


def set_project_gpkg_path(path: str, project: Optional[QgsProject] = None) -> None:
    project = project or QgsProject.instance()
    project.writeEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, path)


def default_project_gpkg_path(project: Optional[QgsProject] = None) -> str:
    project = project or QgsProject.instance()
    return schema.default_gpkg_path(project.fileName(), project.title())


class WorkbenchStore:
    """CRUD access to one workbench GeoPackage."""

    def __init__(self, gpkg_path: str, transform_context: Optional[QgsCoordinateTransformContext] = None):
        self.gpkg_path = gpkg_path
        self.transform_context = transform_context or QgsProject.instance().transformContext()

    # -- lifecycle ----------------------------------------------------------
    def exists(self) -> bool:
        return os.path.exists(self.gpkg_path) and self._table_exists(schema.TABLE_META)

    def ensure_created(self) -> None:
        """Create any missing registry tables (idempotent)."""
        for table, specs in schema.REGISTRY_TABLES.items():
            if not self._table_exists(table):
                self._write_table_rows(table, specs, [])
        meta = self.read_meta()
        if "schema_version" not in meta:
            self.write_meta("schema_version", str(schema.SCHEMA_VERSION))
            self.write_meta("created_utc", schema.utc_now_iso())
        if not self.read_table(schema.TABLE_EVENT_RULE):
            self.seed_default_event_rules()

    def migrate(self) -> None:
        """Upgrade the gpkg to the current SCHEMA_VERSION, one step at a time.

        ``ensure_created`` first adds any tables missing from the declared
        schema (so a pre-v2 gpkg gains the new registry tables), then each
        registered migrator runs in order, backing the file up before every
        step and advancing the stamped ``schema_version``.
        """
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
        """Copy the gpkg aside before a structural change. Returns the copy path."""
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
        rows: List[Dict] = []
        for feature in layer.getFeatures():
            rows.append(_normalise_row({name: feature[name] for name in names}))
        return rows

    def write_table(self, table: str, rows: Sequence[Dict]) -> None:
        specs = schema.REGISTRY_TABLES[table]
        self._write_table_rows(table, specs, list(rows))

    def _write_table_rows(self, table: str, specs, rows: List[Dict]) -> None:
        fields = fields_from_specs(specs)
        write_layer_to_gpkg(
            self.gpkg_path,
            table,
            fields,
            QgsWkbTypes.NoGeometry,
            rows,
            self.transform_context,
        )

    def upsert_rows(self, table: str, rows: Sequence[Dict]) -> None:
        """Insert or replace rows by the table's primary key."""
        key = schema.TABLE_KEYS[table]
        existing = self.read_table(table)
        incoming = {str(r[key]): r for r in rows}
        merged = [r for r in existing if str(r.get(key)) not in incoming]
        merged.extend(rows)
        self.write_table(table, merged)

    def delete_rows(self, table: str, keys: Sequence[str]) -> None:
        key_field = schema.TABLE_KEYS[table]
        drop = {str(k) for k in keys}
        remaining = [r for r in self.read_table(table) if str(r.get(key_field)) not in drop]
        self.write_table(table, remaining)

    # -- meta -----------------------------------------------------------------
    def read_meta(self) -> Dict[str, str]:
        return {r["key"]: r["value"] for r in self.read_table(schema.TABLE_META) if r.get("key")}

    def write_meta(self, key: str, value: str) -> None:
        rows = [r for r in self.read_table(schema.TABLE_META) if r.get("key") != key]
        rows.append({"key": key, "value": value})
        self._write_table_rows(schema.TABLE_META, schema.META_FIELDS, rows)

    # -- assemblies -------------------------------------------------------------
    def list_assemblies(self) -> List[Dict]:
        return sorted(self.read_table(schema.TABLE_ASSEMBLY), key=lambda r: (r.get("name") or ""))

    def get_assembly(self, assembly_id: str) -> Tuple[Optional[Dict], List[Dict]]:
        header = next(
            (r for r in self.read_table(schema.TABLE_ASSEMBLY) if r.get("assembly_id") == assembly_id),
            None,
        )
        items = [
            r for r in self.read_table(schema.TABLE_ASSEMBLY_ITEM) if r.get("assembly_id") == assembly_id
        ]
        items.sort(key=lambda r: int(r.get("seq") or 0))
        return header, items

    def save_assembly(self, header: Dict, items: Sequence[Dict]) -> None:
        """Upsert an assembly header and replace its items."""
        assembly_id = header["assembly_id"]
        header = dict(header)
        header.setdefault("created_utc", schema.utc_now_iso())
        header["modified_utc"] = schema.utc_now_iso()
        self.upsert_rows(schema.TABLE_ASSEMBLY, [header])
        others = [
            r for r in self.read_table(schema.TABLE_ASSEMBLY_ITEM) if r.get("assembly_id") != assembly_id
        ]
        normalised = []
        for seq, item in enumerate(items):
            item = dict(item)
            item["assembly_id"] = assembly_id
            item["seq"] = seq
            item.setdefault("item_id", schema.new_id())
            normalised.append(item)
        self.write_table(schema.TABLE_ASSEMBLY_ITEM, others + normalised)

    def delete_assembly(self, assembly_id: str) -> None:
        self.delete_rows(schema.TABLE_ASSEMBLY, [assembly_id])
        remaining = [
            r for r in self.read_table(schema.TABLE_ASSEMBLY_ITEM) if r.get("assembly_id") != assembly_id
        ]
        self.write_table(schema.TABLE_ASSEMBLY_ITEM, remaining)
        # cascade: fits and topology component referencing this assembly
        fit_ids = [r["fit_id"] for r in self.read_table(schema.TABLE_FIT) if r.get("assembly_id") == assembly_id]
        if fit_ids:
            self.delete_rows(schema.TABLE_FIT, fit_ids)
        self._delete_components_for_subject(assembly_id)

    # -- RPLs -------------------------------------------------------------------
    def list_rpls(self) -> List[Dict]:
        return sorted(self.read_table(schema.TABLE_RPL), key=lambda r: (r.get("name") or ""))

    def get_rpl(self, rpl_id: str) -> Optional[Dict]:
        return next((r for r in self.read_table(schema.TABLE_RPL) if r.get("rpl_id") == rpl_id), None)

    def save_rpl(self, row: Dict) -> None:
        row = dict(row)
        row.setdefault("created_utc", schema.utc_now_iso())
        row["modified_utc"] = schema.utc_now_iso()
        self.upsert_rows(schema.TABLE_RPL, [row])

    def delete_rpl(self, rpl_id: str) -> None:
        """Remove the registry row, its fits, and its topology component.

        Does NOT drop the spatial layers from the gpkg (they may be loaded in
        the project); callers should remove them from the layer tree and may
        recreate/overwrite them later.
        """
        self.delete_rows(schema.TABLE_RPL, [rpl_id])
        fit_ids = [r["fit_id"] for r in self.read_table(schema.TABLE_FIT) if r.get("rpl_id") == rpl_id]
        if fit_ids:
            self.delete_rows(schema.TABLE_FIT, fit_ids)
        for assessment in self.list_assessments(rpl_id):
            self.delete_assessment(assessment["assessment_id"])
        self._delete_components_for_subject(rpl_id)

    def rpl_depth_config(self, rpl_id: str) -> Dict:
        row = self.get_rpl(rpl_id)
        if not row or not row.get("depth_source_config"):
            return {}
        try:
            return json.loads(row["depth_source_config"])
        except (ValueError, TypeError):
            return {}

    # -- fits ---------------------------------------------------------------------
    def list_fits(self, rpl_id: Optional[str] = None, assembly_id: Optional[str] = None) -> List[Dict]:
        rows = self.read_table(schema.TABLE_FIT)
        if rpl_id is not None:
            rows = [r for r in rows if r.get("rpl_id") == rpl_id]
        if assembly_id is not None:
            rows = [r for r in rows if r.get("assembly_id") == assembly_id]
        return rows

    def save_fit(self, row: Dict) -> None:
        row = dict(row)
        row.setdefault("fit_id", schema.new_id())
        row.setdefault("created_utc", schema.utc_now_iso())
        self.upsert_rows(schema.TABLE_FIT, [row])

    def delete_fit(self, fit_id: str) -> None:
        self.delete_rows(schema.TABLE_FIT, [fit_id])

    # -- event rules ------------------------------------------------------------
    def list_event_rules(self) -> List[Dict]:
        rules = self.read_table(schema.TABLE_EVENT_RULE)
        rules.sort(key=lambda r: int(r.get("priority") or 0))
        return rules

    def save_event_rules(self, rows: Sequence[Dict]) -> None:
        normalised = []
        for row in rows:
            row = dict(row)
            row.setdefault("rule_id", schema.new_id())
            normalised.append(row)
        self.write_table(schema.TABLE_EVENT_RULE, normalised)

    def seed_default_event_rules(self) -> None:
        rows = [
            {
                "rule_id": schema.new_id(),
                "pattern": pattern,
                "category": category,
                "body_type": body_type,
                "priority": priority,
            }
            for pattern, category, body_type, priority in schema.DEFAULT_EVENT_RULES
        ]
        self.write_table(schema.TABLE_EVENT_RULE, rows)

    # -- rule sets / rules (route-suitability engine) ----------------------------
    def list_rule_sets(self) -> List[Dict]:
        return sorted(self.read_table(schema.TABLE_RULE_SET), key=lambda r: (r.get("name") or ""))

    def get_rule_set(self, rule_set_id: str) -> Optional[Dict]:
        return next(
            (r for r in self.read_table(schema.TABLE_RULE_SET) if r.get("rule_set_id") == rule_set_id),
            None,
        )

    def list_rules(self, rule_set_id: str) -> List[Dict]:
        rules = [r for r in self.read_table(schema.TABLE_RULE) if r.get("rule_set_id") == rule_set_id]
        rules.sort(key=lambda r: int(r.get("seq") or 0))
        return rules

    def save_rule_set(self, header: Dict, rules: Sequence[Dict]) -> str:
        """Upsert a rule-set header and replace its rules (seq-normalised)."""
        header = dict(header)
        header.setdefault("rule_set_id", schema.new_id())
        rule_set_id = header["rule_set_id"]
        header.setdefault("created_utc", schema.utc_now_iso())
        header["modified_utc"] = schema.utc_now_iso()
        self.upsert_rows(schema.TABLE_RULE_SET, [header])
        others = [r for r in self.read_table(schema.TABLE_RULE) if r.get("rule_set_id") != rule_set_id]
        normalised = []
        for seq, rule in enumerate(rules):
            rule = dict(rule)
            rule["rule_set_id"] = rule_set_id
            rule["seq"] = seq
            rule.setdefault("rule_id", schema.new_id())
            normalised.append(rule)
        self.write_table(schema.TABLE_RULE, others + normalised)
        return rule_set_id

    def delete_rule_set(self, rule_set_id: str) -> None:
        self.delete_rows(schema.TABLE_RULE_SET, [rule_set_id])
        remaining = [r for r in self.read_table(schema.TABLE_RULE) if r.get("rule_set_id") != rule_set_id]
        self.write_table(schema.TABLE_RULE, remaining)

    def seed_default_rule_set(self) -> str:
        """Create the default 'Burial Assessment' template. Returns its id."""
        rule_set_id = schema.new_id()
        header = {
            "rule_set_id": rule_set_id,
            "name": schema.DEFAULT_RULE_SET_NAME,
            "description": "Starter burial-suitability rules (depth + slope). Add hazard/soil rules to suit.",
            "methods_json": json.dumps(schema.DEFAULT_ASSESSMENT_METHODS),
        }
        rules = [
            {
                "rule_id": schema.new_id(),
                "name": name,
                "enabled": 1,
                "kind": kind,
                "action": action,
                "risk_level": risk_level,
                "methods_json": json.dumps(methods),
                "config_json": json.dumps(config),
                "notes": "",
            }
            for name, kind, action, risk_level, methods, config in schema.DEFAULT_RULES
        ]
        self.save_rule_set(header, rules)
        return rule_set_id

    # -- assessments -------------------------------------------------------------
    def list_assessments(self, rpl_id: Optional[str] = None) -> List[Dict]:
        rows = self.read_table(schema.TABLE_ASSESSMENT)
        if rpl_id is not None:
            rows = [r for r in rows if r.get("rpl_id") == rpl_id]
        return sorted(rows, key=lambda r: (r.get("name") or ""))

    def get_assessment(self, assessment_id: str) -> Optional[Dict]:
        return next(
            (r for r in self.read_table(schema.TABLE_ASSESSMENT) if r.get("assessment_id") == assessment_id),
            None,
        )

    def save_assessment(self, row: Dict) -> str:
        row = dict(row)
        row.setdefault("assessment_id", schema.new_id())
        row.setdefault("created_utc", schema.utc_now_iso())
        row["modified_utc"] = schema.utc_now_iso()
        self.upsert_rows(schema.TABLE_ASSESSMENT, [row])
        return row["assessment_id"]

    def delete_assessment(self, assessment_id: str) -> None:
        self.delete_rows(schema.TABLE_ASSESSMENT, [assessment_id])
        remaining = [
            r for r in self.read_table(schema.TABLE_ASSESSMENT_RANGE)
            if r.get("assessment_id") != assessment_id
        ]
        self.write_table(schema.TABLE_ASSESSMENT_RANGE, remaining)

    def list_assessment_ranges(self, assessment_id: str) -> List[Dict]:
        rows = [
            r for r in self.read_table(schema.TABLE_ASSESSMENT_RANGE)
            if r.get("assessment_id") == assessment_id
        ]
        rows.sort(key=lambda r: (r.get("method") or "", float(r.get("start_kp") or 0.0)))
        return rows

    def save_assessment_ranges(self, assessment_id: str, rows: Sequence[Dict]) -> None:
        """Replace all stored ranges for one assessment."""
        others = [
            r for r in self.read_table(schema.TABLE_ASSESSMENT_RANGE)
            if r.get("assessment_id") != assessment_id
        ]
        normalised = []
        for row in rows:
            row = dict(row)
            row["assessment_id"] = assessment_id
            row.setdefault("range_id", schema.new_id())
            normalised.append(row)
        self.write_table(schema.TABLE_ASSESSMENT_RANGE, others + normalised)

    def mark_assessments_stale(self, rpl_id: str) -> None:
        """Flag every current assessment of an RPL as stale (RPL changed)."""
        rows = self.read_table(schema.TABLE_ASSESSMENT)
        changed = False
        for row in rows:
            if row.get("rpl_id") == rpl_id and row.get("status") == "current":
                row["status"] = "stale"
                changed = True
        if changed:
            self.write_table(schema.TABLE_ASSESSMENT, rows)

    # -- topology (CRA core) -----------------------------------------------------
    def list_components(self) -> List[Dict]:
        return self.read_table(schema.TABLE_COMPONENT)

    def list_ports(self) -> List[Dict]:
        return self.read_table(schema.TABLE_PORT)

    def list_connections(self) -> List[Dict]:
        return self.read_table(schema.TABLE_CONNECTION)

    def list_systems(self) -> List[Dict]:
        return self.read_table(schema.TABLE_SYSTEM)

    def save_component(self, row: Dict, port_labels: Sequence[str] = ()) -> str:
        """Upsert a component; optionally create its ports if it has none."""
        row = dict(row)
        row.setdefault("component_id", schema.new_id())
        self.upsert_rows(schema.TABLE_COMPONENT, [row])
        component_id = row["component_id"]
        if port_labels:
            existing = [p for p in self.list_ports() if p.get("component_id") == component_id]
            if not existing:
                ports = [
                    {"port_id": schema.new_id(), "component_id": component_id, "label": label}
                    for label in port_labels
                ]
                self.upsert_rows(schema.TABLE_PORT, ports)
        return component_id

    def component_for_subject(self, subject_id: str) -> Optional[Dict]:
        return next(
            (c for c in self.list_components() if c.get("subject_id") == subject_id),
            None,
        )

    def delete_component(self, component_id: str) -> None:
        port_ids = [p["port_id"] for p in self.list_ports() if p.get("component_id") == component_id]
        if port_ids:
            port_set = set(port_ids)
            conn_ids = [
                c["connection_id"]
                for c in self.list_connections()
                if c.get("port_a_id") in port_set or c.get("port_b_id") in port_set
            ]
            if conn_ids:
                self.delete_rows(schema.TABLE_CONNECTION, conn_ids)
            self.delete_rows(schema.TABLE_PORT, port_ids)
        self.delete_rows(schema.TABLE_COMPONENT, [component_id])

    def _delete_components_for_subject(self, subject_id: str) -> None:
        for component in self.list_components():
            if component.get("subject_id") == subject_id:
                self.delete_component(component["component_id"])

    def connect_ports(self, port_a_id: str, port_b_id: str) -> str:
        """Create a connection, enforcing the CRA core invariants."""
        findings = self._connection_violations(port_a_id, port_b_id)
        if findings:
            raise ValueError("; ".join(f["message"] for f in findings))
        connection_id = schema.new_id()
        self.upsert_rows(
            schema.TABLE_CONNECTION,
            [{"connection_id": connection_id, "port_a_id": port_a_id, "port_b_id": port_b_id}],
        )
        return connection_id

    def disconnect(self, connection_id: str) -> None:
        self.delete_rows(schema.TABLE_CONNECTION, [connection_id])

    def _connection_violations(self, port_a_id: str, port_b_id: str) -> List[Dict]:
        ports = {p["port_id"]: p for p in self.list_ports()}
        findings: List[Dict] = []
        for pid in (port_a_id, port_b_id):
            if pid not in ports:
                findings.append(_finding("connection.port_missing", "error",
                                         f"Port {pid} does not exist.", "connection", ""))
        if findings:
            return findings
        if port_a_id == port_b_id or (
            ports[port_a_id]["component_id"] == ports[port_b_id]["component_id"]
        ):
            findings.append(_finding("connection.self_loop", "error",
                                     "A connection's two ports must belong to different components.",
                                     "connection", ""))
        used = set()
        for conn in self.list_connections():
            used.add(conn.get("port_a_id"))
            used.add(conn.get("port_b_id"))
        for pid in (port_a_id, port_b_id):
            if pid in used:
                findings.append(_finding("connection.port_overconnected", "error",
                                         f"Port {pid} already participates in a connection.",
                                         "connection", ""))
        return findings

    def validate_topology(self) -> List[Dict]:
        """CRA core validation over the whole topology. Returns findings."""
        components = {c["component_id"] for c in self.list_components()}
        ports = {p["port_id"]: p for p in self.list_ports()}
        findings: List[Dict] = []

        for port in ports.values():
            if port.get("component_id") not in components:
                findings.append(_finding(
                    "port.component_missing", "error",
                    f"Port {port['port_id']} references missing component {port.get('component_id')}.",
                    "port", port["port_id"],
                ))

        seen_ports: Dict[str, int] = {}
        for conn in self.list_connections():
            cid = conn.get("connection_id") or ""
            pa, pb = conn.get("port_a_id"), conn.get("port_b_id")
            for pid in (pa, pb):
                if pid not in ports:
                    findings.append(_finding(
                        "connection.port_missing", "error",
                        f"Connection {cid} references missing port {pid}.",
                        "connection", cid,
                    ))
                else:
                    seen_ports[pid] = seen_ports.get(pid, 0) + 1
            if pa in ports and pb in ports:
                if pa == pb or ports[pa]["component_id"] == ports[pb]["component_id"]:
                    findings.append(_finding(
                        "connection.self_loop", "error",
                        f"Connection {cid} joins two ports of the same component.",
                        "connection", cid,
                    ))
        for pid, count in seen_ports.items():
            if count > 1:
                findings.append(_finding(
                    "connection.port_overconnected", "error",
                    f"Port {pid} participates in {count} connections.",
                    "port", pid,
                ))
        return findings

    # -- spatial layers ------------------------------------------------------------
    def write_spatial_layer(self, layer_name: str, field_specs, wkb_type, rows: List[Dict]) -> int:
        """Create/overwrite a spatial layer (EPSG:4326) from row dicts with WKT_KEY geometry."""
        fields = fields_from_specs(field_specs)
        return write_layer_to_gpkg(
            self.gpkg_path, layer_name, fields, wkb_type, rows, self.transform_context
        )

    def open_layer(self, layer_name: str) -> Optional[QgsVectorLayer]:
        return open_gpkg_layer(self.gpkg_path, layer_name)


def _migrate_1_to_2(store: "WorkbenchStore") -> None:
    """v1 -> v2: adds the rules-engine registry tables.

    The new tables (wb_rule_set / wb_rule / wb_assessment / wb_assessment_range)
    are created by ``ensure_created`` before any migrator runs, so this step
    only needs to guarantee they exist; it deliberately does not touch existing
    rows. Kept explicit so the migration framework has a real first step.
    """
    for table in (
        schema.TABLE_RULE_SET,
        schema.TABLE_RULE,
        schema.TABLE_ASSESSMENT,
        schema.TABLE_ASSESSMENT_RANGE,
    ):
        if not store._table_exists(table):
            store._write_table_rows(table, schema.REGISTRY_TABLES[table], [])


# Maps a starting schema version to the function that upgrades it by one step.
MIGRATIONS = {
    1: _migrate_1_to_2,
}


def _finding(rule_id: str, severity: str, message: str, object_type: str, object_id: str) -> Dict:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "message": message,
        "object_type": object_type,
        "object_id": object_id,
    }


def _normalise_row(row: Dict) -> Dict:
    """Convert QVariant-ish NULLs to plain None for dict-level comparisons."""
    out = {}
    for key, value in row.items():
        if value is None:
            out[key] = None
            continue
        # PyQt may hand back a QVariant for NULL attribute values.
        type_name = type(value).__name__
        if type_name == "QVariant":
            out[key] = None if not value.isValid() or value.isNull() else value.value()
        else:
            out[key] = value
    return out


WKT = WKT_KEY  # re-export: geometry key used in spatial row dicts
