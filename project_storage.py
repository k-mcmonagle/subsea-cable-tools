# -*- coding: utf-8 -*-
"""Suggested store locations and repair of manually relocated GeoPackages.

Never moves, copies or creates a database. Existing files keep their paths;
only missing files are searched for, by the same filename in known folders.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from .plugin_log import log_warning

SCOPE = "SubseaCableTools"
RELOCATIONS_KEY = "geopackage_relocations"
FOLDERS = {
    "burial_gpkg": "burial_plans", "workbench_gpkg": "workbench",
    "planner_gpkg": "planner", "span_analysis_gpkg": "span_analysis",
    "cable_lay_gpkg": "cable_lay", "route_comments_register": "route_comments",
}
REGISTRIES = {"burial_plans": "bp_plan", "workbench": "wb_rpl", "planner": "pow_scenario",
              "span_analysis": "sa_scenario", "route_comments": "rc_comment"}


class FileSelectionCancelled(Exception):
    """The user cancelled selection of a new store; no write should follow."""


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def absolute_source(project, source):
    """Expand a stored path without invoking QGIS's path preprocessors."""
    if os.path.isabs(source) or not project.fileName():
        return source
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(project.fileName())), source))


def stored_project_path(project, key):
    value, ok = project.readEntry(SCOPE, key, "")
    return absolute_source(project, value) if ok and value else None


def table_names(path):
    """Read-only, short-lived probe: never creates a missing SQLite file."""
    if not os.path.isfile(path):
        return set()
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except (OSError, sqlite3.Error):
        return set()


def default_gpkg_path(project_path, project_title, folder, suffix, folder_for, slug, prefer_existing=True):
    """Suggest a subfolder, preserving an existing legacy default when binding a store."""
    stem = os.path.splitext(os.path.basename(project_path))[0] if project_path else (
        slug(project_title) if project_title else "project")
    filename = f"{stem}_{suffix}.gpkg"
    if project_path and prefer_existing:
        root = os.path.dirname(os.path.abspath(project_path))
        # Keep old defaults in use; upgrading never chooses a competing copy.
        for path in (os.path.join(root, filename), os.path.join(root, folder, filename)):
            if os.path.isfile(path):
                return path
    base = folder_for(project_path)
    if not project_path and prefer_existing and os.path.isfile(os.path.join(base, filename)):
        return os.path.join(base, filename)
    return os.path.join(base, folder, filename)


def relocated_path(project_file, source, folder=None):
    """Find one recognizable copy of a missing file. Ambiguity requires a user choice."""
    path, sep, suffix = str(source or "").partition("|")
    if not path or not path.lower().endswith(".gpkg") or not project_file:
        return source
    root = os.path.dirname(os.path.abspath(project_file))
    original = path if os.path.isabs(path) else os.path.normpath(os.path.join(root, path))
    if os.path.isfile(original):
        return source
    name = os.path.basename(original)
    folders = (folder,) if folder else tuple(FOLDERS.values())
    candidates = set()
    layer_table = next((part.partition("=")[2] for part in suffix.split("|")
                        if part.startswith("layername=")), "")
    for candidate_folder in (*folders, ""):
        candidate = os.path.join(root, candidate_folder, name)
        tables = table_names(candidate)
        expected = REGISTRIES.get(folder or candidate_folder)
        if "gpkg_contents" not in tables or (expected and expected not in tables):
            continue
        if layer_table and layer_table not in tables:
            continue
        if not folder and not any(table in tables for table in REGISTRIES.values()) \
                and not any(table == "cable_lay" or table.endswith("_cable_lay") for table in tables):
            continue
        candidates.add(candidate)
    if len(candidates) != 1:
        return source
    return candidates.pop() + (sep + suffix if sep else "")


def read_project_path(project, key, folder):
    path = stored_project_path(project, key)
    return relocated_path(project.fileName(), path, folder) if path else None


def write_project_path(project, key, path):
    project.writeEntry(SCOPE, key, project.writePath(os.path.abspath(path)) if path else "")


