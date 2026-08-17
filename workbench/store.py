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
import re
import shutil
from typing import Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsProject,
    QgsVectorLayer,
)

from ..processing.cable_lay_parsers import (
    WKT_KEY,
    fields_from_specs,
    open_gpkg_layer,
    write_layer_to_gpkg,
)
from ..qgis_compat import (
    FIELD_TYPE_DOUBLE,
    FIELD_TYPE_INT,
    FIELD_TYPE_LONG_LONG,
    WKB_NO_GEOMETRY,
)
from . import schema

PROJECT_SCOPE = "SubseaCableTools"
PROJECT_KEY_GPKG = "workbench_gpkg"


class WorkbenchReadOnlyError(ValueError):
    """Raised when code tries to mutate an issued workbench entity."""


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
        # Registry tables are small but opening an OGR layer for every lookup
        # is not. One Workbench screen used to reopen wb_rpl dozens of times.
        # Keep a per-store read-through cache; every mutator below refreshes it.
        self._table_cache: Dict[str, List[Dict]] = {}
        self._table_exists_cache: Dict[str, bool] = {}

    def clear_cache(self) -> None:
        """Forget registry reads, e.g. after an external Processing run."""
        self._table_cache.clear()
        self._table_exists_cache.clear()

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
        if table in self._table_exists_cache:
            return self._table_exists_cache[table]
        if not os.path.exists(self.gpkg_path):
            return False
        exists = open_gpkg_layer(self.gpkg_path, table) is not None
        self._table_exists_cache[table] = exists
        return exists

    def read_table(self, table: str) -> List[Dict]:
        if table in self._table_cache:
            return [dict(row) for row in self._table_cache[table]]
        layer = open_gpkg_layer(self.gpkg_path, table)
        if layer is None:
            return []
        names = [f.name() for f in layer.fields() if f.name().lower() != "fid"]
        rows: List[Dict] = []
        for feature in layer.getFeatures():
            rows.append(_normalise_row({name: feature[name] for name in names}))
        self._table_cache[table] = [dict(row) for row in rows]
        self._table_exists_cache[table] = True
        return [dict(row) for row in rows]

    def write_table(self, table: str, rows: Sequence[Dict]) -> None:
        specs = schema.REGISTRY_TABLES[table]
        self._write_table_rows(table, specs, list(rows))

    def _write_table_rows(self, table: str, specs, rows: List[Dict]) -> None:
        fields = fields_from_specs(specs)
        write_layer_to_gpkg(
            self.gpkg_path,
            table,
            fields,
            WKB_NO_GEOMETRY,
            rows,
            self.transform_context,
        )
        self._table_cache[table] = [dict(row) for row in rows]
        self._table_exists_cache[table] = True

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

    # -- routes ---------------------------------------------------------------
    def list_routes(self) -> List[Dict]:
        return sorted(self.read_table(schema.TABLE_ROUTE), key=lambda r: (r.get("name") or ""))

    def get_route(self, route_id: str) -> Optional[Dict]:
        return next(
            (r for r in self.read_table(schema.TABLE_ROUTE) if r.get("route_id") == route_id),
            None,
        )

    def create_route(self, name: str, system_id: str = "", description: str = "",
                     notes: str = "") -> str:
        now = schema.utc_now_iso()
        route_id = schema.new_id()
        self.upsert_rows(schema.TABLE_ROUTE, [{
            "route_id": route_id,
            "name": name,
            "system_id": system_id or "",
            "description": description or "",
            "created_utc": now,
            "modified_utc": now,
            "notes": notes or "",
        }])
        self.save_component({
            "component_id": schema.new_id(), "kind": "route",
            "subject_id": route_id, "name": name,
            "system_id": system_id or "",
        }, port_labels=["A", "B"])
        return route_id

    def save_route(self, row: Dict) -> None:
        row = dict(row)
        row.setdefault("route_id", schema.new_id())
        row.setdefault("created_utc", schema.utc_now_iso())
        row["modified_utc"] = schema.utc_now_iso()
        self.upsert_rows(schema.TABLE_ROUTE, [row])
        component = self.component_for_segment(row["route_id"])
        if component is not None:
            component["name"] = row.get("name") or component.get("name") or "Cable segment"
            component["system_id"] = row.get("system_id") or ""
            self.save_component(component)

    def delete_route(self, route_id: str) -> None:
        if self.revisions_of_route(route_id):
            raise ValueError("Cannot delete a route while it still has RPL revisions.")
        for makeup in self.list_makeups(route_id):
            self.delete_makeup(makeup.get("makeup_id") or "")
        self._delete_components_for_subject(route_id)
        self.delete_rows(schema.TABLE_ROUTE, [route_id])

    def assign_route_to_system(self, route_id: str, system_id: str = "") -> None:
        row = self.get_route(route_id)
        if row is None:
            raise ValueError("Route not found.")
        row["system_id"] = system_id or ""
        self.save_route(row)

    def revisions_of_route(self, route_id: str) -> List[Dict]:
        """A route's revisions in revision order (oldest first, latest last).

        Numbered labels ("Rev 2") order numerically so importing revisions
        out of receipt order (Rev 3 before a corrected Rev 2) still leaves
        the highest revision as latest; unnumbered labels ("As-laid final")
        sort after all numbered ones by creation time.
        """
        rows = [r for r in self.read_table(schema.TABLE_RPL) if r.get("route_id") == route_id]

        def sort_key(row):
            match = re.search(r"\brev\s*(\d+)\b", str(row.get("rev_label") or ""), re.IGNORECASE)
            number = int(match.group(1)) if match else float("inf")
            return (number, row.get("created_utc") or "", row.get("name") or "")

        rows.sort(key=sort_key)
        return rows

    def latest_revision(self, route_id: str) -> Optional[Dict]:
        revisions = self.revisions_of_route(route_id)
        return revisions[-1] if revisions else None

    def supersedes_chain(self, table: str, row_id: str) -> List[Dict]:
        key = schema.TABLE_KEYS[table]
        rows = {str(r.get(key)): r for r in self.read_table(table)}
        chain: List[Dict] = []
        seen = set()
        cursor = str(row_id or "")
        while cursor and cursor not in seen:
            seen.add(cursor)
            row = rows.get(cursor)
            if row is None:
                break
            chain.append(row)
            cursor = str(row.get("supersedes_id") or "")
        return chain

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
        existing, _ = self.get_assembly(assembly_id)
        if _is_issued(existing):
            raise WorkbenchReadOnlyError("Issued assemblies are read-only. Create a new revision to edit.")
        merged = dict(existing or {})
        merged.update(dict(header))
        header = merged
        header.setdefault("created_utc", schema.utc_now_iso())
        header.setdefault("rev_label", schema.next_rev_label([]))
        header.setdefault("status", schema.STATUS_DRAFT)
        header.setdefault("supersedes_id", "")
        header.setdefault("issued_utc", "")
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
        placements = [
            row for row in self.read_table(schema.TABLE_MAKEUP_ITEM)
            if row.get("kind") == "assembly" and row.get("assembly_id") == assembly_id
        ]
        if placements:
            raise ValueError(
                "Cannot delete an assembly while it is used in a cable-segment make-up.")
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
        existing = self.get_rpl(row.get("rpl_id") or "")
        if _is_issued(existing):
            raise WorkbenchReadOnlyError("Issued RPL revisions are read-only. Create a new revision to edit.")
        merged = dict(existing or {})
        merged.update(dict(row))
        row = merged
        row.setdefault("created_utc", schema.utc_now_iso())
        if not row.get("route_id"):
            row["route_id"] = self.create_route(row.get("name") or "Route")
        row.setdefault("rev_label", schema.next_rev_label([]))
        row.setdefault("status", schema.STATUS_DRAFT)
        row.setdefault("supersedes_id", "")
        row.setdefault("issued_utc", "")
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

    def new_rpl_revision(self, rpl_id: str, rev_label: Optional[str] = None) -> str:
        old = self.get_rpl(rpl_id)
        if old is None:
            raise ValueError("RPL not found.")
        route_id = old.get("route_id")
        if not route_id or self.get_route(route_id) is None:
            route_id = self.create_route(old.get("name") or "Route")
            old["route_id"] = route_id
            self.write_table(schema.TABLE_RPL, [
                old if r.get("rpl_id") == rpl_id else r
                for r in self.read_table(schema.TABLE_RPL)
            ])
        route = self.get_route(route_id) or {"name": old.get("name") or "Route"}
        if not rev_label:
            rev_label = schema.next_rev_label(self.revisions_of_route(route_id))

        new_id = schema.new_id()
        new_name = f"{route.get('name') or old.get('name') or 'Route'} {rev_label}".strip()
        existing_layers = _registered_layer_names(self.read_table(schema.TABLE_RPL))
        points_layer = schema.unique_layer_name(
            existing_layers, schema.rpl_points_layer_name(new_name))
        existing_layers.add(points_layer)
        lines_layer = schema.unique_layer_name(
            existing_layers, schema.rpl_lines_layer_name(new_name))

        self.copy_spatial_layer(old.get("points_layer") or "", points_layer, {"rpl_id": new_id})
        self.copy_spatial_layer(old.get("lines_layer") or "", lines_layer, {"rpl_id": new_id})

        now = schema.utc_now_iso()
        new_row = dict(old)
        new_row.update({
            "rpl_id": new_id,
            "name": new_name,
            "route_id": route_id,
            "rev_label": rev_label,
            "status": schema.STATUS_DRAFT,
            "supersedes_id": rpl_id,
            "issued_utc": "",
            "points_layer": points_layer,
            "lines_layer": lines_layer,
            "created_utc": now,
            "modified_utc": now,
        })
        self.upsert_rows(schema.TABLE_RPL, [new_row])

        fit_rows = []
        for fit in self.list_fits(rpl_id=rpl_id):
            copied = dict(fit)
            copied["fit_id"] = schema.new_id()
            copied["rpl_id"] = new_id
            copied["created_utc"] = now
            fit_rows.append(copied)
        if fit_rows:
            self.upsert_rows(schema.TABLE_FIT, fit_rows)

        self.ensure_segment_component(route_id)
        return new_id

    def new_assembly_revision(self, assembly_id: str, rev_label: Optional[str] = None) -> str:
        header, items = self.get_assembly(assembly_id)
        if header is None:
            raise ValueError("Assembly not found.")
        base_name = _strip_rev_label(header.get("name") or "Assembly")
        if not rev_label:
            related = [
                a for a in self.list_assemblies()
                if _strip_rev_label(a.get("name") or "") == base_name
            ]
            rev_label = schema.next_rev_label(related)
        new_id = schema.new_id()
        now = schema.utc_now_iso()
        new_header = dict(header)
        new_header.update({
            "assembly_id": new_id,
            "name": f"{base_name} {rev_label}".strip(),
            "rev_label": rev_label,
            "status": schema.STATUS_DRAFT,
            "supersedes_id": assembly_id,
            "issued_utc": "",
            "created_utc": now,
            "modified_utc": now,
        })
        new_items = []
        for seq, item in enumerate(items):
            copied = dict(item)
            copied["item_id"] = schema.new_id()
            copied["assembly_id"] = new_id
            copied["seq"] = seq
            new_items.append(copied)
        self.save_assembly(new_header, new_items)
        return new_id

    def copy_spatial_layer(self, src: str, dst: str, overrides: Optional[Dict] = None) -> str:
        layer = self.open_layer(src)
        if layer is None:
            raise ValueError(f"Layer '{src}' not found.")
        overrides = overrides or {}
        specs = _field_specs_from_layer(layer)
        rows: List[Dict] = []
        field_names = [field.name() for field in layer.fields() if field.name().lower() != "fid"]
        for feature in layer.getFeatures():
            row = {name: feature[name] for name in field_names}
            row.update(overrides)
            geom = feature.geometry()
            row[WKT_KEY] = geom.asWkt() if geom is not None and not geom.isEmpty() else None
            rows.append(_normalise_row(row))
        self.write_spatial_layer(dst, specs, layer.wkbType(), rows)
        return dst

    def issue_rpl(self, rpl_id: str) -> None:
        self._set_status(schema.TABLE_RPL, rpl_id, schema.STATUS_ISSUED)

    def reopen_rpl(self, rpl_id: str) -> None:
        self._set_status(schema.TABLE_RPL, rpl_id, schema.STATUS_DRAFT)

    def issue_assembly(self, assembly_id: str) -> None:
        self._set_status(schema.TABLE_ASSEMBLY, assembly_id, schema.STATUS_ISSUED)

    def reopen_assembly(self, assembly_id: str) -> None:
        self._set_status(schema.TABLE_ASSEMBLY, assembly_id, schema.STATUS_DRAFT)

    def _set_status(self, table: str, row_id: str, status: str) -> None:
        key = schema.TABLE_KEYS[table]
        rows = self.read_table(table)
        now = schema.utc_now_iso()
        changed = False
        for row in rows:
            if row.get(key) == row_id:
                row["status"] = status
                row["issued_utc"] = now if status == schema.STATUS_ISSUED else ""
                if "modified_utc" in row:
                    row["modified_utc"] = now
                changed = True
                break
        if not changed:
            raise ValueError("Entity not found.")
        self.write_table(table, rows)

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

    # -- cable-segment make-up -----------------------------------------------
    def list_makeups(self, route_id: Optional[str] = None) -> List[Dict]:
        rows = self.read_table(schema.TABLE_MAKEUP)
        if route_id is not None:
            rows = [row for row in rows if row.get("route_id") == route_id]
        return sorted(rows, key=lambda row: (
            row.get("created_utc") or "", row.get("name") or ""))

    def get_makeup(self, makeup_id: str) -> Tuple[Optional[Dict], List[Dict]]:
        header = next((
            row for row in self.read_table(schema.TABLE_MAKEUP)
            if row.get("makeup_id") == makeup_id
        ), None)
        items = [
            row for row in self.read_table(schema.TABLE_MAKEUP_ITEM)
            if row.get("makeup_id") == makeup_id
        ]
        items.sort(key=lambda row: int(row.get("seq") or 0))
        return header, items

    def current_makeup(self, route_id: str) -> Tuple[Optional[Dict], List[Dict]]:
        rows = self.list_makeups(route_id)
        if not rows:
            return None, []
        current = rows[-1]
        return self.get_makeup(current.get("makeup_id") or "")

    def save_makeup(self, header: Dict, items: Sequence[Dict]) -> str:
        header = dict(header)
        makeup_id = header.setdefault("makeup_id", schema.new_id())
        existing, _existing_items = self.get_makeup(makeup_id)
        if _is_issued(existing):
            raise WorkbenchReadOnlyError(
                "Issued cable make-ups are read-only. Create a new revision to edit.")
        merged = dict(existing or {})
        merged.update(header)
        merged.setdefault("name", "Cable make-up")
        merged.setdefault("rev_label", schema.next_rev_label([]))
        merged.setdefault("status", schema.STATUS_DRAFT)
        merged.setdefault("supersedes_id", "")
        merged.setdefault("created_utc", schema.utc_now_iso())
        merged.setdefault("notes", "")
        merged["modified_utc"] = schema.utc_now_iso()
        self.upsert_rows(schema.TABLE_MAKEUP, [merged])

        others = [
            row for row in self.read_table(schema.TABLE_MAKEUP_ITEM)
            if row.get("makeup_id") != makeup_id
        ]
        normalised = []
        for seq, source in enumerate(items):
            row = dict(source)
            row.setdefault("makeup_item_id", schema.new_id())
            row["makeup_id"] = makeup_id
            row["seq"] = seq
            row.setdefault("kind", "assembly")
            row.setdefault("assembly_id", "")
            row.setdefault("name", "")
            row.setdefault("direction", 1)
            row.setdefault("use_start_m", None)
            row.setdefault("use_end_m", None)
            row.setdefault("params_json", "{}")
            row.setdefault("notes", "")
            normalised.append(row)
        self.write_table(schema.TABLE_MAKEUP_ITEM, others + normalised)
        return makeup_id

    def ensure_makeup(self, route_id: str) -> Tuple[Dict, List[Dict]]:
        header, items = self.current_makeup(route_id)
        if header is not None:
            return header, items
        route = self.get_route(route_id) or {}
        header = {
            "makeup_id": schema.new_id(), "route_id": route_id,
            "name": f"{route.get('name') or 'Cable segment'} make-up",
            "rev_label": "Rev 1", "status": schema.STATUS_DRAFT,
            "supersedes_id": "", "notes": "",
        }
        self.save_makeup(header, [])
        return self.get_makeup(header["makeup_id"])

    def add_makeup_assembly(self, route_id: str, assembly_id: str,
                            direction: int = 1) -> str:
        assembly, _rows = self.get_assembly(assembly_id)
        if assembly is None:
            raise ValueError("Assembly not found.")
        header, items = self.ensure_makeup(route_id)
        placements = [item for item in items if item.get("kind") == "assembly"]
        if placements:
            items.append({
                "makeup_item_id": schema.new_id(), "kind": "joint",
                "name": f"Joint J{len(placements):02d}", "direction": 1,
                "params_json": "{}", "notes": "",
            })
        item_id = schema.new_id()
        items.append({
            "makeup_item_id": item_id, "kind": "assembly",
            "assembly_id": assembly_id,
            "name": assembly.get("name") or "Assembly",
            "direction": 1 if int(direction or 1) >= 0 else -1,
            "use_start_m": None, "use_end_m": None,
            "params_json": "{}", "notes": "",
        })
        self.save_makeup(header, items)
        return item_id

    def delete_makeup_item(self, makeup_item_id: str) -> None:
        source = next((
            row for row in self.read_table(schema.TABLE_MAKEUP_ITEM)
            if row.get("makeup_item_id") == makeup_item_id
        ), None)
        if source is None:
            return
        header, items = self.get_makeup(source.get("makeup_id") or "")
        if header is None:
            return
        index = next(i for i, row in enumerate(items)
                     if row.get("makeup_item_id") == makeup_item_id)
        drop = {index}
        if source.get("kind") == "assembly":
            if index + 1 < len(items) and items[index + 1].get("kind") == "joint":
                drop.add(index + 1)
            elif index > 0 and items[index - 1].get("kind") == "joint":
                drop.add(index - 1)
        self.save_makeup(header, [row for i, row in enumerate(items) if i not in drop])

    def delete_makeup(self, makeup_id: str) -> None:
        self.delete_rows(schema.TABLE_MAKEUP, [makeup_id])
        remaining = [
            row for row in self.read_table(schema.TABLE_MAKEUP_ITEM)
            if row.get("makeup_id") != makeup_id
        ]
        self.write_table(schema.TABLE_MAKEUP_ITEM, remaining)

    # -- event rules ------------------------------------------------------------
    def list_event_rules(self) -> List[Dict]:
        rules = self.read_table(schema.TABLE_EVENT_RULE)
        for rule in rules:
            # older registries had a third "installation" bucket; those marks
            # are map references, so they read as geographic now
            if (rule.get("category") or "").strip().lower() == schema.LEGACY_CATEGORY_INSTALLATION:
                rule["category"] = schema.CATEGORY_GEOGRAPHIC
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

    def create_system(self, name: str, notes: str = "") -> str:
        system_id = schema.new_id()
        self.upsert_rows(schema.TABLE_SYSTEM, [{
            "system_id": system_id,
            "name": name,
            "notes": notes or "",
        }])
        return system_id

    def save_system(self, row: Dict) -> None:
        row = dict(row)
        row.setdefault("system_id", schema.new_id())
        self.upsert_rows(schema.TABLE_SYSTEM, [row])

    def delete_system(self, system_id: str) -> None:
        # wb_system is shared by the manual route grouping and the topology
        # assignment cache. Clear manual route references; topology code may
        # recreate derived rows later if the port graph still needs them.
        routes = self.read_table(schema.TABLE_ROUTE)
        changed = False
        for route in routes:
            if route.get("system_id") == system_id:
                route["system_id"] = ""
                changed = True
        if changed:
            self.write_table(schema.TABLE_ROUTE, routes)
        self.delete_rows(schema.TABLE_SYSTEM, [system_id])

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

    def component_for_segment(self, route_id: str) -> Optional[Dict]:
        """Topology component for a stable cable-segment identity.

        The fallback recognises pre-v4 components that referenced an RPL
        revision, allowing the semantic migration and damaged stores to repair
        themselves without discarding connected ports.
        """
        direct = next(
            (c for c in self.list_components()
             if c.get("kind") == "route" and c.get("subject_id") == route_id),
            None,
        )
        if direct is not None:
            return direct
        revision_ids = {r.get("rpl_id") for r in self.revisions_of_route(route_id)}
        candidates = [
            c for c in self.list_components()
            if c.get("kind") == "rpl" and c.get("subject_id") in revision_ids
        ]
        if not candidates:
            return None
        used_ports = {
            pid for connection in self.list_connections()
            for pid in (connection.get("port_a_id"), connection.get("port_b_id"))
        }
        ports = self.list_ports()
        candidates.sort(
            key=lambda c: sum(1 for p in ports
                              if p.get("component_id") == c.get("component_id")
                              and p.get("port_id") in used_ports),
            reverse=True,
        )
        return candidates[0]

    def ensure_segment_component(self, route_id: str) -> str:
        route = self.get_route(route_id)
        if route is None:
            raise ValueError("Cable segment not found.")
        component = self.component_for_segment(route_id)
        if component is None:
            return self.save_component({
                "component_id": schema.new_id(), "kind": "route",
                "subject_id": route_id, "name": route.get("name") or "Cable segment",
                "system_id": route.get("system_id") or "",
            }, port_labels=["A", "B"])
        component["kind"] = "route"
        component["subject_id"] = route_id
        component["name"] = route.get("name") or component.get("name") or "Cable segment"
        component["system_id"] = route.get("system_id") or component.get("system_id") or ""
        self.save_component(component, port_labels=["A", "B"])
        return component["component_id"]

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


