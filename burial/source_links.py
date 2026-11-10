# -*- coding: utf-8 -*-
"""Ground Model source inventory and repairs that preserve imported measurements."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class SourceLink:
    owner_kind: str
    owner_id: str
    owner_name: str
    role: str
    kind: str
    config_path: tuple
    value: object
    usage: str

    @property
    def key(self):
        return self.owner_kind, self.owner_id, self.config_path, self.role

    @property
    def owner_key(self):
        return self.owner_kind, self.owner_id


def file_path(path, project=None):
    """Resolve project-relative file links without searching for another delivery."""
    if not path or os.path.isabs(path):
        return path or ""
    if project is None:
        from qgis.core import QgsProject
        project = QgsProject.instance()
    from ..project_storage import absolute_source
    return absolute_source(project, path)


def inventory(campaigns, datasets):
    """Every live layer and original import file used by Ground Model data."""
    links = []
    for kind, records, id_key in (("cpt", campaigns, "campaign_id"), ("numeric", datasets, "dataset_id")):
        for record in records:
            owner = (kind, record[id_key], record.get("name") or "Unnamed import")
            config = record.get("config") or {}

            def add(role, source_kind, path, value, usage="Needed to reload", owner=owner):
                links.append(SourceLink(*owner, role, source_kind, tuple(path), deepcopy(value), usage))

            if kind == "cpt":
                add("CPT readings and positions", "stored", (), "", "Saved in GeoPackage")
                for index, entry in enumerate(config.get("files") or []):
                    add("AGS file", "file", ("files", index, "path"), entry.get("path") or "")
                positions = config.get("positions") or {}
                if positions.get("path"):
                    add("Positions / summary table", "file", ("positions", "path"), positions["path"])
                continue
            measurements = config.get("measurements") or {}
            source = measurements.get("source") or {}
            add("Imported measurements", "stored", (), "", "Saved in GeoPackage")
            if source.get("kind") == "cpt":
                add("Measurements from project CPTs", "stored", (), "", "Reload from saved CPTs")
            elif source.get("kind") == "layer":
                add("Measurements layer", "layer", ("measurements", "source"), source)
            elif source:
                add("Measurements file", "file", ("measurements", "source", "path"), source.get("path") or "")
            placement = config.get("placement") or {}
            if placement.get("layer"):
                add("KP ranges / investigation polygons", "layer", ("placement", "layer"), placement["layer"],
                    "Live placement")
            soil = placement.get("soil_constraint") or {}
            if soil.get("enabled"):
                add("Soil constraint polygons", "layer", ("placement", "soil_constraint", "layer"),
                    soil.get("layer") or {}, "Live placement")
    return links


def link_location(link, value=None, project=None):
    value = link.value if value is None else value
    if link.kind == "file":
        return file_path(value, project)
    if link.kind == "layer":
        return (value or {}).get("layer_source") or (value or {}).get("layer_name") or "(no layer selected)"
    return "Saved in the Burial Planner GeoPackage"


def link_status(link, value=None, project=None):
    value = link.value if value is None else value
    if link.kind == "stored":
        return "Stored"
    if link.kind == "file":
        path = file_path(value, project)
        if not os.path.isfile(path):
            return "Missing original"
        try:
            with open(path, "rb") as stream:
                stream.read(1)
        except OSError:
            return "Unreadable original"
        return "Available"
    from .numeric_datasets import resolve_layer
    return "Available" if resolve_layer(value) is not None else "Unavailable layer"


def _headers(path, config):
    from .plan_import import read_grid
    row = max(1, int(config.get("header_row") or 1))
    try:
        try:
            grid, sheets = read_grid(path, config.get("sheet") or None, max_rows=row + 1)
        except UnicodeDecodeError:
            from .cpt_register import read_table_grid
            grid, sheets = read_table_grid(path, config.get("sheet") or None)
    except Exception as exc:
        raise ValueError(f"The replacement table could not be read: {exc}") from exc
    if config.get("sheet") and sheets and config["sheet"] not in sheets:
        raise ValueError(f"The saved worksheet '{config['sheet']}' is not in this workbook. Use Edit to choose a sheet.")
    if len(grid) < row:
        raise ValueError("The saved header row is beyond the end of this file.")
    return grid[row - 1]


def validate_link(link, value, record, model=None):
    """Check the replacement against saved choices; do not import or stamp it."""
    config = record.get("config") or {}
    if link.kind == "file":
        path = file_path(value)
        if not os.path.isfile(path):
            raise ValueError("Choose an existing file.")
        if link.role == "AGS file":
            from ..geotech.ags import looks_like_ags
            if not looks_like_ags(path):
                raise ValueError("This file does not look like an AGS file.")
            return
        choices = (config.get("positions") if link.owner_kind == "cpt" else config.get("measurements")) or {}
        if link.owner_kind == "numeric":
            choices = dict(choices, sheet=(choices.get("source") or {}).get("sheet") or "")
        columns = set(filter(None, (choices.get("columns") or {}).values()))
        missing = columns - set(_headers(path, choices))
    elif link.kind == "layer":
        from . import kp_table, numeric_datasets
        layer = numeric_datasets.resolve_layer(value)
        if layer is None:
            raise ValueError("The selected layer or table cannot be opened.")
        if link.config_path[0] == "measurements":
            columns = set(filter(None, ((config.get("measurements") or {}).get("columns") or {}).values()))
        else:
            placement = config.get("placement") or {}
            soil = placement.get("soil_constraint") or {}
            if "soil_constraint" in link.config_path:
                columns = {soil.get("unit_field") or ""}
                polygons = True
            else:
                columns = {placement.get("id_field") or ""}
                polygons = placement.get("kind") == numeric_datasets.PLACEMENT_POLYGONS
                if not polygons:
                    columns.update(kp_table.fields(placement))
            if polygons and layer.geometryType() != 2:
                raise ValueError("Choose a polygon layer for this source.")
        missing = columns - {""} - set(layer.fields().names())
    else:
        raise ValueError("This data is already saved in the GeoPackage.")
    if missing:
        raise ValueError("The replacement is missing saved columns: " + ", ".join(sorted(missing))
                         + ". Use Edit to review different column choices.")


def with_relink(record, link, value):
    """Change one link while retaining import fingerprints, IDs and other choices."""
    updated = deepcopy(record)
    target = updated.setdefault("config", {})
    for part in link.config_path[:-1]:
        target = target[part]
    target[link.config_path[-1]] = deepcopy(value)
    return updated


def save_links(model, originals, changes):
    """Commit metadata repairs together; imported CPTs/profiles are never replaced."""
    if not changes:
        return True
    from qgis.core import QgsProject
    project = QgsProject.instance()
    for link, value in changes:
        validate_link(link, value, originals[link.owner_key], model)

    def write():
        from .. import schema_guard
        from . import schema
        schema_guard.check(model.store.read_meta(), schema.SCHEMA_VERSION, "The Burial Planner file")
        current = {("cpt", row["campaign_id"]): row for row in model.store.list_cpt_campaigns()}
        current.update({("numeric", row["dataset_id"]): row for row in model.store.list_ground_datasets()})
        updated = {}
        for link, value in changes:
            original, latest = originals[link.owner_key], current.get(link.owner_key)
            if latest is None or any(latest.get(key) != original.get(key)
                                     for key in ("config", "imported_utc", "updated_utc")):
                raise ValueError("The import changed while its sources were open. Reopen Sources and try again.")
            if link.kind == "file":
                value = project.writePath(file_path(value, project)) if project.fileName() else file_path(value, project)
            record = updated.get(link.owner_key, latest)
            updated[link.owner_key] = with_relink(record, link, value)
        for (kind, ident), record in updated.items():
            if kind == "cpt":
                model.store.update_cpt_campaign_config(ident, record["config"])
            else:
                model.store.save_ground_dataset(record)  # no profiles argument: retain every measurement

    ok, _result = model._store_transaction("repair Ground Model source links", write)
    if ok:
        model.cpts.invalidate()
        model.cptsChanged.emit()
        model.groundChanged.emit()
    return ok
