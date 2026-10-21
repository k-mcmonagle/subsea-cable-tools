# -*- coding: utf-8 -*-
"""Cable Lay Data Explorer across project changes (New, Open, Close).

Checked with the real ``QgsProject.instance().clear()`` / ``read()`` and a
stub iface carrying ``projectRead`` / ``newProjectCreated``:

* the map highlights (record marker, span band, hover marker) leave the
  canvas, and the datasets, table, QC findings and running Lay Assessment
  are dropped, as soon as the project is cleared; hovering afterwards draws
  nothing;
* Close leaves the Explorer without a data file (nothing can write to the
  old one) and the project untouched;
* the Lay Assessment's cable-type mapping follows the project, and a change
  is merged into the mapping as the project holds it now;
* reopening the project reloads the layers the Explorer had, and the saved
  session state keeps them while nothing is loaded;
* shutdown() disconnects every project hook.

QSettings are redirected to a temporary folder.
"""

from __future__ import annotations

import json
import os
import tempfile

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QCoreApplication, QObject, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QComboBox, QMainWindow

from ..cable_library import store as library
from ..processing.cable_lay_parsers import WKT_KEY, fields_from_specs, write_layer_to_gpkg
from ..qgis_compat import WKB_POINT

REQUIRES_QGIS = True

_LAY_SPECS = [("kp", "float"), ("tension", "float"), ("cable_type", "str")]


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _pump():
    for _ in range(3):
        QCoreApplication.processEvents()


class _Iface(QObject):
    projectRead = pyqtSignal()
    newProjectCreated = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.window = QMainWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)

    def mapCanvas(self):
        return self.canvas

    def mainWindow(self):
        return self.window


def _lay_gpkg(folder) -> str:
    path = os.path.join(folder, "lay.gpkg")
    rows = [{"kp": i * 0.1, "tension": 10.0 + i, "cable_type": "LW",
             WKT_KEY: f"POINT ({1.0 + i * 0.01} 50.0)"} for i in range(20)]
    write_layer_to_gpkg(path, "lay_data", fields_from_specs(_LAY_SPECS), WKB_POINT, rows,
                        QgsProject.instance().transformContext())
    return path


def _save_project(path, mapping, gpkg=None):
    """Write a project holding ``mapping`` (and the lay layer); return the
    path and the lay layer's id."""
    project = QgsProject.instance()
    project.clear()
    project.writeEntry(library.PROJECT_SCOPE, library.PROJECT_MAPPING_KEY, json.dumps(mapping))
    layer_id = None
    if gpkg:
        layer = QgsVectorLayer(f"{gpkg}|layername=lay_data", "lay A", "ogr")
        project.addMapLayer(layer)
        layer_id = layer.id()
    if not project.write(path):
        raise RuntimeError(f"could not write {path}: {project.error()}")
    project.clear()
    return path, layer_id


def _open(path, iface):
    project = QgsProject.instance()
    if not project.read(path):
        raise RuntimeError(f"could not read {path}: {project.error()}")
    iface.projectRead.emit()
    _pump()


def _window(iface):
    from ..explorer import CableLayExplorerWindow

    window = CableLayExplorerWindow(iface, iface.window)
    window.assessment_panel.persist = True  # the project mapping is under test
    # Load on this thread (no progress dialog or task manager in a test).
    window._start_background_load = lambda to_load, wanted: (
        window._load_layers_sync(to_load), window._apply_loaded_order(wanted))
    return window


def _load(window, layer_id):
    window.set_loaded_layers([layer_id])
    _pump()


def _items(canvas) -> int:
    return len([item for item in canvas.scene().items() if item.isVisible()])


def _teardown(window):
    window.shutdown()
    window.deleteLater()
    QgsProject.instance().clear()
    _pump()


# -- tests ---------------------------------------------------------------------
def test_new_project_drops_highlights_and_data(folder) -> bool:
    gpkg = _lay_gpkg(tempfile.mkdtemp(dir=folder))
    path_a, layer_id = _save_project(os.path.join(folder, "A_new.qgs"), {"LW": "TypeA"}, gpkg)
    iface = _Iface()
    _open(path_a, iface)
    window = _window(iface)
    canvas = iface.canvas

    class _Task:
        def cancel(self):
            pass

    try:
        base = _items(canvas)
        _load(window, layer_id)
        ok = window.dataset is not None and window.dataset.row_count == 20
        window.focus_record(3)
        window.select_rows([5, 9])
        window.broadcast_hover(7)
        ok = ok and _items(canvas) > base
        window.assessment_panel._task = _Task()  # a run in flight
        window.qc_panel._findings = ["finding"]
        run_id = window.assessment_panel._run_id

        QgsProject.instance().clear()
        iface.newProjectCreated.emit()
        _pump()
        ok = ok and _items(canvas) == base
        ok = ok and window.dataset is None and not window.loaded_layer_ids()
        ok = ok and window.table_panel.model.rowCount() == 0
        ok = ok and not window.qc_panel._findings
        ok = ok and window.assessment_panel._task is None
        ok = ok and window.assessment_panel._run_id > run_id
        ok = ok and window.assessment_panel._type_mapping == {}
        window._last_hover_row = None
        window.broadcast_hover(4)  # the mouse over the (now empty) plot
        ok = ok and _items(canvas) == base
        ok = ok and not QgsProject.instance().isDirty()
        detail = f"visible items {_items(canvas)} vs {base}"
    finally:
        _teardown(window)
    return _result("New project: highlights gone, data and run dropped", ok, detail)


