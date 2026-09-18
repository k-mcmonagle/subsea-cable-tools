# -*- coding: utf-8 -*-
"""Checks for workbench project-layer management and standard styling.

Covers source-URI matching (Windows case/slash robustness), ensure_layer
add/dedupe, the system / segment / revision naming and grouping, the
automatic CableType/Event symbology and its preservation of a user's own
style, project-open restore (restore_workbench_layers), and the
project-teardown guard that protects the registry when QGIS clears all
layers.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile

from qgis.core import QgsProject

from ..qgis_compat import WKB_LINESTRING, WKB_POINT
from ..workbench import layer_style, project_layers, schema
from ..workbench.store import WorkbenchStore, project_gpkg_path, set_project_gpkg_path
from ..processing.cable_lay_parsers import WKT_KEY


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


POINT_SPECS = [("rpl_id", "str"), ("SeqNo", "int"), ("Event", "str")]
LINE_SPECS = [("rpl_id", "str"), ("SeqNo", "int"), ("CableType", "str")]


def _store_with_rpl():
    folder = tempfile.mkdtemp(prefix="wb_projlayers_test_")
    store = WorkbenchStore(os.path.join(folder, "workbench.gpkg"))
    store.ensure_created()
    rpl_id = schema.new_id()
    points_layer = schema.rpl_points_layer_name("Test Route")
    lines_layer = schema.rpl_lines_layer_name("Test Route")
    store.write_spatial_layer(points_layer, POINT_SPECS, WKB_POINT, [
        {"rpl_id": rpl_id, "SeqNo": 0, "Event": "BMH", WKT_KEY: "POINT (0 0)"},
        {"rpl_id": rpl_id, "SeqNo": 1, "Event": None, WKT_KEY: "POINT (0.1 0)"},
        {"rpl_id": rpl_id, "SeqNo": 2, "Event": "JOINT", WKT_KEY: "POINT (0.2 0)"},
    ])
    store.write_spatial_layer(lines_layer, LINE_SPECS, WKB_LINESTRING, [
        {"rpl_id": rpl_id, "SeqNo": 0, "CableType": "DA", WKT_KEY: "LINESTRING (0 0, 0.1 0)"},
        {"rpl_id": rpl_id, "SeqNo": 1, "CableType": "LW", WKT_KEY: "LINESTRING (0.1 0, 0.2 0)"},
    ])
    store.save_rpl({
        "rpl_id": rpl_id,
        "name": "Test Route Rev 1",
        "kind": "rpl",
        "points_layer": points_layer,
        "lines_layer": lines_layer,
    })
    return store, store.get_rpl(rpl_id)


def test_layer_name_from_source() -> bool:
    gpkg = r"C:\Data\Proj\proj_workbench.gpkg"
    same = "c:/data/proj/PROJ_workbench.gpkg|layername=rpl_x_lines"
    other = r"C:\Data\Other\other.gpkg|layername=rpl_x_lines"
    ok = project_layers.layer_name_from_source(same, gpkg) == "rpl_x_lines" \
        if os.name == "nt" else True  # case-folding only guaranteed on Windows
    exact = gpkg + "|layername=rpl_x_points"
    ok = ok and project_layers.layer_name_from_source(exact, gpkg) == "rpl_x_points"
    ok = ok and project_layers.layer_name_from_source(other, gpkg) is None
    ok = ok and project_layers.layer_name_from_source("", gpkg) is None
    return _result("layer_name_from_source path robustness", ok)


def test_ensure_layer_add_style_dedupe() -> bool:
    project = QgsProject.instance()
    project.clear()
    store, rpl = _store_with_rpl()

    lines = project_layers.ensure_layer(project, store.gpkg_path, rpl["lines_layer"])
    points = project_layers.ensure_layer(project, store.gpkg_path, rpl["points_layer"])
    ok = lines is not None and points is not None

    from qgis.core import QgsCategorizedSymbolRenderer, QgsRuleBasedRenderer

    renderer = lines.renderer() if lines else None
    ok = ok and isinstance(renderer, QgsCategorizedSymbolRenderer)
    ok = ok and renderer.classAttribute() == layer_style.CABLE_TYPE_FIELD
    if ok:
        colours = {
            str(c.value()): c.symbol().color().name().lower()
            for c in renderer.categories()
        }
        ok = colours.get("DA") == layer_style.KNOWN_CABLE_TYPE_COLOURS["DA"]
        ok = ok and colours.get("LW") == layer_style.KNOWN_CABLE_TYPE_COLOURS["LW"]
    ok = ok and isinstance(points.renderer(), QgsRuleBasedRenderer)

    # group membership + dedupe
    group = project.layerTreeRoot().findGroup(project_layers.WORKBENCH_GROUP)
    ok = ok and group is not None and len(group.findLayers()) == 2
    again = project_layers.ensure_layer(project, store.gpkg_path, rpl["lines_layer"])
    ok = ok and again is not None and again.id() == lines.id()
    ok = ok and len(project.mapLayers()) == 2
    return _result("ensure_layer adds, styles, dedupes", ok)


def test_restore_after_reopen() -> bool:
    project = QgsProject.instance()
    project.clear()
    store, rpl = _store_with_rpl()
    # Simulate "project reopened without the layers": entry present, no layers.
    set_project_gpkg_path(store.gpkg_path, project)
    touched = project_layers.restore_workbench_layers(project)
    names = {
        project_layers.layer_name_from_source(layer.source(), store.gpkg_path)
        for layer in project.mapLayers().values()
    }
    ok = touched == 2
    ok = ok and names == {rpl["points_layer"], rpl["lines_layer"]}
    # Second run is a no-op.
    ok = ok and project_layers.restore_workbench_layers(project) == 0
    return _result("restore_workbench_layers after reopen", ok, f"touched={touched}")


def test_restore_completes_half_present_rpl() -> bool:
    project = QgsProject.instance()
    project.clear()
    store, rpl = _store_with_rpl()
    set_project_gpkg_path(store.gpkg_path, project)
    # Only the lines layer present (e.g. user saved a partial project).
    project_layers.ensure_layer(project, store.gpkg_path, rpl["lines_layer"])
    touched = project_layers.restore_workbench_layers(project)
    ok = touched >= 1 and len(project.mapLayers()) == 2
    return _result("restore completes half-present RPL", ok)


def test_discovers_unique_custom_named_registry() -> bool:
    project = QgsProject.instance()
    project.clear()
    folder = tempfile.mkdtemp(prefix="wb_discovery_test_")
    project.setFileName(os.path.join(folder, "route_project.qgz"))
    store = WorkbenchStore(os.path.join(folder, "client_route_registry.gpkg"))
    store.ensure_created()
    # Simulate an absolute entry left behind after moving from another PC.
    set_project_gpkg_path(os.path.join(folder, "missing", "old_workbench.gpkg"), project)

    found = project_layers.discover_gpkg_path(project)
    ok = os.path.normcase(os.path.abspath(found or "")) == os.path.normcase(
        os.path.abspath(store.gpkg_path)
    )
    project.clear()
    return _result("discover unique custom-named Workbench", ok, found or "not found")


def test_discovery_does_not_guess_between_registries() -> bool:
    project = QgsProject.instance()
    project.clear()
    folder = tempfile.mkdtemp(prefix="wb_discovery_ambiguous_test_")
    project.setFileName(os.path.join(folder, "route_project.qgz"))
    WorkbenchStore(os.path.join(folder, "route_a.gpkg")).ensure_created()
    WorkbenchStore(os.path.join(folder, "route_b.gpkg")).ensure_created()
    set_project_gpkg_path(os.path.join(folder, "missing.gpkg"), project)

    found = project_layers.discover_gpkg_path(project)
    ok = found is None
    project.clear()
    return _result("ambiguous Workbench discovery requires user choice", ok, str(found))


def test_open_validation_does_not_modify_unrelated_gpkg() -> bool:
    from ..workbench.workbench_dock import WorkbenchDock

    folder = tempfile.mkdtemp(prefix="wb_open_validation_test_")
    path = os.path.join(folder, "ordinary.gpkg")
    # A missing path is equally important: validation must not create it.
    try:
        WorkbenchDock._prepare_workbench(path)
        rejected = False
    except ValueError:
        rejected = True
    ok = rejected and not os.path.exists(path)
    return _result("Open Workbench validates without modifying selection", ok)


def test_workbench_dock_shows_and_switches_registry() -> bool:
    from ..workbench.workbench_dock import WorkbenchDock

    project = QgsProject.instance()
    project.clear()
    first, first_rpl = _store_with_rpl()
    assembly_id = schema.new_id()
    first.save_assembly({
        "assembly_id": assembly_id, "name": "Load 01", "kind": "cable",
        "total_cable_len_m": 12000.0,
    }, [{"kind": "section", "name": "LW", "length_m": 12000.0,
         "cable_type": "LW"}])
    first.add_makeup_assembly(first_rpl.get("route_id") or "", assembly_id)
    second_folder = tempfile.mkdtemp(prefix="wb_switch_test_")
    second = WorkbenchStore(os.path.join(second_folder, "second_registry.gpkg"))
    second.ensure_created()
    set_project_gpkg_path(first.gpkg_path, project)

    dock = WorkbenchDock(None)
    ok = os.path.basename(first.gpkg_path) in dock.store_label.text()
    ok = ok and "1 RPL" in dock.store_label.text()
    ok = ok and dock.rpl_panel.store is dock.assembly_panel.store
    ok = ok and dock.rpl_panel.store is dock.assessment_panel.store
    ok = ok and dock.assembly_panel.sld is not None
    ok = ok and [dock.assembly_panel.views.tabText(i)
                 for i in range(dock.assembly_panel.views.count())] == ["Table", "Schematic"]
    tree_text = []

    def collect(item):
        tree_text.append(item.text(0))
        for child_index in range(item.childCount()):
            collect(item.child(child_index))

    for top_index in range(dock.tree.topLevelItemCount()):
        collect(dock.tree.topLevelItem(top_index))
    ok = ok and "Assembly Library" not in tree_text
    ok = ok and "Assembly" in tree_text and "Load 01" in tree_text
    ok = ok and dock._activate_workbench(second.gpkg_path)
    ok = ok and project_gpkg_path(project) == os.path.abspath(second.gpkg_path)
    ok = ok and dock.rpl_panel.store is dock.assembly_panel.store
    ok = ok and dock.rpl_panel.store is dock.assessment_panel.store
    ok = ok and os.path.basename(second.gpkg_path) in dock.store_label.text()
    ok = ok and "0 RPLs" in dock.store_label.text()
    dock.shutdown()
    dock.deleteLater()
    project.clear()
    return _result("Workbench dock displays and switches active registry", ok)


def test_teardown_guard() -> bool:
    from ..workbench.workbench_dock import WorkbenchDock

    project = QgsProject.instance()
    project.clear()
    store, rpl = _store_with_rpl()
    project_layers.ensure_layer(project, store.gpkg_path, rpl["lines_layer"])
    project_layers.ensure_layer(project, store.gpkg_path, rpl["points_layer"])

    all_ids = list(project.mapLayers().keys())
    ok = WorkbenchDock._is_project_teardown((all_ids,))
    ok = ok and not WorkbenchDock._is_project_teardown((all_ids[:1],))
    project.clear()
    return _result("project teardown heuristic", ok)


def test_sync_survives_deleted_layers() -> bool:
    """RplLayerSync must not raise once its project layers are deleted.

    Removing the layers destroys the wrapped C++ objects; the sync's queries
    must degrade (not dirty, nothing to undo) instead of raising
    'wrapped C/C++ object ... has been deleted'.
    """
    from ..workbench.rpl_layer_io import RplLayerSync

    project = QgsProject.instance()
    project.clear()
    store, rpl = _store_with_rpl()
    lines = project_layers.ensure_layer(project, store.gpkg_path, rpl["lines_layer"])
    points = project_layers.ensure_layer(project, store.gpkg_path, rpl["points_layer"])
    sync = RplLayerSync(points, lines, rpl["rpl_id"])
    sync.begin_session()
    ok = sync.is_valid()

    project.removeMapLayers([points.id(), lines.id()])
    try:
        ok = ok and not sync.is_valid()
        ok = ok and not sync.is_dirty()
        ok = ok and not sync.can_undo() and not sync.can_redo()
        sync.undo()
        sync.redo()
        sync.rollback()
        sync.commit()
        sync.begin_session()
    except RuntimeError as exc:
        return _result("sync survives deleted layers", False, str(exc))
    project.clear()
    return _result("sync survives deleted layers", ok)


def _store_with_system_and_route():
    """A registry with one system, one segment and two RPL revisions."""
    folder = tempfile.mkdtemp(prefix="wb_grouping_test_")
    store = WorkbenchStore(os.path.join(folder, "workbench.gpkg"))
    store.ensure_created()
    system_id = store.create_system("Atlantic Link")
    route_id = store.create_route("Segment 1", system_id=system_id)
    rpls = []
    for index, label in enumerate(("Rev 1", "Rev 2")):
        rpl_id = schema.new_id()
        points_layer = schema.rpl_points_layer_name(f"Segment 1 {label}")
        lines_layer = schema.rpl_lines_layer_name(f"Segment 1 {label}")
        store.write_spatial_layer(points_layer, POINT_SPECS, WKB_POINT, [
            {"rpl_id": rpl_id, "SeqNo": 0, "Event": "BMH", WKT_KEY: "POINT (0 0)"},
            {"rpl_id": rpl_id, "SeqNo": 1, "Event": "BU-1",
             WKT_KEY: f"POINT (0.{index + 1} 0)"},
        ])
        store.write_spatial_layer(lines_layer, LINE_SPECS, WKB_LINESTRING, [
            {"rpl_id": rpl_id, "SeqNo": 0, "CableType": "DA",
             WKT_KEY: f"LINESTRING (0 0, 0.{index + 1} 0)"},
        ])
        store.save_rpl({
            "rpl_id": rpl_id, "name": f"Segment 1 {label}", "kind": "rpl",
            "points_layer": points_layer, "lines_layer": lines_layer,
            "route_id": route_id, "rev_label": label,
        })
        rpls.append(store.get_rpl(rpl_id))
    return store, route_id, rpls


def test_layers_are_named_and_grouped() -> bool:
    project = QgsProject.instance()
    project.clear()
    store, _route_id, rpls = _store_with_system_and_route()

    placements = project_layers.build_placements(store)
    lines = project_layers.ensure_layer(
        project, store.gpkg_path, rpls[1]["lines_layer"], placements=placements)
    points = project_layers.ensure_layer(
        project, store.gpkg_path, rpls[1]["points_layer"], placements=placements)
    ok = lines is not None and points is not None
    sep = project_layers.NAME_SEPARATOR
    expected = sep.join(["Atlantic Link", "Segment 1", "Rev 2", "Lines"])
    ok = ok and lines.name() == expected
    ok = ok and points.name().endswith(sep + "Points")

    root = project.layerTreeRoot().findGroup(project_layers.WORKBENCH_GROUP)
    ok = ok and root is not None
    system_group = root.findGroup("Atlantic Link") if root else None
    ok = ok and system_group is not None
    segment_group = system_group.findGroup("Segment 1") if system_group else None
    ok = ok and segment_group is not None
    # The revision group reads like its layers: "<segment> · <revision>".
    revision_group = segment_group.findGroup(sep.join(["Segment 1", "Rev 2"])) \
        if segment_group else None
    ok = ok and (segment_group is None or segment_group.findGroup("Rev 2") is None)
    ok = ok and revision_group is not None and len(revision_group.findLayers()) == 2
    # Layers stay findable by source no matter what they are called.
    ok = ok and project_layers.find_layer(
        project, store.gpkg_path, rpls[1]["lines_layer"]) is lines
    project.clear()
    return _result("layers named and grouped by system / segment / revision", ok)


def test_organise_follows_a_rename() -> bool:
    project = QgsProject.instance()
    project.clear()
    store, route_id, rpls = _store_with_system_and_route()
    placements = project_layers.build_placements(store)
    lines = project_layers.ensure_layer(
        project, store.gpkg_path, rpls[0]["lines_layer"], placements=placements)

    route = store.get_route(route_id)
    route["name"] = "Segment 1A"
    store.save_route(route)
    moved = project_layers.organise_workbench_layers(project, store)

    ok = moved >= 1 and "Segment 1A" in lines.name()
    root = project.layerTreeRoot().findGroup(project_layers.WORKBENCH_GROUP)
    system_group = root.findGroup("Atlantic Link") if root else None
    ok = ok and system_group is not None
    ok = ok and system_group.findGroup("Segment 1A") is not None
    # The stale group is cleaned up rather than left behind empty.
    ok = ok and system_group.findGroup("Segment 1") is None
    project.clear()
    return _result("organise_workbench_layers follows a rename", ok)


def test_saved_style_is_not_overwritten() -> bool:
    """A style the user saved as the layer default must survive a reload."""
    from qgis.core import QgsCategorizedSymbolRenderer, QgsLineSymbol, QgsRendererCategory

    project = QgsProject.instance()
    project.clear()
    store, rpl = _store_with_rpl()
    lines = project_layers.ensure_layer(project, store.gpkg_path, rpl["lines_layer"])
    ok = lines is not None

    # Stand in for the user's own symbology: one magenta category on the same
    # field, saved into the GeoPackage as the default style.
    custom = QgsCategorizedSymbolRenderer(layer_style.CABLE_TYPE_FIELD, [
        QgsRendererCategory("DA", QgsLineSymbol.createSimple({"color": "#ff00ff"}), "DA"),
    ])
    lines.setRenderer(custom)
    layer_style.save_default_style(lines)

    # Re-styling on the way in must leave it alone, only adding the missing
    # category for the cable type that has no rule yet.
    layer_style.style_rpl_layer(lines, rpl["lines_layer"])
    renderer = lines.renderer()
    ok = ok and isinstance(renderer, QgsCategorizedSymbolRenderer)
    colours = {str(c.value()): c.symbol().color().name().lower()
               for c in renderer.categories()}
    ok = ok and colours.get("DA") == "#ff00ff"
    ok = ok and "LW" in colours          # new value picked up
    project.clear()
    return _result("a user's saved style is not overwritten", ok)


def test_user_palette_applies() -> bool:
    from qgis.core import QgsCategorizedSymbolRenderer

    project = QgsProject.instance()
    project.clear()
    store, rpl = _store_with_rpl()
    previous = layer_style.user_cable_type_colours()
    try:
        layer_style.set_user_cable_type_colours({"DA": "#123456"})
        lines = project_layers.ensure_layer(project, store.gpkg_path, rpl["lines_layer"])
        ok = lines is not None
        # ensure_layer's first styling already uses the palette.
        renderer = lines.renderer()
        ok = ok and isinstance(renderer, QgsCategorizedSymbolRenderer)
        colours = {str(c.value()): c.symbol().color().name().lower()
                   for c in renderer.categories()}
        ok = ok and colours.get("DA") == "#123456"

        layer_style.set_user_cable_type_colours({"DA": "#654321"})
        restyled = layer_style.restyle_workbench_layers(
            project, gpkg_path=store.gpkg_path)
        colours = {str(c.value()): c.symbol().color().name().lower()
                   for c in lines.renderer().categories()}
        ok = ok and restyled == 1 and colours.get("DA") == "#654321"
    finally:
        layer_style.set_user_cable_type_colours(previous)
    project.clear()
    return _result("user cable-type palette is applied and reapplied", ok)


def test_organise_does_not_delete_the_registry() -> bool:
    """Moving a layer's tree node must not read as "the user removed it".

    The dock deletes an RPL from the registry when its project layers are
    removed. Regrouping removes and re-adds layer-tree nodes, so if that ever
    reached the removal path it would destroy the revision. The clone is
    inserted before the original node is dropped precisely so QGIS's
    layer-tree/registry bridge still finds the layer and keeps it.
    """
    from ..workbench.workbench_dock import WorkbenchDock

    project = QgsProject.instance()
    project.clear()
    store, route_id, rpls = _store_with_system_and_route()
    set_project_gpkg_path(store.gpkg_path, project)
    placements = project_layers.build_placements(store)
    for rpl in rpls:
        project_layers.ensure_rpl_layers(project, store.gpkg_path, rpl,
                                         placements=placements)
    layer_ids = set(project.mapLayers())
    ok = len(layer_ids) == 4

    dock = WorkbenchDock(None)          # connects the project-layer sync
    try:
        # Flatten everything back into the top group, then re-file it: the
        # move path runs for every layer.
        top = project_layers.workbench_group(project)
        for layer in list(project.mapLayers().values()):
            project_layers.move_layer_to_group(project, layer, top)
        route = store.get_route(route_id)
        route["name"] = "Segment 1 renamed"
        store.save_route(route)
        moved = project_layers.organise_workbench_layers(project, store)

        ok = ok and moved == 4
        ok = ok and set(project.mapLayers()) == layer_ids      # nothing dropped
        ok = ok and len(store.list_rpls()) == 2                # registry intact
        ok = ok and store.get_route(route_id) is not None
        ok = ok and all(layer.isValid() for layer in project.mapLayers().values())
        for rpl in rpls:
            for key in ("points_layer", "lines_layer"):
                ok = ok and project_layers.find_layer(
                    project, store.gpkg_path, rpl[key]) is not None
    finally:
        dock.shutdown()
        dock.deleteLater()
    project.clear()
    return _result("regrouping never deletes registry rows or layers", ok)


def run_all():
    return [
        test_layer_name_from_source(),
        test_ensure_layer_add_style_dedupe(),
        test_layers_are_named_and_grouped(),
        test_organise_follows_a_rename(),
        test_organise_does_not_delete_the_registry(),
        test_saved_style_is_not_overwritten(),
        test_user_palette_applies(),
        test_restore_after_reopen(),
        test_restore_completes_half_present_rpl(),
        test_discovers_unique_custom_named_registry(),
        test_discovery_does_not_guess_between_registries(),
        test_open_validation_does_not_modify_unrelated_gpkg(),
        test_workbench_dock_shows_and_switches_registry(),
        test_teardown_guard(),
        test_sync_survives_deleted_layers(),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(run_all()) else 1)
