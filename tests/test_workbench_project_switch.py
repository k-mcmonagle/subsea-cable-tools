# -*- coding: utf-8 -*-
"""Cable Route Workbench across project changes (New, Open, Close).

QGIS clears the project (``aboutToBeCleared`` ... ``cleared``) before New,
Close and opening another project; ``iface.projectRead`` /
``iface.newProjectCreated`` follow Open and New. Checked with the real
``QgsProject.instance().clear()`` / ``read()`` and a stub iface:

* opening another project leaves nothing of the old one on show, and no
  later action writes to the old project's GeoPackage (RPL page, New
  assembly); every panel works on the new project's registry;
* Close leaves the dock empty and inert;
* a project that never used the Workbench - including every new project,
  which shares the unsaved-project registry - gets no layers, groups or
  project entry (so is not made dirty) until the user works in it; the first
  real write or opened RPL records the registry;
* opening a project beside another project's registry does not adopt it,
  while a project whose layers read that registry still does;
* an assessment run in flight is abandoned, the RPL edit tool handed back
  and position flashes removed;
* shutdown() disconnects every project hook.

QSettings and the unsaved-project folder are redirected to temporary folders.
"""

from __future__ import annotations

import os
import tempfile

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsMapCanvas, QgsMessageBar
from qgis.PyQt.QtCore import QCoreApplication, QObject, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QInputDialog, QMainWindow

from ..processing.cable_lay_parsers import WKT_KEY
from ..qgis_compat import WKB_LINESTRING, WKB_POINT
from ..workbench import project_layers, schema
from ..workbench.store import WorkbenchStore, project_gpkg_path, set_project_gpkg_path

REQUIRES_QGIS = True

POINT_SPECS = [("rpl_id", "str"), ("SeqNo", "int"), ("Event", "str")]
LINE_SPECS = [("rpl_id", "str"), ("SeqNo", "int"), ("CableType", "str")]


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _pump():
    for _ in range(3):
        QCoreApplication.processEvents()


def _same(a, b) -> bool:
    return bool(a) and bool(b) and (os.path.normcase(os.path.abspath(a))
                                     == os.path.normcase(os.path.abspath(b)))


class _Iface(QObject):
    """The QgisInterface parts the dock uses, on real widgets."""

    projectRead = pyqtSignal()
    newProjectCreated = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.window = QMainWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar(self.window)

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.window

    def messageBar(self):
        return self.bar


def _make_store(folder, name, route="Route A", with_rpl=True, with_assembly=False):
    store = WorkbenchStore(os.path.join(folder, name))
    store.ensure_created()
    rpl_id = None
    if with_rpl:
        rpl_id = schema.new_id()
        points = schema.rpl_points_layer_name(route)
        lines = schema.rpl_lines_layer_name(route)
        store.write_spatial_layer(points, POINT_SPECS, WKB_POINT, [
            {"rpl_id": rpl_id, "SeqNo": 0, "Event": "BMH", WKT_KEY: "POINT (0 0)"},
            {"rpl_id": rpl_id, "SeqNo": 1, "Event": None, WKT_KEY: "POINT (0.1 0)"},
            {"rpl_id": rpl_id, "SeqNo": 2, "Event": "JOINT", WKT_KEY: "POINT (0.2 0)"},
        ])
        store.write_spatial_layer(lines, LINE_SPECS, WKB_LINESTRING, [
            {"rpl_id": rpl_id, "SeqNo": 0, "CableType": "DA", WKT_KEY: "LINESTRING (0 0, 0.1 0)"},
            {"rpl_id": rpl_id, "SeqNo": 1, "CableType": "LW", WKT_KEY: "LINESTRING (0.1 0, 0.2 0)"},
        ])
        store.save_rpl({"rpl_id": rpl_id, "name": f"{route} Rev 1", "kind": "rpl",
                        "points_layer": points, "lines_layer": lines})
    assembly_id = None
    if with_assembly:
        assembly_id = schema.new_id()
        store.save_assembly({"assembly_id": assembly_id, "name": "Load 01", "kind": "cable",
                             "total_cable_len_m": 12000.0},
                            [{"kind": "section", "name": "LW", "length_m": 12000.0,
                              "cable_type": "LW"}])
    return store, rpl_id, assembly_id


def _save_project(path, gpkg_path=None) -> str:
    project = QgsProject.instance()
    project.clear()
    if gpkg_path:
        set_project_gpkg_path(gpkg_path, project)
    if not project.write(path):
        raise RuntimeError(f"could not write {path}: {project.error()}")
    project.clear()
    return path


def _open(path, iface):
    project = QgsProject.instance()
    if not project.read(path):
        raise RuntimeError(f"could not read {path}: {project.error()}")
    iface.projectRead.emit()
    _pump()


