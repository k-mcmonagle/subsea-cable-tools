"""Choose polygon sources and optional classification fields for a BAS draft."""
from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QTreeWidget,
                                 QTreeWidgetItem, QVBoxLayout)

from ..qgis_compat import BUTTON_BOX_CANCEL, BUTTON_BOX_OK, GEOMETRY_POLYGON, ITEM_DATA_USER_ROLE


class BasPolygonDialog(QDialog):
    def __init__(self, parent, extent="", full_route=False):
        super().__init__(parent)
        self.setWindowTitle("Generate BAS sections")
        layout = QVBoxLayout(self)
        if extent:
            layout.addWidget(QLabel(extent))
        hint = QLabel("Check the layers to intersect. Expand a layer to choose classification fields, "
                      "or leave its fields unchecked to use its layer name. Review the draft before applying.")
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
        self.buttons.button(BUTTON_BOX_OK).setText("Use full route and generate" if full_route else "Generate")
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


class BasChangesDialog(QDialog):
    """Resolve changes to existing intervals before staging the draft."""

    def __init__(self, parent, existing, conflicts, columns, additions, unchanged):
        super().__init__(parent)
        self.setWindowTitle("Review BAS intervals")
        layout = QVBoxLayout(self)
        hint = QLabel(f"{additions} new · {unchanged} already present · {len(conflicts)} to review. "
                      "Check the changes to stage. Existing notes are retained. Added overlapping rows "
                      "can be reconciled in the table before saving.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Stage change", "Proposed KP", "Polygon values", "Existing intervals"])
        self.tree.setRootIsDecorated(False)
        labels = {c["key"]: c["label"] for c in columns}

        def interval(row):
            return f"{row['start_kp']:.3f}–{row['end_kp']:.3f}"

        def values(row):
            return ", ".join(f"{labels.get(k, k)}: {v}" for k, v in row["values"].items() if v)

        for proposed, hits, exact in conflicts:
            action = "Update values" if exact is not None else "Add overlapping row"
            prior = [f"Row {i + 1} · {interval(existing[i])} · {values(existing[i])}" for i in hits]
            details = " · ".join(filter(None, (values(proposed), proposed.get("notes"))))
            item = QTreeWidgetItem(self.tree, [action, interval(proposed), details, " / ".join(prior)])
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setToolTip(2, details)
            item.setToolTip(3, "\n".join(prior))
        for col in (0, 1):
            self.tree.resizeColumnToContents(col)
        layout.addWidget(self.tree)
        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.button(BUTTON_BOX_OK).setText("Stage draft")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(830, 380)

    def selected(self):
        return [i for i in range(self.tree.topLevelItemCount())
                if self.tree.topLevelItem(i).checkState(0) == Qt.CheckState.Checked]
