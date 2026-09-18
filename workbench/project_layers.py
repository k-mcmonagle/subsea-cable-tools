# -*- coding: utf-8 -*-
"""Project-side management of Cable Route Workbench layers.

One place that knows how to find a workbench layer already in the project
(robust to Windows path case / separator differences), repair one whose
source went stale, add one to the right layer-tree group with the standard
style, and restore the registered layers when a project is (re)opened.

The layer tree mirrors the workbench tree::

    Cable Route Workbench
      <cable system>
        <cable segment>
          <cable segment> · <RPL revision>
            Sys A · Seg 1 · Rev 2 · Lines
            Sys A · Seg 1 · Rev 2 · Points

so a layer name identifies the revision it belongs to even away from its
group (in a processing dialog's layer picker, say), and a system, segment or
revision can be switched on and off as a unit. The revision group carries
the segment name too ("Seg 1 · Rev 2" rather than a bare "Rev 2"), so it
reads like the layers under it when several segments are expanded. Assessment and assembly-fit
outputs join the revision they were produced from.

Placement is applied when a layer is added, and
:func:`organise_workbench_layers` brings an existing project in line after a
rename or a change of system. It only moves layers that are still inside the
workbench group, so a layer deliberately dragged elsewhere stays there.

The restore entry point (:func:`restore_workbench_layers`) is deliberately
cheap when the project has no workbench GeoPackage, so the plugin can call
it from ``iface.projectRead`` unconditionally.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence, Tuple

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
UNASSIGNED_SYSTEM = "Unassigned system"
UNASSIGNED_SEGMENT = "Unassigned segment"
UNLABELLED_REVISION = "Unlabelled revision"
NAME_SEPARATOR = " \u00b7 "


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


# -- naming / grouping -------------------------------------------------------
class LayerPlacement(object):
    """Where one workbench layer belongs: its display name and group path."""

    __slots__ = ("display_name", "group_path")

    def __init__(self, display_name: str, group_path: Sequence[str]):
        self.display_name = display_name
        self.group_path = tuple(group_path)

    def __eq__(self, other):
        return isinstance(other, LayerPlacement) \
            and other.display_name == self.display_name \
            and other.group_path == self.group_path

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"LayerPlacement({self.display_name!r}, {self.group_path!r})"


def _clean(value, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def compose_layer_name(parts: Sequence[str]) -> str:
    """Join the non-empty name parts with the standard separator."""
    return NAME_SEPARATOR.join(part for part in (str(p or "").strip() for p in parts) if part)


def build_placements(store) -> Dict[str, LayerPlacement]:
    """Map every registered layer name to where it belongs in the layer tree.

    Reads the registry once, so callers adding several layers (an import, a
    project restore) can build it a single time.
    """
    placements: Dict[str, LayerPlacement] = {}
    if store is None:
        return placements
    try:
        systems = {row.get("system_id"): _clean(row.get("name"), UNASSIGNED_SYSTEM)
                   for row in store.list_systems()}
        routes = {row.get("route_id"): row for row in store.list_routes()}
        rpls = store.list_rpls()
    except Exception:
        return placements

    rpl_context: Dict[str, Tuple[str, Tuple[str, ...]]] = {}
    for rpl in rpls:
        route = routes.get(rpl.get("route_id") or "")
        segment = _clean((route or {}).get("name"),
                         _clean(rpl.get("name"), UNASSIGNED_SEGMENT))
        system = systems.get((route or {}).get("system_id") or "") or UNASSIGNED_SYSTEM
        revision = _clean(rpl.get("rev_label"),
                          _clean(rpl.get("name"), UNLABELLED_REVISION))
        prefix = (system, segment, revision)
        # The revision group is named like its layers (segment · revision):
        # a bare "Rev 2" is ambiguous once two segments are expanded.
        group = (WORKBENCH_GROUP, system, segment,
                 compose_layer_name((segment, revision)))
        rpl_id = str(rpl.get("rpl_id") or "")
        if rpl_id:
            rpl_context[rpl_id] = (compose_layer_name(prefix), group)
        for key, suffix in (("lines_layer", "Lines"), ("points_layer", "Points")):
            name = rpl.get(key)
            if name:
                placements[str(name)] = LayerPlacement(
                    compose_layer_name(prefix + (suffix,)), group)

    # Assessment and fit outputs belong with the revision they came from.
    try:
        assessments = store.list_assessments()
    except Exception:
        assessments = []
    for row in assessments:
        name = row.get("ranges_layer")
        context = rpl_context.get(str(row.get("rpl_id") or ""))
        if name and context:
            prefix, group = context
            placements[str(name)] = LayerPlacement(
                compose_layer_name((prefix, "Assessment: " + _clean(row.get("name"), "unnamed"))),
                group)

    try:
        fits = store.list_fits()
        assemblies = {row.get("assembly_id"): row for row in store.list_assemblies()}
        rpl_names = {str(row.get("rpl_id") or ""): _clean(row.get("name")) for row in rpls}
    except Exception:
        return placements
    for fit in fits:
        context = rpl_context.get(str(fit.get("rpl_id") or ""))
        assembly = assemblies.get(fit.get("assembly_id") or "")
        if not context or not assembly:
            continue
        prefix, group = context
        # Same name the dock derives when it looks these layers up.
        fit_name = f"{assembly.get('name')}_{rpl_names.get(str(fit.get('rpl_id') or ''), '')}"
        label = _clean(assembly.get("name"), "assembly")
        for layer_name, suffix in (
                (schema.fit_bodies_layer_name(fit_name), "bodies"),
                (schema.fit_sections_layer_name(fit_name), "sections")):
            placements[layer_name] = LayerPlacement(
                compose_layer_name((prefix, f"Fit: {label} ({suffix})")), group)
    return placements


def ensure_group_path(project: Optional[QgsProject], path: Sequence[str]):
    """Find or create a nested layer-tree group, returning the deepest one."""
    project = project or QgsProject.instance()
    node = project.layerTreeRoot()
    first = True
    for name in path:
        name = str(name or "").strip()
        if not name:
            continue
        child = node.findGroup(name)
        if child is None:
            # The workbench group goes to the top of the tree; nested groups
            # keep insertion order so revisions read oldest-added first.
            child = node.insertGroup(0, name) if first else node.addGroup(name)
        node = child
        first = False
    return node


def move_layer_to_group(project: Optional[QgsProject], layer, group) -> bool:
    """Move ``layer``'s tree node under ``group``. True when it moved."""
    project = project or QgsProject.instance()
    root = project.layerTreeRoot()
    node = root.findLayer(layer.id())
    if node is None:
        group.addLayer(layer)
        return True
    if node.parent() is group:
        return False
    # Cloning keeps the checked state and custom properties; inserting the
    # clone before dropping the original means the layer is never orphaned.
    clone = node.clone()
    group.insertChildNode(-1, clone)
    parent = node.parent()
    if parent is not None:
        parent.removeChildNode(node)
    return True


