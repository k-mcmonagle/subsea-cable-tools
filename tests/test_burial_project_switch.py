# -*- coding: utf-8 -*-
"""Burial Planner across project changes (requires the QGIS API).

QGIS keeps plugin map graphics, map tools and file handles when a project is
closed, so the dock drops everything tied to the old project on
``aboutToBeCleared`` (New, Close and the start of an Open) and resolves the
next project's plan file once that project is ready. These checks drive the
real ``QgsProject.clear()`` / ``read()``:

* Project > New with the dock open leaves no map graphics or pick tool, the
  old plan file is released, nothing more is written into it and none of
  its layers reach the new project;
* the other project's Workbench entry is never rewritten from the previous
  project's plan (dock open, or reopened after the switch);
* map markers are gone after opening another project;
* a saved plan file that fails to reopen leaves the dock inert — nothing is
  written into the previous project's file;
* opening a moved project does not duplicate its plan layers;
* a project that never used the Burial Planner gets no layers or entries
  until the user picks a plan;
* shutdown() releases the project-switch hooks.

QSettings are redirected to a temporary folder; dialogs are stubbed.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time

from qgis.core import (
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsRubberBand, QgsVertexMarker
from qgis.PyQt.QtCore import QCoreApplication, QObject, QSettings, pyqtSignal
from qgis.PyQt.QtGui import QColor, QShowEvent
from qgis.PyQt.QtWidgets import QMessageBox, QWidget

from ..burial import burial_dock, gpkg_sql, map_layers, schema, watchdog
from ..burial import store as store_module
from ..burial.kp_pick_tool import KpPickTool
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..processing.cable_lay_parsers import open_gpkg_layer
from ..workbench import schema as wb_schema
from ..workbench import store as wb_store_module
from ..workbench.store import WorkbenchStore

REQUIRES_QGIS = True


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


class _Bar:
    def pushMessage(self, *_args, **_kwargs):
        pass


class _Iface(QObject):
    """The parts of QgisInterface the dock uses, including the New signal."""

    newProjectCreated = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._canvas = QgsMapCanvas()
        self._canvas.resize(600, 400)
        self._main = QWidget()

    def mapCanvas(self):
        return self._canvas

    def mainWindow(self):
        return self._main

    def messageBar(self):
        return _Bar()

    def layerTreeView(self):
        return None


def _pump(seconds: float = 0.4) -> None:
    """Let coalescing / debounce timers fire."""
    end = time.time() + seconds
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def _canvas_items(canvas) -> int:
    return sum(1 for item in canvas.scene().items()
               if isinstance(item, (QgsRubberBand, QgsVertexMarker))
               and item.isVisible())


def _plan_layer_names(project) -> list:
    return sorted(layer.name() for layer in project.mapLayers().values()
                  if layer.name().startswith("bp_"))


def _open(path: str) -> bool:
    return gpkg_sql._key(path) in gpkg_sql._connections


class _Env:
    """Dialogs stubbed, watchdog + unsaved-project folder in a temp tree."""

    def __init__(self, root: str):
        self.root = root
        self.warnings = []

    def __enter__(self):
        self._boxes = {name: getattr(QMessageBox, name)
                       for name in ("warning", "information", "question")}
        QMessageBox.warning = staticmethod(
            lambda *a, **k: self.warnings.append(a[2] if len(a) > 2 else "") or 0)
        QMessageBox.information = staticmethod(lambda *a, **k: 0)
        QMessageBox.question = staticmethod(lambda *a, **k: 0)
        self._log_path = watchdog.default_log_path
        watchdog.default_log_path = lambda: os.path.join(self.root, "stall.log")
        self._unsaved = wb_schema.unsaved_project_folder
        self.unsaved = os.path.join(self.root, "unsaved")
        os.makedirs(self.unsaved, exist_ok=True)
        wb_schema.unsaved_project_folder = lambda: self.unsaved
        QgsProject.instance().clear()
        self.docks = []
        return self

    def dock(self, iface=None):
        dock = burial_dock.BurialPlannerDock(iface or _Iface())
        dock._save_window_state = lambda: None  # never touch QSettings
        self.docks.append(dock)
        _pump(0.1)
        return dock

    def __exit__(self, *_exc):
        try:
            for dock in self.docks:
                dock.shutdown()
                dock.deleteLater()
            _pump(0.1)
            QgsProject.instance().clear()
        finally:
            for name, value in self._boxes.items():
                setattr(QMessageBox, name, value)
            watchdog.default_log_path = self._log_path
            wb_schema.unsaved_project_folder = self._unsaved
            gpkg_sql.close()
        return False


def _line_layer(path: str) -> QgsVectorLayer:
    memory = QgsVectorLayer("LineString?crs=EPSG:4326", "route_line", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromWkt("LINESTRING(0 0, 0.02 0)"))
    memory.dataProvider().addFeatures([feature])
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = "route_line"
    QgsVectorFileWriter.writeAsVectorFormatV3(
        memory, path, QgsCoordinateTransformContext(), options)
    return QgsVectorLayer(path + "|layername=route_line", "route_line", "ogr")


def _workbench(folder: str, rpl_id: str, name: str) -> str:
    path = os.path.join(folder, os.path.basename(folder) + "_workbench.gpkg")
    store = WorkbenchStore(path)
    store.ensure_created()
    store.save_rpl({"rpl_id": rpl_id, "name": name, "rev_label": "R1",
                    "lines_layer": "rpl_lines"})
    return path


def _project_with_plan(folder: str, name: str = "A"):
    """A saved-path project with one plan; returns (plan file, route layer)."""
    os.makedirs(folder, exist_ok=True)
    project = QgsProject.instance()
    project.clear()
    project.setFileName(os.path.join(folder, f"{name}.qgs"))
    gpkg = os.path.join(folder, f"{name}_burial_plans.gpkg")
    store = BurialStore(gpkg)
    store.migrate()
    store_module.set_project_gpkg_path(gpkg)
    PlanModel(store).create_plan(f"Plan {name}", "plough")
    store.close()
    route = _line_layer(os.path.join(folder, "route.gpkg"))
    project.addMapLayer(route)
    return gpkg, route


def _set_route(dock, layer) -> None:
    tab = dock.inputs_tab
    tab.route_layer_radio.setChecked(True)
    tab.fallback_combo.setLayer(layer)
    tab.apply_rpl_button.click()
    tab.full_route_button.click()
    tab.apply_scope_button.click()
    _pump(0.1)


def _draw_everything(dock) -> None:
    dock.highlight_kp(0.6)
    dock.highlight_range(0.2, 0.8)
    dock.flash_point(0.01, 0.0)
    dock.set_exclusion_preview([(0.1, 0.3, QColor("red"))])
    dock.pick_kp_on_map(lambda _kp: None)


def _read(gpkg: str, fn):
    """Read the plan file with a store of the test's own, then release it
    (a store caches its SQL connection)."""
    store = BurialStore(gpkg)
    try:
        return fn(store)
    finally:
        store.close()


def _rewrite_paths(qgs: str, old_folder: str, new_folder: str) -> None:
    with open(qgs, encoding="utf-8") as handle:
        text = handle.read()
    for old, new in ((old_folder, new_folder),
                     (old_folder.replace("\\", "/"),
                      new_folder.replace("\\", "/"))):
        text = text.replace(old, new)
    with open(qgs, "w", encoding="utf-8") as handle:
        handle.write(text)


# -- tests ---------------------------------------------------------------------


def test_new_project_releases_everything(root: str) -> bool:
    project = QgsProject.instance()
    with _Env(root) as env:
        gpkg, route = _project_with_plan(os.path.join(root, "A"))
        iface = _Iface()
        dock = env.dock(iface)
        _set_route(dock, route)
        plan_id = dock.model.plan_id
        dock.model.add_event(0.5, "PLDN")
        dock.model.flush_layer_refresh()
        layers_before = _plan_layer_names(project)
        _draw_everything(dock)
        canvas = iface.mapCanvas()
        drawn = (_canvas_items(canvas), type(canvas.mapTool()).__name__)
        # An edit still waiting on the layer debounce when New is chosen.
        dock.model.add_event(0.7, "PLUP")
        events_name = schema.events_layer_name(
            dock.model.plan.get("name"), dock.model.plan.get("rev_label"),
            plan_id)

        project.clear()  # Project > New: aboutToBeCleared … cleared
        after_clear = (_canvas_items(canvas),
                       isinstance(canvas.mapTool(), KpPickTool),
                       _open(gpkg), dock.store.detached, dock.model.plan_id,
                       dock.plan_combo.count(),
                       dock.workflow_tabs.isEnabled())
        iface.newProjectCreated.emit()
        _pump(0.5)
        layer = open_gpkg_layer(gpkg, events_name)
        flushed = layer.featureCount() if layer is not None else -1
        layer = None
        # Nothing can write into A any more, and A's layers stay out.
        for attempt in (lambda: dock.model.add_event(0.9, "PLUP"),
                        lambda: dock.model.save_tool(
                            {"name": "Probe plough", "tool_type": "plough"})):
            try:
                attempt()
            except ValueError:
                pass  # refused: no plan open
        dock.refresh()  # the toolbar button again
        _pump(0.3)
        tools_in_a = _read(gpkg, BurialStore.list_tools)
        ok = len(layers_before) >= 2 and drawn == (4, "KpPickTool")
        ok = ok and after_clear == (0, False, False, True, "", 0, False)
        ok = ok and flushed == 2  # the pending edit landed in A, not lost
        ok = ok and _read(gpkg, lambda st: len(st.list_events(plan_id))) == 2
        ok = ok and not tools_in_a
        ok = ok and not project.mapLayers() and not _open(gpkg)
        ok = ok and project.layerTreeRoot().findGroup(
            map_layers.BURIAL_GROUP) is None
        ok = ok and store_module.project_gpkg_path() is None
        ok = ok and wb_store_module.project_gpkg_path() is None
        ok = ok and not project.isDirty()
        ok = ok and not os.listdir(env.unsaved)  # no plan file created
        return _result("Project > New: no graphics or pick tool left, plan "
                       "file released, pending edit flushed into it, nothing "
                       "more written, no layers or entries in the new project",
                       ok, f"drawn={drawn} after={after_clear} "
                       f"flushed={flushed} layers={_plan_layer_names(project)} "
                       f"unsaved={os.listdir(env.unsaved)} open={_open(gpkg)} "
                       f"dirty={project.isDirty()}")


def _workbench_projects(root: str):
    """B (own Workbench, no burial plans) on disk; A open with a plan on
    A's Workbench RPL. Returns (B path, B workbench, A workbench)."""
    project = QgsProject.instance()
    folder_b = os.path.join(root, "B")
    os.makedirs(folder_b)
    project.clear()
    project.setFileName(os.path.join(folder_b, "B.qgs"))
    wb_b = _workbench(folder_b, "rplB", "Route B")
    wb_store_module.set_project_gpkg_path(wb_b)
    project.write()
    folder_a = os.path.join(root, "A")
    os.makedirs(folder_a)
    project.clear()
    project.setFileName(os.path.join(folder_a, "A.qgs"))
    wb_a = _workbench(folder_a, "rplA", "Route A")
    wb_store_module.set_project_gpkg_path(wb_a)
    gpkg = os.path.join(folder_a, "A_burial_plans.gpkg")
    store = BurialStore(gpkg)
    store.migrate()
    store_module.set_project_gpkg_path(gpkg)
    store.save_plan({"name": "Plan A", "method": "plough", "rpl_id": "rplA",
                     "rpl_name": "Route A", "rpl_revision": "R1",
                     "rpl_gpkg_path": wb_a})
    store.close()
    return os.path.join(folder_b, "B.qgs"), wb_b, wb_a


