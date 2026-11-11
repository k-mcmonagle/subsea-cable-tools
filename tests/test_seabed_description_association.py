"""QGIS integration tests using synthetic geometry and text only."""

import json
import hashlib

from qgis.PyQt.QtCore import QEventLoop, QTimer
from qgis.core import (
    NULL, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField,
    QgsGeometry, QgsProcessingContext, QgsProcessingFeedback, QgsProcessingOutputLayerDefinition,
    QgsProcessingUtils, QgsProject, QgsVectorLayer, QgsProcessingAlgRunnerTask, QgsApplication, QgsWkbTypes,
)

from ..processing.attach_seabed_descriptions_algorithm import AttachSeabedDescriptionsAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_STRING


def layer(kind, name, rows, fields=(), crs='EPSG:32631'):
    result = QgsVectorLayer(f'{kind}?crs={crs}', name, 'memory')
    result.dataProvider().addAttributes([QgsField(key, field_type) for key, field_type in fields])
    result.updateFields()
    features = []
    for wkt, attributes in rows:
        feature = QgsFeature(result.fields())
        if wkt:
            feature.setGeometry(QgsGeometry.fromWkt(wkt))
        feature.setAttributes(attributes)
        features.append(feature)
    assert result.dataProvider().addFeatures(features)[0]
    result.updateExtents()
    return result


def labels(rows=None):
    return layer('Point', 'Descriptions', rows or [
        ('Point(100 110)', ['Fine sand', 0, 'left']),
        ('Point(100 100)', ['with shells', 0, 'left']),
    ], [('label_text', FIELD_TYPE_STRING), ('label_rotation', FIELD_TYPE_DOUBLE), ('label_alignment', FIELD_TYPE_STRING)])


def polygons(wkts=None, fields=(), attributes=None):
    wkts = wkts or ['Polygon((0 0,20 0,20 20,0 20,0 0))']
    return layer('MultiPolygon', 'Soil', [(wkt, attributes or []) for wkt in wkts], fields)


def leaders(wkts=None):
    return layer('MultiLineString', 'Leaders', [(wkt, []) for wkt in
                                             (wkts or ['LineString(10 10,100 110)'])])


class Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.messages = []

    def reportError(self, error, fatalError=False):
        self.messages.append(str(error))


def run(text, leader_layers=(), polygon_layers=(), **options):
    algorithm = AttachSeabedDescriptionsAlgorithm()
    algorithm.initAlgorithm()
    context = QgsProcessingContext()
    project = QgsProject()
    context.setProject(project)
    feedback = Feedback()
    parameters = {algorithm.TEXT: text, algorithm.TEXT_FIELD: 'label_text',
                  algorithm.LEADERS: list(leader_layers), algorithm.POLYGONS: list(polygon_layers),
                  algorithm.MAX_LINE_GAP: 15, algorithm.OUTPUT_LABELS: 'TEMPORARY_OUTPUT',
                  algorithm.OUTPUT_POLYGONS: 'TEMPORARY_OUTPUT', algorithm.OUTPUT_CHECKS: 'TEMPORARY_OUTPUT'}
    parameters.update(options)
    result, ok = algorithm.run(parameters, context, feedback)
    assert ok, feedback.messages
    layers = {name: QgsProcessingUtils.mapLayerFromString(value, context) for name, value in result.items()}
    assert all(output and output.isValid() for output in layers.values())
    # Keep project/context ownership alive while the caller inspects outputs.
    return layers, context, project, algorithm


def rows(output):
    return list(output.getFeatures())


def absent(value):
    return value is None or value == NULL


def test_supported_assembly_and_reversed_leader():
    text, poly = labels(), polygons()
    for wkt in ('LineString(10 10,100 110)', 'LineString(100 110,10 10)'):
        outputs, context, project, _ = run(text, [leaders([wkt])], [poly])
        label = rows(outputs['OUTPUT_LABELS'])[0]
        polygon = rows(outputs['OUTPUT_POLYGONS'])[0]
        assert label['assembled_text'] == 'Fine sand\nwith shells'
        assert label['text_status'] == 'supported'
        assert label['target_status'] == 'matched'
        assert polygon['soil_description'] == label['assembled_text']
        assert polygon['description_status'] == 'matched'
        assert rows(outputs['OUTPUT_CHECKS'])[0]['match_status'] == 'matched'
    assert text.featureCount() == 2 and poly.fields().names() == []