def prune_empty_groups(project: Optional[QgsProject] = None) -> int:
    """Remove empty groups left inside the workbench group. Returns the count."""
    project = project or QgsProject.instance()
    group = workbench_group(project, create=False)
    if group is None:
        return 0
    removed = 0
    changed = True
    while changed:
        changed = False
        for child in list(group.findGroups(recursive=True)):
            if not child.children():
                parent = child.parent()
                if parent is not None:
                    parent.removeChildNode(child)
                    removed += 1
                    changed = True
    return removed


def organise_workbench_layers(project: Optional[QgsProject] = None,
                              store=None, gpkg_path: str = "") -> int:
    """Rename and regroup the project's workbench layers. Returns the count.

    Layers the user moved out of the workbench group are renamed but left
    where they are; everything inside it is filed under
    system / segment / revision. Never raises.
    """
    try:
        project = project or QgsProject.instance()
        if store is None:
            return 0
        gpkg_path = gpkg_path or getattr(store, "gpkg_path", "") or ""
        if not gpkg_path:
            return 0
        placements = build_placements(store)
        if not placements:
            return 0
        root_group = workbench_group(project, create=False)
        touched = 0
        for layer in list(project.mapLayers().values()):
            if not isinstance(layer, QgsVectorLayer):
                continue
            name = layer_name_from_source(layer.source(), gpkg_path)
            placement = placements.get(name) if name else None
            if placement is None:
                continue
            changed = False
            if layer.name() != placement.display_name:
                layer.setName(placement.display_name)
                changed = True
            if root_group is not None and _inside(root_group, project, layer):
                group = ensure_group_path(project, placement.group_path)
                changed = move_layer_to_group(project, layer, group) or changed
            if changed:
                touched += 1
        if touched:
            prune_empty_groups(project)
        return touched
    except Exception:
        return 0


