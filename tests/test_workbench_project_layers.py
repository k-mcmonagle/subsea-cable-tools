# -*- coding: utf-8 -*-
"""Checks for workbench project-layer management and standard styling.

Covers source-URI matching (Windows case/slash robustness), ensure_layer
add/dedupe, the automatic CableType/Event symbology, project-open restore
(restore_workbench_layers), and the project-teardown guard that protects the
registry when QGIS clears all layers.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile

from qgis.core import QgsProject

from ..qgis_compat import WKB_LINESTRING, WKB_POINT
from ..workbench import layer_style, project_layers, schema
from ..workbench.store import WorkbenchStore, set_project_gpkg_path
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


def run_all():
    return [
        test_layer_name_from_source(),
        test_ensure_layer_add_style_dedupe(),
        test_restore_after_reopen(),
        test_restore_completes_half_present_rpl(),
        test_teardown_guard(),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(run_all()) else 1)
