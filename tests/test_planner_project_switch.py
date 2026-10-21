# -*- coding: utf-8 -*-
"""Planner and Span & Slack Analysis across project changes (requires the QGIS API).

QGIS keeps plugin map graphics, map tools and timers when a project is
closed, so the Planner drops everything tied to the old project on
``aboutToBeCleared`` (New, Close and the start of an Open) and binds to the
next project's planner file once that project is ready. These checks drive
the real ``QgsProject.clear()`` / ``read()``:

* opening the Planner in a project that never used it adds no GeoPackage,
  layers or project entry; the first scenario adopts the file;
* Project > New: playback stops, the map overlay and an active sketch go,
  the canvas snapping is the new project's, nothing more is written into the
  previous project's file and none of its layers reach the new project;
* opening another project shows its plan and layers (no duplicates); with
  the dock closed it follows the project when shown again;
* after Project > Close the dock writes nothing to the closed project's file;
* an unused project only lists the shared file of unsaved projects: no
  layers, entry or dirty flag;
* a sketch ends with the project's current snapping settings;
* Span & Slack Analysis lists the new project's Workbench RPLs after New;
* shutdown() releases the project hooks.

QSettings are redirected to a temporary folder; dialogs are stubbed.
"""

from __future__ import annotations

import os
import tempfile
import time
import traceback
from typing import List

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSnappingConfig,
    QgsTolerance,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas, QgsMapToolPan, QgsMessageBar
from qgis.PyQt.QtCore import QCoreApplication, QObject, QPoint, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QInputDialog, QMainWindow, QMessageBox

from ..planner import schema as planner_schema
from ..planner.planner_dock import PlannerDock
from ..planner.sketch_tool import SketchSession
from ..planner.store import PlannerStore, project_gpkg_path
from ..processing.cable_lay_parsers import open_gpkg_layer
from ..qgis_compat import QAction

REQUIRES_QGIS = True

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def _pump(seconds: float = 0.0) -> None:
    end = time.monotonic() + seconds
    while True:
        QCoreApplication.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.01)


def _wait(condition, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            return False
        _pump()
        time.sleep(0.01)
    return True


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path or "")))


def _uses(layer, gpkg) -> bool:
    return _norm(gpkg) in _norm(layer.source().split("|")[0])


def _planner_layers(project):
    return [layer for layer in project.mapLayers().values()
            if "|layername=pow_task_" in layer.source()]


def _content(path):
    """Scenario names, task rows and task points stored in a planner file."""
    store = PlannerStore(path)
    names = sorted(str(row.get("name")) for row in store.list_scenarios())
    tasks = len(store.read_table(planner_schema.TABLE_TASK))
    points = open_gpkg_layer(path, planner_schema.TABLE_TASK_POINT)
    return names, tasks, points.featureCount() if points is not None else 0


def _combo_texts(combo):
    return [combo.itemText(i) for i in range(combo.count())]


class _Iface(QObject):
    """The parts of QgisInterface the Planner and Span & Slack Analysis use."""

    newProjectCreated = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.window = QMainWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.canvas = QgsMapCanvas(self.window)
        self.canvas.setDestinationCrs(WGS84)
        self.window.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar(self.window)
        self.pan = QgsMapToolPan(self.canvas)
        self.pan_action = QAction("Pan", self.window)
        self.pan_action.triggered.connect(lambda: self.canvas.setMapTool(self.pan))

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def actionPan(self):
        return self.pan_action


class _Stubs:
    """Stub the message boxes and scenario-name prompt; unsaved projects
    default to ``unsaved`` instead of the QGIS profile."""

    def __init__(self, unsaved: str):
        self.unsaved = unsaved
        self.names: List[str] = []

    def __enter__(self):
        self._boxes = {name: getattr(QMessageBox, name)
                       for name in ("warning", "information", "question", "critical")}
        for name in self._boxes:
            setattr(QMessageBox, name, staticmethod(lambda *a, **k: 0))
        self._get_text = QInputDialog.getText
        QInputDialog.getText = staticmethod(lambda *a, **k: (self.names.pop(0), True))
        self._folder_for = planner_schema.gpkg_folder_for
        planner_schema.gpkg_folder_for = (
            lambda path: os.path.dirname(path) if path else self.unsaved)
        return self

    def __exit__(self, *_exc):
        for name, value in self._boxes.items():
            setattr(QMessageBox, name, value)
        QInputDialog.getText = self._get_text
        planner_schema.gpkg_folder_for = self._folder_for
        return False