def test_workbench_entry_not_rewritten(root: str) -> bool:
    project = QgsProject.instance()
    checks = {}
    with _Env(root) as env:
        # Dock open while B is opened.
        b_path, wb_b, _wb_a = _workbench_projects(os.path.join(root, "open"))
        dock = env.dock()
        loaded = dock.model.plan.get("name")
        project.read(b_path)
        _pump(0.4)
        checks["open"] = (wb_store_module.project_gpkg_path() == wb_b,
                          store_module.project_gpkg_path() is None,
                          not os.path.exists(os.path.join(
                              os.path.dirname(b_path), "B_burial_plans.gpkg")),
                          not _plan_layer_names(project),
                          dock.model.plan_id == "")

        # Dock closed during the switch, reopened in B.
        b_path, wb_b, _wb_a = _workbench_projects(os.path.join(root, "closed"))
        dock = env.dock()
        dock.close()
        project.read(b_path)
        QCoreApplication.sendEvent(dock, QShowEvent())
        dock.refresh()  # the plugin's show_burial_planner()
        _pump(0.4)
        checks["closed"] = (wb_store_module.project_gpkg_path() == wb_b,
                            store_module.project_gpkg_path() is None,
                            not project.isDirty(),
                            not _plan_layer_names(project))

        # Project > New, then the toolbar button.
        _workbench_projects(os.path.join(root, "new"))
        iface = _Iface()
        dock = env.dock(iface)
        project.clear()
        iface.newProjectCreated.emit()
        dock.refresh()
        _pump(0.3)
        checks["new"] = (wb_store_module.project_gpkg_path() is None,
                         not project.isDirty())
    ok = loaded == "Plan A" and all(all(v) for v in checks.values())
    return _result("the next project's Workbench entry is never rewritten "
                   "from the previous project's plan", ok, str(checks))


