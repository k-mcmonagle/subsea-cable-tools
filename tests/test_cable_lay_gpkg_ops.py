# -*- coding: utf-8 -*-
"""Tests for the project data-file operations behind the Explorer's Project tab.

Covers :mod:`processing.cable_lay_gpkg_ops` (create / inventory / suffix
resolution / duplicate / delete / discovery) plus a headless smoke test of the
Project panel and the Manage > Import summary. Runs under the QGIS python via
``tests/run_qgis_smoke_tests.py`` (or a single-module driver); needs a
QgsApplication.
"""

from __future__ import annotations

import gc
import os
from typing import List, Optional

from qgis.core import QgsProject, QgsVectorLayer

from ..processing import cable_lay_gpkg_ops as gops
from ..processing import cable_lay_parsers as clp
from .test_cable_lay_manage import (
    _CABLE_LAY_FILE,
    _fresh_gpkg,
    _import,
    _result,
    _write_temp,
)


def _imported_gpkg(tag: str) -> Optional[str]:
    """A fresh GeoPackage holding three cable-lay rows (None on failure)."""
    csv_path = _write_temp(f"sct_gops_{tag}.csv", _CABLE_LAY_FILE)
    gpkg = _fresh_gpkg(f"sct_gops_{tag}.gpkg")
    layer = _import([csv_path], gpkg, "2024-01-01")
    if layer is None:
        return None
    del layer
    gc.collect()
    return gpkg


def _entry(inv: dict, layer_type: str) -> dict:
    for entry in inv["entries"]:
        if entry["type"] == layer_type:
            return entry
    return {}


def _clear_project() -> None:
    project = QgsProject.instance()
    project.removeAllMapLayers()
    gops.set_project_gpkg_path(None, project)


# ---------------------------------------------------------------------------
def test_create_and_inventory() -> bool:
    name = "create_gpkg + inventory"
    gpkg = _fresh_gpkg("sct_gops_create.gpkg")
    try:
        created = gops.create_gpkg(gpkg, QgsProject.instance().transformContext())
        inv = gops.inventory(gpkg)
        try:
            gops.create_gpkg(gpkg, QgsProject.instance().transformContext())
            refused = False
        except RuntimeError:
            refused = True
    except Exception as exc:
        return _result(name, False, repr(exc))
    ok = (
        len(created) == len(gops.ALL_TYPES)
        and inv["valid"]
        and all(e["exists"] and e["rows"] == 0 for e in inv["entries"])
        and not inv["extras"]
        and refused
    )
    return _result(name, ok, f"created={len(created)} valid={inv['valid']} refused={refused}")


def test_inventory_after_import_and_add_missing() -> bool:
    name = "inventory after import + add_missing_layers"
    gpkg = _imported_gpkg("inv")
    if gpkg is None:
        return _result(name, False, "import failed")
    try:
        before = gops.inventory(gpkg)
        cable = _entry(before, "cable_lay")
        missing_before = [e["type"] for e in before["entries"] if not e["exists"]]
        created = gops.add_missing_layers(gpkg, QgsProject.instance().transformContext())
        after = gops.inventory(gpkg)
    except Exception as exc:
        return _result(name, False, repr(exc))
    ok = (
        cable.get("rows") == 3
        and bool(cable.get("last_import", ("", ""))[0])
        and not cable.get("missing_columns")
        and "slack_logs" in missing_before
        and len(created) == len(missing_before)
        and all(e["exists"] for e in after["entries"])
        and _entry(after, "cable_lay")["rows"] == 3
    )
    return _result(
        name, ok,
        f"rows={cable.get('rows')} last_import={cable.get('last_import')} "
        f"missing_before={len(missing_before)} created={len(created)}",
    )


def test_suffix_resolution() -> bool:
    name = "suffix-based layer resolution (renamed prefix, duplicates)"
    gpkg = _fresh_gpkg("sct_gops_suffix.gpkg")
    context = QgsProject.instance().transformContext()
    try:
        wkb, specs = clp.CANONICAL_SCHEMAS["cable_lay"]
        clp.write_layer_to_gpkg(gpkg, "Other_cable_lay", clp.fields_from_specs(specs), wkb, [], context)
        names = [n for n, _ in gops.list_tables(gpkg)]
        found = gops.find_layer_for_type(gpkg, names, "cable_lay")
        created = gops.add_missing_layers(gpkg, context)
        inv = gops.inventory(gpkg)
        entry = _entry(inv, "cable_lay")
        # Now add the preferred (prefixed) table too: it wins, the other is a duplicate.
        preferred = clp.prefixed_layer_name(gpkg, "cable_lay")
        clp.write_layer_to_gpkg(gpkg, preferred, clp.fields_from_specs(specs), wkb, [], context)
        inv2 = gops.inventory(gpkg)
        entry2 = _entry(inv2, "cable_lay")
    except Exception as exc:
        return _result(name, False, repr(exc))
    ok = (
        found == "Other_cable_lay"
        and preferred not in created
        and entry.get("name") == "Other_cable_lay"
        and entry2.get("name") == preferred
        and entry2.get("duplicates") == ["Other_cable_lay"]
        and gops.is_cable_lay_gpkg(gpkg)
    )
    return _result(name, ok, f"found={found} entry={entry.get('name')} dup={entry2.get('duplicates')}")