def _new(iface):
    QgsProject.instance().clear()
    iface.newProjectCreated.emit()
    _pump()


def _snapshot(gpkg_path):
    """The registry rows a stray write would change."""
    store = WorkbenchStore(gpkg_path)
    return (sorted((r.get("rpl_id"), r.get("rev_label"), r.get("modified_utc"))
                   for r in store.list_rpls()),
            sorted(r.get("assembly_id") for r in store.list_assemblies()),
            sorted(r.get("route_id") for r in store.list_routes()))


def _select(dock, kind, entity_id) -> bool:
    def walk(item):
        if item.data(0, Qt.ItemDataRole.UserRole) == (kind, entity_id):
            return item
        for i in range(item.childCount()):
            hit = walk(item.child(i))
            if hit is not None:
                return hit
        return None

    for i in range(dock.tree.topLevelItemCount()):
        hit = walk(dock.tree.topLevelItem(i))
        if hit is not None:
            dock.tree.setCurrentItem(hit)
            _pump()
            return True
    return False


def _dock(iface):
    from ..workbench.workbench_dock import WorkbenchDock

    dock = WorkbenchDock(iface)
    _pump()
    return dock


def _close(dock):
    dock.shutdown()
    dock.deleteLater()
    QgsProject.instance().clear()
    _pump()


# -- tests ---------------------------------------------------------------------
def test_open_other_project_writes_nothing_to_the_old_file(folder) -> bool:
    from ..workbench.workbench_dock import KIND_RPL

    folder_b = tempfile.mkdtemp(dir=folder)
    store_b, _, _ = _make_store(folder_b, "B_workbench.gpkg", with_rpl=False)
    path_b = _save_project(os.path.join(folder_b, "B.qgs"), store_b.gpkg_path)
    store_a, rpl_a, _ = _make_store(tempfile.mkdtemp(dir=folder), "A_workbench.gpkg",
                                    with_assembly=True)
    set_project_gpkg_path(store_a.gpkg_path, QgsProject.instance())
    iface = _Iface()
    dock = _dock(iface)
    try:
        rp = dock.rpl_panel
        ok = _select(dock, KIND_RPL, rpl_a) and rp.sync is not None
        ok = ok and dock.stack.currentWidget() is rp and rp.points_table.rowCount() == 3
        before_a = _snapshot(store_a.gpkg_path)

        _open(path_b, iface)
        ok = ok and dock.stack.currentWidget() is dock.placeholder
        ok = ok and rp.current_rpl is None and rp.points_table.rowCount() == 0
        ok = ok and not rp.revision_save_btn.isEnabled()
        ok = ok and rp.store is not None and _same(rp.store.gpkg_path, store_b.gpkg_path)
        ok = ok and dock.assembly_panel.store is rp.store
        ok = ok and dock.assessment_panel.store is rp.store
        ok = ok and dock.assembly_panel.assembly is None
        # What the old page offered: nothing to save any more.
        rp.revision_edit.setText("Rev 9")
        rp._save_revision_label()
        rp._edit_depth_sources()
        dock._new_assembly()
        _pump()
        fresh_b = WorkbenchStore(store_b.gpkg_path)
        ok = ok and fresh_b.list_rpls() == [] and len(fresh_b.list_assemblies()) == 1
        ok = ok and _snapshot(store_a.gpkg_path) == before_a
        detail = f"B assemblies={len(fresh_b.list_assemblies())}"
    finally:
        _close(dock)
    return _result("open another project: panels reset, nothing written to the old file", ok, detail)


def test_close_project_leaves_the_dock_inert(folder) -> bool:
    from ..workbench.workbench_dock import KIND_RPL

    store_a, rpl_a, _ = _make_store(tempfile.mkdtemp(dir=folder), "A_workbench.gpkg")
    project = QgsProject.instance()
    project.clear()
    set_project_gpkg_path(store_a.gpkg_path, project)
    iface = _Iface()
    dock = _dock(iface)
    try:
        ok = _select(dock, KIND_RPL, rpl_a) and len(project.mapLayers()) == 2
        before_a = _snapshot(store_a.gpkg_path)
        project.clear()  # Project > Close: neither projectRead nor newProjectCreated
        _pump()
        rp = dock.rpl_panel
        ok = ok and dock.tree.topLevelItemCount() == 0
        ok = ok and "Not connected" in dock.store_label.text()
        ok = ok and rp.store is None and dock.assembly_panel.store is None
        ok = ok and dock.assessment_panel.store is None
        ok = ok and dock.stack.currentWidget() is dock.placeholder
        ok = ok and rp.current_rpl is None and rp.sync is None
        rp._save_revision_label()
        rp._edit_depth_sources()
        ok = ok and not project.mapLayers() and not project.isDirty()
        ok = ok and project_gpkg_path(project) is None
        ok = ok and _snapshot(store_a.gpkg_path) == before_a
    finally:
        _close(dock)
    return _result("Close project: dock empty and inert", ok)


