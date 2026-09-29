# subsea_cable_tools.py
# -*- coding: utf-8 -*-
"""
SubseaCableTools
A QGIS plugin with tools for working with subsea cables.
"""

import logging
import os.path
import sys

from qgis.PyQt.QtCore import QTranslator, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMenu, QMessageBox, QToolButton

from qgis.core import QgsApplication

from .plugin_log import log_exception, log_warning
from .qgis_compat import LAYER_VECTOR, QAction, TOOLBUTTON_POPUP_MODE_INSTANT, is_deleted

# Import the KP Mouse Tool (map tool integration)
from .maptools.kp_mouse_maptool import KPMouseTool

# Import the processing provider
from .processing.subsea_cable_processing_provider import SubseaCableProcessingProvider

# NOTE: Larger dock widgets are imported lazily so plugin startup remains robust
# if an optional or vendored plotting dependency fails to load.

TITLE = "Subsea Cable Tools"
_PYQTGRAPH_HINT = " This tool requires the bundled pyqtgraph plotting backend."


def _alive(obj):
    """True for a tool widget that exists and has not been deleted by Qt."""
    return obj is not None and not is_deleted(obj)


def _quietly(description, step):
    """Run one teardown step (a no-argument callable, so that even looking up
    the method happens inside the guard). A failure is logged (debug) and
    swallowed so the remaining steps still run: unload must never stop half
    way."""
    try:
        return step()
    except Exception:
        log_exception(f"Unload: {description} failed", level=logging.DEBUG)
        return None


