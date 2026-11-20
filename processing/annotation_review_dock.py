"""Map-linked review for the outputs of Attach descriptions to seabed polygons."""

from collections import defaultdict
from dataclasses import dataclass
import json
import re

from qgis import processing
from qgis.PyQt.QtCore import QAbstractTableModel, QModelIndex, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QComboBox, QDockWidget, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
    QPushButton, QTableView, QVBoxLayout, QWidget,
)
from qgis.core import (
    NULL, QgsApplication, QgsCsException, QgsFeatureRequest, QgsNotSupportedException,
    QgsProcessingContext, QgsProcessingUtils, QgsProject, QgsRectangle, QgsVectorLayer,
)
from qgis.gui import QgsHighlight

from ..qgis_compat import FEATURE_REQUEST_NO_GEOMETRY, GEOMETRY_LINE, GEOMETRY_POINT, GEOMETRY_POLYGON

from .annotation_blocks import normalized_text
from .attach_seabed_descriptions_algorithm import AttachSeabedDescriptionsAlgorithm, POLYGON_FIELDS, REVIEW_FIELDS

OUTPUTS = {
    'Polygons': (GEOMETRY_POLYGON, 'polygon_ref', ('description_status',), POLYGON_FIELDS),
    'Text blocks': (GEOMETRY_POINT, 'block_id', ('text_status', 'target_status'),
                    ('block_id', 'text_status', 'target_status', 'text_reason', 'polygon_refs', 'candidate_text')),
    'Leaders': (GEOMETRY_LINE, 'leader_ref', ('match_status',),
                ('leader_ref', 'match_status', 'match_reason', 'candidate_block_ids', 'polygon_refs')),
}
ALGORITHM_ID = 'subsea_cable_processing:attach_seabed_descriptions'
ATTENTION = {'multiple_descriptions': 0, 'review': 1, 'partial': 2, 'layout_only': 3, 'unmatched': 4}


def _text(value):
    return '' if value is None or value == NULL else str(value)


def _refs(value):
    try:
        refs = json.loads(_text(value))
    except ValueError:
        return ()
    return tuple(ref for ref in refs if isinstance(ref, str)) if isinstance(refs, list) else ()


@dataclass
class _Output:
    layer: QgsVectorLayer
    role: str
    fields: dict
    run: str = ''

    @classmethod
    def from_layer(cls, layer):
        if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
            return None
        names = layer.fields().names()
        for role, (geometry, _, _, required) in OUTPUTS.items():
            if layer.geometryType() != geometry:
                continue
            mapping = dict(zip(names, names))
            if role == 'Polygons':
                # Processing appends the entire generated schema in order. Use
                # that group so a colliding input field is never edited.
                mapping = {}
                for bases in (required + REVIEW_FIELDS, required):
                    for start in range(len(names) - len(bases), -1, -1):
                        actual = names[start:start + len(bases)]
                        if all(re.fullmatch(base + r'(?:_[0-9]+)?', field) for base, field in zip(bases, actual)):
                            mapping = dict(zip(bases, actual))
                            break
                    if mapping:
                        break
            if not all(field in mapping for field in required):
                return None
            result = cls(layer, role, mapping)
            if all(field in mapping for field in REVIEW_FIELDS):
                field = mapping['association_run']
                request = QgsFeatureRequest().setFlags(FEATURE_REQUEST_NO_GEOMETRY).setLimit(1)
                request.setSubsetOfAttributes([field], layer.fields())
                feature = next(layer.getFeatures(request), None)
                token = _text(feature[field]) if feature is not None else ''
                if re.fullmatch(r'[0-9a-f]{32}', token):
                    result.run = token
            return result
        return None

    def value(self, feature, name):
        return _text(feature[self.fields[name]]) if name in self.fields else ''

    def entries(self):
        _, reference, states, _ = OUTPUTS[self.role]
        keys = (reference, *states, 'review_status', 'association_run', 'polygon_refs', 'candidate_block_ids',
                'block_ids', 'leader_refs', 'upstream_leader_ref')
        request = QgsFeatureRequest().setFlags(FEATURE_REQUEST_NO_GEOMETRY)
        request.setSubsetOfAttributes([self.fields[key] for key in keys if key in self.fields], self.layer.fields())
        for feature in self.layer.getFeatures(request):
            if self.run and self.value(feature, 'association_run') != self.run:
                continue
            ref = self.value(feature, reference)
            if not ref:
                continue
            yield _Entry(
                feature.id(), ref, tuple(self.value(feature, key) for key in states),
                self.value(feature, 'review_status') == 'reviewed',
                _refs(self.value(feature, 'polygon_refs')),
                _refs(self.value(feature, 'candidate_block_ids') or self.value(feature, 'block_ids')),
                _refs(self.value(feature, 'leader_refs')), self.value(feature, 'upstream_leader_ref'),
            )