def _migrate_2_to_3(store: "WorkbenchStore") -> None:
    """v2 -> v3: route table plus lightweight RPL/assembly revision lineage."""
    rpls = store.read_table(schema.TABLE_RPL)
    routes: List[Dict] = []
    now = schema.utc_now_iso()
    for rpl in rpls:
        route_id = rpl.get("route_id") or schema.new_id()
        component = store.component_for_subject(rpl.get("rpl_id") or "")
        route = {
            "route_id": route_id,
            "name": rpl.get("name") or "Route",
            "system_id": (component or {}).get("system_id") or "",
            "description": "",
            "created_utc": rpl.get("created_utc") or now,
            "modified_utc": rpl.get("modified_utc") or now,
            "notes": "",
        }
        routes.append(route)
        rpl["route_id"] = route_id
        rpl["rev_label"] = rpl.get("rev_label") or "Rev 1"
        rpl["status"] = rpl.get("status") or schema.STATUS_DRAFT
        rpl["supersedes_id"] = rpl.get("supersedes_id") or ""
        rpl["issued_utc"] = rpl.get("issued_utc") or ""

    assemblies = store.read_table(schema.TABLE_ASSEMBLY)
    for assembly in assemblies:
        assembly["rev_label"] = assembly.get("rev_label") or "Rev 1"
        assembly["status"] = assembly.get("status") or schema.STATUS_DRAFT
        assembly["supersedes_id"] = assembly.get("supersedes_id") or ""
        assembly["issued_utc"] = assembly.get("issued_utc") or ""

    store.write_table(schema.TABLE_ROUTE, routes)
    store.write_table(schema.TABLE_RPL, rpls)
    store.write_table(schema.TABLE_ASSEMBLY, assemblies)