def _new_dock(iface):
    dock = PlannerDock(iface)
    iface.window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    iface.window.show()
    dock.show()
    _pump()
    return dock


def _add_point_task(dock, x, y):
    dock.task_table.add_task()
    _pump()
    task_id = dock.task_table.rows[-1]["task_id"]
    dock._apply_owned_geometry(
        task_id, QgsGeometry.fromPointXY(QgsPointXY(x, y)), "point", "drawn", {})
    _pump()


def _set_advanced_snapping(project, layer, tolerance):
    config = QgsSnappingConfig(project.snappingConfig())
    config.setEnabled(True)
    config.setMode(Qgis.SnappingMode.AdvancedConfiguration)
    config.setTolerance(tolerance)
    config.setIndividualLayerSettings(layer, QgsSnappingConfig.IndividualLayerSettings(
        True, Qgis.SnappingType.Vertex | Qgis.SnappingType.Segment, 15.0,
        QgsTolerance.UnitType.Pixels))
    project.setSnappingConfig(config)


def test_planner_follows_project(folder: str) -> List[bool]:
    out: List[bool] = []
    project = QgsProject.instance()
    project.clear()
    unsaved = os.path.join(folder, "unsaved")
    os.makedirs(unsaved)
    with _Stubs(unsaved) as stubs:
        iface = _Iface()
        canvas = iface.canvas
        # As the QGIS application does: the canvas snaps with the project's settings.
        project.snappingConfigChanged.connect(canvas.snappingUtils().setConfig)
        a_qgs = os.path.join(folder, "a", "plan_a.qgs")
        a_gpkg = os.path.join(folder, "a", "plan_a_planner.gpkg")
        os.makedirs(os.path.dirname(a_qgs))
        project.setFileName(a_qgs)
        project.setCrs(WGS84)
        dock = _new_dock(iface)
        try:
            out.append(_result(
                "opening the Planner in an unused project adds no file, layers or entry",
                not os.path.exists(a_gpkg) and project_gpkg_path() is None
                and not _planner_layers(project) and dock.scenario_combo.count() == 0))

            stubs.names.append("A plan")
            dock._new_scenario()
            _pump()
            out.append(_result(
                "the first scenario adopts the file: project entry and two task layers",
                os.path.exists(a_gpkg) and _norm(project_gpkg_path()) == _norm(a_gpkg)
                and len(_planner_layers(project)) == 2
                and dock.scenario_combo.currentText() == "A plan"))

            _add_point_task(dock, 1.0, 50.0)
            snap_layer = QgsVectorLayer("LineString?crs=EPSG:4326", "snap lines", "memory")
            project.addMapLayer(snap_layer)
            _set_advanced_snapping(project, snap_layer, 33.0)
            project.write(a_qgs)
            a_before = _content(a_gpkg)

            dock.sim.seek_fraction(0.5)
            dock.play_btn.setChecked(True)
            _pump(0.3)
            dock._sketch_tasks()
            tool = dock.sketch_session.tool
            tool.add_map_point(QgsPointXY(0.0, 0.0))
            tool.add_map_point(QgsPointXY(1.0, 1.0))
            # The Planner's own canvas items (QGIS adds and removes its own,
            # e.g. the short-lived highlight of a selected task).
            planner_items = [item for items in dock.overlay._items.values() for item in items]
            planner_items += [tool.point_band, tool.line_band, tool.motion_band]
            out.append(_result(
                "project A: playing, overlay drawn, sketch active",
                a_before == (["A plan"], 1, 1) and dock.sim.is_playing()
                and len(dock.overlay._items) == 1 and canvas.mapTool() is tool
                and all(item.scene() is canvas.scene() for item in planner_items), str(a_before)))

            # -- Project > New ---------------------------------------------------
            project.clear()
            iface.newProjectCreated.emit()
            _pump(0.3)
            left = [type(item).__name__ for item in planner_items if item.scene() is not None]
            out.append(_result(
                "New: overlay and sketch graphics gone, playback stopped, sketch ended",
                not left and not dock.overlay._items
                and not dock.sim.timer.isActive() and dock.sketch_session.tool is None
                and dock.sketch_session.control is None and canvas.mapTool() is not tool,
                f"left on the map {left}, playing {dock.sim.timer.isActive()}, "
                f"sketch {dock.sketch_session.tool is not None}"))
            config = canvas.snappingUtils().config()
            out.append(_result(
                "New: the canvas snaps with the new project's settings",
                config.tolerance() == project.snappingConfig().tolerance() != 33.0
                and config.mode() == project.snappingConfig().mode(),
                f"canvas {config.tolerance()} / project {project.snappingConfig().tolerance()}"))
            canvas.snappingUtils().snapToMap(QPoint(5, 5))   # no deleted layer left to reach
            out.append(_result(
                "New: nothing of project A is listed, added or recorded",
                dock.scenario_combo.count() == 0 and not _planner_layers(project)
                and project_gpkg_path() is None and _norm(dock.store.gpkg_path) != _norm(a_gpkg)))

            stubs.names.append("New plan")
            dock._new_scenario()
            _add_point_task(dock, 2.0, 51.0)
            shared = dock.store.gpkg_path
            layers = _planner_layers(project)
            out.append(_result(
                "New: work goes to the new project's file; project A's file is unchanged",
                _content(a_gpkg) == a_before and _content(shared) == (["New plan"], 1, 1)
                and _norm(os.path.dirname(shared)) == _norm(unsaved)
                and len(layers) == 2 and not any(_uses(layer, a_gpkg) for layer in layers)
                and _norm(project_gpkg_path()) == _norm(shared)))
            b_qgs = os.path.join(folder, "b", "plan_b.qgs")
            os.makedirs(os.path.dirname(b_qgs))
            project.write(b_qgs)

            # -- Open A (dock shown) ---------------------------------------------
            project.read(a_qgs)
            _pump()
            layers = _planner_layers(project)
            out.append(_result(
                "Open A: its plan and its two task layers, no duplicates",
                dock.scenario_combo.currentText() == "A plan"
                and _norm(dock.store.gpkg_path) == _norm(a_gpkg)
                and len(layers) == 2 and all(_uses(layer, a_gpkg) for layer in layers),
                str([layer.source() for layer in layers])))

            # -- Open B with the dock closed, then show it ---------------------
            dock.close()
            _pump()
            project.read(b_qgs)
            _pump()
            waiting = dock._project_stale and dock.scenario_combo.count() == 0
            dock.show()
            dock.refresh()   # what the plugin does when the Planner is opened again
            _pump()
            out.append(_result(
                "Open B with the Planner closed: it follows B when shown again",
                waiting and dock.scenario_combo.currentText() == "New plan"
                and _norm(dock.store.gpkg_path) == _norm(shared)
                and _content(a_gpkg) == a_before))

            # -- Project > Close with the dock open ----------------------------
            shared_before = _content(shared)
            dock.play_btn.setChecked(True)
            project.clear()
            _pump()
            refused = False
            try:
                dock.store.save_scenario({"name": "written after close"})
            except RuntimeError:
                refused = True
            dock.task_table.add_task()
            _pump()
            out.append(_result(
                "Close: the dock empties and writes nothing to the closed project's file",
                refused and dock.scenario_combo.count() == 0 and not dock.sim.timer.isActive()
                and _content(shared) == shared_before))

            dock.shutdown()
            dock._project_stale = False
            project.clear()
            out.append(_result("shutdown() releases the project hooks",
                               not dock._project_hooks and not dock._project_stale))
        finally:
            dock.shutdown()
            project.snappingConfigChanged.disconnect(canvas.snappingUtils().setConfig)
            project.clear()
            dock.deleteLater()
            _pump()
    return out