def resolve_source(project, source):
    """Resolve captured input URIs through project repairs without changing stored history."""
    path, sep, suffix = str(source or "").partition("|")
    if not path.lower().endswith(".gpkg"):
        return source
    value, _ok = project.readEntry(SCOPE, RELOCATIONS_KEY, "{}")
    try:
        mappings = json.loads(value)
    except (TypeError, ValueError):
        mappings = {}
    current_path = absolute_source(project, path)
    visited = set()
    while isinstance(mappings, dict) and _norm(current_path) not in visited:
        visited.add(_norm(current_path))
        current = next((value for previous, value in mappings.items()
                        if _norm(previous) == _norm(current_path) and isinstance(value, str)), None)
        if not current:
            break
        current_path = relocated_path(project.fileName(), absolute_source(project, current))
        if os.path.isfile(current_path):
            return current_path + (sep + suffix if sep else "")
    return relocated_path(project.fileName(), source)


def repair_project_layers(project, old_path, new_path):
    """Repair missing sources in place after Open existing, preserving IDs and styles.

    Valid layers in another GeoPackage are left alone. Only tables present in
    the selected file are repaired. Returns the repaired layer count.
    """
    from qgis.core import QgsMapLayerStyle

    tables = table_names(new_path)
    repaired = 0
    value, _ok = project.readEntry(SCOPE, RELOCATIONS_KEY, "{}")
    try:
        mappings = json.loads(value)
    except (TypeError, ValueError):
        mappings = {}
    if not isinstance(mappings, dict):
        mappings = {}
    if old_path and _norm(old_path) != _norm(new_path) and not os.path.isfile(old_path):
        mappings[os.path.abspath(old_path)] = project.writePath(os.path.abspath(new_path))
    layers = list(project.mapLayers().values())
    # The project entry can predate a previous path repair. If its layers
    # name one missing file with the old filename, use that as an alias too.
    aliases = {absolute_source(project, layer.source().split("|", 1)[0]) for layer in layers
               if old_path and layer.providerType() == "ogr" and not layer.isValid()
               and os.path.basename(layer.source().split("|", 1)[0]) == os.path.basename(old_path)
               and not os.path.isfile(absolute_source(project, layer.source().split("|", 1)[0]))}
    alias = next(iter(aliases)) if len(aliases) == 1 else None
    for layer in layers:
        if layer.providerType() != "ogr" or layer.isValid() or layer.isEditable():
            continue
        source = layer.source()
        path, sep, options = source.partition("|")
        table = next((part.partition("=")[2] for part in options.split("|")
                      if part.startswith("layername=")), "")
        actual_path = absolute_source(project, path)
        matches_old = ((old_path and _norm(actual_path) == _norm(old_path))
                       or (alias and _norm(actual_path) == _norm(alias)))
        matches_name = ((not old_path or _norm(old_path) == _norm(new_path))
                        and os.path.basename(path) == os.path.basename(new_path)
                        and not os.path.isfile(absolute_source(project, path)))
        if not sep or table not in tables or not (matches_old or matches_name):
            continue
        style = QgsMapLayerStyle()
        style.readFromLayer(layer)
        try:
            layer.setDataSource(new_path + "|" + options, layer.name(), "ogr")
            style.writeToLayer(layer)
        except (RuntimeError, TypeError):
            log_warning(f"Could not relink '{layer.name()}' to {new_path}.")
        if layer.isValid():
            repaired += 1
            mappings[os.path.abspath(absolute_source(project, path))] = project.writePath(os.path.abspath(new_path))
        else:
            layer.setDataSource(source, layer.name(), "ogr")
            style.writeToLayer(layer)
            log_warning(f"Could not relink '{layer.name()}' to {new_path}; its original source was retained.")
    if mappings:
        project.writeEntry(SCOPE, RELOCATIONS_KEY, json.dumps(mappings))
    return repaired


def install_path_preprocessor():
    """Resolve conventional manual moves before QGIS reports unavailable layers."""
    from qgis.core import QgsPathResolver, QgsProject

    return QgsPathResolver.setPathPreprocessor(
        lambda source: relocated_path(QgsProject.instance().fileName(), source))


def choose_new_gpkg(parent, title, suggested):
    """Ask once per new file, with a useful location and no overwrite path."""
    from qgis.PyQt.QtWidgets import QFileDialog, QMessageBox

    folder = os.path.dirname(os.path.abspath(suggested))
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass  # The user can select a writable folder in the dialog.
    path, _selected = QFileDialog.getSaveFileName(
        parent, title, suggested, "GeoPackage (*.gpkg)", options=QFileDialog.Option.DontConfirmOverwrite)
    if not path:
        return None
    if not path.lower().endswith(".gpkg"):
        path += ".gpkg"
    if os.path.exists(path):
        QMessageBox.information(parent, title, "That file already exists. Use Open existing to select it, "
                                "or choose a new filename.")
        return None
    return path
