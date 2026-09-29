# -*- coding: utf-8 -*-
"""Plugin shell lifecycle: initGui() -> open tools -> unload() leaves nothing behind.

The plugin runs against a stub iface backed by real Qt objects (main window,
map canvas, Plugins toolbar, plugin menus, dock area), so the checks see what
QGIS would see: toolbar/menu entries, the layer-tree action, the processing
provider, projectRead hooks, the active map tool, the translator, and the
QObjects the plugin parents to the main window. Heavy tools (Workbench,
Planner, Burial Planner, lay simulator dialogs) are stood in for by small
fakes that record the teardown calls unload() makes.

Also covers the package bootstrap (vendored lib/ must not shadow host
modules), the tool icons and the "tool failed to open" path.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py). QSettings are
redirected to a temporary folder, so nothing persists to the tester's profile.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import logging
import os
import runpy
import struct
import sys
import tempfile
from contextlib import contextmanager
from typing import List

from qgis.core import QgsApplication
from qgis.gui import QgsMapCanvas, QgsMapToolPan, QgsMessageBar
from qgis.PyQt.QtCore import QCoreApplication, QEvent, QObject, QSettings, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QApplication, QDialog, QDockWidget, QMainWindow, QMenu, QWidget

from .. import plugin_log
from ..qgis_compat import QAction, is_deleted

REQUIRES_QGIS = True

_PACKAGE = __name__.rsplit(".tests.", 1)[0]
_PROVIDER_ID = "subsea_cable_processing"


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _flush_deletes():
    """Run pending deleteLater() calls (no event loop runs in the tests)."""
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


# -- stub iface ----------------------------------------------------------------
class _Iface(QObject):
    """The parts of QgisInterface the plugin shell uses, on real widgets."""

    projectRead = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.window = QMainWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar(self.window)
        self.toolbar = self.window.addToolBar("Plugins")
        self.plugin_menus = {}
        self.layer_tree_actions = []

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def layerTreeView(self):
        return None

    def addToolBarIcon(self, action):
        self.toolbar.addAction(action)

    def addToolBarWidget(self, widget):
        return self.toolbar.addWidget(widget)

    def removeToolBarIcon(self, action):
        self.toolbar.removeAction(action)

    def addPluginToMenu(self, name, action):
        menu = self.plugin_menus.get(name)
        if menu is None:
            menu = self.plugin_menus[name] = QMenu(name, self.window)
        menu.addAction(action)

    def removePluginMenu(self, name, action):
        menu = self.plugin_menus.get(name)
        if menu is None:
            return
        menu.removeAction(action)
        if menu.isEmpty():  # QGIS drops an emptied plugin submenu too
            del self.plugin_menus[name]
            menu.deleteLater()

    def addCustomActionForLayerType(self, action, _menu, _layer_type, _all_layers):
        self.layer_tree_actions.append(action)

    def removeCustomActionForLayerType(self, action):
        if action in self.layer_tree_actions:
            self.layer_tree_actions.remove(action)
            return True
        return False

    def addDockWidget(self, area, dock):
        self.window.addDockWidget(area, dock)

    def removeDockWidget(self, dock):
        self.window.removeDockWidget(dock)

    def menu_actions(self):
        return [a for menu in self.plugin_menus.values() for a in menu.actions()]


# -- fakes for the heavy tools ---------------------------------------------------
class _FakeDock(QDockWidget):
    """Stands in for the Workbench / Planner / Burial Planner docks."""

    def __init__(self, title, fail_shutdown=False):
        super().__init__(title)
        self.calls = []
        self._fail = fail_shutdown

    def shutdown(self):
        self.calls.append("shutdown")
        if self._fail:
            raise RuntimeError("shutdown failed on purpose")

    def refresh(self):
        self.calls.append("refresh")


class _FakeToolWindow(QDialog):
    """A tool window with shutdown() (stops its worker, clears map items)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.calls = []

    def shutdown(self):
        self.calls.append("shutdown")


