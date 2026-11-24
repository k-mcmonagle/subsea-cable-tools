"""Choose polygon sources and optional classification fields for a BAS draft."""
from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QTreeWidget,
                                 QTreeWidgetItem, QVBoxLayout)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK, GEOMETRY_POLYGON, ITEM_DATA_USER_ROLE


class BasPolygonDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Generate BAS sections")
        layout = QVBoxLayout(self)
        hint = QLabel("Check the polygon layers to intersect within the plan scope (the whole route if "
                      "no scope is set). Expand a layer to choose "
                      "classification fields, or leave its fields unchecked to use its layer name. "
                      "Generated sections are appended to the table for review before Apply changes.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Polygon layers and fields"])
        layout.addWidget(self.tree)
        project = QgsProject.instance()
        for layer in project.layerTreeRoot().layerOrder():
            if not isinstance(layer, QgsVectorLayer) or layer.geometryType() != GEOMETRY_POLYGON:
                continue
            item = QTreeWidgetItem(self.tree, [layer.name()])
            item.setData(0, ITEM_DATA_USER_ROLE, layer.id())
            item.setCheckState(0, Qt.CheckState.Unchecked)
            for field in layer.fields():
                child = QTreeWidgetItem(item, [field.name()])
                child.setCheckState(0, Qt.CheckState.Unchecked)
        self.buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        self.buttons.button(BUTTON_BOX_OK).setText("Generate")
        self.buttons.button(BUTTON_BOX_OK).setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.tree.itemChanged.connect(self._checked)
        self.resize(490, 410)

    def _checked(self, item, _column):
        if item.parent() is not None and item.checkState(0) == Qt.CheckState.Checked:
            item.parent().setCheckState(0, Qt.CheckState.Checked)
        self.buttons.button(BUTTON_BOX_OK).setEnabled(bool(self.sources()))

    def sources(self):
        sources = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.checkState(0) == Qt.CheckState.Checked:
                layer = QgsProject.instance().mapLayer(item.data(0, ITEM_DATA_USER_ROLE))
                if layer is not None:
                    fields = [item.child(j).text(0) for j in range(item.childCount())
                              if item.child(j).checkState(0) == Qt.CheckState.Checked]
                    sources.append((layer, fields))
        return sources