def test_close_project_leaves_no_data_file(folder) -> bool:
    gpkg = _lay_gpkg(tempfile.mkdtemp(dir=folder))
    path_a, layer_id = _save_project(os.path.join(folder, "A_close.qgs"), {}, gpkg)
    iface = _Iface()
    _open(path_a, iface)
    window = _window(iface)
    try:
        _load(window, layer_id)
        window.project_panel._runner.run_async = False  # scan inline
        window.project_panel.set_path(gpkg, remember=False)
        ok = window.project_path() is not None
        QgsProject.instance().clear()  # Project > Close: no follow-up signal
        _pump()
        ok = ok and window.project_path() is None and window.gpkg_path() is None
        ok = ok and window.dataset is None
        ok = ok and not window.project_panel._refresh_timer.isActive()
        ok = ok and not QgsProject.instance().isDirty()
        ok = ok and library.project_mapping() == {}
    finally:
        _teardown(window)
    return _result("Close project: no data file, nothing scheduled", ok)


def test_mapping_follows_the_project(folder) -> bool:
    path_a, _ = _save_project(os.path.join(folder, "A_map.qgs"), {"LW": "TypeA"})
    path_b, _ = _save_project(os.path.join(folder, "B_map.qgs"), {"LW": "TypeB"})
    iface = _Iface()
    _open(path_a, iface)
    window = _window(iface)
    panel = window.assessment_panel
    try:
        ok = panel._type_mapping == {"LW": "TypeA"}
        _open(path_b, iface)
        ok = ok and panel._type_mapping == {"LW": "TypeB"}
        # Another tool maps SA meanwhile; this change must keep it.
        library.set_project_mapping({"LW": "TypeB", "SA": "TypeS"})
        combo = QComboBox()
        combo.addItem("TypeX", "TypeX")
        panel._on_mapping_changed("DA", combo)
        saved = library.project_mapping()
        ok = ok and saved == {"DA": "TypeX", "LW": "TypeB", "SA": "TypeS"}
        ok = ok and panel._type_mapping == saved
        detail = json.dumps(saved, sort_keys=True)
    finally:
        _teardown(window)
    return _result("Lay Assessment cable-type mapping follows the project", ok, detail)


def test_reopen_reloads_and_session_state_kept(folder) -> bool:
    gpkg = _lay_gpkg(tempfile.mkdtemp(dir=folder))
    path_a, layer_id = _save_project(os.path.join(folder, "A_reopen.qgs"), {}, gpkg)
    iface = _Iface()
    _open(path_a, iface)
    window = _window(iface)
    try:
        _load(window, layer_id)
        QgsProject.instance().clear()
        _pump()
        window._save_state()  # QGIS exit: the project is cleared before unload
        saved = json.loads(window.settings.value("SubseaCableTools/CableLayExplorer/loaded_layers"))
        ok = saved == [layer_id] and window.dataset is None
        _open(path_a, iface)  # window not on screen: reloaded when next shown
        ok = ok and window.dataset is None and window._restore_on_show
        window._restore_released_layers()  # what showing the window does
        _pump()
        ok = ok and window.loaded_layer_ids() == [layer_id] and window.dataset is not None
    finally:
        _teardown(window)
    return _result("reopened project reloads the Explorer's layers", ok)


def test_shutdown_disconnects(folder) -> bool:
    gpkg = _lay_gpkg(tempfile.mkdtemp(dir=folder))
    path_a, layer_id = _save_project(os.path.join(folder, "A_shutdown.qgs"), {}, gpkg)
    iface = _Iface()
    _open(path_a, iface)
    window = _window(iface)
    try:
        _load(window, layer_id)
        window.shutdown()
        calls = []
        window.assessment_panel.project_ready = lambda: calls.append("ready")
        QgsProject.instance().clear()
        iface.projectRead.emit()
        iface.newProjectCreated.emit()
        _pump()
        ok = window.loaded_layer_ids() == [layer_id] and not calls
        ok = ok and window.project_panel._connected_project is None
    finally:
        window.deleteLater()
        QgsProject.instance().clear()
        _pump()
    return _result("shutdown() disconnects the project hooks", ok)


def run_all():
    results = []
    old_format = QSettings.defaultFormat()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        folder = tempfile.mkdtemp(prefix="explorer_switch_")  # GeoPackages: never auto-deleted
        try:
            for test in (lambda: test_new_project_drops_highlights_and_data(folder),
                         lambda: test_close_project_leaves_no_data_file(folder),
                         lambda: test_mapping_follows_the_project(folder),
                         lambda: test_reopen_reloads_and_session_state_kept(folder),
                         lambda: test_shutdown_disconnects(folder)):
                try:
                    results.append(test())
                except Exception as exc:  # report, keep going
                    import traceback
                    traceback.print_exc()
                    results.append(_result(getattr(test, "__name__", "test"), False, repr(exc)))
        finally:
            QSettings.setDefaultFormat(old_format)
            QgsProject.instance().clear()
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