def _migrate_3_to_4(store: "WorkbenchStore") -> None:
    """v3 -> v4: topology follows stable cable segments, not RPL revisions."""
    for route in store.list_routes():
        route_id = route.get("route_id") or ""
        if not route_id:
            continue
        canonical_id = store.ensure_segment_component(route_id)
        revision_ids = {r.get("rpl_id") for r in store.revisions_of_route(route_id)}
        duplicates = [
            c for c in store.list_components()
            if c.get("component_id") != canonical_id
            and c.get("kind") == "rpl"
            and c.get("subject_id") in revision_ids
        ]
        for duplicate in duplicates:
            if _merge_component_ports(store, duplicate["component_id"], canonical_id):
                store.delete_component(duplicate["component_id"])
            else:
                # Preserve conflicting historical wiring for manual review.
                duplicate["kind"] = "legacy_rpl"
                duplicate["name"] = "Legacy topology — " + (duplicate.get("name") or "RPL")
                store.save_component(duplicate)


def _merge_component_ports(store: "WorkbenchStore", source_id: str,
                           target_id: str) -> bool:
    """Move non-conflicting A/B connections; False preserves any conflict."""
    ports = store.list_ports()
    source = {p.get("label"): p for p in ports if p.get("component_id") == source_id}
    target = {p.get("label"): p for p in ports if p.get("component_id") == target_id}
    connections = store.list_connections()
    changed = False
    for label, source_port in source.items():
        source_pid = source_port.get("port_id")
        connection = next(
            (c for c in connections
             if source_pid in (c.get("port_a_id"), c.get("port_b_id"))), None)
        if connection is None:
            continue
        target_port = target.get(label)
        if target_port is None:
            return False
        target_pid = target_port.get("port_id")
        occupied = next(
            (c for c in connections
             if target_pid in (c.get("port_a_id"), c.get("port_b_id"))), None)
        if occupied is not None:
            return False
        if connection.get("port_a_id") == source_pid:
            connection["port_a_id"] = target_pid
        else:
            connection["port_b_id"] = target_pid
        changed = True
    if changed:
        store.write_table(schema.TABLE_CONNECTION, connections)
    return True