def test_labels_only_and_uncertain_auto_spacing():
    outputs, context, project, _ = run(labels())
    assert set(outputs) == {'OUTPUT_LABELS'}
    feature = rows(outputs['OUTPUT_LABELS'])[0]
    assert feature['text_status'] == 'layout_only' and feature['fragment_count'] == 2
    assert feature['target_status'] == 'not_requested'
    outputs, context, project, _ = run(labels(), MAX_LINE_GAP=0)
    feature = rows(outputs['OUTPUT_LABELS'])[0]
    assert feature['text_status'] == 'review'
    assert absent(feature['assembled_text']) and feature['candidate_text'] == 'Fine sand\nwith shells'


def test_overlaps_boundaries_and_outside_targets_are_not_guessed():
    for target, poly, expected in [
        ('0 10', polygons(), 'review'),
        ('0.2 10', polygons(), 'review'),
        ('-0.2 10', polygons(), 'review'),
        ('-10 10', polygons(), 'unmatched'),
        ('10 10', polygons(['Polygon((0 0,20 0,20 20,0 20,0 0))'] * 2), 'review'),
    ]:
        outputs, context, project, _ = run(labels(), [leaders([f'LineString({target},100 110)'])], [poly])
        assert rows(outputs['OUTPUT_CHECKS'])[0]['match_status'] == expected
        assert all(absent(row['soil_description']) for row in rows(outputs['OUTPUT_POLYGONS']))


def test_shared_callout_with_several_leaders_and_multipart_lines():
    poly = polygons(['Polygon((0 0,20 0,20 20,0 20,0 0))', 'Polygon((30 0,50 0,50 20,30 20,30 0))'])
    line = leaders(['MultiLineString((10 10,100 110),(100 110,40 10))'])
    outputs, context, project, _ = run(labels(), [line], [poly])
    assert rows(outputs['OUTPUT_LABELS'])[0]['target_status'] == 'multiple_targets'
    assert outputs['OUTPUT_CHECKS'].featureCount() == 2
    assert all(row['description_status'] == 'matched' for row in rows(outputs['OUTPUT_POLYGONS']))


def test_repeated_descriptions_deduplicate_and_conflicts_stay_candidates():
    for second, expected in [(' fine   sand ', 'matched'), ('Coarse gravel', 'multiple_descriptions')]:
        text = labels([('Point(100 110)', ['Fine sand', 0, 'left']), ('Point(200 110)', [second, 0, 'left'])])
        line = leaders(['LineString(10 10,100 110)', 'LineString(200 110,12 10)'])
        outputs, context, project, _ = run(text, [line], [polygons()])
        row = rows(outputs['OUTPUT_POLYGONS'])[0]
        assert row['description_status'] == expected
        if expected == 'matched':
            assert row['soil_description'] == 'Fine sand'
        else:
            assert absent(row['soil_description']) and 'Coarse gravel' in row['candidate_text']


def test_existing_fields_and_colliding_fids_are_preserved():
    poly = polygons(fields=[('soil_description', FIELD_TYPE_STRING), ('fid', FIELD_TYPE_INT),
                            ('source_fid', FIELD_TYPE_INT)], attributes=['existing interpretation', 21, 35])
    outputs, context, project, _ = run(labels(), [leaders()], [poly])
    row = rows(outputs['OUTPUT_POLYGONS'])[0]
    assert row['soil_description'] == 'existing interpretation'
    assert row['soil_description_2'] == 'Fine sand\nwith shells'
    assert row['source_fid'] == 21 and row['source_fid_2'] == 35
    assert next(poly.getFeatures())['soil_description'] == 'existing interpretation'


def test_invalid_polygon_and_bad_text_remain_reviewable():
    poly = polygons(['Polygon((0 0,20 20,0 20,20 0,0 0))'])
    before = next(poly.getFeatures()).geometry().asWkt()
    outputs, context, project, _ = run(labels(), [leaders(['LineString(10 2,100 110)'])], [poly])
    row = rows(outputs['OUTPUT_POLYGONS'])[0]
    assert row['description_status'] == 'review' and absent(row['soil_description'])
    assert next(poly.getFeatures()).geometry().asWkt() == before
    text = labels([(None, ['Unlocated text', 0, 'left']), ('Point(100 110)', ['', 0, 'left'])])
    outputs, context, project, _ = run(text)
    assert outputs['OUTPUT_LABELS'].featureCount() == 2
    assert all(row['text_status'] == 'review' for row in rows(outputs['OUTPUT_LABELS']))


def test_competing_text_ends_are_flagged_and_traceable():
    text = labels([('Point(100 110)', ['One', 0, 'left']), ('Point(200 110)', ['Two', 0, 'left'])])
    outputs, context, project, _ = run(text, [leaders(['LineString(100 110,200 110)'])], [polygons()])
    check = rows(outputs['OUTPUT_CHECKS'])[0]
    assert check['match_status'] == 'review' and len(json.loads(check['candidate_block_ids'])) == 2
    assert all(row['target_status'] == 'review' for row in rows(outputs['OUTPUT_LABELS']))


