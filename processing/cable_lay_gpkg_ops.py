# -*- coding: utf-8 -*-
"""File-level management of a project's cable-lay GeoPackage.

The Data Explorer's *Project* tab treats one GeoPackage as the project's data
file (the Burial Planner / Planner / Workbench "one file per QGIS project"
pattern: the path is remembered in the project's custom properties, never
created silently, and rediscovered when the project moves). This module holds
every operation the tab needs in a form that has no GUI dependency, so the
behaviour is unit-testable and runnable from a worker thread:

* :func:`discover_gpkg_path` - saved path -> relocated copy beside the project
  -> file referenced by loaded cable-lay layers -> default beside the project.
* :func:`inventory` - what is actually in the file (tables, row counts, schema
  gaps, last import), read with the standard-library ``sqlite3`` so it can run
  inside a ``QgsTask`` without touching any ``QgsVectorLayer``.
* :func:`create_gpkg`, :func:`add_missing_layers`, :func:`duplicate_gpkg`,
  :func:`delete_gpkg` - the file lifecycle.
* :func:`add_layer_to_project`, :func:`project_layers_for`,
  :func:`remove_project_layers_for` - main-thread project helpers.

Layer resolution is **suffix based**: a canonical type ``cable_lay`` is served
by ``<stem>_cable_lay`` when present, else a bare ``cable_lay``, else any table
ending in ``_cable_lay``. A duplicated or renamed file therefore keeps working
with its old table names, and "add missing layers" never creates a second
table for a type that already has one.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from qgis.core import QgsProject, QgsProviderRegistry, QgsVectorLayer

from . import cable_lay_parsers as clp

PROJECT_SCOPE = "SubseaCableTools"
PROJECT_KEY_GPKG = "cable_lay_gpkg"

#: Display order of the canonical layers (matches the import tools' order in
#: the Processing Toolbox and the Create Cable Lay GeoPackage tool).
IMPORTABLE_TYPES: Tuple[str, ...] = (
    "model_solutions",
    "as_laid",
    "body_logs",
    "cable_lay",
    "event_logs",
    "plough_data",
    "slack_logs",
)
SYSTEM_TYPES: Tuple[str, ...] = ("qc_findings", "qc_config", "import_log", "edit_log")
ALL_TYPES: Tuple[str, ...] = IMPORTABLE_TYPES + SYSTEM_TYPES

TYPE_LABELS: Dict[str, str] = {
    "model_solutions": "3D model solutions",
    "as_laid": "As-laid",
    "body_logs": "Body logs",
    "cable_lay": "Cable lay",
    "event_logs": "Event logs",
    "plough_data": "Plough data",
    "slack_logs": "Slack logs",
    "qc_findings": "QC findings",
    "qc_config": "QC config",
    "import_log": "Import log",
    "edit_log": "Edit log",
}

#: Processing algorithm (provider-qualified id) that fills each importable type.
ALGORITHM_FOR_TYPE: Dict[str, str] = {
    "model_solutions": "subsea_cable_processing:import_3d_model_solutions",
    "as_laid": "subsea_cable_processing:import_as_laid",
    "body_logs": "subsea_cable_processing:import_body_log",
    "cable_lay": "subsea_cable_processing:import_cable_lay",
    "event_logs": "subsea_cable_processing:import_event_log",
    "plough_data": "subsea_cable_processing:import_plough_data",
    "slack_logs": "subsea_cable_processing:import_slack_log",
}

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def all_schemas() -> Dict:
    return {**clp.CANONICAL_SCHEMAS, **clp.QC_SCHEMAS, **clp.MANAGEMENT_SCHEMAS}


# ---------------------------------------------------------------------------
# Project-level path bookkeeping
# ---------------------------------------------------------------------------
def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def same_path(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and _norm(a) == _norm(b)


def project_gpkg_path(project: Optional[QgsProject] = None) -> Optional[str]:
    """The cable-lay data file remembered in the QGIS project, if any."""
    project = project or QgsProject.instance()
    value, ok = project.readEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, "")
    return value if ok and value else None


def set_project_gpkg_path(path: Optional[str], project: Optional[QgsProject] = None) -> None:
    project = project or QgsProject.instance()
    if path:
        project.writeEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG, os.path.abspath(path))
    else:
        project.removeEntry(PROJECT_SCOPE, PROJECT_KEY_GPKG)


def default_project_gpkg_path(project: Optional[QgsProject] = None) -> Optional[str]:
    """``<project stem>_cable_lay.gpkg`` beside the project file (None if unsaved)."""
    project = project or QgsProject.instance()
    project_file = project.fileName() or ""
    if not project_file:
        return None
    folder = os.path.dirname(os.path.abspath(project_file))
    stem = os.path.splitext(os.path.basename(project_file))[0] or "project"
    return os.path.join(folder, f"{stem}_cable_lay.gpkg")


# ---------------------------------------------------------------------------
# Reading the file (sqlite only - safe on a worker thread)
# ---------------------------------------------------------------------------
def _connect_readonly(path: str) -> sqlite3.Connection:
    uri = "file:" + os.path.abspath(path).replace("\\", "/").replace("?", "%3f") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def list_tables(path: str) -> Optional[List[Tuple[str, str]]]:
    """``[(table_name, data_type)]`` from ``gpkg_contents``; None if unreadable.

    ``None`` means "not a GeoPackage" (missing file, not SQLite, or no
    ``gpkg_contents`` table) - callers should treat it as invalid, not empty.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        conn = _connect_readonly(path)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gpkg_contents'"
        ).fetchone()
        if row is None:
            return None
        rows = conn.execute(
            "SELECT table_name, data_type FROM gpkg_contents ORDER BY table_name"
        ).fetchall()
        return [(str(name), str(kind or "")) for name, kind in rows]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def is_geopackage(path: str) -> bool:
    return list_tables(path) is not None