class _FakeLegacyWindow(QDialog):
    """A tool window without shutdown(): unload falls back to close()."""

    def __init__(self, parent):
        super().__init__(parent)
        self.calls = []

    def closeEvent(self, event):  # noqa: N802 - Qt API
        self.calls.append("close")
        super().closeEvent(event)


class _DockPickTool(QgsMapToolPan):
    """A plugin-defined map tool a dock set on the canvas itself."""


class _KeepOffScreen(QObject):
    """Tool windows are real top-level windows; keep them off the desktop."""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            obj.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        return False


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level=logging.DEBUG):
        return [r.getMessage() for r in self.records if r.levelno >= level]


@contextmanager
def _patched(obj, name, value):
    missing = object()
    old = getattr(obj, name, missing)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if old is missing:
            delattr(obj, name)
        else:
            setattr(obj, name, old)


# -- helpers ---------------------------------------------------------------------
def _write_qm(path, context, source, translation):
    """Write a minimal compiled Qt translation (.qm) holding one message."""
    def elf_hash(text):
        h = 0
        for byte in text.encode("utf-8"):
            h = ((h << 4) + byte) & 0xFFFFFFFF
            g = h & 0xF0000000
            if g:
                h ^= g >> 24
            h &= ~g & 0xFFFFFFFF
        return h or 1

    def block(tag, data):
        return struct.pack(">BI", tag, len(data)) + data

    message = (block(3, translation.encode("utf-16-be"))  # translation
               + block(6, source.encode("utf-8"))        # source text
               + block(7, context.encode("utf-8"))       # context
               + b"\x01")                                # end of message
    magic = bytes((0x3C, 0xB8, 0x64, 0x18, 0xCA, 0xEF, 0x9C, 0x95,
                   0xCD, 0x21, 0x1C, 0xBF, 0x60, 0xA1, 0xBD, 0xDD))
    with open(path, "wb") as handle:
        handle.write(magic
                     + block(0x42, struct.pack(">II", elf_hash(source), 0))  # hashes
                     + block(0x69, message))                                 # messages


def _address(obj):
    from qgis.PyQt import sip
    return sip.unwrapinstance(obj)


def _owned_objects(iface):
    """Actions, menus, buttons, docks and dialogs hanging off the main window,
    plus hidden top-level widgets (released toolbar buttons end up there)."""
    from qgis.PyQt.QtWidgets import QToolButton
    found = {}
    for cls in (QAction, QMenu, QToolButton, QDockWidget, QDialog):
        for obj in iface.window.findChildren(cls):
            found[_address(obj)] = obj
    for widget in QApplication.topLevelWidgets():
        if widget is not iface.window:
            found[_address(widget)] = widget
    return found


def _describe(obj):
    text = ""
    for getter in ("text", "windowTitle", "objectName"):
        try:
            text = getattr(obj, getter)()
        except Exception:
            continue
        if text:
            break
    return f"{type(obj).__name__}({text!r})"


class _Harness:
    def __init__(self, temp):
        from ..subsea_cable_tools import SubseaCableTools

        self.temp = temp
        self.iface = _Iface()
        self.plugin_class = SubseaCableTools
        self.filter = _KeepOffScreen()
        QApplication.instance().installEventFilter(self.filter)
        self.log = _Capture()
        logger = logging.getLogger("subsea_cable_tools")
        self._old_level = logger.level
        logger.addHandler(self.log)
        logger.setLevel(logging.DEBUG)
        self.restores = {"workbench": 0, "burial": 0}
        self._patches = []
        for module_name, func_name, key in (
                (f"{_PACKAGE}.workbench.project_layers", "restore_workbench_layers", "workbench"),
                (f"{_PACKAGE}.burial.map_layers", "restore_burial_layers", "burial")):
            module = importlib.import_module(module_name)
            original = getattr(module, func_name)
            setattr(module, func_name, self._counter(key))
            self._patches.append((module, func_name, original))

    def _counter(self, key):
        def restore(*_args, **_kwargs):
            self.restores[key] += 1
        return restore

    def new_plugin(self):
        plugin = self.plugin_class(self.iface)
        plugin.i18n_dir = os.path.join(self.temp, "i18n")  # never the plugin's folder
        return plugin

    def close(self):
        QApplication.instance().removeEventFilter(self.filter)
        logger = logging.getLogger("subsea_cable_tools")
        logger.removeHandler(self.log)
        logger.setLevel(self._old_level)
        plugin_log.set_debug_enabled(None)
        for module, func_name, original in self._patches:
            setattr(module, func_name, original)
        self.iface.window.deleteLater()
        _flush_deletes()


