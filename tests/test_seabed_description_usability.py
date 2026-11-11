"""Geographic inputs and the actual Processing dialog's default workflow."""

from contextlib import contextmanager
import importlib
import os
import sys

from qgis.core import (
    Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsProcessingContext, QgsProcessingParameterDefinition, QgsProcessingUtils, QgsProject,
)

from ..processing.attach_seabed_descriptions_algorithm import AttachSeabedDescriptionsAlgorithm
from ..qgis_compat import FIELD_TYPE_INT, FIELD_TYPE_STRING, GEOMETRY_POINT, GEOMETRY_LINE
from .test_seabed_description_association import Feedback, labels, layer, leaders, polygons, rows, run


def reproject(source, crs):
    transform = QgsCoordinateTransform(source.crs(), QgsCoordinateReferenceSystem(crs), QgsProject.instance())
    data = []
    for feature in source.getFeatures():
        geometry = feature.geometry()
        geometry.transform(transform)
        data.append((geometry.asWkt(17), feature.attributes()))
    return layer('Point' if source.geometryType() == GEOMETRY_POINT else
                 'MultiLineString' if source.geometryType() == GEOMETRY_LINE else 'MultiPolygon',
                 source.name(), data, [(f.name(), f.type()) for f in source.fields()], crs)


def test_all_geographic_inputs_with_geographic_preference_use_metres():
    text, line, poly = [reproject(source, 'EPSG:4326') for source in (labels(), leaders(), polygons())]
    outputs, context, project, _ = run(text, [line], [poly], TEXT_FIELD='', WORKING_CRS='EPSG:4326')
    assert rows(outputs['OUTPUT_LABELS'])[0]['assembled_text'] == 'Fine sand\nwith shells'
    assert rows(outputs['OUTPUT_POLYGONS'])[0]['description_status'] == 'matched'
    assert all(output.crs().authid() == 'EPSG:4326' for output in outputs.values())
    # 0.2 metre from the boundary must remain a review case in degree inputs.
    boundary_line = reproject(leaders(['LineString(0.2 10,100 110)']), 'EPSG:4326')
    outputs, context, project, _ = run(text, [boundary_line], [poly], TEXT_FIELD='', WORKING_CRS='EPSG:4326')
    assert rows(outputs['OUTPUT_POLYGONS'])[0]['description_status'] == 'review'


def test_non_metre_preference_and_different_geographic_datum_do_not_relabel_inputs():
    for preference in ('EPSG:2263', 'EPSG:4267'):
        outputs, context, project, _ = run(labels(), [leaders()], [polygons()], TEXT_FIELD='', WORKING_CRS=preference)
        assert rows(outputs['OUTPUT_POLYGONS'])[0]['description_status'] == 'matched'
        assert outputs['OUTPUT_LABELS'].crs().authid() == 'EPSG:32631'


def test_automatic_text_field_is_conservative_and_override_is_case_insensitive():
    for name in ('label_text', 'LABEL_TEXT', 'gm_label_text_2', 'Description', 'CustomNotes'):
        text = layer('Point', 'Text', [('Point(100 100)', [7, 'Fine sediment'])],
                     [('ID1', FIELD_TYPE_INT), (name, FIELD_TYPE_STRING)])
        outputs, context, project, _ = run(text, TEXT_FIELD='')
        assert rows(outputs['OUTPUT_LABELS'])[0]['assembled_text'] == 'Fine sediment'
    text = layer('Point', 'Ambiguous text', [('Point(100 100)', ['Original source attribute', 'Decoded description'])],
                 [('label_text', FIELD_TYPE_STRING), ('gm_label_text', FIELD_TYPE_STRING)])
    algorithm = AttachSeabedDescriptionsAlgorithm()
    algorithm.initAlgorithm()
    feedback = Feedback()
    _, ok = algorithm.run({algorithm.TEXT: text, algorithm.OUTPUT_LABELS: 'TEMPORARY_OUTPUT'}, QgsProcessingContext(), feedback)
    assert not ok and any('Several possible text fields' in message for message in feedback.messages)
    outputs, context, project, _ = run(text, TEXT_FIELD='GM_LABEL_TEXT')
    assert rows(outputs['OUTPUT_LABELS'])[0]['assembled_text'] == 'Decoded description'