def test_layout_only_group_does_not_gain_polygon_certainty_from_loose_anchor_tolerance():
    text = labels([('Point(100 110)', ['Fine sand', 0, 'left']), ('Point(100 109.5)', ['with shells', 0, 'left'])])
    outputs, context, project, _ = run(text, [leaders()], [polygons()], MAX_LINE_GAP=1)
    assert rows(outputs['OUTPUT_LABELS'])[0]['text_status'] == 'layout_only'
    polygon = rows(outputs['OUTPUT_POLYGONS'])[0]
    assert polygon['description_status'] == 'review' and absent(polygon['soil_description'])


def test_different_layer_crs_and_geographic_preference_are_supported():
    text, line, poly = labels(), leaders(), polygons()
    to_wgs84 = QgsCoordinateTransform(line.crs(), QgsCoordinateReferenceSystem('EPSG:4326'), QgsProject.instance())
    changed = []
    for row in line.getFeatures():
        geometry = row.geometry()
        geometry.transform(to_wgs84)
        changed.append((geometry.asWkt(), []))
    line = layer('MultiLineString', 'Geographic leaders', changed, crs='EPSG:4326')
    outputs, context, project, _ = run(text, [line], [poly])
    assert rows(outputs['OUTPUT_POLYGONS'])[0]['description_status'] == 'matched'
    outputs, context, project, _ = run(text, [line], [poly], WORKING_CRS='EPSG:4326')
    assert rows(outputs['OUTPUT_POLYGONS'])[0]['description_status'] == 'matched'
    assert outputs['OUTPUT_LABELS'].crs() == text.crs()


def test_output_label_postprocessor_lifetime_and_name():
    project = QgsProject()
    # Exercise native Processing ownership: algorithm.run clones/destroys its
    # execution instance before the postprocessor is used on the GUI thread.
    outputs, context, retained_project, algorithm = run(labels(), OUTPUT_LABELS=QgsProcessingOutputLayerDefinition('TEMPORARY_OUTPUT', project))
    output = outputs['OUTPUT_LABELS']
    details = context.layerToLoadOnCompletionDetails(output.id())
    postprocessor = details.postProcessor()
    assert postprocessor is not None
    postprocessor.postProcessLayer(output, context, QgsProcessingFeedback())
    assert output.labelsEnabled() and output.labeling().settings().fieldName == 'assembled_text'
    assert output.renderer().classAttribute() == 'text_status'
    assert algorithm.groupId() == 'other_tools' and algorithm.displayName().endswith('(experimental)')


def test_saved_geopackage_keeps_long_unicode_text_and_polygon_z(tmp_path):
    long_text = 'Fine sand – café 😀\n' + 'shell fragments ' * 100
    text = labels([('Point(100 110)', [long_text, 0, 'left'])])
    poly = layer('MultiPolygonZ', '3D soil', [('Polygon Z((0 0 -20,20 0 -20,20 20 -20,0 20 -20,0 0 -20))', [])])
    paths = {'OUTPUT_LABELS': str(tmp_path / 'labels.gpkg'),
             'OUTPUT_POLYGONS': str(tmp_path / 'polygons.gpkg'),
             'OUTPUT_CHECKS': str(tmp_path / 'checks.gpkg')}
    outputs, context, project, _ = run(text, [leaders()], [poly], **paths)
    assert rows(outputs['OUTPUT_LABELS'])[0]['assembled_text'] == long_text.strip()
    result = rows(outputs['OUTPUT_POLYGONS'])[0]
    assert result['soil_description'] == long_text.strip()
    assert QgsWkbTypes.hasZ(result.geometry().wkbType())
    assert next(result.geometry().vertices()).z() == -20


def test_geographic_text_auto_uses_metric_crs_and_preserves_output_crs():
    original = labels()
    transform = QgsCoordinateTransform(original.crs(), QgsCoordinateReferenceSystem('EPSG:4326'), QgsProject.instance())
    transformed = []
    for feature in original.getFeatures():
        geometry = feature.geometry()
        geometry.transform(transform)
        transformed.append((geometry.asWkt(17), feature.attributes()))
    text = layer('Point', 'Geographic text', transformed,
                 [(f.name(), f.type()) for f in original.fields()], crs='EPSG:4326')
    outputs, context, project, _ = run(text, [leaders()], [polygons()])
    assert outputs['OUTPUT_LABELS'].crs().authid() == 'EPSG:4326'
    assert rows(outputs['OUTPUT_POLYGONS'])[0]['description_status'] == 'matched'


