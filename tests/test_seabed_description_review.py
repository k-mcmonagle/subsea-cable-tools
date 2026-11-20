"""QGIS review dock navigation, linked evidence and edits."""

from unittest.mock import patch

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QMainWindow
from qgis.core import QgsApplication, QgsRectangle
from qgis.gui import QgsMapCanvas

from ..processing.annotation_review_dock import AnnotationReviewDock
from ..processing.subsea_cable_processing_provider import SubseaCableProcessingProvider
from ..qgis_compat import FIELD_TYPE_STRING
from .test_seabed_description_association import labels, leaders, polygons, rows, run


class _Iface:
    def __init__(self, layer):
        self.layer = layer
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)
        self.window.resize(1000, 700)

    def activeLayer(self):
        return self.layer

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas


def test_review_zoom_evidence_and_edits_use_generated_fields_from_same_run(tmp_path):
    poly = polygons(['Polygon((0 0,20 0,20 20,0 20,0 0))', 'Polygon((30 0,50 0,50 20,30 20,30 0))'],
                    [('soil_description', FIELD_TYPE_STRING), ('association_run', FIELD_TYPE_STRING),
                     ('review_status', FIELD_TYPE_STRING)], ['source description', 'source run', 'source review'])
    outputs, context, project, _ = run(labels(), [leaders(['LineString(100 110,0 10)'])], [poly],
                                       OUTPUT_POLYGONS=str(tmp_path / 'review_polygons.gpkg'))
    other, other_context, other_project, _ = run(labels([('Point(100 110)', ['Wrong survey', 0, 'left'])]))
    target = outputs['OUTPUT_POLYGONS']
    for output in [*outputs.values(), *other.values()]:
        project.addMapLayer(output)
    iface = _Iface(target)
    iface.canvas.setDestinationCrs(target.crs())
    iface.canvas.setExtent(QgsRectangle(800, 800, 1100, 1100))
    dock = AnnotationReviewDock(iface, project)
    iface.window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    iface.window.show()
    try:
        QgsApplication.processEvents()
        assert dock.queue.rowCount() == 2
        assert dock._current().states == ('review',)
        assert 'Fine sand' in dock.evidence.toPlainText()
        assert 'Wrong survey' not in dock.evidence.toPlainText()
        assert len(dock.highlights) == 3
        assert iface.canvas.extent().center().x() < 150
        dock.next.click()
        assert dock._current().states == ('unmatched',)
        dock.previous.click()
        dock.use_candidate.click()
        assert dock.editor.toPlainText() == 'Fine sand\nwith shells'
        dock.follow.setChecked(False)
        assert dock.editor.toPlainText() == 'Fine sand\nwith shells'
        dock.apply.click()
        assert dock.queue.rowCount() == 1
        assert dock._current().states == ('unmatched',)
        assert target.commitChanges(), target.commitErrors()
        output = rows(target)[0]
        assert output['soil_description'] == 'source description'
        assert output['review_status'] == 'source review'
        assert output['soil_description_2'] == 'Fine sand\nwith shells'
        assert output['review_status_2'] == 'reviewed'
        assert output['description_status'] == 'review'
        dock.refresh()
        dock.filter_picker.setCurrentIndex(2)
        assert dock.queue.rowCount() == 1 and dock._current().reviewed
    finally:
        dock.shutdown()
        iface.window.close()


def test_review_can_choose_between_conflicting_descriptions():
    text = labels([('Point(100 110)', ['Fine sand', 0, 'left']), ('Point(200 110)', ['Coarse gravel', 0, 'left'])])
    line = leaders(['LineString(100 110,10 10)', 'LineString(200 110,15 10)'])
    outputs, context, project, _ = run(text, [line], [polygons()])
    for output in outputs.values():
        project.addMapLayer(output)
    target = outputs['OUTPUT_POLYGONS']
    iface = _Iface(target)
    dock = AnnotationReviewDock(iface, project)
    try:
        assert dock._current().states == ('multiple_descriptions',)
        assert not dock.use_candidate.isEnabled()
        dock.candidate_picker.setCurrentIndex(dock.candidate_picker.findData('Coarse gravel'))
        dock.use_candidate.click()
        dock.apply.click()
        feature = rows(target)[0]
        assert feature['soil_description'] == 'Coarse gravel'
        assert feature['description_status'] == 'multiple_descriptions'
        assert 'Fine sand' in feature['candidate_text']
    finally:
        dock.shutdown()
        iface.window.close()


def test_run_matching_opens_processing_and_selects_new_loaded_output(tmp_path):
    original, context, project, _ = run(labels())
    old_labels = original['OUTPUT_LABELS']
    project.addMapLayer(old_labels)
    path = str(tmp_path / 'matched_polygons.gpkg')
    new, new_context, new_project, _ = run(labels(), [leaders()], [polygons()], OUTPUT_POLYGONS=path)
    iface = _Iface(old_labels)
    dock = AnnotationReviewDock(iface, project)
    registry = QgsApplication.processingRegistry()
    provider = SubseaCableProcessingProvider()
    assert registry.addProvider(provider)

    def finish(algorithm_id):
        assert algorithm_id == 'subsea_cable_processing:attach_seabed_descriptions'
        project.addMapLayer(new['OUTPUT_LABELS'])
        project.addMapLayer(new['OUTPUT_POLYGONS'])
        return {'OUTPUT_LABELS': new['OUTPUT_LABELS'].id(), 'OUTPUT_POLYGONS': path}

    try:
        assert dock.layer_picker.currentData() == old_labels.id()
        with patch('qgis.processing.execAlgorithmDialog', side_effect=finish) as dialog:
            dock.run_button.click()
            dialog.assert_called_once()
        assert dock.layer_picker.currentData() == new['OUTPUT_POLYGONS'].id()
        with patch('qgis.processing.execAlgorithmDialog', return_value=None):
            dock.run_button.click()
        assert dock.layer_picker.currentData() == new['OUTPUT_POLYGONS'].id()
    finally:
        registry.removeProvider(provider)
        dock.shutdown()
        iface.window.close()