def test_unused_project_gets_nothing(folder: str) -> List[bool]:
    out: List[bool] = []
    project = QgsProject.instance()
    project.clear()
    unsaved = os.path.join(folder, "unsaved")
    os.makedirs(unsaved)
    shared = os.path.join(unsaved, "project_planner.gpkg")
    earlier = PlannerStore(shared)   # an earlier unsaved project's plan
    earlier.migrate()
    earlier.create_scenario("Earlier plan", "2026-01-01T00:00")
    before = _content(shared)
    with _Stubs(unsaved):
        iface = _Iface()
        project.setDirty(False)
        dock = _new_dock(iface)
        try:
            out.append(_result(
                "an unsaved, unused project lists the shared file and adds nothing",
                dock.scenario_combo.currentText() == "Earlier plan" and not _planner_layers(project)
                and project_gpkg_path() is None and not project.isDirty()
                and _content(shared) == before))

            project.clear()
            c_qgs = os.path.join(folder, "c", "plan_c.qgs")
            os.makedirs(os.path.dirname(c_qgs))
            project.setFileName(c_qgs)
            project.setDirty(False)
            iface.newProjectCreated.emit()
            _pump()
            out.append(_result(
                "a saved, unused project gets no file, layers, entry or dirty flag",
                dock.scenario_combo.count() == 0 and not _planner_layers(project)
                and project_gpkg_path() is None and not project.isDirty()
                and not os.path.exists(os.path.join(folder, "c", "plan_c_planner.gpkg"))))
        finally:
            dock.shutdown()
            project.clear()
            dock.deleteLater()
            _pump()
    return out