def test_new_project_never_adopts_the_shared_registry(folder, unsaved) -> bool:
    from ..workbench.workbench_dock import KIND_RPL

    shared, rpl_shared, _ = _make_store(unsaved, "project_workbench.gpkg",
                                        route="Old Unsaved Route")
    store_a, rpl_a, _ = _make_store(tempfile.mkdtemp(dir=folder), "A_workbench.gpkg")
    project = QgsProject.instance()
    project.clear()
    iface = _Iface()
    dock = _dock(iface)  # opened in a new, unsaved project
    try:
        ok = not project.mapLayers() and project_gpkg_path(project) is None
        ok = ok and not project.isDirty()
        ok = ok and _same(dock.rpl_panel.store.gpkg_path, shared.gpkg_path)  # listed

        set_project_gpkg_path(store_a.gpkg_path, project)  # a project using its own
        iface.projectRead.emit()
        _pump()
        ok = ok and _select(dock, KIND_RPL, rpl_a) and len(project.mapLayers()) == 2

        _new(iface)
        names = sorted(layer.name() for layer in project.mapLayers().values())
        ok = ok and not names and project_gpkg_path(project) is None
        ok = ok and not project.isDirty()
        ok = ok and project.layerTreeRoot().findGroup(project_layers.WORKBENCH_GROUP) is None
        ok = ok and _same(dock.rpl_panel.store.gpkg_path, shared.gpkg_path)
        ok = ok and dock.rpl_panel.current_rpl is None  # nothing opened unasked

        # The user opens the shared registry's RPL: now it is this project's.
        ok = ok and _select(dock, KIND_RPL, rpl_shared) and len(project.mapLayers()) == 2
        ok = ok and _same(project_gpkg_path(project), shared.gpkg_path)
        detail = f"layers after New={names}"
    finally:
        _close(dock)
    return _result("New project: no layers, entry or dirty flag until the user acts", ok, detail)


def test_first_write_records_the_registry(folder) -> bool:
    project_folder = tempfile.mkdtemp(dir=folder)
    path = _save_project(os.path.join(project_folder, "plain.qgs"))
    iface = _Iface()
    project = QgsProject.instance()
    _open(path, iface)
    dock = _dock(iface)
    try:
        ok = project_gpkg_path(project) is None and not project.isDirty()
        store = dock._store()
        ok = ok and store is not None and not store.exists()
        store.create_route("Route One")  # creates the registry and commits
        expected = os.path.join(project_folder, "plain_workbench.gpkg")
        ok = ok and _same(project_gpkg_path(project), expected)
    finally:
        _close(dock)
    return _result("first registry write records it in the project", ok)


def test_folder_scan_needs_a_project_that_uses_the_registry(folder) -> bool:
    project_folder = tempfile.mkdtemp(dir=folder)
    store, rpl_id, _ = _make_store(project_folder, "survey1_workbench.gpkg", route="Survey One")
    _save_project(os.path.join(project_folder, "survey1.qgs"), store.gpkg_path)
    other = _save_project(os.path.join(project_folder, "other.qgs"))
    project = QgsProject.instance()

    project.read(other)
    found = project_layers.discover_gpkg_path(project)
    touched = project_layers.restore_workbench_layers(project)
    ok = found is None and touched == 0 and not project.mapLayers()
    ok = ok and project_gpkg_path(project) is None
    detail = f"unrelated: found={found}, touched={touched}"

    # An older project that shows the registry's layers but has no entry
    # still finds it (today's behaviour for a project using the Workbench).
    rpl = store.get_rpl(rpl_id)
    layer = QgsVectorLayer(f"{store.gpkg_path}|layername={rpl['lines_layer']}", "lines", "ogr")
    project.addMapLayer(layer)
    legacy = os.path.join(project_folder, "legacy.qgs")
    project.write(legacy)
    project.clear()
    project.read(legacy)
    found = project_layers.discover_gpkg_path(project)
    ok = ok and _same(found, store.gpkg_path)
    detail += f"; legacy: found={os.path.basename(found or '')}"
    project.clear()
    return _result("folder discovery only for a project using that registry", ok, detail)