def test_invalid_files() -> bool:
    name = "list_tables / inventory on non-GeoPackage input"
    text = _write_temp("sct_gops_notagpkg.gpkg", "hello\n")
    missing = _fresh_gpkg("sct_gops_missing.gpkg")
    try:
        tables = gops.list_tables(text)
        inv_text = gops.inventory(text)
        inv_missing = gops.inventory(missing)
        inv_none = gops.inventory("")
    except Exception as exc:
        return _result(name, False, repr(exc))
    ok = (
        tables is None
        and not gops.is_geopackage(text)
        and not inv_text["valid"] and inv_text["exists"]
        and not inv_missing["exists"] and inv_missing["error"] == "File not found."
        and not inv_none["exists"]
    )
    return _result(name, ok, f"text={inv_text['error']!r} missing={inv_missing['error']!r}")


def test_duplicate() -> bool:
    name = "duplicate_gpkg (while open, refuses overwrite)"
    src = _imported_gpkg("dup")
    if src is None:
        return _result(name, False, "import failed")
    dst = _fresh_gpkg("sct_gops_dup_copy.gpkg")
    progress: List[float] = []
    try:
        held = QgsVectorLayer(clp.gpkg_layer_uri(src, clp.prefixed_layer_name(src, "cable_lay")), "held", "ogr")
        result = gops.duplicate_gpkg(src, dst, progress.append)
        inv = gops.inventory(dst)
        try:
            gops.duplicate_gpkg(src, dst)
            refused = False
        except RuntimeError:
            refused = True
        del held
    except Exception as exc:
        return _result(name, False, repr(exc))
    entry = _entry(inv, "cable_lay")
    ok = (
        result == dst and os.path.isfile(dst) and inv["valid"]
        and entry.get("rows") == 3 and refused and progress and progress[-1] == 100.0
    )
    return _result(name, ok, f"rows={entry.get('rows')} refused={refused} progress={len(progress)}")


def test_project_layers_and_delete() -> bool:
    name = "add_layer_to_project / project_layers_for / delete_gpkg"
    gpkg = _imported_gpkg("del")
    if gpkg is None:
        return _result(name, False, "import failed")
    _clear_project()
    project = QgsProject.instance()
    try:
        table = clp.prefixed_layer_name(gpkg, "cable_lay")
        layer = gops.add_layer_to_project(gpkg, table, project)
        again = gops.add_layer_to_project(gpkg, table, project)
        in_project = gops.project_layers_for(gpkg, project)
        group = project.layerTreeRoot().findGroup(clp.gpkg_stem(gpkg))
        renamed_ok = False
        if layer is not None:
            layer.setName("Something else entirely")
            renamed_ok = gops.project_layer_for_table(gpkg, table, project) is layer
        removed = gops.remove_project_layers_for(gpkg, project)
        del layer, again
        gc.collect()
        gops.delete_gpkg(gpkg)
        gone = not os.path.exists(gpkg)
    except Exception as exc:
        _clear_project()
        return _result(name, False, repr(exc))
    _clear_project()
    ok = (
        len(in_project) == 1 and group is not None and renamed_ok
        and removed == 1 and gone
    )
    return _result(name, ok, f"in_project={len(in_project)} removed={removed} gone={gone} renamed_ok={renamed_ok}")


def test_discover() -> bool:
    name = "discover_gpkg_path (saved / loaded layers / missing)"
    gpkg = _imported_gpkg("disc")
    if gpkg is None:
        return _result(name, False, "import failed")
    _clear_project()
    project = QgsProject.instance()
    try:
        none_path, none_note = gops.discover_gpkg_path(project)
        gops.set_project_gpkg_path(gpkg, project)
        saved_path, saved_note = gops.discover_gpkg_path(project)
        # Saved path lost, but a layer from the file is loaded -> recovered with a note.
        gops.set_project_gpkg_path(os.path.join(os.path.dirname(gpkg), "moved_away.gpkg"), project)
        gops.add_layer_to_project(gpkg, clp.prefixed_layer_name(gpkg, "cable_lay"), project)
        rec_path, rec_note = gops.discover_gpkg_path(project)
        gops.remove_project_layers_for(gpkg, project)
        lost_path, lost_note = gops.discover_gpkg_path(project)
    except Exception as exc:
        _clear_project()
        return _result(name, False, repr(exc))
    _clear_project()
    ok = (
        none_path is None and none_note == ""
        and gops.same_path(saved_path, gpkg) and saved_note == ""
        and gops.same_path(rec_path, gpkg) and "not found" in rec_note
        and lost_path is None and "not found" in lost_note
    )
    return _result(name, ok, f"none={none_path} saved={saved_path} rec={rec_path} lost={lost_path}")


