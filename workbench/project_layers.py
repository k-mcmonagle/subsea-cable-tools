# -*- coding: utf-8 -*-
"""Project-side management of Cable Route Workbench layers.

One place that knows how to find a workbench layer already in the project
(robust to Windows path case / separator differences), repair one whose
source went stale, add one to the "Cable Route Workbench" layer-tree group
with the standard style, and restore the registered layers when a project
is (re)opened.

The restore entry point (:func:`restore_workbench_layers`) is deliberately
cheap when the project has no workbench GeoPackage, so the plugin can call
it from ``iface.projectRead`` unconditionally.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from qgis.core import QgsProject, QgsVectorLayer

from ..processing.cable_lay_parsers import gpkg_layer_uri
from . import layer_style, schema
from .store import (
    WorkbenchStore,
    default_project_gpkg_path,
    project_gpkg_path,
    set_project_gpkg_path,
)

WORKBENCH_GROUP = "Cable Route Workbench"


# -- source parsing ----------------------------------------------------------
def normalised_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path or "")))


def layer_name_from_source(source: str, gpkg_path: str) -> Optional[str]:
    """The gpkg layer name if ``source`` points into ``gpkg_path``, else None."""
    if not source or not gpkg_path:
        return None
    parts = str(source).split("|")
    if normalised_path(parts[0]) != normalised_path(gpkg_path):
        return None
    for part in parts[1:]:
        key, sep, value = part.partition("=")
        if sep and key.lower() == "layername":
            return value
    return None


# -- lookup / add / repair ---------------------------------------------------
def workbench_group(project: Optional[QgsProject] = None, create: bool = True):
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    group = root.findGroup(WORKBENCH_GROUP)
    if group is None and create:
        group = root.insertGroup(0, WORKBENCH_GROUP)
    return group


def find_layer(project: QgsProject, gpkg_path: str, layer_name: str) -> Optional[QgsVectorLayer]:
    """A project layer whose source is ``layer_name`` inside ``gpkg_path``."""
    if not layer_name:
        return None
    for layer in project.mapLayers().values():
        if isinstance(layer, QgsVectorLayer) \
                and layer_name_from_source(layer.source(), gpkg_path) == layer_name:
            return layer
    return None


def repair_layer(layer: QgsVectorLayer, gpkg_path: str, layer_name: str) -> bool:
    """Point a broken project layer back at its gpkg table. True on success."""
    uri = gpkg_layer_uri(gpkg_path, layer_name)
    base_name = layer.name() or layer_name
    try:
        from qgis.core import QgsDataProvider

        layer.setDataSource(uri, base_name, "ogr", QgsDataProvider.ProviderOptions())
    except (ImportError, TypeError, AttributeError):
        try:
            layer.setDataSource(uri, base_name, "ogr")
        except TypeError:
            return False
    return layer.isValid()


def ensure_layer(
    project: Optional[QgsProject],
    gpkg_path: str,
    layer_name: Optional[str],
    apply_style: bool = True,
) -> Optional[QgsVectorLayer]:
    """Find, repair, or load-and-add one workbench layer.

    Newly added (and freshly repaired) layers get the standard RPL style;
    layers already present and valid are left exactly as the user styled
    them. Returns None when the gpkg table cannot be opened.
    """
    if not layer_name or not gpkg_path:
        return None
    project = project or QgsProject.instance()

    existing = find_layer(project, gpkg_path, layer_name)
    if existing is not None:
        if not existing.isValid():
            if repair_layer(existing, gpkg_path, layer_name) and apply_style:
                layer_style.style_rpl_layer(existing, layer_name)
        return existing if existing.isValid() else None

    layer = QgsVectorLayer(gpkg_layer_uri(gpkg_path, layer_name), layer_name, "ogr")
    if not layer.isValid():
        return None
    group = workbench_group(project)
    project.addMapLayer(layer, False)
    group.addLayer(layer)
    if apply_style:
        layer_style.style_rpl_layer(layer, layer_name)
    return layer


def ensure_rpl_layers(project: Optional[QgsProject], gpkg_path: str, rpl_row: Dict):
    """Ensure one RPL revision's line + point layers are in the project.

    Lines first so points draw on top of them in the group.
    Returns (points_layer, lines_layer); either may be None.
    """
    lines = ensure_layer(project, gpkg_path, rpl_row.get("lines_layer"))
    points = ensure_layer(project, gpkg_path, rpl_row.get("points_layer"))
    return points, lines


# -- project-open restore ----------------------------------------------------
def discover_gpkg_path(project: Optional[QgsProject] = None) -> Optional[str]:
    """Find the project's existing Workbench registry without creating one.

    Besides the saved entry and conventional filename, recover a registry
    moved with the QGIS project.  If there is exactly one valid Workbench
    GeoPackage beside the project we can select it unambiguously; if there are
    several, leave the choice to the Workbench's *Open existing* action.
    """
    project = project or QgsProject.instance()
    path = project_gpkg_path(project)
    if _is_workbench_gpkg(path):
        return path

    project_file = project.fileName() or ""
    project_folder = os.path.dirname(os.path.abspath(project_file)) if project_file else ""

    # Absolute project entries commonly go stale when a project folder is
    # copied to another computer.  First try the same basename beside the
    # newly opened project, which remains deterministic even if the folder
    # contains several registries.
    if path and project_folder:
        relocated = os.path.join(project_folder, os.path.basename(path))
        if _is_workbench_gpkg(relocated):
            return relocated

    fallback = default_project_gpkg_path(project)
    if _is_workbench_gpkg(fallback):
        return fallback

    if project_folder and os.path.isdir(project_folder):
        candidates = []
        try:
            names = os.listdir(project_folder)
        except OSError:
            names = []
        for name in names:
            lowered = name.lower()
            if not lowered.endswith(".gpkg") or ".bak.gpkg" in lowered:
                continue
            candidate = os.path.join(project_folder, name)
            if _is_workbench_gpkg(candidate):
                candidates.append(candidate)
        if len(candidates) == 1:
            return candidates[0]
    return None


def _is_workbench_gpkg(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        return WorkbenchStore(path).exists()
    except Exception:
        return False


def restore_workbench_layers(project: Optional[QgsProject] = None) -> int:
    """Repair and re-add registered workbench layers after a project opens.

    - Repairs any workbench layer already in the project whose source is
      broken (moved gpkg, stale relative path, ...).
    - Completes half-present RPLs (one of the pair missing).
    - If the project has *no* workbench layers at all but the registry does,
      adds the latest revision of each route back, so an imported RPL never
      silently disappears from the workspace.

    Returns the number of layers added or repaired. Never raises.
    """
    try:
        project = project or QgsProject.instance()
        gpkg_path = discover_gpkg_path(project)
        if not gpkg_path:
            return 0
        store = WorkbenchStore(gpkg_path)
        if not store.exists():
            return 0

        touched = 0

        # Pass 1: repair broken workbench layers already in the project.
        present_names = set()
        for layer in list(project.mapLayers().values()):
            if not isinstance(layer, QgsVectorLayer):
                continue
            name = layer_name_from_source(layer.source(), gpkg_path)
            if not name:
                continue
            present_names.add(name)
            if not layer.isValid() and repair_layer(layer, gpkg_path, name):
                layer_style.style_rpl_layer(layer, name)
                touched += 1

        rpls = store.list_rpls()

        # Pass 2: complete RPLs that are only half present.
        for rpl in rpls:
            names = {rpl.get("points_layer"), rpl.get("lines_layer")} - {None, ""}
            if names and names & present_names and not names <= present_names:
                points, lines = ensure_rpl_layers(project, gpkg_path, rpl)
                touched += sum(
                    1 for layer in (points, lines)
                    if layer is not None and layer.name() not in present_names
                )

        # Pass 3: nothing on the map but RPLs registered -> bring back the
        # latest revision of each route.
        if not present_names and rpls:
            latest: List[Dict] = []
            for route in store.list_routes():
                row = store.latest_revision(route.get("route_id") or "")
                if row:
                    latest.append(row)
            routed_ids = {r.get("rpl_id") for r in latest}
            latest.extend(
                r for r in rpls if not r.get("route_id") and r.get("rpl_id") not in routed_ids
            )
            for rpl in latest:
                points, lines = ensure_rpl_layers(project, gpkg_path, rpl)
                touched += sum(1 for layer in (points, lines) if layer is not None)

        if touched:
            set_project_gpkg_path(gpkg_path, project)
        return touched
    except Exception:
        return 0