def _migrate_4_to_5(store: "WorkbenchStore") -> None:
    """Seed segment make-ups from fits on each segment's latest RPL.

    Fits remain untouched: they are geometric analysis records. A lone fit is
    an unambiguous one-placement make-up; several fits are retained in anchor
    order but explicitly marked for review because legacy data did not record
    physical ordering or joint intent.
    """
    assemblies = {
        row.get("assembly_id"): row for row in store.list_assemblies()
    }
    latest_by_route = {}
    for rpl in store.list_rpls():
        route_id = rpl.get("route_id") or ""
        current = latest_by_route.get(route_id)
        key = (rpl.get("created_utc") or "", rpl.get("name") or "")
        current_key = ((current or {}).get("created_utc") or "",
                       (current or {}).get("name") or "")
        if current is None or key >= current_key:
            latest_by_route[route_id] = rpl

    for route in store.list_routes():
        route_id = route.get("route_id") or ""
        if store.list_makeups(route_id):
            continue
        latest = latest_by_route.get(route_id)
        if latest is None:
            continue
        fits = sorted(
            store.list_fits(rpl_id=latest.get("rpl_id") or ""),
            key=lambda row: float(row.get("anchor_kp_km") or 0.0),
        )
        fits = [fit for fit in fits if fit.get("assembly_id") in assemblies]
        if not fits:
            continue
        review = len(fits) > 1
        header = {
            "makeup_id": schema.new_id(), "route_id": route_id,
            "name": f"{route.get('name') or 'Cable segment'} make-up",
            "rev_label": "Rev 1", "status": schema.STATUS_DRAFT,
            "supersedes_id": "",
            "notes": (
                "Migrated from multiple legacy assembly fits. Review physical "
                "assembly order, directions and joins."
                if review else "Migrated from the legacy assembly fit."),
        }
        items = []
        for index, fit in enumerate(fits):
            if index:
                items.append({
                    "makeup_item_id": schema.new_id(), "kind": "joint",
                    "name": f"Joint J{index:02d}", "direction": 1,
                    "params_json": "{}",
                    "notes": "Inferred between legacy fits; review required.",
                })
            assembly = assemblies.get(fit.get("assembly_id")) or {}
            items.append({
                "makeup_item_id": schema.new_id(), "kind": "assembly",
                "assembly_id": fit.get("assembly_id") or "",
                "name": assembly.get("name") or "Assembly",
                "direction": 1 if int(fit.get("direction") or 1) >= 0 else -1,
                "use_start_m": None, "use_end_m": None,
                "params_json": "{}",
                "notes": "Migrated from fit " + str(fit.get("fit_id") or ""),
            })
        store.save_makeup(header, items)