class _Controller:
    """Minimal Explorer stand-in for the Project / Manage panels."""

    def __init__(self, project_path: Optional[str] = None):
        self._project_path = project_path
        self.loaded: List[str] = []
        self.imports: List[str] = []
        self.import_requests: List[str] = []

    # ProjectPanel contract
    def transform_context(self):
        return QgsProject.instance().transformContext()

    def loaded_layer_ids(self):
        return list(self.loaded)

    def load_layers(self, ids):
        self.loaded += [i for i in ids if i not in self.loaded]

    def unload_layers(self, ids):
        self.loaded = [i for i in self.loaded if i not in set(ids)]

    def is_busy(self):
        return False

    def go_to_import(self, layer_type):
        self.import_requests.append(layer_type)

    # ManagePanel contract (subset)
    @property
    def layer(self):
        return None

    def gpkg_path(self):
        return None

    def project_path(self):
        return self._project_path

    def project_entry(self, _layer_type):
        return None

    def after_import(self, layer_id):
        self.imports.append(layer_id)

    def reload_dataset(self):
        pass


def test_project_panel_smoke() -> bool:
    name = "ProjectPanel headless: scan, add/load/remove, delete"
    try:
        from ..explorer.panels.project_panel import ProjectPanel
    except Exception as exc:  # pragma: no cover - pyqtgraph-less environments
        return _result(name, True, f"skipped ({exc})")
    gpkg = _imported_gpkg("panel")
    if gpkg is None:
        return _result(name, False, "import failed")
    _clear_project()
    ProjectPanel.run_async = False
    controller = _Controller()
    try:
        panel = ProjectPanel(controller)
        panel.set_path(gpkg)
        inv = panel.inventory()
        rows = panel.table.rowCount()
        cable_row = next(
            r for r in range(rows) if panel.table.item(r, 0).text() == gops.TYPE_LABELS["cable_lay"]
        )
        panel.table.selectRow(cable_row)
        panel.add_selected_to_project()
        in_project = len(gops.project_layers_for(gpkg))
        panel.table.selectRow(cable_row)
        panel.load_selected()
        loaded = list(controller.loaded)
        panel.refresh()
        explorer_cell = panel.table.item(cable_row, 3).text()
        panel.table.selectRow(cable_row)
        panel.import_selected()
        panel.table.selectRow(cable_row)
        panel.remove_selected_from_project()
        after_remove = len(gops.project_layers_for(gpkg))
        remembered = gops.same_path(gops.project_gpkg_path(), gpkg)
        deleted = panel.delete_current(confirm=False)
        cleared = gops.project_gpkg_path() is None and panel.current_path() is None
    except Exception as exc:
        _clear_project()
        return _result(name, False, repr(exc))
    finally:
        ProjectPanel.run_async = True
    _clear_project()
    ok = (
        inv is not None and inv["valid"] and rows == len(gops.ALL_TYPES)
        and in_project == 1 and len(loaded) == 1 and explorer_cell == "yes"
        and controller.import_requests == ["cable_lay"]
        and after_remove == 0 and not controller.loaded and remembered
        and deleted and cleared and not os.path.exists(gpkg)
    )
    return _result(
        name, ok,
        f"rows={rows} in_project={in_project} loaded={len(loaded)} explorer={explorer_cell!r} "
        f"after_remove={after_remove} deleted={deleted} cleared={cleared}",
    )


def test_manage_import_summary() -> bool:
    name = "ManagePanel > Import summary and recent imports"
    try:
        from ..explorer.panels.manage_panel import ManagePanel
    except Exception as exc:  # pragma: no cover
        return _result(name, True, f"skipped ({exc})")
    gpkg = _imported_gpkg("mimport")
    if gpkg is None:
        return _result(name, False, "import failed")
    try:
        panel = ManagePanel(_Controller(gpkg))
        panel.show_import("cable_lay")
        summary = panel.import_summary.text()
        recent = panel.import_recent.rowCount()
        panel.show_import("slack_logs")
        summary_missing = panel.import_summary.text()
        none_panel = ManagePanel(_Controller(None))
        none_panel.show_import("cable_lay")
        none_enabled = none_panel.import_button.isEnabled()
    except Exception as exc:
        return _result(name, False, repr(exc))
    table = clp.prefixed_layer_name(gpkg, "cable_lay")
    ok = (
        table in summary and recent == 1
        and "no Slack logs layer yet" in summary_missing
        and not none_enabled
    )
    return _result(name, ok, f"summary={summary!r} recent={recent} none_enabled={none_enabled}")


def run_all() -> List[bool]:
    results = [
        test_create_and_inventory(),
        test_inventory_after_import_and_add_missing(),
        test_suffix_resolution(),
        test_invalid_files(),
        test_duplicate(),
        test_project_layers_and_delete(),
        test_discover(),
        test_project_panel_smoke(),
        test_manage_import_summary(),
    ]
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    run_all()