def test_markers_gone_after_open(root: str) -> bool:
    project = QgsProject.instance()
    with _Env(root) as env:
        folder_b = os.path.join(root, "B")
        os.makedirs(folder_b)
        project.clear()
        project.setFileName(os.path.join(folder_b, "B.qgs"))
        project.write()
        _gpkg, route = _project_with_plan(os.path.join(root, "A"))
        iface = _Iface()
        dock = env.dock(iface)
        _set_route(dock, route)
        _draw_everything(dock)
        canvas = iface.mapCanvas()
        before = _canvas_items(canvas)
        project.read(os.path.join(folder_b, "B.qgs"))
        _pump(0.3)
        after = _canvas_items(canvas)
        tool = canvas.mapTool()
        ok = before == 4 and after == 0 and not isinstance(tool, KpPickTool)
        return _result("map markers, bands and the pick tool are gone after "
                       "opening another project", ok,
                       f"before={before} after={after}")


def test_failed_reopen_writes_nothing(root: str) -> bool:
    project = QgsProject.instance()
    with _Env(root) as env:
        folder_b = os.path.join(root, "B")
        os.makedirs(folder_b)
        project.clear()
        project.setFileName(os.path.join(folder_b, "B.qgs"))
        store_module.set_project_gpkg_path(
            os.path.join(root, "elsewhere", "lost_plans.gpkg"))
        project.write()
        gpkg, _route = _project_with_plan(os.path.join(root, "A"))
        dock = env.dock()
        project.read(os.path.join(folder_b, "B.qgs"))
        _pump(0.3)
        warned = len(env.warnings)
        state = (dock.store_ready, dock.store.detached, _open(gpkg),
                 dock.workflow_tabs.isEnabled(), dock.model.tools)
        dock.model.save_tool({"name": "Probe plough", "tool_type": "plough"})
        dock.refresh()  # reopening the window: no second warning
        _pump(0.2)
        ok = state == (False, True, False, False, [])
        ok = ok and not _read(gpkg, BurialStore.list_tools) and not _open(gpkg)
        ok = ok and warned >= 1 and len(env.warnings) == warned + 1
        # (+1: the refused tool write is reported, the reopen is not)
        ok = ok and "lost_plans.gpkg" in dock.gpkg_name.text()
        return _result("a plan file that fails to reopen leaves the dock "
                       "inert: nothing reaches the previous project's file",
                       ok, f"state={state} warnings={env.warnings}")