@dataclass
class _Entry:
    fid: int
    ref: str
    states: tuple
    reviewed: bool
    polygons: tuple
    blocks: tuple
    leaders: tuple
    upstream: str


class _Queue(QAbstractTableModel):
    def __init__(self, parent):
        super().__init__(parent)
        self.entries = []

    def set_entries(self, entries):
        self.beginResetModel()
        self.entries = entries
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 2

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == Qt.ItemDataRole.DisplayRole:
            entry = self.entries[index.row()]
            status = ' / '.join(state.replace('_', ' ') for state in entry.states if state != 'not_requested' or len(entry.states) == 1)
            return entry.ref if index.column() == 0 else ('Reviewed' if entry.reviewed else status.capitalize())
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ('Reference', 'Status')[section]
        return None


class AnnotationReviewDock(QDockWidget):
    def __init__(self, iface, project=None):
        super().__init__('Seabed Description Review', iface.mainWindow())
        self.setObjectName('SubseaSeabedDescriptionReview')
        self.iface = iface
        self.project = project if project is not None else QgsProject.instance()
        self.canvas = iface.mapCanvas()
        self.outputs = {}
        self.related = {}
        self.entries = {}
        self.leaders_by_block = defaultdict(list)
        self.highlights = []
        self.focus_boxes = []

        panel = QWidget(self)
        layout = QVBoxLayout(panel)
        self.setWidget(panel)
        self.run_button = QPushButton('Run matching\u2026')
        self.run_button.setToolTip('Choose input layers, tolerances and destinations in the QGIS Processing dialog.')
        layout.addWidget(self.run_button)
        self.layer_picker = QComboBox()
        self.layer_picker.setAccessibleName('Association output')
        self.filter_picker = QComboBox()
        self.filter_picker.setAccessibleName('Review filter')
        self.filter_picker.addItems(['Needs review', 'All results', 'Reviewed'])
        self.refresh_button = QPushButton('Refresh')
        layout.addWidget(self.layer_picker)
        self._row(layout, self.filter_picker, self.refresh_button)

        self.queue = _Queue(self)
        self.table = QTableView()
        self.table.setModel(self.queue)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 2)
        self.previous = QPushButton('Previous')
        self.next = QPushButton('Next')
        self.count = QLabel()
        self.follow = QCheckBox('Follow on map')
        self.follow.setChecked(True)
        self._row(layout, self.previous, self.next, self.count)
        layout.addWidget(self.follow)

        self.evidence = QPlainTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setAccessibleName('Association evidence')
        layout.addWidget(self.evidence, 1)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText('Soil description')
        self.editor.setAccessibleName('Soil description')
        layout.addWidget(self.editor, 1)
        self.candidate_picker = QComboBox()
        self.candidate_picker.setAccessibleName('Candidate descriptions')
        self.candidate_picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.candidate_picker.setMinimumContentsLength(14)
        layout.addWidget(self.candidate_picker)
        self.use_candidate = QPushButton('Use candidate')
        self.apply = QPushButton('Apply review')
        self._row(layout, self.use_candidate, self.apply)
        self.notice = QLabel()
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)

        self.run_button.clicked.connect(self._run_algorithm)
        self.layer_picker.currentIndexChanged.connect(self._load_layer)
        self.filter_picker.currentIndexChanged.connect(self._filter)
        self.refresh_button.clicked.connect(self.refresh)
        self.table.selectionModel().currentRowChanged.connect(self._show_current)
        self.previous.clicked.connect(lambda: self._step(-1))
        self.next.clicked.connect(lambda: self._step(1))
        self.follow.toggled.connect(self._zoom_to_evidence)
        self.candidate_picker.currentIndexChanged.connect(self._candidate_changed)
        self.use_candidate.clicked.connect(self._use_candidate)
        self.apply.clicked.connect(self._apply_review)
        self.project.layersAdded.connect(self._layers_changed)
        self.project.layersRemoved.connect(self._layers_changed)
        self.visibilityChanged.connect(self._visibility_changed)
        self.refresh()

    @staticmethod
    def _row(layout, *widgets):
        row = QHBoxLayout()
        for widget in widgets:
            row.addWidget(widget)
        layout.addLayout(row)

    def _layers_changed(self, *_):
        if self.isVisible():
            self.refresh()
        elif any(self.project.mapLayer(key) is None for key in self.outputs):
            self.outputs.clear()
            self.related.clear()

    def _visibility_changed(self, visible):
        if visible:
            self.refresh()
        else:
            self._clear_highlights()

    def _run_algorithm(self):
        if QgsApplication.processingRegistry().algorithmById(ALGORITHM_ID) is None:
            self.notice.setText('QGIS could not find the seabed description matching algorithm.')
            return
        try:
            results = processing.execAlgorithmDialog(ALGORITHM_ID)
        except QgsNotSupportedException:
            self.notice.setText('Enable the QGIS Processing plugin to run matching from this dock.')
            return
        if not results:
            return
        algorithm = AttachSeabedDescriptionsAlgorithm
        context = QgsProcessingContext()
        context.setProject(self.project)
        for key in (algorithm.OUTPUT_POLYGONS, algorithm.OUTPUT_LABELS, algorithm.OUTPUT_CHECKS):
            destination = results.get(key)
            if isinstance(destination, str) and destination:
                layer = QgsProcessingUtils.mapLayerFromString(destination, context, False)
                if layer is not None and self.project.mapLayer(layer.id()) is not None:
                    self.refresh(layer_id=layer.id())
                    return
        self.refresh()

    def refresh(self, *_, layer_id=None):
        selected = self.layer_picker.currentData()
        current = self._current()
        self.outputs = {}
        for layer in self.project.mapLayers().values():
            output = _Output.from_layer(layer)
            if output is not None:
                self.outputs[layer.id()] = output
        active = self.iface.activeLayer()
        preferred = layer_id or selected or (active.id() if active is not None else None)
        self.layer_picker.blockSignals(True)
        try:
            self.layer_picker.clear()
            for key, output in sorted(self.outputs.items(), key=lambda item: (
                    tuple(OUTPUTS).index(item[1].role), item[1].layer.name().casefold(), item[0])):
                self.layer_picker.addItem(f'{output.role} | {output.layer.name()}', key)
            index = self.layer_picker.findData(preferred)
            if index >= 0:
                self.layer_picker.setCurrentIndex(index)
        finally:
            self.layer_picker.blockSignals(False)
        self._load_layer(keep_fid=current.fid if current is not None and selected == self.layer_picker.currentData() else None)

    def _selected_output(self):
        return self.outputs.get(self.layer_picker.currentData())

    def _load_layer(self, *_args, keep_fid=None):
        output = self._selected_output()
        self.related = {}
        self.entries = {}
        self.leaders_by_block.clear()
        if output:
            if output.run:
                for layer_id in sorted(self.outputs):
                    layer = self.outputs[layer_id]
                    if layer.run == output.run:
                        self.related.setdefault(layer.role, layer)
            self.related[output.role] = output
            for role, layer in self.related.items():
                self.entries[role] = {entry.ref: entry for entry in layer.entries()}
            for entry in self.entries.get('Leaders', {}).values():
                for block in entry.blocks:
                    self.leaders_by_block[block].append(entry.ref)
        self._filter(keep_fid=keep_fid)

    def _filter(self, *_args, keep_fid=None, row=0):
        output = self._selected_output()
        mode = self.filter_picker.currentIndex()
        entries = self.entries.get(output.role, {}).values() if output else ()
        filtered = []
        for entry in entries:
            needs_review = any(state in ATTENTION for state in entry.states) and not entry.reviewed
            if mode == 1 or (mode == 0 and needs_review) or (mode == 2 and entry.reviewed):
                filtered.append(entry)
        if mode == 0:
            filtered.sort(key=lambda entry: min(ATTENTION[state] for state in entry.states if state in ATTENTION))
        self.queue.set_entries(filtered)
        if filtered:
            row = next((i for i, entry in enumerate(filtered) if entry.fid == keep_fid), min(row, len(filtered) - 1))
            self.table.setCurrentIndex(self.queue.index(row, 0))
        else:
            self._show_current()

    def _current(self):
        row = self.table.currentIndex().row()
        return self.queue.entries[row] if 0 <= row < len(self.queue.entries) else None

    def _step(self, delta):
        row = self.table.currentIndex().row() + delta
        if 0 <= row < len(self.queue.entries):
            self.table.setCurrentIndex(self.queue.index(row, 0))
            self.table.scrollTo(self.queue.index(row, 0))

    def _linked(self, output, entry):
        refs = {'Polygons': entry.polygons, 'Text blocks': entry.blocks, 'Leaders': list(entry.leaders)}
        if output.role == 'Text blocks':
            refs['Leaders'].extend(self.leaders_by_block[entry.ref])
        if output.role == 'Leaders' and entry.upstream:
            refs['Leaders'].append(entry.upstream)
        visited = {(output.role, entry.ref)}
        for role, values in refs.items():
            current = self.entries.get(role, {})
            for ref in values:
                if (role, ref) in visited or ref not in current:
                    continue
                visited.add((role, ref))
                item = current[ref]
                if role == 'Leaders' and item.upstream:
                    values.append(item.upstream)
                yield self.related[role], item

    def _candidate_changed(self, *_):
        self.use_candidate.setEnabled(self.apply.isEnabled() and bool(self.candidate_picker.currentData()))

    def _use_candidate(self):
        candidate = self.candidate_picker.currentData()
        if candidate:
            self.editor.setPlainText(candidate)

    def _show_current(self, *_):
        self._clear_highlights()
        output, entry = self._selected_output(), self._current()
        row, size = self.table.currentIndex().row(), len(self.queue.entries)
        self.count.setText(f'{row + 1} of {size}' if size else '0 results')
        self.previous.setEnabled(row > 0)
        self.next.setEnabled(0 <= row < size - 1)
        self.evidence.clear()
        self.editor.clear()
        show_editor = bool(output and output.role == 'Polygons')
        self.editor.setVisible(show_editor)
        self.candidate_picker.clear()
        self.candidate_picker.hide()
        self.apply.setEnabled(False)
        self.use_candidate.setVisible(show_editor)
        self.use_candidate.setEnabled(False)
        self.notice.setText('Run matching and load its outputs to start reviewing.' if not output else
                            'No features in this filter.' if not entry else '')
        if not entry:
            return
        feature = output.layer.getFeature(entry.fid)
        if not feature.isValid():
            self.notice.setText('Feature is unavailable. Refresh the review list.')
            return

        details = []
        for key in ('text_reason', 'match_reason'):
            if output.value(feature, key):
                details.append(output.value(feature, key))
        candidate = output.value(feature, 'candidate_text')
        if candidate:
            details.append(candidate)
        linked, proposals = [], {}
        for other, item in self._linked(output, entry):
            linked_feature = other.layer.getFeature(item.fid)
            if not linked_feature.isValid():
                continue
            linked.append((other, linked_feature))
            detail = other.value(linked_feature, 'assembled_text') or other.value(linked_feature, 'candidate_text')
            if other.role == 'Text blocks' and detail.strip():
                proposals.setdefault(normalized_text(detail), detail)
            if other.role == 'Leaders':
                detail = other.value(linked_feature, 'match_reason')
            status = ' / '.join(item.states)
            details.append(f'{item.ref} ({status})' + (f': {detail}' if detail else ''))
        self.evidence.setPlainText('\n\n'.join(details))
        if output.role == 'Polygons':
            self.editor.setPlainText(output.value(feature, 'soil_description'))
            if candidate and (len(entry.blocks) == 1 or entry.states == ('matched',)):
                proposals = {normalized_text(candidate): candidate}
            if len(proposals) > 1:
                self.candidate_picker.addItem('Choose a candidate')
                self.candidate_picker.show()
            for proposal in proposals.values():
                preview = ' '.join(proposal.split())
                self.candidate_picker.addItem(preview[:56] + ('...' if len(preview) > 56 else ''), proposal)
        editable = bool(output.run and output.layer.supportsEditing())
        self.editor.setReadOnly(not editable)
        self.apply.setEnabled(editable)
        self._candidate_changed()
        if not output.run:
            self.notice.setText('Run the updated tool to link the outputs and record review decisions.')
        elif not editable:
            self.notice.setText('This output layer is read-only.')
        else:
            self.notice.setText('Use Save Layer Edits in QGIS to keep review decisions.')
        if self.isVisible():
            self._map_evidence(output, feature, linked)

    def _map_evidence(self, output, feature, linked):
        boxes, context = [], []
        for other, hit in [(output, feature)] + linked:
            geometry = hit.geometry()
            if geometry.isEmpty():
                continue
            try:
                extent = self.canvas.mapSettings().layerExtentToOutputExtent(other.layer, geometry.boundingBox())
            except QgsCsException:
                continue
            if other.role != 'Polygons':
                boxes.append(extent)
            context.append(extent)
            color = QColor('#00a8d6' if other.role == 'Polygons' else '#ee7818' if other.role == 'Leaders' else '#b94bdb')
            highlight = QgsHighlight(self.canvas, geometry, other.layer)
            highlight.setColor(color)
            color.setAlpha(42)
            highlight.setFillColor(color)
            highlight.setWidth(3)
            highlight.show()
            self.highlights.append(highlight)
        self.focus_boxes = boxes or context
        self._zoom_to_evidence()

    def _zoom_to_evidence(self, *_):
        if not self.follow.isChecked() or not self.focus_boxes:
            return
        extent = QgsRectangle(self.focus_boxes[0])
        for box in self.focus_boxes[1:]:
            extent.combineExtentWith(box)
        span = max(extent.width(), extent.height())
        margin = span * 0.12 if span > 0 else 24 * self.canvas.mapUnitsPerPixel()
        extent.grow(max(margin, 1e-8))
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    def _clear_highlights(self):
        for highlight in self.highlights:
            self.canvas.scene().removeItem(highlight)
        self.highlights.clear()
        self.focus_boxes.clear()

    def _apply_review(self):
        output, entry = self._selected_output(), self._current()
        if not output or not entry or not output.run or not output.layer.supportsEditing():
            return
        layer = output.layer
        feature = layer.getFeature(entry.fid)
        if not feature.isValid() or output.value(feature, 'association_run') != output.run:
            self.notice.setText('Feature is unavailable. Refresh the review list.')
            return
        if not layer.isEditable() and not layer.startEditing():
            self.notice.setText('QGIS could not start editing this output layer.')
            return
        changes = {layer.fields().indexFromName(output.fields['review_status']): 'reviewed'}
        if output.role == 'Polygons':
            description = self.editor.toPlainText().strip()
            if description != output.value(feature, 'soil_description'):
                changes[layer.fields().indexFromName(output.fields['soil_description'])] = description or None
        layer.beginEditCommand('Review seabed description')
        if not layer.changeAttributeValues(entry.fid, changes, skipDefaultValues=True):
            layer.destroyEditCommand()
            self.notice.setText('QGIS could not apply this review.')
            return
        layer.endEditCommand()
        entry.reviewed = True
        self._filter(keep_fid=entry.fid, row=self.table.currentIndex().row())
        self.notice.setText('Review applied. Use Save Layer Edits in QGIS to keep it.')

    def shutdown(self):
        self._clear_highlights()
        self.project.layersAdded.disconnect(self._layers_changed)
        self.project.layersRemoved.disconnect(self._layers_changed)
        self.visibilityChanged.disconnect(self._visibility_changed)