def test_sketch_restores_current_snapping() -> List[bool]:
    project = QgsProject.instance()
    project.clear()
    canvas = QgsMapCanvas()
    session = SketchSession(canvas)
    tolerance = None
    try:
        session.start("line", lambda _points: None)
        config = QgsSnappingConfig(project.snappingConfig())
        config.setTolerance(21.0)
        project.setSnappingConfig(config)   # changed while sketching
        session.cancel(False)
        tolerance = canvas.snappingUtils().config().tolerance()
    finally:
        session.cancel(False)
        project.clear()
    return [_result("a sketch ends with the project's current snapping settings",
                    tolerance == 21.0, f"tolerance {tolerance}")]


def test_span_lists_after_new(folder: str) -> List[bool]:
    from ..span_analysis import dialog as span_dialog
    from ..workbench.store import WorkbenchStore
    from ..workbench.store import set_project_gpkg_path as set_workbench_path

    out: List[bool] = []
    project = QgsProject.instance()
    project.clear()
    a_qgs = os.path.join(folder, "a", "span_a.qgs")
    os.makedirs(os.path.dirname(a_qgs))
    project.setFileName(a_qgs)
    workbench = WorkbenchStore(os.path.join(folder, "a", "span_a_workbench.gpkg"))
    workbench.ensure_created()
    workbench.save_rpl({"rpl_id": "rpl-a", "name": "Route A"})
    set_workbench_path(workbench.gpkg_path)
    iface = _Iface()
    dialog = span_dialog.SpanAnalysisDialog(None, iface=iface)
    dialog._persist_ui = False
    dialog.plot.persist = False   # never write the tester's QSettings
    try:
        listed_a = _combo_texts(dialog.setup.rpl_combo)
        project.clear()
        dropped = dialog.setup.workbench is None
        # Project > New, named in the test folder so no profile file is read.
        new_qgs = os.path.join(folder, "new", "new.qgs")
        os.makedirs(os.path.dirname(new_qgs))
        project.setFileName(new_qgs)
        iface.newProjectCreated.emit()
        followed = _wait(lambda: not dialog._project_timer.isActive())
        listed_new = _combo_texts(dialog.setup.rpl_combo)
        out.append(_result("Span: project A's RPLs are listed",
                           any("Route A" in text for text in listed_a), str(listed_a)))
        out.append(_result("Span: closing the project drops its Workbench", dropped))
        out.append(_result(
            "Span: after New the RPL list is the new project's",
            followed and dialog.setup.workbench is None
            and not any("Route A" in text for text in listed_new), str(listed_new)))
    finally:
        dialog.shutdown()
        dialog.deleteLater()
        project.clear()
        _pump()
    return out


def run_all() -> List[bool]:
    results: List[bool] = []
    old_format = QSettings.defaultFormat()
    temp = tempfile.mkdtemp(prefix="sct_project_switch_")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
    try:
        for test in (test_sketch_restores_current_snapping,
                     lambda: test_planner_follows_project(tempfile.mkdtemp(dir=temp)),
                     lambda: test_unused_project_gets_nothing(tempfile.mkdtemp(dir=temp)),
                     lambda: test_span_lists_after_new(tempfile.mkdtemp(dir=temp))):
            try:
                results.extend(test())
            except Exception as exc:  # report, keep going
                traceback.print_exc()
                results.append(_result(getattr(test, "__name__", "test"), False, repr(exc)))
    finally:
        QSettings.setDefaultFormat(old_format)
        QgsProject.instance().clear()
    print(f"\n{sum(results)}/{len(results)} project-switch checks passed.")
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