def _inside(group, project: QgsProject, layer) -> bool:
    """True when ``layer``'s tree node is somewhere under ``group``."""
    node = project.layerTreeRoot().findLayer(layer.id())
    while node is not None:
        node = node.parent()
        if node is group:
            return True
    return False


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
    store=None,
    placements: Optional[Dict[str, LayerPlacement]] = None,
) -> Optional[QgsVectorLayer]:
    """Find, repair, or load-and-add one workbench layer.

    Newly added (and freshly repaired) layers get the standard RPL style;
    layers already present and valid are left exactly as the user styled
    them. With a ``store`` (or a prebuilt ``placements`` map) a new layer is
    named after its system / segment / revision and filed in the matching
    group; without one it keeps the old flat behaviour. Returns None when the
    gpkg table cannot be opened.
    """
    if not layer_name or not gpkg_path:
        return None
    project = project or QgsProject.instance()
    if placements is None and store is not None:
        placements = build_placements(store)
    placement = (placements or {}).get(layer_name)

    existing = find_layer(project, gpkg_path, layer_name)
    if existing is not None:
        if not existing.isValid():
            if repair_layer(existing, gpkg_path, layer_name) and apply_style:
                layer_style.style_rpl_layer(existing, layer_name)
        return existing if existing.isValid() else None

    display_name = placement.display_name if placement else layer_name
    layer = QgsVectorLayer(gpkg_layer_uri(gpkg_path, layer_name), display_name, "ogr")
    if not layer.isValid():
        return None
    group = (ensure_group_path(project, placement.group_path) if placement
             else workbench_group(project))
    project.addMapLayer(layer, False)
    group.addLayer(layer)
    if apply_style:
        layer_style.style_rpl_layer(layer, layer_name)
    return layer


def ensure_rpl_layers(project: Optional[QgsProject], gpkg_path: str, rpl_row: Dict,
                      store=None, placements: Optional[Dict[str, LayerPlacement]] = None):
    """Ensure one RPL revision's line + point layers are in the project.

    Lines first so points draw on top of them in the group.
    Returns (points_layer, lines_layer); either may be None.
    """
    if placements is None and store is not None:
        placements = build_placements(store)
    lines = ensure_layer(project, gpkg_path, rpl_row.get("lines_layer"),
                         placements=placements)
    points = ensure_layer(project, gpkg_path, rpl_row.get("points_layer"),
                          placements=placements)
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
        placements = build_placements(store)

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
                points, lines = ensure_rpl_layers(project, gpkg_path, rpl,
                                                  placements=placements)
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
                points, lines = ensure_rpl_layers(project, gpkg_path, rpl,
                                                  placements=placements)
                touched += sum(1 for layer in (points, lines) if layer is not None)

        # Projects made before the grouped layout (or edited since a rename)
        # get filed correctly on the way in.
        organise_workbench_layers(project, store, gpkg_path)

        if touched:
            set_project_gpkg_path(gpkg_path, project)
        return touched
    except Exception:
        return 0