# -- tests -------------------------------------------------------------------------
def test_full_lifecycle(h: _Harness) -> bool:
    """initGui, open tools, unload: nothing of the plugin is left behind."""
    from ..maptools.transit_measure_tool import TransitMeasureTool

    iface = h.iface
    registry = QgsApplication.processingRegistry()
    before = _owned_objects(iface)
    lang = (QgsApplication.locale() or "en")[0:2]
    os.makedirs(os.path.join(h.temp, "i18n"), exist_ok=True)
    _write_qm(os.path.join(h.temp, "i18n", f"SubseaCableTools_{lang}.qm"),
              "SubseaCableTools", "Experimental", "Expérimental")

    plugin = h.new_plugin()
    plugin.initGui()
    problems = []
    teardown_calls = []
    try:
        # What initGui installs.
        if plugin.translator is None or plugin.experimental_tool_button.text() != "Expérimental":
            problems.append("translation not installed")
        if registry.providerById(_PROVIDER_ID) is None:
            problems.append("processing provider not registered")
        if iface.layer_tree_actions != [plugin.save_layers_gpkg_action]:
            problems.append("layer-tree action not added")
        ours = set(map(_address, plugin.actions))
        if not ours <= set(map(_address, iface.menu_actions())):
            problems.append("an action is missing from the plugin menu")
        start = dict(h.restores)
        iface.projectRead.emit()
        if h.restores["workbench"] != start["workbench"] + 1 or h.restores["burial"] != start["burial"] + 1:
            problems.append("projectRead hooks not connected")

        # Open the lightweight tools for real; stand in for the heavy ones.
        plugin.activate_transit_measure_tool()
        transit = plugin.transit_measure_tool
        if not isinstance(iface.canvas.mapTool(), TransitMeasureTool):
            problems.append("Transit Measure is not the active map tool")
        transit_dialog = transit.dialog
        plugin.show_plotter()
        plugin.show_depth_profile()
        real_docks = [plugin.plotter_dock, plugin.depth_profile_dock]
        if any(d is None for d in real_docks):
            problems.append("a real dock did not open")
        fakes = {
            "workbench_dock": _FakeDock("Workbench"),
            "planner_dock": _FakeDock("Planner", fail_shutdown=True),
            "burial_dock": _FakeDock("Burial Planner"),
        }
        for attr, dock in fakes.items():
            iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
            setattr(plugin, attr, dock)
        windows = {
            "lay_simulator_dialog": _FakeToolWindow(iface.window),
            "bu_lowering_dialog": _FakeLegacyWindow(iface.window),
        }
        for attr, window in windows.items():
            window.show()
            setattr(plugin, attr, window)
        shell = (list(plugin.actions)
                 + [plugin.experimental_tool_button, plugin.experimental_toolbar_action]
                 + real_docks + list(fakes.values()) + list(windows.values()) + [transit])
        QCoreApplication.processEvents()

        # A failing teardown step (the Planner fake) must not stop the rest,
        # and it must be diagnosable with debug logging on.
        plugin_log.set_debug_enabled(True)
        plugin.unload()
        plugin_log.set_debug_enabled(None)
        teardown_calls = [list(tool.calls) for tool in list(fakes.values()) + list(windows.values())]
        _flush_deletes()
    finally:
        plugin_log.set_debug_enabled(None)
        plugin.unload()  # no-op after a clean unload; cleans up after an error

    # What unload must have undone.
    if iface.toolbar.actions():
        problems.append("toolbar entries left: " + ", ".join(map(_describe, iface.toolbar.actions())))
    if iface.menu_actions():
        problems.append("menu entries left: " + ", ".join(map(_describe, iface.menu_actions())))
    if iface.layer_tree_actions:
        problems.append("layer-tree action left")
    if registry.providerById(_PROVIDER_ID) is not None:
        problems.append("processing provider still registered")
    start = dict(h.restores)
    iface.projectRead.emit()
    if h.restores != start:
        problems.append("projectRead hooks still connected")
    if iface.canvas.mapTool() is not None:
        problems.append(f"map tool still set: {iface.canvas.mapTool()!r}")
    if not is_deleted(transit) or (transit_dialog is not None and not is_deleted(transit_dialog)):
        problems.append("Transit Measure tool/dialog not deleted")
    if QCoreApplication.translate("SubseaCableTools", "Experimental") != "Experimental":
        problems.append("translator still installed")
    # Docks: full-teardown shutdown(); windows: shutdown(), else close().
    if teardown_calls != [["shutdown"]] * 4 + [["close"]]:
        problems.append(f"teardown calls: {teardown_calls}")
    alive = [_describe(obj) for obj in shell if not is_deleted(obj)]
    if alive:
        problems.append("not deleted: " + ", ".join(alive))
    if not any("Unload:" in m and "planner_dock.shutdown()" in m for m in h.log.messages()):
        problems.append("failed teardown step was not logged")
    leftovers = [_describe(obj) for addr, obj in _owned_objects(iface).items()
                 if addr not in before and not is_deleted(obj)]
    if leftovers:
        # Includes components the shell delegates to (KPMouseTool.unload()).
        problems.append("objects left on the main window: " + ", ".join(leftovers))
    return _result("full lifecycle: initGui -> tools -> unload leaves nothing",
                   not problems, "; ".join(problems))


