# -*- coding: utf-8 -*-

import os

from qgis.PyQt import uic
from qgis.PyQt import QtWidgets

# Loads the .ui file so that PyQt can populate the plugin with the elements from Qt Designer
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'subsea_cable_tools_dialog_base.ui'))


class SubseaCableToolsDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        """Constructor."""
        super(SubseaCableToolsDialog, self).__init__(parent)

        self.setupUi(self)