def test_moved_project_no_duplicate_layers(root: str) -> bool:
    project = QgsProject.instance()
    with _Env(root) as env:
        folder_x = os.path.join(root, "X")
        _gpkg, route = _project_with_plan(folder_x)
        try:
            from qgis.core import Qgis

            project.setFilePathStorage(Qgis.FilePathType.Absolute)
        except (AttributeError, ImportError):
            project.writeEntryBool("Paths", "/Absolute", True)
        dock = env.dock()
        _set_route(dock, route)
        dock.model.add_event(0.5, "PLDN")
        dock.model.flush_layer_refresh()
        names = _plan_layer_names(project)
        project.write()
        project.clear()
        folder_y = os.path.join(root, "Y")
        shutil.copytree(folder_x, folder_y)
        _rewrite_paths(os.path.join(folder_y, "A.qgs"), folder_x,
                       os.path.join(root, "gone"))
        project.read(os.path.join(folder_y, "A.qgs"))
        _pump(0.3)
        after_read = [(layer.name(), layer.isValid())
                      for layer in project.mapLayers().values()
                      if layer.name().startswith("bp_")]
        map_layers.restore_burial_layers()  # the plugin's projectRead hook
        after_repair = _plan_layer_names(project)
        ok = len(names) >= 2 and sorted(n for n, _v in after_read) == names
        ok = ok and all(valid for _n, valid in after_read)
        ok = ok and after_repair == names
        ok = ok and dock.model.plan.get("name") == "Plan A"
        return _result("opening a moved project repoints its plan layers "
                       "instead of adding duplicates", ok,
                       f"before={names} after_read={after_read}")