def test_window_teardown_calls(h: _Harness) -> bool:
    """shutdown() when a window has it, close() otherwise, then deletion."""
    plugin = h.new_plugin()
    with_shutdown = _FakeToolWindow(h.iface.window)
    legacy = _FakeLegacyWindow(h.iface.window)
    plugin.catenary_calculator_v2_dialog = with_shutdown
    plugin.explorer_window = legacy
    plugin.unload()
    calls = (list(with_shutdown.calls), list(legacy.calls))
    _flush_deletes()
    ok = calls == (["shutdown"], ["close"]) and is_deleted(with_shutdown) and is_deleted(legacy)
    return _result("tool windows: shutdown() or close(), then deleted", ok, repr(calls))


def test_unload_is_idempotent_and_reinit_works(h: _Harness) -> bool:
    """unload twice; initGui again on the same instance and on a new one."""
    iface = h.iface
    registry = QgsApplication.processingRegistry()
    problems = []
    plugin = h.new_plugin()
    for cycle in range(2):  # the same instance, re-initialised after unload
        plugin.initGui()
        if registry.providerById(_PROVIDER_ID) is None:
            problems.append(f"cycle {cycle}: provider not registered")
        tool = _DockPickTool(iface.canvas)
        iface.canvas.setMapTool(tool)  # as a dock's pick tool would
        plugin.unload()
        plugin.unload()
        if iface.canvas.mapTool() is not None:
            problems.append(f"cycle {cycle}: plugin map tool left active")
        if iface.toolbar.actions() or iface.menu_actions():
            problems.append(f"cycle {cycle}: toolbar/menu entries left")
        if registry.providerById(_PROVIDER_ID) is not None:
            problems.append(f"cycle {cycle}: provider still registered")
        tool.deleteLater()
        _flush_deletes()
    reloaded = h.new_plugin()  # a plugin reload builds a fresh instance
    reloaded.initGui()
    ok_reload = registry.providerById(_PROVIDER_ID) is not None and bool(iface.toolbar.actions())
    reloaded.unload()
    _flush_deletes()
    if not ok_reload or iface.toolbar.actions() or iface.menu_actions():
        problems.append("reload into a new instance failed")
    return _result("unload twice, re-initialise, reload", not problems, "; ".join(problems))