def test_assessment_run_abandoned_on_new_project(folder) -> bool:
    store_a, rpl_a, _ = _make_store(tempfile.mkdtemp(dir=folder), "A_workbench.gpkg")
    project = QgsProject.instance()
    project.clear()
    set_project_gpkg_path(store_a.gpkg_path, project)
    iface = _Iface()
    dock = _dock(iface)

    class _Task:
        cancelled = False
        result = type("Result", (), {"warnings": [], "domain": None})()
        sampler = None
        error = None
        cancel_calls = 0

        def cancel(self):
            self.cancel_calls += 1

    try:
        panel = dock.assessment_panel
        task = _Task()
        panel.store = store_a
        panel.rpl_id = rpl_a
        panel.assessment = {"assessment_id": "a1", "rpl_id": rpl_a, "rule_set_id": "", "name": "A1"}
        panel._task = task
        generation = panel._run_generation
        _new(iface)
        ok = panel._task is None and panel._run_generation > generation
        ok = ok and task.cancel_calls == 1 and panel.assessment is None
        before = _snapshot(store_a.gpkg_path)
        panel._on_task_finished(task, generation)  # the late result is ignored
        _pump()
        ok = ok and not project.mapLayers() and _snapshot(store_a.gpkg_path) == before
    finally:
        _close(dock)
    return _result("assessment run in flight abandoned on New project", ok)


def test_edit_tool_and_flashes_released(folder) -> bool:
    from ..workbench.workbench_dock import KIND_RPL

    store_a, rpl_a, _ = _make_store(tempfile.mkdtemp(dir=folder), "A_workbench.gpkg")
    project = QgsProject.instance()
    project.clear()
    set_project_gpkg_path(store_a.gpkg_path, project)
    iface = _Iface()
    canvas = iface.canvas
    dock = _dock(iface)
    try:
        rp = dock.rpl_panel
        ok = _select(dock, KIND_RPL, rpl_a) and rp.sync is not None
        base = len(canvas.scene().items())
        rp.edit_btn.setChecked(True)
        tool = canvas.mapTool()
        ok = ok and tool is not None and tool is rp.edit_tool
        rp._flash_map_position(0.0, 0.05)
        ok = ok and len(canvas.scene().items()) == base + 1
        _new(iface)
        ok = ok and canvas.mapTool() is not tool and not rp.edit_btn.isChecked()
        ok = ok and len(canvas.scene().items()) == base
        detail = f"map tool={type(canvas.mapTool()).__name__}"
    finally:
        _close(dock)
    return _result("edit tool handed back and flashes removed on New project", ok, detail)


def test_shutdown_disconnects_project_hooks(folder) -> bool:
    from ..workbench.workbench_dock import KIND_RPL

    store_a, rpl_a, _ = _make_store(tempfile.mkdtemp(dir=folder), "A_workbench.gpkg")
    project = QgsProject.instance()
    project.clear()
    set_project_gpkg_path(store_a.gpkg_path, project)
    iface = _Iface()
    dock = _dock(iface)
    calls = []
    try:
        ok = _select(dock, KIND_RPL, rpl_a)
        dock.shutdown()
        dock.refresh_tree = lambda *a, **k: calls.append("refresh")
        project.clear()
        iface.projectRead.emit()
        iface.newProjectCreated.emit()
        _pump()
        ok = ok and not calls and (dock.rpl_panel.current_rpl or {}).get("rpl_id") == rpl_a
    finally:
        dock.deleteLater()
        project.clear()
        _pump()
    return _result("shutdown() disconnects the project hooks", ok, str(calls))


def run_all():
    results = []
    old_format = QSettings.defaultFormat()
    old_unsaved = schema.unsaved_project_folder
    old_get_text, old_get_item = QInputDialog.getText, QInputDialog.getItem
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        folder = tempfile.mkdtemp(prefix="wb_switch_")  # GeoPackages: never auto-deleted
        unsaved = tempfile.mkdtemp(dir=folder)
        schema.unsaved_project_folder = lambda: unsaved
        QInputDialog.getText = staticmethod(lambda *a, **k: ("New Asm", True))
        QInputDialog.getItem = staticmethod(lambda *a, **k: ("cable", True))
        try:
            for test in (lambda: test_open_other_project_writes_nothing_to_the_old_file(folder),
                         lambda: test_close_project_leaves_the_dock_inert(folder),
                         lambda: test_new_project_never_adopts_the_shared_registry(folder, unsaved),
                         lambda: test_first_write_records_the_registry(folder),
                         lambda: test_folder_scan_needs_a_project_that_uses_the_registry(folder),
                         lambda: test_assessment_run_abandoned_on_new_project(folder),
                         lambda: test_edit_tool_and_flashes_released(folder),
                         lambda: test_shutdown_disconnects_project_hooks(folder)):
                try:
                    results.append(test())
                except Exception as exc:  # report, keep going
                    import traceback
                    traceback.print_exc()
                    results.append(_result(getattr(test, "__name__", "test"), False, repr(exc)))
        finally:
            schema.unsaved_project_folder = old_unsaved
            QInputDialog.getText, QInputDialog.getItem = old_get_text, old_get_item
            QSettings.setDefaultFormat(old_format)
            QgsProject.instance().clear()
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