# Maps a starting schema version to the function that upgrades it by one step.
MIGRATIONS = {
    1: _migrate_1_to_2,
    2: _migrate_2_to_3,
    3: _migrate_3_to_4,
    4: _migrate_4_to_5,
}


def _is_issued(row: Optional[Dict]) -> bool:
    return bool(row and row.get("status") == schema.STATUS_ISSUED)


def _registered_layer_names(rpl_rows: Sequence[Dict]) -> set:
    names = set()
    for row in rpl_rows:
        if row.get("points_layer"):
            names.add(row.get("points_layer"))
        if row.get("lines_layer"):
            names.add(row.get("lines_layer"))
    return names


def _strip_rev_label(name: str) -> str:
    stripped = re.sub(r"\s+Rev\s+\d+\s*$", "", name or "", flags=re.IGNORECASE).strip()
    return stripped or (name or "Assembly")


def _field_specs_from_layer(layer: QgsVectorLayer) -> List[Tuple[str, str]]:
    specs: List[Tuple[str, str]] = []
    for field in layer.fields():
        name = field.name()
        if name.lower() == "fid":
            continue
        field_type = field.type()
        if field_type == FIELD_TYPE_DOUBLE:
            type_str = "float"
        elif field_type in (FIELD_TYPE_INT, FIELD_TYPE_LONG_LONG):
            type_str = "int"
        else:
            type_str = "str"
        specs.append((name, type_str))
    return specs


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