def test_background_task_keeps_postprocessor_alive_on_final_context():
    algorithm = AttachSeabedDescriptionsAlgorithm()
    algorithm.initAlgorithm()
    text = labels()
    project = QgsProject()
    context = QgsProcessingContext()
    context.setProject(project)
    feedback = Feedback()
    params = {algorithm.TEXT: text, algorithm.TEXT_FIELD: 'label_text', algorithm.MAX_LINE_GAP: 15,
              algorithm.OUTPUT_LABELS: QgsProcessingOutputLayerDefinition('TEMPORARY_OUTPUT', project)}
    loop, timer, outcome = QEventLoop(), QTimer(), []
    task = QgsProcessingAlgRunnerTask(algorithm, params, context, feedback)

    def complete(ok, results):
        outcome.append((ok, results))
        loop.quit()

    task.executed.connect(complete)
    timer.setSingleShot(True)
    timer.timeout.connect(feedback.cancel)
    timer.start(30000)
    QgsApplication.taskManager().addTask(task)
    loop.exec()
    timer.stop()
    assert outcome and outcome[0][0], feedback.messages
    output = QgsProcessingUtils.mapLayerFromString(outcome[0][1]['OUTPUT_LABELS'], context)
    processor = context.layerToLoadOnCompletionDetails(output.id()).postProcessor()
    processor.postProcessLayer(output, context, feedback)
    assert output.labelsEnabled() and output.renderer().classAttribute() == 'text_status'


def test_cancel_and_shapefile_rejection_are_explicit(tmp_path):
    algorithm = AttachSeabedDescriptionsAlgorithm()
    algorithm.initAlgorithm()
    text = labels()
    params = {algorithm.TEXT: text, algorithm.TEXT_FIELD: 'label_text',
              algorithm.OUTPUT_LABELS: str(tmp_path / 'unsafe.shp')}
    feedback = Feedback()
    _, ok = algorithm.run(params, QgsProcessingContext(), feedback)
    assert not ok and any('truncate' in message for message in feedback.messages)
    assert not (tmp_path / 'unsafe.shp').exists()
    feedback = Feedback()
    feedback.cancel()
    _, ok = algorithm.run(params, QgsProcessingContext(), feedback)
    assert not ok and any('canceled' in message for message in feedback.messages)


def test_repaired_polygon_collection_keeps_candidate_but_requires_review():
    poly = polygons(['Polygon((0 0,20 0,20 20,10 20,10 30,10 20,0 20,0 0))'])
    outputs, context, project, _ = run(labels(), [leaders()], [poly])
    polygon = rows(outputs['OUTPUT_POLYGONS'])[0]
    assert polygon['description_status'] == 'review'
    assert polygon['candidate_text'] == 'Fine sand\nwith shells'
    assert absent(polygon['soil_description'])
    assert rows(outputs['OUTPUT_CHECKS'])[0]['match_status'] == 'review'


def test_unsafe_coordinates_are_review_rows_not_native_index_inputs():
    text = labels([('Point(1e100 0)', ['Unsafe position', 0, 'left'])])
    outputs, context, project, _ = run(text)
    row = rows(outputs['OUTPUT_LABELS'])[0]
    assert row['text_status'] == 'review' and row.geometry().isNull()
    assert row['candidate_text'] == 'Unsafe position'


def test_output_cannot_replace_input_or_another_output(tmp_path):
    path = tmp_path / 'input.gpkg'
    outputs, context, project, _ = run(labels(), OUTPUT_LABELS=str(path))
    text = outputs['OUTPUT_LABELS']
    before = hashlib.sha256(path.read_bytes()).digest()
    algorithm = AttachSeabedDescriptionsAlgorithm()
    algorithm.initAlgorithm()
    feedback = Feedback()
    params = {algorithm.TEXT: text, algorithm.TEXT_FIELD: 'assembled_text', algorithm.OUTPUT_LABELS: str(path)}
    _, ok = algorithm.run(params, QgsProcessingContext(), feedback)
    assert not ok and any('input data file' in message for message in feedback.messages)
    assert hashlib.sha256(path.read_bytes()).digest() == before
    feedback = Feedback()
    text, poly = labels(), polygons()
    params = {algorithm.TEXT: text, algorithm.TEXT_FIELD: 'label_text', algorithm.POLYGONS: [poly],
              algorithm.OUTPUT_LABELS: str(tmp_path / 'both.gpkg'), algorithm.OUTPUT_POLYGONS: str(tmp_path / 'both.gpkg')}
    _, ok = algorithm.run(params, QgsProcessingContext(), feedback)
    assert not ok and any('separate files' in message for message in feedback.messages)
    assert not (tmp_path / 'both.gpkg').exists()