def layer_type_for_name(table_name: str) -> Optional[str]:
    """Canonical type served by a physical table name (suffix match), else None."""
    for layer_type in ALL_TYPES:
        if table_name == layer_type or table_name.endswith("_" + layer_type):
            return layer_type
    return None


def find_layer_for_type(path: str, tables: Sequence[str], layer_type: str) -> Optional[str]:
    """Best physical table for ``layer_type`` among ``tables`` (see module doc)."""
    preferred = clp.prefixed_layer_name(path, layer_type)
    names = list(tables)
    if preferred in names:
        return preferred
    if layer_type in names:
        return layer_type
    matches = sorted(n for n in names if n.endswith("_" + layer_type))
    return matches[0] if matches else None


def tables_for_type(tables: Sequence[str], layer_type: str) -> List[str]:
    return [n for n in tables if layer_type_for_name(n) == layer_type]


def is_cable_lay_gpkg(path: str) -> bool:
    """True when the file is a GeoPackage holding at least one cable-lay table."""
    tables = list_tables(path)
    if not tables:
        return False
    return any(layer_type_for_name(name) in IMPORTABLE_TYPES for name, _ in tables)


def _count_rows(conn: sqlite3.Connection, table: str) -> Optional[int]:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {clp_quote(table)}").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None


def clp_quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({clp_quote(table)})")]
    except sqlite3.Error:
        return []


def _last_imports(conn: sqlite3.Connection, tables: Sequence[str]) -> Dict[str, Tuple[str, str]]:
    """``{layer_name: (imported_at, source_file)}`` of the latest import per layer."""
    log_table = None
    for name in tables:
        if layer_type_for_name(name) == "import_log":
            log_table = name
            break
    if log_table is None:
        return {}
    result: Dict[str, Tuple[str, str]] = {}
    try:
        rows = conn.execute(
            f"SELECT layer_name, MAX(imported_at), source_file FROM {clp_quote(log_table)} "
            "GROUP BY layer_name"
        ).fetchall()
    except sqlite3.Error:
        return {}
    for layer_name, stamp, source in rows:
        if layer_name:
            result[str(layer_name)] = (str(stamp or ""), str(source or ""))
    return result


