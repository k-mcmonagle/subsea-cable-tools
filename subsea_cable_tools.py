# subsea_cable_tools.py
# -*- coding: utf-8 -*-
"""
SubseaCableTools
A QGIS plugin with tools for working with subsea cables.
"""

import os.path

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QMenu, QToolButton

from qgis.core import QgsApplication

from .qgis_compat import QAction, TOOLBUTTON_POPUP_MODE_INSTANT

# Load Qt resources
from .resources import *
# Import the KP Mouse Tool (map tool integration)
from .maptools.kp_mouse_maptool import KPMouseTool

# Import the processing provider
from .processing.subsea_cable_processing_provider import SubseaCableProcessingProvider

# NOTE: Larger dock widgets are imported lazily so plugin startup remains robust
# if an optional or vendored plotting dependency fails to load.


class SubseaCableTools:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        """Constructor.
        :param iface: A QGIS interface instance.
        """
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

        # Localization
        try:
            locale = QSettings().value('locale/userLocale')[0:2]
        except Exception:
            locale = 'en'
        locale_path = os.path.join(self.plugin_dir, 'i18n', f'SubseaCableTools_{locale}.qm')
        if os.path.exists(locale_path):
            try:
                self.translator = QTranslator()
                self.translator.load(locale_path)
                QCoreApplication.installTranslator(self.translator)
            except Exception:
                self.translator = None
        else:
            self.translator = None

        # Core state
        self.actions = []
        self.menu = self.tr(u'&Subsea Cable Tools')

        # Components
        self.kp_mouse_tool = KPMouseTool(self.iface)
        self.kpProvider = SubseaCableProcessingProvider()

        # UI elements (dock widgets / actions)
        self.plotter_dock = None
        self.plotter_action = None
        self.catenary_v2_action = None
        self.catenary_calculator_v2_dialog = None
        self.lay_simulator_action = None
        self.lay_simulator_dialog = None
        self.bu_lowering_action = None
        self.bu_lowering_dialog = None
        self.depth_profile_dock = None
        self.depth_profile_action = None
        self.transit_measure_action = None
        self.transit_measure_tool = None
        self.workbench_dock = None
        self.workbench_action = None
        self.planner_dock = None
        self.planner_action = None
        self.burial_dock = None
        self.burial_action = None
        self.explorer_action = None
        self.explorer_window = None
        self.experimental_menu = None
        self.experimental_tool_button = None
        self.experimental_toolbar_action = None

    def tr(self, message):
        """Return the translation for a string."""
        return QCoreApplication.translate('SubseaCableTools', message)

    def add_action(self, icon_path, text, callback, parent=None, add_to_menu=True):
        """Add a toolbar icon and menu item for an action."""
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        self.iface.addToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action

    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""
        # Register the processing provider (adds your algorithms to the Processing Toolbox)
        QgsApplication.processingRegistry().addProvider(self.kpProvider)

        # Initialize the KP Mouse Tool’s UI elements
        self.kp_mouse_tool.initGui()

        # Add action for the KP Plotter (with icon)
        plot_icon_path = os.path.join(self.plugin_dir, 'kp_plot_icon.png')
        self.plotter_action = QAction(QIcon(plot_icon_path), "KP Plot", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.plotter_action.triggered.connect(self.show_plotter)
        self.iface.addToolBarIcon(self.plotter_action)
        self.iface.addPluginToMenu(self.menu, self.plotter_action)
        self.actions.append(self.plotter_action)

        # Depth Profile Tool action (dedicated icon with resource fallback like other tools)
        depth_icon_path = os.path.join(self.plugin_dir, 'depth_profile_icon.png')
        if os.path.exists(depth_icon_path):
            depth_icon = QIcon(depth_icon_path)
        else:
            # Fallback to plugin resource icon
            depth_icon = QIcon(":/plugins/subsea_cable_tools/icon.png")
        self.depth_profile_action = QAction(depth_icon, "Depth Profile", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.depth_profile_action.triggered.connect(self.show_depth_profile)
        self.iface.addToolBarIcon(self.depth_profile_action)
        self.iface.addPluginToMenu(self.menu, self.depth_profile_action)
        self.actions.append(self.depth_profile_action)

        # Add action for Catenary Calculator V2
        icon_v2_path = os.path.join(self.plugin_dir, 'catenary_icon_v2.png')
        if not os.path.exists(icon_v2_path):
            icon_v2_path = os.path.join(self.plugin_dir, 'catenary_icon.png') # Fallback
        self.catenary_v2_action = QAction(QIcon(icon_v2_path), "Catenary Calculator V2", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.catenary_v2_action.triggered.connect(self.show_catenary_calculator_v2)
        self.iface.addToolBarIcon(self.catenary_v2_action)
        self.iface.addPluginToMenu(self.menu, self.catenary_v2_action)
        self.actions.append(self.catenary_v2_action)

        # Add action for the Cable Lay Simulator (3D) — catenary V3
        icon_v3_path = os.path.join(self.plugin_dir, 'lay_simulator_icon.png')
        if not os.path.exists(icon_v3_path):
            icon_v3_path = os.path.join(self.plugin_dir, 'catenary_icon_v2.png')  # Fallback
        self.lay_simulator_action = QAction(QIcon(icon_v3_path), "Cable Lay Simulator (3D)", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.lay_simulator_action.setToolTip("Cable Lay Simulator (3D): static hang, steady lay with drag, and operation simulation (beta).")
        self.lay_simulator_action.triggered.connect(self.show_lay_simulator)
        self.iface.addPluginToMenu(self.menu, self.lay_simulator_action)
        self.actions.append(self.lay_simulator_action)

        # BU Lowering Tool — the lowering-only BU scenario as its own dialog
        self.bu_lowering_action = QAction(QIcon(icon_v3_path), "BU Lowering Tool (3D)", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.bu_lowering_action.setToolTip("BU Lowering Tool (3D): lower a branching unit on its trunk over two pre-laid legs — quick analytic model with a full-solver verify (beta).")
        self.bu_lowering_action.triggered.connect(self.show_bu_lowering)
        self.iface.addPluginToMenu(self.menu, self.bu_lowering_action)
        self.actions.append(self.bu_lowering_action)

        # Cable Route Workbench (assemblies + RPLs + systems in one dock)
        wb_icon = QIcon(":/plugins/subsea_cable_tools/icon.png")
        wb_icon_path = os.path.join(self.plugin_dir, 'workbench_icon.png')
        if os.path.exists(wb_icon_path):
            wb_icon = QIcon(wb_icon_path)
        self.workbench_action = QAction(wb_icon, "Cable Route Workbench", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.workbench_action.setToolTip("Cable Route Workbench: assemblies, RPLs, fits, and cable systems — with map editing and an SLD.")
        self.workbench_action.triggered.connect(self.show_workbench)
        self.iface.addPluginToMenu(self.menu, self.workbench_action)
        self.actions.append(self.workbench_action)

        # Spatial planning scenario editor and simulator
        planner_icon = QIcon(":/plugins/subsea_cable_tools/icon.png")
        self.planner_action = QAction(
            planner_icon, "Planner",
            self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.planner_action.setToolTip(
            "Build map-linked work plans, simulate vessel progress, and copy tasks to MS Project.")
        self.planner_action.triggered.connect(self.show_planner)
        self.iface.addPluginToMenu(self.menu, self.planner_action)
        self.actions.append(self.planner_action)

        # Burial planning workflow (plough / ROV jet) over an RPL
        burial_icon = QIcon(":/plugins/subsea_cable_tools/icon.png")
        self.burial_action = QAction(
            burial_icon, "Burial Planner (beta)",
            self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.burial_action.setToolTip(
            "Plan cable burial: exclusion criteria over route and survey data, "
            "candidate sections, PLDN/PLUP events, synced map/profile/tables.")
        self.burial_action.triggered.connect(self.show_burial_planner)
        self.iface.addPluginToMenu(self.menu, self.burial_action)
        self.actions.append(self.burial_action)

        # Transit Measure Tool action
        transit_icon_path = os.path.join(self.plugin_dir, 'transit_measure_icon.png')
        if os.path.exists(transit_icon_path):
            transit_icon = QIcon(transit_icon_path)
        else:
            # Fallback to plugin resource icon
            transit_icon = QIcon(":/plugins/subsea_cable_tools/icon.png")
        self.transit_measure_action = QAction(transit_icon, "Transit Measure", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.transit_measure_action.triggered.connect(self.activate_transit_measure_tool)
        self.iface.addToolBarIcon(self.transit_measure_action)
        self.iface.addPluginToMenu(self.menu, self.transit_measure_action)
        self.actions.append(self.transit_measure_action)

        # Cable Lay Data Explorer action (standalone analysis / QC window)
        explorer_icon = QIcon(":/plugins/subsea_cable_tools/icon.png")
        self.explorer_action = QAction(explorer_icon, "Cable Lay Data Explorer", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.explorer_action.triggered.connect(self.show_cable_lay_explorer)
        self.iface.addPluginToMenu(self.menu, self.explorer_action)
        self.actions.append(self.explorer_action)

        self._add_experimental_toolbar_menu()

        # Re-add / repair Cable Route Workbench layers whenever a project is
        # opened, without requiring the workbench dock itself to be opened.
        try:
            self.iface.projectRead.connect(self._restore_workbench_layers)
        except Exception:
            pass
        # The plugin may have been enabled while a project is already open.
        self._restore_workbench_layers()

    def _restore_workbench_layers(self):
        """Self-heal workbench layers for the current project (cheap no-op
        when the project has no workbench GeoPackage)."""
        try:
            from .workbench.project_layers import restore_workbench_layers
            restore_workbench_layers()
        except Exception:
            pass

    def _add_experimental_toolbar_menu(self):
        """Add one toolbar dropdown for tools that are still experimental."""
        parent = self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None
        self.experimental_tool_button = QToolButton(parent)
        self.experimental_tool_button.setObjectName(
            "subseaCableToolsExperimentalButton")
        self.experimental_tool_button.setIcon(
            QIcon(":/plugins/subsea_cable_tools/icon.png"))
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

    def show_catenary_calculator_v2(self):
        if self.catenary_calculator_v2_dialog is None:
            try:
                from .catenary.catenary_calculator_v2_dialog import CatenaryCalculatorV2Dialog
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "Catenary Calculator V2 could not be opened.\n\n"
                    f"Details: {e}",
                )
                return

            self.catenary_calculator_v2_dialog = CatenaryCalculatorV2Dialog(self.iface.mainWindow())
        self.catenary_calculator_v2_dialog.show()
        self.catenary_calculator_v2_dialog.raise_()
        self.catenary_calculator_v2_dialog.activateWindow()

    def show_lay_simulator(self):
        if self.lay_simulator_dialog is None:
            try:
                from .catenary.v3.ui.dialog import LaySimulatorDialog
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "Cable Lay Simulator (3D) could not be opened.\n\n"
                    f"Details: {e}",
                )
                return

            self.lay_simulator_dialog = LaySimulatorDialog(self.iface.mainWindow(), iface=self.iface)
        self.lay_simulator_dialog.show()
        self.lay_simulator_dialog.raise_()
        self.lay_simulator_dialog.activateWindow()

    def show_bu_lowering(self):
        if self.bu_lowering_dialog is None:
            try:
                from .catenary.v3.ui.bu_lowering_dialog import BULoweringDialog
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "BU Lowering Tool (3D) could not be opened.\n\n"
                    f"Details: {e}",
                )
                return

            self.bu_lowering_dialog = BULoweringDialog(self.iface.mainWindow(), iface=self.iface)
        self.bu_lowering_dialog.show()
        self.bu_lowering_dialog.raise_()
        self.bu_lowering_dialog.activateWindow()

    def show_cable_lay_explorer(self):
        """Show the standalone Cable Lay Data Explorer window."""
        if self.explorer_window is None:
            try:
                from .explorer import CableLayExplorerWindow
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "Cable Lay Data Explorer could not be opened. This tool requires "
                    "the bundled pyqtgraph plotting backend.\n\n"
                    f"Details: {e}",
                )
                return

            self.explorer_window = CableLayExplorerWindow(self.iface, self.iface.mainWindow())
        self.explorer_window.show()
        self.explorer_window.raise_()
        self.explorer_window.activateWindow()

    def unload(self):
        """Remove the plugin menu items and icons from QGIS GUI and clean up all resources."""
        # Unregister the processing provider
        if hasattr(self, 'kpProvider') and self.kpProvider:
            QgsApplication.processingRegistry().removeProvider(self.kpProvider)
            self.kpProvider = None

        # Unset the map tool if it is active, then unload the map tool UI
        if hasattr(self, 'kp_mouse_tool') and self.kp_mouse_tool:
            try:
                # Attempt to unset the map tool if it is currently active
                canvas = self.iface.mapCanvas() if hasattr(self.iface, 'mapCanvas') else None
                maptool = getattr(self.kp_mouse_tool, 'mapTool', None)
                if canvas and maptool and canvas.mapTool() == maptool:
                    canvas.unsetMapTool(maptool)
            except Exception:
                pass
            try:
                self.kp_mouse_tool.unload()
            except Exception:
                pass
            self.kp_mouse_tool = None

        # Clean up the plotter dock widget
        if hasattr(self, 'plotter_dock') and self.plotter_dock:
            try:
                # First safely clear plot & marker (no hard scene removals)
                if hasattr(self.plotter_dock, 'cleanup_plot_and_marker'):
                    self.plotter_dock.cleanup_plot_and_marker()
            except Exception:
                pass
            try:
                if hasattr(self.plotter_dock, 'cleanup_matplotlib_resources_on_close'):
                    self.plotter_dock.cleanup_matplotlib_resources_on_close()
            except Exception:
                pass
            try:
                self.iface.removeDockWidget(self.plotter_dock)
            except Exception:
                pass
            try:
                self.plotter_dock.deleteLater()
            except Exception:
                pass
            self.plotter_dock = None

        # Clean up depth profile dock
        if hasattr(self, 'depth_profile_dock') and self.depth_profile_dock:
            try:
                if hasattr(self.depth_profile_dock, 'clear_plot'):
                    self.depth_profile_dock.clear_plot()
            except Exception:
                pass
            try:
                self.iface.removeDockWidget(self.depth_profile_dock)
            except Exception:
                pass
            try:
                self.depth_profile_dock.deleteLater()
            except Exception:
                pass
            self.depth_profile_dock = None

        # Stop restoring workbench layers on project read.
        try:
            self.iface.projectRead.disconnect(self._restore_workbench_layers)
        except Exception:
            pass

        # Clean up the Cable Route Workbench dock
        if getattr(self, 'workbench_dock', None):
            try:
                self.workbench_dock.shutdown()
            except Exception:
                pass
            try:
                self.iface.removeDockWidget(self.workbench_dock)
            except Exception:
                pass
            try:
                self.workbench_dock.deleteLater()
            except Exception:
                pass
            self.workbench_dock = None

        # Clean up the Planner dock and its timer/map items.
        if getattr(self, 'planner_dock', None):
            try:
                self.planner_dock.shutdown()
            except Exception:
                pass
            try:
                self.iface.removeDockWidget(self.planner_dock)
            except Exception:
                pass
            try:
                self.planner_dock.deleteLater()
            except Exception:
                pass
            self.planner_dock = None

        # Clean up the Burial Planner dock and its map items.
        if getattr(self, 'burial_dock', None):
            try:
                self.burial_dock.shutdown()
            except Exception:
                pass
            try:
                self.iface.removeDockWidget(self.burial_dock)
            except Exception:
                pass
            try:
                self.burial_dock.deleteLater()
            except Exception:
                pass
            self.burial_dock = None

        # Clean up the Cable Lay Data Explorer window
        if hasattr(self, 'explorer_window') and self.explorer_window:
            try:
                self.explorer_window.shutdown()
            except Exception:
                pass
            try:
                self.explorer_window.close()
            except Exception:
                pass
            try:
                self.explorer_window.deleteLater()
            except Exception:
                pass
            self.explorer_window = None

        # Remove the shared Experimental toolbar widget before its menu actions.
        if getattr(self, 'experimental_toolbar_action', None):
            try:
                self.iface.removeToolBarIcon(self.experimental_toolbar_action)
            except Exception:
                pass
            self.experimental_toolbar_action = None
        if getattr(self, 'experimental_tool_button', None):
            try:
                self.experimental_tool_button.deleteLater()
            except Exception:
                pass
            self.experimental_tool_button = None
        self.experimental_menu = None

        # Remove actions from menu and toolbar
        if hasattr(self, 'actions'):
            for action in self.actions:
                try:
                    self.iface.removePluginMenu(self.tr(u'&Subsea Cable Tools'), action)
                except Exception:
                    pass
                try:
                    self.iface.removeToolBarIcon(action)
                except Exception:
                    pass
            self.actions = []

        # Remove plotter action
        if hasattr(self, 'plotter_action') and self.plotter_action:
            try:
                self.iface.removeToolBarIcon(self.plotter_action)
            except Exception:
                pass
            try:
                self.iface.removePluginMenu(self.menu, self.plotter_action)
            except Exception:
                pass
            self.plotter_action = None
        # Remove depth profile action
        if hasattr(self, 'depth_profile_action') and self.depth_profile_action:
            try:
                self.iface.removeToolBarIcon(self.depth_profile_action)
            except Exception:
                pass
            try:
                self.iface.removePluginMenu(self.menu, self.depth_profile_action)
            except Exception:
                pass
            self.depth_profile_action = None

        # Remove dialog reference
        if hasattr(self, 'dlg'):
            self.dlg = None

        if hasattr(self, 'catenary_calculator_v2_dialog'):
            self.catenary_calculator_v2_dialog = None

        if hasattr(self, 'lay_simulator_dialog'):
            self.lay_simulator_dialog = None

        if hasattr(self, 'bu_lowering_dialog'):
            self.bu_lowering_dialog = None

        # Remove menu reference
        if hasattr(self, 'menu'):
            self.menu = None

        # Remove iface reference (optional, for safety)
        # self.iface = None

        # Remove translator
        if hasattr(self, 'translator'):
            self.translator = None
    def show_plotter(self):
        """Show the KP Data Plotter dock widget."""
        if not self.plotter_dock:
            try:
                from .kp_plotter_dockwidget import KpPlotterDockWidget
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "KP Plot could not be opened. This tool requires the bundled pyqtgraph plotting backend.\n\n"
                    f"Details: {e}",
                )
                return

            self.plotter_dock = KpPlotterDockWidget(self.iface)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.plotter_dock)
        self.plotter_dock.show()

    def show_depth_profile(self):
        """Show the Depth Profile dock widget."""
        if not self.depth_profile_dock:
            try:
                from .depth_profile_dockwidget import DepthProfileDockWidget
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "Depth Profile could not be opened. This tool requires the bundled pyqtgraph plotting backend.\n\n"
                    f"Details: {e}",
                )
                return

            self.depth_profile_dock = DepthProfileDockWidget(self.iface)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.depth_profile_dock)
        self.depth_profile_dock.show()

    def show_workbench(self):
        """Show the Cable Route Workbench dock."""
        if not self.workbench_dock:
            try:
                from .workbench.workbench_dock import WorkbenchDock
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "Cable Route Workbench could not be opened.\n\n"
                    f"Details: {e}",
                )
                return

            self.workbench_dock = WorkbenchDock(self.iface)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.workbench_dock)
        self.workbench_dock.show()
        self.workbench_dock.refresh_tree()

    def show_planner(self):
        """Show the spatial Planner dock."""
        if not self.planner_dock:
            try:
                from .planner.planner_dock import PlannerDock
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(), "Subsea Cable Tools",
                    "Planner could not be opened.\n\nDetails: %s" % e)
                return
            self.planner_dock = PlannerDock(self.iface)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.planner_dock)
        self.planner_dock.show()
        self.planner_dock.refresh()

    def show_burial_planner(self):
        """Show the Burial Planner dock (single instance, raise if open)."""
        if not self.burial_dock:
            try:
                from .burial.burial_dock import BurialPlannerDock
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(), "Subsea Cable Tools",
                    "Burial Planner could not be opened.\n\nDetails: %s" % e)
                return
            self.burial_dock = BurialPlannerDock(self.iface)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.burial_dock)
        self.burial_dock.show()
        self.burial_dock.refresh()

    def activate_transit_measure_tool(self):
        if self.transit_measure_tool is None:
            try:
                from .maptools.transit_measure_tool import TransitMeasureTool
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "Transit Measure could not be activated.\n\n"
                    f"Details: {e}",
                )
                return

            self.transit_measure_tool = TransitMeasureTool(self.iface)
        self.iface.mapCanvas().setMapTool(self.transit_measure_tool)
        # If the tool is already active, QGIS may not call QgsMapTool.activate() again.
        # Always ensure the dialog is shown when the toolbar/menu action is triggered.
        try:
            self.transit_measure_tool.show_dialog()
        except Exception:
            pass