def test_reopen_after_close_or_delete(h: _Harness) -> bool:
    """A closed dock is re-shown (not rebuilt); a deleted one is rebuilt."""
    from qgis.PyQt import sip

    plugin = h.new_plugin()
    problems = []
    try:
        burial = _FakeDock("Burial Planner")
        h.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, burial)
        plugin.burial_dock = burial
        plugin.show_burial_planner()
        burial.close()
        plugin.show_burial_planner()
        if plugin.burial_dock is not burial or burial.isHidden() or burial.calls != ["refresh", "refresh"]:
            problems.append(f"closed Burial Planner not re-shown: {burial.calls}")
        plugin.show_plotter()
        first = plugin.plotter_dock
        sip.delete(first)
        plugin.show_plotter()
        if plugin.plotter_dock is None or is_deleted(plugin.plotter_dock):
            problems.append("deleted KP Plot dock was not rebuilt")
    finally:
        plugin.unload()
        _flush_deletes()
    return _result("tools re-open after close / deletion", not problems, "; ".join(problems))


def test_open_failure_is_reported(h: _Harness) -> bool:
    """A tool that fails to open tells the user and logs the traceback."""
    from .. import subsea_cable_tools as shell

    shown = []

    class _Box:
        @staticmethod
        def critical(_parent, title, text):
            shown.append((title, text))

    plugin = h.new_plugin()
    module_name = f"{_PACKAGE}.explorer"
    missing = object()
    saved = sys.modules.get(module_name, missing)
    sys.modules[module_name] = None  # makes "from .explorer import ..." fail
    h.log.records.clear()
    try:
        with _patched(shell, "QMessageBox", _Box):
            plugin.show_cable_lay_explorer()
    finally:
        if saved is missing:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = saved
        plugin.unload()
    logged = [r for r in h.log.records
              if r.levelno >= logging.WARNING and "Cable Lay Data Explorer" in r.getMessage()]
    ok = (plugin.explorer_window is None and len(shown) == 1
          and "could not be opened" in shown[0][1] and "Details:" in shown[0][1]
          and bool(logged) and "Traceback" in logged[0].getMessage())
    return _result("tool open failure: message box + logged traceback", ok,
                   repr(shown) if not ok else "")


def test_icons(h: _Harness) -> bool:
    """Every tool has its own icon file; the new SVGs render."""
    from qgis.PyQt.QtSvg import QSvgRenderer

    plugin = h.new_plugin()
    expected = {
        "workbench_icon.svg": "Cable Route Workbench",
        "planner_icon.svg": "Planner",
        "burial_planner_icon.svg": "Burial Planner",
        "lay_data_explorer_icon.svg": "Cable Lay Data Explorer",
        "kp_settings_icon.svg": "KP settings",
    }
    problems = []
    paths = set()
    for name in expected:
        path = plugin._icon_path(name)
        paths.add(path)
        if os.path.dirname(path) != plugin.icons_dir or not QSvgRenderer(path).isValid():
            problems.append(f"{name}: {path}")
    if len(paths) != len(expected):
        problems.append("icons are not distinct")
    if os.path.basename(plugin._icon_path("does_not_exist.svg")) != "icon.png":
        problems.append("missing icon does not fall back to icon.png")
    plugin.initGui()
    try:
        for action in plugin.actions:
            if action.icon().isNull():
                problems.append(f"{action.text()}: null icon")
    finally:
        plugin.unload()
        _flush_deletes()
    source = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "subsea_cable_tools.py"),
                  encoding="utf-8").read()
    if ":/plugins" in source or importlib.util.find_spec(f"{_PACKAGE}.resources") is not None:
        problems.append("compiled Qt resources are still referenced")
    return _result("tool icons: distinct files, SVGs valid, no Qt resources",
                   not problems, "; ".join(problems))