def test_auto_field_does_not_treat_metadata_or_review_proposals_as_descriptions():
    algorithm = AttachSeabedDescriptionsAlgorithm()
    algorithm.initAlgorithm()
    for name in ('source', 'candidate_text'):
        text = layer('Point', 'Not a description', [('Point(100 100)', ['Not accepted text'])], [(name, FIELD_TYPE_STRING)])
        feedback = Feedback()
        _, ok = algorithm.run({algorithm.TEXT: text, algorithm.OUTPUT_LABELS: 'TEMPORARY_OUTPUT'}, QgsProcessingContext(), feedback)
        assert not ok and any('No description text field' in message for message in feedback.messages)


@contextmanager
def processing_gui():
    """Load QGIS's Processing plugin, avoiding the repository's namesake folder."""
    original_path = list(sys.path)
    saved = {key: sys.modules.pop(key) for key in list(sys.modules)
             if key == 'processing' or key.startswith('processing.')}
    sys.path.insert(0, os.path.join(QgsApplication.prefixPath(), 'python', 'plugins'))
    try:
        yield importlib.import_module('processing.gui.AlgorithmDialog')
    finally:
        sys.path[:] = original_path
        for key in list(sys.modules):
            if key == 'processing' or key.startswith('processing.'):
                del sys.modules[key]
        sys.modules.update(saved)


def test_real_processing_dialog_runs_with_only_a_geographic_text_layer_selected():
    # Three repeated pairs give the automatic spacing estimator enough evidence.
    source = layer('Point', 'Synthetic descriptions', [
        (f'Point({500100 + i * 100} {5600100 + j * 10})', [i * 2 + j, 'upper' if j else 'lower'])
        for i in range(3) for j in range(2)
    ], [('ID1', FIELD_TYPE_INT), ('label_text', FIELD_TYPE_STRING)])
    source = reproject(source, 'EPSG:4326')
    project = QgsProject.instance()
    previous_crs = project.crs()
    project.setCrs(QgsCoordinateReferenceSystem('EPSG:4326'))
    project.addMapLayer(source)
    dialog = None
    try:
        with processing_gui() as module:
            algorithm = AttachSeabedDescriptionsAlgorithm()
            algorithm.initAlgorithm()
            dialog = module.AlgorithmDialog(algorithm)
            dialog.setParameters({algorithm.TEXT: source})
            params = dialog.mainWidget().createProcessingParameters()
            assert not params.get(algorithm.TEXT_FIELD), 'The field picker must not silently select ID1'
            context = QgsProcessingContext()
            context.setProject(project)
            feedback = Feedback()
            result, ok = algorithm.run(params, context, feedback)
            assert ok, feedback.messages
            assert set(result) == {algorithm.OUTPUT_LABELS}
            output = QgsProcessingUtils.mapLayerFromString(result[algorithm.OUTPUT_LABELS], context)
            assert output.featureCount() == 3
            assert {feature['assembled_text'] for feature in output.getFeatures()} == {'upper\nlower'}
            assert output.crs().authid() == 'EPSG:4326'
            try:
                advanced = Qgis.ProcessingParameterFlag.Advanced
            except AttributeError:
                advanced = QgsProcessingParameterDefinition.FlagAdvanced
            for key in (algorithm.WORKING_CRS, algorithm.ROTATION_FIELD, algorithm.ALIGNMENT_FIELD,
                        algorithm.MAX_LINE_GAP, algorithm.ANCHOR_TOLERANCE, algorithm.BOUNDARY_TOLERANCE):
                assert algorithm.parameterDefinition(key).flags() & advanced
            dialog.close()
            dialog.deleteLater()
            QgsApplication.processEvents()
            dialog = None
    finally:
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
            QgsApplication.processEvents()
        project.removeMapLayer(source.id())
        project.setCrs(previous_crs)
