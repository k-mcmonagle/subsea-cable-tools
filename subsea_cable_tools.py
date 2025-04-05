# subsea_cable_tools.py
# -*- coding: utf-8 -*-
"""
SubseaCableTools
A QGIS plugin with tools for working with subsea cables.
"""

import os.path

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication
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


class SubseaCableTools:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        """
        Constructor.
        :param iface: A QGIS interface instance.
        """
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

        # Set up localization
        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(self.plugin_dir, 'i18n', f'SubseaCableTools_{locale}.qm')
        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        # Declare instance attributes
        self.actions = []
        self.menu = self.tr(u'&Subsea Cable Tools')
        self.first_start = None

        # Initialize the KP Mouse Tool (map tool integration)
        self.kp_mouse_tool = KPMouseTool(self.iface)

        # Initialize the processing provider
        self.kpProvider = SubseaCableProcessingProvider()

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

        # Add the main plugin action (e.g. to open a dialog)
        # icon_path = ':/plugins/subsea_cable_tools/icon.png'
        # self.add_action(
        #     icon_path,
        #     text=self.tr(u'Subsea Cable Tools'),
        #     callback=self.run,
        #     parent=self.iface.mainWindow()
        # )

        self.first_start = True

    def unload(self):
        """Remove the plugin menu items and icons from QGIS GUI."""
        # Unregister the processing provider
        QgsApplication.processingRegistry().removeProvider(self.kpProvider)

        # Unload the map tool UI
        self.kp_mouse_tool.unload()

        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&Subsea Cable Tools'), action)
            self.iface.removeToolBarIcon(action)

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