def test_vendored_libs_do_not_shadow_host(_h: _Harness) -> bool:
    """lib/ is a fallback: host copies win, missing modules come from lib/."""
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lib = os.path.join(package_dir, "lib")

    def norm(path):
        return os.path.normcase(os.path.abspath(path or "."))

    def in_lib(path):
        return bool(path) and norm(path).startswith(norm(lib) + os.sep)

    problems = []
    entries = [norm(p) for p in sys.path]
    if entries.count(norm(lib)) != 1:
        problems.append(f"lib/ is on sys.path {entries.count(norm(lib))} times")
    elif sys.path[0] and norm(sys.path[0]) == norm(lib):
        problems.append("lib/ is first on sys.path")
    others = [p for p in sys.path if norm(p) != norm(lib)]
    report = []
    for entry in sorted(os.listdir(lib)):
        if entry.endswith(".dist-info") or entry.startswith(("_", ".")):
            continue
        name = entry[:-3] if entry.endswith(".py") else entry
        if not (entry.endswith(".py") or os.path.isdir(os.path.join(lib, entry))):
            continue
        host = importlib.machinery.PathFinder.find_spec(name, others)
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # report, keep going
            problems.append(f"{name} does not import: {exc!r}")
            continue
        from_lib = in_lib(getattr(module, "__file__", ""))
        report.append(f"{name}={'lib' if from_lib else 'host'}")
        if from_lib == (host is not None):
            problems.append(f"{name} imported from {'lib' if from_lib else 'host'}")
    # The package bootstrap respects an explicit OPENPYXL_LXML and never
    # adds lib/ twice.
    init = os.path.join(package_dir, "__init__.py")
    saved = os.environ.get("OPENPYXL_LXML")
    try:
        os.environ["OPENPYXL_LXML"] = "True"
        runpy.run_path(init)
        kept = os.environ.get("OPENPYXL_LXML") == "True"
        del os.environ["OPENPYXL_LXML"]
        runpy.run_path(init)
        defaulted = os.environ.get("OPENPYXL_LXML") == "False"
    finally:
        if saved is None:
            os.environ.pop("OPENPYXL_LXML", None)
        else:
            os.environ["OPENPYXL_LXML"] = saved
    if not (kept and defaulted):
        problems.append(f"OPENPYXL_LXML handling: kept={kept} defaulted={defaulted}")
    if [norm(p) for p in sys.path].count(norm(lib)) != 1:
        problems.append("re-running the bootstrap added lib/ again")
    return _result("vendored lib/ is a fallback (" + ", ".join(report) + ")",
                   not problems, "; ".join(problems))


def run_all() -> List[bool]:
    results = []
    old_format = QSettings.defaultFormat()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        harness = _Harness(temp)
        try:
            for test in (test_full_lifecycle, test_window_teardown_calls,
                         test_unload_is_idempotent_and_reinit_works,
                         test_reopen_after_close_or_delete, test_open_failure_is_reported,
                         test_icons, test_vendored_libs_do_not_shadow_host):
                try:
                    results.append(test(harness))
                except Exception as exc:  # report, keep going
                    import traceback
                    traceback.print_exc()
                    results.append(_result(test.__name__, False, repr(exc)))
        finally:
            harness.close()
            QSettings.setDefaultFormat(old_format)
    print("")
    print(f"{sum(results)}/{len(results)} passed")
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