def inventory(
    path: str,
    progress: Optional[Callable[[float], None]] = None,
    is_canceled: Optional[Callable[[], bool]] = None,
) -> Dict:
    """Describe the file: one entry per canonical type plus any extra tables.

    Returns ``{"path", "exists", "valid", "error", "size_bytes", "entries",
    "extras"}``. Each entry: ``{"type", "label", "importable", "name",
    "exists", "rows", "missing_columns", "duplicates", "last_import"}``.
    ``name`` is the physical table (None when missing). Runs entirely on
    sqlite so it is safe inside a worker thread; a multi-GB file costs one
    ``COUNT(*)`` per table, which is why callers run it in a task.
    """
    result: Dict = {
        "path": path, "exists": bool(path) and os.path.isfile(path), "valid": False,
        "error": "", "size_bytes": 0, "entries": [], "extras": [],
    }
    if not result["exists"]:
        result["error"] = "File not found." if path else "No data file selected."
        return result
    try:
        result["size_bytes"] = os.path.getsize(path)
    except OSError:
        pass
    tables = list_tables(path)
    if tables is None:
        result["error"] = "Not a readable GeoPackage (no gpkg_contents table)."
        return result
    names = [name for name, _ in tables]
    result["valid"] = True
    try:
        conn = _connect_readonly(path)
    except sqlite3.Error as exc:
        result["valid"] = False
        result["error"] = f"Could not open the file: {exc}"
        return result
    try:
        last = _last_imports(conn, names)
        schemas = all_schemas()
        total = max(len(ALL_TYPES), 1)
        for index, layer_type in enumerate(ALL_TYPES):
            if is_canceled is not None and is_canceled():
                result["error"] = "Cancelled."
                return result
            physical = find_layer_for_type(path, names, layer_type)
            entry = {
                "type": layer_type,
                "label": TYPE_LABELS.get(layer_type, layer_type),
                "importable": layer_type in IMPORTABLE_TYPES,
                "name": physical,
                "exists": physical is not None,
                "rows": None,
                "missing_columns": [],
                "duplicates": [],
                "last_import": last.get(physical or "", ("", "")),
            }
            if physical is not None:
                entry["rows"] = _count_rows(conn, physical)
                have = set(_columns(conn, physical))
                specs = schemas.get(layer_type, (None, []))[1]
                entry["missing_columns"] = [n for n, _ in specs if n not in have]
                entry["duplicates"] = [
                    n for n in tables_for_type(names, layer_type) if n != physical
                ]
            result["entries"].append(entry)
            if progress is not None:
                progress((index + 1) / total * 100.0)
        known = {e["name"] for e in result["entries"] if e["name"]}
        known |= {d for e in result["entries"] for d in e["duplicates"]}
        for name, kind in tables:
            if name not in known:
                result["extras"].append({
                    "name": name, "kind": kind, "rows": _count_rows(conn, name),
                })
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# File lifecycle
# ---------------------------------------------------------------------------
def create_gpkg(path: str, transform_context) -> List[str]:
    """Create a new data file with every canonical layer (empty).

    Refuses to touch an existing file so a user can never overwrite data by
    picking the wrong name. Returns the created table names in display order.
    """
    if os.path.exists(path):
        raise RuntimeError(
            f"{os.path.basename(path)} already exists. Use Open to work with it, "
            "or choose a different name."
        )
    folder = os.path.dirname(os.path.abspath(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    schemas = all_schemas()
    created: List[str] = []
    for layer_type in ALL_TYPES:
        wkb_type, specs = schemas[layer_type]
        name = clp.prefixed_layer_name(path, layer_type)
        clp.write_layer_to_gpkg(
            path, name, clp.fields_from_specs(specs), wkb_type, [], transform_context
        )
        created.append(name)
    return created


def ensure_layer(path: str, layer_type: str, transform_context) -> str:
    """Return the table serving ``layer_type``, creating the standard one if absent."""
    tables = list_tables(path)
    if tables is None:
        raise RuntimeError("Not a readable GeoPackage.")
    names = [name for name, _ in tables]
    existing = find_layer_for_type(path, names, layer_type)
    if existing is not None:
        return existing
    wkb_type, specs = all_schemas()[layer_type]
    name = clp.prefixed_layer_name(path, layer_type)
    clp.write_layer_to_gpkg(
        path, name, clp.fields_from_specs(specs), wkb_type, [], transform_context
    )
    return name


def add_missing_layers(path: str, transform_context) -> List[str]:
    """Create any canonical type with no table in ``path`` (suffix aware)."""
    tables = list_tables(path)
    if tables is None:
        raise RuntimeError("Not a readable GeoPackage.")
    names = [name for name, _ in tables]
    schemas = all_schemas()
    created: List[str] = []
    for layer_type in ALL_TYPES:
        if find_layer_for_type(path, names, layer_type) is not None:
            continue
        wkb_type, specs = schemas[layer_type]
        name = clp.prefixed_layer_name(path, layer_type)
        clp.write_layer_to_gpkg(
            path, name, clp.fields_from_specs(specs), wkb_type, [], transform_context
        )
        created.append(name)
    return created


def duplicate_gpkg(
    src: str,
    dst: str,
    progress: Optional[Callable[[float], None]] = None,
    is_canceled: Optional[Callable[[], bool]] = None,
) -> str:
    """Copy ``src`` to ``dst`` with the SQLite backup API.

    Consistent even while QGIS has the source open (WAL contents included),
    reports progress per page batch, and never leaves a partial target behind
    on failure or cancellation. Table names are kept as they are - the copy is
    resolved by suffix, see the module docstring.
    """
    if not os.path.isfile(src):
        raise RuntimeError("The source file does not exist.")
    if os.path.exists(dst):
        raise RuntimeError(
            f"{os.path.basename(dst)} already exists. Choose a different name."
        )
    if same_path(src, dst):
        raise RuntimeError("Choose a different file name for the copy.")
    folder = os.path.dirname(os.path.abspath(dst))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)

    class _Cancelled(Exception):
        pass

    def _report(_status, remaining, total):
        if progress is not None and total:
            progress((total - remaining) / float(total) * 100.0)
        if is_canceled is not None and is_canceled():
            raise _Cancelled()

    source = None
    target = None
    try:
        source = sqlite3.connect(src, timeout=10.0)
        target = sqlite3.connect(dst)
        source.backup(target, pages=2048, progress=_report)
    except _Cancelled:
        _remove_quietly(dst)
        raise RuntimeError("Cancelled.")
    except sqlite3.Error as exc:
        _remove_quietly(dst)
        raise RuntimeError(f"Copy failed: {exc}")
    finally:
        for conn in (target, source):
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
    return dst


def _remove_quietly(path: str) -> None:
    for suffix in ("",) + SIDECAR_SUFFIXES:
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def delete_gpkg(path: str) -> None:
    """Delete the file and its WAL/journal sidecars.

    On Windows a file still open by QGIS (a loaded layer) cannot be removed;
    the caller must drop project layers first. Raises ``RuntimeError`` with a
    plain explanation when the OS refuses.
    """
    if not os.path.exists(path):
        return
    try:
        os.remove(path)
    except PermissionError:
        raise RuntimeError(
            "The file is still in use. Remove its layers from the QGIS project "
            "(and close it in any other application), then try again."
        )
    except OSError as exc:
        raise RuntimeError(f"Could not delete the file: {exc}")
    for suffix in SIDECAR_SUFFIXES:
        try:
            os.remove(path + suffix)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# QGIS project helpers (main thread)
# ---------------------------------------------------------------------------
def decode_layer_source(layer) -> Tuple[str, str]:
    """``(gpkg_path, table_name)`` for a project layer, or ``("", "")``."""
    try:
        decoded = QgsProviderRegistry.instance().decodeUri(
            layer.providerType(), layer.source()
        )
    except Exception:
        return "", ""
    path = str(decoded.get("path", "") or "")
    name = str(decoded.get("layerName", "") or "")
    if not path.lower().endswith(".gpkg"):
        return "", ""
    return path, name


def project_layers_for(path: str, project: Optional[QgsProject] = None) -> List[Tuple[object, str]]:
    """``[(layer, table_name)]`` of every project layer backed by ``path``."""
    project = project or QgsProject.instance()
    found = []
    for layer in project.mapLayers().values():
        src, name = decode_layer_source(layer)
        if src and same_path(src, path):
            found.append((layer, name))
    return found


def project_layer_for_table(path: str, table: str, project: Optional[QgsProject] = None):
    for layer, name in project_layers_for(path, project):
        if name == table:
            return layer
    return None


def add_layer_to_project(
    path: str, table: str, project: Optional[QgsProject] = None, group_name: Optional[str] = None
):
    """Add (or reuse) the project layer for ``table`` inside the file's group.

    Returns the layer, or None if it could not be opened. Existing layers are
    returned untouched (their tree position is respected).
    """
    project = project or QgsProject.instance()
    existing = project_layer_for_table(path, table, project)
    if existing is not None:
        return existing
    layer = QgsVectorLayer(clp.gpkg_layer_uri(path, table), table, "ogr")
    if not layer.isValid():
        return None
    project.addMapLayer(layer, False)
    root = project.layerTreeRoot()
    group_name = group_name or clp.gpkg_stem(path) or "Cable Lay"
    group = root.findGroup(group_name)
    if group is None:
        group = root.insertGroup(0, group_name)
    group.addLayer(layer)
    return layer


def remove_project_layers_for(path: str, project: Optional[QgsProject] = None) -> int:
    project = project or QgsProject.instance()
    ids = [layer.id() for layer, _ in project_layers_for(path, project)]
    if ids:
        project.removeMapLayers(ids)
    return len(ids)


def discover_gpkg_path(project: Optional[QgsProject] = None) -> Tuple[Optional[str], str]:
    """Locate the project's data file without creating anything.

    Returns ``(path, note)``. ``path`` is None when nothing usable exists;
    ``note`` explains a recovery ("saved file missing, using ...") so the UI
    can tell the user rather than silently switching files.
    """
    project = project or QgsProject.instance()
    saved = project_gpkg_path(project)
    if saved and is_geopackage(saved):
        return saved, ""

    project_file = project.fileName() or ""
    folder = os.path.dirname(os.path.abspath(project_file)) if project_file else ""
    if saved and folder:
        relocated = os.path.join(folder, os.path.basename(saved))
        if is_geopackage(relocated):
            return relocated, (
                "The data file saved in this project was not found at\n"
                f"{saved}\n\nUsing the copy beside the project instead:\n{relocated}"
            )

    # A file the user already has loaded: pick the one serving most types.
    candidates: Dict[str, Tuple[int, str]] = {}  # norm path -> (type count, path)
    for layer in project.mapLayers().values():
        src, name = decode_layer_source(layer)
        if not src or layer_type_for_name(name) not in IMPORTABLE_TYPES:
            continue
        key = _norm(src)
        if key in candidates or not os.path.isfile(src):
            continue
        tables = list_tables(src) or []
        served = sum(
            1 for table_name, _ in tables
            if layer_type_for_name(table_name) in IMPORTABLE_TYPES
        )
        candidates[key] = (served, src)
    if candidates:
        best = max(candidates.values(), key=lambda item: item[0])[1]
        note = ""
        if saved:
            note = (
                "The data file saved in this project was not found at\n"
                f"{saved}\n\nUsing the file behind the loaded cable-lay layers instead:\n{best}"
            )
        return best, note

    default = default_project_gpkg_path(project)
    if default and is_geopackage(default):
        note = ""
        if saved:
            note = (
                "The data file saved in this project was not found at\n"
                f"{saved}\n\nUsing the project-side data file instead:\n{default}"
            )
        return default, note
    if saved:
        return None, f"The data file saved in this project was not found:\n{saved}"
    return None, ""