class SubseaCableTools:
    """QGIS Plugin Implementation."""

    # Docked tools: (attribute, teardown methods run before the dock is
    # removed). A method a dock does not define is skipped.
    _DOCKS = (
        ('plotter_dock', ('shutdown', 'cleanup_plot_and_marker',
                          'cleanup_matplotlib_resources_on_close')),
        ('depth_profile_dock', ('shutdown', 'clear_plot')),
        ('workbench_dock', ('shutdown',)),
        ('planner_dock', ('shutdown',)),
        ('burial_dock', ('shutdown',)),
    )
    # Top-level tool windows: shutdown() stops their workers and removes their
    # map graphics; close() is the fallback for a window without one.
    _WINDOWS = (
        'explorer_window',
        'catenary_calculator_v2_dialog',
        'lay_simulator_dialog',
        'bu_lowering_dialog',
    )
    # Every action attribute set by initGui (all are also in self.actions).
    _ACTION_ATTRS = (
        'plotter_action', 'depth_profile_action', 'catenary_v2_action',
        'lay_simulator_action', 'bu_lowering_action', 'workbench_action',
        'planner_action', 'burial_action', 'transit_measure_action',
        'explorer_action', 'kp_settings_action', 'save_layers_gpkg_action',
    )

    def __init__(self, iface):
        """Constructor.
        :param iface: A QGIS interface instance.
        """
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.icons_dir = os.path.join(self.plugin_dir, 'icons')
        # Optional translations: a compiled SubseaCableTools_<lang>.qm in this
        # folder is loaded by initGui(). None ship with the plugin (the UI is
        # English only), so normally no translator is installed.
        self.i18n_dir = os.path.join(self.plugin_dir, 'i18n')
        self.translator = None

        # Core state
        self.actions = []
        self.menu = self.tr(u'&Subsea Cable Tools')
        self._project_hooks = []

        # Components (initGui() recreates them if unload() released them)
        self.kp_mouse_tool = KPMouseTool(self.iface, menu_name=self.menu)
        self.kpProvider = SubseaCableProcessingProvider()

        # UI elements (dock widgets / actions)
        self.plotter_dock = None
        self.depth_profile_dock = None
        self.transit_measure_tool = None
        self.workbench_dock = None
        self.planner_dock = None
        self.burial_dock = None
        self.explorer_window = None
        self.catenary_calculator_v2_dialog = None
        self.lay_simulator_dialog = None
        self.bu_lowering_dialog = None
        for attr in self._ACTION_ATTRS:
            setattr(self, attr, None)
        self.experimental_menu = None
        self.experimental_tool_button = None
        self.experimental_toolbar_action = None

    def tr(self, message):
        """Return the translation for a string."""
        return QCoreApplication.translate('SubseaCableTools', message)

    def _main_window(self):
        return self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None

    def _canvas(self):
        try:
            return self.iface.mapCanvas()
        except Exception:
            return None

    def _icon_path(self, *names):
        """Path of the first of ``names`` found in icons/ or the plugin folder,
        falling back to the plugin icon."""
        for name in names:
            for folder in (self.icons_dir, self.plugin_dir):
                path = os.path.join(folder, name)
                if os.path.exists(path):
                    return path
        return os.path.join(self.plugin_dir, 'icon.png')

    def _icon(self, *names):
        return QIcon(self._icon_path(*names))

    def add_action(self, icon, text, callback, tooltip=None, add_to_toolbar=False,
                   add_to_menu=True):
        """Create an action parented to the main window, add it to the plugin
        menu (and optionally the Plugins toolbar) and register it for unload."""
        action = QAction(icon, text, self._main_window())
        self.actions.append(action)
        if tooltip:
            action.setToolTip(tooltip)
        action.triggered.connect(callback)
        if add_to_toolbar:
            self.iface.addToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        return action

    def _install_translator(self):
        """Install the translation for the QGIS UI language, if one exists."""
        if self.translator is not None:
            return
        try:
            locale = (QgsApplication.locale() or 'en')[0:2]
        except Exception:
            locale = 'en'
        locale_path = os.path.join(self.i18n_dir, f'SubseaCableTools_{locale}.qm')
        if not os.path.exists(locale_path):
            return
        translator = QTranslator()
        if translator.load(locale_path):
            QCoreApplication.installTranslator(translator)
            self.translator = translator
        else:
            log_warning(f"Could not load the translation file {locale_path}")

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""
        self._install_translator()
        self.menu = self.tr(u'&Subsea Cable Tools')

        # Register the processing provider (adds your algorithms to the Processing Toolbox)
        if self.kpProvider is None:
            self.kpProvider = SubseaCableProcessingProvider()
        if not QgsApplication.processingRegistry().addProvider(self.kpProvider):
            # The registry takes (and on failure deletes) the provider.
            self.kpProvider = None
            log_warning("The Subsea Cable Tools processing provider could not be "
                        "registered; its algorithms are unavailable this session.")

        # Initialize the KP Mouse Tool’s UI elements
        if self.kp_mouse_tool is None:
            self.kp_mouse_tool = KPMouseTool(self.iface, menu_name=self.menu)
        # Same (now translated) menu as the plugin's other entries.
        self.kp_mouse_tool.menu_name = self.menu
        self.kp_mouse_tool.initGui()

        self.plotter_action = self.add_action(
            self._icon('kp_plot_icon.png'), "KP Plot", self.show_plotter,
            add_to_toolbar=True)
        self.depth_profile_action = self.add_action(
            self._icon('depth_profile_icon.png'), "Depth Profile",
            self.show_depth_profile, add_to_toolbar=True)
        self.catenary_v2_action = self.add_action(
            self._icon('catenary_icon_v2.png', 'catenary_icon.png'),
            "Catenary Calculator V2", self.show_catenary_calculator_v2,
            add_to_toolbar=True)

        # Cable Lay Simulator (3D) — catenary V3
        lay_icon = self._icon('lay_simulator_icon.png', 'catenary_icon_v2.png')
        self.lay_simulator_action = self.add_action(
            lay_icon, "Cable Lay Simulator (3D)", self.show_lay_simulator,
            tooltip="Cable Lay Simulator (3D): static hang, steady lay with drag, "
                    "and operation simulation (beta).")
        # BU Lowering Tool — the lowering-only BU scenario as its own dialog
        self.bu_lowering_action = self.add_action(
            lay_icon, "BU Lowering Tool (3D)", self.show_bu_lowering,
            tooltip="BU Lowering Tool (3D): lower a branching unit on its trunk "
                    "over two pre-laid legs — quick analytic model with a "
                    "full-solver verify (beta).")

        # Cable Route Workbench (assemblies + RPLs + systems in one dock)
        self.workbench_action = self.add_action(
            self._icon('workbench_icon.svg'), "Cable Route Workbench",
            self.show_workbench,
            tooltip="Cable Route Workbench: assemblies, RPLs, fits, and cable "
                    "systems — with map editing and an SLD.")
        # Spatial planning scenario editor and simulator
        self.planner_action = self.add_action(
            self._icon('planner_icon.svg'), "Planner", self.show_planner,
            tooltip="Build map-linked work plans, simulate vessel progress, and "
                    "copy tasks to MS Project.")
        # Burial planning workflow (plough / ROV jet) over an RPL
        self.burial_action = self.add_action(
            self._icon('burial_planner_icon.svg'), "Burial Planner (beta)",
            self.show_burial_planner,
            tooltip="Plan cable burial: exclusion criteria over route and survey "
                    "data, candidate sections, PLDN/PLUP events, synced "
                    "map/profile/tables.")

        self.transit_measure_action = self.add_action(
            self._icon('transit_measure_icon.png'), "Transit Measure",
            self.activate_transit_measure_tool, add_to_toolbar=True)

        # Cable Lay Data Explorer (standalone analysis / QC window)
        self.explorer_action = self.add_action(
            self._icon('lay_data_explorer_icon.svg'), "Cable Lay Data Explorer",
            self.show_cable_lay_explorer)

        # Plugin-wide KP distance setting (Geodesic WGS84 / Cartesian grid)
        self.kp_settings_action = self.add_action(
            self._icon('kp_settings_icon.svg'), "KP settings…", self.show_kp_settings,
            tooltip="How KP is measured across the plugin: Geodesic (WGS84, "
                    "default) or Cartesian (grid).")

        # Save the selected layers (e.g. an MDB import's temporary layers)
        # into one GeoPackage; also offered on the Layers panel context menu.
        self.save_layers_gpkg_action = self.add_action(
            QgsApplication.getThemeIcon("/mActionFileSave.svg"),
            "Save Layers to GeoPackage…", self.save_layers_to_gpkg,
            tooltip="Save the layers selected in the Layers panel into one "
                    "GeoPackage and point the project layers at it.")
        try:
            self.iface.addCustomActionForLayerType(
                self.save_layers_gpkg_action, "", LAYER_VECTOR, True)
        except Exception:
            log_exception("Could not add 'Save Layers to GeoPackage…' to the "
                          "Layers panel context menu")

        self._add_experimental_toolbar_menu()

        # Re-add / repair Cable Route Workbench and Burial Planner layers
        # whenever a project is opened, without requiring the docks themselves
        # to be opened.
        for slot in (self._restore_workbench_layers, self._restore_burial_layers):
            try:
                self.iface.projectRead.connect(slot)
                self._project_hooks.append(slot)
            except Exception:
                log_exception("Could not watch for project loads; Workbench and "
                              "Burial Planner layers will not be repaired when a "
                              "project is opened")
        # The plugin may have been enabled while a project is already open.
        self._restore_workbench_layers()
        self._restore_burial_layers()

    def _restore_workbench_layers(self):
        """Self-heal workbench layers for the current project (cheap no-op
        when the project has no workbench GeoPackage)."""
        try:
            from .workbench.project_layers import restore_workbench_layers
            restore_workbench_layers()
        except Exception:
            log_exception("Cable Route Workbench: restoring project layers failed")

    def _restore_burial_layers(self):
        """Repair broken Burial Planner plan layers for the current project
        (cheap no-op when none are present)."""
        try:
            from .burial.map_layers import restore_burial_layers
            restore_burial_layers()
        except Exception:
            log_exception("Burial Planner: restoring project layers failed")

    def _add_experimental_toolbar_menu(self):
        """Add one toolbar dropdown for tools that are still experimental."""
        self.experimental_tool_button = QToolButton(self._main_window())
        self.experimental_tool_button.setObjectName(
            "subseaCableToolsExperimentalButton")
        self.experimental_tool_button.setIcon(self._icon('icon.png'))
        self.experimental_tool_button.setText(self.tr("Experimental"))
        self.experimental_tool_button.setToolTip(
            self.tr("Experimental tools (beta)"))
        # Every part of the button opens the menu; there is no arbitrary
        # default experimental tool associated with its main click area.
        self.experimental_tool_button.setPopupMode(
            TOOLBUTTON_POPUP_MODE_INSTANT)

        self.experimental_menu = QMenu(self.experimental_tool_button)
        self.experimental_menu.setTitle(self.tr("Experimental tools"))
        for action in (
                self.workbench_action,
                self.planner_action,
                self.burial_action,
                self.explorer_action,
                self.lay_simulator_action,
                self.bu_lowering_action):
            self.experimental_menu.addAction(action)
        self.experimental_tool_button.setMenu(self.experimental_menu)
        self.experimental_toolbar_action = self.iface.addToolBarWidget(
            self.experimental_tool_button)

    def _report_open_failure(self, message):
        """Tell the user a tool failed to open. Call from an ``except`` block:
        the traceback goes to the Subsea Cable Tools log."""
        log_exception(message)
        QMessageBox.critical(
            self._main_window(), TITLE,
            f"{message}\n\nDetails: {sys.exc_info()[1]}")

    def show_catenary_calculator_v2(self):
        if not _alive(self.catenary_calculator_v2_dialog):
            try:
                from .catenary.catenary_calculator_v2_dialog import CatenaryCalculatorV2Dialog
                self.catenary_calculator_v2_dialog = CatenaryCalculatorV2Dialog(self._main_window())
            except Exception:
                self.catenary_calculator_v2_dialog = None
                self._report_open_failure("Catenary Calculator V2 could not be opened.")
                return
        self.catenary_calculator_v2_dialog.show()
        self.catenary_calculator_v2_dialog.raise_()
        self.catenary_calculator_v2_dialog.activateWindow()

    def show_lay_simulator(self):
        if not _alive(self.lay_simulator_dialog):
            try:
                from .catenary.v3.ui.dialog import LaySimulatorDialog
                self.lay_simulator_dialog = LaySimulatorDialog(self._main_window(), iface=self.iface)
            except Exception:
                self.lay_simulator_dialog = None
                self._report_open_failure("Cable Lay Simulator (3D) could not be opened.")
                return
        self.lay_simulator_dialog.show()
        self.lay_simulator_dialog.raise_()
        self.lay_simulator_dialog.activateWindow()

    def show_bu_lowering(self):
        if not _alive(self.bu_lowering_dialog):
            try:
                from .catenary.v3.ui.bu_lowering_dialog import BULoweringDialog
                self.bu_lowering_dialog = BULoweringDialog(self._main_window(), iface=self.iface)
            except Exception:
                self.bu_lowering_dialog = None
                self._report_open_failure("BU Lowering Tool (3D) could not be opened.")
                return
        self.bu_lowering_dialog.show()
        self.bu_lowering_dialog.raise_()
        self.bu_lowering_dialog.activateWindow()

    def show_cable_lay_explorer(self):
        """Show the standalone Cable Lay Data Explorer window."""
        if not _alive(self.explorer_window):
            try:
                from .explorer import CableLayExplorerWindow
                self.explorer_window = CableLayExplorerWindow(self.iface, self._main_window())
            except Exception:
                self.explorer_window = None
                self._report_open_failure(
                    "Cable Lay Data Explorer could not be opened." + _PYQTGRAPH_HINT)
                return
        self.explorer_window.show()
        self.explorer_window.raise_()
        self.explorer_window.activateWindow()

    def show_kp_settings(self):
        from .kp_settings_dialog import edit_global_kp_settings
        if edit_global_kp_settings(self._main_window()):
            from .kp_range_utils import describe_kp_mode
            try:
                self.iface.messageBar().pushMessage(
                    TITLE, "KP distance: " + describe_kp_mode()
                    + ". Reopen KP tools to apply.", duration=5)
            except Exception:
                log_exception("KP settings: could not show the confirmation",
                              level=logging.DEBUG)

    def save_layers_to_gpkg(self):
        from .save_layers_to_gpkg import run_save_layers_dialog
        run_save_layers_dialog(self.iface)

    def unload(self):
        """Remove everything initGui() added and tear down every open tool.

        Each step is isolated: a failure cleaning up one thing is logged
        (debug) and never stops the rest. Calling unload() again is harmless,
        and initGui() may be called again afterwards.
        """
        _quietly("removing the layer-tree action", self._remove_layer_tree_action)
        _quietly("unregistering the processing provider", self._remove_processing_provider)
        _quietly("disconnecting project hooks", self._disconnect_project_hooks)
        _quietly("unloading the KP Mouse Tool", self._unload_kp_mouse_tool)
        _quietly("unloading Transit Measure", self._unload_transit_measure_tool)
        for attr, methods in self._DOCKS:
            _quietly(f"closing {attr}", lambda: self._teardown_dock(attr, methods))
        for attr in self._WINDOWS:
            _quietly(f"closing {attr}", lambda: self._teardown_window(attr))
        # Safety net for tools the docks/windows set on the canvas themselves.
        _quietly("releasing the canvas map tool", self._unset_plugin_map_tool)
        _quietly("removing the Experimental toolbar button", self._remove_experimental_toolbar)
        _quietly("removing menu and toolbar actions", self._remove_actions)
        _quietly("removing the translator", self._remove_translator)

    def _remove_layer_tree_action(self):
        action = self.save_layers_gpkg_action
        if action is not None and not is_deleted(action):
            self.iface.removeCustomActionForLayerType(action)

    def _remove_processing_provider(self):
        provider, self.kpProvider = self.kpProvider, None
        if provider is not None and not is_deleted(provider):
            # The registry deletes the provider it removes.
            QgsApplication.processingRegistry().removeProvider(provider)

    def _disconnect_project_hooks(self):
        hooks, self._project_hooks = self._project_hooks, []
        for slot in hooks:
            _quietly("disconnecting projectRead",
                     lambda: self.iface.projectRead.disconnect(slot))

    def _release_map_tool(self, tool):
        """Unset ``tool`` if it is the canvas's active map tool."""
        canvas = self._canvas()
        if canvas is not None and tool is not None and canvas.mapTool() is tool:
            canvas.unsetMapTool(tool)

    def _unload_kp_mouse_tool(self):
        tool, self.kp_mouse_tool = self.kp_mouse_tool, None
        if tool is None:
            return
        _quietly("unsetting the KP Mouse Tool",
                 lambda: self._release_map_tool(getattr(tool, 'mapTool', None)))
        _quietly("KPMouseTool.unload()", lambda: tool.unload())

    def _unload_transit_measure_tool(self):
        tool, self.transit_measure_tool = self.transit_measure_tool, None
        if not _alive(tool):
            return
        # Deactivating closes the dialog and clears its rubber bands.
        _quietly("unsetting Transit Measure", lambda: self._release_map_tool(tool))
        cleanup = getattr(tool, 'cleanup', None)
        if callable(cleanup):
            _quietly("TransitMeasureTool.cleanup()", cleanup)
        _quietly("deleting Transit Measure", lambda: tool.deleteLater())

    def _teardown_dock(self, attr, methods):
        dock = getattr(self, attr, None)
        setattr(self, attr, None)
        if not _alive(dock):
            return
        for name in methods:
            method = getattr(dock, name, None)
            if callable(method):
                _quietly(f"{attr}.{name}()", method)
        _quietly(f"removing {attr}", lambda: self.iface.removeDockWidget(dock))
        _quietly(f"deleting {attr}", lambda: dock.deleteLater())

    def _teardown_window(self, attr):
        window = getattr(self, attr, None)
        setattr(self, attr, None)
        if not _alive(window):
            return
        shutdown = getattr(window, 'shutdown', None)
        if callable(shutdown):
            _quietly(f"{attr}.shutdown()", shutdown)
        else:
            _quietly(f"closing {attr}", lambda: window.close())
        _quietly(f"deleting {attr}", lambda: window.deleteLater())

    def _unset_plugin_map_tool(self):
        """Never leave a map tool defined by this plugin active on the canvas
        once the plugin's code is being unloaded."""
        canvas = self._canvas()
        tool = canvas.mapTool() if canvas is not None else None
        package = __name__.rpartition('.')[0]
        if tool is not None and type(tool).__module__.startswith(package + '.'):
            canvas.unsetMapTool(tool)

    def _remove_experimental_toolbar(self):
        # Remove the shared toolbar widget before its menu actions go.
        toolbar_action, self.experimental_toolbar_action = self.experimental_toolbar_action, None
        if toolbar_action is not None:
            _quietly("removing the Experimental toolbar widget",
                     lambda: self.iface.removeToolBarIcon(toolbar_action))
            # The toolbar keeps the QWidgetAction it made for the button.
            _quietly("deleting the Experimental toolbar widget action",
                     lambda: toolbar_action.deleteLater())
        button, self.experimental_tool_button = self.experimental_tool_button, None
        self.experimental_menu = None  # owned by the button
        if _alive(button):
            _quietly("deleting the Experimental button", lambda: button.deleteLater())

    def _remove_actions(self):
        actions, self.actions = self.actions, []
        for action in actions:
            if is_deleted(action):
                continue
            _quietly("removing a plugin menu entry",
                     lambda: self.iface.removePluginMenu(self.menu, action))
            _quietly("removing a toolbar icon", lambda: self.iface.removeToolBarIcon(action))
            # Parented to the main window, so it would outlive the plugin.
            _quietly("deleting an action", lambda: action.deleteLater())
        for attr in self._ACTION_ATTRS:
            setattr(self, attr, None)

    def _remove_translator(self):
        translator, self.translator = self.translator, None
        if translator is not None:
            QCoreApplication.removeTranslator(translator)

    def show_plotter(self):
        """Show the KP Data Plotter dock widget."""
        if not _alive(self.plotter_dock):
            try:
                from .kp_plotter_dockwidget import KpPlotterDockWidget
                self.plotter_dock = KpPlotterDockWidget(self.iface)
            except Exception:
                self.plotter_dock = None
                self._report_open_failure("KP Plot could not be opened." + _PYQTGRAPH_HINT)
                return
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.plotter_dock)
        self.plotter_dock.show()

    def show_depth_profile(self):
        """Show the Depth Profile dock widget."""
        if not _alive(self.depth_profile_dock):
            try:
                from .depth_profile_dockwidget import DepthProfileDockWidget
                self.depth_profile_dock = DepthProfileDockWidget(self.iface)
            except Exception:
                self.depth_profile_dock = None
                self._report_open_failure("Depth Profile could not be opened." + _PYQTGRAPH_HINT)
                return
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.depth_profile_dock)
        self.depth_profile_dock.show()

    def show_workbench(self):
        """Show the Cable Route Workbench dock."""
        if not _alive(self.workbench_dock):
            try:
                from .workbench.workbench_dock import WorkbenchDock
                self.workbench_dock = WorkbenchDock(self.iface)
            except Exception:
                self.workbench_dock = None
                self._report_open_failure("Cable Route Workbench could not be opened.")
                return
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.workbench_dock)
            try:
                self.workbench_dock.apply_saved_window_mode()
            except Exception:
                log_exception("Cable Route Workbench: could not restore the saved window layout")
        self.workbench_dock.show()
        self.workbench_dock.refresh_tree()

    def show_planner(self):
        """Show the spatial Planner dock."""
        if not _alive(self.planner_dock):
            try:
                from .planner.planner_dock import PlannerDock
                self.planner_dock = PlannerDock(self.iface)
            except Exception:
                self.planner_dock = None
                self._report_open_failure("Planner could not be opened.")
                return
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.planner_dock)
        self.planner_dock.show()
        self.planner_dock.refresh()

    def show_burial_planner(self):
        """Show the Burial Planner dock (single instance, raise if open).

        Closing the dock only hides it; showing it again re-arms it.
        """
        if not _alive(self.burial_dock):
            try:
                from .burial.burial_dock import BurialPlannerDock
                self.burial_dock = BurialPlannerDock(self.iface)
            except Exception:
                self.burial_dock = None
                self._report_open_failure("Burial Planner could not be opened.")
                return
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.burial_dock)
            try:
                self.burial_dock.apply_saved_window_mode()
            except Exception:
                log_exception("Burial Planner: could not restore the saved window layout")
        self.burial_dock.show()
        self.burial_dock.refresh()

    def activate_transit_measure_tool(self):
        if not _alive(self.transit_measure_tool):
            try:
                from .maptools.transit_measure_tool import TransitMeasureTool
                self.transit_measure_tool = TransitMeasureTool(self.iface)
            except Exception:
                self.transit_measure_tool = None
                self._report_open_failure("Transit Measure could not be activated.")
                return
        self.iface.mapCanvas().setMapTool(self.transit_measure_tool)
        # If the tool is already active, QGIS may not call QgsMapTool.activate() again.
        # Always ensure the dialog is shown when the toolbar/menu action is triggered.
        try:
            self.transit_measure_tool.show_dialog()
        except Exception:
            log_exception("Transit Measure: could not show the measurement window")