def test_reopen_same_project_restores_plan(root: str) -> bool:
    project = QgsProject.instance()
    with _Env(root) as env:
        folder = os.path.join(root, "A")
        gpkg, route = _project_with_plan(folder)
        dock = env.dock()
        _set_route(dock, route)
        dock.model.add_event(0.5, "PLDN")
        dock.model.flush_layer_refresh()
        second = PlanModel(dock.store).create_plan("Plan Z", "plough")
        dock.refresh_plans(second)
        names = _plan_layer_names(project)
        project.write()
        project.read(os.path.join(folder, "A.qgs"))
        _pump(0.3)
        ok = dock.model.plan_id == second and dock.store_ready
        ok = ok and _plan_layer_names(project) == names and _open(gpkg)
        return _result("reopening the same project reselects the plan that "
                       "was open, without duplicating layers", ok,
                       f"plan={dock.model.plan.get('name')} "
                       f"layers={_plan_layer_names(project)}")


def test_unused_project_gets_nothing_until_used(root: str) -> bool:
    project = QgsProject.instance()
    with _Env(root) as env:
        # The shared plan file of unsaved projects already holds a plan.
        shared = os.path.join(env.unsaved, "project_burial_plans.gpkg")
        store = BurialStore(shared)
        store.migrate()
        plan_id = PlanModel(store).create_plan("Shared", "plough")
        store.close()
        gpkg, _route = _project_with_plan(os.path.join(root, "A"))
        iface = _Iface()
        dock = env.dock(iface)
        project.clear()
        iface.newProjectCreated.emit()
        _pump(0.3)
        listed = (dock.store_ready, dock.plan_combo.count(),
                  dock.plan_combo.currentIndex(), dock.model.plan_id,
                  store_module.project_gpkg_path(), project.isDirty(),
                  len(project.mapLayers()))
        # The user picks the plan: from now on the project uses the tool.
        dock.plan_combo.setCurrentIndex(0)
        _pump(0.2)
        used = (dock.model.plan_id == plan_id,
                store_module.project_gpkg_path() == shared)
        ok = listed == (True, 1, -1, "", None, False, 0) and all(used)
        ok = ok and not _open(gpkg)
        return _result("a project that never used the tool gets its plans "
                       "listed but no plan, layers or entries until the user "
                       "picks one", ok, f"listed={listed} used={used}")


def test_hooks_follow_close_and_shutdown(root: str) -> bool:
    project = QgsProject.instance()
    with _Env(root) as env:
        gpkg, _route = _project_with_plan(os.path.join(root, "A"))
        iface = _Iface()
        dock = env.dock(iface)
        hooks = len(dock._lifecycle_hooks)
        # Closed window: the project switch is still followed (plan dropped).
        dock.close()
        project.clear()
        closed = (dock.model.plan_id, dock.store.detached, _open(gpkg))
        QCoreApplication.sendEvent(dock, QShowEvent())
        dock.refresh()
        _pump(0.2)
        # Unload: no hook is left behind.
        calls = []
        dock.refresh = lambda: calls.append("refresh")
        dock.shutdown()
        project.clear()
        iface.newProjectCreated.emit()
        _pump(0.2)
        ok = hooks == 2 and closed == ("", True, False)
        ok = ok and not dock._lifecycle_hooks and not calls
        return _result("project switches are followed while the window is "
                       "closed; shutdown releases the hooks", ok,
                       f"hooks={hooks} closed={closed} calls={calls}")


def run_all() -> list:
    results = []
    old_format = QSettings.defaultFormat()
    root = tempfile.mkdtemp(prefix="bp_project_switch_")
    settings = os.path.join(root, "settings")
    os.makedirs(settings)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      settings)
    project = QgsProject.instance()
    saved_name = project.fileName()
    try:
        for index, test in enumerate((
                test_new_project_releases_everything,
                test_workbench_entry_not_rewritten,
                test_markers_gone_after_open,
                test_failed_reopen_writes_nothing,
                test_moved_project_no_duplicate_layers,
                test_reopen_same_project_restores_plan,
                test_unused_project_gets_nothing_until_used,
                test_hooks_follow_close_and_shutdown)):
            folder = os.path.join(root, f"t{index}")
            os.makedirs(folder)
            try:
                results.append(test(folder))
            except Exception as exc:  # report, keep going
                import traceback
                traceback.print_exc()
                results.append(_result(test.__name__, False, repr(exc)))
    finally:
        QSettings.setDefaultFormat(old_format)
        project.clear()
        project.setFileName(saved_name)
        gpkg_sql.close()
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
