"""End-to-end acquired burial UI, route projection, PDF and remedial integration."""
import json
import hashlib
import time
import tempfile
import unittest
from pathlib import Path

from qgis.core import (QgsFeature, QgsGeometry, QgsProject, QgsVectorFileWriter, QgsVectorLayer)
from qgis.PyQt.QtCore import QCoreApplication

from ..burial import operations
from ..burial.operations_controller import OperationsController
from ..burial.operations_dialogs import MappingDialog
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore
from ..burial.tabs.operations_tabs import AcquiredDataTab, AssessmentTab, ReportingTab
from ..laydata import burial_report as report

REQUIRES_QGIS = True


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.project = QgsProject.instance()
        self.saved_path, _ = self.project.readEntry('SubseaCableTools', 'burial_operations_gpkg', '')
        self.project.writeEntry('SubseaCableTools', 'burial_operations_gpkg', str(self.folder / 'operations.gpkg'))
        layer = QgsVectorLayer('LineString?crs=EPSG:4326', 'Design', 'memory')
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromWkt('LINESTRING(0 0, 0.001 0)'))
        layer.dataProvider().addFeatures([feature])
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = 'GPKG'
        options.layerName = 'route'
        path = str(self.folder / 'route.gpkg')
        QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, self.project.transformContext(), options)
        self.plans = BurialStore(str(self.folder / 'plans.gpkg'))
        self.plans.migrate()
        self.model = PlanModel(self.plans)
        self.model.create_plan('Test cable', 'plough')
        self.model.update_plan({'rpl_name': 'Design', 'rpl_revision': 'A', 'rpl_gpkg_path': path + '|layername=route',
                                'scope_start_kp': 0.0, 'scope_end_kp': .1, 'target_burial_m': 1.5})
        self.controller = OperationsController(self.model)
        self.controller.store(True)
        self.controller.refresh()
        self.errors = []
        # Execute real worker work synchronously for deterministic integration tests.
        class Task:
            def isCanceled(self):
                return False
            def setProgress(self, _):
                pass
        def start(title, work, callback=None):
            result = work(Task())
            self.controller.refresh()
            if callback:
                callback(result)
        self.controller.start = start

    def tearDown(self):
        self.controller.cancel()
        self.model.close_plan()
        self.plans.close()
        for layer in list(self.project.mapLayers().values()):
            if self.tmp.name in layer.source():
                self.project.removeMapLayer(layer.id())
        self.project.writeEntry('SubseaCableTools', 'burial_operations_gpkg', self.saved_path)
        QCoreApplication.processEvents()
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass  # Windows OGR may retain a route handle until Qt deletes widgets.

    def prepare(self):
        content = b'time,kp,depth,pitch,roll,tension\n2026-01-01T00:00:00Z,0,1,1,2,10\n2026-01-01T00:00:20Z,0.020,2,3,4,20\n'
        spec = {'mapping': {k: k for k in ['time', 'kp', 'depth', 'pitch', 'roll', 'tension']}, 'definition': 'Depressor position'}
        identity, _ = self.controller.store().ingest(content, 'day1', 'day1.csv', spec)
        self.controller.refresh()
        self.controller.process(identity, {'kp_mode': 'supplied', 'supplied_kp_confirmed': True})
        self.controller.build_view([self.controller.revisions[-1]['id']], {}, 1, 'sample')
        return identity

    def test_route_projection_uses_design_distance(self):
        self.assertIsNotNone(self.model.route)
        project = operations.RouteProjector(self.model, 'EPSG:4326')
        kp, offset, flags = project(.0005, .00001)
        self.assertAlmostEqual(kp, .0556597, places=5)
        self.assertGreater(offset, 1)
        self.assertEqual(flags, [])

    def test_report_issue_and_shared_layers(self):
        self.prepare()
        self.assertFalse(self.controller.stale())
        destination = self.folder / 'Daily.pdf'
        identity = self.controller.export(str(destination))
        self.assertTrue(destination.read_bytes().startswith(b'%PDF'))
        self.assertGreater(destination.stat().st_size, 1000)
        self.assertIn('contributor_ids', destination.with_suffix('.csv').read_text())
        manifest = json.loads(destination.with_suffix('.json').read_text())
        self.assertEqual(manifest['issue_id'], identity)
        self.assertEqual(manifest['listing_sha256'], hashlib.sha256(destination.with_suffix('.csv').read_bytes()).hexdigest())
        saved = self.controller.store().issue(identity)
        self.assertEqual(saved['snapshot']['composite'], self.controller.snapshot['composite'])
        self.controller.add_layers()
        layers = [v for v in self.project.mapLayers().values() if 'bd_current_samples' in v.source()]
        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0].featureCount(), 2)
        with self.assertRaises(ValueError):
            self.controller.export(str(destination))

    def test_three_tabs_and_preview(self):
        self.prepare()
        class Dock:
            def highlight_kp(self, kp):
                pass
        tabs = [AcquiredDataTab(self.controller), AssessmentTab(self.controller, Dock()), ReportingTab(self.controller)]
        self.addCleanup(lambda: [t.deleteLater() for t in tabs])
        tabs[1].render()
        tabs[2].preview()
        self.assertEqual(len(tabs[1]._plots), 3)
        self.assertTrue(tabs[2].svg.renderer().isValid())
        tabs[2].title.setText('Daily Burial Graph')
        tabs[2].apply()
        self.assertEqual(self.controller.snapshot['template']['title'], 'Daily Burial Graph')

    def test_remedial_reuses_plan_builder(self):
        self.prepare()
        parent_id = self.model.plan_id
        identity = operations.create_remedial_plan(self.model, [(.002, .008), (.007, .012)],
                                                   {'assessment_id': 'test-assessment'}, 'Remedial')
        self.assertNotEqual(identity, parent_id)
        plan = self.plans.get_plan(identity)
        provenance = json.loads(plan['params_json'])['remedial']
        self.assertEqual(provenance['parent_plan_id'], parent_id)
        self.assertEqual(provenance['ranges'], [[.002, .012]])
        self.assertEqual(len(self.plans.list_events(identity)), 2)
        self.assertEqual(self.model.plan_id, parent_id)

    def test_stale_after_new_source_revision(self):
        source = self.prepare()
        store = self.controller.store()
        name, content = store.original(source)
        spec = store.imports()[0]['spec']
        store.ingest(content.replace(b',2,3,4,20', b',3,3,4,20'), 'day1', name, spec)
        self.controller.refresh()
        self.assertTrue(self.controller.stale())

    def test_mapped_import_dialog(self):
        dialog = MappingDialog(['Time', 'Latitude', 'Longitude', 'Burial Depth'], 'Plough')
        self.addCleanup(dialog.deleteLater)
        dialog.accept()
        self.assertEqual(dialog.value['mapping']['depth'], 'Burial Depth')
        self.assertEqual(dialog.value['mapping']['x'], 'Longitude')
        self.assertEqual(dialog.value['definition'], 'Depressor position')

    def test_pdf_pagination(self):
        self.prepare()
        template = dict(report.DEFAULT_TEMPLATE, page_span=.005)
        plots = self.controller.snapshot['plots']
        path = self.folder / 'Paged.pdf'
        count = operations.export_pdf(path, plots, template)
        self.assertEqual(count, 4)
        self.assertTrue(path.read_bytes().startswith(b'%PDF'))

    def test_real_background_task_and_cancel(self):
        del self.controller.start  # use the real QgsTask orchestration
        result = []
        self.controller.start('Background test', lambda task: 42, result.append)
        deadline = time.monotonic() + 10
        while self.controller.task and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(.01)
        self.assertEqual(result, [42])
        def work(task):
            while not task.isCanceled():
                time.sleep(.01)
            raise InterruptedError('Cancelled')
        self.controller.start('Cancellation test', work, result.append)
        self.controller.cancel()
        for _ in range(20):
            QCoreApplication.processEvents()
            time.sleep(.01)
        self.assertEqual(result, [42])

    def test_position_preview_and_original_listing(self):
        content = b'time,x,y,depth\n2026-01-01T00:00:00Z,0,0.00001,1\n2026-01-01T00:00:20Z,0.0001,0.00001,2\n'
        spec = {'mapping': {k: k for k in ['time', 'x', 'y', 'depth']}, 'definition': 'Depressor position', 'crs': 'EPSG:4326'}
        identity, _ = self.controller.store().ingest(content, 'nav', 'nav.csv', spec)
        self.controller.refresh()
        self.controller.process(identity, {'kp_mode': 'position', 'reason': 'Calibration',
                                          'edits': [{'start_kp': 0, 'end_kp': 1, 'offset': .5}]})
        revision = self.controller.revisions[-1]
        self.controller.navigation_layers(revision)
        layers = [v for v in self.project.mapLayers().values() if v.customProperty('subsea/burial_processing_id') == revision['id']]
        self.assertEqual(len(layers), 2)
        self.assertTrue(all(v.featureCount() == 2 for v in layers))
        self.controller.build_view([revision['id']], {}, 1, 'sample', 'original')
        self.assertEqual(self.controller.snapshot['samples'][0]['channels']['depth'], 1)
        for layer in layers:
            self.project.removeMapLayer(layer.id())

    def test_time_axis_and_event(self):
        self.prepare()
        self.controller.template['axis'] = 'time'
        self.controller.build_view([self.controller.revisions[-1]['id']], {}, 1, 'sample')
        class Dock:
            def highlight_kp(self, kp):
                pass
        tab = AssessmentTab(self.controller, Dock())
        self.addCleanup(tab.deleteLater)
        tab.render()
        axis = tab._plots[0][0].getAxis('bottom')
        self.assertIn('01 Jan', axis.tickStrings([self.controller.snapshot['rows'][0]['time']], 1, 1)[0])
