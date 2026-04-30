# subsea_cable_tools.py
# -*- coding: utf-8 -*-
"""
SubseaCableTools
A QGIS plugin with tools for working with subsea cables.
"""

import os.path

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from qgis.core import QgsApplication

# Load Qt resources
from .resources import *
# Import the KP Mouse Tool (map tool integration)
from .maptools.kp_mouse_maptool import KPMouseTool

# Import the processing provider
from .processing.subsea_cable_processing_provider import SubseaCableProcessingProvider

# NOTE: Some dock widgets depend on optional 3rd-party Python packages (e.g. matplotlib).
# To avoid plugin-load failures on systems missing those deps, we import them lazily
# inside the action handlers (show_* methods) and provide a clear error if unavailable.


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
        self.catenary_action = None
        self.catenary_calculator_dialog = None
        self.catenary_v2_action = None
        self.catenary_calculator_v2_dialog = None
        self.depth_profile_dock = None
        self.depth_profile_action = None
        self.transit_measure_action = None
        self.transit_measure_tool = None

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

        # Add action for Catenary Calculator (legacy v1; planned for removal in 1.6 in favour of V2).
        icon_path = os.path.join(self.plugin_dir, 'catenary_icon.png')
        self.catenary_action = QAction(QIcon(icon_path), "Catenary Calculator (Legacy)", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.catenary_action.setToolTip("Legacy Catenary Calculator. Planned for removal in 1.6; use Catenary Calculator V2.")
        self.catenary_action.triggered.connect(self.show_catenary_calculator)
        self.iface.addToolBarIcon(self.catenary_action)
        self.iface.addPluginToMenu(self.menu, self.catenary_action)
        self.actions.append(self.catenary_action)

        # Add action for Catenary Calculator V2
        icon_v2_path = os.path.join(self.plugin_dir, 'catenary_icon_v2.png')
        if not os.path.exists(icon_v2_path):
            icon_v2_path = os.path.join(self.plugin_dir, 'catenary_icon.png') # Fallback
        self.catenary_v2_action = QAction(QIcon(icon_v2_path), "Catenary Calculator V2", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.catenary_v2_action.triggered.connect(self.show_catenary_calculator_v2)
        self.iface.addToolBarIcon(self.catenary_v2_action)
        self.iface.addPluginToMenu(self.menu, self.catenary_v2_action)
        self.actions.append(self.catenary_v2_action)

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

    def show_catenary_calculator(self):
        if self.catenary_calculator_dialog is None:
            try:
                from .catenary_calculator_dialog import CatenaryCalculatorDialog
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "Catenary Calculator could not be opened.\n\n"
                    f"Details: {e}",
                )
                return

            self.catenary_calculator_dialog = CatenaryCalculatorDialog(self.iface.mainWindow())
        self.catenary_calculator_dialog.show()
        self.catenary_calculator_dialog.raise_()
        self.catenary_calculator_dialog.activateWindow()

    def show_catenary_calculator_v2(self):
        if self.catenary_calculator_v2_dialog is None:
            try:
                from .catenary_calculator_v2_dialog import CatenaryCalculatorV2Dialog
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

        if hasattr(self, 'catenary_calculator_dialog'):
            self.catenary_calculator_dialog = None

        if hasattr(self, 'catenary_calculator_v2_dialog'):
            self.catenary_calculator_v2_dialog = None

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
                    "KP Plot could not be opened. This tool requires optional plotting dependencies (e.g. matplotlib).\n\n"
                    f"Details: {e}",
                )
                return

            self.plotter_dock = KpPlotterDockWidget(self.iface)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.plotter_dock)
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
                    "Depth Profile could not be opened. This tool requires optional plotting dependencies (e.g. matplotlib).\n\n"
                    f"Details: {e}",
                )
                return

            self.depth_profile_dock = DepthProfileDockWidget(self.iface)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.depth_profile_dock)
        self.depth_profile_dock.show()

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
