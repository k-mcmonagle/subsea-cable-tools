"""End-to-end translation against shared KP tools and closed-form grid cases."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField,
    QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingException,
    QgsProcessingFeedback, QgsProject, QgsVectorLayer,
)

from ..processing.translate_kp_values_algorithm import TranslateKPValuesAlgorithm
from ..processing.kp_range_csv_algorithm import KPRangeCSVAlgorithm
from ..processing.translate_kp_from_rpl_to_rpl_algorithm import TranslateKPFromRPLToRPLAlgorithm
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_STRING

REQUIRES_QGIS = True


class TranslationQgisTests(unittest.TestCase):
    def setUp(self):
        self.layers = []
        self.old = self.route([(0, 0), (10000, 0)])
        self.new = self.route([(0, 0), (10000, 0)])

    def tearDown(self):
        for layer in self.layers:
            QgsProject.instance().removeMapLayer(layer.id())

    def layer(self, kind, fields=(), rows=(), geometries=(), crs='EPSG:32631'):
        layer = QgsVectorLayer(f'{kind}?crs={crs}', 'translation_test', 'memory')
        layer.dataProvider().addAttributes(list(fields))
        layer.updateFields()
        features = []
        for i in range(max(len(rows), len(geometries))):
            feature = QgsFeature(layer.fields())
            if rows:
                feature.setAttributes(list(rows[i]))
            if geometries:
                feature.setGeometry(geometries[i])
            features.append(feature)
        layer.dataProvider().addFeatures(features)
        QgsProject.instance().addMapLayer(layer)
        self.layers.append(layer)
        return layer

    def route(self, vertices):
        return self.layer('LineString', geometries=[QgsGeometry.fromPolylineXY([
            QgsPointXY(500000 + x, 6000000 + y) for x, y in vertices])])

    def run_algorithm(self, params, algorithm=None, outputs=('RESULTS', 'POINTS', 'RANGES')):
        algorithm = algorithm or TranslateKPValuesAlgorithm()
        algorithm.initAlgorithm({})
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        params = dict(params)
        for key in outputs:
            params[key] = 'memory:'
        valid, error = algorithm.checkParameterValues(params, context)
        self.assertTrue(valid, error)
        results = algorithm.processAlgorithm(params, context, QgsProcessingFeedback())
        return {key: context.takeResultLayer(results[key]) for key in outputs}

    def translate(self, **kwargs):
        params = dict(OLD=self.old, NEW=self.new, OLD_MODE=1, NEW_MODE=1,
                      OLD_GRID='EPSG:32631', NEW_GRID='EPSG:32631',
                      INPUT_MODE=1, PASTE='1\n3', STEP=50, MAX_OFFSET=25, STRETCH=10, ROUNDTRIP=1)
        params.update(kwargs)
        return self.run_algorithm(params)

    def test_paste_points_shift_and_invalid_rows(self):
        out = self.translate(NEW_START=5, PASTE='1\n3\nbad\n11\nnan')
        rows = list(out['RESULTS'].getFeatures())
        self.assertEqual([f['translation_status'] for f in rows], ['accepted'] * 2 + ['invalid'] * 3)
        self.assertAlmostEqual(rows[0]['new_start_kp'], 6, places=8)
        self.assertEqual(out['POINTS'].featureCount(), 2)
        self.assertEqual(out['RANGES'].featureCount(), 0)

    def test_algorithm_run_validation_and_negative_start(self):
        algorithm = TranslateKPValuesAlgorithm()
        algorithm.initAlgorithm({})
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        params = dict(OLD=self.old, NEW=self.new, OLD_MODE=1, NEW_MODE=1,
                      OLD_START=-5, NEW_START=2, INPUT_MODE=1, PASTE='-5\n0\n5\n5.00001',
                      RESULTS='memory:', POINTS='memory:')
        valid, error = algorithm.checkParameterValues(params, context)
        self.assertTrue(valid, error)
        result, ok = algorithm.run(params, context, QgsProcessingFeedback())
        self.assertTrue(ok)
        layer = context.takeResultLayer(result['RESULTS'])
        rows = list(layer.getFeatures())
        self.assertEqual([r['translation_status'] for r in rows], ['accepted'] * 3 + ['invalid'])
        self.assertEqual([round(r['new_start_kp'], 8) for r in rows[:3]], [2, 7, 12])

    def test_mouse_shortcut_passes_current_basis(self):
        from ..maptools.kp_mouse_maptool import KPMouseMapTool
        tool = SimpleNamespace(
            layer=self.old, _route=SimpleNamespace(start_kp_km=12), useCartesian=True,
            distanceArea=SimpleNamespace(grid_crs=lambda: self.old.crs()),
            _locate=lambda point: SimpleNamespace(kp_km=12.3456789))
        with patch('qgis.processing.execAlgorithmDialog') as dialog:
            KPMouseMapTool._show_kp_translation(tool, QgsPointXY(501000, 6000000))
        name, parameters = dialog.call_args.args
        self.assertEqual(name, 'subsea_cable_processing:translate_kp_values')
        self.assertEqual(parameters['OLD_START'], 12)
        self.assertEqual(parameters['OLD_GRID'], 'EPSG:32631')
        self.assertEqual(parameters['OLD_MODE'], 1)
        self.assertEqual(parameters['PASTE'], '12.3456789')

    def test_invalid_geometry_filter_does_not_drop_numeric_rows(self):
        from .test_invalid_geometry_sources import _abort_on_invalid_context
        source = self.layer('LineString', [QgsField('kp', FIELD_TYPE_DOUBLE)], [[1], [2]], [
            QgsGeometry.fromWkt('LINESTRING(500000 6000000,500000 6000000)'), QgsGeometry()])
        algorithm = TranslateKPValuesAlgorithm()
        algorithm.initAlgorithm({})
        context = _abort_on_invalid_context()
        result = algorithm.processAlgorithm(dict(
            OLD=self.old, NEW=self.new, OLD_MODE=1, NEW_MODE=1, INPUT_MODE=0,
            INPUT=source, START_FIELD='kp', RESULTS='memory:'), context, QgsProcessingFeedback())
        rows = list(context.takeResultLayer(result['RESULTS']).getFeatures())
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(f['translation_status'] == 'accepted' for f in rows))

    def test_ranges_reversed_input_zero_length_and_attributes(self):
        out = self.translate(PASTE_TYPE=1, HEADER=True, NEW_START=2,
                             PASTE='start\tend\tgroup\n1\t2\tA\n4\t3\tB\n5\t5\tC')
        rows = list(out['RESULTS'].getFeatures())
        self.assertEqual([f['translation_status'] for f in rows], ['accepted'] * 3)
        self.assertEqual([f['group'] for f in rows], ['A', 'B', 'C'])
        self.assertAlmostEqual(rows[1]['new_start_kp'], 6, places=8)
        self.assertAlmostEqual(rows[1]['new_end_kp'], 5, places=8)
        self.assertEqual(out['RANGES'].featureCount(), 2)
        self.assertEqual(out['POINTS'].featureCount(), 1)
        line = next(out['RANGES'].getFeatures()).geometry().asPolyline()
        self.assertAlmostEqual(line[0].x(), 501000, places=6)
        self.assertAlmostEqual(line[-1].x(), 502000, places=6)

    def test_table_and_grouped_multipart_use_fields_preserve_geometry(self):
        fields = [QgsField('start_kp', FIELD_TYPE_DOUBLE), QgsField('end_kp', FIELD_TYPE_DOUBLE),
                  QgsField('group', FIELD_TYPE_STRING), QgsField('new_start_kp', FIELD_TYPE_DOUBLE)]
        geom = QgsGeometry.fromWkt('MULTILINESTRING((501000 6000000,501500 6000000),'
                                   '(501500 6000000,502000 6000000))')
        for kind, geoms in [('None', []), ('MultiLineString', [geom])]:
            source = self.layer(kind, fields, [[1, 2, 'group A', 999]], geoms)
            params = dict(OLD=self.old, NEW=self.new, OLD_MODE=1, NEW_MODE=1,
                          INPUT_MODE=0, INPUT=source, START_FIELD='start_kp', END_FIELD='end_kp',
                          STEP=50, MAX_OFFSET=25, STRETCH=10, ROUNDTRIP=1)
            out = self.run_algorithm(params, outputs=('RESULTS', 'RANGES', 'ORIGINAL'))
            row = next(out['RESULTS'].getFeatures())
            self.assertEqual(row['translation_status'], 'accepted')
            self.assertEqual(row['group'], 'group A')
            self.assertEqual(row['new_start_kp'], 999)
            self.assertAlmostEqual(row['new_start_kp_2'], 1, places=8)
            if geoms:
                self.assertEqual(next(out['ORIGINAL'].getFeatures()).geometry().asWkt(), geom.asWkt())

    def test_geometry_and_mixed_crs(self):
        xy = QgsPointXY(501000, 6000000)
        xform = QgsCoordinateTransform(self.old.crs(), QgsCoordinateReferenceSystem('EPSG:4326'), QgsProject.instance())
        points = self.layer('Point', geometries=[QgsGeometry.fromPointXY(xform.transform(xy))], crs='EPSG:4326')
        out = self.translate(INPUT_MODE=2, INPUT=points)
        row = next(out['RESULTS'].getFeatures())
        self.assertEqual(row['translation_status'], 'accepted')
        self.assertAlmostEqual(row['new_start_kp'], 1, places=6)
        # New route stored in geographic coordinates, but measured in the same explicit grid.
        geom = QgsGeometry(next(self.new.getFeatures()).geometry())
        geom.transform(xform)
        new = self.layer('LineString', geometries=[geom], crs='EPSG:4326')
        out = self.translate(NEW=new)
        self.assertAlmostEqual(next(out['RESULTS'].getFeatures())['new_start_kp'], 1, places=6)

    def test_geometry_lines_and_off_route_interior(self):
        line = self.route([(1000, 0), (2000, 0)])
        out = self.translate(INPUT_MODE=2, INPUT=line)
        self.assertEqual(next(out['RESULTS'].getFeatures())['translation_status'], 'accepted')
        detour = self.route([(1000, 0), (1500, 100), (2000, 0)])
        out = self.translate(INPUT_MODE=2, INPUT=detour)
        self.assertIn('input_off_old_route', next(out['RESULTS'].getFeatures())['translation_flags'])

    def test_divergence_reversal_and_target_endpoints_require_review(self):
        for new, text, flag in [
            (self.route([(0, 100), (10000, 100)]), '1\t2', 'offset_exceeded'),
            (self.route([(10000, 0), (0, 0)]), '1\t2', 'reversed'),
            (self.route([(0, 0), (5000, 0)]), '8\t9', 'target_endpoint'),
            (self.route([(0, 0), (4000, 0), (5000, 500), (6000, 0), (10000, 0)]),
             '3\t7', 'offset_exceeded'),
        ]:
            out = self.translate(NEW=new, PASTE_TYPE=1, PASTE=text)
            row = next(out['RESULTS'].getFeatures())
            self.assertEqual(row['translation_status'], 'review')
            self.assertIn(flag, row['translation_flags'])
            self.assertEqual(out['RANGES'].featureCount(), 0)

    def test_disconnected_route_refused_and_seqno_honoured(self):
        g1 = QgsGeometry.fromWkt('LINESTRING(500000 6000000,505000 6000000)')
        g2 = QgsGeometry.fromWkt('LINESTRING(505000 6000000,510000 6000000)')
        route = self.layer('LineString', [QgsField('SeqNo', FIELD_TYPE_INT)], [[2], [1]], [g2, g1])
        out = self.translate(OLD=route)
        self.assertAlmostEqual(next(out['RESULTS'].getFeatures())['new_start_kp'], 1, places=8)
        bad = self.layer('LineString', geometries=[g1, QgsGeometry.fromWkt('LINESTRING(506000 6000000,510000 6000000)')])
        with self.assertRaises(QgsProcessingException):
            self.translate(OLD=bad)

    def test_geodesic_highlighter_and_existing_point_tool_agree(self):
        # A long high-latitude leg exercises the geodesic KP path, not straight degrees.
        route = self.layer('LineString', geometries=[QgsGeometry.fromWkt('LINESTRING(-3 58,0 58)')], crs='EPSG:4326')
        highlighter = self.run_algorithm(
            dict(INPUT_LINE=route, PASTED_RANGES='21,22\n23,23', START_KP=20, DISTANCE_MODE=0),
            KPRangeCSVAlgorithm(), ('OUTPUT', 'OUTPUT_POINTS'))
        out = self.translate(OLD=route, NEW=route, OLD_MODE=0, NEW_MODE=0, OLD_START=20, NEW_START=30,
                             PASTE_TYPE=1, PASTE='21,22\n23,23')
        rows = list(out['RESULTS'].getFeatures())
        self.assertEqual([f['translation_status'] for f in rows], ['accepted', 'accepted'])
        self.assertAlmostEqual(rows[0]['new_start_kp'], 31, places=6)
        ours = next(out['RANGES'].getFeatures()).geometry()
        theirs = next(highlighter['OUTPUT'].getFeatures()).geometry()
        self.assertLess(ours.hausdorffDistance(theirs), 1e-8)
        old_tool = self.run_algorithm(dict(INPUT_SOURCE_LINE=route, INPUT_TARGET_POINTS=highlighter['OUTPUT_POINTS'],
                                           START_KP=30), TranslateKPFromRPLToRPLAlgorithm(), ('OUTPUT_POINTS',))
        self.assertAlmostEqual(next(old_tool['OUTPUT_POINTS'].getFeatures())['design_route_kp'],
                               rows[1]['new_start_kp'], places=3)
