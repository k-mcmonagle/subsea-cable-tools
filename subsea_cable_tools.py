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
# Import your plugin’s main dialog (if you have one)
from .subsea_cable_tools_dialog import SubseaCableToolsDialog
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
        self.first_start = None

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
        self.live_data_manager_dialog = None
        self.live_data_dock = None
        self.live_data_cards_dock = None
        self.live_data_plots_dock = None
        self.live_data_table_dock = None
        self.live_data_action = None
        self.transit_measure_action = None
        self.transit_measure_tool = None
        self.sld_dock = None
        self.sld_action = None
        self.rpl_manager_dock = None
        self.rpl_manager_action = None
        self.cable_manager_dock = None
        self.cable_manager_action = None

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

        # Cable Manager Tool action
        cable_manager_icon_path = os.path.join(self.plugin_dir, 'cable_manager_icon.png')
        if os.path.exists(cable_manager_icon_path):
            cable_manager_icon = QIcon(cable_manager_icon_path)
        else:
            # Fallback to plugin resource icon
            cable_manager_icon = QIcon(":/plugins/subsea_cable_tools/icon.png")
        self.cable_manager_action = QAction(cable_manager_icon, "Cable Manager", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.cable_manager_action.triggered.connect(self.show_cable_manager)
        self.iface.addToolBarIcon(self.cable_manager_action)
        self.iface.addPluginToMenu(self.menu, self.cable_manager_action)
        self.actions.append(self.cable_manager_action)

        # RPL Manager Tool action
        rpl_icon_path = os.path.join(self.plugin_dir, 'rpl_manager_icon.png')
        if os.path.exists(rpl_icon_path):
            rpl_icon = QIcon(rpl_icon_path)
        else:
            # Fallback to plugin resource icon
            rpl_icon = QIcon(":/plugins/subsea_cable_tools/icon.png")
        self.rpl_manager_action = QAction(rpl_icon, "RPL Manager", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.rpl_manager_action.triggered.connect(self.show_rpl_manager)
        self.iface.addToolBarIcon(self.rpl_manager_action)
        self.iface.addPluginToMenu(self.menu, self.rpl_manager_action)
        self.actions.append(self.rpl_manager_action)

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

        # Live Data Tool action
        # (moved to end of toolbar order)

        # Add action for Catenary Calculator (unchanged)
        icon_path = os.path.join(self.plugin_dir, 'catenary_icon.png')
        self.catenary_action = QAction(QIcon(icon_path), "Catenary Calculator", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
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

        # NOTE: The legacy standalone SLD dock is no longer exposed as a separate tool.
        # The SLD UI is embedded inside the RPL Manager.

        # Live Data Tool action (placed last in toolbar order)
        live_data_icon_path = os.path.join(self.plugin_dir, 'live_data_icon.png')
        if os.path.exists(live_data_icon_path):
            live_data_icon = QIcon(live_data_icon_path)
        else:
            # Fallback to plugin resource icon
            live_data_icon = QIcon(":/plugins/subsea_cable_tools/icon.png")
        self.live_data_action = QAction(live_data_icon, "Live Data", self.iface.mainWindow() if hasattr(self.iface, 'mainWindow') else None)
        self.live_data_action.triggered.connect(self.show_live_data)
        self.iface.addToolBarIcon(self.live_data_action)
        self.iface.addPluginToMenu(self.menu, self.live_data_action)
        self.actions.append(self.live_data_action)

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

        self.first_start = True

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
        self.catenary_calculator_v2_dialog.activateWindow()

        self.first_start = True

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

        # Clean up SLD dock
        if hasattr(self, 'sld_dock') and self.sld_dock:
            try:
                if hasattr(self.sld_dock, 'cleanup_plot_and_marker'):
                    self.sld_dock.cleanup_plot_and_marker()
            except Exception:
                pass
            try:
                if hasattr(self.sld_dock, 'cleanup_matplotlib_resources_on_close'):
                    self.sld_dock.cleanup_matplotlib_resources_on_close()
            except Exception:
                pass
            try:
                self.iface.removeDockWidget(self.sld_dock)
            except Exception:
                pass
            try:
                self.sld_dock.deleteLater()
            except Exception:
                pass
            self.sld_dock = None

        # Clean up RPL Manager dock
        if hasattr(self, 'rpl_manager_dock') and self.rpl_manager_dock:
            try:
                self.iface.removeDockWidget(self.rpl_manager_dock)
            except Exception:
                pass
            try:
                self.rpl_manager_dock.deleteLater()
            except Exception:
                pass
            self.rpl_manager_dock = None

        # Clean up Cable Manager dock
        if hasattr(self, 'cable_manager_dock') and self.cable_manager_dock:
            try:
                self.iface.removeDockWidget(self.cable_manager_dock)
            except Exception:
                pass
            try:
                self.cable_manager_dock.deleteLater()
            except Exception:
                pass
            self.cable_manager_dock = None

        # Clean up live data manager dialog
        if hasattr(self, 'live_data_manager_dialog') and self.live_data_manager_dialog:
            try:
                # Call force cleanup to disconnect all signals
                if hasattr(self.live_data_manager_dialog, 'force_cleanup'):
                    self.live_data_manager_dialog.force_cleanup()
            except Exception as e:
                print(f"DEBUG: Error in live_data_manager_dialog force_cleanup: {e}")
            try:
                # Disconnect all signals to prevent Qt from trying to emit during reload
                self.live_data_manager_dialog.blockSignals(True)
            except Exception:
                pass
            try:
                self.live_data_manager_dialog.close()
            except Exception:
                pass
            try:
                self.live_data_manager_dialog.deleteLater()
            except Exception:
                pass
            self.live_data_manager_dialog = None
        
        # Clean up live data dock (MAIN dock with worker thread)
        if hasattr(self, 'live_data_dock') and self.live_data_dock:
            try:
                # Call force cleanup to stop the worker thread and disconnect signals
                if hasattr(self.live_data_dock, 'force_cleanup'):
                    self.live_data_dock.force_cleanup()
                print("DEBUG: live_data_dock force_cleanup completed")
            except Exception as e:
                print(f"DEBUG: Error in live_data_dock force_cleanup: {e}")
            try:
                self.live_data_dock.blockSignals(True)
                self.iface.removeDockWidget(self.live_data_dock)
            except Exception:
                pass
            try:
                self.live_data_dock.deleteLater()
            except Exception:
                pass
            self.live_data_dock = None
        
        # Clean up live data cards dock
        if hasattr(self, 'live_data_cards_dock') and self.live_data_cards_dock:
            try:
                # Call force cleanup to disconnect all signals
                if hasattr(self.live_data_cards_dock, 'force_cleanup'):
                    self.live_data_cards_dock.force_cleanup()
            except Exception as e:
                print(f"DEBUG: Error in live_data_cards_dock force_cleanup: {e}")
            try:
                self.live_data_cards_dock.blockSignals(True)
                self.iface.removeDockWidget(self.live_data_cards_dock)
            except Exception:
                pass
            try:
                self.live_data_cards_dock.deleteLater()
            except Exception:
                pass
            self.live_data_cards_dock = None
        
        # Clean up live data plots dock (has matplotlib - extra important to cleanup)
        if hasattr(self, 'live_data_plots_dock') and self.live_data_plots_dock:
            try:
                # Call cleanup if available
                if hasattr(self.live_data_plots_dock, 'grid_widget') and self.live_data_plots_dock.grid_widget:
                    if hasattr(self.live_data_plots_dock.grid_widget, 'cleanup_all'):
                        print("DEBUG: Cleaning up matplotlib resources in live_data_plots_dock...")
                        self.live_data_plots_dock.grid_widget.cleanup_all()
            except Exception as e:
                print(f"DEBUG: Error cleaning up plot widget resources: {e}")
            try:
                self.live_data_plots_dock.blockSignals(True)
                self.iface.removeDockWidget(self.live_data_plots_dock)
            except Exception:
                pass
            try:
                self.live_data_plots_dock.deleteLater()
            except Exception:
                pass
            self.live_data_plots_dock = None
        
        # Clean up live data table dock
        if hasattr(self, 'live_data_table_dock') and self.live_data_table_dock:
            try:
                # Call force cleanup to disconnect all signals
                if hasattr(self.live_data_table_dock, 'force_cleanup'):
                    self.live_data_table_dock.force_cleanup()
            except Exception as e:
                print(f"DEBUG: Error in live_data_table_dock force_cleanup: {e}")
            try:
                self.live_data_table_dock.blockSignals(True)
                self.iface.removeDockWidget(self.live_data_table_dock)
            except Exception:
                pass
            try:
                self.live_data_table_dock.deleteLater()
            except Exception:
                pass
            self.live_data_table_dock = None

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

        # Remove live data action
        if hasattr(self, 'live_data_action') and self.live_data_action:
            try:
                self.iface.removeToolBarIcon(self.live_data_action)
            except Exception:
                pass
            try:
                self.iface.removePluginMenu(self.menu, self.live_data_action)
            except Exception:
                pass
            self.live_data_action = None

        # Remove cable manager action
        if hasattr(self, 'cable_manager_action') and self.cable_manager_action:
            try:
                self.iface.removeToolBarIcon(self.cable_manager_action)
            except Exception:
                pass
            try:
                self.iface.removePluginMenu(self.menu, self.cable_manager_action)
            except Exception:
                pass
            self.cable_manager_action = None

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

    def show_live_data(self):
        """Show the Live Data manager dialog which controls all sub-widgets."""

        try:
            from .live_data.live_data_dockwidget import LiveDataDockWidget
            from .live_data.live_data_cards_dockwidget import LiveDataCardsDockWidget
            from .live_data.live_data_plots_dockwidget import LiveDataPlotsDockWidget
            from .live_data.live_data_table_dockwidget import LiveDataTableDockWidget
            from .live_data.live_data_control_dialog import LiveDataControlDialog
        except Exception as e:
            from qgis.PyQt.QtWidgets import QMessageBox

            QMessageBox.critical(
                self.iface.mainWindow(),
                "Subsea Cable Tools",
                "Live Data could not be opened. This tool requires optional plotting dependencies (e.g. matplotlib).\n\n"
                f"Details: {e}",
            )
            return
        
        # Create all dock widgets on first call (lazy initialization)
        if not self.live_data_dock:
            self.live_data_dock = LiveDataDockWidget(self.iface)
            # Add as floating window (not docked) to avoid crowding the right panel
            self.iface.addDockWidget(Qt.BottomDockWidgetArea, self.live_data_dock)
            self.live_data_dock.setFloating(True)
            # Start hidden - only show when checkbox is checked in manager
            self.live_data_dock.hide()
        
        if not self.live_data_cards_dock:
            self.live_data_cards_dock = LiveDataCardsDockWidget()
            # Add as floating window (not docked) to avoid crowding the right panel
            self.iface.addDockWidget(Qt.BottomDockWidgetArea, self.live_data_cards_dock)
            self.live_data_cards_dock.setFloating(True)
            # Start hidden - only show when checkbox is checked in manager
            self.live_data_cards_dock.hide()
            
            # Wire signals between dockwidgets
            self.live_data_dock.headers_received.connect(
                self.live_data_cards_dock.set_available_headers
            )
            self.live_data_dock.data_received_raw.connect(
                self.live_data_cards_dock.update_all_cards
            )
            self.live_data_dock.connected_state_changed.connect(
                self.live_data_cards_dock.set_connected
            )
        
        if not self.live_data_plots_dock:
            print("DEBUG: Creating LiveDataPlotsDockWidget")
            try:
                self.live_data_plots_dock = LiveDataPlotsDockWidget()
                print(f"DEBUG: LiveDataPlotsDockWidget instantiated successfully")
            except Exception as e:
                print(f"ERROR: Failed to create LiveDataPlotsDockWidget: {e}")
                import traceback
                traceback.print_exc()
                return
            
            # Add as floating window (not docked) to avoid crowding the right panel
            self.iface.addDockWidget(Qt.BottomDockWidgetArea, self.live_data_plots_dock)
            self.live_data_plots_dock.setFloating(True)
            print(f"DEBUG: LiveDataPlotsDockWidget created and added. Object: {self.live_data_plots_dock}")
            
            # Wire signals between dockwidgets BEFORE hiding
            self.live_data_dock.headers_received.connect(
                self.live_data_plots_dock.set_available_headers
            )
            self.live_data_dock.data_received_raw.connect(
                self.live_data_plots_dock.update_all_plots
            )
            self.live_data_dock.connected_state_changed.connect(
                self.live_data_plots_dock.set_connected
            )
            
            # Start hidden - only show when checkbox is checked in manager
            self.live_data_plots_dock.hide()
            print(f"DEBUG: LiveDataPlotsDockWidget hidden")
        
        if not self.live_data_table_dock:
            print("DEBUG: Creating LiveDataTableDockWidget")
            try:
                self.live_data_table_dock = LiveDataTableDockWidget()
                print(f"DEBUG: LiveDataTableDockWidget instantiated successfully")
            except Exception as e:
                print(f"ERROR: Failed to create LiveDataTableDockWidget: {e}")
                import traceback
                traceback.print_exc()
                return
            
            # Add as floating window (not docked) to avoid crowding the right panel
            self.iface.addDockWidget(Qt.BottomDockWidgetArea, self.live_data_table_dock)
            self.live_data_table_dock.setFloating(True)
            print(f"DEBUG: LiveDataTableDockWidget created and added. Object: {self.live_data_table_dock}")
            
            # Wire signals between dockwidgets BEFORE hiding
            self.live_data_dock.headers_received.connect(
                self.live_data_table_dock.set_available_headers
            )
            self.live_data_dock.data_received_raw.connect(
                self.live_data_table_dock.update_all_values
            )
            self.live_data_dock.connected_state_changed.connect(
                self.live_data_table_dock.set_connected
            )
            
            # Start hidden - only show when checkbox is checked in manager
            self.live_data_table_dock.hide()
            print(f"DEBUG: LiveDataTableDockWidget hidden")
        
        # Create and show the unified control dialog
        if not self.live_data_manager_dialog:
            self.live_data_manager_dialog = LiveDataControlDialog(self.iface.mainWindow())
            
            print(f"DEBUG: Created LiveDataControlDialog")
            
            # Wire control dialog signals to dock widgets
            # Windows visibility control
            self.live_data_manager_dialog.show_cards_widget.connect(self.live_data_cards_dock.show)
            self.live_data_manager_dialog.hide_cards_widget.connect(self.live_data_cards_dock.hide)
            
            self.live_data_manager_dialog.show_plots_widget.connect(self.live_data_plots_dock.show)
            self.live_data_manager_dialog.hide_plots_widget.connect(self.live_data_plots_dock.hide)
            print(f"DEBUG: Connected show/hide_plots_widget signals")
            
            self.live_data_manager_dialog.show_tables_widget.connect(self.live_data_table_dock.show)
            self.live_data_manager_dialog.hide_tables_widget.connect(self.live_data_table_dock.hide)
            print(f"DEBUG: Connected show/hide_tables_widget signals")
            
            # Wire headers to control dialog for field access
            self.live_data_dock.headers_received.connect(
                self.live_data_manager_dialog.set_available_fields
            )
            print(f"DEBUG: Connected headers_received to control dialog")
            
            # Overlays config
            self.live_data_manager_dialog.overlays_config_changed.connect(
                self.on_overlays_config_changed
            )
            
            # Connection control - wire dialog buttons to dock widget
            self.live_data_manager_dialog.connect_requested.connect(self.on_connect_requested)
            self.live_data_manager_dialog.disconnect_requested.connect(self.on_disconnect_requested)

            # Mock/testing
            self.live_data_manager_dialog.mock_start_requested.connect(self.on_mock_start_requested)
            self.live_data_manager_dialog.mock_stop_requested.connect(self.on_mock_stop_requested)

            # Active slot selection
            self.live_data_manager_dialog.active_slot_changed.connect(self.on_active_slot_changed)
            
            # Wire dock widget signals to dialog for status updates
            self.live_data_dock.connected_state_changed.connect(
                lambda connected: self.on_live_data_connection_changed(connected)
            )
            
            # Position floating windows to avoid overlap and not crowd the QGIS interface
            self._arrange_live_data_windows()
        
        # Show control dialog (it will control which windows are visible based on saved state)
        self.live_data_manager_dialog.show()
        self.live_data_manager_dialog.raise_()
        self.live_data_manager_dialog.activateWindow()

    def show_sld(self):
        """Legacy entry point: route to the embedded SLD within RPL Manager."""
        self.show_rpl_manager()
        try:
            # Select the embedded SLD tab if present.
            dock = self.rpl_manager_dock
            if dock and hasattr(dock, "tab_widget"):
                for i in range(dock.tab_widget.count()):
                    if (dock.tab_widget.tabText(i) or "").strip().endswith("SLD"):
                        dock.tab_widget.setCurrentIndex(i)
                        break
        except Exception:
            pass

    def show_cable_manager(self):
        """Show the Cable Manager dock widget (placeholder)."""
        if not self.cable_manager_dock:
            try:
                from .cable_manager.cable_manager_dockwidget import CableManagerDockWidget
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "Cable Manager could not be opened.\n\n"
                    f"Details: {e}",
                )
                return

            self.cable_manager_dock = CableManagerDockWidget(self.iface)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.cable_manager_dock)
        self.cable_manager_dock.show()

    def show_rpl_manager(self):
        """Show the RPL Manager dock widget."""
        if not self.rpl_manager_dock:
            try:
                from .rpl_manager import RplManagerDockWidget
            except Exception as e:
                from qgis.PyQt.QtWidgets import QMessageBox

                QMessageBox.critical(
                    self.iface.mainWindow(),
                    "Subsea Cable Tools",
                    "RPL Manager could not be opened.\n\n"
                    f"Details: {e}",
                )
                return

            self.rpl_manager_dock = RplManagerDockWidget(self.iface)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.rpl_manager_dock)
        self.rpl_manager_dock.show()
    
    def on_connect_requested(self, config: dict):
        """Handle Connect button from control dialog."""
        if not self.live_data_dock:
            return

        self.live_data_dock.connect_slot(config)
    
    def on_disconnect_requested(self, config: dict):
        """Handle Disconnect button from control dialog."""
        print(f"DEBUG: on_disconnect_requested")
        if not self.live_data_dock:
            return
        slot_id = (config or {}).get("slot_id")
        if slot_id:
            self.live_data_dock.disconnect_slot(str(slot_id))

    def on_mock_start_requested(self, config: dict):
        """Start mock playback using dock widget backend."""
        if not self.live_data_dock:
            return

        self.live_data_dock.start_mock_slot(config)

    def on_mock_stop_requested(self, config: dict):
        if not self.live_data_dock:
            return
        slot_id = (config or {}).get("slot_id")
        if slot_id:
            self.live_data_dock.disconnect_slot(str(slot_id))

    def on_overlays_config_changed(self, payload: dict):
        if not self.live_data_dock:
            return
        slot_id = (payload or {}).get("slot_id")
        configs = (payload or {}).get("configs")
        if slot_id:
            self.live_data_dock.set_overlays_config_for_slot(str(slot_id), configs)

    def on_active_slot_changed(self, slot_id: str):
        if self.live_data_dock and slot_id:
            self.live_data_dock.set_active_slot(str(slot_id))
    
    def on_live_data_connection_changed(self, connected: bool):
        """Update control dialog with connection status."""
        if self.live_data_manager_dialog and self.live_data_dock:
            host = self.live_data_dock.host_edit.text() if hasattr(self.live_data_dock, 'host_edit') else None
            port = self.live_data_dock.port_edit.text() if hasattr(self.live_data_dock, 'port_edit') else None
            port_num = None
            if port:
                try:
                    port_num = int(port)
                except ValueError:
                    pass
            self.live_data_manager_dialog.set_connected(connected, host, port_num)
    
    def _arrange_live_data_windows(self):
        """
        Arrange floating Live Data windows in a cascading pattern.
        
        Creates a pleasant layout where windows don't completely overlap,
        and none of them crowd the QGIS interface edges.
        """
        try:
            # Get main window geometry to position windows relative to it
            main_window = self.iface.mainWindow()
            if not main_window:
                return
            
            main_geom = main_window.geometry()
            screen_width = main_geom.width()
            screen_height = main_geom.height()
            
            # Target positions relative to main window center
            # Windows cascade with a slight offset
            offset = 20  # pixels between each window
            base_x = main_geom.x() + screen_width // 3
            base_y = main_geom.y() + 100
            
            # Position each data window with cascading offset
            # Connection UI is now in the Control dialog, so only arrange data windows
            windows = [
                (self.live_data_cards_dock, "Live Data Cards"),
                (self.live_data_plots_dock, "Live Data Plots"),
                (self.live_data_table_dock, "Live Data Tables"),
            ]
            
            for index, (dock_widget, name) in enumerate(windows):
                if dock_widget:
                    x = base_x + (index * offset)
                    y = base_y + (index * offset)
                    dock_widget.move(x, y)
                    # Set reasonable default sizes
                    dock_widget.resize(600, 400)
        except Exception as e:
            # Silently fail if positioning doesn't work - windows will still be visible
            pass

    def run(self):
        """Run method (e.g. open a dialog)."""
        if self.first_start:
            self.first_start = False
            self.dlg = SubseaCableToolsDialog()
        self.dlg.show()
        result = self.dlg.exec_()
        if result:
            # Add additional functionality here if needed.
            pass

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
