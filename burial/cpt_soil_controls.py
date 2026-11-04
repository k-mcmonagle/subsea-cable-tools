"""Optional mapped-soil controls for CPT sections; saved references survive reloads."""
from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QCheckBox, QComboBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from . import numeric_datasets as sources, ui_helpers
from .tabs.attribute_widgets import FieldCombo


class CptSoilControls(QWidget):
    changed = pyqtSignal()

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        settings = settings or {}
        self._ref = dict(settings.get("layer") or {})
        self._offline_layer = None
        self._disposed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.enabled = QCheckBox("Constrain CPT influence to mapped soil units")
        self.enabled.setChecked(bool(settings.get("enabled")))
        layout.addWidget(self.enabled)
        self.options = QWidget()
        form = QFormLayout(self.options)
        form.setContentsMargins(0, 0, 0, 0)
        self.layer = QComboBox()
        self.layer.setToolTip("A mapped soil polygon layer, read live from the project or its saved source.")
        self.field = FieldCombo(settings.get("unit_field") or "", "field identifying the mapped soil unit/class")
        form.addRow("Soil polygon layer", self.layer)
        form.addRow("Soil unit/class field", self.field)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        form.addRow(self.notice)
        form.addRow(_hint(
            "Ranges stop at changes of mapped unit, gaps or conflicting mapping. The CPT and its "
            "connection to the route must share that unit. Adjacent polygons with the same field value "
            "are treated as one unit. This constrains lateral influence; it does not infer geology at depth."))
        layout.addWidget(self.options)
        self._refresh_soon = ui_helpers.coalesced(self, self.refresh_layers)
        self.refresh_layers()
        self.layer.currentIndexChanged.connect(self._layer_changed)
        self.field.currentTextChanged.connect(self._changed)
        self.enabled.toggled.connect(self._changed)
        self._changed()
        project = QgsProject.instance()
        project.layersAdded.connect(self._queue_refresh)
        project.layersRemoved.connect(self._queue_refresh)

    def _queue_refresh(self, *_args):
        if not self._disposed:
            self._refresh_soon()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._refresh_soon._timer.stop()
        project = QgsProject.instance()
        for signal in (project.layersAdded, project.layersRemoved):
            try:
                signal.disconnect(self._queue_refresh)
            except (TypeError, RuntimeError):
                pass
        self._offline_layer = None

    def settings(self):
        return {"enabled": self.enabled.isChecked(), "layer": dict(self._ref), "unit_field": self.field.text()}

    def refresh_layers(self):
        if self._disposed:
            return
        resolved = sources.resolve_layer(self._ref)
        project = QgsProject.instance()
        layers = sorted((l for l in project.mapLayers().values()
                         if isinstance(l, QgsVectorLayer) and l.isValid() and l.geometryType() == 2),
                        key=lambda l: l.name().casefold())
        self.layer.blockSignals(True)
        self.layer.clear()
        self.layer.addItem("(choose a soil polygon layer)", {})
        selected = 0
        for layer in layers:
            self.layer.addItem(layer.name(), sources.layer_ref(layer))
            if resolved is layer:
                selected = self.layer.count() - 1
        if self._ref and not selected:
            name = self._ref.get("layer_name") or "Saved soil layer"
            self.layer.addItem(name + (" (saved source)" if resolved is not None else " (unavailable)"), self._ref)
            selected = self.layer.count() - 1
        self.layer.setCurrentIndex(selected)
        self.layer.blockSignals(False)
        self._offline_layer = resolved  # owns a directly opened layer until refresh/disposal
        self.field.set_layer(resolved)
        self._update_notice(resolved)
        self.changed.emit()

    def _update_notice(self, layer):
        if layer is None:
            text = "Restore the saved soil layer or choose another. Enabled constraints leave coverage empty while it is unavailable."
        elif layer.providerType() == "memory":
            text = "Temporary layer: save it to a file and select the saved layer here before closing QGIS."
        else:
            text = "The saved source is read on reopening; keep it available with this project."
        self.notice.setText(text)
        self.notice.setStyleSheet(ui_helpers.hint_style())

    def _layer_changed(self, *_args):
        self._ref = dict(self.layer.currentData() or {})
        self._offline_layer = sources.resolve_layer(self._ref)
        self.field.set_layer(self._offline_layer)
        self._update_notice(self._offline_layer)
        self.changed.emit()

    def _changed(self, *_args):
        self.options.setVisible(self.enabled.isChecked())
        self.changed.emit()


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(ui_helpers.hint_style())
    return label
